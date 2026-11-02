"""Reporting for planted work that runs inside a platform's own long-lived process: a request in an MCP
server, a node of a LangGraph run, an activity in a Temporal worker. Installed read-only for the test
accounts as /var/lib/stopkit/bin/stopkit_report.py; the platform's code imports it from there.

Standalone: the standard library only. Such work is not a process of its own, so it reports:
  hello  once, with the process that hosts it (the recorder watches that process, so its end is seen) and a
         random instance number that tells this run of the work from a retry or a re-run of it;
  beat   each effect it issues, sent to the effect sink and reported to the truth with the same number;
  end    from its exit hook (a finally block), when the work itself ends, however it ends.
A beat or an end that never comes leaves the truth indefinite: silence is never read as the work's end.

    from stopkit_report import Work
    w = Work(truth_socket, sink_socket, tag, item)
    w.hello()
    try:
        while ...: w.beat(); ...
    finally:
        w.end()

Sends never wait: a message that does not fit is lost, and the missing number shows it.
"""
import json
import os
import secrets
import socket
import threading
import time


def _start_time():
    with open('/proc/self/stat', 'rb') as f:
        s = f.read().decode('utf-8', 'replace')
    return int(s[s.rindex(')') + 2:].split()[19])


class Work:
    def __init__(self, truth, sink, tag, item):
        self.truth, self.sink, self.tag, self.item = truth, sink, tag, item
        self.inst = secrets.token_hex(4)
        self.n = 0
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def _send(self, path, msg):
        if not path:
            return
        try:
            self.sock.sendto(json.dumps(msg, separators=(',', ':')).encode(), path)
        except OSError:
            pass                              # full, or the receiver is gone: never wait

    def hello(self):
        self._send(self.truth, {'type': 'hello', 'tag': self.tag, 'item': self.item, 'pid': os.getpid(),
                                'start': _start_time(), 't': time.monotonic(), 'inst': self.inst, 'inproc': True})

    def beat(self):
        with self.lock:
            self.n += 1
            n = self.n
        t = time.monotonic()
        self._send(self.sink, {'tag': self.tag, 'item': self.item, 'n': n, 't': t, 'inst': self.inst})
        self._send(self.truth, {'type': 'beat', 'tag': self.tag, 'item': self.item, 'n': n, 't': t,
                                'inst': self.inst})

    def end(self, why=''):
        self._send(self.truth, {'type': 'end', 'tag': self.tag, 'item': self.item, 't': time.monotonic(),
                                'inst': self.inst, 'why': str(why)[:200]})

    def beat_while(self, period=0.2):
        """Beat every period from a thread of its own until the returned function is called, for work that
        blocks (a synchronous call, a wait on a child process) and cannot beat between steps."""
        done = threading.Event()

        def loop():
            due = time.monotonic()
            while not done.is_set():
                self.beat()
                due += period
                done.wait(max(0.0, due - time.monotonic()))
        threading.Thread(target=loop, daemon=True, name=f'stopkit-beat-{self.tag}').start()
        return done.set
