"""Validity guards for stop-test trials (WSL, no Docker).

A trial only counts if the machine it ran on did not change under it. Each trial records a
snapshot at its start and end; a trial is void if WSL restarted (boot_id or PID 1's start time
changed) or the kernel OOM-killed something during it. A batch stops before a trial if the
Windows C: drive is below the free-space floor, because WSL's disk lives on C: and a full disk
corrupts runs (`df /` inside WSL reports the virtual disk, which is the wrong number).

Also recorded: whether Windows interop is on (a Windows-side process the agent starts would be
invisible to the Linux process table), the network namespace inode, and hashes of the installed
Python and Debian packages so every row can be tied to an exact software state.
"""
import hashlib
import os
import subprocess

DISK_FLOOR_GB_DEFAULT = 10.0


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def boot_id():
    v = _read("/proc/sys/kernel/random/boot_id")
    return v.strip() if v else None


def pid1_start_ticks():
    """Field 22 of /proc/1/stat: PID 1's start time in clock ticks since boot.
    Catches a distro restart that keeps the same VM (and so the same boot_id)."""
    v = _read("/proc/1/stat")
    if not v:
        return None
    # the command name (field 2) is in parentheses and may contain spaces
    rest = v[v.rindex(")") + 2:].split()
    return int(rest[19])  # fields 3.. start at index 0, so field 22 is index 19


def oom_kills():
    v = _read("/proc/vmstat")
    if not v:
        return None
    for line in v.splitlines():
        if line.startswith("oom_kill "):
            return int(line.split()[1])
    return None


def c_drive_free_gb(path="/mnt/c"):
    try:
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / 1e9, 2)
    except OSError:
        return None


def interop_state():
    v = _read("/proc/sys/fs/binfmt_misc/WSLInterop")
    if v is None:
        return "not registered"
    return v.splitlines()[0].strip() if v else "unknown"


def netns_inode():
    try:
        return os.stat("/proc/self/ns/net").st_ino
    except OSError:
        return None


def _hash_cmd(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60).stdout
        return hashlib.sha256(out).hexdigest()[:16]
    except Exception as e:  # noqa: BLE001 - recorded, not fatal
        return f"failed: {e}"


def package_hashes(python_exe):
    return {"pip_freeze_sha256": _hash_cmd([python_exe, "-m", "pip", "freeze", "--all"]),
            "dpkg_sha256": _hash_cmd(["dpkg-query", "-W", "-f", "${Package} ${Version}\\n"])}


def snapshot():
    return {"boot_id": boot_id(), "pid1_start_ticks": pid1_start_ticks(), "oom_kill": oom_kills(),
            "c_free_gb": c_drive_free_gb(), "interop": interop_state(), "netns_inode": netns_inode()}


def violations(start, end):
    """Reasons a trial is void, comparing its start and end snapshots."""
    out = []
    if start.get("boot_id") != end.get("boot_id"):
        out.append("WSL VM restarted during the trial (boot_id changed)")
    if start.get("pid1_start_ticks") != end.get("pid1_start_ticks"):
        out.append("WSL distro restarted during the trial (PID 1 start time changed)")
    s, e = start.get("oom_kill"), end.get("oom_kill")
    if s is not None and e is not None and e > s:
        out.append(f"kernel OOM-killed {e - s} process(es) during the trial")
    if start.get("netns_inode") != end.get("netns_inode"):
        out.append("network namespace changed during the trial")
    return out


def disk_ok(floor_gb=DISK_FLOOR_GB_DEFAULT):
    free = c_drive_free_gb()
    return (free is not None and free >= floor_gb), free


if __name__ == "__main__":
    import json
    import sys
    snap = snapshot()
    print(json.dumps(snap | package_hashes(sys.executable), indent=1))
