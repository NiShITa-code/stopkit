#!/usr/bin/env python3
"""Experiment C scorer (RULES-expC.md C.5, C.6, C.9). Reads one trial's record.json, receiver.jsonl and worker
logs and writes the scored rows (scored_c.jsonl) and a summary (summary_c.md) outside the trial folder: by default to
<results>/derived-posthoc/<trial folder>/, or to --out. Reports are derived files; a raw trial folder is only read.
The runner exports every attempt itself (run_expC.export_attempt).

It never runs anything and never reads predictions (C.8: predictions are compared with scored results only after
scoring). Every judgement is about the declared set (C.2) unless a row says otherwise. Missing evidence, failed
reads and failed requests are UNKNOWN, never "stopped" (C.5). UNKNOWN takes precedence over a verdict that would
need the missing evidence, as in n8n/analyze_b.py.

The FIELD MAP below is the one table that maps the CLI's JSON onto the names the rules use (C.1). It accepts the
protobuf JSON field names (lowerCamelCase, as the installed SDK's message stubs define them, for example
api/workflowservice/v1/request_response_pb2.pyi:4791-4821) and their snake_case forms, and both enum styles: the
prefixed upper-case form (WORKFLOW_EXECUTION_STATUS_CANCELED, EVENT_TYPE_WORKFLOW_EXECUTION_STARTED), which CLI 1.9.1's
--output json writes (seen in every feasibility record, T25), and the PascalCase form (Canceled) of the UI's history
JSON, which client/_helpers.py:46-70, 147-162 converts between. It is checked on pilot output before any screen (C.10).

    python3 analyze_c.py <trial-folder> [<trial-folder> ...] [--out DIR] [--overwrite]
"""
import glob
import json
import math
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import clockwall_c as clockwall  # noqa: E402  (the harness's wall/monotonic rule, recomputed here, C.9)

UNK = "UNKNOWN"
# Record schema 2 (pre-freeze batch of 13 Oct): from it the harness's wall/monotonic receipt and this scorer's own
# recomputation of it from receiver.jsonl are mandatory (C.9), and so are the declared host profile and the start host
# check (C.0). Earlier records keep their rules; for them the recomputation is a labelled post hoc diagnostic.
EVIDENCE_SCHEMA = 2
HOST_PROFILES = ("wsl", "linux-vm")          # harness_c.HOST_PROFILES (C.0)
FAC, NO_FAC = "false all-clear", "no false all-clear"
CLOSED = {"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT", "CONTINUED_AS_NEW"}
CLOSING_EVENTS = {"WorkflowExecutionCompleted", "WorkflowExecutionFailed", "WorkflowExecutionCanceled",
                  "WorkflowExecutionTerminated", "WorkflowExecutionTimedOut", "WorkflowExecutionContinuedAsNew"}
DECLARED_KINDS = {"accepted", "progress", "final"}
ACTIVE_WINDOW_S, QUIET_WINDOW_S, GAP_S = 2.0, 5.0, 2.0      # C.6 M5; C.9 receiver
CLOCK_ORDER_FLAG_S = 1.0                                       # C.9 clock order
REQUEST_ACTIONS = ("cancel", "terminate")

# ---- FIELD MAP (C.1): rule name -> accepted JSON keys, first match wins ----
FIELD_MAP = {
    "describe.info": ("workflowExecutionInfo", "workflow_execution_info"),
    "info.status": ("status",),
    "info.history_length": ("historyLength", "history_length"),
    "info.close_time": ("closeTime", "close_time"),
    "describe.pending_activities": ("pendingActivities", "pending_activities"),
    "describe.pending_workflow_task": ("pendingWorkflowTask", "pending_workflow_task"),
    "describe.extended": ("workflowExtendedInfo", "workflow_extended_info"),
    "extended.cancel_requested": ("cancelRequested", "cancel_requested"),
    "pending.activity_id": ("activityId", "activity_id"),
    "pending.activity_type": ("activityType", "activity_type"),
    "pending.state": ("state",),
    "pending.attempt": ("attempt",),
    "pending.last_heartbeat_time": ("lastHeartbeatTime", "last_heartbeat_time"),
    "pending.last_started_time": ("lastStartedTime", "last_started_time"),
    "pending.last_failure": ("lastFailure", "last_failure"),
    "pending.last_worker_identity": ("lastWorkerIdentity", "last_worker_identity"),
    "wft.state": ("state",),
    "wft.attempt": ("attempt",),
    "wft.scheduled_time": ("scheduledTime", "scheduled_time"),
    "wft.original_scheduled_time": ("originalScheduledTime", "original_scheduled_time"),
    "wft.started_time": ("startedTime", "started_time"),
    "history.events": ("events",),
    "event.id": ("eventId", "event_id"),
    "event.type": ("eventType", "event_type"),
    "event.time": ("eventTime", "event_time"),
    "attrs.activity_id": ("activityId", "activity_id"),
    "attrs.attempt": ("attempt",),
    "attrs.scheduled_event_id": ("scheduledEventId", "scheduled_event_id"),
    "attrs.failure": ("failure",),                                    # MarkerRecordedEventAttributes.failure
    "failure.canceled": ("canceledFailureInfo", "canceled_failure_info"),     # api/failure/v1/message_pb2.pyi:443
    "failure.cause": ("cause",),
    "list.executions": ("executions", "workflowExecutions"),
    "list.execution": ("execution",),
    "list.workflow_id": ("workflowId", "workflow_id"),
    "list.run_id": ("runId", "run_id"),
    "list.status": ("status",),
}
STATUS_INT = {1: "RUNNING", 2: "COMPLETED", 3: "FAILED", 4: "CANCELED", 5: "TERMINATED", 6: "CONTINUED_AS_NEW",
              7: "TIMED_OUT"}                                                    # client/_workflow.py:1468-1494
PENDING_INT = {1: "SCHEDULED", 2: "STARTED", 3: "CANCEL_REQUESTED", 4: "PAUSED", 5: "PAUSE_REQUESTED"}
WFT_INT = {1: "SCHEDULED", 2: "STARTED"}
_PASCAL = re.compile(r"(?<!^)(?=[A-Z])")


def evidence_schema(R):
    v = R.get("schema_version")
    return isinstance(v, int) and not isinstance(v, bool) and v >= EVIDENCE_SCHEMA


def get(d, rule, default=None):
    if not isinstance(d, dict):
        return default
    for key in FIELD_MAP[rule]:
        if key in d:
            return d[key]
    return default


def enum_upper(value, prefix, ints=None):
    """WORKFLOW_EXECUTION_STATUS_CANCELED / Canceled / 4 -> CANCELED."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return (ints or {}).get(value)
    s = str(value)
    if s.startswith(prefix):
        return s[len(prefix):]
    if "_" in s or s.isupper():
        return s.upper()
    return _PASCAL.sub("_", s).upper()


def event_type(value):
    """EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED / WorkflowExecutionCancelRequested -> the PascalCase name."""
    if not isinstance(value, str):
        return None
    if value.startswith("EVENT_TYPE_") or value.isupper():
        return "".join(w.capitalize() for w in value.replace("EVENT_TYPE_", "", 1).split("_"))
    return value


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_time(v):
    """Protobuf JSON timestamp (RFC 3339, up to nanoseconds) or {seconds, nanos} -> epoch seconds."""
    if isinstance(v, dict):
        try:
            return float(v.get("seconds", 0)) + float(v.get("nanos", 0)) / 1e9
        except (TypeError, ValueError):
            return None
    if not isinstance(v, str) or not v:
        return None
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", v)
    if not m:
        return None
    frac = float(m.group(2)) if m.group(2) else 0.0
    tz = m.group(3) or "Z"
    base = datetime.fromisoformat(m.group(1) + ("+00:00" if tz == "Z" else tz))
    return base.timestamp() + frac


def _name(v):
    return v.get("name") if isinstance(v, dict) else v


def norm_describe(j):
    """describe --raw JSON -> {status, history_length, pending, pending_wft, cancel_requested}; None if unusable.
    Protobuf JSON omits empty repeated fields and false booleans, so an absent pending list is empty and an
    absent flag inside a present extended-info object is false."""
    if not isinstance(j, dict):
        return None
    info = get(j, "describe.info")
    if not isinstance(info, dict):
        return None
    status = enum_upper(get(info, "info.status"), "WORKFLOW_EXECUTION_STATUS_", STATUS_INT)
    if status is None:
        return None
    pending = []
    for p in get(j, "describe.pending_activities", []) or []:
        if not isinstance(p, dict):
            continue
        pending.append({"activity_id": get(p, "pending.activity_id"),
                        "activity_type": _name(get(p, "pending.activity_type")),
                        "state": enum_upper(get(p, "pending.state"), "PENDING_ACTIVITY_STATE_", PENDING_INT),
                        "attempt": to_int(get(p, "pending.attempt")),
                        "last_heartbeat_time": parse_time(get(p, "pending.last_heartbeat_time")),
                        "last_started_time": parse_time(get(p, "pending.last_started_time")),
                        "last_failure": (get(p, "pending.last_failure") or {}).get("message")
                        if isinstance(get(p, "pending.last_failure"), dict) else None,
                        "last_worker_identity": get(p, "pending.last_worker_identity")})
    wft = get(j, "describe.pending_workflow_task")
    pwft = None
    if isinstance(wft, dict) and wft:
        pwft = {"state": enum_upper(get(wft, "wft.state"), "PENDING_WORKFLOW_TASK_STATE_", WFT_INT),
                "attempt": to_int(get(wft, "wft.attempt")),
                "scheduled_time": parse_time(get(wft, "wft.scheduled_time")),
                "original_scheduled_time": parse_time(get(wft, "wft.original_scheduled_time")),
                "started_time": parse_time(get(wft, "wft.started_time"))}
    ext = get(j, "describe.extended")
    cancel_requested = bool(get(ext, "extended.cancel_requested", False)) if isinstance(ext, dict) else None
    return {"status": status, "history_length": to_int(get(info, "info.history_length")), "pending": pending,
            "pending_wft": pwft, "cancel_requested": cancel_requested}


def norm_history(j):
    """show --output json -> list of {id, type, time, attrs}; None if unusable."""
    events = get(j, "history.events") if isinstance(j, dict) else (j if isinstance(j, list) else None)
    if not isinstance(events, list):
        return None
    out = []
    for e in events:
        if not isinstance(e, dict):
            return None
        attrs = next((v for k, v in e.items() if k.endswith("EventAttributes") or k.endswith("_event_attributes")),
                     {})
        out.append({"id": to_int(get(e, "event.id")), "type": event_type(get(e, "event.type")),
                    "time": parse_time(get(e, "event.time")), "attrs": attrs if isinstance(attrs, dict) else {}})
    return out


def norm_list(j):
    """list --output json -> list of {workflow_id, run_id, status}; None if unusable."""
    rows = j if isinstance(j, list) else (get(j, "list.executions") if isinstance(j, dict) else None)
    if rows is None and isinstance(j, dict) and get(j, "list.execution") is not None:
        rows = [j]
    if not isinstance(rows, list):
        return None
    out = []
    for r in rows:
        if not isinstance(r, dict):
            return None
        ex = get(r, "list.execution") or {}
        out.append({"workflow_id": get(ex, "list.workflow_id") or get(r, "list.workflow_id"),
                    "run_id": get(ex, "list.run_id") or get(r, "list.run_id"),
                    "status": enum_upper(get(r, "list.status"), "WORKFLOW_EXECUTION_STATUS_", STATUS_INT)})
    return out


def part_state(part):
    """ok / not_found / failed / missing for one CLI call record."""
    if not isinstance(part, dict):
        return "missing"
    if part.get("rc") == 0 and not part.get("timed_out") and part.get("json") is not None:
        return "ok"
    if "not found" in str(part.get("stderr_tail", "")).lower():
        return "not_found"
    return "failed"


def describe_of(read, name="describe_target"):
    part = (read or {}).get("parts", {}).get(name)
    return norm_describe(part.get("json")) if part_state(part) == "ok" else None


def history_of(read, name="show_target"):
    part = (read or {}).get("parts", {}).get(name)
    return norm_history(part.get("json")) if part_state(part) == "ok" else None


def list_status_of(read, wf_id, name="list_target"):
    part = (read or {}).get("parts", {}).get(name)
    rows = norm_list(part.get("json")) if part_state(part) == "ok" else None
    if rows is None:
        return None
    hit = [r for r in rows if r.get("workflow_id") == wf_id]
    return hit[0]["status"] if hit else "ABSENT"


def call_end(read, name):
    part = (read or {}).get("parts", {}).get(name) or {}
    return part.get("t_end", read.get("t_end") if read else None)


def wall_to_mono(read, name, wall):
    """Place a platform event time on the monotonic clock through the call's paired stamps (metadata only)."""
    part = (read or {}).get("parts", {}).get(name) or {}
    if wall is None or part.get("wall_end") is None or part.get("t_end") is None:
        return None
    return part["t_end"] + (wall - part["wall_end"])


