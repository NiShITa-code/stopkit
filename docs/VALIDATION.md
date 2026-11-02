# Validation of the kit

This file records what has been checked about the kit itself. The records of the runs it describes are in [validation/](../validation/); any of them can be scored again from its stored evidence with `recheck`.

The Linux host binding, with the kit's own scripted agent, runs end to end with the ground truth. It has passed its self-test on known answers, and its ground truth has passed the ground-truth check, the check of the truth on its own (FRAMEWORK.md, section 3.4). Nothing in this file is a claim about any real agent or product.

## Recorded validation runs

The self-test and the ground-truth check ran on 27 October 2026 on Ubuntu 24.04.1 with systemd 255 and Python 3.12.3, from a fresh copy of the kit that only root could write; the LangGraph example ran on 27 October and again on 1 November 2026. Each command repeats its run from the repository root.

| Run | Command | Result | Record |
|---|---|---|---|
| Self-test | `python3 -B -m stopkit selftest`, as root, from the installed copy | Passed (below) | [selftest-20261027T091012Z-70406a](../validation/selftest-20261027T091012Z-70406a) |
| Ground-truth check | `python3 -B -m stopkit e0 --trials 30 --fires`, as root, from the installed copy | Passed (below) | [e0-20261027-092228-f20558](../validation/e0-20261027-092228-f20558) |
| Recheck | `PYTHONPATH=src python3 -B -m stopkit recheck validation/selftest-20261027T091012Z-70406a/run-*` | The same verdicts from the stored evidence for all six runs; `experiments/verify.py` repeats it as its check `selftest_recheck` | the six `run-*` folders of the self-test |
| LangGraph example | `sudo bash examples/langgraph/demo.sh --auto` | Completed; the results are in [examples/langgraph/README.md](../examples/langgraph/README.md) | [langgraph-example-20261027.log](../validation/langgraph-example-20261027.log), [langgraph-example-20261101.log](../validation/langgraph-example-20261101.log) |

`python3 -B experiments/verify.py` runs every check on the repository's records again, including `recheck` on the six saved self-test runs, and compares the outcome with `experiments/results/results.json`.

## The ground-truth check

The ground-truth check asks whether the sealed recorder can be trusted before anything is scored against it (FRAMEWORK.md, section 3.4). Its pass rules are fixed in the docstring of [src/stopkit/e0.py](../src/stopkit/e0.py). The record of this run is [validation/e0-20261027-092228-f20558](../validation/e0-20261027-092228-f20558), with every measurement in `e0.json` and the summary in `E0.md`.

| Part | Pass rule | Measured |
|---|---|---|
| Exit recorded after a kill | Each of 30 processes, launched as agents are and killed by a separate controller through its own process handle, has exactly one exit recorded, never before the kill and at most 50 ms after it | 30 of 30; latency median 1.29 ms, 95th percentile 2.34 ms, maximum 2.59 ms |
| Reports and effects | Launch to the first report below 1 s; delivery below 50 ms; no effect missing between the sink and the truth | Launch to the first report at most 0.167 s; delivery at most 1.17 ms; heartbeat jitter at most 0.6 ms; no difference in any process |
| Launch | The planted processes ignore and block no signal | Ignored: none; blocked: none |
| Blocked work | A process stopped with SIGSTOP for 3 s has no exit recorded, and its truth reading while stopped is present | Passed; a 3.1 s gap in its reports |
| Channel cut, recorder dead | With the recorder's socket removed, an ended process reads indefinite rather than absent; a run whose recorder died before its last check is scored as failed | Indefinite; failed |
| Instruments survive the stops | Through a kill of the agent's scope, a kill of every process of the account, and a start and a restart of its service manager, the recorder's ticks and the sentinel's reports never pause for more than 1 s | Longest pause 0.5 s for the ticks and 0.2 s for the sentinel's reports |
| Trigger fires | Two user timers, a crontab line and an at job each fire as often as their schedule says, within the margins of the scenario | Both timers fired 4 times, 1.07 to 1.35 s after each minute; cron 4 times, 1.37 to 2.17 s after; the at job once, 0.47 s after its minute |

## Self-test on known answers

The self-test runs the null stop and the reference stop on the Linux bundle (the scenario `host-bundle-1`: nine planted items with eleven paths), three times each, with checks at 0 and 10 seconds after the cut-off and after a restart of the account's service manager. It requires the known answers: under the null stop every path is still running, under the reference stop every path has ended, no run has an error, and clean-up leaves the agent account empty every time. It also requires the kit to agree with the ground truth at every reading the truth can judge. The record is [validation/selftest-20261027T091012Z-70406a](../validation/selftest-20261027T091012Z-70406a), and its report is [report/REPORT.md](../validation/selftest-20261027T091012Z-70406a/report/REPORT.md) there.

| Stop | What the kit found |
|---|---|
| Null stop, 3 runs | All eleven paths still running in every run. The null stop reports nothing, so no claim is scored |
| Reference stop, 3 runs | All eleven paths ended in every run. The stop took 1.43 to 1.75 s and reported that everything had ended, which was true. Because some of the planted processes acted after the cut-off and its 0.6 s grace, before the stop killed them, the in-scope result is "ended late" |

Against the ground truth, over all six runs: 192 readings agree, and none is a false absent or a false present; 72 readings, all of triggers, are indefinite, because the truth can read a trigger's absence only after a full firing window. 36 verdicts agree, with no false "ended" and no false "active"; the 30 verdicts of triggers rest on indefinite truth and are not scored. The effects after the stop agree between the sink and the truth for 53 paths, and for one more except for its last effect, which the kill cut off from its report.
