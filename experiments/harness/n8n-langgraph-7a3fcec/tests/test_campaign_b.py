"""The Experiment B campaign coordinator (n8n/campaign_b.py) with fake ssh and tar workers; nothing reaches a VM.

The worker-side helper itself runs for real (POSIX only) against a temporary repository, a fake runner and a fake
tmux; it never reads or takes the deployment's launcher lock."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("campaign_b", ROOT / "n8n" / "campaign_b.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
R = C.runner_module()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def captured(function, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        result = function(*args, **kwargs)
    return result, out.getvalue()


def shuffled(conditions, seed):
    """run_expB's own order: random.Random(seed).shuffle of the --conditions list."""
    order = list(conditions)
    random.Random(seed).shuffle(order)
    return order


# A stand-in runner with only what registration asks the committed runner for: its source fingerprint.
FIXTURE_RUNNER = '''import hashlib
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def source_hashes():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "n8n").glob("*.py"))}
'''
SOURCES = {"n8n/campaign_b.py": "# fixture coordinator\n", "n8n/run_expB.py": FIXTURE_RUNNER,
           "n8n/analyze_b.py": "# fixture scorer\n", "n8n/clockwall_b.py": "# fixture clock rule\n"}
STARTED = "2026-10-14T10:00:00+00:00"


class Done:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def batch_bytes(batch):
    return (json.dumps(batch, indent=2) + "\n").encode("utf-8")   # as run_expB.write_batch


class FakeVM:
    """One worker as the coordinator reaches it over ssh: probe, manifest and launch replies, and tar streams. run()
    plays the worker's runner (run_expB.run_batch, with the runner's own settle and next_attempt)."""

    def __init__(self, fx, worker):
        self.fx, self.w = fx, worker
        self.reachable = True
        self.running = False
        self.batch = None
        self.folders = {}          # raw trial folder -> {relative path: bytes}
        self.logs = {}             # launch file -> bytes
        self.launches = []
        self.launch_reply = "ack"  # ack | refuse | timeout | noack
        self.overrides = {}        # probe key path (tuple) -> value
        self.next_trials = None    # replaces the dry run's next_trials
        self.damage = {}           # archive member -> bytes, or None to drop it
        self.extra = {}            # archive member added in transit
        self.derived = {}          # trial -> {file name: bytes} under <batch folder>/derived/<trial>/

    def batch_name(self):
        return f"XB-screen-{self.w['batch_id']}"

    def export(self, entry):
        """As run_expB.export_attempt: reports and a SHA-256 manifest of every raw file and report, outside the raw
        folder, referenced from the batch entry."""
        trial = entry["trial"]
        folders = sorted(n for n in self.folders if re.fullmatch(re.escape(trial) + C.STAMP, n))
        derived = f"{self.batch_name()}/derived/{trial}"
        reports = {"scored_b.jsonl": b'{"measure": "validity"}\n', "summary_b.md": f"report on {trial}\n".encode()}
        manifest = {"schema_version": 1, "trial": trial, "condition": entry["condition"], "attempt": entry["attempt"],
                    "created_utc": STARTED, "paths_relative_to": "n8n/results", "raw_folders": folders,
                    "raw_files": [{"path": f"{n}/{rel}", "bytes": len(data), "sha256": sha(data)}
                                  for n in folders for rel, data in sorted(self.folders[n].items())],
                    "derived_folder": derived,
                    "derived_files": [{"path": f"{derived}/{n}", "bytes": len(d), "sha256": sha(d)}
                                      for n, d in sorted(reports.items())],
                    "scoring": {"status": "scored"},
                    "scorer_sources": {f: self.fx.source_hashes[f] for f in C.SCORER_FILES}}
        data = (json.dumps(manifest, indent=1) + "\n").encode()
        self.derived[trial] = dict(reports, **{"manifest-sha256.json": data})
        entry["export"] = {"exported_utc": STARTED, "status": "exported", "raw_folders": folders,
                           "raw_files": len(manifest["raw_files"]), "scoring": "scored", "derived_folder": derived,
                           "manifest": f"{derived}/manifest-sha256.json", "manifest_sha256": sha(data)}

    def probe(self, a):
        results = a["repo"] + "/n8n/results"
        busy = self.running
        launches = {}
        for name in sorted(self.logs):
            number, suffix = name[len(self.w["name"]) + len("-launch-"):].split(".")
            item = launches.setdefault(number, {"files": []})
            item["files"].append(name)
            if suffix in ("start", "exit"):
                item[suffix] = self.logs[name].decode().strip()
        batch = {"folder": f"{results}/{self.batch_name()}", "name": self.batch_name(), "exists": self.batch is not None,
                 "trial_folders": sorted(self.folders)}
        if self.batch is not None:
            batch.update(sha256=sha(batch_bytes(self.batch)), content=copy.deepcopy(self.batch),
                         lock={"path": "batch.lock", "exists": True, "held": False})
        p = {"probe": 1, "utc": STARTED, "hostname": self.w["name"], "euid": 0, "boot_id": "boot-1",
             "metadata": {"id": self.w["instance_id"], "name": self.w["name"], "zone": "zone-a", "machine_type": "m"},
             "repo": {"head": self.fx.commit, "status": "", "freeze_sha256": self.fx.freeze_sha,
                      "freeze_committed": True},
             "wsl_kernel": False, "lock": {"path": a["lock_file"], "exists": True, "held": busy},
             "processes": [{"pid": 4242, "programs": ["run_expB.py"], "cmdline": "python -B n8n/run_expB.py"}]
             if busy else [],
             "tmux": {"sessions": [a["session"]] if busy else []}, "netns": {"names": []},
             "test_user": {"user": "n8ntest", "uid": 1000, "processes": [], "crontab": False},
             "disk": {results: 40.0, "/var/lib": 40.0},
             "services": {u: {"is_enabled": "disabled", "is_active": "inactive", "ok": True}
                          for u in C.SYSTEM_SERVICES},
             "env_file": True, "ntp": "yes", "python": True, "sentinels": [],
             "trial_paths": {t: {"work_dir": False, "sentinel": False} for t in a["trials"]},
             "batch": batch,
             "screen_batches": [{"name": self.batch_name(), "freeze_sha256": self.batch.get("freeze_sha256")}]
             if self.batch else [],
             "launches": launches}
        if a.get("dry_run") and not busy:
            p["dry_run"] = self.dry_run(a)
        for path, value in self.overrides.items():
            target = p
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
        return p

    def dry_run(self, a):
        plan = {"run_kind": "screen", "timing_mode": "full", "host_profile": "linux-vm", "requires_freeze": True,
                "freeze": a["freeze"], "readiness_checked": False}
        if self.batch is None:
            return {"returncode": 0, "plan": dict(plan, conditions=shuffled(a["conditions"], a["seed"])), "stderr": ""}
        if self.batch.get("completed"):
            return {"returncode": 2, "plan": None, "stderr": "REFUSED: Batch already completed"}
        batch = copy.deepcopy(self.batch)
        R.settle(batch)
        nxt = [R.trial_id("screen", batch["batch_id"], c, R.next_attempt(batch, c))
               for c in batch["conditions"] if R.next_attempt(batch, c)]
        return {"returncode": 0, "stderr": "",
                "plan": dict(plan, conditions=batch["conditions"],
                             next_trials=nxt if self.next_trials is None else self.next_trials)}

    def members(self, root, name):
        if root.endswith("/n8n/results"):
            if name == self.batch_name():
                files = {f"{name}/derived/{trial}/{f}": data for trial, reports in self.derived.items()
                         for f, data in reports.items()}
                return dict(files, **{f"{name}/batch.json": batch_bytes(self.batch), f"{name}/batch.lock": b""})
            return {f"{name}/{rel}": data for rel, data in self.folders[name].items()}
        return {name: self.logs[name]}

    def manifest(self, a):
        items = [{"root": item["root"], "name": item["name"], "other": [],
                  "files": {k: sha(v) for k, v in self.members(item["root"], item["name"]).items()}}
                 for item in a["items"]]
        return {"utc": STARTED, "processes": [], "lock": {"path": a["lock_file"], "held": False},
                "tmux": {"sessions": []}, "items": items}

    def tar(self, root, names):
        members = {}
        for name in names:
            members.update(self.members(root, name))
        for path, data in self.damage.items():
            if path in members:
                if data is None:
                    del members[path]
                else:
                    members[path] = data
        members.update({k: v for k, v in self.extra.items() if k.split("/")[0] in names})
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path, data in sorted(members.items()):
                info = tarfile.TarInfo(path)
                info.size, info.mtime = len(data), 0
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def launch(self, a):
        self.launches.append(a)
        if self.launch_reply == "refuse":
            return {"ok": False, "refused": ["fixture: launch 1 of w1 already has files: a duplicate request"]}
        base = f"{a['worker']}-launch-{a['launch_no']}"
        self.logs[base + ".sh"] = b"#!/bin/sh\n"
        self.logs[base + ".start"] = b"2026-10-14T10:00:00Z\n"
        self.running = True
        if self.launch_reply == "noack":
            return {"ok": False, "ack": False, "error": "no start stamp"}
        return {"ok": True, "ack": True, "session": a["session"], "start_utc": "2026-10-14T10:00:00Z",
                "argv": ["/root/pilot-venv/bin/python", "-B", "n8n/run_expB.py"]}

    def record(self, trial, condition, folder):
        return {"trial": trial, "condition": condition, "run_kind": "screen", "schema_version": 7,
                "host_profile": {"declared": "linux-vm", "variable": "STOPCHECK_HOST_PROFILE"},
                "freeze_manifest_sha256": self.fx.freeze_sha,
                "batch": {"verified": True, "freeze_sha256": self.fx.freeze_sha},
                "source_hashes": dict(self.fx.source_hashes),
                "host_identity": {"status": "recorded", "values": {"id": self.w["instance_id"]}}}

    def run(self, **outcomes):
        """The runner after the last launch: each condition 'valid', 'invalid' or 'killed' (the runner dies while
        the trial is pending, as on a VM restart)."""
        a = self.launches[-1]
        if self.batch is None:
            self.batch = {"schema_version": 1, "run_kind": "screen", "batch_id": a["batch_id"],
                          "conditions": shuffled(a["conditions"], a["seed"]), "seed": a["seed"],
                          "host_profile": "linux-vm", "source_hashes": dict(self.fx.source_hashes), "trials": [],
                          "incomplete": [], "completed": False, "stopped_reason": None, "stops": [],
                          "freeze_sha256": self.fx.freeze_sha}
        R.settle(self.batch)
        for entry in self.batch["trials"]:            # run_expB.export_pending
            if "export" not in entry and entry["status"] in ("finished", "interrupted", "unrecorded"):
                self.export(entry)
        self.batch["stopped_reason"], code, killed = None, 0, False
        for condition in self.batch["conditions"]:
            attempt = R.next_attempt(self.batch, condition)
            if attempt is None:
                continue
            trial = R.trial_id("screen", self.batch["batch_id"], condition, attempt)
            entry = {"trial": trial, "condition": condition, "attempt": attempt, "status": "pending",
                     "exit_code": None, "started_utc": STARTED,
                     "host_identity": {"status": "recorded", "values": {"id": self.w["instance_id"]}}}
            if attempt > 1:
                entry["rerun_of"] = [t for t in self.batch["trials"] if t["condition"] == condition][-1]["trial"]
            self.batch["trials"].append(entry)
            folder = f"{trial}-20261014-100000-{len(self.batch['trials']):09d}"
            self.folders[folder] = {"record.json": json.dumps(self.record(trial, condition, folder)).encode(),
                                    "pings.jsonl": b"{}\n", "logs/main.log": b"main\n"}
            outcome = outcomes.get(condition, "valid")
            if outcome == "killed":
                killed = True
                break
            valid = outcome == "valid"
            entry.update(status="finished", exit_code=0 if valid else 2, ended_utc="2026-10-14T10:20:00+00:00",
                         record=f"/root/stopcheck/n8n/results/{folder}", scored_valid=valid,
                         validity="valid" if valid else "INVALID/void: fixture", accepted=valid)
            self.export(entry)
            if not valid:
                if attempt >= R.ATTEMPTS:
                    self.batch["incomplete"].append(condition)
                self.batch["stopped_reason"], code = f"{trial} returned 2", 2
                break
        else:
            self.batch["completed"] = True
        base = f"{a['worker']}-launch-{a['launch_no']}"
        self.logs[base + ".log"] = b"runner output\n"
        if not killed:
            self.logs[base + ".exit"] = f"{code} 2026-10-14T10:30:00Z\n".encode()
        self.running = False


