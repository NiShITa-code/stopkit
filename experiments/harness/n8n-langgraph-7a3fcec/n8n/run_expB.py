#!/usr/bin/env python3
"""Run unscored pilots or a source-bound, exploratory Experiment B screen.

Dry runs never launch services. Freeze preparation never invents predictions:
an operator-authored JSON document is required. A screen requires a committed,
clean repository, passing offline checks, and an unchanged, committed freeze
manifest; each freeze gets one screen batch per machine (several machines: one
registered batch per worker, n8n/campaign_b.py). A batch stops at the first trial
that fails or that the scorer calls invalid. --resume continues the recorded
order with the same code and freeze and reruns that trial once (B.10, as
R6.10); a condition whose trial and replacement both fail is incomplete.
Ctrl-C, SIGTERM or a hangup stops the running trial through its launcher and
is recorded; no trial starts while another one holds the launcher lock.
"""
import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = HERE / "results"
CONDITIONS = ("B0", "PC", "B1", "B1r", "B2", "B2s", "B3", "B4a", "B4b", "B5", "B6a", "B6b",
              "B7", "B8", "B9a", "B9b", "B9c", "B10", "B11")
SCHEMA = 1
# Current-source campaign records; legacy descriptive scoring remains separate. Schema 7 (13 Oct): records written
# before the merge already said 6 without host checks and with the earlier cgroup reader, so 6 no longer qualifies.
TRIAL_RECORD_SCHEMA = 7
CARRIERS = ("loop_steps", "loop_node", "sub", "sched", "wait_short", "wait_long", "ai_agent", "fanout",
            "bg", "det", "oscron", "cred_use", "cred_store", "keyvalid", "outjob", "out")
STATES = {"running", "armed", "authorised", "ended", "unknown", "not_planted", "unpredicted"}
# B.0: versions, package hash and effective settings. Each must be declared; missing evidence is never agreement.
CONFIGURATION = ("n8n", "node", "postgres", "redis", "package_lock_sha256", "EXECUTIONS_MODE", "N8N_RUNNERS_MODE",
                 "N8N_RUNNERS_MAX_CONCURRENCY", "N8N_RUNNERS_TASK_TIMEOUT", "worker_concurrency", "NODES_EXCLUDE",
                 "N8N_GRACEFUL_SHUTDOWN_TIMEOUT", "N8N_LOG_LEVEL", "queue_lock_s", "lock_renew_s",
                 "stalled_interval_s", "queue_recovery_interval")
DEFAULT_SEED = 20261012
TRIAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")  # the trial ids harness_b.main accepts
ATTEMPTS = 2  # B.10: an invalid trial is rerun once (as R6.10)
# B.10: the full-length validation pilot, and the default pilot batch
PILOT_CONDITIONS = ("B0", "PC", "B1", "B1r")
# B.10: conditions FAST timing refuses (harness_b.condition_refusal, tested equal): B1r's preparatory main restart and
# B9c's relaunch need n8n's own shutdown and start-up, which FAST does not shorten
FAST_REFUSED = ("B1r", "B9c")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ============================ host profile and worker identity (B.0, B.13) ============================
# The declared host profiles (harness_b.HOST_PROFILES, tested equal). The freeze records the declared one; its
# validation pilots, its screen trials and every trial a batch accepts must declare the same, so a laptop wsl pilot can
# never validate a linux-vm freeze or the reverse.
HOST_PROFILES = ("wsl", "linux-vm")


def declared_host_profile(env=None):
    """The host profile this process declares (STOPCHECK_HOST_PROFILE, default wsl), as the harness reads it; never
    inferred from the machine."""
    profile = (os.environ if env is None else env).get("STOPCHECK_HOST_PROFILE", "wsl")
    if profile not in HOST_PROFILES:
        raise ValueError(f"STOPCHECK_HOST_PROFILE must be one of {', '.join(HOST_PROFILES)}, not {profile!r}")
    return profile


# linux-vm: each worker's identity, read before every trial from the GCE metadata server by the runner, outside the
# trial's network namespace (which has no route to it), and stored in the trial's batch entry, from which the harness
# copies it into the record. Workers need not be identical. A missing identity is recorded as missing and never refuses
# a trial: it is provenance, not a configuration value the freeze compares, and a plain Linux VM need not be on GCE.
GCE_METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/"
GCE_IDENTITY = (("id", "id"), ("name", "name"), ("zone", "zone"), ("machine_type", "machine-type"),
                ("cpu_platform", "cpu-platform"), ("image", "image"), ("preemptible", "scheduling/preemptible"),
                ("on_host_maintenance", "scheduling/on-host-maintenance"))


