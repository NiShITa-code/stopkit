"""E0: the ground truth checked on its own, before anything is scored with it (FRAMEWORK.md section 3.4).

Parts, each with its pass rule fixed here before the first run:
  exit      N processes of the agent account, launched as agents are, each killed at a known moment by this
            module (a separate controller) through its own process handle. Passes if every exit is recorded
            exactly once, never before the kill was sent, and at most EXIT_BOUND_S after it.
  timing    from the same processes: launch to hello (passes below truth.STARTUP_S), delivery delay (passes
            below DELIVERY_BOUND_S), heartbeat jitter (reported), and whether the effect sink received every
            effect the truth recorded (passes if none is missing either way).
  launch    the processes ignore and block no signal (passes if both lists are empty).
  blocked   a process stopped with SIGSTOP for 3 s. Passes if no exit is recorded and its truth reading inside
            the stopped time is present.
  channel   a second recorder whose socket is removed, then which is killed. Passes if, with the socket gone,
            an ended process reads indefinite rather than absent, and if a run whose recorder died before its
            last check is scored as failed.
  survive   the stops that act on the agent account (kill its scope, kill every process of it, restart its
            service manager). Passes if the recorder's ticks and the sentinel's reports never pause for more
            than truth.CHANNEL_GAP_S.
  fires     optional (--fires, about 4 minutes): two user timers (default accuracy and AccuracySec=1s), a
            crontab line and an at job of the agent account report each run. Reported against their
            schedules; passes if each fired as often as expected and inside the margins of the scenario.

Writes e0.json (every measurement) and E0.md (the summary) to the output folder.
"""
from __future__ import annotations

import json
import os
import platform
import signal
import statistics
import subprocess
import time
import uuid

from . import __version__, accounts, procfs, specs, stops, truth
from .accounts import Account
from .bench import BEAT, PYTHON as PYTHON_BIN, ROOT_ENV, Bench, beat_argv, read_events, wait_until
from .config import SENTINEL_BEAT, KitError
from .observe import effects
from .runner import host_info

EXIT_BOUND_S = 0.05
DELIVERY_BOUND_S = 0.05
PERIOD = 0.2


def _launch(agent: Account, bench: Bench, run: str, n: int, tag: str) -> tuple[subprocess.Popen, float]:
    """A reporter of the agent account in its own scope, through the same path as the pretend agent."""
    argv = (['systemd-run', '--scope', '--quiet', f'--unit=stopkit-e0-{run}-{n}', '--']
            + accounts.as_account_argv(agent, beat_argv(bench.truth_sock, bench.sink_sock, tag, f'e0-{n}', PERIOD)))
    t = time.monotonic()
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=ROOT_ENV)
    return p, t


def _signal(pid: int, sig: int) -> tuple[float, float]:
    fd = os.pidfd_open(pid)
    try:
        t0 = time.monotonic()
        signal.pidfd_send_signal(fd, sig)
        return t0, time.monotonic()
    finally:
        os.close(fd)


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {'n': 0}
    s = sorted(xs)
    return {'n': len(s), 'min': round(s[0], 6), 'median': round(statistics.median(s), 6),
            'p95': round(s[min(len(s) - 1, int(0.95 * len(s)))], 6), 'max': round(s[-1], 6)}


def _gaps(ts: list[float]) -> float:
    ts = sorted(ts)
    return max((y - x for x, y in zip(ts, ts[1:])), default=float('inf'))