class FakeWorld:
    """Stands in for ssh (and tar over ssh), the only commands the coordinator runs through its runner."""

    def __init__(self):
        self.vms, self.calls = {}, []

    def run(self, argv, input=None, timeout=None, stdout_path=None):
        assert argv[0] == "ssh", argv
        self.calls.append(argv)
        vm = self.vms[argv[-2].split("@", 1)[1]]
        if not vm.reachable:
            return Done(255, b"", b"ssh: connect to host port 22: Connection timed out")
        words = shlex.split(argv[-1])
        if words[:7] == ["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-", "remote"]:
            assert input and b"def remote_probe" in input, "the worker-side helper is this coordinator file"
            op, args = words[7], json.loads(words[8])
            if op == "launch" and vm.launch_reply == "timeout":
                vm.launch(args)
                raise subprocess.TimeoutExpired(argv, timeout)
            reply = {"probe": vm.probe, "manifest": vm.manifest, "launch": vm.launch}[op](args)
            return Done(0, (json.dumps(reply) + "\n").encode(), b"")
        if words[:3] == ["sudo", "-n", "tar"]:
            data = vm.tar(words[words.index("-C") + 1], words[words.index("--") + 1:])
            with open(stdout_path, "xb") as out:
                out.write(data)
            return Done(0, None, b"")
        return Done(127, b"", b"fake ssh: unknown command")

    def count(self, op):
        return sum(1 for argv in self.calls if f" remote {op} " in argv[-1] or (op == "tar" and " tar " in argv[-1]))


TEMPLATE = {}


def tearDownModule():
    if TEMPLATE:
        TEMPLATE["temp"].cleanup()


