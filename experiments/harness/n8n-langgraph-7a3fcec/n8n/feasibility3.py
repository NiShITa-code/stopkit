#!/usr/bin/env python3
"""Experiment B setup checks for RULES-expB draft 2 (unscored; results kept apart from every scored trial).

Runs as root inside a named network namespace (feasibility3_ns.sh). Three checks:
  F1 systemd units: n8n main and worker as transient units (KillMode=control-group, TimeoutStopSec=40,
     Restart=no, User=n8ntest, the trial's network namespace, /mnt hidden, private /tmp, private crontab spool).
     Isolation of the unit processes; cgroup of planted Execute Command children (background, setsid-detached, and
     one that ignores SIGTERM); `systemctl stop` of the worker unit (SIGTERM to the whole cgroup, then SIGKILL
     after TimeoutStopSec); `systemctl kill -s SIGKILL` of the worker unit.
  F2 private cron daemon: a trial-private `cron -f` unit with the same confinement, an empty /etc/crontab and
     /etc/cron.d, its own pidfile and the private spool. The cron-launched canary reports its uid, namespaces,
     cgroup and whether /mnt/c is visible. The cron daemon and its job must survive the worker-unit stops.
  F3 AI Agent: n8n's AI Agent node with a scripted stand-in chat model (stub_model.py) and an HTTP Request tool
     that pings the recorder. Two runs must give the same number of tool calls, end the same way, and make no
     request anywhere but the stand-in.
Results go to n8n/results/feasibility3-<time>/.
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LG = os.path.join(os.path.dirname(HERE), "langgraph")
PY = "/root/pilot-venv/bin/python"
SYSPY = "/usr/bin/python3"
NODE = "/opt/node-v24.21.0/bin/node"
N8N = "/opt/n8n-2.41.7/node_modules/n8n/bin/n8n"
PGBIN = "/usr/lib/postgresql/17/bin"
USER = "n8ntest"
NS = "stopcheck-f3"
W = "/var/lib/stopcheck-n8n/f3"
BIN, SPOOL, LOGS, ETC = f"{W}/bin", f"{W}/spool", f"{W}/logs", f"{W}/etc"
RSOCK = f"{W}/sock/recorder.sock"
CANARY = f"{BIN}/canary_b.py"
TRIAL = "F3"
U_MAIN, U_CRON = "n8n-main-f3.service", "stopcheck-cron-f3.service"
U_WORKERS = ["n8n-worker-f3-1.service", "n8n-worker-f3-2.service", "n8n-worker-f3-3.service"]
U_WORKER = U_WORKERS[0]
OUT = os.path.join(HERE, "results", "feasibility3-" + time.strftime("%Y%m%d-%H%M%S"))
BASE = "http://127.0.0.1:5678"
now = time.monotonic
PROCS = {}
REPORT = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "scope": "unscored setup checks; native WSL2 install",
          "checks": {}, "notes": []}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(OUT, "run.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, timeout=60, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def env_file():
    vals = {}
    for ln in open(os.path.join(HERE, "n8n-test.env"), encoding="utf-8"):
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.strip().split("=", 1)
            vals[k] = v
    return vals


def n8n_env(secrets, role):
    e = {"HOME": f"{W}/n8n", "PATH": "/opt/node-v24.21.0/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
         "DB_TYPE": "postgresdb", "DB_POSTGRESDB_HOST": "127.0.0.1", "DB_POSTGRESDB_PORT": "5433",
         "DB_POSTGRESDB_DATABASE": "n8n", "DB_POSTGRESDB_USER": "postgres", "DB_POSTGRESDB_PASSWORD": "",
         "EXECUTIONS_MODE": "queue", "QUEUE_BULL_REDIS_HOST": "127.0.0.1", "QUEUE_BULL_REDIS_PORT": "6380",
         "N8N_ENCRYPTION_KEY": secrets["N8N_ENCRYPTION_KEY"], "N8N_USER_FOLDER": f"{W}/n8n",
         "N8N_PORT": "5678", "N8N_LISTEN_ADDRESS": "127.0.0.1", "N8N_HOST": "127.0.0.1", "N8N_PROTOCOL": "http",
         "N8N_WEBHOOK_URL": "http://127.0.0.1:5678/", "N8N_SECURE_COOKIE": "false",
         "N8N_RUNNERS_MODE": "internal", "N8N_RUNNERS_TASK_TIMEOUT": "300",
         "N8N_DIAGNOSTICS_ENABLED": "false", "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
         "N8N_TEMPLATES_ENABLED": "false", "N8N_PERSONALIZATION_ENABLED": "false",
         "N8N_HIRING_BANNER_ENABLED": "false", "EXTERNAL_FRONTEND_HOOKS_URLS": "",
         "NODES_EXCLUDE": '["n8n-nodes-base.localFileTrigger","n8n-nodes-base.readWriteFile"]',
         "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "true", "N8N_LOG_LEVEL": "info",
         "N8N_GRACEFUL_SHUTDOWN_TIMEOUT": "30", "QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD": "10000"}
    e["N8N_RUNNERS_BROKER_PORT"] = "5681" if role == "main" else "5682"
    if role == "worker":
        e.update({"QUEUE_HEALTH_CHECK_ACTIVE": "true", "QUEUE_HEALTH_CHECK_PORT": "5680"})
    return e


def write_env(path, env):
    with open(path, "w", encoding="utf-8") as f:
        for k, v in env.items():
            assert "'" not in v, k
            f.write(f"{k}='{v}'\n")  # single quotes: taken literally by systemd's EnvironmentFile parser
    os.chmod(path, 0o600)


CONFINE = [f"NetworkNamespacePath=/run/netns/{NS}", "TemporaryFileSystem=/mnt", "PrivateTmp=yes",
           f"BindPaths={SPOOL}:/var/spool/cron/crontabs"]


def unit_start(unit, cmd, props):
    """Start a transient service unit. Returns the systemd-run result."""
    args = ["systemd-run", f"--unit={unit}", "--collect", "--quiet"]
    for p in props:
        args += ["-p", p]
    r = run(args + ["--"] + cmd)
    log(f"systemd-run {unit}: rc {r.returncode} {r.stderr.strip()[:200]}")
    return r


def n8n_unit(unit, role, secrets):
    envf = f"{ETC}/{role}.env"
    write_env(envf, n8n_env(secrets, role))
    logf = f"{LOGS}/{role}.log"
    props = ["KillMode=control-group", "TimeoutStopSec=40", "Restart=no", f"User={USER}",
             f"WorkingDirectory={W}/n8n", f"EnvironmentFile={envf}",
             f"StandardOutput=append:{logf}", f"StandardError=append:{logf}"] + CONFINE
    return unit_start(unit, [NODE, N8N, "start" if role == "main" else "worker"], props)


def unit_show(unit, props=("ActiveState", "SubState", "MainPID", "ControlGroup", "KillMode", "TimeoutStopUSec",
                           "Restart", "User", "NetworkNamespacePath", "TemporaryFileSystem", "PrivateTmp",
                           "BindPaths", "Result")):
    out = run(["systemctl", "show", unit, "-p", ",".join(props)]).stdout
    return dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)


def wait_inactive(unit, limit=30):
    t0 = now()
    while now() - t0 < limit:
        st = unit_show(unit, ("ActiveState", "LoadState"))
        if st.get("ActiveState") in ("inactive", "failed", "") or st.get("LoadState") == "not-found":
            return True
        time.sleep(0.5)
    return False


def start(name, cmd, user=None):
    if user:
        cmd = ["runuser", "-u", user, "--"] + cmd
    f = open(os.path.join(OUT, f"{name}.log"), "ab")
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    PROCS[name] = p
    log(f"started {name} pid {p.pid}")
    return p


def stop(name, sig=signal.SIGTERM, wait=20):
    p = PROCS.get(name)
    if not p or p.poll() is not None:
        return
    try:
        os.killpg(p.pid, sig)
        p.wait(wait)
    except Exception:  # noqa: BLE001
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def http(method, path, body=None, headers=None, base=BASE, timeout=10):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode(errors="replace")), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:600], e.headers
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}", None


def wait_for(fn, limit, step=0.5):
    t0 = now()
    while now() - t0 < limit:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def pings(canary=None):
    path = os.path.join(OUT, "pings.jsonl")
    if not os.path.exists(path):
        return []
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    return [r for r in rows if canary is None or r.get("canary") == canary]


def proc_info(pid):
    def link(p):
        try:
            return os.readlink(p)
        except OSError:
            return None
    try:
        st = open(f"/proc/{pid}/status", encoding="utf-8").read()
        uid = next((ln.split()[1] for ln in st.splitlines() if ln.startswith("Uid:")), None)
        cg = open(f"/proc/{pid}/cgroup", encoding="utf-8").read().strip()
        args = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")[:140]
        stat = open(f"/proc/{pid}/stat", encoding="utf-8").read()
        rest = stat[stat.rindex(")") + 2:].split()
        pgid, sid = int(rest[2]), int(rest[3])
    except OSError:
        return None
    return {"pid": pid, "uid": uid, "pgid": pgid, "sid": sid, "cgroup": cg, "netns": link(f"/proc/{pid}/ns/net"),
            "mntns": link(f"/proc/{pid}/ns/mnt"), "mnt_c_visible": os.path.exists(f"/proc/{pid}/root/mnt/c/Windows"),
            "args": args}


def pids_in(control_group):
    """Every process whose cgroup is this unit's control group (read from /proc, not /sys/fs/cgroup)."""
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                cg = open(f"/proc/{d}/cgroup", encoding="utf-8").read()
            except OSError:
                continue
            if control_group and any(ln.endswith(":" + control_group) for ln in cg.splitlines()):
                out.append(int(d))
    return sorted(out)


