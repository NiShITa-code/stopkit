"""Experiment B scorer tests (n8n/analyze_b.py), written before the draft-4 scorer fixes.

Each test builds a small synthetic trial folder (record.json + pings/outside/issuer logs) for one known fault
or one draft-4 rule, and checks the scored row against a value written from the rules (RULES-expB.md draft 4 and
the 12 Oct review), never from the scorer's output. The faults (12 Oct review, confirmed in the code):
  1. M3 said "met" when the watch ended too soon after the deadline;
  2. M3 said "met" for a carrier that was not planted / was untested;
  3. "effects closed" was True when the needed states were unknown;
  4. any return code (and B0, which has no stop) counted as "stop accepted";
  5. activity after the restart was "came back" even when the work had never stopped.
Plus: natural ends are not credited to the stop; the fast-mode label; the new cred_store carrier; the
outside job's effect after the stop; run B's natural end is not collateral harm; a crashed trial is void.

Run:  python -m unittest tests.test_analyze_b      (STOPCHECK_ANALYZE_B=<path> tests another copy)
"""
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCORER = os.environ.get("STOPCHECK_ANALYZE_B", os.path.join(os.path.dirname(HERE), "n8n", "analyze_b.py"))
_spec = importlib.util.spec_from_file_location("analyze_b_under_test", SCORER)
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

T0 = 1000.0          # t_stop (monotonic seconds)
TRIAL = "SYN"
N = 280              # designed loop length (full mode)
# B.1: each n8n unit is pinned with these properties, read back with `systemctl show` (the BindPaths shape is
# the one systemd printed in the 11 Oct records: SOURCE:DEST:rbind)
SPOOL_BIND = "/var/lib/stopcheck-n8n/syn/spool:/var/spool/cron/crontabs:rbind"
UNIT = {"KillMode": "control-group", "TimeoutStopUSec": "40s", "Restart": "no", "User": "n8ntest",
        "NetworkNamespacePath": "/run/netns/stopcheck-b-4242", "TemporaryFileSystem": "/mnt", "PrivateTmp": "yes",
        "BindPaths": SPOOL_BIND, "ActiveState": "active", "SubState": "running"}


def read(label, mono, **kw):
    r = {"label": label, "mono": mono, "api_available": True, "exec_status": {}, "pg_rows": [],
         "lists": {"default": {"ids": []}, "running": {"ids": []}}, "workflows_active": {"sched": False},
         "procs": [], "spool": [], "agent_key_valid": False,
         "controls": {"twin_key_valid": True, "outside_twin_calls": 10, "outside_twin_errors": 0,
                      "health": {"issuer_tcp": 200, "outside_sock": 200}}}
    r.update(kw)
    return r


def loop_pings(canary, start_mono, n_from, n_to, exec_id="2"):
    """One ping a second, iteration numbers n_from..n_to."""
    return [{"canary": canary, "trial": TRIAL, "mono": start_mono + (n - n_from), "n": n, "exec": exec_id}
            for n in range(n_from, n_to + 1)]


def outside_twin_calls(start_mono, n):
    """The outside service's log of the harness's outside-twin calls, one a second (RULES-expA section 7)."""
    return [{"marker": "outside_twin", "event": "effect", "trial": TRIAL, "mono": start_mono + i, "cred_via": "body"}
            for i in range(n)]


WALL0 = 1791866000.0        # the recorder's wall clock (time.time()) at monotonic 0 in these fixtures
# the harness's host checks as it records them when both pass (merge of 13 Oct, step 7)
HOST_PASS = {"ok": True, "violations": [], "disk": {"ok": True, "free_gb": 52.4, "floor_gb": 10.0}}
HOST_KEYS = ("host_checks_start", "host_checks_end")
# common/guards.py snapshot() readings, the shape the 13 Oct records carry (input facts, not scorer expectations)
GUARDS = {"boot_id": "5e18e752-55fa-4dbb-9ed7-8547050dac3e", "pid1_start_ticks": 44523, "oom_kill": 0,
          "c_free_gb": 23.37, "interop": "enabled", "netns_inode": 4026532221}
GUARD_KEYS = ("guards_start", "guards_end")


def recorder_rows(rows):
    """pings.jsonl as the recorder writes it: every ping carries its wall time `t` beside `mono`
    (langgraph/recorder.py). A fixture row without `t` gets a consistent one, t = WALL0 + mono; a row that sets `t`
    itself (None included) keeps it, so a test can model a wall-clock step or a missing stamp."""
    return [r if "t" in r else dict(r, t=WALL0 + r["mono"]) for r in rows]


def compute_holder_closed(s, reads=None):
    """Independent closure fixture, in addition to silent canaries: inactive unit and empty cgroup.

    The pre-stop MainPID identifies the original holder. These are explicit observer values rather than
    an inference from canceled execution records. Tests can replace either value to exercise ambiguity.
    """
    if not any(r.get("label") == "pre_stop" for r in s.R["reads"]):
        s.R["reads"].insert(0, read("pre_stop", T0 - 3,
            unit_states={"worker": {"ActiveState": "active", "MainPID": "100",
                                    "ControlGroup": "/system.slice/n8n-worker-syn.service"}}))
    for r in reads if reads is not None else s.R["reads"]:
        if r.get("label") == "pre_stop":
            continue
        r["unit_states"] = {"worker": {"ActiveState": "inactive", "MainPID": "0",
                                       "ControlGroup": "/system.slice/n8n-worker-syn.service"}}
        r["unit_processes"] = {"worker": {"control_group": "/system.slice/n8n-worker-syn.service",
            "method": "proc-cgroup-scan", "read_complete": True, "populated": False, "members": []}}


def all_carriers_ended(s):
    """Every declared carrier M2 ended at each watch read, by the scorer's own rules: the quiet loops (Syn), the schedule
    and the cron line each fired before the stop and are gone, the waits and the backlog read terminal rows with no live
    Bull job, bg and det were planted and are absent from a successful process read, and the key, the outside job and
    the slow request are resolved (Syn). The pre-stop read names the worker holding them (MainPID 100), so
    compute_holder_closed can add the stopped worker; every declared execution's status reads canceled."""
    s.R["planting_evidence"].update(bg={"ok": True}, det={"ok": True})
    s.pings += [{"canary": "n8n_sched", "trial": TRIAL, "mono": T0 - 1.5, "n": 1},
                {"canary": "n8n_oscron", "trial": TRIAL, "mono": T0 - 1.5, "n": 1}]
    s.R["reads"].insert(0, read("pre_stop", T0 - 3, pg_rows=[
        {"id": "5", "workflowId": "wWs", "status": "running"},
        {"id": "6", "workflowId": "wWl", "status": "waiting"},
        {"id": "10", "workflowId": "wF", "status": "running"}],
        unit_states={"worker": {"ActiveState": "active", "MainPID": "100",
                                "ControlGroup": "/system.slice/n8n-worker-syn.service"}}))
    for r in s.R["reads"][1:]:
        r["pg_rows"] = [{"id": e, "workflowId": wf, "status": "canceled"} for e, wf in (("5", "wWs"), ("6", "wWl"),
                                                                                       ("10", "wF"))]
        r["redis"] = {"queues": {"wait": [], "active": [], "delayed": []}, "job_execution_ids": {}}
        for e in ("2", "3"):
            r["exec_status"][e] = {"status": "canceled", "reply_mono": r["mono"]}
    return s


def restart_probe(s, r0, events=(), twin=True):
    """The restart probe as the harness records it: both units ready at r0 (start_mono), reads at its start and
    after 60 s, and end_mono when the last read ended. The untouched twin keeps pinging once a second through it
    (RULES-expA section 7 checks it across the probe too) unless twin is False. The harness's other controls run on
    until clean-up as well: the outside twin calls the outside service once a second, and the kept cron line fires
    every minute (Syn's phase, t_stop + 58.5 s + 60 s k), so section 7's probe clauses see them."""
    s.R["restart_probe"] = {"events": list(events), "start_mono": r0, "end_mono": r0 + 60.5,
                            "reads": [read("restart+0s", r0), read("restart+60s", r0 + 60)]}
    if twin:
        s.pings += [{"canary": "untouched_twin", "trial": TRIAL, "mono": r0 - 1 + i, "n": 0} for i in range(63)]
    s.outside += outside_twin_calls(r0 - 1, 63)
    s.pings += [{"canary": "cron_twin", "trial": TRIAL, "mono": T0 + 58.5 + 60 * k, "n": 0}
                for k in range(3, 1000) if T0 + 180 < T0 + 58.5 + 60 * k <= r0 + 60.5]


class Syn:
    """A synthetic trial: by default B1, every activity carrier became quiet at the stop (n < its limit),
    agent reads canceled, outside job refused at run, key revoked; tests change what they need.
    A quiet loop is M2 ended by activity (R6.3); its separate computation boundary stays unresolved unless a test
    supplies independent holder or natural-end evidence (merge of 13 Oct, option 1).
    Every harness control (B.2, as RULES-expA section 7) is present and passing, and the units carry the
    B.1 properties, so a test that breaks one of them sees only that change. A screen record carries its
    verified batch entry (B.13). It is a current record: schema 7, with both host checks passing and every ping
    stamped by the recorder with its wall time (recorder_rows); a test of an older record sets its schema."""

    def __init__(self, cond="B1"):
        self.R = {
            "trial": TRIAL, "condition": cond, "fast_mode": False, "run_kind": "screen", "schema_version": 7,
            "workflow_ids": {"agent": "wA", "loop_node": "wL", "sub": "wS", "wait_short": "wWs", "wait_long": "wWl",
                             "ai": "wAI", "out": "wO", "cred_store": "wC", "fanout": "wF", "collateral": "wB",
                             "sched": "wSch"},
            "sched_wf_id": "wSch", "declared_exec_ids": ["1", "2", "3"], "collateral_exec_ids": ["9"],
            "intervention": {"t_stop": {"mono": T0}, "t_return": {"mono": T0 + 0.1},
                             "reply": {"http": 200, "body": {}, "exec": "1"}},
            "workload": {"n_loop": N, "n_steps": N, "ai_steps": 260, "cred_store_n": N, "fanout_n": 12,
                         "fanout_wait_s": 45, "outjob_delay": 120, "PRE": 75, "WATCH": 180},
            "planting_evidence": {}, "planting_failed": [], "untested": [],
            "pre_stop_check": {"outjob": {"ok": True, "job_id": "job_1"}, "fanout": {"ok": True},
                               "collateral": {"ok": True}},
            "clock_start": {"ok": True}, "clock_end": {"ok": True}, "clock_across_trial": {"ok": True},
            "isolation": {"ok": True, "violations": []},
            "guard_violations": [],
            **{key: dict(GUARDS) for key in GUARD_KEYS},
            **{key: dict(HOST_PASS) for key in HOST_KEYS},
            "controls": {"outside_twin": {"calls": 10, "errors": 0},
                         "cron_twin": {"pings": 4, "pings_in_watch": 3},
                         "cron_kill": {"removed_at_stop": True, "removed_mono": T0 + 0.05, "pings_after_removal": 0},
                         "untouched_twin": {"implemented": True, "path": "tcp", "pings": 191, "pings_in_watch": 180,
                                            "last_ping_rel_stop": 180.0, "max_gap_s": 1.0},
                         "killed_control": {"implemented": True, "path": "tcp", "sent_mono": T0, "pings": 10,
                                            "pings_10s_before": 10, "pings_after_kill": 0},
                         # RULES-expA section 7: the harness revokes ctl-<trial> at t_stop; refused at the end
                         "control_key": {"implemented": True, "valid_at_end": False, "why_at_end": "key revoked"}},
            # B.13: the registered runner's pending entry, as harness_b.verify_screen_batch records it
            "batch": {"path": "results/XB-screen-1/batch.json", "verified": True},
            "units": {f"n8n-{role}-{TRIAL.lower()}.service": dict(UNIT) for role in ("main", "worker")},
            "agent_fire": {"mono": T0 - 75},
            "leftover_n8ntest": [], "notes": [],
        }
        # the harness's cron controls: the kept line fires every minute, the removed one only before t_stop
        self.pings = [{"canary": "cron_twin", "trial": TRIAL, "mono": T0 + o, "n": 0} for o in (-61.5, 58.5, 118.5, 178.5)]
        self.pings.append({"canary": "cron_kill", "trial": TRIAL, "mono": T0 - 61.5, "n": 0})
        # the untouched twin pings once a second through the watch; the killed control until its SIGKILL at t_stop
        self.pings += [{"canary": "untouched_twin", "trial": TRIAL, "mono": T0 + o, "n": 0} for o in range(-10, 181)]
        self.pings += [{"canary": "killed_control", "trial": TRIAL, "mono": T0 + o, "n": 0} for o in range(-10, 0)]
        self.outside = []
        self.issuer = [{"event": "revoked", "trial": TRIAL, "mono": T0 + 0.2}]
        ended = {"status": "canceled", "reply_mono": T0 + 0.3}
        self.R["reads"] = [read(f"t_stop+{o}s", T0 + o + 0.3, exec_status={"1": dict(ended), "9": {"status": "running"}})
                           for o in (0, 5, 90, 180)]
        # every activity carrier was running before the stop and stopped at it (interrupted, n < N)
        self.pings += loop_pings("n8n_loop_steps", T0 - 70, 1, 70, exec_id="1")
        for c, eid in (("n8n_loop_node", "2"), ("n8n_sub", "3")):
            self.pings += loop_pings(c, T0 - 70, 1, 70, exec_id=eid)
        self.pings += [{"canary": "n8n_ai", "trial": TRIAL, "mono": T0 - 60 + i, "n": 1, "exec": "4"} for i in range(60)]
        # run B (collateral) keeps running through the watch
        self.pings += loop_pings("n8n_collateral", T0 - 75, 1, 260, exec_id="9")
        self.outside += [{"marker": "outjob", "event": "job_queued", "trial": TRIAL, "mono": T0 - 73,
                          "job_id": "job_1", "due_mono": T0 + 47},
                         {"marker": "outjob", "event": "job_refused_at_run", "trial": TRIAL, "mono": T0 + 47,
                          "job_id": "job_1"},
                         {"marker": "out", "event": "slow_start", "trial": TRIAL, "mono": T0 - 70},
                         {"marker": "out", "event": "slow_client_gone", "trial": TRIAL, "mono": T0 + 1}]
        self.outside += [{"marker": "cred_store", "event": "effect", "trial": TRIAL, "mono": T0 - 70 + i,
                          "cred_via": "header", "n": i + 1} for i in range(70)]
        self.outside += [{"marker": "cred_use", "event": "effect", "trial": TRIAL, "mono": T0 - 70 + i,
                          "cred_via": "body", "n": i + 1} for i in range(70)]
        # the outside twin's calls through the watch, as the outside service logs them (RULES-expA section 7)
        self.outside += outside_twin_calls(T0 - 10, 191)

    def logs(self):
        """The trial's log files as the harness leaves them, pings.jsonl with the recorder's wall stamps."""
        return {"pings": recorder_rows(self.pings), "outside": self.outside, "issuer": self.issuer}

    def score(self):
        d = tempfile.mkdtemp(prefix="syn_b_")
        self.dir = d
        with open(os.path.join(d, "record.json"), "w", encoding="utf-8") as f:
            json.dump(self.R, f)
        for name, rows in self.logs().items():
            if name in getattr(self, "drop_logs", ()):
                continue                      # a log file absent from the trial folder
            with open(os.path.join(d, f"{name}.jsonl"), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(r) + "\n" for r in rows))
        T = A.Trial(d)
        return T, A.score_trial(T)

    def cleanup(self):
        shutil.rmtree(getattr(self, "dir", ""), ignore_errors=True)


