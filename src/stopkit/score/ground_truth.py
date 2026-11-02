"""Scoring the ground truth (spec/verdicts.json, truth): the truth reading of every path at the moments the
kit's own checkpoints read it, then the truth verdict from those readings by the same verdict rules.

It reads only the recorder's log (truth/recorder.py) and the run's times: the cut-off, each checkpoint's
listing windows, the restart action. It never reads a listing, a reading or a verdict of the kit, and the
kit's verdicts never read it.

A process's life is known from its own hello (its sender time, at most STARTUP_S after the process began)
and the moment its process handle became readable (at most the measured exit latency after it ended).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

PRESENT, ABSENT, INDEFINITE = 'present', 'absent', 'indefinite'
STARTUP_S = 1.0              # longest time from a planted process's start to its hello (E0 checks it)
CHANNEL_GAP_S = 1.0          # the sentinel's reports must never be further apart than this


@dataclass(frozen=True)
class Log:
    events: tuple[dict, ...]
    problems: tuple[str, ...]

    def msgs(self, tag: str, kind: str) -> list[dict]:
        return [e for e in self.events if e.get('ev') == 'msg' and (e.get('msg') or {}).get('tag') == tag
                and e['msg'].get('type') == kind]

    @property
    def span(self) -> tuple[float | None, float | None]:
        """When the recorder started, and its last sign of life (a tick or its end)."""
        start = next((e['mono'] for e in self.events if e.get('ev') == 'start'), None)
        alive = [e['mono'] for e in self.events if e.get('ev') in ('tick', 'end')]
        return start, (max(alive) if alive else None)


def load(path: str) -> Log:
    events, problems = [], []
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            if n >= len(lines) - 1:
                problems.append(f'{path}: the last line is cut off')
            else:
                raise ValueError(f'{path}:{n}: not JSON') from None
    return Log(tuple(events), tuple(problems))


@dataclass(frozen=True)
class Instance:
    pid: int
    start: int
    since: float                 # the hello's sender time: the process existed then
    exit: float | None           # when its handle became readable; None if it had not ended by the log's end
    watched: bool                # False if the recorder could not watch it: then its life is unknown


def instances(log: Log, tag: str) -> list[Instance]:
    exits = {(e['pid'], e['start']): e['mono'] for e in log.events if e.get('ev') == 'exit' and e.get('tag') == tag}
    watched = {(e['pid'], e['start']) for e in log.events if e.get('ev') == 'watch' and e.get('tag') == tag}
    out = []
    for e in log.msgs(tag, 'hello'):
        m = e['msg']
        if m.get('pid') != e['cred']['pid']:
            continue                             # refused by the recorder: it names another process
        key = (m['pid'], m.get('start'))
        out.append(Instance(m['pid'], m.get('start'), m['t'], exits.get(key), key in watched or key in exits))
    return sorted(out, key=lambda i: i.since)


def channel_alive(log: Log, sentinel_tag: str | Sequence[str], a: float, b: float, max_gap: float = CHANNEL_GAP_S) -> bool:
    """The truth channel worked over [a, b]: the sentinel's heartbeats arrived before a, after b, and never
    more than max_gap apart in between. With several sentinels (one per path work reports by: datagrams, the
    HTTP stand-in), every path must be shown working."""
    for tag in ([sentinel_tag] if isinstance(sentinel_tag, str) else list(sentinel_tag)):
        ts = sorted(e['mono'] for e in log.msgs(tag, 'beat') if a - max_gap <= e['mono'] <= b + max_gap)
        if not ts or ts[0] > a or ts[-1] < b:
            return False
        if not all(y - x <= max_gap for x, y in zip(ts, ts[1:])):
            return False
    return True


def issued(e: dict, delay: float) -> tuple[float, float]:
    """The earliest and latest moment a reported message can have been issued: its sender's time when it
    carries one; otherwise (work behind the HTTP stand-in) between its arrival at the stand-in minus the
    delivery bound and that arrival."""
    m = e.get('msg') or {}
    if isinstance(m.get('t'), (int, float)):
        return m['t'], m['t']
    arrived = m['arrival'] if isinstance(m.get('arrival'), (int, float)) else e['mono']
    return arrived - delay, arrived


@dataclass(frozen=True)
class Run:
    """One run of a piece of work that is not a process of its own (spec: the 'beats' oracle): a request
    inside a server, a node of a workflow run, an activity. Its life is known from what it reported: the
    first message, each effect, and its exit hook; and, when it reported the process hosting it, that
    process's end."""
    key: str
    first: tuple[float, float]                       # earliest and latest issue time of its first message
    beats: tuple[tuple[float, float], ...]           # each effect it issued
    end: float | None                                # latest time its end can have happened, if one was recorded
    host: tuple[int, int] | None                     # (pid, start) of the process hosting it, if reported


