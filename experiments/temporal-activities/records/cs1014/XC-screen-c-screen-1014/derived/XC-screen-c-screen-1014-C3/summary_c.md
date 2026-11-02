# Experiment C trial XC-screen-c-screen-1014-C3 (C3): exploratory screen

- validity / trial validity: invalid - INVALID: readiness and capability jobs
- FAC / headline O2 x by commit: no O2 claim - no read makes the O2 claim
- FAC / headline O1 x by commit: false all-clear - CANCELED at rel 0.122 s; 12 declared effects after
- M2 / claim: CANCELED - first closed read: CANCELED at rel 0.122 s (poll-1)
- M2 / terminal status: CANCELED - last watch read: CANCELED
- M3 / final landed: yes - a final effect landed by the end of the watch
- M4 / duplicates: none - 1 execution(s); steps posted by >1 execution: []; final effects: 1
- M4 / duplicates visible in Temporal: no duplicates - max pending attempt 1; ActivityTaskStarted attempts []; markers 0
- history / history omits an execution that produced effects: yes - 1 of 1 executions not recorded (ActivityTaskStarted attempts []; markers 0)
- pending_on_closed / a closed run lists a pending activity: yes - 10 of 10 closed reads list pending activities
- events / five events: info - accepted=accepted, state_changed=poll-1, work_ceased=True (bounded quiet=True, computation closed), effects_closed=closed by completion, not by the stop (obligation closed), evidence_sufficient=True
- verified_stop / verified stop: unresolved - unresolved (the trial is invalid by C.9 (INVALID: readiness and capability jobs))
- twin / twin control: pass - status COMPLETED; effects {'accepted': 1, 'progress': 20, 'final': 1}; executions 1; attempts [1]
- M1 / by commit, from the cancel: effects after - 12 declared effects committed after; kinds {'progress': 11, 'final': 1}
- M1 / by initiation, from the cancel: effects after - 12 declared effects initiated after
- M1 / ambiguous, from the cancel: info - 0 arrived during the call
- restart / durability probe: unresolved - unresolved (capability job or persistence control failed)
- wall/monotonic clock recomputed from receiver.jsonl: passed; harness recorded: passed [a guard: record schema 2 and later]
