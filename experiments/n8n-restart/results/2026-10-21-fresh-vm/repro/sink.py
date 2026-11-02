"""Standalone sink for the B.11 reproductions: appends each POST body with its arrival time. No StopCheck code.
Usage: python3 -I sink.py PORT OUTFILE"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, PATH = int(sys.argv[1]), sys.argv[2]
LOCK = threading.Lock()


class Sink(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw.decode(errors="replace")}
        with LOCK, open(PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "body": body}) + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Sink).serve_forever()
