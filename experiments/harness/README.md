# Frozen code

Each folder holds the code at a commit that a set of trial records names, as it was archived from the working
repository with `git archive`, with the substitutions that [data/MANIFEST.json](../data/MANIFEST.json) lists. The
setup scripts build a local git repository from these folders to run the trials again.

The working repository held every platform's code side by side (`n8n/`, `langgraph/`, `temporal/`, `openclaw/`, and
the shared `common/`), and each study ran from a different commit of it. So a folder's name says which study it serves,
not everything it holds: `temporal-627df0b/langgraph/recorder.py`, for example, is the outside recorder that the
Temporal and n8n freezes also bind.

| Folder | Commit | What it holds | Used by |
|---|---|---|---|
| `n8n-langgraph-7a3fcec/` | `7a3fcec`, 13 Oct 2026 | The tree at the freeze of the n8n trials: the n8n harness, scorer, rules and tests (`n8n/`, `tests/`); a later revision of the LangGraph harness and its rules (`langgraph/`), with the raw records and scorer of the LangGraph trials of 9 October in `langgraph/results/expA-round1/` (those trials ran commit `cd26851`: see the next row); shared code (`common/`) and the predictions (`predictions/`) | [n8n queue mode](../n8n-queue-mode/), [LangGraph controls](../langgraph-controls/) |
| `langgraph-cd26851/` | `cd26851`, 9 Oct 2026 | The 13 files the LangGraph runner bound by SHA-256 before round 1's first trial; 11 have the SHA-256 the trials recorded, and `langgraph/RULES-expA.md` and `langgraph/analyze_x.py` are as the commit holds them, made after the round. A copy changed in four files so it runs on any machine is in [langgraph-controls/code](../langgraph-controls/code/) | [LangGraph controls](../langgraph-controls/) |
| `n8n-scorer-3832d7c/` | `3832d7c` | The n8n scorer as it was at that commit (`n8n/analyze_b.py`), which the runner's mutation check (`n8n-langgraph-7a3fcec/n8n/mutate_b.py`) reads with `git show 3832d7c:n8n/analyze_b.py` | `experiments/setup/n8n-queue-mode.sh` |
| `temporal-627df0b/` | `627df0b`, 14 Oct 2026 | The files the Temporal freeze (`temporal/FREEZE-expC.json`) binds, the freeze itself, the predictions (`predictions/temporal-expC-cells.json`) and the code beside them | [Temporal activities](../temporal-activities/) |
| `openclaw-b8fd1e4/` | `b8fd1e4`, 14 Oct 2026 | The OpenClaw runner, its stand-in model and its helpers (`openclaw/`) | [OpenClaw commands](../openclaw-commands/) |

`python3 -B experiments/verify.py` checks that `n8n-langgraph-7a3fcec`, `temporal-627df0b` and `langgraph-cd26851` hold
exactly the files the freezes and the trial records name, by SHA-256, and reruns the scorers on the raw records.

## Names in the frozen code and the records

The frozen code and the record folders keep the names the trials were run with:

| Name | Meaning |
|---|---|
| A, `expA`, `_x`, `XA-` | The LangGraph experiment: rules `RULES-expA.md`, scorer `analyze_x.py`, trial ids `XA-...` |
| B, `expB`, `_b`, `XB-` | The n8n queue-mode experiment: rules `RULES-expB.md`, freeze `FREEZE-expB.json`, scorer `analyze_b.py`, trial ids `XB-...` |
| C, `expC`, `_c`, `XC-` | The Temporal experiment: rules `RULES-expC.md`, freeze `FREEZE-expC.json`, scorer `analyze_c.py`, trial ids `XC-...` |
| `cb1013` | The n8n batch of 13 October; `w1` to `w6` are its six worker machines |
| `cs1014` | The Temporal batch of 14 October |
| `expA-round1` | The LangGraph trials of 9 October |
| B1, B1r, C3, L1, ... | A condition: the stop a trial applied, listed in each experiment's rules and in [experiments/docs/WHAT-EACH-STOP-REACHES.md](../docs/WHAT-EACH-STOP-REACHES.md) |
