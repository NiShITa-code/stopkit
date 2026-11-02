"""Experiment C scorer tests (temporal/analyze_c.py), offline only (RULES-expC.md C.12).

Each test builds a small synthetic trial (record + receiver lines + worker log) for one rule and checks the scored
row against a value written from RULES-expC.md (draft 3), never from the scorer's output. No server, worker or
socket is started.

Run:  python3 -B -m unittest tests.test_analyze_c      (STOPCHECK_ANALYZE_C=<path> tests another copy of the scorer,
as temporal/mutate_c.py does)
"""
import copy
import importlib.util
import os
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCORER = os.environ.get("STOPCHECK_ANALYZE_C", os.path.join(os.path.dirname(HERE), "temporal", "analyze_c.py"))
_spec = importlib.util.spec_from_file_location("analyze_c_under_test", SCORER)
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

T0 = 1000.0                 # actual t_stop (CLOCK_MONOTONIC seconds)
WALL0 = 1_760_000_000.0     # wall = WALL0 + mono
TGT = {"workflow_id": "wT", "run_id": "rT", "task_queue": "qT"}
TWIN = {"workflow_id": "wW", "run_id": "rW", "task_queue": "qW"}
PTW = {"workflow_id": "wP", "run_id": "rP", "task_queue": "qW"}
HEALTH_LABELS = ("start", "before t_stop", "end of watch")      # C.9; "end of probe" when the probe ran


def iso(mono):
    return datetime.fromtimestamp(WALL0 + mono, timezone.utc).isoformat().replace("+00:00", "Z")


def describe(status, pending=(), cancel_requested=False, wft=None, style="proto"):
    """describe --raw JSON. style "proto" uses the prefixed enum form, "cli" the PascalCase form (T25)."""
    st = f"WORKFLOW_EXECUTION_STATUS_{status}" if style == "proto" else status.capitalize()
    j = {"workflowExecutionInfo": {"execution": {"workflowId": "wT", "runId": "rT"}, "status": st,
                                   "historyLength": "9"},
         "workflowExtendedInfo": {"cancelRequested": True} if cancel_requested else {}}
    if pending:
        # (activity id, state, attempt) or (..., last heartbeat time on the monotonic clock)
        j["pendingActivities"] = [dict({"activityId": p[0], "state": f"PENDING_ACTIVITY_STATE_{p[1]}", "attempt": p[2]},
                                       **({"lastHeartbeatTime": iso(p[3])} if len(p) > 3 else {}))
                                  for p in pending]
    if wft:
        j["pendingWorkflowTask"] = {"state": f"PENDING_WORKFLOW_TASK_STATE_{wft}", "attempt": 1}
    return j


def history(types, t_end, attrs=None):
    attrs = attrs or {}
    return {"events": [dict({"eventId": str(i + 1), "eventType": t, "eventTime": iso(t_end - 1)}, **attrs.get(i, {}))
                       for i, t in enumerate(types)]}


def listing(status, wf="wT"):
    return [{"execution": {"workflowId": wf, "runId": "r"}, "status": f"WORKFLOW_EXECUTION_STATUS_{status}"}]


def part(j, t_end, rc=0, stderr=""):
    return {"t_start": t_end - 0.05, "t_end": t_end, "wall_end": WALL0 + t_end, "rc": rc, "timed_out": False,
            "json": j if rc == 0 else None, "stderr_tail": stderr}


def sched(offset, status, pending=(), hist=None, list_status=None, twin="RUNNING", cancel_requested=False,
          rc=0, stderr="", wft=None, style="proto", hist_attrs=None):
    t = T0 + offset + 0.2
    h = hist if hist is not None else ["WorkflowExecutionStarted", "ActivityTaskScheduled"] + (
        ["WorkflowExecutionCanceled"] if status == "CANCELED" else [])
    return {"label": f"+{offset:g}", "kind": "scheduled", "offset": offset, "t_start": t - 0.1, "t_end": t,
            "wall_end": WALL0 + t,
            "parts": {"describe_target": part(describe(status, pending, cancel_requested, wft, style), t, rc, stderr),
                      "describe_twin": part(describe(twin), t),
                      "show_target": part(history(h, t, hist_attrs) if isinstance(h, list) else h, t, rc, stderr),
                      "list_target": part(listing(list_status or status), t, rc, stderr)}}


# A closed C1 history as the CLI writes it: the announce activity ran, the timer was cancelled, the work activity was
# never scheduled (DESIGN-expC section 3)
C1_TYPES = ["WorkflowExecutionStarted", "ActivityTaskScheduled", "ActivityTaskStarted", "ActivityTaskCompleted",
            "TimerStarted", "WorkflowExecutionCancelRequested", "TimerCanceled", "WorkflowExecutionCanceled"]
C1_ATTRS = {1: {"activityTaskScheduledEventAttributes": {"activityId": "announce"}},
            2: {"activityTaskStartedEventAttributes": {"scheduledEventId": "2", "attempt": 1}}}


def c1_read(offset, status="CANCELED", **kw):
    return sched(offset, status, hist=C1_TYPES, hist_attrs=C1_ATTRS, **kw)


def poll(n, t, status, pending=(), rc=0, stderr="", cancel_requested=False):
    return {"label": f"poll-{n}", "kind": "poll", "t_start": t - 0.05, "t_end": t,
            "parts": {"describe_target": part(describe(status, pending, cancel_requested), t, rc, stderr)}}


WORKER_PID = 4242          # the pid on every effect of the first target worker (the effect record's `pid`)


def eff(mono, kind, step=None, nonce="n1", attempt=1, role="target", ids=TGT, activity_id="work", smono=None,
        is_local=False, pid=WORKER_PID):
    return {"route": "effect", "seq": 0, "t": WALL0 + mono, "mono": mono, "raw": mono, "trial": "SYN", "role": role,
            "workflow_id": ids["workflow_id"], "run_id": ids["run_id"], "activity_id": activity_id,
            "activity_type": "work_x", "attempt": attempt, "is_local": is_local, "exec_nonce": nonce, "step": step,
            "kind": kind, "pid": pid, "smono": mono - 0.01 if smono is None else smono}


def work_run(start, steps, nonce="n1", attempt=1, final=True, is_local=False, pid=WORKER_PID):
    out = [eff(start + i, "progress", i + 1, nonce, attempt, is_local=is_local, pid=pid) for i in range(steps)]
    if final:
        out.append(eff(start + 20, "final", 20, nonce, attempt, is_local=is_local, pid=pid))
    return out


def drop_target_work(lines):
    """The receiver lines without the target's work effects (the twin's stay, so its control can pass)."""
    return [x for x in lines if not (x.get("activity_id") == "work" and x.get("role") == "target")]


def sigkill(t=T0, pid=WORKER_PID, exit_after=0.01):
    """The procedure's SIGKILL of the target worker, as harness_c.Procs.signal records it."""
    return {"name": "sigkill_worker", "signal": "SIGKILL", "pid": pid, "t_send": t, "t_return": t + 0.001, "rc": 0,
            "exit_mono": t + exit_after if exit_after is not None else None}


def twin_effects(start=T0 - 40):
    out = [eff(start, "accepted", None, "tw0", role="twin", ids=TWIN, activity_id="announce")]
    out += [eff(start + 31 + i, "progress", i + 1, "tw1", role="twin", ids=TWIN) for i in range(20)]
    out.append(eff(start + 51, "final", 20, "tw1", role="twin", ids=TWIN))
    return out


class Syn:
    """A synthetic C3 trial by default: NH activity started at T0 - 4.5 (step 1 at T0 - 4.5), cancel at T0
    returning at T0 + 0.1; the activity keeps posting; the run reads CANCELED from the first poll."""

    def __init__(self, cond="C3", variant="NH", procedure="cancel"):
        self.R = {
            "trial": "SYN", "condition": cond, "run_kind": "screen", "fast_mode": False,
            "spec": {"variant": variant, "procedure": procedure},
            "plan": {"watch_s": 120.0, "n_steps": 20, "delivery_bound_step": 14, "step_s": 1.0, "hb_timeout_s": 5.0,
                     "anchor_step": 4, "stop_after_anchor_s": 5.5},
            "ids": {"target": dict(TGT), "twin": dict(TWIN), "ptwin": dict(PTW)},
            "intervention": {"t_stop": {"mono": T0}, "restarts": [], "outages": [], "worker_dead": [],
                             "actions": [{"name": "cancel", "t_send": T0, "t_return": T0 + 0.1, "rc": 0,
                                          "timed_out": False}]},
            "watch_end_mono": T0 + 120, "pre_check": {"ok": True}, "untested": [], "guard_violations": [],
            "clock_start": {"ok": True}, "clock_end": {"ok": True}, "clock_across_trial": {"ok": True},
            # as harness_c.TrialRun.health writes them: one entry per C.9 check, by label
            "controls": {"ticks": {"sent": 400, "failed": 0},
                         "health": [{"label": lb, "ok": True, "mono": T0} for lb in HEALTH_LABELS]},
            "capability": [{"queue": "qT", "ok": True}, {"queue": "qW", "ok": True}],
            "claim_poll": [poll(1, T0 + 0.6, "CANCELED", pending=(("work", "STARTED", 1),), cancel_requested=True)],
            "reads": [sched(o, "CANCELED", pending=(("work", "STARTED", 1),), cancel_requested=True,
                            twin="RUNNING" if o < 60 else "COMPLETED")
                      for o in (0, 2, 5, 10, 20, 30, 60, 90, 120)],
            "probe": None,
        }
        self.lines = [{"route": "effect", "role": "tick", "kind": "tick", "mono": float(t), "raw": float(t),
                       "t": WALL0 + float(t), "seq": 0} for t in range(int(T0) - 60, int(T0) + 300)]
        self.lines += [eff(T0 - 40, "accepted", None, "a0", activity_id="announce")]
        self.lines += work_run(T0 - 4.5, 20)
        self.lines += twin_effects()
        self.logs = []

    def trial(self):
        return A.Trial(copy.deepcopy(self.R), copy.deepcopy(self.lines), copy.deepcopy(self.logs))


def current_record(s, profile="wsl"):
    """Make a synthetic trial a current-schema record (2, pre-freeze batch), as harness_c now writes one: the wall/
    monotonic receipt (item 5), the declared host profile and its passing start host check (item 6)."""
    s.R["schema_version"] = 2
    s.R["clock_across_trial"] = {"ok": True, "wall_clock": {"ok": True, "policy": "harness"}}
    s.R["host_profile"] = {"declared": profile, "variable": "STOPCHECK_HOST_PROFILE"}
    s.R["host_checks_start"] = {"ok": True, "violations": [], "disk": {"ok": True, "free_gb": 50.0}}
    # pre-freeze review: one StopCheck trial on the host (C.4 step 1, C.10), held and checked by the harness
    s.R["host_exclusion"] = {"ok": True, "lock": {"ok": True}, "processes": [], "namespaces": [], "units": {"ok": True}}
    return s


def rows_of(T):
    return A.score_trial(T)


def find(rows, measure, outcome):
    hit = [r for r in rows if r["measure"] == measure and r["outcome"] == outcome]
    assert hit, f"no row {measure}/{outcome}: {[r['outcome'] for r in rows if r['measure'] == measure]}"
    return hit[0]


class FieldMap(unittest.TestCase):
    """C.1: both enum styles (T25) and protobuf JSON omissions map to the same rule names."""

    def test_status_both_styles(self):
        self.assertEqual(A.norm_describe(describe("CANCELED"))["status"], "CANCELED")
        self.assertEqual(A.norm_describe(describe("CANCELED", style="cli"))["status"], "CANCELED")
        self.assertEqual(A.enum_upper("ContinuedAsNew", "WORKFLOW_EXECUTION_STATUS_"), "CONTINUED_AS_NEW")
        self.assertEqual(A.enum_upper(4, "X", A.STATUS_INT), "CANCELED")

    def test_event_types_both_styles(self):
        self.assertEqual(A.event_type("EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED"),
                         "WorkflowExecutionCancelRequested")
        self.assertEqual(A.event_type("ActivityTaskStarted"), "ActivityTaskStarted")

    def test_omitted_fields(self):
        d = A.norm_describe(describe("RUNNING"))
        self.assertEqual(d["pending"], [])           # an omitted repeated field is empty
        self.assertIs(d["cancel_requested"], False)  # an omitted bool inside a present message is false
        j = describe("RUNNING")
        del j["workflowExtendedInfo"]
        self.assertIsNone(A.norm_describe(j)["cancel_requested"])   # no message at all: unknown
        self.assertIsNone(A.norm_describe({"nope": 1}))

    def test_timestamps(self):
        self.assertAlmostEqual(A.parse_time("2026-10-12T18:00:00.123456789Z"),
                               datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc).timestamp() + 0.123456789, 6)
        self.assertEqual(A.parse_time({"seconds": "10", "nanos": 500000000}), 10.5)

    def test_part_states(self):
        self.assertEqual(A.part_state(part({"a": 1}, 1.0)), "ok")
        self.assertEqual(A.part_state(part(None, 1.0, rc=1, stderr="workflow not found for ID")), "not_found")
        self.assertEqual(A.part_state(part(None, 1.0, rc=1, stderr="connection refused")), "failed")
        self.assertEqual(A.part_state(None), "missing")