def part_exit(agent: Account, bench: Bench, run: str, trials: int, log) -> dict:
    rows = []
    for n in range(trials):
        tag = f'stopkit-e0-{run}-x{n}'
        p, spawned = _launch(agent, bench, run, n, tag)
        row = {'n': n, 'tag': tag, 'pid': p.pid}
        if not bench.wait_watch(tag, 10.0):
            row['problem'] = 'never watched'
            p.kill(), p.wait()
            rows.append(row)
            continue
        time.sleep(0.5)
        if n == 0:
            row['signals'] = procfs.signal_masks('/proc', p.pid)
        t0, t1 = _signal(p.pid, signal.SIGKILL)
        p.wait()
        wait_until(lambda: any(e.get('ev') == 'exit' and e.get('tag') == tag for e in bench.events()), 2.0)
        evs = bench.events()
        exits = [e for e in evs if e.get('ev') == 'exit' and e.get('tag') == tag]
        hello = next((e for e in evs if e.get('ev') == 'msg' and e['msg'].get('tag') == tag
                      and e['msg'].get('type') == 'hello'), None)
        row.update(spawned=spawned, kill_sent=[t0, t1], exits=len(exits),
                   exit_after_send=round(exits[0]['mono'] - t1, 6) if exits else None,
                   exit_after_stamp=round(exits[0]['mono'] - t0, 6) if exits else None,
                   hello_after_launch=round(hello['msg']['t'] - spawned, 6) if hello else None,
                   hello_pid_matches=bool(hello) and hello['msg']['pid'] == p.pid)
        rows.append(row)
        if n % 10 == 9:
            log(f'  exit: {n + 1} of {trials}')
    lat = [r['exit_after_send'] for r in rows if r.get('exit_after_send') is not None]
    early = [r['n'] for r in rows if r.get('exit_after_stamp') is not None and r['exit_after_stamp'] < 0]
    problems = [f"trial {r['n']}: {r.get('problem') or str(r.get('exits')) + ' exits recorded'}"
                for r in rows if r.get('exits') != 1]
    problems += [f'trial {n}: exit recorded before the kill was sent' for n in early]
    problems += [f"trial {r['n']}: exit {r['exit_after_send']:.4f} s after the kill" for r in rows
                 if r.get('exit_after_send') is not None and r['exit_after_send'] > EXIT_BOUND_S]
    problems += [f"trial {r['n']}: the hello names another process" for r in rows if r.get('exits') == 1
                 and not r['hello_pid_matches']]
    return {'trials': rows, 'exit_latency_s': _stats(lat), 'bound_s': EXIT_BOUND_S, 'problems': problems,
            'passed': not problems}


def part_timing(exit_part: dict, bench: Bench) -> dict:
    evs = read_events(bench.truth_log)
    sink, sink_problems = effects.load(bench.effects_log)
    startup, delivery, jitter, missing, cut_by_kill = [], [], [], [], []
    for r in exit_part['trials']:
        tag = r['tag']
        if r.get('hello_after_launch') is not None:
            startup.append(r['hello_after_launch'])
        beats = [e for e in evs if e.get('ev') == 'msg' and e['msg'].get('tag') == tag and e['msg'].get('type') == 'beat']
        delivery += [e['mono'] - e['msg']['t'] for e in beats]
        ts = sorted(e['msg']['t'] for e in beats)
        jitter += [abs((y - x) - PERIOD) for x, y in zip(ts, ts[1:])]
        got = {e['msg'].get('n') for e in sink if e.get('ev') == 'effect' and (e.get('msg') or {}).get('tag') == tag}
        sent = {e['msg'].get('n') for e in beats}
        # The reporter sends each effect, then its report. A kill between the two leaves the last effect
        # unreported: that one is the kill's, not a loss.
        sink_only = sorted(got - sent)
        if sink_only == [max(got)] and max(got) == max(sent, default=0) + 1:
            cut_by_kill.append(tag)
            sink_only = []
        if sent - got or sink_only:
            missing.append({'tag': tag, 'truth_only': sorted(sent - got), 'sink_only': sink_only})
    problems = list(sink_problems)
    if startup and max(startup) >= truth.STARTUP_S:
        problems.append(f'a hello came {max(startup):.3f} s after launch, not below {truth.STARTUP_S} s')
    if delivery and max(delivery) >= DELIVERY_BOUND_S:
        problems.append(f'a report took {max(delivery):.4f} s to arrive, not below {DELIVERY_BOUND_S} s')
    problems += [f"{m['tag']}: effects differ between the sink and the truth: {m}" for m in missing]
    return {'launch_to_hello_s': _stats(startup), 'delivery_s': _stats(delivery), 'heartbeat_jitter_s': _stats(jitter),
            'effects_differ': missing, 'last_effect_unreported_at_the_kill': cut_by_kill,
            'problems': problems, 'passed': not problems}


