# n8n 2.41.7: source facts for the Experiment B predictions (checked 10 Oct 2026; Shutdown section amended 13 Oct)

Two passes, both read-only:
- **Research:** one agent read the installed code in WSL (`/opt/n8n-2.41.7/node_modules`: @n8n/config 2.39.2, n8n-core 2.41.5, n8n-nodes-base 2.41.5, @n8n/task-runner 2.41.6, bull 4.16.4) and the TypeScript at tag `n8n@2.41.7` (commit dcba26c8, 5 Oct 2026).
- **Verification:** a second agent re-opened every cited line to try to refute it. No value was wrong; the corrections below add missing conditions or mark inferences.

Paths are relative to `/opt/n8n-2.41.7/node_modules/`. Items marked *inference* come from reading the code, not from running it, and are predictions to test.

## Queue (Bull)
- **Lock (lease):** 60 s (`QUEUE_WORKER_LOCK_DURATION`), renewed every 10 s (`QUEUE_WORKER_LOCK_RENEW_TIME`). The stalled check runs every 30 s (`QUEUE_WORKER_STALLED_INTERVAL`).
  - Source: `@n8n/config/dist/configs/scaling-mode.config.js:167-206`.
- **`maxStalledCount` is hard-coded to 0**, so a stalled job is never re-run, and no env var changes this.
  - Source: `n8n/dist/scaling/scaling.service.js:121`. Bull's own defaults (lock 30 s, maxStalledCount 1) are overridden: `bull/lib/queue.js:226-240`.
- **Stall detection needs two checks.** A check puts each active job in the stalled set, and each lock renewal takes it out again. A job fails only at a later check that finds it still in the set with its lock gone.
  - Source: `moveStalledJobsToWait-7.lua:60-133`, `extendLock-2.lua`.
  - So a killed worker's job fails 50-60 s after the kill or later.
- **Only workers run stall checks** (the only `.process()` call is `scaling.service.js:158`).
- **After a worker kill, the final status depends on what is still alive:**
  - No worker running or restarted: the job stays active and the row stays `running` indefinitely. Queue recovery skips it, because the job is still in Bull's active list.
  - Worker restarted, same main: the worker's start-up recovery writes `crashed`. When the stall is detected, main overwrites it with `error`, with `stoppedAt` from main's clock.
  - Main restarted too: nothing writes `error`. The `crashed` row from the worker's log stays. If that log has no node events for the execution, the row stays `running` until queue recovery runs, up to 180 min later.
  - So a killed worker does not simply leave `crashed`.
- **PR #40086** (return jobs taken during worker shutdown to the queue) was merged on 7 Oct 2026 into #40050's branch. It is in no release tag from `n8n@2.41.7` to `n8n@2.43.3` (compared by commit), so 2.41.7 does not have it.

## Stop (`POST /api/v1/executions/{id}/stop`)
- **Path:** both the public route and the internal `/rest` route call `ExecutionService.stop` (`n8n/dist/executions/execution.service.js:355-430`).
  - Stoppable statuses: new, unknown, waiting, running.
  - Otherwise `WorkflowOperationError`, which only the public route maps to HTTP 409. A missing execution gives 404.
- **What the stop does:**
  - Main writes `canceled`, with no condition and with `stoppedAt` from main's clock, before it replies. It never waits for the worker.
  - The abort, or `job.remove()` for a queued job, happens only if this main holds the execution in its in-memory ActiveExecutions.
  - The cancel handler is not awaited, so the job can be removed after the HTTP reply.
  - A Redis pub/sub `stop-execution` message goes to the workers.
- **The worker's save on cancel has no condition.** Which `stoppedAt` lands last (main's or the worker's) is a race, so predictions must say which `stoppedAt` they mean: the reply's, or the final row's.
- **A stopped queued execution replies with a start time of 1970-01-01.** Its `startedAt` is empty, and the reply returns `new Date(startedAt)`. This was seen in both feasibility runs.
- **If the main that receives the stop does not hold the execution** (main restarted after enqueueing), only the database row changes:
  - A running execution runs to completion: its progress and final saves are skipped, so the row stays `canceled` while side effects happen.
  - A queued job still runs, because `setRunning` has no condition (`execution.repository.js:259-266`).
  - *Inference;* a prediction to test.

