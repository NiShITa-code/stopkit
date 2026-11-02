#!/usr/bin/env python3
"""Recording MCP server for the client probe: ../clients/record_server.py with the
number of steps set by $REC_STEPS (default 12) and the child's process group logged.
Standard library only.

Logs every message it receives, stdin closing and every SIGINT/SIGTERM/SIGHUP (with
times) to $REC_LOG as JSON lines. Its one tool, `slow`, starts a bash child that writes
a marker line to $REC_MARK every 0.5 s for $REC_STEPS steps, then replies when the child
exits. It ignores cancels on purpose (it only records them), so what the client sends
stays visible. It exits on stdin close, SIGINT or SIGTERM; its child stays in the
server's process group and is not killed by the server.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

LOG = os.environ["REC_LOG"]
MARK = os.environ["REC_MARK"]
STEPS = int(os.environ.get("REC_STEPS", "12"))
INTERVAL = 0.5

_log_lock = threading.Lock()
_out_lock = threading.Lock()


def log(event, **fields):
    record = {"t": time.time(), "event": event, **fields}
    with _log_lock, open(LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())


def send(obj):
    data = (json.dumps(obj) + "\n").encode()
    with _out_lock:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            log("send_failed", error=type(exc).__name__)


def on_signal(signum, _frame):
    name = signal.Signals(signum).name
    log("signal", name=name)
    log("exit", reason=name)
    os._exit(0)


def run_tool(req_id):
    loop = (f"for i in $(seq 0 {STEPS - 1}); do "
            f'echo "$(date +%s.%N) $i $$" >> {MARK}; sleep {INTERVAL}; done')
    child = subprocess.Popen(["bash", "-c", loop], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("child_spawned", pid=child.pid, pgid=os.getpgid(child.pid), request_id=req_id)
    child.wait()
    log("child_exited", pid=child.pid, returncode=child.returncode)
    send({"jsonrpc": "2.0", "id": req_id,
          "result": {"content": [{"type": "text", "text": "done"}], "isError": False}})
    log("result_sent", request_id=req_id)


def handle(msg):
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-11-25"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "probe-server", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [{
            "name": "slow",
            "description": "Runs a bash loop that writes marker lines, then replies 'done'.",
            "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}}}]}})
    elif method == "tools/call":
        threading.Thread(target=run_tool, args=(req_id,), daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif req_id is not None and method is not None:
        send({"jsonrpc": "2.0", "id": req_id,
              "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main():
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    log("start", pid=os.getpid(), pgid=os.getpgid(0), sid=os.getsid(0), ppid=os.getppid(), steps=STEPS)
    for raw in sys.stdin.buffer:
        try:
            msg = json.loads(raw)
        except ValueError:
            log("in_unparsed", raw=raw.decode(errors="replace")[:300])
            continue
        log("in", method=msg.get("method"), id=msg.get("id"), params=msg.get("params"))
        handle(msg)
    log("stdin_eof")
    log("exit", reason="stdin_eof")
    os._exit(0)


if __name__ == "__main__":
    main()
