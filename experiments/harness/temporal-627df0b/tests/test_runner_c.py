"""Experiment C runner tests (temporal/run_expC.py): batch admission, reruns, the host profile, the freeze gate and the
export of every attempt (RULES-expC.md C.9, C.10, C.12; pre-freeze batch of 13 Oct, items 4, 6 and 7). Offline only:
trials are replaced by a fake launcher, and no test starts a server, a worker, a client or a listening socket.

Run:  python3 -B -m unittest tests.test_runner_c      (STOPCHECK_RUN_EXPC=<path> tests another copy of the runner, as
temporal/mutate_c.py does)
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from tests.test_analyze_c import Syn, current_record, sigkill, work_run, T0   # plain fixtures, no TestCase

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.environ.get("STOPCHECK_RUN_EXPC", os.path.join(os.path.dirname(HERE), "temporal", "run_expC.py"))
_spec = importlib.util.spec_from_file_location("run_expC_runner_under_test", RUNNER)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def quiet(function, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args)


def write_raw(results, trial, syn=None, record=True, extra=None):
    """A raw trial folder as harness_c.main names it (<trial>-%Y%m%d-%H%M%S), with the scorer's inputs."""
    folder = Path(results) / f"{trial}-20261014-120000"
    folder.mkdir(parents=True)
    (folder / "server-g1.log").write_text('{"msg": "started"}\n', encoding="utf-8")
    if record:
        s = current_record(syn or Syn())      # a current-schema record, as harness_c writes one (items 5 and 6)
        R_ = dict(s.R, trial=trial, run_kind="pilot")
        R_.update(extra or {})
        (folder / "record.json").write_text(json.dumps(R_), encoding="utf-8")
        (folder / "receiver.jsonl").write_text("".join(json.dumps(x) + "\n" for x in s.lines), encoding="utf-8")
        (folder / "worker-target-g1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in s.logs),
                                                       encoding="utf-8")
    return folder


def pc_syn():
    s = Syn("PC", "NH", "kill_worker")
    s.R["intervention"]["actions"] = [sigkill()]
    s.R["intervention"]["worker_dead"] = [{"role": "target", "t_from": T0 + 0.01, "t_to": None}]
    s.lines = [x for x in s.lines if not (x.get("role") == "target" and x.get("activity_id") == "work")]
    s.lines += work_run(T0 - 8.5, 9, final=False)
    s.R["capability"] = [{"queue": "qW", "ok": True}]
    return s


