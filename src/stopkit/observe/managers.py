"""The state of an account's service manager (user@<uid>.service), as the system's manager sees it.

Asking the system's manager has no side effect on the account. The reading explains why the account's own
unit and timer listings fail when a stop has killed its manager, and shows that the restart checkpoint
really started a new manager (its InvocationID changes).
"""
from __future__ import annotations

import time

from ..system.accounts import Account
from ..system.cmd import Runner, run
from .model import Item, Listing, failed
from .units import parse_show

PROPERTIES = ('ActiveState', 'SubState', 'Result', 'MainPID', 'InvocationID', 'NRestarts')


def list_manager(account: Account, runner: Runner = run) -> Listing:
    taken = time.time()
    unit = f'user@{account.uid}.service'
    r = runner(['systemctl', 'show', unit, '--no-pager', '-p', ','.join(PROPERTIES)])
    if not r.ok:
        return failed('manager', account.name, taken, [f'systemctl show {unit}: {r.error or "exit code " + str(r.rc)}'], (r,))
    blocks = parse_show(r.out)
    if len(blocks) != 1 or 'ActiveState' not in blocks[0]:
        return failed('manager', account.name, taken, [f'systemctl show {unit}: unexpected output'], (r,))
    s = blocks[0]
    return Listing('manager', account.name, True,
                   (Item('manager', unit, account.name, f"{unit} {s.get('ActiveState')}/{s.get('SubState')}", dict(s)),),
                   taken, (r,))