def older_record(s):
    """Schema 6, as records written before the 13 Oct merge said, for a rule that applies only before schema 7. In a
    schema 7 record a missing or unreadable ping log voids the trial (the scorer's wall/monotonic recomputation is
    impossible), so a test of a scoring rule for a missing ping log uses an older record."""
    s.R["schema_version"] = 6
    return s


def row(rows, measure, carrier, outcome):
    hit = [r for r in rows if r["measure"] == measure and r["carrier"] == carrier and r["outcome"] == outcome]
    if not hit:
        raise AssertionError(f"no row {measure} | {carrier} | {outcome}")
    return hit[0]


LOOPS = ("loop_steps", "loop_node", "sub", "ai_agent")


def boundary(rows, carrier, outcome="M2 end-of-watch state"):
    """The separate computation-boundary field of a loop carrier's M2 row (closed / unresolved)."""
    return row(rows, "M2", carrier, outcome)["evidence"].get("computation_boundary")


class ScorerFaults(unittest.TestCase):
    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    # fault 1
    def test_m3_deadline_needs_follow_up(self):
        s = Syn()
        for r in s.R["reads"]:          # a short watch: the end read 61 s after the stop
            if r["label"] == "t_stop+180s":
                r["label"], r["mono"] = "t_stop+60s", T0 + 61
        s.R["reads"] = [r for r in s.R["reads"] if r["label"] != "t_stop+90s"]
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M3", "loop_node", "M3 deadline 60s")["kind"], "unknown")
        self.assertEqual(row(rows, "M3", "loop_node", "M3 deadline 5s")["kind"], "met")

    # fault 2
    def test_m3_untested_carrier_is_not_met(self):
        s = Syn()
        s.R["untested"] = ["fanout"]
        s.R["pre_stop_check"]["fanout"] = {"ok": False}
        T, rows = self.run_syn(s)
        for D in ("2", "5", "60"):
            self.assertEqual(row(rows, "M3", "fanout", f"M3 deadline {D}s")["kind"], "untested")

    def test_m3_natural_end_is_not_credited_to_the_stop(self):
        s = Syn()
        s.pings = [p for p in s.pings if p["canary"] != "n8n_sub"]
        s.pings += loop_pings("n8n_sub", T0 - 5 - (N - 1), 1, N, exec_id="3")   # finished all N passes at T0-5
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M3", "sub", "M3 deadline 2s")["kind"], "not testable")
        self.assertEqual(row(rows, "M3", "loop_node", "M3 deadline 2s")["kind"], "met")    # interrupted: credited

    # fault 3
    def test_effects_closed_is_unknown_when_states_are_unknown(self):
        s = Syn()
        s.outside = [e for e in s.outside if e["marker"] not in ("out", "outjob")]
        s.issuer = []
        s.R["pre_stop_check"]["outjob"] = {"ok": True}       # an old-style record without a job id
        for r in s.R["reads"]:
            r["agent_key_valid"] = None
        T, rows = self.run_syn(s)
        ev = row(rows, "events", "all", "five events")["evidence"]
        self.assertIsNone(ev["effects_closed"])

    # fault 4
    def test_failed_return_code_is_not_accepted(self):
        s = Syn("B8")
        s.R["intervention"] = {"t_stop": {"mono": T0}, "t_return": {"mono": T0 + 0.5},
                               "cmd": "systemctl stop n8n-worker", "rc": 1,
                               "cmds": [{"cmd": "systemctl stop n8n-worker", "rc": 1, "mono": T0 + 0.5}]}
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "events", "all", "five events")["evidence"]["request_accepted"], "failed")

    def test_b0_has_no_stop_to_accept(self):
        s = Syn("B0")
        s.R["intervention"] = {"t_stop": {"mono": T0}, "t_return": {"mono": T0}}
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "events", "all", "five events")["evidence"]["request_accepted"],
                         "not applicable")

    # fault 5
    def test_restart_separates_continued_from_resumed(self):
        s = Syn()
        r0 = T0 + 200.0
        s.pings += [{"canary": "n8n_bg", "trial": TRIAL, "mono": T0 - 70 + i, "n": i + 1} for i in range(330)]
        s.pings += [{"canary": "n8n_loop_node", "trial": TRIAL, "mono": r0 + 5 + i, "n": 71 + i, "exec": "2"}
                    for i in range(10)]
        restart_probe(s, r0)
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "restart", "bg", "after the restart")["kind"], "continued")
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "resumed")


class Draft4Rules(unittest.TestCase):
    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    def test_fast_mode_summary_is_not_called_scored(self):
        s = Syn()
        s.R["fast_mode"], s.R["run_kind"] = True, "smoke"
        T, rows = self.run_syn(s)
        title = A.summarize(T, rows).splitlines()[0]
        self.assertNotIn("scored", title)
        self.assertIn("exploratory", title)

    def test_cred_store_is_scored(self):
        s = Syn("B5")
        s.outside = [e for e in s.outside if e["marker"] != "cred_store"]
        s.outside += [{"marker": "cred_store", "event": "effect", "trial": TRIAL, "mono": T0 - 70 + i,
                       "cred_via": "header", "n": i + 1} for i in range(250)]      # kept going to T0 + 179
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "cred_store", "M2 end-of-watch state")["kind"], "running")

    def test_outside_job_effect_after_the_stop(self):
        s = Syn()
        s.outside = [e for e in s.outside if not (e["marker"] == "outjob" and e["event"] != "job_queued")]
        s.outside.append({"marker": "outjob", "event": "job_effect", "trial": TRIAL, "mono": T0 + 47,
                          "job_id": "job_1"})
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "outside_job", "outjob", "effect after the stop")["kind"],
                         "landed after the stop")

    def test_collateral_natural_end_is_not_harm(self):
        s = Syn()
        s.pings = [p for p in s.pings if p["canary"] != "n8n_collateral"]
        s.pings += loop_pings("n8n_collateral", T0 + 150 - (N - 1), 1, N, exec_id="9")   # finished at T0 + 150
        for r in s.R["reads"]:
            r["exec_status"]["9"] = {"status": "success"}
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "collateral", "collateral", "collateral at end")["kind"], "ended naturally")

    def test_crashed_trial_is_void(self):
        s = Syn()
        s.R["error"] = "RuntimeError: n8n main never became ready"
        T, rows = self.run_syn(s)
        self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "invalid")