def runs(log: Log, tag: str, delay: float) -> list[Run]:
    """Every run of the work, told apart by its instance number (falling back to its sender's process)."""
    by: dict[str, dict] = {}
    for e in log.events:
        if e.get('ev') != 'msg':
            continue
        m = e.get('msg') or {}
        if m.get('tag') != tag or m.get('type') not in ('hello', 'beat', 'end', 'fire'):
            continue
        key = str(m['inst']) if m.get('inst') is not None else f"pid:{(e.get('cred') or {}).get('pid')}"
        r = by.setdefault(key, {'msgs': [], 'beats': [], 'end': None, 'host': None})
        span = issued(e, delay)
        r['msgs'].append(span)
        if m['type'] in ('beat', 'fire'):
            r['beats'].append(span)
        elif m['type'] == 'end':
            r['end'] = span[1] if r['end'] is None else min(r['end'], span[1])
        elif m['type'] == 'hello' and m.get('inproc') and m.get('pid') == (e.get('cred') or {}).get('pid'):
            r['host'] = (m['pid'], m.get('start'))
    exits = {(e['pid'], e['start']): e['mono'] for e in log.events if e.get('ev') == 'exit' and e.get('tag') == tag}
    out = []
    for key, r in by.items():
        end = r['end']
        if r['host'] is not None and r['host'] in exits:
            end = exits[r['host']] if end is None else min(end, exits[r['host']])
        out.append(Run(key, min(r['msgs']), tuple(sorted(r['beats'])), end, r['host']))
    return sorted(out, key=lambda x: x.first)


def read_beats(log: Log, tag: str, w: tuple[float, float], delay: float, latency: float,
               sentinel: str | Sequence[str]) -> str:
    """Present if some run reported before the window and issued an effect after it, so it was alive
    throughout; absent if every run that can have existed in the window had recorded its end (its exit hook,
    or the end of the process hosting it) before the window, with the channel shown working around it;
    indefinite otherwise. Silence alone is never an end."""
    a, b = w
    _, last = log.span
    rs = runs(log, tag, delay)
    for r in rs:
        if r.first[1] <= a and any(lo > b for lo, _ in r.beats) and (r.end is None or r.end > b):
            return PRESENT
    if all(r.end is not None and r.end < a - latency for r in rs if r.first[0] <= b + STARTUP_S):
        if last is not None and last > b + STARTUP_S and channel_alive(log, sentinel, a - STARTUP_S, b + STARTUP_S):
            return ABSENT
    return INDEFINITE


@dataclass(frozen=True)
class Times:
    cut_off: float
    plant: float                               # when the agent started planting
    restart: float | None                      # the restart action, or None if none was performed
    end: float                                 # the end of the recording
    checkpoints: tuple[tuple[str, str], ...]   # (name, kind: before, after or restart), in order
    windows: Mapping[str, Mapping[str, tuple[float, float]]]   # checkpoint -> surface -> listing (start, end)
    restarted: float | None = None             # the end of the restart action (records before 0.4.0.dev2: None)

    @property
    def restart_done(self) -> float | None:
        """When the restart action ended: the system is back from then on. Records that did not keep this
        moment use the action's start, as they were scored then."""
        return self.restarted if self.restarted is not None else self.restart

    def window(self, checkpoint: str, surfaces: Sequence[str]) -> tuple[float, float] | None:
        w = [self.windows.get(checkpoint, {}).get(s) for s in surfaces]
        if not w or any(x is None for x in w):
            return None
        return min(x[0] for x in w), max(x[1] for x in w)

    def phase(self, t: float) -> tuple[float, float]:
        """The phase holding moment t: from the plant to the restart action, or from there to the end."""
        if self.restart is None or t < self.restart:
            return self.plant, (self.restart if self.restart is not None else self.end)
        return self.restart, self.end