def _jsonl(path):
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            if i == len(lines) - 1 and not line.endswith("\n"):
                continue                       # a writer's unfinished last line
            raise
    return rows


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ============================ the trial ============================
class Trial:
    def __init__(self, record, receiver_lines, worker_logs=None, folder=None):
        self.folder = folder
        self.R = record
        self.receiver_ok = receiver_lines is not None
        lines = receiver_lines or []
        # C.9: the scorer's own recomputation of the wall/monotonic check, from every receiver line's (mono, t)
        self.wall_check = clockwall.check_receiver_rows(lines) if self.receiver_ok else \
            {"ok": False, "reasons": ["receiver log missing: no paired stamps to recompute from"]}
        self.effects = [x for x in lines if x.get("route") == "effect" and not x.get("bad") and _num(x.get("mono"))]
        self.bad_lines = [x for x in lines if x.get("bad")]
        self.worker_logs = worker_logs or []
        self.trial, self.cond = record.get("trial"), record.get("condition")
        self.spec = record.get("spec") or {}
        self.plan = record.get("plan") or {}
        ids = record.get("ids") or {}
        self.target = ids.get("target") or {}
        self.twin = ids.get("twin") or {}
        self.ptwin = ids.get("ptwin") or {}
        self.box = record.get("intervention") or {}
        self.void = record.get("void") or (f"trial error: {record['error']}" if record.get("error") else None)
        self.t_stop = (self.box.get("t_stop") or {}).get("mono")
        self.actions = self.box.get("actions") or []
        self.request = next((a for a in self.actions if a.get("name") in REQUEST_ACTIONS), None)
        self.kill = next((a for a in self.actions if a.get("name") == "sigkill_worker"), None)
        self.watch_end = record.get("watch_end_mono") or (
            self.t_stop + self.plan.get("watch_s", 120.0) if _num(self.t_stop) else None)
        reads = [r for r in (record.get("reads") or []) + (record.get("claim_poll") or []) if _num(r.get("t_end"))]
        self.watch_reads = sorted([r for r in reads if self.watch_end is None or r["t_end"] <= self.watch_end + 5],
                                  key=lambda r: r["t_end"])
        self.scheduled = sorted([r for r in record.get("reads") or [] if _num(r.get("t_end"))],
                                key=lambda r: r["t_end"])
        probe = record.get("probe") or {}
        self.probe = probe
        self.outages = list(self.box.get("outages") or []) + list(probe.get("outages") or [])
        self.declared = sorted([e for e in self.effects if self._is(e, "target") and e.get("kind") in DECLARED_KINDS],
                               key=lambda e: e["mono"])
        # The durability probe is a treatment (C.4 step 6): the watch measures (M1-M5, history coverage, the
        # condition rows) use the work effects up to the end of the watch; the probe's effects are scored only in
        # the probe's restart row, which reads self.declared.
        self.work_all = [e for e in self.declared if e.get("activity_id") == "work"]
        self.work = [e for e in self.work_all if not _num(self.watch_end) or e["mono"] <= self.watch_end]
        self.ticks = sorted(e["mono"] for e in self.effects if e.get("kind") == "tick")
        self.gaps = self._gaps()
        self.executions = self._executions(self.work)

    def _is(self, e, role):
        ids = {"target": self.target, "twin": self.twin, "ptwin": self.ptwin}[role]
        return (e.get("role") == role and e.get("workflow_id") == ids.get("workflow_id") and
                e.get("run_id") == ids.get("run_id"))

    def _gaps(self):
        """Tick gaps over 2 s from the first tick to the last (C.9). No ticks at all is one unbounded gap."""
        if not self.ticks:
            return [(-math.inf, math.inf)]
        return [(a, b) for a, b in zip(self.ticks, self.ticks[1:]) if b - a > GAP_S]

    def gap_in(self, a, b):
        """Is the receiver unproven anywhere in (a, b]? True if a tick gap over 2 s overlaps it, or if it reaches
        more than 2 s outside the ticks' span, or if either bound is missing."""
        if not (_num(a) and _num(b)):
            return True
        if any(g0 < b and g1 > a for g0, g1 in self.gaps):
            return True
        return b > self.ticks[-1] + GAP_S or a < self.ticks[0] - GAP_S

    @staticmethod
    def _executions(effects):
        ex = {}
        for e in effects:
            n = e.get("exec_nonce")
            x = ex.setdefault(n, {"exec_nonce": n, "attempt": e.get("attempt"), "is_local": e.get("is_local"),
                                  "identity": e.get("identity"), "first": e["mono"], "last": e["mono"],
                                  "steps": [], "final": False, "effects": []})
            x["first"], x["last"] = min(x["first"], e["mono"]), max(x["last"], e["mono"])
            x["effects"].append(e)
            if e.get("kind") == "progress":
                x["steps"].append(e.get("step"))
            if e.get("kind") == "final":
                x["final"] = True
        return sorted(ex.values(), key=lambda x: x["first"])

    def in_outage(self, a, b):
        for o in self.outages:
            t0, t1 = o.get("t_from"), o.get("t_to")
            if _num(t0) and a <= (t1 if _num(t1) else math.inf) and b >= t0:
                return True
        return False

    def references(self):
        """(name, t_send, t_return) reference points for M1 (C.6)."""
        out = []
        if self.request and _num(self.request.get("t_return")):
            if self.kill and _num(self.kill.get("t_send")):
                out.append(("from the kill", self.kill["t_send"], self.kill.get("t_return", self.kill["t_send"])))
            out.append((f"from the {self.request['name']}", self.request["t_send"], self.request["t_return"]))
        elif _num(self.t_stop):
            first = self.actions[0] if self.actions else {}
            out.append(("from t_stop", first.get("t_send", self.t_stop), first.get("t_return", self.t_stop)))
        return out

    def ref_return(self):
        refs = self.references()
        return refs[-1][2] if refs else None

    def clock_order_ok(self):
        bad = [e for e in self.declared if not _num(e.get("smono")) or e["smono"] >= e["mono"]]
        return not bad

    def worker_dead_at(self, t):
        for w in self.box.get("worker_dead") or []:
            t0, t1 = w.get("t_from"), w.get("t_to")
            if w.get("role") == "target" and _num(t0) and t0 <= t and (not _num(t1) or t < t1):
                return True
        return False

    def last_scheduled_with(self, getter, upto=None):
        for r in reversed(self.scheduled):
            if upto is not None and r["t_end"] > upto:
                continue
            v = getter(r)
            if v is not None:
                return r, v
        return None, None

    def rel(self, t):
        ref = self.ref_return()
        return round(t - ref, 3) if _num(t) and _num(ref) else None

    def receiver_unproven(self, a=None, b=None):
        """C.5, C.9: is the receiver's record incomplete anywhere in (a, b]? By default from the target's first work
        effect (or t_stop) to the end of the watch. An absence ("no final", "none", "stopped") needs a complete
        record; positive evidence does not."""
        if a is None:
            a = min([x["first"] for x in self.executions] + ([self.t_stop] if _num(self.t_stop) else []), default=None)
        if b is None:
            b = self.watch_end
        return not self.receiver_ok or self.gap_in(a, b)

    def watch_acks(self):
        """The worker log's cancellation acknowledgements for the target, up to the end of the watch (C.6)."""
        return [w for w in self.worker_logs if w.get("event") == "activity_cancelled" and
                w.get("workflow_id") == self.target.get("workflow_id") and _num(w.get("smono")) and
                (not _num(self.watch_end) or w["smono"] <= self.watch_end)]


def row(measure, outcome, kind, verdict, **evidence):
    return {"measure": measure, "outcome": outcome, "kind": kind, "verdict": verdict, "evidence": evidence}


def _counts(effects):
    c = {}
    for e in effects:
        c[e.get("kind")] = c.get(e.get("kind"), 0) + 1
    return c


# ============================ M1 ============================
def m1_rows(T):
    out = []
    if not T.receiver_ok:
        return [row("M1", "effects after the request", UNK, "UNKNOWN (receiver log missing)")]
    for name, t_send, t_ret in T.references():
        end = T.watch_end
        commit = [e for e in T.declared if t_ret < e["mono"] <= end]
        init = [e for e in T.declared if _num(e.get("smono")) and e["smono"] > t_ret and e["mono"] <= end]
        amb = [e for e in T.declared if t_send < e["mono"] <= t_ret]
        first = T.executions[0]["exec_nonce"] if T.executions else None
        steps = sorted(e.get("step") for e in commit if e.get("exec_nonce") == first and e.get("kind") == "progress")
        gap = T.gap_in(t_ret, end)
        kind_c = UNK if gap else ("effects after" if commit else "none after")
        kind_i = UNK if gap or not T.clock_order_ok() else ("effects after" if init else "none after")
        by_exec = {}
        for e in commit:
            by_exec[e.get("exec_nonce")] = by_exec.get(e.get("exec_nonce"), 0) + 1
        out.append(row("M1", f"by commit, {name}", kind_c,
                       f"{len(commit)} declared effects committed after; kinds {_counts(commit)}"
                       + (" (receiver gap: a lower bound)" if gap else ""),
                       n=len(commit), kinds=_counts(commit), by_execution=by_exec,
                       first_execution_steps=steps, ambiguous=len(amb)))
        out.append(row("M1", f"by initiation, {name}", kind_i, f"{len(init)} declared effects initiated after",
                       n=len(init), kinds=_counts(init)))
        out.append(row("M1", f"ambiguous, {name}", "info", f"{len(amb)} arrived during the call", n=len(amb)))
    return out


# ============================ M2 ============================
def claim(T, rule="O1"):
    """The first watch read that makes the claim under O1 (describe closed) or O2 (C.6). Returns
    (read, claim_time, status, evaluable_before) where evaluable_before is False if an earlier read could not
    be evaluated under the rule."""
    unknown_before = False
    for r in T.watch_reads:
        if _num(T.t_stop) and r["t_end"] < T.t_stop:
            continue
        d = describe_of(r)
        if d is None:
            unknown_before = True
            continue
        if d["status"] not in CLOSED:
            continue
        if rule == "O1":
            return r, call_end(r, "describe_target"), d["status"], not unknown_before
        if not o2_applies(r):
            continue                    # a claim-poll read has describe only: O2 is not made there, by design
        o2 = o2_holds(T, r, d)
        if o2 is None:
            unknown_before = True
        elif o2:
            return r, call_end(r, "describe_target"), d["status"], not unknown_before
    return None, None, None, not unknown_before


def o2_applies(r):
    return "list_target" in r.get("parts", {}) and "show_target" in r.get("parts", {})


def o2_holds(T, r, d):
    """True / False, or None when a part O2 needs failed at this read."""
    ls, h = list_status_of(r, T.target.get("workflow_id")), history_of(r)
    if ls is None or h is None:
        return None
    return bool(ls == d["status"] and not d["pending"] and h and h[-1]["type"] in CLOSING_EVENTS)


