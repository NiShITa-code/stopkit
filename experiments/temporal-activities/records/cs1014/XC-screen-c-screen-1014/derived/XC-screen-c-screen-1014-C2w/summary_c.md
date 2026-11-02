# Experiment C trial XC-screen-c-screen-1014-C2w (C2w): exploratory screen

- validity / trial validity: valid - valid
- FAC / headline O2 x by commit: no false all-clear - CANCELED at rel 5.046 s; 0 declared effects after
- FAC / headline O1 x by commit: no false all-clear - CANCELED at rel 3.558 s; 0 declared effects after
- M2 / claim: CANCELED - first closed read: CANCELED at rel 3.558 s (poll-8)
- M2 / terminal status: CANCELED - last watch read: CANCELED
- M3 / final landed: no - no final effect by the end of the watch
- M4 / duplicates: none - 1 execution(s); steps posted by >1 execution: []; final effects: 0
- M4 / duplicates visible in Temporal: no duplicates - max pending attempt 1; ActivityTaskStarted attempts [1]; markers 0
- history / history omits an execution that produced effects: no - 0 of 1 executions not recorded (ActivityTaskStarted attempts [1]; markers 0)
- pending_on_closed / a closed run lists a pending activity: no - 0 of 8 closed reads list pending activities
- events / five events: info - accepted=accepted, state_changed=poll-1, work_ceased=None (bounded quiet=True, computation unresolved), effects_closed=not closed (obligation closed), evidence_sufficient=True
- verified_stop / verified stop: unresolved - unresolved (execution f695169e3f18a921 (attempt 1): no independent ending evidence (no final effect; its worker is not shown dead); the history's ActivityTaskStarted for the work activity (attempt 1) matches no closed execution)
- twin / twin control: pass - status COMPLETED; effects {'accepted': 1, 'progress': 20, 'final': 1}; executions 1; attempts [1]
- M1 / by commit, from the cancel: effects after - 4 declared effects committed after; kinds {'progress': 4}
- M1 / by initiation, from the cancel: effects after - 4 declared effects initiated after
- M1 / ambiguous, from the cancel: info - 0 arrived during the call
- restart / durability probe: stayed ended - stayed ended
- wall/monotonic clock recomputed from receiver.jsonl: passed; harness recorded: passed [a guard: record schema 2 and later]
