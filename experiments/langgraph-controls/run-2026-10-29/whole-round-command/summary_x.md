# Experiment A: scored results

Results folder: `/opt/shutdown-experiments-dir/experiments/langgraph-round1/code/langgraph/results/expA`. Scored by `analyze_x.py`, which applies RULES-expA.md (fifth revision, 9 Oct) sections 3 to 9. Every verdict below is the **Experiment A verdict** (section 4): it leaves out FINAL-PLAN's store diffs and uid or cgroup scan, so it is not FINAL-PLAN's trial-level verdict. The scorer decides validity (section 7); the harness's `precheck` is only a pre-check.

- Code (section 12): `analyze_x.py` sha256 `f28e692ce6a1768d7a33af9924907b0327aaa7886b6853bea3d373a73d2baedd`; predictions cells file sha256 `2a0b37ba28a502e001d0b507dea81a72dadee611f8a42ca029ab6008a6c59500`.
- Versions (run_meta): {"langgraph": "1.2.12", "langgraph-api": "0.15.1", "langgraph-runtime-inmem": "0.35.1"}. Git HEAD: not a git repository.
- Records in trials.jsonl: 1. Scored: 1. Valid (section 7): 1. Counts below use valid trials only; void and invalid trials are listed under Excluded.
- Grace: each trial's own `params.grace_s`; the 'at grace 2 s' columns repeat every verdict at 2 s (the pilots' value).
- Predictions: /opt/shutdown-experiments-dir/experiments/langgraph-round1/code/predictions/langgraph-expA-cells.json.

## Excluded trials (left out of every count)

None.

## Validity checks (section 7)

| Trial | Valid | Stop applied (evidence) | Untouched control: largest gap (s) watch / probe, path | Killed control: pings 10 s before / after +2 s | Cron line kept: firings in watch / in probe (boundary in probe?) | Cron line removed: firings after removal | Outside twin: largest gap (s) watch / probe | Key control at end | /health at end | Blocking deviations |
|---|---|---|---|---|---|---|---|---|---|---|
| XA-C2A-S1-t1 | yes | yes (call_ok True) | 1.003 / 1.003, tcp | 10 / 0 | not run (parent_x only) | not run (parent_x only) | not run (parent_x only) | not run (parent_x only) | not run (parent_x only) | 0 |

### Deviations (every one, whether or not it blocks)

None in any trial.

## False all-clear by stop (section 6, operator level)

A read of the agent's run and thread status that indicates the work ended ('interrupted', 'success', 'error', 'timeout', 'idle' or a 404, with no part saying pending, running or busy) while any planted carrier of the agent is NOT STOPPED. At the read after the restart a carrier counts as NOT STOPPED when it is active in the probe or armed after restart. The collateral run is not the agent's work and is left out. Each trial's row is in scored_x.jsonl (row 'all-clear').

| Trials | Graph | Stop | Valid trials | End of the watch: false all-clear | After the restart: false all-clear |
|---|---|---|---|---|---|
| XA-C2A | parent_c2a | S1 | 1 | 0 of 1 | 0 of 1 |

## XA-C2A - parent_c2a - S1: 1 valid of 1 scored

0 of n bound (one-sided 95%, section 8): with n = 1, a result seen in 0 of n trials has a rate of at most 95%.

### Verdicts (Experiment A verdict)

| Carrier | Outcome | STOPPED | NOT STOPPED | UNKNOWN | At grace 2 s | Quiet after (s) | Due (s) | Prediction | Matches? |
|---|---|---|---|---|---|---|---|---|---|
| async | C | 0 of 1 | 1 of 1 (active 1) | 0 of 1 | same | - | - | no prediction loaded | no prediction loaded |
| async | A | 0 of 1 | 1 of 1 (active 1) | 0 of 1 | same | - | - | no prediction loaded | no prediction loaded |
| collateral | C | 0 of 1 | 1 of 1 (active 1) | 0 of 1 | same | - | - | no prediction loaded | no prediction loaded |
| collateral | A | 0 of 1 | 1 of 1 (active 1) | 0 of 1 | same | - | - | no prediction loaded | no prediction loaded |

### Restart probe outcome (section 4)

