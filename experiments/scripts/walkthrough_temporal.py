"""The Temporal trials of 14 October 2026 (records cs1014, a Linux virtual machine): the real lines around one stop.

Nothing here is retyped. Each line under a time is copied from a record file and labeled with where it is: a
file and line number for the files with one event per line, a field path for the harness's record (record.json),
so anyone can open the file and find it. The sources, all written while the trial ran:
  - the harness's record (record.json): the CLI command it ran as the stop (intervention.actions), its describe
    polls until the run read closed (claim_poll), its reads at set times (reads: describe gives the run's status
    and pending activities), and the run's history as `temporal workflow show` printed it at the last read;
  - the stand-in receiver (receiver.jsonl): each effect the target's activity posted, with its step;
  - the target worker's own record (worker-target-g1.jsonl) and the SDK's log lines (worker-target-g1.out).
Times are seconds after the stop was sent, on the VM's monotonic clock. History event times and the SDK's log
times are wall-clock times; the receiver stamps both clocks on every line, so they are placed with the offset
between the two at the nearest receiver line. For the receiver's and the worker's lines only some fields are
shown (the line number leads to the full line); the SDK's log lines are shown in full, with their terminal color
codes removed; nothing else is changed.

    python3 experiments/scripts/walkthrough_temporal.py RECORDS_DIR COND [COND ...] > \\
        experiments/docs/walkthroughs/TEMPORAL.md
"""

import datetime
import glob
import json
import os
import re
import sys
import textwrap

sys.stdout.reconfigure(encoding="utf-8")  # the pages are UTF-8 whatever the console uses
ROOT = sys.argv[1]
COLOR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z")
WINDOW = (-10.0, 121.0)  # seconds around the stop: the 120 s watch, before the durability probe
ACTIONS = {  # each condition's stop
    "C3-a2": "cancel during the non-heartbeating activity (C3, valid rerun)",
    "C2": "cancel during the heartbeating activity (C2)",
    "C5n": "terminate during the non-heartbeating activity (C5n)",
}
RECEIVER_FIELDS = ("seq", "mono", "role", "attempt", "exec_nonce", "step", "kind")
WORKER_SKIP = ("replies", "options", "swall", "pid", "identity", "workflow_id", "run_id")

HEADER = """# Temporal: the real lines around two cancels

These are two of the Temporal trials of 14 October 2026 (records cs1014), run on a Linux virtual machine, one trial
for each condition. The platform is Temporal's development server (Temporal CLI 1.9.1, Server 1.32.0,
`temporal server start-dev` with SQLite), with workers built on the Python SDK (temporalio 1.34.0). A *workflow
run* is Temporal's record of one job; the job's code runs on a *worker*, and its slow part runs as an
*activity*, a unit of work that Temporal hands to a worker and lists as *pending* until it closes. In each trial
the work activity posts 20 progress steps, one a second, and then a final effect, each an HTTP POST to our
stand-in receiver, which writes down its arrival. The stop falls 8.5 s into the activity, between steps 9 and
10. The two conditions differ only in the activity. In C3 (run as C3-a2, the valid rerun of C3) it never sends a
*heartbeat*, a report from the activity to the server that it is alive; in C2 it sends one after every step,
which the SDK passes on to the server at most once every 4 s. In both, the stop is the operator's cancel, the CLI
command `temporal workflow cancel`.

After the stop the harness asks Temporal through the CLI: `describe` polls until the run reads closed, then
`describe` at +0, +2, +5, +10, +20, +30, +60, +90 and +120 s gives the run's status and its pending activities.
`show` gives the run's *history*, its ordered list of events, of which the block shows those from the work
activity's scheduling on.

Each block below is printed by `experiments/scripts/walkthrough_temporal.py` from the trial's raw folder. Each line
under a time is
copied, not retyped, and names its file and line number, or for the harness's record (`record.json`) the field,
so it can be opened and checked; the paragraph above each block is our reading of it. Times are seconds after the
stop was sent, on the VM's monotonic clock. The cancel command returned 0.063 s after that. History times and
the SDK's log times are wall-clock times, placed with the receiver's paired stamps (the VM's two clocks agreed to
within 0.1 ms across each trial). For the receiver's and the worker's lines only the fields that matter are shown;
the line number leads to the full line.

The raw folders are in `experiments/temporal-activities/records/cs1014/` (one per trial, named in each block), and
[What each stop reached](../WHAT-EACH-STOP-REACHES.md) covers all 16 conditions. This page is the script's
output, made from the top of the repository with:

```bash
python3 experiments/scripts/walkthrough_temporal.py experiments/temporal-activities/records/cs1014 C3-a2 C2 \\
    > experiments/docs/walkthroughs/TEMPORAL.md
```
"""