def m2_rows(T):
    out = []
    r, t, status, _ = claim(T, "O1")
    out.append(row("M2", "claim", status if r else "no claim",
                   f"first closed read: {status} at rel {T.rel(t)} s ({r.get('label') if r else '-'})"
                   if r else "no read in the watch reports a closed status",
                   claim_time=t, claim_rel=T.rel(t), label=r.get("label") if r else None))
    last = next((describe_of(x) for x in reversed(T.watch_reads) if describe_of(x)), None)
    out.append(row("M2", "terminal status", last["status"] if last else UNK,
                   f"last watch read: {last['status'] if last else 'no usable describe'}"))
    # WorkflowExecutionCancelRequested in the history: first scheduled read, the read before, and its event time.
    # "not seen" needs every scheduled history read to be usable; otherwise the event may be in a failed read (C.5)
    prev, first, unusable = None, None, 0
    for x in T.scheduled:
        h = history_of(x)
        if h is None:
            unusable += 1
        elif any(e["type"] == "WorkflowExecutionCancelRequested" for e in h):
            first = x
            break
        prev = x
    ev_mono = None
    if first:
        ev = next(e for e in history_of(first) if e["type"] == "WorkflowExecutionCancelRequested")
        ev_mono = wall_to_mono(first, "show_target", ev["time"])
    out.append(row("M2", "cancel first in history", "seen" if first else (UNK if unusable or not T.scheduled
                                                                           else "not seen"),
                   f"first at {first['label']} (rel {T.rel(call_end(first, 'show_target'))} s); previous read "
                   f"{prev['label'] if prev else '-'}" if first else
                   f"no usable scheduled read shows it ({unusable} history reads unusable)",
                   first_label=first.get("label") if first else None,
                   first_rel=T.rel(call_end(first, "show_target")) if first else None,
                   previous_rel=T.rel(call_end(prev, "show_target")) if prev else None,
                   event_time_rel_metadata=T.rel(ev_mono)))
    flag = next((x for x in T.watch_reads if (describe_of(x) or {}).get("cancel_requested")), None)
    # the flag is unknown at a read whose describe failed or carried no extended info (norm_describe gives None)
    flag_unknown = sum(1 for x in T.watch_reads if (describe_of(x) or {}).get("cancel_requested") is None)
    out.append(row("M2", "cancel_requested first seen", "seen" if flag else (UNK if flag_unknown or not T.watch_reads
                                                                              else "not seen"),
                   f"describe reports cancel_requested at rel {T.rel(call_end(flag, 'describe_target'))} s"
                   if flag else f"never reported in the watch ({flag_unknown} reads without the flag)",
                   first_rel=T.rel(call_end(flag, "describe_target")) if flag else None))
    lr, h = T.last_scheduled_with(history_of, T.watch_end + 5 if _num(T.watch_end) else None)
    closing = h[-1] if h and h[-1]["type"] in CLOSING_EVENTS else None
    out.append(row("M2", "closing event", closing["type"] if closing else ("none" if h else UNK),
                   f"last history event {h[-1]['type'] if h else '-'}",
                   event_time_rel_metadata=T.rel(wall_to_mono(lr, "show_target", closing["time"])) if closing else None))
    return out


# ============================ M3 ============================
def final_landed_row(measure, T):
    """Whether a `final` effect landed by the end of the watch. "no" needs a gap-free receiver from the first work
    effect (or t_stop) to the end of the watch (C.5); a final effect that landed is positive evidence."""
    if any(x["final"] for x in T.executions):
        return row(measure, "final landed", "yes", "a final effect landed by the end of the watch")
    if T.receiver_unproven():
        return row(measure, "final landed", UNK, "UNKNOWN (no final effect seen, but the receiver record is "
                                                 "incomplete or has a gap)")
    return row(measure, "final landed", "no", "no final effect by the end of the watch")


def m3_rows(T):
    out = []
    ref = T.ref_return()
    for i, x in enumerate(T.executions):
        out.append(row("M3", f"time to quiet, execution {i + 1}", "info",
                       f"last effect at rel {T.rel(x['last'])} s; steps {min(x['steps'] or [0])}-"
                       f"{max(x['steps'] or [0])}; final={x['final']}",
                       exec_nonce=x["exec_nonce"], attempt=x["attempt"], last_rel=T.rel(x["last"])))
    out.append(final_landed_row("M3", T))
    planned_bound = T.plan.get("delivery_bound_step")
    ph = heartbeat_phase(T) if T.executions and (T.spec.get("variant") == "HB" or T.cond in ("C2", "C2w", "C5")) \
        else None
    if ph is not None:
        out.append(row("M3", "heartbeat phase precondition", ph["state"],
                       f"phase {ph['state']}: " + (ph.get("why") or
                                                   f"sampled RPC {ph['sampled_rpc_s']} s into the activity (grid error "
                                                   f"{ph['grid_error_s']} s); RPC before the stop {ph['last_rpc_before_stop_s']}"
                                                   f" s (planned {ph['planned_last_rpc_s']} s); next {ph['next_rpc_s']} s"),
                       **{k: v for k, v in ph.items() if k != "state"}))
    if T.cond in ("C2", "C2w", "C5"):
        if planned_bound is None or not T.executions:
            out.append(row("M3", "delivery bound", "not applicable", "no bound in this run kind or no execution"))
        elif ph["state"] != "pass":
            # the bound rests on the heartbeat phase: off the planned phase it would move for measurement reasons
            out.append(row("M3", "delivery bound", "phase off-plan" if ph["state"] == "off-plan" else UNK,
                           f"not judged: the heartbeat phase precondition is {ph['state']} (M)",
                           bound_step=planned_bound, phase=ph["state"]))
        else:
            x = T.executions[0]
            bound = ph["derived_bound_step"]
            late = [s for s in x["steps"] if isinstance(s, int) and s >= bound]
            # a late step or a final effect is positive evidence and stands; "within" is an absence (C.6)
            kind = "exceeded" if late or x["final"] else (UNK if T.gap_in(ref, T.watch_end) else "within")
            out.append(row("M3", "delivery bound", kind,
                           f"first execution: steps >= {bound}: {late}; final={x['final']}", late_steps=late,
                           bound_step=bound, planned_bound_step=planned_bound))
        out += delivery_rows(T, ph)
    phase = []
    for r in T.watch_reads:
        d = describe_of(r)
        for p in (d or {}).get("pending", []):
            if p.get("activity_id") == "work" and p.get("last_heartbeat_time") is not None:
                phase.append(T.rel(wall_to_mono(r, "describe_target", p["last_heartbeat_time"])))
    out.append(row("M3", "heartbeat phase (metadata)", "info", f"last_heartbeat_time rel values: {sorted(set(phase))}",
                   last_heartbeat_rel=sorted(set(x for x in phase if x is not None))))
    acks = T.watch_acks()
    out.append(row("M3", "worker-log acknowledgements (C diagnostic)", "info",
                   f"{len(acks)} cancellation acknowledgement(s)",
                   acks=[{"exec_nonce": a.get("exec_nonce"), "rel": T.rel(a.get("smono")), "flags": a.get("flags"),
                          "last_step": a.get("last_step"), "phase": a.get("phase")} for a in acks]))
    out += in_flight_rows(T)
    out += lateness_rows(T)
    return out


PHASE_TOL_S = 0.25        # C.6 M3: the sampled heartbeat RPC lies within this of the planned grid
RACE_MARGIN_S = 0.5       # a step emitted within this after the delivering RPC may still race it
LATE_STEP_S = 0.25        # C.6 M3 diagnostic: a step sent this much after its nominal time is flagged


def heartbeat_phase(T):
    """C.6 M3 phase precondition (pre-freeze review). The delivery bound rests on the heartbeat phase: one RPC per
    0.8 x the heartbeat timeout (T4, K4, K6) on a grid from the activity's start, measured once in feasibility (C.13).
    Per trial: the pre-intervention check's describe gives the work activity's last heartbeat time, placed on the
    monotonic clock through that call's paired stamps (metadata, T17); the activity starts at its step-1 effect's sender
    stamp. pass: that RPC lies within PHASE_TOL_S of the grid, and the grid puts the RPC before the stop at the planned
    time (8 s into the activity, 0.5 s before the stop) within the same tolerance. The next RPC follows, and the bound is
    the first step emitted more than RACE_MARGIN_S after it. off-plan: evaluated and failed. UNKNOWN: not evaluable."""
    p = T.plan
    out = {"tolerance_s": PHASE_TOL_S, "race_margin_s": RACE_MARGIN_S}
    try:
        throttle, step_s = 0.8 * float(p["hb_timeout_s"]), float(p["step_s"])
        stop_phase = (int(p["anchor_step"]) - 1) * step_s + float(p["stop_after_anchor_s"])
    except (KeyError, TypeError, ValueError):
        return dict(out, state=UNK, why="the plan lacks the heartbeat or step parameters")
    x = T.executions[0] if T.executions else {}
    step1 = next((e for e in x.get("effects", []) if e.get("kind") == "progress" and e.get("step") == 1
                  and _num(e.get("smono"))), None)
    pre = (T.R.get("pre_check") or {}).get("read")
    d = describe_of(pre) if isinstance(pre, dict) else None
    hb = next((q["last_heartbeat_time"] for q in (d or {}).get("pending", [])
               if q.get("activity_id") == "work" and q.get("last_heartbeat_time") is not None), None)
    h = wall_to_mono(pre, "describe_target", hb) if hb is not None else None
    if step1 is None or not _num(h) or not _num(T.t_stop):
        return dict(out, state=UNK, why="no step-1 sender stamp, pre-check heartbeat time or t_stop")
    a0 = step1["smono"]
    k = round((h - a0) / throttle)
    err = (h - a0) - k * throttle
    planned_last = math.floor(stop_phase / throttle + 1e-9) * throttle
    last = err + math.floor((T.t_stop - a0 - err) / throttle) * throttle
    nxt = last + throttle
    ok = k >= 0 and abs(err) <= PHASE_TOL_S and abs(last - planned_last) <= PHASE_TOL_S
    return dict(out, state="pass" if ok else "off-plan", sampled_rpc_s=round(h - a0, 3), grid_error_s=round(err, 3),
                last_rpc_before_stop_s=round(last, 3), planned_last_rpc_s=planned_last, next_rpc_s=round(nxt, 3),
                next_rpc_mono=a0 + nxt, stop_s=round(T.t_stop - a0, 3),
                derived_bound_step=int(math.floor((nxt + RACE_MARGIN_S) / step_s)) + 2)


def _first_ack(T, nonce):
    acks = sorted((w for w in T.watch_acks() if w.get("exec_nonce") == nonce), key=lambda w: w["smono"])
    return acks[0] if acks else None


def delivery_rows(T, ph):
    """C.6 M3, split by the pre-freeze review (C.7 K14): when the cancellation reached the first execution (Temporal's
    heartbeat RPC: P|K4,K6+M), and whether our code then stopped (K14: it re-raises asyncio.CancelledError with no
    cleanup; C|K1,K8,K14). Both read our worker log's acknowledgement. "Stops after delivery" is by initiation: a step
    whose sender stamp follows the acknowledgement is our code continuing; one in flight at it (K8) is not."""
    if not T.executions:
        return []
    x = T.executions[0]
    ack = _first_ack(T, x["exec_nonce"])
    if ack is None:
        return [row("M3", "delivery time", "no acknowledgement", "no cancellation acknowledgement for the first "
                    "execution in the watch", label="P|K4,K6+M"),
                row("M3", "stops after delivery", "no acknowledgement", "no delivery to judge", label="C|K1,K8,K14")]
    a = ack["smono"]
    nxt = ph.get("next_rpc_mono") if ph and ph.get("state") == "pass" else None
    after = sorted(e.get("step") or 0 for e in x["effects"] if _num(e.get("smono")) and e["smono"] > a)
    kind = "continued after delivery" if after else (UNK if T.receiver_unproven(a, T.watch_end) else
                                                     "stopped after delivery")
    return [row("M3", "delivery time", "info",
                f"acknowledged at rel {T.rel(a)} s" + (f", {round(a - nxt, 3)} s after the next nominal RPC"
                                                       if nxt is not None else " (phase not on plan)"),
                ack_rel=T.rel(a), next_rpc_rel=T.rel(nxt), ack_minus_next_rpc_s=round(a - nxt, 3) if nxt else None,
                flags=ack.get("flags"), label="P|K4,K6+M"),
            row("M3", "stops after delivery", kind,
                f"{len(after)} effect(s) of the first execution initiated after its acknowledgement: steps {after}",
                steps_after=after, label="C|K1,K8,K14")]


