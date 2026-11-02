#!/usr/bin/env python3
"""Tests for clockguard.py and set_tick.py.

    python3 -m unittest -v common/test_clockguard.py      (from stopcheck/, in WSL; the live tests need Linux)

The decision logic and set_tick's control flow are tested with injected readings, so they run anywhere. The live
tests read this machine's clock (read-only) and check that the measured rate agrees with the kernel's own tick
and frequency settings: two independent routes to the same number. Nothing here changes the clock.
"""
import ctypes
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clockguard  # noqa: E402
import set_tick  # noqa: E402

LINUX = sys.platform.startswith("linux") and hasattr(clockguard.time, "CLOCK_MONOTONIC_RAW")
TIMEX_OK = {"state": 0, "tick_us": 10000, "freq_ppm": -33.3, "status": 0x2000, "offset": 0}
RATES_OK = {"window_raw_s": 2.0, "monotonic_ppm": -33.0, "realtime_ppm": -33.0}


def tx(tick):
    return dict(TIMEX_OK, tick_us=tick)


def rates(ppm):
    return dict(RATES_OK, monotonic_ppm=ppm)


class Evaluate(unittest.TestCase):
    def test_nominal_clock_passes(self):
        r = clockguard.evaluate(TIMEX_OK, RATES_OK, 10000)
        self.assertTrue(r["ok"])
        self.assertEqual(r["reasons"], [])

    def test_the_tick_found_on_10_oct_fails_on_both_counts(self):
        r = clockguard.evaluate(tx(9167), rates(-83300.0), 10000)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["reasons"]), 2)
        self.assertIn("9167", r["reasons"][0])
        self.assertIn("-8.33 %", r["reasons"][0])
        self.assertIn("-83300 ppm", r["reasons"][1])

    def test_rate_limit_is_inclusive_and_symmetric(self):
        for ppm, ok in ((1000.0, True), (-1000.0, True), (1000.1, False), (-1000.1, False), (499.0, True)):
            self.assertEqual(clockguard.evaluate(TIMEX_OK, rates(ppm), 10000)["ok"], ok, ppm)

    def test_off_rate_with_nominal_tick_fails(self):
        r = clockguard.evaluate(TIMEX_OK, rates(-2500.0), 10000)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["reasons"]), 1)
        self.assertIn("CLOCK_MONOTONIC_RAW", r["reasons"][0])

    def test_off_tick_fails_even_if_the_window_looked_fine(self):
        self.assertFalse(clockguard.evaluate(tx(10001), RATES_OK, 10000)["ok"])

    def test_unreadable_inputs_fail_never_pass(self):
        self.assertFalse(clockguard.evaluate(None, RATES_OK, 10000)["ok"])
        self.assertFalse(clockguard.evaluate(TIMEX_OK, None, 10000)["ok"])
        r = clockguard.evaluate(None, None, 10000)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["reasons"]), 2)

    def test_check_never_raises_and_fails_where_it_cannot_read(self):
        r = clockguard.check(0.05)
        self.assertIn("ok", r)
        if not LINUX:
            self.assertFalse(r["ok"])


class Stamps(unittest.TestCase):
    """The whole-trial check from the recorder's paired (mono, raw) stamps."""

    @staticmethod
    def pings(rate=1.0, n=200, step=1.0, raw0=5000.0, slew=None):
        """One stamp per `step` raw seconds; mono advances at `rate`, except inside slew=(start, end, rate)."""
        out, mono = [], 1000.0
        for k in range(n):
            raw = raw0 + k * step
            if k:
                r = slew[2] if slew and slew[0] <= raw - raw0 < slew[1] else rate
                mono += step * r
            out.append((mono, raw))
        return out

    def test_clock_at_the_real_rate_passes(self):
        r = clockguard.check_stamps(self.pings())
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["rate"]["worst_window_ppm"], 0.0, delta=0.5)

    def test_the_slow_clock_of_10_oct_fails(self):
        r = clockguard.check_stamps(self.pings(rate=0.9167))
        self.assertFalse(r["ok"])
        self.assertAlmostEqual(r["rate"]["whole_ppm"], -83300.0, delta=5.0)

    def test_a_short_slew_inside_the_trial_is_caught(self):
        # a 2 s slew at chrony's maximum (1/12 slow) in the middle of a 200 s trial: the whole-trial average stays
        # inside the limit (about -840 ppm) and start and end checks would both pass, but a 10 s span is far off
        r = clockguard.check_stamps(self.pings(slew=(100, 102, 1 - 1 / 12)))
        self.assertLess(abs(r["rate"]["whole_ppm"]), clockguard.RATE_TOL_PPM)
        self.assertFalse(r["ok"])
        self.assertLess(r["rate"]["worst_window_ppm"], -15000)

    def test_a_normal_frequency_correction_passes(self):
        self.assertTrue(clockguard.check_stamps(self.pings(rate=1 + 400e-6))["ok"])
        self.assertFalse(clockguard.check_stamps(self.pings(rate=1 + 1500e-6))["ok"])

    def test_too_few_stamps_fails(self):
        self.assertFalse(clockguard.check_stamps(self.pings(n=5))["ok"])  # 4 s span < 10 s window
        self.assertFalse(clockguard.check_stamps([])["ok"])

    def test_stamps_without_raw_are_ignored_not_trusted(self):
        pts = self.pings()
        mixed = [(m, None) if k % 2 else (m, r) for k, (m, r) in enumerate(pts)]
        self.assertTrue(clockguard.check_stamps(mixed)["ok"])
        self.assertFalse(clockguard.check_stamps([(m, None) for m, _ in pts])["ok"])

    def test_order_of_stamps_does_not_matter(self):
        pts = self.pings(slew=(50, 58, 0.92))
        self.assertEqual(clockguard.check_stamps(pts)["rate"], clockguard.check_stamps(pts[::-1])["rate"])


