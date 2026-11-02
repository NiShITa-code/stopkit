"""Freeze, planning and batch checks; no service or trial is launched."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# STOPCHECK_RUN_EXPB=<path> tests another copy (n8n/mutate_b.py's runner mutants)
spec = importlib.util.spec_from_file_location(
    "run_expB", os.environ.get("STOPCHECK_RUN_EXPB", str(ROOT / "n8n" / "run_expB.py")))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

# RULES-expB B.0: versions, package hash and effective settings recorded in every trial (review F5).
B0_CONFIGURATION = ("n8n", "node", "postgres", "redis", "package_lock_sha256", "EXECUTIONS_MODE", "N8N_RUNNERS_MODE",
                    "N8N_RUNNERS_MAX_CONCURRENCY", "N8N_RUNNERS_TASK_TIMEOUT", "worker_concurrency", "NODES_EXCLUDE",
                    "N8N_GRACEFUL_SHUTDOWN_TIMEOUT", "N8N_LOG_LEVEL", "queue_lock_s", "lock_renew_s",
                    "stalled_interval_s", "queue_recovery_interval")


def prediction():
    return {"schema_version": 1, "prior_exposure": "Synthetic fixture; no empirical prediction.",
            "configuration": {k: "fixture" for k in B0_CONFIGURATION}, "conditions": {"B0": {
                "prediction": "fixture", "basis": "fixture", "refutation": "fixture",
                "carriers": {c: {"predicted_state": "unpredicted", "label": "M", "basis": "fixture",
                                  "refutation": "fixture"} for c in R.CARRIERS}}}}


def quiet(function, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args)


def lock_is_held(path):
    """What harness_b_ns.sh's `flock -n` would find; never creates the file."""
    import fcntl
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def load_harness():
    """harness_b loaded the way tests/test_harness_b.py loads it."""
    spec = importlib.util.spec_from_file_location("harness_b_for_run_expB_test", ROOT / "n8n" / "harness_b.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CampaignGates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_fast_screen_is_refused(self):
        with self.assertRaises(SystemExit):
            R.parse_args(["--fast"])

    def test_duplicate_conditions_are_refused(self):
        with self.assertRaises(SystemExit):
            R.parse_args(["--pilot", "--conditions", "B0", "B0"])

    def test_dry_run_never_runs_checks_or_services(self):
        with patch.object(R, "offline_checks", side_effect=AssertionError("must not run")), \
             patch.object(R.subprocess, "run", side_effect=AssertionError("must not launch")):
            self.assertEqual(R.main(["--pilot", "--dry-run", "--conditions", "B0"]), 0)

    def test_fast_pilot_never_includes_a_condition_fast_timing_refuses(self):
        """B.10: FAST timing refuses B1r and B9c (harness_b.condition_refusal). The FAST pilot's default set leaves B1r
        out, and naming either is refused before any check or trial; the full-length pilot's default is unchanged."""
        def plan(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(R.main(list(argv)), 0)
            return json.loads(out.getvalue())["conditions"]
        self.assertEqual(plan("--pilot", "--fast", "--dry-run"), ["B0", "PC", "B1"])
        self.assertEqual(plan("--pilot", "--dry-run"), ["B0", "PC", "B1", "B1r"])
        for condition in ("B1r", "B9c"):
            with self.subTest(condition=condition), self.assertRaises(SystemExit):
                quiet(R.parse_args, ["--pilot", "--fast", "--conditions", "B0", condition])
        self.assertEqual(R.parse_args(["--pilot", "--conditions", "B1r", "B9c"]).conditions, ["B1r", "B9c"])

    def test_missing_prior_exposure_is_rejected(self):
        data = prediction()
        data.pop("prior_exposure")
        path = self.folder / "p.json"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "exposure"):
            R.read_predictions(path, ["B0"])

    def test_missing_carrier_prediction_is_rejected(self):
        data = prediction()
        data["conditions"]["B0"]["carriers"].pop("outjob")
        path = self.folder / "p.json"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "carrier"):
            R.read_predictions(path, ["B0"])

    def test_changed_source_blocks_freeze(self):
        path = self.folder / "freeze.json"
        path.write_text(json.dumps({"schema_version": 1, "timing_mode": "full", "conditions": ["B0"],
                                   "prediction_file": "predictions/p.json", "prediction_sha256": "pred",
                                   "source_hashes": {"file": "old"}}))
        with patch.object(R, "read_predictions"), patch.object(R, "sha", return_value="pred"), \
             patch.object(R, "source_hashes", return_value={"file": "new"}):
            with self.assertRaisesRegex(ValueError, "Source"):
                R.verify_freeze(path, ["B0"], run_checks=False)

    def test_missing_full_validation_controls_blocks_freeze(self):
        with self.assertRaisesRegex(ValueError, "B0, PC, B1 and B1r"):
            R.validate_pilots([], {}, "wsl")

    def test_fast_validation_is_rejected(self):
        (self.folder / "record.json").write_text(json.dumps({"condition": "B0", "fast_mode": True,
                                                            "run_kind": "smoke"}))
        with self.assertRaisesRegex(ValueError, "full-length"):
            R.validate_pilots([self.folder], {}, "wsl")

    # ---- 13 Oct: the freeze records the declared host profile, and only that profile's host may use it ----
    def test_the_declared_host_profile_is_read_from_the_environment_and_never_inferred(self):
        env = {k: v for k, v in os.environ.items() if k != "STOPCHECK_HOST_PROFILE"}
        self.assertEqual(R.declared_host_profile(env), "wsl")
        self.assertEqual(R.declared_host_profile(dict(env, STOPCHECK_HOST_PROFILE="linux-vm")), "linux-vm")
        for bad in ("cloud", "", "WSL"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "STOPCHECK_HOST_PROFILE"):
                R.declared_host_profile(dict(env, STOPCHECK_HOST_PROFILE=bad))

    def test_freeze_records_the_declared_host_profile_and_validates_pilots_against_it(self):
        for profile in ("wsl", "linux-vm"):
            path = self.folder / f"FREEZE-{profile}.json"
            with self.subTest(profile=profile), patch.dict(os.environ, {"STOPCHECK_HOST_PROFILE": profile}), \
                    patch.object(R, "read_predictions"), patch.object(R, "offline_checks"), \
                    patch.object(R, "source_hashes", return_value={"file": "h"}), \
                    patch.object(R, "validate_pilots", return_value=[]) as validate, \
                    patch.object(R, "sha", return_value="pred"), \
                    patch.object(R, "git", return_value=SimpleNamespace(returncode=0, stdout="abc\n")):
                quiet(R.prepare_freeze, path, ROOT / "predictions" / "n8n-expB-cells.json", ["B0"], [])
                self.assertEqual(validate.call_args.args[2], profile)
            self.assertEqual(json.loads(path.read_text()).get("host_profile"), profile)

    def frozen(self, **manifest):
        path = self.folder / "freeze.json"
        path.write_text(json.dumps(dict({"schema_version": 1, "timing_mode": "full", "conditions": ["B0"],
                                         "prediction_file": "predictions/p.json", "prediction_sha256": "pred",
                                         "source_hashes": {"file": "h"}}, **manifest)))
        return path

    def verify(self, path, **kwargs):
        with patch.object(R, "read_predictions"), patch.object(R, "sha", return_value="pred"), \
                patch.object(R, "source_hashes", return_value={"file": "h"}), patch.object(R, "check_repository"), \
                patch.object(R, "require_committed"):
            return R.verify_freeze(path, ["B0"], run_checks=False, **kwargs)

    def test_a_freeze_runs_only_on_the_host_profile_it_binds(self):
        self.assertEqual(self.verify(self.frozen(host_profile="wsl"), host_profile="wsl")["host_profile"], "wsl")
        with patch.dict(os.environ, {"STOPCHECK_HOST_PROFILE": "linux-vm"}):
            self.assertEqual(self.verify(self.frozen(host_profile="linux-vm"))["host_profile"], "linux-vm")
            with self.assertRaisesRegex(ValueError, "host profile"):
                self.verify(self.frozen(host_profile="wsl"))
        for manifest in ({"host_profile": "linux-vm"}, {}, {"host_profile": None}):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(ValueError, "host profile"):
                self.verify(self.frozen(**manifest), host_profile="wsl")

    # ---- review F5: every B.0 configuration value must be declared, none left null ----
    def test_complete_configuration_is_accepted(self):
        path = self.folder / "p.json"
        path.write_text(json.dumps(prediction()))
        R.read_predictions(path, ["B0"])

    def test_partial_or_null_configuration_is_rejected(self):
        path = self.folder / "p.json"
        for key in B0_CONFIGURATION:
            for change in ("missing", "null"):
                data = prediction()
                if change == "missing":
                    data["configuration"].pop(key)
                else:
                    data["configuration"][key] = None
                path.write_text(json.dumps(data))
                with self.subTest(key=key, change=change), self.assertRaisesRegex(ValueError, "configuration"):
                    R.read_predictions(path, ["B0"])

    # ---- review F11: a repeated key would hide a second prediction for one cell ----
    def test_duplicate_keys_in_predictions_are_rejected(self):
        text = json.dumps(prediction())
        cell = '"outjob": {"predicted_state": "ended", "label": "P", "basis": "b", "refutation": "r"}'
        text = text.replace('"outjob": {', cell + ', "outjob": {', 1)
        path = self.folder / "p.json"
        path.write_text(text)
        with self.assertRaisesRegex(ValueError, "[Dd]uplicate"):
            R.read_predictions(path, ["B0"])

    # ---- review F8: the API key scope file the harness reads is bound by the freeze ----
    def test_source_manifest_binds_api_key_scopes(self):
        self.assertIn("n8n/owner_api_key_scopes.txt", R.source_hashes())


