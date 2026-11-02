#!/usr/bin/env python3
"""Rerun every check on the recorded experiments and write experiments/results/results.json.

    python3 -B experiments/scripts/build_results.py  # every check; writes experiments/results/results.json
    python3 -B experiments/scripts/build_results.py --only NAME ...  # the named checks only; writes nothing

Each check recomputes something from the records with the code that produced them and compares it with
what the records and this repository's documents state. The script prints one line per check and writes
experiments/results/results.json only when every check passes; otherwise it writes nothing and exits 1. Reports that
the scorers rebuild go to a temporary folder that is deleted at the end. experiments/RUNBOOK.md lists what makes each
check fail. It needs only Python 3.10 or newer.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "experiments", "results", "results.json")

CHECKS = {
    "records": "records: every file placed from the source archive has the SHA-256 and size that "
    "experiments/data/MANIFEST.json "
    "records for it",
    "n8n_checksums": "n8n queue mode: every file of the six collections copied off the worker machines, apart from "
    "the folder manifests, is listed in its collection's SHA256SUMS with its SHA-256, which for a file changed by a "
    "substitution is the digest after the change",
    "n8n_freeze": "n8n queue mode: the files the freeze binds are the code in "
    "experiments/harness/n8n-langgraph-7a3fcec, and every "
    "trial record names the same SHA-256 for each of them",
    "n8n_rescore": "n8n queue mode: the frozen scorer, rerun on each trial's raw files, gives the verdicts saved "
    "for every trial",
    "temporal_freeze": "Temporal: the files the freeze binds and the predictions are the code in "
    "experiments/harness/temporal-627df0b",
    "temporal_rescore": "Temporal: the frozen scorer, rerun on each trial's raw files, gives the saved trial reports",
    "langgraph_reproduce": "LangGraph: reproduce.py --quick checks the raw files against their manifest, reruns "
    "the scorer and recomputes every number it quotes",
    "langgraph_round1_code": "LangGraph: experiments/harness/langgraph-cd26851 holds every file round 1's freeze "
    "binds, each with the SHA-256 the freeze records, apart from the two that the commit changed after the round",
    "n8n_restart_verdicts": "n8n stop after a restart: every verdict in the saved transcripts is a reproduction",
    "n8n_restart_updates": "n8n stop after a restart: the receiver file of the run of 29 October on a fresh virtual "
    "machine holds the recorded number of updates from each execution after its stop reply, and in the archive of "
    "every run the control sent none after its reply",
    "client_probe_recheck": "Claude Code and Codex: the probe's own re-check, rerun on each saved batch, gives the "
    "batch's saved check.json",
    "loop_evidence": "n8n loop evidence: experiments/docs/n8n-loop-evidence holds what "
    "experiments/scripts/loop_evidence.py writes from "
    "the raw records of the n8n trials",
    "walkthroughs": "walkthroughs: each page in experiments/docs/walkthroughs is what its script prints from the "
    "records, and "
    "every line it quotes is in the file it names",
    "selftest_recheck": "stopkit: recheck, run on each saved self-test run, gives the saved verdicts from the "
    "stored evidence",
}

RESULTS: list[tuple[str, bool]] = []
COUNTS: dict[str, dict] = {}


def configure(root: str) -> None:
    """Point every check at the repository under `root`."""
    global ROOT, N8N_LANGGRAPH_CODE, TEMPORAL_CODE, N8N_RECORDS, TEMPORAL_RECORDS, RESTART, CLIENT_EXPERIMENT
    global PAGES, LOOP
    ROOT = root
    N8N_LANGGRAPH_CODE = os.path.join(root, "experiments", "harness", "n8n-langgraph-7a3fcec")
    TEMPORAL_CODE = os.path.join(root, "experiments", "harness", "temporal-627df0b")
    N8N_RECORDS = os.path.join(root, "experiments", "n8n-queue-mode", "records", "cb1013")
    TEMPORAL_RECORDS = os.path.join(root, "experiments", "temporal-activities", "records", "cs1014")
    RESTART = os.path.join(root, "experiments", "n8n-restart", "results")
    CLIENT_EXPERIMENT = os.path.join(root, "experiments", "claude-code-codex")
    PAGES = os.path.join(root, "experiments", "docs", "walkthroughs")
    LOOP = os.path.join(root, "experiments", "docs", "n8n-loop-evidence")


configure(ROOT)


def report(name: str, passed: bool, detail: str) -> None:
    RESULTS.append((name, passed))
    print(f"{'PASS' if passed else 'FAIL'}  {CHECKS[name]}: {detail}", flush=True)


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def same_bytes(a: str, b: str) -> bool:
    if not (os.path.isfile(a) and os.path.isfile(b)):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def jsonl(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def run(args: list[str], cwd: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)


# ------------------------------------------------------------------------------ the shipped records


def records() -> None:
    """Every placed file has the digest and size the manifest records."""
    with open(os.path.join(ROOT, "experiments", "data", "MANIFEST.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    bad = []
    for path, entry in manifest["files"].items():
        full = os.path.join(ROOT, path)
        if not os.path.isfile(full) or os.path.getsize(full) != entry["bytes"] or sha256(full) != entry["sha256"]:
            bad.append(path)
    changed = sum(1 for e in manifest["files"].values() if "changes" in e)
    COUNTS["records"] = {"files": len(manifest["files"]), "changed_by_substitution_or_edit": changed}
    report(
        "records",
        bool(manifest["files"]) and not bad,
        f"{len(manifest['files'])} files checked, {len(bad)} differ" + (f": {bad[:3]}" if bad else ""),
    )


# ------------------------------------------------------------------------------ n8n, 13 October 2026


def n8n_checksums() -> None:
    """Every file of the six collections is listed in its SHA256SUMS and has the digest listed there.

    Where a substitution changed a copied file, the build rewrote its entry, so for that file the check compares it
    with the digest after the substitution; the detail counts those entries apart. The files in each collection's
    manifests/ folder are the per-folder lists SHA256SUMS was made from, so they are not in it.
    """
    with open(os.path.join(ROOT, "experiments", "data", "MANIFEST.json"), encoding="utf-8") as f:
        changed = {p for p, e in json.load(f)["files"].items() if "changes" in e}
    collections = sorted(glob.glob(os.path.join(N8N_RECORDS, "w*", "collect-*", "SHA256SUMS")))
    total, rewritten, bad, unlisted = 0, 0, [], []
    for sums in collections:
        base = os.path.dirname(sums)
        listed = set()
        with open(sums, encoding="utf-8") as f:
            for line in f:
                digest, name = line.rstrip("\n").split("  ", 1)
                total += 1
                path = os.path.join(base, name)
                listed.add(os.path.normpath(path))
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                rewritten += rel in changed
                if not os.path.isfile(path) or sha256(path) != digest:
                    bad.append(rel)
        for folder, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if not (folder == base and d == "manifests")]
            for n in names:
                path = os.path.normpath(os.path.join(folder, n))
                if path != os.path.normpath(sums) and path not in listed:
                    unlisted.append(os.path.relpath(path, ROOT))
    COUNTS["n8n_checksums"] = {
        "collections": len(collections),
        "copied_files": total,
        "checked_against_digests_after_substitution": rewritten,
    }
    report(
        "n8n_checksums",
        len(collections) == 6 and total > 0 and not bad and not unlisted,
        f"{len(collections)} collections, {total} files checked ({rewritten} against digests after a substitution), "
        f"{len(bad)} differ, {len(unlisted)} not listed"
        + (f": {bad[:3]}" if bad else "")
        + (f"; not listed: {unlisted[:3]}" if unlisted else ""),
    )


def n8n_freeze() -> None:
    """The frozen code is the code that ran: the files FREEZE-expB.json binds, and the hashes in every record."""
    with open(os.path.join(N8N_RECORDS, "freeze", "FREEZE-expB.json"), encoding="utf-8") as f:
        src = json.load(f)["source_hashes"]
    bad = [
        p
        for p, h in src.items()
        if not os.path.isfile(os.path.join(N8N_LANGGRAPH_CODE, p)) or sha256(os.path.join(N8N_LANGGRAPH_CODE, p)) != h
    ]
    trials = glob.glob(
        os.path.join(N8N_RECORDS, "w*", "collect-*", "results", "XB-screen-cb1013-w*-*-2026*", "record.json")
    )
    mismatch = 0
    for r in trials:
        with open(r, encoding="utf-8") as f:
            rec = json.load(f).get("source_hashes") or {}
        # a missing or an extra entry counts as a difference, as does a different digest
        mismatch += len(set(src) ^ set(rec)) + sum(1 for p in src.keys() & rec.keys() if src[p] != rec[p])
    COUNTS["n8n_freeze"] = {"frozen_files": len(src), "trial_records": len(trials)}
    report(
        "n8n_freeze",
        not bad and len(trials) == 19 and mismatch == 0,
        f"{len(src)} frozen files, {len(bad)} differ; {len(trials)} trial records, {mismatch} recorded hashes differ",
    )


def n8n_rescore(tmp: str) -> None:
    """The frozen scorer, rerun on each trial's raw files, gives the saved verdicts.

    In its 'events' row the scorer lists at most 20 state changes, taken from a Python set whose order varies
    from run to run, so that one display-only list is compared apart. No verdict depends on it.
    """
    raws = sorted(glob.glob(os.path.join(N8N_RECORDS, "w*", "collect-*", "results", "XB-screen-cb1013-w*-*-2026*")))
    agree, bad = 0, []
    for raw in raws:
        m = re.match(r"XB-screen-cb1013-(w\d)-(\w+)-2026", os.path.basename(raw))
        worker, cond = m.group(1), m.group(2)
        saved = glob.glob(
            os.path.join(
                N8N_RECORDS,
                worker,
                "collect-*",
                "results",
                f"XB-screen-cb1013-{worker}",
                "derived",
                f"XB-screen-cb1013-{worker}-{cond}",
                "scored_b.jsonl",
            )
        )[0]
        out = os.path.join(tmp, "n8n-" + cond)
        p = run([sys.executable, "-B", os.path.join("n8n", "analyze_b.py"), "--out", out, raw], cwd=N8N_LANGGRAPH_CODE)
        if p.returncode != 0:
            bad.append(f"{cond}: scorer exit {p.returncode}")
            continue
        rebuilt, kept = jsonl(os.path.join(out, "scored_b.jsonl")), jsonl(saved)
        if len(rebuilt) != len(kept):
            bad.append(f"{cond}: {len(rebuilt)} rows, saved {len(kept)}")
            continue

        def blank(row: dict) -> dict:
            row = json.loads(json.dumps(row))
            if row.get("measure") == "events" and isinstance(row.get("evidence"), dict):
                row["evidence"].pop("state_changes", None)
            return row

        # How many trials match without the exception changes from run to run with the set's order, so only the
        # comparison without the display-only list is counted.
        if any(blank(a) != blank(b) for a, b in zip(rebuilt, kept, strict=True)):
            bad.append(cond)
        else:
            agree += 1
    COUNTS["n8n_rescore"] = {"trials": len(raws), "verdicts_as_saved": agree}
    report(
        "n8n_rescore",
        len(raws) == 19 and not bad,
        f"{len(raws)} trials, {agree} with every verdict as saved" + (f"; differ: {bad}" if bad else ""),
    )


# ------------------------------------------------------------------------------ Temporal, 14 October 2026


def temporal_freeze() -> None:
    with open(os.path.join(TEMPORAL_CODE, "temporal", "FREEZE-expC.json"), encoding="utf-8") as f:
        freeze = json.load(f)
    bad = [p for p, h in freeze["source_hashes"].items() if sha256(os.path.join(TEMPORAL_CODE, p)) != h]
    pred = sha256(os.path.join(TEMPORAL_CODE, "predictions", "temporal-expC-cells.json")) == freeze.get(
        "prediction_sha256"
    )
    COUNTS["temporal_freeze"] = {"frozen_files": len(freeze["source_hashes"]), "predictions_match": pred}
    report(
        "temporal_freeze",
        not bad and pred,
        f"{len(freeze['source_hashes'])} frozen files, {len(bad)} differ; predictions match: {pred}",
    )


def temporal_rescore(tmp: str) -> None:
    raws = sorted(
        d
        for d in glob.glob(os.path.join(TEMPORAL_RECORDS, "XC-screen-c-screen-1014-*"))
        if os.path.isdir(d) and re.search(r"-\d{8}-\d{6}$", d)
    )
    same, bad = 0, []
    for raw in raws:
        name = re.sub(r"-\d{8}-\d{6}$", "", os.path.basename(raw))
        saved = os.path.join(TEMPORAL_RECORDS, "XC-screen-c-screen-1014", "derived", name, "scored_c.jsonl")
        out = os.path.join(tmp, "temporal-" + name)
        p = run([sys.executable, "-B", os.path.join("temporal", "analyze_c.py"), "--out", out, raw], cwd=TEMPORAL_CODE)
        if p.returncode != 0 or not os.path.exists(saved):
            bad.append(f"{name}: scorer exit {p.returncode}")
            continue
        if jsonl(os.path.join(out, "scored_c.jsonl")) == jsonl(saved):
            same += 1
        else:
            bad.append(name)
    COUNTS["temporal_rescore"] = {"trial_reports": len(raws), "reports_as_saved": same}
    report(
        "temporal_rescore",
        len(raws) == 17 and not bad,
        f"{len(raws)} trial reports, {same} identical" + (f"; differ: {bad}" if bad else ""),
    )


# ------------------------------------------------------------------------------ LangGraph, 9 October 2026


# The checks reproduce.py --quick makes on the committed records; a missing folder makes it skip some.
LANGGRAPH_CHECKS = 36


# The two files of commit cd26851 that differ from round 1's freeze, because the commit was made after the round
# (experiments/langgraph-controls/code/CHANGES.md).
ROUND1_LATER = ("langgraph/RULES-expA.md", "langgraph/analyze_x.py")


def langgraph_round1_code() -> None:
    with open(
        os.path.join(N8N_LANGGRAPH_CODE, "langgraph", "results", "expA-round1", "FREEZE.json"), encoding="utf-8"
    ) as f:
        bound = json.load(f)["files"]
    code = os.path.join(ROOT, "experiments", "harness", "langgraph-cd26851")
    differ = sorted(p for p, h in bound.items() if sha256(os.path.join(code, p)) != h)
    COUNTS["langgraph_round1_code"] = {"bound_files": len(bound), "differ": differ}
    report(
        "langgraph_round1_code",
        len(bound) == 13 and differ == sorted(ROUND1_LATER),
        f"{len(bound)} bound files, {len(bound) - len(differ)} with the recorded SHA-256; differ: {', '.join(differ)}",
    )


def langgraph_reproduce() -> None:
    """reproduce.py checks the raw files against MANIFEST.sha256, reruns the scorer and recomputes the numbers.

    MANIFEST.sha256 was written on a Windows copy, where two derived summaries (naive_checks.md and .json) had
    Windows line endings; the files here have Unix line endings. They are accepted only if their content, given
    Windows line endings, has exactly the manifest's digest, and only when reproduce.py completed its run.
    """
    p = run([sys.executable, "-B", "reproduce.py", "--quick"], cwd=N8N_LANGGRAPH_CODE)
    out = p.stdout + p.stderr
    failed = re.findall(r"^\s*FAIL\s+(.+?):", out, re.M)
    known = {"sha256 naive_checks.md", "sha256 naive_checks.json"}
    crlf_ok = True
    if set(failed) & known:
        man = {}
        with open(
            os.path.join(N8N_LANGGRAPH_CODE, "langgraph", "results", "expA-round1", "MANIFEST.sha256"), encoding="utf-8"
        ) as f:
            for line in f:
                if line.strip():
                    h, n = line.split()
                    man[n] = h
        for n in ("naive_checks.md", "naive_checks.json"):
            with open(os.path.join(N8N_LANGGRAPH_CODE, "langgraph", "results", "expA-round1", n), "rb") as f:
                data = f.read()
            crlf_ok &= hashlib.sha256(data.replace(b"\n", b"\r\n")).hexdigest() == man.get(n)
    oks = len(re.findall(r"^\s*ok\s", out, re.M))
    # A crashed subprocess may print no FAIL lines, or stop after a known failure. Neither is a completed check,
    # so the script's final summary must match its return code before the line-ending exception is accepted.
    lines = p.stdout.strip().splitlines()
    final = lines[-1] if lines else ""
    summary = re.fullmatch(r"(\d+) CHECK\(S\) FAILED: (.+)", final)
    completed_failures = bool(
        summary and int(summary.group(1)) == len(failed) and summary.group(2).split("; ") == failed
    )
    skipped = re.findall(r"^\s*skip:.*$", out, re.M)
    passed = (
        oks == LANGGRAPH_CHECKS
        and not skipped
        and (
            (p.returncode == 0 and not failed and final == "ALL CHECKS PASS")
            or (p.returncode == 1 and bool(failed) and completed_failures and set(failed) <= known and crlf_ok)
        )
    )
    detail = f"{oks} checks ok"
    if failed:
        detail += (
            f"; {len(failed)} manifest entries differ only in Windows line endings" if passed else f"; failed: {failed}"
        )
    if not passed:
        detail += f" of {LANGGRAPH_CHECKS}; {len(skipped)} skipped"
        detail += f"; subprocess exit {p.returncode}; final output: {final or '<none>'}"
    COUNTS["langgraph_reproduce"] = {
        "checks_ok": oks,
        "entries_with_windows_line_endings": len(failed) if passed else None,
    }
    report("langgraph_reproduce", passed, detail)


# ------------------------------------------------------------------------------ n8n, stop after a restart


def n8n_restart_verdicts() -> None:
    files = (
        glob.glob(os.path.join(RESTART, "2026-10-14-vm", "out", "p1-*.txt"))
        + glob.glob(os.path.join(RESTART, "2026-10-15-vm-recording", "run", "out", "p1-*.txt"))
        + glob.glob(os.path.join(RESTART, "2026-10-21-fresh-vm", "e1", "e1-*.log"))
        + glob.glob(os.path.join(RESTART, "2026-10-29-local", "console.log"))
        + glob.glob(os.path.join(RESTART, "2026-10-29-local-from-repo", "console.log"))
        + glob.glob(os.path.join(RESTART, "2026-10-29-fresh-vm", "out", "p1-*.txt"))
    )
    yes = no = 0
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        no += text.count("P1 NOT REPRODUCED")
        yes += text.count("P1 REPRODUCED")
    COUNTS["n8n_restart_verdicts"] = {"reproduced": yes, "not_reproduced": no}
    report("n8n_restart_verdicts", yes == 11 and no == 0, f"{yes} runs reproduced, {no} did not")


def updates_after_replies(name: str, transcript: str, rows: list) -> dict:
    """The updates each execution sent after its printed stop reply, from a transcript and its receiver file.

    The transcript prints each reply's UTC time of day; its date is the one in the transcript's name, and a time
    earlier than the transcript's start falls on the next day.
    """
    start = datetime.strptime(re.match(r"p1-(\d{8}T\d{6})Z", name)[1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    replies = {}
    for line in transcript.splitlines():
        m = re.match(r"(\d{2}:\d{2}:\d{2}\.\d+) stop ([AB]) reply:", line)
        if m:
            t = datetime.fromisoformat(start.date().isoformat() + "T" + m[1]).replace(tzinfo=UTC)
            if t < start:
                t += timedelta(days=1)
            replies[m[2]] = t.timestamp()
    return {
        execution: sum(1 for r in rows if str(r["body"].get("exec")) == execution and r["t"] > replies[phase])
        for phase, execution in (("A", "1"), ("B", "2"))
        if phase in replies
    }


def n8n_restart_updates() -> None:
    """Recount, from the receiver files alone, the updates each execution sent after its printed stop reply.

    The run of 29 October on a fresh virtual machine is recounted from its unpacked files; every run, that one
    included, is recounted from the archive teardown.sh wrote (n8n-repro-<time>.tgz).
    """
    base = os.path.join(RESTART, "2026-10-29-fresh-vm")
    name = "p1-20261029T182735Z.txt"
    with open(os.path.join(base, "out", name), encoding="utf-8") as f:
        ticks = updates_after_replies(name, f.read(), jsonl(os.path.join(base, "logs", "sink.jsonl")))
    runs = {}
    for archive in sorted(glob.glob(os.path.join(RESTART, "**", "n8n-repro-*.tgz"), recursive=True)):
        with tarfile.open(archive) as t:
            members = t.getnames()
            out = [m for m in members if re.search(r"(^|/)out/p1-\d{8}T\d{6}Z\.txt$", m)]
            sink = [m for m in members if re.search(r"(^|/)logs/sink\.jsonl$", m)]
            if len(out) != 1 or len(sink) != 1:
                runs[os.path.relpath(archive, RESTART)] = None
                continue
            transcript = t.extractfile(out[0]).read().decode("utf-8")
            rows = [json.loads(ln) for ln in t.extractfile(sink[0]).read().decode("utf-8").splitlines() if ln.strip()]
        runs[os.path.relpath(archive, RESTART)] = updates_after_replies(out[0].rsplit("/", 1)[1], transcript, rows)
    controls_quiet = all(r is not None and r.get("1") == 0 and r.get("2", 0) > 0 for r in runs.values())
    COUNTS["n8n_restart_updates"] = {
        "updates_after_stop_reply": ticks,
        "archived_runs": {k: (v or {}).get("2") for k, v in runs.items()},
    }
    report(
        "n8n_restart_updates",
        ticks == {"1": 0, "2": 237} and len(runs) == 11 and controls_quiet,
        f"execution 1: {ticks.get('1')} updates after its stop reply; execution 2: {ticks.get('2')}; "
        f"{len(runs)} archived runs recounted, "
        f"{sum(1 for r in runs.values() if r and r.get('1') == 0)} with no update from the control after its reply",
    )


# ------------------------------------------------------------------------------ Claude Code and Codex


def client_probe_recheck(tmp: str) -> None:
    """The probe's own re-check (check_probe.py), rerun on a copy of each saved batch, gives its check.json.

    The saved copies of the 25 October batch were written on Windows, so line endings are not compared.
    """
    crlf, lf = bytes([13, 10]), bytes([10])
    batches = [os.path.join(CLIENT_EXPERIMENT, "records", "20261025T142258Z")] + sorted(
        glob.glob(os.path.join(CLIENT_EXPERIMENT, "run-2026-10-29", "2026*"))
    )
    same, bad = 0, []
    for b in batches:
        work = os.path.join(tmp, "probe-" + os.path.basename(b), os.path.basename(b))
        shutil.copytree(b, work)
        # the saved output is removed from the copy, so only what check_probe.py writes is compared
        for n in ("check.json", "check.md"):
            if os.path.exists(os.path.join(work, n)):
                os.remove(os.path.join(work, n))
        p = run([sys.executable, "-B", os.path.join(CLIENT_EXPERIMENT, "check_probe.py"), work], cwd=CLIENT_EXPERIMENT)
        written = os.path.join(work, "check.json")
        rebuilt = b""
        if os.path.exists(written):
            with open(written, "rb") as f:
                rebuilt = f.read().replace(crlf, lf)
        with open(os.path.join(b, "check.json"), "rb") as f:
            saved = f.read().replace(crlf, lf)
        if p.returncode == 0 and rebuilt == saved:
            same += 1
        else:
            bad.append(f"{os.path.basename(b)}: exit {p.returncode}, check.json same: {rebuilt == saved}")
    COUNTS["client_probe_recheck"] = {"batches": len(batches), "batches_as_saved": same}
    report(
        "client_probe_recheck",
        len(batches) == 2 and not bad,
        f"{len(batches)} batches, {same} identical" + (f"; differ: {bad}" if bad else ""),
    )


# ------------------------------------------------------------------------------ pages written from the records


def loop_evidence(tmp: str) -> None:
    """experiments/scripts/loop_evidence.py, rerun on the raw records, writes the committed rows and page."""
    out = os.path.join(tmp, "n8n-loop-evidence")
    p = run(
        [sys.executable, "-B", os.path.join(ROOT, "experiments", "scripts", "loop_evidence.py"), "--out", out], cwd=ROOT
    )
    same = p.returncode == 0 and all(
        same_bytes(os.path.join(out, n), os.path.join(LOOP, n)) for n in ("rows.jsonl", "REPORT.md")
    )
    if same:
        rows = jsonl(os.path.join(LOOP, "rows.jsonl"))
        support = [r["termination_support"] for r in rows]
        quiet = sum(1 for r in rows if r["termination_support"] == "unconfirmed" and not r["recent_activity_observed"])
        COUNTS["loop_evidence"] = {
            "trials": len({r["trial"] for r in rows}),
            "loop_readings": len(rows),
            "computation_end_evidence": support.count("supported"),
            "unconfirmed": support.count("unconfirmed"),
            "unconfirmed_without_recent_activity": quiet,
            "unknown": support.count("unknown"),
        }
        detail = f"{len(rows)} loop readings in {COUNTS['loop_evidence']['trials']} trials, written again identically"
    else:
        detail = (
            f"the rows or the page differ from a fresh reading (exit {p.returncode}): {(p.stderr or p.stdout)[-300:]}"
        )
    report("loop_evidence", same, detail)


WALKTHROUGHS = [
    (
        "LANGGRAPH.md",
        "walkthrough_langgraph.py",
        ("experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1",),
        ("XA-X-L1-t1", "XA-X-L3-t1"),
    ),
    ("N8N.md", "walkthrough_n8n.py", ("experiments/n8n-queue-mode/records/cb1013",), ("B1", "B1r", "B4b")),
    ("TEMPORAL.md", "walkthrough_temporal.py", ("experiments/temporal-activities/records/cs1014",), ("C3-a2", "C2")),
    (
        "OPENCLAW.md",
        "walkthrough_openclaw.py",
        ("experiments/openclaw-commands/records/runs/spike-20261014-071212",),
        (),
    ),
]


def walkthroughs() -> None:
    """Each page is its script's output on the records, and every line it quotes is in the file it names."""
    items, bad = {}, []
    for page, script, (records_dir,), trials in WALKTHROUGHS:
        p = run(
            [sys.executable, "-B", os.path.join(ROOT, "experiments", "scripts", script), records_dir, *trials], cwd=ROOT
        )
        with open(os.path.join(PAGES, page), encoding="utf-8") as f:
            committed = f.read()
        if p.returncode != 0 or p.stdout != committed:
            bad.append(f"{page}: differs from its script's output (exit {p.returncode})")
            continue
        c = run(
            [
                sys.executable,
                "-B",
                os.path.join(ROOT, "experiments", "scripts", "check_pages.py"),
                os.path.join(PAGES, page),
                records_dir,
            ],
            cwd=ROOT,
        )
        last = (c.stdout.strip().splitlines() or ["no output"])[-1]
        m = re.search(r"(\d+) items checked, (\d+) failed", last)
        if c.returncode != 0 or not m or m.group(2) != "0":
            bad.append(f"{page}: {last}")
            continue
        items[page] = int(m.group(1))
    COUNTS["walkthroughs"] = {"quoted_items_checked": items}
    report(
        "walkthroughs",
        not bad and len(items) == len(WALKTHROUGHS),
        ", ".join(f"{p} {n} items" for p, n in items.items()) + (f"; {bad}" if bad else ""),
    )


