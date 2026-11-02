# Temporal: the real lines around two cancels

These are two of the Temporal trials of 14 October 2026 (records cs1014), run on a Linux virtual machine, one trial
for each condition. The platform is Temporal's development server (Temporal CLI 1.9.1, Server 1.32.0,
`temporal server start-dev` with SQLite), with workers built on the Python SDK (temporalio 1.34.0). A *workflow
run* is Temporal's record of one job; the job's code runs on a *worker*, and its slow part runs as an
*activity*, a unit of work that Temporal hands to a worker and lists as *pending* until it closes. In each trial
the work activity posts 20 progress steps, one a second, and then a final effect, each an HTTP POST to our
stand-in receiver, which writes down its arrival. The stop falls 8.5 s into the activity, between steps 9 and
10. The two conditions differ only in the activity. In C3 (run as C3-a2, the valid rerun of C3) it never sends a
*heartbeat*, a report from the activity to the server that it is alive; in C2 it sends one after every step,
which the SDK passes on to the server at most once every 4 s. In both, the stop is the operator's cancel, the CLI
command `temporal workflow cancel`.

After the stop the harness asks Temporal through the CLI: `describe` polls until the run reads closed, then
`describe` at +0, +2, +5, +10, +20, +30, +60, +90 and +120 s gives the run's status and its pending activities.
`show` gives the run's *history*, its ordered list of events, of which the block shows those from the work
activity's scheduling on.

Each block below is printed by `experiments/scripts/walkthrough_temporal.py` from the trial's raw folder. Each line
under a time is
copied, not retyped, and names its file and line number, or for the harness's record (`record.json`) the field,
so it can be opened and checked; the paragraph above each block is our reading of it. Times are seconds after the
stop was sent, on the VM's monotonic clock. The cancel command returned 0.063 s after that. History times and
the SDK's log times are wall-clock times, placed with the receiver's paired stamps (the VM's two clocks agreed to
within 0.1 ms across each trial). For the receiver's and the worker's lines only the fields that matter are shown;
the line number leads to the full line.

The raw folders are in `experiments/temporal-activities/records/cs1014/` (one per trial, named in each block), and
[What each stop reached](../WHAT-EACH-STOP-REACHES.md) covers all 16 conditions. This page is the script's
output, made from the top of the repository with:

```bash
python3 experiments/scripts/walkthrough_temporal.py experiments/temporal-activities/records/cs1014 C3-a2 C2 \
    > experiments/docs/walkthroughs/TEMPORAL.md
```

## C3-a2: cancel during the non-heartbeating activity (C3, valid rerun)

Raw folder `XC-screen-c-screen-1014-C3-a2-20261014-040816`. Stop sent at 2026-10-14 04:09:00.098 UTC. Counted from receiver.jsonl: after the stop and within the 120 s watch, 12 effects of the target's activity arrived, the last at +11.500 s (step 20, final).

What to look at. The cancel command returns at +0.063 s. Temporal's history records the cancel request at +0.046 s (event 17), asks the activity to cancel (event 21) and closes the run CANCELED (event 22) at +0.104 s. The harness's first describe answers `CANCELED` at +0.192 s, and every describe to the end of the watch lists the work activity as pending, in state `CANCEL_REQUESTED`. The activity, which sends no heartbeat, hears nothing of this: steps 10 to 20 reach the receiver from +0.500 s to +10.501 s, and the final effect at +11.500 s, all from the execution that started at -8.50 s (`exec_nonce` b0623e9245d60aff). The worker records the activity as done at +11.501 s (`activity_done`, phase `posting final`), and when the SDK reports the completion, the server answers that the run is gone: `Activity not found on completion ... workflow execution already completed` (worker-target-g1.out:1, +11.504 s). Temporal's history has no ActivityTaskStarted event for this execution: after event 16, `ACTIVITY_TASK_SCHEDULED`, come only the cancel's events. The terminate condition, C5n, gives the same picture.

