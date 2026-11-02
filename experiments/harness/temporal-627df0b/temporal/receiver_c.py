#!/usr/bin/env python3
"""Experiment C receiver: the independent commit record of every effect (DESIGN-expC.md section 2; RULES-expC.md C.2).

It is langgraph/recorder.py's request handler, unmodified (GET /ping is logged as recorder.py logs it; GET
/health answers 200), plus one route of our own:

  POST /effect   body: one JSON object (the effect record, DESIGN-expC section 3).
                 The receiver adds a sequence number and its arrival stamps, CLOCK_REALTIME ("t"),
                 CLOCK_MONOTONIC ("mono") and CLOCK_MONOTONIC_RAW ("raw"), taken on arrival as recorder.py
                 takes them; writes one JSON line and flushes it; and only then answers 200. It never
                 deduplicates. A malformed body is answered 400 and logged as {"route": "effect", "bad": ...}.

Binds to 127.0.0.1 only. It is a stand-in outside service (label S): its only job is to log what arrives.

    python3 receiver_c.py --port 8875 --out <trial>/receiver.jsonl
"""
import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "langgraph"))

RESERVED = ("route", "seq", "t", "mono", "raw", "bad")
MAX_BODY = 64 * 1024
_SEQ = {"n": 0}
_SEQ_LOCK = threading.Lock()


def stamps():
    """Arrival stamps, in the order recorder.py takes them."""
    return time.time(), time.monotonic(), time.clock_gettime(time.CLOCK_MONOTONIC_RAW)


def next_seq(state=_SEQ, lock=_SEQ_LOCK):
    with lock:
        state["n"] += 1
        return state["n"]


def effect_line(body, arrival, seq):
    """Pure: (status, line) for one POST /effect body. `arrival` is (t, mono, raw)."""
    t, mono, raw = arrival
    try:
        rec = json.loads(body.decode("utf-8"))
        if not isinstance(rec, dict):
            raise ValueError("body is not a JSON object")
        clash = [k for k in RESERVED if k in rec]
        if clash:
            raise ValueError("body uses receiver field names: " + ", ".join(clash))
    except (ValueError, UnicodeDecodeError) as e:
        line = {"route": "effect", "seq": seq, "t": t, "mono": mono, "raw": raw, "bad": str(e)[:200],
                "body_head": body[:200].decode("utf-8", "replace")}
        return 400, line
    line = {"route": "effect", "seq": seq, "t": t, "mono": mono, "raw": raw}
    line.update(rec)
    return 200, line


def make_handler():
    """recorder.py's handler (do_GET unmodified) with a POST /effect route. recorder.py is imported here, not at
    module level, because it defines a Unix-socket server class that some platforms lack; the pure parts of this
    file (effect_line) stay importable for offline tests."""
    import recorder  # langgraph/recorder.py, unmodified

    class Handler(recorder.Handler):
        def do_POST(self):  # noqa: N802
            arrival = stamps()
            path = self.path.split("?", 1)[0]
            if path != "/effect":
                return self._reply(404, b"not found")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._reply(413, b"bad length")
            body = self.rfile.read(n)
            with recorder.LOCK:          # the same lock and file as recorder.py's /ping lines
                status, line = effect_line(body, arrival, next_seq())
                recorder.OUT.write(json.dumps(line) + "\n")
                recorder.OUT.flush()
            return self._reply(status, b"ok" if status == 200 else b"bad")

        def _reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    return Handler, recorder


def main(argv=None):
    from http.server import ThreadingHTTPServer
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8875)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.host != "127.0.0.1":
        raise SystemExit("the receiver binds to 127.0.0.1 only")
    Handler, recorder = make_handler()
    recorder.OUT = open(a.out, "a", encoding="utf-8")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"receiver_c listening on {a.host}:{a.port}, writing {a.out}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
