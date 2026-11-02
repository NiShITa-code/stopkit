# Experiment C rules: Temporal cancellation calibration. DRAFT 5, 14 Oct 2026 (pre-freeze batch); not frozen

**Status.** Development protocol, not a research freeze. Five feasibility attempts have run and been inspected: two FAST smokes and one full pilot of C0-HB on earlier development sources, then two full pilots on draft 4, the source later committed unchanged as `9db3f11`: C0-HB and the positive control PC, a target-worker kill (13 Oct, 07:14 and 07:22). No cancel or terminate condition has run. C.13 lists every attempt, what it showed, and the derived files later written into three of the raw folders. The raw records remain unchanged and do not validate this draft. Probe and cleanup actions in these attempts are separate treatments. C.8 expectations remain source-based provisional expectations; the predictions file must disclose the exposure listed in C.13. The validation pilots and procedure smokes that the freeze needs (C.10) will be the first cancel, terminate and server-restart outcomes seen on this source; each is prior exposure for its own condition, so the predictions must either be fixed before it runs or name it (C.8, C.10). These rules become binding only with a committed freeze, final predictions, passing current-source pilots and smokes, and the source manifest (C.12). A pre-freeze review of draft 5 (14 Oct) changed the C4 stage-1 rule, the verified stop, the record of each action, the one-trial-per-host rule and the freeze gate; the revision log lists each change.

**Relation to earlier rules.** "As R6.x" means the sixth revision of RULES-expA.md applies; "as B.x" means RULES-expB.md applies in its draft 5, as frozen for the n8n screen (commit `7a3fcec`), with Temporal's names in place of n8n's. R6.13, the clock guard, applies unchanged. T-numbers are the Temporal facts in DESIGN-expC section 9; K-numbers are the coding choices in DESIGN-expC section 3. Rules are numbered C.x so tests can cite them.

## C.0 Scope, versions, status of the results
- **System:**
  - Temporal CLI 1.9.1 (Server 1.32.0, UI 2.54.1), running `temporal server start-dev` with SQLite persistence (`--db-filename`) and the UI disabled (`--headless`);
  - the Python SDK temporalio 1.34.0 on Python 3.12.3, from `/root/temporal-venv`.
- **Recorded in every trial:**
  - the output of `temporal --version` and the sha256 of the CLI binary;
  - the temporalio version and the sha256 of its `dist-info/RECORD`, and the Python version;
  - the exact start command of every process;
  - each worker's effective options, which it prints at start (K11, K12);
  - the workload parameters (DESIGN-expC section 3), which are also the workflow's input;
  - dynamic configuration: none is set, recorded as "default".
  - the exact timing plan: draft number, fixed anchor offset, precheck lead, I/O reserve, final validation margin, realized deadlines, watch and probe lengths. A late check never substitutes a new planned stop.
  - the declared host profile (`host_profile`) and, on `linux-vm`, the worker's identity (`host_identity`), as below;
  - the one-trial-per-host check (`host_exclusion`, C.4 step 1), with the host's origin: Experiment B's n8n install present (a B campaign host, idle) or absent (a clean image);
  - the predictions file's SHA-256 when the trial starts, before anything runs (`predictions_at_start`, C.8, C.10).

  Any difference from the declared configuration is a blocking deviation. Missing evidence is unresolved configuration, never agreement.
- **Environment:**
  - a native install, not a container, on one of two declared host profiles (`STOPCHECK_HOST_PROFILE`, recorded in every record as `host_profile`): `wsl`, the default, a native WSL2 Ubuntu 24.04 host (the laptop); or `linux-vm`, a native install on a Linux VM (Ubuntu 24.04). The profile is declared, never inferred, and any other value refuses the trial before anything runs. Each profile is a separate, labelled configuration: its trials are never pooled with the other's, and monotonic stamps from different machines are never compared. The freeze records its profile, and its validation pilots, its screen trials and every trial a batch accepts must declare the same (C.10, C.12);
  - on `linux-vm`, before every trial the runner reads the worker's identity from the GCE metadata server, outside the trial's namespace: instance id and name, zone, machine type, CPU platform, image and scheduling. It records them in the trial's batch entry, and the harness copies them into the record. A missing or partial identity is recorded as such and never refuses a trial: it is provenance, not a configuration value the freeze compares. On `wsl` no identity is read;
  - every trial process in the trial's network namespace (C.1);
  - the clock guard (R6.13), checked at start, at end and across the trial, with the wall/monotonic check (C.9).
- **Status of the results:**
  - this is a calibration;
  - one trial per condition is an exploratory screen, and each outcome is reported on its own, never as a rate;
  - claims concern this dev-server configuration only;
  - a P cell that agrees with its basis is reported as "agrees with the documented semantics", never as a finding;
  - a disagreement goes through C.11 before it is reported.

## C.1 Processes, launch and identity
- **Namespace.** Every trial process runs in `/run/netns/expc-<trial>`, a fresh network namespace with only loopback up. `harness_c.py` creates it, re-runs itself inside it, and deletes it after the trial. Its inode is recorded.
  - **Isolation check**, before anything starts: the namespace handle's inode is the harness's own, its only interface is `lo`, and `lo` is up. On `linux-vm` it also needs an outbound probe to fail: a TCP connection from inside the namespace to the host's metadata address (169.254.169.254:80) must fail with "network unreachable" (no route), because a VM's own network has routes out; a connection, a refusal or a timeout fails the check. On `wsl` the check is unchanged. The trial processes share the host's mount namespace by design (they need the root-only SDK environment, DESIGN-expC section 2), so there is no hidden-path check as in B.1.
- **Commands:**
  - **server:** `/opt/temporal-cli-1.9.1/temporal server start-dev --db-filename <trial>/temporal.db --ip 127.0.0.1 --port 7833 --http-port 7834 --metrics-port 7835 --headless --log-format json`, with stdout and stderr to `server-g<n>.log`;
  - **worker:** `<venv>/bin/python -B temporal/jobs_c.py worker --address 127.0.0.1:7833 --task-queue <queue> --identity <role>-<trial>-g<n> --log <trial>/worker-<role>-g<n>.jsonl`. The generation n starts at 1 and goes up by one at each restart;
  - **client:** `<venv>/bin/python -B temporal/jobs_c.py start --address 127.0.0.1:7833 --spec <json>`, which prints, for each workflow it starts, the workflow ID, the run ID (`result_run_id`, T27) and its send and reply stamps. `jobs_c.py ready` starts one readiness or capability job and waits for its result;
  - **receiver:** `<venv>/bin/python -B temporal/receiver_c.py --port 8875 --out <trial>/receiver.jsonl`;
  - **observer:** the CLI, with `--address 127.0.0.1:7833 --namespace default --output json --disable-config-file --disable-config-env` on every call and `--command-timeout` at most 5 s, reduced to the remaining phase budget where a deadline applies; describe also takes `--raw` (T20).
- **Environment.** Every trial process and CLI call starts with the harness's environment minus every `TEMPORAL_*` variable. With the flags above, no shell or config-file setting (address, namespace, TLS, API key, profile) can redirect or alter a read.
- **Launch record.** Each process starts in its own session. The record keeps PID, start ticks (field 22 of `/proc/<pid>/stat`), command line, namespace inode, cgroup, process group, session and executable. An initial empty command line during exec is retained as a sample, never frozen as the signal identity. Within 2 s the same owned PID/start ticks must show the exact requested argv; otherwise launch fails. Identity is never adopted afresh when signalling.
- **Identity before a signal.** Before any SIGTERM or SIGKILL, the harness confirms that the PID's command line and start time match the launch record. A mismatch withholds the signal, and the trial fails. After the signal, the harness records the exit status and its stamp.
- **Readiness.** A fixed sleep never stands in for readiness.
  - Server: `temporal operator cluster health` exits 0, and `temporal workflow list` answers for the default namespace.
  - Worker: its readiness job (DESIGN-expC section 3) completes, and the job's `ready` effect reaches the receiver.
  - Receiver: GET `/health` answers 200.

  The JSON field names the CLI returns are mapped in one table in the scorer (`analyze_c.py`, FIELD MAP). CLI 1.9.1's `--output json` writes enum values in the prefixed upper-case form (`EVENT_TYPE_WORKFLOW_EXECUTION_STARTED`, `WORKFLOW_EXECUTION_STATUS_RUNNING`, `PENDING_ACTIVITY_STATE_STARTED`) and int64 fields such as `eventId` as strings, as every feasibility record shows (T25); the map also accepts the PascalCase form of the UI's history JSON. It is checked on pilot output (C.10).
- **Restart.** A restarted process must have a new PID and start time and pass readiness again. The launch record compares the new process's PID and start ticks with the replaced one's (`replaces`); the same identity fails the launch. A restarted worker has a new identity generation.

## C.2 Jobs, effects and planting evidence
- **The declared set** is the target workflow run, identified by the workflow ID and run ID in the client's start reply, together with every execution of its activities (any attempt, any `exec_nonce`) and their effects. The twin, the probe twin, the readiness and capability jobs and the ticks are controls outside the declared set. Every "ended", "closed" and "landed" judgement in these rules is about the declared set unless the rule says otherwise.
- **Effects.** An effect is a POST to `/effect` carrying the record in DESIGN-expC section 3. Its commit record is the receiver's arrival stamps; the line is flushed before the 200 reply. The receiver never deduplicates.
- **Executions.** The effects that share an `exec_nonce` form one execution. The first execution of the work activity is the first `exec_nonce` that posts.
- **Kinds:** `accepted`, `progress` (steps 1 to N), `final`, `ready` and `tick`.
- **Planting evidence**, observed before the actual intervention:

| Evidence | Applies to | Source |
|---|---|---|
| Job accepted | Every condition | The client's start reply with a run ID; describe reads RUNNING; the history has WorkflowExecutionStarted |
| Stage A | Every condition | The receiver has the target's `accepted` effect |
| Stage T | C1 | The history has TimerStarted and no TimerFired; there is no target `progress` effect; describe reads RUNNING |
| Stage W | Every condition except C1 | The receiver has steps 1-4 of the first execution (FAST: steps 1-3), the latest within 1.5 s, and no `final` effect; describe reads RUNNING. HB and NH: exactly one pending activity, in state STARTED with attempt 1 (T13). LA: no MarkerRecorded for the work activity in the history |
| Process identity | PC, C6, C7, C7l, C8 | The identity check of C.1 on the process to be signalled |
| Twin | Every condition | describe reads RUNNING and the twin's `accepted` effect is present. In C6: the twin is in stage T (TimerStarted and no TimerFired) |
| C6 stage (checked precondition) | C6 | Between the cancel's return and the server SIGTERM, describe reads CANCELED. The harness polls describe and show (in parallel) every 0.2 s in that second, until describe reads CANCELED and the history read in the same poll ends with a closing event; that closed history read is also the "before" side of C6's last-event-ID comparison (C.6) |
| Probe stage (checked precondition) | Every condition | C.4 step 6 |

  Missing evidence makes the target **untested**, never "stopped", and the trial needs a rerun. The intervention still runs at planned t_stop so that the record shows what happened (as B.2), unless the check overran (C.3). A failed C6 precondition still lets the SIGTERM run; its target is untested.

## C.3 Conditions and timing
**Anchors.** t_a is the receiver's arrival stamp (CLOCK_MONOTONIC) of the named effect. "Step 4" means the step-4 `progress` effect of the target's first execution. From draft 5, full-mode planned t_stop is fixed at t_a + 5.5 s, except C1, which remains t_a + 15 s. Step n is emitted about (n − 1) s after the activity starts, so the stop falls 8.5 s into the activity: half a step from every step emission, 0.5 s after the nominal heartbeat RPC at 8 s and 3.5 s before the next at 12 s (DESIGN-expC section 4). Draft 4's t_a + 5 s put the stop on the step-9 emission and on that RPC, so by-commit and by-initiation counts were decided by milliseconds (in the PC pilot step 9 was sent 1.8 ms before t_stop, C.13). FAST retains step 3 + 0.75 s (C1: accepted + 3 s), which is already half a step from its emissions. A late check never moves the planned stop.

