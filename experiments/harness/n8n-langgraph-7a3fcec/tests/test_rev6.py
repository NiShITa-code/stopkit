"""Sixth-revision regression tests: the scorer on synthetic scenarios against expected values that were written
independently from RULES-expA.md and the scenario data (expected_rev6.json), never from the scorer's output.

A disagreement is a failure until it is resolved in writing: either the scorer is fixed, or the expected value is
corrected with a note in expected_rev6.json citing the rule that decides it."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scenarios_rev6 as sc  # noqa: E402
import synth  # noqa: E402

EXPECTED = os.path.join(HERE, "expected_rev6.json")


def load_expected():
    with open(EXPECTED, encoding="utf-8") as f:
        return json.load(f)


class Rev6Scenarios(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.expected = load_expected()

    def test_every_scenario_has_expected_values(self):
        self.assertEqual(sorted(sc.S), sorted(self.expected["scenarios"]))

    def test_scenarios(self):
        failures = []
        for name, (fn, rows, _) in sc.S.items():
            exp = self.expected["scenarios"][name]["rows"]
            v, scored = fn().score()
            for row, carrier, outcome in rows:
                key = f"{row} | {carrier} | {outcome}"
                want = exp[key]
                if row == "valid":
                    got = "valid" if v["valid"] else "invalid"
                else:
                    r = synth.find(scored, row, carrier, outcome, field=None)
                    got = r["kind"]
                    for field, wv in (want.get("also") or {}).items():
                        gv = r.get(field) if not field.startswith("evidence.") else \
                            (r.get("evidence") or {}).get(field.split(".", 1)[1])
                        if gv != wv:
                            failures.append(f"{name}: {key}: {field} is {gv!r}, expected {wv!r}")
                if got != want["kind"]:
                    failures.append(f"{name}: {key}: kind is {got!r}, expected {want['kind']!r}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