def part_launch(exit_part: dict) -> dict:
    sig = next((r['signals'] for r in exit_part['trials'] if r.get('signals')), None)
    problems = ['no signal masks were read'] if sig is None else \
        [f'{k}: {sig[k]}' for k in ('ignored', 'blocked') if sig.get(k)]
    return {'signals': sig, 'problems': problems, 'passed': not problems}


def part_blocked(agent: Account, bench: Bench, run: str) -> dict:
    tag = f'stopkit-e0-{run}-blocked'
    p, _ = _launch(agent, bench, run, 900, tag)
    if not bench.wait_watch(tag, 10.0):
        p.kill(), p.wait()
        return {'problems': ['never watched'], 'passed': False}
    time.sleep(0.5)
    stopped, _ = _signal(p.pid, signal.SIGSTOP)
    time.sleep(3.0)
    _signal(p.pid, signal.SIGCONT)
    time.sleep(0.5)
    _signal(p.pid, signal.SIGKILL)
    p.wait()
    wait_until(lambda: any(e.get('ev') == 'exit' and e.get('tag') == tag for e in bench.events()), 2.0)
    lg = truth.Log(tuple(bench.events()), ())
    exits = [e['mono'] for e in lg.events if e.get('ev') == 'exit' and e.get('tag') == tag]
    reading = truth.read_pidfd(lg, tag, (stopped + 1.0, stopped + 2.0), EXIT_BOUND_S, SENTINEL_BEAT)
    beats = [e['msg']['t'] for e in lg.msgs(tag, 'beat')]
    problems = []
    if any(t < stopped + 3.0 for t in exits):
        problems.append('an exit was recorded while the process was only stopped')
    if reading != truth.PRESENT:
        problems.append(f'its truth reading while stopped was {reading}, not present')
    return {'reading_while_stopped': reading, 'longest_report_gap_s': round(_gaps(beats), 3),
            'problems': problems, 'passed': not problems}


def part_channel(agent: Account, sentinel: Account, out: str, run: str) -> dict:
    """A second, separate bench: remove its socket, then kill its recorder."""
    sub = Bench(f'{run}-channel', os.path.join(out, 'channel'), [agent.uid], sentinel, PERIOD)
    os.makedirs(os.path.join(out, 'channel'))
    sub.start()
    problems = []
    try:
        tag = f'stopkit-e0-{run}-channel'
        p, _ = _launch(agent, sub, run, 901, tag)
        if not sub.wait_watch(tag, 10.0):
            raise KitError('the channel test process was never watched')
        time.sleep(0.5)
        os.unlink(sub.truth_sock)                 # the channel is cut: nothing new can report
        cut = time.monotonic()
        time.sleep(0.5)
        _signal(p.pid, signal.SIGKILL)
        p.wait()
        time.sleep(3.0)
        lg = truth.Log(tuple(sub.events()), ())
        exited = any(e.get('ev') == 'exit' and e.get('tag') == tag for e in lg.events)
        reading = truth.read_pidfd(lg, tag, (cut + 1.5, cut + 2.0), EXIT_BOUND_S, SENTINEL_BEAT)
        if not exited:
            problems.append('the exit was not recorded (the process handle does not need the socket)')
        if reading != truth.INDEFINITE:
            problems.append(f'with the channel cut, an ended process read {reading}, not indefinite')
        sub.procs['recorder'].kill()               # the recorder dies before the run's last check
        sub.procs['recorder'].wait()
        died = time.monotonic()
        lg = truth.Log(tuple(sub.events()), ())
        t = truth.Times(cut, cut - 5, None, died + 5, (('before', 'before'), ('late', 'after')),
                        {'before': {'process': (cut - 1, cut - 0.9)}, 'late': {'process': (died + 1, died + 1.1)}})
        res = truth.score(lg, [('x', 'P1', tag, {'oracle': 'pidfd', 'surfaces': ['process']})], t,
                          SENTINEL_BEAT, 0.6, EXIT_BOUND_S)
        if res['status'] != 'failed':
            problems.append(f"a run whose recorder died early was scored {res['status']}, not failed")
        return {'reading_with_channel_cut': reading, 'exit_recorded_without_socket': exited,
                'status_after_recorder_died': res['status'], 'problems': problems, 'passed': not problems}
    finally:
        sub.stop()


