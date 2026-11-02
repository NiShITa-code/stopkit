#!/usr/bin/env python3
"""Experiment C workload: the Temporal workflows JobC and ReadyC, the activity variants, and the worker and
client entry points (DESIGN-expC.md section 3; RULES-expC.md C.1, C.2).

Every planted item is a benign canary: its only effect is a POST of one JSON record to the local stand-in
receiver (receiver_c.py), which logs it. Nothing here touches anything outside the trial's loopback.

Written against temporalio 1.34.0 as installed in /root/temporal-venv (the paths below are relative to its
site-packages/temporalio/):
  - workflow.execute_activity / execute_local_activity and their cancellation_type, heartbeat_timeout,
    retry_policy and start_to_close_timeout parameters: workflow/_activities.py:482-500, 1524-1538;
  - ActivityCancellationType (TRY_CANCEL, WAIT_CANCELLATION_COMPLETED, ABANDON): workflow/_activities.py:72-83;
  - workflow.sleep: workflow/_context.py:991-1014;
  - activity.info, heartbeat, cancellation_details: activity.py:107-129, 306-333; flags 173-194;
  - RetryPolicy fields: common.py:37-57; WorkflowIDConflictPolicy: common.py:131-142;
  - Worker(identity=..., graceful_shutdown_timeout default zero), Worker.config, Worker.shutdown:
    worker/_worker.py:101-140, 703-716, 917-928;
  - Client.connect, start_workflow (execution_timeout, id_conflict_policy), handle.result_run_id:
    client/_client.py:135-159, 530-560; client/_workflow.py:158-170.

Import rules (offline tests must import this file without temporalio installed):
  - temporalio.client and temporalio.worker are imported lazily, inside the functions that need them;
  - temporalio.activity, temporalio.workflow and temporalio.common are imported in one guarded block at module
    level, because the workflow sandbox imports a workflow class by its module and class name
    (worker/workflow_sandbox/_runner.py:131-146), so the classes must be module attributes. Without temporalio
    the decorators below are no-ops and the module still imports;
  - module-level code does nothing but define names: the sandbox re-imports this module and restricts most os
    calls at workflow run time (worker/workflow_sandbox/_restrictions.py:520-536, 641-655). The workload
    parameters reach the workflow as its input, never from the environment.
  - Run as a script, this file re-imports itself as `jobs_c`, so the workflow classes' module is `jobs_c` and not
    `__main__` (the sandbox does not import `__main__`, _runner.py:133-137).

    python3 jobs_c.py worker --address 127.0.0.1:7833 --task-queue Q --identity ID --log PATH
    python3 jobs_c.py start  --address 127.0.0.1:7833 --spec '[{"workflow": "JobC", ...}]'
    python3 jobs_c.py ready  --address 127.0.0.1:7833 --task-queue Q --workflow-id ID --params '{...}'
"""
import asyncio
import json
import os
import sys
import time
from datetime import timedelta

try:  # guarded: see the module docstring
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy
    HAVE_TEMPORAL = True
except ImportError:  # offline tests
    activity = workflow = RetryPolicy = None
    HAVE_TEMPORAL = False

SCHEMA = 1
VARIANTS = ("HB", "NH", "LA")
CANCELLATION_TYPES = ("TRY_CANCEL", "WAIT_CANCELLATION_COMPLETED")
ROLES = ("target", "twin", "ptwin", "ready")
KINDS = ("accepted", "progress", "final", "ready", "tick")
POST_TIMEOUT_S = 2.0          # K8
MAX_BODY = 64 * 1024

# DESIGN-expC section 3 (full mode) and RULES-expC C.10 (FAST, smoke only).
WORKLOAD = {
    "full": {"timer_s": 30.0, "n_steps": 20, "step_s": 1.0, "hb_timeout_s": 5.0, "work_stc_s": 60.0,
             "announce_stc_s": 10.0, "ready_stc_s": 10.0, "execution_timeout_s": 900.0,
             "retry": {"initial_s": 1.0, "coefficient": 2.0, "max_s": 10.0, "max_attempts": 3}},
    "fast": {"timer_s": 6.0, "n_steps": 8, "step_s": 0.5, "hb_timeout_s": 2.0, "work_stc_s": 60.0,
             "announce_stc_s": 10.0, "ready_stc_s": 10.0, "execution_timeout_s": 900.0,
             "retry": {"initial_s": 1.0, "coefficient": 2.0, "max_s": 10.0, "max_attempts": 3}},
}

# Set by the worker entry point in the worker process (never inside the sandbox).
LOG_PATH = None
IDENTITY = None