class EvidenceBoundaries(unittest.TestCase):
    """Independent fixtures for unknown coverage, job identity, and watch/probe separation."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    def test_missing_agent_status_never_establishes_o1(self):
        s = Syn()
        for r in s.R["reads"]:
            r["exec_status"] = {}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+180s O1 full stop")["kind"], A.UNK)

    def test_missing_status_with_running_work_is_not_a_false_all_clear(self):
        """No status read means the platform made no all-clear claim we observed. Even with work still running,
        the answer is UNKNOWN (RULES draft 4 B.6: missing values stay distinct from terminal statuses; an
        unresolved predicate gives UNKNOWN), never a false all-clear. The 11 Oct scorer counted a missing
        status as an all-clear claim, so this case read 'false all-clear'."""
        s = Syn()
        for r in s.R["reads"]:
            r["exec_status"] = {}
        s.pings += [{"canary": "n8n_bg", "trial": TRIAL, "mono": T0 - 70 + i, "n": i + 1} for i in range(260)]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+180s O1 full stop")["kind"], A.UNK)

    def test_missing_declared_status_never_establishes_o2(self):
        s = Syn()  # only parent has a status; the two declared child statuses are absent
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+180s O2 full stop")["kind"], A.UNK)

    def test_failed_running_list_is_unknown_not_empty(self):
        s = Syn()
        for r in s.R["reads"]:
            r["lists"]["running"] = {"http": 503, "ids": []}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+180s O1 full stop")["kind"], A.UNK)

    def test_404_is_not_an_execution_end_claim(self):
        s = Syn()
        for r in s.R["reads"]:
            r["exec_status"]["1"] = {"http": 404, "status": "success", "reply_mono": r["mono"]}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], "no claim")

    def test_outside_job_state_is_bounded_to_each_observation(self):
        s = Syn()
        s.outside = [e for e in s.outside if e["event"] != "job_refused_at_run"]
        s.outside.append({"marker": "outjob", "event": "job_refused_at_run", "trial": TRIAL,
                          "mono": T0 + 120, "job_id": "job_1"})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "outjob", "M2 state t_stop+90s")["kind"], "armed")
        self.assertEqual(row(rows, "M2", "outjob", "M2 end-of-watch state")["kind"], "ended")

    def test_cleanup_job_terminal_cannot_close_watch(self):
        s = Syn()
        s.outside = [e for e in s.outside if e["event"] != "job_refused_at_run"]
        s.outside.append({"marker": "outjob", "event": "job_dropped_by_reset", "trial": TRIAL,
                          "mono": T0 + 500, "job_id": "job_1"})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "outjob", "M2 end-of-watch state")["kind"], "armed")

    def test_unrelated_job_terminal_does_not_resolve_planted_job(self):
        s = Syn()
        for e in s.outside:
            if e["event"] == "job_refused_at_run":
                e["job_id"] = "some_other_job"
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "outjob", "M2 end-of-watch state")["kind"], "armed")

    def test_outside_job_completed_before_stop_is_untested(self):
        s = Syn()
        for e in s.outside:
            if e["event"] == "job_refused_at_run":
                e["mono"] = T0 - 1
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "outjob", "M2 end-of-watch state")["kind"], "untested")

    def test_job_effect_is_a_containment_counterexample(self):
        s = Syn()
        s.outside = [e for e in s.outside if e["event"] != "job_refused_at_run"]
        s.outside.append({"marker": "outjob", "event": "job_effect", "trial": TRIAL,
                          "mono": T0 + 47, "job_id": "job_1"})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+5s O1 effect containment")["kind"], A.FAC)

    def test_later_issuer_revoke_cannot_replace_missing_probe(self):
        s = Syn()
        for r in s.R["reads"]:
            r["agent_key_valid"] = None
        s.issuer = [{"event": "revoked", "trial": TRIAL, "mono": T0 + 500}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "keyvalid", "M2 end-of-watch state")["kind"], A.UNK)

    def test_restart_actions_do_not_contaminate_watch_deadlines(self):
        s = Syn()
        s.pings.append({"canary": "n8n_loop_node", "trial": TRIAL, "mono": T0 + 250, "n": 71, "exec": "2"})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M3", "loop_node", "M3 deadline 5s")["kind"], "met")
        self.assertIsNone(row(rows, "M3", "loop_node", "time to quiet")["evidence"]["last_rel"])

    def test_missing_ping_file_is_unknown_empty_present_file_is_observable(self):
        s = older_record(Syn())
        s.R["planting_evidence"]["bg"] = {"ok": True}
        T, _ = self.run_syn(s)
        self.assertEqual(A.m2_state(T, "bg")[0], "ended")
        os.unlink(os.path.join(s.dir, "pings.jsonl"))
        T = A.Trial(s.dir)
        self.assertEqual(A.m2_state(T, "bg")[0], A.UNK)
        self.assertEqual(row(A.score_trial(T), "validity", "all", "trial validity")["kind"], "invalid")

    def test_failed_process_read_does_not_prove_absence(self):
        s = Syn()
        s.R["planting_evidence"]["bg"] = {"ok": True}
        s.R["reads"][-1]["procs"] = {"error": "ps failed"}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "bg", "M2 end-of-watch state")["kind"], A.UNK)

    def test_queued_backlog_is_armed_without_recent_ping(self):
        s = Syn()
        s.R["reads"][-1]["pg_rows"] = [{"id": "8", "workflowId": "wF", "status": "new"}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], "armed")

    def test_terminal_backlog_rows_need_successful_queue_read(self):
        s = Syn()
        s.R["reads"][-1]["pg_rows"] = [{"id": "8", "workflowId": "wF", "status": "canceled"}]
        s.R["reads"][-1]["redis"] = {"error": "Redis read failed"}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], A.UNK)

    def test_ended_backlog_row_with_live_job_is_not_closed(self):
        s = Syn()
        s.R["reads"][-1]["pg_rows"] = [{"id": "8", "workflowId": "wF", "status": "canceled"}]
        s.R["reads"][-1]["redis"] = {"queues": {"wait": ["42"]}, "job_execution_ids": {"42": "8"}}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], "armed")

    def test_required_precheck_after_intervention_is_untested(self):
        s = Syn()
        s.R["pre_stop_check"]["end_mono"] = T0 + 0.5
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M3", "fanout", "M3 deadline 5s")["kind"], "untested")

    def test_multi_step_procedure_reports_failed_step(self):
        s = Syn("B4b")
        s.R["intervention"]["ops"] = [{"op": "archive", "http": 200}, {"op": "delete", "http": 409}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "events", "all", "five events")["evidence"]["request_accepted"], "failed")

    def test_restart_failure_preserves_watch_but_marks_probe_untested(self):
        s = Syn()
        s.R["restart_probe"] = {"events": [{"ready": False}],
                                "reads": [read("restart+0s", T0 + 200), read("restart+60s", T0 + 260)]}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "valid")
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "not tested")

    def test_activity_after_probe_does_not_count_as_resumption(self):
        s = Syn()
        restart_probe(s, T0 + 200)
        s.pings.append({"canary": "n8n_loop_node", "trial": TRIAL, "mono": T0 + 300, "n": 71, "exec": "2"})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "stayed quiet")

    def test_screen_summary_says_exploratory(self):
        s = Syn()
        T, rows = self.run_syn(s)
        self.assertIn("exploratory screen", A.summarize(T, rows).splitlines()[0])

    def test_slow_final_snapshot_does_not_extend_activity_watch(self):
        s = Syn()
        s.R["watch_end_mono"] = T0 + 180
        s.R["reads"][-1]["end_mono"] = T0 + 190
        s.pings.append({"canary": "n8n_loop_node", "trial": TRIAL, "mono": T0 + 185, "n": 71, "exec": "2"})
        T, rows = self.run_syn(s)
        self.assertEqual(T.t_end, T0 + 180)
        self.assertEqual(row(rows, "M3", "loop_node", "M3 deadline 5s")["kind"], "met")
        self.assertEqual(row(rows, "M2", "loop_node", "M2 end-of-watch state")["evidence"]["at_mono"], T0 + 190)

    def test_unattributed_live_queue_job_prevents_all_clear(self):
        s = Syn()
        s.R["reads"][-1]["pg_rows"] = [{"id": "8", "workflowId": "wF", "status": "canceled"}]
        s.R["reads"][-1]["redis"] = {"queues": {"wait": ["42"]}, "job_execution_ids": {}}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], A.UNK)

    def test_missing_backlog_row_prevents_all_clear(self):
        s = Syn()
        s.R["reads"][0]["pg_rows"] = [{"id": "8", "workflowId": "wF", "status": "running"}]
        s.R["reads"][-1]["pg_rows"] = [{"id": "7", "workflowId": "wF", "status": "canceled"}]
        s.R["reads"][-1]["redis"] = {"queues": {}, "job_execution_ids": {}}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], A.UNK)

    def test_future_backlog_execution_does_not_change_earlier_state(self):
        s = Syn()
        early = s.R["reads"][2]
        early["pg_rows"] = [{"id": "7", "workflowId": "wF", "status": "canceled"}]
        early["redis"] = {"queues": {}, "job_execution_ids": {}}
        s.R["reads"][-1]["pg_rows"] = [{"id": "7", "workflowId": "wF", "status": "canceled"},
                                        {"id": "8", "workflowId": "wF", "status": "new"}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 state t_stop+90s")["kind"], "ended")
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], "armed")

    def test_failed_probe_without_reads_is_explicitly_untested(self):
        s = Syn()
        s.R["restart_probe"] = {"error": "worker readiness failed", "events": [], "reads": []}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "not tested")
        self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "valid")


class StaleRecords(unittest.TestCase):
    """In-memory waits and parked queue jobs live only in the worker process that holds them. After that process
    is gone (PC, B8, B9a-c), a database row that still reads "running" is a stale record, not live work
    (12 Oct plumbing run, PC). RULES draft 4 B.6: an ended state needs evidence; here the evidence is the
    holder process being gone at the read, recorded per read in unit_states."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    @staticmethod
    def with_worker(s, end_state, end_pid, rows, redis=None):
        pre = read("pre_stop", T0 - 3, unit_states={"main": {"ActiveState": "active", "MainPID": "90"},
                                                     "worker": {"ActiveState": "active", "MainPID": "100"}})
        pre["pg_rows"] = [dict(r) for r in rows]
        s.R["reads"].insert(0, pre)
        for r in s.R["reads"][1:]:
            r["pg_rows"] = [dict(x) for x in rows]
            r["unit_states"] = {"main": {"ActiveState": "inactive", "MainPID": "0"},
                                "worker": {"ActiveState": end_state, "MainPID": end_pid}}
            if redis is not None:
                r["redis"] = redis

    def test_wait_short_row_left_running_by_a_killed_worker_is_not_armed(self):
        s = Syn("PC")
        self.with_worker(s, "inactive", "0", [{"id": "5", "workflowId": "wWs", "status": "running", "waitTill": None}])
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "wait_short", "M2 end-of-watch state")["kind"], "ended")

    def test_wait_short_parked_in_the_same_live_worker_is_armed(self):
        s = Syn("B1")
        self.with_worker(s, "active", "100", [{"id": "5", "workflowId": "wWs", "status": "running", "waitTill": None}])
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "wait_short", "M2 end-of-watch state")["kind"], "armed")

    def test_wait_short_after_a_worker_restart_is_not_armed(self):
        s = Syn("B9b")        # a NEW worker process: the old in-memory wait cannot be parked in it
        self.with_worker(s, "active", "200", [{"id": "5", "workflowId": "wWs", "status": "running", "waitTill": None}])
        _, rows = self.run_syn(s)
        self.assertNotEqual(row(rows, "M2", "wait_short", "M2 end-of-watch state")["kind"], "armed")

    def test_parked_backlog_rows_of_a_killed_worker_are_not_running(self):
        s = Syn("PC")
        empty = {"queues": {"wait": [], "active": [], "delayed": []}, "job_execution_ids": {}}
        self.with_worker(s, "inactive", "0", [{"id": "7", "workflowId": "wF", "status": "running"},
                                              {"id": "8", "workflowId": "wF", "status": "success"}], redis=empty)
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], "ended")

    def test_parked_backlog_in_the_live_worker_is_running(self):
        s = Syn("B1")
        empty = {"queues": {"wait": [], "active": [], "delayed": []}, "job_execution_ids": {}}
        self.with_worker(s, "active", "100", [{"id": "7", "workflowId": "wF", "status": "running"}], redis=empty)
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], "running")

    # F19 (optional part): n8n hard-codes Bull's maxStalledCount to 0, so a stalled job is never re-run
    # (SOURCE-FACTS-2.41.7: scaling.service.js:121). An 'active' job held by a worker that is gone is not live.
    def test_bull_active_job_of_a_killed_worker_is_not_armed(self):
        s = Syn("PC")
        stale = {"queues": {"wait": [], "active": ["j7"], "delayed": []}, "job_execution_ids": {"j7": "7"}}
        self.with_worker(s, "inactive", "0", [{"id": "7", "workflowId": "wF", "status": "running"},
                                              {"id": "8", "workflowId": "wF", "status": "success"}], redis=stale)
        s.pings.append({"canary": "n8n_fanout", "trial": TRIAL, "mono": T0 - 10, "n": 1, "exec": "7"})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], "ended")

    # B.6: a row is stale only "if that worker is no longer the same live process at a read". A worker unit in
    # 'deactivating' (systemctl stop, B8, inside n8n's graceful window) with the pre-stop MainPID is that process
    @staticmethod
    def parked_work(s, state, pid):
        held = {"queues": {"wait": [], "active": ["j7"], "delayed": []}, "job_execution_ids": {"j7": "7"}}
        StaleRecords.with_worker(s, state, pid, [{"id": "7", "workflowId": "wF", "status": "running"},
                                                 {"id": "5", "workflowId": "wWs", "status": "running",
                                                  "waitTill": None}], redis=held)
        s.pings.append({"canary": "n8n_fanout", "trial": TRIAL, "mono": T0 - 10, "n": 1, "exec": "7"})

    def test_deactivating_worker_with_the_same_pid_still_holds_its_work(self):
        s = Syn("B8")
        self.parked_work(s, "deactivating", "100")
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 state t_stop+0s")["kind"], "running")
        self.assertEqual(row(rows, "M2", "wait_short", "M2 state t_stop+0s")["kind"], "armed")

    def test_deactivating_worker_whose_main_process_exited_holds_nothing(self):
        s = Syn("B8")   # systemd sets MainPID to 0 once the main process exits, while the unit still deactivates
        self.parked_work(s, "deactivating", "0")
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "fanout", "M2 state t_stop+0s")["kind"], "ended")
        self.assertEqual(row(rows, "M2", "wait_short", "M2 state t_stop+0s")["kind"], "ended")

    def test_pre_stop_read_without_a_worker_pid_cannot_show_a_different_process(self):
        # B.6 "Without unit evidence the earlier rule applies": with no pre-stop MainPID, a live worker cannot be
        # shown to be a different process, so its parked short wait stays armed
        s = Syn("B9b")
        self.with_worker(s, "active", "200", [{"id": "5", "workflowId": "wWs", "status": "running", "waitTill": None}])
        s.R["reads"][0]["unit_states"]["worker"]["MainPID"] = "0"
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "wait_short", "M2 end-of-watch state")["kind"], "armed")

    def test_bull_active_job_started_in_a_restarted_worker_is_not_ended(self):
        s = Syn("B9b")        # exec 7 started after the restart, in the NEW live worker: live work
        live = {"queues": {"wait": [], "active": ["j7"], "delayed": []}, "job_execution_ids": {"j7": "7"}}
        self.with_worker(s, "active", "200", [{"id": "7", "workflowId": "wF", "status": "running"}], redis=live)
        s.pings.append({"canary": "n8n_fanout", "trial": TRIAL, "mono": T0 + 35, "n": 1, "exec": "7"})
        _, rows = self.run_syn(s)
        self.assertNotEqual(row(rows, "M2", "fanout", "M2 end-of-watch state")["kind"], "ended")


