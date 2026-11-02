"""Rerun every check on the recorded experiments and compare the outcome with experiments/results/results.json.

    python3 -B experiments/verify.py

It needs only Python 3.11 or later and its standard library: no root, no Linux host and no network. The checks are in
experiments/scripts/build_results.py; experiments/RUNBOOK.md says what makes each one fail.
"""

from __future__ import annotations

import filecmp
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results" / "results.json"
BUILDER = HERE / "scripts" / "build_results.py"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        rebuilt = Path(tmp) / "results.json"
        build = subprocess.run(
            [sys.executable, "-B", str(BUILDER), "--out", str(rebuilt)],
            cwd=HERE.parent,
            capture_output=True,
            text=True,
        )
        print(build.stdout.strip() or build.stderr.strip())
        if build.returncode != 0:
            return 1
        if not RESULTS.is_file() or not filecmp.cmp(rebuilt, RESULTS, shallow=False):
            print("the rebuilt results differ from experiments/results/results.json")
            return 1
    print("the rebuild matches experiments/results/results.json byte for byte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
