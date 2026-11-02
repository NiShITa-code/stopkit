# Experiment C design: cancellation calibration on Temporal

Draft 5, 14 October 2026 (pre-freeze batch). Not frozen. Five feasibility attempts have run and been inspected: C0-HB attempts that exposed launch-identity capture, planting, precheck-budget and capability-deadline limitations, and a full pilot of the positive control PC, a target-worker kill (RULES-expC C.13 lists them all). No cancel or terminate condition has run. Probe and cleanup actions are separate treatments. The earlier records remain unchanged and do not validate this revision. [RULES-expC.md](RULES-expC.md) holds the prospective definitions: conditions and timing fields, measures, validity and provisional expectations. Every source-based statement about Temporal cites a numbered fact (T1, T2, ...) in section 9. Coding choices are numbered K1-K14 (section 3). A pre-freeze review of draft 5 (14 October) changed the C4 stage-1 rule, the verified stop, what each action records, the one-trial-per-host rule and the freeze gate (section 11). Section 11 records why each draft changed.

## 1. Question and role

**Question.** When an operator cancels or terminates a Temporal workflow that Temporal accepted before the stop, which of the job's effects still reach an independent receiver, what does Temporal report and when, and does the job stay ended across worker and server restarts?

**Role: calibration of the accepted-remote-job case.**
- In Experiment B the agent submits a job to a stand-in outside service that has no cancellation API, so a stop can only leave that job unresolved (DESIGN-expB section 2; RULES-expB B.14 step 6). Temporal is a real service that accepts jobs before the stop and offers cancel and terminate.
- Temporal documents what these requests do (T1-T12). Predictions are written from that documentation and the installed code (RULES C.8). The experiment then checks whether StopCheck, measuring only from outside, reproduces the documented behaviour, including the cases where Temporal's own status disagrees with the job's effects.
- Agreement is evidence that the instrument and its scoring rules see what they claim to see. A disagreement is first treated as a possible instrument fault or a misreading of the sources. It is reproduced outside the harness (RULES C.11) before it is reported as a difference from the documentation.
- The documented behaviour is not a finding of this project. No result is described as novel, and no rate is drawn from one trial.

**What the operator tool needs from it.** A tested rule for when a provider's status may close an accepted job, and when independent effect evidence is still needed. The candidate rule under test: a terminal workflow status closes the workflow record, but it does not by itself show that a running activity has stopped producing effects (T3, T6).

## 2. Deployment and trust boundary

| Component | Process | Role | What it can show |
|---|---|---|---|
| Temporal dev server | `temporal server start-dev --db-filename <trial>/temporal.db`, headless, on 127.0.0.1 (CLI 1.9.1, Server 1.32.0) | Under test | Its own records: event history, execution description, visibility list |
| Target worker | Python 3.12.3 with temporalio 1.34.0, own process (`jobs_c.py worker`), task queue `expc-target-<trial>` | Under test (SDK code), running our workload code (C) | Nothing directly: its effects are judged at the receiver. Its own event log is a diagnostic |
| Twin worker | Same code, separate process, task queue `expc-twin-<trial>` | Hosts the controls | As the target worker |
| Client | Short-lived Python process (`jobs_c.py start`, `jobs_c.py ready`) that starts workflows through the SDK | Submission | Acceptance: the start reply with workflow ID and run ID |
| Receiver | `receiver_c.py`: the unmodified handler of `langgraph/recorder.py` (GET `/ping`, `/health`) plus a POST `/effect` route | Instrument; stand-in outside service (S) | The commit record of every effect |
| Observer | A harness thread that runs the CLI (`describe --raw`, `show`, `list`, JSON output) | Instrument | What an operator would read, with reply stamps |
| Harness | Root Python process (`harness_c.py`) | Instrument | Launch, signal and restart records; stamps; guards |

**Network and ports.** Every trial process runs in a fresh network namespace that has only loopback. No Temporal process can reach the internet, and the fixed ports cannot clash with other services: server gRPC 7833, HTTP API 7834, metrics 7835, receiver 8875.

**Persistence.** Each trial gets a new `--db-filename` in its own results folder; without that flag the dev server loses executions when its process dies (T12). The database file is kept as raw evidence.

**User.** The SDK virtual environment is under `/root`, which only root can read, so the server, workers and harness all run as root. This differs from Experiment B, where only the harness ran as root and n8n ran as the unprivileged user `n8ntest`. The planted work is only loopback POSTs inside the namespace. Running the workers as an unprivileged user needs a readable copy of the environment (open decision 1, section 10).

**Trust boundary.**
- **Under test:** the dev server, the CLI and the SDK code inside the worker processes.
- **Ours, inside the system under test:** the workflow and activity code. Its choices are labelled C, or named in a compound label (RULES C.7).
- **Instrument:** the receiver, the observer and the harness.
  - The receiver is the only source for "an effect landed". An effect lands when the receiver has written and flushed its log line, stamped on arrival with CLOCK_REALTIME, CLOCK_MONOTONIC and CLOCK_MONOTONIC_RAW, as `recorder.py` does.
  - Temporal's records are claims to be scored, never evidence that work stopped.
  - The worker's event log is our code's view from inside the system under test. It is used to time when a cancellation reached the activity, to show that a local activity was cancelled before it returned (C4 stage 1), to say which step was in flight, and to detect our own code's failures (RULES C.9); never as evidence of cessation.
