#!/usr/bin/env python3
"""Experiment B feasibility spike (DESIGN-expB.md section 8). Not a scored trial.

Runs as root inside a fresh network namespace (spike_ns.sh): only loopback, so nothing can reach the internet.
Starts Postgres, Valkey, the outside recorder and an untouched-control canary, then n8n 2.41.7 in queue mode
(main + one worker, internal task runners) as the unprivileged user `nobody`. Then it checks:
  C1 memory: every process's RSS while an execution runs;
  C2 control: a Code node in the task runner pings the recorder each second; POST /executions/{id}/stop; does it stop?
  C3 independence: the untouched control keeps pinging while the worker, Valkey and main are stopped;
  C4 queue: with the worker down, a webhook execution waits in Valkey; stopping it removes the job.
Everything is torn down at the end. Results and logs go to n8n/results/spike-<time>/.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LG = os.path.join(ROOT, "langgraph")
PY = "/root/pilot-venv/bin/python"
NODE = "/opt/node-v24.21.0/bin/node"
N8N = "/opt/n8n-2.41.7/node_modules/n8n/bin/n8n"
PGBIN = "/usr/lib/postgresql/16/bin"
WORK = "/var/lib/stopcheck-n8n/spike"
OUT = os.path.join(HERE, "results", "spike-" + time.strftime("%Y%m%d-%H%M%S"))
BASE = "http://127.0.0.1:5678"
TRIAL = "SPIKE"
now = time.monotonic
PROCS = {}
REPORT = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "checks": {}, "notes": []}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(OUT, "spike.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


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
    # each n8n process runs its own internal task broker (default port 5679); they share the namespace, so each
    # gets its own port, and the worker's health server another
    e["N8N_RUNNERS_BROKER_PORT"] = "5681" if role == "main" else "5682"
    if role == "worker":
        e.update({"QUEUE_HEALTH_CHECK_ACTIVE": "true", "QUEUE_HEALTH_CHECK_PORT": "5680"})
    return e


def start(name, cmd, user=None, env=None, cwd=None):
    if user:
        cmd = ["runuser", "-u", user, "--"] + (["env", "-i"] + [f"{k}={v}" for k, v in (env or {}).items()]
                                                if env is not None else []) + cmd
    f = open(os.path.join(OUT, f"{name}.log"), "ab")
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=cwd, start_new_session=True,
                         env=None if user else env)
    PROCS[name] = p
    log(f"started {name} pid {p.pid}")
    return p


def stop(name, sig=signal.SIGTERM, wait=40):
    p = PROCS.get(name)
    if not p or p.poll() is not None:
        return None
    t0 = now()
    os.killpg(p.pid, sig)
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


def pings(canary):
    path = os.path.join(OUT, "pings.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip() and f'"canary": "{canary}"' in x]


def rss_table():
    out = subprocess.run(["ps", "-eo", "pid,ppid,user,rss,args", "--no-headers"], capture_output=True, text=True).stdout
    rows = []
    for ln in out.splitlines():
        parts = ln.split(None, 4)
        if len(parts) == 5 and any(k in parts[4] for k in ("n8n", "postgres", "valkey", "recorder.py", "canary.py",
                                                           "task-runner", "node ")):
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "user": parts[2], "rss_mb": round(int(parts[3]) / 1024, 1),
                         "args": parts[4][:140]})
    return rows


def main():
    os.makedirs(OUT, exist_ok=True)
    secrets = env_file()
    shutil.rmtree(WORK, ignore_errors=True)
    for d, owner in (("pg", "postgres"), ("pgsock", "postgres"), ("valkey", "valkey"), ("n8n", "nobody")):
        os.makedirs(f"{WORK}/{d}", exist_ok=True)
        shutil.chown(f"{WORK}/{d}", owner, owner if owner != "nobody" else "nogroup")
    os.chmod(WORK, 0o755)
    try:
        # ---- services
        subprocess.run(["runuser", "-u", "postgres", "--", f"{PGBIN}/initdb", "-D", f"{WORK}/pg", "-A", "trust",
                        "-U", "postgres", "--no-sync"], check=True, capture_output=True)
        start("postgres", [f"{PGBIN}/postgres", "-D", f"{WORK}/pg", "-c", "listen_addresses=127.0.0.1", "-p", "5433",
                           "-c", f"unix_socket_directories={WORK}/pgsock", "-c", "fsync=off"], user="postgres")
        ok = wait_for(lambda: subprocess.run([f"{PGBIN}/pg_isready", "-h", "127.0.0.1", "-p", "5433"],
                                             capture_output=True).returncode == 0, 30)
        log(f"postgres ready: {bool(ok)}")
        subprocess.run(["runuser", "-u", "postgres", "--", f"{PGBIN}/createdb", "-h", "127.0.0.1", "-p", "5433", "n8n"],
                       check=True)
        start("valkey", ["valkey-server", "--bind", "127.0.0.1", "--port", "6380", "--save", "", "--appendonly", "no",
                         "--dir", f"{WORK}/valkey"], user="valkey")
        start("recorder", [PY, os.path.join(LG, "recorder.py"), "--out", os.path.join(OUT, "pings.jsonl")])
        time.sleep(1.5)
        start("twin", [PY, os.path.join(LG, "canary.py"), "twin", TRIAL])
        # ---- n8n main and worker, as nobody
        start("n8n-main", [NODE, N8N, "start"], user="nobody", env=n8n_env(secrets, "main"), cwd=f"{WORK}/n8n")
        up = wait_for(lambda: http("GET", "/healthz/readiness")[0] == 200, 300, 1.0)  # migrated and fully ready
        log(f"n8n main healthy: {bool(up)}")
        if not up:
            REPORT["notes"].append("n8n main never became healthy; see n8n-main.log")
            return
        start("n8n-worker", [NODE, N8N, "worker"], user="nobody", env=n8n_env(secrets, "worker"), cwd=f"{WORK}/n8n")
        wup = wait_for(lambda: http("GET", "/healthz/readiness", base="http://127.0.0.1:5680")[0] == 200, 180, 1.0)
        log(f"n8n worker healthy: {bool(wup)}")
        # ---- owner, login, API key (throwaway test values)
        st, body, _ = http("POST", "/rest/owner/setup", {"email": secrets["N8N_TEST_OWNER_EMAIL"], "firstName": "Stop",
                                                          "lastName": "Check", "password": secrets["N8N_TEST_OWNER_PASSWORD"]})
        log(f"owner setup: HTTP {st}")
        st, body, hd = http("POST", "/rest/login", {"emailOrLdapLoginId": secrets["N8N_TEST_OWNER_EMAIL"],
                                                    "password": secrets["N8N_TEST_OWNER_PASSWORD"]})
        cookie = (hd.get("Set-Cookie") or "").split(";")[0] if hd else ""
        log(f"login: HTTP {st}, cookie {'yes' if cookie else 'no'}")
        scopes = [x for x in open(os.path.join(HERE, "owner_api_key_scopes.txt"), encoding="utf-8").read().split() if x]
        st, body, _ = http("POST", "/rest/api-keys", {"label": "spike", "scopes": scopes, "expiresAt": None},
                           headers={"Cookie": cookie})
        key = (body.get("data") or {}).get("rawApiKey") if isinstance(body, dict) else None
        log(f"api key: HTTP {st}, key {'yes' if key else 'no'}")
        if not key:
            REPORT["notes"].append(f"API key not created: {str(body)[:300]}")
            return
        H = {"X-N8N-API-KEY": key}
        # ---- the workflow: Webhook -> Code node that pings the recorder each second
        code = ("const n0 = Date.now();\n"
                "for (let i = 1; i <= 150; i++) {\n"
                "  let sm = '';\n"
                "  try { sm = '&smono=' + (Number(process.hrtime.bigint()) / 1e9).toFixed(6); } catch (e) { sm = '&smono_err=' + encodeURIComponent(String(e).slice(0, 40)); }\n"
                "  try { await this.helpers.httpRequest({ method: 'GET', url: 'http://127.0.0.1:8765/ping?canary=n8n_code&trial=SPIKE&n=' + i + sm }); } catch (e) {}\n"
                "  await new Promise((r) => setTimeout(r, 1000));\n"
                "}\n"
                "return [{ json: { done: true, ms: Date.now() - n0 } }];\n")
        wf = {"name": "spike-code-loop", "settings": {"executionOrder": "v1"},
              "nodes": [{"id": "w1", "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
                         "position": [0, 0], "webhookId": "5e0b6a1e-0000-4000-8000-000000000001",
                         "parameters": {"httpMethod": "POST", "path": "spike", "responseMode": "onReceived"}},
                        {"id": "c1", "name": "Loop", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [300, 0],
                         "parameters": {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": code}}],
              "connections": {"Webhook": {"main": [[{"node": "Loop", "type": "main", "index": 0}]]}}}
        st, body, _ = http("POST", "/api/v1/workflows", wf, headers=H)
        wid, vid = (body.get("id"), body.get("versionId")) if isinstance(body, dict) else (None, None)
        log(f"create workflow: HTTP {st}, id {wid}")
        st, body, _ = http("POST", f"/api/v1/workflows/{wid}/publish", {"versionId": vid}, headers=H)
        log(f"publish: HTTP {st} {str(body)[:120] if st != 200 else ''}")

        def running_exec():
            st, b, _ = http("GET", f"/api/v1/executions?workflowId={wid}&status=running", headers=H)
            d = (b.get("data") or []) if isinstance(b, dict) else []
            return d[0]["id"] if d else None

        # ---- C2: run, observe, stop
        t_pub = now()  # the publication service registers webhooks asynchronously: retry until it answers
        st = wait_for(lambda: (lambda r: r if r == 200 else None)(http("POST", "/webhook/spike", {"trial": TRIAL})[0]), 60, 1.0)
        log(f"webhook: HTTP {st} after {now() - t_pub:.1f} s")
        REPORT["checks"]["webhook_registered_after_s"] = round(now() - t_pub, 1)
        eid = wait_for(running_exec, 30)
        log(f"running execution: {eid}")
        time.sleep(10)
        REPORT["checks"]["C1_memory_during_execution"] = rss_table()
        before = len(pings("n8n_code"))
        t_stop = now()
        st, body, _ = http("POST", f"/api/v1/executions/{eid}/stop", {}, headers=H)
        t_reply = now()
        log(f"stop execution: HTTP {st} in {t_reply - t_stop:.3f} s: {str(body)[:200]}")
        statuses = []
        for _ in range(20):
            s2, b2, _ = http("GET", f"/api/v1/executions/{eid}", headers=H)
            statuses.append((round(now() - t_stop, 2), (b2 or {}).get("status") if isinstance(b2, dict) else s2))
            time.sleep(0.5)
        time.sleep(20)
        cp = pings("n8n_code")
        after = [p for p in cp if p["mono"] > t_stop]
        REPORT["checks"]["C2_code_node_stop"] = {
            "pings_before_stop": before, "pings_after_stop": len(after),
            "last_after_stop_rel": round(after[-1]["mono"] - t_stop, 3) if after else None,
            "smono_present": any("smono" in p for p in cp), "smono_error": next((p.get("smono_err") for p in cp
                                                                                 if "smono_err" in p), None),
            "stop_http": st, "stop_reply_s": round(t_reply - t_stop, 3), "status_after_stop": statuses}
        log(f"C2: {before} pings before the stop, {len(after)} after (last at "
            f"{REPORT['checks']['C2_code_node_stop']['last_after_stop_rel']} s); statuses {statuses[:3]} ... {statuses[-1]}")
        # ---- C4: worker down, a queued job, stop it
        stop("n8n-worker")
        st, _, _ = http("POST", "/webhook/spike", {"trial": TRIAL})
        time.sleep(3)
        keys = subprocess.run(["valkey-cli", "-p", "6380", "--scan", "--pattern", "bull:*"], capture_output=True, text=True).stdout.split()
        st2, b2, _ = http("GET", f"/api/v1/executions?workflowId={wid}", headers=H)
        ex = (b2.get("data") or []) if isinstance(b2, dict) else []
        st5, b5, _ = http("GET", f"/api/v1/executions?workflowId={wid}&status=new", headers=H)
        ex_new = (b5.get("data") or []) if isinstance(b5, dict) else []
        jobs = [k for k in keys if k.split(":")[-1].isdigit()]
        job_data = subprocess.run(["valkey-cli", "-p", "6380", "HGET", jobs[0], "data"], capture_output=True,
                                  text=True).stdout.strip() if jobs else ""
        try:
            job_eid = str(json.loads(job_data).get("executionId"))
        except Exception:  # noqa: BLE001
            job_eid = None
        qid = (ex_new[0]["id"] if ex_new else None) or job_eid
        REPORT["checks"]["C4_listing"] = {"unfiltered_list_ids": [(e.get("id"), e.get("status")) for e in ex],
                                          "status_new_list_ids": [(e.get("id"), e.get("status")) for e in ex_new],
                                          "bull_job_execution_id": job_eid}
        st3, b3, _ = http("POST", f"/api/v1/executions/{qid}/stop", {}, headers=H) if qid else (None, "no queued execution", None)
        time.sleep(2)
        keys_after = subprocess.run(["valkey-cli", "-p", "6380", "--scan", "--pattern", "bull:*"], capture_output=True, text=True).stdout.split()
        s4, b4, _ = http("GET", f"/api/v1/executions/{qid}", headers=H) if qid else (None, None, None)
        REPORT["checks"]["C4_queue"] = {"webhook_http": st, "queued_execution": qid,
                                        "statuses_listed": [(e.get("id"), e.get("status")) for e in ex],
                                        "bull_keys_before": sorted(keys)[:30], "stop_http": st3,
                                        "stop_body": str(b3)[:200], "bull_keys_after": sorted(keys_after)[:30],
                                        "status_after": (b4 or {}).get("status") if isinstance(b4, dict) else s4}
        log(f"C4: queued {qid}, stop HTTP {st3}, status after {REPORT['checks']['C4_queue']['status_after']}; "
            f"bull keys {len(keys)} -> {len(keys_after)}")
        # ---- C3: the untouched control while Valkey and main are stopped
        t3 = now()
        stop("valkey")
        time.sleep(12)
        stop("n8n-main")
        time.sleep(5)
        tw = [p["mono"] for p in pings("twin") if p["mono"] > t3 - 1]
        gaps = [round(b - a, 3) for a, b in zip(tw, tw[1:])]
        REPORT["checks"]["C3_recorder_independence"] = {"twin_pings": len(tw), "max_gap_s": max(gaps) if gaps else None,
                                                        "window_s": round(now() - t3, 1)}
        log(f"C3: twin {len(tw)} pings over {now() - t3:.1f} s, largest gap {max(gaps) if gaps else None} s")
        REPORT["checks"]["memory_total_mb_during_execution"] = round(sum(r["rss_mb"] for r in
                                                                         REPORT["checks"]["C1_memory_during_execution"]), 1)
    finally:
        for name in ("n8n-worker", "n8n-main", "twin", "recorder", "valkey", "postgres"):
            try:
                stop(name, signal.SIGTERM, 40)
            except Exception as e:  # noqa: BLE001
                log(f"stop {name}: {e}")
        left = subprocess.run(["pgrep", "-af", "n8n|valkey-server|postgres -D|recorder.py|canary.py"], capture_output=True,
                              text=True).stdout.strip()
        me = {str(os.getpid()), str(os.getppid())}
        REPORT["leftover_processes"] = [x for x in left.splitlines() if "pgrep" not in x and "spike" not in x
                                        and x.split()[0] not in me]
        json.dump(REPORT, open(os.path.join(OUT, "report.json"), "w", encoding="utf-8"), indent=1)
        log(f"report: {os.path.join(OUT, 'report.json')}; leftover processes: {REPORT['leftover_processes'] or 'none'}")


if __name__ == "__main__":
    main()
