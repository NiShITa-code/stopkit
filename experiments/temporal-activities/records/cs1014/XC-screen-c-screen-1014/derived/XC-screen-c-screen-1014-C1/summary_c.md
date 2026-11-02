# Experiment C trial XC-screen-c-screen-1014-C1 (C1): exploratory screen

- validity / trial validity: valid - valid
- FAC / headline O2 x by commit: no false all-clear - CANCELED at rel 0.122 s; 0 declared effects after
- FAC / headline O1 x by commit: no false all-clear - CANCELED at rel 0.128 s; 0 declared effects after
- M2 / claim: CANCELED - first closed read: CANCELED at rel 0.128 s (poll-1)
- M2 / terminal status: CANCELED - last watch read: CANCELED
- M3 / final landed: no - no final effect by the end of the watch
- M4 / duplicates: none - 0 execution(s); steps posted by >1 execution: []; final effects: 0
- M4 / duplicates visible in Temporal: no duplicates - max pending attempt 0; ActivityTaskStarted attempts []; markers 0
- history / history omits an execution that produced effects: not applicable - no work execution posted
- pending_on_closed / a closed run lists a pending activity: no - 0 of 10 closed reads list pending activities
- events / five events: info - accepted=accepted, state_changed=poll-1, work_ceased=True (bounded quiet=True, computation closed), effects_closed=closed (obligation closed), evidence_sufficient=True
- verified_stop / verified stop: stopped - stopped (the run is CANCELED and every work execution has independent ending evidence)
- twin / twin control: pass - status COMPLETED; effects {'accepted': 1, 'progress': 20, 'final': 1}; executions 1; attempts [1]
- M1 / by commit, from the cancel: none after - 0 declared effects committed after; kinds {}
- M1 / by initiation, from the cancel: none after - 0 declared effects initiated after
- M1 / ambiguous, from the cancel: info - 0 arrived during the call
- restart / durability probe: stayed ended - stayed ended
- wall/monotonic clock recomputed from receiver.jsonl: passed; harness recorded: passed [a guard: record schema 2 and later]
