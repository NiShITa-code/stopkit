"""A platform's own processes, as the systemd units the binding runs them in (spec/surfaces.json, component).

A binding starts each component of the platform under test (n8n's main process and its worker, a LangGraph
server, a Temporal server and worker) as a system unit that runs as the agent account and is named by the
run's tag for it, so a fresh listing finds it without trusting any earlier record. The system's manager is
asked about them as root, which changes nothing about them. The same listing shows the kit's worker
stand-in, which no stop under test touches: seen active, it proves that the listing worked. Each unit's
invocation ID shows whether a restart action really started it again.
"""
from __future__ import annotations

import time
from typing import Sequence

from ..system.cmd import Runner, run
from ..system.names import WORKER_UNIT
from .model import Item, Listing, failed
from .units import parse_show

PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'Result', 'MainPID', 'InvocationID', 'NRestarts')
RUNNING = ('active', 'activating', 'deactivating', 'reloading', 'refreshing')
SENTINEL_KEY = 'stopkit-sentinel-component'


def tracked(tag: str, pid: int, start: int, owner: str | None, alive: bool, runner: Runner = run,
            sentinel_unit: str = WORKER_UNIT) -> Listing:
    """The component surface for a platform process that is not a unit of its own (a server started in a scope
    from a terminal, as a person starts it): the process the binding started, followed by its identity (its ID
    and start time, read by the caller). Its 'invocation' is that identity, so a restart action that starts it
    again shows a new one. The same command as list_components reads the kit's worker stand-in for the proof."""
    base = list_components([], runner, sentinel_unit)
    if not base.ok:
        return base
    item = Item('component', tag, owner, f'{tag} pid {pid}', {'pid': pid, 'start': start, 'in_force': alive,
                                                             'ActiveState': 'active' if alive else 'inactive',
                                                             'InvocationID': f'{pid}:{start}'})
    return Listing('component', 'all', True, base.items + (item,), base.taken_at, base.commands)


def list_components(units: Sequence[str], runner: Runner = run, sentinel_unit: str = WORKER_UNIT) -> Listing:
    taken = time.time()
    names = list(units) + [sentinel_unit]
    r = runner(['systemctl', 'show', '--no-pager', '-p', ','.join(PROPERTIES), '--'] + names)
    if not r.ok:
        return failed('component', 'all', taken, [f'systemctl show: {r.error or "exit code " + str(r.rc)}'], (r,))
    blocks = parse_show(r.out)
    if len(blocks) != len(names) or any(b.get('Id') != n for b, n in zip(blocks, names)):
        return failed('component', 'all', taken, ['systemctl show: one block per unit, in order, was expected'], (r,))
    items = []
    for name, b in zip(names, blocks):
        if name == sentinel_unit:
            if b.get('ActiveState') == 'active':
                items.append(Item('component', SENTINEL_KEY, 'system', f'{name} active', {'proof': True, **b}))
            continue
        items.append(Item('component', name, None, name, {**b, 'in_force': b.get('ActiveState') in RUNNING}))
    return Listing('component', 'all', True, tuple(items), taken, (r,))