class FalseAllClear(unittest.TestCase):
    def test_c3_o1_headline_is_false_all_clear(self):
        # C.6: wrong if a declared effect commits in (claim time, end of watch]; steps 6-20 and final follow
        rows = rows_of(Syn().trial())
        self.assertEqual(find(rows, "FAC", "headline O1 x by commit")["kind"], A.FAC)

    def test_c3_o2_never_claimed_when_closed_run_lists_pending(self):
        # C.6: O2 needs "describe lists no pending activity"; "no O2 claim" is its own outcome
        rows = rows_of(Syn().trial())
        self.assertEqual(find(rows, "FAC", "headline O2 x by commit")["kind"], "no O2 claim")
        self.assertEqual(find(rows, "pending_on_closed", "a closed run lists a pending activity")["kind"], "yes")

    def test_c3_o2_claimed_when_pending_cleared(self):
        s = Syn()
        s.R["reads"] = [sched(o, "CANCELED") for o in (0, 2, 5, 10, 20, 30, 60, 90, 120)]
        s.R["claim_poll"] = [poll(1, T0 + 0.6, "CANCELED")]
        rows = rows_of(s.trial())
        h = find(rows, "FAC", "headline O2 x by commit")
        self.assertEqual(h["kind"], A.FAC)
        self.assertAlmostEqual(h["evidence"]["claim_rel"], 0.1, places=3)      # the +0 read, 0.2 s after t_stop
        self.assertEqual(find(rows, "pending_on_closed", "a closed run lists a pending activity")["kind"], "no")

    def test_c1_honest_status(self):
        s = Syn("C1", "HB")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"]      # never scheduled
        s.R["reads"] = [sched(o, "CANCELED", hist=["WorkflowExecutionStarted", "TimerStarted", "TimerCanceled",
                                                   "WorkflowExecutionCanceled"]) for o in (0, 2, 5, 10, 120)]
        s.R["claim_poll"] = [poll(1, T0 + 0.6, "CANCELED")]
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "FAC", "headline O1 x by commit")["kind"], A.NO_FAC)
        self.assertEqual(find(rows, "FAC", "headline O2 x by commit")["kind"], A.NO_FAC)

    def test_receiver_gap_after_claim_is_unknown(self):
        # C.6: UNKNOWN on a receiver gap after the claim
        s = Syn("C1", "HB")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work" and
                   not (x.get("kind") == "tick" and T0 + 30 < x["mono"] < T0 + 40)]
        s.R["claim_poll"] = [poll(1, T0 + 0.6, "CANCELED")]
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "FAC", "headline O1 x by commit")["kind"], A.UNK)

    def test_claim_poll_sets_claim_time(self):
        # C.6 M2: the claim is the first closed read, poll or scheduled; its time is that call's reply stamp.
        # The +0 scheduled read (T0 + 0.2) still reads RUNNING, so the first closed read is the poll at T0 + 0.6.
        s = Syn()
        s.R["reads"][0] = sched(0, "RUNNING", pending=(("work", "STARTED", 1),), cancel_requested=True)
        rows = rows_of(s.trial())
        c = find(rows, "M2", "claim")
        self.assertEqual(c["kind"], "CANCELED")
        self.assertEqual(c["evidence"]["label"], "poll-1")
        self.assertAlmostEqual(c["evidence"]["claim_rel"], 0.5, places=3)     # poll at T0 + 0.6, t_return T0 + 0.1

    def test_scheduled_read_before_poll_sets_claim_time(self):
        # the same rule when the +0 scheduled read (reply at T0 + 0.2) is already closed: it is the first claim
        c = find(rows_of(Syn().trial()), "M2", "claim")
        self.assertEqual(c["evidence"]["label"], "+0")
        self.assertAlmostEqual(c["evidence"]["claim_rel"], 0.1, places=3)

    def test_earlier_unevaluable_o2_read_makes_no_fac_headline_unknown(self):
        s = Syn("C1", "HB")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"]
        s.R["claim_poll"] = []
        reads = [sched(o, "CANCELED") for o in (0, 2, 120)]
        reads[0]["parts"]["list_target"] = part(None, reads[0]["t_end"], rc=1, stderr="deadline exceeded")
        s.R["reads"] = reads
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "FAC", "headline O2 x by commit")["kind"], A.UNK)
        self.assertEqual(find(rows, "FAC", "headline O1 x by commit")["kind"], A.NO_FAC)

    def test_void_trial_is_unknown(self):
        s = Syn()
        s.R["void"] = "clock guard failed"
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "FAC", "headline O1 x by commit")["kind"], A.UNK)
        self.assertEqual(find(rows, "validity", "trial validity")["kind"], "invalid")


class EffectsAfterRequest(unittest.TestCase):
    def test_by_commit_initiation_and_ambiguous(self):
        # C.6 M1: by commit = arrival after t_return; by initiation = smono after t_return; ambiguous = arrival in
        # (t_send, t_return]
        s = Syn("C1", "HB")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"]
        s.lines.append(eff(T0 + 0.05, "progress", 1, "nA"))                    # ambiguous
        s.lines.append(eff(T0 + 0.3, "progress", 2, "nA", smono=T0 + 0.05))   # in flight: committed, not initiated
        s.lines.append(eff(T0 + 1.3, "progress", 3, "nA"))                    # initiated and committed after
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "M1", "by commit, from the cancel")["evidence"]["n"], 2)
        self.assertEqual(find(rows, "M1", "by initiation, from the cancel")["evidence"]["n"], 1)
        self.assertEqual(find(rows, "M1", "ambiguous, from the cancel")["evidence"]["n"], 1)

    def test_c8_counts_from_kill_and_cancel(self):
        s = Syn("C8", "HB", "kill_cancel_restart")
        s.R["intervention"]["actions"] = [
            {"name": "sigkill_worker", "t_send": T0, "t_return": T0 + 0.001, "rc": 0, "exit_mono": T0 + 0.01},
            {"name": "cancel", "t_send": T0 + 3, "t_return": T0 + 3.1, "rc": 0, "timed_out": False}]
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 5, final=False)
        s.lines += work_run(T0 + 21, 2, nonce="n2", attempt=2, final=False)
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "M1", "by commit, from the kill")["evidence"]["n"], 2)
        self.assertEqual(find(rows, "M1", "by commit, from the cancel")["evidence"]["n"], 2)

    def test_effects_closed_by_completion(self):
        # C.5 event 4: effects after the request, then the final effect -> "closed by completion, not by the stop"
        ev = find(rows_of(Syn().trial()), "events", "five events")["evidence"]
        self.assertEqual(ev["effects_closed"], "closed by completion, not by the stop")
        self.assertEqual(ev["request_accepted"], "accepted")

    def test_failed_request_is_never_accepted(self):
        s = Syn()
        s.R["intervention"]["actions"][0].update(rc=1)
        ev = find(rows_of(s.trial()), "events", "five events")["evidence"]
        self.assertEqual(ev["request_accepted"], "failed")


class ReadsAndOutages(unittest.TestCase):
    def test_failed_read_outside_outage_is_unknown_and_insufficient(self):
        s = Syn()
        s.R["reads"][3] = sched(10, "CANCELED", rc=1, stderr="context deadline exceeded")
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "M5", "status vs reality +10")["kind"], A.UNK)
        ev = find(rows, "events", "five events")["evidence"]
        self.assertFalse(ev["evidence_sufficient"])
        self.assertTrue(any("read failed: +10" in r for r in ev["unresolved_reasons"]))

    def test_failed_read_in_declared_outage_is_declared(self):
        s = Syn()
        s.R["reads"][3] = sched(10, "CANCELED", rc=1, stderr="connection refused")
        s.R["intervention"]["outages"] = [{"kind": "server", "t_from": T0 + 5.5, "t_to": T0 + 14}]
        rows = rows_of(s.trial())
        r = find(rows, "M5", "status vs reality +10")
        self.assertEqual(r["verdict"], "SERVER_UNAVAILABLE_DUE_TO_INTERVENTION")
        ev = find(rows, "events", "five events")["evidence"]
        self.assertFalse(any("read failed" in x for x in ev["unresolved_reasons"]))

    def test_not_found_is_record_absent(self):
        s = Syn()
        s.R["reads"][8] = sched(120, "CANCELED", rc=1, stderr="workflow execution not found")
        ev = find(rows_of(s.trial()), "events", "five events")["evidence"]
        self.assertTrue(any("NotFound" in x for x in ev["unresolved_reasons"]))

    def test_status_vs_reality(self):
        # C.6 M5: closed with a declared effect in (t_r - 2, t_r] is false; closed and quiet is true
        rows = rows_of(Syn().trial())
        self.assertEqual(find(rows, "M5", "status vs reality +5")["kind"], "false")
        self.assertEqual(find(rows, "M5", "status vs reality +60")["kind"], "true")

    def test_stale_activity_state_when_worker_dead(self):
        s = Syn("PC", "NH", "kill_worker")
        s.R["intervention"]["actions"] = [{"name": "sigkill_worker", "t_send": T0, "t_return": T0 + 0.001,
                                           "rc": 0, "exit_mono": T0 + 0.01}]
        s.R["intervention"]["worker_dead"] = [{"role": "target", "t_from": T0 + 0.01, "t_to": None}]
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 5, final=False)
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "RUNNING", pending=(("work", "STARTED", 1),)) for o in (0, 2, 5)]
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "M5", "stale activity state +2")["kind"], "stale activity state")


class Duplicates(unittest.TestCase):
    def test_c7_duplicates_visible_through_attempts(self):
        s = Syn("C7", "HB", "kill_worker_restart")
        s.R["intervention"]["actions"] = [{"name": "sigkill_worker", "t_send": T0, "t_return": T0 + 0.001,
                                           "rc": 0, "exit_mono": T0 + 0.01}]
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"]
        s.lines += work_run(T0 - 4.5, 5, final=False) + work_run(T0 + 16, 20, nonce="n2", attempt=2)
        h = ["WorkflowExecutionStarted", "ActivityTaskScheduled", "ActivityTaskStarted", "ActivityTaskCompleted",
             "WorkflowExecutionCompleted"]
        attrs = {1: {"activityTaskScheduledEventAttributes": {"activityId": "work"}},
                 2: {"activityTaskStartedEventAttributes": {"scheduledEventId": "2", "attempt": 2}}}
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "COMPLETED" if o >= 60 else "RUNNING", hist=history(h, T0 + o + 0.2, attrs))
                        for o in (0, 2, 60, 120)]
        rows = rows_of(s.trial())
        d = find(rows, "M4", "duplicates")
        self.assertEqual(d["evidence"]["executions"], 2)
        self.assertEqual(d["evidence"]["duplicated_steps"], [1, 2, 3, 4, 5])
        self.assertEqual(d["evidence"]["finals"], 1)
        self.assertEqual(find(rows, "M4", "duplicates visible in Temporal")["kind"], "yes")
        # C.6 history coverage: only attempt 2 is recorded, so the attempt-1 execution is omitted
        self.assertEqual(find(rows, "history", "history omits an execution that produced effects")["kind"], "yes")

    def test_duplicates_by_nonce_when_attempts_repeat(self):
        # C.2: executions are told apart by exec_nonce even where the attempt number does not change (C7l)
        s = Syn("C7l", "LA", "kill_worker_restart")
        s.R["intervention"]["actions"] = [{"name": "sigkill_worker", "t_send": T0, "t_return": T0 + 0.001,
                                           "rc": 0, "exit_mono": T0 + 0.01}]
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"]
        s.lines += work_run(T0 - 4.5, 5, final=False, is_local=True)
        s.lines += work_run(T0 + 16, 20, nonce="n2", attempt=1, is_local=True)
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "COMPLETED", hist=["WorkflowExecutionStarted", "MarkerRecorded",
                                                    "WorkflowExecutionCompleted"]) for o in (60, 120)]
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "M4", "duplicates")["evidence"]["executions"], 2)
        # a local activity has no pending-activity attempts and no ActivityTaskStarted, and the scorer does not read
        # the attempt inside the one marker, so whether Temporal's record shows the duplicate is unknown (C.6 M4)
        self.assertEqual(find(rows, "M4", "duplicates visible in Temporal")["kind"], A.UNK)
        self.assertEqual(find(rows, "history", "history omits an execution that produced effects")["kind"], "yes")

    def test_c3_history_has_no_started_event(self):
        rows = rows_of(Syn().trial())
        self.assertEqual(find(rows, "history", "history omits an execution that produced effects")["kind"], "yes")


