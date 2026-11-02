#!/usr/bin/env python3
"""Experiment C trial harness: run ONE Temporal cancellation-calibration trial and save its record
(DESIGN-expC.md; RULES-expC.md C.1-C.4, C.9).

Scope: start the receiver, the dev server (--db-filename in the trial folder) and both workers in a fresh network
namespace; submit the target and the twin; wait for the receiver anchor; run the pre-intervention check; apply the
condition's one intervention in its own thread; run the claim poll and the scheduled reads (CLI describe, show and
list, plus the receiver); do the condition's in-watch restarts; run the durability probe; clean up last; write
record.json. It does not score (analyze_c.py) and never reads predictions.

Every planted item is a benign canary (jobs_c.py): its only effect is a POST to the local receiver. Nothing here
reaches beyond the trial's loopback namespace.

    run as root, from run_expC.py:   python3 -B harness_c.py <condition> <trial-id>
It creates /run/netns/expc-<trial>, re-runs itself inside it, and deletes it afterwards.
Exit codes (as n8n/harness_b.py): 0 completed; 3 ran with untested work (rerun needed); 2 error or void.

Patterns copied (not the files) from n8n/harness_b.py: the intervention thread and its stamp box, the gate that
withholds a late intervention, run-kind checks, clean-up last. Reused unmodified: common/clockguard.py,
common/guards.py (and langgraph/recorder.py, through receiver_c.py).
"""
import errno
import hashlib
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "common"))
import clockguard  # noqa: E402  (common/clockguard.py, unmodified)
import guards      # noqa: E402  (common/guards.py, unmodified)
sys.path.insert(0, HERE)
import jobs_c as J     # noqa: E402  (pure parts only; temporalio is not needed to import it)
import analyze_c as A  # noqa: E402  (the one FIELD MAP, C.1)
import clockwall_c as clockwall  # noqa: E402  (the wall/monotonic rule the scorer recomputes, C.9)

# ---- the declared host profile (RULES-expC C.0 environment, C.1 isolation, C.9 disk floor; as n8n/harness_b.py) ----
# "wsl" (the default) is the laptop's native WSL2 install: the disk floor is common/guards.py disk_ok (free space on the
# Windows C: drive, which holds WSL's disk), and the isolation check is the trial namespace's (C.1). "linux-vm" is a
# plain Linux VM, which has no /mnt/c: its floor is the free space of the filesystems the trial writes to (the results
# folder, which holds the trial's database and logs, and /, which holds /root, /opt and /tmp), its kernel must not be
# WSL's, and its isolation check adds an outbound probe from inside the namespace. Declared, never inferred.
HOST_PROFILES = ("wsl", "linux-vm")
HOST_PROFILE = os.environ.get("STOPCHECK_HOST_PROFILE", "wsl")
SCOPE = {"wsl": "native WSL2 install", "linux-vm": "native install on a Linux VM (host profile linux-vm)"}
VM_DISK_PATHS = (os.path.join(HERE, "results"), "/")     # linux-vm: each must have the floor free
METADATA_ADDRESS = ("169.254.169.254", 80)               # the GCE metadata server: the host's nearest outside endpoint

PY = os.environ.get("STOPCHECK_TEMPORAL_PY", "/root/temporal-venv/bin/python")
CLI = os.environ.get("STOPCHECK_TEMPORAL_CLI", "/opt/temporal-cli-1.9.1/temporal")
EXPECTED = {"cli_version": "temporal version 1.9.1 (Server 1.32.0, UI 2.54.1)", "temporalio": "1.34.0",
            "python": "3.12.3"}
PORTS = {"grpc": 7833, "http": 7834, "metrics": 7835, "receiver": 8875}
ADDRESS = f"127.0.0.1:{PORTS['grpc']}"
RECEIVER = f"127.0.0.1:{PORTS['receiver']}"
CLI_TIMEOUT_S = 5
# Record schema 2 (pre-freeze batch of 13 Oct): from it the scorer requires the wall/monotonic receipt in
# clock_across_trial and its own recomputation from receiver.jsonl (C.9), and every record declares its host profile
SCHEMA = 2
now = time.monotonic

# ---- conditions (RULES-expC C.3) ----
PROCEDURES = ("none", "cancel", "terminate", "kill_worker", "cancel_server_restart", "kill_worker_restart",
              "kill_cancel_restart")
CONDITIONS = {
    "C0-HB": {"variant": "HB", "procedure": "none"},
    "C0-NH": {"variant": "NH", "procedure": "none"},
    "C0-LA": {"variant": "LA", "procedure": "none"},
    "PC": {"variant": "NH", "procedure": "kill_worker"},
    "C1": {"variant": "HB", "procedure": "cancel", "anchor": "accepted"},
    "C2": {"variant": "HB", "procedure": "cancel"},
    "C2w": {"variant": "HB", "procedure": "cancel", "cancellation_type": "WAIT_CANCELLATION_COMPLETED"},
    "C3": {"variant": "NH", "procedure": "cancel"},
    "C3w": {"variant": "NH", "procedure": "cancel", "cancellation_type": "WAIT_CANCELLATION_COMPLETED"},
    "C4": {"variant": "LA", "procedure": "cancel"},
    "C5": {"variant": "HB", "procedure": "terminate"},
    "C5n": {"variant": "NH", "procedure": "terminate"},
    "C6": {"variant": "HB", "procedure": "cancel_server_restart", "restart": "server"},
    "C7": {"variant": "HB", "procedure": "kill_worker_restart", "restart": "worker"},
    "C7l": {"variant": "LA", "procedure": "kill_worker_restart", "restart": "worker"},
    "C8": {"variant": "HB", "procedure": "kill_cancel_restart", "restart": "worker"},
}
SIGNALLED = {"kill_worker", "cancel_server_restart", "kill_worker_restart", "kill_cancel_restart"}


def spec_of(cond):
    if cond not in CONDITIONS:
        raise ValueError(f"unknown condition {cond}")
    s = {"anchor": "step", "cancellation_type": "TRY_CANCEL", "restart": None}
    s.update(CONDITIONS[cond])
    s["condition"] = cond
    s["has_request"] = s["procedure"] in ("cancel", "terminate", "cancel_server_restart", "kill_cancel_restart")
    s["signalled"] = "server" if s["procedure"] == "cancel_server_restart" else (
        "target worker" if s["procedure"] in SIGNALLED else None)
    return s


def timing_plan(fast):
    """All timing parameters of one trial (DESIGN-expC section 4; RULES-expC C.3, C.4, C.10)."""
    w = J.WORKLOAD["fast" if fast else "full"]
    if fast:
        p = {"anchor_step": 3, "stop_after_anchor_s": 0.75, "precheck_lead_s": 0.75,
             "precheck_io_reserve_s": 0.1, "c1_stop_after_accepted_s": 3.0, "watch_s": 30.0,
             "claim_poll_every_s": 0.5, "claim_poll_until_s": 20.0, "read_offsets_s": [0, 2, 5, 10, 20, 30],
             "capability_at_s": 25.0, "c6_server_delay_s": 1.0, "c6_twin_delay_s": 5.0, "c7_restart_s": 8.0,
             "c8_cancel_s": 1.5, "c8_restart_s": 12.0, "probe_obs_s": 20.0, "probe_read_every_s": 5.0,
             "probe_twin_timer_s": 8.0, "probe_twin_steps": 3, "delivery_bound_step": None,
             "anchor_wait_s": 60.0}
    else:
        # Draft 5 (C.3): the stop is 5.5 s after the step-4 anchor, 8.5 s into the activity: half a step from every
        # step emission, 0.5 s after the nominal heartbeat RPC at 8 s and 3.5 s before the next at 12 s (T4: one RPC
        # per 0.8 x 5 s). Draft 4's 5.0 s put the stop on the step-9 emission and that RPC. The check still starts 5 s
        # before the stop (0.5 s after the anchor) with its 4.5 s I/O budget. delivery_bound_step: the 12 s RPC
        # carries the cancellation; step 13 is emitted at that moment, so a first execution that posts step 14 or its
        # final effect exceeds the bound (C.6 M3; C2, C2w and C5)
        p = {"anchor_step": 4, "stop_after_anchor_s": 5.5, "precheck_lead_s": 5.0,
             "precheck_io_reserve_s": 0.5, "c1_stop_after_accepted_s": 15.0, "watch_s": 120.0,
             "claim_poll_every_s": 0.5, "claim_poll_until_s": 60.0,
             "read_offsets_s": [0, 2, 5, 10, 20, 30, 60, 90, 120], "capability_at_s": 112.0,
             "c6_server_delay_s": 1.0, "c6_twin_delay_s": 25.0, "c7_restart_s": 15.0, "c8_cancel_s": 3.0,
             "c8_restart_s": 20.0, "probe_obs_s": 60.0, "probe_read_every_s": 10.0, "probe_twin_timer_s": 30.0,
             "probe_twin_steps": 3, "delivery_bound_step": 14, "anchor_wait_s": 120.0}
    p.update({"precheck_margin_s": 0.1, "stage_w_latest_within_s": 1.5, "c6_precondition_poll_s": 0.2,
              "exit_wait_s": 15.0, "ready_timeout_s": 30.0, "server_ready_s": 60.0, "tick_every_s": 1.0,
              "n_steps": w["n_steps"], "step_s": w["step_s"], "timer_s": w["timer_s"],
              "hb_timeout_s": w["hb_timeout_s"], "fast": bool(fast)})
    # C.4 step 5, C.9: each end-of-watch capability job is a readiness job with no shared watch deadline (the shared
    # t_stop + watch - 0.1 s of every attempt so far left them about 7.9 s, and they timed out in 2 of the 4 attempts
    # that reached them).
    # ready_job without a deadline gives its client ready_timeout_s to see the run complete, the client process 20 s
    # more, and the `ready` effect 5 s after the client exits; the jobs run beside the last scheduled read, may end
    # after the watch, and the durability probe starts only once they have ended (capability_join_s bounds that wait)
    p["capability_join_s"] = p["ready_timeout_s"] + 20 + 5 + 5
    p["protocol_draft"] = 5
    p["precheck_policy"] = "fixed stop; parallel reads share I/O deadline; reap/validation before final gate"
    p["precheck_io_budget_s"] = p["precheck_lead_s"] - p["precheck_io_reserve_s"]
    if p["read_offsets_s"][-1] != p["watch_s"] or p["capability_at_s"] >= p["watch_s"]:
        raise ValueError("read schedule must end at the watch end, with the capability check inside it")
    return p


def planned_t_stop(spec, t_a, plan):
    """Draft 5 C.3: t_a + 5.5 s (FAST 0.75 s); C1 remains t_a + 15 s (FAST 3 s)."""
    return t_a + (plan["c1_stop_after_accepted_s"] if spec["anchor"] == "accepted" else plan["stop_after_anchor_s"])


def pre_check_start(planned, plan):
    """Draft 5 C.3: the pre-intervention check starts 5 s (FAST 0.75 s) before planned t_stop. In the step-anchored
    full conditions that is t_a + 0.5 s (FAST: t_a itself). In C1, t_a (the `accepted` effect) is 15 s before the
    stop, and right after it the history may not yet hold TimerStarted, nor the receiver the twin's `accepted` effect;
    starting there would make a correctly planted target look untested, and would check stage T 15 s before the stop
    instead of near it."""
    return planned - plan["precheck_lead_s"]


def gate_open(check_end, planned, margin):
    """The pre-intervention check must end before planned t_stop - margin (C.3); otherwise it is withheld."""
    return isinstance(check_end, (int, float)) and check_end < planned - margin


def pre_check_outcome(chk):
    """C.2, C.3: what the pre-intervention check decides. An overrun withholds the intervention and fails the
    trial. On time but with missing evidence, the intervention still runs (so the record shows what happened) and
    the target is untested. Returns {"withhold", "trial_failure", "untested"}."""
    if not chk.get("completed_before_deadline"):
        return {"withhold": True, "trial_failure": "pre-intervention check overran its deadline; intervention withheld",
                "untested": None}
    if not chk.get("ok"):
        return {"withhold": False, "trial_failure": None,
                "untested": "target (planting evidence: " + ", ".join(chk.get("failed") or ["unknown"]) + ")"}
    return {"withhold": False, "trial_failure": None, "untested": None}


