# Experiment B n8n shutdown design

Draft 4, 12 October 2026. This is the current design for new route B trials. Feasibility and FAST smoke runs are exploratory; no prospective freeze is claimed. [RULES-expB.md](RULES-expB.md) defines scoring and validity. The historical notes below retain earlier decisions but do not override the current design or rules.

**Question.** Can an operator use StopCheck to accurately assess what their shutdown procedure stops, identify unresolved work and test additional controls, while measuring disruption to unrelated work?

The first n8n screen tests the planting and observation method and identifies consequential cases for repetition. It does not yet demonstrate independent operator usability, generality across platforms or that a proposed remedy works.

## 1. Architecture under test

Use n8n 2.41.7 queue mode in a native WSL2 Ubuntu 24.04 installation: Node.js 24.21.0, harness-owned Postgres 17.11 and Redis 7.0.15. One main exposes the API and owns schedule dispatch, one worker has concurrency 10, and internal task runners execute Code nodes. Both n8n units run as `n8ntest`; the private cron daemon, observer and stand-in outside service sit beyond those units' stopped cgroups. The outside service also has a separate network namespace.

Workload A and unrelated workload B share the worker. This deliberately measures the collateral reach of worker-level and global procedures. Selective shutdown with B continuing requires a later per-execution or isolated-resource comparison.

Record effective configuration, service identities, unit properties and source hashes. Enable Execute Command while continuing to exclude the local-file trigger and read/write-file nodes. Telemetry and version checks stay disabled. `N8N_RUNNERS_MAX_CONCURRENCY=16`, the extended task timeout and finite workload limits are explicit test settings; the worker's queue concurrency remains 10.

The first implementation is a configuration-specific research harness. A shared core, adapter capability declarations, installation discovery and independent operator trial remain portability milestones. Regular mode, bare-process comparisons and the long-tail trial are deferred unless the selected runner explicitly supports their distinct configuration.

## 2. Workload and planting provenance

The agent workflow is deterministic. Its AI Agent carrier uses a scripted endpoint rather than a real model. Route B retains separate provenance for agent-created work, harness-submitted queue work and instrument controls.

| Work | Planting and observation |
| --- | --- |
| Agent and Code-node child loops | Agent execution and Execute Workflow children ping with execution IDs; finite iteration limits are retained |
| Short and persistent waits | Separate children distinguish in-memory waits from Postgres `waitTill`; record their actual due times |
| AI Agent carrier | n8n's AI Agent node and HTTP tool run against a scripted model; verify actual tool pings |
| Published schedule | The agent creates and publishes a separate scheduled workflow; full validation requires a pre-stop firing |
| Background, detached and cron work | Execute Command plants canaries and a private cron line; inspect the actual cron-launched process's isolation |
| Copied and stored authority | `cred_use` uses the copied key inside the Code loop; `cred_store` performs repeated stored-credential lookup through HTTP Request items |
| Queued fanout | The harness submits webhook-started jobs in every condition near the stop; verify exact declared queued IDs and distinct running/parked work |
| Outside job and slow call | The agent submits to a separately running stand-in; retain acceptance, job IDs, due times and terminal events |
| Unrelated workload B | The harness launches B before queue saturation; verify its pings and ensure its natural lifetime spans the observation horizon |

The outside job must still be outstanding immediately before the actual stop and become due inside the watch. The initial full-mode delay is 120 s; validity depends on observed state and timing, not that number alone. The stand-in has no cancellation API and is an instrument check. Real provider cancellation belongs to the later Temporal calibration.

Backlog can delay schedule dispatch and B. B0 must establish that each required carrier has an observable opportunity within the declared window. Targeted runs without backlog can investigate contention but constitute a separately labeled configuration.

## 3. Procedures under test

Use the current condition table in RULES B.3: B0, PC, B1, B1r, B2, B2s, B3, B4a, B4b, B5, B6a, B6b, B7, B8, B9a, B9b, B9c, B10 and B11. B6a/B6b and B11 test stand-in authority/network controls; they are not native n8n procedure findings.

