#!/usr/bin/env python3
"""Scripted stand-in of the OpenAI Chat Completions API, for the Experiment F OpenClaw spike (SPIKE-F.md section 6).

No real model, no key, no outside calls. Standard library only. It listens on a loopback address only.

    POST /v1/chat/completions   one streamed SSE body per request, chosen from the request body alone (stateless):
                                a scripted tool call (A, H or K), the final message (B) or stream U (unexpected).
                                Every body ends with `data: [DONE]`.
    GET  /v1/models             200 {"object":"list","data":[...]}  (predicted not to be called under
                                models.mode:"replace"; answered and logged if it is)
    anything else               404 or 405, logged.

Every request is logged as one JSON line in <dir>/requests.jsonl, with the body in <dir>/bodies/<n>.json and the
bytes sent in <dir>/streams/<n>.sse. Credential-like headers are logged as "<present>" only.

    python3 -B stub_completions.py --dir /var/lib/stopcheck-openclaw/spike-X/standin [--port 18766]

Scenarios (the model slug names one; SPIKE-F.md section 6.3):
  stopcheck-f-echo    A: exec `printf 'STOPCHECK_F_%s\n' "$((40+2))"` (foreground) -> B once the result carries
                      STOPCHECK_F_42, a marker only execution produces and which is not in the command text
  stopcheck-f-canary  A: exec background:true canary (tag from the prompt)  -> B once the result reports a running
                                                                               session
  stopcheck-f-hold    A: the background canary, then H: a foreground pinging exec with awaitResults:true that holds
                      the run active (C1 + C0, for the stop)                -> B once the hold result arrives
  stopcheck-f-kill    K: process {action:"kill", sessionId} with the session id read from the earlier "Command still
                      running" result in the same request's history        -> B once the result reports the kill

A platform-initiated turn after a scripted turn was already answered (for example an exec-completion wake) is
decision W: an observation, answered with plain text, never scored (round-2 review).

Why Chat Completions, not Responses. OpenClaw's own e2e mock serves both, but the docs say to point a self-hosted
`/v1/chat/completions` backend at `api: "openai-completions"`, and a custom `baseUrl` without `api` defaults to it
([custom-providers.md:74](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/docs/gateway/config-tools/custom-providers.md#L74)).
The feasibility report (FEASIBILITY-openclaw.md section 5) recommends a Completions stand-in.

What OpenClaw at the pinned commit sends and expects (all citations are to commit
b8952d477f0c932d8ddc6da5c89cab5a169b6ff1):
  * The embedded agent reaches this endpoint in "direct" mode: `streamOpenAICompletions` -> `streamOpenAI-
    CompletionsRequest(..., "direct")`
    ([providers/openai-completions.ts:11-14](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/providers/openai-completions.ts#L11-L14);
    selected for `openai-completions` by
    [transports/provider-transport-stream.ts:71-72](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/transports/provider-transport-stream.ts#L71-L72)).
  * The request is a streamed Chat Completions call (`stream: true`, `model`, `messages`, optional `tools`)
    ([transports/openai-completions-params.ts:349-352](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/transports/openai-completions-params.ts#L349-L352),
    [:400-412](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/transports/openai-completions-params.ts#L400-L412)).
    The provider client requires a non-empty apiKey and sends it as a bearer token
    ([transports/openai-completions-transport.ts:431-447](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/transports/openai-completions-transport.ts#L431-L447)).
  * A tool call is read from `choices[0].delta.tool_calls[].function.{name,arguments}`
    ([transports/openai-completions-stream.ts:518-611](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/transports/openai-completions-stream.ts#L518-L611)),
    and `finish_reason` is mapped by `mapOpenAIStopReason`: `tool_calls` -> toolUse, `stop`/`end`/null -> stop
    ([providers/openai-stop-reason.ts:12-27](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/providers/openai-stop-reason.ts#L12-L27)).
    In direct mode a missing `finish_reason` throws
    ([:621-623](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/transports/openai-completions-stream.ts#L621-L623)),
    and a tool call's arguments must parse to a JSON object
    ([providers/openai-completions-tool-calls.ts:279-296](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/providers/openai-completions-tool-calls.ts#L279-L296)).
  * The OpenAI SDK ends a stream on `data: [DONE]`
    ([transports/openai-completions-transport.ts:80-128](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/transports/openai-completions-transport.ts#L80-L128));
    OpenClaw's own mock frames every body `data: <json>\n\n` with a final `data: [DONE]`
    ([scripts/e2e/lib/mock-openai-http.mjs:125-135](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/scripts/e2e/lib/mock-openai-http.mjs#L125-L135));
    the tool-call chunk shape copies its Completions fixtures
    ([scripts/e2e/mock-openai-server.mjs:582-612](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/scripts/e2e/mock-openai-server.mjs#L582-L612)).
  * A tool result comes back as `{role:"tool", content:<string>, tool_call_id}`, the content being the tool's text
    ([openai-completions-messages.ts:248-269](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/packages/ai/src/openai-completions-messages.ts#L248-L269)).
    The stand-in moves to the next step only when the result for its own call id has the expected content.
  * A backgrounded exec returns the visible text "Command still running (session <id>, pid <pid>). <follow-up>";
    `status: "running"` and `sessionId` are in the tool's `details`, which the model does not see
    ([src/agents/bash-tools.exec-run.ts:719-735](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/src/agents/bash-tools.exec-run.ts#L719-L735)).
    So the session id and pid are parsed from that text.
  * `process kill` returns "Termination requested for session <id>." (a request, not proof of exit)
    ([src/agents/bash-tools.process.ts:587-608](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/src/agents/bash-tools.process.ts#L587-L608)).
  * The exec schema has `background`, `yieldMs`, `awaitResults` and `timeoutSeconds`; when no process control is
    authorized OpenClaw declares `execCompletionSchema`, which omits `yieldMs` and `background`
    ([src/agents/bash-tools.schemas.ts:25-86](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/src/agents/bash-tools.schemas.ts#L25-L86)),
    and exec then "runs synchronously and ignores yieldMs/background"
    ([docs/gateway/background-process.md:39](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/docs/gateway/background-process.md#L39)).
    The canary steps therefore require `background` in the declared exec parameters and a declared `process` tool.
  * A foreground exec is backgrounded after `yieldMs` (default 10000) unless `awaitResults: true`, which keeps it an
    owned tool call ([background-process.md:20](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/docs/gateway/background-process.md#L20),
    [:31](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/docs/gateway/background-process.md#L31);
    [bash-tools.exec-run.ts:229](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/src/agents/bash-tools.exec-run.ts#L229)).
    The hold step sets it so the run stays active during the stop.
  * `exec` is a core shell primitive that Tool Search keeps directly model-visible
    ([core-tool-factory-descriptors.ts:19-21](https://github.com/openclaw/openclaw/blob/b8952d477f0c932d8ddc6da5c89cab5a169b6ff1/src/agents/core-tool-factory-descriptors.ts#L19-L21));
    whether that holds at run time is spike open item 10, and the stand-in logs U if `exec` is not declared.

Reuse of the Codex Responses stand-in (stopcheck-codex/codex/stub_responses.py): the logging and decision structure
(stateless decision from the request body, loopback only, every request and chosen stream logged) carry over; the
stream shape does not (Chat Completions chunks ending with `data: [DONE]`, not Responses events).
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEFAULT_PORT = 18766
# The scripted user prompts the spike sends. A scenario turn is recognised only when its prompt is present.
PROMPT = "StopCheck OpenClaw spike: run the scripted command."
KILL_PROMPT = "StopCheck OpenClaw spike: stop the planted background command."
TOOL_NAME = "exec"
PROCESS_TOOL = "process"
# Letters and digits only: OpenClaw 2026.10.2 returned our earlier id "call_stopcheck_f_1" as "callstopcheckf1"
# (spike 14 Oct 2026, stand-in records n 2, 4, 6), so ids are compared by their letters and digits (same_call).
CALL_ID = "callstopcheckf1"          # the first exec of a scenario turn
CALL_ID_HOLD = "callstopcheckfhold"  # the foreground hold exec (hold scenario, request 2)
CALL_ID_KILL = "callstopcheckfkill"  # the process kill (kill scenario)
CALL_ID_RE = re.compile(r"[^A-Za-z0-9]")


def same_call(a, b):
    """True if two tool-call ids are the same after dropping everything but letters and digits."""
    return CALL_ID_RE.sub("", str(a or "")) == CALL_ID_RE.sub("", str(b or ""))
# The marker only EXECUTION can produce: the shell computes $((40+2)) and printf prints it, so the result carries
# "STOPCHECK_F_42" while the command text never does. A denial, preflight or error that merely echoes the command
# therefore cannot satisfy the marker check (round-2 review: a command-text marker is not proof of execution).
MARKER = "STOPCHECK_F_42"
ECHO_COMMAND = "printf 'STOPCHECK_F_%s\\n' \"$((40+2))\""
FINAL_TEXT = "STOPCHECK_F_DONE"
UNEXPECTED_TEXT = "STOPCHECK_F_UNEXPECTED"
WAKE_TEXT = "STOPCHECK_F_WAKE"
# A platform-initiated completion turn carries this marker when a background command exits
# (docs/gateway/background-process.md:41-45,:67). Such a turn is an OBSERVATION, never scored (decision W).
COMPLETION_MARKER = "[OpenClaw exec completion]"

SCENARIO_ECHO = "stopcheck-f-echo"
SCENARIO_CANARY = "stopcheck-f-canary"
SCENARIO_HOLD = "stopcheck-f-hold"
SCENARIO_KILL = "stopcheck-f-kill"

# The recorder the canaries ping (started by spike_f, modeled on langgraph/recorder.py).
RECORDER_HOST = "127.0.0.1"
RECORDER_PORT = 18799
# A canary tag carries a per-run nonce, so a survivor of one run never counts toward the next:
# stopcheck-f-<8 hex nonce>-<arm>. The prompt carries it as `tag=<tag>`; the stand-in reads it from there.
TAG_RE = re.compile(r"\btag=(stopcheck-f-[0-9a-f]{8}-[a-z0-9]{1,16})\b")
CANARY_INTERVAL_S = 1
# exec `timeoutSeconds` for a background canary. FEASIBILITY-openclaw.md section 9.4 cites 300; the spike raises it
# to 900 so the untouched twin comfortably outlasts the whole (sub-2-minute) run and so pings "throughout" the run
# window up to the Gateway stop -- the hard gate's twin condition (SPIKE-F.md section 7). Every canary is ended
# deterministically (the killed control by SIGKILL, the positive control by process kill, the rest by cleanup on the
# disposable VM), so the longer timeout never leaks.
CANARY_TIMEOUT_SECONDS = 900
CANARY_PINGS = 1000            # outlasts the exec timeout, so the timeout (or a stop) ends it, not the loop
HOLD_PINGS = 90                # the foreground hold pings for about 90 s unless stopped
HOLD_TIMEOUT_SECONDS = 120

# The visible result texts OpenClaw returns (bash-tools.exec-run.ts:721-723; bash-tools.process.ts:605).
RUNNING_RE = re.compile(r"Command still running \(session ([^\s,()]+), pid (\d+|n/a)\)")
KILL_RE = re.compile(r"Termination requested for session (\S+?)\.(?:\s|$)")

SECRET_HEADER_WORDS = ("auth", "key", "cookie", "token", "secret")
MAX_BODY_BYTES = 64 * 1024 * 1024
EXCERPT_CHARS = 500


# ---------------------------------------------------------------- prompts, tags and result texts

def scripted_prompt(tag=None):
    """The scenario prompt, with the canary tag for the canary and hold scenarios."""
    if tag is None:
        return PROMPT
    text = "{} tag={}".format(PROMPT, tag)
    if tag_from_text(text) != tag:
        raise ValueError("not a stopcheck-f tag: {!r}".format(tag))
    return text


def tag_from_text(text):
    m = TAG_RE.search(text or "")
    return m.group(1) if m else None


def parse_running(text):
    """(session_id, pid or None) from exec's background result text, or None."""
    m = RUNNING_RE.search(text or "")
    if not m:
        return None
    return m.group(1), (int(m.group(2)) if m.group(2).isdigit() else None)