PILOTS = ("B0", "PC", "B1", "B1r")      # B.10: the full-length validation pilot
# the GCE metadata server's answers for one worker, by metadata path (input facts for the linux-vm identity fixtures)
METADATA_FIXTURE = {"id": "4242424242", "name": "stopcheck-w1", "zone": "projects/12/zones/europe-west1-b",
                    "machine-type": "projects/12/machineTypes/e2-standard-4", "cpu-platform": "Intel Broadwell",
                    "image": "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20261001",
                    "scheduling/preemptible": "FALSE", "scheduling/on-host-maintenance": "MIGRATE"}


def host_profile(profile="wsl"):
    """The host profile block every current record carries (harness_b.run_trial)."""
    return {"declared": profile, "variable": "STOPCHECK_HOST_PROFILE"}


def b1r_restart(ready_rel_stop=-50.0):
    """B1r's intervention box as the harness records it: the pre-stop main restart, verified ready with a changed
    main process identity before its deadline (15 s before the planned stop), then the stop at t_stop."""
    return {"target_mono": 300.0, "t_stop": {"mono": 300.2}, "t_return": {"mono": 300.3},
            "preparatory_restart": {"deadline_rel_stop": -15, "need_s": 112.6},
            "restarts": [{"tag": "pre-stop", "role": "main", "ok": True, "ready": True, "identity_changed": True,
                          "old_pid": 11, "new_pid": 12, "ready_mono": 300.0 + ready_rel_stop}]}


def pilot_record(condition, hashes, profile="wsl"):
    """A full-length pilot record that passes every record-level check the runner made before review F4."""
    record = {"trial": f"XB-pilot-x-{condition}", "condition": condition, "fast_mode": False, "run_kind": "pilot",
              "schema_version": 7, "host_profile": host_profile(profile),
              "source_hashes": hashes, "clock_start": {"ok": True}, "clock_end": {"ok": True},
              "clock_across_trial": {"ok": True},
              "pre_stop_check": {c: {"ok": True} for c in ("outjob", "fanout", "collateral")},
              "controls": {"outside_twin": {"calls": 3, "errors": 0}},
              "restart_probe": {"events": [{"unit": "main", "restarted": True, "ready": True}],
                                "reads": [{"label": "restart+0s", "controls": {}}]}}
    if condition == "B1r":
        record["intervention"] = b1r_restart()
    return record


