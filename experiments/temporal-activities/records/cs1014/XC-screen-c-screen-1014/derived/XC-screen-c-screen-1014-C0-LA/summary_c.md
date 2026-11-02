# Experiment C trial XC-screen-c-screen-1014-C0-LA (C0-LA): exploratory screen

- validity / trial validity: valid - valid
- FAC / headline O2 x by commit: no false all-clear - COMPLETED at rel 20.17 s; 0 declared effects after
- FAC / headline O1 x by commit: no false all-clear - COMPLETED at rel 11.565 s; 0 declared effects after
- M2 / claim: COMPLETED - first closed read: COMPLETED at rel 11.565 s (poll-24)
- M2 / terminal status: COMPLETED - last watch read: COMPLETED
- M3 / final landed: yes - a final effect landed by the end of the watch
- M4 / duplicates: none - 1 execution(s); steps posted by >1 execution: []; final effects: 1
- M4 / duplicates visible in Temporal: no duplicates - max pending attempt 0; ActivityTaskStarted attempts []; markers 1
- history / history omits an execution that produced effects: no - 0 of 1 executions not recorded (ActivityTaskStarted attempts []; markers 1)
- pending_on_closed / a closed run lists a pending activity: no - 0 of 6 closed reads list pending activities
- events / five events: info - accepted=not applicable, state_changed=poll-24, work_ceased=True (bounded quiet=True, computation closed), effects_closed=closed by completion, not by the stop (obligation closed), evidence_sufficient=True
- verified_stop / verified stop: ended by completion, not by the stop - ended by completion, not by the stop (the run is COMPLETED and every work execution has independent ending evidence; 1 execution(s) ran to their final effect)
- twin / twin control: pass - status COMPLETED; effects {'accepted': 1, 'progress': 20, 'final': 1}; executions 1; attempts [1]
- M1 / by commit, from t_stop: effects after - 12 declared effects committed after; kinds {'progress': 11, 'final': 1}
- M1 / by initiation, from t_stop: effects after - 12 declared effects initiated after
- M1 / ambiguous, from t_stop: info - 0 arrived during the call
- restart / durability probe: stayed ended - stayed ended
- wall/monotonic clock recomputed from receiver.jsonl: passed; harness recorded: passed [a guard: record schema 2 and later]
