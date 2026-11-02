"""The bench instruments of one run (FRAMEWORK.md section 3): the sealed ground-truth recorder, the effect sink,
and the sentinel's reporter, whose steady reports show that the truth channel and the sink kept working.

They run outside the agent account: the recorder and the sink as root, the sentinel's reporter as the sentinel
account. No stop under test acts on either account; E0 checks that they survive the stops that act on the
agent account.

For work that can reach the outside only over HTTP (an n8n Code node), the bench also runs the HTTP stand-in
(http_standin.py), as root, which passes each request on to the recorder and the sink, and a second sentinel
that reports through it, so that the HTTP path is shown working too.
"""
from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import subprocess
import time
from typing import Callable, Sequence

from ..system import accounts
from ..system.accounts import Account
from ..system.names import BIN_DIR, SENTINEL_BEAT, SENTINEL_HTTP
from ..system import KitError

HERE = os.path.dirname(os.path.abspath(__file__))
RECORDER = os.path.join(HERE, 'recorder.py')
SINK = os.path.join(os.path.dirname(HERE), 'observe', 'effect_sink.py')
STANDIN = os.path.join(HERE, 'http_standin.py')
BEAT_SOURCE = os.path.join(HERE, 'beat.py')
REPORT_SOURCE = os.path.join(HERE, 'work_report.py')
BEAT = f'{BIN_DIR}/beat.py'
REPORT = f'{BIN_DIR}/stopkit_report.py'        # imported by planted work inside a platform's own process
SOCKET_ROOT = '/run/stopkit-truth'
PYTHON = '/usr/bin/python3'
ROOT_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}


def sha256(path: str) -> str:
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _install(source: str, target: str, mode: int) -> str:
    with open(source, 'rb') as f:
        data = f.read()
    os.makedirs(BIN_DIR, mode=0o755, exist_ok=True)
    tmp = target + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, target)
    return hashlib.sha256(data).hexdigest()


def install_beat() -> str:
    """Copy the reporter to a root-owned place every account can read. Returns its SHA-256."""
    return _install(BEAT_SOURCE, BEAT, 0o755)


def install_report() -> str:
    """Copy the in-process reporter (truth/work_report.py) next to it. Returns its SHA-256."""
    return _install(REPORT_SOURCE, REPORT, 0o644)


def beat_argv(truth: str, sink: str, tag: str, item: str, period: float | None = None, fire: bool = False) -> list[str]:
    """The command planted work runs to report itself (truth/beat.py)."""
    argv = [PYTHON, '-I', '-B', BEAT, 'fire' if fire else 'run', truth, sink, tag, item]
    if period is not None and not fire:
        argv.append(f'{period:g}')
    return argv


def exec_argv(truth: str, tag: str, item: str, program: Sequence[str]) -> list[str]:
    """The command that reports a process to the truth and then becomes `program` (a platform's component)."""
    return [PYTHON, '-I', '-B', BEAT, 'exec', truth, tag, item, '--'] + list(program)


