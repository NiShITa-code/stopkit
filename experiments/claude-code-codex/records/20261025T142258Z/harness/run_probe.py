#!/usr/bin/env python3
"""Client probe harness (see PREDICTIONS.md). Standard library only; runs in WSL.

Each run starts one client's interactive terminal interface in a pseudo-terminal, as the
foreground job of a bash shell, so the shell (not the client) holds the terminal session,
as in a real terminal. The client gets a fresh home folder, the probe MCP server
(probe_server.py) and a local stand-in for its model API (stub_model.py). The harness
answers first-run setup screens, waits until the client has listed the server's tools,
types the prompt, waits until the server logs that the tool's child started, applies the
stop path 1.2 s later and watches for 10 s. If the client is still running, it then types
one follow-up message, so that what the client tells the model about the stopped call
shows up in the next request. Everything is recorded outside the client.

Usage (in WSL):
  python3 run_probe.py smoke <client> <path>[,<path>...]   verbose runs, results/smoke-<time>/
  python3 run_probe.py batch                               the full batch, results/<time>/
Clients: claude-2.1.310 claude-2.1.308 codex-0.164.0 codex-0.165.0-alpha.1
Paths: control esc timeout bg-stop (Claude Code only) quit sigterm
"""

import fcntl
import hashlib
import json
import os
import pty
import random
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MC = os.path.expanduser("~/mcp-clients")
CLIENTS = {
    "claude-2.1.310": ("claude", f"{MC}/_anthropic-ai_claude-code_2.1.310/node_modules/@anthropic-ai/claude-code/bin/claude.exe"),
    "claude-2.1.308": ("claude", f"{MC}/_anthropic-ai_claude-code_2.1.308/node_modules/@anthropic-ai/claude-code/bin/claude.exe"),
    "codex-0.164.0": ("codex", f"{MC}/_openai_codex_0.164.0/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"),
    "codex-0.165.0-alpha.1": ("codex", f"{MC}/_openai_codex_0.165.0-alpha.1/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"),
}
PATHS = {"claude": ["esc", "timeout", "bg-stop", "quit", "sigterm"], "codex": ["esc", "timeout", "quit", "sigterm"]}
PORT = 18800
STEPS = 30                # the tool's child writes 30 markers, 0.5 s apart (15 s)
INTERVAL = 0.5
STOP_AFTER_START = 1.2
WATCH = 10.0
GRACE = 0.6
TIMEOUT_MS = 3000
BG_MS = 2000
SEED = 20261025
ROWS, COLS = 45, 140
PROMPT = "Call the slow tool from the probe MCP server with run_id {run_id}, then say done."
FOLLOW_UP = "What happened to the slow tool call?"

# Replies to terminal queries, as a terminal emulator would send them (some interfaces
# wait for these at start-up).
QUERIES = [
    (re.compile(rb"\x1b\[6n"), lambda m: b"\x1b[1;1R"),
    (re.compile(rb"\x1b\[0?c"), lambda m: b"\x1b[?1;2c"),
    (re.compile(rb"\x1b\[>0?c"), lambda m: b"\x1b[>0;0;0c"),
    (re.compile(rb"\x1b\](1[0-2]);\?(\x07|\x1b\\)"),
     lambda m: b"\x1b]" + m.group(1) + (b";rgb:0000/0000/0000" if m.group(1) == b"11" else b";rgb:ffff/ffff/ffff")
     + m.group(2)),
    (re.compile(rb"\x1b\]4;(\d+);\?(\x07|\x1b\\)"), lambda m: b"\x1b]4;" + m.group(1) + b";rgb:8080/8080/8080" + m.group(2)),
]
DIALOGS = [  # (pattern in newly shown text, keys, label)
    (re.compile(r"(?i)do you want to use this api key"), b"\x1b[A\r", "claude: approve the stand-in API key"),
    (re.compile(r"(?i)(do you trust the files|trust this folder|is this a project you created|quick safety check)"), b"\r",
     "trust the run folder"),
    (re.compile(r"(?i)(choose the text style|select a theme)"), b"\r", "claude: theme"),
    (re.compile(r"(?i)press enter to continue"), b"\r", "press enter to continue"),
    (re.compile(r"(?i)(allow codex to work in this folder|trust this directory|do you trust the contents)"), b"\r",
     "codex: trust folder"),
]
# Codex answers Ctrl+C during a running task with a menu; quit chooses "3. Exit: Stop the
# current task and exit Codex" (Down, Down, Enter from the first option).
QUIT_MENU = re.compile(r"(?i)choose what happens to the current task")
CSI = re.compile(rb"\x1b\[([0-9;:?<>=]*)[ -/]*([@-~])")
OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
DCS = re.compile(rb"\x1b[P^_X][^\x1b]*\x1b\\")
ESC2 = re.compile(rb"\x1b[()*+][0-9A-Za-z]|\x1b[=>78DEHMNOZc]")


