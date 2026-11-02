#!/usr/bin/env python3
"""Run Experiment C trials one after another: unscored smoke runs (FAST) and pilots, or a source-bound,
exploratory screen (RULES-expC.md C.10, C.12). Patterns copied from n8n/run_expB.py: the source manifest, the
predictions schema check, the freeze, and (pre-freeze batch of 13 Oct) the scorer's admission of every attempt with
one rerun and --resume.

A dry run never launches anything. Freeze preparation never invents predictions: an operator-authored JSON file
is required (C.8). A screen requires a committed, clean repository, passing offline checks and an unchanged
freeze manifest; without a freeze it is refused.

A batch stops at the first attempt that is not accepted. An attempt is accepted only when its harness exited 0, it was
not interrupted, and analyze_c.py calls its raw record valid (for PC, also its gate passed: C.9); an exit status alone
never accepts it. Every attempt is kept in batch.json. --resume continues the recorded order with the same code and
reruns a failed condition once under its own trial id (<trial>-a2, as R6.10); a condition whose trial and rerun both
fail is incomplete.

    python3 run_expC.py --dry-run [--smoke | --pilot] [--conditions ...]
    python3 run_expC.py --smoke --conditions C0-HB         (root; unscored plumbing run)
    python3 run_expC.py --pilot                            (root; the C.10 pilot set, unscored)
    python3 run_expC.py --pilot --resume temporal/results/XC-pilot-<id>/batch.json
    python3 run_expC.py --freeze-only --validation <pilot trial folders...>
    python3 run_expC.py                                    (root; the screen, frozen manifest required)
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
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = HERE / "results"
CONDITIONS = ("C0-HB", "C0-NH", "C0-LA", "PC", "C1", "C2", "C2w", "C3", "C3w", "C4", "C5", "C5n", "C6", "C7",
              "C7l", "C8")
PILOT_SET = ("C0-HB", "C0-NH", "C0-LA", "PC", "C2", "C3", "C6")        # C.10
# C.10 (pre-freeze review): a FAST smoke of each procedure the pilot set never runs live (terminate, a cancel in the
# timer, WAIT_CANCELLATION_COMPLETED, a worker kill with a restart, kill then cancel then restart), on the freeze's source
SMOKE_SET = ("C1", "C2w", "C5", "C7", "C8")
SCHEMA = 1
# the record schema harness_c.SCHEMA writes (tested equal): from 2 the wall/monotonic receipt and the host profile are
# mandatory evidence (pre-freeze batch of 13 Oct), so a record written earlier cannot back a freeze or a campaign
TRIAL_RECORD_SCHEMA = 2
MEASURES = ("request_accepted", "state", "effects_after_request", "false_all_clear", "duplicates",
            "history_coverage", "pending_on_closed", "cancel_first_seen", "restart_outcome", "twin")   # C.8
LABEL = re.compile(r"^(P|C|O|S|M)(\|K\d{1,2}(,K\d{1,2})*)?(\+M)?$")                                 # C.7
# the reused files, unmodified, and the clock guard's tests, which offline_checks runs (bound as run_expB binds them)
REUSED = ("common/clockguard.py", "common/guards.py", "langgraph/recorder.py", "common/test_clockguard.py")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    return subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", "-C", str(ROOT), *args],
                          capture_output=True, text=True)


def source_hashes():
    """The files a freeze binds (C.12): Experiment C's code and rules, its tests, and the reused files."""
    paths = {p for p in HERE.glob("*.py") if p.is_file()} | {p for p in HERE.glob("*.md") if p.is_file()}
    paths |= {p for p in (ROOT / "tests").glob("test_*_c.py") if p.is_file()}
    paths |= {ROOT / p for p in REUSED}
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise ValueError("Missing source files: " + ", ".join(missing))
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(paths)}


def check_label(label):
    return isinstance(label, str) and (label == "—" or label == "-" or bool(LABEL.match(label)))


