"""What planted work reports about itself, to the ground-truth recorder and to the effect sink.

Standalone: the standard library only, installed read-only for the test accounts, and run as the work itself.

    beat.py run  TRUTH SINK TAG ITEM [PERIOD]   report this process to the truth (hello), then every PERIOD
                                                seconds (default 0.2) issue one effect to the sink and report
                                                it to the truth with the same number, until the process ends
    beat.py fire TRUTH SINK TAG ITEM            one run of a trigger's job: issue its effect, report the fire,
                                                and exit
    beat.py exec TRUTH TAG ITEM -- PROGRAM ARG...
                                                report this process to the truth (hello), then become PROGRAM
                                                (exec keeps the process ID and start time, so the recorder's
                                                handle follows the program); for a platform's own processes,
                                                which cannot report themselves
    beat.py http-run URL TAG ITEM [PERIOD]      every PERIOD seconds POST one effect to the HTTP stand-in at
                                                URL, stamped with the sender's time, until the process ends;
                                                for the sentinel of the HTTP path, and to measure its delivery

Every message carries the sender's monotonic time ("t"). Sends never wait: a message that does not fit is
lost. Python ignores SIGPIPE and SIGXFSZ and turns SIGINT into an exception; all three are put back to their
default action first, so the process ends on a signal exactly as a plain program does (and a program started
with exec inherits the default actions). The tag is on the command line, which is how the kit's observers
recognize the process.
"""
import json
import os
import signal
import socket
import sys
import time
import urllib.request


def start_time():
    with open('/proc/self/stat', 'rb') as f:
        s = f.read().decode('utf-8', 'replace')
    return int(s[s.rindex(')') + 2:].split()[19])


def _sender():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.setblocking(False)

    def send(path, msg):
        try:
            s.sendto(json.dumps(msg, separators=(',', ':')).encode(), path)
        except OSError:
            pass                              # full, or the receiver is gone: never wait
    return send


def _http_run(url, tag, item, period):
    n, due = 0, time.monotonic()
    while True:
        n += 1
        body = json.dumps({'tag': tag, 'item': item, 'n': n, 't': time.monotonic(), 'kind': 'beat'}).encode()
        try:
            req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=2).read()
        except OSError:
            pass                              # the stand-in is gone or slow: the gap shows in its log
        due += period
        time.sleep(max(0.0, due - time.monotonic()))


def main(argv):
    for name in ('SIGINT', 'SIGPIPE', 'SIGXFSZ'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    mode = argv[1] if len(argv) > 1 else ''
    if mode == 'exec':
        if len(argv) < 7 or argv[5] != '--':
            print(__doc__.split('\n\n')[2], file=sys.stderr)
            return 2
        truth, tag, item, program = argv[2], argv[3], argv[4], argv[6:]
        _sender()(truth, {'type': 'hello', 'tag': tag, 'item': item, 'pid': os.getpid(), 'start': start_time(),
                          't': time.monotonic(), 'exec': program[0]})
        os.execv(program[0], program)
    if mode == 'http-run':
        if len(argv) < 5:
            print(__doc__.split('\n\n')[2], file=sys.stderr)
            return 2
        _http_run(argv[2], argv[3], argv[4], float(argv[5]) if len(argv) > 5 else 0.2)
        return 0
    if len(argv) < 6 or mode not in ('run', 'fire'):
        print(__doc__.split('\n\n')[2], file=sys.stderr)
        return 2
    truth, sink, tag, item = argv[2:6]
    period = float(argv[6]) if len(argv) > 6 else 0.2
    send = _sender()
    pid = os.getpid()
    if mode == 'fire':
        t = time.monotonic()
        send(sink, {'tag': tag, 'item': item, 'n': 0, 't': t, 'fire': True})
        send(truth, {'type': 'fire', 'tag': tag, 'item': item, 'pid': pid, 't': t})
        return 0
    send(truth, {'type': 'hello', 'tag': tag, 'item': item, 'pid': pid, 'start': start_time(), 't': time.monotonic()})
    n, due = 0, time.monotonic()
    while True:
        n += 1
        t = time.monotonic()
        send(sink, {'tag': tag, 'item': item, 'n': n, 't': t})          # the effect itself
        send(truth, {'type': 'beat', 'tag': tag, 'item': item, 'n': n, 't': t})   # and the report of it
        due += period
        time.sleep(max(0.0, due - time.monotonic()))


if __name__ == '__main__':
    sys.exit(main(sys.argv))
