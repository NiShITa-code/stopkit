"""Names and places the kit uses. Everything the kit creates starts with 'stopkit-'."""

AGENT = 'stopkit-agent'            # the agent under test runs as this account
SENTINEL = 'stopkit-sentinel'      # holds one known item per surface; no stop under test touches it
WORKER = 'stopkit-worker'          # runs the worker stand-in, which runs jobs handed to it
WORKER_UNIT = 'stopkit-worker.service'
WORKER_SOCKET = '/run/stopkit-worker/sock'
STATE_DIR = '/var/lib/stopkit'     # root-owned: the account marker, the agent scripts and per-run plans
MARKER = f'{STATE_DIR}/accounts.json'
BIN_DIR = f'{STATE_DIR}/bin'
RUNS_DIR = f'{STATE_DIR}/runs'

# The sentinel's item on each surface. A listing proves itself by showing these (DESIGN.md section 4).
SENTINEL_TAGS = {
    'process': 'stopkit-sentinel-process',
    'unit': 'stopkit-sentinel-unit',
    'timer': 'stopkit-sentinel-timer',
    'cron': 'stopkit-sentinel-cron',
    'at': 'stopkit-sentinel-at',
    'listener': 'stopkit-sentinel-listener',
    'worker': 'stopkit-sentinel-worker',
}

# The sentinel's reporter to the ground-truth recorder and the effect sink: its steady reports show that the
# truth channel and the sink kept working (stopkit/bench.py).
SENTINEL_BEAT = 'stopkit-sentinel-beat'
SENTINEL_HTTP = 'stopkit-sentinel-http'      # the same, through the HTTP stand-in, for work that reports over HTTP

# A harmless listener: binds a free port on 127.0.0.1 and sleeps. Its tag is passed as an argument, so it
# shows in the holder's command line.
LISTENER_CODE = ('import socket,time;s=socket.socket();s.bind(("127.0.0.1",0));s.listen();'
                 'time.sleep(10**9)')


# Hands one job to the worker stand-in: python3 -I -c WORKER_SUBMIT SOCKET TAG (exit 0 if it took the job).
WORKER_SUBMIT = ('import json,socket,sys\n'
                 's=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])\n'
                 's.sendall(json.dumps({"op":"submit","tag":sys.argv[2]}).encode()+bytes([10]))\n'
                 'sys.exit(0 if "id" in json.loads(s.recv(4096)) else 1)\n')


class KitError(Exception):
    """The kit refuses to go on: the message says why."""
