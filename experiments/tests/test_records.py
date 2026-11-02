"""Tests of the shipped records against experiments/data/MANIFEST.json and the substitutions it lists."""

from __future__ import annotations

import gzip
import io
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "experiments" / "data" / "MANIFEST.json").read_text())
ARCHIVE = ROOT / "experiments" / "data" / "source" / "archive.zip"
HEX = re.compile(rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def builder():
    """experiments/scripts/build_data.py, imported."""
    sys.path.insert(0, str(ROOT / "experiments" / "scripts"))
    try:
        import build_data
    finally:
        sys.path.pop(0)
    return build_data


class ManifestTests(unittest.TestCase):
    def test_every_placed_file_is_tracked(self):
        missing = sorted(set(MANIFEST["files"]) - set(tracked()))
        self.assertEqual(missing, [], "files the manifest lists are not tracked (an ignore rule hides them?)")

    def test_every_source_file_is_accounted_for(self):
        placed = [
            e for e in MANIFEST["files"].values() if not e["from"].startswith("harness/stopcheck-history.bundle ")
        ]
        total = len(placed) + len(MANIFEST["written_into"]) + len(MANIFEST["not_shipped"])
        self.assertEqual(total, MANIFEST["source"]["files"])

    def test_the_manifest_holds_no_digest_of_a_replaced_value(self):
        """A digest of a short value, such as a project number or an address, gives the value back."""
        extra = [k for k, s in MANIFEST["substitutions"].items() if set(s) - {"description", "occurrences", "files"}]
        self.assertEqual(extra, [])
        extra = [p for p, e in MANIFEST["files"].items() if set(e) - {"from", "sha256", "bytes", "changes"}]
        self.assertEqual(extra, [])

    def test_no_file_cites_an_edited_file(self):
        """A record citing an edited file by its digest would name code that never ran."""
        b = builder()
        edited = {p: set(b.digests((ROOT / p).read_bytes())) for p in b.EDITS}
        cites = []
        for rel in tracked():
            if rel == "experiments/data/MANIFEST.json":
                continue
            tokens = set(HEX.findall((ROOT / rel).read_bytes()))
            cites += [f"{rel} cites {p}" for p, d in edited.items() if p != rel and tokens & d]
        self.assertEqual(cites, [])

    def test_every_cited_work_has_a_record(self):
        """Every arXiv identifier and GitHub issue or pull request a Markdown file cites has a manifest citation."""
        known = {c["id"] for c in MANIFEST["citations"]}
        cited = set()
        for rel in tracked():
            if rel.endswith(".md"):
                text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
                cited |= {"arXiv:" + m for m in re.findall(r"arXiv[: ](\d{4}\.\d{4,5})", text)}
                cited |= set(re.findall(r"\b([a-z0-9-]+/[a-z0-9-]+#\d+)", text))
        self.assertEqual(cited - known, set())
        for c in MANIFEST["citations"]:
            self.assertTrue(c["cited_by"], c["id"])

    def test_every_substitution_was_made(self):
        for key, s in MANIFEST["substitutions"].items():
            self.assertGreater(s["occurrences"], 0, key)
            self.assertGreater(s["files"], 0, key)


class BuilderTests(unittest.TestCase):
    def test_an_edit_to_a_cited_file_is_refused(self):
        b = builder()
        code = b"print('ran')\n"
        record = b'{"sha256": "' + b.sha(code).encode() + b'"}\n'
        with self.assertRaises(SystemExit):
            b.refuse_cited_edits({"tool.py": code, "record.json": record}, {"tool.py": []})
        b.refuse_cited_edits({"tool.py": code, "record.json": b"{}\n"}, {"tool.py": []})


@unittest.skipUnless(
    ARCHIVE.is_file(), "needs the source archive, experiments/data/source/archive.zip, which is not tracked"
)
class SourceArchiveTests(unittest.TestCase):
    """What only the source archive can show: the values and digests the substitutions replaced are gone."""

    @classmethod
    def setUpClass(cls):
        cls.b = builder()
        cls.source, cls.top = cls.b.load_source()
        cls.files = {rel: (ROOT / rel).read_bytes() for rel in tracked()}

    def test_no_file_cites_the_digest_a_changed_file_had(self):
        old = set()
        for e in MANIFEST["files"].values():
            if "changes" in e and e["from"] in self.source:
                old |= set(self.b.digests(self.source[e["from"]]))
        stale = [rel for rel, blob in self.files.items() if old & set(HEX.findall(blob))]
        self.assertEqual(stale, [])

    def test_no_file_holds_a_digest_of_a_replaced_value(self):
        values = {o for s in self.b.learn(self.source, self.top) for o in s.found}
        values |= {m.group(0) for blob in self.source.values() for m in self.b.WORKSPACE.finditer(blob)}
        hashes = {self.b.sha(v).encode() for v in values}
        self.assertTrue(hashes)
        hits = [rel for rel, blob in self.files.items() if hashes & set(HEX.findall(blob))]
        self.assertEqual(hits, [])


class SubstitutionTests(unittest.TestCase):
    def test_cloud_hosts_carry_the_neutral_project(self):
        host = re.compile(rb"\.c\.([a-z][a-z0-9-]{4,28}[a-z0-9])\.internal\b")
        projects = set()
        for rel in tracked():
            projects |= set(host.findall((ROOT / rel).read_bytes()))
        self.assertEqual(projects, {b"shutdown-test-project-00"})

    def test_worker_addresses_are_documentation_addresses(self):
        campaign = json.loads((ROOT / "experiments/n8n-queue-mode/records/cb1013/campaign.json").read_text())
        addresses = [ipaddress.ip_address(w["ip"]) for w in campaign["workers"]]
        self.assertEqual(len(addresses), 6)
        self.assertTrue(all(a in ipaddress.ip_network("198.51.100.0/24") for a in addresses))

    def test_no_home_folder_in_any_file_or_archive_member(self):
        p = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "identity_scan.py"), "--no-names"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])

    def test_no_identity_in_any_file_or_archive_member(self):
        names = os.environ.get("IDENTITY_PATTERN", "")
        if not names:
            self.skipTest("IDENTITY_PATTERN is not set")
        p = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "identity_scan.py")],
            env=dict(os.environ, NAMES=names),
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])