def selftest_recheck() -> None:
    """The kit's recheck, run on each saved self-test run, scores it again from its stored evidence."""
    runs = sorted(glob.glob(os.path.join(ROOT, "validation", "selftest-*", "run-*")))
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    bad = []
    for r in runs:
        p = run([sys.executable, "-B", "-m", "stopkit", "recheck", r], cwd=ROOT, env=env)
        if p.returncode != 0 or "same verdicts from the evidence" not in p.stdout:
            problems = [ln.strip() for ln in p.stdout.splitlines() if ln.startswith("  ")]
            bad.append(
                f"{os.path.basename(r)}: exit {p.returncode}" + (f" ({'; '.join(problems)})" if problems else "")
            )
    COUNTS["selftest_recheck"] = {"selftest_runs": len(runs), "runs_as_saved": len(runs) - len(bad)}
    report(
        "selftest_recheck",
        len(runs) == 6 and not bad,
        f"{len(runs)} self-test runs, {len(runs) - len(bad)} with the saved verdicts" + (f"; {bad}" if bad else ""),
    )


ORDER = [
    ("records", records, False),
    ("n8n_checksums", n8n_checksums, False),
    ("n8n_freeze", n8n_freeze, False),
    ("n8n_rescore", n8n_rescore, True),
    ("temporal_freeze", temporal_freeze, False),
    ("temporal_rescore", temporal_rescore, True),
    ("langgraph_reproduce", langgraph_reproduce, False),
    ("langgraph_round1_code", langgraph_round1_code, False),
    ("n8n_restart_verdicts", n8n_restart_verdicts, False),
    ("n8n_restart_updates", n8n_restart_updates, False),
    ("client_probe_recheck", client_probe_recheck, True),
    ("loop_evidence", loop_evidence, True),
    ("walkthroughs", walkthroughs, False),
    ("selftest_recheck", selftest_recheck, False),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", choices=list(CHECKS), help="run only these checks; write nothing")
    parser.add_argument("--root", default=ROOT, help="the repository to check (default: this one)")
    parser.add_argument(
        "--out", default=None, help="where to write the results (default: experiments/results/results.json)"
    )
    args = parser.parse_args(argv)
    configure(os.path.abspath(args.root))
    out = args.out or os.path.join(ROOT, "experiments", "results", "results.json")
    tmp = tempfile.mkdtemp(prefix="shutdown-checks-")
    try:
        for name, check, needs_tmp in ORDER:
            if args.only and name not in args.only:
                continue
            try:
                check(tmp) if needs_tmp else check()
            except (OSError, ValueError, KeyError, IndexError, AttributeError) as e:
                report(name, False, f"could not run: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed.")
    if failed or args.only:
        return 1 if failed else 0
    results = {
        "checks": {"passed": len(RESULTS), "checks": [CHECKS[n] for n, _, _ in ORDER]},
        "counts": {name: COUNTS[name] for name, _, _ in ORDER},
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps(results, indent=1) + "\n")
    if args.out is None:
        print("wrote experiments/results/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
