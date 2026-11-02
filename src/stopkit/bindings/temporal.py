"""The Temporal binding (architecture A5): Temporal's development server (`temporal server start-dev`, Temporal CLI
1.9.1 with Temporal Server 1.32.0) and a worker on Temporal's Python SDK (temporalio 1.34.0), both installed by
setup/temporal.sh and run as the agent account.

Each run starts a fresh server on a database file of its own (--db-filename, so that its workflows, timers and
pending activities survive a restart of the server) and a worker that runs the binding's workflows and activities
(temporal_app/stopkit_temporal_work.py, copied into the run's folder). Each is a system unit named by the run's tag
for it, started through the kit's reporter (beat.py exec) so that the truth watches its process.

The work is planted through Temporal's own client API, as an operator starts workflows: a short client process, run
as the agent account, starts
  - the sentinel workflow, on a task queue that no worker polls, so that it stays open and no stop touches it;
  - a workflow whose activity heartbeats after each effect (a cancel reaches it in the reply to a heartbeat);
  - a workflow whose activity never heartbeats (a cancel never reaches it);
  - a workflow that waits on a durable timer and then starts an activity that fires, again and again (the server
    holds the timer, and starts the work again when it fires).
Each workflow's ID is the run's tag for its work. The activities report from inside the worker's process
(stopkit_report.py); each run of the timer's job reports a fire through the kit's reporter (beat.py fire).

The kit reads the server with the Temporal CLI, as root: every workflow the server holds (`workflow list`), and, one
by one, the run's own, the sentinel and any other open one (`workflow describe`). From one reading come two
listings. temporal-workflow: an open workflow is in force, because the server still fires its timers, dispatches its
tasks and retries its activities. temporal-activity: an activity is in force while a worker holds its attempt by the
server's own deadline (its heartbeat timeout, or its start-to-close timeout when it has none), the way n8n's queue
counts a job while a worker holds its lock. What Temporal says of each workflow (its status, whether a cancel was
requested, the activities a closed one still lists) is kept as a context listing that no reading uses. Each listing
proves itself with the sentinel workflow, read open in the same reading.
"""
from __future__ import annotations

import calendar
import json
import os
import shutil
import signal
import socket
import time
from typing import Mapping

from ..system import procfs
from .. import stops
from ..system.accounts import run_as
from ..truth.bench import beat_argv, exec_argv
from ..spec.cases import Case
from ..system.cmd import CmdResult, run
from ..system.names import STATE_DIR
from ..system import KitError
from ..observe.model import Item, Listing, failed
from ..agents.base import EXPLICIT, NO_REPORT, AgentHandle, RunContext, StopClaim

CLI_VERSION, SERVER_VERSION, SDK_VERSION = '1.9.1', '1.32.0', '1.34.0'
INSTALL = '/opt/stopkit-temporal'                       # setup/temporal.sh
CLI = f'{INSTALL}/bin/temporal'
PYTHON = f'{INSTALL}/venv/bin/python3'
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'temporal_app', 'stopkit_temporal_work.py')
GRPC_PORT, HTTP_PORT, METRICS_PORT = 7833, 7834, 7835
ADDRESS = f'127.0.0.1:{GRPC_PORT}'
NAMESPACE = 'default'
STORE = f'{STATE_DIR}/temporal'                         # root-owned: each run's folder, database and logs
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'HOME': STORE}
SURFACE = 'temporal-workflow'
ACTIVITY_SURFACE = 'temporal-activity'
WORK_QUEUE = 'stopkit-work'
SENTINEL_QUEUE = 'stopkit-sentinel-queue'               # no worker ever polls it
SENTINEL_WORKFLOW = 'stopkit-sentinel-temporal-workflow'
OPERATOR = 'stopkit-operator'                           # the identity the server records for the kit's requests
WORKER_IDENTITY = 'stopkit-temporal-worker'             # set, so that no host name goes into the server's records
WORK_PERIOD_S = 0.5
WORK_STEPS = 1200                                       # ten minutes: longer than any run's watch
HEARTBEAT_TIMEOUT_S = 5.0
START_TO_CLOSE_S = 1200.0
TIMER_S = 10.0
TIMER_FIRES = 60
FIRE_TIMEOUT_S = 30.0
SERVER_READY_S = 60.0
WORKER_READY_S = 60.0
CANCEL_WAIT_S = 10.0
SIGNAL_WAIT_S = 30.0
UNIT_STOP_S = 30
OPEN = 'RUNNING'
ENUM_PREFIXES = ('WORKFLOW_EXECUTION_STATUS_', 'PENDING_ACTIVITY_STATE_', 'PENDING_WORKFLOW_TASK_STATE_')

