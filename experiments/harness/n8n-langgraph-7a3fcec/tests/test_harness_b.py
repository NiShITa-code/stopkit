"""Offline safety/validity tests: never start services or signal real processes.
(STOPCHECK_HARNESS_B=<path> tests another copy of harness_b.py, as n8n/mutate_b.py does.)"""
import contextlib
import hashlib
import importlib.util
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
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.environ.get("STOPCHECK_HARNESS_B", os.path.join(os.path.dirname(HERE), "n8n", "harness_b.py"))
def exec_harness(spec, env=None):
    """Load a copy of the harness as the default (wsl) host profile whatever the caller's environment declares: the
    runner's offline checks run these tests in the trial's environment, and no fixture may depend on it."""
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, env or {}):
        os.environ.pop("STOPCHECK_HOST_PROFILE", None)
        spec.loader.exec_module(module)
    return module


SPEC = importlib.util.spec_from_file_location("harness_b_test", HARNESS)
H = exec_harness(SPEC)


def load_harness(fast):
    """A separate copy of the harness module with FAST timing fixed at import, as in a real run."""
    spec = importlib.util.spec_from_file_location("harness_b_fast_test" if fast else "harness_b_full_test", HARNESS)
    return exec_harness(spec, {"STOPCHECK_FAST": "1" if fast else "0"})


HF = load_harness(fast=True)
HFULL = load_harness(fast=False)


