"""Timers: work scheduled to start later, for one account's service manager or for the whole system.

A timer shows no activity until it fires, so a check that watches only what runs never finds one: only
a listing can.
"""
from __future__ import annotations

import time

from ..system.accounts import Account
from ..system.cmd import Runner, run
from .model import Item, Listing, failed
from .units import json_rows, user_manager_argv


def list_timers(account: Account | None, runner: Runner = run) -> Listing:
    """Timers of `account`'s service manager, or the system's timers if account is None."""
    scope = account.name if account else 'system'
    taken = time.time()
    argv = (user_manager_argv(account) if account else ['systemctl', '--no-pager']) + ['list-timers', '--all', '-o', 'json']
    r = runner(argv)
    problems: list[str] = []
    rows = json_rows(r, 'list-timers', problems)
    if problems:
        return failed('timer', scope, taken, problems, (r,))
    items = []
    for t in rows:
        unit = t.get('unit')
        if not unit:
            return failed('timer', scope, taken, [f'list-timers: a row has no unit name: {t!r}'], (r,))
        items.append(Item('timer', unit, scope, f"{unit} {t.get('activates') or ''}".strip(),
                          {'activates': t.get('activates'), 'next_us': t.get('next'), 'last_us': t.get('last')}))
    return Listing('timer', scope, True, tuple(items), taken, (r,))
