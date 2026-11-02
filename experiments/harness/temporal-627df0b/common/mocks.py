#!/usr/bin/env python3
"""Stand-in services for stop tests: a key issuer and an outside service (standard library only).

Why: two of the stops we test act outside the agent software. "Revoke a key" acts at whoever
issues the agent's credentials, and "cut the network" acts on the path to the services it
calls. These mocks let a scripted agent use a key and reach an outside service exactly as a
real one would, while every use is logged where the agent cannot change it.

Issuer (default 127.0.0.1:8770, and a Unix socket):
  POST /keys    {"key_id"}                  -> {"key"}            long-lived key
  POST /token   {"key", "ttl_s"?}           -> {"token", "exp"}   short-lived token minted from a key
  POST /revoke  {"key"} or {"key_id"}, "cascade"?  -> revoke the key; with cascade, also every
                token minted from it. Without cascade, tokens already minted stay valid until
                they expire (the usual behaviour of self-contained tokens).
  POST /check   {"credential"}              -> {"valid", "why"}
Outside service (default 127.0.0.1:8771, and a Unix socket):
  POST /act     {"credential", "marker"}    -> checks the credential at the issuer; records an effect
  POST /job     {"credential", "marker", "delay_s", "check_at": "submit"|"run"}
                -> queues work that runs later; the credential is checked when submitted or when run
  POST /reset   {}                          -> harness clean-up between trials: queued jobs are dropped
  POST /slow    {"marker", "seconds"}       -> a request that stays in flight for N seconds. The reply
                streams one byte a second, so the service logs "slow_client_gone" within a second
                or two of the caller hanging up (and "slow_end" if it never does).
Both:  GET /health, GET /log (all events so far, JSON lines)

Roles (--role): "both" (default, one process), "issuer" or "outside". For the network-cut stop the
outside service runs in its own network namespace, joined to the agent's by a virtual cable, and
checks credentials through the issuer's Unix socket (--issuer-sock), which a network cut does not
touch. Every request body may carry "trial"; it is copied into the logged event.

Every event is appended to <logdir>/issuer.jsonl or <logdir>/outside.jsonl with wall and
monotonic times. The Unix sockets let work running outside the trial's network namespace
(for example a cron job) still reach the mocks.
"""
import argparse
import http.client
import http.server
import json
import os
import secrets
import socket
import socketserver
import threading
import time
import urllib.request


class UnixHTTP(http.client.HTTPConnection):
    """HTTP over a Unix socket (the standard library has no built-in client for it)."""

    def __init__(self, path, timeout=3):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sock_path)


