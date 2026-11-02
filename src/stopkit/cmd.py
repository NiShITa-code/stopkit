"""Running commands for the kit.

Every listing and every action goes through run(), so each leaves the same evidence: the argument list,
the exit code, the output, when it started and how long it took. run() never raises for a command that
fails. A failure is data, which callers turn into "unknown", never into "nothing there".
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

# Output kept per stream. A listing whose output was cut cannot be trusted to be complete, so a cut
# result is never ok (see CmdResult.ok); the limit is far above any listing this kit makes.
MAX_KEEP = 5_000_000


@dataclass(frozen=True)
class CmdResult:
    argv: tuple[str, ...]
    rc: int | None                 # exit code; None if the command could not run or timed out
    out: str
    err: str
    started: float                 # wall clock, seconds since the epoch
    seconds: float                 # how long it took (monotonic clock)
    error: str | None = None       # why it could not run at all, or that it timed out
    truncated: bool = False

    @property
    def ok(self) -> bool:
        """True only if the command ran to the end, exited with status 0 and its output was kept whole."""
        return self.error is None and self.rc == 0 and not self.truncated

    def summary(self) -> dict:
        """A short, JSON-ready description for messages and logs. Records keep the full output through
        evidence.EvidenceStore.command(), never through this."""
        return {'argv': list(self.argv), 'rc': self.rc, 'error': self.error, 'started': round(self.started, 3),
                'seconds': round(self.seconds, 3), 'out_chars': len(self.out), 'err_chars': len(self.err),
                'truncated': self.truncated}


Runner = Callable[[Sequence[str]], CmdResult]


def _text(data) -> str:
    if data is None:
        return ''
    return data.decode('utf-8', 'replace') if isinstance(data, bytes) else data


def run(argv: Sequence[str], timeout: float = 30.0, input: str | None = None,
        env: Mapping[str, str] | None = None) -> CmdResult:
    """Run argv (never through a shell) and return what happened."""
    argv = tuple(str(a) for a in argv)
    started, t0 = time.time(), time.monotonic()
    try:
        p = subprocess.run(argv, input=input, capture_output=True, text=True, errors='replace',
                           timeout=timeout, env=dict(env) if env is not None else None)
    except FileNotFoundError:
        return CmdResult(argv, None, '', '', started, time.monotonic() - t0, error=f'command not found: {argv[0]}')
    except subprocess.TimeoutExpired as e:
        return CmdResult(argv, None, _text(e.stdout), _text(e.stderr), started, time.monotonic() - t0,
                         error=f'timed out after {timeout:g} s')
    except OSError as e:
        return CmdResult(argv, None, '', '', started, time.monotonic() - t0, error=f'{type(e).__name__}: {e}')
    out, err = p.stdout or '', p.stderr or ''
    cut = len(out) > MAX_KEEP or len(err) > MAX_KEEP
    return CmdResult(argv, p.returncode, out[:MAX_KEEP], err[:MAX_KEEP], started, time.monotonic() - t0, truncated=cut)