class Restarts(unittest.TestCase):
    def _c7(self):
        s = Syn("C7", "HB", "kill_worker_restart")
        s.R["intervention"]["actions"] = [{"name": "sigkill_worker", "t_send": T0, "t_return": T0 + 0.001,
                                           "rc": 0, "exit_mono": T0 + 0.01}]
        s.R["intervention"]["restarts"] = [{"what": "worker:target", "t_start": T0 + 15, "t_ready": T0 + 15.8,
                                            "ok": True, "capability": [{"ok": True}]}]
        s.lines = drop_target_work(s.lines)
        s.lines += work_run(T0 - 4.5, 5, final=False) + work_run(T0 + 16, 20, nonce="n2", attempt=2)
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "COMPLETED" if o >= 60 else "RUNNING", hist=["WorkflowExecutionStarted"] +
                              (["WorkflowExecutionCompleted"] if o >= 60 else []),
                              twin="COMPLETED" if o >= 60 else "RUNNING") for o in (0, 2, 5, 10, 20, 30, 60, 90, 120)]
        return s

    def test_c7_rerun_closed_after_restart(self):
        r = find(rows_of(self._c7().trial()), "restart", "in-watch restart 1 (worker:target)")
        self.assertEqual(r["kind"], "re-run, closed after restart: COMPLETED")

    def _probe(self, s, extra_effects=(), cap_ok=True, ptwin_ok=True, after_status="CANCELED", hist_after=None):
        t_stop_probe = T0 + 125
        pre = {"ok": True, "interrupted": [], "read": sched(124.5, "CANCELED")}
        reads = []
        for k in range(7):
            r = sched(130 + 10 * k, after_status, hist=hist_after)
            r["label"] = f"probe+{10 * k}"
            reads.append(r)
        s.R["probe"] = {"start_mono": T0 + 121, "pre_check": pre, "stop_start_mono": t_stop_probe,
                        "outages": [{"kind": "server", "t_from": T0 + 126, "t_to": T0 + 129}],
                        "restarts": [{"ok": True}, {"ok": True}],
                        "capability": [{"ok": cap_ok}, {"ok": True}], "ready_mono": T0 + 129.5, "reads": reads,
                        "ptwin_completed": ptwin_ok, "end_mono": T0 + 195}
        s.lines += list(extra_effects)
        return s

    def _quiet_c1(self):
        # C1: cancelled in its timer; the closed history shows the work activity was never scheduled; the twin is
        # COMPLETED by the end of the watch
        s = Syn("C1", "HB")
        s.lines = drop_target_work(s.lines)
        s.R["reads"] = [c1_read(o, twin="COMPLETED" if o >= 60 else "RUNNING") for o in (0, 2, 120)]
        return s

    def test_probe_stayed_ended(self):
        r = find(rows_of(self._probe(self._quiet_c1()).trial()), "restart", "durability probe")
        self.assertEqual(r["kind"], "stayed ended")

    def test_probe_resumed(self):
        s = self._probe(Syn(), extra_effects=[eff(T0 + 140, "progress", 3, "n1")])
        r = find(rows_of(s.trial()), "restart", "durability probe")
        self.assertIn("resumed", r["kind"])

    def test_probe_rerun(self):
        s = self._probe(self._quiet_c1(), extra_effects=[eff(T0 + 140, "progress", 1, "n9", attempt=2)])
        self.assertIn("re-run", find(rows_of(s.trial()), "restart", "durability probe")["kind"])

    def test_probe_unresolved_on_control_failure(self):
        s = self._probe(self._quiet_c1(), cap_ok=False)
        self.assertEqual(find(rows_of(s.trial()), "restart", "durability probe")["kind"], "unresolved")
        s = self._probe(self._quiet_c1(), ptwin_ok=False)
        self.assertEqual(find(rows_of(s.trial()), "restart", "durability probe")["kind"], "unresolved")

    def test_probe_state_lost(self):
        s = self._probe(self._quiet_c1())
        for r in s.R["probe"]["reads"]:
            r["parts"]["describe_target"] = part(None, r["t_end"], rc=1, stderr="workflow not found")
        r = find(rows_of(s.trial()), "restart", "durability probe")
        self.assertEqual(r["kind"], "unresolved")
        self.assertTrue(any("state lost" in x for x in r["evidence"]["reasons"]))

    def test_probe_interrupted_running_work(self):
        # C.4 step 6.2: a declared effect in the 5 s before the probe stops anything (stop at T0 + 125) is running
        # work, and the probe outcome is then unpredicted; a quiet closed target is not
        quiet = find(rows_of(self._probe(self._quiet_c1()).trial()), "restart", "durability probe")
        self.assertNotIn("probe_interrupted_running_work", quiet["evidence"])
        s = self._probe(self._quiet_c1(), extra_effects=[eff(T0 + 122, "progress", 9, "n7")])
        r = find(rows_of(s.trial()), "restart", "durability probe")
        self.assertTrue(r["evidence"]["probe_interrupted_running_work"])
        self.assertIn("unpredicted", r["verdict"])

    def test_probe_running_work_unknown_on_a_receiver_gap(self):
        # C.4 step 6.2: "a declared effect in the 5 s before the probe stops anything" cannot be ruled out while
        # the receiver had a gap there (the probe stops things at T0 + 125)
        s = drop_ticks(self._probe(self._quiet_c1()), T0 + 121, T0 + 126)
        r = find(rows_of(s.trial()), "restart", "durability probe")
        self.assertEqual(r["evidence"]["probe_interrupted_running_work"], A.UNK)

    def test_closed_run_listing_started_is_recorded_not_interrupted(self):
        s = self._probe(self._quiet_c1())
        s.R["probe"]["pre_check"]["closed_run_lists_started"] = ["target"]
        r = find(rows_of(s.trial()), "restart", "durability probe")
        self.assertTrue(r["evidence"]["closed_run_lists_started_activity"])
        self.assertNotIn("probe_interrupted_running_work", r["evidence"])
        self.assertEqual(r["kind"], "stayed ended")

    def test_probe_precheck_failure_is_unknown(self):
        s = self._probe(self._quiet_c1())
        s.R["probe"]["pre_check"]["ok"] = False
        self.assertEqual(find(rows_of(s.trial()), "restart", "durability probe")["kind"], A.UNK)

    def test_missing_histories_on_both_sides_are_never_stayed_ended(self):
        # C.6: "stayed ended" needs the history's last event ID unchanged; with no history read before or after the
        # restart that condition is missing evidence, so the outcome is unresolved
        s = self._probe(self._quiet_c1())
        for r in s.R["reads"] + s.R["probe"]["reads"] + [s.R["probe"]["pre_check"]["read"]]:
            del r["parts"]["show_target"]
        r = find(rows_of(s.trial()), "restart", "durability probe")
        self.assertEqual(r["kind"], "unresolved")

    def test_changed_last_event_id_is_not_stayed_ended(self):
        s = self._probe(self._quiet_c1(), hist_after=["WorkflowExecutionStarted", "ActivityTaskScheduled",
                                                      "WorkflowExecutionCanceled", "ActivityTaskTimedOut"])
        self.assertEqual(find(rows_of(s.trial()), "restart", "durability probe")["kind"], "unresolved")

    def test_c6_server_restart_compares_a_closed_history_read_before_the_sigterm(self):
        # C.6 restart outcome for C6's in-watch server restart. The latest read before the SIGTERM is the
        # describe-only claim poll (T0 + 0.6); the history comes from the latest read before the SIGTERM that has
        # one, the +0 read (T0 + 0.2, closed, last event ID 3), and the reads after readiness (T0 + 4.0) end at 3
        s = Syn("C6", "HB", "cancel_server_restart")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 5, final=False)
        s.R["intervention"]["outages"] = [{"kind": "server", "t_from": T0 + 1.1, "t_to": T0 + 4.0}]
        s.R["intervention"]["restarts"] = [{"what": "server", "t_start": T0 + 1.2, "t_ready": T0 + 4.0, "ok": True,
                                            "capability": [{"ok": True}, {"ok": True}]}]
        s.R["intervention"]["precondition"] = {"ok": True, "why": "CANCELED at c6-pre-1", "reads": []}
        r = find(rows_of(s.trial()), "restart", "in-watch restart 1 (server)")
        self.assertEqual(r["kind"], "stayed ended")


class ConditionRows(unittest.TestCase):
    def _c4(self, order, ack_offset=None, late_effects=False):
        s = Syn("C4", "LA")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"]
        if order == "a":
            s.lines += work_run(T0 - 4.5, 8, final=False, is_local=True)       # stops near T0 + 3.5
            if late_effects:
                s.lines += [eff(T0 + 9, "progress", 14, "n1", is_local=True)]
            h = ["WorkflowExecutionStarted", "WorkflowExecutionCancelRequested", "WorkflowExecutionCanceled"]
        else:
            # reading (b): the local activity runs to its final effect (T0 + 15.5) while the run stays open; a describe
            # that started after the final effect still reads RUNNING, and the run closes after it
            s.lines += work_run(T0 - 4.5, 20, is_local=True)
            h = ["WorkflowExecutionStarted", "MarkerRecorded", "WorkflowExecutionCancelRequested",
                 "WorkflowExecutionCanceled"]
            s.R["claim_poll"] = [poll(1, T0 + 0.6, "RUNNING"), poll(2, T0 + 16.0, "RUNNING"),
                                 poll(3, T0 + 16.5, "CANCELED")]
        s.R["reads"] = ([sched(o, "CANCELED", hist=h) for o in (0, 20, 120)] if order == "a" else
                        [sched(0, "RUNNING", hist=h[:1])] + [sched(o, "CANCELED", hist=h) for o in (20, 120)])
        # the target worker's log covers the local execution (activity_start), so a missing acknowledgement is known
        s.logs = [{"event": "activity_start", "workflow_id": "wT", "smono": T0 - 4.5, "exec_nonce": "n1",
                   "activity_id": "work", "is_local": True}]
        if ack_offset is not None:
            s.logs.append({"event": "activity_cancelled", "workflow_id": "wT", "smono": T0 + ack_offset,
                           "exec_nonce": "n1", "flags": {"cancel_requested": True}})
        return s

    def test_c4_stage1_reading_a_and_stage2_stopped(self):
        rows = rows_of(self._c4("a", ack_offset=3.4).trial())
        self.assertEqual(find(rows, "C4", "stage 1: when the cancel reaches workflow code")["kind"],
                         "(a) before the local activity finished")
        self.assertEqual(find(rows, "C4", "stage 2: local activity stops without heartbeats")["kind"], "stopped")

    def test_c4_stage2_did_not_stop(self):
        rows = rows_of(self._c4("a", ack_offset=3.4, late_effects=True).trial())
        self.assertEqual(find(rows, "C4", "stage 2: local activity stops without heartbeats")["kind"], "did not stop")

    def test_c4_stage1_reading_b_leaves_stage2_untested(self):
        rows = rows_of(self._c4("b").trial())
        self.assertEqual(find(rows, "C4", "stage 1: when the cancel reaches workflow code")["kind"],
                         "(b) after the local activity finished")
        self.assertEqual(find(rows, "C4", "stage 2: local activity stops without heartbeats")["kind"], "not tested")

    def test_c6_outage_rows(self):
        s = Syn("C6", "HB", "cancel_server_restart")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 9, final=False)
        s.R["intervention"]["precondition"] = {"ok": True, "why": "CANCELED at c6-pre-1"}
        s.R["intervention"]["outages"] = [{"kind": "server", "t_from": T0 + 1.1, "t_to": T0 + 4.0}]
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "C6", "precondition: CANCELED before the SIGTERM")["kind"], "pass")
        # steps 7, 8 at T0 + 1.5, T0 + 2.5 and step 9 at T0 + 3.5 fall inside (T0 + 1.1, T0 + 4.0]
        self.assertEqual(find(rows, "C6", "effects during the outage")["evidence"]["n"], 3)
        self.assertEqual(find(rows, "C6", "final landed")["kind"], "no")

    def _c6(self):
        s = Syn("C6", "HB", "cancel_server_restart")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 9, final=False)
        s.R["intervention"]["outages"] = [{"kind": "server", "t_from": T0 + 1.1, "t_to": T0 + 4.0}]
        s.R["intervention"]["precondition"] = {"ok": True, "why": "CANCELED at c6-pre-1", "reads": []}
        return s

    def test_c6_last_event_id_unchanged(self):
        # C.8 C6 "After the restart": the history's last event ID equals the one read before the SIGTERM. The +0
        # read (T0 + 0.2) is closed (3 events); the reads after readiness (T0 + 4.0) end at the same event ID 3.
        r = find(rows_of(self._c6().trial()), "C6", "last event ID across the restart")
        self.assertEqual(r["kind"], "unchanged")
        self.assertEqual((r["evidence"]["before"], r["evidence"]["after"]), (3, 3))

    def test_c6_last_event_id_changed(self):
        s = self._c6()
        grown = ["WorkflowExecutionStarted", "ActivityTaskScheduled", "WorkflowExecutionCanceled",
                 "ActivityTaskTimedOut"]
        for i, o in enumerate((0, 2, 5, 10, 20, 30, 60, 90, 120)):
            if o >= 5:
                s.R["reads"][i] = sched(o, "CANCELED", hist=grown)
        self.assertEqual(find(rows_of(s.trial()), "C6", "last event ID across the restart")["kind"], "changed")

    def test_c6_last_event_id_needs_a_closed_history_before_the_sigterm(self):
        # a history read taken before the run closed cannot be compared: UNKNOWN, never "unchanged"
        s = self._c6()
        s.R["claim_poll"] = []
        s.R["reads"][0] = sched(0, "RUNNING", hist=["WorkflowExecutionStarted", "ActivityTaskScheduled"])
        self.assertEqual(find(rows_of(s.trial()), "C6", "last event ID across the restart")["kind"], A.UNK)
        # a closed history from the precondition polls (before the SIGTERM) is used
        pre = sched(0.3, "CANCELED")
        pre["label"] = "c6-pre-2"
        s.R["intervention"]["precondition"]["reads"] = [pre]
        self.assertEqual(find(rows_of(s.trial()), "C6", "last event ID across the restart")["kind"], "unchanged")

    def test_pc_gate_ignores_the_probe_rerun(self):
        # C.8 PC: the gate concerns the watch; attempt 2 re-running on the probe's restarted worker (after the
        # watch end, T0 + 120) is PC's predicted probe outcome, not an effect after the kill in the watch
        s = Syn("PC", "NH", "kill_worker")
        s.R["intervention"]["actions"] = [sigkill()]
        s.R["intervention"]["worker_dead"] = [{"role": "target", "t_from": T0 + 0.01, "t_to": None}]
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 5, final=False)
        s.lines += work_run(T0 + 135, 20, nonce="n2", attempt=2, pid=5151)
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "PC", "no effect after the kill (gate)")["kind"], "pass")
        # the watch's duplicate count does not include the probe's execution (the probe is a treatment, C.4)
        self.assertEqual(find(rows, "M4", "duplicates")["evidence"]["executions"], 1)
        # the probe-phase effects do not stop the watch from being quiet at its end (C.5 event 3)
        self.assertTrue(find(rows, "events", "five events")["evidence"]["work_ceased"])

    def test_pc_gate(self):
        s = Syn("PC", "NH", "kill_worker")
        s.R["intervention"]["actions"] = [{"name": "sigkill_worker", "t_send": T0, "t_return": T0 + 0.001,
                                           "rc": 0, "exit_mono": T0 + 0.01}]
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 5, final=False)
        s.lines.append(eff(T0 + 0.05, "progress", 6, "n1", smono=T0 - 0.02))     # in flight: allowed
        self.assertEqual(find(rows_of(s.trial()), "PC", "no effect after the kill (gate)")["kind"], "pass")
        s.lines.append(eff(T0 + 1.5, "progress", 7, "n1", smono=T0 + 1.49))
        self.assertEqual(find(rows_of(s.trial()), "PC", "no effect after the kill (gate)")["kind"], "fail")

    def test_twin_control(self):
        rows = rows_of(Syn().trial())
        self.assertEqual(find(rows, "twin", "twin control")["kind"], "pass")
        s = Syn()
        s.lines = [x for x in s.lines if not (x.get("role") == "twin" and x.get("kind") == "final")]
        self.assertEqual(find(rows_of(s.trial()), "twin", "twin control")["kind"], "fail")


