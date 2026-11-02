"""OpenClaw run 3 of 14 October 2026 (a Linux virtual machine): the real lines around two stops.

Nothing here is retyped. Each line under a time is copied from a record file and labeled with the file and line
number (for the runner's record, record.json, the field), so anyone can open the file and find it. The sources,
all written while the run ran:
  - the Gateway's own logs (gateway.out and gateway.err), whose lines carry UTC times;
  - our recorder (recorder.jsonl): the pings of the commands the agent ran, one a second while each is alive;
  - OpenClaw's answers to the runner's CLI calls (cmds/NNNN.out), which carry no time of their own except the
    times OpenClaw writes into them;
  - the runner's record (record.json), whose observations are printed after each block, untimed.
The runner did not record when it sent each stop, so each block counts seconds from the Gateway's own log line for
that stop. The recorder stamps each ping with the machine's wall clock, the same clock the Gateway's times come
from, and that clock stayed in step with the monotonic clock to within 0.1 ms across the run. Long lines are cut
at 200 characters with ' ...'; nothing else is changed.

    python3 openclaw_walkthrough.py RUN_DIR > OPENCLAW.md
"""

import datetime
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")  # the pages are UTF-8 whatever the console uses
RUN = sys.argv[1]
STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)\+00:00")
TAG = "stopcheck-f-af49d070-"

HEADER = """# OpenClaw: the real lines around two stops

This is the third of the three unscored OpenClaw runs of 14 October 2026, made on a Linux virtual machine with OpenClaw
2026.10.2. OpenClaw's *Gateway* is the long-running process that runs the agent. A scripted stand-in model drives
it, so the agent always runs the same commands. The runs were not scored: each observation below comes
from this one run. The agent runs *commands* through OpenClaw's `exec` tool, either in the foreground (the agent
waits for them) or in the background (they go on while the agent continues). Each command sends a ping to our
recorder once a second, under a name that gives its role: `hold-fg` and `hold`, the foreground and background
commands of the run that the first stop aborts; `twin`, a background command in another session, which no stop
targets before the Gateway stop; and `pk`, a background command that OpenClaw's own process tool later kills, as
a positive control. The two stops are an abort of the run through the Gateway's `sessions.abort` call, and,
later, SIGTERM to the Gateway process.

Each block below is printed by `experiments/scripts/walkthrough_openclaw.py` from the run's folder. Each line under a
time is
copied, not retyped, and names its file and line number, so it can be opened and checked; the paragraph above each block
is our reading of it. The Gateway's log lines carry UTC times, and the recorder stamps each ping with the same
machine's clock, so all lines share one clock. The runner did not record when it sent each stop, so each block
counts seconds from the Gateway's own log line for that stop: `sessions.abort requested` for the first and
`received SIGTERM; shutting down` for the second. The runner's own summary of each stop (`record.json`
observations) follows its block. Long lines are cut at 200 characters with ` ...`.

The run's folder is `experiments/openclaw-commands/records/runs/spike-20261014-071212/`, and
[What each stop reached](../WHAT-EACH-STOP-REACHES.md) covers the OpenClaw runs. This page is the script's
output, made from the top of the repository with:

```bash
python3 experiments/scripts/walkthrough_openclaw.py experiments/openclaw-commands/records/runs/spike-20261014-071212 \\
    > experiments/docs/walkthroughs/OPENCLAW.md
```
"""

