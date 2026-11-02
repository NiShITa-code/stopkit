#!/usr/bin/env python3
"""Local stand-in for the model APIs in the client probe (see PREDICTIONS.md).
Standard library only.

Serves the Anthropic Messages API (POST /v1/messages, for Claude Code) and the OpenAI
Responses API (POST /v1/responses, for Codex) on 127.0.0.1. Every request is recorded,
without its credential headers, to the current run's log. The reply follows a fixed
script:
  1. while the conversation has no call to the probe server's `slow` tool, ask for it;
  2. in the "bg-stop" scenario, once a tool result reports that the call moved to the
     background, ask for the client's TaskStop tool on that task;
  3. otherwise reply with a short closing text.
A request that offers no `slow` tool (a side request such as a title) gets "ok".

The control file names the current run and is read at every request, so one stand-in
serves a whole batch: {"run_id": ..., "log": <path>, "scenario": <stop path>}.

Usage: python3 stub_model.py <port> <control file>
"""

import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
CONTROL = sys.argv[2]
_lock = threading.Lock()
TASK_ID = re.compile(r"(?i)\btask(?:[ _-]?id)?\b[\"'`]?\s*[:=]?\s*[\"'`]?([A-Za-z0-9][A-Za-z0-9_-]{3,})")
SKIP_HEADERS = {"authorization", "x-api-key", "cookie"}


def control():
    try:
        with open(CONTROL) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"run_id": "none", "log": None, "scenario": "control"}


def record(ctl, entry):
    if ctl.get("log"):
        with _lock, open(ctl["log"], "a") as f:
            f.write(json.dumps(entry) + "\n")


def find_tool(tools, test):
    """(name, namespace) of the first tool, at any depth, whose name passes test."""
    stack = [(t, None) for t in tools or []]
    while stack:
        tool, namespace = stack.pop(0)
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if isinstance(name, str) and test(name):
            return name, namespace
        for key in ("tools", "functions"):
            if isinstance(tool.get(key), list):
                stack.extend((t, tool.get("name")) for t in tool[key])
    return None, None


def text_of(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(text_of(v) for v in value)
    if isinstance(value, dict):
        return " ".join(text_of(value.get(k)) for k in ("text", "content", "output") if k in value)
    return ""


def schema_id_param(tool_list, name):
    for tool in tool_list or []:
        if isinstance(tool, dict) and tool.get("name") == name:
            props = ((tool.get("input_schema") or tool.get("parameters") or {}).get("properties") or {})
            for key in props:
                if "id" in key.lower():
                    return key
            return next(iter(props), "task_id")
    return "task_id"


# --------------------------------------------------------------------------- Anthropic

def anthropic_plan(body, ctl):
    """Claude Code 2.1.310 defers MCP tools and TaskStop: the request names them in a
    reminder and offers ToolSearch, which loads them ("select:<name>,..."), as a real
    model would do first. Loaded tools then appear in later requests' tool lists."""
    tools = body.get("tools") or []
    if not tools:
        return {"kind": "text", "text": "ok"}
    loaded = {t.get("name") for t in tools if isinstance(t, dict) and not t.get("defer_loading")}
    text = json.dumps(body)
    slow = next((n for n in loaded if isinstance(n, str) and n.endswith("slow")), None)
    if slow is None:
        named = re.search(r"mcp__[A-Za-z0-9_-]+__slow", text)
        slow = named.group(0) if named else None
    if slow is None:
        return {"kind": "text", "text": "ok"}
    stop_tool = "TaskStop" if ctl.get("scenario") == "bg-stop" and "TaskStop" in text else None
    called = set()
    results = {}
    for message in body.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                called.add(block.get("name"))
                results.setdefault("_ids", {})[block.get("id")] = block.get("name")
            elif block.get("type") == "tool_result":
                name = results.get("_ids", {}).get(block.get("tool_use_id"))
                results[name] = text_of(block.get("content"))
    need = [n for n in (slow, stop_tool) if n and n not in loaded and n not in called]
    if need and "ToolSearch" in loaded and "ToolSearch" not in called:
        return {"kind": "tool", "name": "ToolSearch", "input": {"query": "select:" + ",".join(need), "max_results": 5}}
    if slow not in called:
        return {"kind": "tool", "name": slow, "input": {"run_id": ctl.get("run_id", "x")}}
    if stop_tool and stop_tool not in called and results.get(slow):
        match = TASK_ID.search(results[slow])
        if match:
            return {"kind": "tool", "name": stop_tool,
                    "input": {schema_id_param(tools, stop_tool): match.group(1)}}
    return {"kind": "text", "text": "Done."}


def anthropic_stream(handler, model, plan):
    msg_id = "msg_" + uuid.uuid4().hex[:24]

    def ev(event, data):
        handler.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        handler.wfile.flush()

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()
    ev("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}})
    if plan["kind"] == "tool":
        ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:22], "name": plan["name"], "input": {}}})
        ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                   "delta": {"type": "input_json_delta", "partial_json": json.dumps(plan["input"])}})
        stop_reason = "tool_use"
    else:
        ev("content_block_start", {"type": "content_block_start", "index": 0,
                                   "content_block": {"type": "text", "text": ""}})
        ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                   "delta": {"type": "text_delta", "text": plan["text"]}})
        stop_reason = "end_turn"
    ev("content_block_stop", {"type": "content_block_stop", "index": 0})
    ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                         "usage": {"output_tokens": 12}})
    ev("message_stop", {"type": "message_stop"})


