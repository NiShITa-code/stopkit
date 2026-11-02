"""The n8n trials of 13 October 2026 (records cb1013, a Linux virtual machine): the real lines around one stop.

Nothing here is retyped. Each line under a time is copied from a record file and labeled with where it is: a
file and line number for the files with one event per line, a field path for the harness's record (record.json),
so anyone can open the file and find it. The sources, all written while the trial ran:
  - the harness's record (record.json): what the harness did and when (the stop, any restart of an n8n process),
    n8n's reply to the stop, and the harness's reads at set times, each holding n8n's public API answer for
    execution 2 (exec_status), the execution's row in n8n's Postgres database (pg_rows) and the active jobs in
    n8n's Redis queue (redis.queues.active);
  - our recorder (pings.jsonl): the pings of the agent's loop (canary n8n_loop_steps, execution 2);
  - the stand-in outside service (outside.jsonl): the uses of the agent's credential it accepted (cred_use);
  - n8n's own logs (logs/main.log, logs/worker.log). n8n printed these without times, so they are shown in a
    second block, in file order: the lines that name execution 2 and the lines about shutdowns, restarts and
    recovery. A line printed several times in a row is shown once, with the numbers of the lines that repeat it.
Times are seconds after the stop was sent, on the VM's monotonic clock. n8n's database times are wall-clock
times; on this VM the two clocks stayed within 0.1 ms of each other across each trial (record.json,
wall_clock_across_trial), so a database time is placed with the offset between the clocks at the nearest read.
Long lines are cut at 200 characters with ' ...'; nothing else is changed.

    python3 experiments/scripts/walkthrough_n8n.py RECORDS_DIR COND [COND ...] > experiments/docs/walkthroughs/N8N.md
"""

import datetime
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")  # the pages are UTF-8 whatever the console uses
ROOT = sys.argv[1]
ACTIONS = {  # each condition's action, with the request the harness sends
    "B1": "stop the agent by id; main still holds it (POST /api/v1/executions/2/stop)",
    "B1r": "restart main, then stop the agent by id (POST /api/v1/executions/2/stop)",
    "B4b": "archive, then delete, the agent workflow (POST /api/v1/workflows/{id}/archive, then "
    "DELETE /api/v1/workflows/{id}, retried once a second while n8n answers 409)",
}
LOG_PATTERNS = (
    "execution 2 ",
    "Execution 2 ",
    "Task cancelled",
    "Received SIGTERM",
    "Shutdown timed out",
    "n8n ready on",
    "Marked executions as",
    "Completed queue recovery",
    "Waiting for 2 active",
    "Drain timeout",
    "Stopping worker",
)

HEADER = """# n8n: the real lines around three stops

These are three of the n8n trials of 13 October 2026 (records cb1013), run on a Linux virtual machine, one trial for
each condition. n8n 2.41.7 runs in queue mode, as two processes: *main*, which takes requests, keeps the records in
a Postgres database and puts work on a queue (held in Redis by the Bull library), and a *worker*, which takes
work off the queue and runs it. Each start of a workflow is an *execution*, with a number. The agent is a
scripted workflow with no AI model. It runs as execution 2: a loop that, once a second, pings our recorder and
uses a credential at a stand-in outside service, and that starts further executions of its own (children, a
schedule, waits, a fan-out). Execution 1 is an unrelated workflow the harness (the script that runs each trial)
starts as a control. The three conditions are B1, a stop of the agent by its execution id while main still
holds it; B1r, the same stop after main has been restarted; and B4b, archiving and then deleting the agent's
workflow.

After the stop, the harness reads n8n at set times (+0, +5, +90 and +180 s, then later reads where the
condition has them). Each read holds n8n's public API answer for execution 2 (`exec_status`), the execution's
row in n8n's database (`pg_rows`) and the list of jobs the queue holds as active (`redis.queues.active`).
Within one read the harness reads the database first, then n8n's API (each answer carries the time it arrived,
`reply_mono`), then the queue; `timings` gives how long each part took, in seconds. So one read can show the
database from just before a change and the API or the queue from just after it. Our recorder gets the agent
loop's pings (`n8n_loop_steps`), and the outside service writes down each use of the agent's credential
(`cred_use`).

Each block below is printed by `experiments/scripts/walkthrough_n8n.py` from the trial's raw folder. Each line under a
time is
copied, not retyped, and names its file and line number, or for the harness's record (`record.json`) the field,
so it can be opened and checked; the paragraph above each block is our reading of it. Times are seconds after
the stop was sent, on the VM's monotonic clock; on this VM the wall clock and the monotonic clock stayed within
0.1 ms of each other across each trial, so n8n's database times can be placed directly. n8n printed its own log
lines without times, so they follow in a second block, in file order, and the paragraph says what places them.
Long lines are cut at 200 characters with ` ...`.

The raw folders are in `experiments/n8n-queue-mode/records/cb1013/` (one per trial, named in each block), and
[What each stop reached](../WHAT-EACH-STOP-REACHES.md) covers all 19 conditions. This page is the script's output, made
from the top of the repository with:

```bash
python3 experiments/scripts/walkthrough_n8n.py experiments/n8n-queue-mode/records/cb1013 B1 B1r B4b \\
    > experiments/docs/walkthroughs/N8N.md
```
"""

