# How an experiment works

The question is: **after a particular stop, which pieces of work actually ended, and did the platform's status agree with the observations?**

The experiments test the execution and shutdown mechanisms around an agent: run cancellation, workers, processes, schedules and delegated work. The workloads are scripted, so the experiments measure what a stop reaches, not whether a language model chooses to obey a shutdown instruction. The stop test kit at the root of this repository is the reusable form of this checking; its [README](../../README.md) describes it and [docs/VALIDATION.md](../../docs/VALIDATION.md) its validation.

## Design

For each trial, fix the platform version and configuration, a known workload, one intervention, its intended target, the observation period and the result rule. Start the work, confirm that it exists, apply the intervention and observe its effects. Controls check the instrumentation and, where conditions are compared, account for differences such as workload age and timing.

The n8n, LangGraph and Temporal trials map what different controls reach. Most conditions have one trial, so each row of their tables describes one observed case, not a rate. A helper deliberately placed outside a run-cancel control's scope shows that control's boundary; its survival alone is not evidence that the control broke its promise.

## Checking that computation actually ended

Silence is an observation, not proof of termination. A process needs evidence about that process's exit and its relevant children; a task inside a shared worker needs execution-specific completion evidence or the closure of its known computation holder. A schedule needs a separate armed-state check, and an outside job needs observations from the service that receives it. Natural completion, timeout, cancellation and later cleanup are distinct endings.

The [n8n loop evidence](n8n-loop-evidence/REPORT.md) presents the four Code/AI loop carriers beside the scorer's activity-based label (measure M2 of [`RULES-expB.md`](../harness/n8n-langgraph-7a3fcec/n8n/RULES-expB.md)). It reads the saved raw files with the frozen scorer's own validity and computation-boundary functions. Of 76 loop readings in the 19 trials, 16 have computation-end evidence at the snapshot; 60 are unconfirmed, including 17 with no recent activity, and those 17 are not counted as ended because they are quiet. The view does not read the platform's own logs: B1's run is among the 17, although its worker's log records `Task cancelled` and then an error for it ([worker.log lines 38–39](../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B1-20261013-225502-559385487/logs/worker.log#L38)). The [table of what each stop reached](WHAT-EACH-STOP-REACHES.md) counts running work as stopped when it is silent at the end of the watch, the scorer's rule, so it lists B1's run as stopped. Each row is one reading of one trial.

Regenerate the rows and the page from the repository root:

```bash
python3 -B experiments/scripts/loop_evidence.py --out experiments/docs/n8n-loop-evidence --overwrite
```

It covers the four n8n loop carriers. Each JSONL row keeps the activity count, the time window, the platform status and the computation-boundary evidence separately.

## Who writes each record

| Artifact | Written by | Role |
|---|---|---|
| Platform logs such as `main.log` and `worker.log` | The running platform; its output is redirected to files | What the platform itself reported |
| Receiver logs such as `sink.jsonl`, `pings.jsonl` or `receiver.jsonl` | The receiver process, while requests arrive during execution | Raw observations from outside the platform's own status machinery |
| `record.json` and command transcripts | The harness or the reproduction script | Stop actions, identifiers, times and observations copied from APIs and system reads; some fields are already computed |
| `scored_*.jsonl` and `summary_*.md` | Analysis code, after the raw observations are available | Derived classifications and summaries, not platform events |

For n8n, [the scorer](../harness/n8n-langgraph-7a3fcec/n8n/analyze_b.py) reads the raw files, and [the batch runner](../harness/n8n-langgraph-7a3fcec/n8n/run_expB.py) writes its results into `derived/`. In the standalone reproduction of the stop after a main restart, [sink.py](../n8n-restart/sink.py) appends each HTTP POST it receives with its arrival time and imports no scorer. The reproduction script applies its own explicit pass rule, so it still has analysis code of its own while depending on none of the harness.

"Outside the platform" means outside the platform's own status machinery. The receiver, the harness and the scorer are this repository's code, and [experiments/RUNBOOK.md](../RUNBOOK.md) lists the checks that test them.

## One result, recounted from the raw files

The [29 October n8n reproduction](../n8n-restart/results/2026-10-29-fresh-vm/) is the shortest route from a claim to its evidence. The workload is a Code-node loop that sends its execution ID and an increasing step number to a local receiver, awaiting each request and then waiting one second. The [setup source](../n8n-restart/setup.sh) creates this workload; the [reproduction script](../n8n-restart/p1_stop_after_restart.sh) defines the comparison and the result rule.

