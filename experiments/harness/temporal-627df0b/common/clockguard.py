"""Clock guard: the trial clock must run at the real rate (RULES-expA.md R6.13; Experiment B the same).

Found on 10 Oct 2026 (n8n/SPIKE-RESULTS.md, "Clock"): on this WSL2 machine the kernel's tick length was 9167 us
instead of 10000 us. So CLOCK_MONOTONIC, which every duration the harness measures is taken on and which every
sleep and timer of the systems under test runs on, ran 8.3 % slow. CLOCK_MONOTONIC_RAW, which tick and
frequency adjustments do not touch, matched Windows' clock. A trial may start only when:
  - the kernel's tick length is nominal (1e6 / USER_HZ microseconds), and
  - CLOCK_MONOTONIC keeps pace with CLOCK_MONOTONIC_RAW within RATE_TOL_PPM over a short window.
The NTP frequency correction alone is capped at 500 ppm, so a larger gap means the clock is being slewed by
something else (such as the tick).

Read-only: adjtimex is called with modes = 0, which changes nothing. Setting the tick is set_tick.py, which the
fellow runs as root.

    python3 clockguard.py [--seconds 2]    prints the check as JSON; exit 0 if the clock is fit for trials
"""
import argparse
import ctypes
import ctypes.util
import json
import os
import sys
import time

RATE_TOL_PPM = 1000.0
ADJ_TICK = 0x4000
_L = ctypes.c_long


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", _L), ("tv_usec", _L)]


class Timex(ctypes.Structure):
    """struct timex from <sys/timex.h> (glibc, 64-bit Linux): 208 bytes."""
    _fields_ = [("modes", ctypes.c_uint), ("offset", _L), ("freq", _L), ("maxerror", _L), ("esterror", _L),
                ("status", ctypes.c_int), ("constant", _L), ("precision", _L), ("tolerance", _L),
                ("time", _Timeval), ("tick", _L), ("ppsfreq", _L), ("jitter", _L), ("shift", ctypes.c_int),
                ("stabil", _L), ("jitcnt", _L), ("calcnt", _L), ("errcnt", _L), ("stbcnt", _L),
                ("tai", ctypes.c_int), ("_reserved", ctypes.c_int * 11)]


def _libc():
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def adjtimex(modes=0, tick=None):
    """Call adjtimex(2). modes=0 only reads. Returns (clock state, Timex); raises OSError on failure."""
    t = Timex()
    t.modes = modes
    if tick is not None:
        t.tick = tick
    state = _libc().adjtimex(ctypes.byref(t))
    if state == -1:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return state, t


def nominal_tick_us():
    return round(1_000_000 / os.sysconf("SC_CLK_TCK"))


def read_timex():
    """The kernel's clock settings, read only. freq is in ppm (the kernel keeps it scaled by 2**16)."""
    state, t = adjtimex(0)
    return {"state": state, "tick_us": int(t.tick), "freq_ppm": t.freq / 65536.0, "status": int(t.status),
            "offset": int(t.offset)}


def _stamp():
    """CLOCK_MONOTONIC and CLOCK_REALTIME, each against the mean of two CLOCK_MONOTONIC_RAW reads around them."""
    r0 = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    m = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    w = time.clock_gettime_ns(time.CLOCK_REALTIME)
    r1 = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    return (r0 + r1) / 2.0, m, w


def measure_rates(seconds=2.0):
    """Rate of CLOCK_MONOTONIC and CLOCK_REALTIME against CLOCK_MONOTONIC_RAW, in ppm (0 = same rate).
    The realtime figure includes any step the time service makes inside the window; it is recorded, not judged."""
    a = _stamp()
    time.sleep(seconds)
    b = _stamp()
    raw = b[0] - a[0]
    return {"window_raw_s": round(raw / 1e9, 6), "monotonic_ppm": round(((b[1] - a[1]) / raw - 1.0) * 1e6, 1),
            "realtime_ppm": round(((b[2] - a[2]) / raw - 1.0) * 1e6, 1)}