def read_predictions(path, conditions):
    """C.8 schema. A missing or empty field is refused; `unpredicted` is allowed as a prediction."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA:
        raise ValueError("Predictions require schema_version=1")
    if not isinstance(data.get("prior_exposure"), str) or not data["prior_exposure"].strip():
        raise ValueError("Predictions must describe prior exposure to Experiment C results")
    if not isinstance(data.get("configuration"), dict) or not data["configuration"]:
        raise ValueError("Predictions must declare configuration expectations")
    entries = data.get("conditions")
    if not isinstance(entries, dict):
        raise ValueError("Predictions require a conditions object")
    for condition in conditions:
        entry = entries.get(condition)
        measures = entry.get("measures") if isinstance(entry, dict) else None
        if not isinstance(measures, dict) or not set(MEASURES) <= set(measures):
            raise ValueError(f"{condition}: measures must include " + ", ".join(MEASURES))
        for name, cell in measures.items():
            if not isinstance(cell, dict) or not all(isinstance(cell.get(k), str) and cell[k].strip()
                                                     for k in ("prediction", "label", "basis", "refutation")):
                raise ValueError(f"{condition}/{name}: prediction, label, basis and refutation must be non-empty")
            if not check_label(cell["label"]):
                raise ValueError(f"{condition}/{name}: label {cell['label']!r} does not follow C.7")
    # C.8, C.10 (pre-freeze review): a validation run whose outcome existed before the predictions were fixed is named
    # here, by its trial id and record SHA-256, as prior exposure
    exposed = data.get("exposed_validation", [])
    if not isinstance(exposed, list) or not all(
            isinstance(x, dict) and isinstance(x.get("trial"), str) and x["trial"].strip()
            and isinstance(x.get("record_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", x["record_sha256"])
            for x in exposed):
        raise ValueError("exposed_validation must be a list of {trial, record_sha256} receipts")
    return data


def offline_checks():
    """C.12: every C test module, the clock guard's tests and the mutation check (temporal/mutate_c.py: one mutant per
    correction of the pre-freeze batch, each killed by an assertion of its focused test)."""
    commands = ([sys.executable, "-B", "-m", "unittest", "tests.test_analyze_c", "tests.test_harness_c",
                 "tests.test_runner_c", "tests.test_clockwall_c"],
                [sys.executable, "-B", "-m", "unittest", "discover", "-s", "common", "-p", "test_clockguard.py"],
                [sys.executable, "-B", str(HERE / "mutate_c.py")])
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if result.returncode:
            raise ValueError("Offline check failed: " + " ".join(command) + "\n" +
                             (result.stdout + result.stderr)[-6000:])


def check_repository():
    head, status = git("rev-parse", "HEAD"), git("status", "--porcelain")
    if head.returncode or status.returncode:
        raise ValueError("Cannot verify repository HEAD and working tree")
    if status.stdout.strip():
        raise ValueError("Screen requires committed code and freeze; working tree is dirty:\n" + status.stdout)
    return head.stdout.strip()


def require_committed(path, what):
    """As run_expB (review F3): git status omits ignored files, so check each freeze input itself: inside the
    repository, not ignored, tracked and unchanged."""
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


def pilot_problems(record, expected_hashes, rows=None):
    """C.9, C.10: what a full-length pilot record must show before it can back a freeze. `rows` are its scored
    rows (needed for PC). Returns the problems found; an empty list means the record qualifies."""
    out = []
    if record.get("fast_mode") or record.get("run_kind") != "pilot":
        out.append("freeze validation requires full-length unscored pilots")
    if type(record.get("schema_version")) is not int or record["schema_version"] != TRIAL_RECORD_SCHEMA:
        out.append(f"current-source pilots need record schema_version {TRIAL_RECORD_SCHEMA}; found "
                   f"{record.get('schema_version')!r}")
    if record.get("source_hashes") != expected_hashes:
        out.append("pilot source manifest differs from current code")
    if any(record.get(k) for k in ("error", "void", "untested", "rerun_needed", "guard_violations")):
        out.append("pilot has an error, invalidity, untested work or a guard violation")
    for key in ("clock_start", "clock_end", "clock_across_trial"):
        if (record.get(key) or {}).get("ok") is not True:
            out.append(f"missing or failed {key}")
    # C.9 (item 5): the prospective wall/monotonic receipt; the scorer's own recomputation is part of its validity
    if ((record.get("clock_across_trial") or {}).get("wall_clock") or {}).get("ok") is not True:
        out.append("missing or failed wall/monotonic clock receipt (clock_across_trial.wall_clock)")
    pre = record.get("pre_check") or {}
    if pre.get("ok") is not True or pre.get("completed_before_deadline") is not True:
        out.append("missing pre-intervention evidence, or the check did not end before planned t_stop")
    probe = record.get("probe") or {}
    if "ok" not in (probe.get("pre_check") or {}):
        out.append("the pre-probe check was not evaluated")
    if probe.get("error"):
        out.append(f"the durability probe failed: {probe['error']}")
    restarts = probe.get("restarts") or []
    if not restarts or not all(x.get("ok") for x in restarts):
        out.append("a durability-probe restart did not complete")
    out += restart_identity_problems(record)
    if record.get("condition") == "C6" and "ok" not in ((record.get("intervention") or {}).get("precondition") or {}):
        out.append("the C6 precondition was not evaluated")
    if record.get("condition") == "PC":
        rows = rows or []
        gate = next((r for r in rows if r.get("measure") == "PC"), None)
        if not gate or gate.get("kind") != "pass":
            out.append("the PC gate did not pass")
        probe_row = next((r for r in rows if r.get("measure") == "restart" and r.get("outcome") == "durability probe"),
                         None)
        if not probe_row or "re-run" not in str(probe_row.get("kind")):
            out.append("the durability probe did not detect PC's re-run")
    return out


PROBE_RESTARTED = ("server", "worker-target", "worker-twin")      # the durability probe restarts all three (C.4)


def restart_identity_problems(record):
    """C.1, C.10 (pre-freeze review): every restarted process got a new PID and start time (harness_c.Procs.start records
    the comparison as `replaces`), and the record shows the durability probe's restarts of the server and both
    workers."""
    replaced = [x for x in record.get("launches") or [] if isinstance(x, dict) and "replaces" in x]
    out = []
    if any((x.get("replaces") or {}).get("new_identity") is not True for x in replaced):
        out.append("a restarted process did not get a new PID and start time (C.1)")
    missing = [n for n in PROBE_RESTARTED if n not in {x.get("name") for x in replaced}]
    if missing:
        out.append("no restart with a new identity is recorded for " + ", ".join(missing))
    return out


def smoke_problems(record, expected_hashes):
    """C.10 (pre-freeze review): what a FAST smoke must show before it can stand for its procedure at the freeze: the
    freeze's source and schema, and the procedure run end to end (t_stop stamped, every request exited 0 within its
    timeout, every signal's exit observed, every in-watch restart ready, the probe without an error). A smoke is
    plumbing: it is never scored, so no validity verdict is asked of it."""
    out = []
    if record.get("run_kind") != "smoke" or record.get("fast_mode") is not True:
        out.append("a procedure smoke must be a FAST smoke run")
    if type(record.get("schema_version")) is not int or record["schema_version"] != TRIAL_RECORD_SCHEMA:
        out.append(f"record schema_version {record.get('schema_version')!r} is not the current {TRIAL_RECORD_SCHEMA}")
    if record.get("source_hashes") != expected_hashes:
        out.append("the smoke's source manifest differs from current code")
    if any(record.get(k) for k in ("error", "void", "untested", "rerun_needed")):
        out.append("the smoke has an error, invalidity or untested work")
    box = record.get("intervention") or {}
    if not isinstance(box.get("t_stop"), dict) or box.get("error") or box.get("withheld"):
        out.append("the procedure did not start, or its thread failed")
    actions = box.get("actions") or []
    if not actions or not all(
            (a.get("rc") == 0 and not a.get("timed_out")) if a.get("name") in ("cancel", "terminate")
            else isinstance(a.get("exit_mono"), (int, float)) for a in actions):
        out.append("an action of the procedure failed (a request did not exit 0, or a signal's exit was not observed)")
    if not all(rs.get("ok") is True for rs in box.get("restarts") or []):
        out.append("an in-watch restart did not complete")
    if (record.get("probe") or {}).get("error"):
        out.append("the durability probe failed")
    return out


def _predictions_binding(record, record_sha, predictions_sha, exposed):
    """C.8, C.10 (pre-freeze review): a validation run's outcome may not precede the predictions unseen. Either the
    predictions file the harness hashed when the run started is the frozen one, or the predictions name the run's
    receipt (trial id and record SHA-256) in exposed_validation. None otherwise."""
    at_start = (record.get("predictions_at_start") or {}).get("sha256")
    if isinstance(at_start, str) and at_start == predictions_sha:
        return "fixed before this run"
    if {"trial": record.get("trial"), "record_sha256": record_sha} in [
            {"trial": x.get("trial"), "record_sha256": x.get("record_sha256")} for x in exposed or []]:
        return "disclosed as prior exposure"
    return None


def validate_pilots(paths, expected_hashes, host_profile, predictions_sha, exposed):
    """Freeze gate (C.9, C.10, C.12; item 7 and the pre-freeze review): one full-length validation pilot of every
    condition of PILOT_SET (C.10's required set: the three no-stop references, PC, C2, C3 and C6) and one FAST smoke of
    every condition of SMOKE_SET, each on the exact source the freeze binds, in the current record schema and declaring
    the freeze's host profile. Each pilot must be valid by the scorer and show what C.10 requires (pilot_problems; PC's
    gate and its probe re-run come from its scored rows); the C2 or C3 pilot must capture a closed run's pending-activity
    list; each smoke must have run its procedure (smoke_problems). Every run's outcome must follow the predictions, or
    the predictions must disclose it (_predictions_binding)."""
    scorer = _scorer()
    found, smokes, receipts, rows_by = set(), set(), [], {}
    for folder in paths:
        record_path = Path(folder) / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        condition, kind = record.get("condition"), record.get("run_kind")
        smoke = kind == "smoke" and condition in SMOKE_SET
        if not smoke and condition not in PILOT_SET:
            continue
        seen = smokes if smoke else found
        if condition in seen:
            raise ValueError(f"{folder}: a freeze binds one validation {'smoke' if smoke else 'pilot'} per condition; "
                             f"{condition} is given twice")
        record_sha = sha(record_path)
        if smoke:
            problems = smoke_problems(record, expected_hashes)
        else:
            try:
                rows = scorer.score_trial(scorer.load(str(folder)))
            except Exception as exc:  # noqa: BLE001  (an unscorable pilot cannot validate a freeze)
                raise ValueError(f"{folder}: the scorer cannot read this pilot: {type(exc).__name__}: {exc}") from None
            rows_by[condition] = rows
            problems = pilot_problems(record, expected_hashes, rows)
            validity = next((r for r in rows if r.get("measure") == "validity"), {})
            if validity.get("kind") != "valid":
                problems.append(f"the scorer calls this pilot invalid: {validity.get('verdict')}")
        if recorded_host_profile(record) != host_profile:
            problems.append(f"the run's declared host profile {recorded_host_profile(record)!r} is not the "
                            f"freeze's {host_profile!r}")
        binding = _predictions_binding(record, record_sha, predictions_sha, exposed)
        if binding is None:
            problems.append("the predictions were not fixed before this run (its predictions_at_start SHA-256 is not "
                            "the frozen file's) and exposed_validation does not name it (C.8, C.10)")
        if problems:
            raise ValueError(f"{folder}: " + "; ".join(problems))
        seen.add(condition)
        receipts.append({"condition": condition, "kind": "smoke" if smoke else "pilot", "trial": record.get("trial"),
                         "record_sha256": record_sha, "folder": str(Path(folder).resolve()),
                         "host_profile": host_profile, "predictions": binding})
    if found != set(PILOT_SET):
        raise ValueError("Freeze requires successful, current-source full-length pilots for " + ", ".join(PILOT_SET)
                         + "; missing: " + ", ".join(sorted(set(PILOT_SET) - found)))
    if smokes != set(SMOKE_SET):
        raise ValueError("Freeze requires a current-source FAST smoke of each procedure the pilots do not run: "
                         + ", ".join(SMOKE_SET) + "; missing smoke: " + ", ".join(sorted(set(SMOKE_SET) - smokes)))
    # C.10: one closed run's pending-activity list, from C2 or C3, so the row has a captured example
    pending = [r.get("kind") for c in ("C2", "C3") for r in rows_by.get(c, [])
               if r.get("measure") == "pending_on_closed"]
    if not any(k in ("yes", "no") for k in pending):
        raise ValueError("Freeze requires the C2 or C3 pilot to capture a closed run's pending-activity list "
                         f"(its pending_on_closed row reads {pending})")
    return receipts


def prepare_freeze(path, predictions, conditions, validation, seed):
    """`conditions` is the screen's order, shuffled with `seed` (plan); the manifest binds both (C.10)."""
    path, predictions = Path(path).resolve(), Path(predictions).resolve()
    data = read_predictions(predictions, conditions)
    exposed = data.get("exposed_validation") if isinstance(data, dict) else None
    profile = declared_host_profile()
    offline_checks()
    hashes = source_hashes()
    predictions_sha = sha(predictions)
    receipts = validate_pilots(validation, hashes, profile, predictions_sha, exposed if isinstance(exposed, list) else [])
    head = git("rev-parse", "HEAD")
    if head.returncode:
        raise ValueError("Cannot identify repository HEAD")
    manifest = {"schema_version": SCHEMA, "prepared_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "prepared_from_head": head.stdout.strip(), "timing_mode": "full", "conditions": list(conditions),
                "seed": seed, "order": list(conditions), "host_profile": profile,
                "prediction_file": predictions.relative_to(ROOT).as_posix(), "prediction_sha256": predictions_sha,
                "source_hashes": hashes, "validation_pilots": receipts}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:      # never replace an existing freeze
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    print(f"Prepared {path}. Commit this manifest, the predictions, code and rules before a screen.")