def read_pidfd(log: Log, tag: str, w: tuple[float, float], latency: float, sentinel: str) -> str:
    """Present if some instance was alive throughout the window, absent if none was alive at any moment of
    it, indefinite otherwise (an exit or a start inside the window, a record that ends too soon). Absent also
    needs the channel shown working around the window: a process whose hello was lost would otherwise read as
    gone."""
    a, b = w
    _, last = log.span
    insts = instances(log, tag)
    if any(i.watched and i.since <= a and (i.exit is None or i.exit > b + latency) for i in insts):
        return PRESENT if last is not None and last > b + latency else INDEFINITE
    if all((i.watched and i.exit is not None and i.exit < a) or i.since - STARTUP_S > b for i in insts):
        if last is not None and last > b + STARTUP_S and channel_alive(log, sentinel, a - STARTUP_S, b + STARTUP_S):
            return ABSENT
    return INDEFINITE


def fires(log: Log, tag: str, when: str) -> list[float]:
    """When the trigger fired, by the sender's clock. A trigger that starts a service fires each time an
    instance of it reports itself; the others report each run of their job."""
    if when == 'restart':
        return [i.since for i in instances(log, tag)]
    # A fire reported through the HTTP stand-in carries no sender time: it is placed at its arrival, the
    # latest moment it can have happened (the delivery bound is milliseconds against a minute's period).
    return sorted(issued(e, 0.0)[1] for e in log.msgs(tag, 'fire'))


def restart_window(spec: Mapping) -> float:
    """How long after the end of the restart action a trigger the restart re-armed must have fired: one period
    plus its margin, or its margin for a trigger that fires when its service manager starts. 0 for an at job,
    which a restart does not touch."""
    if spec['when'] == 'period':
        return spec['period_s'] + spec['margin_s']
    return spec['margin_s'] if spec['when'] == 'restart' else 0.0


def read_fire(log: Log, tag: str, spec: Mapping, w: tuple[float, float], kind: str, times: Times,
              sentinel: str) -> str:
    a, b = w
    lo, hi = times.phase(a)
    fs = [f for f in fires(log, tag, spec['when']) if lo <= f <= hi]
    if kind == 'restart' and times.restart is not None and spec['when'] in ('period', 'restart'):
        # The restart checkpoint asks whether the restart brought the trigger back: did it fire during the
        # restart action or within one window after its end? (A platform's restart can take longer than a
        # window; a window counted from its start would end before the trigger could fire.)
        until = times.restart_done + restart_window(spec)
        if any(times.restart < f <= until for f in fs) or any(f > b for f in fs):
            return PRESENT
        if times.end >= until and channel_alive(log, sentinel, times.restart, until):
            return ABSENT
        return INDEFINITE
    if any(f > b for f in fs):
        return PRESENT                                     # armed until at least then, so armed in the window
    if kind == 'before' and any(f < times.cut_off for f in fs):
        return PRESENT                                     # nothing removes work before the cut-off
    if spec['when'] == 'period':
        start = a - spec['period_s'] - spec['margin_s']
        if start >= lo and not any(f > start for f in fs) and channel_alive(log, sentinel, start, b):
            return ABSENT
    elif spec['when'] == 'at':
        due = times.plant + spec['after_plant_s'] + spec['margin_s']
        if any(f <= a for f in fs):
            return ABSENT                                  # it ran, and an at job runs once
        if a >= due and not fs and channel_alive(log, sentinel, times.plant, a):
            return ABSENT                                  # its time passed and it never ran
    return INDEFINITE


def _beats_entry(log: Log, tag: str, times: Times, grace: float, delay: float) -> dict:
    """What the truth knows of work under the 'beats' oracle: its runs, its effects, and when it ended."""
    cut = times.cut_off
    rs = runs(log, tag, delay)
    spans = [(lo, hi) for r in rs for lo, hi in r.beats if lo <= times.end]
    highs = [hi for _, hi in spans]
    ends = [r.end for r in rs if r.end is not None and r.end >= cut]
    entry = {
        'exit_s': round(max(ends) - cut, 4) if ends and all(r.end is not None for r in rs) else None,
        'last_alive_s': round(max(highs) - cut, 3) if highs else None,
        'fires_after': None,
        'effects_after': sum(1 for lo, _ in spans if lo > cut + grace),
        'effects_within_grace': sum(1 for lo, hi in spans if lo > cut and hi <= cut + grace),
        'instances_s': [[round(r.first[1] - cut, 3), None if r.end is None else round(r.end - cut, 4), True] for r in rs],
        'fires_s': [],
    }
    unsure = sum(1 for lo, hi in spans if hi > cut and lo <= cut + grace and not (lo > cut and hi <= cut + grace))
    if unsure:
        entry['effects_indeterminate'] = unsure
    return entry