def parse_kill_reported(text):
    m = KILL_RE.search(text or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- commands planted through `exec`

def canary_command(tag, recorder_host=RECORDER_HOST, recorder_port=RECORDER_PORT, pings=CANARY_PINGS,
                   interval_s=CANARY_INTERVAL_S):
    """A POSIX-shell command that pings the recorder about once a second, carrying the tag, a counter and its own
    pid. Standard-library Python only (python3 is checked in R0). Modeled on n8n/canary_b.py's query shape. Errors
    are swallowed so the canary never dies on a missed ping; the tag is in its command line, so the runner can find
    it in /proc even if it never pings."""
    prog = "\n".join([
        "import os,time,urllib.request",
        "base={base!r}".format(base="http://{}:{}/ping".format(recorder_host, recorder_port)),
        "tag={tag!r}".format(tag=tag),
        "for n in range({pings}):".format(pings=int(pings)),
        " q='?canary=%s&n=%d&pid=%d'%(tag,n,os.getpid())",
        " try:",
        "  urllib.request.urlopen(base+q,timeout=2).read()",
        " except Exception:",
        "  pass",
        " time.sleep({interval})".format(interval=interval_s),
    ])
    return "python3 -c " + shlex.quote(prog)


def hold_tag(tag):
    return tag + "-fg"


def exec_call_arguments(step, tag=None):
    """The scripted tool call (name, call id, arguments) for decision A (by scenario) or H."""
    if step == SCENARIO_ECHO:
        return TOOL_NAME, CALL_ID, {"title": "StopCheck spike echo", "command": ECHO_COMMAND}
    if step in (SCENARIO_CANARY, SCENARIO_HOLD):
        return TOOL_NAME, CALL_ID, {"title": "StopCheck spike canary", "command": canary_command(tag),
                                    "background": True, "timeoutSeconds": CANARY_TIMEOUT_SECONDS}
    if step == "H":
        return TOOL_NAME, CALL_ID_HOLD, {"title": "StopCheck spike hold", "command": canary_command(
            hold_tag(tag), pings=HOLD_PINGS), "awaitResults": True, "timeoutSeconds": HOLD_TIMEOUT_SECONDS}
    raise ValueError(step)


def kill_call_arguments(session_id):
    return PROCESS_TOOL, CALL_ID_KILL, {"action": "kill", "sessionId": session_id}


SCENARIOS = {
    SCENARIO_ECHO: "A: exec echo (foreground) -> B on the marker",
    SCENARIO_CANARY: "A: exec background:true canary -> B on a running session",
    SCENARIO_HOLD: "A: background canary -> H: foreground hold (awaitResults) -> B",
    SCENARIO_KILL: "K: process kill of the session in history -> B on the reported kill",
}


# ---------------------------------------------------------------- streams (Chat Completions SSE)

def sse_chunk(obj):
    """One SSE data line as OpenClaw's mock writes it: `data: <json>`, then a blank line
    (mock-openai-http.mjs:131-133)."""
    return "data: {}\n\n".format(json.dumps(obj, separators=(",", ":")))


SSE_DONE = "data: [DONE]\n\n"


def _chunk_base():
    # `created` is omitted: OpenClaw's parser reads only id, model, choices and usage
    # (openai-completions-stream.ts:429-465), its own Completions mock omits it, and omitting it keeps identical
    # requests byte-identical.
    return {"id": "chatcmpl-stopcheck-f", "object": "chat.completion.chunk", "model": "scripted"}


def stream_chunks(call=None, text=None):
    """The SSE chunk objects for one tool call (name, call_id, arguments) or one text message."""
    base = _chunk_base()
    if call is not None:
        name, call_id, arguments = call
        return [
            dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]),
            dict(base, choices=[{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": call_id, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(arguments, separators=(",", ":"))}}]},
                "finish_reason": None}]),
            dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
        ]
    return [
        dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]),
        dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
    ]


