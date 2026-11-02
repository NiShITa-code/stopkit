"""The LangGraph trials of 9 October 2026: the real log lines around one stop, merged on one clock.

Nothing here is retyped. Each line under a time is copied from a record file and labeled with the file and
line number (for the harness's trial record, the line and the field), so anyone can open the file and find it.
The sources, all written while the trial ran:
  - LangGraph's own server log for this trial (the first server and the one restarted for the restart check):
    the lines that name the agent's run or its thread, the line where the server starts shutting down, every
    warning or error, and, where SHOW lists 'cron', the lines of the runs LangGraph's scheduler started for the
    agent's scheduled job;
  - LangGraph's answers when the harness asked (trials.jsonl, fields status_*), one answer per line;
  - what the harness did and when (trials.jsonl, fields t_stop, t_lever_returned, restart_probe, restart);
  - our recorder (pings.jsonl): pings from the agent's run ('parent') and from the other work SHOW lists;
  - the stand-in outside service (mocks/outside.jsonl): the uses of the agent's key and token it accepted.
Times are seconds after the stop was sent, on the monotonic clock, which the harness used for all its timing
(on the WSL host it ran 8.33 % slow; rule R6.13 of RULES-expA.md). LangGraph's log is stamped with the wall clock (UTC),
which ran slow the same way and was stepped forward by a few seconds about every 32 s while the trials ran.
The recorder stamps both clocks on every ping (as does the outside service on every line), so each log line is
placed with the offset between the two clocks at the nearest moment one of those lines was stamped. Log lines are
shown with their terminal color codes removed; the lines listed in KEY_LINES are shown in full, wrapped at spaces,
and other long lines are cut at 200 characters; nothing else is changed.

    python3 experiments/scripts/walkthrough_langgraph.py RESULTS_DIR TRIAL [TRIAL ...] > \\
        experiments/docs/walkthroughs/LANGGRAPH.md
"""

import datetime
import json
import os
import re
import sys
import textwrap

sys.stdout.reconfigure(encoding="utf-8")  # the pages are UTF-8 whatever the console uses
R1 = sys.argv[1]
COLOR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z")
PROBLEM = re.compile(r"Z \[(warning|error|critical)\s*\]")  # LangGraph's warnings and errors are all shown
WINDOW = (-60.0, 100.0)  # seconds around the stop
KEY_LINES = ("Cancelled runs", "Background run interrupted", "Background run failed")  # log lines shown in full

SHOW = {  # per trial: other work of the agent's whose pings to print, and other answers of LangGraph's to print
    "XA-X-L1-t1": {"pings": ["bgproc", "cron"], "status": ["helper_run", "crons"]},
}