class PilotValidation(unittest.TestCase):
    """Review F4: B.10 requires recovery starts and cleanup to succeed in the freeze-validation pilots. 13 Oct: the
    freeze needs all four full-length validation pilots of B.10 (B0, PC, B1 and B1r), each valid by the scorer, with the
    exact source fingerprints the freeze binds and the freeze's declared host profile; B1r's must show its main restart
    verified ready, with a changed identity, before its stop."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.hashes = {"file": "h"}

    def folders(self, change=None, target="PC", conditions=PILOTS, profile="wsl"):
        out = []
        for condition in conditions:
            folder = self.folder / condition
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir()
            record = pilot_record(condition, self.hashes, profile)
            if change and condition == target:
                change(record)
            (folder / "record.json").write_text(json.dumps(record))
            for name in ("pings", "outside", "issuer"):
                (folder / f"{name}.jsonl").write_text("")
            out.append(folder)
        return out

    def scorer(self, valid=True, probe_failure=None):
        real = R._scorer()
        return SimpleNamespace(Trial=real.Trial, _probe_failure=probe_failure or real._probe_failure,
                               _probe_control_failure=real._probe_control_failure,
                               validity_rows=lambda T: [{"kind": "valid" if valid else "invalid",
                                                         "verdict": "valid" if valid else "INVALID"}])

    def validate(self, folders, scorer, profile="wsl"):
        with patch.object(R, "_scorer", return_value=scorer):
            return R.validate_pilots(folders, self.hashes, profile)

    def test_passing_pilots_are_accepted(self):
        receipts = self.validate(self.folders(), self.scorer())
        self.assertEqual({r["condition"] for r in receipts}, set(PILOTS))

    def test_freeze_requires_all_four_validation_pilots(self):
        for missing in PILOTS:
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, f"missing.*{missing}"):
                self.validate(self.folders(conditions=[c for c in PILOTS if c != missing]), self.scorer())
        with self.assertRaisesRegex(ValueError, "B0, PC, B1 and B1r"):
            self.validate(self.folders(conditions=("B0", "PC")), self.scorer())

    def test_every_pilot_must_carry_the_freezes_source_fingerprints(self):
        for condition in PILOTS:
            def other(record):
                record["source_hashes"] = {"file": "other"}
            with self.subTest(condition=condition), self.assertRaisesRegex(ValueError, f"{condition}: .*source"):
                self.validate(self.folders(other, target=condition), self.scorer())

    def test_a_pilot_from_another_host_profile_cannot_validate_the_freeze(self):
        """A laptop wsl pilot can never validate a linux-vm freeze, nor the reverse; a record without its declared
        profile cannot either."""
        for change, freeze in ((lambda r: r.update(host_profile=host_profile("linux-vm")), "wsl"),
                               (lambda r: r.update(host_profile=host_profile("wsl")), "linux-vm"),
                               (lambda r: r.pop("host_profile"), "wsl"),
                               (lambda r: r.update(host_profile="wsl"), "wsl")):
            for condition in PILOTS:
                with self.subTest(freeze=freeze, condition=condition), \
                        self.assertRaisesRegex(ValueError, f"{condition}: .*host profile"):
                    self.validate(self.folders(change, target=condition, profile=freeze), self.scorer(), freeze)
        receipts = self.validate(self.folders(profile="linux-vm"), self.scorer(), "linux-vm")
        self.assertEqual({r["condition"] for r in receipts}, set(PILOTS))

    def test_b1r_pilot_needs_its_verified_preparatory_restart_before_the_stop(self):
        """B.10: B1r really changes main identity and completes readiness before its stop."""
        def restarts(*events):
            return lambda r: r["intervention"].update(restarts=list(events))
        good = b1r_restart()["restarts"][0]
        for name, change in (("no restart", restarts()),
                             ("two restarts", restarts(good, dict(good))),
                             ("not ok", restarts(dict(good, ok=False))),
                             ("not ready", restarts(dict(good, ready=False))),
                             ("same identity", restarts(dict(good, identity_changed=False))),
                             ("ready after the deadline", restarts(dict(good, ready_mono=300.0 - 10))),
                             ("ready time missing", restarts({k: v for k, v in good.items() if k != "ready_mono"})),
                             ("no intervention", lambda r: r.pop("intervention"))):
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, "B1r: .*restart"):
                self.validate(self.folders(change, target="B1r"), self.scorer())

    def test_matching_sources_cannot_admit_missing_or_downgraded_record_schema(self):
        # schema 6 included: records written before the 13 Oct merge already said 6 without its evidence
        for schema in (None, 4, 5, 6, "7", 7.0, 8):
            def change(record):
                if schema is None:
                    record.pop("schema_version")
                else:
                    record["schema_version"] = schema
            with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, "PC.*schema_version=7"):
                self.validate(self.folders(change), self.scorer())

    def test_scorer_invalid_pilot_blocks_freeze(self):
        with self.assertRaisesRegex(ValueError, "PC"):
            self.validate(self.folders(), SimpleNamespace(
                Trial=lambda folder: folder, _probe_failure=lambda T: False, _probe_control_failure=lambda T: None,
                validity_rows=lambda T: [{"kind": "invalid" if str(T).endswith("PC") else "valid",
                                          "verdict": "INVALID"}]))

    def test_real_scorer_verdict_is_applied(self):
        # These records have no actual stop or watch reads: the scorer itself must call them invalid.
        with self.assertRaisesRegex(ValueError, "B0|PC|B1"):
            R.validate_pilots(self.folders(), self.hashes, "wsl")

    def test_failed_restart_probe_blocks_freeze(self):
        def failed(record):
            record["restart_probe"]["error"] = "RuntimeError: restart probe readiness failed: n8n-main"
        with self.assertRaisesRegex(ValueError, "PC"):
            self.validate(self.folders(failed), self.scorer())

    def test_scorer_probe_control_failure_blocks_freeze(self):
        def key_control_failed(record):
            record["restart_probe"]["reads"] = [{"label": "restart+0s", "controls": {"twin_key_valid": True}},
                                                {"label": "restart+60s", "controls": {"twin_key_valid": False}}]
        with self.assertRaisesRegex(ValueError, "PC"):
            self.validate(self.folders(key_control_failed), self.scorer())

    def test_restart_that_never_became_ready_blocks_freeze(self):
        def unready(record):
            record["restart_probe"]["events"] = [{"unit": "main", "restarted": True, "ready": False}]
        with self.assertRaisesRegex(ValueError, "PC"):
            self.validate(self.folders(unready), self.scorer(probe_failure=lambda T: False))

    def test_restart_probe_without_reads_blocks_freeze(self):
        def no_reads(record):
            record["restart_probe"]["reads"] = []
        with self.assertRaisesRegex(ValueError, "PC"):
            self.validate(self.folders(no_reads), self.scorer(probe_failure=lambda T: False))

    # ---- 12 Oct check, item 1: B.10's full-length validation must show that "every carrier fires and the recorder
    # sees it", and B.9 adopts RULES-expA section 7's untouched control "across the watch and across the restart
    # probe". R6.9 keeps a probe-control failure out of trial validity, so the freeze gate must check it itself ----
    def real_pilots(self, change=None):
        """Complete B0, PC, B1 and B1r pilot records (tests/test_analyze_b.Syn, every control passing) with a 60 s
        restart probe from t_stop + 200 s; scored by the real scorer, no part of it replaced."""
        from tests.test_analyze_b import Syn, restart_probe, T0
        out = []
        for condition in PILOTS:
            s = Syn(condition)
            s.R.update(run_kind="pilot", source_hashes=self.hashes, schema_version=7, host_profile=host_profile())
            if condition == "B1r":
                box = b1r_restart()
                s.R["intervention"].update(target_mono=T0 - 0.2, preparatory_restart=box["preparatory_restart"],
                                           restarts=[dict(box["restarts"][0], ready_mono=T0 - 50)])
            restarted = condition == "PC"           # PC killed both units; the others' stayed active
            restart_probe(s, T0 + 200.0, events=[{"unit": u, "restarted": restarted, "ready": True} if restarted else
                                                  {"unit": u, "restarted": False, "already_active": True}
                                                  for u in ("main", "worker")])
            if change and condition == "PC":
                change(s, T0 + 200.0)
            folder = self.folder / f"real-{condition}"
            folder.mkdir()
            (folder / "record.json").write_text(json.dumps(s.R))
            for name, rows in s.logs().items():
                (folder / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            out.append(folder)
        return out

    def test_real_pilots_with_passing_probe_controls_are_accepted(self):
        receipts = R.validate_pilots(self.real_pilots(), self.hashes, "wsl")
        self.assertEqual({r["condition"] for r in receipts}, set(PILOTS))

    def test_untouched_twin_gap_in_the_restart_probe_blocks_freeze(self):
        def gap(s, r0):          # a 10 s recorder silence mid-probe: more than section 7's 3 s
            s.pings = [p for p in s.pings if not (p["canary"] == "untouched_twin" and r0 + 20 < p["mono"] < r0 + 30)]
        with self.assertRaisesRegex(ValueError, r"real-PC: .*probe control"):
            R.validate_pilots(self.real_pilots(gap), self.hashes, "wsl")

    def test_schema_7_pilot_without_a_host_check_cannot_validate_the_freeze(self):
        """Schema 7: the real scorer voids a record that lacks a host check, so the pilot cannot validate a freeze."""
        for key in ("host_checks_start", "host_checks_end"):
            for condition in PILOTS:
                shutil.rmtree(self.folder / f"real-{condition}", ignore_errors=True)

            def drop(s, r0, key=key):
                del s.R[key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "real-PC: .*invalid.*host precondition"):
                R.validate_pilots(self.real_pilots(drop), self.hashes, "wsl")


class CurrentTrialSchemaAcceptance(unittest.TestCase):
    """A campaign cannot downgrade its record to escape required modern evidence checks."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.results = Path(self.temp.name)

    def write(self, record, rows=None):
        folder = self.results / f"{record['trial']}-fixture"
        folder.mkdir(exist_ok=True)
        (folder / "record.json").write_text(json.dumps(record))
        for name, values in (rows or {}).items():
            (folder / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in values))
        return folder

    def test_pilot_and_screen_reject_removed_or_legacy_schema_before_scoring(self):
        approving_scorer = SimpleNamespace(Trial=lambda folder: folder,
                                          validity_rows=lambda trial: [{"kind": "valid", "verdict": "valid"}])
        for kind in ("pilot", "screen"):
            for schema in (None, 1, 4, 5, 6, "7", 7.0):
                record = {"trial": "XB-current", "run_kind": kind, "source_hashes": {"current": "hash"}}
                if schema is not None:
                    record["schema_version"] = schema
                self.write(record)
                with self.subTest(kind=kind, schema=schema), patch.object(R, "RESULTS", self.results), \
                        patch.object(R, "_scorer", return_value=approving_scorer) as scorer:
                    result = R.trial_validity("XB-current")
                self.assertFalse(result["scored_valid"])
                self.assertIn("schema_version=7", result["validity"])
                scorer.assert_not_called()

    def test_schema_seven_still_requires_the_scorers_verdict(self):
        self.write({"trial": "XB-current", "schema_version": 7, "run_kind": "screen"})
        for kind in ("valid", "invalid"):
            scorer = SimpleNamespace(Trial=lambda folder: folder,
                                     validity_rows=lambda trial: [{"kind": kind, "verdict": kind}])
            with self.subTest(verdict=kind), patch.object(R, "RESULTS", self.results), \
                    patch.object(R, "_scorer", return_value=scorer):
                result = R.trial_validity("XB-current")
            self.assertEqual(result["scored_valid"], kind == "valid")

    def test_a_record_from_another_host_profile_is_never_accepted(self):
        """A batch accepts only records that declare its host profile (a laptop record can never count in a linux-vm
        batch, nor the reverse); a record that declares none is not accepted either."""
        approving = SimpleNamespace(Trial=lambda folder: folder,
                                    validity_rows=lambda trial: [{"kind": "valid", "verdict": "valid"}])
        for recorded, expected in ((host_profile("linux-vm"), False), (None, False), ("wsl", False),
                                   (host_profile("wsl"), True)):
            record = {"trial": "XB-current", "schema_version": 7, "run_kind": "screen"}
            if recorded is not None:
                record["host_profile"] = recorded
            self.write(record)
            with self.subTest(recorded=recorded), patch.object(R, "RESULTS", self.results), \
                    patch.object(R, "_scorer", return_value=approving):
                result = R.trial_validity("XB-current", "wsl")
                self.assertIs(result["scored_valid"], expected)
                if not expected:
                    self.assertIn("host profile", result["validity"])

    def test_current_schema_missing_isolation_or_guard_evidence_is_rejected_by_real_scorer(self):
        from tests.test_analyze_b import Syn
        for missing in ("isolation", "guard_violations"):
            fixture = Syn("B0")
            fixture.R.update(schema_version=7, run_kind="pilot")
            fixture.R.pop(missing)
            self.write(fixture.R, fixture.logs())
            with self.subTest(missing=missing), patch.object(R, "RESULTS", self.results):
                result = R.trial_validity(fixture.R["trial"])
            self.assertFalse(result["scored_valid"])
            self.assertIn(missing.split("_")[0], result["validity"].lower())