def verify_freeze(path, conditions, run_checks=True, host_profile=None, seed=None, order=None):
    """The freeze, checked for this host. host_profile is the caller's declared profile (a screen trial's harness passes
    its own); by default this process's. A screen batch passes its seed and its whole condition order, which must be the
    frozen ones (C.10, pre-freeze review)."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Screen refused: no freeze manifest at {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA or manifest.get("timing_mode") != "full":
        raise ValueError("Unsupported freeze schema or timing mode")
    if not set(conditions) <= set(manifest.get("conditions", [])):
        raise ValueError("Selected conditions are not all registered in the freeze")
    if seed is not None and (manifest.get("seed") is None or manifest.get("seed") != seed):
        raise ValueError(f"The screen's seed {seed!r} is not the frozen seed {manifest.get('seed')!r} (C.10)")
    if order is not None and manifest.get("order") != list(order):
        raise ValueError(f"The screen's order {list(order)} is not the frozen order {manifest.get('order')} (C.10)")
    prediction_path = (ROOT / manifest["prediction_file"]).resolve()
    if ROOT != prediction_path and ROOT not in prediction_path.parents:
        raise ValueError("Prediction path escapes the repository")
    read_predictions(prediction_path, conditions)
    if sha(prediction_path) != manifest.get("prediction_sha256"):
        raise ValueError("Prediction file changed after the freeze")
    if source_hashes() != manifest.get("source_hashes"):
        raise ValueError("Source files changed after the freeze")
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


CLEANUP_GRACE_S = 180.0     # as harness_c.CLEANUP_GRACE_S, plus the namespace wrapper's own clean-up


def run_trial(cmd, env, cwd, grace_s=CLEANUP_GRACE_S):
    """Run one harness_c.py trial. On an interrupt the trial cleans up by itself (C.4 step 7), so the runner waits
    for it, up to grace_s, instead of killing it (killing it would leave its server, workers and receiver running);
    then the interrupt is passed on."""
    p = subprocess.Popen(cmd, cwd=cwd, env=env)
    try:
        return p.wait()
    except KeyboardInterrupt:
        print(f"Interrupted: waiting up to {grace_s:g} s for the trial to clean up", file=sys.stderr)
        try:
            p.wait(timeout=grace_s)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            p.kill()
            p.wait()
        raise


def require_clock():
    sys.path.insert(0, str(ROOT / "common"))
    import clockguard
    result = clockguard.check(2.0)
    if not result.get("ok"):
        raise ValueError("Clock guard failed: " + "; ".join(result.get("reasons", [])))


# ============================ host profile and worker identity (C.0, C.12; item 6) ============================
# The declared host profiles (harness_c.HOST_PROFILES, tested equal). A batch runs on one profile; every attempt it
# accepts must declare it, and a freeze binds the profile of its validation pilots (prepare_freeze, verify_freeze).
HOST_PROFILES = ("wsl", "linux-vm")
# linux-vm: each worker's identity, read before every trial from the GCE metadata server by the runner, outside the
# trial's network namespace (which has no route to it), and stored in the trial's batch entry, from which the harness
# copies it into the record. A missing identity is recorded as missing and never refuses a trial: it is provenance, not
# a configuration value the freeze compares, and a plain Linux VM need not be on GCE.
GCE_METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/"
GCE_IDENTITY = (("id", "id"), ("name", "name"), ("zone", "zone"), ("machine_type", "machine-type"),
                ("cpu_platform", "cpu-platform"), ("image", "image"), ("preemptible", "scheduling/preemptible"),
                ("on_host_maintenance", "scheduling/on-host-maintenance"))


def declared_host_profile(env=None):
    """The host profile this process declares (STOPCHECK_HOST_PROFILE, default wsl), as the harness reads it; never
    inferred from the machine."""
    profile = (os.environ if env is None else env).get("STOPCHECK_HOST_PROFILE", "wsl")
    if profile not in HOST_PROFILES:
        raise ValueError(f"STOPCHECK_HOST_PROFILE must be one of {', '.join(HOST_PROFILES)}, not {profile!r}")
    return profile


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


def recorded_host_profile(record):
    """The host profile a record declares (harness_c.new_record: host_profile.declared), or None."""
    block = record.get("host_profile")
    return block.get("declared") if isinstance(block, dict) else None


# ============================ batches: every attempt admitted by the scorer (C.9, C.10; item 4) ======================
ATTEMPTS = 2                                             # C.9: an invalid trial is rerun once (as R6.10)
TRIAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,40}")  # the trial ids harness_c.main accepts
RAW_FOLDER_SUFFIX = r"-\d{8}-\d{6}"                      # harness_c.main: <trial>-%Y%m%d-%H%M%S
BATCH_SCHEMA = 2          # batch.json of the pre-freeze batch's runner (attempts, verdicts, reruns); 1 is older
DEFAULT_SEED = 20261012


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def trial_id(kind, batch_id, condition, attempt=1):
    """Trial ids are never reused: a rerun gets its own id, raw folder and namespace."""
    base = f"XC-{kind}-{batch_id}-{condition}"
    return base if attempt == 1 else f"{base}-a{attempt}"


def check_batch_id(kind, batch_id, conditions):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", batch_id or ""):
        raise ValueError("Invalid batch id: use letters, digits and hyphens, starting with a letter or digit")
    for condition in conditions:
        for attempt in range(1, ATTEMPTS + 1):
            trial = trial_id(kind, batch_id, condition, attempt)
            if not TRIAL_ID.fullmatch(trial):
                raise ValueError(f"Batch id too long: trial id {trial} exceeds the harness's 41-character limit")


def write_batch(path, batch):
    """Replace batch.json in one step."""
    temporary = Path(path).with_name(Path(path).name + ".tmp")
    temporary.write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextlib.contextmanager
def batch_lock(folder):
    """One runner per batch, so a resume cannot run beside the runner still using it. Live trials run only as root on
    Linux, where fcntl exists; without it (offline tests on Windows) no lock is taken."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(Path(folder) / "batch.lock", "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError(f"Another runner is using this batch: {folder}") from None
        yield


