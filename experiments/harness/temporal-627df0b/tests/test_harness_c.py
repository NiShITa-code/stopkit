"""Experiment C harness tests (temporal/harness_c.py, jobs_c.py, receiver_c.py, run_expC.py), offline only
(RULES-expC.md C.12).

No test starts a server, a worker, a client or a listening socket, and no test signals a real process: process
launches, CLI calls and signals are replaced with fakes. Expected values are written from RULES-expC.md (draft 4)
and DESIGN-expC.md, never from the harness's own output.

Run:  python3 -B -m unittest tests.test_harness_c      (STOPCHECK_HARNESS_C=<path> and STOPCHECK_RUN_EXPC=<path> test
other copies of the harness and the runner, as temporal/mutate_c.py does)
"""
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
TDIR = os.path.join(os.path.dirname(HERE), "temporal")


def _load(name, filename, variable=None):
    path = (os.environ.get(variable) if variable else None) or os.path.join(TDIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _load("harness_c_under_test", "harness_c.py", "STOPCHECK_HARNESS_C")
J = _load("jobs_c_under_test", "jobs_c.py", "STOPCHECK_JOBS_C")
RX = _load("receiver_c_under_test", "receiver_c.py")
RUN = _load("run_expC_under_test", "run_expC.py", "STOPCHECK_RUN_EXPC")
A = H.A
POSIX = hasattr(time, "CLOCK_MONOTONIC_RAW") and hasattr(signal, "SIGKILL")

TGT = {"workflow_id": "expc-t1-target", "run_id": "rT", "task_queue": "expc-target-t1"}
TWIN = {"workflow_id": "expc-t1-twin", "run_id": "rW", "task_queue": "expc-twin-t1"}


def eff(mono, kind, step=None, nonce="n1", role="target", ids=TGT, activity_id="work"):
    return {"route": "effect", "mono": mono, "role": role, "workflow_id": ids["workflow_id"],
            "run_id": ids["run_id"], "activity_id": activity_id, "exec_nonce": nonce, "step": step, "kind": kind}


def hist(*types_):
    return [{"id": i + 1, "type": t, "time": None, "attrs": {}} for i, t in enumerate(types_)]


def describe_json(status, pending=(), wft=None):
    j = {"workflowExecutionInfo": {"status": f"WORKFLOW_EXECUTION_STATUS_{status}"}, "workflowExtendedInfo": {}}
    if pending:
        j["pendingActivities"] = [{"activityId": a, "state": f"PENDING_ACTIVITY_STATE_{s}", "attempt": n}
                                  for a, s, n in pending]
    if wft:
        j["pendingWorkflowTask"] = {"state": f"PENDING_WORKFLOW_TASK_STATE_{wft}", "attempt": 1}
    return j


def norm(status, pending=(), wft=None):
    return A.norm_describe(describe_json(status, pending, wft))


def read(t_end, status=None, rc=0, label="r", show=None):
    parts = {"describe_target": {"t_start": t_end - 0.05, "t_end": t_end, "wall_end": t_end, "rc": rc,
                                 "timed_out": False, "json": describe_json(status) if rc == 0 else None,
                                 "stderr_tail": "" if rc == 0 else "connection refused"}}
    if show is not None:
        parts["show_target"] = {"t_start": t_end - 0.05, "t_end": t_end, "wall_end": t_end, "rc": 0,
                                "timed_out": False, "json": {"events": show}, "stderr_tail": ""}
    return {"label": label, "kind": "x", "t_start": t_end - 0.1, "t_end": t_end, "parts": parts}


def plan_full():
    return H.timing_plan(False)


def plan_fast():
    return H.timing_plan(True)


# ============================ timing plans and anchors (C.3, C.4, C.10) ============================
class TimingPlans(unittest.TestCase):
    def test_full_plan_matches_the_rules(self):
        p = plan_full()
        # Draft 5 C.3: anchor step 4, t_stop = t_a + 5.5 s (C1: accepted + 15 s); the check starts 5 s before the stop
        # with the 4.5 s I/O budget; C6 SIGTERM 1 s after the cancel returns; C7 restart at t_stop + 15 s; C8 cancel at
        # + 3 s and restart at + 20 s; C6's twin 25 s after the target
        self.assertEqual((p["anchor_step"], p["stop_after_anchor_s"], p["c1_stop_after_accepted_s"]), (4, 5.5, 15.0))
        self.assertEqual((p["precheck_lead_s"], p["precheck_io_reserve_s"], p["precheck_io_budget_s"]),
                         (5.0, 0.5, 4.5))
        self.assertEqual(p["protocol_draft"], 5)
        self.assertEqual((p["c6_server_delay_s"], p["c6_twin_delay_s"]), (1.0, 25.0))
        self.assertEqual((p["c7_restart_s"], p["c8_cancel_s"], p["c8_restart_s"]), (15.0, 3.0, 20.0))
        # C.4 step 5: 120 s watch; claim poll every 0.5 s until t_stop + 60 s; reads at the listed offsets;
        # capability jobs at + 112 s
        self.assertEqual(p["watch_s"], 120.0)
        self.assertEqual((p["claim_poll_every_s"], p["claim_poll_until_s"]), (0.5, 60.0))
        self.assertEqual(p["read_offsets_s"], [0, 2, 5, 10, 20, 30, 60, 90, 120])
        self.assertEqual(p["capability_at_s"], 112.0)
        # C.4 step 6: probe observation 60 s, every 10 s; DESIGN section 3: probe twin 30 s timer, 3 HB steps
        self.assertEqual((p["probe_obs_s"], p["probe_read_every_s"]), (60.0, 10.0))
        self.assertEqual((p["probe_twin_timer_s"], p["probe_twin_steps"]), (30.0, 3))
        # C.3 final gate; C.2 stage W fresh within 1.5 s; C.6 M3: the HB delivery bound is step 14 (StopPhase)
        self.assertEqual((p["precheck_margin_s"], p["stage_w_latest_within_s"]), (0.1, 1.5))
        self.assertEqual(p["delivery_bound_step"], 14)
        # DESIGN section 3: 30 s timer, 20 steps at 1 s, heartbeat timeout 5 s
        self.assertEqual((p["timer_s"], p["n_steps"], p["step_s"], p["hb_timeout_s"]), (30.0, 20, 1.0, 5.0))

    def test_fast_plan_matches_c10(self):
        p = plan_fast()
        self.assertEqual((p["timer_s"], p["n_steps"], p["step_s"], p["hb_timeout_s"]), (6.0, 8, 0.5, 2.0))
        self.assertEqual((p["anchor_step"], p["stop_after_anchor_s"], p["c1_stop_after_accepted_s"]), (3, 0.75, 3.0))
        self.assertEqual(p["watch_s"], 30.0)
        self.assertEqual(p["claim_poll_until_s"], 20.0)
        self.assertEqual(p["read_offsets_s"], [0, 2, 5, 10, 20, 30])
        self.assertEqual(p["capability_at_s"], 25.0)
        self.assertEqual((p["c6_twin_delay_s"], p["c7_restart_s"], p["c8_cancel_s"], p["c8_restart_s"]),
                         (5.0, 8.0, 1.5, 12.0))
        self.assertEqual((p["probe_obs_s"], p["probe_read_every_s"], p["probe_twin_timer_s"]), (20.0, 5.0, 8.0))
        self.assertTrue(p["fast"])
        self.assertEqual((p["precheck_lead_s"], p["precheck_io_reserve_s"], p["precheck_io_budget_s"]),
                         (0.75, 0.1, 0.65))
        self.assertIsNone(p["delivery_bound_step"])          # FAST is plumbing only: no delivery bound

    def test_planned_t_stop(self):
        self.assertEqual(H.planned_t_stop(H.spec_of("C2"), 100.0, plan_full()), 105.5)
        self.assertEqual(H.planned_t_stop(H.spec_of("C1"), 100.0, plan_full()), 115.0)
        self.assertEqual(H.planned_t_stop(H.spec_of("C2"), 100.0, plan_fast()), 100.75)
        self.assertEqual(H.planned_t_stop(H.spec_of("C1"), 100.0, plan_fast()), 103.0)

    def test_pre_check_starts_close_to_the_stop(self):
        # Draft 5: the full check starts 5 s before the stop, 0.5 s after the step-4 anchor; C1 starts at accepted
        # + 10 s. FAST remains 0.75 s before the stop. Stage T is seen near the stop and not in the moment after
        # `accepted`, when TimerStarted may not be written yet
        for fast, t_a, cond, start in ((False, 100.0, "C2", 100.5), (False, 100.0, "C1", 110.0),
                                       (True, 100.0, "C2", 100.0), (True, 100.0, "C1", 102.25)):
            p = plan_fast() if fast else plan_full()
            planned = H.planned_t_stop(H.spec_of(cond), t_a, p)
            self.assertAlmostEqual(H.pre_check_start(planned, p), start, msg=(fast, cond))
            # the budget before the deadline (planned t_stop - 0.1 s) is the same in every condition
            self.assertAlmostEqual(planned - p["precheck_margin_s"] - H.pre_check_start(planned, p),
                                   (0.65 if fast else 4.9), msg=(fast, cond))
            self.assertAlmostEqual(planned - p["precheck_io_reserve_s"] - H.pre_check_start(planned, p),
                                   (0.65 if fast else 4.5), msg=(fast, cond))

    def test_watch_covers_the_predicted_events(self):
        # Draft 5: the final effect is due 11.5 s after the stop (step k at (k-1) s, final at n s, stop at step 4
        # + 5.5 s); PC's start-to-close timeout is due 51.5 s after the kill; C7's second final falls inside the
        # watch. The 120 s watch leaves at least 60 s after each.
        p, w = plan_full(), J.WORKLOAD["full"]
        t_stop_after_start = (p["anchor_step"] - 1) * w["step_s"] + p["stop_after_anchor_s"]
        self.assertAlmostEqual(w["n_steps"] * w["step_s"] - t_stop_after_start, 11.5)
        pc_timeout = w["work_stc_s"] - t_stop_after_start
        self.assertAlmostEqual(pc_timeout, 51.5)
        self.assertGreaterEqual(p["watch_s"] - pc_timeout, 60 - 1e-9)
        c7_second_final = p["c7_restart_s"] + w["n_steps"] * w["step_s"] + 2.0      # 2 s for pick-up
        self.assertGreaterEqual(p["watch_s"] - c7_second_final, 60)

    def test_c8_restart_leaves_room_for_the_sticky_wait(self):
        # DESIGN section 4: C8's cancel at 3 s comes after the kill and before attempt 1's heartbeat timeout
        # (StopPhase); its restart at 20 s is after the 10 s sticky schedule-to-start timeout (T21)
        p = plan_full()
        self.assertEqual(p["c8_cancel_s"], 3.0)
        self.assertGreater(p["c8_restart_s"], 10.0 + p["c8_cancel_s"])


class StopPhase(unittest.TestCase):
    """Pre-freeze batch, item 2. Draft 4's stop at step 4 + 5.0 s fell on the step-9 emission (the PC pilot's step 9
    was sent 1.8 ms before t_stop) and on the nominal heartbeat RPC 8 s into the activity, so by-commit and
    by-initiation counts were decided by milliseconds. Draft 5 stops at step 4 + 5.5 s: half a step from every
    emission, 0.5 s after a nominal heartbeat RPC and 3.5 s before the next (DESIGN section 4; T4)."""

    def phase(self, p=None):
        p = p or plan_full()
        return (p["anchor_step"] - 1) * p["step_s"] + p["stop_after_anchor_s"]     # seconds into the activity

    def test_the_stop_falls_half_a_step_from_every_step_emission(self):
        p = plan_full()
        offset = self.phase(p) % p["step_s"]
        self.assertAlmostEqual(min(offset, p["step_s"] - offset), 0.5 * p["step_s"])
        self.assertAlmostEqual(self.phase(p), 8.5)

    def test_the_stop_falls_after_one_nominal_heartbeat_rpc_and_well_before_the_next(self):
        p = plan_full()
        throttle = 0.8 * p["hb_timeout_s"]                     # T4: 0.8 x the heartbeat timeout
        since = self.phase(p) % throttle
        self.assertAlmostEqual(since, 0.5)
        self.assertAlmostEqual(throttle - since, 3.5)

    def test_the_delivery_bound_is_the_step_after_the_one_at_the_next_heartbeat(self):
        # the next nominal RPC after the stop, 12 s in, carries the cancellation; step 13 is emitted at that moment
        # (a race), so the first step that cannot be posted is 14
        p = plan_full()
        throttle = 0.8 * p["hb_timeout_s"]
        next_rpc = (self.phase(p) // throttle + 1) * throttle
        self.assertAlmostEqual(next_rpc, 12.0)
        self.assertEqual(p["delivery_bound_step"], int(next_rpc // p["step_s"]) + 2)

    def test_c8_cancels_before_attempt_1s_heartbeat_timeout(self):
        # the last nominal RPC before the kill is 0.5 s earlier, so the 5 s heartbeat timeout falls 4.5 s after the
        # stop; the cancel at + 3 s precedes it by 1.5 s (K6, T10)
        p = plan_full()
        throttle = 0.8 * p["hb_timeout_s"]
        last_rpc = (self.phase(p) // throttle) * throttle
        timeout_after_stop = last_rpc + p["hb_timeout_s"] - self.phase(p)
        self.assertAlmostEqual(timeout_after_stop, 4.5)
        self.assertGreaterEqual(timeout_after_stop - p["c8_cancel_s"], 1.5 - 1e-9)

    def test_c6_sigterm_precedes_the_next_nominal_heartbeat_rpc(self):
        # the SIGTERM is due 1 s after the cancel returns; the activity's next nominal RPC is 3.5 s after the stop.
        # Whether the server is already down then is a race (DESIGN section 4): only the order of the two dues is set
        p = plan_full()
        throttle = 0.8 * p["hb_timeout_s"]
        self.assertLess(p["c6_server_delay_s"] + 0.5, throttle - self.phase(p) % throttle)

    def test_the_precheck_keeps_its_budget_inside_the_offset(self):
        p = plan_full()
        for cond in ("C2", "C1"):
            spec = H.spec_of(cond)
            planned = H.planned_t_stop(spec, 100.0, p)
            start = H.pre_check_start(planned, p)
            self.assertGreater(start, 100.0, cond)                  # after the anchor
            self.assertAlmostEqual(planned - p["precheck_io_reserve_s"] - start, 4.5)

    def test_fast_keeps_its_off_boundary_offsets(self):
        p = plan_fast()
        offset = self.phase(p) % p["step_s"]
        self.assertAlmostEqual(min(offset, p["step_s"] - offset), 0.5 * p["step_s"])
        self.assertEqual((p["stop_after_anchor_s"], p["precheck_lead_s"]), (0.75, 0.75))

    def test_watch_schedule_order_and_capability_roles(self):
        sch = H.watch_schedule(1000.0, plan_full(), "C2")
        self.assertEqual([x[0] for x in sch], sorted(x[0] for x in sch))
        reads = [x for x in sch if x[1] == "read"]
        self.assertEqual([x[2] for x in reads], [0, 2, 5, 10, 20, 30, 60, 90, 120])
        cap = [x for x in sch if x[1] == "capability"]
        self.assertEqual(len(cap), 1)
        self.assertEqual(cap[0][0], 1112.0)
        self.assertEqual(cap[0][3], ["target", "twin"])
        # the capability check falls between the +90 and +120 reads
        i = [x[1] for x in sch].index("capability")
        self.assertEqual((sch[i - 1][2], sch[i + 1][2]), (90, 120))
        # C.4 step 5: PC skips the target queue, whose worker is dead by design
        self.assertEqual([x for x in H.watch_schedule(0.0, plan_full(), "PC") if x[1] == "capability"][0][3],
                         ["twin"])

    def test_read_schedule_ends_at_the_watch_end(self):
        for p in (plan_full(), plan_fast()):
            self.assertEqual(p["read_offsets_s"][-1], p["watch_s"])
            self.assertLess(p["capability_at_s"], p["watch_s"])


class Anchors(unittest.TestCase):
    def test_anchor_is_step_4_of_the_first_execution(self):
        effects = [eff(10.0, "accepted", activity_id="announce", nonce="a"),
                   eff(41.0, "progress", 1), eff(42.0, "progress", 2), eff(43.0, "progress", 3),
                   eff(44.0, "progress", 4),
                   eff(43.5, "progress", 4, role="twin", ids=TWIN, nonce="tw"),     # a twin effect: ignored
                   eff(60.0, "progress", 4, nonce="n2")]                               # a later execution: ignored
        a = H.anchor_effect(effects, H.spec_of("C2"), TGT, plan_full())
        self.assertEqual((a["mono"], a["step"], a["exec_nonce"]), (44.0, 4, "n1"))

    def test_c1_anchor_is_the_accepted_effect(self):
        effects = [eff(10.0, "accepted", activity_id="announce", nonce="a"),
                   eff(9.0, "accepted", role="twin", ids=TWIN, activity_id="announce", nonce="b")]
        self.assertEqual(H.anchor_effect(effects, H.spec_of("C1"), TGT, plan_full())["mono"], 10.0)

    def test_no_anchor_without_the_step(self):
        effects = [eff(41.0, "progress", 1), eff(42.0, "progress", 2)]
        self.assertIsNone(H.anchor_effect(effects, H.spec_of("C2"), TGT, plan_full()))


# ============================ pre-intervention check (C.2, C.3) ============================
class PreCheck(unittest.TestCase):
    def test_gate_requires_the_check_to_end_before_the_margin(self):
        self.assertTrue(H.gate_open(101.39, 101.5, 0.1))
        self.assertFalse(H.gate_open(101.4, 101.5, 0.1))        # "must end before planned t_stop - 0.1 s"
        self.assertFalse(H.gate_open(101.45, 101.5, 0.1))
        self.assertFalse(H.gate_open(None, 101.5, 0.1))

    def test_overrun_withholds_and_fails_the_trial(self):
        out = H.pre_check_outcome({"completed_before_deadline": False, "ok": True})
        self.assertTrue(out["withhold"])
        self.assertIn("overran", out["trial_failure"])
        self.assertIsNone(out["untested"])

    def test_on_time_check_with_missing_evidence_runs_and_marks_untested(self):
        # C.2: missing evidence makes the target untested; the intervention still runs at planned t_stop
        out = H.pre_check_outcome({"completed_before_deadline": True, "ok": False, "failed": ["stage_w", "pending"]})
        self.assertFalse(out["withhold"])
        self.assertIsNone(out["trial_failure"])
        self.assertIn("stage_w", out["untested"])
        self.assertIn("pending", out["untested"])
        self.assertEqual(H.pre_check_outcome({"completed_before_deadline": True, "ok": True}),
                         {"withhold": False, "trial_failure": None, "untested": None})

    def test_required_evidence_per_condition(self):
        # C.2 planting-evidence table: job accepted, stage A and twin everywhere; stage T in C1; stage W and the
        # pending-activity evidence elsewhere; process identity in PC, C6, C7, C7l and C8
        ok = {"ok": True}
        for cond in H.CONDITIONS:
            spec = H.spec_of(cond)
            need = set(H.pre_check_decision(spec, {})["required"])
            self.assertTrue({"accepted", "stage_a", "twin"} <= need, cond)
            if cond == "C1":
                self.assertIn("stage_t", need)
                self.assertNotIn("stage_w", need)
            else:
                self.assertTrue({"stage_w", "pending"} <= need, cond)
            self.assertEqual("identity" in need, cond in ("PC", "C6", "C7", "C7l", "C8"), cond)
            full = {k: ok for k in need}
            self.assertTrue(H.pre_check_decision(spec, full)["ok"], cond)
            for k in need:                      # every item is needed: a missing one fails the check
                ev = dict(full)
                del ev[k]
                d = H.pre_check_decision(spec, ev)
                self.assertFalse(d["ok"])
                self.assertEqual(d["failed"], [k])
            ev = dict(full, accepted={"ok": None})
            self.assertFalse(H.pre_check_decision(spec, ev)["ok"])   # only ok is True counts

    def test_stage_w_evidence(self):
        p = plan_full()
        good = [eff(41.0, "progress", 1), eff(42.0, "progress", 2), eff(43.0, "progress", 3), eff(44.0, "progress", 4)]
        self.assertTrue(H.stage_w_evidence(good, TGT, p, 44.5)["ok"])
        self.assertFalse(H.stage_w_evidence(good, TGT, p, 45.6)["ok"])            # latest older than 1.5 s
        self.assertFalse(H.stage_w_evidence([e for e in good if e["step"] != 3], TGT, p, 44.5)["ok"])
        self.assertFalse(H.stage_w_evidence(good + [eff(44.2, "final", 20)], TGT, p, 44.5)["ok"])
        twin_only = [dict(e, role="twin", workflow_id=TWIN["workflow_id"], run_id=TWIN["run_id"]) for e in good]
        self.assertFalse(H.stage_w_evidence(twin_only, TGT, p, 44.5)["ok"])
        self.assertTrue(H.stage_w_evidence(good[:3], TGT, plan_fast(), 43.5)["ok"])   # FAST: steps 1-3

    def test_stage_t_evidence(self):
        self.assertTrue(H.stage_t_evidence(hist("WorkflowExecutionStarted", "TimerStarted"), [], TGT)["ok"])
        self.assertFalse(H.stage_t_evidence(hist("WorkflowExecutionStarted", "TimerStarted", "TimerFired"), [],
                                            TGT)["ok"])
        self.assertFalse(H.stage_t_evidence(hist("WorkflowExecutionStarted"), [], TGT)["ok"])
        self.assertFalse(H.stage_t_evidence(None, [], TGT)["ok"])
        self.assertFalse(H.stage_t_evidence(hist("TimerStarted"), [eff(1.0, "progress", 1)], TGT)["ok"])

    def test_pending_evidence(self):
        # C.2 stage W: HB and NH exactly one pending activity, STARTED, attempt 1; LA no MarkerRecorded
        h = hist("WorkflowExecutionStarted")
        self.assertTrue(H.pending_evidence(norm("RUNNING", [("work", "STARTED", 1)]), h, "HB")["ok"])
        self.assertFalse(H.pending_evidence(norm("RUNNING", [("work", "STARTED", 2)]), h, "HB")["ok"])
        self.assertFalse(H.pending_evidence(norm("RUNNING", [("work", "SCHEDULED", 1)]), h, "NH")["ok"])
        self.assertFalse(H.pending_evidence(norm("RUNNING", [("work", "STARTED", 1), ("x", "STARTED", 1)]), h,
                                            "HB")["ok"])
        self.assertFalse(H.pending_evidence(norm("RUNNING"), h, "HB")["ok"])
        self.assertFalse(H.pending_evidence(norm("CANCELED", [("work", "STARTED", 1)]), h, "HB")["ok"])
        self.assertFalse(H.pending_evidence(None, h, "HB")["ok"])
        self.assertTrue(H.pending_evidence(norm("RUNNING"), h, "LA")["ok"])
        self.assertFalse(H.pending_evidence(norm("RUNNING"), hist("WorkflowExecutionStarted", "MarkerRecorded"),
                                            "LA")["ok"])
        self.assertFalse(H.pending_evidence(norm("RUNNING"), None, "LA")["ok"])

    def test_twin_evidence_and_c6_timer(self):
        e = [eff(5.0, "accepted", role="twin", ids=TWIN, activity_id="announce")]
        self.assertTrue(H.twin_evidence(norm("RUNNING"), None, e, TWIN, in_timer=False)["ok"])
        self.assertFalse(H.twin_evidence(norm("RUNNING"), None, [], TWIN, in_timer=False)["ok"])
        self.assertFalse(H.twin_evidence(norm("COMPLETED"), None, e, TWIN, in_timer=False)["ok"])
        # C.2 twin row, C6: the twin must be in stage T
        self.assertTrue(H.twin_evidence(norm("RUNNING"), hist("WorkflowExecutionStarted", "TimerStarted"), e, TWIN,
                                        in_timer=True)["ok"])
        self.assertFalse(H.twin_evidence(norm("RUNNING"), hist("TimerStarted", "TimerFired"), e, TWIN,
                                         in_timer=True)["ok"])
        self.assertFalse(H.twin_evidence(norm("RUNNING"), None, e, TWIN, in_timer=True)["ok"])


class CheckedPreconditions(unittest.TestCase):
    def test_c6_precondition(self):
        # C.2: describe reads CANCELED before the server SIGTERM
        self.assertTrue(H.c6_precondition([read(10.0, "RUNNING"), read(10.4, "CANCELED")], 11.0)["ok"])
        self.assertFalse(H.c6_precondition([read(11.2, "CANCELED")], 11.0)["ok"])     # reply after the SIGTERM
        self.assertFalse(H.c6_precondition([read(10.2, "RUNNING")], 11.0)["ok"])
        self.assertFalse(H.c6_precondition([read(10.2, rc=1)], 11.0)["ok"])            # a failed read is not CANCELED
        self.assertFalse(H.c6_precondition([], 11.0)["ok"])

    def test_untested_after_a_failed_c6_precondition(self):
        self.assertIn("C6 precondition", H.untested_after_watch("C6", {"precondition": {"ok": False}}))
        self.assertIn("C6 precondition", H.untested_after_watch("C6", {}))
        self.assertIsNone(H.untested_after_watch("C6", {"precondition": {"ok": True}}))
        self.assertIsNone(H.untested_after_watch("C2", {}))

    def test_pre_probe_check(self):
        # C.4 step 6.2: the probe twin must be in its timer: TimerStarted, no TimerFired, no pending activity
        in_timer = hist("WorkflowExecutionStarted", "ActivityTaskScheduled", "ActivityTaskStarted",
                        "ActivityTaskCompleted", "TimerStarted")
        ds = {"target": norm("CANCELED"), "twin": norm("COMPLETED"), "ptwin": norm("RUNNING")}
        out = H.probe_precheck_decision(ds, in_timer)
        self.assertTrue(out["ok"])
        self.assertEqual(out["interrupted"], [])
        self.assertFalse(H.probe_precheck_decision(ds, in_timer + hist("TimerFired"))["ok"])
        self.assertFalse(H.probe_precheck_decision(ds, None)["ok"])
        ds2 = dict(ds, ptwin=norm("RUNNING", [("work", "SCHEDULED", 1)]))
        self.assertFalse(H.probe_precheck_decision(ds2, in_timer)["ok"])
        ds3 = dict(ds, ptwin=None)
        out3 = H.probe_precheck_decision(ds3, in_timer)
        self.assertFalse(out3["ok"])
        self.assertEqual(out3["unreadable"], ["ptwin"])

    def test_pre_probe_check_records_running_work(self):
        # a run with a STARTED pending activity: "the probe interrupted running work"; a STARTED workflow task is
        # recorded too
        ds = {"target": norm("RUNNING", [("work", "STARTED", 2)], wft="STARTED"), "twin": norm("COMPLETED"),
              "ptwin": norm("RUNNING")}
        out = H.probe_precheck_decision(ds, hist("TimerStarted"))
        self.assertTrue(out["ok"])                                   # the persistence control itself is fine
        self.assertEqual(out["interrupted"], ["target"])
        self.assertEqual(out["started_activities"]["target"], [("work", 2)])
        self.assertEqual(out["workflow_task_started"], ["target"])
        # PC's attempt 2 waits SCHEDULED: not running work (C.8 common row "Pre-probe check")
        ds["target"] = norm("RUNNING", [("work", "SCHEDULED", 2)])
        self.assertEqual(H.probe_precheck_decision(ds, hist("TimerStarted"))["interrupted"], [])

    def test_closed_run_listing_a_started_activity_is_not_running_work(self):
        # C.4 step 6.2 and C.8: whether a closed run lists its abandoned activity is unpredicted; such a listing is
        # recorded apart and is not "the probe interrupted running work"
        ds = {"target": norm("CANCELED", [("work", "STARTED", 1)]), "twin": norm("COMPLETED"),
              "ptwin": norm("RUNNING")}
        out = H.probe_precheck_decision(ds, hist("TimerStarted"))
        self.assertEqual(out["interrupted"], [])
        self.assertEqual(out["closed_run_lists_started"], ["target"])
        self.assertTrue(out["ok"])


# ============================ identity before signals (C.1) ============================
class FakePopen:
    def __init__(self, pid=4242, alive=True, exit_status=0, wait_times_out=False):
        self.pid, self._alive, self.returncode = pid, alive, None if alive else exit_status
        self.exit_status, self.wait_times_out = exit_status, wait_times_out

    def poll(self):
        return None if self._alive else self.returncode

    def wait(self, timeout=None):
        if self.wait_times_out:
            raise subprocess.TimeoutExpired("x", timeout)
        self._alive, self.returncode = False, self.exit_status
        return self.exit_status


LAUNCH = {"pid": 4242, "start_ticks": 777, "cmdline": ["/root/temporal-venv/bin/python", "-B", "jobs_c.py", "worker"]}


class Identity(unittest.TestCase):
    def test_identity_matches(self):
        info = dict(LAUNCH, state="S", netns_inode=1)
        self.assertTrue(H.identity_matches(LAUNCH, info))
        self.assertFalse(H.identity_matches(LAUNCH, dict(info, start_ticks=778)))     # a reused PID
        self.assertFalse(H.identity_matches(LAUNCH, dict(info, cmdline=["sleep", "100"])))
        self.assertFalse(H.identity_matches(LAUNCH, dict(info, pid=4243)))
        self.assertFalse(H.identity_matches(LAUNCH, None))                             # the process is gone
        self.assertFalse(H.identity_matches(None, info))
        self.assertFalse(H.identity_matches(dict(LAUNCH, cmdline=[]), dict(info, cmdline=[])))
        self.assertFalse(H.identity_matches(dict(LAUNCH, start_ticks=None), dict(info, start_ticks=None)))
        for field in ("netns_inode", "cgroup", "pgid", "sid", "exe"):
            self.assertFalse(H.identity_matches(dict(LAUNCH, **{field: "expected"}),
                                               dict(info, **{field: "different"})), field)

    def test_launch_waits_for_nonempty_exec_identity(self):
        # C0 smoke 13 Oct: the first /proc read had correct start ticks but empty cmdline.
        # Freezing that transient value made the later legitimate server signal fail its identity guard.
        command = [H.CLI, "server", "start-dev", "--db-filename", "/tmp/trial.db"]
        first = {"pid": 4242, "start_ticks": 777, "cmdline": [], "netns_inode": 17}
        settled = dict(first, cmdline=command)
        with tempfile.TemporaryDirectory() as tmp:
            P = H.Procs(tmp, {"launches": []})
            with patch.object(H.subprocess, "Popen", return_value=FakePopen()), \
                    patch.object(H, "proc_info", side_effect=[first, settled]), \
                    patch.object(H.time, "sleep", lambda _: None):
                launch = P.start("server", command, "server.log")
            P.p["server"]["log"].close()
        self.assertEqual(launch["start_ticks"], 777)
        self.assertEqual(launch["cmdline"], command)
        self.assertTrue(H.identity_matches(launch, settled))
        self.assertEqual(launch["identity_samples"][0]["cmdline"], [])
        self.assertIn("identity_verified_mono", launch)

    def test_launch_cannot_adopt_a_reused_pid(self):
        command = [H.CLI, "server", "start-dev"]
        first = {"pid": 4242, "start_ticks": 777, "cmdline": [], "netns_inode": 17}
        reused = dict(first, start_ticks=778, cmdline=command)
        with tempfile.TemporaryDirectory() as tmp:
            P = H.Procs(tmp, {"launches": []})
            with patch.object(H.subprocess, "Popen", return_value=FakePopen()), \
                    patch.object(H, "proc_info", side_effect=[first, reused]), \
                    patch.object(H.time, "sleep", lambda _: None):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    P.start("server", command, "server.log")
            for process in P.p.values():
                process["log"].close()

    def test_launch_without_exact_command_line_fails_boundedly(self):
        command = [H.CLI, "server", "start-dev"]
        unresolved = {"pid": 4242, "start_ticks": 777, "cmdline": [], "netns_inode": 17}
        with tempfile.TemporaryDirectory() as tmp:
            P = H.Procs(tmp, {"launches": []})
            with patch.object(H.subprocess, "Popen", return_value=FakePopen()), \
                    patch.object(H, "proc_info", return_value=unresolved), \
                    patch.object(H, "now", side_effect=[0.0, 0.0, 0.0, 3.0]):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    P.start("server", command, "server.log")
            self.assertIn("identity_error", P.R["launches"][0])
            P.p["server"]["log"].close()

    def _procs(self, tmp, popen):
        R = {"launches": []}
        P = H.Procs(tmp, R)
        P.p["worker-target"] = {"popen": popen, "launch": dict(LAUNCH), "log": io.BytesIO()}
        return P

    @unittest.skipUnless(POSIX, "needs CLOCK_MONOTONIC_RAW and POSIX signals")
    def test_mismatch_withholds_the_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            P = self._procs(tmp, FakePopen())
            a = {}
            with patch.object(H, "proc_info", return_value=dict(LAUNCH, start_ticks=1)), \
                    patch.object(H.os, "kill") as kill:
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    P.signal("worker-target", signal.SIGKILL, 1, a)
            kill.assert_not_called()
            self.assertFalse(a["identity_ok"])
            self.assertNotIn("t_send", a)

    @unittest.skipUnless(POSIX, "needs CLOCK_MONOTONIC_RAW and POSIX signals")
    def test_matching_identity_signals_and_records_the_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            P = self._procs(tmp, FakePopen(exit_status=-9))
            a = {}
            with patch.object(H, "proc_info", return_value=dict(LAUNCH)), patch.object(H.os, "kill") as kill:
                P.signal("worker-target", signal.SIGKILL, 1, a)
            kill.assert_called_once_with(4242, signal.SIGKILL)
            self.assertTrue(a["identity_ok"])
            self.assertLessEqual(a["t_send"], a["t_return"])
            self.assertEqual(a["exit_status"], -9)
            self.assertIsNotNone(a["exit_mono"])

    @unittest.skipUnless(POSIX, "needs CLOCK_MONOTONIC_RAW and POSIX signals")
    def test_unobserved_exit_is_recorded_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            P = self._procs(tmp, FakePopen(wait_times_out=True))
            a = {}
            with patch.object(H, "proc_info", return_value=dict(LAUNCH)), patch.object(H.os, "kill"):
                P.signal("worker-target", signal.SIGTERM, 0.01, a)
            self.assertIsNone(a["exit_mono"])
            self.assertIsNone(a["exit_status"])

    def test_no_launch_after_clean_up_has_begun(self):
        # C.4 step 7: an intervention thread still running at clean-up (C7's restart, C6's server restart) must not
        # start a process after stop_all, which would outlive the trial
        with tempfile.TemporaryDirectory() as tmp:
            P = H.Procs(tmp, {"launches": []})
            P.stop_all([])
            with patch.object(H.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "clean-up"):
                    P.start("worker-target", ["true"], "x.log")
            popen.assert_not_called()

    def test_refuses_to_replace_a_live_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            P = self._procs(tmp, FakePopen(alive=True))
            with patch.object(H.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "live process"):
                    P.start("worker-target", ["true"], "x.log")
            popen.assert_not_called()


# ============================ clean slate, clean-up and the environment (C.1, C.4) ============================
class FakeProc:
    """A Popen stand-in for the namespace wrapper: wait() answers from a script of results or exceptions."""

    def __init__(self, script):
        self.script, self.waits, self.killed, self.pid = list(script), [], False, 999

    def wait(self, timeout=None):
        self.waits.append(timeout)
        x = self.script.pop(0)
        if isinstance(x, BaseException):
            raise x
        return x

    def kill(self):
        self.killed = True


def done(args, rc=0, out=""):
    return subprocess.CompletedProcess(args, rc, out, "")


class CleanSlateAndCleanup(unittest.TestCase):
    def test_trial_processes_are_recognised_from_command_lines(self):
        # C.4 step 1: any process left from an earlier Experiment C trial, judged from its command line. The dev
        # server's command line names neither jobs_c.py nor an expc- ID, so it is recognised by its own command
        yes = (["/opt/temporal-cli-1.9.1/temporal", "server", "start-dev", "--db-filename",
                "/mnt/c/x/temporal/results/XC-pilot-1-C2-20261013-120000/temporal.db", "--ip", "127.0.0.1"],
               ["temporal", "server", "start-dev"],
               ["/root/temporal-venv/bin/python", "-B", "/mnt/c/x/temporal/jobs_c.py", "worker", "--address", "a"],
               ["/root/temporal-venv/bin/python", "-B", "/mnt/c/x/temporal/receiver_c.py", "--port", "8875"],
               ["/opt/temporal-cli-1.9.1/temporal", "workflow", "describe", "-w", "expc-t1-target"])
        no = (["ip", "netns", "exec", "expc-t1", "/usr/bin/python3", "-B", "/mnt/c/x/temporal/harness_c.py", "C2",
               "T1"], ["/usr/bin/python3", "-B", "/mnt/c/x/temporal/harness_c.py", "C2", "T1"],
              ["python3", "run_expC.py", "--pilot"], ["node", "/usr/bin/n8n", "start"], [], None)
        for c in yes:
            self.assertTrue(H.is_trial_process(c), c)
        for c in no:
            self.assertFalse(H.is_trial_process(c), c)

    def test_trial_env_has_no_temporal_variables(self):
        # C.1: no TEMPORAL_* variable (address, namespace, TLS, API key, profile, config file) reaches a trial process
        env = {"PATH": "/usr/bin", "HOME": "/root", "TEMPORAL_ADDRESS": "10.0.0.1:7233", "TEMPORAL_TLS": "true",
               "TEMPORAL_CONFIG_FILE": "/x.toml", "temporal_profile": "p", "STOPCHECK_FAST": "0"}
        self.assertEqual(H.trial_env(env), {"PATH": "/usr/bin", "HOME": "/root", "STOPCHECK_FAST": "0"})

    def test_cli_calls_ignore_configuration_and_temporal_variables(self):
        argv = H.cli_argv(["workflow", "describe"])
        # global options listed by `temporal options` (CLI 1.9.1): no config file, no environment configuration
        self.assertIn("--disable-config-file", argv)
        self.assertIn("--disable-config-env", argv)
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            return done(argv, 0, '{"a": 1}')
        with patch.dict(H.os.environ, {"TEMPORAL_ADDRESS": "10.0.0.1:7233"}), \
                patch.object(H.subprocess, "run", fake_run):
            out = H.cli(["workflow", "describe"])
        self.assertNotIn("TEMPORAL_ADDRESS", seen["env"])
        self.assertEqual(out["json"], {"a": 1})

    def test_launched_processes_get_the_trial_env(self):
        seen = {}

        def fake_popen(cmd, **kw):
            seen.update(kw)
            return FakePopen()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(H.os.environ, {"TEMPORAL_NAMESPACE": "other"}), \
                patch.object(H.subprocess, "Popen", fake_popen), \
                patch.object(H, "proc_info", return_value=dict(LAUNCH, cmdline=["true"])):
            P = H.Procs(tmp, {"launches": []})
            P.start("receiver", ["true"], "r.out")
            P.p["receiver"]["log"].close()
        self.assertNotIn("TEMPORAL_NAMESPACE", seen["env"])
        self.assertTrue(seen["start_new_session"])

    def test_receiver_startup_polls_are_not_health_checks(self):
        # C.9 lists four /health checks (start, before t_stop, end of watch, end of probe); the polls that wait for
        # the receiver to come up are readiness (C.1), so only one "start" entry is recorded
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            with patch.object(H, "http_get", side_effect=[ConnectionRefusedError(), ConnectionRefusedError(), 200]), \
                    patch.object(H.time, "sleep", lambda s: None):
                self.assertTrue(run.wait_receiver())
            self.assertEqual([(h["label"], h["ok"], h["polls"]) for h in run.R["controls"]["health"]],
                             [("start", True, 3)])
            run2 = H.TrialRun("C2", "T2", tmp, "pilot", False)
            with patch.object(H, "http_get", side_effect=ConnectionRefusedError()), \
                    patch.object(H.time, "sleep", lambda s: None):
                self.assertFalse(run2.wait_receiver(limit_s=0.0))
            self.assertEqual([(h["label"], h["ok"]) for h in run2.R["controls"]["health"]], [("start", False)])

    def _wrap(self, proc, leftovers):
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return done(argv)
        with patch.object(H.os.path, "exists", return_value=False), patch.object(H.subprocess, "run", fake_run), \
                patch.object(H.subprocess, "Popen", lambda *a, **k: proc), \
                patch.object(H, "acquire_host_locks", lambda: ([], {"locks": []})), \
                patch.object(H, "release_host_locks", lambda handles: None), \
                patch.object(H, "kill_namespace_leftovers", lambda ns: calls.append(["leftovers", ns]) or leftovers), \
                patch.object(H, "mark_record_void", lambda trial, why: calls.append(["void", trial])), \
                redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            rc = H.wrap_in_namespace("C2", "T1")
        return rc, calls

    def test_namespace_leftovers_are_stopped_before_the_namespace_is_deleted(self):
        # C.4 step 7: no trial process may outlive the trial; deleting a namespace does not stop its processes
        rc, calls = self._wrap(FakeProc([0]), [4242])
        self.assertEqual(rc, 2)                                   # a leftover voids the run
        i = calls.index(["leftovers", "expc-t1"])
        self.assertEqual(calls[i + 1:], [["void", "T1"], ["ip", "netns", "delete", "expc-t1"]])
        rc, calls = self._wrap(FakeProc([3]), [])
        self.assertEqual(rc, 3)
        self.assertNotIn(["void", "T1"], calls)
        self.assertEqual(calls[-1], ["ip", "netns", "delete", "expc-t1"])

    def test_mark_record_void(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "results", "T1-20261013-120000")
            os.makedirs(folder)
            with open(os.path.join(folder, "record.json"), "w", encoding="utf-8") as f:
                json.dump({"trial": "T1", "void": None}, f)
            with patch.object(H, "HERE", tmp):
                H.mark_record_void("T1", "leftover processes [4242]")
            with open(os.path.join(folder, "record.json"), encoding="utf-8") as f:
                R = json.load(f)
        self.assertEqual(R["void"], "leftover processes [4242]")
        self.assertEqual(R["trial"], "T1")

    def test_unlisted_namespace_is_not_assumed_empty(self):
        rc, _ = self._wrap(FakeProc([0]), None)                   # `ip netns pids` failed: not verified
        self.assertEqual(rc, 2)

    def test_interrupt_waits_for_the_trial_to_clean_up(self):
        # the trial process turns the interrupt into its own clean-up (finalize); killing it then would leave the
        # server, workers and receiver running
        proc = FakeProc([KeyboardInterrupt(), 2])
        with self.assertRaises(KeyboardInterrupt):
            self._wrap(proc, [])
        self.assertFalse(proc.killed)
        self.assertIsNotNone(proc.waits[1])                       # the second wait is bounded

    @unittest.skipUnless(POSIX, "needs POSIX signals")
    def test_kill_namespace_leftovers(self):
        sent = []
        with patch.object(H, "namespace_pids", side_effect=[[11, 12], []]), \
                patch.object(H.os, "kill", lambda pid, sig: sent.append((pid, sig))), \
                patch.object(H.time, "sleep", lambda s: None):
            self.assertEqual(H.kill_namespace_leftovers("expc-t1"), [11, 12])
        self.assertEqual(sent, [(11, signal.SIGTERM), (12, signal.SIGTERM)])
        sent.clear()
        with patch.object(H, "namespace_pids", return_value=[11]), \
                patch.object(H.os, "kill", lambda pid, sig: sent.append((pid, sig))), \
                patch.object(H.time, "sleep", lambda s: None):
            self.assertEqual(H.kill_namespace_leftovers("expc-t1", wait_s=0.0), [11])
        self.assertEqual(sent, [(11, signal.SIGTERM), (11, signal.SIGKILL)])
        with patch.object(H, "namespace_pids", return_value=None):
            self.assertIsNone(H.kill_namespace_leftovers("expc-t1"))
        with patch.object(H, "namespace_pids", return_value=[]):
            self.assertEqual(H.kill_namespace_leftovers("expc-t1"), [])

    def test_namespace_pids(self):
        with patch.object(H.subprocess, "run", return_value=done(["ip"], 0, "11\n12\n")):
            self.assertEqual(H.namespace_pids("expc-t1"), [11, 12])
        with patch.object(H.subprocess, "run", return_value=done(["ip"], 1, "")):
            self.assertIsNone(H.namespace_pids("expc-t1"))


# ============================ the intervention thread (C.3, as B.3) ============================
class Gate:
    def __init__(self, opened):
        self.opened = opened

    def wait(self, timeout=None):
        return self.opened


def new_box(planned):
    return {"planned_t_stop": planned, "actions": [], "restarts": [], "outages": [], "worker_dead": [],
            "_started": threading.Event(), "_first_return": threading.Event(),
            "_request_returned": threading.Event(), "_cancel": threading.Event()}


@unittest.skipUnless(POSIX, "needs CLOCK_MONOTONIC_RAW")
class Intervention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_of(self, cond, fast=False):
        with patch.object(H.subprocess, "Popen", side_effect=AssertionError("no launch")), \
                patch.object(H.subprocess, "run", side_effect=AssertionError("no launch")):
            run = H.TrialRun(cond, "T1", self.tmp.name, "smoke" if fast else "pilot", fast)
        run.ids["target"]["run_id"] = "rT"
        return run

    def test_construction_launches_nothing(self):
        run = self.run_of("C2")
        self.assertEqual(run.procs.p, {})
        self.assertEqual(run.R["launches"], [])

    def test_gate_never_opened_withholds(self):
        run = self.run_of("C2")
        box = new_box(H.now())
        with patch.object(run, "runner", side_effect=AssertionError("must not run")):
            run.run_intervention(box, Gate(False))
        self.assertTrue(box["withheld"])
        self.assertTrue(box["_request_returned"].is_set())          # the claim poll is released
        self.assertIn("thread_finished_mono", box)

    def test_withheld_flag_wins_over_an_opened_gate(self):
        run = self.run_of("C2")
        box = new_box(H.now())
        box["withheld"] = True
        with patch.object(run, "runner", side_effect=AssertionError("must not run")):
            run.run_intervention(box, Gate(True))
        self.assertTrue(box["withheld"])

    def test_thread_errors_are_recorded(self):
        run = self.run_of("C2")
        box = new_box(H.now())
        with patch.object(run, "runner", side_effect=RuntimeError("boom")):
            run.run_intervention(box, Gate(True))
        self.assertIn("boom", box["error"])
        self.assertTrue(box["_first_return"].is_set())

    def test_failed_request_is_never_accepted(self):
        # C.3: a non-zero exit or a timeout is a failed request; the trial fails
        run = self.run_of("C2")
        box = new_box(H.now())
        with patch.object(H, "cli", return_value={"rc": 1, "timed_out": False, "stderr_tail": "x", "t_start": 1,
                                                  "t_end": 2}):
            with self.assertRaisesRegex(RuntimeError, "failed request"):
                run.request(box, "cancel")
        self.assertEqual(box["actions"][0]["rc"], 1)
        self.assertTrue(box["_request_returned"].is_set())
        with patch.object(H, "cli", return_value={"rc": None, "timed_out": True, "stderr_tail": "timeout",
                                                  "t_start": 1, "t_end": 2}):
            with self.assertRaisesRegex(RuntimeError, "failed request"):
                run.request(new_box(H.now()), "terminate")

    def test_request_argv_and_t_stop_before_the_call(self):
        run = self.run_of("C5")
        box = new_box(H.now())
        seen = {}

        def fake_cli(args):
            seen["args"], seen["t_stop_set"] = args, "t_stop" in box
            return {"rc": 0, "timed_out": False, "stderr_tail": "", "t_start": H.now(), "t_end": H.now()}
        with patch.object(H, "cli", fake_cli):
            a = run.request(box, "terminate")
        self.assertTrue(seen["t_stop_set"])          # t_stop is the start of the procedure's first action
        self.assertEqual(seen["args"], ["workflow", "terminate", "-w", "expc-t1-target", "-r", "rT", "--reason",
                                        "expc-T1"])
        self.assertLessEqual(box["t_stop"]["mono"], a["t_send"])
        self.assertLessEqual(a["t_send"], a["t_return"])
        with patch.object(H, "cli", fake_cli):
            run.request(new_box(H.now()), "cancel")
        self.assertEqual(seen["args"], ["workflow", "cancel", "-w", "expc-t1-target", "-r", "rT"])

    def test_t_stop_is_stamped_once(self):
        run = self.run_of("C2")
        box = new_box(H.now())
        run.stamp(box, "t_stop")
        first = box["t_stop"]
        run.stamp(box, "t_stop")
        self.assertIs(box["t_stop"], first)
        self.assertTrue(box["_started"].is_set())

    def _sequence(self, run, box, kill_exit=True):
        calls = []

        def kill(b):
            run.stamp(b, "t_stop")
            calls.append(("kill", b["t_stop"]["mono"]))
            return {"exit_mono": H.now() if kill_exit else None}
        patches = [patch.object(run, "kill_target_worker", kill),
                   patch.object(run, "_sleep_until", lambda t, b: calls.append(("sleep_until", t))),
                   patch.object(run, "request", lambda b, name: calls.append(("request", name)) or {"t_return": 0}),
                   patch.object(run, "restart_target_worker", lambda b, at: calls.append(("restart", at)))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        run.runner(box)
        return calls

    def test_c8_sequence(self):
        # C.3: SIGKILL; at t_stop + 3 s, once the exit is confirmed, cancel; at t_stop + 20 s, restart
        run = self.run_of("C8")
        calls = self._sequence(run, new_box(H.now()))
        t = calls[0][1]
        self.assertEqual([c[0] for c in calls], ["kill", "sleep_until", "request", "restart"])
        self.assertAlmostEqual(calls[1][1], t + 3.0)
        self.assertEqual(calls[2][1], "cancel")
        self.assertAlmostEqual(calls[3][1], t + 20.0)
        run_fast = self.run_of("C8", fast=True)
        calls = self._sequence(run_fast, new_box(H.now()))
        t = calls[0][1]
        self.assertAlmostEqual(calls[1][1], t + 1.5)
        self.assertAlmostEqual(calls[3][1], t + 12.0)

    def test_c7_sequence_has_no_request(self):
        run = self.run_of("C7")
        calls = self._sequence(run, new_box(H.now()))
        self.assertEqual([c[0] for c in calls], ["kill", "restart"])
        self.assertAlmostEqual(calls[1][1], calls[0][1] + 15.0)

    def test_c6_sequence_checks_the_precondition_then_restarts_the_server(self):
        run = self.run_of("C6")
        box = new_box(H.now())
        t_ret = H.now()
        polls, restarts = [], []
        # the first poll reads CANCELED while its history (read in parallel) is not yet closed; the second has the
        # closed history that C6's last-event-ID comparison needs (C.2, C.6)
        shows = iter([[{"eventId": "1", "eventType": "WorkflowExecutionStarted"},
                       {"eventId": "2", "eventType": "ActivityTaskScheduled"}],
                      [{"eventId": "1", "eventType": "WorkflowExecutionStarted"},
                       {"eventId": "2", "eventType": "ActivityTaskScheduled"},
                       {"eventId": "3", "eventType": "WorkflowExecutionCanceled"}]])

        def fake_read(label, kind, parts, offset=None, parallel=False, deadline=None):
            polls.append(([n for n, _ in parts], parallel))
            r = read(H.now(), "CANCELED", show=next(shows))
            r["label"] = label
            return r
        with patch.object(run, "request", lambda b, name: {"t_return": t_ret, "name": name}), \
                patch.object(H, "read_parts", fake_read), patch.object(H.time, "sleep", lambda s: None), \
                patch.object(run, "server_restart", lambda b, at, reason: restarts.append((at, reason))):
            run.runner(box)
        self.assertEqual(polls[0], (["describe_target", "show_target"], True))
        self.assertEqual(len(polls), 2)                  # stops at the first CANCELED read with a closed history
        self.assertTrue(box["precondition"]["ok"])
        self.assertEqual(box["precondition"]["why"], "CANCELED at c6-pre-1")
        self.assertAlmostEqual(restarts[0][0], t_ret + 1.0)            # SIGTERM at t_return + 1.0 s
        self.assertEqual(restarts[0][1], "C6")

    def test_c6_poll_done(self):
        closed = [{"eventId": "1", "eventType": "WorkflowExecutionStarted"},
                  {"eventId": "3", "eventType": "WorkflowExecutionCanceled"}]
        self.assertTrue(H.c6_poll_done(read(1.0, "CANCELED", show=closed)))
        self.assertFalse(H.c6_poll_done(read(1.0, "CANCELED", show=closed[:1])))     # history not closed yet
        self.assertFalse(H.c6_poll_done(read(1.0, "CANCELED")))                     # no history read
        self.assertFalse(H.c6_poll_done(read(1.0, "RUNNING", show=closed)))
        self.assertFalse(H.c6_poll_done(read(1.0, rc=1)))

    def test_c8_cancel_withheld_without_a_confirmed_exit(self):
        run = self.run_of("C8")
        with self.assertRaisesRegex(RuntimeError, "exit is not confirmed"):
            self._sequence(run, new_box(H.now()), kill_exit=False)


@unittest.skipUnless(POSIX, "needs CLOCK_MONOTONIC_RAW")
class ClaimPoll(unittest.TestCase):
    def test_poll_starts_after_the_request_and_stops_at_the_first_closed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            run.ids["target"]["run_id"] = "rT"
            run.plan["claim_poll_every_s"] = 0.01
            box = new_box(H.now())
            run.stamp(box, "t_stop")
            box["actions"].append({"name": "cancel", "t_return": H.now()})
            box["_request_returned"].set()
            answers = iter(["RUNNING", "RUNNING", "CANCELED", "CANCELED"])

            def fake_read(label, kind, parts, offset=None, parallel=False, deadline=None):
                self.assertEqual([n for n, _ in parts], ["describe_target"])
                return dict(read(H.now(), next(answers)), label=label)
            with patch.object(H, "read_parts", fake_read):
                run.claim_poll(box, threading.Event())
            self.assertEqual([r["label"] for r in run.R["claim_poll"]], ["poll-1", "poll-2", "poll-3"])

    def test_no_poll_without_a_returned_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            box = new_box(H.now())
            stop = threading.Event()
            stop.set()
            with patch.object(H, "read_parts", side_effect=AssertionError("no read")):
                run.claim_poll(box, stop)
            self.assertEqual(run.R["claim_poll"], [])


# ============================ records, exit codes, conditions (C.3, C.9, C.10) ============================
class Records(unittest.TestCase):
    def test_new_record_has_every_field(self):
        R = H.new_record("C2w", "T1", "pilot", False)
        self.assertTrue(set(H.RECORD_FIELDS) <= set(R))
        self.assertEqual((R["experiment"], R["condition"], R["run_kind"], R["fast_mode"]), ("C", "C2w", "pilot", False))
        self.assertEqual(R["spec"]["cancellation_type"], "WAIT_CANCELLATION_COMPLETED")
        self.assertEqual(R["plan"]["watch_s"], 120.0)
        self.assertEqual((R["untested"], R["rerun_needed"], R["reads"], R["claim_poll"]), ([], False, [], []))
        self.assertIn("start-dev", R["scope"])
        self.assertIn("this configuration only", R["scope"])

    def test_exit_codes(self):
        # C.10: 0 completed; 3 ran with untested work (rerun needed); 2 error or void
        self.assertEqual(H.exit_code({}), 0)
        self.assertEqual(H.exit_code({"rerun_needed": True}), 3)
        self.assertEqual(H.exit_code({"error": "x"}), 2)
        self.assertEqual(H.exit_code({"void": "clock"}), 2)
        self.assertEqual(H.exit_code({"error": "x", "rerun_needed": True}), 2)

    def test_record_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "record.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("original")
            with self.assertRaises(FileExistsError), redirect_stdout(io.StringIO()):
                H.save({}, tmp)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "original")

    def test_conditions_match_the_rules_table(self):
        # C.3 table: variant, procedure kind, request, signal target and cancellation type of each condition
        expect = {
            "C0-HB": ("HB", False, None), "C0-NH": ("NH", False, None), "C0-LA": ("LA", False, None),
            "PC": ("NH", False, "target worker"), "C1": ("HB", True, None), "C2": ("HB", True, None),
            "C2w": ("HB", True, None), "C3": ("NH", True, None), "C3w": ("NH", True, None), "C4": ("LA", True, None),
            "C5": ("HB", True, None), "C5n": ("NH", True, None), "C6": ("HB", True, "server"),
            "C7": ("HB", False, "target worker"), "C7l": ("LA", False, "target worker"),
            "C8": ("HB", True, "target worker")}
        self.assertEqual(set(H.CONDITIONS), set(expect))
        self.assertEqual(set(RUN.CONDITIONS), set(expect))
        for cond, (variant, has_request, signalled) in expect.items():
            s = H.spec_of(cond)
            self.assertEqual((s["variant"], s["has_request"], s["signalled"]), (variant, has_request, signalled), cond)
            want_ct = "WAIT_CANCELLATION_COMPLETED" if cond in ("C2w", "C3w") else "TRY_CANCEL"
            self.assertEqual(s["cancellation_type"], want_ct, cond)
            self.assertEqual(s["anchor"], "accepted" if cond == "C1" else "step", cond)
        self.assertEqual(H.spec_of("C5")["procedure"], "terminate")
        self.assertEqual(H.spec_of("C5n")["procedure"], "terminate")
        with self.assertRaises(ValueError):
            H.spec_of("C9")

    def test_job_specs(self):
        # DESIGN section 3: the twin runs the target's workflow, variant and cancellation type on its own queue
        # (K10); the probe twin is a 30 s timer then a 3-step HB activity on the twin queue
        ids = H.ids_for("T1", "C2w")
        spec, p = H.spec_of("C2w"), plan_full()
        t = H.job_specs("T1", spec, ids, p, "target")
        w = H.job_specs("T1", spec, ids, p, "twin")
        pt = H.job_specs("T1", spec, ids, p, "ptwin")
        self.assertEqual({t["workflow"], w["workflow"], pt["workflow"]}, {"JobC"})
        self.assertEqual((t["params"]["variant"], w["params"]["variant"]), ("HB", "HB"))
        self.assertEqual({t["params"]["cancellation_type"], w["params"]["cancellation_type"]},
                         {"WAIT_CANCELLATION_COMPLETED"})
        self.assertNotEqual(t["task_queue"], w["task_queue"])
        self.assertEqual(pt["task_queue"], w["task_queue"])
        self.assertEqual((pt["params"]["variant"], pt["params"]["timer_s"], pt["params"]["n_steps"]), ("HB", 30.0, 3))
        self.assertEqual(len({t["workflow_id"], w["workflow_id"], pt["workflow_id"]}), 3)
        for s in (t, w, pt):
            self.assertEqual(s["params"]["receiver"], "127.0.0.1:8875")
            self.assertEqual(s["params"]["queue"], s["task_queue"])

    def test_cli_arguments(self):
        ident = {"workflow_id": "w", "run_id": "r"}
        self.assertEqual(H.describe_args(ident), ["workflow", "describe", "-w", "w", "-r", "r", "--raw"])
        self.assertEqual(H.show_args(ident), ["workflow", "show", "-w", "w", "-r", "r"])
        self.assertEqual(H.list_args(ident), ["workflow", "list", "--query", 'WorkflowId="w"'])
        argv = H.cli_argv(["workflow", "describe"])
        # C.1: every observer call has --address 127.0.0.1:7833 --output json --command-timeout 5s
        for a, b in (("--address", "127.0.0.1:7833"), ("--output", "json"), ("--command-timeout", "5s"),
                     ("--namespace", "default")):
            self.assertEqual(argv[argv.index(a) + 1], b)
        self.assertEqual(argv[1:3], ["workflow", "describe"])

    def test_server_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C6", "T1", tmp, "pilot", False)
            cmd = run.server_cmd()
        # C.1 server command: --db-filename in the trial folder, loopback, fixed ports, headless, JSON logs
        self.assertEqual(cmd[1:3], ["server", "start-dev"])
        self.assertEqual(cmd[cmd.index("--db-filename") + 1], os.path.join(tmp, "temporal.db"))
        for a, b in (("--ip", "127.0.0.1"), ("--port", "7833"), ("--http-port", "7834"), ("--metrics-port", "7835"),
                     ("--log-format", "json")):
            self.assertEqual(cmd[cmd.index(a) + 1], b)
        self.assertIn("--headless", cmd)

    def test_listening_ports(self):
        text = ("  sl  local_address rem_address   st tx_queue rx_queue\n"
                "   0: 0100007F:1E99 00000000:0000 0A 00000000:00000000\n"       # 127.0.0.1:7833 LISTEN
                "   1: 0100007F:22AB 0100007F:1E99 01 00000000:00000000\n")      # established: not a listener
        self.assertEqual(H.listening_ports(text), {7833})

    def test_parse_json_output(self):
        self.assertEqual(H.parse_json_output('{"a": 1}'), {"a": 1})
        self.assertEqual(H.parse_json_output('{"a": 1}\n{"a": 2}\n'), [{"a": 1}, {"a": 2}])
        self.assertIsNone(H.parse_json_output("not json"))
        self.assertIsNone(H.parse_json_output(""))

    def test_receiver_log_keeps_an_unfinished_line_for_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "receiver.jsonl")
            log = H.ReceiverLog(path)
            self.assertEqual(log.poll(), [])                       # no file yet
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"route": "effect", "mono": 1, "kind": "tick"}\n{"route": "effect", "mo')
            self.assertEqual(len(log.poll()), 1)
            with open(path, "a", encoding="utf-8") as f:
                f.write('no": 2, "kind": "tick"}\n{"bad": "x", "route": "effect"}\n')
            self.assertEqual([r.get("mono") for r in log.poll()], [1, 2, None])
            self.assertEqual(len(log.effects()), 2)                # a 400-logged body is not an effect
            with open(path, "a", encoding="utf-8") as f:
                f.write("garbage\n")
            with self.assertRaises(ValueError):                    # a malformed complete line is never hidden
                log.poll()


class RunKinds(unittest.TestCase):
    def test_run_kind_checks(self):
        H.check_run_kind("smoke", True, None)
        H.check_run_kind("pilot", False, None)
        H.check_run_kind("screen", False, "/x/FREEZE-expC.json")
        for kind, fast, manifest in (("pilot", True, None), ("screen", True, "m"), ("smoke", False, None),
                                     ("screen", False, None), ("other", False, None)):
            with self.assertRaises(ValueError, msg=(kind, fast, manifest)):
                H.check_run_kind(kind, fast, manifest)

    def test_main_refuses_before_launching(self):
        with patch.object(H, "wrap_in_namespace", side_effect=AssertionError("no launch")):
            for argv in (["C9", "T1"], ["C2"], ["C2", "bad id!"], ["C2", "-T1"]):
                with self.assertRaises(SystemExit, msg=argv):
                    H.main(argv)
            with patch.object(H.os, "geteuid", return_value=1000, create=True):
                with self.assertRaisesRegex(SystemExit, "root"):
                    H.main(["C2", "T1"])


# ============================ the runner (C.8, C.10, C.12) ============================
class Runner(unittest.TestCase):
    def test_labels_follow_c7(self):
        for ok in ("P", "C", "O", "S", "M", "P|K2", "P|K2,K3", "P|K4,K6+M", "C+M", "M", "—"):
            self.assertTrue(RUN.check_label(ok), ok)
        for bad in ("X", "P|", "P|K2+", "PK2", "P|K2,", "P+K2", "", None, "P|C2"):
            self.assertFalse(RUN.check_label(bad), bad)

    def _predictions(self, tmp, conditions, drop=None, label="P"):
        cell = {"prediction": "p", "label": label, "basis": "b", "refutation": "r"}
        measures = {m: dict(cell) for m in RUN.MEASURES if m != drop}
        data = {"schema_version": 1, "prior_exposure": "none", "configuration": {"cli": "1.9.1"},
                "conditions": {c: {"measures": measures} for c in conditions}}
        path = os.path.join(tmp, "p.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_prediction_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            RUN.read_predictions(self._predictions(tmp, ["C2"]), ["C2"])
            with self.assertRaisesRegex(ValueError, "measures must include"):
                RUN.read_predictions(self._predictions(tmp, ["C2"], drop="pending_on_closed"), ["C2"])
            with self.assertRaisesRegex(ValueError, "does not follow C.7"):
                RUN.read_predictions(self._predictions(tmp, ["C2"], label="P|X"), ["C2"])
            with self.assertRaisesRegex(ValueError, "measures must include"):
                RUN.read_predictions(self._predictions(tmp, ["C2"]), ["C2", "C3"])
        # C.8: the measures every condition must carry
        self.assertEqual(set(RUN.MEASURES), {"request_accepted", "state", "effects_after_request", "false_all_clear",
                                             "duplicates", "history_coverage", "pending_on_closed",
                                             "cancel_first_seen", "restart_outcome", "twin"})

    def test_screen_refused_without_a_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "FREEZE-expC.json")
            with self.assertRaisesRegex(ValueError, "Screen refused"):
                RUN.verify_freeze(missing, ["C2"], run_checks=False)
            with patch.object(RUN, "offline_checks"), \
                    patch.object(RUN.subprocess, "run", side_effect=AssertionError("no trial launch")), \
                    patch.object(RUN.subprocess, "Popen", side_effect=AssertionError("no trial launch")):
                with self.assertRaisesRegex(ValueError, "Screen refused"):
                    RUN.main(["--freeze", missing, "--conditions", "C2"])

    def test_interrupted_trial_is_waited_for_not_killed(self):
        # as the namespace wrapper: the harness cleans up on an interrupt, so the runner waits (bounded) for it
        proc = FakeProc([KeyboardInterrupt(), 2])
        with patch.object(RUN.subprocess, "Popen", lambda *a, **k: proc), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                RUN.run_trial(["python3", "harness_c.py", "C2", "T1"], {}, ".")
        self.assertFalse(proc.killed)
        self.assertIsNotNone(proc.waits[1])
        proc = FakeProc([0])
        with patch.object(RUN.subprocess, "Popen", lambda *a, **k: proc):
            self.assertEqual(RUN.run_trial(["x"], {}, "."), 0)

    def test_pilot_requirements(self):
        # C.10: a pilot backs the freeze only if it shows what the pilots must show
        good = {"condition": "C2", "fast_mode": False, "run_kind": "pilot", "source_hashes": {"a": "1"},
                "schema_version": 2,
                "clock_start": {"ok": True}, "clock_end": {"ok": True},
                "clock_across_trial": {"ok": True, "wall_clock": {"ok": True}},
                "pre_check": {"ok": True, "completed_before_deadline": True},
                # the probe restarts the server and both workers, each with a new identity (C.1)
                "launches": [{"name": n, "replaces": {"pid": 1, "start_ticks": 1, "new_identity": True}}
                             for n in ("server", "worker-target", "worker-twin")],
                "probe": {"pre_check": {"ok": True}, "restarts": [{"ok": True}, {"ok": True}]}}
        self.assertEqual(RUN.pilot_problems(good, {"a": "1"}), [])
        for change, word in (({"fast_mode": True}, "full-length"), ({"source_hashes": {"a": "2"}}, "manifest"),
                             ({"untested": ["target"]}, "untested"), ({"clock_end": {"ok": False}}, "clock_end"),
                             # item 5: the prospective wall/monotonic receipt, in a current-schema record
                             ({"clock_across_trial": {"ok": True}}, "wall"),
                             ({"clock_across_trial": {"ok": True, "wall_clock": {"ok": False}}}, "wall"),
                             ({"schema_version": 1}, "schema"), ({"schema_version": None}, "schema"),
                             ({"pre_check": {"ok": False}}, "pre-intervention"),
                             ({"pre_check": {"ok": True, "completed_before_deadline": False}}, "pre-intervention"),
                             ({"probe": None}, "pre-probe"), ({"probe": {"pre_check": {}}}, "pre-probe"),
                             ({"probe": {"pre_check": {"ok": True}, "restarts": [{"ok": False}]}}, "restart")):
            problems = RUN.pilot_problems(dict(good, **change), {"a": "1"})
            self.assertTrue(problems and any(word in p for p in problems), (change, problems))
        c6 = dict(good, condition="C6")
        self.assertTrue(any("C6 precondition" in p for p in RUN.pilot_problems(c6, {"a": "1"})))
        c6["intervention"] = {"precondition": {"ok": True}}
        self.assertEqual(RUN.pilot_problems(c6, {"a": "1"}), [])
        # PC: the gate passes and the probe detects the re-run (scored rows passed in)
        pc = dict(good, condition="PC")
        gate = {"measure": "PC", "outcome": "no effect after the kill (gate)", "kind": "pass"}
        probe = {"measure": "restart", "outcome": "durability probe", "kind": "re-run, closed after restart: COMPLETED"}
        self.assertEqual(RUN.pilot_problems(pc, {"a": "1"}, [gate, probe]), [])
        self.assertTrue(any("re-run" in p for p in RUN.pilot_problems(pc, {"a": "1"}, [gate, dict(probe,
                                                                                    kind="stayed ended")])))
        self.assertTrue(any("gate" in p for p in RUN.pilot_problems(pc, {"a": "1"}, [dict(gate, kind="fail"), probe])))

    def test_dry_run_launches_nothing(self):
        out = io.StringIO()
        with patch.object(RUN.subprocess, "run", side_effect=AssertionError("no launch")), \
                patch.object(RUN.subprocess, "Popen", side_effect=AssertionError("no launch")), redirect_stdout(out):
            self.assertEqual(RUN.main(["--dry-run", "--pilot"]), 0)
        plan = json.loads(out.getvalue())
        self.assertFalse(plan["launches_anything"])
        self.assertEqual(plan["run_kind"], "pilot")
        # C.10: the required pilot set
        self.assertEqual(plan["conditions"], ["C0-HB", "C0-NH", "C0-LA", "PC", "C2", "C3", "C6"])

    def test_screen_order_is_seeded(self):
        a = RUN.plan(RUN.parse_args(["--seed", "7"]))
        b = RUN.plan(RUN.parse_args(["--seed", "7"]))
        self.assertEqual(a, b)
        self.assertEqual(a[0], "screen")
        self.assertEqual(sorted(a[1]), sorted(RUN.CONDITIONS))
        self.assertEqual(RUN.plan(RUN.parse_args(["--smoke"])), ("smoke", ["C0-HB"]))

    def test_freeze_binds_the_reused_files_and_both_test_files(self):
        hashes = RUN.source_hashes()
        for p in ("common/clockguard.py", "common/guards.py", "langgraph/recorder.py", "tests/test_analyze_c.py",
                  "tests/test_harness_c.py", "temporal/RULES-expC.md", "temporal/DESIGN-expC.md",
                  "temporal/harness_c.py", "temporal/jobs_c.py", "temporal/receiver_c.py", "temporal/analyze_c.py",
                  "temporal/run_expC.py",
                  # pre-freeze batch: the shared wall/monotonic rule, the mutation check and their tests, and the
                  # clock guard's tests, which the offline checks run
                  "temporal/clockwall_c.py", "temporal/mutate_c.py", "tests/test_clockwall_c.py",
                  "tests/test_runner_c.py", "common/test_clockguard.py"):
            self.assertIn(p, hashes)
        self.assertFalse(any(p.startswith("n8n/") for p in hashes))


# ============================ the workload (jobs_c.py) and the receiver (receiver_c.py) ============================
class Workload(unittest.TestCase):
    def test_full_and_fast_parameters(self):
        f = J.make_params(trial="T1", role="target", variant="HB", fast=False, receiver="127.0.0.1:8875")
        # DESIGN section 3: 30 s timer, steps 1-20 one per second, heartbeat timeout 5 s, start-to-close 60 s,
        # announce 10 s, retry 1 s x 2.0 up to 10 s, at most 3 attempts, workflow execution timeout 15 min (K13)
        self.assertEqual((f["timer_s"], f["n_steps"], f["step_s"], f["hb_timeout_s"], f["work_stc_s"],
                          f["announce_stc_s"], f["execution_timeout_s"]), (30.0, 20, 1.0, 5.0, 60.0, 10.0, 900.0))
        self.assertEqual(f["retry"], {"initial_s": 1.0, "coefficient": 2.0, "max_s": 10.0, "max_attempts": 3})
        self.assertEqual(f["cancellation_type"], "TRY_CANCEL")          # K3
        s = J.make_params(trial="T1", role="target", variant="HB", fast=True, receiver="127.0.0.1:8875")
        self.assertEqual((s["timer_s"], s["n_steps"], s["step_s"], s["hb_timeout_s"]), (6.0, 8, 0.5, 2.0))
        f["retry"]["max_attempts"] = 99                                  # make_params copies the template
        self.assertEqual(J.WORKLOAD["full"]["retry"]["max_attempts"], 3)

    def test_bad_jobs_are_refused_before_they_start(self):
        base = dict(trial="T1", role="target", variant="HB", fast=False, receiver="127.0.0.1:8875")
        for change in ({"receiver": "10.0.0.5:8875"}, {"receiver": "localhost:8875"}, {"receiver": "127.0.0.1:x"},
                       {"variant": "XX"}, {"role": "agent"}, {"cancellation_type": "ABANDON"},
                       {"variant": "LA", "cancellation_type": "WAIT_CANCELLATION_COMPLETED"}, {"n_steps": 0}):
            with self.assertRaises(ValueError, msg=change):
                J.make_params(**dict(base, **change))
        p = J.make_params(**base)
        p["retry"]["max_attempts"] = 0                                   # K5: the retry policy must be bounded
        with self.assertRaises(ValueError):
            J.validate_params(p)

    def test_effect_record_fields(self):
        p = J.make_params(trial="T1", role="twin", variant="NH", fast=False, receiver="127.0.0.1:8875", queue="q")
        info = {"workflow_id": "w", "run_id": "r", "activity_type": "work_nh", "activity_id": "work", "attempt": 2,
                "is_local": False}
        rec = J.effect_record(p, kind="progress", step=3, exec_nonce="abc", info=info, identity="twin-t1-g1", pid=9)
        # DESIGN section 3, effect record
        for k in ("trial", "role", "workflow_id", "run_id", "activity_type", "activity_id", "variant", "attempt",
                  "is_local", "exec_nonce", "step", "kind", "identity", "pid", "smono", "swall"):
            self.assertIn(k, rec)
        self.assertEqual((rec["role"], rec["attempt"], rec["exec_nonce"], rec["step"]), ("twin", 2, "abc", 3))
        self.assertFalse(set(rec) & set(RX.RESERVED))                    # the receiver adds its own stamps
        with self.assertRaises(ValueError):
            J.effect_record(p, kind="other", step=None, exec_nonce="x")

    def test_http_helpers(self):
        req = J.build_post("127.0.0.1", 8875, "/effect", {"a": 1})
        head, body = req.split(b"\r\n\r\n", 1)
        self.assertTrue(head.startswith(b"POST /effect HTTP/1.1\r\n"))
        self.assertIn(b"Content-Length: 7", head)
        self.assertIn(b"Connection: close", head)
        self.assertEqual(json.loads(body), {"a": 1})
        self.assertEqual(J.parse_status(b"HTTP/1.0 200 OK\r\n"), 200)
        self.assertEqual(J.parse_status(b"HTTP/1.1 400 Bad\r\n"), 400)
        self.assertIsNone(J.parse_status(b""))
        self.assertIsNone(J.parse_status(b"garbage"))

    def test_cancellation_flags_and_options(self):
        self.assertIsNone(J.cancellation_flags(None))
        d = types.SimpleNamespace(not_found=True, cancel_requested=False)
        self.assertEqual(J.cancellation_flags(d), {"not_found": True, "cancel_requested": False, "paused": False,
                                                   "reset": False, "timed_out": False, "worker_shutdown": False})
        from datetime import timedelta
        o = J.options_record({"task_queue": "q", "graceful_shutdown_timeout": timedelta(0),
                              "max_cached_workflows": 1000})
        self.assertEqual((o["task_queue"], o["graceful_shutdown_timeout"], o["max_cached_workflows"]),
                         ("q", "0:00:00", 1000))
        self.assertIsNone(o["identity"])
        json.dumps(o)

    def test_importable_without_temporalio(self):
        # the workflow classes exist as module attributes either way (the sandbox imports them by name, T26)
        self.assertEqual(set(J.WORKFLOWS), {"JobC", "ReadyC"})
        self.assertEqual(len(J.ACTIVITIES), 5)
        if importlib.util.find_spec("temporalio") is None:
            self.assertFalse(J.HAVE_TEMPORAL)
            with self.assertRaisesRegex(SystemExit, "temporalio"):
                J.main(["worker", "--address", "a", "--task-queue", "q", "--identity", "i", "--log", "l"])

    def test_work_progress_tracks_the_phase_and_the_effect_in_flight(self):
        # review, minor (K8): where the activity is when a cancellation hits it, kept in memory and written only in its
        # last log line
        p = J.WorkProgress()
        self.assertEqual(p.snapshot(), {"phase": "starting", "inflight": None, "replies": []})
        p.posting(3, "progress")
        p.sent(10.0)
        self.assertEqual(p.snapshot()["phase"], "posting 3")
        self.assertEqual(p.snapshot()["inflight"], {"step": 3, "kind": "progress", "smono": 10.0})
        p.replied(10.2)
        self.assertIsNone(p.snapshot()["inflight"])
        self.assertEqual(p.snapshot()["replies"], [[3, "progress", 10.0, 10.2]])
        p.heartbeat(3)
        self.assertEqual(p.snapshot()["phase"], "heartbeat 3")
        p.sleeping(3)
        self.assertEqual(p.snapshot()["phase"], "sleeping after 3")
        p.posting(20, "final")
        self.assertEqual(p.snapshot()["phase"], "posting final")

    def test_post_effect_marks_the_send_and_the_reply_and_keeps_an_interrupted_post_in_flight(self):
        import asyncio
        params = J.make_params(trial="T1", role="target", variant="HB", fast=False, receiver="127.0.0.1:8875")

        async def ok(host, port, body):
            return None

        async def interrupted(host, port, body):
            raise asyncio.CancelledError()
        p = J.WorkProgress()
        p.posting(1, "progress")
        with patch.object(J, "_post", ok):
            rec = asyncio.run(J.post_effect(params, {"kind": "progress"}, progress=p))
        self.assertIsNone(p.snapshot()["inflight"])
        self.assertEqual([r[:3] for r in p.snapshot()["replies"]], [[1, "progress", rec["smono"]]])
        p.posting(2, "progress")
        with patch.object(J, "_post", interrupted), self.assertRaises(asyncio.CancelledError):
            asyncio.run(J.post_effect(params, {"kind": "progress"}, progress=p))
        self.assertEqual(p.snapshot()["inflight"]["step"], 2)                 # sent, never answered
        self.assertIsNotNone(p.snapshot()["inflight"]["smono"])
        self.assertEqual(len(p.snapshot()["replies"]), 1)

    def test_wlog_writes_one_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "w.jsonl")
            with patch.object(J, "LOG_PATH", path), patch.object(J, "IDENTITY", "target-t1-g1"):
                J.wlog("activity_cancelled", exec_nonce="n1", flags={"not_found": True})
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(x) for x in f]
        self.assertEqual(rows[0]["event"], "activity_cancelled")
        self.assertEqual(rows[0]["identity"], "target-t1-g1")
        self.assertIn("smono", rows[0])


class Receiver(unittest.TestCase):
    def test_effect_line_adds_sequence_and_arrival_stamps(self):
        status, line = RX.effect_line(json.dumps({"kind": "tick", "n": 1}).encode(), (1.5, 2.5, 3.5), 7)
        self.assertEqual(status, 200)
        self.assertEqual((line["route"], line["seq"], line["t"], line["mono"], line["raw"]),
                         ("effect", 7, 1.5, 2.5, 3.5))
        self.assertEqual((line["kind"], line["n"]), ("tick", 1))

    def test_bad_bodies_are_answered_400_and_logged(self):
        for body in (b"[1, 2]", b"not json", b"\xff\xfe", json.dumps({"mono": 1}).encode(),
                     json.dumps({"seq": 1}).encode()):
            status, line = RX.effect_line(body, (1.0, 2.0, 3.0), 1)
            self.assertEqual(status, 400, body)
            self.assertIn("bad", line)
            self.assertEqual(line["mono"], 2.0)          # still stamped on arrival

    def test_sequence_numbers_increase(self):
        state, lock = {"n": 0}, threading.Lock()
        self.assertEqual([RX.next_seq(state, lock) for _ in range(3)], [1, 2, 3])

    def test_receiver_binds_loopback_only(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("http.server.ThreadingHTTPServer", side_effect=AssertionError("no socket")):
            with self.assertRaisesRegex(SystemExit, "127.0.0.1"):
                RX.main(["--host", "0.0.0.0", "--out", os.path.join(tmp, "r.jsonl")])


class ObservationBudgets(unittest.TestCase):
    def test_full_parallel_four_second_reads_finish_before_fixed_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            run.ids["target"]["run_id"] = "rT"
            run.ids["twin"]["run_id"] = "rW"
            anchor = H.now() - 0.5                   # the check starts now, 0.5 s after the anchor (draft 5)
            planned = H.planned_t_stop(run.spec, anchor, run.plan)
            self.assertAlmostEqual(planned - anchor, 5.5)
            self.assertAlmostEqual(H.pre_check_start(planned, run.plan), anchor + 0.5)
            together = threading.Barrier(3)
            timeouts = []
            def slow_parallel_cli(argv, **kwargs):
                timeouts.append(kwargs["timeout"])
                together.wait(timeout=1.0)
                time.sleep(4.0)
                payload = ({"events": [{"eventId": "1", "eventType": "WorkflowExecutionStarted"}]}
                           if argv[2] == "show" else describe_json("RUNNING", [("work", "STARTED", 1)]))
                return done(argv, 0, json.dumps(payload))
            run.rx.effects = lambda: [eff(H.now() - 0.5, "accepted"),
                                      eff(H.now() - 0.5, "accepted", role="twin", ids=TWIN)] + [
                                          eff(H.now() - 0.01, "progress", step) for step in range(1, 5)]
            with patch.object(H.subprocess, "run", slow_parallel_cli):
                result = run.pre_check(planned)
            self.assertEqual(len(timeouts), 3)
            self.assertTrue(all(4.0 < value <= 4.5 for value in timeouts))
            self.assertTrue(result["ok"])
            self.assertTrue(result["completed_before_deadline"])
            self.assertEqual(result["planned_t_stop"], planned)
            self.assertLess(result["end_mono"], planned - 0.1)

    def test_reaping_or_validation_overrun_still_withholds_fixed_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            run.ids["target"]["run_id"] = "rT"
            run.ids["twin"]["run_id"] = "rW"
            planned = 100.0
            with patch.object(H, "read_parts", return_value={"parts": {}}) as call, \
                    patch.object(H, "now", side_effect=[95.0, 100.2, 100.2]):
                result = run.pre_check(planned)
            self.assertEqual(call.call_args.kwargs["deadline"], 99.5)
            self.assertEqual(result["planned_t_stop"], 100.0)
            self.assertFalse(result["completed_before_deadline"])
            self.assertTrue(H.pre_check_outcome(result)["withhold"])

    def test_expired_cli_budget_launches_nothing(self):
        with patch.object(H, "now", return_value=10.0), \
                patch.object(H.subprocess, "run", side_effect=AssertionError("late CLI must not launch")):
            result = H.cli(["workflow", "describe"], deadline=10.0)
        self.assertTrue(result["deadline_exhausted"])
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["rc"])
        self.assertIsNone(result["json"])

    def test_cli_timeout_uses_remaining_phase_budget(self):
        with patch.object(H, "now", return_value=10.0), \
                patch.object(H.subprocess, "run", return_value=done([], 0, '{"status": "RUNNING"}')) as call:
            result = H.cli(["workflow", "describe"], deadline=10.4)
        argv = call.call_args.args[0]
        self.assertEqual(argv[argv.index("--command-timeout") + 1], "0.4s")
        self.assertLessEqual(call.call_args.kwargs["timeout"], 0.401)
        self.assertEqual(result["json"], {"status": "RUNNING"})

    def test_parallel_read_gives_every_call_the_same_deadline(self):
        seen = []
        def fake_cli(args, deadline=None):
            seen.append((args, deadline))
            return {"rc": 0, "json": {}, "t_end": 10.1}
        with patch.object(H, "cli", fake_cli):
            result = H.read_parts("pre", "pre", [("a", ["describe"]), ("b", ["show"])],
                                  parallel=True, deadline=10.4)
        self.assertEqual(sorted(seen), [(["describe"], 10.4), (["show"], 10.4)])
        self.assertEqual(result["deadline_mono"], 10.4)

    def test_precheck_read_is_bounded_by_stop_margin(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            run.ids["target"]["run_id"] = "rT"
            run.ids["twin"]["run_id"] = "rW"
            with patch.object(H, "read_parts", return_value={"parts": {}}) as call:
                result = run.pre_check(100.0)
            self.assertEqual(call.call_args.kwargs["deadline"], 99.5)
            self.assertFalse(result["ok"])

    def test_ready_job_client_and_effect_check_share_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            run.rx.effects = lambda: [{"kind": "ready", "workflow_id": "expc-t1-cap-watch-target-1"}]
            with patch.object(H, "now", return_value=10.0), \
                    patch.object(H.subprocess, "run", return_value=done([], 0, '{"ok": true}')) as call:
                result = run.ready_job("target", "cap-watch-target", deadline=10.3)
            self.assertTrue(result["ok"])
            self.assertLessEqual(call.call_args.kwargs["timeout"], 0.301)
            argv = call.call_args.args[0]
            self.assertLess(float(argv[argv.index("--timeout") + 1]), 0.3)
            self.assertEqual(result["deadline_mono"], 10.3)

    def test_expired_ready_budget_is_failed_evidence_without_a_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            with patch.object(H, "now", return_value=10.0), \
                    patch.object(H.subprocess, "run", side_effect=AssertionError("no late client")):
                result = run.ready_job("target", "cap-watch-target", deadline=9.0)
            self.assertFalse(result["ok"])
            self.assertIsNone(result["rc"])
            self.assertFalse(result["effect_seen"])

    def test_queue_controls_start_concurrently_with_one_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            both_started = threading.Barrier(2)
            seen = []
            def fake_ready(role, label, deadline=None):
                seen.append((role, deadline))
                both_started.wait(timeout=1)
                return {"queue": role, "ok": role == "target"}
            with patch.object(run, "ready_job", fake_ready):
                result = run.watch_capability(["target", "twin"], 119.9)
            self.assertEqual(sorted(seen), [("target", 119.9), ("twin", 119.9)])
            self.assertEqual(result, [{"queue": "target", "ok": True}, {"queue": "twin", "ok": False}])

    def test_capability_thread_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            with patch.object(run, "ready_job", side_effect=RuntimeError("client failed")):
                result = run.watch_capability(["target", "twin"], 119.9)
            self.assertEqual(len(result), 2)
            self.assertTrue(all(row["ok"] is False and "client failed" in row["error"] for row in result))


# ============================ pre-freeze batch, item 6: the declared host profile (C.0, C.1, C.9) ====================
class HostProfiles(unittest.TestCase):
    """STOPCHECK_HOST_PROFILE=wsl (the default, the laptop) or linux-vm (a plain Linux VM), declared and never inferred,
    as n8n's harness. wsl keeps its checks; linux-vm measures the disk floor where the trial writes, refuses a WSL
    kernel, and adds an outbound check to the namespace isolation; a VM's GCE identity is copied from the runner's
    batch entry."""

    def profile(self, value):
        p = patch.object(H, "HOST_PROFILE", value)
        p.start()
        self.addCleanup(p.stop)

    def test_an_undeclared_profile_is_refused_before_anything_runs(self):
        self.profile("gce")
        with patch.object(H, "wrap_in_namespace", side_effect=AssertionError("no launch")), \
                patch.object(H.os, "geteuid", return_value=0, create=True):
            with self.assertRaisesRegex(SystemExit, "STOPCHECK_HOST_PROFILE"):
                H.main(["C2", "T1"])
        self.assertEqual(H.HOST_PROFILES, ("wsl", "linux-vm"))
        self.assertEqual(H.HOST_PROFILES, RUN.HOST_PROFILES)
        self.assertEqual(H.HOST_PROFILES, A.HOST_PROFILES)

    def test_every_record_carries_the_declared_profile_and_names_the_host(self):
        for value, words in (("wsl", "WSL2"), ("linux-vm", "Linux VM")):
            self.profile(value)
            R = H.new_record("C2", "T1", "pilot", False)
            self.assertEqual(R["host_profile"], {"declared": value, "variable": "STOPCHECK_HOST_PROFILE"})
            self.assertIn(words, R["scope"])

    def test_wsl_host_checks_are_unchanged(self):
        self.profile("wsl")
        with patch.object(H.guards, "disk_ok", return_value=(True, 50.0)), \
                patch.object(H.guards, "c_drive_free_gb", side_effect=AssertionError("no VM paths on wsl")), \
                patch.object(H, "_wsl_kernel", side_effect=AssertionError("no kernel check on wsl")):
            report = H.host_checks()
        self.assertTrue(report["ok"])
        self.assertEqual(report["disk"]["free_gb"], 50.0)
        with patch.object(H.guards, "disk_ok", return_value=(False, 5.0)):
            report = H.host_checks()
        self.assertFalse(report["ok"])
        self.assertIn("disk below the floor: 5.0 GB", report["violations"][0])

    def test_linux_vm_disk_floor_is_measured_where_the_trial_writes(self):
        self.profile("linux-vm")
        self.assertEqual(H.VM_DISK_PATHS, (os.path.join(H.HERE, "results"), "/"))
        for free, ok in (({H.VM_DISK_PATHS[0]: 50.0, "/": 40.0}, True), ({H.VM_DISK_PATHS[0]: 50.0, "/": 9.9}, False),
                         ({H.VM_DISK_PATHS[0]: None, "/": 40.0}, False)):
            with self.subTest(free=free), patch.object(H.guards, "c_drive_free_gb", side_effect=free.get), \
                    patch.object(H.guards, "disk_ok", side_effect=AssertionError("no /mnt/c on a VM")), \
                    patch.object(H, "_wsl_kernel", return_value=False):
                report = H.host_checks()
            self.assertIs(report["ok"], ok, report)
            self.assertEqual(report["disk"]["paths"], free)

    def test_linux_vm_declared_on_a_wsl_or_unreadable_kernel_fails(self):
        self.profile("linux-vm")
        for kernel in (True, None):
            with self.subTest(kernel=kernel), patch.object(H.guards, "c_drive_free_gb", return_value=50.0), \
                    patch.object(H, "_wsl_kernel", return_value=kernel):
                report = H.host_checks()
            self.assertFalse(report["ok"])
            self.assertTrue(any("kernel" in v for v in report["violations"]))

    def test_a_failing_host_check_refuses_the_trial_before_any_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            with patch.object(run, "isolation_record", return_value={"ok": True}), \
                    patch.object(run, "clean_slate", return_value={"ok": True}), \
                    patch.object(H, "host_exclusion", return_value={"ok": True}), \
                    patch.object(H.guards, "snapshot", return_value={}), \
                    patch.object(H, "host_checks", return_value={"ok": False, "violations": ["disk floor"],
                                                                 "disk": {"free_gb": 1.0}}), \
                    patch.object(H.clockguard, "check", side_effect=AssertionError("refused before the clock")), \
                    patch.object(run.procs, "start", side_effect=AssertionError("no launch")), \
                    patch.object(run, "finalize"):
                R = run.run()
        self.assertIn("host precondition failed at start", R["error"])
        self.assertEqual(R["host_checks_start"]["violations"], ["disk floor"])

    def test_linux_vm_isolation_needs_the_metadata_address_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            for value, outbound, expected in (("linux-vm", {"ok": True}, True), ("linux-vm", {"ok": False}, False),
                                              ("wsl", AssertionError("not on wsl"), True)):
                self.profile(value)
                with self.subTest(value=value, outbound=outbound), \
                        patch.dict(H.os.environ, {"STOPCHECK_C_NETNS": "expc-t1"}), \
                        patch.object(H.guards, "netns_inode", return_value=4242), \
                        patch.object(H.os, "stat", return_value=types.SimpleNamespace(st_ino=4242)), \
                        patch.object(H, "network_interfaces", return_value=["lo"]), \
                        patch.object(H, "loopback_up", return_value=True), \
                        patch.object(H, "outbound_unreachable", side_effect=[outbound] if isinstance(
                            outbound, dict) else outbound):
                    self.assertIs(run.isolation_record()["ok"], expected)

    def test_the_outbound_check_requires_network_unreachable(self):
        import errno as E

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        for result, ok in ((OSError(E.ENETUNREACH, "Network is unreachable"), True),
                           (ConnectionRefusedError(E.ECONNREFUSED, "refused"), False),
                           (TimeoutError("timed out"), False), (Conn(), False)):
            effect = {"side_effect": result} if isinstance(result, BaseException) else {"return_value": result}
            with self.subTest(result=result), patch.object(H.socket, "create_connection", **effect) as connect:
                out = H.outbound_unreachable()
            self.assertIs(out["ok"], ok, out)
            self.assertEqual(connect.call_args.args[0], ("169.254.169.254", 80))

    def test_the_record_carries_the_worker_identity_the_runner_read_for_this_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "batch.json")
            identity = {"status": "recorded", "host_profile": "linux-vm", "values": {"name": "stopcheck-1"}}
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"trials": [{"trial": "T1", "host_identity": identity}, {"trial": "T2"}]}, f)
            self.profile("linux-vm")
            copied = H.host_identity_record(path, "T1")
            self.assertEqual((copied.get("status"), copied.get("values")), ("recorded", {"name": "stopcheck-1"}))
            self.assertEqual(H.host_identity_record(path, "T2")["status"], "missing")
            self.assertEqual(H.host_identity_record(None, "T1")["status"], "missing")
            self.assertEqual(H.host_identity_record(os.path.join(tmp, "absent.json"), "T1")["status"], "missing")
            self.profile("wsl")
            self.assertEqual(H.host_identity_record(path, "T1"), {"status": "not applicable", "host_profile": "wsl"})

    def test_a_screen_trial_checks_the_freeze_against_its_declared_host_profile(self):
        self.profile("linux-vm")
        seen = {}
        fake = types.SimpleNamespace(verify_freeze=lambda manifest, conds, run_checks=True, host_profile=None:
                                     seen.update(manifest=manifest, conds=conds, run_checks=run_checks,
                                                 host_profile=host_profile) or {"ok": 1})
        self.assertEqual(H.screen_freeze(fake, "/x/FREEZE-expC.json", "C2"), {"ok": 1})
        self.assertEqual(seen, {"manifest": "/x/FREEZE-expC.json", "conds": ["C2"], "run_checks": False,
                                "host_profile": "linux-vm"})


