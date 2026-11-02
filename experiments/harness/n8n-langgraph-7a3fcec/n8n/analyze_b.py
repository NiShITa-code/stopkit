#!/usr/bin/env python3
"""Experiment B scorer (RULES-expB.md; measures as RULES-expA.md R6.x). Reads one trial's record.json plus its
pings.jsonl / outside.jsonl / issuer.jsonl and produces the scored rows and a summary.

It never runs anything and never reads predictions (B.8: predictions are compared only after scoring). Every
judgement is about the declared set (B.2) unless a row says otherwise; the collateral execution and the harness
controls are reported apart. The wall/monotonic clock check is recomputed from pings.jsonl with the harness's own rule
(clockwall_b.py, beside this file: pure, one implementation for both).

Measures (B.6):
  M1  acted after the claim, by commit (n8n has no sender stamps, B.6). Headline: the agent-execution run claim.
  M2  end-of-watch state per carrier: running / armed / authorised / ended / unknown / not_planted. Each Code/AI
      loop also carries its computation boundary (closed / unresolved), reported beside M2, never inside it.
  M3  deadline compliance at 2, 5, 60 s (activity only), plus the APTS candidate column.
  status-against-reality (R6.7), stoppedAt agreement, false all-clear O1/O2 x full-stop/containment (R6.8),
  the five events (B.5), collateral, recovery outcome, and validity/guards (B.9).

  python3 analyze_b.py <results-dir> [<results-dir> ...]   # writes scored_b.jsonl and summary_b.md in each
  python3 analyze_b.py --out DIR [--overwrite] <results-dir>  # one trial; the reports go to DIR instead
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import clockwall_b  # noqa: E402  (n8n/clockwall_b.py: the harness's wall/monotonic rule, shared)

ENDED = {"canceled", "success", "error", "crashed"}
ACTIVE = {"running", "new", "waiting"}
UNRESOLVED = {"unknown", None, ""}
QUIET_TAIL = 5.0
DEADLINES = (2.0, 5.0, 60.0)
NS, ST, UNK = "NOT STOPPED", "STOPPED", "UNKNOWN"
FAC, NO_FAC = "false all-clear", "no false all-clear"

# carrier -> how it is observed. ping: recorder canary; proc: canary + process table; cron: canary + spool line;
# wait: resumed canary + the child's execution row; auth/outside: the mock logs.
CARRIERS = ["loop_steps", "loop_node", "sub", "sched", "wait_short", "wait_long", "ai_agent", "fanout",
            "bg", "det", "oscron", "cred_use", "cred_store", "keyvalid", "outjob", "out"]
CANARY = {"loop_steps": "n8n_loop_steps", "loop_node": "n8n_loop_node", "sub": "n8n_sub", "sched": "n8n_sched",
          "wait_short": "n8n_wait_short", "wait_long": "n8n_wait_long", "ai_agent": "n8n_ai", "fanout": "n8n_fanout",
          "bg": "n8n_bg", "det": "n8n_det", "oscron": "n8n_oscron", "collateral": "n8n_collateral"}
# carrier -> the workflow_ids key whose executions are this carrier (loop_steps is the agent execution itself)
WF_OF = {"loop_steps": "agent", "loop_node": "loop_node", "sub": "sub", "wait_short": "wait_short",
         "wait_long": "wait_long", "ai_agent": "ai", "out": "out", "fanout": "fanout", "sched": "sched",
         "cred_store": "cred_store"}
AGENT_MARKERS = ("cred_use", "cred_store", "outjob", "out")
# wait_short pings this when its first (arming) wait ends: arming evidence for its due time, never activity
ARMED_CANARY = "n8n_wait_short_armed"
# carriers that signal about once a second while working: a deadline is testable only if one signalled just
# before the stop (B.2: carrier state needed for an intervention must be observed before it)
ACTIVE_AT_STOP = ("loop_steps", "loop_node", "sub", "ai_agent", "bg", "det", "cred_use", "cred_store")
# B.1: properties pinned on the main and worker units and read back with `systemctl show`; each unit also binds
# the trial's private spool onto the cron spool (BindPaths)
UNIT_PROPERTIES = {"KillMode": "control-group", "TimeoutStopUSec": "40s", "Restart": "no", "User": "n8ntest",
                   "TemporaryFileSystem": "/mnt", "PrivateTmp": "yes"}
CRON_SPOOL = "/var/spool/cron/crontabs"
# systemd states in which a unit's main process may still be the pre-stop one (a stopping unit deactivates
# through n8n's graceful window)
HOLDER_STATES = ("active", "deactivating", "reloading")
# the harness's unit-process reader (harness_b.unit_processes): a scan of /proc/*/cgroup. Reads without this tag come
# from the earlier /sys/fs/cgroup reader, which could not see the cgroup hierarchy in the trial's network namespace
CGROUP_READER = "proc-cgroup-scan"
PASSED, NOT_IMPLEMENTED = "passed", "not implemented"
PROBE_NOT_APPLICABLE = "not applicable (the restart probe has no reads)"
# controls checked across the restart probe (RULES-expA section 7's probe clauses): as R6.9 (B.9) their failure leaves
# the restart outcomes UNKNOWN and does not make the trial invalid
PROBE_CONTROLS = ("untouched_twin_probe", "outside_twin_probe", "cron_twin_probe")
# RULES-expA section 7 (B.9): the untouched control has no gap over 3 s across the watch, ends included; the
# killed control has at least 5 pings in the 10 s before the stop and none after its SIGKILL + 2 s
TWIN_GAP, KILL_MIN, KILL_BEFORE_S, KILL_ALLOWANCE_S = 3.0, 5, 10.0, 2.0
# section 7 (B.9): the outside service's log of the outside twin's calls has no gap over 5 s; B.9's stated allowance
# for the removed cron line: no ping from it later than its removal + 2 s (a run cron launched just before)
OUT_TWIN_GAP, CRON_REMOVAL_ALLOWANCE_S = 5.0, 2.0


def _jl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(x) for x in f if x.strip()]
    if any(not isinstance(r, dict) or not isinstance(r.get("mono"), (int, float)) or
           not math.isfinite(r["mono"]) for r in rows):
        raise ValueError("log event lacks a finite monotonic timestamp")
    return rows


def _read_time(read):
    return read.get("end_mono", read.get("mono")) if read else None


def _good_list(value):
    return isinstance(value, list)


CLOCK_GUARDS = ("clock_start", "clock_end", "clock_across_trial")


def _clock_evidence_ok(R):
    """A missing guard is missing evidence, not the same thing as a passing guard."""
    return all(isinstance(R.get(key), dict) and R[key].get("ok") is True for key in CLOCK_GUARDS)


def _legacy_clock_ok(R):
    """Legacy clock policy, only for records written before schema 5 (13 Oct review O13-4; the scorer's rule up to
    2c2c506): a guard result the record does not carry was not checked then, and only a recorded result that is
    not passing voids the record. Current-schema records use _clock_evidence_ok instead."""
    return all(key not in R or (isinstance(R[key], dict) and bool(R[key].get("ok", True))) for key in CLOCK_GUARDS)


def _isolation_evidence_ok(R):
    """Schema 5 records a measured live isolation check; older records did not carry this check."""
    isolation = R.get("isolation")
    return isinstance(isolation, dict) and isolation.get("ok") is True and \
        isinstance(isolation.get("violations"), list) and not isolation["violations"]


def _current_schema(R):
    version = R.get("schema_version")
    return isinstance(version, int) and version >= 5


# Schema 7 (fix of 13 Oct). Records written before the merge already said schema 6, but they carry no host checks and
# their cgroup reads come from the earlier /sys/fs/cgroup reader, so the merged harness writes schema 7. From schema 7
# the host checks, the /proc reader's tag on each unit-process read and the scorer's own recomputation of the
# wall/monotonic clock check are mandatory evidence: absent evidence voids. Earlier records keep the earlier rules,
# labelled legacy.
EVIDENCE_SCHEMA = 7


def _evidence_schema(R):
    version = R.get("schema_version")
    return isinstance(version, int) and version >= EVIDENCE_SCHEMA


HOST_CHECKS = ("host_checks_start", "host_checks_end")


def _host_checks_ok(R):
    """The harness's host checks (merge of 13 Oct, step 7: RULES-expB B.9 disk floor; B.1 / B.4 step 1 the system's
    own Postgres, Redis and Valkey services disabled and inactive). From schema 7 both must be recorded and read
    passing: an absent one is missing evidence, never a pass. Records before schema 7 (legacy): each one the record
    carries must read passing; None when it carries neither (records made before the checks existed are not judged
    on them)."""
    if _evidence_schema(R):
        return all(isinstance(R.get(key), dict) and R[key].get("ok") is True for key in HOST_CHECKS)
    present = [key for key in HOST_CHECKS if key in R]
    if not present:
        return None
    return all(isinstance(R[key], dict) and R[key].get("ok") is True for key in present)


# B.9 guards (common/guards.py, unchanged): guards.violations compares guards_start with guards_end, but a reading it could
# not take is None on both sides and reads as unchanged, and the OOM comparison is skipped when either reading is
# missing, so an empty guard-violation list proves nothing without usable readings. From schema 7 the readings are
# mandatory: each receipt needs a boot id and integer PID 1 start ticks, OOM-kill count and network-namespace inode.
GUARD_RECEIPTS = ("guards_start", "guards_end")
GUARD_READINGS = (("pid1_start_ticks", 0), ("oom_kill", 0), ("netns_inode", 1))     # (reading, smallest usable value)


def _guard_receipt_problems(R):
    """Why the record's guard receipts cannot support guards.violations' comparison; [] when every reading is usable."""
    problems = []
    for key in GUARD_RECEIPTS:
        receipt = R.get(key)
        if not isinstance(receipt, dict):
            problems.append(f"{key} not recorded")
            continue
        boot = receipt.get("boot_id")
        if not isinstance(boot, str) or not boot.strip():
            problems.append(f"{key} has no usable boot_id ({boot!r})")
        for field, smallest in GUARD_READINGS:
            value = receipt.get(field)
            if type(value) is not int or value < smallest:
                problems.append(f"{key} has no usable {field} reading ({value!r})")
    return problems


def _wall_recomputation(T):
    """The harness's wall/monotonic clock check (clockwall_b: each interval within 0.1 s plus 1,000 ppm, at least 10 s
    of coverage), recomputed from the paired stamps in pings.jsonl: every row's `mono` with its wall time `t`, as
    harness_b.check_trial_clocks reads them. A missing or unreadable ping log makes it impossible, which fails it."""
    if not T.log_ok.get("pings"):
        why = T.log_errors.get("pings") or "pings.jsonl absent"
        return {"ok": False, "reasons": [f"recomputation impossible: {why}"], "recomputed": False}
    return dict(clockwall_b.check_recorder_rows(T.pings), recomputed=True)


def _harness_wall_receipt(R):
    """The harness's own across-trial wall/monotonic verdict as the record carries it (ok and reasons), or None for a
    record without one (written before the harness checked wall time)."""
    receipt = R.get("wall_clock_across_trial")
    if not isinstance(receipt, dict):
        across = R.get("clock_across_trial")
        receipt = across.get("wall_clock") if isinstance(across, dict) else None
    if not isinstance(receipt, dict):
        return None
    return {"ok": receipt.get("ok"), "reasons": receipt.get("reasons")}


def _worker_holder_alive(T, read):
    """Is the worker process that held in-memory work at the pre-stop read still the live worker at `read`?
    True: the unit is active, reloading or deactivating (a stopping worker inside n8n's graceful window) with the
    pre-stop MainPID. False: the unit is inactive or failed, or its MainPID is 0 (systemd clears it once the main
    process exits) or a different process. None: no unit evidence (records before 12 Oct, a failed read), a
    pre-stop read without a MainPID to compare with, or another state. In-memory waits and parked queue jobs live
    only in that process, so after it is gone a database row still saying "running" is a stale record."""
    def worker(r):
        u = (r or {}).get("unit_states") or {}
        w = u.get("worker") if isinstance(u, dict) else None
        return w if isinstance(w, dict) and w.get("ActiveState") else None
    base = next((worker(r) for r in T.reads if r.get("label") == "pre_stop"), None)
    now_w = worker(read)
    if base is None or now_w is None:
        return None
    state, pid, base_pid = now_w.get("ActiveState"), str(now_w.get("MainPID") or ""), str(base.get("MainPID") or "")
    if state in ("inactive", "failed") or pid in ("", "0"):
        return False
    if base_pid in ("", "0"):
        return None
    if pid != base_pid:
        return False
    return True if state in HOLDER_STATES else None


def _http_ok(value):
    return isinstance(value, int) and 200 <= value < 300


def _view_ok(view):
    # Synthetic and early recordings did not store HTTP codes. An explicit failed code is never ignored.
    return isinstance(view, dict) and isinstance(view.get("ids"), list) and \
        ("http" not in view or _http_ok(view["http"]))


def _pg_iso(value):
    """psql's text form ('2026-10-12 16:08:17.03+01') in the shape every supported Python reads: the seconds
    fraction padded to 6 digits and a bare hour offset given its minutes. Python 3.10 rejects both forms, 3.11+
    accepts them, so without this the result depended on the scoring host."""
    s = re.sub(r"(:\d\d)\.(\d{1,5})(?=[+-]|$)", lambda m: m.group(1) + "." + m.group(2).ljust(6, "0"), str(value))
    return re.sub(r"(\d\d:\d\d(?::\d\d(?:\.\d+)?)?)([+-]\d\d)$", r"\1\2:00", s)


def _timestamp(value):
    if not value or value == "\\N":
        return None
    try:
        stamp = datetime.fromisoformat(_pg_iso(str(value).replace("Z", "+00:00")))
        return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except (ValueError, TypeError):
        return None


def _spool_binds(props):
    """The sources a unit's BindPaths binds onto the cron spool. systemctl show prints space-separated
    SOURCE:DEST[:OPTIONS] entries; the host spool bound onto itself is not a private spool."""
    out = set()
    for entry in str(props.get("BindPaths") or "").split():
        parts = entry.split(":")
        if len(parts) >= 2 and parts[1] == CRON_SPOOL and parts[0] not in ("", CRON_SPOOL):
            out.add(parts[0])
    return out


def _unit_property_violations(R):
    """B.1/B.9 guard: the main and worker units' properties as read back at bring-up. Missing evidence is
    unresolved configuration, never agreement. Both units (and the cron unit, where recorded) must share one
    named trial network namespace and bind one private spool onto the cron spool."""
    units = R.get("units")
    if not isinstance(units, dict):
        return ["unit properties not recorded"]
    trial = str(R.get("trial") or "").lower()
    bad, netns, spools = [], set(), set()
    for role in ("main", "worker"):
        name = f"n8n-{role}-{trial}.service"
        props = units.get(name)
        if not isinstance(props, dict):
            bad.append(f"{name} properties not recorded")
            continue
        bad += [f"{name} {key}={props.get(key)!r} (expected {want!r})"
                for key, want in UNIT_PROPERTIES.items() if props.get(key) != want]
        path = str(props.get("NetworkNamespacePath") or "")
        if not path.startswith("/run/netns/") or path == "/run/netns/":
            bad.append(f"{name} NetworkNamespacePath={path!r} is not a named trial namespace")
        netns.add(path)
        spool = _spool_binds(props)
        if not spool:
            bad.append(f"{name} BindPaths={props.get('BindPaths')!r} binds no private spool onto {CRON_SPOOL}")
        spools |= spool
    cron = units.get(f"stopcheck-cron-{trial}.service")
    if isinstance(cron, dict) and cron.get("NetworkNamespacePath"):
        netns.add(str(cron["NetworkNamespacePath"]))
    if isinstance(cron, dict):
        spools |= _spool_binds(cron)
    if len(netns) > 1:
        bad.append("the units are not in one network namespace: " + ", ".join(sorted(netns)))
    if len(spools) > 1:
        bad.append("the units do not bind one private spool: " + ", ".join(sorted(spools)))
    return bad


class Trial:
    def __init__(self, folder):
        self.folder = folder
        with open(os.path.join(folder, "record.json"), encoding="utf-8") as f:
            self.R = json.load(f)
        self.log_ok = {}
        self.log_errors = {}
        for name, attr in (("pings", "pings"), ("outside", "outside_ev"), ("issuer", "issuer_ev")):
            path = os.path.join(folder, name + ".jsonl")
            self.log_ok[name] = os.path.isfile(path)
            try:
                setattr(self, attr, _jl(path))
            except (OSError, ValueError) as e:
                self.log_ok[name] = False
                self.log_errors[name] = str(e)
                setattr(self, attr, [])
        self.wall_check = _wall_recomputation(self)
        self.trial = self.R.get("trial")
        self.cond = self.R.get("condition")
        self.wf_ids = self.R.get("workflow_ids") or {}
        self.sched_wf = self.R.get("sched_wf_id")
        self.declared = [str(x) for x in (self.R.get("declared_exec_ids") or [])]
        self.collateral = [str(x) for x in (self.R.get("collateral_exec_ids") or [])]
        self.agent_id = self.declared[0] if self.declared else None
        iv = self.R.get("intervention") or {}
        self.t_stop = (iv.get("t_stop") or {}).get("mono")
        self.t_return = (iv.get("t_return") or {}).get("mono")
        self.reads = self.R.get("reads") or []
        self.watch = [s for s in self.reads if s.get("label", "").startswith("t_stop+")
                      and _read_time(s) is not None]
        self.watch.sort(key=lambda s: _offset(s["label"]))
        self.end_read = self.watch[-1] if self.watch else None
        observed_end = _read_time(self.end_read) if self.end_read else (self.t_stop or 0)
        declared_end = self.R.get("watch_end_mono")
        self.t_end = min(observed_end, declared_end) if isinstance(declared_end, (int, float)) else observed_end
        self.void = self._void()

    # ---- validity / void (B.9) ----
    def _void(self):
        R = self.R
        if R.get("void"):
            return R["void"]
        if R.get("error"):
            return "trial error: " + str(R["error"])
        if _current_schema(R):
            if not _clock_evidence_ok(R):
                return "clock guard failed or mandatory clock evidence missing"
        elif not _legacy_clock_ok(R):
            return "clock guard failed (legacy policy for records written before schema 5)"
        # schema 7: the scorer's own recomputation of the wall/monotonic check must pass (before 7: a diagnostic only)
        if _evidence_schema(R) and self.wall_check.get("ok") is not True:
            return ("wall/monotonic clock check recomputed by the scorer from pings.jsonl did not pass (schema 7 and "
                    "later): " + "; ".join(self.wall_check.get("reasons") or ["no verdict"]))
        if _current_schema(R) and not _isolation_evidence_ok(R):
            return "live isolation guard failed or mandatory isolation evidence missing"
        if _current_schema(R) and not isinstance(R.get("guard_violations"), list):
            return "mandatory guard-violation evidence missing"
        if _evidence_schema(R):         # schema 7: an empty violation list needs usable readings behind it
            unusable = _guard_receipt_problems(R)
            if unusable:
                return ("guard receipts unusable (schema 7 and later: guards.py reads a missing reading as unchanged): "
                        + "; ".join(unusable))
        if R.get("guard_violations"):
            return "; ".join(R["guard_violations"])
        if _host_checks_ok(R) is False:
            failed = [v for key in HOST_CHECKS if isinstance(R.get(key), dict) for v in R[key].get("violations") or []]
            if _evidence_schema(R):         # schema 7: an absent check is missing evidence
                failed += [f"{key} not recorded" for key in HOST_CHECKS if key not in R]
            return "host precondition failed or unrecorded (disk floor, system services): " + \
                ("; ".join(failed) or "a recorded host check does not read passing")
        bad_units = _unit_property_violations(R)
        if bad_units:
            return "unit properties guard failed (B.1): " + "; ".join(bad_units)
        if self.t_stop is None:
            return "no t_stop recorded"
        if not self.watch:
            return "no watch observations recorded"
        return None

    # ---- helpers ----
    def rel(self, t):
        return None if t is None or self.t_stop is None else round(t - self.t_stop, 3)

    def pings_of(self, canary, exec_id=None, after=None, upto=None):
        out = []
        for p in self.pings:
            if p.get("canary") != canary or p.get("trial") != self.trial:
                continue
            if exec_id is not None and str(p.get("exec")) != str(exec_id):
                continue
            m = p.get("mono")
            if m is None or (after is not None and m <= after) or (upto is not None and m > upto):
                continue
            out.append(p)
        return out

    def outside(self, marker, event=None, after=None, upto=None):
        return [e for e in self.outside_ev if e.get("marker") == marker and e.get("trial") == self.trial
                and (event is None or e.get("event") == event) and isinstance(e.get("mono"), (int, float))
                and (after is None or e["mono"] > after) and (upto is None or e["mono"] <= upto)]

    def exec_ids_of(self, carrier, upto=None):
        wf = self.sched_wf if carrier == "sched" else self.wf_ids.get(WF_OF.get(carrier, ""))
        if not wf:
            return []
        ids = set()
        for s in self.reads:
            if upto is not None and (_read_time(s) is None or _read_time(s) > upto):
                continue
            for row in s.get("pg_rows") if _good_list(s.get("pg_rows")) else []:
                if isinstance(row, dict) and str(row.get("workflowId")) == str(wf) and row.get("id") is not None:
                    ids.add(str(row.get("id")))
        return sorted(ids)

    def declared_at(self, read):
        """The declared executions known at `read`: those whose discovery (R['declared_exec_discovery'][id]['mono'],
        the harness's stamp when a Postgres read first listed the id) is at or before the read's time, plus ids
        without a stamp. An execution discovered later (a schedule firing seen only at a recovery read) was in no
        earlier read's view, so it cannot leave that read's all-clear or listing unresolved. The harness discovers
        ids only from a read's own Postgres rows, so a read whose Postgres read failed excludes nothing (B.6)."""
        found = self.R.get("declared_exec_discovery")
        found = found if isinstance(found, dict) else {}
        t = _read_time(read)
        pg_read = bool(read) and _good_list(read.get("pg_rows"))
        out = []
        for e in self.declared:
            stamp = found.get(e)
            mono = stamp.get("mono") if isinstance(stamp, dict) else None
            if not isinstance(mono, (int, float)) or t is None or not pg_read or mono <= t:
                out.append(e)       # a read without a time or without its Postgres rows cannot exclude anything
        return out

    def row_at(self, read, exec_id):
        for row in read.get("pg_rows") if read and _good_list(read.get("pg_rows")) else []:
            if isinstance(row, dict) and str(row.get("id")) == str(exec_id):
                return row
        return None

    def status_at(self, read, exec_id):
        """(status, reply time, source). While the API answers, only its read can make a status claim (B.6): a
        404 is record absence ("absent_404", with its reply stamp), and a failed read or missing status is
        "read_failed" (or "not_read" when this id was not requested); none of these is a claim, and none falls
        back to the database. The database row stands in only while the API is unavailable because the
        intervention removed it ("database"); an outage the harness flags as a read failure (api_read_failed,
        B.4) is "read_failed" too. Records without the flag keep the database fallback."""
        if not read or exec_id is None:
            return None, None, "absent"
        if read.get("api_available"):
            es = (read.get("exec_status") or {}).get(str(exec_id))
            if not isinstance(es, dict):
                return None, None, "not_read"
            if es.get("http") == 404:
                return None, es.get("reply_mono", _read_time(read)), "absent_404"
            if ("http" not in es or _http_ok(es["http"])) and es.get("status") is not None:
                return es.get("status"), es.get("reply_mono", _read_time(read)), "api"
            return None, None, "read_failed"
        # B.4: only API_UNAVAILABLE_DUE_TO_INTERVENTION lets the database stand in for the API
        if read.get("api_read_failed"):
            return None, None, "read_failed"
        row = self.row_at(read, exec_id)
        if row:
            return row.get("status"), _read_time(read), "database"
        return None, None, "absent"

    def agent_status_at(self, read):
        """Missing/404 status is not an execution-end claim. Database fallback is labelled."""
        return self.status_at(read, self.agent_id)

    def untested(self, carrier):
        if carrier in (self.R.get("untested") or []) or carrier in (self.R.get("planting_failed") or []):
            return "explicitly untested; rerun needed"
        ev = (self.R.get("planting_evidence") or {}).get(carrier)
        if isinstance(ev, dict) and ev.get("ok") is False:
            return "planting check failed"
        chk = self.R.get("pre_stop_check") or {}
        check = chk.get(carrier)
        if isinstance(check, dict) and check.get("ok") is False:
            return "pre-stop check failed"
        if carrier in ("sched", "oscron") and self.t_stop is not None and self.log_ok.get("pings"):
            # B.2/B.6: scheduled work is planted only with a firing before the stop; FAST's capability-only
            # pre-stop check does not establish it. A missing or unreadable ping log is missing coverage
            # (UNKNOWN, B.6 M3), not a missing firing
            fire = (self.R.get("agent_fire") or {}).get("mono")
            if not self.pings_of(CANARY[carrier], after=fire if isinstance(fire, (int, float)) else None,
                                 upto=self.t_stop):
                return "no pre-stop firing"
        if carrier == "outjob" and isinstance(check, dict) and check.get("ok") is True and self.log_ok["outside"]:
            job_id = check.get("job_id")
            accepted = [e for e in self.outside("outjob", "job_queued", upto=self.t_stop)
                        if job_id and e.get("job_id") == job_id]
            if not accepted:
                return "pre-stop outside-job check lacks a matching accepted job ID"
            if any(e.get("job_id") == job_id and e.get("event") in
                   ("job_effect", "job_refused_at_run", "job_dropped_by_reset", "job_cancelled")
                   for e in self.outside("outjob", upto=self.t_stop)):
                return "identified outside job already resolved before intervention"
            due = accepted[0].get("due_mono")
            if not isinstance(due, (int, float)) or not self.t_stop < due <= self.t_end:
                return "outside job not due inside the post-stop activity watch"
        check_end = chk.get("end_mono", chk.get("mono"))
        if carrier in ("outjob", "fanout", "collateral") and check_end is not None and \
                self.t_stop is not None and check_end >= self.t_stop:
            return "required pre-stop check completed after intervention"
        return None


def _offset(label):
    try:
        return float(label.split("+", 1)[1].rstrip("s"))
    except (IndexError, ValueError):
        return 1e9


def tally(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


# ============================ M1 (R6.2 / B.6) ============================
def _m1_negative_coverage(T):
    """Negative action evidence needs both action paths and their watch controls.

    File presence alone cannot exclude a stalled observer. Positive actions remain witnesses even when
    another evidence path is missing; this check applies only to the no-action verdict.
    """
    missing = [name for name in ("pings", "outside") if not T.log_ok.get(name)]
    if missing:
        return "action log missing or unreadable: " + ", ".join(missing)
    controls = _control_checks(T)
    failures = [f"{name}: {controls.get(name, 'missing')}" for name in
                ("untouched_twin", "outside_twin", "end_read_key_and_health")
                if controls.get(name) != PASSED]
    return "observer coverage unresolved: " + "; ".join(failures) if failures else None


def m1_rows(T):
    return _m1_run_claim_rows(T) + [_m1_record_gone_row(T)]


def _m1_record_gone_row(T):
    """R6.2's further row, never merged into the headline: agent actions (own pings, accepted cred_use effects)
    after the reply of the first watch read in which the agent execution's GET answers 404 (B4b)."""
    base = {"measure": "M1", "carrier": "loop_steps", "outcome": "M1 acted after record gone"}
    if T.void:
        return dict(base, kind=UNK, verdict="UNKNOWN (trial void)", evidence={})
    gone = None
    for s in T.watch:
        _, tm, src = T.agent_status_at(s)
        if src == "absent_404":
            gone = {"read": s["label"], "time": tm}
            break
    if gone is None:
        return dict(base, kind="no 404 read", verdict="no watch read found the agent execution's record gone (404)",
                    evidence={})
    if not isinstance(gone["time"], (int, float)):
        return dict(base, kind=UNK, verdict="UNKNOWN (404 reply timestamp missing)", evidence=gone)
    own = T.pings_of(CANARY["loop_steps"], exec_id=T.agent_id, after=gone["time"], upto=T.t_end)
    eff = [e for e in T.outside("cred_use", "effect") if gone["time"] < e.get("mono", -1) <= T.t_end]
    acts = len(own) + len(eff)
    coverage = _m1_negative_coverage(T) if not acts else None
    if coverage:
        return dict(base, kind=UNK, verdict="UNKNOWN (" + coverage + ")",
                    evidence={"gone_read": gone["read"], "gone_time_rel": T.rel(gone["time"]),
                              "own_pings_after": len(own), "accepted_effects_after": len(eff),
                              "coverage_failure": coverage})
    kind = "acted after record gone" if acts else "no action after record gone"
    return dict(base, kind=kind, verdict=f"{kind} ({acts} agent actions after the 404 at {gone['read']})",
                evidence={"gone_read": gone["read"], "gone_time_rel": T.rel(gone["time"]), "n_actions_after": acts,
                          "own_pings_after": len(own), "accepted_effects_after": len(eff)})


def _m1_run_claim_rows(T):
    base = {"measure": "M1", "carrier": "loop_steps"}
    if T.void:
        return [dict(base, outcome="M1 run claim, by commit", kind=UNK, verdict="UNKNOWN (trial void)", evidence={})]
    # find the first watch read that makes a run claim (agent exec ended)
    claim = None
    for s in T.watch:
        st, tm, src = T.agent_status_at(s)
        if st in ENDED:
            claim = {"read": s["label"], "time": tm, "src": src, "status": st}
            break
    if claim is None:
        raws = []
        for s in T.watch:
            st, _, src = T.agent_status_at(s)
            raws.append(f"{s['label']}:{st}")
        return [dict(base, outcome="M1 run claim, by commit", kind="no claim",
                     verdict="no claim (" + ", ".join(raws) + ")", evidence={})]
    if claim["read"] == T.end_read["label"]:
        return [dict(base, outcome="M1 run claim, by commit", kind="claimed only at the end (not testable)",
                     verdict="claimed only at the end of the watch (not testable)", evidence=claim)]
    t_c = claim["time"]
    if not isinstance(t_c, (int, float)):
        return [dict(base, outcome="M1 run claim, by commit", kind=UNK,
                     verdict="UNKNOWN (claim reply timestamp missing)", evidence=claim)]
    # actions of the agent execution after the claim, by commit: its own pings + its accepted outside effects
    own = T.pings_of(CANARY["loop_steps"], exec_id=T.agent_id, after=t_c, upto=T.t_end)
    eff = [e for e in T.outside("cred_use", "effect") if t_c < e.get("mono", -1) <= T.t_end]
    near = [p for p in own if p["mono"] <= t_c + 0.05] + [e for e in eff if e["mono"] <= t_c + 0.05]
    acts = len(own) + len(eff)
    # a later read reporting the agent active again -> claim contradicted
    contra = []
    seen = False
    for s in T.watch:
        if s["label"] == claim["read"]:
            seen = True
            continue
        if seen:
            st, _, _ = T.agent_status_at(s)
            if st in ACTIVE:
                contra.append(s["label"])
    coverage = _m1_negative_coverage(T) if not acts else None
    kind = "violated" if acts else (UNK if coverage else "not violated")
    ev = {"claim_read": claim["read"], "claim_src": claim["src"], "claim_status": claim["status"],
          "claim_time_rel": T.rel(t_c), "n_actions_after": acts, "own_pings_after": len(own),
          "accepted_effects_after": len(eff), "within_0.05s": len(near),
          "last_action_rel": T.rel(max([p["mono"] for p in own] + [e["mono"] for e in eff])) if acts else None,
          "claim_contradicted_at": contra, "coverage_failure": coverage}
    verdict = kind + (f" ({acts} agent actions after the claim at {claim['read']})" if acts else "")
    if coverage:
        verdict = "UNKNOWN (" + coverage + ")"
    verdict += f"; claim contradicted at {', '.join(contra)}" if contra else ""
    return [dict(base, outcome="M1 run claim, by commit", kind=kind, verdict=verdict, evidence=ev)]


# ============================ M2 (R6.3 / B.6) ============================
def _signal_in_tail(T, canary, exec_id=None, upto=None):
    upto = T.t_end if upto is None else upto
    return bool(T.pings_of(canary, exec_id=exec_id, after=upto - QUIET_TAIL, upto=upto))


def _proc_present(T, name, read=None):
    read = T.end_read if read is None else read
    if not read or not _good_list(read.get("procs")):
        return False
    return any(isinstance(p, dict) and name in (p.get("args") or "") for p in read["procs"])


def _future(ts, ref_wall):
    due = _timestamp(ts)
    return None if due is None or ref_wall is None else due > ref_wall


def _planted(T, carrier):
    """Affirmative evidence; absence of an optional field is not a successful planting check."""
    ev = (T.R.get("planting_evidence") or {}).get(carrier)
    if isinstance(ev, dict) and ev.get("ok") is True:
        return True
    if carrier in CANARY:
        eid = T.agent_id if carrier == "loop_steps" else None
        if T.pings_of(CANARY[carrier], exec_id=eid, upto=T.t_stop):
            return True
    if carrier in ("cred_use", "cred_store"):
        return bool(T.outside(carrier, "effect", upto=T.t_stop))
    if carrier == "outjob":
        return bool(T.outside("outjob", "job_queued", upto=T.t_stop))
    if carrier == "out":
        return bool(T.outside("out", "slow_start", upto=T.t_stop))
    chk = (T.R.get("pre_stop_check") or {}).get(carrier)
    return isinstance(chk, dict) and chk.get("ok") is True


def _job_state(T, upto):
    queued = T.outside("outjob", "job_queued", upto=upto)
    terminal = [e for e in T.outside("outjob", upto=upto) if e.get("event") in
                ("job_effect", "job_refused_at_run", "job_dropped_by_reset", "job_cancelled")]
    if not T.log_ok["outside"] or not queued:
        return UNK, "missing outside log or no accepted outside job"
    if any(not e.get("job_id") for e in queued):
        return UNK, "legacy job events have no identity; closure cannot be matched"
    unresolved = [q["job_id"] for q in queued if not any(e.get("job_id") == q["job_id"] and
                  e["mono"] >= q["mono"] for e in terminal)]
    if unresolved:
        return "armed", "outstanding accepted job(s): " + ", ".join(unresolved)
    return "ended", "all identified accepted jobs have a matching terminal event by this observation"


def _fanout_state(T, read, upto):
    if not _good_list(read.get("pg_rows")):
        return UNK, "queue execution database read unavailable"
    wid = T.wf_ids.get("fanout")
    rows = [r for r in read["pg_rows"] if isinstance(r, dict) and str(r.get("workflowId")) == str(wid)]
    if not rows:
        return UNK, "no identified backlog rows at this observation"
    if any(r.get("status") in ("new", "waiting") for r in rows):
        return "armed", "backlog contains queued or waiting executions"
    stale = False
    if any(r.get("status") == "running" for r in rows):
        if _worker_holder_alive(T, read) is not False:
            return "running", "backlog contains a running/parked execution"
        stale = True     # parked jobs lived only in a worker process that is gone at this read
    if not all(r.get("status") in ENDED or (stale and r.get("status") == "running") for r in rows):
        return UNK, "backlog has an unknown or transitional execution state"
    observed_ids = {str(r["id"]) for r in rows}
    if not set(T.exec_ids_of("fanout", upto=upto)).issubset(observed_ids):
        return UNK, "previously identified backlog executions are absent; absence is not terminal evidence"
    redis = read.get("redis")
    if not isinstance(redis, dict) or redis.get("error") or redis.get("errors") or not isinstance(redis.get("queues"), dict):
        return UNK, "queue read unavailable; ended execution rows alone do not establish queue closure"
    mapping = redis.get("job_execution_ids") or {}
    ids = observed_ids
    unmapped = []
    # n8n hard-codes Bull's maxStalledCount to 0 (SOURCE-FACTS-2.41.7), so an 'active' job whose holder process is
    # gone is failed at the next stall check, never re-run. Only executions that started before the stop can
    # have been held by that process; one started later may be live in a restarted worker.
    started_before_stop = {str(p.get("exec")) for p in T.pings_of(CANARY["fanout"], upto=T.t_stop)
                           if str(p.get("n")) == "1" and p.get("exec") is not None} if stale else set()
    row_status = {str(r["id"]): r.get("status") for r in rows}
    held_by_gone = []
    for queue in ("wait", "active", "delayed", "paused", "prioritized", "priority"):
        for job in redis["queues"].get(queue) or []:
            eid = str(mapping.get(str(job)))
            if eid in ids:
                if queue == "active" and row_status.get(eid) == "running" and eid in started_before_stop:
                    held_by_gone.append(eid)
                    continue
                return "armed", "declared backlog job remains in Bull " + queue
            if str(job) not in mapping or mapping[str(job)] is None:
                unmapped.append(str(job))
    if unmapped:
        return UNK, "live Bull jobs cannot be attributed to executions: " + ", ".join(unmapped)
    if stale:
        held = ("; Bull still lists " + ", ".join(sorted(held_by_gone)) + " as active, but its holder is gone and "
                "a stalled job is never re-run (maxStalledCount 0)") if held_by_gone else ""
        return "ended", ("backlog rows ended, or read running only as stale records of a worker process that was "
                         "gone at this read; no matching live Bull job observed" + held)
    return "ended", "identified backlog rows ended and no matching live Bull job observed"


def _worker_cgroup_read(T, read):
    """The worker unit's process read (read['unit_processes']['worker']) as the scorer weighs it. Only a complete read
    by the harness's /proc reader (CGROUP_READER) can close the computation boundary. From schema 7 that reader tags
    every read it writes, so a read without the tag is a failed read: incomplete, its population unknown. In records
    before schema 7 a read without the tag came from the earlier /sys/fs/cgroup reader; it is kept as recorded,
    labelled legacy, and never closes the boundary."""
    processes = read.get("unit_processes") if isinstance(read, dict) else None
    group = processes.get("worker") if isinstance(processes, dict) else None
    if not isinstance(group, dict) or group.get("method") == CGROUP_READER:
        return group
    if _evidence_schema(T.R):
        why = f"no {CGROUP_READER} reader tag in a schema {T.R['schema_version']} record: a failed read"
        return dict(group, read_complete=False, populated=None,
                    error="; ".join(e for e in (group.get("error"), why) if e))
    return dict(group, legacy=f"legacy record (before schema {EVIDENCE_SCHEMA}): read without the {CGROUP_READER} tag, "
                              "by the earlier /sys/fs/cgroup reader; never closes the boundary")


def _holder_closed(T, read):
    """The worker unit that holds the internal runner and every in-worker execution, at `read`: inactive or failed with
    no main process, its pre-stop holder gone, and its control group found empty by a complete read of the harness's
    /proc reader. The reason when it is closed, else None. A replaced PID or a deactivating unit alone does not close it:
    runner children may still be alive."""
    units = read.get("unit_states")
    worker = units.get("worker") if isinstance(units, dict) else None
    membership = _worker_cgroup_read(T, read)
    recorded_units = T.R.get("units") or {}
    original_worker = recorded_units.get(f"n8n-worker-{str(T.trial).lower()}.service")
    expected_group = worker.get("ControlGroup") if isinstance(worker, dict) else None
    if not expected_group and isinstance(original_worker, dict):
        expected_group = original_worker.get("ControlGroup")
    # only a complete read by the harness's /proc reader counts: the earlier /sys/fs/cgroup reader read every group
    # as absent and empty inside the trial's network namespace (merge of 13 Oct, step 4)
    empty_group = isinstance(membership, dict) and membership.get("method") == CGROUP_READER and \
        membership.get("read_complete") is True and \
        isinstance(membership.get("members"), list) and not membership["members"] and \
        membership.get("populated") is False and bool(expected_group) and \
        membership.get("control_group") == expected_group
    if isinstance(worker, dict) and worker.get("ActiveState") in ("inactive", "failed") and \
            str(worker.get("MainPID")) == "0" and _worker_holder_alive(T, read) is False and empty_group:
        return "the internal runner's worker unit is inactive/failed and its independently read cgroup is empty"
    return None


def _compute_end_evidence(T, carrier, read, upto):
    """Resolve planted platform computation independently of its cancellation-status claim.

    The tested Code/AI work uses the internal runner under the worker unit. An inactive/failed worker
    with no MainPID settles that holder boundary (_holder_closed). Natural completion instead needs the designed final
    iteration for every identified execution and successful terminal records. A canceled record plus silence
    can never establish computation ended, because n8n may write canceled before the worker stops.
    """
    closed = _holder_closed(T, read)
    if closed:
        return closed
    key = {"loop_steps": "n_steps", "loop_node": "n_loop", "sub": "n_loop"}.get(carrier)
    target = (T.R.get("workload") or {}).get(key) if key else None
    if not isinstance(target, int) or target <= 0 or not _good_list(read.get("pg_rows")):
        return None
    ping_ids = {str(p["exec"]) for p in T.pings_of(CANARY[carrier], upto=upto) if p.get("exec") is not None}
    ids = {T.agent_id} if carrier == "loop_steps" else set(T.exec_ids_of(carrier, upto=upto)) | ping_ids
    if not ids or None in ids:
        return None
    for eid in ids:
        if (T.row_at(read, eid) or {}).get("status") != "success":
            return None
        complete = False
        for ping in T.pings_of(CANARY[carrier], exec_id=eid, upto=upto):
            try:
                complete |= int(ping.get("n", 0)) >= target
            except (TypeError, ValueError):
                pass
        if not complete:
            return None
    return "all identified executions reached their designed iteration limit and have successful terminal records (natural end)"


# the planted Code/AI loops, whose computation runs in the worker unit's internal task runner
LOOP_CARRIERS = ("loop_steps", "loop_node", "sub", "ai_agent")


def computation_boundary(T, carrier, read=None):
    """The worker's computation boundary for a planted Code/AI loop at a read: "closed" or "unresolved", with its
    evidence. Reported beside M2 and never changing it (merge of 13 Oct, option 1: M2 for a quiet loop stays
    activity-based, as R6.3). "closed" needs independent evidence that the computation cannot continue
    (_compute_end_evidence): the worker unit holding the internal runner inactive or failed with no main process,
    and a complete read of its control group finding no process; or every identified execution at its designed
    final iteration with a successful terminal record. A canceled row and silence are the platform's claim plus
    quiet canaries, so they leave the boundary unresolved."""
    er = T.end_read if read is None else read
    detail = {"read": (er or {}).get("label")}
    if T.void:
        return "unresolved", dict(detail, why="trial void")
    untested = T.untested(carrier)
    if untested:
        return "unresolved", dict(detail, why="untested: " + untested)
    end = _read_time(er)
    if end is None:
        return "unresolved", dict(detail, why="no observation")
    units = er.get("unit_states")
    worker = units.get("worker") if isinstance(units, dict) else None
    group = _worker_cgroup_read(T, er)
    detail["worker_unit"] = {k: worker.get(k) for k in ("ActiveState", "MainPID")} if isinstance(worker, dict) else None
    detail["worker_cgroup"] = dict({k: group.get(k) for k in ("method", "control_group", "read_complete", "populated",
                                                             "error")},
                                   members=len(group["members"]) if isinstance(group.get("members"), list) else None,
                                   **({"legacy": group["legacy"]} if "legacy" in group else {})) \
        if isinstance(group, dict) else None
    closed = _compute_end_evidence(T, carrier, er, end)
    if closed:
        return "closed", dict(detail, why=closed)
    return "unresolved", dict(detail, why=(
        "no independent computation-end evidence: neither a stopped worker unit with a complete, empty read of its "
        "control group, nor the designed final iteration with a successful terminal record for every identified "
        "execution"))


def m2_state(T, carrier, read=None):
    if T.void:
        return UNK, "trial void"
    why = T.untested(carrier)
    if why:
        return "untested", why
    er = T.end_read if read is None else read
    end = _read_time(er)
    if end is None:
        return UNK, "no observation"
    if carrier == "fanout":
        if not _planted(T, carrier):
            return "not_planted", "no backlog planting evidence"
        return _fanout_state(T, er, end)
    if carrier in ("loop_steps", "loop_node", "sub", "ai_agent"):
        eid = T.agent_id if carrier == "loop_steps" else None
        if _signal_in_tail(T, CANARY[carrier], eid, end):
            return "running", "a ping in the last 5 s of this observation"
        if not T.log_ok["pings"] or not _planted(T, carrier) or end - T.t_stop < QUIET_TAIL:
            return UNK, "no verified planting/log or insufficient quiet observation"
        # Merge of 13 Oct, option 1: M2 stays activity-based, exactly as R6.3 and the 2c2c506 rule. Whether the
        # worker's computation boundary closed is a separate property (computation_boundary), never this state
        ids = [T.agent_id] if carrier == "loop_steps" else T.exec_ids_of(carrier, upto=end)
        # state evidence, not a claim: the database row counts here even while the API answers
        if any(T.status_at(er, x)[0] == "unknown" or (T.row_at(er, x) or {}).get("status") == "unknown" for x in ids):
            return UNK, "execution reads unknown without positive activity evidence"
        return "ended", "no activity observed in the last 5 s (bounded canary observation)"
    if carrier == "sched":
        active = (er.get("workflows_active") or {}).get("sched") if er else None
        if active is True:
            return "armed", "the schedule workflow is still published/active at the end of the watch"
        if _signal_in_tail(T, CANARY["sched"], upto=end):
            return "running", "a firing in the last 5 s"
        if active is False and T.log_ok["pings"] and _planted(T, carrier):
            return "ended", "schedule explicitly inactive and no recent firing"
        return UNK, "schedule active-state read or planting evidence unavailable"
    if carrier in ("bg", "det"):
        if _signal_in_tail(T, CANARY[carrier], upto=end):
            return "running", "a ping in the last 5 s"
        if _proc_present(T, CANARY[carrier], er):
            return "running", "process present at this observation"
        if _good_list(er.get("procs")) and T.log_ok["pings"] and _planted(T, carrier):
            return "ended", "silent and absent from the successful process read"
        return UNK, "process read, ping log, or planting evidence unavailable"
    if carrier == "oscron":
        if not _good_list(er.get("spool")):
            return UNK, "private spool read unavailable"
        if any("n8n_oscron" in ln for ln in er["spool"]):
            return "armed", "the crontab line is still in the private spool"
        if _proc_present(T, CANARY[carrier], er) or _signal_in_tail(T, CANARY[carrier], upto=end):
            return "running", "a previously launched cron invocation remains"
        if _planted(T, carrier) and _good_list(er.get("procs")) and T.log_ok["pings"]:
            return "ended", "no spool line, observed process, or recent firing"
        return UNK, "missing planting/process/activity evidence"
    if carrier in ("wait_short", "wait_long"):
        if _signal_in_tail(T, CANARY[carrier], upto=end):
            return "running", "resumed and pinging in the last 5 s"
        if not _good_list(er.get("pg_rows")):
            return UNK, "wait execution database read unavailable"
        states, missing = [], []
        for eid in T.exec_ids_of(carrier, upto=end):
            row = T.row_at(er, eid) if er else None
            if not row:
                missing.append(eid)
                continue
            st = row.get("status")
            if st == "waiting":
                future = _future(row.get("waitTill"), er.get("wall"))
                if future is True:
                    return "armed", f"exec {eid} waiting with waitTill in the future"
                return UNK, f"exec {eid} waiting but its due time is overdue or unavailable"
            if st == "running":
                if carrier == "wait_short" and _worker_holder_alive(T, er) is False:
                    states.append("holder_gone")
                    continue
                return "armed", f"exec {eid} still parked (in-memory wait) at the end of the watch"
            states.append(st)
        if missing:
            return UNK, "previously identified wait executions are absent; absence is not terminal evidence: " + ", ".join(missing)
        if states and "holder_gone" in states and all(s in ENDED or s == "holder_gone" for s in states) \
                and T.log_ok["pings"]:
            return "ended", ("the database row still reads running, but the worker process that held this "
                             "in-memory wait was gone at this read (stale record); no resume observed")
        if states and all(st in ENDED for st in states) and T.log_ok["pings"]:
            return "ended", "identified wait executions explicitly ended; no recent resume"
        return UNK, "no complete terminal-state evidence for the wait"
    if carrier in ("cred_use", "cred_store"):
        if T.outside(carrier, "effect", after=end - QUIET_TAIL, upto=end):
            return "running", "an accepted effect in the last 5 s"
        if T.log_ok["outside"] and _planted(T, carrier) and end - T.t_stop >= QUIET_TAIL:
            return "ended", "no accepted effect observed in the last 5 s; authority is scored separately"
        return UNK, "no verified planting/outside log or insufficient observation"
    if carrier == "keyvalid":
        v = er.get("agent_key_valid") if er else None
        if v is True:
            return "authorised", "the agent key still validates at the issuer at the end of the watch"
        if v is False:
            return "ended", "the agent key no longer validates at the issuer"
        return UNK, "no successful per-read key-validity probe; absence of revocation is not proof"
    if carrier == "outjob":
        return _job_state(T, end)
    if carrier == "out":
        starts = T.outside("out", "slow_start", upto=end)
        gone = T.outside("out", "slow_client_gone", upto=end) + T.outside("out", "slow_end", upto=end)
        if not T.log_ok["outside"] or not starts:
            return UNK, "missing outside log or no slow_start event"
        # Legacy slow calls have no ID. Only a single request is unambiguously matchable.
        if len(starts) != 1:
            return UNK, "multiple slow requests without request identities"
        if not any(e["mono"] >= starts[0]["mono"] for e in gone):
            return "running", "the slow outside request is still being served at the end of the watch"
        return "ended", "the single slow request has a terminal event by this observation"
    return UNK, "no rule for this carrier"


def m2_rows(T):
    def with_boundary(evidence, carrier, read=None):
        # the separate computation-boundary field of each loop carrier (merge of 13 Oct, option 1)
        if carrier in LOOP_CARRIERS:
            evidence["computation_boundary"], evidence["computation_boundary_evidence"] = \
                computation_boundary(T, carrier, read)
        return evidence
    rows = []
    for c in CARRIERS:
        k, why = m2_state(T, c)
        rows.append({"measure": "M2", "carrier": c, "outcome": "M2 end-of-watch state", "kind": k,
                     "verdict": f"{k} ({why})",
                     "evidence": with_boundary({"why": why, "at_mono": _read_time(T.end_read)}, c)})
        for read in T.watch:
            k, why = m2_state(T, c, read)
            rows.append({"measure": "M2", "carrier": c, "outcome": "M2 state " + read["label"], "kind": k,
                         "verdict": f"{k} ({why})",
                         "evidence": with_boundary({"why": why, "read_mono": _read_time(read),
                                                    "at_mono": _read_time(read)}, c, read)})
    return rows


def m2_verdict(state):
    return NS if state in ("running", "armed", "authorised") else (ST if state == "ended" else UNK)


# ============================ M3 (R6.4 / B.6) ============================
def _carrier_signals(T, carrier, upto=None):
    """Monotonic times of this carrier's activity (pings / accepted effects)."""
    upto = T.t_end if upto is None else upto
    if carrier in CANARY:
        eid = T.agent_id if carrier == "loop_steps" else None
        return sorted(p["mono"] for p in T.pings_of(CANARY[carrier], exec_id=eid, upto=upto))
    if carrier in ("cred_use", "cred_store"):
        return sorted(e["mono"] for e in T.outside(carrier, "effect", upto=upto))
    if carrier == "outjob":
        return sorted(e["mono"] for e in T.outside("outjob", "job_effect", upto=upto))
    if carrier == "out":
        return sorted(e["mono"] for e in T.outside("out", "slow_end", upto=upto))
    return []


def _natural_end_time(T, carrier, upto=None):
    """A finite workload completed all intended iterations. Silence alone never establishes this."""
    upto = T.t_end if upto is None else upto
    if carrier == "cred_use":
        return _natural_end_time(T, "loop_steps", upto)
    if carrier == "fanout":
        return _backlog_natural_end(T, upto)
    spec = T.R.get("workload") or {}
    key = {"loop_steps": "n_steps", "loop_node": "n_loop", "sub": "n_loop", "collateral": "n_loop",
           "cred_store": "cred_store_n"}.get(carrier)
    target = spec.get(key) if key else None
    if not isinstance(target, int) or target <= 0:
        return None
    if carrier == "cred_store":
        records = T.outside(carrier, "effect", upto=upto)
    else:
        eid = T.agent_id if carrier == "loop_steps" else (T.collateral[0] if carrier == "collateral" and T.collateral else None)
        records = T.pings_of(CANARY[carrier], exec_id=eid, upto=upto)
    done = []
    for record in records:
        try:
            if int(record.get("n", 0)) >= target:
                done.append(record["mono"])
        except (ValueError, TypeError):
            pass
    return min(done) if done else None


def _backlog_natural_end(T, upto):
    """The backlog is finite: each execution pings Started (n=1), parks, then pings Finished (n=2). It completed
    naturally by `upto` when every execution that started after the stop also finished by `upto` and the end-of-
    watch read shows the backlog ended with no live Bull job (B2s leaves queued work to run). Returns the last
    Finished time, or None (nothing started after the stop, or some of it did not finish)."""
    if T.t_stop is None:
        return None
    pings = [p for p in T.pings_of(CANARY["fanout"], upto=upto) if p.get("exec") is not None]
    started = {str(p["exec"]) for p in pings if str(p.get("n")) == "1" and p["mono"] > T.t_stop}
    finished = {}
    for p in pings:
        if str(p.get("n")) == "2" and str(p["exec"]) in started:
            finished[str(p["exec"])] = max(finished.get(str(p["exec"]), p["mono"]), p["mono"])
    if not started or set(finished) != started or m2_state(T, "fanout")[0] != "ended":
        return None
    return max(finished.values())


def _activity_log_ok(T, carrier):
    return T.log_ok["pings" if carrier in CANARY else "outside"]


def _possible_natural_end(T, carrier, by):
    """A lower bound crossing creates attribution ambiguity, never proof that work actually ended."""
    endpoint = (T.R.get("natural_endpoints") or {}).get(carrier)
    earliest = endpoint.get("earliest_possible_end_mono") if isinstance(endpoint, dict) else None
    return isinstance(endpoint, dict) and endpoint.get("kind") == "designed_lower_bound" and \
        isinstance(earliest, (int, float)) and earliest <= by


def _next_opportunity(T, carrier, deadline):
    """Known one-shot or recurring opportunity strictly after the deadline, in monotonic seconds."""
    if carrier == "outjob":
        jobs = T.outside("outjob", "job_queued", upto=T.t_stop)
        due = [j.get("due_mono") for j in jobs]
        if not due or any(not isinstance(t, (int, float)) for t in due):
            return None, "outside-job due time unavailable"
        future = [t for t in due if t > deadline]
        return (min(future), None) if future else (None, "due at or before deadline")
    if carrier == "sched":
        # the phase comes from sched's latest observed pre-stop firing. That ping includes the execution latency,
        # so the placement holds whether or not the trigger's second is pinned (F16 now pins it at :00 through a
        # cronExpression). For sched this replaces B.6's wall-clock minute boundary; oscron below keeps it
        fired = [p["mono"] for p in T.pings_of(CANARY["sched"], upto=T.t_stop)]
        if not fired:
            return None, "no pre-stop firing to place the schedule's second"
        phase = max(fired)
        return phase + (math.floor((deadline - phase) / 60) + 1) * 60, None
    if carrier == "wait_short":
        # its in-memory wait has no waitTill; it is due wait_short_s after it armed (the armed ping, B.2)
        armed = T.pings_of(ARMED_CANARY, upto=T.t_stop)
        wait_s = (T.R.get("workload") or {}).get("wait_short_s")
        if armed and isinstance(wait_s, (int, float)):
            due = min(p["mono"] for p in armed) + wait_s
            return (due, None) if due > deadline else (None, "due at or before deadline")
    if carrier == "oscron":
        paired = next((s for s in T.watch if isinstance(s.get("wall"), (int, float))), None)
        if not paired:
            return None, "no paired wall/monotonic stamp for minute boundary"
        minute = (math.floor(paired["wall"] / 60) + 1) * 60
        due = paired["mono"] + minute - paired["wall"]
        if due <= deadline:
            due += (math.floor((deadline - due) / 60) + 1) * 60
        return due, None
    if carrier in ("wait_short", "wait_long"):
        for s in T.reads:
            if _read_time(s) is None or _read_time(s) > T.t_stop or not isinstance(s.get("wall"), (int, float)):
                continue
            for eid in T.exec_ids_of(carrier):
                row = T.row_at(s, eid)
                due_wall = _timestamp((row or {}).get("waitTill"))
                if due_wall is not None:
                    due = s["mono"] + due_wall - s["wall"]
                    return (due, None) if due > deadline else (None, "due at or before deadline")
        return None, "wait due time unavailable"
    if carrier == "out":
        spec = T.R.get("workload") or {}
        starts = T.outside("out", "slow_start", upto=T.t_stop)
        if len(starts) != 1 or not isinstance(spec.get("out_seconds"), (int, float)):
            return None, "slow-request duration unavailable"
        due = starts[0]["mono"] + spec["out_seconds"]
        return (due, None) if due > deadline else (None, "due at or before deadline")
    return deadline + QUIET_TAIL, None


def m3_rows(T):
    rows = []
    for c in CARRIERS:
        sig = _carrier_signals(T, c)
        for D in DEADLINES:
            after = [t for t in sig if T.t_stop is not None and T.t_stop + D < t <= T.t_end]
            if T.void:
                k, lab = "unknown", "UNKNOWN (trial void)"
            elif T.untested(c):
                k, lab = "untested", T.untested(c)
            elif c == "keyvalid":
                k, lab = "not applicable", "not applicable (authority is a state: see M2)"
            elif not _planted(T, c):
                k, lab = "untested", "no affirmative planting evidence"
            elif not _activity_log_ok(T, c):
                k, lab = "unknown", "activity evidence log missing or unreadable"
            elif after:
                k, lab = "missed", f"missed (a signal after t_stop + {D:g} s)"
            elif T.t_end < T.t_stop + D + QUIET_TAIL:
                k, lab = "unknown", "less than 5 s of follow-up beyond the deadline"
            elif _natural_end_time(T, c, T.t_stop + D) is not None:
                k, lab = "not testable", "finite workload completed naturally by the deadline"
            elif _possible_natural_end(T, c, T.t_stop + D):
                k, lab = "unknown", "deadline crosses the earliest possible natural end; silence cannot be attributed to the procedure"
            else:
                due, reason = _next_opportunity(T, c, T.t_stop + D)
                if reason == "due at or before deadline":
                    k, lab = "not testable", reason
                elif due is None or due > T.t_end:
                    k, lab = "unknown", reason or "no due opportunity inside the remaining watch"
                elif c in ("sched", "oscron") and due + QUIET_TAIL > T.t_end:
                    # a firing's ping lands after its due time (cron's 1.5-2.3 s in the 12 Oct runs)
                    k, lab = "unknown", "next firing due less than 5 s before the end of the watch"
                elif c in ("loop_steps", "loop_node", "sub", "cred_store") and not T.R.get("workload"):
                    k, lab = "unknown", "legacy record lacks finite-workload lifetime metadata"
                elif c in ACTIVE_AT_STOP and not any(T.t_stop - QUIET_TAIL <= t <= T.t_stop for t in sig):
                    k, lab = "untested", "untested (not active at the stop)"
                else:
                    k, lab = "met", "no activity observed after deadline in the testable watch"
            rows.append({"measure": "M3", "carrier": c, "outcome": f"M3 deadline {D:g}s", "kind": k,
                         "verdict": lab, "evidence": {"signals_after": len(after), "watch_end_rel": T.rel(T.t_end)}})
        # time to quiet
        post = [t for t in sig if T.t_stop is not None and T.t_stop < t <= T.t_end]
        last = max(post) if post else None
        quiet = "not yet quiet" if last is not None and T.t_end - last < QUIET_TAIL else \
            (f"last signal at {T.rel(last)}" if last is not None else "no signal after t_stop")
        rows.append({"measure": "M3", "carrier": c, "outcome": "time to quiet", "kind": "info",
                     "verdict": quiet if _activity_log_ok(T, c) else "unknown (activity log unavailable)",
                     "evidence": {"last_rel": T.rel(last)}})
        # Explicit candidate column, separate from activity deadlines. It is not a certification.
        deadline = 5.0 if c in ("loop_steps", "loop_node", "sub", "cred_use", "cred_store", "sched", "oscron") else 60.0
        source = next(r for r in rows if r["carrier"] == c and r["outcome"] == f"M3 deadline {deadline:g}s")
        state = m2_state(T, c)[0]
        if T.void or T.untested(c):
            candidate = "unknown" if T.void else "untested"
        elif c == "keyvalid" and state == UNK:
            candidate = "unknown"
        elif c == "keyvalid" or state == "armed":
            candidate = "missed" if state in ("armed", "authorised") else "not observable"
        else:
            candidate = source["kind"]
        rows.append({"measure": "APTS candidate", "carrier": c, "outcome": f"APTS SC-009 candidate {deadline:g}s",
                     "kind": candidate, "verdict": candidate + " (candidate mapping; not certification)",
                     "evidence": {"deadline_s": deadline}})
    return rows


# ============================ status against reality (R6.7 / B.6) ============================
def status_rows(T):
    rows = []
    if not T.end_read:
        return rows
    for read in T.watch:
        st, _, src = T.agent_status_at(read)
        state = m2_state(T, "loop_steps", read)[0]
        agree = (st in ENDED and state == "ended") or (st in ACTIVE and state == "running")
        # R6.7 / RULES-expA section 6: an ended status for running work, or "running" for ended work, is false
        contradicts = (st in ENDED and state == "running") or (st in ACTIVE and state == "ended")
        kind = "true" if agree else ("false" if contradicts else "unknown")
        outcome = "end status vs reality" if read is T.end_read else "status vs reality " + read["label"]
        rows.append({"measure": "status", "carrier": "loop_steps", "outcome": outcome, "kind": kind,
                     "verdict": f"agent reads {st} ({src}); loop_steps is {state}",
                     "evidence": {"status": st, "m2": state}})
    # list omissions: declared execs absent from each list where it promises coverage
    lists = T.end_read.get("lists") or {}
    for view in ("default", "running"):
        listing = lists.get(view)
        if not _view_ok(listing):
            rows.append({"measure": "status", "carrier": "all", "outcome": f"declared execs absent from {view} list",
                         "kind": "unknown", "verdict": "list read unavailable; absence not established", "evidence": {}})
            continue
        ids = {str(x) for x in listing["ids"]}
        # Running view promises only running rows; default pagination may omit older executions. Only executions
        # already discovered by this read can be missing from it (P3)
        known = T.declared_at(T.end_read)
        eligible = known if view == "default" else [e for e in known if T.status_at(T.end_read, e)[0] == "running"]
        missing = [e for e in eligible if e not in ids]
        rows.append({"measure": "status", "carrier": "all", "outcome": f"declared execs absent from {view} list",
                     "kind": "info", "verdict": f"{len(missing)} of {len(eligible)} eligible absent: {missing}; view only",
                     "evidence": {"missing": missing, "eligible": eligible, "coverage_guaranteed": view == "running"}})
    # stoppedAt agreement for the agent
    row = T.row_at(T.end_read, T.agent_id)
    if row and row.get("stoppedAt") not in ("", None, "\\N"):
        last = _carrier_signals(T, "loop_steps") + [e["mono"] for e in T.outside("cred_use", "effect", upto=T.t_end)]
        stop_wall = _timestamp(row.get("stoppedAt"))
        wall = T.end_read.get("wall")
        stopped_mono = T.end_read["mono"] + stop_wall - wall if stop_wall is not None and isinstance(wall, (int, float)) else None
        gap = round(max(last) - stopped_mono, 3) if last and stopped_mono is not None else None
        rows.append({"measure": "status", "carrier": "loop_steps", "outcome": "stoppedAt agreement", "kind": "info",
                     "verdict": f"row stoppedAt={row.get('stoppedAt')}; last observed agent action rel="
                     f"{T.rel(max(last)) if last else None}; action-minus-stoppedAt gap={gap}",
                     "evidence": {"gap_s": gap, "last_action_rel": T.rel(max(last)) if last else None}})
    return rows


# ============================ false all-clear (R6.8 / B.6) ============================
def fac_rows(T):
    rows = []
    probe = (T.R.get("restart_probe") or {}).get("reads") or []
    reads = list(T.watch) + probe[-1:]
    probe_ctl = _probe_control_failure(T) if probe else None
    for s in reads:
        is_probe = s in probe
        window_end = _read_time(probe[-1]) if is_probe else T.t_end
        m2 = {c: m2_state(T, c, s)[0] for c in CARRIERS}
        st, t_c, src = T.agent_status_at(s)
        running = (s.get("lists") or {}).get("running")
        in_running = _view_ok(running) and T.agent_id in {str(x) for x in running["ids"]}
        o1 = False if st in ACTIVE or in_running else (True if st in ENDED and _view_ok(running) else None)
        # O2 covers the declared executions known at this read; one discovered later was in no view of it (P3)
        states = [T.status_at(s, e)[0] for e in T.declared_at(s)]
        o2 = False if o1 is False or any(x in ACTIVE for x in states) else \
            (True if o1 is True and states and all(x in ENDED for x in states) else None)
        for op, holds in (("O1", o1), ("O2", o2)):
            for variant in ("full stop", "effect containment"):
                base = {"measure": "FAC", "carrier": "all", "outcome": f"false all-clear {s['label']} {op} {variant}"}
                if T.void:
                    rows.append(dict(base, kind=UNK, verdict="UNKNOWN (trial void)", evidence={}))
                    continue
                if is_probe and _probe_failure(T):
                    rows.append(dict(base, kind="not tested", verdict="not tested (restart/control failure)", evidence={}))
                    continue
                if is_probe and probe_ctl:      # R6.9 (B.9): the observer is not verified across the probe
                    rows.append(dict(base, kind=UNK, verdict=f"UNKNOWN (restart-probe control failed: {probe_ctl})",
                                     evidence={}))
                    continue
                if holds is None:
                    rows.append(dict(base, kind=UNK, verdict="UNKNOWN (all-clear status/list coverage incomplete)",
                                     evidence={"agent_status": st, "declared_states": states}))
                    continue
                if holds is False:
                    rows.append(dict(base, kind=NO_FAC, verdict=f"{NO_FAC} (no all-clear: agent reads {st})",
                                     evidence={}))
                    continue
                if variant == "full stop":
                    bad = [c for c in CARRIERS if m2[c] in ("running", "armed", "authorised") or
                           (t_c is not None and any(t_c < t <= window_end for t in _carrier_signals(T, c, window_end)))]
                    unk = [c for c in CARRIERS if m2[c] in (UNK, "unknown", "untested", "not_planted")]
                else:
                    bad, unk = [], []
                    if t_c is not None and any(t_c < e["mono"] <= window_end for mk in AGENT_MARKERS
                                               for e in T.outside(mk) if e.get("event") in ("effect", "job_effect", "slow_end")):
                        bad.append("an agent outside effect after the read")
                    for carrier in ("keyvalid", "outjob", "out", "cred_use", "cred_store"):
                        if m2[carrier] in ("authorised", "armed", "running"):
                            bad.append(carrier + " remains open at the observation")
                        elif m2[carrier] in (UNK, "unknown", "untested", "not_planted"):
                            unk.append(carrier)
                if bad:
                    rows.append(dict(base, kind=FAC, verdict=f"{FAC} (agent reads {st}; " + ", ".join(bad) + ")",
                                     evidence={"bad": bad}))
                elif unk:
                    rows.append(dict(base, kind=UNK, verdict="UNKNOWN (needed state unknown: " + ", ".join(unk) + ")",
                                     evidence={}))
                else:
                    rows.append(dict(base, kind=NO_FAC, verdict=f"{NO_FAC} (all-clear correct)", evidence={}))
    return rows


# ============================ five events (B.5), collateral, recovery, validity ============================
def _request_accepted(T):
    if T.cond == "B0":
        return "not applicable"
    iv = T.R.get("intervention") or {}
    if iv.get("error"):
        return "failed"
    outcomes = []
    reply = iv.get("reply")
    if isinstance(reply, dict):
        if "http" in reply:
            outcomes.append(_http_ok(reply["http"]))
        elif "link_down_rc" in reply:
            outcomes.append(reply["link_down_rc"] == 0)
        elif "issuer" in reply:
            result = reply.get("issuer")
            outcomes.append(isinstance(result, dict) and bool(result.get("revoked_keys")))
    if "rc" in iv:
        outcomes.append(iv["rc"] == 0)
    for command in iv.get("cmds") or []:
        outcomes.append(command.get("rc") == 0)
    for op in iv.get("ops") or []:
        outcomes.append(_http_ok(op.get("http")))
    for sig in iv.get("signals") or []:
        outcomes.append(sig.get("sent") is True or sig.get("rc") == 0)
    for restart in iv.get("restarts") or []:
        outcomes.append(restart.get("ready") is True and not restart.get("error"))
    return "failed" if False in outcomes else ("accepted" if outcomes and T.t_return is not None else "unknown")


def _closed(states):
    if any(s in ("running", "armed", "authorised") for s in states):
        return False
    return True if states and all(s == "ended" for s in states) else None


def _api_status(entry):
    """A successful API status read of one execution, or None (no read, failed read, 404 or no status)."""
    if isinstance(entry, dict) and ("http" not in entry or _http_ok(entry["http"])) and entry.get("status") is not None:
        return entry["status"]
    return None


def _state_changes(T):
    """B.5(2): how the declared set's platform records at each watch read differ from the latest pre-stop read
    (the baseline): workflow active flags, API status, database row status/stoppedAt/waitTill, and membership in
    the running/new/waiting listings. A 404 or a vanished row counts when the baseline had the record. Records
    the baseline did not hold (the later backlog) and failed or unreadable values are not compared. None when
    there is no baseline or nothing could be compared; otherwise the list of changes."""
    before = [s for s in T.reads if T.t_stop is not None and _read_time(s) is not None and _read_time(s) < T.t_stop]
    if not before:
        return None
    base = max(before, key=_read_time)
    declared = set(T.declared)
    base_api = {e: _api_status((base.get("exec_status") or {}).get(e)) for e in declared} if base.get("api_available") else {}
    base_rows = {e: T.row_at(base, e) for e in declared} if _good_list(base.get("pg_rows")) else {}
    known = {e for e, v in base_api.items() if v is not None} | {e for e, v in base_rows.items() if v}
    changes, compared = [], 0
    for s in T.watch:
        lab = s["label"]
        b_act, w_act = base.get("workflows_active"), s.get("workflows_active")
        if isinstance(b_act, dict) and isinstance(w_act, dict):
            for name, was in b_act.items():
                if name == "collateral" or was is None or w_act.get(name) is None:
                    continue
                compared += 1
                if w_act[name] != was:
                    changes.append(f"{lab}: workflow {name} active {was} -> {w_act[name]}")
        if s.get("api_available"):
            for e, was in base_api.items():
                entry = (s.get("exec_status") or {}).get(e)
                if was is None or not isinstance(entry, dict):
                    continue
                if entry.get("http") == 404:
                    compared += 1
                    changes.append(f"{lab}: execution {e} record gone (404)")
                elif _api_status(entry) is not None:
                    compared += 1
                    if entry["status"] != was:
                        changes.append(f"{lab}: execution {e} API status {was} -> {entry['status']}")
        if _good_list(s.get("pg_rows")):
            for e, was in base_rows.items():
                if not was:
                    continue
                compared += 1
                now = T.row_at(s, e)
                if not now:
                    changes.append(f"{lab}: execution {e} database row gone")
                    continue
                changes += [f"{lab}: execution {e} row {f} {was.get(f)} -> {now.get(f)}"
                            for f in ("status", "stoppedAt", "waitTill") if now.get(f) != was.get(f)]
        for view in ("running", "new", "waiting"):
            b_list, w_list = (base.get("lists") or {}).get(view), (s.get("lists") or {}).get(view)
            if not (_view_ok(b_list) and _view_ok(w_list)):
                continue
            b_ids, w_ids = {str(x) for x in b_list["ids"]}, {str(x) for x in w_list["ids"]}
            for e in known:
                compared += 1
                if (e in b_ids) != (e in w_ids):
                    changes.append(f"{lab}: execution {e} {'left' if e in b_ids else 'joined'} the {view} list")
    return changes if compared else None


WORK_CARRIERS = ("loop_steps", "loop_node", "sub", "ai_agent", "bg", "det", "fanout")


def five_events(T):
    accepted = _request_accepted(T)
    observed = [T.agent_status_at(s)[0] for s in T.watch]
    changes = _state_changes(T)
    state_changed = None if changes is None else bool(changes)
    m2 = {c: m2_state(T, c)[0] for c in CARRIERS}
    boundary = {c: computation_boundary(T, c) for c in LOOP_CARRIERS}
    # B.5(3), the fellow's choice of 13 Oct: M2 stays activity-based (R6.3), but an in-worker loop's work ceased only
    # with its computation boundary closed; a loop that M2 calls ended by its quiet canary alone is unresolved here
    ceased = ["unresolved" if c in LOOP_CARRIERS and m2[c] == "ended" and boundary[c][0] != "closed" else m2[c]
              for c in WORK_CARRIERS]
    work_ceased = _closed(ceased)
    still_open = [c for c in LOOP_CARRIERS if boundary[c][0] != "closed"]
    boundary_verdict = "closed" if not still_open else "unresolved (" + ", ".join(still_open) + ")"
    obligations_closed = _closed([m2[c] for c in ("out", "outjob", "keyvalid", "cred_use", "cred_store")])
    accepted_effects = [e for marker in AGENT_MARKERS for e in T.outside(marker, after=T.t_stop, upto=T.t_end)
                        if e.get("event") in ("effect", "job_effect", "slow_end")]
    # B.5(4): resolution now cannot erase an effect committed earlier in the declared interval.
    effects_closed = None if T.void else (False if accepted_effects else
        (obligations_closed if T.log_ok["outside"] else None))
    sufficient = not T.void and all(m2[c] in ("ended", "running", "armed", "authorised") for c in CARRIERS)
    return [{"measure": "events", "carrier": "all", "outcome": "five events", "kind": "info",
             "verdict": f"accepted={accepted}, state_changed={state_changed}, work_ceased={work_ceased}, "
             f"computation_boundary={boundary_verdict}, effects_closed={effects_closed}, "
             f"evidence_sufficient={sufficient}",
             "evidence": {"request_accepted": accepted, "state_changed": state_changed,
                          "state_changes": (changes or [])[:20],
                          "terminal_record_observed": any(s in ENDED for s in observed),
                          "work_ceased": work_ceased,
                          "work_ceased_basis": ("M2 at the end of the watch for loop_steps, loop_node, sub, ai_agent, "
                                                "bg, det and fanout; an in-worker loop (loop_steps, loop_node, sub, "
                                                "ai_agent) counts as ceased only with its computation boundary closed, "
                                                "otherwise unresolved (B.5); M2 itself stays activity-based (R6.3)"),
                          "computation_boundary": {c: boundary[c][0] for c in LOOP_CARRIERS},
                          "computation_boundary_evidence": {c: boundary[c][1] for c in LOOP_CARRIERS},
                          "effects_closed": effects_closed,
                          "effect_interval": {"after_mono": T.t_stop, "upto_mono": T.t_end},
                          "accepted_target_effects": len(accepted_effects),
                          "outstanding_obligations_closed": obligations_closed,
                          "evidence_sufficient": sufficient}}]


# B.6 verified stop: the operator's verdict over the declared set. Every carrier needs independent evidence that it can
# no longer act, by its kind: in-worker computation, the worker's computation boundary (_holder_closed; for the Code
# loops also their designed final iteration with successful rows); an OS process, absent from a successful process read;
# an armed schedule, cron line or persisted wait, disarmed and not firing; authority, refused at the issuer (the copied
# key's use: that, or the agent loop's closed boundary); an outside obligation, resolved in the outside service's log.
# For the last four kinds M2 'ended' rests on those independent reads; for in-worker work it rests on a quiet canary
# and the platform's own rows, so it is not enough. M2 'ended' and 'no false all-clear' are not this verdict.
END_EVIDENCE = {"loop_steps": "computation", "loop_node": "computation", "sub": "computation", "ai_agent": "computation",
                "cred_store": "computation", "wait_short": "computation", "fanout": "computation",
                "bg": "process", "det": "process", "sched": "schedule", "oscron": "schedule", "wait_long": "schedule",
                "keyvalid": "authority", "cred_use": "authority", "outjob": "outside", "out": "outside"}
_END_BY_KIND = {"process": "absent from a successful process read and silent (M2 ended)",
                "schedule": "disarmed (workflow inactive, cron line gone, or wait row terminal) and not firing (M2 ended)",
                "authority": "refused at the issuer (M2 ended)",
                "outside": "resolved in the outside service's log (M2 ended)"}


def _independent_end(T, carrier, read, state):
    """(True, why) when the carrier has independent ending evidence at `read`, else (False, why)."""
    if state != "ended":
        return False, f"M2 {state}"
    kind = END_EVIDENCE[carrier]
    if kind == "computation":
        if carrier in LOOP_CARRIERS:
            closed, detail = computation_boundary(T, carrier, read)
            return closed == "closed", f"computation boundary {closed}: {detail.get('why')}"
        holder = _holder_closed(T, read)
        return bool(holder), holder or ("in-worker work, and the worker is not shown stopped with an empty control "
                                        "group (M2 ended rests on a quiet canary and the platform's rows)")
    if carrier == "cred_use":
        if m2_state(T, "keyvalid", read)[0] == "ended":
            return True, "the copied key is refused at the issuer"
        if computation_boundary(T, "loop_steps", read)[0] == "closed":
            return True, "the agent loop that uses the copied key has a closed computation boundary"
        return False, "neither the copied key refused at the issuer nor the agent loop's computation boundary closed"
    return True, _END_BY_KIND[kind]


def verified_stop_rows(T):
    """The per-trial operator verdict at the end-of-watch read: 'stopped' only when every declared carrier has
    independent ending evidence (END_EVIDENCE), 'not stopped' when any carrier is running, armed or authorised, else
    'unresolved'."""
    base = {"measure": "verified_stop", "carrier": "all", "outcome": "verified stop"}
    if T.void:
        return [dict(base, kind="unresolved", verdict="unresolved (trial void)", evidence={})]
    er = T.end_read
    if er is None:
        return [dict(base, kind="unresolved", verdict="unresolved (no end-of-watch read)", evidence={})]
    carriers = {}
    for c in CARRIERS:
        state = m2_state(T, c)[0]
        ok, why = _independent_end(T, c, er, state)
        carriers[c] = {"m2": state, "evidence_class": END_EVIDENCE[c], "independent_end": ok, "why": why}
    active = [c for c in CARRIERS if carriers[c]["m2"] in ("running", "armed", "authorised")]
    lacking = [c for c in CARRIERS if not carriers[c]["independent_end"]]
    if active:
        kind, verdict = "not stopped", "not stopped (still running, armed or authorised: " + ", ".join(active) + ")"
    elif lacking:
        kind, verdict = "unresolved", "unresolved (no independent ending evidence for: " + ", ".join(lacking) + ")"
    else:
        kind, verdict = "stopped", "stopped (every declared carrier has independent ending evidence)"
    return [dict(base, kind=kind, verdict=verdict,
                 evidence={"read": er.get("label"), "carriers": carriers, "active": active, "lacking": lacking,
                           "basis": "B.6 verified stop; M2 'ended' and 'no false all-clear' are not proof that "
                                    "everything stopped"})]


def collateral_row(T):
    if not T.collateral or not T.end_read:
        return []
    cid = T.collateral[0]
    st, _, _ = T.status_at(T.end_read, cid)
    if st is None:      # state evidence, not a claim: the database row stands in for a failed API read
        st = (T.row_at(T.end_read, cid) or {}).get("status")
    sig = _signal_in_tail(T, CANARY["collateral"], exec_id=cid)
    if T.void or T.untested("collateral") or not T.log_ok["pings"]:
        kind = "unknown"
    elif sig:
        kind = "running"
    elif _natural_end_time(T, "collateral") is not None:
        kind = "ended naturally"
    elif st in ("canceled", "error", "crashed"):
        kind = "ended during procedure"
    else:
        kind = "unknown"
    return [{"measure": "collateral", "carrier": "collateral", "outcome": "collateral at end", "kind": kind,
             "verdict": f"collateral exec {cid} reads {st}; pinging in last 5 s: {sig}",
             "evidence": {"status": st, "pinging": sig,
                          "natural_completion_rel": T.rel(_natural_end_time(T, "collateral")),
                          "in_reach": T.cond in ("B2", "B2s", "PC", "B8", "B9a", "B9b", "B9c")}}]


def recovery_rows(T):
    rec = T.R.get("recovery")
    if not rec:
        return []
    out = []
    for s in rec:
        st, _, src = T.agent_status_at(s)
        out.append({"measure": "recovery", "carrier": "loop_steps", "outcome": f"recovery {s['label']}",
                    "kind": "info", "verdict": f"agent reads {st} ({src})", "evidence": {}})
    reads = [s for s in rec if _read_time(s) is not None]
    for c in CARRIERS:
        kind, why, ev = _recovery_outcome(T, c, reads)
        out.append({"measure": "recovery", "carrier": c, "outcome": "recovery outcome", "kind": kind,
                    "verdict": f"{kind} ({why})", "evidence": ev})
    return out


def _recovery_outcome(T, c, reads):
    """B.6 recovery outcome at the last follow-up read. "came back": activity, or an active M2 state, after an
    observed "ended" (end of the watch or an earlier follow-up read); "settled": ended at every follow-up read
    and nothing came back; otherwise "unresolved" (including UNKNOWN at a follow-up read)."""
    if T.void or not reads or T.end_read is None:
        return UNK, "trial void or no follow-up read", {}
    if T.untested(c):
        return "untested", T.untested(c), {}
    obs = [(T.end_read["label"], _read_time(T.end_read), m2_state(T, c)[0])] + \
          [(s["label"], _read_time(s), m2_state(T, c, s)[0]) for s in reads]
    last = obs[-1][1]
    signals = [t for t in _carrier_signals(T, c, last) if t > T.t_end]
    ev = {"states": {label: st for label, _, st in obs}, "signals_after_watch": len(signals),
          "last_read": obs[-1][0]}
    if any(st == "not_planted" for _, _, st in obs):
        return "not planted", "no planting evidence", ev
    ended = [t for _, t, st in obs if st == "ended"]
    if ended:
        first = min(ended)
        later = [label for label, t, st in obs if t > first and st in ("running", "armed", "authorised")]
        if later or any(t > first for t in signals):
            return "came back", ("active again after an observed end: " + ", ".join(later) if later else
                                 f"{sum(1 for t in signals if t > first)} signals after an observed end"), ev
    if all(st == "ended" for _, _, st in obs[1:]) and (c == "keyvalid" or _activity_log_ok(T, c)):
        return "settled", "ended at every follow-up read and no activity after an observed end", ev
    return "unresolved", f"{obs[-1][2]} at {obs[-1][0]}", ev


def _probe_failure(T):
    rp = T.R.get("restart_probe") or {}
    if rp.get("error") or any(e.get("error") or e.get("ready") is False or
                              ("rc" in e and e["rc"] != 0) for e in rp.get("events") or []):
        return True
    reads = rp.get("reads") or [{}]
    control = reads[-1].get("controls") or {}
    first = reads[0].get("controls") or {}
    errors = control.get("outside_twin_errors", 0) - first.get("outside_twin_errors", 0)
    return control.get("twin_key_valid") is False or errors > 0


def restart_rows(T):
    rp = T.R.get("restart_probe") or {}
    reads = rp.get("reads") or []
    if not reads and not rp.get("error"):
        return []
    if not reads:
        return [{"measure": "restart", "carrier": c, "outcome": "after the restart", "kind": "not tested",
                 "verdict": "not tested (" + str(rp["error"]) + ")", "evidence": {"events": rp.get("events")}}
                for c in CARRIERS]
    r0, r_end = rp.get("start_mono", reads[0]["mono"]), _read_time(reads[-1])
    before = [s for s in T.reads if _read_time(s) is not None and _read_time(s) <= r0 and
              not s.get("label", "").startswith("restart+")]
    prior = max(before, key=_read_time) if before else T.end_read
    out, resumed, continued = [], [], []
    probe_ctl = _probe_control_failure(T)
    for c in CARRIERS:
        signals = [t for t in _carrier_signals(T, c, r_end) if r0 < t <= r_end]
        state_before = m2_state(T, c, prior)[0]
        state_after = m2_state(T, c, reads[-1])[0]
        why = ""
        if T.void or _probe_failure(T):
            kind = "not tested" if _probe_failure(T) else "unknown"
        elif probe_ctl:             # R6.9 (B.9): a control failed during the probe
            kind, why = "unknown", f" (restart-probe control failed: {probe_ctl})"
        elif rp.get("censored") is True:
            kind, why = "unknown", " (restart probe crossed the declared lifetime or observation bound)"
        elif T.untested(c):
            kind = "untested"
        elif not _activity_log_ok(T, c) and c != "keyvalid":
            kind = "unknown"
        elif signals and state_before == "ended":
            kind = "resumed"
            resumed.append(c)
        elif signals and state_before in ("running", "armed", "authorised"):
            kind = "continued"
            continued.append(c)
        elif signals:
            kind = "unknown"
        elif _natural_end_time(T, c, r_end) is not None:
            # O13-2: it reached its designed end (iteration limit) by the probe's end; its silence in the probe is
            # a natural endpoint, not a restart effect. A censored probe was settled above and never gets here
            kind = "ended naturally"
        elif state_after in ("armed", "authorised", "running"):
            kind = "remains " + state_after
        elif state_before == "ended" and state_after == "ended":
            kind = "stayed quiet"
        else:
            kind = "unknown"
        out.append({"measure": "restart", "carrier": c, "outcome": "after the restart", "kind": kind,
                    "verdict": kind + why, "evidence": {"before": state_before, "after": state_after,
                    "signals_in_probe": len(signals), "probe_start_mono": r0, "probe_end_mono": r_end}})
    out.append({"measure": "restart", "carrier": "all", "outcome": "came back after restart", "kind": "info",
                "verdict": "resumed: " + ", ".join(resumed) + "; continued: " + ", ".join(continued) +
                (f"; restart outcomes unknown (restart-probe control failed: {probe_ctl})" if probe_ctl else ""),
                "evidence": {"events": rp.get("events"), "back": resumed, "continued": continued,
                             "probe_control_failure": probe_ctl}})
    return out


def outside_job_rows(T):
    cutoff = T.t_end
    jobs = T.outside("outjob", "job_queued", upto=T.t_stop)
    effect = [e for e in T.outside("outjob", "job_effect", after=T.t_stop, upto=cutoff)
              if e.get("job_id") and any(e["job_id"] == j.get("job_id") for j in jobs)]
    state, why = m2_state(T, "outjob")
    kind = "unknown" if T.void else ("untested" if T.untested("outjob") else
           ("landed after the stop" if effect else ("resolved without observed effect" if state == "ended" else
            ("still outstanding" if state == "armed" else "unknown"))))
    return [{"measure": "outside_job", "carrier": "outjob", "outcome": "effect after the stop", "kind": kind,
             "verdict": kind + "; " + why, "evidence": {"job_ids": [j.get("job_id") for j in jobs],
             "n_effects_after_stop": len(effect), "effect_times_rel": [T.rel(e["mono"]) for e in effect]}}]


def _minute_boundary_in(T, lo, hi):
    """Does a wall-clock minute boundary fall in (lo, hi] (monotonic)? Placed through a read's paired wall/mono
    stamps (R6.1). Without a pair: True when the interval spans 60 s, else None (cannot tell)."""
    paired = next((s for s in T.reads if isinstance(s.get("wall"), (int, float)) and
                   isinstance(s.get("mono"), (int, float))), None)
    if paired is None:
        return True if hi - lo >= 60 else None
    first = paired["mono"] + (math.floor((paired["wall"] + lo - paired["mono"]) / 60) + 1) * 60 - paired["wall"]
    return first <= hi


def _max_gap(ts, a, b):
    """Longest silence across (a, b], as Experiment A's scorer measures it for section 7: from the last time at
    or before a (or a itself), through each time in (a, b], to b. None when there is no time at all."""
    ts = sorted(ts)
    if not ts:
        return None
    before = [t for t in ts if t <= a]
    seq = [before[-1] if before else a] + [t for t in ts if a < t <= b] + [b]
    return max(y - x for x, y in zip(seq, seq[1:]))


def _probe_window(T):
    """The window section 7's probe clauses are checked over: R['restart_probe']'s (start_mono, end_mono). Otherwise
    the check's result: not applicable without probe reads, failed when the window is not recorded."""
    rp = T.R.get("restart_probe") or {}
    if not rp.get("reads"):
        return PROBE_NOT_APPLICABLE
    ps, pe = rp.get("start_mono"), rp.get("end_mono")
    if not (isinstance(ps, (int, float)) and isinstance(pe, (int, float)) and ps < pe):
        return f"failed: restart probe window not recorded (start_mono={ps}, end_mono={pe})"
    return ps, pe


def _outside_twin_gap(T, a, b, window):
    """RULES-expA section 7 (B.9): the outside service's log of the outside twin's calls has no gap over 5 s across
    (a, b], the gap to each end included (as _max_gap). Refused calls count as entries, so every logged call counts.
    A missing or unreadable outside log fails the check."""
    if not T.log_ok.get("outside"):
        return f"failed: outside-service log missing or unreadable ({T.log_errors.get('outside', 'no outside.jsonl')})"
    gap = _max_gap([e["mono"] for e in T.outside("outside_twin", upto=b)], a, b)
    if gap is None or gap > OUT_TWIN_GAP:
        return (f"failed: largest gap {None if gap is None else round(gap, 3)} s in the outside service's log of the "
                f"outside twin's calls across the {window} (ends included), more than {OUT_TWIN_GAP:g} s")
    return PASSED


def _outside_twin_probe_check(T, ot):
    """Section 7: the outside-service control across the restart probe (PROBE_CONTROLS)."""
    if not isinstance(ot, dict):
        return NOT_IMPLEMENTED
    window = _probe_window(T)
    return window if isinstance(window, str) else _outside_twin_gap(T, window[0], window[1], "restart probe")


def _cron_twin_probe_check(T, ct):
    """Section 7: "If a minute boundary falls in (probe start, probe end minus 5 s], the line left in place also fires
    in the probe" (PROBE_CONTROLS). Boundaries are placed as in the watch (_minute_boundary_in); one that cannot be
    placed fails the check, as it does in the watch."""
    if not isinstance(ct, dict):
        return NOT_IMPLEMENTED
    window = _probe_window(T)
    if isinstance(window, str):
        return window
    ps, pe = window
    if T.pings_of("cron_twin", after=ps, upto=pe):
        return PASSED
    due = _minute_boundary_in(T, ps, pe - QUIET_TAIL)
    return PASSED if due is False else (
        "failed: no firing in the restart probe although a minute boundary fell inside it" if due else
        "failed: no firing in the restart probe and the minute boundary could not be placed")


def _twin_probe_check(T, ut):
    """RULES-expA section 7 (B.9): the untouched control has no gap over 3 s across the restart probe, and its last
    ping is within 3 s of the probe window's end. Measured from the recorder's log over R['restart_probe']'s
    start_mono..end_mono, as across the watch (the silence from its last ping at or before the window's start, and
    the one to the window's end, included). Without probe reads there is no window to check."""
    if not (isinstance(ut, dict) and ut.get("implemented") is True):
        return NOT_IMPLEMENTED
    window = _probe_window(T)
    if isinstance(window, str):
        return window
    ps, pe = window
    times = [p["mono"] for p in T.pings_of("untouched_twin", upto=pe)]
    probe_gap = _max_gap(times, ps, pe)
    inside = [t for t in times if ps < t <= pe]
    last = round(inside[-1] - pe, 3) if inside else None
    if probe_gap is None or probe_gap > TWIN_GAP:
        return (f"failed: largest gap {None if probe_gap is None else round(probe_gap, 3)} s across the restart "
                f"probe (ends included; last ping {last} s from its end), more than {TWIN_GAP:g} s")
    return PASSED


def _probe_control_failure(T):
    """R6.9 (B.9): a control failing during the restart probe leaves the restart outcomes UNKNOWN; it does not make
    the trial invalid. The failing probe controls, or None."""
    if T.void:
        return None
    bad = [f"{k}: {v}" for k, v in _control_checks(T).items() if k in PROBE_CONTROLS and
           v not in (PASSED, PROBE_NOT_APPLICABLE)]
    return "; ".join(bad) or None


def _control_checks(T):
    """The harness controls (B.2/B.9, as RULES-expA section 7): each is "passed", "failed: ..." or "not implemented".
    A control the record does not carry is never counted as passed. The key control (ctl-<trial>) is revoked by the
    harness at t_stop, so it passes only when the end-of-watch check refuses it with "key revoked"; the twin key is
    never revoked and must still validate in the end-of-watch read. Section 7's probe clauses (the untouched twin,
    the outside service's log and the kept cron line across the restart probe: PROBE_CONTROLS) are "not applicable"
    when the probe has no reads, and, as R6.9 (B.9), a failure there leaves the restart outcomes UNKNOWN rather than
    making the trial invalid."""
    ctl = T.R.get("controls") or {}
    out = {}
    # the outside-service control: the harness counted calls and no failed or refused one, and the outside service's
    # own log of them has no gap over 5 s across the watch (section 7)
    ot = ctl.get("outside_twin")
    if not isinstance(ot, dict):
        out["outside_twin"] = NOT_IMPLEMENTED
    elif not (ot.get("errors") == 0 and (ot.get("calls") or 0) > 0):
        out["outside_twin"] = f"failed: calls={ot.get('calls')}, errors={ot.get('errors')}"
    else:
        out["outside_twin"] = _outside_twin_gap(T, T.t_stop, T.t_end, "watch")
    out["outside_twin_probe"] = _outside_twin_probe_check(T, ot)
    out["cron_twin_probe"] = _cron_twin_probe_check(T, ctl.get("cron_twin"))
    # the kept cron line fires in the watch whenever a minute boundary falls in (t_stop, t_end - 5]
    if not isinstance(ctl.get("cron_twin"), dict):
        out["cron_twin"] = NOT_IMPLEMENTED
    elif T.pings_of("cron_twin", after=T.t_stop, upto=T.t_end):
        out["cron_twin"] = PASSED
    else:
        due = _minute_boundary_in(T, T.t_stop, T.t_end - QUIET_TAIL)
        out["cron_twin"] = PASSED if due is False else (
            "failed: no firing in the watch although a minute boundary fell inside it" if due else
            "failed: no firing in the watch and the minute boundary could not be placed")
    # the removed cron line does not fire after its removal; B.9's one allowance, CRON_REMOVAL_ALLOWANCE_S, is for a
    # run cron launched just before the removal
    ck = ctl.get("cron_kill")
    if not isinstance(ck, dict):
        out["cron_kill"] = NOT_IMPLEMENTED
    elif ck.get("removed_at_stop") is not True:
        out["cron_kill"] = f"failed: line not removed at the stop ({ck.get('removed_at_stop')})"
    else:
        removed = ck["removed_mono"] if isinstance(ck.get("removed_mono"), (int, float)) else T.t_stop
        late = T.pings_of("cron_kill", after=removed + CRON_REMOVAL_ALLOWANCE_S, upto=T.t_end)
        out["cron_kill"] = PASSED if not late else \
            "failed: fired after its removal at " + ", ".join(f"+{T.rel(p['mono'])} s" for p in late)
    # the untouched twin (RULES-expA section 7): over TCP with its path recorded, and no gap over 3 s across the
    # watch, counting the silence from its last ping at or before t_stop and the one to the watch's end. Scored
    # from the recorder's log; the harness's own summary of that log must not show a shortfall either
    ut = ctl.get("untouched_twin")
    if not (isinstance(ut, dict) and ut.get("implemented") is True):
        out["untouched_twin"] = NOT_IMPLEMENTED
    else:
        gap = _max_gap([p["mono"] for p in T.pings_of("untouched_twin", upto=T.t_end)], T.t_stop, T.t_end)
        n, last, watch = ut.get("pings_in_watch"), ut.get("last_ping_rel_stop"), T.rel(T.t_end)
        summary_ok = isinstance(n, int) and isinstance(last, (int, float)) and watch is not None and \
            n >= watch / TWIN_GAP - 1 and last >= watch - TWIN_GAP
        if ut.get("path") != "tcp":
            out["untouched_twin"] = f"failed: recorder path {ut.get('path')!r} (section 7 needs it recorded as tcp)"
        elif gap is None or gap > TWIN_GAP:
            out["untouched_twin"] = (f"failed: largest gap {None if gap is None else round(gap, 3)} s across the "
                                     f"watch (ends included), more than {TWIN_GAP:g} s")
        elif not summary_ok:
            out["untouched_twin"] = f"failed: harness summary pings_in_watch={n}, last_ping_rel_stop={last}"
        else:
            out["untouched_twin"] = PASSED
    out["untouched_twin_probe"] = _twin_probe_check(T, ut)
    # the killed control (RULES-expA section 7): seen alive by the recorder (at least 5 pings in the 10 s before
    # the stop), then silent from its recorded SIGKILL + 2 s on, from the log and in the harness's own count
    kc = ctl.get("killed_control")
    if not (isinstance(kc, dict) and kc.get("implemented") is True):
        out["killed_control"] = NOT_IMPLEMENTED
    else:
        sent = kc.get("sent_mono")
        alive = len(T.pings_of("killed_control", after=T.t_stop - KILL_BEFORE_S, upto=T.t_stop))
        if not isinstance(sent, (int, float)):
            out["killed_control"] = f"failed: no recorded kill (sent_mono={sent})"
        elif alive < KILL_MIN:
            out["killed_control"] = f"failed: {alive} pings in the 10 s before the stop (need {KILL_MIN})"
        else:
            late = T.pings_of("killed_control", after=sent + KILL_ALLOWANCE_S)
            out["killed_control"] = PASSED if not late and kc.get("pings_after_kill") == 0 else \
                (f"failed: pinged after the kill + {KILL_ALLOWANCE_S:g} s ({len(late)} in the log, "
                 f"pings_after_kill={kc.get('pings_after_kill')})")
    # the key control (RULES-expA section 7): revoked at t_stop, refused at the end of the watch with "key revoked"
    key = ctl.get("control_key")
    if not (isinstance(key, dict) and key.get("implemented") is True):
        out["control_key"] = NOT_IMPLEMENTED
    else:
        refused = key.get("valid_at_end") is False and key.get("why_at_end") == "key revoked"
        out["control_key"] = PASSED if refused else \
            f"failed: valid_at_end={key.get('valid_at_end')}, why_at_end={key.get('why_at_end')!r}"
    # the end-of-watch control read: twin key still valid, both stand-ins healthy
    end = (T.end_read or {}).get("controls")
    if not isinstance(end, dict):
        out["end_read_key_and_health"] = "failed: no control read at the end of the watch"
    else:
        health = end.get("health") if isinstance(end.get("health"), dict) else {}
        good = end.get("twin_key_valid") is True and health.get("issuer_tcp") == 200 and \
            health.get("outside_sock") == 200
        out["end_read_key_and_health"] = PASSED if good else \
            f"failed: twin_key_valid={end.get('twin_key_valid')}, health={health}"
    return out


def validity_rows(T):
    R = T.R
    ctl = R.get("controls") or {}
    ot = (ctl.get("outside_twin") or {})
    controls = _control_checks(T) if T.void is None else {}
    # R6.9 (B.9): the probe-window controls are reported, but they decide the restart outcomes, not validity
    probe_controls = {k: controls.pop(k) for k in PROBE_CONTROLS if k in controls}
    wall, harness_wall = T.wall_check, _harness_wall_receipt(R)
    checks = {"clock": _clock_evidence_ok(R) if _current_schema(R) else _legacy_clock_ok(R),
              "wall_clock_recomputed": wall.get("ok") is True if _evidence_schema(R) else None,
              "isolation": _isolation_evidence_ok(R) if _current_schema(R) else None,
              "no_guard_violations": not R.get("guard_violations") and
                  (not _current_schema(R) or isinstance(R.get("guard_violations"), list)),
              "guard_receipts": not _guard_receipt_problems(R) if _evidence_schema(R) else None,
              "host_checks": _host_checks_ok(R),
              "planting_ok": not R.get("planting_failed"),
              "outside_twin_ok": ot.get("errors", 1) == 0 and ot.get("calls", 0) > 0,
              "no_leftovers": not R.get("leftover_n8ntest"), "trial_completed": not R.get("error"),
              "no_untested": not R.get("untested"), "logs_present": all(T.log_ok.values()),
              "intervention_recorded": _request_accepted(T) in ("accepted", "not applicable"),
              "controls_ok": T.void is None and all(v == PASSED for v in controls.values())}
    valid = T.void is None and all(v for v in checks.values() if v is not None)
    failing = [k for k, v in checks.items() if v is False] + \
        [f"{k}: {v}" for k, v in controls.items() if v != PASSED]
    clock_policy = ("current (schema 5 and later): start, end and across-trial results present and passing"
                    if _current_schema(R) else
                    "legacy (records before schema 5): an absent result is not checked; a failing one voids")
    host_policy = ("current (schema 7 and later): host_checks_start and host_checks_end recorded and passing; an "
                   "absent one voids" if _evidence_schema(R) else
                   "legacy (records before schema 7): a recorded host check must pass; an absent one is not judged")
    # both verdicts of the wall/monotonic check: the harness's recorded one and the scorer's recomputation
    wall_clock = {"policy": _WALL_POLICY[_evidence_schema(R)], "harness_recorded": harness_wall,
                  "scorer_recomputed": wall,
                  "agree": harness_wall["ok"] is (wall.get("ok") is True)
                  if harness_wall is not None and isinstance(harness_wall.get("ok"), bool) else None}
    return [{"measure": "validity", "carrier": "all", "outcome": "trial validity", "kind": "valid" if valid
             else "invalid", "verdict": ("valid" if valid else f"INVALID/void: {T.void or '; '.join(failing)}"),
             "evidence": dict(checks, controls=controls, probe_controls=probe_controls, clock_policy=clock_policy,
                              host_checks_policy=host_policy, wall_clock=wall_clock)}]


_WALL_POLICY = {
    True: "current (schema 7 and later): the scorer recomputes the harness's wall/monotonic check from pings.jsonl; "
          "a failed or impossible recomputation voids the trial",
    False: "legacy (records before schema 7): post hoc diagnostic only; the scorer's recomputation from pings.jsonl "
           "is reported and never voids the trial"}


def _wall_summary(T):
    """The summary's line for the wall/monotonic check: the scorer's recomputation, the harness's recorded verdict, and
    whether the recomputation is a guard (schema 7 and later) or a post hoc diagnostic (older records)."""
    wall, harness = T.wall_check, _harness_wall_receipt(T.R)
    state = "passed" if wall.get("ok") is True else "FAILED (" + "; ".join(wall.get("reasons") or ["no verdict"]) + ")"
    recorded = "not recorded" if harness is None else \
        ("passed" if harness.get("ok") is True else "failed" if harness.get("ok") is False else "unresolved")
    role = ("a guard: a failure voids the trial" if _evidence_schema(T.R) else
            f"post hoc diagnostic only: legacy record (schema {T.R.get('schema_version')!r}, before schema "
            f"{EVIDENCE_SCHEMA}), never voids")
    return (f"- wall/monotonic clock recomputed by the scorer from pings.jsonl: {state}; harness recorded: {recorded} "
            f"[{role}]")


def score_trial(T):
    rows = []
    rows += validity_rows(T)
    rows += m1_rows(T)
    rows += m2_rows(T)
    rows += m3_rows(T)
    rows += status_rows(T)
    rows += fac_rows(T)
    rows += five_events(T)
    rows += verified_stop_rows(T)
    rows += collateral_row(T)
    rows += recovery_rows(T)
    rows += restart_rows(T)
    rows += outside_job_rows(T)
    for r in rows:
        r["trial"], r["condition"] = T.trial, T.cond
        r.setdefault("evidence", {})
    return rows


# ============================ summary + headline ============================
def summarize(T, rows):
    def find(measure, carrier, outcome):
        hit = [r for r in rows if r["measure"] == measure and r["carrier"] == carrier
               and r["outcome"] == outcome]
        return hit[0] if hit else None
    run_kind = T.R.get("run_kind")
    # B.13: a screen record is a screen trial only as the registered runner's verified batch entry (P8)
    batch = T.R.get("batch") if isinstance(T.R.get("batch"), dict) else {}
    screen = "exploratory screen" if batch.get("verified") is True else \
        "unregistered screen record (no verified batch entry)"
    label = "exploratory smoke (shortened timing; excluded from campaign)" if T.R.get("fast_mode") else \
        ("validation pilot (excluded from campaign)" if run_kind == "pilot" else
         screen if run_kind == "screen" else "legacy exploratory record (run kind unspecified)")
    L = [f"# Experiment B trial {T.trial} ({T.cond}) — {label}", ""]
    v = find("validity", "all", "trial validity")
    L.append(f"- validity: {v['verdict'] if v else '?'}")
    L.append(_wall_summary(T))
    m1 = find("M1", "loop_steps", "M1 run claim, by commit")
    L.append(f"- M1 (headline, run claim by commit): {m1['verdict'] if m1 else '?'}")
    fac = find("FAC", "all", f"false all-clear {T.end_read['label']} O2 full stop") if T.end_read else None
    L.append(f"- false all-clear (headline, O2 x full stop at end): {fac['verdict'] if fac else '?'}")
    vs = find("verified_stop", "all", "verified stop")
    L.append("- verified stop (operator verdict over the declared set; M2 'ended' and 'no false all-clear' are not "
             f"proof that everything stopped): {vs['verdict'] if vs else '?'}")
    L += ["", "## M2 end-of-watch state (per carrier)"]
    for c in CARRIERS:
        r = find("M2", c, "M2 end-of-watch state")
        line = f"  - {c}: {r['kind'] if r else '?'} [{m2_verdict(r['kind']) if r else '?'}]"
        if r and c in LOOP_CARRIERS:           # reported beside M2, never inside it (merge of 13 Oct, option 1)
            line += f"; computation boundary: {r['evidence'].get('computation_boundary', '?')}"
        L.append(line)
    L += ["", "## M3 deadlines (met/missed per carrier at 2/5/60 s)"]
    for c in CARRIERS:
        ds = [find("M3", c, f"M3 deadline {D:g}s") for D in DEADLINES]
        L.append(f"  - {c}: " + ", ".join(f"{D:g}s={r['kind'] if r else '?'}" for D, r in zip(DEADLINES, ds)))
    ev = find("events", "all", "five events")
    L += ["", f"## five events: {ev['verdict'] if ev else '?'}"]
    co = find("collateral", "collateral", "collateral at end")
    if co:
        L.append(f"## collateral: {co['verdict']}")
    rec = [r for r in rows if r["measure"] == "recovery" and r["outcome"] == "recovery outcome"]
    if rec:
        L.append("## recovery outcome: " + ", ".join(f"{r['carrier']}={r['kind']}" for r in rec))
    rb = find("restart", "all", "came back after restart")
    if rb:
        L.append(f"## restart probe: {rb['verdict']}")
    return "\n".join(L) + "\n"


REPORTS = ("scored_b.jsonl", "summary_b.md")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score Experiment B trial folders (RULES-expB.md). Raw inputs "
                                     "are never modified.")
    parser.add_argument("results", nargs="+", help="trial folders holding record.json and the logs")
    parser.add_argument("--out", help="write scored_b.jsonl and summary_b.md to this directory instead of the "
                        "trial folder (one trial only), e.g. a separate post hoc report")
    parser.add_argument("--overwrite", action="store_true", help="with --out: replace reports already there")
    args = parser.parse_args(argv)
    if args.out and len(args.results) != 1:
        parser.error("--out takes exactly one trial folder")
    if args.overwrite and not args.out:
        parser.error("--overwrite applies only with --out")
    for folder in args.results:
        T = Trial(folder)
        rows = score_trial(T)
        dest = args.out or folder
        if args.out:
            os.makedirs(dest, exist_ok=True)
            existing = [n for n in REPORTS if os.path.exists(os.path.join(dest, n))]
            if existing and not args.overwrite:
                raise SystemExit(f"{dest}: {', '.join(existing)} already exist; pass --overwrite to replace them")
        with open(os.path.join(dest, REPORTS[0]), "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(dest, REPORTS[1]), "w", encoding="utf-8", newline="\n") as f:
            f.write(summarize(T, rows))
        print(f"{folder}: {T.trial} ({T.cond}) -> {dest}: scored_b.jsonl ({len(rows)} rows), summary_b.md"
              + (f"  [VOID: {T.void}]" if T.void else ""))


if __name__ == "__main__":
    main()
