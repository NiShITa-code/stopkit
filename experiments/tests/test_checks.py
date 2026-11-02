"""Tests of experiments/scripts/build_results.py: each check fails when one record is changed, and nothing is written.

Every mutation is made in a view of the repository in a temporary folder, in which only the changed file is
a real copy and everything else links back, so no record of the repository is ever touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "experiments" / "scripts" / "build_results.py"

spec = importlib.util.spec_from_file_location("build_results", BUILDER)
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def mirror(tmp: Path, mutable: list[str]) -> Path:
    """A view of the repository in which the files in `mutable` are real copies and the rest are links."""
    view = tmp / "repo"
    needed = {Path(m) for m in mutable}
    real_dirs = {p for m in needed for p in m.parents if p != Path(".")}

    def build(rel: Path) -> None:
        (view / rel).mkdir(parents=True, exist_ok=True)
        for entry in (ROOT / rel).iterdir():
            r = rel / entry.name
            if r in real_dirs:
                build(r)
            elif r in needed:
                shutil.copy2(entry, view / r)
            else:
                (view / r).symlink_to(entry)

    build(Path("."))
    return view


def first(pattern: str) -> str:
    """The first tracked path, in sorted order, that matches `pattern`."""
    hits = sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern))
    assert hits, f"nothing matches {pattern}"
    return hits[0]


def edit_text(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} is not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def edit_first_row(path: Path, change) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    row = json.loads(lines[0])
    change(row)
    lines[0] = json.dumps(row) + "\n"
    path.write_text("".join(lines), encoding="utf-8")


def flip_a_value(row: dict) -> None:
    key = next(k for k, v in row.items() if isinstance(v, str) and k not in ("trial", "cond", "measure"))
    row[key] = row[key] + " changed"


class MutationTests(unittest.TestCase):
    """Each check, run alone on a view with one record changed, exits nonzero with its own message."""

    def fails(self, name: str, path: str, mutate, also: tuple[str, ...] = (), expect: str | None = None) -> None:
        """`mutate` gets the changed file's path in the view and the view; `also` are more files copied into it."""
        with tempfile.TemporaryDirectory() as tmp:
            view = mirror(Path(tmp), [path, *also])
            if mutate.__code__.co_argcount == 2:
                mutate(view / path, view)
            else:
                mutate(view / path)
            out = Path(tmp) / "results.json"
            p = subprocess.run(
                [sys.executable, "-B", str(BUILDER), "--root", str(view), "--out", str(out), "--only", name],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(p.returncode, 0, p.stdout)
            self.assertIn(f"FAIL  {checks.CHECKS[name]}", p.stdout)
            if expect:
                self.assertIn(expect, p.stdout)
            self.assertFalse(out.exists())

    def test_records(self):
        path = "experiments/n8n-restart/results/2026-10-29-fresh-vm/logs/sink.jsonl"
        self.fails("records", path, lambda p: p.write_bytes(p.read_bytes() + b"\n"))

    def test_n8n_checksums(self):
        path = first(
            "experiments/n8n-queue-mode/records/cb1013/w4/collect-001/results/XB-screen-cb1013-w4-B1-2026*/pings.jsonl"
        )
        self.fails("n8n_checksums", path, lambda p: p.write_bytes(p.read_bytes()[:-2] + b"\n"))

    def test_n8n_freeze(self):
        path = "experiments/harness/n8n-langgraph-7a3fcec/n8n/analyze_b.py"
        self.fails("n8n_freeze", path, lambda p: p.write_bytes(p.read_bytes() + b"\n"))

    def test_n8n_rescore(self):
        path = first("experiments/n8n-queue-mode/records/cb1013/w4/collect-001/results/*/derived/*-B1/scored_b.jsonl")
        self.fails("n8n_rescore", path, lambda p: edit_first_row(p, flip_a_value))

    def test_temporal_freeze(self):
        path = "experiments/harness/temporal-627df0b/temporal/analyze_c.py"
        self.fails("temporal_freeze", path, lambda p: p.write_bytes(p.read_bytes() + b"\n"))

    def test_temporal_rescore(self):
        path = first(
            "experiments/temporal-activities/records/cs1014/XC-screen-c-screen-1014/derived/*-C2/scored_c.jsonl"
        )
        self.fails("temporal_rescore", path, lambda p: edit_first_row(p, flip_a_value))

    def test_langgraph_reproduce(self):
        path = "experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1/trials.jsonl"
        self.fails("langgraph_reproduce", path, lambda p: p.write_bytes(p.read_bytes() + b"\n"))

    def test_langgraph_round1_code(self):
        path = "experiments/harness/langgraph-cd26851/langgraph/harness.py"
        self.fails("langgraph_round1_code", path, lambda p: p.write_bytes(p.read_bytes() + b"\n"))

    def test_langgraph_reproduce_rescores(self):
        """A changed scorer output, with its manifest entry updated to match, fails the scorer rerun itself."""
        results = "experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1"

        def mutate(scored: Path, view: Path) -> None:
            before = hashlib.sha256(scored.read_bytes()).hexdigest()
            edit_first_row(scored, flip_a_value)
            after = hashlib.sha256(scored.read_bytes()).hexdigest()
            manifest = view / results / "MANIFEST.sha256"
            manifest.write_text(manifest.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")

        self.fails(
            "langgraph_reproduce",
            f"{results}/scored_x.jsonl",
            mutate,
            also=(f"{results}/MANIFEST.sha256",),
            expect="scored_x.jsonl byte-identical",
        )

    def test_langgraph_reproduce_without_the_post_hoc_records(self):
        """A run of reproduce.py that skips the post hoc section is not a pass."""
        results = "experiments/harness/n8n-langgraph-7a3fcec/langgraph/results"
        self.fails(
            "langgraph_reproduce",
            f"{results}/expA-round1/MANIFEST.sha256",
            lambda p, view: (view / results / "expA-round1-rev6-posthoc").unlink(),
            expect="skipped",
        )

    def test_n8n_restart_verdicts(self):
        path = "experiments/n8n-restart/results/2026-10-29-fresh-vm/out/p1-20261029T182735Z.txt"
        self.fails("n8n_restart_verdicts", path, lambda p: edit_text(p, "P1 REPRODUCED", "P1 NOT REPRODUCED"))

    def test_n8n_restart_updates(self):
        path = "experiments/n8n-restart/results/2026-10-29-fresh-vm/logs/sink.jsonl"
        self.fails("n8n_restart_updates", path, lambda p: p.write_text("".join(p.read_text().splitlines(True)[:-1])))

    def test_client_probe_recheck(self):
        path = "experiments/claude-code-codex/records/20261025T142258Z/check.json"
        self.fails("client_probe_recheck", path, lambda p: p.write_bytes(p.read_bytes().replace(b"true", b"false", 1)))

    def test_loop_evidence(self):
        path = "experiments/docs/n8n-loop-evidence/rows.jsonl"
        self.fails("loop_evidence", path, lambda p: p.write_text("".join(p.read_text().splitlines(True)[1:])))

    def test_walkthroughs(self):
        path = "experiments/docs/walkthroughs/N8N.md"
        self.fails("walkthroughs", path, lambda p: edit_text(p, "Task cancelled", "Task canceled"))

    def test_selftest_recheck(self):
        """A changed verdict, with the run's SHA256SUMS sealed again, is caught by rescoring the evidence."""
        run = "validation/selftest-20261027T091012Z-70406a/run-01-none"

        def mutate(record: Path, view: Path) -> None:
            edit_text(record, '"verdict": "still running"', '"verdict": "ended"')
            sys.path.insert(0, str(ROOT / "src"))
            try:
                from stopkit import protocol
            finally:
                sys.path.pop(0)
            (view / run / "SHA256SUMS").write_text(protocol._sums(str(view / run)), encoding="utf-8")

        self.fails(
            "selftest_recheck",
            f"{run}/record.json",
            mutate,
            also=(f"{run}/SHA256SUMS",),
            expect="verdict: recorded ended, evidence gives still running",
        )

    def test_every_check_has_a_mutation_test(self):
        tested = {
            m.removeprefix("test_")
            for m in dir(self)
            if m.startswith("test_") and m.removeprefix("test_") in checks.CHECKS
        }
        self.assertEqual(set(checks.CHECKS) - tested, set())


class FailClosedTests(unittest.TestCase):
    def test_a_failing_build_writes_nothing(self):
        path = "experiments/n8n-restart/results/2026-10-29-fresh-vm/logs/sink.jsonl"
        with tempfile.TemporaryDirectory() as tmp:
            view = mirror(Path(tmp), [path])
            (view / path).write_bytes((view / path).read_bytes() + b"\n")
            out = Path(tmp) / "results.json"
            p = subprocess.run(
                [sys.executable, "-B", str(BUILDER), "--root", str(view), "--out", str(out)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p.returncode, 1)
            self.assertFalse(out.exists())
            self.assertRegex(p.stdout, r"\n13 of 14 checks passed\.")


class LangGraphCompletionTests(unittest.TestCase):
    """The Windows line-ending exception is accepted only from a completed run of reproduce.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.result_dir = Path(self.tmp.name, "langgraph", "results", "expA-round1")
        self.result_dir.mkdir(parents=True)
        manifest = []
        for name in ("naive_checks.md", "naive_checks.json"):
            self.result_dir.joinpath(name).write_bytes(b"original content\n")
            digest = hashlib.sha256(b"original content\r\n").hexdigest()
            manifest.append(f"{digest}  {name}\n")
        self.result_dir.joinpath("MANIFEST.sha256").write_text("".join(manifest), encoding="utf-8")

    def accepted(self, returncode, stdout, stderr=""):
        process = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with (
            patch.object(checks, "run", return_value=process),
            patch.object(checks, "N8N_LANGGRAPH_CODE", self.tmp.name),
            patch.object(checks, "LANGGRAPH_CHECKS", 1),
            patch.object(checks, "report") as report,
        ):
            checks.langgraph_reproduce()
        return report.call_args.args[1]

    @staticmethod
    def known_failure_output():
        return (
            "  ok   scorer exit status: 0\n"
            "  FAIL sha256 naive_checks.md: different\n"
            "  FAIL sha256 naive_checks.json: different\n"
            "2 CHECK(S) FAILED: sha256 naive_checks.md; sha256 naive_checks.json\n"
        )

    def test_completed_success(self):
        self.assertTrue(self.accepted(0, "  ok   scorer exit status: 0\nALL CHECKS PASS\n"))

    def test_crash_without_fail_lines_is_not_success(self):
        self.assertFalse(self.accepted(1, "", "Traceback: import failed\n"))

    def test_early_exit_after_successful_checks_is_not_success(self):
        self.assertFalse(self.accepted(0, "  ok   scorer exit status: 0\n"))

    def test_crash_after_known_failure_is_not_success(self):
        output = "  ok   scorer exit status: 0\n  FAIL sha256 naive_checks.md: different\n"
        self.assertFalse(self.accepted(1, output, "Traceback: interrupted check\n"))

    def test_failure_exit_with_success_footer_is_not_success(self):
        self.assertFalse(self.accepted(1, "  ok   scorer exit status: 0\nALL CHECKS PASS\n"))

    def test_a_skipped_section_is_not_success(self):
        self.assertFalse(self.accepted(0, "  ok   scorer exit status: 0\n  skip: not committed yet\nALL CHECKS PASS\n"))

    def test_fewer_checks_than_expected_is_not_success(self):
        out = "  ok   scorer exit status: 0\n  ok   another\nALL CHECKS PASS\n"
        self.assertFalse(self.accepted(0, out))

    def test_completed_line_ending_exception_is_accepted(self):
        self.assertTrue(self.accepted(1, self.known_failure_output()))

    def test_changed_content_is_not_a_line_ending_exception(self):
        self.result_dir.joinpath("naive_checks.md").write_bytes(b"changed content\n")
        self.assertFalse(self.accepted(1, self.known_failure_output()))

    def test_unknown_failure_is_not_accepted(self):
        output = "  ok   scorer exit status: 0\n  FAIL scorer exit status: 1\n1 CHECK(S) FAILED: scorer exit status\n"
        self.assertFalse(self.accepted(1, output))

    def test_inconsistent_completion_summary_is_not_accepted(self):
        output = self.known_failure_output().replace("2 CHECK(S)", "1 CHECK(S)")
        self.assertFalse(self.accepted(1, output))


class ResultsFileTests(unittest.TestCase):
    def test_every_check_is_listed_in_order(self):
        results = json.loads((ROOT / "experiments" / "results" / "results.json").read_text())
        self.assertEqual(results["checks"]["checks"], [checks.CHECKS[n] for n, _, _ in checks.ORDER])
        self.assertEqual(list(results["counts"]), [n for n, _, _ in checks.ORDER])

    def test_the_runbook_lists_every_check(self):
        runbook = (ROOT / "experiments/RUNBOOK.md").read_text()
        missing = [n for n in checks.CHECKS if not re.search(rf"\| `{n}` \|", runbook)]
        self.assertEqual(missing, [])


class CheckPagesTests(unittest.TestCase):
    """experiments/scripts/check_pages.py refuses a page whose quoted line is not the line its record holds."""

    def test_a_changed_quoted_line_fails(self):
        page = ROOT / "experiments" / "docs" / "walkthroughs" / "N8N.md"
        records = ROOT / "experiments" / "n8n-queue-mode" / "records" / "cb1013"
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "N8N.md"
            text = page.read_text(encoding="utf-8")
            line = next(
                ln for ln in text.splitlines() if ln.strip().startswith("Task cancelled: Task cancelled by user")
            )
            copy.write_text(text.replace(line, line.replace("by user", "by usr"), 1), encoding="utf-8")
            p = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "experiments" / "scripts" / "check_pages.py"),
                    str(copy),
                    str(records),
                ],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertNotEqual(p.returncode, 0, p.stdout)
            self.assertIn("page shows", p.stdout)


if __name__ == "__main__":
    unittest.main()
