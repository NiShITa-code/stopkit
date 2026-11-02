"""The kit's disposable accounts: created, marked as the kit's own, given a running service manager.

The kit acts only on accounts listed in its marker file (config.MARKER). An account with a kit name that
the kit did not create is refused, so a real user can never be touched by mistake.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from .cmd import CmdResult, Runner, run
from .names import MARKER, STATE_DIR
from . import KitError

MANAGER_UP = ('running', 'degraded')       # `systemctl --user is-system-running` once start-up has finished

# User and group IDs for the kit's accounts. Measured in WSL on 26 Oct 2026: release archives unpacked as root
# keep their builders' numbers, and ID 1001 owned 14,724 files there (Node.js under /opt and /root, n8n, a
# temporal binary). useradd gave that free number to the agent account, which could then have changed programs
# that root runs. So the kit takes IDs far from the usual ones, and only an ID that owns nothing in CHECKED.
SAFE_IDS = range(59900, 60000)
CHECKED = ('/usr', '/opt', '/etc', '/var', '/srv', '/root', '/home')
NOT_CHECKED = ('/var/spool/cron', '/var/tmp', '/var/lib/stopkit')   # the accounts' own crontabs, at jobs, scratch


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int
    home: str

    @property
    def runtime_dir(self) -> str:
        return f'/run/user/{self.uid}'

    @property
    def manager_unit(self) -> str:
        return f'user@{self.uid}.service'

    def env(self) -> dict[str, str]:
        """The environment a login of this account would have, enough for `systemctl --user`, crontab and at."""
        return {'HOME': self.home, 'USER': self.name, 'LOGNAME': self.name, 'SHELL': '/bin/bash',
                'PATH': '/usr/local/bin:/usr/bin:/bin', 'XDG_RUNTIME_DIR': self.runtime_dir,
                'DBUS_SESSION_BUS_ADDRESS': f'unix:path={self.runtime_dir}/bus'}


def lookup(name: str) -> Account | None:
    import pwd                                  # Linux only; imported here so records can be rechecked anywhere
    try:
        p = pwd.getpwnam(name)
    except KeyError:
        return None
    return Account(p.pw_name, p.pw_uid, p.pw_gid, p.pw_dir)


def load_marker(path: str = MARKER) -> dict[str, int]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise KitError(f'cannot read the kit\'s account marker {path}: {e}') from e
    return {str(k): int(v) for k, v in data.items()}


def save_marker(marker: dict[str, int], path: str = MARKER) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        json.dump(marker, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def owned(name: str, marker_path: str = MARKER) -> Account:
    """The account, if it exists and the kit created it. Raises KitError otherwise."""
    a = lookup(name)
    marker = load_marker(marker_path)
    if a is None:
        raise KitError(f'account {name} does not exist; run `stopkit setup` first')
    if marker.get(name) != a.uid:
        raise KitError(f'account {name} exists but the kit did not create it (not in {marker_path}); refusing to use it')
    return a


def as_account_argv(a: Account, argv: list[str]) -> list[str]:
    """argv run as account a (its own user and groups), with that account's login environment and nothing
    of root's. Used for everything the kit does inside an account, so that the account's own permissions
    apply: a path the account controls can never lead a root process somewhere else.

    Every signal is unblocked and set to its default action first (env --default-signal, coreutils 9.0 or
    later), and the command starts in the account's home (env --chdir). Whatever launched the kit cannot then
    change how the agent reacts to a stop or where its work runs. Both were found in runs: on 26 Oct a batch
    started with `nohup ... &` handed SIGINT to the agent as ignored, and every SIGINT run was void; and an at
    job records the directory it was queued from, so one queued from the kit's own folder, which the account
    cannot enter, never ran its command (E0, 26 Oct)."""
    return (['setpriv', f'--reuid={a.uid}', f'--regid={a.gid}', '--init-groups', '--', 'env', '-i', '--default-signal',
             f'--chdir={a.home}']
            + [f'{k}={v}' for k, v in sorted(a.env().items())] + list(argv))


def run_as(a: Account, argv: list[str], runner: Runner = run, **kw) -> CmdResult:
    return runner(as_account_argv(a, argv), **kw)


def manager_state(a: Account, runner: Runner = run) -> str:
    """What the account's own service manager says about itself ('running', 'starting', 'degraded', ...),
    or 'unreachable' if it does not answer (it is not running). Asking as the account starts nothing."""
    r = run_as(a, ['systemctl', '--user', 'is-system-running'], runner)
    return r.out.strip() or 'unreachable'


def wait_manager(a: Account, runner: Runner = run, timeout: float = 60.0) -> str:
    """Wait until the account's service manager has finished starting its units. Raises KitError if it
    has not within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    state = manager_state(a, runner)
    while state not in MANAGER_UP:
        if time.monotonic() >= deadline:
            raise KitError(f'the service manager of {a.name} ({a.manager_unit}) is not up after {timeout:g} s: {state}')
        time.sleep(0.5)
        state = manager_state(a, runner)
    return state


