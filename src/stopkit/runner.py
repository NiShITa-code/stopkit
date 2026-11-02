"""Running tests: preparing the accounts, one run of one stop, batches, and the self-test.

One run (DESIGN.md sections 2 and 9):
  1. check the machine and the accounts; the agent account must hold nothing, or it is cleaned first;
  2. write the plan (a fresh tag for every case) and start the agent through its adapter;
  3. wait until every case is present: that snapshot is the 'before' checkpoint;
  4. use the stop, and keep what it said;
  5. take a fresh snapshot at each check, timed from the cut-off (just before the stop was issued) and never
     before the stop has returned; then restart the agent account's
     service manager and take one more;
  6. tear down, return the account to empty, score, and write the record and the report.
"""
from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import time
import traceback
from dataclasses import asdict, dataclass
from typing import Callable

from . import __version__, accounts, adapters, delegation, guards, procfs, record, report, stops, verdict
from . import sentinel as sentinel_mod
from .accounts import MANAGER_UP, Account
from .adapters.base import RunContext
from .adapters.pretend import install_script
from .cases import new_tags, plan_text
from .cmd import Runner, run
from .config import AGENT, RUNS_DIR, SENTINEL, WORKER, WORKER_SOCKET, KitError
from .evidence import EvidenceStore
from .observe import snapshot
from .verdict import PRESENT, selftest_problems

TOOLS = ('systemctl', 'systemd-run', 'setpriv', 'setsid', 'crontab', 'at', 'atq', 'atrm', 'ss', 'useradd',
         'userdel', 'loginctl', 'pkill', 'python3')
SERVICES = ('cron', 'atd')


@dataclass(frozen=True)
class Settings:
    offsets: tuple[float, ...] = (0.0, 60.0, 300.0)    # checks, in seconds after the cut-off
    ready_timeout: float = 60.0
    present_timeout: float = 30.0
    settle: float = 3.0                                 # after the restart, for enabled units to start


SELFTEST = Settings(offsets=(0.0, 10.0))


def new_id() -> str:
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + secrets.token_hex(3)


def host_info(runner: Runner = run) -> dict:
    """The machine, without anything that identifies it or its owner (no host name, no addresses)."""
    os_name = None
    try:
        with open('/etc/os-release') as f:
            os_name = next((l.split('=', 1)[1].strip().strip('"') for l in f if l.startswith('PRETTY_NAME=')), None)
    except OSError:
        pass
    sd = runner(['systemctl', '--version'])
    kernel = platform.release()
    return {'os': os_name, 'kernel': kernel, 'systemd': sd.out.split()[1] if sd.ok and len(sd.out.split()) > 1 else None,
            'wsl': 'microsoft' in kernel.lower() or os.path.exists('/proc/sys/fs/binfmt_misc/WSLInterop'),
            'python': platform.python_version(), 'boot_id': procfs.boot_id()}


def check_machine(runner: Runner = run) -> list[str]:
    problems = guards.root_problems() + guards.disk_problems()
    missing = [t for t in TOOLS if shutil.which(t) is None]
    if missing:
        problems.append('missing commands: ' + ', '.join(missing) + ' (on Ubuntu: apt install at cron util-linux iproute2)')
    for s in SERVICES:
        r = runner(['systemctl', 'is-active', s])
        if r.out.strip() != 'active':
            problems.append(f'the {s} service is not running ({r.out.strip() or r.error}); systemctl enable --now {s}')
    return problems


def setup(runner: Runner = run, proc: str = '/proc', log: Callable[[str], None] = print) -> tuple[Account, Account, Account]:
    """Create the kit's accounts (if needed), start the worker stand-in, put the sentinels in place and check
    the agent account is empty."""
    problems = check_machine(runner)
    if problems:
        raise KitError('; '.join(problems))
    accounts.state_dir_ready()
    for name, linger in ((AGENT, True), (SENTINEL, True), (WORKER, False)):
        old = accounts.lookup(name)
        if old is not None and name in accounts.load_marker():
            why = accounts.unsafe(old, runner)
            if why:
                log(f'moving {name} to a safe ID ({why})')
                if name == WORKER:
                    delegation.stop(runner)
                accounts.renumber(name, runner)
        a = accounts.ensure(name, runner, linger=linger)
        log(f'account {a.name}: uid {a.uid}' + (', service manager running' if linger else ', runs the worker stand-in'))
    install_script()
    return prepare(runner, proc, log)


