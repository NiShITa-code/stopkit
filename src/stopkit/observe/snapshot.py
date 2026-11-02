"""One complete reading of every surface, taken fresh from the system.

A check that only re-reads the items an earlier listing found can never find what that listing missed:
it shows that those items closed, not that nothing is left. So every checkpoint calls take(), which runs
every lister again from scratch; nothing is carried over from an earlier snapshot, and leftovers are
recognized by their tags in the new listings.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

from ..system import procfs
from ..system.accounts import Account
from ..system.cmd import Runner, run
from .cron import list_at_jobs, list_crontab
from .managers import list_manager
from .model import Item, Listing
from .processes import command_text, list_processes
from .sockets import list_listeners
from .timers import list_timers
from .units import list_user_units
from .worker import list_worker_jobs


@dataclass(frozen=True)
class Snapshot:
    taken_at: float                  # wall clock, for people
    finished_at: float
    listings: tuple[Listing, ...]
    # Monotonic clock (time.monotonic, which no clock change moves): the snapshot's start and end, and each
    # listing's start and end, in the order of `listings`. Every duration the kit reports is computed from these.
    mono: tuple[float, float] = (0.0, 0.0)
    listing_mono: tuple[tuple[float, float], ...] = ()

    def listing(self, surface: str, scope: str | None = None) -> Listing | None:
        for l in self.listings:
            if l.surface == surface and (scope is None or l.scope == scope):
                return l
        return None

    def find(self, tag: str) -> dict[tuple[str, str], list[Item]]:
        """{(surface, scope): items carrying tag}, over every listing (ok or not: callers must check ok)."""
        return {(l.surface, l.scope): l.find(tag) for l in self.listings}

    @property
    def problems(self) -> list[str]:
        return [f'{l.surface}/{l.scope}: {p}' for l in self.listings for p in l.problems]


def account_name(uid: int | None) -> str | None:
    import pwd                                  # Linux only; imported here so records can be rechecked anywhere
    if uid is None:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f'uid:{uid}'


def make_describe_pid(proc: str = '/proc'):
    """describe_pid(pid) -> (account name or None, masked command line) for listening-socket holders. The
    command line is '' if the process has gone, and None if it exists but could not be read."""
    def describe(pid: int) -> tuple[str | None, str | None]:
        try:
            p = procfs.read_process(proc, pid)
        except procfs.ReadError:
            return None, None
        if p is None:
            return None, ''
        return account_name(p['uid']), command_text(p['args'] or procfs.thread_args(proc, pid))
    return describe


def take(accounts: Sequence[Account], runner: Runner = run, proc: str = '/proc',
         system_timers: bool = True, process_only: Sequence[Account] = (), worker: str | None = None,
         extra: Sequence[Callable[[], Listing | Sequence[Listing]]] = ()) -> Snapshot:
    """A fresh reading of every surface for the given accounts: their processes, service-manager state,
    units, timers and crontabs; plus the system's timers, the at queue and every listening socket. Accounts
    in process_only add only their processes (the worker's account, whose jobs are processes). With worker
    (its socket path), the worker stand-in's own list of jobs is read too. Each of `extra` returns one or more
    further listings, read after the others: the effect surface, and a platform's own stores and components."""
    t0, m0 = time.time(), time.monotonic()
    everyone = list(accounts) + list(process_only)
    steps = [lambda: list_processes({a.uid: a.name for a in everyone}, proc)]
    for a in sorted(accounts, key=lambda a: a.name):
        steps += [lambda a=a: list_manager(a, runner), lambda a=a: list_user_units(a, runner),
                  lambda a=a: list_timers(a, runner), lambda a=a: list_crontab(a.name, runner)]
    if system_timers:
        steps.append(lambda: list_timers(None, runner))
    steps += [lambda: list_at_jobs(runner), lambda: list_listeners(runner, make_describe_pid(proc))]
    if worker:
        steps.append(lambda: list_worker_jobs(worker, {a.uid: a.name for a in everyone}))
    steps += list(extra)
    listings, spans = [], []
    for step in steps:
        s = time.monotonic()
        got = step()
        for l in ([got] if isinstance(got, Listing) else list(got)):
            listings.append(l)
            spans.append((s, time.monotonic()))
    return Snapshot(t0, time.time(), tuple(listings), (m0, time.monotonic()), tuple(spans))
