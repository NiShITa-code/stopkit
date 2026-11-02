"""The framework's own hash (EXECUTION-PLAN.md section 1: every run records the framework's hash).

One SHA-256 over every file of the package that can change a result (its code, its specification and its
scripts), in a fixed order: each file's path relative to the package and its own SHA-256. Two runs with the
same hash ran the same kit, whether or not the code was committed; the hash of a committed tree can be
computed again from a checkout to show which commit a run used.
"""
from __future__ import annotations

import hashlib
import os

PACKAGE = os.path.dirname(os.path.abspath(__file__))
SUFFIXES = ('.py', '.json', '.sh', '.js')


def files(root: str = PACKAGE) -> list[str]:
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != '__pycache__')
        for n in names:
            if n.endswith(SUFFIXES):
                out.append(os.path.relpath(os.path.join(d, n), root).replace(os.sep, '/'))
    return sorted(out)


def code_sha256(root: str = PACKAGE) -> str:
    h = hashlib.sha256()
    for rel in files(root):
        with open(os.path.join(root, rel), 'rb') as f:
            h.update(f'{rel}\0{hashlib.sha256(f.read()).hexdigest()}\n'.encode())
    return h.hexdigest()