class IdentityScanTests(unittest.TestCase):
    """The scan finds a name in a tracked path and a Windows home folder, and refuses to run without names."""

    def scan(self, files: dict[str, bytes], *args: str, names: str = "") -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for rel, data in files.items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_bytes(data)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            env = dict(os.environ, NAMES=names)
            return subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "identity_scan.py"), "--root", str(repo), *args],
                env=env,
                capture_output=True,
                text=True,
            )

    def test_a_name_in_a_path_only_is_found(self):
        p = self.scan({"notes/zzplanted-name.txt": b"nothing here\n"}, names="zzplanted")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("(its path)", p.stdout)

    def test_a_windows_home_folder_is_found(self):
        # A home folder with no person's name; scripts/identity_scan.py lets this file, and no other, hold it.
        home = b"C:\\\\Users\\\\zzplanted\\\\x"
        p = self.scan({"record.json": b'{"cwd": "' + home + b'"}\n'}, "--no-names")
        self.assertEqual(p.returncode, 1, p.stdout)

    def test_a_name_in_an_archive_header_only_is_found(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            data = b"nothing here\n"
            info = tarfile.TarInfo("a.txt")
            info.size, info.uname = len(data), "zzplanted"
            t.addfile(info, io.BytesIO(data))
        p = self.scan({"run.tgz": gzip.compress(buf.getvalue())}, names="zzplanted")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("run.tgz", p.stdout)

    def test_no_names_and_no_flag_refuses_to_run(self):
        p = self.scan({"a.txt": b"a\n"})
        self.assertEqual(p.returncode, 2, p.stdout)


class DatabaseTests(unittest.TestCase):
    def test_every_database_passes_its_integrity_check(self):
        paths = tracked()
        files = [p for p in paths if (ROOT / p).read_bytes()[:16] == b"SQLite format 3\0"]
        self.assertTrue(files)
        for rel in files:
            folder, base = rel.rsplit("/", 1)
            stem = base.split("__")[0]
            wal = [p for p in paths if p.startswith(folder + "/") and p.rsplit("/", 1)[1].startswith(stem + "-wal")]
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "x.db"
                db.write_bytes((ROOT / rel).read_bytes())
                if wal:
                    (Path(tmp) / "x.db-wal").write_bytes((ROOT / wal[0]).read_bytes())
                con = sqlite3.connect(db)
                try:
                    result = con.execute("pragma integrity_check").fetchone()[0]
                finally:
                    con.close()
            self.assertEqual(result, "ok", rel)


if __name__ == "__main__":
    unittest.main()