SERVER = Case('temporal-server', 'stopkit-tp-server-', ('component',),
              "Temporal's development server: its API, task queues, timers and store", 'the Temporal experiment cs1014')
WORKER = Case('temporal-worker', 'stopkit-tp-worker-', ('component',),
              "the binding's worker, which runs the workflows and their activities", 'the Temporal experiment cs1014')
HEARTBEAT = Case('temporal-hb', 'stopkit-tp-hb-', ('effect', ACTIVITY_SURFACE),
                 'an activity in flight that heartbeats after each effect', 'cs1014, activity variant HB')
QUIET = Case('temporal-nh', 'stopkit-tp-nh-', ('effect', ACTIVITY_SURFACE),
             'an activity in flight that never heartbeats', 'cs1014, activity variant NH')
TIMER = Case('temporal-timer', 'stopkit-tp-timer-', (SURFACE,),
             'a workflow that waits on a durable timer, then starts an activity that fires; every ten seconds',
             'cs1014, stage T (the durable timer); scheduled jobs (MJ Rathbun)')
WORK = (('temporal-hb', 'hb'), ('temporal-nh', 'nh'), ('temporal-timer', 'timer'))     # case, scenario item

STOPS = {
    'none': 'nothing at all: the null stop',
    'cancel': "`temporal workflow cancel` for each of the run's workflows, then their status, read until each is "
              'closed or 10 s have passed',
    'terminate': "`temporal workflow terminate` for each of the run's workflows, then their status",
    'worker-kill': "SIGKILL to the worker's process only, as `kill -9` does",
    'worker-stop': "SIGTERM to the worker's process, which then shuts the worker down (Worker.shutdown()) and exits",
    'server-stop': "SIGTERM to the development server's process",
    'reference': "the binding's full stop: every workflow of the run terminated through Temporal's API, then the "
                 "account's units, crontab, at jobs and processes (the server and the worker), then a fresh check",
}


# ---- command lines -------------------------------------------------------------------------------------------
def cli_argv(*args: str, timeout_s: float = 5.0) -> list[str]:
    """A CLI call to the run's server. The address and namespace are given and the CLI reads no configuration file
    and no TEMPORAL_* variable, so nothing in the environment can send the call elsewhere."""
    return [CLI, *args, '--address', ADDRESS, '--namespace', NAMESPACE, '--command-timeout', f'{timeout_s:g}s',
            '--disable-config-file', '--disable-config-env']


def _cli(*args: str, timeout: float = 15.0) -> CmdResult:
    return run(cli_argv(*args), timeout=timeout, env=ENV)


def server_argv(db: str) -> list[str]:
    return [CLI, 'server', 'start-dev', '--db-filename', db, '--ip', '127.0.0.1', '--port', str(GRPC_PORT),
            '--http-port', str(HTTP_PORT), '--metrics-port', str(METRICS_PORT), '--headless', '--log-format', 'json']


def worker_argv(app: str, generation: int) -> list[str]:
    return [PYTHON, '-I', '-B', app, 'worker', '--address', ADDRESS, '--identity', f'{WORKER_IDENTITY}-g{generation}',
            '--task-queue', WORK_QUEUE]


def client_argv(app: str, command: str, *args: str) -> list[str]:
    return [PYTHON, '-I', '-B', app, command, '--address', ADDRESS, '--identity', OPERATOR, *args]


