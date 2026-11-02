## Open points in the rules

The second revision of RULES-expA.md settled the earlier open points: status detectors for inrun, sync, async and collateral, and how split, "pending" and failed reads score (section 6); NET × out E (an instrument check, section 9); L3 on the other graphs (not run, section 10); "copy present", which is reported without a verdict (section 4); planting for the other graphs' carriers (section 5); and the collateral run and restart probe on every graph (section 10). The third revision settled "tok_use P": section 9 now names only keyvalid P for KEY and KEYC, so there is no tok_use P cell. The fourth revision settled how status is judged (against what it claims, with out of scope for a 404), the circular reads after the restart, and sync and async in the pending-run rule. These remain:

- **KEYC's class.** The draft gave none, so the KEYC cells have no class. Set D or T before committing; it decides 3 or 10 trials.
- **Restart cells of instrument-check carriers.** Section 9 gives values for C, A, E or P only, so the restart cells of KEY and KEYC × key_use, tok_use, keyvalid, outjob_s and outjob_r, NET × keyvalid and out, and PG × det are open, not instrument checks. Fill them, or extend section 9.
- **Two readings in the false all-clear (section 6).** The scorer counts a unit read as indicating that the work ended only when no part of it says pending, running or busy, so a split read ("interrupted" with "busy") or a 404 next to "busy" is no all-clear. Under the other reading the L2 and S1 cells on parent_x would be "false all-clear". The scorer also leaves the collateral run out of "any planted carrier", since the harness starts it and it is not the agent's work. Confirm both readings, or say which to change.

## Before committing

1. Fill every [your call] cell, here and in `langgraph-expA-cells.json`, and confirm or change every false all-clear cell marked "[from a pilot: confirm]". To leave a cell unpredicted, leave it null or write "no prediction"; both mean not predicted, and the scorer reports such a cell without counting it. The two files must agree: edit `tools/build_preds.py` and run `python -I tools/build_preds.py`, which writes both. Open cells by stop:

{{COUNTS}}

2. Settle the open points above.
3. If Experiment A runs on 0.15.2, as the plan pins, re-check the cited 0.15.1 lines first, above all LANGGRAPH-SCOPE findings 3, 5 and 6. Downloading 0.15.2 needs your approval.
4. Commit RULES-expA.md, `langgraph-expA-cells.json`, this file (renamed without -DRAFT), `tools/` and the code (harness, scorer, graphs, canary, recorder, mocks and guards; add `common/mocks.py` to git, F20) together with `stopcheck/.gitattributes` (`* text eol=lf`), before the first XA- run. First run `git add` on every one of these paths, including the files already staged on 8 Oct, which `git status --short` shows as `AM` (staged, then changed: the index holds the 8 Oct copy). Then check that `git status --short` shows none of them as `AM` or `??`. Otherwise the commit holds the 8 Oct copies, which do not match the sha256 each run records (RULES section 12).
