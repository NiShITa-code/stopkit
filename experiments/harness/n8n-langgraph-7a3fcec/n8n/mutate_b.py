#!/usr/bin/env python3
"""Check that focused regression fixtures detect individual legacy scorer faults.

Each mutant replaces one corrected function with its archived development-checkpoint
implementation. Assertion failures kill a mutant; import/runtime errors do not count.
Where the corrected function's signature changed (m2_state gained a `read` argument), the
legacy body is kept under a private name behind a thin adapter with the current signature,
so the legacy LOGIC runs and a behavioural fault fails an assertion instead of a TypeError.
Corrections added after the checkpoint have no legacy body: a stub mutant replaces a whole
helper with one that disables the correction, and a patch mutant reverts exact source
fragments inside a larger function (each fragment must occur exactly once, so a mutant can
never silently leave the code unchanged).
Runner corrections are patch mutants of run_expB.py, run against tests/test_run_expB.py
(STOPCHECK_RUN_EXPB), together with the scorer mutants.
Harness corrections are patch mutants of harness_b.py, run against tests/test_harness_b.py
(STOPCHECK_HARNESS_B); the canary's patch mutants (canary_b.py, STOPCHECK_CANARY_B) run against the same test
module. harness_b.py imports Unix-only modules, so on Windows those mutants are
reported as NOT RUN and only the scorer, runner and clock-rule mutants are checked; `--harness-only`
runs just the mutants that need Linux (no git checkpoint needed): the harness, canary and planting
mutants, and the runner's batch mutants, whose tests take POSIX file locks.
The wall/monotonic rule that the harness applies and the scorer recomputes lives in clockwall_b.py;
its patch mutants run against tests/test_clockwall_b.py (STOPCHECK_CLOCKWALL_B) on every platform.
The reproduction script's patch mutants (reproduce_b.py, STOPCHECK_REPRODUCE_B) run against
tests/test_run_expB.py on every platform; the planting's (workflows_b.py, STOPCHECK_WORKFLOWS_B) run
against tests/test_harness_b.py with the harness mutants, under Linux.
The scorer, like the harness, imports clockwall_b from its own folder, so every scorer and harness
mutant keeps HERE pointing at n8n/ (RELOCATE).
Nothing launches services or changes saved experiment records.
"""
import ast
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LEGACY_COMMIT = "3832d7c"
TESTS = "tests.test_analyze_b."
MUTANTS = (
    ("m3_rows", "tests.test_analyze_b.ScorerFaults.test_m3_deadline_needs_follow_up"),
    ("five_events", "tests.test_analyze_b.ScorerFaults.test_effects_closed_is_unknown_when_states_are_unknown"),
    ("restart_rows", "tests.test_analyze_b.ScorerFaults.test_restart_separates_continued_from_resumed"),
    ("m2_state", "tests.test_analyze_b.Draft4Rules.test_cred_store_is_scored"),
    ("fac_rows", "tests.test_analyze_b.EvidenceBoundaries.test_missing_status_with_running_work_is_not_a_false_all_clear"),
)
# corrections added after the checkpoint (no legacy body exists): the mutant disables the correction
STUB_MUTANTS = (
    # 13 Oct evidence repairs: these helpers separate missing evidence from negative observations.
    ("_clock_evidence_ok", TESTS + "EvidenceRepairs20261013.test_current_schema_requires_every_clock_guard",
     "def _clock_evidence_ok(R):\n    return True\n"),
    ("_isolation_evidence_ok", TESTS + "EvidenceRepairs20261013.test_current_schema_requires_recorded_passing_isolation",
     "def _isolation_evidence_ok(R):\n    return True\n"),
    ("_compute_end_evidence", TESTS + "EvidenceRepairs20261013.test_quiet_computation_with_canceled_records_is_unresolved",
     "def _compute_end_evidence(T, carrier, read, upto):\n    return 'quiet canary treated as ended'\n"),
    ("_possible_natural_end", TESTS + "EvidenceRepairs20261013.test_possible_natural_endpoint_is_ambiguity_not_a_computation_end",
     "def _possible_natural_end(T, carrier, by):\n    return False\n"),
    ("_worker_holder_alive", "tests.test_analyze_b.StaleRecords.test_wait_short_row_left_running_by_a_killed_worker_is_not_armed",
     "def _worker_holder_alive(T, read):\n    return None\n"),
    # F17: validity ignores the harness controls / the unit-properties guard
    ("_control_checks", TESTS + "Draft5Scorer.test_removed_cron_line_that_still_fires_invalidates",
     "def _control_checks(T):\n    return {}\n"),
    ("_unit_property_violations", TESTS + "Draft5Scorer.test_unit_property_mismatch_voids_the_trial",
     "def _unit_property_violations(R):\n    return []\n"),
    # F20: the backlog has no natural endpoint
    ("_backlog_natural_end", TESTS + "Draft5Scorer.test_finite_backlog_completed_after_the_stop_cannot_test_60s",
     "def _backlog_natural_end(T, upto):\n    return None\n"),
    # F22: the 12 Oct state-changed rule, comparing only the agent's status
    ("_state_changes", TESTS + "Draft5Scorer.test_unpublish_is_a_state_change_while_the_agent_still_runs",
     "def _state_changes(T):\n"
     "    base = [T.agent_status_at(s)[0] for s in T.reads if _read_time(s) is not None and _read_time(s) < T.t_stop]\n"
     "    base = [x for x in base if x is not None]\n"
     "    if not base:\n"
     "        return None\n"
     "    return [x for x in (T.agent_status_at(s)[0] for s in T.watch) if x is not None and x != base[-1]]\n"),
    # F25: psql text timestamps passed to fromisoformat unchanged
    ("_pg_iso", TESTS + "Draft5Scorer.test_psql_timestamp_is_normalised",
     "def _pg_iso(value):\n    return value\n"),
    # F26: no recovery outcome is computed
    ("_recovery_outcome", TESTS + "Draft5Scorer.test_recovery_outcomes",
     "def _recovery_outcome(T, c, reads):\n    return 'unresolved', 'not computed', {}\n"),
)
# corrections inside a larger function: (label, test, ((fragment, replacement), ...))
PATCH_MUTANTS = (
    ("O13 effects completion erases accepted-effect history",
     TESTS + "EvidenceRepairs20261013.test_resolved_obligation_does_not_erase_an_earlier_accepted_effect",
     (('effects_closed = None if T.void else (False if accepted_effects else\n'
       '        (obligations_closed if T.log_ok["outside"] else None))',
       'effects_closed = None if T.void else obligations_closed'),)),
    ("O13 absent identified wait row ignored",
     TESTS + "EvidenceRepairs20261013.test_missing_one_of_two_identified_wait_rows_leaves_wait_unresolved",
     (('                missing.append(eid)\n', '                pass\n'),)),
    ("O13 M1 negative ignores missing action evidence",
     TESTS + "EvidenceRepairs20261013.test_m1_no_action_needs_both_action_logs",
     (('kind = "violated" if acts else (UNK if coverage else "not violated")',
       'kind = "violated" if acts else "not violated"'),)),
    ("O13 M1 negative ignores stalled observer controls",
     TESTS + "EvidenceRepairs20261013.test_m1_no_action_needs_working_observer_not_just_present_files",
     (('controls = _control_checks(T)\n    failures =',
       'controls = {name: PASSED for name in ("untouched_twin", "outside_twin", "end_read_key_and_health")}\n    failures ='),)),
    ("O13 record-gone negative ignores action coverage",
     TESTS + "EvidenceRepairs20261013.test_record_gone_negative_needs_log_coverage",
     (('coverage = _m1_negative_coverage(T) if not acts else None\n    if coverage:',
       'coverage = None\n    if coverage:'),)),
    ("O13 main PID disappearance ignores internal runner membership",
     TESTS + "EvidenceRepairs20261013.test_inactive_main_pid_alone_cannot_exclude_a_live_runner",
     (('and _worker_holder_alive(T, read) is False and empty_group:',
       'and _worker_holder_alive(T, read) is False:'),)),
    ("O13 unrelated empty cgroup settles worker computation",
     TESTS + "EvidenceRepairs20261013.test_inactive_main_pid_alone_cannot_exclude_a_live_runner",
     (('membership.get("control_group") == expected_group', 'True'),)),
    ("O13 censored restart reported as successful resumption",
     TESTS + "EvidenceRepairs20261013.test_lifetime_censored_probe_does_not_report_successful_resumption",
     (('        elif rp.get("censored") is True:', '        elif False:'),)),
    ("F17 unimplemented control counted as passed", TESTS + "Draft5Scorer.test_unimplemented_controls_are_reported_and_never_passed",
     (('"controls_ok": T.void is None and all(v == PASSED for v in controls.values())}',
       '"controls_ok": T.void is None and all(v in (PASSED, NOT_IMPLEMENTED) for v in controls.values())}'),)),
    ("F17 removed cron line firing ignored", TESTS + "Draft5Scorer.test_removed_cron_line_that_still_fires_invalidates",
     (('late = T.pings_of("cron_kill", after=removed + CRON_REMOVAL_ALLOWANCE_S, upto=T.t_end)', 'late = []'),)),
    ("F17 kept cron line silence ignored", TESTS + "Draft5Scorer.test_kept_cron_line_silent_through_the_watch_invalidates",
     (('elif T.pings_of("cron_twin", after=T.t_stop, upto=T.t_end):', 'elif True:'),)),
    ("F17 killed control harness count ignored", TESTS + "Draft5Scorer.test_failing_untouched_killed_or_key_control_invalidates",
     (('PASSED if not late and kc.get("pings_after_kill") == 0 else', 'PASSED if not late else'),)),
    ("F17 killed control never seen alive", TESTS + "Draft5Scorer.test_killed_control_never_seen_alive_invalidates",
     (('elif alive < KILL_MIN:', 'elif False:'),)),
    ("F17 killed control late ping in the log ignored",
     TESTS + "Draft5Scorer.test_killed_control_ping_after_the_allowance_in_the_log_invalidates",
     (('late = T.pings_of("killed_control", after=sent + KILL_ALLOWANCE_S)', 'late = []'),)),
    ("F17 untouched twin gap ignored", TESTS + "Draft5Scorer.test_untouched_twin_gap_in_the_watch_invalidates",
     (('elif gap is None or gap > TWIN_GAP:', 'elif False:'),)),
    ("F17 untouched twin gap at the watch ends ignored",
     TESTS + "Draft5Scorer.test_untouched_twin_silent_at_the_start_or_end_of_the_watch_invalidates",
     (('seq = [before[-1] if before else a] + [t for t in ts if a < t <= b] + [b]',
       'seq = [t for t in ts if a < t <= b]'),)),
    ("F17 untouched twin path ignored", TESTS + "Draft5Scorer.test_untouched_twin_path_must_be_recorded_as_tcp",
     (('if ut.get("path") != "tcp":', 'if False:'),)),
    ("F17 untouched twin harness summary ignored", TESTS + "Draft5Scorer.test_failing_untouched_killed_or_key_control_invalidates",
     (('elif not summary_ok:', 'elif False:'),)),
    ("F17 hidden drive and private /tmp not checked", TESTS + "Draft5Scorer.test_unit_property_mismatch_voids_the_trial",
     (('"User": "n8ntest",\n                   "TemporaryFileSystem": "/mnt", "PrivateTmp": "yes"}', '"User": "n8ntest"}'),)),
    ("F17 spool bind not checked", TESTS + "Draft5Scorer.test_unit_property_mismatch_voids_the_trial",
     (('        if not spool:\n', '        if False:\n'),)),
    ("F17 units binding different spools accepted", TESTS + "Draft5Scorer.test_unit_property_mismatch_voids_the_trial",
     (('    if len(spools) > 1:\n', '    if False:\n'),)),
    ("F17 end-read key and health not checked", TESTS + "Draft5Scorer.test_end_read_key_or_health_failure_invalidates",
     (('good = end.get("twin_key_valid") is True and health.get("issuer_tcp") == 200 and \\\n'
       '            health.get("outside_sock") == 200', 'good = True'),)),
    ("F18 running status for ended work not false", TESTS + "Draft5Scorer.test_running_status_for_ended_work_is_false",
     (('contradicts = (st in ENDED and state == "running") or (st in ACTIVE and state == "ended")',
       'contradicts = st in ENDED and state == "running"'),)),
    ("F19 stale Bull active job counted as armed", TESTS + "StaleRecords.test_bull_active_job_of_a_killed_worker_is_not_armed",
     (('if queue == "active" and row_status.get(eid) == "running" and eid in started_before_stop:', 'if False:'),)),
    ("F19 job started in a restarted worker treated as stale", TESTS + "StaleRecords.test_bull_active_job_started_in_a_restarted_worker_is_not_ended",
     (('if queue == "active" and row_status.get(eid) == "running" and eid in started_before_stop:',
       'if queue == "active" and row_status.get(eid) == "running" and stale:'),)),
    ("F19 deactivating worker counted as gone", TESTS + "StaleRecords.test_deactivating_worker_with_the_same_pid_still_holds_its_work",
     (('return True if state in HOLDER_STATES else None', 'return state == "active"'),)),
    ("F19 pre-stop read without a PID counted as a different process",
     TESTS + "StaleRecords.test_pre_stop_read_without_a_worker_pid_cannot_show_a_different_process",
     (('    if base_pid in ("", "0"):\n        return None\n', ''),)),
    ("F21 carrier silent before the stop credited", TESTS + "Draft5Scorer.test_carrier_silent_before_the_stop_is_not_credited",
     (('elif c in ACTIVE_AT_STOP and not any(T.t_stop - QUIET_TAIL <= t <= T.t_stop for t in sig):', 'elif False:'),)),
    ("F23 schedule without pre-stop firing tested", TESTS + "Draft5Scorer.test_fast_schedule_and_cron_without_pre_stop_firing_are_untested",
     (('if carrier in ("sched", "oscron") and self.t_stop is not None and self.log_ok.get("pings"):', 'if False:'),)),
    ("F23 missing ping log read as no pre-stop firing",
     TESTS + "Draft5Scorer.test_missing_ping_log_leaves_schedule_and_cron_unknown_not_untested",
     (('if carrier in ("sched", "oscron") and self.t_stop is not None and self.log_ok.get("pings"):',
       'if carrier in ("sched", "oscron") and self.t_stop is not None:'),)),
    ("F23 firing due at the watch end counted", TESTS + "Draft5Scorer.test_cron_due_too_close_to_the_watch_end_is_not_met",
     (('elif c in ("sched", "oscron") and due + QUIET_TAIL > T.t_end:', 'elif False:'),)),
    ("F23 schedule placed on :00", TESTS + "Draft5Scorer.test_schedule_due_time_uses_its_observed_second",
     (('    if carrier == "sched":\n        # the phase comes from', '    if False:\n        # the phase comes from'),
      ('    if carrier == "oscron":\n        paired', '    if carrier in ("sched", "oscron"):\n        paired'))),
    ("F24 failed API read falls back to the database", TESTS + "Draft5Scorer.test_failed_agent_read_while_api_is_up_is_not_a_database_claim",
     (('            return None, None, "read_failed"\n        # B.4: only API_UNAVAILABLE', '        # B.4: only API_UNAVAILABLE'),)),
    ("F34 harness-flagged API read failure resolved from the database",
     TESTS + "Draft5Scorer.test_api_read_failure_is_not_resolved_from_the_database_row",
     (('        if read.get("api_read_failed"):\n            return None, None, "read_failed"\n', ''),)),
    ("F24 404 not kept as record absence", TESTS + "Draft5Scorer.test_actions_after_a_404_are_acted_after_record_gone",
     (('            if es.get("http") == 404:\n                return None, es.get("reply_mono", _read_time(read)), "absent_404"\n', ''),)),
    ("F27 --out ignored", TESTS + "Draft5Scorer.test_out_writes_reports_to_a_separate_directory",
     (('dest = args.out or folder', 'dest = folder'),)),
    ("F27 --out overwrites without --overwrite", TESTS + "Draft5Scorer.test_out_refuses_to_overwrite_without_flag",
     (('if existing and not args.overwrite:', 'if False:'),)),
    ("contract wait_short armed ping unused", TESTS + "Draft5Scorer.test_wait_short_due_time_comes_from_its_armed_ping",
     (('    if carrier == "wait_short":\n        # its in-memory wait', '    if False:\n        # its in-memory wait'),)),
    # 12 Oct pre-pilot review (P1, P3, P4, P8)
    ("P1 key control passes while the key still validates", TESTS + "PrePilotCorrections.test_control_key_passes_only_when_refused_as_revoked",
     (('refused = key.get("valid_at_end") is False and key.get("why_at_end") == "key revoked"',
       'refused = key.get("valid_at_end") is True'),)),
    ("P1 key control refusal reason ignored", TESTS + "PrePilotCorrections.test_control_key_passes_only_when_refused_as_revoked",
     (('refused = key.get("valid_at_end") is False and key.get("why_at_end") == "key revoked"',
       'refused = key.get("valid_at_end") is False'),)),
    ("P4 untouched twin gap in the restart probe ignored",
     TESTS + "PrePilotCorrections.test_untouched_twin_gap_in_the_restart_probe_leaves_restart_outcomes_unknown",
     (('    if probe_gap is None or probe_gap > TWIN_GAP:\n', '    if False:\n'),)),
    ("P4 restart outcomes kept after a probe-control failure",
     TESTS + "PrePilotCorrections.test_untouched_twin_gap_in_the_restart_probe_leaves_restart_outcomes_unknown",
     (('        elif probe_ctl:             # R6.9', '        elif False:             # R6.9'),)),
    ("P4 probe all-clear kept after a probe-control failure",
     TESTS + "PrePilotCorrections.test_untouched_twin_gap_in_the_restart_probe_leaves_restart_outcomes_unknown",
     (('                if is_probe and probe_ctl:', '                if False:'),)),
    ("P4 probe-control failure invalidates the trial (not R6.9)",
     TESTS + "PrePilotCorrections.test_untouched_twin_gap_in_the_restart_probe_leaves_restart_outcomes_unknown",
     (('    probe_controls = {k: controls.pop(k) for k in PROBE_CONTROLS if k in controls}\n',
       '    probe_controls = {k: controls[k] for k in PROBE_CONTROLS if k in controls}\n'),)),
    ("P4 probe without reads checked as a window", TESTS + "PrePilotCorrections.test_twin_probe_check_is_not_applicable_without_probe_reads",
     (('    if not rp.get("reads"):\n        return PROBE_NOT_APPLICABLE\n', ''),)),
    ("P3 later-discovered execution kept in an earlier O2",
     TESTS + "PrePilotCorrections.test_execution_discovered_after_a_read_does_not_make_its_o2_unknown",
     (('states = [T.status_at(s, e)[0] for e in T.declared_at(s)]', 'states = [T.status_at(s, e)[0] for e in T.declared]'),)),
    ("P3 later-discovered execution kept in the list-omission rows",
     TESTS + "PrePilotCorrections.test_execution_discovered_after_a_read_does_not_make_its_o2_unknown",
     (('        known = T.declared_at(T.end_read)\n', '        known = T.declared\n'),)),
    ("P3 execution without a discovery stamp dropped",
     TESTS + "PrePilotCorrections.test_execution_known_at_a_read_or_without_a_stamp_still_needs_its_status",
     (('if not isinstance(mono, (int, float)) or t is None or not pg_read or mono <= t:',
       'if isinstance(mono, (int, float)) and (t is None or not pg_read or mono <= t):'),)),
    # 12 Oct check (items 2 and 3)
    ("item 3 read with a failed Postgres read excludes a later-discovered execution",
     TESTS + "PrePilotCorrections.test_read_whose_postgres_read_failed_cannot_exclude_a_later_discovered_execution",
     (('if not isinstance(mono, (int, float)) or t is None or not pg_read or mono <= t:',
       'if not isinstance(mono, (int, float)) or t is None or mono <= t:'),)),
    ("item 2a outside service's log not checked across the watch",
     TESTS + "PrePilotCorrections.test_outside_twin_log_gap_in_the_watch_invalidates",
     (('        out["outside_twin"] = _outside_twin_gap(T, T.t_stop, T.t_end, "watch")\n',
       '        out["outside_twin"] = PASSED\n'),)),
    ("item 2a outside service's log gap ignored", TESTS + "PrePilotCorrections.test_outside_twin_log_gap_in_the_watch_invalidates",
     (('    if gap is None or gap > OUT_TWIN_GAP:\n', '    if False:\n'),)),
    ("item 2a outside service's log not checked across the restart probe",
     TESTS + "PrePilotCorrections.test_outside_twin_log_gap_in_the_restart_probe_leaves_restart_outcomes_unknown",
     (('    out["outside_twin_probe"] = _outside_twin_probe_check(T, ot)\n', '    out["outside_twin_probe"] = PASSED\n'),)),
    ("item 2 new probe clauses counted in trial validity (not R6.9)",
     TESTS + "PrePilotCorrections.test_outside_twin_log_gap_in_the_restart_probe_leaves_restart_outcomes_unknown",
     (('PROBE_CONTROLS = ("untouched_twin_probe", "outside_twin_probe", "cron_twin_probe")',
       'PROBE_CONTROLS = ("untouched_twin_probe",)'),)),
    ("item 2b kept cron line not checked across the restart probe",
     TESTS + "PrePilotCorrections.test_kept_cron_line_silent_in_the_restart_probe_leaves_restart_outcomes_unknown",
     (('    out["cron_twin_probe"] = _cron_twin_probe_check(T, ctl.get("cron_twin"))\n',
       '    out["cron_twin_probe"] = PASSED\n'),)),
    ("item 2b unplaceable minute boundary in the probe passed",
     TESTS + "PrePilotCorrections.test_kept_cron_line_silent_in_the_restart_probe_leaves_restart_outcomes_unknown",
     (('    return PASSED if due is False else (\n        "failed: no firing in the restart probe',
       '    return PASSED if not due else (\n        "failed: no firing in the restart probe'),)),
    ("item 2b firing required for a boundary in the probe's last 5 s",
     TESTS + "PrePilotCorrections.test_kept_cron_line_needs_no_probe_firing_without_a_boundary_before_the_probes_last_5_s",
     (('due = _minute_boundary_in(T, ps, pe - QUIET_TAIL)', 'due = _minute_boundary_in(T, ps, pe)'),)),
    ("item 2c removed cron line allowed 3 s", TESTS + "PrePilotCorrections.test_removed_cron_line_allowance_is_two_seconds",
     (('OUT_TWIN_GAP, CRON_REMOVAL_ALLOWANCE_S = 5.0, 2.0', 'OUT_TWIN_GAP, CRON_REMOVAL_ALLOWANCE_S = 5.0, 3.0'),)),
    ("item 2c removed cron line allowed no launch delay",
     TESTS + "PrePilotCorrections.test_removed_cron_line_allowance_is_two_seconds",
     (('OUT_TWIN_GAP, CRON_REMOVAL_ALLOWANCE_S = 5.0, 2.0', 'OUT_TWIN_GAP, CRON_REMOVAL_ALLOWANCE_S = 5.0, 0.0'),)),
    ("P8 screen label without a verified batch entry", TESTS + "PrePilotCorrections.test_screen_label_needs_a_verified_batch_entry",
     (('screen = "exploratory screen" if batch.get("verified") is True else', 'screen = "exploratory screen" if True else'),)),
)
# The draft5-fixes line's 13 Oct review corrections (O13-2 to O13-6), with every fragment rewritten against the merged
# scorer, then the merge's own corrections (merge of 13 Oct, steps 2-4). One patch mutant per correction, each
# killed by an assertion of its focused test.
O13 = TESTS + "ReviewO13."
PATCH_MUTANTS = PATCH_MUTANTS + (
    ("O13-3 an effect after the stop erased by the job's later resolution",
     O13 + "test_effect_after_the_stop_is_not_closed_even_if_the_job_later_resolved",
     (('effects_closed = None if T.void else (False if accepted_effects else\n'
       '        (obligations_closed if T.log_ok["outside"] else None))',
       'effects_closed = None if T.void else (obligations_closed if T.log_ok["outside"] else None)'),)),
    ("O13-4 missing clock evidence accepted for a current-schema record",
     O13 + "test_current_schema_record_missing_clock_evidence_is_void",
     (('            if not _clock_evidence_ok(R):\n                return "clock guard failed or mandatory clock evidence missing"',
       '            if False:\n                return "clock guard failed or mandatory clock evidence missing"'),)),
    ("O13-4 missing or failed isolation check accepted",
     O13 + "test_current_schema_record_without_a_passing_isolation_check_is_void",
     (('        if _current_schema(R) and not _isolation_evidence_ok(R):', '        if False:'),)),
    ("O13-5 an absent identified wait execution skipped", O13 + "test_partial_read_of_identified_waits_is_unknown",
     (('        if missing:\n            return UNK, "previously identified wait',
       '        if False:\n            return UNK, "previously identified wait'),)),
    ("O13-6 not violated without the activity logs", O13 + "test_m1_without_the_ping_log_is_unknown_not_clean",
     (('    coverage = _m1_negative_coverage(T) if not acts else None\n    kind = "violated"',
       '    coverage = None\n    kind = "violated"'),)),
    ("O13-2 a natural end before the probe read as a restart outcome",
     O13 + "test_carrier_reaching_its_natural_end_before_the_probe_ended_naturally",
     (("        elif _natural_end_time(T, c, r_end) is not None:\n            # O13-2",
       "        elif False:\n            # O13-2"),)),
    # merge step 2: the natural-end branch sits after the censoring check; the legacy clock policy (schema < 5)
    ("merge natural end ahead of the censoring check", O13 + "test_censored_probe_stays_unknown_even_after_a_natural_end",
     (('        elif rp.get("censored") is True:\n',
       '        elif not signals and _natural_end_time(T, c, r_end) is not None:\n'
       '            kind = "ended naturally"\n'
       '        elif rp.get("censored") is True:\n'),)),
    ("merge legacy record held to the current clock policy",
     O13 + "test_legacy_record_without_clock_evidence_keeps_the_legacy_policy",
     (('        if _current_schema(R):\n            if not _clock_evidence_ok(R):',
       '        if True:\n            if not _clock_evidence_ok(R):'),
      ('"clock": _clock_evidence_ok(R) if _current_schema(R) else _legacy_clock_ok(R),', '"clock": _clock_evidence_ok(R),'))),
    ("merge failing legacy clock guard accepted", O13 + "test_legacy_record_with_a_failing_clock_guard_is_void",
     (('        elif not _legacy_clock_ok(R):\n', '        elif False:\n'),)),
    # merge step 3 (option 1): M2 for quiet loops stays activity-based (R6.3); the computation boundary is separate
    ("merge quiet-loop M2 made to depend on the computation boundary",
     TESTS + "EvidenceRepairs20261013.test_quiet_computation_with_canceled_records_is_unresolved",
     (('        return "ended", "no activity observed in the last 5 s (bounded canary observation)"',
       '        if computation_boundary(T, carrier, er)[0] != "closed":\n'
       '            return UNK, "independent computation-end evidence unavailable"\n'
       '        return "ended", "no activity observed in the last 5 s (bounded canary observation)"'),)),
    ("merge unknown execution status ignored for a quiet loop",
     TESTS + "EvidenceRepairs20261013.test_quiet_loop_whose_execution_reads_unknown_is_unknown",
     (('        if any(T.status_at(er, x)[0] == "unknown" or (T.row_at(er, x) or {}).get("status") == "unknown" for x in ids):\n'
       '            return UNK, "execution reads unknown without positive activity evidence"',
       '        if False:\n'
       '            return UNK, "execution reads unknown without positive activity evidence"'),)),
    ("merge computation boundary dropped from the M2 rows",
     TESTS + "EvidenceRepairs20261013.test_quiet_computation_with_canceled_records_is_unresolved",
     (('        if carrier in LOOP_CARRIERS:\n            evidence["computation_boundary"]',
       '        if False:\n            evidence["computation_boundary"]'),)),
    ("merge computation boundary dropped from the five events",
     TESTS + "EvidenceRepairs20261013.test_work_ceased_needs_the_closed_computation_boundary_for_quiet_loops",
     (('"computation_boundary": {c: boundary[c][0] for c in LOOP_CARRIERS},', '"computation_boundary": {},'),)),
    ("merge computation boundary dropped from the summary",
     TESTS + "EvidenceRepairs20261013.test_computation_boundary_is_reported_per_read_and_in_the_summary",
     (("            line += f\"; computation boundary: {r['evidence'].get('computation_boundary', '?')}\"",
       "            pass"),)),
    # merge step 4: only a complete read by the /proc reader closes the boundary
    ("merge pre-fix /sys cgroup read trusted as complete",
     TESTS + "EvidenceRepairs20261013.test_pre_fix_sys_cgroup_read_never_closes_the_boundary",
     (('    empty_group = isinstance(membership, dict) and membership.get("method") == CGROUP_READER and \\\n',
       '    empty_group = isinstance(membership, dict) and \\\n'),)),
    # merge step 7: a recorded failing host check (disk floor, system services) voids the trial in the scorer too
    ("merge recorded failing host check ignored by the scorer",
     TESTS + "HostChecksInTheRecord.test_a_recorded_failing_host_check_voids_the_trial",
     (('        if _host_checks_ok(R) is False:\n', '        if False:\n'),)),
)
# schema 7 (fix of 13 Oct): records before the merge already said schema 6, so the evidence the merged harness writes
# is mandatory only from schema 7. One patch mutant per correction
PATCH_MUTANTS = PATCH_MUTANTS + (
    ("schema 7 record missing a host check accepted",
     TESTS + "HostChecksInTheRecord.test_schema_7_record_missing_a_host_check_is_void",
     (('    if _evidence_schema(R):\n        return all(isinstance(R.get(key), dict)',
       '    if False:\n        return all(isinstance(R.get(key), dict)'),)),
    ("schema 7 cgroup read without the reader tag kept as recorded",
     TESTS + "EvidenceRepairs20261013.test_schema_7_cgroup_read_without_the_proc_reader_tag_is_a_failed_read",
     (('    if _evidence_schema(T.R):\n        why = f"no {CGROUP_READER} reader tag',
       '    if False:\n        why = f"no {CGROUP_READER} reader tag'),)),
    # the scorer's recomputation of the wall/monotonic check from pings.jsonl (clockwall_b, the harness's rule)
    ("schema 7 failed or impossible wall/monotonic recomputation accepted",
     TESTS + "WallClockRecomputation.test_schema_7_record_whose_recomputation_fails_is_void",
     (('        if _evidence_schema(R) and self.wall_check.get("ok") is not True:\n', '        if False:\n'),)),
    ("record before schema 7 voided on its post hoc wall/monotonic recomputation",
     TESTS + "WallClockRecomputation.test_record_before_schema_7_reports_the_recomputation_only_as_a_post_hoc_diagnostic",
     (('        if _evidence_schema(R) and self.wall_check.get("ok") is not True:\n',
       '        if self.wall_check.get("ok") is not True:\n'),)),
)
# pre-freeze batch of 13 Oct: mandatory guard receipts (item 6), and quiet versus stopped (item 4: work_ceased needs the
# closed computation boundary for the in-worker loops; the verified_stop operator verdict). One patch mutant per change
PATCH_MUTANTS = PATCH_MUTANTS + (
    ("13 Oct schema 7 record with unusable guard receipts accepted",
     TESTS + "GuardReceipts.test_schema_7_record_without_a_usable_guard_reading_is_void",
     (('        if _evidence_schema(R):         # schema 7: an empty violation list needs usable readings behind it\n',
       '        if False:         # schema 7: an empty violation list needs usable readings behind it\n'),)),
    ("13 Oct missing OOM-kill reading accepted (guards.py skips that comparison)",
     TESTS + "GuardReceipts.test_schema_7_record_without_a_usable_guard_reading_is_void",
     (('GUARD_READINGS = (("pid1_start_ticks", 0), ("oom_kill", 0), ("netns_inode", 1))',
       'GUARD_READINGS = (("pid1_start_ticks", 0), ("netns_inode", 1))'),)),
    ("13 Oct work ceased credits a quiet loop without its closed computation boundary",
     TESTS + "EvidenceRepairs20261013.test_work_ceased_needs_the_closed_computation_boundary_for_quiet_loops",
     (('    ceased = ["unresolved" if c in LOOP_CARRIERS and m2[c] == "ended" and boundary[c][0] != "closed" else m2[c]\n'
       '              for c in WORK_CARRIERS]\n', '    ceased = [m2[c] for c in WORK_CARRIERS]\n'),)),
    ("13 Oct verified stop credits in-worker work on M2 ended alone",
     TESTS + "VerifiedStop.test_quiet_carriers_and_no_false_all_clear_are_not_a_verified_stop",
     (('            return closed == "closed", f"computation boundary', '            return True, f"computation boundary'),
      ('        return bool(holder), holder or (', '        return True, holder or ('))),
    ("13 Oct verified stop calls active carriers unresolved",
     TESTS + "VerifiedStop.test_any_running_armed_or_authorised_carrier_is_not_stopped",
     (('    if active:\n        kind, verdict = "not stopped"', '    if False:\n        kind, verdict = "not stopped"'),)),
    ("13 Oct verified stop credits the copied key's use without refusal or a closed loop",
     TESTS + "VerifiedStop.test_copied_key_use_needs_refusal_at_the_issuer_or_the_agent_loops_closed_boundary",
     (('        return False, "neither the copied key refused', '        return True, "neither the copied key refused'),)),
    ("13 Oct verified stop left out of the summary",
     TESTS + "VerifiedStop.test_void_trial_is_unresolved_and_the_summary_reports_the_verdict",
     (('    vs = find("verified_stop", "all", "verified stop")\n    L.append(',
       '    vs = find("verified_stop", "all", "verified stop")\n    False and L.append('),)),
)
# harness corrections, as patch mutants of harness_b.py run against tests/test_harness_b.py. harness_b imports its
# siblings from its own folder, so each mutant keeps HERE pointing at n8n/ (RELOCATE) while it lives in a temp dir
HARNESS_TESTS = "tests.test_harness_b.DraftFiveFixTests."
HARNESS_PATCH_MUTANTS = (
    ("O13 wall-clock failure omitted from trial clock guard",
     "tests.test_harness_b.WallClockDiscontinuities.test_actual_wall_step_fails_even_when_mono_raw_rate_guard_passes",
     (('    if not wall["ok"]:\n', '    if False:\n'),)),
    ("C1 recovery interval not read from main's environment",
     HARNESS_TESTS + "test_queue_recovery_interval_comes_from_mains_runtime_environment",
     (('"queue_recovery_interval": recovery if recovery is not None else "180 (default)",',
       '"queue_recovery_interval": "180 (default)",'),)),
    ("P7 every declared key compared with the config and the workload",
     HARNESS_TESTS + "test_run_trial_compares_b0_settings_not_b1rs_workload",
     (('R["configuration_mismatches"] = prediction_mismatches(expected, R["config"], keys)',
       'R["configuration_mismatches"] = prediction_mismatches(expected, {**R["config"], **R["workload"]}, list(expected))'),)),
    ("P7 a setting missing on both sides counted as agreement",
     HARNESS_TESTS + "test_frozen_settings_are_compared_only_on_the_b0_keys_against_the_config",
     (('        if want is None or have is None or want != have:\n', '        if want != have:\n'),)),
    # 12 Oct check, item 4
    ("item 4 B.0 settings recorded as the harness's intended values",
     HARNESS_TESTS + "test_b0_settings_come_from_the_units_runtime_environment",
     (('"EXECUTIONS_MODE": effective["EXECUTIONS_MODE"], "N8N_RUNNERS_MODE": effective["N8N_RUNNERS_MODE"],',
       '"EXECUTIONS_MODE": "queue", "N8N_RUNNERS_MODE": "internal",'),)),
    ("item 4 shutdown timeout absent from the process recorded as configured",
     HARNESS_TESTS + "test_b0_settings_come_from_the_units_runtime_environment",
     (('f"{shutdown} (configured)" if shutdown is not None else None', 'f"{shutdown} (configured)"'),)),
    ("item 4 worker running with a different B.0 setting accepted",
     HARNESS_TESTS + "test_b0_setting_that_differs_between_main_and_worker_is_refused",
     (('    if differ:\n', '    if False:\n'),)),
)
# the draft5-fixes line's O13-1 harness correction, rewritten against the merged harness (its O13-2 mutants targeted
# workload_lifetime, PROBE_PREP_BUDGET_S and budget(), which the merged harness replaced; they are dropped)
HARNESS_PATCH_MUTANTS = HARNESS_PATCH_MUTANTS + (
    ("O13-1 task timeout varying with the condition's PRE",
     "tests.test_harness_b.TimingAndNaturalEndpoints.test_task_timeout_is_one_frozen_value_for_every_condition",
     (('"task_timeout_s": TASK_TIMEOUT_S, "runner_concurrency": 16,',
       '"task_timeout_s": TASK_TIMEOUT_S - PRE + pre, "runner_concurrency": 16,'),)),
)
# merge of 13 Oct, step 4: the unit-process reader scans /proc (the /sys cgroup hierarchy is not visible in the netns)
CGROUP_TESTS = "tests.test_harness_b.CgroupReaderInTheTrialNamespace."
HARNESS_PATCH_MUTANTS = HARNESS_PATCH_MUTANTS + (
    ("merge active unit with no process found read as an empty group",
     CGROUP_TESTS + "test_active_unit_whose_group_shows_no_process_is_a_failed_read",
     (('        if not members and state not in STOPPED_UNIT_STATES:\n', '        if False:\n'),)),
    ("merge nested runner groups left out of the unit's members",
     CGROUP_TESTS + "test_members_include_nested_runner_groups_but_not_a_sibling_prefix",
     (('            elif path == group or path.startswith(group + "/"):\n', '            elif path == group:\n'),)),
    ("merge unit state change during the scan ignored",
     CGROUP_TESTS + "test_unit_state_change_during_the_scan_is_a_failed_read",
     (('        if (state, main_pid) != (after.get("ActiveState"), str(after.get("MainPID") or "0")):\n',
       '        if False:\n'),)),
    ("merge group not taken from systemd",
     CGROUP_TESTS + "test_active_unit_with_its_processes_in_proc_is_populated",
     (('        live = shown.stdout.strip() if shown.returncode == 0 else ""\n', '        live = ""\n'),)),
    ("merge snapshot reads a unit without its recorded group",
     HARNESS_TESTS + "test_snapshot_reads_each_units_processes_by_unit_name_and_recorded_group",
     (('        role: unit_processes(unit, ((ctx["R"].get("units") or {}).get(unit) or {}).get("ControlGroup"))\n',
       '        role: unit_processes(unit)\n'),)),
)
# merge of 13 Oct, step 7: the disk floor and the system services, checked at trial start (refuse) and end (void)
HOST_TESTS = "tests.test_harness_b.HostPreconditions."
HARNESS_PATCH_MUTANTS = HARNESS_PATCH_MUTANTS + (
    ("merge failing start host checks do not refuse the trial",
     HOST_TESTS + "test_trial_is_refused_before_any_setup_when_the_start_checks_fail",
     (('    if not R["host_checks_start"]["ok"]:\n', '    if False:\n'),)),
    ("merge failing end host checks do not void the trial",
     HOST_TESTS + "test_end_checks_are_recorded_and_a_failure_voids_the_trial",
     (('        if not R["host_checks_end"]["ok"]:\n', '        if False:\n'),)),
    ("merge disk below the floor accepted",
     HOST_TESTS + "test_disk_below_the_floor_or_unreadable_fails",
     (('        if not disk_ok:\n            report["violations"].append(f"disk floor: {free} GB free on C:',
       '        if False:\n            report["violations"].append(f"disk floor: {free} GB free on C:'),)),
    ("merge enabled or active system service accepted",
     HOST_TESTS + "test_an_enabled_active_or_unknown_system_service_fails",
     (('        state["ok"] = state["is_enabled"] in SERVICE_NOT_ENABLED and state["is_active"] == "inactive"\n',
       '        state["ok"] = True\n'),)),
    # schema 7 (fix of 13 Oct): the harness writes the runner's current record schema
    ("schema 7 harness writes the pre-merge record schema",
     "tests.test_harness_b.RecordSchema.test_trial_record_is_schema_7_the_runners_current_record_schema",
     (('"schema_version": run_expB.TRIAL_RECORD_SCHEMA,', '"schema_version": 6,'),)),
)
# cloud host, 13 Oct: the declared host profile (STOPCHECK_HOST_PROFILE). wsl keeps the laptop's checks; linux-vm
# measures the disk floor on the results folder and /var/lib and checks a /mnt sentinel instead of /mnt/c/Windows
PROFILE_TESTS = "tests.test_harness_b.HostProfiles."
HARNESS_PATCH_MUTANTS = HARNESS_PATCH_MUTANTS + (
    ("profile undeclared host profile accepted",
     PROFILE_TESTS + "test_an_undeclared_profile_is_refused_before_anything_runs",
     (('    if HOST_PROFILE not in HOST_PROFILES:\n', '    if False:\n'),)),
    ("profile not recorded",
     PROFILE_TESTS + "test_every_record_carries_the_profile",
     (('         "host_profile": {"declared": HOST_PROFILE, "variable": "STOPCHECK_HOST_PROFILE"},\n', ''),)),
    ("profile linux-vm records still say WSL",
     PROFILE_TESTS + "test_the_scope_names_the_declared_host",
     (('{SCOPE[HOST_PROFILE]}', '{SCOPE["wsl"]}'),)),
    ("profile wsl host checks take the linux-vm branch",
     PROFILE_TESTS + "test_wsl_host_checks_are_unchanged",
     (('    if HOST_PROFILE == "linux-vm":\n        floor = ', '    if True:\n        floor = '),)),
    ("profile linux-vm disk below the floor accepted",
     PROFILE_TESTS + "test_linux_vm_below_the_floor_or_unreadable_fails",
     (('        disk_ok = all(v is not None and v >= floor for v in paths.values())\n', '        disk_ok = True\n'),)),
    ("profile linux-vm accepted on a WSL kernel",
     PROFILE_TESTS + "test_linux_vm_declared_on_a_wsl_or_unreadable_kernel_fails",
     (('        if wsl is not False:\n', '        if False:\n'),)),
    ("profile unit that sees the sentinel accepted",
     PROFILE_TESTS + "test_linux_vm_unit_that_sees_the_sentinel_fails",
     (('            hidden = os.path.exists(f"/proc/{info[\'pid\']}/root{sentinel}")\n', '            hidden = False\n'),)),
    ("profile sentinel invisible on the host accepted",
     PROFILE_TESTS + "test_linux_vm_sentinel_the_host_cannot_see_fails",
     (('            report["violations"].append(f"host sentinel not visible on the host: {sentinel}")\n',
       '            pass\n'),)),
    ("profile canary that sees the sentinel accepted",
     PROFILE_TESTS + "test_linux_vm_canary_that_sees_the_sentinel_or_does_not_say_fails",
     (('(vm and str(sender.get("mnts")) != "False")', 'False'),)),
    ("profile sentinel never placed",
     PROFILE_TESTS + "test_linux_vm_places_a_fresh_sentinel_and_wsl_places_none",
     (('    path = host_sentinel(trial)\n    with open(path, "x"', '    return\n    path = host_sentinel(trial)\n    with open(path, "x"'),)),
    ("profile existing sentinel overwritten",
     PROFILE_TESTS + "test_an_existing_sentinel_is_refused_and_left_alone",
     (('    with open(path, "x", encoding="utf-8") as f:\n        f.write(f"stopcheck host sentinel',
       '    with open(path, "w", encoding="utf-8") as f:\n        f.write(f"stopcheck host sentinel'),)),
    ("profile sentinel left behind at the end",
     PROFILE_TESTS + "test_finalize_removes_the_placed_sentinel",
     (('        attempt("host sentinel", remove_sentinel)\n', '        pass\n'),)),
)
# pre-freeze batch of 13 Oct: B1r's later planned stop and FAST's refusal of B1r (item 1); the screen bound to the
# freeze's host profile and the worker identity copied into the record (item 3)
FIX_TESTS = "tests.test_harness_b.DraftFiveFixTests."
HARNESS_PATCH_MUTANTS = HARNESS_PATCH_MUTANTS + (
    ("13 Oct FAST timing runs B1r", FIX_TESTS + "test_fast_timing_refuses_b1r_like_b9c",
     (('    if cond == "B1r" and FAST:\n', '    if False:\n'),)),
    ("13 Oct a refused condition reaches setup", FIX_TESTS + "test_fast_timing_refuses_b1r_like_b9c",
     (('    if condition_refusal(cond):              # B.10: before anything is read or set up (B9c, and B1r in FAST)\n'
       '        raise ValueError(condition_refusal(cond))\n', ''),)),
    ("13 Oct B1r planned stop back at 130 s", FIX_TESTS + "test_b1r_planned_stop_leaves_room_for_the_measured_main_restart",
     (('PRE_B1R = 180                        # full timing only\n', 'PRE_B1R = 130                        # full timing only\n'),)),
    ("13 Oct B1r restart planned with the 12 Oct cost",
     FIX_TESTS + "test_b1r_planned_stop_leaves_room_for_the_measured_main_restart",
     (('B1R_RESTART_NEED_S = B1R_STOP_MEASURED_S + B1R_READY_MEASURED_S + 15\n', 'B1R_RESTART_NEED_S = 31 + 21.4 + 10\n'),)),
    ("13 Oct lifetime cap counts the B1r stop FAST refuses",
     "tests.test_harness_b.TimingAndNaturalEndpoints.test_long_lifetime_covers_maximum_allowed_probe_delay_and_both_reads",
     (('MAX_PRE_S = PRE if FAST else max(PRE, PRE_B1R)\n', 'MAX_PRE_S = max(PRE, PRE_B1R)\n'),)),
    ("13 Oct screen freeze not checked against the declared host profile",
     PROFILE_TESTS + "test_a_screen_trial_checks_the_freeze_against_its_declared_host_profile",
     (('run_expB.verify_freeze(manifest, [cond], run_checks=False, host_profile=HOST_PROFILE)',
       'run_expB.verify_freeze(manifest, [cond], run_checks=False)'),)),
    ("13 Oct worker identity not copied into the record",
     PROFILE_TESTS + "test_the_record_carries_the_worker_identity_the_runner_read_for_this_trial",
     (('    return dict(identity, copied_from=', '    return dict(missing, why="not copied", copied_from='),)),
)
# item 8: planted trigger node ids fit n8n's varchar(36) trigger-status column (workflows_b.py, run against
# tests/test_harness_b.py with STOPCHECK_WORKFLOWS_B; that test module imports the harness, so these run under Linux)
WORKFLOWS_PATCH_MUTANTS = (
    ("13 Oct trigger node id carries the webhook path",
     "tests.test_harness_b.PlantedNodeIds.test_every_planted_node_id_fits_n8ns_trigger_status_column",
     (('    return {"id": "w-" + name, "name": name, "type": "n8n-nodes-base.webhook",',
       '    return {"id": "w-" + path, "name": name, "type": "n8n-nodes-base.webhook",'),)),
)
# the cron/OS canary's own sentinel report (canary_b.py, run against tests/test_harness_b.py with STOPCHECK_CANARY_B;
# that test module imports the harness, so these run under Linux with the harness mutants)
CANARY_PATCH_MUTANTS = (
    ("profile canary never reports the sentinel",
     PROFILE_TESTS + "test_canary_reports_whether_its_trials_sentinel_is_visible",
     (('"mnts": str(trial is not None and os.path.exists(sentinel(trial)))', '"mnts": "False"'),)),
)
# harness_b and analyze_b import their siblings (clockwall_b among them) from their own folder, so every harness and
# scorer mutant keeps HERE pointing at n8n/ while it lives in a temp dir
RELOCATE = ("HERE = os.path.dirname(os.path.abspath(__file__))", f"HERE = {str(HERE)!r}")
# the wall/monotonic rule, moved from harness_b.py into clockwall_b.py so the harness and the scorer share one
# implementation: patch mutants of clockwall_b.py, run against tests/test_clockwall_b.py (STOPCHECK_CLOCKWALL_B). The
# module is pure, so these run on every platform
CLOCKWALL_PATCH_MUTANTS = (
    ("O13 offset excursion hidden by adjacent and whole-span checks only",
     "tests.test_clockwall_b.WallClockRule.test_small_repeated_forward_then_backward_steps_cannot_hide_in_whole_span",
     (('        for i in (low, high):\n', '        for i in (j - 1,):\n'),
      ('    whole = interval(0, len(rows) - 1)\n    out.update(',
       '    whole = interval(0, len(rows) - 1)\n'
       '    if whole["excess_s"] > worst["excess_s"]:\n'
       '        worst = whole\n'
       '    out.update('))),
)
# runner corrections, as patch mutants of run_expB.py run against tests/test_run_expB.py (STOPCHECK_RUN_EXPB). The
# runner loads the scorer from its own folder, so each mutant keeps HERE pointing at n8n/ (RUNNER_RELOCATE)
RUNNER_TESTS = "tests.test_run_expB."
RUNNER_PATCH_MUTANTS = (
    ("O13 absent or downgraded trial schema qualifies freeze pilots",
     RUNNER_TESTS + "PilotValidation.test_matching_sources_cannot_admit_missing_or_downgraded_record_schema",
     (('if type(record.get("schema_version")) is not int or record["schema_version"] != TRIAL_RECORD_SCHEMA:',
       'if False:'),)),
    ("O13 absent or downgraded trial schema qualifies campaign results",
     RUNNER_TESTS + "CurrentTrialSchemaAcceptance.test_pilot_and_screen_reject_removed_or_legacy_schema_before_scoring",
     (('if type(record.get("schema_version")) is not int or record["schema_version"] != TRIAL_RECORD_SCHEMA:',
       'if False:'),)),
    ("item 1 restart-probe control failure not gating the freeze",
     RUNNER_TESTS + "PilotValidation.test_untouched_twin_gap_in_the_restart_probe_blocks_freeze",
     (('probe_failed = scorer._probe_failure(trial) or scorer._probe_control_failure(trial)',
       'probe_failed = scorer._probe_failure(trial)'),)),
    # schema 7 (fix of 13 Oct): a pre-merge schema 6 record no longer qualifies a pilot or a screen
    ("schema 7 runner still admits schema 6 records",
     RUNNER_TESTS + "PilotValidation.test_matching_sources_cannot_admit_missing_or_downgraded_record_schema",
     (("TRIAL_RECORD_SCHEMA = 7\n", "TRIAL_RECORD_SCHEMA = 6\n"),)),
)
# pre-freeze batch of 13 Oct: FAST refuses B1r (item 1); the freeze gate over all four pilots (item 2); the host profile
# and worker identity (item 3); every attempt scored and exported (item 7). One or more patch mutants per change
RUNNER_PATCH_MUTANTS = RUNNER_PATCH_MUTANTS + (
    ("13 Oct FAST pilot default keeps B1r",
     RUNNER_TESTS + "CampaignGates.test_fast_pilot_never_includes_a_condition_fast_timing_refuses",
     (('        default = [c for c in PILOT_CONDITIONS if not (args.fast and c in FAST_REFUSED)] if args.pilot else CONDITIONS\n',
       '        default = list(PILOT_CONDITIONS) if args.pilot else CONDITIONS\n'),)),
    ("13 Oct FAST admits a condition its timing refuses",
     RUNNER_TESTS + "CampaignGates.test_fast_pilot_never_includes_a_condition_fast_timing_refuses",
     (('    if args.fast and set(args.conditions or ()) & set(FAST_REFUSED):\n', '    if False:\n'),)),
    ("13 Oct freeze accepts the B0 and PC pilots alone",
     RUNNER_TESTS + "PilotValidation.test_freeze_requires_all_four_validation_pilots",
     (('    missing = [c for c in PILOT_CONDITIONS if c not in found]\n',
       '    missing = [c for c in ("B0", "PC") if c not in found]\n'),)),
    ("13 Oct pilot host profile not compared with the freeze's",
     RUNNER_TESTS + "PilotValidation.test_a_pilot_from_another_host_profile_cannot_validate_the_freeze",
     (('        if recorded_host_profile(record) != host_profile:\n            raise ValueError(f"{folder}: the pilot',
       '        if False:\n            raise ValueError(f"{folder}: the pilot'),)),
    ("13 Oct B1r pilot accepted without its verified preparatory restart",
     RUNNER_TESTS + "PilotValidation.test_b1r_pilot_needs_its_verified_preparatory_restart_before_the_stop",
     (('        if condition == "B1r" and _b1r_restart_problem(record):\n', '        if False:\n'),)),
    ("13 Oct freeze does not record its host profile",
     RUNNER_TESTS + "CampaignGates.test_freeze_records_the_declared_host_profile_and_validates_pilots_against_it",
     (('                "host_profile": profile,\n', ''),)),
    ("13 Oct freeze runs on another host profile",
     RUNNER_TESTS + "CampaignGates.test_a_freeze_runs_only_on_the_host_profile_it_binds",
     (('    if here != frozen:\n', '    if False:\n'),)),
    ("13 Oct record from another host profile accepted by the batch",
     RUNNER_TESTS + "CurrentTrialSchemaAcceptance.test_a_record_from_another_host_profile_is_never_accepted",
     (('        if host_profile is not None and recorded_host_profile(record) != host_profile:\n', '        if False:\n'),)),
    ("13 Oct metadata reply accepted without Metadata-Flavor: Google",
     RUNNER_TESTS + "WorkerIdentity.test_the_metadata_read_sends_its_header_bypasses_proxies_and_needs_a_google_reply",
     (('        if reply.headers.get("Metadata-Flavor") != "Google":\n', '        if False:\n'),)),
)
# the runner's batch mutants: their tests drive whole batches, which take POSIX file locks (fcntl), so they run with
# the harness mutants under Linux (harness_plan; --harness-only), and are reported NOT RUN on Windows
RUNNER_POSIX_PATCH_MUTANTS = (
    ("13 Oct batch entry lacks the worker identity",
     RUNNER_TESTS + "Batches.test_batch_records_its_host_profile_and_each_trials_worker_identity",
     (('                 "host_identity": worker_identity(batch["host_profile"])}', '                 }'),)),
    ("13 Oct resume on another host profile accepted",
     RUNNER_TESTS + "Batches.test_resume_refuses_another_host_profile_and_acceptance_uses_the_batchs",
     (('            if batch.get("host_profile") != profile:\n', '            if False:\n'),)),
    ("13 Oct export only for accepted attempts",
     RUNNER_TESTS + "Batches.test_every_attempt_is_scored_and_exported_outside_its_raw_folder",
     (('        entry["export"] = export_attempt(entry, manifest_path.parent)\n        write_batch(manifest_path, batch)\n'
       '        if interrupted:',
       '        if entry["accepted"]:\n            entry["export"] = export_attempt(entry, manifest_path.parent)\n'
       '        write_batch(manifest_path, batch)\n        if interrupted:'),)),
    ("13 Oct export manifest omits nested raw files",
     RUNNER_TESTS + "Batches.test_every_attempt_is_scored_and_exported_outside_its_raw_folder",
     (('    for dirpath, dirnames, filenames in os.walk(folder):\n',
       '    for dirpath, dirnames, filenames in list(os.walk(folder))[:1]:\n'),)),
    ("13 Oct derived reports written into the raw folder",
     RUNNER_TESTS + "Batches.test_every_attempt_is_scored_and_exported_outside_its_raw_folder",
     (('    derived = Path(batch_folder) / DERIVED / trial\n',
       '    derived = next(iter(raw_folders(trial)), Path(batch_folder) / DERIVED / trial)\n'),)),
    ("13 Oct resume leaves an attempt unexported",
     RUNNER_TESTS + "Batches.test_resume_exports_an_attempt_that_ended_before_its_export",
     (('    export_pending(batch, manifest_path, env)\n    for condition in batch["conditions"]:',
       '    for condition in batch["conditions"]:'),)),
)
RUNNER_RELOCATE = ("HERE = Path(__file__).resolve().parent", f"HERE = Path({str(HERE)!r})")
# item 5: a replay is source-matched only when every recorded source matches, the scorer's clockwall_b.py included
# (patch mutants of reproduce_b.py, run against tests/test_run_expB.py with STOPCHECK_REPRODUCE_B; it loads the scorer
# from its own folder, so each mutant keeps HERE pointing at n8n/: the runner's relocation)
REPRODUCE_PATCH_MUTANTS = (
    ("13 Oct replay source match checks only analyze_b.py",
     RUNNER_TESTS + "ReproduceScorerIdentity.test_replay_is_source_matched_only_when_every_recorded_source_matches",
     (('    for path, digest in sorted(recorded.items()):\n',
       '    for path, digest in sorted(i for i in recorded.items() if i[0] == "n8n/analyze_b.py"):\n'),
      ('SCORER_SOURCES = ("n8n/analyze_b.py", "n8n/clockwall_b.py")', 'SCORER_SOURCES = ("n8n/analyze_b.py",)'))),
)
# legacy functions whose signature changed: run the legacy body behind the current signature
ADAPTERS = {
    "m2_state": "def m2_state(T, carrier, read=None):\n    return _legacy_m2_state(T, carrier)\n",
}


