#!/usr/bin/env python3
"""Condition D harness: runs khan_probe.py trials inside pilot-01's procedure
(outside recorder, clean-slate check, untouched and directly-killed controls, 90 s watch).

Per trial: clean slate (no pings for 5 s, no leftover canaries or probes) -> start the two
controls -> wait until the killed control has pinged 5 times -> start one khan_probe.py
process (fresh interpreter). The probe kills the control and cancels its run at the same
moment, watches 90 s and prints its in-process record. The harness adds the recorder's
view, applies Khan's predicate and ours (PREDICATES.md, condition D), kills the controls
and appends one record to results/trials.jsonl. Variants alternate: sync t1, async t1, ...

Run through run_pilot.sh (loopback-only network namespace):
  SCRIPT=harness_d.py bash run_pilot.sh --trials 5
"""
import argparse
import json
import os
import subprocess
import sys
import time
import traceback

import harness as H  # pilot-02 harness: recorder handling, ping log, process views, logging

PROBE = os.path.join(H.PILOT, "khan_probe.py")
P_GRACE = 2.0


def probe_procs():
    return [p for p in H.proc_table().values() if any(a.endswith("khan_probe.py") for a in p["argv"])
            and p["state"] != "Z"]


def clean_slate(pl):
    t0 = H.now()
    time.sleep(5)
    pings = pl.select(t0=t0)
    d = {"pings_in_5s": len(pings), "pinging": sorted({f"{p['canary']}/{p['trial']}" for p in pings}),
         "canary_procs": [p["pid"] for p in H.canary_procs()], "probe_procs": [p["pid"] for p in probe_procs()]}
    d["ok"] = d["pings_in_5s"] == 0 and not d["canary_procs"] and not d["probe_procs"]
    return d


def analyse(R, P, pl):
    trial = R["trial"]
    t_stop, t_obs, t_end = P["t_cancel"], P["t_observed"], P["t_end_watch"]
    rec = sorted(pl.select(trial=trial), key=lambda p: p["mono"])

    def times(c):
        return [p["mono"] for p in rec if p.get("canary") == c]

    inproc_eff = [e for e in P.get("event_log", []) if e["kind"] == "effect"]
    inproc_start = [e for e in P.get("event_log", []) if e["kind"] == "tool_start"]
    rec_eff = [p for p in rec if p.get("canary") == "effect"]
    A = {
        "tool_started_before_cancel": any(e["mono"] < t_stop for e in inproc_start),
        "tool_thread": [e["thread"] for e in inproc_start],
        "tool_on_main_thread": [e["is_main_thread"] for e in inproc_start],
        "caller_saw": P.get("caller_saw"),
        "observed_after_cancel_s": round(t_obs - t_stop, 4),
        "effects_inproc": [{"rel_cancel": round(e["mono"] - t_stop, 4), "rel_observed": round(e["mono"] - t_obs, 4),
                            "thread": e["thread"], "is_main_thread": e["is_main_thread"]} for e in inproc_eff],
        "effects_recorder": [{"rel_cancel": round(p["mono"] - t_stop, 4), "rel_observed": round(p["mono"] - t_obs, 4),
                              "thread": p.get("thread")} for p in rec_eff],
    }
    A["n_effects_inproc"], A["n_effects_recorder"] = len(inproc_eff), len(rec_eff)
    # Khan's predicate: the effect lands after the caller sees the cancellation (no grace)
    A["khan_violation_inproc"] = any(e["mono"] > t_obs for e in inproc_eff)
    A["khan_violation_recorder"] = any(p["mono"] > t_obs for p in rec_eff)
    # ours: an effect ping after t_cancel + 2 s, within the watch
    A["ours_violation"] = any(t_stop + P_GRACE < p["mono"] <= t_end for p in rec_eff)
    if not A["tool_started_before_cancel"]:
        A["verdict_khan"] = A["verdict_ours"] = "untested (tool never started before the cancel)"
    else:
        A["verdict_khan"] = "VIOLATION" if A["khan_violation_inproc"] else "clean"
        A["verdict_ours"] = "VIOLATION" if A["ours_violation"] else "clean"
    # in-process log vs recorder: arrival minus commit, per effect
    if inproc_eff and rec_eff:
        A["recorder_minus_inproc_s"] = [round(r["mono"] - e["mono"], 4) for e, r in zip(inproc_eff, rec_eff)]
    # validity (PREDICATES.md section 1)
    tw = [t for t in times("twin") if t_stop <= t <= t_end]
    pts = [t_stop] + tw + [t_end]
    twin_gap = max(b - a for a, b in zip(pts, pts[1:]))
    ck = times("control_kill")
    ck_late = [round(t - t_stop, 3) for t in ck if t > t_stop + P_GRACE]
    reasons = []
    if twin_gap > 5.0:
        reasons.append(f"untouched control silent for {twin_gap:.1f} s")
    if ck_late:
        reasons.append(f"killed control pinged {ck_late[0]} s after the stop")
    A["valid"], A["invalid_reasons"] = not reasons, reasons
    A["twin_max_gap_incl_edges_s"] = round(twin_gap, 3)
    A["control_kill_pings_10s_before"] = sum(1 for t in ck if t_stop - 10 <= t <= t_stop)
    A["control_kill_last_rel"] = round(ck[-1] - t_stop, 3) if ck else None
    return A


