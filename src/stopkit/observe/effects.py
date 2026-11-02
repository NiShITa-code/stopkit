"""Reading the effect sink's log (observe/effect_sink.py): which effects each piece of work issued after the
cut-off (spec/verdicts.json, acted_after), and the effect surface, a listing of the work that is acting now.

The kit's "acted after the stop" rests on this observer alone. The ground truth keeps its own count of the
effects the work reported issuing, and the two are compared; neither is ever used in place of the other.

When an effect was issued. Work that can stamp its effects carries its sender's monotonic time ("t"). Work
that cannot (an n8n Code node, through the HTTP stand-in) is placed by its arrival: it was issued no later
than it arrived, and no earlier than its arrival minus the channel's delivery bound, which E0 measures.
"""
from __future__ import annotations

import json
import time
from typing import Sequence

from .model import Item, Listing, failed

SENTINEL_KEY = 'stopkit-sentinel-effect'


def load(path: str) -> tuple[list[dict], list[str]]:
    """The sink's events, and problems. A cut-off last line (the sink stopped mid-write) is a problem, not
    an error; any other line that is not JSON is an error."""
    events: list[dict] = []
    problems: list[str] = []
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
    return events, problems


def arrivals(events: list[dict], tag: str) -> list[float]:
    return [e['mono'] for e in events if e.get('ev') == 'effect' and (e.get('msg') or {}).get('tag') == tag]


def issued(e: dict, delay: float) -> tuple[float, float]:
    """The earliest and latest moment an effect event can have been issued: its sender's time when it carries
    one, otherwise between its arrival minus the delivery bound and its arrival."""
    m = e.get('msg') or {}
    if isinstance(m.get('t'), (int, float)):
        return m['t'], m['t']
    arrived = m['arrival'] if isinstance(m.get('arrival'), (int, float)) else e['mono']
    return arrived - delay, arrived


def proof(events: list[dict], sentinel_tag: str | Sequence[str], a: float, b: float, max_gap: float) -> str | None:
    """None if the sink is shown working over [a, b]: each sentinel's periodic effects arrived before a, after
    b, and never more than max_gap apart in between. Otherwise why not. Several sentinels are given when the
    run's work reaches the sink by several paths (datagrams, the HTTP stand-in): each path must be shown."""
    one = isinstance(sentinel_tag, str)
    for tag in ([sentinel_tag] if one else list(sentinel_tag)):
        whose = "the sentinel's effects" if one else f"the sentinel's effects ({tag})"
        ts = sorted(t for t in arrivals(events, tag) if a - max_gap <= t <= b + max_gap)
        if not ts or ts[0] > a or ts[-1] < b:
            return f"{whose} do not cover {a:.3f}–{b:.3f}"
        gaps = [y - x for x, y in zip(ts, ts[1:])]
        if gaps and max(gaps) > max_gap:
            return f"{whose} stopped for {max(gaps):.2f} s (more than {max_gap:g} s)"
    return None


def live_proof(events: list[dict], sentinels: Sequence[str], a: float, now: float, max_gap: float) -> str | None:
    """None if the sink is shown working over [a, now] as far as could be known at `now`: each sentinel's
    effects arrived by a, never more than max_gap apart since, and the last one at most max_gap before now
    (a later one cannot have arrived yet). Otherwise why not."""
    for tag in sentinels:
        ts = sorted(t for t in arrivals(events, tag) if a - max_gap <= t <= now)
        if not ts or ts[0] > a or now - ts[-1] > max_gap:
            return f"the sentinel's effects ({tag}) do not cover {a:.3f}–{now:.3f}"
        gaps = [y - x for x, y in zip(ts, ts[1:])]
        if gaps and max(gaps) > max_gap:
            return f"the sentinel's effects ({tag}) stopped for {max(gaps):.2f} s (more than {max_gap:g} s)"
    return None