| Id | Variant | Anchor | Procedure from t_stop | In-watch restart |
|---|---|---|---|---|
| C0-HB, C0-NH, C0-LA | HB, NH, LA | Step 4 | None; t_stop is only the observation anchor | None |
| PC | NH | Step 4 | SIGKILL the target worker | None |
| C1 | HB | `accepted` | `temporal workflow cancel -w <id> -r <run>` | None |
| C2 | HB | Step 4 | Cancel, as C1 | None |
| C2w | HB; the work activity is scheduled with cancellation type WAIT_CANCELLATION_COMPLETED | Step 4 | Cancel | None |
| C3 | NH | Step 4 | Cancel | None |
| C3w | NH; the work activity is scheduled with cancellation type WAIT_CANCELLATION_COMPLETED | Step 4 | Cancel | None |
| C4 | LA | Step 4 | Cancel | None |
| C5 | HB | Step 4 | `temporal workflow terminate -w <id> -r <run> --reason expc-<trial>` | None |
| C5n | NH | Step 4 | Terminate, as C5 | None |
| C6 | HB | Step 4 | Cancel. Poll describe and show until describe reads CANCELED (the C6 precondition, C.2). At t_return + 1.0 s, SIGTERM the server and wait for its exit (up to 15 s; after that SIGKILL, recorded as a deviation). Then start it at once with the same command and database file | Server |
| C7 | HB | Step 4 | SIGKILL the target worker. At t_stop + 15 s, start worker generation 2 | Target worker |
| C7l | LA | Step 4 | As C7 | Target worker |
| C8 | HB | Step 4 | SIGKILL the target worker. At t_stop + 3 s, once its exit is confirmed, cancel. At t_stop + 20 s, start worker generation 2 | Target worker |

In C6 the twin starts 25 s after the target, so that it is in its timer during the restart. In every other condition the twin starts with the target.

**Timing fields**, all on CLOCK_MONOTONIC:
- planned t_stop, kept even when the actual start differs;
- **t_stop:** the actual start of the procedure's first action, that is, the CLI call or the signal;
- for each action: `t_send` (just before the call or signal), `t_return` (when the CLI exits or the signal call returns); for a CLI call, the exact command typed (`argv_full`: the whole argv, with the address, namespace, command timeout, run ID and reason) and Temporal's reply: the exit code, the raw stdout, the full stderr and the parsed JSON. The clean-up terminate (C.4 step 7) keeps the same. For signals, also the stamp of the target's observed exit;
- for each restart: its start stamp and its readiness stamp;
- for each declared outage (C6's server restart, the durability probe): the stamp of the SIGTERM and the stamp of the server's readiness.

**Request accepted** means the CLI exited with status 0 within its 5 s command timeout. A non-zero exit, a timeout or a harness exception is a **failed request**. A failed request is never "accepted" and never "stopped"; the trial fails (C.9).

**Pre-intervention check.** Full mode starts 5 s before the fixed planned stop: 0.5 s after the step-4 anchor, or accepted + 10 s in C1. FAST retains its 0.75 s lead. The receiver health check, made as soon as the anchor is detected, and then the parallel target describe/show and twin describe calls (C6 adds the twin's show) share an I/O deadline at planned t_stop − 0.5 s in full mode, giving at most 4.5 s from the intended check start; time spent detecting the anchor and checking health consumes that same budget. The remaining 0.5 s reserves process termination/reaping, evidence validation and the final 0.1 s safety margin. FAST retains its I/O deadline at planned t_stop − 0.1 s and has no additional reserve.

Every call records its own start and end; these parallel reads are not an atomic snapshot. The check records its actual start/end, I/O and validation deadlines and the fixed planned stop. It reads the receiver and process identities after the calls. Validation must still end strictly before planned t_stop − 0.1 s. A late call, reaping or validation can therefore still withhold the intervention; nothing is backfilled from after the stop. Failed evidence on time makes the target untested (C.2). Five seconds is a declared feasibility budget for this host, not a bound guaranteed on other infrastructure.

**The intervention runs in its own thread** (as B.3). Its exceptions, a missing t_stop stamp, or failure to finish within the watch are trial failures.

## C.4 Timeline and reads
1. **Clean slate:**
   - a new trial folder and database path, and a fresh namespace;
   - no listener on ports 7833-7835 or 8875;
   - no process left from an earlier Experiment C trial, judged from command lines: a receiver, worker or client of `jobs_c.py`, a CLI call naming an `expc-` ID, or any `temporal server start-dev` (its command line names neither);
   - **one StopCheck trial on the host** (C.10). Before it creates the trial's namespace, the namespace wrapper takes an exclusive, non-blocking flock on Experiment C's host lock (`/run/lock/stopcheck-trial.lock`) and on the n8n launcher's lock (`/run/stopcheck-n8n.lock`, `n8n/harness_b_ns.sh` at `7a3fcec`), and it holds both until the namespace is deleted after clean-up (step 7). So two C runners can never both pass the clean slate, and a B trial cannot start beside a C trial; a lock already held refuses the trial. Then, inside the namespace and before anything starts, the trial confirms through `/proc/locks` that its wrapper holds both locks, and it refuses if any of these is present: a StopCheck process of Experiment A, B or C other than its own ancestors (judged from command lines: the harnesses, runners, campaign and launch scripts of each experiment, the n8n process, B's postgres and redis by their data directory and port, C's workers, receiver and dev server; a terminal multiplexer's own argv is skipped, the processes it started are not); another StopCheck network namespace (`expc-`, `expb-`, `stopcheck-`); an active StopCheck systemd unit (`n8n-*`, `stopcheck-*`; with no systemd as PID 1 none can run, and an unreadable unit list refuses); or a listener on C's or B's fixed ports (5678, 5433, 6380) in the host's network namespace. Experiment A's launcher takes no lock, so its trials are found by their processes only. The check is recorded as `host_exclusion`, with the host's origin (C.0). C is run on a VM whose B campaign has finished, with n8n's units inactive, or on a clean image;
   - guard snapshots, and the clock check (R6.13).
2. **Start and readiness** (C.1): the receiver, the server, the twin worker and the target worker, then readiness jobs on both queues.
3. **Submission:** the client starts the target and the twin (in C6 the twin follows 25 s later). The tick canary starts.
4. **Pre-intervention check, then the intervention** at planned t_stop (C.3).
5. **Watch: 120 s from the actual t_stop.**
   - **Claim poll:** describe for the target every 0.5 s. It starts at the request's `t_return`, or at t_stop where there is no request (C0, PC, C7, C7l). It stops at the first closed status or at t_stop + 60 s.
   - **Scheduled reads** at +0 (right after the first action returns), +2, +5, +10, +20, +30, +60, +90 and +120 s. Each read has four parts: describe for the target and the twin, show for the target, and `list --query 'WorkflowId="<id>"'` for the target. Each CLI call records its own start and reply stamps. A read that starts late keeps its real stamps. Each scheduled read, and the pre-intervention check after its gate, also records the observer's own load: the host's load averages and its cumulative CPU and steal ticks (`host_load`), a diagnostic (C.6 M3).
   - **Recorded from every describe** (claim poll and scheduled reads): the status; each pending activity's state, attempt, last heartbeat time, last started time, last failure and last worker identity (T13); the pending workflow task's state, attempt and times (T22); and the `cancel_requested` flag (T23).
   - **In-watch restarts** (C.3). After each, capability jobs run on the restarted worker's queue, or on both queues after a server restart.
   - **At +112 s:** capability jobs on both queues. PC skips the target queue, whose worker is dead by design. They start together and run beside the +120 s read, never delaying it. Each has the bound of every readiness job: its client waits up to 30 s for the run to complete, the client process gets 20 s more, and its `ready` effect must reach the receiver within 5 s after the client exits. A job may end after the watch; its start and end stamps are recorded, and the durability probe starts only once both have ended (at most 60 s after they started). Every attempt so far, under draft 4 and the development sources before it, gave both one shared deadline at t_stop + watch − 0.1 s (t_stop + 119.9 s, about 7.9 s after they started; 4.9 s in FAST), and they timed out in 2 of the 4 attempts that reached them, 3 of their 7 jobs (C.13).
6. **Durability probe** (every condition):
   1. start the probe twin on the twin queue;
   2. **pre-probe check** (a checked precondition): once the probe twin's `accepted` effect has arrived, read describe for the target, the twin and the probe twin, and show for the probe twin. Record every pending activity in state STARTED and every pending workflow task in state STARTED. The probe twin must be in its timer: TimerStarted, no TimerFired, no pending activity. If it is not, the probe's persistence control is untested and every probe restart outcome is UNKNOWN. A run is recorded as "the probe interrupted running work", and its probe outcome is unpredicted (DESIGN-expC section 8), if it is still open and lists a pending activity in state STARTED, or if any of its declared effects arrived in the 5 s before step 3. A STARTED activity that a closed run still lists is recorded apart, not as running work: whether a closed run lists its abandoned activity is unpredicted (C.8, "Pending activities on a closed run");
   3. SIGTERM each running worker (graceful, K12), then the server, recording each exit;
   4. start the server on the same database and wait for readiness, then start both workers (new generations) and run capability jobs on both queues;
   5. observe for 60 s from the end of step 4: describe, show and list for the target and describe for the probe twin every 10 s, and the receiver throughout.

   The probe is a treatment. Every result after it is labelled "after the durability probe".
7. **Clean-up**, only after the last observation:
   - terminate any open workflow of this trial, recorded as clean-up, not as an intervention;
   - SIGTERM, then SIGKILL, every trial process; once this begins no process may be started (an intervention thread can still be running);
   - confirm that none is left and the ports are free;
   - after the trial process exits, its wrapper lists the processes still in the trial's namespace (`ip netns pids`). Any it finds get SIGTERM, then SIGKILL, and the trial is void (exit 2, written into `record.json`); a namespace whose processes cannot be listed counts the same. Only then is the namespace deleted, because deleting it does not stop the processes in it, and only after that does the wrapper release the host locks (step 1);
   - on an interrupt, the wrapper and the runner wait for the trial's own clean-up (up to 120 s and 180 s) instead of killing it.

   The database, the logs and the raw CLI output are kept: every CLI call's record holds its full argv, raw stdout and full stderr beside the parsed JSON (C.3).

**Reads are read-only.** Destructive actions happen only in step 7.

**SERVER_UNAVAILABLE_DUE_TO_INTERVENTION** is a declared observation, not a fault. It is recorded when CLI reads fail because a declared outage (C6, the durability probe) has stopped the server. The receiver keeps recording during the outage. Outside a declared outage, a failed read is a read failure (C.9).

## C.5 The five events, kept apart (as B.5)
1. **Request accepted:** C.3.
2. **State changed,** against the pre-intervention baseline (RUNNING, no WorkflowExecutionCancelRequested):
   - the first read whose history shows WorkflowExecutionCancelRequested, for a cancel;
   - the first read whose describe reports `cancel_requested`, for a cancel;
   - the first read with a closed status.

   History event times are recorded as metadata (T17).
3. **Work ceased** (as B.5): positive only when both of these hold; otherwise it is unresolved.
   - **Bounded quiet**, draft 4's activity-based measure, kept and reported as it was: the declared effects have been quiet for at least 5 s before the end of the watch, the tick canary shows no receiver gap in that interval, and either the end-of-watch capability job on the target queue completed, or the target worker was killed by design and its exit is confirmed.
   - **Computation boundary closed** (MEASURES-COMMON section 5): every execution of the target's work activity that posted by the end of the watch, before t_stop included, has independent ending evidence. Either its `final` effect landed (the designed final iteration: the final POST is the activity's last statement), or every effect it posted came from a worker process the procedure killed with its exit observed before the end of the watch. A cancellation status, our worker log's acknowledgement (our code's view, DESIGN-expC section 2) and silence leave it unresolved. So do a pending attempt in state STARTED or CANCEL_REQUESTED at the end-of-watch read that no closed execution accounts for, a missing end-of-watch describe, and an incomplete receiver record.
     - **History cross-check** (pre-freeze review). Receiver silence never closes the boundary by itself; the end-of-watch read's history must agree. Every ActivityTaskStarted of the work activity must carry the attempt of a closed execution (T9). With no work execution at the receiver, the history must show positively that the work never ran: a closed history in which no ActivityTaskScheduled names the work activity, or in which each one that does has a closing activity event; for a local activity, which leaves no scheduled event (T8), a timer that started and never fired (the workflow closed before it reached the work). An ActivityTaskScheduled that names no activity, or a missing end-of-watch history, leaves the boundary unresolved.