NOTES = {  # per block: what to look at; our reading, written after looking at the lines
    "abort": (
        "What to look at. The run that is stopped (run id 78411da0...) began its turn at -5.111 s (gateway.out:36), "
        "and its foreground command `hold-fg` and background command `hold` ping once a second. The Gateway logs "
        "the abort request at 0 (gateway.out:37) and the run's turn ending with `This operation was aborted` at "
        "+0.081 s and +0.084 s (gateway.err:24-25). OpenClaw replies `aborted` for that run (cmds/0019.out), and "
        "its session list then shows the session as `killed`, with an end time 0.110 s after the request "
        "(cmds/0020.out). The foreground command stops: its last ping is at -0.336 s, just before the Gateway "
        "logged the request. The background command does not stop. It goes on pinging once a second, 353 times "
        "after the request, until the Gateway shuts down at the end of the second stop below; its last ping comes "
        "353.125 s after the request, at 07:19:53.537 UTC. The runner's own counts, printed below, start just "
        "before it launched the abort command and end 10 s after the command returned (spike_f.py:1801-1807), so "
        "they count the foreground ping at -0.336 s as after the stop and show only 11 background pings. A "
        "background command can be ended from inside OpenClaw: its own process tool killed the runner's positive "
        "control `pk`, which then stopped pinging (`observations.positive_control_process_kill`)."
    ),
    "sigterm": (
        "What to look at. The Gateway logs the SIGTERM at 0, closes admission and starts to drain: `draining active "
        "work before stop with timeout 315000ms: backgroundExecSessions=2` (gateway.out:43-46). Our recorder shows "
        "two background commands alive, `twin` and the aborted run's `hold`, and both go on pinging once a second "
        "through the whole drain, while the Gateway logs `still draining active work before stop: "
        "backgroundExecSessions=2` every 30 s (gateway.err:28-37). At +315.007 s it logs `active-work drain timeout "
        "reached; proceeding with shutdown` (gateway.err:38), and one millisecond later `active-work drain settled; "
        "beginning server close` (gateway.out:47), then `[shutdown] completed cleanly in 140ms` (gateway.out:50). "
        "The last pings arrive at +314.580 s (`twin`) and +314.939 s (`hold`), and none after. The runner records "
        "the Gateway's exit with code 0, 315.686 s after it sent the SIGTERM, with no SIGKILL needed, and the "
        "twin's process gone afterwards."
    ),
}


def wall(stamp):
    return datetime.datetime.strptime(stamp[:26], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=datetime.UTC).timestamp()