def drop_ticks(s, a, b):
    """Remove the tick canary's effects in (a, b): a receiver gap there (C.9)."""
    s.lines = [x for x in s.lines if not (x.get("kind") == "tick" and a < x["mono"] < b)]
    return s


class MissingEvidenceStaysUnknown(unittest.TestCase):
    """C.5: HTTP and RPC failures and missing evidence are UNKNOWN, never "stopped" or "none"."""

    def test_c4_stage2_stopped_needs_a_gap_free_receiver(self):
        s = ConditionRows()._c4("a", ack_offset=3.4)
        drop_ticks(s, T0 + 10, T0 + 20)          # no effect seen after the acknowledgement, but none could be seen
        self.assertEqual(find(rows_of(s.trial()), "C4", "stage 2: local activity stops without heartbeats")["kind"],
                         A.UNK)

    def test_final_not_landed_needs_a_gap_free_receiver(self):
        s = Syn("C2", "HB")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 8, final=False)
        self.assertEqual(find(rows_of(s.trial()), "M3", "final landed")["kind"], "no")
        drop_ticks(s, T0 + 12, T0 + 18)          # the final effect was due about T0 + 15.5 (DESIGN section 4)
        self.assertEqual(find(rows_of(s.trial()), "M3", "final landed")["kind"], A.UNK)

    def test_c6_final_not_landed_needs_a_gap_free_receiver(self):
        s = ConditionRows()._c6()
        self.assertEqual(find(rows_of(s.trial()), "C6", "final landed")["kind"], "no")
        drop_ticks(s, T0 + 12, T0 + 18)
        self.assertEqual(find(rows_of(s.trial()), "C6", "final landed")["kind"], A.UNK)

    def test_no_duplicates_needs_a_gap_free_receiver(self):
        s = drop_ticks(Syn(), T0 + 5, T0 + 12)
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "M4", "duplicates")["kind"], A.UNK)
        # positive evidence is kept: the C3 history omits the execution whatever the gap hides
        self.assertEqual(find(rows, "history", "history omits an execution that produced effects")["kind"], "yes")

    def test_history_records_every_execution_needs_a_gap_free_receiver(self):
        s = Restarts()._c7()
        h = ["WorkflowExecutionStarted", "ActivityTaskScheduled", "ActivityTaskStarted", "ActivityTaskCompleted",
             "WorkflowExecutionCompleted"]
        attrs = {1: {"activityTaskScheduledEventAttributes": {"activityId": "work"}},
                 2: {"activityTaskStartedEventAttributes": {"scheduledEventId": "2", "attempt": 2}}}
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + \
            work_run(T0 + 16, 20, nonce="n2", attempt=2)                    # only attempt 2 posted: recorded
        s.R["reads"] = [sched(o, "COMPLETED" if o >= 60 else "RUNNING", hist=history(h, T0 + o + 0.2, attrs))
                        for o in (0, 2, 60, 120)]
        self.assertEqual(find(rows_of(s.trial()), "history",
                              "history omits an execution that produced effects")["kind"], "no")
        drop_ticks(s, T0 + 3, T0 + 10)           # an execution could have posted unseen
        self.assertEqual(find(rows_of(s.trial()), "history",
                              "history omits an execution that produced effects")["kind"], A.UNK)

    def test_pc_gate_unknown_when_the_kill_was_never_sent(self):
        # an identity mismatch withholds the SIGKILL (C.1): the action has no t_send, and the gate is UNKNOWN
        s = Syn("PC", "NH", "kill_worker")
        s.R["intervention"]["actions"] = [{"name": "sigkill_worker", "signal": "SIGKILL", "pid": 4242,
                                           "identity_ok": False}]
        s.R["error"] = "RuntimeError: intervention thread failed: identity mismatch"
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 4.5, 20)
        self.assertEqual(find(rows_of(s.trial()), "PC", "no effect after the kill (gate)")["kind"], A.UNK)

    def test_c8_claim_without_a_restart_record_is_unknown(self):
        s = Syn("C8", "HB", "kill_cancel_restart")
        s.R["intervention"]["actions"] = [
            {"name": "sigkill_worker", "t_send": T0, "t_return": T0 + 0.001, "rc": 0, "exit_mono": T0 + 0.01},
            {"name": "cancel", "t_send": T0 + 3, "t_return": T0 + 3.1, "rc": 0, "timed_out": False}]
        s.R["intervention"]["restarts"] = []
        self.assertEqual(find(rows_of(s.trial()), "C8", "claim relative to the restart")["kind"], A.UNK)

    def test_failed_describe_before_the_first_claim_makes_no_fac_unknown(self):
        # C.6: a failed read is UNKNOWN. The +0 read failed, so the first claiming read is not known; "no false
        # all-clear" at the +2 read does not settle what an earlier claim would have scored
        s = Syn("C1", "HB")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"]
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(0, "CANCELED", rc=1, stderr="context deadline exceeded"), sched(2, "CANCELED"),
                        sched(120, "CANCELED")]
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "FAC", "headline O1 x by commit")["kind"], A.UNK)
        self.assertEqual(find(rows, "FAC", "headline O2 x by commit")["kind"], A.UNK)

    def test_failed_describe_before_a_false_all_clear_keeps_it(self):
        # effects after a later claim are also after any earlier one, so a false all-clear stands
        s = Syn()
        s.R["claim_poll"] = []
        s.R["reads"][0] = sched(0, "CANCELED", rc=1, stderr="context deadline exceeded")
        self.assertEqual(find(rows_of(s.trial()), "FAC", "headline O1 x by commit")["kind"], A.FAC)

    def test_pending_on_closed_unknown_when_no_describe_was_usable(self):
        s = Syn()
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "CANCELED", rc=1, stderr="context deadline exceeded") for o in (0, 2, 5)]
        self.assertEqual(find(rows_of(s.trial()), "pending_on_closed", "a closed run lists a pending activity")["kind"],
                         A.UNK)

    def test_cancel_not_seen_is_unknown_when_reads_failed(self):
        s = Syn("C2", "HB")                      # the default history has no WorkflowExecutionCancelRequested
        self.assertEqual(find(rows_of(s.trial()), "M2", "cancel first in history")["kind"], "not seen")
        s.R["reads"][1] = sched(2, "CANCELED", rc=1, stderr="context deadline exceeded")
        self.assertEqual(find(rows_of(s.trial()), "M2", "cancel first in history")["kind"], A.UNK)

    def test_cancel_requested_not_seen_is_unknown_without_extended_info(self):
        s = Syn("C2", "HB")
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "CANCELED") for o in (0, 2, 120)]
        self.assertEqual(find(rows_of(s.trial()), "M2", "cancel_requested first seen")["kind"], "not seen")
        del s.R["reads"][1]["parts"]["describe_target"]["json"]["workflowExtendedInfo"]   # the flag is not known
        self.assertEqual(find(rows_of(s.trial()), "M2", "cancel_requested first seen")["kind"], A.UNK)

    def test_worker_log_acknowledgements_count_only_the_watch(self):
        # C.6: the watch measures stop at the end of the watch; the probe is a treatment
        s = Syn("C2", "HB")
        s.logs = [{"event": "activity_cancelled", "workflow_id": "wT", "smono": T0 + 3.6, "exec_nonce": "n1",
                   "flags": {"not_found": True}},
                  {"event": "activity_cancelled", "workflow_id": "wT", "smono": T0 + 130, "exec_nonce": "n9",
                   "flags": {"worker_shutdown": True}}]
        acks = find(rows_of(s.trial()), "M3", "worker-log acknowledgements (C diagnostic)")["evidence"]["acks"]
        self.assertEqual([a["exec_nonce"] for a in acks], ["n1"])


