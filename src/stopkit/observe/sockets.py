"""Listening sockets (TCP listening and UDP bound), IPv4 and IPv6, with the processes that hold them.

An earlier listener check of this project matched addresses with
`(\\d+\\.\\d+\\.\\d+\\.\\d+|\\*):(\\d+)\\s`, so a server listening on IPv6 (`[::]:8000`), or on an
address with an interface suffix (`127.0.0.53%lo:53`), was never seen. This parser reads every address
form `ss` prints and fails the listing, rather than skipping, on any line it cannot understand.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from ..system.cmd import Runner, run
from .model import Item, Listing, failed

LINE = re.compile(r'^(?P<proto>\S+)\s+(?P<state>\S+)\s+(?P<recvq>\d+)\s+(?P<sendq>\d+)\s+'
                  r'(?P<local>\S+)\s+(?P<peer>\S+)(?:\s+(?P<proc>.*))?$')
OWNER = re.compile(r'\("(?P<name>(?:[^"\\]|\\.)*)",pid=(?P<pid>\d+),fd=(?P<fd>\d+)\)')


def split_address(addr: str) -> tuple[str, str, str | None]:
    """(host, port, interface) of an address as ss prints it, e.g. '[::]:22', '[fe80::1%eth0]:546',
    '*:68', '0.0.0.0:22', '127.0.0.53%lo:53'. Raises ValueError if it has no port."""
    if addr.startswith('['):
        end = addr.find(']')
        if end < 0 or not addr[end + 1:].startswith(':'):
            raise ValueError(f'unreadable address {addr!r}')
        host, port = addr[1:end], addr[end + 2:]
    else:
        host, sep, port = addr.rpartition(':')
        if not sep:
            raise ValueError(f'unreadable address {addr!r}')
    iface = None
    if '%' in host:
        host, iface = host.split('%', 1)
    if not port:
        raise ValueError(f'unreadable address {addr!r}')
    return host, port, iface


def parse_ss(text: str) -> tuple[list[dict], list[str]]:
    """Rows of `ss -H -tulpn` output, and the lines that could not be understood."""
    rows, bad = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = LINE.match(line.strip())
        if not m:
            bad.append(line)
            continue
        try:
            host, port, iface = split_address(m['local'])
        except ValueError:
            bad.append(line)
            continue
        owners = [{'name': o['name'], 'pid': int(o['pid']), 'fd': int(o['fd'])} for o in OWNER.finditer(m['proc'] or '')]
        rows.append({'proto': m['proto'], 'state': m['state'], 'host': host, 'port': port, 'iface': iface,
                     'peer': m['peer'], 'owners': owners})
    return rows, bad


def list_listeners(runner: Runner = run,
                   describe_pid: Callable[[int], tuple[str | None, str | None]] | None = None) -> Listing:
    """Every listening socket. describe_pid(pid) -> (owner account or None, masked command line) lets the
    snapshot attach the holder's account and command line, where leftovers carry their tags. A command line
    of None means the holder exists but could not be read, which fails the listing: its tag could be there."""
    taken = time.time()
    r = runner(['ss', '-H', '-tulpn'])
    if not r.ok:
        return failed('listener', 'all', taken, [f'ss: {r.error or "exit code " + str(r.rc)}: {r.err.strip()[:200]}'], (r,))
    rows, bad = parse_ss(r.out)
    if bad:
        return failed('listener', 'all', taken, [f'ss: {len(bad)} line(s) could not be understood, e.g. {bad[0]!r}'], (r,))
    items, problems = [], []
    for row in rows:
        host = f'[{row["host"]}]' if ':' in row['host'] else row['host']
        key = f'{row["proto"]}:{host}:{row["port"]}' + (f'%{row["iface"]}' if row['iface'] else '')
        owners, texts = set(), []
        for o in row['owners']:
            who, command = describe_pid(o['pid']) if describe_pid else (None, '')
            if command is None:
                problems.append(f'{key}: the holder (pid {o["pid"]}) exists but its command line could not be read')
            o['account'], o['command'] = who, command
            if who:
                owners.add(who)
            texts.append(command or o['name'])
        owner = ','.join(sorted(owners)) or None
        items.append(Item('listener', key, owner, ' | '.join(texts), dict(row)))
    if problems:
        return failed('listener', 'all', taken, problems, (r,))
    return Listing('listener', 'all', True, tuple(items), taken, (r,))