class BatchFixture(unittest.TestCase):
    """An isolated results folder, stubbed checks and a fake launcher; holds no tests of its own."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.results = self.folder / "results"
        self.results.mkdir()
        self.freeze = self.folder / "FREEZE-expC.json"
        self.freeze.write_text("{}\n")
        self.hashes = {"file": "h"}
        self.outcomes = {}      # condition -> exit codes, consumed per launch (default 0)
        self.invalid = set()    # trial ids the scorer calls invalid
        self.launches = []
        patches = [patch.object(R, "RESULTS", self.results), patch.object(R, "offline_checks"),
                   patch.object(R, "require_clock"), patch.object(R, "verify_freeze", return_value={}),
                   patch.object(R, "source_hashes", side_effect=lambda: dict(self.hashes)),
                   patch.object(R.os, "geteuid", return_value=0, create=True),
                   patch.object(R, "trial_validity", side_effect=lambda trial, host_profile=None: {
                       "record": None, "scored_valid": trial not in self.invalid, "validity": "fixture"}),
                   patch.object(R, "run_trial", side_effect=self.fake_launch)]
        if hasattr(R, "_gce_metadata"):      # no fixture may reach the GCE metadata server
            patches.append(patch.object(R, "_gce_metadata", side_effect=AssertionError("must not ask")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(R.os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        R.os.environ.pop("STOPCHECK_HOST_PROFILE", None)

    def fake_launch(self, cmd, env, cwd, grace_s=None):
        condition, trial = cmd[-2], cmd[-1]
        batch = json.loads(Path(env["STOPCHECK_BATCH"]).read_text(encoding="utf-8")) if env.get("STOPCHECK_BATCH") \
            else None
        self.launches.append({"condition": condition, "trial": trial, "env": dict(env), "batch": batch})
        codes = self.outcomes.get(condition) or [0]
        return codes.pop(0) if len(codes) > 1 else codes[0]

    def batch_path(self, kind, batch_id):
        return self.results / f"XC-{kind}-{batch_id}" / "batch.json"

    def batch(self, kind, batch_id):
        return json.loads(self.batch_path(kind, batch_id).read_text(encoding="utf-8"))

    def run_main(self, *argv):
        return quiet(R.main, list(argv))

    def resume_until_done(self, kind, batch_id, limit=6):
        flags = ["--freeze", str(self.freeze)] if kind == "screen" else (["--smoke"] if kind == "smoke" else
                                                                          ["--pilot"])
        for _ in range(limit):
            if self.batch(kind, batch_id).get("completed"):
                return
            self.run_main(*flags, "--resume", str(self.batch_path(kind, batch_id)))
        self.fail("batch never completed")


# ============================ item 4: the scorer's validity admits each attempt ============================
class Admission(BatchFixture):
    """C.9, C.10 (as B.10): an exit status alone never accepts a trial; the scorer's validity does. Every attempt is
    kept, and an invalid trial is rerun once."""

    def test_exit_status_without_the_scorers_verdict_is_not_acceptance(self):
        self.assertIs(R.accepted({"trial": "x", "status": "finished", "exit_code": 0}), False)
        self.assertIs(R.accepted({"trial": "x", "status": "finished", "exit_code": 0, "accepted": True}), True)

    def test_a_trial_the_scorer_calls_invalid_stops_the_batch_and_is_rerun_once(self):
        self.invalid.add("XC-pilot-t1-C0-HB")
        self.assertNotEqual(self.run_main("--pilot", "--conditions", "C0-HB", "PC", "--batch-id", "t1"), 0)
        self.assertEqual([x["condition"] for x in self.launches], ["C0-HB"])
        first = self.batch("pilot", "t1")["trials"][0]
        self.assertEqual((first["exit_code"], first["scored_valid"], first["accepted"]), (0, False, False))
        self.assertFalse(self.batch("pilot", "t1")["completed"])
        self.resume_until_done("pilot", "t1")
        self.assertEqual([x["trial"] for x in self.launches],
                         ["XC-pilot-t1-C0-HB", "XC-pilot-t1-C0-HB-a2", "XC-pilot-t1-PC"])
        trials = self.batch("pilot", "t1")["trials"]
        self.assertEqual([(t["attempt"], t["accepted"]) for t in trials], [(1, False), (2, True), (1, True)])
        self.assertEqual(trials[1]["rerun_of"], "XC-pilot-t1-C0-HB")

    def test_a_condition_whose_trial_and_rerun_both_fail_is_incomplete(self):
        self.outcomes["PC"] = [3]
        self.assertNotEqual(self.run_main("--pilot", "--conditions", "PC", "C0-HB", "--batch-id", "t2"), 0)
        self.resume_until_done("pilot", "t2")
        batch = self.batch("pilot", "t2")
        self.assertEqual([t["trial"] for t in batch["trials"] if t["condition"] == "PC"],
                         ["XC-pilot-t2-PC", "XC-pilot-t2-PC-a2"])
        self.assertEqual(batch["incomplete"], ["PC"])
        self.assertEqual(len({t["trial"] for t in batch["trials"]}), len(batch["trials"]))   # every attempt kept
        with self.assertRaisesRegex(ValueError, "complete"):
            self.run_main("--pilot", "--resume", str(self.batch_path("pilot", "t2")))

    def test_a_non_zero_exit_is_never_accepted_even_if_the_record_scores_valid(self):
        for code in (2, 3):
            with self.subTest(code=code):
                self.outcomes["C0-HB"] = [code]
                batch_id = f"t3x{code}"
                self.run_main("--pilot", "--conditions", "C0-HB", "--batch-id", batch_id)
                entry = self.batch("pilot", batch_id)["trials"][0]
                self.assertEqual((entry["exit_code"], entry["scored_valid"], entry["accepted"]), (code, True, False))

    def test_an_interrupted_trial_is_recorded_and_never_accepted(self):
        def interrupted(cmd, env, cwd, grace_s=None):
            raise KeyboardInterrupt
        with patch.object(R, "run_trial", side_effect=interrupted):
            self.assertEqual(self.run_main("--pilot", "--conditions", "C0-HB", "--batch-id", "t4"), 130)
        entry = self.batch("pilot", "t4")["trials"][0]
        self.assertEqual((entry["status"], entry["interrupted"], entry["accepted"]), ("interrupted", True, False))
        self.assertIn("interrupted", self.batch("pilot", "t4")["stopped_reason"])

    def test_resume_keeps_the_recorded_order_and_refuses_changed_code(self):
        self.outcomes["C0-HB"] = [3]
        self.run_main("--pilot", "--conditions", "PC", "C0-HB", "C0-NH", "--batch-id", "t5")
        path = str(self.batch_path("pilot", "t5"))
        for extra in (["--conditions", "C0-HB"], ["--seed", "1"], ["--batch-id", "x"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.run_main("--pilot", "--resume", path, *extra)
        self.hashes = {"file": "changed"}
        with self.assertRaisesRegex(ValueError, "[Ss]ource"):
            self.run_main("--pilot", "--resume", path)
        self.hashes = {"file": "h"}
        self.resume_until_done("pilot", "t5")
        self.assertEqual([x["condition"] for x in self.launches], ["PC", "C0-HB", "C0-HB", "C0-NH"])

    def test_every_trial_id_an_accepted_batch_id_can_create_passes_the_harness(self):
        harness_rule = r"[A-Za-z0-9][A-Za-z0-9-]{0,40}"           # harness_c.main
        accepted = 0
        for length in range(1, 40):
            try:
                R.check_batch_id("screen", "a" * length, list(R.CONDITIONS))
            except ValueError:
                continue
            accepted += 1
            for condition in R.CONDITIONS:
                for attempt in (1, 2):
                    self.assertRegex(R.trial_id("screen", "a" * length, condition, attempt), "^" + harness_rule + "$")
        self.assertGreater(accepted, 0)
        for bad in ("pilot_1", "-night", "a" * 40):
            with self.assertRaisesRegex(ValueError, "[Bb]atch id"):
                R.check_batch_id("pilot", bad, ["C0-HB"])

    def test_each_trial_is_told_where_the_predictions_are(self):
        # review, major 2: the harness records the predictions' SHA-256 at the start of every trial
        self.run_main("--pilot", "--conditions", "C0-HB", "--batch-id", "p1")
        self.assertEqual(self.launches[0]["env"].get("STOPCHECK_C_PREDICTIONS"),
                         str((R.ROOT / "predictions" / "temporal-expC-cells.json").resolve()))

    def test_a_screen_checks_its_seed_and_order_against_the_freeze(self):
        # review, minor (C.10): before the batch and before each trial
        self.assertEqual(self.run_main("--freeze", str(self.freeze), "--conditions", "C2", "C3", "--seed", "5",
                                       "--batch-id", "s1"), 0)
        order = self.batch("screen", "s1")["conditions"]
        calls = R.verify_freeze.call_args_list
        self.assertGreaterEqual(len(calls), 3)
        for call in calls:
            self.assertEqual((call.kwargs.get("seed"), call.kwargs.get("order")), (5, order))

    def test_the_rerun_has_its_own_raw_folder_pattern(self):
        write_raw(self.results, "XC-pilot-t6-C2")
        write_raw(self.results, "XC-pilot-t6-C2-a2")
        write_raw(self.results, "XC-pilot-t6-C2w")
        self.assertEqual([p.name for p in R.raw_folders("XC-pilot-t6-C2")], ["XC-pilot-t6-C2-20261014-120000"])
        self.assertEqual([p.name for p in R.raw_folders("XC-pilot-t6-C2-a2")], ["XC-pilot-t6-C2-a2-20261014-120000"])


# ============================ item 6: the declared host profile and each worker's identity ============================
METADATA_FIXTURE = {"id": "4242424242", "name": "stopcheck-w1", "zone": "projects/12/zones/europe-west1-b",
                    "machine-type": "projects/12/machineTypes/n2-standard-4", "cpu-platform": "Intel Cascade Lake",
                    "image": "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20261001",
                    "scheduling/preemptible": "FALSE", "scheduling/on-host-maintenance": "MIGRATE"}


class WorkerIdentity(unittest.TestCase):
    """C.0 (as B.0): on linux-vm the runner reads each worker's identity from the GCE metadata server, outside the
    trial's network namespace, before each trial. A missing identity is recorded as missing and never refuses the
    trial. Fixtures only: no request leaves the process."""

    def test_the_declared_host_profile_is_read_from_the_environment_and_never_inferred(self):
        self.assertEqual(R.declared_host_profile({}), "wsl")
        self.assertEqual(R.declared_host_profile({"STOPCHECK_HOST_PROFILE": "linux-vm"}), "linux-vm")
        for bad in ("gce", "", "WSL"):
            with self.assertRaisesRegex(ValueError, "STOPCHECK_HOST_PROFILE"):
                R.declared_host_profile({"STOPCHECK_HOST_PROFILE": bad})

    def test_linux_vm_identity_comes_from_the_gce_metadata_server(self):
        identity = R.worker_identity("linux-vm", fetch=METADATA_FIXTURE.__getitem__)
        self.assertEqual((identity["status"], identity["host_profile"]), ("recorded", "linux-vm"))
        self.assertEqual(set(identity["values"]), {"id", "name", "zone", "machine_type", "cpu_platform", "image",
                                                   "preemptible", "on_host_maintenance"})
        self.assertEqual((identity["missing"], identity["errors"]), ([], {}))

    def test_a_partial_or_missing_identity_is_recorded_never_raised(self):
        def flaky(path):
            if path == "cpu-platform":
                raise OSError("timed out")
            return METADATA_FIXTURE[path]
        partial = R.worker_identity("linux-vm", fetch=flaky)
        self.assertEqual((partial["status"], partial["missing"]), ("partial", ["cpu_platform"]))

        def down(path):
            raise OSError("no route")
        missing = R.worker_identity("linux-vm", fetch=down)
        self.assertEqual((missing["status"], missing["values"]), ("missing", {}))

    def test_wsl_has_no_identity_and_never_asks(self):
        def never(path):
            raise AssertionError("asked")
        self.assertEqual(R.worker_identity("wsl", fetch=never), {"status": "not applicable", "host_profile": "wsl"})

    def test_the_metadata_read_sends_its_header_bypasses_proxies_and_needs_a_google_reply(self):
        import urllib.request
        seen, handlers = {}, []

        class Reply:
            def __init__(self, flavor):
                self.headers = {"Metadata-Flavor": flavor} if flavor else {}

            def read(self, size=-1):
                return b"projects/12/machineTypes/n2-standard-4\n"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Opener:
            def __init__(self, flavor):
                self.flavor = flavor

            def open(self, request, timeout=None):
                seen.update(url=request.full_url, header=request.get_header("Metadata-flavor"), timeout=timeout)
                return Reply(self.flavor)

        def build(*given):
            handlers.extend(given)
            return Opener("Google")
        with patch.object(urllib.request, "build_opener", side_effect=build):
            self.assertEqual(R._gce_metadata("machine-type"), "projects/12/machineTypes/n2-standard-4")
        self.assertEqual(seen["url"], "http://metadata.google.internal/computeMetadata/v1/instance/machine-type")
        self.assertEqual(seen["header"], "Google")
        self.assertTrue(seen["timeout"] and seen["timeout"] <= 5)
        self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers))
        with patch.object(urllib.request, "build_opener", return_value=Opener(None)), \
                self.assertRaisesRegex(ValueError, "Metadata-Flavor"):
            R._gce_metadata("machine-type")


class HostProfileBatches(BatchFixture):
    """A batch binds its declared host profile; each trial's worker identity is read before it starts."""

    def test_batch_records_its_host_profile_and_each_trials_worker_identity(self):
        asked = []

        def metadata(path, timeout=2.0):
            asked.append(path)
            return METADATA_FIXTURE[path]
        with patch.dict(R.os.environ, {"STOPCHECK_HOST_PROFILE": "linux-vm"}), \
                patch.object(R, "_gce_metadata", side_effect=metadata):
            self.assertEqual(self.run_main("--pilot", "--conditions", "C0-HB", "PC", "--batch-id", "h1"), 0)
        batch = self.batch("pilot", "h1")
        self.assertEqual(batch.get("host_profile"), "linux-vm")
        for entry, launch in zip(batch["trials"], self.launches):
            identity = entry.get("host_identity") or {}
            self.assertEqual(identity.get("status"), "recorded")
            self.assertEqual((identity.get("values") or {}).get("name"), "stopcheck-w1")
            pending = [t for t in launch["batch"]["trials"] if t["trial"] == launch["trial"]]
            self.assertEqual(pending[0].get("host_identity"), identity)              # what the harness copies
            self.assertEqual(launch["env"]["STOPCHECK_HOST_PROFILE"], "linux-vm")
        self.assertEqual(len(asked), 2 * len(METADATA_FIXTURE))                     # read again for every trial

    def test_a_missing_worker_identity_is_recorded_as_missing_and_never_refuses(self):
        def unreachable(path, timeout=2.0):
            raise OSError("no route to metadata.google.internal")
        with patch.dict(R.os.environ, {"STOPCHECK_HOST_PROFILE": "linux-vm"}), \
                patch.object(R, "_gce_metadata", side_effect=unreachable):
            self.assertEqual(self.run_main("--pilot", "--conditions", "C0-HB", "--batch-id", "h2"), 0)
        entry = self.batch("pilot", "h2")["trials"][0]
        self.assertEqual((entry["host_identity"]["status"], entry["accepted"]), ("missing", True))

    def test_wsl_batches_record_no_identity_and_never_ask_the_metadata_server(self):
        self.assertEqual(self.run_main("--pilot", "--conditions", "C0-HB", "--batch-id", "h3"), 0)
        batch = self.batch("pilot", "h3")
        self.assertEqual(batch["host_profile"], "wsl")
        self.assertEqual(batch["trials"][0]["host_identity"], {"status": "not applicable", "host_profile": "wsl"})

    def test_an_undeclared_profile_is_refused_before_any_batch(self):
        with patch.dict(R.os.environ, {"STOPCHECK_HOST_PROFILE": "gce"}), \
                self.assertRaisesRegex(ValueError, "STOPCHECK_HOST_PROFILE"):
            self.run_main("--pilot", "--conditions", "C0-HB", "--batch-id", "h4")
        self.assertEqual(self.launches, [])
        self.assertFalse((self.results / "XC-pilot-h4").exists())

    def test_resume_refuses_another_host_profile_and_acceptance_uses_the_batchs(self):
        seen = []

        def validity(trial, host_profile=None):
            seen.append(host_profile)
            return {"record": None, "scored_valid": trial not in self.invalid, "validity": "fixture"}
        self.outcomes["C0-HB"] = [3]
        with patch.object(R, "trial_validity", side_effect=validity):
            self.run_main("--pilot", "--conditions", "C0-HB", "PC", "--batch-id", "h5")
        self.assertEqual(seen, ["wsl"])
        with patch.dict(R.os.environ, {"STOPCHECK_HOST_PROFILE": "linux-vm"}), \
                self.assertRaisesRegex(ValueError, "host profile"):
            self.run_main("--pilot", "--resume", str(self.batch_path("pilot", "h5")))
        self.assertEqual(len(self.launches), 1)