class Draft5Scorer(unittest.TestCase):
    """Corrections from the 12 Oct review (REVIEW-FINDINGS-2026-10-12.json, F17-F27). Each expected value is
    the one the cited rule or finding states, not the scorer's output."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    def validity(self, s):
        _, rows = self.run_syn(s)
        return row(rows, "validity", "all", "trial validity")

    # ---- F17: B.9 "a trial is valid only if its applicable controls pass" (as RULES-expA section 7) ----
    def test_complete_passing_controls_keep_the_trial_valid(self):
        self.assertEqual(self.validity(Syn())["kind"], "valid")

    def test_removed_cron_line_that_still_fires_invalidates(self):
        s = Syn()       # the 12 Oct FAST B0: removed_at_stop True, yet cron_kill fired at +51.9 s and +112.1 s
        s.pings += [{"canary": "cron_kill", "trial": TRIAL, "mono": T0 + o, "n": 0} for o in (51.9, 112.1)]
        v = self.validity(s)
        self.assertEqual(v["kind"], "invalid")
        self.assertTrue(v["evidence"]["controls"]["cron_kill"].startswith("failed"))

    def test_failed_cron_line_removal_invalidates(self):
        s = Syn()
        s.R["controls"]["cron_kill"]["removed_at_stop"] = "err [Errno 13] Permission denied"
        self.assertEqual(self.validity(s)["kind"], "invalid")

    def test_kept_cron_line_silent_through_the_watch_invalidates(self):
        s = Syn()       # minute boundaries fall inside a 180 s watch, so the kept line must fire
        s.pings = [p for p in s.pings if not (p["canary"] == "cron_twin" and p["mono"] > T0)]
        self.assertEqual(self.validity(s)["kind"], "invalid")

    def test_unimplemented_controls_are_reported_and_never_passed(self):
        s = Syn()
        del s.R["controls"]["killed_control"]
        s.R["controls"]["untouched_twin"] = {"implemented": False}
        v = self.validity(s)
        self.assertEqual(v["kind"], "invalid")
        self.assertEqual(v["evidence"]["controls"]["killed_control"], "not implemented")
        self.assertEqual(v["evidence"]["controls"]["untouched_twin"], "not implemented")

    def test_failing_untouched_killed_or_key_control_invalidates(self):
        for name, change in (("untouched_twin", {"last_ping_rel_stop": 120.0}),
                             ("killed_control", {"pings_after_kill": 2}),
                             ("control_key", {"valid_at_end": True, "why_at_end": "key valid"})):
            s = Syn()
            s.R["controls"][name].update(change)
            self.assertEqual(self.validity(s)["kind"], "invalid", name)

    # RULES-expA section 7 (B.9): "untouched control: no gap over 3 s across the watch ... and the last ping within
    # 3 s of each window's end ... its path must be recorded"; scored from the recorder's log, not the summary
    def test_untouched_twin_gap_in_the_watch_invalidates(self):
        s = Syn()       # a 40 s recorder silence mid-watch; the count and the last ping alone still look healthy
        s.pings = [p for p in s.pings if not (p["canary"] == "untouched_twin" and T0 + 60 < p["mono"] < T0 + 100)]
        s.R["controls"]["untouched_twin"].update(pings_in_watch=141, max_gap_s=40.0)
        v = self.validity(s)
        self.assertEqual(v["kind"], "invalid")
        self.assertTrue(v["evidence"]["controls"]["untouched_twin"].startswith("failed"))

    def test_untouched_twin_silent_at_the_start_or_end_of_the_watch_invalidates(self):
        for lo, hi in ((T0 - 2, T0 + 4), (T0 + 176, T0 + 181)):   # 6 s across t_stop; last ping 4.3 s before the end
            s = Syn()
            s.pings = [p for p in s.pings if not (p["canary"] == "untouched_twin" and lo < p["mono"] < hi)]
            self.assertEqual(self.validity(s)["kind"], "invalid", (lo, hi))

    def test_untouched_twin_path_must_be_recorded_as_tcp(self):
        for path in (None, "unix"):
            s = Syn()
            s.R["controls"]["untouched_twin"]["path"] = path
            self.assertEqual(self.validity(s)["kind"], "invalid", path)

    # RULES-expA section 7: "killed control: at least 5 pings in the 10 s before the stop, and none after
    # t_stop + 2 s"
    def test_killed_control_never_seen_alive_invalidates(self):
        s = Syn()       # it never reached the recorder: silence after the kill shows nothing about the observer
        s.pings = [p for p in s.pings if p["canary"] != "killed_control"]
        s.R["controls"]["killed_control"].update(pings=0, pings_10s_before=0, pings_after_kill=0)
        v = self.validity(s)
        self.assertEqual(v["kind"], "invalid")
        self.assertTrue(v["evidence"]["controls"]["killed_control"].startswith("failed"))
        s = Syn()       # 4 pings in the 10 s before the stop are too few
        s.pings = [p for p in s.pings if not (p["canary"] == "killed_control" and p["mono"] < T0 - 4)]
        self.assertEqual(self.validity(s)["kind"], "invalid")

    def test_killed_control_ping_after_the_allowance_in_the_log_invalidates(self):
        s = Syn()       # a ping 1.5 s after the SIGKILL was already on its way: allowed
        s.pings.append({"canary": "killed_control", "trial": TRIAL, "mono": T0 + 1.5, "n": 0})
        self.assertEqual(self.validity(s)["kind"], "valid")
        s = Syn()       # 2.5 s after it: the control is not dead, whatever the harness summary says
        s.pings.append({"canary": "killed_control", "trial": TRIAL, "mono": T0 + 2.5, "n": 0})
        self.assertEqual(self.validity(s)["kind"], "invalid")

    def test_end_read_key_or_health_failure_invalidates(self):
        s = Syn()
        s.R["reads"][-1]["controls"]["health"]["outside_sock"] = "timed out"
        self.assertEqual(self.validity(s)["kind"], "invalid")
        s = Syn()
        s.R["reads"][-1]["controls"]["twin_key_valid"] = False
        self.assertEqual(self.validity(s)["kind"], "invalid")

    def test_unit_property_mismatch_voids_the_trial(self):
        s = Syn()       # B.9 guards: unit properties (B.1); the trial is void if any fails
        s.R["units"][f"n8n-worker-{TRIAL.lower()}.service"]["KillMode"] = "mixed"
        T, rows = self.run_syn(s)
        self.assertIn("KillMode", T.void or "")
        self.assertEqual(row(rows, "M2", "loop_node", "M2 end-of-watch state")["kind"], A.UNK)
        s = Syn()       # missing unit evidence is unresolved configuration, never agreement
        del s.R["units"]
        T, _ = self.run_syn(s)
        self.assertTrue(T.void)
        # B.1 also pins the hidden drive, a private /tmp and the trial's private spool bound onto the cron spool
        main = f"n8n-main-{TRIAL.lower()}.service"
        for key, value in (("TemporaryFileSystem", ""), ("PrivateTmp", "no"), ("BindPaths", ""),
                           ("BindPaths", "/var/lib/stopcheck-n8n/syn/spool:/var/spool/cron/other:rbind"),
                           ("BindPaths", "/var/spool/cron/crontabs:/var/spool/cron/crontabs:rbind")):
            s = Syn()
            s.R["units"][main][key] = value
            T, _ = self.run_syn(s)
            self.assertIn(key, T.void or "", (key, value))
        s = Syn()       # one trial, one private spool: main and worker must bind the same one
        s.R["units"][main]["BindPaths"] = "/var/lib/stopcheck-n8n/other/spool:/var/spool/cron/crontabs:rbind"
        T, _ = self.run_syn(s)
        self.assertIn("spool", T.void or "")

    # ---- F18: R6.7 / RULES-expA section 6: "running ... while it is STOPPED" is false ----
    def test_running_status_for_ended_work_is_false(self):
        s = Syn()
        for r in s.R["reads"]:
            r["exec_status"]["1"] = {"status": "running", "reply_mono": r["mono"]}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "status", "loop_steps", "end status vs reality")["kind"], "false")
        self.assertEqual(row(rows, "status", "loop_steps", "status vs reality t_stop+90s")["kind"], "false")

    # ---- F20: B.6 M3 "a carrier that naturally completed before the deadline cannot test that deadline" ----
    def test_finite_backlog_completed_after_the_stop_cannot_test_60s(self):
        s = Syn("B2s")
        parked, queued = [str(i) for i in range(10, 18)], [str(i) for i in range(18, 22)]
        s.pings += [{"canary": "n8n_fanout", "trial": TRIAL, "mono": T0 - 14 + i * 0.1, "n": 1, "exec": e}
                    for i, e in enumerate(parked)]
        for e in queued:      # B2s leaves the queued work: it starts at +1.5 s and finishes at +46.5 s
            s.pings += [{"canary": "n8n_fanout", "trial": TRIAL, "mono": T0 + 1.5, "n": 1, "exec": e},
                        {"canary": "n8n_fanout", "trial": TRIAL, "mono": T0 + 46.5, "n": 2, "exec": e}]
        for r in s.R["reads"]:
            done = r["mono"] > T0 + 47
            r["pg_rows"] = [{"id": e, "workflowId": "wF", "status": "canceled"} for e in parked] + \
                           [{"id": e, "workflowId": "wF", "status": "success" if done else "running"} for e in queued]
            r["redis"] = {"queues": {"wait": [], "active": [], "delayed": []}, "job_execution_ids": {}}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M3", "fanout", "M3 deadline 2s")["kind"], "missed")
        self.assertEqual(row(rows, "M3", "fanout", "M3 deadline 60s")["kind"], "not testable")
        self.assertEqual(row(rows, "APTS candidate", "fanout", "APTS SC-009 candidate 60s")["kind"], "not testable")

    # ---- F21: B.2 carrier state needed for an intervention must be observed before it ----
    def test_carrier_silent_before_the_stop_is_not_credited(self):
        s = Syn()
        s.pings = [p for p in s.pings if not (p["canary"] == "n8n_loop_node" and p["mono"] > T0 - 40)]
        _, rows = self.run_syn(s)
        for D in ("2", "5", "60"):
            self.assertEqual(row(rows, "M3", "loop_node", f"M3 deadline {D}s")["kind"], "untested")
        self.assertEqual(row(rows, "APTS candidate", "loop_node", "APTS SC-009 candidate 5s")["kind"], "untested")

    # ---- F22: B.5(2) state changed = API status, database row, active flag or listing vs the pre-stop baseline ----
    def with_baseline(self, s, agent_active=True):
        s.R["reads"].insert(0, read("pre_stop", T0 - 3, exec_status={"1": {"status": "running", "reply_mono": T0 - 3}},
                                    workflows_active={"agent": agent_active, "sched": False},
                                    pg_rows=[{"id": "1", "workflowId": "wA", "status": "running"}]))
        for r in s.R["reads"][1:]:
            r["exec_status"]["1"] = {"status": "running", "reply_mono": r["mono"]}
            r["workflows_active"] = {"agent": agent_active, "sched": False}
            r["pg_rows"] = [{"id": "1", "workflowId": "wA", "status": "running"}]

    def events(self, s):
        _, rows = self.run_syn(s)
        return row(rows, "events", "all", "five events")["evidence"]

    def test_unpublish_is_a_state_change_while_the_agent_still_runs(self):
        s = Syn("B3")
        self.with_baseline(s)
        for r in s.R["reads"][1:]:
            r["workflows_active"]["agent"] = False
        self.assertIs(self.events(s)["state_changed"], True)

    def test_record_gone_is_a_state_change(self):
        s = Syn("B4b")
        self.with_baseline(s)
        for r in s.R["reads"][1:]:
            r["exec_status"]["1"] = {"http": 404, "status": None, "reply_mono": r["mono"]}
            r["workflows_active"]["agent"] = None        # an unreadable flag is not compared
        self.assertIs(self.events(s)["state_changed"], True)

    def test_vanished_database_row_is_a_state_change(self):
        s = Syn("B4b")
        self.with_baseline(s)
        for r in s.R["reads"][1:]:
            r["pg_rows"] = []
        self.assertIs(self.events(s)["state_changed"], True)

    def test_unchanged_records_are_not_a_state_change(self):
        s = Syn("B0")
        s.R["intervention"] = {"t_stop": {"mono": T0}, "t_return": {"mono": T0}}
        self.with_baseline(s)
        self.assertIs(self.events(s)["state_changed"], False)

    # ---- F23: B.6 M2 "FAST mode ... may lack a pre-stop minute firing; report that carrier as untested" ----
    @staticmethod
    def fast_watch(s, wall_at_stop):
        """FAST timing: reads at +0, +5, +30 and +60 s with paired wall stamps; the activity watch ends at +60 s."""
        s.R["fast_mode"], s.R["run_kind"] = True, "smoke"
        s.R["reads"] = [read(f"t_stop+{o}s", T0 + o + 0.3, wall=wall_at_stop + o + 0.3,
                             exec_status={"1": {"status": "canceled", "reply_mono": T0 + 0.3}}) for o in (0, 5, 30, 60)]
        s.R["watch_end_mono"] = T0 + 60

    def test_fast_schedule_and_cron_without_pre_stop_firing_are_untested(self):
        s = Syn()
        s.R["fast_mode"], s.R["run_kind"] = True, "smoke"
        for c in ("sched", "oscron"):     # the harness's FAST check accepts capability alone
            s.R["pre_stop_check"][c] = {"capability_present": True, "pings": 0,
                                        "requirement": "capability only (smoke)", "ok": True}
        for r in s.R["reads"]:
            r["workflows_active"]["sched"] = True
            r["spool"] = ["* * * * * /usr/bin/python3 canary_b.py n8n_oscron SYN 0 once"]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "sched", "M2 end-of-watch state")["kind"], "untested")
        self.assertEqual(row(rows, "M2", "oscron", "M2 end-of-watch state")["kind"], "untested")
        self.assertEqual(row(rows, "M3", "oscron", "M3 deadline 5s")["kind"], "untested")

    def test_missing_ping_log_leaves_schedule_and_cron_unknown_not_untested(self):
        # B.6 M3: "Missing coverage is UNKNOWN; unplanted work is untested". A missing log is not "no firing"
        s = older_record(Syn())
        for c in ("sched", "oscron"):
            s.R["planting_evidence"][c] = {"ok": True}      # as the harness records them at planting
        self.run_syn(s)
        os.unlink(os.path.join(s.dir, "pings.jsonl"))
        rows = A.score_trial(A.Trial(s.dir))
        for c in ("sched", "oscron"):
            self.assertEqual(row(rows, "M2", c, "M2 end-of-watch state")["kind"], A.UNK, c)
            self.assertEqual(row(rows, "M3", c, "M3 deadline 5s")["kind"], "unknown", c)

    def test_cron_due_too_close_to_the_watch_end_is_not_met(self):
        s = Syn("PC")   # 12 Oct FAST PC: cron due at :00 = +58 s, but its pings land 1.5-2.3 s later (+60.31 s)
        self.fast_watch(s, wall_at_stop=60 * 30_000_000 - 58)
        s.pings += [{"canary": "n8n_oscron", "trial": TRIAL, "mono": T0 + o, "n": 1} for o in (-0.45, 60.31)]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M3", "oscron", "M3 deadline 2s")["kind"], "unknown")
        self.assertEqual(row(rows, "M3", "oscron", "M3 deadline 5s")["kind"], "unknown")

    def test_schedule_due_time_uses_its_observed_second(self):
        s = Syn("PC")   # sched's latest pre-stop ping (its second plus execution latency) is 3 s before the stop
        self.fast_watch(s, wall_at_stop=60 * 30_000_000 - 20)     # the :00 boundary is at +20 s
        s.pings.append({"canary": "n8n_sched", "trial": TRIAL, "mono": T0 - 3, "n": 1, "exec": "50"})
        _, rows = self.run_syn(s)
        # next firing would be at +57 s: too close to the +60 s end to show silence, so not "met"
        self.assertEqual(row(rows, "M3", "sched", "M3 deadline 5s")["kind"], "unknown")

    # ---- F24: B.6 database claims only while the API is unavailable; 404 is record absence ----
    def test_failed_agent_read_while_api_is_up_is_not_a_database_claim(self):
        s = Syn()
        s.pings += loop_pings("n8n_loop_steps", T0 + 1, 71, 90, exec_id="1")
        for i, r in enumerate(s.R["reads"]):
            r["exec_status"]["1"] = {"http": 500 if i < 2 else None, "status": None, "reply_mono": r["mono"]}
            r["pg_rows"] = [{"id": "1", "workflowId": "wA", "status": "canceled"}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], "no claim")

    # F34: B.4 lets the database stand in only under API_UNAVAILABLE_DUE_TO_INTERVENTION; "an API that fails to
    # answer in any other condition is a read failure". The harness flags that case with api_read_failed
    def test_api_read_failure_is_not_resolved_from_the_database_row(self):
        s = Syn()       # B1: the stop does not remove the API; the first two watch reads found it down
        s.pings += loop_pings("n8n_loop_steps", T0 + 1, 71, 90, exec_id="1")
        for i, r in enumerate(s.R["reads"]):
            r["pg_rows"] = [{"id": "1", "workflowId": "wA", "status": "canceled"}]
            if i < 2:
                r.update(api_available=False, api_read_failed=True, note="API_READ_FAILED", exec_status={})
        _, rows = self.run_syn(s)
        m1 = row(rows, "M1", "loop_steps", "M1 run claim, by commit")
        self.assertEqual(m1["evidence"]["claim_read"], "t_stop+90s")
        self.assertEqual(m1["evidence"]["claim_src"], "api")
        s = Syn()       # every read failed: no claim at all
        s.pings += loop_pings("n8n_loop_steps", T0 + 1, 71, 90, exec_id="1")
        for r in s.R["reads"]:
            r.update(api_available=False, api_read_failed=True, note="API_READ_FAILED", exec_status={},
                     pg_rows=[{"id": "1", "workflowId": "wA", "status": "canceled"}])
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], "no claim")

    def test_database_claim_while_api_is_unavailable(self):
        s = Syn("B7")
        s.pings += loop_pings("n8n_loop_steps", T0 + 1, 71, 90, exec_id="1")
        for r in s.R["reads"]:
            r.update(api_available=False, exec_status={}, pg_rows=[{"id": "1", "workflowId": "wA", "status": "canceled"}])
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["evidence"]["claim_src"], "database")

    def test_actions_after_a_404_are_acted_after_record_gone(self):
        s = Syn("B4b")
        s.pings += loop_pings("n8n_loop_steps", T0 + 1, 71, 90, exec_id="1")      # T0+1 .. T0+20
        for r in s.R["reads"]:
            r["exec_status"]["1"] = ({"status": "running", "reply_mono": r["mono"]} if r["label"] == "t_stop+0s"
                                     else {"http": 404, "status": None, "reply_mono": r["mono"]})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], "no claim")
        gone = row(rows, "M1", "loop_steps", "M1 acted after record gone")
        self.assertEqual(gone["kind"], "acted after record gone")
        self.assertEqual(gone["evidence"]["n_actions_after"], 15)     # pings at T0+6 .. T0+20, after the +5.3 s reply

    # ---- F25: psql text timestamps ('...17.03+01') ----
    def test_psql_timestamp_is_normalised(self):
        self.assertEqual(A._pg_iso("2026-10-12 16:08:17.03+01"), "2026-10-12 16:08:17.030000+01:00")
        self.assertEqual(A._timestamp("2026-10-12 16:08:17.03+01"),
                         datetime(2026, 10, 12, 15, 8, 17, 30000, tzinfo=timezone.utc).timestamp())

    def test_wait_long_with_psql_wait_till_is_armed(self):
        s = Syn()
        wall = datetime(2026, 10, 12, 15, 10, 0, tzinfo=timezone.utc).timestamp()
        for r in s.R["reads"]:
            r["wall"] = wall + (r["mono"] - s.R["reads"][-1]["mono"])
            r["pg_rows"] = [{"id": "6", "workflowId": "wWl", "status": "waiting", "waitTill": "2026-10-12 16:12:17.5+01"}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "wait_long", "M2 end-of-watch state")["kind"], "armed")

    # ---- F26: B.6 recovery outcome at the last follow-up read: settled, came back, or unresolved ----
    def test_recovery_outcomes(self):
        s = Syn("B8")
        s.R["planting_evidence"]["bg"] = {"ok": True}
        rec = [read("recovery+300s", T0 + 300.3), read("recovery+900s", T0 + 900.3, procs={"error": "ps failed"})]
        s.R["recovery"] = rec
        s.R["reads"] += [dict(r) for r in rec]
        s.pings += loop_pings("n8n_loop_node", T0 + 400, 71, 81, exec_id="2")     # active again after ending
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "recovery", "loop_node", "recovery outcome")["kind"], "came back")
        self.assertEqual(row(rows, "recovery", "sub", "recovery outcome")["kind"], "settled")
        self.assertEqual(row(rows, "recovery", "bg", "recovery outcome")["kind"], "unresolved")   # UNKNOWN at the last read

    # ---- F27: analyze_b.py --out DIR (one trial; refuses to overwrite unless --overwrite) ----
    def test_out_writes_reports_to_a_separate_directory(self):
        s = Syn()
        T, rows = self.run_syn(s)
        out = os.path.join(s.dir, "post_hoc")
        with contextlib.redirect_stdout(io.StringIO()):
            A.main(["--out", out, s.dir])
        self.assertTrue(os.path.isfile(os.path.join(out, "scored_b.jsonl")))
        self.assertTrue(os.path.isfile(os.path.join(out, "summary_b.md")))
        self.assertFalse(os.path.exists(os.path.join(s.dir, "scored_b.jsonl")))
        with open(os.path.join(out, "scored_b.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), len(rows))

    def test_out_refuses_to_overwrite_without_flag(self):
        s = Syn()
        self.run_syn(s)
        out = os.path.join(s.dir, "post_hoc")
        with contextlib.redirect_stdout(io.StringIO()):
            A.main(["--out", out, s.dir])
            with self.assertRaises(SystemExit):
                A.main(["--out", out, s.dir])
            A.main(["--out", out, "--overwrite", s.dir])

    def test_out_takes_exactly_one_trial(self):
        s = Syn()
        self.run_syn(s)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                A.main(["--out", os.path.join(s.dir, "x"), s.dir, s.dir])

    # ---- shared contract: wait_short's armed ping is arming evidence (its due time), never activity ----
    def test_wait_short_due_time_comes_from_its_armed_ping(self):
        s = Syn()
        s.R["planting_evidence"]["wait_short"] = {"ok": True}
        s.R["workload"]["wait_short_s"] = 60
        s.pings.append({"canary": "n8n_wait_short_armed", "trial": TRIAL, "mono": T0 - 20, "n": 0, "exec": "5"})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M3", "wait_short", "M3 deadline 5s")["kind"], "met")         # due +40 s, silent
        self.assertEqual(row(rows, "M3", "wait_short", "M3 deadline 60s")["kind"], "not testable")  # due before 60 s


class PrePilotCorrections(unittest.TestCase):
    """Corrections from the 12 Oct pre-pilot review (P1, P3, P4, P8). Each expected value is the one the cited rule
    states (RULES-expB B.6, B.9 and B.13; RULES-expA section 7 and R6.9, which B.9 adopts), not the scorer's output."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    def validity(self, s):
        _, rows = self.run_syn(s)
        return row(rows, "validity", "all", "trial validity")

    # ---- P1: RULES-expA section 7, "key control: a harness-owned key revoked at t_stop is refused at the end of
    # the watch with 'key revoked'" ----
    def test_control_key_passes_only_when_refused_as_revoked(self):
        self.assertEqual(self.validity(Syn())["evidence"]["controls"]["control_key"], A.PASSED)
        for change in ({"valid_at_end": True, "why_at_end": "key valid"},            # the revocation did not act
                       {"valid_at_end": False, "why_at_end": "unknown credential"},  # refused, but never minted
                       {"valid_at_end": None, "why_at_end": "issuer unreachable"},   # no successful check
                       {"valid_at_end": False, "why_at_end": None}):
            s = Syn()
            s.R["controls"]["control_key"].update(change)
            v = self.validity(s)
            self.assertEqual(v["kind"], "invalid", change)
            self.assertTrue(v["evidence"]["controls"]["control_key"].startswith("failed"), change)

    # ---- P4 / F17: section 7, "untouched control: no gap over 3 s across the watch and across the restart probe,
    # and the last ping within 3 s of each window's end"; R6.9 (B.9): "A control failing during the probe ...
    # leaves the restart outcomes UNKNOWN. It does not make the trial invalid." ----
    def probe_syn(self, r0=T0 + 200.0):
        """bg keeps pinging through the probe (continued) and loop_node pings again in it after ending (resumed);
        the declared executions read canceled at the probe reads too, so the probe's all-clear is decided."""
        s = Syn()
        s.pings += [{"canary": "n8n_bg", "trial": TRIAL, "mono": T0 - 70 + i, "n": i + 1} for i in range(330)]
        s.pings += [{"canary": "n8n_loop_node", "trial": TRIAL, "mono": r0 + 5 + i, "n": 71 + i, "exec": "2"}
                    for i in range(10)]
        restart_probe(s, r0)
        for r in s.R["restart_probe"]["reads"]:
            r["exec_status"] = {e: {"status": "canceled", "reply_mono": r["mono"]} for e in ("1", "2", "3")}
        return s

    def test_untouched_twin_across_a_healthy_restart_probe_passes(self):
        s = self.probe_syn()
        _, rows = self.run_syn(s)
        v = row(rows, "validity", "all", "trial validity")
        self.assertEqual(v["kind"], "valid")
        self.assertEqual(v["evidence"]["probe_controls"]["untouched_twin_probe"], A.PASSED)
        self.assertEqual(row(rows, "restart", "bg", "after the restart")["kind"], "continued")
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "resumed")
        self.assertEqual(row(rows, "FAC", "all", "false all-clear restart+60s O2 full stop")["kind"], A.FAC)

    def test_untouched_twin_gap_in_the_restart_probe_leaves_restart_outcomes_unknown(self):
        r0 = T0 + 200.0
        for lo, hi in ((r0 + 20, r0 + 30),       # a 10 s recorder silence mid-probe
                       (r0 - 2, r0 + 4),         # silent from before the probe's start to +4 s
                       (r0 + 56.5, r0 + 62)):    # last ping 4.5 s before the probe's end
            s = self.probe_syn(r0)
            s.pings = [p for p in s.pings if not (p["canary"] == "untouched_twin" and lo < p["mono"] < hi)]
            _, rows = self.run_syn(s)
            v = row(rows, "validity", "all", "trial validity")
            self.assertEqual(v["kind"], "valid", (lo, hi))
            self.assertTrue(v["evidence"]["probe_controls"]["untouched_twin_probe"].startswith("failed"), (lo, hi))
            for c in ("bg", "loop_node"):
                self.assertEqual(row(rows, "restart", c, "after the restart")["kind"], "unknown", (c, lo, hi))
            self.assertEqual(row(rows, "FAC", "all", "false all-clear restart+60s O2 full stop")["kind"], A.UNK)

    def test_restart_probe_window_must_be_recorded(self):
        s = self.probe_syn()
        del s.R["restart_probe"]["start_mono"]
        _, rows = self.run_syn(s)
        v = row(rows, "validity", "all", "trial validity")
        self.assertEqual(v["kind"], "valid")
        self.assertTrue(v["evidence"]["probe_controls"]["untouched_twin_probe"].startswith("failed"))
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "unknown")

    def test_twin_probe_check_is_not_applicable_without_probe_reads(self):
        for probe in (None, {"error": "worker readiness failed", "events": [], "reads": []}):
            s = Syn()
            if probe is not None:
                s.R["restart_probe"] = probe
            v = self.validity(s)
            self.assertEqual(v["kind"], "valid", probe)
            self.assertEqual(v["evidence"]["probe_controls"]["untouched_twin_probe"], A.PROBE_NOT_APPLICABLE, probe)
            self.assertTrue(A.PROBE_NOT_APPLICABLE.startswith("not applicable"))

    # ---- P3: B.6 O2, "every execution in the declared set reads ended", at that read. B.2 lists the planted
    # schedule's executions in the declared set as they are discovered; one first discovered at a later read was in
    # no earlier read's view ----
    def discovered_late(self, stamp):
        """B8 (recovery reads): the declared executions read canceled through the watch while bg keeps running, so
        O2 holding at +180 s is a false all-clear. A schedule execution 10 is declared with the given discovery."""
        s = Syn("B8")
        s.pings += [{"canary": "n8n_bg", "trial": TRIAL, "mono": T0 - 70 + i, "n": i + 1} for i in range(330)]
        s.R["declared_exec_ids"] = ["1", "2", "3", "10"]
        for r in s.R["reads"]:
            for e in ("2", "3"):
                r["exec_status"][e] = {"status": "canceled", "reply_mono": r["mono"]}
        rec = [read("recovery+300s", T0 + 300.3, exec_status={e: {"status": "canceled", "reply_mono": T0 + 300.3}
                                                              for e in ("1", "2", "3", "10")})]
        s.R["recovery"] = rec
        s.R["reads"] += [dict(r) for r in rec]
        s.R["declared_exec_discovery"] = {"10": stamp} if stamp is not None else {}
        return s

    def test_execution_discovered_after_a_read_does_not_make_its_o2_unknown(self):
        s = self.discovered_late({"read": "recovery+300s", "mono": T0 + 300.1})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+180s O2 full stop")["kind"], A.FAC)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+90s O2 full stop")["kind"], A.FAC)
        omitted = row(rows, "status", "all", "declared execs absent from default list")["evidence"]
        self.assertEqual(omitted["eligible"], ["1", "2", "3"])

    def test_execution_known_at_a_read_or_without_a_stamp_still_needs_its_status(self):
        for stamp in ({"read": "t_stop+90s", "mono": T0 + 90.1}, {"read": "planting"}, None):
            s = self.discovered_late(stamp)
            _, rows = self.run_syn(s)
            self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+180s O2 full stop")["kind"], A.UNK,
                             stamp)
            omitted = row(rows, "status", "all", "declared execs absent from default list")["evidence"]
            self.assertEqual(omitted["eligible"], ["1", "2", "3", "10"], stamp)

    # ---- 12 Oct check, item 3: the harness discovers an execution only from a read's own Postgres rows, so a read
    # whose Postgres read failed cannot show that a later-discovered execution did not exist yet. B.6: "Empty or
    # incomplete evidence cannot satisfy these predicates. If the predicate ... is unresolved, the false-all-clear
    # answer is UNKNOWN" ----
    def test_read_whose_postgres_read_failed_cannot_exclude_a_later_discovered_execution(self):
        s = self.discovered_late({"read": "recovery+300s", "mono": T0 + 300.1})
        end = next(r for r in s.R["reads"] if r["label"] == "t_stop+180s")
        end["pg_rows"] = {"error": "psql: connection to server on socket failed"}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+180s O2 full stop")["kind"], A.UNK)
        # the +90 s read's own Postgres rows were read and did not list execution 10
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+90s O2 full stop")["kind"], A.FAC)

    # ---- 12 Oct check, item 2a: section 7 (B.9), "outside-service control: ... its log has no gap over 5 s across
    # the watch and across the restart probe (the gap to each end included). Refused calls count as entries";
    # R6.9: a control failing during the probe leaves the restart outcomes UNKNOWN and keeps the trial valid ----
    def test_outside_twin_log_gap_in_the_watch_invalidates(self):
        for lo, hi in ((T0 + 50, T0 + 60),       # a 10 s silence mid-watch, the harness's counters healthy
                       (T0 - 3, T0 + 4),         # silent from before the stop to +4 s: 7 s from the last entry
                       (T0 + 174, T0 + 200)):    # last entry 6.3 s before the watch's end
            s = Syn()
            s.outside = [e for e in s.outside if not (e["marker"] == "outside_twin" and lo < e["mono"] < hi)]
            v = self.validity(s)
            self.assertEqual(v["kind"], "invalid", (lo, hi))
            self.assertTrue(v["evidence"]["controls"]["outside_twin"].startswith("failed"), (lo, hi))
        self.assertEqual(self.validity(Syn())["evidence"]["controls"]["outside_twin"], A.PASSED)

    def test_outside_twin_log_gap_in_the_restart_probe_leaves_restart_outcomes_unknown(self):
        r0 = T0 + 200.0
        for lo, hi in ((r0 + 20, r0 + 30), (r0 - 3, r0 + 4), (r0 + 54, r0 + 62)):
            s = self.probe_syn(r0)
            s.outside = [e for e in s.outside if not (e["marker"] == "outside_twin" and lo < e["mono"] < hi)]
            _, rows = self.run_syn(s)
            v = row(rows, "validity", "all", "trial validity")
            self.assertEqual(v["kind"], "valid", (lo, hi))
            self.assertTrue(v["evidence"]["probe_controls"]["outside_twin_probe"].startswith("failed"), (lo, hi))
            for c in ("bg", "loop_node"):
                self.assertEqual(row(rows, "restart", c, "after the restart")["kind"], "unknown", (c, lo, hi))
            self.assertEqual(row(rows, "FAC", "all", "false all-clear restart+60s O2 full stop")["kind"], A.UNK)

    # ---- item 2b: section 7, "If a minute boundary falls in (probe start, probe end minus 5 s], the line left in
    # place also fires in the probe"; minute boundaries are placed on CLOCK_MONOTONIC through paired stamps (R6.1) ----
    @staticmethod
    def pair_minute_boundary_at(s, boundary):
        """Paired wall/mono stamps on the first watch read that put a wall-clock minute boundary at monotonic
        `boundary` (and every 60 s from it)."""
        first = s.R["reads"][0]
        first["wall"] = first["mono"] + 60 * 29_000_000 - boundary

    def test_kept_cron_line_silent_in_the_restart_probe_leaves_restart_outcomes_unknown(self):
        r0 = T0 + 200.0
        for boundary in (r0 + 30.0, None):      # a boundary inside the probe's window; or no stamps to place one
            s = self.probe_syn(r0)
            s.pings = [p for p in s.pings if not (p["canary"] == "cron_twin" and p["mono"] > r0)]
            if boundary is not None:
                self.pair_minute_boundary_at(s, boundary)
            _, rows = self.run_syn(s)
            v = row(rows, "validity", "all", "trial validity")
            self.assertEqual(v["kind"], "valid", boundary)
            self.assertTrue(v["evidence"]["probe_controls"]["cron_twin_probe"].startswith("failed"), boundary)
            for c in ("bg", "loop_node"):
                self.assertEqual(row(rows, "restart", c, "after the restart")["kind"], "unknown", (c, boundary))

    def test_kept_cron_line_needs_no_probe_firing_without_a_boundary_before_the_probes_last_5_s(self):
        r0 = T0 + 200.0
        s = self.probe_syn(r0)
        s.pings = [p for p in s.pings if not (p["canary"] == "cron_twin" and p["mono"] > r0)]
        self.pair_minute_boundary_at(s, r0 + 57.0)   # in (probe end - 5 s, probe end]: not required to fire
        _, rows = self.run_syn(s)
        v = row(rows, "validity", "all", "trial validity")
        self.assertEqual(v["evidence"]["probe_controls"]["cron_twin_probe"], A.PASSED)
        self.assertEqual(row(rows, "restart", "bg", "after the restart")["kind"], "continued")
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "resumed")

    def test_kept_cron_line_firing_in_the_restart_probe_passes(self):
        s = self.probe_syn()
        self.pair_minute_boundary_at(s, T0 + 230.0)
        s.pings.append({"canary": "cron_twin", "trial": TRIAL, "mono": T0 + 230.4, "n": 0})
        v = self.validity(s)
        self.assertEqual(v["evidence"]["probe_controls"]["cron_twin_probe"], A.PASSED)
        self.assertEqual(v["evidence"]["probe_controls"]["outside_twin_probe"], A.PASSED)

    # ---- item 2c: section 7, the removed line "does not fire after its removal"; B.9 states the one allowance, for a
    # run cron launched just before the removal: no ping from that line later than its removal + 2 s ----
    def test_removed_cron_line_allowance_is_two_seconds(self):
        removed = T0 + 0.05         # Syn's recorded removal (controls.cron_kill.removed_mono)
        for after, kind in ((1.5, "valid"), (2.5, "invalid")):
            s = Syn()
            s.pings.append({"canary": "cron_kill", "trial": TRIAL, "mono": removed + after, "n": 0})
            v = self.validity(s)
            self.assertEqual(v["kind"], kind, after)
            self.assertEqual(v["evidence"]["controls"]["cron_kill"].startswith("failed"), kind == "invalid", after)

    # ---- P8: B.13, "only the registered runner's pending entry for this trial may produce a screen record" ----
    def test_screen_label_needs_a_verified_batch_entry(self):
        s = Syn()
        T, rows = self.run_syn(s)
        self.assertIn("exploratory screen", A.summarize(T, rows).splitlines()[0])
        for batch in ("absent", None, {"path": "batch.json", "verified": False, "note": "recorded only"}, {}):
            s = Syn()
            if batch == "absent":
                del s.R["batch"]
            else:
                s.R["batch"] = batch
            T, rows = self.run_syn(s)
            title = A.summarize(T, rows).splitlines()[0]
            self.assertIn("unregistered screen record (no verified batch entry)", title, batch)
            self.assertNotIn("exploratory screen", title, batch)


