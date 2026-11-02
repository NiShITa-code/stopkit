#!/usr/bin/env python3
"""Canary: usage `canary.py <id> <trial> [delay_s] [once]`.

Loops forever, sending GET /ping?canary=<id>&trial=<trial>&n=<k>&pid=<pid>
to the recorder about once per second. Prints nothing (so it behaves the same
whatever stdout/stderr it inherited). Recorder errors are ignored.

stopcheck additions (the default behaviour above is unchanged):
  delay_s  stay silent for this many seconds before the first ping (dormant work that is
           due later: alive in the process table but sending nothing at the time of a stop)
  once     send one ping and exit (for an OS schedule such as a crontab entry)
  PILOT_RECORDER_SOCK  if set, ping through this Unix socket instead of TCP, so work running
           outside the trial's network namespace (an OS cron job) can still reach the recorder
"""
import http.client
import os
import socket
import sys
import time
import urllib.parse
import urllib.request

RECORDER = os.environ.get("PILOT_RECORDER_URL", "http://127.0.0.1:8765")
SOCK = os.environ.get("PILOT_RECORDER_SOCK")


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=2)
        self.sock_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(self.sock_path)


def _send(q):
    if SOCK:
        c = _UnixHTTP(SOCK)
        try:
            c.request("GET", f"/ping?{q}")
            c.getresponse().read()
        finally:
            c.close()
    else:
        urllib.request.urlopen(f"{RECORDER}/ping?{q}", timeout=2).read()


def main():
    name, trial = sys.argv[1], sys.argv[2]
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    once = len(sys.argv) > 4 and sys.argv[4] == "once"
    if delay > 0:
        time.sleep(delay)
    n = 0
    while True:
        n += 1
        q = urllib.parse.urlencode({"canary": name, "trial": trial, "n": n, "pid": os.getpid()})
        try:
            _send(q)
        except Exception:
            pass
        if once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