## Crash recovery (two writers)
- **Queue recovery** runs on the leader main: at start-up, then every 180 min (`N8N_EXECUTIONS_QUEUE_RECOVERY_INTERVAL`), in batches of 100. It sets `stoppedAt` to the time of the check (`scaling.service.js:540-612`; `execution.repository.js:219-258`).
- **Event-log recovery** (mode `extensive`) amends executions from the event log.
  - `stoppedAt` is the event time of the last node, in workflow order, that has a started event and no saved run data: its finished time if it finished, otherwise its first started time. This explains the crashed execution whose `stoppedAt` was 31 ms before its first ping.
  - It does nothing when the log has only workflow-level messages.
  - It writes with no status guard.
- **Worker event logs never close an execution** (`execution-lifecycle-hooks.js:567-590`; `message-event-bus-log-writer.js:196-217`). At every worker start, extensive recovery re-amends every execution in its current and previous log file that is not success, error or canceled with data.
  - A parked `waiting` row is written `crashed` for a moment, then rewritten `waiting` because `waitTill` is set. Its `stoppedAt` and data change. *Untested.*
- **Main's event-log recovery does nothing in queue mode**, because main's hooks drop node events.

## Listing
- **Status filter values:** canceled, crashed, error, new, running, success, unknown, waiting.
- **Why queued executions are missing:** when the status is omitted or is `new`, the list excludes the ids that this main holds in memory as `running`. In queue mode a queued execution is `running` in main's memory while its row is `new`. That is why queued executions were absent from the default list and from `status=new` in both feasibility runs.
  - Source: `n8n/dist/public-api/v1/controllers/executions.public.controller.js:65-81`; `execution.service.js:456-479`; `active-executions.js:46-77`.
  - *Inference:* after a main restart, the same queued rows appear with `status=new`.

## Waiting executions
- **Where a wait is held:** the Wait node keeps waits shorter than 65 s in process (`setTimeout`, cancellable), and writes longer ones to the database as `waiting` with `waitTill`. This applies only to time-interval or specific-time resume (`n8n-nodes-base/dist/nodes/Wait/Wait.node.js:505-518`).
- **WaitTracker:** runs only on the leader. It polls at start and every 60 s for waits due within 70 s, and resumes them with `setTimeout` (`n8n/dist/wait-tracker.js:47-127`).

## Execute Command
- **How commands start:** `child_process.spawn(command, {shell: true, detached: true})` on Linux, so each command gets its own process group and session.
- **On abort:** SIGTERM to the group, then SIGKILL 5 s later. The SIGKILL timer is cleared when the child's stdio closes, so a `&` child that redirects its stdio and ignores SIGTERM survives a stop.
- **If the worker itself is SIGKILLed or force-exits,** nothing kills the group.
- Source: `n8n-nodes-base/dist/nodes/ExecuteCommand/ExecuteCommand.node.js:6-72`.

## Task runner (internal mode)
- **Task timeout:** 300 s (`N8N_RUNNERS_TASK_TIMEOUT`). On timeout in internal mode, the runner process is SIGKILLed and restarted.
- **Heartbeat:** every 30 s (`N8N_RUNNERS_HEARTBEAT_INTERVAL`). A missed pong fails the runner's in-flight tasks and SIGKILLs and restarts the runner, so a runner whose event loop is blocked dies after about 30-60 s, not 300 s.
- **Default JS mode** is `secure`.
- Source: `@n8n/config/dist/configs/runners.config.js:20-31`; `task-broker-ws-server.js:47-70, 145-170`.

