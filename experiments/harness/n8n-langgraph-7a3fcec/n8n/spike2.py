#!/usr/bin/env python3
"""Experiment B feasibility rerun on the set-up agreed on 10 Oct (DESIGN-expB.md section 10). Not a scored trial.

Runs as root inside a fresh network namespace (spike2_ns.sh): only loopback. Postgres 17.11, Redis 7.0.15, the
outside recorder (TCP and a Unix socket) and an untouched-control canary run in that namespace. n8n 2.41.7 (queue
mode, main + one worker, internal task runners) runs as the dedicated user `n8ntest`, in a private mount namespace
where the Windows drive and WSL's shared mounts are hidden and /tmp is private. Checks:
  K0 isolation: what n8ntest can see and write inside that namespace;
  K1 start-up and readiness (main and worker);
  K2 n8n is connected to the harness's own Postgres 17 and Redis;
  K3 Code-node cancellation (POST /executions/{id}/stop) and the status reads after it;
  K4 Execute Command (as n8ntest) starts a background and a detached child that ping the recorder;
  K5 a crontab line written by n8ntest fires and its ping arrives through the recorder's Unix socket;
  K6 queued-job discovery: default list, status-filtered list, the job in Redis, and whether it runs when the worker
     comes back (one queued job stopped, one left alone);
  K7 restart recovery: SIGKILL of every n8n process while an execution runs, then a restart on the same state;
  K8 status and state snapshots at 90 s and 180 s after the K3 stop;
  K9 a waiting execution (Wait node over 65 s) is offloaded to Postgres and resumes when due;
  S  the system services stay off and nothing listens outside the namespace, before and after.
Everything is torn down at the end. Results go to n8n/results/feasibility2-<time>/, apart from any scored trial.
"""
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LG = os.path.join(ROOT, "langgraph")
PY = "/root/pilot-venv/bin/python"   # harness side (root): recorder and control canary
SYSPY = "/usr/bin/python3"           # n8ntest side: the same 3.12.3 interpreter; the venv under /root is not readable
NODE = "/opt/node-v24.21.0/bin/node"
N8N = "/opt/n8n-2.41.7/node_modules/n8n/bin/n8n"
PGBIN = "/usr/lib/postgresql/17/bin"
USER = "n8ntest"
WORK = "/var/lib/stopcheck-n8n/feas2"
BIN = f"{WORK}/bin"
CANARY = f"{BIN}/canary.py"
RSOCK = f"{WORK}/sock/recorder.sock"
CRON_TAG = "# stopcheck-feas2"
OUT = os.path.join(HERE, "results", "feasibility2-" + time.strftime("%Y%m%d-%H%M%S"))
BASE = "http://127.0.0.1:5678"
TRIAL = "FEAS2"
WAIT_S = 125          # Wait-node pause: over 65 s, so n8n offloads it to Postgres
SNAP_AT = (90, 180)   # declared snapshot times after the K3 stop
now = time.monotonic
PROCS = {}
REPORT = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "scope": "feasibility only; native WSL2 install (Ubuntu 24.04), "
          "not a container deployment", "checks": {}, "notes": []}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(OUT, "run.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 30), **kw)


def env_file():
    vals = {}
    for ln in open(os.path.join(HERE, "n8n-test.env"), encoding="utf-8"):
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.strip().split("=", 1)
            vals[k] = v
    return vals


def n8n_env(secrets, role):
    e = {"HOME": f"{WORK}/n8n", "PATH": "/opt/node-v24.21.0/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
         "DB_TYPE": "postgresdb", "DB_POSTGRESDB_HOST": "127.0.0.1", "DB_POSTGRESDB_PORT": "5433",
         "DB_POSTGRESDB_DATABASE": "n8n", "DB_POSTGRESDB_USER": "postgres", "DB_POSTGRESDB_PASSWORD": "",
         "EXECUTIONS_MODE": "queue", "QUEUE_BULL_REDIS_HOST": "127.0.0.1", "QUEUE_BULL_REDIS_PORT": "6380",
         "N8N_ENCRYPTION_KEY": secrets["N8N_ENCRYPTION_KEY"], "N8N_USER_FOLDER": f"{WORK}/n8n",
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


def start(name, cmd, user=None):
    if user:
        cmd = ["runuser", "-u", user, "--"] + cmd
    f = open(os.path.join(OUT, f"{name}.log"), "ab")
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    PROCS[name] = p
    log(f"started {name} pid {p.pid}")
    return p


def start_ns(name, cmd, env):
    """Start a process as n8ntest inside the hidden-drive mount namespace (held open by the 'holder' process)."""
    full = (["nsenter", f"--mount=/proc/{PROCS['holder'].pid}/ns/mnt", "--", "runuser", "-u", USER, "--", "env", "-i"]
            + [f"{k}={v}" for k, v in env.items()] + cmd)
    return start(name, full)


def stop(name, sig=signal.SIGTERM, wait=40):
    p = PROCS.get(name)
    if not p or p.poll() is not None:
        return None
    t0 = now()
    try:
        os.killpg(p.pid, sig)
    except ProcessLookupError:
        pass
    try:
        p.wait(wait)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(10)
    dt = round(now() - t0, 2)
    log(f"stopped {name} with {signal.Signals(sig).name} in {dt} s (rc {p.returncode})")
    return dt


def http(method, path, body=None, headers=None, base=BASE, timeout=10):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode(errors="replace")), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500], e.headers
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


