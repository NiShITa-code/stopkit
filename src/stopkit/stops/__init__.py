"""Stops the kit carries out itself, and the checks around them.

  send()            a signal to one process, only if it is still the same process (pidfd and start time)
                    and belongs to the test account
  kill_tree()       SIGKILL to a process and everything under it
  kill_account()    SIGKILL to every process of the account, its service manager included
  reference_stop()  the playbook's order (DESIGN.md section 8); it must end every case
  leftovers()       what of the account is still there, from fresh listings
  cleanup()         the reference stop, the account's own unit files removed, its service manager
                    restarted, then a check that the account holds nothing

Nothing here uses the cases' tags. The reference stop and the checks find the account's work through the
account alone, so the self-test cannot pass by knowing the answers.
"""
from __future__ import annotations

import os
import re
import select
import signal
import time
from typing import Callable

from ..system import procfs
from ..system.accounts import MANAGER_UP, Account, manager_state, restart_manager, run_as, start_manager
from ..agents.base import EXPLICIT, StopClaim
from ..system.cmd import CmdResult, Runner, run
from ..system import KitError
from ..observe.cron import list_at_jobs, list_crontab
from ..observe.snapshot import make_describe_pid
from ..observe.sockets import list_listeners
from ..observe.units import UNSURE, json_rows, parse_show, unit_present, user_manager_argv
from ..observe.worker import NOT_RUNNING, request
from ..system.names import WORKER_SOCKET

UNIT_NAME = re.compile(r'^[A-Za-z0-9:_.\\@-]+\.(service|socket|timer|path|target|mount|automount|swap|slice|scope)$')
SYSTEM_PREFIXES = ('/usr/', '/lib/', '/etc/', '/run/systemd/')     # only root can write unit files there
MANAGER_OWN = ('init.scope', '-.slice')                            # the service manager itself
TRIGGERS = ('.timer', '.path', '.socket')    # stopped first, so that they cannot start anything mid-stop
Target = tuple[int, int]                     # (pid, start time): a process's identity


# ---- signals ---------------------------------------------------------------------------------------------

def send(pid: int, start: int, sig: int, uid: int, proc: str = '/proc') -> str:
    """Send sig to the process (pid, start) if it still exists and belongs to uid. Returns 'sent', 'gone',
    'refused: ...' or 'error: ...'. The pidfd makes the signal reach that very process even if its ID were
    reused in between; the start time proves the pidfd was opened on the right one."""
    if pid <= 1 or pid == os.getpid():
        return 'refused: protected process'
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return 'gone'
    except OSError as e:
        return f'error: pidfd_open: {e}'
    try:
        p = procfs.read_process(proc, pid)
        if p is None or p['start'] != start:
            return 'gone'
        if p['uid'] != uid:
            return f'refused: owned by uid {p["uid"]}, not the test account'
        signal.pidfd_send_signal(fd, sig)
        return 'sent'
    except ProcessLookupError:
        return 'gone'
    except (procfs.ReadError, OSError) as e:
        return f'error: {e}'
    finally:
        os.close(fd)


def alive(pid: int, start: int, proc: str = '/proc') -> bool:
    """True if the process is still there and running. A zombie whose threads have all ended has ended. If
    it cannot be read, it counts as alive: doubt never becomes 'gone'."""
    try:
        st = procfs.read_stat(proc, pid)
        if st is None or st['start'] != start:
            return False
        if st['state'] in ('Z', 'X'):
            return procfs.live_threads(proc, pid) > 0
        return True
    except procfs.ReadError:
        return True


def wait_gone(targets: list[Target], timeout: float, proc: str = '/proc',
              poll: Callable[[], object] | None = None) -> list[Target]:
    """Wait until every target has ended, or timeout. Returns those still alive. poll() is called on every
    round (to reap the kit's own child, for example).

    The wait wakes the moment a process ends: each target is watched through a pidfd, which becomes readable
    when its process exits. (Waking every 50 ms instead made every short stop measure about 0.05 s.)"""
    fds: dict[Target, int] = {}
    for t in targets:
        try:
            fd = os.pidfd_open(t[0])
        except OSError:
            continue                             # gone already, or no pidfd: then plain polling below
        if alive(*t, proc=proc):                 # opened on the same process, not a reused ID
            fds[t] = fd
        else:
            os.close(fd)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if poll:
                poll()
            left = [t for t in targets if alive(*t, proc=proc)]
            now = time.monotonic()
            if not left or now >= deadline:
                return left
            watch = [fds[t] for t in left if t in fds]
            if len(watch) == len(left):
                select.select(watch, [], [], min(deadline - now, 1.0))
            else:
                time.sleep(0.01)
    finally:
        for fd in fds.values():
            os.close(fd)