class Struct(unittest.TestCase):
    @unittest.skipUnless(LINUX and ctypes.sizeof(ctypes.c_long) == 8, "64-bit Linux layout")
    def test_timex_matches_the_glibc_layout(self):
        self.assertEqual(ctypes.sizeof(clockguard.Timex), 208)
        self.assertEqual(clockguard.Timex.tick.offset, 88)
        self.assertEqual(clockguard.Timex.freq.offset, 16)
        self.assertEqual(clockguard.Timex.status.offset, 40)


@unittest.skipUnless(LINUX, "needs Linux (adjtimex, CLOCK_MONOTONIC_RAW)")
class Live(unittest.TestCase):
    """Read-only checks on this machine's real clock."""

    def test_tick_is_in_the_kernel_range(self):
        nominal = clockguard.nominal_tick_us()
        t = clockguard.read_timex()["tick_us"]
        self.assertGreaterEqual(t, nominal * 0.9)
        self.assertLessEqual(t, nominal * 1.1)

    def test_measured_rate_agrees_with_the_kernel_settings(self):
        # expected rate of CLOCK_MONOTONIC against RAW: (tick / nominal) * (1 + freq) - 1
        nominal = clockguard.nominal_tick_us()
        t0 = clockguard.read_timex()
        m = clockguard.measure_rates(1.0)
        t1 = clockguard.read_timex()
        self.assertEqual(t0["tick_us"], t1["tick_us"], "the tick changed during the test")
        expect = ((t0["tick_us"] / nominal) * (1 + t0["freq_ppm"] / 1e6) - 1) * 1e6
        # 300 ppm covers the time service's own slewing (its offset correction) during the window
        self.assertAlmostEqual(m["monotonic_ppm"], expect, delta=300.0, msg=f"measured {m}, expected {expect:.0f}")

    def test_check_verdict_follows_the_tick(self):
        r = clockguard.check(0.5)
        nominal_tick = r["timex"]["tick_us"] == r["nominal_tick_us"]
        if not nominal_tick:
            self.assertFalse(r["ok"])
        elif abs(r["rates"]["monotonic_ppm"]) <= r["tol_ppm"]:
            self.assertTrue(r["ok"])


class SetTick(unittest.TestCase):
    def fake_check(self, tick, ok, ppm=0.0):
        return lambda seconds: {"ok": ok, "reasons": [] if ok else ["off"], "nominal_tick_us": 10000,
                                "timex": None if tick is None else tx(tick), "rates": rates(ppm)}

    def no_apply(self, tick):
        raise AssertionError("apply must not be called")

    def test_dry_run_changes_nothing(self):
        self.assertEqual(set_tick.main([], geteuid=lambda: 0, apply=self.no_apply,
                                       check=self.fake_check(9167, False, -83300.0)), 0)

    def test_apply_refused_without_root(self):
        self.assertEqual(set_tick.main(["--apply"], geteuid=lambda: 1000, apply=self.no_apply,
                                       check=self.fake_check(9167, False, -83300.0)), 2)

    def test_nothing_to_do_when_nominal(self):
        self.assertEqual(set_tick.main(["--apply"], geteuid=lambda: 0, apply=self.no_apply,
                                       check=self.fake_check(10000, True)), 0)

    def test_unreadable_tick_changes_nothing(self):
        self.assertEqual(set_tick.main(["--apply"], geteuid=lambda: 0, apply=self.no_apply,
                                       check=self.fake_check(None, False)), 2)

    def test_apply_sets_nominal_and_reports_the_recheck(self):
        calls = []
        checks = iter([self.fake_check(9167, False, -83300.0), self.fake_check(10000, True, -20.0)])
        rc = set_tick.main(["--apply"], geteuid=lambda: 0, apply=lambda t: calls.append(t) or 0,
                           check=lambda s: next(checks)(s))
        self.assertEqual(calls, [10000])
        self.assertEqual(rc, 0)

    def test_apply_that_does_not_fix_the_rate_exits_nonzero(self):
        checks = iter([self.fake_check(9167, False, -83300.0), self.fake_check(10000, False, -83300.0)])
        rc = set_tick.main(["--apply"], geteuid=lambda: 0, apply=lambda t: 0, check=lambda s: next(checks)(s))
        self.assertEqual(rc, 1)

    def test_kernel_refusal_changes_nothing(self):
        def refuse(t):
            raise OSError(1, "Operation not permitted")
        self.assertEqual(set_tick.main(["--apply"], geteuid=lambda: 0, apply=refuse,
                                       check=self.fake_check(9167, False, -83300.0)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