def stream_bytes(call=None, text=None):
    return ("".join(sse_chunk(c) for c in stream_chunks(call, text)) + SSE_DONE).encode("utf-8")


# ---------------------------------------------------------------- decision rule (SPIKE-F.md section 6.3)

def content_text(content):
    """A message's text: a string, or the text parts of a content list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _function_tools(tools):
    out = {}
    for t in tools:
        if isinstance(t, dict) and t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            if isinstance(fn.get("name"), str):
                params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
                props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
                out[fn["name"]] = sorted(props)
    return out


def _anchor(messages, needle):
    """Index of the last user message containing `needle`, or None."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user" and needle in content_text(m.get("content")):
            return i
    return None


def _tool_results(messages):
    return [(i, m.get("tool_call_id"), content_text(m.get("content"))) for i, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") == "tool"]


def _assistant_command(messages, call_id):
    """The `command` argument of the assistant tool call with `call_id`, or None. Used to check that the
    execution-only marker is NOT in the command text (round-2 review)."""
    for m in messages:
        if not (isinstance(m, dict) and m.get("role") == "assistant"):
            continue
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict) and same_call(tc.get("id"), call_id):
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    return None
                return args.get("command") if isinstance(args, dict) else None
    return None


def _answered_after(messages, index):
    """True if an assistant text message without tool calls follows `index` (the step was already answered)."""
    for m in messages[index + 1:]:
        if isinstance(m, dict) and m.get("role") == "assistant" and not m.get("tool_calls") \
                and content_text(m.get("content")).strip():
            return True
    return False


