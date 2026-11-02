#!/usr/bin/env python3
"""Coordinate one registered Experiment B screen campaign across several Linux VMs, one active trial per VM.

Each worker VM runs exactly one local screen batch, `run_expB.py --freeze`, restricted to the conditions this campaign
assigned to it, under a batch id made of the campaign id and the worker name. A replacement is the runner's own
`--resume` of that batch on the same worker, never a new batch and never another worker. Nothing here runs a trial or
judges its validity: the runner and its frozen scorer do. This file registers the campaign, checks each worker before
a launch, starts the worker's runner inside tmux, copies and verifies the raw evidence, and keeps the append-only
attempt ledger that holds the campaign-wide attempt count.

The same file is the worker-side helper: ssh feeds it on stdin to the VM's system Python, as root, for a read-only
probe, a file manifest or the launch (the `remote` functions below, standard library only). See n8n/CAMPAIGN-B.md.

  python -B n8n/campaign_b.py register SPEC.json [--results-root DIR] [--dry-run]
  python -B n8n/campaign_b.py preflight|launch|status|collect CAMPAIGN_DIR [--worker NAME ...] [--address NAME=IP]
  python -B n8n/campaign_b.py reconcile CAMPAIGN_DIR --worker NAME
"""
import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

CAMPAIGN_SCHEMA = "stopcheck-campaign-b/1"
HOST_PROFILE = "linux-vm"            # the one host profile a cloud campaign declares (RULES-expB B.0)
DEFAULT_SEED = 20261012              # run_expB.DEFAULT_SEED
FREEZE = "n8n/FREEZE-expB.json"
# The coordinator relies on these; each must equal the freeze's source hash at the campaign commit, so the
# coordinator, the runner's trial-id rule and the scorer of the runner's exported reports are the frozen ones.
FROZEN_FILES = ("n8n/campaign_b.py", "n8n/run_expB.py", "n8n/analyze_b.py", "n8n/clockwall_b.py")
SCORER_FILES = ("n8n/analyze_b.py", "n8n/clockwall_b.py")
# Conditions with recovery follow-up reads take about 25 minutes; the others about 8-10 (cloud pilot of 13 Oct:
# B0, PC and B1 6-7 minutes, B1r 19 minutes before its pre-stop interval grew).
LONG_CONDITIONS = ("B1r", "B7", "B8", "B9a", "B9b", "B9c", "B10")
MINUTES = {"long": 25, "other": 9}
REMOTE = {"repo": "/root/stopcheck", "python": "/root/pilot-venv/bin/python", "system_python": "/usr/bin/python3",
          "lock_file": "/run/stopcheck-n8n.lock", "log_dir": "/root/stopcheck-campaign", "test_user": "n8ntest",
          "metadata_url": "http://metadata.google.internal/computeMetadata/v1/instance/"}
SSH = {"user": "stopcheck", "key": "~/.ssh/stopcheck_ed25519", "known_hosts": "~/.ssh/stopcheck_known_hosts"}
# never prompt, never accept an unknown or changed host key, and notice a dead connection
SSH_OPTIONS = ("-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=8")
SESSION_PREFIX = "sc-"
# A running program with one of these file names means an experiment is active (one per VM, across A, B and C).
MARKERS = ("run_expA.py", "run_expB.py", "run_expC.py", "harness_b.py", "harness_b_ns.sh")
DISK_FLOOR_GB = 10.0                 # harness_b.host_checks on linux-vm: n8n/results and /var/lib
SYSTEM_SERVICES = ("postgresql.service", "redis-server.service", "valkey-server.service")
SERVICE_NOT_ENABLED = ("disabled", "masked", "masked-runtime", "not-found")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")
STAMP = r"-\d{8}-\d{6}-\d{9}"        # harness_b.main: <trial>-YYYYmmdd-HHMMSS-<nanoseconds>
GENESIS = "0" * 64
ACCEPTED_MEANS = ("accepted: the attempt's evidence is admitted (the runner accepted it under the frozen scorer, and "
                  "its copy and bindings to this campaign verified); it says nothing about whether the shutdown "
                  "succeeded")


# ============================ worker side: runs on the VM as root, standard library only ============================
def _utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(argv, timeout=60, **kwargs):
    """(exit status, stdout, stderr) of a command; the status is None when it cannot run."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout, done.stderr


def _first_line(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.readline().strip()
    except OSError:
        return None


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _lock_state(path):
    """Whether any process holds a lock on `path`, read from /proc/locks. Unlike run_expB's flock probe it never
    takes the lock, so a probe cannot make a starting launcher refuse its trial; it never creates the file."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {"path": path, "exists": False, "held": False}
    except OSError as exc:
        return {"path": path, "exists": None, "held": None, "error": f"{type(exc).__name__}: {exc}"}
    key = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    try:
        with open("/proc/locks", encoding="utf-8", errors="replace") as stream:
            lines = stream.read().splitlines()
    except OSError as exc:
        return {"path": path, "exists": True, "held": None, "error": f"/proc/locks: {exc}"}
    holders = []
    for line in lines:   # "1: FLOCK  ADVISORY  WRITE <pid> <major hex>:<minor hex>:<inode> 0 EOF"
        for field in line.split():
            m = re.fullmatch(r"([0-9a-f]+):([0-9a-f]+):(\d+)", field)
            if m and (int(m.group(1), 16), int(m.group(2), 16), int(m.group(3))) == key:
                holders.append(line.strip())
    return {"path": path, "exists": True, "held": bool(holders), "holders": holders}


