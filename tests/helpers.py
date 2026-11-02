"""Test helpers: a fake command runner, a fake /proc tree, and hand-made snapshots."""
from __future__ import annotations

import os
import tempfile

from stopkit.system.accounts import Account
from stopkit.system.cmd import CmdResult
from stopkit.system.names import SENTINEL_TAGS as ST
from stopkit.observe.model import Item, Listing
from stopkit.observe.snapshot import Snapshot

AGENT = Account('stopkit-agent', 1001, 1002, '/home/stopkit-agent')
SENTINEL = Account('stopkit-sentinel', 1002, 1003, '/home/stopkit-sentinel')
PROC_SCOPE = 'stopkit-agent,stopkit-sentinel'


def proc_item(text: str, owner: str = AGENT.name, running: bool = True, pid: int = 10) -> Item:
    return Item('process', f'{pid}:1', owner, text, {'pid': pid, 'state': 'S' if running else 'Z', 'running': running})


def unit_item(name: str, owner: str = AGENT.name, active: str | None = 'active', file_state: str | None = 'enabled') -> Item:
    return Item('unit', name, owner, name, {'active': active, 'file_state': file_state, 'loaded': active is not None})


def sentinel_items() -> dict[tuple[str, str], list[Item]]:
    s = SENTINEL.name
    return {
        ('process', PROC_SCOPE): [proc_item(f"{ST['process']} infinity", s, pid=500)],
        ('unit', AGENT.name): [unit_item('default.target', AGENT.name, 'active', 'static')],
        ('unit', s): [unit_item('default.target', s, 'active', 'static'), unit_item(f"{ST['unit']}.service", s)],
        ('timer', AGENT.name): [],
        ('timer', s): [Item('timer', f"{ST['timer']}.timer", s, f"{ST['timer']}.timer", {})],
        ('cron', AGENT.name): [],
        ('cron', s): [Item('cron', 'cron:1', s, f"0 0 1 1 * /bin/true {ST['cron']}", {})],
        ('at', 'all'): [Item('at', 'at:1', s, f"/bin/true {ST['at']}", {'job': '1'})],
        ('listener', 'all'): [Item('listener', 'tcp:127.0.0.1:4000', s, f"python3 -c ... {ST['listener']}", {})],
        ('worker', 'all'): [Item('worker', 'job:1', s, ST['worker'], {'id': 1, 'uid': SENTINEL.uid, 'tag': ST['worker'],
                                                                     'pid': 700, 'status': 'running'})],
    }


def manager_item(owner: str, uid: int, invocation: str = 'inv-1', active: str = 'active') -> Item:
    return Item('manager', f'user@{uid}.service', owner, f'user@{uid}.service {active}/running',
                {'ActiveState': active, 'SubState': 'running', 'InvocationID': invocation, 'MainPID': '300'})


def snap(add=None, drop=(), fail=(), invocation: str = 'inv-1', manager: str = 'active') -> Snapshot:
    """A snapshot where every listing succeeded and shows the sentinel's item, then changed: `add` maps
    (surface, scope) to extra items; `drop` lists surfaces whose sentinel item is missing; `fail` lists
    (surface, scope) whose listing failed. The agent's service manager has the given invocation ID and state."""
    listings = []
    items_by_key = sentinel_items()
    items_by_key[('manager', AGENT.name)] = [manager_item(AGENT.name, AGENT.uid, invocation, manager)]
    items_by_key[('manager', SENTINEL.name)] = [manager_item(SENTINEL.name, SENTINEL.uid)]
    for key, items in items_by_key.items():
        surface, scope = key
        if surface in drop:
            items = [i for i in items if not any(t in i.key or t in i.text for t in ST.values())]
        items = items + list((add or {}).get(key, []))
        if key in fail:
            listings.append(Listing(surface, scope, False, (), 0.0, (), (), (f'{surface} listing failed',)))
        else:
            listings.append(Listing(surface, scope, True, tuple(items), 0.0))
    return Snapshot(0.0, 0.0, tuple(listings))


def result(out: str = '', rc: int | None = 0, err: str = '', error: str | None = None, argv=('fake',)) -> CmdResult:
    return CmdResult(tuple(argv), rc, out, err, 0.0, 0.0, error)


class FakeRunner:
    """Answers commands from a table {argv tuple: CmdResult, list of CmdResults (one per call), or a
    function(argv) -> CmdResult}. Unknown commands fail as 'command not found', and every call is recorded."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, **_kw) -> CmdResult:
        argv = tuple(argv)
        self.calls.append(argv)
        answer = self.table.get(argv)
        if answer is None:
            return result(rc=None, error=f'command not found: {argv[0]}', argv=argv)
        if callable(answer):
            return answer(argv)
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    def count(self, argv) -> int:
        return self.calls.count(tuple(argv))


def stat_line(pid: int, comm: str, ppid: int = 1, start: int = 1000, state: str = 'S', flags: int = 0,
              pgrp: int | None = None, session: int | None = None) -> str:
    pgrp = pid if pgrp is None else pgrp
    session = pid if session is None else session
    return (f'{pid} ({comm}) {state} {ppid} {pgrp} {session} 0 -1 {flags} 0 0 0 0 0 0 0 0 20 0 1 0 {start} 0 0\n')


class FakeProc:
    """A temporary directory laid out like /proc, for the procfs and process-listing tests."""

    def __init__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = self._dir.name
        with open(os.path.join(self.root, 'stat'), 'w') as f:
            f.write('cpu 0 0 0 0\nbtime 1700000000\n')
        self.mounts = os.path.join(self.root, 'mounts')
        with open(self.mounts, 'w') as f:
            f.write(f'proc {self.root} proc rw,nosuid 0 0\n')

    def add(self, pid: int, comm: str, uid: int, args: list[str], ppid: int = 1, start: int = 1000,
            cgroup: str = '/user.slice', state: str = 'S', flags: int = 0, status: bool = True):
        d = os.path.join(self.root, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'stat'), 'w') as f:
            f.write(stat_line(pid, comm, ppid, start, state, flags))
        if status:
            with open(os.path.join(d, 'status'), 'w') as f:
                f.write(f'Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        with open(os.path.join(d, 'cgroup'), 'w') as f:
            f.write(f'0::{cgroup}\n')
        with open(os.path.join(d, 'cmdline'), 'wb') as f:
            f.write(b'\0'.join(a.encode() for a in args) + b'\0')

    def close(self):
        self._dir.cleanup()