4. **Effects closed:** the protected effects (the declared effects at the receiver after the request, M1) and the accepted durable obligation (the target run, which Temporal keeps and can retry) are judged apart. Effects are never "closed" while the run can still run or retry work: the PC pilot's quiet watch scored "closed" under draft 4 while its run stayed RUNNING with attempt 2 SCHEDULED, and the probe then re-ran that attempt (C.13).
   - **The durable obligation** is closed when the end-of-watch describe reports a closed status other than CONTINUED_AS_NEW: a closed execution takes no further events (T2, T9), so nothing of it can run or be retried. It is open when that describe reads RUNNING (the run can still schedule and retry work, T10, T11) or CONTINUED_AS_NEW (a new run carries the job). If that describe failed, an earlier closed read stands, because closure is permanent; an earlier RUNNING read does not. Otherwise it is unknown.
   - **The verdict:** "not closed" when an execution committed effects after the request but not its designed last effect (`final`; for the announce activity, its one `accepted` effect) by the end of the watch. Otherwise "obligation open" while the obligation is open. Otherwise, with a closed obligation, "closed by completion, not by the stop" when effects did commit after the request (each execution that committed them then reached its last effect), or "closed" when none did. A receiver gap over (request, end of watch] makes it unknown whatever else holds; an unknown obligation makes it unknown unless it is "not closed".
5. **Evidence sufficient.** Otherwise the result is **unresolved**. Unresolved means any of:
   - a needed read failed outside a declared outage;
   - the receiver had a gap;
   - a state was still changing at the last read;
   - Temporal answered NotFound for a run that the persisted server should hold, which is recorded as state lost.

There is no single "stop succeeded" flag: the operator's verdict, the verified stop (C.6), is reported beside the five events, never in place of them. HTTP and RPC failures and missing evidence are UNKNOWN, never "stopped".

## C.6 Measures
**Watch and probe.** M1-M5, pending activities on a closed run, history coverage, the false all-clear, the five events, the verified stop and the condition rows (PC, C4, C6, C8) use the declared effects, and the worker log's acknowledgements, up to the end of the watch. Effects after it belong to the durability probe, which is a treatment (C.4 step 6), and are scored only in the probe's restart outcome.

**Absences need complete evidence.** A result that says something did not happen ("no `final`", no duplicates, no execution omitted from the history, "stopped", "not seen", no pending activity on a closed run, no false all-clear, the twin's "once each") needs a gap-free receiver over the interval it covers, from the target's first work effect (or t_stop) to the end of the watch unless the row says otherwise, and usable reads. Otherwise it is UNKNOWN. Positive evidence (an effect, an event, a status that was read) stands whatever else is missing.

- **M1, effects after the request.** Counts of declared effects, by kind and by execution, in three versions:
  - **by commit:** arrival after the request's `t_return`, up to the end of the watch;
  - **by initiation:** sender stamp `smono` after `t_return`;
  - **ambiguous:** arrival in (`t_send`, `t_return`], listed separately.

  Without a request (C0, PC, C7, C7l), the same counts are taken from t_stop. C8 reports them from the kill and from the cancel. M1 also lists the step numbers of the first execution's post-request `progress` effects.
- **M2, claim and terminal status.**
  - The **claim** is the first read, from the claim poll or a scheduled read, whose describe reports a closed status (CANCELED, TERMINATED, COMPLETED, FAILED, TIMED_OUT or CONTINUED_AS_NEW; T14). The **claim time** is that call's reply stamp. The workload never continues as new; for the durable obligation a continued-as-new run is open (C.5 event 4).
  - Also recorded: the closing event's type and `event_time`; the first scheduled read whose history shows WorkflowExecutionCancelRequested, the read before it, and the event's `event_time`; the first read whose describe reports `cancel_requested`; the list view's status at each read.
  - Event times are platform metadata, never stop times (as B.6 treats `stoppedAt`).
- **M3, delivery and quiet.**
  - **Time to quiet**, per execution: the last effect's arrival minus `t_return`.
  - Whether the `final` effect landed.
  - **Delivery diagnostic** for HB under cancel or terminate (C2, C2w, C5): the first execution's actual steps, final effect, heartbeat metadata and cancellation acknowledgement.
  - **Heartbeat phase precondition** (HB; pre-freeze review). The delivery bound rests on the heartbeat phase, which was measured once, in a WSL feasibility pilot (C.13). Per trial: the pre-intervention check's describe gives the work activity's last heartbeat time, placed on the monotonic clock through that call's paired stamps (metadata, T17); the activity starts at its step-1 effect's sender stamp. The phase passes when that RPC lies within 0.25 s of the grid of one RPC per 0.8 × the heartbeat timeout from the activity's start (T4), and the grid puts the RPC before the stop within 0.25 s of the planned one (8 s into the activity, 0.5 s before the stop). The next RPC follows from the grid, and the bound is the first step emitted more than 0.5 s after it (a step emitted within 0.5 s of the RPC may race it). Otherwise the phase is off-plan, or UNKNOWN when it cannot be evaluated.
  - **Delivery bound** (C2, C2w, C5; restored in draft 5, `delivery_bound_step = 14`): the first execution posts no step from the bound on and no `final` effect. The stop is 3.5 s before the nominal heartbeat RPC 12 s into the activity, which carries the cancellation; step 13 is emitted at that moment, so the first step that cannot be posted is 14 (DESIGN-expC section 4). The bound is judged only when the phase precondition passed, with the bound derived from the trial's own phase (on plan it is 14); otherwise the row reads "phase off-plan" (M) or UNKNOWN, never within or exceeded, because a shifted phase would move the bound for measurement reasons. A late step or a final effect is positive evidence and stands; "within" is an absence and needs a gap-free receiver from the request to the end of the watch. FAST has no bound (plumbing only). Draft 4 had disabled the earlier step-10 bound, which belonged to the stop at step 4 + 1.5 s.
  - **Delivery time** and **stops after delivery** (C2, C2w, C5; pre-freeze review, C.7 K14), reported apart. Temporal decides when the cancellation reaches the activity (the heartbeat RPC); our code decides whether the activity then stops (K14). Delivery time: the first execution's first cancellation acknowledgement in the worker log, against the request's return and the next nominal RPC (when the phase passed). Stops after delivery: whether the first execution initiated an effect after that acknowledgement, by sender stamp: "continued after delivery" (positive), "stopped after delivery" (an absence: a gap-free receiver from the acknowledgement to the end of the watch), or "no acknowledgement". An effect in flight at the acknowledgement (sent before it, arriving after it, K8) is not continuing.
  - **Step interrupted in flight** (C|K8; pre-freeze review): for each execution with an acknowledgement, the activity's phase when the cancellation hit it (posting n, heartbeat n, sleeping after n, posting final; recorded in the acknowledgement line) and, when a POST was in flight, whether that step committed at the receiver: committed, not committed (an absence: a gap-free receiver from its sender stamp) or not in flight.
  - **Step lateness and load** (diagnostic, M; pre-freeze review): each execution's step lateness, its sender stamp minus (step 1's + (n − 1) × step_s), with the steps more than 0.25 s late flagged as a measurement-load diagnostic; the heartbeat RPC spacing from the distinct `last_heartbeat_time` values; and the observer's load samples (C.4 step 5).
  - The heartbeat phase: each read's `last_heartbeat_time` for the work activity, placed on the monotonic clock (metadata).
  - The worker log's cancellation acknowledgements (`smono`, the cancellation-detail flags, T5, and the phase) are reported as C-labelled diagnostics.
- **M4, duplicates and attempts.**
  - For each step, the number of distinct executions that posted it, and the number of `final` effects.
  - Temporal's attempt view is compared with the receiver's executions: the pending activity's attempt during the watch (T13) and ActivityTaskStarted's attempt in the history (T9). The result is "duplicates visible in Temporal": yes, no or unknown.
- **M5, status against reality**, at each read (as R6.7 and B.6).
  - **Reality** is bounded by the read's completion stamp t_r:
    - **active:** a declared effect arrived in (t_r − 2 s, t_r];
    - **quiet:** none arrived in (t_r − 5 s, t_r], and the receiver had no gap;
    - otherwise **unknown**.

    Later events never settle an earlier read.
  - **Temporal's claim at the read:** describe's status and the pending activities' states.
  - A closed status with active reality is false at that read.
  - A pending activity in state STARTED while its worker is confirmed dead is reported as a **stale activity state**. It is not a false all-clear: it claims dispatch, not liveness.
  - Disagreement between the list view and describe is reported separately.
