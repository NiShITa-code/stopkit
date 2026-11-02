"""Scenarios for the sixth-revision regression tests (RULES-expA.md R6.x).

Each scenario edits one round 1 trial (synth.Scenario) and names the scored rows it is about. The expected value
of each row is NOT computed here: it is written independently, from the rules and the scenario's fact sheet
(facts.py), into expected_rev6.json, and test_rev6.py compares the scorer with it."""
from synth import Scenario

S = {}  # name -> (build function, [(row, carrier, outcome), ...], one-line purpose)


def scenario(name, rows, purpose):
    def deco(fn):
        S[name] = (fn, rows, purpose)
        return fn
    return deco


M1 = lambda lvl, ver: ("M1", "run", f"M1 {lvl}, by {ver}")  # noqa: E731
M1_ALL = [M1(l, v) for l in ("run claim", "unit claim", "record gone (404)") for v in ("commit", "initiation")]


# ---------------------------------------------------------------- M1 (R6.2)
@scenario("A_claim_then_act_sender", M1_ALL, "M1 with sender stamps: one more step and two accepted calls after "
          "an 'interrupted' read")
def a():
    s = Scenario("XA-X-L1-t1")
    s.stamp_all()
    s.set_read("status_right_after", reply_rel=0.06)
    s.set_read("status_after_5s", reply_rel=5.05)
    s.set_read("status_end", reply_rel=90.05)
    for p in s.pings:  # the two attempts of step 12 get start stamps
        if p.get("canary") == "attempt" and str(p.get("n")) == "12":
            p["smono_init"] = f"{p['mono'] - 0.008:.6f}"
    return s


@scenario("B_claim_nothing_after", M1_ALL, "M1: the run stops before the claim")
def b():
    s = Scenario("XA-X-L1-t1")
    s.drop_pings("parent", after_rel=0.0)
    s.drop_pings("attempt", after_rel=0.0)
    s.drop_outside("key_use", "effect", after_rel=0.0)
    s.drop_outside("tok_use", "effect", after_rel=0.0)
    s.stamp_all()
    s.set_read("status_right_after", reply_rel=0.06)
    s.set_read("status_after_5s", reply_rel=5.05)
    s.set_read("status_end", reply_rel=90.05)
    return s


@scenario("C_claim_late", M1_ALL + [("verdict", "own", "C")], "M1: the first claim is the +5 s read; the run went "
          "quiet at +3 s. M3 for own at 2, 5 and 60 s")
def c():
    s = Scenario("XA-X-L1-t1")
    s.drop_pings("parent", after_rel=0.0)
    s.drop_pings("attempt", after_rel=0.0)
    s.drop_outside("key_use", "effect", after_rel=0.0)
    s.drop_outside("tok_use", "effect", after_rel=0.0)
    for k in (1, 2, 3):
        s.add_ping("parent", k + 0.002, n=11 + k)
    s.stamp_all()
    s.set_read("status_right_after", run="running", thread="busy", reply_rel=0.06)
    s.set_read("status_after_5s", run="interrupted", thread="interrupted", reply_rel=5.30)
    s.set_read("status_end", reply_rel=90.05)
    return s


def _quiet_after_stop(base):
    s = Scenario(base)
    s.drop_pings("parent", after_rel=0.0)
    s.drop_pings("attempt", after_rel=0.0)
    s.drop_outside("key_use", "effect", after_rel=0.0)
    s.drop_outside("tok_use", "effect", after_rel=0.0)
    return s


@scenario("D1_transit_inside", [M1("run claim", "initiation"), M1("run claim", "commit")],
          "M1 on arrival times (round 1): one ping arriving 0.049 s after the claim bound")
def d1():
    s = _quiet_after_stop("XA-X-L1-t1")
    b = s.R["host_right_after"]["t"] - s.t0
    s.add_ping("parent", b + 0.049, n=12, run=s.R["ids"]["parent_run"])
    return s


@scenario("D2_transit_outside", [M1("run claim", "initiation"), M1("run claim", "commit")],
          "M1 on arrival times: one ping arriving 0.051 s after the claim bound")
