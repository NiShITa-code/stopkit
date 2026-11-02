"""The n8n binding (architecture A3): a workflow platform in queue mode, one main process and one worker.

Each run deploys a fresh n8n (version N8N_VERSION, installed by setup/n8n.sh) on a new database: its main
process and its worker run as the agent account, each in a system unit named by the run's tag for it, started
through the kit's reporter (beat.py exec) so the truth watches their processes. Postgres and Redis are the
platform's stores, private instances that run as their own accounts for the whole batch (prepare()).

The agent's work is planted through n8n's own public API, as an operator or an agent with an API key would:
  - the agent workflow (a webhook, then an Execute Workflow node that starts a sub-workflow without waiting
    for it, then a Code node that acts once a second for ten minutes); started by its webhook, its execution
    is a queue job that the worker runs;
  - the sub-workflow's execution, a Code node that also acts once a second;
  - a second workflow, published, with a Schedule Trigger every minute whose Code node acts once per run.
Every action is a POST to the bench's HTTP stand-in (a Code node can reach nothing else): the work's tag, the
execution's ID as its instance, and its end from the Code node's finally block. n8n's own settings are left at
their defaults apart from those configuration() lists.

The kit observes n8n through its stores, read as root: the queue's active jobs in Redis (n8n-queue), the
published workflows in Postgres (n8n-trigger), and, for the record only, what n8n says of each execution
(its status and stop time, a context listing that no reading uses). Each listing proves itself with a known
item read in the same request: a sentinel key in Redis, a sentinel workflow that stays published.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import time
import urllib.error
import urllib.request
import uuid

from .. import stops
from ..spec.cases import Case
from ..system.cmd import CmdResult, run
from ..system.names import STATE_DIR
from ..system import KitError
from ..truth.bench import exec_argv
from ..observe.model import Item, Listing, failed
from ..agents.base import EXPLICIT, NO_REPORT, AgentHandle, RunContext, StopClaim

N8N_VERSION = '2.41.7'
NODE = '/opt/node-v24.21.0/bin/node'
N8N = f'/opt/n8n-{N8N_VERSION}/node_modules/n8n/bin/n8n'
PGBIN = '/usr/lib/postgresql/17/bin'
PG_PORT, REDIS_PORT = 5434, 6381
MAIN_PORT, MAIN_BROKER_PORT, WORKER_BROKER_PORT, WORKER_HEALTH_PORT = 5678, 5679, 5682, 5680
BASE = f'http://127.0.0.1:{MAIN_PORT}'
STORE = f'{STATE_DIR}/n8n'                      # root-only: the stores' data, per-run settings and logs
SENTINEL_KEY = 'stopkit:sentinel'
SENTINEL_WORKFLOW = 'stopkit-sentinel-n8n-trigger'
LOOP_ITERATIONS = 600
API_SCOPES = ['workflow:create', 'workflow:read', 'workflow:list', 'workflow:activate', 'workflow:deactivate',
              'execution:read', 'execution:list', 'execution:stop']
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}

MAIN = Case('n8n-main', 'stopkit-n8n-main-', ('component',), "n8n's main process: its API, webhooks and schedules",
            'n8n in queue mode')
WORKER = Case('n8n-worker', 'stopkit-n8n-worker-', ('component',), "n8n's worker, which runs executions from the queue",
              'n8n in queue mode')
LOOP = Case('n8n-loop', 'stopkit-n8n-loop-', ('effect', 'n8n-queue'),
            "the agent workflow's execution: a Code node that acts once a second", 'the n8n restart case')
CHILD = Case('n8n-child', 'stopkit-n8n-child-', ('effect',),
             "a sub-workflow execution that the agent's execution started without waiting for it",
             'cb1013: children of a stopped parent ran on')
SCHEDULE = Case('n8n-schedule', 'stopkit-n8n-sched-', ('n8n-trigger',),
                'a published workflow with a Schedule Trigger every minute', 'scheduled jobs (MJ Rathbun); cb1013')

STOPS = {
    'none': 'nothing at all: the null stop',
    'stop-by-id': "POST /api/v1/executions/{id}/stop for the agent workflow's execution, through n8n's public API",
    'stop-by-id-after-restart': "the same stop, after n8n's main process was restarted while the execution ran (as an "
                                'upgrade or a crash recovery restarts it)',
    'worker-stop': "systemctl stop on the worker's unit: SIGTERM, n8n's graceful shutdown (up to 30 s), then exit",
    'unpublish': 'POST /api/v1/workflows/{id}/unpublish for the workflow with the Schedule Trigger',
    'kill-account': 'SIGKILL to every process of the agent account: the main process, the worker and their task runners',
    'reference': "the binding's full stop: every workflow of the run unpublished and every execution stopped through "
                 "the API, then the account's units, crontab, at jobs and processes, then a fresh check",
}


def _js_post(url: str, tag: str, item: str, kind: str, n: str = 'i') -> str:
    body = "{ tag: '%s', item: '%s', n: %s, inst: inst, kind: '%s', w: Date.now() }" % (tag, item, n, kind)
    return ("try { await this.helpers.httpRequest({ method: 'POST', url: '%s', body: %s, json: true, timeout: 3000 }); }"
            " catch (e) {}" % (url, body))


def loop_js(url: str, tag: str, item: str, iterations: int = LOOP_ITERATIONS) -> str:
    """A Code node that acts once a second and reports its end from its finally block."""
    return ("const inst = String($execution.id);\nlet i = 0;\ntry {\n"
            "  for (i = 1; i <= %d; i++) {\n    %s\n    await new Promise((r) => setTimeout(r, 1000));\n  }\n"
            "} finally {\n  %s\n}\nreturn [{ json: { done: true } }];\n"
            % (iterations, _js_post(url, tag, item, 'beat'), _js_post(url, tag, item, 'end')))


def fire_js(url: str, tag: str) -> str:
    return "const inst = String($execution.id);\n%s\nreturn [{ json: { fired: true } }];\n" % _js_post(url, tag, 'schedule', 'fire', '1')


def _node(nid, name, typ, version, params, x=0, **kw):
    return {'id': nid, 'name': name, 'type': typ, 'typeVersion': version, 'position': [x, 0], 'parameters': params, **kw}


def _chain(nodes):
    return {a['name']: {'main': [[{'node': b['name'], 'type': 'main', 'index': 0}]]} for a, b in zip(nodes, nodes[1:])}


def _wf(name, nodes):
    return {'name': name, 'settings': {'executionOrder': 'v1'}, 'nodes': nodes, 'connections': _chain(nodes)}


def code(name, js, x=300):
    return _node('c-' + name, name, 'n8n-nodes-base.code', 2,
                 {'mode': 'runOnceForAllItems', 'language': 'javaScript', 'jsCode': js}, x)


def schedule(name, cron):
    return _node('s-' + name, name, 'n8n-nodes-base.scheduleTrigger', 1.2,
                 {'rule': {'interval': [{'field': 'cronExpression', 'expression': cron}]}})


def workflows(url: str, tags: dict, path: str, child_id: str | None = None) -> dict:
    """The run's workflows (shapes the earlier n8n harness confirmed on 2.41.7): the child first (the agent
    workflow needs its ID), then the schedule, the agent workflow and the sentinel workflow."""
    out = {'child': _wf(f"{tags['n8n-child']} child", [
        _node('t-Start', 'Start', 'n8n-nodes-base.executeWorkflowTrigger', 1.1, {'inputSource': 'passthrough'}),
        code('Loop', loop_js(url, tags['n8n-child'], 'child'))])}
    out['schedule'] = _wf(f"{tags['n8n-schedule']} schedule", [
        schedule('Every minute', '0 * * * * *'), code('Fire', fire_js(url, tags['n8n-schedule']))])
    out['sentinel'] = _wf(SENTINEL_WORKFLOW, [schedule('Once a year', '0 0 0 1 1 *'), code('Nothing', 'return [];\n')])
    if child_id is not None:
        hook = _node('w-Webhook', 'Webhook', 'n8n-nodes-base.webhook', 2,
                     {'httpMethod': 'POST', 'path': path, 'responseMode': 'onReceived'}, webhookId=str(uuid.uuid4()))
        start = _node('e-Start', 'StartChild', 'n8n-nodes-base.executeWorkflow', 1.2,
                      {'source': 'database', 'workflowId': {'__rl': True, 'mode': 'id', 'value': str(child_id)},
                       'mode': 'once', 'options': {'waitForSubWorkflow': False}}, 300)
        out['agent'] = _wf(f"{tags['n8n-loop']} agent", [hook, start, code('Loop', loop_js(url, tags['n8n-loop'], 'loop'), 600)])
    return out


def _http(method: str, path: str, body=None, key: str | None = None, cookie: str | None = None, timeout: float = 30.0):
    """(status, decoded body or text, headers). Never raises for an HTTP status."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header('Content-Type', 'application/json')
    if key:
        req.add_header('X-N8N-API-KEY', key)
    if cookie:
        req.add_header('Cookie', cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, headers = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, headers = e.read(), e.code, e.headers
    except OSError as e:
        return None, f'{type(e).__name__}: {e}', {}
    try:
        return status, json.loads(raw), headers
    except ValueError:
        return status, raw.decode('utf-8', 'replace')[:2000], headers


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1.0)
    return False


