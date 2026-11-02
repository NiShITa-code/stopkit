"""Exercise launch failure paths with fake commands; never create a real namespace."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("flock"), "requires POSIX bash/flock")
class LauncherFailures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stopcheck launch ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.bin = self.folder / "bin"
        self.bin.mkdir()
        self.log = self.folder / "commands"
        self.wrapper = self.folder / "harness_b_ns.sh"
        shutil.copyfile(ROOT / "n8n" / "harness_b_ns.sh", self.wrapper)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], FAKE_LOG=str(self.log),
                        STOPCHECK_LOCK_FILE=str(self.folder / "lock"), STOPCHECK_PYTHON=str(self.bin / "python"))
        self.script("id", 'printf "0\\n"\n')
        self.script("python", 'printf "python %s\\n" "$*" >> "$FAKE_LOG"\nexit "${FAKE_EXIT:-0}"\n')
        # Like iproute2, "ip netns exec" replaces itself with the command, so the launcher's child is the command.
        self.script("ip", '''printf 'ip %s\n' "$*" >> "$FAKE_LOG"
if [ "$1 $2" = 'netns list' ]; then printf '%s\n' "${FAKE_EXISTING:-}"; exit 0; fi
if [ "$1 $2" = 'netns add' ] && [ "${FAKE_ADD_FAIL:-0}" = 1 ]; then exit 9; fi
if [ "$1 $2" = 'netns exec' ]; then shift 3; exec "$@"; fi
exit 0
''')

    def script(self, name, contents):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + contents, encoding="utf-8")
        path.chmod(0o755)

    def run_wrapper(self, trial="TEST", env=None, **extra):
        return subprocess.run(["bash", str(self.wrapper), "B0", trial, "owned-test"],
                              env=dict(env or self.env, **extra), capture_output=True, text=True, timeout=10)

    def test_harness_untested_status_is_preserved_and_owned_namespace_removed(self):
        result = self.run_wrapper(FAKE_EXIT="3")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("ip netns del owned-test", self.log.read_text())

    def test_existing_namespace_is_never_deleted(self):
        result = self.run_wrapper(FAKE_EXISTING="owned-test (id: 1)")
        self.assertEqual(result.returncode, 2)
        logged = self.log.read_text()
        self.assertNotIn("netns add", logged)
        self.assertNotIn("netns del", logged)

    def test_failed_creation_does_not_delete_an_unowned_namespace(self):
        result = self.run_wrapper(FAKE_ADD_FAIL="1")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("netns del", self.log.read_text())

    def test_held_lock_prevents_any_namespace_operation(self):
        import fcntl
        with open(self.env["STOPCHECK_LOCK_FILE"], "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_wrapper()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_preflight_stops_and_preserves_first_failed_trial(self):
        preflight = self.folder / "_preflight.sh"
        shutil.copyfile(ROOT / "n8n" / "_preflight.sh", preflight)
        self.wrapper.write_text('#!/bin/bash\nprintf "%s\\n" "$1" >> "$FAKE_LOG"\nexit 3\n')
        result = subprocess.run(["bash", str(preflight), "B0", "PC"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.log.read_text().splitlines(), ["B0"])

    # ---- review F10 / F40: the launcher accepts exactly the trial ids harness_b.main accepts ----
    def test_trial_ids_the_harness_rejects_are_refused_before_any_namespace_operation(self):
        for trial in ("TEST_1", "-TEST", "A" * 65):
            with self.subTest(trial=trial):
                result = self.run_wrapper(trial=trial)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.log.exists())
        self.assertEqual(self.run_wrapper(trial="A" * 64).returncode, 0)

    # ---- review F12: the documented STOPCHECK_PY also selects the launcher's interpreter ----
    def test_documented_stopcheck_py_selects_the_harness_interpreter(self):
        env = dict(self.env, STOPCHECK_PY=str(self.bin / "python"))
        env.pop("STOPCHECK_PYTHON")
        result = self.run_wrapper(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"python -B -u {self.folder}/harness_b.py B0 TEST", self.log.read_text())

    def test_stopcheck_python_still_overrides_stopcheck_py(self):
        self.script("other-python", 'printf "other %s\\n" "$*" >> "$FAKE_LOG"\nexit 0\n')
        result = self.run_wrapper(STOPCHECK_PY=str(self.bin / "other-python"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("other -B", self.log.read_text())
        self.assertIn("python -B -u", self.log.read_text())

    # ---- review F6: Ctrl-C reaches the backgrounded harness and the owned namespace is removed ----
    def test_interrupt_stops_the_harness_and_removes_the_namespace(self):
        # A background child of a non-interactive bash starts with SIGINT ignored, as harness_b.py does.
        self.script("python", '''printf 'started\\n' >> "$FAKE_LOG"
trap 'printf "terminated\\n" >> "$FAKE_LOG"; exit 2' TERM
for i in $(seq 40); do sleep 0.1; done
printf 'finished\\n' >> "$FAKE_LOG"
''')
        process = subprocess.Popen(["bash", str(self.wrapper), "B0", "TEST", "owned-test"], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.log.exists() and "started" in self.log.read_text()):
                self.assertLess(time.monotonic(), deadline, "fake harness never started")
                time.sleep(0.05)
            began = time.monotonic()
            process.send_signal(signal.SIGINT)
            status = process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
        logged = self.log.read_text()
        self.assertIn("terminated", logged)
        self.assertNotIn("finished", logged)
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertEqual(status, 130)
        self.assertIn("ip netns del owned-test", logged)

    # ---- review F9: full-timing preflight trials are pilots, and a preflight is never a screen ----
    def preflight(self, **extra):
        preflight = self.folder / "_preflight.sh"
        shutil.copyfile(ROOT / "n8n" / "_preflight.sh", preflight)
        self.wrapper.write_text('#!/bin/bash\nprintf "%s %s %s %s %s\\n" "$1" "$STOPCHECK_RUN_KIND" "$STOPCHECK_FAST" '
                                '"${STOPCHECK_FROZEN_MANIFEST:-unset}" "${STOPCHECK_BATCH:-unset}" >> "$FAKE_LOG"\n')
        env = {k: v for k, v in self.env.items() if k not in ("STOPCHECK_FAST", "STOPCHECK_RUN_KIND")}
        return subprocess.run(["bash", str(preflight), "B0"], env=dict(env, **extra),
                              capture_output=True, text=True, timeout=10)

    def test_preflight_defaults_to_fast_smoke(self):
        self.assertEqual(self.preflight().returncode, 0)
        self.assertEqual(self.log.read_text().splitlines(), ["B0 smoke 1 unset unset"])

    def test_preflight_full_timing_runs_as_pilot(self):
        result = self.preflight(STOPCHECK_FAST="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), ["B0 pilot 0 unset unset"])

    def test_preflight_never_runs_a_screen_or_a_frozen_batch(self):
        for fast in ("0", "1"):
            with self.subTest(fast=fast):
                result = self.preflight(STOPCHECK_FAST=fast, STOPCHECK_RUN_KIND="screen",
                                        STOPCHECK_FROZEN_MANIFEST="/x/FREEZE-expB.json")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())
        result = self.preflight(STOPCHECK_FROZEN_MANIFEST="/x/FREEZE-expB.json", STOPCHECK_BATCH="/x/batch.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), ["B0 smoke 1 unset unset"])