def d2():
    s = _quiet_after_stop("XA-X-L1-t1")
    b = s.R["host_right_after"]["t"] - s.t0
    s.add_ping("parent", b + 0.051, n=12, run=s.R["ids"]["parent_run"])
    return s


@scenario("A2_at_claim_time", [M1("run claim", "initiation"), M1("run claim", "commit")],
          "M1 with sender stamps: one ping sent exactly at the claim time (arriving 2 ms later)")
def a2():
    s = _quiet_after_stop("XA-X-L1-t1")
    s.stamp_all()
    s.set_read("status_right_after", reply_rel=0.10)
    s.set_read("status_after_5s", reply_rel=5.05)
    s.set_read("status_end", reply_rel=90.05)
    s.add_ping("parent", 0.102, sender_rel=0.10, n=12, run=s.R["ids"]["parent_run"])
    return s


@scenario("A3_one_ms_after", [M1("run claim", "initiation"), M1("run claim", "commit")],
          "M1 with sender stamps: one ping sent 1 ms after the claim time")
def a3():
    s = _quiet_after_stop("XA-X-L1-t1")
    s.stamp_all()
    s.set_read("status_right_after", reply_rel=0.10)
    s.set_read("status_after_5s", reply_rel=5.05)
    s.set_read("status_end", reply_rel=90.05)
    s.add_ping("parent", 0.103, sender_rel=0.101, n=12, run=s.R["ids"]["parent_run"])
    return s


@scenario("A4_refused_then_accepted", M1_ALL[:4] + [("verdict", "key_use", "A"), ("verdict", "key_use", "A-effect")],
          "M1 and R6.6 pairing: after the claim, a refused key call, then an accepted one a step later")
def a4():
    s = _quiet_after_stop("XA-X-L1-t1")
    s.stamp_all()
    s.set_read("status_right_after", reply_rel=0.06)
    s.set_read("status_after_5s", reply_rel=5.05)
    s.set_read("status_end", reply_rel=90.05)
    rid = s.R["ids"]["parent_run"]
    for n, (t, result, event) in ((12, (0.30, "refused", "refused")), (13, (1.30, "accepted", "effect"))):
        s.add_ping("parent", t, sender_rel=t - 0.002, n=n, run=rid)
        s.add_outside(event, "key_use", t + 0.010, credential="k", why="key valid" if event == "effect" else "test")
        s.add_ping("attempt", t + 0.015, sender_rel=t + 0.013, n=n, run=rid, marker="key_use", result=result,
                   smono_init=t + 0.005)
    return s


@scenario("C2_claim_contradicted", M1_ALL[:4], "M1: 'interrupted' right after the stop, then 'running' and "
          "'busy' at the +5 s read")
def c2():
    s = Scenario("XA-X-L1-t1")
    s.stamp_all()
    s.set_read("status_right_after", reply_rel=0.06)
    s.set_read("status_after_5s", run="running", thread="busy", reply_rel=5.05)
    s.set_read("status_end", reply_rel=90.05)
    return s


@scenario("G2_unit_claim_only_at_end", M1_ALL[:4], "M1: run 'interrupted' with thread 'busy' until the end read, "
          "where the run reads 'success' and the thread 'idle' (late cancel)")
def g2():
    return Scenario("XA-X-S2-t1")


@scenario("E_record_gone", M1_ALL, "M1: run and thread answer 404 while the agent keeps acting (delete thread)")
def e():
    return Scenario("XA-X-L3-t1")


@scenario("F_server_down", M1_ALL, "M1: no read inside the watch answers (SIGTERM to the server)")
def f():
    return Scenario("XA-X-L4-t1")


@scenario("G_split_read", M1_ALL, "M1: the run reads 'interrupted' while the thread reads 'busy' (cancel-all)")
def g():
    return Scenario("XA-X-S1-t1")


# ---------------------------------------------------------------- M2 (R6.2)
_M2 = ["own", "sep", "pcron", "bg", "det", "sleeper", "oscron", "keyvalid", "outjob_s", "outjob_r", "out"]


@scenario("H1_end_states", [("M2", c, "M2 end-of-watch state") for c in _M2], "M2 for every carrier after cancel")
def h1():
    return Scenario("XA-X-L1-t1")


