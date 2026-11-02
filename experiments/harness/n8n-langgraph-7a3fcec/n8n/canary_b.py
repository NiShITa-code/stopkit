#!/usr/bin/env python3
"""Experiment B canary: usage `canary_b.py <id> <trial> [max_s] [once]`.

Sends GET /ping?canary=<id>&trial=<trial>&n=<k>&pid=... to the recorder about once per second, like
../langgraph/canary.py, and adds what an isolation check needs about the process that sends it:
  uid, mntns, netns (namespace links of /proc/self/ns), cgroup (/proc/self/cgroup), mntc (whether /mnt/c/Windows
  is visible), mnts (whether this trial's linux-vm host sentinel /mnt/stopcheck-sentinel-<trial> is visible; it
  reads False on the wsl profile, which places none), and smono (the sender's CLOCK_MONOTONIC reading when it sends).
  max_s  stop after this many seconds (default: run until killed)
  once   send one ping and exit
  PILOT_RECORDER_SOCK  if set, ping through this Unix socket instead of TCP 127.0.0.1:8765, so work outside the
                       trial's network namespace can still reach the recorder
Prints nothing; recorder errors are ignored.
"""
import http.client
import os
import socket
import sys
import time
import urllib.parse

SOCK = os.environ.get("PILOT_RECORDER_SOCK")


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=2)
        self.sock_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(self.sock_path)


def _link(p):
    try:
        return os.readlink(p)
    except OSError:
        return None


def sentinel(trial):
    """The linux-vm host sentinel path; the same formula as harness_b.host_sentinel."""
    return f"/mnt/stopcheck-sentinel-{trial}"


def identity(trial=None):
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as f:
            cg = f.read().strip().replace("\n", "|")
    except OSError:
        cg = None
    return {"uid": str(os.getuid()), "mntns": _link("/proc/self/ns/mnt"), "netns": _link("/proc/self/ns/net"),
            "cgroup": cg, "mntc": str(os.path.exists("/mnt/c/Windows")),
            "mnts": str(trial is not None and os.path.exists(sentinel(trial))), "pid": str(os.getpid()),
            "pgid": str(os.getpgid(0)), "sid": str(os.getsid(0))}


def ping(name, trial, n, ident):
    q = dict(ident, canary=name, trial=trial, n=str(n), smono=f"{time.monotonic():.6f}")
    path = "/ping?" + urllib.parse.urlencode(q)
    try:
        c = UnixHTTP(SOCK) if SOCK else http.client.HTTPConnection("127.0.0.1", 8765, timeout=2)
        c.request("GET", path)
        c.getresponse().read()
        c.close()
    except Exception:  # noqa: BLE001
        pass


def main():
    name, trial = sys.argv[1], sys.argv[2]
    max_s = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] not in ("", "0") else None
    once = len(sys.argv) > 4 and sys.argv[4] == "once"
    ident = identity(trial)
    t0, n = time.monotonic(), 0
    while True:
        n += 1
        ping(name, trial, n, ident)
        if once or (max_s is not None and time.monotonic() - t0 >= max_s):
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
