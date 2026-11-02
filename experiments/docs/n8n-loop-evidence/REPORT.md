# n8n loop activity and computation-end evidence

In each of the 19 trials of 13 October 2026 the agent ran four loops inside n8n: its own loop and three children (a Code-node loop, a sub-workflow loop and an AI Agent node driven by a scripted stand-in model). For every trial and loop, the tables below give the updates our receiver got after the stop, whether one arrived in the last five seconds of the watch, the status n8n reported, and whether the raw records show that the loop's computation ended.

- Trials: 19; loop readings: 76.
- Computation-end evidence at the snapshot: 16.
- Termination unconfirmed: 60; of these, 17 have no recent activity.
- Invalid, untested or incomplete: 0.

A loop that is quiet for the last five seconds of the watch counts as unconfirmed unless the records show its computation ended. A loop can be active in those five seconds and still have ended by the snapshot.

## How to read this page

Updates are receiver arrivals after the recorded stop (for B0, the trial with no stop, after its control anchor) and up to the declared end of the watch. *Recent* means an arrival in the final five seconds of that window. The system snapshot, the harness's last reading of processes and services, has its own completion time and can finish slightly after the window.

Computation-end evidence is one of two readings of the raw records: the worker's service inactive or failed, with no main process and a complete, empty reading of its process group; or every identified execution at its designed last step with a successful record. Either shows that the loop's computation ended; neither shows what ended it, and a cleanup or restart after the snapshot does not change the reading at the snapshot.

The rows are computed with the frozen scorer's own reader, validity checks and computation-boundary function (`experiments/harness/n8n-langgraph-7a3fcec/n8n/analyze_b.py`), from each trial's `record.json` and the recorder and service logs.

## Three examples: the main execution

| Condition | Updates after stop | Recorded platform status | Evidence assessment |
|---|---:|---|---|
| B1 | 0 | canceled | No recent activity observed; termination unconfirmed |
| B1r | 179 | canceled | Recent activity observed; termination unconfirmed |
| B8 | 24 | error | Computation-end evidence at the snapshot |

B1 stops the execution by its id; B1r gives the same stop after main has restarted; B8 stops the worker's service. The three stops have different intended scopes, and the workload is older at the stop in B1r than in B1.

## B0: XB-screen-cb1013-w2-B0