def manager_pid(account: Account, runner: Runner = run) -> tuple[int, str | None]:
    """The process ID of the account's service manager, as the system's manager records it (0 if it is
    not running), and a problem if it could not be read. A process ID, unlike a control group, cannot be
    taken on by another process: the account can move its own processes into the manager's group."""
    r = runner(['systemctl', 'show', account.manager_unit, '-p', 'MainPID', '--value'])
    if not r.ok:
        return 0, f'systemctl show {account.manager_unit}: {r.error or "exit code " + str(r.rc)}'
    try:
        return int(r.out.strip()), None
    except ValueError:
        return 0, f'systemctl show {account.manager_unit}: unexpected output {r.out.strip()[:80]!r}'


def account_processes(account: Account, proc: str = '/proc', runner: Runner = run,
                      with_manager: bool = False) -> tuple[list[dict], list[str]]:
    """The account's running processes, without its service manager and the manager's PAM helper
    ((sd-pam), its child) unless asked, and reading problems."""
    problems: list[str] = []
    try:
        table = procfs.read_table(proc, {account.uid}, problems)
    except procfs.TableError as e:
        return [], [str(e)]
    keep: set[int] = set()
    if not with_manager:
        main, why = manager_pid(account, runner)
        if why:
            problems.append(f'cannot tell the service manager apart: {why}')
        if main:
            keep = {pid for pid, p in table.items() if pid == main or (p['ppid'] == main and p['comm'] == '(sd-pam)')}
    out = [p for pid, p in sorted(table.items()) if pid not in keep and alive(pid, p['start'], proc)]
    return out, problems


def _claim(t0: float, said: bool | None, message: str, details: dict, commands=(), report: str = 'apparent') -> StopClaim:
    return StopClaim(said, message, t0, time.time(), details, tuple(commands), report=report)


def kill_tree(pid: int, start: int, uid: int, proc: str = '/proc', timeout: float = 10.0,
              poll: Callable[[], object] | None = None) -> StopClaim:
    """SIGKILL to the process and everything under it, found by parent IDs at the moment of the stop.
    Reports stopped if every process it signalled has ended."""
    t0 = time.time()
    problems: list[str] = []
    try:
        table = procfs.read_table(proc, None, problems)
    except procfs.TableError as e:
        return _claim(t0, None, f'could not read the process table: {e}', {})
    tree = procfs.descendants(table, pid) if pid in table and table[pid]['start'] == start else set()
    targets = [(p, table[p]['start']) for p in sorted(tree)]
    sent = {str(p): send(p, s, signal.SIGKILL, uid, proc) for p, s in targets}
    left = wait_gone(targets, timeout, proc, poll)
    said = bool(targets) and not left
    msg = (f'killed the process tree ({len(targets)} processes)' if said else
           f'{len(left)} of {len(targets)} processes in the tree still running' if targets else
           'the agent process was already gone')
    return _claim(t0, said, msg, {'signalled': sent, 'still_running': [p for p, _ in left], 'problems': problems})


def stop_scope(scope: str, main: Target, runner: Runner = run, proc: str = '/proc',
               poll: Callable[[], object] | None = None) -> StopClaim:
    """`systemctl stop` on the agent's own control group (its scope), as stopping a service does: SIGTERM to
    every process in it, then SIGKILL. Reports stopped if the command succeeded and the scope is gone."""
    t0 = time.time()
    r = runner(['systemctl', 'stop', scope], timeout=120)
    state = runner(['systemctl', 'is-active', scope])
    wait_gone([main], 10.0, proc, poll)
    said = r.ok and state.out.strip() != 'active'
    return _claim(t0, said, f'systemctl stop {scope}: exit code {r.rc}; the scope is now {state.out.strip()}',
                  {}, (r, state))