class Controls(unittest.TestCase):
    """C.9: a trial is valid only if its applicable controls pass."""

    def validity(self, s):
        return find(rows_of(s.trial()), "validity", "trial validity")

    def test_health_needs_every_required_check(self):
        self.assertEqual(self.validity(Syn())["kind"], "valid")
        s = Syn()
        s.R["controls"]["health"] = [h for h in s.R["controls"]["health"] if h["label"] != "end of watch"]
        self.assertFalse(self.validity(s)["evidence"]["checks"]["receiver health checks"])
        s = Syn()
        s.R["controls"]["health"][1]["ok"] = False                       # before t_stop
        self.assertFalse(self.validity(s)["evidence"]["checks"]["receiver health checks"])

    def test_end_of_probe_health_needed_when_the_probe_ran(self):
        s = Restarts()._probe(Restarts()._quiet_c1())
        self.assertFalse(self.validity(s)["evidence"]["checks"]["receiver health checks"])
        s.R["controls"]["health"].append({"label": "end of probe", "ok": True, "mono": T0 + 200})
        self.assertTrue(self.validity(s)["evidence"]["checks"]["receiver health checks"])

    def test_failed_twin_invalidates_the_trial(self):
        s = Syn()
        s.lines = [x for x in s.lines if not (x.get("role") == "twin" and x.get("kind") == "final")]
        v = self.validity(s)
        self.assertEqual(v["kind"], "invalid")
        self.assertFalse(v["evidence"]["checks"]["twin control"])

    def test_c6_twin_is_a_persistence_control_not_a_validity_check(self):
        # C.9: "If it fails, the trial is invalid; in C6, the restart outcomes are UNKNOWN instead"
        s = ConditionRows()._c6()
        s.lines = [x for x in s.lines if not (x.get("role") == "twin" and x.get("kind") == "final")]
        self.assertNotIn("twin control", self.validity(s)["evidence"]["checks"])

    def test_twin_pass_needs_a_gap_free_receiver(self):
        s = drop_ticks(Syn(), T0 - 20, T0 - 10)                          # during the twin's work
        self.assertEqual(find(rows_of(s.trial()), "twin", "twin control")["kind"], A.UNK)


class DurableObligations(unittest.TestCase):
    """Pre-freeze batch, item 3 (C.5 events 3 and 4, C.6 verified stop). Computation, protected effects and the
    accepted durable obligation are judged separately; a quiet watch never closes effects while the run can still run
    or retry work. The PC pilot (XC-pilot-c-pc-1013) scored effects_closed "closed" while its run stayed RUNNING with
    attempt 2 SCHEDULED, and the probe then re-ran that attempt (21 effects)."""

    def pc(self):
        s = Syn("PC", "NH", "kill_worker")
        s.R["intervention"]["actions"] = [sigkill()]
        s.R["intervention"]["worker_dead"] = [{"role": "target", "t_from": T0 + 0.01, "t_to": None}]
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 8.5, 9, final=False)
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "RUNNING", pending=(("work", "STARTED", 1),) if o < 60 else (("work", "SCHEDULED", 2),),
                              hist=["WorkflowExecutionStarted", "ActivityTaskScheduled"])
                        for o in (0, 2, 5, 10, 20, 30, 60, 90, 120)]
        s.R["capability"] = [{"queue": "qW", "ok": True}]
        return s

    def cancelled_hb(self, steps=13, final=False, pending=()):
        s = Syn("C2", "HB")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 8.5, steps, final=final)
        s.R["claim_poll"] = [poll(1, T0 + 0.6, "CANCELED")]
        s.R["reads"] = [sched(o, "CANCELED", pending=pending) for o in (0, 2, 5, 10, 20, 30, 60, 90, 120)]
        return s

    def events(self, s):
        return find(rows_of(s.trial()), "events", "five events")["evidence"]

    def verdict(self, s):
        return find(rows_of(s.trial()), "verified_stop", "verified stop")

    def test_a_quiet_watch_never_closes_effects_while_the_run_is_open(self):
        ev = self.events(self.pc())
        self.assertIs(ev["bounded_quiet"], True)                  # the native quiet measure is kept as it was
        self.assertEqual(ev["computation_boundary"], "closed")    # the killed worker held the only execution
        self.assertIs(ev["work_ceased"], True)
        self.assertEqual(ev["durable_obligation"], "open")
        self.assertEqual(ev["effects_closed"], "obligation open")
        v = self.verdict(self.pc())
        self.assertEqual(v["kind"], "not stopped")
        self.assertEqual(v["evidence"]["obligation"]["status"], "RUNNING")

    def test_a_continued_as_new_run_is_an_open_obligation(self):
        s = Restarts()._quiet_c1()
        s.R["reads"] = [sched(o, "CONTINUED_AS_NEW", hist=["WorkflowExecutionStarted",
                                                          "WorkflowExecutionContinuedAsNew"]) for o in (0, 2, 120)]
        self.assertEqual(self.events(s)["effects_closed"], "obligation open")
        self.assertEqual(self.verdict(s)["kind"], "not stopped")

    def test_quiet_after_a_cancel_without_a_final_or_a_dead_process_is_unresolved(self):
        # C2: the run is CANCELED and the activity has been quiet since step 13, but its worker is alive and no final
        # landed: a cancellation status and silence leave the computation unresolved (MEASURES-COMMON section 5)
        s = self.cancelled_hb()
        ev = self.events(s)
        self.assertIs(ev["bounded_quiet"], True)
        self.assertEqual(ev["computation_boundary"], "unresolved")
        self.assertIsNone(ev["work_ceased"])
        self.assertEqual(ev["durable_obligation"], "closed")
        self.assertEqual(ev["effects_closed"], "not closed")
        v = self.verdict(s)
        self.assertEqual(v["kind"], "unresolved")
        self.assertEqual([x["closed"] for x in v["evidence"]["computation"]["executions"]], [False])

    def test_natural_completion_closes_the_computation_and_a_closed_run_the_obligation(self):
        s = Syn()                                                  # C3: final at T0 + 15.5, CANCELED throughout
        ev = self.events(s)
        self.assertEqual((ev["computation_boundary"], ev["durable_obligation"]), ("closed", "closed"))
        self.assertIs(ev["work_ceased"], True)
        self.assertEqual(ev["effects_closed"], "closed by completion, not by the stop")
        v = self.verdict(s)
        self.assertEqual(v["kind"], A.ENDED_BY_COMPLETION)                    # review, major 6: not "stopped"
        self.assertIs(v["evidence"]["by_completion"], True)

    def test_a_killed_attempt_and_a_completed_rerun_end_by_completion(self):
        # both executions have independent ending evidence; the re-run's final effect means the job ended by
        # completion, not by the stop (review, major 6)
        s = Restarts()._c7()
        s.R["intervention"]["actions"] = [sigkill()]
        s.lines = drop_target_work(s.lines)
        s.lines += work_run(T0 - 8.5, 9, final=False) + work_run(T0 + 16, 20, nonce="n2", attempt=2, pid=5151)
        v = self.verdict(s)
        self.assertEqual(v["kind"], A.ENDED_BY_COMPLETION)
        self.assertEqual(sorted(x["how"].split(":")[0] for x in v["evidence"]["computation"]["executions"]),
                         ["final effect", "worker process confirmed dead"])

    def test_a_kill_does_not_close_an_execution_of_another_live_worker(self):
        # C8: attempt 2 ran on generation 2 (another pid), which is alive and posted no final
        s = Syn("C8", "HB", "kill_cancel_restart")
        s.R["intervention"]["actions"] = [sigkill(), {"name": "cancel", "t_send": T0 + 3, "t_return": T0 + 3.1,
                                                      "rc": 0, "timed_out": False}]
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 8.5, 9, final=False)
        s.lines += work_run(T0 + 21, 2, nonce="n2", attempt=2, final=False, pid=5151)
        s.R["reads"] = [sched(o, "CANCELED") for o in (0, 2, 5, 10, 20, 30, 60, 90, 120)]
        v = self.verdict(s)
        self.assertEqual(v["kind"], "unresolved")
        self.assertEqual([x["closed"] for x in v["evidence"]["computation"]["executions"]], [True, False])

    def test_a_kill_without_an_observed_exit_closes_nothing(self):
        s = self.pc()
        s.R["intervention"]["actions"] = [sigkill(exit_after=None)]
        self.assertEqual(self.events(s)["computation_boundary"], "unresolved")

    def test_a_run_that_never_scheduled_work_is_stopped_once_closed(self):
        s = Restarts()._quiet_c1()                                 # C1: CANCELED, no work effect
        ev = self.events(s)
        self.assertEqual((ev["computation_boundary"], ev["effects_closed"]), ("closed", "closed"))
        self.assertEqual(self.verdict(s)["kind"], "stopped")

    def test_a_final_effect_on_a_run_still_open_is_not_closed(self):
        # C3w-like: the final effect landed, but the end-of-watch read still shows the run RUNNING
        s = Syn("C3w", "NH")
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "RUNNING", pending=(("work", "CANCEL_REQUESTED", 1),)) for o in
                        (0, 2, 5, 10, 20, 30, 60, 90, 120)]
        self.assertEqual(self.events(s)["effects_closed"], "obligation open")
        self.assertEqual(self.verdict(s)["kind"], "not stopped")

    def test_a_declared_effect_in_the_last_5_s_of_the_watch_is_not_stopped(self):
        s = Syn()
        s.lines.append(eff(T0 + 118, "progress", 3, "n7", attempt=2, pid=5151))
        v = self.verdict(s)
        self.assertEqual(v["kind"], "not stopped")
        self.assertEqual(v["evidence"]["active_effects"], 1)

    def test_missing_evidence_is_unresolved_never_stopped(self):
        s = drop_ticks(Syn(), T0 + 30, T0 + 40)                    # a receiver gap inside the watch
        self.assertEqual(self.verdict(s)["kind"], "unresolved")
        s = Restarts()._quiet_c1()
        s.R["reads"] = [sched(o, "CANCELED", rc=1, stderr="context deadline exceeded") for o in (0, 2, 120)]
        s.R["claim_poll"] = []
        self.assertEqual(self.events(s)["effects_closed"], "unknown")
        self.assertEqual(self.verdict(s)["kind"], "unresolved")
        s = Syn()
        s.R["void"] = "clock guard failed"
        self.assertEqual(self.verdict(s)["kind"], "unresolved")

    def test_an_earlier_closed_read_stands_when_the_end_read_failed(self):
        # closure is permanent (T2, T9): a closed run takes no further events
        s = Restarts()._quiet_c1()
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(0, "CANCELED"), sched(2, "CANCELED"), sched(120, "CANCELED", rc=1, stderr="timeout")]
        self.assertEqual(self.events(s)["durable_obligation"], "closed")

    def test_an_unrecognised_status_is_an_unknown_obligation(self):
        # open means RUNNING or CONTINUED_AS_NEW only; a status outside T14's list proves neither
        s = Restarts()._quiet_c1()
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "PAUSED") for o in (0, 2, 120)]
        ev = self.events(s)
        self.assertEqual((ev["durable_obligation"], ev["effects_closed"]), ("unknown", "unknown"))
        self.assertEqual(self.verdict(s)["kind"], "unresolved")

    def test_without_the_end_of_watch_read_an_earlier_running_read_decides_nothing(self):
        # the +120 s read decides; an earlier RUNNING read cannot stand in for it (closure, being permanent, can)
        s = Restarts()._quiet_c1()
        s.R["claim_poll"] = []
        s.R["reads"] = [sched(o, "RUNNING") for o in (0, 2, 90)]
        self.assertEqual(self.events(s)["durable_obligation"], "unknown")
        self.assertEqual(self.verdict(s)["kind"], "unresolved")

    def test_a_started_attempt_listed_without_a_closed_execution_is_unresolved(self):
        s = Restarts()._quiet_c1()                                 # no work effect, but the end read lists one
        s.R["reads"] = [c1_read(o, pending=(("work", "STARTED", 1),), twin="COMPLETED" if o >= 60 else "RUNNING")
                        for o in (0, 2, 120)]
        self.assertEqual(self.events(s)["computation_boundary"], "unresolved")
        self.assertEqual(self.verdict(s)["kind"], "unresolved")

    def test_the_summary_reports_the_verdict(self):
        T = Restarts()._quiet_c1().trial()
        self.assertIn("verified_stop / verified stop: stopped", A.summarize(T, A.score_trial(T)))


class DeliveryBound(unittest.TestCase):
    """C.6 M3, restored in draft 5 (pre-freeze batch, item 2): for HB under cancel or terminate, the first execution
    posts no step at or after delivery_bound_step (14 in full mode) and no final effect. The stop is 8.5 s into the
    activity, so steps 10-13 follow it (step 13 at the next heartbeat RPC, 12 s in). The pre-check's heartbeat time puts
    the phase on plan (review: the phase precondition)."""

    def hb(self, steps, final=False, cond="C2"):
        s = Syn(cond, "HB", "terminate" if cond == "C5" else "cancel")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 8.5, steps, final=final)
        return pre_check_with_heartbeat(s, T0 - 8.51 + 4.02)

    def bound(self, s):
        return find(rows_of(s.trial()), "M3", "delivery bound")

    def test_a_first_execution_that_stops_by_step_13_is_within(self):
        for cond in ("C2", "C2w", "C5"):
            self.assertEqual(self.bound(self.hb(13, cond=cond))["kind"], "within", cond)

    def test_step_14_or_a_final_effect_exceeds(self):
        self.assertEqual(self.bound(self.hb(14))["kind"], "exceeded")
        self.assertEqual(self.bound(self.hb(14))["evidence"]["late_steps"], [14])
        self.assertEqual(self.bound(self.hb(13, final=True))["kind"], "exceeded")

    def test_exceeded_stands_on_a_receiver_gap_but_within_does_not(self):
        # C.6: positive evidence stands whatever else is missing; "within" is an absence and needs a gap-free receiver
        self.assertEqual(self.bound(drop_ticks(self.hb(14), T0 + 20, T0 + 30))["kind"], "exceeded")
        self.assertEqual(self.bound(drop_ticks(self.hb(13), T0 + 20, T0 + 30))["kind"], A.UNK)

    def test_no_bound_without_a_planned_one(self):
        s = self.hb(14)
        s.R["plan"]["delivery_bound_step"] = None             # FAST, or a record written under draft 4
        self.assertEqual(self.bound(s)["kind"], "not applicable")


