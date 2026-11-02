#!/usr/bin/env python3
"""Self-test for mocks.py: run before each batch so a broken stand-in is never mistaken for a
platform result. Starts the mocks on spare ports, exercises every route over TCP and the Unix
socket, and checks the logged events. Exit 0 only if every check passes."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
IP, OP = 18770, 18771


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self.path_ = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.path_)


def post(port, route, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def post_unix(sock, route, body):
    c = UnixHTTP(sock)
    c.request("POST", route, body=json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def raw_post(route, body):
    """A bare HTTP/1.0 POST, for callers that read a streamed reply byte by byte."""
    crlf = bytes([13, 10])
    head = [f"POST {route} HTTP/1.0", "Content-Type: application/json", f"Content-Length: {len(body)}", "", ""]
    return crlf.join(h.encode() for h in head) + body


def events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def main():
    tmp = tempfile.mkdtemp(prefix="mocks-test-")
    sockdir = os.path.join(tmp, "sock")
    # as in a trial: two processes, the outside service reaching the issuer only by its Unix socket
    base = [sys.executable, os.path.join(HERE, "mocks.py"), "--logdir", tmp, "--issuer-port", str(IP),
            "--outside-port", str(OP), "--sockdir", sockdir]
    p_iss = subprocess.Popen(base + ["--role", "issuer"])
    time.sleep(1.0)
    p = subprocess.Popen(base + ["--role", "outside", "--issuer-sock", os.path.join(sockdir, "issuer.sock")])
    fails = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{OP}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        k = post(IP, "/keys", {"key_id": "agent-1"})[1]["key"]
        t = post(IP, "/token", {"key": k, "ttl_s": 300})[1]["token"]
        short = post(IP, "/token", {"key": k, "ttl_s": 1})[1]["token"]
        check("key works before revoke", post(OP, "/act", {"credential": k, "marker": "m1"})[1]["accepted"])
        check("token works before revoke", post(OP, "/act", {"credential": t, "marker": "m2"})[1]["accepted"])
        check("same route over the Unix socket", post_unix(os.path.join(sockdir, "outside.sock"), "/act",
                                                           {"credential": k, "marker": "m3"})[1]["accepted"])
        # job checked only when it runs, queued before the revoke
        post(OP, "/job", {"credential": t, "marker": "job-run", "delay_s": 1.5, "check_at": "run"})
        # job checked at submit, queued before the revoke
        post(OP, "/job", {"credential": t, "marker": "job-submit", "delay_s": 1.5, "check_at": "submit"})
        post(IP, "/revoke", {"key": k, "cascade": False})
        check("revoked key refused", not post(OP, "/act", {"credential": k, "marker": "m4"})[1]["accepted"])
        check("token from revoked key still accepted without cascade",
              post(OP, "/act", {"credential": t, "marker": "m5"})[1]["accepted"])
        check("no new token from revoked key", post(IP, "/token", {"key": k})[0] == 403)
        time.sleep(1.2)
        check("expired token refused", not post(OP, "/act", {"credential": short, "marker": "m6"})[1]["accepted"])
        post(IP, "/revoke", {"key": k, "cascade": True})
        check("token refused after cascade revoke", not post(OP, "/act", {"credential": t, "marker": "m7"})[1]["accepted"])
        time.sleep(1.0)
        ev = events(os.path.join(tmp, "outside.jsonl"))
        by = {(e["event"], e.get("marker")) for e in ev}
        check("job checked at run is refused after revoke", ("job_refused_at_run", "job-run") in by)
        check("job checked at submit still takes effect after revoke", ("job_effect", "job-submit") in by)
        # a caller that hangs up after two bytes: the service must notice within a few seconds
        sk = socket.create_connection(("127.0.0.1", OP), timeout=5)
        body = json.dumps({"marker": "slow-gone", "seconds": 30, "trial": "T1"}).encode()
        sk.sendall(raw_post("/slow", body))
        got = b""
        while got.count(b".") < 2:
            got += sk.recv(64)
        sk.close()
        # a caller that stays to the end
        sk2 = socket.create_connection(("127.0.0.1", OP), timeout=10)
        body2 = json.dumps({"marker": "slow-full", "seconds": 2}).encode()
        sk2.sendall(raw_post("/slow", body2))
        while sk2.recv(64):
            pass
        sk2.close()
        time.sleep(4)
        ev = events(os.path.join(tmp, "outside.jsonl"))
        gone = [e for e in ev if e["event"] == "slow_client_gone" and e.get("marker") == "slow-gone"]
        check("service notices a caller that hung up", bool(gone) and gone[0]["after_s"] < 6, str(gone))
        check("trial field copied into the event", bool(gone) and gone[0].get("trial") == "T1")
        check("full slow request logs its end", any(e["event"] == "slow_end" and e.get("marker") == "slow-full" for e in ev))
        check("outside service reached the issuer by socket", any(e["event"] == "outside_started" and
                                                                  e.get("issuer", "").endswith("issuer.sock") for e in ev))
        k2 = post(IP, "/keys", {"key_id": "agent-2"})[1]["key"]
        post(OP, "/job", {"credential": k2, "marker": "job-reset", "delay_s": 1.0})
        post(OP, "/reset", {})
        time.sleep(1.5)
        ev = events(os.path.join(tmp, "outside.jsonl"))
        check("reset drops a queued job", any(e["event"] == "job_dropped_by_reset" and e.get("marker") == "job-reset" for e in ev)
              and not any(e["event"] == "job_effect" and e.get("marker") == "job-reset" for e in ev))
        mono_ok = all(isinstance(e.get("mono"), float) for e in ev)
        check("every event has a monotonic time", mono_ok)
    finally:
        for q in (p, p_iss):
            q.terminate()
            q.wait(5)
    print(f"{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}  (logs in {tmp})")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