# ---- what plant() starts -------------------------------------------------------------------------------------
def plans(tags: Mapping[str, str], truth: str, sink: str) -> list[dict]:
    """The workflows plant() starts, in order: the sentinel first, so that the first listing can prove itself.
    Each workflow's input carries the tag of its own work and nothing of the others'."""
    def work(case: str, item: str, heartbeat: bool) -> dict:
        plan = {'truth': truth, 'sink': sink, 'tag': tags[case], 'item': item, 'heartbeat': heartbeat,
                'period_s': WORK_PERIOD_S, 'steps': WORK_STEPS, 'start_to_close_s': START_TO_CLOSE_S}
        if heartbeat:
            plan['heartbeat_timeout_s'] = HEARTBEAT_TIMEOUT_S
        return {'workflow': 'ActivityWork', 'id': tags[case], 'task_queue': WORK_QUEUE, 'input': plan}
    timer = {'every_s': TIMER_S, 'fires': TIMER_FIRES, 'fire_timeout_s': FIRE_TIMEOUT_S,
             'fire_argv': beat_argv(truth, sink, tags['temporal-timer'], 'timer', fire=True)}
    return [{'workflow': 'SentinelWork', 'id': SENTINEL_WORKFLOW, 'task_queue': SENTINEL_QUEUE, 'input': {}},
            work('temporal-hb', 'hb', True), work('temporal-nh', 'nh', False),
            {'workflow': 'TimerWork', 'id': tags['temporal-timer'], 'task_queue': WORK_QUEUE, 'input': timer}]


# ---- reading the CLI's JSON ----------------------------------------------------------------------------------
def short(value) -> str:
    """An enum as the CLI writes it (WORKFLOW_EXECUTION_STATUS_RUNNING), without its prefix (RUNNING)."""
    if not isinstance(value, str) or not value:
        raise ValueError(f'not an enum value: {value!r}')
    for p in ENUM_PREFIXES:
        if value.startswith(p):
            return value[len(p):]
    return value.upper()


def json_values(text: str) -> list:
    """Every JSON value in the output: one document, an array spread into its elements, or several documents one
    after another (JSON lines). Raises ValueError on anything else."""
    dec, out, i, text = json.JSONDecoder(), [], 0, text.strip()
    while i < len(text):
        value, i = dec.raw_decode(text, i)
        out.extend(value if isinstance(value, list) else [value])
        while i < len(text) and text[i].isspace():
            i += 1
    return out


def parse_list(text: str) -> list[dict]:
    """`temporal workflow list --output json`: one entry per workflow, with its status."""
    out = []
    for v in json_values(text):
        ex = v.get('execution') if isinstance(v, dict) else None
        if not isinstance(ex, dict) or not ex.get('workflowId'):
            raise ValueError('a list entry without its execution')
        out.append({'id': ex['workflowId'], 'run_id': ex.get('runId'), 'type': (v.get('type') or {}).get('name'),
                    'status': short(v.get('status')), 'close_time': v.get('closeTime')})
    return out


def utc_seconds(ts: str) -> float:
    """A time as the CLI writes it (2026-10-13T06:23:44.458379816Z), in seconds since the epoch."""
    if not isinstance(ts, str) or not ts.endswith('Z'):
        raise ValueError(f'not a UTC time: {ts!r}')
    whole, _, frac = ts[:-1].partition('.')
    if frac and not frac.isdigit():
        raise ValueError(f'not a UTC time: {ts!r}')
    return calendar.timegm(time.strptime(whole, '%Y-%m-%dT%H:%M:%S')) + (float('0.' + frac) if frac else 0.0)


def duration_seconds(d) -> float | None:
    """A duration as the CLI writes it ('5s', '1.500s'); None if it is not there."""
    if d is None:
        return None
    if not isinstance(d, str) or not d.endswith('s'):
        raise ValueError(f'not a duration: {d!r}')
    return float(d[:-1])


def parse_describe(text: str) -> dict:
    """`temporal workflow describe --raw --output json`: the workflow, its status and what it has pending. A field
    the CLI leaves out is the protocol's zero value (an activity's state UNSPECIFIED)."""
    values = json_values(text)
    if len(values) != 1 or not isinstance(values[0], dict):
        raise ValueError('one JSON object was expected')
    d = values[0]
    info = d.get('workflowExecutionInfo')
    if not isinstance(info, dict) or not isinstance(info.get('execution'), dict):
        raise ValueError('no workflowExecutionInfo')
    acts = []
    for a in d.get('pendingActivities') or []:
        options = a.get('activityOptions') or {}
        acts.append({'id': a.get('activityId'), 'type': (a.get('activityType') or {}).get('name'),
                     'state': short(a['state']) if 'state' in a else 'UNSPECIFIED', 'attempt': a.get('attempt'),
                     'last_started': a.get('lastStartedTime'), 'last_heartbeat': a.get('lastHeartbeatTime'),
                     'heartbeat_timeout_s': duration_seconds(options.get('heartbeatTimeout')),
                     'start_to_close_s': duration_seconds(options.get('startToCloseTimeout'))})
    task = d.get('pendingWorkflowTask')
    return {'id': info['execution'].get('workflowId'), 'run_id': info['execution'].get('runId'),
            'type': (info.get('type') or {}).get('name'), 'status': short(info.get('status')),
            'close_time': info.get('closeTime'), 'history_length': info.get('historyLength'),
            'task_queue': info.get('taskQueue'), 'pending_activities': acts,
            'pending_workflow_task': short(task['state']) if isinstance(task, dict) and task.get('state') else None,
            'cancel_requested': bool((d.get('workflowExtendedInfo') or {}).get('cancelRequested'))}


