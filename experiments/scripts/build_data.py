#!/usr/bin/env python3
"""Build the shipped records, the stopkit package and data/MANIFEST.json from the source archive.

    python3 scripts/build_data.py            # write the files and the manifest
    python3 scripts/build_data.py --check    # rebuild in memory and compare with the tree

The source archive, experiments/data/source/archive.zip, is not part of the repository, so this script runs only
where it is present. Every file in it is accounted for in the manifest: shipped under its path here,
written into one of this repository's documents or scripts, or not shipped, each with a reason.

Recorded files ship byte for byte apart from the substitutions in SUBSTITUTIONS. Each keeps the length
of what it replaces, so sizes and offsets stay as recorded; archives are rewritten member by member and
SQLite write-ahead logs get their frame checksums recomputed. Every SHA-256 that a record cites for a
changed file is then rewritten to the new value, wherever it is cited, until no citation is stale. The
manifest gives each changed file's SHA-256 as shipped and the kinds of change it received. It holds no
replaced string and no digest of one, nor the digest a changed file had: a digest of a short value, such as
a project number or an address, gives the value back to anyone who tries every candidate. A rebuild with
--check, which needs the archive, is the proof. The script writes nothing and exits nonzero on any
disagreement.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "experiments" / "data" / "source" / "archive.zip"
MANIFEST = ROOT / "experiments" / "data" / "MANIFEST.json"

# Source prefix -> path in this repository. The first matching prefix wins.
PLACES = [
    ("stopkit/validation/", "validation/"),
    ("experiments/n8n-campaign/", "experiments/n8n-queue-mode/"),
    ("experiments/temporal-campaign/", "experiments/temporal-activities/"),
    ("experiments/langgraph-round1/", "experiments/langgraph-controls/"),
    ("experiments/n8n-finding1/", "experiments/n8n-restart/"),
    ("experiments/openclaw-spike/", "experiments/openclaw-commands/"),
    ("experiments/claude-code-codex/", "experiments/claude-code-codex/"),
    ("harness/stopcheck-7a3fcec/", "experiments/harness/n8n-langgraph-7a3fcec/"),
    ("harness/", "experiments/harness/"),
    ("setup/n8n-2.41.7/", "experiments/setup/n8n-2.41.7/"),
]

# Folders of the runs of 29 October, named in this repository for what they hold. Applied to a placed path, to every
# segment that matches exactly.
FOLDERS = {
    "practice-run-2026-10-29": "run-2026-10-29",
    "fresh-vm-run-2026-10-29": "vm-run-2026-10-29",
    "2026-10-29-laptop": "2026-10-29-local",
    "2026-10-29-laptop-from-repo": "2026-10-29-local-from-repo",
}

# Source files this repository does not ship, by exact path or by prefix (a trailing slash). An exact entry here
# or in WRITTEN_INTO wins over a prefix.
NOT_SHIPPED = {
    "CITATION.cff": "citation metadata",
    "stopkit/CITATION.cff": "citation metadata",
    "harness/stopcheck-history.bundle": (
        "the working repository's history; the files the records name from its commits are under experiments/harness/"
    ),
    # The kit's code, tests and examples at the source commit. This repository carries the kit as maintained code at
    # its root, not as records.
    "stopkit/stopkit/": "an earlier version of the kit's code; the kit is maintained under src/",
    "stopkit/tests/": "an earlier version of the kit's tests; the kit's tests are maintained under tests/",
    "stopkit/examples/": "an earlier version of the kit's examples; they are maintained under examples/",
    "docs/SLIDE-EVIDENCE.md": "line references into the records; the pages in docs/walkthroughs take their place",
    "docs/slide-evidence.json": "line references into the records; the pages in docs/walkthroughs take their place",
    "experiments/n8n-campaign/records/cb1013/RESULTS-cb1013.md": (
        "a results write-up; the documents in docs/ take its place"
    ),
    "experiments/temporal-campaign/records/cs1014/RESULTS-cs1014.md": (
        "a results write-up; the documents in docs/ take its place"
    ),
    "experiments/openclaw-spike/records/RESULTS-openclaw-spike.md": (
        "a results write-up; the documents in docs/ take its place"
    ),
    "harness/stopcheck-7a3fcec/.gitattributes": (
        "the working repository's line-ending rule; this repository's .gitattributes keeps every file byte for byte"
    ),
}

# The works the shipped files and this repository's documents cite. Each entry's pattern finds the shipped files that
# cite it; the manifest lists them under "cited_by".
CITATIONS = [
    {
        "id": "arXiv:2607.14166",
        "title": "Stop Means Stop: Measuring and Repairing the Enforcement Gap in Agent-Framework Control Primitives",
        "authors": ["Sajjad Khan"],
        "year": 2026,
        "version": "v3, 8 August 2026",
        "url": "https://arxiv.org/abs/2607.14166",
        "pattern": r"2607\.14166|\bKhan\b",
    },
    {
        "id": "anthropics/claude-code#104714",
        "title": "an issue report on Claude Code's TaskStop tool",
        "url": "https://github.com/anthropics/claude-code/issues/104714",
        "pattern": r"claude-code(?:#|/issues/)104714",
    },
    {
        "id": "openai/codex#56132",
        "title": "a pull request to the Codex CLI that sends a cancel when a pending tool call is dropped",
        "url": "https://github.com/openai/codex/pull/56132",
        "pattern": r"codex(?:#|/pull/)56132",
    },
    {
        "id": "RFC 7009",
        "title": "OAuth 2.0 Token Revocation",
        "authors": ["T. Lodderstedt", "S. Dronia", "M. Scurtescu"],
        "year": 2013,
        "url": "https://www.rfc-editor.org/rfc/rfc7009",
        "pattern": r"RFC ?7009",
    },
    {
        "id": "RFC 8693",
        "title": "OAuth 2.0 Token Exchange",
        "authors": ["M. Jones", "A. Nadalin", "B. Campbell", "J. Bradley", "C. Mortimore"],
        "year": 2020,
        "url": "https://www.rfc-editor.org/rfc/rfc8693",
        "pattern": r"RFC ?8693",
    },
    {
        "id": "Temporal: Cancellation and Termination",
        "title": "Temporal documentation: Cancellation and Termination; Cancellation - Python SDK",
        "url": "https://docs.temporal.io/encyclopedia/workflow/cancellation-and-termination",
        "pattern": r"docs\.temporal\.io/(?:encyclopedia/workflow/cancellation-and-termination"
        r"|develop/python/cancellation)",
    },
]

# Files taken from commits of the history bundle (which itself is not shipped). The n8n runner's mutation check reads
# the scorer as it was at commit 3832d7c. The LangGraph trials of 9 October ran commit cd26851; ROUND1_FILES are the
# 13 files their runner bound by SHA-256 before the first trial (experiments/langgraph-controls/code/CHANGES.md).
ROUND1_FILES = (
    "common/guards.py",
    "common/mocks.py",
    "langgraph/RULES-expA.md",
    "langgraph/analyze_x.py",
    "langgraph/app/agent_graphs.py",
    "langgraph/app/langgraph.json",
    "langgraph/canary.py",
    "langgraph/harness.py",
    "langgraph/recorder.py",
    "langgraph/run_expA.py",
    "langgraph/run_trial_ns.sh",
    "predictions/langgraph-expA-DRAFT.md",
    "predictions/langgraph-expA-cells.json",
)
FROM_HISTORY = {
    "experiments/harness/n8n-scorer-3832d7c/n8n/analyze_b.py": ("3832d7c", "n8n/analyze_b.py"),
    **{f"experiments/harness/langgraph-cd26851/{p}": ("cd26851", p) for p in ROUND1_FILES},
}
BUNDLE = "harness/stopcheck-history.bundle"

# Source files whose content this repository carries in documents or scripts written for it.
WRITTEN_INTO = {
    "README.md": ["experiments/README.md"],
    ".gitignore": [".gitignore"],
    ".gitattributes": [".gitattributes"],
    "stopkit/.gitignore": [".gitignore"],
    "docs/METHOD.md": ["experiments/docs/METHOD.md"],
    "docs/VERIFICATION.md": ["experiments/RUNBOOK.md"],
    "docs/WHAT-EACH-STOP-REACHES.md": ["experiments/docs/WHAT-EACH-STOP-REACHES.md"],
    "docs/log-walkthroughs/README.md": ["experiments/docs/walkthroughs/README.md"],
    "docs/log-walkthroughs/LANGGRAPH.md": ["experiments/docs/walkthroughs/LANGGRAPH.md"],
    "docs/log-walkthroughs/N8N.md": ["experiments/docs/walkthroughs/N8N.md"],
    "docs/log-walkthroughs/TEMPORAL.md": ["experiments/docs/walkthroughs/TEMPORAL.md"],
    "docs/log-walkthroughs/OPENCLAW.md": ["experiments/docs/walkthroughs/OPENCLAW.md"],
    "docs/log-walkthroughs/lg_walkthrough.py": ["experiments/scripts/walkthrough_langgraph.py"],
    "docs/log-walkthroughs/n8n_walkthrough.py": ["experiments/scripts/walkthrough_n8n.py"],
    "docs/log-walkthroughs/temporal_walkthrough.py": ["experiments/scripts/walkthrough_temporal.py"],
    "docs/log-walkthroughs/openclaw_walkthrough.py": ["experiments/scripts/walkthrough_openclaw.py"],
    "docs/n8n-termination-review/REPORT.md": ["experiments/docs/n8n-loop-evidence/REPORT.md"],
    "docs/n8n-termination-review/rows.jsonl": ["experiments/docs/n8n-loop-evidence/rows.jsonl"],
    "check/run_all_checks.py": ["experiments/scripts/build_results.py"],
    "check/check_pages.py": ["experiments/scripts/check_pages.py"],
    "check/report_n8n_evidence.py": ["experiments/scripts/loop_evidence.py"],
    "check/test_run_all_checks.py": ["experiments/tests/test_checks.py"],
    "check/test_n8n_evidence_report.py": ["experiments/tests/test_loop_evidence.py"],
    "experiments/claude-code-codex/README.md": ["experiments/claude-code-codex/README.md"],
    "experiments/langgraph-demo/README.md": ["examples/langgraph/README.md"],
    "experiments/langgraph-round1/README.md": ["experiments/langgraph-controls/README.md"],
    "experiments/langgraph-round1/code/CHANGES.md": ["experiments/langgraph-controls/code/CHANGES.md"],
    "experiments/n8n-campaign/README.md": ["experiments/n8n-queue-mode/README.md"],
    "experiments/n8n-campaign/HOW-IT-RAN.md": ["experiments/n8n-queue-mode/README.md"],
    "experiments/n8n-finding1/README.md": ["experiments/n8n-restart/README.md"],
    "experiments/n8n-finding1/RESULTS-2026-10-14.md": ["experiments/n8n-restart/README.md"],
    "experiments/openclaw-spike/README.md": ["experiments/openclaw-commands/README.md"],
    "experiments/temporal-campaign/README.md": ["experiments/temporal-activities/README.md"],
    "harness/README.md": ["experiments/harness/README.md"],
    "stopkit/README.md": ["README.md"],
    "stopkit/examples/langgraph/README.md": ["examples/langgraph/README.md"],
    "stopkit/docs/DESIGN.md": ["docs/DESIGN.md"],
    "stopkit/docs/FORMAT.md": ["docs/FORMAT.md"],
    "stopkit/docs/FRAMEWORK.md": ["docs/FRAMEWORK.md"],
    "stopkit/docs/VALIDATION.md": ["docs/VALIDATION.md"],
    "setup/claude-code-codex.sh": ["experiments/setup/claude-code-codex.sh"],
    "setup/clock_tick.py": ["experiments/setup/clock_tick.py"],
    "setup/common.sh": ["experiments/setup/common.sh"],
    "setup/constraints-temporal.txt": ["experiments/setup/constraints-temporal.txt"],
    "setup/langgraph-round1.sh": ["experiments/setup/langgraph-controls.sh"],
    "setup/n8n-campaign.sh": ["experiments/setup/n8n-queue-mode.sh"],
    "setup/openclaw-spike.sh": ["experiments/setup/openclaw-commands.sh"],
    "setup/temporal-campaign.sh": ["experiments/setup/temporal-activities.sh"],
}


def L(text: str) -> str:
    """A pattern that matches `text` literally."""
    return re.escape(text)


# Edits to moved code, applied after it is placed. Each pattern must match exactly `count` times. A file that another
# shipped file cites by its SHA-256 is never edited: the citation names the code a record was made with.
EDITS = {
    "experiments/n8n-restart/run_with_ticks.sh": [(r"\(reproduction of \w+ 1\)", "(stop after a main restart)", 1)],
    "experiments/n8n-restart/run_repeats.sh": [
        (L("setup -> P1 -> teardown"), "setup -> stop after a main restart (p1_stop_after_restart.sh) -> teardown", 1),
        (r"/root/e1\b", "/root/n8n-restart-repeats", 2),
    ],
}

# A home-directory path on the machine that ran the early trials, in the forms the records hold it in:
# with slashes, with backslashes, and without the folder. Matched by its shape, never by its letters.
WORKSPACE = re.compile(
    rb"User[s](?P<sep>/|\\\\|\\)(?P<user>[A-Za-z0-9._-]+)(?:(?P=sep)Desktop(?P=sep)[A-Za-z0-9._-]+)?"
)
# The cloud project, learned from the internal host names of its machines.
PROJECT_HOST = re.compile(rb"\.c\.(?P<id>[a-z][a-z0-9-]{4,28}[a-z0-9])\.internal\b")
PROJECT_NUMBER = re.compile(rb"\bprojects/(?P<num>\d{6,})\b")
# The worker machines, as listed in the batch's campaign.json.
MACHINE = re.compile(rb'"ip":\s*"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})",\s*"instance_id":\s*"(?P<id>\d{12,20})"')


# The shortest part of a learned name that the build replaces, and that the residue check refuses, on its own.
FRAGMENT = 8


def same_length(word: str, n: int, pad: str = "-") -> bytes:
    """`word` cut or padded to exactly n characters."""
    return (word + pad * n)[:n].encode()


@dataclass
class Substitution:
    key: str
    description: str
    before: bytes = rb"(?<![0-9A-Za-z])"  # what may not precede a replaced string
    after: bytes = rb"(?![0-9A-Za-z])"  # what may not follow it
    found: dict[bytes, bytes] = field(default_factory=dict)  # replaced string -> its replacement
    occurrences: int = 0
    files: set[str] = field(default_factory=set)
    used: set[bytes] = field(default_factory=set)  # the replaced strings that occurred

    def pattern(self) -> re.Pattern | None:
        if not self.found:
            return None
        alts = sorted(self.found, key=len, reverse=True)
        return re.compile(self.before + b"(?:" + b"|".join(re.escape(a) for a in alts) + b")" + self.after)


def workspace_replacement(m: re.Match) -> bytes:
    sep = m.group("sep")
    n = len(m.group(0))
    head = b"work" + sep
    return head + same_length("agent-shutdown-test", n - len(head))


def learn(source: dict[str, bytes], top: str) -> list[Substitution]:
    """Find, by shape, the identifying strings the substitutions replace."""
    word = rb"(?<![0-9A-Za-z-])", rb"(?![0-9A-Za-z-])"
    digits = rb"(?<!\d)", rb"(?!\d)"
    project = Substitution("cloud-project", "the cloud project's name, in its machines' internal host names", *word)
    number = Substitution("cloud-project-number", "the cloud project's number", *digits)
    address = Substitution(
        "machine-address", "the public addresses of the six cloud machines", rb"(?<![\d.])", rb"(?!\.?\d)"
    )
    instance = Substitution("machine-instance", "the instance numbers of the six cloud machines", *digits)
    for path, blob in source.items():
        for m in PROJECT_HOST.finditer(blob):
            project.found.setdefault(m.group("id"), same_length("shutdown-test-project-00", len(m.group("id"))))
        for m in PROJECT_NUMBER.finditer(blob):
            num = m.group("num")
            number.found.setdefault(num, b"1" + b"0" * (len(num) - 2) + b"1")
        if not path.endswith("/campaign.json"):
            continue
        for m in MACHINE.finditer(blob):
            ip, iid = m.group("ip"), m.group("id")
            if ip not in address.found:
                # A documentation address (RFC 5737) of the same length, one per machine.
                k = len(address.found) + 1
                last = {12: str(k), 13: str(10 + k), 14: str(100 + k)}.get(len(ip))
                if last is None:
                    raise SystemExit(f"no same-length documentation address for an address of {len(ip)} characters")
                address.found[ip] = f"198.51.100.{last}".encode()
            instance.found.setdefault(iid, b"1" + str(len(instance.found) + 1).encode().rjust(len(iid) - 1, b"0"))
    # The folder name of the repository the later runs were made from: the archive's top folder without the branch
    # suffix GitHub adds, matched by its words, never by its letters.
    folder = Substitution("repository-folder", "the folder name of the repository the later runs were made from", *word)
    words = [w for w in re.split(r"[^0-9A-Za-z]+", re.sub(r"-main$", "", top)) if w]
    spelled = re.compile(r"(?<![0-9A-Za-z])" + r"[-_\s]*".join(map(re.escape, words)) + r"(?![0-9A-Za-z])", re.I)
    for blob in source.values():
        for m in spelled.finditer(blob.decode("latin-1")):
            name = m.group(0).encode("latin-1")
            folder.found.setdefault(name, same_length("shutdown-experiments-dir", len(name)))
    if len(project.found) != 1 or len(number.found) != 1 or len(address.found) != 6 or len(instance.found) != 6:
        raise SystemExit(
            f"expected one project, one project number and six machines; found {len(project.found)}, "
            f"{len(number.found)}, {len(address.found)} addresses and {len(instance.found)} instances"
        )
    # Inside a database a host name can be cut off where a page or a record ends, so that only the start of the
    # project's name follows ".c.". Every start of at least FRAGMENT characters is replaced by the same-length start
    # of its replacement.
    fragment = Substitution(
        "cloud-project-fragment",
        "the start of the cloud project's name, where a database page ends inside a host name",
        rb"(?<=\.c\.)",
        b"",
    )
    for name, replacement in project.found.items():
        for n in range(FRAGMENT, len(name)):
            fragment.found[name[:n]] = replacement[:n]
    return [project, fragment, number, address, instance, folder]


class Scrubber:
    def __init__(self, learned: list[Substitution]):
        self.workspace = Substitution("home-path", "a home-directory path on the machine that ran the early trials")
        self.subs = [self.workspace, *learned]

    def apply(self, blob: bytes, where: str) -> bytes:
        def ws(m: re.Match) -> bytes:
            r = workspace_replacement(m)
            self.workspace.found.setdefault(m.group(0), r)
            self.workspace.occurrences += 1
            self.workspace.files.add(where)
            return r

        out = WORKSPACE.sub(ws, blob)
        for s in self.subs[1:]:
            rx = s.pattern()
            if rx is None:
                continue

            def rep(m: re.Match, s: Substitution = s) -> bytes:
                s.occurrences += 1
                s.files.add(where)
                s.used.add(m.group(0))
                return s.found[m.group(0)]

            out = rx.sub(rep, out)
        if len(out) != len(blob):
            raise SystemExit(f"{where}: a substitution changed the length")
        return out

    def residue(self, blob: bytes) -> list[str]:
        """Names of the substitutions whose originals still occur in `blob`.

        For the cloud project's name and number, any start or end of at least FRAGMENT characters counts too, so a
        copy cut off at a page boundary, at either end, is found.
        """
        left = []
        if WORKSPACE.search(blob):
            left.append(self.workspace.key)
        for s in self.subs[1:]:
            if any(original in blob for original in s.found):
                left.append(s.key)
            elif s.key in ("cloud-project", "cloud-project-number") and any(
                part in blob
                for original in s.found
                for n in range(FRAGMENT, len(original))
                for part in (original[:n], original[-n:])
            ):
                left.append(s.key + " (a part of it)")
        return left


# ----------------------------------------------------------------------------- containers


def gzip_parts(blob: bytes) -> tuple[bytes, bytes, int]:
    """(header, payload, compression level) of a single-member gzip file."""
    if blob[:2] != b"\x1f\x8b" or blob[2] != 8:
        raise SystemExit("not a gzip file")
    flags, pos = blob[3], 10
    if flags & 4:
        pos += 2 + int.from_bytes(blob[pos : pos + 2], "little")
    for bit in (8, 16):
        if flags & bit:
            pos = blob.index(b"\0", pos) + 1
    if flags & 2:
        pos += 2
    d = zlib.decompressobj(-15)
    payload = d.decompress(blob[pos:])
    if len(d.unused_data) != 8:
        raise SystemExit("a gzip file with more than one member")
    return blob[:pos], payload, {2: 9, 4: 1}.get(blob[8], 6)


def gzip_join(header: bytes, payload: bytes, level: int) -> bytes:
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    body = c.compress(payload) + c.flush()
    return header + body + struct.pack("<II", zlib.crc32(payload) & 0xFFFFFFFF, len(payload) & 0xFFFFFFFF)


def rewrite_tar(tar: bytes, transform, where: str) -> bytes:
    """Apply `transform` to every regular member; same-length results are patched in place."""
    out = bytearray(tar)
    resized = False
    members = []
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as t:
        for m in t.getmembers():
            for text in (m.name, m.linkname, m.uname, m.gname):
                if WORKSPACE.search(text.encode()):
                    raise SystemExit(f"{where}: an archive member's header holds a substitution")
            data = t.extractfile(m).read() if m.isfile() else None
            members.append((m, data))
    rebuilt = []
    for m, data in members:
        if data is None:
            rebuilt.append((m, None))
            continue
        new = transform(data, f"{where}::{m.name}")
        if len(new) != len(data):
            resized = True
        elif new != data:
            out[m.offset_data : m.offset_data + m.size] = new
        rebuilt.append((m, new))
    if not resized:
        return bytes(out)
    fmt = tarfile.GNU_FORMAT if tar[257:265] == b"ustar  \0" else tarfile.PAX_FORMAT
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:", format=fmt) as t:
        for m, data in rebuilt:
            if data is not None:
                m.size = len(data)
                t.addfile(m, io.BytesIO(data))
            else:
                t.addfile(m)
    return buf.getvalue()


def wal_valid_frames(wal: bytes) -> int:
    """Number of frames at the start of a SQLite write-ahead log whose checksums hold."""
    magic = int.from_bytes(wal[:4], "big")
    if magic not in (0x377F0682, 0x377F0683):
        raise SystemExit("not a SQLite write-ahead log")
    s0, s1 = wal_sum(wal[:24], 0, 0, magic & 1)
    if (s0, s1) != struct.unpack(">II", wal[24:32]):
        return 0
    page = int.from_bytes(wal[8:12], "big")
    n, pos = 0, 32
    while pos + 24 + page <= len(wal) and wal[pos + 8 : pos + 16] == wal[16:24]:
        s0, s1 = wal_sum(wal[pos : pos + 8] + wal[pos + 24 : pos + 24 + page], s0, s1, magic & 1)
        if (s0, s1) != struct.unpack(">II", wal[pos + 16 : pos + 24]):
            break
        n, pos = n + 1, pos + 24 + page
    return n


def wal_sum(data: bytes, s0: int, s1: int, big: int) -> tuple[int, int]:
    words = struct.unpack((">" if big else "<") + "I" * (len(data) // 4), data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def wal_resum(old: bytes, new: bytes) -> bytes:
    """`new` with the checksums of the frames that were valid in `old` recomputed."""
    frames = wal_valid_frames(old)
    magic = int.from_bytes(new[:4], "big")
    out = bytearray(new)
    s0, s1 = wal_sum(bytes(out[:24]), 0, 0, magic & 1)
    page = int.from_bytes(out[8:12], "big")
    pos = 32
    for _ in range(frames):
        s0, s1 = wal_sum(bytes(out[pos : pos + 8] + out[pos + 24 : pos + 24 + page]), s0, s1, magic & 1)
        out[pos + 16 : pos + 24] = struct.pack(">II", s0, s1)
        pos += 24 + page
    if wal_valid_frames(bytes(out)) != frames:
        raise SystemExit("the write-ahead log's checksums do not hold after the rewrite")
    return bytes(out)


def is_wal(path: str) -> bool:
    return path.endswith("-wal") or "-wal__" in path


def transform_blob(blob: bytes, path: str, scrub: Scrubber) -> bytes:
    """Apply the substitutions to a file, inside gzip and tar containers and keeping SQLite logs valid."""
    name = path.rsplit("::", 1)[-1]
    if name.endswith((".tgz", ".tar.gz")):
        header, tar, level = gzip_parts(blob)
        new = rewrite_tar(tar, lambda d, w: transform_blob(d, w, scrub), path)
        return blob if new == tar else gzip_join(header, new, level)
    if name.endswith(".gz"):
        header, payload, level = gzip_parts(blob)
        new = transform_blob(payload, path + "::gunzip", scrub)
        return blob if new == payload else gzip_join(header, new, level)
    if name.endswith(".tar"):
        return rewrite_tar(blob, lambda d, w: transform_blob(d, w, scrub), path)
    new = scrub.apply(blob, path)
    if new != blob and is_wal(name):
        new = wal_resum(blob, new)
    return new


def leaves(blob: bytes, path: str):
    """Yield (path, bytes) for a file and, recursively, every member of the containers inside it."""
    yield path, blob
    name = path.rsplit("::", 1)[-1]
    if name.endswith((".tgz", ".tar.gz", ".tar")):
        tar = gzip_parts(blob)[1] if name.endswith((".tgz", ".tar.gz")) else blob
        with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as t:
            for m in t.getmembers():
                if m.isfile():
                    yield from leaves(t.extractfile(m).read(), f"{path}::{m.name}")
    elif name.endswith(".gz"):
        yield from leaves(gzip_parts(blob)[1], f"{path}::gunzip")


# ----------------------------------------------------------------------------- the build


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def is_text(b: bytes) -> bool:
    return b"\0" not in b[:65536]


def place(src: str) -> str | None:
    for old, new in PLACES:
        if src.startswith(old):
            dest = new + src[len(old) :]
            return "/".join(FOLDERS.get(part, part) for part in dest.split("/"))
    return None


def load_source() -> tuple[dict[str, bytes], str]:
    """The archive's files by path, and the name of its single top-level folder."""
    with zipfile.ZipFile(ARCHIVE) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        top = infos[0].filename.split("/")[0] + "/"
        if not all(i.filename.startswith(top) for i in infos):
            raise SystemExit("the archive does not hold a single top-level folder")
        return {i.filename[len(top) :]: z.read(i) for i in infos}, top.rstrip("/")


