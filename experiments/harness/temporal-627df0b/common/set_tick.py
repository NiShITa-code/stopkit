"""Set the kernel's tick length back to nominal (a stop-gap; not the fix for the slow clock of 10 Oct 2026).

The cause was two time services fighting (RULES-expA.md R6.13): WSL's own chronyd, which follows the Windows clock,
rewrites the tick on every update, so a tick set by hand holds only until chrony's next update while
systemd-timesyncd still runs. The fix is to turn off systemd-timesyncd in WSL and resync the Windows clock.

Run it yourself in WSL, as root:
    python3 /mnt/c/work/agent-shutdown-test/stopcheck/common/set_tick.py           shows what it would do; changes nothing
    python3 /mnt/c/work/agent-shutdown-test/stopcheck/common/set_tick.py --apply   sets the tick, then checks the clock again

What it changes: only the kernel's tick length (adjtimex with ADJ_TICK), to 1e6 / USER_HZ microseconds (10000 on
this machine). It leaves the frequency correction to the time service, writes no files and installs nothing.
The setting lasts until the WSL VM restarts (wsl --shutdown, or WSL stopping when idle); the harness checks the
clock before every trial and refuses to start if it is off again.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clockguard  # noqa: E402


def apply_tick(tick):
    """Set the tick (root only). Returns the clock state; raises OSError on failure."""
    state, _ = clockguard.adjtimex(clockguard.ADJ_TICK, tick=tick)
    return state


def main(argv=None, geteuid=getattr(os, "geteuid", lambda: -1), apply=apply_tick, check=clockguard.check):
    ap = argparse.ArgumentParser(description="Set the kernel tick to nominal (needs root with --apply).")
    ap.add_argument("--apply", action="store_true", help="make the change; without it nothing is changed")
    a = ap.parse_args(argv)
    before = check(2.0)
    nominal = before["nominal_tick_us"]
    tick = (before.get("timex") or {}).get("tick_us")
    print("before:", json.dumps({"ok": before["ok"], "reasons": before["reasons"], "tick_us": tick,
                                 "monotonic_ppm": (before.get("rates") or {}).get("monotonic_ppm")}))
    if tick is None:
        print("cannot read the kernel tick; nothing changed")
        return 2
    if tick == nominal:
        print(f"the tick is already nominal ({nominal} us); nothing to do")
        return 0 if before["ok"] else 1
    if not a.apply:
        print(f"would set the tick from {tick} us to {nominal} us; nothing changed (run with --apply, as root)")
        return 0
    if geteuid() != 0:
        print("--apply needs root (in WSL: wsl.exe -d Ubuntu -u root ...); nothing changed")
        return 2
    try:
        apply(nominal)
    except OSError as e:
        print(f"adjtimex refused the change: {e}; nothing changed")
        return 2
    after = check(5.0)
    print("after:", json.dumps({"ok": after["ok"], "reasons": after["reasons"],
                                "tick_us": (after.get("timex") or {}).get("tick_us"),
                                "monotonic_ppm": (after.get("rates") or {}).get("monotonic_ppm")}))
    return 0 if after["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
