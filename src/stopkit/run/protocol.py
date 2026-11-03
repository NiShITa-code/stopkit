"""The run protocol with the bench (FRAMEWORK.md section 4): one run of one stop, recorded in the format
stopkit-record/2 (FORMAT.md).

  1. guards, launch normalization and an empty agent account (runner.prepare, accounts.as_account_argv); for an
     agent that is not the kit's own, its account is kept off the network (system/egress.py);
  2. the bench: the truth recorder, the effect sink and the sentinel's reporter, and the HTTP stand-in with its
     own sentinel when the scenario's work reports over HTTP (truth/bench.py);
  3. the binding starts the agent, which plants the scenario's items through their normal paths; the run
     waits until the truth shows every item running (each process watched, each periodic trigger fired once,
     each piece of work inside a platform reporting) and the kit's listings show every path;
  4. the binding's action before the stop, if the stop has one (n8n's main restarted); the 'before'
     checkpoint, with one effect lookback before it and after it when work is read on the effect surface
     (before_wait); the cut-off; the stop; the checks, timed from the cut-off;
  5. once the last check can be read (restart_gap), the declared restart action (the account's service
     manager, or the binding's own: its platform restarted) and its checkpoint; the truth keeps recording for
     one trigger window more, and long enough to read that checkpoint (recording_end);
  6. teardown, the bench stopped, the agent account returned to empty;
  7. scoring: the kit's readings and verdicts from its listings alone, and acted-after from the effect sink;
     then, separately, the truth from the recorder's log; the coverage manifest; the record, its evidence,
     its checksums and the run's own report. With rules=False (check --facts-only) nothing is scored: the
     record keeps the readings and the facts alone, and summarize() applies the rules to the batch later.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time
import traceback
from typing import Callable

from .. import __version__, spec, stops
from ..system import accounts, egress
from ..report import codehash, record, reports, results
from ..score import coverage, ground_truth, verdict
from ..system.accounts import Account
from ..agents.base import RunContext
from ..truth.bench import BEAT, Bench
from ..spec.cases import new_tags, plan_text
from ..system.cmd import Runner, run
from ..system.names import RUNS_DIR, SENTINEL_BEAT, SENTINEL_HTTP, WORKER_SOCKET
from ..system import KitError
from ..system.evidence import EvidenceStore
from ..observe import effects, snapshot
from ..observe.components import list_components
from .runner import host_info, new_id, window

FORMAT = 'stopkit-record/2'
TRUTH_LATENCY_S = 0.05        # exit latency bound; E0 measured at most 3 ms in WSL (26 Oct)

# What the host scenario's pretend agent sets up; other bindings say it themselves (configuration()).
HOST_CONFIGURATION = {'timer': 'OnCalendar=minutely, AccuracySec=1s', 'cron': 'every minute',
                      'at': 'now + 3 minutes', 'service_restart': 'Restart=always, RestartSec=2'}


def binding_of(adapter) -> dict:
    scen = getattr(adapter, 'scenario', None)
    if not scen:
        raise KitError(f'the {adapter.name} adapter has no scenario yet; it runs only with the earlier format '
                       '(stopkit dev test)')
    return {'architecture': adapter.architecture, 'prefix': adapter.prefix, 'scenario': scen}


def _path_id(item, kind: str) -> str:
    return f'{item.id}/{kind}'


def _surface_windows(snap, agent: Account, surfaces) -> dict[str, list[float]]:
    """The moments each surface's listing speaks about (monotonic start and end): when it ran, for a listing of
    what exists; the window it read, for the effect surface, whose reading is "acted in this window". The truth
    is read over the same window, so the two are compared on the same question."""
    out = {}
    for s in surfaces:
        l = verdict.listing_for(snap, s, agent)
        if l is None:
            continue
        i = next((n for n, x in enumerate(snap.listings) if x is l), None)
        if i is not None and i < len(snap.listing_mono):
            out[s] = [round(snap.listing_mono[i][0], 6), round(snap.listing_mono[i][1], 6)]
            if s == 'effect':
                w = next((json.loads(text) for name, text in l.raw if name == 'effect-window'), None)
                if w and isinstance(w.get('since'), (int, float)) and isinstance(w.get('now'), (int, float)):
                    out[s] = [round(w['since'], 6), round(w['now'], 6)]
    return out


def _take(ctx: RunContext, adapter=None, handle=None, scen: spec.Scenario | None = None, since: float | None = None):
    """A fresh snapshot for a run: the agent's and the sentinel's surfaces, the worker's processes and its own
    list of jobs; the effect surface, when the scenario reads work on it (`since` is the window's lower bound:
    None before the stop, the cut-off plus grace after it, the restart action after the restart); and the
    binding's components and stores."""
    extra = []
    bench = ctx.bench
    if bench is not None and scen is not None and scen.effect_lookback_s:
        extra.append(lambda: effects.list_effects(bench.effects_log, time.monotonic(), since, scen.effect_lookback_s,
                                                  bench.sentinels(), ground_truth.CHANNEL_GAP_S, scen.delivery_bound_s))
    if adapter is not None and handle is not None:
        units = adapter.components(ctx, handle) if hasattr(adapter, 'components') else []
        if units:
            extra.append(lambda: list_components(units, ctx.runner))
        if hasattr(adapter, 'listings'):
            extra.append(lambda: adapter.listings(ctx, handle))
    worker = [ctx.worker] if ctx.worker else []
    return snapshot.take([ctx.agent, ctx.sentinel], ctx.runner, ctx.proc, process_only=worker,
                         worker=WORKER_SOCKET if worker else None, extra=extra)