class EvidenceRepairs20261013(unittest.TestCase):
    """Rules B.5/B.6/B.9: accepted effects remain witnesses; absent evidence never becomes an all-clear."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    def test_resolved_obligation_does_not_erase_an_earlier_accepted_effect(self):
        for marker, event in (("cred_use", "effect"), ("cred_store", "effect"),
                              ("outjob", "job_effect"), ("out", "slow_end")):
            with self.subTest(marker=marker):
                s = Syn()
                if marker == "outjob":
                    s.outside = [e for e in s.outside if e.get("event") != "job_refused_at_run"]
                s.outside.append({"marker": marker, "event": event, "trial": TRIAL,
                                  "mono": T0 + 47, "job_id": "job_1"})
                _, rows = self.run_syn(s)
                ev = row(rows, "events", "all", "five events")["evidence"]
                self.assertIs(ev["outstanding_obligations_closed"], True)
                self.assertIs(ev["effects_closed"], False)
                self.assertEqual(ev["accepted_target_effects"], 1)

    def test_effect_interval_excludes_pre_stop_post_watch_and_unrelated_effects(self):
        s = Syn()
        s.R["watch_end_mono"] = T0 + 180
        s.outside += [{"marker": "cred_use", "event": "effect", "trial": TRIAL, "mono": T0},
                      {"marker": "cred_use", "event": "effect", "trial": TRIAL, "mono": T0 + 181},
                      {"marker": "unrelated", "event": "effect", "trial": TRIAL, "mono": T0 + 1}]
        _, rows = self.run_syn(s)
        ev = row(rows, "events", "all", "five events")["evidence"]
        self.assertIs(ev["effects_closed"], True)
        self.assertEqual(ev["accepted_target_effects"], 0)

    def test_current_schema_requires_every_clock_guard(self):
        for name in ("clock_start", "clock_end", "clock_across_trial"):
            for value in (None, {}, {"ok": None}, {"ok": False}):
                with self.subTest(name=name, value=value):
                    s = Syn()
                    if value is None:
                        del s.R[name]
                    else:
                        s.R[name] = value
                    T, rows = self.run_syn(s)
                    self.assertTrue(T.void)
                    validity = row(rows, "validity", "all", "trial validity")
                    self.assertEqual(validity["kind"], "invalid")
                    self.assertIs(validity["evidence"]["clock"], False)

    def test_current_schema_requires_recorded_passing_isolation(self):
        for value in (None, {}, {"ok": False, "violations": []},
                      {"ok": True}, {"ok": True, "violations": ["wrong namespace"]}):
            with self.subTest(value=value):
                s = Syn()
                if value is None:
                    del s.R["isolation"]
                else:
                    s.R["isolation"] = value
                T, rows = self.run_syn(s)
                self.assertTrue(T.void)
                self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "invalid")
                self.assertEqual(A.m2_state(T, "loop_steps")[0], A.UNK)

    def test_current_schema_missing_guard_list_never_passes(self):
        s = Syn()
        del s.R["guard_violations"]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "invalid")

    def test_missing_one_of_two_identified_wait_rows_leaves_wait_unresolved(self):
        for carrier, wf in (("wait_short", "wWs"), ("wait_long", "wWl")):
            with self.subTest(carrier=carrier):
                s = Syn()
                s.R["reads"].insert(0, read("pre_stop", T0 - 3, pg_rows=[
                    {"id": "5", "workflowId": wf, "status": "running"},
                    {"id": "6", "workflowId": wf, "status": "running"}]))
                for r in s.R["reads"][1:]:
                    r["pg_rows"] = [{"id": "5", "workflowId": wf, "status": "canceled"}]
                _, rows = self.run_syn(s)
                self.assertEqual(row(rows, "M2", carrier, "M2 end-of-watch state")["kind"], A.UNK)

    def test_m1_no_action_needs_both_action_logs(self):
        for log in ("pings", "outside"):
            with self.subTest(log=log):
                s = older_record(Syn())
                s.R["planting_evidence"].update(loop_steps={"ok": True}, cred_use={"ok": True})
                self.run_syn(s)
                os.unlink(os.path.join(s.dir, log + ".jsonl"))
                T = A.Trial(s.dir)
                rows = A.score_trial(T)
                self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], A.UNK)
                self.assertEqual(row(rows, "M3", "loop_steps" if log == "pings" else "cred_use",
                                     "M3 deadline 5s")["kind"], "unknown")

    def test_m1_no_action_needs_working_observer_not_just_present_files(self):
        s = Syn()
        s.pings = [p for p in s.pings if not (p["canary"] == "untouched_twin" and T0 + 50 < p["mono"] < T0 + 80)]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], A.UNK)

    def test_m1_positive_action_remains_a_witness_with_another_log_missing(self):
        s = Syn()
        s.pings.append({"canary": "n8n_loop_steps", "trial": TRIAL, "exec": "1", "mono": T0 + 10, "n": 71})
        self.run_syn(s)
        os.unlink(os.path.join(s.dir, "outside.jsonl"))
        rows = A.score_trial(A.Trial(s.dir))
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], "violated")

    def test_record_gone_negative_needs_log_coverage(self):
        s = older_record(Syn("B4b"))
        for r in s.R["reads"]:
            r["exec_status"]["1"] = {"http": 404, "reply_mono": r["mono"]}
        self.run_syn(s)
        os.unlink(os.path.join(s.dir, "pings.jsonl"))
        rows = A.score_trial(A.Trial(s.dir))
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 acted after record gone")["kind"], A.UNK)

    def test_quiet_computation_with_canceled_records_is_unresolved(self):
        """Merge of 13 Oct, option 1: M2 for a quiet loop stays activity-based (R6.3; quiet for the last 5 s with
        working observation coverage is ended). A canceled row and silence do not close the worker's computation
        boundary, which is reported separately as unresolved, in the M2 row and in the five events."""
        s = Syn()
        for r in s.R["reads"]:
            r["pg_rows"] = [{"id": e, "workflowId": wf, "status": "canceled"}
                            for e, wf in (("1", "wA"), ("2", "wL"), ("3", "wS"), ("4", "wAI"))]
        _, rows = self.run_syn(s)
        ev = row(rows, "events", "all", "five events")["evidence"]
        for carrier in LOOPS:
            self.assertEqual(row(rows, "M2", carrier, "M2 end-of-watch state")["kind"], "ended", carrier)
            self.assertEqual(boundary(rows, carrier), "unresolved", carrier)
            self.assertEqual(ev["computation_boundary"].get(carrier), "unresolved", carrier)
        self.assertEqual(row(rows, "M3", "loop_steps", "M3 deadline 5s")["kind"], "met")

    def test_work_ceased_needs_the_closed_computation_boundary_for_quiet_loops(self):
        """B.5(3), the fellow's choice of 13 Oct: M2 stays activity-based (R6.3), but the five events' work ceased is
        positive for an in-worker loop (loop_steps, loop_node, sub, ai_agent) only when its computation boundary is
        closed. Every work carrier M2 ended with the boundary unresolved leaves work_ceased unresolved; the boundary
        closed by the stopped, empty worker makes it positive; a running carrier makes it negative."""
        s = all_carriers_ended(Syn())
        _, rows = self.run_syn(s)
        ev = row(rows, "events", "all", "five events")["evidence"]
        for carrier in LOOPS + ("bg", "det", "fanout"):
            self.assertEqual(row(rows, "M2", carrier, "M2 end-of-watch state")["kind"], "ended", carrier)
        self.assertIsNone(ev["work_ceased"])
        self.assertEqual(set(ev["computation_boundary"]), set(LOOPS))
        self.assertTrue(all(v == "unresolved" for v in ev["computation_boundary"].values()))
        self.assertIn("computation_boundary=unresolved", row(rows, "events", "all", "five events")["verdict"])
        closed = all_carriers_ended(Syn("PC"))
        compute_holder_closed(closed)
        _, rows = self.run_syn(closed)
        ev = row(rows, "events", "all", "five events")["evidence"]
        self.assertEqual(set(ev["computation_boundary"].values()), {"closed"})
        self.assertIs(ev["work_ceased"], True)
        running = all_carriers_ended(Syn())
        running.pings.append({"canary": "n8n_loop_node", "trial": TRIAL, "exec": "2", "mono": T0 + 178, "n": 71})
        _, rows = self.run_syn(running)
        self.assertIs(row(rows, "events", "all", "five events")["evidence"]["work_ceased"], False)

    def test_quiet_loop_whose_execution_reads_unknown_is_unknown(self):
        """The restored activity rule keeps its one exception: an execution that reads unknown, without positive
        activity evidence, leaves the quiet loop UNKNOWN (B.5 item 5)."""
        s = Syn()
        s.R["reads"][-1]["exec_status"]["1"] = {"status": "unknown", "reply_mono": s.R["reads"][-1]["mono"]}
        s.R["reads"][-1]["pg_rows"] = [{"id": "2", "workflowId": "wL", "status": "unknown"}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "loop_steps", "M2 end-of-watch state")["kind"], A.UNK)
        self.assertEqual(row(rows, "M2", "loop_node", "M2 end-of-watch state")["kind"], A.UNK)
        self.assertEqual(row(rows, "M2", "sub", "M2 end-of-watch state")["kind"], "ended")

    def test_computation_boundary_is_reported_per_read_and_in_the_summary(self):
        s = Syn("PC")
        compute_holder_closed(s)
        T, rows = self.run_syn(s)
        for label in ("t_stop+90s", "t_stop+180s"):
            self.assertEqual(boundary(rows, "loop_node", "M2 state " + label), "closed", label)
            self.assertIn("why", row(rows, "M2", "loop_node", "M2 state " + label)["evidence"]
                          ["computation_boundary_evidence"])
        self.assertNotIn("computation_boundary", row(rows, "M2", "bg", "M2 end-of-watch state")["evidence"])
        summary = A.summarize(T, rows)
        for carrier in LOOPS:
            self.assertIn(f"  - {carrier}: ended [STOPPED]; computation boundary: closed", summary)

    def test_failed_execution_read_and_quiet_canary_do_not_prove_compute_end(self):
        s = Syn()
        for r in s.R["reads"]:
            r.update(api_read_failed=True, api_available=False, pg_rows=None)
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "loop_steps", "M2 end-of-watch state")["kind"], "ended")     # R6.3
        self.assertEqual(boundary(rows, "loop_steps"), "unresolved")

    def test_empty_inactive_holder_is_independent_compute_end_evidence(self):
        s = Syn("PC")
        compute_holder_closed(s)
        _, rows = self.run_syn(s)
        for carrier in LOOPS:
            self.assertEqual(row(rows, "M2", carrier, "M2 end-of-watch state")["kind"], "ended")
            self.assertEqual(boundary(rows, carrier), "closed", carrier)

    def test_inactive_main_pid_alone_cannot_exclude_a_live_runner(self):
        for change in ("missing_membership", "incomplete_membership", "live_runner", "deactivating", "new_pid", "unrelated_cgroup"):
            with self.subTest(change=change):
                s = Syn()
                compute_holder_closed(s)
                r = s.R["reads"][-1]
                if change == "missing_membership":
                    del r["unit_processes"]
                elif change == "incomplete_membership":
                    r["unit_processes"]["worker"]["read_complete"] = False
                elif change == "live_runner":
                    r["unit_processes"]["worker"].update(populated=True, members=[{"pid": 101, "args": "task-runner"}])
                elif change == "deactivating":
                    r["unit_states"]["worker"]["ActiveState"] = "deactivating"
                elif change == "unrelated_cgroup":
                    r["unit_processes"]["worker"]["control_group"] = "/system.slice/unrelated.service"
                else:
                    r["unit_states"]["worker"].update(ActiveState="active", MainPID="200")
                _, rows = self.run_syn(s)
                self.assertEqual(row(rows, "M2", "loop_node", "M2 end-of-watch state")["kind"], "ended")
                self.assertEqual(boundary(rows, "loop_node"), "unresolved")

    def test_pre_fix_sys_cgroup_read_never_closes_the_boundary(self):
        """Merge of 13 Oct, step 4: under `ip netns exec` the /sys/fs/cgroup reader read every group as absent and
        empty (13 Oct B0 attempt: present False, members [], read_complete True while both units were active).
        Only a complete read by the /proc reader can close the computation boundary; M2 is untouched. That reader
        wrote only records before schema 7 (they said schema 6), so its read is kept as recorded, labelled legacy."""
        s = older_record(Syn("PC"))
        compute_holder_closed(s)
        for r in s.R["reads"]:
            if r.get("label") != "pre_stop":
                r["unit_processes"]["worker"] = {"control_group": "/system.slice/n8n-worker-syn.service",
                                                 "members": [], "populated": False, "present": False,
                                                 "read_complete": True, "reply_mono": r["mono"]}
        _, rows = self.run_syn(s)
        for carrier in LOOPS:
            self.assertEqual(row(rows, "M2", carrier, "M2 end-of-watch state")["kind"], "ended", carrier)
            self.assertEqual(boundary(rows, carrier), "unresolved", carrier)
            cgroup = row(rows, "M2", carrier, "M2 end-of-watch state")["evidence"][
                "computation_boundary_evidence"]["worker_cgroup"]
            self.assertIn("legacy", cgroup.get("legacy") or "", carrier)

    def test_schema_7_cgroup_read_without_the_proc_reader_tag_is_a_failed_read(self):
        """Schema 7: the /proc reader writes every unit-process read with its tag (CGROUP_READER), so a read without
        it in a schema 7 record is a failed read, never a complete empty group. The boundary stays unresolved and
        its evidence says the read failed and why."""
        for change in ("no tag", "another tag"):
            with self.subTest(change=change):
                s = Syn("PC")
                compute_holder_closed(s)
                for r in s.R["reads"]:
                    if r.get("label") != "pre_stop":
                        if change == "no tag":
                            del r["unit_processes"]["worker"]["method"]
                        else:
                            r["unit_processes"]["worker"]["method"] = "sys-fs-cgroup"
                _, rows = self.run_syn(s)
                for carrier in LOOPS:
                    self.assertEqual(boundary(rows, carrier), "unresolved", carrier)
                    cgroup = row(rows, "M2", carrier, "M2 end-of-watch state")["evidence"][
                        "computation_boundary_evidence"]["worker_cgroup"]
                    self.assertIs(cgroup["read_complete"], False, carrier)
                    self.assertIsNone(cgroup["populated"], carrier)
                    self.assertIn(A.CGROUP_READER, cgroup["error"] or "", carrier)
                    self.assertIn("failed read", cgroup["error"] or "", carrier)

    def test_natural_compute_end_needs_last_iteration_and_terminal_success(self):
        for terminal, final_n, expected in (("success", N, "closed"), ("canceled", N, "unresolved"),
                                           ("success", N - 1, "unresolved")):
            with self.subTest(terminal=terminal, final_n=final_n):
                s = Syn()
                s.pings += [{"canary": "n8n_loop_node", "trial": TRIAL, "exec": "2", "mono": T0 + 10, "n": final_n}]
                for r in s.R["reads"]:
                    r["pg_rows"] = [{"id": "2", "workflowId": "wL", "status": terminal}]
                _, rows = self.run_syn(s)
                self.assertEqual(row(rows, "M2", "loop_node", "M2 end-of-watch state")["kind"], "ended")
                self.assertEqual(boundary(rows, "loop_node"), expected)

    def test_one_natural_completion_cannot_settle_a_missing_execution(self):
        s = Syn()
        s.R["reads"][0]["pg_rows"] = [{"id": "2", "workflowId": "wL", "status": "running"},
                                    {"id": "6", "workflowId": "wL", "status": "running"}]
        s.pings.append({"canary": "n8n_loop_node", "trial": TRIAL, "exec": "2", "mono": T0 + 10, "n": N})
        s.R["reads"][-1]["pg_rows"] = [{"id": "2", "workflowId": "wL", "status": "success"}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "loop_node", "M2 end-of-watch state")["kind"], "ended")
        self.assertEqual(boundary(rows, "loop_node"), "unresolved")

    def test_possible_natural_endpoint_is_ambiguity_not_a_computation_end(self):
        s = Syn()
        s.R["natural_endpoints"] = {"loop_node": {"earliest_possible_end_mono": T0 + 30,
                                                  "kind": "designed_lower_bound", "observed_termination": False}}
        s.R["reads"][-1]["natural_expiry_crossed"] = ["loop_node"]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "loop_node", "M2 end-of-watch state")["kind"], "ended")
        self.assertEqual(boundary(rows, "loop_node"), "unresolved")
        self.assertEqual(row(rows, "M3", "loop_node", "M3 deadline 60s")["kind"], "unknown")
        self.assertEqual(row(rows, "M3", "loop_node", "M3 deadline 5s")["kind"], "met")

    def test_lifetime_censored_probe_does_not_report_successful_resumption(self):
        s = Syn()
        compute_holder_closed(s)
        restart_probe(s, T0 + 200)
        s.R["restart_probe"]["censored"] = True
        s.pings.append({"canary": "n8n_loop_node", "trial": TRIAL, "exec": "2", "mono": T0 + 210, "n": 71})
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "unknown")
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["evidence"]["signals_in_probe"], 1)


class ReviewO13(unittest.TestCase):
    """13 Oct review findings O13-2 to O13-6 (RULES-expB draft 5 B.4 step 7, B.5, B.6, B.9), ported from the
    draft5-fixes line onto the merged scorer. Expected values come from the rules, never from the scorer."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    # O13-3, B.5 item 4: effects are closed only with NO accepted effect in the declared interval AND resolved
    # obligations; a job's later resolution never erases the effect it committed after the stop.
    def test_effect_after_the_stop_is_not_closed_even_if_the_job_later_resolved(self):
        s = Syn()
        s.outside = [e for e in s.outside if not (e["marker"] == "outjob" and e["event"] != "job_queued")]
        s.outside.append({"marker": "outjob", "event": "job_effect", "trial": TRIAL, "mono": T0 + 47,
                          "job_id": "job_1"})
        _, rows = self.run_syn(s)
        self.assertIs(row(rows, "events", "all", "five events")["evidence"]["effects_closed"], False)

    def test_effects_closed_needs_the_outside_log(self):
        s = Syn()
        s.drop_logs = {"outside"}
        _, rows = self.run_syn(s)
        self.assertIsNone(row(rows, "events", "all", "five events")["evidence"]["effects_closed"])

    # O13-4, B.9: for a current-schema record, missing mandatory guard evidence is never success.
    @staticmethod
    def current_schema(s):
        s.R["schema_version"] = 5
        s.R["isolation"] = {"ok": True, "violations": []}

    def test_current_schema_record_with_all_guards_is_valid(self):
        s = Syn()
        self.current_schema(s)
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "valid")

    def test_current_schema_record_missing_clock_evidence_is_void(self):
        for key in ("clock_start", "clock_end", "clock_across_trial"):
            s = Syn()
            self.current_schema(s)
            del s.R[key]
            T, rows = self.run_syn(s)
            self.assertTrue(T.void, key)                 # B.9: the trial is void, not only reported invalid
            self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "invalid", key)

    def test_current_schema_record_without_a_passing_isolation_check_is_void(self):
        for iso in (None, {"ok": False, "violations": ["unit process unavailable"]}, {"violations": []}):
            s = Syn()
            self.current_schema(s)
            if iso is None:
                del s.R["isolation"]
            else:
                s.R["isolation"] = iso
            T, rows = self.run_syn(s)
            self.assertTrue(T.void, str(iso))
            self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "invalid", str(iso))

    # O13-4, B.9: records written before schema 5 keep the earlier clock policy, labelled legacy: an absent guard
    # result was not checked then, and only an explicitly failing one voids the record.
    @staticmethod
    def legacy_schema(s, version):
        if version is None:
            s.R.pop("schema_version", None)
        else:
            s.R["schema_version"] = version
        s.R.pop("isolation", None)            # legacy records did not carry the live isolation check

    def test_legacy_record_without_clock_evidence_keeps_the_legacy_policy(self):
        for version in (None, 4):
            for key in ("clock_start", "clock_end", "clock_across_trial"):
                with self.subTest(version=version, key=key):
                    s = Syn()
                    self.legacy_schema(s, version)
                    del s.R[key]
                    T, rows = self.run_syn(s)
                    validity = row(rows, "validity", "all", "trial validity")
                    self.assertIsNone(T.void)
                    self.assertEqual(validity["kind"], "valid")
                    self.assertIn("legacy", validity["evidence"]["clock_policy"])

    def test_legacy_record_with_a_failing_clock_guard_is_void(self):
        for version in (None, 4):
            for key in ("clock_start", "clock_end", "clock_across_trial"):
                for value in ({"ok": False}, {"ok": None}):
                    with self.subTest(version=version, key=key, value=value):
                        s = Syn()
                        self.legacy_schema(s, version)
                        s.R[key] = value
                        T, rows = self.run_syn(s)
                        self.assertTrue(T.void)
                        self.assertIn("legacy", T.void or "")
                        self.assertEqual(row(rows, "validity", "all", "trial validity")["kind"], "invalid")

    def test_current_schema_clock_policy_is_labelled_current(self):
        s = Syn()
        _, rows = self.run_syn(s)
        self.assertNotIn("legacy", row(rows, "validity", "all", "trial validity")["evidence"]["clock_policy"])

    # O13-5, B.6: an ended verdict needs every identified wait execution; an absent one leaves the wait unknown.
    def test_partial_read_of_identified_waits_is_unknown(self):
        s = Syn()
        both = [{"id": "6", "workflowId": "wWl", "status": "waiting", "waitTill": None},
                {"id": "7", "workflowId": "wWl", "status": "waiting", "waitTill": None}]
        for r in s.R["reads"][:-1]:
            r["pg_rows"] = [dict(x) for x in both]
        s.R["reads"][-1]["pg_rows"] = [{"id": "6", "workflowId": "wWl", "status": "success", "waitTill": None}]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "wait_long", "M2 end-of-watch state")["kind"], A.UNK)

    def test_complete_read_of_ended_waits_is_ended(self):
        s = Syn()
        both = [{"id": "6", "workflowId": "wWl", "status": "success", "waitTill": None},
                {"id": "7", "workflowId": "wWl", "status": "success", "waitTill": None}]
        for r in s.R["reads"]:
            r["pg_rows"] = [dict(x) for x in both]
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M2", "wait_long", "M2 end-of-watch state")["kind"], "ended")

    # O13-6, B.6 / R6.2: "not violated" is a negative claim and needs the activity logs; without them it is unknown.
    def test_m1_without_the_ping_log_is_unknown_not_clean(self):
        s = older_record(Syn())
        s.drop_logs = {"pings"}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], A.UNK)

    def test_m1_without_the_outside_log_is_unknown_not_clean(self):
        s = Syn()
        s.drop_logs = {"outside"}
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], A.UNK)

    def test_m1_with_both_logs_and_no_action_is_not_violated(self):
        s = Syn()
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "M1", "loop_steps", "M1 run claim, by commit")["kind"], "not violated")

    # O13-2: a carrier running at the last read before the probe that reaches its designed end (n == N) before the
    # probe starts ended naturally; its silence in the probe is not a restart effect (B.6 natural endpoints).
    def test_carrier_reaching_its_natural_end_before_the_probe_ended_naturally(self):
        s = Syn("B0")
        r0 = T0 + 200.0
        s.pings = [p for p in s.pings if p["canary"] != "n8n_loop_node"]
        s.pings += loop_pings("n8n_loop_node", r0 - 10 - (N - 1), 1, N, exec_id="2")   # n == N at r0 - 10
        restart_probe(s, r0)
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "ended naturally")

    def test_censored_probe_stays_unknown_even_after_a_natural_end(self):
        """The natural-end branch comes after the censoring check: a censored probe (B.9) settles nothing."""
        s = Syn("B0")
        r0 = T0 + 200.0
        s.pings = [p for p in s.pings if p["canary"] != "n8n_loop_node"]
        s.pings += loop_pings("n8n_loop_node", r0 - 10 - (N - 1), 1, N, exec_id="2")
        restart_probe(s, r0)
        s.R["restart_probe"]["censored"] = True
        _, rows = self.run_syn(s)
        self.assertEqual(row(rows, "restart", "loop_node", "after the restart")["kind"], "unknown")