def strip(raw):
    """Terminal output as plain text: cursor moves become line breaks, repeated lines are dropped."""
    raw = OSC.sub(b"", DCS.sub(b"", raw))
    raw = CSI.sub(lambda m: b"\n" if m.group(2) in b"HfABEFd" else (b" " if m.group(2) in b"CG" else b""), raw)
    raw = ESC2.sub(b"", raw)
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    out = []
    for line in (line.rstrip() for line in text.split("\n")):
        if line.strip() and (not out or out[-1] != line):
            out.append(line)
    return "\n".join(out)


class Term:
    """A pseudo-terminal running argv; records all output and answers terminal queries."""

    def __init__(self, argv, env, cwd, raw_path):
        self.buf = bytearray()
        self.last_out = time.time()
        self.lock = threading.Lock()
        self.wlock = threading.Lock()
        self.tail = b""
        self.raw = open(raw_path, "wb")
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
                os.chdir(cwd)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            window = self.tail + data
            found = sorted((m.start(), reply(m)) for pattern, reply in QUERIES
                           for m in pattern.finditer(window) if m.end() > len(self.tail))
            for _, answer in found:
                self.write(answer)
            self.tail = window[-64:]
            with self.lock:
                self.buf += data
                self.last_out = time.time()
            self.raw.write(data)
            self.raw.flush()

    def write(self, data, chunk=None):
        step = chunk or len(data)
        for i in range(0, len(data), step):
            with self.wlock:
                try:
                    os.write(self.fd, data[i:i + step])
                except OSError:
                    return
            if chunk:
                time.sleep(0.03)

    def pos(self):
        with self.lock:
            return len(self.buf)

    def text(self, since=0):
        with self.lock:
            raw = bytes(self.buf[since:])
        return strip(raw)

    def close(self):
        self.reader.join(timeout=3)
        for closer in (lambda: os.close(self.fd), self.raw.close):
            try:
                closer()
            except OSError:
                pass


# --------------------------------------------------------------------------- processes and logs

def stat(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return {"state": rest[0], "ppid": int(rest[1]), "pgid": int(rest[2]), "sid": int(rest[3]), "tpgid": int(rest[5])}
    except (OSError, IndexError, ValueError):
        return None


def alive(pid):
    s = stat(pid) if pid else None
    return bool(s) and s["state"] not in ("Z", "X")


def all_pids():
    return [int(d) for d in os.listdir("/proc") if d.isdigit()]


def children(pid):
    return [p for p in all_pids() if (stat(p) or {}).get("ppid") == pid]


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def with_home(home):
    """Processes whose environment has HOME=<home>: everything a run's client started."""
    key = f"HOME={home}".encode()
    out = []
    for p in all_pids():
        try:
            with open(f"/proc/{p}/environ", "rb") as f:
                if key in f.read().split(b"\0"):
                    out.append(p)
        except OSError:
            pass
    return out


def process_tree(home):
    """Every process the run's client started, with its parent, group and session."""
    out = []
    for p in sorted(with_home(home)):
        s = stat(p)
        if s and s["state"] not in ("Z", "X"):
            out.append({"pid": p, "ppid": s["ppid"], "pgid": s["pgid"], "sid": s["sid"], "cmd": cmdline(p)[:300]})
    return out


def read_text(path, tries=5):
    """A file another process may be writing; retries a failed read (WSL's /mnt/c can
    report ENODATA mid-write). Only complete lines are used by the callers."""
    for _ in range(tries):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""
        except OSError:
            time.sleep(0.05)
    return ""


def complete_lines(path):
    text = read_text(path)
    return text.split("\n")[:-1] if text else []


def read_jsonl(path):
    out = []
    for line in complete_lines(path):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def read_marks(path):
    return [float(line.split()[0]) for line in complete_lines(path) if line.strip()]


def first_event(path, test):
    return next((e for e in read_jsonl(path) if test(e)), None)


def taskstop_request(stub_log):
    return next((s for s in read_jsonl(stub_log) if (s.get("reply") or {}).get("kind") == "tool"
                 and "stop" in str((s.get("reply") or {}).get("name", "")).lower()), None)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- client set-up

def claude_setup(run, home, project, env):
    cfg_dir = os.path.join(home, ".claude")
    os.makedirs(cfg_dir, exist_ok=True)
    state = {"hasCompletedOnboarding": True, "lastOnboardingVersion": run["client"].split("-", 1)[1],
             "theme": "dark", "numStartups": 5, "autoUpdates": False,
             "customApiKeyResponses": {"approved": [env["ANTHROPIC_API_KEY"][-20:]], "rejected": []},
             "projects": {project: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True,
                                    "allowedTools": [], "projectOnboardingSeenCount": 5}}}
    for path in (os.path.join(home, ".claude.json"), os.path.join(cfg_dir, ".claude.json")):
        with open(path, "w") as f:
            json.dump(state, f)
    server = {"type": "stdio", "command": "python3", "args": [os.path.join(HERE, "probe_server.py")],
              "env": {"REC_LOG": run["server_log"], "REC_MARK": run["mark"], "REC_STEPS": str(STEPS)}}
    if run["path"] == "timeout":
        server["timeout"] = TIMEOUT_MS
    mcp_path = os.path.join(home, "mcp.json")
    with open(mcp_path, "w") as f:
        json.dump({"mcpServers": {"probe": server}}, f)
    return ["--mcp-config", mcp_path, "--strict-mcp-config", "--allowedTools", "mcp__probe__slow,TaskStop"]