def _checkpoint(name: str, kind: str, planned: float | None, snap, ctx: RunContext, path_cases, surfaces,
                store: EvidenceStore, cut_off: float | None, action=(), prev=None, restart_kind: str = 'manager') -> dict:
    actions = [store.command(r) for r in action]
    problem = verdict.restart_problem(prev, snap, ctx.agent, actions, restart_kind) if kind == 'restart' else None
    seen, readings = verdict.read_checkpoint(snap, path_cases, ctx.agent, ctx.sentinel, problem)
    cp = {'name': name, 'kind': kind, 'planned_s': planned, 'window_s': window(snap, cut_off),
          'surface_windows_mono': _surface_windows(snap, ctx.agent, surfaces),
          'snapshot': record.snapshot_to_dict(snap, store), 'sentinels': seen,
          'proofs': coverage.proofs_at(snap, surfaces, ctx.agent, seen), 'action': actions,
          'readings': {n: {'reading': r.reading, 'why': r.why, 'evidence': list(r.evidence)} for n, r in readings.items()}}
    if kind == 'restart':
        cp['restart_problem'] = problem
        cp['restart_kind'] = restart_kind
    return cp


def _needs(items, tags) -> list[tuple[str, str]]:
    """What the truth must show before the stop: every process reported and watched, every periodic trigger
    fired once, every piece of work inside a platform reporting. An at job cannot show it is armed until it
    runs."""
    need = []
    for it in items:
        for t in it.truth.values():
            if t['oracle'] == 'pidfd' or (t['oracle'] == 'fire' and t['when'] == 'restart'):
                need.append((tags[it.case], 'watch'))
            elif t['oracle'] == 'fire' and t['when'] == 'period':
                need.append((tags[it.case], 'fire'))
            elif t['oracle'] == 'beats':
                need.append((tags[it.case], 'beat'))
    return sorted(set(need))


def _wait_ready(ctx: RunContext, path_cases, bench: Bench, need, timeout: float, take: Callable):
    """Until the truth shows every item running and the kit's listings show every path present, or timeout.
    Returns the last snapshot (the 'before' checkpoint, unless the stop has an action before it) and what
    readiness took."""
    t0 = time.monotonic()
    deadline = t0 + timeout
    polls = 0
    missing: list[str] = []
    absent: list[str] = []
    while True:
        evs = bench.events()
        watched = {e.get('tag') for e in evs if e.get('ev') == 'watch'}
        reported = {}
        for e in evs:
            if e.get('ev') == 'msg':
                reported.setdefault((e.get('msg') or {}).get('type'), set()).add((e.get('msg') or {}).get('tag'))
        seen = {'watch': watched, 'fire': reported.get('fire', set()), 'beat': reported.get('beat', set())}
        missing = [f'{tag} ({what})' for tag, what in need if tag not in seen[what]]
        late = time.monotonic() >= deadline
        if not missing or late:
            snap = take()
            polls += 1
            _, readings = verdict.read_checkpoint(snap, path_cases, ctx.agent, ctx.sentinel)
            absent = sorted(n for n, r in readings.items() if r.reading != verdict.PRESENT)
            if (not missing and not absent) or late:
                return snap, {'seconds': round(time.monotonic() - t0, 3), 'kit_polls': polls,
                              'truth_not_ready': missing, 'kit_not_present': absent}
        time.sleep(0.5)


def before_wait(scen: spec.Scenario, surfaces) -> float:
    """How long the run waits after readiness before the 'before' listing, and again after it before the
    cut-off: one lookback when work is read on the effect surface, otherwise nothing. That listing reads such
    work over one lookback, and the truth reads it present only if it ran throughout that window and acted
    after it (truth.read_beats). Taken at readiness, the window began before work that had only just started
    (each MCP run of 29 Oct), and a stop issued at once could end the work before it acted again (n8n's
    reference stop); either way the truth could not confirm the work was there before the stop."""
    return float(scen.effect_lookback_s or 0.0) if 'effect' in surfaces else 0.0


def recording_end(restarted: float, tail: float, listed: float) -> float:
    """When the run may stop recording: one trigger window after the end of the restart action (tail,
    truth.restart_window), and late enough for the truth to read the last listing, whose end is `listed`: an
    absence needs the recorder alive and the sentinel's reports arriving until STARTUP_S past that listing
    (truth.read_pidfd, read_beats), and the reports are at most CHANNEL_GAP_S apart; half a second to spare.
    Without triggers the recording used to end within half a second of the listing, and nothing that had
    ended could be read absent there."""
    return max(restarted + tail + 1.0, listed + ground_truth.STARTUP_S + ground_truth.CHANNEL_GAP_S + 0.5)


def restart_gap(paths) -> float:
    """How long after the end of the last check's listing the restart action may begin. Long enough that what
    the restart starts again is seen to start after that listing: the truth leaves a start within STARTUP_S of
    a window's end undecided (truth.read_pidfd, read_beats). And, with a periodic trigger, one firing window
    (its period and margin): the truth reads a trigger present at a check only from a firing after the check
    and before the restart (truth.read_fire), so without that window it could not show a trigger still armed
    at the last check, and a false absent there would go unseen. In n8n's runs of 29 Oct the restart began
    within 0.1 s of the last listing: the main process it started again reported 0.2 s after that listing, and
    a schedule still armed had no time to fire, so both readings were indefinite."""
    window = max((t['period_s'] + t['margin_s'] for t in (it.truth[k] for it, k in paths)
                  if t['oracle'] == 'fire' and t.get('when') == 'period'), default=0.0)
    return max(ground_truth.STARTUP_S + 0.5, window)


