#!/usr/bin/env python3
"""Look for a personal name or a personal home-folder path in every tracked file, archive members included.

    NAMES='first|second' python3 .github/scripts/identity_scan.py
    python3 .github/scripts/identity_scan.py --no-names          # the home-folder checks only

NAMES is an extended regular expression, matched ignoring case. CI takes it from the IDENTITY_PATTERN repository
secret, so the names never appear in the repository. Without NAMES the scan refuses to run (exit 2) unless --no-names
is given. The scan also refuses any macOS or Windows home folder and any Linux home folder other than those of the
test accounts and tool folders in ACCOUNTS. It reads every tracked path, text and binary files alike, and gzip and tar
archives through their content, headers and members, prints each file that matches (never the match) and exits 1 if
there is one.
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import re
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The home folders the records hold that belong to no person: the kit's and the OpenClaw runs' test accounts, the
# per-run home folders of the client probe, the build path compiled into the Codex binary, and a frozen test's
# placeholder path.
ACCOUNTS = (
    "stopkit-agent",
    "stopkit-sentinel",
    "stopkit-worker",
    "stopkit-test",
    "ocspike",
    ".claude",
    ".codex",
    "runner",
    "u",
)
HOME = re.compile(rb"/hom[e]/([A-Za-z0-9_.-]+)/")
# A home folder on macOS or Windows: the Users folder, a slash or one or more backslashes, and a name.
OTHER_HOME = re.compile(rb"User[s](?:/|\\+)[A-Za-z0-9_.-]+")
# The test of the Windows home-folder check holds the folder it plants in a scratch repository; that file may hold
# that one folder, which names no person.
PLANTED = ("experiments/tests/test_records.py", b"zzplanted")


def gzip_header(blob: bytes) -> bytes:
    """The readable fields of a gzip header: the original file name and the comment, when present."""
    flags, pos, out = blob[3], 10, b""
    if flags & 4:
        pos += 2 + int.from_bytes(blob[pos : pos + 2], "little")
    for flag in (8, 16):
        if flags & flag:
            end = blob.index(b"\0", pos)
            out += blob[pos:end] + b"\n"
            pos = end + 1
    return out


def members(name: str, blob: bytes):
    """Yield (label, bytes) for a file and, recursively, for every member of the archives inside it.

    A compressed archive is read through its header fields and its content, never its compressed bytes, where a short
    name can occur by chance. A tar archive is read whole, so its headers (member names, owners, link targets) are
    read as well as each member.
    """
    try:
        if name.endswith((".tgz", ".tar.gz", ".tar")):
            data = blob if name.endswith(".tar") else gzip.decompress(blob)
            yield name, data if name.endswith(".tar") else gzip_header(blob) + data
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
                for m in t.getmembers():
                    if m.isfile():
                        yield from members(f"{name}::{m.name}", t.extractfile(m).read())
        elif name.endswith(".gz"):
            yield name, gzip_header(blob)
            yield from members(f"{name}::gunzip", gzip.decompress(blob))
        else:
            yield name, blob
    except (OSError, EOFError, IndexError, ValueError, tarfile.TarError) as e:
        raise SystemExit(f"{name}: cannot read the archive: {e}") from e


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-names", action="store_true", help="run without NAMES: the home-folder checks only")
    parser.add_argument("--root", default=ROOT, help="the repository to scan (default: this one)")
    args = parser.parse_args(argv)
    names = os.environ.get("NAMES", "")
    if not names and not args.no_names:
        print("NAMES is not set; give the names to refuse, or --no-names for the home-folder checks only")
        return 2
    rx = re.compile(names.encode(), re.I) if names else None
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=args.root, capture_output=True, check=True).stdout
    found, scanned = [], 0
    for rel in filter(None, tracked.decode().split("\0")):
        if rx and rx.search(rel.encode()):
            found.append(f"{rel} (its path)")
        if rel == "LICENSE":
            continue
        with open(os.path.join(args.root, rel), "rb") as f:
            blob = f.read()
        for label, data in members(rel, blob):
            scanned += 1
            homes = {h.decode() for h in HOME.findall(data)} - set(ACCOUNTS)
            other = [m.group(0) for m in OTHER_HOME.finditer(data)]
            if label == PLANTED[0]:
                other = [m for m in other if not m.endswith(PLANTED[1])]
            if (rx and rx.search(data)) or other or homes:
                found.append(label)
    for label in found:
        print(f"identity: {label}")
    print(f"{scanned} files and archive members scanned, {len(found)} match" + ("" if rx else " (no NAMES given)"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
