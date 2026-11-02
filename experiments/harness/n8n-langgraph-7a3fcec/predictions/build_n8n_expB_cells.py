#!/usr/bin/env python3
"""Build predictions/n8n-expB-cells.json (RULES-expB.md B.8), DRAFT for the operator's review.

predicted_state is each carrier's M2 state at the end of the 3-minute watch (the t_stop+180s read), in the scorer's
vocabulary (analyze_b.m2_state). Every cell names its basis: n8n's installed code (file:function, paths relative to
/opt/n8n-2.41.7/node_modules/, see n8n/SOURCE-FACTS-2.41.7.md), systemd semantics measured on 10-11 Oct, or our own
workload and stand-in code (harness_b, workflows_b, mocks_b). A scoring rule is cited only next to the code that makes
the state, never alone. Every cell also names an observable refutation. Labels (B.7): P n8n code, C our workload,
O OS/supervisor, S stand-in, M measurement rule; all of B6a, B6b and B11 is S.

The configuration block is copied from a full-length pilot record made by the FINAL code (so it matches exactly what the
harness records), and is never edited by hand:
    python3 predictions/build_n8n_expB_cells.py --config-from n8n/results/<pilot-B0-dir>/record.json
Predictions never feed the scorer.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "n8n"))

SF = "n8n/SOURCE-FACTS-2.41.7.md"
CARRIERS = ("loop_steps", "loop_node", "sub", "sched", "wait_short", "wait_long", "ai_agent", "fanout",
            "bg", "det", "oscron", "cred_use", "cred_store", "keyvalid", "outjob", "out")
INSTRUMENT_CHECKS = ("B6a", "B6b", "B11")          # B.7: every cell of these is S
# Results of these conditions were inspected before the predictions were written (prior_exposure below)
EXPOSURE = {"B0": "12-13 Oct full-length pilots, 12 Oct preflight smoke and 11 Oct fast smoke",
            "PC": "12-13 Oct full-length pilots and 12 Oct preflight smoke",
            "B1": "12-13 Oct full-length pilots and 11 Oct fast smoke",
            "B1r": "three full-length pilots, none valid: 12 Oct, the preparatory restart failed on timing; 12-13 Oct "
                   "night, main restarted and the stop was sent, but the trial was invalid (the schedule had no "
                   "pre-stop firing) and its record may have been inspected; 13 Oct, main was not ready by its restart "
                   "deadline, so no stop was sent",
            "B2": "11 Oct fast smoke (the backlog starved the agent; that setup has since changed)",
            "B3": "11 Oct fast smoke", "B4b": "11 Oct fast smoke (delete refused with HTTP 409)",
            "B5": "11 Oct fast smoke (credential path built in; since changed)"}
# B.8: cells whose expectation was also seen in a feasibility run (n8n/SPIKE-RESULTS.md, by check)
K7 = "K7 (after SIGKILL of main's and the worker's process groups the killed execution never pinged again)"
F1_KILL = "F1 (systemctl kill -s SIGKILL of the worker unit ended every canary in it at once)"
F1_CRON = "F1 (stopping or SIGKILLing the worker unit left the private cron unit and its job running)"
FEAS = {("B1", "loop_steps"): "C2, K3 (a running Code-node execution stopped by POST /executions/{id}/stop: no ping "
                              "after the reply, status canceled)",
        ("PC", "loop_steps"): K7, ("B9c", "loop_steps"): K7,
        ("B2", "fanout"): "C4, K6 (stopping one queued execution removed its job and it read canceled)",
        ("B8", "fanout"): "C4, K6 (with the worker down a webhook execution waited in the queue)",
        ("B9a", "fanout"): "C4, K6 (with the worker down a webhook execution waited in the queue)",
        ("B9b", "fanout"): "K6 (a queued execution left alone ran when the worker returned)",
        ("B9c", "fanout"): "K6 (a queued execution left alone ran when the worker returned)",
        ("B8", "bg"): "F1 (systemctl stop of the worker unit: SIGTERM reached the whole cgroup at once)",
        ("B8", "det"): "F1 (systemctl stop of the worker unit: SIGTERM reached the whole cgroup at once)",
        **{(c, k): F1_KILL for c in ("PC", "B9a", "B9b", "B9c") for k in ("bg", "det")},
        **{(c, "oscron"): F1_CRON for c in ("PC", "B8", "B9a", "B9b", "B9c")}}
WAIT_LONG_K9 = " [seen in feasibility: SPIKE-RESULTS K9 (a persisted wait resumed and finished success)]"

# n8n source bases used in several cells (paths relative to /opt/n8n-2.41.7/node_modules/)
RPC = ("its pings and outside calls are Code-node helpers.httpRequest RPCs served by the worker process "
       "(task-managers/task-requester.js handleRpc)")
STOP_ONE = ("execution.service.js stopInScalingMode: main holds the agent's queue execution in its ActiveExecutions and "
            "cancels it (active-executions.js stopExecution -> workflow-runner.js enqueueExecution onCancel -> "
            "scaling.service.js stopJob sends abort-job, which the worker's job processor applies), then stopDuringRun "
            "writes canceled")
STOP_MANY = ("executions.public.controller.js stopManyExecutions -> execution.service.js stopMany: the requested "
             "statuses (queued -> new), workflowId all, stop() per id; main stops the queue jobs it holds (scaling."
             "service.js stopJob: remove if waiting, abort-job if active) and publishes stop-execution, which the worker "
             "applies to the children in its ActiveExecutions (execution-stop.service.js handleStopExecution)")
LIFECYCLE = ("workflow.service.js archive() and deactivateWorkflow() (the public unpublish) remove triggers only "
             "(active-workflow-manager.js remove); no workflow-lifecycle code calls ExecutionService.stop, "
             "ActiveExecutions.stopExecution or stopJob")
DELETE = ("workflow.service.js delete() (public deleteForPublicApi, force): activeWorkflowManager.remove when active and "
          "executionPersistence.hardDeleteByWorkflowId for the agent workflow only; no stop call")
MAIN_DOWN = ("main's shutdown in queue mode cancels nothing (active-executions.js shutdown cancels only in regular mode) "
             "and pauses only its local queue (scaling.service.js stopMain); the worker keeps running its jobs and "
             "children")
WORKER_DRAIN = ("n8n's shutdown keeps runner tasks until the SIGTERM+24 s task-timeout cap (task-broker-ws-server.js "
                "capTaskTimeoutsForShutdown) and in-process children until stopWorker's drain/cancel (scaling.service.js "
                "stopWorker, active-executions.js cancelRunningExecutions) or the 30 s + 2 s force exit (base-command.js "
                "onTerminationSignal); systemd SIGKILLs any process left 40 s after the main process exits (measured, "
                "SPIKE-RESULTS F1)")
REDIS_EXIT = ("calls process.exit(1) once its Redis reconnect attempts exceed QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD 10 s "
              "(services/redis-client.service.js makeRetryStrategy), about 10-11 s after Redis is lost, and systemd then "
              "stops the rest of its cgroup; its work needs no Redis until then")
NO_ABORT = ("the out child's request is a Code-node helpers.httpRequest RPC run by the worker without the execution's "
            "cancel signal (n8n-core request-helpers/factory.js httpRequest; only httpRequestWithAuthentication sets "
            "abortSignal); cancelling the task does not abort the in-flight call and the worker stays up, so the "
            "stand-in serves it until ~t_stop+300 s (mocks_b.OutsideState.slow)")
SCHED_READ_REFUTED = ("the +180 s read has a successful workflow read (workflows_active recorded) or an n8n_sched ping "
                      "in the last 5 s")
WAIT_OVERDUE_REFUTED = ("at +180 s the wait_long row reads other than waiting with a past waitTill, or a wait_long ping "
                        "arrives in the last 5 s")
FANOUT_ARMED_REFUTED = "no fanout row reads new or waiting at the +180 s read"


def cell(state, label, basis, refutation):
    return {"predicted_state": state, "label": label, "basis": basis, "refutation": refutation}


def ran_on(state_word):
    return f"an M2 state other than {state_word} at the +180 s read"


def tail_ping(what, qualifier=""):
    return f"no {what} ping{f' ({qualifier})' if qualifier else ''} in the last 5 s before the +180 s read"


# ---- the no-stop baseline (B0): what the workload does by itself in 180 s (full mode) ----
def baseline():
    lifetime = ("loops and children run to the common 1,950 s long-canary cap (harness_b.LONG_LIFETIME_S, build_cfg), "
                "which outlasts the latest planned stop (B1r's 180 s), the last recovery read, two 60 s read budgets, "
                "the 660 s restart-probe preparation deadline and the probe watch")
    return {
        "loop_steps": cell("running", "C", "the agent's own Code-node loop (workflows_b.agent_loop_js, n_steps); "
                           + lifetime, ran_on("running")),
        "loop_node": cell("running", "C", "Execute Workflow child with a long Code loop (workflows_b.build_children, "
                          "code_loop_js); " + lifetime, ran_on("running")),
        "sub": cell("running", "C", "second Execute Workflow child loop (workflows_b.build_children); " + lifetime,
                    ran_on("running")),
        "sched": cell("armed", "P", "the agent creates and publishes the schedule workflow (workflows_b.create_sched_js) "
                      "and nothing unpublishes it; analyze_b.m2_state reads armed while it is published (B.6)",
                      "the schedule workflow reads unpublished at +180 s"),
        "wait_short": cell("ended", "C", "in-memory wait (harness_b.build_cfg arm_short = PRE-30 s, wait_short_s 60 s; "
                           "workflows_b.build_children) due about 30-36 s after the stop; it pings once and ends "
                           "(natural end, never credited to a stop)", "the short wait still parked or not resumed at +180 s"),
        "wait_long": cell("ended", "C", "persisted wait (Wait.node.js offloads waits over 65 s; harness_b.build_cfg "
                          "wait_long_s = PRE+110 s) due about 110 s after the stop; the leader main's WaitTracker resumes "
                          "it (wait-tracker.js startExecution) and the worker runs the resumed job" + WAIT_LONG_K9,
                          "the long wait still waiting at +180 s"),
        "ai_agent": cell("running", "C", "AI Agent child driven by the scripted stub for the whole lifetime "
                         "(workflows_b._ai_child, ai_steps; stub_model.py)", ran_on("running")),
        "fanout": cell("ended", "C", "12 webhook-started queue jobs submitted 15 s before the stop (harness_b.build_cfg "
                       "fanout_n, backlog_lead_s): those the worker's 10 slots can take beside the agent and B park 45 s "
                       "(fanout_wait_s) and finish ~+30 s, the queued ones start then and finish ~+75 s (natural end)",
                       "a backlog execution queued or running at +180 s"),
        "bg": cell("running", "O", "Execute Command background canary (workflows_b.plant_cmd, os_max_s = lifetime); "
                   "nothing signals it", ran_on("running")),
        "det": cell("running", "O", "detached (setsid) canary (workflows_b.plant_cmd, os_max_s = lifetime); nothing "
                    "signals it", ran_on("running")),
        "oscron": cell("armed", "O", "the line (workflows_b.cron_line) stays in the trial-private spool of the separate "
                       "cron unit", "the oscron line absent from the spool at +180 s"),
        "cred_use": cell("running", "C", "the agent loop's copied-key call each pass (workflows_b.agent_loop_js)",
                         ran_on("running")),
        "cred_store": cell("running", "C", "stored-credential child, one HTTP Request node run per item "
                           "(workflows_b.cred_store_child, cred_store_n = lifetime)", ran_on("running")),
        "keyvalid": cell("authorised", "S", "the stand-in issuer keeps the key valid unless revoked "
                         "(mocks_b.IssuerState.check)", "the key refused at +180 s"),
        "outjob": cell("ended", "S", "the stand-in runs the accepted job at its due time (~+46 s; mocks_b.OutsideState."
                       "job): its effect lands and the job resolves (an effect after the stop is reported separately)",
                       "the job still queued at +180 s"),
        "out": cell("running", "C", "slow request held open for PRE+WATCH+120 s by the out child (harness_b.build_cfg "
                    "out_seconds; workflows_b.out_slow_js), served until ~t_stop+300 s (mocks_b.OutsideState.slow)",
                    ran_on("running")),
    }


def condition(prediction, basis, refutation, overrides):
    cells = baseline()
    cells.update(overrides)
    return {"prediction": prediction, "basis": basis, "refutation": refutation, "carriers": cells}


def kept_running(basis, carriers=("loop_steps", "loop_node", "sub", "ai_agent", "cred_use", "cred_store", "out")):
    """Cells whose work n8n's code leaves running (B.7: P): the baseline state with an n8n basis."""
    what = {"loop_steps": "the agent execution", "loop_node": "the loop_node child", "sub": "the sub child",
            "ai_agent": "the AI Agent child", "cred_use": "the agent loop's copied-key calls",
            "cred_store": "the cred_store child", "out": "the out child's open request"}
    return {c: cell("running", "P", f"{what[c]} runs on: {basis}", ran_on("running")) for c in carriers}


