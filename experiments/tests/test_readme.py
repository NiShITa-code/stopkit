"""The figures the README states and the program output it quotes, against experiments/results/results.json and a "
"fresh run."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "experiments" / "README.md").read_text(encoding="utf-8")
COUNTS = json.loads((ROOT / "experiments" / "results" / "results.json").read_text(encoding="utf-8"))["counts"]


def quoted_blocks() -> list[list[str]]:
    """The README's plain code blocks that quote check output: the lines that start with PASS or FAIL."""
    blocks = re.findall(r"^```\n((?:PASS|FAIL).*?)\n```", README, re.M | re.S)
    return [[ln for ln in b.splitlines() if ln.startswith(("PASS", "FAIL"))] for b in blocks]


class FigureTests(unittest.TestCase):
    def test_the_loop_counts(self):
        m = re.search(
            r"Of the (\d+) loop readings in the (\d+) n8n trials, (\d+) meet this rule and (\d+) do\s+not, "
            r"(\d+) of them",
            README,
        )
        self.assertTrue(m, "the README's sentence on the loop readings is not found")
        loops = COUNTS["loop_evidence"]
        self.assertEqual(
            tuple(int(x) for x in m.groups()),
            (
                loops["loop_readings"],
                loops["trials"],
                loops["computation_end_evidence"],
                loops["unconfirmed"],
                loops["unconfirmed_without_recent_activity"],
            ),
        )

    def test_the_restart_counts(self):
        n1 = re.search(r"\$N_1 = (\d+)\$", README)
        n2 = re.search(r"\$N_2 = (\d+)\$", README)
        updates = COUNTS["n8n_restart_updates"]["updates_after_stop_reply"]
        self.assertEqual((int(n1[1]), int(n2[1])), (updates["1"], updates["2"]))

    def test_the_restart_table_matches_the_recount(self):
        """Each row of the n8n-restart table states the updates the archives of its runs hold."""
        text = (ROOT / "experiments" / "n8n-restart" / "README.md").read_text(encoding="utf-8")
        archived = COUNTS["n8n_restart_updates"]["archived_runs"]
        row = r"^\| \d+ Oct 2026 \|[^|]*\| ([\d.]+)[^|]*\|[^|]*\|[^|]*\| ([\d, ]+) \| \[`results/([^`]+)/`\]"
        rows = re.findall(row, text, re.M)
        self.assertEqual(len(rows), 7, "the table of runs is not found")
        stated = []
        for version, updates, folder in rows:
            runs = sorted(k for k in archived if k.startswith(f"{folder}/"))
            if any("/e1/" in k for k in runs):
                runs = [k for k in runs if f"/{version}-r" in k]
            self.assertEqual([int(x) for x in updates.split(",")], [archived[k] for k in runs], folder)
            stated += runs
        self.assertEqual(sorted(stated), sorted(archived))


class QuotedOutputTests(unittest.TestCase):
    def test_every_quoted_line_is_what_the_checks_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "experiments" / "scripts" / "build_results.py"),
                    "--out",
                    str(Path(tmp) / "r.json"),
                ],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        printed = set(p.stdout.splitlines())
        quoted = [ln for block in quoted_blocks() for ln in block]
        self.assertGreaterEqual(len(quoted), 14)
        self.assertEqual([ln for ln in quoted if ln not in printed], [])


if __name__ == "__main__":
    unittest.main()