class CapabilityJobs(unittest.TestCase):
    """C.9 "Readiness and capability jobs complete" (pre-freeze batch, item 1): draft 4's validity rows ignored them."""

    def checks(self, s):
        return find(rows_of(s.trial()), "validity", "trial validity")

    def test_a_failed_end_of_watch_capability_job_invalidates(self):
        self.assertIs(self.checks(Syn())["evidence"]["checks"]["readiness and capability jobs"], True)
        for queue in ("qT", "qW"):
            s = Syn()
            next(c for c in s.R["capability"] if c["queue"] == queue)["ok"] = False
            v = self.checks(s)
            self.assertEqual(v["kind"], "invalid", queue)
            self.assertIs(v["evidence"]["checks"]["readiness and capability jobs"], False, queue)

    def test_missing_end_of_watch_capability_jobs_invalidate(self):
        for capability in ([], [{"queue": "qT", "ok": True}], None):
            s = Syn()
            s.R["capability"] = capability
            self.assertIs(self.checks(s)["evidence"]["checks"]["readiness and capability jobs"], False, capability)

    def test_pc_needs_only_the_twin_queues_job(self):
        # C.4 step 5: PC skips the target queue, whose worker is dead by design
        s = Syn("PC", "NH", "kill_worker")
        s.R["capability"] = [{"queue": "qW", "ok": True}]
        self.assertIs(self.checks(s)["evidence"]["checks"]["readiness and capability jobs"], True)
        s.R["capability"] = [{"queue": "qW", "ok": False}, {"queue": "qT", "ok": True}]
        self.assertIs(self.checks(s)["evidence"]["checks"]["readiness and capability jobs"], False)

    def test_a_failed_readiness_or_in_watch_restart_job_invalidates(self):
        s = Restarts()._c7()
        self.assertIs(self.checks(s)["evidence"]["checks"]["readiness and capability jobs"], True)
        s.R["intervention"]["restarts"][0]["capability"][0]["ok"] = False
        self.assertIs(self.checks(s)["evidence"]["checks"]["readiness and capability jobs"], False)
        s = Syn()
        s.R["readiness"] = [{"what": "worker-target-g1", "ok": False, "t_ready": T0 - 60}]
        self.assertIs(self.checks(s)["evidence"]["checks"]["readiness and capability jobs"], False)

    def test_the_probes_own_jobs_decide_its_outcome_not_validity(self):
        # as R6.9: the probe's readiness and capability jobs are its controls (C.6 restart outcome)
        s = Restarts()._probe(Restarts()._quiet_c1(), cap_ok=False)
        s.R["controls"]["health"].append({"label": "end of probe", "ok": True, "mono": T0 + 200})
        s.R["readiness"] = [{"what": "worker-target-g2", "ok": False, "t_ready": T0 + 128}]   # the probe's restart
        rows = rows_of(s.trial())
        self.assertIs(find(rows, "validity", "trial validity")["evidence"]["checks"]["readiness and capability jobs"],
                      True)
        self.assertEqual(find(rows, "restart", "durability probe")["kind"], "unresolved")


class WallClockRecomputation(unittest.TestCase):
    """Pre-freeze batch, item 5 (C.9). From record schema 2 the scorer recomputes the harness's wall/monotonic check
    from receiver.jsonl's paired stamps (`mono`, `t`) with the harness's rule (temporal/clockwall_c.py) and requires both
    verdicts; for earlier records the recomputation is a labelled post hoc diagnostic that never voids."""

    def current(self, step=None):
        s = current_record(Syn())
        if step:
            for x in s.lines:
                if x["mono"] >= T0 + 50:
                    x["t"] += step
        return s

    def validity(self, s):
        return find(rows_of(s.trial()), "validity", "trial validity")

    def test_a_current_record_with_consistent_stamps_is_valid(self):
        v = self.validity(self.current())
        self.assertEqual(v["kind"], "valid", v["verdict"])
        self.assertIs(v["evidence"]["checks"]["wall clock recorded"], True)
        self.assertIs(v["evidence"]["checks"]["wall clock recomputed"], True)

    def test_a_wall_step_in_the_receivers_stamps_invalidates_a_current_record(self):
        v = self.validity(self.current(step=30.0))
        self.assertEqual(v["kind"], "invalid")
        self.assertIs(v["evidence"]["checks"]["wall clock recomputed"], False)
        self.assertFalse(v["evidence"]["wall_clock"]["scorer_recomputed"]["ok"])

    def test_a_current_record_without_a_passing_harness_receipt_is_invalid(self):
        for receipt in (None, {"ok": False}):
            s = self.current()
            if receipt is None:
                del s.R["clock_across_trial"]["wall_clock"]
            else:
                s.R["clock_across_trial"]["wall_clock"] = receipt
            v = self.validity(s)
            self.assertEqual(v["kind"], "invalid", receipt)
            self.assertIs(v["evidence"]["checks"]["wall clock recorded"], False, receipt)

    def test_an_earlier_record_reports_the_recomputation_only_as_a_diagnostic(self):
        s = Syn()                                         # no schema_version: written before the pre-freeze batch
        for x in s.lines:
            if x["mono"] >= T0 + 50:
                x["t"] += 30.0
        v = self.validity(s)
        self.assertEqual(v["kind"], "valid")
        self.assertNotIn("wall clock recomputed", v["evidence"]["checks"])
        self.assertIn("post hoc", v["evidence"]["wall_clock"]["policy"])
        self.assertFalse(v["evidence"]["wall_clock"]["scorer_recomputed"]["ok"])


class HostProfileInTheRecord(unittest.TestCase):
    """Pre-freeze batch, item 6 (C.0, C.9): from record schema 2 every record declares its host profile (wsl or
    linux-vm) and carries its passing start host check (the profile's disk floor and, on linux-vm, the kernel check).
    Records written before are not judged on them."""

    def checks(self, s):
        return find(rows_of(s.trial()), "validity", "trial validity")

    def test_a_current_record_declares_a_known_profile(self):
        for profile in ("wsl", "linux-vm"):
            self.assertEqual(self.checks(current_record(Syn(), profile))["kind"], "valid", profile)
        for block in (None, {"declared": "gce"}, "wsl"):
            s = current_record(Syn())
            s.R["host_profile"] = block
            v = self.checks(s)
            self.assertEqual(v["kind"], "invalid", block)
            self.assertIs(v["evidence"]["checks"]["host profile declared"], False, block)

    def test_a_current_record_needs_its_passing_start_host_check(self):
        for block in (None, {"ok": False, "violations": ["disk floor"]}):
            s = current_record(Syn())
            s.R["host_checks_start"] = block
            v = self.checks(s)
            self.assertEqual(v["kind"], "invalid", block)
            self.assertIs(v["evidence"]["checks"]["host checks at start"], False, block)

    def test_an_earlier_record_is_not_judged_on_them(self):
        v = self.checks(Syn())
        self.assertEqual(v["kind"], "valid")
        self.assertNotIn("host profile declared", v["evidence"]["checks"])


class ReportsOutsideRawFolders(unittest.TestCase):
    """Pre-freeze batch, items 7 and 8 (C.12): reports are derived files, never raw ones. analyze_c.py writes them
    outside the trial folder (by default <results>/derived-posthoc/<trial folder>/, or --out), never into a folder that
    holds a record, and replaces earlier reports only with --overwrite."""

    def raw(self, tmp):
        import json as _json
        folder = os.path.join(tmp, "results", "XC-pilot-r1-C3-20261014-120000")
        os.makedirs(folder)
        s = Syn()
        with open(os.path.join(folder, "record.json"), "w", encoding="utf-8") as f:
            _json.dump(dict(s.R, trial="XC-pilot-r1-C3"), f)
        with open(os.path.join(folder, "receiver.jsonl"), "w", encoding="utf-8") as f:
            f.write("".join(_json.dumps(x) + "\n" for x in s.lines))
        return folder

    def quiet_main(self, argv):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return A.main(argv)

    def test_the_default_destination_is_outside_the_raw_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.raw(tmp)
            before = sorted(os.listdir(folder))
            self.assertEqual(self.quiet_main([folder]), 0)
            self.assertEqual(sorted(os.listdir(folder)), before)                    # nothing added to the raw folder
            dest = os.path.join(tmp, "results", "derived-posthoc", os.path.basename(folder))
            self.assertEqual(sorted(os.listdir(dest)), ["scored_c.jsonl", "summary_c.md"])

    def test_out_writes_there_and_refuses_to_overwrite_without_the_flag(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder, out = self.raw(tmp), os.path.join(tmp, "reports")
            self.assertEqual(self.quiet_main([folder, "--out", out]), 0)
            self.assertTrue(os.path.isfile(os.path.join(out, "scored_c.jsonl")))
            with self.assertRaises(SystemExit):
                self.quiet_main([folder, "--out", out])
            self.assertEqual(self.quiet_main([folder, "--out", out, "--overwrite"]), 0)

    def test_a_destination_inside_a_raw_folder_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.raw(tmp)
            for out in (folder, os.path.join(folder, "sub")):
                with self.assertRaisesRegex(SystemExit, "raw"):
                    self.quiet_main([folder, "--out", out])
            self.assertEqual(sorted(os.listdir(folder)), ["receiver.jsonl", "record.json"])


class Validity(unittest.TestCase):
    def test_guard_violation_invalidates(self):
        s = Syn()
        s.R["guard_violations"] = ["WSL VM restarted during the trial (boot_id changed)"]
        self.assertEqual(find(rows_of(s.trial()), "validity", "trial validity")["kind"], "invalid")

    def test_clean_synthetic_trial_is_valid(self):
        self.assertEqual(find(rows_of(Syn().trial()), "validity", "trial validity")["kind"], "valid")

    def test_clock_order_violation_makes_initiation_unknown(self):
        s = Syn()
        s.lines.append(eff(T0 + 50, "progress", 21, "n1", smono=T0 + 51))      # sent after it arrived
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "M1", "by initiation, from the cancel")["kind"], A.UNK)
        # C.9: the violation makes the by-initiation versions UNKNOWN; it is a diagnostic, not a validity check
        self.assertIs(find(rows, "validity", "trial validity")["evidence"]["clock_order_ok"], False)

    def test_missing_receiver_log_is_unknown(self):
        T = A.Trial(Syn().R, None, [])
        rows = rows_of(T)
        self.assertEqual(find(rows, "M1", "effects after the request")["kind"], A.UNK)
        self.assertEqual(find(rows, "validity", "trial validity")["kind"], "invalid")


# ============================ pre-freeze review (14 Oct): the corrections below are test-first ============================
STAGE1 = "stage 1: when the cancel reaches workflow code"
STAGE2 = "stage 2: local activity stops without heartbeats"
ORDER_ROW = "server recorded the cancel request before the marker"
LA_HISTORY = ["WorkflowExecutionStarted", "WorkflowTaskCompleted", "WorkflowExecutionCancelRequested",
              "MarkerRecorded", "WorkflowExecutionCanceled"]


