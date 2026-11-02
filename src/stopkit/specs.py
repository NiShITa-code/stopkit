"""The versioned test specification (FRAMEWORK.md sections 1 to 4): kinds of work, outcome families, item
structures, the stop register, the surfaces and their proofs, the verdict rules and the scenarios. Everything
that scores or reports reads these files, so the framework, the reports and the coverage matrix share one set
of definitions.

The files live in stopkit/spec/ and are data, not code. load() checks them; a broken specification raises
SpecError rather than producing results under a definition nobody wrote down.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

SPEC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'spec')
REPORT_KINDS = ('explicit', 'apparent', 'none')
SCOPE_SOURCES = ('documentation', 'mechanism', 'definition')
ORACLES = ('pidfd', 'fire', 'holder-log', 'beats')
EFFECT_CHANNELS = ('datagram', 'http')     # how planted work reaches the effect sink: directly, or the HTTP stand-in
FIRE_WHEN = {'period': ('period_s', 'margin_s'), 'at': ('after_plant_s', 'margin_s'), 'restart': ('margin_s',)}
OBSERVER_KINDS = ('existence', 'effect', 'access', 'trace')
VERDICTS = ('invalid', 'still running', 'came back', 'ended', 'ended late', 'unknown')
READINGS = ('present', 'absent', 'unknown')


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Stop:
    id: str
    cls: str
    implemented: bool
    mechanism: str
    structures: tuple[str, ...]        # the item structures inside its intended scope; '*' means every structure
    scope_source: str                  # documentation, mechanism or definition
    report_kind: str
    actor: str
    note: str = ''
    scope_doc: str = ''                # the documentation the intended scope is taken from, when it is

    def in_scope(self, structure: str) -> bool:
        return '*' in self.structures or structure in self.structures

    @property
    def whole_agent(self) -> bool:
        return '*' in self.structures


@dataclass(frozen=True)
class Surface:
    name: str
    implemented: bool
    observer: str
    kind: str                          # one of OBSERVER_KINDS
    lists: str
    proof: str
    kinds: tuple[str, ...]             # the kinds of work it can show
    blind_spots: tuple[str, ...]


@dataclass(frozen=True)
class Item:
    id: str
    case: str
    structure: str
    paths: Mapping[str, tuple[str, ...]]     # kind of work -> surfaces where that path is looked for
    path_structures: Mapping[str, str]       # a path whose structure differs from the item's (a unit's trigger)
    truth: Mapping[str, Mapping[str, Any]]   # kind of work -> how the ground truth reads that path
    raw: Mapping[str, Any]

    def structure_of(self, kind: str) -> str:
        return self.path_structures.get(kind, self.structure)


@dataclass(frozen=True)
class Scenario:
    name: str
    binding: str
    items: tuple[Item, ...]
    heartbeat_s: float
    grace_s: float
    checks_s: tuple[float, ...]
    restart_action: str
    # For work read on the effect surface: how far back a listing looks (several of the work's periods plus
    # the delivery bound; an absence needs a whole window of silence), the paths its effects take to the
    # sink, and the delivery bound of work placed by its arrival (the HTTP stand-in; measured in E0).
    effect_lookback_s: float | None = None
    effect_channels: tuple[str, ...] = ('datagram',)
    delivery_bound_s: float = 0.0
    restart_settle_s: float = 3.0              # after the restart action, before the restart checkpoint
    ready_timeout_s: float = 100.0             # for the truth and the listings to show every item running

    def paths(self) -> list[tuple[Item, str]]:
        """Every (item, kind of work) pair, in file order: one row per pair in every result."""
        return [(it, k) for it in self.items for k in it.paths]


def _read(name: str) -> Any:
    with open(os.path.join(SPEC_DIR, name), encoding='utf-8') as f:
        return json.load(f)


@lru_cache(maxsize=1)
def version() -> str:
    return _read('version.json')['spec_version']


@lru_cache(maxsize=1)
def kinds() -> dict:
    data = _read('kinds.json')
    fams = data['families']
    for k, v in data['kinds'].items():
        if v.get('family') not in fams:
            raise SpecError(f'kind {k}: unknown family {v.get("family")!r}')
    return data


def family_of(kind: str) -> str:
    try:
        return kinds()['kinds'][kind]['family']
    except KeyError:
        raise SpecError(f'unknown kind of work {kind!r}') from None


@lru_cache(maxsize=1)
def structures() -> dict[str, str]:
    return dict(_read('structures.json')['structures'])


def _check_structure(where: str, s: str) -> None:
    if s != '*' and s not in structures():
        raise SpecError(f'{where}: unknown structure {s!r} (spec/structures.json)')


@lru_cache(maxsize=1)
def stops() -> dict[str, Stop]:
    data = _read('stops.json')
    classes = data['classes']
    out = {}
    for sid, s in data['stops'].items():
        if s.get('class') not in classes:
            raise SpecError(f'stop {sid}: unknown class {s.get("class")!r}')
        if s.get('report_kind') not in REPORT_KINDS:
            raise SpecError(f'stop {sid}: report_kind must be one of {REPORT_KINDS}')
        if s.get('scope_source') not in SCOPE_SOURCES:
            raise SpecError(f'stop {sid}: scope_source must be one of {SCOPE_SOURCES}')
        if s['scope_source'] == 'documentation' and not str(s.get('scope_doc', '')).startswith('https://'):
            raise SpecError(f'stop {sid}: a scope taken from documentation needs a scope_doc link')
        for field in ('mechanism', 'actor'):
            if not s.get(field):
                raise SpecError(f'stop {sid}: {field} is required')
        for st in s['intended_scope']['structures']:
            _check_structure(f'stop {sid}', st)
        out[sid] = Stop(sid, s['class'], bool(s.get('implemented')), s['mechanism'],
                        tuple(s['intended_scope']['structures']), s['scope_source'], s['report_kind'],
                        s['actor'], s.get('note', ''), s.get('scope_doc', ''))
    return out


def stop(stop_id: str) -> Stop:
    try:
        return stops()[stop_id]
    except KeyError:
        raise SpecError(f'stop {stop_id!r} is not in the stop register (spec/stops.json)') from None


@lru_cache(maxsize=1)
def surfaces() -> dict[str, Surface]:
    known = kinds()['kinds']
    out = {}
    for name, s in _read('surfaces.json')['surfaces'].items():
        if s.get('observer_kind') not in OBSERVER_KINDS:
            raise SpecError(f'surface {name}: observer_kind must be one of {OBSERVER_KINDS}')
        for k in s['kinds']:
            if k not in known:
                raise SpecError(f'surface {name}: unknown kind {k!r}')
        for field in ('observer', 'lists', 'proof'):
            if not s.get(field):
                raise SpecError(f'surface {name}: {field} is required')
        out[name] = Surface(name, bool(s.get('implemented')), s['observer'], s['observer_kind'], s['lists'],
                            s['proof'], tuple(s['kinds']), tuple(s.get('blind_spots', ())))
    return out


@lru_cache(maxsize=1)
def verdict_rules() -> dict:
    data = _read('verdicts.json')
    if tuple(v['verdict'] for v in data['verdicts']) != VERDICTS:
        raise SpecError(f'spec/verdicts.json: the verdicts must be, in order, {VERDICTS}')
    if set(data['readings']) != set(READINGS):
        raise SpecError(f'spec/verdicts.json: the readings must be {READINGS}')
    for fam, words in data['words'].items():
        if fam != 'about' and set(words) != set(VERDICTS):
            raise SpecError(f'spec/verdicts.json: family {fam} needs a word for every verdict')
    for key, cat in list(data['accuracy']['verdict_pairs'].items()) + list(data['accuracy']['reading_pairs'].items()):
        if cat not in data['accuracy']['categories']:
            raise SpecError(f'spec/verdicts.json: pair {key!r}: unknown category {cat!r}')
    for ex in data['examples']:
        if ex['verdict'] not in VERDICTS or ex['before'] not in READINGS or \
                any(r not in READINGS for r in ex['after']) or ex['restart'] not in READINGS + (None,):
            raise SpecError(f'spec/verdicts.json: bad example {ex}')
    return data


def _check_truth(where: str, kind: str, t: Mapping[str, Any]) -> None:
    if t.get('oracle') not in ORACLES:
        raise SpecError(f'{where}, {kind}: oracle must be one of {ORACLES}')
    if t['oracle'] == 'fire':
        need = FIRE_WHEN.get(t.get('when'))
        if need is None:
            raise SpecError(f'{where}, {kind}: a fire oracle needs "when", one of {tuple(FIRE_WHEN)}')
        for field in need:
            if not isinstance(t.get(field), (int, float)) or t[field] <= 0:
                raise SpecError(f'{where}, {kind}: a fire oracle with when={t["when"]} needs {field} > 0')


@lru_cache(maxsize=None)
def scenario(name: str) -> Scenario:
    data = _read(os.path.join('scenarios', f'{name}.json'))
    known, surf = kinds()['kinds'], surfaces()
    items = []
    for it in data['items']:
        where = f'scenario {name}, item {it["id"]}'
        _check_structure(where, it['structure'])
        for k, ss in it['paths'].items():
            if k not in known:
                raise SpecError(f'{where}: unknown kind {k!r}')
            for s in ss:
                if s not in surf:
                    raise SpecError(f'{where}, {k}: unknown surface {s!r} (spec/surfaces.json)')
            if k not in it.get('truth', {}):
                raise SpecError(f'{where}, {k}: no ground truth for this path')
            _check_truth(where, k, it['truth'][k])
        for k, st in it.get('path_structures', {}).items():
            if k not in it['paths']:
                raise SpecError(f'{where}: path_structures names {k!r}, which is not one of its paths')
            _check_structure(where, st)
        items.append(Item(it['id'], it.get('case', it['id']), it['structure'],
                          {k: tuple(v) for k, v in it['paths'].items()}, dict(it.get('path_structures', {})),
                          {k: dict(v) for k, v in it['truth'].items()}, it))
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        raise SpecError(f'scenario {name}: item ids repeat')
    if not float(data['grace_s']) >= float(data['heartbeat_s']) > 0:
        raise SpecError(f'scenario {name}: grace_s must be at least one heartbeat period (heartbeat_s > 0)')
    uses_effect = any('effect' in ss for it in items for ss in it.paths.values())
    lookback = data.get('effect_lookback_s')
    if uses_effect and not (isinstance(lookback, (int, float)) and lookback > 0):
        raise SpecError(f'scenario {name}: work read on the effect surface needs effect_lookback_s > 0')
    channels = tuple(data.get('effect_channels', ('datagram',)))
    if not channels or any(c not in EFFECT_CHANNELS for c in channels):
        raise SpecError(f'scenario {name}: effect_channels must name some of {EFFECT_CHANNELS}')
    return Scenario(data['scenario'], data['binding'], tuple(items), float(data['heartbeat_s']),
                    float(data['grace_s']), tuple(float(x) for x in data['checks_s']), data['restart_action'],
                    float(lookback) if lookback else None, channels, float(data.get('delivery_bound_s', 0.0)),
                    float(data.get('restart_settle_s', 3.0)), float(data.get('ready_timeout_s', 100.0)))


def load() -> None:
    """Check every specification file now (the CLI calls this before any run)."""
    version(); kinds(); structures(); stops(); surfaces(); verdict_rules()
    for f in os.listdir(os.path.join(SPEC_DIR, 'scenarios')):
        if f.endswith('.json'):
            scenario(f[:-5])
