"""Live demo: what stopping a LangGraph agent does, and what it leaves behind. Started by demo.sh, as root, on Linux.

One window, step by step. It starts LangGraph's development server (`langgraph dev`) as the kit's test account
and gives the demo agent (graph.py) its task. Then it tries the stops a developer has, one after another:
cancelling the run, pressing Ctrl+C on the server, ending the server's process, and starting the server again.
During each one it shows, as they happen, LangGraph's own log lines about runs and shutting down, LangGraph's
answer about the run whenever it changes (asked once a second), and everything the outside service receives. It
ends with the toolkit's full stop, which lists what is left from outside the agent, removes it, and watches for
over a minute that nothing more arrives. Between the steps nothing is lost: when a step starts it first shows how
many messages the outside service received since the last step, and any of LangGraph's lines from that time, marked.

Every action the agent sends carries the process that sent it, so updates from an old server process are never
mistaken for a run that came back in a new one. The agent has no model; graph.py says what its steps stand for.
Its helper and its scheduled job are constructions: the demo agent was written to leave them, as real agents do
when a task asks for it. What the demo tests is which stop reaches them, and what LangGraph does with the run.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.abspath(os.path.join(HERE, '..', '..'))
# the package is under src/ in the repository, and at the top of an installed copy (setup/base.sh)
sys.path.insert(0, os.path.join(KIT, 'src') if os.path.isdir(os.path.join(KIT, 'src', 'stopkit')) else KIT)
from stopkit.system import accounts  # noqa: E402
from stopkit import stops  # noqa: E402
from stopkit.system.names import AGENT, STATE_DIR  # noqa: E402
from langgraph_sdk import get_sync_client  # noqa: E402  (installed in the demo's environment)
import log_filter  # noqa: E402  which of LangGraph's log lines the demo shows (also used by check_log_filter.py)

VENV = '/opt/stopkit-demo/venv'
SERVER = 'http://127.0.0.1:2024'
OUTSIDE_PORT = 8899
APP_FILES = ('graph.py', 'helper.py', 'langgraph.json')
LANGGRAPH_ENV = ['LANGGRAPH_CLI_NO_ANALYTICS=1', 'LANGGRAPH_NO_VERSION_CHECK=true', 'LANGSMITH_TRACING=false',
                 'LANGCHAIN_TRACING_V2=false']
CTRL_C_WAIT = 10.0

arrivals: list[tuple[float, str, str, int | None]] = []     # (wall time, sender, what, sending process)
lock = threading.Lock()
live = threading.Event()                                     # print arrivals as they come
out = threading.Lock()                                       # one line at a time on the screen
pending: list[str] = []                                      # LangGraph's lines from between steps, shown at the next step
paused_since: list[float] = []                               # when the last live stretch ended
done = threading.Event()                                     # stops the log and status followers at the end
followed: dict[str, str | None] = {'thread': None, 'said': None}   # the thread asked about, and LangGraph's last answer


class Outside(http.server.BaseHTTPRequestHandler):
    """The outside service: every action of the agent, its helper and its scheduled job arrives here."""

    def do_POST(self):
        try:
            msg = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        except ValueError:
            msg = {}
        t = time.time()
        with lock:
            arrivals.append((t, msg.get('source', '?'), msg.get('text', ''), msg.get('pid')))
        with out:
            if live.is_set():
                pid = f" (process {msg['pid']})" if msg.get('pid') else ''
                print(f"      {time.strftime('%H:%M:%S', time.localtime(t))}  received from {msg.get('source', '?')}"
                      f"{pid}: {msg.get('text', '')}", flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *_args):
        pass


def say(text: str) -> None:
    with out:
        print('\n' + text, flush=True)


def note(text: str) -> None:
    with out:
        print('   ' + text, flush=True)


def pause(auto: bool) -> None:
    if not auto:
        input('\n   [Enter] ')


def go_live(header: str = "Live: LangGraph's log, LangGraph's answer when it changes, and what the outside "
                          'service receives:') -> None:
    with out:
        print('   ' + header, flush=True)
        if paused_since:
            since = paused_since.pop()
            got = [(s, count(since, s)) for s in ('agent', 'helper', 'scheduled job')]
            if any(n for _, n in got):
                print('      Between the steps the outside service received '
                      + ', '.join(f'{n} from the {s}' for s, n in got if n) + '.', flush=True)
        for line in pending:
            print(line + '   (between the steps)', flush=True)
        pending.clear()
        live.set()


def end_live() -> None:
    with out:
        live.clear()
        paused_since[:] = [time.time()]


def emit(t: float, text: str) -> None:
    """One of LangGraph's own lines: shown at once while live, otherwise when the next step starts."""
    line = f"      {time.strftime('%H:%M:%S', time.localtime(t))}  {text}"
    with out:
        if live.is_set():
            print(line, flush=True)
        else:
            pending.append(line)


