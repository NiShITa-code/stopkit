#!/usr/bin/env python3
"""Experiment A batch runner, sixth revision (RULES-expA.md R6.0, R6.10, R6.12). Run in WSL as root, inside one
long wsl.exe call so WSL does not go idle:
    python3 run_expA.py --rounds 2-11            # every round from 2 on
    python3 run_expA.py --rounds 2-11 --dry-run  # print the plan only

Design:
- Output: results/expA-rev6b/, which must be empty before the first trial (R6.0, R6.13). Round 1 is never pooled;
  the 12 trials in results/expA-rev6/ ran before the clock guard and are set apart, never pooled (R6.13).
- Cells: parent_x under all 12 stops; parent_c1, parent_c2s, parent_c2a under the 7 stops that apply to them
  (section 10). Class T (L2, S1, S2): rounds 2 to 11, 10 trials. Class D: rounds 2 to 4, 3 trials; a D cell whose
  3 valid trials differ on any printed measure becomes class T and also runs rounds 5 to 11 (R6.10), decided by
  scoring the folder before round 5.
- Replacements (section 7, R6.10): after the last round, each invalid trial is rerun once as t<k+20>. A cell
  whose trial and replacement are both invalid is reported incomplete by the scorer.
- Rounds: round r runs every cell that needs an r-th trial, in an order shuffled with a fixed seed per round.
- Trial ids: XA-X-<stop>-t<r> (parent_x), XA-C1-, XA-C2S-, XA-C2A- (the other graphs). S2 uses --pre 245.
- Before the first trial (R6.12) the runner refuses to start unless: the stopcheck repo has a HEAD and no
  uncommitted changes; the regression tests and reproduce.py pass; and the freeze record matches (it is
  written, with the sha256 of every frozen file and of round 1's result files, if the folder is empty).
- Safety: stops before a trial if C: has under 10 GB free, if /root/expA.STOP exists, or if the trial clock is
  off (R6.13: kernel tick not nominal, or CLOCK_MONOTONIC not keeping pace with CLOCK_MONOTONIC_RAW).
"""
import argparse
import datetime
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "common"))
import clockguard  # noqa: E402  (stopcheck/common/clockguard.py, R6.13)

OUT = os.path.join(HERE, "results", "expA-rev6b")
R1 = os.path.join(HERE, "results", "expA-round1")
STOPFILE = "/root/expA.STOP"
GRAPHS = {"parent_x": "XA-X", "parent_c1": "XA-C1", "parent_c2s": "XA-C2S", "parent_c2a": "XA-C2A"}
STOPS_X = ["L1", "L2", "L3", "DA", "S1", "S2", "L4", "PG", "CC", "KEY", "KEYC", "NET"]
STOPS_OTHER = ["L1", "L2", "S1", "S2", "L4", "PG", "CC"]
T_CLASS = {"L2", "S1", "S2"}
FIRST_ROUND, D_LAST, T_LAST, REPLACE_OFFSET = 2, 4, 11, 20
# R6.12: every file the trials, the scoring and the reproduction depend on
FROZEN = ["langgraph/RULES-expA.md", "predictions/langgraph-expA-cells.json", "predictions/langgraph-expA-DRAFT.md",
          "langgraph/harness.py", "langgraph/app/agent_graphs.py", "langgraph/app/langgraph.json",
          "langgraph/canary.py", "langgraph/recorder.py", "langgraph/run_trial_ns.sh", "langgraph/run_expA.py",
          "common/mocks.py", "common/guards.py", "common/clockguard.py", "common/test_clockguard.py", "langgraph/analyze_x.py", "langgraph/measures6.py",
          "langgraph/naive_checks.py", "langgraph/archive/analyze_x_rev5.py", "reproduce.py"]


def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "batch.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def frozen_files():
    files = list(FROZEN)
    tests = os.path.join(ROOT, "tests")
    for fn in sorted(os.listdir(tests)):
        if fn.endswith((".py", ".json")) and not fn.startswith("."):
            files.append(f"tests/{fn}")
    out = {p: sha(os.path.join(ROOT, p)) for p in files}
    for dirpath, _, fns in os.walk(R1):
        for fn in sorted(fns):
            p = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/")
            out[p] = sha(os.path.join(ROOT, p))
    return out


