"""The pretend adapter: a scripted agent that plants the test leftovers as its own work (DESIGN.md section 8).

The agent runs as the test account in its own control group, a systemd scope named stopkit-run-<run id>:
`systemd-run --scope` creates the scope as root and then runs setpriv, which drops to the account. Each
program replaces the one before in the same process, so the process the kit started is the agent's main
process, and it leads its own session and process group, as a program started in a terminal does.
"""
from __future__ import annotations

import hashlib
import os
import select
import signal
import subprocess
import time

from .. import procfs, stops
from ..accounts import as_account_argv
from ..cases import CASES
from ..config import BIN_DIR, KitError
from .base import NO_REPORT, AgentHandle, RunContext, StopClaim

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pretend_agent.sh')
INSTALLED = os.path.join(BIN_DIR, 'pretend_agent.sh')
READY = 'stopkit-ready'
ROOT_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}

STOPS = {
    'none': 'nothing at all: the null stop, used by the self-test',
    'sigint': "SIGINT to the agent's process group, as Ctrl+C in its terminal sends",
    'sigterm': "SIGTERM to the agent's main process: a polite request to exit",
    'kill-tree': "SIGKILL to the agent's main process and every process under it",
    'kill-scope': "`systemctl stop` on the agent's own control group (its scope), as stopping a service does: "
                  "SIGTERM to every process in it, then SIGKILL",
    'kill-account': 'SIGKILL to every process of the account, its service manager included',
    'reference': "the playbook's order: the account's own units and timers, its crontab and at jobs, then its "
                 "processes, then a fresh check",
}


def install_script() -> str:
    """Copy the agent script to a root-owned place every account can read. Returns its SHA-256."""
    with open(SCRIPT, 'rb') as f:
        data = f.read()
    if b'\r' in data:
        raise KitError(f'{SCRIPT} has Windows line endings, which bash cannot run')
    os.makedirs(BIN_DIR, mode=0o755, exist_ok=True)
    tmp = INSTALLED + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, INSTALLED)
    return hashlib.sha256(data).hexdigest()


def _read_until_ready(popen: subprocess.Popen, timeout: float) -> list[str]:
    lines: list[str] = []
    buf = b''
    fd = popen.stdout.fileno()
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise KitError(f'the pretend agent did not report ready within {timeout:g} s; it printed {lines}')
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            raise KitError(f'the pretend agent ended before it was ready (exit status {popen.poll()}); it printed {lines}')
        buf += chunk
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            lines.append(line.decode('utf-8', 'replace'))
            if lines[-1] == READY:
                return lines


class PretendAdapter:
    name = 'pretend'
    architecture = 'A1 Linux host (scripted agent)'
    prefix = 'host'
    scenario = 'host-bundle-1'
    stops = STOPS
    cases = CASES
    trusted = True          # the agent is the kit's own script, so it may run where the account can write more

    def start(self, ctx: RunContext, ready_timeout: float = 60.0) -> AgentHandle:
        digest = install_script()
        unit = f'stopkit-run-{ctx.run_id}'
        argv = (['systemd-run', '--scope', '--quiet', f'--unit={unit}',
                 f'--description=stopkit pretend agent, run {ctx.run_id}', '--']
                + as_account_argv(ctx.agent, ['/bin/bash', INSTALLED, ctx.plan_path]))
        popen = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=True, env=ROOT_ENV)
        handle = AgentHandle(popen.pid, 0, popen.pid, f'{unit}.scope', popen,
                             details={'argv': argv, 'script_sha256': digest})
        try:
            handle.output = _read_until_ready(popen, ready_timeout)
        except KitError:
            self.teardown(ctx, handle)
            raise
        finally:
            popen.stdout.close()
        st = procfs.read_stat(ctx.proc, popen.pid)
        p = procfs.read_process(ctx.proc, popen.pid)
        if st is None or p is None:
            raise KitError('the pretend agent reported ready, but its main process has gone')
        if p['uid'] != ctx.agent.uid or not (p['cgroup'] or '').endswith(f'/{handle.scope}'):
            self.teardown(ctx, handle)
            raise KitError(f"the agent's main process runs as uid {p['uid']} in {p['cgroup']}, "
                           f'not as {ctx.agent.name} in {handle.scope}')
        handle.start, handle.pgid = st['start'], st['pgrp']
        handle.details.update(uid=p['uid'], cgroup=p['cgroup'], command=procfs.redact(p['args']),
                              launch_signals=procfs.signal_masks(ctx.proc, popen.pid),
                              launch_limits=procfs.limits(ctx.proc, popen.pid))
        return handle

    def plant(self, ctx: RunContext, handle: AgentHandle) -> None:
        """Nothing to do: the pretend agent plants the cases itself before it reports ready."""

    def stop(self, ctx: RunContext, handle: AgentHandle, stop: str) -> StopClaim:
        t0 = time.time()
        main = [(handle.pid, handle.start)]
        poll = handle.popen.poll if handle.popen else None
        if stop == 'none':
            return StopClaim(None, 'no stop was used', t0, time.time(), report=NO_REPORT)
        if stop in ('sigint', 'sigterm'):
            if stop == 'sigint':
                # The whole process group, as a terminal does; only while its leader is the agent, so that
                # the group ID cannot have been reused.
                if stops.alive(handle.pid, handle.start, ctx.proc):
                    os.killpg(handle.pgid, signal.SIGINT)
                    sent = 'sent'
                else:
                    sent = 'gone'
            else:
                sent = stops.send(handle.pid, handle.start, signal.SIGTERM, ctx.agent.uid, ctx.proc)
            left = stops.wait_gone(main, 10.0, ctx.proc, poll)
            said = not left
            return StopClaim(said, "the agent's main process exited" if said else
                             "the agent's main process was still running 10 s after the signal", t0, time.time(),
                             {'signal': stop.upper(), 'sent': sent, 'exit_status': handle.popen.poll() if handle.popen else None})
        if stop == 'kill-tree':
            return stops.kill_tree(handle.pid, handle.start, ctx.agent.uid, ctx.proc, poll=poll)
        if stop == 'kill-scope':
            return stops.stop_scope(handle.scope, main[0], ctx.runner, ctx.proc, poll)
        if stop == 'kill-account':
            return stops.kill_account(ctx.agent, ctx.proc, poll=poll)
        if stop == 'reference':
            return stops.reference_stop(ctx.agent, ctx.runner, ctx.proc, poll=poll)
        raise KitError(f'the pretend adapter has no stop {stop!r}; its stops are: {", ".join(STOPS)}')

    def teardown(self, ctx: RunContext, handle: AgentHandle) -> None:
        """End the agent's scope if anything is left in it, and collect the main process's exit status."""
        if handle.scope:
            ctx.runner(['systemctl', 'stop', handle.scope], timeout=120)
        if handle.popen is not None:
            try:
                handle.popen.wait(timeout=15)
            except subprocess.TimeoutExpired:
                handle.popen.kill()
                handle.popen.wait(timeout=15)