def function_source(text, name):
    tree = ast.parse(text)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = text.splitlines(keepends=True)
    return node.lineno - 1, node.end_lineno, "".join(lines[node.lineno - 1:node.end_lineno])


def replace_function(candidate, legacy, name):
    start, end, _ = function_source(candidate, name)
    _, _, body = function_source(legacy, name)
    if name in ADAPTERS:
        body = body.replace(f"def {name}(", f"def _legacy_{name}(", 1) + "\n\n" + ADAPTERS[name]
    lines = candidate.splitlines(keepends=True)
    return "".join(lines[:start]) + body + "\n" + "".join(lines[end:])


def stub_function(candidate, name, stub):
    start, end, _ = function_source(candidate, name)
    lines = candidate.splitlines(keepends=True)
    return "".join(lines[:start]) + stub + "\n" + "".join(lines[end:])


def patch_source(candidate, label, edits):
    text = candidate
    for old, new in edits:
        if text.count(old) != 1:
            raise RuntimeError(f"patch mutant {label!r}: fragment found {text.count(old)} times, expected once")
        text = text.replace(old, new)
    return text


SCORER = (HERE / "analyze_b.py", "STOPCHECK_ANALYZE_B")
HARNESS = (HERE / "harness_b.py", "STOPCHECK_HARNESS_B")
RUNNER = (HERE / "run_expB.py", "STOPCHECK_RUN_EXPB")
CLOCKWALL = (HERE / "clockwall_b.py", "STOPCHECK_CLOCKWALL_B")
CANARY = (HERE / "canary_b.py", "STOPCHECK_CANARY_B")
REPRODUCE = (HERE / "reproduce_b.py", "STOPCHECK_REPRODUCE_B")
WORKFLOWS = (HERE / "workflows_b.py", "STOPCHECK_WORKFLOWS_B")


