#!/usr/bin/env python3
"""Independent re-check of a client-probe batch (see PREDICTIONS.md). Standard library only.

Re-derives every measure from the raw files (server logs, marker files, the stand-in's
request logs, the terminal recordings), using only the action times the harness wrote,
compares them with the harness's own records, scores the pre-registered predictions run
by run, and summarises the exploratory watch after a client exited.

Usage: python3 check_probe.py results/<batch>
Writes <batch>/check.json and <batch>/check.md.
"""

import hashlib
import json
import os
import re
import statistics
import sys
from collections import defaultdict

GRACE = 0.6
TIMEOUT_S = 3.0
ANSI = re.compile(rb"\x1b\[[0-9;:?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[P^_X][^\x1b]*\x1b\\|\x1b[()*+][0-9A-Za-z]|\x1b[=>78DEHMNOZc]")
# Messages a person may be shown after a stop (searched in the terminal recording).
SHOWN = ["Interrupted", "What should Claude do instead", "timed out", "stopped", "Stop Task",
         "Conversation interrupted", "aborted", "Disconnected from this task", "The current turn was stopped",
         "Task is still running", "Run in background", "Resume this session", "codex resume", "Terminated",
         "Shutting down", "Press Ctrl-C again to exit", "Error"]


def lp(path):
    """On Windows, the long-path form, since some client records exceed 260 characters."""
    return "\\\\?\\" + os.path.abspath(path) if os.name == "nt" else path


