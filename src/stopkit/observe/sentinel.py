"""The sentinel account's known items: one on every surface, so that each listing can prove it worked.

DESIGN.md section 4. No stop under test touches the sentinel account. At every checkpoint a listing counts
only if it shows the sentinel's item: a system-wide listing (processes, at queue, listeners) must show it
in the same listing; an account's own listing (units, timers, crontab) is proved by the same listing run
for the sentinel account at the same checkpoint.
"""
from __future__ import annotations

import os
import time

from ..system.accounts import Account, run_as
from ..system.cmd import Runner, run
from ..system.names import LISTENER_CODE, SENTINEL_TAGS as TAGS, WORKER_SOCKET, WORKER_SUBMIT
from ..system import KitError
from . import snapshot
from .model import CONTEXT

UNIT_DIR = '.config/systemd/user'
UNIT_FILES = {
    f"{TAGS['unit']}.service": (
        '[Unit]\nDescription=stopkit sentinel: a known service and process (never stopped by a test)\n'
        f"[Service]\nExecStart=/bin/bash -c 'exec -a {TAGS['process']} sleep infinity'\n"
        'Restart=always\nRestartSec=5\n[Install]\nWantedBy=default.target\n'),
    f"{TAGS['listener']}.service": (
        '[Unit]\nDescription=stopkit sentinel: a known listener on 127.0.0.1 (never stopped by a test)\n'
        f"[Service]\nExecStart=/usr/bin/python3 -c '{LISTENER_CODE}' {TAGS['listener']}\n"
        'Restart=always\nRestartSec=5\n[Install]\nWantedBy=default.target\n'),
    f"{TAGS['timer']}.timer": (
        '[Unit]\nDescription=stopkit sentinel: a known timer (never stopped by a test)\n'
        '[Timer]\nOnCalendar=*-01-01 00:00:00\n[Install]\nWantedBy=timers.target\n'),
    f"{TAGS['timer']}.service": (
        '[Unit]\nDescription=stopkit sentinel: the job of the known timer\n'
        f"[Service]\nType=oneshot\nExecStart=/bin/true {TAGS['timer']}\n"),
}
ENABLE = (f"{TAGS['unit']}.service", f"{TAGS['listener']}.service", f"{TAGS['timer']}.timer")
CRON_LINE = f"0 0 1 1 * /bin/true {TAGS['cron']}"         # once a year: in force, never in the way
AT_COMMAND = f"/bin/true {TAGS['at']}"


def seen(snapshot, sentinel: Account) -> dict[str, str | None]:
    """For each surface: None if this snapshot shows the sentinel's item in a listing that succeeded,
    otherwise why not."""
    def check(listing, match) -> str | None:
        if listing is None:
            return 'no listing was taken'
        if not listing.ok:
            return 'the listing failed: ' + '; '.join(listing.problems)[:300]
        return None if any(match(i) for i in listing.items) else 'the sentinel\'s item is not in the listing'

    out = {
        'process': check(snapshot.listing('process'), lambda i: i.has(TAGS['process']) and
                         i.owner == sentinel.name and i.fields.get('running', True)),
        'unit': check(snapshot.listing('unit', sentinel.name), lambda i: i.key == f"{TAGS['unit']}.service" and
                      i.fields.get('active') == 'active'),
        'timer': check(snapshot.listing('timer', sentinel.name), lambda i: i.key == f"{TAGS['timer']}.timer"),
        'cron': check(snapshot.listing('cron', sentinel.name), lambda i: i.has(TAGS['cron'])),
        'at': check(snapshot.listing('at', 'all'), lambda i: i.has(TAGS['at']) and i.owner == sentinel.name),
        'listener': check(snapshot.listing('listener', 'all'), lambda i: i.has(TAGS['listener'])),
        'worker': check(snapshot.listing('worker', 'all'), lambda i: i.has(TAGS['worker']) and
                        i.owner == sentinel.name and i.fields.get('status') == 'running'),
    }
    # Every other surface (the effect surface, a platform's components and stores) proves itself: its lister
    # adds an item named stopkit-sentinel-<...> marked as proof only when its own known item was seen in the
    # same reading (spec/surfaces.json says what that item is for each surface).
    for l in snapshot.listings:
        if l.surface not in out and l.surface not in CONTEXT:
            out[l.surface] = check(l, lambda i: i.key.startswith('stopkit-sentinel-') and i.fields.get('proof') is True)
    return out


