# n8n: a stop sent after the main process restarts

This folder repeats one result of the n8n trials of 13 October on a fresh machine with four commands: after the main
process restarts, a stop says "canceled" while the run keeps working. It uses no code from the test harness: only
`curl` against n8n's public API, `systemctl` and `systemd-run`, read-only `psql` and `redis-cli` queries, and
`sink.py`, a small HTTP listener that appends every request it receives, with its arrival time, to `sink.jsonl`. The
scripts import nothing from the harness (`harness_b.py`), the planting (`workflows_b.py`), the recorder or the scorer
(`analyze_b.py`). Rule B.11 of the harness's rules (`experiments/harness/n8n-langgraph-7a3fcec/n8n/RULES-expB.md`) counts a result of
the harness as a property of n8n only once a short script like these reproduces it with no harness code.

## The question

n8n is a workflow-automation platform with its own AI agent node. In its production set-up, called queue mode, one
*main* process takes requests such as "stop this run", and a separate *worker* process does the work, taking jobs from a
Redis queue. The question is: if main restarts while a run is working, as it does in any upgrade or crash recovery, and
an operator then stops that run through n8n's public API (`POST /api/v1/executions/{id}/stop`), does the work stop, and
does n8n report what happened?

## Result

The work does not stop, and n8n reports that it did. The stop replies HTTP 200 with status `canceled`, and both n8n's
API and its database row read `canceled` from then on, but the worker keeps running the job until n8n's own
300-second task timeout ends it, about four minutes later. A second stop is refused with HTTP 409, because the run
already reads `canceled`. The control, the same stop sent without a restart, ends the run within about a second.

Every repetition gave this result:

| Date | Machine | n8n | Runs | Reproduced | Updates after "canceled" | Records |
|---|---|---|---|---|---|---|
| 14 Oct 2026 | cloud machine, Ubuntu 24.04 | 2.41.7 | 1 | 1 of 1 | 245 | [`results/2026-10-14-vm/`](results/2026-10-14-vm/) |
| 15 Oct 2026 | same machine, with the terminal recorded | 2.41.7 | 1 | 1 of 1 | 243 | [`results/2026-10-15-vm-recording/`](results/2026-10-15-vm-recording/) |
| 21 Oct 2026 | fresh cloud machine, Ubuntu 24.04, built with `provision.sh` | 2.41.7 | 3 | 3 of 3 | 235, 240, 240 | [`results/2026-10-21-fresh-vm/`](results/2026-10-21-fresh-vm/) |
| 21 Oct 2026 | the same fresh machine | 2.44.5 (newest release that day) | 3 | 3 of 3 | 239, 234, 240 | [`results/2026-10-21-fresh-vm/`](results/2026-10-21-fresh-vm/) |
| 29 Oct 2026 | a Windows machine running WSL 2 with Ubuntu 24.04 | 2.41.7 | 1 | 1 of 1 | 203 | [`results/2026-10-29-local/`](results/2026-10-29-local/) |
| 29 Oct 2026 | the same machine, run from this repository's folder with `run_with_ticks.sh` | 2.41.7 | 1 | 1 of 1 | 178 | [`results/2026-10-29-local-from-repo/`](results/2026-10-29-local-from-repo/) |
| 29 Oct 2026 | fresh cloud machine, Ubuntu 24.04, set up with `experiments/setup/n8n-queue-mode.sh` | 2.41.7 | 1 | 1 of 1 | 237 | [`results/2026-10-29-fresh-vm/`](results/2026-10-29-fresh-vm/) |

Each update is a request the agent's run sends to the sink after n8n's `canceled` reply; the scripts print each one
as a `tick`. The number of updates depends only on how far into the run the stop landed, because the run always ends
at n8n's 300-second task timeout: under WSL 2, main took about 70 seconds to restart, so the stop came later in the run. The checks
`n8n_restart_verdicts` and `n8n_restart_updates` of `experiments/scripts/build_results.py` recount, from these records, the 11
reproduced verdicts and the updates of every run, from the archive each run's `teardown.sh` wrote:

```bash
python3 -B experiments/scripts/build_results.py --only n8n_restart_verdicts n8n_restart_updates
```

## The code path