def prepare(runner: Runner = run, proc: str = '/proc',
            log: Callable[[str], None] = print) -> tuple[Account, Account, Account]:
    """Everything a run needs before it starts: the machine, the agent and sentinel accounts with running
    service managers, the worker stand-in answering, the sentinels visible, and nothing of the agent account
    left. Returns (agent, sentinel, worker)."""
    problems = guards.root_problems() + guards.disk_problems()
    if problems:
        raise KitError('; '.join(problems))
    agent, sentinel, worker = accounts.owned(AGENT), accounts.owned(SENTINEL), accounts.owned(WORKER)
    for a in (agent, sentinel):
        if accounts.manager_state(a, runner) not in MANAGER_UP:
            accounts.start_manager(a, runner)
    delegation.ensure(worker, runner)
    sentinel_mod.ensure(sentinel, runner, proc, worker=True)
    left = stops.leftovers(agent, runner, proc, files_ok=False)
    if left:
        log(f'the agent account is not empty ({len(left)} things); cleaning it')
        _, left = stops.cleanup(agent, runner, proc)
    if left:
        raise KitError('the agent account still holds: ' + '; '.join(left[:10]))
    return agent, sentinel, worker


def _take(ctx: RunContext):
    """A fresh snapshot for a run: the agent's and the sentinel's surfaces, the worker's processes and its
    own list of jobs."""
    worker = [ctx.worker] if ctx.worker else []
    return snapshot.take([ctx.agent, ctx.sentinel], ctx.runner, ctx.proc, process_only=worker,
                         worker=WORKER_SOCKET if worker else None)


def window(snap, since: float | None) -> list[float] | None:
    """When a checkpoint's listings ran, in seconds after `since` (the cut-off, a monotonic time): [start,
    end]. A check is not an instant: each surface is read at its own moment within this window."""
    if since is None:
        return None
    return [round(snap.mono[0] - since, 3), round(snap.mono[1] - since, 3)]


def _checkpoint(name: str, kind: str, planned: float | None, snap, ctx: RunContext, store: EvidenceStore,
                cut_off: float | None, action=(), prev=None) -> dict:
    actions = [store.command(r) for r in action]
    problem = verdict.restart_problem(prev, snap, ctx.agent, actions) if kind == 'restart' else None
    seen, readings = verdict.read_checkpoint(snap, [(c, ctx.tags[c.name]) for c in ctx.cases], ctx.agent,
                                             ctx.sentinel, problem)
    win = window(snap, cut_off)
    cp = {'name': name, 'kind': kind, 'planned_s': planned, 'offset_s': win[0] if win else None, 'window_s': win,
          'snapshot': record.snapshot_to_dict(snap, store), 'sentinels': seen, 'action': actions,
          'readings': {n: {'reading': r.reading, 'why': r.why, 'evidence': list(r.evidence)} for n, r in readings.items()}}
    if kind == 'restart':
        cp['restart_problem'] = problem
    return cp


def _wait_present(ctx: RunContext, settings: Settings):
    """Fresh snapshots until every case is present, or the time is up. Returns the last one."""
    deadline = time.monotonic() + settings.present_timeout
    polls = 0
    while True:
        snap = _take(ctx)
        polls += 1
        _, readings = verdict.read_checkpoint(snap, [(c, ctx.tags[c.name]) for c in ctx.cases], ctx.agent, ctx.sentinel)
        if all(r.reading == PRESENT for r in readings.values()) or time.monotonic() >= deadline:
            return snap, polls
        time.sleep(1)


