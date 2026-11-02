#!/usr/bin/env python3
"""Scripted stand-in chat model for the ai_agent carrier (Experiment B). No real model and no outside calls.

An OpenAI-compatible endpoint: POST /v1/chat/completions and GET /v1/models. While a conversation has fewer
than --steps tool results, each reply asks for one call of the first tool offered; after that it answers "done".
The step count is read from the request itself (the number of tool messages), so every conversation follows the
same script whatever else runs at the same time. Replies wait --delay seconds, which paces the agent's loop.
Both plain and streamed (SSE) replies are supported. Every request is logged to --log (JSON lines).

    python3 stub_model.py --port 8899 --log stub.jsonl [--steps 15] [--delay 1.0]
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None
LOCK = threading.Lock()


def tool_args(tool):
    """Arguments for a call: a short string for each required property, otherwise none."""
    params = (tool.get("function") or {}).get("parameters") or {}
    props = params.get("properties") or {}
    return {k: "ping" for k in (params.get("required") or []) if k in props}


def reply(req):
    msgs = req.get("messages") or []
    done = sum(1 for m in msgs if m.get("role") == "tool")
    tools = req.get("tools") or []
    if tools and done < ARGS.steps:
        t = tools[0]
        call = {"id": f"call_{done + 1}", "type": "function",
                "function": {"name": t["function"]["name"], "arguments": json.dumps(tool_args(t))}}
        return {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls", done
    return {"role": "assistant", "content": "done"}, "stop", done


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_req(self, rec):
        rec["t"], rec["mono"] = time.time(), time.monotonic()
        with LOCK, open(ARGS.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def do_GET(self):  # noqa: N802
        self.log_req({"method": "GET", "path": self.path})
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "stub-model", "object": "model"}]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            req = {}
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.log_req({"method": "POST", "path": self.path, "unsupported": True})
            return self._send(404, {"error": {"message": "only /v1/chat/completions is implemented"}})
        msg, finish, done = reply(req)
        self.log_req({"method": "POST", "path": self.path, "stream": bool(req.get("stream")),
                      "model": req.get("model"), "tools": [t.get("function", {}).get("name") for t in req.get("tools") or []],
                      "tool_results_so_far": done, "finish": finish})
        time.sleep(ARGS.delay)
        cid, now = f"chatcmpl-stub-{done}", int(time.time())
        if not req.get("stream"):
            return self._send(200, {"id": cid, "object": "chat.completion", "created": now, "model": req.get("model"),
                                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        chunks = []
        base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": req.get("model")}
        if msg.get("tool_calls"):
            c = msg["tool_calls"][0]
            chunks.append({"index": 0, "delta": {"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": c["id"], "type": "function", "function": {"name": c["function"]["name"], "arguments": ""}}]},
                "finish_reason": None})
            chunks.append({"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": c["function"]["arguments"]}}]},
                           "finish_reason": None})
        else:
            chunks.append({"index": 0, "delta": {"role": "assistant", "content": msg["content"]}, "finish_reason": None})
        chunks.append({"index": 0, "delta": {}, "finish_reason": finish})
        body = "".join("data: " + json.dumps(dict(base, choices=[ch])) + "\n\n" for ch in chunks) + "data: [DONE]\n\n"
        return self._send(200, body.encode(), "text/event-stream")

    def log_message(self, *a):
        pass


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--log", required=True)
    ap.add_argument("--steps", type=int, default=15)
    ap.add_argument("--delay", type=float, default=1.0)
    ARGS = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