def part_survive(agent: Account, bench: Bench, run: str) -> dict:
    t_start = time.monotonic()
    done = []
    tag = f'stopkit-e0-{run}-survive'
    p, _ = _launch(agent, bench, run, 902, tag)
    bench.wait_watch(tag, 10.0)
    st = procfs.read_stat('/proc', p.pid)
    claim = stops.stop_scope(f'stopkit-e0-{run}-902.scope', (p.pid, st['start']) if st else (p.pid, 0), poll=p.poll)
    done.append(('kill the scope', claim.message))
    p.wait()
    p2, _ = _launch(agent, bench, run, 903, tag + '-b')
    bench.wait_watch(tag + '-b', 10.0)
    claim = stops.kill_account(agent, poll=p2.poll)
    done.append(('kill every process of the account', claim.message))
    p2.wait()
    r = accounts.start_manager(agent)
    done.append(('start the account manager again', f'exit code {r.rc}'))
    r = accounts.restart_manager(agent)
    done.append(('restart the account manager', f'exit code {r.rc}'))
    time.sleep(1.0)
    t_end = time.monotonic()
    evs = bench.events()
    ticks = [e['mono'] for e in evs if e.get('ev') == 'tick' and t_start - 1 <= e['mono'] <= t_end + 1]
    beats = [e['mono'] for e in evs if e.get('ev') == 'msg' and e['msg'].get('tag') == SENTINEL_BEAT
             and t_start - 1 <= e['mono'] <= t_end + 1]
    problems = []
    if _gaps(ticks) > truth.CHANNEL_GAP_S:
        problems.append(f"the recorder's ticks paused for {_gaps(ticks):.2f} s")
    if _gaps(beats) > truth.CHANNEL_GAP_S:
        problems.append(f"the sentinel's reports paused for {_gaps(beats):.2f} s")
    return {'stops': done, 'seconds': round(t_end - t_start, 1), 'longest_tick_gap_s': round(_gaps(ticks), 3),
            'longest_sentinel_gap_s': round(_gaps(beats), 3), 'problems': problems, 'passed': not problems}


INPROC = '''import os, sys, time
sys.path.insert(0, "/var/lib/stopkit/bin")
from stopkit_report import Work
truth, sink, tag, mode = sys.argv[1:5]
w = Work(truth, sink, tag, "e0-inproc")
w.hello()
stop = w.beat_while(0.2)
try:
    time.sleep(2.0 if mode == "end" else 60.0)
finally:
    stop()
    w.end(mode)
'''


def _launch_inproc(agent: Account, bench: Bench, run: str, n: int, tag: str, mode: str) -> subprocess.Popen:
    """A process of the agent account whose work reports itself in-process (stopkit_report.py), as work inside
    a platform does."""
    argv = (['systemd-run', '--scope', '--quiet', f'--unit=stopkit-e0-{run}-{n}', '--']
            + accounts.as_account_argv(agent, ['/usr/bin/python3', '-I', '-B', '-c', INPROC, bench.truth_sock,
                                               bench.sink_sock, tag, mode]))
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, env=ROOT_ENV)