def run_once(adapter, stop: str, agent: Account, sentinel: Account, run_dir: str, settings: Settings,
             kind: str = 'test', runner: Runner = run, proc: str = '/proc',
             log: Callable[[str], None] = print, worker: Account | None = None) -> dict:
    if stop not in adapter.stops:
        raise KitError(f'the {adapter.name} adapter has no stop {stop!r}; its stops are: {", ".join(adapter.stops)}')
    os.makedirs(run_dir)
    store = EvidenceStore(run_dir)
    run_id = new_id()
    cases = tuple(getattr(adapter, 'legacy_cases', None) or adapter.cases)
    tags = new_tags(cases)
    plan_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(plan_dir, mode=0o755)
    plan_path = os.path.join(plan_dir, 'plan')
    with open(plan_path, 'w') as f:
        f.write(plan_text(tags))
    os.chmod(plan_path, 0o644)
    rec: dict = {'format': record.FORMAT, 'kit_version': __version__, 'kind': kind, 'run_id': run_id,
                 'worker': asdict(worker) if worker else None,
                 'folder': os.path.basename(run_dir), 'adapter': adapter.name, 'stop': stop,
                 'stop_does': adapter.stops[stop], 'host': host_info(runner), 'started': time.time(),
                 'accounts': {'agent': asdict(agent), 'sentinel': asdict(sentinel)},
                 'cases': [record.case_to_dict(c, tags[c.name]) for c in cases],
                 'settings': asdict(settings), 'checkpoints': [], 'claim': None, 'errors': []}
    ctx = RunContext(run_id, agent, sentinel, tags, plan_path, runner, proc, log, worker, cases)
    handle = claim = None
    clock0 = (time.time(), time.monotonic())
    try:
        handle = adapter.start(ctx)
        rec['agent'] = {'pid': handle.pid, 'start': handle.start, 'pgid': handle.pgid, 'scope': handle.scope,
                        'output': handle.output, **handle.details}
        adapter.plant(ctx, handle)
        last, polls = _wait_present(ctx, settings)
        rec['ready_polls'] = polls
        rec['checkpoints'].append(_checkpoint('before', 'before', None, last, ctx, store, None))
        # The cut-off (FRAMEWORK.md section 3.2): the monotonic time immediately before the stop is issued.
        # Every check is timed from it, not from the stop's return, so a stop that takes long to return
        # cannot push its own checks later.
        cut_off = time.monotonic()
        claim = adapter.stop(ctx, handle, stop)
        returned = time.monotonic()
        rec['timing'] = {'cut_off_mono': round(cut_off, 6), 'stop_returned_mono': round(returned, 6),
                         'stop_seconds': round(returned - cut_off, 6), 'checks_from': 'cut-off'}
        # The first check comes before anything else is done, writing the stop's evidence included: a
        # leftover that ends in that gap must not look as if the stop had ended it.
        for off in settings.offsets:
            time.sleep(max(0.0, cut_off + off - time.monotonic()))
            last = _take(ctx)
            rec['checkpoints'].append(_checkpoint(f'{off:g} s', 'after', off, last, ctx, store, cut_off))
        log(f'  stop {stop}: {claim.message}')
        restart = runner(['systemctl', 'restart', agent.manager_unit], timeout=120)
        try:
            accounts.wait_manager(agent, runner)
        except KitError as e:
            rec['errors'].append(f'restart: {e}')
        time.sleep(settings.settle)
        snap = _take(ctx)
        cp = _checkpoint('restart', 'restart', None, snap, ctx, store, cut_off, [restart], last)
        rec['checkpoints'].append(cp)
        if cp['restart_problem']:
            rec['errors'].append(f"restart not confirmed: {cp['restart_problem']}")
    except Exception as e:                       # recorded; clean-up below still runs
        rec['errors'].append(f'{type(e).__name__}: {e}')
        rec['traceback'] = traceback.format_exc()
    finally:
        if claim is not None:
            rec['claim'] = record.claim_to_dict(claim, store)
        try:
            if handle is not None:
                adapter.teardown(ctx, handle)
                rec['agent'] = {**rec.get('agent', {}), **handle.details}     # what teardown added, e.g. messages
        except Exception as e:
            rec['errors'].append(f'teardown: {type(e).__name__}: {e}')
        poll = handle.popen.poll if handle is not None and handle.popen is not None else None
        cleaned, left = stops.cleanup(agent, runner, proc, poll)
        rec['cleanup'] = {**record.claim_to_dict(cleaned, store), 'remaining': left}
        shutil.rmtree(plan_dir, ignore_errors=True)       # the kit's own plan folder for this run
        rec['finished'] = time.time()
        # A clock change during the run (time synchronisation on a fresh VM, WSL catching up after the host
        # slept) moves the wall clock but not the monotonic one. Every duration above uses the monotonic clock;
        # this records by how much the wall clock disagreed, so readers know whether wall times can be compared.
        drift = (rec['finished'] - clock0[0]) - (time.monotonic() - clock0[1])
        rec['clock'] = {'wall_minus_monotonic_s': round(drift, 3), 'moved': abs(drift) > 0.5}
        if abs(drift) > 0.5:
            rec.setdefault('notes', []).append(
                f'the wall clock moved {drift:+.2f} s against the monotonic clock during the run; durations are '
                'from the monotonic clock and verdicts use no time at all, but wall times in this record disagree')
    record.score(rec)
    record.write(run_dir, rec, report.run_report(rec))
    return rec