# ============================ pure helpers (offline-tested) ============================
def make_params(*, trial, role, variant, fast, receiver, cancellation_type="TRY_CANCEL", timer_s=None,
                n_steps=None, queue=None):
    """The workflow input: the workload parameters of one job (DESIGN-expC section 3)."""
    base = dict(WORKLOAD["fast" if fast else "full"])
    base["retry"] = dict(base["retry"])
    p = {"schema": SCHEMA, "trial": trial, "role": role, "variant": variant,
         "cancellation_type": cancellation_type, "receiver": receiver, "fast": bool(fast), "queue": queue}
    p.update(base)
    if timer_s is not None:
        p["timer_s"] = float(timer_s)
    if n_steps is not None:
        p["n_steps"] = int(n_steps)
    validate_params(p)
    return p


def validate_params(p):
    """Refuse a bad job before it is started: a ValueError raised inside a workflow fails the workflow task,
    which Temporal retries, so bad input would never close the run."""
    if p.get("role") not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if p.get("variant") not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}")
    if p.get("cancellation_type") not in CANCELLATION_TYPES:
        raise ValueError(f"cancellation_type must be one of {CANCELLATION_TYPES}")
    if p["variant"] == "LA" and p["cancellation_type"] != "TRY_CANCEL":
        raise ValueError("the first pass schedules the local activity with TRY_CANCEL only (K3)")
    host, _, port = str(p.get("receiver", "")).rpartition(":")
    if host != "127.0.0.1" or not port.isdigit():
        raise ValueError("receiver must be 127.0.0.1:<port> (trial loopback only)")
    for key in ("timer_s", "step_s", "hb_timeout_s", "work_stc_s", "announce_stc_s", "ready_stc_s"):
        if not (isinstance(p.get(key), (int, float)) and p[key] > 0):
            raise ValueError(f"{key} must be a positive number")
    if not (isinstance(p.get("n_steps"), int) and p["n_steps"] >= 1):
        raise ValueError("n_steps must be a positive integer")
    r = p.get("retry") or {}
    if not (r.get("max_attempts", 0) >= 1 and r.get("initial_s", 0) > 0 and r.get("coefficient", 0) >= 1):
        raise ValueError("retry policy must be bounded (K5)")
    return True


def effect_record(params, *, kind, step, exec_nonce, info=None, identity=None, pid=None, smono=None, swall=None):
    """One effect, as posted (DESIGN-expC section 3). `info` is a dict with the activity's identity fields."""
    if kind not in KINDS:
        raise ValueError(f"unknown effect kind {kind}")
    info = info or {}
    return {"trial": params.get("trial"), "role": params.get("role"), "variant": params.get("variant"),
            "workflow_id": info.get("workflow_id"), "run_id": info.get("run_id"),
            "activity_type": info.get("activity_type"), "activity_id": info.get("activity_id"),
            "attempt": info.get("attempt"), "is_local": info.get("is_local"), "exec_nonce": exec_nonce,
            "step": step, "kind": kind, "identity": identity, "pid": pid, "queue": params.get("queue"),
            "smono": smono, "swall": swall}