def user_pids():
    return [int(x) for x in run(["pgrep", "-u", USER]).stdout.split()]


def main():
    global U_WORKER
    os.makedirs(OUT, exist_ok=True)
    secrets = env_file()
    ns_net = os.readlink("/proc/self/ns/net")
    host_mnt = os.readlink("/proc/1/ns/mnt")
    REPORT["trial_netns"], REPORT["host_mntns"] = ns_net, host_mnt
    uid = run(["id", "-u", USER]).stdout.strip()
    # ---- folders
    for u in [U_MAIN, U_CRON] + U_WORKERS:
        run(["systemctl", "stop", u])
        run(["systemctl", "reset-failed", u])
    shutil.rmtree(W, ignore_errors=True)
    for d, owner, mode in (("pg", "postgres", 0o700), ("pgsock", "postgres", 0o755), ("redis", "redis", 0o755),
                           ("n8n", USER, 0o755), ("bin", "root", 0o755), ("sock", "root", 0o755),
                           ("logs", "root", 0o755), ("etc", "root", 0o755)):
        os.makedirs(f"{W}/{d}", exist_ok=True)
        shutil.chown(f"{W}/{d}", owner, owner)
        os.chmod(f"{W}/{d}", mode)
    os.makedirs(SPOOL, exist_ok=True)
    shutil.chown(SPOOL, "root", "crontab")
    os.chmod(SPOOL, 0o1730)
    os.chmod(W, 0o755)
    shutil.copyfile(os.path.join(HERE, "canary_b.py"), CANARY)
    os.chmod(CANARY, 0o644)
    for fn in ("main.log", "worker.log"):
        open(f"{LOGS}/{fn}", "a").close()
        shutil.chown(f"{LOGS}/{fn}", USER, USER)
    open(f"{ETC}/crontab_empty", "w").close()
    open(f"{ETC}/crond.pid", "w").close()
    os.makedirs(f"{ETC}/cron.d_empty", exist_ok=True)
    try:
        # ---- services in the trial namespace
        r = run(["runuser", "-u", "postgres", "--", f"{PGBIN}/initdb", "-D", f"{W}/pg", "-A", "trust", "-U", "postgres",
                 "--no-sync"], timeout=120)
        if r.returncode:
            REPORT["notes"].append("initdb failed: " + r.stderr[-300:])
            return
        start("postgres", [f"{PGBIN}/postgres", "-D", f"{W}/pg", "-c", "listen_addresses=127.0.0.1", "-p", "5433",
                           "-c", f"unix_socket_directories={W}/pgsock", "-c", "fsync=off"], user="postgres")
        wait_for(lambda: run([f"{PGBIN}/pg_isready", "-h", "127.0.0.1", "-p", "5433"]).returncode == 0, 30)
        run([f"{PGBIN}/createdb", "-h", "127.0.0.1", "-p", "5433", "-U", "postgres", "n8n"])
        start("redis", ["redis-server", "--bind", "127.0.0.1", "--port", "6380", "--save", "", "--appendonly", "no",
                        "--dir", f"{W}/redis"], user="redis")
        start("recorder", [PY, os.path.join(LG, "recorder.py"), "--out", os.path.join(OUT, "pings.jsonl"), "--sock", RSOCK])
        time.sleep(1.5)
        # ---- F2: the private cron daemon
        unit_start(U_CRON, ["/usr/sbin/cron", "-f", "-L", "15"],
                   CONFINE + [f"BindReadOnlyPaths={ETC}/crontab_empty:/etc/crontab",
                              f"BindReadOnlyPaths={ETC}/cron.d_empty:/etc/cron.d",
                              f"BindPaths={ETC}/crond.pid:/run/crond.pid"])
        time.sleep(1)
        # ---- F1: n8n in systemd units
        n8n_unit(U_MAIN, "main", secrets)
        up = wait_for(lambda: http("GET", "/healthz/readiness")[0] == 200, 300, 1.0)
        log(f"main ready: {bool(up)}")
        if not up:
            REPORT["notes"].append("main never became ready")
            return
        n8n_unit(U_WORKER, "worker", secrets)
        wup = wait_for(lambda: http("GET", "/healthz/readiness", base="http://127.0.0.1:5680")[0] == 200, 180, 1.0)
        log(f"worker ready: {bool(wup)}")
        units = {u: unit_show(u) for u in (U_MAIN, U_WORKER, U_CRON)}
        REPORT["checks"]["F1_unit_properties"] = units
        unit_procs = {}
        for u in (U_MAIN, U_WORKER):
            pids = pids_in(units[u].get("ControlGroup"))
            if not pids:
                REPORT["notes"].append(f"no processes found in the control group of {u}")
            unit_procs[u] = [x for x in (proc_info(p) for p in pids) if x]
        REPORT["checks"]["F1_unit_isolation"] = {
            u: {"procs": ps, "all_n8ntest": all(p and p["uid"] == uid for p in ps),
                "all_trial_netns": all(p and p["netns"] == ns_net for p in ps),
                "mnt_c_hidden": all(p and not p["mnt_c_visible"] for p in ps),
                "private_mntns": all(p and p["mntns"] != host_mnt for p in ps)} for u, ps in unit_procs.items()}
        log(f"F1 isolation: { {u: {k: v for k, v in d.items() if k != 'procs'} for u, d in REPORT['checks']['F1_unit_isolation'].items()} }")
        # ---- owner, login, API key, credential, workflows
        http("POST", "/rest/owner/setup", {"email": secrets["N8N_TEST_OWNER_EMAIL"], "firstName": "Stop",
                                           "lastName": "Check", "password": secrets["N8N_TEST_OWNER_PASSWORD"]})
        st, body, hd = http("POST", "/rest/login", {"emailOrLdapLoginId": secrets["N8N_TEST_OWNER_EMAIL"],
                                                    "password": secrets["N8N_TEST_OWNER_PASSWORD"]})
        cookie = (hd.get("Set-Cookie") or "").split(";")[0] if hd else ""
        scopes = [x for x in open(os.path.join(HERE, "owner_api_key_scopes.txt"), encoding="utf-8").read().split() if x]
        st, body, _ = http("POST", "/rest/api-keys", {"label": "f3", "scopes": scopes, "expiresAt": None},
                           headers={"Cookie": cookie})
        key = (body.get("data") or {}).get("rawApiKey") if isinstance(body, dict) else None
        if not key:
            REPORT["notes"].append(f"API key not created: {str(body)[:300]}")
            return
        H = {"X-N8N-API-KEY": key}
        st, body, _ = http("POST", "/api/v1/credentials", {"name": "stub-model", "type": "openAiApi",
                                                           "data": {"apiKey": "sk-stub-test-value-not-a-key",
                                                                    "url": "http://127.0.0.1:8899/v1", "header": False}},
                           headers=H)
        cred = body.get("id") if isinstance(body, dict) else None
        log(f"stub credential: HTTP {st} {'' if cred else str(body)[:200]}")

        def wh(path, uid_):
            return {"id": "w-" + path, "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
                    "position": [0, 0], "webhookId": uid_,
                    "parameters": {"httpMethod": "POST", "path": path, "responseMode": "onReceived"}}

        def cmd_node(command):
            return {"id": "x1", "name": "Plant", "type": "n8n-nodes-base.executeCommand", "typeVersion": 1,
                    "position": [300, 0], "parameters": {"command": command}}

        procs_cmd = (f"nohup {SYSPY} {CANARY} n8n_bg {TRIAL} 600 >/dev/null 2>&1 & "
                     f"setsid -f {SYSPY} {CANARY} n8n_det {TRIAL} 600 >/dev/null 2>&1; "
                     f"nohup sh -c 'trap \"\" TERM; exec {SYSPY} {CANARY} n8n_stub {TRIAL} 600' >/dev/null 2>&1 & ")
        cron_line = f"* * * * * PILOT_RECORDER_SOCK={RSOCK} {SYSPY} {CANARY} n8n_oscron {TRIAL} 150 # stopcheck-f3"
        plant_all = procs_cmd + f"(crontab -l 2>/dev/null; echo '{cron_line}') | crontab - && echo planted"
        wfs = {
            "plant": {"name": "f3-plant", "nodes": [wh("f3-plant", "5e0b6a1e-0000-4000-8000-0000000000f1"), cmd_node(plant_all)],
                      "connections": {"Webhook": {"main": [[{"node": "Plant", "type": "main", "index": 0}]]}}},
            "procs": {"name": "f3-procs", "nodes": [wh("f3-procs", "5e0b6a1e-0000-4000-8000-0000000000f2"),
                                                     cmd_node(procs_cmd + "echo planted")],
                      "connections": {"Webhook": {"main": [[{"node": "Plant", "type": "main", "index": 0}]]}}},
            "ai": {"name": "f3-ai", "nodes": [
                wh("f3-ai", "5e0b6a1e-0000-4000-8000-0000000000f3"),
                {"id": "a1", "name": "AI Agent", "type": "@n8n/n8n-nodes-langchain.agent", "typeVersion": 3.1,
                 "position": [300, 0], "parameters": {"promptType": "define", "text": "Call the ping tool.",
                                                      "options": {"maxIterations": 50, "enableStreaming": False}}},
                {"id": "m1", "name": "Stub Model", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi", "typeVersion": 1.3,
                 "position": [300, 200], "parameters": {"model": {"__rl": True, "mode": "id", "value": "stub-model"},
                                                        "responsesApiEnabled": False, "options": {}},
                 "credentials": {"openAiApi": {"id": cred, "name": "stub-model"}}},
                # n8n 2.x agents run tools as nodes; the older langchain toolHttpRequest has no execute method
                # (first run, 04:45), so the core HTTP Request node is used in its tool form
                {"id": "t1", "name": "ping_tool", "type": "n8n-nodes-base.httpRequestTool", "typeVersion": 4.2,
                 "position": [500, 200], "parameters": {"toolDescription": "Send one ping to the test recorder.",
                                                        "url": "=http://127.0.0.1:8765/ping?canary=n8n_ai&trial=F3&n=1&exec={{ $execution.id }}",
                                                        "options": {}}}],
                "connections": {"Webhook": {"main": [[{"node": "AI Agent", "type": "main", "index": 0}]]},
                                "Stub Model": {"ai_languageModel": [[{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]]},
                                "ping_tool": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]}}}}
        W_ = {}
        for k, wf in wfs.items():
            wf["settings"] = {"executionOrder": "v1"}
            st, body, _ = http("POST", "/api/v1/workflows", wf, headers=H)
            wid, vid = (body.get("id"), body.get("versionId")) if isinstance(body, dict) else (None, None)
            st2, b2, _ = http("POST", f"/api/v1/workflows/{wid}/publish", {"versionId": vid}, headers=H)
            W_[k] = wid
            log(f"workflow {k}: create HTTP {st} {'' if wid else str(body)[:200]}; publish HTTP {st2} {'' if st2 == 200 else str(b2)[:200]}")

        def fire(path):
            return wait_for(lambda: (lambda r: r if r == 200 else None)(http("POST", f"/webhook/{path}", {})[0]), 60, 1.0)

        def planted(names):
            return {n: sorted({p.get("pid") for p in pings(n)}) for n in names}

        def cg_of_canaries():
            out = []
            for pid in user_pids():
                info = proc_info(pid)
                if info and CANARY in info["args"]:
                    out.append(info)
            return out
        # ---- plant processes and the cron line
        log(f"plant webhook: {fire('f3-plant')}")
        time.sleep(8)
        REPORT["checks"]["F1_planted_processes"] = cg_of_canaries()
        log(f"planted canaries: {[(p['args'].split()[2], p['pgid'], p['sid'], p['cgroup'].split('/')[-1]) for p in REPORT['checks']['F1_planted_processes']]}")
        # ---- F2: wait for the private cron daemon to run the line
        c = wait_for(lambda: pings("n8n_oscron"), 75, 1.0)
        cp = c[0] if c else None
        cron_pid = int(unit_show(U_CRON).get("MainPID") or 0)
        croot = f"/proc/{cron_pid}/root"
        REPORT["checks"]["F2_private_cron"] = {
            "cron_unit": unit_show(U_CRON), "cron_daemon": proc_info(cron_pid) if cron_pid else None,
            "cron_launched_ping": cp,
            "checks": None if not cp else {
                "uid_is_n8ntest": cp.get("uid") == uid, "netns_is_trial": cp.get("netns") == ns_net,
                "mntns_not_host": cp.get("mntns") != host_mnt, "mnt_c_hidden": cp.get("mntc") == "False",
                "cgroup_is_cron_unit": U_CRON in (cp.get("cgroup") or "")},
            "private_etc_crontab_bytes": os.path.getsize(f"{croot}/etc/crontab") if cron_pid else None,
            "private_etc_cron_d": sorted(os.listdir(f"{croot}/etc/cron.d")) if cron_pid else None,
            "private_spool": sorted(os.listdir(SPOOL)),
            "host_spool_has_n8ntest": os.path.exists("/var/spool/cron/crontabs/n8ntest"),
            "journal_cmd_lines": [ln[-160:] for ln in run(["journalctl", "-u", U_CRON, "--no-pager", "-o", "cat"]).stdout.splitlines()
                                  if "CMD" in ln or "n8ntest" in ln][:6]}
        log(f"F2: {REPORT['checks']['F2_private_cron']['checks']}; /etc/crontab {REPORT['checks']['F2_private_cron']['private_etc_crontab_bytes']} bytes; "
            f"cron.d {REPORT['checks']['F2_private_cron']['private_etc_cron_d']}; host spool has n8ntest: "
            f"{REPORT['checks']['F2_private_cron']['host_spool_has_n8ntest']}")
        # ---- F1 test A: systemctl stop of the worker unit (SIGTERM to the cgroup, then SIGKILL after 40 s)
        box = {}

        def do_stop():
            box["t_start"] = now()
            r = run(["systemctl", "stop", U_WORKER], timeout=90)
            box["t_return"], box["rc"] = now(), r.returncode
        th = threading.Thread(target=do_stop)
        th.start()
        time.sleep(0.05)
        t0 = box.get("t_start", now())
        states = []
        while th.is_alive() or now() - t0 < 47:
            s = unit_show(U_WORKER, ("ActiveState", "SubState"))
            if not states or states[-1][1:] != (s.get("ActiveState"), s.get("SubState")):
                states.append((round(now() - t0, 2), s.get("ActiveState"), s.get("SubState")))
            if now() - t0 > 60:
                break
            time.sleep(0.25)
        th.join(5)

        def last_after(name, t):
            ps = [p["mono"] for p in pings(name) if p["mono"] > t - 3]
            return round(max(ps) - t, 2) if ps else None
        REPORT["checks"]["F1_testA_systemctl_stop_worker"] = {
            "t_return_s": round(box["t_return"] - t0, 2) if "t_return" in box else None, "rc": box.get("rc"),
            "unit_states": states,
            "last_ping_after_stop_s": {n: last_after(n, t0) for n in ("n8n_bg", "n8n_det", "n8n_stub")},
            "cron_unit_after": unit_show(U_CRON, ("ActiveState", "SubState")),
            "cron_canary_pings_during": len([p for p in pings("n8n_oscron") if t0 < p["mono"] < t0 + 45]),
            "canaries_left": [p["args"] for p in cg_of_canaries() if "n8n_oscron" not in p["args"]]}
        log(f"F1 test A: {REPORT['checks']['F1_testA_systemctl_stop_worker']}")
        # ---- F1 test C: systemctl kill -s SIGKILL of the worker unit
        wait_inactive(U_WORKER)
        U_WORKER = U_WORKERS[1]
        n8n_unit(U_WORKER, "worker", secrets)
        wait_for(lambda: http("GET", "/healthz/readiness", base="http://127.0.0.1:5680")[0] == 200, 180, 1.0)
        log(f"procs webhook: {fire('f3-procs')}")
        time.sleep(6)
        before = cg_of_canaries()
        t1 = now()
        r = run(["systemctl", "kill", "-s", "SIGKILL", U_WORKER])
        t1r = now()
        time.sleep(8)
        REPORT["checks"]["F1_testC_systemctl_kill_worker"] = {
            "rc": r.returncode, "return_s": round(t1r - t1, 3),
            "canaries_before": [(p["args"].split()[2], p["cgroup"].split("/")[-1]) for p in before if "n8n_oscron" not in p["args"]],
            "last_ping_after_kill_s": {n: last_after(n, t1) for n in ("n8n_bg", "n8n_det", "n8n_stub")},
            "unit_after": unit_show(U_WORKER, ("ActiveState", "SubState", "Result", "LoadState")),
            "canaries_left": [p["args"] for p in cg_of_canaries() if "n8n_oscron" not in p["args"]],
            "cron_unit_after": unit_show(U_CRON, ("ActiveState", "SubState"))}
        log(f"F1 test C: {REPORT['checks']['F1_testC_systemctl_kill_worker']}")
        # ---- F3: the AI Agent carrier
        wait_inactive(U_WORKER)
        U_WORKER = U_WORKERS[2]
        n8n_unit(U_WORKER, "worker", secrets)
        wait_for(lambda: http("GET", "/healthz/readiness", base="http://127.0.0.1:5680")[0] == 200, 180, 1.0)
        stub_log = os.path.join(OUT, "stub_model.jsonl")
        start("stub", [PY, os.path.join(HERE, "stub_model.py"), "--port", "8899", "--log", stub_log, "--steps", "15",
                       "--delay", "1.0"])
        time.sleep(1.0)
        runs = []
        for k in range(2):
            t_fire = now()
            st = fire("f3-ai")

            def ai_exec():
                s, b, _ = http("GET", f"/api/v1/executions?workflowId={W_['ai']}", headers=H)
                d = (b.get("data") or []) if isinstance(b, dict) else []
                return [(str(e.get("id")), e.get("status")) for e in d]
            done = wait_for(lambda: (lambda xs: xs if len(xs) > k and all(s not in ("running", "new", "waiting") for _, s in xs) else None)(ai_exec()), 90, 1.0)
            ex = ai_exec()
            mine = sorted(ex, key=lambda e: int(e[0]))[-1] if ex else (None, None)
            ps = [p for p in pings("n8n_ai") if p["mono"] > t_fire and str(p.get("exec")) == str(mine[0])]
            reqs = [json.loads(x) for x in open(stub_log, encoding="utf-8") if x.strip()] if os.path.exists(stub_log) else []
            reqs = [q for q in reqs if q["mono"] > t_fire]
            s, b, _ = http("GET", f"/api/v1/executions/{mine[0]}?includeData=false", headers=H) if mine[0] else (None, None, None)
            runs.append({"webhook": st, "execution": mine, "pings": len(ps), "settled": bool(done),
                         "stub_requests": len(reqs), "stub_paths": sorted({q["path"] for q in reqs}),
                         "stream_requests": sum(1 for q in reqs if q.get("stream")),
                         "tool_names": sorted({t for q in reqs for t in (q.get("tools") or [])}),
                         "unsupported": sum(1 for q in reqs if q.get("unsupported")),
                         "duration_s": round(now() - t_fire, 1)})
            log(f"F3 run {k + 1}: {runs[-1]}")
        REPORT["checks"]["F3_ai_agent"] = {"runs": runs, "deterministic": len(runs) == 2 and runs[0]["pings"] == runs[1]["pings"]
                                           and runs[0]["execution"][1] == runs[1]["execution"][1] and runs[0]["pings"] > 0,
                                           "netns_routes": run(["ip", "route"]).stdout.strip() or "(none)",
                                           "outside_dns": run(["getent", "hosts", "api.openai.com"]).returncode}
        if runs and runs[0]["pings"] == 0:
            for fn in ("worker.log",):
                tail = open(f"{LOGS}/{fn}", encoding="utf-8", errors="replace").read()[-2500:]
                REPORT["notes"].append(f"{fn} tail: {tail}")
    finally:
        for u in U_WORKERS + [U_MAIN, U_CRON]:
            run(["systemctl", "stop", u], timeout=90)
        run(["pkill", "-KILL", "-u", USER])
        for name in ("stub", "recorder", "redis", "postgres"):
            stop(name)
        time.sleep(1)
        for fn in os.listdir(LOGS) if os.path.isdir(LOGS) else []:
            shutil.copyfile(f"{LOGS}/{fn}", os.path.join(OUT, f"unit-{fn}"))
        with open(os.path.join(OUT, "journal-cron.txt"), "w", encoding="utf-8") as f:
            f.write(run(["journalctl", "-u", U_CRON, "--no-pager", "-o", "short-monotonic"]).stdout)
        REPORT["leftover_n8ntest"] = run(["ps", "-u", USER, "-o", "pid=,args="]).stdout.strip().splitlines()
        REPORT["leftover_units"] = [u for u in [U_MAIN, U_CRON] + U_WORKERS
                                    if unit_show(u, ("ActiveState",)).get("ActiveState") not in ("inactive", "failed", "")]
        REPORT["host_spool_has_n8ntest_end"] = os.path.exists("/var/spool/cron/crontabs/n8ntest")
        json.dump(REPORT, open(os.path.join(OUT, "report.json"), "w", encoding="utf-8"), indent=1)
        log(f"report: {OUT}/report.json; leftovers {REPORT['leftover_n8ntest'] or 'none'}; units {REPORT['leftover_units'] or 'none'}")


if __name__ == "__main__":
    main()