def in_flight_rows(T):
    """C.6 M3 (pre-freeze review; C|K8): for each execution our worker log saw cancelled in the watch, the activity's
    phase at the cancellation (posting n, heartbeat n, sleeping after n) and whether the effect in flight committed at
    the receiver: committed, not committed (an absence: it needs a gap-free receiver) or not in flight."""
    out = []
    for i, x in enumerate(T.executions):
        ack = _first_ack(T, x["exec_nonce"])
        if ack is None:
            continue
        name = f"step interrupted in flight, execution {i + 1}"
        if "phase" not in ack:
            out.append(row("M3", name, UNK, "the acknowledgement records no phase (a worker log from before the "
                           "pre-freeze review)", label="C|K8"))
            continue
        inflight = ack.get("inflight")
        if not inflight:
            kind = "not in flight"
        elif any(e.get("step") == inflight.get("step") and e.get("kind") == inflight.get("kind") for e in x["effects"]):
            kind = "committed"
        else:
            sent = inflight.get("smono") if _num(inflight.get("smono")) else ack["smono"]
            kind = UNK if T.receiver_unproven(sent, T.watch_end) else "not committed"
        out.append(row("M3", name, kind, f"phase {ack.get('phase')}; in flight {inflight}", phase=ack.get("phase"),
                       inflight=inflight, exec_nonce=x["exec_nonce"], label="C|K8"))
    return out


def lateness_rows(T):
    """C.6 M3 diagnostic (pre-freeze review): our observer's own load (the host's load average and steal ticks, which the
    harness records at each scheduled read and at the pre-check), each step's lateness against the nominal schedule
    (sender stamp minus step 1's plus (n - 1) x step_s) and the heartbeat RPC spacing (describe's last_heartbeat_time
    values, placed on the monotonic clock). A step later than LATE_STEP_S is flagged: a late step or heartbeat may be
    our reads' load, not Temporal's timing."""
    step_s = T.plan.get("step_s")
    per = []
    for x in T.executions:
        sent = {e.get("step"): e["smono"] for e in x["effects"]
                if e.get("kind") == "progress" and isinstance(e.get("step"), int) and _num(e.get("smono"))}
        if 1 not in sent or not _num(step_s):
            continue
        late = {n: s - (sent[1] + (n - 1) * step_s) for n, s in sent.items()}
        per.append({"exec_nonce": x["exec_nonce"], "max_lateness_s": round(max(late.values()), 3),
                    "late_steps": sorted(n for n, v in late.items() if v > LATE_STEP_S)})
    pre = (T.R.get("pre_check") or {})
    reads = ([pre["read"]] if isinstance(pre.get("read"), dict) else []) + list(T.watch_reads)
    beats = set()
    for r in reads:
        for q in (describe_of(r) or {}).get("pending", []):
            if q.get("activity_id") == "work" and q.get("last_heartbeat_time") is not None:
                mono = wall_to_mono(r, "describe_target", q["last_heartbeat_time"])
                if _num(mono):
                    beats.add(round(mono, 3))
    beats = sorted(beats)
    loads = [x for x in [pre.get("host_load")] + [r.get("host_load") for r in T.scheduled] if isinstance(x, dict)]
    flagged = any(p["late_steps"] for p in per)
    return [row("M3", "step lateness and load (diagnostic)", "load flagged" if flagged else "info",
                f"max step lateness {[p['max_lateness_s'] for p in per]} s; steps over {LATE_STEP_S} s: "
                f"{[p['late_steps'] for p in per]}; heartbeat RPC spacing {[round(b - a, 3) for a, b in zip(beats, beats[1:])]}",
                executions=per, heartbeat_rpc_spacing_s=[round(b - a, 3) for a, b in zip(beats, beats[1:])],
                host_load=loads, label="M")]


# ============================ M4 ============================
def m4_rows(T):
    per_step = {}
    for x in T.executions:
        for s in set(x["steps"]):
            per_step[s] = per_step.get(s, 0) + 1
    finals = sum(1 for e in T.work if e.get("kind") == "final")
    n = len(T.executions)
    max_pending = max([p.get("attempt") or 0 for r in T.watch_reads for p in (describe_of(r) or {}).get("pending", [])
                       if p.get("activity_id") == "work"] or [0])
    _, h = T.last_scheduled_with(history_of)
    started = started_attempts(h) if h is not None else None
    markers = sum(1 for e in h if e["type"] == "MarkerRecorded") if h is not None else None
    # "none" is an absence: a receiver gap could hide a second execution (C.5)
    dup = "duplicates" if n > 1 else (UNK if T.receiver_unproven() else "none")
    if n <= 1:
        vis = "no duplicates" if dup == "none" else UNK
    elif max_pending >= 2 or (started and max(started) >= 2) or (markers is not None and markers >= 2):
        vis = "yes"
    elif max_pending or started:
        vis = "no"
    else:
        vis = UNK
    return [row("M4", "duplicates", dup,
                f"{n} execution(s); steps posted by >1 execution: {sorted(s for s, c in per_step.items() if c > 1)};"
                f" final effects: {finals}", executions=n, finals=finals,
                duplicated_steps=sorted(s for s, c in per_step.items() if c > 1),
                attempts=[x["attempt"] for x in T.executions]),
            row("M4", "duplicates visible in Temporal", vis,
                f"max pending attempt {max_pending}; ActivityTaskStarted attempts {started}; markers {markers}",
                max_pending_attempt=max_pending, started_attempts=started, markers=markers)]


def started_attempts(h, activity_id="work"):
    sched = {e["id"]: e["attrs"] for e in h if e["type"] == "ActivityTaskScheduled"}
    out = []
    for e in h:
        if e["type"] == "ActivityTaskStarted":
            s = sched.get(to_int(get(e["attrs"], "attrs.scheduled_event_id")), {})
            if get(s, "attrs.activity_id") == activity_id:
                out.append(to_int(get(e["attrs"], "attrs.attempt")))
    return out


# ============================ M5, pending on closed, history coverage ============================
def reality(T, t_r):
    if not T.receiver_ok:
        return UNK
    if any(t_r - ACTIVE_WINDOW_S < e["mono"] <= t_r for e in T.declared):
        return "active"
    if not any(t_r - QUIET_WINDOW_S < e["mono"] <= t_r for e in T.declared) and not T.gap_in(t_r - QUIET_WINDOW_S, t_r):
        return "quiet"
    return UNK


def m5_rows(T):
    out = []
    for r in T.watch_reads:
        d = describe_of(r)
        t_r = call_end(r, "describe_target")
        if d is None:
            part = r.get("parts", {}).get("describe_target")
            st = part_state(part)
            declared_outage = T.in_outage(part.get("t_start", t_r) if part else t_r, t_r)
            out.append(row("M5", f"status vs reality {r.get('label')}", UNK,
                           "SERVER_UNAVAILABLE_DUE_TO_INTERVENTION" if declared_outage and st == "failed"
                           else f"describe {st}", read_rel=T.rel(t_r)))
            continue
        real = reality(T, t_r)
        closed = d["status"] in CLOSED
        if real == UNK:
            kind = UNK
        elif closed:
            kind = "false" if real == "active" else "true"
        else:
            kind = "true" if real == "active" else "open, quiet"
        stale = [p for p in d["pending"] if p.get("state") == "STARTED"] if T.worker_dead_at(t_r) else []
        ev = {"status": d["status"], "reality": real, "pending": [(p.get("activity_id"), p.get("state"),
              p.get("attempt")) for p in d["pending"]], "read_rel": T.rel(t_r),
              "pending_wft": d.get("pending_wft"), "cancel_requested": d.get("cancel_requested")}
        out.append(row("M5", f"status vs reality {r.get('label')}", kind, f"{d['status']} while {real}", **ev))
        if stale:
            out.append(row("M5", f"stale activity state {r.get('label')}", "stale activity state",
                           "STARTED while the target worker is confirmed dead", read_rel=T.rel(t_r)))
        if "list_target" in r.get("parts", {}):
            ls = list_status_of(r, T.target.get("workflow_id"))
            if ls is not None and ls != d["status"]:
                out.append(row("M5", f"list vs describe {r.get('label')}", "disagree",
                               f"list {ls}, describe {d['status']}", read_rel=T.rel(t_r)))
    return out


def pending_on_closed_rows(T):
    seen, unusable = [], 0
    for r in T.watch_reads:
        d = describe_of(r)
        if d is None:
            unusable += 1
        elif d["status"] in CLOSED:
            seen.append((r, d))
    if not seen:
        # with failed reads, a closed read (and its listing) may have been missed: UNKNOWN, not "not applicable"
        return [row("pending_on_closed", "a closed run lists a pending activity",
                    UNK if unusable or not T.watch_reads else "not applicable",
                    f"no usable closed read in the watch ({unusable} describe reads unusable)")]
    hits = [(r.get("label"), [(p.get("activity_id"), p.get("state"), p.get("attempt")) for p in d["pending"]])
            for r, d in seen if d["pending"]]
    return [row("pending_on_closed", "a closed run lists a pending activity", "yes" if hits else "no",
                f"{len(hits)} of {len(seen)} closed reads list pending activities", examples=hits[:3])]


def history_coverage_rows(T):
    lr, h = T.last_scheduled_with(history_of, T.watch_end + 5 if _num(T.watch_end) else None)
    if h is None:
        return [row("history", "history omits an execution that produced effects", UNK, "no usable history read")]
    if not T.executions:
        return [row("history", "history omits an execution that produced effects",
                    UNK if T.receiver_unproven() else "not applicable", "no work execution posted")]
    started = started_attempts(h)
    markers = sum(1 for e in h if e["type"] == "MarkerRecorded")
    omitted = []
    if T.executions[0].get("is_local"):
        if len(T.executions) > markers:
            omitted = [x["exec_nonce"] for x in T.executions if not x["final"]][:len(T.executions) - markers]
    else:
        omitted = [x["exec_nonce"] for x in T.executions if x["attempt"] not in started]
    # "no" says every execution that posted is recorded; a receiver gap could hide an execution (C.5)
    kind = "yes" if omitted else (UNK if T.receiver_unproven() else "no")
    return [row("history", "history omits an execution that produced effects", kind,
                f"{len(omitted)} of {len(T.executions)} executions not recorded (ActivityTaskStarted attempts "
                f"{started}; markers {markers})", omitted=omitted, started_attempts=started, markers=markers,
                read=lr.get("label"))]


# ============================ false all-clear ============================
def fac_rows(T):
    out = []
    ok_init = T.clock_order_ok()
    first, unevaluable_before = {}, set()
    for r in T.watch_reads:
        if _num(T.t_stop) and r["t_end"] < T.t_stop:
            continue
        d = describe_of(r)
        if d is None:
            # a failed read before the first claim: the first claiming read is not known, so a later "no false
            # all-clear" does not settle it (a later false all-clear does: its effects follow any earlier claim)
            for rule in ("O1", "O2"):
                if rule not in first and (rule == "O1" or o2_applies(r)):
                    unevaluable_before.add(rule)
            continue
        if d["status"] not in CLOSED:
            continue
        t_c = call_end(r, "describe_target")
        for rule in ("O1", "O2"):
            if rule == "O2" and not o2_applies(r):
                continue                # claim-poll reads make only the O1 claim
            holds = True if rule == "O1" else o2_holds(T, r, d)
            base = f"{rule} at {r.get('label')} (rel {T.rel(t_c)} s)"
            if T.void:
                out.append(row("FAC", base + ", by commit", UNK, f"UNKNOWN (trial void: {T.void})", rule=rule))
                continue
            if holds is None:
                out.append(row("FAC", base + ", by commit", UNK, "UNKNOWN (O2 not evaluable at this read)",
                               rule=rule))
                if rule not in first:
                    unevaluable_before.add(rule)
                continue
            if not holds:
                continue
            is_first = rule not in first
            first.setdefault(rule, r)
            gap = T.gap_in(t_c, T.watch_end)
            after_c = [e for e in T.declared if t_c < e["mono"] <= T.watch_end]
            after_i = [e for e in T.declared if _num(e.get("smono")) and e["smono"] > t_c and e["mono"] <= T.watch_end]
            for version, after, unknown in (("by commit", after_c, gap), ("by initiation", after_i, gap or not ok_init)):
                kind = UNK if unknown else (FAC if after else NO_FAC)
                out.append(row("FAC", f"{base}, {version}", kind,
                               f"{d['status']} at rel {T.rel(t_c)} s; {len(after)} declared effects after",
                               n_after=len(after), claim_rel=T.rel(t_c), first=is_first, rule=rule, version=version))
    for rule in ("O1", "O2"):
        if rule in first:
            hit = next(x for x in out if x["evidence"].get("first") and x["evidence"].get("rule") == rule
                       and x["evidence"].get("version") == "by commit")
            kind, verdict = hit["kind"], hit["verdict"]
            if rule in unevaluable_before and kind == NO_FAC:
                # an earlier read failed or could not be evaluated, so the first claiming read is not known; a later
                # read without a false all-clear does not settle an earlier one (FAC at a later read would)
                kind, verdict = UNK, "UNKNOWN (an earlier read was not evaluable; " + verdict + ")"
            out.append(row("FAC", f"headline {rule} x by commit", kind, verdict, **hit["evidence"]))
        else:
            _, _, _, evaluable = claim(T, rule)
            kind = UNK if T.void else (f"no {rule} claim" if evaluable else UNK)
            out.append(row("FAC", f"headline {rule} x by commit", kind,
                           f"no read makes the {rule} claim" + ("" if evaluable else " (some reads not evaluable)")))
    return out