def watch_schedule(t_stop, plan, cond):
    """C.4 step 5: the scheduled reads and the end-of-watch capability check, in time order (a read due at the same
    moment as the capability check goes first). PC skips the target queue, whose worker is dead by design."""
    events = [(t_stop + off, "read", off, None) for off in plan["read_offsets_s"]]
    events.append((t_stop + plan["capability_at_s"], "capability", None, ["twin"] if cond == "PC" else ["target", "twin"]))
    return sorted(events, key=lambda x: (x[0], x[1] != "read"))


def untested_after_watch(cond, box):
    """C.2, C.9: a failed C6 precondition makes C6's target untested (its SIGTERM still ran)."""
    if cond == "C6" and not (box.get("precondition") or {}).get("ok"):
        return "target (C6 precondition: CANCELED before the SIGTERM not observed)"
    return None


def ids_for(trial, cond):
    t = trial.lower()
    return {"target": {"workflow_id": f"expc-{t}-target", "task_queue": f"expc-target-{t}"},
            "twin": {"workflow_id": f"expc-{t}-twin", "task_queue": f"expc-twin-{t}"},
            "ptwin": {"workflow_id": f"expc-{t}-ptwin", "task_queue": f"expc-twin-{t}"}}


def job_specs(trial, spec, ids, plan, role):
    """The client spec for one JobC run (the target, the twin or the probe twin)."""
    fast = plan["fast"]
    if role == "ptwin":
        params = J.make_params(trial=trial, role="ptwin", variant="HB", fast=fast, receiver=RECEIVER,
                               timer_s=plan["probe_twin_timer_s"], n_steps=plan["probe_twin_steps"],
                               queue=ids["ptwin"]["task_queue"])
    else:   # the twin runs the same workflow, variant and cancellation type as the target (DESIGN-expC section 3)
        params = J.make_params(trial=trial, role=role, variant=spec["variant"], fast=fast, receiver=RECEIVER,
                               cancellation_type=spec["cancellation_type"], queue=ids[role]["task_queue"])
    return {"workflow": "JobC", "workflow_id": ids[role]["workflow_id"], "task_queue": ids[role]["task_queue"],
            "params": params}


# ============================ pure evidence logic (offline-tested) ============================
def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_role(e, role, ident):
    return (e.get("route") == "effect" and e.get("role") == role and e.get("workflow_id") == ident.get("workflow_id")
            and e.get("run_id") == ident.get("run_id"))


def first_execution(effects, ident):
    work = [e for e in effects if is_role(e, "target", ident) and e.get("activity_id") == "work"]
    if not work:
        return None, []
    nonce = min(work, key=lambda e: e["mono"]).get("exec_nonce")
    return nonce, sorted([e for e in work if e.get("exec_nonce") == nonce], key=lambda e: e["mono"])


def anchor_effect(effects, spec, ident, plan):
    """The anchor (C.3): the target's `accepted` effect (C1), or the first execution's step-k progress effect."""
    if spec["anchor"] == "accepted":
        hits = [e for e in effects if is_role(e, "target", ident) and e.get("kind") == "accepted"]
        return min(hits, key=lambda e: e["mono"]) if hits else None
    _, ex = first_execution(effects, ident)
    hits = [e for e in ex if e.get("kind") == "progress" and e.get("step") == plan["anchor_step"]]
    return hits[0] if hits else None


def stage_w_evidence(effects, ident, plan, t_check):
    """Stage W (C.2): steps 1..k of the first execution, the latest within 1.5 s of the check, and no final."""
    _, ex = first_execution(effects, ident)
    steps = {e.get("step") for e in ex if e.get("kind") == "progress"}
    want = set(range(1, plan["anchor_step"] + 1))
    latest = max((e["mono"] for e in ex), default=None)
    finals = [e for e in effects if is_role(e, "target", ident) and e.get("kind") == "final"]
    reasons = []
    if not want <= steps:
        reasons.append(f"missing steps {sorted(want - steps)}")
    if latest is None or t_check - latest > plan["stage_w_latest_within_s"]:
        reasons.append("latest progress effect older than the limit")
    if finals:
        reasons.append("a final effect already landed")
    return {"ok": not reasons, "reasons": reasons, "steps": sorted(s for s in steps if s is not None)}


def stage_t_evidence(history, effects, ident):
    """Stage T (C.2): TimerStarted and no TimerFired in the history, and no target progress effect."""
    if history is None:
        return {"ok": False, "reasons": ["history unreadable"]}
    types = [e["type"] for e in history]
    reasons = []
    if "TimerStarted" not in types:
        reasons.append("no TimerStarted")
    if "TimerFired" in types:
        reasons.append("TimerFired present")
    if any(is_role(e, "target", ident) and e.get("kind") == "progress" for e in effects):
        reasons.append("a progress effect already landed")
    return {"ok": not reasons, "reasons": reasons}


def pending_evidence(describe, history, variant):
    """C.2: HB/NH exactly one pending activity, STARTED, attempt 1; LA no MarkerRecorded for the work activity."""
    if describe is None:
        return {"ok": False, "reasons": ["describe unreadable"]}
    if describe["status"] != "RUNNING":
        return {"ok": False, "reasons": [f"status {describe['status']}"]}
    if variant == "LA":
        if history is None:
            return {"ok": False, "reasons": ["history unreadable"]}
        markers = [e for e in history if e["type"] == "MarkerRecorded"]
        return {"ok": not markers, "reasons": ["MarkerRecorded present"] if markers else []}
    p = describe["pending"]
    ok = len(p) == 1 and p[0].get("state") == "STARTED" and p[0].get("attempt") == 1
    return {"ok": ok, "reasons": [] if ok else [f"pending activities {[(x.get('state'), x.get('attempt')) for x in p]}"]}


def pre_check_decision(spec, ev):
    """Combine the planting evidence (C.2) into one decision. `ev` maps evidence names to {"ok": ...}; a missing
    item counts as failed. Returns {"ok", "failed"}."""
    need = ["accepted", "stage_a", "twin"]
    need += ["stage_t"] if spec["anchor"] == "accepted" else ["stage_w", "pending"]
    if spec["signalled"]:
        need.append("identity")
    failed = [k for k in need if not (isinstance(ev.get(k), dict) and ev[k].get("ok") is True)]
    return {"ok": not failed, "failed": failed, "required": need}


def twin_evidence(describe, history, effects, ident, in_timer):
    if describe is None or describe["status"] != "RUNNING":
        return {"ok": False, "reasons": ["twin not RUNNING or unreadable"]}
    if not any(is_role(e, "twin", ident) and e.get("kind") == "accepted" for e in effects):
        return {"ok": False, "reasons": ["no twin accepted effect"]}
    if in_timer:
        h = history or []
        types = [e["type"] for e in h]
        if history is None or "TimerStarted" not in types or "TimerFired" in types:
            return {"ok": False, "reasons": ["twin not in its timer"]}
    return {"ok": True, "reasons": []}


def c6_poll_done(read):
    """C.2: the C6 precondition polls stop once describe reads CANCELED and the history read in the same poll ends
    with a closing event. Describe and show run in parallel, so a poll can straddle the close; the closed history is
    the "before" side of C6's last-event-ID comparison across the restart (C.6)."""
    d, h = A.describe_of(read), A.history_of(read)
    return bool(d and d["status"] == "CANCELED" and h and h[-1]["type"] in A.CLOSING_EVENTS)


def c6_precondition(reads, t_sigterm):
    """C.2: describe reads CANCELED before the server SIGTERM."""
    for r in reads:
        d = A.describe_of(r)
        if d and d["status"] == "CANCELED" and A.call_end(r, "describe_target") < t_sigterm:
            return {"ok": True, "why": f"CANCELED at {r.get('label')}"}
    return {"ok": False, "why": "describe did not read CANCELED before the SIGTERM"}


def probe_precheck_decision(describes, ptwin_history):
    """C.4 step 6.2. describes: role -> normalised describe (or None). Returns the pre-probe check record.
    Running work is a STARTED pending activity on a run that is still open. A STARTED activity that a closed run
    still lists is recorded apart: whether a closed run lists its abandoned activity is unpredicted (C.8), so it
    is not taken as running work (the scorer adds the receiver's view, C.6)."""
    started = {role: [(p.get("activity_id"), p.get("attempt")) for p in d["pending"] if p.get("state") == "STARTED"]
               for role, d in describes.items() if d}
    wft_started = [role for role, d in describes.items()
                   if d and d.get("pending_wft") and d["pending_wft"].get("state") == "STARTED"]
    d = describes.get("ptwin")
    types = [e["type"] for e in ptwin_history or []]
    in_timer = bool(d and d["status"] == "RUNNING" and not d["pending"] and ptwin_history is not None and
                    "TimerStarted" in types and "TimerFired" not in types)
    return {"ok": in_timer, "ptwin_in_timer": in_timer, "started_activities": started,
            "workflow_task_started": wft_started,
            "interrupted": sorted(r for r, v in started.items() if v and describes[r]["status"] not in A.CLOSED),
            "closed_run_lists_started": sorted(r for r, v in started.items()
                                               if v and describes[r]["status"] in A.CLOSED),
            "unreadable": sorted(r for r, d in describes.items() if d is None)}


def is_trial_process(cmdline):
    """C.4 step 1, judged from the command line: a process of an Experiment C trial (a receiver, a worker or client
    of jobs_c.py, a CLI call naming an expc- ID, or a Temporal dev server, whose command line names neither), never
    the harness itself."""
    cmd = [str(c) for c in (cmdline or [])]
    joined = " ".join(cmd)
    if not cmd or "harness_c.py" in joined:
        return False
    if "jobs_c.py" in joined or "receiver_c.py" in joined or "expc-" in joined:
        return True
    return os.path.basename(cmd[0]) == "temporal" and "server" in cmd and "start-dev" in cmd


def trial_env(base=None):
    """The environment of every trial process and CLI call: the caller's, without any TEMPORAL_* variable, so no
    address, namespace, TLS, API-key, profile or config-file setting from the operator's shell reaches the CLI
    (connection options, `temporal options`) or the SDK processes (C.1)."""
    base = os.environ if base is None else base
    return {k: v for k, v in base.items() if not k.upper().startswith("TEMPORAL_")}


def identity_matches(launch, info):
    """C.1: before a signal, the PID's command line and start time must match the launch record."""
    if not (info and launch and isinstance(launch.get("start_ticks"), int) and launch["start_ticks"] > 0
            and launch.get("cmdline") and info.get("pid") == launch.get("pid")
            and info.get("start_ticks") == launch["start_ticks"] and info.get("cmdline") == launch["cmdline"]):
        return False
    return all(info.get(field) == launch[field] for field in ("netns_inode", "cgroup", "pgid", "sid", "exe")
               if launch.get(field) is not None)


def check_trial_clocks(rows):
    """C.9, across the trial: the mono/raw pace check (R6.13, common/clockguard.py) over the receiver's paired stamps,
    and the prospective wall/monotonic consistency check (clockwall_c.py, n8n/clockwall_b.py's rule) over the same
    rows. Matching mono/raw rates cannot exclude both clocks pausing while wall time advances; a wall failure fails the
    whole check. The scorer recomputes the wall rule from receiver.jsonl."""
    pace = clockguard.check_stamps([(row.get("mono"), row.get("raw")) for row in rows])
    wall = clockwall.check_receiver_rows(rows)
    pace["wall_clock"] = wall
    if not wall["ok"]:
        pace["ok"] = False
        pace.setdefault("reasons", []).extend("wall clock: " + reason for reason in wall["reasons"])
    return pace


def host_load(loadavg_path="/proc/loadavg", stat_path="/proc/stat"):
    """Our observer's own load (pre-freeze review; C.6 M3 diagnostic): the host's 1-, 5- and 15-minute load averages
    and its cumulative CPU and steal ticks (/proc/stat's first line; steal is its eighth value) at one moment. Each
    scheduled read runs four CLI processes beside the server and both workers, so a late step or heartbeat can be ours."""
    out = {"mono": time.monotonic()}
    try:
        with open(loadavg_path, encoding="ascii") as f:
            out["loadavg"] = [float(x) for x in f.read().split()[:3]]
    except (OSError, ValueError):
        out["loadavg"] = None
    try:
        with open(stat_path, encoding="ascii") as f:
            ticks = [int(x) for x in f.readline().split()[1:]]
        out["cpu_ticks"], out["steal_ticks"] = sum(ticks), (ticks[7] if len(ticks) > 7 else None)
    except (OSError, ValueError):
        out["cpu_ticks"] = out["steal_ticks"] = None
    return out