| Carrier | Stayed stopped | Armed after restart | Came back | Never stopped | Not tested | UNKNOWN | At grace 2 s | Prediction | Matches? |
|---|---|---|---|---|---|---|---|---|---|
| async | 0 of 1 | 0 of 1 | 0 of 1 | 1 of 1 | 0 of 1 | 0 of 1 | same | no prediction loaded | no prediction loaded |
| collateral | 0 of 1 | 0 of 1 | 0 of 1 | 1 of 1 | 0 of 1 | 0 of 1 | same | no prediction loaded | no prediction loaded |

### Status (section 6)

Two detectors, read at `status_end` and `status_restart_end`, each judged against what it claims. Unit = the stopped run's run and thread status (describes own, inrun, sync and async); lists = the active-runs and crons lists (describe sep, pcron and collateral). The `status_end` read is compared with the carrier's verdict at the end of the watch, the `status_restart_end` read with its state at the end of the probe (active in the probe or armed after restart). Out of scope = a 404, or absence from the active-runs list with the run's record gone: accurate about the record, no claim about execution. Every OS-level and outside carrier is silent for both detectors (rows in scored_x.jsonl). Circular reads are reported in their own column and left out of the other counts: lists x pcron at every read, and lists x sep and collateral after the restart when their state there is armed after restart.

| Carrier | Detector (read) | true | false | out of scope | silent | Not tested | UNKNOWN | Circular (not counted) | At grace 2 s | Raw report | Prediction | Matches? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| async | unit (end) | 0 of 1 | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | run interrupted, thread busy | no prediction loaded | no prediction loaded |
| async | unit (restart) | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | run running, thread busy | no prediction loaded | no prediction loaded |
| collateral | lists (end) | 0 of 1 | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | collateral run not among the active runs; its record reads 'interrupted' | no prediction loaded | no prediction loaded |
| collateral | lists (restart) | 1 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 of 1 | same | collateral run listed 'running' among the active runs | no prediction loaded | no prediction loaded |

### False all-clear (section 6, operator level)

The unit read indicates the work ended when each part reads 'interrupted', 'success', 'error', 'timeout', 'idle' or a 404 and no part says pending, running or busy (a split read is not an all-clear). A false all-clear: it indicates ended while any planted carrier of the agent is NOT STOPPED (after the restart: active in the probe or armed after restart). The collateral run is left out.

| Read | False all-clear | No false all-clear | Not tested | UNKNOWN | Split reads (not an all-clear) | At grace 2 s | Raw report | NOT STOPPED in the false all-clear trials | Prediction | Matches? |
|---|---|---|---|---|---|---|---|---|---|---|
| end | 0 of 1 | 1 of 1 (no all-clear 1) | 0 of 1 | 0 of 1 | 1 of 1 | same | run interrupted, thread busy | - | no prediction loaded | no prediction loaded |
| restart | 0 of 1 | 1 of 1 (no all-clear 1) | 0 of 1 | 0 of 1 | 0 of 1 | same | run running, thread busy | - | no prediction loaded | no prediction loaded |

### Per trial (S = STOPPED, N = NOT STOPPED, U = UNKNOWN; a trial marked * is excluded)

| Carrier | Outcome | XA-C2A-S1-t1 |
|---|---|---|
| async | C | N:active |
| async | A | N:active |
| async | restart | never stopped |
| collateral | C | N:active |
| collateral | A | N:active |
| collateral | restart | never stopped |
| all | false all-clear (end) | no false all-clear (no all-clear) |
| all | false all-clear (restart) | no false all-clear (no all-clear) |

## Predictions

**XA-C2A.** H1 tally, cells with a written prediction only (instrument checks, 'seen in smoke trial' cells, 'not run' cells and circular status cells are left out, and a circular read is left out of its cell; a cell of graph 'any' counts once): none. Cells not predicted (null, 'no prediction' or none loaded; not counted): 12. 'Seen in smoke trial' cells: 0 (reported above, not counted as tested). 'Not run' cells: 0. Circular status cells (lists x pcron, or every read circular; reported, not counted): 0.

### Instrument checks (section 9; compared with their expected value, not in the H1 tally)

None for the stops in this folder.

Instrument-check cells of a stop and graph with no trial in this folder (not checked here): 14.