@scenario("H2_sleeper_silent", [("M2", "sleeper", "M2 end-of-watch state"), ("verdict", "sleeper", "C")],
          "M2: the dormant process is alive but has sent nothing since +20 s")
def h2():
    s = Scenario("XA-X-L1-t1")
    s.drop_pings("sleeper", after_rel=20.0)
    return s


@scenario("H3_credential_probe_failed", [("M2", "keyvalid", "M2 end-of-watch state")],
          "M2: the end-of-watch credential probe got no HTTP status")
def h3():
    s = Scenario("XA-X-L1-t1")
    for c in ("key_in_state", "token_in_state", "key_in_file"):
        d = s.R["credentials_at_end"][c]
        d["status"], d["accepted"], d["error"] = None, None, "ConnectionRefusedError"
    return s


# ---------------------------------------------------------------- M3 (R6.2, R6.3, R6.4)
@scenario("I1_quiet_at_3s", [("verdict", "own", "C"), ("verdict", "own", "A")], "M3: own's last ping at +3 s")
def i1():
    s = Scenario("XA-X-L1-t1")
    s.drop_pings("parent", after_rel=0.0)
    s.add_ping("parent", 3.0, n=12)
    return s


@scenario("I2_quiet_at_30s", [("verdict", "own", "C"), ("verdict", "own", "A")], "M3: own pings every second "
          "until +30 s")
def i2():
    s = Scenario("XA-X-L1-t1")
    s.drop_pings("parent", after_rel=0.0)
    for k in range(1, 31):
        s.add_ping("parent", float(k), n=11 + k)
    return s


@scenario("J_job_due_before_60", [("verdict", "outjob_s", "E"), ("verdict", "outjob_r", "E")],
          "M3: outside jobs that take effect about 45 to 50 s after the stop")
def j():
    return Scenario("XA-X-L1-t1")


@scenario("K_schedule_armed_not_fired", [("verdict", "pcron", "A"), ("verdict", "pcron", "armed")],
          "R6.3: the platform schedule never fires in the watch (server down) but is armed")
def k():
    return Scenario("XA-X-L4-t1")


@scenario("L1_attempts_refused", [("verdict", "key_use", "A"), ("verdict", "key_use", "A-effect"),
                                  ("verdict", "tok_use", "A"), ("verdict", "tok_use", "A-effect")],
          "R6.4: after revoking key and tokens the agent keeps trying; every call is refused")
def l1():
    return Scenario("XA-X-KEYC-t1")


@scenario("L2_token_still_works", [("verdict", "key_use", "A"), ("verdict", "key_use", "A-effect"),
                                   ("verdict", "tok_use", "A"), ("verdict", "tok_use", "A-effect")],
          "R6.4: after revoking the key only, key calls are refused and token calls accepted")
def l2():
    return Scenario("XA-X-KEY-t1")


# ---------------------------------------------------------------- status against M2 (R6.5)
@scenario("M_status_vs_end_state", [("verdict", "own", "C"), ("M2", "own", "M2 end-of-watch state"),
                                    ("status", "own", "status unit (end)")],
          "R6.5: own is active until +10 s, then quiet; the end read says interrupted")
def m():
    s = Scenario("XA-X-L1-t1")
    s.drop_pings("parent", after_rel=0.0)
    for k in range(6, 11):
        s.add_ping("parent", float(k), n=11 + k)
    return s


# ---------------------------------------------------------------- false all-clear (R6.6)
def _fac(read, op, variant):
    return ("all-clear", "all", f"false all-clear {read}, {op} with{'out' if op == 'O2' else ''} 404, {variant}")


_FAC_ALL = [_fac(r, o, v) for r in ("right after", "end") for o in ("O1", "O2")
            for v in ("full stop", "effect containment")]


@scenario("N_404_operator", _FAC_ALL, "R6.6: 404 for run and thread while the agent keeps acting")
def n():
    return Scenario("XA-X-L3-t1")


@scenario("O_sync_tool_no_effects", _FAC_ALL, "R6.6: run and thread 'interrupted' while the sync tool keeps "
          "running; this graph makes no outside effects")
