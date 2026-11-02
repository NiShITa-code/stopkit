"""Present the recorded n8n loop activity beside the computation-end evidence, one row per trial and loop.

    python3 -B experiments/scripts/loop_evidence.py --out experiments/docs/n8n-loop-evidence --overwrite

It reads each trial's raw files through the frozen scorer's own reader, validity checks and computation-boundary
function, and writes rows.jsonl and the page REPORT.md, whose links point from experiments/docs/n8n-loop-evidence/.
"""

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).absolute().parents[2]
SCORER = ROOT / "experiments/harness/n8n-langgraph-7a3fcec/n8n/analyze_b.py"
TRIALS = ROOT / "experiments/n8n-queue-mode/records/cb1013"
PAGE_DIR = ROOT / "experiments/docs/n8n-loop-evidence"


def load_analyzer():
    spec = importlib.util.spec_from_file_location("n8n_frozen_scorer", SCORER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def presentation(eligible, recent_activity, boundary, reason):
    """Silence is never promoted to confirmed termination by this view."""
    if not eligible:
        return "unknown", "Termination unconfirmed: invalid, untested or incomplete observations", "unknown"
    if boundary == "closed" and reason:
        if reason.startswith("all identified executions reached their designed iteration limit"):
            return "supported", "Natural completion evidence at the snapshot", "natural completion"
        return "supported", "Computation-end evidence at the snapshot", "not established by this view"
    if recent_activity:
        return "unconfirmed", "Recent activity observed; termination unconfirmed", "unknown"
    return "unconfirmed", "No recent activity observed; termination unconfirmed", "unknown"


def describe_trial(folder, analyzer):
    folder = Path(folder).absolute()
    trial = analyzer.Trial(str(folder))
    validity = analyzer.validity_rows(trial)[0]
    snapshot = trial.end_read
    snapshot_time = analyzer._read_time(snapshot)
    start, end = trial.t_stop, trial.t_end
    timed = isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start
    try:
        with (folder / "pings.jsonl").open(encoding="utf-8") as stream:
            numbered_pings = [(n, json.loads(line)) for n, line in enumerate(stream, 1) if line.strip()]
    except (OSError, ValueError):
        numbered_pings = []
        trial.log_ok["pings"] = False
    rows = []
    for carrier in analyzer.LOOP_CARRIERS:
        execution = trial.agent_id if carrier == "loop_steps" else None
        matches = [
            (n, ping)
            for n, ping in numbered_pings
            if ping.get("trial") == trial.trial
            and ping.get("canary") == analyzer.CANARY[carrier]
            and (execution is None or str(ping.get("exec")) == str(execution))
            and isinstance(ping.get("mono"), (int, float))
        ]
        after = [(n, ping) for n, ping in matches if timed and start < ping["mono"] <= end]
        # the scorer's quiet tail, counted back from the end of the watch
        tail = analyzer.QUIET_TAIL
        recent = bool(timed and any(max(start, end - tail) < ping["mono"] <= end for _, ping in matches))
        boundary, evidence = analyzer.computation_boundary(trial, carrier)
        untested = trial.untested(carrier)
        eligible = (
            validity["kind"] == "valid"
            and not untested
            and timed
            and snapshot_time is not None
            and trial.log_ok.get("pings") is True
        )
        supported, description, cause = presentation(eligible, recent, boundary, evidence.get("why"))
        ids = (
            [trial.agent_id]
            if carrier == "loop_steps" and trial.agent_id is not None
            else trial.exec_ids_of(carrier, upto=snapshot_time)
        )
        statuses = (
            {eid: (snapshot.get("exec_status", {}).get(str(eid)) or {}).get("status", "unavailable") for eid in ids}
            if snapshot
            else {}
        )
        last = max(after, key=lambda entry: entry[1]["mono"]) if after else None
        rows.append(
            {
                "trial": trial.trial,
                "condition": trial.cond,
                "carrier": carrier,
                "raw_folder": folder.relative_to(ROOT).as_posix() if folder.is_relative_to(ROOT) else str(folder),
                "validity": validity["kind"],
                "validity_reason": validity["verdict"],
                "untested": untested,
                "activity_window_after_stop_s": round(end - start, 3) if timed else None,
                "snapshot_after_stop_s": round(snapshot_time - start, 3)
                if timed and snapshot_time is not None
                else None,
                "snapshot_label": (snapshot or {}).get("label"),
                "pings_after_stop": len(after) if timed and trial.log_ok.get("pings") else None,
                "recent_activity_observed": recent if timed and trial.log_ok.get("pings") else None,
                "last_ping_after_stop_s": round(last[1]["mono"] - start, 3) if last else None,
                "last_ping_line": last[0] if last else None,
                "platform_statuses": statuses,
                "recorded_computation_boundary": boundary,
                "computation_evidence": evidence,
                "termination_support": supported,
                "assessment": description,
                "cause_of_ending": cause,
            }
        )
    return rows


def md_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown(rows, output):
    counts = Counter(row["termination_support"] for row in rows)
    quiet = sum(row["termination_support"] == "unconfirmed" and not row["recent_activity_observed"] for row in rows)
    lines = [
        "# n8n loop activity and computation-end evidence",
        "",
        f"In each of the {len({row['trial'] for row in rows})} trials of 13 October 2026 the agent ran four loops "
        "inside n8n: its own loop and three "
        "children (a Code-node loop, a sub-workflow loop and an AI Agent node driven by a scripted stand-in model). "
        "For every trial and loop, the tables below give the updates our receiver got after the stop, whether one "
        "arrived in the last five seconds of the watch, the status n8n reported, and whether the raw records show "
        "that the loop's computation ended.",
        "",
        f"- Trials: {len({row['trial'] for row in rows})}; loop readings: {len(rows)}.",
        f"- Computation-end evidence at the snapshot: {counts['supported']}.",
        f"- Termination unconfirmed: {counts['unconfirmed']}; of these, {quiet} have no recent activity.",
        f"- Invalid, untested or incomplete: {counts['unknown']}.",
        "",
        "A loop that is quiet for the last five seconds of the watch counts as unconfirmed unless the records show "
        "its computation ended. A loop can be active in those five seconds and still have ended by the snapshot.",
        "",
        "## How to read this page",
        "",
        "Updates are receiver arrivals after the recorded stop (for B0, the trial with no stop, after its control "
        "anchor) and up to the declared end of the watch. *Recent* means an arrival in the final five seconds of that "
        "window. The system snapshot, the harness's last reading of processes and services, has its own completion "
        "time and can finish slightly after the window.",
        "",
        "Computation-end evidence is one of two readings of the raw records: the worker's service inactive or failed, "
        "with no main process and a complete, empty reading of its process group; or every identified execution at "
        "its designed last step with a successful record. Either shows that the loop's computation ended; neither "
        "shows what ended it, and a cleanup or restart after the snapshot does not change the reading at the "
        "snapshot.",
        "",
        "The rows are computed with the frozen scorer's own reader, validity checks and computation-boundary "
        "function (`experiments/harness/n8n-langgraph-7a3fcec/n8n/analyze_b.py`), from each trial's `record.json` and "
        "the "
        "recorder and service logs.",
        "",
    ]
    examples = [
        next((row for row in rows if row["condition"] == condition and row["carrier"] == "loop_steps"), None)
        for condition in ("B1", "B1r", "B8")
    ]
    if any(examples):
        lines += [
            "## Three examples: the main execution",
            "",
            "| Condition | Updates after stop | Recorded platform status | Evidence assessment |",
            "|---|---:|---|---|",
        ]
        for row in examples:
            if row:
                statuses = ", ".join(row["platform_statuses"].values()) or "unavailable"
                lines.append(
                    "| "
                    + " | ".join(
                        md_cell(value)
                        for value in (row["condition"], row["pings_after_stop"], statuses, row["assessment"])
                    )
                    + " |"
                )
        lines += [
            "",
            "B1 stops the execution by its id; B1r gives the same stop after main has restarted; B8 "
            "stops the worker's service. The three stops have different intended scopes, and the "
            "workload is older at the stop in B1r than in B1.",
            "",
        ]
    trials = list(dict.fromkeys(row["trial"] for row in rows))
    for trial in trials:
        selected = [row for row in rows if row["trial"] == trial]
        first = selected[0]
        folder = Path(first["raw_folder"])
        if not folder.is_absolute():
            folder = ROOT / folder
        record_link = os.path.relpath(folder / "record.json", PAGE_DIR).replace(os.sep, "/")
        pings_link = os.path.relpath(folder / "pings.jsonl", PAGE_DIR).replace(os.sep, "/")
        lines += [
            f"## {first['condition']}: {trial}",
            "",
            f"[Raw record]({record_link}) · [Receiver log]({pings_link})",
            "",
            f"Validity: {first['validity']}. Activity window: {first['activity_window_after_stop_s']} s; "
            f"snapshot completion: +{first['snapshot_after_stop_s']} s ({first['snapshot_label']}).",
            "",
            "| Work | Updates after stop | Last update (+s) | Recent activity | Platform status | Assessment |",
            "|---|---:|---:|---|---|---|",
        ]
        for row in selected:
            statuses = ", ".join(f"{key}: {value}" for key, value in row["platform_statuses"].items()) or "unavailable"
            last = row["last_ping_after_stop_s"] if row["last_ping_after_stop_s"] is not None else "none"
            if row["last_ping_line"]:
                last = f"[{last}]({pings_link}#L{row['last_ping_line']})"
            values = [
                row["carrier"],
                row["pings_after_stop"],
                last,
                row["recent_activity_observed"],
                statuses,
                row["assessment"],
            ]
            lines.append("| " + " | ".join(md_cell(value) for value in values) + " |")
        lines.append("")
        for row in selected:
            lines.append(
                f"- **{row['carrier']} evidence:** {row['computation_evidence'].get('why', 'unavailable')}. "
                f"Cause: {row['cause_of_ending']}."
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trials", nargs="*", type=Path, help="raw trial folders; defaults to the 19 trials of 13 October"
    )
    parser.add_argument("--out", required=True, type=Path, help="new output directory for REPORT.md and rows.jsonl")
    parser.add_argument("--overwrite", action="store_true", help="replace this script's own output files")
    args = parser.parse_args(argv)
    folders = args.trials or sorted(
        path.parent for path in TRIALS.glob("w*/collect-*/results/XB-screen-cb1013-w*-*-2026*/record.json")
    )
    if not folders:
        parser.error("no raw trials found")
    output = args.out.resolve()
    # Everything under experiments/ holds records or the code that reads them, apart from the pages in experiments/docs.
    studies = ROOT / "experiments"
    protected = [p for p in studies.iterdir() if p.is_dir() and p.name != "docs"] + [Path(f).resolve() for f in folders]
    if any(output.is_relative_to(path.resolve()) for path in protected):
        parser.error("write the output outside the records, the frozen code and the studies' scripts")
    paths = [output / "REPORT.md", output / "rows.jsonl"]
    if not args.overwrite and any(path.exists() for path in paths):
        parser.error("output exists; choose a new directory or explicitly use --overwrite")
    analyzer = load_analyzer()
    rows = [row for folder in folders for row in describe_trial(folder, analyzer)]
    rows.sort(key=lambda row: (row["condition"], row["carrier"]))
    output.mkdir(parents=True, exist_ok=True)
    paths[0].write_text(markdown(rows, output), encoding="utf-8")
    paths[1].write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    print(f"{len(folders)} trials, {len(rows)} loop readings; report: {paths[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