class C4StageOneFromWorkflowCode(unittest.TestCase):
    """Review, major 1 (C.6, C.8 C4). The order of WorkflowExecutionCancelRequested and the marker in the history shows
    when the server wrote the request, not when workflow code saw it: a cancel that arrives while a workflow task is
    open is buffered and written at the next task completion, which a long local activity forces at each workflow-task
    heartbeat (nominally 16 s into it, before the marker at about 20 s) under either reading. Stage 1 is decided from
    evidence that workflow code acted on the cancel before the local activity finished; the event order is a separate
    descriptive row. The local activity starts at T0 - 8.5 (the stop 8.5 s in), so its final effect is due at T0 + 11.5."""

    def la(self, steps=20, final=True, ack=None, polls=(), reads=None, marker_failure=None):
        s = Syn("C4", "LA")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"]
        s.lines += work_run(T0 - 8.5, steps, final=final, is_local=True)
        s.R["claim_poll"] = [poll(i + 1, T0 + t, st) for i, (t, st) in enumerate(polls)]
        attrs = {3: {"markerRecordedEventAttributes": {"markerName": "core_local_activity", "failure": marker_failure}}} \
            if marker_failure else None
        s.R["reads"] = [sched(o, st, hist=(LA_HISTORY if st == "CANCELED" else LA_HISTORY[:2]), hist_attrs=attrs)
                        for o, st in (reads or ())]
        s.logs = [{"event": "activity_start", "workflow_id": "wT", "smono": T0 - 8.5, "exec_nonce": "n1",
                   "activity_id": "work", "is_local": True}]
        if ack is not None:
            s.logs.append({"event": "activity_cancelled", "workflow_id": "wT", "smono": T0 + ack, "exec_nonce": "n1",
                           "activity_id": "work", "flags": {"cancel_requested": True}})
        return s

    def stage(self, s, name=STAGE1):
        return find(rows_of(s.trial()), "C4", name)

    def test_cancel_written_before_the_marker_with_the_final_effect_and_no_acknowledgement_is_not_reading_a(self):
        # the review's fixture: CancelRequested precedes the marker, `final` landed, no acknowledgement. A describe that
        # started after the final effect (T0 + 11.5) still read RUNNING, and the run closed after it: reading (b)
        s = self.la(polls=((0.6, "RUNNING"), (12.0, "RUNNING"), (12.5, "CANCELED")),
                    reads=((0, "RUNNING"), (2, "RUNNING"), (5, "RUNNING"), (10, "RUNNING"), (20, "CANCELED"),
                           (120, "CANCELED")))
        self.assertEqual(self.stage(s)["kind"], "(b) after the local activity finished")
        self.assertEqual(self.stage(s, ORDER_ROW)["kind"], "yes")          # the server wrote it first all the same
        self.assertEqual(self.stage(s, STAGE2)["kind"], "not tested")

    def test_without_a_read_between_the_final_effect_and_the_close_the_reading_is_unknown_never_a(self):
        # the same world, but no describe started after the final effect while the run was open: undetermined
        s = self.la(polls=((0.6, "RUNNING"),),
                    reads=((0, "RUNNING"), (2, "RUNNING"), (5, "RUNNING"), (10, "RUNNING"), (20, "CANCELED"),
                           (120, "CANCELED")))
        self.assertEqual(self.stage(s)["kind"], A.UNK)
        self.assertEqual(self.stage(s, ORDER_ROW)["kind"], "yes")

    def test_an_acknowledgement_shows_reading_a(self):
        # the local activity was cancelled before it returned: workflow code acted on the cancel while it ran
        s = self.la(steps=12, final=False, ack=7.6,
                    polls=((0.6, "RUNNING"), (8.0, "CANCELED")),
                    reads=((0, "RUNNING"), (2, "RUNNING"), (5, "RUNNING"), (10, "CANCELED"), (120, "CANCELED")))
        self.assertEqual(self.stage(s)["kind"], "(a) before the local activity finished")
        self.assertEqual(self.stage(s, STAGE2)["kind"], "stopped")

    def test_a_close_before_the_final_effect_shows_reading_a_even_when_the_activity_runs_on(self):
        # TRY_CANCEL (K3) and K2: the run closed CANCELED at T0 + 8.0, before the final effect at T0 + 11.5, which landed:
        # reading (a), and stage 2 did not stop
        s = self.la(polls=((0.6, "RUNNING"), (8.0, "CANCELED")),
                    reads=((0, "RUNNING"), (2, "RUNNING"), (5, "RUNNING"), (10, "CANCELED"), (120, "CANCELED")))
        self.assertEqual(self.stage(s)["kind"], "(a) before the local activity finished")
        self.assertEqual(self.stage(s, STAGE2)["kind"], "did not stop")

    def test_a_cancelled_marker_shows_reading_a(self):
        # the claim (+20 s read) follows the final effect and no acknowledgement was logged: only the marker decides
        reads = ((0, "RUNNING"), (20, "CANCELED"), (120, "CANCELED"))
        cancelled = {"message": "Local activity cancelled", "cause": {"canceledFailureInfo": {}}}
        s = self.la(polls=((0.6, "RUNNING"),), reads=reads, marker_failure=cancelled)
        self.assertEqual(self.stage(s)["kind"], "(a) before the local activity finished")
        s = self.la(polls=((0.6, "RUNNING"),), reads=reads, marker_failure={"message": "other failure"})
        self.assertEqual(self.stage(s)["kind"], A.UNK)


class VerifiedStopNeedsValidityAndHistory(unittest.TestCase):
    """Review, major 5 (C.6 verified stop). "stopped" is an absence of work: it needs a valid trial (C.9), and Temporal's
    history must show no work attempt that no closed execution accounts for; with no work execution at the receiver it
    needs positive history evidence that the work activity never ran."""

    def verdict(self, s):
        return find(rows_of(s.trial()), "verified_stop", "verified stop")

    def test_a_c1_whose_history_shows_the_work_never_scheduled_is_stopped(self):
        self.assertEqual(self.verdict(Restarts()._quiet_c1())["kind"], "stopped")

    def test_an_invalid_trial_is_never_stopped(self):
        s = Restarts()._quiet_c1()
        next(c for c in s.R["capability"] if c["queue"] == "qT")["ok"] = False      # the target queue's job failed
        v = self.verdict(s)
        self.assertEqual(v["kind"], "unresolved")
        self.assertIn("invalid", v["verdict"])

    def failed_after_attempt_3(self, cap_ok=True):
        # the review's probe: the history shows the work activity scheduled and started (attempt 3), then the run
        # failed; no work effect reached the receiver
        s = Restarts()._quiet_c1()
        types = ["WorkflowExecutionStarted", "ActivityTaskScheduled", "ActivityTaskStarted", "ActivityTaskCompleted",
                 "TimerStarted", "TimerFired", "ActivityTaskScheduled", "ActivityTaskStarted", "ActivityTaskFailed",
                 "WorkflowExecutionFailed"]
        attrs = dict(C1_ATTRS)
        attrs.update({6: {"activityTaskScheduledEventAttributes": {"activityId": "work"}},
                      7: {"activityTaskStartedEventAttributes": {"scheduledEventId": "7", "attempt": 3}},
                      8: {"activityTaskFailedEventAttributes": {"scheduledEventId": "7"}}})
        s.R["reads"] = [sched(o, "FAILED", hist=types, hist_attrs=attrs, twin="COMPLETED" if o >= 60 else "RUNNING")
                        for o in (0, 2, 120)]
        s.R["claim_poll"] = []
        if not cap_ok:
            next(c for c in s.R["capability"] if c["queue"] == "qT")["ok"] = False
        return s

    def test_a_started_work_attempt_without_an_execution_is_unresolved(self):
        s = self.failed_after_attempt_3()
        self.assertEqual(find(rows_of(s.trial()), "validity", "trial validity")["kind"], "valid")
        v = self.verdict(s)
        self.assertEqual(v["kind"], "unresolved")
        self.assertTrue(any("ActivityTaskStarted" in r for r in v["evidence"]["computation"]["reasons"]), v["verdict"])
        self.assertEqual(self.verdict(self.failed_after_attempt_3(cap_ok=False))["kind"], "unresolved")

    def test_with_no_execution_a_scheduled_work_activity_needs_its_closing_event(self):
        s = Restarts()._quiet_c1()
        types = C1_TYPES[:-1] + ["ActivityTaskScheduled", "WorkflowExecutionCanceled"]
        attrs = {**C1_ATTRS, 8: {"activityTaskScheduledEventAttributes": {"activityId": "work"}}}
        s.R["reads"] = [sched(o, "CANCELED", hist=types, hist_attrs=attrs, twin="COMPLETED" if o >= 60 else "RUNNING")
                        for o in (0, 2, 120)]
        self.assertEqual(self.verdict(s)["kind"], "unresolved")
        # an ActivityTaskScheduled that names no activity cannot rule the work activity out
        s.R["reads"] = [sched(o, "CANCELED", twin="COMPLETED" if o >= 60 else "RUNNING") for o in (0, 2, 120)]
        self.assertEqual(self.verdict(s)["kind"], "unresolved")

    def test_without_a_usable_end_of_watch_history_stopped_is_unresolved(self):
        s = Restarts()._quiet_c1()
        del s.R["reads"][-1]["parts"]["show_target"]
        self.assertEqual(self.verdict(s)["kind"], "unresolved")

    def test_every_started_attempt_needs_a_closed_execution(self):
        # C7-like: the history's ActivityTaskStarted shows attempt 3, but the receiver has executions of attempts 1, 2
        s = Restarts()._c7()
        s.R["intervention"]["actions"] = [sigkill()]
        s.lines = drop_target_work(s.lines)
        s.lines += work_run(T0 - 8.5, 9, final=False) + work_run(T0 + 16, 20, nonce="n2", attempt=2, pid=5151)
        h = ["WorkflowExecutionStarted", "ActivityTaskScheduled", "ActivityTaskStarted", "ActivityTaskCompleted",
             "WorkflowExecutionCompleted"]
        for attempt, kind in ((2, A.ENDED_BY_COMPLETION), (3, "unresolved")):
            attrs = {1: {"activityTaskScheduledEventAttributes": {"activityId": "work"}},
                     2: {"activityTaskStartedEventAttributes": {"scheduledEventId": "2", "attempt": attempt}}}
            s.R["reads"] = [sched(o, "COMPLETED" if o >= 60 else "RUNNING", hist=h, hist_attrs=attrs,
                                  twin="COMPLETED" if o >= 60 else "RUNNING") for o in (0, 2, 5, 10, 20, 30, 60, 90, 120)]
            self.assertEqual(self.verdict(s)["kind"], kind, attempt)

    def test_a_local_activity_variant_with_no_execution_needs_the_timer_unfired(self):
        s = Restarts()._quiet_c1()
        s.R["spec"]["variant"] = "LA"
        self.assertEqual(self.verdict(s)["kind"], "stopped")                 # TimerStarted, no TimerFired
        types = ["WorkflowExecutionStarted", "TimerStarted", "TimerFired", "WorkflowExecutionCanceled"]
        s.R["reads"] = [sched(o, "CANCELED", hist=types, twin="COMPLETED" if o >= 60 else "RUNNING") for o in (0, 2, 120)]
        self.assertEqual(self.verdict(s)["kind"], "unresolved")


class CompletionIsNotAStop(unittest.TestCase):
    """Review, major 6 (C.6 verified stop, as B.5): when the work ran to its final effect, the verdict is "ended by
    completion, not by the stop", never "stopped": our 20-step job finished inside the watch."""

    def verdict(self, s):
        return find(rows_of(s.trial()), "verified_stop", "verified stop")

    def test_c3_ends_by_completion(self):
        v = self.verdict(Syn())
        self.assertEqual(v["kind"], A.ENDED_BY_COMPLETION)
        self.assertEqual(A.ENDED_BY_COMPLETION, "ended by completion, not by the stop")
        self.assertIs(v["evidence"]["by_completion"], True)
        T = Syn().trial()
        self.assertIn("verified_stop / verified stop: " + A.ENDED_BY_COMPLETION, A.summarize(T, A.score_trial(T)))

    def test_stopped_stays_for_a_stop_or_a_confirmed_kill(self):
        self.assertEqual(self.verdict(Restarts()._quiet_c1())["kind"], "stopped")        # nothing ran
        s = DurableObligations().pc()               # the only execution was killed; here the run is also terminated
        s.lines = drop_target_work(Syn().lines) + work_run(T0 - 8.5, 9, final=False)
        s.R["reads"] = [sched(o, "TERMINATED", hist=["WorkflowExecutionStarted", "ActivityTaskScheduled",
                                                     "WorkflowExecutionTerminated"],
                              twin="COMPLETED" if o >= 60 else "RUNNING") for o in (0, 2, 5, 10, 20, 30, 60, 90, 120)]
        v = self.verdict(s)
        self.assertEqual(v["kind"], "stopped", v["verdict"])