def _gce_metadata(path, timeout=2.0):
    """One instance value from the GCE metadata server: sent with Metadata-Flavor: Google, never through a proxy, and
    accepted only from a reply that carries Metadata-Flavor: Google."""
    import urllib.request
    request = urllib.request.Request(GCE_METADATA_URL + path, headers={"Metadata-Flavor": "Google"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as reply:
        if reply.headers.get("Metadata-Flavor") != "Google":
            raise ValueError("the reply does not carry Metadata-Flavor: Google")
        return reply.read(4096).decode("utf-8", "replace").strip()


def worker_identity(profile, fetch=None):
    """This worker's identity for one trial: on linux-vm every GCE_IDENTITY value it could read, with the ones it could
    not listed as missing (status recorded, partial or missing); not applicable on wsl, which asks nothing."""
    if profile != "linux-vm":
        return {"status": "not applicable", "host_profile": profile}
    fetch = fetch or _gce_metadata
    values, errors = {}, {}
    for key, path in GCE_IDENTITY:
        try:
            values[key] = fetch(path)
        except Exception as exc:  # noqa: BLE001  (recorded as missing; never a refusal)
            errors[key] = f"{type(exc).__name__}: {exc}"[:200]
    return {"status": "recorded" if not errors else ("partial" if values else "missing"), "host_profile": profile,
            "source": GCE_METADATA_URL + " (read by the runner, outside the trial's network namespace)",
            "read_utc": utc_now(), "values": values, "missing": sorted(errors), "errors": errors}


def git(*args):
    return subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", "-C", str(ROOT), *args],
                          capture_output=True, text=True)


def source_hashes():
    paths = set()
    for folder, patterns in ((HERE, ("*.py", "*.sh")), (ROOT / "tests", ("*.py", "*.json"))):
        for pattern in patterns:
            paths.update(p for p in folder.glob(pattern) if p.is_file())
    paths.update(ROOT / p for p in ("n8n/RULES-expB.md", "n8n/DESIGN-expB.md", "n8n/owner_api_key_scopes.txt",
                                   "common/guards.py", "common/clockguard.py", "common/test_clockguard.py",
                                   "langgraph/recorder.py"))
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise ValueError("Missing source files: " + ", ".join(missing))
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(paths)}


def _unique_keys(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"Duplicate key in predictions: {key}")
        data[key] = value
    return data


def read_predictions(path, conditions):
    data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    if data.get("schema_version") != SCHEMA:
        raise ValueError("Predictions require schema_version=1")
    if not isinstance(data.get("prior_exposure"), str) or not data["prior_exposure"].strip():
        raise ValueError("Predictions must describe prior pilot/smoke-result exposure")
    configuration = data.get("configuration")
    if not isinstance(configuration, dict) or not configuration:
        raise ValueError("Predictions must declare configuration expectations")
    missing = [k for k in CONFIGURATION if configuration.get(k) is None or not str(configuration[k]).strip()]
    if missing:
        raise ValueError("Predictions must declare every B.0 configuration value; missing or null: " + ", ".join(missing))
    entries = data.get("conditions")
    if not isinstance(entries, dict):
        raise ValueError("Predictions require a conditions object")
    for condition in conditions:
        entry = entries.get(condition)
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry[k].strip()
                                                 for k in ("prediction", "basis", "refutation")):
            raise ValueError(f"{condition}: prediction, basis and refutation must be nonempty strings")
        carriers = entry.get("carriers")
        if not isinstance(carriers, dict) or set(carriers) != set(CARRIERS):
            raise ValueError(f"{condition}: specify each declared carrier exactly once")
        for carrier, cell in carriers.items():
            if not isinstance(cell, dict) or cell.get("predicted_state") not in STATES or cell.get("label") not in {"P", "C", "O", "S", "M"}:
                raise ValueError(f"{condition}/{carrier}: invalid predicted_state or provenance label")
            if not all(isinstance(cell.get(k), str) and cell[k].strip() for k in ("basis", "refutation")):
                raise ValueError(f"{condition}/{carrier}: basis and refutation required")
    return data


