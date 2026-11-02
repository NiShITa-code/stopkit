"""The worker stand-in's own record of the jobs handed to it: what the system that took the work says."""
from __future__ import annotations

import json
import socket
import time

from ..system.names import WORKER_SOCKET
from .model import Item, Listing, failed

NOT_RUNNING = (FileNotFoundError, ConnectionRefusedError)    # no worker, so no job of it can be running


def request(obj: dict, path: str = WORKER_SOCKET, timeout: float = 15.0) -> dict:
    """One request to the worker: a JSON line out, a JSON line back. Raises OSError or ValueError."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(json.dumps(obj).encode() + b'\n')
        data = b''
        while not data.endswith(b'\n'):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def list_worker_jobs(path: str = WORKER_SOCKET, names=None) -> Listing:
    """Every job the worker holds, with the account that handed it in (names: {uid: account name})."""
    taken = time.time()
    try:
        reply = request({'op': 'list'}, path)
    except (OSError, ValueError) as e:
        return failed('worker', 'all', taken, [f'the worker at {path} could not be asked: {type(e).__name__}: {e}'])
    jobs = reply.get('jobs') if isinstance(reply, dict) else None
    if not isinstance(jobs, list) or not all(isinstance(j, dict) and {'id', 'uid', 'tag', 'status'} <= set(j) for j in jobs):
        return failed('worker', 'all', taken, [f'the worker gave an unexpected answer: {str(reply)[:200]}'])
    items = tuple(Item('worker', f"job:{j['id']}", (names or {}).get(j['uid'], f"uid:{j['uid']}"), j['tag'], dict(j))
                  for j in jobs)
    return Listing('worker', 'all', True, items, taken, (), (('worker-jobs', json.dumps(reply, sort_keys=True)),))