def jsonl(path):
    out = []
    if os.path.exists(lp(path)):
        with open(lp(path), encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(lp(path), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def results_of(body):
    """{tool name: result text} from one request, either API, matching results to calls by id."""
    names, out = {}, {}
    for message in body.get("messages") or []:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names[block.get("id")] = block.get("name")
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                out[names.get(block.get("tool_use_id"), "?")] = json.dumps(block.get("content"))
    for item in body.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            names[item.get("call_id")] = item.get("name")
        elif isinstance(item, dict) and item.get("type") == "function_call_output":
            out[names.get(item.get("call_id"), "?")] = json.dumps(item.get("output"))
    return out


def derive(batch, rec):
    """Every measure, from the raw files."""
    rid = rec["run_id"]
    events = jsonl(os.path.join(batch, "serverlogs", f"{rid}.jsonl"))
    stub = jsonl(os.path.join(batch, "stub", f"{rid}.jsonl"))
    marks_path = os.path.join(batch, "marks", f"{rid}.log")
    marks = [float(l.split()[0]) for l in open(marks_path)] if os.path.exists(marks_path) else []
    t_end, t_late = rec["t_end"], rec.get("t_follow_end", rec["t_end"])
    call = next((e for e in events if e.get("event") == "in" and e.get("method") == "tools/call"), None)
    d = {"call_seen": bool(call)}
    if rec["path"] in ("esc", "quit", "sigterm"):
        t_stop = rec["t_action"]
    elif rec["path"] == "timeout":
        t_stop = call["t"] + TIMEOUT_S if call else None
    elif rec["path"] == "bg-stop":
        req = next((s for s in stub if (s.get("reply") or {}).get("kind") == "tool"
                    and "stop" in str((s.get("reply") or {}).get("name", "")).lower()), None)
        t_stop = req["t"] if req else None
    else:
        t_stop = None
    d["t_stop"] = t_stop
    ref = t_stop if t_stop is not None else rec["t_action"]
    rel = lambda t: round(t - ref, 3)
    win = [e for e in events if e["t"] <= t_end]
    cancels = [e for e in events if e.get("event") == "in" and e.get("method") == "notifications/cancelled"
               and call and (e.get("params") or {}).get("requestId") == call.get("id")]
    first = next((e for e in cancels if e["t"] <= t_end), None)
    d["cancel"] = bool(first)
    d["cancel_rel"] = rel(first["t"]) if first else None
    d["cancel_reason"] = (first.get("params") or {}).get("reason") if first else None
    d["cancel_late"] = any(t_end < e["t"] <= t_late for e in cancels)
    d["signals"] = [[e["name"], rel(e["t"])] for e in win if e.get("event") == "signal"]
    eof = next((e for e in win if e.get("event") == "stdin_eof"), None)
    d["stdin_eof_rel"] = rel(eof["t"]) if eof else None
    end = next((e for e in win if e.get("event") == "exit"), None)
    d["server_exit"] = [end["reason"], rel(end["t"])] if end else None
    sent = next((e for e in win if e.get("event") == "result_sent"), None)
    d["result_sent_rel"] = rel(sent["t"]) if sent else None
    d["markers_after_stop"] = sum(1 for t in marks if t_stop + GRACE < t <= t_end) if t_stop is not None else None
    d["work"] = (("NOT STOPPED" if d["markers_after_stop"] else "STOPPED") if t_stop is not None
                 else ("finished and replied" if sent else "no reply"))
    told = {}
    for s in stub:
        if ref <= s["t"] <= t_late:
            told.update(results_of(s.get("body") or {}))
    d["told_slow"] = next((v for k, v in told.items() if str(k).endswith("slow")), None)
    d["told_stop"] = next((v for k, v in told.items() if "stop" in str(k).lower()), None)
    raw_path = os.path.join(batch, "tty", f"{rid}.raw")
    text = ANSI.sub(b"", open(raw_path, "rb").read()).decode("utf-8", errors="replace") if os.path.exists(raw_path) else ""
    d["shown"] = [p for p in SHOWN if p.lower() in text.lower()]
    # Exploratory: after the client exited.
    exit_rel = rec.get("client_exit_rel_action")
    if exit_rel is not None:
        t_exit = rec["t_action"] + exit_rel
        d["after_exit"] = {
            "tool_finished_after_exit": any(e.get("event") == "result_sent" and e["t"] > t_exit for e in events
                                            if e["t"] <= t_late),
            "server_events_after_exit": [e.get("event") for e in events if t_exit < e["t"] <= t_late],
            "requests_after_exit": [[rel(s["t"]), (s.get("reply") or {}).get("kind"),
                                     sorted(results_of(s.get("body") or {}))] for s in stub if s["t"] > t_exit],
            "markers_after_exit": sum(1 for t in marks if t > t_exit),
        }
    return d


def predictions(client, path, d):
    """The pre-registered predictions (PREDICTIONS.md table) as checks on one run: [(item, held)]."""
    told = (d["told_slow"] or "").lower()
    first_signal = d["signals"][0][0] if d["signals"] else None
    if path == "control":
        return [("tool finished and result sent", d["result_sent_rel"] is not None), ("no cancel", not d["cancel"])]
    if client.startswith("claude"):
        if path == "esc":
            return [("cancel sent", d["cancel"])]
        if path == "timeout":
            return [("cancel sent", d["cancel"]), ("model gets a timeout error", "timed out" in told or "timeout" in told)]
        if path == "bg-stop":
            stop = (d["told_stop"] or "").lower()
            return [("cancel sent", d["cancel"]), ("TaskStop reports success", "success" in stop)]
        if path in ("quit", "sigterm"):
            return [("no cancel", not d["cancel"]), ("SIGINT to the server first", first_signal == "SIGINT"),
                    ("child survives", bool(d["markers_after_stop"]))]
    new = client == "codex-0.165.0-alpha.1"
    if path == "esc":
        if new:
            return [("cancel sent", d["cancel"]),
                    ("reason 'tool call response future dropped'", d["cancel_reason"] == "tool call response future dropped")]
        return [("no cancel", not d["cancel"])]
    if path == "timeout":
        items = [("cancel sent", d["cancel"])] if new else [("no cancel", not d["cancel"])]
        if not new:
            items.append(("model gets a timeout error", "timed out" in told or "timeout" in told))
        return items
    if path in ("quit", "sigterm"):
        return [("no cancel", not d["cancel"]), ("SIGTERM to the server", first_signal == "SIGTERM"),
                ("child killed", d["markers_after_stop"] == 0)]
    return []


def main():
    batch = sys.argv[1].rstrip("/")
    manifest = json.load(open(os.path.join(batch, "manifest.json")))
    recs = jsonl(os.path.join(batch, "runs.jsonl"))
    report = {"batch": os.path.basename(batch), "integrity": {}, "runs": [], "mismatches": [], "excluded": []}

    # Integrity: the batch's files against SHA256SUMS.json; the clients unchanged.
    sums = json.load(open(os.path.join(batch, "SHA256SUMS.json")))
    bad = [p for p, h in sums.items()
           if not os.path.exists(lp(os.path.join(batch, p))) or sha256(os.path.join(batch, p)) != h]
    report["integrity"]["files_checked"] = len(sums)
    report["integrity"]["files_mismatched"] = bad
    after = json.load(open(os.path.join(batch, "clients-after.json")))
    report["integrity"]["clients_unchanged"] = after.get("unchanged")

    held = defaultdict(lambda: defaultdict(list))
    summary = defaultdict(list)
    for rec in recs:
        if rec.get("verdict", "").startswith("EXCLUDED"):
            report["excluded"].append([rec["run_id"], rec["verdict"]])
            continue
        d = derive(batch, rec)
        # Compare with the harness's own record.
        for key in ("cancel", "cancel_reason", "markers_after_stop", "stdin_eof_rel", "server_exit", "result_sent_rel"):
            if rec.get(key) != d.get(key):
                report["mismatches"].append([rec["run_id"], key, rec.get(key), d.get(key)])
        if [s[0] for s in rec.get("signals") or []] != [s[0] for s in d["signals"]]:
            report["mismatches"].append([rec["run_id"], "signals", rec.get("signals"), d["signals"]])
        if rec.get("cancel_rel") is not None and d["cancel_rel"] is not None and abs(rec["cancel_rel"] - d["cancel_rel"]) > 0.002:
            report["mismatches"].append([rec["run_id"], "cancel_rel", rec["cancel_rel"], d["cancel_rel"]])
        checks = predictions(rec["client"], rec["path"], d)
        for item, ok in checks:
            held[(rec["client"], rec["path"])][item].append(ok)
        row = {"run_id": rec["run_id"], "client": rec["client"], "path": rec["path"], **d,
               "client_exit_rel_action": rec.get("client_exit_rel_action"),
               "client_exit_code": rec.get("client_exit_code"), "actions": rec.get("actions"),
               "predictions": checks}
        report["runs"].append(row)
        summary[(rec["client"], rec["path"])].append(row)

    report["predictions"] = {f"{c} / {p}": {item: f"{sum(v)}/{len(v)}" for item, v in items.items()}
                             for (c, p), items in sorted(held.items())}
    with open(os.path.join(batch, "check.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)

    # A readable summary.
    lines = [f"# Re-check of {report['batch']}", "",
             f"Files checked against SHA256SUMS.json: {len(sums)}; mismatched: {len(bad)}. "
             f"Client executables unchanged after the batch: {report['integrity']['clients_unchanged']}.",
             f"Runs: {len(report['runs'])} measured, {len(report['excluded'])} excluded. "
             f"Differences between the harness's records and this re-derivation: {len(report['mismatches'])}.", "",
             "| Client | Path | n | Cancel | Delay (s) | Reason | Server got | Server ended | Markers after stop | Work | Client exit (s) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    order = ["control", "esc", "timeout", "bg-stop", "quit", "sigterm"]
    for (c, p) in sorted(summary, key=lambda k: (k[0], order.index(k[1]))):
        rows = summary[(c, p)]
        delays = [r["cancel_rel"] for r in rows if r["cancel_rel"] is not None]
        reasons = sorted({r["cancel_reason"] for r in rows if r["cancel_reason"]})
        sigs = sorted({"+".join(s[0] for s in r["signals"]) or "nothing" for r in rows})
        ends = sorted({(r["server_exit"] or ["still running"])[0] for r in rows})
        marks = [r["markers_after_stop"] for r in rows if r["markers_after_stop"] is not None]
        works = sorted({r["work"] for r in rows})
        exits = [r["client_exit_rel_action"] for r in rows if r["client_exit_rel_action"] is not None]
        lines.append(f"| {c} | {p} | {len(rows)} | {sum(r['cancel'] for r in rows)}/{len(rows)} | "
                     f"{round(statistics.median(delays), 3) if delays else '–'} | {'; '.join(reasons) or '–'} | "
                     f"{'; '.join(sigs)} | {'; '.join(ends)} | {statistics.median(marks) if marks else '–'} | "
                     f"{'; '.join(works)} | {round(statistics.median(exits), 3) if exits else 'stayed open'} |")
    lines += ["", "## Predictions, run by run", "", "| Client / path | Prediction | Held |", "|---|---|---|"]
    for key, items in report["predictions"].items():
        for item, score in items.items():
            lines.append(f"| {key} | {item} | {score} |")
    lines += ["", "## What the model was told about the stopped call", ""]
    for (c, p) in sorted(summary, key=lambda k: (k[0], order.index(k[1]))):
        texts = sorted({(r["told_slow"] or "nothing")[:220] for r in summary[(c, p)]})
        stops = sorted({(r["told_stop"] or "")[:220] for r in summary[(c, p)]} - {""})
        lines.append(f"- {c} / {p}: " + " | ".join(texts) + (f" (TaskStop: {' | '.join(stops)})" if stops else ""))
    lines += ["", "## Messages shown in the terminal (phrase found in the recording, runs with it / runs)", ""]
    for (c, p) in sorted(summary, key=lambda k: (k[0], order.index(k[1]))):
        rows = summary[(c, p)]
        counts = defaultdict(int)
        for r in rows:
            for s in r["shown"]:
                counts[s] += 1
        lines.append(f"- {c} / {p}: " + ", ".join(f"\"{k}\" {v}/{len(rows)}" for k, v in sorted(counts.items())))
    lines += ["", "## Exploratory: after the client exited", ""]
    for (c, p) in sorted(summary, key=lambda k: (k[0], order.index(k[1]))):
        rows = [r for r in summary[(c, p)] if r.get("after_exit")]
        if rows:
            fin = sum(r["after_exit"]["tool_finished_after_exit"] for r in rows)
            req = sum(bool(r["after_exit"]["requests_after_exit"]) for r in rows)
            mk = [r["after_exit"]["markers_after_exit"] for r in rows]
            lines.append(f"- {c} / {p}: tool finished and replied after the exit in {fin}/{len(rows)}; "
                         f"a request reached the model after the exit in {req}/{len(rows)}; "
                         f"markers after the exit (median) {statistics.median(mk)}")
    if report["mismatches"]:
        lines += ["", "## Differences from the harness's records", ""] + [f"- {m}" for m in report["mismatches"]]
    if report["excluded"]:
        lines += ["", "## Excluded runs", ""] + [f"- {e[0]}: {e[1]}" for e in report["excluded"]]
    with open(os.path.join(batch, "check.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
