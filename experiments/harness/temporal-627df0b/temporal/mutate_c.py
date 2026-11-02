#!/usr/bin/env python3
"""Check that focused regression fixtures detect each correction of Experiment C's pre-freeze batch (13-14 Oct 2026)
and of the pre-freeze review of draft 5 (14 Oct 2026; labels R and r) if it were undone. Modelled on n8n/mutate_b.py (RULES-expB B.13); required by RULES-expC C.12 and run by
run_expC.offline_checks before any batch or freeze.

Each patch mutant reverts exact source fragments of one correction (each fragment must occur exactly once, so a mutant
can never silently leave the code unchanged); a stub mutant replaces a whole helper with one that disables the
correction. A mutant is killed only when its focused test, which passes on the clean file, fails an assertion; an
import or runtime error does not count. Every scorer, harness and runner mutant keeps HERE pointing at temporal/
(RELOCATE), because those files import their siblings from their own folder. A fixture skipped on this platform is
reported NOT RUN and left out of the count; the C fixtures used here run on Windows and Linux alike.

Nothing launches services or changes saved experiment records.

    python3 -B temporal/mutate_c.py
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

SCORER = (HERE / "analyze_c.py", "STOPCHECK_ANALYZE_C")
HARNESS = (HERE / "harness_c.py", "STOPCHECK_HARNESS_C")
RUNNER = (HERE / "run_expC.py", "STOPCHECK_RUN_EXPC")
CLOCKWALL = (HERE / "clockwall_c.py", "STOPCHECK_CLOCKWALL_C")
JOBS = (HERE / "jobs_c.py", "STOPCHECK_JOBS_C")             # the workload's pure parts (tests.test_harness_c's J)
RELOCATE = {SCORER: ("HERE = os.path.dirname(os.path.abspath(__file__))", f"HERE = {str(HERE)!r}"),
            HARNESS: ("HERE = os.path.dirname(os.path.abspath(__file__))", f"HERE = {str(HERE)!r}"),
            RUNNER: ("HERE = Path(__file__).resolve().parent", f"HERE = Path({str(HERE)!r})"),
            CLOCKWALL: None, JOBS: None}

TA, TH, TR, TC = "tests.test_analyze_c.", "tests.test_harness_c.", "tests.test_runner_c.", "tests.test_clockwall_c."

# (label, target, focused test, ((fragment, replacement), ...))
PATCH_MUTANTS = (
    # ---- item 1: the end-of-watch capability jobs ----
    ("1 capability jobs share a deadline at the watch end again", HARNESS,
     TH + "EndOfWatchCapability.test_jobs_never_delay_the_last_read_and_may_end_after_the_watch",
     (("        results = self.watch_capability(roles)\n",
       "        results = self.watch_capability(roles, watch_end - 0.1)\n"),)),
    ("1 capability jobs block the scheduled reads", HARNESS,
     TH + "EndOfWatchCapability.test_jobs_never_delay_the_last_read_and_may_end_after_the_watch",
     (("                cap_thread.start()\n", "                cap_thread.start()\n                cap_thread.join()\n"),)),
    ("1 the probe does not wait for the capability jobs", HARNESS,
     TH + "EndOfWatchCapability.test_jobs_that_outlast_their_bound_fail_the_trial",
     (('        thread.join(self.plan["capability_join_s"])\n        if thread.is_alive():\n',
       '        if False:\n'),)),
    ("1 validity ignores the capability jobs", SCORER,
     TA + "CapabilityJobs.test_a_failed_end_of_watch_capability_job_invalidates",
     (('        "readiness and capability jobs": readiness_and_capability(T)[0],\n',
       '        "readiness and capability jobs": True,\n'),)),
    ("1 PC asked for the dead target queue's job", SCORER, TA + "CapabilityJobs.test_pc_needs_only_the_twin_queues_job",
     (('    queues = [T.twin.get("task_queue")] if T.cond == "PC" else [T.target.get("task_queue"), '
       'T.twin.get("task_queue")]\n',
       '    queues = [T.target.get("task_queue"), T.twin.get("task_queue")]\n'),)),
    ("1 the probe's own readiness jobs counted in validity", SCORER,
     TA + "CapabilityJobs.test_the_probes_own_jobs_decide_its_outcome_not_validity",
     (("            continue                                     # a durability-probe restart (C.4 step 6)\n",
       "            pass\n"),)),
    # ---- item 2: the stop off the step boundary ----
    ("2 the stop back on the step-9 emission (anchor + 5.0 s)", HARNESS,
     TH + "StopPhase.test_the_stop_falls_half_a_step_from_every_step_emission",
     (('"stop_after_anchor_s": 5.5, "precheck_lead_s": 5.0,', '"stop_after_anchor_s": 5.0, "precheck_lead_s": 5.0,'),)),
    ("2 the check starts at the anchor and loses its 4.5 s budget", HARNESS,
     TH + "TimingPlans.test_pre_check_starts_close_to_the_stop",
     (('"stop_after_anchor_s": 5.5, "precheck_lead_s": 5.0,', '"stop_after_anchor_s": 5.5, "precheck_lead_s": 5.5,'),)),
    ("2 the HB delivery bound disabled again", HARNESS,
     TH + "StopPhase.test_the_delivery_bound_is_the_step_after_the_one_at_the_next_heartbeat",
     (('"probe_twin_steps": 3, "delivery_bound_step": 14,', '"probe_twin_steps": 3, "delivery_bound_step": None,'),)),
    ("2 a receiver gap hides an exceeded delivery bound", SCORER,
     TA + "DeliveryBound.test_exceeded_stands_on_a_receiver_gap_but_within_does_not",
     (('            kind = "exceeded" if late or x["final"] else (UNK if T.gap_in(ref, T.watch_end) else "within")\n',
       '            kind = UNK if T.gap_in(ref, T.watch_end) else ("exceeded" if late or x["final"] else "within")\n'),)),
    # ---- item 3: durable obligations versus quiet ----
    ("3 effects closed while the run is open", SCORER,
     TA + "DurableObligations.test_a_quiet_watch_never_closes_effects_while_the_run_is_open",
     (('    elif ob["state"] == "open":\n        effects_closed = "obligation open"\n',
       '    elif False:\n        effects_closed = "obligation open"\n'),)),
    ("3 a continued-as-new run counted as a closed obligation", SCORER,
     TA + "DurableObligations.test_a_continued_as_new_run_is_an_open_obligation",
     (('OBLIGATION_CLOSED = CLOSED - {"CONTINUED_AS_NEW"}', 'OBLIGATION_CLOSED = CLOSED'),)),
    ("3 work ceased credited on bounded quiet alone", SCORER,
     TA + "DurableObligations.test_quiet_after_a_cancel_without_a_final_or_a_dead_process_is_unresolved",
     (('    work_ceased = True if bounded_quiet and comp["state"] == "closed" else None\n',
       '    work_ceased = True if bounded_quiet else None\n'),)),
    ("3 any kill closes every execution", SCORER,
     TA + "DurableObligations.test_a_kill_does_not_close_an_execution_of_another_live_worker",
     (('        elif pids and None not in pids and all(p in dead and (not _num(T.watch_end) or dead[p] <= T.watch_end)\n'
       '                                               for p in pids):\n',
       '        elif dead:\n'),)),
    ("3 a kill without an observed exit closes its execution", SCORER,
     TA + "DurableObligations.test_a_kill_without_an_observed_exit_closes_nothing",
     (('    return {a["pid"]: a["exit_mono"] for a in T.actions\n',
       '    return {a["pid"]: a.get("exit_mono") or 0 for a in T.actions\n'),
      ('            if a.get("name") == "sigkill_worker" and a.get("pid") is not None and _num(a.get("exit_mono"))}\n',
       '            if a.get("name") == "sigkill_worker" and a.get("pid") is not None}\n'))),
    ("3 a STARTED attempt with no closed execution ignored", SCORER,
     TA + "DurableObligations.test_a_started_attempt_listed_without_a_closed_execution_is_unresolved",
     (('    unaccounted = [p for p in _pending_view(d) if p[1] in ACTIVE_PENDING and p[2] not in closed_attempts]\n',
       '    unaccounted = []\n'),)),
    ("3 an unrecognised status read as an open obligation", SCORER,
     TA + "DurableObligations.test_an_unrecognised_status_is_an_unknown_obligation",
     (('    if d and (d["status"] in OBLIGATION_CLOSED or d["status"] in OBLIGATION_OPEN):\n', '    if d:\n'),)),
    ("3 an earlier scheduled read stands in for the end-of-watch read", SCORER,
     TA + "DurableObligations.test_without_the_end_of_watch_read_an_earlier_running_read_decides_nothing",
     (('    hits = [r for r in T.scheduled if _num(want) and _num(r.get("offset")) and r["offset"] == want]\n',
       '    hits = [r for r in T.scheduled if not _num(T.watch_end) or r["t_end"] <= T.watch_end + 5]\n'),)),
    ("3 the verified stop ignores an open run", SCORER,
     TA + "DurableObligations.test_a_quiet_watch_never_closes_effects_while_the_run_is_open",
     (('    if ob["state"] == "open" or active:\n', '    if active:\n'),)),
    ("3 the verified stop left out of the summary", SCORER, TA + "DurableObligations.test_the_summary_reports_the_verdict",
     (('("events", "five events"), ("verified_stop", "verified stop"), ("twin", "twin control")',
       '("events", "five events"), ("twin", "twin control")'),)),
    # ---- item 4: the runner admits attempts by the scorer's validity ----
    ("4 exit status 0 accepts an attempt again", RUNNER,
     TR + "Admission.test_a_trial_the_scorer_calls_invalid_stops_the_batch_and_is_rerun_once",
     (('    entry["accepted"] = (entry.get("exit_code") == 0 and not entry.get("interrupted")\n'
       '                         and entry.get("scored_valid") is True)\n',
       '    entry["accepted"] = (entry.get("exit_code") == 0 and not entry.get("interrupted"))\n'),)),
    ("4 a second rerun allowed", RUNNER, TR + "Admission.test_a_condition_whose_trial_and_rerun_both_fail_is_incomplete",
     (("\nATTEMPTS = 2 ", "\nATTEMPTS = 3 "),)),
    ("4 the batch goes on past a failed attempt", RUNNER,
     TR + "Admission.test_a_trial_the_scorer_calls_invalid_stops_the_batch_and_is_rerun_once",
     (('        if not entry["accepted"]:\n            why = ', '        if False:\n            why = '),)),
    ("4 a PC whose gate failed admitted", RUNNER, TR + "RealScorerAdmission.test_pc_needs_its_gate_to_pass",
     (('        if out["pc_gate"] != "pass":\n', '        if False:\n'),)),
    # ---- item 5: the wall/monotonic clock gate ----
    ("5 a wall failure left out of the across-trial clock check", HARNESS,
     TH + "TrialClocks.test_a_wall_step_fails_even_when_mono_and_raw_agree",
     (('    if not wall["ok"]:\n        pace["ok"] = False\n', '    if False:\n        pace["ok"] = False\n'),)),
    ("5 the across-trial check reads only mono/raw pace again", HARNESS,
     TH + "TrialClocks.test_the_trial_check_reads_every_receiver_row",
     (('        return check_trial_clocks(self.rx.poll())\n',
       '        return clockguard.check_stamps([(e.get("mono"), e.get("raw")) for e in self.rx.effects()])\n'),)),
    ("5 records written in schema 1 again", HARNESS, TH + "TrialClocks.test_records_are_schema_2",
     (("SCHEMA = 2\nnow = time.monotonic", "SCHEMA = 1\nnow = time.monotonic"),)),
    ("5 the scorer's recomputation not required", SCORER,
     TA + "WallClockRecomputation.test_a_wall_step_in_the_receivers_stamps_invalidates_a_current_record",
     (('        checks["wall clock recomputed"] = T.wall_check.get("ok") is True\n',
       '        checks["wall clock recomputed"] = True\n'),)),
    ("5 the harness's wall receipt not required", SCORER,
     TA + "WallClockRecomputation.test_a_current_record_without_a_passing_harness_receipt_is_invalid",
     (('        checks["wall clock recorded"] = isinstance(harness_wall, dict) and harness_wall.get("ok") is True\n',
       '        checks["wall clock recorded"] = True\n'),)),
    ("5 legacy records judged by the current clock policy", SCORER,
     TA + "WallClockRecomputation.test_an_earlier_record_reports_the_recomputation_only_as_a_diagnostic",
     (('    if evidence_schema(R):\n        # C.9 (schema 2)', '    if True:\n        # C.9 (schema 2)'),)),
    ("5 a pilot without the wall receipt backs a freeze", RUNNER, TH + "Runner.test_pilot_requirements",
     (('    if ((record.get("clock_across_trial") or {}).get("wall_clock") or {}).get("ok") is not True:\n',
       '    if False:\n'),)),
    ("5 a record below the current schema admitted", RUNNER,
     TR + "RealScorerAdmission.test_a_record_below_the_current_schema_is_never_admitted",
     (('        if type(record.get("schema_version")) is not int or record["schema_version"] != TRIAL_RECORD_SCHEMA:\n'
       '            # item 5',
       '        if False:\n            # item 5'),)),
    # ---- item 6: the declared host profile ----
    ("6 an undeclared host profile runs a trial", HARNESS,
     TH + "HostProfiles.test_an_undeclared_profile_is_refused_before_anything_runs",
     (("    if host_profile_refusal():                 # C.0", "    if False:                 # C.0"),)),
    ("6 the record does not declare its profile", HARNESS,
     TH + "HostProfiles.test_every_record_carries_the_declared_profile_and_names_the_host",
     (('              "host_profile": {"declared": HOST_PROFILE, "variable": "STOPCHECK_HOST_PROFILE"},\n', ''),)),
    ("6 the wsl host check takes the linux-vm branch", HARNESS, TH + "HostProfiles.test_wsl_host_checks_are_unchanged",
     (('    if HOST_PROFILE == "linux-vm":\n        paths = ', '    if True:\n        paths = '),)),
    ("6 a linux-vm disk below the floor accepted", HARNESS,
     TH + "HostProfiles.test_linux_vm_disk_floor_is_measured_where_the_trial_writes",
     (('        disk_ok = all(v is not None and v >= floor for v in paths.values())\n', '        disk_ok = True\n'),)),
    ("6 linux-vm accepted on a WSL kernel", HARNESS,
     TH + "HostProfiles.test_linux_vm_declared_on_a_wsl_or_unreadable_kernel_fails",
     (('        if wsl is not False:\n', '        if False:\n'),)),
    ("6 a failing host check does not refuse the trial", HARNESS,
     TH + "HostProfiles.test_a_failing_host_check_refuses_the_trial_before_any_launch",
     (('            if not R["host_checks_start"]["ok"]:\n', '            if False:\n'),)),
    ("6 the linux-vm outbound check ignored", HARNESS,
     TH + "HostProfiles.test_linux_vm_isolation_needs_the_metadata_address_unreachable",
     (('            rec["ok"] = rec["ok"] and rec["outbound"].get("ok") is True\n', '            pass\n'),)),
    ("6 any failed outbound connection taken as isolation", HARNESS,
     TH + "HostProfiles.test_the_outbound_check_requires_network_unreachable",
     (('        return {"ok": e.errno == errno.ENETUNREACH, "address": list(address),',
       '        return {"ok": True, "address": list(address),'),)),
    ("6 the worker identity not copied into the record", HARNESS,
     TH + "HostProfiles.test_the_record_carries_the_worker_identity_the_runner_read_for_this_trial",
     (('    return dict(identity, copied_from=os.path.basename(batch_path))\n',
       '    return dict(missing, why="not copied")\n'),)),
    ("6 a screen freeze not checked against the declared profile", HARNESS,
     TH + "HostProfiles.test_a_screen_trial_checks_the_freeze_against_its_declared_host_profile",
     (('    return runner.verify_freeze(manifest, [cond], run_checks=False, host_profile=HOST_PROFILE)\n',
       '    return runner.verify_freeze(manifest, [cond], run_checks=False)\n'),)),
    ("6 the runner accepts an undeclared profile", RUNNER,
     TR + "WorkerIdentity.test_the_declared_host_profile_is_read_from_the_environment_and_never_inferred",
     (('    if profile not in HOST_PROFILES:\n        raise ValueError(f"STOPCHECK_HOST_PROFILE',
       '    if False:\n        raise ValueError(f"STOPCHECK_HOST_PROFILE'),)),
    ("6 a metadata reply accepted without Metadata-Flavor: Google", RUNNER,
     TR + "WorkerIdentity.test_the_metadata_read_sends_its_header_bypasses_proxies_and_needs_a_google_reply",
     (('        if reply.headers.get("Metadata-Flavor") != "Google":\n', '        if False:\n'),)),
    ("6 the batch entry lacks the worker identity", RUNNER,
     TR + "HostProfileBatches.test_batch_records_its_host_profile_and_each_trials_worker_identity",
     (('                 "host_identity": worker_identity(batch["host_profile"])}', '                 }'),)),
    ("6 a record from another host profile admitted", RUNNER,
     TR + "RealScorerAdmission.test_a_record_from_another_host_profile_is_never_admitted",
     (('        if host_profile is not None and recorded_host_profile(record) != host_profile:\n            # item 6',
       '        if False:\n            # item 6'),)),
    ("6 a resume on another host profile accepted", RUNNER,
     TR + "HostProfileBatches.test_resume_refuses_another_host_profile_and_acceptance_uses_the_batchs",
     (('            if batch.get("host_profile") != profile:\n', '            if False:\n'),)),
    ("6 a current record without its host profile accepted", SCORER,
     TA + "HostProfileInTheRecord.test_a_current_record_declares_a_known_profile",
     (('        checks["host profile declared"] = isinstance(profile, dict) and profile.get("declared") in HOST_PROFILES\n',
       '        checks["host profile declared"] = True\n'),)),
    ("6 a current record without its passing host check accepted", SCORER,
     TA + "HostProfileInTheRecord.test_a_current_record_needs_its_passing_start_host_check",
     (('        checks["host checks at start"] = isinstance(R.get("host_checks_start"), dict) and \\\n'
       '            R["host_checks_start"].get("ok") is True\n',
       '        checks["host checks at start"] = True\n'),)),
    # ---- item 7: the freeze gate and the export of every attempt ----
    ("7 the freeze accepts the no-stop reference and PC alone", RUNNER,
     TR + "FreezeGate.test_the_freeze_requires_every_pilot_of_the_set",
     (('    if found != set(PILOT_SET):\n', '    if not {"C0-HB", "PC"} <= found:\n'),)),
    ("7 only PC's pilot scored, as in draft 4", RUNNER,
     TR + "FreezeGate.test_the_full_pilot_set_passes_and_each_receipt_binds_its_record",
     (('                rows = scorer.score_trial(scorer.load(str(folder)))\n            except',
       '                rows = scorer.score_trial(scorer.load(str(folder))) if condition == "PC" else '
       '[{"measure": "validity", "kind": "valid"}, {"measure": "pending_on_closed", "kind": "yes"}]\n            except'),)),
    ("7 a pilot the scorer calls invalid backs a freeze", RUNNER,
     TR + "FreezeGate.test_a_pilot_the_scorer_calls_invalid_cannot_validate",
     (('            if validity.get("kind") != "valid":\n                problems.append(f"the scorer calls',
       '            if False:\n                problems.append(f"the scorer calls'),)),
    ("7 a pilot's host profile not compared with the freeze's", RUNNER,
     TR + "FreezeGate.test_a_pilot_from_another_host_profile_cannot_validate",
     (('        if recorded_host_profile(record) != host_profile:\n            problems.append(f"the run',
       '        if False:\n            problems.append(f"the run'),)),
    ("7 the freeze does not record its host profile", RUNNER,
     TR + "FreezeGate.test_freeze_records_the_host_profile_predictions_and_sources",
     (('"order": list(conditions), "host_profile": profile,\n', '"order": list(conditions),\n'),)),
    ("7 a freeze runs on another host profile", RUNNER, TR + "FreezeGate.test_a_freeze_runs_only_on_the_host_profile_it_binds",
     (('    if here != frozen:\n', '    if False:\n'),)),
    ("7 an uncommitted freeze or prediction file accepted", RUNNER,
     TR + "FreezeGate.test_verify_freeze_needs_the_freeze_and_the_predictions_committed",
     (('    require_committed(path, "Freeze manifest")\n    require_committed(prediction_path, "Prediction file")\n',
       ''),)),
    ("7 a git-ignored freeze file accepted as committed", RUNNER,
     TR + "FreezeFilesCommitted.test_an_ignored_uncommitted_or_changed_file_is_refused",
     (('    if ignored.returncode == 0:\n        raise ValueError(f"{what} is in a git-ignored path',
       '    if False:\n        raise ValueError(f"{what} is in a git-ignored path'),)),
    ("7 the clock guard's tests left out of the freeze", RUNNER,
     TH + "Runner.test_freeze_binds_the_reused_files_and_both_test_files",
     ((', "common/test_clockguard.py")', ')'),)),
    ("7 the offline checks skip the mutation check", RUNNER,
     TR + "FreezeGate.test_offline_checks_run_every_c_test_module_and_the_mutation_check",
     ((',\n                [sys.executable, "-B", str(HERE / "mutate_c.py")])', ')'),)),
    ("7 only accepted attempts exported", RUNNER, TR + "Exports.test_every_attempt_is_scored_and_exported_outside_its_raw_folder",
     (('        entry["export"] = export_attempt(entry, manifest_path.parent)\n        write_batch(manifest_path, batch)\n'
       '        if interrupted:',
       '        if entry["accepted"]:\n            entry["export"] = export_attempt(entry, manifest_path.parent)\n'
       '        write_batch(manifest_path, batch)\n        if interrupted:'),)),
    ("7 derived reports written into the raw folder by the runner", RUNNER,
     TR + "Exports.test_every_attempt_is_scored_and_exported_outside_its_raw_folder",
     (('    derived = Path(batch_folder) / DERIVED / trial\n',
       '    derived = next(iter(raw_folders(trial)), Path(batch_folder) / DERIVED / trial)\n'),)),
    ("7 a resume leaves an attempt unexported", RUNNER, TR + "Exports.test_resume_exports_an_attempt_that_ended_before_its_export",
     (('    export_pending(batch, manifest_path)\n    for condition in batch["conditions"]:',
       '    for condition in batch["conditions"]:'),)),
    ("7 the scorer writes into a raw folder", SCORER,
     TA + "ReportsOutsideRawFolders.test_a_destination_inside_a_raw_folder_is_refused",
     (('        if os.path.isfile(os.path.join(probe, "record.json")):\n            raise SystemExit',
       '        if False:\n            raise SystemExit'),)),
    ("7 the scorer replaces reports without --overwrite", SCORER,
     TA + "ReportsOutsideRawFolders.test_out_writes_there_and_refuses_to_overwrite_without_the_flag",
     (('        if existing and not args.overwrite:\n', '        if False:\n'),)),
    # ==== pre-freeze review, 14 Oct (R: a major finding; r: a minor one) ====
    # ---- R1: C4 stage 1 from what workflow code did, not from the server's event order ----
    ("R1 a close before the final effect not taken as reading (a)", SCORER,
     TA + "C4StageOneFromWorkflowCode.test_a_close_before_the_final_effect_shows_reading_a_even_when_the_activity_runs_on",
     (('    if acks or closed_before_final or marker_cancelled:\n', '    if acks or marker_cancelled:\n'),)),
    ("R1 a cancelled marker not taken as reading (a)", SCORER,
     TA + "C4StageOneFromWorkflowCode.test_a_cancelled_marker_shows_reading_a",
     (('    if acks or closed_before_final or marker_cancelled:\n', '    if acks or closed_before_final:\n'),)),
    # ---- R2: predictions fixed before each validation run, or the run disclosed ----
    ("R2 a run neither preceded by the predictions nor disclosed backs a freeze", RUNNER,
     TR + "FreezeGate.test_each_validation_run_has_its_predictions_fixed_before_it_or_disclosed",
     (('        if binding is None:\n            problems.append("the predictions were not fixed',
       '        if False:\n            problems.append("the predictions were not fixed'),)),
    ("R2 a trial does not hash the predictions it starts under", HARNESS,
     TH + "PredictionsAtStart.test_the_receipt_hashes_the_file_or_records_it_missing",
     (('        rec.update(sha256=sha256_file(path), exists=True)\n', '        rec.update(sha256=None, exists=True)\n'),)),
    ("R2 the batch does not tell each trial where the predictions are", RUNNER,
     TR + "Admission.test_each_trial_is_told_where_the_predictions_are",
     (('                   STOPCHECK_C_PREDICTIONS=str(Path(args.predictions).resolve()))\n',
       '                   STOPCHECK_C_PREDICTIONS_UNUSED="")\n'),)),
    # ---- R3: the typed command and Temporal's reply ----
    ("R3 the request drops the typed command and the reply", HARNESS,
     TH + "TypedCommandAndReply.test_the_request_keeps_the_typed_command_and_the_reply",
     (('        a.update(cli_reply(res))\n', ''),)),
    ("R3 the CLI call keeps no full argv", HARNESS,
     TH + "TypedCommandAndReply.test_the_request_keeps_the_typed_command_and_the_reply",
     (('    out["argv_full"] = list(argv)\n', ''),)),
    ("R3 the clean-up terminate drops its command and reply", HARNESS,
     TH + "TypedCommandAndReply.test_the_cleanup_terminate_keeps_its_command_and_reply",
     (('                entry.update(cli_reply(res))\n', ''),)),
    # ---- R5: the verified stop needs a valid trial and the history's evidence ----
    ("R5 the verified stop ignores trial validity", SCORER,
     TA + "VerifiedStopNeedsValidityAndHistory.test_an_invalid_trial_is_never_stopped",
     (('    if validity["kind"] != "valid":\n        lacking.append(', '    if False:\n        lacking.append('),)),
    ("R5 the history's started work attempts not cross-checked", SCORER,
     TA + "VerifiedStopNeedsValidityAndHistory.test_a_started_work_attempt_without_an_execution_is_unresolved",
     (('    history_reasons = history_work_evidence(T, execs)\n', '    history_reasons = []\n'),)),
    ("R5 no execution needs no history evidence that the work never ran", SCORER,
     TA + "VerifiedStopNeedsValidityAndHistory.test_with_no_execution_a_scheduled_work_activity_needs_its_closing_event",
     (('    if not execs:\n        types = [e["type"] for e in h]\n', '    if False:\n        types = []\n'),)),
    # ---- R6: natural completion is its own kind ----
    ("R6 natural completion reported as stopped", SCORER, TA + "CompletionIsNotAStop.test_c3_ends_by_completion",
     (('    kind = ENDED_BY_COMPLETION if completed else "stopped"\n', '    kind = "stopped"\n'),)),
    # ---- R4: delivery and what follows it are reported apart ----
    ("R4 a step in flight at the acknowledgement counted as our code continuing", SCORER,
     TA + "DeliveryAndWhatFollows.test_a_step_in_flight_at_the_acknowledgement_is_not_continuing",
     (('    after = sorted(e.get("step") or 0 for e in x["effects"] if _num(e.get("smono")) and e["smono"] > a)\n',
       '    after = sorted(e.get("step") or 0 for e in x["effects"] if e["mono"] > a)\n'),)),
    # ---- R7: one StopCheck trial per host ----
    ("R7 the wrapper takes no host lock", HARNESS,
     TH + "OneTrialPerHost.test_the_wrapper_holds_the_locks_around_the_namespace",
     (('    handles, lock_record = acquire_host_locks()\n', '    handles, lock_record = [], {"locks": []}\n'),)),
    ("R7 a host lock held by another process counts as the wrapper's", HARNESS,
     TH + "OneTrialPerHost.test_the_exclusion_needs_every_host_lock_held_by_the_wrapper",
     (('    return {"ok": all(x["held_by_wrapper"] for x in locks), "wrapper_pid"',
       '    return {"ok": True, "wrapper_pid"'),)),
    ("R7 another StopCheck process does not refuse the trial", HARNESS,
     TH + "OneTrialPerHost.test_any_other_stopcheck_trial_or_service_refuses",
     (('    rec["ok"] = bool(rec["lock"].get("ok") and not rec["processes"] and rec["namespaces"].get("ok")\n',
       '    rec["ok"] = bool(rec["lock"].get("ok") and rec["namespaces"].get("ok")\n'),)),
    ("R7 B's and A's programs not recognised", HARNESS,
     TH + "OneTrialPerHost.test_stopcheck_programs_are_recognised_from_command_lines",
     (('    hits = sorted({os.path.basename(a) for a in args} & set(STOPCHECK_PROGRAMS))\n', '    hits = []\n'),)),
    ("R7 a shared host does not refuse the trial", HARNESS,
     TH + "OneTrialPerHost.test_the_trial_refuses_before_any_launch_when_the_host_is_shared",
     (('            if not R["host_exclusion"]["ok"]:\n', '            if False:\n'),)),
    ("R7 a current record without the host exclusion check accepted", SCORER,
     TA + "HostExclusionInTheRecord.test_a_current_record_needs_the_host_exclusion_check",
     (('        checks["one StopCheck trial on the host"] = isinstance(R.get("host_exclusion"), dict) and \\\n'
       '            R["host_exclusion"].get("ok") is True\n',
       '        checks["one StopCheck trial on the host"] = True\n'),)),
    # ---- minor findings ----
    ("r the freeze ignores the screen seed", RUNNER, TR + "FreezeGate.test_the_freeze_binds_the_screen_seed_and_order",
     (('    if seed is not None and (manifest.get("seed") is None or manifest.get("seed") != seed):\n', '    if False:\n'),)),
    ("r the freeze ignores the screen order", RUNNER, TR + "FreezeGate.test_the_freeze_binds_the_screen_seed_and_order",
     (('    if order is not None and manifest.get("order") != list(order):\n', '    if False:\n'),)),
    ("r the manifest records no seed or order", RUNNER, TR + "FreezeGate.test_the_freeze_binds_the_screen_seed_and_order",
     (('                "seed": seed, "order": list(conditions), "host_profile": profile,\n',
       '                "host_profile": profile,\n'),)),
    ("r a screen trial not checked against the frozen seed and order", RUNNER,
     TR + "Admission.test_a_screen_checks_its_seed_and_order_against_the_freeze",
     (('            verify_freeze(args.freeze, [condition], run_checks=False, seed=batch.get("seed"),\n'
       '                          order=batch["conditions"])\n',
       '            verify_freeze(args.freeze, [condition], run_checks=False)\n'),)),
    ("r effects during a server outage counted toward resumed", SCORER,
     TA + "ServerRestartWindow.test_effects_during_the_outage_are_neither_resumed_nor_re_run",
     (('count_from=rs.get("t_ready") if rs.get("what") == "server" else None))', 'count_from=None))'),)),
    ("r a clock-order violation invalidates the trial again", SCORER,
     TA + "ValidityMatchesC9.test_a_clock_order_violation_alone_keeps_the_trial_valid",
     (('        "no worker-side effect failure in the watch": not worker_faults(T, None, T.watch_end),\n',
       '        "no worker-side effect failure in the watch": not worker_faults(T, None, T.watch_end),\n'
       '        "clock order": T.clock_order_ok(),\n'),)),
    ("r one failed tick post invalidates the trial again", SCORER,
     TA + "ValidityMatchesC9.test_one_failed_tick_post_without_a_gap_keeps_the_trial_valid",
     (('        "no worker-side effect failure in the watch": not worker_faults(T, None, T.watch_end),\n',
       '        "no worker-side effect failure in the watch": not worker_faults(T, None, T.watch_end),\n'
       '        "tick posts all answered": ticks.get("failed", 1) == 0,\n'),)),
    ("r a restarted process with the old identity accepted", HARNESS,
     TH + "RestartIdentity.test_the_old_identity_fails_the_launch",
     (('            if same:\n                raise RuntimeError(f"restarted {name}',
       '            if False:\n                raise RuntimeError(f"restarted {name}'),)),
    ("r a pilot whose restarts kept old identities backs a freeze", RUNNER,
     TR + "FreezeGate.test_restarts_need_new_identities",
     (('    out += restart_identity_problems(record)\n', ''),)),
    ("r no C2 or C3 pending-activity example needed", RUNNER,
     TR + "FreezeGate.test_a_closed_runs_pending_list_must_be_captured_by_c2_or_c3",
     (('    if not any(k in ("yes", "no") for k in pending):\n', '    if False:\n'),)),
    ("r the delivery bound judged off the planned heartbeat phase", SCORER,
     TA + "HeartbeatPhasePrecondition.test_off_the_planned_phase_the_bound_reads_phase_off_plan",
     (('        elif ph["state"] != "pass":\n', '        elif False:\n'),)),
    ("r the phase precondition ignores the grid", SCORER,
     TA + "HeartbeatPhasePrecondition.test_off_the_planned_phase_the_bound_reads_phase_off_plan",
     (('    ok = k >= 0 and abs(err) <= PHASE_TOL_S and abs(last - planned_last) <= PHASE_TOL_S\n', '    ok = True\n'),)),
    ("r the in-flight step's commit not looked up at the receiver", SCORER,
     TA + "StepInFlightAtTheCancellation.test_each_phase",
     (('        elif any(e.get("step") == inflight.get("step") and e.get("kind") == inflight.get("kind") for e in '
       'x["effects"]):\n', '        elif False:\n'),)),
    ("r a POST's reply does not clear the effect in flight", JOBS,
     TH + "Workload.test_post_effect_marks_the_send_and_the_reply_and_keeps_an_interrupted_post_in_flight",
     (('        progress.replied(time.monotonic())\n', '        pass\n'),)),
    ("r a worker-side effect failure in the watch ignored", SCORER,
     TA + "WorkerSideFaults.test_an_effect_post_failure_in_the_watch_invalidates",
     (('        "no worker-side effect failure in the watch": not worker_faults(T, None, T.watch_end),\n',
       '        "no worker-side effect failure in the watch": True,\n'),)),
    ("r a worker-side failure during the probe ignored", SCORER,
     TA + "WorkerSideFaults.test_after_the_watch_it_leaves_the_probe_unresolved_not_the_trial_invalid",
     (('            controls = controls and not probe_faults\n', ''),)),
    ("r the scheduled reads do not record our observer's load", HARNESS,
     TH + "ObserverLoad.test_scheduled_reads_and_the_pre_check_record_it",
     (('        r["host_load"] = host_load()          # our observer', '        r["no_load"] = host_load()          # our'
       ' observer'),)),
    ("r late steps not flagged", SCORER, TA + "StepLateness.test_a_late_step_is_flagged",
     (('                    "late_steps": sorted(n for n, v in late.items() if v > LATE_STEP_S)})\n',
       '                    "late_steps": []})\n'),)),
    ("r a procedure without a FAST smoke backs a freeze", RUNNER,
     TR + "FreezeGate.test_every_procedure_needs_a_fast_smoke_on_the_freezes_source",
     (('    if smokes != set(SMOKE_SET):\n', '    if False:\n'),)),
    ("r a smoke whose procedure failed backs a freeze", RUNNER,
     TR + "FreezeGate.test_every_procedure_needs_a_fast_smoke_on_the_freezes_source",
     (('            problems = smoke_problems(record, expected_hashes)\n', '            problems = []\n'),)),
)
# corrections that live in a whole helper: (label, target, focused test, function name, stub)
STUB_MUTANTS = (
    ("7 the scorer's reports go beside the record again, as before the batch", SCORER,
     TA + "ReportsOutsideRawFolders.test_the_default_destination_is_outside_the_raw_folder", "report_folder",
     "def report_folder(folder, out=None):\n    return os.path.abspath(out) if out else os.path.abspath(folder)\n"),
    # pre-freeze review R1: draft 5's stage 1, decided by the order of CancelRequested and the marker in the history
    ("R1 C4 stage 1 decided by the server's event order again", SCORER,
     TA + "C4StageOneFromWorkflowCode.test_cancel_written_before_the_marker_with_the_final_effect_and_no_acknowledgement_"
          "is_not_reading_a", "c4_stage1",
     "def c4_stage1(T):\n"
     "    lr, h = T.last_scheduled_with(history_of, T.watch_end + 5 if _num(T.watch_end) else None)\n"
     "    if h is None:\n"
     "        return UNK, {}\n"
     "    cr = next((e['id'] for e in h if e['type'] == 'WorkflowExecutionCancelRequested'), None)\n"
     "    mk = next((e['id'] for e in h if e['type'] == 'MarkerRecorded'), None)\n"
     "    if cr is None:\n"
     "        return 'cancel never reached the history', {}\n"
     "    if mk is None or cr < mk:\n"
     "        return '(a) before the local activity finished', {}\n"
     "    return '(b) after the local activity finished', {}\n"),
)
# the wall/monotonic rule itself (clockwall_c.py, as n8n/mutate_b.py's clock-rule mutant)
CLOCKWALL_MUTANTS = (
    ("5 an offset excursion hidden by adjacent and whole-span checks only", CLOCKWALL,
     TC + "WallClockRule.test_small_repeated_forward_then_backward_steps_cannot_hide_in_whole_span",
     (('        for i in (low, high):\n', '        for i in (j - 1,):\n'),
      ('    whole = interval(0, len(rows) - 1)\n    out.update(',
       '    whole = interval(0, len(rows) - 1)\n'
       '    if whole["excess_s"] > worst["excess_s"]:\n'
       '        worst = whole\n'
       '    out.update('))),
)


def function_source(text, name):
    tree = ast.parse(text)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = text.splitlines(keepends=True)
    return node.lineno - 1, node.end_lineno


def stub_function(candidate, name, stub):
    start, end = function_source(candidate, name)
    lines = candidate.splitlines(keepends=True)
    return "".join(lines[:start]) + stub + "\n" + "".join(lines[end:])


def patch_source(candidate, label, edits):
    text = candidate
    for old, new in edits:
        if text.count(old) != 1:
            raise RuntimeError(f"patch mutant {label!r}: fragment found {text.count(old)} times, expected once")
        text = text.replace(old, new)
    return text


def run_fixture(path, name, variable):
    # A fresh interpreter avoids reuse of the normal scorer, harness or runner imported by other tests.
    code = ("import io,json,unittest; "
            "suite=unittest.defaultTestLoader.loadTestsFromName(" + repr(name) + "); "
            "r=unittest.TextTestRunner(stream=io.StringIO()).run(suite); "
            "print(json.dumps({'tests':r.testsRun,'failures':len(r.failures),'errors':len(r.errors),"
            "'skipped':len(r.skipped)}))")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env[variable] = str(path)
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def check_mutant(folder, index, text, test, target):
    """Returns (result, verdict): verdict is "killed", "survived" or "not run" (the fixture is skipped here)."""
    clean_path, variable = target
    clean = run_fixture(clean_path, test, variable)
    if clean.get("skipped"):
        return clean, "not run"
    if clean != {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}:
        raise RuntimeError(f"Clean {clean_path.name} fails prerequisite fixture: {test}: {clean}")
    path = Path(folder) / f"mutant_{index:02d}.py"
    path.write_text(text, encoding="utf-8")
    result = run_fixture(path, test, variable)
    # A fixture with subTest may report several assertions for the same defect. Every runtime/import error still
    # refuses the mutant; only one executed fixture with at least one assertion kills it.
    killed = result["tests"] == 1 and result["failures"] >= 1 and result["errors"] == 0
    return result, "killed" if killed else "survived"


def plan():
    sources = {target: target[0].read_text(encoding="utf-8") for target in (SCORER, HARNESS, RUNNER, CLOCKWALL, JOBS)}
    out = []
    for label, target, test, edits in PATCH_MUTANTS + CLOCKWALL_MUTANTS:
        relocate = (RELOCATE[target],) if RELOCATE[target] else ()
        out.append((label, patch_source(sources[target], label, relocate + tuple(edits)), test, target))
    for label, target, test, name, stub in STUB_MUTANTS:
        relocate = (RELOCATE[target],) if RELOCATE[target] else ()
        out.append((label, patch_source(stub_function(sources[target], name, stub), label, relocate), test, target))
    return out


def main(argv=None):
    mutants = plan()
    verdicts = []
    with tempfile.TemporaryDirectory(prefix="stopcheck_c_mutants_") as folder:
        for index, (label, text, test, target) in enumerate(mutants):
            result, verdict = check_mutant(folder, index, text, test, target)
            print(f"{verdict.upper()} {label}: {result}")
            verdicts.append(verdict)
    run = [v for v in verdicts if v != "not run"]
    killed = run.count("killed")
    note = f"; {len(verdicts) - len(run)} NOT RUN (fixtures skipped on this platform)" if len(run) < len(verdicts) else ""
    print(f"Mutation check: {killed}/{len(run)} faults detected by assertions{note}")
    return 0 if killed == len(run) and run else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, StopIteration) as error:
        print(f"Mutation check refused: {error}", file=sys.stderr)
        sys.exit(2)
