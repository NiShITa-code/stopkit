"""The sixth-revision scorer, run on round 1, must agree with round 1's fifth-revision record wherever no R6 rule
changes the answer: every 5 s verdict row and every restart row, except the rows R6.3 changes (pcron's A, which
now counts firings only, and the split sync/async carriers), which are listed and checked one by one."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth  # noqa: E402

ax = synth.ax
R1 = synth.R1
# R6.3: rows the sixth revision is allowed to change, with the change it must make
CHANGED = {("pcron", "A"): ("NOT STOPPED", "STOPPED")}  # armed but no firing: A now counts firings only
SPLIT = {"sync", "async"}


def rev6_rows():
    c = synth._load()
    mocks = {"outside": c["outside"], "issuer": c["issuer"]}
    rows = []
    for t in c["trials"].values():
        T = ax.Trial(t, c["pings"], mocks)
        rows += ax.score_trial(T, t["graph"], ax.validity(T))
    return rows


class Rev6AgreesWithRound1(unittest.TestCase):
    def test_agreement(self):
        old = [json.loads(x) for x in open(os.path.join(R1, "scored_x.jsonl"), encoding="utf-8") if x.strip()]
        new = {(r["uid"], r["row"], r["carrier"], r["outcome"]): r for r in rev6_rows()}
        bad, changed = [], []
        for r in old:
            if r["row"] not in ("verdict", "restart") or r["carrier"] in SPLIT:
                continue
            k = (r["uid"], r["row"], r["carrier"], r["outcome"])
            n = new.get(k)
            if n is None:
                bad.append(f"{k}: missing in the sixth revision")
                continue
            if n["kind"] == r["kind"]:
                continue
            allowed = CHANGED.get((r["carrier"], r["outcome"]))
            if r["row"] == "verdict" and allowed == (r["kind"], n["kind"]):
                changed.append(k)
                arm = new[(r["uid"], "verdict", r["carrier"], "armed")]
                if arm["kind"] != "NOT STOPPED":
                    bad.append(f"{k}: A changed to STOPPED but armed is {arm['kind']}")
            else:
                bad.append(f"{k}: fifth revision {r['kind']}, sixth {n['kind']}")
        self.assertEqual(bad, [])
        self.assertEqual(len(changed), 3, changed)  # the three server stops, where pcron never fires in the watch


if __name__ == "__main__":
    unittest.main()