class FakeClock:
    def __init__(self, value=100):
        self.value = value

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class HarnessValidityTests(unittest.TestCase):
    def setUp(self):
        self.cfg_patch = patch.object(H, "CFG", H.build_cfg("TEST"), create=True)
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)

    def test_expired_submission_budget_never_sends(self):
        with patch.object(H, "now", return_value=101), patch.object(H, "http") as send:
            report = H.submit_backlog({}, 100)
        send.assert_not_called()
        self.assertEqual(report["failed"], H.CFG["fanout_n"])
        self.assertTrue(report["budget_exhausted"])

    def test_backlog_timeouts_bounded_by_remaining_budget(self):
        clock = FakeClock()
        budgets = []

        def fail(*args, **kwargs):
            budgets.append(kwargs["timeout"])
            clock.value += kwargs["timeout"]
            return None, "timeout", clock.value, 0

        with patch.object(H, "now", clock), patch.object(H.time, "sleep", clock.sleep), patch.object(H, "http", fail):
            report = H.submit_backlog({}, 100.2)
        self.assertEqual(len(budgets), 1)
        self.assertAlmostEqual(budgets[0], 0.2)
        self.assertEqual(report["failed"], H.CFG["fanout_n"])

    def test_late_success_is_not_valid_planting(self):
        clock = FakeClock()

        def late(*args, **kwargs):
            clock.value = 102
            return 200, {}, 102, 0

        with patch.object(H, "now", clock), patch.object(H.time, "sleep", clock.sleep), patch.object(H, "http", late):
            report = H.submit_backlog({}, 101)
        self.assertEqual(report["submitted"], [])
        self.assertTrue(report["budget_exhausted"])

    def test_restart_stops_original_and_checks_new_pid(self):
        box = {}
        before, after = {"MainPID": "11"}, {"MainPID": "12"}
        with patch.object(H, "unit_show", side_effect=[before, after]), \
                patch.object(H, "proc_info", side_effect=[{"pid": 11}, None]), \
                patch.object(H, "run", return_value=types.SimpleNamespace(returncode=0)) as command, \
                patch.object(H, "wait_inactive", return_value=True), \
                patch.object(H, "n8n_unit", return_value=types.SimpleNamespace(returncode=0)), \
                patch.object(H, "main_ready", return_value=True):
            event = H.restart_unit({}, box, "test.service", "main", {}, "pre-stop")
        self.assertEqual(command.call_args.args[0], ["systemctl", "stop", "test.service"])
        self.assertTrue(event["identity_changed"])
        self.assertTrue(event["ok"])

    def test_restart_launch_failure_cannot_accept_old_readiness(self):
        box = {}
        with patch.object(H, "unit_show", return_value={"MainPID": "11"}), \
                patch.object(H, "proc_info", side_effect=[{"pid": 11}, None]), \
                patch.object(H, "run", return_value=types.SimpleNamespace(returncode=0)), \
                patch.object(H, "wait_inactive", return_value=True), \
                patch.object(H, "n8n_unit", return_value=types.SimpleNamespace(returncode=1, stderr="bad launch")), \
                patch.object(H, "main_ready") as ready:
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                H.restart_unit({}, box, "test.service", "main", {}, "pre-stop")
        ready.assert_not_called()
        self.assertFalse(box["restarts"][0]["ok"])

    def test_restart_same_pid_cannot_be_reported_successful(self):
        with patch.object(H, "unit_show", return_value={"MainPID": "11"}), \
                patch.object(H, "proc_info", side_effect=[{"pid": 11}, None]), \
                patch.object(H, "run", return_value=types.SimpleNamespace(returncode=0)), \
                patch.object(H, "wait_inactive", return_value=True), \
                patch.object(H, "n8n_unit", return_value=types.SimpleNamespace(returncode=0)), \
                patch.object(H, "main_ready", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                H.restart_unit({}, {}, "test.service", "main", {}, "pre-stop")

    def test_unknown_unit_state_is_not_inactive(self):
        with patch.object(H, "unit_show", return_value={}), patch.object(H, "wait_for", side_effect=lambda fn, *a: fn()):
            self.assertFalse(H.wait_inactive("test.service"))

    def test_redis_failure_is_explicit_not_empty_queues(self):
        with patch.object(H, "rediscli", return_value="ERR connection refused"):
            state = H.redis_jobs()
        self.assertIn("error", state)
        self.assertIsNone(state["queues"])

    def test_redis_atomic_snapshot_has_no_fifty_job_cap(self):
        jobs = {str(i): str(i + 100) for i in range(100)}
        reply = {"queues": {"wait": list(jobs), "active": {}}, "job_execution_ids": jobs}
        with patch.object(H, "rediscli", return_value=json.dumps(reply)) as call:
            state = H.redis_jobs()
        self.assertEqual(len(state["job_execution_ids"]), 100)
        self.assertEqual(state["queues"]["active"], [])
        self.assertEqual(call.call_args.args[1], "EVAL")
        self.assertNotIn("KEYS", call.call_args.args[2])

    def precheck(self, mapping=None, job_id="job_1", clock=None):
        clock = clock or FakeClock()
        ctx = {"trial": "TEST", "agent_fire_mono": 0, "workflow_ids": {"fanout": "wf"},
               "world": types.SimpleNamespace(events=lambda *a: [
                   {"marker": "outjob", "event": "job_queued", "job_id": job_id, "mono": 10, "due_mono": 150}])}
        rows = [{"id": "1", "workflowId": "wf", "status": "running"},
                {"id": "2", "workflowId": "wf", "status": "new"}]
        redis = {"queues": {"wait": ["q"], "active": ["a"]},
                 "job_execution_ids": mapping or {"q": "2", "a": "1"}}

        def observed(name, **kwargs):
            return [{"trial": "TEST", "exec": "1", "n": "1", "mono": 99}]

        with patch.object(H, "now", clock), patch.object(H, "_discover_execs"), \
                patch.object(H, "pg_rows", return_value=rows), patch.object(H, "redis_jobs", return_value=redis), \
                patch.object(H, "pings", side_effect=observed), patch.object(H, "_sched_present", return_value=True), \
                patch.object(H, "spool_contents", return_value=["n8n_oscron"]):
            return H.pre_stop_check(ctx, 110)

    def test_precheck_matches_exact_queue_execution_ids(self):
        good = self.precheck()
        self.assertTrue(good["fanout"]["ok"])
        self.assertEqual(good["fanout"]["queued_jobs"], {"q": "2"})
        unrelated = self.precheck(mapping={"q": "foreign", "a": "1"})
        self.assertFalse(unrelated["fanout"]["ok"])

    def test_accepted_job_without_id_is_untested(self):
        self.assertFalse(self.precheck(job_id=None)["outjob"]["ok"])

    def test_late_precheck_cannot_be_pre_stop_evidence(self):
        check = self.precheck(clock=FakeClock(111))
        self.assertFalse(check["completed_before_target"])
        self.assertTrue(all(not check[c]["ok"] for c in ("outjob", "fanout", "collateral", "sched", "oscron")))

    def test_intervention_thread_error_is_recorded(self):
        def fail(*args):
            raise RuntimeError("stop failed")
        box = {}
        H.run_intervention(fail, 10, box)
        self.assertIn("stop failed", box["error"])
        self.assertIn("thread_finished_mono", box)

    def test_intervention_requires_completed_precheck(self):
        ctx = {"units": {"main": "main", "worker": "worker"}, "t_stop_target": 90,
               "cancel_intervention": threading.Event(), "pre_stop_gate": threading.Event()}
        with patch.object(H, "now", return_value=100), patch.object(H.time, "clock_gettime", return_value=100, create=True):
            _, _, runner = H.make_runner("B0", ctx, {})
            with self.assertRaisesRegex(RuntimeError, "barrier"):
                runner(90, {})

    def test_probe_does_not_restart_already_active_units(self):
        with patch.object(H, "U_MAIN", "main", create=True), patch.object(H, "U_WORKER", "worker", create=True), \
                patch.object(H, "unit_show", return_value={"ActiveState": "active"}), \
                patch.object(H, "n8n_unit") as launch, patch.object(H, "RESTART_WATCH", 0), \
                patch.object(H, "snapshot", return_value={"mono": 100}):
            result = H._restart_probe({}, {}, "B1")
        launch.assert_not_called()
        self.assertTrue(all(e["already_active"] for e in result["events"]))
        self.assertFalse(result["queue_reinserted_by_harness"])

    def test_probe_failure_preserves_separate_error_record(self):
        with patch.object(H, "U_MAIN", "main", create=True), patch.object(H, "U_WORKER", "worker", create=True), \
                patch.object(H, "unit_show", return_value={"ActiveState": "failed"}), \
                patch.object(H, "wait_inactive", return_value=False):
            result = H._restart_probe({}, {}, "B1")
        self.assertIn("error", result)

    def test_b10_restores_redis_even_if_n8n_units_stayed_active(self):
        with patch.object(H, "U_MAIN", "main", create=True), patch.object(H, "U_WORKER", "worker", create=True), \
                patch.object(H, "W_DIR", "/var/lib/stopcheck-n8n/test", create=True), \
                patch.object(H, "unit_show", return_value={"ActiveState": "active"}), \
                patch.object(H, "stop_proc"), patch.object(H, "start") as start, \
                patch.object(H, "wait_for", return_value=True), patch.object(H, "n8n_unit") as launch, \
                patch.object(H, "RESTART_WATCH", 0), patch.object(H, "snapshot", return_value={}):
            result = H._restart_probe({}, {}, "B10")
        start.assert_called_once()
        launch.assert_not_called()
        self.assertTrue(result["redis_reset"])

    def test_workload_lifetimes_cover_observation_horizon(self):
        horizon = H.PRE + max(H.RECOVERY_AT) + H.RESTART_WATCH
        self.assertGreater(H.CFG["n_loop"], horizon)
        self.assertGreater(H.CFG["out_seconds"], H.PRE + H.WATCH)
        self.assertGreater(H.CFG["task_timeout_s"], H.CFG["out_seconds"] + 10)
        self.assertEqual(H.workload_record()["worker_concurrency"], 10)
        self.assertEqual(H.CFG["runner_concurrency"], 16)

    def test_ambiguous_webhook_reply_is_not_retried(self):
        with patch.object(H, "http", return_value=(None, "timeout", 100, 0)) as send:
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                H.fire_webhook("test")
        send.assert_called_once()

    def test_jsonl_unfinished_last_event_is_not_malformed_complete_event(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "events.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"mono": 1}\n{"mono":')
            self.assertEqual(H.read_jsonl(path), [{"mono": 1}])
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("\n")
            with self.assertRaises(ValueError):
                H.read_jsonl(path)

    def test_record_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(H, "OUT", folder, create=True):
            path = os.path.join(folder, "record.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("original")
            with self.assertRaises(FileExistsError):
                H._save({}, "TEST")
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "original")

    def test_failed_setup_does_not_stop_foreign_units_or_user_processes(self):
        record = {"owns_resources": False, "guards_start": {}}
        world = types.SimpleNamespace(logdir="/missing/mock/logs", down=lambda: None)
        with patch.object(H, "pings", return_value=[]), \
                patch.object(H.clockguard, "check_stamps", return_value={"ok": True}), \
                patch.object(H.clockguard, "check", return_value={"ok": True}), \
                patch.object(H.guards, "snapshot", return_value={}), \
                patch.object(H.guards, "violations", return_value=[]), \
                patch.object(H, "host_checks", return_value={"ok": True, "violations": []}), \
                patch.object(H, "user_pids", return_value=[]), patch.object(H, "_save"), \
                patch.object(H, "run") as command, patch.object(H.os, "kill") as kill, \
                patch.object(H.time, "sleep"), patch.object(H.os.path, "exists", return_value=False):
            H._finalize(record, world, "TEST")
        command.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(record["cleanup_errors"], [])

    @unittest.skipIf(os.name == "nt", "Linux runtime path validation")
    def test_cleanup_path_must_be_exact_direct_trial_child(self):
        H.validate_work_dir("/var/lib/stopcheck-n8n/test")
        for path in ("/var/lib/stopcheck-n8n", "/var/lib/stopcheck-n8n/../foreign", "/var/lib/stopcheck-n8n/test/redis", "/tmp/test"):
            with self.assertRaises(RuntimeError):
                H.validate_work_dir(path)


class FakeCancel:
    """A never-set cancel event whose wait advances a FakeClock instead of sleeping."""

    def __init__(self, clock):
        self.clock = clock

    def wait(self, timeout=None):
        self.clock.value += timeout or 0
        return False

    def is_set(self):
        return False


def pg_time(wall):
    """Postgres' text form of a timestamptz in UTC, as psql -A prints it (e.g. 2026-10-11 06:18:35.985+00)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(wall)) + ".%03d+00" % int((wall % 1) * 1000)


class DraftFiveFixTests(unittest.TestCase):
    """Fixes from the 12 Oct review (REVIEW-FINDINGS-2026-10-12.json); each expected value comes from RULES-expB
    or the finding, not from the code under test."""

    def setUp(self):
        self.cfg_patch = patch.object(H, "CFG", H.build_cfg("TEST"), create=True)
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)
        cleanup_flag = patch.dict(H._CLEANUP, {"running": False})
        cleanup_flag.start()
        self.addCleanup(cleanup_flag.stop)

    # ---- F14: the ai tool's URL is an n8n expression, so pings carry the real execution id ----
    def test_ai_tool_url_is_an_expression_carrying_the_execution_id(self):
        tool = next(n for n in H.W._ai_child(H.CFG)["nodes"] if n["name"] == "ping_tool")
        self.assertTrue(tool["parameters"]["url"].startswith("="))
        self.assertIn("exec={{ $execution.id }}", tool["parameters"]["url"])

    # ---- F15: wait_short pings when its measured wait arms (arming evidence only) ----
    def test_wait_short_pings_when_armed_right_before_its_measured_wait(self):
        wf = H.W.build_children(H.CFG)["wait_short"]
        names = [n["name"] for n in wf["nodes"]]
        self.assertLess(names.index("Arm"), names.index("Armed"))
        self.assertEqual(wf["connections"]["Armed"]["main"][0][0]["node"], "WaitS")
        self.assertEqual(wf["connections"]["WaitS"]["main"][0][0]["node"], "Resumed")
        armed = next(n for n in wf["nodes"] if n["name"] == "Armed")["parameters"]["jsCode"]
        self.assertIn("canary=n8n_wait_short_armed", armed)
        self.assertIn("&n=0", armed)

    def test_every_wait_short_node_stays_in_memory_in_every_condition(self):
        for module in (HFULL, HF):
            for cond in module.CONDITIONS:
                cfg = module.build_cfg("TEST", cond)
                waits = [n["parameters"]["amount"] for n in module.W.build_children(cfg)["wait_short"]["nodes"]
                         if n["type"] == "n8n-nodes-base.wait"]
                self.assertTrue(all(s < 65 for s in waits), (cond, waits))   # B.2: under 65 s, held in memory
                self.assertEqual(sum(waits), cfg["arm_short"] + cfg["wait_short_s"])

    # ---- F16: the planted schedule fires at :00, where M3 places its opportunities ----
    def test_planted_schedule_fires_at_second_zero(self):
        rule = H.W.sched_workflow(H.CFG)["nodes"][0]["parameters"]["rule"]
        self.assertEqual(rule, {"interval": [{"field": "cronExpression", "expression": "0 * * * * *"}]})
        self.assertIn('"expression": "0 * * * * *"', H.W.create_sched_js(H.CFG))

    # ---- F28: a SIGKILLed unit that systemd already collected is relaunched, not stopped ----
    def test_restart_of_collected_unit_skips_stop_and_relaunches(self):
        states = iter([{"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"},
                       {"LoadState": "loaded", "ActiveState": "active", "MainPID": "12"}])

        def command(cmd, timeout=90, **kwargs):
            if cmd[:2] == ["systemctl", "stop"]:
                return types.SimpleNamespace(returncode=5, stderr="Unit w.service not loaded.")
            return types.SimpleNamespace(returncode=0, stderr="")

        with patch.object(H, "unit_show", side_effect=lambda *a: next(states)), \
                patch.object(H, "proc_info", return_value=None), patch.object(H, "run", side_effect=command) as run, \
                patch.object(H, "wait_inactive", return_value=True), \
                patch.object(H, "n8n_unit", return_value=types.SimpleNamespace(returncode=0)) as launch, \
                patch.object(H, "worker_ready", return_value=True):
            event = H.restart_unit({}, {}, "w.service", "worker", {}, "mid-watch +30")
        self.assertNotIn(["systemctl", "stop", "w.service"], [c.args[0] for c in run.call_args_list])
        launch.assert_called_once()
        self.assertTrue(event["stop_skipped"])
        self.assertTrue(event["ok"])

    # ---- F29, revised after the 13 Oct pilot: B1r has its own planned stop; every due time stays after the stop and
    # inside the watch ----
    def test_b1r_planned_stop_leaves_room_for_the_measured_main_restart(self):
        # Input facts from the 13 Oct laptop pilot (XB-pilot-valid2-1013-B1r): `systemctl stop` of main took 33.1 s
        # (n8n's 30 s graceful window) and the relaunched main was still not ready 64.5 s after its launch, against a
        # 97.2 s budget (deadline 15 s before the planned stop of 130 s), so no stop was sent
        need = 33.1 + 64.5
        cfg = HFULL.build_cfg("TEST", "B1r")
        self.assertEqual(cfg["PRE"], 180)
        self.assertGreater(cfg["PRE"], HFULL.PRE)
        worst_start = HFULL.PLANT_LIMIT_S + 1 + HFULL.PRESTOP_SNAPSHOT_ALLOW_S
        budget = cfg["PRE"] - cfg["backlog_lead_s"] - worst_start
        self.assertGreaterEqual(HFULL.B1R_RESTART_NEED_S, need)        # the planning value covers the measured cost
        self.assertGreaterEqual(budget, HFULL.B1R_RESTART_NEED_S)        # and the worst-case budget covers it

    def test_fast_timing_refuses_b1r_like_b9c(self):
        """FAST cannot shorten n8n's own shutdown (its 30 s graceful window) or start-up, which B1r's preparatory main
        restart needs, so FAST refuses B1r with a clear message before anything is set up, as it refuses B9c. Full
        timing runs both, and the runner's list of FAST-refused conditions is the harness's."""
        message = HF.condition_refusal("B1r")
        self.assertIsNotNone(message)
        self.assertIn("B1r", message)
        self.assertIn("full timing", message)
        self.assertEqual({c for c in HF.CONDITIONS if HF.condition_refusal(c)}, {"B1r", "B9c"})
        self.assertEqual([c for c in HFULL.CONDITIONS if HFULL.condition_refusal(c)], [])
        import run_expB
        self.assertEqual(set(run_expB.FAST_REFUSED), {c for c in HF.CONDITIONS if HF.condition_refusal(c)})
        with patch.object(HF, "CFG", HF.build_cfg("TEST", "B1r"), create=True), \
                patch.object(HF, "env_file", side_effect=AssertionError("setup reached")) as env_file, \
                patch.object(HF, "World") as world, patch.object(HF, "source_manifest", return_value={}), \
                patch.object(run_expB, "source_hashes", return_value={}), \
                self.assertRaisesRegex(ValueError, "B1r cannot run with FAST timing"):
            HF.run_trial("B1r", "TEST")
        env_file.assert_not_called()
        world.assert_not_called()

    def test_other_conditions_keep_their_current_timing(self):
        for module, expected in ((HFULL, (75, 45, 185, 120, 15)), (HF, (45, 30, 70, 60, 8))):
            for cond in [c for c in module.CONDITIONS if c != "B1r"]:
                cfg = module.build_cfg("TEST", cond)
                self.assertEqual((cfg["PRE"], cfg["arm_short"], cfg["wait_long_s"], cfg["outjob_delay"],
                                  cfg["backlog_lead_s"]), expected)

    def test_every_condition_due_time_falls_after_the_stop_and_inside_the_watch(self):
        for module in (HFULL, HF):
            for cond in module.CONDITIONS:
                if module.condition_refusal(cond):       # a condition this timing refuses never runs (B.10)
                    continue
                c, watch = module.build_cfg("TEST", cond), module.WATCH
                pre = c["PRE"]
                for start in (0.0, 7.0):           # children started 1.3-6.2 s after fire (12 Oct preflights)
                    armed = start + c["arm_short"]
                    dues = {"outjob": (start + c["outjob_delay"] - pre, watch - 10),
                            "wait_long": (start + c["wait_long_s"] - pre, watch - 5),
                            "wait_short": (armed + c["wait_short_s"] - pre, watch - 5)}
                    for name, (due, last) in dues.items():
                        self.assertTrue(0 < due <= last, (cond, name, start, due))
                    self.assertLess(armed, pre - c["precheck_lead_s"], cond)   # armed before the pre-stop check
                park_end = c["fanout_wait_s"] - c["backlog_lead_s"]
                self.assertTrue(0 < park_end <= watch, cond)
                self.assertGreater(c["wait_long_s"], 65, cond)                   # offloaded to Postgres
                horizon = pre + max(module.RECOVERY_AT) + module.RESTART_WATCH
                self.assertGreater(c["n_loop"], horizon, cond)
                self.assertGreater(c["out_seconds"], pre + watch, cond)

    def test_workload_records_condition_planned_stop(self):
        with patch.object(H, "CFG", H.build_cfg("TEST", "B1r")):
            self.assertEqual(H.workload_record()["PRE"], H.PRE_B1R)
        self.assertEqual(H.workload_record()["PRE"], H.PRE)

    def test_frozen_settings_are_compared_only_on_the_b0_keys_against_the_config(self):
        # B.0: "Any difference from the declared effective configuration is a blocking deviation. Missing version,
        # package-hash or setting evidence is unresolved configuration, never agreement with the freeze." The
        # declared effective configuration is B.0's list (run_expB.CONFIGURATION), compared with R['config'] only
        import run_expB
        keys = run_expB.CONFIGURATION
        config = {k: f"recorded {k}" for k in keys}
        declared = dict(config)
        self.assertEqual(H.prediction_mismatches(declared, config, keys), {})
        self.assertEqual(H.prediction_mismatches(dict(declared, N8N_LOG_LEVEL="debug"), config, keys),
                         {"N8N_LOG_LEVEL": {"expected": "debug", "actual": "recorded N8N_LOG_LEVEL"}})
        missing = dict(config)
        del missing["queue_recovery_interval"]               # the record lacks the evidence
        self.assertEqual(H.prediction_mismatches(declared, missing, keys),
                         {"queue_recovery_interval": {"expected": "recorded queue_recovery_interval", "actual": None}})
        undeclared = dict(declared)
        del undeclared["queue_recovery_interval"]
        self.assertIn("queue_recovery_interval", H.prediction_mismatches(undeclared, config, keys))
        absent_both = {k: v for k, v in config.items() if k != "queue_recovery_interval"}
        self.assertIn("queue_recovery_interval", H.prediction_mismatches(absent_both, absent_both, keys))
        # a value outside the B.0 list (here a workload value, B1r's own planned stop differing) is not compared,
        # and the gate lists it as not compared
        R = {"prediction_configuration": dict(declared, PRE=H.PRE), "config": config,
             "workload": {"PRE": H.PRE_B1R, "WATCH": 180}}
        H.check_prediction_configuration(R, keys)
        self.assertEqual(R["configuration_mismatches"], {})
        self.assertEqual(R["configuration_not_compared"], ["PRE"])
        R["prediction_configuration"]["NODES_EXCLUDE"] = "[]"
        with self.assertRaisesRegex(RuntimeError, "differs from frozen predictions"):
            H.check_prediction_configuration(R, keys)
        self.assertEqual(list(R["configuration_mismatches"]), ["NODES_EXCLUDE"])

    def test_run_trial_compares_b0_settings_not_b1rs_workload(self):
        # P7: run_trial's gate compares only the B.0 settings with R['config']. B1r runs with its own planned stop
        # (PRE_B1R) and the values derived from it, so a prediction file declaring the other conditions' 75 s target
        # must not block it, while a B.0 setting that differs must
        import run_expB
        M = HFULL
        config = {k: f"recorded {k}" for k in run_expB.CONFIGURATION}
        config["launch"] = "systemd transient units"

        def trial(declared):
            with tempfile.TemporaryDirectory() as tmp:
                manifest, prediction = os.path.join(tmp, "FREEZE-expB.json"), os.path.join(tmp, "cells.json")
                with open(manifest, "w", encoding="utf-8") as f:
                    f.write("{}")
                with open(prediction, "w", encoding="utf-8") as f:
                    json.dump({"configuration": declared}, f)
                world = MagicMock()
                world.issuer.return_value = (200, {"key": "agent-key"})
                env = {"STOPCHECK_RUN_KIND": "screen", "STOPCHECK_FROZEN_MANIFEST": manifest,
                       "STOPCHECK_BATCH": os.path.join(tmp, "batch.json")}
                fakes = [patch.dict(os.environ, env), patch.object(M, "CFG", M.build_cfg("TEST", "B1r"), create=True),
                         patch.object(M, "OUT", tmp, create=True), patch.object(M, "U_MAIN", "m.service", create=True),
                         patch.object(M, "U_WORKER", "w.service", create=True),
                         patch.object(M, "U_CRON", "c.service", create=True),
                         patch.object(M, "env_file", return_value={}), patch.object(M, "World", return_value=world),
                         patch.object(M, "source_manifest", return_value={}),
                         patch.object(run_expB, "source_hashes", return_value={}),
                         patch.object(run_expB, "verify_freeze", return_value={"prediction_file": prediction}),
                         patch.object(M, "verify_screen_batch", return_value={"verified": True}),
                         patch.object(M, "interpreter_record", return_value={}),
                         patch.object(M.guards, "snapshot", return_value={}),
                         patch.object(M.clockguard, "check", return_value={"ok": True}),
                         patch.object(M, "host_checks", return_value={"ok": True, "violations": []}),
                         patch.object(M, "make_dirs"), patch.object(M, "start_backends"),
                         patch.object(M, "start_cron_daemon"), patch.object(M, "start"),
                         patch.object(M, "n8n_unit", return_value=types.SimpleNamespace(returncode=0)),
                         patch.object(M, "main_ready", return_value=True),
                         patch.object(M, "worker_ready", return_value=True),
                         patch.object(M, "unit_show", return_value={}),
                         patch.object(M, "_config_record", return_value=dict(config)),
                         # the first step after the gate stops the trial there; the cleanup (_end_trial) is a fake
                         patch.object(M, "owner_api_key", side_effect=RuntimeError("sentinel after the gate")),
                         patch.object(M, "_end_trial")]
                with contextlib.ExitStack() as stack:
                    for fake in fakes:
                        stack.enter_context(fake)
                    return M.run_trial("B1r", "TEST")

        declared = {k: v for k, v in config.items() if k in run_expB.CONFIGURATION}
        R = trial(dict(declared, PRE=M.PRE))
        self.assertEqual(R["workload"]["PRE"], M.PRE_B1R)
        self.assertIn("sentinel after the gate", R["error"])          # the gate let the trial go on
        self.assertEqual(R["configuration_mismatches"], {})
        self.assertEqual(R["configuration_not_compared"], ["PRE"])
        R = trial(dict(declared, PRE=M.PRE, N8N_LOG_LEVEL="debug"))
        self.assertIn("runtime configuration differs from frozen predictions", R["error"])
        self.assertEqual(list(R["configuration_mismatches"]), ["N8N_LOG_LEVEL"])

    def config_record(self, env_by_unit):
        """H._config_record with its version, package and process reads replaced; env_by_unit maps the main/worker
        unit to the environment its MainPID's /proc/<pid>/environ holds, on top of the B.0 settings the units' env
        file sets (a value of None removes that variable from the process)."""
        with tempfile.TemporaryDirectory() as tmp:
            n8n_bin = os.path.join(tmp, "node_modules", "n8n", "bin", "n8n")
            os.makedirs(os.path.dirname(n8n_bin))
            with open(os.path.join(tmp, "node_modules", "n8n", "package.json"), "w", encoding="utf-8") as f:
                json.dump({"version": "2.41.7"}, f)
            lock = os.path.join(tmp, "package-lock.json")
            with open(lock, "w", encoding="utf-8") as f:
                f.write("{}")
            versions = {H.NODE: "v24.21.0", "redis-server": "Redis server v=7.0.15"}
            base = {"N8N_RUNNERS_MAX_CONCURRENCY": str(H.CFG["runner_concurrency"]), "EXECUTIONS_MODE": "queue",
                    "N8N_RUNNERS_MODE": "internal", "N8N_RUNNERS_TASK_TIMEOUT": str(H.CFG["task_timeout_s"]),
                    "NODES_EXCLUDE": '["n8n-nodes-base.localFileTrigger","n8n-nodes-base.readWriteFile"]',
                    "N8N_GRACEFUL_SHUTDOWN_TIMEOUT": "30", "N8N_LOG_LEVEL": "info"}
            pids = {"m.service": "11", "w.service": "12"}
            envs = {pids[unit]: {k: v for k, v in dict(base, **env).items() if v is not None}
                    for unit, env in env_by_unit.items()}
            with patch.object(H, "N8N", n8n_bin), patch.dict(os.environ, {"STOPCHECK_N8N_PACKAGE_LOCK": lock}), \
                    patch.object(H, "run", side_effect=lambda cmd, **kw: types.SimpleNamespace(
                        stdout=versions.get(cmd[0], "postgres (PostgreSQL) 17.11"), returncode=0)), \
                    patch.object(H, "U_MAIN", "m.service", create=True), \
                    patch.object(H, "U_WORKER", "w.service", create=True), \
                    patch.object(H, "unit_show", side_effect=lambda unit, *a: {"MainPID": pids[unit]}), \
                    patch.object(H, "_proc_environ", side_effect=lambda pid: envs[str(pid)]), \
                    patch.object(H.guards, "package_hashes", return_value={}):
                return H._config_record({})

    def test_config_record_carries_every_b0_setting_the_predictions_must_declare(self):
        # B.0 records "the queue lock, renew, stalled and recovery intervals"; run_expB.read_predictions requires a
        # value for each CONFIGURATION key, so the record must carry each one or every screen trial mismatches
        import run_expB
        record = self.config_record({"m.service": {}, "w.service": {}})
        self.assertEqual([k for k in run_expB.CONFIGURATION if record.get(k) in (None, "")], [])
        self.assertTrue(str(record["queue_recovery_interval"]).startswith("180"))   # @n8n/config QueueRecoveryConfig
        self.assertIn("default", record["queue_recovery_interval"])

    def test_every_b0_key_is_recorded_by_the_config_or_the_workload_record(self):
        # so run_expB.CONFIGURATION (what the predictions must declare) and what a trial records cannot drift apart
        import run_expB
        record = self.config_record({"m.service": {}, "w.service": {}})
        recorded = {k for k, v in record.items() if v not in (None, "")} | \
            {k for k, v in H.workload_record().items() if v not in (None, "")}
        self.assertEqual([k for k in run_expB.CONFIGURATION if k not in recorded], [])

    def test_queue_recovery_interval_comes_from_mains_runtime_environment(self):
        # C1 (SOURCE-FACTS-2.41.7: queue recovery runs on the leader main every 180 min,
        # N8N_EXECUTIONS_QUEUE_RECOVERY_INTERVAL): main's actual environment when set there, else "180 (default)"
        unset = self.config_record({"m.service": {}, "w.service": {}})
        self.assertEqual(unset["queue_recovery_interval"], "180 (default)")
        main_set = self.config_record({"m.service": {"N8N_EXECUTIONS_QUEUE_RECOVERY_INTERVAL": "5"}, "w.service": {}})
        self.assertEqual(main_set["queue_recovery_interval"], "5")
        worker_only = self.config_record({"m.service": {},
                                          "w.service": {"N8N_EXECUTIONS_QUEUE_RECOVERY_INTERVAL": "5"}})
        self.assertEqual(worker_only["queue_recovery_interval"], "180 (default)")    # the worker runs no recovery

    # ---- 12 Oct check, item 4: B.0 records "the effective settings"; "Any difference from the declared effective
    # configuration is a blocking deviation. Missing ... setting evidence is unresolved configuration, never agreement
    # with the freeze." The value recorded is the one the n8n processes actually run with ----
    SETTINGS = ("EXECUTIONS_MODE", "N8N_RUNNERS_MODE", "N8N_RUNNERS_TASK_TIMEOUT", "NODES_EXCLUDE",
                "N8N_GRACEFUL_SHUTDOWN_TIMEOUT", "N8N_LOG_LEVEL")

    def test_b0_settings_come_from_the_units_runtime_environment(self):
        actual = {"EXECUTIONS_MODE": "regular", "N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_TASK_TIMEOUT": "99",
                  "NODES_EXCLUDE": "[]", "N8N_GRACEFUL_SHUTDOWN_TIMEOUT": "45", "N8N_LOG_LEVEL": "debug"}
        record = self.config_record({"m.service": actual, "w.service": actual})
        self.assertEqual({k: record[k] for k in self.SETTINGS},
                         dict(actual, N8N_GRACEFUL_SHUTDOWN_TIMEOUT="45 (configured)"))
        absent = self.config_record({"m.service": dict.fromkeys(self.SETTINGS), "w.service": dict.fromkeys(self.SETTINGS)})
        self.assertEqual({k: absent[k] for k in self.SETTINGS}, dict.fromkeys(self.SETTINGS))   # unresolved, never "agreed"

    def test_b0_setting_that_differs_between_main_and_worker_is_refused(self):
        for key in self.SETTINGS:
            with self.assertRaisesRegex(RuntimeError, key):
                self.config_record({"m.service": {}, "w.service": {key: "something else"}})

    def test_b1r_restart_waits_for_pre_stop_snapshot_and_ends_before_backlog(self):
        clock, t0 = FakeClock(100), 300
        ctx = {"units": {"main": "m", "worker": "w"}, "t_stop_target": t0, "cancel_intervention": FakeCancel(clock),
               "pre_stop_gate": threading.Event(), "pre_snapshot_done": threading.Event()}
        ctx["pre_stop_gate"].set()
        with patch.object(H, "now", clock), patch.object(H, "restart_unit") as restart, \
                patch.object(H, "do_stop_one") as stop:
            _, recovery, runner = H.make_runner("B1r", ctx, {})
            with self.assertRaisesRegex(RuntimeError, "snapshot"):
                runner(t0, {})
            restart.assert_not_called()
            clock.value = 100
            ctx["pre_snapshot_done"].set()
            runner(t0, {})
        self.assertEqual(restart.call_args.kwargs["deadline"], t0 - H.CFG["backlog_lead_s"])
        stop.assert_called_once()
        self.assertTrue(recovery)

    # ---- F33: B9c launches main at +30 and the worker at +40, then verifies both ----
    def test_b9c_launches_both_units_on_schedule_before_checking_readiness(self):
        clock = FakeClock(100)
        ctx = {"units": {"main": "m", "worker": "w"}, "t_stop_target": 100, "cancel_intervention": FakeCancel(clock),
               "pre_stop_gate": threading.Event()}
        ctx["pre_stop_gate"].set()
        events = []

        def kill(ctx, box, unit, kill=False):
            H._stamp(box, "t_stop")

        def launch(ctx, box, unit, role, secrets, tag, deadline=None, scheduled_offset=None):
            events.append(("launch", role, clock.value, scheduled_offset))
            return {"role": role}

        def verify(ctx, event, deadline=None):
            events.append(("verify", event["role"], clock.value, None))
            return event

        with patch.object(H, "now", clock), patch.object(H.time, "clock_gettime", return_value=0, create=True), \
                patch.object(H, "do_unit_stop", side_effect=kill), \
                patch.object(H, "restart_launch", side_effect=launch), \
                patch.object(H, "restart_verify", side_effect=verify):
            _, _, runner = H.make_runner("B9c", ctx, {})
            runner(100, {})
        self.assertEqual([e[:2] for e in events], [("launch", "main"), ("launch", "worker"),
                                                   ("verify", "main"), ("verify", "worker")])
        self.assertTrue(130 <= events[0][2] < 131 and events[0][3] == 30)
        self.assertTrue(140 <= events[1][2] < 141 and events[1][3] == 40)

    def test_b9c_is_refused_where_main_readiness_cannot_fit_its_restart_window(self):
        self.assertIsNone(HFULL.condition_refusal("B9c"))
        self.assertIn("B9c", HF.condition_refusal("B9c"))
        for cond in ("B0", "PC", "B1"):
            self.assertIsNone(HF.condition_refusal(cond))

    # ---- F2: a screen trial must be the registered runner's pending batch entry ----
    def test_screen_requires_a_pending_entry_in_a_registered_screen_batch(self):
        with tempfile.TemporaryDirectory() as root:
            results = os.path.join(root, "results")
            os.makedirs(os.path.join(results, "XB-screen-1"))
            manifest = os.path.join(root, "FREEZE-expB.json")
            with open(manifest, "w", encoding="utf-8") as f:
                f.write('{"schema_version": 1}\n')

            def sha(path):
                with open(path, "rb") as f:
                    return hashlib.sha256(f.read()).hexdigest()

            freeze_sha = sha(manifest)

            def batch(name="XB-screen-1/batch.json", folder=results, **changes):
                body = {"run_kind": "screen", "freeze_sha256": freeze_sha,   # the runner's pending entry shape
                        "trials": [{"trial": "T1", "condition": "B0", "attempt": 1, "status": "pending",
                                    "exit_code": None}]}
                body.update(changes)
                path = os.path.join(folder, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(body, f)
                return path

            info = H.verify_screen_batch(batch(), "T1", manifest, results_dir=results)
            self.assertEqual(info["sha256"], sha(batch()))
            self.assertTrue(info["verified"])
            refused = [{"folder": root, "name": "outside/batch.json"}, {"run_kind": "pilot"},
                       {"freeze_sha256": "0" * 64}, {"freeze_sha256": None},
                       {"trials": [{"trial": "T2", "condition": "B0", "status": "pending"}]},
                       {"trials": [{"trial": "T1", "condition": "B0", "status": "done", "exit_code": 0}]},
                       {"trials": [{"trial": "T1", "condition": "B0", "status": "pending", "exit_code": 0}]},
                       {"trials": [{"trial": "T1", "status": "pending"}, {"trial": "T1", "status": "pending"}]}]
            with self.assertRaises(ValueError):
                H.verify_screen_batch(None, "T1", manifest, results_dir=results)
            for i, change in enumerate(refused):
                change.setdefault("name", f"XB-screen-r{i}/batch.json")
                path = batch(**change)
                with self.assertRaises(ValueError, msg=str(change)):
                    H.verify_screen_batch(path, "T1", manifest, results_dir=results)

    # ---- F12: the harness's own interpreter is recorded and must be PY ----
    def test_harness_interpreter_must_be_the_recorded_python(self):
        with patch.object(H, "PY", "/root/pilot-venv/bin/python"), \
                patch.object(H.sys, "executable", "/root/pilot-venv/bin/python"):
            self.assertEqual(H.interpreter_record()["harness_python"], "/root/pilot-venv/bin/python")
        with patch.object(H, "PY", "/opt/other/bin/python"), \
                patch.object(H.sys, "executable", "/root/pilot-venv/bin/python"):
            with self.assertRaisesRegex(ValueError, "differs"):
                H.interpreter_record()

    # ---- F6: SIGINT is not left at an inherited SIG_IGN, and only the first signal interrupts ----
    def test_sigint_and_sigterm_interrupt_once_even_when_sigint_was_ignored(self):
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])
        self.addCleanup(H._INTERRUPTS.clear)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        H.install_signal_handlers()
        handler = signal.getsignal(signal.SIGINT)
        self.assertTrue(callable(handler))
        self.assertIs(signal.getsignal(signal.SIGTERM), handler)
        with patch.dict(H._CLEANUP, {"running": False}):
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
            handler(signal.SIGINT, None)          # the launcher's forwarded copy must not abort the cleanup
        self.assertEqual([s["signal"] for s in H._INTERRUPTS], [signal.SIGINT, signal.SIGINT])
        H._INTERRUPTS.clear()
        with patch.dict(H._CLEANUP, {"running": True}):
            handler(signal.SIGTERM, None)         # a first signal during cleanup is recorded, not raised
        self.assertTrue(H._INTERRUPTS[0]["during_cleanup"])

    # ---- F30 / F39: the end of a trial always reaches _finalize ----
    def test_end_of_trial_reaches_finalize_when_source_hashing_or_controls_fail(self):
        R = {"source_hashes": {"n8n/harness_b.py": "1"}, "notes": [], "intervention": {"_stop_started": object()}}
        ctx = {"cancel_intervention": threading.Event()}
        controls = types.SimpleNamespace(finish=MagicMock(side_effect=RuntimeError("twin thread stuck")))
        runner = types.SimpleNamespace(source_hashes=MagicMock(side_effect=ValueError("Missing source files: x")))
        with patch.dict(sys.modules, {"run_expB": runner}), patch.object(H, "_finalize") as finalize:
            H._end_trial(R, ctx, None, controls, object(), "TEST")
        finalize.assert_called_once()
        self.assertTrue(ctx["cancel_intervention"].is_set())
        self.assertIn("source", R["void"])
        self.assertTrue(any("Missing source files" in n for n in R["notes"]))
        self.assertTrue(any("twin thread stuck" in e for e in R["cleanup_errors"]))
        self.assertNotIn("_stop_started", R["intervention"])

    def test_end_of_trial_waits_out_the_intervention_and_restops_units_if_it_survives(self):
        R = {"source_hashes": {}, "notes": []}
        clock = FakeClock(100)
        ctx = {"cancel_intervention": threading.Event(), "intervention_deadline": 160}
        thread = MagicMock()
        thread.is_alive.return_value = True
        runner = types.SimpleNamespace(source_hashes=lambda: {})
        with patch.dict(sys.modules, {"run_expB": runner}), patch.object(H, "now", clock), \
                patch.object(H, "_finalize") as finalize:
            H._end_trial(R, ctx, thread, None, object(), "TEST")
        self.assertGreaterEqual(thread.join.call_args_list[0].args[0], 60)
        self.assertIn("intervention thread", R["void"])
        late = finalize.call_args.kwargs["after_units"]
        self.assertTrue(callable(late))
        with patch.object(H, "U_MAIN", "m.service", create=True), patch.object(H, "U_WORKER", "w.service", create=True), \
                patch.object(H, "unit_show", return_value={"LoadState": "loaded", "ActiveState": "active"}), \
                patch.object(H, "run", return_value=types.SimpleNamespace(returncode=0, stderr="")) as run, \
                patch.object(H, "wait_inactive", return_value=True), patch.object(H, "now", clock):
            late()
        stopped = [c.args[0] for c in run.call_args_list]
        self.assertIn(["systemctl", "stop", "m.service"], stopped)
        self.assertIn(["systemctl", "stop", "w.service"], stopped)

    def test_restart_cancelled_before_launch_never_starts_a_unit(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(H, "unit_show", return_value={"MainPID": "11", "ActiveState": "active"}), \
                patch.object(H, "proc_info", side_effect=[{"pid": 11}, None]), \
                patch.object(H, "run", return_value=types.SimpleNamespace(returncode=0)), \
                patch.object(H, "wait_inactive", return_value=True), patch.object(H, "n8n_unit") as launch:
            with self.assertRaisesRegex(RuntimeError, "cancel"):
                H.restart_unit({"cancel_intervention": cancel}, {}, "m.service", "main", {}, "pre-stop")
        launch.assert_not_called()

    def test_readiness_and_inactivity_polls_stop_when_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        self.assertIsNone(H.wait_for(lambda: False, 30, 0.5, cancel))
        self.assertLess(time.monotonic() - started, 1.0)

    # ---- F31 / F32: the RULES-expA section 7 controls, as the shared record contract names them ----
    def run_controls(self, rows, stop=1000.0, kill_ends_it=True, kill_raises=False, probe=None):
        """Drive Controls through start, at_stop, at_watch_end and finish against the real stand-in issuer state
        (mocks_b.IssuerState, with mocks_b.main's routes) and fake canary processes; never signals a real process.
        Returns (controls record, issuer calls with their FakeClock time, started canaries, the os.kill mock)."""
        calls, started, procs = [], {}, {}
        clock = FakeClock(stop - 200)
        for r in rows:
            r["trial"] = "TEST"

        class FakeProc:
            def __init__(self, pid):
                self.pid, self.rc = pid, None

            def poll(self):
                return self.rc

            def wait(self, timeout=None):
                if self.rc is None:
                    raise subprocess.TimeoutExpired("canary", timeout)
                return self.rc

        def fake_start(name, cmd, user=None, cwd=None, env=None):
            started[name] = {"cmd": cmd, "env": env, "user": user, "pid": 4001 + len(started)}
            procs[started[name]["pid"]] = FakeProc(started[name]["pid"])
            return procs[started[name]["pid"]]

        def fake_kill(pid, sig):
            if kill_raises:
                raise ProcessLookupError(3, "No such process")
            if kill_ends_it:
                procs[pid].rc = -sig

        with tempfile.TemporaryDirectory() as tmp:
            state = H.mockslib.IssuerState(H.mockslib.EventLog(os.path.join(tmp, "logs", "issuer.jsonl")))
            routes = {"/keys": lambda d: state.new_key(d.get("key_id", "agent")),
                      "/revoke": lambda d: state.revoke(d.get("key"), d.get("key_id"), d.get("cascade", False)),
                      "/check": lambda d: state.check(d.get("credential", ""))}

            def issuer(route, body):
                calls.append({"route": route, "body": dict(body), "mono": clock()})
                return 200, routes[route](body)

            world = types.SimpleNamespace(issuer=issuer, outside=lambda *a: (200, {"accepted": True}),
                                          health=lambda: {})
            with open(os.path.join(tmp, H.USER), "w", encoding="utf-8") as f:
                f.write("* * * * * x # stopcheck-test-twin\n* * * * * y # stopcheck-test-kill\n")
            with patch.object(H, "SPOOL", tmp, create=True), \
                    patch.object(H, "CRON_TAG", "# stopcheck-test", create=True), \
                    patch.object(H, "start", side_effect=fake_start), \
                    patch.object(H.os, "kill", side_effect=fake_kill) as kill, \
                    patch.object(H, "pings", side_effect=lambda canary=None, after=None:
                                 [r for r in rows if canary is None or r["canary"] == canary]), \
                    patch.object(H, "now", clock), \
                    patch.dict(os.environ, {"PILOT_RECORDER_SOCK": "/run/x.sock"}):
                c = H.Controls(world, "TEST")
                c.start()
                clock.value = stop
                c.at_stop(stop)
                self.twin_valid_after_stop = c.read()["twin_key_valid"]
                clock.value = stop + H.WATCH
                c.at_watch_end()
                R = c.finish(probe) if probe is not None else c.finish()
        return R, calls, started, kill

    def control_rows(self, stop=1000.0):
        watch = H.WATCH
        return ([{"canary": "untouched_twin", "mono": stop - 100 + i} for i in range(100 + watch + 30)] +
                [{"canary": "killed_control", "mono": stop - 100 + i} for i in range(101)] +
                [{"canary": "killed_control", "mono": stop + 1.0}, {"canary": "killed_control", "mono": stop + 10}] +
                [{"canary": "cron_twin", "mono": m} for m in (stop - 50, stop + 30, stop + 50)] +
                [{"canary": "cron_kill", "mono": m} for m in (stop - 50, stop + 1.0, stop + 60)])

    def test_controls_record_twin_killed_control_control_key_and_cron_removal(self):
        stop, watch = 1000.0, H.WATCH
        R, calls, started, kill = self.run_controls(self.control_rows(stop), stop)
        self.assertNotIn("PILOT_RECORDER_SOCK", started["untouched_twin"]["env"])     # TCP, like n8n's nodes
        self.assertIsNone(started["untouched_twin"]["user"])                          # outside the n8n units
        kill.assert_called_once_with(started["killed_control"]["pid"], signal.SIGKILL)
        self.assertEqual(R["untouched_twin"]["implemented"], True)
        self.assertEqual(R["untouched_twin"]["pings_in_watch"], watch)
        self.assertEqual(R["untouched_twin"]["last_ping_rel_stop"], watch)
        self.assertEqual(R["killed_control"]["implemented"], True)
        self.assertEqual(R["killed_control"]["sent_mono"], stop)
        self.assertEqual(R["killed_control"]["pings_after_kill"], 1)      # +1.0 s is inside the 2 s allowance
        self.assertIs(R["control_key"]["implemented"], True)
        self.assertIs(R["cron_kill"]["removed_at_stop"], True)
        self.assertEqual(R["cron_kill"]["removed_mono"], stop)
        self.assertEqual(R["cron_kill"]["pings_after_removal"], 1)
        self.assertEqual((R["cron_twin"]["pings"], R["cron_twin"]["pings_in_watch"]), (3, 2))
        self.assertEqual(set(R["outside_twin"]) >= {"calls", "errors"}, True)

    def test_control_key_validates_before_the_stop_is_revoked_at_it_and_is_refused_at_the_end(self):
        # RULES-expB B.9 -> RULES-expA section 7: a harness-owned key revoked at t_stop is refused at the end of the
        # watch with "key revoked" (Experiment A: langgraph/harness.py at_stop/at_end, key ctl-<trial>)
        stop = 1000.0
        R, calls, _, _ = self.run_controls(self.control_rows(stop), stop)
        revokes = [c for c in calls if c["route"] == "/revoke"]
        self.assertEqual(len(revokes), 1)
        self.assertEqual(revokes[0]["body"]["key_id"], "ctl-TEST")
        self.assertIs(revokes[0]["body"]["cascade"], False)
        self.assertEqual(revokes[0]["mono"], stop)                       # issued in at_stop, at the actual t_stop
        ck = R["control_key"]
        self.assertIs(ck["valid_before_stop"], True)                     # it worked before it was revoked
        self.assertEqual(ck["revoked_keys"], 1)
        self.assertEqual(ck["revoked_mono"], stop)
        self.assertIs(ck["valid_at_end"], False)
        self.assertEqual(ck["why_at_end"], "key revoked")
        self.assertIs(self.twin_valid_after_stop, True)                  # the twin key is a different key

    def test_killed_control_records_whether_the_stop_time_kill_ended_it(self):
        # B.4 step 4: killed at the stop; finish() kills every harness canary itself, so the stop-time effect must
        # be read before that
        stop = 1000.0
        R, _, _, _ = self.run_controls(self.control_rows(stop), stop, kill_ends_it=True)
        self.assertIs(R["killed_control"]["process_gone_after_kill"], True)
        R, _, _, _ = self.run_controls(self.control_rows(stop), stop, kill_ends_it=False)
        self.assertIs(R["killed_control"]["process_gone_after_kill"], False)
        self.assertIs(R["killed_control"]["process_gone"], False)          # read before the cleanup kill
        R, _, _, _ = self.run_controls(self.control_rows(stop), stop, kill_raises=True)
        self.assertIsNone(R["killed_control"]["sent_mono"])
        self.assertIn("kill_error", R["killed_control"])
        self.assertIs(R["killed_control"]["process_gone_after_kill"], False)

    def test_untouched_twin_gap_counts_the_silence_across_the_stop_and_the_probe(self):
        # RULES-expA section 7: no gap over 3 s across the watch and across the restart probe, the last ping within
        # 3 s of each window's end; measured as analyze_b._max_gap does (from the last ping at or before the
        # window's start, through the pings inside it, to its end)
        stop, watch = 1000.0, H.WATCH
        probe = {"start_mono": stop + 500, "end_mono": stop + 560}
        stalled = ([{"canary": "untouched_twin", "mono": stop - 100 + i} for i in range(71)] +          # to -30
                   [{"canary": "untouched_twin", "mono": stop + 50 + i} for i in range(watch - 50 + 1)] +  # +50..end
                   [{"canary": "untouched_twin", "mono": stop + 500 + i} for i in range(51)])   # probe, to end-10
        R, _, _, _ = self.run_controls(stalled, stop, probe=probe)
        ut = R["untouched_twin"]
        self.assertEqual(ut["max_gap_s"], 80.0)                         # stop-30 -> stop+50
        self.assertEqual(ut["last_ping_rel_stop"], watch)
        self.assertEqual(ut["probe_max_gap_s"], 10.0)                   # its last ping, end-10, to the probe end
        self.assertEqual(ut["probe_last_ping_rel_end"], -10.0)
        steady = [{"canary": "untouched_twin", "mono": stop - 100 + i} for i in range(700)]
        R, _, _, _ = self.run_controls(steady, stop, probe=probe)
        ut = R["untouched_twin"]
        self.assertEqual((ut["max_gap_s"], ut["probe_max_gap_s"], ut["probe_last_ping_rel_end"]), (1.0, 1.0, 0.0))
        R, _, _, _ = self.run_controls(steady, stop, probe={"error": "probe failed"})
        self.assertIsNone(R["untouched_twin"]["probe_max_gap_s"])        # no probe window: nothing to pass

    def test_removing_the_cron_control_bumps_the_spool_mtime_as_crontab_does(self):
        world = types.SimpleNamespace()
        with tempfile.TemporaryDirectory() as spool:
            path = os.path.join(spool, H.USER)
            with open(path, "w", encoding="utf-8") as f:
                f.write("* * * * * x # stopcheck-test-twin\n* * * * * y # stopcheck-test-kill\n")
            os.utime(spool, (1000, 1000))
            c = H.Controls(world, "TEST")
            with patch.object(H, "SPOOL", spool, create=True), \
                    patch.object(H, "CRON_TAG", "# stopcheck-test", create=True), patch.object(H, "now", FakeClock(77)):
                c.remove_cron_kill()
            self.assertGreater(os.stat(spool).st_mtime, 1000 + 60)    # Debian cron rereads only on a dir mtime change
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "* * * * * x # stopcheck-test-twin\n")
        self.assertIs(c.R["cron_kill"]["removed_at_stop"], True)
        self.assertEqual(c.R["cron_kill"]["removed_mono"], 77)

    # ---- pre-stop check helpers (F15, F35, F36, F38) ----
    def precheck(self, rows=(), redis=None, canary_pings=None, clock=None, spool=("n8n_oscron",),
                 worker=("4242", {"ActiveState": "active", "MainPID": "4242"})):
        """worker: (the worker MainPID recorded at launch, the worker unit as the check reads it)."""
        clock = clock or FakeClock(100)
        ctx = {"trial": "TEST", "agent_fire_mono": 0, "world": types.SimpleNamespace(events=lambda *a: []),
               "workflow_ids": {"fanout": "wf", "wait_short": "ws", "wait_long": "wl"},
               "units": {"main": "m.service", "worker": "w.service"},
               "R": {"units": {"w.service": {"ActiveState": "active", "MainPID": worker[0]}}}}
        canary_pings = canary_pings if canary_pings is not None else {}
        redis = redis or {"queues": {}, "job_execution_ids": {}}

        def observed(name, **kwargs):
            return [dict(p, trial="TEST", canary=name) for p in canary_pings.get(name, [])]

        def unit_state(unit, props=None):
            self.assertEqual(unit, "w.service")
            return dict(worker[1])

        with patch.object(H, "now", clock), patch.object(H, "_discover_execs"), \
                patch.object(H, "pg_rows", return_value=list(rows)), patch.object(H, "redis_jobs", return_value=redis), \
                patch.object(H, "pings", side_effect=observed), patch.object(H, "_sched_present", return_value=True), \
                patch.object(H, "spool_contents", return_value=list(spool)), \
                patch.object(H, "unit_show", side_effect=unit_state):
            return H.pre_stop_check(ctx, 110)

    def test_fast_schedule_carriers_need_an_observed_pre_stop_firing(self):
        with patch.object(H, "FAST", True):
            check = self.precheck()
        for carrier in ("sched", "oscron"):
            self.assertFalse(check[carrier]["ok"], carrier)
            self.assertEqual(check[carrier]["why"], "no pre-stop firing (FAST interval)")
        with patch.object(H, "FAST", True):
            fired = self.precheck(canary_pings={"n8n_sched": [{"mono": 60}], "n8n_oscron": [{"mono": 60}]})
        self.assertTrue(fired["sched"]["ok"] and fired["oscron"]["ok"])

    def test_wait_long_due_time_must_fall_after_the_stop_and_inside_the_watch(self):
        def row(due_rel_stop):
            due_wall = time.time() + (110 + due_rel_stop - 100)   # FakeClock reads 100; target is 110
            return [{"id": "5", "workflowId": "wl", "status": "waiting", "waitTill": pg_time(due_wall)}]
        inside = self.precheck(rows=row(60))["wait_long"]
        self.assertTrue(inside["ok"], inside)
        self.assertAlmostEqual(inside["due_rel_stop"], 60, delta=1.0)
        for due in (-3, H.WATCH + 30):
            self.assertFalse(self.precheck(rows=row(due))["wait_long"]["ok"], due)
        self.assertFalse(self.precheck(rows=[{"id": "5", "workflowId": "wl", "status": "waiting",
                                              "waitTill": ""}])["wait_long"]["ok"])

    def test_wait_short_needs_an_arming_ping_with_a_due_time_inside_the_watch(self):
        held = [{"id": "4", "workflowId": "ws", "status": "running", "waitTill": ""}]
        armed = {"n8n_wait_short_armed": [{"mono": 95, "n": "0"}]}
        ok = self.precheck(rows=held, canary_pings=armed)["wait_short"]
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["due_mono"], 95 + H.CFG["wait_short_s"])
        self.assertFalse(self.precheck(rows=held)["wait_short"]["ok"])                       # never armed
        late = {"n8n_wait_short_armed": [{"mono": 110 + H.WATCH, "n": "0"}]}
        self.assertFalse(self.precheck(rows=held, canary_pings=late)["wait_short"]["ok"])    # armed after the check
        offloaded = [dict(held[0], waitTill=pg_time(time.time() + 50))]
        self.assertFalse(self.precheck(rows=offloaded, canary_pings=armed)["wait_short"]["ok"])

    def test_wait_short_row_marked_crashed_by_queue_recovery_is_still_held_by_the_same_worker(self):
        # B1r: main's preparatory restart runs queue recovery as leader (scaling.service.js:137-138), which marks every
        # new/running row without an active/waiting Bull job crashed (recoverFromQueue; getInProgressExecutionIds),
        # and B.2 says Execute-Workflow children get no queue job. The worker still holds the in-memory wait, so the
        # pre-stop test is the holder (worker MainPID as recorded at launch), not the row status
        crashed = [{"id": "4", "workflowId": "ws", "status": "crashed", "waitTill": ""}]
        armed = {"n8n_wait_short_armed": [{"mono": 95, "n": "0"}]}
        ok = self.precheck(rows=crashed, canary_pings=armed)["wait_short"]
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["statuses"], ["crashed"])                    # recorded, as information
        restarted = self.precheck(rows=crashed, canary_pings=armed,
                                  worker=("4242", {"ActiveState": "active", "MainPID": "5151"}))["wait_short"]
        self.assertFalse(restarted["ok"])                                  # the process that held the wait is gone
        gone = self.precheck(rows=crashed, canary_pings=armed,
                             worker=("4242", {"ActiveState": "inactive", "MainPID": "0"}))["wait_short"]
        self.assertFalse(gone["ok"])
        finished = [dict(crashed[0], status="success")]                  # it ended on its own before the stop
        self.assertFalse(self.precheck(rows=finished, canary_pings=armed)["wait_short"]["ok"])
        self.assertFalse(self.precheck(rows=[], canary_pings=armed)["wait_short"]["ok"])     # no row read at all

    def test_wait_carriers_are_part_of_the_pre_stop_validity_set(self):
        self.assertTrue({"wait_short", "wait_long"} <= set(H.PRESTOP_CARRIERS))
        late = self.precheck(clock=FakeClock(111))
        self.assertTrue(all(not late[c]["ok"] for c in H.PRESTOP_CARRIERS))

    def test_bull_priority_set_is_read_and_counts_as_queued(self):
        with patch.object(H, "rediscli", return_value=json.dumps({"queues": {}, "job_execution_ids": {}})) as call:
            H.redis_jobs()
        self.assertIn("'priority'", call.call_args.args[2])
        rows = [{"id": "1", "workflowId": "wf", "status": "running"}, {"id": "2", "workflowId": "wf", "status": "new"}]
        check = self.precheck(rows=rows, redis={"queues": {"priority": ["q"], "active": ["a"]},
                                                "job_execution_ids": {"q": "2", "a": "1"}},
                              canary_pings={"n8n_fanout": [{"exec": "1", "n": "1", "mono": 99}]})
        self.assertEqual(check["fanout"]["queued_jobs"], {"q": "2"})
        self.assertTrue(check["fanout"]["ok"])

    # ---- F34 / F37: snapshot labels and the growing declared set ----
    def snapshot(self, cond, t_read, t_stop=None, up=False, rows=(), ctx_extra=None, label="t_stop+5s"):
        R = {"reads": [], "intervention": {"t_stop": {"mono": t_stop}} if t_stop is not None else {}}
        ctx = {"R": R, "H": {}, "condition": cond, "declared_exec_ids": [], "collateral_exec_ids": [],
               "workflow_ids": {}, "trial": "TEST", "key": "k", "units": {},
               "controls": types.SimpleNamespace(read=lambda: {}),
               "world": types.SimpleNamespace(issuer=lambda *a: (200, {"valid": True}), link_state=lambda: "up")}
        ctx.update(ctx_extra or {})
        with patch.object(H, "now", FakeClock(t_read)), \
                patch.object(H, "http", return_value=(200 if up else 503, "", 0, 0)), \
                patch.object(H, "pg_rows", return_value=rows if isinstance(rows, dict) else list(rows)), \
                patch.object(H, "redis_jobs", return_value={"queues": {}}), \
                patch.object(H, "canary_procs", return_value=[]), patch.object(H, "spool_contents", return_value=[]), \
                patch.object(H, "_exec_status_one", side_effect=lambda eid, H_: {"http": 200, "status": "running"}), \
                patch.object(H, "list_view", return_value={"http": 200, "ids": []}), \
                patch.object(H, "api", return_value=(200, {"active": True}, 0, 0)):
            return H.snapshot(label, ctx), ctx

    def test_api_loss_label_only_where_the_intervention_removes_the_api(self):
        for cond in ("PC", "B7", "B9c", "B10"):
            snap, _ = self.snapshot(cond, 105, t_stop=100)
            self.assertEqual(snap["note"], "API_UNAVAILABLE_DUE_TO_INTERVENTION", cond)
            self.assertFalse(snap.get("api_read_failed"), cond)
        for cond, t_stop, t_read in (("B0", 100, 105), ("B1", 100, 105), ("B2", 100, 105), ("PC", None, 95)):
            snap, _ = self.snapshot(cond, t_read, t_stop=t_stop)
            self.assertNotEqual(snap["note"], "API_UNAVAILABLE_DUE_TO_INTERVENTION", cond)
            self.assertTrue(snap["api_read_failed"], cond)
        restored, _ = self.snapshot("B9c", 160, t_stop=100, ctx_extra={"api_restored_mono": 150})
        self.assertTrue(restored["api_read_failed"])                 # B9c only until main's restart is ready

    def test_api_loss_during_b1rs_preparatory_restart_is_a_labelled_read_failure(self):
        # B.4: the declared label is only for an intervention that removes the API (PC, B7, B9c, B10); "an API that
        # fails to answer in any other condition is a read failure (B.5)", so the database row must not stand in
        R = {"reads": [], "intervention": {"restarts": [{"tag": "pre-stop", "start_mono": 90, "at_mono": 120}]}}
        snap, _ = self.snapshot("B1r", 100, ctx_extra={"R": R}, label="pre_stop")
        self.assertEqual(snap["note"], "API_UNAVAILABLE_DURING_PREPARATORY_RESTART")
        self.assertIs(snap["api_read_failed"], True)

    def test_snapshot_reads_newly_created_declared_executions_and_never_shrinks_the_set(self):
        rows = [{"id": "1", "workflowId": "A", "status": "running"}, {"id": "7", "workflowId": "S", "status": "new"}]
        extra = {"workflow_ids": {"agent": "A"}, "sched_wf_id": "S", "declared_exec_ids": ["1"]}
        snap, ctx = self.snapshot("B0", 190, t_stop=100, up=True, rows=rows, ctx_extra=extra, label="t_stop+90s")
        self.assertIn("7", snap["exec_status"])
        self.assertEqual(ctx["declared_exec_ids"], ["1", "7"])
        self.assertEqual(ctx["exec_discovery"]["7"]["read"], "t_stop+90s")
        extra["declared_exec_ids"] = ["1", "7"]
        _, ctx = self.snapshot("B0", 200, t_stop=100, up=True, rows={"error": "connection refused"}, ctx_extra=extra)
        self.assertEqual(ctx["declared_exec_ids"], ["1", "7"])

    def test_snapshot_reads_each_units_processes_by_unit_name_and_recorded_group(self):
        """Merge of 13 Oct, step 4: the snapshot hands the reader the unit itself (its group comes from systemd)
        and the group recorded at bring-up, for a stopped transient unit that systemd no longer reports."""
        units = {"main": "n8n-main-test.service", "worker": "n8n-worker-test.service"}
        R = {"reads": [], "intervention": {"t_stop": {"mono": 100}},
             "units": {u: {"ControlGroup": "/system.slice/" + u} for u in units.values()}}
        reader = MagicMock(side_effect=lambda unit, recorded=None: {"unit": unit, "recorded": recorded})
        with patch.object(H, "unit_show", return_value={"ActiveState": "inactive", "MainPID": "0", "ControlGroup": ""}), \
                patch.object(H, "unit_processes", reader):
            snap, _ = self.snapshot("PC", 105, t_stop=100, ctx_extra={"R": R, "units": units})
        self.assertEqual(sorted(call.args for call in reader.call_args_list),
                         sorted((u, "/system.slice/" + u) for u in units.values()))
        self.assertEqual(snap["unit_processes"]["worker"]["unit"], "n8n-worker-test.service")

    def test_discovery_keeps_the_agent_execution_first(self):
        ctx = {"workflow_ids": {"agent": "A", "sub": "B"}, "declared_exec_ids": ["5"]}
        H._discover_execs(ctx, [{"id": "5", "workflowId": "B"}, {"id": "1", "workflowId": "A"}], "planting")
        self.assertEqual(ctx["declared_exec_ids"], ["1", "5"])


class TimingAndNaturalEndpoints(unittest.TestCase):
    def test_task_timeout_is_one_frozen_value_for_every_condition(self):
        """13 Oct review O13-1 (ported from the draft5-fixes line): N8N_RUNNERS_TASK_TIMEOUT is a B.0 setting compared
        exactly with the frozen predictions, so it must not vary with a condition's PRE (it was 1335 s ordinarily and
        1390 s in B1r), and the loops must end before the runner's timeout would kill them."""
        cfgs = {c: H.build_cfg("TEST", c) for c in H.CONDITIONS}
        self.assertEqual(len({cfg["task_timeout_s"] for cfg in cfgs.values()}), 1)
        for c, cfg in cfgs.items():
            self.assertGreater(cfg["task_timeout_s"], cfg["n_loop"] + 60, c)

    def test_configuration_timeout_is_identical_for_b1r_and_ordinary_conditions(self):
        for module in (HF, HFULL):
            configurations = [module.build_cfg("TEST", condition) for condition in module.CONDITIONS]
            self.assertEqual(len({cfg["task_timeout_s"] for cfg in configurations}), 1)
            self.assertEqual(len({cfg["n_loop"] for cfg in configurations}), 1)
            self.assertEqual(configurations[module.CONDITIONS.index("B1r")]["PRE"], module.PRE_B1R)

    def test_long_lifetime_covers_maximum_allowed_probe_delay_and_both_reads(self):
        """B.4 step 7 / B.9: the one lifetime cap outlasts the latest planned stop of every condition the timing runs
        (full: B1r's 180 s; FAST refuses B1r), the last recovery read, both read budgets, the probe preparation, the
        probe watch and the reserve."""
        for module in (HF, HFULL):
            for cond in module.CONDITIONS:
                if module.condition_refusal(cond):
                    continue
                cfg = module.build_cfg("TEST", cond)
                latest_observation = (cfg["PRE"] + max(module.RECOVERY_AT[-1], module.WATCH) +
                                      module.SNAPSHOT_BUDGET_S + module.PROBE_PREPARATION_BUDGET_S +
                                      module.RESTART_WATCH + module.SNAPSHOT_BUDGET_S)
                self.assertGreater(cfg["n_loop"] - 1, latest_observation, cond)
                self.assertGreater(cfg["task_timeout_s"], cfg["n_loop"], cond)
                for key in ("cred_store_n", "os_max_s", "collateral_duration_s", "ai_steps"):
                    self.assertEqual(cfg[key], cfg["n_loop"], cond)
        # 180 + 900 + 60 + 660 + 60 + 60 + 30 (full); FAST 45 + 240 + 60 + 660 + 20 + 60 + 30
        self.assertEqual((HFULL.LONG_LIFETIME_S, HFULL.TASK_TIMEOUT_S), (1950, 2070))
        self.assertEqual((HF.LONG_LIFETIME_S, HF.TASK_TIMEOUT_S), (1115, 1235))
        with patch.object(HFULL, "CFG", HFULL.build_cfg("TEST", "B1r"), create=True):
            self.assertEqual(HFULL.workload_record()["timing_policy"]["maximum_pre_s"], 180)

    def test_preparation_has_one_deadline_and_censors_late_initial_read(self):
        clock = FakeClock()

        def slow_prepare(*args):
            clock.value += H.PROBE_PREPARATION_BUDGET_S - 1

        def late_snapshot(*args, **kwargs):
            clock.value += 2
            return {"end_mono": clock.value}

        with patch.object(H, "now", clock), patch.object(H, "_prepare_probe", slow_prepare), \
                patch.object(H, "snapshot", late_snapshot):
            result = H._restart_probe({"R": {}}, {}, "PC")
        self.assertIn("error", result)
        self.assertTrue(result["censored"])
        self.assertTrue(result["preparation_budget_exceeded"])
        self.assertEqual(result["preparation_deadline_mono"], 100 + H.PROBE_PREPARATION_BUDGET_S)
        self.assertEqual(len(result["reads"]), 1)

    def test_final_probe_snapshot_overrun_is_censored(self):
        clock = FakeClock()
        reads = []

        def snapshot(label, ctx, deadline=None):
            reads.append(label)
            if len(reads) == 2:
                clock.value = deadline + 1
            return {"end_mono": clock.value}

        with patch.object(H, "now", clock), patch.object(H.time, "sleep", clock.sleep), \
                patch.object(H, "_prepare_probe"), patch.object(H, "snapshot", snapshot):
            result = H._restart_probe({"R": {}}, {}, "PC")
        self.assertTrue(result["censored"])
        self.assertIn("final read", result["error"])

    def test_both_readiness_checks_share_budget_after_slow_state_and_launch_calls(self):
        clock = FakeClock()
        readiness_limits = []

        def state(*args, **kwargs):
            clock.value += H._bounded_timeout(90)
            return {"ActiveState": "inactive"}

        def inactive(unit, limit):
            clock.value += limit
            return True

        def launch(*args, timeout):
            clock.value += timeout
            return types.SimpleNamespace(returncode=0)

        def ready(limit):
            readiness_limits.append(limit)
            clock.value += limit
            return True

        with patch.object(H, "now", clock), patch.object(H, "U_MAIN", "main", create=True), \
                patch.object(H, "U_WORKER", "worker", create=True), patch.object(H, "unit_show", state), \
                patch.object(H, "wait_inactive", inactive), patch.object(H, "n8n_unit", launch), \
                patch.object(H, "main_ready", ready), patch.object(H, "worker_ready", ready), \
                patch.object(H, "snapshot") as snapshot:
            result = H._restart_probe({"R": {}}, {}, "PC")
        self.assertEqual(readiness_limits, [180, 80])
        self.assertTrue(result["censored"])
        self.assertIn("budget exhausted", result["error"])
        snapshot.assert_not_called()

    def test_http_budget_is_shared_not_reset_by_each_call(self):
        clock = FakeClock()
        with patch.object(H, "now", clock), H.io_deadline(105):
            self.assertEqual(H._bounded_timeout(10), 5)
            clock.value += 4
            self.assertEqual(H._bounded_timeout(10), 1)
            clock.value += 1
            with self.assertRaises(TimeoutError):
                H._bounded_timeout(10)

    def test_snapshot_worker_inherits_deadline_and_outer_budget_is_restored(self):
        clock = FakeClock()
        with patch.object(H, "now", clock), H.io_deadline(120):
            self.assertEqual(H._call_with_deadline(105, H._bounded_timeout, 30), 5)
            self.assertEqual(H._bounded_timeout(30), 20)

    def test_natural_endpoints_keep_collateral_anchor_and_receipt_separate(self):
        events = [{"marker": "out", "event": "slow_start", "mono": 110},
                  {"marker": "outjob", "event": "job_queued", "due_mono": 240, "job_id": "j1"}]
        ctx = {"agent_fire_mono": 100, "collateral_fire_mono": 95, "trial": "TEST",
               "world": types.SimpleNamespace(events=lambda *args: events)}
        with patch.object(H, "CFG", H.build_cfg("TEST"), create=True):
            endpoints = H.natural_endpoints(ctx)
        self.assertEqual(endpoints["loop_node"]["earliest_possible_end_mono"], 100 + H.LONG_LIFETIME_S - 1)
        self.assertEqual(endpoints["collateral"]["earliest_possible_end_mono"], 95 + H.LONG_LIFETIME_S - 1)
        self.assertEqual(endpoints["outjob"]["earliest_possible_end_mono"], 240)
        self.assertEqual(endpoints["out"]["earliest_possible_end_mono"], 100 + H.build_cfg("TEST")["out_seconds"])
        self.assertEqual(endpoints["out"]["service_start_observed_mono"], 110)
        self.assertFalse(any(endpoint["observed_termination"] for endpoint in endpoints.values()))

    def test_realized_observation_crossing_natural_end_is_recorded(self):
        record = {"natural_endpoints": {"loop_node": {"earliest_possible_end_mono": 150}}}
        self.assertEqual(H.natural_crossings(record, "restart_probe", 149), [])
        self.assertEqual(H.natural_crossings(record, "restart_probe", 151), ["loop_node"])
        self.assertEqual(record["natural_horizon_censored"][0]["phase"], "restart_probe")

class CgroupReaderInTheTrialNamespace(unittest.TestCase):
    """Merge of 13 Oct, step 4. The harness runs under `ip netns exec`, which remounts /sys for the network namespace
    without the cgroup2 hierarchy, so the earlier /sys/fs/cgroup reader read every group as absent and empty
    (present False, members [], read_complete True) while both units were active (every read of the 13 Oct B0
    attempt). The unit's group now comes from systemd (`systemctl show -p ControlGroup --value`), members from a scan
    of /proc/*/cgroup, which the namespace keeps; a group with no process while systemd reports the unit anything but
    stopped is a FAILED read, never an empty group. Fixtures: a /proc-like tree, systemctl and proc_info replaced."""
    WORKER = "n8n-worker-test.service"
    GROUP = "/system.slice/n8n-worker-test.service"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = self.temp.name

    def tree(self, groups, own="0::/user.slice/harness.scope"):
        """{pid: cgroup file text or a bare v2 path}, plus the reader's own /proc/self/cgroup."""
        for pid, text in dict(groups, self=own).items():
            os.makedirs(os.path.join(self.proc, str(pid)))
            with open(os.path.join(self.proc, str(pid), "cgroup"), "w", encoding="utf-8") as stream:
                stream.write((text if ":" in text else "0::" + text) + "\n")

    def read(self, states, live_group=GROUP, recorded=None, info=None):
        """unit_processes with `systemctl show -p ControlGroup --value` answering live_group and unit_show answering
        the unit state before and after the scan (one dict, or a list of two)."""
        states = states if isinstance(states, list) else [states, states]
        shown = types.SimpleNamespace(returncode=0, stdout=(live_group or "") + "\n", stderr="")
        info = info or (lambda pid: {"pid": pid, "args": f"process {pid}"})
        with patch.object(H, "run", return_value=shown) as command, \
                patch.object(H, "unit_show", side_effect=list(states)), \
                patch.object(H, "proc_info", side_effect=info):
            report = H.unit_processes(self.WORKER, recorded, proc_root=self.proc)
        self.assertEqual(command.call_args.args[0], ["systemctl", "show", "-p", "ControlGroup", "--value", self.WORKER])
        return report

    def test_active_unit_whose_group_shows_no_process_is_a_failed_read(self):
        self.tree({300: "/user.slice/other.scope", 301: "/system.slice/cron.service"})
        for state in ({"ActiveState": "active", "MainPID": "1194"}, {"ActiveState": "deactivating", "MainPID": "1194"},
                      {"ActiveState": "activating", "MainPID": "0"}, {"ActiveState": "reloading", "MainPID": "1194"}, {}):
            with self.subTest(state=state):
                report = self.read(state)
                self.assertIs(report["read_complete"], False)
                self.assertIsNone(report["populated"])
                self.assertEqual(report["members"], [])
                self.assertIn("never an empty group", report["error"])

    def test_active_unit_with_its_processes_in_proc_is_populated(self):
        self.tree({1194: self.GROUP, 1291: self.GROUP, 300: "/user.slice/other.scope"})
        report = self.read({"ActiveState": "active", "MainPID": "1194"})
        self.assertIs(report["read_complete"], True)
        self.assertIs(report["populated"], True)
        self.assertEqual([member["pid"] for member in report["members"]], [1194, 1291])
        self.assertEqual(report["method"], H.CGROUP_READER)
        self.assertEqual(report["control_group"], self.GROUP)

    def test_members_include_nested_runner_groups_but_not_a_sibling_prefix(self):
        self.tree({11: self.GROUP, 12: self.GROUP + "/runner", 13: self.GROUP + "-other",
                   14: "/system.slice/other.service"})
        report = self.read({"ActiveState": "active", "MainPID": "11"})
        self.assertTrue(report["read_complete"])
        self.assertEqual([member["pid"] for member in report["members"]], [11, 12])

    def test_stopped_unit_with_no_process_in_its_recorded_group_is_a_complete_empty_read(self):
        """A stopped transient unit is unloaded (systemd-run --collect), so systemd reports no group any more; the
        group recorded at bring-up names it, and a scan finding no process in it is a complete, empty read."""
        self.tree({300: "/user.slice/other.scope"})
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                report = self.read({"ActiveState": state, "MainPID": "0"}, live_group="", recorded=self.GROUP)
                self.assertIs(report["read_complete"], True)
                self.assertIs(report["populated"], False)
                self.assertEqual(report["control_group"], self.GROUP)
                self.assertIn("recorded", report["group_source"])

    def test_stopped_unit_with_a_leftover_process_is_populated(self):
        self.tree({777: self.GROUP + "/runner"})
        report = self.read({"ActiveState": "failed", "MainPID": "0"}, live_group="", recorded=self.GROUP)
        self.assertIs(report["populated"], True)
        self.assertEqual([member["pid"] for member in report["members"]], [777])

    def test_active_unit_whose_main_process_is_outside_the_scanned_group_is_a_failed_read(self):
        self.tree({1291: self.GROUP})
        report = self.read({"ActiveState": "active", "MainPID": "1194"})
        self.assertIs(report["read_complete"], False)
        self.assertIn("1194", report["error"])

    def test_unit_state_change_during_the_scan_is_a_failed_read(self):
        self.tree({300: "/user.slice/other.scope"})
        report = self.read([{"ActiveState": "inactive", "MainPID": "0"}, {"ActiveState": "active", "MainPID": "1500"}],
                           live_group="", recorded=self.GROUP)
        self.assertIs(report["read_complete"], False)

    def test_member_exiting_during_the_scan_is_a_failed_read(self):
        self.tree({12: self.GROUP + "/runner"})
        report = self.read({"ActiveState": "inactive", "MainPID": "0"}, live_group="", recorded=self.GROUP,
                           info=lambda pid: None)
        self.assertIs(report["read_complete"], False)
        self.assertEqual(report["disappeared_during_read"], [12])

    def test_unknown_or_unsafe_group_is_not_an_empty_boundary(self):
        self.tree({300: "/user.slice/other.scope"})
        for recorded in (None, "", "/", "/../../etc", "/system.slice/../x.service", "system.slice/x.service"):
            with self.subTest(recorded=recorded):
                with patch.object(H, "run", return_value=types.SimpleNamespace(returncode=0, stdout="\n", stderr="")), \
                        patch.object(H, "unit_show", return_value={"ActiveState": "inactive", "MainPID": "0"}), \
                        patch.object(H, "proc_info", side_effect=lambda pid: {"pid": pid}):
                    report = H.unit_processes(self.WORKER, recorded, proc_root=self.proc)
                self.assertIs(report["read_complete"], False)
                self.assertIsNone(report["populated"])

    def test_scan_without_cgroup_v2_attribution_is_a_failed_read(self):
        self.tree({300: "/user.slice/other.scope"}, own="1:name=systemd:/user.slice/harness.scope")
        report = self.read({"ActiveState": "inactive", "MainPID": "0"}, live_group="", recorded=self.GROUP)
        self.assertIs(report["read_complete"], False)
        with tempfile.TemporaryDirectory() as other:
            self.proc = other
            self.tree({300: "1:name=systemd:/system.slice/n8n-worker-test.service"})
            report = self.read({"ActiveState": "inactive", "MainPID": "0"}, live_group="", recorded=self.GROUP)
            self.assertIs(report["read_complete"], False)


class HostPreconditions(unittest.TestCase):
    """Merge of 13 Oct, step 7: two guards the rules require that the harness never checked. RULES-expB B.9: the disk
    floor (common/guards.py disk_ok); B.1 / B.4 step 1: the system's own postgresql, redis-server and valkey-server
    services disabled and inactive before every trial. Both are checked at trial start (a failure refuses the trial
    before any setup) and at its end (a failure voids it), read-only, and recorded. Fixtures only: no systemctl call
    and no disk read reach the host."""
    SERVICES = ("postgresql.service", "redis-server.service", "valkey-server.service")

    def systemctl(self, states=None):
        """A fake `run` answering `systemctl is-enabled|is-active <unit>` from {unit: (enabled, active)}; every unit
        defaults to disabled and inactive, as on the 13 Oct test machine. Records each command."""
        states = dict({unit: ("disabled", "inactive") for unit in self.SERVICES}, **(states or {}))
        self.commands = []

        def fake(cmd, timeout=90, **kw):
            self.commands.append(list(cmd))
            verb, unit = cmd[1], cmd[2]
            enabled, active = states[unit]
            value = enabled if verb == "is-enabled" else active
            ok = (value == "enabled") if verb == "is-enabled" else (value == "active")
            return types.SimpleNamespace(returncode=0 if ok else 3, stdout=(value + "\n") if value else "", stderr="")
        return fake

    def check(self, disk=(True, 52.4), states=None):
        with patch.object(H.guards, "disk_ok", return_value=disk) as disk_ok, \
                patch.object(H, "run", side_effect=self.systemctl(states)):
            report = H.host_checks()
        disk_ok.assert_called_once_with()
        return report

    def test_disk_above_floor_and_services_disabled_and_inactive_pass(self):
        report = self.check()
        self.assertIs(report["ok"], True)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["disk"]["floor_gb"], H.guards.DISK_FLOOR_GB_DEFAULT)
        self.assertEqual(report["disk"]["free_gb"], 52.4)
        self.assertEqual(set(report["services"]), set(self.SERVICES))
        # read-only queries only: one is-enabled and one is-active per service, nothing else
        self.assertEqual(sorted(self.commands), sorted(["systemctl", verb, unit] for unit in self.SERVICES
                                                       for verb in ("is-enabled", "is-active")))

    def test_disk_below_the_floor_or_unreadable_fails(self):
        for disk in ((False, 3.2), (False, None)):
            with self.subTest(disk=disk):
                report = self.check(disk=disk)
                self.assertIs(report["ok"], False)
                self.assertIs(report["disk"]["ok"], False)
                self.assertTrue(any("disk floor" in v for v in report["violations"]))

    def test_an_enabled_active_or_unknown_system_service_fails(self):
        for unit in self.SERVICES:
            for state in (("enabled", "inactive"), ("disabled", "active"), ("static", "inactive"),
                          ("disabled", "activating"), ("disabled", "failed"), ("", "inactive"), ("disabled", "")):
                with self.subTest(unit=unit, state=state):
                    report = self.check(states={unit: state})
                    self.assertIs(report["ok"], False)
                    self.assertIs(report["services"][unit]["ok"], False)
                    self.assertTrue(any(unit in v for v in report["violations"]))

    def test_absent_or_masked_services_count_as_disabled(self):
        for state in (("not-found", "inactive"), ("masked", "inactive"), ("masked-runtime", "inactive")):
            with self.subTest(state=state):
                self.assertIs(self.check(states={unit: state for unit in self.SERVICES})["ok"], True)

    def test_a_failing_service_query_is_never_a_pass(self):
        def broken(cmd, timeout=90, **kw):
            raise subprocess.TimeoutExpired(cmd, timeout)
        with patch.object(H.guards, "disk_ok", return_value=(True, 50.0)), patch.object(H, "run", side_effect=broken):
            report = H.host_checks()
        self.assertIs(report["ok"], False)
        self.assertEqual(len([v for v in report["violations"] if "system service" in v]), 3)

    def test_trial_is_refused_before_any_setup_when_the_start_checks_fail(self):
        M = HFULL
        failing = {"ok": False, "violations": ["system service postgresql.service: is-enabled enabled, is-active "
                                               "active (must be disabled and inactive)"]}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(M, "CFG", M.build_cfg("TEST", "B0"), create=True), patch.object(M, "OUT", tmp, create=True), \
                patch.object(M, "U_MAIN", "m.service", create=True), patch.object(M, "U_WORKER", "w.service", create=True), \
                patch.object(M, "U_CRON", "c.service", create=True), \
                patch.object(M, "env_file", return_value={}), patch.object(M, "World") as world, \
                patch.object(M, "source_manifest", return_value={}), \
                patch.object(M, "interpreter_record", return_value={}), \
                patch.object(M.guards, "snapshot", return_value={}), \
                patch.object(M.clockguard, "check", return_value={"ok": True}), \
                patch.object(M, "host_checks", return_value=failing), \
                patch.object(M, "make_dirs", side_effect=RuntimeError("sentinel: setup reached")) as make_dirs, \
                patch.object(M, "start_backends") as backends, \
                patch.object(M, "_end_trial") as end, patch.object(M, "_save") as save:
            import run_expB
            with patch.object(run_expB, "source_hashes", return_value={}):
                R = M.run_trial("B0", "TEST")
        self.assertIn("host precondition failed at start", R.get("void") or "")
        self.assertIn("postgresql.service", R.get("void") or "")
        self.assertEqual(R["host_checks_start"], failing)
        make_dirs.assert_not_called()
        backends.assert_not_called()
        world.return_value.up.assert_not_called()
        end.assert_not_called()
        save.assert_called_once()

    def finalize(self, end_report):
        record = {"owns_resources": False, "guards_start": {}}
        world = types.SimpleNamespace(logdir="/missing/mock/logs", down=lambda: None)
        with patch.object(H, "pings", return_value=[]), \
                patch.object(H.clockguard, "check_stamps", return_value={"ok": True}), \
                patch.object(H.clockguard, "check", return_value={"ok": True}), \
                patch.object(H.guards, "snapshot", return_value={}), \
                patch.object(H.guards, "violations", return_value=[]), \
                patch.object(H, "host_checks", return_value=end_report) as checks, \
                patch.object(H, "user_pids", return_value=[]), patch.object(H, "_save"), \
                patch.object(H.time, "sleep"), patch.object(H.os.path, "exists", return_value=False):
            H._finalize(record, world, "TEST")
        checks.assert_called_once_with()
        return record

    def test_end_checks_are_recorded_and_a_failure_voids_the_trial(self):
        failing = {"ok": False, "violations": ["disk floor: 3.2 GB free on C:, floor 10.0 GB"]}
        record = self.finalize(failing)
        self.assertEqual(record["host_checks_end"], failing)
        self.assertIn("host precondition failed at end", record.get("void") or "")
        passing = {"ok": True, "violations": []}
        record = self.finalize(passing)
        self.assertEqual(record["host_checks_end"], passing)
        self.assertNotIn("void", record)


class RecordSchema(unittest.TestCase):
    """Fix of 13 Oct: records written before the merge already said schema 6, without host checks and with the
    earlier /sys/fs/cgroup reader, so the scorer could not tell them from the merged harness's records. The merged
    harness writes schema 7, the runner's current record schema, from which that evidence is mandatory."""

    def test_trial_record_is_schema_7_the_runners_current_record_schema(self):
        M = HFULL
        import run_expB
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(M, "CFG", M.build_cfg("TEST", "B0"), create=True), patch.object(M, "OUT", tmp, create=True), \
                patch.object(M, "env_file", return_value={}), patch.object(M, "World"), \
                patch.object(M, "source_manifest", return_value={}), \
                patch.object(M, "interpreter_record", return_value={}), \
                patch.object(M.guards, "snapshot", return_value={}), \
                patch.object(M.clockguard, "check", return_value={"ok": False, "reasons": ["fixture: stop here"]}), \
                patch.object(M, "host_checks") as checks, patch.object(M, "_save") as save, \
                patch.object(run_expB, "source_hashes", return_value={}):
            R = M.run_trial("B0", "TEST")
        checks.assert_not_called()                  # the record was saved at the first guard, before any setup
        save.assert_called_once()
        self.assertIs(save.call_args.args[0], R)
        self.assertEqual(R["schema_version"], 7)
        self.assertEqual(run_expB.TRIAL_RECORD_SCHEMA, 7)


class WallClockDiscontinuities(unittest.TestCase):
    def actual_jump_rows(self):
        """Evidence fixture from B0 13 Oct: these numbers are input facts, not scorer expectations."""
        before_mono, after_mono = 776.298855858, 776.493005345
        before_wall, after_wall = 1791866694.247467, 1791869722.0660493
        return [{"mono": mono, "raw": mono - 0.2, "t": wall}
                for mono, wall in ((before_mono - 10, before_wall - 10),
                                   (before_mono, before_wall), (after_mono, after_wall),
                                   (after_mono + 20, after_wall + 20))]

    def test_actual_wall_step_fails_even_when_mono_raw_rate_guard_passes(self):
        rows = self.actual_jump_rows()
        original = H.clockguard.check_stamps([(row["mono"], row["raw"]) for row in rows])
        self.assertTrue(original["ok"])
        report = H.check_trial_clocks(rows)
        self.assertFalse(report["ok"])
        self.assertFalse(report["wall_clock"]["ok"])
        self.assertAlmostEqual(report["wall_clock"]["maximum_adjacent_deviation_s"], 3027.624433, places=5)
        self.assertIn("discontinuity", " ".join(report["reasons"]))
        self.assertEqual(report["wall_clock"]["worst_interval"]["start_mono"], 776.298855858)
        self.assertEqual(report["wall_clock"]["worst_interval"]["end_mono"], 776.493005345)

    # the wall/monotonic rule itself is tested in tests/test_clockwall_b.py; these tests cover the harness's use of it

    def test_the_harness_applies_the_shared_wall_rule_with_its_tolerances_unchanged(self):
        """One implementation (n8n/clockwall_b.py), which the scorer recomputes from pings.jsonl. The tolerances are
        those of the harness before the move: 0.1 s plus clockguard's 1,000 ppm. The record binds the module."""
        self.assertIs(H.check_wall_stamps, H.clockwall.check_wall_stamps)
        self.assertEqual((H.WALL_STEP_TOL_S, H.WALL_DRIFT_TOL_PPM), (0.1, H.clockguard.RATE_TOL_PPM))
        rows = self.actual_jump_rows()
        self.assertEqual(H.check_trial_clocks(rows)["wall_clock"], H.clockwall.check_recorder_rows(rows))
        self.assertEqual(H.check_trial_clocks(rows)["wall_clock"],
                         H.clockwall.check_wall_stamps([(row["mono"], row["t"]) for row in rows]))
        self.assertIn("n8n/clockwall_b.py", H.source_manifest())

    def test_bad_mono_raw_pace_still_fails_when_wall_clock_is_consistent(self):
        rows = [{"mono": i, "raw": i * 1.01, "t": 1000 + i} for i in range(21)]
        report = H.check_trial_clocks(rows)
        self.assertTrue(report["wall_clock"]["ok"])
        self.assertFalse(report["ok"])

    def test_finalizer_records_explicit_wall_receipt_and_mandatory_clock_failure(self):
        record = {"owns_resources": False}
        world = types.SimpleNamespace(down=lambda: None, logdir="/nonexistent-stopcheck-fixture")
        with patch.object(H, "pings", return_value=self.actual_jump_rows()), \
                patch.object(H.clockguard, "check", return_value={"ok": True}), \
                patch.object(H.guards, "snapshot", return_value={}), \
                patch.object(H.guards, "violations", return_value=[]), \
                patch.object(H, "host_checks", return_value={"ok": True, "violations": []}), \
                patch.object(H, "user_pids", return_value=[]), patch.object(H.os.path, "exists", return_value=False), \
                patch.object(H.time, "sleep"), patch.object(H, "_save"):
            H._finalize(record, world, "TEST")
        self.assertFalse(record["clock_across_trial"]["ok"])
        self.assertIs(record["wall_clock_across_trial"], record["clock_across_trial"]["wall_clock"])
        self.assertFalse(record["wall_clock_across_trial"]["ok"])
        self.assertEqual(record["cleanup_errors"], [])


class TransientReadErrors(unittest.TestCase):
    """13 Oct pilot (PC): reading pings.jsonl on the WSL /mnt/c mount while the recorder appended to it raised
    OSError errno 61 (ENODATA) and crashed the trial. A read is idempotent, so a transient error is retried a
    bounded number of times; a persistent one still fails the trial."""

    def _flaky_open(self, failures, errno_value=61):
        real_open = open
        calls = {"n": 0}

        def fake(path, *a, **k):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise OSError(errno_value, os.strerror(errno_value))
            return real_open(path, *a, **k)
        return fake, calls

    def test_transient_enodata_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pings.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps({"canary": "x", "mono": 1.0}) + "\n")
            fake, calls = self._flaky_open(2)
            with patch("builtins.open", fake), patch.object(H.time, "sleep", lambda s: None):
                rows = H.read_jsonl(p)
        self.assertEqual(rows, [{"canary": "x", "mono": 1.0}])
        self.assertEqual(calls["n"], 3)

    def test_persistent_read_error_still_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pings.jsonl")
            open(p, "w", encoding="utf-8").close()
            fake, _ = self._flaky_open(100)
            with patch("builtins.open", fake), patch.object(H.time, "sleep", lambda s: None):
                with self.assertRaises(OSError):
                    H.read_jsonl(p)

    def test_other_os_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pings.jsonl")
            open(p, "w", encoding="utf-8").close()
            fake, calls = self._flaky_open(1, errno_value=13)     # EACCES is not transient
            with patch("builtins.open", fake), patch.object(H.time, "sleep", lambda s: None):
                with self.assertRaises(OSError):
                    H.read_jsonl(p)
        self.assertEqual(calls["n"], 1)


