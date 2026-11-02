#!/usr/bin/env python3
"""Put the kernel's clock tick back to nominal before a trial, without changing the clock's rate.

The harnesses refuse to start a trial unless the kernel's tick is nominal (10000 us at 100 ticks a second), because
every duration they measure is taken on CLOCK_MONOTONIC. On some cloud machines chrony can leave the tick at
10001 us. This moves that offset into the frequency correction (100 ppm for each microsecond of tick), so the clock
keeps the same rate. The setup scripts stop chrony first (experiments/setup/common.sh), so that nothing moves the tick again.
Run as root: python3 experiments/setup/clock_tick.py
"""
import ctypes
import ctypes.util
import os
import sys

ADJ_FREQUENCY, ADJ_TICK = 0x0002, 0x4000
_L = ctypes.c_long


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", _L), ("tv_usec", _L)]


class Timex(ctypes.Structure):
    """struct timex from <sys/timex.h> (glibc, 64-bit Linux), as in the harnesses' common/clockguard.py."""
    _fields_ = [("modes", ctypes.c_uint), ("offset", _L), ("freq", _L), ("maxerror", _L), ("esterror", _L),
                ("status", ctypes.c_int), ("constant", _L), ("precision", _L), ("tolerance", _L),
                ("time", _Timeval), ("tick", _L), ("ppsfreq", _L), ("jitter", _L), ("shift", ctypes.c_int),
                ("stabil", _L), ("jitcnt", _L), ("calcnt", _L), ("errcnt", _L), ("stbcnt", _L),
                ("tai", ctypes.c_int), ("_reserved", ctypes.c_int * 11)]


def adjtimex(t):
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    if libc.adjtimex(ctypes.byref(t)) == -1:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return t


def main():
    nominal = round(1_000_000 / os.sysconf("SC_CLK_TCK"))
    now = adjtimex(Timex())
    tick, freq = int(now.tick), int(now.freq)
    if tick == nominal:
        print(f"clock tick: {tick} us, nominal; nothing to change")
        return 0
    new_freq = freq + (tick - nominal) * 100 * 65536          # 1 us of tick in 10000 us is 100 ppm; freq is ppm * 2**16
    if abs(new_freq) > 500 * 65536:
        print(f"clock tick: {tick} us, but moving it into the frequency would need {new_freq / 65536:.1f} ppm")
        return 1
    t = Timex()
    t.modes, t.tick, t.freq = ADJ_TICK | ADJ_FREQUENCY, nominal, new_freq
    adjtimex(t)
    after = adjtimex(Timex())
    print(f"clock tick: {tick} -> {int(after.tick)} us; frequency {freq / 65536:.3f} -> {after.freq / 65536:.3f} ppm")
    return 0 if int(after.tick) == nominal else 1


if __name__ == "__main__":
    sys.exit(main())