```text
   -8.515 s  [Temporal's history, event 16; record.json reads[+120].parts.show_target]
            {"eventId": "16", "eventTime": "2026-10-14T04:08:51.583386973Z", "eventType": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"}
   -8.502 s  [target worker, worker-target-g1.jsonl:2]
            {"event": "activity_start", "smono": 19653.792237439, "exec_nonce": "b0623e9245d60aff", "heartbeat": false, "activity_type": "work_nh",
              "activity_id": "work", "attempt": 1, "is_local": false}
   -0.500 s  [receiver, receiver.jsonl:63]
            {"seq": 63, "mono": 19661.793880555, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 9, "kind": "progress"}
   +0.000 s  [harness runs the stop, record.json intervention.actions[0]]
            {"name": "cancel", "argv": ["workflow", "cancel", "-w", "expc-xc-screen-c-screen-1014-c3-a2-target", "-r", "01a13898-d082-7f2f-9923-c00becdc9f72"], "t_send": 19662.293978203}
   +0.046 s  [Temporal's history, event 17; record.json reads[+120].parts.show_target]
            {"eventId": "17", "eventTime": "2026-10-14T04:09:00.145085373Z", "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED"}
   +0.046 s  [Temporal's history, event 18; record.json reads[+120].parts.show_target]
            {"eventId": "18", "eventTime": "2026-10-14T04:09:00.145123618Z", "eventType": "EVENT_TYPE_WORKFLOW_TASK_SCHEDULED"}
   +0.059 s  [Temporal's history, event 19; record.json reads[+120].parts.show_target]
            {"eventId": "19", "eventTime": "2026-10-14T04:09:00.158130219Z", "eventType": "EVENT_TYPE_WORKFLOW_TASK_STARTED"}
   +0.063 s  [the CLI command returns, record.json intervention.actions[0]]
            {"rc": 0, "t_return": 19662.356872368, "stdout": "", "stderr": ""}
   +0.104 s  [Temporal's history, event 20; record.json reads[+120].parts.show_target]
            {"eventId": "20", "eventTime": "2026-10-14T04:09:00.202870681Z", "eventType": "EVENT_TYPE_WORKFLOW_TASK_COMPLETED"}
   +0.104 s  [Temporal's history, event 21; record.json reads[+120].parts.show_target]
            {"eventId": "21", "eventTime": "2026-10-14T04:09:00.202960052Z", "eventType": "EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED"}
   +0.104 s  [Temporal's history, event 22; record.json reads[+120].parts.show_target]
            {"eventId": "22", "eventTime": "2026-10-14T04:09:00.203014065Z", "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED"}
   +0.183 s  [harness's describe at +0 s, answer arrived; record.json reads[+0]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
   +0.192 s  [harness's describe poll, answer arrived; record.json claim_poll[0] (poll-1)]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
   +0.500 s  [receiver, receiver.jsonl:66]
            {"seq": 66, "mono": 19662.79385609, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 10, "kind": "progress"}
   +1.500 s  [receiver, receiver.jsonl:69]
            {"seq": 69, "mono": 19663.794156889, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 11, "kind": "progress"}
   +2.111 s  [harness's describe at +2 s, answer arrived; record.json reads[+2]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
   +2.500 s  [receiver, receiver.jsonl:72]
            {"seq": 72, "mono": 19664.794319957, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 12, "kind": "progress"}
   +3.501 s  [receiver, receiver.jsonl:75]
            {"seq": 75, "mono": 19665.794524609, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 13, "kind": "progress"}
   +4.501 s  [receiver, receiver.jsonl:78]
            {"seq": 78, "mono": 19666.794711151, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 14, "kind": "progress"}
   +5.113 s  [harness's describe at +5 s, answer arrived; record.json reads[+5]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
   +5.501 s  [receiver, receiver.jsonl:81]
            {"seq": 81, "mono": 19667.794991815, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 15, "kind": "progress"}
   +6.500 s  [receiver, receiver.jsonl:84]
            {"seq": 84, "mono": 19668.794354013, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 16, "kind": "progress"}
   +7.500 s  [receiver, receiver.jsonl:87]
            {"seq": 87, "mono": 19669.794448477, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 17, "kind": "progress"}
   +8.501 s  [receiver, receiver.jsonl:90]
            {"seq": 90, "mono": 19670.794512768, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 18, "kind": "progress"}
   +9.501 s  [receiver, receiver.jsonl:93]
            {"seq": 93, "mono": 19671.794774091, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 19, "kind": "progress"}
  +10.107 s  [harness's describe at +10 s, answer arrived; record.json reads[+10]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
  +10.501 s  [receiver, receiver.jsonl:96]
            {"seq": 96, "mono": 19672.794930537, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 20, "kind": "progress"}
  +11.500 s  [receiver, receiver.jsonl:99]
            {"seq": 99, "mono": 19673.794106359, "role": "target", "attempt": 1, "exec_nonce": "b0623e9245d60aff", "step": 20, "kind": "final"}
  +11.501 s  [target worker, worker-target-g1.jsonl:3]
            {"event": "activity_done", "smono": 19673.794544533, "exec_nonce": "b0623e9245d60aff", "phase": "posting final", "inflight": null,
              "activity_type": "work_nh", "activity_id": "work", "attempt": 1, "is_local": false}
  +11.504 s  [the SDK's log, worker-target-g1.out:1]
            2026-10-14T04:09:11.602479Z  WARN temporalio_sdk_core::worker::activities: Activity not found on completion. This may happen if the
              activity has already been cancelled but completed anyway.
              task_token=TaskToken(CiQwMWExMzg5OC1jODcyLTc5NWMtYTEzOC0yNGYzMGUxN2M2N2QSKWV4cGMteGMtc2NyZWVuLWMtc2NyZWVuLTEwMTQtYzMtYTItdGFyZ2V0GiQwMWExMzg5OC1kMDgyLTdmMmYtOTkyMy1jMDBiZWNkYzlmNzIgECgBMgR3b3JrQgd3b3JrX25oSggIARCigUAYAQ==)
              details=Status { code: NotFound, message: "workflow execution already completed", details: b"\x08\x05\x12$workflow execution already
              completed\x1aB\n@type.googleapis.com/temporal.api.errordetails.v1.NotFoundFailure", metadata: MetadataMap { headers: {"content-type":
              "application/grpc"} }, source: None }
  +20.300 s  [harness's describe at +20 s, answer arrived; record.json reads[+20]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
  +30.107 s  [harness's describe at +30 s, answer arrived; record.json reads[+30]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
  +60.107 s  [harness's describe at +60 s, answer arrived; record.json reads[+60]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
  +90.122 s  [harness's describe at +90 s, answer arrived; record.json reads[+90]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
 +120.118 s  [harness's describe at +120 s, answer arrived; record.json reads[+120]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c3-a2-g1"}]
```