def start_manager(a: Account, runner: Runner = run) -> CmdResult:
    r = runner(['systemctl', 'start', a.manager_unit], timeout=90)
    wait_manager(a, runner)
    return r


def restart_manager(a: Account, runner: Runner = run) -> CmdResult:
    """Restart the account's service manager, standing in for a reboot: every unit of the account is
    stopped, and only what is set to start again (enabled units and timers) comes back."""
    r = runner(['systemctl', 'restart', a.manager_unit], timeout=120)
    wait_manager(a, runner)
    return r


def owned_elsewhere(n: int, home: str | None = None, runner: Runner = run) -> list[str]:
    """The first file found owned by user or group ID n in the folders where programs and their data live
    (CHECKED, on the root file system only, never other drives), outside `home`. Empty if there is none."""
    dirs = [d for d in CHECKED if os.path.isdir(d)]
    prune = [p for d in NOT_CHECKED + ((home,) if home else ()) for p in ('-path', d, '-o')][:-1]
    r = runner(['find', *dirs, '-xdev', '(', *prune, ')', '-prune', '-o', '(', '-uid', str(n), '-o', '-gid', str(n),
                ')', '-print', '-quit'], timeout=900)
    if r.error:
        return [f'could not check: {r.error}']
    return [l for l in r.out.splitlines() if l][:1]


def free_id(runner: Runner = run) -> int:
    """An ID in SAFE_IDS that no account or group has, and that owns no file outside the kit's own places."""
    import grp
    import pwd
    for n in SAFE_IDS:
        try:
            pwd.getpwuid(n)
            continue
        except KeyError:
            pass
        try:
            grp.getgrgid(n)
            continue
        except KeyError:
            pass
        if not owned_elsewhere(n, runner=runner):
            return n
    raise KitError(f'no free user ID in {SAFE_IDS.start}-{SAFE_IDS.stop - 1} owns nothing')


def unsafe(a: Account, runner: Runner = run) -> str | None:
    """Why the account's ID is unsafe to run an agent as, or None."""
    if a.uid not in SAFE_IDS or a.gid not in SAFE_IDS:
        return f'its IDs ({a.uid}, {a.gid}) are outside the kit\'s range {SAFE_IDS.start}-{SAFE_IDS.stop - 1}'
    found = owned_elsewhere(a.uid, a.home, runner) + (owned_elsewhere(a.gid, a.home, runner) if a.gid != a.uid else [])
    return f'its ID owns {found[0]}' if found else None