def kill_account(account: Account, proc: str = '/proc', timeout: float = 10.0,
                 poll: Callable[[], object] | None = None) -> StopClaim:
    """SIGKILL to every process of the account, again and again until none is left or timeout. This kills
    the account's service manager too, as `pkill -KILL -u` or `loginctl kill-user` would. If the process
    table could not be read completely, it reports nothing either way."""
    t0 = time.time()
    deadline = time.monotonic() + timeout
    sent: dict[str, str] = {}
    while True:
        if poll:
            poll()
        procs, problems = account_processes(account, proc, with_manager=True)
        if (not procs and not problems) or time.monotonic() >= deadline:
            break
        for p in procs:
            sent[f"{p['pid']}:{p['start']}"] = send(p['pid'], p['start'], signal.SIGKILL, account.uid, proc)
        time.sleep(0.1)
    if problems:
        said, msg = None, 'could not read the whole process table: ' + '; '.join(problems)[:300]
    else:
        said = not procs
        msg = 'no process of the account is left' if said else f'{len(procs)} processes of the account still running'
    return _claim(t0, said, msg, {'signalled': sent, 'still_running': [p['pid'] for p in procs],
                                  'problems': problems})


def term_then_kill(account: Account, proc: str = '/proc', runner: Runner = run, term_wait: float = 5.0,
                   kill_wait: float = 5.0, poll: Callable[[], object] | None = None, rounds: int = 3) -> dict:
    """SIGTERM to every process of the account except its service manager; SIGKILL to any still running
    after term_wait; repeated up to `rounds` times in case something started meanwhile. Signals nothing if
    the service manager cannot be told apart."""
    log: list[dict] = []
    for _ in range(rounds):
        procs, problems = account_processes(account, proc, runner)
        if any(p.startswith('cannot tell the service manager apart') for p in problems):
            log.append({'skipped': problems})
            break
        if not procs:
            break
        targets = [(p['pid'], p['start']) for p in procs]
        log.append({'signal': 'SIGTERM', 'sent': {f'{t[0]}:{t[1]}': send(*t, signal.SIGTERM, account.uid, proc)
                                                   for t in targets}, 'problems': problems})
        left = wait_gone(targets, term_wait, proc, poll)
        if left:
            log.append({'signal': 'SIGKILL', 'sent': {f'{t[0]}:{t[1]}': send(*t, signal.SIGKILL, account.uid, proc)
                                                       for t in left}})
            wait_gone(left, kill_wait, proc, poll)
    procs, _ = account_processes(account, proc, runner)
    return {'rounds': log, 'still_running': [p['pid'] for p in procs]}


# ---- the account's own units -----------------------------------------------------------------------------

def made_by_system(path: str, account: Account) -> bool:
    """True if a unit defined at `path` comes from the system rather than from the account: from a
    directory only root can write, or from the system's unit generators (run by the manager at every
    reload). Limitation: generators the account itself installs (~/.config/systemd/user-generators) are
    taken as the system's."""
    if path.startswith(SYSTEM_PREFIXES):
        return True
    gen = os.path.join(account.runtime_dir, 'systemd', 'generator')
    return any(path.startswith(f'{gen}{suffix}/') for suffix in ('', '.early', '.late'))


def own_unit_dirs(account: Account) -> list[str]:
    """Where the account itself can define units (its home and its runtime directory)."""
    return [os.path.join(account.home, '.config/systemd/user'), os.path.join(account.home, '.local/share/systemd/user'),
            os.path.join(account.runtime_dir, 'systemd/user')]


def scan_own_dirs(account: Account, runner: Runner = run) -> tuple[dict[str, str], list[str], list[CmdResult]]:
    """Unit names defined or enabled in the account's own unit directories: ({name: where}, problems,
    commands). The directories are read by `find` running as the account, so that a symbolic link the
    account planted is followed only with its own permissions, exactly as its service manager follows it."""
    found: dict[str, str] = {}
    problems: list[str] = []
    commands: list[CmdResult] = []
    for d in own_unit_dirs(account):
        r = run_as(account, ['find', '-H', d, '-mindepth', '1', '-maxdepth', '2', '-printf', r'%d\t%y\t%P\n'], runner)
        commands.append(r)
        if not r.ok:
            if r.rc == 1 and r.err.strip() == f"find: '{d}': No such file or directory":
                continue                            # the directory does not exist: nothing defined there
            problems.append(f'find {d}: {r.error or "exit code " + str(r.rc)}: {r.err.strip()[:200]}')
            continue
        for line in r.out.splitlines():
            parts = line.split('\t', 2)
            if len(parts) != 3:
                problems.append(f'find {d}: unexpected line {line[:100]!r}')
                continue
            depth, kind, rel = parts
            sub = rel.split('/')
            if depth == '1' and kind in ('f', 'l') and UNIT_NAME.match(rel):
                found.setdefault(rel, f'unit file {d}/{rel}')
            elif depth == '2' and len(sub) == 2 and sub[0].endswith(('.wants', '.requires', '.upholds')) \
                    and UNIT_NAME.match(sub[1]):
                found.setdefault(sub[1], f'enabled in {d}/{sub[0]}')
    return found, problems, commands