def evaluate(timex, rates, nominal, tol_ppm=RATE_TOL_PPM):
    """Pure decision from the readings. timex or rates is None when it could not be read."""
    reasons = []
    if timex is None:
        reasons.append("cannot read the kernel clock settings (adjtimex)")
    elif timex["tick_us"] != nominal:
        pct = (timex["tick_us"] / nominal - 1.0) * 100.0
        reasons.append(f"kernel tick is {timex['tick_us']} us, nominal {nominal} us: "
                       f"CLOCK_MONOTONIC runs {pct:+.2f} % off real time")
    if rates is None:
        reasons.append("cannot compare CLOCK_MONOTONIC with CLOCK_MONOTONIC_RAW")
    elif abs(rates["monotonic_ppm"]) > tol_ppm:
        reasons.append(f"CLOCK_MONOTONIC runs {rates['monotonic_ppm']:+.0f} ppm against CLOCK_MONOTONIC_RAW "
                       f"(limit +/-{tol_ppm:.0f} ppm)")
    return {"ok": not reasons, "reasons": reasons, "nominal_tick_us": nominal, "tol_ppm": tol_ppm,
            "timex": timex, "rates": rates}


def rate_from_stamps(pairs, window_s=10.0):
    """CLOCK_MONOTONIC against CLOCK_MONOTONIC_RAW across a trial, from (mono, raw) pairs read together (the
    recorder stamps both on every ping). Returns the largest deviation over any span of at least window_s raw
    seconds, and over the whole span, in ppm; None if no two stamps are window_s apart. This catches a slew in
    the middle of a trial (for example WSL's chronyd following a step of the Windows clock), which the checks at
    the start and end of the trial would miss."""
    pts = sorted((r, m) for m, r in pairs if m is not None and r is not None)
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < window_s:
        return None
    worst, j = 0.0, 0
    for i in range(len(pts)):
        while j < len(pts) and pts[j][0] - pts[i][0] < window_s:
            j += 1
        if j == len(pts):
            break
        ppm = ((pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0]) - 1.0) * 1e6
        if abs(ppm) > abs(worst):
            worst = ppm
    whole = ((pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]) - 1.0) * 1e6
    return {"stamps": len(pts), "span_raw_s": round(pts[-1][0] - pts[0][0], 3), "window_s": window_s,
            "worst_window_ppm": round(worst, 1), "whole_ppm": round(whole, 1)}


def check_stamps(pairs, window_s=10.0, tol_ppm=RATE_TOL_PPM):
    """Judge the clock across a trial from paired stamps. Too few stamps fails: the clock is then unverified."""
    r = rate_from_stamps(pairs, window_s)
    reasons = []
    if r is None:
        reasons.append(f"too few raw-clock stamps to check the trial clock (need two at least {window_s:.0f} s apart)")
    elif abs(r["worst_window_ppm"]) > tol_ppm:
        reasons.append(f"CLOCK_MONOTONIC ran {r['worst_window_ppm']:+.0f} ppm against CLOCK_MONOTONIC_RAW in a "
                       f"{window_s:.0f} s span during the trial (limit +/-{tol_ppm:.0f} ppm)")
    return {"ok": not reasons, "reasons": reasons, "rate": r, "tol_ppm": tol_ppm}


def check(seconds=2.0, tol_ppm=RATE_TOL_PPM):
    """Read and judge the trial clock. Never raises: a reading that fails makes the check fail."""
    try:
        timex = read_timex()
    except (OSError, AttributeError):
        timex = None
    try:
        rates = measure_rates(seconds)
    except (OSError, AttributeError, ValueError):
        rates = None
    try:
        nominal = nominal_tick_us()
    except (OSError, ValueError, AttributeError):
        nominal = 10000
    out = evaluate(timex, rates, nominal, tol_ppm)
    out["checked_at_wall"] = time.time()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check that the trial clock runs at the real rate (read-only).")
    ap.add_argument("--seconds", type=float, default=2.0, help="length of the rate-measuring window")
    a = ap.parse_args(argv)
    r = check(a.seconds)
    print(json.dumps(r, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
