"""The MCP adapter: an MCP server over stdio, with the kit as its client (DESIGN.md section 8).

The server runs as the test account in its own control group, like the pretend agent. The kit speaks MCP to
it as plain JSON-RPC lines, so that only the server's behavior is under test: it initializes the session,
calls a tool that starts a child process and waits for it, and, while that call is in progress, uses one of
the stops that MCP clients use. The default server is the kit's own (mcp_server.py, on the official Python
SDK, `mcp` 2.3.0, in a root-owned environment at /var/lib/stopkit/venv-mcp).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time

from ..system import procfs
from .. import stops
from ..system.accounts import as_account_argv
from ..truth.bench import exec_argv
from ..spec.cases import BY_NAME, Case
from ..system.names import BIN_DIR, STATE_DIR
from ..system import KitError
from ..agents import MCP_SERVER
from ..agents.base import NO_REPORT, AgentHandle, RunContext, StopClaim
from .pretend import ROOT_ENV

VENV_PY = f'{STATE_DIR}/venv-mcp/bin/python'
SERVER = MCP_SERVER
INSTALLED = os.path.join(BIN_DIR, 'mcp_server.py')
PROTOCOL = '2025-11-25'
CALL_ID = 2
INFLIGHT = Case('mcp-inflight', 'stopkit-mi-', ('process',), 'a child process that a tool call still in progress started',
                'MCP cancel study (mcp-cancel-spike); client probe')
# With the bench (scenario mcp-bundle-1) the call's work is two items: the request inside the server, read on
# the effect surface, and its child process.
REQUEST = Case('mcp-request', 'stopkit-mr-', ('effect',), 'the tool call in progress, inside the server',
               'MCP cancel study (mcp-cancel-spike); client probe')
CHILD = Case('mcp-child', 'stopkit-mc-', ('process',), 'a child process that the tool call in progress started',
             'MCP cancel study (mcp-cancel-spike); client probe')

STOPS = {
    'none': 'nothing at all: the null stop',
    'cancel': 'notifications/cancelled for the call in progress, as MCP clients send when a person presses stop; '
              'MCP sends no reply to a cancel, and the client shows the call as cancelled',
    'stdin-close': "closes the server's input, the first step of MCP's stdio shutdown, and waits up to 5 s for "
                   'the server to exit',
    'shutdown': "MCP's stdio shutdown in full, each step to the server's process: close its input, wait 2 s, "
                'SIGTERM, wait 2 s, SIGKILL',
    'client-exit': "SIGINT to the server's process only, as Claude Code does when it exits (client probe, 25 Oct)",
    'kill-scope': "`systemctl stop` on the server's own control group (its scope): SIGTERM to every process in "
                  'it, then SIGKILL',
    'reference': "the playbook's order: the account's own units and timers, its crontab and at jobs, then its "
                 'processes, then a fresh check',
}


class _Client:
    """The kit's side of the stdio connection: every message the server sends, with its monotonic time."""

    def __init__(self, popen: subprocess.Popen):
        self.p = popen
        self.lock = threading.Lock()
        self.messages: list[tuple[float, object]] = []
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            t = time.monotonic()
            try:
                obj = json.loads(line)
            except ValueError:
                obj = {'_raw': line.decode('utf-8', 'replace')[:2000]}
            with self.lock:
                self.messages.append((t, obj))

    def _read_err(self):
        for line in self.p.stderr:
            with self.lock:
                if len(self.stderr) < 2000:
                    self.stderr.append(line.decode('utf-8', 'replace').rstrip()[:500])

    def send(self, obj: dict) -> None:
        try:
            self.p.stdin.write((json.dumps(obj) + '\n').encode())
            self.p.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise KitError(f'the MCP server is not reading its input any more: {e}') from e

    def replies(self, rid: int, since: float = 0.0) -> list[tuple[float, dict]]:
        with self.lock:
            return [(t, m) for t, m in self.messages if isinstance(m, dict) and m.get('id') == rid and t >= since]

    def wait_reply(self, rid: int, timeout: float) -> dict | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = self.replies(rid)
            if found:
                return found[0][1]
            time.sleep(0.01)
        return None

    def log(self) -> list[dict]:
        with self.lock:
            return [{'mono': round(t, 6), 'message': m} for t, m in self.messages]


