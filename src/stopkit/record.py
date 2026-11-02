"""The record of a run: everything needed to check its verdicts again later, by anyone.

A run folder holds:
  record.json   what was planned, done and read: every listing with its commands, every reading, the
                stop's own report, the verdicts and the clean-up
  report.md     the score sheet, for people
  blobs/        every command's whole output and every listing's items, gzip-compressed, named by SHA-256
  SHA256SUMS    checksums of record.json and report.md

recheck() reads the listings back from blobs/, computes every reading and verdict again with the same
rules, and lists any difference. Nothing in a run folder depends on the machine it was made on.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from typing import Any

from .accounts import Account
from .adapters.base import StopClaim
from .cases import Case
from .evidence import EvidenceStore
from .observe.model import Item, Listing
from .observe.snapshot import Snapshot
from .verdict import PRESENT, UNKNOWN, final, read_checkpoint, restart_problem, truthfulness

FORMAT = 'stopkit-record/1'
FILES = ('record.json', 'report.md')


def item_to_dict(i: Item) -> dict:
    return {'surface': i.surface, 'key': i.key, 'owner': i.owner, 'text': i.text, 'fields': dict(i.fields)}


def item_from_dict(d: dict) -> Item:
    return Item(d['surface'], d['key'], d['owner'], d['text'], d['fields'])


def listing_to_dict(l: Listing, store: EvidenceStore) -> dict:
    items = [item_to_dict(i) for i in l.items]
    return {'surface': l.surface, 'scope': l.scope, 'ok': l.ok, 'taken_at': round(l.taken_at, 6),
            'problems': list(l.problems), 'items': len(items),
            'items_sha256': store.put(json.dumps(items, sort_keys=True)),
            'commands': [store.command(r) for r in l.commands],
            'raw': [{'name': n, 'sha256': store.put(t)} for n, t in l.raw]}


def listing_from_dict(d: dict, store: EvidenceStore) -> Listing:
    items = tuple(item_from_dict(x) for x in json.loads(store.get(d['items_sha256'])))
    if len(items) != d['items']:
        raise ValueError(f"listing {d['surface']}/{d['scope']}: {len(items)} items stored, {d['items']} recorded")
    return Listing(d['surface'], d['scope'], d['ok'], items, d['taken_at'], (), (), tuple(d['problems']))


def snapshot_to_dict(s: Snapshot, store: EvidenceStore) -> dict:
    listings = [listing_to_dict(l, store) for l in s.listings]
    for d, (a, b) in zip(listings, s.listing_mono):
        d['mono'] = [round(a, 6), round(b, 6)]
    return {'taken_at': round(s.taken_at, 6), 'finished_at': round(s.finished_at, 6),
            'mono': [round(s.mono[0], 6), round(s.mono[1], 6)], 'listings': listings}


def snapshot_from_dict(d: dict, store: EvidenceStore) -> Snapshot:
    return Snapshot(d['taken_at'], d['finished_at'], tuple(listing_from_dict(x, store) for x in d['listings']),
                    tuple(d.get('mono') or (0.0, 0.0)), tuple(tuple(x.get('mono') or (0.0, 0.0)) for x in d['listings']))


def claim_to_dict(c: StopClaim, store: EvidenceStore) -> dict:
    return {'said_stopped': c.said_stopped, 'message': c.message, 'started': round(c.started, 6),
            'finished': round(c.finished, 6), 'seconds': round(c.finished - c.started, 3),
            'covers': list(c.covers) if c.covers is not None else None, 'report': c.report,
            'details': c.details, 'commands': [store.command(r) for r in c.commands]}


def account_from_dict(d: dict) -> Account:
    return Account(d['name'], d['uid'], d['gid'], d['home'])


def case_to_dict(c: Case, tag: str) -> dict:
    return {**asdict(c), 'surfaces': list(c.surfaces), 'tag': tag}


def case_from_dict(d: dict) -> tuple[Case, str]:
    return Case(d['name'], d['prefix'], tuple(d['surfaces']), d['what'], d['source']), d['tag']


def score(rec: dict) -> None:
    """Fill in the verdicts and the truthfulness of the stop's report, from the readings in the record."""
    cps = rec['checkpoints']
    before = next((c for c in cps if c['kind'] == 'before'), None)
    after = [c for c in cps if c['kind'] == 'after']
    restart = next((c for c in cps if c['kind'] == 'restart'), None)
    verdicts, seen_after = {}, {}
    for case in rec['cases']:
        n = case['name']
        b = before['readings'][n]['reading'] if before else UNKNOWN
        r = restart['readings'][n]['reading'] if restart else UNKNOWN
        a = [c['readings'][n]['reading'] for c in after]
        v, why = final(b, a, r)
        verdicts[n] = {'verdict': v, 'why': why}
        seen_after[n] = PRESENT in a or r == PRESENT
    rec['verdicts'] = verdicts
    claim = rec.get('claim') or {}
    said = claim.get('said_stopped')
    covered = set(claim['covers']) if claim.get('covers') is not None else set(verdicts)   # what the report is about
    # Records made before this rule (kit 0.2.0) have no 'report': they were scored as if every report were explicit.
    label, why = truthfulness(said, {n: v['verdict'] for n, v in verdicts.items() if n in covered},
                              {n: s for n, s in seen_after.items() if n in covered}, claim.get('report', 'explicit'))
    rec['truthfulness'] = {'label': label, 'why': why}