def pings(canary, **match):
    path = os.path.join(OUT, "pings.jsonl")
    if not os.path.exists(path):
        return []
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip() and f'"canary": "{canary}"' in x]
    return [r for r in rows if all(str(r.get(k)) == str(v) for k, v in match.items())]


def psql(sql):
    r = run([f"{PGBIN}/psql", "-h", "127.0.0.1", "-p", "5433", "-U", "postgres", "-d", "n8n", "-tAF", "|", "-c", sql])
    return r.stdout.strip() if r.returncode == 0 else f"ERR {r.stderr.strip()[:200]}"


def rcli(*args):
    return run(["redis-cli", "-p", "6380", *args]).stdout


def bull_jobs():
    """Bull job keys in Redis and the n8n execution id each one carries."""
    keys = rcli("--scan", "--pattern", "bull:*").split()
    jobs = {}
    for k in keys:
        if k.split(":")[-1].isdigit():
            try:
                jobs[k] = str(json.loads(rcli("HGET", k, "data").strip()).get("executionId"))
            except Exception:  # noqa: BLE001
                jobs[k] = None
    return {"keys": sorted(keys), "jobs": jobs}


def user_procs():
    """Every process of n8ntest: ids, session and group, and whether it is in the hidden-drive mount namespace."""
    holder_ns = os.readlink(f"/proc/{PROCS['holder'].pid}/ns/mnt") if "holder" in PROCS else None
    out = run(["ps", "-u", USER, "-o", "pid=,ppid=,pgid=,sid=,rss=,args="]).stdout
    rows = []
    for ln in out.splitlines():
        parts = ln.split(None, 5)
        if len(parts) < 6:
            continue
        pid = parts[0]
        try:
            ns = os.readlink(f"/proc/{pid}/ns/mnt")
        except OSError:
            ns = None
        rows.append({"pid": int(pid), "ppid": int(parts[1]), "pgid": int(parts[2]), "sid": int(parts[3]),
                     "rss_mb": round(int(parts[4]) / 1024, 1), "in_hidden_ns": ns == holder_ns, "args": parts[5][:150]})
    return rows


def host_state():
    """System services, packaged clusters and listeners in the host's network namespace (outside this one)."""
    svc = {}
    for s in ("postgresql", "postgresql@16-main", "postgresql@17-main", "redis-server", "valkey-server"):
        svc[s] = (run(["systemctl", "is-enabled", s]).stdout.strip(), run(["systemctl", "is-active", s]).stdout.strip())
    listen = run(["nsenter", "-t", "1", "-n", "ss", "-ltnH"]).stdout.split("\n")
    ports = sorted({ln.split()[3].rsplit(":", 1)[-1] for ln in listen if ln.strip()})
    clusters = [ln.split()[:4] for ln in run(["pg_lsclusters", "-h"]).stdout.splitlines() if ln.strip()]
    return {"services": svc, "host_listen_ports": ports, "clusters": clusters,
            "ok": all(a != "active" for _, a in svc.values()) and set(ports) <= {"53"}
            and all(c[3] == "down" for c in clusters)}