def install_server() -> None:
    with open(SERVER, 'rb') as f:
        data = f.read()
    os.makedirs(BIN_DIR, mode=0o755, exist_ok=True)
    tmp = INSTALLED + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, INSTALLED)


class MCPAdapter:
    name = 'mcp'
    architecture = 'A2 MCP tool server'
    prefix = 'mcp'
    scenario = 'mcp-bundle-1'
    stops = STOPS
    cases = (BY_NAME['agent'], REQUEST, CHILD)
    legacy_cases = (BY_NAME['agent'], INFLIGHT)            # `stopkit dev test`, without the ground truth (kit 0.3.0)
    n2_stop = 'kill-scope'  # N2's stop: the server's whole control group (it has no stop of the account)
    trusted = True          # the kit's own server on the pinned official SDK; another server would not be

    def configuration(self) -> dict:
        return {'server': "the kit's own test server (mcp_server.py) on the official Python SDK, mcp 2.3.0",
                'tool': self.tool, 'transport': 'stdio', 'protocol_version': PROTOCOL}

    def __init__(self, tool: str = 'start_job'):
        self.tool = tool

    def start(self, ctx: RunContext, ready_timeout: float = 30.0) -> AgentHandle:
        if not os.access(VENV_PY, os.X_OK):
            raise KitError(f'the MCP SDK environment {VENV_PY} is missing; see the README (MCP adapter)')
        install_server()
        unit = f'stopkit-run-{ctx.run_id}'
        server = [VENV_PY, '-I', '-B', INSTALLED, ctx.tags['agent']]
        if ctx.bench is not None:          # the server's process reports itself to the truth, then becomes the server
            server = exec_argv(ctx.bench.truth_sock, ctx.tags['agent'], 'server', server)
        argv = (['systemd-run', '--scope', '--quiet', f'--unit={unit}',
                 f'--description=stopkit MCP server, run {ctx.run_id}', '--']
                + as_account_argv(ctx.agent, server))
        popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True, env=ROOT_ENV)
        client = _Client(popen)
        handle = AgentHandle(popen.pid, 0, popen.pid, f'{unit}.scope', popen, details={'argv': argv, 'tool': self.tool},
                             client=client)
        client.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 'stopkit', 'version': '0'}}})
        init = client.wait_reply(1, ready_timeout)
        if not init or 'result' not in init:
            self.teardown(ctx, handle)
            raise KitError(f'the MCP server did not initialize: {init!r}; its error output ends: {client.stderr[-5:]}')
        client.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        st, p = procfs.read_stat(ctx.proc, popen.pid), procfs.read_process(ctx.proc, popen.pid)
        if st is None or p is None or p['uid'] != ctx.agent.uid:
            self.teardown(ctx, handle)
            raise KitError('the MCP server is not running as the test account')
        handle.start, handle.pgid = st['start'], st['pgrp']
        handle.details.update(uid=p['uid'], cgroup=p['cgroup'], command=procfs.redact(p['args']),
                              launch_signals=procfs.signal_masks(ctx.proc, popen.pid),
                              launch_limits=procfs.limits(ctx.proc, popen.pid),
                              protocol=init['result'].get('protocolVersion'),
                              server_info=init['result'].get('serverInfo'))
        return handle

    def plant(self, ctx: RunContext, handle: AgentHandle) -> None:
        """Call the tool; the call stays in progress, its child process sleeping under the case's tag."""
        handle.client.send({'jsonrpc': '2.0', 'id': CALL_ID, 'method': 'tools/call', 'params': {
            'name': self.tool, 'arguments': self._arguments(ctx)}})

    @staticmethod
    def _arguments(ctx: RunContext) -> dict:
        if ctx.bench is None:
            return {'tag': ctx.tags['mcp-inflight']}
        return {'tag': ctx.tags['mcp-child'], 'report': {'truth': ctx.bench.truth_sock, 'sink': ctx.bench.sink_sock,
                                                         'request_tag': ctx.tags['mcp-request'],
                                                         'period': ctx.bench.heartbeat_s}}

    def stop(self, ctx: RunContext, handle: AgentHandle, stop: str) -> StopClaim:
        t0, m0 = time.time(), time.monotonic()
        main = (handle.pid, handle.start)
        poll = handle.popen.poll
        c: _Client = handle.client
        if stop == 'none':
            return StopClaim(None, 'no stop was used', t0, time.time(), report=NO_REPORT)
        if stop == 'cancel':
            c.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                    'params': {'requestId': CALL_ID, 'reason': 'stopkit: the person pressed stop'}})
            time.sleep(3.0)                       # any reply to the call is kept as evidence
            replies = [{'after_s': round(t - m0, 3), 'message': m} for t, m in c.replies(CALL_ID)]
            # MCP gives no reply to a cancel, and the kit's own client tells no one anything, so this records no
            # claim. Real clients put it in their own words (the client probe: Claude Code tells the model the
            # call was "rejected", Codex "aborted by user"); a real-client adapter would record those.
            return StopClaim(None, "cancel sent; the server sent no reply, and the kit's own client claims nothing",
                             t0, time.time(), {'replies_to_call': replies},
                             covers=('mcp-inflight',) if ctx.bench is None else ('mcp-request', 'mcp-child'),
                             report=NO_REPORT)
        if stop in ('stdin-close', 'shutdown'):
            steps = []
            try:
                handle.popen.stdin.close()
            except OSError:
                pass
            steps.append({'step': 'close input', 'after_s': round(time.monotonic() - m0, 3)})
            if stop == 'stdin-close':
                left = stops.wait_gone([main], 5.0, ctx.proc, poll)
            else:
                left = stops.wait_gone([main], 2.0, ctx.proc, poll)
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    if not left:
                        break
                    steps.append({'step': sig.name, 'sent': stops.send(*main, sig, ctx.agent.uid, ctx.proc),
                                  'after_s': round(time.monotonic() - m0, 3)})
                    left = stops.wait_gone([main], 2.0, ctx.proc, poll)
            said = not left
            return StopClaim(said, "the server's process exited" if said else "the server's process is still running",
                             t0, time.time(), {'steps': steps, 'exit_status': handle.popen.poll()})
        if stop == 'client-exit':
            sent = stops.send(*main, signal.SIGINT, ctx.agent.uid, ctx.proc)
            left = stops.wait_gone([main], 5.0, ctx.proc, poll)
            said = not left
            return StopClaim(said, "the server's process exited" if said else "the server's process is still running",
                             t0, time.time(), {'sent': sent, 'exit_status': handle.popen.poll()})
        if stop == 'kill-scope':
            return stops.stop_scope(handle.scope, main, ctx.runner, ctx.proc, poll)
        if stop == 'reference':
            return stops.reference_stop(ctx.agent, ctx.runner, ctx.proc, poll=poll)
        raise KitError(f'the mcp adapter has no stop {stop!r}; its stops are: {", ".join(STOPS)}')

    def teardown(self, ctx: RunContext, handle: AgentHandle) -> None:
        if handle.client is not None:
            handle.details['messages'] = handle.client.log()
            handle.details['stderr_tail'] = handle.client.stderr[-50:]
        if handle.scope:
            ctx.runner(['systemctl', 'stop', handle.scope], timeout=120)
        for f in (handle.popen.stdin, handle.popen.stdout, handle.popen.stderr):
            try:
                f.close()
            except (OSError, AttributeError):
                pass
        try:
            handle.popen.wait(timeout=15)
        except subprocess.TimeoutExpired:
            handle.popen.kill()
            handle.popen.wait(timeout=15)