def git(*args):
    return subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, text=True)


def preflight(need_clean=True):
    """R6.12: refuse to run on uncommitted code, without a HEAD, or with failing tests or reproduction.
    need_clean=False (--freeze-only) skips the clean-tree check, since the freeze record is committed with the code."""
    why = []
    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        why.append("no git HEAD in the stopcheck repo")
    dirty = [x for x in git("status", "--porcelain").stdout.splitlines() if x.strip()]
    if dirty and need_clean:
        why.append(f"uncommitted changes: {dirty[:5]}")
    t = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", os.path.join(ROOT, "tests"),
                        "-t", ROOT], cwd=ROOT, capture_output=True, text=True)
    if t.returncode != 0:
        why.append("regression tests fail: " + (t.stderr or t.stdout)[-400:])
    r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "reproduce.py"), "--quick"], capture_output=True,
                       text=True)
    if r.returncode != 0:
        why.append("reproduce.py fails: " + r.stdout[-400:])
    c = subprocess.run([sys.executable, "-B", "-m", "unittest", os.path.join("common", "test_clockguard.py")],
                       cwd=ROOT, capture_output=True, text=True)
    if c.returncode != 0:
        why.append("clock guard tests fail: " + (c.stderr or c.stdout)[-400:])
    ck = clockguard.check(2.0)  # R6.13
    if not ck["ok"]:
        why.append("trial clock is off (R6.13): " + "; ".join(ck["reasons"]))
    return head.stdout.strip(), why


def freeze(head, smoke):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "FREEZE.json")
    files = frozen_files()
    if os.path.exists(path):
        old = json.load(open(path, encoding="utf-8"))
        changed = [p for p in sorted(set(old["files"]) | set(files)) if old["files"].get(p) != files.get(p)]
        if changed:
            log(f"FREEZE MISMATCH: these files changed since the freeze: {changed}; refusing to run")
            sys.exit(5)
        log(f"freeze record matches ({old['written']}, parent commit {old.get('git_parent', '?')[:10]})")
        return
    trials = os.path.join(OUT, "trials.jsonl")
    if os.path.exists(trials) and os.path.getsize(trials) > 0:
        log("results/expA-rev6b already holds trials but no freeze record; refusing to run")
        sys.exit(5)
    rec = {"written": datetime.datetime.now().isoformat(timespec="seconds"), "git_parent": head,
           "note": "committed together with the code it lists; git_parent is the commit before that one",
           "rules": "RULES-expA.md, sixth revision with R6.13 (clock guard)", "smoke_trials_excluded": smoke, "files": files}
    json.dump(rec, open(path, "w", encoding="utf-8"), indent=1)
    log(f"freeze record written: {path} ({len(files)} files, parent commit {head[:10]})")


def base_cells():
    out = []
    for g, cond in GRAPHS.items():
        for s in (STOPS_X if g == "parent_x" else STOPS_OTHER):
            out.append((g, cond, s, "T" if s in T_CLASS else "D"))
    return out


def c_free_gb():
    st = os.statvfs("/mnt/c")
    return st.f_bavail * st.f_frsize / 1e9


def run_one(graph, cond, stop, k):
    cmd = ["bash", os.path.join(HERE, "run_trial_ns.sh"), "--graph", graph, "--levers", stop, "--trials", "1",
           "--start-k", str(k), "--cond", cond, "--outdir", OUT]
    if stop == "S2":
        cmd += ["--pre", "245"]
    t0 = time.monotonic()
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return p.returncode, time.monotonic() - t0, (p.stderr or "").strip().splitlines()[-3:]


