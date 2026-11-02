# Runbook

## Regenerating the evidence

From the repository root. The checks need only Python 3.11 or later: no root, no Linux host and no network. The tests
need pytest, which the dev extras install (`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`).

```bash
python3 -B experiments/verify.py                                          # every check; compares with the results file
python3 -B experiments/scripts/build_results.py                           # the same checks; writes the results file
python3 -B experiments/scripts/build_results.py --only n8n_restart_updates  # one check, by its name below
.venv/bin/python -m pytest -q experiments/tests                            # the studies' tests
```

A full run of the checks takes about ten seconds; the tests take under a minute. The pages written from the
records are made again with:

```bash
python3 -B experiments/scripts/loop_evidence.py --out experiments/docs/n8n-loop-evidence --overwrite
python3 -B experiments/scripts/walkthrough_langgraph.py experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1 \
    XA-X-L1-t1 XA-X-L3-t1 > experiments/docs/walkthroughs/LANGGRAPH.md
python3 -B experiments/scripts/walkthrough_n8n.py experiments/n8n-queue-mode/records/cb1013 B1 B1r B4b > experiments/docs/walkthroughs/N8N.md
python3 -B experiments/scripts/walkthrough_temporal.py experiments/temporal-activities/records/cs1014 C3-a2 C2 \
    > experiments/docs/walkthroughs/TEMPORAL.md
python3 -B experiments/scripts/walkthrough_openclaw.py experiments/openclaw-commands/records/runs/spike-20261014-071212 \
    > experiments/docs/walkthroughs/OPENCLAW.md
```

`python3 -B experiments/scripts/build_data.py --check` compares a rebuild from `experiments/data/source/archive.zip`, which is not tracked, with
the tree; without `--check` it writes nothing unless every check it makes passes, and
`experiments/scripts/build_results.py` must run after it.

## Checks

| Check | Fails when |
|---|---|
| `records` | a file placed from the source archive differs in size or SHA-256 from what `experiments/data/MANIFEST.json` records |
| `n8n_checksums` | a file copied off the six n8n machines no longer has the SHA-256 in its collection's `SHA256SUMS`, a file outside its `manifests/` folder is not listed there, or other than six collections are found |
| `n8n_freeze` | a file `FREEZE-expB.json` binds differs from `experiments/harness/n8n-langgraph-7a3fcec`, a trial record names another SHA-256 for it, or other than 19 trial records are found |
| `n8n_rescore` | the frozen scorer, rerun on a trial's raw files, gives a row other than the saved one; the display-only list of state changes in the `events` row is left out of the comparison, because the scorer takes it from a Python set, whose order varies from run to run |
| `temporal_freeze` | a file `FREEZE-expC.json` binds, or the predictions, differ from `experiments/harness/temporal-627df0b` |
| `temporal_rescore` | the frozen scorer, rerun on a trial's raw files, gives a report other than the saved one, or other than 17 reports are found |
| `langgraph_reproduce` | `reproduce.py --quick` fails or skips a check, passes other than 36, or does not finish; its two derived summaries whose manifest entries were written with Windows line endings pass only when their content, given Windows line endings, has exactly the manifest's SHA-256 |
| `langgraph_round1_code` | round 1's `FREEZE.json` binds other than 13 files, or a file in `experiments/harness/langgraph-cd26851` has another SHA-256 than the freeze records, apart from `langgraph/RULES-expA.md` and `langgraph/analyze_x.py`, which the commit changed after the round (`experiments/langgraph-controls/code/CHANGES.md`) |
| `n8n_restart_verdicts` | the saved transcripts hold other than 11 reproduced verdicts, or any run that did not reproduce |
| `n8n_restart_updates` | the recount of updates after each stop reply, in the receiver file of the 29 October run on a fresh virtual machine, differs from the recorded counts, other than 11 run archives are found, or in one of them the control sent an update after its reply |
| `client_probe_recheck` | the probe's own re-check, rerun on a copy of a saved batch, writes a `check.json` other than the saved one, line endings aside |
| `loop_evidence` | `experiments/scripts/loop_evidence.py`, rerun on the raw records, writes rows or a page other than those in `experiments/docs/n8n-loop-evidence/` |
| `walkthroughs` | a page in `experiments/docs/walkthroughs/` differs from its script's output, or quotes a line, a field, a time or a number that its record does not hold |
| `selftest_recheck` | `stopkit recheck`, run on one of the six saved self-test runs, does not give its saved verdicts |

Each check prints the claim it tests and what it found, and the build writes `experiments/results/results.json` only when all of
them pass. The names of the records and of the frozen code are explained in
[experiments/harness/README.md](harness/README.md#names-in-the-frozen-code-and-the-records).

## Constraints on what may be claimed

- Most conditions ran once. A row of a results table describes one observed case, not a rate, and the documents say
  so where a row is summarized.
- Do not change `loop_evidence` or `n8n_rescore` to accept silence or a platform status as an end; see the ended rule
  in [README.md](README.md#overview).
- A recorded run is never edited to match a new result; a new run goes in a new folder, beside the recorded ones.
- A changed record's citations are rewritten with it: never edit a record without rebuilding through
  `experiments/scripts/build_data.py`.

## Release checklist

Before the repository is made public:

1. The repository's final name is settled.
2. `python3 -B experiments/scripts/build_data.py --check` passes against the source archive.
3. `python3 -B experiments/verify.py` passes from a fresh clone, and CI is green on every job.