class WorkerIdentity(unittest.TestCase):
    """B.0, 13 Oct: on linux-vm the runner reads each worker's identity from the GCE metadata server, outside the trial's
    network namespace, before each trial. Workers need not be identical. A missing identity is recorded as missing and
    never refuses the trial. Fixtures only: no request leaves the process."""
    VALUES = METADATA_FIXTURE

    def test_linux_vm_identity_comes_from_the_gce_metadata_server(self):
        identity = R.worker_identity("linux-vm", fetch=self.VALUES.__getitem__)
        self.assertEqual(identity["status"], "recorded")
        self.assertEqual(identity["host_profile"], "linux-vm")
        self.assertEqual(set(identity["values"]), {"id", "name", "zone", "machine_type", "cpu_platform", "image",
                                                   "preemptible", "on_host_maintenance"})
        self.assertEqual((identity["missing"], identity["errors"]), ([], {}))
        self.assertIn("metadata", identity["source"])

    def test_a_partial_or_missing_identity_is_recorded_never_raised(self):
        def flaky(path):
            if path == "cpu-platform":
                raise OSError("timed out")
            return self.VALUES[path]
        partial = R.worker_identity("linux-vm", fetch=flaky)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["missing"], ["cpu_platform"])
        self.assertIn("timed out", partial["errors"]["cpu_platform"])

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
                return b"projects/12/machineTypes/e2-standard-4\n"

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
            self.assertEqual(R._gce_metadata("machine-type"), "projects/12/machineTypes/e2-standard-4")
        self.assertEqual(seen["url"], "http://metadata.google.internal/computeMetadata/v1/instance/machine-type")
        self.assertEqual(seen["header"], "Google")
        self.assertTrue(seen["timeout"] and seen["timeout"] <= 5)
        self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers))
        with patch.object(urllib.request, "build_opener", return_value=Opener(None)), \
                self.assertRaisesRegex(ValueError, "Metadata-Flavor"):
            R._gce_metadata("machine-type")