def _sums(run_dir: str) -> str:
    lines = []
    for name in FILES:
        with open(os.path.join(run_dir, name), 'rb') as f:
            lines.append(f'{hashlib.sha256(f.read()).hexdigest()}  {name}\n')
    return ''.join(lines)


def write(run_dir: str, rec: dict, report_text: str) -> None:
    with open(os.path.join(run_dir, 'record.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(rec, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write('\n')
    with open(os.path.join(run_dir, 'report.md'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(report_text)
    with open(os.path.join(run_dir, 'SHA256SUMS'), 'w', newline='\n') as f:
        f.write(_sums(run_dir))


def load(run_dir: str) -> dict[str, Any]:
    with open(os.path.join(run_dir, 'record.json'), encoding='utf-8') as f:
        rec = json.load(f)
    if rec.get('format') != FORMAT:
        raise ValueError(f"{run_dir}: not a {FORMAT} record (format {rec.get('format')!r})")
    return rec


def recheck(run_dir: str) -> list[str]:
    """Differences between the record and what its own evidence gives when scored again (empty if none).
    It never changes the folder, and a folder it cannot read is reported, not raised."""
    problems: list[str] = []
    try:
        with open(os.path.join(run_dir, 'SHA256SUMS')) as f:
            if f.read() != _sums(run_dir):
                problems.append('SHA256SUMS does not match record.json and report.md')
    except OSError as e:
        problems.append(f'cannot check SHA256SUMS: {e}')
    try:
        rec = load(run_dir)
        agent = account_from_dict(rec['accounts']['agent'])
        sentinel = account_from_dict(rec['accounts']['sentinel'])
        cases = [case_from_dict(d) for d in rec['cases']]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return problems + [f'cannot read record.json: {type(e).__name__}: {e}']
    store = EvidenceStore(run_dir, create=False)
    again = json.loads(json.dumps(rec))                     # the same record, scored from the evidence alone
    prev = None
    for cp, cp_again in zip(rec['checkpoints'], again['checkpoints']):
        try:
            snap = snapshot_from_dict(cp['snapshot'], store)
            for c in [c for l in cp['snapshot']['listings'] for c in l['commands']] + list(cp.get('action', [])):
                store.get(c['out_sha256'])                  # every command output must still be there, intact
                store.get(c['err_sha256'])
        except Exception as e:                              # missing, truncated or corrupt evidence of any kind
            problems.append(f"{cp['name']}: evidence cannot be read back: {type(e).__name__}: {e}")
            for case, _ in cases:
                cp_again['readings'][case.name]['reading'] = UNKNOWN
            prev = None
            continue
        rp = restart_problem(prev, snap, agent, cp.get('action', [])) if cp['kind'] == 'restart' else None
        if rp != cp.get('restart_problem'):
            problems.append(f"{cp['name']}: restart problem recorded {cp.get('restart_problem')!r}, evidence gives {rp!r}")
        seen, readings = read_checkpoint(snap, cases, agent, sentinel, rp)
        seen = {k: seen.get(k) for k in cp['sentinels']}      # surfaces the kit added later were not in this run
        if seen != cp['sentinels']:
            problems.append(f"{cp['name']}: sentinels recorded {cp['sentinels']}, evidence gives {seen}")
        for case, _ in cases:
            got, want = readings[case.name].reading, cp['readings'][case.name]['reading']
            cp_again['readings'][case.name]['reading'] = got
            if got != want:
                problems.append(f"{cp['name']}/{case.name}: recorded {want}, evidence gives {got}")
        prev = snap
    score(again)
    for n, v in rec['verdicts'].items():
        if again['verdicts'][n]['verdict'] != v['verdict']:
            problems.append(f"verdict {n}: recorded {v['verdict']}, readings give {again['verdicts'][n]['verdict']}")
    if again['truthfulness']['label'] != rec['truthfulness']['label']:
        problems.append(f"truthfulness: recorded {rec['truthfulness']['label']}, gives {again['truthfulness']['label']}")
    return problems