## C2: cancel during the heartbeating activity (C2)

Raw folder `XC-screen-c-screen-1014-C2-20261014-031626`. Stop sent at 2026-10-14 03:17:11.387 UTC. Counted from receiver.jsonl: after the stop and within the 120 s watch, 4 effects of the target's activity arrived, the last at +3.500 s (step 13, progress).

What to look at. The same cancel, with the activity sending a heartbeat after every step. Temporal closes the run just as fast: event 22, `CANCELED`, at +0.091 s, and the first describe answers `CANCELED` at +0.190 s. The activity posts steps 10 to 13 (+0.499 s to +3.500 s) and learns of the cancel only at +3.505 s, when the SDK passes on its next heartbeat: the worker records `activity_cancelled` after step 13, with the flag `not_found` (the server no longer knew the activity) and the phase `sleeping after 13`. Nothing more reaches the receiver: no step 14 and no final effect. The SDK logs `Completing activity as failed` and a `CancelledError` (worker-target-g1.out, lines 1 and 20, printed without a time). Every describe still lists the activity as pending, `CANCEL_REQUESTED`, to the end of the watch, the same as in C3-a2, where the activity ran to its end: Temporal's record does not show which of the two happened.

```text
   -8.516 s  [Temporal's history, event 16; record.json reads[+120].parts.show_target]
            {"eventId": "16", "eventTime": "2026-10-14T03:17:02.871699440Z", "eventType": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"}
   -8.502 s  [target worker, worker-target-g1.jsonl:2]
            {"event": "activity_start", "smono": 16545.080376941, "exec_nonce": "a4c4f39869ee4d79", "heartbeat": true, "activity_type": "work_hb",
              "activity_id": "work", "attempt": 1, "is_local": false}
   -0.501 s  [receiver, receiver.jsonl:64]
            {"seq": 64, "mono": 16553.08176132, "role": "target", "attempt": 1, "exec_nonce": "a4c4f39869ee4d79", "step": 9, "kind": "progress"}
   +0.000 s  [harness runs the stop, record.json intervention.actions[0]]
            {"name": "cancel", "argv": ["workflow", "cancel", "-w", "expc-xc-screen-c-screen-1014-c2-target", "-r", "01a13869-610b-773e-a14d-715c0457f781"], "t_send": 16553.582542677}
   +0.046 s  [Temporal's history, event 17; record.json reads[+120].parts.show_target]
            {"eventId": "17", "eventTime": "2026-10-14T03:17:11.433032888Z", "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED"}
   +0.046 s  [Temporal's history, event 18; record.json reads[+120].parts.show_target]
            {"eventId": "18", "eventTime": "2026-10-14T03:17:11.433068914Z", "eventType": "EVENT_TYPE_WORKFLOW_TASK_SCHEDULED"}
   +0.059 s  [Temporal's history, event 19; record.json reads[+120].parts.show_target]
            {"eventId": "19", "eventTime": "2026-10-14T03:17:11.446133294Z", "eventType": "EVENT_TYPE_WORKFLOW_TASK_STARTED"}
   +0.062 s  [the CLI command returns, record.json intervention.actions[0]]
            {"rc": 0, "t_return": 16553.644059639, "stdout": "", "stderr": ""}
   +0.090 s  [Temporal's history, event 20; record.json reads[+120].parts.show_target]
            {"eventId": "20", "eventTime": "2026-10-14T03:17:11.477587471Z", "eventType": "EVENT_TYPE_WORKFLOW_TASK_COMPLETED"}
   +0.090 s  [Temporal's history, event 21; record.json reads[+120].parts.show_target]
            {"eventId": "21", "eventTime": "2026-10-14T03:17:11.477663771Z", "eventType": "EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED"}
   +0.091 s  [Temporal's history, event 22; record.json reads[+120].parts.show_target]
            {"eventId": "22", "eventTime": "2026-10-14T03:17:11.477752315Z", "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED"}
   +0.190 s  [harness's describe poll, answer arrived; record.json claim_poll[0] (poll-1)]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
   +0.191 s  [harness's describe at +0 s, answer arrived; record.json reads[+0]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
   +0.499 s  [receiver, receiver.jsonl:67]
            {"seq": 67, "mono": 16554.081626056, "role": "target", "attempt": 1, "exec_nonce": "a4c4f39869ee4d79", "step": 10, "kind": "progress"}
   +1.500 s  [receiver, receiver.jsonl:70]
            {"seq": 70, "mono": 16555.082215239, "role": "target", "attempt": 1, "exec_nonce": "a4c4f39869ee4d79", "step": 11, "kind": "progress"}
   +2.100 s  [harness's describe at +2 s, answer arrived; record.json reads[+2]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
   +2.499 s  [receiver, receiver.jsonl:73]
            {"seq": 73, "mono": 16556.081699367, "role": "target", "attempt": 1, "exec_nonce": "a4c4f39869ee4d79", "step": 12, "kind": "progress"}
   +3.500 s  [receiver, receiver.jsonl:76]
            {"seq": 76, "mono": 16557.08290591, "role": "target", "attempt": 1, "exec_nonce": "a4c4f39869ee4d79", "step": 13, "kind": "progress"}
   +3.505 s  [target worker, worker-target-g1.jsonl:3]
            {"event": "activity_cancelled", "smono": 16557.087686687, "exec_nonce": "a4c4f39869ee4d79", "last_step": 13, "flags": {"not_found": true,
              "cancel_requested": false, "paused": false, "reset": false, "timed_out": false, "worker_shutdown": false}, "phase": "sleeping after 13",
              "inflight": null, "activity_type": "work_hb", "activity_id": "work", "attempt": 1, "is_local": false}
   +5.103 s  [harness's describe at +5 s, answer arrived; record.json reads[+5]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
  +10.119 s  [harness's describe at +10 s, answer arrived; record.json reads[+10]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
  +20.112 s  [harness's describe at +20 s, answer arrived; record.json reads[+20]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
  +30.127 s  [harness's describe at +30 s, answer arrived; record.json reads[+30]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
  +60.119 s  [harness's describe at +60 s, answer arrived; record.json reads[+60]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
  +90.109 s  [harness's describe at +90 s, answer arrived; record.json reads[+90]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
 +120.107 s  [harness's describe at +120 s, answer arrived; record.json reads[+120]]
            workflowExecutionInfo.status: "WORKFLOW_EXECUTION_STATUS_CANCELED"
            pendingActivities: [{"activityId": "work", "state": "PENDING_ACTIVITY_STATE_CANCEL_REQUESTED", "attempt": 1, "lastWorkerIdentity": "target-xc-screen-c-screen-1014-c2-g1"}]
```

The SDK's log lines printed without a time, in file order (the indented lines between them are a Python traceback):

```text
worker-target-g1.out:1
            Completing activity as failed ({'activity_id': 'work', 'activity_type': 'work_hb', 'attempt': 1, 'namespace': 'default', 'task_queue': 'expc-target-xc-screen-c-screen-1014-c2', 'workflow_id': 'expc-xc ...
worker-target-g1.out:20
            asyncio.exceptions.CancelledError: Activity cancelled
```