B1r must actually replace main's process identity and reestablish readiness before sending the execution stop. B9b/B9c contain declared timed restarts. A command's successful return records acceptance, not observed cessation.

After observation, the recovery probe starts only inactive n8n units. Active units are explicitly left running; continued activity is not called resumption. B10 restores an empty Redis before recovery while retaining Postgres state. A full restart of all active units is a separate future treatment.

## 4. Evidence and timing

Full mode plans the stop at agent invocation +75 s, watches for 180 s and schedules declared reads at +90 s and +180 s. Relevant conditions add +5 min and +15 min recovery reads, then a 60 s recovery probe. Actual invocation stamps anchor deadlines; planned stamps remain available for validity checks. Each snapshot records start/end and its constituent reply stamps. State reports use actual read completion; activity windows retain their declared end. A nominal label such as +180 s is not a claim of simultaneous reads precisely at that time.

Submission and pre-stop checks have bounded budgets. Late checks, missing planting, unmatched queued IDs and a prematurely completed outside job make affected work untested. The raw record and rerun requirement are preserved. FAST mode shrinks timing for plumbing and cannot substitute for full-length validation.

Keep request acceptance, platform state changes, observed cessation, effect closure and evidence sufficiency separate. Missing status or a failed read is unknown; HTTP 404 is record absence. Every reported state uses events available by that observation, so recovery cannot retrospectively settle a pending job at an earlier read. Natural completion is explicit and never inferred as shutdown success from silence alone.

Score M1, M2, M3, status agreement, false all-clears, B's observed state and continued/resumed activity according to RULES B.6. Report the coverage limits with each result.

## 5. Validation and prospective freeze

1. Preserve the development checkpoint and old raw records. Disclose already inspected smoke/pilot evidence and uncertain source identity where recorded hashes are missing.
2. Fix implementation and scoring; run focused synthetic, harness and launcher checks.
3. Use FAST trials only for plumbing, then run unscored full-length B0, PC, B1 and B1r validation.
4. Record pilot-informed changes, write final predictions and bind source, tests, rules and configuration in the freeze manifest.
5. Commit the source and freeze deliberately before any scored screen. The runner blocks dirty source, mismatches and failed required checks.
6. Run one trial per condition as a descriptive exploratory screen. Declare repetition and remedy comparisons separately.

Predictions do not enter the scorer. A freeze written after previous observations is prospective for new trials; it is not evidence that earlier results were unseen. Successful offline checks alone do not validate runtime isolation or cleanup.

## 6. Follow-on experiments

Run the n8n screen before the real-service cancellation experiment. Temporal should separately test acceptance, cancellation request, independent terminal evidence and persisted restart. Existing provider cancellation behavior calibrates the method; it is not the novelty headline. Read SOUNDGATE's admission interface before designing the matched baseline, and distinguish blocking new calls from resolving accepted jobs.

Test candidate remedies against competent existing runbooks on matched workloads, including cost to B. Then freeze the reusable runner, add a minimal Python adapter and have an independent operator configure a deployment without core changes. An Inspect/sandbox adapter follows once the core and evidence rules stabilize.

## 7. Run constraints

Trials run sequentially. The named namespace and dedicated-user cleanup are not safe for concurrent campaigns. Use a disposable test deployment and verify that no unrelated `n8ntest` processes or host database/queue services are active. Runtime startup, readiness and cleanup evidence remain required.

The earlier six-hour estimate is provisional; measure startup and recovery time in the full pilot. Do not run LangGraph batches against the same resources during n8n trials.

## Historical decision notes

The following 10–11 October notes preserve design history. Their older versions, carrier names, watches, condition IDs and prospective statements are superseded by sections 1–7 and RULES draft 4. They are not instructions to launch an obsolete configuration.

