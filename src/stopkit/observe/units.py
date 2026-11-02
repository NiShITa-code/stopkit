"""User units of an account: every loaded unit, including temporary ones, and every unit file.

A check that counts only units whose `is-enabled` says "enabled" misses a temporary unit started with
`systemd-run --user`, which is never enabled. This lister keeps every unit
the account's service manager has loaded (`list-units --all`) and every unit file it knows
(`list-unit-files`), whatever their state. For loaded units it also asks the manager (`show`) for each
unit's own enablement and for the units that trigger it: `list-unit-files` lists a template once (as
"indirect"), never its enabled instances, and a timer can start an untagged-looking service.

How the account's service manager is reached. systemctl runs as the account itself and talks only to the
manager's own socket under /run/user/<uid>. Measured in WSL on 26 Oct 2026: the earlier form,
`systemctl --user -M <account>@` run as root, opens a login session for the account, and when its manager
had been killed, that one listing started it again, together with the account's enabled services. An
observer must not do that. Run as the account, systemctl fails with "Failed to connect to bus" while the
manager is down; the listing is then reported as failed (unknown), and nothing is started.
"""
from __future__ import annotations

import json
import time
from typing import Mapping

from ..system.accounts import Account, as_account_argv
from ..system.cmd import CmdResult, Runner, run
from .model import Item, Listing, failed

NO_MANAGER = ('Failed to connect to bus', 'No such file or directory', 'Transport endpoint is not connected',
              'Host is down')
ACTIVE = frozenset({'active', 'activating', 'deactivating', 'reloading', 'refreshing'})
ENABLED = frozenset({'enabled', 'enabled-runtime'})
UNSURE = frozenset({'generated', 'indirect', 'alias'})   # may run again; the listing alone cannot tell
SHOW_PROPS = 'Id,UnitFileState,TriggeredBy,FragmentPath,Transient'


def user_manager_argv(account: Account) -> list[str]:
    """systemctl, run as `account`, talking to that account's running service manager."""
    return as_account_argv(account, ['systemctl', '--user', '--no-pager'])


def json_rows(r: CmdResult, what: str, problems: list[str]) -> list[dict]:
    """The JSON array a systemctl -o json command printed, or [] with a problem noted."""
    if not r.ok:
        why = r.error or f'exit code {r.rc}'
        if any(s in r.err for s in NO_MANAGER):
            why += ' (the account\'s service manager is not running, or cannot be reached)'
        problems.append(f'{what}: {why}: {r.err.strip()[:200]}')
        return []
    try:
        rows = json.loads(r.out)
    except ValueError as e:
        problems.append(f'{what}: output is not JSON: {e}')
        return []
    if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
        problems.append(f'{what}: output is not a JSON array of objects')
        return []
    return rows


def parse_show(text: str) -> list[dict[str, str]]:
    """Blocks of `systemctl show` output (Key=Value lines, blocks separated by blank lines)."""
    blocks, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        key, sep, value = line.partition('=')
        if sep:
            cur[key] = value
    if cur:
        blocks.append(cur)
    return blocks


def unit_present(active: str | None, file_state: str | None) -> bool:
    """A unit counts as present if it is running, starting or stopping, or if it is enabled, because an
    enabled unit runs again when its manager next starts. A disabled, stopped unit is not present, even if
    its file remains."""
    return active in ACTIVE or file_state in ENABLED


def unit_in_force(name: str, by_name: Mapping[str, Mapping], _seen: frozenset = frozenset()) -> bool | None:
    """True if the unit is present (unit_present), or is started by a unit that is in force (a timer,
    path or socket); None if it may run again but the listing cannot tell (a generated, indirect or alias
    unit file, or a trigger that is not in the listing); False otherwise."""
    f = by_name.get(name)
    if f is None:
        return None
    if unit_present(f.get('active'), f.get('file_state')):
        return True
    result: bool | None = None if f.get('file_state') in UNSURE else False
    for t in f.get('triggered_by') or ():
        if t in _seen or t == name:
            continue
        r = unit_in_force(t, by_name, _seen | {name})
        if r:
            return True
        if r is None:
            result = None
    return result


def list_user_units(account: Account, runner: Runner = run) -> Listing:
    taken = time.time()
    base = user_manager_argv(account)
    r_units = runner(base + ['list-units', '--all', '-o', 'json'])
    r_files = runner(base + ['list-unit-files', '-o', 'json'])
    commands = [r_units, r_files]
    problems: list[str] = []
    units = json_rows(r_units, 'list-units', problems)
    files = json_rows(r_files, 'list-unit-files', problems)
    bad = ([u for u in units if not isinstance(u.get('unit'), str) or 'active' not in u] +
           [f for f in files if not isinstance(f.get('unit_file'), str) or 'state' not in f])
    if bad:
        problems.append(f'{len(bad)} rows lack the expected fields, for example {bad[0]!r}')
    shown: dict[str, dict] = {}
    if units and not problems:
        names = sorted({u['unit'] for u in units})
        r_show = runner(base + ['show', '-p', SHOW_PROPS, '--', *names], timeout=60)
        commands.append(r_show)
        if not r_show.ok:
            problems.append(f'show: {r_show.error or "exit code " + str(r_show.rc)}: {r_show.err.strip()[:200]}')
        shown = {b['Id']: b for b in parse_show(r_show.out) if 'Id' in b}
        missing = [n for n in names if n not in shown]
        if r_show.ok and missing:
            problems.append(f'show did not describe {len(missing)} loaded units, for example {missing[0]}')
    if problems:
        return failed('unit', account.name, taken, problems, commands)
    loaded = {u['unit']: u for u in units}
    file_state = {f['unit_file']: f['state'] for f in files}
    items = []
    for name in sorted(set(loaded) | set(file_state)):
        u, s = loaded.get(name, {}), shown.get(name, {})
        items.append(Item('unit', name, account.name, f"{name} {u.get('description', '')}".strip(),
                          {'load': u.get('load'), 'active': u.get('active'), 'sub': u.get('sub'),
                           'file_state': (s.get('UnitFileState') or None) if name in loaded else file_state.get(name),
                           'triggered_by': (s.get('TriggeredBy') or '').split(), 'loaded': name in loaded,
                           'fragment': s.get('FragmentPath') or None, 'transient': s.get('Transient') == 'yes'}))
    return Listing('unit', account.name, True, tuple(items), taken, tuple(commands))