class RealScorerAdmission(unittest.TestCase):
    """trial_validity applies analyze_c's validity row to the attempt's own raw record, and PC's gate (C.9)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.results = Path(self.temp.name)
        p = patch.object(R, "RESULTS", self.results)
        p.start()
        self.addCleanup(p.stop)

    def test_the_scorers_validity_decides(self):
        write_raw(self.results, "XC-pilot-v1-C3", Syn())
        self.assertIs(R.trial_validity("XC-pilot-v1-C3")["scored_valid"], True)
        s = Syn()
        s.R["capability"][1]["ok"] = False          # item 1: a failed end-of-watch capability job invalidates
        write_raw(self.results, "XC-pilot-v2-C3", s)
        out = R.trial_validity("XC-pilot-v2-C3")
        self.assertIs(out["scored_valid"], False)
        self.assertIn("readiness and capability jobs", out["validity"])

    def test_a_record_below_the_current_schema_is_never_admitted(self):
        # item 5: from schema 2 the wall/monotonic receipt and its recomputation are mandatory; an older or downgraded
        # record cannot use the legacy path to skip them
        for schema in (1, None, "2"):
            trial = f"XC-pilot-s{schema}-C3".replace("None", "n").replace("'", "")
            write_raw(self.results, trial, Syn(), extra={"schema_version": schema})
            out = R.trial_validity(trial)
            self.assertIs(out["scored_valid"], False, schema)
            self.assertIn("schema", out["validity"], schema)

    def test_a_record_from_another_host_profile_is_never_admitted(self):
        write_raw(self.results, "XC-pilot-hp-C3", Syn(),
                  extra={"host_profile": {"declared": "linux-vm", "variable": "STOPCHECK_HOST_PROFILE"}})
        self.assertIs(R.trial_validity("XC-pilot-hp-C3", "linux-vm")["scored_valid"], True)
        out = R.trial_validity("XC-pilot-hp-C3", "wsl")
        self.assertIs(out["scored_valid"], False)
        self.assertIn("host profile", out["validity"])

    def test_no_record_or_two_records_is_never_valid(self):
        write_raw(self.results, "XC-pilot-v3-C3", record=False)
        self.assertIs(R.trial_validity("XC-pilot-v3-C3")["scored_valid"], False)
        self.assertIs(R.trial_validity("XC-pilot-v4-C3")["scored_valid"], False)

    def test_pc_needs_its_gate_to_pass(self):
        write_raw(self.results, "XC-pilot-v5-PC", pc_syn())
        out = R.trial_validity("XC-pilot-v5-PC")
        self.assertEqual((out["scored_valid"], out["pc_gate"]), (True, "pass"))
        s = pc_syn()
        s.lines += work_run(T0 + 1.5, 1, nonce="n1", final=False)     # an effect initiated after the kill
        write_raw(self.results, "XC-pilot-v6-PC", s)
        out = R.trial_validity("XC-pilot-v6-PC")
        self.assertEqual((out["scored_valid"], out["pc_gate"]), (False, "fail"))


# ============================ item 7: the freeze gate and the export of every attempt ============================
# the probe restarts the server and both workers, each with a new identity (C.1; pre-freeze review)
RESTARTED = [{"name": n, "pid": 5000 + i, "replaces": {"pid": 4000 + i, "start_ticks": 1, "new_identity": True}}
             for i, n in enumerate(("server", "worker-target", "worker-twin"))]


def pilot_record(condition, hashes, profile="wsl", predictions="pred"):
    """A full-length pilot record that shows what C.10 requires of it (pilot_problems)."""
    return {"condition": condition, "trial": f"XC-pilot-f-{condition}", "fast_mode": False, "run_kind": "pilot",
            "schema_version": 2, "source_hashes": dict(hashes),
            "host_profile": {"declared": profile, "variable": "STOPCHECK_HOST_PROFILE"},
            "predictions_at_start": {"sha256": predictions, "exists": True},
            "clock_start": {"ok": True}, "clock_end": {"ok": True},
            "clock_across_trial": {"ok": True, "wall_clock": {"ok": True}},
            "pre_check": {"ok": True, "completed_before_deadline": True},
            "launches": json.loads(json.dumps(RESTARTED)),                     # a deep copy per record
            "probe": {"pre_check": {"ok": True}, "restarts": [{"ok": True}, {"ok": True}]},
            "intervention": {"precondition": {"ok": True}} if condition == "C6" else {}}


def smoke_record(condition, hashes, profile="wsl", predictions="pred"):
    """A FAST smoke of one procedure on the freeze's source (C.10, pre-freeze review): its procedure ran end to end."""
    action = {"C1": "cancel", "C2w": "cancel", "C5": "terminate"}.get(condition)
    actions = ([{"name": action, "rc": 0, "timed_out": False, "t_send": 1.0, "t_return": 1.2}] if action else
               [{"name": "sigkill_worker", "rc": 0, "t_send": 1.0, "t_return": 1.0, "exit_mono": 1.01}] +
               ([{"name": "cancel", "rc": 0, "timed_out": False, "t_send": 2.5, "t_return": 2.6}]
                if condition == "C8" else []))
    restarts = [{"what": "worker:target", "ok": True, "t_start": 9.0, "t_ready": 10.0}] if condition in ("C7", "C8") \
        else []
    return {"condition": condition, "trial": f"XC-smoke-f-{condition}", "fast_mode": True, "run_kind": "smoke",
            "schema_version": 2, "source_hashes": dict(hashes),
            "host_profile": {"declared": profile, "variable": "STOPCHECK_HOST_PROFILE"},
            "predictions_at_start": {"sha256": predictions, "exists": True},
            "intervention": {"t_stop": {"mono": 1.0}, "actions": actions, "restarts": restarts},
            "probe": {"pre_check": {"ok": True}, "restarts": [{"ok": True}, {"ok": True}]}}


