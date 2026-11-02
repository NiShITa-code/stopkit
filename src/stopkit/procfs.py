"""Reading the Linux process table safely.

Copied on 25 Oct 2026 from shutdown-verifier/verifier/collectors/linux_process.py: the reading and
safety helpers only. The original stays frozen with the results it produced. Changes:
  - read_table() can keep only the processes of given accounts, and reads their command lines only then;
  - no environment labels and no linking by timing (the kit finds its leftovers by their tags);
  - type hints.

The rules that matter, kept from the original:
  - a process is identified by its ID and its start time together, so a reused ID is never taken for it;
  - a read that fails is an error (ReadError), never evidence that the process exited;
  - control groups that stand for the whole system, a user's whole service manager or a login session
    are never treated as one agent's group, and essential system units are never touched.
"""
from __future__ import annotations

import os
import posixpath
import re
import signal

CLK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
PF_KTHREAD = 0x00200000          # flag bit in /proc/<pid>/stat marking a kernel thread
PROTECTED_CGROUPS = {'', '/', '/system.slice', '/user.slice', '/init.scope', '/machine.slice'}
# Units that keep the machine reachable and running. Their control group never stands for an agent.
ESSENTIAL_UNITS = re.compile(r'^(systemd-.*|sshd|ssh|dbus|dbus-broker|docker|containerd|cron|crond|rsyslog|auditd|'
                             r'chrony|chronyd|polkit|snapd|NetworkManager|networkd-dispatcher|wpa_supplicant|'
                             r'multipathd|udisks2|accounts-daemon|unattended-upgrades|getty@.*|serial-getty@.*|'
                             r'console-getty|wsl.*)\.(service|scope)$')
SECRET_NAME = re.compile(r'(?i)(key|token|secret|passw|pwd|auth|cookie|credential|bearer)')
SECRET_VALUE = re.compile(r'(sk-|ghp_|gho_|ghs_|github_pat_|xox[abpors]-|AKIA|AIza)[A-Za-z0-9_\-]{8,}')


class ReadError(Exception):
    """A process's /proc entry exists but could not be read or understood. Not evidence of exit."""


class TableError(Exception):
    """The process table itself could not be read, so nothing can be said about any process."""


def redact(args: list[str]) -> str:
    """Command line with likely secrets masked: values of NAME=VALUE pairs and of options whose name
    suggests a secret, and strings shaped like common API keys. Pattern-based: check before sharing."""
    out, hide_next = [], False
    for a in args:
        if hide_next:
            out.append('[masked]')
            hide_next = False
        elif '=' in a and SECRET_NAME.search(a.split('=', 1)[0]):
            out.append(a.split('=', 1)[0] + '=[masked]')
        else:
            out.append(SECRET_VALUE.sub('[masked]', a))
            hide_next = (a.startswith('-') and bool(SECRET_NAME.search(a))) or a.lower() == 'bearer'
    return ' '.join(out)[:2000]


def read_stat(proc: str, pid: int | str) -> dict | None:
    """Fields of /proc/<pid>/stat, or None if the process is gone. Any other failure raises ReadError."""
    path = f'{proc}/{pid}/stat'
    try:
        with open(path, 'rb') as f:
            s = f.read().decode('utf-8', 'replace')
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as e:
        raise ReadError(f'{path}: {type(e).__name__}: {e}') from e
    try:
        r = s.rindex(')')                # the command name can itself contain ')' or spaces
        f = s[r + 2:].split()            # f[0] is field 3 of the stat line
        return {'comm': s[s.index('(') + 1:r], 'state': f[0], 'ppid': int(f[1]), 'pgrp': int(f[2]),
                'session': int(f[3]), 'flags': int(f[6]), 'start': int(f[19])}
    except (ValueError, IndexError) as e:
        raise ReadError(f'{path}: malformed content ({type(e).__name__}: {e})') from e


def boot_time(proc: str = '/proc') -> int:
    with open(f'{proc}/stat') as f:
        for line in f:
            if line.startswith('btime '):
                return int(line.split()[1])
    raise TableError(f'{proc}/stat has no boot time (btime)')


def boot_id(proc: str = '/proc') -> str | None:
    """The kernel's random ID for this boot, or None. Unlike the boot time, it does not move when the clock
    is changed, so it tells a restart from a clock change."""
    try:
        with open(f'{proc}/sys/kernel/random/boot_id') as f:
            return f.read().strip() or None
    except OSError:
        return None