def listening_ports(proc_net_tcp_text):
    """Local ports in state LISTEN (0A) from /proc/net/tcp or tcp6 text."""
    out = set()
    for line in proc_net_tcp_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 3 and parts[3] == "0A":
            out.add(int(parts[1].rsplit(":", 1)[1], 16))
    return out


def exit_code(R):
    """0 completed; 3 ran with untested work (rerun needed); 2 error or void (C.10)."""
    if R.get("error") or R.get("void"):
        return 2
    return 3 if R.get("rerun_needed") else 0


def check_run_kind(run_kind, fast, manifest):
    """As n8n/harness_b.py: FAST is only ever a smoke run; a screen needs a frozen manifest."""
    if run_kind not in ("smoke", "pilot", "screen"):
        raise ValueError("STOPCHECK_RUN_KIND must be smoke, pilot or screen")
    if fast and run_kind != "smoke":
        raise ValueError("FAST timing can only be labelled smoke")
    if run_kind == "smoke" and not fast:
        raise ValueError("smoke requires STOPCHECK_FAST=1; full validation is pilot")
    if run_kind == "screen" and not manifest:
        raise ValueError("screen requires STOPCHECK_FROZEN_MANIFEST; use run_expC.py")


RECORD_FIELDS = ("schema_version", "experiment", "trial", "condition", "spec", "plan", "run_kind", "fast_mode",
                 "started_wall", "notes", "ids", "launches", "readiness", "submission", "anchor", "planned_t_stop",
                 "pre_check", "intervention", "claim_poll", "reads", "capability", "probe", "controls", "untested",
                 "rerun_needed", "error", "void", "host_profile", "host_exclusion", "predictions_at_start")


def new_record(cond, trial, run_kind, fast):
    spec = spec_of(cond)
    R = {k: None for k in RECORD_FIELDS}
    R.update({"schema_version": SCHEMA, "experiment": "C", "trial": trial, "condition": cond, "spec": spec,
              "plan": timing_plan(fast), "run_kind": run_kind, "fast_mode": bool(fast),
              "started_wall": time.strftime("%Y-%m-%dT%H:%M:%S"), "notes": [], "launches": [], "readiness": [],
              "claim_poll": [], "reads": [], "capability": [], "untested": [], "rerun_needed": False,
              "controls": {"ticks": {"sent": 0, "failed": 0, "failures": []}, "health": []},
              "host_profile": {"declared": HOST_PROFILE, "variable": "STOPCHECK_HOST_PROFILE"},
              "scope": f"exploratory single trial; {SCOPE.get(HOST_PROFILE, HOST_PROFILE)}; temporal server "
                       "start-dev (CLI 1.9.1, Server 1.32.0) with SQLite; temporalio 1.34.0; claims about this "
                       "configuration only"})
    return R


# ============================ host profile checks (C.0, C.1, C.9; item 6) ============================
def host_profile_refusal(profile=None):
    """Why this process may not run a trial under its declared host profile, or None (C.0: declared, never inferred)."""
    profile = HOST_PROFILE if profile is None else profile
    if profile not in HOST_PROFILES:
        return f"STOPCHECK_HOST_PROFILE must be one of {', '.join(HOST_PROFILES)}, not {profile!r}"
    return None


def _wsl_kernel():
    """True when the running kernel is WSL's (its release names Microsoft); None when unreadable."""
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return None


def host_checks():
    """The start-of-trial host precondition (C.9 disk floor), recorded as host_checks_start; a failure refuses the
    trial. wsl: unchanged, common/guards.py disk_ok at its 10 GB default (the Windows C: drive). linux-vm: the same
    10 GB free on each of VM_DISK_PATHS, where an unreadable one fails, and a kernel that reads as WSL (or cannot be
    read) fails, because the declared profile must describe the host."""
    floor = guards.DISK_FLOOR_GB_DEFAULT
    if HOST_PROFILE == "linux-vm":
        paths = {path: guards.c_drive_free_gb(path) for path in VM_DISK_PATHS}
        disk_ok = all(v is not None and v >= floor for v in paths.values())
        free = min((v for v in paths.values() if v is not None), default=None)
        report = {"host_profile": HOST_PROFILE, "violations": [],
                  "disk": {"ok": disk_ok, "free_gb": free, "floor_gb": floor, "paths": paths,
                           "measured": "statvfs of each path (guards.c_drive_free_gb(path))"}}
        if not disk_ok:
            report["violations"].append(f"disk floor: free GB {paths}, floor {floor} GB on each")
        wsl = _wsl_kernel()
        report["kernel"] = {"wsl_kernel": wsl, "ok": wsl is False}
        if wsl is not False:
            report["violations"].append(f"host profile linux-vm declared, but the kernel reads WSL: {wsl}")
    else:
        disk_ok, free = guards.disk_ok()
        report = {"host_profile": HOST_PROFILE, "violations": [],
                  "disk": {"ok": bool(disk_ok), "free_gb": free, "floor_gb": floor,
                           "measured": "guards.c_drive_free_gb, /mnt/c"}}
        if not disk_ok:
            report["violations"].append(f"disk below the floor: {free} GB")
    report["ok"] = not report["violations"]
    return report


def outbound_unreachable(address=METADATA_ADDRESS, timeout=1.0):
    """linux-vm isolation (C.1): from inside the trial's namespace, a TCP connection to the host's metadata address must
    fail with "network unreachable" (no route): a namespace with only loopback up has no route there. A connection, a
    refusal or a timeout means a route exists, so the namespace is not isolated from the host's network."""
    try:
        with socket.create_connection(address, timeout=timeout):
            return {"ok": False, "address": list(address), "result": "connected"}
    except OSError as e:
        return {"ok": e.errno == errno.ENETUNREACH, "address": list(address),
                "result": f"{type(e).__name__}: errno {e.errno}: {e}"[:200]}


def host_identity_record(batch_path, trial):
    """linux-vm: the worker's identity for this trial (GCE instance id and name, zone, machine type, CPU platform,
    image, scheduling), which the runner read from the metadata server outside the trial's namespace and wrote into
    this trial's batch entry; copied here into the record. A missing or unreadable entry is recorded as missing and
    never refuses the trial: it is provenance, not configuration. Not applicable on wsl."""
    if HOST_PROFILE != "linux-vm":
        return {"status": "not applicable", "host_profile": HOST_PROFILE}
    missing = {"status": "missing", "host_profile": HOST_PROFILE}
    if not batch_path:
        return dict(missing, why="no runner batch entry (STOPCHECK_BATCH unset: a direct harness run)")
    try:
        with open(batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        entries = [t for t in batch.get("trials") or [] if isinstance(t, dict) and t.get("trial") == trial]
    except (OSError, ValueError, AttributeError) as e:
        return dict(missing, why=f"runner batch unreadable: {type(e).__name__}: {e}"[:300])
    identity = entries[0].get("host_identity") if len(entries) == 1 else None
    if not isinstance(identity, dict):
        return dict(missing, why=f"the trial's batch entry carries no worker identity ({len(entries)} entries)")
    return dict(identity, copied_from=os.path.basename(batch_path))


def screen_freeze(runner, manifest, cond):
    """C.12: a screen trial checks the freeze against this host's declared profile (the freeze binds its validation
    pilots' profile)."""
    return runner.verify_freeze(manifest, [cond], run_checks=False, host_profile=HOST_PROFILE)


# ============================ one StopCheck trial per host (C.4 step 1, C.10; pre-freeze review) ====================
# A C trial never runs beside another StopCheck trial (Experiments A, B or C) on the same host: they share its ports,
# process table, clock and memory. The namespace wrapper holds a host flock from before the clean slate until the
# trial's namespace is deleted: Experiment C's own lock, so two C runners can never both pass the clean slate, and the
# n8n launcher's lock (n8n/harness_b_ns.sh at 7a3fcec), so a B trial cannot start beside a C trial and a C trial refuses
# while one runs. Experiment A's launcher takes no lock, so its trials are found by their processes. Then, inside the
# namespace and before anything starts, the trial refuses if any StopCheck process, network namespace, active systemd
# unit or fixed-port listener in the host's network namespace is present.
HOST_LOCK = "/run/lock/stopcheck-trial.lock"
B_LAUNCHER_LOCK = "/run/stopcheck-n8n.lock"
HOST_LOCKS = (HOST_LOCK, B_LAUNCHER_LOCK)
STOPCHECK_PROGRAMS = (                              # matched as the base name of any argument
    "harness_c.py", "run_expC.py", "jobs_c.py", "receiver_c.py", "mutate_c.py",                     # C
    "harness_b.py", "harness_b_ns.sh", "run_expB.py", "campaign_b.py", "canary_b.py", "mocks_b.py",  # B
    "feasibility3.py", "feasibility3_ns.sh", "spike.py", "spike2.py", "spike_ns.sh", "spike2_ns.sh",
    "stub_model.py", "reproduce_b.py", "n8n",
    "harness_d.py", "run_expA.py", "run_trial_ns.sh", "khan_probe.py")                               # A
STOPCHECK_PATH_SUFFIXES = ("langgraph/harness.py", "langgraph/canary.py", "langgraph/recorder.py")   # A's generic names
STOPCHECK_UNIT_PATTERNS = ("n8n-*", "stopcheck-*")         # harness_b.py's transient units (7a3fcec)
STOPCHECK_NETNS = re.compile(r"^(expc-|expb-|stopcheck)")  # C's, and harness_b_ns.sh's stopcheck-b-<pid>
OTHER_FIXED_PORTS = {"n8n": 5678, "B postgres": 5433, "B redis": 6380}


TERMINAL_MULTIPLEXERS = ("tmux", "tmux: server", "screen", "SCREEN")   # keep their launch argv; their children count


def stopcheck_markers(cmdline):
    """Why a command line belongs to a StopCheck trial or its services ([] if it does not). A terminal multiplexer that
    launched one keeps that command in its own argv; it is skipped, because the processes it started are found."""
    args = [str(a) for a in (cmdline or [])]
    if not args or os.path.basename(args[0]) in TERMINAL_MULTIPLEXERS:
        return []
    hits = sorted({os.path.basename(a) for a in args} & set(STOPCHECK_PROGRAMS))
    hits += [s for s in STOPCHECK_PATH_SUFFIXES if any(a.endswith("/" + s) or a == s for a in args)]
    prog = os.path.basename(args[0])
    if prog == "postgres" and any("stopcheck" in a for a in args):
        hits.append("postgres (B's data directory)")
    if prog == "redis-server" and str(OTHER_FIXED_PORTS["B redis"]) in args:
        hits.append("redis-server (B's port)")
    if is_trial_process(args) and not hits:
        hits.append("Experiment C trial process")
    return hits


def ancestors(pid=None, proc="/proc"):
    """This process and its ancestors (the namespace wrapper, the runner, their shells), whose command lines name
    StopCheck programs as data."""
    seen, pid = set(), os.getpid() if pid is None else pid
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            with open(f"{proc}/{pid}/stat", encoding="utf-8", errors="replace") as stream:
                pid = int(stream.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return seen


def stopcheck_processes(proc="/proc", mine=None):
    """Every running StopCheck process other than this trial's own process and its ancestors."""
    mine = ancestors(proc=proc) if mine is None else mine
    found = []
    for name in sorted(os.listdir(proc), key=lambda n: int(n) if n.isdigit() else -1):
        if not name.isdigit() or int(name) in mine:
            continue
        try:
            with open(os.path.join(proc, name, "cmdline"), "rb") as stream:
                args = [a.decode("utf-8", "replace") for a in stream.read().split(b"\0") if a]
        except OSError:
            continue
        hits = stopcheck_markers(args)
        if hits:
            found.append({"pid": int(name), "programs": hits, "cmdline": " ".join(args)[:300]})
    return found


def other_namespaces(own, root="/run/netns"):
    """StopCheck network namespaces other than this trial's own. No /run/netns directory means no named namespace; an
    unreadable one fails the check."""
    try:
        names = sorted(n for n in os.listdir(root) if STOPCHECK_NETNS.match(n) and n != own)
    except FileNotFoundError:
        return {"ok": True, "names": [], "note": f"{root} does not exist: no named network namespace"}
    except OSError as e:
        return {"ok": False, "names": None, "error": f"{type(e).__name__}: {e}"[:200]}
    return {"ok": True, "names": names}


def stopcheck_units(pid1=None, run=None):
    """Active StopCheck systemd units (B's n8n-main/-worker and stopcheck-cron units). With no systemd as PID 1 no unit
    can run; with systemd, an unreadable unit list fails the check."""
    if pid1 is None:
        try:
            with open("/proc/1/comm", encoding="utf-8") as f:
                pid1 = f.read().strip()
        except OSError:
            pid1 = None
    if pid1 != "systemd":
        return {"ok": True, "pid1": pid1, "active": [], "note": "PID 1 is not systemd: no systemd unit can be active"}
    run = run or subprocess.run
    try:
        r = run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager",
                 *STOPCHECK_UNIT_PATTERNS], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "pid1": pid1, "error": f"{type(e).__name__}: {e}"[:200]}
    if r.returncode:
        return {"ok": False, "pid1": pid1, "error": (r.stderr or "")[-300:]}
    units = [line.split() for line in r.stdout.splitlines() if line.strip()]
    active = [u[0] for u in units if len(u) >= 3 and u[2] not in ("inactive", "failed")]
    return {"ok": not active, "pid1": pid1, "listed": [" ".join(u[:4]) for u in units], "active": active}