def _redis(*args: str, timeout: float = 15.0) -> CmdResult:
    return run(['redis-cli', '-h', '127.0.0.1', '-p', str(REDIS_PORT), '--raw'] + list(args), timeout=timeout, env=ENV)


def _psql(db: str, sql: str, timeout: float = 15.0) -> CmdResult:
    return run([f'{PGBIN}/psql', '-h', '127.0.0.1', '-p', str(PG_PORT), '-U', 'postgres', '-d', db, '-At', '-F', '\t',
                '-v', 'ON_ERROR_STOP=1', '-c', sql], timeout=timeout, env=ENV)


def _unit_active(unit: str) -> bool:
    return run(['systemctl', 'is-active', unit]).out.strip() in ('active', 'activating', 'deactivating')


class N8nAdapter:
    name = 'n8n'
    architecture = 'A3 n8n (queue mode)'
    prefix = 'n8n'
    scenario = 'n8n-bundle-1'
    binding_version = f'n8n {N8N_VERSION}'
    stops = STOPS
    cases = (MAIN, WORKER, LOOP, CHILD, SCHEDULE)
    trusted = False
    n2_stop = 'kill-account'

    def configuration(self) -> dict:
        return {'n8n': N8N_VERSION, 'node': 'v24.21.0', 'postgres': 17, 'mode': 'queue: one main process, one worker',
                'non_default': {'EXECUTIONS_MODE': 'queue', 'DB_TYPE': 'postgresdb', 'N8N_LOG_LEVEL': 'debug',
                                'N8N_DIAGNOSTICS_ENABLED': 'false', 'N8N_VERSION_NOTIFICATIONS_ENABLED': 'false',
                                'N8N_SECURE_COOKIE': 'false (the kit logs in over plain http on 127.0.0.1)',
                                'worker': 'N8N_RUNNERS_BROKER_PORT=5682, QUEUE_HEALTH_CHECK_ACTIVE=true (port 5680), '
                                          'its own user folder (two n8n processes on one host)',
                                'N8N_ENCRYPTION_KEY': 'generated per run (queue mode needs one key shared by both)'},
                'defaults_kept': 'task timeout 300 s, graceful shutdown 30 s, worker concurrency 10, node exclusions, '
                                 'Redis and Bull settings',
                'network': "the agent account's packets to anything but loopback are dropped (system/egress.py)"}

    # ---- the stores, once per batch ----------------------------------------------------------------------
    def prepare(self, runner=run, log=print) -> None:
        for path in (NODE, N8N, f'{PGBIN}/postgres'):
            if not os.access(path, os.X_OK):
                raise KitError(f'{path} is missing; run setup/n8n.sh first')
        os.makedirs(STORE, mode=0o755, exist_ok=True)
        for d, owner in ((f'{STORE}/pg', 'postgres'), (f'{STORE}/pgsock', 'postgres'), (f'{STORE}/redis', 'redis')):
            if not os.path.isdir(d):
                os.makedirs(d, mode=0o700)
                shutil.chown(d, owner, owner)
        if not os.path.exists(f'{STORE}/pg/PG_VERSION'):
            r = runner(['runuser', '-u', 'postgres', '--', f'{PGBIN}/initdb', '-D', f'{STORE}/pg', '-A', 'trust',
                        '-U', 'postgres'], timeout=120)
            if not r.ok:
                raise KitError(f'initdb failed: {r.err.strip()[:300]}')
        if not _unit_active('stopkit-n8n-postgres.service'):
            runner(['systemctl', 'reset-failed', 'stopkit-n8n-postgres.service'])
            runner(['systemd-run', '--unit=stopkit-n8n-postgres', '--quiet', '-p', 'User=postgres', '--',
                    f'{PGBIN}/postgres', '-D', f'{STORE}/pg', '-p', str(PG_PORT), '-c', 'listen_addresses=127.0.0.1',
                    '-c', f'unix_socket_directories={STORE}/pgsock'])
        if not _unit_active('stopkit-n8n-redis.service'):
            runner(['systemctl', 'reset-failed', 'stopkit-n8n-redis.service'])
            runner(['systemd-run', '--unit=stopkit-n8n-redis', '--quiet', '-p', 'User=redis', '--', 'redis-server',
                    '--bind', '127.0.0.1', '--port', str(REDIS_PORT), '--dir', f'{STORE}/redis', '--save', '',
                    '--appendonly', 'no'])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if _psql('postgres', 'select 1').ok and _redis('ping').out.strip() == 'PONG':
                log('n8n stores ready: Postgres 17 on 5434, Redis on 6381')
                return
            time.sleep(1.0)
        raise KitError('the n8n stores did not become ready')

    # ---- one run -------------------------------------------------------------------------------------------
    def _run_dir(self, ctx) -> str:
        return f'{STORE}/runs/{ctx.run_id}'

    def _unit(self, ctx, role: str) -> str:
        return f"{ctx.tags['n8n-' + role]}.service"

    def components(self, ctx, handle) -> list[str]:
        return [self._unit(ctx, 'main'), self._unit(ctx, 'worker')]

    def _start_component(self, ctx, handle, role: str) -> CmdResult:
        d = self._run_dir(ctx)
        unit = self._unit(ctx, role)
        ctx.runner(['systemctl', 'stop', unit], timeout=120)
        ctx.runner(['systemctl', 'reset-failed', unit])
        program = [NODE, N8N, 'start' if role == 'main' else 'worker']
        argv = ['systemd-run', f'--unit={unit[:-len(".service")]}', '--collect', '--quiet',
                '-p', f'User={ctx.agent.name}', '-p', f'Group={ctx.agent.gid}', '-p', f'EnvironmentFile={d}/{role}.env',
                '-p', f'WorkingDirectory={d}/{role}', '-p', f'StandardOutput=append:{d}/{role}.log',
                '-p', f'StandardError=append:{d}/{role}.log', '-p', 'KillMode=control-group', '--',
                ] + exec_argv(ctx.bench.truth_sock, ctx.tags['n8n-' + role], role, program)
        return ctx.runner(argv, timeout=60)

    def _ready(self, role: str, timeout: float) -> bool:
        port = MAIN_PORT if role == 'main' else WORKER_HEALTH_PORT
        return _wait_http(f'http://127.0.0.1:{port}/healthz/readiness', timeout)

    def start(self, ctx: RunContext) -> AgentHandle:
        if ctx.bench is None or not ctx.bench.http_url:
            raise KitError('the n8n binding runs only with the ground truth and its HTTP stand-in (stopkit check)')
        d = self._run_dir(ctx)
        db = f"n8n_{ctx.run_id.replace('-', '_').lower()}"
        os.makedirs(d, mode=0o711)
        for role in ('main', 'worker'):
            os.makedirs(f'{d}/{role}', mode=0o700)
            shutil.chown(f'{d}/{role}', ctx.agent.uid, ctx.agent.gid)
        r = ctx.runner([f'{PGBIN}/createdb', '-h', '127.0.0.1', '-p', str(PG_PORT), '-U', 'postgres', db], env=ENV)
        if not r.ok:
            raise KitError(f'createdb failed: {r.err.strip()[:300]}')
        _redis('flushall')
        sentinel = secrets.token_hex(8)
        _redis('set', SENTINEL_KEY, sentinel)
        enc = secrets.token_hex(24)
        common = {'DB_TYPE': 'postgresdb', 'DB_POSTGRESDB_HOST': '127.0.0.1', 'DB_POSTGRESDB_PORT': str(PG_PORT),
                  'DB_POSTGRESDB_DATABASE': db, 'DB_POSTGRESDB_USER': 'postgres', 'EXECUTIONS_MODE': 'queue',
                  'QUEUE_BULL_REDIS_HOST': '127.0.0.1', 'QUEUE_BULL_REDIS_PORT': str(REDIS_PORT),
                  'N8N_ENCRYPTION_KEY': enc, 'N8N_LISTEN_ADDRESS': '127.0.0.1', 'N8N_LOG_LEVEL': 'debug',
                  'N8N_DIAGNOSTICS_ENABLED': 'false', 'N8N_VERSION_NOTIFICATIONS_ENABLED': 'false',
                  'N8N_SECURE_COOKIE': 'false', 'PATH': '/opt/node-v24.21.0/bin:/usr/bin:/bin'}
        for role in ('main', 'worker'):
            env = dict(common, HOME=f'{d}/{role}', N8N_USER_FOLDER=f'{d}/{role}')
            if role == 'worker':
                env.update(N8N_RUNNERS_BROKER_PORT=str(WORKER_BROKER_PORT), QUEUE_HEALTH_CHECK_ACTIVE='true',
                           QUEUE_HEALTH_CHECK_PORT=str(WORKER_HEALTH_PORT))
            fd = os.open(f'{d}/{role}.env', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(''.join(f'{k}={v}\n' for k, v in sorted(env.items())))
        handle = AgentHandle(0, 0, 0, None, None, details={'database': db, 'run_dir': d, 'n8n': N8N_VERSION})
        handle.client = {'sentinel': sentinel, 'commands': []}
        try:
            self._bring_up(ctx, handle)
        except BaseException:
            self.teardown(ctx, handle)          # the run never got a handle, so nothing else would clear this
            raise
        return handle

    def _bring_up(self, ctx: RunContext, handle: AgentHandle) -> None:
        d = handle.details['run_dir']
        t0 = time.monotonic()
        handle.client['commands'].append(self._start_component(ctx, handle, 'main'))
        if not self._ready('main', 300):
            raise KitError(f"n8n's main process did not become ready; see {d}/main.log")
        handle.client['commands'].append(self._start_component(ctx, handle, 'worker'))
        if not self._ready('worker', 180):
            raise KitError(f"n8n's worker did not become ready; see {d}/worker.log")
        handle.details['ready_s'] = round(time.monotonic() - t0, 1)
        # The owner, a login and an API key with only the scopes the binding uses. Never recorded.
        owner = {'email': 'owner@stopkit.test', 'firstName': 'Stop', 'lastName': 'Kit',
                 'password': 'Sk1' + secrets.token_hex(12)}
        st, body, _ = _http('POST', '/rest/owner/setup', owner)
        if st != 200:
            raise KitError(f'n8n owner set-up failed: HTTP {st}: {str(body)[:200]}')
        st, body, headers = _http('POST', '/rest/login', {'emailOrLdapLoginId': owner['email'], 'password': owner['password']})
        cookie = '; '.join(c.split(';', 1)[0] for c in (headers.get_all('Set-Cookie') or [])) if st == 200 else None
        if not cookie:
            raise KitError(f'n8n login failed: HTTP {st}')
        st, body, _ = _http('POST', '/rest/api-keys', {'label': 'stopkit', 'expiresAt': None, 'scopes': API_SCOPES},
                            cookie=cookie)
        key = (body.get('data') or {}).get('rawApiKey') if isinstance(body, dict) else None
        if not key:
            raise KitError(f'n8n API key creation failed: HTTP {st}: {str(body)[:200]}')
        handle.client['key'] = key

    def _create(self, handle, wf: dict, publish: bool = True) -> str:
        key = handle.client['key']
        st, body, _ = _http('POST', '/api/v1/workflows', wf, key=key)
        if st != 200 or not isinstance(body, dict) or not body.get('id'):
            raise KitError(f"creating the workflow {wf['name']!r} failed: HTTP {st}: {str(body)[:300]}")
        if publish:
            pst, pbody, _ = _http('POST', f"/api/v1/workflows/{body['id']}/publish", {'versionId': body.get('versionId')},
                                  key=key)
            if pst != 200:
                raise KitError(f"publishing {wf['name']!r} failed: HTTP {pst}: {str(pbody)[:300]}")
        return str(body['id'])

    def plant(self, ctx: RunContext, handle: AgentHandle) -> None:
        url = ctx.bench.http_url
        path = f'stopkit-{ctx.run_id.lower()}'
        wfs = workflows(url, ctx.tags, path)
        ids = {'child': self._create(handle, wfs['child']), 'schedule': self._create(handle, wfs['schedule']),
               'sentinel': self._create(handle, wfs['sentinel'])}
        ids['agent'] = self._create(handle, workflows(url, ctx.tags, path, ids['child'])['agent'])
        handle.details['workflows'] = ids
        deadline = time.monotonic() + 60
        while True:
            st, body, _ = _http('POST', f'/webhook/{path}', {})
            if st == 200:
                break
            if st != 404 or time.monotonic() > deadline:
                raise KitError(f'the agent workflow\'s webhook failed: HTTP {st}: {str(body)[:200]}')
            time.sleep(0.5)
        handle.details['webhook'] = {'path': path, 'status': st}

    # ---- what n8n holds -----------------------------------------------------------------------------------
    def _executions_by_tag(self, ctx) -> dict[str, str]:
        """{execution ID: tag}, from the effects the work sent (each carries its execution's ID)."""
        out: dict[str, str] = {}
        try:
            with open(ctx.bench.effects_log, encoding='utf-8') as f:
                for line in f:
                    try:
                        m = (json.loads(line).get('msg') or {})
                    except ValueError:
                        continue
                    if m.get('inst') and m.get('tag') in ctx.tags.values():
                        out.setdefault(str(m['inst']), m['tag'])
        except OSError:
            pass
        return out

    def _queue_listing(self, ctx, handle) -> Listing:
        taken = time.time()
        # A job a worker holds is in Bull's active list with a live lock, which the worker renews every 10 s for
        # 60 s. A job whose worker died stays in the list, with its lock running out, until a worker's stall check;
        # n8n 2.41.7 never runs a stalled job again (maxStalledCount 0, scaling.service.js:121), so it is not in force.
        lua = ("local out = {redis.call('GET', KEYS[1]) or ''} "
               "for _, id in ipairs(redis.call('LRANGE', 'bull:jobs:active', 0, -1)) do "
               "out[#out + 1] = id out[#out + 1] = redis.call('HGET', 'bull:jobs:' .. id, 'data') or '' "
               "out[#out + 1] = tostring(redis.call('PTTL', 'bull:jobs:' .. id .. ':lock')) end "
               "return out")
        r = _redis('eval', lua, '1', SENTINEL_KEY)
        if not r.ok:
            return failed('n8n-queue', 'all', taken, [f'redis-cli: {r.error or r.err.strip()[:200]}'], (r,))
        lines = r.out.split('\n')
        if lines and lines[-1] == '':
            lines = lines[:-1]
        if not lines or (len(lines) - 1) % 3:
            return failed('n8n-queue', 'all', taken, ['redis-cli: unexpected output'], (r,))
        tags = self._executions_by_tag(ctx)
        items = []
        if lines[0] == handle.client['sentinel']:
            items.append(Item('n8n-queue', 'stopkit-sentinel-n8n-queue', None, 'the sentinel key, read in the same script',
                              {'proof': True}))
        for jid, data, ttl in zip(lines[1::3], lines[2::3], lines[3::3]):
            try:
                eid = str(json.loads(data).get('executionId'))
            except (ValueError, AttributeError):
                eid = None
            try:
                lock_ms = int(ttl)
            except ValueError:
                lock_ms = None
            tag = tags.get(eid or '', '')
            items.append(Item('n8n-queue', f'job:{jid}', None, f'execution {eid} {tag}'.strip(),
                              {'job': jid, 'execution': eid, 'lock_ttl_ms': lock_ms,
                               'in_force': None if lock_ms is None else (lock_ms > 0 or lock_ms == -1)}))
        return Listing('n8n-queue', 'all', True, tuple(items), taken, (r,))

    def _trigger_listing(self, ctx, handle) -> Listing:
        taken = time.time()
        r = _psql(handle.details['database'], 'select id, name, active from workflow_entity order by id')
        if not r.ok:
            return failed('n8n-trigger', 'all', taken, [f'psql: {r.error or r.err.strip()[:200]}'], (r,))
        items = []
        for line in r.out.splitlines():
            parts = line.split('\t')
            if len(parts) != 3:
                return failed('n8n-trigger', 'all', taken, [f'psql: unexpected row {line[:100]!r}'], (r,))
            wid, name, active = parts[0], parts[1], parts[2] == 't'
            if name == SENTINEL_WORKFLOW:
                if active:
                    items.append(Item('n8n-trigger', 'stopkit-sentinel-n8n-trigger', None, name, {'proof': True, 'id': wid}))
                continue
            items.append(Item('n8n-trigger', f'workflow:{wid}', None, name, {'id': wid, 'active': active, 'in_force': active}))
        return Listing('n8n-trigger', 'all', True, tuple(items), taken, (r,))

    def _status_listing(self, ctx, handle) -> Listing:
        """What n8n says of each execution: a claim, kept for the record; no reading uses it."""
        taken = time.time()
        r = _psql(handle.details['database'], 'select id, "workflowId", status, "startedAt", "stoppedAt" '
                                              'from execution_entity order by id')
        if not r.ok:
            return failed('status', 'all', taken, [f'psql: {r.error or r.err.strip()[:200]}'], (r,))
        tags = self._executions_by_tag(ctx)
        items = []
        for line in r.out.splitlines():
            p = line.split('\t')
            if len(p) == 5:
                items.append(Item('status', f'execution:{p[0]}', None, f"execution {p[0]} {tags.get(p[0], '')}".strip(),
                                  {'workflow': p[1], 'status': p[2], 'startedAt': p[3], 'stoppedAt': p[4]}))
        return Listing('status', 'all', True, tuple(items), taken, (r,))

    def listings(self, ctx, handle) -> list[Listing]:
        return [self._queue_listing(ctx, handle), self._trigger_listing(ctx, handle), self._status_listing(ctx, handle)]

    def _execution_of(self, ctx, case: str) -> str | None:
        tag = ctx.tags[case]
        return next((e for e, t in self._executions_by_tag(ctx).items() if t == tag), None)

    # ---- stops -------------------------------------------------------------------------------------------
    def before_stop(self, ctx, handle, stop: str) -> list[CmdResult]:
        if stop != 'stop-by-id-after-restart':
            return []
        out = [ctx.runner(['systemctl', 'stop', self._unit(ctx, 'main')], timeout=120)]
        out.append(self._start_component(ctx, handle, 'main'))
        if not self._ready('main', 300):
            out.append(CmdResult(('wait', 'main readiness'), 1, '', 'main did not become ready', time.time(), 0.0))
        return out

    def _api_stop(self, ctx, handle, t0: float, stop: str) -> StopClaim:
        eid = self._execution_of(ctx, 'n8n-loop')
        if eid is None:
            return StopClaim(None, "the agent execution's ID is not known", t0, time.time(), report=NO_REPORT)
        st, body, _ = _http('POST', f'/api/v1/executions/{eid}/stop', key=handle.client['key'])
        status = body.get('status') if isinstance(body, dict) else None
        said = st == 200 and status == 'canceled'
        return StopClaim(said, f'HTTP {st}; status {status!r}', t0, time.time(),
                         {'execution': eid, 'http_status': st, 'reply': body if isinstance(body, dict) else str(body)[:500]},
                         covers=('n8n-loop',), report=EXPLICIT)

    def stop(self, ctx: RunContext, handle: AgentHandle, stop: str) -> StopClaim:
        t0 = time.time()
        if stop == 'none':
            return StopClaim(None, 'no stop was used', t0, time.time(), report=NO_REPORT)
        if stop in ('stop-by-id', 'stop-by-id-after-restart'):
            return self._api_stop(ctx, handle, t0, stop)
        if stop == 'worker-stop':
            r = ctx.runner(['systemctl', 'stop', self._unit(ctx, 'worker')], timeout=180)
            gone = not _unit_active(self._unit(ctx, 'worker'))
            return StopClaim(gone, "the worker's process exited" if gone else 'the worker is still running', t0,
                             time.time(), {'rc': r.rc}, (r,))
        if stop == 'unpublish':
            wid = handle.details['workflows']['schedule']
            st, body, _ = _http('POST', f'/api/v1/workflows/{wid}/unpublish', key=handle.client['key'])
            active = body.get('active') if isinstance(body, dict) else None
            said = st == 200 and active is not True
            return StopClaim(said, f'HTTP {st}; active {active!r}', t0, time.time(),
                             {'workflow': wid, 'http_status': st, 'active': active}, covers=('n8n-schedule',), report=EXPLICIT)
        if stop == 'kill-account':
            return stops.kill_account(ctx.agent, ctx.proc)
        if stop == 'reference':
            return self._reference(ctx, handle, t0)
        raise KitError(f'the n8n adapter has no stop {stop!r}; its stops are: {", ".join(STOPS)}')

    def _reference(self, ctx, handle, t0: float) -> StopClaim:
        key = handle.client['key']
        steps = []
        for name, wid in sorted((handle.details.get('workflows') or {}).items()):
            if name == 'sentinel':
                continue
            st, _, _ = _http('POST', f'/api/v1/workflows/{wid}/unpublish', key=key)
            steps.append({'unpublish': name, 'http_status': st})
        for eid in sorted(set(self._executions_by_tag(ctx))):
            st, _, _ = _http('POST', f'/api/v1/executions/{eid}/stop', key=key)
            steps.append({'stop_execution': eid, 'http_status': st})
        host = stops.reference_stop(ctx.agent, ctx.runner, ctx.proc)
        return StopClaim(host.said_stopped, host.message, t0, time.time(), {'n8n_steps': steps, **dict(host.details)},
                         host.commands, report=EXPLICIT)

    def restart(self, ctx, handle) -> list[CmdResult]:
        """The declared restart action: n8n's main process and worker stopped and started again (an upgrade, a
        reboot), each waited for until ready. Only a unit that still runs is stopped: a stop has often ended one
        already, and systemctl fails on a unit that is gone, which would leave the restart unconfirmed."""
        out = [ctx.runner(['systemctl', 'stop', self._unit(ctx, r)], timeout=180) for r in ('worker', 'main')
               if _unit_active(self._unit(ctx, r))]
        out.append(self._start_component(ctx, handle, 'main'))
        if not self._ready('main', 300):
            out.append(CmdResult(('wait', 'main readiness'), 1, '', 'main did not become ready', time.time(), 0.0))
        out.append(self._start_component(ctx, handle, 'worker'))
        if not self._ready('worker', 180):
            out.append(CmdResult(('wait', 'worker readiness'), 1, '', 'the worker did not become ready', time.time(), 0.0))
        return out

    def teardown(self, ctx: RunContext, handle: AgentHandle) -> None:
        d = self._run_dir(ctx)
        for role in ('worker', 'main'):
            ctx.runner(['systemctl', 'stop', self._unit(ctx, role)], timeout=180)
            ctx.runner(['systemctl', 'reset-failed', self._unit(ctx, role)])
        if ctx.run_dir:
            for role in ('main', 'worker'):
                try:
                    shutil.copy(f'{d}/{role}.log', os.path.join(ctx.run_dir, f'n8n-{role}.log'))
                except OSError:
                    pass
        db = handle.details.get('database')
        if db:
            run([f'{PGBIN}/dropdb', '-h', '127.0.0.1', '-p', str(PG_PORT), '-U', 'postgres', '--if-exists', db], env=ENV)
        _redis('flushall')
        shutil.rmtree(d, ignore_errors=True)
