"""The effect sink: where planted work sends the effects it issues (spec/surfaces.json, effect).

An observer, written separately from the ground-truth recorder and run as its own process. Like a real
service receiving an agent's requests, it knows only what each effect says about itself and when it
arrived; it does not ask the kernel who sent it.

    python3 -I -B effect_sink.py SOCKET LOG

One JSON line per event, flushed at once: start; {"mono": arrival time, "msg": the effect}; end.
"""
import json
import os
import signal
import socket
import sys
import time


def main(argv):
    if len(argv) != 3:
        print(__doc__.split('\n\n')[2], file=sys.stderr)
        return 2
    path, log_path = argv[1], argv[2]
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    s.bind(path)
    os.chmod(path, 0o666)
    s.settimeout(0.5)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    with open(log_path, 'a', encoding='utf-8') as log:
        def write(ev):
            log.write(json.dumps(ev, sort_keys=True, separators=(',', ':')) + '\n')
            log.flush()
        write({'ev': 'start', 'mono': time.monotonic(), 'socket': path})
        print('stopkit-sink-ready', flush=True)
        while not stopping:
            try:
                data = s.recv(65536)
            except (socket.timeout, InterruptedError):
                continue
            t = time.monotonic()
            try:
                write({'ev': 'effect', 'mono': t, 'msg': json.loads(data)})
            except ValueError:
                write({'ev': 'bad', 'mono': t, 'data': data[:200].decode('utf-8', 'replace')})
        write({'ev': 'end', 'mono': time.monotonic()})
        log.flush()
        os.fsync(log.fileno())
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