@unittest.skipUnless(shutil.which("git"), "requires git")
class Fixture(unittest.TestCase):
    """A committed fixture checkout and freeze, a results root and fake workers; holds no tests of its own."""
    CONDITIONS = ["B0", "PC", "B2", "B7"]
    ASSIGNMENT = {"w1": ["B0", "B7"], "w2": ["PC", "B2"]}

    def template(self):
        """The fixture checkout, committed once per run and copied into each test (git is slow on Windows)."""
        if not TEMPLATE:
            TEMPLATE["temp"] = tempfile.TemporaryDirectory(prefix="campaign-b-template-", ignore_cleanup_errors=True)
            self.repo = Path(TEMPLATE["temp"].name).resolve() / "repo"
            (self.repo / "n8n").mkdir(parents=True)
            for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                         ("config", "user.name", "fixture"), ("config", "core.autocrlf", "false")):
                self.git(*args)
            for rel, text in SOURCES.items():
                (self.repo / rel).write_text(text, encoding="utf-8", newline="\n")
            hashes = {rel: sha((self.repo / rel).read_bytes()) for rel in SOURCES}
            self.write_freeze(hashes)
            TEMPLATE.update(repo=self.repo, commit=self.commit, freeze_sha=self.freeze_sha, hashes=hashes)
        return TEMPLATE

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="campaign-b-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        template = self.template()
        self.repo = self.base / "repo"
        shutil.copytree(template["repo"], self.repo)
        self.commit, self.freeze_sha = template["commit"], template["freeze_sha"]
        self.source_hashes = dict(template["hashes"])
        self.root = self.base / "cloud-results"
        self.spec = {"campaign_id": "t1", "source_commit": self.commit, "assignment": self.ASSIGNMENT,
                     "workers": [{"name": "w1", "ip": "10.0.0.1", "instance_id": "111"},
                                 {"name": "w2", "ip": "10.0.0.2", "instance_id": "222"}]}
        self.world = FakeWorld()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def write_freeze(self, hashes, **changes):
        freeze = dict({"schema_version": 1, "timing_mode": "full", "conditions": self.CONDITIONS,
                       "host_profile": "linux-vm", "prediction_file": "predictions/n8n-expB-cells.json",
                       "prediction_sha256": "cd" * 32, "source_hashes": hashes}, **changes)
        path = self.repo / "n8n" / "FREEZE-expB.json"
        path.write_text(json.dumps(freeze, indent=2) + "\n", encoding="utf-8", newline="\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture freeze")
        self.commit, self.freeze_sha = self.git("rev-parse", "HEAD"), sha(path.read_bytes())
        if hasattr(self, "spec"):
            self.spec["source_commit"] = self.commit

    def register(self, **changes):
        path = self.base / "spec.json"
        path.write_text(json.dumps(dict(self.spec, **changes)), encoding="utf-8")
        self.folder, _ = captured(C.register, path, self.root, self.repo)
        for w in self.camp().data["workers"]:
            self.world.vms[w["ip"]] = FakeVM(self, w)
        return self.folder

    def camp(self):
        return C.Campaign(self.folder, self.repo)

    def vm(self, name):
        return self.world.vms[self.camp().workers[name]["ip"]]

    def cli(self, command, *extra):
        return captured(C.main, [command, str(self.folder), *extra], self.world, self.repo)

    def entries(self, kind=None, worker=None):
        return [e for e in C.Ledger(self.folder, self.camp().fingerprint).entries()
                if (kind is None or e["type"] == kind) and (worker is None or e.get("worker") == worker)]

    def state(self, worker):
        return C.worker_state(self.entries(), worker)

    def attempts(self, condition=None):
        return [a for a in C.latest_attempts(self.entries()).values() if condition in (None, a["condition"])]

    def launch_run_collect(self, worker, **outcomes):
        code, out = self.cli("launch", "--worker", worker)
        self.assertEqual(code, 0, out)
        self.vm(worker).run(**outcomes)
        code, out = self.cli("collect", "--worker", worker)
        self.assertEqual(code, 0, out)


class Assignment(unittest.TestCase):
    def test_balanced_assignment_spreads_the_long_recovery_conditions(self):
        workers = [f"w{i}" for i in range(1, 7)]
        minutes = {c: C.MINUTES["long"] if c in C.LONG_CONDITIONS else C.MINUTES["other"] for c in R.CONDITIONS}
        lists = C.balanced_assignment(list(R.CONDITIONS), workers, minutes)
        self.assertEqual(sorted(c for w in workers for c in lists[w]), sorted(R.CONDITIONS))
        loads = [sum(minutes[c] for c in lists[w]) for w in workers]
        longs = sorted(sum(c in C.LONG_CONDITIONS for c in lists[w]) for w in workers)
        # seven long conditions on six workers: one worker takes two, and no worker exceeds the best possible 52 min
        self.assertEqual(longs, [1, 1, 1, 1, 1, 2])
        self.assertEqual(max(loads), 52)


