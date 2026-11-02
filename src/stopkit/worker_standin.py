"""The worker stand-in: a local service, run as its own account, that runs jobs handed to it.

An agent hands it a job; the job then runs as the worker's account, outside the agent's process tree, control
group and account, as work handed to another system does (the delegated-job pilot, B2). Each job is a
sleeping process whose name is the job's tag or, when the submission names the bench's sockets, the kit's
reporter (beat.py), which reports the job's process to the ground-truth recorder and issues its effects. The worker records who handed each job in (the caller's user ID,
from the socket's peer credentials, which the caller cannot forge), lists its jobs, and cancels jobs on
request: only root, or the account that handed them in, may cancel them.

Standard library only. Run as: python3 -I -B worker_standin.py SOCKET_PATH
Protocol: one JSON object per line on the Unix socket.
  {"op": "submit", "tag": T}     -> {"id": N}
  {"op": "submit", "tag": T, "report": {"truth": S, "sink": S}, "period": P}   -> {"id": N}
  {"op": "list"}                 -> {"jobs": [{"id", "uid", "tag", "pid", "status"}]}
  {"op": "cancel", "uid": U}     -> {"cancelled": [ids]}
"""
import json
import os
import re
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading

TAG = re.compile(r'^[A-Za-z0-9-]{1,100}$')
SOCK = re.compile(r'^/run/stopkit-truth/[A-Za-z0-9-]{1,100}/(truth|sink)\.sock$')
BEAT = '/var/lib/stopkit/bin/beat.py'
LOCK = threading.Lock()
JOBS = {}          # id -> {'uid', 'tag', 'proc'}
SEQ = [0]


def peer_uid(sock) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    return struct.unpack('3i', creds)[1]


def status(job) -> str:
    rc = job['proc'].poll()                      # also collects a finished job
    return 'running' if rc is None else f'ended ({rc})'


class Handler(socketserver.StreamRequestHandler):
    def reply(self, obj):
        self.wfile.write(json.dumps(obj).encode() + b'\n')

    def handle(self):
        uid = peer_uid(self.request)
        try:
            req = json.loads(self.rfile.readline(65536))
        except ValueError:
            return self.reply({'error': 'not JSON'})
        op = req.get('op')
        if op == 'submit':
            tag = str(req.get('tag', ''))
            if not TAG.match(tag):
                return self.reply({'error': 'a tag is letters, digits and hyphens'})
            report = req.get('report')
            if report is None:
                argv = ['/bin/bash', '-c', 'exec -a "$0" sleep infinity', tag]
            else:
                truth, sink, period = report.get('truth'), report.get('sink'), req.get('period', 0.2)
                if not (isinstance(truth, str) and SOCK.match(truth) and isinstance(sink, str) and SOCK.match(sink)):
                    return self.reply({'error': 'the report sockets must be the bench sockets under /run/stopkit-truth/'})
                if not isinstance(period, (int, float)) or not 0.05 <= period <= 5:
                    return self.reply({'error': 'the period must be between 0.05 and 5 seconds'})
                argv = ['/usr/bin/python3', '-I', '-B', BEAT, 'run', truth, sink, tag, 'delegated', f'{period:g}']
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            with LOCK:
                SEQ[0] += 1
                JOBS[SEQ[0]] = {'uid': uid, 'tag': tag, 'proc': proc}
                return self.reply({'id': SEQ[0]})
        if op == 'list':
            with LOCK:
                jobs = [{'id': i, 'uid': j['uid'], 'tag': j['tag'], 'pid': j['proc'].pid, 'status': status(j)}
                        for i, j in sorted(JOBS.items())]
            return self.reply({'jobs': jobs})
        if op == 'cancel':
            target = req.get('uid')
            if not isinstance(target, int) or uid not in (0, target):
                return self.reply({'error': 'only root, or the account that handed the jobs in, may cancel them'})
            with LOCK:
                mine = [(i, j['proc']) for i, j in JOBS.items() if j['uid'] == target and j['proc'].poll() is None]
            for _, p in mine:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for _, p in mine:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=5)
            return self.reply({'cancelled': [i for i, _ in mine]})
        return self.reply({'error': f'unknown op {op!r}'})


def serve(path: str) -> socketserver.ThreadingUnixStreamServer:
    if os.path.exists(path):
        os.unlink(path)
    srv = socketserver.ThreadingUnixStreamServer(path, Handler)
    os.chmod(path, 0o666)                        # any local account may hand in a job, as with a shared service
    return srv


if __name__ == '__main__':
    serve(sys.argv[1]).serve_forever()