def build_post(host, port, path, body):
    """An HTTP/1.1 POST with Connection: close (one effect per connection)."""
    if not isinstance(body, bytes):
        body = json.dumps(body, separators=(",", ":")).encode()
    head = (f"POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    return head + body


def parse_status(first_line):
    """Status code of an HTTP status line; None if it is not one."""
    try:
        parts = first_line.decode("latin-1").split()
        return int(parts[1]) if len(parts) >= 2 and parts[0].startswith("HTTP/") else None
    except (ValueError, AttributeError):
        return None


def info_dict(info):
    """The activity identity fields used in an effect record (activity.py:107-129)."""
    return {"workflow_id": info.workflow_id, "run_id": info.workflow_run_id, "activity_type": info.activity_type,
            "activity_id": info.activity_id, "attempt": info.attempt, "is_local": info.is_local}


def cancellation_flags(details):
    """ActivityCancellationDetails as a dict (activity.py:173-194); None if no details were set."""
    if details is None:
        return None
    return {k: bool(getattr(details, k, False))
            for k in ("not_found", "cancel_requested", "paused", "reset", "timed_out", "worker_shutdown")}


OPTION_KEYS = ("task_queue", "identity", "max_concurrent_activities", "max_concurrent_local_activities",
               "max_concurrent_workflow_tasks", "max_cached_workflows", "graceful_shutdown_timeout",
               "sticky_queue_schedule_to_start_timeout", "max_heartbeat_throttle_interval",
               "default_heartbeat_throttle_interval", "disable_eager_activity_execution",
               "max_eager_activity_reservations_per_workflow_task", "no_remote_activities")


def options_record(cfg):
    """The worker's effective options (K11, K12), as strings so the record is plain JSON."""
    out = {}
    for k in OPTION_KEYS:
        v = cfg.get(k) if isinstance(cfg, dict) else None
        out[k] = v if isinstance(v, (bool, int, float, str)) or v is None else str(v)
    return out


def wlog(event, **fields):
    """Our code's view from inside the worker (diagnostic only, DESIGN-expC section 2)."""
    rec = {"event": event, "smono": time.monotonic(), "swall": time.time(), "pid": os.getpid(),
           "identity": IDENTITY}
    rec.update(fields)
    if LOG_PATH:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
            f.flush()
    return rec


# ============================ effects (activities only, K7) ============================
class EffectPostError(RuntimeError):
    """A POST that did not get a 200: the activity fails (K8)."""


async def _post(host, port, body):
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(build_post(host, port, "/effect", body))
        await writer.drain()
        first = await reader.readline()
        await reader.read(MAX_BODY)            # drain the reply; the server closes the connection
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass
    status = parse_status(first)
    if status != 200:
        raise EffectPostError(f"receiver answered {status}")


class WorkProgress:
    """Where the work activity is (pre-freeze review; K8): its phase (posting n, heartbeat n, sleeping after n, posting
    final), the effect in flight (step, kind and sender stamp, until its reply) and each POST's reply stamp. Kept in
    memory and written only in the activity's last log line (activity_done, activity_cancelled or activity_error), so
    the worker does no extra file I/O per step. A cancellation is raised only at an await (the POST or the sleep), so an
    acknowledgement in "posting n" means step n's POST was cut off, possibly after the receiver logged it."""

    def __init__(self):
        self.phase, self.inflight, self.replies = "starting", None, []

    def posting(self, step, kind):
        self.phase = f"posting {step}" if kind == "progress" else f"posting {kind}"
        self.inflight = {"step": step, "kind": kind, "smono": None}

    def sent(self, smono):
        if self.inflight is not None:
            self.inflight["smono"] = smono

    def replied(self, mono):
        if self.inflight is not None:
            self.replies.append([self.inflight["step"], self.inflight["kind"], self.inflight["smono"], mono])
        self.inflight = None

    def heartbeat(self, step):
        self.phase = f"heartbeat {step}"

    def sleeping(self, step):
        self.phase = f"sleeping after {step}"

    def snapshot(self):
        return {"phase": self.phase, "inflight": dict(self.inflight) if self.inflight else None,
                "replies": [list(r) for r in self.replies]}


async def post_effect(params, rec, timeout=POST_TIMEOUT_S, progress=None):
    """Post one effect over an asyncio stream, so a cancellation can interrupt it (K8). `progress` (WorkProgress) learns
    the sender stamp before the POST and the reply stamp after it."""
    host, _, port = params["receiver"].rpartition(":")
    rec["smono"], rec["swall"] = time.monotonic(), time.time()
    if progress is not None:
        progress.sent(rec["smono"])
    await asyncio.wait_for(_post(host, int(port), rec), timeout)
    if progress is not None:
        progress.replied(time.monotonic())
    return rec


def _defn_activity(fn):
    return activity.defn(fn) if HAVE_TEMPORAL else fn


def _defn_workflow(cls):
    return workflow.defn(cls) if HAVE_TEMPORAL else cls


def _run_method(fn):
    return workflow.run(fn) if HAVE_TEMPORAL else fn


async def _one_effect(params, kind, step=None):
    info = info_dict(activity.info())
    nonce = os.urandom(8).hex()
    rec = effect_record(params, kind=kind, step=step, exec_nonce=nonce, info=info, identity=IDENTITY,
                        pid=os.getpid())
    await post_effect(params, rec)
    return {"exec_nonce": nonce}


@_defn_activity
async def announce(params: dict) -> dict:
    """Stage A: one `accepted` effect."""
    return await _one_effect(params, "accepted")


@_defn_activity
async def ready(params: dict) -> dict:
    """Readiness and capability job: one `ready` effect."""
    return await _one_effect(params, "ready")


async def _work(params, heartbeat):
    """Stage W: `progress` for steps 1..N on a monotonic step clock, then `final` (DESIGN-expC section 3).
    HB heartbeats once per step with no details (K4); a retried execution starts again at step 1.
    K14: a cancellation is never caught for good: the handler logs it (with the phase and the effect in flight) and
    re-raises asyncio.CancelledError, with no cleanup and no final effect, so whether the activity stops after the
    cancellation reaches it is decided here, by our code."""
    info = info_dict(activity.info())
    nonce = os.urandom(8).hex()
    n, step_s = int(params["n_steps"]), float(params["step_s"])
    t0 = time.monotonic()
    wlog("activity_start", exec_nonce=nonce, heartbeat=heartbeat, **info)
    step = 0
    progress = WorkProgress()
    try:
        for step in range(1, n + 1):
            rec = effect_record(params, kind="progress", step=step, exec_nonce=nonce, info=info,
                                identity=IDENTITY, pid=os.getpid())
            progress.posting(step, "progress")
            await post_effect(params, rec, progress=progress)
            if heartbeat:
                progress.heartbeat(step)
                activity.heartbeat()
            progress.sleeping(step)
            await asyncio.sleep(max(0.0, t0 + step * step_s - time.monotonic()))
        rec = effect_record(params, kind="final", step=n, exec_nonce=nonce, info=info, identity=IDENTITY,
                            pid=os.getpid())
        progress.posting(n, "final")
        await post_effect(params, rec, progress=progress)
    except asyncio.CancelledError:
        wlog("activity_cancelled", exec_nonce=nonce, last_step=step,
             flags=cancellation_flags(activity.cancellation_details()), **progress.snapshot(), **info)
        raise
    except BaseException as e:  # noqa: BLE001 - recorded, then re-raised
        wlog("activity_error", exec_nonce=nonce, last_step=step, error=f"{type(e).__name__}: {e}",
             **progress.snapshot(), **info)
        raise
    wlog("activity_done", exec_nonce=nonce, **progress.snapshot(), **info)
    return {"exec_nonce": nonce, "steps": n}


@_defn_activity
async def work_hb(params: dict) -> dict:
    return await _work(params, heartbeat=True)


@_defn_activity
async def work_nh(params: dict) -> dict:
    return await _work(params, heartbeat=False)


@_defn_activity
async def work_la(params: dict) -> dict:
    return await _work(params, heartbeat=False)


ACTIVITIES = (announce, ready, work_hb, work_nh, work_la)


# ============================ workflows ============================
def _retry_policy(params):
    r = params["retry"]
    return RetryPolicy(initial_interval=timedelta(seconds=r["initial_s"]), backoff_coefficient=r["coefficient"],
                       maximum_interval=timedelta(seconds=r["max_s"]), maximum_attempts=int(r["max_attempts"]))


@_defn_workflow
class JobC:
    """Stage A, stage T, stage W, then return (DESIGN-expC section 3). K2: cancellation is never caught, so a
    cancelled exception that reaches this function closes the run CANCELED (worker/_workflow_instance.py:2852-2862)."""

    @_run_method
    async def run(self, params: dict) -> dict:
        rp = _retry_policy(params)
        await workflow.execute_activity(announce, params, activity_id="announce", retry_policy=rp,
                                        start_to_close_timeout=timedelta(seconds=params["announce_stc_s"]))
        await workflow.sleep(params["timer_s"])
        ct = workflow.ActivityCancellationType[params["cancellation_type"]]
        stc = timedelta(seconds=params["work_stc_s"])
        variant = params["variant"]
        if variant == "HB":
            await workflow.execute_activity(work_hb, params, activity_id="work", start_to_close_timeout=stc,
                                            heartbeat_timeout=timedelta(seconds=params["hb_timeout_s"]),
                                            retry_policy=rp, cancellation_type=ct)
        elif variant == "NH":
            await workflow.execute_activity(work_nh, params, activity_id="work", start_to_close_timeout=stc,
                                            retry_policy=rp, cancellation_type=ct)
        else:
            await workflow.execute_local_activity(work_la, params, activity_id="work", start_to_close_timeout=stc,
                                                  retry_policy=rp, cancellation_type=ct)
        return {"role": params["role"], "variant": variant}


@_defn_workflow
class ReadyC:
    """One `ready` effect from an ordinary activity on this queue."""

    @_run_method
    async def run(self, params: dict) -> dict:
        await workflow.execute_activity(ready, params, activity_id="ready", retry_policy=_retry_policy(params),
                                        start_to_close_timeout=timedelta(seconds=params["ready_stc_s"]))
        return {"role": params["role"], "queue": params.get("queue")}


WORKFLOWS = {"JobC": JobC, "ReadyC": ReadyC}


# ============================ entry points (lazy imports) ============================
def _stamp():
    return {"mono": time.monotonic(), "wall": time.time()}


def worker_main(args):
    import signal
    from temporalio.client import Client
    from temporalio.worker import Worker
    global LOG_PATH, IDENTITY
    LOG_PATH, IDENTITY = args.log, args.identity

    async def run():
        client = await Client.connect(args.address, identity=args.identity)
        w = Worker(client, task_queue=args.task_queue, workflows=[JobC, ReadyC], activities=list(ACTIVITIES),
                   identity=args.identity)      # every other option at its SDK default (K11, K12)
        wlog("worker_start", task_queue=args.task_queue, options=options_record(w.config(active_config=True)))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(s, stop.set)
        run_task = asyncio.create_task(w.run())
        stop_task = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if run_task in done:
            stop_task.cancel()
            exc = run_task.exception()
            wlog("worker_stopped_without_signal", error=repr(exc) if exc else None)
            return 1 if exc else 0
        wlog("sigterm_received")
        await w.shutdown()                     # K12: graceful timeout is the SDK default (zero)
        wlog("worker_shutdown_complete")
        return 0

    return asyncio.run(run())


def start_main(args):
    from temporalio.client import Client
    from temporalio.common import WorkflowIDConflictPolicy
    specs = json.loads(args.spec)
    for s in specs:
        if s.get("workflow") not in WORKFLOWS:
            raise SystemExit(f"unknown workflow {s.get('workflow')}")
        validate_params(s["params"])

    async def one(client, s):
        out = {"workflow_id": s["workflow_id"], "role": s["params"]["role"], "t_send": _stamp()}
        try:
            h = await client.start_workflow(
                s["workflow"], s["params"], id=s["workflow_id"], task_queue=s["task_queue"],
                execution_timeout=timedelta(seconds=s["params"]["execution_timeout_s"]),
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL)
            out.update(run_id=h.result_run_id, ok=True)
        except Exception as e:  # noqa: BLE001 - reported, never hidden
            out.update(ok=False, error=f"{type(e).__name__}: {e}")
        out["t_reply"] = _stamp()
        return out

    async def run():
        client = await Client.connect(args.address)
        return await asyncio.gather(*(one(client, s) for s in specs))

    started = asyncio.run(run())
    print(json.dumps({"started": started}), flush=True)
    return 0 if all(s.get("ok") for s in started) else 1


def ready_main(args):
    from temporalio.client import Client
    from temporalio.common import WorkflowIDConflictPolicy
    params = json.loads(args.params)
    validate_params(params)

    async def run():
        out = {"workflow_id": args.workflow_id, "task_queue": args.task_queue, "t_send": _stamp()}
        try:
            client = await Client.connect(args.address)
            h = await client.start_workflow("ReadyC", params, id=args.workflow_id, task_queue=args.task_queue,
                                            execution_timeout=timedelta(seconds=max(args.timeout * 2, 60)),
                                            id_conflict_policy=WorkflowIDConflictPolicy.FAIL)
            out["run_id"] = h.result_run_id
            out["t_reply"] = _stamp()
            out["result"] = await asyncio.wait_for(h.result(), args.timeout)
            out["ok"] = True
        except Exception as e:  # noqa: BLE001
            out.update(ok=False, error=f"{type(e).__name__}: {e}")
        out["t_done"] = _stamp()
        return out

    out = asyncio.run(run())
    print(json.dumps(out), flush=True)
    return 0 if out.get("ok") else 1


def parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Experiment C worker and client (no service is started by import).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("worker")
    w.add_argument("--address", required=True)
    w.add_argument("--task-queue", required=True)
    w.add_argument("--identity", required=True)
    w.add_argument("--log", required=True)
    s = sub.add_parser("start")
    s.add_argument("--address", required=True)
    s.add_argument("--spec", required=True, help="JSON list of {workflow, workflow_id, task_queue, params}")
    r = sub.add_parser("ready")
    r.add_argument("--address", required=True)
    r.add_argument("--task-queue", required=True)
    r.add_argument("--workflow-id", required=True)
    r.add_argument("--params", required=True)
    r.add_argument("--timeout", type=float, default=30.0)
    return ap.parse_args(argv)


def main(argv=None):
    if not HAVE_TEMPORAL:
        raise SystemExit("temporalio is not importable here; run with /root/temporal-venv/bin/python")
    args = parse_args(argv)
    return {"worker": worker_main, "start": start_main, "ready": ready_main}[args.cmd](args)


if __name__ == "__main__":
    # Re-import under the real module name so the workflow classes' module is `jobs_c` (see the docstring).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jobs_c
    sys.exit(jobs_c.main())
