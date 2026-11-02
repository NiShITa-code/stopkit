# Round 1, rescored under the sixth revision: POST HOC, not preregistered (RULES-expA.md R6.11)

These files were produced by `analyze_x.py` (sixth revision) from round 1's raw files in `../expA-round1/`, which
are unchanged. Round 1's record is the fifth-revision scoring in `../expA-round1/`. Here:
- round 1 has no sender or reply stamps, so claim times are upper bounds and M1 counts are lower bounds;
- initiation counts assume a 0.05 s arrival allowance. The smoke trials of the new harness (results/smoke-x6)
  saw a maximum send-to-arrival delay of 54 ms (5 of 1,634 pings over 50 ms), so round 1's initiation counts are
  NOT lower bounds. The commit counts (the headline) need no allowance;
- nothing here is compared with the predictions file.
Rebuild with `python3 reproduce.py` from the repository root, which also checks these files are unchanged.