def from_history(bundle: bytes) -> dict[str, bytes]:
    """The FROM_HISTORY files, read from the history bundle with git."""
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "history.bundle").write_bytes(bundle)
        clone = Path(tmp) / "repo"
        subprocess.run(
            ["git", "clone", "-q", "--no-checkout", str(Path(tmp) / "history.bundle"), str(clone)], check=True
        )
        for dest, (commit, path) in FROM_HISTORY.items():
            show = subprocess.run(
                ["git", "-C", str(clone), "show", f"{commit}:{path}"], capture_output=True, check=True
            )
            out[dest] = show.stdout
    return out


def apply_edits(path: str, blob: bytes) -> tuple[bytes, list[dict]]:
    """The file with its EDITS applied, and for each edit its count and the SHA-256 of what it replaced."""
    text = blob.decode("utf-8")
    applied = []
    for pattern, replacement, count in EDITS[path]:
        replaced = [m.group(0) for m in re.finditer(pattern, text)]
        text, n = re.subn(pattern, replacement, text)
        if n != count:
            raise SystemExit(f"{path}: an edit applied {n} times, not {count}")
        applied.append({"count": n, "replaced_sha256": sorted({sha(r.encode()) for r in replaced})})
    return text.encode("utf-8"), applied


def digests(blob: bytes) -> list[bytes]:
    """The SHA-256 of a file as recorded files cite it: of its bytes and, for text, with CRLF line ends."""
    out = [sha(blob).encode()]
    if is_text(blob):
        out.append(sha(blob.replace(b"\n", b"\r\n")).encode())
    return out