def anthropic_json(model, plan):
    if plan["kind"] == "tool":
        content = [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:22], "name": plan["name"],
                    "input": plan["input"]}]
        stop_reason = "tool_use"
    else:
        content = [{"type": "text", "text": plan["text"]}]
        stop_reason = "end_turn"
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 12}}


# --------------------------------------------------------------------------- OpenAI Responses

def openai_plan(body, ctl):
    slow, namespace = find_tool(body.get("tools") or [], lambda n: n.endswith("slow"))
    if not slow:
        return {"kind": "text", "text": "ok"}
    called = any(isinstance(item, dict) and item.get("type") == "function_call"
                 and str(item.get("name", "")).endswith("slow") for item in body.get("input") or [])
    if not called:
        return {"kind": "tool", "name": slow, "namespace": namespace,
                "input": {"run_id": ctl.get("run_id", "x")}}
    return {"kind": "text", "text": "Done."}


def openai_stream(handler, plan):
    resp_id = "resp_" + uuid.uuid4().hex[:24]

    def ev(data):
        handler.wfile.write(f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode())
        handler.wfile.flush()

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()
    ev({"type": "response.created", "response": {"id": resp_id}})
    if plan["kind"] == "tool":
        item = {"type": "function_call", "call_id": "call_" + uuid.uuid4().hex[:20], "name": plan["name"],
                "arguments": json.dumps(plan["input"])}
        if plan.get("namespace"):
            item["namespace"] = plan["namespace"]
    else:
        item = {"type": "message", "role": "assistant", "id": "msg_" + uuid.uuid4().hex[:20],
                "content": [{"type": "output_text", "text": plan["text"]}]}
    ev({"type": "response.output_item.done", "item": item})
    ev({"type": "response.completed", "response": {"id": resp_id, "usage": {
        "input_tokens": 10, "input_tokens_details": None, "output_tokens": 12,
        "output_tokens_details": None, "total_tokens": 22}}})


# --------------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return raw, json.loads(raw) if raw else {}
        except ValueError:
            return raw, {}

    def _record(self, raw, body, plan):
        ctl = control()
        record(ctl, {"t": time.time(), "run_id": ctl.get("run_id"), "method": self.command, "path": self.path,
                     "headers": {k: v for k, v in self.headers.items() if k.lower() not in SKIP_HEADERS},
                     "bytes": len(raw), "body": body, "reply": plan})
        return ctl

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self._record(b"", {}, None)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._record(b"", {}, None)
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "stub-model", "object": "model",
                                                         "type": "model", "display_name": "stub"}],
                             "has_more": False})
        else:
            self._json(404, {"error": {"type": "not_found", "message": "stub: unknown path"}})

    def do_POST(self):
        raw, body = self._body()
        path = self.path.split("?")[0]
        ctl = control()
        if path.endswith("/messages/count_tokens"):
            self._record(raw, body, {"kind": "count_tokens"})
            self._json(200, {"input_tokens": max(1, len(raw) // 4)})
        elif path.endswith("/messages"):
            plan = anthropic_plan(body, ctl)
            self._record(raw, body, plan)
            model = body.get("model", "stub-model")
            if body.get("stream"):
                anthropic_stream(self, model, plan)
                self.close_connection = True
            else:
                self._json(200, anthropic_json(model, plan))
        elif path.endswith("/responses"):
            plan = openai_plan(body, ctl)
            self._record(raw, body, plan)
            openai_stream(self, plan)
            self.close_connection = True
        else:
            self._record(raw, body, {"kind": "unknown"})
            self._json(404, {"error": {"type": "not_found", "message": "stub: unknown path"}})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"stub model listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