class Registration(Fixture):
    def test_registration_writes_the_campaign_its_fingerprint_and_the_first_ledger_entry(self):
        folder = self.register()
        raw = (folder / "campaign.json").read_bytes()
        fingerprint = json.loads((folder / "registration.json").read_text(encoding="utf-8"))["campaign_sha256"]
        self.assertEqual(fingerprint, sha(raw))
        first = self.entries()[0]
        self.assertEqual((first["type"], first["campaign_sha256"], first["freeze_sha256"], first["source_commit"]),
                         ("registered", fingerprint, self.freeze_sha, self.commit))
        data = json.loads(raw)
        self.assertEqual([(w["batch_id"], w["session"]) for w in data["workers"]],
                         [("t1-w1", "sc-t1-w1"), ("t1-w2", "sc-t1-w2")])
        self.assertEqual(data["host_profile"], "linux-vm")
        self.assertEqual((folder / "freeze" / "FREEZE-expB.json").read_bytes(),
                         (self.repo / "n8n" / "FREEZE-expB.json").read_bytes())

    def test_a_changed_campaign_file_is_refused_by_every_later_command(self):
        folder = self.register()
        path = folder / "campaign.json"
        path.write_text(path.read_text(encoding="utf-8").replace("10.0.0.1", "10.0.0.9"), encoding="utf-8")
        for command in ("preflight", "launch", "status", "collect"):
            code, out = self.cli(command)
            self.assertEqual(code, 2, command)
            self.assertIn("changed since registration", out)
        self.assertEqual(self.world.calls, [])

    def test_each_worker_order_is_the_runners_own_shuffle_of_its_conditions(self):
        self.register(seed=7)
        for w in self.camp().data["workers"]:
            self.assertEqual(w["conditions"], [c for c in R.CONDITIONS if c in self.ASSIGNMENT[w["name"]]])
            self.assertEqual(w["order"], shuffled(w["conditions"], 7))

    def test_every_frozen_condition_must_be_assigned_exactly_once(self):
        for assignment in ({"w1": ["B0", "B7"], "w2": ["PC"]}, {"w1": ["B0", "B7"], "w2": ["PC", "B2", "B7"]},
                           {"w1": ["B0", "PC", "B2", "B7"], "w2": []}, {"w1": ["B0", "B7", "B1"], "w2": ["PC", "B2"]}):
            with self.subTest(assignment=assignment), self.assertRaises(C.CampaignError):
                self.register(assignment=assignment)
        self.assertFalse((self.root / "t1").exists())

    def test_without_an_assignment_the_long_conditions_are_balanced(self):
        spec = dict(self.spec)
        del spec["assignment"]
        self.spec = spec
        self.register()
        lists = {w["name"]: w["conditions"] for w in self.camp().data["workers"]}
        # B7 (25 min) first, to w1; then each 9-minute condition to the less-loaded worker
        self.assertEqual(lists, {"w1": ["B7"], "w2": ["B0", "PC", "B2"]})
        self.assertEqual([w["expected_minutes"] for w in self.camp().data["workers"]], [25, 27])

    def test_a_freeze_that_does_not_bind_the_committed_coordinator_is_refused(self):
        (self.repo / "n8n" / "campaign_b.py").write_text("# a later coordinator\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "coordinator changed after the freeze")
        self.spec["source_commit"] = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(C.CampaignError, "does not bind n8n/campaign_b.py"):
            self.register()

    def test_a_commit_whose_code_is_not_the_frozen_code_is_refused(self):
        (self.repo / "n8n" / "extra.py").write_text("# added after the freeze\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a source file added after the freeze")
        self.spec["source_commit"] = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(C.CampaignError, r"not the code the freeze binds \(differs: n8n/extra.py\)"):
            self.register()

    def test_a_checkout_whose_frozen_files_differ_is_refused_before_and_after_registration(self):
        scorer = self.repo / "n8n" / "analyze_b.py"
        scorer.write_text("# fixture scorer, edited locally\n", encoding="utf-8")
        with self.assertRaisesRegex(C.CampaignError, "differs from the campaign commit"):
            self.register()
        scorer.write_text(SOURCES["n8n/analyze_b.py"], encoding="utf-8", newline="\n")
        self.register()
        scorer.write_text("# fixture scorer, edited locally\n", encoding="utf-8")
        code, out = self.cli("status", "--no-ssh")
        self.assertEqual(code, 2)
        self.assertIn("differ(s) from the registered frozen source", out)

    def test_malformed_spec_values_are_refused(self):
        bad_ip = [dict(w, ip="10.0.0.1 -oProxyCommand=x") if w["name"] == "w1" else w for w in self.spec["workers"]]
        for changes in ({"workers": bad_ip}, {"freeze": "../FREEZE-expB.json"}, {"seed": "7"},
                        {"assignment": {"w1": "B0 B7", "w2": ["PC", "B2"]}}, {"source_commit": "HEAD"}):
            with self.subTest(changes=changes), self.assertRaises(C.CampaignError):
                self.register(**changes)
        self.assertFalse((self.root / "t1").exists())

    def test_a_campaign_is_registered_once(self):
        self.register()
        with self.assertRaisesRegex(C.CampaignError, "registered once"):
            self.register()

    def test_a_batch_id_the_harness_would_reject_is_refused(self):
        workers = [dict(w, name="w" * 32) if w["name"] == "w1" else w for w in self.spec["workers"]]
        with self.assertRaisesRegex(C.CampaignError, "64-character"):
            self.register(campaign_id="c" * 32, workers=workers, assignment={"w" * 32: ["B0", "B7"], "w2": ["PC", "B2"]})

    def test_only_the_linux_vm_profile_is_registered(self):
        with self.assertRaisesRegex(C.CampaignError, "linux-vm"):
            self.register(host_profile="wsl")
        self.write_freeze(self.source_hashes, host_profile="wsl")
        with self.assertRaisesRegex(C.CampaignError, "binds host profile 'wsl'"):
            self.register()


class Preflight(Fixture):
    def setUp(self):
        super().setUp()
        self.register()

    def test_a_ready_worker_passes_and_the_check_is_recorded(self):
        code, out = self.cli("preflight", "--worker", "w1")
        self.assertEqual(code, 0, out)
        entry = self.entries("preflight", "w1")[-1]
        self.assertEqual((entry["ok"], entry["mode"], entry["identity"]["instance_id"]), (True, "new", "111"))

    def test_each_condition_that_must_refuse_a_launch_is_reported(self):
        cases = {("repo", "head"): ("f" * 40, "not the campaign commit"),
                 ("repo", "status"): (" M n8n/harness_b.py\n", "working tree is not clean"),
                 ("repo", "freeze_sha256"): ("0" * 64, "has SHA-256"),
                 ("repo", "freeze_committed"): (False, "not committed unchanged"),
                 ("wsl_kernel",): (True, "reads as WSL"),
                 ("lock", "held"): (True, "launcher lock"),
                 ("metadata", "id"): ("999", "VM identity"),
                 ("processes",): ([{"pid": 7, "programs": ["harness_b.py"], "cmdline": "harness_b.py B0"}],
                                  "StopCheck programs are running"),
                 ("tmux", "sessions"): (["sc-t1-w9"], "campaign tmux session"),
                 ("netns", "names"): (["stopcheck-b-77"], "network namespaces"),
                 ("test_user", "crontab"): (True, "crontab"),
                 ("test_user", "processes"): ([1234], "has processes"),
                 ("disk", "/var/lib"): (9.5, "disk floor"),
                 ("services", "redis-server.service", "ok"): (False, "redis-server.service"),
                 ("env_file",): (False, "n8n-test.env"),
                 ("dry_run", "plan", "conditions"): (["B0"], "differs from the registered order"),
                 ("trial_paths", "XB-screen-t1-w1-B0", "work_dir"): (True, "work directory or host sentinel")}
        for path, (value, text) in cases.items():
            with self.subTest(path=path):
                self.vm("w1").overrides = {path: value}
                code, out = self.cli("preflight", "--worker", "w1")
                self.assertEqual(code, 1, out)
                self.assertIn(text, out)
                self.assertFalse(self.entries("preflight", "w1")[-1]["ok"])
                code, out = self.cli("launch", "--worker", "w1")
                self.assertEqual(code, 1)
                self.assertEqual(self.vm("w1").launches, [])

    def test_an_unreachable_worker_is_not_ready(self):
        self.vm("w2").reachable = False
        code, out = self.cli("preflight")
        self.assertEqual(code, 1)
        self.assertIn("w1: ready", out)
        self.assertIn("unreachable", out)


class Launch(Fixture):
    def setUp(self):
        super().setUp()
        self.register()

    def test_a_launch_records_the_request_then_the_acknowledgement(self):
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 0, out)
        self.assertEqual([e["type"] for e in self.entries(worker="w1")], ["preflight", "launch_request", "launch_ack"])
        args, w = self.vm("w1").launches[-1], self.camp().workers["w1"]
        self.assertEqual((args["mode"], args["batch_id"], args["conditions"], args["seed"], args["launch_no"],
                          args["host_profile"]), ("new", "t1-w1", w["conditions"], C.DEFAULT_SEED, 1, "linux-vm"))
        self.assertEqual(self.entries("launch_request", "w1")[-1]["expected_trials"],
                         [R.trial_id("screen", "t1-w1", c, 1) for c in w["order"]])
        self.assertEqual(self.state("w1"), "running")

    def test_a_running_worker_is_refused_from_the_ledger_without_contacting_it(self):
        self.cli("launch", "--worker", "w1")
        calls = len(self.world.calls)
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("it is running", out)
        self.assertEqual(len(self.world.calls), calls)
        self.assertEqual(len(self.vm("w1").launches), 1)

    def lost_acknowledgement(self, reply):
        self.vm("w1").launch_reply = reply
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertEqual(self.entries(worker="w1")[-1]["type"], "launch_unresolved")
        for command in ("launch", "collect"):
            code, out = self.cli(command, "--worker", "w1")
            self.assertEqual(code, 1)
            self.assertIn("reconcile first", out)
        self.assertEqual(len(self.vm("w1").launches), 1)

    def test_an_ssh_timeout_during_the_launch_leaves_the_worker_unresolved_until_reconcile(self):
        self.lost_acknowledgement("timeout")

    def test_a_launch_without_a_start_stamp_leaves_the_worker_unresolved_until_reconcile(self):
        self.lost_acknowledgement("noack")

    def test_live_activity_refuses_a_launch_the_ledger_allows(self):
        self.vm("w1").overrides = {("lock", "held"): True}
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("launcher lock", out)
        self.assertEqual(self.entries("launch_request"), [])
        self.assertEqual(self.vm("w1").launches, [])

    def test_a_worker_refusal_starts_nothing_and_restores_the_state(self):
        self.vm("w1").launch_reply = "refuse"
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertEqual((self.entries(worker="w1")[-1]["type"], self.state("w1")), ("launch_refused", "idle"))
        self.vm("w1").launch_reply = "ack"
        self.vm("w1").running = False
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.vm("w1").launches[-1]["launch_no"], 2)

    def test_a_stopped_worker_is_relaunched_only_after_a_verified_collect(self):
        self.cli("launch", "--worker", "w1")
        self.vm("w1").run(B7="invalid")
        code, out = self.cli("launch", "--worker", "w1")
        self.assertIn("then collect", out)
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 0, out)
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.vm("w1").launches[-1]["mode"], "resume")
        self.assertEqual(self.entries("launch_request", "w1")[-1]["expected_trials"][0], "XB-screen-t1-w1-B7-a2")

    def test_a_batch_changed_since_its_collect_is_not_resumed(self):
        self.launch_run_collect("w1", B7="invalid")
        self.vm("w1").batch["stops"].append({"utc": STARTED, "reason": "changed on the worker after the collect"})
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("collect first", out)


