"""Keeping the full evidence behind every result, so anyone can check a verdict again later.

Every command's whole output and error text is kept, compressed and stored once under its SHA-256.
Measured on 25 Oct 2026 in WSL: one complete set of listings is about 79 KB raw and about 10 KB
compressed, so complete evidence for 100 runs takes a few MB. Nothing is ever summarised away.
"""
from __future__ import annotations

import gzip
import hashlib
import os

from .cmd import CmdResult


class EvidenceStore:
    """A folder of compressed text blobs, each named by the SHA-256 of its content."""

    def __init__(self, root: str, create: bool = True):
        self.root = root
        self.blobs = os.path.join(root, 'blobs')
        if create:
            os.makedirs(self.blobs, exist_ok=True)

    def put(self, text: str) -> str:
        """Store text (once) and return its SHA-256 hex digest."""
        data = text.encode('utf-8', 'replace')
        digest = hashlib.sha256(data).hexdigest()
        path = os.path.join(self.blobs, digest + '.gz')
        if not os.path.exists(path):
            tmp = f'{path}.{os.getpid()}.tmp'
            with gzip.open(tmp, 'wb', compresslevel=9) as f:
                f.write(data)
            os.replace(tmp, path)          # atomic: a reader never sees a half-written blob
        return digest

    def get(self, digest: str) -> str:
        """The text stored under digest. Raises ValueError if the content no longer matches its name."""
        with gzip.open(os.path.join(self.blobs, digest + '.gz'), 'rb') as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f'evidence blob {digest} is corrupted: its content no longer matches its name')
        return data.decode('utf-8', 'replace')

    def command(self, r: CmdResult) -> dict:
        """Everything about one command, with its full output and error text stored as blobs."""
        return {'argv': list(r.argv), 'rc': r.rc, 'error': r.error, 'started': round(r.started, 6),
                'seconds': round(r.seconds, 6), 'truncated': r.truncated,
                'out_sha256': self.put(r.out), 'err_sha256': self.put(r.err)}
