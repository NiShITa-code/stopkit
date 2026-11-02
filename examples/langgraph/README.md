# Live example: stopping a LangGraph workload

This demonstration launches a real **LangGraph development server** and runs a scripted LangGraph agent on it, as a test account. The workload is a fixed script with no language model: it starts a helper, schedules a job and sends regular updates. The script then applies the stops a developer has, one after another, to the same workload: cancel the run, Ctrl+C on the server, `kill -9`, and a restart. During each stop it shows, line by line as they happen, what LangGraph's own log and API say and what still reaches an outside receiver, and pauses for explanation. It ends with cleanup, Stopkit's external listings, a 65-second observation of the remaining activity and a terminal summary. It takes about three minutes unattended, or ten to fifteen minutes with pauses for discussion.

## Prepare and run

Follow the [quick start](../../README.md#quick-start) on a disposable Linux environment with systemd and cgroup v2, such as an Ubuntu 24.04 cloud virtual machine or WSL 2. The commands below run as root, from the copy of the kit that `setup/base.sh` installs into a folder only root can write, for example `/opt/stopkit`:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv cron at iproute2 procps util-linux
sudo systemctl enable --now cron atd
sudo bash setup/base.sh /opt/stopkit               # from the repository: installs the kit into /opt/stopkit
cd /opt/stopkit
sudo python3 -B -m stopkit setup                   # creates the test accounts stopkit-agent, stopkit-sentinel, stopkit-worker
sudo bash examples/langgraph/demo.sh
```

The first launch downloads the pinned LangGraph environment into `/opt/stopkit-demo/venv`; it needs internet access. The demo was run with LangGraph 1.2.12 and langgraph-api 0.15.1; the versions are in [requirements.txt](requirements.txt) and [constraints.txt](constraints.txt). Ports **2024** (LangGraph) and **8899** (outside receiver) must be free. No model API key is used.

Press **Enter** at each prompt. The script sends cancellation and Ctrl+C to the server itself; pressing Ctrl+C in the demo terminal would interrupt the demonstration. To run it without prompts:

```bash
sudo bash examples/langgraph/demo.sh --auto
```

The demo clears any existing work under the kit's `stopkit-agent` account, replaces that account's demo application, and removes its work and LangGraph saved state when it finishes. Keep that account dedicated to the example.

## Follow one run

| Stage | What the script does | What to inspect |
|---|---|---|
| 1. Start | Launches `langgraph dev` as `stopkit-agent` | LangGraph's log (`Application started up`), the server's readiness and the path of its full log |
| 2. Launch work | Starts a helper, a per-minute cron job and a synchronous task with a 30-minute limit | LangGraph's log (`Created run`, `Starting background run`), its answer (`running`), and updates from the task and helper |
| 3. Cancel | Sends `client.runs.cancel(thread_id, run_id)` | LangGraph's log (`Cancelled runs`, `Background run interrupted`), its answer (`interrupted`), and updates arriving after the cancel |
| 4. Ctrl+C | Sends SIGINT to the server's process group, and repeats it if the server stays alive | LangGraph's log (`Shutting down background workers`), whether the server answers, whether its process ends, and whether updates continue |
| 5. Terminate | Sends SIGKILL to the server's process group if it is still running | Which activity ends and whether the detached helper or schedule remains |
| 6. Restart | Starts the development server on its saved state | LangGraph's log (`Application started up`), its answer about the canceled run, and whether that run resumes |
| 7. Inspect | Uses Stopkit to list what remains under the test account | Processes, the listening port, the crontab line and LangGraph's saved state on disk |
| 8. Clean up | Ends the remaining test work and removes saved state, then lists again and observes for 65 seconds | The final listings, incoming activity and summary counts |

The helper and scheduled job are planted deliberately so the demonstration can show which controls reach them. The server behavior and the incoming updates are observed during the run.

Each live line is labeled with its source:
- `LangGraph's log:` lines are read from the server's log file as LangGraph writes it. The demo shows every warning or error, every line about a run, every line about shutting down, and the line that says the server has started ([log_filter.py](log_filter.py)). Start-up settings, statistics printed every few seconds, the start-up banner and the lines of a traceback stay in the full log only.
- `LangGraph's API:` lines are LangGraph's answer about the run, asked once a second and shown when it changes.
- `received from …` lines are messages arriving at the outside receiver.

When a step starts after a pause, the demo first reports how many messages the outside receiver got during the pause, and any of LangGraph's lines from that time.

To check that a run showed exactly the lines this rule picks, save its terminal output, for example with `sudo bash examples/langgraph/demo.sh | tee demo.log`, and compare it with the full log, whose path STEP 1 prints:

```bash
sudo python3 examples/langgraph/check_log_filter.py /var/lib/stopkit/demo-logs/langgraph-server-<time>.log demo.log
```

The check sorts every line of the full log, prints in full any warning or error the rule left out and any line it cannot place, and confirms that the printed lines match the full log one for one, in order and in time. It needs only Python.

## Recorded example

The saved run was completed on **1 November 2026**, in WSL 2 with Ubuntu 24.04.1, LangGraph 1.2.12 and langgraph-api 0.15.1. It used `--auto`. The complete transcript is [langgraph-example-20261101.log](../../validation/langgraph-example-20261101.log), and LangGraph's full log of the same run is [langgraph-example-20261101-server.log](../../validation/langgraph-example-20261101-server.log); `python3 examples/langgraph/check_log_filter.py validation/langgraph-example-20261101-server.log validation/langgraph-example-20261101.log` checks one against the other. The 27 October run is in [langgraph-example-20261027.log](../../validation/langgraph-example-20261027.log); that transcript has no lines from LangGraph's own log.

| Stage | Agent updates | Helper reports | Scheduled job runs | Recorded status or observation |
|---|---|---|---|---|
| Cancel the run | 8 | 4 | 0 | LangGraph's log: `Cancelled runs`, then `Background run interrupted`; its answer: `interrupted` |
| Ctrl+C twice | 26 | 13 | 0 | LangGraph's log: `Shutting down background workers` and three more shutdown lines; the server no longer answered; its process remained alive |
| Terminate the server | 0 | 7 | 1 | Server process ended |
| Restart the server | 0 | 5 | 0 | Canceled run stayed interrupted and did not resume |
| Full cleanup | 0 | 0 | 0 | Four items listed before, none after; no arrivals during the final 65 seconds |

Counts use the stage-specific windows implemented in [demo.py](demo.py); for example, the cancellation count starts one second after sending the request.

The task is a plain (synchronous) Python function, which LangGraph runs in a thread of its own. A cancel ends only the asynchronous task waiting for that thread, and Python cannot stop a running thread, so the run reads "interrupted" while the function keeps working. Khan (arXiv:2607.14166, Table 2) first reported this difference between a tool on a worker thread and a pure-async one for LangGraph's library; the demonstration shows it on the agent server, from outside. Ctrl+C makes the server stop taking requests, but Python does not let the process exit until that thread has finished. Killing the server's process ends the agent's own work, but not the detached helper it started or the job it scheduled, which only the full stop removes.

## Output

The terminal contains the live lines described above and the final count table. STEP 1 prints the path of LangGraph's full server log, `/var/lib/stopkit/demo-logs/langgraph-server-<time>.log`, which only root can read and which stays after the demo ends. The standard `record.json`, `report.md` and evidence bundle come from the [kit's own runs](../../README.md#quick-start).

The platform is the pinned development server, `langgraph dev`, with its local saved state. [graph.py](graph.py) also defines an async task variant; this demo launches the synchronous variant.

The receiver runs outside the agent's account. Stopkit supplies the account listings and cleanup through `src/stopkit/stops/`. The kit's own validation is in [VALIDATION.md](../../docs/VALIDATION.md).

## Files

| File | Purpose |
|---|---|
| [demo.sh](demo.sh) | Installs the pinned environment once and launches the demonstration |
| [demo.py](demo.py) | Runs the stages, receives activity and prints counts |
| [log_filter.py](log_filter.py) | Which of LangGraph's log lines the demo shows |
| [check_log_filter.py](check_log_filter.py) | Checks a run's printed lines against LangGraph's full log |
| [graph.py](graph.py) | Defines the scripted LangGraph workloads |
| [helper.py](helper.py) | The detached helper started by the workload |
| [langgraph.json](langgraph.json) | LangGraph application definition |
| [requirements.txt](requirements.txt), [constraints.txt](constraints.txt) | Dependency versions |