class FakeScorer:
    """The scorer's rows for the validation tests: validity, PC's gate and probe outcome, and C2's and C3's
    pending-on-closed row (C.10: one captured example)."""

    def __init__(self, invalid=(), pc_gate="pass", probe="re-run, closed after restart: COMPLETED",
                 pending=(("C2", "yes"), ("C3", "no"))):
        self.invalid, self.pc_gate, self.probe, self.loaded = set(invalid), pc_gate, probe, []
        self.pending = dict(pending)

    def load(self, folder):
        self.loaded.append(Path(folder).name)
        return json.loads((Path(folder) / "record.json").read_text(encoding="utf-8"))

    def score_trial(self, record):
        bad = record["condition"] in self.invalid
        rows = [{"measure": "validity", "outcome": "trial validity", "kind": "invalid" if bad else "valid",
                 "verdict": "INVALID: twin control" if bad else "valid"}]
        if record["condition"] == "PC":
            rows += [{"measure": "PC", "outcome": "no effect after the kill (gate)", "kind": self.pc_gate},
                     {"measure": "restart", "outcome": "durability probe", "kind": self.probe}]
        if record["condition"] in ("C2", "C3"):
            rows.append({"measure": "pending_on_closed", "outcome": "a closed run lists a pending activity",
                         "kind": self.pending.get(record["condition"], "UNKNOWN")})
        return rows