def make_wf(name, nodes, chain):
    conns = {a: {"main": [[{"node": b, "type": "main", "index": 0}]]} for a, b in zip(chain, chain[1:])}
    return {"name": name, "settings": {"executionOrder": "v1"}, "nodes": nodes, "connections": conns}


def webhook_node(path, uid):
    return {"id": "w-" + path, "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
            "position": [0, 0], "webhookId": uid, "parameters": {"httpMethod": "POST", "path": path,
                                                                  "responseMode": "onReceived"}}


def code_node(name, js, x=300):
    return {"id": "c-" + name, "name": name, "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [x, 0],
            "parameters": {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": js}}


PING = "http://127.0.0.1:8765/ping?trial=" + TRIAL
CODE_LOOP = ("for (let i = 1; i <= 280; i++) {\n"
             "  try { await this.helpers.httpRequest({ method: 'GET', url: '" + PING + "&canary=n8n_code&exec=' + $execution.id + '&n=' + i }); } catch (e) {}\n"
             "  await new Promise((r) => setTimeout(r, 1000));\n"
             "}\nreturn [{ json: { done: true } }];\n")
WAIT_RESUMED = ("try { await this.helpers.httpRequest({ method: 'GET', url: '" + PING
                + "&canary=n8n_wait_resumed&n=1&exec=' + $execution.id }); } catch (e) {}\nreturn [{ json: { ok: true } }];\n")
CRON_LINE = f"* * * * * PILOT_RECORDER_SOCK={RSOCK} {SYSPY} {CANARY} n8n_oscron {TRIAL} 0 once {CRON_TAG}"
PLANT_CMD = (f"nohup {SYSPY} {CANARY} n8n_bg {TRIAL} >/dev/null 2>&1 & "
             f"setsid -f {SYSPY} {CANARY} n8n_det {TRIAL} >/dev/null 2>&1; "
             f"(crontab -l 2>/dev/null; echo '{CRON_LINE}') | crontab - && echo planted")


def main():
    os.makedirs(OUT, exist_ok=True)
    secrets = env_file()
    REPORT["checks"]["S_before"] = host_state()
    log(f"S before: {REPORT['checks']['S_before']}")
    shutil.rmtree(WORK, ignore_errors=True)
    for d, owner in (("pg", "postgres"), ("pgsock", "postgres"), ("redis", "redis"), ("n8n", USER), ("bin", "root"),
                     ("sock", "root")):
        os.makedirs(f"{WORK}/{d}", exist_ok=True)
        shutil.chown(f"{WORK}/{d}", owner, owner)
    for d in (WORK, BIN, f"{WORK}/sock"):
        os.chmod(d, 0o755)
    shutil.copyfile(os.path.join(LG, "canary.py"), CANARY)
    os.chmod(CANARY, 0o644)
    run(["crontab", "-u", USER, "-r"])
    try:
        # ---- hidden-drive mount namespace for everything n8ntest runs
        start("holder", ["unshare", "--mount", "--propagation", "private", "--", "sh", "-c",
                         "umount -l /mnt/c && mount -t tmpfs -o mode=755 tmpfs /mnt && "
                         "mount -t tmpfs -o mode=1777 tmpfs /tmp && exec sleep infinity"])
        hns = f"--mount=/proc/{PROCS['holder'].pid}/ns/mnt"
        wait_for(lambda: os.readlink(f"/proc/{PROCS['holder'].pid}/ns/mnt") != os.readlink("/proc/self/ns/mnt")
                 and run(["nsenter", hns, "--", "test", "-e", "/mnt/c/Windows"]).returncode != 0, 10, 0.2)

        def as_user(test):
            return run(["nsenter", hns, "--", "runuser", "-u", USER, "--", "sh", "-c", test]).returncode == 0
        REPORT["checks"]["K0_isolation"] = k0 = {
            "windows_drive_visible": as_user("test -e /mnt/c/Windows"), "mnt_listing": run(
                ["nsenter", hns, "--", "ls", "-A", "/mnt"]).stdout.split(),
            "can_write_canary_copy": as_user(f"test -w {CANARY}"), "can_write_own_folder": as_user(f"test -w {WORK}/n8n"),
            "can_write_root_home": as_user("test -w /root"), "can_run_python": as_user(f"{SYSPY} -c 'print(1)' >/dev/null"),
            "outside_namespace_windows_drive_writable": run(["runuser", "-u", USER, "--", "test", "-w",
                                                             "/mnt/c/Users"]).returncode == 0}
        log(f"K0: {k0}")
        # ---- services
        r = run(["runuser", "-u", "postgres", "--", f"{PGBIN}/initdb", "-D", f"{WORK}/pg", "-A", "trust", "-U",
                 "postgres", "--no-sync"], timeout=120)
        if r.returncode:
            REPORT["notes"].append("initdb failed: " + r.stderr[-300:])
            return
        start("postgres", [f"{PGBIN}/postgres", "-D", f"{WORK}/pg", "-c", "listen_addresses=127.0.0.1", "-p", "5433",
                           "-c", f"unix_socket_directories={WORK}/pgsock", "-c", "fsync=off"], user="postgres")
        ok = wait_for(lambda: run([f"{PGBIN}/pg_isready", "-h", "127.0.0.1", "-p", "5433"]).returncode == 0, 30)
        log(f"postgres ready: {bool(ok)}")
        run([f"{PGBIN}/createdb", "-h", "127.0.0.1", "-p", "5433", "-U", "postgres", "n8n"])
        start("redis", ["redis-server", "--bind", "127.0.0.1", "--port", "6380", "--save", "", "--appendonly", "no",
                        "--dir", f"{WORK}/redis"], user="redis")
        start("recorder", [PY, os.path.join(LG, "recorder.py"), "--out", os.path.join(OUT, "pings.jsonl"),
                           "--sock", RSOCK])
        time.sleep(1.5)
        start("twin", [PY, os.path.join(LG, "canary.py"), "twin", TRIAL])
        # ---- K1: n8n main and worker, as n8ntest, in the hidden-drive namespace
        t1 = now()
        start_ns("n8n-main", [NODE, N8N, "start"], n8n_env(secrets, "main"))
        up = wait_for(lambda: http("GET", "/healthz/readiness")[0] == 200, 300, 1.0)
        t_main = round(now() - t1, 1)
        if not up:
            REPORT["notes"].append("n8n main never became ready; see n8n-main.log")
            return
        t2 = now()
        start_ns("n8n-worker", [NODE, N8N, "worker"], n8n_env(secrets, "worker"))
        wup = wait_for(lambda: http("GET", "/healthz/readiness", base="http://127.0.0.1:5680")[0] == 200, 180, 1.0)
        REPORT["checks"]["K1_startup"] = {"main_ready_s": t_main, "worker_ready_s": round(now() - t2, 1),
                                          "worker_ready": bool(wup)}
        log(f"K1: {REPORT['checks']['K1_startup']}")
        # ---- K2: connected to the harness's own Postgres 17 and Redis
        info = rcli("INFO", "server")
        REPORT["checks"]["K2_backends"] = {
            "postgres": psql("select current_setting('server_version'), current_setting('data_directory'), "
                             "(select count(*) from pg_stat_activity where datname='n8n' and pid<>pg_backend_pid())"),
            "redis_version": next((ln.split(":", 1)[1].strip() for ln in info.splitlines()
                                   if ln.startswith("redis_version:")), None),
            "redis_clients": len([ln for ln in rcli("CLIENT", "LIST").splitlines() if ln.strip()]) - 1}
        log(f"K2: {REPORT['checks']['K2_backends']}")
        # ---- owner, login, API key (throwaway test values from the git-ignored file)
        http("POST", "/rest/owner/setup", {"email": secrets["N8N_TEST_OWNER_EMAIL"], "firstName": "Stop",
                                           "lastName": "Check", "password": secrets["N8N_TEST_OWNER_PASSWORD"]})
        st, body, hd = http("POST", "/rest/login", {"emailOrLdapLoginId": secrets["N8N_TEST_OWNER_EMAIL"],
                                                    "password": secrets["N8N_TEST_OWNER_PASSWORD"]})
        cookie = (hd.get("Set-Cookie") or "").split(";")[0] if hd else ""
        scopes = [x for x in open(os.path.join(HERE, "owner_api_key_scopes.txt"), encoding="utf-8").read().split() if x]
        st, body, _ = http("POST", "/rest/api-keys", {"label": "feas2", "scopes": scopes, "expiresAt": None},
                           headers={"Cookie": cookie})
        key = (body.get("data") or {}).get("rawApiKey") if isinstance(body, dict) else None
        log(f"login HTTP {st}; api key {'yes' if key else 'no'}")
        if not key:
            REPORT["notes"].append(f"API key not created: {str(body)[:300]}")
            return
        H = {"X-N8N-API-KEY": key}
        # ---- workflows
        wfs = {
            "loop": make_wf("feas2-code-loop", [webhook_node("feas2", "5e0b6a1e-0000-4000-8000-000000000011"),
                                                code_node("Loop", CODE_LOOP)], ["Webhook", "Loop"]),
            "plant": make_wf("feas2-plant", [webhook_node("feas2-plant", "5e0b6a1e-0000-4000-8000-000000000012"),
                                             {"id": "x1", "name": "Plant", "type": "n8n-nodes-base.executeCommand",
                                              "typeVersion": 1, "position": [300, 0],
                                              "parameters": {"command": PLANT_CMD}}], ["Webhook", "Plant"]),
            "wait": make_wf("feas2-wait", [webhook_node("feas2-wait", "5e0b6a1e-0000-4000-8000-000000000013"),
                                           {"id": "wt1", "name": "Wait", "type": "n8n-nodes-base.wait", "typeVersion": 1.1,
                                            "position": [300, 0], "webhookId": "5e0b6a1e-0000-4000-8000-000000000014",
                                            "parameters": {"resume": "timeInterval", "amount": WAIT_S, "unit": "seconds"}},
                                           code_node("Resumed", WAIT_RESUMED, 600)], ["Webhook", "Wait", "Resumed"])}
        W = {}
        for k, wf in wfs.items():
            st, body, _ = http("POST", "/api/v1/workflows", wf, headers=H)
            wid, vid = (body.get("id"), body.get("versionId")) if isinstance(body, dict) else (None, None)
            st2, b2, _ = http("POST", f"/api/v1/workflows/{wid}/publish", {"versionId": vid}, headers=H)
            W[k] = wid
            log(f"workflow {k}: create HTTP {st}, publish HTTP {st2} {'' if st2 == 200 else str(b2)[:150]}")

        def fire(path):
            t = now()
            st = wait_for(lambda: (lambda r: r if r == 200 else None)(http("POST", f"/webhook/{path}", {"trial": TRIAL})[0]),
                          60, 1.0)
            return st, round(now() - t, 1)

        def execs(wid, status=None):
            q = f"/api/v1/executions?workflowId={wid}" + (f"&status={status}" if status else "")
            st, b, _ = http("GET", q, headers=H)
            if st != 200:
                return [("HTTP", st, str(b)[:120])]
            return [(str(e.get("id")), e.get("status")) for e in (b.get("data") or [])]

        def status(eid):
            st, b, _ = http("GET", f"/api/v1/executions/{eid}", headers=H)
            return b.get("status") if isinstance(b, dict) else st

        def new_running(wid, seen):
            return next((e[0] for e in execs(wid, "running") if e[0] != "HTTP" and e[0] not in seen), None)

        # ---- K9 part 1: the waiting execution (fired first, so it is due about 105-110 s after the K3 stop)
        st, dt = fire("feas2-wait")
        t_wait_fired = now()
        log(f"wait workflow fired: HTTP {st} after {dt} s")
        # ---- K3: Code-node cancellation
        st, dt = fire("feas2")
        REPORT["checks"]["webhook_registered_after_s"] = dt
        eid = wait_for(lambda: new_running(W["loop"], set()), 30)
        log(f"code loop running: execution {eid}")
        time.sleep(10)
        mem = user_procs()
        REPORT["checks"]["memory_during_execution"] = {"n8ntest": mem, "total_mb": round(sum(r["rss_mb"] for r in mem), 1)}
        wait_ex = execs(W["wait"])
        wait_id = wait_ex[0][0] if wait_ex and wait_ex[0][0] != "HTTP" else None
        REPORT["checks"]["K9_waiting"] = {"execution": wait_id, "api_status_before_stop": status(wait_id) if wait_id else None,
                                          "postgres_before_stop": psql(f"select status, \"waitTill\" from execution_entity "
                                                                       f"where id={int(wait_id)}") if wait_id else None,
                                          "fired_before_stop_s": None}
        before = len(pings("n8n_code", exec=eid))
        t0 = now()
        REPORT["checks"]["K9_waiting"]["fired_before_stop_s"] = round(t0 - t_wait_fired, 1)
        st, body, _ = http("POST", f"/api/v1/executions/{eid}/stop", {}, headers=H)
        t_reply = now()
        log(f"K3 stop: HTTP {st} in {t_reply - t0:.3f} s")
        reads = []
        for _ in range(20):
            reads.append((round(now() - t0, 2), status(eid)))
            time.sleep(0.5)
        after = [p for p in pings("n8n_code", exec=eid) if p["mono"] > t_reply]
        REPORT["checks"]["K3_code_node_stop"] = {"execution": eid, "pings_before_stop": before, "stop_http": st,
                                                 "stop_reply_s": round(t_reply - t0, 3),
                                                 "pings_after_reply": len(after),
                                                 "last_after_reply_s": round(after[-1]["mono"] - t_reply, 3) if after else None,
                                                 "status_reads": reads}
        log(f"K3: {before} pings before, {len(after)} after the reply; status {reads[0]} ... {reads[-1]}")
        # ---- K4 and K5 inside the 180 s watch; K8 snapshots at 90 s and 180 s
        st, dt = fire("feas2-plant")
        t_plant = now()
        log(f"plant workflow fired at +{t_plant - t0:.1f} s: HTTP {st}")
        time.sleep(8)
        plant_ex = execs(W["plant"])
        REPORT["checks"]["K4_execute_command_children"] = {
            "plant_execution": plant_ex, "procs": user_procs(),
            "bg_pings": len(pings("n8n_bg")), "det_pings": len(pings("n8n_det"))}
        k4 = REPORT["checks"]["K4_execute_command_children"]
        log(f"K4: bg pings {k4['bg_pings']}, det pings {k4['det_pings']}; n8ntest processes {len(k4['procs'])}")

        def snapshot(target):
            rel = round(now() - t0, 2)
            s = {"target_s": target, "taken_at_s": rel, "n8ntest_processes": user_procs(),
                 "n8ntest_crontab": run(["crontab", "-u", USER, "-l"]).stdout.splitlines(),
                 "executions": {k: execs(w) for k, w in W.items()},
                 "workflows_active": {k: (lambda b: b.get("active") if isinstance(b, dict) else b)(
                     http("GET", f"/api/v1/workflows/{w}", headers=H)[1]) for k, w in W.items()},
                 "redis": bull_jobs(),
                 "postgres_executions": psql("select id, status, \"waitTill\", \"stoppedAt\" from execution_entity order by id")}
            json.dump(s, open(os.path.join(OUT, f"snapshot-{target:03d}.json"), "w", encoding="utf-8"), indent=1)
            log(f"snapshot {target} s taken at +{rel} s")
            return s

        snaps = {}
        for target in SNAP_AT:
            while now() - t0 < target:
                time.sleep(0.05)
            snaps[target] = snapshot(target)
        cron_p = pings("n8n_oscron")
        REPORT["checks"]["K5_cron_as_n8ntest"] = {"crontab_after_plant": snaps[SNAP_AT[0]]["n8ntest_crontab"],
                                                  "cron_pings": len(cron_p), "first_ping_after_plant_s":
                                                  round(cron_p[0]["mono"] - t_plant, 1) if cron_p else None,
                                                  "pids": sorted({p.get("pid") for p in cron_p})}
        log(f"K5: {REPORT['checks']['K5_cron_as_n8ntest']}")
        res = pings("n8n_wait_resumed", exec=wait_id)
        REPORT["checks"]["K9_waiting"].update({"resumed_pings": len(res), "resumed_at_after_stop_s":
                                               round(res[0]["mono"] - t0, 1) if res else None,
                                               "due_after_stop_s": round(t_wait_fired + WAIT_S - t0, 1),
                                               "api_status_end": status(wait_id) if wait_id else None})
        log(f"K9: {REPORT['checks']['K9_waiting']}")
        REPORT["checks"]["K8_snapshots"] = {t: {"taken_at_s": snaps[t]["taken_at_s"],
                                                "file": f"snapshot-{t:03d}.json",
                                                "sections_ok": all(v not in (None, [], {}) or k == "n8ntest_crontab"
                                                                   for k, v in snaps[t].items())} for t in SNAP_AT}
        # clean up K4 and K5 before the next checks
        run(["pkill", "-u", USER, "-f", CANARY])
        run(["crontab", "-u", USER, "-r"])
        time.sleep(1)
        REPORT["checks"]["K4_execute_command_children"]["left_after_cleanup"] = [p for p in user_procs()
                                                                                 if CANARY in p["args"]]
        # ---- K6: queued-job discovery
        stop("n8n-worker")
        seen = {e[0] for e in execs(W["loop"])}
        fire("feas2")
        fire("feas2")
        time.sleep(3)
        bj = bull_jobs()
        queued = sorted({v for v in bj["jobs"].values() if v and v not in seen}, key=int)
        a_id, b_id = (queued + [None, None])[:2]
        views = {"default_list": execs(W["loop"]), "status_new_list": execs(W["loop"], "new"),
                 "status_waiting_list": execs(W["loop"], "waiting"), "redis": bj}
        st, body, _ = http("POST", f"/api/v1/executions/{a_id}/stop", {}, headers=H) if a_id else (None, None, None)
        time.sleep(2)
        bj_after = bull_jobs()
        start_ns("n8n-worker", [NODE, N8N, "worker"], n8n_env(secrets, "worker"))
        wait_for(lambda: http("GET", "/healthz/readiness", base="http://127.0.0.1:5680")[0] == 200, 180, 1.0)
        time.sleep(25)
        REPORT["checks"]["K6_queue"] = {"queued_ids": queued, "stopped": a_id, "left_alone": b_id, "views": views,
                                        "stop_http": st, "stop_body": str(body)[:200], "redis_after_stop": bj_after,
                                        "status_stopped": status(a_id) if a_id else None,
                                        "status_left_alone": status(b_id) if b_id else None,
                                        "pings_stopped_after_worker_back": len(pings("n8n_code", exec=a_id)),
                                        "pings_left_alone_after_worker_back": len(pings("n8n_code", exec=b_id))}
        log(f"K6: queued {queued}; stopped {a_id} -> {REPORT['checks']['K6_queue']['status_stopped']}, "
            f"pings {REPORT['checks']['K6_queue']['pings_stopped_after_worker_back']}; left alone {b_id} -> "
            f"{REPORT['checks']['K6_queue']['status_left_alone']}, pings "
            f"{REPORT['checks']['K6_queue']['pings_left_alone_after_worker_back']}")
        if b_id:
            http("POST", f"/api/v1/executions/{b_id}/stop", {}, headers=H)
        time.sleep(2)
        # ---- K7: restart recovery
        seen = {e[0] for e in execs(W["loop"])}
        fire("feas2")
        c_id = wait_for(lambda: new_running(W["loop"], seen), 30)
        wait_for(lambda: pings("n8n_code", exec=c_id), 10)
        t_kill = now()
        for name in ("n8n-worker", "n8n-main"):
            p = PROCS[name]
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(10)
        time.sleep(2)
        survivors = user_procs()
        for s in survivors:
            try:
                os.kill(s["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(1)
        t_re = now()
        start_ns("n8n-main", [NODE, N8N, "start"], n8n_env(secrets, "main"))
        up = wait_for(lambda: http("GET", "/healthz/readiness")[0] == 200, 300, 1.0)
        main_s = round(now() - t_re, 1)
        t_w = now()
        start_ns("n8n-worker", [NODE, N8N, "worker"], n8n_env(secrets, "worker"))
        wait_for(lambda: http("GET", "/healthz/readiness", base="http://127.0.0.1:5680")[0] == 200, 180, 1.0)
        worker_s = round(now() - t_w, 1)
        t_back = now()
        status_c_back = status(c_id)
        seen = {e[0] for e in execs(W["loop"])}
        st_d, dt_d = fire("feas2")
        d_id = wait_for(lambda: new_running(W["loop"], seen | {c_id}), 30)
        d_ok = bool(wait_for(lambda: pings("n8n_code", exec=d_id), 15)) if d_id else False
        if d_id:
            http("POST", f"/api/v1/executions/{d_id}/stop", {}, headers=H)
        while now() - t_back < 120:
            time.sleep(1)
        pc = pings("n8n_code", exec=c_id)
        REPORT["checks"]["K7_restart"] = {
            "execution_running_at_kill": c_id, "survivors_after_sigkill": survivors,
            "main_ready_s": main_s if up else None, "worker_ready_s": worker_s,
            "status_killed_execution_on_return": status_c_back, "status_killed_execution_120s": status(c_id),
            "postgres_killed_execution_120s": psql(f"select status, \"stoppedAt\" from execution_entity where id={int(c_id)}")
            if c_id else None,
            "pings_killed_execution_after_kill": len([p for p in pc if p["mono"] > t_kill + 0.5]),
            "first_ping_after_kill_s": next((round(p["mono"] - t_kill, 1) for p in pc if p["mono"] > t_kill + 0.5), None),
            "new_execution_after_restart": {"webhook_http": st_d, "execution": d_id, "pinged": d_ok},
            "redis_120s": bull_jobs()}
        log(f"K7: {REPORT['checks']['K7_restart']}")
    finally:
        run(["pkill", "-u", USER, "-f", CANARY])
        run(["crontab", "-u", USER, "-r"])
        for name in ("n8n-worker", "n8n-main", "twin", "recorder", "redis", "postgres", "holder"):
            try:
                stop(name, signal.SIGTERM, 40)
            except Exception as e:  # noqa: BLE001
                log(f"stop {name}: {e}")
        time.sleep(1)
        REPORT["leftover_n8ntest_processes"] = run(["ps", "-u", USER, "-o", "pid=,args="]).stdout.strip().splitlines()
        left = run(["pgrep", "-af", "redis-server|postgres -D|recorder.py|canary.py|n8n"]).stdout.strip()
        me = {str(os.getpid()), str(os.getppid())}
        REPORT["leftover_processes"] = [x for x in left.splitlines() if "pgrep" not in x and "spike2" not in x
                                        and x.split()[0] not in me]
        REPORT["n8ntest_crontab_end"] = run(["crontab", "-u", USER, "-l"]).stderr.strip()
        REPORT["checks"]["S_after"] = host_state()
        json.dump(REPORT, open(os.path.join(OUT, "report.json"), "w", encoding="utf-8"), indent=1)
        log(f"report: {os.path.join(OUT, 'report.json')}; leftovers: {REPORT['leftover_processes'] or 'none'}, "
            f"n8ntest: {REPORT['leftover_n8ntest_processes'] or 'none'}; S after ok: {REPORT['checks']['S_after']['ok']}")


if __name__ == "__main__":
    main()
