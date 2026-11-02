"""Checks the kit makes about the machine before it changes anything."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager

from .accounts import Account, run_as
from .cmd import Runner, run
from .names import STATE_DIR
from . import KitError

# 1 GB. On 25 Oct C: had about 1.2 GB free, and WSL's disk image lives on C: and does not shrink when files
# inside WSL are deleted. A full C: breaks WSL itself, so the kit stops before that can happen.
DISK_FLOOR = 1_000_000_000
WATCHED = ('/', '/mnt/c')            # the Linux root; and C: when running in WSL
LOCK = f'{STATE_DIR}/lock'


def free_bytes(path: str) -> int | None:
    try:
        st = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    return st.f_bavail * st.f_frsize


def disk_problems(paths=WATCHED, floor: int = DISK_FLOOR) -> list[str]:
    """Why the kit must not start, if any watched file system has less than `floor` bytes free. A path
    that does not exist is skipped (for example /mnt/c outside WSL), except the root, which must be readable."""
    problems = []
    for p in paths:
        free = free_bytes(p)
        if free is None:
            if p == '/':
                problems.append('could not read the free space of /')
            continue
        if free < floor:
            problems.append(f'{p} has {free / 1e9:.2f} GB free, below the {floor / 1e9:.2f} GB floor')
    return problems


def root_problems() -> list[str]:
    return [] if os.geteuid() == 0 else ['the kit must run as root, to see every account and service manager']


@contextmanager
def single_run(path: str = LOCK):
    """Only one kit command that changes things may run at a time: they share the test accounts."""
    import fcntl                                 # Linux only; imported here so records can be rechecked anywhere
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    f = open(path, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise KitError('another stopkit command is running on this machine; the test accounts are shared, so '
                       'wait for it to finish')
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _ancestors(path: str) -> list[str]:
    out, cur = [], os.path.abspath(path)
    while True:
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        cur = parent


def changeable_by(account: Account, paths: list[str], runner: Runner = run) -> list[str]:
    """Which of `paths` the account could change, as the kernel decides it (`test -w` run as the account):
    a path it can write, or a folder above it that it can write, unless that folder has the sticky bit and
    the account does not own the entry below it (as with /tmp)."""
    chain = list(dict.fromkeys(a for p in paths for a in _ancestors(p)))
    r = run_as(account, ['sh', '-c', 'for p; do [ -w "$p" ] && printf "%s\\n" "$p"; done; exit 0', 'sh', *chain], runner)
    if not r.ok:
        return [f'could not check what {account.name} can write: {r.error or r.err.strip()[:200]}']
    writable = set(r.out.splitlines())
    out = []
    for p in paths:
        chain_p = _ancestors(p)
        if chain_p[0] in writable:
            out.append(f'{chain_p[0]} can be written by {account.name}')
        for child, folder in zip(chain_p, chain_p[1:]):
            if folder not in writable:
                continue
            try:
                sticky = bool(os.stat(folder).st_mode & stat.S_ISVTX)
                owner = os.lstat(child).st_uid
            except OSError:
                sticky, owner = False, account.uid
            if not (sticky and owner != account.uid):
                out.append(f'{folder} can be written by {account.name}, so it could replace {child}')
    return out


def code_problems(account: Account, code_dir: str, runner: Runner = run) -> list[str]:
    """Anything under the folder the kit runs its code from that the account could change."""
    out = changeable_by(account, [code_dir], runner)
    r = run_as(account, ['find', code_dir, '-writable', '-print', '-quit'], runner)
    if r.out.strip():
        out.append(f'{r.out.strip()} (under the kit\'s code) can be written by {account.name}')
    elif not r.ok and 'Permission denied' not in r.err:      # unreadable to the account is fine
        out.append(f'could not check the kit\'s code for {account.name}: {r.error or r.err.strip()[:200]}')
    return out


def writable_windows_drives(account: Account, runner: Runner = run, mounts: str = '/proc/self/mounts') -> list[str]:
    """WSL's Windows drives (9p or drvfs mounts) that the account can write. In WSL every account can
    usually write them, so an agent run as the test account could change files on the Windows side."""
    points = []
    try:
        with open(mounts) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and (parts[2] == 'drvfs' or (parts[2] == '9p' and 'aname=drvfs' in parts[3])):
                    points.append(parts[1])
    except OSError:
        return []
    return [p for p in points if run_as(account, ['test', '-w', p], runner).rc == 0]
