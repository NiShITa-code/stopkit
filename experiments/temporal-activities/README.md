# Temporal activities: how a cancel, terminate or kill reaches a running step, 14 October 2026

Temporal is a workflow engine that many teams use to run long jobs, agents among them. A Temporal *workflow* is the
control code of a job, and an *activity* is a step that does the work; a long activity can send *heartbeats*, regular
check-ins with the server, and the reply to a heartbeat is how an activity learns it was cancelled
([Temporal's documentation](https://docs.temporal.io/develop/python/cancellation)). These trials calibrate the method
against that documented path, also measured by Khan (arXiv:2607.14166, §3.4), and extend it to terminate, local
activities, worker kills and restarts. These trials ran
16 conditions, one trial each, on one cloud machine running Ubuntu 24.04 (see [../README.md](../README.md)),
with Temporal's development server (CLI 1.9.1, server 1.32.0, storing its state in SQLite) and the Python SDK 1.34.0.
Each trial ran one job whose activity posted each step's effect to a receiver outside Temporal, pressed one stop (a
cancel, a terminate, or a kill of the worker or the server; three control trials pressed none), watched for 120
seconds, and then restarted the workers and the server on the same database to see what came back. Unlike the
LangGraph and n8n trials, the job planted nothing outside its own run: these trials test only how a stop reaches
running code.

## Results

[What each stop reached](../docs/WHAT-EACH-STOP-REACHES.md) covers all 16 conditions.
[The Temporal walkthrough](../docs/walkthroughs/TEMPORAL.md) shows the real log lines around two of the stops
(C3-a2 and C2). Condition C3 was run twice, because the scorer marks its first attempt invalid (`INVALID: readiness
and capability jobs`, in that trial's `summary_c.md`); the results use the second, `C3-a2`.

## Contents

| Path | What it is |
|---|---|
| [`records/cs1014/`](records/cs1014/) | One raw folder per trial (`record.json`; `receiver.jsonl`, every effect the receiver got; the worker and server logs; Temporal's own SQLite database), the scorer's reports under `XC-screen-c-screen-1014/derived/`, and `cs1014.tgz`, the archive as copied off the machine |
| [`run-2026-10-29/`](run-2026-10-29/), [`vm-run-2026-10-29/`](vm-run-2026-10-29/) | The runs of 29 October, made with the steps below (see [The runs of 29 October](#the-runs-of-29-october)) |
| [`../../harness/temporal-627df0b/`](../harness/temporal-627df0b/) | The harness, scorer, rules (`temporal/RULES-expC.md`), design and predictions, exactly as frozen: the 17 files that `temporal/FREEZE-expC.json` binds, the freeze itself and the predictions, as they were at commit `627df0b` |

## Checking the records

From the top of the repository, `python3 -B experiments/verify.py` reruns every check of this repository and compares
the outcome with `experiments/results/results.json`; `python3 -B experiments/scripts/build_results.py` reruns them and
writes that file.
The two checks of these trials can be run alone:

```bash
python3 -B experiments/scripts/build_results.py --only temporal_freeze temporal_rescore
```

They check that the code in `experiments/harness/temporal-627df0b` is the frozen code and predictions, and re-score all 17 trial
reports. To re-score one by hand, from `experiments/harness/temporal-627df0b`:

```bash
python3 -B temporal/analyze_c.py --out /tmp/rescore-C3 ../../experiments/temporal-activities/records/cs1014/XC-screen-c-screen-1014-C3-a2-20261014-040816
```

## Set-up

On a disposable Ubuntu 24.04 machine, as root (see [../README.md](../README.md)), from the top of this repository (a
cloud virtual machine, or WSL2 with systemd):

```bash
bash experiments/setup/temporal-activities.sh
```

This installs the Temporal CLI 1.9.1 at `/opt/temporal-cli-1.9.1` (downloaded from Temporal's GitHub release and checked
against its published SHA-256), a Python environment at `/root/temporal-venv` with `temporalio` 1.34.0 and the exact
versions of its dependencies that ran (`experiments/setup/constraints-temporal.txt`), tmux, and the harness at
`/root/stopcheck-temporal`: a local git repository whose one commit holds `experiments/harness/temporal-627df0b`, the code at the
frozen commit `627df0b`. On this throwaway machine it also stops Ubuntu's automatic updates and its time service and
sets the kernel's clock tick to the value the harness requires (`experiments/setup/common.sh`, `experiments/setup/clock_tick.py`).

## Running the trials

From `/root/stopcheck-temporal`, one unscored trial (`--pilot`) of any condition (about six minutes; on WSL leave out
`STOPCHECK_HOST_PROFILE=linux-vm`):

```bash
STOPCHECK_HOST_PROFILE=linux-vm python3 -B temporal/run_expC.py --pilot --conditions C3
```

or the whole frozen batch of 16 conditions, exactly as on 14 October (about an hour and a half; a Linux virtual machine
is required, because the freeze binds the `linux-vm` host profile):

```bash
STOPCHECK_HOST_PROFILE=linux-vm python3 -B temporal/run_expC.py --freeze temporal/FREEZE-expC.json --batch-id rerun1
```

Each trial writes its raw folder to `temporal/results/XC-<kind>-<batch>-<condition>-<time>/` (the same files as
`records/cs1014/`) and the scorer's report to `temporal/results/XC-<kind>-<batch>/derived/<trial>/summary_c.md`. Compare
that report with the recorded one for the same condition under `records/cs1014/XC-screen-c-screen-1014/derived/`.

## The runs of 29 October

[`run-2026-10-29/`](run-2026-10-29/) holds an unscored run of C3 (`--pilot`) made with exactly these steps, with its
raw files and report: the run read CANCELED 0.31 s after the cancel, and the activity's 12 remaining effects landed
after that, as in the trials of 14 October.

[`vm-run-2026-10-29/`](vm-run-2026-10-29/) holds an unscored trial of C3 made with exactly these steps on a
fresh cloud machine running Ubuntu 24.04, with its raw files and report: the run read CANCELED 0.21 s after the
cancel, 12 effects landed after that, the activity ran to its final effect, and Temporal's history holds no record of
that execution, as in the trials of 14 October.