def codex_setup(run, codex_home, project, binary):
    os.makedirs(codex_home, exist_ok=True)
    # Codex runs MCP servers in a shared app-server daemon, which it installs on first
    # launch by copying its whole package (about 430 MB) into CODEX_HOME/packages. A
    # returning user already has it; here it is pre-installed as hard links to the
    # installed client's files, so no copy is made.
    vendor = os.path.dirname(os.path.dirname(binary))
    with open(os.path.join(vendor, "codex-package.json")) as f:
        package = json.load(f)
    release = os.path.join(codex_home, "packages", "app-server-daemon", "releases",
                           f'{package["version"]}-{package["target"]}')
    os.makedirs(os.path.dirname(release))
    subprocess.run(["cp", "-al", vendor, release], check=True)
    os.symlink("bin/codex", os.path.join(release, "codex"))
    # The daemon's own updater checks for new releases online; switched off like the
    # other update checks. Its 60 s shutdown grace stays at the default.
    os.makedirs(os.path.join(codex_home, "app-server-daemon"))
    with open(os.path.join(codex_home, "app-server-daemon", "settings.json"), "w") as f:
        json.dump({"updater": {"autoUpdateEnabled": False}}, f)
    timeout = TIMEOUT_MS // 1000 if run["path"] == "timeout" else 120
    lines = [
        'model = "gpt-5-mini"',
        'model_provider = "stub"',
        'approval_policy = "never"',
        'check_for_update_on_startup = false',
        '',
        '[features]',
        'plugins = false',
        '',
        '[analytics]',
        'enabled = false',
        '',
        '[otel]',
        'metrics_exporter = "none"',
        '',
        '[history]',
        'persistence = "none"',
        '',
        '[model_providers.stub]',
        'name = "stub"',
        f'base_url = "http://127.0.0.1:{PORT}/v1"',
        'env_key = "STUB_API_KEY"',
        'wire_api = "responses"',
        '',
        '[mcp_servers.probe]',
        'command = "python3"',
        f'args = ["{os.path.join(HERE, "probe_server.py")}"]',
        f'tool_timeout_sec = {timeout}',
        'default_tools_approval_mode = "approve"',
        '',
        '[mcp_servers.probe.env]',
        f'REC_LOG = "{run["server_log"]}"',
        f'REC_MARK = "{run["mark"]}"',
        f'REC_STEPS = "{STEPS}"',
        '',
        f'[projects."{project}"]',
        'trust_level = "trusted"',
    ]
    with open(os.path.join(codex_home, "config.toml"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return []


def set_control(control_path, run):
    tmp = control_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"run_id": run["run_id"], "log": run["stub_log"], "scenario": run["path"]}, f)
    os.replace(tmp, control_path)


