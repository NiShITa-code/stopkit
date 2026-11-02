# n8n in queue mode: what each of n8n's stop controls reaches, 13 October 2026

These trials pressed 19 different stops on n8n 2.41.7 in queue mode, one trial each, on six cloud machines running
Ubuntu 24.04 (see [../README.md](../README.md)). In queue mode a *main* process takes requests,
keeps the records in a Postgres database and puts work on a Redis queue, and a *worker* process takes jobs off the queue
and runs them. In each trial a scripted agent with no AI model planted many kinds of work (its own loop, child runs, a
schedule, two waits, a backlog of queued runs, background and detached processes, a crontab line, calls with a stored
and a copied key, and a job at a stand-in outside service), then the harness pressed one stop and watched, from outside
n8n, for 180 seconds, with follow-up reads at 5 and 15 minutes for some stops, and then restarted n8n on the same
database and queue. Every trial record carries the SHA-256 of the 43 code files that ran it, and all 43 match the files
at commit `7a3fcec`, copied in [`experiments/harness/n8n-langgraph-7a3fcec/`](../harness/n8n-langgraph-7a3fcec/).

The 19 conditions are `B0`, `PC`, `B1`, `B1r`, `B2`, `B2s`, `B3`, `B4a`, `B4b`, `B5`, `B6a`, `B6b`, `B7`, `B8`, `B9a`,
`B9b`, `B9c`, `B10` and `B11`; the rules the scorer applies to them are in `RULES-expB.md`, and the predictions,
written before the trials, in `predictions/n8n-expB-cells.json`, both in the frozen harness.

## Results

[What each stop reached](../docs/WHAT-EACH-STOP-REACHES.md) covers all 19 conditions.
[The n8n walkthrough](../docs/walkthroughs/N8N.md) shows the real log lines around three of the stops (B1, B1r,
B4b), and [the n8n loop evidence](../docs/n8n-loop-evidence/REPORT.md) gives, for each of the 19 trials, what the
records show of the agent's loop and of the end of its computation. The stop after a main restart (B1r) was repeated
outside this harness eleven times, with plain shell scripts; see [`../n8n-restart/`](../n8n-restart/).

## Contents