def load_batch(path, kind):
    path = Path(path).resolve()
    prefix = f"XC-{kind}-"
    if path.name != "batch.json" or path.parent.parent != RESULTS.resolve() or not path.parent.name.startswith(prefix):
        raise ValueError(f"--resume needs {RESULTS / (prefix + '<id>') / 'batch.json'} for this run kind")
    batch = json.loads(path.read_text(encoding="utf-8"))
    if batch.get("schema_version") != BATCH_SCHEMA or batch.get("run_kind") != kind:
        raise ValueError(f"{path}: another batch schema or run kind (a batch written before 14 Oct cannot resume)")
    if batch.get("completed"):
        raise ValueError(f"Batch already completed: {path}")
    conditions = batch.get("conditions")
    if (not isinstance(conditions, list) or not conditions or len(set(conditions)) != len(conditions)
            or not set(conditions) <= set(CONDITIONS)):
        raise ValueError(f"{path}: invalid recorded condition order")
    if not isinstance(batch.get("trials"), list) or any(t.get("condition") not in conditions for t in batch["trials"]):
        raise ValueError(f"{path}: recorded trials do not match its conditions")
    check_batch_id(kind, batch.get("batch_id"), conditions)
    batch.setdefault("incomplete", [])
    batch.setdefault("stops", [])
    return batch