# --------------------------------------------------------------------------- one run

def wait_ready(term, run, client_pid, max_wait=90.0):
    """Answer setup screens until the client has listed the server's tools and the screen is quiet."""
    start, scan_from, seen, t_list = time.time(), 0, [], None
    while time.time() - start < max_wait:
        time.sleep(0.3)
        if not alive(client_pid):
            return seen, "client exited"
        new = term.text(since=scan_from)
        for pattern, keys, label in DIALOGS:
            if pattern.search(new):
                scan_from = term.pos()
                term.write(keys)
                seen.append(label)
                break
        if t_list is None:
            listed = first_event(run["server_log"], lambda e: e.get("event") == "in" and e.get("method") == "tools/list")
            t_list = listed and time.time()
        if t_list and (time.time() - term.last_out > 1.0 or time.time() - t_list > 6.0):
            return seen, "ready"
    return seen, "timed out"


def run_one(n, client, path, dirs, control_path, verbose=False):
    kind, binary = CLIENTS[client]
    run_id = f"p{n:02d}-{client}-{path}"
    live = os.path.join(dirs["live"], run_id)
    home, project, logs = os.path.join(live, "home"), os.path.join(live, "project"), os.path.join(live, "logs")
    for d in (home, project, logs):
        os.makedirs(d)
    # Logs are written on WSL's own file system during the run and copied into the
    # results after clean-up (reading /mnt/c while another process writes it can fail).
    run = {"run_id": run_id, "client": client, "kind": kind, "path": path,
           "server_log": os.path.join(logs, "server.jsonl"), "mark": os.path.join(logs, "marks.log"),
           "stub_log": os.path.join(logs, "stub.jsonl"), "tty": os.path.join(logs, "tty.raw")}
    copies = [(run["server_log"], os.path.join(dirs["serverlogs"], f"{run_id}.jsonl")),
              (run["mark"], os.path.join(dirs["marks"], f"{run_id}.log")),
              (run["stub_log"], os.path.join(dirs["stub"], f"{run_id}.jsonl")),
              (run["tty"], os.path.join(dirs["tty"], f"{run_id}.raw"))]
    env = {"HOME": home, "PATH": "/usr/local/bin:/usr/bin:/bin", "TERM": "xterm-256color", "SHELL": "/bin/bash",
           "USER": os.environ.get("USER", "root"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if kind == "claude":
        env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PORT}",
                    "ANTHROPIC_API_KEY": "sk-ant-api03-stub-" + hashlib.sha256(run_id.encode()).hexdigest()[:40],
                    "ANTHROPIC_MODEL": "claude-haiku-5-5",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
                    "DISABLE_AUTOUPDATER": "1"})
        if path == "bg-stop":
            env["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"] = str(BG_MS)
        args = claude_setup(run, home, project, env)
    else:
        env.update({"CODEX_HOME": os.path.join(home, ".codex"), "STUB_API_KEY": "stub-key"})
        args = codex_setup(run, env["CODEX_HOME"], project, binary)
    set_control(control_path, run)
    client_cmd = shlex.join([binary] + args)
    shell_cmd = f"set -m; {client_cmd}; s=$?; echo; echo __CLIENT_EXIT__ $s; exec sleep 600"

    rec = {k: run[k] for k in ("run_id", "client", "kind", "path")}
    rec["client_args"] = args
    term = Term(["/bin/bash", "--norc", "--noprofile", "-c", shell_cmd], env, project, run["tty"])
    rec["shell_pid"] = term.pid
    client_pid = None
    try:
        deadline = time.time() + 10
        while client_pid is None and time.time() < deadline:
            kids = [p for p in children(term.pid) if os.path.exists(f"/proc/{p}/exe")
                    and os.path.realpath(f"/proc/{p}/exe") == os.path.realpath(binary)]
            client_pid = kids[0] if kids else None
            time.sleep(0.05)
        if client_pid is None:
            rec["verdict"] = "EXCLUDED: client process not found"
            return rec
        rec["client_pid"] = client_pid
        rec["client_stat"] = stat(client_pid)

        rec["dialogs"], rec["ready"] = wait_ready(term, run, client_pid)
        rec["t_ready"] = time.time()
        if rec["ready"] != "ready":
            rec["verdict"] = f"EXCLUDED: client not ready ({rec['ready']})"
            rec["screen_end"] = term.text()[-3000:]
            return rec
        term.write(PROMPT.format(run_id=run_id).encode(), chunk=24)
        time.sleep(0.6)
        term.write(b"\r")
        rec["t_prompt"] = time.time()
        if verbose:
            print(f"[{run_id}] prompt sent; dialogs={rec['dialogs']}", flush=True)

        spawn, deadline = None, time.time() + 45
        while spawn is None and time.time() < deadline and alive(client_pid):
            spawn = first_event(run["server_log"], lambda e: e.get("event") == "child_spawned")
            time.sleep(0.05)
        if spawn is None:
            rec["verdict"] = "EXCLUDED: the tool never started"
            rec["screen_end"] = term.text()[-3000:]
            return rec
        t_start = rec["t_start"] = spawn["t"]
        rec["tree_at_start"] = process_tree(home)
        call = first_event(run["server_log"], lambda e: e.get("event") == "in" and e.get("method") == "tools/call")
        t_call = rec["t_call"] = call["t"] if call else t_start

        # Apply the stop and watch.
        t_action = t_start + STOP_AFTER_START
        time.sleep(max(0.0, t_action - time.time()))
        t_action = rec["t_action"] = time.time()
        pos_action = term.pos()
        rec["actions"] = []
        quit_next, quit_presses, menu_from, menus = t_action, 0, pos_action, 0

        def act(what, data=None):
            rec["actions"].append([round(time.time() - t_action, 3), what, data])

        if path == "esc":
            term.write(b"\x1b")
            act("key", "Esc")
        elif path == "sigterm":
            os.kill(client_pid, signal.SIGTERM)
            act("signal", "SIGTERM")
        while True:
            now = time.time()
            if path == "quit" and now >= quit_next and alive(client_pid):
                if menus < 2 and QUIT_MENU.search(term.text(since=menu_from)):
                    for key in (b"\x1b[B", b"\x1b[B", b"\r"):
                        term.write(key)
                        time.sleep(0.1)
                    act("menu", "Down Down Enter (3. Exit)")
                    menu_from, menus, quit_next = term.pos(), menus + 1, time.time() + 1.0
                elif quit_presses < 6:
                    term.write(b"\x03")
                    act("key", "Ctrl+C")
                    quit_presses, quit_next = quit_presses + 1, time.time() + 0.3
            if not alive(client_pid) and "client_exit_rel_action" not in rec:
                rec["client_exit_rel_action"] = round(now - t_action, 3)
            if path in ("esc", "quit", "sigterm"):
                t_end = t_action + WATCH
            elif path == "timeout":
                t_end = t_call + TIMEOUT_MS / 1000 + WATCH
            elif path == "bg-stop":
                req = taskstop_request(run["stub_log"])
                t_end = req["t"] + WATCH if req else t_action + 20.0
            else:
                done = first_event(run["server_log"], lambda e: e.get("event") == "result_sent")
                t_end = done["t"] + 3.0 if done else t_start + STEPS * INTERVAL + 10.0
            if now >= t_end:
                break
            time.sleep(0.1)
        rec["t_end"] = time.time()
        rec["client_alive_end"] = alive(client_pid)
        server_start = first_event(run["server_log"], lambda e: e.get("event") == "start")
        rec["server_pid"] = server_start["pid"] if server_start else None
        rec["server_alive_end"] = alive(rec["server_pid"])
        rec["child_pid"] = spawn["pid"]
        rec["child_alive_end"] = alive(spawn["pid"])
        rec["child_stat_end"] = stat(spawn["pid"]) if rec["child_alive_end"] else None
        rec["screen_end"] = term.text(since=pos_action)[-3000:]

        # One follow-up message, so that what the client tells the model shows up in a request.
        if path != "control" and alive(client_pid):
            pos_follow = term.pos()
            term.write(FOLLOW_UP.encode(), chunk=24)
            time.sleep(0.6)
            term.write(b"\r")
            t_follow = rec["t_follow"] = time.time()
            seen_at = None
            while time.time() < t_follow + 12 and (seen_at is None or time.time() < seen_at + 2.0):
                if seen_at is None and any(s["t"] > t_follow and FOLLOW_UP in json.dumps(s.get("body"))
                                           for s in read_jsonl(run["stub_log"])):
                    seen_at = time.time()
                time.sleep(0.2)
            rec["follow_seen"] = seen_at is not None
            rec["screen_follow"] = term.text(since=pos_follow)[-2000:]
        # Exploratory, outside the predictions: if the client has exited but processes it
        # started are still running, keep watching until the tool would have finished, to
        # see whether anything carries the turn on without the interface.
        elif not alive(client_pid) and any(alive(p) for p in with_home(home) if p != term.pid):
            rec["after_exit_left"] = process_tree(home)
            until = t_start + STEPS * INTERVAL + 6.0
            while time.time() < until and any(alive(p) for p in with_home(home) if p != term.pid):
                time.sleep(0.2)
            rec["t_after_exit_end"] = time.time()
        rec["t_follow_end"] = time.time()
        return rec
    finally:
        rec.update(cleanup(term, home, run, rec))
        for src, dst in copies:
            if os.path.exists(src):
                shutil.copyfile(src, dst)
        rec.update(measure(rec, run))
        save_client_state(kind, home, os.path.join(dirs["client_state"], run_id))


