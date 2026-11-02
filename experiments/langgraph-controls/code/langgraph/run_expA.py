#!/usr/bin/env python3
"""Experiment A batch runner (RULES-expA.md sections 2, 8 and 10). Run in WSL as root, inside one
long wsl.exe call so WSL does not go idle:
    python3 run_expA.py --rounds 1        # first round: every (agent, stop) once
    python3 run_expA.py --rounds 2-10     # the remaining rounds

Design:
- Cells: parent_x under all 12 stops; parent_c1, parent_c2s, parent_c2a under the 7 stops that apply to
  them (section 10). Trial counts: 3 for class D, 10 for class T (L2, S1, S2); KEYC has no class in the
  predictions and is run like KEY (3), as noted in the batch log.
- Rounds: round r runs every cell that needs an r-th trial once, in an order shuffled with a fixed seed
  per round, so drift on the machine cannot line up with one stop.
- Trial ids: XA-X-<stop>-t<r> (parent_x), XA-C1-, XA-C2S-, XA-C2A- (the other graphs).
- Each trial is one harness run through run_trial_ns.sh (fresh network namespace). S2 uses --pre 245.
- Before the first trial a freeze record is written: sha256 of the rules, the predictions and every code
  file, with the time, plus copies of the prediction files. Nothing in them may change after it.
- Safety: stops before a trial if C: has under 10 GB free, or if /root/expA.STOP exists.
  On a machine without /mnt/c (a Linux VM) the free space is that of the disk holding this folder;
  STOPCHECK_DISK and EXPA_STOPFILE set another folder to check and another stop file.
"""
import argparse
import datetime
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "results", "expA")
STOPFILE = os.environ.get("EXPA_STOPFILE", "/root/expA.STOP")
DISK = os.environ.get("STOPCHECK_DISK", "/mnt/c" if os.path.isdir("/mnt/c") else HERE)  # WSL: Windows C:
GRAPHS = {"parent_x": "XA-X", "parent_c1": "XA-C1", "parent_c2s": "XA-C2S", "parent_c2a": "XA-C2A"}
STOPS_X = ["L1", "L2", "L3", "DA", "S1", "S2", "L4", "PG", "CC", "KEY", "KEYC", "NET"]
STOPS_OTHER = ["L1", "L2", "S1", "S2", "L4", "PG", "CC"]
T_CLASS = {"L2", "S1", "S2"}
FROZEN = ["langgraph/RULES-expA.md", "predictions/langgraph-expA-DRAFT.md", "predictions/langgraph-expA-cells.json",
          "langgraph/harness.py", "langgraph/analyze_x.py", "langgraph/app/agent_graphs.py",
          "langgraph/app/langgraph.json", "langgraph/canary.py", "langgraph/recorder.py",
          "langgraph/run_trial_ns.sh", "common/mocks.py", "common/guards.py", "langgraph/run_expA.py"]


def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(os.path.join(OUT, "batch.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def freeze():
    path = os.path.join(OUT, "FREEZE.json")
    rec = {"written": datetime.datetime.now().isoformat(timespec="seconds"),
           "files": {p: sha(os.path.join(ROOT, p)) for p in FROZEN}}
    if os.path.exists(path):
        old = json.load(open(path, encoding="utf-8"))
        changed = [p for p in FROZEN if old["files"].get(p) != rec["files"][p]]
        # the scorer does not change what a trial does; a scorer fix after the freeze is allowed only
        # with a dated addendum in RULES-expA.md, so it warns instead of blocking
        blocking = [p for p in changed if p != "langgraph/analyze_x.py"]
        if "langgraph/analyze_x.py" in changed:
            log("note: analyze_x.py changed since the freeze; record the fix in a dated RULES addendum")
        if blocking:
            log(f"FREEZE MISMATCH: these files changed since the freeze: {blocking}; refusing to run")
            sys.exit(5)
        log(f"freeze record matches ({old['written']})")
        return
    os.makedirs(os.path.join(OUT, "frozen"), exist_ok=True)
    for p in FROZEN:
        if p.startswith("predictions/") or p.endswith("RULES-expA.md"):
            shutil.copy2(os.path.join(ROOT, p), os.path.join(OUT, "frozen", os.path.basename(p)))
    json.dump(rec, open(path, "w", encoding="utf-8"), indent=1)
    log(f"freeze record written: {path}")


def cells():
    out = []
    for g, cond in GRAPHS.items():
        for s in (STOPS_X if g == "parent_x" else STOPS_OTHER):
            out.append((g, cond, s, 10 if s in T_CLASS else 3))
    return out


def c_free_gb():
    st = os.statvfs(DISK)
    return st.f_bavail * st.f_frsize / 1e9


def run_one(graph, cond, stop, k):
    cmd = ["bash", os.path.join(HERE, "run_trial_ns.sh"), "--graph", graph, "--levers", stop, "--trials", "1",
           "--start-k", str(k), "--cond", cond, "--outdir", OUT]
    if stop == "S2":
        cmd += ["--pre", "245"]
    t0 = time.monotonic()
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    dt = time.monotonic() - t0
    tail = (p.stderr or "").strip().splitlines()[-3:]
    return p.returncode, dt, tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", default="1", help="a round number or a range, e.g. 1 or 2-10")
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    lo, _, hi = a.rounds.partition("-")
    rounds = range(int(lo), int(hi or lo) + 1)
    os.makedirs(OUT, exist_ok=True)
    plan = cells()
    log(f"batch start: rounds {a.rounds}; {len(plan)} cells; KEYC has no class in the predictions and is run as D (3 trials)")
    freeze()
    for r in rounds:
        todo = [c for c in plan if c[3] >= r]
        random.Random(a.seed + r).shuffle(todo)
        log(f"round {r}: {len(todo)} trials")
        for graph, cond, stop, _ in todo:
            if os.path.exists(STOPFILE):
                log("stop file found; ending the batch")
                return
            free = c_free_gb()
            if free < 10:
                log(f"{DISK} has {free:.1f} GB free (< 10 GB floor); ending the batch")
                return
            tid = f"{cond}-{stop}-t{r}"
            if a.dry_run:
                log(f"  would run {tid} ({graph})")
                continue
            log(f"  start {tid} ({graph})")
            rc, dt, tail = run_one(graph, cond, stop, r)
            log(f"  end   {tid} rc={rc} in {dt / 60:.1f} min" + (f"; stderr tail: {tail}" if rc else ""))
    log("batch done")


if __name__ == "__main__":
    main()
