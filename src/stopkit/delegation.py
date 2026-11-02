"""Setting up the worker stand-in, the program the pretend agent hands a job to (the `delegated` case).

The worker runs as the account stopkit-worker, as a system service of its own (stopkit-worker.service), so
its jobs are outside the agent's process tree, control group, account and service manager. Restarting the
agent account's service manager does not touch it.
"""
from __future__ import annotations

import hashlib
import os
import time

from .accounts import Account
from .cmd import Runner, run
from .config import BIN_DIR, WORKER_SOCKET, WORKER_UNIT, KitError
from .observe.worker import request

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'worker_standin.py')
INSTALLED = os.path.join(BIN_DIR, 'worker_standin.py')


def install() -> str:
    """Copy the worker's program to the root-owned folder it runs from. Returns its SHA-256."""
    with open(SCRIPT, 'rb') as f:
        data = f.read()
    os.makedirs(BIN_DIR, mode=0o755, exist_ok=True)
    tmp = INSTALLED + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, INSTALLED)
    return hashlib.sha256(data).hexdigest()


def running(runner: Runner = run) -> bool:
    return runner(['systemctl', 'is-active', WORKER_UNIT]).out.strip() == 'active'


RUNNING_SHA = os.path.join(os.path.dirname(BIN_DIR), 'worker.sha256')    # the program the service started with


def ensure(worker: Account, runner: Runner = run, timeout: float = 15.0) -> None:
    """Start the worker service if it is not running, or restart it if its program has changed since it
    started, and wait until it answers."""
    digest = install()
    try:
        with open(RUNNING_SHA) as f:
            started_with = f.read().strip()
    except OSError:
        started_with = None
    if running(runner) and started_with != digest:
        stop(runner)
    if not running(runner):
        runner(['systemctl', 'reset-failed', WORKER_UNIT])
        r = runner(['systemd-run', f'--unit={WORKER_UNIT}', '--description=stopkit worker stand-in',
                    f'--property=User={worker.name}', f'--property=Group={worker.gid}',
                    '--property=RuntimeDirectory=stopkit-worker', '--property=RuntimeDirectoryMode=0755',
                    '--', '/usr/bin/python3', '-I', '-B', INSTALLED, WORKER_SOCKET])
        if not r.ok:
            raise KitError(f'could not start the worker stand-in: {r.error or r.err.strip()[:300]}')
        with open(RUNNING_SHA, 'w') as f:
            f.write(digest + '\n')
    deadline = time.monotonic() + timeout
    while True:
        try:
            request({'op': 'list'})
            return
        except (OSError, ValueError) as e:
            if time.monotonic() >= deadline:
                raise KitError(f'the worker stand-in does not answer at {WORKER_SOCKET}: {e}')
            time.sleep(0.3)


def stop(runner: Runner = run) -> None:
    runner(['systemctl', 'stop', WORKER_UNIT], timeout=60)
