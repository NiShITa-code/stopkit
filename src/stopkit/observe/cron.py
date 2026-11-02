"""Scheduled commands outside systemd: an account's crontab lines and the at queue."""
from __future__ import annotations

import hashlib
import re
import time

from ..system.cmd import Runner, run
from .model import Item, Listing, failed

ENV_LINE = re.compile(r'^\s*[A-Za-z_][A-Za-z0-9_]*\s*=')


def list_crontab(account: str, runner: Runner = run) -> Listing:
    """The lines of `account`'s crontab that can run: comments, blank lines and VAR=value lines are left out.
    "no crontab for <account>" is a successful, empty listing; any other failure proves nothing."""
    taken = time.time()
    r = runner(['crontab', '-l', '-u', account])
    if r.error:
        return failed('cron', account, taken, [f'crontab: {r.error}'], (r,))
    if r.rc == 1 and f'no crontab for {account}' in r.err:
        return Listing('cron', account, True, (), taken, (r,))
    if r.rc != 0 or r.truncated:
        return failed('cron', account, taken, [f'crontab: exit code {r.rc}: {r.err.strip()[:200]}'], (r,))
    items = []
    for line in r.out.splitlines():
        s = line.strip()
        if not s or s.startswith('#') or ENV_LINE.match(s):
            continue
        key = 'cron:' + hashlib.sha256(s.encode()).hexdigest()[:16]
        items.append(Item('cron', key, account, s, {'line': s}))
    return Listing('cron', account, True, tuple(items), taken, (r,))


def list_at_jobs(runner: Runner = run) -> Listing:
    """Every queued at job (run as root, atq lists every account's jobs), with each job's whole script,
    so that a tag anywhere in its commands is found."""
    taken = time.time()
    r = runner(['atq'])
    if not r.ok:
        return failed('at', 'all', taken, [f'atq: {r.error or "exit code " + str(r.rc)}: {r.err.strip()[:200]}'], (r,))
    commands, items, problems = [r], [], []
    for line in r.out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            problems.append(f'atq: could not read the line {line!r}')
            continue
        job, queue, user = parts[0], parts[-2], parts[-1]
        rc = runner(['at', '-c', job])
        commands.append(rc)
        if not rc.ok:
            # The job may have run or been removed between atq and at -c. Ask again: if it has left the
            # queue it is simply not there any more; if it is still queued, this reading is incomplete.
            again = runner(['atq'])
            commands.append(again)
            still = again.ok and any(l.split()[:1] == [job] for l in again.out.splitlines())
            if again.ok and not still:
                continue
            problems.append(f'at -c {job}: {rc.error or "exit code " + str(rc.rc)}')
            continue
        items.append(Item('at', f'at:{job}', user, rc.out.rstrip(), {'job': job, 'queue': queue, 'user': user,
                                                                   'when': ' '.join(parts[1:-2])}))
    if problems:
        return failed('at', 'all', taken, problems, commands)
    return Listing('at', 'all', True, tuple(items), taken, tuple(commands))