def renumber(name: str, runner: Runner = run, marker_path: str = MARKER) -> Account:
    """Move one of the kit's accounts to a safe ID (usermod; nothing is deleted). Its processes, lingering,
    crontab and at jobs are stopped first, as usermod requires; setup puts its own items back afterwards."""
    a = owned(name, marker_path)
    n = free_id(runner)
    runner(['loginctl', 'disable-linger', name])
    runner(['systemctl', 'stop', a.manager_unit], timeout=120)
    runner(['crontab', '-r', '-u', name])
    atq = runner(['atq'])
    for line in atq.out.splitlines() if atq.ok else []:
        if line.split()[-1:] == [name]:
            runner(['atrm', line.split()[0]])
    for _ in range(10):
        runner(['pkill', '-KILL', '-U', str(a.uid)])
        r1 = runner(['groupmod', '--gid', str(n), name])
        r2 = runner(['usermod', '--uid', str(n), '--gid', str(n), name]) if r1.ok else r1
        if r1.ok and r2.ok:
            break
        time.sleep(1)
    else:
        raise KitError(f'could not move {name} to ID {n}: {(r2.error or r2.err.strip())[:300]}')
    b = lookup(name)
    run_as(b, ['chgrp', '-R', '-h', str(n), '--', b.home], runner)   # usermod moves the owner, not the group
    marker = load_marker(marker_path)
    marker[name] = b.uid
    save_marker(marker, marker_path)
    return b


def ensure(name: str, runner: Runner = run, marker_path: str = MARKER, linger: bool = True) -> Account:
    """Create the account if it does not exist (with a safe ID: see SAFE_IDS), mark it as the kit's own, and
    (with linger) make its service manager run without a login. An existing account the kit did not create
    is refused."""
    a = lookup(name)
    marker = load_marker(marker_path)
    if a is not None and marker.get(name) != a.uid:
        raise KitError(f'account {name} exists but the kit did not create it; refusing to use it')
    if a is None:
        n = free_id(runner)
        for argv in (['groupadd', '--gid', str(n), name],
                     ['useradd', '--uid', str(n), '--gid', str(n), '--create-home', '--shell', '/bin/bash',
                      '--comment', 'stopkit test account, safe to delete', name]):
            r = runner(argv)
            if not r.ok:
                raise KitError(f'could not create account {name}: {r.error or r.err.strip()}')
        a = lookup(name)
        if a is None:
            raise KitError(f'account {name} was created but cannot be found')
        marker[name] = a.uid
        save_marker(marker, marker_path)
    if not linger:
        return a
    r = runner(['loginctl', 'enable-linger', name])
    if not r.ok:
        raise KitError(f'could not turn on lingering for {name}: {r.error or r.err.strip()}')
    start_manager(a, runner)
    return a


def remove(name: str, runner: Runner = run, marker_path: str = MARKER) -> list[str]:
    """Delete an account the kit created, with its home, crontab and at jobs. The account leaves the
    marker only once it is really gone, so that a failed attempt can be repeated. Returns problems."""
    a = owned(name, marker_path)
    problems = []
    for argv in (['loginctl', 'disable-linger', name], ['systemctl', 'stop', a.manager_unit]):
        r = runner(argv, timeout=120)
        if not r.ok:
            problems.append(f'{" ".join(argv)}: {r.error or r.err.strip()}')
    runner(['crontab', '-r', '-u', name])                       # "no crontab" is fine
    atq = runner(['atq'])
    for line in atq.out.splitlines() if atq.ok else []:
        if line.split()[-1:] == [name]:
            runner(['atrm', line.split()[0]])
    for _ in range(5):                       # userdel refuses while a just-killed process is not yet reaped
        runner(['pkill', '-KILL', '-U', name])                  # real user ID
        runner(['pkill', '-KILL', '-u', name])                  # effective user ID
        r = runner(['userdel', '--remove', name])
        if lookup(name) is None:
            break
        time.sleep(1)
    if lookup(name) is None:
        marker = load_marker(marker_path)
        marker.pop(name, None)
        save_marker(marker, marker_path)
    else:
        problems.append(f'userdel {name}: {r.error or r.err.strip()}; it stays marked as the kit\'s, so cleanup can retry')
    return problems


def state_dir_ready() -> None:
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