def follow_log(path: str) -> None:
    """Follow LangGraph's log as the server writes it, and show the lines log_filter picks (see its docstring)."""
    pos, buf = 0, b''
    while not done.is_set():
        try:
            with open(path, 'rb') as f:
                f.seek(pos)
                data = f.read()
            pos += len(data)
            *lines, buf = (buf + data).split(b'\n')
            for raw in lines:
                shown = log_filter.shown(raw.decode('utf-8', 'replace'))
                if shown:
                    emit(*shown)
        except OSError:
            pass
        time.sleep(0.1)


def runs_now(thread_id: str) -> str:
    """LangGraph's answer about the thread's runs, asked through its HTTP API with a 1 s limit."""
    try:
        with urllib.request.urlopen(f'{SERVER}/threads/{thread_id}/runs', timeout=1.0) as r:
            runs = json.loads(r.read())
        return ', '.join(f"run {x['run_id'][:8]} is {x['status']}" for x in runs) or 'no runs'
    except urllib.error.HTTPError as e:
        return f'answers HTTP {e.code}'
    except OSError:
        return 'does not answer'
    except (ValueError, KeyError, TypeError):
        return 'gives an answer the demo cannot read'


def follow_status() -> None:
    """Ask LangGraph about the agent's run once a second, and show its answer whenever it changes."""
    while not done.is_set():
        tid = followed['thread']
        if tid:
            t = time.time()
            said = runs_now(tid)
            if said != followed['said']:
                followed['said'] = said
                emit(t, f"LangGraph's API: {said}")
        time.sleep(1.0)


def count(since: float, source: str, pid: int | None = None, until: float | None = None) -> int:
    with lock:
        return sum(1 for t, s, _, p in arrivals if t >= since and (until is None or t <= until) and s == source
                   and (pid is None or p == pid))


def last_update(pid: int) -> str:
    with lock:
        ups = [x for _, s, x, p in arrivals if s == 'agent' and p == pid and x.startswith('update')]
    return ups[-1] if ups else 'none'


def answers(timeout: float = 1.0) -> bool:
    try:
        return urllib.request.urlopen(SERVER + '/ok', timeout=timeout).status == 200
    except OSError:
        return False


class Server:
    """LangGraph's development server, run as the test account in its own process group, like a command started
    in a terminal. ctrl_c() sends SIGINT to the group, as pressing Ctrl+C in that terminal does; end() sends
    SIGKILL, as `kill -9` or ending the task in a process manager does."""

    def __init__(self, agent, app: str, log_path: str):
        self.agent, self.app, self.log_path, self.p = agent, app, log_path, None

    def start(self, timeout: float = 90.0) -> float:
        argv = accounts.as_account_argv(self.agent, ['env'] + LANGGRAPH_ENV + [
            'sh', '-c', 'cd "$1" && shift && exec "$@"', 'sh', self.app,
            f'{VENV}/bin/langgraph', 'dev', '--no-browser', '--no-reload', '--host', '127.0.0.1', '--port', '2024'])
        log = open(self.log_path, 'a')
        t0 = time.monotonic()
        self.p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        while time.monotonic() - t0 < timeout:
            if answers():
                return time.monotonic() - t0
            if self.p.poll() is not None:
                raise SystemExit(f'the LangGraph server exited while starting; see {self.log_path}')
            time.sleep(0.5)
        raise SystemExit(f'the LangGraph server did not answer within {timeout:g} s; see {self.log_path}')

    @property
    def pid(self) -> int:
        return self.p.pid                       # every step to the server exec()s, so this is the server itself

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def ctrl_c(self) -> None:
        os.killpg(self.p.pid, signal.SIGINT)

    def wait(self, seconds: float) -> bool:
        try:
            self.p.wait(seconds)
            return True
        except subprocess.TimeoutExpired:
            return False

    def end(self) -> None:
        if self.alive():
            os.killpg(self.p.pid, signal.SIGKILL)
            self.p.wait()


def show_leftovers(items: list[str]) -> None:
    if not items:
        print('   Nothing: every listing succeeded and found nothing of the account.')
    for x in items:
        print(f'   - {x[:200]}')