def utc(w):
    return datetime.datetime.fromtimestamp(w, datetime.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def short(text, width=200):
    return text if len(text) <= width else text[:width] + " ..."


def gateway_lines():
    out = []
    for name in ("gateway.out", "gateway.err"):
        with open(os.path.join(RUN, name), encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                m = STAMP.match(line)
                if m:
                    out.append((wall(m.group(1)), f"the Gateway's log, {name}:{n}", line.rstrip()))
    return out


def pings():
    with open(os.path.join(RUN, "recorder.jsonl"), encoding="utf-8") as f:
        return [(n, line.rstrip("\n"), json.loads(line)) for n, line in enumerate(f, 1) if line.strip()]


def block(key, title, t0, window, log_filter, ping_picks, extra, summary, counts):
    print(f"## {title}\n")
    print(f"The Gateway logged this stop at {utc(t0)} UTC. {counts}")
    if key in NOTES:
        print(f"\n{NOTES[key]}")
    rows = [
        (w - t0, src, [line])
        for w, src, line in gateway_lines()
        if window[0] <= w - t0 <= window[1] and log_filter(line)
    ]
    for n, raw, p in ping_picks:
        rows.append((p["t_wall"] - t0, f"our recorder, recorder.jsonl:{n}", [raw]))
    rows += extra
    print("\n```text")
    for rel, src, lines in sorted(rows, key=lambda x: x[0]):
        print(f"{rel:+9.3f} s  [{src}]")
        for line in lines:
            print(f"            {short(line)}")
    print("```\n")
    print(
        "Untimed: OpenClaw's reply where it gave one, and the runner's own record of this stop (record.json):"
        "\n\n```text"
    )
    for label, value in summary:
        print(f"{label}\n            {short(json.dumps(value), 400)}")
    print("```\n")


def main():
    r = json.load(open(os.path.join(RUN, "record.json"), encoding="utf-8"))
    obs = r["observations"]
    gl = gateway_lines()
    ps = pings()

    def by(role):
        return [x for x in ps if x[2]["canary"] == TAG + role]

    # 1. sessions.abort
    t_abort = next(w for w, _, line in gl if "sessions.abort requested" in line)
    run_id = obs["abort_sessions_abort"]["aborted_run_id"]
    fg, bg = by("hold-fg"), by("hold")
    bg_before = [x for x in bg if x[2]["t_wall"] <= t_abort]
    bg_after = [x for x in bg if x[2]["t_wall"] > t_abort]
    reply = open(os.path.join(RUN, "cmds", "0019.out"), encoding="utf-8").read()
    sessions = json.load(open(os.path.join(RUN, "cmds", "0020.out"), encoding="utf-8"))["sessions"]
    s3 = next(s for s in sessions if s["key"].endswith("stopcheck-f-s3"))
    extra = [
        (
            s3["endedAt"] / 1000 - t_abort,
            "OpenClaw's session list after the abort, cmds/0020.out (its endedAt time)",
            [json.dumps({k: s3.get(k) for k in ("key", "status", "abortedLastRun", "lastRunId", "endedAt")})],
        )
    ]
    later_fg = [x for x in fg if x[2]["t_wall"] > t_abort]
    block(
        "abort",
        "Abort the run: `openclaw gateway call sessions.abort`",
        t_abort,
        (-6, 12),
        lambda line: run_id[:8] in line or "stopcheck-f-s3" in line or "abort" in line.lower(),
        fg + bg_before[-3:] + bg_after[:6],
        extra,
        [
            ("OpenClaw's reply to the abort, cmds/0019.out", json.loads(reply)),
            (
                "observations.abort_sessions_abort (parts)",
                {
                    k: obs["abort_sessions_abort"][k]
                    for k in (
                        "abort_rc",
                        "abort_status",
                        "aborted_run_id_matches_hold",
                        "fg_carrier_pings_before_stop",
                        "fg_carrier_pings_after_stop",
                        "bg_canary_pings_before_stop",
                        "bg_canary_pings_after_stop",
                    )
                },
            ),
            ("observations.positive_control_process_kill", obs["positive_control_process_kill"]),
        ],
        f"Counted from recorder.jsonl: after that line the foreground command `hold-fg` pinged {len(later_fg)} "
        f"times, and the background command `hold` pinged {len(bg_after)} times, the last at "
        f"{bg_after[-1][2]['t_wall'] - t_abort:+.3f} s.",
    )

    # 2. SIGTERM to the Gateway
    t_term = next(w for w, _, line in gl if "received SIGTERM; shutting down" in line)
    t_timeout = next(w for w, _, line in gl if "drain timeout reached" in line)
    picks, parts = [], []
    for role in ("twin", "hold"):
        xs = by(role)
        before = [x for x in xs if x[2]["t_wall"] <= t_term]
        during = [x for x in xs if t_term < x[2]["t_wall"] <= t_timeout]
        after = [x for x in xs if x[2]["t_wall"] > t_timeout]
        picks += before[-1:] + during[:2] + during[-2:]
        parts.append(
            f"`{role}` pinged {len(during)} times between that line and the line `active-work drain "
            f"timeout reached` ({t_timeout - t_term:.3f} s later), the last at "
            f"{during[-1][2]['t_wall'] - t_term:+.3f} s, and {len(after)} times after it"
        )
    gs = r["gateway_stop"]
    block(
        "sigterm",
        "Stop the Gateway: SIGTERM to its process",
        t_term,
        (-2, 330),
        lambda line: True,
        picks,
        [],
        [
            (
                "gateway_stop (parts)",
                {
                    k: gs[k]
                    for k in (
                        "signal_target",
                        "pid",
                        "grace_s",
                        "sigterm_sent",
                        "escalated_to_sigkill",
                        "time_to_exit_s",
                        "returncode",
                    )
                },
            ),
            (
                "observations.gateway_stop_boundary (parts)",
                {
                    k: obs["gateway_stop_boundary"][k]
                    for k in (
                        "twin_tag",
                        "twin_pings_after_gateway_stop",
                        "twin_pid",
                        "twin_pid_alive_after_gateway_stop",
                    )
                },
            ),
        ],
        "Counted from recorder.jsonl: " + "; ".join(parts) + ".",
    )


print(HEADER)
main()
