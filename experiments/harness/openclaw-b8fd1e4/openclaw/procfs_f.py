"""Read-only /proc helpers for the Experiment F spike (SPIKE-F.md section 5): process state, the process chain of a
canary, the search for a run's leftover processes (by nonce marker and by the Gateway's session) and the census of
what the runner's uid still has running after cleanup. Standard library only; every function takes `proc_root` so
the tests can use a fake tree.

/proc/<pid>/stat fields follow proc(5): "pid (comm) state ppid pgrp session ...", where comm may itself contain
spaces and parentheses, so it is cut at the last ")".
"""
import os

PROC = "/proc"
DEAD_STATES = ("Z", "X", "x")   # zombie or dead: the process has exited (proc(5))


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def read_stat(pid, proc_root=PROC):
    """{pid, comm, state, ppid, pgid, sid} from /proc/<pid>/stat, or None if it cannot be read."""
    text = _read_text(os.path.join(proc_root, str(pid), "stat"))
    if not text or "(" not in text or ")" not in text:
        return None
    left, right = text.index("("), text.rindex(")")
    rest = text[right + 1:].split()
    try:
        return {"pid": int(text[:left].strip()), "comm": text[left + 1:right], "state": rest[0],
                "ppid": int(rest[1]), "pgid": int(rest[2]), "sid": int(rest[3])}
    except (IndexError, ValueError):
        return None


def pid_state(pid, proc_root=PROC):
    st = read_stat(pid, proc_root)
    return st["state"] if st else None


def pid_alive(pid, proc_root=PROC):
    """True while the process exists and has not exited (a zombie has exited)."""
    state = pid_state(pid, proc_root)
    return state is not None and state not in DEAD_STATES


def read_cmdline(pid, proc_root=PROC, limit=400):
    raw = _read_bytes(os.path.join(proc_root, str(pid), "cmdline"))
    if raw is None:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()[:limit]


def process_chain(pid, proc_root=PROC, max_depth=32):
    """The process and its ancestors, each with pid, comm, state, ppid, pgid, sid, cgroup and a command-line excerpt,
    up to pid 1 (or the first ancestor that cannot be read)."""
    chain, seen, current = [], set(), pid
    while current and current not in seen and len(chain) < max_depth:
        seen.add(current)
        st = read_stat(current, proc_root)
        if st is None:
            break
        st["cgroup"] = (_read_text(os.path.join(proc_root, str(current), "cgroup")) or "").strip()
        st["cmdline"] = read_cmdline(current, proc_root)
        chain.append(st)
        if current == 1:
            break
        current = st["ppid"]
    return chain


def read_uids(pid, proc_root=PROC):
    """(real uid, effective uid) from the "Uid:" line of /proc/<pid>/status, or None."""
    text = _read_text(os.path.join(proc_root, str(pid), "status"))
    for line in (text or "").splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            try:
                return int(parts[1]), int(parts[2])
            except (IndexError, ValueError):
                return None
    return None


def _live_pids(proc_root):
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    return sorted(int(n) for n in names if n.isdigit())


def pids_in_session(sid, proc_root=PROC):
    """Live processes whose session id is `sid`. The Gateway is started in its own session, so this finds what it
    left behind even in another process group (the spawn broker has its own, background-process.md:136)."""
    found = []
    for pid in _live_pids(proc_root):
        st = read_stat(pid, proc_root)
        if st and st["sid"] == sid and st["state"] not in DEAD_STATES:
            found.append(pid)
    return found


def processes_owned_by(uid, proc_root=PROC, exclude=()):
    """Live processes whose real or effective uid is `uid`, each with pid, comm, state, ppid, pgid, sid, uid and a
    command-line excerpt (the post-cleanup census of what the runner's user still has running)."""
    rows = []
    skip = set(exclude)
    for pid in _live_pids(proc_root):
        if pid in skip:
            continue
        uids = read_uids(pid, proc_root)
        if not uids or int(uid) not in uids:
            continue
        st = read_stat(pid, proc_root)
        if not st or st["state"] in DEAD_STATES:
            continue
        st["uid"] = uids[0]
        st["cmdline"] = read_cmdline(pid, proc_root)
        rows.append(st)
    return rows


def find_pids_with_marker(marker, proc_root=PROC, exclude=()):
    """Live processes whose command line contains `marker` (the run's nonce), excluding this process."""
    needle = marker.encode("utf-8")
    skip = set(exclude) | {os.getpid()}
    found = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid in skip:
            continue
        raw = _read_bytes(os.path.join(proc_root, name, "cmdline"))
        if raw and needle in raw and pid_alive(pid, proc_root):
            found.append(pid)
    return sorted(found)