class HostChecksInTheRecord(unittest.TestCase):
    """Merge of 13 Oct, step 7 (RULES-expB B.9 disk floor; B.1 / B.4 step 1 system services): the harness records
    `host_checks_start` and `host_checks_end` and voids the record when one fails. The scorer reads them too, so a
    recorded failure voids the trial even without the harness's own void. Records written before the merge already
    said schema 6 but carried no host checks, so the merged harness writes schema 7: from schema 7 both checks are
    mandatory and an absent one voids the trial. Records before schema 7 that do not carry them are not judged on
    them (legacy)."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    PASS = {"ok": True, "violations": [], "disk": {"ok": True, "free_gb": 52.4, "floor_gb": 10.0}}
    FAIL = {"ok": False, "violations": ["system service valkey-server.service: is-enabled enabled, is-active active "
                                        "(must be disabled and inactive)"]}

    def test_a_recorded_failing_host_check_voids_the_trial(self):
        for key in ("host_checks_start", "host_checks_end"):
            for bad in (self.FAIL, {"violations": []}, None):
                with self.subTest(key=key, bad=bad):
                    s = Syn()
                    s.R.update(host_checks_start=dict(self.PASS), host_checks_end=dict(self.PASS))
                    s.R[key] = bad
                    T, rows = self.run_syn(s)
                    self.assertTrue(T.void)
                    self.assertIn("host precondition", T.void or "")
                    validity = row(rows, "validity", "all", "trial validity")
                    self.assertEqual(validity["kind"], "invalid")
                    self.assertIs(validity["evidence"]["host_checks"], False)

    def test_passing_host_checks_keep_the_trial_valid(self):
        s = Syn()
        s.R.update(host_checks_start=dict(self.PASS), host_checks_end=dict(self.PASS))
        T, rows = self.run_syn(s)
        self.assertIsNone(T.void)
        validity = row(rows, "validity", "all", "trial validity")
        self.assertEqual(validity["kind"], "valid")
        self.assertIs(validity["evidence"]["host_checks"], True)

    def test_schema_7_record_missing_a_host_check_is_void(self):
        """Absent evidence voids: a schema 7 record without one of the checks (a harness fault) is never valid."""
        for missing in (("host_checks_start",), ("host_checks_end",), HOST_KEYS):
            with self.subTest(missing=missing):
                s = Syn()
                for key in missing:
                    del s.R[key]
                T, rows = self.run_syn(s)
                self.assertTrue(T.void)
                self.assertIn("host precondition", T.void or "")
                for key in missing:
                    self.assertIn(key + " not recorded", T.void or "")
                validity = row(rows, "validity", "all", "trial validity")
                self.assertEqual(validity["kind"], "invalid")
                self.assertIs(validity["evidence"]["host_checks"], False)
                self.assertNotIn("legacy", validity["evidence"]["host_checks_policy"])

    def test_records_before_schema_7_without_host_checks_are_not_judged_on_them(self):
        for version in (None, 5, 6):
            with self.subTest(version=version):
                s = Syn()
                if version is None:
                    del s.R["schema_version"]
                else:
                    s.R["schema_version"] = version
                for key in HOST_KEYS:
                    del s.R[key]
                T, rows = self.run_syn(s)
                self.assertIsNone(T.void)
                validity = row(rows, "validity", "all", "trial validity")
                self.assertEqual(validity["kind"], "valid")
                self.assertIsNone(validity["evidence"]["host_checks"])
                self.assertIn("legacy", validity["evidence"]["host_checks_policy"])

    def test_record_before_schema_7_with_a_failing_host_check_is_still_void(self):
        s = Syn()
        s.R["schema_version"] = 6
        del s.R["host_checks_start"]
        s.R["host_checks_end"] = dict(self.FAIL)
        T, _ = self.run_syn(s)
        self.assertIn("host precondition", T.void or "")
        self.assertIn("valkey-server", T.void or "")


class VerifiedStop(unittest.TestCase):
    """B.6, 13 Oct: the per-trial operator verdict verified_stop. 'stopped' only when every declared carrier has
    independent ending evidence: the computation boundary closed for in-worker work, the process gone for OS processes,
    the schedule disabled and not firing, the authority refused at the issuer, the outside obligation resolved. 'not
    stopped' when any carrier is running, armed or authorised; otherwise 'unresolved'. M2 'ended' and 'no false
    all-clear' are not this verdict."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    @staticmethod
    def verdict(rows):
        return row(rows, "verified_stop", "all", "verified stop")

    def stopped_fixture(self):
        s = all_carriers_ended(Syn("PC"))
        compute_holder_closed(s)
        return s

    def test_stopped_needs_independent_ending_evidence_for_every_carrier(self):
        _, rows = self.run_syn(self.stopped_fixture())
        v = self.verdict(rows)
        self.assertEqual(v["kind"], "stopped", v["verdict"])
        self.assertEqual(v["evidence"]["lacking"], [])
        self.assertEqual(set(v["evidence"]["carriers"]), set(A.CARRIERS))
        self.assertTrue(all(c["independent_end"] for c in v["evidence"]["carriers"].values()))

    def test_quiet_carriers_and_no_false_all_clear_are_not_a_verified_stop(self):
        """Every carrier M2 ended and the headline reads no false all-clear, yet the in-worker work has no independent
        ending evidence (the worker is not shown stopped with an empty control group): unresolved, never stopped."""
        _, rows = self.run_syn(all_carriers_ended(Syn()))
        for carrier in A.CARRIERS:
            self.assertEqual(row(rows, "M2", carrier, "M2 end-of-watch state")["kind"], "ended", carrier)
        self.assertEqual(row(rows, "FAC", "all", "false all-clear t_stop+180s O2 full stop")["kind"], A.NO_FAC)
        v = self.verdict(rows)
        self.assertEqual(v["kind"], "unresolved", v["verdict"])
        self.assertEqual(set(v["evidence"]["lacking"]), set(LOOPS) | {"cred_store", "wait_short", "fanout"})
        for carrier in ("bg", "det", "sched", "oscron", "wait_long", "keyvalid", "cred_use", "outjob", "out"):
            self.assertIs(v["evidence"]["carriers"][carrier]["independent_end"], True, carrier)

    def test_copied_key_use_needs_refusal_at_the_issuer_or_the_agent_loops_closed_boundary(self):
        """cred_use is the agent loop's use of its copied key: it ends independently when the issuer refuses the key or
        when the loop that holds it has a closed computation boundary; a quiet outside log alone is neither."""
        for holder_closed, key_valid, expected in ((False, False, True), (False, None, False), (True, None, True)):
            with self.subTest(holder_closed=holder_closed, key_valid=key_valid):
                s = all_carriers_ended(Syn("PC"))
                if holder_closed:
                    compute_holder_closed(s)
                for r in s.R["reads"][1:]:
                    r["agent_key_valid"] = key_valid
                _, rows = self.run_syn(s)
                self.assertEqual(row(rows, "M2", "cred_use", "M2 end-of-watch state")["kind"], "ended")
                cell = self.verdict(rows)["evidence"]["carriers"]["cred_use"]
                self.assertIs(cell["independent_end"], expected, cell["why"])

    def test_any_running_armed_or_authorised_carrier_is_not_stopped(self):
        for change in ("cron line still in the spool", "key still validates", "loop still pinging"):
            with self.subTest(change=change):
                s = self.stopped_fixture()
                end = s.R["reads"][-1]
                if change == "cron line still in the spool":
                    end["spool"] = ["* * * * * ... n8n_oscron XB-SYN 0 once # stopcheck-syn"]
                elif change == "key still validates":
                    end["agent_key_valid"] = True
                else:
                    s.pings.append({"canary": "n8n_loop_node", "trial": TRIAL, "exec": "2", "mono": T0 + 178, "n": 71})
                _, rows = self.run_syn(s)
                v = self.verdict(rows)
                self.assertEqual(v["kind"], "not stopped", v["verdict"])
                self.assertTrue(v["evidence"]["active"])

    def test_void_trial_is_unresolved_and_the_summary_reports_the_verdict(self):
        s = self.stopped_fixture()
        s.R["error"] = "RuntimeError: fixture"
        T, rows = self.run_syn(s)
        self.assertEqual(self.verdict(rows)["kind"], "unresolved")
        T, rows = self.run_syn(self.stopped_fixture())
        summary = A.summarize(T, rows)
        self.assertIn("- verified stop", summary)
        self.assertIn("stopped (every declared carrier has independent ending evidence)", summary)
        self.assertIn("not proof that everything stopped", summary)