def _write_as(account: Account, path: str, content: str, runner: Runner) -> None:
    """Write a file in the account's home, as the account (read first, also as the account)."""
    if run_as(account, ['cat', '--', path], runner).out == content:
        return
    r = run_as(account, ['sh', '-c', 'cat > "$1"', 'sh', path], runner, input=content)
    if not r.ok:
        raise KitError(f'could not write {path} as {account.name}: {r.error or r.err.strip()}')


def ensure(sentinel: Account, runner: Runner = run, proc: str = '/proc', timeout: float = 30.0,
           worker: bool = False) -> None:
    """Put the sentinel's items in place (idempotent) and wait until a fresh snapshot shows every one. With
    worker, the sentinel also keeps one job at the worker stand-in, which must be running."""
    unit_dir = os.path.join(sentinel.home, UNIT_DIR)
    steps = [run_as(sentinel, ['mkdir', '-p', unit_dir], runner)]
    for name, content in UNIT_FILES.items():
        _write_as(sentinel, os.path.join(unit_dir, name), content, runner)
    steps.append(run_as(sentinel, ['systemctl', '--user', 'daemon-reload'], runner))
    steps.append(run_as(sentinel, ['systemctl', '--user', 'enable', '--now', *ENABLE], runner, timeout=60))
    for r in steps:
        if not r.ok:
            raise KitError(f'sentinel set-up failed: {" ".join(r.argv[-4:])}: {r.error or r.err.strip()[:300]}')
    sock = WORKER_SOCKET if worker else None
    first = seen(snapshot.take([sentinel], runner, proc, system_timers=False, worker=sock), sentinel)
    if first['cron'] is not None:
        r = runner(['crontab', '-u', sentinel.name, '-'], input=CRON_LINE + '\n')
        if not r.ok:
            raise KitError(f'could not set the sentinel\'s crontab: {r.error or r.err.strip()}')
    if first['at'] is not None:
        r = run_as(sentinel, ['at', 'now + 365 days'], runner, input=AT_COMMAND + '\n')
        if not r.ok:
            raise KitError(f'could not queue the sentinel\'s at job: {r.error or r.err.strip()}')
    if worker and first['worker'] is not None:
        r = run_as(sentinel, ['python3', '-I', '-c', WORKER_SUBMIT, WORKER_SOCKET, TAGS['worker']], runner)
        if not r.ok:
            raise KitError(f'could not hand the sentinel\'s job to the worker: {r.error or r.err.strip()[:300]}')
    deadline = time.monotonic() + timeout
    while True:
        missing = {s: why for s, why in seen(snapshot.take([sentinel], runner, proc, system_timers=False, worker=sock),
                                             sentinel).items() if why and (worker or s != 'worker')}
        if not missing:
            return
        if time.monotonic() >= deadline:
            raise KitError(f'sentinel items not visible after {timeout:g} s: {missing}')
        time.sleep(1)


def remove(sentinel: Account, runner: Runner = run) -> list[str]:
    """Stop the sentinel's units (for `stopkit cleanup --all`; accounts.remove then removes its crontab, at
    job and home). Returns problems."""
    problems = []
    r = run_as(sentinel, ['systemctl', '--user', 'disable', '--now', *ENABLE], runner, timeout=60)
    if not r.ok:
        problems.append(f'disable sentinel units: {r.error or r.err.strip()[:200]}')
    return problems