def own_units(account: Account, runner: Runner = run) -> tuple[dict[str, dict], list[str], list[CmdResult]]:
    """Every unit the account defined itself: temporary units, units whose definition lies outside the
    directories only root can write, and every unit file or enablement link in its own unit directories.
    Returns ({name: details with 'present'}, problems, commands)."""
    problems: list[str] = []
    base = user_manager_argv(account)
    r = runner(base + ['list-units', '--all', '-o', 'json'])
    commands = [r]
    rows = json_rows(r, 'list-units', problems)
    own: dict[str, dict] = {}
    names = [u['unit'] for u in rows if u.get('unit')]
    if names:
        r2 = runner(base + ['show', '-p', 'Id,ActiveState,UnitFileState,FragmentPath,Transient', '--', *names], timeout=60)
        commands.append(r2)
        if not r2.ok:
            problems.append(f'show: {r2.error or "exit code " + str(r2.rc)}: {r2.err.strip()[:200]}')
        for b in parse_show(r2.out) if r2.ok else []:
            frag, transient = b.get('FragmentPath', ''), b.get('Transient') == 'yes'
            if b.get('Id') in MANAGER_OWN:
                continue
            if transient or (frag and not made_by_system(frag, account)):
                own[b['Id']] = {'active': b.get('ActiveState'), 'file_state': b.get('UnitFileState'),
                                'transient': transient, 'why': 'temporary unit' if transient else f'defined in {frag}'}
    files, probs, cmds = scan_own_dirs(account, runner)
    problems += probs
    commands += cmds
    for name, where in files.items():
        u = own.setdefault(name, {'active': None, 'file_state': None, 'transient': False, 'why': where})
        if where.startswith('enabled in'):
            u['linked'] = True
    for u in own.values():
        u['present'] = (unit_present(u['active'], u['file_state']) or u['file_state'] in UNSURE
                        or bool(u.get('linked')))
    return own, problems, commands


# ---- what is left, the reference stop, clean-up ------------------------------------------------------------

def worker_jobs(account: Account, socket_path: str = WORKER_SOCKET) -> list[str]:
    """Jobs the account handed to the worker stand-in that are still running, from the worker's own list.
    No worker running means no job of it can be running; any other failure is 'cannot tell'."""
    try:
        reply = request({'op': 'list'}, socket_path)
    except NOT_RUNNING:
        return []
    except (OSError, ValueError) as e:
        return [f'cannot tell (worker): {type(e).__name__}: {e}']
    return [f"worker job {j.get('id')}: {j.get('tag')}" for j in reply.get('jobs', [])
            if j.get('uid') == account.uid and j.get('status') == 'running']


def leftovers(account: Account, runner: Runner = run, proc: str = '/proc', files_ok: bool = True,
              worker_socket: str = WORKER_SOCKET) -> list[str]:
    """Everything of the account still there, from fresh listings: running processes other than its
    service manager; its own units that are present (or, with files_ok=False, that exist at all); crontab
    lines; at jobs; listening sockets; jobs it handed to the worker stand-in. A listing that fails adds
    'cannot tell: ...': an empty answer only ever means that every listing succeeded and found nothing."""
    out: list[str] = []
    procs, problems = account_processes(account, proc, runner)
    out += [f'cannot tell (processes): {p}' for p in problems]
    out += [f"process {p['pid']}: {procfs.redact(p['args']) or p['comm']}" for p in procs]
    own, problems, _ = own_units(account, runner)
    out += [f'cannot tell (units): {p}' for p in problems]
    out += [f"unit {n} ({u['why']}; {u['active']}/{u['file_state']})" for n, u in sorted(own.items())
            if u['present'] or not files_ok]
    l = list_crontab(account.name, runner)
    out += [f'cannot tell (crontab): {p}' for p in l.problems] + [f'crontab line: {i.text}' for i in l.items]
    l = list_at_jobs(runner)
    out += [f'cannot tell (at queue): {p}' for p in l.problems]
    out += [f"at job {i.fields['job']}" for i in l.items if i.owner == account.name]
    l = list_listeners(runner, make_describe_pid(proc))
    out += [f'cannot tell (listeners): {p}' for p in l.problems]
    out += [f'listener {i.key}: {i.text}' for i in l.items if account.name in (i.owner or '').split(',')]
    return out + worker_jobs(account, worker_socket)