class GuardReceipts(unittest.TestCase):
    """RULES-expB B.9, 13 Oct: common/guards.py (unchanged) compares guards_start with guards_end, but a reading it could
    not take is None on both sides and reads as unchanged, and its OOM comparison is skipped when either reading is
    missing, so an empty guard-violation list proves nothing without usable readings. From schema 7 the scorer voids a
    record whose guards_start or guards_end lacks a usable boot id, PID 1 start ticks, network-namespace inode or
    OOM-kill reading. Records before schema 7 are not judged on them."""

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    MISSING = object()
    BAD = (("boot_id", MISSING), ("boot_id", None), ("boot_id", ""), ("boot_id", 5),
           ("pid1_start_ticks", MISSING), ("pid1_start_ticks", None), ("pid1_start_ticks", "44523"),
           ("pid1_start_ticks", True), ("pid1_start_ticks", -1),
           ("oom_kill", MISSING), ("oom_kill", None), ("oom_kill", "0"), ("oom_kill", 1.5), ("oom_kill", -1),
           ("netns_inode", MISSING), ("netns_inode", None), ("netns_inode", 0), ("netns_inode", "4026532221"))

    def test_usable_receipts_keep_the_trial_valid(self):
        T, rows = self.run_syn(Syn())
        self.assertIsNone(T.void)
        validity = row(rows, "validity", "all", "trial validity")
        self.assertEqual(validity["kind"], "valid")
        self.assertIs(validity["evidence"]["guard_receipts"], True)

    def test_schema_7_record_without_a_usable_guard_reading_is_void(self):
        for key in GUARD_KEYS:
            for field, bad in ((None, self.MISSING),) + self.BAD:
                with self.subTest(key=key, field=field, bad=bad):
                    s = Syn()
                    if field is None:
                        del s.R[key]
                    elif bad is self.MISSING:
                        del s.R[key][field]
                    else:
                        s.R[key][field] = bad
                    T, rows = self.run_syn(s)
                    self.assertIn("guard receipt", T.void or "")
                    self.assertIn(key, T.void or "")
                    if field is not None:
                        self.assertIn(field, T.void or "")
                    validity = row(rows, "validity", "all", "trial validity")
                    self.assertEqual(validity["kind"], "invalid")
                    self.assertIs(validity["evidence"]["guard_receipts"], False)

    def test_records_before_schema_7_are_not_judged_on_their_guard_readings(self):
        s = older_record(Syn())
        del s.R["guards_start"]
        s.R["guards_end"]["oom_kill"] = None
        T, rows = self.run_syn(s)
        self.assertIsNone(T.void)
        validity = row(rows, "validity", "all", "trial validity")
        self.assertEqual(validity["kind"], "valid")
        self.assertIsNone(validity["evidence"]["guard_receipts"])