def run_fixture(path, name, variable="STOPCHECK_ANALYZE_B"):
    # A fresh interpreter avoids reuse of the normal scorer or harness imported by other tests.
    code = ("import io,json,unittest; "
            "suite=unittest.defaultTestLoader.loadTestsFromName(" + repr(name) + "); "
            "r=unittest.TextTestRunner(stream=io.StringIO()).run(suite); "
            "print(json.dumps({'tests':r.testsRun,'failures':len(r.failures),'errors':len(r.errors)}))")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env[variable] = str(path)
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    import json
    return json.loads(result.stdout.strip().splitlines()[-1])


def check_mutant(folder, index, text, test, target=SCORER):
    clean_path, variable = target
    clean = run_fixture(clean_path, test, variable)
    if clean != {"tests": 1, "failures": 0, "errors": 0}:
        raise RuntimeError(f"Clean {clean_path.name} fails prerequisite fixture: {test}: {clean}")
    path = Path(folder) / f"mutant_{index:02d}.py"
    path.write_text(text, encoding="utf-8")
    result = run_fixture(path, test, variable)
    # A fixture with subTest may report several assertions for the same defect. Every runtime/import
    # error still refuses the mutant; only one executed fixture with at least one assertion kills it.
    return result, result["tests"] == 1 and result["failures"] >= 1 and result["errors"] == 0


