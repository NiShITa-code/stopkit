# Re-check of smoke-20261029T183940Z

Files checked against SHA256SUMS.json: 16; mismatched: 0. Client executables unchanged after the batch: True.
Runs: 1 measured, 0 excluded. Differences between the harness's records and this re-derivation: 0.

| Client | Path | n | Cancel | Delay (s) | Reason | Server got | Server ended | Markers after stop | Work | Client exit (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| codex-0.165.0-alpha.1 | esc | 1 | 1/1 | 0.003 | tool call response future dropped | nothing | still running | 19 | NOT STOPPED | stayed open |

## Predictions, run by run

| Client / path | Prediction | Held |
|---|---|---|
| codex-0.165.0-alpha.1 / esc | cancel sent | 1/1 |
| codex-0.165.0-alpha.1 / esc | reason 'tool call response future dropped' | 1/1 |

## What the model was told about the stopped call

- codex-0.165.0-alpha.1 / esc: "aborted by user after 1.2s"

## Messages shown in the terminal (phrase found in the recording, runs with it / runs)

- codex-0.165.0-alpha.1 / esc: "Conversation interrupted" 1/1, "Error" 1/1, "Interrupted" 1/1

## Exploratory: after the client exited