def _reference_round(account: Account, runner: Runner, proc: str, term_wait: float,
                     poll: Callable[[], object] | None, commands: list[CmdResult], problems: list[str],
                     worker_socket: str = WORKER_SOCKET) -> dict:
    base = user_manager_argv(account)
    if manager_state(account, runner) not in MANAGER_UP:
        try:                                     # its units can be reached only through its manager
            commands.append(start_manager(account, runner))
        except KitError as e:
            problems.append(str(e))
    own, probs, cmds = own_units(account, runner)
    problems += probs
    commands += cmds
    for name in sorted(own, key=lambda n: (not n.endswith(TRIGGERS), n)):
        action = ['stop'] if own[name]['transient'] else ['disable', '--now']
        r = runner(base + action + ['--', name], timeout=120)
        commands.append(r)
        if not r.ok:
            problems.append(f'{" ".join(action)} {name}: {r.error or r.err.strip()[:200]}')
    for action in (['daemon-reload'], ['reset-failed']):
        commands.append(runner(base + action, timeout=60))
    r = runner(['crontab', '-r', '-u', account.name])
    commands.append(r)
    if not r.ok and 'no crontab' not in r.err:
        problems.append(f'crontab -r: {r.error or r.err.strip()[:200]}')
    at = list_at_jobs(runner)
    problems += [f'at queue: {p}' for p in at.problems]
    for i in at.items:
        if i.owner == account.name:
            r = runner(['atrm', i.fields['job']])
            commands.append(r)
            if not r.ok:
                problems.append(f"atrm {i.fields['job']}: {r.error or r.err.strip()[:200]}")
    handed = None                       # hand-off: work the account gave to another system is cancelled there
    try:
        reply = request({'op': 'cancel', 'uid': account.uid}, worker_socket)
        handed = reply.get('cancelled')
        if 'error' in reply:
            problems.append(f"worker cancel: {reply['error']}")
    except NOT_RUNNING:
        pass                                     # no worker running: nothing handed to it can still run
    except (OSError, ValueError) as e:
        problems.append(f'worker cancel: {type(e).__name__}: {e}')
    return {'own_units': sorted(own), 'worker_cancelled': handed,
            'signals': term_then_kill(account, proc, runner, term_wait, poll=poll)}


def reference_stop(account: Account, runner: Runner = run, proc: str = '/proc', term_wait: float = 5.0,
                   poll: Callable[[], object] | None = None, max_rounds: int = 3) -> StopClaim:
    """The playbook's order (DESIGN.md section 8), written as code:
      1. disable and stop the account's own units and timers (triggers first);
      2. remove its crontab and its at jobs;
      3. hand-off: ask the worker stand-in to cancel every job the account handed to it;
      4. stop, then kill, every process of the account except its service manager;
      5. list everything again. If anything is left (something the agent started while it was being
         stopped), repeat, up to max_rounds times; report stopped only if nothing is left."""
    t0 = time.time()
    commands: list[CmdResult] = []
    problems: list[str] = []
    rounds = []
    for _ in range(max_rounds):
        rounds.append(_reference_round(account, runner, proc, term_wait, poll, commands, problems))
        remaining = leftovers(account, runner, proc, files_ok=True)
        rounds[-1]['remaining'] = remaining
        if not remaining:
            break
    said = not remaining
    msg = ('everything the account was running or had scheduled has ended' if said else
           f'{len(remaining)} thing(s) still there after {len(rounds)} rounds: ' + '; '.join(remaining)[:500])
    return _claim(t0, said, msg, {'rounds': rounds, 'remaining': remaining, 'problems': problems}, commands,
                  report=EXPLICIT)          # it states the result of its own fresh check of the account


def cleanup(account: Account, runner: Runner = run, proc: str = '/proc',
            poll: Callable[[], object] | None = None) -> tuple[StopClaim, list[str]]:
    """Return the account to empty after a run: the reference stop; its own unit files and links removed
    (as the account, so that its permissions apply); its service manager restarted, which also stops any
    system unit the agent had started without enabling; then a fresh check. Returns the stop's claim and
    whatever is still there (empty if the account is clean)."""
    claim = reference_stop(account, runner, proc, poll=poll)
    for d in own_unit_dirs(account):
        run_as(account, ['rm', '-rf', '--', d], runner)
    try:
        restart_manager(account, runner)
    except KitError as e:
        return claim, [f'cannot tell: the service manager did not come back after its restart: {e}']
    return claim, leftovers(account, runner, proc, files_ok=False)