NOTES = {  # per condition: what to look at in the block; our reading, written after looking at the lines
    "C3-a2": (
        "What to look at. The cancel command returns at +0.063 s. Temporal's history records the cancel request "
        "at +0.046 s (event 17), asks the activity to cancel (event 21) and closes the run CANCELED (event 22) at "
        "+0.104 s. The harness's first describe answers `CANCELED` at +0.192 s, and every describe to the end of "
        "the watch lists the work activity as pending, in state `CANCEL_REQUESTED`. The activity, which sends no "
        "heartbeat, hears nothing of this: steps 10 to 20 reach the receiver from +0.500 s to +10.501 s, and the "
        "final effect at +11.500 s, all from the execution that started at -8.50 s (`exec_nonce` "
        "b0623e9245d60aff). The worker records the activity as done at +11.501 s (`activity_done`, phase `posting "
        "final`), and when the SDK reports the completion, the server answers that the run is gone: `Activity not "
        "found on completion ... workflow execution already completed` (worker-target-g1.out:1, +11.504 s). "
        "Temporal's history has no ActivityTaskStarted event for this execution: after event 16, "
        "`ACTIVITY_TASK_SCHEDULED`, come only the cancel's events. The terminate condition, C5n, gives the same "
        "picture."
    ),
    "C2": (
        "What to look at. The same cancel, with the activity sending a heartbeat after every step. Temporal closes "
        "the run just as fast: event 22, `CANCELED`, at +0.091 s, and the first describe answers `CANCELED` at "
        "+0.190 s. The activity posts steps 10 to 13 (+0.499 s to +3.500 s) and learns of the cancel only at "
        "+3.505 s, when the SDK passes on its next heartbeat: the worker records `activity_cancelled` after step "
        "13, with the flag `not_found` (the server no longer knew the activity) and the phase `sleeping after 13`. "
        "Nothing more reaches the receiver: no step 14 and no final effect. The SDK logs `Completing activity as "
        "failed` and a `CancelledError` (worker-target-g1.out, lines 1 and 20, printed without a time). Every "
        "describe still lists the activity as pending, `CANCEL_REQUESTED`, to the end of the watch, the same as "
        "in C3-a2, where the activity ran to its end: Temporal's record does not show which of the two happened."
    ),
}


def numbered_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                yield n, line.rstrip("\n"), json.loads(line)


def short(text, width=200):
    return text if len(text) <= width else text[:width] + " ..."


def describe_lines(part):
    d = json.loads(part["stdout"]) if part.get("rc") == 0 and part.get("stdout") else None
    if d is None:
        return [f"rc {part.get('rc')}, stderr: {short(str(part.get('stderr_tail') or part.get('stderr')))}"]
    pend = [
        {k: a.get(k) for k in ("activityId", "state", "attempt", "lastWorkerIdentity")}
        for a in d.get("pendingActivities") or []
    ]
    return [
        f"workflowExecutionInfo.status: {json.dumps(d['workflowExecutionInfo'].get('status'))}",
        f"pendingActivities: {json.dumps(pend)}",
    ]