def run_once(adapter, stop: str, agent: Account, sentinel: Account, worker: Account | None, run_dir: str,
             batch_id: str, checks: tuple[float, ...] | None = None, settle: float | None = None, kind: str = 'test',
             runner: Runner = run, proc: str = '/proc', log: Callable[[str], None] = print,
             instruments: bool = True, rules: bool = True) -> dict:
    """One run of one stop. checks, when given, replace the scenario's (the self-test's short runs); the
    record says which were used. With instruments=False the truth recorder is not run (the work's reports to
    it go nowhere), for the non-interference test N3; such a run has no truth. With rules=False the run is the
    same, but no rule is applied to it: the record holds the readings and facts() and no verdict."""
    b = binding_of(adapter)
    scen = spec.scenario(b['scenario'])
    if stop not in adapter.stops:
        raise KitError(f'the {adapter.name} adapter has no stop {stop!r}; its stops are: {", ".join(adapter.stops)}')
    sspec = spec.stop(f"{b['prefix']}.{stop}")
    checks = tuple(checks) if checks else scen.checks_s
    settle = scen.restart_settle_s if settle is None else settle
    os.makedirs(run_dir)
    store = EvidenceStore(run_dir)
    run_id = new_id()
    cases = {c.name: c for c in adapter.cases}
    items = [it for it in scen.items if it.case in cases]
    tags = new_tags([cases[it.case] for it in items])
    paths = [(it, k) for it in items for k in it.paths]
    path_cases = [(dataclasses.replace(cases[it.case], name=_path_id(it, k), surfaces=tuple(it.paths[k])),
                   tags[it.case]) for it, k in paths]
    surfaces = sorted({s for it, k in paths for s in it.paths[k]})
    bench = Bench(run_id, run_dir, [agent.uid] + ([worker.uid] if worker else []), sentinel, scen.heartbeat_s,
                  http='http' in scen.effect_channels)
    plan_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(plan_dir, mode=0o755)
    plan_path = os.path.join(plan_dir, 'plan')
    configuration = adapter.configuration() if hasattr(adapter, 'configuration') else dict(HOST_CONFIGURATION)
    rec: dict = {
        'format': FORMAT, 'kit_version': __version__, 'spec_version': spec.version(),
        'git_commit': os.environ.get('STOPKIT_GIT_COMMIT'), 'git_dirty': os.environ.get('STOPKIT_GIT_DIRTY'),
        'code_sha256': codehash.code_sha256(),
        'run_id': run_id, 'batch_id': batch_id, 'kind': kind, 'evidence_origin': 'framework',
        'folder': os.path.basename(run_dir), 'started': time.time(), 'instruments': instruments,
        'system': {'architecture': b['architecture'], 'binding': adapter.name,
                   'binding_version': getattr(adapter, 'binding_version', __version__),
                   'scenario': scen.name, 'platform': host_info(runner),
                   'configuration': {'heartbeat_s': scen.heartbeat_s, 'checks_s': list(checks),
                                     'scenario_checks_s': list(scen.checks_s), 'settle_s': settle, **configuration}},
        'stop_spec': {**dataclasses.asdict(sspec), 'class': sspec.cls, 'adapter_stop': stop,
                      'adapter_says': adapter.stops[stop]},
        'accounts': {'agent': dataclasses.asdict(agent), 'sentinel': dataclasses.asdict(sentinel),
                     'worker': dataclasses.asdict(worker) if worker else None},
        'cases': [record.case_to_dict(cases[it.case], tags[it.case]) for it in items],
        'paths': [{'id': _path_id(it, k), 'item': it.id, 'kind': k, 'case': it.case, 'surfaces': list(it.paths[k]),
                   'tag': tags[it.case], 'truth': it.truth[k]} for it, k in paths],
        'not_realised': [it.id for it in scen.items if it.case not in cases],
        'checkpoints': [], 'claim': None, 'errors': [], 'notes': []}
    # A scenario and stop from outside the spec files (an agent's config file) travel with the record, so the run
    # can be scored again where that file is not (spec.register_from_record).
    if spec.registered('scenario', scen.name):
        rec['system']['scenario_data'] = spec.registered('scenario', scen.name)
    if spec.registered('stop', sspec.id):
        rec['stop_spec']['register_entry'] = spec.registered('stop', sspec.id)
    ctx = RunContext(run_id, agent, sentinel, tags, plan_path, runner, proc, log, worker,
                     tuple(cases[it.case] for it in items), bench, run_dir)
    handle = claim = None
    times: dict = {}
    clock0 = (time.time(), time.monotonic())
    tail = max((ground_truth.restart_window(it.truth[k]) for it, k in paths if it.truth[k]['oracle'] == 'fire'), default=0.0)
    wait = before_wait(scen, surfaces)
    gap = restart_gap(paths)
    blocked = not getattr(adapter, 'trusted', False)
    take = lambda since=None: _take(ctx, adapter, handle, scen, since)    # noqa: E731
    try:
        if blocked:
            rec['egress'] = {'blocked': True, 'commands': [store.command(r) for r in egress.block(agent, runner)]}
        bench.start(truth=instruments)
        rec['bench'] = bench.info()
        with open(plan_path, 'w') as f:
            f.write(plan_text(tags) + f'@truth {bench.truth_sock}\n@sink {bench.sink_sock}\n@beat {BEAT}\n'
                    f'@period {scen.heartbeat_s:g}\n' + (f'@http {bench.http_url}\n' if bench.http_url else ''))
        os.chmod(plan_path, 0o644)
        times['plant'] = time.monotonic()
        handle = adapter.start(ctx)
        rec['agent'] = {'pid': handle.pid, 'start': handle.start, 'pgid': handle.pgid, 'scope': handle.scope,
                        'output': handle.output, **{k: v for k, v in handle.details.items() if k not in ('launch_signals', 'launch_limits')}}
        rec['launch'] = {'signals': handle.details.get('launch_signals'), 'limits': handle.details.get('launch_limits'),
                         'working_directory': agent.home,
                         'environment': 'the account login environment only (env -i)'}
        adapter.plant(ctx, handle)
        need = _needs(items, tags) if instruments else []
        snap, rec['ready'] = _wait_ready(ctx, path_cases, bench, need, scen.ready_timeout_s, take)
        if hasattr(adapter, 'before_stop'):
            t0 = time.monotonic()
            done = adapter.before_stop(ctx, handle, stop) or []
            if done:
                rec['before_stop'] = {'actions': [store.command(r) for r in done],
                                      'from_mono': t0, 'to_mono': time.monotonic()}
                bad = [r for r in done if not r.ok]
                if bad:
                    rec['errors'].append(f"the action before the stop failed: {' '.join(bad[0].argv)[:200]}")
                snap = take()
        if wait:
            time.sleep(wait)
            snap = take()
        rec['checkpoints'].append(_checkpoint('before', 'before', None, snap, ctx, path_cases, surfaces, store, None))
        time.sleep(wait)
        times['cut_off'] = time.monotonic()           # the cut-off: immediately before the stop is issued
        claim = adapter.stop(ctx, handle, stop)
        times['returned'] = time.monotonic()
        last = snap
        for off in checks:
            time.sleep(max(0.0, times['cut_off'] + off - time.monotonic()))
            last = take(times['cut_off'] + scen.grace_s)
            rec['checkpoints'].append(_checkpoint(f'{off:g} s', 'after', off, last, ctx, path_cases, surfaces,
                                                  store, times['cut_off']))
        log(f'  stop {stop}: {claim.message}')
        last_listed = max((w[1] for w in rec['checkpoints'][-1]['surface_windows_mono'].values()),
                          default=time.monotonic())
        time.sleep(max(0.0, last_listed + gap - time.monotonic()))
        times['restart'] = time.monotonic()
        if hasattr(adapter, 'restart'):
            restart_kind, restart = 'components', adapter.restart(ctx, handle)
        else:
            restart_kind = 'manager'
            restart = [runner(['systemctl', 'restart', agent.manager_unit], timeout=120)]
            try:
                accounts.wait_manager(agent, runner)
            except KitError as e:
                rec['errors'].append(f'restart: {e}')
        times['restarted'] = time.monotonic()
        time.sleep(settle)
        snap = take(times['restart'])
        cp = _checkpoint('restart', 'restart', None, snap, ctx, path_cases, surfaces, store, times['cut_off'],
                         restart, last, restart_kind)
        rec['checkpoints'].append(cp)
        if cp['restart_problem']:
            rec['errors'].append(f"restart not confirmed: {cp['restart_problem']}")
        listed = max((w[1] for w in cp['surface_windows_mono'].values()), default=time.monotonic())
        time.sleep(max(0.0, recording_end(times['restarted'], tail, listed) - time.monotonic()))
    except Exception as e:                       # recorded; clean-up below still runs
        rec['errors'].append(f'{type(e).__name__}: {e}')
        rec['traceback'] = traceback.format_exc()
    finally:
        times['end'] = time.monotonic()
        if claim is not None:
            rec['claim'] = record.claim_to_dict(claim, store)
        try:
            if handle is not None:
                adapter.teardown(ctx, handle)
        except Exception as e:
            rec['errors'].append(f'teardown: {type(e).__name__}: {e}')
        rec['bench_exit'] = bench.stop()
        poll = handle.popen.poll if handle is not None and handle.popen is not None else None
        cleaned, left = stops.cleanup(agent, runner, proc, poll)
        rec['cleanup'] = {**record.claim_to_dict(cleaned, store), 'remaining': left}
        if blocked:
            rec.setdefault('egress', {'blocked': True})
            rec['egress']['status'] = store.command(egress.status(runner))
            rec['egress']['removed'] = store.command(egress.unblock(runner))
        shutil.rmtree(plan_dir, ignore_errors=True)
        rec['finished'] = time.time()
        drift = (rec['finished'] - clock0[0]) - (time.monotonic() - clock0[1])
        rec['clock'] = {'wall_minus_monotonic_s': round(drift, 3), 'moved': abs(drift) > 0.5}
    rec['timing'] = _timing(times, scen.grace_s, tail, wait, gap)
    if rules:
        score(rec, scen, sspec, run_dir)
    else:
        rec['rules'] = {'applied': False}
        rec['facts'] = facts(rec, scen, run_dir)
    write(run_dir, rec)
    return rec