def _completion_wake_after(messages, index):
    """True if a message after `index` carries the platform completion marker (an exec-exit wake)."""
    for m in messages[index + 1:]:
        if isinstance(m, dict) and COMPLETION_MARKER in content_text(m.get("content")):
            return True
    return False


def decide(body):
    """Choose the next scripted step from the request body alone. Returns a dict that is also logged.

    The anchor is the last user message carrying the scenario's prompt; only tool results after it count.
      A  no tool result yet: plant the scenario's first call (exec echo, or the background canary).
      H  hold scenario, the canary's result reports a running session: plant the foreground hold exec.
      K  kill scenario, no tool result yet: process kill of the latest running session found in the history.
      B  the expected result for the stand-in's own call id arrived with the expected content: final message.
      W  a platform-initiated turn after the scripted turn was already answered (for example an exec-completion
         wake). It is an OBSERVATION, never scored, and is answered with plain text (round-2 review).
      U  anything else, with a reason (unknown scenario, missing tool, missing prompt or tag, an error or
         unexpected result, or another call id).
    """
    info = {"decision": "U", "reason": "", "scenario": None, "tag": None, "tools": [], "exec_declared": False,
            "exec_background_param": False, "process_declared": False, "tool_search_controls": False,
            "prompt_present": False, "tool_result_call_ids": [], "tool_result": None, "bg_session_id": None,
            "bg_pid": None, "kill_session_id": None, "kill_reported_session_id": None,
            "marker_in_result": None, "marker_in_command": None}
    if not isinstance(body, dict):
        info["reason"] = "body is not a JSON object"
        return info
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    tools = _function_tools(body.get("tools") if isinstance(body.get("tools"), list) else [])
    scenario = body.get("model")
    info["scenario"] = scenario
    info["tools"] = sorted(tools)
    info["exec_declared"] = TOOL_NAME in tools
    info["exec_background_param"] = "background" in tools.get(TOOL_NAME, [])
    info["process_declared"] = PROCESS_TOOL in tools
    info["tool_search_controls"] = any(n in tools for n in ("tool_search", "tool_describe", "tool_call"))
    results = _tool_results(messages)
    info["tool_result_call_ids"] = [r[1] for r in results]

    def fail(reason):
        info["decision"], info["reason"] = "U", reason
        return info

    def wake(reason):
        info["decision"], info["reason"] = "W", reason
        return info

    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        return fail("unknown scenario (model) {!r}".format(scenario))
    anchor = _anchor(messages, KILL_PROMPT if scenario == SCENARIO_KILL else PROMPT)
    info["prompt_present"] = anchor is not None
    if anchor is None:
        return fail("scripted prompt not found in a user message")
    info["tag"] = tag_from_text(content_text(messages[anchor].get("content")))
    if _completion_wake_after(messages, anchor):
        return wake("platform completion-exit wake ({})".format(COMPLETION_MARKER))
    after = [r for r in results if r[0] > anchor]

    if not after:
        if _answered_after(messages, anchor):
            return wake("no new tool result after an answered turn (platform wake)")
        if scenario == SCENARIO_KILL:
            if not info["process_declared"]:
                return fail("process tool not declared")
            running = [parse_running(r[2]) for r in results if r[0] < anchor and parse_running(r[2])]
            if not running:
                return fail("no background session ('Command still running') in the history")
            info["kill_session_id"] = running[-1][0]
            info["decision"], info["reason"] = "K", "kill the latest running session in the history"
            return info
        if not info["exec_declared"]:
            return fail("exec tool not declared; tool_search controls present (set tools.toolSearch:false)"
                        if info["tool_search_controls"] else "exec tool not declared")
        if scenario in (SCENARIO_CANARY, SCENARIO_HOLD):
            if not info["exec_background_param"]:
                return fail("exec declared without a background parameter (process control not authorized?)")
            if not info["process_declared"]:
                return fail("process tool not declared, so background:true would run synchronously")
            if info["tag"] is None:
                return fail("no canary tag (tag=stopcheck-f-<nonce>-<arm>) in the prompt")
        info["decision"], info["reason"] = "A", "first request: plant the scripted call"
        return info

    idx, call_id, text = after[-1]
    info["tool_result"] = {"id": call_id, "excerpt": text[:EXCERPT_CHARS]}
    if _answered_after(messages, idx):
        return wake("tool result after an answered turn (platform wake)")
    info["tool_call_id_rewritten"] = call_id not in (CALL_ID, CALL_ID_HOLD, CALL_ID_KILL)
    if scenario == SCENARIO_ECHO:
        if not same_call(call_id, CALL_ID):
            return fail("tool result for another call id {!r}".format(call_id))
        command = _assistant_command(messages, CALL_ID)
        info["marker_in_result"] = MARKER in text
        if command is None:
            # Round-3 review: a command that cannot be found makes "marker not in the command" unknown, not true.
            return fail("the assistant tool call {} or its command cannot be found in the history, so the "
                        "marker-not-in-command check cannot be made".format(CALL_ID))
        info["marker_in_command"] = MARKER in command
        if not info["marker_in_result"]:
            return fail("echo tool result lacks the execution-only marker")
        if info["marker_in_command"]:
            return fail("the marker is in the command text, so the result does not prove execution")
        info["decision"], info["reason"] = "B", "echo result carries the execution-only marker"
        return info
    if scenario in (SCENARIO_CANARY, SCENARIO_HOLD):
        if scenario == SCENARIO_HOLD and same_call(call_id, CALL_ID_HOLD):
            info["decision"], info["reason"] = "B", "hold result received"
            return info
        if not same_call(call_id, CALL_ID):
            return fail("tool result for another call id {!r}".format(call_id))
        running = parse_running(text)
        if not running:
            return fail("background exec result does not report a running session")
        info["bg_session_id"], info["bg_pid"] = running
        if scenario == SCENARIO_HOLD:
            if info["tag"] is None:
                return fail("no canary tag in the prompt")
            info["decision"], info["reason"] = "H", "canary running: plant the foreground hold"
        else:
            info["decision"], info["reason"] = "B", "canary reported running"
        return info
    # kill scenario
    if not same_call(call_id, CALL_ID_KILL):
        return fail("tool result for another call id {!r}".format(call_id))
    reported = parse_kill_reported(text)
    if not reported:
        return fail("process kill result does not report the kill")
    info["kill_reported_session_id"] = reported
    info["decision"], info["reason"] = "B", "process kill reported"
    return info