| Part | What was done or recorded |
|---|---|
| Comparison A | Start an execution and cancel it through the main process that started it |
| Sequence B | Start another execution, restart main while it runs, then call the same execution-stop API |
| Primary observation | Does the receiver keep recording ticks from that same execution after the platform reports it canceled? |
| Additional observations | Public API status, the execution's database row, queue membership and worker logs |

The saved [command transcript](../n8n-restart/results/2026-10-29-fresh-vm/out/p1-20261029T182735Z.txt) records execution 1's canceled reply at **18:27:52.969 UTC** (line 8) and execution 2's canceled reply at **18:29:54.882 UTC** (line 17). A separate recount of [the receiver file](../n8n-restart/results/2026-10-29-fresh-vm/logs/sink.jsonl), which imports no scorer, gives:

| Execution | Receiver ticks after the printed reply time | Relevant raw evidence |
|---|---|---|
| 1, comparison A | 0 | Its 16 ticks arrive before the reply |
| 2, sequence B | 237 | Receiver lines 79–315, steps 63–299; first at 18:29:55.623 UTC, last at 18:33:52.748 UTC |

Transcript line 20 also records execution 2 in the queue's active list at the nominal +60-second read, while its API and database status are `canceled`. The [worker log](../n8n-restart/results/2026-10-29-fresh-vm/logs/worker.log) records the later 300-second task timeout at line 175. Together these support the statement:

> In this tested sequence, n8n reported the execution as canceled while the same execution continued producing observable activity.

The count uses receiver arrival times and the printed reply time, not the time each request was sent; the sustained sequence, the workload code, the queue state and the worker log together are the evidence of ongoing work.

