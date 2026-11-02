#!/usr/bin/env python3
"""Reproduce every reported Experiment A number from the committed raw records, and fail on any drift.

Modelled on Khan's soundgate-paper reproduce.sh: each check recomputes a number from committed evidence and
compares it with the value we report. Nothing is fabricated: a missing file fails its check.

  python3 reproduce.py            # all checks (about a minute)
  python3 reproduce.py --quick    # skip the test suite

What it checks:
  1. The round 1 raw files match their committed hashes (results/expA-round1/MANIFEST.sha256).
  2. The fifth-revision scorer (langgraph/archive/analyze_x_rev5.py), rerun on round 1's raw files in a scratch
     folder, gives scored_x.jsonl byte for byte, and summary_x.md except the two lines that print file paths.
  3. naive_checks.py, rerun there, gives naive_checks.md and naive_checks.json as committed.
  4. The numbers quoted in our write-ups (CLAIMS below), recomputed from those outputs and the raw files.
  5. The sixth-revision post hoc rescoring of round 1, if committed, reruns identically.
  6. The regression tests (tests/), unless --quick.
Exit status 0 only if every check passes. Run from anywhere; paths are relative to this file.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
LG = os.path.join(ROOT, "langgraph")
R1 = os.path.join(LG, "results", "expA-round1")
R1_POSTHOC = os.path.join(LG, "results", "expA-round1-rev6-posthoc")
CELLS = os.path.join(ROOT, "predictions", "langgraph-expA-cells.json")
RAW = ["trials.jsonl", "pings.jsonl", "run_meta.json", "mocks/outside.jsonl", "mocks/issuer.jsonl"]
PATH_LINES = ("Results folder: ", "- Predictions: ")
FAIL = []


def ok(label, got, want):
    good = got == want
    print(f"  {'ok  ' if good else 'FAIL'} {label}: {got!r}" + ("" if good else f" (expected {want!r})"))
    if not good:
        FAIL.append(label)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def jl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def strip_paths(text):
    return "\n".join(x for x in text.splitlines() if not x.startswith(PATH_LINES))


def scratch_copy(tmp):
    os.makedirs(os.path.join(tmp, "mocks"))
    for r in RAW:
        shutil.copy2(os.path.join(R1, r), os.path.join(tmp, r))


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stdout[-2000:], p.stderr[-2000:])
    return p.returncode


def check_manifest():
    print("1. round 1 raw files against MANIFEST.sha256")
    man = os.path.join(R1, "MANIFEST.sha256")
    if not os.path.exists(man):
        ok("manifest present", False, True)
        return
    for line in open(man, encoding="utf-8"):
        if line.strip():
            h, name = line.split(None, 1)
            name = name.strip().lstrip("*")
            p = os.path.join(R1, name)
            ok(f"sha256 {name}", sha(p) if os.path.exists(p) else "missing", h)


def check_rev5(tmp):
    print("2. fifth-revision scorer reruns round 1 identically")
    rc = run([sys.executable, "-B", os.path.join(LG, "archive", "analyze_x_rev5.py"), "--res", tmp, "--cells", CELLS])
    ok("scorer exit status", rc, 0)
    ok("scorer is the archived fifth revision (sha256 prefix)", sha(os.path.join(LG, "archive", "analyze_x_rev5.py"))[:12],
       "f28e692ce6a1")
    ok("scored_x.jsonl byte-identical", sha(os.path.join(tmp, "scored_x.jsonl")), sha(os.path.join(R1, "scored_x.jsonl")))
    a = strip_paths(open(os.path.join(tmp, "summary_x.md"), encoding="utf-8").read())
    b = strip_paths(open(os.path.join(R1, "summary_x.md"), encoding="utf-8").read())
    ok("summary_x.md identical except path lines", a == b, True)


def check_naive(tmp):
    print("3. one-day checks (naive_checks.py) rerun identically")
    rc = run([sys.executable, "-B", os.path.join(LG, "naive_checks.py"), "--res", tmp])
    ok("naive_checks exit status", rc, 0)
    for fn in ("naive_checks.md", "naive_checks.json"):
        a = open(os.path.join(tmp, fn), encoding="utf-8").read().replace("\r\n", "\n")
        b = open(os.path.join(R1, fn), encoding="utf-8").read().replace("\r\n", "\n")
        ok(f"{fn} identical", a == b, True)


def check_claims(tmp):
    print("4. numbers quoted in the write-ups")
    rows = jl(os.path.join(tmp, "scored_x.jsonl"))
    trials = jl(os.path.join(R1, "trials.jsonl"))
    pings = jl(os.path.join(R1, "pings.jsonl"))
    outside = jl(os.path.join(R1, "mocks", "outside.jsonl"))
    nc = json.load(open(os.path.join(tmp, "naive_checks.json"), encoding="utf-8"))
    uids = {r["uid"] for r in rows}
    ok("trials scored", len(uids), 33)
    ok("trials valid", len({r["uid"] for r in rows if r["trial_valid"]}), 33)
    ok("rows scored", len(rows), 1671)
    ok("rows whose kind differs between the 5 s and 2 s grace", sum(1 for r in rows if r["kind"] != r.get("kind_grace2")), 0)
    ok("trials with something left behind (one-day check ground truth)", sum(1 for r in nc if r["left_behind"]), 29)
    checks = {"status": lambda r: r["status_says_ended"], "ps+cron": lambda r: not r["ps_cron_sees_something"],
              "no-restart": lambda r: not r["left_end_only"], "short-watch": lambda r: not r["left_short_watch"],
              "tcp-only": lambda r: not r["left_tcp_only"]}
    want = {"status": (18, 16), "ps+cron": (11, 7), "no-restart": (7, 3), "short-watch": (9, 5), "tcp-only": (4, 0)}
    miss_union = set()
    for name, f in checks.items():
        clean = [r for r in nc if f(r)]
        miss = [r for r in clean if r["left_behind"]]
        miss_union |= {r["trial"] for r in miss}
        ok(f"quick check {name}: says clean / missed", (len(clean), len(miss)), want[name])
    ok("distinct trials missed by some quick check", len(miss_union), 21)
    summ = open(os.path.join(tmp, "summary_x.md"), encoding="utf-8").read().splitlines()
    last = [ln.rstrip("|").strip().split("|")[-1].strip() for ln in summ if ln.startswith("|")]
    ok("predicted rows that matched / did not", (sum(1 for m in last if m.startswith("yes (")),
                                                sum(1 for m in last if m.startswith("no ("))), (172, 2))
    T = {t["trial"]: t for t in trials}
    l3 = T["XA-X-L3-t1"]
    a = [p for p in pings if p["trial"] == "XA-X-L3-t1" and p["canary"] == "parent" and p["mono"] > l3["t_stop"]]
    ok("delete thread: agent pings after the stop", len(a), 87)
    ok("delete thread: last agent ping (s after the stop)", round(a[-1]["mono"] - l3["t_stop"], 1), 89.5)
    ok("delete thread: run read at the end of the watch is a 404",
       str((l3["status_end"]["parent_run"] or {}).get("error", "")).startswith("HTTP 404"), True)
    l1 = T["XA-X-L1-t1"]
    ra = l1["status_right_after"]
    ok("cancel: run and thread read right after the stop",
       (ra["parent_run"]["status"], ra["parent_thread"]["status"]), ("interrupted", "interrupted"))
    eff = sorted(round(e["mono"] - l1["t_stop"], 4) for e in outside if e.get("trial") == "XA-X-L1-t1"
                 and e.get("event") == "effect" and e.get("marker") in ("key_use", "tok_use")
                 and e["mono"] > l1["host_right_after"]["t"] and e["mono"] <= l1["t_end_watch"])
    ok("cancel: accepted outside effects after the 'interrupted' read was in hand (s after the stop)",
       eff, [0.218, 0.2315])
    ok("cancel: the read was in hand by (s after the stop)", round(l1["host_right_after"]["t"] - l1["t_stop"], 3), 0.12)


def check_posthoc():
    print("5. sixth-revision post hoc rescoring of round 1")
    if not os.path.isdir(R1_POSTHOC):
        print("  skip: not committed yet")
        return
    with tempfile.TemporaryDirectory() as out:
        rc = run([sys.executable, "-B", os.path.join(LG, "analyze_x.py"), "--res", R1, "--cells", CELLS, "--out", out])
        ok("rev6 scorer exit status", rc, 0)
        ok("post hoc scored_x.jsonl identical", sha(os.path.join(out, "scored_x.jsonl")),
           sha(os.path.join(R1_POSTHOC, "scored_x.jsonl")))
        a = strip_paths(open(os.path.join(out, "summary_x.md"), encoding="utf-8").read())
        b = strip_paths(open(os.path.join(R1_POSTHOC, "summary_x.md"), encoding="utf-8").read())
        ok("post hoc summary_x.md identical except path lines", a == b, True)


def check_tests():
    print("6. regression tests")
    rc = run([sys.executable, "-B", "-m", "unittest", "discover", "-s", os.path.join(ROOT, "tests"), "-t", ROOT])
    ok("unittest exit status", rc, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the test suite")
    a = ap.parse_args()
    check_manifest()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.join(tmp, "r1")
        scratch_copy(tmp)
        check_rev5(tmp)
        check_naive(tmp)
        check_claims(tmp)
    check_posthoc()
    if not a.quick:
        check_tests()
    print(f"\n{'ALL CHECKS PASS' if not FAIL else f'{len(FAIL)} CHECK(S) FAILED: ' + '; '.join(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