def accepted(entry):
    """C.9, C.10: only the scorer's verdict accepts an attempt; an exit status alone never does."""
    return entry.get("accepted") is True


def next_attempt(batch, condition):
    """The attempt a condition still needs, or None once it is accepted or has used its one rerun."""
    attempts = [t for t in batch["trials"] if t.get("condition") == condition]
    if any(accepted(t) for t in attempts) or condition in batch["incomplete"] or len(attempts) >= ATTEMPTS:
        return None
    return len(attempts) + 1


def judge(entry, batch):
    """The scorer's verdict on one attempt, stored in its batch entry."""
    entry.update(trial_validity(entry["trial"], batch.get("host_profile")))
    entry["accepted"] = (entry.get("exit_code") == 0 and not entry.get("interrupted")
                         and entry.get("scored_valid") is True)


def settle(batch):
    """On resume: an attempt the runner never finished recording still counts (so it can leave a condition
    incomplete), and one that ended before its verdict was stored is judged now, as run_batch would have judged it."""
    for entry in batch["trials"]:
        if entry.get("status") == "pending":
            entry["status"] = "unrecorded"
        elif entry.get("status") in ("finished", "interrupted") and "accepted" not in entry:
            judge(entry, batch)
            entry["judged_on_resume_utc"] = utc_now()
    for condition in batch["conditions"]:
        attempts = [t for t in batch["trials"] if t.get("condition") == condition]
        if (len(attempts) >= ATTEMPTS and not any(accepted(t) for t in attempts)
                and condition not in batch["incomplete"]):
            batch["incomplete"].append(condition)
    batch["pc_gate"] = pc_gate(batch)