HEADER = """# LangGraph: the real log lines around two stops

These are two of the LangGraph trials of 9 October 2026, run on a WSL 2 host (Ubuntu 24.04). The platform is
LangGraph's development server (`langgraph dev`, langgraph-api 0.15.1). The agent is a script with no AI model, so
every trial plants the same work. On LangGraph a *run* is one execution of an agent, and a *thread* holds a run's
saved state. Trial names read as experiment A, agent version X (the version that starts all the work below), the
stop's code, and the trial number: XA-X-L1-t1 is the cancel, XA-X-L3-t1 the thread delete.

When the harness (the script that runs each trial) starts the agent's run, recorded as `parent`, the run starts
more work of its own: a second LangGraph run (`helper`), a child process of the server (`bgproc`), a process
detached from the server (`det`), a process that sleeps and then wakes (`sleeper`), a line in the system's
crontab (`oscron`), and a job in LangGraph's own scheduler, which starts a run of the graph `tick` each minute
(recorded as `cron`). At every step the run also uses a key and a token at a stand-in outside service. Each of
these sends a ping to our recorder while it is alive (runs and processes about once a second, scheduled jobs each
time they fire), and the outside service writes down every use of the key or token, accepted or refused. One
stop is sent per trial. The harness then watches for 90 s, asks LangGraph about the run at set times, and at the
end stops the server with SIGTERM and starts it again on the same saved state, to see what comes back (the
restart check).

Each block below is printed by `experiments/scripts/walkthrough_langgraph.py` from the trial's record files. Each line
under a
time is copied, not retyped, and names its file and line number, so it can be opened and checked; the paragraph above
each block is our reading of it. From LangGraph's own log, a block shows the lines that name the agent's run or
its thread, the line where the server starts shutting down, every warning or error, and, for the cancel trial,
the runs of the agent's scheduled job. Times are seconds after the stop was sent, on the monotonic clock, which the
harness used for all its timing. On the WSL 2 host that clock ran 8.33 % slow, because the kernel's tick
was set to 9167 µs instead of 10000 µs (found on 10 October; rule R6.13 of
`experiments/harness/n8n-langgraph-7a3fcec/langgraph/RULES-expA.md`). So every duration on this page, the 90 s watch
included, is
about 8 % shorter than in real time (+90 s is about 98 real seconds); the order of events is unaffected. The wall
clock (UTC), which stamps LangGraph's log, ran slow the same way and was stepped forward by the time service about
every 32 s (2.7 s at the median). The recorder stamps both clocks on every ping, which is how the steps show, so
two UTC times in LangGraph's log cannot be subtracted directly; the script places each log line with the offset
between the clocks at that moment. The log lines the paragraphs rely on are shown in full, wrapped at spaces;
other long lines are cut at 200 characters with ` ...`.

The record files are in `experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1/`. This page is the
script's
output, made from the top of the repository with:

```bash
python3 experiments/scripts/walkthrough_langgraph.py \\
    experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1 \\
    XA-X-L1-t1 XA-X-L3-t1 > experiments/docs/walkthroughs/LANGGRAPH.md
```
"""

NOTES = {  # per trial: what to look at in the block; our reading, written after looking at the lines
    "XA-X-L1-t1": (
        "What to look at. LangGraph's own log records the cancel at +0.031 s (`Cancelled runs`) and the end of the "
        "agent's run at +0.033 s (`Background run interrupted`, with the same run ID as the run's pings). Asked at "
        "+0.063 s, LangGraph answers that the run and its thread are `interrupted`; the answer was back before "
        "+0.12 s, when the harness's next check began (field `host_right_after`). The run itself goes quiet: its "
        "last ping arrives at +0.209 s, and the outside service accepts its key and token for the last time at "
        "+0.218 s and +0.232 s, after that answer. The work the run had started keeps going. Its child process "
        "`bgproc` pings through the whole watch, the last time in the watch at +89.06 s, and again at +93.08 s, "
        "after the server was restarted. LangGraph's own scheduler starts the agent's scheduled job at the next two "
        "minutes: its log shows `Starting background run` for the graph `tick` at +27.16 s and +82.21 s, and the "
        "job pings at +27.17 s and +82.22 s. In every answer, before and after the restart, LangGraph lists that "
        "job as `enabled`, and the helper run the agent started as `running` (after the restart, `pending`). When "
        "the harness stops the server for the restart check, LangGraph's log reports `Background run failed, will "
        "retry` for the two runs still executing, the helper run and an unrelated control run (`graph_id=helper` "
        "and `graph_id=collateral`), each followed by `RETRYING`, which matches the restarted server listing the "
        "helper run as `pending`. The `UserWarning` at +27.165 s says that LangGraph's `durability` setting has no "
        "effect without a checkpointer; it is not about the stop."
    ),
    "XA-X-L3-t1": (
        "What to look at. The stop deletes the agent's thread. The call returns at +0.033 s, and LangGraph's log has "
        "no line for the delete. Asked at +0.064 s, LangGraph answers 404 (not found) for both the run and the "
        "thread, and its log shows those two reads at +0.098 s and +0.101 s; every later answer, including the one "
        "after the restart, is the same 404. The run keeps working all the same: it pings 87 times after the stop, "
        "the last at +89.52 s, and the outside service keeps accepting its key and token, the last time at "
        "+89.55 s, just before the watch ends at +90 s. At +90.255 s the harness stops the server with SIGTERM for "
        "the restart check, and LangGraph's own log then reports `Background run failed, will retry` for this run "
        "(the same run ID and thread ID as above, `run_ended_at` 14:01:11.88), followed by `RETRYING`, and the same "
        "for the two other runs still executing, the helper run and the control run (`graph_id=helper` and "
        "`graph_id=collateral`). So in "
        "LangGraph's own worker the run was still executing until the shutdown, 90 s after the delete, while its "
        "API had been answering 404 for it since +0.1 s. After the restart the run does not come back: the "
        "restarted server starts the helper run and an unrelated control run again (lines 43 and 44 of "
        "`logs/server_20261009-145908_002.log`) but not the agent's run, and no further pings arrive from it. The "
        "`UserWarning` at +27.088 s is the same `durability` warning as in the cancel trial; it is not about the "
        "stop."
    ),
}