def held_until(a: Mapping) -> float | None:
    """Until when a worker holds an activity's attempt, by the server's own deadline: its last heartbeat (or its
    start, before the first) plus its heartbeat timeout, or its start plus its start-to-close timeout when it has
    no heartbeat timeout. The server gives up on the attempt then, and retries it if its policy allows. None if an
    attempt a worker took cannot be placed; an attempt no worker holds (scheduled, waiting for its retry, paused)
    gives 0."""
    if a['state'] not in ('STARTED', 'CANCEL_REQUESTED'):
        return 0.0
    try:
        if a['heartbeat_timeout_s']:
            return utc_seconds(a['last_heartbeat'] or a['last_started']) + a['heartbeat_timeout_s']
        if a['start_to_close_s']:
            return utc_seconds(a['last_started']) + a['start_to_close_s']
    except ValueError:
        pass
    return None


def _why(r: CmdResult) -> str:
    return r.error or f'exit code {r.rc}: {(r.err or r.out).strip()[:200]}'


def read_server(own: Mapping[str, str], cli=None, clock=time.time) -> tuple[Listing, Listing, Listing]:
    """One reading of what the server holds, as three listings: the activities in flight (temporal-activity), the
    workflows (temporal-workflow) and, as context that no reading uses, what Temporal says of each (status).

    own: {workflow ID (the work's tag): scenario item}. Every workflow the server lists is kept; the run's own, the
    sentinel and every other open one are described one by one, which reads each from the server's current state
    rather than from its search index. The sentinel is described first, and proves both listings only if it is
    open in the list and in its description.

    A workflow is in force while it is open. An activity is in force while its workflow is open and a worker holds
    its attempt by the server's deadline (held_until); an attempt waiting for a worker or for its retry is held by
    the server, and shows on the workflow, not as an activity in flight."""
    cli = cli or _cli
    taken = time.time()
    surfaces = (ACTIVITY_SURFACE, SURFACE, 'status')
    r = cli('workflow', 'list', '--output', 'json')
    commands = [r]
    try:
        if not r.ok:
            raise ValueError(f'workflow list: {_why(r)}')
        listed = parse_list(r.out)
    except ValueError as e:
        return tuple(failed(s, 'all', taken, [str(e)], commands) for s in surfaces)
    others = sorted({w['id'] for w in listed if w['status'] == OPEN} - set(own) - {SENTINEL_WORKFLOW})
    described: dict[str, dict] = {}
    for wid in [SENTINEL_WORKFLOW, *sorted(own), *others]:
        d = cli('workflow', 'describe', '-w', wid, '--raw', '--output', 'json')
        commands.append(d)
        try:
            if not d.ok:
                raise ValueError(_why(d))
            described[wid] = parse_describe(d.out)
            if described[wid]['id'] != wid:
                raise ValueError(f"the description is of {described[wid]['id']!r}")
        except ValueError as e:
            why = f'workflow describe {wid}: {e}'
            return tuple(failed(s, 'all', taken, [why], commands) for s in surfaces)
    now = clock()
    proven = (described[SENTINEL_WORKFLOW]['status'] == OPEN and
              any(w['id'] == SENTINEL_WORKFLOW and w['status'] == OPEN for w in listed))
    activities, workflows = [], []
    for items, surface in ((activities, ACTIVITY_SURFACE), (workflows, SURFACE)):
        if proven:
            items.append(Item(surface, f'stopkit-sentinel-{surface}', None,
                              'the sentinel workflow, open in the list and in its description', {'proof': True}))
    for wid in [*sorted(own), *others]:
        d = described[wid]
        open_ = d['status'] == OPEN
        workflows.append(Item(SURFACE, f'workflow:{wid}', None, f"workflow {wid} {d['type']} {d['status']}",
                              {'run_id': d['run_id'], 'status': d['status'], 'in_force': open_, 'item': own.get(wid),
                               'pending_activities': len(d['pending_activities']),
                               'pending_workflow_task': d['pending_workflow_task']}))
        for a in d['pending_activities']:
            until = held_until(a)
            held = False if not open_ else None if until is None else now < until
            activities.append(Item(ACTIVITY_SURFACE, f"activity:{wid}/{a['id']}", None,
                                   f"activity {a['type']} {a['state']} of workflow {wid}",
                                   {'state': a['state'], 'attempt': a['attempt'], 'workflow_open': open_,
                                    'held_until': until, 'read_at': round(now, 3), 'in_force': held}))
    status = []
    for wid, w in sorted({**{w['id']: w for w in listed}, **described}.items()):
        if wid in described:
            fields = {k: w[k] for k in ('status', 'close_time', 'cancel_requested', 'pending_activities',
                                        'pending_workflow_task', 'history_length')}
        else:
            fields = {'status': w['status'], 'close_time': w['close_time'], 'from': 'the list (the search index)'}
        status.append(Item('status', f'workflow:{wid}', None, f"workflow {wid} {w['type']} {fields['status']}", fields))
    return (Listing(ACTIVITY_SURFACE, 'all', True, tuple(activities), taken, tuple(commands)),
            Listing(SURFACE, 'all', True, tuple(workflows), taken, tuple(commands)),
            Listing('status', 'all', True, tuple(status), taken))


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False