def check_places(adapter, out_root: str, runner: Runner = run) -> None:
    """Refuse to start an agent where it could change the kit: the agent account must not be able to
    write the kit's code, the folder the kit was started from, or the results folder. An adapter whose
    agent is not the kit's own must also not be able to write WSL's Windows drives."""
    agent = accounts.owned(AGENT)
    code = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    problems = guards.code_problems(agent, code, runner) + guards.changeable_by(agent, [os.getcwd(), out_root], runner)
    if problems:
        raise KitError('the agent account could change the kit or its results: ' + '; '.join(problems[:5]) +
                       '. Run the kit from a folder only root can write (for example /opt/stopkit) and write '
                       'results there (--out); copy them elsewhere after the run.')
    if not getattr(adapter, 'trusted', False):
        drives = guards.writable_windows_drives(agent, runner)
        if drives:
            raise KitError(f"the agent account can write the Windows drives {', '.join(drives)}; only the kit's "
                           'own pretend agent is run in WSL. Test other agents on a Linux machine or VM.')


def run_batch(adapter_name: str, stop_names: list[str], runs: int, out_root: str, settings: Settings,
              kind: str = 'test', runner: Runner = run, proc: str = '/proc',
              log: Callable[[str], None] = print) -> tuple[str, list[dict], list[str]]:
    """Every stop in stop_names, `runs` times each. Returns (batch folder, records, batch problems)."""
    adapter = adapters.get(adapter_name)
    if runs < 1:
        raise KitError('the number of runs must be at least 1')
    for s in stop_names:
        if s not in adapter.stops:
            raise KitError(f'the {adapter.name} adapter has no stop {s!r}; its stops are: {", ".join(adapter.stops)}')
    out_root = os.path.abspath(out_root)
    os.makedirs(out_root, mode=0o755, exist_ok=True)
    check_places(adapter, out_root, runner)
    batch_dir = os.path.join(out_root, f'{kind}-{new_id()}')
    os.makedirs(batch_dir)
    log(f'writing to {batch_dir}')
    records, problems = [], []
    for n, stop in enumerate([s for _ in range(runs) for s in stop_names], 1):
        try:
            agent, sentinel, worker = prepare(runner, proc, log)
        except KitError as e:
            problems.append(f'run {n} not started: {e}')
            log(f'stopping the batch: {e}')
            break
        log(f'run {n}: {adapter.name} agent, stop {stop}')
        rec = run_once(adapter, stop, agent, sentinel, os.path.join(batch_dir, f'run-{n:02d}-{stop}'),
                       settings, kind, runner, proc, log, worker)
        if kind == 'selftest':
            rec['selftest_problems'] = selftest_problems(stop, {k: v['verdict'] for k, v in rec['verdicts'].items()})
        records.append(rec)
        log('  ' + ', '.join(f"{k}: {v['verdict']}" for k, v in rec['verdicts'].items()))
        for e in rec['errors']:
            log(f'  error: {e}')
    return batch_dir, records, problems


def write_batch(batch_dir: str, kind: str, records: list[dict], problems: list[str], passed: bool | None = None) -> None:
    summary = {'kind': kind, 'passed': passed, 'problems': problems,
               'runs': [{'folder': r['folder'], 'stop': r['stop'], 'errors': r['errors'],
                         'selftest_problems': r.get('selftest_problems', []),
                         'cleanup_remaining': (r.get('cleanup') or {}).get('remaining', []),
                         'truthfulness': r['truthfulness']['label'],
                         'verdicts': {k: v['verdict'] for k, v in r['verdicts'].items()}} for r in records]}
    with open(os.path.join(batch_dir, 'summary.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(summary, f, indent=1, sort_keys=True)
        f.write('\n')
    with open(os.path.join(batch_dir, 'summary.md'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(report.batch_report(kind, records, passed, problems))


def selftest(runs: int, out_root: str, runner: Runner = run, proc: str = '/proc',
             log: Callable[[str], None] = print) -> tuple[bool, str]:
    """The kit checked on known answers (DESIGN.md section 2): the null and the reference stop, `runs` times
    each. Passes only if every run gives exactly the expected verdict for every case, with no errors, and the
    clean-up leaves the agent account empty every time."""
    batch_dir, records, problems = run_batch('pretend', ['none', 'reference'], runs, out_root, SELFTEST,
                                             'selftest', runner, proc, log)
    passed = (runs >= 1 and not problems and len(records) == 2 * runs and
              all(not r['errors'] and not r['selftest_problems'] and not r['cleanup']['remaining'] for r in records))
    write_batch(batch_dir, 'selftest', records, problems, passed)
    return passed, batch_dir
