"""Round 1's record must not drift: the archived fifth-revision scorer, rerun on round 1's committed raw files in a
scratch folder, reproduces the committed scored_x.jsonl byte for byte; and the committed files match their
manifest. (reproduce.py runs the same checks plus every quoted number.)"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LG = os.path.join(ROOT, "langgraph")
R1 = os.path.join(LG, "results", "expA-round1")
RAW = ["trials.jsonl", "pings.jsonl", "run_meta.json", "mocks/outside.jsonl", "mocks/issuer.jsonl"]


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Round1Record(unittest.TestCase):
    def test_manifest(self):
        bad = []
        for line in open(os.path.join(R1, "MANIFEST.sha256"), encoding="utf-8"):
            if line.strip():
                h, name = line.split(None, 1)
                if sha(os.path.join(R1, name.strip().lstrip("*"))) != h:
                    bad.append(name.strip())
        self.assertEqual(bad, [])

    def test_rev5_rerun_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "mocks"))
            for r in RAW:
                shutil.copy2(os.path.join(R1, r), os.path.join(tmp, r))
            p = subprocess.run([sys.executable, "-B", os.path.join(LG, "archive", "analyze_x_rev5.py"), "--res", tmp,
                                "--cells", os.path.join(ROOT, "predictions", "langgraph-expA-cells.json")],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr[-1000:])
            self.assertEqual(sha(os.path.join(tmp, "scored_x.jsonl")), sha(os.path.join(R1, "scored_x.jsonl")))

    def test_archived_scorer_unchanged(self):
        self.assertEqual(sha(os.path.join(LG, "archive", "analyze_x_rev5.py"))[:12], "f28e692ce6a1")


if __name__ == "__main__":
    unittest.main()