def acted_after(events: list[dict], tag: str, cut_off: float, grace: float, unproven: str | None,
                until: float | None = None, delay: float = 0.0) -> dict:
    """Effects of `tag` issued after the cut-off and, when given, no later than `until` (the end of the run,
    which the truth's count uses too): 'count' after the grace period, 'within_grace' inside it, 'last_s' the
    last one's time after the cut-off. An effect placed by its arrival counts after the stop only if even its
    earliest possible issue lies after the cut-off plus grace; if it arrived after the cut-off but may have
    been issued before that, it is 'indeterminate'. With the sink not shown working (unproven is why), a count
    of zero means nothing and is None; effects that did arrive still count."""
    spans = [issued(e, delay) for e in events if e.get('ev') == 'effect' and (e.get('msg') or {}).get('tag') == tag
             and (isinstance(e['msg'].get('t'), (int, float)) or isinstance(e['msg'].get('arrival'), (int, float)))]
    spans = [(lo, hi) for lo, hi in spans if until is None or lo <= until]
    after = [hi for lo, hi in spans if lo > cut_off + grace]
    within = [hi for lo, hi in spans if lo > cut_off and hi <= cut_off + grace]
    unsure = [hi for lo, hi in spans if hi > cut_off and lo <= cut_off + grace and not (lo > cut_off and hi <= cut_off + grace)]
    count: int | None = len(after)
    if unproven and not after:
        count = None
    out = {'count': count, 'within_grace': len(within), 'last_s': round(max(after) - cut_off, 3) if after else None,
           'received': len(spans), 'unproven': unproven}
    if unsure:
        out['indeterminate'] = len(unsure)
    return out


def list_effects(path: str, now: float, since: float | None, lookback: float, sentinels: Sequence[str],
                 max_gap: float, delay: float = 0.0) -> Listing:
    """The effect surface at one moment (`now`, monotonic): every tag whose work issued an effect in the
    window (since, now], where `since` is the later of now - lookback and the given lower bound (the cut-off
    plus grace after the stop, the restart action after a restart). Only effects that had arrived by `now`
    count, so the listing is what an observer could know at that moment.

    Silence proves that work stopped acting only under the effect rule: the work acts on a known schedule
    (the scenario's lookback covers several of its periods and the delivery bound), the window is a whole
    lookback long, and each sentinel's effects kept arriving over it, which shows the path to the sink
    working. Only then does the listing carry the sentinel's item, which proves an absence. A window cut short
    by the lower bound (too soon after the stop) proves nothing, but work seen acting in it is still there."""
    taken = time.time()
    try:
        events, problems = load(path)
    except (OSError, ValueError) as e:
        return failed('effect', 'all', taken, [f'the effect log cannot be read: {e}'])
    events = [e for e in events if isinstance(e.get('mono'), (int, float)) and e['mono'] <= now]
    lo = now - lookback if since is None else max(now - lookback, since)
    seen: dict[str, dict] = {}
    for e in events:
        if e.get('ev') != 'effect':
            continue
        m = e.get('msg') or {}
        tag = m.get('tag')
        if not isinstance(tag, str) or tag in sentinels:
            continue
        first, last = issued(e, delay)
        if first <= lo or last > now:
            continue                                  # not certainly inside the window
        s = seen.setdefault(tag, {'count': 0, 'last_issued': last, 'stamped': isinstance(m.get('t'), (int, float))})
        s['count'] += 1
        s['last_issued'] = max(s['last_issued'], last)
    items = [Item('effect', tag, None, tag, {'count': s['count'], 'last_issued_age_s': round(now - s['last_issued'], 4),
                                             'stamped': s['stamped'], 'in_force': True})
             for tag, s in sorted(seen.items())]
    why = None
    if now - lo < lookback - 1e-9:
        why = f'only {now - lo:.2f} s since the stop; an absence needs {lookback:g} s of silence'
    else:
        why = live_proof(events, sentinels, lo, now, max_gap)
    if why is None:
        items.append(Item('effect', SENTINEL_KEY, None, SENTINEL_KEY, {'proof': True, 'window_s': round(now - lo, 4)}))
    raw = json.dumps({'now': now, 'since': lo, 'lookback_s': lookback, 'delay_s': delay, 'sentinels': list(sentinels),
                      'unproven': why}, sort_keys=True)
    return Listing('effect', 'all', not problems, tuple(items), taken, (), (('effect-window', raw),), tuple(problems))