@unittest.skipUnless(shutil.which("git"), "requires git")
class FreezeFilesCommitted(unittest.TestCase):
    """Review F3: the freeze manifest and predictions must be committed, never in an ignored or outside path."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = (Path(self.temp.name) / "repo").resolve()
        (self.repo / "n8n" / "results").mkdir(parents=True)
        (self.repo / "predictions").mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "fixture")
        (self.repo / ".gitignore").write_text("*/results/*\n")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "fixture")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def write(self, relative, data, commit):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        if commit:
            self.git("add", "--", relative)
            self.git("commit", "-q", "-m", "fixture")
        return path

    def manifest(self, prediction_file):
        return {"schema_version": 1, "timing_mode": "full", "conditions": ["B0"], "prediction_file": prediction_file,
                "prediction_sha256": "pred", "source_hashes": {"file": "h"}, "host_profile": "wsl"}

    def verify(self, freeze):
        with patch.object(R, "ROOT", self.repo), patch.object(R, "read_predictions"), \
             patch.object(R, "sha", return_value="pred"), patch.object(R, "source_hashes", return_value={"file": "h"}):
            return R.verify_freeze(freeze, ["B0"], run_checks=False, host_profile="wsl")

    def test_committed_freeze_and_predictions_pass(self):
        self.write("predictions/p.json", prediction(), commit=True)
        freeze = self.write("n8n/FREEZE-expB.json", self.manifest("predictions/p.json"), commit=True)
        self.assertEqual(self.verify(freeze)["prediction_file"], "predictions/p.json")

    def test_freeze_in_ignored_path_is_refused(self):
        self.write("predictions/p.json", prediction(), commit=True)
        freeze = self.write("n8n/results/FREEZE-expB.json", self.manifest("predictions/p.json"), commit=False)
        with self.assertRaisesRegex(ValueError, "ignored"):
            self.verify(freeze)

    def test_predictions_in_ignored_path_are_refused(self):
        self.write("n8n/results/p.json", prediction(), commit=False)
        freeze = self.write("n8n/FREEZE-expB.json", self.manifest("n8n/results/p.json"), commit=True)
        with self.assertRaisesRegex(ValueError, "ignored"):
            self.verify(freeze)

    def test_freeze_outside_repository_is_refused(self):
        self.write("predictions/p.json", prediction(), commit=True)
        outside = Path(self.temp.name) / "outside" / "FREEZE-expB.json"
        outside.parent.mkdir()
        outside.write_text(json.dumps(self.manifest("predictions/p.json")))
        with self.assertRaisesRegex(ValueError, "repository"):
            self.verify(outside)


class BatchFixture(unittest.TestCase):
    """Isolated results folder, stubbed checks and a fake launcher; holds no tests of its own."""
    patch_launch = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.results = self.folder / "results"
        self.results.mkdir()
        self.freeze = self.folder / "FREEZE-expB.json"
        self.freeze.write_text("{}\n")
        self.hashes = {"file": "h"}
        self.outcomes = {}      # condition -> list of exit codes, consumed per launch (default 0)
        self.invalid = set()    # trial ids the scorer calls invalid
        self.launches = []
        # Never the deployment's /run/stopcheck-n8n.lock: a probe of it could make a live trial's launcher refuse.
        self.lock_file = self.folder / "launcher.lock"
        patches = [patch.dict(R.os.environ, {"STOPCHECK_LOCK_FILE": str(self.lock_file)}),
                   patch.object(R, "RESULTS", self.results), patch.object(R, "offline_checks"),
                   patch.object(R, "require_clock"), patch.object(R, "verify_freeze", return_value={}),
                   patch.object(R, "source_hashes", side_effect=lambda: dict(self.hashes)),
                   patch.object(R.os, "geteuid", return_value=0, create=True),
                   patch.object(R, "trial_validity", side_effect=lambda trial, host_profile=None: {
                       "record": None, "scored_valid": trial not in self.invalid, "validity": "fixture"}),
                   # no fixture may reach the GCE metadata server; a linux-vm test supplies its own reader
                   patch.object(R, "_gce_metadata", side_effect=AssertionError("must not ask the metadata server"))]
        if self.patch_launch:
            patches.append(patch.object(R, "launch", side_effect=self.fake_launch))
        else:
            # The real launch() is under test: a test must supply its own fake Popen, never the real launcher.
            patches.append(patch.object(R.subprocess, "Popen", side_effect=AssertionError("must not launch")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # the caller's declared host profile never reaches a fixture (the runner's offline checks run these tests on
        # the trial host); patch.dict above restores it afterwards
        R.os.environ.pop("STOPCHECK_HOST_PROFILE", None)

    def fake_launch(self, condition, trial, env):
        batch = json.loads(Path(env["STOPCHECK_BATCH"]).read_text(encoding="utf-8"))
        self.launches.append({"condition": condition, "trial": trial, "env": dict(env), "batch": batch})
        if os.name == "posix" and lock_is_held(env["STOPCHECK_LOCK_FILE"]):
            return 2, False  # like harness_b_ns.sh: "Another StopCheck n8n trial owns the lock"
        codes = self.outcomes.get(condition) or [0]
        return (codes.pop(0) if len(codes) > 1 else codes[0]), False

    @contextlib.contextmanager
    def launcher_lock(self):
        """The lock a still-running launcher holds, as harness_b_ns.sh takes it (exec 9>"$LOCK_FILE"; flock -n 9)."""
        try:
            import fcntl
        except ImportError:
            self.skipTest("requires fcntl")
        with open(self.lock_file, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def batch_path(self, kind, batch_id):
        return self.results / f"XB-{kind}-{batch_id}" / "batch.json"

    def batch(self, kind, batch_id):
        return json.loads(self.batch_path(kind, batch_id).read_text(encoding="utf-8"))

    def run_main(self, *argv):
        return quiet(R.main, list(argv))

    def screen(self, batch_id, *conditions):
        return self.run_main("--freeze", str(self.freeze), "--conditions", *conditions, "--seed", "7",
                             "--batch-id", batch_id)

    def resume_until_done(self, kind, batch_id, limit=6):
        flags = ["--freeze", str(self.freeze)] if kind == "screen" else ["--pilot"]
        for _ in range(limit):
            if self.batch(kind, batch_id).get("completed"):
                return
            self.run_main(*flags, "--resume", str(self.batch_path(kind, batch_id)))
        self.fail("batch never completed")


class Batches(BatchFixture):
    """B.10 / R6.10 reruns and the batch link (F2), with a fake launcher."""

    # ---- F2: the harness can check that this exact trial is a pending entry of a screen batch ----
    def test_each_trial_sees_its_pending_entry_and_batch_path(self):
        self.assertEqual(self.screen("t2", "B0", "B1"), 0)
        path = self.batch_path("screen", "t2")
        self.assertEqual(len(self.launches), 2)
        for launch in self.launches:
            self.assertEqual(Path(launch["env"]["STOPCHECK_BATCH"]).resolve(), path.resolve())
            self.assertEqual(launch["env"]["STOPCHECK_FROZEN_MANIFEST"], str(self.freeze.resolve()))
            self.assertEqual(launch["batch"]["run_kind"], "screen")
            self.assertEqual(launch["batch"]["freeze_sha256"], hashlib.sha256(self.freeze.read_bytes()).hexdigest())
            entry = [t for t in launch["batch"]["trials"] if t["trial"] == launch["trial"]]
            self.assertEqual(len(entry), 1)
            self.assertEqual(entry[0]["status"], "pending")
        self.assertFalse([t for t in self.batch("screen", "t2")["trials"] if t["status"] == "pending"])

    def test_pilot_trials_also_receive_the_batch_path(self):
        self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t2p"), 0)
        self.assertEqual(Path(self.launches[0]["env"]["STOPCHECK_BATCH"]).resolve(),
                         self.batch_path("pilot", "t2p").resolve())

    # ---- F1: B.10 "invalid trials are rerun once"; R6.10: both invalid -> incomplete ----
    def test_failed_trial_is_rerun_once_then_incomplete(self):
        self.outcomes["B1"] = [3]
        self.assertNotEqual(self.screen("t1", "B0", "B1"), 0)
        self.resume_until_done("screen", "t1")
        batch = self.batch("screen", "t1")
        b1 = [t for t in batch["trials"] if t["condition"] == "B1"]
        self.assertEqual([t["attempt"] for t in b1], [1, 2])
        self.assertEqual(b1[1]["rerun_of"], b1[0]["trial"])
        self.assertEqual(len({t["trial"] for t in batch["trials"]}), len(batch["trials"]))
        self.assertEqual([t["condition"] for t in batch["trials"]].count("B0"), 1)
        self.assertEqual(batch["incomplete"], ["B1"])
        with self.assertRaisesRegex(ValueError, "complete"):
            self.run_main("--freeze", str(self.freeze), "--resume", str(self.batch_path("screen", "t1")))
        self.assertEqual(len(self.launches), 3)

    def test_resume_keeps_recorded_order_and_skips_accepted_conditions(self):
        self.outcomes["B1"] = [2, 0]
        self.assertEqual(self.screen("t3", "B0", "B1", "B2", "B3"), 2)
        order = self.batch("screen", "t3")["conditions"]
        self.resume_until_done("screen", "t3")
        i = order.index("B1")
        self.assertEqual([x["condition"] for x in self.launches], order[:i + 1] + [order[i]] + order[i + 1:])
        batch = self.batch("screen", "t3")
        self.assertEqual(batch["conditions"], order)
        self.assertEqual(batch["incomplete"], [])

    def test_trial_the_scorer_calls_invalid_is_rerun(self):
        self.invalid.add("XB-pilot-t5-B0")
        self.assertNotEqual(self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", "t5"), 0)
        self.assertEqual([x["condition"] for x in self.launches], ["B0"])
        self.resume_until_done("pilot", "t5")
        self.assertEqual([x["condition"] for x in self.launches], ["B0", "B0", "B1"])

    def test_new_screen_refused_when_the_freeze_already_has_a_batch(self):
        old = self.results / "XB-screen-old"
        old.mkdir()
        (old / "batch.json").write_text(json.dumps({"schema_version": 1, "run_kind": "screen", "trials": [],
                                                    "freeze_sha256": hashlib.sha256(self.freeze.read_bytes()).hexdigest()}))
        with self.assertRaisesRegex(ValueError, "resume"):
            self.screen("t6", "B0")
        self.assertEqual(self.launches, [])
        self.assertFalse((self.results / "XB-screen-t6").exists())

    def test_resume_refuses_a_new_order_or_changed_code(self):
        self.outcomes["B0"] = [3]
        self.screen("t7", "B0", "B1")
        path = str(self.batch_path("screen", "t7"))
        for extra in (["--conditions", "B0"], ["--seed", "1"], ["--batch-id", "x"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.run_main("--freeze", str(self.freeze), "--resume", path, *extra)
        self.hashes = {"file": "changed"}
        with self.assertRaisesRegex(ValueError, "[Cc]ode|[Ss]ource"):
            self.run_main("--freeze", str(self.freeze), "--resume", path)
        self.assertEqual(len(self.launches), 1)

    def test_exception_during_batch_records_stopped_reason(self):
        def launch_then_change_code(condition, trial, env):
            self.hashes = {"file": "edited during the batch"}
            return 0, False
        with patch.object(R, "launch", side_effect=launch_then_change_code):
            with self.assertRaisesRegex(ValueError, "Code changed"):
                self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", "t8")
        self.assertIn("Code changed", self.batch("pilot", "t8")["stopped_reason"])

    def test_attempt_without_recorded_exit_counts_on_resume(self):
        def runner_dies(condition, trial, env):
            raise RuntimeError("runner died while the trial ran")
        with patch.object(R, "launch", side_effect=runner_dies), self.assertRaises(RuntimeError):
            self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t9")
        batch = self.batch("pilot", "t9")
        self.assertIn("runner died", batch["stopped_reason"])
        self.assertEqual(batch["trials"][0]["status"], "pending")
        self.resume_until_done("pilot", "t9")
        trials = self.batch("pilot", "t9")["trials"]
        self.assertEqual([t["attempt"] for t in trials], [1, 2])
        self.assertNotEqual(trials[0]["status"], "pending")
        self.assertEqual(trials[1]["rerun_of"], trials[0]["trial"])

    def test_second_runner_on_the_same_batch_is_refused(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest("requires fcntl")
        self.outcomes["B0"] = [3]
        self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t10")
        with open(self.batch_path("pilot", "t10").parent / "batch.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "[Aa]nother runner"):
                self.run_main("--pilot", "--resume", str(self.batch_path("pilot", "t10")))
        self.assertEqual(len(self.launches), 1)

    # ---- F10 / F40: every trial id the batch can create must pass the harness's own pattern ----
    def test_batch_ids_the_harness_would_reject_are_refused_before_any_check(self):
        with patch.object(R, "offline_checks", side_effect=AssertionError("must not run")):
            for batch_id in ("pilot_1", "-night", "a" * 60):
                with self.subTest(batch_id=batch_id), self.assertRaisesRegex(ValueError, "[Bb]atch id"):
                    self.run_main("--pilot", f"--batch-id={batch_id}")
        self.assertEqual(list(self.results.iterdir()), [])
        self.assertEqual(self.launches, [])

    def test_every_trial_id_an_accepted_batch_id_can_create_passes_the_harness(self):
        harness_rule = r"[A-Za-z0-9][A-Za-z0-9-]{0,63}"  # harness_b.main
        conditions = ["B0", "PC", "B1", "B1r"]
        accepted = 0
        for length in range(1, 66):
            batch_id = "a" * length
            try:
                R.check_batch_id("pilot", batch_id, conditions)
            except ValueError:
                continue
            accepted += 1
            for condition in conditions:
                for attempt in (1, 2):  # B.10: one rerun, under its own trial id
                    trial = R.trial_id("pilot", batch_id, condition, attempt)
                    self.assertRegex(trial, "^" + harness_rule + "$")
        self.assertGreater(accepted, 0)
        ids = {R.trial_id("pilot", "x", "B0", attempt) for attempt in (1, 2)}
        self.assertEqual(len(ids), 2)

    def test_valid_batch_id_runs(self):
        self.assertEqual(self.run_main("--pilot", "--conditions", "B1r", "--batch-id", "night-1"), 0)
        self.assertEqual(self.launches[0]["trial"], "XB-pilot-night-1-B1r")

    # ---- checker C2: B.10 makes the scorer's validity the criterion; an exit status alone never accepts ----
    def interrupt_first_verdict(self):
        """Ctrl-C lands while the scorer judges the first trial, after the runner stored its exit status."""
        calls = []

        def validity(trial, host_profile=None):
            calls.append(trial)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return {"record": None, "scored_valid": trial not in self.invalid, "validity": "fixture"}
        return patch.object(R, "trial_validity", side_effect=validity)

    def test_exit_status_without_a_verdict_is_not_acceptance(self):
        self.assertIs(R.accepted({"trial": "x", "status": "finished", "exit_code": 0}), False)

    def test_interrupt_before_the_verdict_never_accepts_an_invalid_trial(self):
        self.invalid.add("XB-pilot-t12-B0")
        with self.interrupt_first_verdict():
            with self.assertRaises(KeyboardInterrupt):
                self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", "t12")
            first = self.batch("pilot", "t12")["trials"][0]
            self.assertEqual((first["status"], first["exit_code"]), ("finished", 0))
            self.assertNotIn("accepted", first)
            self.resume_until_done("pilot", "t12")
        self.assertEqual([x["trial"] for x in self.launches],
                         ["XB-pilot-t12-B0", "XB-pilot-t12-B0-a2", "XB-pilot-t12-B1"])
        b0 = [t for t in self.batch("pilot", "t12")["trials"] if t["condition"] == "B0"]
        self.assertEqual([(t["scored_valid"], t["accepted"]) for t in b0], [(False, False), (True, True)])
        self.assertEqual(self.batch("pilot", "t12")["incomplete"], [])

    def test_interrupt_before_the_verdict_is_judged_on_resume(self):
        with self.interrupt_first_verdict():
            with self.assertRaises(KeyboardInterrupt):
                self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", "t13")
            path = str(self.batch_path("pilot", "t13"))
            plan = io.StringIO()
            with contextlib.redirect_stdout(plan):
                self.assertEqual(R.main(["--pilot", "--dry-run", "--resume", path]), 0)
            self.assertEqual(json.loads(plan.getvalue())["next_trials"], ["XB-pilot-t13-B1"])
            self.resume_until_done("pilot", "t13")
        self.assertEqual([x["trial"] for x in self.launches], ["XB-pilot-t13-B0", "XB-pilot-t13-B1"])
        first = self.batch("pilot", "t13")["trials"][0]
        self.assertEqual((first["scored_valid"], first["validity"], first["accepted"]), (True, "fixture", True))

    # ---- checker C3: no attempt starts, or is recorded, while an earlier trial still holds the launcher lock ----
    def test_resume_refuses_while_an_orphaned_trial_holds_the_launcher_lock(self):
        def runner_killed(condition, trial, env):
            raise RuntimeError("runner killed; its detached trial runs on")
        with patch.object(R, "launch", side_effect=runner_killed), self.assertRaises(RuntimeError):
            self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t14")
        path = str(self.batch_path("pilot", "t14"))
        with self.launcher_lock():
            with self.assertRaisesRegex(ValueError, "still running"):
                self.run_main("--pilot", "--resume", path)
        batch = self.batch("pilot", "t14")
        self.assertEqual(len(batch["trials"]), 1)
        self.assertEqual(batch["incomplete"], [])
        self.assertIn("still running", batch["stopped_reason"])
        self.assertEqual(self.launches, [])
        self.resume_until_done("pilot", "t14")
        batch = self.batch("pilot", "t14")
        self.assertEqual([(t["attempt"], t.get("accepted")) for t in batch["trials"]], [(1, None), (2, True)])
        self.assertEqual(batch["incomplete"], [])

    def test_new_batch_starts_no_trial_while_another_trial_holds_the_launcher_lock(self):
        with self.launcher_lock():
            with self.assertRaisesRegex(ValueError, "still running"):
                self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t16")
        self.assertEqual(self.batch("pilot", "t16")["trials"], [])
        self.assertEqual(self.launches, [])

    def test_lock_probe_never_creates_the_lock_file(self):
        self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t17"), 0)
        self.assertFalse(self.lock_file.exists())

    # ---- 13 Oct: a batch binds its declared host profile; on linux-vm each trial's worker identity is recorded ----
    METADATA = METADATA_FIXTURE

    def test_batch_records_its_host_profile_and_each_trials_worker_identity(self):
        asked = []

        def metadata(path, timeout=2.0):
            asked.append(path)
            return self.METADATA[path]
        with patch.dict(R.os.environ, {"STOPCHECK_HOST_PROFILE": "linux-vm"}), \
                patch.object(R, "_gce_metadata", side_effect=metadata):
            self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", "t20"), 0)
        batch = self.batch("pilot", "t20")
        self.assertEqual(batch.get("host_profile"), "linux-vm")
        self.assertEqual(len(batch["trials"]), 2)
        for entry, launch in zip(batch["trials"], self.launches):
            identity = entry.get("host_identity") or {}
            self.assertEqual(identity.get("status"), "recorded")
            self.assertEqual(identity.get("values"), {
                "id": "4242424242", "name": "stopcheck-w1", "zone": "projects/12/zones/europe-west1-b",
                "machine_type": "projects/12/machineTypes/e2-standard-4", "cpu_platform": "Intel Broadwell",
                "image": "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20261001",
                "preemptible": "FALSE", "on_host_maintenance": "MIGRATE"})
            pending = [t for t in launch["batch"]["trials"] if t["trial"] == launch["trial"]]
            self.assertEqual(pending[0].get("host_identity"), identity)   # what the harness copies into the record
        self.assertEqual(sorted(set(asked)), sorted(self.METADATA))
        self.assertEqual(len(asked), 2 * len(self.METADATA))              # read again for every trial

    def test_a_missing_worker_identity_is_recorded_as_missing_and_never_refuses(self):
        def unreachable(path, timeout=2.0):
            raise OSError("no route to metadata.google.internal")
        with patch.dict(R.os.environ, {"STOPCHECK_HOST_PROFILE": "linux-vm"}), \
                patch.object(R, "_gce_metadata", side_effect=unreachable):
            self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t21"), 0)
        entry = self.batch("pilot", "t21")["trials"][0]
        identity = entry.get("host_identity") or {}
        self.assertEqual(identity.get("status"), "missing")
        self.assertEqual(identity.get("values"), {})
        self.assertEqual(len(identity.get("missing") or []), len(self.METADATA))
        self.assertIs(entry["accepted"], True)
        self.assertEqual(len(self.launches), 1)

    def test_wsl_batches_record_no_identity_and_never_ask_the_metadata_server(self):
        self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t22"), 0)
        batch = self.batch("pilot", "t22")
        self.assertEqual(batch.get("host_profile"), "wsl")
        self.assertEqual(batch["trials"][0].get("host_identity"), {"status": "not applicable", "host_profile": "wsl"})

    def test_resume_refuses_another_host_profile_and_acceptance_uses_the_batchs(self):
        seen = []

        def validity(trial, host_profile=None):
            seen.append(host_profile)
            return {"record": None, "scored_valid": trial not in self.invalid, "validity": "fixture"}
        self.outcomes["B0"] = [3]
        with patch.object(R, "trial_validity", side_effect=validity):
            self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", "t23")
        self.assertEqual(seen, ["wsl"])
        with patch.dict(R.os.environ, {"STOPCHECK_HOST_PROFILE": "linux-vm"}), \
                self.assertRaisesRegex(ValueError, "host profile"):
            self.run_main("--pilot", "--resume", str(self.batch_path("pilot", "t23")))
        self.assertEqual(len(self.launches), 1)

    # ---- 13 Oct: every attempt is scored and exported outside its raw folder, with a SHA-256 manifest ----
    def write_raw(self, trial, condition, record=True):
        """A raw trial folder as harness_b.main names it (<trial>-<date>-<time>-<9 digits>), with a nested log."""
        from tests.test_analyze_b import Syn
        folder = self.results / f"{trial}-20261013-120000-000000001"
        (folder / "logs").mkdir(parents=True)
        (folder / "logs" / "main.log").write_text("Initializing n8n process\n", encoding="utf-8")
        if record:
            s = Syn(condition)
            s.R.update(trial=trial, run_kind="pilot")
            (folder / "record.json").write_text(json.dumps(s.R), encoding="utf-8")
            for name, rows in s.logs().items():
                (folder / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return folder

    @staticmethod
    def fingerprint(folder):
        return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(folder).rglob("*")) if p.is_file()}

    def check_export(self, entry, raw_folder=None):
        """The entry's export: a manifest outside the raw folder, binding every raw file and the derived reports."""
        self.assertIn("export", entry, f"attempt {entry['trial']} was not exported")
        export = entry["export"]
        self.assertEqual(export["status"], "exported", export)
        manifest_path = self.results / export["manifest"]
        self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), export["manifest_sha256"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["trial"], entry["trial"])
        derived = self.results / manifest["derived_folder"]
        if raw_folder is not None:
            self.assertNotIn(raw_folder.resolve(), [derived.resolve(), *derived.resolve().parents])
            listed = {f["path"]: f["sha256"] for f in manifest["raw_files"]}
            self.assertEqual(listed, {f"{raw_folder.name}/{path}": digest
                                      for path, digest in self.fingerprint(raw_folder).items()})
            self.assertIn(f"{raw_folder.name}/logs/main.log", listed)
        for item in manifest["derived_files"]:
            self.assertEqual(hashlib.sha256((self.results / item["path"]).read_bytes()).hexdigest(), item["sha256"])
        return manifest, derived

    def test_every_attempt_is_scored_and_exported_outside_its_raw_folder(self):
        raw, before = {}, {}

        def launch(condition, trial, env):
            raw[trial] = self.write_raw(trial, condition)
            before[trial] = self.fingerprint(raw[trial])
            return 0, False
        self.invalid.add("XB-pilot-t30-B1")          # the scorer's verdict for the batch: invalid, so not accepted
        with patch.object(R, "launch", side_effect=launch):
            self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", "t30"), 2)
        trials = self.batch("pilot", "t30")["trials"]
        self.assertEqual([(t["condition"], t["accepted"]) for t in trials], [("B0", True), ("B1", False)])
        scorer = R._scorer()
        for entry in trials:
            folder = raw[entry["trial"]]
            manifest, derived = self.check_export(entry, folder)
            self.assertEqual(self.fingerprint(folder), before[entry["trial"]])        # raw files untouched
            self.assertEqual(manifest["scoring"]["status"], "scored")
            T = scorer.Trial(str(folder))
            rows = scorer.score_trial(T)
            self.assertEqual((derived / "scored_b.jsonl").read_text(encoding="utf-8"),
                             "".join(json.dumps(r) + "\n" for r in rows))
            self.assertEqual((derived / "summary_b.md").read_text(encoding="utf-8"), scorer.summarize(T, rows))
            self.assertEqual({Path(f["path"]).name for f in manifest["derived_files"]}, {"scored_b.jsonl", "summary_b.md"})

    def test_interrupted_and_recordless_attempts_are_exported_too(self):
        raw = {}

        def interrupted(condition, trial, env):
            raw[trial] = self.write_raw(trial, condition, record=False)     # stopped before the harness saved a record
            return 143, True
        with patch.object(R, "launch", side_effect=interrupted):
            self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t31"), 130)
        entry = self.batch("pilot", "t31")["trials"][0]
        self.assertIs(entry["interrupted"], True)
        manifest, _ = self.check_export(entry, raw[entry["trial"]])
        self.assertEqual(manifest["scoring"]["status"], "not scored")
        self.assertEqual(manifest["derived_files"], [])

    def test_resume_exports_an_attempt_that_ended_before_its_export(self):
        def runner_dies(condition, trial, env):
            self.write_raw(trial, condition)
            raise RuntimeError("runner died while the trial ran")
        with patch.object(R, "launch", side_effect=runner_dies), self.assertRaises(RuntimeError):
            self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t32")
        self.assertNotIn("export", self.batch("pilot", "t32")["trials"][0])
        self.resume_until_done("pilot", "t32")
        first, second = self.batch("pilot", "t32")["trials"]
        self.assertEqual(first["status"], "unrecorded")
        self.check_export(first, self.results / "XB-pilot-t32-B0-20261013-120000-000000001")
        self.check_export(second)

    # ---- checker C5: the batch.json the runner writes passes the harness's own screen-batch check ----
    def test_harness_verifies_the_runner_written_entry_and_its_rerun(self):
        H = load_harness()
        verified = []

        def launch_and_verify(condition, trial, env):
            verified.append(H.verify_screen_batch(env["STOPCHECK_BATCH"], trial, env["STOPCHECK_FROZEN_MANIFEST"],
                                                  results_dir=str(self.results)))
            return (3 if len(verified) == 1 else 0), False
        with patch.object(R, "launch", side_effect=launch_and_verify):
            self.assertEqual(self.screen("t15", "B0"), 3)
            self.resume_until_done("screen", "t15")
        self.assertEqual([v["verified"] for v in verified], [True, True])
        self.assertEqual([v["entry"]["trial"] for v in verified], ["XB-screen-t15-B0", "XB-screen-t15-B0-a2"])
        self.assertEqual([v["entry"]["attempt"] for v in verified], [1, 2])
        self.assertEqual(verified[1]["entry"]["rerun_of"], "XB-screen-t15-B0")