This is a reading of n8n's code, not a test. Main acts on a stop only for runs it holds in its own memory
(`activeExecutions.has(id)` in `executions/execution.service.js`). After a restart that memory is empty, so main only
publishes a `stop-execution` message. The worker's handler for that message (`scaling/execution-stop.service.js`)
returns at once unless the worker's own `activeExecutions` holds the run, and the worker never registers queue jobs
there: its job processor (`scaling/job-processor.js`) tracks them elsewhere. So the message reaches the worker and
does nothing, while main writes `canceled` into the database. n8n's log shows the difference: after the control stop,
main logs "Sent abort signal to worker"; after the stop that follows a restart, it does not. On 21 October the stop
path of 2.44.5 was read as well: the main-side stop and the worker's handler are identical in 2.41.7 and 2.44.5.

## Requirements

A disposable Ubuntu 24.04 machine (x86-64), as root (see [../README.md](../README.md)): a cloud virtual machine, or
WSL2 on Windows with systemd enabled. The set-up installs packages, creates a user called `n8ntest`, and starts
services, so do not use a machine you care about. `provision.sh` needs internet access to download Postgres 17, Redis,
Node.js 24.21.0 and n8n. The test's own traffic stays on `127.0.0.1` and needs the ports 5678, 5679, 5680, 5682, 5434,
6381 and 9911 to be free; n8n itself may still contact its online services (on 29 October its main process refreshed
an online registry of MCP servers). No AI model or API key is used.

## Running it

As root, from this folder:

```bash
bash provision.sh 2.41.7          # once per machine; each n8n version took about 3 minutes to install on 21 Oct
bash setup.sh                     # starts a private n8n (main and worker), Postgres, Redis and the sink; 1 to 3 minutes
bash p1_stop_after_restart.sh     # the test, about 7 minutes; prints its verdict at the end
bash teardown.sh                  # stops everything, archives the logs to /root/n8n-repro-<time>.tgz, removes /srv/n8n-repro
```

To use another n8n version, set it for every step, for example `N8N_VER=2.44.5 bash setup.sh`. Two wrappers run the same
steps for you: `bash run_with_ticks.sh` runs set-up, test and teardown once and prints one line for each update the agent
sends, which is useful on a shared screen (about 8 minutes); `N8N_VER=2.41.7 bash run_repeats.sh 3` runs three repeats
and keeps each one's archive under `/root/n8n-restart-repeats/`.

## Output

The test first runs a control: it starts a run, stops it without a restart, and checks that no update arrives more than
5 seconds later. Then it starts a second run, restarts main, sends the same stop, and reads n8n's API, the Postgres row,
the Redis queue and the sink at +5, +30, +60 and +120 seconds, sends a second stop, and reads once more after the
300-second timeout. The pass rule is written at the top of `p1_stop_after_restart.sh` and was fixed before the first
run. The end of the output looks like this (from the run of 29 October on the fresh cloud machine,
`results/2026-10-29-fresh-vm/out/p1-20261029T182735Z.txt`):

```text
== Verdict (rule at the top of this script)
control: stop A HTTP 200, ticks from A between +5 s and +60 s: 0 -> yes
1. stop B HTTP 200, status canceled -> yes
2. at +5 s API canceled, Postgres canceled -> yes
3. ticks from B between +30 s and +60 s: 29 -> yes
4. Bull active at +60 s: 2 -> yes
P1 REPRODUCED
```

If the control fails, the stop does not act in your set-up at all, and the script reports `P1 NOT REPRODUCED`. `P1` is
the script's name for the result it reproduces, the stop after a main restart; the recorded transcripts print the verdict
with that name, and the `n8n_restart_verdicts` check reads it there.

## Files