class FreezeGate(unittest.TestCase):
    """C.10, C.12 (item 7): the freeze needs one valid full-length pilot of every condition of C.10's pilot set (the
    no-stop references, PC, C2, C3 and C6), on the final source and the declared host profile, each valid by the scorer
    and showing what C.10 requires; it binds the source fingerprints, the predictions and the host profile."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.hashes = {"file": "h"}

    def folders(self, conditions=R.PILOT_SET, change=None, target="PC", profile="wsl", smokes=R.SMOKE_SET):
        out = []
        base = Path(tempfile.mkdtemp(dir=self.folder))          # a fresh set of folders for every call
        for kind, names in (("pilot", conditions), ("smoke", smokes)):
            for condition in names:
                folder = base / f"{kind}-{condition}-{len(out)}"
                folder.mkdir()
                record = (pilot_record if kind == "pilot" else smoke_record)(condition, self.hashes, profile)
                if change and (condition == target if kind == "pilot" else f"smoke {condition}" == target):
                    change(record)
                (folder / "record.json").write_text(json.dumps(record), encoding="utf-8")
                out.append(folder)
        return out

    def validate(self, folders, scorer=None, profile="wsl", predictions="pred", exposed=()):
        with patch.object(R, "_scorer", return_value=scorer or FakeScorer()):
            return R.validate_pilots(folders, self.hashes, profile, predictions, list(exposed))

    def test_the_full_pilot_set_passes_and_each_receipt_binds_its_record(self):
        scorer = FakeScorer()
        receipts = [r for r in self.validate(self.folders(), scorer) if r["kind"] == "pilot"]
        self.assertEqual([r["condition"] for r in receipts], list(R.PILOT_SET))
        self.assertEqual(len(scorer.loaded), len(R.PILOT_SET))          # every pilot is scored, not only PC
        for receipt in receipts:
            path = Path(receipt["folder"]) / "record.json"
            self.assertEqual(receipt["record_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(receipt["host_profile"], "wsl")
        self.assertEqual(set(R.PILOT_SET), {"C0-HB", "C0-NH", "C0-LA", "PC", "C2", "C3", "C6"})   # C.10

    def test_the_freeze_requires_every_pilot_of_the_set(self):
        for missing in R.PILOT_SET:
            with self.subTest(missing=missing):
                folders = self.folders([c for c in R.PILOT_SET if c != missing])
                with self.assertRaisesRegex(ValueError, f"missing.*{missing}"):
                    self.validate(folders, FakeScorer(pending=(("C2", "yes"), ("C3", "yes"))))
                for f in folders:
                    for p in f.iterdir():
                        p.unlink()
                    f.rmdir()

    # ---- pre-freeze review ----
    def test_each_validation_run_has_its_predictions_fixed_before_it_or_disclosed(self):
        # review, major 2 (C.8, C.10): the predictions' SHA-256 recorded when a pilot or smoke started must equal the
        # frozen file's; otherwise the predictions must name that run (trial id and record SHA-256) as prior exposure
        receipts = self.validate(self.folders())
        self.assertEqual({r["predictions"] for r in receipts}, {"fixed before this run"})
        folders = self.folders(change=lambda r: r.update(predictions_at_start={"sha256": "draft", "exists": True}),
                               target="C2")
        with self.assertRaisesRegex(ValueError, "predictions"):
            self.validate(folders)
        record = next(f / "record.json" for f in folders if json.loads((f / "record.json").read_text())["condition"]
                      == "C2" and "pilot" in f.name)
        disclosed = {"trial": "XC-pilot-f-C2", "record_sha256": hashlib.sha256(record.read_bytes()).hexdigest()}
        receipts = self.validate(folders, exposed=[disclosed])
        self.assertEqual(next(r for r in receipts if r["condition"] == "C2" and r["kind"] == "pilot")["predictions"],
                         "disclosed as prior exposure")
        with self.assertRaisesRegex(ValueError, "predictions"):            # a missing file is never "fixed before"
            self.validate(self.folders(change=lambda r: r.update(predictions_at_start={"sha256": None,
                                                                                      "exists": False}), target="C6"))

    def test_a_closed_runs_pending_list_must_be_captured_by_c2_or_c3(self):
        # review, minor (C.10): one closed run's pending-activity list, from C2 or C3, reads yes or no
        self.validate(self.folders(), FakeScorer(pending=(("C2", "UNKNOWN"), ("C3", "yes"))))
        with self.assertRaisesRegex(ValueError, "pending"):
            self.validate(self.folders(), FakeScorer(pending=(("C2", "UNKNOWN"), ("C3", "not applicable"))))

    def test_every_procedure_needs_a_fast_smoke_on_the_freezes_source(self):
        # review, minor (C.10): terminate, a cancel in the timer, WAIT_CANCELLATION_COMPLETED, a worker kill with a
        # restart and kill-cancel-restart run live before the freeze, as FAST smokes on its source and host profile
        self.assertEqual(R.SMOKE_SET, ("C1", "C2w", "C5", "C7", "C8"))
        for missing in R.SMOKE_SET:
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, f"smoke.*{missing}"):
                self.validate(self.folders(smokes=[c for c in R.SMOKE_SET if c != missing]))
        for change, word in ((lambda r: r.update(error="RuntimeError: boom"), "error"),
                             (lambda r: r["intervention"]["actions"][0].update(rc=1), "action"),
                             (lambda r: r.update(source_hashes={"file": "old"}), "source"),
                             (lambda r: r.update(host_profile={"declared": "linux-vm"}), "host profile"),
                             (lambda r: r.update(fast_mode=False), "FAST")):
            with self.subTest(word=word), self.assertRaisesRegex(ValueError, word):
                self.validate(self.folders(change=change, target="smoke C5"))
        with self.assertRaisesRegex(ValueError, "restart"):
            self.validate(self.folders(change=lambda r: r["intervention"]["restarts"][0].update(ok=False),
                                       target="smoke C7"))

    def test_restarts_need_new_identities(self):
        # review, minor (C.1, C.10): every restarted process got a new PID and start time, and the probe's restarts
        # of the server and both workers are on record
        good = pilot_record("C2", self.hashes)
        self.assertEqual(R.pilot_problems(good, self.hashes), [])
        stale = pilot_record("C2", self.hashes)
        stale["launches"][1]["replaces"]["new_identity"] = False
        self.assertTrue(any("new PID" in p for p in R.pilot_problems(stale, self.hashes)))
        partial = pilot_record("C2", self.hashes)
        partial["launches"] = partial["launches"][:2]
        self.assertTrue(any("worker-twin" in p for p in R.pilot_problems(partial, self.hashes)))

    def test_the_freeze_binds_the_screen_seed_and_order(self):
        # review, minor (C.10): the manifest records the seed and the shuffled order; a screen with another refuses
        path = self.folder / "FREEZE-seed.json"
        with patch.object(R, "read_predictions", return_value={}), patch.object(R, "offline_checks"), \
                patch.object(R, "source_hashes", return_value={"file": "h"}), \
                patch.object(R, "validate_pilots", return_value=[]), patch.object(R, "sha", return_value="pred"), \
                patch.object(R, "git", return_value=types.SimpleNamespace(returncode=0, stdout="abc\n")):
            quiet(R.prepare_freeze, path, R.ROOT / "predictions" / "temporal-expC-cells.json", ["C3", "C2"], [], 7)
        manifest = json.loads(path.read_text())
        self.assertEqual((manifest.get("seed"), manifest.get("order")), (7, ["C3", "C2"]))
        frozen = self.frozen(host_profile="wsl", seed=7, order=["C3", "C2"], conditions=["C3", "C2"])
        self.verify(frozen, host_profile="wsl", seed=7, order=["C3", "C2"])
        for seed, order in ((8, ["C3", "C2"]), (7, ["C2", "C3"])):
            with self.subTest(seed=seed, order=order), self.assertRaisesRegex(ValueError, "seed|order"):
                self.verify(frozen, host_profile="wsl", seed=seed, order=order)
        with self.assertRaisesRegex(ValueError, "seed"):
            self.verify(self.frozen(host_profile="wsl"), host_profile="wsl", seed=7, order=["C2"])

    def test_read_predictions_checks_the_disclosed_runs(self):
        cell = {"prediction": "p", "label": "P", "basis": "b", "refutation": "r"}
        data = {"schema_version": 1, "prior_exposure": "C.13", "configuration": {"cli": "1.9.1"},
                "conditions": {"C2": {"measures": {m: dict(cell) for m in R.MEASURES}}}}
        path = self.folder / "p.json"
        for exposed, ok in (([], True), ([{"trial": "XC-pilot-f-C2", "record_sha256": "a" * 64}], True),
                            ([{"trial": "XC-pilot-f-C2"}], False), ("C2", False)):
            path.write_text(json.dumps(dict(data, exposed_validation=exposed)))
            if ok:
                R.read_predictions(path, ["C2"])
            else:
                with self.assertRaisesRegex(ValueError, "exposed_validation"):
                    R.read_predictions(path, ["C2"])

    def test_a_pilot_the_scorer_calls_invalid_cannot_validate(self):
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.validate(self.folders(), FakeScorer(invalid={"C3"}))

    def test_a_pilot_from_another_host_profile_cannot_validate(self):
        with self.assertRaisesRegex(ValueError, "host profile"):
            self.validate(self.folders(change=lambda r: r.update(host_profile={"declared": "linux-vm"}),
                                       target="C0-HB"))

    def test_every_pilot_must_carry_the_freezes_source_fingerprints(self):
        with self.assertRaisesRegex(ValueError, "source manifest"):
            self.validate(self.folders(change=lambda r: r.update(source_hashes={"file": "old"}), target="C6"))

    def test_a_pilot_without_the_wall_receipt_or_the_current_schema_cannot_validate(self):
        for change, word in ((lambda r: r["clock_across_trial"].pop("wall_clock"), "wall"),
                             (lambda r: r.update(schema_version=1), "schema")):
            with self.subTest(word=word):
                sub = self.folder / word
                sub.mkdir()
                record = pilot_record("C2", self.hashes)
                change(record)
                (sub / "record.json").write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, word):
                    self.validate([sub])

    def test_pc_needs_its_gate_and_the_probes_rerun(self):
        with self.assertRaisesRegex(ValueError, "gate"):
            self.validate(self.folders(), FakeScorer(pc_gate="fail"))
        with self.assertRaisesRegex(ValueError, "re-run"):
            self.validate(self.folders(), FakeScorer(probe="stayed ended"))

    def test_two_pilots_of_one_condition_are_refused(self):
        with self.assertRaisesRegex(ValueError, "one validation pilot"):
            self.validate(self.folders(list(R.PILOT_SET) + ["C3"]))

    def test_freeze_records_the_host_profile_predictions_and_sources(self):
        for profile in ("wsl", "linux-vm"):
            path = self.folder / f"FREEZE-{profile}.json"
            with self.subTest(profile=profile), patch.dict(os.environ, {"STOPCHECK_HOST_PROFILE": profile}), \
                    patch.object(R, "read_predictions"), patch.object(R, "offline_checks"), \
                    patch.object(R, "source_hashes", return_value={"file": "h"}), \
                    patch.object(R, "validate_pilots", return_value=[{"condition": "PC"}]) as validate, \
                    patch.object(R, "sha", return_value="pred"), \
                    patch.object(R, "git", return_value=types.SimpleNamespace(returncode=0, stdout="abc\n")):
                quiet(R.prepare_freeze, path, R.ROOT / "predictions" / "temporal-expC-cells.json", ["C2"], [], 1)
                self.assertEqual(validate.call_args.args[1:], ({"file": "h"}, profile, "pred", []))
            manifest = json.loads(path.read_text())
            self.assertEqual((manifest.get("host_profile"), manifest.get("prediction_sha256"),
                              manifest.get("source_hashes"), manifest.get("validation_pilots")),
                             (profile, "pred", {"file": "h"}, [{"condition": "PC"}]))
            self.assertEqual(manifest.get("prediction_file"), "predictions/temporal-expC-cells.json")

    def test_verify_freeze_needs_the_freeze_and_the_predictions_committed(self):
        seen = []
        with patch.object(R, "read_predictions"), patch.object(R, "sha", return_value="pred"), \
                patch.object(R, "source_hashes", return_value={"file": "h"}), patch.object(R, "check_repository"), \
                patch.object(R, "require_committed", side_effect=lambda path, what: seen.append(what)):
            R.verify_freeze(self.frozen(host_profile="wsl"), ["C2"], run_checks=False, host_profile="wsl")
        self.assertEqual(seen, ["Freeze manifest", "Prediction file"])

    def frozen(self, **manifest):
        path = self.folder / f"freeze-{len(list(self.folder.glob('freeze-*.json')))}.json"
        path.write_text(json.dumps(dict({"schema_version": 1, "timing_mode": "full", "conditions": ["C2"],
                                         "prediction_file": "predictions/p.json", "prediction_sha256": "pred",
                                         "source_hashes": {"file": "h"}}, **manifest)))
        return path

    def verify(self, path, **kwargs):
        with patch.object(R, "read_predictions"), patch.object(R, "sha", return_value="pred"), \
                patch.object(R, "source_hashes", return_value={"file": "h"}), patch.object(R, "check_repository"), \
                patch.object(R, "require_committed"):
            return R.verify_freeze(path, ["C2"], run_checks=False, **kwargs)

    def test_a_freeze_runs_only_on_the_host_profile_it_binds(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOPCHECK_HOST_PROFILE", None)
            self.assertEqual(self.verify(self.frozen(host_profile="wsl"))["host_profile"], "wsl")
        with patch.dict(os.environ, {"STOPCHECK_HOST_PROFILE": "linux-vm"}):
            self.assertEqual(self.verify(self.frozen(host_profile="linux-vm"))["host_profile"], "linux-vm")
            with self.assertRaisesRegex(ValueError, "host profile"):
                self.verify(self.frozen(host_profile="wsl"))
        for manifest in ({"host_profile": "linux-vm"}, {}, {"host_profile": None}):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(ValueError, "host profile"):
                self.verify(self.frozen(**manifest), host_profile="wsl")

    def test_offline_checks_run_every_c_test_module_and_the_mutation_check(self):
        seen = []
        with patch.object(R.subprocess, "run", side_effect=lambda cmd, **kw: seen.append(cmd) or
                          types.SimpleNamespace(returncode=0, stdout="", stderr="")):
            R.offline_checks()
        flat = [" ".join(map(str, c)) for c in seen]
        for name in ("tests.test_analyze_c", "tests.test_harness_c", "tests.test_runner_c", "tests.test_clockwall_c",
                     "test_clockguard.py", "mutate_c.py"):
            self.assertTrue(any(name in c for c in flat), (name, flat))


@unittest.skipUnless(shutil.which("git"), "requires git")
class FreezeFilesCommitted(unittest.TestCase):
    """As B.13 (review F3): the freeze manifest and the predictions must be committed, never in an ignored path."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = (Path(self.temp.name) / "repo").resolve()
        (self.repo / "temporal" / "results").mkdir(parents=True)
        (self.repo / "predictions").mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "fixture")
        (self.repo / ".gitignore").write_text("*/results/*\n")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "fixture")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def write(self, relative, commit):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        if commit:
            self.git("add", "--", relative)
            self.git("commit", "-q", "-m", "fixture")
        return path

    def check(self, path):
        with patch.object(R, "ROOT", self.repo):
            R.require_committed(path, "Freeze manifest")

    def test_a_committed_file_passes(self):
        self.check(self.write("temporal/FREEZE-expC.json", commit=True))

    def test_an_ignored_uncommitted_or_changed_file_is_refused(self):
        with self.assertRaisesRegex(ValueError, "is in a git-ignored path"):
            self.check(self.write("temporal/results/FREEZE-expC.json", commit=False))
        with self.assertRaisesRegex(ValueError, "not committed"):
            self.check(self.write("predictions/p.json", commit=False))
        path = self.write("temporal/F.json", commit=True)
        path.write_text('{"changed": 1}\n')
        with self.assertRaisesRegex(ValueError, "differs"):
            self.check(path)
        with self.assertRaisesRegex(ValueError, "inside the repository"):
            self.check(Path(self.temp.name) / "outside.json")