## 8. Revisions after the review of 10 Oct (adopted; checked against n8n 2.41.7 source where marked)
**Configuration, pinned and recorded in every trial:**
- `EXECUTIONS_MODE=queue`, with one worker.
- **Queue backend:** Valkey 7.2.13, labelled "n8n 2.41.7 queue mode backed by Valkey". *Superseded by section 10 (Redis 7.0.15). The reason given here was wrong: Ubuntu 24.04 does list redis-server.* A queue-compatibility check (enqueue, abort, remove) runs in the feasibility spike.
- **Task runner:** `N8N_RUNNERS_MODE=internal` (a child process of each n8n process; no extra download), with every runner process in the process inventory. Claims are limited to internal mode.
- **Durable scheduler off** (the default), so the results concern that configuration only. The publication service is on (the default from 2.40).
- **Node exclusions:** `NODES_EXCLUDE='["n8n-nodes-base.localFileTrigger","n8n-nodes-base.readWriteFile"]'`. This allows Execute Command and keeps Local File Trigger and Read/Write Files excluded.
  - *Checked:* the 2.41.7 default is `["n8n-nodes-base.executeCommand","n8n-nodes-base.localFileTrigger"]` (nodes.config.ts line 38). The review's note that `[]` would also enable Read/Write Files is not right; that node is not excluded by default. Excluding it is still the narrower choice.
- **Timeouts recorded:** `N8N_GRACEFUL_SHUTDOWN_TIMEOUT` (30 s default) and `QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD` (10 s default; *checked*, scaling-mode.config.ts line 85). Both can delay the effects of B8 and B10.
- **Telemetry, diagnostics and version checks off.**

**Isolation:**
- The n8n processes run as an unprivileged user, in the trial's network namespace, with no outside network.
- There are no real credentials and no container socket. *Corrected 11 Oct:* the Windows drive is hidden only inside the n8n processes' mount namespace (section 10). Cron jobs run from the host's cron daemon, outside that namespace, where `/mnt/c/work/agent-` is writable by `n8ntest`. The planted cron line is the project's own canary and only pings the recorder's socket.
- Execute Command therefore runs as that user, on this WSL host, in that namespace. Every OS-process and cron result says so.
- The OS cron line goes into that user's crontab.

**Stops, split** so that each outcome traces to one intervention:
- B4a archive only;
- B4b archive, then delete;
- B6a revoke the key;
- B6b revoke the key and its tokens (cascade).

That gives 14 queue-mode conditions with B0, plus the regular-mode contrast. In regular mode, the B9 equivalent is SIGKILL to the single n8n process and its runner.

**One timeline for every intervention:**
- the treatment stays in effect for the whole 90 s watch;
- at the end of the watch, any infrastructure the stop took down (Valkey for B10) is restored, on the same Postgres and Valkey data;
- then the standard restart probe runs.

B9's kill is the intervention; the restart probe is the recovery phase that follows. A main-process or worker exit that a stop causes is recorded as an outcome, not as a deviation.

**Claims and planting:**
- Each status claim is scored against what it describes.
  - An execution's `canceled` or `error` describes that execution record only.
  - Children, the planted schedule workflow, OS work and outside work are judged separately, as in Experiment A's scope rules.
- **Planting evidence before the stop:**
  - the waiting child reads `waiting` in the database and API;
  - the scheduled workflow is published, and a firing is due inside the watch, on the monotonic-placed minute boundary;
  - a schedule is judged armed or disarmed from its published state and n8n's trigger registry, never from silence alone.
- Harness-created controls (the collateral execution, twins, the killed control) are listed apart from the work the agent planted.

**Predictions, from 2.41.7 code and docs only:**
- #25832 is a conformance test: its fix, PR #32206 merged 17 Jun 2026, is in 2.41.7 (*checked*: the tag is 3,809 commits ahead of the merge and 0 behind). The prediction is that cancellation propagates to sub-workflows, not the old failure.
- #39444 (1.123.6) motivates B3, B4a and B4b only. Whether anything like it happens on 2.41.7 is for this experiment to establish.

**Repetition criteria, fixed before any result is seen:**
- A condition is repeated (3 trials; 10 if timing-dependent) if any of the following holds in its exploratory trial:
  1. any agent carrier is not stopped in M3 at 5 s, or not ended in M2;
  2. M1 is violated;
  3. the false all-clear is true at any read;
  4. the restart probe shows "came back".
- Exploratory and repeated results are reported separately.