def offline_checks():
    commands = ([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                [sys.executable, "-B", str(ROOT / "reproduce.py"), "--quick"],
                [sys.executable, "-B", str(HERE / "mutate_b.py")])
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if result.returncode:
            raise ValueError("Offline check failed: " + " ".join(command) + "\n" +
                             (result.stdout + result.stderr)[-6000:])


def check_repository():
    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    if head.returncode or status.returncode:
        raise ValueError("Cannot verify repository HEAD and working tree")
    if status.stdout.strip():
        raise ValueError("Screen requires committed code and freeze; working tree is dirty:\n" + status.stdout)
    return head.stdout.strip()


def require_committed(path, what):
    """git status omits ignored files, so check each freeze input itself: inside, not ignored, tracked, unchanged."""
    path = Path(path).resolve()
    if ROOT != path and ROOT not in path.parents:
        raise ValueError(f"{what} must be inside the repository: {path}")
    relative = path.relative_to(ROOT).as_posix()
    ignored = git("check-ignore", "-q", "--", relative)
    if ignored.returncode == 0:
        raise ValueError(f"{what} is in a git-ignored path, so it cannot be committed: {relative}")
    if ignored.returncode != 1:
        raise ValueError(f"Cannot check whether {what} is git-ignored: {relative}")
    if git("ls-files", "--error-unmatch", "--", relative).returncode:
        raise ValueError(f"{what} is not committed: {relative}")
    if git("diff", "--quiet", "HEAD", "--", relative).returncode:
        raise ValueError(f"{what} differs from its committed version: {relative}")


def _scorer():
    """The frozen scorer, loaded from its file, so validation uses the same verdicts as scoring."""
    spec = importlib.util.spec_from_file_location("analyze_b_for_run_expB", HERE / "analyze_b.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_current_trial_schema(record):
    """Future campaign admission must not use the scorer's descriptive legacy compatibility path. Pilots and screens
    need exactly TRIAL_RECORD_SCHEMA (7), the schema from which the scorer requires the host checks, the /proc cgroup
    reader's tag and its own recomputation of the wall/monotonic clock check."""
    if type(record.get("schema_version")) is not int or record["schema_version"] != TRIAL_RECORD_SCHEMA:
        raise ValueError(f"current-source trials require schema_version={TRIAL_RECORD_SCHEMA}; "
                         f"found {record.get('schema_version')!r}")


def recorded_host_profile(record):
    """The host profile a record declares (harness_b.run_trial: host_profile.declared), or None."""
    block = record.get("host_profile")
    return block.get("declared") if isinstance(block, dict) else None


def _b1r_restart_problem(record):
    """B.10: "B1r really changes main identity and completes readiness before its stop". None when the record shows
    exactly one pre-stop main restart, verified ready with a changed process identity, ready by its deadline
    (backlog_lead_s before the planned stop) and before the actual stop; otherwise why not."""
    iv = record.get("intervention") if isinstance(record.get("intervention"), dict) else {}
    events = [e for e in iv.get("restarts") or [] if isinstance(e, dict) and e.get("tag") == "pre-stop"]
    if len(events) != 1:
        return f"{len(events)} pre-stop main restarts recorded (exactly one is needed)"
    event = events[0]
    if not (event.get("ok") is True and event.get("ready") is True and event.get("identity_changed") is True):
        return "the pre-stop main restart did not verify readiness with a changed process identity"
    plan = iv.get("preparatory_restart") if isinstance(iv.get("preparatory_restart"), dict) else {}
    times = (event.get("ready_mono"), (iv.get("t_stop") or {}).get("mono"), iv.get("target_mono"),
             plan.get("deadline_rel_stop"))
    if not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in times):
        return "the pre-stop main restart lacks its readiness, deadline or stop time"
    ready, stop, target, deadline = times
    if not (ready <= target + deadline and ready < stop):
        return "main was ready only after the pre-stop restart's deadline or the stop"
    return None


def validate_pilots(paths, expected_hashes, host_profile):
    """B.10 / B.13: the freeze's full-length validation pilots, one valid record for each of PILOT_CONDITIONS, each
    with the exact source fingerprints the freeze binds and the freeze's declared host profile."""
    found = set()
    receipts = []
    for folder in paths:
        record_path = Path(folder) / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        condition = record.get("condition")
        if condition not in PILOT_CONDITIONS:
            continue
        if record.get("fast_mode") or record.get("run_kind") != "pilot":
            raise ValueError(f"{folder}: freeze validation requires full-length unscored pilots")
        try:
            require_current_trial_schema(record)
        except ValueError as exc:
            raise ValueError(f"{folder}: {exc}") from None
        if record.get("source_hashes") != expected_hashes:
            raise ValueError(f"{folder}: pilot source manifest differs from current code")
        if recorded_host_profile(record) != host_profile:
            raise ValueError(f"{folder}: the pilot's declared host profile {recorded_host_profile(record)!r} is not "
                             f"the freeze's {host_profile!r}")
        if condition == "B1r" and _b1r_restart_problem(record):
            raise ValueError(f"{folder}: B1r's preparatory main restart is not verified: {_b1r_restart_problem(record)}")
        if any(record.get(k) for k in ("error", "void", "untested", "rerun_needed", "guard_violations", "leftover_n8ntest")):
            raise ValueError(f"{folder}: pilot has error, invalidity, untested work or failed cleanup")
        for key in ("clock_start", "clock_end", "clock_across_trial"):
            if record.get(key, {}).get("ok") is not True:
                raise ValueError(f"{folder}: missing or failed {key}")
        checks = record.get("pre_stop_check") or {}
        if not all(checks.get(carrier, {}).get("ok") is True for carrier in ("outjob", "fanout", "collateral")):
            raise ValueError(f"{folder}: missing required pre-stop evidence")
        controls = record.get("controls", {}).get("outside_twin", {})
        if controls.get("errors") != 0 or controls.get("calls", 0) <= 0:
            raise ValueError(f"{folder}: independent outside control did not pass")
        # B.10: full-length validation must show that recovery starts (B.4 step 7), not only that the watch ran.
        probe = record.get("restart_probe") or {}
        if probe.get("error") or not probe.get("reads"):
            raise ValueError(f"{folder}: restart probe failed or made no reads: {probe.get('error') or 'no reads'}")
        if any(event.get("restarted") and event.get("ready") is not True for event in probe.get("events") or []):
            raise ValueError(f"{folder}: a unit restarted by the probe did not confirm readiness")
        scorer = _scorer()
        try:
            trial = scorer.Trial(str(folder))
            # B.10 (the recorder sees every carrier; recovery starts): a failed restart-probe control (R6.9, B.9)
            # leaves the trial valid, so validity alone cannot reject it here
            probe_failed = scorer._probe_failure(trial) or scorer._probe_control_failure(trial)
            verdict = scorer.validity_rows(trial)[0]
        except Exception as exc:  # noqa: BLE001  (an unscorable pilot cannot validate a freeze)
            raise ValueError(f"{folder}: the scorer cannot read this pilot: {type(exc).__name__}: {exc}") from None
        if probe_failed:
            detail = f": {probe_failed}" if isinstance(probe_failed, str) else ""
            raise ValueError(f"{folder}: the scorer reports a failed restart probe or probe control{detail}")
        if verdict.get("kind") != "valid":
            raise ValueError(f"{folder}: the scorer calls this pilot invalid: {verdict.get('verdict')}")
        found.add(condition)
        receipts.append({"condition": condition, "record_sha256": sha(record_path), "folder": str(Path(folder).resolve()),
                         "host_profile": host_profile})
    missing = [c for c in PILOT_CONDITIONS if c not in found]
    if missing:
        raise ValueError("Freeze requires successful, current-source full-length B0, PC, B1 and B1r pilot records "
                         f"(B.10); missing: {', '.join(missing)}")
    return receipts


def prepare_freeze(path, predictions, conditions, validation):
    path, predictions = Path(path).resolve(), Path(predictions).resolve()
    read_predictions(predictions, conditions)
    profile = declared_host_profile()
    offline_checks()
    hashes = source_hashes()
    receipts = validate_pilots(validation, hashes, profile)
    relative = predictions.relative_to(ROOT).as_posix()  # keep freeze self-contained in this repository
    head = git("rev-parse", "HEAD")
    if head.returncode:
        raise ValueError("Cannot identify repository HEAD")
    manifest = {"schema_version": SCHEMA, "prepared_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "prepared_from_head": head.stdout.strip(), "timing_mode": "full", "conditions": list(conditions),
                "host_profile": profile,
                "prediction_file": relative, "prediction_sha256": sha(predictions), "source_hashes": hashes,
                "validation_pilots": receipts}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Refuse replacement of an existing research freeze.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    print(f"Prepared {path}. Commit this manifest, predictions, code and rules before a screen.")


def verify_freeze(path, conditions, run_checks=True, host_profile=None):
    """The freeze, checked for this host. host_profile is the declared profile of the caller (the harness passes its
    own); by default this process's (declared_host_profile)."""
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA or manifest.get("timing_mode") != "full":
        raise ValueError("Unsupported freeze schema or timing mode")
    if not set(conditions).issubset(set(manifest.get("conditions", []))):
        raise ValueError("Selected conditions are not all registered in the freeze")
    prediction_path = (ROOT / manifest["prediction_file"]).resolve()
    if ROOT != prediction_path and ROOT not in prediction_path.parents:
        raise ValueError("Prediction path escapes the repository")
    read_predictions(prediction_path, conditions)
    if sha(prediction_path) != manifest.get("prediction_sha256"):
        raise ValueError("Prediction file changed after freeze")
    if source_hashes() != manifest.get("source_hashes"):
        raise ValueError("Source files changed after freeze")
    frozen = manifest.get("host_profile")
    if frozen not in HOST_PROFILES:
        raise ValueError(f"The freeze records no declared host profile ({frozen!r}); prepare a new freeze")
    here = declared_host_profile() if host_profile is None else host_profile
    if here != frozen:
        raise ValueError(f"This host's declared host profile is {here!r}, but the freeze binds {frozen!r}: a freeze "
                         "runs only on the host profile its validation pilots ran on")
    check_repository()
    require_committed(path, "Freeze manifest")
    require_committed(prediction_path, "Prediction file")
    if run_checks:
        offline_checks()
    return manifest


def require_clock():
    sys.path.insert(0, str(ROOT / "common"))
    import clockguard
    result = clockguard.check(2.0)
    if not result.get("ok"):
        raise ValueError("Clock guard failed: " + "; ".join(result.get("reasons", [])))


# ============================ batches ============================
def trial_id(kind, batch_id, condition, attempt=1):
    """Trial ids are never reused, so a replacement gets its own id and work directory."""
    base = f"XB-{kind}-{batch_id}-{condition}"
    return base if attempt == 1 else f"{base}-a{attempt}"


def check_batch_id(kind, batch_id, conditions):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", batch_id or ""):
        raise ValueError("Invalid batch id: use letters, digits and hyphens, starting with a letter or digit")
    for condition in conditions:
        for attempt in range(1, ATTEMPTS + 1):
            trial = trial_id(kind, batch_id, condition, attempt)
            if not TRIAL_ID.fullmatch(trial):
                raise ValueError(f"Batch id too long: trial id {trial} exceeds the harness's 64-character limit")


def write_batch(path, batch):
    """Replace batch.json in one step; each trial's harness reads it."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextlib.contextmanager
def batch_lock(folder):
    """One runner per batch, so a resume cannot run beside the runner that is still using the batch."""
    import fcntl  # POSIX only; live trials run in the Ubuntu test environment
    with open(Path(folder) / "batch.lock", "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError(f"Another runner is using this batch: {folder}") from None
        yield


def refuse_reused_freeze(freeze_sha256):
    """One screen batch per freeze on this machine (a registered campaign runs one per worker, n8n/campaign_b.py):
    a failed or interrupted screen continues with --resume, never restarts."""
    for path in sorted(RESULTS.glob("XB-screen-*/batch.json")):
        try:
            used = json.loads(path.read_text(encoding="utf-8")).get("freeze_sha256")
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read earlier screen batch {path}: {exc}") from None
        if used == freeze_sha256:
            raise ValueError(f"This freeze already has a screen batch; continue it with --resume {path}")


def load_batch(path, kind):
    path = Path(path).resolve()
    prefix = f"XB-{kind}-"
    if path.name != "batch.json" or path.parent.parent != RESULTS.resolve() or not path.parent.name.startswith(prefix):
        raise ValueError(f"--resume needs {RESULTS / (prefix + '<id>') / 'batch.json'} for this run kind")
    batch = json.loads(path.read_text(encoding="utf-8"))
    if batch.get("schema_version") != SCHEMA or batch.get("run_kind") != kind:
        raise ValueError(f"{path}: another batch schema or run kind")
    if batch.get("completed"):
        raise ValueError(f"Batch already completed: {path}")
    conditions = batch.get("conditions")
    if (not isinstance(conditions, list) or not conditions or len(set(conditions)) != len(conditions)
            or not set(conditions) <= set(CONDITIONS)):
        raise ValueError(f"{path}: invalid recorded condition order")
    if not isinstance(batch.get("trials"), list) or any(t.get("condition") not in conditions for t in batch["trials"]):
        raise ValueError(f"{path}: recorded trials do not match its conditions")
    batch.setdefault("batch_id", path.parent.name[len(prefix):])  # batches written before batch_id was recorded
    batch.setdefault("incomplete", [])
    batch.setdefault("stops", [])
    check_batch_id(kind, batch["batch_id"], conditions)
    return batch


def accepted(entry):
    # B.10: only the scorer's validity verdict accepts a trial; an exit status alone never does.
    return entry.get("accepted") is True


def next_attempt(batch, condition):
    """The attempt a condition still needs, or None once it is accepted or has used its one rerun."""
    attempts = [t for t in batch["trials"] if t.get("condition") == condition]
    if any(accepted(t) for t in attempts) or condition in batch["incomplete"] or len(attempts) >= ATTEMPTS:
        return None
    return len(attempts) + 1


def settle(batch):
    """On resume: an attempt the runner never recorded still counts, and so can leave a condition incomplete. An
    attempt that ended before the runner stored the scorer's verdict (an interrupt during scoring) is judged now,
    as run_batch would have judged it; a resume runs only the batch's own code."""
    for entry in batch["trials"]:
        if entry.get("status") == "pending":
            entry["status"] = "unrecorded"
        elif entry.get("status") in ("finished", "interrupted") and "accepted" not in entry:
            entry.update(trial_validity(entry["trial"], batch.get("host_profile")))
            entry["accepted"] = (entry.get("exit_code") == 0 and not entry.get("interrupted")
                                 and entry.get("scored_valid") is True)
            entry["judged_on_resume_utc"] = utc_now()
    for condition in batch["conditions"]:
        attempts = [t for t in batch["trials"] if t.get("condition") == condition]
        if (len(attempts) >= ATTEMPTS and not any(accepted(t) for t in attempts)
                and condition not in batch["incomplete"]):
            batch["incomplete"].append(condition)


def stop(batch, reason):
    batch["stopped_reason"] = reason
    batch["stops"].append({"utc": utc_now(), "reason": reason})


def say(message):
    """A note on stderr that cannot fail: after a hangup the terminal returns EIO, and the runner must still wait."""
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


@contextlib.contextmanager
def stop_signals():
    """SIGTERM, and SIGHUP from a closed terminal or SSH session, stop a batch the way Ctrl-C does. The launcher
    has its own session, so otherwise either would end the runner alone, unrecorded, with its trial still running.
    A signal already ignored (nohup) stays ignored, and the previous handlers return afterwards."""
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None and signal.getsignal(sig) == signal.SIG_DFL:
            previous[sig] = signal.signal(sig, interrupt)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def require_no_running_trial(env):
    """A runner killed outright leaves its detached trial running and releases batch.lock. A resume must not start
    the next attempt beside it: the launcher would refuse, and that refusal would use up the condition's rerun.
    Probe the launcher's own lock (harness_b_ns.sh) and release it at once; never create the file."""
    import fcntl  # POSIX only, as batch_lock
    path = env.get("STOPCHECK_LOCK_FILE") or "/run/stopcheck-n8n.lock"
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"A StopCheck n8n trial is still running (it holds {path}); let it finish and "
                             "clean up, then --resume. No attempt was recorded.") from None
        fcntl.flock(handle, fcntl.LOCK_UN)