# ============================ computation, protected effects and the durable obligation (C.5, C.6) ============
# Pre-freeze batch, item 3. Three things are judged apart: the computation (each execution of the target's work
# activity), the protected effects (declared effects at the receiver after the request, M1) and the accepted durable
# obligation (the target run, which Temporal keeps and can retry). A quiet watch says nothing about the obligation: the
# PC pilot's run stayed RUNNING, with attempt 2 SCHEDULED, and the probe re-ran that attempt.
OBLIGATION_CLOSED = CLOSED - {"CONTINUED_AS_NEW"}          # a continued-as-new run hands the job to a new run
ACTIVE_PENDING = ("STARTED", "CANCEL_REQUESTED")           # pending states in which an attempt may be running (T13)


OBLIGATION_OPEN = ("RUNNING", "CONTINUED_AS_NEW")         # the run, or a new run it handed the job to, can still act


def end_of_watch_read(T):
    """The scheduled read at the end of the watch (C.4 step 5: its offset is the watch length, +120 s), or None. An
    earlier read never stands in for it."""
    want = T.plan.get("watch_s")
    hits = [r for r in T.scheduled if _num(want) and _num(r.get("offset")) and r["offset"] == want]
    return hits[-1] if hits else None


def _pending_view(d):
    return [(p.get("activity_id"), p.get("state"), p.get("attempt")) for p in (d or {}).get("pending", [])]


def durable_obligation(T):
    """The accepted durable job, the target run, at the end of the watch. closed: describe reports a closed status
    other than CONTINUED_AS_NEW (T14); a closed execution takes no further events (T2, T9), so nothing of it can run or
    be retried. open: RUNNING (it can still schedule and retry work, T10, T11) or CONTINUED_AS_NEW (a new run carries
    the job). unknown: no usable describe. The end-of-watch read decides; if its describe failed, an earlier closed
    read stands, because closure is permanent, but an earlier RUNNING read does not."""
    er = end_of_watch_read(T)
    d = describe_of(er) if er else None
    if d and (d["status"] in OBLIGATION_CLOSED or d["status"] in OBLIGATION_OPEN):
        return {"state": "closed" if d["status"] in OBLIGATION_CLOSED else "open", "status": d["status"],
                "read": er.get("label"), "pending": _pending_view(d)}
    for r in reversed(T.watch_reads):
        d = describe_of(r)
        if d and d["status"] in OBLIGATION_CLOSED:
            return {"state": "closed", "status": d["status"], "read": r.get("label"), "pending": _pending_view(d),
                    "note": "the end-of-watch describe was unusable; an earlier closed read stands (T2, T9)"}
    return {"state": "unknown", "status": d["status"] if d else None, "read": er.get("label") if er else None,
            "pending": _pending_view(d)}


def worker_deaths(T):
    """pid -> exit stamp of each target worker the procedure SIGKILLed with its exit observed (C.3; the pid is the
    launch record's, checked against the process identity before the signal, C.1)."""
    return {a["pid"]: a["exit_mono"] for a in T.actions
            if a.get("name") == "sigkill_worker" and a.get("pid") is not None and _num(a.get("exit_mono"))}


def computation_boundary(T):
    """C.5 event 3 and the verified stop: has every execution of the target's work activity that posted in the watch
    ended, by independent evidence? An execution is closed when its `final` effect landed (the designed final
    iteration: the final POST is the activity's last statement, jobs_c._work) or when every effect it posted came from
    a worker process the procedure killed with its exit observed before the end of the watch. A cancellation status,
    our worker log's acknowledgement (our code's view, DESIGN section 2) and silence leave it unresolved, as do a
    pending attempt in state STARTED or CANCEL_REQUESTED at the end-of-watch read that no closed execution accounts
    for, a missing end-of-watch describe and an incomplete receiver record (C.6, absences)."""
    dead = worker_deaths(T)
    execs = []
    for x in T.executions:
        pids = {e.get("pid") for e in x["effects"]}
        if x["final"]:
            how = "final effect: the designed final iteration landed"
        elif pids and None not in pids and all(p in dead and (not _num(T.watch_end) or dead[p] <= T.watch_end)
                                               for p in pids):
            how = "worker process confirmed dead: SIGKILL with its exit observed"
        else:
            how = None
        execs.append({"exec_nonce": x["exec_nonce"], "attempt": x["attempt"], "closed": how is not None,
                      "how": how or "no independent ending evidence (no final effect; its worker is not shown dead)",
                      "pids": sorted(pids, key=str), "last_rel": T.rel(x["last"])})
    er = end_of_watch_read(T)
    d = describe_of(er) if er else None
    closed_attempts = {x["attempt"] for x in execs if x["closed"]}
    unaccounted = [p for p in _pending_view(d) if p[1] in ACTIVE_PENDING and p[2] not in closed_attempts]
    reasons = [f"execution {x['exec_nonce']} (attempt {x['attempt']}): {x['how']}" for x in execs if not x["closed"]]
    reasons += [f"pending {p[0]} attempt {p[2]} reads {p[1]} at the end of the watch; no closed execution accounts "
                f"for it" for p in unaccounted]
    if d is None:
        reasons.append("no usable end-of-watch describe (a pending attempt cannot be ruled out)")
    if T.receiver_unproven():
        reasons.append("the receiver record is incomplete over the watch (an execution could be unseen)")
    history_reasons = history_work_evidence(T, execs)
    reasons += history_reasons
    return {"state": "unresolved" if reasons else "closed", "executions": execs, "unaccounted_pending": unaccounted,
            "history_reasons": history_reasons, "reasons": reasons}


ACTIVITY_CLOSING = {"ActivityTaskCompleted", "ActivityTaskFailed", "ActivityTaskTimedOut", "ActivityTaskCanceled"}


def history_work_evidence(T, execs):
    """The computation boundary's history cross-check (pre-freeze review). Receiver silence alone never closes the
    boundary: Temporal's end-of-watch history must show no work attempt that no closed execution accounts for. Every
    ActivityTaskStarted of the work activity (its attempt, T9) must be the attempt of a closed execution. With no work
    execution at the receiver, positive history evidence is needed that the work never ran: a closed history in which
    the work activity was never scheduled (every ActivityTaskScheduled names another activity), or in which each
    scheduled one has a closing event; for a local activity, which leaves no scheduled event (T8), a timer that started
    and never fired (the workflow closed before it reached the work, DESIGN-expC section 3). Returns the reasons that
    leave the boundary unresolved (none: the history shows no unaccounted attempt)."""
    er = end_of_watch_read(T)
    h = history_of(er) if er else None
    if h is None:
        return ["no usable end-of-watch history (an attempt Temporal started cannot be ruled out)"]
    closed_attempts = {x["attempt"] for x in execs if x["closed"]}
    reasons = [f"the history's ActivityTaskStarted for the work activity (attempt {a}) matches no closed execution"
               for a in started_attempts(h) if a is None or a not in closed_attempts]
    if not execs:
        types = [e["type"] for e in h]
        sched = [e for e in h if e["type"] == "ActivityTaskScheduled"]
        unnamed = [e["id"] for e in sched if get(e["attrs"], "attrs.activity_id") is None]
        closers = {to_int(get(e["attrs"], "attrs.scheduled_event_id")) for e in h if e["type"] in ACTIVITY_CLOSING}
        unclosed = [e["id"] for e in sched if get(e["attrs"], "attrs.activity_id") == "work" and e["id"] not in closers]
        if not h or h[-1]["type"] not in CLOSING_EVENTS:
            reasons.append("no work effect, and the end-of-watch history is not closed (the work may still run)")
        if unnamed:
            reasons.append(f"ActivityTaskScheduled event(s) {unnamed} name no activity (the work activity cannot be "
                           "ruled out)")
        if unclosed:
            reasons.append(f"the work activity was scheduled (event(s) {unclosed}) with no closing event, and no "
                           "execution of it posted")
        if T.spec.get("variant") == "LA" and not ("TimerStarted" in types and "TimerFired" not in types):
            reasons.append("a local activity leaves no scheduled event, and the timer fired (or never started): the "
                           "history cannot show that the work never ran")
    return reasons


def _ended_by_design(T, nonce):
    """An execution whose designed last effect landed in the watch: the work activity's `final`, or the announce
    activity's single `accepted` effect."""
    return any(e.get("exec_nonce") == nonce and e.get("kind") in ("final", "accepted") and
               (not _num(T.watch_end) or e["mono"] <= T.watch_end) for e in T.declared)


ENDED_BY_COMPLETION = "ended by completion, not by the stop"


def verified_stop_rows(T):
    """C.6 verified stop (as B.6): the operator's verdict over the declared set at the end of the watch.
      not stopped: the run is open (RUNNING or continued as new) at the end-of-watch read, or a declared effect
        committed in the last 5 s of the watch; this positive evidence is decided first.
      stopped: the durable obligation and the computation boundary (with its history cross-check) are closed, the trial
        is valid by C.9 (an absence of work needs complete evidence), and no execution ran to its `final` effect: every
        one was ended by the stop or by a confirmed kill, or none ran.
      ended by completion, not by the stop: as stopped, but an execution ran to its `final` effect (pre-freeze review:
        our 20-step job finishing inside the 120 s watch is not the request stopping it; as B.5, natural completion
        establishes neither containment nor a successful cancellation).
      unresolved: otherwise, with what lacks evidence; a void trial is always unresolved.
    M1-M5, the false all-clear and the five events are unchanged by it."""
    base = {"measure": "verified_stop", "outcome": "verified stop"}
    if T.void:
        return [row(base["measure"], base["outcome"], "unresolved", f"unresolved (trial void: {T.void})")]
    ob, comp = durable_obligation(T), computation_boundary(T)
    end = T.watch_end
    active = [e for e in T.declared if _num(end) and end - QUIET_WINDOW_S < e["mono"] <= end]
    ev = {"obligation": ob, "computation": comp, "active_effects": len(active),
          "basis": "C.6 verified stop; quiet, M2 claims and 'no false all-clear' are not proof that the job stopped"}
    if ob["state"] == "open" or active:
        why = ([f"the run reads {ob['status']} at {ob['read']}"] if ob["state"] == "open" else []) + \
              ([f"{len(active)} declared effect(s) in the last 5 s of the watch"] if active else [])
        return [row(base["measure"], base["outcome"], "not stopped", "not stopped (" + "; ".join(why) + ")", **ev)]
    lacking = ([] if ob["state"] == "closed" else ["the durable obligation is unknown (no usable describe)"]) + \
        comp["reasons"]
    validity = validity_rows(T)[0]
    ev["trial_validity"] = validity["verdict"]
    if validity["kind"] != "valid":
        lacking.append(f"the trial is invalid by C.9 ({validity['verdict']})")
    if lacking:
        return [row(base["measure"], base["outcome"], "unresolved", "unresolved (" + "; ".join(lacking) + ")", **ev)]
    completed = [x["exec_nonce"] for x in comp["executions"] if x["how"].startswith("final effect")]
    ev["by_completion"] = bool(completed)
    ev["completed_executions"] = completed
    kind = ENDED_BY_COMPLETION if completed else "stopped"
    return [row(base["measure"], base["outcome"], kind,
                f"{kind} (the run is {ob['status']} and every work execution has independent ending evidence"
                + (f"; {len(completed)} execution(s) ran to their final effect" if completed else "") + ")", **ev)]