def walkthrough(cond):
    raw = glob.glob(os.path.join(ROOT, f"XC-screen-c-screen-1014-{cond}-2026*"))[0]
    r = json.load(open(os.path.join(raw, "record.json"), encoding="utf-8"))
    iv = r["intervention"]
    t0 = iv["t_stop"]["mono"]
    receiver = list(numbered_jsonl(os.path.join(raw, "receiver.jsonl")))
    pairs = [(e["t"] - e["mono"], e["mono"]) for _, _, e in receiver if "t" in e and "mono" in e]

    def rel_of_wall(stamp):  # an ISO time such as 2026-10-14T04:09:00.203014065Z
        w = (
            datetime.datetime.strptime(stamp.rstrip("Z")[:26], "%Y-%m-%dT%H:%M:%S.%f")
            .replace(tzinfo=datetime.UTC)
            .timestamp()
        )
        off = min(pairs, key=lambda p: abs(w - p[0] - p[1]))[0]
        return w - off - t0

    events = []  # (seconds after the stop, source, lines, show in full)
    for i, a in enumerate(iv.get("actions") or []):
        src = f"record.json intervention.actions[{i}]"
        events.append(
            (
                a["t_send"] - t0,
                f"harness runs the stop, {src}",
                [json.dumps({"name": a["name"], "argv": a["argv"], "t_send": a["t_send"]})],
                False,
            )
        )
        events.append(
            (
                a["t_return"] - t0,
                f"the CLI command returns, {src}",
                [json.dumps({k: a.get(k) for k in ("rc", "t_return", "stdout", "stderr")})],
                False,
            )
        )
    for i, poll in enumerate(r.get("claim_poll") or []):
        part = poll["parts"]["describe_target"]
        lines = describe_lines(part)
        events.append(
            (
                part["t_end"] - t0,
                f"harness's describe poll, answer arrived; record.json claim_poll[{i}] ({poll['label']})",
                lines,
                False,
            )
        )
        if "CANCELED" in lines[0] or "TERMINATED" in lines[0] or "COMPLETED" in lines[0]:
            break
    for rd in r["reads"]:
        part = rd["parts"]["describe_target"]
        events.append(
            (
                part["t_end"] - t0,
                f"harness's describe at {rd['label']} s, answer arrived; record.json reads[{rd['label']}]",
                describe_lines(part),
                False,
            )
        )
    last = r["reads"][-1]
    hist = json.loads(last["parts"]["show_target"]["stdout"])["events"]
    first = max(i for i, e in enumerate(hist) if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED")
    for e in hist[first:]:
        events.append(
            (
                rel_of_wall(e["eventTime"]),
                f"Temporal's history, event {e['eventId']}; record.json reads[{last['label']}].parts.show_target",
                [json.dumps({k: e[k] for k in ("eventId", "eventTime", "eventType")})],
                False,
            )
        )

    target = [x for x in receiver if x[2].get("role") == "target" and x[2].get("route") == "effect"]
    before = [x for x in target if x[2]["mono"] <= t0]
    after = [x for x in target if x[2]["mono"] > t0]
    for n, _, e in before[-1:] + after:
        events.append(
            (
                e["mono"] - t0,
                f"receiver, receiver.jsonl:{n}",
                [json.dumps({k: e.get(k) for k in RECEIVER_FIELDS})],
                False,
            )
        )
    for n, _, w in numbered_jsonl(os.path.join(raw, "worker-target-g1.jsonl")):
        if w.get("event") != "worker_start":
            events.append(
                (
                    w["smono"] - t0,
                    f"target worker, worker-target-g1.jsonl:{n}",
                    [json.dumps({k: v for k, v in w.items() if k not in WORKER_SKIP})],
                    True,
                )
            )
    untimed = []  # SDK log lines printed without a time: the first line of each message and its last line
    with open(os.path.join(raw, "worker-target-g1.out"), encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            clean = COLOR.sub("", line).rstrip()
            m = STAMP.match(clean)
            if m:
                events.append(
                    (rel_of_wall(m.group(1) + "Z"), f"the SDK's log, worker-target-g1.out:{n}", [clean], True)
                )
            elif clean and not clean[0].isspace() and not clean.startswith("Traceback"):
                untimed.append((n, clean))

    rows = sorted((e for e in events if WINDOW[0] <= e[0] <= WINDOW[1]), key=lambda x: x[0])
    stop_wall = t0 + min(pairs, key=lambda p: abs(p[1] - t0))[0]
    when = datetime.datetime.fromtimestamp(stop_wall, datetime.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    watch = [x for x in after if x[2]["mono"] <= r["watch_end_mono"]]
    lastx = watch[-1][2] if watch else None
    print(f"## {cond}: {ACTIONS[cond]}\n")
    print(
        f"Raw folder `{os.path.basename(raw)}`. Stop sent at {when} UTC. Counted from receiver.jsonl: after the stop "
        f"and within the {r['watch_end_mono'] - t0:.0f} s watch, {len(watch)} effects of the target's activity "
        f"arrived"
        + (f", the last at {lastx['mono'] - t0:+.3f} s (step {lastx['step']}, {lastx['kind']})." if lastx else ".")
    )
    if cond in NOTES:
        print(f"\n{NOTES[cond]}")
    print("\n```text")
    for rel, src, lines, full in rows:
        print(f"{rel:+9.3f} s  [{src}]")
        for line in lines:
            if full:
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
    if untimed:
        print(
            "The SDK's log lines printed without a time, in file order (the indented lines between them are a "
            "Python traceback):\n\n```text"
        )
        for n, line in untimed:
            print(f"worker-target-g1.out:{n}\n            {short(line)}")
        print("```\n")


print(HEADER)
for c in sys.argv[2:]:
    walkthrough(c)