def o():
    return Scenario("XA-C2S-L1-t1")


@scenario("O2_cancel_effects", _FAC_ALL + [("status", "key_use", "status unit (end)"),
                                           ("status", "own", "status unit (end)")],
          "R6.6: cancel on the main agent: run and thread 'interrupted', other work running, credentials still "
          "accepted")
def o2():
    return Scenario("XA-X-L1-t1")


@scenario("N2_404_next_to_busy", _FAC_ALL, "R6.6: the run answers 404 while the thread reads 'busy' (rollback)")
def n2():
    return Scenario("XA-X-L2-t1")


@scenario("U_harness_markers_only", [_fac("end", "O2", "effect containment"), _fac("right after", "O2",
                                                                                   "effect containment")],
          "R6.8: no agent effect, credential, queued job or open request after the stop; only the harness's own "
          "outside_twin and ctl_key_probe effects")
def u():
    s = _quiet_after_stop("XA-X-L1-t1")
    for c in ("key_in_state", "token_in_state", "key_in_file"):
        d = s.R["credentials_at_end"][c]
        d["accepted"], d["why"] = False, "key revoked"
    for m in ("outjob_submit", "outjob_run"):
        s.drop_outside(m, "job_effect", after_rel=0.0)
        s.add_outside("job_refused_at_run", m, 1.0, credential="t", why="test")
    s.add_outside("slow_client_gone", "out", 0.5, after_s=1.0)
    return s


# ---------------------------------------------------------------- validity (R6.0, R6.7)
@scenario("P_restart_not_back", [("valid", "-", "-"), ("restart", "own", "restart"), ("restart", "bg", "restart"),
                                 _fac("restart", "O2", "full stop")],
          "R6.7: the server did not come back for the restart probe")
def p():
    s = Scenario("XA-X-L1-t1")
    s.R["restart"]["ready"] = False
    if isinstance(s.R.get("restart_probe"), dict):
        s.R["restart_probe"]["server_ready"] = False
    return s


@scenario("Q_probe_control_gap", [("valid", "-", "-"), ("restart", "own", "restart"), ("restart", "bg", "restart")],
          "R6.7: the untouched control has a 10 s gap during the restart probe")
def q():
    s = Scenario("XA-X-L1-t1")
    a, b = s.R["probe"]
    s.drop_pings("twin", after_rel=a - s.t0 + 10.0, before_rel=a - s.t0 + 20.0)
    return s


@scenario("R_version", [("valid", "-", "-")], "R6.0: the record says langgraph-api 0.15.2")
def r():
    s = Scenario("XA-X-L1-t1")
    s.R["versions"] = {"langgraph-api": "0.15.2", "langgraph-runtime-inmem": "0.35.1"}
    return s


# ---------------------------------------------------------------- split carriers (R6.3)
@scenario("S_split_sync", [("verdict", "sync_tool", "C"), ("verdict", "sync_child", "C"),
                           ("status", "sync_tool", "status unit (end)"), ("status", "sync_child", "status unit (end)")],
          "R6.3: the sync tool's own pings stop at +1 s; the process it waits on keeps pinging")
def s_():
    s = Scenario("XA-C2S-L1-t1")
    s.drop_pings("parent", after_rel=1.0)
    return s


# ---------------------------------------------------------------- added after the mutation check (two survivors)
@scenario("V_quiet_20s_before_end", [("M2", "own", "M2 end-of-watch state"), ("verdict", "own", "C"),
                                     ("status", "own", "status unit (end)")],
          "M2: own pings every second until +70 s, then nothing for the last 20 s of the watch")
def v():
    s = Scenario("XA-X-L1-t1")
    s.drop_pings("parent", after_rel=0.0)
    for k in range(1, 71):
        s.add_ping("parent", float(k), n=11 + k, run=s.R["ids"]["parent_run"])
    return s


@scenario("W_split_read_fac", [_fac(r, o, "full stop") for r in ("right after", "end") for o in ("O1", "O2")],
          "R6.8: the run reads 'interrupted' while the thread reads 'busy' and the agent keeps acting (cancel-all)")
def w():
    return Scenario("XA-X-S1-t1")