def save_client_state(kind, home, dest):
    """Copy the client's own records of the session (its transcript and logs), flattened
    to <folder>__<file> so the copies stay within Windows' 260-character path limit."""
    root = os.path.join(home, ".codex" if kind == "codex" else ".claude")
    picks = (["sessions", "log", "app-server-daemon", "logs_2.sqlite", "logs_2.sqlite-wal"] if kind == "codex"
             else ["projects", "debug"])
    for name in picks:
        src = os.path.join(root, name)
        paths = [src] if os.path.isfile(src) else [os.path.join(d, f) for d, _, fs in os.walk(src) for f in fs]
        for path in paths:
            if os.path.isfile(path) and not os.path.islink(path) and os.path.getsize(path) < 20 << 20:
                os.makedirs(dest, exist_ok=True)
                shutil.copyfile(path, os.path.join(dest, f"{name}__{os.path.basename(path)}"))


def cleanup(term, home, run, rec):
    """Kill everything the run started, then confirm nothing is left. Repeated, because a
    loop being killed can start one last short-lived process (its `sleep`)."""
    killed = set()
    for attempt in range(4):
        victims = set(p for p in with_home(home) if alive(p)) | ({term.pid} if attempt == 0 else set())
        for key in ("server_pid", "child_pid"):
            pid = rec.get(key)
            if pid and alive(pid) and ("probe_server.py" in cmdline(pid) or run["mark"] in cmdline(pid)):
                victims.add(pid)
        if not victims:
            break
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        killed |= victims
        if attempt == 0:
            try:
                os.waitpid(term.pid, 0)
            except ChildProcessError:
                pass
        deadline = time.time() + 1.0
        while time.time() < deadline and any(alive(p) for p in victims):
            time.sleep(0.1)
    victims = killed
    term.close()
    exit_code = re.search(r"__CLIENT_EXIT__ (\d+)", term.text())
    return {"killed_at_cleanup": sorted(p for p in victims if p != term.pid),
            "left_after_cleanup": [p for p in with_home(home) if alive(p)],
            "client_exit_code": int(exit_code.group(1)) if exit_code else None}