def host_listeners(paths=("/proc/1/net/tcp", "/proc/1/net/tcp6")):
    """Listeners on Experiment C's or B's fixed ports in the host's network namespace (PID 1's, read from inside the
    trial's namespace)."""
    busy, errors = set(), []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                busy |= listening_ports(f.read())
        except OSError as e:
            errors.append(f"{path}: {type(e).__name__}")
    wanted = set(PORTS.values()) | set(OTHER_FIXED_PORTS.values())
    return {"ok": len(errors) < len(paths), "ports": sorted(busy & wanted), "errors": errors}


def lock_state(path, locks="/proc/locks"):
    """Which processes hold a lock on `path`, from /proc/locks (never takes the lock, never creates the file)."""
    try:
        st = os.stat(path)
    except OSError:
        return {"path": path, "exists": False, "pids": []}
    key = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    try:
        with open(locks, encoding="utf-8", errors="replace") as stream:
            lines = stream.read().splitlines()
    except OSError as e:
        return {"path": path, "exists": True, "pids": None, "error": f"{locks}: {type(e).__name__}"}
    pids = []
    for line in lines:   # "1: FLOCK  ADVISORY  WRITE <pid> <major hex>:<minor hex>:<inode> 0 EOF"
        fields = line.split()
        for i, field in enumerate(fields):
            m = re.fullmatch(r"([0-9a-f]+):([0-9a-f]+):(\d+)", field)
            if m and (int(m.group(1), 16), int(m.group(2), 16), int(m.group(3))) == key and fields[i - 1].isdigit():
                pids.append(int(fields[i - 1]))
    return {"path": path, "exists": True, "pids": pids}


def _exclusion_lock():
    """C.10: the namespace wrapper (this process's parent) holds every host lock."""
    wrapper = os.getppid()
    locks = []
    for path in HOST_LOCKS:
        state = lock_state(path)
        locks.append(dict(state, held_by_wrapper=wrapper in (state.get("pids") or [])))
    return {"ok": all(x["held_by_wrapper"] for x in locks), "wrapper_pid": wrapper, "locks": locks,
            "acquired": json.loads(os.environ.get("STOPCHECK_C_HOST_LOCKS") or "null")}


def n8n_install():
    """Provenance, not a check: whether this host has Experiment B's n8n install (a B campaign VM reused for C, which
    the exclusion check shows idle) or none (a clean image)."""
    found = sorted(glob_paths("/opt/n8n-*")) + [p for p in ("/var/lib/stopcheck-n8n",) if os.path.exists(p)]
    try:
        with open("/etc/passwd", encoding="utf-8") as f:
            user = any(line.startswith("n8ntest:") for line in f)
    except OSError:
        user = None
    installed = bool(found or user)
    return {"installed": installed, "paths": found, "n8ntest_user": user,
            "origin": "B's n8n install is present (a B campaign host; the exclusion check shows whether it is idle)"
            if installed else "no n8n install found (a clean image)"}


def glob_paths(pattern):
    import glob
    return glob.glob(pattern)


def host_exclusion(own_netns):
    """C.4 step 1, C.10 (pre-freeze review): this trial is the only StopCheck trial on the host. Recorded as
    host_exclusion; a failure refuses the trial before anything starts."""
    rec = {"lock": _exclusion_lock(), "processes": stopcheck_processes(), "namespaces": other_namespaces(own_netns),
           "units": stopcheck_units(), "listeners": host_listeners(), "host_origin": n8n_install()}
    rec["ok"] = bool(rec["lock"].get("ok") and not rec["processes"] and rec["namespaces"].get("ok")
                     and not rec["namespaces"].get("names") and rec["units"].get("ok")
                     and rec["listeners"].get("ok") and not rec["listeners"].get("ports"))
    return rec


def acquire_host_locks(paths=HOST_LOCKS):
    """Take an exclusive, non-blocking flock on each host lock, held by the namespace wrapper until the trial's namespace
    is deleted (C.10). Experiment C's own lock file gets this process's PID. Returns (handles, record); a lock another
    process holds refuses the trial (SystemExit) and releases the ones already taken."""
    import fcntl
    handles, rec = [], {"pid": os.getpid(), "locks": []}
    for path in paths:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handle = open(path, "a+", encoding="utf-8")
        except OSError as e:
            release_host_locks(handles)
            raise SystemExit(f"cannot open the host lock {path}: {e}")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            release_host_locks(handles)
            raise SystemExit(f"another StopCheck trial holds the host lock {path}: trials never run at the same time "
                             "on one host (C.10)")
        if path == paths[0]:
            handle.truncate(0)
            handle.write(f"{os.getpid()}\n")
            handle.flush()
        handles.append(handle)
        rec["locks"].append({"path": path, "acquired_mono": now()})
    return handles, rec


def release_host_locks(handles):
    import fcntl
    for handle in handles:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


PREDICTIONS = os.environ.get("STOPCHECK_C_PREDICTIONS") or os.path.join(ROOT, "predictions", "temporal-expC-cells.json")


def predictions_receipt(path=None):
    """C.8, C.10 (pre-freeze review): the predictions file's SHA-256 when the trial starts, before anything runs, so the
    freeze can show that the predictions were fixed before this run's outcome existed (or the predictions must name the
    run as prior exposure)."""
    path = path or PREDICTIONS
    rec = {"path": os.path.relpath(path, ROOT) if os.path.abspath(path).startswith(ROOT + os.sep) else path,
           "read_wall": time.strftime("%Y-%m-%dT%H:%M:%S"), "read_mono": now()}
    try:
        rec.update(sha256=sha256_file(path), exists=True)
    except FileNotFoundError:
        rec.update(sha256=None, exists=False)
    except OSError as e:
        rec.update(sha256=None, exists=None, error=f"{type(e).__name__}: {e}"[:200])
    return rec


