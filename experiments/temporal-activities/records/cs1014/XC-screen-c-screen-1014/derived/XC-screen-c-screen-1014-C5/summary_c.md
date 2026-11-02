# Experiment C trial XC-screen-c-screen-1014-C5 (C5): exploratory screen

- validity / trial validity: valid - valid
- FAC / headline O2 x by commit: no O2 claim - no read makes the O2 claim
- FAC / headline O1 x by commit: false all-clear - TERMINATED at rel 0.099 s; 4 declared effects after
- M2 / claim: TERMINATED - first closed read: TERMINATED at rel 0.099 s (+0)
- M2 / terminal status: TERMINATED - last watch read: TERMINATED
- M3 / final landed: no - no final effect by the end of the watch
- M4 / duplicates: none - 1 execution(s); steps posted by >1 execution: []; final effects: 0
- M4 / duplicates visible in Temporal: no duplicates - max pending attempt 1; ActivityTaskStarted attempts []; markers 0
- history / history omits an execution that produced effects: yes - 1 of 1 executions not recorded (ActivityTaskStarted attempts []; markers 0)
- pending_on_closed / a closed run lists a pending activity: yes - 10 of 10 closed reads list pending activities
- events / five events: info - accepted=accepted, state_changed=+0, work_ceased=None (bounded quiet=True, computation unresolved), effects_closed=not closed (obligation closed), evidence_sufficient=True
- verified_stop / verified stop: unresolved - unresolved (execution 5c9aee44c5d7cf3a (attempt 1): no independent ending evidence (no final effect; its worker is not shown dead); pending work attempt 1 reads STARTED at the end of the watch; no closed execution accounts for it)
- twin / twin control: pass - status COMPLETED; effects {'accepted': 1, 'progress': 20, 'final': 1}; executions 1; attempts [1]
- M1 / by commit, from the terminate: effects after - 4 declared effects committed after; kinds {'progress': 4}
- M1 / by initiation, from the terminate: effects after - 4 declared effects initiated after
- M1 / ambiguous, from the terminate: info - 0 arrived during the call
- restart / durability probe: stayed ended - stayed ended
- wall/monotonic clock recomputed from receiver.jsonl: passed; harness recorded: passed [a guard: record schema 2 and later]