# ============================ pre-freeze batch, item 5: the prospective wall/monotonic clock check (C.9) ==============
class TrialClocks(unittest.TestCase):
    """The across-trial clock check adds the wall/monotonic rule (temporal/clockwall_c.py, n8n/clockwall_b.py's rule) to
    the mono/raw pace check: matching CLOCK_MONOTONIC and CLOCK_MONOTONIC_RAW rates cannot exclude both pausing while
    wall time advances."""

    def rows(self, step_at=None, step_s=0.0, n=60):
        return [{"route": "effect", "kind": "tick", "mono": 100.0 + i, "raw": 50.0 + i,
                 "t": 1_760_000_000.0 + i + (step_s if step_at is not None and i >= step_at else 0.0)}
                for i in range(n)]

    def test_a_wall_step_fails_even_when_mono_and_raw_agree(self):
        good = H.check_trial_clocks(self.rows())
        self.assertTrue(good["ok"], good["reasons"])
        self.assertTrue(good["wall_clock"]["ok"])
        stepped = self.rows(step_at=30, step_s=45.0)
        self.assertTrue(H.clockguard.check_stamps([(r["mono"], r["raw"]) for r in stepped])["ok"])   # pace alone passes
        bad = H.check_trial_clocks(stepped)
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["wall_clock"]["ok"])
        self.assertTrue(any(r.startswith("wall clock: ") for r in bad["reasons"]), bad["reasons"])

    def test_rows_without_wall_stamps_fail(self):
        rows = self.rows()
        for r in rows[:3]:
            del r["t"]
        self.assertFalse(H.check_trial_clocks(rows)["ok"])

    def test_the_trial_check_reads_every_receiver_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            with open(os.path.join(tmp, "receiver.jsonl"), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(r) + "\n" for r in self.rows(step_at=30, step_s=45.0)))
            out = run.across_trial_clock()
        self.assertFalse(out["ok"])
        self.assertEqual(out["wall_clock"]["stamps"], 60)

    def test_records_are_schema_2(self):
        # schema 2 (pre-freeze batch): the wall/monotonic receipt and the host profile are mandatory evidence
        self.assertEqual(H.new_record("C2", "T1", "pilot", False)["schema_version"], 2)
        self.assertEqual(RUN.TRIAL_RECORD_SCHEMA, H.SCHEMA)          # the runner admits what the harness writes


