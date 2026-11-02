#!/usr/bin/env python3
"""Ping recorder for pilot-01 (runs as its own process, outside the agent).

Every GET /ping?canary=..&trial=..&n=.. is written as one JSON line
{"t": <unix time on receipt>, "canary": .., "trial": .., "n": ..} to the output
file, plus "mono" (time.monotonic() on receipt; the harness measures all
intervals on this clock because the WSL2 wall clock can step). Extra query
fields sent by a canary (pid, run, attempt) are kept as extra keys.
GET /health answers 200 so the harness can check the recorder is up.
Binds to 127.0.0.1 only.
"""
import argparse
import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
OUT = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        t = time.time()
        mono = time.monotonic()  # system-wide monotonic clock (immune to wall-clock steps)
        raw = time.clock_gettime(time.CLOCK_MONOTONIC_RAW)  # R6.13: untouched by tick and frequency settings
        u = urlparse(self.path)
        if u.path == "/ping":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            rec = {"t": t, "mono": mono, "raw": raw, "canary": q.pop("canary", None), "trial": q.pop("trial", None)}
            try:
                rec["n"] = int(q.pop("n"))
            except (KeyError, ValueError):
                rec["n"] = None
            rec.update(q)  # extra fields (pid, run)
            line = json.dumps(rec) + "\n"
            with LOCK:
                OUT.write(line)
                OUT.flush()
            body = b"ok"
            self.send_response(200)
        elif u.path == "/health":
            body = b"ok"
            self.send_response(200)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep stderr quiet
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sock", default=None,
                    help="stopcheck: also listen on this Unix socket, so work outside the trial's network "
                         "namespace (an OS cron job) can reach the recorder")
    a = ap.parse_args()
    global OUT
    OUT = open(a.out, "a", encoding="utf-8")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    if a.sock:
        os.makedirs(os.path.dirname(a.sock), exist_ok=True)
        try:
            os.unlink(a.sock)
        except FileNotFoundError:
            pass
        usrv = UnixRecorder(a.sock, Handler)
        os.chmod(a.sock, 0o666)
        threading.Thread(target=usrv.serve_forever, daemon=True).start()
        print(f"recorder also listening on {a.sock}", flush=True)
    print(f"recorder listening on {a.host}:{a.port}, writing {a.out}", flush=True)
    srv.serve_forever()


class UnixRecorder(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        req, _ = super().get_request()
        return req, ("unix", 0)  # the handler expects a (host, port) client address


if __name__ == "__main__":
    main()