def post_json(url_or_sock, route, body, timeout=3):
    """POST JSON to http://host:port or to a Unix socket path; returns (status, dict)."""
    data = json.dumps(body).encode()
    if url_or_sock.startswith("http"):
        req = urllib.request.Request(url_or_sock + route, data=data, headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    c = UnixHTTP(url_or_sock, timeout)
    try:
        c.request("POST", route, body=data, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        c.close()


def _now():
    return {"wall": time.time(), "mono": time.monotonic()}


_ctx = threading.local()  # the "trial" of the request being handled, copied into its events


class EventLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, **ev):
        ev.setdefault("trial", getattr(_ctx, "trial", None))
        ev |= _now()
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        return ev

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""


class IssuerState:
    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.keys = {}    # key -> {"key_id", "revoked": bool}
        self.tokens = {}  # token -> {"key", "exp", "revoked": bool}

    def new_key(self, key_id):
        k = "key_" + secrets.token_hex(8)
        with self.lock:
            self.keys[k] = {"key_id": key_id, "revoked": False}
        self.log.write(event="key_issued", key_id=key_id, key=k)
        return {"key": k}

    def mint(self, key, ttl_s=300.0):
        with self.lock:
            rec = self.keys.get(key)
            if not rec or rec["revoked"]:
                self.log.write(event="token_refused", key=key, why="unknown or revoked key")
                return None
            t = "tok_" + secrets.token_hex(8)
            exp = time.time() + float(ttl_s)
            self.tokens[t] = {"key": key, "exp": exp, "revoked": False}
        self.log.write(event="token_minted", key=key, token=t, exp=exp)
        return {"token": t, "exp": exp}

    def revoke(self, key=None, key_id=None, cascade=False):
        with self.lock:
            hits = [k for k, r in self.keys.items() if k == key or (key_id and r["key_id"] == key_id)]
            for k in hits:
                self.keys[k]["revoked"] = True
            n_tok = 0
            if cascade:
                for t, r in self.tokens.items():
                    if r["key"] in hits and not r["revoked"]:
                        r["revoked"] = True
                        n_tok += 1
        self.log.write(event="revoked", keys=hits, cascade=bool(cascade), tokens_revoked=n_tok)
        return {"revoked_keys": hits, "tokens_revoked": n_tok}

    def check(self, cred):
        with self.lock:
            if cred in self.keys:
                ok, why = (not self.keys[cred]["revoked"]), ("key revoked" if self.keys[cred]["revoked"] else "key valid")
            elif cred in self.tokens:
                r = self.tokens[cred]
                if r["revoked"]:
                    ok, why = False, "token revoked"
                elif time.time() > r["exp"]:
                    ok, why = False, "token expired"
                else:
                    ok, why = True, "token valid (its key is not re-checked)"
            else:
                ok, why = False, "unknown credential"
        self.log.write(event="check", credential=cred, valid=ok, why=why)
        return {"valid": ok, "why": why}


class OutsideState:
    def __init__(self, log, issuer):
        self.log = log
        self.issuer = issuer  # http://127.0.0.1:port, or a Unix socket path
        self.generation = 0   # bumped by /reset: queued jobs from before a reset never take effect

    def _check(self, cred):
        try:
            return post_json(self.issuer, "/check", {"credential": cred})[1]
        except Exception as e:  # noqa: BLE001 - an unreachable issuer counts as a refusal
            return {"valid": False, "why": f"issuer unreachable: {e}"}

    def act(self, cred, marker):
        c = self._check(cred)
        self.log.write(event="effect" if c["valid"] else "refused", marker=marker, credential=cred, why=c["why"])
        return {"accepted": c["valid"], "why": c["why"]}

    def job(self, cred, marker, delay_s, check_at="submit"):
        if check_at == "submit":
            c = self._check(cred)
            if not c["valid"]:
                self.log.write(event="job_refused", marker=marker, credential=cred, why=c["why"])
                return {"queued": False, "why": c["why"]}
        self.log.write(event="job_queued", marker=marker, credential=cred, delay_s=delay_s, check_at=check_at)

        trial, gen = getattr(_ctx, "trial", None), self.generation

        def run():
            _ctx.trial = trial
            time.sleep(float(delay_s))
            if gen != self.generation:
                self.log.write(event="job_dropped_by_reset", marker=marker)
                return
            if check_at == "run":
                c2 = self._check(cred)
                if not c2["valid"]:
                    self.log.write(event="job_refused_at_run", marker=marker, credential=cred, why=c2["why"])
                    return
            self.log.write(event="job_effect", marker=marker, credential=cred)

        threading.Thread(target=run, daemon=True).start()
        return {"queued": True}

    def reset(self):
        """Harness clean-up between trials (not a stop under test): drop every queued job."""
        self.generation += 1
        self.log.write(event="reset", generation=self.generation)
        return {"generation": self.generation}

    def slow(self, handler, marker, seconds):
        """Stream one byte a second for `seconds`; log when the caller goes away."""
        t0 = time.monotonic()
        self.log.write(event="slow_start", marker=marker, seconds=seconds)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            while time.monotonic() - t0 < float(seconds):
                handler.wfile.write(b".")
                handler.wfile.flush()
                time.sleep(1)
        except OSError as e:  # BrokenPipeError, ConnectionResetError, ...
            self.log.write(event="slow_client_gone", marker=marker, after_s=round(time.monotonic() - t0, 3),
                           error=type(e).__name__)
            return
        self.log.write(event="slow_end", marker=marker, after_s=round(time.monotonic() - t0, 3))


def make_handler(routes, log, streams=None):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep stderr quiet
            pass

        def _send(self, code, obj, ctype="application/json"):
            body = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/log":
                return self._send(200, log.read().encode(), "application/x-ndjson")
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            _ctx.trial = data.get("trial")
            if streams and self.path in streams:
                return streams[self.path](self, data)
            fn = routes.get(self.path)
            if not fn:
                return self._send(404, {"error": "not found"})
            out = fn(data)
            return self._send(200 if out is not None else 403, out if out is not None else {"error": "refused"})

        # Unix-socket requests have no (host, port) client address
        def address_string(self):
            return "unix" if isinstance(self.client_address, (str, bytes)) or not self.client_address else super().address_string()

    return H


class TCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        req, _ = super().get_request()
        return req, ("unix", 0)


def serve(name, host, port, sock, handler):
    servers = [TCPServer((host, port), handler)]
    if sock:
        os.makedirs(os.path.dirname(sock), exist_ok=True)
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass
        servers.append(UnixServer(sock, handler))
        os.chmod(sock, 0o666)
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True, name=f"{name}-{type(s).__name__}").start()
    return servers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logdir", required=True)
    ap.add_argument("--role", choices=["both", "issuer", "outside"], default="both")
    ap.add_argument("--issuer-port", type=int, default=8770)
    ap.add_argument("--outside-port", type=int, default=8771)
    ap.add_argument("--outside-host", default="127.0.0.1")
    ap.add_argument("--sockdir", default="/run/stopcheck")
    ap.add_argument("--issuer-sock", default=None,
                    help="outside role: reach the issuer through this Unix socket (default: its TCP port)")
    args = ap.parse_args()
    isock, osock = os.path.join(args.sockdir, "issuer.sock"), os.path.join(args.sockdir, "outside.sock")
    if args.role in ("both", "issuer"):
        ilog = EventLog(os.path.join(args.logdir, "issuer.jsonl"))
        iss = IssuerState(ilog)
        iroutes = {"/keys": lambda d: iss.new_key(d.get("key_id", "agent")),
                   "/token": lambda d: iss.mint(d.get("key", ""), d.get("ttl_s", 300.0)),
                   "/revoke": lambda d: iss.revoke(d.get("key"), d.get("key_id"), d.get("cascade", False)),
                   "/check": lambda d: iss.check(d.get("credential", ""))}
        serve("issuer", "127.0.0.1", args.issuer_port, isock, make_handler(iroutes, ilog))
        ilog.write(event="issuer_started", port=args.issuer_port, sock=isock)
    if args.role in ("both", "outside"):
        olog = EventLog(os.path.join(args.logdir, "outside.jsonl"))
        out = OutsideState(olog, args.issuer_sock or f"http://127.0.0.1:{args.issuer_port}")
        oroutes = {"/act": lambda d: out.act(d.get("credential", ""), d.get("marker", "")),
                   "/job": lambda d: out.job(d.get("credential", ""), d.get("marker", ""), d.get("delay_s", 30),
                                             d.get("check_at", "submit")),
                   "/reset": lambda d: out.reset()}
        ostreams = {"/slow": lambda h, d: out.slow(h, d.get("marker", ""), d.get("seconds", 30))}
        serve("outside", args.outside_host, args.outside_port, osock, make_handler(oroutes, olog, ostreams))
        olog.write(event="outside_started", host=args.outside_host, port=args.outside_port, sock=osock,
                   issuer=out.issuer)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
