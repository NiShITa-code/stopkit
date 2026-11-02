#!/usr/bin/env python3
"""Condition D: Khan (2026, arXiv 2607.14166, s3.2) "cancellation orphan" probe, run on the
LangGraph Python library (no server). One trial per process; started by harness_d.py.

A one-node graph (START -> tool -> END) is run with graph.ainvoke() in an asyncio task.
The tool needs TOOL_S seconds. CANCEL_AFTER_S after the tool starts, the probe kills the
directly-killed control canary and cancels the task (task.cancel()) at the same moment,
then records when `await task` raises CancelledError, i.e. when the caller sees the
cancellation. The tool's effect is one append to an in-process event log (monotonic time,
thread name), as in Khan (s3.1), plus one ping to our recorder, both at the commit point.

  --variant sync : plain function with time.sleep(TOOL_S); LangGraph runs a sync node on
                   a worker thread under ainvoke (Khan's "Cancel: worker/async" row).
  --variant async: the same tool as a coroutine with await asyncio.sleep(TOOL_S)
                   (Khan's "Cancel: pure async" row, which he reports clean for LangGraph).

Prints one JSON object (this trial's in-process record) as the last line on stdout.
"""
import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from importlib import metadata
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

RECORDER = os.environ.get("PILOT_RECORDER_URL", "http://127.0.0.1:8765")
TOOL_S = 5.0
CANCEL_AFTER_S = 1.0
now = time.monotonic  # system-wide monotonic clock, the same one the recorder stamps with

EVENT_LOG: list[dict] = []  # in-process event log (Khan s3.1): appended at the commit point
_LOCK = threading.Lock()
_on_tool_start = None  # set in run(): wakes the caller when the tool has started


def ping(canary: str, trial: str, **extra) -> None:
    q = {"canary": canary, "trial": trial, "n": 0, "pid": os.getpid(), **{k: str(v) for k, v in extra.items()}}
    try:
        urllib.request.urlopen(f"{RECORDER}/ping?{urllib.parse.urlencode(q)}", timeout=2).read()
    except Exception:
        pass


def record(kind: str, trial: str) -> None:
    th = threading.current_thread()
    entry = {"kind": kind, "mono": now(), "thread": th.name, "is_main_thread": th is threading.main_thread()}
    with _LOCK:
        EVENT_LOG.append(entry)  # the in-process log entry (Khan's instrument)
    ping(kind, trial, thread=th.name)  # our recorder's copy, stamped on arrival


class S(TypedDict, total=False):
    trial: str
    done: bool


def tool_sync(state: S) -> dict:
    record("tool_start", state["trial"])
    _on_tool_start()
    time.sleep(TOOL_S)  # blocking work (stands in for e.g. a blocking HTTP call)
    record("effect", state["trial"])  # commit point
    return {"done": True}


async def tool_async(state: S) -> dict:
    record("tool_start", state["trial"])
    _on_tool_start()
    await asyncio.sleep(TOOL_S)  # the same wait, as a coroutine
    record("effect", state["trial"])  # commit point
    return {"done": True}


def build(variant: str):
    g = StateGraph(S)
    g.add_node("tool", tool_sync if variant == "sync" else tool_async)
    g.add_edge(START, "tool")
    g.add_edge("tool", END)
    return g.compile()


async def run(args) -> dict:
    global _on_tool_start
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    _on_tool_start = lambda: loop.call_soon_threadsafe(started.set)  # noqa: E731 (works from any thread)
    graph = build(args.variant)
    R = {"trial": args.trial, "variant": args.variant, "tool_s": TOOL_S, "cancel_after_s": CANCEL_AFTER_S,
         "watch_s": args.watch, "probe_pid": os.getpid()}
    R["t_invoke"] = now()
    task = asyncio.create_task(graph.ainvoke({"trial": args.trial}), name="run")
    try:
        await asyncio.wait_for(started.wait(), 15)
    except asyncio.TimeoutError:
        R["error"] = "tool did not start within 15 s"
    R["t_tool_start_seen"] = now()
    await asyncio.sleep(CANCEL_AFTER_S)

    # ---- the stop: kill the directly-killed control and cancel the run at the same moment
    R["t_cancel"] = t_cancel = now()
    if args.control_kill_pid:
        try:
            os.kill(args.control_kill_pid, signal.SIGKILL)
            R["control_kill_signalled"] = True
        except ProcessLookupError:
            R["control_kill_signalled"] = False
    R["t_control_kill"] = now()
    task.cancel()
    try:
        await task
        R["caller_saw"] = "completed (no cancellation)"
    except asyncio.CancelledError:
        R["caller_saw"] = "CancelledError"
    except Exception as e:  # recorded as data
        R["caller_saw"] = f"{type(e).__name__}: {e}"
    R["t_observed"] = now()  # the caller has now seen the outcome of the cancel

    # ---- watch: keep the process (and any orphaned worker thread) alive
    await asyncio.sleep(max(0.0, t_cancel + args.watch - now()))
    R["t_end_watch"] = now()
    with _LOCK:
        R["event_log"] = list(EVENT_LOG)
    R["threads_alive_at_end"] = [t.name for t in threading.enumerate()]
    R["versions"] = {p: metadata.version(p) for p in ("langgraph", "langgraph-checkpoint", "langchain-core")}
    R["python"] = sys.version.split()[0]
    return R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["sync", "async"], required=True)
    ap.add_argument("--trial", required=True)
    ap.add_argument("--control-kill-pid", type=int, default=0)
    ap.add_argument("--watch", type=float, default=90.0)
    args = ap.parse_args()
    R = asyncio.run(run(args))
    print(json.dumps(R), flush=True)


if __name__ == "__main__":
    main()
