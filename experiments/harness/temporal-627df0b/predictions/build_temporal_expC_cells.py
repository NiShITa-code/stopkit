#!/usr/bin/env python3
"""Build predictions/temporal-expC-cells.json (RULES-expC.md C.8): Experiment C's predictions, written before any
draft-5 validation pilot or procedure smoke (C.10) has run, for the operator's review.

Each cell either restates a C.8 row (origin "C.8": a condition row, or a common row that applies to the condition) or
follows from C.8's rows and the facts its basis names (origin "derived": a prediction C.8 does not state, which the
operator reviews before the freeze). Each cell carries:
  prediction       the expected outcome in words, with its timing where C.8 gives one;
  predicted_state  the expected value of the scorer rows named in scored_as, in analyze_c.py's vocabulary;
                   "unpredicted" or "reported, not predicted" where C.8 leaves the cell descriptive; "conditional" where
                   the value depends on an unpredicted row;
  label            C.7's grammar; "—" for a descriptive cell;
  basis            DESIGN-expC section 9's T-facts (their sources are listed there), our workload code (jobs_c.py), the
                   timing plan (harness_c.timing_plan), and what was seen in feasibility (C.13, marked as C.8 asks);
  refutation       the observation that would refute it;
  scored_as        the scorer rows ("<measure> / <outcome>", analyze_c.row) it is compared with after scoring;
                   "record / <field>" names a record.json field that no scored row carries;
  origin           "C.8" or "derived", as above.
Predictions never feed the scorer (C.8).

The configuration block is generated from what each trial records and checks (harness_c.EXPECTED, PORTS,
CLI_TIMEOUT_S, timing_plan, spec_of; jobs_c.WORKLOAD) and from the SDK defaults that DESIGN-expC section 9 names. It is
never edited by hand. The file also carries the SHA-256 of the rules and the design it was written from.
tests/test_runner_c.PredictionsFile checks that the committed file is exactly this script's output, so a change to the
rules, the timing plan or the workload shows before a validation run hashes the file (predictions_at_start, C.8, C.10).

    /root/temporal-venv/bin/python -B predictions/build_temporal_expC_cells.py      (writes and schema-checks the file)
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "temporal"))
import harness_c as H  # noqa: E402  (pure parts: the declared versions, the timing plan and the conditions)
import jobs_c as J     # noqa: E402  (the workload parameters; importing it needs no temporalio)

OUT = os.path.join(HERE, "temporal-expC-cells.json")
WRITTEN_FROM = ("temporal/RULES-expC.md", "temporal/DESIGN-expC.md")
DASH = "—"                                                    # C.8: the label of a descriptive cell

# ---- what the feasibility attempts showed (C.13), cited where a cell rests on it (C.8: "seen in feasibility") ----
FEAS_HB = ("[seen in feasibility: C.13, the C0-HB pilot of 13 Oct 07:14 placed the heartbeat RPCs about 0.02 s after "
           "each 4 s mark from the activity's start]")
FEAS_PC = ("[seen in feasibility: C.13, the PC pilot of 13 Oct 07:22 read STARTED, attempt 1, through the +30 s read and "
           "SCHEDULED, attempt 2, with a start-to-close timeout failure from the +60 s read]")
FEAS_PC_PROBE = ("[seen in feasibility: C.13, in the PC pilot's probe attempt 2 posted all 20 steps and its final effect "
                 "on the restarted worker, and the run closed COMPLETED]")
FEAS_EXIT = ("[seen in feasibility: C.13, the dev server took 2.0 s and 1.8 s to exit after the probe's SIGTERM in the two "
             "full pilots]")
FEAS_C0 = ("[seen in feasibility: C.13, the C0-HB pilot of 07:14 ran to its final effect, closed COMPLETED and stayed "
           "ended across the probe; draft 4 stopped 0.5 s earlier in the activity]")

# ---- our workload and timing plan (C cells; the time base of every timed row) ----
STEPS = ("jobs_c._work posts step n about (n - 1) s into the activity on a monotonic step clock, then its final effect "
         "about 20 s in (DESIGN-expC section 3)")
STOP = ("harness_c.timing_plan(full) puts planned t_stop 5.5 s after the step-4 anchor, 8.5 s into the activity (C.3), "
        "so step 9 is emitted 0.5 s before t_stop, step n from 10 on about (n - 9.5) s after it, and the final effect "
        "about 11.5 s after it")
HB_PHASE = ("T4 (D3 throttling; one heartbeat RPC per 0.8 x the 5 s heartbeat timeout, K6, so every 4 s), K4 (one "
            "heartbeat per step); the next RPC after the stop falls 12 s into the activity, 3.5 s after t_stop, when "
            "step 13 is emitted; the core's first send and flush times were not read (DESIGN-expC section 9, Not read)")
NOT_READ_PENDING = ("DESIGN-expC section 9, Not read: whether a closed run's description still lists its abandoned "
                    "activities was not read")
CLOSED_NO_EVENTS = "T2 and T9 (a closed execution takes no further events)"


def cell(prediction, state, label, basis, refutation, scored, origin="C.8"):
    return {"prediction": prediction, "predicted_state": state, "label": label, "basis": basis,
            "refutation": refutation, "scored_as": list(scored), "origin": origin}


def unpredicted(prediction, basis, scored, state="unpredicted", label=DASH, origin="C.8"):
    return cell(prediction, state, label, basis, "— (descriptive: no outcome refutes it)", scored, origin)


# ============================ rows shared by several conditions ============================
REQUEST_ROWS = ("events / five events: request_accepted",)


def request_sent(what, basis, extra=""):
    return cell(f"The {what} exits 0 within its 5 s command timeout{extra}", "accepted", "P",
                f"C.8 common Request accepted row: {basis}; the reply waits for no worker",
                "A non-zero exit, a timeout or an RPC error while the server is ready", REQUEST_ROWS)


CANCEL_SENT = ("T1 (the cancel records WorkflowExecutionCancelRequested and schedules a workflow task; the workflow's "
               "code acts on it later)")
TERMINATE_SENT = "T2 (terminate closes the execution with WorkflowExecutionTerminated)"


def no_request(procedure):
    return cell(f"No request is sent: the procedure is {procedure} (C.3), so the scorer reports 'not applicable'",
                "not applicable", "M", "C.3 procedure table (harness_c.CONDITIONS); analyze_c.request_state",
                "A cancel or terminate action in the record's intervention", REQUEST_ROWS, origin="derived")


CANCEL_ROWS = ("M2 / cancel first in history", "M2 / cancel_requested first seen")


def cancel_seen(read="+2 s", when="the cancel returns", why="no workflow task is open when the cancel arrives "
                "(the workflow is waiting on its work activity)"):
    return cell(f"WorkflowExecutionCancelRequested is in the history by the {read} read, the first scheduled read after "
                f"{when}, and describe reports cancel_requested from that read on",
                "cancel first in history: seen; cancel_requested first seen: seen", "P",
                "C.8 common Cancel recorded row: T1, T23 (*inference* for the flag: when the server sets it was not "
                f"read); {why}",
                f"Absent at the {read} read while the server is up", CANCEL_ROWS)


def cancel_not_seen(why):
    return cell("No read shows WorkflowExecutionCancelRequested, and describe never reports cancel_requested",
                "cancel first in history: not seen; cancel_requested first seen: not seen", "P",
                f"{why}; T1 (only a cancel request records the event), T23 (*inference*: the flag stays false without "
                "one)", "WorkflowExecutionCancelRequested in a read, or describe reporting cancel_requested",
                CANCEL_ROWS, origin="derived")


def twin(extra_basis=""):
    return cell("The twin reads COMPLETED; its accepted effect, steps 1-20 and final arrive once each, from one "
                "execution, attempt 1", "pass", "P",
                "C.8 common Twin row: T14; the untouched job returns after its work activity (jobs_c.JobC.run); it runs "
                "on its own worker and task queue, which no intervention touches (K10)" + extra_basis,
                "Any other outcome: another status, a missing or repeated effect, a second execution or another "
                "attempt", ("twin / twin control",))


def probe_stayed_ended(extra_basis=""):
    return cell("Stayed ended: the target, closed before the probe, keeps its status and its history's last event ID "
                "across the probe's restarts of both workers and the server, no declared effect arrives after them, and "
                "the probe's capability jobs and probe twin pass", "stayed ended", "P",
                f"C.8 common Durability probe row: T12 (--db-filename keeps the executions across a server restart); "
                f"{CLOSED_NO_EVENTS}" + extra_basis,
                "A declared effect after the probe's restarts, a status change, a changed last event ID, or NotFound",
                ("restart / durability probe",))


def pre_probe(what):
    return cell("Passes: no open trial run lists a pending activity in state STARTED, no declared effect arrives in the "
                "5 s before the probe stops anything, and the probe twin is in its timer (TimerStarted, no TimerFired, "
                f"no pending activity); {what}", "ok (the probe did not interrupt running work)", "M",
                "C.8 common Pre-probe check row: the watch is long enough for every target except PC's to close; T10, "
                "T11; the probe twin's 30 s timer (harness_c.timing_plan probe_twin_timer_s)",
                "A STARTED activity on an open run, a declared effect in those 5 s, or the probe twin outside its timer",
                ("record / probe.pre_check", "restart / durability probe"))


PROBE_TWIN = cell("The probe twin completes after the probe's restarts, within the probe's 60 s observation",
                  "completed", "P", "C.8 common Probe twin row: T12 (its timer and history persist in the database "
                  "file); a 30 s timer, then a 3-step HB activity (DESIGN-expC section 3)",
                  "Not completed within the probe's observation", ("record / probe.ptwin_completed",))


def receiver(outage=""):
    return cell("The receiver logs every tick, with no gap over 2 s from the first tick to the end of the probe, "
                "through the probe's server outage" + outage + " too", "no gap", "S",
                "C.8 common Receiver row: receiver_c.py runs outside every Temporal process; the harness posts a tick "
                "every second (C.9)", "A gap over 2 s, or a malformed effect post",
                ("validity / trial validity",))


VS_ROWS = ("verified_stop / verified stop",)
VS_BASIS = ("C.8 common Verified stop row: C.5 (the computation boundary with its history cross-check, and the durable "
            "obligation) and C.6; the outcomes it combines carry the condition rows' labels")


def verified(kind, why):
    if kind == "unpredicted":
        return unpredicted(f"Unpredicted: it follows {why}", VS_BASIS, VS_ROWS)
    return cell(f"{kind[0].upper() + kind[1:]}: {why}. Any verdict needs a valid trial (C.9); otherwise it is "
                "unresolved", kind, "M", VS_BASIS, "Another verdict", VS_ROWS)


def vs_reality(state, prediction, causal):
    return cell(prediction + ". The scorer's M5 rule (C.6) decides each read from the declared effects that arrived "
                "in the 2 s (active) or 5 s (quiet) before it", state, "M",
                f"Derived from C.8's common Status against reality row ('causal labels from the condition rows'): "
                f"{causal}", "A 'false' verdict at a read where the timed rows put no declared effect in the 2 s "
                "before a closed status, or none where they put one; an M5 verdict that uses later evidence",
                ("M5 / status vs reality <read>", "M5 / stale activity state <read>"), origin="derived")


def heartbeat_phase(extra=""):
    return cell("The heartbeat phase precondition passes: the pre-intervention check's describe places the last "
                "heartbeat RPC within 0.25 s of the 4 s grid from the activity's start, and the grid's RPC before the "
                "stop within 0.25 s of 8 s into the activity, so the next RPC is at 12 s and the derived bound is step "
                "14" + extra, "pass", "P|K4,K6+M", f"C.6 M3 precondition: {HB_PHASE} {FEAS_HB}",
                "off-plan or UNKNOWN", ("M3 / heartbeat phase precondition",), origin="derived")


FAC_O2 = ("FAC / headline O2 x by commit",)
FAC_O1 = ("FAC / headline O1 x by commit",)
DUP_ROWS = ("M4 / duplicates", "M4 / duplicates visible in Temporal")
HIST_ROWS = ("history / history omits an execution that produced effects",)
POC_ROWS = ("pending_on_closed / a closed run lists a pending activity",)
STATE_ROWS = ("M2 / claim", "M2 / terminal status", "M2 / closing event")


def m1_rows(ref):
    return (f"M1 / by commit, {ref}", f"M1 / by initiation, {ref}", f"M1 / ambiguous, {ref}", "M3 / final landed")


def pending_unpredicted():
    return unpredicted("Unpredicted: whether the server clears a closed run's abandoned activity was not read",
                       "C.8 common Pending activities on a closed run row: " + NOT_READ_PENDING, POC_ROWS)


def o2_unpredicted():
    return unpredicted("Unpredicted: O2 needs 'no pending activity' at the claiming read, and whether a closed run "
                       "still lists its abandoned activity is unpredicted (pending_on_closed)",
                       "C.8 common False all-clear under O2 row: " + NOT_READ_PENDING, FAC_O2)


def omitted_running(extra=""):
    return cell("Yes, the history omits the execution: the workflow closes while the ordinary activity's execution is "
                "still running, so the closed history has ActivityTaskScheduled but no ActivityTaskStarted for it, and "
                "nothing in Temporal records its effects" + extra, "yes", "P",
                "C.8 common History coverage row: T9 (ActivityTaskStarted is written only when the activity execution "
                "closes; *inference*); " + CLOSED_NO_EVENTS,
                "An ActivityTaskStarted event for that execution", HIST_ROWS)


def one_execution(label, basis, extra=""):
    return cell("None: one execution, attempt 1" + extra, "none; visible in Temporal: no duplicates", label, basis,
                "A second execution (a second exec_nonce) of the work activity in the watch", DUP_ROWS,
                origin="derived")


ONE_EXEC_CLOSED = ("derived: " + CLOSED_NO_EVENTS + ", so nothing of the closed run is retried; and the activity's own "
                   "end, a cancellation or a completion, is not a failure that the retry policy (T11, K5) retries "
                   "(*inference*, D6)")


# ============================ the conditions ============================
def c0(variant):
    local = variant == "LA"
    c = {
        "request_accepted": no_request("none: t_stop is only the observation anchor"),
        "state": cell("COMPLETED: the first read with a closed status comes only after the final effect, about 11.5 s "
                      "after t_stop; the closing event is WorkflowExecutionCompleted",
                      "claim COMPLETED (after the final effect); terminal COMPLETED; closing WorkflowExecutionCompleted",
                      "P", "C.8 C0 Status row: T14; the untouched workflow returns once its work activity's result "
                      "reaches it (jobs_c.JobC.run; T7, the await continues on the activity's result); " + STEPS
                      + (" " + FEAS_C0 if variant == "HB" else ""),
                      "Another status, or a closed status before the final effect", STATE_ROWS),
        "effects_after_request": cell(
            "Counted from t_stop, the observation anchor: steps 10-20 and the final effect (12 effects) commit after "
            "it, by commit and by initiation; none during the call, since no effect is in flight at t_stop",
            "by commit: effects after (12); by initiation: effects after (12); ambiguous: 0; final landed: yes",
            "C+M", "C.8 C0 M1 row: " + STEPS + "; " + STOP,
            "Another split, or an effect sent within 0.25 s of t_stop", m1_rows("from t_stop")),
        "false_all_clear": cell("No false all-clear: the first read that makes the O2 claim (COMPLETED, the list view "
                                "agreeing, no pending activity, the history ending with WorkflowExecutionCompleted) "
                                "follows the final effect, the job's last", "no false all-clear", "P",
                                "C.8 C0 Status row ('no false all-clear'): T14; the final POST is the work activity's "
                                "last statement (jobs_c._work), and the workflow then returns",
                                "A declared effect committing after the first O2 claim", FAC_O2),
        "false_all_clear_o1": cell("No false all-clear under O1 either: the first closed describe follows the final "
                                   "effect", "no false all-clear", "P", "C.8 C0 Status row, as above",
                                   "A declared effect committing after the first O1 claim", FAC_O1),
        "duplicates": cell("None: the accepted effect, steps 1-20 and the final effect arrive once each, from one "
                           "execution, attempt 1", "none; visible in Temporal: no duplicates", "C",
                           "C.8 C0 Effects row: DESIGN-expC section 3 (jobs_c.announce, jobs_c._work); nothing fails, so "
                           "the declared retry policy (K5) never applies", "A missing or repeated effect",
                           DUP_ROWS + ("M3 / final landed",)),
        "history_coverage": (
            cell("Nothing omitted: one MarkerRecorded for the work activity, and WorkflowTaskCompleted events during "
                 "the 20 s local activity from workflow-task heartbeating", "no", "P|K9",
                 "C.8 C0 History row: T8 (a local activity's result is a MarkerRecorded event; the SDK heartbeats the "
                 "workflow task at about 80 % of its 10 s timeout); K9 makes the local activity run about 20 s",
                 "No marker for the work activity, or no workflow-task heartbeat events during the local activity",
                 HIST_ROWS) if local else
            cell("Nothing omitted: ActivityTaskStarted (attempt 1) and ActivityTaskCompleted record the one execution",
                 "no", "P", "C.8 C0 History row: T9 (ActivityTaskStarted is written when the execution closes, with "
                 "its attempt)", "A missing ActivityTaskStarted or ActivityTaskCompleted", HIST_ROWS)),
        "pending_on_closed": (
            cell("No: a local activity is never listed as a pending activity", "no", "P",
                 "derived: T8 (a local activity runs in the worker and never enters an activity task queue), T13 "
                 "(*inference*: describe lists the server's pending activities, which a local activity is not)",
                 "A closed read listing a pending activity", POC_ROWS, origin="derived") if local else
            cell("No: the work activity completed before the workflow did, so no closed read lists a pending activity",
                 "no", "P", "derived: T7 (the workflow returns only after the activity's result reaches it), T13",
                 "A closed read listing a pending activity", POC_ROWS, origin="derived")),
        "cancel_first_seen": cancel_not_seen("derived: no cancel is sent (C.3)"),
        "restart_outcome": probe_stayed_ended(" " + FEAS_C0 if variant == "HB" else ""),
        "twin": twin("; in C0-LA the twin is a local-activity job" if local else ""),
        "verified_stop": verified("ended by completion, not by the stop", "the one execution ends with its final "
                                  "effect and the run closes COMPLETED"),
        "pre_probe_check": pre_probe("the target closes about 11.5 s after t_stop, long before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": vs_reality(
            "false at the claim read only; true at every other usable read",
            "Open and 'true' (active) at every read before the final effect; 'false' at the claim read, because the "
            "final effect arrived less than 2 s before the run's first closed read (the claim poll reads every 0.5 s "
            "and the run closes in the workflow task that follows the activity's result): a measurement artefact of "
            "natural completion, not a false all-clear; closed and 'true' (quiet) from the +20 s read",
            "the C0 Status row (P) and the 0.5 s claim poll (M)"),
    }
    if variant == "HB":
        c["heartbeat_phase"] = heartbeat_phase()
    return c


def pc():
    return {
        "request_accepted": no_request("a SIGKILL of the target worker"),
        "state": cell("RUNNING throughout the watch, so no claim. The pending work activity reads STARTED, attempt 1, "
                      "until about 60 s after attempt 1 started, about 51.5 s after t_stop (a stale activity state while "
                      "its worker is dead), then SCHEDULED, attempt 2, with a start-to-close timeout failure",
                      "claim: no claim; terminal RUNNING; stale activity state while STARTED attempt 1 is listed",
                      "P|K5,K6", "C.8 PC Status row: T10 (the server does not detect the dead worker; the 60 s "
                      "start-to-close timeout, K6, fails attempt 1), T11 and K5 (one retry after 1 s), T13; no worker "
                      "polls the target queue in the watch (C.3, K10) " + FEAS_PC,
                      "A closed status; no SCHEDULED attempt 2 by t_stop + 70 s",
                      ("M2 / claim", "M2 / terminal status", "M5 / stale activity state <read>")),
        "effects_after_request": cell(
            "Counted from t_stop: no effect commits or is initiated after the kill in the watch, and none is in flight "
            "at it (step 9 is emitted 0.5 s before the kill and step 10 would be 0.5 s after)",
            "by commit: none after; by initiation: none after; ambiguous: 0; final landed: no", "O",
            "C.8 PC Effects row: SIGKILL ends the worker process, and every effect is a POST from it "
            "(jobs_c.post_effect); " + STOP, "Any effect initiated after the kill in the watch, or one in flight at it",
            m1_rows("from t_stop")),
        "pc_gate": cell("Passes (C.9's gate over the effects row): no effect initiated after the kill in the watch, and "
                        "none arriving after t_stop + 0.1 s unless its sender stamp precedes the kill. Attempt 2's "
                        "re-run in the durability probe is the restart_outcome row, not a gate failure", "pass", "O",
                        "C.8 PC Effects row; C.9 (PC is the gate)", "fail",
                        ("PC / no effect after the kill (gate)",)),
        "false_all_clear": cell("No O2 claim: no read in the watch reports a closed status", "no O2 claim",
                                "P|K5,K6", "derived from the PC Status row", "A closed status in the watch", FAC_O2,
                                origin="derived"),
        "false_all_clear_o1": cell("No O1 claim, for the same reason", "no O1 claim", "P|K5,K6",
                                   "derived from the PC Status row", "A closed status in the watch", FAC_O1,
                                   origin="derived"),
        "duplicates": one_execution("P|K5,K6", "derived from the PC Status row: attempt 2 is scheduled about 52.5 s "
                                    "after t_stop (T10, T11, K5, K6), and no worker polls the target queue until the "
                                    "probe; the twin's worker polls only the twin queue (K10)",
                                    " (steps 1-9) in the watch; attempt 2 waits SCHEDULED until the probe"),
        "history_coverage": cell("Yes, the history omits the execution: at the end of the watch the open run's history "
                                 "has ActivityTaskScheduled for the work activity and no ActivityTaskStarted, because "
                                 "the activity has not closed (attempt 2 is pending)", "yes", "P|K5,K6",
                                 "derived: T9 (ActivityTaskStarted is written only when the activity execution closes, "
                                 "with its last attempt; *inference*); K5 allows attempt 2, so attempt 1's timeout does "
                                 "not close the activity", "An ActivityTaskStarted for the work activity in the watch",
                                 HIST_ROWS, origin="derived"),
        "pending_on_closed": cell("Not applicable: no read in the watch reports a closed status", "not applicable",
                                  "P|K5,K6", "derived from the PC Status row", "A closed read in the watch", POC_ROWS,
                                  origin="derived"),
        "cancel_first_seen": cancel_not_seen("derived: no cancel is sent (C.3)"),
        "restart_outcome": cell("Re-run, then closed: attempt 2 runs all 20 steps and the final effect on the restarted "
                                "target worker, and the run closes COMPLETED within the probe's observation (allowing "
                                "the sticky-queue wait)", "re-run, closed after restart: COMPLETED", "P|K5",
                                "C.8 PC Durability probe row: T10, T11 (attempt 2 is pending when the probe restarts "
                                "the target worker), T21 (the workflow task may first wait up to 10 s on the dead "
                                "worker's sticky queue) " + FEAS_PC_PROBE, "No attempt-2 effect in the probe",
                                ("restart / durability probe",)),
        "twin": twin(),
        "verified_stop": verified("not stopped", "the run reads RUNNING at the end-of-watch read"),
        "pre_probe_check": pre_probe("PC's run is still open, but its attempt 2 waits SCHEDULED on the dead worker's "
                                     "queue, which is not running work"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": vs_reality(
            "never false; stale activity state while STARTED attempt 1 is listed (to about +51.5 s)",
            "Never 'false', because no read is closed: 'true' (open and active) at reads less than 2 s after step 9 "
            "arrives (about 1.5 s after t_stop), UNKNOWN until 5 s after it, then 'open, quiet'; a 'stale activity "
            "state' row at every read that lists STARTED attempt 1 while the worker is dead (to about +51.5 s)",
            "the PC Status (P|K5,K6) and Effects (O) rows"),
    }


def c1():
    return {
        "request_accepted": request_sent("cancel", CANCEL_SENT),
        "state": cell("TimerCanceled, then WorkflowExecutionCanceled; the claim reads CANCELED by the +2 s read",
                      "claim CANCELED (by +2 s); terminal CANCELED; closing WorkflowExecutionCanceled", "P|K2",
                      "C.8 C1 State row: T1 (the cancel schedules a workflow task), T7 (the cancel job cancels the "
                      "workflow's main task, workflow.sleep cancels its timer, and the cancelled exception escapes the "
                      "workflow function under K2, which becomes the CancelWorkflowExecution command)",
                      "Another status; TimerFired", STATE_ROWS),
        "effects_after_request": cell("None after the request: the work activity is never scheduled",
                                      "by commit: none after; by initiation: none after; final landed: no", "P|K2",
                                      "C.8 C1 Effects row: T7",
                                      "Any progress or final effect, or ActivityTaskScheduled for the work activity",
                                      m1_rows("from the cancel")),
        "false_all_clear": cell("No false all-clear at any read that makes the O2 claim", "no false all-clear", "P|K2",
                                "C.8 C1 False all-clear row: follows from the State and Effects rows",
                                "A declared effect after the claim", FAC_O2),
        "false_all_clear_o1": cell("No false all-clear at any read that makes the O1 claim", "no false all-clear",
                                   "P|K2", "C.8 C1 False all-clear row, as above", "A declared effect after the claim",
                                   FAC_O1),
        "duplicates": cell("None: no work execution runs", "none; visible in Temporal: no duplicates", "P|K2",
                           "derived from the C1 Effects row (T7)", "A work execution", DUP_ROWS, origin="derived"),
        "history_coverage": cell("Not applicable: no work execution posts; the closed history has no "
                                 "ActivityTaskScheduled for the work activity", "not applicable", "P|K2",
                                 "C.8 C1 Effects row: T7",
                                 "An ActivityTaskScheduled for the work activity, or a work effect", HIST_ROWS),
        "pending_on_closed": cell("No: announce completed before the timer started, and the work activity was never "
                                  "scheduled", "no", "P|K2", "derived: T7 (the workflow awaits announce's result before "
                                  "its timer; the cancelled timer ends the workflow before the work), T13",
                                  "A closed read listing a pending activity", POC_ROWS, origin="derived"),
        "cancel_first_seen": cancel_seen(why="no workflow task is open when the cancel arrives (the workflow waits in "
                                         "its timer)"),
        "restart_outcome": probe_stayed_ended(),
        "twin": twin(),
        "verified_stop": verified("stopped", "no work execution runs, and the closed history shows the work activity "
                                  "never scheduled"),
        "pre_probe_check": pre_probe("the target closes by the +2 s read, long before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": vs_reality(
            "never false",
            "Never 'false': the last declared effect is the accepted effect, 15 s before t_stop, so open reads are "
            "'open, quiet' and closed reads 'true'", "the C1 State and Effects rows (P|K2)"),
    }


def hb_cut(cond, what):
    """C2, C2w and C5: an HB activity under cancel or terminate learns of it at its next heartbeat RPC."""
    learns = {"cancel": "learns of the cancellation", "terminate": "learns that its run is gone"}[what]
    flag = "cancel_requested" if cond == "C2w" else "not_found"
    flag_basis = ("T5 (an explicit cancel is marked 'by request'; the run is still open, so the server answers the "
                  "heartbeat with the cancellation; *inference*)" if cond == "C2w" else
                  "T5 (NOT_FOUND: the server no longer knows the activity; *inference*: the server's answer to a "
                  "heartbeat from a closed run's activity was not read, DESIGN-expC section 9)")
    return {
        "effects_after_request": cell(
            f"Steps 10-12 of the first execution land after the request, and perhaps step 13; no step from 14 on and no "
            f"final effect. The activity {learns} at its next heartbeat RPC, nominally 12 s into the activity, 3.5 s "
            f"after t_stop; step 13 is emitted at that moment, and our code then stops (K14)",
            "by commit: effects after (steps 10-12, perhaps 13, of the first execution); final landed: no",
            "P|K1,K4,K6,K8,K14+M",
            f"C.8 {cond} Effects row: T3, T5" + (", T2" if what == "terminate" else "") + f"; {HB_PHASE}; {STOP} "
            + FEAS_HB, "A step from 14 on or a final effect from the first execution; no step 12 while the server is "
            "up (delivery before the next heartbeat)", m1_rows(f"from the {what}")),
        "delivery_bound": cell("Within: the first execution posts no step from the bound (14) on and no final effect; "
                               "judged because the heartbeat phase precondition passes", "within",
                               "P|K1,K4,K6,K8,K14+M",
                               f"C.8 {cond} Effects row and C.6 M3 (delivery_bound_step 14, harness_c.timing_plan): "
                               f"{HB_PHASE}; K14 (our activity re-raises the cancellation with no further effect)",
                               "exceeded: a step from 14 on, or a final effect, from the first execution",
                               ("M3 / delivery bound",)),
        "heartbeat_phase": heartbeat_phase(),
        "delivery_time": cell("The first execution's cancellation acknowledgement comes at the next nominal heartbeat "
                              "RPC, 12 s into the activity, about 3.5 s after t_stop",
                              "acknowledged within 0.5 s of the next nominal RPC", "P|K4,K6+M",
                              f"C.8 {cond} Delivery time row: T3, T5; {HB_PHASE}",
                              "An acknowledgement more than 0.5 s from that RPC while the phase precondition passed, "
                              "or none", ("M3 / delivery time",)),
        "stops_after_delivery": cell("No effect of the first execution is initiated after its acknowledgement",
                                     "stopped after delivery", "C|K1,K8,K14",
                                     f"C.8 {cond} Stops after delivery row: K14 (jobs_c._work logs the cancellation and "
                                     "re-raises it, with no cleanup and no final effect), K1 (an asyncio task cancel), "
                                     "K8 (the POST is interruptible)",
                                     "An effect whose sender stamp follows the acknowledgement",
                                     ("M3 / stops after delivery",)),
        "worker_log": cell(f"One cancellation acknowledgement, in the first execution, with the {flag} flag",
                           f"1 acknowledgement; flags: {flag}", "C", f"C.8 {cond} Worker log row: {flag_basis}",
                           "No acknowledgement, or another flag",
                           ("M3 / worker-log acknowledgements (C diagnostic)", "M3 / delivery time")),
    }


def c2():
    c = {
        "request_accepted": request_sent("cancel", CANCEL_SENT),
        "state": cell("ActivityTaskCancelRequested, then WorkflowExecutionCanceled, in one workflow task; the claim "
                      "reads CANCELED by the +2 s read, before the activity's next heartbeat (nominally 3.5 s after "
                      "t_stop)", "claim CANCELED (by +2 s); terminal CANCELED; closing WorkflowExecutionCanceled",
                      "P|K2,K3", "C.8 C2 State row: T6 (TRY_CANCEL reports the cancellation to the workflow at once), "
                      "T7 (the cancelled exception escapes under K2) " + FEAS_HB,
                      "RUNNING at the +2 s read with the worker up; ActivityTaskCanceled before "
                      "WorkflowExecutionCanceled", STATE_ROWS),
    }
    c.update(hb_cut("C2", "cancel"))
    c.update({
        "false_all_clear": o2_unpredicted(),
        "false_all_clear_o1": cell("True at the first claiming read: the claim comes by the +2 s read, and at least "
                                   "step 12, emitted 2.5 s after t_stop, commits after it", "false all-clear",
                                   "P|K2,K3,K4,K6+M", "C.8 C2 False all-clear row: T3-T7 and the rows above",
                                   "No declared effect after the first claim", FAC_O1),
        "duplicates": one_execution("P", ONE_EXEC_CLOSED),
        "history_coverage": omitted_running(),
        "pending_on_closed": pending_unpredicted(),
        "cancel_first_seen": cancel_seen(),
        "restart_outcome": probe_stayed_ended(),
        "twin": twin(),
        "verified_stop": verified("unresolved", "the first execution stops without its final effect while its worker "
                                  "stays alive, so its end has no independent evidence"),
        "pre_probe_check": pre_probe("the target closes by the +2 s read and its last effect lands about 3.5 s after "
                                     "t_stop, long before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": vs_reality(
            "false from the claim read through the +2 s read (the +5 s read only if step 13 landed); true from +10 s",
            "'false' at the claim read and at every closed read up to 2 s after the first execution's last effect "
            "(step 12 at about +2.5 s, or step 13 at +3.5 s if it lands), so through the +2 s read, and at the +5 s "
            "read only if step 13 landed; closed and 'true' (quiet) from the +10 s read",
            "the C2 State (P|K2,K3) and Effects (P|K1,K4,K6,K8,K14+M) rows"),
    })
    return c


def c2w():
    c = {
        "request_accepted": request_sent("cancel", CANCEL_SENT),
        "state": cell("RUNNING, with the pending activity in state CANCEL_REQUESTED, at the +2 s read, until the "
                      "activity acknowledges the cancellation at its next heartbeat RPC (nominally 3.5 s after t_stop); "
                      "then ActivityTaskCanceled and WorkflowExecutionCanceled. The claim follows the activity's last "
                      "effect", "claim CANCELED (after the activity's last effect); terminal CANCELED; closing "
                      "WorkflowExecutionCanceled", "P|K2",
                      "C.8 C2w State row: T5, T6 (WAIT_CANCELLATION_COMPLETED waits for the activity's confirmation), "
                      "T7, T13 (*inference*: the server sets CANCEL_REQUESTED when it records the cancel command; the "
                      "core's handling of a cancelled completion was not read) " + FEAS_HB,
                      "A closed status before the activity's last effect; no ActivityTaskCanceled; RUNNING without "
                      "CANCEL_REQUESTED at the +2 s read", STATE_ROWS),
    }
    c.update(hb_cut("C2w", "cancel"))
    c.update({
        "false_all_clear": cell("No false all-clear at any read, under O2: the run stays open until the activity's "
                                "last effect, and a closed read lists no pending activity", "no false all-clear", "P|K2",
                                "C.8 C2w False all-clear row: follows from the rows above", "A declared effect after "
                                "the claim", FAC_O2),
        "false_all_clear_o1": cell("No false all-clear at any read, under O1", "no false all-clear", "P|K2",
                                   "C.8 C2w False all-clear row, as above", "A declared effect after the claim", FAC_O1),
        "duplicates": one_execution("P", ONE_EXEC_CLOSED),
        "history_coverage": cell("Nothing omitted: ActivityTaskStarted (attempt 1) and ActivityTaskCanceled record the "
                                 "execution", "no", "P", "C.8 C2w History coverage row: T9 (*inference*)",
                                 "Either event missing", HIST_ROWS),
        "pending_on_closed": cell("No: ActivityTaskCanceled closes the activity before the workflow closes", "no", "P",
                                  "derived: T6 (WAIT_CANCELLATION_COMPLETED waits for the activity's confirmation), T9, "
                                  "T13", "A closed read listing a pending activity", POC_ROWS, origin="derived"),
        "cancel_first_seen": cancel_seen(),
        "restart_outcome": probe_stayed_ended(),
        "twin": twin(),
        "verified_stop": verified("unresolved", "the first execution stops without its final effect while its worker "
                                  "stays alive, so its end has no independent evidence"),
        "pre_probe_check": pre_probe("the target closes about 4 s after t_stop, long before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": vs_reality(
            "false at the claim read (and at the +5 s read only if step 13 landed); true from +10 s",
            "Open and 'true' (active) before the claim; 'false' at the claim read, because the activity's last effect "
            "(step 12 or 13) arrives less than 2 s before the run closes: a measurement artefact, not a false "
            "all-clear; the +5 s read is 'false' too if step 13 landed; closed and 'true' (quiet) from the +10 s read",
            "the C2w State (P|K2) and Effects rows"),
    })
    return c


def nh_runs_on(cond, closes, closes_when):
    """C3, C3w and C5n: a non-heartbeating activity never learns of the request and runs to its final effect."""
    return {
        "effects_after_request": cell(
            "The remaining steps (10-20) and the final effect land after the request, the final effect nominally "
            "about 11.5 s after t_stop; one execution",
            "by commit: effects after (steps 10-20 and final, 12 effects); final landed: yes", "P",
            f"C.8 {cond} Effects row: T3 (an activity that does not heartbeat cannot receive a cancellation; it runs to "
            f"completion)" + (", T2 (pending activities get no chance to stop cleanly; *inference*)"
                              if cond == "C5n" else "") + f"; {STEPS}; {STOP}",
            "progress stops before step 20 without a final effect",
            m1_rows("from the terminate" if cond == "C5n" else "from the cancel")),
        "worker_log": cell("No cancellation acknowledgement", "0 acknowledgements", "P",
                           (f"C.8 {cond} Effects row" if cond != "C3w" else "derived from the C3w Effects row")
                           + ": T3 (the activity never heartbeats, so nothing is delivered to it)",
                           "An acknowledgement", ("M3 / worker-log acknowledgements (C diagnostic)",),
                           origin="derived" if cond == "C3w" else "C.8"),
        "verified_stop": verified("ended by completion, not by the stop", f"the execution runs on to its final effect "
                                  f"and the run is closed ({closes})"),
        "pre_probe_check": pre_probe(f"{closes_when}, long before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "restart_outcome": probe_stayed_ended(),
        "twin": twin(),
    }


def c3():
    c = {
        "request_accepted": request_sent("cancel", CANCEL_SENT),
        "state": cell("The claim reads CANCELED by the +2 s read; the history ends with WorkflowExecutionCanceled",
                      "claim CANCELED (by +2 s); terminal CANCELED; closing WorkflowExecutionCanceled", "P|K2,K3",
                      "C.8 C3 State row: T6 (TRY_CANCEL reports the cancellation to the workflow at once), T7",
                      "RUNNING at +2 s with the worker up", STATE_ROWS),
        "false_all_clear": o2_unpredicted(),
        "false_all_clear_o1": cell("True at every claiming read before the final effect", "false all-clear",
                                   "P|K2,K3", "C.8 C3 False all-clear row: T3, T6, T7",
                                   "No declared effect after the first claim", FAC_O1),
        "duplicates": one_execution("P", ONE_EXEC_CLOSED),
        "history_coverage": omitted_running("; it has no ActivityTaskCompleted either, so nothing in Temporal "
                                            "records the final effect (C.8 C3 'Temporal's record of the run' row)"),
        "pending_on_closed": pending_unpredicted(),
        "cancel_first_seen": cancel_seen(),
    }
    c.update(nh_runs_on("C3", "CANCELED", "the target closes by the +2 s read and its final effect lands about "
                                          "11.5 s after t_stop"))
    c["status_vs_reality"] = vs_reality(
        "false at the claim read and the +2, +5 and +10 s reads; true from +20 s",
        "'false' at the claim read and at every closed read up to 2 s after the final effect (about +13.5 s): the "
        "+2, +5 and +10 s reads; closed and 'true' (quiet) from the +20 s read",
        "the C3 State (P|K2,K3) and Effects (P) rows")
    return c


def c3w():
    c = {
        "request_accepted": request_sent("cancel", CANCEL_SENT),
        "state": cell("Before the final effect: RUNNING, with the pending activity in state CANCEL_REQUESTED. After it: "
                      "COMPLETED, because the shielded await continues on the activity's result and the workflow "
                      "returns it", "RUNNING with CANCEL_REQUESTED before the final effect; claim COMPLETED (after it); "
                      "terminal COMPLETED; closing WorkflowExecutionCompleted", "P|K2",
                      "C.8 C3w rows: T6, T13 (*inference*: the server sets CANCEL_REQUESTED when it records the cancel "
                      "command); T7, SDK worker/_workflow_instance.py:2345-2390 (the SDK code was read; the core's "
                      "handling was not)", "A closed status before the final effect; CANCELED or FAILED", STATE_ROWS),
        "false_all_clear": cell("No false all-clear at any read, under O2", "no false all-clear", "P|K2",
                                "C.8 C3w False all-clear row: follows from the State and Effects rows",
                                "A declared effect after the claim", FAC_O2),
        "false_all_clear_o1": cell("No false all-clear at any read, under O1", "no false all-clear", "P|K2",
                                   "C.8 C3w False all-clear row, as above", "A declared effect after the claim", FAC_O1),
        "duplicates": one_execution("P", ONE_EXEC_CLOSED),
        "history_coverage": cell("Nothing omitted: ActivityTaskStarted (attempt 1) and ActivityTaskCompleted record "
                                 "the execution", "no", "P", "derived: T9 (*inference*: the server accepts the "
                                 "completion of an activity in state CANCEL_REQUESTED and writes the started event "
                                 "then); the C3w Terminal status row", "Either event missing", HIST_ROWS,
                                 origin="derived"),
        "pending_on_closed": cell("No: the activity completed before the workflow did", "no", "P",
                                  "derived: T7 (the workflow returns only after the activity's result), T13",
                                  "A closed read listing a pending activity", POC_ROWS, origin="derived"),
        "cancel_first_seen": cancel_seen(),
    }
    c.update(nh_runs_on("C3w", "COMPLETED", "the target closes right after its final effect, about 11.5 s after "
                                            "t_stop"))
    c["status_vs_reality"] = vs_reality(
        "false at the claim read only; true at every other usable read",
        "Open and 'true' (active) until the final effect; 'false' at the claim read, because the final effect arrives "
        "less than 2 s before it: a measurement artefact, not a false all-clear; closed and 'true' (quiet) from the "
        "+20 s read", "the C3w State (P|K2) and Effects (P) rows")
    return c


def c4():
    stage = "C4 stage 1 (unpredicted)"
    return {
        "request_accepted": request_sent("cancel", CANCEL_SENT),
        "c4_stage1": unpredicted(
            "Unpredicted; neither reading is primary. Reading (a): the cancel reaches workflow code at the next "
            "workflow-task heartbeat, nominally 16 s into the local activity, about 7.5 s after t_stop (the first, at "
            "about 80 % of the 10 s workflow-task timeout, falls about 0.5 s before the stop). Reading (b): only after "
            "the local activity finishes, nominally about 11.5 s after t_stop. C.6 decides the reading from what "
            "workflow code did (an acknowledgement, the run's close before the final effect, or a cancelled marker; "
            "or the run open after the final effect), never from the order of the events in the history",
            "C.8 C4 Stage 1 row: T8, T22, T24; the core's workflow-task heartbeat times and the server's buffering of "
            "a cancel request were not read (DESIGN-expC section 9, Not read); either reading would be P|K2,K3,K9",
            ("C4 / stage 1: when the cancel reaches workflow code",)),
        "c4_server_order": cell("Reported, not predicted: likely 'yes' under either reading, since the workflow-task "
                                "heartbeat completes a task before the marker", "reported, not predicted (likely yes)",
                                "M", "C.8 C4 row: T8, T24", "— (descriptive: no outcome refutes it)",
                                ("C4 / server recorded the cancel request before the marker",)),
        "c4_stage2_a": cell("Under reading (a): the local activity is cancelled without heartbeats: no final effect, "
                            "and no local-activity effect commits more than 1.5 s after its cancellation "
                            "acknowledgement in the worker log; the run closes CANCELED",
                            "stopped (when stage 1 reads (a))", "P|K1,K2,K8,K9,K14",
                            "C.8 C4 Stage 2 (a) row: T8 (D2: a local activity can be cancelled without heartbeats; the "
                            "SDK issues RequestCancelLocalActivity from the shielded await), T6, T7; *inference*: the "
                            "core's local-activity cancel path was not read; once it is delivered, our code stops (K14)",
                            "A final effect, or effects continuing more than 1.5 s after the acknowledgement",
                            ("C4 / stage 2: local activity stops without heartbeats",)),
        "c4_stage2_b": unpredicted("Under reading (b): stage 2 is not tested, because every step and the final effect "
                                   "land first; the closing status (CANCELED or COMPLETED) is unpredicted",
                                   "C.8 C4 Stage 2 (b) row: T8", ("C4 / stage 2: local activity stops without "
                                                                  "heartbeats", "M2 / terminal status"),
                                   state="unpredicted closing status; stage 2 not tested (when stage 1 reads (b))",
                                   label="P|K9"),
        "state": unpredicted("Unpredicted: it follows stage 1. Under reading (a) the run closes CANCELED about 7.5 s "
                             "after t_stop; under reading (b) it closes after the final effect, about 11.5 s after "
                             "t_stop, CANCELED or COMPLETED", "the C4 stage rows of C.8", STATE_ROWS),
        "effects_after_request": unpredicted("Unpredicted: it follows stage 1. Under (a): no final effect, and no "
                                             "local-activity effect more than 1.5 s after the acknowledgement; under "
                                             "(b): every remaining step and the final effect",
                                             "the C4 stage rows of C.8", m1_rows("from the cancel")),
        "false_all_clear": cell("Conditional on stage 1. Under (a): true if a local-activity effect commits after the "
                                "claim. Under (b): false at every read", "conditional (follows " + stage + ")",
                                "P|K2,K9", "C.8 C4 False all-clear row (the answer under (a) is M)",
                                "Under (b), a declared effect after the claim", FAC_O2),
        "false_all_clear_o1": cell("Conditional on stage 1, as under O2", "conditional (follows " + stage + ")",
                                   "P|K2,K9", "C.8 C4 False all-clear row (the answer under (a) is M)",
                                   "Under (b), a declared effect after the claim", FAC_O1),
        "duplicates": cell("None: one execution of the local activity", "none; visible in Temporal: no duplicates",
                           "P", "C.8 C4 Duplicates row: T8 (at least once, so a second execution would need a "
                           "failure or a lost workflow task, and neither is planted)", "Two executions", DUP_ROWS),
        "history_coverage": unpredicted("Unpredicted: it follows stage 1. Under (b): nothing omitted, one "
                                        "MarkerRecorded (T8). Under (a): whether the core records a marker for a "
                                        "cancelled local activity was not read",
                                        "the C4 stage rows of C.8; T8", HIST_ROWS, origin="derived"),
        "pending_on_closed": cell("No: a local activity is never listed as a pending activity, under either reading",
                                  "no", "P", "derived: T8, T13 (*inference*, as in C0-LA)",
                                  "A closed read listing a pending activity", POC_ROWS, origin="derived"),
        "cancel_first_seen": cell("Reported, not predicted: the first read whose history shows "
                                  "WorkflowExecutionCancelRequested and the read before it, the first read with "
                                  "cancel_requested, and the pending workflow task at each read",
                                  "reported, not predicted", "M", "C.8 C4 Cancel first seen row: T22, T23; the common "
                                  "Cancel recorded row does not apply to C4", "— (descriptive: no outcome refutes it)",
                                  CANCEL_ROWS),
        "restart_outcome": probe_stayed_ended("; the run closes by about 11.5 s after t_stop under either reading"),
        "twin": twin("; in C4 the twin is a local-activity job"),
        "verified_stop": verified("unpredicted", "stage 1 (under (a) the execution ends without its final effect while "
                                  "its worker stays alive; under (b) it ends by completion)"),
        "pre_probe_check": pre_probe("the target closes by about 11.5 s after t_stop under either reading, long "
                                     "before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": unpredicted("Unpredicted: it follows stage 1",
                                         "C.8's common Status against reality row takes its causes from the C4 rows",
                                         ("M5 / status vs reality <read>",), origin="derived"),
    }


def c5():
    c = {
        "request_accepted": request_sent("terminate", TERMINATE_SENT),
        "state": cell("TERMINATED at the +0 read; the closing event is WorkflowExecutionTerminated",
                      "claim TERMINATED (by the +0 read); terminal TERMINATED; closing WorkflowExecutionTerminated", "P",
                      "C.8 C5 State row: T2 (terminate closes the execution; workflow code cannot see or respond to it)",
                      "Another status at +0", STATE_ROWS),
    }
    c.update(hb_cut("C5", "terminate"))
    c.update({
        "false_all_clear": o2_unpredicted(),
        "false_all_clear_o1": cell("True at the first claiming read (the +0 read or an earlier claim-poll read): at "
                                   "least steps 11 and 12, emitted 1.5 s and 2.5 s after t_stop, commit after it",
                                   "false all-clear", "P|K4,K6+M", "C.8 C5 False all-clear row: T2, T4 and the rows "
                                   "above", "No declared effect after the first claim", FAC_O1),
        "duplicates": one_execution("P", ONE_EXEC_CLOSED),
        "history_coverage": omitted_running(),
        "pending_on_closed": pending_unpredicted(),
        "cancel_first_seen": cancel_not_seen("derived: a terminate is sent, not a cancel (T2)"),
        "restart_outcome": probe_stayed_ended(),
        "twin": twin(),
        "verified_stop": verified("unresolved", "the first execution stops without its final effect while its worker "
                                  "stays alive, so its end has no independent evidence"),
        "pre_probe_check": pre_probe("the target closes by the +0 read and its last effect lands about 3.5 s after "
                                     "t_stop, long before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": vs_reality(
            "false at the claim, +0 and +2 s reads (the +5 s read only if step 13 landed); true from +10 s",
            "'false' at the claim read (the first claim-poll read), the +0 and +2 s reads, and any closed read up to "
            "2 s after the first execution's last effect (step 12 at about +2.5 s, or step 13 at +3.5 s if it lands); "
            "closed and 'true' (quiet) from the +10 s read", "the C5 State (P) and Effects rows"),
    })
    return c


def c5n():
    c = {
        "request_accepted": request_sent("terminate", TERMINATE_SENT),
        "state": cell("TERMINATED at the +0 read", "claim TERMINATED (by the +0 read); terminal TERMINATED; closing "
                      "WorkflowExecutionTerminated", "P", "C.8 C5n State row: T2", "Another status", STATE_ROWS),
        "false_all_clear": o2_unpredicted(),
        "false_all_clear_o1": cell("True at every claiming read before the final effect", "false all-clear", "P",
                                   "C.8 C5n False all-clear row: T2, T3", "No declared effect after the first claim",
                                   FAC_O1),
        "duplicates": one_execution("P", ONE_EXEC_CLOSED),
        "history_coverage": omitted_running(),
        "pending_on_closed": pending_unpredicted(),
        "cancel_first_seen": cancel_not_seen("derived: a terminate is sent, not a cancel (T2)"),
    }
    c.update(nh_runs_on("C5n", "TERMINATED", "the target closes by the +0 read and its final effect lands about "
                                             "11.5 s after t_stop"))
    c["status_vs_reality"] = vs_reality(
        "false at the claim read and the +0, +2, +5 and +10 s reads; true from +20 s",
        "'false' at the claim read and the +0, +2, +5 and +10 s reads, every closed read up to 2 s after the final "
        "effect (about +13.5 s); closed and 'true' (quiet) from the +20 s read", "the C5n State and Effects rows (P)")
    return c


def c6():
    descriptive = "C.8 C6 (descriptive): the core's handling of a failed heartbeat RPC was not read " + FEAS_EXIT
    return {
        "request_accepted": request_sent("cancel", CANCEL_SENT),
        "state": cell("The claim reads CANCELED before the server's SIGTERM, due 1 s after the cancel returns (the "
                      "precondition), and every usable read after the restart reads CANCELED",
                      "claim CANCELED (before the SIGTERM); terminal CANCELED; closing WorkflowExecutionCanceled",
                      "P|K2,K3", "C.8 C6 Precondition and After the restart rows: T6, T7, T12",
                      "Another status, before the SIGTERM or after the restart", STATE_ROWS),
        "effects_after_request": cell(
            "Step 10, emitted 0.5 s after t_stop and before the SIGTERM, commits after the cancel returns; what the "
            "abandoned execution posts from the SIGTERM on is recorded, not predicted (c6_effects_during_outage)",
            "by commit: effects after (at least step 10); later effects descriptive", "P|K4,K6+M",
            "derived: nothing reaches the activity before its next heartbeat RPC, 12 s into the activity (T3, T4, K4, "
            f"K6); {STOP}; C.8 C6 leaves the outage descriptive " + FEAS_HB,
            "No step 10, or an acknowledgement before step 10's emission", m1_rows("from the cancel"),
            origin="derived"),
        "c6_precondition": cell("describe reads CANCELED before the SIGTERM", "pass", "P|K2,K3",
                                "C.8 C6 Precondition row: T6, T7",
                                "fail (C.2: the target is then untested, and the trial is rerun)",
                                ("C6 / precondition: CANCELED before the SIGTERM",)),
        "c6_effects_during_outage": cell("Recorded, not predicted: the abandoned HB execution's effects between the "
                                         "SIGTERM and the server's readiness, and the worker log's acknowledgement and "
                                         "flags; the SIGTERM is due about 1.2 s after t_stop and 2.3 s before the next "
                                         "heartbeat RPC, and the server's exit races that RPC",
                                         "reported, not predicted", "M", descriptive,
                                         "— (descriptive: no outcome refutes it)", ("C6 / effects during the outage",)),
        "c6_final": cell("Unpredicted: whether the final effect lands is set by our 1 s delay (M), the server's stop "
                         "and restart time (O) and the core's heartbeat retry (not read)", "unpredicted", "M",
                         descriptive, "— (descriptive: no outcome refutes it)", ("C6 / final landed",)),
        "c6_after_restart": cell("The run reads CANCELED after the restart, and the history's last event ID equals "
                                 "that of the last closed history read before the SIGTERM",
                                 "after the restart: CANCELED; last event ID across the restart: unchanged", "P",
                                 "C.8 C6 After the restart row: T12; " + CLOSED_NO_EVENTS,
                                 "Another status, a changed last event ID, or NotFound",
                                 ("C6 / after the restart", "C6 / last event ID across the restart")),
        "c6_rerun_branch": unpredicted("Only if the precondition failed: a second execution may start beside attempt "
                                       "1 if the server treats attempt 1's heartbeat timeout as expired; recorded after "
                                       "the restart are the pending activity's attempt and last failure",
                                       "C.8 C6 Re-run branch row: T10, K5; the server's handling of an overdue timeout "
                                       "at restart was not read", ("C6 / after the restart", "M4 / duplicates")),
        "in_watch_restart": unpredicted("Unpredicted: 'resumed' if the abandoned execution still posts after the "
                                        "server's readiness, otherwise 'stayed ended'; it follows the descriptive "
                                        "outage rows", "C.6 restart outcome (after a server restart the window starts "
                                        "at readiness); " + descriptive,
                                        ("restart / in-watch restart 1 (server)",), origin="derived"),
        "false_all_clear": o2_unpredicted(),
        "false_all_clear_o1": unpredicted("Unpredicted: step 10 (+0.5 s) races the claim, and the next effect that "
                                          "could follow the claim, step 11 (+1.5 s), is emitted after the SIGTERM, "
                                          "inside the outage that C.8 leaves descriptive",
                                          "derived: C.8 gives no C6 O1 row; the C6 rows and the State row",
                                          FAC_O1, origin="derived"),
        "duplicates": one_execution("P", ONE_EXEC_CLOSED + "; the precondition passes (the re-run branch applies only "
                                    "if it fails)"),
        "history_coverage": omitted_running(),
        "pending_on_closed": pending_unpredicted(),
        "cancel_first_seen": cancel_seen(read="+0 s", when="the cancel returns, before the SIGTERM (the +2 s read "
                                         "falls in the outage)"),
        "restart_outcome": probe_stayed_ended(),
        "twin": cell("The twin, the persistence control, completes after the server restart: it started 25 s after the "
                     "target and is in its timer during the restart; its effects arrive once each, from one execution, "
                     "attempt 1", "pass", "P", "C.8 C6 Twin row: T12 (its timer persists in the database file), T14",
                     "Not completed by the end of the watch, or another twin outcome", ("twin / twin control",)),
        "verified_stop": verified("unpredicted", "the descriptive outage rows"),
        "heartbeat_phase": heartbeat_phase(),
        "pre_probe_check": pre_probe("the target closes before the in-watch SIGTERM, and the abandoned execution "
                                     "cannot run past its final effect, about 11.5 s after t_stop, long before the "
                                     "probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(" and C6's in-watch server outage"),
        "status_vs_reality": vs_reality(
            "false at the claim read; reads failed in the outage UNKNOWN; the rest unpredicted",
            "'false' at the claim read (step 9 or 10 arrives less than 2 s before it); reads that fail in the outage "
            "are UNKNOWN (SERVER_UNAVAILABLE_DUE_TO_INTERVENTION); the other reads are unpredicted, as the outage is",
            "the C6 State row (P|K2,K3) and the descriptive outage rows"),
    }


def c7(local):
    first = "The first execution posts steps 1-9 and stops at the kill (step 9 is emitted 0.5 s before it)"
    if local:
        state = cell("RUNNING until COMPLETED after the second execution's final effect, with no earlier claim; "
                     "describe lists no pending activity (a local activity is not one)",
                     "claim COMPLETED (after the second final effect); terminal COMPLETED; closing "
                     "WorkflowExecutionCompleted", "P|K9", "C.8 C7l Status row: T8, T14",
                     "Another status, or a closed status before the second final effect", STATE_ROWS)
        effects = cell(first + ". After the killed worker's workflow task times out (up to 10 s) and generation 2 "
                       "starts at +15 s, the local activity runs again from step 1, because no marker was recorded: "
                       "steps are duplicated, and there is one final effect",
                       "from t_stop: effects after (the second execution's steps 1-20 and final); none from the first "
                       "execution; final landed: yes", "P|K9", "C.8 C7l Effects row: T8 (at least once); " + STEPS,
                       "No second execution", m1_rows("from t_stop"))
        dup = cell("Duplicates: two executions, the second repeating steps 1-9; whether Temporal's attempt numbers or "
                   "markers show the first is unpredicted", "duplicates; visible in Temporal: unpredicted", "P|K9",
                   "C.8 C7l Effects row (T8); the Attempt numbers row: the core's local-activity bookkeeping is "
                   "compiled and was not read", "No second execution", DUP_ROWS)
        hist = unpredicted("Unpredicted: the attempt numbers and the history trace of the first execution",
                           "C.8 C7l Attempt numbers and history trace row: the core's local-activity bookkeeping is "
                           "compiled and was not read", HIST_ROWS)
        poc = cell("No: a local activity is never listed as a pending activity", "no", "P",
                   "derived: T8, T13 (*inference*, as in C0-LA)", "A closed read listing a pending activity",
                   POC_ROWS, origin="derived")
        restart = cell("Re-run, closed after restart (COMPLETED): a new execution posts after generation 2 starts, "
                       "and the run, RUNNING before, closes COMPLETED", "re-run, closed after restart: COMPLETED",
                       "P|K9", "derived from the C7l Effects and Status rows (T8)",
                       "No new execution after the restart, or another closing status",
                       ("restart / in-watch restart 1 (worker:target)",), origin="derived")
        fac_origin, causal = "derived", "the C7l Status and Effects rows (P|K9)"
        claim_vs = ("'false' at the claim read, because the local activity's result and the workflow's completion go "
                    "in one workflow task completion right after the final effect (T8): a measurement artefact, not a "
                    "false all-clear; no stale-activity rows")
        fac_label, fac_basis = "P|K9", "derived from the C7l Status row"
        vs_state = "false at the claim read only; never before it"
        closes = "the target closes right after the second execution's final effect, about 35-40 s after t_stop"
    else:
        state = cell("RUNNING until COMPLETED after the second execution's final effect, with no earlier claim. The "
                     "pending activity reads STARTED, attempt 1, at the +0 and +2 s reads (stale: its worker is dead), "
                     "then SCHEDULED, attempt 2, with a heartbeat-timeout failure by the +10 s read (STARTED only after "
                     "generation 2 starts at +15 s). The last heartbeat RPC is about 0.5 s before the kill, so the 5 s "
                     "heartbeat timeout falls about 4.5 s after t_stop and the retry follows after its 1 s initial "
                     "interval; the +5 s read is too close to call",
                     "claim COMPLETED (after the second final effect); terminal COMPLETED; closing "
                     "WorkflowExecutionCompleted; STARTED attempt 1 at +2 s; SCHEDULED attempt 2 by +10 s",
                     "P|K4,K5,K6", "C.8 C7 State row: T10, T11, T13, T21; K6 (the 5 s heartbeat timeout), K5 (the 1 s "
                     "initial retry interval) " + FEAS_HB,
                     "A closed status before the second final effect; not STARTED attempt 1 at the +2 s read; no "
                     "attempt 2 by the +10 s read", STATE_ROWS + ("M5 / stale activity state <read>",))
        effects = cell(first + ". The second execution (attempt 2) posts steps 1-20 and the final effect after the "
                       "restart at +15 s, repeating steps 1-9. There is one final effect",
                       "from t_stop: effects after (attempt 2's steps 1-20 and final, 21 effects); none from the first "
                       "execution; final landed: yes", "P|K4,K5,K6",
                       "C.8 C7 Effects row: T10, T11; K4 (a retried execution starts again at step 1); " + STOP,
                       "A first-execution step after 9; no second execution; a second final effect",
                       m1_rows("from t_stop"))
        dup = cell("Duplicates: two executions, attempt 2 repeating steps 1-9; Temporal shows them through attempt 2 "
                   "(describe lists attempt 2 from the +10 s read; ActivityTaskStarted carries attempt 2)",
                   "duplicates; visible in Temporal: yes", "P|K4,K5,K6", "C.8 C7 Effects and History rows: T9, T10, T11",
                   "No second execution, or no attempt 2 in describe or the history", DUP_ROWS)
        hist = cell("Yes, the history omits the first execution: one ActivityTaskStarted, with attempt 2 and the "
                    "heartbeat-timeout failure; only the attempt number shows that an earlier execution existed, and "
                    "nothing records its effects", "yes", "P", "C.8 C7 History row: T9 (the started event reflects "
                    "the last attempt)", "An ActivityTaskStarted for attempt 1", HIST_ROWS)
        poc = cell("No: attempt 2 completes before the workflow does", "no", "P",
                   "derived: T7 (the workflow returns only after the activity's result), T13",
                   "A closed read listing a pending activity", POC_ROWS, origin="derived")
        restart = cell("Re-run, closed after restart (COMPLETED); no false all-clear", "re-run, closed after restart: "
                       "COMPLETED", "P|K4,K5,K6", "C.8 C7 Restart outcome row: as the State and Effects rows",
                       "No new execution after the restart, or another closing status",
                       ("restart / in-watch restart 1 (worker:target)",))
        fac_origin, causal = "C.8", "the C7 State and Effects rows (P|K4,K5,K6)"
        claim_vs = ("whether the claim read is 'false' depends on how soon after the second final effect the run "
                    "closes, which the sticky-queue wait can delay up to 10 s (T21; whether the server skips it when "
                    "the sticky worker is gone was not read): unpredicted; 'stale activity state' while STARTED "
                    "attempt 1 is listed with the worker dead, to about 5 s after t_stop")
        fac_label, fac_basis = "P|K4,K5,K6", "C.8 C7 Restart outcome row ('no false all-clear')"
        vs_state = ("never false before the claim; the claim read unpredicted; stale activity state while STARTED "
                    "attempt 1 is listed (to about +5 s)")
        closes = ("the target closes after the second execution's final effect, about 35-50 s after t_stop (the "
                  "sticky-queue wait included)")
    c = {
        "request_accepted": no_request("a SIGKILL of the target worker, then a restart at +15 s"),
        "state": state,
        "effects_after_request": effects,
        "false_all_clear": cell("No false all-clear under O2: the run closes only after the second execution's final "
                                "effect", "no false all-clear", fac_label, fac_basis,
                                "A declared effect after the first O2 claim", FAC_O2, origin=fac_origin),
        "false_all_clear_o1": cell("No false all-clear under O1, for the same reason", "no false all-clear",
                                   fac_label, fac_basis, "A declared effect after the first O1 claim", FAC_O1,
                                   origin=fac_origin),
        "duplicates": dup,
        "history_coverage": hist,
        "pending_on_closed": poc,
        "cancel_first_seen": cancel_not_seen("derived: no cancel is sent (C.3)"),
        "in_watch_restart": restart,
        "restart_outcome": probe_stayed_ended(),
        "twin": twin("; in C7l the twin is a local-activity job" if local else ""),
        "verified_stop": verified("ended by completion, not by the stop", "the killed execution's worker is confirmed "
                                  "dead and the second execution ends with its final effect; the run closes COMPLETED"),
        "pre_probe_check": pre_probe(closes + ", long before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": vs_reality(
            vs_state,
            "Open until the claim: 'true' while effects arrive, 'open, quiet' from about 4.5 s after t_stop until the "
            "second execution's first step; " + claim_vs, causal),
    }
    if not local:
        c["heartbeat_phase"] = heartbeat_phase(" (the pre-intervention check runs before the kill)")
    return c


def c8():
    return {
        "request_accepted": request_sent("cancel", CANCEL_SENT, extra=", sent 3 s after the kill while no worker runs"),
        "state": cell("RUNNING before the restart, with no worker to act on the cancel: the cancel at +3 s comes "
                      "before attempt 1's heartbeat timeout (the last heartbeat RPC is about 0.5 s before the kill, so "
                      "the 5 s timeout falls about 4.5 s after t_stop, 1.5 s after the cancel); so the pending activity "
                      "reads STARTED, attempt 1, at the +2 s read and SCHEDULED, attempt 2, with a heartbeat-timeout "
                      "failure, by the +10 s read; no CANCEL_REQUESTED state appears before the restart, because no "
                      "worker runs the workflow task that would request the activity's cancellation. The run closes "
                      "CANCELED after generation 2 starts",
                      "before the restart: RUNNING, STARTED attempt 1 at +2 s, SCHEDULED attempt 2 by +10 s; claim "
                      "CANCELED (after the restart); terminal CANCELED; closing WorkflowExecutionCanceled",
                      "P|K2,K5,K6", "C.8 C8 State before the restart row: T1, T10, T11, T13 " + FEAS_HB,
                      "CANCEL_REQUESTED or a closed status before the restart; attempt 2 or a heartbeat-timeout failure "
                      "at the +2 s read; no attempt 2 by the +10 s read", STATE_ROWS),
        "c8_claim": cell("CANCELED only after generation 2 starts, by 5 s after its readiness", "after the restart",
                         "P|K2,K3", "C.8 C8 Claim row: T1, T7", "A closed status before the restart",
                         ("C8 / claim relative to the restart",)),
        "effects_after_request": cell(
            "No final effect from any execution, and no first-execution effect after the kill. Generation 2 may start "
            "attempt 2 before it processes the cancel, so attempt-2 progress effects can land; their number is "
            "unpredicted (a race)", "conditional: M1 reads 'effects after' only if attempt 2 posts (count "
            "unpredicted); final landed: no", "P|K2,K4", "C.8 C8 Effects row: T4, T5, T10",
            "A final effect, or a first-execution effect after the kill",
            ("M1 / by commit, from the kill", "M1 / by commit, from the cancel", "M3 / final landed")),
        "false_all_clear": unpredicted("Unpredicted: O2 needs 'no pending activity', which is unpredicted here "
                                       "(pending_on_closed), and attempt 2's effects are a race",
                                       "derived: C.8 C8 Effects row; " + NOT_READ_PENDING, FAC_O2, origin="derived"),
        "false_all_clear_o1": cell("True if an attempt-2 effect commits after the claim", "conditional (attempt 2's "
                                   "race)", "M", "C.8 C8 False all-clear row", "Under the condition, no declared "
                                   "effect after the claim", FAC_O1),
        "duplicates": unpredicted("Unpredicted (a race): two executions if generation 2 starts attempt 2 before it "
                                  "processes the cancel; then describe shows attempt 2 from the +10 s read",
                                  "C.8 C8 Effects row (T4, T5, T10)", DUP_ROWS),
        "history_coverage": cell("Yes, the history omits the first execution: the run closes while the activity is "
                                 "pending, so no ActivityTaskStarted is written for attempt 1, or for attempt 2 if it "
                                 "ran", "yes", "P", "derived: T9 (*inference*, as the common History coverage row for "
                                 "a run that closes while its activity is pending)",
                                 "An ActivityTaskStarted for the work activity", HIST_ROWS, origin="derived"),
        "pending_on_closed": unpredicted("Unpredicted: whether the server clears a closed run's abandoned activity, "
                                         "SCHEDULED or STARTED at the cancel, was not read",
                                         "derived: the basis of C.8's common row; " + NOT_READ_PENDING, POC_ROWS,
                                         origin="derived"),
        "cancel_first_seen": cancel_seen(read="+5 s", when="the cancel, sent at +3 s, returns",
                                         why="no workflow task can be open while no worker runs (C.8 C8 Cancel "
                                             "recorded row, *inference*)"),
        "in_watch_restart": cell("Closed after restart (CANCELED); also re-run if attempt 2 posts",
                                 "closed after restart: CANCELED (with re-run if attempt 2 posts)", "P|K2",
                                 "C.8 C8 Restart outcome row: T1, T7, T10",
                                 "Another closing status after the restart, or the first execution posting after it",
                                 ("restart / in-watch restart 1 (worker:target)",)),
        "restart_outcome": probe_stayed_ended(),
        "twin": twin(),
        "verified_stop": verified("unpredicted", "attempt 2's race and the unpredicted pending-on-closed row"),
        "heartbeat_phase": heartbeat_phase(" (the pre-intervention check runs before the kill)"),
        "pre_probe_check": pre_probe("the target closes soon after generation 2 starts at +20 s, and attempt 2, "
                                     "if it runs, cannot outlast its 20 steps (about +41 s), long before the probe"),
        "probe_twin": PROBE_TWIN,
        "receiver": receiver(),
        "status_vs_reality": vs_reality(
            "never false before the restart; stale activity state while STARTED attempt 1 is listed (to about "
            "+5 s); after the restart unpredicted",
            "Open before the restart: 'true' while effects arrive, 'stale activity state' while STARTED attempt 1 is "
            "listed with the worker dead (to about 5 s after t_stop), 'open, quiet' from about 4.5 s after t_stop; "
            "after the "
            "restart unpredicted (attempt 2's race)", "the C8 State and Effects rows"),
    }


# ============================ prior exposure (C.13) ============================
SEEN_HB_PHASE = "the heartbeat phase its timed rows use was seen in the C0-HB pilot of 13 Oct 07:14"
NH_SEEN = ("an NH job's uninterrupted steps were seen in the PC pilot of 13 Oct 07:22 (attempt 2 in its probe, and its "
           "untouched twin)")
EXPOSURE = {
    "C0-HB": ("Seen (C.13): four C0-HB attempts on 13 Oct, on wsl, record schema 1, sources before draft 5. FAST smokes "
              "at 05:33 (the watch and both capability jobs completed; the probe failed on a launch-identity mismatch) "
              "and 06:40 (target untested: the twin's accepted effect was missing; both capability jobs timed out); a "
              "full pilot at 06:48 whose procedure was withheld (its pre-stop reads timed out); and a full pilot at "
              "07:14 on draft 4 (source 9db3f11): the target ran to its final effect, closed COMPLETED and stayed "
              "ended across the probe; the heartbeat RPCs came about 0.02 s after each 4 s mark; step 9 arrived "
              "0.001 s after draft 4's t_stop; the twin queue's capability job timed out; the server took 2.0 s to exit "
              "after the probe's SIGTERM."),
    "C0-NH": ("No C0-NH trial has run (C.13). Related: the PC pilot of 13 Oct 07:22 ran NH jobs; its target posted "
              "steps 1-9 before the kill and, in the probe, all 20 steps and its final effect as attempt 2; its twin, "
              "an untouched NH job, was in the inspected record, though C.13 does not report the twin's outcome."),
    "C0-LA": "None (C.13): no local-activity job has run in any attempt; the attempts ran HB and NH jobs only.",
    "PC": ("Seen (C.13): one full pilot on 13 Oct 07:22, draft 4 (source 9db3f11), with the stop at step 4 + 5 s: no "
           "declared effect was initiated after the kill in the watch (gate pass); the run stayed RUNNING, STARTED "
           "attempt 1 through the +30 s read, then SCHEDULED attempt 2 with a start-to-close timeout failure from the "
           "+60 s read; in the probe attempt 2 posted all 20 steps and its final effect and the run closed COMPLETED; "
           "the server took 1.8 s to exit after the probe's SIGTERM."),
    "C1": "None for this condition (C.13: no cancel or terminate has run); stage A and the 30 s timer ran in every "
          "attempt.",
    "C2": f"None for this condition (C.13: no cancel or terminate has run); {SEEN_HB_PHASE}.",
    "C2w": f"None for this condition (C.13: no cancel or terminate has run); {SEEN_HB_PHASE}.",
    "C3": f"None for this condition (C.13: no cancel or terminate has run); {NH_SEEN}.",
    "C3w": f"None for this condition (C.13: no cancel or terminate has run); {NH_SEEN}.",
    "C4": "None (C.13): no cancel and no local-activity job has run.",
    "C5": f"None for this condition (C.13: no cancel or terminate has run); {SEEN_HB_PHASE}.",
    "C5n": f"None for this condition (C.13: no cancel or terminate has run); {NH_SEEN}.",
    "C6": ("None for this condition (C.13: no cancel and no in-watch server restart has run); its descriptive rows use "
           "the dev server's exit times after the probe's SIGTERM in the two full pilots of 13 Oct (2.0 s and 1.8 s), "
           f"and {SEEN_HB_PHASE}."),
    "C7": ("None for this condition (C.13). Related: the PC pilot of 13 Oct 07:22 killed the target worker during NH "
           "with no restart (a stale STARTED attempt 1, then SCHEDULED attempt 2); " + SEEN_HB_PHASE + "."),
    "C7l": "None (C.13): no local-activity job and no in-watch worker restart has run.",
    "C8": ("None for this condition (C.13: no cancel has run). Related: the PC pilot's worker kill (13 Oct 07:22); "
           + SEEN_HB_PHASE + "."),
}

PRIOR_EXPOSURE = (
    "Disclosed as RULES-expC C.8 and C.13 require. Before these predictions were written, five Experiment C "
    "feasibility attempts had run (13 Oct 2026, on the wsl host profile, record schema 1, sources before draft 5), and "
    "their outputs were inspected while drafts 4 and 5 were written: four of C0-HB and one of PC (C.13 lists them). No "
    "cancel, terminate or in-watch restart condition and no local-activity job has run. Each condition's "
    "prior_exposure says what was seen of it; cells that rest on a feasibility observation say '[seen in feasibility: "
    "...]' in their basis (C.8). The predictions were written on 14 Oct 2026 from RULES-expC, DESIGN-expC and the "
    "code at commit feb1b97 of branch expC-temporal (written_from holds the rules' and the design's SHA-256), without "
    "opening any raw record. No draft-5 validation pilot or procedure smoke (C.10) had run, so exposed_validation is "
    "empty. Every later trial records this file's SHA-256 when it starts (predictions_at_start): a validation run "
    "that starts under another version of this file is prior exposure and must be named in exposed_validation (C.8, "
    "C.10).")

NOT_PREDICTED = [
    "M3 / time to quiet, execution <n> (info)",
    "M3 / heartbeat phase (metadata) (info)",
    "M3 / worker-log acknowledgements (C diagnostic), except where a worker_log cell names its count and flags",
    "M3 / step interrupted in flight, execution <n> (C|K8 diagnostic)",
    "M3 / step lateness and load (diagnostic)",
    "M5 / list vs describe <read>",
    "events / five events, beyond request_accepted: work ceased, effects closed and evidence sufficient are combined "
    "in the verified stop (C.6)",
    "validity / trial validity: every cell assumes a valid trial (C.9); validity is a precondition, not a prediction",
]


# ============================ configuration (C.0) ============================
def configuration():
    plan = H.timing_plan(False)
    server = H.TrialRun.server_cmd(types.SimpleNamespace(db="<trial>/temporal.db"))[1:]
    return {
        "generated_from": "harness_c.EXPECTED, PORTS, CLI_TIMEOUT_S, TrialRun.server_cmd, timing_plan(full) and "
                          "spec_of, and jobs_c.WORKLOAD['full'] (the values each trial records and checks, C.0); "
                          "worker_defaults from DESIGN-expC section 9",
        "versions": dict(H.EXPECTED),
        "cli_path": "/opt/temporal-cli-1.9.1/temporal (RULES-expC C.1)",
        "server_args": server,
        "persistence": "SQLite, --db-filename in the trial folder (T12)",
        "dynamic_config": "default",
        "namespace": "default",
        "ports": dict(H.PORTS),
        "cli_command_timeout_s": H.CLI_TIMEOUT_S,
        "host_profiles": list(H.HOST_PROFILES),
        "host_profile_note": "the freeze binds one declared profile (C.0, C.10); the feasibility timings these rows "
                             "cite came from wsl",
        "workflow_task_timeout_s": 10.0,
        "workflow_task_timeout_note": "the server default (T8); each run's WorkflowExecutionStarted event records it",
        "worker_defaults": {
            "graceful_shutdown_timeout_s": 0,
            "sticky_queue_schedule_to_start_timeout_s": 10,
            "heartbeat_throttle": "0.8 x the heartbeat timeout when one is set (4 s for HB); otherwise 30 s; at most 60 s",
            "sources": "T15 and K12; T21; T4 (SDK worker/_worker.py); every other option at its SDK default (K11)"},
        "workload": J.WORKLOAD["full"],
        "timing_plan": plan,
        "conditions": {c: H.spec_of(c) for c in H.CONDITIONS},
    }


ORDER = ("request_accepted", "state", "c8_claim", "effects_after_request", "pc_gate", "heartbeat_phase",
         "delivery_bound", "delivery_time", "stops_after_delivery", "worker_log", "c4_stage1", "c4_server_order",
         "c4_stage2_a", "c4_stage2_b", "c6_precondition", "c6_effects_during_outage", "c6_final", "c6_after_restart",
         "c6_rerun_branch", "false_all_clear", "false_all_clear_o1", "duplicates", "history_coverage",
         "pending_on_closed", "cancel_first_seen", "status_vs_reality", "in_watch_restart", "restart_outcome",
         "pre_probe_check", "probe_twin", "twin", "receiver", "verified_stop")


def build():
    conditions = {"C0-HB": c0("HB"), "C0-NH": c0("NH"), "C0-LA": c0("LA"), "PC": pc(), "C1": c1(), "C2": c2(),
                  "C2w": c2w(), "C3": c3(), "C3w": c3w(), "C4": c4(), "C5": c5(), "C5n": c5n(), "C6": c6(),
                  "C7": c7(False), "C7l": c7(True), "C8": c8()}
    if list(conditions) != list(H.CONDITIONS):
        raise ValueError("the conditions differ from harness_c.CONDITIONS")
    out = {}
    for cond, measures in conditions.items():
        spec = H.spec_of(cond)
        unknown = set(measures) - set(ORDER)
        if unknown:
            raise ValueError(f"{cond}: measures outside ORDER: {sorted(unknown)}")
        out[cond] = {"variant": spec["variant"], "procedure": spec["procedure"],
                     "cancellation_type": spec["cancellation_type"], "prior_exposure": EXPOSURE[cond],
                     "measures": {m: measures[m] for m in ORDER if m in measures}}
    written_from = {}
    for rel in WRITTEN_FROM:
        with open(os.path.join(ROOT, rel), "rb") as f:
            written_from[rel] = hashlib.sha256(f.read()).hexdigest()
    return {
        "schema_version": 1,
        "status": "Predictions for Experiment C, written before any draft-5 validation pilot or procedure smoke, for "
                  "the operator's review; not frozen (C.12)",
        "written": "2026-10-14",
        "written_from": written_from,
        "rules": "RULES-expC.md C.7 (labels) and C.8 (predictions), draft 5 with the pre-freeze review",
        "fields": {
            "prediction": "the expected outcome in words",
            "predicted_state": "the expected value of the scored_as rows, in analyze_c.py's vocabulary, at the end of "
                               "the 120 s watch unless the cell says otherwise; 'unpredicted' or 'reported, not "
                               "predicted' where C.8 leaves the cell descriptive; 'conditional' where it depends on an "
                               "unpredicted row",
            "label": "C.7's grammar; '—' for a descriptive cell",
            "basis": "T-facts of DESIGN-expC section 9 (sources listed there), our code (jobs_c.py, the timing plan), "
                     "and '[seen in feasibility: ...]' for what C.13's attempts showed",
            "refutation": "the observation that would refute the prediction",
            "scored_as": "the scorer rows '<measure> / <outcome>' (analyze_c.row) it is compared with, after scoring; "
                         "'record / <field>' is a record.json field; <read> and <n> stand for each read's label and "
                         "each execution",
            "origin": "'C.8': the cell restates a C.8 row (a condition row, or a common row that applies); 'derived': "
                      "C.8 states no such row, and the cell follows from C.8's rows and the facts in its basis, for "
                      "the operator's review"},
        "times": "Times are on CLOCK_MONOTONIC from t_stop unless a cell says otherwise; the work activity starts "
                 "8.5 s before t_stop (C.3); '+N s' names the scheduled read at t_stop + N s (C.4 step 5)",
        "not_predicted": NOT_PREDICTED,
        "prior_exposure": PRIOR_EXPOSURE,
        "exposed_validation": [],
        "configuration": configuration(),
        "conditions": out,
    }


def render(data):
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    data = build()
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(data))
    spec = importlib.util.spec_from_file_location("run_expC_schema", os.path.join(ROOT, "temporal", "run_expC.py"))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.read_predictions(a.out, list(runner.CONDITIONS))          # the runner's own schema check (C.8)
    cells = sum(len(v["measures"]) for v in data["conditions"].values())
    print(f"wrote {a.out}: {len(data['conditions'])} conditions, {cells} cells; the runner's schema check passed")


if __name__ == "__main__":
    main()