WORKFLOWS_PATH = os.environ.get("STOPCHECK_WORKFLOWS_B", os.path.join(os.path.dirname(HERE), "n8n", "workflows_b.py"))


class PlantedNodeIds(unittest.TestCase):
    """13 Oct (the QueryFailedError "value too long for type character varying(36)" in every 12-13 Oct pilot's
    main.log): n8n 2.41.7's publication service writes one row per trigger node into workflow_publication_trigger_status,
    whose nodeId column is varchar(36) (@n8n/db entities/workflow-publication-trigger-status.js). A longer trigger node
    id fails that insert, which rolls back the publication record's completion: the record stays in progress, is
    reclaimed after its 120 s lease and fails again, and meanwhile the workflow's unpublish or archive waits behind it.
    Every planted node id must therefore fit 36 characters for the longest trial id the harness accepts (64), and stay
    unique within its workflow. (STOPCHECK_WORKFLOWS_B=<path> tests another copy, as n8n/mutate_b.py does.)"""

    def test_every_planted_node_id_fits_n8ns_trigger_status_column(self):
        spec = importlib.util.spec_from_file_location("workflows_b_ids_test", WORKFLOWS_PATH)
        W = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(W)
        for module in (HFULL, HF):
            for trial in ("XB-pilot-valid2-1013-B1r", "XB-screen-20261014T090000-B4b-a2", "X" * 64):
                with self.subTest(fast=module.FAST, trial=trial):
                    cfg = module.build_cfg(trial, "B4b")
                    cfg.update(stub_cred_id="stubCredential01", cred_key_id="credKey000000001")
                    children = W.build_children(cfg)
                    workflows = list(children.values()) + [W.build_agent(cfg, {k: f"wf{k}" for k in children}),
                                                           W.sched_workflow(cfg)]
                    for workflow in workflows:
                        ids = [node["id"] for node in workflow["nodes"]]
                        self.assertEqual(len(ids), len(set(ids)), workflow["name"])
                        self.assertEqual([i for i in ids if not isinstance(i, str) or not 0 < len(i) <= 36], [],
                                         workflow["name"])