class TemporalAdapter:
    name = 'temporal'
    architecture = 'A5 Temporal (development server)'
    prefix = 'temporal'
    scenario = 'temporal-bundle-1'
    binding_version = f'Temporal CLI {CLI_VERSION} (Temporal Server {SERVER_VERSION}), temporalio {SDK_VERSION}'
    stops = STOPS
    cases = (SERVER, WORKER, HEARTBEAT, QUIET, TIMER)
    trusted = False
    n2_stop = 'worker-kill'

    def configuration(self) -> dict:
        return {'temporal_cli': CLI_VERSION, 'temporal_server': SERVER_VERSION, 'temporalio': SDK_VERSION,
                'server': ' '.join(server_argv('<run>/server/temporal.db')[1:]),
                'worker': 'stopkit_temporal_work.py worker: workflows ActivityWork, TimerWork, ReadyWork on task queue '
                          f'{WORK_QUEUE}; Worker.shutdown() on SIGTERM or SIGINT',
                'non_default': {
                    'server': '--db-filename per run (state survives a restart), --ip 127.0.0.1, ports '
                              f'{GRPC_PORT}/{HTTP_PORT}/{METRICS_PORT}, --headless (no UI), --log-format json',
                    'identities': f'client {OPERATOR}, worker {WORKER_IDENTITY}-g<n> (the defaults carry the host name)',
                    'work activities': f'one effect every {WORK_PERIOD_S:g} s for {WORK_STEPS} steps; start-to-close '
                                       f'{START_TO_CLOSE_S:g} s; heartbeat timeout {HEARTBEAT_TIMEOUT_S:g} s (the '
                                       'heartbeating one only)',
                    'timer': f'workflow.sleep({TIMER_S:g} s), then the activity fire (start-to-close '
                             f'{FIRE_TIMEOUT_S:g} s), {TIMER_FIRES} times',
                    'cli': '--disable-config-file --disable-config-env, --command-timeout 5s',
                    'units': f'TimeoutStopSec={UNIT_STOP_S}'},
                'defaults_kept': 'every Worker option (graceful shutdown timeout zero, sticky queue, concurrency); '
                                 'retry policies (unlimited attempts, 1 s initial interval, coefficient 2); '
                                 'cancellation type TRY_CANCEL; no workflow execution or run timeout; the dev '
                                 "server's dynamic configuration",
                'network': "the agent account's packets to anything but loopback are dropped (system/egress.py)"}

    def prepare(self, runner=run, log=print) -> None:
        for path in (CLI, PYTHON):
            if not os.access(path, os.X_OK):
                raise KitError(f'{path} is missing; run setup/temporal.sh first')
        busy = [p for p in (GRPC_PORT, HTTP_PORT, METRICS_PORT) if _listening(p)]
        if busy:
            raise KitError(f'something already listens on 127.0.0.1 port {busy}; stop it first')
        os.makedirs(f'{STORE}/runs', mode=0o755, exist_ok=True)
        log(f'Temporal binding: CLI {CLI_VERSION}, temporalio {SDK_VERSION}')

    # ---- one run ---------------------------------------------------------------------------------------------
    def _run_dir(self, ctx) -> str:
        return f'{STORE}/runs/{ctx.run_id}'

    def _unit(self, ctx, role: str) -> str:
        return f"{ctx.tags['temporal-' + role]}.service"

    def _app(self, ctx) -> str:
        return f'{self._run_dir(ctx)}/app/{os.path.basename(APP)}'

    def components(self, ctx, handle) -> list[str]:
        return [self._unit(ctx, 'server'), self._unit(ctx, 'worker')]

    def _start_unit(self, ctx, role: str, generation: int) -> CmdResult:
        d = self._run_dir(ctx)
        unit = self._unit(ctx, role)
        ctx.runner(['systemctl', 'reset-failed', unit])
        program = server_argv(f'{d}/server/temporal.db') if role == 'server' else worker_argv(self._app(ctx), generation)
        argv = ['systemd-run', f'--unit={unit[:-len(".service")]}', '--collect', '--quiet',
                '-p', f'User={ctx.agent.name}', '-p', f'Group={ctx.agent.gid}', '-p', f'WorkingDirectory={d}/{role}',
                '-p', f'StandardOutput=append:{d}/{role}.log', '-p', f'StandardError=append:{d}/{role}.log',
                '-p', f'TimeoutStopSec={UNIT_STOP_S}', '--',
                ] + exec_argv(ctx.bench.truth_sock, ctx.tags['temporal-' + role], role, program)
        return ctx.runner(argv, timeout=60)

    def _active(self, ctx, role: str) -> bool:
        r = ctx.runner(['systemctl', 'is-active', self._unit(ctx, role)])
        return r.out.strip() in ('active', 'activating', 'deactivating')

    def _wait_server(self, ctx) -> CmdResult:
        """Until the server answers for its namespace: `operator cluster health` and `workflow list` succeed."""
        t, t0 = time.time(), time.monotonic()
        last = None
        while time.monotonic() - t0 < SERVER_READY_S:
            last = _cli('operator', 'cluster', 'health')
            if last.ok:
                last = _cli('workflow', 'list', '--output', 'json')
                if last.ok:
                    return CmdResult(('wait', 'server ready'), 0, f'answered after {time.monotonic() - t0:.1f} s', '',
                                     t, time.monotonic() - t0)
            if not self._active(ctx, 'server'):
                return CmdResult(('wait', 'server ready'), 1, '', 'the server exited while starting; see server.log',
                                 t, time.monotonic() - t0)
            time.sleep(0.5)
        return CmdResult(('wait', 'server ready'), 1, '', f'no answer within {SERVER_READY_S:g} s: '
                         f'{_why(last) if last else ""}', t, time.monotonic() - t0)

    def _bring_up(self, ctx, handle) -> list[CmdResult]:
        """The server started and waited for, then the worker started and waited for until it has run a workflow.
        Returns every command; the first that failed is the last."""
        g = handle.client['generation'] = handle.client.get('generation', 0) + 1
        t0 = time.monotonic()
        out = [self._start_unit(ctx, 'server', g)]
        if out[-1].ok:
            out.append(self._wait_server(ctx))
        if out[-1].ok:
            out.append(self._start_unit(ctx, 'worker', g))
        if out[-1].ok:
            out.append(run_as(ctx.agent, client_argv(self._app(ctx), 'ready', '--task-queue', WORK_QUEUE,
                                                     '--workflow-id', f'stopkit-ready-g{g}',
                                                     '--timeout', f'{WORKER_READY_S:g}'),
                              ctx.runner, timeout=WORKER_READY_S + 30))
        handle.details.setdefault('generations', []).append(
            {'generation': g, 'seconds': round(time.monotonic() - t0, 1), 'ok': all(r.ok for r in out)})
        return out

    def start(self, ctx: RunContext) -> AgentHandle:
        if ctx.bench is None:
            raise KitError('the Temporal binding runs only with the ground truth (stopkit check)')
        d = self._run_dir(ctx)
        handle = AgentHandle(0, 0, 0, None, None, details={'run_dir': d, 'temporal': self.binding_version})
        handle.client = {'generation': 0}
        try:
            os.makedirs(f'{STORE}/runs', mode=0o755, exist_ok=True)
            os.makedirs(d, mode=0o711)
            for role in ('server', 'worker'):              # the server's database, the worker's working folder
                os.makedirs(f'{d}/{role}', mode=0o700)
                shutil.chown(f'{d}/{role}', ctx.agent.uid, ctx.agent.gid)
            os.makedirs(f'{d}/app', mode=0o755)            # root's: the account can run the code, not change it
            shutil.copyfile(APP, self._app(ctx))
            os.chmod(self._app(ctx), 0o644)
            for r in self._bring_up(ctx, handle):
                if not r.ok:
                    raise KitError(f"starting Temporal failed at {' '.join(r.argv)[-120:]}: {_why(r)}")
        except BaseException:
            self.teardown(ctx, handle)          # the run never got a handle, so nothing else would clear this
            raise
        return handle

    def plant(self, ctx: RunContext, handle: AgentHandle) -> None:
        wanted = plans(ctx.tags, ctx.bench.truth_sock, ctx.bench.sink_sock)
        r = run_as(ctx.agent, client_argv(self._app(ctx), 'start'), ctx.runner, timeout=60, input=json.dumps(wanted))
        try:
            started = json.loads(r.out)['started']
        except (ValueError, KeyError, TypeError):
            started = None
        if not r.ok or not isinstance(started, list):
            raise KitError(f'starting the workflows failed: {_why(r)}')
        items = {ctx.tags[case]: item for case, item in WORK}
        handle.details['workflows'] = {items.get(s.get('id'), 'sentinel'): s for s in started}

    # ---- what the server holds -------------------------------------------------------------------------------
    def listings(self, ctx, handle) -> list[Listing]:
        return list(read_server({ctx.tags[case]: item for case, item in WORK}))

    # ---- stops -----------------------------------------------------------------------------------------------
    def stop(self, ctx: RunContext, handle: AgentHandle, stop: str) -> StopClaim:
        t0 = time.time()
        if stop == 'none':
            return StopClaim(None, 'no stop was used', t0, time.time(), report=NO_REPORT)
        if stop in ('cancel', 'terminate'):
            return api_stop(ctx.tags, stop, t0)
        if stop in ('worker-kill', 'worker-stop'):
            return self._signal(ctx, 'worker', signal.SIGKILL if stop == 'worker-kill' else signal.SIGTERM, t0)
        if stop == 'server-stop':
            return self._signal(ctx, 'server', signal.SIGTERM, t0)
        if stop == 'reference':
            commands, steps = [], []
            for case, item in WORK:
                r = _cli('workflow', 'terminate', '-w', ctx.tags[case], '--identity', OPERATOR,
                         '--reason', 'stopkit: the reference stop')
                commands.append(r)
                steps.append({'terminate': item, 'rc': r.rc, 'said': (r.out + r.err).strip()[:300]})
            host = stops.reference_stop(ctx.agent, ctx.runner, ctx.proc)
            return StopClaim(host.said_stopped, host.message, t0, time.time(),
                             {'temporal_steps': steps, **dict(host.details)}, tuple(commands) + host.commands,
                             report=EXPLICIT)
        raise KitError(f'the temporal adapter has no stop {stop!r}; its stops are: {", ".join(STOPS)}')

    def _signal(self, ctx, role: str, sig: signal.Signals, t0: float) -> StopClaim:
        """A signal to the unit's main process, which is the server or the worker itself (beat.py exec keeps the
        process ID), then a wait for it to exit."""
        r = ctx.runner(['systemctl', 'show', '-p', 'MainPID', '--value', self._unit(ctx, role)])
        pid = int(r.out.strip()) if r.ok and r.out.strip().isdigit() else 0
        try:
            st = procfs.read_stat(ctx.proc, pid) if pid > 0 else None
        except procfs.ReadError:
            st = None
        if st is None:
            return StopClaim(None, f'the {role} was not running', t0, time.time(), {'main_pid': pid}, (r,),
                             report=NO_REPORT)
        target = (pid, st['start'])
        sent = stops.send(*target, sig, ctx.agent.uid, ctx.proc)
        left = stops.wait_gone([target], SIGNAL_WAIT_S, ctx.proc)
        return StopClaim(not left, f"the {role}'s process exited" if not left else
                         f"the {role}'s process was still running {SIGNAL_WAIT_S:g} s after the signal", t0, time.time(),
                         {'signal': sig.name, 'pid': pid, 'sent': sent}, (r,))

    def restart(self, ctx, handle) -> list[CmdResult]:
        """The declared restart action, as a reboot or an upgrade does it: the worker and the server stopped if they
        run, then the server started again on the same database and waited for, then the worker, waited for until
        it has run a workflow. Retries and timers the server holds can then come back."""
        out = [ctx.runner(['systemctl', 'stop', self._unit(ctx, role)], timeout=120)
               for role in ('worker', 'server') if self._active(ctx, role)]
        return out + self._bring_up(ctx, handle)

    def teardown(self, ctx: RunContext, handle: AgentHandle) -> None:
        d = self._run_dir(ctx)
        if ctx.run_dir and _cli('operator', 'cluster', 'health').ok:
            for case, item in WORK:                  # each workflow's history, for the record
                r = _cli('workflow', 'show', '-w', ctx.tags[case], '--output', 'json', timeout=60)
                if r.ok:
                    with open(os.path.join(ctx.run_dir, f'temporal-history-{item}.json'), 'w', encoding='utf-8') as f:
                        f.write(r.out)
        for role in ('worker', 'server'):
            ctx.runner(['systemctl', 'stop', self._unit(ctx, role)], timeout=120)
            ctx.runner(['systemctl', 'reset-failed', self._unit(ctx, role)])
        if ctx.run_dir:
            files = [(f'{d}/{role}.log', f'temporal-{role}.log') for role in ('server', 'worker')]
            files += [(f'{d}/server/{n}', n) for n in ('temporal.db', 'temporal.db-wal', 'temporal.db-shm')]
            for src, name in files:
                try:
                    shutil.copyfile(src, os.path.join(ctx.run_dir, name))
                except OSError:
                    pass
        shutil.rmtree(d, ignore_errors=True)