def system_start(proc: str = '/proc') -> str | None:
    """Which start of the system this is: the kernel's boot ID and the start time of process 1. In WSL the
    distribution's system (process 1) is powered off when no WSL window is open and started again on the next
    call, while the kernel's boot ID stays the same; the start time of process 1 tells the two apart."""
    st = None
    try:
        st = read_stat(proc, 1)
    except ReadError:
        pass
    b = boot_id(proc)
    return f"{b}/{st['start']}" if b and st else None


def live_threads(proc: str, pid: int) -> int:
    """How many threads of the process are still running (not zombie or dead). Raises ReadError if the
    thread list cannot be read."""
    tdir = f'{proc}/{pid}/task'
    try:
        tids = [t for t in os.listdir(tdir) if t.isdigit()]
    except FileNotFoundError:
        return 0
    except OSError as e:
        raise ReadError(f'{tdir}: {type(e).__name__}: {e}') from e
    n = 0
    for t in tids:
        st = read_stat(f'{proc}/{pid}/task', t)
        if st is not None and st['state'] not in ('Z', 'X'):
            n += 1
    return n


def thread_args(proc: str, pid: int) -> list[str]:
    """The command line as seen through a still-running thread of the process. Once the main thread has
    exited, the process's own cmdline reads empty while its other threads run on."""
    tdir = f'{proc}/{pid}/task'
    try:
        tids = sorted(t for t in os.listdir(tdir) if t.isdigit())
    except OSError:
        return []
    for t in tids:
        try:
            with open(f'{tdir}/{t}/cmdline', 'rb') as f:
                data = f.read()
        except OSError:
            continue
        if data:
            return [a.decode('utf-8', 'replace') for a in data.split(b'\0') if a]
    return []


def agent_cgroup(cg: str | None) -> tuple[str, str | None]:
    """Normalise a control-group path and say whether it can stand for one agent: (path, problem).
    problem is None if it can; otherwise it says why not (system-wide, a whole user manager, a login
    session, an essential unit, or not a scope or service). Such a group is never used or killed."""
    path = posixpath.normpath('/' + (cg or '').strip())
    path = '/' + path.lstrip('/')                   # normpath keeps a leading '//'
    parts = [p for p in path.split('/') if p]
    if not parts or path in PROTECTED_CGROUPS or len(parts) < 2 or parts[-1] == 'init.scope':
        return path, f'{path} is a system-wide control group, so it cannot identify one agent'
    last = parts[-1]
    if ESSENTIAL_UNITS.match(last):
        return path, f'{path} belongs to an essential system service, so it cannot identify one agent'
    if last.startswith('user@') and last.endswith('.service'):
        return path, f"{path} is a user's whole service manager, so it cannot identify one agent"
    if last.startswith('session-') and last.endswith('.scope'):
        return path, f'{path} is a login session, so it cannot identify one agent'
    if not (last.endswith('.scope') or last.endswith('.service')):
        return path, f'{path} is not a scope or service unit'
    return path, None


SIGNAL_MASKS = {'SigBlk': 'blocked', 'SigIgn': 'ignored', 'SigCgt': 'caught'}


def _signame(n: int) -> str:
    try:
        return signal.Signals(n).name
    except ValueError:
        return f'SIG{n}'                      # a real-time signal


def signal_masks(proc: str, pid: int) -> dict | None:
    """How a process treats signals, from /proc/<pid>/status: its name ('comm') and the signals it has
    'blocked', 'ignored' and 'caught', by name. None if it is gone. Ignored and blocked signals pass to
    every program it starts; a process launched as the kit launches agents has neither
    (accounts.as_account_argv)."""
    path = f'{proc}/{pid}/status'
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as e:
        raise ReadError(f'{path}: {type(e).__name__}: {e}') from e
    out: dict = {}
    for line in lines:
        key, _, val = line.partition(':')
        if key == 'Name':
            out['comm'] = val.strip()
        elif key in SIGNAL_MASKS:
            try:
                mask = int(val.strip(), 16)
            except ValueError as e:
                raise ReadError(f'{path}: malformed {key} line {line!r}') from e
            out[SIGNAL_MASKS[key]] = [_signame(n) for n in range(1, 65) if mask >> (n - 1) & 1]
    if set(SIGNAL_MASKS.values()) - set(out):
        raise ReadError(f'{path}: no signal masks')
    return out