# --------------------------------------------------------------------------- measures

def tool_results(body):
    """{tool name: text of its latest result} from one model request (either API)."""
    names, results = {}, {}
    for message in body.get("messages") or []:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                names[block.get("id")] = block.get("name")
            elif block.get("type") == "tool_result":
                results[names.get(block.get("tool_use_id"), block.get("tool_use_id"))] = json.dumps(block.get("content"))
    for item in body.get("input") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            names[item.get("call_id")] = item.get("name")
        elif item.get("type") == "function_call_output":
            results[names.get(item.get("call_id"), item.get("call_id"))] = json.dumps(item.get("output"))
    return results


def measure(rec, run):
    """The run's measures, from the raw logs (check_probe.py re-derives them independently)."""
    out = {}
    t_end = rec.get("t_end")
    if t_end is None or "t_start" not in rec:
        return out
    t_follow_end = rec.get("t_follow_end", t_end)
    events = read_jsonl(run["server_log"])
    stub = read_jsonl(run["stub_log"])
    marks = read_marks(run["mark"])
    call = next((e for e in events if e.get("event") == "in" and e.get("method") == "tools/call"), None)
    call_id = call.get("id") if call else None
    if rec["path"] in ("esc", "quit", "sigterm"):
        t_stop = rec["t_action"]
    elif rec["path"] == "timeout":
        t_stop = rec["t_call"] + TIMEOUT_MS / 1000
    elif rec["path"] == "bg-stop":
        req = taskstop_request(run["stub_log"])
        t_stop = req["t"] if req else None
    else:
        t_stop = None
    out["t_stop"] = t_stop
    ref = t_stop if t_stop is not None else rec["t_action"]

    def rel(t):
        return round(t - ref, 3)

    window = [e for e in events if e["t"] <= t_end]
    cancels = [e for e in events if e.get("event") == "in" and e.get("method") == "notifications/cancelled"
               and (e.get("params") or {}).get("requestId") == call_id]
    in_window = [e for e in cancels if e["t"] <= t_end]
    out["cancel"] = bool(in_window)
    out["cancel_rel"] = rel(in_window[0]["t"]) if in_window else None
    out["cancel_reason"] = (in_window[0].get("params") or {}).get("reason") if in_window else None
    out["cancel_after_watch"] = any(t_end < e["t"] <= t_follow_end for e in cancels)
    out["messages_after_call"] = [e.get("method") for e in window
                                  if e.get("event") == "in" and call and e["t"] > call["t"]][:10]
    out["signals"] = [[e["name"], rel(e["t"])] for e in window if e.get("event") == "signal"]
    eof = next((e for e in window if e.get("event") == "stdin_eof"), None)
    out["stdin_eof_rel"] = rel(eof["t"]) if eof else None
    ended = next((e for e in window if e.get("event") == "exit"), None)
    out["server_exit"] = [ended["reason"], rel(ended["t"])] if ended else None
    sent = next((e for e in window if e.get("event") == "result_sent"), None)
    out["result_sent_rel"] = rel(sent["t"]) if sent else None
    start = next((e for e in events if e.get("event") == "start"), None)
    spawn = next((e for e in events if e.get("event") == "child_spawned"), None)
    out["server_ids"] = {k: start.get(k) for k in ("pid", "pgid", "sid", "ppid")} if start else None
    out["child_pgid"] = spawn.get("pgid") if spawn else None
    out["markers_total"] = len(marks)
    if t_stop is not None:
        out["markers_after_stop"] = sum(1 for t in marks if t_stop + GRACE < t <= t_end)
        out["work"] = "NOT STOPPED" if out["markers_after_stop"] else "STOPPED"
    else:
        out["work"] = "CONTROL: finished and replied" if sent else "CONTROL: no reply in the watch"
    told = {}
    for s in stub:
        if ref <= s["t"] <= t_follow_end:
            told.update(tool_results(s.get("body") or {}))
    out["model_told"] = {name: text[:300] for name, text in told.items()}
    # After the watch (the follow-up, or the exploratory watch after the client exited).
    out["late_server_events"] = [[e.get("event"), e.get("method") or e.get("name") or e.get("reason"), rel(e["t"])]
                                 for e in events if t_end < e["t"] <= t_follow_end]
    if rec.get("client_exit_rel_action") is not None:
        t_exit = rec["t_action"] + rec["client_exit_rel_action"]
        out["requests_after_client_exit"] = [[rel(s["t"]), (s.get("reply") or {}).get("kind"),
                                              list(tool_results(s.get("body") or {}))] for s in stub if s["t"] > t_exit]
    out["stub_requests"] = [[round(s["t"] - rec["t_ready"], 2), s.get("path"), s.get("bytes"),
                             (s.get("reply") or {}).get("kind")] for s in stub]
    return out