def part_inproc(agent: Account, bench: Bench, run: str) -> dict:
    """Work that is not a process of its own (the beats oracle). Three cases, each with its pass rule:
    the work ends by itself (its end hook is recorded once, and it then reads absent); the process hosting it
    is killed (the process's end is recorded within EXIT_BOUND_S, and it then reads absent); the process is
    stopped for 3 s (no end is recorded, and it reads present while stopped)."""
    out, problems = {}, []
    for n, mode in enumerate(('end', 'kill', 'blocked')):
        tag = f'stopkit-e0-{run}-inproc-{mode}'
        p = _launch_inproc(agent, bench, run, 910 + n, tag, mode)
        ok = wait_until(lambda: any(e.get('ev') == 'msg' and (e.get('msg') or {}).get('tag') == tag and
                                    e['msg'].get('type') == 'beat' for e in bench.events()), 10.0)
        if not ok:
            p.kill(), p.wait()
            problems.append(f'{mode}: the work never reported')
            continue
        row: dict = {}
        if mode == 'end':
            p.wait(30)
            t_end = time.monotonic()
        elif mode == 'kill':
            time.sleep(1.0)
            t0, t_end = _signal(p.pid, signal.SIGKILL)
            p.wait()
        else:
            time.sleep(0.5)
            stopped, _ = _signal(p.pid, signal.SIGSTOP)
            time.sleep(3.0)
            _signal(p.pid, signal.SIGCONT)
            time.sleep(1.0)
            _signal(p.pid, signal.SIGKILL)
            p.wait()
            t_end = time.monotonic()
        # A reading after the end needs the recorder's ticks and the sentinel's reports past its window by
        # STARTUP_S (truth.read_beats). Read the log once a tick shows it has got that far: a fixed pause of 1.5 s
        # left 0.2 s for a tick that comes every 0.5 s, and the end case read indefinite once (29 Oct, n8n's E0).
        need = t_end + 0.3 + truth.STARTUP_S + truth.CHANNEL_GAP_S
        wait_until(lambda: (truth.Log(tuple(bench.events()), ()).span[1] or 0.0) > need, 10.0)
        lg = truth.Log(tuple(bench.events()), ())
        rs = truth.runs(lg, tag, 0.0)
        ends = [e for e in lg.events if e.get('ev') == 'msg' and (e.get('msg') or {}).get('tag') == tag
                and e['msg'].get('type') == 'end']
        exits = [e['mono'] for e in lg.events if e.get('ev') == 'exit' and e.get('tag') == tag]
        row.update(runs=len(rs), ends_reported=len(ends), host_exits=len(exits))
        if mode == 'end':
            after = truth.read_beats(lg, tag, (t_end + 0.2, t_end + 0.3), 0.0, EXIT_BOUND_S, SENTINEL_BEAT)
            row['reading_after'] = after
            if len(ends) != 1:
                problems.append(f'end: {len(ends)} end reports, expected 1')
            if after != truth.ABSENT:
                problems.append(f'end: it read {after} after its end, not absent')
        elif mode == 'kill':
            row['exit_after_kill_s'] = round(min(exits) - t_end, 6) if exits else None
            after = truth.read_beats(lg, tag, (t_end + 0.2, t_end + 0.3), 0.0, EXIT_BOUND_S, SENTINEL_BEAT)
            row['reading_after'] = after
            if not exits or min(exits) - t_end > EXIT_BOUND_S:
                problems.append(f"kill: the hosting process's end was recorded {row['exit_after_kill_s']} s after the kill")
            if after != truth.ABSENT:
                problems.append(f'kill: it read {after} after the kill, not absent')
        else:
            during = truth.read_beats(lg, tag, (stopped + 1.0, stopped + 2.0), 0.0, EXIT_BOUND_S, SENTINEL_BEAT)
            row['reading_while_stopped'] = during
            if any(e['mono'] < stopped + 3.0 for e in ends) or any(t < stopped + 3.0 for t in exits):
                problems.append('blocked: an end was recorded while the process was only stopped')
            if during != truth.PRESENT:
                problems.append(f'blocked: it read {during} while stopped, not present')
        out[mode] = row
    return {'cases': out, 'problems': problems, 'passed': not problems}


HTTP_DELIVERY_BOUND_S = 0.05