class GlobalAttemptLimit(Fixture):
    def setUp(self):
        super().setUp()
        self.register()

    def test_a_replacement_resumes_the_same_batch_on_the_same_worker(self):
        self.launch_run_collect("w1", B7="invalid")
        self.launch_run_collect("w1")
        b7 = self.attempts("B7")
        self.assertEqual([(a["trial"], a["attempt"], a["global_attempt"], a["rerun_of"], a["accepted"]) for a in b7],
                         [("XB-screen-t1-w1-B7", 1, 1, None, False),
                          ("XB-screen-t1-w1-B7-a2", 2, 2, "XB-screen-t1-w1-B7", True)])
        self.assertEqual({a["batch_id"] for a in b7}, {"t1-w1"})

    def test_no_attempt_beyond_the_limit_even_when_the_worker_would_start_one(self):
        self.launch_run_collect("w1", B7="invalid")
        self.launch_run_collect("w1", B7="invalid")
        self.assertEqual(len(self.attempts("B7")), 2)
        vm = self.vm("w1")
        vm.next_trials = ["XB-screen-t1-w1-B7-a3"]     # a runner that would start a third attempt
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("campaign-wide limit of 2 attempts is reached", out)
        vm.next_trials = None                         # a batch.json restored from before both attempts
        vm.batch["trials"] = [t for t in vm.batch["trials"] if t["condition"] != "B7"]
        vm.batch["incomplete"], vm.batch["completed"], vm.batch["stopped_reason"] = [], False, None
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("no longer lists attempts the ledger recorded", out)
        self.assertIn("limit of 2 attempts is reached", out)
        self.assertEqual(len(vm.launches), 2)

    def test_an_attempt_on_another_worker_counts_toward_the_limit(self):
        vm2 = self.vm("w2")    # a stray batch on w2 that also ran B7, registered to w1
        vm2.launches.append({"worker": "w2", "launch_no": 1, "batch_id": "t1-w2", "conditions": ["PC", "B2", "B7"],
                             "seed": C.DEFAULT_SEED})
        vm2.run()
        code, out = self.cli("collect", "--worker", "w2")
        self.assertEqual(code, 0, out)
        stray = self.attempts("B7")
        self.assertEqual([(a["worker"], a["accepted"]) for a in stray], [("w2", False)])
        self.assertTrue(any("batch.json conditions" in r for r in stray[0]["reasons"]))
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("the ledger holds 1", out)
        self.assertEqual(self.vm("w1").launches, [])