def _timing(times: dict, grace: float, tail: float, before: float = 0.0, gap: float = 0.0) -> dict:
    c = times.get('cut_off')
    rel = (lambda k: round(times[k] - c, 6) if c is not None and k in times else None)
    return {'plant_mono': times.get('plant'), 'cut_off_mono': c, 'stop_returned_mono': times.get('returned'),
            'before_wait_s': before, 'restart_gap_s': gap,
            'restart_action_mono': times.get('restart'), 'restart_done_mono': times.get('restarted'),
            'end_mono': times.get('end'),
            'stop_sent_rel_s': 0.0 if c is not None else None, 'stop_returned_rel_s': rel('returned'),
            'stop_seconds': rel('returned'), 'restart_action_rel_s': rel('restart'),
            'restart_done_rel_s': rel('restarted'), 'end_rel_s': rel('end'),
            'grace_s': grace, 'truth_tail_s': tail, 'checks_from': 'cut-off', 'truth_latency_s': TRUTH_LATENCY_S,
            'truth_startup_s': ground_truth.STARTUP_S}


def sentinels_of(rec: dict) -> list[str]:
    """The sentinels' tags of a run: one per path its work reported by (records before 0.4.0.dev2 had one)."""
    b = rec.get('bench') or {}
    return [b.get('sentinel_tag') or SENTINEL_BEAT] + ([b['http_sentinel_tag']] if b.get('http_sentinel_tag') else [])


def seen_after(items: list[dict], after: list[dict], restart: dict | None, unread: set[str]) -> dict[str, bool]:
    """Per path, whether it was present at a check after the stop, or after the restart. A path of an item in
    `unread` (marked "restart_read": false: the restart action itself starts it again) is judged without the
    restart, as its verdict and the truth are: being there after the restart says nothing about the stop."""
    return {f"{i['id']}/{i['kind']}": any(i['readings'][c['name']] == verdict.PRESENT for c in after)
            or (restart is not None and i['id'] not in unread and i['readings'][restart['name']] == verdict.PRESENT)
            for i in items}


