"""Experiment B's wall/monotonic clock-consistency rule (n8n/clockwall_b.py), the one implementation that the harness
applies at the end of a trial and the scorer recomputes from pings.jsonl (RULES-expB B.9): each interval between
paired recorder stamps agrees within 0.1 s plus 1,000 ppm of the interval, over at least 10 s of coverage, and
missing paired evidence fails. Pure: nothing here touches a service, a clock or a file.

Run:  python -m unittest tests.test_clockwall_b      (STOPCHECK_CLOCKWALL_B=<path> tests another copy, as
n8n/mutate_b.py does)
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RULE = os.environ.get("STOPCHECK_CLOCKWALL_B", os.path.join(os.path.dirname(HERE), "n8n", "clockwall_b.py"))
_spec = importlib.util.spec_from_file_location("clockwall_b_under_test", RULE)
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)


class WallClockRule(unittest.TestCase):
    def test_normal_constant_clock_offset_and_bounded_drift_pass(self):
        for ppm in (-1000, 0, 1000):
            pairs = [(100 + i, 1791866000 + i * (1 + ppm / 1_000_000)) for i in range(101)]
            with self.subTest(ppm=ppm):
                report = C.check_wall_stamps(pairs)
                self.assertTrue(report["ok"], report["reasons"])
                self.assertEqual(report["rejected_stamps"], 0)
                self.assertEqual(report["span_mono_s"], 100)

    def test_backward_step_is_detected(self):
        pairs = [(i, 1000 + i - (1 if i >= 20 else 0)) for i in range(41)]
        report = C.check_wall_stamps(pairs)
        self.assertFalse(report["ok"])
        self.assertLess(report["worst_interval"]["deviation_s"], 0)

    def test_small_repeated_forward_then_backward_steps_cannot_hide_in_whole_span(self):
        # The largest adjacent deviation is 12.5 ms, and the whole-span deviation is zero.
        # A 250 ms excursion between them must still fail, so adjacent+whole checks alone are insufficient.
        pairs = [(i, 1000 + i + max(0, min(i, 40 - i)) * 0.0125) for i in range(61)]
        report = C.check_wall_stamps(pairs)
        self.assertLess(report["maximum_adjacent_deviation_s"], 0.1)
        self.assertAlmostEqual(report["whole_span_deviation_s"], 0)
        self.assertFalse(report["ok"])
        self.assertGreater(report["worst_interval"]["excess_s"], 0.1)

    def test_missing_nonfinite_or_insufficient_sampler_coverage_is_unverified(self):
        for pairs in ([], [(1, 1000)], [(1, 1000), (5, 1004)],
                      [(1, 1000), (11, 1010), (None, 1011)],
                      [(1, 1000), (11, 1010), (12, float("nan"))]):
            with self.subTest(pairs=pairs):
                self.assertFalse(C.check_wall_stamps(pairs)["ok"])

    def test_each_interval_may_deviate_by_0_1_s_plus_1000_ppm_of_it(self):
        self.assertEqual((C.WALL_STEP_TOL_S, C.WALL_DRIFT_TOL_PPM, C.WALL_COVERAGE_S), (0.1, 1000.0, 10.0))
        for deviation, ok in ((0.19, True), (0.21, False)):     # a 100 s interval may deviate by 0.1 + 0.1 s
            with self.subTest(deviation=deviation):
                report = C.check_wall_stamps([(0.0, 5000.0), (100.0, 5100.0 + deviation)])
                self.assertIs(report["ok"], ok, report["reasons"])
                self.assertAlmostEqual(report["worst_interval"]["allowed_deviation_s"], 0.2)

    def test_recorder_rows_pair_each_monotonic_stamp_with_its_wall_stamp(self):
        """The recorder writes `mono` and the wall time `t` on every ping (langgraph/recorder.py); a row without a
        finite wall stamp is rejected evidence, so the check fails."""
        rows = [{"mono": 1.0, "t": 1001.0, "raw": 0.9}, {"mono": 12.0, "t": 1012.0}, {"mono": 13.0}]
        self.assertEqual(C.recorder_pairs(rows), [(1.0, 1001.0), (12.0, 1012.0), (13.0, None)])
        report = C.check_recorder_rows(rows)
        self.assertFalse(report["ok"])
        self.assertEqual(report["rejected_stamps"], 1)
        self.assertTrue(C.check_recorder_rows(rows[:2])["ok"])


if __name__ == "__main__":
    unittest.main()
