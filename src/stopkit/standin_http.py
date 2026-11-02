"""The HTTP stand-in: an outside service that planted work reaches over HTTP, for work that can send nothing
else (an n8n Code node can make HTTP requests, but has no process ID and no access to the kit's sockets).

    python3 -I -B standin_http.py TRUTH_SOCKET SINK_SOCKET LOG

It runs as root for one run, on 127.0.0.1 at a port the system picks, and prints
"stopkit-standin-ready <port>" once it listens. Each POST to /effect carries one JSON object from the work:
  tag, item  which planted work sent it
  kind       beat (an effect), fire (one run of a trigger's job), end (the work's exit hook)
  n, inst    the effect's number, and the instance of the work (an execution ID, a random number)
  t          the sender's monotonic time, when it can stamp one; work that cannot is placed by its arrival
  w          optional: the sender's wall clock in milliseconds, kept with the arrival's wall clock, so that the
             delivery delay of work that cannot stamp a monotonic time is measured on every effect
For each it writes one line to LOG and passes the report on twice, as datagrams: to the sealed ground-truth
recorder (which logs it as a message from root, the stand-in) and to the effect sink (which the kit's
observers read). Its own arrival time ("arrival", CLOCK_MONOTONIC) goes with both.

It is the one piece both channels share: a request it never received is missing from both. That shared
failure cannot be seen by comparing the two; E0 measures how often requests are lost on the way.
"""
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KINDS = ('beat', 'fire', 'end')


class Standin:
    def __init__(self, truth, sink, log_path):
        self.truth, self.sink = truth, sink
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.log = open(log_path, 'a', encoding='utf-8')
        self.lock = threading.Lock()
        self.write({'ev': 'start', 'mono': time.monotonic(), 'pid': os.getpid()})

    def write(self, ev):
        with self.lock:
            self.log.write(json.dumps(ev, sort_keys=True, separators=(',', ':')) + '\n')
            self.log.flush()

    def forward(self, path, msg):
        try:
            self.sock.sendto(json.dumps(msg, separators=(',', ':')).encode(), path)
            return True
        except OSError:
            return False

    def receive(self, body, arrival):
        try:
            msg = json.loads(body)
            if not isinstance(msg, dict) or not isinstance(msg.get('tag'), str) or msg.get('kind', 'beat') not in KINDS:
                raise ValueError('not an object with a tag and a known kind')
        except ValueError as e:
            self.write({'ev': 'bad', 'mono': arrival, 'why': str(e)[:200], 'data': body[:200].decode('utf-8', 'replace')})
            return False
        kind = msg.get('kind', 'beat')
        t = msg.get('t') if isinstance(msg.get('t'), (int, float)) else None
        common = {'tag': msg['tag'], 'item': str(msg.get('item', ''))[:100], 'n': msg.get('n'),
                  'inst': None if msg.get('inst') is None else str(msg['inst'])[:100], 't': t, 'arrival': arrival,
                  'via': 'http'}
        if isinstance(msg.get('w'), (int, float)):
            # The sender's wall clock in milliseconds (an n8n Code node's Date.now()), kept with this machine's wall
            # clock at arrival: their difference is the delivery delay, measured on every effect of the run.
            common.update(w=msg['w'], arrival_wall=round(time.time(), 6))
        to_truth = self.forward(self.truth, {'type': kind, **common})
        to_sink = True
        if kind in ('beat', 'fire'):
            to_sink = self.forward(self.sink, {**common, 'fire': kind == 'fire'})
        self.write({'ev': 'effect', 'mono': arrival, 'kind': kind, 'msg': common, 'to_truth': to_truth,
                    'to_sink': to_sink})
        return True


def main(argv):
    if len(argv) != 4:
        print(__doc__.split('\n\n')[1], file=sys.stderr)
        return 2
    standin = Standin(argv[1], argv[2], argv[3])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            arrival = time.monotonic()
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            ok = self.path.split('?')[0] == '/effect' and standin.receive(body, arrival)
            self.send_response(200 if ok else 400)
            self.end_headers()
            self.wfile.write(b'ok' if ok else b'bad')

        def do_GET(self):
            self.send_response(200 if self.path == '/health' else 404)
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True

    def stop(_sig, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f'stopkit-standin-ready {server.server_address[1]}', flush=True)
    server.serve_forever(poll_interval=0.2)
    standin.write({'ev': 'end', 'mono': time.monotonic()})
    standin.log.close()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