def effect_log(rec: dict, run_dir: str) -> tuple[list[dict], list[str], str | None, float]:
    """The effect sink's events and problems for a run; whether the sink is shown working from the cut-off to the
    end of what the run observes (None, or why not); and that end."""
    t = rec['timing']
    try:
        evs, problems = effects.load(os.path.join(run_dir, 'effects.jsonl'))
    except OSError as e:
        evs, problems = [], [f'no effect log: {e}']
    cut = t['cut_off_mono']
    obs_end = (t.get('restart_done_mono') or t['restart_action_mono'] or cut or 0) + (t['truth_tail_s'] or 0)
    sentinels = sentinels_of(rec)
    proof_of = sentinels[0] if len(sentinels) == 1 else sentinels
    unproven = ('no cut-off' if cut is None else
                effects.proof(evs, proof_of, cut, min(obs_end, t['end_mono'] or obs_end), ground_truth.CHANNEL_GAP_S))
    return evs, problems, unproven, obs_end


def facts(rec: dict, scen: spec.Scenario, run_dir: str) -> dict:
    """What a run saw, with no rule applied (check --facts-only): for each planted item, how many of its actions
    reached the effect sink after the cut-off and when the first and the last were sent, and whether the sink is
    shown working. A time is the work's own clock where it stamps its actions, otherwise the action's arrival.
    The readings are in the checkpoints; summarize() later scores the same record and logs."""
    evs, problems, unproven, _ = effect_log(rec, run_dir)
    cut = rec['timing']['cut_off_mono']
    items: dict = {}
    for p in rec['paths']:
        if p['item'] in items:                   # the paths of one item share its tag, and so its actions
            continue
        sent = sorted(effects.issued(e, scen.delivery_bound_s)[1] - cut for e in evs
                      if cut is not None and e.get('ev') == 'effect' and (e.get('msg') or {}).get('tag') == p['tag'])
        after = [round(x, 3) for x in sent if x > 0]
        items[p['item']] = {'after_cut_off': len(after), 'first_s': after[0] if after else None,
                            'last_s': after[-1] if after else None}
    return {'effects': {'log': 'effects.jsonl', 'sha256': _sha(os.path.join(run_dir, 'effects.jsonl')),
                        'shown_working': unproven is None, 'why_not': unproven, 'problems': problems},
            'items': items}