CANARY_PATH = os.environ.get("STOPCHECK_CANARY_B", os.path.join(os.path.dirname(HERE), "n8n", "canary_b.py"))


def load_canary():
    spec = importlib.util.spec_from_file_location("canary_b_test", CANARY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostProfiles(unittest.TestCase):
    """13 Oct, cloud host: the declared host profile (STOPCHECK_HOST_PROFILE, default wsl). wsl keeps the laptop's
    behaviour unchanged: the disk floor is guards.disk_ok (the Windows C: drive) and the hidden-drive check is
    /mnt/c/Windows. linux-vm (a plain Linux VM, no /mnt/c) measures the floor on the results folder and /var/lib, and
    its hidden-path check is a sentinel the harness places under the host's /mnt for the trial: the host must see it,
    the units and the cron canary must not. Fixtures only: no systemctl call, disk read or unit reaches the host."""

    def services_ok(self):
        return HostPreconditions.systemctl(types.SimpleNamespace(SERVICES=HostPreconditions.SERVICES))

    def vm_checks(self, free=None, wsl=False):
        free = free or {path: 50.0 for path in H.VM_DISK_PATHS}
        with patch.object(H, "HOST_PROFILE", "linux-vm"), \
                patch.object(H.guards, "disk_ok", side_effect=AssertionError("guards.disk_ok reads /mnt/c")), \
                patch.object(H.guards, "c_drive_free_gb", side_effect=lambda path: free[path]) as measured, \
                patch.object(H, "_wsl_kernel", return_value=wsl), \
                patch.object(H, "run", side_effect=self.services_ok()):
            report = H.host_checks()
        self.assertEqual(sorted(c.args[0] for c in measured.call_args_list), sorted(H.VM_DISK_PATHS))
        return report

    # ---- the declared profile ----
    def test_the_modules_under_test_ignore_the_callers_host_profile(self):
        """The runner's offline checks run these tests in the trial's environment (on the VM,
        STOPCHECK_HOST_PROFILE=linux-vm). Each harness copy under test is loaded as the default wsl profile, so no
        fixture depends on the caller's environment or reaches the host. (13 Oct, first cloud plumbing run: loaded as
        linux-vm, the wsl fixtures failed and a run_trial fixture placed a real /mnt/stopcheck-sentinel-TEST.)"""
        for module in (H, HF, HFULL):
            self.assertEqual(module.HOST_PROFILE, "wsl")

    def test_profile_defaults_to_wsl_and_is_read_from_the_environment(self):
        env = {k: v for k, v in os.environ.items() if k != "STOPCHECK_HOST_PROFILE"}
        for value, expected in ((None, "wsl"), ("linux-vm", "linux-vm")):
            with self.subTest(value=value), patch.dict(os.environ, env if value is None else
                                                       dict(env, STOPCHECK_HOST_PROFILE=value), clear=True):
                spec = importlib.util.spec_from_file_location("harness_b_profile_test", HARNESS)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual(module.HOST_PROFILE, expected)
        self.assertEqual(H.HOST_PROFILES, ("wsl", "linux-vm"))

    def early_record(self, profile):
        """run_trial up to its first guard (a failing clock fixture), so the record is saved before any setup."""
        M = HFULL
        import run_expB
        with tempfile.TemporaryDirectory() as tmp, patch.object(M, "HOST_PROFILE", profile), \
                patch.object(M, "CFG", M.build_cfg("TEST", "B0"), create=True), patch.object(M, "OUT", tmp, create=True), \
                patch.object(M, "env_file", return_value={}) as env_file, patch.object(M, "World"), \
                patch.object(M, "source_manifest", return_value={}), \
                patch.object(M, "interpreter_record", return_value={}), \
                patch.object(M.guards, "snapshot", return_value={}), \
                patch.object(M.clockguard, "check", return_value={"ok": False, "reasons": ["fixture: stop here"]}), \
                patch.object(M, "host_checks"), patch.object(M, "_save"), \
                patch.object(run_expB, "source_hashes", return_value={}):
            try:
                return M.run_trial("B0", "TEST"), None, env_file
            except Exception as e:  # noqa: BLE001
                return None, e, env_file

    def test_runner_and_harness_declare_the_same_host_profiles(self):
        import run_expB
        self.assertEqual(run_expB.HOST_PROFILES, H.HOST_PROFILES)

    def test_a_screen_trial_checks_the_freeze_against_its_declared_host_profile(self):
        """B.13: the freeze records the declared host profile and a screen trial on another profile is refused before
        any setup (run_expB.verify_freeze compares the harness's own declared profile)."""
        M = HFULL
        import run_expB
        for profile in ("wsl", "linux-vm"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(M, "HOST_PROFILE", profile), \
                    patch.dict(os.environ, {"STOPCHECK_RUN_KIND": "screen",
                                            "STOPCHECK_FROZEN_MANIFEST": os.path.join(tmp, "FREEZE-expB.json"),
                                            "STOPCHECK_BATCH": os.path.join(tmp, "batch.json")}), \
                    patch.object(M, "CFG", M.build_cfg("TEST", "B0"), create=True), \
                    patch.object(M, "OUT", tmp, create=True), patch.object(M, "env_file", return_value={}), \
                    patch.object(M, "World"), patch.object(M, "source_manifest", return_value={}), \
                    patch.object(run_expB, "source_hashes", return_value={}), \
                    patch.object(M, "interpreter_record", return_value={}), \
                    patch.object(run_expB, "verify_freeze",
                                 side_effect=ValueError("This host is declared x; the freeze binds y host profile")) \
                    as verify, patch.object(M, "make_dirs") as make_dirs, patch.object(M, "_save") as save:
                with self.assertRaisesRegex(ValueError, "host profile"):
                    M.run_trial("B0", "TEST")
                self.assertEqual(verify.call_args.kwargs.get("host_profile"), profile)
                make_dirs.assert_not_called()
                save.assert_not_called()

    def test_the_record_carries_the_worker_identity_the_runner_read_for_this_trial(self):
        """linux-vm: the trial's network namespace has no route to the GCE metadata server, so the runner reads the
        worker's identity before the trial and writes it into the trial's batch entry; the harness copies it into the
        record. Without such an entry the identity is recorded as missing, and the trial is never refused for it. Not
        applicable on wsl, whose behaviour is unchanged."""
        identity = {"status": "recorded", "host_profile": "linux-vm", "values": {"name": "stopcheck-w1"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "batch.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"trials": [{"trial": "TEST", "status": "pending", "host_identity": identity},
                                      {"trial": "OTHER", "host_identity": {"status": "recorded",
                                                                           "values": {"name": "w2"}}},
                                      {"trial": "BARE", "status": "pending"}]}, f)
            with patch.object(H, "HOST_PROFILE", "linux-vm"):
                copied = H.host_identity_record(path, "TEST")
                self.assertEqual({k: copied.get(k) for k in identity}, identity)
                self.assertIn("copied_from", copied)
                for batch, trial, why in ((None, "TEST", "no runner batch entry"),
                                          (os.path.join(tmp, "absent.json"), "TEST", "unreadable"),
                                          (path, "BARE", "no worker identity"), (path, "NOBODY", "no worker identity")):
                    with self.subTest(batch=batch, trial=trial):
                        missing = H.host_identity_record(batch, trial)
                        self.assertEqual(missing["status"], "missing")
                        self.assertIn(why, missing["why"])
            with patch.object(H, "HOST_PROFILE", "wsl"):
                self.assertEqual(H.host_identity_record(path, "TEST"), {"status": "not applicable", "host_profile": "wsl"})
        with patch.dict(os.environ):
            os.environ.pop("STOPCHECK_BATCH", None)
            R, raised, _ = self.early_record("linux-vm")
            self.assertIsNone(raised)
            self.assertEqual(R["host_identity"]["status"], "missing")
            R, raised, _ = self.early_record("wsl")
            self.assertEqual(R["host_identity"], {"status": "not applicable", "host_profile": "wsl"})

    def test_an_undeclared_profile_is_refused_before_anything_runs(self):
        for profile in ("cloud", "", "WSL"):
            with self.subTest(profile=profile):
                R, raised, env_file = self.early_record(profile)
                self.assertIsNone(R)
                self.assertIsInstance(raised, ValueError)
                self.assertIn("STOPCHECK_HOST_PROFILE", str(raised))
                env_file.assert_not_called()

    def test_every_record_carries_the_profile(self):
        for profile in ("wsl", "linux-vm"):
            with self.subTest(profile=profile):
                R, raised, _ = self.early_record(profile)
                self.assertIsNone(raised)
                self.assertEqual(R.get("host_profile"), {"declared": profile, "variable": "STOPCHECK_HOST_PROFILE"})

    def test_the_scope_names_the_declared_host(self):
        R, _, _ = self.early_record("wsl")
        self.assertEqual(R["scope"], "exploratory single trial; native WSL2 install, not a container; n8n 2.41.7 "
                                     "queue mode; internal task runners; claims about this config only")
        R, _, _ = self.early_record("linux-vm")
        self.assertNotIn("WSL", R["scope"])
        self.assertIn("Linux VM (host profile linux-vm)", R["scope"])

    # ---- the disk floor ----
    def test_wsl_host_checks_are_unchanged(self):
        with patch.object(H, "HOST_PROFILE", "wsl"), \
                patch.object(H.guards, "disk_ok", return_value=(True, 52.4)) as disk_ok, \
                patch.object(H.guards, "c_drive_free_gb", side_effect=AssertionError("not on wsl")), \
                patch.object(H, "_wsl_kernel", side_effect=AssertionError("not on wsl")), \
                patch.object(H, "run", side_effect=self.services_ok()):
            report = H.host_checks()
        disk_ok.assert_called_once_with()
        self.assertEqual(set(report), {"mono", "services", "violations", "disk", "ok"})
        self.assertEqual(report["disk"], {"ok": True, "free_gb": 52.4, "floor_gb": H.guards.DISK_FLOOR_GB_DEFAULT,
                                          "measured": "guards.c_drive_free_gb, /mnt/c"})
        self.assertIs(report["ok"], True)

    def test_linux_vm_floor_is_measured_on_the_results_folder_and_var_lib(self):
        report = self.vm_checks()
        self.assertIs(report["ok"], True)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["disk"]["paths"], {path: 50.0 for path in H.VM_DISK_PATHS})
        self.assertEqual(report["disk"]["floor_gb"], H.guards.DISK_FLOOR_GB_DEFAULT)
        self.assertEqual(report["profile"], {"declared": "linux-vm", "wsl_kernel": False, "ok": True})
        self.assertIn(os.path.join(H.HERE, "results"), H.VM_DISK_PATHS)

    def test_linux_vm_below_the_floor_or_unreadable_fails(self):
        results, var = H.VM_DISK_PATHS
        for free in ({results: 9.99, var: 50.0}, {results: 50.0, var: 3.0}, {results: None, var: 50.0},
                     {results: 50.0, var: None}):
            with self.subTest(free=free):
                report = self.vm_checks(free)
                self.assertIs(report["ok"], False)
                self.assertIs(report["disk"]["ok"], False)
                self.assertTrue(any("disk floor" in v for v in report["violations"]))

    def test_linux_vm_declared_on_a_wsl_or_unreadable_kernel_fails(self):
        for wsl in (True, None):
            with self.subTest(wsl=wsl):
                report = self.vm_checks(wsl=wsl)
                self.assertIs(report["ok"], False)
                self.assertTrue(any("host profile linux-vm" in v for v in report["violations"]))

    # ---- the hidden-path sentinel ----
    def isolation(self, profile, *, host_sees=True, unit_sees=(), mnt_c=(), mnts=None):
        trial = "TEST"
        sentinel = H.host_sentinel(trial)
        units = {"m.service": 11, "w.service": 12, "c.service": 13}
        mnts = mnts or {}

        def exists(path):
            if path == sentinel:
                return host_sees
            for unit, pid in units.items():
                if path == f"/proc/{pid}/root{sentinel}":
                    return unit in unit_sees
            raise AssertionError(f"unexpected path check: {path}")

        def info(pid):
            unit = next(u for u, p in units.items() if p == pid)
            return {"pid": pid, "uid": "1002", "netns": "net:[1]", "mntns": f"mnt:[{pid}]",
                    "mnt_c_visible": unit in mnt_c}

        def rows(name):
            return [{"trial": trial, "uid": "1002", "netns": "net:[1]", "mntns": "mnt:[99]", "mntc": "False",
                     "mnts": mnts.get(name, "False")}]
        links = {f"/run/netns/{H.NS}": "net:[1]", "/proc/1/ns/mnt": "mnt:[host]"}
        with patch.object(H, "HOST_PROFILE", profile), \
                patch.object(H, "U_MAIN", "m.service", create=True), patch.object(H, "U_WORKER", "w.service", create=True), \
                patch.object(H, "U_CRON", "c.service", create=True), \
                patch.object(H.os.path, "islink", return_value=True), \
                patch.object(H.os, "readlink", side_effect=lambda p: links[p]), \
                patch.object(H.os.path, "exists", side_effect=exists), \
                patch("pwd.getpwnam", return_value=types.SimpleNamespace(pw_uid=1002)), \
                patch.object(H, "unit_show", side_effect=lambda u: {"MainPID": str(units[u])}), \
                patch.object(H, "proc_info", side_effect=info), patch.object(H, "pings", side_effect=rows):
            return H.live_isolation({"trial": trial})

    def test_wsl_isolation_is_unchanged(self):
        self.assertIs(self.isolation("wsl", host_sees=False)["ok"], True)
        report = self.isolation("wsl", mnt_c=("w.service",))
        self.assertEqual(report["violations"], ["unit namespace confinement failed: w.service"])
        self.assertNotIn("host_sentinel", report)
        # the wsl profile neither places nor checks a sentinel
        self.assertIs(self.isolation("wsl", mnts={"n8n_bg": "True"})["ok"], True)

    def test_linux_vm_isolation_passes_when_only_the_host_sees_the_sentinel(self):
        report = self.isolation("linux-vm")
        self.assertIs(report["ok"], True)
        self.assertEqual(report["host_sentinel"], {"path": "/mnt/stopcheck-sentinel-TEST", "host_visible": True})
        self.assertEqual({u: v["sentinel_visible"] for u, v in report["units"].items()},
                         {"m.service": False, "w.service": False, "c.service": False})

    def test_linux_vm_unit_that_sees_the_sentinel_fails(self):
        for unit in ("m.service", "w.service", "c.service"):
            with self.subTest(unit=unit):
                report = self.isolation("linux-vm", unit_sees=(unit,))
                self.assertIs(report["ok"], False)
                self.assertIn(f"unit namespace confinement failed: {unit}", report["violations"])

    def test_linux_vm_sentinel_the_host_cannot_see_fails(self):
        report = self.isolation("linux-vm", host_sees=False)
        self.assertIs(report["ok"], False)
        self.assertIn("host sentinel not visible on the host: /mnt/stopcheck-sentinel-TEST", report["violations"])

    def test_linux_vm_canary_that_sees_the_sentinel_or_does_not_say_fails(self):
        for name in ("n8n_bg", "n8n_det", "n8n_oscron"):
            for value in ("True", None):
                with self.subTest(name=name, value=value):
                    report = self.isolation("linux-vm", mnts={name: value})
                    self.assertIs(report["ok"], False)
                    self.assertIn(f"OS-canary confinement failed: {name}", report["violations"])

    def test_linux_vm_places_a_fresh_sentinel_and_wsl_places_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sentinel")
            with patch.object(H, "host_sentinel", return_value=path):
                R = {}
                with patch.object(H, "HOST_PROFILE", "wsl"):
                    H.place_host_sentinel(R, "TEST")
                self.assertEqual(R, {})
                self.assertFalse(os.path.exists(path))
                with patch.object(H, "HOST_PROFILE", "linux-vm"):
                    H.place_host_sentinel(R, "TEST")
                self.assertEqual(R.get("host_sentinel"), {"path": path, "placed": True})
                self.assertTrue(os.path.exists(path))

    def test_an_existing_sentinel_is_refused_and_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sentinel")
            with open(path, "w", encoding="utf-8") as f:
                f.write("placed by another run")
            R = {}
            raised = None
            with patch.object(H, "host_sentinel", return_value=path), patch.object(H, "HOST_PROFILE", "linux-vm"):
                try:
                    H.place_host_sentinel(R, "TEST")
                except Exception as e:  # noqa: BLE001
                    raised = e
            self.assertIsInstance(raised, FileExistsError)
            self.assertNotIn("host_sentinel", R)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "placed by another run")

    def test_finalize_removes_the_placed_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sentinel")
            open(path, "w", encoding="utf-8").close()
            record = {"owns_resources": False, "guards_start": {}, "host_sentinel": {"path": path, "placed": True}}
            world = types.SimpleNamespace(logdir="/missing/mock/logs", down=lambda: None)
            real_exists = os.path.exists
            with patch.object(H, "pings", return_value=[]), \
                    patch.object(H.clockguard, "check_stamps", return_value={"ok": True}), \
                    patch.object(H.clockguard, "check", return_value={"ok": True}), \
                    patch.object(H.guards, "snapshot", return_value={}), \
                    patch.object(H.guards, "violations", return_value=[]), \
                    patch.object(H, "host_checks", return_value={"ok": True, "violations": []}), \
                    patch.object(H, "user_pids", return_value=[]), patch.object(H, "_save"), \
                    patch.object(H.time, "sleep"), \
                    patch.object(H.os.path, "exists", side_effect=lambda p: p == path and real_exists(p)):
                H._finalize(record, world, "TEST")
            self.assertFalse(real_exists(path))
            self.assertIs(record["host_sentinel"].get("removed"), True)
            self.assertEqual(record["cleanup_errors"], [])

    # ---- the canary's own report ----
    def test_canary_reports_whether_its_trials_sentinel_is_visible(self):
        C = load_canary()
        self.assertEqual(C.sentinel("XB-T-1"), H.host_sentinel("XB-T-1"))
        for visible in (True, False):
            with self.subTest(visible=visible):
                with patch.object(C.os.path, "exists", side_effect=lambda p: visible and p == C.sentinel("XB-T-1")):
                    self.assertEqual(C.identity("XB-T-1")["mnts"], str(visible))
        with patch.object(C.os.path, "exists", return_value=True):
            self.assertEqual(C.identity()["mnts"], "False")       # no trial given: nothing to look for


if __name__ == "__main__":
    unittest.main()