class Interrupts(BatchFixture):
    """Review F6: Ctrl-C must stop the trial through its launcher, never SIGKILL the launcher."""
    patch_launch = False

    def test_interrupt_terminates_the_launcher_and_is_recorded(self):
        started = []

        class FakeLauncher:
            def __init__(self, args, **kwargs):
                self.args, self.kwargs, self.signals, self.waits, self.pid = args, kwargs, [], 0, 4242
                started.append(self)

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise KeyboardInterrupt
                return 143

            def send_signal(self, sig):
                self.signals.append(sig)

            def kill(self):
                self.signals.append(signal.SIGKILL)

        with patch.object(R.subprocess, "Popen", FakeLauncher):
            rc = self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", "t11")
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(started), 1)
        self.assertIs(started[0].kwargs.get("start_new_session"), True)
        self.assertEqual(started[0].signals, [signal.SIGTERM])
        batch = self.batch("pilot", "t11")
        self.assertEqual(batch["trials"][0]["exit_code"], 143)
        self.assertIs(batch["trials"][0]["interrupted"], True)
        self.assertEqual(batch["stopped_reason"], "interrupted")

    def starting_handler(self, sig, handler):
        previous = signal.signal(sig, handler)
        self.addCleanup(signal.signal, sig, previous)

    # ---- checker C3: SIGTERM, or SIGHUP from a closed terminal or SSH session, is handled like Ctrl-C ----
    def test_sigterm_and_sighup_stop_the_trial_through_its_launcher(self):
        names = [n for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n) and hasattr(signal, "raise_signal")]
        if not names:
            self.skipTest("requires POSIX signals")
        for name in names:
            sig = getattr(signal, name)
            self.starting_handler(sig, signal.SIG_DFL)
            started = []

            class FakeLauncher:
                def __init__(self, args, **kwargs):
                    self.signals, self.waits, self.pid = [], 0, 4242
                    started.append(self)

                def wait(self, timeout=None):
                    self.waits += 1
                    if self.waits == 1:
                        # Delivered to the runner itself, as a terminal hangup or `kill` would; never to the test
                        # process while the default action (end the process without cleanup) is in place.
                        if not callable(signal.getsignal(sig)):
                            raise AssertionError(f"{name} would end the runner with no cleanup or record")
                        signal.raise_signal(sig)
                    return 143

                def send_signal(self, sent):
                    self.signals.append(sent)

            with self.subTest(signal=name), patch.object(R.subprocess, "Popen", FakeLauncher):
                batch_id = f"t18-{name.lower()}"
                self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "B1", "--batch-id", batch_id), 130)
                self.assertEqual(len(started), 1)
                self.assertEqual(started[0].signals, [signal.SIGTERM])
                batch = self.batch("pilot", batch_id)
                self.assertEqual((batch["trials"][0]["exit_code"], batch["trials"][0].get("interrupted")), (143, True))
                self.assertEqual(batch["stopped_reason"], "interrupted")
                self.assertEqual(signal.getsignal(sig), signal.SIG_DFL)  # the runner restores what it found

    def test_hangups_stay_ignored_under_nohup(self):
        if not hasattr(signal, "SIGHUP"):
            self.skipTest("requires POSIX signals")
        self.starting_handler(signal.SIGHUP, signal.SIG_IGN)
        seen = []

        class FakeLauncher:
            def __init__(self, args, **kwargs):
                self.pid = 4242

            def wait(self, timeout=None):
                seen.append(signal.getsignal(signal.SIGHUP))
                return 0

        with patch.object(R.subprocess, "Popen", FakeLauncher):
            self.assertEqual(self.run_main("--pilot", "--conditions", "B0", "--batch-id", "t19"), 0)
        self.assertEqual(seen, [signal.SIG_IGN])
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)