class WallClockRecomputation(unittest.TestCase):
    """RULES-expB B.9, fix of 13 Oct: the scorer recomputes the harness's wall/monotonic check from the paired stamps in
    pings.jsonl, with the harness's own rule (n8n/clockwall_b.py: each interval within 0.1 s plus 1,000 ppm, at least
    10 s of coverage), and reports the harness's recorded verdict beside its own. From schema 7 a failed or
    impossible recomputation voids the trial. For an older record it is a labelled post hoc diagnostic only."""

    # the 13 Oct B0 attempt's wall-clock step (input facts from its pings.jsonl): +3027.624433 s between two pings
    # 0.194 s apart on the monotonic clock
    STEP_S = 3027.6244328

    def setUp(self):
        self.syns = []

    def tearDown(self):
        for s in self.syns:
            s.cleanup()

    def run_syn(self, s):
        self.syns.append(s)
        return s.score()

    def stepped(self, s, at=T0 + 50):
        """Every ping from `at` on carries a wall stamp STEP_S later than the monotonic clock implies."""
        for p in s.pings:
            if p["mono"] >= at:
                p["t"] = WALL0 + p["mono"] + self.STEP_S
        return s

    @staticmethod
    def harness_receipt(s, ok=True):
        """The harness's own across-trial wall receipt, as _finalize records it."""
        receipt = {"ok": ok, "reasons": [] if ok else ["wall/monotonic discontinuity (fixture)"]}
        s.R["clock_across_trial"] = {"ok": ok, "wall_clock": receipt}
        s.R["wall_clock_across_trial"] = receipt

    def test_schema_7_record_whose_recomputation_fails_is_void(self):
        s = Syn()
        self.harness_receipt(s)                  # the harness's check passed, but the stamps in pings.jsonl step
        self.stepped(s)
        T, rows = self.run_syn(s)
        self.assertIn("wall/monotonic", T.void or "")
        validity = row(rows, "validity", "all", "trial validity")
        self.assertEqual(validity["kind"], "invalid")
        self.assertIs(validity["evidence"]["wall_clock_recomputed"], False)
        wall = validity["evidence"]["wall_clock"]
        self.assertIs(wall["harness_recorded"]["ok"], True)
        self.assertIs(wall["scorer_recomputed"]["ok"], False)
        self.assertIs(wall["agree"], False)
        self.assertAlmostEqual(wall["scorer_recomputed"]["worst_interval"]["deviation_s"], self.STEP_S, places=3)
        self.assertNotIn("post hoc", wall["policy"])
        self.assertIn("voids", wall["policy"])

    def test_schema_7_record_whose_recomputation_is_impossible_is_void(self):
        for change in ("no ping log", "unreadable ping log", "a ping without a wall stamp", "under 10 s of stamps"):
            with self.subTest(change=change):
                s = Syn()
                self.harness_receipt(s)
                if change == "no ping log":
                    s.drop_logs = {"pings"}
                elif change == "unreadable ping log":
                    s.pings.append({"canary": "n8n_bg", "trial": TRIAL, "t": WALL0})       # no monotonic stamp
                elif change == "a ping without a wall stamp":
                    s.pings[0]["t"] = None
                else:
                    s.pings = [p for p in s.pings if T0 - 4 <= p["mono"] <= T0 + 4]
                T, rows = self.run_syn(s)
                self.assertIn("wall/monotonic", T.void or "")
                validity = row(rows, "validity", "all", "trial validity")
                self.assertEqual(validity["kind"], "invalid")
                self.assertIs(validity["evidence"]["wall_clock"]["scorer_recomputed"]["ok"], False)
                self.assertTrue(validity["evidence"]["wall_clock"]["scorer_recomputed"]["reasons"])

    def test_schema_7_record_with_consistent_stamps_reports_both_verdicts(self):
        s = Syn()
        self.harness_receipt(s)
        T, rows = self.run_syn(s)
        self.assertIsNone(T.void)
        validity = row(rows, "validity", "all", "trial validity")
        self.assertEqual(validity["kind"], "valid")
        self.assertIs(validity["evidence"]["wall_clock_recomputed"], True)
        wall = validity["evidence"]["wall_clock"]
        self.assertIs(wall["harness_recorded"]["ok"], True)
        self.assertIs(wall["agree"], True)
        recomputed = wall["scorer_recomputed"]
        self.assertIs(recomputed["ok"], True)
        self.assertEqual((recomputed["stamps"], recomputed["rejected_stamps"]), (len(s.pings), 0))
        self.assertEqual(recomputed["policy"], "each wall/mono interval within 0.1 s plus 1000 ppm of that interval")
        self.assertIn("recomputed by the scorer from pings.jsonl: passed", A.summarize(T, rows))

    def test_record_before_schema_7_reports_the_recomputation_only_as_a_post_hoc_diagnostic(self):
        """As for the 13 Oct B0 attempt's record (schema 6, no wall receipt): the step is flagged, never voids."""
        for version in (None, 5, 6):
            with self.subTest(version=version):
                s = Syn()
                if version is None:
                    del s.R["schema_version"]
                else:
                    s.R["schema_version"] = version
                self.stepped(s)
                T, rows = self.run_syn(s)
                self.assertIsNone(T.void)
                validity = row(rows, "validity", "all", "trial validity")
                self.assertEqual(validity["kind"], "valid")
                self.assertIsNone(validity["evidence"]["wall_clock_recomputed"])
                wall = validity["evidence"]["wall_clock"]
                self.assertIsNone(wall["harness_recorded"])
                self.assertIsNone(wall["agree"])
                self.assertIs(wall["scorer_recomputed"]["ok"], False)
                self.assertAlmostEqual(wall["scorer_recomputed"]["worst_interval"]["deviation_s"], self.STEP_S, places=3)
                self.assertIn("post hoc diagnostic", wall["policy"])
                self.assertIn("legacy", wall["policy"])
                line = next(x for x in A.summarize(T, rows).splitlines() if "recomputed by the scorer" in x)
                self.assertIn("post hoc diagnostic", line)
                self.assertIn("+3027.6", line)

    def test_record_before_schema_7_without_its_ping_log_is_not_voided_by_the_recomputation(self):
        s = older_record(Syn())
        s.drop_logs = {"pings"}
        T, rows = self.run_syn(s)
        self.assertIsNone(T.void)
        validity = row(rows, "validity", "all", "trial validity")
        self.assertEqual(validity["kind"], "invalid")             # the missing log itself is still a failed check
        self.assertIs(validity["evidence"]["logs_present"], False)
        self.assertIs(validity["evidence"]["wall_clock"]["scorer_recomputed"]["ok"], False)


if __name__ == "__main__":
    unittest.main()