# ============================ processes (C.1) ============================
def proc_info(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            stat = f.read()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = [c.decode(errors="replace") for c in f.read().split(b"\0") if c]
        net = os.stat(f"/proc/{pid}/ns/net").st_ino
    except OSError:
        return None
    rest = stat[stat.rindex(")") + 2:].split()
    info = {"pid": pid, "start_ticks": int(rest[19]), "cmdline": cmd, "state": rest[0], "netns_inode": net,
            "pgid": int(rest[2]), "sid": int(rest[3])}
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as stream:
            info["cgroup"] = stream.read().strip()
        info["exe"] = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        info["cgroup"], info["exe"] = None, None
    return info


class Procs:
    """Launch records, identity checks and signals for every trial process."""

    def __init__(self, out_dir, record):
        self.out, self.R, self.p, self.lock = out_dir, record, {}, threading.Lock()
        self.closed = False

    def start(self, name, cmd, log_name):
        with self.lock:
            if self.closed:
                raise RuntimeError(f"refusing to start {name}: clean-up has begun")
            old = self.p.get(name)
            if old and old["popen"].poll() is None:
                raise RuntimeError(f"refusing to replace live process {name}")
            replaced = old["launch"] if old else None
            if old:
                old["log"].close()
                self.R["launches"].append({"name": name, "replaced_pid": old["launch"]["pid"],
                                           "exit_status": old["popen"].returncode})
        f = open(os.path.join(self.out, log_name), "ab")
        t0 = now()
        pop = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True, cwd=self.out,
                               env=trial_env())
        info = proc_info(pop.pid) or {"pid": pop.pid}
        launch = {"name": name, "cmd": cmd, "t_start": t0, "wall": time.time(), "pid": pop.pid,
                  "start_ticks": info.get("start_ticks"), "cmdline": info.get("cmdline"),
                  "netns_inode": info.get("netns_inode"), "log": log_name}
        with self.lock:
            self.p[name] = {"popen": pop, "launch": launch, "log": f}
            self.R["launches"].append(launch)
            late = self.closed           # clean-up began while this process was being started
        if late:
            os.killpg(pop.pid, signal.SIGKILL)
            pop.wait(timeout=5)
            raise RuntimeError(f"{name} started while clean-up began; killed")
        # /proc/<pid>/cmdline can briefly be empty during exec. Never freeze that transient value,
        # and never repair a signal-time mismatch by adopting the process that happens to be there.
        # Bind the same owned PID/start_ticks to the exact requested argv before readiness begins.
        launch["identity_samples"] = []
        deadline = now() + 2.0
        start_ticks = info.get("start_ticks")
        while True:
            launch["identity_samples"].append(info)
            if pop.poll() is not None:
                launch["identity_error"] = "process exited before launch identity was verified"
                raise RuntimeError(f"{name} launch identity failed: {launch['identity_error']}")
            if info and info.get("pid") != pop.pid:
                launch["identity_error"] = "PID differs from owned child"
                raise RuntimeError(f"{name} launch identity failed: {launch['identity_error']}")
            ticks = (info or {}).get("start_ticks")
            if start_ticks is None and isinstance(ticks, int) and ticks > 0:
                start_ticks = ticks
            if ticks is not None and ticks != start_ticks:
                launch["identity_error"] = "start_ticks changed while waiting for exec identity"
                raise RuntimeError(f"{name} launch identity failed: {launch['identity_error']}")
            if info and isinstance(start_ticks, int) and start_ticks > 0 and info.get("cmdline") == list(cmd):
                for field in ("start_ticks", "cmdline", "netns_inode", "cgroup", "pgid", "sid", "exe"):
                    launch[field] = info.get(field)
                launch["identity_verified_mono"] = now()
                break
            if now() >= deadline:
                launch["identity_error"] = "exact requested command line not observed before deadline"
                raise RuntimeError(f"{name} launch identity failed: {launch['identity_error']}")
            time.sleep(0.01)
            info = proc_info(pop.pid)
        if replaced is not None:
            # C.1: a restarted process must have a new PID and start time (pre-freeze review)
            same = (launch.get("pid"), launch.get("start_ticks")) == (replaced.get("pid"), replaced.get("start_ticks"))
            launch["replaces"] = {"pid": replaced.get("pid"), "start_ticks": replaced.get("start_ticks"),
                                  "new_identity": not same}
            if same:
                raise RuntimeError(f"restarted {name} has the replaced process's PID and start time (C.1)")
        return launch

    def alive(self, name):
        x = self.p.get(name)
        return bool(x and x["popen"].poll() is None)

    def signal(self, name, sig, wait_s, action):
        """Identity check, then the signal, then wait for the exit; stamps go into `action` (C.1, C.3)."""
        x = self.p.get(name)
        if not x:
            raise RuntimeError(f"no launch record for {name}")
        launch = x["launch"]
        info = proc_info(launch["pid"])
        action.update(pid=launch["pid"], identity_ok=identity_matches(launch, info), identity_observed=info)
        if not action["identity_ok"]:
            self.R.setdefault("identity_failures", []).append({"name": name, "signal": sig,
                                                               "action": dict(action)})
            raise RuntimeError(f"identity mismatch before signalling {name}; signal withheld")
        action["t_send"] = now()
        action["raw_send"] = time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
        os.kill(launch["pid"], sig)
        action["t_return"] = now()
        try:
            action["exit_status"] = x["popen"].wait(timeout=wait_s)
            action["exit_mono"] = now()
        except subprocess.TimeoutExpired:
            action["exit_status"] = None
            action["exit_mono"] = None
        action["rc"] = 0
        return action

    def stop_all(self, errors):
        with self.lock:
            self.closed = True           # no process may start after this (an intervention thread may still run)
        for name, x in list(self.p.items()):
            if x["popen"].poll() is None:
                try:
                    os.killpg(x["popen"].pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = now() + 10
        for name, x in list(self.p.items()):
            try:
                x["popen"].wait(timeout=max(0.1, deadline - now()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(x["popen"].pid, signal.SIGKILL)
                    x["popen"].wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired) as e:
                    errors.append(f"{name}: {type(e).__name__}")
            x["log"].close()


# ============================ the observer (CLI) and the receiver ============================
def network_interfaces(path="/proc/self/net/dev"):
    """Read this process's network namespace, independent of the sysfs mount's namespace."""
    try:
        with open(path, encoding="ascii", errors="replace") as stream:
            return sorted(line.split(":", 1)[0].strip() for line in stream if ":" in line)
    except OSError:
        return None


def loopback_up():
    """Query IFF_UP on a socket created in this process's network namespace."""
    try:
        import fcntl
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            flags = fcntl.ioctl(sock.fileno(), 0x8913, struct.pack("16sH22x", b"lo", 0))
        return bool(struct.unpack_from("16sH", flags)[1] & 0x1)
    except (ImportError, OSError):
        return None


def cli_argv(args, command_timeout_s=CLI_TIMEOUT_S):
    """C.1: every observer call names the address, the namespace, JSON output and the 5 s command timeout, and turns
    off the CLI's config file and environment configuration (global options listed by `temporal options`, T20).
    With trial_env(), which drops every TEMPORAL_* variable, no shell or file setting can redirect or alter a read."""
    return [CLI, *args, "--address", ADDRESS, "--namespace", "default", "--output", "json",
            "--command-timeout", f"{command_timeout_s:g}s", "--disable-config-file", "--disable-config-env"]


CLI_REPLY_FIELDS = ("argv", "argv_full", "stdout", "stderr", "json")   # what an action keeps of its CLI call (C.3)


def _text(value):
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


def cli(args, timeout=CLI_TIMEOUT_S + 3, deadline=None):
    """One CLI call. The record keeps the exact command typed (argv_full: cli_argv's list, with the address, namespace,
    command timeout and output flags), the raw stdout and the full stderr, and the parsed JSON beside them (C.3, C.4:
    the raw CLI output is kept; pre-freeze review). stderr_tail stays for the scorer's NotFound test."""
    out = {"argv": args, "t_start": now()}
    remaining = deadline - out["t_start"] if deadline is not None else timeout
    if remaining <= 0:
        out.update(rc=None, timed_out=True, deadline_exhausted=True, json=None, stderr_tail="deadline exhausted",
                   argv_full=None, stdout=None, stderr=None, t_end=now(), wall_end=time.time())
        return out
    timeout = min(timeout, remaining)
    argv = cli_argv(args, min(CLI_TIMEOUT_S, timeout))
    out["argv_full"] = list(argv)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=trial_env())
        out.update(rc=p.returncode, timed_out=False, stdout=_text(p.stdout), stderr=_text(p.stderr))
    except subprocess.TimeoutExpired as e:
        out.update(rc=None, timed_out=True, stdout=_text(e.stdout), stderr=_text(e.stderr))
    out["t_end"], out["wall_end"] = now(), time.time()
    out["json"] = parse_json_output(out["stdout"]) if out.get("rc") == 0 else None
    if out["rc"] == 0 and out["json"] is None:
        out["parse_error"] = True
    out["stderr_tail"] = (out["stderr"][-590:] + (" [timeout]" if out["timed_out"] else "")).strip()
    return out


def cli_reply(res):
    """The typed command and Temporal's reply of one CLI call, as an action keeps them (C.3)."""
    return {k: res.get(k) for k in CLI_REPLY_FIELDS}


def parse_json_output(text):
    """One JSON document, or JSON lines (a list)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        try:
            return [json.loads(x) for x in text.splitlines() if x.strip()]
        except ValueError:
            return None


def describe_args(ident):
    return ["workflow", "describe", "-w", ident["workflow_id"], "-r", ident["run_id"], "--raw"]


def show_args(ident):
    return ["workflow", "show", "-w", ident["workflow_id"], "-r", ident["run_id"]]


def list_args(ident):
    return ["workflow", "list", "--query", f'WorkflowId="{ident["workflow_id"]}"']


def read_parts(label, kind, parts, offset=None, parallel=False, deadline=None):
    r = {"label": label, "kind": kind, "offset": offset, "t_start": now(), "parts": {}}
    if deadline is not None:
        r["deadline_mono"] = deadline
    def call(args):
        return cli(args, deadline=deadline) if deadline is not None else cli(args)
    if parallel:
        res = {}
        ths = [threading.Thread(target=lambda n=n, a=a: res.__setitem__(n, call(a))) for n, a in parts]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        r["parts"] = {n: res.get(n) for n, _ in parts}
    else:
        for n, a in parts:
            r["parts"][n] = call(a)
    r["t_end"], r["wall_end"] = now(), time.time()
    return r


class ReceiverLog:
    """Incremental reader of receiver.jsonl (never hides a malformed complete line)."""

    def __init__(self, path):
        self.path, self.pos, self.rows, self.lock = path, 0, [], threading.Lock()

    def poll(self):
        with self.lock:
            try:
                with open(self.path, "rb") as f:
                    f.seek(self.pos)
                    data = f.read()
            except FileNotFoundError:
                return self.rows
            end = data.rfind(b"\n")
            if end >= 0:
                for line in data[:end + 1].splitlines():
                    if line.strip():
                        self.rows.append(json.loads(line))
                self.pos += end + 1
            return self.rows

    def effects(self):
        return [x for x in self.poll() if x.get("route") == "effect" and not x.get("bad")]


def http_get(path, timeout=2.0):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", PORTS["receiver"], timeout=timeout)
    try:
        c.request("GET", path)
        return c.getresponse().status
    finally:
        c.close()


def http_post_effect(rec, timeout=1.0):
    import http.client
    body = json.dumps(rec).encode()
    c = http.client.HTTPConnection("127.0.0.1", PORTS["receiver"], timeout=timeout)
    try:
        c.request("POST", "/effect", body=body, headers={"Content-Type": "application/json"})
        return c.getresponse().status
    finally:
        c.close()


class Ticks(threading.Thread):
    """The tick canary (DESIGN-expC section 3): one `tick` effect a second."""

    def __init__(self, R, trial, every):
        super().__init__(daemon=True, name="ticks")
        self.R, self.trial, self.every, self.stop = R, trial, every, threading.Event()

    def run(self):
        n, stats = 0, self.R["controls"]["ticks"]
        while not self.stop.is_set():
            n += 1
            rec = {"trial": self.trial, "role": "tick", "kind": "tick", "n": n, "smono": now(), "swall": time.time()}
            try:
                ok = http_post_effect(rec) == 200
            except OSError:
                ok = False
            stats["sent"] += 1
            if not ok:
                stats["failed"] += 1
                stats["failures"].append(round(now(), 3))
            self.stop.wait(self.every)


# ============================ the trial ============================
class TrialRun:
    def __init__(self, cond, trial, out_dir, run_kind, fast):
        self.cond, self.trial, self.out, self.fast = cond, trial, out_dir, fast
        self.R = new_record(cond, trial, run_kind, fast)
        self.spec, self.plan = self.R["spec"], self.R["plan"]
        self.ids = ids_for(trial, cond)
        self.R["ids"] = self.ids
        self.procs = Procs(out_dir, self.R)
        self.rx = ReceiverLog(os.path.join(out_dir, "receiver.jsonl"))
        self.db = os.path.join(out_dir, "temporal.db")
        self.gen = {"target": 0, "twin": 0, "server": 0}
        self.ready_n = 0
        self.ready_lock = threading.Lock()
        self.outages = []

    # ---- commands (C.1) ----
    def server_cmd(self):
        return [CLI, "server", "start-dev", "--db-filename", self.db, "--ip", "127.0.0.1",
                "--port", str(PORTS["grpc"]), "--http-port", str(PORTS["http"]), "--metrics-port", str(PORTS["metrics"]),
                "--headless", "--log-format", "json"]

    def start_server(self):
        self.gen["server"] += 1
        g = self.gen["server"]
        launch = self.procs.start("server", self.server_cmd(), f"server-g{g}.log")
        ok = self.wait_server_ready()
        self.R["readiness"].append({"what": f"server-g{g}", "ok": ok, "t_ready": now()})
        if not ok:
            raise RuntimeError("server never became ready")
        return launch

    def wait_server_ready(self):
        deadline = now() + self.plan["server_ready_s"]
        while now() < deadline:
            if cli(["operator", "cluster", "health"])["rc"] == 0 and cli(["workflow", "list", "--limit", "1"])["rc"] == 0:
                return True
            time.sleep(0.5)
        return False

    def start_worker(self, role):
        self.gen[role] += 1
        g = self.gen[role]
        queue = self.ids[role]["task_queue"]
        cmd = [PY, "-B", os.path.join(HERE, "jobs_c.py"), "worker", "--address", ADDRESS, "--task-queue", queue,
               "--identity", f"{role}-{self.trial.lower()}-g{g}",
               "--log", os.path.join(self.out, f"worker-{role}-g{g}.jsonl")]
        launch = self.procs.start(f"worker-{role}", cmd, f"worker-{role}-g{g}.out")
        cap = self.ready_job(role, f"ready-{role}-g{g}")
        self.R["readiness"].append({"what": f"worker-{role}-g{g}", "ok": cap["ok"], "t_ready": now(), "job": cap})
        if not cap["ok"]:
            raise RuntimeError(f"worker {role} generation {g} never became ready")
        return launch, cap

    def ready_job(self, role, label, deadline=None):
        """A readiness or capability job (ReadyC) on this role's queue; ok only if the run completed and its
        `ready` effect reached the receiver (C.1)."""
        with self.ready_lock:
            self.ready_n += 1
            ready_n = self.ready_n
        queue = self.ids[role]["task_queue"]
        wf = f"expc-{self.trial.lower()}-{label}-{ready_n}"
        params = J.make_params(trial=self.trial, role="ready", variant="HB", fast=self.fast, receiver=RECEIVER,
                               queue=queue)
        t0 = now()
        out = {"label": label, "queue": queue, "workflow_id": wf, "t_start": t0}
        if deadline is not None:
            out["deadline_mono"] = deadline
        try:
            remaining = deadline - now() if deadline is not None else self.plan["ready_timeout_s"] + 20
            if remaining <= 0:
                raise subprocess.TimeoutExpired(["ready"], 0)
            client_timeout = min(self.plan["ready_timeout_s"], max(0.001, remaining - 0.05))
            p = subprocess.run([PY, "-B", os.path.join(HERE, "jobs_c.py"), "ready", "--address", ADDRESS,
                                "--task-queue", queue, "--workflow-id", wf, "--params", json.dumps(params),
                                "--timeout", str(client_timeout)],
                               capture_output=True, text=True, timeout=remaining,
                               env=trial_env())
            out["client"] = parse_json_output(p.stdout)
            out["rc"] = p.returncode
            out["stderr_tail"] = p.stderr[-400:]
        except subprocess.TimeoutExpired:
            out.update(rc=None, client=None, stderr_tail="timeout")
        seen = False
        effect_deadline = min(now() + 5, deadline) if deadline is not None else now() + 5
        while now() < effect_deadline and not seen:
            seen = any(e.get("kind") == "ready" and e.get("workflow_id") == wf for e in self.rx.effects())
            if not seen:
                time.sleep(min(0.1, max(0.0, effect_deadline - now())))
        out.update(effect_seen=seen, ok=bool(out.get("rc") == 0 and (out.get("client") or {}).get("ok") and seen),
                   t_end=now())
        with self.ready_lock:
            self.R.setdefault("capability_all", []).append(out)
        return out

    def watch_capability(self, roles, deadline=None):
        """The end-of-watch capability jobs (C.4 step 5), on the given queues at once. Each is a readiness job with that
        job's own bound (ready_job without a deadline; timing_plan, capability_join_s); there is no shared deadline at
        the end of the watch. A failed job is failed evidence (C.9), never a reason to extend the declared watch."""
        results = {}

        def one(role):
            try:
                results[role] = self.ready_job(role, f"cap-watch-{role}", deadline=deadline)
            except Exception as error:  # noqa: BLE001 - recorded as a failed control
                results[role] = {"label": f"cap-watch-{role}", "queue": self.ids[role]["task_queue"],
                                 "ok": False, "error": f"{type(error).__name__}: {error}",
                                 "deadline_mono": deadline, "t_end": now()}
        threads = [threading.Thread(target=one, args=(role,)) for role in roles]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return [results[role] for role in roles]

    def _capability_into_record(self, roles, watch_end):
        window = {"start_mono": now(), "roles": list(roles)}
        self.R["capability_window"] = window
        results = self.watch_capability(roles)
        for c in results:
            c["ended_after_watch_end"] = bool(_is_num(c.get("t_end")) and c["t_end"] > watch_end)
        self.R["capability"] = results
        window["end_mono"] = now()

    def watch(self, t_stop, schedule):
        """C.4 step 5: the scheduled reads and the end-of-watch capability jobs, in time order. The capability jobs run
        in their own thread, so they never delay a scheduled read; finish_capability waits for them before the
        durability probe. Returns that thread (None if the schedule has no capability event)."""
        cap_thread = None
        watch_end = t_stop + self.plan["watch_s"]
        for index, (t_ev, what, off, roles) in enumerate(schedule):
            while now() < t_ev:
                time.sleep(0.02)
            if what == "read":
                # Do not let a stalled earlier read consume the next scheduled read's budget.
                next_due = schedule[index + 1][0] if index + 1 < len(schedule) else t_ev + CLI_TIMEOUT_S
                self.R["reads"].append(self.scheduled_read(f"+{off:g}", off,
                                                           deadline=min(t_ev + CLI_TIMEOUT_S, next_due)))
            else:
                cap_thread = threading.Thread(target=self._capability_into_record, args=(roles, watch_end),
                                              daemon=True, name="capability")
                cap_thread.start()
        return cap_thread

    def finish_capability(self, thread):
        """Wait for the end-of-watch capability jobs, within their bound (capability_join_s), before the probe."""
        if thread is None:
            return
        thread.join(self.plan["capability_join_s"])
        if thread.is_alive():
            raise RuntimeError("the end-of-watch capability jobs did not end within their bound")

    def submit(self, roles):
        specs = [job_specs(self.trial, self.spec, self.ids, self.plan, r) for r in roles]
        t0 = now()
        p = subprocess.run([PY, "-B", os.path.join(HERE, "jobs_c.py"), "start", "--address", ADDRESS,
                            "--spec", json.dumps(specs)], capture_output=True, text=True, timeout=60,
                           env=trial_env())
        out = parse_json_output(p.stdout) or {}
        rec = {"roles": roles, "t_start": t0, "t_end": now(), "rc": p.returncode, "reply": out,
               "stderr_tail": p.stderr[-400:], "params": {s["params"]["role"]: s["params"] for s in specs}}
        for s in out.get("started") or []:
            role = s.get("role")
            if s.get("ok") and role in self.ids:
                self.ids[role]["run_id"] = s.get("run_id")
        if p.returncode != 0 or any(not self.ids[r].get("run_id") for r in roles):
            raise RuntimeError(f"submission failed for {roles}: {out or p.stderr[-300:]}")
        return rec

    def health(self, label, record=True, deadline=None):
        """One /health check. With record=True it is one of the C.9 controls (start, before t_stop, end of watch,
        end of probe) and is written to controls.health, which the scorer reads by label."""
        try:
            remaining = deadline - now() if deadline is not None else 2.0
            ok = remaining > 0 and http_get("/health", timeout=min(2.0, remaining)) == 200
        except OSError:
            ok = False
        if record:
            self.R["controls"]["health"].append({"label": label, "ok": ok, "mono": now()})
        return ok

    def wait_receiver(self, limit_s=15.0, every_s=0.2):
        """C.1 readiness of the receiver: poll /health until it answers 200. The polls before it is up are readiness,
        not the C.9 check, so one "start" entry is recorded, with the number of polls it took."""
        deadline, polls = now() + limit_s, 0
        while True:
            polls += 1
            ok = self.health("start", record=False)
            if ok or now() >= deadline:
                break
            time.sleep(every_s)
        self.R["controls"]["health"].append({"label": "start", "ok": ok, "mono": now(), "polls": polls})
        return ok

    # ---- reads (C.4) ----
    def watch_parts(self):
        t, tw = self.ids["target"], self.ids["twin"]
        return [("describe_target", describe_args(t)), ("describe_twin", describe_args(tw)),
                ("show_target", show_args(t)), ("list_target", list_args(t))]

    def scheduled_read(self, label, offset=None, kind="scheduled", deadline=None):
        r = read_parts(label, kind, self.watch_parts(), offset, parallel=True, deadline=deadline)
        r["during_declared_outage"] = self.outage_open()
        r["host_load"] = host_load()          # our observer's own load at this read (diagnostic, C.6 M3)
        return r

    def outage_open(self):
        return any(o.get("t_to") is None for o in self.outages)

    # ---- pre-intervention check (C.2, C.3) ----
    def pre_check(self, planned):
        chk = {"start_mono": now(), "planned_t_stop": planned,
               "io_deadline_mono": planned - self.plan["precheck_io_reserve_s"],
               "validation_deadline_mono": planned - self.plan["precheck_margin_s"]}
        t, tw = self.ids["target"], self.ids["twin"]
        parts = [("describe_target", describe_args(t)), ("show_target", show_args(t)),
                 ("describe_twin", describe_args(tw))]
        if self.cond == "C6":
            parts.append(("show_twin", show_args(tw)))
        r = read_parts("pre", "pre", parts, parallel=True,
                       deadline=chk["io_deadline_mono"])
        chk["read"] = r
        eff = self.rx.effects()
        d, h = A.describe_of(r), A.history_of(r)
        dt, ht = A.describe_of(r, "describe_twin"), A.history_of(r, "show_twin") if self.cond == "C6" else None
        ev = {"accepted": {"ok": bool(d and d["status"] == "RUNNING" and h and h[0]["type"] == "WorkflowExecutionStarted")},
              "stage_a": {"ok": any(is_role(e, "target", t) and e.get("kind") == "accepted" for e in eff)},
              "twin": twin_evidence(dt, ht, eff, tw, in_timer=self.cond == "C6")}
        if self.spec["anchor"] == "accepted":
            ev["stage_t"] = stage_t_evidence(h, eff, t)
        else:
            ev["stage_w"] = stage_w_evidence(eff, t, self.plan, now())
            ev["pending"] = pending_evidence(d, h, self.spec["variant"])
        if self.spec["signalled"]:
            name = "server" if self.spec["signalled"] == "server" else "worker-target"
            x = self.procs.p.get(name)
            ev["identity"] = {"ok": bool(x and identity_matches(x["launch"], proc_info(x["launch"]["pid"])))}
        chk["evidence"] = ev
        chk.update(pre_check_decision(self.spec, ev))
        chk["end_mono"] = now()
        chk["completed_before_deadline"] = gate_open(chk["end_mono"], planned, self.plan["precheck_margin_s"])
        chk["host_load"] = host_load()        # after the gate: a diagnostic never delays the decision
        return chk

    # ---- interventions (C.3), run in their own thread ----
    def stamp(self, box, key):
        if key == "t_stop" and key in box:
            return
        box[key] = {"mono": now(), "raw": time.clock_gettime(time.CLOCK_MONOTONIC_RAW)}
        if key == "t_stop":
            box["_started"].set()

    def request(self, box, name):
        t = self.ids["target"]
        args = ["workflow", "cancel", "-w", t["workflow_id"], "-r", t["run_id"]] if name == "cancel" else \
            ["workflow", "terminate", "-w", t["workflow_id"], "-r", t["run_id"], "--reason", f"expc-{self.trial}"]
        self.stamp(box, "t_stop")
        a = {"name": name, "t_send": now()}
        res = cli(args)
        a.update(t_return=now(), rc=res["rc"], timed_out=res["timed_out"], stderr_tail=res["stderr_tail"],
                 cli_t_start=res["t_start"], cli_t_end=res["t_end"])
        # C.3: what the operator typed and what Temporal answered (exit code, stdout, stderr), kept whole
        a.update(cli_reply(res))
        box["actions"].append(a)
        box["_first_return"].set()
        box["_request_returned"].set()
        if a["rc"] != 0 or a["timed_out"]:
            raise RuntimeError(f"failed request: {name} rc={a['rc']} timed_out={a['timed_out']}")
        return a

    def kill_target_worker(self, box):
        self.stamp(box, "t_stop")
        a = {"name": "sigkill_worker", "signal": "SIGKILL"}
        try:
            self.procs.signal("worker-target", signal.SIGKILL, self.plan["exit_wait_s"], a)
        finally:
            box["actions"].append(a)
            box["_first_return"].set()
        if a.get("exit_mono") is None:
            raise RuntimeError("target worker exit not observed after SIGKILL")
        box["worker_dead"].append({"role": "target", "t_from": a["exit_mono"], "t_to": None})
        return a

    def restart_target_worker(self, box, at):
        self._sleep_until(at, box)
        rs = {"what": "worker:target", "t_start": now()}
        box["restarts"].append(rs)
        for w in box["worker_dead"]:
            if w["t_to"] is None:
                w["t_to"] = rs["t_start"]
        launch, cap = self.start_worker("target")
        rs.update(t_ready=now(), ok=cap["ok"], generation=self.gen["target"], launch_pid=launch["pid"],
                  capability=[cap])

    def server_restart(self, box, at, reason):
        """SIGTERM the server, wait for its exit (SIGKILL after 15 s, a deviation), start it again on the same
        database, then capability jobs on both queues (C.3)."""
        self._sleep_until(at, box)
        a = {"name": "sigterm_server", "signal": "SIGTERM", "reason": reason}
        outage = {"kind": "server", "t_from": None, "t_to": None, "reason": reason}
        box["outages"].append(outage)
        self.outages.append(outage)
        try:
            self.procs.signal("server", signal.SIGTERM, self.plan["exit_wait_s"], a)
            outage["t_from"] = a["t_send"]
            if a.get("exit_mono") is None:
                k = {"name": "sigkill_server", "signal": "SIGKILL", "deviation": "server did not exit within 15 s"}
                self.procs.signal("server", signal.SIGKILL, 5, k)
                box["actions"].append(k)
                self.R["notes"].append("deviation: server SIGKILLed after SIGTERM timeout")
        finally:
            box["actions"].append(a)
        rs = {"what": "server", "t_start": now()}
        box["restarts"].append(rs)
        self.start_server()
        outage["t_to"] = now()
        rs["t_ready"] = outage["t_to"]
        rs["capability"] = [self.ready_job("target", "cap-after-server-target"),
                            self.ready_job("twin", "cap-after-server-twin")]
        rs["ok"] = all(c["ok"] for c in rs["capability"])

    def _sleep_until(self, t, box):
        while now() < t:
            if box["_cancel"].wait(min(0.05, max(0.0, t - now()))):
                raise RuntimeError("intervention cancelled")

    def runner(self, box):
        proc, plan = self.spec["procedure"], self.plan
        if proc == "none":
            self.stamp(box, "t_stop")
            box["_first_return"].set()
            return
        if proc in ("cancel", "terminate"):
            self.request(box, proc)
            return
        if proc == "kill_worker":
            self.kill_target_worker(box)
            return
        if proc == "kill_worker_restart":
            self.kill_target_worker(box)
            self.restart_target_worker(box, box["t_stop"]["mono"] + plan["c7_restart_s"])
            return
        if proc == "kill_cancel_restart":
            k = self.kill_target_worker(box)
            self._sleep_until(box["t_stop"]["mono"] + plan["c8_cancel_s"], box)
            if k.get("exit_mono") is None:
                raise RuntimeError("cancel withheld: the worker's exit is not confirmed")
            self.request(box, "cancel")
            self.restart_target_worker(box, box["t_stop"]["mono"] + plan["c8_restart_s"])
            return
        if proc == "cancel_server_restart":
            a = self.request(box, "cancel")
            t_sig = a["t_return"] + plan["c6_server_delay_s"]
            polls = []
            while now() < t_sig - plan["c6_precondition_poll_s"]:
                # show as well as describe: the closed history read here is the "before" side of C6's
                # last-event-ID comparison across the restart (C.6, C6 outage)
                r = read_parts(f"c6-pre-{len(polls) + 1}", "precondition",
                               [("describe_target", describe_args(self.ids["target"])),
                                ("show_target", show_args(self.ids["target"]))], parallel=True,
                               deadline=t_sig - 0.05)
                polls.append(r)
                if c6_poll_done(r):
                    break
                time.sleep(plan["c6_precondition_poll_s"])
            pre = c6_precondition(polls, t_sig)
            pre["reads"] = polls
            box["precondition"] = pre
            self.server_restart(box, t_sig, "C6")
            return
        raise ValueError(f"unknown procedure {proc}")

    def run_intervention(self, box, gate):
        box["thread_started_mono"] = now()
        try:
            if not gate.wait(max(0.0, box["planned_t_stop"] - now()) + 1.0) or box.get("withheld"):
                box["withheld"] = True
                return
            self._sleep_until(box["planned_t_stop"], box)
            self.runner(box)
        except Exception as e:  # noqa: BLE001 - thread failures are data
            box["error"] = f"{type(e).__name__}: {e}"
        finally:
            box["thread_finished_mono"] = now()
            box["_first_return"].set()
            box["_request_returned"].set()

    # ---- claim poll (C.4 step 5) ----
    def claim_poll(self, box, stop_evt):
        plan = self.plan
        if self.spec["has_request"]:
            deadline = now() + plan["watch_s"]
            while not box["_request_returned"].is_set() and not stop_evt.is_set() and now() < deadline:
                box["_request_returned"].wait(0.2)
            if not box["_request_returned"].is_set():
                return
            req = next((a for a in box["actions"] if a.get("name") in ("cancel", "terminate")), None)
            t0 = req["t_return"] if req else None
            if t0 is None:
                return
        else:
            deadline = now() + plan["watch_s"]
            while not box["_started"].is_set() and not stop_evt.is_set() and now() < deadline:
                box["_started"].wait(0.2)
            t0 = box.get("t_stop", {}).get("mono")
            if t0 is None:
                return
        until = box["t_stop"]["mono"] + plan["claim_poll_until_s"]
        n = 0
        while now() < until and not stop_evt.is_set():
            n += 1
            r = read_parts(f"poll-{n}", "poll", [("describe_target", describe_args(self.ids["target"]))],
                           deadline=until)
            r["during_declared_outage"] = self.outage_open()
            self.R["claim_poll"].append(r)
            d = A.describe_of(r)
            if d and d["status"] in A.CLOSED:
                return
            stop_evt.wait(max(0.0, plan["claim_poll_every_s"] - (now() - r["t_start"])))

    # ---- durability probe (C.4 step 6) ----
    def durability_probe(self):
        P = {"start_mono": now(), "outages": [], "restarts": [], "capability": [], "reads": [], "actions": []}
        self.R["probe"] = P
        try:
            P["submission"] = self.submit(["ptwin"])
            deadline = now() + 20
            ptw = self.ids["ptwin"]
            while now() < deadline and not any(is_role(e, "ptwin", ptw) and e.get("kind") == "accepted"
                                               for e in self.rx.effects()):
                time.sleep(0.1)
            time.sleep(0.5)                              # let the TimerStarted reach the history
            parts = [("describe_target", describe_args(self.ids["target"])),
                     ("describe_twin", describe_args(self.ids["twin"])),
                     ("describe_ptwin", describe_args(ptw)), ("show_ptwin", show_args(ptw)),
                     ("show_target", show_args(self.ids["target"]))]
            r = read_parts("pre-probe", "pre-probe", parts, parallel=True)
            pre = probe_precheck_decision({"target": A.describe_of(r), "twin": A.describe_of(r, "describe_twin"),
                                           "ptwin": A.describe_of(r, "describe_ptwin")}, A.history_of(r, "show_ptwin"))
            pre["read"] = r
            P["pre_check"] = pre
            P["stop_start_mono"] = now()
            for role in ("target", "twin"):
                if self.procs.alive(f"worker-{role}"):
                    a = {"name": f"sigterm_worker_{role}", "signal": "SIGTERM"}
                    self.procs.signal(f"worker-{role}", signal.SIGTERM, self.plan["exit_wait_s"], a)
                    P["actions"].append(a)
                    if a.get("exit_mono") is None:
                        k = {"name": f"sigkill_worker_{role}", "deviation": "worker did not exit within 15 s"}
                        self.procs.signal(f"worker-{role}", signal.SIGKILL, 5, k)
                        P["actions"].append(k)
            outage = {"kind": "server", "t_from": None, "t_to": None, "reason": "durability probe"}
            P["outages"].append(outage)
            self.outages.append(outage)
            a = {"name": "sigterm_server", "signal": "SIGTERM"}
            self.procs.signal("server", signal.SIGTERM, self.plan["exit_wait_s"], a)
            outage["t_from"] = a["t_send"]
            P["actions"].append(a)
            if a.get("exit_mono") is None:
                k = {"name": "sigkill_server", "deviation": "server did not exit within 15 s"}
                self.procs.signal("server", signal.SIGKILL, 5, k)
                P["actions"].append(k)
            self.start_server()
            outage["t_to"] = now()
            for role in ("target", "twin"):
                launch, cap = self.start_worker(role)
                P["restarts"].append({"what": f"worker:{role}", "ok": cap["ok"], "generation": self.gen[role],
                                      "t_ready": now()})
            P["capability"] = [self.ready_job("target", "cap-probe-target"), self.ready_job("twin", "cap-probe-twin")]
            P["ready_mono"] = now()
            t0 = P["ready_mono"]
            k = 0
            while True:
                tgt = t0 + k * self.plan["probe_read_every_s"]
                if tgt > t0 + self.plan["probe_obs_s"]:
                    break
                while now() < tgt:
                    time.sleep(0.05)
                parts = self.watch_parts()[:1] + [self.watch_parts()[2], self.watch_parts()[3],
                                                  ("describe_ptwin", describe_args(ptw))]
                P["reads"].append(read_parts(f"probe+{k * self.plan['probe_read_every_s']:g}", "probe", parts,
                                             offset=k * self.plan["probe_read_every_s"], parallel=True))
                k += 1
            last = P["reads"][-1] if P["reads"] else None
            dp = A.describe_of(last, "describe_ptwin") if last else None
            P["ptwin_completed"] = bool(dp and dp["status"] == "COMPLETED") if last else None
        except Exception as e:  # noqa: BLE001 - the completed watch stays usable (as harness_b)
            P["error"] = f"{type(e).__name__}: {e}"
        P["end_mono"] = now()
        self.health("end of probe")
        return P

    # ---- configuration (C.0) ----
    def config_record(self):
        cfg = {}
        v = subprocess.run([CLI, "--version"], capture_output=True, text=True, timeout=20, env=trial_env())
        cfg["cli_version"] = v.stdout.strip()
        cfg["cli_sha256"] = sha256_file(CLI)
        code = ("import importlib.metadata as m, hashlib, json, platform; d = m.distribution('temporalio'); "
                "print(json.dumps({'temporalio': d.version, 'record_sha256': "
                "hashlib.sha256((d.read_text('RECORD') or '').encode()).hexdigest(), "
                "'python': platform.python_version()}))")
        p = subprocess.run([PY, "-B", "-c", code], capture_output=True, text=True, timeout=60, env=trial_env())
        cfg.update(parse_json_output(p.stdout) or {"probe_error": p.stderr[-300:]})
        cfg["dynamic_config"] = "default"
        cfg["server_cmd"] = self.server_cmd()
        mism = {k: {"expected": e, "actual": cfg.get(k)} for k, e in EXPECTED.items() if cfg.get(k) != e}
        cfg["mismatches"] = mism
        if mism:
            raise RuntimeError(f"configuration differs from the declared one (C.0): {mism}")
        return cfg

    def isolation_record(self):
        ns = os.environ.get("STOPCHECK_C_NETNS")
        rec = {"netns": ns, "self_inode": guards.netns_inode()}
        try:
            rec["handle_inode"] = os.stat(f"/run/netns/{ns}").st_ino if ns else None
        except OSError:
            rec["handle_inode"] = None
        rec["interfaces"] = network_interfaces()
        rec["loopback_up"] = loopback_up()
        rec["ok"] = bool(ns and rec["handle_inode"] == rec["self_inode"] and rec["interfaces"] == ["lo"]
                         and rec["loopback_up"] is True)
        if HOST_PROFILE == "linux-vm":
            # a VM's host network has routes out (the metadata server, the internet); the namespace must have none
            rec["outbound"] = outbound_unreachable()
            rec["ok"] = rec["ok"] and rec["outbound"].get("ok") is True
        return rec

    def clean_slate(self):
        busy = set()
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                busy |= listening_ports(open(path, encoding="utf-8").read())
            except OSError:
                pass
        clash = sorted(p for p in PORTS.values() if p in busy)
        others = []
        for pid in os.listdir("/proc"):
            if pid.isdigit() and int(pid) != os.getpid():
                info = proc_info(int(pid))
                cmdline = (info or {}).get("cmdline") or []
                if is_trial_process(cmdline):
                    others.append({"pid": int(pid), "cmd": " ".join(cmdline)[:200]})
        return {"ports_busy": clash, "leftover_processes": others, "ok": not clash and not others}

    # ---- the whole trial ----
    def run(self):
        R, plan, spec = self.R, self.plan, self.spec
        th, ticks, stop_evt = None, None, threading.Event()
        box = {"planned_t_stop": None, "actions": [], "restarts": [], "outages": [], "worker_dead": [],
               "_started": threading.Event(), "_first_return": threading.Event(),
               "_request_returned": threading.Event(), "_cancel": threading.Event()}
        R["intervention"] = box
        poll_th = None
        try:
            R["isolation"] = self.isolation_record()
            if not R["isolation"]["ok"]:
                raise RuntimeError(f"not in a loopback-only trial namespace: {R['isolation']}")
            R["clean_slate"] = self.clean_slate()
            if not R["clean_slate"]["ok"]:
                raise RuntimeError(f"clean slate failed: {R['clean_slate']}")
            # C.4 step 1, C.10: the only StopCheck trial on this host, under the wrapper's host lock
            R["host_exclusion"] = host_exclusion(os.environ.get("STOPCHECK_C_NETNS"))
            if not R["host_exclusion"]["ok"]:
                ex = R["host_exclusion"]
                raise RuntimeError("another StopCheck trial or its services are on this host (C.10): " + json.dumps(
                    {k: ex.get(k) for k in ("lock", "processes", "namespaces", "units", "listeners")},
                    default=str)[:1500])
            R["guards_start"] = guards.snapshot()
            # C.9 disk floor, by the declared host profile (wsl: unchanged)
            R["host_checks_start"] = host_checks()
            R["disk_free_gb"] = (R["host_checks_start"].get("disk") or {}).get("free_gb")
            if not R["host_checks_start"]["ok"]:
                raise RuntimeError("host precondition failed at start: " +
                                   "; ".join(R["host_checks_start"]["violations"]))
            R["clock_start"] = clockguard.check(2.0)
            if not R["clock_start"]["ok"]:
                R["void"] = "clock guard failed at start: " + "; ".join(R["clock_start"]["reasons"])
                return R
            R["config"] = self.config_record()
            # receiver, server, workers, readiness (C.4 steps 1-2)
            self.procs.start("receiver", [PY, "-B", os.path.join(HERE, "receiver_c.py"), "--port",
                                          str(PORTS["receiver"]), "--out", os.path.join(self.out, "receiver.jsonl")],
                             "receiver.out")
            if not self.wait_receiver():
                raise RuntimeError("receiver never answered /health")
            ticks = Ticks(R, self.trial, plan["tick_every_s"])
            ticks.start()
            self.start_server()
            self.start_worker("twin")
            self.start_worker("target")
            # submission (C.4 step 3)
            R["submission"] = {"main": self.submit(["target"] if self.cond == "C6" else ["target", "twin"])}
            t_sub = R["submission"]["main"]["t_start"]
            if self.cond == "C6":
                while now() < t_sub + plan["c6_twin_delay_s"]:
                    time.sleep(0.05)
                R["submission"]["twin"] = self.submit(["twin"])
            R["ids"] = self.ids
            # anchor (C.3)
            t_lim = now() + plan["anchor_wait_s"]
            anchor = None
            while now() < t_lim and anchor is None:
                anchor = anchor_effect(self.rx.effects(), spec, self.ids["target"], plan)
                if anchor is None:
                    time.sleep(0.02)
            if anchor is None:
                raise RuntimeError("the anchor effect never arrived")
            R["anchor"] = {"kind": spec["anchor"], "step": anchor.get("step"), "t_a": anchor["mono"],
                           "seq": anchor.get("seq"), "detected_mono": now()}
            planned = planned_t_stop(spec, anchor["mono"], plan)
            R["planned_t_stop"] = planned
            box["planned_t_stop"] = planned
            gate = threading.Event()
            th = threading.Thread(target=self.run_intervention, args=(box, gate), daemon=True, name="intervention")
            th.start()
            poll_th = threading.Thread(target=self.claim_poll, args=(box, stop_evt), daemon=True, name="claim-poll")
            poll_th.start()
            self.health("before t_stop", deadline=planned - plan["precheck_io_reserve_s"])
            t_check = pre_check_start(planned, plan)       # t_a, except in C1 (C.3)
            while now() < t_check:
                time.sleep(0.02)
            R["pre_check"] = self.pre_check(planned)
            decision = pre_check_outcome(R["pre_check"])
            if decision["withhold"]:
                box["withheld"] = True
                gate.set()
                raise RuntimeError(decision["trial_failure"])
            if decision["untested"]:
                R["untested"].append(decision["untested"])
            gate.set()
            if not box["_started"].wait(max(0.0, planned - now()) + 2.0):
                raise RuntimeError("intervention never started: " + str(box.get("error", "missing t_stop stamp")))
            t_stop = box["t_stop"]["mono"]
            R["t_stop_mono"] = t_stop
            box["start_lateness_s"] = round(t_stop - planned, 4)
            # watch (C.4 step 5)
            box["_first_return"].wait(10)
            cap_thread = self.watch(t_stop, watch_schedule(t_stop, plan, self.cond))
            R["watch_end_mono"] = t_stop + plan["watch_s"]
            self.health("end of watch")
            self.finish_capability(cap_thread)       # the capability jobs may end after the watch (C.4 step 5)
            th.join(timeout=5)
            if th.is_alive():
                raise RuntimeError("intervention thread exceeded the watch")
            if box.get("error"):
                raise RuntimeError("intervention thread failed: " + box["error"])
            late_untested = untested_after_watch(self.cond, box)
            if late_untested:
                R["untested"].append(late_untested)
            # durability probe (C.4 step 6)
            self.durability_probe()
        except Exception as e:  # noqa: BLE001
            R["error"] = f"{type(e).__name__}: {e}"
            import traceback
            R["traceback"] = traceback.format_exc()[-2000:]
        except KeyboardInterrupt:
            R["error"] = "KeyboardInterrupt: trial interrupted; no scored result"
        finally:
            box["_cancel"].set()
            stop_evt.set()
            if th:
                th.join(5)
                R["intervention_thread_alive"] = th.is_alive()
                if th.is_alive():
                    R["void"] = R.get("void") or "intervention thread still alive; inspect clean-up"
            if poll_th:
                poll_th.join(10)
            for k in ("_started", "_first_return", "_request_returned", "_cancel"):
                box.pop(k, None)
            R["rerun_needed"] = bool(R["untested"])
            self.finalize(ticks)
        return R

    def across_trial_clock(self):
        """C.9: the across-trial clock check over every line the receiver wrote (each carries `t`, `mono`, `raw`)."""
        return check_trial_clocks(self.rx.poll())

    def cleanup_terminate(self):
        """C.4 step 7: terminate every open workflow of this trial, recorded as clean-up, not as an intervention; each
        entry keeps the typed command and Temporal's reply, as an intervention's request does (C.3)."""
        out = []
        for role in ("target", "twin", "ptwin"):
            ident = self.ids[role]
            if not ident.get("run_id"):
                continue
            d = A.norm_describe(cli(describe_args(ident))["json"])
            if d and d["status"] == "RUNNING":
                res = cli(["workflow", "terminate", "-w", ident["workflow_id"], "-r", ident["run_id"],
                           "--reason", "expc-cleanup"])
                entry = {"workflow_id": ident["workflow_id"], "rc": res["rc"], "timed_out": res["timed_out"]}
                entry.update(cli_reply(res))
                out.append(entry)
        return out

    def finalize(self, ticks):
        """Clean-up last (C.4 step 7), then the end guards, then record.json."""
        R = self.R
        errors = []
        R["cleanup"] = {"terminated": [], "errors": errors}
        try:
            if self.procs.alive("server"):
                R["cleanup"]["terminated"] = self.cleanup_terminate()
        except Exception as e:  # noqa: BLE001
            errors.append(f"terminate: {type(e).__name__}: {e}")
        try:
            R["clock_across_trial"] = self.across_trial_clock()
        except Exception as e:  # noqa: BLE001
            errors.append(f"clock across trial: {type(e).__name__}: {e}")
        if ticks:
            ticks.stop.set()
            ticks.join(3)
        self.procs.stop_all(errors)
        time.sleep(0.3)
        try:
            R["clock_end"] = clockguard.check(2.0)
            R["guards_end"] = guards.snapshot()
            R["guard_violations"] = guards.violations(R.get("guards_start") or {}, R["guards_end"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"end guards: {type(e).__name__}: {e}")
        slate = self.clean_slate()
        R["leftovers"] = slate
        if errors or not slate["ok"]:
            R["void"] = R.get("void") or "clean-up failed or resources remained; inspect the clean-up record"
        if R.get("guard_violations"):
            R["void"] = R.get("void") or "guard violation: " + "; ".join(R["guard_violations"])
        save(R, self.out)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save(R, out_dir):
    with open(os.path.join(out_dir, "record.json"), "x", encoding="utf-8") as f:
        f.write(json.dumps(R, indent=1, default=str))
    print(f"record: {os.path.join(out_dir, 'record.json')}; error: {R.get('error') or R.get('void') or 'none'}; "
          f"untested: {R.get('untested') or 'none'}", flush=True)


# ============================ entry point ============================
def ns_name(trial):
    return "expc-" + trial.lower()


CLEANUP_GRACE_S = 120.0     # how long an interrupted trial gets to finish its own clean-up (C.4 step 7)


def mark_record_void(trial, why):
    """Record a clean-up failure found after the trial process exited in that trial's record.json, so the scorer
    sees the trial as void (C.9). Does nothing if the trial left no single record."""
    import glob
    paths = glob.glob(os.path.join(HERE, "results", f"{trial}-*", "record.json"))
    if len(paths) != 1:
        print(f"warning: could not mark the record of {trial} void ({len(paths)} records found)", file=sys.stderr)
        return
    with open(paths[0], encoding="utf-8") as f:
        R = json.load(f)
    R["void"] = R.get("void") or why
    R["namespace_leftovers"] = why
    with open(paths[0], "w", encoding="utf-8") as f:
        f.write(json.dumps(R, indent=1, default=str))


def namespace_pids(ns):
    """PIDs of the processes in the named network namespace (`ip netns pids`); None if they cannot be listed."""
    r = subprocess.run(["ip", "netns", "pids", ns], capture_output=True, text=True)
    if r.returncode:
        return None
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def kill_namespace_leftovers(ns, wait_s=10.0):
    """C.4 step 7: no trial process may outlive the trial, and deleting a namespace does not stop the processes in
    it. Any process still in the trial's namespace after the trial gets SIGTERM, then SIGKILL. Returns the PIDs
    found ([] if none), or None if the namespace's processes could not be listed."""
    pids = namespace_pids(ns)
    if not pids:
        return pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = now() + wait_s
    left = pids
    while left and now() < deadline:
        time.sleep(0.2)
        left = namespace_pids(ns) or []
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return pids


def wrap_in_namespace(cond, trial):
    """Create the trial's loopback-only namespace, re-run this file inside it, stop anything the trial left in it,
    and delete the namespace (C.1, C.4 step 7). On an interrupt the trial process cleans up by itself (finalize),
    so this waits for it, up to CLEANUP_GRACE_S, instead of killing it; then the interrupt is passed on."""
    ns = ns_name(trial)
    # C.10 (pre-freeze review): the host locks are held from before the namespace exists, and so before the trial's
    # clean slate, until the namespace is deleted after clean-up
    handles, lock_record = acquire_host_locks()
    try:
        if os.path.exists(f"/run/netns/{ns}"):
            raise SystemExit(f"namespace {ns} already exists: not a clean slate")
        subprocess.run(["ip", "netns", "add", ns], check=True)
        rc = 2
        try:
            subprocess.run(["ip", "-n", ns, "link", "set", "lo", "up"], check=True)
            env = dict(os.environ, STOPCHECK_C_IN_NS=trial, STOPCHECK_C_NETNS=ns,
                       STOPCHECK_C_HOST_LOCKS=json.dumps(lock_record))
            p = subprocess.Popen(["ip", "netns", "exec", ns, sys.executable, "-B", os.path.abspath(__file__), cond,
                                  trial], env=env)
            try:
                rc = p.wait()
            except KeyboardInterrupt:
                print(f"interrupted: waiting up to {CLEANUP_GRACE_S:g} s for trial {trial} to clean up",
                      file=sys.stderr)
                try:
                    rc = p.wait(timeout=CLEANUP_GRACE_S)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    p.kill()
                    rc = p.wait()
                raise
        finally:
            left = kill_namespace_leftovers(ns)
            if left is None or left:
                rc = 2
                why = ("the trial namespace's processes could not be listed after the trial" if left is None else
                       f"processes left in the trial namespace after clean-up were stopped: {left}")
                print(f"warning: namespace {ns}: {why}; the trial is void", file=sys.stderr)
                mark_record_void(trial, why)
            r = subprocess.run(["ip", "netns", "delete", ns], capture_output=True, text=True)
            if r.returncode:
                print(f"warning: could not delete namespace {ns}: {r.stderr.strip()}", file=sys.stderr)
    finally:
        release_host_locks(handles)
    return rc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        raise SystemExit(f"usage: harness_c.py <condition> <trial-id>; conditions: {', '.join(CONDITIONS)}")
    cond, trial = argv
    if cond not in CONDITIONS:
        raise SystemExit(f"unknown condition {cond}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,40}", trial):
        raise SystemExit("trial ID: 1-41 letters, digits or hyphens, starting with a letter or digit")
    if host_profile_refusal():                 # C.0: before anything is read or set up
        raise SystemExit(host_profile_refusal())
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise SystemExit("run as root (the SDK environment is root-only, DESIGN-expC section 2)")
    fast = os.environ.get("STOPCHECK_FAST") == "1"
    run_kind = os.environ.get("STOPCHECK_RUN_KIND", "smoke" if fast else "pilot")
    manifest = os.environ.get("STOPCHECK_FROZEN_MANIFEST")
    check_run_kind(run_kind, fast, manifest)
    if os.environ.get("STOPCHECK_C_IN_NS") != trial:
        return wrap_in_namespace(cond, trial)

    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    out = os.path.join(HERE, "results", f"{trial}-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=False)
    run = TrialRun(cond, trial, out, run_kind, fast)
    import run_expC
    run.R["source_hashes"] = run_expC.source_hashes()
    # C.8, C.10: the predictions as they stand before this trial runs (the freeze compares them with the frozen file)
    run.R["predictions_at_start"] = predictions_receipt()
    run.R["host_identity"] = host_identity_record(os.environ.get("STOPCHECK_BATCH"), trial)
    if run_kind == "screen":
        run.R["freeze"] = screen_freeze(run_expC, manifest, cond)
        run.R["freeze_manifest_sha256"] = hashlib.sha256(open(manifest, "rb").read()).hexdigest()
    R = run.run()
    if run_expC.source_hashes() != R["source_hashes"]:
        R["void"] = R.get("void") or "source files changed during the trial"
        with open(os.path.join(out, "record.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(R, indent=1, default=str))
    return exit_code(R)


if __name__ == "__main__":
    sys.exit(main())