class ReproduceScorerIdentity(unittest.TestCase):
    """Review F7: a report reproduces only with the scorer the trial recorded, unless it is a labelled post hoc one.
    13 Oct: "the scorer" is every load-bearing scorer source, analyze_b.py and the clockwall_b.py it imports, checked
    through the full source manifest the record carries (run_expB.source_hashes): every recorded file must match.
    (STOPCHECK_REPRODUCE_B=<path> tests another copy, as n8n/mutate_b.py does.)"""

    @classmethod
    def setUpClass(cls):
        reproduce = importlib.util.spec_from_file_location(
            "reproduce_b_under_test", os.environ.get("STOPCHECK_REPRODUCE_B", str(ROOT / "n8n" / "reproduce_b.py")))
        cls.P = importlib.util.module_from_spec(reproduce)
        reproduce.loader.exec_module(cls.P)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.current = self.manifest()

    @staticmethod
    def manifest(drop=(), **changes):
        """This checkout's full source manifest as a trial records it, with some files dropped or given other hashes
        (keyword names use __ for / and _dot_ for .)."""
        out = R.source_hashes()
        for path in drop:
            del out[path]
        for key, digest in changes.items():
            out[key.replace("__", "/").replace("_dot_", ".")] = digest
        return out

    def trial(self, manifest, run_kind="pilot", frozen=None, reports=None):
        folder = self.folder / "XB-trial"
        folder.mkdir()
        record = {"trial": "XB-trial", "condition": "B0", "run_kind": run_kind}
        if manifest is not None:
            record["source_hashes"] = manifest
        if frozen is not None:
            record["freeze"] = {"source_hashes": frozen}
        (folder / "record.json").write_text(json.dumps(record))
        for name in ("pings", "outside", "issuer"):
            (folder / f"{name}.jsonl").write_text("")
        T = self.P.A.Trial(str(folder))
        rows = self.P.A.score_trial(T)
        target = reports or folder
        target.mkdir(exist_ok=True)
        (target / "scored_b.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (target / "summary_b.md").write_text(self.P.A.summarize(T, rows), encoding="utf-8")
        return folder

    def reproduce(self, *argv):
        return quiet(self.P.main, [str(a) for a in argv])

    def other_scorer(self):
        return self.manifest(n8n__analyze_b_dot_py="0" * 64)

    def test_recorded_scorer_reproduces(self):
        self.assertEqual(self.reproduce(self.trial(self.current)), 0)

    def test_other_recorded_scorer_fails(self):
        self.assertEqual(self.reproduce(self.trial(self.other_scorer())), 1)

    def test_missing_recorded_scorer_fails(self):
        self.assertEqual(self.reproduce(self.trial(self.manifest(drop=("n8n/analyze_b.py",)))), 1)
        shutil.rmtree(self.folder / "XB-trial")
        self.assertEqual(self.reproduce(self.trial(None)), 1)

    def test_replay_is_source_matched_only_when_every_recorded_source_matches(self):
        """The scorer imports clockwall_b.py for its wall/monotonic recomputation (B.9): a record whose clockwall_b.py
        differs from this checkout's, or that does not bind it, is not a source-matched replay, and neither is one
        that differs in any other file of its recorded manifest (here the harness)."""
        for manifest in (self.manifest(n8n__clockwall_b_dot_py="0" * 64),
                         self.manifest(drop=("n8n/clockwall_b.py",)),
                         self.manifest(n8n__harness_b_dot_py="0" * 64)):
            with self.subTest(manifest={k: v for k, v in manifest.items() if v != self.current.get(k)} or
                              sorted(set(self.current) - set(manifest))):
                shutil.rmtree(self.folder / "XB-trial", ignore_errors=True)
                self.assertEqual(self.reproduce(self.trial(manifest)), 1)

    def test_screen_requires_the_frozen_scorer(self):
        self.assertEqual(self.reproduce(self.trial(self.current, run_kind="screen", frozen=self.other_scorer())), 1)
        shutil.rmtree(self.folder / "XB-trial")
        frozen = self.manifest(n8n__clockwall_b_dot_py="0" * 64)
        self.assertEqual(self.reproduce(self.trial(self.current, run_kind="screen", frozen=frozen)), 1)
        shutil.rmtree(self.folder / "XB-trial")
        self.assertEqual(self.reproduce(self.trial(self.current, run_kind="screen", frozen=self.current)), 0)

    def test_labelled_post_hoc_reports_may_use_a_new_scorer(self):
        reports = self.folder / "XB-trial-posthoc"
        folder = self.trial(self.other_scorer(), reports=reports)
        self.assertEqual(self.reproduce(folder, "--reports", reports), 0)

    def test_documented_post_hoc_workflow_reproduces(self):
        # Review F13: the analyze_b.py --out step that reproduce_b documents must exist and be checkable.
        folder = self.trial(self.other_scorer())
        reports = self.folder / "XB-trial-posthoc"
        result = subprocess.run([sys.executable, "-B", str(ROOT / "n8n" / "analyze_b.py"), "--out", str(reports),
                                 str(folder)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.reproduce(folder, "--reports", reports), 0)
        self.assertEqual(self.reproduce(folder), 1)

    def test_unlabelled_separate_reports_still_need_the_recorded_scorer(self):
        reports = self.folder / "rescored"
        folder = self.trial(self.other_scorer(), reports=reports)
        self.assertEqual(self.reproduce(folder, "--reports", reports), 1)