def harness_plan():
    """The harness patch mutants, each also relocated (RELOCATE) so it imports n8n/'s siblings from a temp dir.
    None where harness_b.py cannot be imported: it loads mocks_b, whose Unix-socket server Windows lacks."""
    if os.name == "nt":
        return None
    candidate = (HERE / "harness_b.py").read_text(encoding="utf-8")
    canary = (HERE / "canary_b.py").read_text(encoding="utf-8")
    workflows = (HERE / "workflows_b.py").read_text(encoding="utf-8")
    runner = (HERE / "run_expB.py").read_text(encoding="utf-8")
    return [(f"patched harness {label}", patch_source(candidate, label, (RELOCATE,) + edits), test, HARNESS)
            for label, test, edits in HARNESS_PATCH_MUTANTS] + \
        [(f"patched canary {label}", patch_source(canary, label, edits), test, CANARY)
         for label, test, edits in CANARY_PATCH_MUTANTS] + \
        [(f"patched planting {label}", patch_source(workflows, label, edits), test, WORKFLOWS)
         for label, test, edits in WORKFLOWS_PATCH_MUTANTS] + \
        [(f"patched runner batch {label}", patch_source(runner, label, (RUNNER_RELOCATE,) + edits), test, RUNNER)
         for label, test, edits in RUNNER_POSIX_PATCH_MUTANTS]