- **Pending activities on a closed run.** At every read whose describe reports a closed status, the pending activities it still lists, with their states. Reported per trial as its own row ("a closed run lists a pending activity": yes, no or unknown).
- **History coverage.** At the last read of the watch: for each execution that posted effects, whether the history records it (ActivityTaskStarted with that execution's attempt for an ordinary activity, MarkerRecorded for a local one; T8, T9). The row "history omits an execution that produced effects" is reported per trial.
- **False all-clear** (as R6.8).
  - **Operator rules:**
    - **O1:** describe reports a closed status;
    - **O2:** O1 holds, the list view reports the same closed status, describe lists no pending activity, and the history's last event is a closing event.
  - **Wrong:** a declared effect commits in (claim time, end of watch]. The by-commit version is the main one; the by-initiation version is also printed.
  - Scored at every read that makes the claim, and at the first one.
  - **Headline:** O2 × by commit at the first read that makes the O2 claim. O1 × by commit at its first claiming read is always printed beside it. "No O2 claim" is its own outcome; it is never merged with "no false all-clear".
  - **UNKNOWN:** a failed read; a receiver gap after the claim; or, for the by-initiation version, a failed clock-order check (C.9). A read whose describe failed before the first claiming read makes a "no false all-clear" headline UNKNOWN, because the first claim is then not known; a false all-clear stands, because effects after a later claim also follow any earlier one.
  - No claim inside the watch is reported as "no claim".
- **Verified stop** (as B.6; new in draft 5, changed by the pre-freeze review): the operator's verdict over the declared set at the end of the watch, built from C.5's computation boundary (with its history cross-check) and durable obligation.
  - **unresolved** for a void trial, always;
  - **not stopped:** the run is open at the end-of-watch read, or a declared effect committed in the last 5 s of the watch. This positive evidence is decided first;
  - **stopped:** otherwise, when the durable obligation is closed, the computation boundary is closed, the trial is valid by C.9 (an absence of work needs complete evidence: a failed capability job, twin or receiver control leaves it unresolved), and no execution ran to its `final` effect: each was ended by the stop or by a confirmed kill, or none ran;
  - **ended by completion, not by the stop:** as stopped, except that an execution ran to its `final` effect. Our 20-step job finishing inside the 120 s watch is not the request stopping it; as B.5, natural completion establishes neither containment nor a successful cancellation;
  - **unresolved:** otherwise, listing what lacks evidence.

  M1-M5, the false all-clear, the five events and the restart outcomes are unchanged by it. Bounded quiet, a closed status and "no false all-clear" are not proof that the job stopped.
- **C4 stages.**
  - **Stage 1, when the cancel reaches workflow code** (rewritten by the pre-freeze review). Decided from evidence that workflow code acted on the cancel, never from the order of WorkflowExecutionCancelRequested and MarkerRecorded in the history. That order shows when the server wrote the request: a cancel that arrives while a workflow task is open is buffered and written at the next task completion, and a local activity that outlasts the workflow task timeout forces a completion at each workflow-task heartbeat (T8, T22, T24; nominally 16 s into the activity, before the marker at about 20 s), so the request can precede the marker under either reading.
    - **(a) before the local activity finished**, on any of: a cancellation acknowledgement of the local work execution in the worker log in the watch (the local activity was cancelled before it returned); the claim (the first closed describe, M2) reading CANCELED before the `final` effect arrived, or with no `final` effect in a gap-free watch (under K2 and K3, TRY_CANCEL, the run closes as soon as workflow code sees the cancel); a MarkerRecorded for the work activity whose failure is, or is caused by, a cancellation.
    - **(b) after the local activity finished**, when all hold: the `final` effect landed; the worker log covers that execution (its `activity_start` line) and has no acknowledgement for it; no marker records a cancellation; and a describe that started after the `final` effect arrived still read RUNNING (the run was open after the local activity returned, so, under K2 and K3, workflow code had not acted on the cancel).
    - **UNKNOWN** otherwise; "cancel never reached the history" when the last usable history has no CancelRequested and nothing shows (a).
    - Reported apart, descriptively: "server recorded the cancel request before the marker" (yes, no, no marker, or the request not in the history), from the event IDs; and, as before, the time the event first appears (M2), the pending workflow task at each read and the `cancel_requested` flag.
  - **Stage 2, whether the local activity stops without heartbeats** (only under reading (a)): whether `final` landed; the local activity's effects committed after its cancellation acknowledgement in the worker log; the time from that acknowledgement to its last effect.
- **C6 outage.** The declared effects that arrive between the server SIGTERM and the server's readiness; whether `final` landed; the precondition result; after the restart, the status, the history's last event ID against that of the last closed history read before the SIGTERM, and the pending activities' attempts and last failures. A history read taken before the run closed is not compared; without a closed read before the SIGTERM, the comparison is UNKNOWN.
- **Restart outcome**, for each in-watch restart and for the durability probe:
  - **the window after the restart** starts at the restart's start, except after a server restart (C6's in-watch restart and the probe's), where it starts at the server's readiness (pre-freeze review): an abandoned activity can keep posting while the server is down, because nothing restarted it, so the declared effects between the SIGTERM and readiness are reported as "continued through the outage" and count toward neither resumed nor re-run (C6's outage row also lists them). An execution that posts after readiness and posted at or before it is resumed;
  - **stayed ended:** the status was closed before and after the restart; the history's last event ID is unchanged, comparing the latest closed history read before the restart (in C6, the precondition polls count) with the latest history read after readiness; no declared effect arrives in the window after the restart; and the capability jobs and the persistence control passed. A missing history read on either side makes the outcome unresolved. During the durability probe, a worker-side failure of our own code (C.9) also fails its controls;
  - **resumed:** an execution that posted before the restart posts after it (the same `exec_nonce`);
  - **re-run:** a new execution posts after the restart;
  - **closed after restart:** the target was RUNNING before the restart and reaches a closed status after it; the status is named (for example COMPLETED in C7, CANCELED in C8). It is reported together with resumed or re-run;
  - **unresolved:** a capability job or the persistence control failed, a read failed outside a declared outage, the state was still changing at the last read, or the state was lost.
- **Twin:** its status, its effects and its executions, reported as a control (C.9).

## C.7 Cell labels, written before any data (as B.7)
Every condition × measure cell gets one label:
- **P:** decided by Temporal's code or documented semantics, including the SDK code inside the worker;
- **C:** depends on our workload code (K1-K14);
- **O:** operating-system semantics (signals, process exit);
- **S:** decided by our stand-in (the receiver);
- **M:** set by a measurement rule (read schedule, claim-poll interval, quiet thresholds, watch length, anchor rule).

**Compound labels.** Many outcomes are decided by Temporal's code given one of our coding choices. Such a cell is labelled `P|K<n>` (several choices: `P|K2,K3`), which means "Temporal's code decides it, given Kn; a different Kn could change it". A trailing `+M` adds a measurement rule that also sets it (for example `P|K4+M`: the throttle is Temporal's, the heartbeat cadence is K4, and the phase at the stop is set by the anchor rule). The grammar is `X`, `X|K<n>[,K<m>...]` or either with `+M`, where X is one of P, C, O, S or M.

**Which choices must be named.** By the rule above:
- K2 decides the closing status of every cancel condition: the SDK writes the cancel command only when a cancelled exception escapes the workflow (T7). Only terminate closes the run whatever the workflow code does (T2).
- K3 decides whether a cancel closes the run before the activity acknowledges (T6).
- K4 sets the heartbeat phase and the length of a re-run.
- K6 sets the heartbeat throttle interval (0.8 × the 5 s heartbeat timeout, T4) and how soon a dead worker's attempt times out (T10).
- K9 makes the local activity long enough for workflow-task heartbeating (T8).
- K1, K8 and K14 decide what a delivered cancellation does to the activity (pre-freeze review): K1 makes it an asyncio task cancel, not a thread exception (T5); K8 makes the effect POST interruptible, so at most one step is cut off in flight; K14 makes our activity re-raise the cancellation with no cleanup and no final effect. Temporal decides when the cancellation is delivered (the heartbeat RPC, T3-T5); whether the activity then stops producing effects is our code's. So every effects row that says the activity stops after delivery names K1, K8 and K14.

The variant (HB, NH, LA) is part of the condition's definition, so a cell that Temporal's code decides for that variant is P without a K. A cell decided by our code alone is C.

**What each label allows.**
- In Experiment C even P cells are calibration results, not findings about Temporal (C.0).
- O and S cells are instrument checks.
- C and M cells, and compound labels, are stated together with the choice that sets them.

## C.8 Predictions
- **Sources:** DESIGN-expC section 9 only. A prediction that depends on the compiled core or on the server's code, neither of which was read, says so. The heartbeat phase the timed rows use (one RPC per 0.8 × 5 s = 4 s, T4) was also seen in a feasibility run: in the C0-HB pilot, describe's `last_heartbeat_time` placed the RPCs about 0.02 s after each 4 s mark from the activity's start (C.13). Rows that rest on it say "seen in feasibility" (as B.8).
- **Every prediction names the observation that would refute it.**
- **Predictions never feed the scorer.** They are compared with the scored results only after scoring.
- **Prior exposure from the validation runs** (pre-freeze review). The freeze's validation pilots and procedure smokes (C.10) run on the freeze's own source, so C2's, C3's and C6's pilots and the smokes of C1, C2w, C5, C7 and C8 will be the first outcomes of those conditions seen on it: each is prior exposure for its own condition. C.13 cannot list them, because the rules' SHA-256 is bound by the very pilots. So either the predictions are fixed before each such run (every trial records the predictions file's SHA-256 before anything runs, `predictions_at_start`, and the freeze checks that it equals the frozen file's), or the predictions name the run in `exposed_validation` by its trial id and record SHA-256. The freeze refuses a run that is neither and records, for each, which held. A screen result of a condition whose validation run was disclosed is reported as a repeat of an inspected run.
- **The predictions file**, written before the freeze, is `predictions/temporal-expC-cells.json`, relative to the repository root. Its version 1 JSON has:
  - `prior_exposure`;
  - `exposed_validation` (optional): a list of `{trial, record_sha256}` receipts of validation runs whose outcome was seen before the predictions were fixed (above);
  - the expected `configuration`;
  - `conditions`, keyed by condition id. Each condition has `measures`, which must include `request_accepted`, `state`, `effects_after_request`, `false_all_clear`, `duplicates`, `history_coverage`, `pending_on_closed`, `cancel_first_seen`, `restart_outcome` and `twin`, and may add condition rows (such as `c4_stage1`). Each measure has `prediction`, `label` (C.7 grammar), `basis` and `refutation`.

  `unpredicted` is allowed, and stays descriptive.

**Common rows** (they apply unless a condition's row overrides them):

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| Request accepted | Every cancel and terminate exits 0, including C8's cancel while no worker runs | P | T1, T2: the request records a history event; the workflow's code acts on it later | A non-zero exit or an RPC error while the server is ready |
| Cancel recorded | WorkflowExecutionCancelRequested appears in the history at the first scheduled read after a cancel returns, and describe reports `cancel_requested` from that read on. Not C4, which has its own rows | P (*inference* for the flag) | T1, T23 | Absent at the first scheduled read after the cancel returns (the +2 s read; in C8, whose cancel is sent at +3 s, the +5 s read) while the server is up |
| Twin | COMPLETED; `accepted`, steps 1-20 and `final` once each; one execution, attempt 1 | P | T14; in C6 it is the persistence control (T12) | Any other outcome |
| Pre-probe check | No open trial run has a pending activity in state STARTED, and no declared effect arrives in the 5 s before the probe stops anything; the probe twin is in its timer. A closed run that still lists a STARTED activity is not running work (C.4 step 6.2); whether one does is unpredicted in C2, C3, C5, C5n and C6 (row below) | M (the watch is long enough for every target except PC's to close; PC's attempt 2 waits SCHEDULED on the dead worker's queue) | The condition rows below; T10, T11 | A STARTED activity on an open run, a declared effect in those 5 s, or the probe twin outside its timer |
| Durability probe, target closed before it | Stayed ended | P | T12; T2 and T9: a closed execution takes no further events | A declared effect after the probe's restarts, a status change, or NotFound |
| Probe twin | Completes after the probe's restarts | P | T12 | Not completed within the probe's observation |
| Receiver | Logs every tick, with no gap over 2 s, during declared server outages too | S | Our stand-in, outside every Temporal process | A gap |
| History coverage | Where the workflow closes while an ordinary activity's execution is still running (C2, C3, C5, C5n, C6), the closed history has ActivityTaskScheduled but no ActivityTaskStarted for that execution | P (*inference*) | T9 | An ActivityTaskStarted event for that execution |
| Pending activities on a closed run | Unpredicted in C2, C3, C5, C5n and C6: whether the server clears a closed run's abandoned activity was not read | — | DESIGN-expC section 9, "Not read" | — |
| False all-clear under O2 | Unpredicted in C2, C3, C5, C5n and C6, because O2 needs "no pending activity" (row above). The O1 cells are predicted in the rows below | — | As above | — |
| Status against reality | Compare each usable read with actual observed activity or quiet; the timed condition rows below give the expected effects around each read | M; causal labels from the condition rows | C.6 and the observed evidence | An unsupported verdict or use of later evidence for an earlier read |
| Verified stop (C.6) | Ended by completion, not by the stop, in C0-HB, C0-NH, C0-LA, C3, C3w and C5n (each execution ends with its `final` effect and the run closes) and in C7 and C7l (the killed execution's worker is dead; the second execution ends with its `final` effect). Stopped in C1 only (no work execution runs, and the closed history shows the work activity never scheduled). Not stopped in PC (the run stays RUNNING). Unresolved in C2, C2w and C5: the first execution stops without its `final` effect while its worker stays alive, so its end has no independent evidence. Unpredicted in C4 (it follows stage 1), C6 and C8 (they follow unpredicted rows). Any of these needs a valid trial; otherwise unresolved | M (the rule decides which evidence counts); the outcomes it combines carry the condition rows' labels | C.5, C.6 and the condition rows | Another verdict where one is predicted |

**C0-HB, C0-NH, C0-LA**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| Effects | `accepted`, steps 1-20 and `final` once each; one execution | C | DESIGN-expC section 3 | A missing or repeated effect |
| M1 from t_stop, the observation anchor | Steps 10-20 and `final` (12 effects) commit after t_stop, by commit and by initiation; step 9 is emitted 0.5 s before it, so no effect is in flight at it | C+M | DESIGN-expC section 3; the anchor rule (C.3) | Another split, or an effect sent within 0.25 s of t_stop |
| Status | COMPLETED, first read only after the `final` effect; no false all-clear | P | T14 | Another status, or a closed status before `final` |
| History | HB and NH: ActivityTaskStarted (attempt 1) and ActivityTaskCompleted. LA: one MarkerRecorded for the work activity, and WorkflowTaskCompleted events during it from workflow-task heartbeating | P; LA: P\|K9 | T8, T9 | A missing event; no workflow-task heartbeat events during the 20 s LA |

**PC**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| Effects | No declared effect initiated after the kill within the watch, and none in flight at it: step 9 is emitted 0.5 s before the kill and step 10 would be 0.5 s after (C.3), so M1 counts none by commit, none by initiation and none during the call. The validity gate (C.9) is the first part: no effect initiated after the kill, and none arriving after t_stop + 0.1 s unless its `smono` is before the kill (in flight). Attempt 2's re-run in the durability probe is the probe row's prediction, not a gate failure | O | SIGKILL ends the process | Any effect initiated after the kill in the watch, or one in flight at it |
| Status | RUNNING throughout the watch, so no claim. The pending activity reads STARTED, attempt 1, until about 60 s after attempt 1 started, about 51.5 s after t_stop (stale activity state), then SCHEDULED, attempt 2, with a start-to-close timeout failure (seen in feasibility, C.13) | P\|K5,K6 | T10, T11, T13 | A closed status; no SCHEDULED attempt 2 by t_stop + 70 s |
| Durability probe | Re-run: attempt 2 runs all steps and `final` on the restarted worker, then COMPLETED within the probe's observation (allowing the sticky-queue wait, T21) | P\|K5 | T10, T11, T21 | No attempt-2 effect in the probe |

**C1**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| State | TimerCanceled, then WorkflowExecutionCanceled; claim CANCELED by the +2 s read | P\|K2 | T1, T7 | Another status; TimerFired |
| Effects | None after the request; the work activity is never scheduled | P\|K2 | T7 | Any `progress` or `final` effect, or ActivityTaskScheduled for the work activity |
| False all-clear | False at every read, under O1 and O2 | P\|K2 | Follows from the rows above | A declared effect after the claim |

**C2**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| State | ActivityTaskCancelRequested, then WorkflowExecutionCanceled, in one workflow task; claim CANCELED by the +2 s read, before the activity's next heartbeat (nominally 3.5 s after t_stop; the heartbeat phase was seen in feasibility) | P\|K2,K3 | T6 (TRY_CANCEL reports at once), T7 (the cancelled exception escapes under K2) | RUNNING at the +2 s read with the worker up; ActivityTaskCanceled before WorkflowExecutionCanceled |
| Effects | Steps 10-12 of the first execution land after the request, and perhaps step 13; no step from 14 on and no `final` effect (the delivery bound, C.6 M3, judged only when the heartbeat phase precondition passes). The activity learns of the cancellation at its next heartbeat RPC, nominally 12 s into the activity, 3.5 s after t_stop; step 13 is emitted at that moment (the heartbeat phase was seen in feasibility), and our code then stops (K14) | P\|K1,K4,K6,K8,K14+M | T3, T4 (one RPC per 0.8 × 5 s), T5; the core's first send and flush times were not read | A step from 14 on or a `final` effect from the first execution; no step 12 while the server is up (delivery before the next heartbeat) |
| Delivery time | The first execution's cancellation acknowledgement comes at the next nominal heartbeat RPC, 12 s into the activity, about 3.5 s after t_stop | P\|K4,K6+M | T3, T4, T5 | An acknowledgement more than 0.5 s from that RPC while the phase precondition passed, or none |
| Stops after delivery | No effect of the first execution is initiated after its acknowledgement | C\|K1,K8,K14 | K14: our activity re-raises the cancellation with no cleanup | An effect whose sender stamp follows the acknowledgement |
| Worker log | One cancellation acknowledgement in the first execution, with the `not_found` flag | C (*inference*, as above) | T5 (NOT_FOUND: the server no longer knows the activity) | No acknowledgement, or another flag |
| False all-clear, O1 × by commit | True at the first claiming read: the claim comes by the +2 s read (State), and at least step 12, emitted 2.5 s after t_stop, commits after it | P\|K2,K3,K4,K6+M | T3-T7 and the rows above | No declared effect after the first claim |

**C2w**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| State | RUNNING, with the pending activity in state CANCEL_REQUESTED at the +2 s read, until the activity acknowledges the cancellation at its next heartbeat RPC (nominally 3.5 s after t_stop; seen in feasibility); then ActivityTaskCanceled and WorkflowExecutionCanceled. The claim follows the activity's last effect | P\|K2 (*inference*: the server sets CANCEL_REQUESTED when it records the cancel command; the core's handling of a cancelled completion was not read) | T5, T6, T7, T13 | A closed status before the activity's last effect; no ActivityTaskCanceled; RUNNING without CANCEL_REQUESTED at the +2 s read |
| Effects | As C2: steps 10-12, perhaps 13; no step from 14 on and no `final` effect | P\|K1,K4,K6,K8,K14+M | T3, T4, T5 | As C2 |
| Delivery time | As C2 | P\|K4,K6+M | As C2 | As C2 |
| Stops after delivery | As C2 | C\|K1,K8,K14 | As C2 | As C2 |
| Worker log | One acknowledgement, with the `cancel_requested` flag | C (*inference*) | T5 | No acknowledgement, or another flag |
| False all-clear | False at every read, under O1 and O2 | P\|K2 | The rows above | A declared effect after the claim |
| History coverage | ActivityTaskStarted (attempt 1) and ActivityTaskCanceled for the execution | P (*inference*) | T9 | Either missing |

**C3**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| State | Claim CANCELED by the +2 s read | P\|K2,K3 | T6, T7 | RUNNING at +2 s with the worker up |
| Effects | The remaining steps and the `final` effect land after the claim; `final` nominally about 11.5 s after t_stop, with actual stamps retained; one execution; no cancellation acknowledgement in the worker log | P | T3 | `progress` stops before step 20 without `final`, or an acknowledgement |
| False all-clear, O1 × by commit | True at every claiming read before the `final` effect | P\|K2,K3 | T3, T6, T7 | No declared effect after the first claim |
| Temporal's record of the run | The history ends with WorkflowExecutionCanceled and has no ActivityTaskStarted or ActivityTaskCompleted for the execution, so nothing in Temporal records the `final` effect | P (*inference*) | T9; a closed execution takes no further events | Either event in the history |

**C3w**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| State before the `final` effect | RUNNING, with the pending activity in state CANCEL_REQUESTED | P (*inference*: the server sets that state when it records the cancel command) | T6, T13 | A closed status before `final` |
| Terminal status | COMPLETED after the `final` effect: the shielded await continues on the activity's result and the workflow returns it | P\|K2 (the SDK code was read; the core's handling was not) | T7, `worker/_workflow_instance.py:2345-2390` | CANCELED or FAILED |
| Effects | All remaining steps and `final` land | P | T3 | `progress` stops before step 20 |
| False all-clear | False at every read | P\|K2 | Follows from the rows above | A declared effect after the claim |

**C4.** Two stages (DESIGN-expC section 4). The common row "Cancel recorded" does not apply.

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| Stage 1: when the cancel reaches workflow code | Unpredicted; neither reading is primary. Reading (a): at the next workflow-task heartbeat, nominally 16 s into the local activity, about 7.5 s after t_stop (the first, at about 80 % of the 10 s workflow-task timeout, falls about 0.5 s before the stop). Reading (b): only after the local activity finishes, nominally about 11.5 s after t_stop. The two times differ by about 4 s. The evidence of C.6 decides the reading (an acknowledgement, the run's close before the `final` effect, or a cancelled marker; or the run open after the `final` effect); the order of the events in the history does not, because a buffered request is written at the next task completion under either reading | — (either reading would be P\|K2,K3,K9: the close-time evidence rests on K2 and TRY_CANCEL) | T8, T22, T24; the core's workflow-task heartbeat times and the server's buffering of a cancel request were not read | — |
| Server recorded the cancel request before the marker | Reported, not predicted: likely "yes" under either reading, since the workflow-task heartbeat completes a task before the marker (T8, T24) | M | T24 | — |
| Cancel first seen | Reported, not predicted: the first read whose history shows WorkflowExecutionCancelRequested and the read before it; the first read with `cancel_requested`; the pending workflow task at each read | M | T22, T23 | — |
| Stage 2, under reading (a) | The local activity is cancelled without heartbeats: no `final` effect, and no local-activity effect commits more than 1.5 s after its cancellation acknowledgement in the worker log. The run closes CANCELED | P\|K1,K2,K8,K9,K14 (*inference*: the core's local-activity cancel path was not read; once it is delivered, our code stops, K14) | T8 (D2), T6, T7 | A `final` effect, or effects continuing more than 1.5 s after the acknowledgement |
| Stage 2, under reading (b) | Not tested: all steps and `final` land first. The closing status (CANCELED or COMPLETED) is unpredicted | P\|K9 | T8 | — |
| False all-clear | Under (a): true if a local-activity effect commits after the claim. Under (b): false at every read | P\|K2,K9; the answer under (a) is M | Follows from the rows above | — |
| Duplicates | None | P | T8 | Two executions |

**C5**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| State | TERMINATED at the +0 read; the closing event is WorkflowExecutionTerminated | P | T2 | Another status at +0 |
| Effects | As C2: steps 10-12, perhaps 13; no step from 14 on and no `final` effect (the heartbeat phase was seen in feasibility) | P\|K1,K4,K6,K8,K14+M | T2, T4, T5 | As C2 |
| Delivery time | As C2 | P\|K4,K6+M | As C2 | As C2 |
| Stops after delivery | As C2 | C\|K1,K8,K14 | As C2 | As C2 |
| Worker log | One acknowledgement, with the `not_found` flag | C (*inference*) | T5 | No acknowledgement, or another flag |
| False all-clear, O1 × by commit | True at the first claiming read (the +0 read or an earlier claim poll): at least steps 11 and 12, emitted 1.5 s and 2.5 s after t_stop, commit after it | P\|K4,K6+M | T2, T4 and the rows above | No declared effect after the first claim |

**C5n**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| State | TERMINATED at the +0 read | P | T2 | Another status |
| Effects | The remaining steps and `final` land after the claim; no acknowledgement | P (*inference*) | T2 (pending activities get no chance to stop cleanly), T3 | `progress` stops before step 20 without `final` |
| False all-clear, O1 × by commit | True at every claiming read before `final` | P | As above | No declared effect after the first claim |

**C6.** Descriptive: no row predicts what the abandoned activity does during the outage. The server's SIGTERM is due 1 s after the cancel returns, about 1.2 s after t_stop and 2.3 s before the activity's next nominal heartbeat RPC (12 s into the activity); in the two full pilots the dev server took 1.8 s and 2.0 s to exit after the probe's SIGTERM (C.13). Whether the server is already down when that RPC is due is therefore a race, left unpredicted. Report the observed order; absence of live work during the outage is not evidence of an outage-time cancellation mechanism.

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| Precondition | describe reads CANCELED before the SIGTERM | P\|K2,K3 | T6, T7 | — (a failure makes the target untested, C.2) |
| Effects during the outage | Recorded, not predicted: the abandoned HB execution's effects between the SIGTERM and the server's readiness, and the worker log's acknowledgement and flags. Whether a failed heartbeat RPC ends the activity is decided in the core, which was not read | M | DESIGN-expC section 9, "Not read" | — |
| Final effect | Unpredicted: set by our 1 s delay (M), the server's stop and restart time (O) and the core's heartbeat retry (not read) | M | As above | — |
| After the restart | The run reads CANCELED, and the history's last event ID equals the one read before the SIGTERM | P | T12; a closed execution takes no further events | Another status, a changed last event ID, or NotFound |
| Re-run branch, only if the precondition failed | Unpredicted: a second execution may start beside attempt 1 if the server treats attempt 1's heartbeat timeout as expired. Recorded after the restart: the pending activity's attempt and last failure | — | T10, K5; the server's handling of an overdue timeout at restart was not read | — |
| Twin (persistence control) | Completes after the restart | P | T12 | Not completed by the end of the watch |

**C7**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| State | RUNNING until COMPLETED after the second execution's `final`, with no earlier claim. The pending activity reads STARTED, attempt 1, at the +0 and +2 s reads (stale: its worker is dead), then SCHEDULED, attempt 2, with a heartbeat-timeout failure, by the +10 s read (STARTED only after generation 2 starts at +15 s). The last heartbeat RPC is about 0.5 s before the kill (seen in feasibility), so the 5 s heartbeat timeout falls about 4.5 s after t_stop, and the retry follows after its 1 s initial interval; the +5 s read is too close to call | P\|K4,K5,K6 | T10, T11, T13, T21 | A closed status before the second `final`; not STARTED attempt 1 at the +2 s read; no attempt 2 by the +10 s read |
| Effects | The first execution posts steps 1-9 and stops at the kill (step 9 is emitted 0.5 s before it). The second execution (attempt 2) posts steps 1-20 and `final` after the restart at +15 s, duplicating steps 1-9. There is one `final` | P\|K4,K5,K6 | T10, T11 | A first-execution step after 9; no second execution; a second `final` |
| History | One ActivityTaskStarted, with attempt 2 and the heartbeat-timeout failure. Only the attempt number shows that an earlier execution existed; nothing records its effects | P | T9 | An ActivityTaskStarted for attempt 1 |
| Restart outcome | Re-run, closed after restart (COMPLETED); no false all-clear | P\|K4,K5,K6 | As above | — |

**C7l**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| Effects | After the killed worker's workflow task times out (up to 10 s) and generation 2 starts, the LA runs again from step 1, because no marker was recorded. Steps are duplicated, and there is one `final` | P\|K9 | T8 (at least once) | No second execution |
| Status | COMPLETED after the second `final` | P\|K9 | T8, T14 | Another status |
| Attempt numbers and history trace of the first execution | Unpredicted: the core's local-activity bookkeeping is compiled and was not read | — | — | — |

**C8**

| Measure | Prediction | Label | Basis | Refuted by |
|---|---|---|---|---|
| Request accepted | Yes, with no worker running | P | T1 | A non-zero exit |
| Cancel recorded | The common row applies: no workflow task is open while no worker runs | P (*inference*) | T1, T23 | As the common row |
| State before the restart | RUNNING, with no worker to act on the cancel. The cancel at +3 s comes before attempt 1's heartbeat timeout: the last heartbeat RPC is about 0.5 s before the kill (seen in feasibility), so the 5 s timeout falls about 4.5 s after t_stop, 1.5 s after the cancel. So the pending activity reads STARTED, attempt 1, at the +2 s read, and SCHEDULED, attempt 2, with a heartbeat-timeout failure, by the +10 s read. No CANCEL_REQUESTED state appears before the restart, because no worker runs the workflow task that would request the activity's cancellation | P\|K2,K5,K6 | T1, T10, T11, T13 | CANCEL_REQUESTED or a closed status before the restart; attempt 2 or a heartbeat-timeout failure at the +2 s read; no attempt 2 by the +10 s read |
| Claim | CANCELED only after generation 2 starts, by 5 s after its readiness | P\|K2,K3 | T1, T7 | A closed status before the restart |
| Effects | No `final` effect from any execution. Generation 2 may start attempt 2 before it processes the cancel, so attempt-2 `progress` effects can land; their number is unpredicted (a race) | P\|K2,K4 | T4, T5, T10 | A `final` effect |
| False all-clear | True if an attempt-2 effect commits after the claim | M | As above | — |
| Restart outcome | Closed after restart (CANCELED); also re-run if attempt 2 posts | P\|K2 | As above | — |

## C.9 Validity
- **Controls.** A trial is valid only if its applicable controls pass.
  - **Twin** (C.8). If it fails, or a receiver gap leaves its "once each" unproven, the trial is invalid; in C6, the restart outcomes are unresolved instead.
  - **Receiver:**
    - `/health` answers 200 at start, before t_stop, at the end of the watch and at the end of the probe. Each of these four checks is recorded once, by label; the polls that wait for the receiver to come up are readiness (C.1), not checks;
    - the tick canary shows no gap over 2 s from the first tick to the end of the probe. A failed tick POST without such a gap (a POST has a 1 s timeout) is reported as a diagnostic, not a failure;
    - no malformed effect post: a POST the receiver logged as bad is an effect whose record cannot be attributed, so absences cannot be judged; it invalidates the trial.

    A gap makes declared effects in that interval UNKNOWN; a gap inside the watch voids the trial.
  - **Our code inside the system under test** (pre-freeze review). An `activity_error` line in a target or twin worker log (K8: a failed effect POST fails the activity, and Temporal retries it) or a worker that stopped without a signal is an instrument fault: inside the watch it invalidates the trial, so that a duplicate or re-run it causes is never scored as Temporal's behaviour; during the durability probe it fails the probe's controls (its restart outcome is unresolved).
  - **Readiness and capability jobs** complete: every readiness job recorded by the end of the watch (at start-up and at each in-watch restart), every in-watch restart's capability jobs, and one completed end-of-watch capability job on each queue it covers (both; PC the twin's only; C.4 step 5). Otherwise the trial is invalid. A failed end-of-watch capability job on the target queue also leaves bounded quiet, and so work ceased, unresolved (C.5). The durability probe's own readiness and capability jobs are its controls: they decide its restart outcome, not the trial's validity (as R6.9). The draft-4 scorer ignored these jobs in its validity verdict; from draft 5 it requires them.
  - **Persistence controls** (C6's twin, the probe twin). A failure makes the affected restart outcomes UNKNOWN; it does not void the watch measures (as R6.9).
  - **Checked preconditions** (C6's stage, the pre-probe check). A failed C6 precondition makes C6's target untested. A failed pre-probe check makes the probe's restart outcomes UNKNOWN; it does not void the watch measures.
  - **PC is the gate.** A full-length pilot PC must pass before any screen. A screen PC that fails voids the activity-stage cells of the grid until it passes on rerun.
- **Guards** (the trial is void if any fails):
  - the clock (R6.13): at start and at end, and across the trial from every line the receiver wrote, ticks included. Across the trial there are two checks over the same lines: the mono/raw pace check over the paired (`mono`, `raw`) stamps, and the prospective **wall/monotonic consistency check** over the paired (`mono`, `t`) stamps. Each interval between paired stamps must agree within 0.1 s plus 1,000 ppm of the interval, over at least 10 s of coverage; a stamp without a finite pair fails it. This is RULES-expB B.9's rule, copied unchanged into `temporal/clockwall_c.py` so C's freeze binds no n8n file. Matching mono/raw rates alone cannot exclude both clocks pausing while wall time advances, which would skew Temporal's own timers and event times against the harness's. The harness records the check in `clock_across_trial` (`wall_clock`); the scorer recomputes it from receiver.jsonl with the same rule and reports both verdicts. From record schema 2, a missing or failing receipt, or a failing recomputation, invalidates the trial. For earlier records the recomputation is a labelled post hoc diagnostic (all five feasibility records pass it, C.13);
  - `common/guards.py` snapshots at start and end: boot id, PID 1, OOM kills, network namespace;
  - the disk floor at the start of the trial, which refuses the trial below it: on `wsl`, `guards.py` `disk_ok` at its 10 GB default (free space on the Windows C: drive, which holds WSL's disk), unchanged; on `linux-vm`, 10 GB free on each of the filesystems holding `temporal/results` (the trial's database and logs) and `/` (`/root`, `/opt`, `/tmp`), where an unreadable one fails, and a `linux-vm` declaration on a kernel that reads as WSL, or cannot be read, fails too. Recorded as `host_checks_start`;
  - the declared host profile (C.0) and the isolation check (C.1). From record schema 2 the scorer requires a declared profile and a passing `host_checks_start`;
  - versions and configuration (C.0);
  - the clean slate (C.4 step 1), including one StopCheck trial on the host: from record schema 2 the scorer requires a passing `host_exclusion`;
  - readiness (C.1).
- **Untested:** missing planting evidence or a failed C6 precondition (C.2). The raw record is kept, and the trial is rerun.
- **Trial failure** (no scored result; rerun):
  - a failed request;
  - an intervention-thread error;
  - an identity mismatch before a signal;
  - an overrun pre-intervention check;
  - a harness exception.
- **Read failures** are UNKNOWN. NotFound is a separate observation, "record absent". After a restart on the same database it means state was lost: the restart outcome is unresolved and the persistence failure is reported.
- **Clock order** (as R6.1). Each effect's `smono` must be earlier than its arrival stamp. Arrival more than 1 s after `smono` is flagged. Any violation makes the by-initiation versions UNKNOWN for that trial; it does not invalidate the trial, and the scorer reports it beside the validity verdict (draft 5's scorer had also invalidated the trial, which this rule never said).
- **Invalid trials** are rerun once (as R6.10), under the runner's admission rule (C.10).

## C.10 Run kinds, counts and order
- **smoke:** requires `STOPCHECK_FAST=1`. For plumbing only; never scored; excluded from all results (as B.10). FAST values:
  - timer 6 s;
  - 8 steps at 0.5 s;
  - heartbeat timeout 2 s;
  - anchor at step 3, with planned t_stop = t_a + 0.75 s; C1: t_a + 3 s;
  - watch 30 s, claim poll until t_stop + 20 s, scheduled reads at +0, +2, +5, +10, +20 and +30 s, capability jobs at +25 s;
  - C6's twin 5 s after the target; C7's restart at +8 s; C8's cancel at +1.5 s and restart at +12 s;
  - probe observation 20 s with reads every 5 s, and an 8 s probe-twin timer.
- **pilot:** full length, unscored. The required set is C0-HB, C0-NH, C0-LA, PC, C2, C3 and C6. The pilots must show:
  - every anchor and pre-intervention check completed before planned t_stop;
  - the scorer's CLI field mapping works on real output;
  - one closed run's pending-activity list (from C2 or C3), so the row "pending activities on a closed run" has a captured example;
  - restarts complete with new identities;
  - the C6 precondition and the pre-probe check are evaluated;
  - the probe detects PC's re-run;
  - the clock guard passes, the wall/monotonic check included;
  - the readiness and capability jobs complete;
  - clean-up leaves no process or listener;
  - the measured trial duration.

  `run_expC.py --freeze-only` refuses a pilot record that does not show these where the record can: the pre-intervention check ended on time with its evidence, the pre-probe check was evaluated, the probe's restarts completed with new identities (every restarted process's `replaces` comparison shows a new PID and start time, and the server and both workers were restarted), C6's precondition was evaluated, the wall/monotonic receipt passed, PC's gate passed and its probe outcome includes the re-run, and the C2 or the C3 pilot's "a closed run lists a pending activity" row reads yes or no. Every pilot must also be valid by the scorer, in record schema 2, on the freeze's source and declared host profile (C.12).
- **Procedure smokes** (pre-freeze review). The pilot set never runs terminate, a cancel during the timer, WAIT_CANCELLATION_COMPLETED, a worker kill with a restart, or a kill then a cancel then a restart; their first live run would otherwise be inside the scored screen. So the freeze also needs one FAST smoke of each of C1, C2w, C5, C7 and C8 on the freeze's source, schema and declared host profile, whose procedure ran end to end: t_stop stamped, the intervention thread without an error, every request exited 0 within its timeout, every signal's exit observed, every in-watch restart ready, and the durability probe without an error. A smoke is plumbing and is never scored. Every pilot and smoke is prior exposure for its own condition (C.8).
- **screen:**
  - requires a frozen manifest, a committed clean tree and passing tests and mutants;
  - one trial per condition, in an order shuffled with a fixed seed recorded in the manifest: the freeze writes the seed and the resulting order, and the runner refuses a screen, before the batch and before each trial, whose seed or order differs from the frozen ones;
  - on one host, trials run one after another and never at the same time as any other StopCheck trial: they share the fixed ports (7833-7835 and 8875), the process table the clean slate checks, the clock and the memory. The host lock and the one-trial-per-host check of C.4 step 1 enforce this for C, and against B's launcher; Experiment A's launcher takes no lock, so its trials are caught only if they are already running. Independent VMs may each run one active trial at a time, on the same frozen source and declared host profile. Each architecture (Experiments A, B and C) has its own freeze, and a C freeze is never shared with another experiment's. A C screen split across VMs needs a campaign registered before any launch, with one assignment and attempt ledger and the rerun limit counted across the workers, as B.10's campaign has; C has no campaign tool yet, so until one is built a C screen runs as one batch on one host.
- **Admission of each attempt** (as B.10). Every attempt is kept in its batch's `batch.json`. An attempt is accepted only when its harness exited 0, it was not interrupted, and `analyze_c.py` calls its own raw record valid, in record schema 2 and with the batch's host profile; a PC attempt also needs its gate to pass. An exit status alone never accepts a trial. A batch stops at the first attempt it does not accept. `run_expC.py --resume <batch.json>`, given the batch's run-kind flag (`--pilot`, `--smoke`, or none for a screen), continues the recorded order with the same code and reruns that condition once, under its own trial id (`<trial>-a2`); a condition whose trial and rerun both fail is incomplete. The batch records its PC gate (`pc_gate`): until it reads "passed", the batch's activity-stage cells are void (C.9). Batches written before draft 5 have no such accounting and cannot be resumed.
- **Exit codes** (as `n8n/harness_b.py`): 0 completed; 3 ran with untested work (rerun needed); 2 error or void.
- **Repetition criteria**, fixed now. A condition is repeated if its trial:
  - contradicts a prediction;
  - shows a false all-clear where none was predicted;
  - shows a declared effect after the request where none was predicted;
  - shows duplicates where none were predicted;
  - gives a durability-probe outcome other than "stayed ended" for a target closed before the probe;
  - or is C4, whose stage 1 is unpredicted.

  Repeat counts require a further prospective addendum before running. Draft 5's offset puts the stop 0.5 s after a nominal heartbeat RPC and half a step from the step emissions, so C2, C2w and C5 have one race left, step 13's emission at the next RPC, which their rows name. Repeating one offset does not provide systematic phase coverage (C2φ, DESIGN-expC section 4). Exploratory and repeated results are reported separately.

## C.11 Reproduction outside the harness
Before any result is reported as agreeing or disagreeing with Temporal's documentation, a short script reproduces it without harness code (as B.11). The script may use:
- the CLI;
- a minimal Python SDK script for the workflow, the worker and the client;
- a ten-line HTTP sink in place of the receiver.

The script and its output are committed. A result that does not reproduce this way is reported as a harness observation only.

## C.12 Tests, reproduction and the freeze
- **Offline only.** No test starts a server, a worker or a listening socket.
- **`tests/test_analyze_c.py`:** scorer fixtures for:
  - missing reads, NotFound, failures and declared outages;
  - claim-poll timing and the O1/O2 headline;
  - in-flight and ambiguous effects;
  - duplicates found by `exec_nonce` when attempt numbers repeat;
  - history coverage, and pending activities on a closed run;
  - the C4 stages and the C6 outage rows;
  - each restart outcome;
  - receiver gaps;
  - the delivery bound, the computation boundary, the durable obligation and the verified stop, the readiness and capability jobs in validity, the wall/monotonic recomputation, the host profile in a current record, and reports written outside raw folders (draft 5);
  - pre-freeze review: C4 stage 1 from workflow-code evidence (with a fixture in which CancelRequested precedes the marker and `final` landed with no acknowledgement, which must not read as (a)); the verified stop's validity requirement, history cross-check and "ended by completion"; validity as C.9 states it (clock order and failed ticks as diagnostics, worker-side faults, the host exclusion); the server-restart window; the heartbeat phase precondition, delivery time and stops after delivery; the step in flight; step lateness and load.
- **`tests/test_clockwall_c.py`:** the wall/monotonic rule (n8n's fixtures, and the receiver's row format).
- **`tests/test_runner_c.py`:** the runner's admission of each attempt, reruns and `--resume`; the host profile and the worker identity; the freeze gate over the seven pilots; committed freeze inputs; the export of every attempt.
- **`temporal/mutate_c.py`**, the mutation check (as B.13): one or more mutants per correction of draft 5, each reverting it in the scorer, the harness, the runner or the wall/monotonic rule, and each killed only by an assertion of its focused test (an import or runtime error does not count).
- **`tests/test_harness_c.py`:** fixtures for:
  - timing plans, anchors and pre-intervention budgets, including withholding on overrun;
  - pre-check decisions, the C6 precondition and the pre-probe check;
  - identity checks before signals;
  - the order and timing of each procedure's actions (C.3), with the CLI and signals replaced by fakes;
  - record fields and exit codes;
  - the clean slate's command-line test, the trial environment, the health checks, and clean-up: no launch after it begins, leftovers in the namespace, and waiting on an interrupt;
  - run-kind checks (FAST only as smoke, a frozen manifest for a screen), and the runner's refusals and prediction schema;
  - the pure parts of `jobs_c.py` (workload parameters, input checks, the effect record) and of `receiver_c.py` (the logged line);
  - draft 5: the stop phase and the delivery bound, the end-of-watch capability jobs, the across-trial clock check, and the host profiles (disk floor, kernel, outbound isolation probe, worker identity, the screen's freeze check);
  - pre-freeze review: each action's typed command and reply, and the clean-up terminate's; a restart's new identity; the observer's load; the host locks, the wrapper holding them around the namespace, and the one-trial-per-host check; the predictions' SHA-256 at a trial's start; the workload's phase and in-flight tracking (`jobs_c.WorkProgress`).
- **`tests/test_runner_c.py`**, pre-freeze review: the freeze's predictions binding (fixed before each validation run, or disclosed), the procedure smokes, the pending-on-closed example, the restart identities, and the seed and order bound by the freeze and checked by the screen.
- **The freeze** binds, with sha256 values:
  - these rules and DESIGN-expC.md;
  - `predictions/temporal-expC-cells.json`;
  - the implementation and tests: every `temporal/*.py`, among them `clockwall_c.py` and `mutate_c.py`, and every `tests/test_*_c.py`;
  - the reused files, which stay unmodified: `common/clockguard.py`, `common/guards.py` and `langgraph/recorder.py`, and `common/test_clockguard.py`, which the offline checks run;
  - the declared host profile;
  - the screen's seed and its shuffled condition order (C.10).

  `run_expC.py --freeze-only --validation <pilot and smoke folders>` checks the operator-written predictions, runs the offline checks (every C test module, the clock guard's tests and the mutation check), and needs one full-length validation pilot of each condition of C.10's set: C0-HB, C0-NH, C0-LA, PC, C2, C3 and C6, and one FAST procedure smoke of each of C1, C2w, C5, C7 and C8. Each pilot must be valid by the scorer, in record schema 2, with the exact source fingerprints the freeze binds, declaring the host profile the freeze records, and showing what C.10 requires (PC: its gate passed and its probe shows the re-run); each smoke must show its procedure ran (C.10). For each pilot and smoke, the predictions must have been fixed before it ran or must disclose it (C.8). The manifest records each run's record SHA-256, folder, kind and predictions binding, and the screen's seed and order. Freeze creation never invents predictions, never replaces an existing freeze, and does not commit anything.

  `run_expC.py` refuses a screen on a dirty tree, a manifest mismatch, incomplete predictions, failing tests or mutants, a freeze or prediction file that is git-ignored, uncommitted or changed since its commit, and a host whose declared profile is not the freeze's; each screen trial's harness checks the freeze against its own declared profile again (as B.13). A later freeze can be prospective only for newly started screen attempts, with the inspected feasibility attempts disclosed (C.13). Old records retain their original source hashes and timing; they are not rescored or relabelled as current-source validation. The prospective wall/monotonic guard is now implemented (C.9); the read-only clock audits written into two raw folders on 13 Oct (`posthoc-clocks.json`, C.13) are post hoc evidence, not that gate.

  **Every attempt is scored and exported** (as B.13). After each attempt the runner scores its raw folder into `<batch folder>/derived/<trial>/` (`scored_c.jsonl` and `summary_c.md`) and writes beside them `manifest-sha256.json`: the SHA-256 and size of every raw file and of the derived reports, and the SHA-256 of the scorer's sources (`analyze_c.py`, `clockwall_c.py`); `batch.json` references the manifest and its hash. This covers accepted, invalid and interrupted attempts and attempts without a record; an attempt that ended before its export is exported when the batch resumes. Raw folders are only read. `analyze_c.py` itself writes its reports outside the trial folder, by default to `<results>/derived-posthoc/<trial folder>/` or to `--out`, refuses a destination inside a raw folder, and replaces earlier reports only with `--overwrite`.

## C.13 Prior exposure: the feasibility attempts (disclosure, 14 Oct 2026)
Every Experiment C trial run before draft 5 is listed here. All five ran on the laptop (`wsl`), in record schema 1, before the host profile, the wall/monotonic guard and the runner's admission existed. None is a counted result or validates a freeze, and none is rescored or relabelled as current-source validation. Their outputs were inspected while drafts 4 and 5 were written, so they are prior exposure for the predictions: the predictions file's `prior_exposure` must cite this section. No cancel or terminate condition has run. The only target-worker kill that has run is PC; draft 4's status, written at 07:11 on 13 Oct, before the last two pilots, said that none had.

| Attempt (folder under `temporal/results/`) | Started, 13 Oct | Kind and source | What it showed |
|---|---|---|---|
| `XC-smoke-c-ready-1013-C0-HB-20261013-053350` (batch `XC-smoke-c-ready-1013`) | 05:33 | FAST smoke, C0-HB; an uncommitted development source | The watch completed and both end-of-watch capability jobs completed. The durability probe failed: the server's SIGTERM was withheld on an identity mismatch, because the launch record had kept a transient empty command line (fixed before the next attempt, C.1 launch record). Exit 0, and the batch was marked completed |
| `XC-smoke-c-fix-1013-C0-HB-20261013-064026` (batch `XC-smoke-c-fix-1013`, which listed C0-HB, PC, C2 and C3) | 06:40 | FAST smoke, C0-HB; a later uncommitted source | The twin's `accepted` effect was missing at the pre-stop check, so the target was untested. Both end-of-watch capability jobs timed out at their shared deadline, 4.9 s after they started. Exit 3; the batch stopped, and PC, C2 and C3 never ran |
| `XC-pilot-c-pilot-1013-C0-HB-20261013-064853` (batch `XC-pilot-c-pilot-1013`, which listed C0-HB and PC) | 06:48 | Full pilot, C0-HB; the same source, with the stop at step 4 + 1.5 s | All three parallel pre-stop CLI reads timed out under the 1.4 s budget, so the check overran and the procedure was withheld. Exit 2; PC never ran |
| `XC-pilot-c-budget-1013-C0-HB-20261013-071440` (batch `XC-pilot-c-budget-1013`) | 07:14 | Full pilot, C0-HB; draft 4, the source later committed unchanged as `9db3f11` (the batch's source hashes equal those files), with the stop at step 4 + 5 s | The target ran to its `final` effect and closed COMPLETED; the probe read "stayed ended". The twin queue's end-of-watch capability job timed out at the shared deadline, 7.9 s after it started; its `ready` effect arrived 139 s after t_stop, after the probe had restarted the twin worker. Describe's `last_heartbeat_time` placed the heartbeat RPCs about 0.02 s after each 4 s mark from the activity's start (C.8 sources; draft 5's offset, C.3). The target's step 9 arrived 0.001 s after t_stop. In the probe the dev server took 2.0 s to exit after its SIGTERM. Exit 0 |
| `XC-pilot-c-pc-1013-PC-20261013-072243` (batch `XC-pilot-c-pc-1013`) | 07:22 | Full pilot, PC; the same draft-4 source, with the stop at step 4 + 5 s | The target worker was SIGKILLed at t_stop; step 9 had been sent 1.8 ms before t_stop. No declared effect was initiated after the kill in the watch (PC gate: pass), and the twin queue's capability job completed. The run stayed RUNNING through the watch: the pending activity read STARTED, attempt 1, through the +30 s read, and SCHEDULED, attempt 2, with a start-to-close timeout failure, from the +60 s read. In the probe, attempt 2 posted all 20 steps and its `final` effect on the restarted worker, and the run closed COMPLETED; the dev server took 1.8 s to exit after the probe's SIGTERM. The draft-4 scorer reported `effects_closed` "closed" for this open run (C.5 event 4 now reports "obligation open"). Exit 0 |

**Derived files inside raw folders.** After the trials, files that are not raw were written into three of these raw folders: the scorer's reports, `scored_c.jsonl` and `summary_c.md`, in `XC-smoke-c-ready-1013-C0-HB-20261013-053350`, `XC-pilot-c-budget-1013-C0-HB-20261013-071440` and `XC-pilot-c-pc-1013-PC-20261013-072243`; and `posthoc-clocks.json`, a read-only audit of each pilot's clocks (and of PC's calibration) that labels itself post hoc and "not a prospective C clock guard", in the last two. They are derived, not raw: they are not trial evidence, they were written after the trials with the draft-4 scorer and a separate audit script, and a reader or an export must not take them for raw files (an export manifest lists every file it finds in a folder). They are left in place, so the folders stay as found. Draft 5's scorer and runner never write into a raw folder (C.12). Recomputed by the draft-5 scorer as a post hoc diagnostic, the wall/monotonic check passes in all five records.

## Revision log
- **Draft 1, 12 Oct 2026:** written from DESIGN-expC draft 1 and the sources in its section 9. Not reviewed; nothing implemented or run.
- **Draft 2, 12 Oct 2026:** revised for the review of draft 1, with DESIGN-expC draft 2 (its section 11 gives the reasons point by point). Nothing run.
  - C.7: compound labels `P|Kn` and `+M`, and the list of choices that must be named (review point 3). Labels in C.8 changed to match.
  - C.8: C2 and C5 share one effects prediction (point 2); C2w added (point 2); the heartbeat-phase prediction "steps 6-8, and perhaps 9" (point 4); C4 split into two stages and exempted from "Cancel recorded" (points 5 and 6); O2 cells and pending activities on a closed run unpredicted in C2, C3, C5, C5n and C6 (point 7); C6 rewritten as descriptive, with its precondition and re-run branch, and its "durability of the cancel" row removed (point 8); the pre-probe check row (point 9).
  - C.2, C.4 and C.9: the C6 precondition and the pre-probe check as checked preconditions (points 8 and 9); describe's pending workflow task and `cancel_requested` recorded at every read (point 5).
  - C.6: the C4 stage measures, the C6 outage measures, the pending-on-closed row, "closed after restart" in place of "completed after restart" (C8 closes CANCELED after its restart), and an O1 column printed beside the O2 headline.
  - C.10: C2 and C5 leave the ten-trial set (point 4); C4 is repeated because stage 1 is unpredicted, not because the documents disagree (point 6); FAST values for every condition; the pilots must capture a closed run's pending activities.
  - C.1: the implementation's command lines (`jobs_c.py worker`, `start`, `ready`; describe with `--raw`) and the scorer's field map.
- **Draft 3, 13 Oct 2026:** changes found while implementing and testing draft 2 (DESIGN-expC section 11). Nothing run against a server.
  - C.7 and C.8: K6 is added to the choices that must be named, and to the labels of the cells it sets. The heartbeat throttle interval is 0.8 × K6's heartbeat timeout (C2, C2w and C5 effects and false all-clear), and K6's timeout fails a dead worker's attempt (C7; C8 before the restart).
  - C.4 step 6.2 and the common "Pre-probe check" row: running work is a STARTED activity on an open run, or a declared effect in the 5 s before the probe stops anything. A STARTED activity listed on a closed run is recorded apart, so the server behaviour left unread under review point 7 cannot by itself make a probe outcome unpredicted.
  - C.6: the watch measures use effects up to the end of the watch, and the probe's effects are scored only in its restart outcome. PC's gate row says "within the watch" for the same reason.
  - C.2, C.3 and C.6: C6's precondition polls read show as well as describe, and the last-event-ID comparison uses a closed history read before the SIGTERM; without one it is UNKNOWN.
  - C.1: the observer names the namespace. C.12: the harness test list matches `tests/test_harness_c.py`.
- **Draft 3, independent check, 13 Oct 2026:** problems found by checking the implementation against these rules, fixed in the code and the tests and written in here. Nothing run against a server.
  - C.3: the pre-intervention check starts 1.5 s before planned t_stop, not at t_a. In C1 a check at t_a raced the TimerStarted event and the twin's `accepted` effect, so a correctly planted target could be marked untested.
  - C.9: the receiver's start-up polls were recorded as failed `/health` checks, which would have made every live trial invalid; each check is now recorded once, by label, and the scorer requires all four. The twin control is now part of validity (except C6), as this section already said.
  - C.6: a restart outcome was "stayed ended" when no history was read on either side, and C6's server restart was compared against a describe-only claim-poll read; now both sides need a history read, and the before side is the latest closed one. Absences ("stopped", "no `final`", no duplicates, "not seen") are UNKNOWN when the receiver had a gap or the reads failed; a failed describe before the first claim makes "no false all-clear" UNKNOWN; worker-log acknowledgements count only in the watch. The scorer no longer stops on a PC trial whose SIGKILL was withheld.
  - C.2: C6's precondition polls continue until the same poll has a closed history, since describe and show run in parallel and can straddle the close.
  - C.1 and C.4: no `TEMPORAL_*` variable reaches a trial process, and the CLI ignores config files and environment configuration. The clean slate recognises a leftover dev server. Clean-up refuses new launches once it starts, stops anything left in the namespace before deleting it, and is no longer cut short by an interrupt.
  - C.10: the freeze refuses pilots that do not show the probe detecting PC's re-run, an evaluated pre-probe check, completed probe restarts or an evaluated C6 precondition.

- **Draft 4, prospective feasibility amendment, 13 Oct 2026:** no research freeze exists. C0-HB feasibility output has been inspected; no cancel, terminate or target-worker-kill condition has run. The failed full attempt `XC-pilot-c-pilot-1013-C0-HB-20261013-064853` keeps its original record: all three parallel CLI reads timed out under the shared 1.4 s budget; termination/reaping and validation finished after the gate, so the procedure was withheld. This motivated a new full-mode offset, fixed before the next attempt: step-4 arrival + 5 s, with 4.5 s shared I/O budget and 0.5 s reserve. C1 remains accepted + 15 s, with its check starting +10 s. The final gate remains stop − 0.1 s. FAST, the 120 s watch and the 60 s probe are unchanged. (Correction in draft 5: this entry and the draft-4 status were written at 07:11 on 13 Oct; two full pilots then ran on this draft, C0-HB at 07:14 and PC, a target-worker kill, at 07:22. C.13 lists them.)
  - Strict stage, identity, observer and cleanup requirements remain. Late evidence still withholds the procedure; missing on-time evidence is still untested. The new budget is a feasibility choice, not a guaranteed response bound.
  - The new stop phase removes support for the old exact C2/C2w/C5 step counts, step-10 delivery bound, C4 delivery delay and C6/C8 phase order. Those details are conditional or unpredicted; actual request replies, heartbeat, acknowledgement, pending-state and effect stamps remain evidence. Repeat counts require a further prospective addendum.
  - Offline fixtures demonstrate three parallel four-second reads fitting the new fixed-stop budget and a reaping/validation overrun still withholding it. Prior feasibility attempts remain excluded from current-source validation. Paired wall/monotonic guarding remains required before a screen freeze.
- **Draft 5, pre-freeze batch, 14 Oct 2026:** corrections made before any freeze, each test-first, with one or more mutants per correction in `temporal/mutate_c.py` (C.12). Nothing was run against a server. Records are schema 2.
  - C.4 step 5, C.9: the end-of-watch capability jobs get a readiness job's own bound. The shared deadline at t_stop + watch − 0.1 s, which every attempt so far had, left them about 7.9 s (4.9 s in FAST), and they timed out in 2 of the 4 attempts that reached them (3 of 7 jobs). They now run beside the +120 s read, may end after the watch, and the probe waits for them. The scorer's validity now requires them, as C.9 already said.
  - C.3, C.6, C.8: the full-mode stop moves from step 4 + 5 s to step 4 + 5.5 s, off the step-9 emission and the nominal heartbeat RPC 8 s into the activity; the check still starts 5 s before the stop, with its 4.5 s budget. Restored for the new phase: the delivery bound (step 14, with a late step or final now standing on a receiver gap), the exact C2, C2w and C5 step and false all-clear rows, C4's nominal delivery times under each reading, C7's retry timing and C8's cancel before attempt 1's heartbeat timeout. Left descriptive or unpredicted: C4 stage 1, C6's outage (the server's exit races the next heartbeat RPC), C7l's attempt numbers, C8's attempt-2 race, step 13 in C2, C2w and C5, and the O2 and pending-on-closed cells.
  - C.5, C.6: work ceased needs the computation boundary closed besides bounded quiet, which is kept as it was; effects closed judges the protected effects and the durable obligation apart and is never "closed" while the run is open or continued as new; the new per-trial verdict, verified stop (as B.6). M1-M5, the false all-clear and the restart outcomes are unchanged.
  - C.10: the runner accepts an attempt only on the scorer's validity (PC: and its gate), keeps every attempt, reruns a condition once, resumes a stopped batch and records the PC gate.
  - C.9: the prospective wall/monotonic check across the trial, in the harness and recomputed by the scorer from receiver.jsonl, required from record schema 2 and by the freeze.
  - C.0, C.1, C.9, C.10: the declared host profile, `wsl` or `linux-vm`: the VM's disk floor, kernel check, outbound isolation probe and worker identity; `wsl` unchanged. Independent VMs may each run one active trial; each architecture has its own freeze.
  - C.12: the freeze needs the seven pilots of C.10, each valid by the scorer on the final source and the declared host profile; it records the profile, needs its manifest and the predictions committed, and runs the mutation check. Every attempt is exported with a SHA-256 manifest outside its raw folder, and `analyze_c.py` writes its reports outside raw folders.
  - Status, C.13: the dated disclosure of every feasibility attempt and of the derived files inside three raw folders.

  New validation pilots on this source are needed before a freeze; the attempts of C.13 cannot serve.
- **Pre-freeze review of draft 5, 14 Oct 2026:** corrections made before any freeze, each checked against the code first and made test-first, with one or more mutants per behavioural correction in `temporal/mutate_c.py`. Nothing was run against a server. Record schema stays 2: no schema-2 record exists yet.
  - C.6, C.8 (C4 stage 1): decided from evidence that workflow code acted on the cancel before the local activity finished (an acknowledgement, the close before the `final` effect, a cancelled marker) or did not (the run open after the `final` effect), never from the order of CancelRequested and the marker. That order shows when the server wrote the request, and a request buffered while a workflow task is open is written at the next task completion, which the local activity's workflow-task heartbeat forces before the marker under either reading; draft 5 would have read (a) in a (b) world and then refuted stage 2's prediction. The order is now a descriptive row.
  - C.8, C.10, C.12: the validation pilots and the procedure smokes are prior exposure for their own conditions. Each trial records the predictions' SHA-256 at its start; the freeze needs it to equal the frozen file's, or the predictions to name the run in `exposed_validation`.
  - C.3, C.4: each action keeps the exact command typed and Temporal's whole reply (stdout, stderr, JSON); so does the clean-up terminate, and every CLI call keeps its raw output.
  - C.7, C.8: K14 (the activity re-raises the cancellation with no cleanup) is named, with K1 and K8, in the must-name list; the C2, C2w and C5 effects rows and C4 stage 2 are relabelled, and the HB rows are split into delivery time (P|K4,K6+M) and stops after delivery (C|K1,K8,K14).
  - C.5, C.6 (verified stop): "stopped" needs a valid trial and the history cross-check (every started work attempt maps to a closed execution; with no execution, positive history evidence that the work never ran); an execution that ran to its `final` effect gives "ended by completion, not by the stop". C.8's verified-stop row changes to match.
  - C.4 step 1, C.9, C.10: one StopCheck trial per host, enforced by host locks held around the namespace and a check for other trials, their processes, namespaces, units and fixed-port listeners; recorded as `host_exclusion`, required from schema 2.
  - C.10, C.12 (minor): the freeze binds the screen's seed and order; it checks the pending-on-closed example, the restarts' new identities and a FAST smoke of each procedure the pilots do not run.
  - C.6 (minor): a server restart's window starts at readiness; effects during the outage are "continued through the outage". The delivery bound needs the heartbeat phase precondition. New diagnostics: the step in flight at a cancellation (from the workload's new phase tracking), step lateness and the observer's load.
  - C.9 (minor): the scorer's validity now has C.9's checks: clock order and a failed tick POST without a gap are diagnostics; a malformed effect post (written in here) and a worker-side failure of our code invalidate.
  - C.1, T25 (minor): the CLI writes prefixed upper-case enums, not PascalCase; CONTINUED_AS_NEW is listed among M2's closed statuses, as the scorer had it.