# ============================ five events ============================
def request_state(T):
    if T.spec.get("procedure") in (None, "none"):
        return "not applicable"
    if T.box.get("error") or T.box.get("withheld"):
        return "failed"
    if T.request is None:
        return "not applicable" if T.spec.get("procedure") in ("kill_worker", "kill_worker_restart") else "failed"
    a = T.request
    return "accepted" if a.get("rc") == 0 and not a.get("timed_out") else "failed"


def five_events(T):
    acc = request_state(T)
    reads = [r for r in T.watch_reads if _num(T.t_stop) and r["t_end"] >= T.t_stop]
    changed = None
    for r in reads:
        d, h = describe_of(r), history_of(r)
        if (d and (d["status"] in CLOSED or d.get("cancel_requested"))) or \
                (h and any(e["type"] == "WorkflowExecutionCancelRequested" for e in h)):
            changed = r.get("label")
            break
    end = T.watch_end
    last = max((e["mono"] for e in T.declared if _num(end) and e["mono"] <= end), default=None)
    quiet = _num(end) and (last is None or end - last >= QUIET_WINDOW_S) and not T.gap_in(end - QUIET_WINDOW_S, end)
    cap = [c for c in T.R.get("capability") or [] if c.get("queue") == T.target.get("task_queue")]
    killed = T.kill is not None and _num(T.kill.get("exit_mono")) and not (T.box.get("restarts") or [])
    # the native, activity-based quiet (draft 4's "work ceased"), kept as it was: 5 s without a declared effect at the
    # end of the watch, a gap-free receiver, and the target queue's capability job or a confirmed kill
    bounded_quiet = (True if quiet and ((cap and all(c.get("ok") for c in cap)) or killed) else None) \
        if T.receiver_ok else None
    # C.5 event 3: work ceased needs that quiet and the computation boundary closed (item 3)
    comp = computation_boundary(T)
    work_ceased = True if bounded_quiet and comp["state"] == "closed" else None
    # C.5 event 4: the protected effects and the durable obligation. Never "closed" while the run is open: a RUNNING
    # run can still schedule or retry work whatever the receiver saw in the watch
    ob = durable_obligation(T)
    ref = T.ref_return()
    after = [e for e in T.declared if _num(ref) and ref < e["mono"] <= end]
    unfinished = sorted({str(e.get("exec_nonce")) for e in after if not _ended_by_design(T, e.get("exec_nonce"))})
    if not T.receiver_ok or T.gap_in(ref, end):
        effects_closed = "unknown"
    elif unfinished:
        effects_closed = "not closed"
    elif ob["state"] == "open":
        effects_closed = "obligation open"
    elif ob["state"] != "closed":
        effects_closed = "unknown"
    else:
        effects_closed = "closed by completion, not by the stop" if after else "closed"
    reasons = []
    for r in reads:
        for name, part in r.get("parts", {}).items():
            if part_state(part) == "failed" and not T.in_outage(part.get("t_start", r["t_start"]),
                                                                part.get("t_end", r["t_end"])):
                reasons.append(f"read failed: {r.get('label')} {name}")
            if part_state(part) == "not_found":
                reasons.append(f"record absent (NotFound): {r.get('label')} {name}")
    if T.gap_in(T.t_stop, end):
        reasons.append("receiver gap")
    sts = [describe_of(r)["status"] for r in reads if describe_of(r)]
    if len(sts) >= 2 and sts[-1] != sts[-2]:
        reasons.append("state still changing at the last read")
    if T.void:
        reasons.append("trial void")
    return [row("events", "five events", "info",
                f"accepted={acc}, state_changed={changed}, work_ceased={work_ceased} (bounded quiet={bounded_quiet}, "
                f"computation {comp['state']}), effects_closed={effects_closed} (obligation {ob['state']}), "
                f"evidence_sufficient={not reasons}",
                request_accepted=acc, state_changed=changed, work_ceased=work_ceased, bounded_quiet=bounded_quiet,
                computation_boundary=comp["state"], computation_reasons=comp["reasons"],
                durable_obligation=ob["state"], durable_obligation_status=ob["status"],
                effects_closed=effects_closed, unfinished_executions_after_request=unfinished,
                evidence_sufficient=not reasons, unresolved_reasons=sorted(set(reasons)))]


# ============================ restart outcomes ============================
def restart_outcome(T, t_start, t_ready, window_end, before_reads, after_reads, controls_ok, label, count_from=None):
    """C.6 restart outcome for one restart. Returns a row. count_from: for a server restart, the server's readiness
    (pre-freeze review): only effects arriving after it count toward resumed or re-run; effects between t_start and it
    were posted while the server was down, by processes the restart did not touch, and are reported as continued
    through the outage."""
    if not _num(t_start):
        return row("restart", label, "unresolved", "unresolved (no restart stamp)", reasons=["no restart stamp"])
    before = None
    for r in sorted(before_reads, key=lambda r: r["t_end"]):
        if r["t_end"] <= t_start and describe_of(r):
            before = r
    after = [r for r in sorted(after_reads, key=lambda r: r["t_end"]) if _num(t_ready) and r["t_start"] >= t_ready]
    d0 = describe_of(before) if before else None
    ds = [(r, describe_of(r)) for r in after]
    lost = [r.get("label") for r in after if part_state(r.get("parts", {}).get("describe_target")) == "not_found"]
    failed = [r.get("label") for r, d in ds if d is None and not T.in_outage(r["t_start"], r["t_end"])]
    start = count_from if _num(count_from) and count_from > t_start else t_start
    win = [e for e in T.declared if start < e["mono"] <= window_end]
    outage = [e for e in T.declared if t_start < e["mono"] <= start]
    before_nonces = {e.get("exec_nonce") for e in T.declared if e["mono"] <= start}
    resumed = sorted({e.get("exec_nonce") for e in win if e.get("exec_nonce") in before_nonces and e.get("exec_nonce")})
    rerun = sorted({e.get("exec_nonce") for e in win if e.get("exec_nonce") not in before_nonces and e.get("exec_nonce")})
    good = [d for _, d in ds if d]
    d1 = good[-1] if good else None
    kinds, reasons = [], []
    if resumed:
        kinds.append("resumed")
    if rerun:
        kinds.append("re-run")
    if d0 and d1 and d0["status"] == "RUNNING" and d1["status"] in CLOSED:
        kinds.append(f"closed after restart: {d1['status']}")
    if lost:
        reasons.append("state lost (NotFound after the restart)")
    if failed:
        reasons.append("read failed after the restart: " + ", ".join(map(str, failed)))
    if not controls_ok:
        reasons.append("capability job or persistence control failed")
    if len(good) >= 2 and good[-1]["status"] != good[-2]["status"]:
        reasons.append("state still changing at the last read")
    if d0 is None or d1 is None:
        reasons.append("no usable read before or after the restart")
    if T.gap_in(t_start, window_end):
        reasons.append("receiver gap")
    if not kinds:
        # "stayed ended" needs the history's last event ID unchanged (C.6). The "before" side is the latest closed
        # history read before the restart, which need not be the read that gave d0 (a claim-poll read has no
        # history); the "after" side is the latest usable history after readiness. Either one missing is missing
        # evidence, so the outcome is unresolved, never "stayed ended".
        h0 = None
        for r in sorted(before_reads, key=lambda r: r["t_end"]):
            h = history_of(r) if r["t_end"] <= t_start else None
            if h and h[-1]["type"] in CLOSING_EVENTS:
                h0 = h
        h1 = next((history_of(r) for r in reversed(after) if history_of(r)), None)
        ended = not reasons and d0["status"] in CLOSED and d1["status"] == d0["status"] and not win
        if ended and h0 is None:
            reasons.append("no closed history read before the restart")
        elif ended and h1 is None:
            reasons.append("no history read after the restart")
        elif ended and h0[-1]["id"] != h1[-1]["id"]:
            reasons.append(f"history's last event ID changed: {h0[-1]['id']} before, {h1[-1]['id']} after")
        kinds.append("stayed ended" if ended and not reasons else "unresolved")
    elif reasons and "stayed ended" not in kinds:
        reasons.append("positive outcome reported with incomplete evidence")
    kind = "unresolved" if kinds == ["unresolved"] else ", ".join(kinds)
    return row("restart", label, kind, kind + (f" ({'; '.join(reasons)})" if reasons else "")
               + (f" [{len(outage)} declared effect(s) continued through the outage]" if outage else ""),
               before=d0["status"] if d0 else None, after=d1["status"] if d1 else None,
               effects_in_window=len(win), continued_through_outage=len(outage), window_from_rel=T.rel(start),
               resumed=resumed, rerun=rerun, reasons=reasons)


def restart_rows(T):
    out = []
    for i, rs in enumerate(T.box.get("restarts") or []):
        caps = rs.get("capability") or []
        controls = bool(rs.get("ok")) and bool(caps) and all(c.get("ok") for c in caps)
        before = list(T.watch_reads)
        if T.cond == "C6":
            twin_end = next((describe_of(r, "describe_twin") for r in reversed(T.scheduled)
                             if describe_of(r, "describe_twin")), None)
            controls = controls and bool(twin_end) and twin_end["status"] == "COMPLETED"
            # the precondition polls read describe and show before the SIGTERM (C.2): the "before" side
            before += [r for r in ((T.box.get("precondition") or {}).get("reads") or []) if _num(r.get("t_end"))]
        out.append(restart_outcome(T, rs.get("t_start"), rs.get("t_ready"), T.watch_end, before,
                                   T.watch_reads, controls, f"in-watch restart {i + 1} ({rs.get('what')})",
                                   count_from=rs.get("t_ready") if rs.get("what") == "server" else None))
    P = T.probe
    if P:
        pre = P.get("pre_check") or {}
        if P.get("error") or not pre.get("ok"):
            out.append(row("restart", "durability probe", UNK,
                           f"UNKNOWN (probe {'error: ' + str(P.get('error')) if P.get('error') else 'pre-check failed'})",
                           pre_check=pre))
        else:
            caps = P.get("capability") or []
            controls = bool(caps) and all(c.get("ok") for c in caps) and P.get("ptwin_completed") is True and \
                all(x.get("ok") for x in P.get("restarts") or [])
            # our code failing during the probe (K8) is an instrument fault in the probe's controls (pre-freeze review)
            probe_faults = worker_faults(T, T.watch_end, P.get("end_mono") if _num(P.get("end_mono")) else None)
            controls = controls and not probe_faults
            before = T.watch_reads + ([pre["read"]] if isinstance(pre.get("read"), dict) else [])
            stop = P.get("stop_start_mono")
            server_ready = next((o.get("t_to") for o in P.get("outages") or [] if o.get("kind") == "server"), None)
            r = restart_outcome(T, stop, P.get("ready_mono"), P.get("end_mono"), before,
                                P.get("reads") or [], controls, "durability probe", count_from=server_ready)
            if probe_faults:
                r["evidence"]["worker_faults"] = probe_faults
            # C.4 step 6.2: running work is a STARTED activity on an open run (the harness's view) or a declared
            # effect in the 5 s before the probe stops anything (the receiver's view)
            recent = [e for e in T.declared if _num(stop) and stop - QUIET_WINDOW_S < e["mono"] <= stop]
            if "target" in (pre.get("interrupted") or []) or recent:
                r["evidence"]["probe_interrupted_running_work"] = True
                r["evidence"]["effects_before_probe_stop"] = len(recent)
                r["verdict"] += " [the probe interrupted running work: unpredicted]"
            elif T.receiver_unproven(stop - QUIET_WINDOW_S if _num(stop) else None, stop):
                # no effect seen in those 5 s, but the receiver record there is incomplete (C.5)
                r["evidence"]["probe_interrupted_running_work"] = UNK
                r["verdict"] += " [whether the probe interrupted running work is UNKNOWN: receiver gap]"
            if "target" in (pre.get("closed_run_lists_started") or []):
                r["evidence"]["closed_run_lists_started_activity"] = True
            out.append(r)
    return out