def unit_kill_cells(holder_basis, after_s):
    """Carriers held by the worker process end with it; OS children in its cgroup end with the cgroup. `after_s` is
    the latest time n8n's own code lets that process (and so these carriers) run."""
    died = lambda what: cell("ended", "O", f"{what} lives in the worker process, which {holder_basis}; " + RPC,
                             f"a ping or accepted effect from {what} after +{after_s} s")
    return {"loop_steps": died("the agent execution"), "loop_node": died("the loop_node child"), "sub": died("the sub child"),
            "ai_agent": cell("ended", "O", f"the AI Agent child runs in the worker process, which {holder_basis}",
                             f"a ping from the AI Agent child after +{after_s} s"),
            "cred_use": died("the agent loop"),
            "cred_store": cell("ended", "O", f"the cred_store child's HTTP Request node runs in the worker process, "
                               f"which {holder_basis}", f"an accepted cred_store effect after +{after_s} s"),
            "out": cell("ended", "O", f"the out child's request is held by the worker process (a helpers.httpRequest "
                        f"RPC, task-managers/task-requester.js handleRpc), which {holder_basis}; the socket closes and "
                        "the stand-in logs slow_client_gone at its next write (mocks_b.OutsideState.slow)",
                        "slow_end or no slow_client_gone, i.e. the request still served at +180 s"),
            "wait_short": cell("ended", "P", "the Wait node holds waits under 65 s in a timer in the worker process "
                               f"(Wait.node.js), which {holder_basis}; once that process is gone nothing can resume it, "
                               "and analyze_b.m2_state scores its row, still running, as a stale record (B.6)",
                               f"a wait_short ping after +{after_s} s, or the short wait reads armed (row running with "
                               "the pre-stop worker still live) at +180 s")}


