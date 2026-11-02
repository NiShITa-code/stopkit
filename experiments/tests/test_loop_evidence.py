"""Tests of experiments/scripts/loop_evidence.py: silence or an incomplete reading never counts as a confirmed end.

The tests read the 13 October trials as fixtures and change only objects in memory or copies in temporary
folders, never a record of the repository.
"""

import importlib.util
import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "loop_evidence", Path(__file__).resolve().parents[1] / "scripts" / "loop_evidence.py"
)
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)


class PresentationSafetyTests(unittest.TestCase):
    def test_silence_without_end_evidence_stays_unconfirmed(self):
        support, text, _ = view.presentation(True, False, "unresolved", "No independent evidence")
        self.assertEqual(support, "unconfirmed")
        self.assertIn("No recent activity", text)
        self.assertIn("termination unconfirmed", text)

    def test_recent_activity_does_not_claim_snapshot_liveness(self):
        support, text, _ = view.presentation(True, True, "unresolved", "No independent evidence")
        self.assertEqual(support, "unconfirmed")
        self.assertIn("Recent activity observed", text)
        self.assertNotIn("still running", text)

    def test_recent_activity_and_later_closure_are_compatible(self):
        # A task may act during the final five seconds and then end before the
        # snapshot. Recent activity must not erase actual completion evidence.
        support, text, cause = view.presentation(True, True, "closed", "worker unit is inactive and its group is empty")
        self.assertEqual(support, "supported")
        self.assertIn("snapshot", text)
        self.assertNotEqual(cause, "cancellation")

    def test_invalid_observations_cannot_confirm_even_a_closed_boundary(self):
        support, _, _ = view.presentation(False, False, "closed", "worker unit is inactive and its group is empty")
        self.assertEqual(support, "unknown")

    def test_closed_label_without_supporting_reason_is_not_enough(self):
        support, _, _ = view.presentation(True, False, "closed", None)
        self.assertEqual(support, "unconfirmed")

    def test_natural_completion_is_separate_from_cancellation(self):
        reason = (
            "all identified executions reached their designed iteration limit "
            "and have successful terminal records (natural end)"
        )
        support, text, cause = view.presentation(True, False, "closed", reason)
        self.assertEqual(support, "supported")
        self.assertIn("Natural completion", text)
        self.assertEqual(cause, "natural completion")


class RecordedEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyzer = view.load_analyzer()
        cls.cancel_folder = next(view.TRIALS.glob("w4/collect-001/results/XB-screen-cb1013-w4-B1-2026*"))
        cls.worker_stop_folder = next(view.TRIALS.glob("w3/collect-001/results/XB-screen-cb1013-w3-B8-2026*"))

    def trial(self, folder=None):
        return self.analyzer.Trial(str(folder or self.cancel_folder))

    def add_final_iteration(self, trial, time):
        event = deepcopy(
            next(
                p
                for p in trial.pings
                if p.get("canary") == self.analyzer.CANARY["loop_steps"] and str(p.get("exec")) == trial.agent_id
            )
        )
        event["mono"] = time
        event["n"] = trial.R["workload"]["n_steps"]
        trial.pings.append(event)

    @staticmethod
    def set_agent_status(trial, read, status):
        for row in read["pg_rows"]:
            if str(row.get("id")) == trial.agent_id:
                row["status"] = status

    def test_recorded_quiet_cancel_is_unconfirmed(self):
        rows = view.describe_trial(self.cancel_folder, self.analyzer)
        own = next(row for row in rows if row["carrier"] == "loop_steps")
        self.assertEqual(own["validity"], "valid")
        self.assertIn("canceled", own["platform_statuses"].values())
        self.assertFalse(own["recent_activity_observed"])
        self.assertEqual(own["recorded_computation_boundary"], "unresolved")
        self.assertEqual(own["termination_support"], "unconfirmed")

    def test_final_counter_without_success_record_cannot_confirm(self):
        trial = self.trial()
        self.add_final_iteration(trial, self.analyzer._read_time(trial.end_read) - 0.1)
        boundary, _ = self.analyzer.computation_boundary(trial, "loop_steps")
        self.assertEqual(boundary, "unresolved")

    def test_final_counter_and_success_record_support_natural_completion(self):
        trial = self.trial()
        self.add_final_iteration(trial, self.analyzer._read_time(trial.end_read) - 0.1)
        self.set_agent_status(trial, trial.end_read, "success")
        boundary, evidence = self.analyzer.computation_boundary(trial, "loop_steps")
        self.assertEqual(boundary, "closed")
        _, text, cause = view.presentation(True, False, boundary, evidence["why"])
        self.assertIn("Natural completion", text)
        self.assertEqual(cause, "natural completion")

    def test_incomplete_process_group_read_cannot_confirm_worker_closure(self):
        trial = self.trial(self.worker_stop_folder)
        self.assertEqual(self.analyzer.computation_boundary(trial, "loop_steps")[0], "closed")
        trial.end_read["unit_processes"]["worker"]["read_complete"] = False
        boundary, _ = self.analyzer.computation_boundary(trial, "loop_steps")
        self.assertEqual(boundary, "unresolved")

    def test_later_restart_completion_does_not_settle_earlier_snapshot(self):
        trial = self.trial()
        earlier_snapshot_time = self.analyzer._read_time(trial.end_read)
        later = next(read for read in trial.reads if read.get("label") == "restart+60s")
        self.assertGreater(self.analyzer._read_time(later), earlier_snapshot_time)
        self.set_agent_status(trial, later, "success")
        self.add_final_iteration(trial, self.analyzer._read_time(later) - 0.1)
        self.assertEqual(self.analyzer.computation_boundary(trial, "loop_steps", later)[0], "closed")
        self.assertEqual(self.analyzer.computation_boundary(trial, "loop_steps")[0], "unresolved")

    def copy_inputs(self, folder):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        for name in ("record.json", "pings.jsonl", "outside.jsonl", "issuer.jsonl"):
            shutil.copy2(folder / name, path / name)
        return path

    def test_missing_receiver_log_is_unknown_not_zero_activity(self):
        path = self.copy_inputs(self.cancel_folder)
        path.joinpath("pings.jsonl").unlink()
        rows = view.describe_trial(path, self.analyzer)
        for row in rows:
            self.assertEqual(row["termination_support"], "unknown")
            self.assertIsNone(row["pings_after_stop"])
            self.assertIsNone(row["recent_activity_observed"])

    def test_malformed_receiver_log_is_unknown_not_zero_activity(self):
        path = self.copy_inputs(self.cancel_folder)
        path.joinpath("pings.jsonl").write_text("{not valid json}\n", encoding="utf-8")
        rows = view.describe_trial(path, self.analyzer)
        for row in rows:
            self.assertEqual(row["termination_support"], "unknown")
            self.assertIsNone(row["pings_after_stop"])

    def test_invalid_trial_never_confirms_termination(self):
        path = self.copy_inputs(self.worker_stop_folder)
        record_path = path / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["void"] = "test: observation invalidated"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        rows = view.describe_trial(path, self.analyzer)
        for row in rows:
            self.assertEqual(row["termination_support"], "unknown")


if __name__ == "__main__":
    unittest.main()