HEX = re.compile(rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


def refuse_cited_edits(files: dict[str, bytes], edits) -> None:
    """Exit if an edited file is one that another file cites by its SHA-256.

    Rewriting the citation would make the record name code that never ran, so only the substitutions may change
    such a file.
    """
    cited = {d for b in files.values() for d in HEX.findall(b)}
    for path in edits:
        if set(digests(files[path])) & cited:
            raise SystemExit(f"{path}: another shipped file cites it by its SHA-256, so it is not edited")


def rewrite_citations(files: dict[str, bytes], original: dict[str, bytes]) -> tuple[int, set[str]]:
    """Rewrite every cited SHA-256 of a changed file, until no citation of an earlier digest remains."""
    unchanged = {d for p, b in files.items() if b == original[p] for d in digests(b)}
    for path, blob in files.items():
        if blob != original[path] and set(digests(original[path])) & unchanged:
            raise SystemExit(f"{path}: changed, but another shipped file has its original content")
    tokens = {p: set(HEX.findall(b)) for p, b in files.items()}
    cited = {p for p, t in tokens.items() if t}
    versions = {p: [digests(original[p])] for p, b in files.items() if b != original[p]}
    rewritten, touched = 0, set()
    for _ in range(20):
        mapping: dict[bytes, bytes] = {}
        for path in list(versions):
            now = digests(files[path])
            if versions[path][-1] != now:
                versions[path].append(now)
            for earlier in versions[path][:-1]:
                mapping.update(zip(earlier, now, strict=True))
        due = [p for p in cited if tokens[p] & mapping.keys()]
        if not due:
            return rewritten, touched
        for path in due:
            if not is_text(files[path]):
                raise SystemExit(f"{path}: a binary file cites the digest of a changed file")
            new = HEX.sub(lambda m, table=mapping: table.get(m.group(0), m.group(0)), files[path])
            rewritten += sum(1 for m in HEX.finditer(files[path]) if m.group(0) in mapping)
            touched.add(path)
            files[path] = new
            tokens[path] = set(HEX.findall(new))
            if path not in versions:
                versions[path] = [digests(original[path])]
    raise SystemExit("digest citations did not settle after 20 passes")


def check_databases(files: dict[str, bytes], original: dict[str, bytes]) -> int:
    """Every SQLite database that changed, or whose log changed, still passes its integrity check."""
    checked = 0
    for path, blob in files.items():
        if not blob.startswith(b"SQLite format 3\0"):
            continue
        folder, base = path.rsplit("/", 1)
        stem = base.split("__")[0]
        wal = next(
            (p for p in files if p.startswith(folder + "/") and p.rsplit("/", 1)[1].startswith(stem + "-wal")), None
        )
        if blob == original[path] and (wal is None or files[wal] == original[wal]):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "x.db"
            db.write_bytes(blob)
            if wal:
                (Path(tmp) / "x.db-wal").write_bytes(files[wal])
            con = sqlite3.connect(db)
            result = con.execute("pragma integrity_check").fetchone()[0]
            con.close()
        if result != "ok":
            raise SystemExit(f"{path}: integrity check after the rewrite: {result}")
        checked += 1
    return checked


def build() -> tuple[dict[str, bytes], dict]:
    source, top = load_source()
    files: dict[str, bytes] = {}
    from_src: dict[str, str] = {}
    not_shipped, written = {}, {}
    for src in sorted(source):
        if src in NOT_SHIPPED:
            not_shipped[src] = {"sha256": sha(source[src]), "bytes": len(source[src]), "reason": NOT_SHIPPED[src]}
            continue
        if src in WRITTEN_INTO:
            written[src] = {"sha256": sha(source[src]), "bytes": len(source[src]), "into": WRITTEN_INTO[src]}
            continue
        prefix = next((p for p in NOT_SHIPPED if p.endswith("/") and src.startswith(p)), None)
        if prefix:
            not_shipped[src] = {"sha256": sha(source[src]), "bytes": len(source[src]), "reason": NOT_SHIPPED[prefix]}
            continue
        dest = place(src)
        if dest is None:
            raise SystemExit(
                f"{src}: the source file is neither placed, written into a document, nor listed as not shipped"
            )
        files[dest] = source[src]
        from_src[dest] = src
    for name in [*NOT_SHIPPED, *WRITTEN_INTO]:
        if name not in source and not (name.endswith("/") and any(s.startswith(name) for s in source)):
            raise SystemExit(f"{name}: listed, but not in the archive")
    for dest, blob in from_history(source[BUNDLE]).items():
        commit, path = FROM_HISTORY[dest]
        files[dest] = blob
        from_src[dest] = f"{BUNDLE} {commit}:{path}"

    original = dict(files)
    refuse_cited_edits(original, EDITS)
    edit_log = {}
    for path in EDITS:
        files[path], edit_log[path] = apply_edits(path, files[path])
    edited = {p for p in EDITS}

    scrub = Scrubber(learn(source, top))
    for path in sorted(files):
        files[path] = transform_blob(files[path], path, scrub)
    for path, blob in files.items():
        if len(blob) != len(original[path]) and path not in edited and not path.endswith((".tgz", ".tar.gz", ".gz")):
            raise SystemExit(f"{path}: the size changed")

    before = {p: b for p, b in files.items()}
    rewritten, touched = rewrite_citations(files, original)
    for path in touched:
        if len(files[path]) != len(before[path]):
            raise SystemExit(f"{path}: rewriting digests changed the size")

    for path, blob in files.items():
        for leaf, data in leaves(blob, path):
            left = scrub.residue(data)
            if left:
                raise SystemExit(f"{leaf}: still holds {left}")
    for path, blob in files.items():
        if blob != original[path] and re.search(r"(?<![0-9a-f])" + sha(original[path]) + r"(?![0-9a-f])", path):
            raise SystemExit(f"{path}: the file is named by its own digest and its content changed")
    databases = check_databases(files, original)

    changes: dict[str, list[str]] = {p: [] for p in files}
    for s in scrub.subs:
        for where in s.files:
            top = where.split("::")[0]
            if s.key not in changes[top]:
                changes[top].append(s.key)
    for p in touched:
        changes[p].append("digest-citations")
    for p in edited:
        changes[p].insert(0, "edited")

    manifest = {
        "source": {
            "archive": "experiments/data/source/archive.zip",
            "sha256": sha(ARCHIVE.read_bytes()),
            "files": len(source),
            "bytes": sum(len(b) for b in source.values()),
        },
        "substitutions": {
            s.key: {
                "description": s.description,
                "occurrences": s.occurrences,
                "files": len({w.split("::")[0] for w in s.files}),
            }
            for s in scrub.subs
        },
        "edits": {p: edit_log[p] for p in sorted(EDITS)},
        "citations": [
            {
                **{k: v for k, v in c.items() if k != "pattern"},
                "cited_by": sorted(
                    p
                    for p, b in files.items()
                    if is_text(b) and re.search(c["pattern"].encode(), b) and not p.endswith((".log", ".jsonl"))
                ),
            }
            for c in CITATIONS
        ],
        "digest_citations_rewritten": rewritten,
        "databases_checked": databases,
        "files": {},
        "written_into": written,
        "not_shipped": not_shipped,
    }
    for path in sorted(files):
        entry = {"from": from_src[path], "sha256": sha(files[path]), "bytes": len(files[path])}
        if files[path] != original[path]:
            entry["changes"] = sorted(set(changes[path]), key=changes[path].index)
        manifest["files"][path] = entry
    return files, manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="compare a rebuild with the tree; write nothing")
    args = parser.parse_args()
    if not ARCHIVE.is_file():
        print(f"{ARCHIVE.relative_to(ROOT)} is not present; nothing to build", file=sys.stderr)
        return 1
    files, manifest = build()
    text = json.dumps(manifest, indent=1, sort_keys=False) + "\n"
    if args.check:
        stale = [p for p, b in files.items() if not (ROOT / p).is_file() or (ROOT / p).read_bytes() != b]
        if MANIFEST.read_text() != text:
            stale.append(str(MANIFEST.relative_to(ROOT)))
        if stale:
            print(f"{len(stale)} files differ from a rebuild, for example {stale[:3]}")
            return 1
        print(f"{len(files)} files and the manifest match a rebuild")
        return 0
    previous = json.loads(MANIFEST.read_text())["files"] if MANIFEST.is_file() else {}
    for path in previous:
        if path not in files and (ROOT / path).is_file():
            (ROOT / path).unlink()
    for path, blob in files.items():
        target = ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != blob:
            target.write_bytes(blob)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(text)
    changed = sum(1 for e in manifest["files"].values() if "changes" in e)
    subs = ", ".join(f"{k} {v['occurrences']}" for k, v in manifest["substitutions"].items())
    print(
        f"{len(files)} files placed, {changed} changed ({subs}); {manifest['digest_citations_rewritten']} "
        f"digest citations rewritten; {manifest['databases_checked']} databases checked; "
        f"{len(manifest['not_shipped'])} not shipped, {len(manifest['written_into'])} written into documents"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