| File | What it does |
|---|---|
| `provision.sh` | Installs Postgres 17, Redis, Node.js 24.21.0, each n8n version given (into `/opt/n8n-<version>`), and the user `n8ntest` |
| `lib.sh` | Paths, ports and two helpers (start an n8n unit; wait for n8n's readiness endpoint) |
| `setup.sh` | Starts a private Postgres and Redis, the sink, and n8n in queue mode (one main, one worker). Creates a throwaway owner, an API key and the test workflow |
| `p1_stop_after_restart.sh` | The test of the stop after a main restart. Its pass rule is written at the top of the script and was fixed before the first run |
| `teardown.sh` | Stops everything, archives logs and outputs to `/root/n8n-repro-<time>.tgz`, and removes `/srv/n8n-repro` |
| `run_with_ticks.sh`, `run_repeats.sh` | The two wrappers described above |
| `sink.py` | The HTTP listener that records every update |
| `results/` | The archive from each run, and its unpacked copy |

## Set-up compared with the trials of 13 October

n8n runs on its defaults except for connection settings, a separate user folder and port for each process, debug
logging (so the stop and abort messages are printed), and switched-off telemetry. `setup.sh` lists every setting it
sets.

| Setting | Trials of 13 October (records cb1013) | Here |
|---|---|---|
| Task-runner timeout | 2,070 s | 300 s (n8n's default) |
| Execute Command node | re-enabled | excluded (n8n's default) |
| n8n user folder | one folder shared by main and worker | one folder each |
| Log level | info | debug, so stop and abort messages are printed |
| Redis persistence | off | Redis's default |
| Isolation | own network namespace, mount and cron confinement | none; everything on 127.0.0.1 |
| systemd stop timeout | 40 s | systemd's default |
| Workload | 16 planted carriers | one workflow: a production webhook, then one Code node that sends an update to the sink once a second |

Same in both: n8n 2.41.7, Node v24.21.0, Postgres 17.11, Redis 7.0.15, queue mode with one main and one worker, worker
concurrency 10, and internal task runners. The result therefore does not depend on the harness's planting, recorder,
reads or scorer, nor on the 2,070 s task timeout, the shared user folder, Execute Command or the network namespace of
the trials of 13 October.

## The run of 14 October

The run of 14 October was made on a cloud machine with n8n 2.41.7. Output:
`results/2026-10-14-vm/out/p1-20261014T132322Z.txt`; archive: `results/2026-10-14-vm/n8n-repro-20261014T133103Z.tgz`.
All five parts of the rule held. Times below are UTC. Log line numbers refer to `results/2026-10-14-vm/logs/`.

**The control, run A (same main, no restart).**
- Run A is execution 1.
- The stop at 13:23:38.79 replied HTTP 200, status `canceled`.
- Main logged "Sent abort signal to worker" (`main.log:960`), and the worker logged "Task cancelled" (`worker.log:141`).
- The last update arrived 0.92 s before the stop, and none came after it.
- The job had left Bull's active list by +5 s.

**Run B (main restarted between the run's start and the stop).**
- Run B is execution 2, started at 13:24:38.91.
- **Restart.** Main took 32.0 s to stop and was ready 5.6 s after relaunch. Its start-up queue recovery found "no dangling executions" (`main.log:1341`), because job 2 was still active in Bull.
- **Stop.** The stop at 13:25:33.14, 54.2 s into the run, replied HTTP 200, status `canceled`.
  - Main only published the `stop-execution` message (`main.log:1425`). There is no "Sent abort signal" line for this stop.
  - The worker received the message (`worker.log:160`) and logged no "Task cancelled".
- **Records.** The public API and the Postgres row read `canceled` at every read from +5 s on. Job 2 stayed in Bull's active list at +5, +30, +60 and +120 s.
- **Effects.** The sink received 245 more updates from execution 2 after the stop, the last at +245.45 s.
- **How it ended.** At 13:29:38.92, 300 s after the run started, n8n's default task timeout ended the Code task: "Task execution timed out after 300 seconds" (`worker.log:175`).
- **The row stayed `canceled`.** The worker's final save was refused: "Skipped saving execution data for execution ID 2 - update conditions not met" (`worker.log:182`). At 13:30:09.96 the row still read `canceled`, with the stop's time (13:25:33.138) as `stoppedAt`.
- **Second stop.** It returned HTTP 409: "Only running or waiting executions can be stopped and 2 is currently canceled".

With n8n's defaults, the task timeout sets how long the orphaned run keeps acting: 300 s from the task's start. Here
that was 245 s after the stop, because the stop came 54 s into the run. The worker log warns that this default "will
be reduced from 300 (5 minutes) to 60 (1 minute) in a future version" (`worker.log:23`).
