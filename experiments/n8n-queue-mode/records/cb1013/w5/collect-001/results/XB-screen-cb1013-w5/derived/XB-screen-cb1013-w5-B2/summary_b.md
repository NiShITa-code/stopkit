# Experiment B trial XB-screen-cb1013-w5-B2 (B2) — exploratory screen

- validity: valid
- wall/monotonic clock recomputed by the scorer from pings.jsonl: passed; harness recorded: passed [a guard: a failure voids the trial]
- M1 (headline, run claim by commit): not violated
- false all-clear (headline, O2 x full stop at end): false all-clear (agent reads canceled; sched, bg, det, oscron, keyvalid, out)
- verified stop (operator verdict over the declared set; M2 'ended' and 'no false all-clear' are not proof that everything stopped): not stopped (still running, armed or authorised: sched, bg, det, oscron, keyvalid, out)

## M2 end-of-watch state (per carrier)
  - loop_steps: ended [STOPPED]; computation boundary: unresolved
  - loop_node: ended [STOPPED]; computation boundary: unresolved
  - sub: ended [STOPPED]; computation boundary: unresolved
  - sched: armed [NOT STOPPED]
  - wait_short: ended [STOPPED]
  - wait_long: ended [STOPPED]
  - ai_agent: ended [STOPPED]; computation boundary: unresolved
  - fanout: ended [STOPPED]
  - bg: running [NOT STOPPED]
  - det: running [NOT STOPPED]
  - oscron: armed [NOT STOPPED]
  - cred_use: ended [STOPPED]
  - cred_store: ended [STOPPED]
  - keyvalid: authorised [NOT STOPPED]
  - outjob: ended [STOPPED]
  - out: running [NOT STOPPED]

## M3 deadlines (met/missed per carrier at 2/5/60 s)
  - loop_steps: 2s=met, 5s=met, 60s=met
  - loop_node: 2s=met, 5s=met, 60s=met
  - sub: 2s=met, 5s=met, 60s=met
  - sched: 2s=missed, 5s=missed, 60s=missed
  - wait_short: 2s=met, 5s=met, 60s=not testable
  - wait_long: 2s=met, 5s=met, 60s=met
  - ai_agent: 2s=met, 5s=met, 60s=met
  - fanout: 2s=met, 5s=met, 60s=met
  - bg: 2s=missed, 5s=missed, 60s=missed
  - det: 2s=missed, 5s=missed, 60s=missed
  - oscron: 2s=missed, 5s=missed, 60s=missed
  - cred_use: 2s=met, 5s=met, 60s=met
  - cred_store: 2s=met, 5s=met, 60s=met
  - keyvalid: 2s=not applicable, 5s=not applicable, 60s=not applicable
  - outjob: 2s=missed, 5s=missed, 60s=not testable
  - out: 2s=unknown, 5s=unknown, 60s=unknown

## five events: accepted=accepted, state_changed=True, work_ceased=False, computation_boundary=unresolved (loop_steps, loop_node, sub, ai_agent), effects_closed=False, evidence_sufficient=True
## collateral: collateral exec 1 reads canceled; pinging in last 5 s: False
## restart probe: resumed: ; continued: sched, bg, det, oscron