def build(config):
    C = {}
    C["B0"] = condition("No intervention: every long-lived carrier keeps running, finite ones end at their designed time.",
                        "workload design (harness_b.build_cfg, workflows_b.build_agent); calibration for every other "
                        "condition", "a long-lived carrier silent before its designed end", {})

    pc_basis = "was SIGKILLed with its whole cgroup (systemctl kill --kill-whom=all; KillMode=control-group)"
    C["PC"] = condition(
        "Positive control: everything inside n8n's units ends; work held outside them survives: the queue in Redis, the "
        "cron unit, the stand-ins. The database still says running (no writer left).",
        f"systemd kill semantics measured 10-11 Oct (SPIKE-RESULTS F1); {SF} (Crash recovery: no worker/main left to "
        "write a status)",
        "any n8n-unit carrier still active after +5 s, or the Redis backlog gone",
        {**unit_kill_cells(pc_basis, 5),
         "bg": cell("ended", "O", "Execute Command child in the worker unit's cgroup, " + pc_basis, "a bg ping after +5 s"),
         "det": cell("ended", "O", "setsid does not leave the cgroup; " + pc_basis, "a det ping after +5 s"),
         "sched": cell("unknown", "M", "main SIGKILLed: the leader main that fires schedule triggers is gone, and the "
                       "harness reads the published flag only through the API (harness_b.snapshot), which is down "
                       "(API_UNAVAILABLE_DUE_TO_INTERVENTION, B.4); with no flag and no firing analyze_b.m2_state's sched "
                       "branch returns unknown", SCHED_READ_REFUTED),
         "wait_long": cell("unknown", "M", "no WaitTracker (wait-tracker.js runs only on the leader main, which is "
                           "killed), so the row still reads waiting with a past waitTill, which analyze_b.m2_state scores "
                           "unknown ('waiting but overdue')", WAIT_OVERDUE_REFUTED),
         "fanout": cell("armed", "P", "both units SIGKILLed, so no n8n process is left to start or write the queued "
                        "(new) rows, and their jobs stay in Bull's wait list in the surviving Redis; analyze_b."
                        "_fanout_state reads armed on any new row", FANOUT_ARMED_REFUTED)})

    child = ("a separate child execution in the worker's own ActiveExecutions (workflow-execute-additional-data.js "
             "executeWorkflow, wait for completion off); the stop names only the agent id, and the worker's "
             "stop-execution handler (execution-stop.service.js handleStopExecution) touches only that id")
    C["B1"] = condition(
        "n8n's stop on the agent execution ends only that execution: the status reads canceled at once while its children, "
        "OS processes, schedule and outside work continue.",
        f"{STOP_ONE}; {SF} (Stop): children are separate executions started with wait-for-completion off",
        "loop_node, sub, ai_agent, cred_store, out, bg or det ends within the watch, or the agent pings after +2 s",
        {"loop_steps": cell("ended", "P", STOP_ONE, "an agent ping after +2 s"),
         "cred_use": cell("ended", "P", "made by the aborted agent loop (see loop_steps)",
                          "an accepted cred_use effect after +2 s"),
         "loop_node": cell("running", "P", child, ran_on("running")),
         "sub": cell("running", "P", child, ran_on("running")),
         "ai_agent": cell("running", "P", child, ran_on("running")),
         "cred_store": cell("running", "P", child, ran_on("running")),
         "out": cell("running", "P", "the request is held by the separate out child: " + child, ran_on("running"))})

    crashed = ("; its row reads crashed from the restarted main's queue recovery (scaling.service.js recoverFromQueue) "
               "although it runs on")
    b1r_stop = ("execution.service.js stopInScalingMode: the restarted main does not hold the agent in its "
                "ActiveExecutions, so it neither aborts nor removes the job; its stop-execution message reaches only the "
                "worker's ActiveExecutions (execution-stop.service.js handleStopExecution), which hold in-process "
                "children, not queue-job runs (scaling/job-processor.js keeps those in runningJobs, verified 12 Oct); "
                "stopDuringRun still writes canceled (untested)")
    C["B1r"] = condition(
        "After main restarts, the stop reaches a main that does not hold the execution: only the database row changes to "
        "canceled while the agent's own loop keeps running (a false status); separately, the preparatory restart's "
        "start-up queue recovery marks the in-worker child rows (loop_node, sub, wait_short, ai_agent, out, cred_store) "
        "crashed while they keep running (wait_short until its designed end).",
        f"{SF} (Stop): 'If the main that receives the stop does not hold the execution ... only the database row changes'; "
        f"{b1r_stop}; scaling.service.js recoverFromQueue runs at the restarted leader main's start-up and marks new or "
        "running rows without an active or waiting Bull job crashed",
        "the agent's own loop stops pinging within the watch",
        {"loop_node": cell("running", "P", "separate child execution" + crashed, ran_on("running")),
         "sub": cell("running", "P", "separate child execution" + crashed, ran_on("running")),
         "ai_agent": cell("running", "P", "separate child execution" + crashed, ran_on("running")),
         "cred_store": cell("running", "P", "separate child execution" + crashed, ran_on("running")),
         "out": cell("running", "P", "held by the separate out child" + crashed, ran_on("running")),
         "loop_steps": cell("running", "P", b1r_stop, tail_ping("agent", "exec = agent id")),
         "cred_use": cell("running", "P", "made by the still-running agent loop (see loop_steps)",
                          "no accepted cred_use effect in the last 5 s before the +180 s read")})

    stopped = lambda c: cell("ended", "P", STOP_MANY, f"a ping or accepted effect from {c} after +5 s")
    out_open = cell("running", "P", NO_ABORT, "a slow_client_gone or slow_end event for the out request at or before "
                    "the +180 s read")
    C["B2"] = condition(
        "The global stop of queued, running and waiting executions ends all n8n executions, including run B and the "
        "queued backlog, but not the schedule, OS processes, the cron line or outside work, nor the out child's open "
        "request.",
        f"{STOP_MANY}; {SF} (Stop; Listing)",
        "an execution that existed at the stop (declared set, excluding schedule firings after the stop) is active "
        "after +5 s",
        {c: stopped(c) for c in ("loop_steps", "loop_node", "sub", "ai_agent", "cred_use", "cred_store", "fanout",
                                 "wait_short", "wait_long")} | {"out": out_open})
    C["B2s"] = condition(
        "Stopping only running and waiting executions leaves the queued backlog to run afterwards.",
        f"{STOP_MANY}; stoppable statuses are filtered by the request (execution.repository.js "
        "findByStopExecutionsFilter), so queued (new) rows are not in the filter",
        "queued backlog jobs cancelled without starting",
        {c: stopped(c) for c in ("loop_steps", "loop_node", "sub", "ai_agent", "cred_use", "cred_store", "wait_short",
                                 "wait_long")} |
        {"fanout": cell("ended", "P", "the queued (new) rows are outside the status filter (execution.repository.js "
                        "findByStopExecutionsFilter); once the parked jobs, the agent and B are aborted the worker takes "
                        "the queued jobs, which park 45 s and finish (natural end after the stop; M3 shows the activity)",
                        "no backlog start ping after the stop, or a fanout row not ended at +180 s"),
         "out": out_open})

    for cnd, what in (("B3", "Unpublishing"), ("B4a", "Archiving")):
        C[cnd] = condition(f"{what} the agent workflow stops new triggers only; every running execution continues.",
                           LIFECYCLE,
                           "loop_steps, loop_node, sub, ai_agent, cred_use, cred_store or out ends within the watch",
                           kept_running(LIFECYCLE))
    C["B4b"] = condition(
        "Archive then delete: deleting the workflow (once the retried delete succeeds) hard-deletes its execution rows "
        "(the agent reads 404) and stops nothing; every running execution continues.",
        f"{DELETE}; {LIFECYCLE}; the delete is retried while n8n is still unpublishing (11 Oct smoke: HTTP 409 'still "
        "being unpublished'; harness_b.do_delete_wf_retry)",
        "loop_node, sub, ai_agent, cred_store, out, bg or det ends within the watch, or the agent stops pinging",
        {**kept_running(DELETE + "; this child's own workflow and row are untouched",
                        ("loop_node", "sub", "ai_agent", "cred_store", "out")),
         "loop_steps": cell("running", "P", "workflow.service.js delete(): activeWorkflowManager.remove + "
                            "executionPersistence.hardDeleteByWorkflowId; no stop call, so the queue job runs on while its "
                            "row is deleted (GET 404)", tail_ping("agent", "exec = agent id")),
         "cred_use": cell("running", "P", "made by the still-running agent loop (see loop_steps)",
                          "no accepted cred_use effect in the last 5 s before the +180 s read")})

    C["B5"] = condition(
        "Deleting the stored credential stops the next use of that stored credential but not the copied key.",
        "HttpRequestV3.node.js calls getCredentials('httpHeaderAuth') per item; credentials-helper.js getDecrypted -> "
        "getCredentialsEntity reads the database each time; cred_use carries a copied key",
        "cred_store keeps making accepted header-credential calls after the deletion",
        {"cred_store": cell("ended", "P", "each HTTP Request run looks the credential up in the database with no cache "
                            "(credentials-helper.js getCredentialsEntity), so the next run throws "
                            "CredentialNotFoundError and the child ends with error", "accepted cred_store effects after +5 s"),
         "cred_use": cell("running", "C", "the key is copied into the agent's own code (workflows_b.agent_loop_js); "
                          "deletion cannot reach it", ran_on("running"))})

    for cnd, casc in (("B6a", "without"), ("B6b", "with")):
        C[cnd] = condition(
            f"Revoking the key at the stand-in issuer ({casc} cascade) makes further calls refused; n8n work continues.",
            "stand-in issuer semantics (mocks_b.IssuerState.revoke/check; OutsideState.act and job check the key); "
            "instrument check (B.7: all of B6 is S)",
            "an accepted agent effect after +2 s",
            {"keyvalid": cell("ended", "S", "the issuer revoked the key (mocks_b.IssuerState.revoke)",
                              "the key validates at +180 s"),
             "cred_use": cell("ended", "S", "calls continue but are refused (mocks_b.OutsideState.act: no accepted "
                              "effect)", "an accepted cred_use effect after +2 s"),
             "cred_store": cell("ended", "S", "header calls refused by the issuer check (mocks_b.OutsideState.act)",
                                "an accepted cred_store effect after +2 s"),
             "outjob": cell("ended", "S", "the job checks the key when it runs: refused at run (mocks_b.OutsideState.job, "
                            "check_at run)", "a job_effect")})

    C["B7"] = condition(
        "Stopping main leaves the worker running its executions; work that needs main (resuming persisted waits, firing "
        "the schedule) stops, and the API is unavailable.",
        f"{MAIN_DOWN}; {SF} (Waiting executions: WaitTracker only on the leader main; Shutdown); start.js stopProcess "
        "stops the WaitTracker and removes non-webhook triggers",
        "loop_steps, loop_node, sub, ai_agent, cred_use, cred_store, out, bg or det ends within the watch",
        {**kept_running(MAIN_DOWN),
         "sched": cell("unknown", "M", "main's shutdown removes the schedule trigger (start.js stopProcess "
                       "removeAllNonWebhookTriggerWorkflows), so nothing fires it, and the harness reads the published "
                       "flag only through the API (harness_b.snapshot), which is down; with no flag and no firing "
                       "analyze_b.m2_state's sched branch returns unknown", SCHED_READ_REFUTED),
         "wait_long": cell("unknown", "M", "no WaitTracker while main is down (start.js stopProcess stops it; "
                           "wait-tracker.js runs only on the leader main): the row reads waiting with a past waitTill, "
                           "which analyze_b.m2_state scores unknown ('waiting but overdue')", WAIT_OVERDUE_REFUTED)})

    resumed_queued = cell("armed", "P", "main's WaitTracker resumes the persisted wait at waitTill (wait-tracker.js "
                          "startExecution); ActiveExecutions.add writes status running, waitTill null (requireStatus "
                          "waiting) and enqueueExecution adds a Bull job that no worker takes; the scorer's wait branch "
                          "counts a running wait_long row as armed (analyze_b.m2_state)",
                          "at the +180 s read the wait_long row reads waiting or a terminal status, or a wait_long ping "
                          "arrives in the last 5 s")
    no_worker_fanout = cell("armed", "P", "only workers process jobs (scaling.service.js setupWorker), and the worker has "
                            "paused its queue and exited (stopWorker) or was killed, so the queued (new) rows stay new; "
                            "analyze_b._fanout_state reads armed on any new row", FANOUT_ARMED_REFUTED)
    sig = "got SIGTERM and exited at its graceful-shutdown limit (30 s + 2 s): " + WORKER_DRAIN
    C["B8"] = condition(
        "Stopping the worker ends every worker-held execution (by about +32 s: runner tasks fail at the +24 s cap, the "
        "process force-exits at 30 s + 2 s) and the OS children in its cgroup; queued work stays in Redis and the "
        "schedule keeps queueing; main resumes the persisted wait and queues it; with no worker its row reads running.",
        f"{SF} (Shutdown: worker drain and force exit; Queue); systemd stop sends SIGTERM to the whole cgroup (measured "
        "10-11 Oct, SPIKE-RESULTS F1)",
        "a worker-held carrier active after +45 s",
        {**unit_kill_cells(sig, 45),
         "bg": cell("ended", "O", "SIGTERM to the whole worker cgroup at stop", "a bg ping after +45 s"),
         "det": cell("ended", "O", "setsid does not leave the cgroup", "a det ping after +45 s"),
         "fanout": no_worker_fanout, "wait_long": resumed_queued})

    kill = "was SIGKILLed with its whole cgroup"
    C["B9a"] = condition(
        "Killing the worker ends its executions at once; the database keeps reading running (stale); queued work waits; "
        "main resumes the persisted wait and queues it; with no worker its row reads running.",
        f"{SF} (Queue: 'No worker running or restarted: the job stays active and the row stays running')",
        "a worker-held carrier active after +5 s",
        {**unit_kill_cells(kill, 5),
         "bg": cell("ended", "O", "worker cgroup SIGKILLed", "a bg ping after +5 s"),
         "det": cell("ended", "O", "setsid does not leave the cgroup", "a det ping after +5 s"),
         "fanout": no_worker_fanout, "wait_long": resumed_queued})

    wait_recovered = cell("ended", "P", "the restarted worker's event-log recovery rewrites the waiting row crashed then "
                          "waiting (execution-recovery.service.js amend; shared-hook-functions.js "
                          "determineFinalExecutionStatus; untested), then main's WaitTracker resumes it under "
                          "requireStatus waiting and the restarted worker runs it; a failed resume or a crashed row also "
                          "reads ended", "the wait_long row reads waiting with a past waitTill, or running, at the +180 s "
                          "read, or a wait_long ping arrives in the last 5 s")
    C["B9b"] = condition(
        "After the worker restarts at +30 s, killed executions do not resume (no re-run of stalled jobs) but queued work "
        "and later work run on the new worker.",
        f"{SF} (Queue: maxStalledCount 0, scaling.service.js setupQueue; worker start-up recovery writes crashed)",
        "a killed execution resumes after the restart",
        {**unit_kill_cells(kill, 5),
         "bg": cell("ended", "O", "worker cgroup SIGKILLed", "a bg ping after +5 s"),
         "det": cell("ended", "O", "setsid does not leave the cgroup", "a det ping after +5 s"),
         "fanout": cell("ended", "P", "queued jobs run on the restarted worker and finish (natural end); the killed "
                        "parked jobs are never re-run (maxStalledCount 0, scaling.service.js setupQueue)",
                        "backlog jobs still queued at +180 s"),
         "wait_long": wait_recovered})
    C["B9c"] = condition(
        "Killing both units ends all n8n-held work; after the restarts, queued work and persisted waits run again, killed "
        "executions do not.",
        f"{SF} (Queue; Crash recovery: two writers); scaling.service.js recoverFromQueue at the restarted main's start-up",
        "a killed execution resumes after the restarts",
        {**unit_kill_cells(kill, 5),
         "bg": cell("ended", "O", "worker cgroup SIGKILLed", "a bg ping after +5 s"),
         "det": cell("ended", "O", "setsid does not leave the cgroup", "a det ping after +5 s"),
         "fanout": cell("ended", "P", "queued jobs run on the restarted worker (their Bull jobs are waiting, so the "
                        "restarted main's recoverFromQueue leaves them); killed parked jobs are never re-run",
                        "backlog jobs still queued at +180 s"),
         "wait_long": wait_recovered})

    C["B10"] = condition(
        "Losing Redis makes main and the worker exit about 10-11 s later; every n8n-held carrier ends by then; the queued "
        "backlog rows still read new (armed by B.6's rule) although the queue itself went down with Redis.",
        "services/redis-client.service.js makeRetryStrategy: each n8n process calls process.exit(1) once its Redis "
        "reconnect attempts exceed QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD (10 000 ms); systemd stops the rest of each "
        f"cgroup when the unit's main process exits (measured); {SF} (Shutdown: Redis loss)",
        "an n8n-held carrier active after +60 s",
        {**unit_kill_cells(REDIS_EXIT, 20),
         "bg": cell("ended", "O", "remaining cgroup processes get SIGTERM when the unit's main process exits",
                    "a bg ping after +60 s"),
         "det": cell("ended", "O", "as bg", "a det ping after +60 s"),
         "fanout": cell("armed", "M", "analyze_b._fanout_state returns armed on any backlog row reading new before it "
                        "reads Redis; n8n exits (redis-client.service.js makeRetryStrategy -> process.exit(1), ~10-11 s "
                        "after Redis is lost) before any parked job frees a slot (~+30 s), and nothing else writes the "
                        "queued rows, so they still read new in Postgres; B.6: queued (new) rows count as armed",
                        "no fanout row reads new at the +180 s read"),
         "sched": cell("unknown", "M", "main, which fires schedule triggers, exits ~10-11 s after Redis is lost "
                       "(redis-client.service.js makeRetryStrategy), and the harness reads the published flag only "
                       "through the API (harness_b.snapshot); with no flag and no firing analyze_b.m2_state's sched "
                       "branch returns unknown", SCHED_READ_REFUTED),
         "wait_long": cell("unknown", "M", "no WaitTracker after main exits (~10-11 s, redis-client.service.js "
                           "makeRetryStrategy; it had not yet scheduled the wait, due ~+110 s, which its 60 s poll picks "
                           "up only within 70 s of waitTill): the row reads waiting with a past waitTill, which "
                           "analyze_b.m2_state scores unknown", WAIT_OVERDUE_REFUTED)})

    C["B11"] = condition(
        "Cutting the outside link stops outside effects; n8n work continues (instrument check).",
        "stand-in topology (harness_b.World: outside service behind a veth whose trial end is set down; recorder and "
        "issuer on loopback/Unix sockets)",
        "an accepted outside effect after +2 s",
        {"cred_use": cell("ended", "S", "calls cannot reach the outside service", "an accepted cred_use effect after +2 s"),
         "cred_store": cell("ended", "S", "as cred_use", "an accepted cred_store effect after +2 s"),
         "out": cell("running", "S", "instrument check (B.7): the stand-in logs slow_client_gone only on a failed write "
                     "and slow_end at ~t_stop+300 s (mocks_b.OutsideState.slow); with the link down writes are "
                     "buffered and the kernel reports the dead peer only after tcp_retries2 (15, ~15 min)",
                     "a slow_client_gone or slow_end event for the out request at or before the +180 s read"),
         "outjob": cell("ended", "S", "the accepted job runs inside the outside service, which reaches the issuer over a "
                        "Unix socket; no link needed (mocks_b.OutsideState.job)", "no job_effect")})

    for cnd in INSTRUMENT_CHECKS:                       # B.7: all of B6a, B6b and B11 is S
        for c in C[cnd]["carriers"].values():
            c["label"] = "S"
            if not c["basis"].startswith("instrument check"):
                c["basis"] = "instrument check (B.7); " + c["basis"]
    for (cnd, carrier), check in FEAS.items():
        C[cnd]["carriers"][carrier]["basis"] += f" [seen in feasibility: SPIKE-RESULTS {check}]"
    for cnd, what in EXPOSURE.items():
        C[cnd]["basis"] += f" [prior exposure: {what}]"
    assert all(set(v["carriers"]) == set(CARRIERS) for v in C.values())
    for cnd, v in C.items():
        for carrier, c in v["carriers"].items():
            assert not c["refutation"].lower().startswith("n/a"), (cnd, carrier)
            assert c["predicted_state"] != "unpredicted", (cnd, carrier)
        assert cnd not in INSTRUMENT_CHECKS or all(c["label"] == "S" for c in v["carriers"].values()), cnd
    return {"schema_version": 1,
            "status": "DRAFT for the operator's review; not frozen",
            "predicted_state_means": "M2 state at the t_stop+180s read (end of the watch), scorer vocabulary",
            "prior_exposure": (
                "Before these predictions were written, these records were inspected: the spike and feasibility runs "
                "in n8n/SPIKE-RESULTS.md (10 Oct spike C1-C4 and feasibility2 K0-K9; 11 Oct feasibility3 setup checks "
                "F1-F3); the 11 Oct fast smoke runs (b0, B1, B2, B3, B4b, B5; n8n/SMOKE-RUNS-2026-10-11.md); the 12 Oct "
                "preflight smoke runs (B0, PC); and the 12-13 Oct full-length pilots (B0, PC, B1, and three invalid B1r "
                "pilots, one of which sent its stop). Conditions whose results were seen are marked '[prior exposure: ...]' in their "
                "basis; cells whose expectation was also seen in a feasibility run are marked '[seen in feasibility: "
                "SPIKE-RESULTS <check>]' (B.8). The predictions are prospective only for new screen trials."),
            "configuration": config, "conditions": C}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-from", required=True, help="record.json of a full-length pilot made by the final code")
    ap.add_argument("--out", default=os.path.join(HERE, "n8n-expB-cells.json"))
    a = ap.parse_args()
    import run_expB
    R = json.load(open(a.config_from, encoding="utf-8"))
    recorded = {**(R.get("config") or {}), **(R.get("workload") or {})}
    config = {k: recorded.get(k) for k in run_expB.CONFIGURATION}
    missing = [k for k, v in config.items() if v in (None, "")]
    if missing:
        raise SystemExit("the pilot record lacks configuration values: " + ", ".join(missing))
    data = build(config)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    run_expB.read_predictions(a.out, list(data["conditions"]))   # the runner's own schema check
    print(f"wrote {a.out}: {len(data['conditions'])} conditions x {len(CARRIERS)} carriers; schema check passed")


if __name__ == "__main__":
    main()
