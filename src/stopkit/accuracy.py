"""Accuracy against ground truth (spec/verdicts.json, accuracy): the kit's readings and verdicts set beside
the truth's, at the same moments, for bench runs. It only counts; it never changes a verdict.

  reading_pairs   every path at every checkpoint: the kit's reading against the truth reading
  verdict_pairs   every path: the kit's verdict against the truth verdict (scored only where it is definite)
  effect_pairs    every item's activity: the effect sink's count after the cut-off against the truth's
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from . import specs


def _truth_of(rec: dict, item: str, kind: str) -> dict:
    return ((rec.get('truth') or {}).get('items') or {}).get(item, {}).get(kind) or {}


def reading_pairs(rec: dict) -> list[dict]:
    if (rec.get('truth') or {}).get('status') != 'ok':
        return []
    rules = specs.verdict_rules()['accuracy']['reading_pairs']
    out = []
    for it in rec.get('items', ()):
        tr = _truth_of(rec, it['id'], it['kind']).get('readings') or {}
        for cp, kit in it['readings'].items():
            tru = tr.get(cp)
            if tru is None:
                continue
            cat = 'indefinite' if tru == 'indefinite' else rules.get(f'{kit}|{tru}', f'unexpected {kit}|{tru}')
            out.append({'run': rec['run_id'], 'stop': rec['stop_spec']['id'], 'item': it['id'], 'kind': it['kind'],
                        'checkpoint': cp, 'kit': kit, 'truth': tru, 'category': cat})
    return out


def verdict_pairs(rec: dict) -> list[dict]:
    if (rec.get('truth') or {}).get('status') != 'ok':
        return []
    rules = specs.verdict_rules()['accuracy']['verdict_pairs']
    out = []
    for it in rec.get('items', ()):
        tr = _truth_of(rec, it['id'], it['kind'])
        kit, tv, definite = it['verdict'], tr.get('verdict'), bool(tr.get('definite'))
        if kit == 'invalid' or tv == 'invalid':
            cat = 'invalid'
        elif not definite or tv is None:
            cat = 'indefinite'
        elif kit == 'unknown':
            cat = 'kit unknown'
        else:
            cat = rules.get(f'{kit}|{tv}', f'unexpected {kit}|{tv}')
        out.append({'run': rec['run_id'], 'stop': rec['stop_spec']['id'], 'item': it['id'], 'kind': it['kind'],
                    'kit': kit, 'truth': tv, 'definite': definite, 'category': cat})
    return out


def effect_pairs(rec: dict) -> list[dict]:
    """The sink's count of effects after the cut-off plus grace against the truth's, for each item whose
    activity is counted on that path."""
    if (rec.get('truth') or {}).get('status') != 'ok':
        return []
    out = []
    for it in rec.get('items', ()):
        acted = it.get('acted_after') or {}
        if 'counted_on' in acted:
            continue
        tr = _truth_of(rec, it['id'], it['kind'])
        kit, tru = acted.get('count'), tr.get('effects_after')
        if kit is None:
            cat = 'kit unknown'
        elif tru is None:
            cat = 'indefinite'
        elif kit == tru:
            cat = 'agree'
        elif kit == tru + 1 and tr.get('exit_s') is not None:
            # The reporter sends each effect, then its report: a kill between the two leaves the last effect
            # unreported (E0 found no other difference). Counted apart, not as an error.
            cat = 'agree but for the last effect, cut off from its report by the kill'
        else:
            cat = 'sink missed effects' if kit < tru else 'sink has more'
        out.append({'run': rec['run_id'], 'stop': rec['stop_spec']['id'], 'item': it['id'], 'kind': it['kind'],
                    'kit': kit, 'truth': tru, 'category': cat})
    return out


def tally(pairs: Iterable[dict], by: str | None = None) -> dict:
    """Counts of each category, overall or per value of field `by`."""
    if by is None:
        return dict(Counter(p['category'] for p in pairs))
    out: dict = {}
    for p in pairs:
        out.setdefault(p[by], Counter())[p['category']] += 1
    return {k: dict(v) for k, v in sorted(out.items())}
