"""The sealed ground-truth recorder (FRAMEWORK.md section 3; spec/verdicts.json, truth).

Standalone on purpose: it uses only Python's standard library and shares no code with the observers, so a
bug in an observer cannot also be a bug in the truth. The verdicts never read what it writes.

    python3 -I -B recorder.py SOCKET LOG UID[,UID...]

It runs as root for one run, owns a datagram socket, and writes one JSON line per event to LOG, flushed at
once:
  start  the recorder is up: its socket, its process ID and the accounts it takes reports from
  msg    a report from planted work, with the sender's process ID and account as the kernel gives them
         (SO_PASSCRED), so a report cannot claim to come from another process
  watch  a process handle (pidfd) opened on a process that reported itself with a hello
  exit   a watched process ended: the moment its handle became readable
  bad    a report refused, and why
  tick   every half second: the recorder was alive, and how many processes it watched
  end    it was told to stop (SIGTERM or SIGINT)
Every "mono" is CLOCK_MONOTONIC, the same clock in every process on the host. Senders never wait: a report
that does not fit in the queue is lost, and the missing heartbeat number shows it.

The process handle is opened only after the work has reported its own process ID and start time, and the
start time is checked again after opening, so the handle is on that very process and the recorder never
becomes its parent: it changes nothing about how the process is reaped or signalled.
"""
import json
import os
import select
import signal
import socket
import struct
import sys
import time

TICK_S = 0.5
UCRED = struct.Struct('iII')               # struct ucred: pid, uid, gid
BUF = 65536


def start_time(pid):
    """Field 22 of /proc/<pid>/stat: the start time in clock ticks since boot. None if it cannot be read."""
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            s = f.read().decode('utf-8', 'replace')
        return int(s[s.rindex(')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class Recorder:
    def __init__(self, sock_path, log_path, uids):
        self.uids = set(uids)
        self.log = open(log_path, 'a', encoding='utf-8')
        self.watched = {}                      # pidfd -> (pid, start, tag, item)
        self.stopping = False
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind(sock_path)
        os.chmod(sock_path, 0o666)             # any process may send; the kernel says which one did
        self.sock.setblocking(False)
        self.poller = select.poll()
        self.poller.register(self.sock.fileno(), select.POLLIN)
        self.write({'ev': 'start', 'mono': time.monotonic(), 'pid': os.getpid(), 'socket': sock_path,
                    'uids': sorted(self.uids), 'tick_s': TICK_S})

    def write(self, ev):
        self.log.write(json.dumps(ev, sort_keys=True, separators=(',', ':')) + '\n')
        self.log.flush()

    def receive(self):
        while True:
            try:
                data, anc, _flags, _addr = self.sock.recvmsg(BUF, socket.CMSG_SPACE(UCRED.size))
            except (BlockingIOError, InterruptedError):
                return
            t = time.monotonic()
            cred = None
            for level, kind, payload in anc:
                if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(payload) >= UCRED.size:
                    cred = UCRED.unpack(payload[:UCRED.size])
            try:
                msg = json.loads(data)
                if not isinstance(msg, dict):
                    raise ValueError('not an object')
            except ValueError as e:
                self.write({'ev': 'bad', 'mono': t, 'why': f'not a JSON object: {e}', 'cred': cred,
                            'data': data[:200].decode('utf-8', 'replace')})
                continue
            if cred is None:
                self.write({'ev': 'bad', 'mono': t, 'why': 'no sender credentials', 'msg': msg})
                continue
            if cred[1] not in self.uids:
                self.write({'ev': 'bad', 'mono': t, 'why': f'sender uid {cred[1]} is not one of the run', 'msg': msg,
                            'cred': {'pid': cred[0], 'uid': cred[1]}})
                continue
            self.write({'ev': 'msg', 'mono': t, 'cred': {'pid': cred[0], 'uid': cred[1]}, 'msg': msg})
            if msg.get('type') == 'hello':
                self.hello(cred, msg)

    def hello(self, cred, msg):
        pid, start, tag, item = msg.get('pid'), msg.get('start'), msg.get('tag'), msg.get('item')
        if pid != cred[0]:
            self.write({'ev': 'bad', 'mono': time.monotonic(), 'why': 'the hello names another process than its sender',
                        'msg': msg, 'cred': {'pid': cred[0], 'uid': cred[1]}})
            return
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            self.write({'ev': 'exit', 'mono': time.monotonic(), 'pid': pid, 'start': start, 'tag': tag, 'item': item,
                        'before_watch': True})
            return
        except OSError as e:
            self.write({'ev': 'bad', 'mono': time.monotonic(), 'why': f'pidfd_open: {e}', 'msg': msg})
            return
        now = start_time(pid)
        if now is None:                         # gone between the open and the check
            os.close(fd)
            self.write({'ev': 'exit', 'mono': time.monotonic(), 'pid': pid, 'start': start, 'tag': tag, 'item': item,
                        'before_watch': True})
            return
        if now != start:
            os.close(fd)
            self.write({'ev': 'bad', 'mono': time.monotonic(), 'why': f'start time {now} is not the reported {start}',
                        'msg': msg})
            return
        self.watched[fd] = (pid, start, tag, item)
        self.poller.register(fd, select.POLLIN)
        self.write({'ev': 'watch', 'mono': time.monotonic(), 'pid': pid, 'start': start, 'tag': tag, 'item': item})

    def ended(self, fd):
        t = time.monotonic()
        pid, start, tag, item = self.watched.pop(fd)
        self.poller.unregister(fd)
        os.close(fd)
        self.write({'ev': 'exit', 'mono': t, 'pid': pid, 'start': start, 'tag': tag, 'item': item})

    def run(self):
        next_tick = time.monotonic()
        while not self.stopping:
            wait_ms = max(0.0, next_tick - time.monotonic()) * 1000
            for fd, _ev in self.poller.poll(wait_ms):
                if fd == self.sock.fileno():
                    self.receive()
                elif fd in self.watched:
                    self.ended(fd)
            now = time.monotonic()
            if now >= next_tick:
                self.write({'ev': 'tick', 'mono': now, 'watching': len(self.watched)})
                next_tick = max(next_tick + TICK_S, now)
        self.receive()                           # whatever arrived before the stop
        self.write({'ev': 'end', 'mono': time.monotonic(),
                    'still_watching': sorted([p, s, t] for p, s, t, _ in self.watched.values())})
        self.log.flush()
        os.fsync(self.log.fileno())
        self.log.close()


def main(argv):
    if len(argv) != 4:
        print(__doc__.split('\n\n')[2], file=sys.stderr)
        return 2
    rec = Recorder(argv[1], argv[2], [int(u) for u in argv[3].split(',') if u])

    def stop(_sig, _frame):
        rec.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print('stopkit-truth-ready', flush=True)
    rec.run()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