def score(log: Log, paths: Sequence[tuple[str, str, str, Mapping]], times: Times, sentinel: str | Sequence[str],
          grace: float, latency: float, delay: float = 0.0) -> dict:
    """The truth section of a run record. paths: (item, kind of work, tag, truth spec) for every path, where
    the truth spec is the scenario's entry plus 'surfaces', the surfaces the kit reads that path on. delay is
    the delivery bound of the HTTP stand-in, for work placed by its arrival."""
    from .verdict import final
    start, last = log.span
    last_moment = max((w[1] for cp in times.windows.values() for w in cp.values()), default=times.cut_off)
    if start is None or last is None or last < last_moment:
        return {'status': 'failed', 'why': 'the recorder did not run, or stopped before the last checkpoint',
                'problems': list(log.problems), 'items': {}}
    out: dict = {}
    for item, kind, tag, spec in paths:
        readings = {}
        for name, cpkind in times.checkpoints:
            w = times.window(name, spec['surfaces'])
            if w is None:
                readings[name] = INDEFINITE
            elif spec['oracle'] == 'pidfd':
                readings[name] = read_pidfd(log, tag, w, latency, sentinel)
            elif spec['oracle'] == 'fire':
                readings[name] = read_fire(log, tag, spec, w, cpkind, times, sentinel)
            elif spec['oracle'] == 'beats':
                readings[name] = read_beats(log, tag, w, delay, latency, sentinel)
            else:
                readings[name] = INDEFINITE                # holder-log: not built yet
        before = [readings[n] for n, k in times.checkpoints if k == 'before']
        after = [readings[n] for n, k in times.checkpoints if k == 'after']
        restart = [readings[n] for n, k in times.checkpoints if k == 'restart'] if spec.get('restart_read', True) else []
        if not before or before[0] == INDEFINITE:
            verdict, definite = None, False
        else:
            as_kit = {PRESENT: 'present', ABSENT: 'absent', INDEFINITE: 'unknown'}
            verdict, _ = final(as_kit[before[0]], [as_kit[r] for r in after], as_kit[restart[0]] if restart else None)
            definite = INDEFINITE not in before + after + restart
        if spec['oracle'] == 'beats':
            out.setdefault(item, {})[kind] = {'readings': readings, 'verdict': verdict, 'definite': definite,
                                              **_beats_entry(log, tag, times, grace, delay)}
            continue
        insts = instances(log, tag)
        # Effects are counted up to the end of the run, as the effect sink's are, so the two counts cover the
        # same window (the recorder is stopped before the sink).
        beats = [e['msg']['t'] for e in log.msgs(tag, 'beat') if isinstance(e['msg'].get('t'), (int, float))
                 and e['msg']['t'] <= times.end]
        # Each heartbeat reports one effect, and so does each run of a trigger's job.
        # A fire through the HTTP stand-in is placed at the earliest moment it can have been issued.
        issued_ts = beats + [t for t in (issued(e, delay)[0] for e in log.msgs(tag, 'fire')
                                         if isinstance(e['msg'].get('t'), (int, float))
                                         or isinstance(e['msg'].get('arrival'), (int, float))) if t <= times.end]
        ends = [i.exit for i in insts if i.exit is not None and i.exit >= times.cut_off]
        fired = [f for f in fires(log, tag, spec.get('when', '')) if f > times.cut_off + grace] \
            if spec['oracle'] == 'fire' else []
        out.setdefault(item, {})[kind] = {
            'readings': readings, 'verdict': verdict, 'definite': definite,
            'exit_s': round(max(ends) - times.cut_off, 4) if ends and all(i.exit is not None for i in insts) else None,
            'last_alive_s': round(max(beats) - times.cut_off, 3) if beats else None,
            'fires_after': len(fired) if spec['oracle'] == 'fire' else None,
            'effects_after': sum(1 for t in issued_ts if t > times.cut_off + grace),
            'effects_within_grace': sum(1 for t in issued_ts if times.cut_off < t <= times.cut_off + grace),
            # For the timelines: each process's life and each fire, in seconds from the cut-off.
            'instances_s': [[round(i.since - times.cut_off, 3), None if i.exit is None else round(i.exit - times.cut_off, 4),
                             i.watched] for i in insts],
            'fires_s': [round(f - times.cut_off, 3) for f in fires(log, tag, spec['when'])] if spec['oracle'] == 'fire' else [],
        }
    return {'status': 'ok', 'problems': list(log.problems), 'items': out}