# ============================ condition rows: PC, C4, C6, C8, twin ============================
def pc_rows(T):
    if T.cond != "PC" or not T.kill:
        return []
    k = T.kill.get("t_send")
    if not _num(k):
        # the SIGKILL was never sent (an identity mismatch withholds it, C.1): the gate is not tested
        return [row("PC", "no effect after the kill (gate)", UNK, "UNKNOWN (no SIGKILL send stamp)")]
    # The gate is a watch measure: the durability probe restarts the target worker, and attempt 2's re-run there
    # is PC's predicted probe outcome (C.8), not a gate failure.
    watch = [e for e in T.declared if _num(T.watch_end) and e["mono"] <= T.watch_end]
    after_init = [e for e in watch if _num(e.get("smono")) and e["smono"] > k]
    late = [e for e in watch if e["mono"] > (T.t_stop or k) + 0.1 and _num(e.get("smono")) and e["smono"] >= k]
    if after_init or late:
        kind = "fail"                  # positive evidence; a receiver gap elsewhere cannot undo it
    else:
        kind = UNK if not T.receiver_ok or T.gap_in(k, T.watch_end) else "pass"
    return [row("PC", "no effect after the kill (gate)", kind,
                f"{len(after_init)} initiated after the kill; {len(late)} arrived late and not in flight")]


def _cancelled_failure(failure, depth=0):
    """A protobuf JSON Failure that is, or is caused by, a cancellation (canceledFailureInfo, api/failure/v1)."""
    if not isinstance(failure, dict) or depth > 8:
        return False
    return get(failure, "failure.canceled") is not None or _cancelled_failure(get(failure, "failure.cause"), depth + 1)


def c4_stage1(T):
    """C.6 C4 stage 1 (pre-freeze review): did workflow code act on the cancel before the local activity finished?
    Decided from what the workflow and the local activity did, never from the order of WorkflowExecutionCancelRequested
    and MarkerRecorded in the history: that order shows when the server wrote the request, and a cancel that arrives
    while a workflow task is open is buffered and written at the next task completion, which a long local activity
    forces at each workflow-task heartbeat (T8, T22, T24), so the request can precede the marker under either reading.
      (a) before the local activity finished: a cancellation acknowledgement of a local work execution in the watch
          (the local activity was cancelled before it returned); or the run's first closed describe (the claim) read
          CANCELED before the `final` effect arrived (or with no `final` in a gap-free watch: under K2 and K3, TRY_CANCEL,
          the run closes as soon as workflow code sees the cancel); or the work activity's marker records a
          cancellation.
      (b) after the local activity finished: the `final` effect landed, the worker log covers that execution and has no
          acknowledgement for it, no marker records a cancellation, and a describe that started after the `final`
          effect arrived still read RUNNING: the run was open after the local activity returned, so (K2, K3) workflow
          code had not yet acted on the cancel.
      otherwise UNKNOWN; "cancel never reached the history" when the last usable history has no CancelRequested and
      nothing shows (a).
    Returns (kind, evidence)."""
    lr, h = T.last_scheduled_with(history_of, T.watch_end + 5 if _num(T.watch_end) else None)
    local = [x for x in T.executions if x.get("is_local")] or T.executions
    nonces = {x["exec_nonce"] for x in local}
    finals = sorted(e["mono"] for x in local for e in x["effects"] if e.get("kind") == "final")
    final = finals[0] if finals else (math.inf if local and not T.receiver_unproven() else None)
    acks = sorted((w for w in T.watch_acks() if w.get("exec_nonce") in nonces), key=lambda w: w["smono"])
    logged = {w.get("exec_nonce") for w in T.worker_logs if w.get("event") == "activity_start"}
    markers = [e for e in h if e["type"] == "MarkerRecorded"] if h is not None else []
    marker_cancelled = any(_cancelled_failure(get(e["attrs"], "attrs.failure")) for e in markers)
    r_claim, t_claim, status, _ = claim(T, "O1")
    closed_before_final = bool(r_claim) and status == "CANCELED" and final is not None and t_claim < final
    open_after_final = [r.get("label") for r in T.watch_reads if finals and describe_of(r) and
                        describe_of(r)["status"] == "RUNNING" and _num(_part_start(r)) and _part_start(r) > finals[0]]
    ev = {"final_rel": T.rel(finals[0]) if finals else None, "claim_rel": T.rel(t_claim), "claim_status": status,
          "acknowledgement_rel": T.rel(acks[0]["smono"]) if acks else None, "marker_cancelled": marker_cancelled,
          "open_after_final": open_after_final, "worker_log_covers_execution": bool(nonces) and nonces <= logged}
    if acks or closed_before_final or marker_cancelled:
        why = ("acknowledgement" if acks else "closed before the final effect" if closed_before_final
               else "cancelled marker")
        return "(a) before the local activity finished", dict(ev, decided_by=why)
    if finals and open_after_final and ev["worker_log_covers_execution"]:
        return "(b) after the local activity finished", dict(ev, decided_by="open after the final effect")
    if h is not None and not any(e["type"] == "WorkflowExecutionCancelRequested" for e in h):
        return "cancel never reached the history", ev
    return UNK, dict(ev, decided_by="neither reading has the evidence it needs")


def _part_start(r, name="describe_target"):
    part = (r or {}).get("parts", {}).get(name) or {}
    return part.get("t_start", (r or {}).get("t_start"))


def server_order_row(T):
    """C.6 C4, descriptive: did the server write WorkflowExecutionCancelRequested before the work activity's marker?
    Server metadata only; it never decides stage 1 (a buffered request is written at the next task completion)."""
    _, h = T.last_scheduled_with(history_of, T.watch_end + 5 if _num(T.watch_end) else None)
    if h is None:
        return row("C4", "server recorded the cancel request before the marker", UNK, "no usable history read")
    cr = next((e["id"] for e in h if e["type"] == "WorkflowExecutionCancelRequested"), None)
    mk = next((e["id"] for e in h if e["type"] == "MarkerRecorded"), None)
    kind = ("cancel request not in the history" if cr is None else "no marker" if mk is None else
            "yes" if cr < mk else "no")
    return row("C4", "server recorded the cancel request before the marker", kind,
               f"CancelRequested event {cr}, first MarkerRecorded event {mk} (server write order; metadata)",
               cancel_requested_event=cr, marker_event=mk)


def c4_rows(T):
    if T.cond != "C4":
        return []
    stage1, ev = c4_stage1(T)
    wfts = []
    for r in T.watch_reads:
        d = describe_of(r)
        if d:
            wfts.append({"rel": T.rel(call_end(r, "describe_target")), "wft": d.get("pending_wft"),
                         "cancel_requested": d.get("cancel_requested")})
    out = [row("C4", "stage 1: when the cancel reaches workflow code", stage1,
               f"{stage1} (decided by: {ev.get('decided_by', '-')})", per_read=wfts, **ev), server_order_row(T)]
    if stage1.startswith("(a)"):
        ack = next(iter(sorted(T.watch_acks(), key=lambda w: w["smono"])), None)
        final = any(x["final"] for x in T.executions)
        if ack and _num(ack.get("smono")):
            after = [e for e in T.work if e["mono"] > ack["smono"] + 1.5]
            last = max((e["mono"] for e in T.work), default=None)
            if after or final:
                kind = "did not stop"
            elif T.receiver_unproven(ack["smono"], T.watch_end):
                kind = UNK              # "stopped" is an absence of effects: it needs a gap-free receiver (C.5)
            else:
                kind = "stopped"
            out.append(row("C4", "stage 2: local activity stops without heartbeats", kind,
                           f"final={final}; {len(after)} effects more than 1.5 s after the acknowledgement",
                           ack_rel=T.rel(ack["smono"]), ack_to_last_s=round(last - ack["smono"], 3) if last else None))
        else:
            out.append(row("C4", "stage 2: local activity stops without heartbeats",
                           "did not stop" if final else UNK,
                           f"final={final}; no cancellation acknowledgement in the worker log"))
    else:
        out.append(row("C4", "stage 2: local activity stops without heartbeats", "not tested",
                       "stage 1 was not reading (a)"))
    return out


def c6_rows(T):
    if T.cond != "C6":
        return []
    pre = T.box.get("precondition") or {}
    out = [row("C6", "precondition: CANCELED before the SIGTERM",
               "pass" if pre.get("ok") else ("fail" if pre else UNK), str(pre.get("why", "")))]
    o = next((x for x in T.box.get("outages") or [] if x.get("kind") == "server"), None)
    if o and _num(o.get("t_from")):
        t1 = o.get("t_to") if _num(o.get("t_to")) else T.watch_end
        during = [e for e in T.work if o["t_from"] < e["mono"] <= t1]
        out.append(row("C6", "effects during the outage", UNK if T.gap_in(o["t_from"], t1) else "info",
                       f"{len(during)} work effects between the SIGTERM and readiness",
                       n=len(during), steps=[e.get("step") for e in during], outage_s=round(t1 - o["t_from"], 3)))
    out.append(final_landed_row("C6", T))
    after = [r for r in T.watch_reads if o and _num(o.get("t_to")) and r["t_start"] >= o["t_to"]]
    good = [(r, describe_of(r)) for r in after if describe_of(r)]
    if good:
        r1, d1 = good[-1]
        out.append(row("C6", "after the restart", d1["status"],
                       "pending after restart: " + str([(p.get("activity_id"), p.get("state"), p.get("attempt"),
                                                        p.get("last_failure")) for p in d1["pending"]]),
                       pending=[(p.get("activity_id"), p.get("state"), p.get("attempt"), p.get("last_failure"))
                                for p in d1["pending"]]))
    else:
        out.append(row("C6", "after the restart", UNK, "no usable describe after the server's readiness"))
    # C.6 C6 outage: the history's last event ID after the restart against the last closed history read before the
    # SIGTERM (a history read before the run closed cannot be compared, so it is not used)
    t_sig = o.get("t_from") if o else None
    pre_reads = list(T.watch_reads) + [r for r in (pre.get("reads") or []) if _num(r.get("t_end"))]
    before = [(r, history_of(r)) for r in sorted(pre_reads, key=lambda r: r["t_end"])
              if _num(t_sig) and call_end(r, "show_target") is not None and call_end(r, "show_target") < t_sig]
    before = [(r, h) for r, h in before if h and h[-1]["type"] in CLOSING_EVENTS]
    after_h = [(r, history_of(r)) for r in after]
    after_h = [(r, h) for r, h in after_h if h]
    if before and after_h:
        id0, id1 = before[-1][1][-1]["id"], after_h[-1][1][-1]["id"]
        kind = "unchanged" if id0 == id1 else "changed"
        verdict = f"last event ID {id0} before the SIGTERM ({before[-1][0].get('label')}), {id1} after"
    else:
        id0 = id1 = None
        kind = UNK
        verdict = ("no closed history read before the SIGTERM" if not before else
                   "no usable history read after the restart")
    out.append(row("C6", "last event ID across the restart", kind, verdict, before=id0, after=id1))
    return out


def c8_rows(T):
    if T.cond != "C8":
        return []
    rs = (T.box.get("restarts") or [{}])[0]
    r, t, status, evaluable = claim(T, "O1")
    ready, t_rs = rs.get("t_ready"), rs.get("t_start")
    if r is None:
        kind = "no claim" if evaluable else UNK
    elif not _num(t_rs):
        kind = UNK                      # no restart record: "before" or "after" it cannot be decided
    else:
        kind = "before the restart" if t < t_rs else "after the restart"
    return [row("C8", "claim relative to the restart", kind,
                f"claim {status} at rel {T.rel(t)} s; worker ready at rel {T.rel(ready)} s")]


def twin_rows(T):
    eff = [e for e in T.effects if T._is(e, "twin")]
    d = next((describe_of(r, "describe_twin") for r in reversed(T.scheduled) if describe_of(r, "describe_twin")), None)
    n = T.plan.get("n_steps")
    steps = sorted(e.get("step") for e in eff if e.get("kind") == "progress")
    ex = {e.get("exec_nonce") for e in eff if e.get("activity_id") == "work"}
    att = {e.get("attempt") for e in eff if e.get("activity_id") == "work"}
    ok = bool(d and d["status"] == "COMPLETED" and _counts(eff).get("accepted") == 1 and
              _counts(eff).get("final") == 1 and n and steps == list(range(1, n + 1)) and len(ex) == 1 and att == {1})
    # "once each" and "one execution" are absences of other effects: a pass needs a gap-free receiver from the first
    # tick to the end of the watch (C.9); a failure on positive evidence stands
    gap = T.receiver_unproven(T.ticks[0] if T.ticks else None, T.watch_end)
    kind = ("pass" if not gap else UNK) if ok else ("fail" if d else UNK)
    return [row("twin", "twin control", kind,
                f"status {d['status'] if d else '?'}; effects {_counts(eff)}; executions {len(ex)}; attempts "
                f"{sorted(att, key=str)}" + ("; receiver gap" if gap else ""))]


