"""Watch mode (DESIGN.md section 2): a person stops an agent however they like, and the kit lists, before and
after, what the agent's account still has running or scheduled. Nothing is planted.

  stopkit watch baseline --account U    optional, before the agent starts: what the account normally holds
  stopkit watch before --account U      just before the stop (or: watch before FOLDER, after a baseline)
  ... the person stops the agent their own way ...
  stopkit watch after FOLDER            any number of times
  stopkit watch restart FOLDER --yes    restarts U's service manager (this stops U's services), then lists
  stopkit watch report FOLDER           the score sheet

With no tags, each thing is followed by its identity in the listings: a process by its ID and start time, a
unit by its name, a crontab line by its text, an at job by its number, a listening socket by its address.
The same proofs as in a test run apply: the sentinel account's items, and the account's own default.target.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict

from .. import __version__
from ..system import accounts, procfs
from ..report import record
from ..observe import sentinel as sentinel_mod
from ..system.accounts import Account
from ..system.cmd import Runner, run
from ..system.names import SENTINEL
from ..system import KitError
from ..system.evidence import EvidenceStore
from ..observe import snapshot
from ..observe.model import Item
from ..observe.units import unit_in_force
from .runner import host_info, new_id
from ..stops import MANAGER_OWN, made_by_system
from ..score.verdict import ABSENT, PRESENT, UNKNOWN, final, listing_for, proof, restart_problem

FORMAT = 'stopkit-check/1'
KINDS = ('baseline', 'before', 'after', 'restart')


def _own_unit(u: Item, account: Account) -> bool:
    if u.key in MANAGER_OWN:
        return False
    frag = u.fields.get('fragment')
    return bool(u.fields.get('transient')) or bool(frag and not made_by_system(frag, account))


def account_items(snap, account: Account) -> dict[str, Item]:
    """What the account has in force in this snapshot, by 'surface|key': its running processes other than its
    service manager (told apart by the manager's process ID in the same snapshot), its own units that are in
    force or may run again, its crontab lines, its at jobs and the sockets it holds."""
    out: dict[str, Item] = {}
    mgr = listing_for(snap, 'manager', account)
    main = int(mgr.items[0].fields.get('MainPID') or 0) if mgr is not None and mgr.ok and mgr.items else 0
    pl = snap.listing('process')
    for i in pl.items if pl is not None else ():
        f = i.fields
        if i.owner != account.name or not f.get('running', True):
            continue
        if main and (f.get('pid') == main or (f.get('ppid') == main and f.get('comm') == '(sd-pam)')):
            continue
        out[f'process|{i.key}'] = i
    ul = listing_for(snap, 'unit', account)
    if ul is not None:
        by_name = {u.key: u.fields for u in ul.items}
        for u in ul.items:
            if _own_unit(u, account) and unit_in_force(u.key, by_name) is not False:
                out[f'unit|{u.key}'] = u
    for surface in ('cron', 'at', 'listener'):
        l = listing_for(snap, surface, account)
        for i in l.items if l is not None else ():
            if surface == 'cron' or account.name in (i.owner or '').split(','):
                out[f'{surface}|{i.key}'] = i
    return out


def describe(item: Item) -> str:
    """One line saying what a followed thing is: an at job by its command (the last line of its script), a
    listening socket by its address and holder, anything else by its listed text."""
    if item.surface == 'at':
        lines = [l for l in item.text.splitlines() if l.strip()]
        text = f"job {item.fields.get('job')}: {lines[-1] if lines else ''}"
    elif item.surface == 'listener':
        text = f'{item.key} held by {item.text}'
    else:
        text = item.text
    return ' '.join(text.split())[:200]


def read_item(snap, surface: str, key: str, account: Account, seen) -> tuple[str, str]:
    """present / absent / unknown for one followed thing, with the reason."""
    l = listing_for(snap, surface, account)
    hit = next((i for i in l.items if i.key == key), None) if l is not None else None
    if hit is not None:
        if surface == 'process':
            state: bool | None = bool(hit.fields.get('running', True))
        elif surface == 'unit':
            state = unit_in_force(key, {u.key: u.fields for u in l.items})
        else:
            state = surface != 'listener' or account.name in (hit.owner or '').split(',')
        if state:
            return PRESENT, 'still listed and in force'
        if state is None:
            return UNKNOWN, f"listed, but the listing cannot tell whether it will run again ({hit.fields.get('file_state')})"
    why = proof(snap, surface, account, seen)
    if why:
        return UNKNOWN, why
    return ABSENT, 'not listed in force' if hit is None else 'listed, but stopped and disabled'


def score(doc: dict, store: EvidenceStore) -> dict:
    """The results of a check folder, computed from its snapshots alone (the same function serves
    `report` and `recheck`)."""
    account, sent = record.account_from_dict(doc['account']), record.account_from_dict(doc['sentinel'])
    cps = doc['checkpoints']
    snaps = [record.snapshot_from_dict(c['snapshot'], store) for c in cps]
    problems = {}                                # each restart is confirmed again from the evidence
    for i, (c, s) in enumerate(zip(cps, snaps)):
        if c['kind'] == 'restart':
            problems[c['name']] = restart_problem(snaps[i - 1] if i else None, s, account, c['action'])
    by_kind = lambda k: [(c, s) for c, s in zip(cps, snaps) if c['kind'] == k]
    before = by_kind('before')
    if not before:
        raise KitError('this check has no "before" listing yet; run `stopkit watch before` first')
    b_cp, b_snap = before[-1]
    tracked = account_items(b_snap, account)
    baseline = by_kind('baseline')
    if baseline:
        normal = account_items(baseline[-1][1], account)
        tracked = {k: v for k, v in tracked.items() if k not in normal}
    afters = [(c, s) for c, s in zip(cps, snaps) if c['kind'] in ('after', 'restart')
              and c['taken_at'] > b_cp['taken_at']]
    results = []
    for k, item in sorted(tracked.items()):
        surface, key = k.split('|', 1)
        readings = {}
        for c, s in afters:
            if c['kind'] == 'restart' and problems.get(c['name']):
                readings[c['name']] = (UNKNOWN, f"the restart is not confirmed: {problems[c['name']]}")
                continue
            r = read_item(s, surface, key, account, sentinel_mod.seen(s, sent))
            if r[0] == ABSENT and c.get('system_start') != b_cp.get('system_start'):
                # The whole system started again since the 'before' listing (in WSL it powers off when no
                # window is open): that alone ends every process, so an absence here says nothing about the stop.
                r = (UNKNOWN, "the system was restarted after the 'before' listing, so this absence may be the "
                              "restart's doing, not the stop's")
            readings[c['name']] = r
        after = [readings[c['name']][0] for c, _ in afters if c['kind'] == 'after']
        restart = [readings[c['name']][0] for c, _ in afters if c['kind'] == 'restart']
        verdict, why = final(PRESENT, after, restart[-1] if restart else None)
        results.append({'surface': surface, 'key': key, 'text': describe(item), 'verdict': verdict, 'why': why,
                        'readings': {n: {'reading': r, 'why': w} for n, (r, w) in readings.items()}})
    seen_keys = set(account_items(b_snap, account))
    new = []
    for c, s in afters:
        for k, item in sorted(account_items(s, account).items()):
            if k not in seen_keys:
                seen_keys.add(k)
                surface, key = k.split('|', 1)
                new.append({'surface': surface, 'key': key, 'text': describe(item), 'first_seen': c['name']})
    return {'results': results, 'new': new, 'baseline': bool(baseline), 'restart_problems': problems}


# ---- the commands --------------------------------------------------------------------------------------

def _load(folder: str) -> tuple[dict, EvidenceStore]:
    with open(os.path.join(folder, 'check.json'), encoding='utf-8') as f:
        doc = json.load(f)
    if doc.get('format') != FORMAT:
        raise KitError(f'{folder} is not a check folder')
    return doc, EvidenceStore(folder)


def _save(folder: str, doc: dict) -> None:
    tmp = os.path.join(folder, 'check.json.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write('\n')
    os.replace(tmp, os.path.join(folder, 'check.json'))


def _account(name: str) -> Account:
    a = accounts.lookup(name)
    if a is None:
        raise KitError(f'there is no account {name!r}')
    if a.uid == 0:
        raise KitError('check mode follows one non-root account; an agent running as root can change anything')
    return a


def new_check(account_name: str, out_root: str, runner: Runner = run) -> str:
    account, sent = _account(account_name), accounts.owned(SENTINEL)
    folder = os.path.join(os.path.abspath(out_root), f'check-{account.name}-{new_id()}')
    os.makedirs(folder)
    _save(folder, {'format': FORMAT, 'kit_version': __version__, 'account': asdict(account),
                   'sentinel': asdict(sent), 'host': host_info(runner), 'created': time.time(), 'checkpoints': []})
    return folder


def add(folder: str, kind: str, runner: Runner = run, proc: str = '/proc', restart: bool = False) -> dict:
    """Take one fresh listing of the account (and the sentinel, for the proofs) and add it to the folder."""
    doc, store = _load(folder)
    account, sent = record.account_from_dict(doc['account']), accounts.owned(SENTINEL)
    if accounts.manager_state(sent, runner) not in accounts.MANAGER_UP:
        accounts.start_manager(sent, runner)
    sentinel_mod.ensure(sent, runner, proc)
    action, prev = [], None
    if restart:
        if doc['checkpoints']:
            prev = record.snapshot_from_dict(doc['checkpoints'][-1]['snapshot'], store)
        action.append(runner(['systemctl', 'restart', account.manager_unit], timeout=120))
        try:
            accounts.wait_manager(account, runner)
        except KitError:
            pass                                 # recorded: the restart check below will not confirm it
        time.sleep(3)
    snap = snapshot.take([account, sent], runner, proc)
    actions = [store.command(r) for r in action]
    n = sum(1 for c in doc['checkpoints'] if c['kind'] == kind) + 1
    cp = {'name': kind if kind != 'after' else f'after {n}', 'kind': kind, 'taken_at': snap.taken_at,
          'boot_id': procfs.boot_id(), 'system_start': procfs.system_start(), 'snapshot': record.snapshot_to_dict(snap, store),
          'sentinels': sentinel_mod.seen(snap, sent), 'action': actions}
    if restart:
        cp['restart_problem'] = restart_problem(prev, snap, account, actions)
    doc['checkpoints'].append(cp)
    _save(folder, doc)
    return cp


def _times(doc: dict) -> str:
    """Each listing's start, in seconds after the 'before' listing started: by the monotonic clock when both
    ran in the same boot, otherwise by the wall clock (marked)."""
    before = next((c for c in doc['checkpoints'] if c['kind'] == 'before'), None)
    out = []
    for c in doc['checkpoints']:
        if before is None:
            out.append(c['name'])
            continue
        same_boot = c.get('boot_id') and c.get('boot_id') == before.get('boot_id')
        mono_c, mono_b = (c['snapshot'].get('mono') or [0])[0], (before['snapshot'].get('mono') or [0])[0]
        if same_boot and mono_c and mono_b:
            out.append(f"{c['name']} {mono_c - mono_b:+.1f}")
        else:
            out.append(f"{c['name']} {c['taken_at'] - before['taken_at']:+.1f} (wall clock)")
    return '; '.join(out)


def report(folder: str) -> str:
    doc, store = _load(folder)
    res = score(doc, store)
    doc['results'] = res
    _save(folder, doc)
    acc = doc['account']['name']
    lines = [f'# Check: what account `{acc}` still runs after a stop', '',
             f"- Listings taken (seconds from the start of the 'before' listing): {_times(doc)}. Kit "
             f"{doc['kit_version']}. Check mode does not see the stop itself, so it cannot time it.",
             '- ' + ("Only what was not there at the baseline counts as the agent's work." if res['baseline'] else
                     "No baseline was taken, so everything the account had in force before the stop is followed, "
                     "including anything it normally runs."), '',
             '## What the account had before the stop', '',
             '| Kind | What | Result | Why |', '|---|---|---|---|']
    for r in res['results']:
        lines.append(f"| {r['surface']} | {r['text'].replace('|', '/')[:90]} | **{r['verdict']}** | {r['why']} |")
    if not res['results']:
        lines.append('| | nothing | | |')
    lines += ['', '## New after the stop', '']
    lines += [f"- {n['surface']}: {n['text'][:120]} (first seen at {n['first_seen']})" for n in res['new']] or ['None.']
    unknown = sorted({w for r in res['results'] for v in r['readings'].values() if v['reading'] == UNKNOWN
                      for w in [v['why']]})
    if unknown:
        lines += ['', '## Why some readings are unknown', ''] + [f'- {w}' for w in unknown]
    lines += ['', '`python3 -B -m stopkit recheck <this folder>` scores it again from the stored listings.', '']
    text = '\n'.join(lines)
    with open(os.path.join(folder, 'report.md'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return text


def recheck(folder: str) -> list[str]:
    """Differences between the results stored in the folder and those its listings give again."""
    try:
        doc, store = _load(folder)
    except (OSError, ValueError, KitError) as e:
        return [f'cannot read check.json: {e}']
    if 'results' not in doc:
        return ['no report has been made for this check yet']
    try:
        again = score(doc, store)
    except Exception as e:                       # missing, truncated or corrupt evidence of any kind
        return [f'evidence cannot be read back: {type(e).__name__}: {e}']
    problems = []
    old = {(r['surface'], r['key']): r['verdict'] for r in doc['results']['results']}
    new = {(r['surface'], r['key']): r['verdict'] for r in again['results']}
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            problems.append(f'{k[0]} {k[1]}: recorded {old.get(k)}, the listings give {new.get(k)}')
    if [(n['surface'], n['key']) for n in doc['results']['new']] != [(n['surface'], n['key']) for n in again['new']]:
        problems.append('the list of things new after the stop differs')
    if doc['results'].get('restart_problems') != again['restart_problems']:
        problems.append(f"restart confirmation: recorded {doc['results'].get('restart_problems')}, "
                        f"the listings give {again['restart_problems']}")
    return problems