def limits(proc: str, pid: int) -> dict | None:
    """A process's resource limits, from /proc/<pid>/limits: {name: [soft, hard]} as the kernel prints them.
    None if it is gone. Limits pass to every program a process starts, like ignored signals."""
    try:
        with open(f'{proc}/{pid}/limits') as f:
            lines = f.read().splitlines()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as e:
        raise ReadError(f'{proc}/{pid}/limits: {type(e).__name__}: {e}') from e
    out = {}
    for line in lines[1:]:
        name, rest = line[:26].strip(), line[26:].split()
        if name and len(rest) >= 2:
            out[name] = rest[:2]
    return out


def _read_uid(proc: str, pid: int) -> int | None:
    try:
        with open(f'{proc}/{pid}/status') as f:
            return int(next(l for l in f if l.startswith('Uid:')).split()[1])
    except (OSError, StopIteration, ValueError, IndexError):
        return None


def read_process(proc: str, pid: int, uids: set[int] | None = None) -> dict | None:
    """One process: its stat fields, uid, control group and command line. None if it is gone, exited while
    being read, is a kernel thread, or (when uids is given) belongs to another account. Raises ReadError
    if its entry exists but cannot be read."""
    st = read_stat(proc, pid)
    if st is None or st['flags'] & PF_KTHREAD:
        return None
    uid = _read_uid(proc, pid)
    if uids is not None and uid not in uids:
        if uid is None and read_stat(proc, pid) is not None:
            raise ReadError(f'{proc}/{pid}/status: could not read the owner of a running process')
        return None
    p = dict(st, pid=pid, uid=uid, cgroup=None, args=[])
    try:
        with open(f'{proc}/{pid}/cgroup') as f:
            p['cgroup'] = next((l.strip()[3:] for l in f if l.startswith('0::')), None)
    except OSError:
        pass
    try:
        with open(f'{proc}/{pid}/cmdline', 'rb') as f:
            p['args'] = [a.decode('utf-8', 'replace') for a in f.read().split(b'\0') if a]
    except OSError:
        pass
    again = read_stat(proc, pid)
    if again is None or again['start'] != st['start']:
        return None                  # it exited while being read
    p['state'] = again['state']
    return p


def proc_hides_others(proc: str = '/proc', mounts: str = '/proc/self/mounts') -> str | None:
    """The hidepid option of the proc file system at `proc`, if it hides other users' processes; else None."""
    target = os.path.realpath(proc)
    try:
        with open(mounts) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[2] == 'proc' and os.path.realpath(parts[1]) == target:
                    for opt in parts[3].split(','):
                        if opt.startswith('hidepid=') and opt.split('=', 1)[1] not in ('0', 'off'):
                            return opt
    except OSError:
        return None
    return None


def read_table(proc: str = '/proc', uids: set[int] | None = None, problems: list[str] | None = None) -> dict[int, dict]:
    """Every readable user-space process (only those of `uids`, if given). Entries that exist but cannot
    be read are left out and described in `problems`, so the caller can report the listing as incomplete."""
    try:
        pids = [int(d) for d in os.listdir(proc) if d.isdigit()]
    except OSError as e:
        raise TableError(f'cannot list {proc}: {e}') from e
    table = {}
    for pid in pids:
        try:
            p = read_process(proc, pid, uids)
        except ReadError as e:
            if problems is not None:
                problems.append(str(e))
            continue
        if p is not None:
            table[pid] = p
    return table


def descendants(table: dict[int, dict], root: int) -> set[int]:
    """root and every process below it, by the parent IDs the kernel records."""
    kids: dict[int, list[int]] = {}
    for pid, p in table.items():
        kids.setdefault(p['ppid'], []).append(pid)
    out, todo = set(), [root]
    while todo:
        pid = todo.pop()
        if pid not in out:
            out.add(pid)
            todo += kids.get(pid, [])
    return out


def cgroup_members(cgdir: str) -> tuple[set[int], list[str]]:
    """Every process in the control group and its child groups, from each group's cgroup.procs: (pids,
    errors). A cgroup.procs that is missing or unreadable is an error: the member list is then incomplete."""
    pids, errors = set(), []
    for d, dirs, _ in os.walk(cgdir):
        dirs[:] = [x for x in dirs if x != 'cgroup.procs']
        path = os.path.join(d, 'cgroup.procs')
        try:
            with open(path) as f:
                pids |= {int(x) for x in f.read().split()}
        except (OSError, ValueError) as e:
            errors.append(f'{path}: {type(e).__name__}: {e}')
    return pids, errors