## Shutdown and other defaults
*Amended 13 Oct 2026 from a re-read of the installed code (worker drain, runner tasks, main's shutdown, Redis loss). Paths below are under `n8n/dist/` unless they name another package.*
- **Graceful shutdown timeout:** `N8N_GRACEFUL_SHUTDOWN_TIMEOUT` is 30 s.
  - At SIGTERM or SIGINT a force-exit timer is armed. When it fires, the process logs, sleeps 2 s, then exits with code 1, so the process is gone at SIGTERM + 32 s at the latest.
  - Source: `generic.config.js:20`; `base-command.js:223-228` (`exitWithCrash`), `386-403` (`onTerminationSignal`); the worker's `stopProcess` runs only after the shutdown handlers finish (`commands/worker.js:39-49`).
- **Worker drain** (`scaling.service.js:225-254`, `stopWorker`, a highest-priority shutdown handler, `:595-596`):
  - It pauses its local queues (`pauseAllQueues`, `queue.pause(true, true)`, `:213-216`).
  - It then loops while any queue job is still running, with no bound, or while in-process executions (Execute Workflow children) remain and less than 80 % of the window (24 s) has passed.
  - Only when no queue job runs and the 24 s are spent does it call `ActiveExecutions.cancelRunningExecutions` (`active-executions.js:225-251`: writes `canceled` within a write deadline, then `stopExecution` for each).
  - While a queue job (the agent, B, a parked backlog job) still runs, the loop lasts until the force exit, so the cancel step is never reached.
- **Runner tasks are capped at SIGTERM + 24 s** (`task-runners/task-broker/task-broker-ws-server.js:28` ratio 0.8, `:73-79` `capTaskTimeoutsForShutdown`, a highest-priority shutdown handler at `:216-221`; `task-broker.service.js:673-691`).
  - When a capped task times out in internal mode, the runner is not killed and is not told to cancel. The task fails with `TaskRunnerShutdownTimeoutError` and the broker stops tracking it (`task-broker.service.js:401-430`, `441-450`).
  - The broker's drain of active tasks has the same 24 s budget (`task-broker-ws-server.js:195-213`).
- **A Code node's HTTP calls run in the worker process:** `helpers.httpRequest` is one of the runner's RPC methods (`@n8n/task-runner/dist/runner-types.js:4-12`; `js-task-runner/js-task-runner.js:346-350`). The worker's task requester makes the call in its own process (`task-runners/task-managers/task-requester.js:279-330`). The broker ignores RPCs for a task it no longer tracks (`task-broker.service.js:189-193`), and the runner's `makeRpcCall` has no timeout (`@n8n/task-runner/dist/task-runner.js:341-359`).
  - So a Code node's pings and outside calls end when its task fails or when the worker process is gone, whatever the runner process does.
- **`helpers.httpRequest` carries no cancel signal** (`n8n-core/dist/execution-engine/node-execution-context/utils/request-helpers/factory.js:22-32`). Only `httpRequestWithAuthentication` sets `requestOptions.abortSignal` (`authentication.js:14`). Cancelling an execution or its task does not abort an in-flight `helpers.httpRequest`. Its socket closes when the call ends or the worker process exits.
- **The internal task runner's own SIGTERM:** the runner is in the worker unit's cgroup, so systemd's stop reaches it directly.
  - It waits up to `N8N_RUNNERS_GRACEFUL_SHUTDOWN_TIMEOUT` (30 s) for the broker's drain message, then stops taking tasks (`isDraining`, `stopTaskOffers`), waits for its running tasks until 30 s after the signal, closes its connection and exits. It does not abort its running tasks.
  - It force-exits 30 + 10 s after the signal (`N8N_RUNNERS_SHUTDOWN_FORCE_KILL_MARGIN`).
  - Source: `@n8n/task-runner/dist/start.js:16-48, 71-76`; `config/base-runner-config.js:44-45`; `task-runner.js:401-419`.
- **systemd** (measured 11 Oct, SPIKE-RESULTS F1): `systemctl stop` sends SIGTERM to every process of the unit's cgroup at once. When the main process exits, the remaining processes get SIGTERM again (the final-SIGTERM stage) and SIGKILL 40 s later (`TimeoutStopSec` 40 s).
- **So a worker stopped with running work** (*inference from the code*, tested by B8):
  - Code-node work stops by SIGTERM + 24 s.
  - Other in-process work (AI Agent and HTTP Request nodes, in-memory waits) runs until the worker process exits, at SIGTERM + 32 s at the latest.
  - The worker's open `helpers.httpRequest` sockets close at that exit.
- **Main's shutdown in queue mode:**
  - `stopMain` pauses only its local queue (single main) and stops queue recovery and metrics (`scaling.service.js:217-224`).
  - `stopProcess` stops the WaitTracker and removes the non-webhook triggers (`commands/start.js:76-95`).
  - `ActiveExecutions.shutdown` cancels only in regular mode with `cancelAll`. Otherwise it drops waiting and new entries and waits for the rest (`active-executions.js:264-291`).
  - Nothing is sent to the worker, and the worker's jobs and children keep running.
- **Redis loss makes each n8n process exit** (`services/redis-client.service.js:29-35, 135-165`, `makeRetryStrategy`). Bull's clients come from the same service (`scaling.service.js:124`).
  - Each client retries every 1 s and adds up the time between attempts; a gap of more than 30 s resets the sum.
  - Once the sum exceeds `QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD` (10000 ms), the process logs "Exiting process due to Redis connection error" and calls `process.exit(1)` (`:147-153`). `exitOnRedisUnreachable` is true outside tests (`:35`).
  - There is no graceful shutdown. Main and the worker each exit about 10-11 s after Redis goes away, and their running work needs no Redis until then.
  - *Inference;* B10 tests it.
- **Redis timeout threshold:** `QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD` is 10000 ms.
- **Default `NODES_EXCLUDE`:** `["n8n-nodes-base.executeCommand","n8n-nodes-base.localFileTrigger"]` (`@n8n/config/dist/configs/nodes.config.js:34`; tag `nodes.config.ts:38`).

## Publication bookkeeping and long trigger node ids (checked 13 Oct)
Paths are relative to `/opt/n8n-2.41.7/node_modules/`. Every 12-13 Oct pilot's `logs/main.log` shows `QueryFailedError: value too long for type character varying(36)` from `workflow-publication-trigger-status.repository.ts`.
- **The column:** `workflow_publication_trigger_status` keys one row per trigger node on `workflowId` and `nodeId`, both `varchar(36)` (`@n8n/db/dist/entities/workflow-publication-trigger-status.js:19-23`; migration `1784000000040-CreateWorkflowPublicationTriggerStatusTable.js`). The other inserted values fit: n8n's 16-character workflow id, the version id (a UUID), and short status and trigger-kind enums. n8n's own node ids are UUIDs (36 characters).
- **Our value that did not fit:** the webhook trigger node id, `"w-" + path` with path `xb-<trial>-<agent|fanout|collateral>` (`workflows_b.webhook_node` before 13 Oct): 6 + len(trial id) + len(name) characters. The collateral's exceeded 36 for every trial id over 20 characters (all 12-13 Oct pilots), the agent's for trial ids over 25 (the runner's default batch ids, so every screen trial id), the backlog's for ids over 24.
- **What fails:** after the applier has activated the triggers, the status reporter writes the rows and marks the outbox record completed in one transaction (`n8n/dist/workflows/publication/publication-status-reporter.js:160-165`; `replaceForWorkflow` deletes then inserts, `@n8n/db/dist/repositories/workflow-publication-trigger-status.repository.js:22-27`). The insert fails, the transaction rolls back and the consumer only logs it (`workflow-publication-outbox-consumer.js:236-239`). The webhooks work, but the record stays in progress. It is reclaimed after its 120 s lease (`@n8n/config/dist/configs/workflows.config.js:27`; poll every 15 s, `:26`) and processed again, and fails again: 3 errors in a B0 pilot's ~6 min main lifetime, 9 (3 workflows x 3) in the 27-character-id pilots.
- **Why it matters for B3 and B4:** unpublish and archive set the workflow inactive in the database at once and queue an unpublish record (`n8n/dist/workflows/workflow.service.js:1202`, called at `:892` and `:982`). A pending record is not claimed while its workflow has an in-progress one (`workflow-publication-outbox.repository.js:174`); the stuck record is reclaimed first when its lease expires (`:181`) and, the workflow now inactive, takes the unpublish branch (`workflow-publication-applier.js:60`, removing the published version at `:262`). So when the agent's trigger node id did not fit, its trigger teardown waited until about 120-135 s after its publication (roughly t_stop + 30-60 s), its webhook stayed registered while the API read inactive, and B4b's delete, which is refused while the published version exists (`workflow.service.js:924-926`, HTTP 409 "still being unpublished"), would have stayed refused beyond the harness's 20 s retry.
- **Since 13 Oct** the webhook node id is `"w-" + node name` (`w-Webhook`), unique in each workflow; the path and webhook id are unchanged. `tests/test_harness_b.PlantedNodeIds` checks every planted node id fits 36 characters for the longest accepted trial id.