class Exports(BatchFixture):
    """C.12 (item 7, as B.13): every attempt, accepted or not, interrupted or without a record, is scored into
    <batch folder>/derived/<trial>/ beside a SHA-256 manifest of its raw files and reports; raw files are only read."""

    def setUp(self):
        super().setUp()
        self.raw = {}

    def launch_writing(self, record=True, code=0, interrupt=False):
        def launch(cmd, env, cwd, grace_s=None):
            trial = cmd[-1]
            self.raw[trial] = write_raw(self.results, trial, record=record)
            self.launches.append({"trial": trial})
            if interrupt:
                raise KeyboardInterrupt
            return code
        return launch

    @staticmethod
    def fingerprint(folder):
        return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(folder).rglob("*")) if p.is_file()}

    def check_export(self, entry, raw_folder):
        self.assertIn("export", entry, f"attempt {entry['trial']} was not exported")
        export = entry["export"]
        self.assertEqual(export["status"], "exported", export)
        manifest_path = self.results / export["manifest"]
        self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), export["manifest_sha256"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["trial"], entry["trial"])
        derived = self.results / manifest["derived_folder"]
        self.assertNotIn(raw_folder.resolve(), [derived.resolve(), *derived.resolve().parents])
        listed = {f["path"]: f["sha256"] for f in manifest["raw_files"]}
        self.assertEqual(listed, {f"{raw_folder.name}/{p}": d for p, d in self.fingerprint(raw_folder).items()})
        for item in manifest["derived_files"]:
            self.assertEqual(hashlib.sha256((self.results / item["path"]).read_bytes()).hexdigest(), item["sha256"])
        self.assertIn("temporal/analyze_c.py", manifest["scorer_sources"])
        self.assertIn("temporal/clockwall_c.py", manifest["scorer_sources"])
        return manifest, derived

    def test_every_attempt_is_scored_and_exported_outside_its_raw_folder(self):
        self.invalid.add("XC-pilot-e1-C3")
        with patch.object(R, "run_trial", side_effect=self.launch_writing()):
            self.assertEqual(self.run_main("--pilot", "--conditions", "C2", "C3", "--batch-id", "e1"), 2)
        trials = self.batch("pilot", "e1")["trials"]
        self.assertEqual([(t["condition"], t["accepted"]) for t in trials], [("C2", True), ("C3", False)])
        before = {t: self.fingerprint(f) for t, f in self.raw.items()}
        scorer = R._scorer()
        for entry in trials:
            folder = self.raw[entry["trial"]]
            manifest, derived = self.check_export(entry, folder)
            self.assertEqual(manifest["scoring"]["status"], "scored")
            self.assertEqual(self.fingerprint(folder), before[entry["trial"]])             # raw files untouched
            self.assertEqual(sorted(os.listdir(folder)), sorted(self.fingerprint(folder)))  # nothing added there
            T = scorer.load(str(folder))
            rows = scorer.score_trial(T)
            self.assertEqual((derived / "scored_c.jsonl").read_text(encoding="utf-8"),
                             "".join(json.dumps(r, default=str) + "\n" for r in rows))
            self.assertEqual((derived / "summary_c.md").read_text(encoding="utf-8"), scorer.summarize(T, rows))

    def test_interrupted_and_recordless_attempts_are_exported_too(self):
        with patch.object(R, "run_trial", side_effect=self.launch_writing(record=False, interrupt=True)):
            self.assertEqual(self.run_main("--pilot", "--conditions", "C0-HB", "--batch-id", "e2"), 130)
        entry = self.batch("pilot", "e2")["trials"][0]
        self.assertIs(entry["interrupted"], True)
        manifest, _ = self.check_export(entry, self.raw[entry["trial"]])
        self.assertEqual(manifest["scoring"]["status"], "not scored")
        self.assertEqual(manifest["derived_files"], [])

    def test_resume_exports_an_attempt_that_ended_before_its_export(self):
        def runner_dies(cmd, env, cwd, grace_s=None):
            self.raw[cmd[-1]] = write_raw(self.results, cmd[-1])
            raise RuntimeError("runner died while the trial ran")
        with patch.object(R, "run_trial", side_effect=runner_dies), self.assertRaises(RuntimeError):
            self.run_main("--pilot", "--conditions", "C0-HB", "--batch-id", "e3")
        self.assertNotIn("export", self.batch("pilot", "e3")["trials"][0])
        with patch.object(R, "run_trial", side_effect=self.launch_writing()):
            self.resume_until_done("pilot", "e3")
        first, second = self.batch("pilot", "e3")["trials"]
        self.assertEqual(first["status"], "unrecorded")
        self.check_export(first, self.raw[first["trial"]])
        self.check_export(second, self.raw[second["trial"]])


