#!/usr/bin/env python3
"""Mutation check: plant one known bug at a time in a scratch copy of the scorer and confirm the regression tests
catch it. A mutant the tests do not catch ('survived') means a rule the tests do not really check.

  python3 tests/mutate.py      # a few minutes
Exit status 0 only if every mutant is caught."""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LG = os.path.join(ROOT, "langgraph")
TESTS = ["tests.test_rev6", "tests.test_rev6_vs_rev5"]
Q = '"'
# (file, original text, mutated text, what the bug breaks)
MUTANTS = [
    ("measures6.py", "ALLOW = 0.05 ", "ALLOW = 0.0 ", "R6.11 arrival allowance dropped"),
    ("measures6.py", "                if t_c < e[" + Q + "mono" + Q + "] <= T.t_end:\n                    out.append(",
     "                if t_c - 1.0 < e[" + Q + "mono" + Q + "] <= T.t_end:\n                    out.append(",
     "M1 commit window opened 1 s early"),
    ("measures6.py", '"unit": run_claim and th in ax.THREAD_ENDED,', '"unit": run_claim,', "unit claim ignores the thread"),
    ("measures6.py", "    lo = T.t_end - QUIET_TAIL\n", "    lo = T.t_end - 30.0\n", "M2 'running' window 30 s, not 5 s"),
    ("measures6.py", 'return None if ev is None else [e for e in ev if e.get("event") in ("effect", "refused")]',
     'return None if ev is None else [e for e in ev if e.get("event") in ("effect",)]',
     "M1 by commit drops refused calls"),
    ("measures6.py", '"O2": run_claim and th in ax.THREAD_ENDED}', '"O2": run_claim}', "O2 all-clear ignores the thread"),
    ("measures6.py", "and th in (ax.THREAD_ENDED | {nf}),", "and th in ax.THREAD_ENDED,", "O1 no longer counts a 404 thread"),
    ("measures6.py", '    if carrier in ("pcron", "oscron"):\n        a = [o for o in obs if o["armed"]]',
     '    if carrier in ():\n        a = [o for o in obs if o["armed"]]', "M2 schedules from recent firings"),
    ("measures6.py", '            if key == "status_end":', "            if False:", "end-read claim scored as testable"),
    ("measures6.py", '        elif "smono" in p:\n            t = float(p["smono"])',
     '        elif False:\n            t = float(p["smono"])', "M1 initiation ignores sender stamps"),
    ("measures6.py", 'AGENT_MARKERS = ("key_use", "tok_use", "outjob_submit", "outjob_run", "out")',
     'AGENT_MARKERS = ("key_use", "tok_use", "outjob_submit", "outjob_run", "out", "outside_twin")',
     "R6.8 containment counts the harness's own effects"),
    ("measures6.py", '                return "armed", "present and silent: " + a[0]["armed_ev"]',
     '                return "running", "present and silent: " + a[0]["armed_ev"]', "M2 silent sleeper called running"),
    ("analyze_x.py", 'if o["armed"] and not (carrier in ("pcron", "oscron") and outcome == "A"):',
     'if o["armed"]:', "R6.5 schedule A folds armed back in"),
    ("analyze_x.py", '                    s1 = (measures6.m2_as_verdict(m2[carrier]), f"M2 {m2[carrier]}")',
     '                    s1 = (combined(verdicts[carrier]["g"]), "")', "R6.7 status against the cumulative verdict"),
    ("analyze_x.py", 'PROBE_ONLY = ("server back for the restart probe",)', "PROBE_ONLY = ()",
     "R6.9 restart not back blocks again"),
    ("analyze_x.py", '    if "versions" in R:  # R6.0', "    if False:  # R6.0", "R6.0 version check removed"),
    ("analyze_x.py", '                    q["_t"] = float(p["smono"])', '                    q["_t"] = p["mono"]',
     "R6.1 sender stamps ignored"),
    ("analyze_x.py", '"sync_tool": ["parent"], "sync_child": ["bgproc"],',
     '"sync_tool": ["parent", "bgproc"], "sync_child": ["bgproc"],', "R6.5 sync tool merged with its child again"),
    ("analyze_x.py", "        if g is None or g > TWIN_GAP:  # R6.7", "        if False:  # R6.7",
     "R6.9 probe control gap ignored"),
    ("analyze_x.py", 'DESCRIBED = {("unit", c) for c in IN_RUN + ("key_use", "tok_use")}',
     'DESCRIBED = {("unit", c) for c in IN_RUN}', "R6.5 key and token calls left out of the unit detector"),
]


def run_tests(lg_dir):
    env = dict(os.environ, STOPCHECK_LG=lg_dir)
    p = subprocess.run([sys.executable, "-B", "-m", "unittest"] + TESTS, cwd=ROOT, env=env,
                       capture_output=True, text=True)
    return p.returncode


def main():
    if run_tests(LG) != 0:
        print("the tests fail on the unmutated scorer; fix that first")
        sys.exit(2)
    survived = []
    for fn, old, new, what in MUTANTS:
        with tempfile.TemporaryDirectory() as tmp:
            lg = os.path.join(tmp, "langgraph")
            shutil.copytree(LG, lg, ignore=shutil.ignore_patterns("results", "__pycache__", "logs"))
            shutil.copytree(os.path.join(LG, "results", "expA-round1"), os.path.join(lg, "results", "expA-round1"))
            p = os.path.join(lg, fn)
            s = open(p, encoding="utf-8").read()
            if s.count(old) != 1:
                print(f"  ERROR    {what}: the original text occurs {s.count(old)} times in {fn}")
                survived.append(what)
                continue
            open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
            rc = run_tests(lg)
            print(f"  {'caught  ' if rc else 'SURVIVED'} {what}")
            if not rc:
                survived.append(what)
    print(f"\n{len(MUTANTS) - len(survived)} of {len(MUTANTS)} mutants caught")
    sys.exit(1 if survived else 0)


if __name__ == "__main__":
    main()