def read_events(path: str) -> list[dict]:
    """The complete lines of a JSON-lines log written by a live process (a line still being written is left)."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return []
    out = []
    for line in text.split('\n')[:-1]:
        if line.strip():
            out.append(json.loads(line))
    return out


def wait_until(pred: Callable[[], bool], timeout: float, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


class Bench:
    def __init__(self, run_id: str, out_dir: str, uids: Sequence[int], sentinel: Account, heartbeat_s: float = 0.2,
                 http: bool = False):
        self.dir = os.path.join(SOCKET_ROOT, run_id)
        self.truth_sock = os.path.join(self.dir, 'truth.sock')
        self.sink_sock = os.path.join(self.dir, 'sink.sock')
        self.truth_log = os.path.join(out_dir, 'truth.jsonl')
        self.effects_log = os.path.join(out_dir, 'effects.jsonl')
        self.standin_log = os.path.join(out_dir, 'standin.jsonl')
        # The stand-in passes the HTTP path's reports on as root, so the recorder takes reports from root too
        # (only the kit runs as root on the bench).
        self.uids = sorted(set(uids) | {sentinel.uid} | ({0} if http else set()))
        self.sentinel = sentinel
        self.heartbeat_s = heartbeat_s
        self.http = http
        self.http_port: int | None = None
        self.procs: dict[str, subprocess.Popen] = {}
        self.digests: dict[str, str] = {}

    @property
    def http_url(self) -> str | None:
        return None if self.http_port is None else f'http://127.0.0.1:{self.http_port}/effect'

    def _spawn(self, name: str, argv: list[str], ready: str, timeout: float) -> str:
        """Start an instrument and wait for its ready line, which starts with `ready`. Returns the line."""
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=ROOT_ENV, start_new_session=True)
        self.procs[name] = p
        r, _, _ = select.select([p.stdout], [], [], timeout)
        line = p.stdout.readline().decode('utf-8', 'replace').strip() if r else ''
        if line != ready and not line.startswith(ready + ' '):
            raise KitError(f'the {name} did not start: {line!r}; {p.stderr.read1(2000).decode("utf-8", "replace")}'
                           if p.poll() is not None else f'the {name} did not report ready within {timeout:g} s')
        return line

    def _sentinel(self, name: str, argv: list[str]) -> None:
        self.procs[name] = subprocess.Popen(accounts.as_account_argv(self.sentinel, argv), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ROOT_ENV,
                                            start_new_session=True)

    def start(self, timeout: float = 10.0, truth: bool = True) -> None:
        """Start the instruments. With truth=False the recorder is not run: the work's reports to it go nowhere
        (sends never wait), while the sink and the sentinels work as usual. That is the run without the
        instruments of the non-interference test N3."""
        os.makedirs(SOCKET_ROOT, mode=0o755, exist_ok=True)
        os.makedirs(self.dir, mode=0o755)
        os.chmod(self.dir, 0o755)
        self.digests = {'beat.py': install_beat(), 'stopkit_report.py': install_report(),
                        'recorder.py': sha256(RECORDER), 'effect_sink.py': sha256(SINK)}
        self.truth = truth
        try:
            if truth:
                self._spawn('recorder', [PYTHON, '-I', '-B', RECORDER, self.truth_sock, self.truth_log,
                                         ','.join(str(u) for u in self.uids)], 'stopkit-truth-ready', timeout)
            self._spawn('sink', [PYTHON, '-I', '-B', SINK, self.sink_sock, self.effects_log], 'stopkit-sink-ready', timeout)
            self._sentinel('sentinel', beat_argv(self.truth_sock, self.sink_sock, SENTINEL_BEAT, 'channel', self.heartbeat_s))
            if not self._wait_report(SENTINEL_BEAT, timeout):
                raise KitError("the sentinel's reporter never reached the instruments")
            if self.http:
                self.digests['http_standin.py'] = sha256(STANDIN)
                line = self._spawn('standin', [PYTHON, '-I', '-B', STANDIN, self.truth_sock, self.sink_sock,
                                               self.standin_log], 'stopkit-standin-ready', timeout)
                self.http_port = int(line.split()[1])
                self._sentinel('http-sentinel', [PYTHON, '-I', '-B', BEAT, 'http-run', self.http_url, SENTINEL_HTTP,
                                                 'channel-http', f'{self.heartbeat_s:g}'])
                if not self._wait_report(SENTINEL_HTTP, timeout):
                    raise KitError("the HTTP sentinel's reports never reached the instruments through the stand-in")
        except BaseException:
            self.stop()
            raise

    def _wait_report(self, tag: str, timeout: float) -> bool:
        """The sentinel's reports arrive: at the recorder (watched, or one message) when it runs, otherwise at
        the sink."""
        if not getattr(self, 'truth', True):
            return wait_until(lambda: any(e.get('ev') == 'effect' and (e.get('msg') or {}).get('tag') == tag
                                          for e in read_events(self.effects_log)), timeout)
        if tag == SENTINEL_BEAT:
            return self.wait_watch(tag, timeout)
        return wait_until(lambda: any(e.get('ev') == 'msg' and (e.get('msg') or {}).get('tag') == tag
                                      for e in self.events()), timeout)

    def events(self) -> list[dict]:
        return read_events(self.truth_log)

    def wait_watch(self, tag: str, timeout: float) -> bool:
        return wait_until(lambda: any(e.get('ev') == 'watch' and e.get('tag') == tag for e in self.events()), timeout)

    def sentinels(self) -> list[str]:
        """The sentinels' tags: one per path planted work can report by."""
        return [SENTINEL_BEAT] + ([SENTINEL_HTTP] if self.http else [])

    def info(self) -> dict:
        out = {'truth_log': os.path.basename(self.truth_log), 'effects_log': os.path.basename(self.effects_log),
               'uids': self.uids, 'heartbeat_s': self.heartbeat_s, 'sentinel_tag': SENTINEL_BEAT,
               'sha256': dict(self.digests)}
        if self.http:
            out.update(http_url=self.http_url, standin_log=os.path.basename(self.standin_log),
                       http_sentinel_tag=SENTINEL_HTTP)
        return out

    def stop(self) -> dict:
        """End the sentinels' reporters, then the stand-in, the recorder and the sink (each writes its end line).
        Returns their exit statuses."""
        out = {}
        for name in ('sentinel', 'http-sentinel', 'standin', 'recorder', 'sink'):
            p = self.procs.get(name)
            if p is None:
                continue
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            for stream in (p.stdout, p.stderr):
                if stream:
                    stream.close()
            out[name] = p.returncode
        shutil.rmtree(self.dir, ignore_errors=True)
        return out
