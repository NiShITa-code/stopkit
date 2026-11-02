"""Processes of given accounts, read from the kernel's process table in /proc."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Mapping

from ..system import procfs
from .model import Item, Listing, failed

TAG = re.compile(r'stopkit-[A-Za-z0-9-]+')


def command_text(args: list[str]) -> str:
    """The command line with likely secrets masked (procfs.redact). The kit's tags are random nonces, never
    secrets, so any tag that the masking or the length limit removed is added back at the end: a tag on a
    command line is always visible to the kit. (Without this, `--name task-<tag>` loses its tag, because
    the masking takes 'sk-...' for an API key.)"""
    text = procfs.redact(args)
    lost = [t for t in dict.fromkeys(TAG.findall(' '.join(args))) if t not in text]
    return text + (' [tags: ' + ' '.join(lost) + ']' if lost else '')


def list_processes(accounts: Mapping[int, str], proc: str = '/proc',
                   mounts: str = '/proc/self/mounts') -> Listing:
    """Every process owned by the given accounts ({uid: name}).

    Not ok if the table cannot be read, if any entry exists but cannot be read (it could be one of ours),
    or if /proc hides other users' processes from a non-root caller."""
    scope = ','.join(sorted(accounts.values())) or '(none)'
    taken = time.time()
    problems: list[str] = []
    try:
        table = procfs.read_table(proc, set(accounts), problems)
    except procfs.TableError as e:
        return failed('process', scope, taken, [str(e)])
    hidden = procfs.proc_hides_others(proc, mounts)
    if hidden and os.geteuid() != 0:
        problems.append(f'{proc} is mounted with {hidden}, so other users\' processes are hidden; run as root')
    items = []
    for pid, p in sorted(table.items()):
        # A zombie has ended, unless other threads of it are still running (its main thread exited first).
        running = p['state'] not in ('Z', 'X')
        args = p['args']
        if not running:
            try:
                running = procfs.live_threads(proc, pid) > 0
            except procfs.ReadError as e:
                problems.append(str(e))
                running = True                 # cannot tell, so never counted as ended
            if running and not args:
                args = procfs.thread_args(proc, pid)
        command = command_text(args) or f"[{p['comm']}]"
        items.append(Item('process', f"{pid}:{p['start']}", accounts.get(p['uid'], f"uid:{p['uid']}"), command,
                          {'pid': pid, 'start': p['start'], 'ppid': p['ppid'], 'pgrp': p['pgrp'],
                           'session': p['session'], 'uid': p['uid'], 'state': p['state'], 'comm': p['comm'],
                           'cgroup': p['cgroup'], 'command': command, 'running': running}))
    raw = json.dumps([dict(i.fields) for i in items], sort_keys=True)
    return Listing('process', scope, not problems, tuple(items), taken, (), (('proc-table', raw),), tuple(problems))