class Collect(Fixture):
    def setUp(self):
        super().setUp()
        self.register()
        self.cli("launch", "--worker", "w1")
        self.vm("w1").run()

    def test_a_verified_copy_admits_the_attempts_the_runner_accepted(self):
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 0, out)
        folder = self.folder / "w1" / "collect-001"
        lines = (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines)
        for line in lines:
            digest, rel = line.split("  ", 1)
            self.assertEqual(sha((folder / rel).read_bytes()), digest)
        self.assertTrue((folder / "launch" / "w1-launch-1.exit").read_text().startswith("0 "))
        attempts = self.attempts()
        self.assertEqual(sorted(a["condition"] for a in attempts), ["B0", "B7"])
        for a in attempts:
            self.assertTrue(a["accepted"] and a["runner_accepted"], a["reasons"])
            self.assertEqual((a["job"], a["instance_id"], a["batch_id"], a["status"], a["exit_code"]),
                             (f"t1-{a['condition']}", "111", "t1-w1", "finished", 0))
            raw = self.folder / a["raw_folder"]
            manifest = folder / "manifests" / f"results__{raw.name}.sha256"
            self.assertEqual(a["raw_manifest_sha256"], sha(manifest.read_bytes()))
            self.assertEqual(a["record_sha256"], sha((raw / "record.json").read_bytes()))
            # the runner's own export, collected with the batch folder, outside the raw folder
            entry = next(t for t in self.vm("w1").batch["trials"] if t["trial"] == a["trial"])
            self.assertEqual(a["export_manifest_sha256"], entry["export"]["manifest_sha256"])
            self.assertEqual(a["derived_report"], f"w1/collect-001/results/XB-screen-t1-w1/derived/{a['trial']}")
            self.assertTrue((self.folder / a["derived_report"] / "summary_b.md").is_file())
            self.assertEqual(a["derived_by"], {f: self.source_hashes[f] for f in C.SCORER_FILES})
        self.assertEqual(self.state("w1"), "stopped")

    def test_raw_files_that_differ_from_the_runners_export_are_not_admitted(self):
        vm = self.vm("w1")
        folder = sorted(vm.folders)[0]
        for label, change in (("rewritten", ("logs/main.log", b"rewritten after the export\n")),
                              ("added", ("logs/added.log", b"added after the export\n"))):
            with self.subTest(change=label):
                original = dict(vm.folders[folder])
                vm.folders[folder][change[0]] = change[1]
                code, out = self.cli("collect", "--worker", "w1")
                self.assertEqual(code, 0, out)               # the copy matches the worker as it is now
                attempt = next(a for a in self.attempts() if a["raw_folder"].endswith(folder))
                self.assertTrue(attempt["runner_accepted"])
                self.assertFalse(attempt["accepted"])
                self.assertIn(f"differs from the runner's export manifest, written when the attempt ended: "
                              f"{folder}/{change[0]}", " ".join(attempt["reasons"]))
                vm.folders[folder] = original

    def test_an_export_manifest_that_differs_from_its_batch_json_hash_is_not_admitted(self):
        vm = self.vm("w1")
        trial = sorted(vm.derived)[0]
        vm.derived[trial]["manifest-sha256.json"] += b" "
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 0, out)
        attempt = C.latest_attempts(self.entries())[trial]
        self.assertFalse(attempt["accepted"])
        self.assertIn("export manifest is missing from the collection or differs", " ".join(attempt["reasons"]))
        self.assertIsNone(attempt["derived_report"])

    def test_a_copy_that_fails_verification_admits_nothing(self):
        vm = self.vm("w1")
        record = next(f"{n}/record.json" for n in vm.folders)
        damages = {"corrupt": ({record: b"{}"}, {}), "missing": ({record: None}, {}),
                   "extra": ({}, {f"{vm.batch_name()}/planted.txt": b"x"})}
        for label, (damage, extra) in damages.items():
            with self.subTest(damage=label):
                vm.damage, vm.extra = damage, extra
                code, out = self.cli("collect", "--worker", "w1")
                self.assertEqual(code, 1)
                self.assertIn("failed verification", out)
                last = self.entries(worker="w1")[-1]
                self.assertEqual(last["type"], "collect_failed")
                self.assertTrue((self.folder / last["path"]).is_dir())
                self.assertEqual(self.attempts(), [])
        vm.damage, vm.extra = {}, {}
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.entries("collect", "w1")[-1]["path"], "w1/collect-004")
        self.assertEqual(len(self.attempts()), 2)

    def test_the_ssh_commands_never_prompt_and_check_host_keys(self):
        self.cli("collect", "--worker", "w1")
        probe, tar = self.world.calls[0], next(argv for argv in self.world.calls if " tar " in argv[-1])
        self.assertEqual(probe[:3], ["ssh", "-i", os.path.expanduser("~/.ssh/stopcheck_ed25519")])
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes",
                       "UserKnownHostsFile=" + os.path.expanduser("~/.ssh/stopcheck_known_hosts")):
            self.assertIn(option, probe)
        self.assertEqual(probe[-2], "stopcheck@10.0.0.1")
        self.assertTrue(probe[-1].startswith("sudo -n /usr/bin/python3 -I -B - remote probe '{"))
        self.assertTrue(tar[-1].startswith("sudo -n tar -C /root/stopcheck/n8n/results -czf - -- XB-screen-t1-w1 "))

    def test_a_running_worker_is_not_copied(self):
        self.vm("w1").running = True
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("collect after its runner stops", out)
        self.assertEqual(self.world.count("tar"), 0)
        self.assertFalse((self.folder / "w1").exists())

    def test_an_unchanged_worker_is_not_copied_twice(self):
        self.cli("collect", "--worker", "w1")
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 0, out)
        self.assertIn("unchanged since collection 1", out)
        self.assertEqual(sorted(p.name for p in (self.folder / "w1").iterdir()), ["collect-001"])
        self.assertEqual(len(self.entries("attempt")), 2)

    def test_a_record_that_does_not_bind_to_the_campaign_is_not_admitted(self):
        vm = self.vm("w1")
        folder = sorted(vm.folders)[0]
        record = json.loads(vm.folders[folder]["record.json"])
        record["host_profile"]["declared"] = "wsl"
        vm.folders[folder]["record.json"] = json.dumps(record).encode()
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 0, out)
        bound = {a["raw_folder"].rsplit("/", 1)[-1]: a for a in self.attempts()}
        self.assertTrue(bound[folder]["runner_accepted"])
        self.assertFalse(bound[folder]["accepted"])
        self.assertIn("declares host profile 'wsl'", " ".join(bound[folder]["reasons"]))

    def test_a_raw_folder_that_changes_after_its_collection_is_flagged(self):
        self.cli("collect", "--worker", "w1")
        vm = self.vm("w1")
        folder = sorted(vm.folders)[0]
        vm.folders[folder]["logs/main.log"] = b"rewritten on the worker\n"
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 0, out)
        changed = [a for a in self.attempts() if a["raw_folder"].endswith(folder)]
        self.assertFalse(changed[0]["accepted"])
        self.assertIn("changed after an earlier verified collection", " ".join(changed[0]["reasons"]))
        self.assertTrue((self.folder / "w1" / "collect-001").is_dir())


class LedgerAppendOnly(Fixture):
    def setUp(self):
        super().setUp()
        self.register()
        self.ledger = self.camp().ledger

    def test_an_append_keeps_every_earlier_byte_and_chains_to_the_line_before(self):
        before = self.ledger.path.read_bytes()
        self.ledger.append({"type": "note", "text": "one"})
        self.ledger.append({"type": "note", "text": "two"})
        after = self.ledger.path.read_bytes()
        self.assertTrue(after.startswith(before))
        lines = after.split(b"\n")[:-1]
        entries = self.ledger.entries()
        self.assertEqual([e["seq"] for e in entries], [1, 2, 3])
        self.assertEqual([e["prev_sha256"] for e in entries], [C.GENESIS, sha(lines[0]), sha(lines[1])])

    def test_an_edited_or_removed_line_is_refused(self):
        self.ledger.append({"type": "note", "text": "one"})
        self.ledger.append({"type": "note", "text": "two"})
        original = self.ledger.path.read_bytes()
        lines = original.split(b"\n")[:-1]
        for label, damaged in (("edited", original.replace(b'"one"', b'"0ne"')),
                               ("removed in the middle", lines[0] + b"\n" + lines[2] + b"\n"),
                               ("removed at the end", b"\n".join(lines[:2]) + b"\n")):
            with self.subTest(damage=label):
                self.ledger.path.write_bytes(damaged)
                with self.assertRaises(C.CampaignError):
                    self.ledger.entries()
                with self.assertRaises(C.CampaignError):
                    self.ledger.append({"type": "note", "text": "three"})
                code, out = self.cli("status", "--no-ssh")
                self.assertEqual(code, 2)
        self.ledger.path.write_bytes(original)
        self.assertEqual(len(self.ledger.entries()), 3)

    def test_a_changed_attempt_is_a_new_line_and_earlier_lines_stay(self):
        self.launch_run_collect("w1", B7="killed")       # the runner died with B7 pending (a VM restart)
        pending = self.attempts("B7")
        self.assertEqual([a["status"] for a in pending], ["pending"])
        self.assertFalse(pending[0]["accepted"])
        snapshot = self.ledger.path.read_bytes()
        self.launch_run_collect("w1")                     # resume: the runner settles it as unrecorded, reruns
        self.assertTrue(self.ledger.path.read_bytes().startswith(snapshot))
        history = [(a["trial"], a["status"]) for a in self.entries("attempt") if a["condition"] == "B7"]
        self.assertEqual(history, [("XB-screen-t1-w1-B7", "pending"), ("XB-screen-t1-w1-B7", "unrecorded"),
                                   ("XB-screen-t1-w1-B7-a2", "finished")])
        self.assertEqual([a["global_attempt"] for a in self.attempts("B7")], [1, 2])