def score_folder():
    """Score results/expA-rev6b into a scratch folder; returns the scored rows (the trials are not touched)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "analyze_x.py"), "--res", OUT, "--out", tmp],
                           capture_output=True, text=True)
        if p.returncode != 0:
            log(f"scoring failed: {p.stderr[-300:]}")
            return []
        return [json.loads(x) for x in open(os.path.join(tmp, "scored_x.jsonl"), encoding="utf-8") if x.strip()]


def split_cells(rows):
    """R6.10: D cells whose valid trials differ on any printed measure."""
    kinds = {}
    for r in rows:
        if not r.get("trial_valid") or r["row"] not in ("verdict", "M1", "M2"):
            continue
        for f in ("kind", "m3_d2", "m3_d5", "m3_d60", "m3_apts"):
            if f in r:
                kinds.setdefault((r["graph"], r["stop"], r["row"], r["carrier"], r["outcome"], f), set()).add(r[f])
    out = set()
    for (g, s, *_), ks in kinds.items():
        if s not in T_CLASS and len({k for k in ks if k not in ("UNKNOWN", None)}) > 1:
            out.add((g, s))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", default=f"{FIRST_ROUND}-{T_LAST}", help="e.g. 2-11 (the default) or 5-11")
    ap.add_argument("--seed", type=int, default=20261010)
    ap.add_argument("--smoke", default="SMOKEX6-L1-t1, SMOKEX6S-L1-t1 (results/smoke-x6)",
                    help="smoke trials of this harness, named in the freeze record and excluded (R6.12)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--freeze-only", action="store_true", help="run the checks, write the freeze record, stop")
    a = ap.parse_args()
    if a.freeze_only:
        head, why = preflight(need_clean=False)
        if why:
            for x in why:
                log(f"PREFLIGHT FAILED: {x}")
            sys.exit(6)
        freeze(head, a.smoke)
        return
    lo, _, hi = a.rounds.partition("-")
    rounds = range(int(lo), int(hi or lo) + 1)
    if min(rounds) < FIRST_ROUND:
        sys.exit("round 1 is not rerun: it is the fifth-revision record (R6.0)")
    plan = base_cells()
    log(f"batch start: rounds {a.rounds}; {len(plan)} cells (T: rounds {FIRST_ROUND}-{T_LAST}, D: rounds "
        f"{FIRST_ROUND}-{D_LAST} unless split)")
    if not a.dry_run:
        head, why = preflight()
        if why:
            for x in why:
                log(f"PREFLIGHT FAILED: {x}")
            sys.exit(6)
        freeze(head, a.smoke)
    extended = None
    for r in rounds:
        if r > D_LAST and extended is None:
            extended = split_cells(score_folder()) if not a.dry_run else set()
            log(f"D cells that split and run to 10 trials (R6.10): {sorted(extended) or 'none'}")
        todo = [c for c in plan if c[3] == "T" or r <= D_LAST or (c[0], c[2]) in (extended or set())]
        random.Random(a.seed + r).shuffle(todo)
        log(f"round {r}: {len(todo)} trials")
        for graph, cond, stop, _ in todo:
            if os.path.exists(STOPFILE):
                log("stop file found; ending the batch")
                return
            if not a.dry_run and c_free_gb() < 10:
                log(f"C: has {c_free_gb():.1f} GB free (< 10 GB floor); ending the batch")
                return
            tid = f"{cond}-{stop}-t{r}"
            if not a.dry_run:
                ck = clockguard.check(1.0)  # R6.13
                if not ck["ok"]:
                    log(f"trial clock is off before {tid} ({'; '.join(ck['reasons'])}); ending the batch")
                    return
            if a.dry_run:
                log(f"  would run {tid} ({graph})")
                continue
            log(f"  start {tid} ({graph})")
            rc, dt, tail = run_one(graph, cond, stop, r)
            log(f"  end   {tid} rc={rc} in {dt / 60:.1f} min" + (f"; stderr tail: {tail}" if rc else ""))
    if T_LAST in rounds and not a.dry_run:  # replacements, once each (section 7)
        rows = score_folder()
        bad = sorted({(r["graph"], r["stop"], r["k"]) for r in rows if not r.get("trial_valid")
                      and int(r["k"]) < REPLACE_OFFSET})
        log(f"invalid trials to replace once: {bad or 'none'}")
        for graph, stop, k in bad:
            if os.path.exists(STOPFILE):
                log("stop file found; ending the batch")
                return
            rc, dt, tail = run_one(graph, GRAPHS[graph], stop, int(k) + REPLACE_OFFSET)
            log(f"  replacement {GRAPHS[graph]}-{stop}-t{int(k) + REPLACE_OFFSET} rc={rc} in {dt / 60:.1f} min")
    log("batch done")


if __name__ == "__main__":
    main()