# ============================ the predictions file (C.8) ============================
PREDICTIONS_FILE = Path(R.ROOT) / "predictions" / "temporal-expC-cells.json"
PREDICTIONS_BUILDER = Path(R.ROOT) / "predictions" / "build_temporal_expC_cells.py"
# the scorer's row families (analyze_c.row's first argument), written from RULES-expC C.5, C.6 and C.9; "record" names a
# field of record.json that no scored row carries (the pre-probe check's verdict)
SCORER_MEASURES = {"M1", "M2", "M3", "M4", "M5", "FAC", "history", "pending_on_closed", "events", "verified_stop",
                   "restart", "PC", "C4", "C6", "C8", "twin", "validity", "record"}


class PredictionsFile(unittest.TestCase):
    """C.8, C.10: the committed predictions file, which the freeze binds and every trial hashes before it runs
    (predictions_at_start), loads under the runner's own schema check for every condition, gives each condition its
    prior exposure (C.13), names for each cell the scorer rows it is compared with after scoring, and is exactly what its
    builder writes from the current rules, timing plan and workload: a change to any of them shows here, before a
    validation run records the file's SHA-256."""

    def load(self):
        return R.read_predictions(PREDICTIONS_FILE, list(R.CONDITIONS))

    def test_it_loads_under_the_runners_schema_for_every_condition(self):
        data = self.load()
        self.assertEqual(set(data["conditions"]), set(R.CONDITIONS))
        self.assertIn("C.13", data["prior_exposure"])
        self.assertIsInstance(data.get("exposed_validation"), list)

    def test_each_cell_names_its_state_scored_rows_and_origin(self):
        for condition, entry in self.load()["conditions"].items():
            self.assertIn("C.13", entry.get("prior_exposure") or "", condition)
            for name, cell in entry["measures"].items():
                with self.subTest(condition=condition, measure=name):
                    self.assertTrue(isinstance(cell.get("predicted_state"), str) and cell["predicted_state"].strip())
                    self.assertIn(cell.get("origin"), ("C.8", "derived"))
                    rows = cell.get("scored_as")
                    self.assertTrue(isinstance(rows, list) and rows)
                    for scored in rows:
                        self.assertIn(scored.split(" / ")[0], SCORER_MEASURES, scored)
                    if cell["label"] in ("—", "-"):      # C.8: an unlabelled cell is one C.8 leaves descriptive
                        self.assertTrue(cell["predicted_state"].startswith(("unpredicted", "reported")),
                                        cell["predicted_state"])

    def test_the_file_is_what_its_builder_writes_from_the_current_code_and_rules(self):
        spec = importlib.util.spec_from_file_location("build_temporal_expC_cells_under_test", PREDICTIONS_BUILDER)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertEqual(PREDICTIONS_FILE.read_text(encoding="utf-8"), builder.render(builder.build()))


if __name__ == "__main__":
    unittest.main()