- **One host clock.** All processes of a trial run on one host (the laptop's WSL2, or one VM under the `linux-vm` host profile) and share CLOCK_MONOTONIC (as R6.1). Temporal's history event times are platform metadata (T17). They are placed on the monotonic clock through paired wall/monotonic stamps and never used as stop times, as Experiment B treats `stoppedAt`. That placement, and Temporal's own timers, assume that wall and monotonic time agree across the trial, which the wall/monotonic check now tests (RULES-expC C.9).

**Why the dev server.** It is the installed configuration. Results concern the dev server with SQLite persistence only, which its own help text marks as not for production (T12).

## 3. The job (workload)

Each job is one run of the workflow `JobC`:
1. **Stage A, accept:** an ordinary activity `announce` posts one `accepted` effect.
2. **Stage T, timer:** `workflow.sleep` for 30 s.
3. **Stage W, work:** one activity of one variant posts a `progress` effect for each step 1-20, one per second, then one `final` effect, then returns. Step n is posted at about (n − 1) s after the activity starts, on a monotonic step clock, so the steps do not drift.
4. The workflow returns, so an untouched job reads COMPLETED.

Every planted item is a benign canary: its only effect is a POST to the local receiver, which logs it. Effects come only from activities. The workflow sandbox restricts non-deterministic calls (T16), so Temporal's usual pattern puts side effects in activities. The workload parameters are the workflow's input, so each run's history records them.

**Effect record** (one JSON POST per effect): trial, role (target, twin, ptwin, ready), workflow ID, run ID, activity type and ID, variant, attempt and `is_local` (T19), `exec_nonce`, step, kind, worker identity and PID, and the sender stamps `smono` and `swall` taken just before the POST. `exec_nonce` is a random value drawn once per activity invocation, so two executions are told apart even where Temporal's attempt number does not change. The receiver adds its sequence number and arrival stamps.

**Activity variants** (full mode; FAST values for smoke runs are in RULES C.10):

| Variant | Kind | Heartbeat | Timeouts | Documented path of a cancellation |
|---|---|---|---|---|
| HB | Ordinary activity | `heartbeat()` after each progress effect; heartbeat timeout 5 s | Start-to-close 60 s | Delivered when the activity heartbeats; heartbeats are throttled to one RPC per 0.8 × 5 s = 4 s (T3, T4, T5) |
| NH | Ordinary activity | None | Start-to-close 60 s | Never delivered (T3) |
| LA | Local activity | Not available for local activities (T8) | Start-to-close 60 s | Two stages (condition C4): when the cancel reaches workflow code (T8, D4), then whether the local activity stops without heartbeats (T8, D2) |

All activities use one declared retry policy: initial interval 1 s, coefficient 2.0, maximum interval 10 s, at most 3 attempts. Temporal's default allows unlimited attempts (T11); a bound keeps a dead worker's retries from running on after the trial. `announce` has start-to-close 10 s. The workflow task timeout is the server default (10 s, T8), recorded per trial.

**Coding choices, fixed for the first pass** (label C, or named in a compound label):

| Id | Choice | Why it matters |
|---|---|---|
| K1 | Activities are `async def` | A threaded synchronous activity is cancelled differently (T5); deferred contrast |
| K2 | The workflow does not catch cancellation, so a cancellation that reaches the workflow function ends it CANCELED (T7) | It decides the closing status of every cancel condition: the SDK writes the cancel command only when a cancelled exception escapes the workflow (T7) |
| K3 | Cancellation type is the SDK default, TRY_CANCEL (T6), except in C2w and C3w | Decides whether the workflow closes before the activity acknowledges |
| K4 | HB heartbeats once per step with no details; a retried execution starts again at step 1 | Sets the heartbeat phase (section 4, "Timing"), and the number of duplicate effects after a retry |
| K5 | The declared retry policy above | Sets retry timing and the attempt bound |
| K6 | The timeouts above | Set crash detection (T10), and the heartbeat throttle interval, which is 0.8 × the heartbeat timeout (T4) |
| K7 | Effects only from activities | Standard pattern (T16) |
| K8 | The POST uses an asyncio stream with a 2 s timeout, so a cancellation can interrupt it. A failed POST fails the activity. The activity tracks its phase (posting n, heartbeat n, sleeping after n) and the effect in flight, in memory, and writes them with each POST's reply stamp only in its last log line | At most one effect is in flight when a cancellation arrives, and the log says which. A failed POST is expected only when the receiver is unavailable; the tick canary checks for that, and an `activity_error` line in the watch invalidates the trial (RULES C.9) |
| K9 | The LA runs about 20 s | Longer than the documented guidance that local activities be short (T8); it is the case D4 handles with workflow-task heartbeating. It is chosen to make a cancellation window observable; it is not typical use |
| K10 | Target and twin run on separate workers and task queues | Worker kills never reach the twin |
| K11 | Eager activity execution, sticky cache and concurrency limits at SDK defaults; the worker prints its effective options at start | Recorded configuration |
| K12 | On SIGTERM the worker script calls `Worker.shutdown()` (T27); the graceful shutdown timeout is the SDK default, zero (T15) | Decides what a graceful worker stop does to a running activity (durability probe, section 8) |
| K13 | Workflow execution timeout 15 min | Safety bound; outside every observation window |
| K14 | The work activity does not catch cancellation for good: it logs asyncio.CancelledError and re-raises it, with no cleanup and no final effect | Temporal decides when a cancellation reaches the activity (the heartbeat RPC); whether the activity then stops producing effects is decided here. With K1 and K8 it is named wherever an effects row says the activity stops after delivery (RULES C.7) |

**Heartbeats and the heartbeat timeout.** The sources differ in strength (T3). D2 says that cancelling an activity from a workflow requires that the activity sends heartbeats and sets a heartbeat timeout. D1 and D3 say only that a cancellation is delivered when the activity heartbeats. The installed worker uses 0.8 × the heartbeat timeout as the throttle interval when one is set, and 30 s otherwise (T4). That sets the delay of a heartbeat, but it does not show that the server delivers a cancellation to an activity without a heartbeat timeout. Delivery without a timeout is therefore untested here; HB0, a deferred contrast, would test it. The first pass follows D2: HB heartbeats and sets a timeout.

**Control jobs in each trial** (outside the declared set):
- **Twin:** the same workflow and variant as the target, started at the same moment on the twin worker (25 s later in C6, so that it is in its timer during C6's restart). No intervention is aimed at it.
- **Readiness and capability jobs:** a one-activity workflow `ReadyC` that posts a `ready` effect. It runs on each task queue after start-up, after every restart and at the end of the watch.
- **Probe twin (ptwin):** started at the beginning of the durability probe: a 30 s timer, then a 3-step HB activity. Its being in its timer when the probe stops the workers is a checked precondition (section 8).
- **Tick canary:** the harness posts a `tick` effect to the receiver every second.

## 4. Conditions

One intervention procedure per condition. The binding table, with exact timing fields and pre-intervention checks, is RULES C.3.

| Id | Intervention | Calibrates |
|---|---|---|
| C0-HB, C0-NH, C0-LA | None | Each variant's natural effect sequence and history |
| PC | SIGKILL the target worker during NH; no restart in the watch | That the receiver sees a stop; Temporal's view of an activity whose worker is dead (T10, T13); that the probe detects a re-run |
| C1 | Cancel during the timer | T1, T7 |
| C2 | Cancel during HB (TRY_CANCEL) | T3-T7. The workflow closes at once, before the activity's next heartbeat; expected to look like C5 |
| C2w | Cancel during HB, with cancellation type WAIT_CANCELLATION_COMPLETED | T3-T6. The only condition in which the run stays open until the activity confirms: the positive calibration of "the activity learns at its next heartbeat" (D1, D5) |
| C3 | Cancel during NH | T3, T6 |
| C3w | Cancel during NH, with cancellation type WAIT_CANCELLATION_COMPLETED | T6, T7. Separates what TRY_CANCEL does to the status from what the activity does |
| C4 | Cancel during LA | T8 in two stages: when the cancel reaches workflow code (D4), and whether the local activity then stops without heartbeats (D2) |
| C5 | Terminate during HB | T2, T5 |
| C5n | Terminate during NH | T2, T3 |
| C6 | Cancel during HB; SIGTERM the dev server 1 s after the cancel returns, then restart it on the same database file | What an abandoned activity does while heartbeat RPCs cannot reach the server; that the closed record survives (T12). Descriptive (section 7) |
| C7 | SIGKILL the target worker during HB; restart it 15 s later; no cancel | T9, T10, T11 |
| C7l | As C7, during LA | T8 |
| C8 | SIGKILL the target worker during HB; cancel 3 s later while no worker runs; restart the worker 20 s after the kill | A cancel that is recorded but not yet acted on (T1, T10, T11) |

Every trial ends with the same durability probe, which restarts both workers and the server (section 8; RULES C.4). So "stays ended" is tested in every condition, and PC tests that the probe can detect a re-run.

**Timing.**
- **Anchored on the receiver.** The full-mode anchor t_a remains the first execution's step-4 progress arrival. Draft 5 fixes planned t_stop at t_a + 5.5 s, 8.5 s into the activity. C1 remains accepted + 15 s. These are prospective offsets, never delays chosen after inspecting a cancellation outcome. FAST retains its former offsets and is plumbing only.
- **Why 5.5 s.** Step n is emitted about (n − 1) s after the activity starts. Draft 4's t_a + 5 s put the stop 8 s in, on the step-9 emission: in the PC pilot step 9 was sent 1.8 ms before t_stop, so whether it counted as before or after the stop was decided by milliseconds. It also put the stop on a nominal heartbeat RPC. At 8.5 s the stop is half a step from every emission. Both facts came from no-stop or kill pilots (RULES-expC C.13); no cancellation outcome was seen.
- **Heartbeat phase.** D3 and T4 describe the throttle: with K4's one heartbeat per step and K6's 5 s timeout, one RPC per 4 s. In the C0-HB pilot, describe's `last_heartbeat_time` placed the RPCs about 0.02 s after each 4 s mark from the activity's start (seen in feasibility; the compiled core was not read). So the new stop falls 0.5 s after the RPC at 8 s and 3.5 s before the next at 12 s, the phase relation draft 2 had at step 4 + 1.5 s, one throttle period later. That restores exact predictions: after a cancel or terminate in HB, steps 10-12 follow the request, step 13 races the 12 s RPC, and no step from 14 on lands (the delivery bound). Repeating this one offset is not systematic phase coverage; C2φ remains a separate deferred contrast.
- **Nominal remaining work.** With one-second steps, the stop is 8.5 s into a 20 s activity, leaving about 11.5 s to natural completion. Actual effect stamps take precedence over this calculation.
  - HB under cancel or terminate: delivery at the 12 s RPC, about 3.5 s after the stop.
  - NH: nominal final about 11.5 s after the stop, inside the watch.
  - LA: the first workflow-task heartbeat, at about 80 % of the 10 s workflow-task timeout, falls about 0.5 s before the stop, and the next about 7.5 s after it. Under C4's reading (a) the cancel arrives then; under reading (b), after the local activity finishes, about 11.5 s after the stop. Which reading holds stays unpredicted, and it is decided from what workflow code did (an acknowledgement, the run's close before the final effect, a cancelled marker; or the run still open after the final effect), never from the order of the request and the marker in the history: a request that arrives while a workflow task is open is written at the next task completion, and the workflow-task heartbeat is one, so the request can precede the marker under either reading (RULES-expC C.6; T24).
  - C7 and C8: the last heartbeat RPC before the kill is about 0.5 s earlier (seen in feasibility), so attempt 1's 5 s heartbeat timeout falls about 4.5 s after the stop. C8's cancel at +3 s precedes it by 1.5 s, with the worker confirmed dead. Restarts remain at +15 and +20 s, with actual pending and sticky-queue state retained.
  - C6: CANCELED before the server SIGTERM remains a checked precondition. The SIGTERM is due about 1.2 s after the stop, 2.3 s before the activity's next nominal heartbeat RPC; in the two full pilots the dev server took 1.8 s and 2.0 s to exit after a SIGTERM. Whether the server is down when that RPC is due is therefore a race, and C6 stays descriptive. Report the observed order and do not force this contrast to qualify.
  - PC: nominal start-to-close timeout about 51.5 s after the kill; observed timer and status stamps are authoritative.
- **Precheck budget.** Full mode starts 5 s before the planned stop, 0.5 s after the work anchor, or at accepted + 10 s for C1. The receiver health check, made as soon as the anchor is detected, and the parallel CLI reads (three; four in C6, which adds the twin's history) share an I/O deadline at stop − 0.5 s, a 4.5 s budget for the reads. The remaining reserve covers process reaping and validation, which must still finish before stop − 0.1 s. A late result withholds the procedure; an on-time evidence failure is untested. Call timestamps are separate, not an atomic snapshot. Stage and identity requirements are unchanged.
- **Watch of 120 s and probe of 60 s.** These remain unchanged. The end-of-watch capability jobs start at +112 s with a readiness job's own bound and may end after the watch; the probe waits for them (RULES-expC C.4 step 5). Nominal completion and retry calculations motivate the opportunity; actual readiness, effects and control evidence decide whether an attempt qualifies.

**Deferred contrasts** (not in the first pass): HB0 (heartbeats without a heartbeat timeout, which tests D2's requirement); C2φ (C2 with a seeded phase offset); threaded synchronous versions of HB and NH; C6k (server SIGKILL instead of SIGTERM); C6n (server restart during NH); C7n (worker kill during NH, detected only by the 60 s start-to-close timeout); C8t (worker down during the timer); workflow cleanup handlers; a local activity cancelled from inside the workflow without a workflow cancel.

## 5. Measures

Definitions are in RULES C.5 and C.6. In short:
- the five events kept apart, as in Experiment B: request accepted, state changed, work ceased, effects closed, evidence sufficient. Work ceased needs the computation boundary closed (each work execution ended by its `final` effect or by its worker's confirmed death) besides the activity-based bounded quiet; effects closed judges the protected effects and the accepted durable obligation, the run itself, apart, and is never "closed" while the run is open, because a running workflow can still retry;
- the verified stop, the operator's verdict over the declared set at the end of the watch (stopped; ended by completion, not by the stop; not stopped; or unresolved, as B.6). It needs a valid trial and a history that shows no work attempt the receiver's executions leave open; a job that ran to its final effect inside the watch ended by completion, which is not the request stopping it;
- effects after the request, at the receiver, by execution and attempt, by commit and by initiation;
- the claim (the first read showing a closed status), the terminal status, when WorkflowExecutionCancelRequested first appears in the history, and when describe first reports that a cancel was requested (T23);
- when progress stopped, and whether the final effect landed;
- duplicate effects, and whether Temporal's attempt numbers reveal them;
- status against reality at each read, and the false all-clear. The platform-decided example is C5n: terminate closes the run whatever the workflow code does (T2), the non-heartbeating activity never learns of it (T3), and its final effect lands after Temporal reads TERMINATED. C3 shows the same pattern for a cancel, but there the closed status also depends on our workflow code (K2) and on the TRY_CANCEL default (K3);
- pending activities on a closed run, reported as their own row, because no source read says whether the server clears them when a run closes (section 9, "Not read");
- whether the history records each execution that produced effects;
- restart outcomes: stayed ended, resumed, re-run, closed after the restart, or unresolved. The watch measures count effects up to the end of the watch; the durability probe is a treatment, and its effects count only in its own restart outcome;
- the twin's outcome.

## 6. Validity

Rules are in RULES C.9 and C.10. In short:
- the clock guard (R6.13), with the wall/monotonic consistency check across the trial that the harness applies and the scorer recomputes from the receiver's stamps, and `common/guards.py`;
- the declared host profile, `wsl` or `linux-vm`, with its own disk floor and isolation check; a VM's identity is recorded;
- one StopCheck trial per host: host locks held around the trial's namespace, and a check for any other StopCheck trial, process, namespace, unit or fixed-port listener before anything starts;
- our own code failing inside the system under test (a failed effect POST, a worker that stopped by itself) invalidates the trial;
- controls: the untouched twin, receiver health and the tick canary, readiness and capability jobs, the persistence controls (C6's twin and the probe twin), and PC as the gate before any screen;
- planting evidence, and the checked preconditions of C6 and of the durability probe; an untested target leads to a rerun;
- a failed read or request is UNKNOWN, never "stopped";
- destructive clean-up only after the last observation;
- the smoke, pilot and screen run kinds of Experiment B; the runner accepts an attempt only on the scorer's validity, keeps every attempt, reruns once, and exports each with a SHA-256 manifest outside its raw folder.

## 7. Predictions

The provisional expectations use section 9. The C0-HB and PC feasibility pilots have been inspected (RULES-expC C.13), so the eventual predictions JSON must disclose that exposure. No cancel or terminate condition has been observed. The table (condition × measure, with label, basis and refutation) is RULES C.8. The source-based pattern is:
- an honest status in C1, C2w and C3w;
- in C2 and C5, brief activity after a terminal claim: steps 10-12, perhaps 13, then nothing, so a false all-clear at the first claim;
- a false all-clear that lasts until the final effect in C3 and C5n;
- in C3w, no false all-clear but no stop either: the run reads COMPLETED after the final effect;
- in C4, stage 1 is unpredicted: the two readings in D4 (external events wait until local activities finish; long local activities heartbeat the workflow task) give different times, and the trial records which held;
- in C6, a descriptive record of what the abandoned activity does during the outage; whether its final effect lands is unpredicted;
- in C8, a cancel recorded 1.5 s before attempt 1's heartbeat timeout, with no worker to act on it until the restart;
- duplicate effects in C7 and C7l, with a history that shows only the last attempt;
- the verified stop: ended by completion, not by the stop, where an execution ran to its final effect and the run closes (C0, C3, C3w, C5n, C7, C7l); stopped only in C1, where no work ran; not stopped in PC, whose run stays open; unresolved in C2, C2w and C5, whose first execution ends without independent evidence.

Whether the operator rule O2 ever holds in C2, C3, C5, C5n and C6 depends on whether a closed run still lists its abandoned activity as pending, which no source read settles. Those O2 cells are unpredicted.

## 8. Trial timeline, duration and scope

| Phase | Approximate time | Content |
|---|---|---|
| Clean slate | about 10 s | Clock check (2 s window), guard snapshot, ports free, no leftover Experiment C process, new namespace and database path |
| Start-up | measured in the pilot | Receiver; server and its readiness; both workers; readiness jobs |
| Submission | t_sub | The client starts the target and the twin |
| Stage A, then stage T | t_sub + about 2.5 s to + 34 s | `accepted` effects; 30 s timer |
| Stage W | from about t_sub + 34 s | Step 4 arrives about 3 s later, about t_sub + 37 s (36.7 s and 37.4 s in the two full pilots) |
| Intervention | t_stop: nominally about t_sub + 17.5 s (C1), about t_sub + 42 to 43 s (others); actual effect anchors decide | Pre-intervention check, then the intervention thread |
| Watch | 120 s from t_stop | Claim poll, scheduled reads, in-watch restarts, capability jobs from +112 s (they may end after the watch; the probe waits for them) |
| Durability probe | about 80-90 s | Probe twin; pre-probe check; graceful stop of both workers and the server; restart on the same database; capability jobs; 60 s observation |
| Clean-up | about 10 s | Terminate open trial workflows, stop all trial processes, check nothing is left |

**The pre-probe check.** With a graceful shutdown timeout of zero (T15), stopping a worker cancels any activity it is running, with reason WORKER_SHUTDOWN, and the Python worker then reports that activity as cancelled (T5). What the server does with a "cancelled" result for an activity it never asked to cancel is server code, which was not read; if it refuses the result, the attempt times out and is retried (T10, K5). So before the probe stops anything, the harness reads describe for every open trial run and records any pending activity in state STARTED, and it checks that the probe twin is in its timer (TimerStarted, no TimerFired, no pending activity). No open run is predicted to have a STARTED activity at that point, in any condition (RULES C.8). A run that is still open and has one, or whose declared effects reached the receiver in the 5 s before the probe stops anything, is reported as "the probe interrupted running work", and its probe outcome is unpredicted. A closed run that still lists a STARTED activity is recorded apart: whether a closed run lists its abandoned activity was not read (section 9, "Not read"), so that listing alone is not taken as running work.

A trial takes about 5 minutes. The first pass takes about 1.3 hours, plus full-length pilots (about 35 minutes), FAST smokes of the five procedures the pilots do not run (RULES-expC C.10) and reruns. On one machine, live runs never overlap any other StopCheck trial, an Experiment A or B trial included, because they share the machine, its ports, its clock and its memory. The namespace wrapper holds Experiment C's host lock and n8n's launcher lock from before the clean slate until the namespace is deleted, and the trial refuses if another StopCheck trial or its services are present (RULES-expC C.4 step 1). Independent VMs may each run one active trial at a time, under the `linux-vm` host profile. Each architecture (A, B, C) has its own freeze, and results from different machines or host profiles are never pooled (RULES-expC C.0, C.10).

**Out of scope:**
- production Temporal deployments: other persistence backends, multi-service clusters, Temporal Cloud;
- SDKs other than Python 1.34.0, and other CLI or server versions;
- threaded synchronous activities and the other deferred contrasts (section 4);
- workflow cleanup logic, child workflows, continue-as-new, signals and updates, schedules, Nexus, reset, pause and batch operations by query;
- standalone activities, which CLI 1.9.1 also offers (T18);
- any claim about Temporal beyond this dev-server configuration, any rate, and any claim of novelty;
- real outside services (the receiver is a stand-in) and model-driven agents.

**Implementation** (built and offline-tested; the feasibility attempts of RULES-expC C.13 ran against the dev server on 13 October, on earlier source). Files under `temporal/`:
- `receiver_c.py`: the receiver;
- `jobs_c.py`: the workflows `JobC` and `ReadyC`, the activities, and the worker and client entry points. The worker imports this file under its own module name, because the workflow sandbox imports a workflow class by module and class name (T26);
- `harness_c.py`: one trial;
- `analyze_c.py`: the scorer, which writes its reports outside the trial folder;
- `clockwall_c.py`: the wall/monotonic clock rule, shared by the harness and the scorer;
- `run_expC.py`: the sequential runner, with `--dry-run`, `--pilot`, `--smoke`, `--resume`, the scorer's admission and the export of every attempt, and the freeze;
- `mutate_c.py`: the mutation check (RULES C.12).

Not yet built: `reproduce_c.py` (RULES C.11), and a campaign tool for a screen split across VMs (RULES C.10). Offline tests are in `tests/test_analyze_c.py`, `tests/test_harness_c.py`, `tests/test_runner_c.py` and `tests/test_clockwall_c.py`; they replace every launch, CLI call and signal with a fake. One check used the installed SDK: on 13 October the workflow classes of `jobs_c.py` were passed, in-process and with no client, worker or server, to the validation a worker runs when it is built (SDK `worker/_workflow.py:213-221`, `worker/workflow_sandbox/_runner.py:78-97`), and both imported in the sandbox under the module name `jobs_c`. `common/clockguard.py`, `common/guards.py` and `langgraph/recorder.py` are imported unmodified. The following patterns are copied, not the files:
- from `n8n/harness_b.py`: the intervention thread with its stamp box, the pre-stop gate that withholds a late intervention, run-kind checks, clean-up last, and (draft 5, from the freeze `7a3fcec`) the declared host profile with its disk floor, kernel check and copied worker identity;
- from `n8n/analyze_b.py`: events bounded per read, the order in which UNKNOWN takes precedence, and (draft 5) the computation boundary and the verified stop;
- from `n8n/run_expB.py`: the source manifest, the predictions schema check and the freeze, and (draft 5) the scorer's admission of each attempt with one rerun and `--resume`, the GCE worker identity, the export of every attempt with a SHA-256 manifest, and committed freeze inputs;
- from `n8n/clockwall_b.py`: the wall/monotonic rule, copied unchanged as `clockwall_c.py`; and from `n8n/mutate_b.py`, the mutation check's method.

## 9. Temporal facts used (read 12 October 2026)

**SDK** means `/root/temporal-venv/lib/python3.12/site-packages/temporalio/` (temporalio 1.34.0). **CLI help** means the help text printed by `/opt/temporal-cli-1.9.1/temporal` (version 1.9.1, Server 1.32.0, UI 2.54.1). Documentation pages:
- D1: [Cancellation and Termination](https://docs.temporal.io/encyclopedia/workflow/cancellation-and-termination)
- D2: [Cancellation - Python SDK](https://docs.temporal.io/develop/python/cancellation)
- D3: [Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures)
- D4: [Local Activity](https://docs.temporal.io/local-activity)
- D5: [Events reference](https://docs.temporal.io/references/events)
- D6: [Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies)
- D7: [Temporal CLI server command reference](https://docs.temporal.io/cli/server)

The facts below are our paraphrases of what each source says. Where a prediction in RULES C.8 goes beyond what a source states, it is marked *inference*: it comes from reading code or documentation, not from running anything.

| Id | Fact | Source |
|---|---|---|
| T1 | A cancel request records a WorkflowExecutionCancelRequested event, and the service schedules a new task for the workflow. What happens next is decided by the workflow's code | CLI help, `temporal workflow cancel --help`; D1, "What happens when you cancel a Workflow"; SDK `api/enums/v1/event_type_pb2.pyi:301-302` |
| T2 | Terminate closes the execution with a WorkflowExecutionTerminated event. Workflow code cannot see or respond to it, and pending activities get no chance to stop cleanly | CLI help, `temporal workflow terminate --help`; D1, "What happens when you terminate a Workflow"; D5, WorkflowExecutionTerminated; SDK `api/enums/v1/event_type_pb2.pyi:324-327` |
| T3 | D1 and D3 say that an ordinary activity's cancellation is delivered when it heartbeats, and that one that does not heartbeat cannot receive it (it runs to completion, fails or times out). D2 is stronger: cancelling an activity from a workflow requires that the activity sends heartbeats and sets a heartbeat timeout. Heartbeat throttling can delay delivery | D1, "Cancellation and Activities"; D2, "Cancel an Activity from a Workflow"; D3, "Activity Heartbeat"; D5, ActivityTaskCancelRequested; SDK `api/enums/v1/event_type_pb2.pyi:289-292` |
| T4 | Throttle mechanism (D3): after sending a heartbeat, the worker sets a timer for the throttle interval; when it fires, the worker sends the most recent heartbeat and sets the timer again. Python interval: 0.8 × the heartbeat timeout when one is set; otherwise the default 30 s; at most 60 s | D3, "Throttling"; SDK `worker/_worker.py:129-130, 257-261` |
| T5 | When the core sends a cancel for a running activity, the Python worker cancels it whatever the reason: an explicit request, the activity no longer known to the server ("The activity no longer exists according to server (may be already completed)"), a timeout, worker shutdown ("Core is shutting down and the graceful timeout has elapsed"), pause or reset. An async activity's task is cancelled; a threaded one gets an exception raised in its thread. The worker marks the cancel as "by request" for an explicit cancel or a worker shutdown, and an activity cancelled that way that raises the cancellation is completed as cancelled | SDK `worker/_activity.py:166-167, 202-219, 420-435, 712-730`; reasons in `bridge/proto/activity_task/activity_task_pb2.pyi:40-51`; flags in `activity.py:173-194` |
| T6 | Cancellation types. TRY_CANCEL requests cancellation and reports it to the workflow at once. WAIT_CANCELLATION_COMPLETED waits until the activity confirms, which needs heartbeats. ABANDON sends no request. The Python SDK defaults to TRY_CANCEL for both ordinary and local activities and passes the value unchanged to the core. The proto comment for local activities says language SDKs should default to WAIT_CANCELLATION_COMPLETED; the installed Python SDK does not | SDK `bridge/proto/workflow_commands/workflow_commands_pb2.pyi:50-60, 683-687`; `workflow/_activities.py:72-83, 266, 1343`; `worker/_workflow_instance.py:3505-3508` |
| T7 | Workflow-side cancellation in Python. The cancel job cancels the workflow's main asyncio task. An awaited activity is shielded: the cancellation becomes a cancel command, and the await continues on the same result, so the activity's outcome decides what the workflow code receives; if the activity completes, its result is returned. A cancelled exception that escapes the workflow function after a cancel was requested becomes a CancelWorkflowExecution command; nothing else writes that command. `workflow.sleep` cancels its timer when it is cancelled | SDK `worker/_workflow_instance.py:660-672` (cancel job), `2087-2097` and `2345-2390` (activity await), `2820-2866` (closing as cancelled), `1928-1953` and `3370-3375` (timer), `3541-3549` (activity cancel commands) |
| T8 | Local activities run in the worker process and never enter an activity task queue. The result is recorded as a MarkerRecorded event when the workflow task completes. Execution is at least once. They do not support activity heartbeats. D4 says both that signals and other external workflow events are not processed until local activities finish, and that when a local activity approaches about 80 % of the workflow task timeout (10 s by default) the SDK heartbeats the workflow task. Separately, D2 says local activities, unlike ordinary ones, can be cancelled without heartbeats; in the SDK that cancel is a RequestCancelLocalActivity command issued from the shielded await | D4, including its list of workflow-task heartbeating trade-offs; D2, the note in "Cancel an Activity from a Workflow"; SDK `bridge/proto/activity_task/activity_task_pb2.pyi:225-227`; `worker/_workflow_instance.py:2087-2097, 3541-3549` |
| T9 | ActivityTaskStarted is not written to the history until the activity execution closes, and it reflects the last attempt; attempts count from 1. WorkflowExecutionCanceled means the workflow finished after a cancellation request and closed | D5; SDK `api/history/v1/message_pb2.pyi:1436-1437`; `api/enums/v1/event_type_pb2.pyi:271-274, 293-294, 303-304` |
| T10 | The server does not detect a crashed worker directly. It relies on the start-to-close timeout, or the heartbeat timeout when one is set, to fail the attempt; a retry follows if the retry policy allows | D3 |
| T11 | Activities retry by default (initial interval 1 s, coefficient 2.0, maximum interval 100 × the initial interval, unlimited attempts); workflows do not. Every activity needs a start-to-close or schedule-to-close timeout | D6; SDK `common.py:37-57`; `workflow/_activities.py:275-276, 295-297`; `worker/_workflow_instance.py:2079-2082` |
| T12 | `--db-filename` gives the dev server a persistent store; without it, workflow executions are lost when the server process dies. The dev server is not intended for production | CLI help, `temporal server start-dev --help`; D7 |
| T13 | An execution's description lists pending activities with state (SCHEDULED, STARTED, CANCEL_REQUESTED, PAUSED, PAUSE_REQUESTED), attempt, last heartbeat and start times, last failure and last worker identity | SDK `api/workflow/v1/message_pb2.pyi:751, 882-897`; `api/enums/v1/workflow_pb2.pyi:254-261`; `api/workflowservice/v1/request_response_pb2.pyi:4791-4795` |
| T14 | Workflow statuses: RUNNING, COMPLETED, FAILED, CANCELED, TERMINATED, CONTINUED_AS_NEW, TIMED_OUT | SDK `client/_workflow.py:1468-1494` |
| T15 | `graceful_shutdown_timeout` (default zero) is how long activities get after worker shutdown is called before their tasks are cancelled | SDK `worker/_worker.py:134, 277-279, 866-870` |
| T16 | The workflow sandbox restricts non-deterministic calls | SDK `worker/workflow_sandbox/__init__.py:6-9` |
| T17 | Every history event carries an `event_time` | SDK `api/history/v1/message_pb2.pyi:4472` |
| T18 | CLI 1.9.1 also offers standalone activities, with their own cancel and terminate | CLI help, `temporal activity --help` |
| T19 | Activity info includes activity ID, attempt, `is_local`, workflow ID and run ID | SDK `activity.py:107-129` |
| T20 | The CLI can describe an execution (`--raw` prints properties without changing their format), show its history, list executions by query and report cluster health; global options include `--address`, `--output json`, `--command-timeout` and `--log-format json` | CLI help: `temporal workflow --help`, `workflow describe --help`, `workflow show --help`, `workflow list --help`, `operator cluster health --help`, `temporal options` |
| T21 | A workflow task may sit on the sticky queue of the worker that last processed the workflow for up to the sticky schedule-to-start timeout (Python worker default 10 s) before it moves to the normal queue, where any worker may take it | SDK `worker/_worker.py:128, 253-256` |
| T22 | An execution's description also reports its pending workflow task: state (SCHEDULED or STARTED), scheduled time, original scheduled time, started time and attempt. The comment on the original scheduled time describes the workflow-task heartbeat as a workflow task completion that forces a new workflow task and has no command | SDK `api/workflowservice/v1/request_response_pb2.pyi:4803-4805`; `api/workflow/v1/message_pb2.pyi:1152-1172`; `api/enums/v1/workflow_pb2.pyi:283-285` |
| T23 | An execution's description has an extended-info flag `cancel_requested`, which "indicates if the workflow received a cancel request" | SDK `api/workflowservice/v1/request_response_pb2.pyi:4819-4821`; `api/workflow/v1/message_pb2.pyi:338-339` |
| T24 | The API has a notion of an unflushed buffered event: a request's history event can exist without a known event ID. It is documented on RequestIdInfo, which the same file says covers only StartWorkflowExecution requests. So the installed API shows that the server buffers some events; it does not show whether, or when, a cancel request is buffered. A worker can also force a new workflow task with no command, and the API names local activities that outlast the task timeout as the prime example; each such completion is a workflow task completion. So, if a cancel request arriving while a task is open is buffered, it is written at the next workflow-task heartbeat, before the local activity's marker, whatever the workflow code does with it (RULES C.6 C4) | SDK `api/workflow/v1/message_pb2.pyi:355-356, 2219-2236`; `api/workflowservice/v1/request_response_pb2.pyi:1597-1601` (`force_create_new_workflow_task`) |
| T25 | CLI 1.9.1's `--output json` writes enum values in the prefixed upper-case form (`EVENT_TYPE_WORKFLOW_EXECUTION_STARTED`, `WORKFLOW_EXECUTION_STATUS_RUNNING`, `PENDING_ACTIVITY_STATE_STARTED`) and int64 fields such as `eventId` as strings. The UI's history JSON uses PascalCase (`WorkflowExecutionStarted`), which the SDK converts to the prefixed form when it parses it; the scorer accepts both | Observed in every feasibility record, for example `temporal/results/XC-pilot-c-pc-1013-PC-20261013-072243/record.json` (RULES C.13); SDK `client/_helpers.py:46-70, 147-162` |
| T26 | The workflow sandbox imports the workflow class by its module and class name, and maps the module name `__main__` to `__temporal_main__`. Standard-library modules are passed through into the sandbox, and most `os` calls are restricted only at workflow run time | SDK `worker/workflow_sandbox/_runner.py:131-146`; `worker/workflow_sandbox/_restrictions.py:520-536, 641-655` |
| T27 | `Worker.shutdown()` sets the worker's shutdown event and waits until shutdown completes. `Client.start_workflow` returns a handle whose `result_run_id` is the started run's ID | SDK `worker/_worker.py:917-928`; `client/_workflow.py:158-170` |

**Not read.** The SDK's Rust core is compiled into `temporalio/bridge` and was not read. So the following are not established by any source above:
- whether the core sends an activity's first heartbeat at once, and exactly when it flushes a throttled one (D3 describes the mechanism; the code was not read);
- what the core does when a heartbeat RPC fails while the server is down, and how it retries it;
- how it numbers the attempts of a local activity re-run after a worker crash.

The server's code was not read either. So none of these is established:
- what the server answers to a heartbeat from an activity whose run has closed (the core's NOT_FOUND reason only says the server no longer knows the activity, T5);
- whether a closed run's description still lists its abandoned activities as pending;
- when the server buffers a cancel request while a workflow task is open (T24), and whether `cancel_requested` (T23) is set while it is buffered; so the order of the request and a local activity's marker in the history cannot show when workflow code saw the request (RULES C.6 C4);
- whether the server evaluates an overdue heartbeat timeout when it restarts;
- what it does with a "cancelled" completion for an activity it never asked to cancel;
- whether it skips the sticky-queue wait (T21) when the sticky worker is gone.

Predictions that depend on any of these points say so (RULES C.8).

## 10. Open decisions for the fellow

1. Run the server and workers as root, or copy the SDK environment to a readable location and run them as an unprivileged user.
2. Keep C2w, C3w, C5n and C7l in the first pass, or defer them.
3. Keep C6, whose outage rows are descriptive or unpredicted (section 7; its precondition, the closed record after the restart and its twin are predicted), or defer it.
4. Use the CLI as the only observer, or add an SDK reader as a second, independent observer.
5. The 120 s watch.
6. The durability probe in every condition (about 1.5 minutes per trial), or only in the conditions without an in-watch restart.
7. Vary the heartbeat phase in the first pass (C2φ), or keep it fixed as now.

## 11. Revision log
- **Draft 1, 12 October 2026:** written from the installed CLI help and SDK source and the documentation pages D1-D7. Not reviewed; nothing run.
- **Draft 2, 12 October 2026:** revised for the review of draft 1 (points 1-9; the review's text ends partway through point 9). The D2, D3 and D4 wording used below was re-read on the live pages on 12 October. The implementation was built and tested offline; nothing was run against a server.
  - Point 1, accepted. The note that the brief's heartbeat wording was wrong is removed. T3 now states D2's requirement next to the weaker D1 and D3 wording, delivery without a heartbeat timeout is called untested (HB0), and the claim is gone from the summary.
  - Point 2, accepted. C2 is now expected to look like C5, with the same prediction. C2w (HB with WAIT_CANCELLATION_COMPLETED) is added as the positive calibration of delivery at the next heartbeat.
  - Point 3, accepted. Cells that depend on K2, K3, K4 or K9 now carry a compound label that names the deciding choice (RULES C.7). C5n replaces C3 as the example of a false all-clear that the platform decides (section 5).
  - Point 4, accepted in part. The phase reasoning, the D3 citation, the C+M dependence and the removal of C2 and C5 from the ten-repeat set are adopted. The prediction is stated as "steps 6-8, and perhaps step 9" rather than "about 4". The phase is kept fixed rather than varied, because a fixed phase gives one sharp, refutable prediction; the varied version is deferred as C2φ (open decision 7).
  - Point 5, accepted with one correction. C4 is exempt from the "cancel recorded at the first read" row, the time CancelRequested first appears is a C4 measure, and describe's pending workflow task is read at every read (T22). The cited RequestIdInfo field documents buffering only for start requests (T24), so it shows that the server buffers some events, not that it buffers a cancel. Added: describe's `cancel_requested` flag (T23) at every read, as a second view of whether the server holds the request.
  - Point 6, accepted. C4 is split into stage 1 (when the cancel reaches workflow code) and stage 2 (whether the local activity then stops without heartbeats). "The docs contradict each other" is removed; the tension is between two statements inside D4.
  - Point 7, accepted. O2 cells in C2, C3, C5, C5n and C6 are unpredicted. Pending activities on a closed run are a row of their own, and the pilots must capture one example from C2 or C3, which were already in the pilot set (RULES C.10).
  - Point 8, accepted. C6's stage is a checked precondition; whether `final` lands is unpredicted; the re-run branch for a run left open across the outage is added; the "durability of the cancel" row is removed, because the durability probe covers a closed run and C8 covers a recorded cancel that was not yet acted on.
  - Point 9, accepted as far as its text goes. The review's last sentence breaks off ("The probe twin is assumed to be in its"); it is read as saying that the probe twin's being in its timer is assumed, not checked. Added: the pre-probe check in section 8.
  - Not taken: none of the review's points was rejected outright. Point 4's suggested seeded offset was deferred rather than adopted, for the reason given above.
  - Other changes: the implementation file names (section 8); facts T22-T27; the "Not read" list now names the server and core behaviour each revised prediction depends on.
- **Draft 3, 13 October 2026:** changes found while implementing and testing draft 2. Nothing was run against a server.
  - Point 4's label. The review asked for C+M. The cells carry `P|K4,K6+M` instead, because the throttle mechanism is the platform's (D3, T4), while the heartbeat cadence (K4), the heartbeat timeout that sets the 4 s interval (K6) and the phase at the stop (the anchor rule, M) are ours. RULES C.7's compound label names all three. K6 was missing from draft 2's labels and is now named wherever it sets a cell (RULES C.7, C.8).
  - Points 7 and 9 together. Draft 2's pre-probe check counted any STARTED pending activity as running work. A closed run that still lists its abandoned activity, which point 7 leaves unpredicted, would then have made the probe outcome unpredicted too. Running work is now a STARTED activity on an open run, or a declared effect in the 5 s before the probe stops anything; a closed run's listing is recorded apart (section 8).
  - The durability probe is a treatment, so the watch measures stop at the end of the watch (section 5). Without this, PC's predicted re-run in the probe would have failed PC's gate.
  - C6's precondition polls read the history as well as the description, so the last-event-ID comparison across the restart has a closed history read from before the SIGTERM. A history read taken before the run closed is not compared.
  - The CLI observer names the namespace (`--namespace default`), so that no `TEMPORAL_*` variable in the environment can redirect a read.
- **Draft 3, independent check, 13 October 2026:** the implementation was checked against RULES-expC draft 3, and the problems found were fixed in the code, the tests and the rules (RULES-expC revision log, "independent check"). For this document: the pre-intervention check starts 1.5 s before planned t_stop (section 4, "A check just before the intervention"), and trial processes run without `TEMPORAL_*` variables (RULES C.1). Nothing was run against a server.
- **Draft 4, prospective feasibility amendment, 13 October 2026:** inspected C0-HB feasibility attempts are now disclosed. The full attempt with a 1.4 s parallel CLI budget withheld its procedure after timeouts and reaping/validation overrun. Before a new attempt, the full stop moves to step-4 arrival + 5 s; health and parallel reads have a shared stop − 0.5 s I/O deadline, with the final validation gate still stop − 0.1 s. C1 remains accepted + 15 s and checks from +10 s. FAST, full watch and probe lengths stay unchanged. No old record is relabelled or rescored.
  - The changed heartbeat/workflow-task phase makes the old exact post-request step counts and C6/C8 timing order unsupported. They are now conditional or unpredicted. No cancellation outcome was inspected to choose this change. Stage, identity and observer evidence requirements remain strict.
  - A prospective paired wall/monotonic consistency guard remains required before a scored screen. The read-only audit of earlier receiver/sender timestamps cannot substitute for that gate.
  - (Correction in draft 5: this document's draft 4 was last written at 07:10 on 13 October, and RULES-expC's at 07:11; two full pilots then ran on that source, C0-HB at 07:14 and PC at 07:22. RULES-expC C.13 lists every attempt.)
- **Draft 5, pre-freeze batch, 14 October 2026:** corrections made before any freeze, each test-first with a mutant in `temporal/mutate_c.py`; RULES-expC's revision log lists them rule by rule. Nothing was run against a server. For this document:
  - the stop moves to step 4 + 5.5 s, half a step from the step emissions and 0.5 s after a nominal heartbeat RPC, which restores the exact HB predictions and the delivery bound and puts C8's cancel 1.5 s before attempt 1's heartbeat timeout; C6 stays descriptive, now because the server's exit races the next heartbeat RPC (section 4);
  - the end-of-watch capability jobs get a readiness job's bound and may end after the watch (sections 4 and 8);
  - work ceased needs the computation boundary closed; effects closed separates the protected effects from the durable obligation; the verified stop is added (section 5);
  - the wall/monotonic clock check, the host profiles, the scorer's admission of each attempt and the export of every attempt (section 6); independent VMs may each run one active trial (section 8);
  - the implementation list (section 8).
- **Pre-freeze review of draft 5, 14 October 2026:** each finding was checked against the code first; the corrections are test-first, with mutants, and RULES-expC's revision log lists them rule by rule. Nothing was run against a server. For this document:
  - C4 stage 1 is decided from what workflow code did, not from the history's event order, which a buffered request and the local activity's workflow-task heartbeat make the same under both readings (section 4; T24, "Not read");
  - K14 names our activity's handling of a delivered cancellation, and K8 now records the activity's phase and the effect in flight (section 3);
  - the verified stop needs a valid trial and the history's agreement, and a job that ran to its final effect ended by completion, not by the stop (sections 5 and 7);
  - one StopCheck trial per host, under host locks, and our own code's failures invalidate a trial (sections 6 and 8);
  - T25 is corrected to the CLI's observed output.