def runs_of(client, thread_id: str) -> str:
    try:
        return ', '.join(f"run {r['run_id'][:8]} is {r['status']}" for r in client.runs.list(thread_id)) or 'no runs'
    except Exception as e:                              # the server may be down
        return f'nothing (it does not answer: {type(e).__name__})'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--auto', action='store_true', help='no pauses: run every step straight through')
    auto = ap.parse_args().auto
    agent = accounts.owned(AGENT)
    app = os.path.join(agent.home, 'lgdemo')
    log_dir = os.path.join(STATE_DIR, 'demo-logs')               # root-only and kept, unlike /tmp
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    log_path = os.path.join(log_dir, f'langgraph-server-{time.strftime("%Y%m%d-%H%M%S")}.log')
    rows: list[tuple[str, str, int, int, int]] = []    # stop, what LangGraph or the system said, agent, helper, job

    # Set-up: an empty test account, the demo app in its home, the outside service.
    if stops.leftovers(agent, files_ok=False):
        print('The test account still holds something from an earlier run; clearing it first.')
        stops.cleanup(agent)
    if answers():
        raise SystemExit('something already answers on 127.0.0.1:2024; stop it first')
    shutil.rmtree(app, ignore_errors=True)
    os.makedirs(app)
    for f in APP_FILES:
        shutil.copy(os.path.join(HERE, f), app)
    subprocess.run(['chown', '-R', f'{agent.uid}:{agent.gid}', app], check=True)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', OUTSIDE_PORT), Outside)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    server = Server(agent, app, log_path)
    client = get_sync_client(url=SERVER)
    threading.Thread(target=follow_log, args=(log_path,), daemon=True).start()
    threading.Thread(target=follow_status, daemon=True).start()

    try:
        say('STEP 1. Start the agent: LangGraph\'s development server (`langgraph dev`; LangGraph 1.2.12, '
            f'langgraph-api 0.15.1), as the test account {agent.name}.')
        go_live()
        took = server.start()
        first = server.pid
        note(f'The server (process {first}) answers after {took:.1f} s. Its full log: {log_path}')
        time.sleep(1.0)
        end_live()
        pause(auto)

        say('STEP 2. Give the agent its task. It starts a helper in the background, schedules a follow-up job for '
            'every minute, then sends one update a second for thirty minutes.')
        go_live()
        thread = client.threads.create()
        tid = thread['thread_id']
        followed['thread'] = tid
        rid = client.runs.create(tid, 'agent', input={})['run_id']
        time.sleep(7.5)
        end_live()
        print(f'   Asked now, LangGraph says: {runs_of(client, tid)}.')
        pause(auto)

        say('STEP 3. Stop 1, LangGraph\'s own stop: cancel the run (client.runs.cancel, the request LangGraph\'s '
            'client library sends).')
        go_live()
        t = time.time()
        client.runs.cancel(tid, rid)
        time.sleep(1.0)
        said = runs_of(client, tid)
        time.sleep(8)
        end_live()
        n = count(t + 1.0, 'agent', first)
        rows.append(('Cancel the run', said, n, count(t + 1.0, 'helper'), count(t + 1.0, 'scheduled job')))
        print(f'   Asked one second after the cancel, LangGraph said: {said}.')
        if n and 'interrupted' in said:
            print(f'   From then on the agent sent {n} more updates. The task is a plain (synchronous) function '
                  'running in a thread of its own: the cancel stops LangGraph waiting for it, not the function, so '
                  'it runs on while the run reads interrupted.')
        else:
            print(f'   From then on the agent sent {n} more updates.')
        pause(auto)

        say('STEP 4. Stop 2: stop the server, as a developer presses Ctrl+C in its terminal.')
        go_live()
        t = time.time()
        server.ctrl_c()
        presses = 1
        exited = server.wait(CTRL_C_WAIT)
        if not exited:
            note(f'{CTRL_C_WAIT:g} s later the server process is still running; pressing Ctrl+C again, as people do.')
            server.ctrl_c()
            presses = 2
            exited = server.wait(CTRL_C_WAIT)
        answering = not exited and answers()
        state = ('the process has exited' if exited else
                 f"the server {'still answers' if answering else 'no longer answers'}, but its process "
                 f'({first}) is still running')
        note(f'{time.time() - t:.0f} s after the first Ctrl+C: {state}.')
        time.sleep(6)
        end_live()
        n = count(t, 'agent', first)
        rows.append(('Ctrl+C on the server' + (', twice' if presses == 2 else ''), state, n, count(t, 'helper'),
                     count(t, 'scheduled job')))
        why = ''
        if n and not exited and not answering:
            why = (' Ctrl+C makes the server stop taking requests, but Python does not let the process exit until '
                   'the task\'s thread has finished, so the task runs on.')
        print(f'   Since the Ctrl+C the agent sent {n} updates (last: {last_update(first)}).' + why)
        pause(auto)

        if exited:
            say('STEP 5. The server process has ended. Waiting for the next minute, when the scheduled job is due.')
        else:
            say('STEP 5. Stop 3: end the server\'s process, as `kill -9` or ending it in a process manager does.')
        go_live()
        t = time.time()
        if not exited:
            server.end()
            note(f'Process {first} has ended.')
        time.sleep(6)
        note('Waiting for the next minute, when the scheduled job is due:')
        while time.time() - t < 75 and not count(t, 'scheduled job'):
            time.sleep(0.5)
        end_live()
        n, h, j = count(t + 0.5, 'agent', first), count(t, 'helper'), count(t, 'scheduled job')
        rows.append(('After the server exited' if exited else 'End the server process', 'the process has ended',
                     n, h, j))
        print(f"   With the server's process ended, the agent sent {n} updates; its helper sent {h} reports and the "
              f'scheduled job ran {j} time(s).')
        pause(auto)

        say('STEP 6. Start the server again, as you would after fixing something. Nobody gives the agent a task.')
        go_live()
        t = time.time()
        took = server.start()
        second = server.pid
        note(f'The new server (process {second}) answers after {took:.1f} s.')
        time.sleep(10)
        end_live()
        n = count(t, 'agent', second)
        said = runs_of(client, tid)
        rows.append(('Start the server again', said, n, count(t, 'helper'), count(t, 'scheduled job')))
        print(f'   Asked now, LangGraph says: {said}. The new server sent {n} updates on its own: '
              + ('the stopped run came back.' if n else 'the cancelled run did not come back.'))
        pause(auto)

        say('STEP 7. What is left, listed from outside the agent by the toolkit (stopkit), for the test account:')
        before = stops.leftovers(agent, files_ok=False)
        show_leftovers(before)
        state_dir = os.path.join(app, '.langgraph_api')
        saved = sorted(os.listdir(state_dir)) if os.path.isdir(state_dir) else []
        print(f"   LangGraph's saved state on disk, {state_dir}: {', '.join(saved) if saved else 'nothing'}.")
        print(f'   Asked now, the restarted server (a new process) says: {runs_of(client, tid)}.')
        pause(auto)

        say('STEP 8. The full stop, in the playbook\'s order: stop the server, remove the scheduled job, end every '
            'process of the account, and delete LangGraph\'s saved state.')
        go_live()
        server.ctrl_c()
        if not server.wait(CTRL_C_WAIT):
            server.end()
        claim, _ = stops.cleanup(agent)
        shutil.rmtree(state_dir, ignore_errors=True)
        time.sleep(1.0)
        end_live()
        print(f'   The toolkit\'s stop: {claim.message}.')
        print('   Listed again from outside:')
        after = stops.leftovers(agent, files_ok=False)
        show_leftovers(after)
        print(f"   LangGraph's saved state: {'still on disk' if os.path.exists(state_dir) else 'deleted'}.")
        t = time.time()
        go_live("Watching for 65 s, past the next minute when the scheduled job was due (LangGraph's log and "
                'answers, and the outside service):')
        time.sleep(65)
        end_live()
        got = sum(count(t, s) for s in ('agent', 'helper', 'scheduled job'))
        rows.append(('The toolkit\'s full stop', f'{len(before)} things listed before, {len(after)} after',
                     count(t, 'agent'), count(t, 'helper'), count(t, 'scheduled job')))
        print(f'   Received in those 65 s: {got}.')

        say('SUMMARY: what still reached the outside service after each stop (counted, not typed in).')
        print(f"   {'Stop':30} {'agent':>6} {'helper':>7} {'job':>4}   what LangGraph or the system said")
        for name, said, a, h, j in rows:
            print(f'   {name:30} {a:>6} {h:>7} {j:>4}   {said}')
        return 0
    finally:
        done.set()
        live.clear()
        if server.p is not None:
            server.end()
        stops.cleanup(agent)
        shutil.rmtree(os.path.join(app, '.langgraph_api'), ignore_errors=True)
        srv.shutdown()


if __name__ == '__main__':
    sys.exit(main())