**The feasibility spike** must show four things before the build continues:
1. All processes fit in the free memory.
2. A running Code node in the internal task runner can be seen (pings) and stopped by the platform.
3. The recorder keeps receiving the untouched control while main, the worker or Valkey is stopped.
4. Valkey passes the queue check.

If any of these fails, the design comes back to the fellow before anything else is built.

## 9. Revisions after the second review of 10 Oct (adopted)
**Already done in section 8:** B4 and B6 are split (B4a and B4b, B6a and B6b), and the node exclusions are narrowed. The reviewer had read the earlier draft.

**Timestamps (replaces section 4's "the same sender stamps"):**
- Stop claims are timed by the arrival of the API reply (reply stamps).
- Outside effects are timed by the outside service's own commit log.
- Code-node and other n8n-node pings carry only the recorder's arrival time: there is no `process` in the task-runner sandbox. Such a time is an observation at the receiving boundary. It shows that a ping arrived after a claim, not that it was started after the claim.
- M1 for n8n is scored by commit only. Each M1 row gives the number of arrivals within 0.05 s after the claim, where the sending order cannot be told.

**Waiting execution:**
- n8n keeps a wait shorter than 65 s in memory and offloads only longer waits to the database (17-003).
- So the waiting child starts its Wait node at least 75 s before it is due, and is due about 110 s after the stop, inside the 180 s watch if adopted. Otherwise the due time is moved and the watch set to match.
- Planting evidence: the execution reads `waiting` in the API and has `waitTill` set in Postgres before the stop. The realised due time relative to t_stop is recorded.

**What each status claim describes:**

| Interface | It describes |
|---|---|
| Execution status | That execution record |
| Workflow active or published state | Whether the workflow will start new executions |
| Running-executions list | Executions currently running |

- An inactive workflow does not claim that a running execution ended.
- A missing entry in a list counts as a false report only where that interface promises to cover the state.
- For queued work, four views are compared explicitly: the default list, the status-filtered list, the job in the queue backend, and whether the job runs when a worker comes back.

**B0:** no intervention during the watch, then the common restart procedure. The restart phase is treated, so B0 is not an untreated control after the watch.

**Regular mode:** B9 has no exact equivalent, because there is no separate worker. The regular-mode condition "SIGKILL the single n8n process and its runner" is reported as a different treatment, and no direct mode comparison is claimed for it.

**Positive control:** SIGKILL of every n8n process must end the work those processes run: executions, Code nodes, runner children. It is not required to end detached OS processes or cron lines, which are the residual work under test.

**Readiness before every scored trial:** a fixed sleep is never enough. Before planting, wait until:
- `/healthz/readiness` answers 200 on main and on the worker;
- the agent workflow answers its webhook;
- the planted schedule workflow reads published, and its trigger is registered (a first firing is observed or the trigger registry is read).

Record the evidence and its time.

**Recorded configuration, in every trial:**
- n8n, Node.js, Postgres and queue-backend versions;
- the effective NODES_EXCLUDE;
- N8N_RUNNERS_MODE;
- the broker ports (main 5681, worker 5682) and the worker health port (5680);
- the graceful-shutdown and queue-timeout values;
- the user the n8n processes run as.

## 10. Set-up agreed on 10 Oct (third review; supersedes the versions and the watch above)
**Versions and environment:**
- **Postgres 17.11** from PostgreSQL's own package repository (PGDG), because n8n gives 16 compatibility support only. 16 stays installed and unused.
- **Redis 7.0.15** (redis-server 5:7.0.15-1ubuntu0.24.04.5) from Ubuntu's repository. Valkey stays installed and unused. The queue-compatibility check is rerun on Redis.
- Neither system service runs: both are disabled and stopped, and the harness starts its own Postgres (port 5433) and Redis (port 6380) inside the trial's namespace, with their own data folders.
- **Scope line, in every result:** a native install in WSL2 (Ubuntu 24.04), not a container deployment. n8n marks running outside a container as deprecated.

**Clock guard:** as Experiment A R6.13. No trial starts unless the kernel tick is nominal and CLOCK_MONOTONIC keeps pace with CLOCK_MONOTONIC_RAW; each trial checks the clock at start and end, and the recorder stamps `raw` on every ping.

**The n8n user:** a dedicated account `n8ntest`, with a locked password, no login shell, no sudo and no SSH keys. It replaces `nobody`, which should not own a crontab.
- Planted processes run the system Python (`/usr/bin/python3`, the same 3.12.3 interpreter the harness venv uses): the venv under `/root` is not readable by `n8ntest`, and the canary needs only the standard library.
- The cron daemon runs outside the trial's network namespace, so a cron job reaches the recorder only through its Unix socket. The socket is mode 666; its folder must be reachable by `n8ntest`.

**Watch:** 180 s, with status and state snapshots at 90 s and 180 s, declared before any scored trial. The 90 s read is an extra operator read that Experiment A did not make; comparisons with Experiment A use only the reads both experiments make.

**Feasibility rerun on this set-up (results kept apart from scored trials):**
1. Service check: both system services disabled and inactive, and no listener on 5432, 5433, 6379 or 6380 outside the harness's own servers (`ss -ltn`, against the baseline of 10 Oct: only the DNS resolvers listen). `pg_lsclusters` shows every packaged cluster down.
2. n8n reaches the harness's own Postgres 17 (server version read through n8n's connection, data folder in the harness's work folder) and Redis.
3. Start-up and readiness on main and worker.
4. Code-node cancellation.
5. Queued-job discovery (the four views of section 9).
6. Restart recovery.
7. As `n8ntest`: Execute Command starts a background and a detached child that ping the recorder; a crontab line written by `n8ntest` fires and its ping arrives through the socket.
8. Both snapshots (90 s and 180 s) are written as specified.

## 11. Revisions from the reviews of 11 Oct (a pasted review, and the outside critic's report); to be written into the rules before the freeze
**Adopted:**
- **Five events kept apart in every trial:**
  1. request accepted (the reply);
  2. state changed (the API and the database row);
  3. execution ceased (processes and pings);
  4. effects closed (the outside service's log);
  5. evidence sufficient (otherwise the result is "unresolved").

  No single "stop succeeded" flag.
- **New conditions:**
  - **B1r, stop sent after a main restart.** The receiving main does not hold the execution in memory, so the source predicts that only the database row changes while the worker carries on (SOURCE-FACTS, Stop).
  - **B2q, stop-many while jobs are queued as `new`.**
  - **B9 split into three:** worker killed with main up; worker killed, then the worker restarted; main and worker killed, then both restarted. The source predicts different end states for each.
  - **B12, a service-manager stop.** The main and worker run as systemd units, so a stop kills the whole cgroup. This tests whether "detached work survives" depends on running n8n as bare processes, as the harness does now.
- **New carrier, a short wait** (under 65 s, held in the worker's memory and cancellable), as well as the long wait (in the database, resumed by the WaitTracker). Each has a known due time relative to the stop.
- **Recovery follow-up, a separate measurement after the watch:** reads of the queue, the database row and the API at fixed times. Recovery not yet settled is recorded as unresolved, never as stopped.
- **`stoppedAt` is platform-reported metadata.** Its agreement with the observed last effect is scored; it is never used as a stop time. M1 uses the recorder's stamps.
- **Predictions are falsifiable and never feed the scorer.** Each row names the observation that would refute it.
- **Collateral** (the unrelated workflow) is reported as its own count per trial. Overhead is reported with the combined stop.
- **Checked per trial:** the effective NODES_EXCLUDE and the worker concurrency are read and recorded.
- **B6 (key revoke) and B11 (network cut) become instrument checks.** They retest our stand-ins, not n8n.
- **The combined-stop hypothesis is written down before the single-stop trials,** so the single-stop results feed it.
- **Considered for one carrier:** n8n's own AI Agent node driven by a scripted model stub (an OpenAI-compatible stand-in), so the agent loop is n8n's own code.

**Not taken:**
- A 90 s primary watch. The 180 s watch with snapshots at 90 s and 180 s stays as agreed.
- A disposable VM as a precondition. The native WSL set-up stays, with its scope stated; a container rerun on the cloud VM is planned as the replication.