class ValidityMatchesC9(unittest.TestCase):
    """Review, minor (C.9): the scorer's validity has C.9's checks only. A clock-order violation makes the by-initiation
    versions UNKNOWN, not the trial invalid; ticks are judged by the 2 s gap rule, not by one failed POST; a malformed
    effect post (now written into C.9) invalidates."""

    def validity(self, s):
        return find(rows_of(s.trial()), "validity", "trial validity")

    def test_a_clock_order_violation_alone_keeps_the_trial_valid(self):
        s = Syn()
        s.lines.append(eff(T0 + 50, "progress", 21, "n1", smono=T0 + 51))
        rows = rows_of(s.trial())
        v = find(rows, "validity", "trial validity")
        self.assertEqual(v["kind"], "valid", v["verdict"])
        self.assertIs(v["evidence"]["clock_order_ok"], False)
        self.assertEqual(find(rows, "M1", "by initiation, from the cancel")["kind"], A.UNK)

    def test_one_failed_tick_post_without_a_gap_keeps_the_trial_valid(self):
        s = Syn()
        s.R["controls"]["ticks"] = {"sent": 400, "failed": 1, "failures": [T0 + 30]}
        v = self.validity(s)
        self.assertEqual(v["kind"], "valid", v["verdict"])
        self.assertEqual(v["evidence"]["tick_posts"]["failed"], 1)

    def test_a_malformed_effect_post_invalidates(self):
        s = Syn()
        s.lines.append({"route": "effect", "bad": "not json", "mono": T0 + 3, "raw": T0 + 3, "t": WALL0 + T0 + 3})
        self.assertEqual(self.validity(s)["kind"], "invalid")


class WorkerSideFaults(unittest.TestCase):
    """Review, minor (K8). A failed effect POST fails our activity and Temporal retries it; a worker that stops without
    a signal is our instrument failing. Either in the watch invalidates the trial (an instrument fault, never Temporal's
    behaviour); during the durability probe it leaves the probe's outcome unresolved."""

    def validity(self, s):
        return find(rows_of(s.trial()), "validity", "trial validity")

    def test_an_effect_post_failure_in_the_watch_invalidates(self):
        for log in ({"event": "activity_error", "workflow_id": "wT", "smono": T0 + 5, "exec_nonce": "n1",
                     "error": "EffectPostError: receiver answered 500"},
                    {"event": "activity_error", "workflow_id": "wW", "smono": T0 - 20, "exec_nonce": "tw1",
                     "error": "TimeoutError: "},
                    {"event": "worker_stopped_without_signal", "smono": T0 + 7, "error": "RuntimeError('poll')"}):
            s = Syn()
            s.logs = [log]
            v = self.validity(s)
            self.assertEqual(v["kind"], "invalid", log)
            self.assertIs(v["evidence"]["checks"]["no worker-side effect failure in the watch"], False, log)

    def test_after_the_watch_it_leaves_the_probe_unresolved_not_the_trial_invalid(self):
        s = Restarts()._probe(Restarts()._quiet_c1())
        s.R["controls"]["health"].append({"label": "end of probe", "ok": True, "mono": T0 + 200})
        self.assertEqual(find(rows_of(s.trial()), "restart", "durability probe")["kind"], "stayed ended")
        s.logs = [{"event": "activity_error", "workflow_id": "wP", "smono": T0 + 140, "exec_nonce": "p1",
                   "error": "EffectPostError: receiver answered 500"}]
        rows = rows_of(s.trial())
        self.assertEqual(find(rows, "validity", "trial validity")["kind"], "valid")
        self.assertEqual(find(rows, "restart", "durability probe")["kind"], "unresolved")


class ServerRestartWindow(unittest.TestCase):
    """Review, minor (C.6 restart outcome, C6). After a server restart only effects arriving after the server's
    readiness count toward resumed or re-run; effects between the SIGTERM and readiness are reported as continued
    through the outage. The abandoned HB activity may keep posting while the 12 s heartbeat RPC races the shutdown."""

    def c6(self, effects):
        s = Syn("C6", "HB", "cancel_server_restart")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 8.5, 10, final=False)
        s.lines += effects
        s.R["intervention"]["outages"] = [{"kind": "server", "t_from": T0 + 1.1, "t_to": T0 + 6.0}]
        s.R["intervention"]["restarts"] = [{"what": "server", "t_start": T0 + 3.0, "t_ready": T0 + 6.0, "ok": True,
                                            "capability": [{"ok": True}, {"ok": True}]}]
        s.R["intervention"]["precondition"] = {"ok": True, "why": "CANCELED at c6-pre-1", "reads": []}
        return find(rows_of(s.trial()), "restart", "in-watch restart 1 (server)")

    def test_effects_during_the_outage_are_neither_resumed_nor_re_run(self):
        r = self.c6([eff(T0 + 3.5, "progress", 12, "n1"), eff(T0 + 5.5, "progress", 13, "n1")])
        self.assertEqual(r["kind"], "stayed ended", r["verdict"])
        self.assertEqual(r["evidence"]["continued_through_outage"], 2)

    def test_an_effect_after_readiness_is_still_resumed(self):
        r = self.c6([eff(T0 + 3.5, "progress", 12, "n1"), eff(T0 + 7.0, "progress", 13, "n1")])
        self.assertIn("resumed", r["kind"])


def pre_check_with_heartbeat(s, hb_mono, t_end=T0 - 4.3):
    """The pre-intervention check's read, with the work activity's last heartbeat time (C.6 M3 phase precondition)."""
    s.R["pre_check"] = {"ok": True, "read": {"label": "pre", "t_start": t_end - 0.2, "t_end": t_end, "parts": {
        "describe_target": part(describe("RUNNING", pending=(("work", "STARTED", 1, hb_mono),)), t_end)}}}
    return s


class HeartbeatPhasePrecondition(unittest.TestCase):
    """Review, minor (C.6 M3). The delivery bound rests on the heartbeat phase, measured once in feasibility. Per trial,
    the pre-intervention describe's last heartbeat time, placed on the monotonic clock, must lie on the 4 s grid from
    the activity's start (step 1's sender stamp) within 0.25 s, so that an RPC falls 8 s in, 0.5 s before the stop;
    the next RPC and the bound follow. Otherwise the bound reads "phase off-plan" (M), never within or exceeded."""

    def hb(self, steps=13, final=False, phase_error=0.02, cond="C2"):
        s = Syn(cond, "HB", "terminate" if cond == "C5" else "cancel")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 8.5, steps, final=final)
        a0 = T0 - 8.5 - 0.01                                  # step 1's sender stamp: the activity's start
        return pre_check_with_heartbeat(s, a0 + 4.0 + phase_error)

    def rows(self, s):
        rows = rows_of(s.trial())
        return find(rows, "M3", "delivery bound"), find(rows, "M3", "heartbeat phase precondition")

    def test_on_the_planned_phase_the_bound_is_derived_and_applied(self):
        bound, phase = self.rows(self.hb())
        self.assertEqual(phase["kind"], "pass")
        self.assertAlmostEqual(phase["evidence"]["next_rpc_s"], 12.02, places=2)
        self.assertEqual(phase["evidence"]["derived_bound_step"], 14)
        self.assertEqual(bound["kind"], "within")
        self.assertEqual(self.rows(self.hb(steps=14))[0]["kind"], "exceeded")

    def test_off_the_planned_phase_the_bound_reads_phase_off_plan(self):
        bound, phase = self.rows(self.hb(phase_error=1.0))
        self.assertEqual(phase["kind"], "off-plan")
        self.assertEqual(bound["kind"], "phase off-plan")
        self.assertEqual(self.rows(self.hb(steps=15, phase_error=1.0))[0]["kind"], "phase off-plan")

    def test_without_a_heartbeat_time_the_bound_is_unknown(self):
        s = self.hb()
        s.R["pre_check"] = {"ok": True}
        bound, phase = self.rows(s)
        self.assertEqual((bound["kind"], phase["kind"]), (A.UNK, A.UNK))


class DeliveryAndWhatFollows(unittest.TestCase):
    """Review, major 4 (C.6 M3, C.7 K14). Temporal decides when a cancellation reaches the activity (the heartbeat RPC);
    our code decides whether the activity then stops (K14: it re-raises asyncio.CancelledError with no cleanup). The two
    are reported apart: "delivery time" and "stops after delivery" (by initiation: a step started after the
    acknowledgement is our code continuing; one in flight at it is not)."""

    def c2(self, steps=13, ack=3.5, extra=()):
        s = HeartbeatPhasePrecondition().hb(steps=steps)
        s.lines += list(extra)
        if ack is not None:
            s.logs = [{"event": "activity_cancelled", "workflow_id": "wT", "smono": T0 + ack, "exec_nonce": "n1",
                       "last_step": steps, "flags": {"not_found": True}}]
        rows = rows_of(s.trial())
        return find(rows, "M3", "delivery time"), find(rows, "M3", "stops after delivery")

    def test_the_activity_stops_after_delivery(self):
        delivery, after = self.c2()
        self.assertEqual(after["kind"], "stopped after delivery")
        self.assertAlmostEqual(delivery["evidence"]["ack_rel"], 3.4, places=3)
        self.assertAlmostEqual(delivery["evidence"]["ack_minus_next_rpc_s"], 0.0, delta=0.05)

    def test_a_step_started_after_the_acknowledgement_is_our_code_continuing(self):
        self.assertEqual(self.c2(extra=[eff(T0 + 4.5, "progress", 14, "n1")])[1]["kind"], "continued after delivery")

    def test_a_step_in_flight_at_the_acknowledgement_is_not_continuing(self):
        # step 13 was sent before the acknowledgement and arrived after it (K8: at most one effect in flight)
        in_flight = [eff(T0 + 3.6, "progress", 13, "n1", smono=T0 + 3.45)]
        self.assertEqual(self.c2(steps=12, extra=in_flight)[1]["kind"], "stopped after delivery")

    def test_without_an_acknowledgement_there_is_no_delivery_to_judge(self):
        delivery, after = self.c2(ack=None)
        self.assertEqual((delivery["kind"], after["kind"]), ("no acknowledgement", "no acknowledgement"))


class StepInFlightAtTheCancellation(unittest.TestCase):
    """Review, minor (K8, C|K8). The cancellation line records the activity's phase (posting, heartbeat or sleeping)
    and the effect in flight; the scorer reports whether that step committed."""

    def row(self, ack_fields, extra=()):
        s = Syn("C2", "HB")
        s.lines = [x for x in s.lines if x.get("activity_id") != "work"] + work_run(T0 - 8.5, 12, final=False)
        s.lines += list(extra)
        s.logs = [dict({"event": "activity_cancelled", "workflow_id": "wT", "smono": T0 + 3.5, "exec_nonce": "n1",
                        "last_step": 13}, **ack_fields)]
        return find(rows_of(s.trial()), "M3", "step interrupted in flight, execution 1")

    def test_each_phase(self):
        inflight = {"phase": "posting 13", "inflight": {"step": 13, "kind": "progress", "smono": T0 + 3.49}}
        self.assertEqual(self.row(inflight, [eff(T0 + 3.6, "progress", 13, "n1", smono=T0 + 3.49)])["kind"],
                         "committed")
        self.assertEqual(self.row(inflight)["kind"], "not committed")
        self.assertEqual(self.row({"phase": "sleeping after 12", "inflight": None})["kind"], "not in flight")
        self.assertEqual(self.row({})["kind"], A.UNK)                     # a worker log written before the review


class StepLateness(unittest.TestCase):
    """Review, minor: our own observer's load is recorded (load average and steal at each scheduled read and the
    pre-check), and the scorer reports each step's lateness against the nominal (n - 1) x step_s schedule and the
    heartbeat RPC spacing; lateness over 0.25 s is flagged as a measurement-load diagnostic."""

    def test_a_late_step_is_flagged(self):
        r = find(rows_of(Syn().trial()), "M3", "step lateness and load (diagnostic)")
        self.assertEqual(r["kind"], "info")
        s = Syn()
        late = next(x for x in s.lines if x.get("step") == 15 and x.get("role") == "target")
        late["smono"] += 0.4
        late["mono"] += 0.4
        s.R["reads"][0]["host_load"] = {"loadavg": [3.9, 2.0, 1.0], "steal_ticks": 10, "cpu_ticks": 1000}
        r = find(rows_of(s.trial()), "M3", "step lateness and load (diagnostic)")
        self.assertEqual(r["kind"], "load flagged")
        self.assertEqual(r["evidence"]["executions"][0]["late_steps"], [15])
        self.assertEqual(len(r["evidence"]["host_load"]), 1)


class HostExclusionInTheRecord(unittest.TestCase):
    """Review, major 7 (C.4 step 1, C.10): from record schema 2 the record shows the harness held the host lock and found
    no other StopCheck trial, process, namespace or unit; without it the trial is invalid."""

    def test_a_current_record_needs_the_host_exclusion_check(self):
        v = find(rows_of(current_record(Syn()).trial()), "validity", "trial validity")
        self.assertEqual(v["kind"], "valid", v["verdict"])
        for block in (None, {"ok": False, "processes": [{"pid": 7, "programs": ["harness_b.py"]}]}):
            s = current_record(Syn())
            s.R["host_exclusion"] = block
            v = find(rows_of(s.trial()), "validity", "trial validity")
            self.assertEqual(v["kind"], "invalid", block)
            self.assertIs(v["evidence"]["checks"]["one StopCheck trial on the host"], False, block)


if __name__ == "__main__":
    unittest.main()