# ============================ pre-freeze batch, item 1: the end-of-watch capability jobs (C.4 step 5, C.9) ===========
class EndOfWatchCapability(unittest.TestCase):
    """Draft 4 gave both end-of-watch capability jobs one deadline at t_stop + watch - 0.1 s (about 7.9 s full, 4.9 s
    FAST), and they timed out in 2 of the 4 attempts that reached them (RULES-expC C.13). Draft 5: each job has the bound
    of every readiness job, runs beside the last scheduled read, may end after the watch, and the probe waits for it."""

    def run_of(self, cond="C2", fast=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return H.TrialRun(cond, "T1", tmp.name, "smoke" if fast else "pilot", fast)

    def ready(self, run, role, ok=True):
        return {"label": f"cap-watch-{role}", "queue": run.ids[role]["task_queue"], "ok": ok, "t_start": H.now(),
                "t_end": H.now()}

    def test_each_job_gets_the_readiness_bound_not_a_shared_watch_deadline(self):
        run = self.run_of()
        seen = []

        def fake_ready(role, label, deadline=None):
            seen.append((role, label, deadline))
            return self.ready(run, role)
        with patch.object(run, "ready_job", fake_ready):
            out = run.watch_capability(["target", "twin"])
        self.assertEqual(sorted(seen), [("target", "cap-watch-target", None), ("twin", "cap-watch-twin", None)])
        self.assertEqual([x["ok"] for x in out], [True, True])

    def test_the_bound_is_a_readiness_jobs_and_the_join_covers_it(self):
        for p in (plan_full(), plan_fast()):
            # ready_job without a deadline: the client waits up to ready_timeout_s for the run's result, its process
            # gets 20 s more, and the `ready` effect 5 s after the client exits
            self.assertEqual(p["ready_timeout_s"], 30.0)
            self.assertGreaterEqual(p["capability_join_s"], p["ready_timeout_s"] + 20 + 5)

    def test_jobs_never_delay_the_last_read_and_may_end_after_the_watch(self):
        run = self.run_of()
        t_stop = H.now() - 500.0                   # every scheduled event is already due: no waiting
        order, release = [], threading.Event()

        def fake_read(label, offset=None, kind="scheduled", deadline=None):
            order.append(("read", offset))
            return {"label": label, "offset": offset, "t_end": H.now(), "parts": {}}

        deadlines = []

        def fake_ready(role, label, deadline=None):
            deadlines.append(deadline)
            order.append(("start", role))
            release.wait(5)
            order.append(("end", role))
            return self.ready(run, role)
        with patch.object(run, "scheduled_read", fake_read), patch.object(run, "ready_job", fake_ready):
            thread = run.watch(t_stop, H.watch_schedule(t_stop, run.plan, "C2"))
            self.assertIn(("read", 120), order)
            self.assertNotIn(("end", "target"), order)          # the +120 s read did not wait for the jobs
            self.assertNotIn(("end", "twin"), order)
            release.set()
            run.finish_capability(thread)
        self.assertEqual(deadlines, [None, None])               # no shared deadline at the end of the watch
        self.assertEqual([(c["queue"], c["ok"]) for c in run.R["capability"]],
                         [(run.ids["target"]["task_queue"], True), (run.ids["twin"]["task_queue"], True)])
        self.assertTrue(all(c["ended_after_watch_end"] for c in run.R["capability"]))
        self.assertIn("start_mono", run.R["capability_window"])
        self.assertIn("end_mono", run.R["capability_window"])

    def test_pc_runs_the_twin_job_only(self):
        run = self.run_of("PC")
        roles = []
        t_stop = H.now() - 500.0

        def fake_ready(role, label, deadline=None):
            roles.append(role)
            return self.ready(run, role)
        with patch.object(run, "scheduled_read", lambda *a, **k: {"parts": {}}), \
                patch.object(run, "ready_job", fake_ready):
            run.finish_capability(run.watch(t_stop, H.watch_schedule(t_stop, run.plan, "PC")))
        self.assertEqual(roles, ["twin"])

    def test_jobs_that_outlast_their_bound_fail_the_trial(self):
        run = self.run_of()
        run.plan["capability_join_s"] = 0.05
        hold = threading.Event()
        self.addCleanup(hold.set)
        thread = threading.Thread(target=hold.wait, args=(5,), daemon=True)
        thread.start()
        with self.assertRaisesRegex(RuntimeError, "capability"):
            run.finish_capability(thread)
        run.finish_capability(None)                # no capability event: nothing to wait for


# ============================ pre-freeze review (14 Oct): harness corrections, test-first ============================
@unittest.skipUnless(POSIX, "needs CLOCK_MONOTONIC_RAW")
class TypedCommandAndReply(unittest.TestCase):
    """Review, major 3 (C.3, C.4): every action keeps the exact command typed (the full CLI argv, with the address,
    namespace, command timeout, run ID and reason) and Temporal's reply (exit code, raw stdout, full stderr and the
    parsed JSON); so does the clean-up terminate."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_ = H.TrialRun("C5", "T1", tmp.name, "pilot", False)
        self.run_.ids["target"]["run_id"] = "rT"

    def test_the_request_keeps_the_typed_command_and_the_reply(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, '{"accepted": true}\n', "warning: x\n")
        with patch.object(H.subprocess, "run", fake_run):
            a = self.run_.request(new_box(H.now()), "cancel")
        self.assertEqual(a.get("argv_full"), seen["argv"])
        self.assertEqual(a.get("argv_full"), H.cli_argv(["workflow", "cancel", "-w", "expc-t1-target", "-r", "rT"]))
        self.assertEqual((a.get("stdout"), a.get("stderr"), a.get("json")), ('{"accepted": true}\n', "warning: x\n",
                                                                             {"accepted": True}))

    def test_a_refused_request_keeps_temporals_whole_reply(self):
        error = "Error: unable to terminate workflow: " + "x" * 900 + " workflow not found"
        box = new_box(H.now())
        with patch.object(H.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", error)):
            with self.assertRaisesRegex(RuntimeError, "failed request"):
                self.run_.request(box, "terminate")
        a = box["actions"][0]
        self.assertEqual((a["rc"], a["stderr"], a["stdout"]), (1, error, ""))
        self.assertEqual(a["argv_full"][a["argv_full"].index("--reason") + 1], "expc-T1")
        self.assertIn("--command-timeout", a["argv_full"])

    def test_the_cleanup_terminate_keeps_its_command_and_reply(self):
        self.run_.ids["twin"]["run_id"] = "rW"
        replies = {"describe": {"expc-t1-target": "RUNNING", "expc-t1-twin": "COMPLETED"}}

        def fake_run(argv, **kw):
            if argv[2] == "describe":
                status = replies["describe"][argv[argv.index("-w") + 1]]
                return subprocess.CompletedProcess(argv, 0, json.dumps(describe_json(status)), "")
            return subprocess.CompletedProcess(argv, 0, "{}\n", "")
        with patch.object(H.subprocess, "run", fake_run):
            out = self.run_.cleanup_terminate()
        self.assertEqual([x["workflow_id"] for x in out], ["expc-t1-target"])
        self.assertEqual((out[0].get("argv_full") or [])[1:3], ["workflow", "terminate"])
        self.assertEqual((out[0].get("rc"), out[0].get("stdout")), (0, "{}\n"))

    def test_every_cli_call_keeps_its_raw_output(self):
        with patch.object(H.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '{"a": 1}', "")):
            out = H.cli(["workflow", "describe"])
        self.assertEqual((out["stdout"], out["stderr"], out["json"]), ('{"a": 1}', "", {"a": 1}))
        self.assertEqual(out["argv_full"][0], H.CLI)


class RestartIdentity(unittest.TestCase):
    """Review, minor (C.1): a restarted process must have a new PID and start time; the launch record says so."""

    def start(self, new_pid, new_ticks):
        command = ["true"]
        with tempfile.TemporaryDirectory() as tmp:
            P = H.Procs(tmp, {"launches": []})
            P.p["worker-target"] = {"popen": FakePopen(alive=False, exit_status=-9), "launch": dict(LAUNCH),
                                    "log": io.BytesIO()}
            info = {"pid": new_pid, "start_ticks": new_ticks, "cmdline": command}
            try:
                with patch.object(H.subprocess, "Popen", return_value=FakePopen(pid=new_pid)), \
                        patch.object(H, "proc_info", return_value=info), patch.object(H.time, "sleep", lambda _: None):
                    return P.start("worker-target", command, "w.log")
            finally:
                P.p["worker-target"]["log"].close()

    def test_a_new_identity_is_recorded(self):
        launch = self.start(4243, 900)
        self.assertEqual(launch["replaces"], {"pid": 4242, "start_ticks": 777, "new_identity": True})

    def test_the_old_identity_fails_the_launch(self):
        with self.assertRaisesRegex(RuntimeError, "PID and start time"):
            self.start(4242, 777)


class ObserverLoad(unittest.TestCase):
    """Review, minor: the host's load average and steal ticks at each scheduled read and at the pre-check."""

    def test_host_load_reads_loadavg_and_steal(self):
        with tempfile.TemporaryDirectory() as tmp:
            la, st = os.path.join(tmp, "loadavg"), os.path.join(tmp, "stat")
            with open(la, "w", encoding="ascii") as f:
                f.write("1.50 0.75 0.25 2/300 4242\n")
            with open(st, "w", encoding="ascii") as f:
                f.write("cpu  100 0 50 800 10 0 5 7 0 0\ncpu0 1 2 3\n")
            out = H.host_load(la, st)
            self.assertEqual((out["loadavg"], out["cpu_ticks"], out["steal_ticks"]), ([1.5, 0.75, 0.25], 972, 7))
            missing = H.host_load(os.path.join(tmp, "x"), os.path.join(tmp, "y"))
            self.assertEqual((missing["loadavg"], missing["steal_ticks"]), (None, None))

    def test_scheduled_reads_and_the_pre_check_record_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            run.ids["target"]["run_id"], run.ids["twin"]["run_id"] = "rT", "rW"
            with patch.object(H, "read_parts", return_value={"parts": {}, "t_end": 1.0}), \
                    patch.object(H, "host_load", return_value={"loadavg": [1.0, 1.0, 1.0]}):
                r = run.scheduled_read("+2", 2)
                chk = run.pre_check(H.now() + 100)
        self.assertEqual(r.get("host_load"), {"loadavg": [1.0, 1.0, 1.0]})
        self.assertEqual(chk.get("host_load"), {"loadavg": [1.0, 1.0, 1.0]})


class OneTrialPerHost(unittest.TestCase):
    """Review, major 7 (C.4 step 1, C.10). A C trial never runs beside another StopCheck trial: the namespace wrapper
    holds a host flock (Experiment C's /run/lock/stopcheck-trial.lock, and n8n's launcher lock) from before the clean
    slate to after clean-up, and the trial refuses if any StopCheck process, network namespace, active systemd unit or
    fixed-port listener of Experiments A, B or C is present. Both are recorded."""

    def test_stopcheck_programs_are_recognised_from_command_lines(self):
        yes = (["/root/pilot-venv/bin/python", "-B", "/root/stopcheck/n8n/harness_b.py", "B2", "XB-1"],
               ["bash", "/root/stopcheck/n8n/harness_b_ns.sh", "B2", "XB-1"],
               ["python3", "n8n/run_expB.py", "--screen"], ["python3", "/root/stopcheck/n8n/campaign_b.py", "status"],
               ["node", "/opt/n8n-2.41.7/node_modules/n8n/bin/n8n", "worker"],
               ["/usr/lib/postgresql/16/bin/postgres", "-D", "/var/lib/stopcheck-n8n/xb-1/pg", "-p", "5433"],
               ["redis-server", "--bind", "127.0.0.1", "--port", "6380", "--save", ""],
               ["/root/pilot-venv/bin/python", "-u", "/mnt/c/x/stopcheck/langgraph/harness.py", "A1"],
               ["/usr/bin/python3", "-B", "/mnt/c/x/stopcheck-temporal/temporal/harness_c.py", "C2", "T2"],
               ["/usr/bin/python3", "-B", "temporal/run_expC.py", "--pilot"],
               ["/opt/temporal-cli-1.9.1/temporal", "server", "start-dev"])
        no = (["python3", "other.py"], ["/usr/lib/postgresql/16/bin/postgres", "-D", "/var/lib/postgresql/16/main"],
              ["redis-server", "--port", "6379"], ["vim", "notes.txt"], ["bash"], [], None,
              ["python3", "/home/u/harness.py"])
        for c in yes:
            self.assertTrue(H.stopcheck_markers(c), c)
        for c in no:
            self.assertFalse(H.stopcheck_markers(c), c)

    def fake_proc(self, tmp, processes):
        for pid, args in processes.items():
            os.makedirs(os.path.join(tmp, str(pid)))
            with open(os.path.join(tmp, str(pid), "cmdline"), "wb") as f:
                f.write(b"\0".join(a.encode() for a in args) + b"\0")
        return tmp

    def test_the_process_scan_skips_this_trial_and_its_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fake_proc(tmp, {10: ["python3", "run_expC.py", "--pilot"],      # our runner (an ancestor)
                                        11: ["python3", "harness_c.py", "C2", "T1"],    # our wrapper (an ancestor)
                                        20: ["python3", "/x/n8n/harness_b.py", "B2", "XB"],
                                        21: ["sleep", "100"]})
            found = H.stopcheck_processes(root, mine={10, 11})
        self.assertEqual([(p["pid"], p["programs"]) for p in found], [(20, ["harness_b.py"])])

    def test_other_stopcheck_namespaces_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("expc-t1", "expc-t2", "stopcheck-b-4242", "docker-x"):
                open(os.path.join(tmp, name), "w").close()
            self.assertEqual(H.other_namespaces("expc-t1", tmp), {"ok": True, "names": ["expc-t2", "stopcheck-b-4242"]})
            # no /run/netns yet: no named namespace at all; an unreadable one fails the check
            absent = H.other_namespaces("expc-t1", os.path.join(tmp, "absent"))
            self.assertEqual((absent["ok"], absent["names"]), (True, []))
            with patch.object(H.os, "listdir", side_effect=PermissionError("denied")):
                self.assertIs(H.other_namespaces("expc-t1", tmp)["ok"], False)

    def test_active_stopcheck_units_are_found(self):
        listing = ("n8n-main-xb-1.service loaded active running n8n main\n"
                   "stopcheck-cron-xb-1.service loaded inactive dead cron\n")
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, listing, "")
        out = H.stopcheck_units(pid1="systemd", run=fake_run)
        self.assertFalse(out["ok"])
        self.assertEqual(out["active"], ["n8n-main-xb-1.service"])
        self.assertIn("n8n-*", seen["argv"])
        self.assertTrue(H.stopcheck_units(pid1="systemd", run=lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, listing.splitlines()[1] + "\n", ""))["ok"])
        self.assertFalse(H.stopcheck_units(pid1="systemd", run=lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, "", "Failed to connect to bus"))["ok"])
        self.assertTrue(H.stopcheck_units(pid1="init", run=None)["ok"])         # no systemd: no unit can run

    def test_the_exclusion_needs_every_host_lock_held_by_the_wrapper(self):
        wrapper = os.getppid()
        for pids, ok in (({H.HOST_LOCK: [wrapper], H.B_LAUNCHER_LOCK: [wrapper]}, True),
                         ({H.HOST_LOCK: [wrapper], H.B_LAUNCHER_LOCK: [99999]}, False),
                         ({H.HOST_LOCK: [], H.B_LAUNCHER_LOCK: [wrapper]}, False)):
            with patch.object(H, "lock_state", lambda path, locks="/proc/locks", p=pids: {"path": path,
                                                                                            "pids": p[path]}):
                self.assertIs(H._exclusion_lock()["ok"], ok, pids)

    def test_the_lock_must_be_held_by_the_wrapper(self):
        st = os.stat(__file__)
        line = f"1: FLOCK  ADVISORY  WRITE {{pid}} {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            locks = os.path.join(tmp, "locks")
            with open(locks, "w", encoding="ascii") as f:
                f.write(line.format(pid=4242))
            self.assertEqual(H.lock_state(__file__, locks)["pids"], [4242])
            self.assertEqual(H.lock_state(os.path.join(tmp, "absent"), locks)["pids"], [])

    def exclusion(self, **change):
        parts = {"lock": {"ok": True}, "processes": [], "namespaces": {"ok": True, "names": []},
                 "units": {"ok": True}, "listeners": {"ok": True, "ports": []}}
        parts.update(change)
        with patch.object(H, "_exclusion_lock", return_value=parts["lock"]), \
                patch.object(H, "stopcheck_processes", return_value=parts["processes"]), \
                patch.object(H, "other_namespaces", return_value=parts["namespaces"]), \
                patch.object(H, "stopcheck_units", return_value=parts["units"]), \
                patch.object(H, "host_listeners", return_value=parts["listeners"]), \
                patch.object(H, "n8n_install", return_value={"installed": False}):
            return H.host_exclusion("expc-t1")

    def test_any_other_stopcheck_trial_or_service_refuses(self):
        self.assertTrue(self.exclusion()["ok"])
        for change in ({"lock": {"ok": False}}, {"processes": [{"pid": 20, "programs": ["harness_b.py"]}]},
                       {"namespaces": {"ok": True, "names": ["stopcheck-b-1"]}}, {"namespaces": {"ok": False}},
                       {"units": {"ok": False}}, {"listeners": {"ok": True, "ports": [5678]}},
                       {"listeners": {"ok": False}}):
            self.assertFalse(self.exclusion(**change)["ok"], change)

    def test_the_trial_refuses_before_any_launch_when_the_host_is_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            with patch.object(run, "isolation_record", return_value={"ok": True}), \
                    patch.object(run, "clean_slate", return_value={"ok": True}), \
                    patch.object(H, "host_exclusion", return_value={"ok": False, "processes": [{"pid": 20}]}), \
                    patch.object(H.guards, "snapshot", side_effect=AssertionError("refused before the guards")), \
                    patch.object(run.procs, "start", side_effect=AssertionError("no launch")), \
                    patch.object(run, "finalize"):
                R = run.run()
        self.assertIn("another StopCheck trial", R["error"])
        self.assertEqual(R["host_exclusion"]["processes"], [{"pid": 20}])

    @unittest.skipUnless(POSIX, "needs fcntl")
    def test_the_host_locks_are_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = (os.path.join(tmp, "lock", "c.lock"), os.path.join(tmp, "b.lock"))
            handles, record = H.acquire_host_locks(paths)
            self.assertEqual([x["path"] for x in record["locks"]], list(paths))
            with self.assertRaisesRegex(SystemExit, "another StopCheck trial"):
                H.acquire_host_locks(paths[1:])
            H.release_host_locks(handles)
            again, _ = H.acquire_host_locks(paths)
            H.release_host_locks(again)

    def test_the_wrapper_holds_the_locks_around_the_namespace(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv[:3]))
            return done(argv)
        with patch.object(H, "acquire_host_locks", lambda: calls.append(["lock"]) or (["h"], {"locks": []})), \
                patch.object(H, "release_host_locks", lambda handles: calls.append(["release", handles])), \
                patch.object(H.os.path, "exists", return_value=False), patch.object(H.subprocess, "run", fake_run), \
                patch.object(H.subprocess, "Popen", lambda *a, **k: FakeProc([0])), \
                patch.object(H, "kill_namespace_leftovers", lambda ns: []), \
                redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            H.wrap_in_namespace("C2", "T1")
        self.assertEqual(calls[0], ["lock"])
        self.assertEqual(calls[1], ["ip", "netns", "add"])
        self.assertEqual(calls[-2], ["ip", "netns", "delete"])
        self.assertEqual(calls[-1], ["release", ["h"]])