def numbered_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                yield n, line.rstrip("\n"), json.loads(line)


def short(text, width=200):
    return text if len(text) <= width else text[:width] + " ..."


def walkthrough(trial_id):
    trial_line, trial = next(
        (n, t) for n, _, t in numbered_jsonl(os.path.join(R1, "trials.jsonl")) if t["trial"] == trial_id
    )
    tl = f"trials.jsonl:{trial_line}"
    show = SHOW.get(trial_id, {})
    t_stop, t_end = trial["t_stop"], trial["t_end_watch"]
    pings = [x for x in numbered_jsonl(os.path.join(R1, "pings.jsonl")) if x[2].get("trial") == trial_id]
    outside = [x for x in numbered_jsonl(os.path.join(R1, "mocks", "outside.jsonl")) if x[2].get("trial") == trial_id]
    # moments where one process stamped both clocks at once: (wall, mono)
    refs = (
        [(p["t"], p["mono"]) for _, _, p in pings]
        + [(e["wall"], e["mono"]) for _, _, e in outside if "wall" in e]
        + [(trial["t_stop_wall"], t_stop)]
    )

    def rel_of_wall(w):  # offset taken at the nearest stamped moment
        rw, rm = min(refs, key=lambda r: abs(r[0] - w))
        return w - (rw - rm) - t_stop

    events = []  # (seconds after the stop, source, lines)

    # LangGraph's own log: the first server and the one restarted for the restart check
    names = {v for k, v in (trial.get("ids") or {}).items() if k.startswith("parent") and isinstance(v, str)}
    tick = "cron" in show.get("pings", [])
    for log in [trial["server_start"]["log"], (trial.get("restart") or {}).get("log")]:
        if not log:
            continue
        with open(os.path.join(R1, log), encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                clean = COLOR.sub("", line).rstrip()
                m = STAMP.match(clean)
                if not m or not (
                    any(x in clean for x in names)
                    or "Shutting down background workers" in clean
                    or PROBLEM.search(clean)
                    or tick
                    and "graph_id=tick" in clean
                    and ("Starting background run" in clean or "Background run succeeded" in clean)
                ):
                    continue
                w = datetime.datetime.strptime(m.group(1)[:26], "%Y-%m-%dT%H:%M:%S.%f")
                events.append(
                    (
                        rel_of_wall(w.replace(tzinfo=datetime.UTC).timestamp()),
                        f"LangGraph's own log, {log}:{n}",
                        [clean],
                    )
                )

    # what the harness did
    events.append(
        (
            0.0,
            f"harness sends the stop, {tl} fields lever, lever_desc, t_stop",
            [json.dumps({k: trial.get(k) for k in ("lever", "lever_desc", "t_stop")})],
        )
    )
    events.append(
        (
            trial["t_lever_returned"] - t_stop,
            f"harness: the stop call returns, {tl} fields t_lever_returned, lever_result",
            [json.dumps({k: trial.get(k) for k in ("t_lever_returned", "lever_result")})],
        )
    )
    hr = trial.get("host_right_after") or {}
    if "t" in hr:
        events.append(
            (
                hr["t"] - t_stop,
                f"harness starts its next check (the host's processes), {tl} field host_right_after",
                [json.dumps({"t": hr["t"]})],
            )
        )
    rp, rs = trial.get("restart_probe") or {}, trial.get("restart") or {}
    if "t_server_stop" in rp:
        events.append(
            (
                rp["t_server_stop"] - t_stop,
                f"harness stops the server for the restart check, {tl} field restart_probe",
                [json.dumps({k: rp[k] for k in ("t_server_stop", "how")})],
            )
        )
    if "t_ready" in rs:
        events.append(
            (
                rs["t_ready"] - t_stop,
                f"harness: the restarted server answers, {tl} field restart",
                [json.dumps({k: rs.get(k) for k in ("pid", "log", "wiped_state", "t_ready")})],
            )
        )

    # LangGraph's answers when the harness asked
    for field in ("status_right_after", "status_after_5s", "status_end", "status_after_restart"):
        s = trial.get(field) or {}
        if "t" in s:
            keys = ["parent_run", "parent_thread"] + show.get("status", [])
            events.append(
                (
                    s["t"] - t_stop,
                    f"harness asks LangGraph; its answers, {tl} field {field}",
                    [f"{k}: {json.dumps(s[k])}" for k in keys if k in s],
                )
            )

    # our recorder: the agent's run, then the other work listed in SHOW
    counts = []
    for canary in ["parent"] + show.get("pings", []):
        rows = [x for x in pings if x[2].get("canary") == canary]
        after = [x for x in rows if x[2]["mono"] > t_stop]
        watch = [x for x in after if x[2]["mono"] <= t_end]
        if canary == "parent":
            shown = [x for x in rows if x[2]["mono"] <= t_stop][-3:] + after[:3] + after[-2:]
        else:  # the first two after the stop, the last in the watch, the first after the restart
            shown = after[:2] + watch[-1:] + [x for x in after if x[2]["mono"] >= rs.get("t_ready", float("inf"))][:1]
        for n, raw, p in shown:
            events.append((p["mono"] - t_stop, f"our recorder, pings.jsonl:{n}", [raw]))
        counts.append(
            f"`{canary}` {len(watch)}" + (f", the last at {watch[-1][2]['mono'] - t_stop:+.2f} s" if watch else "")
        )

    # the outside service: accepted uses of the agent's key and token
    used = [
        x
        for x in outside
        if x[2].get("event") == "effect" and x[2].get("marker") in ("key_use", "tok_use") and x[2]["mono"] > t_stop - 2
    ]
    used_watch = [x for x in used if t_stop < x[2]["mono"] <= t_end]
    for n, raw, e in used[:6] + used_watch[-2:]:
        events.append((e["mono"] - t_stop, f"outside service, mocks/outside.jsonl:{n}", [raw]))

    rows, seen = [], set()
    for rel, src, lines in sorted(events, key=lambda x: x[0]):
        if WINDOW[0] <= rel <= WINDOW[1] and (src, tuple(lines)) not in seen:
            seen.add((src, tuple(lines)))
            rows.append((rel, src, lines))
    when = datetime.datetime.fromtimestamp(trial["t_stop_wall"], datetime.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"## {trial_id}: {trial.get('lever_desc', '')}\n")
    print(
        f"Stop sent at {when} UTC ({tl}, fields t_stop and t_stop_wall). Counted from the record files, after the "
        f"stop and within the {t_end - t_stop:.0f} s watch: pings from "
        + "; from ".join(counts)
        + f". Uses of the agent's key or token the outside service accepted in that time: {len(used_watch)}"
        + (f", the last at {used_watch[-1][2]['mono'] - t_stop:+.2f} s." if used_watch else ".")
    )
    if trial_id in NOTES:
        print(f"\n{NOTES[trial_id]}")
    print("\n```text")
    for rel, src, lines in rows:
        print(f"{rel:+9.3f} s  [{src}]")
        for line in lines:
            if any(k in line for k in KEY_LINES):  # shown in full, wrapped at spaces
                print(
                    "\n".join(
                        textwrap.wrap(
                            line,
                            150,
                            initial_indent=" " * 12,
                            subsequent_indent=" " * 14,
                            break_long_words=False,
                            break_on_hyphens=False,
                        )
                    )
                )
            else:
                print(f"            {short(line)}")
    print("```\n")


print(HEADER)
for t in sys.argv[2:]:
    walkthrough(t)