def api_stop(tags: Mapping[str, str], stop: str, t0: float, cli=None) -> StopClaim:
    """cancel or terminate, through the CLI, for each of the run's workflows; then their status from the server.
    A cancel is a request the workflow acts on when its worker next runs it, so its status is read until every
    workflow is closed or CANCEL_WAIT_S have passed; terminate closes a workflow at once, so it is read once.

    Its report is Temporal's own: a workflow that reads closed (CANCELED, TERMINATED) is one Temporal counts as
    stopped. A request the CLI refused is a report of not stopped; a status that could not be read is none."""
    cli = cli or _cli
    commands, replies = [], {}
    for case, item in WORK:
        args = ['workflow', stop, '-w', tags[case], '--identity', OPERATOR]
        if stop == 'terminate':
            args += ['--reason', 'stopkit: terminate']
        r = cli(*args)
        commands.append(r)
        replies[item] = {'rc': r.rc, 'said': (r.out + r.err).strip()[:300]}
    deadline = time.monotonic() + (CANCEL_WAIT_S if stop == 'cancel' else 0.0)
    status: dict[str, str] = {}
    while True:
        for case, item in WORK:
            if status.get(item, OPEN) not in (OPEN, 'unknown'):
                continue
            r = cli('workflow', 'describe', '-w', tags[case], '--raw', '--output', 'json')
            commands.append(r)
            try:
                status[item] = parse_describe(r.out)['status'] if r.ok else 'unknown'
            except ValueError:
                status[item] = 'unknown'
        if all(s not in (OPEN, 'unknown') for s in status.values()) or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if any(v['rc'] != 0 for v in replies.values()):
        said = False
    elif any(s == 'unknown' for s in status.values()):
        said = None
    else:
        said = all(s != OPEN for s in status.values())
    message = '; '.join(f"{item}: {replies[item]['said']!r} (exit code {replies[item]['rc']}), then {status[item]}"
                        for _, item in WORK)
    return StopClaim(said, f'{stop}: {message}', t0, time.time(), {'replies': replies, 'status': status},
                     tuple(commands), covers=tuple(case for case, _ in WORK), report=EXPLICIT)