def score(rec: dict, scen: spec.Scenario, sspec: spec.Stop, run_dir: str) -> None:
    """Every verdict of the run, from its own checkpoints and logs. The kit's verdicts and acted-after come
    first, from the listings and the effect sink; the truth is scored after, from the recorder's log alone."""
    cps = rec['checkpoints']
    t = rec['timing']
    before = next((c for c in cps if c['kind'] == 'before'), None)
    after = [c for c in cps if c['kind'] == 'after']
    restart = next((c for c in cps if c['kind'] == 'restart'), None)
    restart_confirmed = None if restart is None else restart.get('restart_problem') is None
    items_by_id = {it.id: it for it in scen.items}
    effects_log = os.path.join(run_dir, 'effects.jsonl')
    delay = scen.delivery_bound_s
    evs, eproblems, unproven, obs_end = effect_log(rec, run_dir)
    cut = t['cut_off_mono']
    out = []
    for p in rec['paths']:
        it = items_by_id[p['item']]
        pid, k = p['id'], p['kind']
        b = before['readings'][pid]['reading'] if before else verdict.UNKNOWN
        a = [c['readings'][pid]['reading'] for c in after]
        read_after_restart = it.raw.get('restart_read', True)
        r = restart['readings'][pid]['reading'] if restart and read_after_restart else None
        v, why = verdict.final(b, a, r)
        fam = spec.family_of(k)
        family_1 = [kk for kk in it.paths if spec.family_of(kk) == '1']
        if cut is None:
            acted = {'count': None, 'within_grace': None, 'last_s': None, 'unproven': 'the stop was not reached'}
        elif fam == '1' or not family_1:
            acted = effects.acted_after(evs, p['tag'], cut, t['grace_s'], unproven, t['end_mono'], delay)
        else:                                    # the item's activity is counted once, on its family 1 path
            acted = {'count': 0, 'within_grace': 0, 'last_s': None, 'counted_on': family_1[0]}
        completed = None
        if p['truth'].get('when') == 'at' and cut is not None:
            completed = any((e.get('msg') or {}).get('tag') == p['tag'] and e['msg'].get('fire')
                            and e['msg'].get('t', 0) > cut for e in evs if e.get('ev') == 'effect')
        out.append({'id': it.id, 'kind': k, 'family': fam, 'structure': it.structure_of(k),
                    'in_scope': sspec.in_scope(it.structure_of(k)), 'surfaces': p['surfaces'], 'tag': p['tag'],
                    'readings': {c['name']: c['readings'][pid]['reading'] for c in cps},
                    'verdict': v, 'why': why, 'restart_confirmed': restart_confirmed if read_after_restart else None,
                    'acted_after': acted,
                    'ended_by_completion': completed,
                    'evidence': [f"checkpoints[{c['name']}].readings[{pid}]" for c in cps]})
    rec['items'] = out
    rec['effects'] = {'log': 'effects.jsonl', 'sha256': _sha(effects_log), 'proof': unproven, 'problems': eproblems,
                      'window_mono': [cut, obs_end]}
    # Work placed by its arrival is placed with the scenario's delivery bound. Where it also sent its wall clock,
    # every effect measures the bound: say how often it was exceeded.
    delays = sorted(e['msg']['arrival_wall'] - e['msg']['w'] / 1000.0 for e in evs if e.get('ev') == 'effect'
                    and isinstance((e.get('msg') or {}).get('w'), (int, float))
                    and isinstance(e['msg'].get('arrival_wall'), (int, float)))
    if delays:
        rec['effects']['delivery_wall_s'] = {'n': len(delays), 'median': round(delays[len(delays) // 2], 4),
                                             'max': round(delays[-1], 4), 'bound': delay,
                                             'over_bound': sum(1 for d in delays if d > delay)}
    # Report truthfulness, judged on the paths the report covers: its claimed scope when it names one,
    # otherwise the stop's intended scope.
    claim = rec.get('claim') or {}
    covers = set(claim['covers']) if claim.get('covers') is not None else None
    covered = [i for i, p in zip(out, rec['paths']) if (p['case'] in covers if covers is not None else i['in_scope'])]
    seen = seen_after(covered, after, restart, {i.id for i in scen.items if not i.raw.get('restart_read', True)})
    label, why = verdict.truthfulness(claim.get('said_stopped'), {f"{i['id']}/{i['kind']}": i['verdict'] for i in covered},
                                      seen, claim.get('report', 'none'))
    rec['truthfulness'] = {'label': label, 'why': why, 'judged_on': sorted(seen)}
    # Coverage: which surfaces were proven at which checkpoint, and what was not tested.
    realised = dataclasses.replace(scen, items=tuple(i for i in scen.items if i.id in {p['item'] for p in rec['paths']}))
    rec['coverage'] = coverage.manifest(realised, {c['name']: c['proofs'] for c in cps})
    # The truth, last, from the recorder's log alone.
    rec['truth'] = score_truth(rec, run_dir, scen)
    rows = results.rows(rec)
    rec['scope_result'] = results.scope_result(rows)


def score_truth(rec: dict, run_dir: str, scen: spec.Scenario | None = None) -> dict:
    t = rec['timing']
    log_path = os.path.join(run_dir, 'truth.jsonl')
    if rec.get('instruments') is False:
        return {'status': 'failed', 'why': 'the run was made without the truth recorder (a non-interference run)',
                'items': {}}
    if t.get('cut_off_mono') is None or t.get('plant_mono') is None:
        return {'status': 'failed', 'why': 'the run did not reach the stop', 'items': {}}
    try:
        lg = ground_truth.load(log_path)
    except (OSError, ValueError) as e:
        return {'status': 'failed', 'why': f'the truth log cannot be read: {e}', 'items': {}}
    cps = rec['checkpoints']
    times = ground_truth.Times(t['cut_off_mono'], t['plant_mono'], t.get('restart_action_mono'), t['end_mono'],
                        tuple((c['name'], c['kind']) for c in cps),
                        {c['name']: {s: tuple(w) for s, w in c['surface_windows_mono'].items()} for c in cps},
                        t.get('restart_done_mono'))
    items_raw = {it.id: it.raw for it in (scen.items if scen is not None else ())}
    paths = [(p['item'], p['kind'], p['tag'], {**p['truth'], 'surfaces': p['surfaces'],
                                               'restart_read': items_raw.get(p['item'], {}).get('restart_read', True)})
             for p in rec['paths']]
    sentinels = sentinels_of(rec)
    delay = scen.delivery_bound_s if scen is not None else 0.0
    res = ground_truth.score(lg, paths, times, sentinels[0] if len(sentinels) == 1 else sentinels, t['grace_s'],
                      t['truth_latency_s'], delay)
    res.update(log='truth.jsonl', sha256=_sha(log_path), latency_s=t['truth_latency_s'], startup_s=t['truth_startup_s'])
    return res


# A run folder's checksums (report/record.py), which `stopkit logs` reads too; _sums keeps the name that the
# checks of the earlier experiments use.
_sha, _sums, sums_problems = record.file_sha, record.folder_sums, record.folder_sums_problems


def write(run_dir: str, rec: dict) -> None:
    with open(os.path.join(run_dir, 'record.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(rec, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write('\n')
    with open(os.path.join(run_dir, 'report.md'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(reports.run_details(rec))
    with open(os.path.join(run_dir, 'SHA256SUMS'), 'w', newline='\n') as f:
        f.write(_sums(run_dir))


def load(run_dir: str) -> dict:
    with open(os.path.join(run_dir, 'record.json'), encoding='utf-8') as f:
        rec = json.load(f)
    if rec.get('format') != FORMAT:
        raise ValueError(f"{run_dir}: not a {FORMAT} record (format {rec.get('format')!r})")
    spec.register_from_record(rec)
    return rec


def recheck(run_dir: str) -> list[str]:
    """Differences between a record and what its own evidence gives when scored again (empty if none): every
    reading from the stored listings, every verdict, acted-after from the effect log, and the truth from the
    recorder's log. A record made with rules=False has readings and facts but no verdicts, so those two are
    compared. It never changes the folder."""
    problems: list[str] = sums_problems(run_dir)
    try:
        rec = load(run_dir)
        agent = record.account_from_dict(rec['accounts']['agent'])
        sentinel = record.account_from_dict(rec['accounts']['sentinel'])
        cases = {c['name']: record.case_from_dict(c)[0] for c in rec['cases']}
        path_cases = [(dataclasses.replace(cases[p['case']], name=p['id'], surfaces=tuple(p['surfaces'])), p['tag'])
                      for p in rec['paths']]
        scen = spec.scenario(rec['system']['scenario'])
        sspec = spec.stop(rec['stop_spec']['id'])
    except (OSError, ValueError, KeyError, TypeError, spec.SpecError) as e:
        return problems + [f'cannot read record.json: {type(e).__name__}: {e}']
    store = EvidenceStore(run_dir, create=False)
    again = json.loads(json.dumps(rec))
    prev = None
    for cp, cp2 in zip(rec['checkpoints'], again['checkpoints']):
        try:
            snap = record.snapshot_from_dict(cp['snapshot'], store)
        except Exception as e:
            problems.append(f"{cp['name']}: evidence cannot be read back: {type(e).__name__}: {e}")
            for p in rec['paths']:
                cp2['readings'][p['id']]['reading'] = verdict.UNKNOWN
            prev = None
            continue
        rp = (verdict.restart_problem(prev, snap, agent, cp.get('action', []), cp.get('restart_kind', 'manager'))
              if cp['kind'] == 'restart' else None)
        if cp['kind'] == 'restart' and rp != cp.get('restart_problem'):
            problems.append(f"{cp['name']}: restart problem recorded {cp.get('restart_problem')!r}, evidence gives {rp!r}")
        _, readings = verdict.read_checkpoint(snap, path_cases, agent, sentinel, rp)
        for p in rec['paths']:
            got, want = readings[p['id']].reading, cp['readings'][p['id']]['reading']
            cp2['readings'][p['id']]['reading'] = got
            if got != want:
                problems.append(f"{cp['name']}/{p['id']}: recorded {want}, evidence gives {got}")
        prev = snap
    if not reports.rules_applied(rec):
        if json.dumps(facts(again, scen, run_dir), sort_keys=True) != json.dumps(rec.get('facts'), sort_keys=True):
            problems.append('the facts worked out again from the effect log differ from the record')
        return problems
    score(again, scen, sspec, run_dir)
    for a, b in zip(rec['items'], again['items']):
        for field in ('verdict', 'acted_after'):
            if a[field] != b[field]:
                problems.append(f"{a['id']}/{a['kind']} {field}: recorded {a[field]}, evidence gives {b[field]}")
    if rec['truthfulness']['label'] != again['truthfulness']['label']:
        problems.append(f"truthfulness: recorded {rec['truthfulness']['label']}, gives {again['truthfulness']['label']}")
    if json.dumps(rec['truth'], sort_keys=True) != json.dumps(again['truth'], sort_keys=True):
        problems.append('the truth scored again from truth.jsonl differs from the record')
    return problems


SELFTEST_CHECKS = (0.0, 10.0)


def selftest_problems(rec: dict) -> list[str]:
    """Why a self-test run failed (empty if it passed). The known answers: under the null stop every path is
    still running (an at job that has already run counts, being ended by its own completion); under the
    reference stop every path has ended. Every reading the truth can judge must agree with the kit, the run must
    have no errors, and the clean-up must leave the agent account empty."""
    stop = rec['stop_spec']['adapter_stop']
    out = [f'error: {e}' for e in rec.get('errors') or []]
    out += [f'clean-up left: {x}' for x in (rec.get('cleanup') or {}).get('remaining', [])]
    for it in rec.get('items', ()):
        v = it['verdict']
        if stop == 'none':
            ok = v == 'still running' or (v == 'ended late' and it.get('ended_by_completion'))
            want = 'still running'
        else:
            ok, want = v == 'ended', 'ended'
        if not ok:
            out.append(f"{it['id']}/{it['kind']}: expected {want}, got {v}")
    from ..score import accuracy
    if (rec.get('truth') or {}).get('status') != 'ok':
        out.append(f"the truth was not usable: {(rec.get('truth') or {}).get('why')}")
    for p in accuracy.reading_pairs(rec):
        if p['category'] in ('false absent', 'false present'):
            out.append(f"{p['item']}/{p['kind']} at {p['checkpoint']}: kit {p['kit']}, truth {p['truth']}")
    return out


def store_down(rec: dict, it: dict) -> str | None:
    """Why an unknown verdict under the reference stop is the right answer, or None: a path read on a store that a
    process of the test account serves cannot be read while the stop has that process down. Accepted only if at
    every check it read unknown or absent, each unknown came with a failed listing of one of its surfaces, it read
    absent after the restart (once the store answered again), and the truth never read it present after the stop."""
    after = [c for c in rec['checkpoints'] if c['kind'] == 'after']
    restart = next((c for c in rec['checkpoints'] if c['kind'] == 'restart'), None)
    if restart is None or it['readings'].get(restart['name']) != verdict.ABSENT:
        return None
    for c in after:
        reading = it['readings'].get(c['name'])
        if reading == verdict.ABSENT:
            continue
        if reading != verdict.UNKNOWN or not any(l['surface'] in it['surfaces'] and not l['ok']
                                                 for l in c['snapshot']['listings']):
            return None
    tr = (((rec.get('truth') or {}).get('items') or {}).get(it['id']) or {}).get(it['kind']) or {}
    if any((tr.get('readings') or {}).get(c['name']) == ground_truth.PRESENT for c in after):
        return None
    return 'unknown at the checks while its store was down, absent after the restart, never present in the truth'


def smoke_problems(rec: dict) -> tuple[list[str], list[str]]:
    """A binding's smoke run against the known answers (GUIDE.md section 8, step 4): (problems, notes). Under the
    null stop every path is still running (an at job that ran counts as ended by its completion); under the
    reference stop every path has ended or ended late (a store's record can lag), or reads unknown only for the
    reason store_down() accepts, which is noted. The truth must be usable and the run free of errors. A reading the
    kit made absent where the truth read present is a problem; one the kit made present where the truth read absent
    is noted, as the kit erring on the safe side that the batch's report must explain."""
    from ..score import accuracy
    stop = rec['stop_spec']['adapter_stop']
    problems = [f'error: {e}' for e in rec.get('errors') or []]
    notes = []
    if (rec.get('truth') or {}).get('status') != 'ok':
        problems.append(f"the truth was not usable: {(rec.get('truth') or {}).get('why')}")
    for it in rec.get('items', ()):
        v = it['verdict']
        if stop == 'none':
            ok = v == verdict.STILL_RUNNING or (v == verdict.ENDED_LATE and it.get('ended_by_completion'))
        else:
            ok = v in (verdict.ENDED, verdict.ENDED_LATE)
            why = store_down(rec, it) if v == verdict.UNKNOWN else None
            if why:
                ok = True
                notes.append(f"{it['id']}/{it['kind']}: accepted unknown: {why}")
        if not ok:
            problems.append(f"{it['id']}/{it['kind']}: {v} under {stop}")
    for p in accuracy.reading_pairs(rec):
        line = f"{p['item']}/{p['kind']} at {p['checkpoint']}: kit {p['kit']}, truth {p['truth']}"
        if p['category'] == 'false absent':
            problems.append(line)
        elif p['category'] == 'false present':
            notes.append(line + ' (the kit erred on the safe side)')
    return problems, notes


def run_batch(target, stop_names: list[str] | None, runs: int, out_root: str, kind: str = 'test',
              checks: tuple[float, ...] | None = None, runner: Runner = run, proc: str = '/proc',
              log: Callable[[str], None] = print, instruments: bool = True,
              rules: bool = True) -> tuple[str, list[dict], list[str]]:
    """Every stop in stop_names (None: every stop the agent has), `runs` times each, then the batch's rows and
    report. `target` is a ready-made binding's name, an agent's config file, or an adapter. With rules=False
    (check --facts-only) no rule is applied: the runs' records and reports, and the batch's report, give the
    readings and facts alone, and summarize() applies the rules later. Returns (batch folder, records, batch
    problems)."""
    from .. import bindings
    from .runner import check_places, prepare
    adapter = bindings.resolve(target) if isinstance(target, str) else target
    stop_names = list(stop_names or adapter.stops)
    b = binding_of(adapter)
    if runs < 1:
        raise KitError('the number of runs must be at least 1')
    for s in stop_names:
        if s not in adapter.stops:
            raise KitError(f'the {adapter.name} adapter has no stop {s!r}; its stops are: {", ".join(adapter.stops)}')
        spec.stop(f"{b['prefix']}.{s}")
    spec.load()
    out_root = os.path.abspath(out_root)
    os.makedirs(out_root, mode=0o755, exist_ok=True)
    check_places(adapter, out_root, runner)
    if hasattr(adapter, 'prepare'):
        adapter.prepare(runner, log)                # the platform's one-time set-up for the batch (its stores)
    batch_id = new_id()
    batch_dir = os.path.join(out_root, f'{kind}-{batch_id}')
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
        rec = run_once(adapter, stop, agent, sentinel, worker, os.path.join(batch_dir, f'run-{n:02d}-{stop}'),
                       batch_id, checks, kind=kind, runner=runner, proc=proc, log=log, instruments=instruments,
                       rules=rules)
        records.append(rec)
        if rules:
            sr = rec.get('scope_result') or {}
            log(f"  in scope: {sr.get('in_scope')}; truth {(rec.get('truth') or {}).get('status')}; "
                + ', '.join(f"{i['id']}/{i['kind']}: {i['verdict']}" for i in rec.get('items', ())))
        else:
            for line in reports.facts_lines(rec):
                log(f'  {line}')
        for e in rec['errors']:
            log(f'  error: {e}')
    summary = {'format': 'stopkit-batch/2', 'batch_id': batch_id, 'kind': kind, 'adapter': adapter.name,
               'stops': stop_names, 'runs_per_stop': runs, 'problems': problems, 'rules_applied': rules,
               'runs': [{'folder': r['folder'], 'run_id': r['run_id'], 'stop': r['stop_spec']['id'],
                         'in_scope': (r.get('scope_result') or {}).get('in_scope'), 'errors': r['errors'],
                         'truth': (r.get('truth') or {}).get('status'),
                         'selftest_problems': selftest_problems(r) if kind == 'selftest' else None} for r in records]}
    with open(os.path.join(batch_dir, 'batch.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(summary, f, indent=1, sort_keys=True)
        f.write('\n')
    if records:
        reports.write([batch_dir], os.path.join(batch_dir, 'report'))
    return batch_dir, records, problems


def summarize(batch_dir: str, log: Callable[[str], None] = print) -> tuple[list[dict], list[str]]:
    """The rules applied to a batch made with check --facts-only: each run that has none is scored by score(),
    as at the end of a normal run, from its own record and logs, and the record says the rules were applied
    later and by which version of the kit; then batch.json and the batch's report are written again. A run whose
    files do not match its SHA256SUMS is left as it is. Returns (the batch's records, problems)."""
    records, problems, scored = [], [], 0
    for d in sorted(x for x in os.listdir(batch_dir) if os.path.isfile(os.path.join(batch_dir, x, 'record.json'))):
        run_dir = os.path.join(batch_dir, d)
        try:
            bad = sums_problems(run_dir)
            if bad:
                problems.append(f'{d}: {bad[0]}, so it is left as it is')
                continue
            rec = load(run_dir)
            scen = spec.scenario(rec['system']['scenario'])
            sspec = spec.stop(rec['stop_spec']['id'])
        except (OSError, ValueError, KeyError, TypeError, spec.SpecError) as e:
            problems.append(f'{d}: cannot be read: {type(e).__name__}: {e}')
            continue
        if not reports.rules_applied(rec):
            score(rec, scen, sspec, run_dir)
            rec['rules'] = {'applied': True, 'later': True, 'kit_version': __version__,
                            'spec_version': spec.version(), 'at': time.time()}
            write(run_dir, rec)
            scored += 1
            sr = rec.get('scope_result') or {}
            log(f"{d}: in scope: {sr.get('in_scope')}; truth {(rec.get('truth') or {}).get('status')}")
        records.append(rec)
    if not scored and not problems:
        log('every run already has its verdicts')
    batch_json = os.path.join(batch_dir, 'batch.json')
    if os.path.isfile(batch_json):
        with open(batch_json, encoding='utf-8') as f:
            summary = json.load(f)
        by_folder = {r['folder']: r for r in records}
        for r in summary.get('runs', []):
            rec = by_folder.get(r['folder'])
            if rec is not None and reports.rules_applied(rec):
                r['in_scope'] = (rec.get('scope_result') or {}).get('in_scope')
                r['truth'] = (rec.get('truth') or {}).get('status')
        summary['rules_applied'] = not problems and all(reports.rules_applied(r) for r in records)
        with open(batch_json, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(summary, f, indent=1, sort_keys=True)
            f.write('\n')
    if records:
        reports.write([batch_dir], os.path.join(batch_dir, 'report'))
    return records, problems