def main(argv=None):
    harness_only = "--harness-only" in (sys.argv[1:] if argv is None else argv)
    plan = []
    if not harness_only:
        old = subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", "-C", str(ROOT),
                              "show", f"{LEGACY_COMMIT}:n8n/analyze_b.py"], capture_output=True, text=True)
        if old.returncode:
            raise RuntimeError("Legacy checkpoint unavailable; mutation coverage cannot be verified")
        candidate = (HERE / "analyze_b.py").read_text(encoding="utf-8")
        plan = [(f"legacy {function}", patch_source(replace_function(candidate, old.stdout, function), function,
                                                    (RELOCATE,)), test, SCORER)
                for function, test in MUTANTS]
        plan += [(f"disabled {function}", patch_source(stub_function(candidate, function, stub), function, (RELOCATE,)),
                  test, SCORER)
                 for function, test, stub in STUB_MUTANTS]
        plan += [(f"patched {label}", patch_source(candidate, label, (RELOCATE,) + edits), test, SCORER)
                 for label, test, edits in PATCH_MUTANTS]
        runner = (HERE / "run_expB.py").read_text(encoding="utf-8")
        plan += [(f"patched runner {label}", patch_source(runner, label, (RUNNER_RELOCATE,) + edits), test, RUNNER)
                 for label, test, edits in RUNNER_PATCH_MUTANTS]
        reproduce = (HERE / "reproduce_b.py").read_text(encoding="utf-8")
        plan += [(f"patched reproduction {label}", patch_source(reproduce, label, (RUNNER_RELOCATE,) + edits), test,
                  REPRODUCE) for label, test, edits in REPRODUCE_PATCH_MUTANTS]
        clock = (HERE / "clockwall_b.py").read_text(encoding="utf-8")
        plan += [(f"patched clock rule {label}", patch_source(clock, label, edits), test, CLOCKWALL)
                 for label, test, edits in CLOCKWALL_PATCH_MUTANTS]
    harness = harness_plan()
    if harness is None and harness_only:
        raise RuntimeError("harness_b.py cannot be imported on this platform; run the harness mutants under Linux")
    plan += harness or []
    killed = 0
    with tempfile.TemporaryDirectory(prefix="stopcheck_b_mutants_") as folder:
        for index, (label, text, test, target) in enumerate(plan):
            result, good = check_mutant(folder, index, text, test, target)
            print(f"{'KILLED' if good else 'FAILED'} {label}: {result}")
            killed += good
    unix_only = (len(HARNESS_PATCH_MUTANTS) + len(CANARY_PATCH_MUTANTS) + len(WORKFLOWS_PATCH_MUTANTS) +
                 len(RUNNER_POSIX_PATCH_MUTANTS))
    note = "" if harness is not None else (f"; {unix_only} harness, canary, planting and runner batch mutants NOT RUN "
                                           "(harness_b.py needs Unix sockets and batches POSIX file locks: run this "
                                           "check under Linux)")
    print(f"Mutation check: {killed}/{len(plan)} faults detected by assertions{note}")
    return 0 if killed == len(plan) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, StopIteration) as error:
        print(f"Mutation check refused: {error}", file=sys.stderr)
        sys.exit(2)