def response_for(info):
    """The SSE bytes for a decision."""
    d = info["decision"]
    if d == "A":
        return stream_bytes(call=exec_call_arguments(info["scenario"], info["tag"]))
    if d == "H":
        return stream_bytes(call=exec_call_arguments("H", info["tag"]))
    if d == "K":
        return stream_bytes(call=kill_call_arguments(info["kill_session_id"]))
    if d == "B":
        return stream_bytes(text=FINAL_TEXT)
    if d == "W":
        return stream_bytes(text=WAKE_TEXT)
    return stream_bytes(text=UNEXPECTED_TEXT)


# ---------------------------------------------------------------- HTTP

def _clock_raw():
    try:
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    except AttributeError:  # not Linux; the spike itself runs on Linux
        return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StopCheckStandInF/2"
    sys_version = ""

    def _read_body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            parts, total = [], 0
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(parts)
                total += size
                if total > MAX_BODY_BYTES:
                    raise ValueError("body too large")
                parts.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _headers_for_log(self):
        out = {}
        for name, value in self.headers.items():
            low = name.lower()
            out[low] = "<present>" if any(w in low for w in SECRET_HEADER_WORDS) else value
        return out

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _record(self, body=None, **fields):
        srv = self.server
        url = urlsplit(self.path)
        with srv.lock:
            srv.count += 1
            n = srv.count
        rec = {"n": n, "t_wall": time.time(), "t_mono_raw": _clock_raw(), "t_mono": time.monotonic(),
               "client": "{}:{}".format(*self.client_address[:2]), "method": self.command, "path": url.path,
               "query": url.query, "http_version": self.request_version, "headers": self._headers_for_log(),
               "authorization_present": self.headers.get("Authorization") is not None,
               "content_encoding": self.headers.get("Content-Encoding")}
        if body is not None:
            name = os.path.join("bodies", "{:04d}.json".format(n))
            with open(os.path.join(srv.log_dir, name), "wb") as f:
                f.write(body)
            rec.update(body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest(), body_file=name)
        rec.update(fields)
        return rec

    def _log(self, rec, code):
        rec["status"] = code
        with self.server.lock, open(os.path.join(self.server.log_dir, "requests.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    def _finish(self, rec, code, data, ctype, save_stream=False):
        if save_stream:
            name = os.path.join("streams", "{:04d}.sse".format(rec["n"]))
            with open(os.path.join(self.server.log_dir, name), "wb") as f:
                f.write(data)
            rec.update(stream_file=name, stream_bytes=len(data))
        self._log(rec, code)
        self._send(code, data, ctype)

    def _json(self, obj):
        return json.dumps(obj).encode("utf-8")

    def do_GET(self):  # noqa: N802
        rec = self._record()
        if urlsplit(self.path).path == "/v1/models":
            models = {"object": "list", "data": [{"id": "scripted", "object": "model", "owned_by": "stopcheck"}]}
            return self._finish(rec, 200, self._json(models), "application/json")
        rec["unexpected"] = True
        return self._finish(rec, 404, self._json({"error": "not found"}), "application/json")

    def do_POST(self):  # noqa: N802
        try:
            body = self._read_body()
        except ValueError as e:
            self.close_connection = True
            rec = self._record(unexpected=True, reason=str(e))
            return self._finish(rec, 413, self._json({"error": str(e)}), "application/json")
        encoding = (self.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding != "identity":
            rec = self._record(body, unexpected=True, reason="cannot decode Content-Encoding {!r}".format(encoding))
            return self._finish(rec, 415, self._json({"error": "unsupported Content-Encoding"}), "application/json")
        if urlsplit(self.path).path != "/v1/chat/completions":
            rec = self._record(body, unexpected=True, reason="unknown route")
            return self._finish(rec, 404, self._json({"error": "only POST /v1/chat/completions is implemented"}),
                                "application/json")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            rec = self._record(body, unexpected=True, reason="body is not JSON: {}".format(e))
            return self._finish(rec, 400, self._json({"error": "body is not JSON"}), "application/json")
        info = decide(parsed)
        info["unexpected"] = info["decision"] == "U"
        info["accept"] = self.headers.get("Accept")
        info["stream_requested"] = parsed.get("stream") if isinstance(parsed, dict) else None
        rec = self._record(body, **info)
        return self._finish(rec, 200, response_for(info), "text/event-stream", save_stream=True)

    def _unsupported(self):
        rec = self._record(unexpected=True, reason="unsupported method")
        self._finish(rec, 405, self._json({"error": "method not allowed"}), "application/json")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _unsupported  # noqa: N815

    def do_HEAD(self):  # noqa: N802
        self._log(self._record(unexpected=True, reason="unsupported method"), 405)
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Linux: rebind the port at once for the next run. Windows (tests only, port 0): SO_REUSEADDR there would let a
    # second listener share the port, so it stays off.
    allow_reuse_address = os.name != "nt"


class StandIn:
    """The stand-in as an object: start() serves in a background thread; stop() shuts it down and frees the port."""

    def __init__(self, log_dir, host="127.0.0.1", port=DEFAULT_PORT):
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("the stand-in listens on a loopback address only, not {!r}".format(host))
        self.log_dir, self.host, self.port = log_dir, host, port
        self._server = None
        self._thread = None

    def start(self):
        for sub in ("", "bodies", "streams"):
            os.makedirs(os.path.join(self.log_dir, sub), exist_ok=True)
        srv = _Server((self.host, self.port), _Handler)
        try:
            srv.log_dir, srv.lock, srv.count = self.log_dir, threading.Lock(), 0
            port = srv.server_address[1]
            with open(__file__, "rb") as f:
                own_hash = hashlib.sha256(f.read()).hexdigest()
            meta = {"started_wall": time.time(), "started_mono_raw": _clock_raw(), "pid": os.getpid(),
                    "host": self.host, "port": port, "script_sha256": own_hash, "prompt": PROMPT,
                    "kill_prompt": KILL_PROMPT, "scenarios": SCENARIOS,
                    "call_ids": [CALL_ID, CALL_ID_HOLD, CALL_ID_KILL],
                    "recorder": {"host": RECORDER_HOST, "port": RECORDER_PORT, "tag_pattern": TAG_RE.pattern}}
            with open(os.path.join(self.log_dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=1, sort_keys=True)
            self._thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                                            name="stopcheck-standin-f", daemon=True)
            self._thread.start()
        except BaseException:
            srv.server_close()  # do not keep the port bound when start() fails
            raise
        self._server, self.port = srv, port
        return self

    @property
    def base_url(self):
        return "http://{}:{}/v1".format(self.host, self.port)

    @property
    def request_count(self):
        """Requests received so far (the runner marks phase boundaries with it)."""
        if self._server is None:
            return 0
        with self._server.lock:
            return self._server.count

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(5)
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def read_log(log_dir, skipped=None):
    """The logged requests, in arrival order. A line that does not decode to a JSON object is skipped, never fatal
    (round-4 review: one bad line must not empty the whole list). When `skipped` is a list, each skipped line is
    appended to it as {"line_no", "partial_last_line", "error"}; a last line with no newline yet is a write still in
    progress."""
    path = os.path.join(log_dir, "requests.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        lines = f.read().split(b"\n")
    out = []
    for i, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
            if not isinstance(rec, dict):
                raise ValueError("not a JSON object")
        except ValueError as e:   # UnicodeDecodeError and JSONDecodeError are both ValueErrors
            if skipped is not None:
                skipped.append({"line_no": i, "partial_last_line": i == len(lines),
                                "error": "{}: {}".format(type(e).__name__, e)[:200]})
            continue
        out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, help="log folder (requests.jsonl, bodies/, streams/)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    standin = StandIn(args.dir, args.host, args.port).start()
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    print("stand-in listening on {}".format(standin.base_url), flush=True)
    while not done.wait(0.5):
        pass
    standin.stop()


if __name__ == "__main__":
    main()
