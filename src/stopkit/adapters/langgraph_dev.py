"""The LangGraph binding (architecture A4): LangGraph's development server (`langgraph dev`, langgraph-api 0.15.1,
LangGraph 1.2.12, the in-memory runtime), run as the agent account like a command started in a terminal.

Each run copies the binding's agent (langgraph_app/graph.py: a helper in the background, a crontab line, and a
main task in a plain synchronous node) into the account's home, starts the server in a control group of its own
(a scope) through the kit's reporter (beat.py exec), and starts one background run on a new thread through the
server's API, as a client does. The run's input carries the bench's plan, so its work reports to the truth.

The kit observes the server's process by the identity it started (its process ID and start time), the run by
the server's own API (its status: a claim of LangGraph's, read as an existence listing of what the server holds),
the run's work by its effects, the helper as a process and the scheduled job in the crontab. The restart action
stands in for a reboot: the server's process is killed if it still runs, and a new server is started in the
same folder, which loads what the last one saved and resumes the runs it had saved as pending.

The results concern the development server only; a deployed LangGraph server keeps its runs in a database.
langgraph-api is under the Elastic License 2.0: whether results about it are published is the user's decision.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request

from .. import procfs, stops
from ..accounts import as_account_argv, run_as
from ..bench import BEAT, exec_argv
from ..cases import BY_NAME, Case
from ..cmd import CmdResult, run
from ..config import KitError
from ..observe.components import tracked
from ..observe.model import Item, Listing, failed
from .base import EXPLICIT, NO_REPORT, AgentHandle, RunContext, StopClaim
from .pretend import ROOT_ENV

VENV = '/opt/stopkit-langgraph/venv'
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'langgraph_app')
PORT = 2024
BASE = f'http://127.0.0.1:{PORT}'
WORK_PERIOD = 0.5
LANGGRAPH_ENV = ['LANGGRAPH_CLI_NO_ANALYTICS=1', 'LANGGRAPH_NO_VERSION_CHECK=true', 'LANGSMITH_TRACING=false',
                 'LANGCHAIN_TRACING_V2=false']
IN_FORCE = ('pending', 'running')

SERVER = Case('lg-server', 'stopkit-lg-server-', ('component',), "LangGraph's development server, which runs the graph",
              'the LangGraph demo (27 Oct 2026); expA')
RUN = Case('lg-run', 'stopkit-lg-run-', ('effect', 'langgraph-run'),
           "the agent's run: its main task, a synchronous node that acts every half second", 'the LangGraph demo; expA')
HELPER = Case('lg-helper', 'stopkit-lg-helper-', ('process',),
              'a helper the run started in the background, in a session of its own', 'E2b dev server; the LangGraph demo')
CRON = Case('lg-cron', 'stopkit-lg-cron-', ('cron',), 'a crontab line the run added, which runs a job every minute',
            'scheduled jobs (MJ Rathbun); the LangGraph demo')

STOPS = {
    'none': 'nothing at all: the null stop',
    'cancel': "POST /threads/{thread}/runs/{run}/cancel, the request LangGraph's client library sends for runs.cancel",
    'delete-thread': 'DELETE /threads/{thread}: the thread and its runs deleted through the API',
    'ctrl-c': "SIGINT to the server's process group twice, a second apart, as pressing Ctrl+C twice in its terminal does",
    'sigterm': "SIGTERM to the server's process",
    'kill': "SIGKILL to the server's process only, as `kill -9` does",
    'kill-scope': "`systemctl stop` on the server's control group (its scope): SIGTERM to every process in it, then SIGKILL",
    'reference': "the binding's full stop: the run cancelled and its thread deleted through the API, then the account's "
                 'units, crontab, at jobs and processes, then a fresh check',
}


def _http(method: str, path: str, body=None, timeout: float = 10.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except OSError as e:
        return None, f'{type(e).__name__}: {e}'
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode('utf-8', 'replace')[:2000]


def answers(timeout: float = 1.0) -> bool:
    st, _ = _http('GET', '/ok', timeout=timeout)
    return st == 200


class LangGraphAdapter:
    name = 'langgraph'
    architecture = 'A4 LangGraph (development server)'
    prefix = 'langgraph'
    scenario = 'langgraph-bundle-1'
    binding_version = 'LangGraph 1.2.12, langgraph-api 0.15.1, langgraph-cli 0.4.32, langgraph-runtime-inmem 0.35.1'
    stops = STOPS
    cases = (SERVER, RUN, HELPER, CRON)
    trusted = False
    n2_stop = 'kill'

    def configuration(self) -> dict:
        return {'server': 'langgraph dev --no-browser --no-reload --host 127.0.0.1 --port 2024 (the in-memory runtime, '
                          'which saves to .langgraph_api/ in the app folder)',
                'environment': ' '.join(LANGGRAPH_ENV) + ' (analytics, version checks and tracing off)',
                'agent': 'langgraph_app/graph.py: no model; a helper, a crontab line, a synchronous main task',
                'network': "the agent account's packets to anything but loopback are dropped (egress.py)"}

    def prepare(self, runner=run, log=print) -> None:
        if not os.access(f'{VENV}/bin/langgraph', os.X_OK):
            raise KitError(f'{VENV} is missing; run setup/langgraph.sh first')
        if answers():
            raise KitError(f'something already answers on 127.0.0.1:{PORT}; stop it first')

    def _app(self, ctx) -> str:
        return os.path.join(ctx.agent.home, f'lg-{ctx.run_id.lower()}')

    def _start_server(self, ctx, handle, generation: int) -> CmdResult | None:
        unit = f"{ctx.tags['lg-server']}-g{generation}"
        program = ['/usr/bin/env'] + LANGGRAPH_ENV + ['/bin/sh', '-c', 'cd "$1" && shift && exec "$@"', 'sh', self._app(ctx),
                                                      f'{VENV}/bin/langgraph', 'dev', '--no-browser', '--no-reload',
                                                      '--host', '127.0.0.1', '--port', str(PORT)]
        argv = (['systemd-run', '--scope', '--quiet', f'--unit={unit}', f'--description=stopkit LangGraph server, run {ctx.run_id}',
                 '--'] + as_account_argv(ctx.agent, exec_argv(ctx.bench.truth_sock, ctx.tags['lg-server'], 'server', program)))
        log = open(os.path.join(ctx.run_dir, f'langgraph-server-g{generation}.log'), 'ab')
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                             env=ROOT_ENV)
        log.close()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            if answers():
                break
            if p.poll() is not None:
                raise KitError(f'the LangGraph server exited while starting; see langgraph-server-g{generation}.log')
            time.sleep(0.5)
        else:
            raise KitError('the LangGraph server did not answer within 120 s')
        st = procfs.read_stat(ctx.proc, p.pid)
        handle.popen = p
        handle.pid, handle.start, handle.pgid = p.pid, st['start'] if st else 0, st['pgrp'] if st else p.pid
        handle.scope = f'{unit}.scope'
        handle.details.setdefault('servers', []).append({'generation': generation, 'pid': p.pid,
                                                         'start': handle.start, 'scope': handle.scope,
                                                         'answered_after_s': round(time.monotonic() - t0, 2)})
        return None

    def start(self, ctx: RunContext) -> AgentHandle:
        if ctx.bench is None:
            raise KitError('the LangGraph binding runs only with the bench (stopkit bench)')
        app = self._app(ctx)
        run_as(ctx.agent, ['mkdir', '-p', app])
        for f in ('graph.py', 'langgraph.json'):
            with open(os.path.join(APP, f), 'rb') as src:
                r = run_as(ctx.agent, ['sh', '-c', 'cat > "$1"', 'sh', f'{app}/{f}'], input=src.read().decode())
            if not r.ok:
                raise KitError(f'could not write {f} as {ctx.agent.name}: {r.err.strip()[:200]}')
        handle = AgentHandle(0, 0, 0, None, None, details={'app': app})
        handle.client = {}
        try:
            self._start_server(ctx, handle, 1)
            handle.details['launch_signals'] = procfs.signal_masks(ctx.proc, handle.pid)
            handle.details['launch_limits'] = procfs.limits(ctx.proc, handle.pid)
        except BaseException:
            self.teardown(ctx, handle)
            raise
        return handle

    def plant(self, ctx: RunContext, handle: AgentHandle) -> None:
        st, thread = _http('POST', '/threads', {})
        st2, sentinel = _http('POST', '/threads', {'metadata': {'stopkit': 'sentinel'}})
        if st != 200 or st2 != 200:
            raise KitError(f'creating the threads failed: HTTP {st}, {st2}')
        plan = {'truth': ctx.bench.truth_sock, 'sink': ctx.bench.sink_sock, 'beat': BEAT, 'period': ctx.bench.heartbeat_s,
                'work_period': WORK_PERIOD, 'run': ctx.tags['lg-run'], 'helper': ctx.tags['lg-helper'],
                'cron': ctx.tags['lg-cron']}
        st, run_ = _http('POST', f"/threads/{thread['thread_id']}/runs", {'assistant_id': 'agent', 'input': {'plan': plan}})
        if st != 200 or not isinstance(run_, dict):
            raise KitError(f'starting the run failed: HTTP {st}: {str(run_)[:200]}')
        handle.client.update(thread=thread['thread_id'], run=run_['run_id'], sentinel=sentinel['thread_id'])
        handle.details.update(thread=thread['thread_id'], run=run_['run_id'])

    # ---- what the server holds ----------------------------------------------------------------------------
    def listings(self, ctx, handle) -> list[Listing]:
        return [self._server_listing(ctx, handle), self._run_listing(ctx, handle)]

    def _server_listing(self, ctx, handle) -> Listing:
        """The server's process, followed by the identity it was started with (components.tracked)."""
        alive = handle.pid > 0 and stops.alive(handle.pid, handle.start, ctx.proc)
        return tracked(ctx.tags['lg-server'], handle.pid, handle.start, ctx.agent.name, alive, ctx.runner)

    def _run_listing(self, ctx, handle) -> Listing:
        taken = time.time()
        if 'thread' not in handle.client:
            return failed('langgraph-run', 'all', taken, ['no run was started'])
        st, sentinel = _http('GET', f"/threads/{handle.client['sentinel']}")
        if st != 200:
            return failed('langgraph-run', 'all', taken, [f'the server did not answer for the sentinel thread: HTTP {st}'])
        st, runs = _http('GET', f"/threads/{handle.client['thread']}/runs")
        items = [Item('langgraph-run', 'stopkit-sentinel-langgraph-run', None, 'the sentinel thread, read just before',
                      {'proof': True})]
        if st == 404:
            pass                                     # the thread is gone, and its runs with it
        elif st != 200 or not isinstance(runs, list):
            return failed('langgraph-run', 'all', taken, [f'listing the runs failed: HTTP {st}'])
        else:
            for r in runs:
                mine = r.get('run_id') == handle.client['run']
                items.append(Item('langgraph-run', f"run:{r.get('run_id')}", None,
                                  f"run {r.get('run_id')} {ctx.tags['lg-run'] if mine else ''}".strip(),
                                  {'status': r.get('status'), 'in_force': r.get('status') in IN_FORCE}))
        return Listing('langgraph-run', 'all', True, tuple(items), taken)

    # ---- stops ---------------------------------------------------------------------------------------------
    def stop(self, ctx: RunContext, handle: AgentHandle, stop: str) -> StopClaim:
        t0 = time.time()
        main = (handle.pid, handle.start)
        poll = handle.popen.poll if handle.popen else None
        if stop == 'none':
            return StopClaim(None, 'no stop was used', t0, time.time(), report=NO_REPORT)
        if stop == 'cancel':
            tid, rid = handle.client['thread'], handle.client['run']
            st, body = _http('POST', f'/threads/{tid}/runs/{rid}/cancel?wait=0&action=interrupt')
            st2, after = _http('GET', f'/threads/{tid}/runs/{rid}')
            status = after.get('status') if isinstance(after, dict) else None
            return StopClaim(st in (200, 202, 204) and status not in IN_FORCE,
                             f'cancel: HTTP {st}; the run then reads {status!r}', t0, time.time(),
                             {'cancel_status': st, 'run_status': status}, covers=('lg-run',), report=EXPLICIT)
        if stop == 'delete-thread':
            st, body = _http('DELETE', f"/threads/{handle.client['thread']}")
            return StopClaim(None, f'delete thread: HTTP {st}', t0, time.time(), {'http_status': st},
                             covers=('lg-run',), report=NO_REPORT)
        if stop == 'ctrl-c':
            sent = []
            for _ in range(2):
                if stops.alive(*main, ctx.proc):
                    os.killpg(handle.pgid, signal.SIGINT)
                    sent.append(round(time.time() - t0, 3))
                    time.sleep(1.0)
            left = stops.wait_gone([main], 10.0, ctx.proc, poll)
            return StopClaim(not left, "the server's process exited" if not left else
                             "the server's process was still running 10 s after the second Ctrl+C", t0, time.time(),
                             {'sigint_at_s': sent, 'server_answers': answers()})
        if stop in ('sigterm', 'kill'):
            sig = signal.SIGTERM if stop == 'sigterm' else signal.SIGKILL
            sent = stops.send(*main, sig, ctx.agent.uid, ctx.proc)
            left = stops.wait_gone([main], 10.0, ctx.proc, poll)
            return StopClaim(not left, "the server's process exited" if not left else
                             "the server's process was still running 10 s after the signal", t0, time.time(), {'sent': sent})
        if stop == 'kill-scope':
            return stops.stop_scope(handle.scope, main, ctx.runner, ctx.proc, poll)
        if stop == 'reference':
            steps = []
            if 'thread' in handle.client:
                st, _ = _http('POST', f"/threads/{handle.client['thread']}/runs/{handle.client['run']}/cancel?wait=0&action=interrupt")
                steps.append({'cancel': st})
                st, _ = _http('DELETE', f"/threads/{handle.client['thread']}")
                steps.append({'delete_thread': st})
            host = stops.reference_stop(ctx.agent, ctx.runner, ctx.proc, poll=poll)
            return StopClaim(host.said_stopped, host.message, t0, time.time(), {'langgraph_steps': steps, **dict(host.details)},
                             host.commands, report=EXPLICIT)
        raise KitError(f'the langgraph adapter has no stop {stop!r}; its stops are: {", ".join(STOPS)}')

    def restart(self, ctx, handle) -> list[CmdResult]:
        """A reboot of the server: its process killed if it still runs, then a new server started in the same
        folder, waited for until it answers."""
        out = []
        if handle.pid and stops.alive(handle.pid, handle.start, ctx.proc):
            out.append(CmdResult(('kill', '-KILL', str(handle.pid)), 0 if stops.send(handle.pid, handle.start, signal.SIGKILL,
                                                                                    ctx.agent.uid, ctx.proc) == 'sent' else 1,
                                 '', '', time.time(), 0.0))
            stops.wait_gone([(handle.pid, handle.start)], 10.0, ctx.proc, handle.popen.poll if handle.popen else None)
        generation = len(handle.details.get('servers', [])) + 1
        t = time.time()
        try:
            self._start_server(ctx, handle, generation)
            out.append(CmdResult(('start', f'server generation {generation}'), 0, '', '', t, time.time() - t))
        except KitError as e:
            out.append(CmdResult(('start', f'server generation {generation}'), 1, '', str(e), t, time.time() - t))
        return out

    def teardown(self, ctx: RunContext, handle: AgentHandle) -> None:
        for s in handle.details.get('servers', []):
            ctx.runner(['systemctl', 'stop', s['scope']], timeout=120)
        if handle.popen is not None:
            try:
                handle.popen.wait(timeout=15)
            except subprocess.TimeoutExpired:
                handle.popen.kill()
        run_as(ctx.agent, ['rm', '-rf', '--', self._app(ctx)])