NOTES = {  # per condition: what to look at in the blocks; our reading, written after looking at the lines
    "B1": (
        "What to look at. This is the stop doing what it says, for comparison with B1r. The agent's last ping is "
        "at -0.529 s and its last accepted credential use at -0.526 s; nothing arrives from it after the stop. "
        "n8n's database stamps execution 2 `canceled` at +0.224 s, and the reply `canceled` arrives at +0.358 s. "
        "From the first read after the stop, the queue no longer lists job 2 as active. In n8n's own logs the "
        "worker prints `Task cancelled: Task cancelled by user` and `Worker errored while running execution 2 "
        "(job 2)` (worker.log:38-39), and main prints `Execution 2 (job 2) failed` (main.log:636). The `SIGTERM` "
        "lines further down are the harness's clean-up after the last read; nothing was restarted in this trial "
        "(record.json `restart_probe.events`)."
    ),
    "B1r": (
        "What to look at. The harness first restarts main. The restart begins at -166.65 s; main prints `Received "
        "SIGTERM. Shutting down...`, then `Waiting for 2 active executions to finish...` once a second, then "
        "`Shutdown timed out after 30 seconds` (main.log:620-640). The old process has stopped at -134.60 s, and "
        "the new one answers at -126.59 s (main.log:656). The new main runs its queue recovery (main.log:686-687), "
        "which marks the agent's in-process children `crashed` in the database while they keep running. "
        "Then comes the same stop as in B1: n8n's database stamps execution 2 `canceled` "
        "at +0.341 s, and the reply `canceled` arrives at +0.460 s. From +5.16 s on, every read gives `canceled` "
        "in the API and in the database, but the queue lists job 2 as active in every read, the last at +961.1 s. "
        "The agent does not stop: its loop pings 179 times in the 180 s watch and 979 times in all after the "
        "stop, the last at +989.00 s, and the outside service accepts its credential just as often, the last time "
        "at +989.01 s. The worker's log has no `Task cancelled` line, unlike B1. At the clean-up after the last "
        "read it prints `Waiting for 2 active executions to finish... (execution IDs: 1, 2)` (worker.log:93, "
        "repeated up to line 115) and only then `Worker finished execution 2 (job 2)` (worker.log:121). The "
        "worker was still running the execution n8n had been reporting as canceled for over 16 minutes."
    ),
    "B4b": (
        "What to look at. n8n accepts the archive at +0.285 s. It refuses the first delete with 409, `Workflow is "
        "still being unpublished` (+0.489 s), and accepts the second at +1.517 s. From the read at +5.10 s on, "
        "n8n's API answers 404 for execution 2 and the database has no row with id 2, but the queue lists job 2 "
        "as active in every read, the last at +240.8 s. The agent keeps going: its loop pings 179 times in the "
        "180 s watch and 298 times in all after the stop, the last at +298.88 s, and the outside service accepts "
        "its credential 298 times. n8n's logs have no line for the archive or the delete. At the clean-up after "
        "the last read, the worker prints `Waiting for 2 active executions to finish... (execution IDs: 1, 2)` "
        "(worker.log:67, repeated up to line 89) and then `Worker finished execution 2 (job 2)` (worker.log:96). "
        "The worker was still running the execution whose record n8n had deleted."
    ),
}