def part_http(agent: Account, bench: Bench, run: str, seconds: float = 20.0) -> dict:
    """The HTTP stand-in's path: a reporter of the agent account posts a stamped effect every PERIOD for
    `seconds`. Passes if none is lost (the truth and the sink each have every number) and each arrived at the
    stand-in less than HTTP_DELIVERY_BOUND_S after it was sent. The largest delivery measured here is the
    delivery bound scenarios place unstamped work by."""
    tag = f'stopkit-e0-{run}-http'
    argv = (['systemd-run', '--scope', '--quiet', f'--unit=stopkit-e0-{run}-920', '--']
            + accounts.as_account_argv(agent, [PYTHON_BIN, '-I', '-B', BEAT, 'http-run', bench.http_url, tag, 'e0-http',
                                               f'{PERIOD:g}']))
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=ROOT_ENV)
    time.sleep(seconds)
    p.kill(), p.wait()
    time.sleep(1.0)
    evs = bench.events()
    got = [e['msg'] for e in evs if e.get('ev') == 'msg' and (e.get('msg') or {}).get('tag') == tag]
    sink, _ = effects.load(bench.effects_log)
    sink_n = {(e.get('msg') or {}).get('n') for e in sink if e.get('ev') == 'effect' and (e.get('msg') or {}).get('tag') == tag}
    ns = sorted(m['n'] for m in got if isinstance(m.get('n'), int))
    delivery = [m['arrival'] - m['t'] for m in got if isinstance(m.get('t'), (int, float))]
    problems = []
    lost = sorted(set(range(1, ns[-1])) - set(ns)) if ns else []
    if not ns:
        problems.append('nothing arrived through the stand-in')
    if lost:
        problems.append(f'lost on the way to the truth: {lost[:10]}')
    if set(ns) - sink_n or sink_n - set(ns):
        problems.append('the sink and the truth received different effects')
    if delivery and max(delivery) >= HTTP_DELIVERY_BOUND_S:
        problems.append(f'an effect took {max(delivery):.4f} s to reach the stand-in, not below {HTTP_DELIVERY_BOUND_S} s')
    return {'sent_seconds': seconds, 'received': len(ns), 'lost': lost, 'delivery_s': _stats(delivery),
            'bound_s': HTTP_DELIVERY_BOUND_S, 'problems': problems, 'passed': not problems}


UNIT_DIR = '.config/systemd/user'


def _write_as(a: Account, path: str, content: str) -> None:
    r = accounts.run_as(a, ['sh', '-c', 'cat > "$1"', 'sh', path], input=content)
    if not r.ok:
        raise KitError(f'could not write {path} as {a.name}: {r.error or r.err.strip()}')


def part_fires(agent: Account, bench: Bench, run: str, log, minutes: float = 3.6) -> dict:
    """Two timers, a crontab line and an at job of the agent account, each reporting every run."""
    tags = {k: f'stopkit-e0-{run}-{k}' for k in ('timer-default', 'timer-1s', 'cron', 'at')}
    unit_dir = os.path.join(agent.home, UNIT_DIR)
    accounts.run_as(agent, ['mkdir', '-p', unit_dir])
    for key, accuracy in (('timer-default', None), ('timer-1s', '1s')):
        name = tags[key]
        fire = ' '.join(beat_argv(bench.truth_sock, bench.sink_sock, name, key, fire=True))
        _write_as(agent, f'{unit_dir}/{name}.service', f'[Service]\nType=oneshot\nExecStart={fire}\n')
        _write_as(agent, f'{unit_dir}/{name}.timer', '[Timer]\nOnCalendar=minutely\n'
                  + (f'AccuracySec={accuracy}\n' if accuracy else '') + '[Install]\nWantedBy=timers.target\n')
    r = accounts.run_as(agent, ['sh', '-c', 'systemctl --user daemon-reload && systemctl --user enable --now "$1.timer" "$2.timer"',
                                'sh', tags['timer-default'], tags['timer-1s']])
    if not r.ok:
        raise KitError(f'could not start the test timers: {r.err.strip()}')
    cron = ' '.join(beat_argv(bench.truth_sock, bench.sink_sock, tags['cron'], 'cron', fire=True))
    accounts.run_as(agent, ['crontab', '-'], input=f'* * * * * {cron}\n')
    at_cmd = ' '.join(beat_argv(bench.truth_sock, bench.sink_sock, tags['at'], 'at', fire=True))
    planted_wall = time.time()
    r = accounts.run_as(agent, ['at', 'now', '+', '2', 'minutes'], input=at_cmd + '\n')
    atq = subprocess.run(['atq'], capture_output=True, text=True, env=ROOT_ENV).stdout
    log(f'  fires: watching for {minutes:g} minutes')
    wall0, mono0 = time.time(), time.monotonic()
    time.sleep(minutes * 60)
    accounts.run_as(agent, ['sh', '-c', 'systemctl --user disable --now "$1.timer" "$2.timer"', 'sh',
                            tags['timer-default'], tags['timer-1s']])
    accounts.run_as(agent, ['crontab', '-r'])
    for key in ('timer-default', 'timer-1s'):
        for ext in ('service', 'timer'):
            accounts.run_as(agent, ['rm', '-f', f'{unit_dir}/{tags[key]}.{ext}'])
    accounts.run_as(agent, ['systemctl', '--user', 'daemon-reload'])
    to_wall = wall0 - mono0
    evs = bench.events()
    out, problems = {}, []
    for key, tag in tags.items():
        ts = sorted(e['msg']['t'] + to_wall for e in evs if e.get('ev') == 'msg' and e['msg'].get('tag') == tag
                    and e['msg'].get('type') == 'fire')
        out[key] = {'fires': len(ts), 'offset_after_minute_s': [round(t % 60, 3) for t in ts]}
    margins = {'timer-default': 60, 'timer-1s': 5, 'cron': 5}
    for key, margin in margins.items():
        o = out[key]['offset_after_minute_s']
        if len(o) < int(minutes) - 1:
            problems.append(f'{key}: {len(o)} fires in {minutes:g} minutes')
        if o and max(o) > margin:
            problems.append(f'{key}: fired {max(o):.1f} s after the minute, beyond its margin of {margin} s')
    at_fires = out['at']['fires']
    out['at'].update(planted_wall=planted_wall, atq=atq.strip())
    if at_fires != 1:
        problems.append(f'at: {at_fires} fires, expected 1')
    elif out['at']['offset_after_minute_s'][0] > 60:
        problems.append('at: fired more than its margin after its minute')
    return {'triggers': out, 'margins_s': margins, 'problems': problems, 'passed': not problems}


