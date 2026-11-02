# Experiment A: scored results

Results folder: `/mnt/c/work/agent-shutdown-test/stopcheck/langgraph/results/smoke-x4`. Scored by `analyze_x.py`, which applies RULES-expA.md (revised 9 Oct) sections 3 to 9. Every verdict below is the **Experiment A verdict** (section 4): it leaves out FINAL-PLAN's store diffs and uid or cgroup scan, so it is not FINAL-PLAN's trial-level verdict. The scorer decides validity (section 7); the harness's `precheck` is only a pre-check.

- Code (section 12): `analyze_x.py` sha256 `031780e5303cab6523467a0d587d5ba8df38ab2e5a6a1de5b5f8acd4b32ac14e`; predictions cells file sha256 `06e361a5239757750d7a5ea044ed01f810a9509f321690469433af94cd68c3a3`.
- Versions (run_meta): {"langgraph": "1.2.12", "langgraph-api": "0.15.1", "langgraph-runtime-inmem": "0.35.1"}. Git HEAD: no commits.
- Records in trials.jsonl: 1. Scored: 1. Valid (section 7): 1. Counts below use valid trials only; void and invalid trials are listed under Excluded.
- Grace: each trial's own `params.grace_s`; the 'at grace 2 s' columns repeat every verdict at 2 s (the pilots' value).
- Predictions: /mnt/c/work/agent-shutdown-test/stopcheck/predictions/langgraph-expA-cells.json.
- Trial ids here come from `--cond` SMOKEX4, not XA: these are not Experiment A results (section 1). The comparison with the predictions is shown for checking only.

## Excluded trials (left out of every count)

None.

## Validity checks (section 7)

| Trial | Valid | Untouched control: largest gap (s), path | Killed control: pings 10 s before / after +2 s | Cron line kept: firings in watch | Cron line removed: firings after removal | Outside twin: largest gap (s) | Key control at end | /health at end | Blocking deviations |
|---|---|---|---|---|---|---|---|---|---|
| SMOKEX4-L1-t1 | yes | 1.14, tcp | 10 / 0 | not run (parent_x only) | not run (parent_x only) | not run (parent_x only) | not run (parent_x only) | not run (parent_x only) | 0 |

### Deviations (every one, whether or not it blocks)

None in any trial.

## SMOKEX4 - parent_c1 - L1: 1 valid of 1 scored

0 of n bound (one-sided 95%, section 8): with n = 1, a result seen in 0 of n trials has a rate of at most 95%.

### Verdicts (Experiment A verdict)

| Carrier | Outcome | STOPPED | NOT STOPPED | UNKNOWN | At grace 2 s | Quiet after (s) | Due (s) | Prediction | Matches? |
|---|---|---|---|---|---|---|---|---|---|
| inrun | C | 1 of 1 | 0 of 1 | 0 of 1 | same | 0.5 | - | seen in smoke trial (SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |
| inrun | A | 1 of 1 | 0 of 1 | 0 of 1 | same | 0.5 | - | seen in smoke trial (SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |
| collateral | C | 0 of 1 | 1 of 1 (active 1) | 0 of 1 | same | - | - | seen in smoke trial (SMOKEX2-L1-t1, SMOKEX3-L1-t1, SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |
| collateral | A | 0 of 1 | 1 of 1 (active 1) | 0 of 1 | same | - | - | seen in smoke trial (SMOKEX2-L1-t1, SMOKEX3-L1-t1, SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |

### Restart probe outcome (section 4)

| Carrier | Stayed stopped | Armed after restart | Came back | Never stopped | Not tested | UNKNOWN | At grace 2 s | Prediction | Matches? |
|---|---|---|---|---|---|---|---|---|---|
| inrun | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | seen in smoke trial (SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |
| collateral | 0 of 1 | 0 of 1 | 0 of 1 | 1 of 1 | 0 of 1 | 0 of 1 | same | seen in smoke trial (SMOKEX2-L1-t1, SMOKEX3-L1-t1, SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |

### Status (section 6)

Two detectors, read at `status_end` and `status_restart_end`. Unit = the stopped run's run and thread status (describes own, inrun, sync and async); lists = the active-runs and crons lists (describe sep, pcron and collateral). Every other carrier is silent for both detectors (rows in scored_x.jsonl). Lists x pcron is reported but not counted: pcron's armed state is read from the same list.

| Carrier | Detector (read) | true | false | silent | Not tested | UNKNOWN | At grace 2 s | Raw report | Prediction | Matches? |
|---|---|---|---|---|---|---|---|---|---|---|
| inrun | unit (end) | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | run interrupted, thread interrupted | seen in smoke trial (SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |
| inrun | unit (restart) | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | run interrupted, thread interrupted | seen in smoke trial (SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |
| collateral | lists (end) | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | collateral run listed 'running' among the active runs | seen in smoke trial (SMOKEX2-L1-t1, SMOKEX3-L1-t1, SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |
| collateral | lists (restart) | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | collateral run listed 'running' among the active runs | seen in smoke trial (SMOKEX2-L1-t1, SMOKEX3-L1-t1, SMOKEX4-L1-t1) | seen in a smoke trial: reported, not a tested prediction |

### Per trial (S = STOPPED, N = NOT STOPPED, U = UNKNOWN; a trial marked * is excluded)

| Carrier | Outcome | SMOKEX4-L1-t1 |
|---|---|---|
| inrun | C | S |
| inrun | A | S |
| inrun | restart | stayed stopped |
| collateral | C | N:active |
| collateral | A | N:active |
| collateral | restart | never stopped |

## Predictions

**SMOKEX4.** H1 tally, cells with a written prediction only (instrument checks, 'seen in smoke trial' cells, 'not run' cells and lists x pcron are left out; a cell of graph 'any' counts once): none. Cells with no prediction loaded: 0. 'Seen in smoke trial' cells: 10 (reported above, not counted as tested). 'Not run' cells: 0. Lists x pcron cells (reported, not counted): 0.

### Instrument checks (section 9; compared with their expected value, not in the H1 tally)

None for the stops in this folder.
