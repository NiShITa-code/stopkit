# n8n fast-mode smoke runs, 11 Oct 2026 (exploratory)

**What they are.** Six plumbing runs of the Experiment B harness in fast mode (`STOPCHECK_FAST=1`). Fast mode puts the stop 20 s after the agent fires and watches for 60 s; full trials use 75 s and 180 s (`harness_b.py:67-68`). The runs are in `results/XB-fast-*`:

| Run | Started (11 Oct) |
|---|---|
| b0 | 06:16:37 |
| B1 | 06:24:37 |
| B3 | 06:27:44 |
| B4b | 06:30:45 |
| B5 | 06:33:58 |
| B2 | 06:37:25 |

**Not scored trials.** The rules were draft 3, not frozen, no predictions existed, and fast mode shortens every timing. They are excluded from every count.

**Scoring.**
- Only b0 and B1 were scored, at 06:30 and 06:31, by `analyze_b.py` as committed here.
- Their summaries say "scored". That label is to be corrected.
- The scorer has five known defects, so these rows are not reliable:
  - M3 reads "met" when the watch ends before the deadline (`analyze_b.py:328`);
  - M3 reads "met" for work that was not planted (`:325`);
  - "effects closed" can be true when the state is unknown (`:435`);
  - any return code counts as "stop accepted", and so does B0, which has no stop (`:429`);
  - "came back after restart" does not check that the work had stopped first (`:476`).

**Provenance.** The records hold no code hashes, so this commit does not prove which code produced which run. File times are the only evidence:
- `workflows_b.py` (05:51) and `harness_b_ns.sh` (05:42) are older than all six runs.
- `harness_b.py` was last changed at 06:28:31. That is after b0, B1 and B3 started, and before B4b, B5 and B2 did.
- So b0, B1 and B3 ran an earlier, unrecorded version of `harness_b.py`. The other three probably ran the version committed here.

**Who has seen them.**
- The session that built the harness ran them.
- An outside review on 12 Oct read saved summaries and records.
- Before this commit, this session had looked only at file names and labels.

Predictions written from now on are prospective for new runs only.