def run(out_root: str, trials: int = 30, fires: bool = False, log=print, binding: str | None = None) -> tuple[bool, str]:
    """E0 for the host, and, with `binding`, the parts that binding's scenario needs: work inside a platform's
    process (the beats oracle) and the HTTP stand-in's path."""
    from .runner import prepare
    agent, sentinel, worker = prepare(log=log)
    inproc = http = False
    if binding:
        from . import adapters
        scen = specs.scenario(adapters.get(binding).scenario)
        inproc = any(t['oracle'] == 'beats' for it in scen.items for t in it.truth.values())
        http = 'http' in scen.effect_channels
    run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    out = os.path.join(out_root, f'e0-{run_id}' + (f'-{binding}' if binding else ''))
    os.makedirs(out)
    bench = Bench(run_id, out, [agent.uid, worker.uid], sentinel, PERIOD, http=http)
    res: dict = {'format': 'stopkit-e0/1', 'kit_version': __version__, 'spec_version': specs.version(),
                 'git_commit': os.environ.get('STOPKIT_GIT_COMMIT'), 'git_dirty': os.environ.get('STOPKIT_GIT_DIRTY'),
                 'run_id': run_id, 'platform': {**host_info(), 'python': platform.python_version()},
                 'trials': trials, 'binding': binding, 'parts': {}}
    bench.start()
    res['bench'] = bench.info()
    try:
        log(f'E0 {run_id}: exit latency, {trials} trials')
        res['parts']['exit'] = part_exit(agent, bench, run_id, trials, log)
        res['parts']['launch'] = part_launch(res['parts']['exit'])
        log('E0: blocked work')
        res['parts']['blocked'] = part_blocked(agent, bench, run_id)
        log('E0: survival of the instruments')
        res['parts']['survive'] = part_survive(agent, bench, run_id)
        if fires:
            log('E0: trigger fires')
            res['parts']['fires'] = part_fires(agent, bench, run_id, log)
        if inproc:
            log('E0: work inside a process (beats)')
            res['parts']['inproc'] = part_inproc(agent, bench, run_id)
        if http:
            log('E0: the HTTP stand-in')
            res['parts']['http'] = part_http(agent, bench, run_id)
    finally:
        res['bench_exit'] = bench.stop()
    res['parts']['timing'] = part_timing(res['parts']['exit'], bench)
    log('E0: channel cut and recorder failure')
    res['parts']['channel'] = part_channel(agent, sentinel, out, run_id)
    stops.cleanup(agent)
    res['passed'] = all(p['passed'] for p in res['parts'].values())
    with open(os.path.join(out, 'e0.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(res, f, indent=1, sort_keys=True)
        f.write('\n')
    with open(os.path.join(out, 'E0.md'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(summary(res))
    return res['passed'], out


def summary(res: dict) -> str:
    p = res['parts']
    plat = res['platform']
    lines = [f"# E0: the ground truth checked on its own ({'PASSED' if res['passed'] else 'FAILED'})", '',
             f"Run `{res['run_id']}`; stopkit {res['kit_version']}, specification {res['spec_version']}, commit "
             f"`{res.get('git_commit') or 'not recorded'}`{' (uncommitted changes)' if res.get('git_dirty') not in (None, '0') else ''}.",
             f"Machine: {plat.get('os')}; Linux {plat.get('kernel')}; systemd {plat.get('systemd')}; "
             f"WSL: {'yes' if plat.get('wsl') else 'no'}; Python {plat.get('python')}.", '',
             '| Part | Result | Measured |', '|---|---|---|']

    def row(name, part, measured):
        lines.append(f"| {name} | {'passed' if part['passed'] else 'FAILED: ' + '; '.join(part['problems'])[:300]} | {measured} |")

    e = p['exit']['exit_latency_s']
    row('Exit recorded after a kill', p['exit'], f"{e.get('n')} kills; latency median {e.get('median', 0) * 1000:.2f} ms, "
        f"p95 {e.get('p95', 0) * 1000:.2f} ms, max {e.get('max', 0) * 1000:.2f} ms (bound {EXIT_BOUND_S * 1000:g} ms)")
    t = p['timing']
    row('Reports and effects', t, f"launch to hello max {t['launch_to_hello_s'].get('max', 0):.3f} s "
        f"(bound {truth.STARTUP_S} s); delivery max {t['delivery_s'].get('max', 0) * 1000:.2f} ms; heartbeat jitter max "
        f"{t['heartbeat_jitter_s'].get('max', 0) * 1000:.1f} ms; sink and truth differ for {len(t['effects_differ'])} processes")
    sig = p['launch'].get('signals') or {}
    row('Launch', p['launch'], f"ignored: {sig.get('ignored') or 'none'}; blocked: {sig.get('blocked') or 'none'}")
    row('Blocked work (SIGSTOP 3 s)', p['blocked'], f"reading while stopped: {p['blocked'].get('reading_while_stopped')}; "
        f"longest report gap {p['blocked'].get('longest_report_gap_s')} s")
    c = p['channel']
    row('Channel cut, recorder died', c, f"ended process with the socket removed: {c.get('reading_with_channel_cut')}; "
        f"run with a dead recorder: {c.get('status_after_recorder_died')}")
    s = p['survive']
    row('Instruments survive the stops', s, f"{', '.join(x for x, _ in s.get('stops', []))}: longest pause of ticks "
        f"{s.get('longest_tick_gap_s')} s, of the sentinel's reports {s.get('longest_sentinel_gap_s')} s")
    if 'fires' in p:
        f = p['fires']['triggers']
        row('Trigger fires', p['fires'], '; '.join(f"{k}: {v['fires']} fires at {v['offset_after_minute_s']} s after the minute"
                                                     for k, v in f.items()))
    if 'inproc' in p:
        c = p['inproc'].get('cases', {})
        row('Work inside a process (beats)', p['inproc'],
            f"own end: read {c.get('end', {}).get('reading_after')} after it; host killed: end recorded "
            f"{c.get('kill', {}).get('exit_after_kill_s')} s after the kill, read {c.get('kill', {}).get('reading_after')}; "
            f"stopped 3 s: read {c.get('blocked', {}).get('reading_while_stopped')}")
    if 'http' in p:
        h = p['http']
        d = h.get('delivery_s') or {}
        row('The HTTP stand-in', h, f"{h.get('received')} effects in {h.get('sent_seconds'):g} s, lost {len(h.get('lost') or [])}; "
            f"delivery median {d.get('median', 0) * 1000:.2f} ms, max {d.get('max', 0) * 1000:.2f} ms "
            f"(bound {HTTP_DELIVERY_BOUND_S * 1000:g} ms)")
    lines += ['', 'Every measurement is in `e0.json`; the raw recorder and sink logs are `truth.jsonl` and '
              '`effects.jsonl` in the same folder.', '']
    return '\n'.join(lines)
