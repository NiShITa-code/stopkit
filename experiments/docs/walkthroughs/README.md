# Walkthroughs

Four pages, one per platform, each showing the real record lines around a few stops: the platform's own log lines and
answers, the recorder's pings and the stand-in outside service's lines, merged on one clock. Every line names the file
and line number it was copied from, and a short paragraph above each block says what to look at. Each page is the
output of its script under `scripts/`, run on the records.

| Page | Trials | Records | Script |
|---|---|---|---|
| [LANGGRAPH.md](LANGGRAPH.md) | The LangGraph trials of 9 October: cancel the run (XA-X-L1-t1), delete its thread (XA-X-L3-t1) | `experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1/` | `experiments/scripts/walkthrough_langgraph.py` |
| [N8N.md](N8N.md) | The n8n trials of 13 October: stop by id (B1), the same stop after a main restart (B1r), delete the workflow (B4b) | `experiments/n8n-queue-mode/records/cb1013/` | `experiments/scripts/walkthrough_n8n.py` |
| [TEMPORAL.md](TEMPORAL.md) | The Temporal trials of 14 October: cancel without heartbeats (C3-a2), cancel with heartbeats (C2) | `experiments/temporal-activities/records/cs1014/` | `experiments/scripts/walkthrough_temporal.py` |
| [OPENCLAW.md](OPENCLAW.md) | OpenClaw run 3 of 14 October: abort the run, then SIGTERM to the Gateway | `experiments/openclaw-commands/records/runs/spike-20261014-071212/` | `experiments/scripts/walkthrough_openclaw.py` |

`experiments/scripts/check_pages.py` opens every file and record field a page cites and checks that the page shows what the file
holds, that each stamped line sits at the right time, and that every decimal number in a paragraph appears in its
block. `python3 -B experiments/scripts/build_results.py` writes each page again from the records, compares it with the committed
one and runs the page check on it.