[Raw record](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B0-20261013-225019-203451906/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B0-20261013-225019-203451906/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.623 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.501](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B0-20261013-225019-203451906/pings.jsonl#L2140) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.655](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B0-20261013-225019-203451906/pings.jsonl#L2141) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.379](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B0-20261013-225019-203451906/pings.jsonl#L2139) | True | 2: running | Recent activity observed; termination unconfirmed |
| sub | 180 | [179.655](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B0-20261013-225019-203451906/pings.jsonl#L2142) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B1: XB-screen-cb1013-w4-B1

[Raw record](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B1-20261013-225502-559385487/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B1-20261013-225502-559385487/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.654 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.874](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B1-20261013-225502-559385487/pings.jsonl#L1963) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.721](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B1-20261013-225502-559385487/pings.jsonl#L1962) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 0 | none | False | 2: canceled | No recent activity observed; termination unconfirmed |
| sub | 179 | [178.998](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B1-20261013-225502-559385487/pings.jsonl#L1959) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B10: XB-screen-cb1013-w1-B10

[Raw record](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B10-20261013-225705-766322237/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B10-20261013-225705-766322237/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.114 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 10 | [9.756](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B10-20261013-225705-766322237/pings.jsonl#L758) | False | 7: unavailable | Computation-end evidence at the snapshot |
| loop_node | 10 | [9.804](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B10-20261013-225705-766322237/pings.jsonl#L761) | False | 3: unavailable | Computation-end evidence at the snapshot |
| loop_steps | 10 | [9.195](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B10-20261013-225705-766322237/pings.jsonl#L757) | False | 2: unavailable | Computation-end evidence at the snapshot |
| sub | 10 | [9.929](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B10-20261013-225705-766322237/pings.jsonl#L763) | False | 4: unavailable | Computation-end evidence at the snapshot |

- **ai_agent evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **loop_node evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **loop_steps evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **sub evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.

## B11: XB-screen-cb1013-w3-B11

[Raw record](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B11-20261013-230732-217173034/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B11-20261013-230732-217173034/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.585 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.147](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B11-20261013-230732-217173034/pings.jsonl#L2143) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.635](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B11-20261013-230732-217173034/pings.jsonl#L2146) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.273](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B11-20261013-230732-217173034/pings.jsonl#L2144) | True | 2: running | Recent activity observed; termination unconfirmed |
| sub | 179 | [179.032](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B11-20261013-230732-217173034/pings.jsonl#L2141) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B1r: XB-screen-cb1013-w1-B1r

[Raw record](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B1r-20261013-223702-894940371/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B1r-20261013-223702-894940371/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.987 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 174 | [179.781](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B1r-20261013-223702-894940371/pings.jsonl#L3084) | True | 7: crashed | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.744](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B1r-20261013-223702-894940371/pings.jsonl#L3083) | True | 3: crashed | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.463](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B1r-20261013-223702-894940371/pings.jsonl#L3082) | True | 2: canceled | Recent activity observed; termination unconfirmed |
| sub | 179 | [179.014](../../n8n-queue-mode/records/cb1013/w1/collect-001/results/XB-screen-cb1013-w1-B1r-20261013-223702-894940371/pings.jsonl#L3078) | True | 4: crashed | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B2: XB-screen-cb1013-w5-B2

[Raw record](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B2-20261013-225502-158276030/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B2-20261013-225502-158276030/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.652 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 1 | [0.316](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B2-20261013-225502-158276030/pings.jsonl#L686) | False | 7: canceled | No recent activity observed; termination unconfirmed |
| loop_node | 1 | [0.121](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B2-20261013-225502-158276030/pings.jsonl#L685) | False | 3: canceled | No recent activity observed; termination unconfirmed |
| loop_steps | 1 | [0.359](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B2-20261013-225502-158276030/pings.jsonl#L688) | False | 2: canceled | No recent activity observed; termination unconfirmed |
| sub | 1 | [0.511](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B2-20261013-225502-158276030/pings.jsonl#L689) | False | 4: canceled | No recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B2s: XB-screen-cb1013-w6-B2s

[Raw record](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B2s-20261013-225510-436673413/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B2s-20261013-225510-436673413/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.725 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 1 | [0.309](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B2s-20261013-225510-436673413/pings.jsonl#L687) | False | 7: canceled | No recent activity observed; termination unconfirmed |
| loop_node | 1 | [0.129](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B2s-20261013-225510-436673413/pings.jsonl#L685) | False | 3: canceled | No recent activity observed; termination unconfirmed |
| loop_steps | 1 | [0.343](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B2s-20261013-225510-436673413/pings.jsonl#L688) | False | 2: canceled | No recent activity observed; termination unconfirmed |
| sub | 1 | [0.522](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B2s-20261013-225510-436673413/pings.jsonl#L690) | False | 4: canceled | No recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B3: XB-screen-cb1013-w2-B3

[Raw record](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B3-20261013-223649-863179536/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B3-20261013-223649-863179536/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.587 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.497](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B3-20261013-223649-863179536/pings.jsonl#L2140) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.523](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B3-20261013-223649-863179536/pings.jsonl#L2141) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.276](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B3-20261013-223649-863179536/pings.jsonl#L2139) | True | 2: running | Recent activity observed; termination unconfirmed |
| sub | 179 | [179.023](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B3-20261013-223649-863179536/pings.jsonl#L2138) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B4a: XB-screen-cb1013-w3-B4a

[Raw record](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B4a-20261013-223701-106506393/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B4a-20261013-223701-106506393/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.583 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.833](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B4a-20261013-223701-106506393/pings.jsonl#L2141) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.833](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B4a-20261013-223701-106506393/pings.jsonl#L2143) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.534](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B4a-20261013-223701-106506393/pings.jsonl#L2139) | True | 2: running | Recent activity observed; termination unconfirmed |
| sub | 180 | [179.833](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B4a-20261013-223701-106506393/pings.jsonl#L2142) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B4b: XB-screen-cb1013-w4-B4b

[Raw record](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B4b-20261013-230147-210900872/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B4b-20261013-230147-210900872/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.767 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.541](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B4b-20261013-230147-210900872/pings.jsonl#L2145) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 179 | [179.442](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B4b-20261013-230147-210900872/pings.jsonl#L2141) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.116](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B4b-20261013-230147-210900872/pings.jsonl#L2139) | True | 2: None | Recent activity observed; termination unconfirmed |
| sub | 180 | [179.595](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B4b-20261013-230147-210900872/pings.jsonl#L2146) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B5: XB-screen-cb1013-w5-B5

[Raw record](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B5-20261013-230048-624433734/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B5-20261013-230048-624433734/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.658 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 177 | [179.894](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B5-20261013-230048-624433734/pings.jsonl#L2144) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.615](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B5-20261013-230048-624433734/pings.jsonl#L2141) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.894](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B5-20261013-230048-624433734/pings.jsonl#L2143) | True | 2: running | Recent activity observed; termination unconfirmed |
| sub | 180 | [179.93](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B5-20261013-230048-624433734/pings.jsonl#L2146) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B6a: XB-screen-cb1013-w6-B6a

[Raw record](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B6a-20261013-230056-842952070/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B6a-20261013-230056-842952070/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.589 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.335](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B6a-20261013-230056-842952070/pings.jsonl#L2140) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.706](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B6a-20261013-230056-842952070/pings.jsonl#L2143) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.611](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B6a-20261013-230056-842952070/pings.jsonl#L2141) | True | 2: running | Recent activity observed; termination unconfirmed |
| sub | 180 | [179.706](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B6a-20261013-230056-842952070/pings.jsonl#L2144) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B6b: XB-screen-cb1013-w2-B6b

[Raw record](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B6b-20261013-224334-563382725/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B6b-20261013-224334-563382725/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.705 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.573](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B6b-20261013-224334-563382725/pings.jsonl#L2144) | True | 7: running | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.579](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B6b-20261013-224334-563382725/pings.jsonl#L2145) | True | 3: running | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.324](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B6b-20261013-224334-563382725/pings.jsonl#L2143) | True | 2: running | Recent activity observed; termination unconfirmed |
| sub | 180 | [179.987](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B6b-20261013-224334-563382725/pings.jsonl#L2149) | True | 4: running | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B7: XB-screen-cb1013-w2-B7

[Raw record](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B7-20261013-225703-498121477/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B7-20261013-225703-498121477/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.208 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 176 | [179.529](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B7-20261013-225703-498121477/pings.jsonl#L2136) | True | 7: unavailable | Recent activity observed; termination unconfirmed |
| loop_node | 180 | [179.534](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B7-20261013-225703-498121477/pings.jsonl#L2137) | True | 3: unavailable | Recent activity observed; termination unconfirmed |
| loop_steps | 179 | [179.263](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B7-20261013-225703-498121477/pings.jsonl#L2135) | True | 2: unavailable | Recent activity observed; termination unconfirmed |
| sub | 180 | [179.97](../../n8n-queue-mode/records/cb1013/w2/collect-001/results/XB-screen-cb1013-w2-B7-20261013-225703-498121477/pings.jsonl#L2141) | True | 4: unavailable | Recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B8: XB-screen-cb1013-w3-B8

[Raw record](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B8-20261013-224345-719875604/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B8-20261013-224345-719875604/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.716 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 31 | [31.213](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B8-20261013-224345-719875604/pings.jsonl#L842) | False | 7: canceled | Computation-end evidence at the snapshot |
| loop_node | 24 | [23.356](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B8-20261013-224345-719875604/pings.jsonl#L824) | False | 3: error | Computation-end evidence at the snapshot |
| loop_steps | 24 | [23.658](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B8-20261013-224345-719875604/pings.jsonl#L827) | False | 2: error | Computation-end evidence at the snapshot |
| sub | 24 | [23.356](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-B8-20261013-224345-719875604/pings.jsonl#L823) | False | 4: error | Computation-end evidence at the snapshot |

- **ai_agent evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **loop_node evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **loop_steps evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **sub evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.

## B9a: XB-screen-cb1013-w4-B9a

[Raw record](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B9a-20261013-223711-478045382/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B9a-20261013-223711-478045382/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.577 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 0 | none | False | 7: running | Computation-end evidence at the snapshot |
| loop_node | 0 | none | False | 3: running | Computation-end evidence at the snapshot |
| loop_steps | 0 | none | False | 2: running | Computation-end evidence at the snapshot |
| sub | 0 | none | False | 4: running | Computation-end evidence at the snapshot |

- **ai_agent evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **loop_node evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **loop_steps evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **sub evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.

## B9b: XB-screen-cb1013-w5-B9b

[Raw record](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B9b-20261013-223715-472737880/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w5/collect-001/results/XB-screen-cb1013-w5-B9b-20261013-223715-472737880/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.503 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 0 | none | False | 7: crashed | No recent activity observed; termination unconfirmed |
| loop_node | 0 | none | False | 3: crashed | No recent activity observed; termination unconfirmed |
| loop_steps | 0 | none | False | 2: error | No recent activity observed; termination unconfirmed |
| sub | 0 | none | False | 4: crashed | No recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## B9c: XB-screen-cb1013-w6-B9c

[Raw record](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B9c-20261013-223723-806800993/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w6/collect-001/results/XB-screen-cb1013-w6-B9c-20261013-223723-806800993/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.701 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 0 | none | False | 7: crashed | No recent activity observed; termination unconfirmed |
| loop_node | 0 | none | False | 3: crashed | No recent activity observed; termination unconfirmed |
| loop_steps | 0 | none | False | 2: crashed | No recent activity observed; termination unconfirmed |
| sub | 0 | none | False | 4: crashed | No recent activity observed; termination unconfirmed |

- **ai_agent evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_node evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **loop_steps evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.
- **sub evidence:** no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its control group, nor the designed final iteration with a successful terminal record for every identified execution. Cause: unknown.

## PC: XB-screen-cb1013-w3-PC

[Raw record](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-PC-20261013-230136-669438843/record.json) · [Receiver log](../../n8n-queue-mode/records/cb1013/w3/collect-001/results/XB-screen-cb1013-w3-PC-20261013-230136-669438843/pings.jsonl)

Validity: valid. Activity window: 180.0 s; snapshot completion: +180.251 s (t_stop+180s).

| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |
|---|---:|---:|---|---|---|
| ai_agent | 0 | none | False | 7: unavailable | Computation-end evidence at the snapshot |
| loop_node | 0 | none | False | 3: unavailable | Computation-end evidence at the snapshot |
| loop_steps | 0 | none | False | 2: unavailable | Computation-end evidence at the snapshot |
| sub | 0 | none | False | 4: unavailable | Computation-end evidence at the snapshot |

- **ai_agent evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **loop_node evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **loop_steps evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.
- **sub evidence:** the internal runner's worker unit is inactive/failed and its independently read cgroup is empty. Cause: not established by this view.