def pc_gate(batch):
    """C.9: PC is the gate. 'passed' once an accepted PC attempt (whose gate passed, trial_validity) is recorded; until
    then a batch's activity-stage cells are void ('failed: rerun pending' or 'failed: incomplete')."""
    if "PC" not in batch["conditions"]:
        return "not in this batch"
    attempts = [t for t in batch["trials"] if t.get("condition") == "PC"]
    if any(accepted(t) for t in attempts):
        return "passed"
    if not attempts:
        return "not run yet"
    return "failed: incomplete" if "PC" in batch["incomplete"] else "failed: rerun pending"


def stop(batch, reason):
    batch["stopped_reason"] = reason
    batch["stops"].append({"utc": utc_now(), "reason": reason})


def raw_folders(trial):
    """Every raw folder the harness made for this trial id, with or without a record (an exact match, so a rerun's
    folders, <trial>-a2-..., and another condition's, such as C2w's beside C2's, are never taken for this one)."""
    if not RESULTS.is_dir():
        return []
    pattern = re.compile(re.escape(trial) + RAW_FOLDER_SUFFIX)
    return sorted(p for p in RESULTS.iterdir() if p.is_dir() and pattern.fullmatch(p.name))


def _scorer():
    """The scorer, loaded from its file, so admission uses the same verdicts as scoring."""
    spec = importlib.util.spec_from_file_location("analyze_c_for_run_expC", HERE / "analyze_c.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trial_validity(trial, host_profile=None):
    """The scorer's validity verdict on the attempt's own raw record (C.9); for PC also its gate (C.9: PC is the gate),
    so a PC whose gate did not pass is rerun like an invalid trial."""
    records = [folder for folder in raw_folders(trial) if (folder / "record.json").is_file()]
    if len(records) != 1:
        return {"record": None, "scored_valid": False,
                "validity": f"expected one raw record for {trial}, found {len(records)}"}
    folder = records[0]
    try:
        record = json.loads((folder / "record.json").read_text(encoding="utf-8"))
        if type(record.get("schema_version")) is not int or record["schema_version"] != TRIAL_RECORD_SCHEMA:
            # item 5: a record below the current schema would take the scorer's legacy path, which skips the
            # wall/monotonic guard; a campaign never admits it
            return {"record": str(folder), "scored_valid": False,
                    "validity": f"record schema_version {record.get('schema_version')!r} is not the current "
                                f"{TRIAL_RECORD_SCHEMA}"}
        if host_profile is not None and recorded_host_profile(record) != host_profile:
            # item 6: a batch accepts only records that declare its host profile
            return {"record": str(folder), "scored_valid": False,
                    "validity": f"the record's declared host profile {recorded_host_profile(record)!r} is not the "
                                f"batch's {host_profile!r}"}
        scorer = _scorer()
        rows = scorer.score_trial(scorer.load(str(folder)))
        validity = next(r for r in rows if r.get("measure") == "validity")
    except Exception as exc:  # noqa: BLE001  (an unscorable record is not a valid trial)
        return {"record": str(folder), "scored_valid": False, "validity": f"unscorable: {type(exc).__name__}: {exc}"}
    out = {"record": str(folder), "scored_valid": validity.get("kind") == "valid", "validity": validity.get("verdict")}
    if record.get("condition") == "PC":
        gate = next((r for r in rows if r.get("measure") == "PC"), None)
        out["pc_gate"] = gate.get("kind") if gate else "not scored"
        if out["pc_gate"] != "pass":
            out["scored_valid"] = False
            out["validity"] = f"{out['validity']}; PC gate: {out['pc_gate']}"
    return out


# ============================ score and export every attempt (C.12; item 7, as B.13) ============================
# After each attempt, accepted or not, interrupted or without a record, the runner scores its raw folder into
# <batch folder>/derived/<trial>/ (scored_c.jsonl and summary_c.md) and writes beside them a SHA-256 manifest of every
# raw file and of those reports; batch.json references both. Raw folders are only read: nothing is written into them.
# Paths in the manifest are relative to temporal/results.
EXPORT_SCHEMA = 1
DERIVED = "derived"
EXPORT_MANIFEST = "manifest-sha256.json"
REPORTS = ("scored_c.jsonl", "summary_c.md")
SCORER_SOURCES = ("temporal/analyze_c.py", "temporal/clockwall_c.py")


def _results_relative(path):
    return Path(path).resolve().relative_to(RESULTS.resolve()).as_posix()


def _file_entries(folder):
    """Every file under folder, recursively and in a stable order: path (relative to temporal/results), bytes, sha256."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            out.append({"path": _results_relative(path), "bytes": path.stat().st_size, "sha256": sha(path)})
    return out


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
        for name in REPORTS:                         # a re-export never leaves an earlier report behind
            (derived / name).unlink(missing_ok=True)
        scoring = {"status": "not scored", "why": f"{len(records)} raw records for this trial (exactly one is needed)"}
        if len(records) == 1:
            try:
                scorer = _scorer()
                T = scorer.load(str(records[0]))
                rows = scorer.score_trial(T)
                with open(derived / REPORTS[0], "w", encoding="utf-8", newline="\n") as stream:
                    stream.write("".join(json.dumps(row, default=str) + "\n" for row in rows))
                with open(derived / REPORTS[1], "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(scorer.summarize(T, rows))
                validity = next((r for r in rows if r.get("measure") == "validity"), {})
                scoring = {"status": "scored", "rows": len(rows), "validity": validity.get("verdict")}
            except Exception as exc:  # noqa: BLE001  (an unscorable attempt is still exported)
                scoring = {"status": "not scored", "why": f"scorer failed: {type(exc).__name__}: {exc}"[:300]}
        reports = [item for item in _file_entries(derived) if Path(item["path"]).name != EXPORT_MANIFEST]
        manifest = {"schema_version": EXPORT_SCHEMA, "trial": trial, "condition": entry.get("condition"),
                    "attempt": entry.get("attempt"), "created_utc": utc_now(), "paths_relative_to": "temporal/results",
                    "raw_folders": [_results_relative(folder) for folder in folders], "raw_files": raw,
                    "derived_folder": _results_relative(derived), "derived_files": reports, "scoring": scoring,
                    "scorer_sources": {path: sha(ROOT / path) for path in SCORER_SOURCES}}
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


def export_pending(batch, manifest_path):
    """An attempt that ended without its export (an interrupt during scoring or export, or a runner that died while its
    trial ran) is exported when the batch resumes."""
    pending = [t for t in batch["trials"]
               if "export" not in t and t.get("status") in ("finished", "interrupted", "unrecorded")]
    for entry in pending:
        entry["export"] = export_attempt(entry, manifest_path.parent)
    if pending:
        write_batch(manifest_path, batch)


def run_batch(args, kind, batch, manifest_path, env):
    export_pending(batch, manifest_path)
    for condition in batch["conditions"]:
        attempt = next_attempt(batch, condition)
        if attempt is None:
            continue
        if source_hashes() != batch["source_hashes"]:          # recheck before EACH trial
            raise ValueError("Code changed during the batch; stopping before the next trial")
        if kind == "screen":
            verify_freeze(args.freeze, [condition], run_checks=False, seed=batch.get("seed"),
                          order=batch["conditions"])
        require_clock()
        trial = trial_id(kind, batch["batch_id"], condition, attempt)
        entry = {"trial": trial, "condition": condition, "attempt": attempt, "status": "pending", "exit_code": None,
                 "started_utc": utc_now(),
                 # read here, outside the trial's network namespace; the harness copies it into the record
                 "host_identity": worker_identity(batch["host_profile"])}
        if attempt > 1:
            entry["rerun_of"] = [t for t in batch["trials"] if t.get("condition") == condition][-1]["trial"]
        batch["trials"].append(entry)
        write_batch(manifest_path, batch)
        print(f"Starting {trial}" + (f" (the one rerun of {entry['rerun_of']})" if attempt > 1 else ""), flush=True)
        interrupted = False
        try:
            returncode = run_trial([sys.executable, "-B", str(HERE / "harness_c.py"), condition, trial], env, HERE)
        except KeyboardInterrupt:
            returncode, interrupted = None, True
        entry.update(status="interrupted" if interrupted else "finished", exit_code=returncode, ended_utc=utc_now())
        if interrupted:
            entry["interrupted"] = True
        judge(entry, batch)
        if not entry["accepted"] and attempt >= ATTEMPTS:
            batch["incomplete"].append(condition)              # R6.10: the trial and its rerun both failed
        batch["pc_gate"] = pc_gate(batch)
        # every attempt, accepted, invalid or interrupted, is scored and exported outside its raw folder
        entry["export"] = export_attempt(entry, manifest_path.parent)
        write_batch(manifest_path, batch)
        if interrupted:
            stop(batch, f"interrupted during {trial}")
            print(f"Batch interrupted during {trial}. Inspect the raw trial record; --resume {manifest_path} "
                  "continues the batch.", file=sys.stderr)
            return 130
        if not entry["accepted"]:
            why = f"returned {returncode}" if returncode else f"is invalid by the scorer: {entry.get('validity')}"
            stop(batch, f"{trial} {why}")
            after = (f"{condition} is incomplete; --resume continues with the remaining conditions"
                     if attempt >= ATTEMPTS else f"--resume {manifest_path} reruns {condition} once")
            print(f"Batch stopped: {trial} {why}. Inspect the raw trial record; {after}.", file=sys.stderr)
            return returncode if returncode and returncode > 0 else 2
    batch["completed"] = True
    write_batch(manifest_path, batch)
    print(f"Completed {kind} batch. Batch record: {manifest_path}" +
          (f". Incomplete conditions: {', '.join(batch['incomplete'])}" if batch["incomplete"] else ""))
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--pilot", action="store_true", help="unscored full-length pilot; no freeze required")
    kind.add_argument("--smoke", action="store_true", help="unscored FAST plumbing run (STOPCHECK_FAST=1)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only; no services, no files")
    parser.add_argument("--freeze-only", action="store_true", help="prepare a new freeze after offline checks")
    parser.add_argument("--freeze", type=Path, default=HERE / "FREEZE-expC.json")
    parser.add_argument("--predictions", type=Path, default=ROOT / "predictions" / "temporal-expC-cells.json")
    parser.add_argument("--validation", nargs="+", type=Path, default=[], help="full-length pilot trial folders")
    parser.add_argument("--conditions", nargs="+", choices=CONDITIONS)
    parser.add_argument("--seed", type=int, default=None, help=f"screen order seed (default {DEFAULT_SEED})")
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--resume", type=Path, help="continue temporal/results/XC-<kind>-<id>/batch.json in its "
                        "recorded order with the same code; a failed condition is rerun once")
    args = parser.parse_args(argv)
    if args.freeze_only and (args.pilot or args.smoke or args.dry_run):
        parser.error("--freeze-only cannot be combined with --pilot, --smoke or --dry-run")
    if args.freeze_only and not args.validation:
        parser.error("--freeze-only requires --validation pilot folders")
    if args.conditions and len(set(args.conditions)) != len(args.conditions):
        parser.error("Duplicate conditions are not allowed")
    if args.resume and (args.freeze_only or args.conditions or args.seed is not None or args.batch_id is not None):
        parser.error("--resume keeps the recorded conditions, order, seed and batch id; it cannot be combined "
                     "with --freeze-only, --conditions, --seed or --batch-id")
    return args


def plan(args):
    kind = "smoke" if args.smoke else ("pilot" if args.pilot else "screen")
    conditions = list(args.conditions or (PILOT_SET if args.pilot else (("C0-HB",) if args.smoke else CONDITIONS)))
    if kind == "screen":
        random.Random(DEFAULT_SEED if args.seed is None else args.seed).shuffle(conditions)
    return kind, conditions


def main(argv=None):
    args = parse_args(argv)
    kind, conditions = plan(args)
    seed = DEFAULT_SEED if args.seed is None else args.seed
    if args.freeze_only:
        prepare_freeze(args.freeze, args.predictions, conditions, args.validation, seed)
        return 0
    profile = declared_host_profile()          # C.0: declared, never inferred; a batch runs on one profile
    if args.resume:
        manifest_path = args.resume.resolve()
        batch = load_batch(manifest_path, kind)
        batch_id, conditions = batch["batch_id"], list(batch["conditions"])
        seed = batch.get("seed")
    else:
        batch_id = args.batch_id or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
        check_batch_id(kind, batch_id, conditions)
    if args.dry_run:
        plan_out = {"run_kind": kind, "timing_mode": "fast" if kind == "smoke" else "full", "conditions": conditions,
                    "seed": seed if kind == "screen" else None, "requires_freeze": kind == "screen",
                    "freeze": str(args.freeze), "host_profile": profile, "launches_anything": False}
        if args.resume:
            settle(batch)                       # in memory only: what --resume would run
            plan_out.update(resume=str(manifest_path), next_trials=[
                trial_id(kind, batch_id, c, next_attempt(batch, c)) for c in conditions if next_attempt(batch, c)])
        print(json.dumps(plan_out, indent=2))
        return 0
    offline_checks()
    if kind == "screen":
        verify_freeze(args.freeze, conditions, run_checks=False, seed=seed, order=conditions)
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValueError("Live trials require root (the SDK environment is root-only)")
    require_clock()
    if args.resume:
        folder = manifest_path.parent
    else:
        folder = RESULTS / f"XC-{kind}-{batch_id}"
        folder.mkdir(parents=True, exist_ok=False)
        manifest_path = folder / "batch.json"
    with batch_lock(folder):
        if args.resume:
            batch = load_batch(manifest_path, kind)            # reread under the lock
            if batch.get("source_hashes") != source_hashes():
                raise ValueError("Source files changed since this batch started; a batch runs one code version")
            if kind == "screen" and batch.get("freeze_sha256") != sha(args.freeze):
                raise ValueError("This freeze differs from the one the batch started with")
            if batch.get("host_profile") != profile:
                raise ValueError(f"This host's declared host profile is {profile!r}, but the batch started on "
                                 f"{batch.get('host_profile')!r}; a batch runs on one host profile")
            settle(batch)
        else:
            batch = {"schema_version": BATCH_SCHEMA, "run_kind": kind, "batch_id": batch_id,
                     "conditions": conditions, "seed": seed, "host_profile": profile,
                     "source_hashes": source_hashes(), "trials": [],
                     "incomplete": [], "completed": False, "stopped_reason": None, "stops": []}
            if kind == "screen":
                batch["freeze_sha256"] = sha(args.freeze)
            batch["pc_gate"] = pc_gate(batch)
        batch["stopped_reason"] = None
        write_batch(manifest_path, batch)
        env = dict(os.environ, STOPCHECK_FAST="1" if kind == "smoke" else "0", STOPCHECK_RUN_KIND=kind,
                   STOPCHECK_BATCH=str(manifest_path.resolve()),
                   # C.8, C.10: each trial records this file's SHA-256 before it runs (harness_c.predictions_receipt)
                   STOPCHECK_C_PREDICTIONS=str(Path(args.predictions).resolve()))
        env.pop("STOPCHECK_C_IN_NS", None)
        if kind == "screen":
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
        print("Interrupted. The batch record notes where the batch stopped.", file=sys.stderr)
        sys.exit(130)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)