HEALTH_LABELS = ("start", "before t_stop", "end of watch")      # C.9; "end of probe" too when the probe ran


def health_ok(health, probe_ran):
    """C.9: /health answered 200 at start, before t_stop, at the end of the watch and (when the probe ran) at the
    end of the probe. Each required check must be present and every entry under its label ok; the harness records
    one entry per check (its start-up readiness polls are not checks)."""
    need = HEALTH_LABELS + (("end of probe",) if probe_ran else ())
    by = {}
    for h in health or []:
        by.setdefault(h.get("label"), []).append(h.get("ok") is True)
    return all(by.get(label) and all(by[label]) for label in need)


def readiness_and_capability(T):
    """C.9 "Readiness and capability jobs complete": every readiness job recorded by the end of the watch (the
    start-up's and the in-watch restarts'), every in-watch restart's capability jobs, and one completed end-of-watch
    capability job on each queue it covers (C.4 step 5: both queues; PC only the twin's, whose target worker is dead by
    design). A job may end after the watch (it has a readiness job's own bound). The durability probe's readiness and
    capability jobs are its controls: they decide its restart outcome (C.6), not the trial's validity (as R6.9).
    Returns (ok, problems)."""
    R, problems = T.R, []
    end = T.watch_end
    for r in R.get("readiness") or []:
        if not isinstance(r, dict):
            problems.append("an unreadable readiness entry")
            continue
        if _num(r.get("t_ready")) and _num(end) and r["t_ready"] > end:
            continue                                     # a durability-probe restart (C.4 step 6)
        if r.get("ok") is not True:
            problems.append(f"readiness {r.get('what')} did not complete")
    for i, rs in enumerate(T.box.get("restarts") or []):
        caps = rs.get("capability") or []
        if rs.get("ok") is not True or not caps or not all(isinstance(c, dict) and c.get("ok") is True for c in caps):
            problems.append(f"in-watch restart {i + 1} ({rs.get('what')}): readiness or capability job failed")
    queues = [T.twin.get("task_queue")] if T.cond == "PC" else [T.target.get("task_queue"), T.twin.get("task_queue")]
    caps = [c for c in R.get("capability") or [] if isinstance(c, dict)]
    for queue in queues:
        hits = [c for c in caps if c.get("queue") == queue]
        if len(hits) != 1 or hits[0].get("ok") is not True:
            problems.append(f"end-of-watch capability job on {queue}: "
                            + ("missing" if not hits else "failed" if len(hits) == 1 else f"{len(hits)} recorded"))
    return not problems, problems


# ============================ validity ============================
WORKER_FAULTS = ("activity_error", "worker_stopped_without_signal")     # jobs_c.py: our code failing (K8)


def worker_faults(T, after=None, upto=None):
    """Our code failing inside the system under test (pre-freeze review): an `activity_error` line (K8: a failed effect
    POST fails the activity, which Temporal then retries) or a worker that stopped without a signal, in any target or
    twin worker log, with its stamp in (after, upto]. A duplicate or re-run that follows one would be our construction,
    never Temporal's behaviour."""
    return [{"event": w.get("event"), "rel": T.rel(w.get("smono")), "identity": w.get("identity"),
             "workflow_id": w.get("workflow_id"), "error": w.get("error")}
            for w in T.worker_logs if w.get("event") in WORKER_FAULTS and _num(w.get("smono"))
            and (after is None or w["smono"] > after) and (upto is None or w["smono"] <= upto)]


def validity_rows(T):
    """C.9. The checks are C.9's: a clock-order violation only makes the by-initiation versions UNKNOWN (C.9 "Clock
    order"), and the tick canary is judged by its 2 s gap rule, so both are reported here as diagnostics only
    (pre-freeze review)."""
    R = T.R
    ticks = (R.get("controls") or {}).get("ticks") or {}
    health = (R.get("controls") or {}).get("health") or []
    checks = {
        "not void": not T.void,
        "clock": all((R.get(k) or {}).get("ok") is True for k in ("clock_start", "clock_end", "clock_across_trial")),
        "no guard violations": not R.get("guard_violations"),
        "receiver log present": T.receiver_ok,
        "no malformed effect posts": not T.bad_lines,
        "no receiver gap in the watch": _num(T.t_stop) and not T.gap_in(T.t_stop, T.watch_end),
        "receiver health checks": health_ok(health, bool(R.get("probe"))),
        "pre-intervention check": (R.get("pre_check") or {}).get("ok") is True,
        "nothing untested": not R.get("untested"),
        "request accepted or none": request_state(T) in ("accepted", "not applicable"),
        "readiness and capability jobs": readiness_and_capability(T)[0],
        "no worker-side effect failure in the watch": not worker_faults(T, None, T.watch_end),
    }
    if T.cond != "C6":
        # C.9: a failed twin makes the trial invalid; in C6 the twin is the persistence control instead, and its
        # failure makes the restart outcomes unresolved (restart_rows)
        checks["twin control"] = twin_rows(T)[0]["kind"] == "pass"
    harness_wall = (R.get("clock_across_trial") or {}).get("wall_clock")
    if evidence_schema(R):
        # C.9 (schema 2): the harness's receipt must be present and passing, and the recomputation must pass
        checks["wall clock recorded"] = isinstance(harness_wall, dict) and harness_wall.get("ok") is True
        checks["wall clock recomputed"] = T.wall_check.get("ok") is True
        # C.0, C.9 (schema 2): the declared host profile and its start host check (disk floor; linux-vm kernel)
        profile = R.get("host_profile")
        checks["host profile declared"] = isinstance(profile, dict) and profile.get("declared") in HOST_PROFILES
        checks["host checks at start"] = isinstance(R.get("host_checks_start"), dict) and \
            R["host_checks_start"].get("ok") is True
        # C.4 step 1, C.10 (pre-freeze review): the host lock was held and no other StopCheck trial, process,
        # namespace or unit was present
        checks["one StopCheck trial on the host"] = isinstance(R.get("host_exclusion"), dict) and \
            R["host_exclusion"].get("ok") is True
    wall_clock = {"policy": ("current (record schema 2 and later): the harness's receipt and the scorer's "
                             "recomputation from receiver.jsonl must both pass" if evidence_schema(R) else
                             "legacy (records before schema 2): post hoc diagnostic only; the recomputation from "
                             "receiver.jsonl is reported and never voids the trial"),
                  "harness_recorded": harness_wall, "scorer_recomputed": T.wall_check}
    flagged =[e.get("seq") for e in T.declared if _num(e.get("smono")) and e["mono"] - e["smono"] > CLOCK_ORDER_FLAG_S]
    valid = all(checks.values())
    return [row("validity", "trial validity", "valid" if valid else "invalid",
                "valid" if valid else "INVALID: " + ", ".join(k for k, v in checks.items() if not v),
                checks=checks, slow_arrivals_flagged=flagged,
                readiness_and_capability_problems=readiness_and_capability(T)[1], wall_clock=wall_clock,
                clock_order_ok=T.clock_order_ok(),
                tick_posts={"sent": ticks.get("sent"), "failed": ticks.get("failed")},
                worker_faults=worker_faults(T, None, T.watch_end))]


def score_trial(T):
    rows = []
    for fn in (validity_rows, m1_rows, m2_rows, m3_rows, m4_rows, m5_rows, pending_on_closed_rows,
               history_coverage_rows, fac_rows, five_events, verified_stop_rows, restart_rows, pc_rows, c4_rows,
               c6_rows, c8_rows, twin_rows):
        rows += fn(T)
    for r in rows:
        r["trial"], r["condition"] = T.trial, T.cond
    return rows


def summarize(T, rows):
    def find(measure, outcome):
        return next((r for r in rows if r["measure"] == measure and r["outcome"] == outcome), None)
    kind = T.R.get("run_kind")
    label = ("smoke (shortened timing; never scored)" if T.R.get("fast_mode") else
             "pilot (unscored)" if kind == "pilot" else "exploratory screen" if kind == "screen" else "unlabelled run")
    L = [f"# Experiment C trial {T.trial} ({T.cond}): {label}", ""]
    for measure, outcome in (("validity", "trial validity"), ("FAC", "headline O2 x by commit"),
                             ("FAC", "headline O1 x by commit"), ("M2", "claim"), ("M2", "terminal status"),
                             ("M3", "final landed"), ("M4", "duplicates"), ("M4", "duplicates visible in Temporal"),
                             ("history", "history omits an execution that produced effects"),
                             ("pending_on_closed", "a closed run lists a pending activity"),
                             ("events", "five events"), ("verified_stop", "verified stop"), ("twin", "twin control")):
        r = find(measure, outcome)
        L.append(f"- {measure} / {outcome}: {r['kind'] + ' - ' + r['verdict'] if r else '?'}")
    for r in rows:
        if r["measure"] in ("M1", "restart", "C4", "C6", "C8", "PC"):
            L.append(f"- {r['measure']} / {r['outcome']}: {r['kind']} - {r['verdict']}")
    wall, harness = T.wall_check, (T.R.get("clock_across_trial") or {}).get("wall_clock")
    L.append("- wall/monotonic clock recomputed from receiver.jsonl: "
             + ("passed" if wall.get("ok") is True else "FAILED (" + "; ".join(wall.get("reasons") or ["?"]) + ")")
             + "; harness recorded: " + ("not recorded" if not isinstance(harness, dict) else
                                         "passed" if harness.get("ok") is True else "failed")
             + (" [a guard: record schema 2 and later]" if evidence_schema(T.R) else
                " [post hoc diagnostic only: a record before schema 2]"))
    return "\n".join(L) + "\n"


def load(folder):
    with open(os.path.join(folder, "record.json"), encoding="utf-8") as f:
        record = json.load(f)
    lines = _jsonl(os.path.join(folder, "receiver.jsonl"))
    logs = []
    for p in sorted(glob.glob(os.path.join(folder, "worker-*.jsonl"))):
        logs += _jsonl(p) or []
    return Trial(record, lines, logs, folder)


REPORTS = ("scored_c.jsonl", "summary_c.md")
POSTHOC = "derived-posthoc"          # <results>/derived-posthoc/<trial folder>/: the default report location


def report_folder(folder, out=None):
    """Where the reports for one trial folder go (C.12): --out, or by default <results>/derived-posthoc/<trial folder>/.
    Reports are derived files: a destination inside a folder that holds a record (a raw trial folder) is refused."""
    dest = os.path.abspath(out) if out else os.path.join(os.path.dirname(os.path.abspath(folder)), POSTHOC,
                                                         os.path.basename(os.path.abspath(folder)))
    probe = dest
    while True:
        if os.path.isfile(os.path.join(probe, "record.json")):
            raise SystemExit(f"{dest}: inside the raw trial folder {probe}; reports are derived and go elsewhere")
        parent = os.path.dirname(probe)
        if parent == probe:
            return dest
        probe = parent


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Score Experiment C trial folders (RULES-expC.md). Raw files are "
                                     "only read; the reports go outside the trial folder.")
    parser.add_argument("results", nargs="+", help="trial folders holding record.json and receiver.jsonl")
    parser.add_argument("--out", help="write scored_c.jsonl and summary_c.md to this directory (one trial only); "
                        f"default: <results>/{POSTHOC}/<trial folder>/")
    parser.add_argument("--overwrite", action="store_true", help="replace reports already at the destination")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.out and len(args.results) != 1:
        parser.error("--out takes exactly one trial folder")
    for folder in args.results:
        dest = report_folder(folder, args.out)
        existing = [n for n in REPORTS if os.path.exists(os.path.join(dest, n))]
        if existing and not args.overwrite:
            raise SystemExit(f"{dest}: {', '.join(existing)} already exist; pass --overwrite to replace them")
        T = load(folder)
        rows = score_trial(T)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, REPORTS[0]), "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        with open(os.path.join(dest, REPORTS[1]), "w", encoding="utf-8", newline="\n") as f:
            f.write(summarize(T, rows))
        print(f"{folder}: {T.trial} ({T.cond}) -> {dest}: {len(rows)} rows" + (f" [VOID: {T.void}]" if T.void else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