def run_trial(variant, k, args, pl):
    trial = f"D-{variant}-t{k}"
    R = {"trial": trial, "cond": "D", "lever": f"cancel-{variant}", "variant": variant, "k": k, "deviations": [],
         "params": {"tool_s": None, "cancel_after_s": None, "watch_s": args.watch, "grace_s": P_GRACE}}
    H.log(f"=== {trial}: Khan cancellation-orphan probe, {variant} tool")
    R["clean_slate"] = clean_slate(pl)
    if not R["clean_slate"]["ok"]:
        R["deviations"].append(f"clean slate failed: {R['clean_slate']}")
        H.kill_canaries()
        time.sleep(3)
        R["clean_slate_retry"] = clean_slate(pl)
        if not R["clean_slate_retry"]["ok"]:
            raise RuntimeError(f"clean slate failed twice: {R['clean_slate_retry']}")
    dn = subprocess.DEVNULL
    twin = subprocess.Popen([H.PY, H.CANARY, "twin", trial], stdin=dn, stdout=dn, stderr=dn, start_new_session=True)
    ck = subprocess.Popen([H.PY, H.CANARY, "control_kill", trial], stdin=dn, stdout=dn, stderr=dn, start_new_session=True)
    R["control_pids"] = {"twin": twin.pid, "control_kill": ck.pid}
    t_c = H.now()
    while H.now() < t_c + 15 and len(pl.select(t0=t_c, trial=trial, canary="control_kill")) < 5:
        time.sleep(0.2)
    errlog = open(os.path.join(H.LOGS, f"probe_{trial}.stderr.log"), "wb")
    probe = subprocess.Popen([H.PY, PROBE, "--variant", variant, "--trial", trial, "--control-kill-pid", str(ck.pid),
                              "--watch", str(args.watch)], stdin=dn, stdout=subprocess.PIPE, stderr=errlog,
                             start_new_session=True)
    R["probe_pid"] = probe.pid
    try:
        out, _ = probe.communicate(timeout=args.watch + 90)
    except subprocess.TimeoutExpired:
        probe.kill()
        out, _ = probe.communicate()
        R["deviations"].append("probe process overran its time limit and was killed")
    errlog.close()
    R["probe_returncode"] = probe.returncode
    lines = [ln for ln in out.decode(errors="replace").splitlines() if ln.strip().startswith("{")]
    if not lines:
        raise RuntimeError(f"probe printed no record (rc={probe.returncode}); see logs/probe_{trial}.stderr.log")
    P = json.loads(lines[-1])
    R["probe"] = P
    R["params"]["tool_s"], R["params"]["cancel_after_s"] = P["tool_s"], P["cancel_after_s"]
    R["t_stop"], R["t_end_watch"] = P["t_cancel"], P["t_end_watch"]
    try:
        ck.wait(5)
    except subprocess.TimeoutExpired:
        R["deviations"].append("killed control still running after the probe ended")
    R["host_end"] = {"control_kill_alive": ck.poll() is None, "twin_alive": twin.poll() is None,
                     "probe_procs_left": [p["pid"] for p in probe_procs()]}
    time.sleep(1.0)  # let the last pings land before reading the recorder
    R["analysis"] = analyse(R, P, pl)
    # cleanup
    killed = H.kill_canaries()
    for ch in (twin, ck):
        try:
            ch.kill()
        except Exception:
            pass
        try:
            ch.wait(5)
        except Exception:
            pass
    time.sleep(2)
    t_chk = H.now()
    time.sleep(3)
    R["cleanup"] = {"killed_canaries": killed, "leftover_canary_procs": [p["pid"] for p in H.canary_procs()],
                    "leftover_probe_procs": [p["pid"] for p in probe_procs()],
                    "pings_in_3s_after_cleanup": len(pl.select(t0=t_chk))}
    R["cleanup"]["ok"] = (not R["cleanup"]["leftover_canary_procs"] and not R["cleanup"]["leftover_probe_procs"]
                          and R["cleanup"]["pings_in_3s_after_cleanup"] == 0)
    A = R["analysis"]
    H.log(f"  caller saw {A['caller_saw']} {A['observed_after_cancel_s']}s after cancel; "
          f"effects in-process {[e['rel_cancel'] for e in A['effects_inproc']]} (thread {A['tool_thread']}); "
          f"Khan: {A['verdict_khan']}; ours: {A['verdict_ours']}; valid={A['valid']} {A['invalid_reasons']}")
    return R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--start-k", type=int, default=1)
    ap.add_argument("--watch", type=float, default=90.0)
    ap.add_argument("--variants", default="sync,async")
    ap.add_argument("--tag", default="_D")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()
    if args.outdir:
        H.RES = os.path.abspath(args.outdir)
        H.LOGS, H.LAUNCH_DIR = os.path.join(H.RES, "logs"), os.path.join(H.RES, "launch")
        H.PINGS, H.TRIALS, H.PROGRESS = (os.path.join(H.RES, n) for n in ("pings.jsonl", "trials.jsonl", "progress.log"))
    os.makedirs(H.LOGS, exist_ok=True)
    meta = H.env_meta() | {"args": vars(args), "condition": "D"}
    meta["server_cmd"] = "(none: library probe, no server)"
    with open(os.path.join(H.RES, f"run_meta{args.tag}.json"), "a", encoding="utf-8") as f:
        f.write(json.dumps(meta) + "\n")
    H.log(f"harness_d start: {json.dumps(vars(args))}")
    H.log(f"versions: {meta['versions']}")
    H.log(f"interfaces: {meta['network_interfaces_in_namespace']!r}")
    if H.find_listening_pid(8765) or H.canary_procs() or probe_procs():
        H.log("ERROR: something is already running (port 8765, canary or probe); refusing to start")
        sys.exit(2)
    reclog = open(os.path.join(H.LOGS, "recorder.log"), "ab")
    recorder = subprocess.Popen([H.PY, H.RECORDER, "--out", H.PINGS], stdin=subprocess.DEVNULL, stdout=reclog,
                                stderr=subprocess.STDOUT, start_new_session=True)
    if not H.wait_http(H.RECORDER_URL + "/health", 15):
        H.log("ERROR: recorder did not start")
        recorder.kill()
        sys.exit(3)
    H.log(f"recorder pid={recorder.pid}")
    pl = H.PingLog(H.PINGS)
    pl.mark_start()  # only this run's pings count (fix added after D had run; D's own runs were unaffected)
    try:
        for k in range(args.start_k, args.start_k + args.trials):
            for v in args.variants.split(","):
                try:
                    R = run_trial(v, k, args, pl)
                except Exception as e:
                    H.log(f"  TRIAL ERROR D-{v}-t{k}: {H.err_str(e)}\n{traceback.format_exc()}")
                    R = {"trial": f"D-{v}-t{k}", "cond": "D", "lever": f"cancel-{v}", "variant": v, "k": k,
                         "error": H.err_str(e), "traceback": traceback.format_exc()}
                    H.kill_canaries()
                    for p in probe_procs():
                        try:
                            os.kill(p["pid"], 9)
                        except ProcessLookupError:
                            pass
                R["harness_tag"] = args.tag
                with open(H.TRIALS, "a", encoding="utf-8") as f:
                    f.write(json.dumps(R) + "\n")
    finally:
        H.log("final teardown (D)")
        H.kill_canaries()
        for p in probe_procs():
            try:
                os.kill(p["pid"], 9)
            except ProcessLookupError:
                pass
        recorder.terminate()
        try:
            recorder.wait(10)
        except subprocess.TimeoutExpired:
            recorder.kill()
        time.sleep(1)
        final = {"canary_procs": [p["pid"] for p in H.canary_procs()], "probe_procs": [p["pid"] for p in probe_procs()],
                 "recorder_alive": recorder.poll() is None, "port_8765": H.find_listening_pid(8765), "ps": H.ps_lines()}
        H.log(f"final check (inside namespace): {final}")
        with open(os.path.join(H.RES, f"final_check{args.tag}.json"), "w", encoding="utf-8") as f:
            json.dump(final, f, indent=1)


if __name__ == "__main__":
    main()