A and B are stopped at different task ages, and A always runs first; the 13 October trials record the same kind of age difference between B1 and B1r. The explanation of the difference comes from n8n's code ([the reproduction's guide](../n8n-restart/README.md) gives it), and the saved logs agree with it: main acts on a stop only for runs it holds in its own memory, and the restarted main no longer holds execution 2, so the stop message reaches the worker and does nothing while main writes `canceled`. After stop A, main logged `Sent abort signal to worker` ([main.log line 962](../n8n-restart/results/2026-10-29-fresh-vm/logs/main.log#L962)) and the worker logged `Task cancelled` ([worker.log line 140](../n8n-restart/results/2026-10-29-fresh-vm/logs/worker.log#L140)). After stop B, the worker received the stop message ([worker.log line 160](../n8n-restart/results/2026-10-29-fresh-vm/logs/worker.log#L160)), and neither line appears.

<details>
<summary>Recount the two executions directly from the receiver file</summary>

From the repository root, run this with Python 3. It reads the transcript and the JSONL file, imports no scorer and writes nothing:

```python
import json
import re
from datetime import datetime, timezone
from pathlib import Path

base = Path("experiments/n8n-restart/results/2026-10-29-fresh-vm")
transcript = (base / "out/p1-20261029T182735Z.txt").read_text(encoding="utf-8")
rows = [json.loads(line) for line in (base / "logs/sink.jsonl").read_text().splitlines()]
replies = {}
for line in transcript.splitlines():
    match = re.match(r"(\d{2}:\d{2}:\d{2}\.\d+) stop ([AB]) reply:", line)
    if match:
        replies[match[2]] = datetime.fromisoformat("2026-10-29T" + match[1]).replace(tzinfo=timezone.utc).timestamp()
for phase, execution in (("A", "1"), ("B", "2")):
    ticks = [r for r in rows if str(r["body"].get("exec")) == execution and r["t"] > replies[phase]]
    print(f"Execution {execution}: {len(ticks)} ticks after the printed reply")
```

It prints 0 and 237. `python3 -B experiments/scripts/build_results.py --only n8n_restart_updates` runs the same recount as one of the repository's checks.

</details>

## What the checks compare

| Kind of check | What it compares |
|---|---|
| Known live and dead controls, instrument checks | The observer's readings against work known to be running or ended, in the tested conditions |
| Hash checks and rerunning the analysis | Recorded bytes against their recorded SHA-256, and a recomputed analysis against the saved one |
| Recounting a claim from raw records without the scorer | The claim against the raw records alone |
| A reproduction with separate code | The same platform behavior, observed with no harness code |
| Stopkit's known-answer self-tests | The kit's verdicts against a ground truth recorded beside the planted work |

[experiments/RUNBOOK.md](../RUNBOOK.md) lists each check `experiments/scripts/build_results.py` runs and what makes it fail.

These checks show that the shipped records are the recorded bytes and that the saved analyses follow from them. They
do not show that a design or an analysis rule is right, and passing them does not make every conclusion in the
documents correct. The n8n loop evidence is a reading of records that already existed, not a new experiment. The
receiver, the harnesses and the scorers are this repository's own code, so they can have faults that no check here
would find; the reproduction without harness code reduces that dependence for one result but does not remove it. The
recount of the stop after a main restart uses the receiver's arrival times and the printed time of n8n's reply, so it
does not measure how many milliseconds a stop takes. The kit's self-tests show its verdicts on the tested cases, not an
error rate.

## Five roles

| Role | What it does | Where to look |
|---|---|---|
| Harness | Sets up a trial, starts known work, applies a stop and records its timing | The experiment's guide and its code under `experiments/` or `experiments/harness/` |
| Platform | Runs the task and exposes the control under test | The pinned LangGraph, n8n, Temporal, OpenClaw or client installation |
| Workload | Performs harmless, observable actions; some experiments deliberately start helpers, schedules or outside work | The scripted graph, workflow or tool of that experiment |
| Outside observer | Receives activity and reads process, queue, schedule and service state, apart from the platform's reported run status | Receiver logs and system snapshots in the raw records |
| Analysis | Applies stated rules and windows to the observations, keeping the evidence for each result | Scorers, check scripts and derived reports |

The scripts automate the commands a human operator would otherwise run and record. They do not replace the platform with a mock. Model responses and external services are stand-ins where the experiment's guide says so.

## Follow one run

1. Prepare the specified environment and start a fresh trial.
2. Start the known workload and check that the items under test exist.
3. Record the selected stop, the target identifiers, the send time and the reply.
4. Observe during the declared watch window. Compare actual activity and state with the platform's report.
5. Where the protocol requires it, restart on the same saved state and observe again.
6. Keep the raw records, apply the analysis rules and clean up the test work.

The details differ by experiment. Temporal uses one long activity, rather than the full set of helpers and schedules the LangGraph and n8n workloads plant. The client probes observe protocol messages and tool children. The LangGraph demonstration applies successive controls to one workload and is designed for explanation.

## Three ways to use the repository

- **Inspect or reanalyze saved evidence:** read recorded logs or rerun analysis on them. This catches calculation, transcription and consistency errors; it is not a new repetition of an experiment.
- **Run a fresh trial:** build the environment and run the platform again. Keep its versions, settings, raw records and analysis apart from the recorded runs.
- **Run the live demonstration:** launch the [interactive LangGraph example](../../examples/langgraph/README.md), pause before each control and show the incoming activity in the terminal. Its stages run in sequence on one workload; they are not independent benchmark cells.

The [reproduction of the stop after a main restart](../n8n-restart/README.md) uses no harness code.

## Reading a result

For each result, identify the workload, the stop's target, the tested version, the observation window, the control checks and the raw evidence. Separate work ended by the stop from natural completion, and distinguish running work from a schedule or credential that remains usable. A request being accepted, a missing database row and silence at a receiver are different observations.

Some records use frozen rules whose hashes were saved with the run. The LangGraph trials of 9 October also have a separately labeled post-hoc revision of their analysis (`experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1-rev6-posthoc/`). Use the version of the rules that belongs to the result being discussed.

The digests the records cite are those of the files as shipped. A few strings were replaced in the records with strings of the same length (a home-folder path, the cloud project's name and number, the six cloud machines' addresses and instance numbers, and the folder name of the repository the later runs were made from), and each digest that cited a changed file was rewritten to match; [experiments/data/MANIFEST.json](../data/MANIFEST.json) gives, for each changed file, its SHA-256 as shipped and the substitutions it received, and no digest of a replaced string, which would give a short value back to anyone who tried every candidate. `experiments/scripts/build_data.py --check` rebuilds every file from the source archive, which is not tracked.

The [walkthroughs](walkthroughs/README.md) are the shortest route from a result to its source lines.

## Extend one experiment

Choose one experiment and write down the new stop or workload, its intended target, the expected observation and the timing before running it. Keep a no-stop control and a control that can actually end the target work. Use a new output folder and record the code, the dependencies and the configuration. Compare raw observations before derived verdicts. Keep uncertainty and failed setup attempts, and do not edit recorded runs to match a new result.