class PredictionsAtStart(unittest.TestCase):
    """Review, major 2 (C.8, C.10): every trial records the predictions file's SHA-256 before anything runs, so the
    freeze can show the predictions were fixed before a validation run's outcome existed."""

    def test_the_receipt_hashes_the_file_or_records_it_missing(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"schema_version": 1}\n')
            rec = H.predictions_receipt(path)
            with open(path, "rb") as f:
                self.assertEqual(rec["sha256"], hashlib.sha256(f.read()).hexdigest())
            self.assertIs(rec["exists"], True)
            missing = H.predictions_receipt(os.path.join(tmp, "absent.json"))
            self.assertEqual((missing["sha256"], missing["exists"]), (None, False))
        self.assertEqual(H.PREDICTIONS, os.path.join(H.ROOT, "predictions", "temporal-expC-cells.json"))


class NamespaceEvidence(unittest.TestCase):
    def test_namespace_interfaces_do_not_depend_on_sysfs(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as stream:
            stream.write("Inter-| Receive | Transmit\n face | bytes | packets\n lo: 0 0 0 0\n")
            path = stream.name
        try:
            with patch.object(H.os, "listdir", side_effect=AssertionError("must not read sysfs")):
                self.assertEqual(H.network_interfaces(path), ["lo"])
        finally:
            os.unlink(path)

    def test_missing_interface_evidence_remains_unknown(self):
        with patch("builtins.open", side_effect=FileNotFoundError()):
            self.assertIsNone(H.network_interfaces())

    @unittest.skipUnless(POSIX, "Linux ioctl interface flags")
    def test_loopback_requires_kernel_up_flag(self):
        import fcntl
        for flags, expected in [(1, True), (0, False), (8, False)]:
            with patch.object(H.socket, "socket") as factory, \
                    patch.object(fcntl, "ioctl", return_value=H.struct.pack("16sH22x", b"lo", flags)):
                factory.return_value.__enter__.return_value.fileno.return_value = 12
                self.assertIs(H.loopback_up(), expected)
        with patch.object(H.socket, "socket", side_effect=OSError("unreadable")):
            self.assertIsNone(H.loopback_up())

    def test_isolation_requires_matching_inode_and_up_loopback(self):
        # Pinned to the default (wsl) profile: the caller's STOPCHECK_HOST_PROFILE must not change this test, because
        # linux-vm adds a live outbound probe (covered with a fake probe in the next test).
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            for interfaces, up, expected in [(["lo"], True, True), (["lo"], False, False),
                                              (["lo"], None, False), (["eth0", "lo"], True, False)]:
                with patch.object(H, "HOST_PROFILE", "wsl"), \
                        patch.dict(H.os.environ, {"STOPCHECK_C_NETNS": "expc-t1"}), \
                        patch.object(H.guards, "netns_inode", return_value=4242), \
                        patch.object(H.os, "stat", return_value=types.SimpleNamespace(st_ino=4242)), \
                        patch.object(H, "network_interfaces", return_value=interfaces), \
                        patch.object(H, "loopback_up", return_value=up):
                    self.assertEqual(run.isolation_record()["ok"], expected)

    def test_linux_vm_isolation_also_requires_the_outbound_probe_to_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = H.TrialRun("C2", "T1", tmp, "pilot", False)
            for outbound_ok, expected in [(True, True), (False, False)]:
                with patch.object(H, "HOST_PROFILE", "linux-vm"), \
                        patch.dict(H.os.environ, {"STOPCHECK_C_NETNS": "expc-t1"}), \
                        patch.object(H.guards, "netns_inode", return_value=4242), \
                        patch.object(H.os, "stat", return_value=types.SimpleNamespace(st_ino=4242)), \
                        patch.object(H, "network_interfaces", return_value=["lo"]), \
                        patch.object(H, "loopback_up", return_value=True), \
                        patch.object(H, "outbound_unreachable", return_value={"ok": outbound_ok}):
                    rec = run.isolation_record()
                    self.assertEqual(rec["ok"], expected)
                    self.assertEqual(rec["outbound"], {"ok": outbound_ok})


if __name__ == "__main__":
    unittest.main()