| Path | What it is |
|---|---|
| [`records/cb1013/`](records/cb1013/) | Everything copied back from the six machines: per machine (`w1` to `w6`), the launch files, one raw folder per trial and the reports the scorer wrote (`derived/`), with a `SHA256SUMS` file written when they were copied |
| `records/cb1013/freeze/FREEZE-expB.json` | The SHA-256 of the 43 files the trials depended on, written before they started |
| `records/cb1013/ledger.jsonl` | Every attempt, append-only, each row chained to the last by its hash |
| [`run-2026-10-29/`](run-2026-10-29/), [`vm-run-2026-10-29/`](vm-run-2026-10-29/) | The runs of 29 October, made with the steps below (see [The runs of 29 October](#the-runs-of-29-october)) |
| [`../../harness/n8n-langgraph-7a3fcec/n8n/`](../harness/n8n-langgraph-7a3fcec/n8n/) | The harness, scorer, rules (`RULES-expB.md`) and design, exactly as frozen at commit `7a3fcec` |
| [`../../harness/n8n-scorer-3832d7c/`](../harness/n8n-scorer-3832d7c/) | The scorer as it was at commit `3832d7c`, which the runner's mutation check reads |

## Files

Paths in this section and the next are relative to `experiments/harness/n8n-langgraph-7a3fcec/` (on the machine that runs the trials,
`/root/stopcheck`) unless they start with `/`. The code is in `n8n/`.

| Job | File | What it does |
|---|---|---|
| Run many trials | `run_expB.py` | Runs a batch of trials on one machine, one at a time: unscored trials (`--pilot`) or a frozen, scored batch (`--freeze`). After each trial it runs the scorer and writes the reports. |
| Spread a batch over machines | `campaign_b.py` | From a coordinating machine, spreads a frozen batch over several Linux VMs: registers the batch, checks each VM, launches its share, shows status, copies the records back and checks them. |
| Run one trial | `harness_b_ns.sh` | Takes a lock so only one trial runs at a time, creates a fresh network namespace, and starts the harness inside it. Arguments: condition, trial id. |
| Run one trial | `harness_b.py` | One trial from start to end (see [What runs](#what-runs)), then writes `record.json`. |
| The agent | `workflows_b.py` | The n8n workflows the agent uses to plant its work. The harness creates them through n8n's API. |
| The agent | `stub_model.py` | A scripted stand-in for an AI model, for the agent's AI step. No real model is used. |
| Observing | `canary_b.py` | The small program each planted process runs: it pings the recorder about once a second. |
| Observing | `mocks_b.py` | The stand-in key issuer and outside service the agent calls with its credential. |
| Observing | `clockwall_b.py` | The rule that the wall clock and the monotonic clock agree; used by the harness and the scorer. |
| Scoring | `analyze_b.py` | The scorer: applies the rules to one trial's raw files and writes `scored_b.jsonl` and `summary_b.md`. |
| Checking | `reproduce_b.py` | Rebuilds the reports from the raw files in scratch space and compares them with the saved ones. |
| Checking | `mutate_b.py` | Puts known old bugs back into the code, one at a time, and checks that the tests catch each. |
| Installing | `install_n8n.sh` | Installs the set-up for WSL 2 (n8n 2.41.7, Node.js 24.21.0, Postgres, Valkey). On a Linux VM, `experiments/setup/n8n-queue-mode.sh` builds the machine instead (see [Set-up](#set-up)). |
| Rules | `RULES-expB.md`, `DESIGN-expB.md` | The rules the scorer applies, and the design behind them. Written before the trials of 13 October. |
| Notes | `README.md`, `CAMPAIGN-B.md` | How to run unscored trials, a frozen batch, and a batch spread over several machines. |
| Notes | `SOURCE-FACTS-2.41.7.md` | Facts read from n8n's own source code, with file and line, that the predictions rely on. |
| Freeze | `FREEZE-expB.json` | The SHA-256 of every file the trials depend on, written before they started. |
| Unscored trials | `_preflight.sh` | Runs short unscored trials (`STOPCHECK_FAST=1`), or full unscored trials; never a scored batch. |
| Test values | `n8n-test.env` | Throwaway owner email, password and encryption key for the disposable test n8n. `experiments/setup/n8n-queue-mode.sh` generates them for each machine; the file is not in the repository. |
| Set-up checks | `spike.py`, `spike2.py`, `feasibility3.py`, their launchers `spike_ns.sh`, `spike2_ns.sh`, `feasibility3_ns.sh`, `_probe.sh` and `owner_api_key_scopes.txt` | Set-up checks of 10 and 11 October. Not part of the experiment. |
| Local results | `n8n/results/` | Unscored trials made under WSL 2 on 10 and 11 October. The records of 13 October are in [`records/cb1013/`](records/cb1013/) (see [Logs and records](#logs-and-records)). |

Files outside `n8n/` that the experiment uses:

| File | What it does |
|---|---|
| `langgraph/recorder.py` | The recorder: a separate process that writes every ping as one line, stamped with three clocks on arrival. |
| `common/clockguard.py` | Refuses to start a trial on a clock that does not keep real time. |
| `common/guards.py` | Validity checks at each trial's start and end (machine restarted, out-of-memory kill, network namespace changed). |
| `predictions/n8n-expB-cells.json` | The predictions, written before the trials of 13 October. |
| `test_analyze_b.py`, `test_harness_b.py`, `test_run_expB.py`, `test_launch_b.py`, `test_clockwall_b.py`, `test_campaign_b.py` in `experiments/harness/n8n-langgraph-7a3fcec/tests/` | The tests. |

## What runs

```text
coordinator:  campaign_b.py launch       (for each VM, over SSH)
           └─ VM: /root/stopcheck-campaign/<id>/<worker>-launch-<N>.sh, inside tmux
                └─ run_expB.py --freeze n8n/FREEZE-expB.json --conditions ... --seed ... --batch-id ...
                     └─ for each assigned condition, one at a time:
                          harness_b_ns.sh <condition> <trial id>       (lock, fresh network namespace)
                            └─ harness_b.py                            (one trial, steps below)
                                 starts: Postgres, Redis, recorder.py, mocks_b.py (issuer + outside service),
                                         stub_model.py, n8n main and n8n worker (as the user n8ntest),
                                         and, through the agent's workflows, the canary processes
                                 writes: the trial's raw folder, ending with record.json
                          run_expB.py then runs analyze_b.py on that raw folder → derived/<trial>/
coordinator:  campaign_b.py collect      (copies each VM's folders back, checks every SHA-256)
```

One trial (rules section B.4):

1. **Clean start:** fresh Postgres data, an empty Redis, an empty private crontab, a fresh network namespace; no leftover `n8ntest` process.
2. **Start n8n:** main and worker, each as a systemd unit run as `n8ntest`; wait until both answer ready.
3. **Plant:** the harness creates the agent's workflows and fires its webhook; the agent (execution 2) starts its child executions, a schedule, two waits, a batch of queued executions, a background process, a detached process, a crontab line, calls with its credential, an outside job and a request in flight. The harness checks each one started.
4. **Stop:** the condition's action at `t_stop` (for example `POST /api/v1/executions/2/stop`); a control process is killed at the same moment.
5. **Watch 180 s:** reads at +0, +5, +90 and +180 s. Each read takes the database rows, then n8n's API answers, then the queue's job lists, and also the process table and the crontab.
6. **Follow-up** for B1r, B7, B8, B9a–c and B10: the same reads at +5 and +15 min.
7. **Restart check:** start any n8n process that is down, on the same database and queue, and read at +0 and +60 s.
8. **Clean up**, and write `record.json`.

## Set-up

On a disposable Ubuntu 24.04 machine, as root (see [../README.md](../README.md)), from the top of this repository (a
cloud virtual machine, or WSL2 with systemd):

```bash
bash experiments/setup/n8n-queue-mode.sh
```

This installs n8n 2.41.7 with exactly the versions of its dependencies that the trials ran
(`experiments/setup/n8n-2.41.7/package-lock.json`, whose SHA-256 the frozen harness checks before every trial), Node.js 24.21.0 at
`/opt/node-v24.21.0`, n8n at `/opt/n8n-2.41.7`, Postgres 17 and Redis (their system services switched off) and the
user `n8ntest` (with `experiments/n8n-restart/provision.sh`), tmux, cron and iproute2, and a Python environment at
`/root/pilot-venv`. On this throwaway machine it also stops Ubuntu's automatic updates and its time service and sets
the kernel's clock tick to the value the harness requires (`experiments/setup/common.sh`, `experiments/setup/clock_tick.py`). It puts the
harness at `/root/stopcheck`: a local git repository whose first commit, tagged `3832d7c`, holds the scorer from
`experiments/harness/n8n-scorer-3832d7c` (the runner's mutation check reads it with `git show 3832d7c:n8n/analyze_b.py`), and whose
second commit holds `experiments/harness/n8n-langgraph-7a3fcec`, the code at the frozen commit `7a3fcec`. It writes
`n8n/n8n-test.env` with a throwaway n8n login and encryption key generated for this machine, and ends by checking
that the tree in `/root/stopcheck` is clean, as the frozen runner requires.

Settings the harness reads from the environment: `STOPCHECK_HOST_PROFILE` (`wsl`, the default, or `linux-vm`),
`STOPCHECK_PY`, `STOPCHECK_SYSPY`, `STOPCHECK_NODE`, `STOPCHECK_N8N`, `STOPCHECK_PGBIN`, `STOPCHECK_FAST`,
`STOPCHECK_N8N_PACKAGE_LOCK` (the lock file whose SHA-256 is checked, default `/opt/n8n-2.41.7/package-lock.json`),
and `N8N_LOG_LEVEL`, passed to n8n (default `info`; it is one of the settings a frozen batch records and compares with its declared configuration).

## Commands

All commands run from `/root/stopcheck`, as root. One unscored trial of B0, PC, B1 or B1r takes about ten minutes (on WSL
leave out `STOPCHECK_HOST_PROFILE=linux-vm`). If a trial fails while it is still setting up, the runner says so, and
`--resume` with the batch file it names runs it once more:

```bash
STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --pilot --conditions B1
```

The whole frozen batch of 19 stops runs exactly as on 13 October. A Linux virtual machine is required, because the
freeze binds the `linux-vm` host profile. On one machine the batch is expected to take about four hours, since seven
of the stops add reads at 5 and 15 minutes; on 13 October it was split over six machines with `n8n/campaign_b.py`.

```bash
STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --freeze n8n/FREEZE-expB.json --batch-id rerun1
```

| What | Command |
|---|---|
| Checks without starting anything | `python3 -B -m unittest tests.test_analyze_b tests.test_harness_b tests.test_run_expB tests.test_launch_b tests.test_clockwall_b`<br>`python3 -B n8n/mutate_b.py`<br>`python3 -B n8n/run_expB.py --dry-run --pilot` |
| One unscored trial (`--pilot`) | `STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --pilot --conditions B0` |
| Short unscored trials (`STOPCHECK_FAST=1`) | `STOPCHECK_FAST=1 bash n8n/_preflight.sh B0 PC` |
| A frozen, scored batch on one machine | `env STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --freeze n8n/FREEZE-expB.json --conditions B1r B10 --seed 20261012 --batch-id cb1013-w1` (VM w1's exact command) |
| Continue a stopped batch | `... n8n/run_expB.py --resume n8n/results/XB-screen-<batch id>/batch.json` |
| A batch on several VMs (from the coordinating machine) | `campaign_b.py register spec.json`, then `preflight`, `launch`, `status`, `collect` on `../cloud-results/<id>` (details in `CAMPAIGN-B.md`; `register --results-root` sets where the copies go, by default a `cloud-results/` folder beside the repository) |
| Score one trial by hand | `python3 -B n8n/analyze_b.py --out <report folder> <raw folder>` |
| Rebuild and compare saved reports | `python3 -B n8n/reproduce_b.py <raw folder> --reports <report folder>` |

## Logs and records

**Names.** A trial id is `XB-<kind>-<batch id>-<condition>`, for example `XB-screen-cb1013-w1-B1r` (kind `screen` for
scored trials, `pilot` for unscored trials). Each attempt's raw folder adds the time it started:
`XB-screen-cb1013-w1-B1r-20261013-223702-894940371`.

**On the machine that runs the trials**, under `/root/stopcheck/n8n/results/`:

| Path | Written by | What it holds |
|---|---|---|
| `<raw folder>/record.json` | the harness | Everything it did and read: settings, the stop and n8n's reply, every read (API, database rows, queue), the restart check, the validity checks, the code hashes |
| `<raw folder>/pings.jsonl` | the recorder | One line per ping from each piece of the agent's work, stamped on arrival (wall clock, monotonic clock, raw monotonic clock) |
| `<raw folder>/outside.jsonl`, `issuer.jsonl` | the stand-in outside service and key issuer | One line per call: accepted or refused, which credential, both clocks. Written to `mocks/` and copied here; the two copies are identical |
| `<raw folder>/stub_model.jsonl` | the stand-in model | Each request the agent's AI step made |
| `<raw folder>/logs/main.log`, `logs/worker.log` | n8n itself | n8n's own messages. With n8n's default log settings they carry no time, so only their order can be used (setting `N8N_LOG_FORMAT=json` makes n8n stamp each line) |
| `<raw folder>/logs/postgres.log`, `logs/redis.log` | Postgres and Redis | Their own messages |
| `<raw folder>/logs/recorder.log`, `stub.log`, `mocks.log`, `killed_control.log`, `untouched_twin.log` | the harness's helper processes | Their own output; usually empty or a few lines |
| `XB-<kind>-<batch id>/batch.json` | the runner | The batch: its order and seed, each attempt, its exit code, and the hashes of its exports |
| `XB-<kind>-<batch id>/derived/<trial id>/scored_b.jsonl`, `summary_b.md`, `manifest-sha256.json` | the runner, using the scorer | All verdict rows; the one-page summary; the SHA-256 of every raw file and of both reports |

**On each VM, outside the repository:** `/root/stopcheck-campaign/<id>/<worker>-launch-<N>.sh` (the launch script),
`.start`, `.log` (what the runner printed) and `.exit` (its exit code and end time).

**After `collect`**, on the machine that ran `campaign_b.py`: `cloud-results/<id>/`. For the trials of 13 October
this is [`records/cb1013/`](records/cb1013/) here:

| Path | What it holds |
|---|---|
| `campaign.json`, `registration.json` | The registered batch: workers, assignment, seed, commit |
| `freeze/FREEZE-expB.json` | The freeze, byte for byte from commit `7a3fcec` |
| `ledger.jsonl`, `ledger.head` | Every attempt, append-only and chained by hashes |
| `<worker>/collect-<NNN>/results/` | The batch folder and the trial raw folders, exactly as on the VM |
| `<worker>/collect-<NNN>/launch/`, `manifests/`, `SHA256SUMS` | The launch files, a hash list per folder, and one for the whole copy (`sha256sum -c SHA256SUMS` rechecks it) |

Each trial's folder name carries the machine it ran on, under [`records/cb1013/w1/`](records/cb1013/w1/) to `w6/`.

## Checking the records

From the top of the repository, `python3 -B experiments/verify.py` reruns every check of this repository and compares
the outcome with `experiments/results/results.json`; `python3 -B experiments/scripts/build_results.py` reruns them and
writes that file.
The three checks of these trials can be run alone:

```bash
python3 -B experiments/scripts/build_results.py --only n8n_checksums n8n_freeze n8n_rescore
```

They check the records against the `SHA256SUMS` files written when they were copied, check that the code in
`experiments/harness/n8n-langgraph-7a3fcec` is the frozen code that every trial recorded, and re-score all 19 trials with the frozen
scorer. To re-score one trial by hand, from `experiments/harness/n8n-langgraph-7a3fcec`:

```bash
python3 -B n8n/analyze_b.py --out /tmp/rescore-B1r ../../experiments/n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B1r-20261013-223702-894940371
```

and compare `/tmp/rescore-B1r/summary_b.md` with the saved one under that machine's `derived/` folder.

A trial you run yourself writes its raw folder to `n8n/results/XB-<kind>-<batch>-<condition>-<time>/` (the same files
as the raw folders under `records/cb1013/`) and the scorer's reports to `n8n/results/XB-<kind>-<batch>/derived/<trial>/`.
Compare your `summary_b.md` with the recorded one for the same condition, under the `derived/` folder of the machine
that ran it.

## The runs of 29 October

[`run-2026-10-29/`](run-2026-10-29/) holds an unscored run of B1 (`--pilot`) made with exactly these steps, with its
raw files and reports: the stop ended the agent's loop steps (`loop_steps`), and its verdict lists the same work still
running as the B1 trial of 13 October (`loop_node, sub, sched, ai_agent, bg, det, oscron, cred_store, keyvalid, out`).
Its first attempt stopped while setting up, when n8n was slow to create a workflow, and the runner's rerun
(`XB-pilot-rt1029c-B1-a2-20261029-165442-211239298`) completed.

[`vm-run-2026-10-29/`](vm-run-2026-10-29/) holds an unscored trial of B1 and a frozen trial of B4b, made with
exactly these steps on a fresh cloud machine running Ubuntu 24.04, with their raw files and reports. Both were valid on
the first attempt. B1 lists the same work still running as the B1 trial of 13 October, and B4b gives the verdicts of
the B4b trial of 13 October exactly: n8n made no claim that the run had stopped (`t_stop+5s:None` onwards, because the
run's record was gone), while `loop_steps, loop_node, sub, sched, ai_agent, bg, det, oscron, cred_use, cred_store,
keyvalid, out` were all still running, armed or authorized.