# --------------------------------------------------------------------------- batch

def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    mode = sys.argv[1]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if mode == "smoke":
        runs = [(sys.argv[2], p) for p in sys.argv[3].split(",")]
        batch = f"smoke-{stamp}"
    else:
        runs = [(c, "control") for c in CLIENTS] + [(c, p) for c in CLIENTS for p in PATHS[CLIENTS[c][0]]
                                                    for _ in range(3)]
        random.Random(SEED).shuffle(runs)
        batch = stamp
    out = os.path.join(HERE, "results", batch)
    dirs = {"serverlogs": os.path.join(out, "serverlogs"), "marks": os.path.join(out, "marks"),
            "stub": os.path.join(out, "stub"), "tty": os.path.join(out, "tty"),
            "client_state": os.path.join(out, "client-state"),
            "live": os.path.expanduser(f"~/client-probe-live/{batch}")}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    if port_open(PORT):
        sys.exit(f"port {PORT} is already in use")
    control_path = os.path.join(dirs["live"], "stub-control.json")
    stub = subprocess.Popen([sys.executable, os.path.join(HERE, "stub_model.py"), str(PORT), control_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 5
    while not port_open(PORT) and time.time() < deadline:
        time.sleep(0.1)
    manifest = {"batch": batch, "mode": mode, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "clients": {c: {"path": os.path.realpath(b), "sha256": sha256(b)} for c, (_, b) in CLIENTS.items()
                            if any(c == r[0] for r in runs)},
                "files_sha256": {name: sha256(os.path.join(HERE, name))
                                 for name in ("PREDICTIONS.md", "run_probe.py", "probe_server.py", "stub_model.py")},
                "constants": {"STEPS": STEPS, "INTERVAL": INTERVAL, "STOP_AFTER_START": STOP_AFTER_START,
                              "WATCH": WATCH, "GRACE": GRACE, "TIMEOUT_MS": TIMEOUT_MS, "BG_MS": BG_MS,
                              "SEED": SEED, "PROMPT": PROMPT, "FOLLOW_UP": FOLLOW_UP},
                "order": [f"{c}/{p}" for c, p in runs]}
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    def attempt(n, client, path):
        try:
            return run_one(n, client, path, dirs, control_path, verbose=(mode == "smoke"))
        except Exception as exc:  # a harness fault must not end the batch; the run is excluded
            return {"run_id": f"p{n:02d}-{client}-{path}", "client": client, "kind": CLIENTS[client][0],
                    "path": path, "verdict": f"EXCLUDED: harness error ({type(exc).__name__}: {exc})"}

    try:
        with open(os.path.join(out, "runs.jsonl"), "w") as f:
            for n, (client, path) in enumerate(runs, 1):
                rec = attempt(n, client, path)
                if rec.get("verdict", "").startswith(("EXCLUDED: client not ready", "EXCLUDED: harness error")):
                    # Both records are kept; the exclusion is reported with the results.
                    f.write(json.dumps(rec) + "\n")
                    print(f"{rec['run_id']:<34} {rec['verdict']}; repeating once", flush=True)
                    rec = attempt(n + 100, client, path)
                    rec["repeat_of"] = f"p{n:02d}-{client}-{path}"
                f.write(json.dumps(rec) + "\n")
                f.flush()
                print(f"{rec['run_id']:<34} {rec.get('verdict', rec.get('work'))} | cancel={rec.get('cancel')} "
                      f"{rec.get('cancel_rel')} {rec.get('cancel_reason')!r} | signals={rec.get('signals')} "
                      f"eof={rec.get('stdin_eof_rel')} exit={rec.get('server_exit')} | after={rec.get('markers_after_stop')} "
                      f"client_exit={rec.get('client_exit_rel_action')} left={rec.get('left_after_cleanup')}", flush=True)
                if mode == "smoke":
                    print(json.dumps({k: rec.get(k) for k in ("dialogs", "ready", "client_stat", "server_ids",
                                                              "child_pgid", "actions", "model_told",
                                                              "stub_requests", "follow_seen")}, indent=1))
                    print("---- screen after the stop ----\n" + (rec.get("screen_end") or "")[-1800:])
                    print("---- screen after the follow-up ----\n" + (rec.get("screen_follow") or "")[-1200:], flush=True)
                time.sleep(1.0)
    finally:
        os.killpg(stub.pid, signal.SIGKILL)
        # The Codex runs hard-link the installed package; confirm no run changed it.
        after = {c: sha256(b) for c, (_, b) in CLIENTS.items() if c in manifest["clients"]}
        with open(os.path.join(out, "clients-after.json"), "w") as f:
            json.dump({"sha256": after, "unchanged": all(after[c] == manifest["clients"][c]["sha256"] for c in after)},
                      f, indent=2)
        sums = {}
        for root, _, files in os.walk(out):
            for name in sorted(files):
                if name != "SHA256SUMS.json":
                    p = os.path.join(root, name)
                    sums[os.path.relpath(p, out)] = sha256(p)
        with open(os.path.join(out, "SHA256SUMS.json"), "w") as f:
            json.dump(sums, f, indent=2)
        print(f"results: {out}")


if __name__ == "__main__":
    main()