def _ancestors():
    seen, pid = set(), os.getpid()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as stream:
                pid = int(stream.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return seen


def _processes(markers):
    """Running programs with a marker as an argument's file name, except this helper and its ancestors (sudo, the
    ssh session's shell), whose arguments carry the markers as data."""
    mine, wanted, found = _ancestors(), set(markers), []
    for name in _listdir("/proc"):
        if not name.isdigit() or int(name) in mine:
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as stream:
                args = [a.decode("utf-8", "replace") for a in stream.read().split(b"\0") if a]
        except OSError:
            continue
        hits = sorted({os.path.basename(a) for a in args} & wanted)
        if hits:
            found.append({"pid": int(name), "programs": hits, "cmdline": " ".join(args)[:300]})
    return found


def _tmux_sessions(tmux):
    code, out, err = _run([tmux, "list-sessions", "-F", "#{session_name}"], timeout=15)
    if code == 0:
        return {"sessions": [s for s in out.splitlines() if s]}
    text = (err or "").lower()
    if code is not None and ("no server running" in text or "error connecting" in text):
        return {"sessions": []}
    return {"sessions": None, "error": (err or f"exit {code}").strip()[:300]}


def _netns(ip):
    """Network namespaces named like the launcher's own (harness_b_ns.sh: stopcheck-b-<pid>)."""
    code, out, err = _run([ip, "netns", "list"], timeout=15)
    if code != 0:
        return {"names": None, "error": (err or f"exit {code}").strip()[:300]}
    names = [line.split()[0] for line in out.splitlines() if line.split()]
    return {"names": [n for n in names if n.startswith("stopcheck-b-")]}


def _test_user(user, cron_spool):
    """harness_b.make_dirs refuses a trial while the test user has processes or a crontab; that refusal is a
    recorded attempt, so the probe checks both first."""
    import pwd
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return {"user": user, "uid": None, "error": "no such user"}
    pids = []
    for name in _listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", encoding="utf-8", errors="replace") as stream:
                uids = next((line.split()[1:3] for line in stream if line.startswith("Uid:")), [])
        except OSError:
            continue
        if str(uid) in uids:
            pids.append(int(name))
    return {"user": user, "uid": uid, "processes": pids, "crontab": os.path.lexists(os.path.join(cron_spool, user))}


def _free_gb(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return round(st.f_bavail * st.f_frsize / 1e9, 2)   # as common/guards.c_drive_free_gb


def _services(systemctl):
    """harness_b.host_checks' rule: a system service passes only when disabled (or masked, or absent) and inactive."""
    out = {}
    for unit in SYSTEM_SERVICES:
        state = {}
        for verb, key in (("is-enabled", "is_enabled"), ("is-active", "is_active")):
            code, text, err = _run([systemctl, verb, unit], timeout=15)
            lines = text.strip().splitlines()
            state[key] = lines[0].strip() if lines else ("" if code is not None else None)
            if code is None:
                state["error"] = err
        state["ok"] = state["is_enabled"] in SERVICE_NOT_ENABLED and state["is_active"] == "inactive"
        out[unit] = state
    return out


def _ntp(timedatectl):
    code, out, err = _run([timedatectl, "show", "-p", "NTPSynchronized", "--value"], timeout=15)
    return out.strip() if code == 0 else None


def _wsl_kernel():
    """As harness_b._wsl_kernel: True when the kernel release names Microsoft, None when unreadable."""
    release = _first_line("/proc/sys/kernel/osrelease")
    return None if release is None else "microsoft" in release.lower()


def _metadata(base):
    """The GCE instance identity, as run_expB.worker_identity reads it: no proxy, and only from a reply that carries
    Metadata-Flavor: Google."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    out = {}
    for key, path in (("id", "id"), ("name", "name"), ("zone", "zone"), ("machine_type", "machine-type")):
        request = urllib.request.Request(base + path, headers={"Metadata-Flavor": "Google"})
        try:
            with opener.open(request, timeout=3) as reply:
                if reply.headers.get("Metadata-Flavor") != "Google":
                    raise ValueError("the reply lacks Metadata-Flavor: Google")
                out[key] = reply.read(4096).decode("utf-8", "replace").strip()
        except Exception as exc:  # noqa: BLE001  (an unreadable identity is reported, and refuses a launch)
            out["error"] = f"{key}: {type(exc).__name__}: {exc}"[:300]
            break
    return out


def _repo_state(git, repo, freeze):
    base = [git, "-c", f"safe.directory={repo}", "--no-optional-locks", "-C", repo]
    out = {}
    code, text, err = _run(base + ["rev-parse", "HEAD"])
    out["head"] = text.strip() if code == 0 else None
    code2, text2, err2 = _run(base + ["status", "--porcelain"])
    out["status"] = text2 if code2 == 0 else None
    if out["head"] is None or out["status"] is None:
        out["error"] = ((err or "") + (err2 or "")).strip()[:300] or "git failed"
    try:
        out["freeze_sha256"] = _sha256_file(os.path.join(repo, freeze))
    except OSError as exc:
        out["freeze_sha256"] = None
        out["freeze_error"] = f"{type(exc).__name__}: {exc}"
    tracked = _run(base + ["ls-files", "--error-unmatch", "--", freeze])[0] == 0
    unchanged = _run(base + ["diff", "--quiet", "HEAD", "--", freeze])[0] == 0
    out["freeze_committed"] = tracked and unchanged
    return out


def _batch_state(results, batch_id, conditions):
    folder = os.path.join(results, f"XB-screen-{batch_id}")
    out = {"folder": folder, "name": os.path.basename(folder), "exists": os.path.isdir(folder)}
    if out["exists"]:
        try:
            with open(os.path.join(folder, "batch.json"), "rb") as stream:
                raw = stream.read()
            out["sha256"], out["content"] = hashlib.sha256(raw).hexdigest(), json.loads(raw)
        except (OSError, ValueError) as exc:
            out["sha256"], out["content"], out["error"] = None, None, f"{type(exc).__name__}: {exc}"
        out["lock"] = _lock_state(os.path.join(folder, "batch.lock"))
    alternatives = "|".join(map(re.escape, conditions))
    pattern = re.compile(rf"XB-screen-{re.escape(batch_id)}-(?:{alternatives})(?:-a\d+)?{STAMP}")
    out["trial_folders"] = [n for n in _listdir(results)
                            if pattern.fullmatch(n) and os.path.isdir(os.path.join(results, n))]
    return out


def _screen_batches(results):
    found = []
    for name in _listdir(results):
        path = os.path.join(results, name, "batch.json")
        if name.startswith("XB-screen-") and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as stream:
                    found.append({"name": name, "freeze_sha256": json.load(stream).get("freeze_sha256")})
            except (OSError, ValueError, AttributeError) as exc:
                found.append({"name": name, "freeze_sha256": None, "error": f"{type(exc).__name__}: {exc}"})
    return found


def _launch_files(log_dir, worker):
    pattern = re.compile(rf"{re.escape(worker)}-launch-(\d+)\.(sh|start|log|exit)")
    out = {}
    for name in _listdir(log_dir):
        m = pattern.fullmatch(name)
        if m:
            item = out.setdefault(m.group(1), {"files": []})
            item["files"].append(name)
            if m.group(2) in ("start", "exit"):
                item[m.group(2)] = _first_line(os.path.join(log_dir, name))
    return out


def _runner_argv(a, resume):
    argv = [a["python"], "-B", "n8n/run_expB.py", "--freeze", a["freeze"]]
    if resume:
        return argv + ["--resume", f"n8n/results/XB-screen-{a['batch_id']}/batch.json"]
    return argv + ["--conditions", *a["conditions"], "--seed", str(a["seed"]), "--batch-id", a["batch_id"]]


def _dry_run(a, resume):
    """The runner's own plan (`--dry-run`: no services, no files): the order it would use, or what --resume would
    run next."""
    argv = _runner_argv(a, resume) + ["--dry-run"]
    env = dict(os.environ, STOPCHECK_HOST_PROFILE=a["host_profile"])
    code, out, err = _run(argv, timeout=300, cwd=a["repo"], env=env)
    try:
        plan = json.loads(out) if code == 0 else None
    except ValueError:
        plan = None
    return {"argv": argv, "returncode": code, "plan": plan, "stderr": (err or "")[-2000:]}


def remote_probe(a):
    """Read-only facts about one worker. The coordinator decides; this only reports."""
    repo = a["repo"]
    results = os.path.join(repo, "n8n", "results")
    mnt, work_root = a.get("mnt", "/mnt"), a.get("work_root", "/var/lib/stopcheck-n8n")
    out = {"probe": 1, "utc": _utc(), "hostname": socket.gethostname(), "euid": os.geteuid(),
           "boot_id": _first_line("/proc/sys/kernel/random/boot_id"),
           "metadata": _metadata(a["metadata_url"]),
           "repo": _repo_state(a.get("git", "git"), repo, a["freeze"]),
           "wsl_kernel": _wsl_kernel(),
           "lock": _lock_state(a["lock_file"]),
           "processes": _processes(a["markers"]),
           "tmux": _tmux_sessions(a.get("tmux", "tmux")),
           "netns": _netns(a.get("ip", "ip")),
           "test_user": _test_user(a["test_user"], a.get("cron_spool", "/var/spool/cron/crontabs")),
           "disk": {path: _free_gb(path) for path in (results, a.get("var_lib", "/var/lib"))},
           "services": _services(a.get("systemctl", "systemctl")),
           "env_file": os.path.isfile(os.path.join(repo, "n8n", "n8n-test.env")),
           "ntp": _ntp(a.get("timedatectl", "timedatectl")),
           "python": os.path.isfile(a["python"]) and os.access(a["python"], os.X_OK),
           "sentinels": [n for n in _listdir(mnt) if n.startswith("stopcheck-sentinel-")],
           # harness_b refuses a trial whose work directory or host sentinel exists, and that refusal is an attempt
           "trial_paths": {t: {"work_dir": os.path.lexists(os.path.join(work_root, t.lower())),
                               "sentinel": os.path.lexists(os.path.join(mnt, "stopcheck-sentinel-" + t))}
                           for t in a["trials"]},
           "batch": _batch_state(results, a["batch_id"], a["all_conditions"]),
           "screen_batches": _screen_batches(results),
           "launches": _launch_files(a["log_dir"], a["worker"])}
    if a.get("dry_run") and not out["processes"] and out["lock"].get("held") is False:
        out["dry_run"] = _dry_run(a, out["batch"]["exists"])   # never beside a running trial
    return out


def remote_manifest(a):
    """SHA-256 of every regular file under each requested item, with anything else (links, devices) listed, and the
    activity facts again, so the coordinator can refuse a copy of a worker that started running meanwhile."""
    out = {"utc": _utc(), "processes": _processes(a["markers"]), "lock": _lock_state(a["lock_file"]),
           "tmux": _tmux_sessions(a.get("tmux", "tmux")), "items": []}
    for item in a["items"]:
        root, name = item["root"], item["name"]
        path = os.path.join(root, name)
        files, other = {}, []
        if os.path.islink(path) or not os.path.exists(path):
            other.append(f"{name}: missing, or a link")
        elif os.path.isfile(path):
            files[name] = _sha256_file(path)
        else:
            for folder, dirs, names in os.walk(path):
                for entry in dirs + names:
                    full = os.path.join(folder, entry)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    if os.path.islink(full) or not (os.path.isdir(full) or os.path.isfile(full)):
                        other.append(rel)
                    elif os.path.isfile(full):
                        files[rel] = _sha256_file(full)
        out["items"].append({"root": root, "name": name, "files": files, "other": sorted(other)})
    return out


def _launch_script(a, base):
    """The shell script tmux runs: start stamp, the runner with the declared host profile, its exit status."""
    argv = _runner_argv(a, a["mode"] == "resume")
    q = shlex.quote
    stamp = '"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'
    lines = ["#!/bin/sh",
             f"# campaign {a['campaign_id']}, worker {a['worker']}, launch {a['launch_no']}; written by campaign_b.py",
             f"printf '%s\\n' {stamp} > {q(base + '.start.tmp')} && mv {q(base + '.start.tmp')} {q(base + '.start')}",
             f"cd {q(a['repo'])} || {{ printf '97 %s\\n' {stamp} > {q(base + '.exit')}; exit 97; }}",
             f"env STOPCHECK_HOST_PROFILE={q(a['host_profile'])} {' '.join(q(x) for x in argv)} "
             f"> {q(base + '.log')} 2>&1",
             "status=$?",
             f"printf '%s %s\\n' \"$status\" {stamp} > {q(base + '.exit.tmp')} "
             f"&& mv {q(base + '.exit.tmp')} {q(base + '.exit')}",
             'exit "$status"', ""]
    return "\n".join(lines), argv


def remote_launch(a):
    """Recheck the worker immediately before starting, then start the runner in a detached tmux session, so an ssh
    drop cannot end it. The acknowledgement is the start stamp the session's own script writes."""
    tmux = a.get("tmux", "tmux")
    base = f"{a['log_dir']}/{a['worker']}-launch-{a['launch_no']}"
    refusals = []
    procs, lock, sessions = _processes(a["markers"]), _lock_state(a["lock_file"]), _tmux_sessions(tmux)
    if procs:
        refusals.append("StopCheck programs are running: " + "; ".join(p["cmdline"][:120] for p in procs))
    if lock.get("held") is not False:
        refusals.append(f"the launcher lock {lock['path']} is held or unreadable")
    if sessions.get("sessions") is None:
        refusals.append(f"tmux is unavailable: {sessions.get('error')}")
    elif any(s.startswith(a["session_prefix"]) for s in sessions["sessions"]):
        refusals.append("a campaign tmux session is present: " + ", ".join(sessions["sessions"]))
    batch = os.path.join(a["repo"], "n8n", "results", f"XB-screen-{a['batch_id']}", "batch.json")
    try:
        current = _sha256_file(batch)
    except FileNotFoundError:
        current = None
    except OSError as exc:
        current = f"unreadable: {exc}"
    if current != a.get("expect_batch_sha256"):
        refusals.append("batch.json changed after the coordinator's probe")
    if any(os.path.lexists(base + suffix) for suffix in (".sh", ".start", ".log", ".exit")):
        refusals.append(f"launch {a['launch_no']} of {a['worker']} already has files: a duplicate request")
    if refusals:
        return {"ok": False, "refused": refusals}
    os.makedirs(a["log_dir"], mode=0o700, exist_ok=True)
    script, argv = _launch_script(a, base)
    with open(base + ".sh", "x", encoding="utf-8") as stream:
        stream.write(script)
    code, out, err = _run([tmux, "new-session", "-d", "-s", a["session"], f"sh {shlex.quote(base + '.sh')}"],
                          timeout=30)
    if code != 0:
        return {"ok": False, "refused": [f"tmux new-session failed (exit {code}): {(err or '').strip()[:300]}"]}
    deadline = time.monotonic() + float(a.get("ack_timeout", 20))
    while time.monotonic() < deadline:
        start = _first_line(base + ".start")
        if start:
            return {"ok": True, "ack": True, "session": a["session"], "start_utc": start, "script": base + ".sh",
                    "argv": argv}
        time.sleep(0.2)
    return {"ok": False, "ack": False, "script": base + ".sh",
            "error": f"tmux created session {a['session']}, but no start stamp appeared within "
                     f"{a.get('ack_timeout', 20)} s"}


REMOTE_OPS = {"probe": remote_probe, "manifest": remote_manifest, "launch": remote_launch}


def remote_main(argv):
    if len(argv) != 2 or argv[0] not in REMOTE_OPS:
        print(json.dumps({"error": "usage: campaign_b.py remote probe|manifest|launch JSON"}))
        return 2
    print(json.dumps(REMOTE_OPS[argv[0]](json.loads(argv[1])), sort_keys=True))
    return 0


# ============================ coordinator side: the operator's machine ============================
class CampaignError(Exception):
    """A refusal. Nothing changed beyond what the message and the ledger say."""


class RemoteError(Exception):
    """ssh or the worker-side helper failed, so whatever it was asked to do is unconfirmed."""


class Runner:
    """Runs ssh (and tar over ssh); the tests substitute fake workers."""

    def run(self, argv, input=None, timeout=None, stdout_path=None):
        if stdout_path is None:
            return subprocess.run(argv, input=input, capture_output=True, timeout=timeout)
        with open(stdout_path, "xb") as out:
            return subprocess.run(argv, input=input, stdout=out, stderr=subprocess.PIPE, timeout=timeout)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def tail(data, limit=400):
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return (data or "").strip()[-limit:]


def replace(source, target, tries=20):
    """os.replace, retried briefly on Windows, where a virus scanner reading a just-written file denies the rename."""
    for attempt in range(tries):
        try:
            return os.replace(source, target)
        except PermissionError:
            if os.name != "nt" or attempt == tries - 1:
                raise
            time.sleep(0.25)


_RUNNER = []


def runner_module():
    """run_expB.py beside this file: its condition list, attempt limit and trial-id rule (the registered checkout's
    copy is checked against the freeze by Campaign)."""
    if not _RUNNER:
        spec = importlib.util.spec_from_file_location("run_expB_for_campaign_b",
                                                      Path(__file__).resolve().with_name("run_expB.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RUNNER.append(module)
    return _RUNNER[0]


def git(repo, *args):
    return subprocess.run(["git", "-c", f"safe.directory={Path(repo).as_posix()}", "-C", str(repo), *args],
                          capture_output=True)


def git_blob(repo, commit, relative):
    done = git(repo, "cat-file", "blob", f"{commit}:{relative}")
    if done.returncode:
        raise CampaignError(f"{relative} is not committed at {commit}: {tail(done.stderr, 200)}")
    return done.stdout


SOURCE_PATHS = ("n8n", "tests", "common", "langgraph/recorder.py")   # what run_expB.source_hashes reads


def commit_source_hashes(repo, commit):
    """run_expB.source_hashes() as the runner committed at `commit` computes it over that commit's own tree (the
    folders it reads, extracted from git archive into a temporary folder): what every worker's runner will find."""
    listed = git(repo, "ls-tree", "--name-only", commit, "--", *SOURCE_PATHS)
    paths = listed.stdout.decode("utf-8", "replace").split()
    if listed.returncode or "n8n" not in paths:
        raise CampaignError(f"cannot read the tree at {commit}: {tail(listed.stderr, 200)}")
    archived = git(repo, "archive", "--format=tar", commit, "--", *paths)
    if archived.returncode:
        raise CampaignError(f"cannot archive the tree at {commit}: {tail(archived.stderr, 200)}")
    with tempfile.TemporaryDirectory(prefix="campaign-b-tree-") as scratch:
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
            archive.extractall(scratch, filter="data")
        spec = importlib.util.spec_from_file_location("run_expB_at_campaign_commit",
                                                      Path(scratch) / "n8n" / "run_expB.py")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            return module.source_hashes()
        except Exception as exc:  # noqa: BLE001  (a tree the runner cannot fingerprint cannot be registered)
            raise CampaignError(f"the runner at {commit} cannot compute its source hashes: "
                                f"{type(exc).__name__}: {exc}") from None


def run_order(conditions, seed):
    """The order run_expB gives these conditions (it shuffles the --conditions list with --seed)."""
    order = list(conditions)
    random.Random(seed).shuffle(order)
    return order


def balanced_assignment(conditions, workers, minutes):
    """Longest expected duration first, each to the least-loaded worker (ties: the earlier condition and worker)."""
    canonical = list(runner_module().CONDITIONS)
    loads, lists = {w: 0 for w in workers}, {w: [] for w in workers}
    for condition in sorted(conditions, key=lambda c: (-minutes[c], canonical.index(c))):
        worker = min(workers, key=lambda w: (loads[w], workers.index(w)))
        lists[worker].append(condition)
        loads[worker] += minutes[condition]
    return {w: [c for c in canonical if c in lists[w]] for w in workers}


def register(spec_path, results_root, repo, dry_run=False):
    """Validate an operator-written spec against the freeze committed at the campaign commit and write the campaign
    folder: campaign.json, its fingerprint, a copy of the freeze manifest and the ledger's first entry. Returns the
    folder (or the campaign, with dry_run, writing nothing)."""
    R = runner_module()
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    cid = spec.get("campaign_id")
    if not isinstance(cid, str) or not NAME.fullmatch(cid):
        raise CampaignError("campaign_id: 1-32 letters, digits or hyphens, starting with a letter or digit")
    commit = spec.get("source_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise CampaignError("source_commit: the full 40-character commit every worker must have checked out")
    if spec.get("host_profile", HOST_PROFILE) != HOST_PROFILE:
        raise CampaignError(f"host_profile: a cloud campaign declares {HOST_PROFILE}")
    seed = spec.get("seed", DEFAULT_SEED)
    if type(seed) is not int:
        raise CampaignError("seed: an integer")
    freeze_rel = spec.get("freeze", FREEZE)
    if (not isinstance(freeze_rel, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", freeze_rel)
            or ".." in freeze_rel.split("/")):
        raise CampaignError("freeze: a path inside the repository, such as n8n/FREEZE-expB.json")
    freeze_raw = git_blob(repo, commit, freeze_rel)
    try:
        freeze = json.loads(freeze_raw)
    except ValueError as exc:
        raise CampaignError(f"{freeze_rel} at {commit} is not JSON: {exc}") from None
    if freeze.get("schema_version") != 1 or freeze.get("timing_mode") != "full":
        raise CampaignError("the freeze manifest has another schema or timing mode")
    conditions = freeze.get("conditions")
    if (not isinstance(conditions, list) or not conditions or len(set(conditions)) != len(conditions)
            or not set(conditions) <= set(R.CONDITIONS)):
        raise CampaignError("the freeze manifest's condition list is invalid")
    notes = []
    if "host_profile" not in freeze:
        notes.append("the freeze records no host profile; each record's declared profile is checked at collection")
    elif freeze["host_profile"] != HOST_PROFILE:
        raise CampaignError(f"the freeze binds host profile {freeze['host_profile']!r}, not {HOST_PROFILE}")
    hashes = freeze.get("source_hashes") or {}
    frozen = {}
    for rel in FROZEN_FILES:
        committed = sha256_bytes(git_blob(repo, commit, rel))
        if hashes.get(rel) != committed:
            raise CampaignError(f"the freeze does not bind {rel} as committed at {commit}; prepare the freeze after "
                                "committing the coordinator, so its source hashes include it")
        local = Path(repo) / rel
        if not local.is_file() or sha256_bytes(local.read_bytes()) != committed:
            raise CampaignError(f"this checkout's {rel} differs from the campaign commit; run the coordinator "
                                f"from a checkout at {commit}")
        frozen[rel] = committed
    at_commit = commit_source_hashes(repo, commit)
    if at_commit != hashes:
        differ = sorted(k for k in set(at_commit) | set(hashes) if at_commit.get(k) != hashes.get(k))
        raise CampaignError(f"the code at {commit} is not the code the freeze binds (differs: {', '.join(differ[:8])}"
                            f"{' ...' if len(differ) > 8 else ''}); the workers' runners would refuse it")
    workers = spec.get("workers")
    if not isinstance(workers, list) or not workers:
        raise CampaignError("workers: a nonempty list of {name, ip, instance_id}")
    names = [w.get("name") for w in workers if isinstance(w, dict)]
    if len(names) != len(workers) or len(set(names)) != len(names) or not all(
            isinstance(n, str) and NAME.fullmatch(n) for n in names):
        raise CampaignError("workers: unique names of 1-32 letters, digits or hyphens")
    for w in workers:
        if not isinstance(w.get("ip"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", w["ip"]):
            raise CampaignError(f"worker {w['name']}: ip must be an address or host name")
        if not isinstance(w.get("instance_id"), str) or not w["instance_id"].strip():
            raise CampaignError(f"worker {w['name']}: instance_id must be the VM's instance id, as a string")
    minutes = {c: MINUTES["long"] if c in LONG_CONDITIONS else MINUTES["other"] for c in conditions}
    minutes.update({c: m for c, m in (spec.get("expected_minutes") or {}).items() if c in minutes})
    if not all(type(m) in (int, float) and m > 0 for m in minutes.values()):
        raise CampaignError("expected_minutes: positive numbers")
    assignment = spec.get("assignment")
    method = "explicit (spec)"
    if assignment is None:
        assignment = balanced_assignment(conditions, names, minutes)
        method = "longest expected duration first, each to the least-loaded worker"
    if (not isinstance(assignment, dict) or set(assignment) != set(names)
            or not all(isinstance(v, list) and all(isinstance(c, str) for c in v) for v in assignment.values())):
        raise CampaignError("assignment: one list of conditions per registered worker")
    flat = [c for n in names for c in assignment[n]]
    if sorted(flat) != sorted(conditions) or len(set(flat)) != len(flat):
        raise CampaignError("assignment: every frozen condition exactly once, on exactly one worker")
    if any(not assignment[n] for n in names):
        raise CampaignError("assignment: every worker needs at least one condition (a worker without one has no batch)")
    out_workers = []
    for w in workers:
        conds = [c for c in R.CONDITIONS if c in assignment[w["name"]]]
        batch_id = f"{cid}-{w['name']}"
        try:
            R.check_batch_id("screen", batch_id, conds)
        except ValueError as exc:
            raise CampaignError(f"worker {w['name']}: {exc}; shorten the campaign id or worker name") from None
        out_workers.append({"name": w["name"], "ip": w["ip"].strip(), "instance_id": w["instance_id"].strip(),
                            "zone": w.get("zone"), "batch_id": batch_id, "session": SESSION_PREFIX + batch_id,
                            "conditions": conds, "order": run_order(conds, seed),
                            "expected_minutes": sum(minutes[c] for c in conds)})
    campaign = {"schema": CAMPAIGN_SCHEMA, "campaign_id": cid, "registered_utc": utc_now(), "run_kind": "screen",
                "counted": True, "host_profile": HOST_PROFILE, "source_commit": commit,
                "freeze": {"path": freeze_rel, "sha256": sha256_bytes(freeze_raw), "conditions": conditions,
                           "host_profile": freeze.get("host_profile"), "prediction_file": freeze.get("prediction_file"),
                           "prediction_sha256": freeze.get("prediction_sha256"),
                           "copy": "freeze/" + Path(freeze_rel).name},
                "frozen_sources": frozen, "seed": seed, "attempt_limit": R.ATTEMPTS,
                "assignment_policy": {"method": method, "expected_minutes": minutes,
                                      "long_conditions": [c for c in LONG_CONDITIONS if c in conditions],
                                      "one_batch_per_worker": True, "replacement": "--resume on the same worker",
                                      "reassignment": "none"},
                "workers": out_workers, "remote": dict(REMOTE, **(spec.get("remote") or {})),
                "ssh": dict(SSH, **(spec.get("ssh") or {})), "accepted_means": ACCEPTED_MEANS,
                "notes": ([spec["notes"]] if spec.get("notes") else []) + notes}
    text = json.dumps(campaign, indent=2) + "\n"
    if dry_run:
        print(text, end="")
        return campaign
    folder = Path(results_root) / cid
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise CampaignError(f"{folder} exists; a campaign is registered once (choose a new campaign id)") from None
    (folder / "freeze").mkdir()
    (folder / campaign["freeze"]["copy"]).write_bytes(freeze_raw)
    with open(folder / "campaign.json", "x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    fingerprint = sha256_bytes(text.encode("utf-8"))
    with open(folder / "registration.json", "x", encoding="utf-8", newline="\n") as stream:
        json.dump({"campaign_file": "campaign.json", "campaign_sha256": fingerprint, "registered_utc": utc_now()},
                  stream, indent=2)
        stream.write("\n")
    Ledger(folder, fingerprint).append({"type": "registered", "campaign_file": "campaign.json",
                                        "source_commit": commit, "freeze_sha256": campaign["freeze"]["sha256"],
                                        "frozen_sources": frozen, "workers": names, "accepted_means": ACCEPTED_MEANS})
    return folder


class Ledger:
    """ledger.jsonl, append-only: one JSON object per line, each carrying the SHA-256 of the line before it and the
    registered campaign fingerprint; ledger.head names the last line, so removed lines are detected too. Nothing
    here rewrites or deletes a line."""

    def __init__(self, folder, fingerprint):
        self.path = Path(folder) / "ledger.jsonl"
        self.head = Path(folder) / "ledger.head"
        self.fingerprint = fingerprint

    def _read(self):
        raw = self.path.read_bytes() if self.path.exists() else b""
        if raw and not raw.endswith(b"\n"):
            raise CampaignError(f"{self.path} ends in a partial line; inspect it (nothing was appended)")
        lines = raw.split(b"\n")[:-1] if raw else []
        entries, previous = [], GENESIS
        for number, line in enumerate(lines, 1):
            try:
                entry = json.loads(line)
            except ValueError:
                raise CampaignError(f"{self.path} line {number} is not JSON") from None
            if (not isinstance(entry, dict) or entry.get("seq") != number or entry.get("prev_sha256") != previous
                    or entry.get("campaign_sha256") != self.fingerprint):
                raise CampaignError(f"{self.path} line {number} breaks the append-only chain (edited, removed, "
                                    "reordered or from another campaign)")
            previous = sha256_bytes(line)
            entries.append(entry)
        try:
            head = json.loads(self.head.read_text(encoding="utf-8")) if self.head.exists() else None
        except ValueError:
            raise CampaignError(f"{self.head} is not JSON; inspect the ledger") from None
        if lines and not isinstance(head, dict):
            raise CampaignError(f"{self.head} is missing")
        if head is not None:
            n = head.get("seq") if isinstance(head, dict) else None
            if type(n) is not int or not 1 <= n <= len(lines) or sha256_bytes(lines[n - 1]) != head.get("sha256"):
                raise CampaignError(f"{self.path} is shorter than {self.head} records, or its recorded last line "
                                    "changed: lines were removed or edited")
        return entries, lines

    def entries(self):
        return self._read()[0]

    def append(self, entry):
        entries, lines = self._read()
        record = dict(entry, seq=len(entries) + 1, utc=utc_now(), campaign_sha256=self.fingerprint,
                      prev_sha256=sha256_bytes(lines[-1]) if lines else GENESIS)
        line = dumps(record).encode("ascii")
        with open(self.path, "ab") as stream:
            stream.write(line + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary = self.head.with_name(self.head.name + ".tmp")
        temporary.write_text(dumps({"seq": record["seq"], "sha256": sha256_bytes(line)}) + "\n", encoding="utf-8")
        replace(temporary, self.head)
        return record


class Campaign:
    """A registered campaign folder, loaded only if campaign.json still has its registered fingerprint, the ledger
    chain is intact and this checkout's coordinator, runner and scorer are the registered frozen files."""

    def __init__(self, folder, repo):
        self.folder, self.repo = Path(folder).resolve(), Path(repo)
        try:
            raw = (self.folder / "campaign.json").read_bytes()
            registration = json.loads((self.folder / "registration.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CampaignError(f"{self.folder} is not a registered campaign folder: {exc}") from None
        self.fingerprint = sha256_bytes(raw)
        if registration.get("campaign_sha256") != self.fingerprint:
            raise CampaignError(f"campaign.json changed since registration (registered "
                                f"{registration.get('campaign_sha256')}, now {self.fingerprint}); commands refuse "
                                "a changed campaign file")
        self.data = json.loads(raw)
        self.cid = self.data.get("campaign_id")
        if self.data.get("schema") != CAMPAIGN_SCHEMA or self.cid != self.folder.name:
            raise CampaignError("campaign.json has another schema, or its campaign id is not its folder's name")
        self.ledger = Ledger(self.folder, self.fingerprint)
        entries = self.ledger.entries()
        if not entries or entries[0].get("type") != "registered":
            raise CampaignError("the ledger has no registration entry")
        freeze_raw = (self.folder / self.data["freeze"]["copy"]).read_bytes()
        self.freeze_sha = self.data["freeze"]["sha256"]
        if sha256_bytes(freeze_raw) != self.freeze_sha:
            raise CampaignError("the campaign's copy of the freeze manifest changed")
        self.freeze = json.loads(freeze_raw)
        changed = [rel for rel, h in self.data["frozen_sources"].items()
                   if not (self.repo / rel).is_file() or sha256_bytes((self.repo / rel).read_bytes()) != h]
        if changed:
            raise CampaignError(f"this checkout's {', '.join(changed)} differ(s) from the registered frozen source; "
                                f"run the coordinator from a checkout at {self.data['source_commit']}")
        self.workers = {w["name"]: w for w in self.data["workers"]}
        self.remote_log_dir = f"{self.data['remote']['log_dir']}/{self.cid}"

    def owner(self, condition):
        return next((w["name"] for w in self.data["workers"] if condition in w["conditions"]), None)

    def rel(self, path):
        return None if path is None else Path(path).resolve().relative_to(self.folder).as_posix()


# ---------- the ledger's views ----------
def worker_state(entries, name):
    """idle -> launch_requested -> running (acknowledged) or unresolved; reconcile -> running or halted (stopped,
    not yet collected); a verified collect of a stopped worker -> stopped. Only idle and stopped may launch."""
    state = before = "idle"
    for e in entries:
        if e.get("worker") != name:
            continue
        kind = e.get("type")
        if kind == "launch_request":
            before, state = state, "launch_requested"
        elif kind == "launch_ack":
            state = "running"
        elif kind == "launch_unresolved":
            state = "unresolved"
        elif kind == "launch_refused":
            state = before
        elif kind == "reconciled":
            state = "running" if e.get("state") == "running" else "halted"
        elif kind == "collect":
            state = "stopped"
    return state


def latest_attempts(entries):
    """trial id -> the latest attempt entry, in first-seen order."""
    out = {}
    for e in entries:
        if e.get("type") == "attempt":
            out[e["trial"]] = e
    return out


def condition_trials(entries):
    """condition -> every distinct trial id the ledger has seen for it, on any worker, in first-seen order."""
    out = {}
    for trial, e in latest_attempts(entries).items():
        out.setdefault(e["condition"], []).append(trial)
    return out


def last_collect(entries, name):
    return next((e for e in reversed(entries) if e.get("worker") == name and e.get("type") == "collect"), None)


def identity(p):
    p = p or {}
    meta = p.get("metadata") or {}
    return {"instance_id": meta.get("id"), "name": meta.get("name"), "zone": meta.get("zone"),
            "machine_type": meta.get("machine_type"), "boot_id": p.get("boot_id"), "hostname": p.get("hostname"),
            "head": (p.get("repo") or {}).get("head")}


# ---------- worker checks ----------
def ssh_argv(camp, w, command, address=None):
    ssh = camp.data["ssh"]
    known = ["-o", "UserKnownHostsFile=" + os.path.expanduser(ssh["known_hosts"])] if ssh.get("known_hosts") else []
    return ["ssh", "-i", os.path.expanduser(ssh["key"]), *SSH_OPTIONS, *known, f"{ssh['user']}@{address or w['ip']}",
            command]


def remote_call(camp, name, op, args, runner, address=None, timeout=300):
    """Run one worker-side operation: this file, fed to the VM's system Python as root, replying with one JSON line."""
    command = (f"sudo -n {shlex.quote(camp.data['remote']['system_python'])} -I -B - remote {op} "
               f"{shlex.quote(dumps(args))}")
    argv = ssh_argv(camp, camp.workers[name], command, address)
    try:
        done = runner.run(argv, input=Path(__file__).read_bytes(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError(f"{name}: ssh {op} timed out after {timeout} s") from None
    except OSError as exc:
        raise RemoteError(f"{name}: cannot run ssh: {exc}") from None
    if done.returncode != 0:
        raise RemoteError(f"{name}: ssh {op} exited {done.returncode}: {tail(done.stderr)}")
    try:
        return json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError, UnicodeDecodeError):
        raise RemoteError(f"{name}: unreadable reply to {op}: {tail(done.stdout)}") from None


def probe_args(camp, w, dry_run=True):
    R, r = runner_module(), camp.data["remote"]
    trials = [R.trial_id("screen", w["batch_id"], c, n) for c in w["conditions"]
              for n in range(1, camp.data["attempt_limit"] + 1)]
    return {"repo": r["repo"], "python": r["python"], "freeze": camp.data["freeze"]["path"],
            "host_profile": camp.data["host_profile"], "lock_file": r["lock_file"], "log_dir": camp.remote_log_dir,
            "worker": w["name"], "batch_id": w["batch_id"], "conditions": w["conditions"],
            "all_conditions": list(R.CONDITIONS), "seed": camp.data["seed"], "session": w["session"],
            "session_prefix": SESSION_PREFIX, "test_user": r["test_user"], "metadata_url": r["metadata_url"],
            "markers": list(MARKERS), "trials": trials, "dry_run": dry_run}


def busy_reasons(p):
    """What shows that an experiment is active on the worker (a probe or a manifest reply)."""
    out = []
    if p.get("processes"):
        out.append("StopCheck programs are running: " + "; ".join(f"{x['pid']} {x['cmdline'][:100]}"
                                                                   for x in p["processes"]))
    lock = p.get("lock") or {}
    if lock.get("held") is not False:
        out.append(f"the launcher lock {lock.get('path')} is held or unreadable ({lock.get('error') or 'held'})")
    ours = [s for s in (p.get("tmux") or {}).get("sessions") or [] if s.startswith(SESSION_PREFIX)]
    if ours:
        out.append("campaign tmux session(s) present: " + ", ".join(ours))
    return out


def parse_trial(w, trial):
    R = runner_module()
    for condition in w["conditions"]:
        for attempt in range(1, 10):   # beyond the limit too, so an over-limit attempt is recognised and refused
            if R.trial_id("screen", w["batch_id"], condition, attempt) == trial:
                return condition, attempt
    return None


def batch_problems(camp, w, batch):
    """The batch must be the registered one for this worker: its id, run kind, freeze, seed, order and code."""
    out = []
    for key, value in (("run_kind", "screen"), ("batch_id", w["batch_id"]), ("freeze_sha256", camp.freeze_sha),
                       ("seed", camp.data["seed"]), ("conditions", w["order"])):
        if batch.get(key) != value:
            out.append(f"batch.json {key} is {batch.get(key)!r}, registered {value!r}")
    if "host_profile" in batch and batch["host_profile"] != camp.data["host_profile"]:
        out.append(f"batch.json host_profile is {batch['host_profile']!r}, registered {camp.data['host_profile']!r}")
    if batch.get("source_hashes") != camp.freeze.get("source_hashes"):
        out.append("batch.json source hashes differ from the freeze's")
    return out


def assess(camp, w, p):
    """Every reason this probe forbids starting the worker's runner now, with warnings, the batch mode (new or
    resume) and the attempts (condition, attempt, trial id) the runner's own dry run says it would start. Checks
    the harness would otherwise fail at a trial's start (which uses up an attempt) are included."""
    R = runner_module()
    reasons, warnings = [], []

    def need(ok, why):
        if not ok:
            reasons.append(why)
    meta, repo = p.get("metadata") or {}, p.get("repo") or {}
    commit, profile = camp.data["source_commit"], camp.data["host_profile"]
    need(meta.get("id") == w["instance_id"], f"VM identity: the metadata server reports instance id "
         f"{meta.get('id')!r} ({meta.get('error') or meta.get('name')}), registered {w['instance_id']!r}")
    need(repo.get("head") == commit,
         f"repository HEAD is {repo.get('head') or repo.get('error')!r}, not the campaign commit {commit}")
    need(repo.get("status") == "", "the working tree is not clean: " + repr((repo.get("status") or repo.get("error")
                                                                            or "")[:300]))
    need(repo.get("freeze_sha256") == camp.freeze_sha, f"{camp.data['freeze']['path']} has SHA-256 "
         f"{repo.get('freeze_sha256') or repo.get('freeze_error')!r}, registered {camp.freeze_sha}")
    need(repo.get("freeze_committed") is True, "the freeze file is not committed unchanged at HEAD")
    need(p.get("wsl_kernel") is False,
         f"host profile {profile}: the kernel reads as WSL, or cannot be read ({p.get('wsl_kernel')!r})")
    reasons.extend(busy_reasons(p))
    tmux, netns, user = p.get("tmux") or {}, p.get("netns") or {}, p.get("test_user") or {}
    need(tmux.get("sessions") is not None, f"tmux is unavailable: {tmux.get('error')}")
    need(netns.get("names") == [],
         f"leftover StopCheck network namespaces, or an unreadable list: {netns.get('names') or netns.get('error')}")
    need(user.get("uid") is not None, f"test user {user.get('user')}: {user.get('error')}")
    need(not user.get("processes"), f"test user {user.get('user')} has processes {user.get('processes')}")
    need(not user.get("crontab"), f"test user {user.get('user')} has a crontab")
    for path, free in (p.get("disk") or {}).items():
        need(free is not None and free >= DISK_FLOOR_GB, f"disk floor: {free} GB free on {path}, floor {DISK_FLOOR_GB}")
    for unit, state in (p.get("services") or {}).items():
        need(state.get("ok") is True, f"system service {unit}: is-enabled {state.get('is_enabled')!r}, is-active "
             f"{state.get('is_active')!r} (must be disabled and inactive)")
    need(p.get("env_file") is True, "n8n/n8n-test.env is missing on the worker")
    need(p.get("python") is True, f"the runner interpreter {camp.data['remote']['python']} is missing")
    if p.get("ntp") != "yes":
        warnings.append(f"NTPSynchronized reads {p.get('ntp')!r}; the runner's clock guard decides")
    if p.get("sentinels"):
        warnings.append("leftover host sentinels: " + ", ".join(p["sentinels"]))
    batch = p.get("batch") or {}
    others = [b["name"] for b in p.get("screen_batches") or []
              if b.get("name") != batch.get("name") and b.get("freeze_sha256") in (camp.freeze_sha, None)]
    need(not others, f"another screen batch on this worker uses this freeze, or is unreadable: {', '.join(others)}")
    mode = "resume" if batch.get("exists") else "new"
    content = batch.get("content") if isinstance(batch.get("content"), dict) else None
    if mode == "resume":
        if content is None:
            reasons.append(f"batch.json is unreadable: {batch.get('error')}")
        else:
            reasons.extend(batch_problems(camp, w, content))
            if content.get("completed"):
                reasons.append("the batch is completed: nothing to launch")
        need((batch.get("lock") or {}).get("held") is False, "the batch lock is held or unreadable")
    expected, dry = [], p.get("dry_run")
    if dry is None:
        need(reasons, "the runner's dry run did not run")
    elif dry.get("returncode") != 0 or not isinstance(dry.get("plan"), dict):
        need(content is not None and content.get("completed"),
             f"the runner's dry run failed (exit {dry.get('returncode')}): {tail(dry.get('stderr'), 300)}")
    else:
        plan = dry["plan"]
        need(plan.get("run_kind") == "screen" and plan.get("requires_freeze") is True,
             f"the runner's dry run plans {plan.get('run_kind')!r}, not a screen")
        need(plan.get("host_profile", profile) == profile, f"the runner's dry run declares {plan.get('host_profile')!r}")
        need(plan.get("conditions") == w["order"],
             f"the runner's order {plan.get('conditions')} differs from the registered order {w['order']}")
        if mode == "new":
            expected = [(c, 1, R.trial_id("screen", w["batch_id"], c, 1)) for c in w["order"]]
        else:
            for trial in plan.get("next_trials") or []:
                parsed = parse_trial(w, trial)
                if parsed is None:
                    reasons.append(f"the runner would start {trial}, which is not a trial of this worker's batch")
                else:
                    expected.append(parsed + (trial,))
    for condition, attempt, trial in expected:
        paths = (p.get("trial_paths") or {}).get(trial) or {"work_dir": None}
        need(paths.get("work_dir") is False and paths.get("sentinel") is False,
             f"{trial}: its work directory or host sentinel exists or was not checked (the harness would refuse it)")
    return {"reasons": reasons, "warnings": warnings, "mode": mode, "expected": expected}


def ledger_checks(camp, w, p, result, entries):
    """What the ledger forbids: an uncollected batch, a batch that lost attempts the ledger recorded, a condition
    registered elsewhere, and any attempt beyond the campaign-wide limit, counted on every worker."""
    name, limit, reasons = w["name"], camp.data["attempt_limit"], []
    mine = [a for a in latest_attempts(entries).values() if a.get("worker") == name]
    batch = p.get("batch") or {}
    if result["mode"] == "new":
        if mine:
            reasons.append(f"the ledger holds {len(mine)} attempt(s) of this worker, but its batch folder is missing")
    else:
        last = last_collect(entries, name)
        if last is None or last.get("batch_json_sha256") != batch.get("sha256"):
            reasons.append("batch.json changed since the last verified collect, or was never collected: collect first")
        live = {t.get("trial") for t in ((batch.get("content") or {}).get("trials") or [])}
        lost = sorted(a["trial"] for a in mine if a["trial"] not in live)
        if lost:
            reasons.append("batch.json no longer lists attempts the ledger recorded (a restored or edited batch): "
                           + ", ".join(lost))
    counts = condition_trials(entries)
    for condition, attempt, trial in result["expected"]:
        seen = counts.get(condition, [])
        if camp.owner(condition) != name:
            reasons.append(f"{condition} is registered to {camp.owner(condition)}, not {name}")
        if len(seen) >= limit:
            reasons.append(f"{condition}: the campaign-wide limit of {limit} attempts is reached ({', '.join(seen)})")
        elif trial in seen:
            reasons.append(f"{trial} is already in the ledger; trial ids are never reused")
        elif attempt != len(seen) + 1:
            reasons.append(f"{condition}: the runner would start attempt {attempt}, but the ledger holds {len(seen)} "
                           f"({', '.join(seen) or 'none'}); the batch and the ledger disagree")
    if not result["expected"] and not result["reasons"] and not reasons:
        reasons.append("nothing left to run: every condition is accepted or has used its attempts")
    return reasons


# ---------- commands ----------
def preflight(camp, names, runner, addresses):
    failed = 0
    for name in names:
        w, p = camp.workers[name], None
        try:
            p = remote_call(camp, name, "probe", probe_args(camp, w), runner, addresses.get(name))
            result = assess(camp, w, p)
        except RemoteError as exc:
            result = {"reasons": [f"unreachable, or the probe failed: {exc}"], "warnings": [], "mode": None}
        camp.ledger.append({"type": "preflight", "worker": name, "purpose": "preflight", "ok": not result["reasons"],
                            "reasons": result["reasons"], "warnings": result["warnings"], "mode": result["mode"],
                            "identity": identity(p), "address": addresses.get(name)})
        failed += bool(result["reasons"])
        print(f"{name}: " + ("ready" if not result["reasons"] else "NOT READY") +
              (f" ({result['mode']} batch)" if result["mode"] else ""))
        for line in [f"  refused: {r}" for r in result["reasons"]] + [f"  warning: {x}" for x in result["warnings"]]:
            print(line)
    return 1 if failed else 0


def launch_worker(camp, name, runner, address=None):
    """Start one worker's runner, recording the request before it is sent and the worker's acknowledgement after.
    A lost reply leaves the worker unresolved until reconcile."""
    w = camp.workers[name]
    entries = camp.ledger.entries()
    state = worker_state(entries, name)
    if state not in ("idle", "stopped"):
        why = {"running": "it is running; wait for it to stop, then collect",
               "launch_requested": "a launch was requested and never acknowledged: reconcile first",
               "unresolved": "its last launch is unresolved: reconcile first",
               "halted": "it stopped since its last launch, but was not collected: collect first"}[state]
        raise CampaignError(f"{name}: launch refused: {why}")
    try:
        p = remote_call(camp, name, "probe", probe_args(camp, w), runner, address)
    except RemoteError as exc:
        raise CampaignError(f"{name}: launch refused: the probe failed, nothing was started: {exc}") from None
    result = assess(camp, w, p)
    reasons = result["reasons"] + ledger_checks(camp, w, p, result, entries)
    camp.ledger.append({"type": "preflight", "worker": name, "purpose": "launch", "ok": not reasons,
                        "reasons": reasons, "warnings": result["warnings"], "mode": result["mode"],
                        "identity": identity(p), "address": address})
    if reasons:
        raise CampaignError(f"{name}: launch refused: " + "; ".join(reasons))
    number = 1 + sum(1 for e in entries if e.get("worker") == name and e.get("type") == "launch_request")
    trials = [t for _, _, t in result["expected"]]
    args = dict(probe_args(camp, w), campaign_id=camp.cid, mode=result["mode"], launch_no=number,
                expect_batch_sha256=(p.get("batch") or {}).get("sha256"), ack_timeout=20)
    camp.ledger.append({"type": "launch_request", "worker": name, "launch_no": number, "mode": result["mode"],
                        "expected_trials": trials, "identity": identity(p), "address": address})
    try:
        reply = remote_call(camp, name, "launch", args, runner, address, timeout=120)
    except RemoteError as exc:
        camp.ledger.append({"type": "launch_unresolved", "worker": name, "launch_no": number, "error": str(exc)})
        raise CampaignError(f"{name}: launch {number} is unresolved (it may have started): {exc}; reconcile") from None
    if reply.get("refused"):
        camp.ledger.append({"type": "launch_refused", "worker": name, "launch_no": number,
                            "reasons": reply["refused"]})
        raise CampaignError(f"{name}: the worker refused launch {number}, nothing started: "
                            + "; ".join(reply["refused"]))
    if not reply.get("ack"):
        camp.ledger.append({"type": "launch_unresolved", "worker": name, "launch_no": number,
                            "error": reply.get("error")})
        raise CampaignError(f"{name}: launch {number} is unresolved: {reply.get('error')}; reconcile")
    camp.ledger.append({"type": "launch_ack", "worker": name, "launch_no": number, "session": reply["session"],
                        "start_utc": reply["start_utc"], "argv": reply["argv"]})
    print(f"{name}: launch {number} acknowledged ({result['mode']} batch {w['batch_id']}, session {reply['session']},"
          f" started {reply['start_utc']}); may start: {', '.join(trials)}")


def batch_summary(content):
    if not isinstance(content, dict):
        return "no batch yet"
    parts = []
    for condition in content.get("conditions") or []:
        marks = []
        for t in content.get("trials") or []:
            if t.get("condition") == condition:
                status = t.get("status")
                if status in ("finished", "interrupted") and "accepted" in t:
                    status = "accepted" if t["accepted"] else f"not accepted ({status})"
                marks.append(status)
        parts.append(f"{condition}:{'/'.join(marks) or '-'}")
    flags = (["completed"] if content.get("completed") else []) + \
            ([f"stopped: {content['stopped_reason']}"] if content.get("stopped_reason") else []) + \
            ([f"incomplete: {', '.join(content['incomplete'])}"] if content.get("incomplete") else [])
    return " ".join(parts) + (f" [{'; '.join(flags)}]" if flags else "")


def status(camp, names, runner, addresses, no_ssh=False):
    """Read-only: the ledger's view of each worker and, unless --no-ssh, what the worker shows now."""
    entries = camp.ledger.entries()
    for name in names:
        w = camp.workers[name]
        print(f"{name} ({w['ip']}, instance {w['instance_id']}): ledger {worker_state(entries, name)}; batch "
              f"{w['batch_id']}; order {' '.join(w['order'])}")
        if no_ssh:
            continue
        try:
            p = remote_call(camp, name, "probe", probe_args(camp, w), runner, addresses.get(name))
        except RemoteError as exc:
            print(f"  live: unreachable ({exc})")
            continue
        busy = busy_reasons(p)
        meta = (p.get("metadata") or {}).get("id")
        print(f"  live: {'; '.join(busy) or 'no StopCheck activity'}" +
              ("" if meta == w["instance_id"] else f"; INSTANCE ID {meta!r} IS NOT THE REGISTERED ONE"))
        print(f"  batch: {batch_summary((p.get('batch') or {}).get('content'))}")
        plan = (p.get("dry_run") or {}).get("plan") or {}
        if plan.get("next_trials") is not None:
            print(f"  --resume would run: {', '.join(plan['next_trials']) or 'nothing'}")
    latest, limit = latest_attempts(entries), camp.data["attempt_limit"]
    print("ledger, by condition (accepted = evidence admitted, not shutdown success):")
    for w in camp.data["workers"]:
        for condition in w["order"]:
            tries = [a for a in latest.values() if a["condition"] == condition]
            text = "; ".join(f"{a['trial']} {a['status']}, {'accepted' if a['accepted'] else 'not accepted'}"
                             for a in tries) or "no attempt collected"
            print(f"  {condition:4} {w['name']}: {len(tries)}/{limit} attempts: {text}")
    return 0


def manifest_text(files):
    return "".join(f"{files[rel]}  {rel}\n" for rel in sorted(files, key=lambda r: r.encode("utf-8")))


def verify_tree(root, expected):
    """Every regular file under root against the worker's manifest: the same relative file set, the same hashes,
    nothing that is not a regular file."""
    problems, actual = [], {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            problems.append(f"not a regular file after the copy: {rel}")
        elif path.is_file():
            actual[rel] = _sha256_file(path)
    problems += [f"missing after the copy: {rel}" for rel in sorted(set(expected) - set(actual))]
    problems += [f"not in the worker's manifest: {rel}" for rel in sorted(set(actual) - set(expected))]
    problems += [f"SHA-256 differs from the worker's: {rel}" for rel in sorted(set(expected) & set(actual))
                 if expected[rel] != actual[rel]]
    return problems


def fetch(camp, name, area, root, names, target, staging, runner, address):
    """Stream `tar -cz` of the named entries from the worker into a local file, then extract it into staging/area
    (tarfile's data filter: no absolute paths, no escapes, no device files)."""
    for n in names:
        if not SAFE_NAME.fullmatch(n):
            raise CampaignError(f"refusing to copy an unexpected name: {n!r}")
    command = f"sudo -n tar -C {shlex.quote(root)} -czf - -- " + " ".join(shlex.quote(n) for n in names)
    try:
        done = runner.run(ssh_argv(camp, camp.workers[name], command, address), timeout=3600, stdout_path=target)
    except subprocess.TimeoutExpired:
        raise RemoteError(f"{name}: tar over ssh timed out") from None
    if done.returncode:
        raise RemoteError(f"{name}: tar over ssh exited {done.returncode}: {tail(done.stderr)}")
    (staging / area).mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "r:gz") as archive:
        archive.extractall(staging / area, filter="data")
    target.unlink()


def native_reasons(entry):
    out, status = [], entry.get("status")
    if status not in ("finished", "interrupted"):
        out.append(f"status {status}: the runner recorded no end for this attempt; it still counts")
    if status == "interrupted" or entry.get("interrupted"):
        out.append("interrupted")
    if status in ("finished", "interrupted") and entry.get("exit_code") != 0:
        out.append(f"exit code {entry.get('exit_code')}")
    if "scored_valid" in entry and entry.get("scored_valid") is not True:
        out.append(f"scorer: {entry.get('validity')}")
    if status in ("finished", "interrupted") and "accepted" not in entry:
        out.append("no acceptance verdict recorded yet (the runner judges it on its next resume)")
    return out


def record_problems(camp, w, entry, record):
    """The raw record must be this trial's screen record, on the declared host profile, bound to this freeze's
    verified batch entry and frozen code, and (when the runner recorded it) on the registered instance."""
    problems, notes = [], []
    for key in ("trial", "condition"):
        if record.get(key) != entry.get(key):
            problems.append(f"record.json {key} is {record.get(key)!r}, batch.json says {entry.get(key)!r}")
    if record.get("run_kind") != "screen":
        problems.append(f"record.json run kind is {record.get('run_kind')!r}")
    declared = (record.get("host_profile") or {}).get("declared") if isinstance(record.get("host_profile"), dict) \
        else None
    if declared != camp.data["host_profile"]:
        problems.append(f"record.json declares host profile {declared!r}, the campaign {camp.data['host_profile']!r}")
    if record.get("freeze_manifest_sha256") != camp.freeze_sha:
        problems.append("record.json is bound to another freeze manifest")
    link = record.get("batch") if isinstance(record.get("batch"), dict) else {}
    if link.get("verified") is not True or link.get("freeze_sha256") != camp.freeze_sha:
        problems.append("record.json has no verified batch entry of this freeze")
    if record.get("source_hashes") != camp.freeze.get("source_hashes"):
        problems.append("record.json source hashes differ from the freeze's")
    recorded = ((record.get("host_identity") or {}).get("values") or {}).get("id") \
        if isinstance(record.get("host_identity"), dict) else None
    if recorded is not None and recorded != w["instance_id"]:
        problems.append(f"record.json was written on instance {recorded}, registered {w['instance_id']}")
    elif recorded is None:
        notes.append("record.json carries no instance id from the runner")
    return problems, notes


def export_check(camp, results, entry):
    """The runner's own export of this attempt (run_expB.export_attempt, written when the attempt ended): its manifest
    must be in the collection with the SHA-256 batch.json records, every raw file and report it lists must be there
    with its hash, the raw folders must hold nothing it does not list, and its reports must come from the frozen
    scorer. Returns (reasons, notes, export) with export = (manifest SHA-256, derived folder, scorer sources)."""
    reasons, notes, export = [], [], entry.get("export")
    if not isinstance(export, dict):
        if entry.get("status") in ("finished", "interrupted", "unrecorded"):
            notes.append("the runner has not exported this attempt yet (it exports it on the next resume)")
        return reasons, notes, None
    if export.get("status") != "exported":
        notes.append(f"the runner's export failed: {export.get('error')}")
        return reasons, notes, None
    path = results / str(export.get("manifest"))
    if not path.is_file() or _sha256_file(path) != export.get("manifest_sha256"):
        reasons.append("the runner's export manifest is missing from the collection or differs from the SHA-256 "
                       "batch.json records")
        return reasons, notes, None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        listed = {item["path"]: item["sha256"]
                  for item in (manifest.get("raw_files") or []) + (manifest.get("derived_files") or [])}
        folders = [str(folder) for folder in manifest.get("raw_folders") or []]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        reasons.append(f"the runner's export manifest is unreadable: {type(exc).__name__}: {exc}")
        return reasons, notes, None
    if manifest.get("trial") != entry.get("trial"):
        reasons.append(f"the runner's export manifest is for {manifest.get('trial')!r}")
    differ = sorted(rel for rel, digest in listed.items()
                    if not (results / rel).is_file() or _sha256_file(results / rel) != digest)
    unlisted = sorted(p.relative_to(results).as_posix() for folder in folders
                      for p in (results / folder).rglob("*") if p.is_file()
                      and p.relative_to(results).as_posix() not in listed)
    if differ or unlisted:
        reasons.append("the raw folder differs from the runner's export manifest, written when the attempt ended: "
                       + ", ".join((differ + unlisted)[:5]))
    scorer = manifest.get("scorer_sources")
    if scorer != {f: camp.data["frozen_sources"][f] for f in SCORER_FILES}:
        reasons.append(f"the runner's reports come from another scorer: {scorer}")
    return reasons, notes, (export["manifest_sha256"], manifest.get("derived_folder"), scorer)


SUBSTANTIVE = ("status", "exit_code", "started_utc", "ended_utc", "attempt", "rerun_of", "native_validity",
               "runner_accepted", "accepted", "reasons", "raw_manifest_sha256", "record_sha256",
               "export_manifest_sha256", "derived_report")


def attempt_records(camp, w, final, batch, items, entries, seen_identity, round_no):
    """One ledger attempt record per batch.json entry, from the verified copy, with findings for the collect entry."""
    name, bid = w["name"], w["batch_id"]
    results = final / "results"
    folders = {}
    for child in sorted(results.iterdir()) if results.is_dir() else []:
        m = re.fullmatch(rf"(XB-screen-{re.escape(bid)}-.+?){STAMP}", child.name)
        if m and child.is_dir():
            folders.setdefault(m.group(1), []).append(child.name)
    trials = [t for t in batch.get("trials") or [] if isinstance(t, dict)]
    listed = {t.get("trial") for t in trials}
    previous = latest_attempts(entries)
    findings = {"batch_problems": batch_problems(camp, w, batch),
                "unlisted_folders": sorted(n for t, ns in folders.items() if t not in listed for n in ns),
                "lost_attempts": sorted(t for t, a in previous.items() if a.get("worker") == name and t not in listed)}
    counts = {c: list(ts) for c, ts in condition_trials(entries).items()}
    records = []
    for entry in trials:
        trial, condition = entry.get("trial"), entry.get("condition")
        reasons, notes = native_reasons(entry) + findings["batch_problems"], []
        names = folders.get(trial, [])
        raw = manifest_sha = record_sha = None
        earlier = previous.get(trial)
        if len(names) == 1:
            raw = results / names[0]
            manifest_sha = items[f"results/{names[0]}"]["manifest_sha256"]
            named = str(entry.get("record") or "").rsplit("/", 1)[-1]
            if named and named != names[0]:
                reasons.append(f"batch.json names raw folder {named}, but {names[0]} was collected")
            if (raw / "record.json").is_file():
                record_sha = sha256_bytes((raw / "record.json").read_bytes())
                try:
                    record = json.loads((raw / "record.json").read_text(encoding="utf-8"))
                except ValueError as exc:
                    record = None
                    reasons.append(f"record.json is unreadable: {exc}")
                if isinstance(record, dict):
                    problems, record_notes = record_problems(camp, w, entry, record)
                    reasons, notes = reasons + problems, notes + record_notes
            elif entry.get("status") in ("finished", "interrupted"):
                reasons.append("the raw trial folder has no record.json")
            if earlier and earlier.get("raw_manifest_sha256") not in (None, manifest_sha):
                reasons.append("the raw trial folder changed after an earlier verified collection")
        elif names:
            reasons.append("several raw folders for one trial: " + ", ".join(names))
        elif entry.get("status") in ("finished", "interrupted"):
            reasons.append("no raw trial folder for this attempt was found on the worker")
        else:
            notes.append("no raw trial folder: the attempt has no recorded end")
        ran_on = ((entry.get("host_identity") or {}).get("values") or {}).get("id") \
            if isinstance(entry.get("host_identity"), dict) else None
        if ran_on is not None and ran_on != w["instance_id"]:
            reasons.append(f"the runner read instance id {ran_on} for this attempt, registered {w['instance_id']}")
        elif ran_on is None:
            notes.append("the batch entry carries no instance id; the coordinator read "
                         f"{seen_identity.get('instance_id')} at this collection")
        export_reasons, export_notes, export = export_check(camp, results, entry)
        reasons, notes = reasons + export_reasons, notes + export_notes
        export_sha, derived, scorer = export or (None, None, None)
        if earlier:
            global_attempt = earlier.get("global_attempt")
        else:
            counts.setdefault(condition, []).append(trial)
            global_attempt = len(counts[condition])
        runner_accepted = entry.get("accepted") is True
        records.append({"type": "attempt", "worker": name, "job": f"{camp.cid}-{condition}", "condition": condition,
                        "instance_id": seen_identity.get("instance_id"), "batch_id": bid, "trial": trial,
                        "attempt": entry.get("attempt"), "global_attempt": global_attempt,
                        "rerun_of": entry.get("rerun_of"), "status": entry.get("status"),
                        "started_utc": entry.get("started_utc"), "ended_utc": entry.get("ended_utc"),
                        "exit_code": entry.get("exit_code"),
                        "native_validity": {"scored_valid": entry.get("scored_valid"), "validity": entry.get("validity")},
                        "runner_accepted": runner_accepted, "accepted": runner_accepted and not reasons,
                        "reasons": reasons, "notes": notes, "raw_folder": camp.rel(raw),
                        "raw_manifest_sha256": manifest_sha, "record_sha256": record_sha,
                        "export_manifest_sha256": export_sha,
                        "derived_report": camp.rel(results / derived) if derived else None, "derived_by": scorer,
                        "collection": round_no})
    return records, findings


def collect_worker(camp, name, runner, address=None):
    """Copy the worker's batch folder (with the runner's derived reports and export manifests), trial folders and
    launch logs into a new numbered collection, verify every file against the worker's own SHA-256 manifest, and only
    then append the collect entry and the attempt records. A worker that is running, unresolved or not the registered
    instance is refused; collected files are never changed."""
    w = camp.workers[name]
    entries = camp.ledger.entries()
    if worker_state(entries, name) in ("launch_requested", "unresolved"):
        raise CampaignError(f"{name}: its last launch is unresolved: reconcile first")
    try:
        p = remote_call(camp, name, "probe", probe_args(camp, w, dry_run=False), runner, address)
    except RemoteError as exc:
        raise CampaignError(f"{name}: not collected: {exc}") from None
    seen = identity(p)
    if seen["instance_id"] != w["instance_id"]:
        raise CampaignError(f"{name}: not collected: the metadata server reports instance {seen['instance_id']!r}, "
                            f"registered {w['instance_id']!r}")
    if busy_reasons(p):
        raise CampaignError(f"{name}: running ({'; '.join(busy_reasons(p))}); collect after its runner stops")
    r, batch = camp.data["remote"], p.get("batch") or {}
    items = [("results", f"{r['repo']}/n8n/results", n)
             for n in ([batch["name"]] if batch.get("exists") else []) + list(batch.get("trial_folders") or [])]
    for number in sorted(p.get("launches") or {}, key=int):
        items += [("launch", camp.remote_log_dir, n) for n in sorted(p["launches"][number]["files"])]
    last = last_collect(entries, name)
    if not items:
        camp.ledger.append({"type": "collect", "worker": name, "round": None, "path": None, "items": {},
                            "batch_json_sha256": None, "runner_state": "stopped", "identity": seen,
                            "address": address})
        print(f"{name}: nothing to collect (no batch folder and no launch files)")
        return
    try:
        m = remote_call(camp, name, "manifest", {"items": [{"root": root, "name": n} for _, root, n in items],
                                                 "markers": list(MARKERS), "lock_file": r["lock_file"]},
                        runner, address, timeout=900)
    except RemoteError as exc:
        raise CampaignError(f"{name}: not collected: {exc}") from None
    if busy_reasons(m):
        raise CampaignError(f"{name}: started running during the collection ({'; '.join(busy_reasons(m))})")
    problems, expected, per_item = [], {}, {}
    for (area, _, n), item in zip(items, m["items"]):
        problems += [f"{area}/{x}: not a regular file on the worker" for x in item["other"]]
        for rel, digest in item["files"].items():
            if "\n" in rel or "\\" in rel:
                problems.append(f"{area}/{rel!r}: unsupported file name")
            expected[f"{area}/{rel}"] = digest
            per_item.setdefault(f"{area}/{n}", {})[f"{area}/{rel}"] = digest
    texts = {key: manifest_text(files) for key, files in per_item.items()}
    hashes = {key: {"files": len(per_item[key]), "manifest_sha256": sha256_bytes(texts[key].encode("utf-8"))}
              for key in per_item}
    batch_key = f"results/{batch.get('name')}/batch.json"
    if last and last.get("items") == hashes and not problems:
        camp.ledger.append({"type": "collect", "worker": name, "round": last.get("round"), "path": last.get("path"),
                            "unchanged": True, "items": hashes, "batch_json_sha256": expected.get(batch_key),
                            "runner_state": "stopped", "identity": seen, "address": address})
        print(f"{name}: unchanged since collection {last.get('round')}; nothing copied")
        return
    wdir = camp.folder / name
    wdir.mkdir(exist_ok=True)
    used = [e.get("round") for e in entries if e.get("worker") == name and type(e.get("round")) is int]
    used += [int(x.name[8:11]) for x in wdir.glob("collect-[0-9][0-9][0-9]*")]
    round_no = 1 + max(used, default=0)
    final, staging = wdir / f"collect-{round_no:03d}", wdir / f"collect-{round_no:03d}.partial"
    staging.mkdir()
    if not problems:
        try:
            for area in ("results", "launch"):
                names = [n for a, _, n in items if a == area]
                if names:
                    root = next(root for a, root, _ in items if a == area)
                    fetch(camp, name, area, root, names, wdir / f".collect-{round_no:03d}-{area}.tgz", staging,
                          runner, address)
            problems += verify_tree(staging, expected)
        except (RemoteError, CampaignError, tarfile.TarError, OSError) as exc:
            problems.append(f"the copy failed: {type(exc).__name__}: {exc}")
    if problems:
        failed = wdir / f"collect-{round_no:03d}-FAILED"
        replace(staging, failed)
        for archive in wdir.glob(f".collect-{round_no:03d}-*.tgz"):   # an interrupted transfer, kept for inspection
            replace(archive, failed / archive.name.lstrip("."))
        camp.ledger.append({"type": "collect_failed", "worker": name, "round": round_no, "path": camp.rel(failed),
                            "problems": problems[:100], "identity": seen, "address": address})
        raise CampaignError(f"{name}: collection {round_no} failed verification, nothing was admitted: "
                            + "; ".join(problems[:5]))
    (staging / "manifests").mkdir()
    for key, text in texts.items():
        (staging / "manifests" / (key.replace("/", "__") + ".sha256")).write_text(text, encoding="utf-8",
                                                                                   newline="\n")
    sums = manifest_text(expected)
    (staging / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")
    replace(staging, final)
    records, findings = [], {}
    if batch_key in expected:
        try:
            content = json.loads((final / batch_key).read_text(encoding="utf-8"))
        except ValueError as exc:
            content, findings = None, {"batch_problems": [f"batch.json is not JSON: {exc}"]}
        if isinstance(content, dict):
            records, findings = attempt_records(camp, w, final, content, hashes, entries, seen, round_no)
    previous = latest_attempts(entries)
    camp.ledger.append({"type": "collect", "worker": name, "round": round_no, "path": camp.rel(final),
                        "items": hashes, "sums_sha256": sha256_bytes(sums.encode("utf-8")),
                        "batch_json_sha256": expected.get(batch_key), "runner_state": "stopped", "identity": seen,
                        "address": address, **findings})
    appended = 0
    for rec in records:
        earlier = previous.get(rec["trial"])
        if earlier is None or any(earlier.get(k) != rec.get(k) for k in SUBSTANTIVE):
            camp.ledger.append(rec)
            appended += 1
    print(f"{name}: collection {round_no} verified ({len(expected)} files) in {camp.rel(final)}; "
          f"{appended} attempt record(s) appended")
    for key, label in (("batch_problems", "batch"), ("lost_attempts", "LOST ATTEMPTS"),
                       ("unlisted_folders", "unlisted folders")):
        if findings.get(key):
            print(f"  {label}: {'; '.join(findings[key])}")
    for rec in records:
        print(f"  {rec['trial']}: {rec['status']}, {'accepted' if rec['accepted'] else 'not accepted'}"
              + (f" ({'; '.join(rec['reasons'])})" if rec["reasons"] else ""))


def reconcile_worker(camp, name, runner, address=None):
    """After a lost reply or disconnect: inspect the worker's runner, lock, tmux session, launch stamps and batch.json,
    and record a reconciled state only when it is known (running, or stopped with nothing active)."""
    w = camp.workers[name]
    entries = camp.ledger.entries()
    state = worker_state(entries, name)
    try:
        p = remote_call(camp, name, "probe", probe_args(camp, w, dry_run=False), runner, address)
    except RemoteError as exc:
        raise CampaignError(f"{name}: the worker cannot be inspected, so its state is unknown (still {state}): "
                            f"{exc}") from None
    seen = identity(p)
    if seen["instance_id"] != w["instance_id"]:
        raise CampaignError(f"{name}: the metadata server reports instance {seen['instance_id']!r}, registered "
                            f"{w['instance_id']!r}; state unknown (still {state})")
    sessions = (p.get("tmux") or {}).get("sessions")
    if sessions is None:
        raise CampaignError(f"{name}: tmux cannot be read ({(p.get('tmux') or {}).get('error')}); state unknown")
    runners = [x for x in p.get("processes") or [] if "run_expB.py" in x["programs"]]
    others = [x for x in p.get("processes") or [] if "run_expB.py" not in x["programs"]]
    lock = p.get("lock") or {}
    number = max([e.get("launch_no") for e in entries if e.get("worker") == name
                  and e.get("type") == "launch_request"], default=None)
    launch = (p.get("launches") or {}).get(str(number)) if number else None
    batch = p.get("batch") or {}
    evidence = {"identity": seen, "session_present": w["session"] in sessions, "runner_pids": [x["pid"] for x in runners],
                "lock": lock, "launch_no": number, "launch_files": launch, "batch_json_sha256": batch.get("sha256"),
                "batch": batch_summary(batch.get("content"))}
    notes = []
    if runners:
        known = "running"
    elif others or lock.get("held") is not False:
        raise CampaignError(f"{name}: a StopCheck trial or program is active without its runner (lock {lock}, "
                            f"programs {[x['cmdline'][:80] for x in others]}); let it finish its clean-up, then "
                            f"reconcile again (still {state})")
    elif any(s.startswith(SESSION_PREFIX) for s in sessions):
        raise CampaignError(f"{name}: a campaign tmux session is alive but no runner is visible; inspect it with "
                            f"sudo tmux attach -t {w['session']}, then reconcile again (still {state})")
    else:
        known = "stopped"
        if number and not (launch or {}).get("start"):
            notes.append(f"launch {number} never started (no start stamp)")
        elif number and not (launch or {}).get("exit"):
            notes.append(f"launch {number} started but wrote no exit status (the runner was killed or the VM restarted)")
        elif number:
            notes.append(f"launch {number} exited: {launch['exit']}")
    request = next((e for e in reversed(entries) if e.get("worker") == name and e.get("type") == "launch_request"), None)
    booted = ((request or {}).get("identity") or {}).get("boot_id")
    if booted and seen["boot_id"] and booted != seen["boot_id"]:
        notes.append(f"the VM restarted since launch {number} (boot id {booted} then, {seen['boot_id']} now)")
    camp.ledger.append({"type": "reconciled", "worker": name, "state": known, "previous_state": state,
                        "evidence": evidence, "notes": notes, "address": address})
    print(f"{name}: reconciled as {known} (was {state})" + (f"; {'; '.join(notes)}" if notes else "") +
          ("; collect before any relaunch" if known == "stopped" else ""))


@contextlib.contextmanager
def campaign_lock(folder):
    """One coordinator command at a time per campaign folder (an exclusive lock file; works on Windows too)."""
    path = Path(folder) / "coordinator.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise CampaignError(f"another coordinator command holds {path}; if none is running, delete it") from None
    try:
        os.write(fd, f"pid {os.getpid()} since {utc_now()}\n".encode("ascii"))
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    register_parser = sub.add_parser("register", help="validate a spec and register the campaign folder")
    register_parser.add_argument("spec", type=Path)
    register_parser.add_argument("--results-root", type=Path,
                                 default=Path(__file__).resolve().parents[2] / "cloud-results")
    register_parser.add_argument("--dry-run", action="store_true", help="print the campaign file; write nothing")
    for command in ("preflight", "launch", "status", "collect", "reconcile"):
        p = sub.add_parser(command)
        p.add_argument("campaign", type=Path, help="the campaign folder (<results-root>/<campaign id>)")
        p.add_argument("--worker", action="append", default=[], help="a registered worker (repeatable; default all)")
        p.add_argument("--address", action="append", default=[], metavar="WORKER=IP",
                       help="reach a worker at another address for this command (recorded; identity is still checked)")
        if command == "status":
            p.add_argument("--no-ssh", action="store_true", help="the ledger's view only")
    args = parser.parse_args(argv)
    if args.command == "reconcile" and len(args.worker) != 1:
        parser.error("reconcile takes exactly one --worker")
    return args


def main(argv=None, runner=None, repo=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["remote"]:
        return remote_main(argv[1:])
    if sys.version_info < (3, 12):
        print("REFUSED: campaign_b.py needs Python 3.12 or later", file=sys.stderr)
        return 2
    args = parse_args(argv)
    runner = runner or Runner()
    repo = Path(repo) if repo else Path(__file__).resolve().parents[1]
    try:
        if args.command == "register":
            result = register(args.spec, args.results_root, repo, dry_run=args.dry_run)
            if not args.dry_run:
                fingerprint = json.loads((result / "registration.json").read_text(encoding="utf-8"))["campaign_sha256"]
                print(f"Registered {result}. Campaign fingerprint (SHA-256 of campaign.json): {fingerprint}")
            return 0
        camp = Campaign(args.campaign, repo)
        unknown = [n for n in args.worker if n not in camp.workers]
        if unknown:
            raise CampaignError("not registered workers: " + ", ".join(unknown))
        names = args.worker or list(camp.workers)
        addresses = {}
        for item in args.address:
            worker, _, ip = item.partition("=")
            if worker not in camp.workers or not ip:
                raise CampaignError(f"--address {item!r}: use WORKER=IP for a registered worker")
            addresses[worker] = ip
        if args.command == "status":
            return status(camp, names, runner, addresses, args.no_ssh)
        with campaign_lock(camp.folder):
            if args.command == "preflight":
                return preflight(camp, names, runner, addresses)
            failed = 0
            for name in names:
                try:
                    if args.command == "launch":
                        launch_worker(camp, name, runner, addresses.get(name))
                    elif args.command == "collect":
                        collect_worker(camp, name, runner, addresses.get(name))
                    else:
                        reconcile_worker(camp, name, runner, addresses.get(name))
                except CampaignError as exc:
                    failed += 1
                    print(f"REFUSED: {exc}", file=sys.stderr)
            return 1 if failed else 0
    except CampaignError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