class Reconcile(Fixture):
    def setUp(self):
        super().setUp()
        self.register()
        self.vm("w1").launch_reply = "timeout"     # the launch started, but its reply was lost
        self.cli("launch", "--worker", "w1")

    def test_a_worker_whose_runner_is_alive_reconciles_as_running(self):
        self.assertEqual(self.state("w1"), "unresolved")
        code, out = self.cli("reconcile", "--worker", "w1")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.entries(worker="w1")[-1]["state"], self.state("w1")), ("running", "running"))
        code, out = self.cli("launch", "--worker", "w1")
        self.assertIn("it is running", out)

    def test_a_stopped_worker_reconciles_and_is_relaunched_only_after_a_collect(self):
        self.vm("w1").run(B7="invalid")
        code, out = self.cli("reconcile", "--worker", "w1")
        self.assertEqual(code, 0, out)
        last = self.entries(worker="w1")[-1]
        self.assertEqual((last["type"], last["state"], last["previous_state"]), ("reconciled", "stopped", "unresolved"))
        self.assertEqual(last["evidence"]["launch_files"]["exit"], "2 2026-10-14T10:30:00Z")
        self.assertEqual(self.state("w1"), "halted")
        code, out = self.cli("launch", "--worker", "w1")
        self.assertIn("collect first", out)
        code, out = self.cli("collect", "--worker", "w1")
        self.assertEqual(code, 0, out)
        self.vm("w1").launch_reply = "ack"
        code, out = self.cli("launch", "--worker", "w1")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.vm("w1").launches[-1]["mode"], self.vm("w1").launches[-1]["launch_no"]), ("resume", 2))

    def test_a_runner_lost_with_its_vm_reconciles_as_stopped_with_the_restart_noted(self):
        vm = self.vm("w1")
        vm.run(B7="killed")                           # B7 left pending, no exit status
        vm.overrides = {("boot_id",): "boot-2"}
        code, out = self.cli("reconcile", "--worker", "w1")
        self.assertEqual(code, 0, out)
        notes = " ".join(self.entries(worker="w1")[-1]["notes"])
        self.assertIn("wrote no exit status", notes)
        self.assertIn("the VM restarted since launch 1 (boot id boot-1 then, boot-2 now)", notes)

    def test_an_unreachable_worker_stays_unresolved(self):
        self.vm("w1").reachable = False
        code, out = self.cli("reconcile", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("state is unknown", out)
        self.assertEqual((self.entries("reconciled"), self.state("w1")), ([], "unresolved"))

    def test_a_trial_active_without_its_runner_is_not_reconciled(self):
        vm = self.vm("w1")
        vm.running = False
        vm.overrides = {("lock", "held"): True}
        code, out = self.cli("reconcile", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("without its runner", out)
        self.assertEqual(self.entries("reconciled"), [])

    def test_a_different_instance_is_not_reconciled(self):
        self.vm("w1").overrides = {("metadata", "id"): "999"}
        code, out = self.cli("reconcile", "--worker", "w1")
        self.assertEqual(code, 1)
        self.assertIn("registered '111'", out)
        self.assertEqual(self.state("w1"), "unresolved")


FAKE_RUNNER = '''import json, os, sys
argv = sys.argv[1:]
if "--dry-run" in argv:
    conditions = argv[argv.index("--conditions") + 1:argv.index("--seed")] if "--conditions" in argv else []
    print(json.dumps({"run_kind": "screen", "requires_freeze": True, "conditions": conditions,
                      "host_profile": os.environ.get("STOPCHECK_HOST_PROFILE")}))
else:
    print("fake runner: " + " ".join(argv) + " profile=" + os.environ.get("STOPCHECK_HOST_PROFILE", ""))
'''
FAKE_TMUX = '''#!/bin/sh
case "$1" in
  list-sessions) echo "no server running on /tmp/fake-tmux" >&2; exit 1 ;;
  new-session) shift; while [ "$#" -gt 1 ]; do shift; done; nohup sh -c "$1" >/dev/null 2>&1 & exit 0 ;;
esac
exit 1
'''


class ShellRunner:
    """Runs the command line ssh would send through a local POSIX shell instead, with a sudo that only drops -n."""

    def __init__(self, bin_dir):
        sudo = bin_dir / "sudo"
        sudo.write_text('#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n')
        sudo.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def run(self, argv, input=None, timeout=None, stdout_path=None):
        assert argv[0] == "ssh"
        command = ["sh", "-c", argv[-1]]
        if stdout_path is None:
            return subprocess.run(command, input=input, capture_output=True, timeout=timeout, env=self.env)
        with open(stdout_path, "xb") as out:
            return subprocess.run(command, input=input, stdout=out, stderr=subprocess.PIPE, timeout=timeout,
                                  env=self.env)


@unittest.skipUnless(os.name == "posix" and shutil.which("git"), "the worker-side helper needs POSIX and git")
class WorkerSide(unittest.TestCase):
    """The helper the coordinator feeds to each VM, run for real on a temporary repository."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="campaign-b-worker-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "stopcheck"
        (self.repo / "n8n" / "results").mkdir(parents=True)
        for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                     ("config", "user.name", "fixture")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        (self.repo / ".gitignore").write_text("n8n/results/\n")
        (self.repo / "n8n" / "run_expB.py").write_text(FAKE_RUNNER)
        (self.repo / "n8n" / "FREEZE-expB.json").write_text('{"schema_version": 1}\n')
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "fixture"], check=True, capture_output=True)
        self.commit = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
                                     capture_output=True, text=True).stdout.strip()
        missing = str(self.base / "no-such-binary")
        self.args = {"repo": str(self.repo), "python": sys.executable, "freeze": "n8n/FREEZE-expB.json",
                     "host_profile": "linux-vm", "lock_file": str(self.base / "launcher.lock"),
                     "log_dir": str(self.base / "campaign-logs" / "t1"), "worker": "w1", "batch_id": "t1-w1",
                     "conditions": ["B0", "PC"], "all_conditions": list(R.CONDITIONS), "seed": 7,
                     "session": "sc-t1-w1", "session_prefix": "sc-", "test_user": "no-such-user-campaign-b",
                     "metadata_url": "http://127.0.0.1:9/", "markers": ["no-such-program-campaign-b.py"],
                     "trials": ["XB-screen-t1-w1-B0", "XB-screen-t1-w1-PC"], "dry_run": True,
                     "tmux": missing, "ip": missing, "systemctl": missing, "timedatectl": missing,
                     "mnt": str(self.base / "mnt"), "work_root": str(self.base / "work"), "var_lib": str(self.base),
                     "cron_spool": str(self.base / "spool")}

    def test_the_probe_reports_the_repository_freeze_lock_and_the_runners_own_plan(self):
        p = C.remote_probe(self.args)
        json.dumps(p)
        self.assertEqual((p["repo"]["head"], p["repo"]["status"], p["repo"]["freeze_committed"]),
                         (self.commit, "", True))
        self.assertEqual(p["repo"]["freeze_sha256"], sha((self.repo / "n8n" / "FREEZE-expB.json").read_bytes()))
        self.assertEqual(p["lock"], {"path": self.args["lock_file"], "exists": False, "held": False})
        self.assertFalse(Path(self.args["lock_file"]).exists())
        self.assertEqual((p["dry_run"]["returncode"], p["dry_run"]["plan"]["conditions"],
                          p["dry_run"]["plan"]["host_profile"]), (0, ["B0", "PC"], "linux-vm"))
        self.assertIn("error", p["metadata"])
        self.assertIsNone(p["tmux"]["sessions"])
        self.assertEqual(p["test_user"]["error"], "no such user")
        self.assertEqual((p["batch"]["exists"], p["launches"], p["processes"]), (False, {}, []))
        batch = self.repo / "n8n" / "results" / "XB-screen-t1-w1"
        batch.mkdir()
        (batch / "batch.json").write_bytes(b'{"run_kind": "screen"}\n')
        (self.repo / "n8n" / "results" / "XB-screen-t1-w1-B0-20261014-100000-000000001").mkdir()
        (self.repo / "n8n" / "results" / "XB-screen-t1-w1-x-B0-20261014-100000-000000002").mkdir()
        p = C.remote_probe(dict(self.args, dry_run=False))
        self.assertEqual((p["batch"]["exists"], p["batch"]["sha256"]), (True, sha(b'{"run_kind": "screen"}\n')))
        self.assertEqual(p["batch"]["trial_folders"], ["XB-screen-t1-w1-B0-20261014-100000-000000001"])
        self.assertNotIn("dry_run", p)

    def camp(self):
        """Just what remote_call and fetch read from a campaign."""
        camp = type("Camp", (), {})()
        camp.data = {"remote": {"system_python": sys.executable},
                     "ssh": {"user": "stopcheck", "key": "~/.ssh/none", "known_hosts": None}}
        camp.workers = {"w1": {"ip": "192.0.2.1"}}
        return camp

    def test_the_coordinators_command_line_reaches_the_helper_through_a_shell(self):
        (self.base / "bin").mkdir()
        reply = C.remote_call(self.camp(), "w1", "probe", self.args, ShellRunner(self.base / "bin"))
        self.assertEqual((reply["repo"]["head"], reply["dry_run"]["plan"]["conditions"]), (self.commit, ["B0", "PC"]))

    @unittest.skipUnless(shutil.which("tar"), "requires tar")
    def test_a_copy_made_with_tar_verifies_against_the_workers_manifest(self):
        (self.base / "bin").mkdir()
        results = self.repo / "n8n" / "results"
        trial = results / "XB-screen-t1-w1-B0-20261014-100000-000000001"
        (trial / "logs").mkdir(parents=True)
        (trial / "record.json").write_bytes(b'{"trial": "XB-screen-t1-w1-B0"}\n')
        (trial / "logs" / "main.log").write_bytes(b"main\n")
        manifest = C.remote_manifest({"items": [{"root": str(results), "name": trial.name}],
                                      "markers": self.args["markers"], "lock_file": self.args["lock_file"],
                                      "tmux": self.args["tmux"]})
        expected = {f"results/{rel}": digest for rel, digest in manifest["items"][0]["files"].items()}
        staging = self.base / "staging"
        staging.mkdir()
        C.fetch(self.camp(), "w1", "results", str(results), [trial.name], self.base / "copy.tgz", staging,
                ShellRunner(self.base / "bin"), None)
        self.assertEqual(C.verify_tree(staging, expected), [])
        (staging / "results" / trial.name / "logs" / "main.log").write_bytes(b"changed\n")
        self.assertEqual(C.verify_tree(staging, expected),
                         [f"SHA-256 differs from the worker's: results/{trial.name}/logs/main.log"])

    def test_the_helper_runs_from_stdin_as_ssh_feeds_it(self):
        done = subprocess.run([sys.executable, "-I", "-B", "-", "remote", "probe", json.dumps(self.args)],
                              input=(ROOT / "n8n" / "campaign_b.py").read_bytes(), capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.decode().splitlines()[-1])["repo"]["head"], self.commit)

    def test_the_lock_is_read_from_proc_locks_never_taken_or_created(self):
        import fcntl
        path = self.base / "held.lock"
        self.assertEqual(C._lock_state(str(path))["held"], False)
        self.assertFalse(path.exists())
        with open(path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(C._lock_state(str(path))["held"])
            with open(path, "rb") as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)   # still ours: the probe took nothing
        self.assertFalse(C._lock_state(str(path))["held"])

    def test_a_marker_program_is_found_and_this_helper_is_not(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "/fixture/run_expB.py"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and child.pid not in [x["pid"] for x in C._processes(["run_expB.py"])]:
            time.sleep(0.05)
        self.assertIn(child.pid, [x["pid"] for x in C._processes(["run_expB.py"])])
        self.assertNotIn(os.getpid(), [x["pid"] for x in C._processes(["python", "python3", sys.executable])])

    def test_the_manifest_hashes_regular_files_and_lists_anything_else(self):
        results = self.repo / "n8n" / "results"
        folder = results / "XB-screen-t1-w1"
        (folder / "logs").mkdir(parents=True)
        (folder / "batch.json").write_bytes(b"{}\n")
        (folder / "logs" / "a.log").write_bytes(b"a\n")
        os.symlink("batch.json", folder / "link")
        m = C.remote_manifest({"items": [{"root": str(results), "name": "XB-screen-t1-w1"}],
                               "markers": self.args["markers"], "lock_file": self.args["lock_file"],
                               "tmux": self.args["tmux"]})
        item = m["items"][0]
        self.assertEqual(item["files"], {"XB-screen-t1-w1/batch.json": sha(b"{}\n"),
                                         "XB-screen-t1-w1/logs/a.log": sha(b"a\n")})
        self.assertEqual(item["other"], ["XB-screen-t1-w1/link"])

    def test_the_launch_runs_the_runner_in_tmux_with_stamps_and_refuses_a_duplicate(self):
        tmux = self.base / "bin" / "tmux"
        tmux.parent.mkdir()
        tmux.write_text(FAKE_TMUX)
        tmux.chmod(0o755)
        args = dict(self.args, tmux=str(tmux), campaign_id="t1", mode="new", launch_no=1,
                    expect_batch_sha256=None, ack_timeout=10)
        reply = C.remote_launch(args)
        self.assertTrue(reply.get("ack"), reply)
        base = Path(args["log_dir"]) / "w1-launch-1"
        exit_file = Path(str(base) + ".exit")
        deadline = time.monotonic() + 15
        while not exit_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(exit_file.read_text().startswith("0 "))
        log = Path(str(base) + ".log").read_text()
        self.assertIn("--freeze n8n/FREEZE-expB.json --conditions B0 PC --seed 7 --batch-id t1-w1", log)
        self.assertIn("profile=linux-vm", log)
        again = C.remote_launch(args)
        self.assertFalse(again["ok"])
        self.assertIn("a duplicate request", " ".join(again["refused"]))
        stale = C.remote_launch(dict(args, launch_no=2, expect_batch_sha256="0" * 64))
        self.assertIn("batch.json changed after the coordinator's probe", " ".join(stale["refused"]))


if __name__ == "__main__":
    unittest.main()