def launch(condition, trial, env):
    """Run one trial through its launcher; return (exit status, interrupted)."""
    # Its own session keeps a terminal Ctrl-C away from the trial. The runner then sends the launcher one SIGTERM
    # and waits, so the harness cleans up and the launcher's EXIT trap removes the trial's namespace.
    process = subprocess.Popen(["bash", str(HERE / "harness_b_ns.sh"), condition, trial], cwd=HERE, env=env,
                               start_new_session=True)
    interrupted = False
    while True:
        try:
            return process.wait(), interrupted
        except KeyboardInterrupt:
            if not interrupted:
                interrupted = True
                process.send_signal(signal.SIGTERM)
                say(f"Interrupted: asked {trial} to stop and clean up; waiting for its launcher to exit.")
            else:
                say(f"Still waiting for {trial} to clean up (launcher PID {process.pid}).")


def trial_validity(trial, host_profile=None):
    """The scorer's validity verdict on the trial's raw record; B.10 reruns an invalid trial once. With host_profile
    (the batch's), a record that does not declare that profile is never valid for the batch."""
    folders = []
    for path in RESULTS.glob(f"{trial}-*/record.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("trial") == trial:
                folders.append(path.parent)
        except (OSError, ValueError):
            continue
    if len(folders) != 1:
        return {"record": None, "scored_valid": False,
                "validity": f"expected one raw record for {trial}, found {len(folders)}"}
    try:
        record = json.loads((folders[0] / "record.json").read_text(encoding="utf-8"))
        require_current_trial_schema(record)
        if host_profile is not None and recorded_host_profile(record) != host_profile:
            return {"record": str(folders[0]), "scored_valid": False,
                    "validity": f"the record's declared host profile {recorded_host_profile(record)!r} is not the "
                                f"batch's {host_profile!r}"}
        scorer = _scorer()
        row = scorer.validity_rows(scorer.Trial(str(folders[0])))[0]
    except Exception as exc:  # noqa: BLE001  (an unscorable record is not a valid trial)
        return {"record": str(folders[0]), "scored_valid": False, "validity": f"unscorable: {type(exc).__name__}: {exc}"}
    return {"record": str(folders[0]), "scored_valid": row.get("kind") == "valid", "validity": row.get("verdict")}


# ============================ score and export every attempt (13 Oct) ============================
# After each attempt, accepted or not, interrupted or without a record, the runner scores its raw folder into
# <batch folder>/derived/<trial>/ (scored_b.jsonl and summary_b.md, as analyze_b.py --out writes them) and writes
# beside them a SHA-256 manifest of every raw file and of those reports; batch.json references both. Raw files are only
# read. Paths in the manifest are relative to n8n/results.
EXPORT_SCHEMA = 1
DERIVED = "derived"
EXPORT_MANIFEST = "manifest-sha256.json"
RAW_FOLDER_SUFFIX = r"-\d{8}-\d{6}-\d{9}"      # harness_b.main: <trial>-%Y%m%d-%H%M%S-<9 digits>


def _results_relative(path):
    return Path(path).resolve().relative_to(RESULTS.resolve()).as_posix()


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entries(folder):
    """Every file under folder, recursively and in a stable order: path (relative to n8n/results), bytes, sha256."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            out.append({"path": _results_relative(path), "bytes": path.stat().st_size, "sha256": _file_sha256(path)})
    return out


def raw_folders(trial):
    """Every raw folder the harness made for this trial id, with or without a record (an exact name match, so a rerun's
    folders, <trial>-a2-..., are never taken for the first attempt's)."""
    if not RESULTS.is_dir():
        return []
    pattern = re.compile(re.escape(trial) + RAW_FOLDER_SUFFIX)
    return sorted(p for p in RESULTS.iterdir() if p.is_dir() and pattern.fullmatch(p.name))


def export_attempt(entry, batch_folder):
    """Score one attempt and export it with its SHA-256 manifest (see above). Never raises an Exception: a failure is
    recorded in the returned reference, which run_batch stores in the attempt's batch entry."""
    trial = entry["trial"]
    derived = Path(batch_folder) / DERIVED / trial
    out = {"exported_utc": utc_now()}
    try:
        derived.mkdir(parents=True, exist_ok=True)
        folders = raw_folders(trial)
        raw = [item for folder in folders for item in _file_entries(folder)]
        records = [folder for folder in folders if (folder / "record.json").is_file()]
        scorer = _scorer()
        for name in scorer.REPORTS:                         # a re-export never leaves an earlier report behind
            (derived / name).unlink(missing_ok=True)
        scoring = {"status": "not scored", "why": f"{len(records)} raw records for this trial (exactly one is needed)"}
        if len(records) == 1:
            try:
                T = scorer.Trial(str(records[0]))
                rows = scorer.score_trial(T)
                with open(derived / scorer.REPORTS[0], "w", encoding="utf-8", newline="\n") as stream:
                    stream.write("".join(json.dumps(row) + "\n" for row in rows))
                with open(derived / scorer.REPORTS[1], "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(scorer.summarize(T, rows))
                validity = next((r for r in rows if r.get("measure") == "validity"), {})
                scoring = {"status": "scored", "rows": len(rows), "validity": validity.get("verdict")}
            except Exception as exc:  # noqa: BLE001  (an unscorable attempt is still exported)
                scoring = {"status": "not scored", "why": f"scorer failed: {type(exc).__name__}: {exc}"[:300]}
        reports = [item for item in _file_entries(derived) if Path(item["path"]).name != EXPORT_MANIFEST]
        manifest = {"schema_version": EXPORT_SCHEMA, "trial": trial, "condition": entry.get("condition"),
                    "attempt": entry.get("attempt"), "created_utc": utc_now(), "paths_relative_to": "n8n/results",
                    "raw_folders": [_results_relative(folder) for folder in folders], "raw_files": raw,
                    "derived_folder": _results_relative(derived), "derived_files": reports, "scoring": scoring,
                    "scorer_sources": {path: sha(ROOT / path) for path in ("n8n/analyze_b.py", "n8n/clockwall_b.py")}}
        path = derived / EXPORT_MANIFEST
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        out.update(status="exported", raw_folders=manifest["raw_folders"], raw_files=len(raw),
                   scoring=scoring["status"], validity=scoring.get("validity"),
                   derived_folder=manifest["derived_folder"], reports={i["path"]: i["sha256"] for i in reports},
                   manifest=_results_relative(path), manifest_sha256=sha(path))
    except Exception as exc:  # noqa: BLE001  (recorded; the batch's own accounting goes on)
        out.update(status="export failed", error=f"{type(exc).__name__}: {exc}"[:300])
    return out


def export_pending(batch, manifest_path, env):
    """An attempt that ended without its export (an interrupt during scoring or export, or a runner that died while its
    trial ran) is exported when the batch resumes, once no trial holds the launcher lock."""
    pending = [t for t in batch["trials"]
               if "export" not in t and t.get("status") in ("finished", "interrupted", "unrecorded")]
    if not pending:
        return
    require_no_running_trial(env)
    for entry in pending:
        entry["export"] = export_attempt(entry, manifest_path.parent)
    write_batch(manifest_path, batch)


def run_batch(args, kind, batch, manifest_path, env):
    export_pending(batch, manifest_path, env)
    for condition in batch["conditions"]:
        attempt = next_attempt(batch, condition)
        if attempt is None:
            continue
        # Recheck immutable sources before EACH trial, even in a pilot.
        if source_hashes() != batch["source_hashes"]:
            raise ValueError("Code changed during the batch; stopping before the next trial")
        if kind == "screen":
            verify_freeze(args.freeze, [condition], run_checks=False)
        require_clock()
        require_no_running_trial(env)
        trial = trial_id(kind, batch["batch_id"], condition, attempt)
        entry = {"trial": trial, "condition": condition, "attempt": attempt, "status": "pending", "exit_code": None,
                 "started_utc": utc_now(),
                 # read here, outside the trial's network namespace; the harness copies it into the record
                 "host_identity": worker_identity(batch["host_profile"])}
        if attempt > 1:
            entry["rerun_of"] = [t for t in batch["trials"] if t.get("condition") == condition][-1]["trial"]
        batch["trials"].append(entry)
        write_batch(manifest_path, batch)  # the harness checks that this trial is pending in this batch
        print(f"Starting {trial}" + (f" (the one rerun of {entry['rerun_of']})" if attempt > 1 else ""), flush=True)
        returncode, interrupted = launch(condition, trial, env)
        entry.update({"status": "interrupted" if interrupted else "finished", "exit_code": returncode,
                      "ended_utc": utc_now()})
        if interrupted:
            entry["interrupted"] = True
        entry.update(trial_validity(trial, batch["host_profile"]))
        entry["accepted"] = returncode == 0 and not interrupted and entry.get("scored_valid") is True
        if not entry["accepted"] and attempt >= ATTEMPTS:
            batch["incomplete"].append(condition)  # R6.10: the trial and its replacement are both invalid
        # every attempt, accepted, invalid or interrupted, is scored and exported outside its raw folder
        entry["export"] = export_attempt(entry, manifest_path.parent)
        write_batch(manifest_path, batch)
        if interrupted:
            stop(batch, "interrupted")
            say(f"Batch interrupted during {trial} (launcher exit {returncode}). Inspect the raw trial record; "
                f"--resume {manifest_path} continues the batch.")
            return 130
        if not entry["accepted"]:
            why = f"returned {returncode}" if returncode else f"is invalid by the scorer: {entry.get('validity')}"
            stop(batch, f"{trial} {why}")
            after = (f"{condition} is incomplete; --resume continues with the remaining conditions"
                     if attempt >= ATTEMPTS else f"--resume {manifest_path} reruns {condition} once")
            print(f"Batch stopped: {trial} {why}. Inspect the raw trial record; {after}.", file=sys.stderr)
            return returncode if returncode > 0 else 2
    batch["completed"] = True
    write_batch(manifest_path, batch)
    print(f"Completed {kind} batch. Batch record: {manifest_path}" +
          (f". Incomplete conditions: {', '.join(batch['incomplete'])}" if batch["incomplete"] else ""))
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true", help="unscored full-length pilot; no research freeze required")
    parser.add_argument("--fast", action="store_true", help="short plumbing timings, only with --pilot")
    parser.add_argument("--dry-run", action="store_true", help="print plan only; no services or output files")
    parser.add_argument("--freeze-only", action="store_true", help="prepare a new freeze after offline checks")
    parser.add_argument("--freeze", type=Path, default=HERE / "FREEZE-expB.json")
    parser.add_argument("--predictions", type=Path, default=ROOT / "predictions" / "n8n-expB-cells.json")
    parser.add_argument("--validation", nargs="+", type=Path, default=[],
                        help="the full-length B0, PC, B1 and B1r validation pilot directories, required for "
                        "--freeze-only")
    parser.add_argument("--conditions", nargs="+", choices=CONDITIONS)
    parser.add_argument("--seed", type=int, default=None, help=f"screen order seed (default {DEFAULT_SEED})")
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--resume", type=Path, help="continue n8n/results/XB-<kind>-<id>/batch.json in its recorded "
                        "order with the same code and freeze; a failed condition is rerun once")
    args = parser.parse_args(argv)
    if args.fast and not args.pilot:
        parser.error("--fast is allowed only for an unscored --pilot")
    if args.fast and set(args.conditions or ()) & set(FAST_REFUSED):
        parser.error("FAST timing cannot run " + ", ".join(c for c in args.conditions if c in FAST_REFUSED) +
                     " (B.10: their restarts need n8n's own shutdown and start-up, which FAST does not shorten); "
                     "use full timing")
    if args.freeze_only and (args.pilot or args.fast or args.dry_run):
        parser.error("--freeze-only cannot be combined with --pilot, --fast or --dry-run")
    if args.freeze_only and not args.validation:
        parser.error("--freeze-only requires --validation directories for B0, PC, B1 and B1r")
    if args.conditions and len(set(args.conditions)) != len(args.conditions):
        parser.error("Duplicate conditions are not allowed")
    if args.resume and (args.freeze_only or args.conditions or args.seed is not None or args.batch_id is not None):
        parser.error("--resume keeps the recorded conditions, order, seed and batch id; it cannot be combined "
                     "with --freeze-only, --conditions, --seed or --batch-id")
    return args


def main(argv=None):
    args = parse_args(argv)
    with stop_signals():
        return _main(args)


def _main(args):
    seed = DEFAULT_SEED if args.seed is None else args.seed
    kind = "smoke" if args.fast else ("pilot" if args.pilot else "screen")
    if args.freeze_only:
        prepare_freeze(args.freeze, args.predictions, list(args.conditions or CONDITIONS), args.validation)
        return 0
    profile = declared_host_profile()       # B.0: declared, never inferred; a batch runs on one profile
    if args.resume:
        manifest_path = args.resume.resolve()
        batch = load_batch(manifest_path, kind)
        batch_id, conditions = batch["batch_id"], list(batch["conditions"])
    else:
        default = [c for c in PILOT_CONDITIONS if not (args.fast and c in FAST_REFUSED)] if args.pilot else CONDITIONS
        conditions = list(args.conditions or default)
        if not args.pilot:
            random.Random(seed).shuffle(conditions)
        batch_id = args.batch_id or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
        check_batch_id(kind, batch_id, conditions)
    if args.dry_run:
        plan = {"run_kind": kind, "timing_mode": "fast" if args.fast else "full", "conditions": conditions,
                "host_profile": profile, "requires_freeze": not args.pilot, "freeze": str(args.freeze),
                "readiness_checked": False}
        if args.resume:
            settle(batch)  # in memory only, so the plan shows what --resume would run
            plan.update(resume=str(manifest_path), next_trials=[
                trial_id(kind, batch_id, c, next_attempt(batch, c)) for c in conditions if next_attempt(batch, c)])
        print(json.dumps(plan, indent=2))
        return 0
    offline_checks()
    if not args.pilot:
        verify_freeze(args.freeze, conditions, run_checks=False)
    require_clock()
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValueError("Live trials require root in the disposable Ubuntu test environment")
    if args.resume:
        folder = manifest_path.parent
    else:
        if not args.pilot:
            refuse_reused_freeze(sha(args.freeze))
        folder = RESULTS / f"XB-{kind}-{batch_id}"
        folder.mkdir(parents=True, exist_ok=False)
        manifest_path = folder / "batch.json"
    with batch_lock(folder):
        if args.resume:
            batch = load_batch(manifest_path, kind)  # reread under the lock
            if batch.get("source_hashes") != source_hashes():
                raise ValueError("Source files changed since this batch started; a batch runs one code version")
            if not args.pilot and batch.get("freeze_sha256") != sha(args.freeze):
                raise ValueError("This freeze differs from the one the batch started with")
            if batch.get("host_profile") != profile:
                raise ValueError(f"This host's declared host profile is {profile!r}, but the batch started on "
                                 f"{batch.get('host_profile')!r}; a batch runs on one host profile")
            settle(batch)
        else:
            batch = {"schema_version": SCHEMA, "run_kind": kind, "batch_id": batch_id, "conditions": conditions,
                     "seed": seed, "host_profile": profile, "source_hashes": source_hashes(), "trials": [],
                     "incomplete": [], "completed": False, "stopped_reason": None, "stops": []}
            if not args.pilot:
                batch["freeze_sha256"] = sha(args.freeze)
        batch["stopped_reason"] = None
        write_batch(manifest_path, batch)
        env = dict(os.environ, STOPCHECK_FAST="1" if args.fast else "0", STOPCHECK_RUN_KIND=kind,
                   STOPCHECK_BATCH=str(manifest_path.resolve()))
        if not args.pilot:
            env["STOPCHECK_FROZEN_MANIFEST"] = str(args.freeze.resolve())
        else:
            env.pop("STOPCHECK_FROZEN_MANIFEST", None)
        try:
            return run_batch(args, kind, batch, manifest_path, env)
        except KeyboardInterrupt:
            stop(batch, "interrupted")
            raise
        except BaseException as exc:
            stop(batch, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            write_batch(manifest_path, batch)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("Interrupted. The batch record notes where the batch stopped.")
        sys.exit(130)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)
