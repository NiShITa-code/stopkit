#!/usr/bin/env python3
"""Rebuild B reports in scratch space and compare with saved reports.

This never modifies raw records or existing reports. A report reproduces only
with the scorer the trial recorded (and, for a screen, the frozen scorer). The
replay is source-matched only when every file of the trial's recorded source
manifest (and, for a screen, the freeze's) matches this checkout, and the
manifest binds every load-bearing scorer source: analyze_b.py and the
clockwall_b.py it imports. Reports from any other scorer, such as legacy
exploratory reports, are post hoc: save them in a separate directory whose name
says so, then check that directory:

    python3 -B n8n/analyze_b.py --out n8n/results/TRIAL-posthoc n8n/results/TRIAL
    python3 -B n8n/reproduce_b.py n8n/results/TRIAL --reports n8n/results/TRIAL-posthoc
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCORER = HERE / "analyze_b.py"
# the scorer's load-bearing sources, relative to ROOT: analyze_b.py imports clockwall_b.py (B.9 recomputation)
SCORER_SOURCES = ("n8n/analyze_b.py", "n8n/clockwall_b.py")
spec = importlib.util.spec_from_file_location("analyze_b_reproduce", SCORER)
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)


def _current_hash(relative):
    """This checkout's sha256 of a recorded path, or None when it is missing or outside the repository."""
    path = (ROOT / relative).resolve()
    if ROOT.resolve() not in path.parents or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_differences(recorded, label):
    """Where a recorded source manifest (path -> sha256, as run_expB.source_hashes records it) does not match this
    checkout: every recorded file is compared, and the manifest must bind each of SCORER_SOURCES."""
    if not isinstance(recorded, dict) or not recorded:
        return [f"{label}: no source manifest"]
    out = [f"{label}: {path} not recorded" for path in SCORER_SOURCES if path not in recorded]
    for path, digest in sorted(recorded.items()):
        now = _current_hash(path)
        if now is None or now != digest:
            out.append(f"{label}: {path} recorded {digest or 'no hash'}, now {now or 'missing'}")
    return out


def scorer_differences(record):
    """Why this replay is not source-matched: the trial's recorded manifest and, for a screen, the freeze's."""
    out = manifest_differences(record.get("source_hashes"), "record source_hashes")
    if record.get("run_kind") == "screen":
        out += manifest_differences((record.get("freeze") or {}).get("source_hashes"), "freeze source_hashes")
    return out


def labelled_post_hoc(reports, folder):
    name = reports.resolve().name.lower().replace("-", "").replace("_", "")
    return reports.resolve() != folder.resolve() and "posthoc" in name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+", type=Path, help="directories with raw record.json/logs")
    parser.add_argument("--reports", type=Path, help="separate saved report directory (one raw trial only); a "
                        "report from a scorer other than the recorded one needs 'posthoc' in this directory's name")
    args = parser.parse_args(argv)
    if args.reports and len(args.results) != 1:
        parser.error("--reports accepts one raw trial")
    failures = []
    for folder in args.results:
        trial = A.Trial(str(folder))
        rows = A.score_trial(trial)
        row_text = "".join(json.dumps(row) + "\n" for row in rows)
        summary = A.summarize(trial, rows)
        reports = args.reports or folder
        differences = scorer_differences(trial.R)
        if differences and not labelled_post_hoc(reports, folder):
            failures.append(f"{folder}: this replay is not source-matched to the trial's recorded sources ("
                            + "; ".join(differences[:8]) + ("; ..." if len(differences) > 8 else "")
                            + "); save a rescoring in a separate post hoc directory")
        elif differences:
            print(f"{trial.trial}: post hoc report in {reports}; the sources differ from the recorded ones")
        with tempfile.TemporaryDirectory(prefix="stopcheck_b_repro_") as scratch:
            for name, text in (("scored_b.jsonl", row_text), ("summary_b.md", summary)):
                generated = Path(scratch) / name
                generated.write_text(text, encoding="utf-8", newline="\n")
                saved = reports / name
                if not saved.is_file() or saved.read_text(encoding="utf-8") != generated.read_text(encoding="utf-8"):
                    failures.append(str(saved))
        print(f"Rebuilt {trial.trial}: {len(rows)} rows; raw files unchanged")
    if failures:
        print("Reports differ, are missing or come from another scorer:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print("All Experiment B reports reproduce")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"Reproduction failed: {error}", file=sys.stderr)
        sys.exit(2)