def numbered_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                yield n, line.rstrip("\n"), json.loads(line)


def short(text, width=200):
    return text if len(text) <= width else text[:width] + " ..."


def spans(nums):  # [625, 626, 627, 654] -> "625-627, 654"
    out, start = [], nums[0]
    for a, b in zip(nums, nums[1:] + [None], strict=False):
        if b != a + 1:
            out.append(f"{start}-{a}" if a != start else f"{a}")
            start = b
    return ", ".join(out)


def walkthrough(cond):
    raw = glob.glob(os.path.join(ROOT, "w*", "collect-001", "results", f"XB-screen-cb1013-w*-{cond}-2026*"))[0]
    r = json.load(open(os.path.join(raw, "record.json"), encoding="utf-8"))
    iv = r["intervention"]
    t0, watch_end = iv["t_stop"]["mono"], r["watch_end_mono"]
    offsets = [(rd["wall"] - rd["mono"], rd["mono"]) for rd in r["reads"]]  # wall minus mono at each read

    def rel_of_db(stamp):  # an n8n database time such as '2026-10-13 22:40:26.444+00'
        w = (
            datetime.datetime.strptime(stamp.replace("+00", ""), "%Y-%m-%d %H:%M:%S.%f")
            .replace(tzinfo=datetime.UTC)
            .timestamp()
        )
        off = min(offsets, key=lambda o: abs(w - o[0] - o[1]))[0]
        return w - off - t0

    events = []  # (seconds after the stop, source, lines)
    for i, rs in enumerate(iv.get("restarts") or []):
        src = f"harness, record.json intervention.restarts[{i}]"
        events.append(
            (
                rs["start_mono"] - t0,
                f"{src}: begins restarting {rs['role']}",
                [json.dumps({k: rs.get(k) for k in ("tag", "unit", "old_pid", "start_mono")})],
            )
        )
        events.append(
            (
                rs["stop_mono"] - t0,
                f"{src}: the old {rs['role']} process has stopped",
                [json.dumps({k: rs.get(k) for k in ("stop_mono", "stop_rc")})],
            )
        )
        events.append(
            (
                rs["ready_mono"] - t0,
                f"{src}: the new {rs['role']} process answers",
                [json.dumps({k: rs.get(k) for k in ("new_pid", "ready_mono")})],
            )
        )
    events.append(
        (
            0.0,
            "harness sends the stop, record.json condition and intervention.t_stop",
            [json.dumps({"condition": r["condition"], "t_stop": iv["t_stop"]})],
        )
    )
    if "reply" in iv:
        events.append(
            (
                iv["t_return"]["mono"] - t0,
                "n8n's reply to the stop, record.json intervention.reply",
                [json.dumps(iv["reply"])],
            )
        )
    for i, op in enumerate(iv.get("ops") or []):
        for j, a in enumerate(op.get("attempts") or [op]):
            path = f"intervention.ops[{i}]" + (f".attempts[{j}]" if "attempts" in op else "")
            events.append((a["mono"] - t0, f"n8n's reply to {op['op']}, record.json {path}", [json.dumps(a)]))
    for rd in r["reads"]:
        row = [x for x in rd.get("pg_rows") or [] if x.get("id") == "2"]
        lines = [
            f'exec_status["2"]: {json.dumps((rd.get("exec_status") or {}).get("2"))}',
            f"pg_rows, id 2: {json.dumps(row[0])}"
            if row
            else f"pg_rows, ids present: {json.dumps([x.get('id') for x in rd.get('pg_rows') or []])}",
            f"redis.queues.active: {json.dumps(((rd.get('redis') or {}).get('queues') or {}).get('active'))}",
            f"timings: {json.dumps(rd.get('timings'))}",
        ]
        events.append((rd["mono"] - t0, f"harness reads n8n, record.json reads[{rd['label']}]", lines))
        if row and row[0].get("stoppedAt") and not any(e[1].startswith("n8n's database") for e in events):
            events.append(
                (
                    rel_of_db(row[0]["stoppedAt"]),
                    f"n8n's database: execution 2's stoppedAt, from record.json reads[{rd['label']}].pg_rows",
                    [json.dumps(row[0]["stoppedAt"])],
                )
            )

    loop = [x for x in numbered_jsonl(os.path.join(raw, "pings.jsonl")) if x[2].get("canary") == "n8n_loop_steps"]
    before, after = [x for x in loop if x[2]["mono"] <= t0], [x for x in loop if x[2]["mono"] > t0]
    in_watch = [x for x in after if x[2]["mono"] <= watch_end]
    for n, line, p in before[-3:] + after[:3] + in_watch[-1:] + after[-2:]:
        events.append((p["mono"] - t0, f"our recorder, pings.jsonl:{n}", [line]))
    used = [
        x
        for x in numbered_jsonl(os.path.join(raw, "outside.jsonl"))
        if x[2].get("event") == "effect" and x[2].get("marker") == "cred_use"
    ]
    used_before, used_after = [x for x in used if x[2]["mono"] <= t0], [x for x in used if x[2]["mono"] > t0]
    for n, line, e in used_before[-1:] + used_after[:2] + used_after[-1:]:
        events.append((e["mono"] - t0, f"outside service, outside.jsonl:{n}", [line]))

    rows, seen = [], set()
    for rel, src, lines in sorted(events, key=lambda x: x[0]):
        if (src, tuple(lines)) not in seen:
            seen.add((src, tuple(lines)))
            rows.append((rel, src, lines))
    stop_wall = t0 + min(offsets, key=lambda o: abs(o[1] - t0))[0]
    when = datetime.datetime.fromtimestamp(stop_wall, datetime.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def last(xs):
        return f", the last at {xs[-1][2]['mono'] - t0:+.2f} s" if xs else ""

    print(f"## {cond}: {ACTIONS[cond]}\n")
    print(
        f"Raw folder `{os.path.relpath(raw, ROOT).replace(os.sep, '/')}`. Stop sent at {when} UTC. Counted from "
        f"the record files: after the stop the agent's loop pinged {len(after)} times ({len(in_watch)} within the "
        f"{watch_end - t0:.0f} s watch){last(after)}, and the outside service accepted the agent's credential "
        f"{len(used_after)} times{last(used_after)}."
    )
    if cond in NOTES:
        print(f"\n{NOTES[cond]}")
    print("\n```text")
    for rel, src, lines in rows:
        print(f"{rel:+10.3f} s  [{src}]")
        for line in lines:
            print(f"             {short(line)}")
    print("```\n")
    print("n8n's own logs, in file order (n8n printed them without times):\n\n```text")
    for name in ("main.log", "worker.log"):
        groups = []  # [text, [line numbers]]; a repeat of the previous selected line joins its group
        with open(os.path.join(raw, "logs", name), encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                text = line.rstrip()
                if text.startswith("Error:") or not any(p in text for p in LOG_PATTERNS):
                    continue
                if groups and groups[-1][0] == text:
                    groups[-1][1].append(n)
                else:
                    groups.append([text, [n]])
        for text, ns in groups:
            again = f"  (the same line again at {spans(ns[1:])})" if len(ns) > 1 else ""
            print(f"logs/{name}:{ns[0]}{again}\n             {short(text)}")
    print("```\n")


print(HEADER)
for c in sys.argv[2:]:
    walkthrough(c)
