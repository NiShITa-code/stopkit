"""Check a walkthrough page against the record files it quotes.

For every line under a time (and every untimed line) the page names a file and line number, or a field of the
harness's record. This script opens each one and checks that what the page shows is what the file holds:
  - a quoted log or JSON line must equal the file's line (after removing terminal color codes), or be its start
    when the page cut it with ' ...', or, where the page shows only some fields, match the line in every field
    it shows;
  - a record field must hold the value the page shows;
  - where a line carries its own clock stamp (mono, smono or, for OpenClaw, t_wall), the time the page gives it
    must equal that stamp minus the stop's time;
  - every number with a decimal point in a block's "What to look at" paragraph must appear in that block (its
    times, its lines or its counted sentence), within the rounding the paragraph uses.
It prints each failure and ends with a count. Exit status 0 only if nothing failed.

    python3 experiments/scripts/check_pages.py experiments/docs/walkthroughs/LANGGRAPH.md \
        experiments/harness/n8n-langgraph-7a3fcec/langgraph/results/expA-round1
    python3 experiments/scripts/check_pages.py experiments/docs/walkthroughs/N8N.md \\
        experiments/n8n-queue-mode/records/cb1013
    python3 experiments/scripts/check_pages.py experiments/docs/walkthroughs/TEMPORAL.md \\
        experiments/temporal-activities/records/cs1014
    python3 experiments/scripts/check_pages.py experiments/docs/walkthroughs/OPENCLAW.md \
        experiments/openclaw-commands/records/runs/spike-20261014-071212
"""

import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
PAGE, BASE = sys.argv[1], sys.argv[2]
KIND = os.path.basename(PAGE).split(".")[0].lower()  # langgraph, n8n, temporal or openclaw
COLOR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TIMED = re.compile(r"^\s*([+-]\d+\.\d+) s  \[(.+)\]$")
UNTIMED_LOG = re.compile(r"^((?:logs/)?[\w.-]+\.(?:log|out)):(\d+)(?:  \(the same line again at (.+)\))?$")
LINE_REF = re.compile(r"([\w./-]+\.(?:log|jsonl|out|err)):(\d+)")
FAIL, CHECKED = [], [0]


def fail(where, msg):
    FAIL.append(f"{where}: {msg}")


def file_line(path, n):
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i == n:
                return COLOR.sub("", line).rstrip("\n").rstrip()
    return None


def as_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def same_shown(shown, full):
    """The page's text against the full text: equal, or a cut start, or (for JSON objects) equal in every field
    the page shows; whitespace is compared loosely because long lines are wrapped at spaces."""
    if shown.endswith(" ..."):
        return " ".join(full.split()).startswith(" ".join(shown[:-4].split()))
    a, b = as_json(shown), as_json(full)
    if isinstance(a, dict) and isinstance(b, dict):
        return all(k in b and b[k] == v for k, v in a.items())
    if a is not None and b is not None and not isinstance(a, dict):
        return a == b
    return " ".join(shown.split()) == " ".join(full.split())


def parse(page):
    """Blocks of the page: heading, the sentence lines, the paragraph, and the entries of each text fence."""
    blocks, cur, fence, entry = [], None, False, None
    for raw in open(page, encoding="utf-8").read().splitlines():
        if raw.startswith("## "):
            cur = {"heading": raw[3:], "text": [], "entries": []}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        if raw.startswith("```"):
            fence = raw.strip() == "```text"
            entry = None
            continue
        if not fence:
            cur["text"].append(raw)
            continue
        m = TIMED.match(raw)
        if m:
            entry = {"time": float(m.group(1)), "label": m.group(2), "lines": []}
            cur["entries"].append(entry)
        elif raw.startswith(" " * 14) and entry and entry["lines"] and not raw.startswith(" " * 15):
            entry["lines"][-1] += " " + raw.strip()
        elif raw.startswith(" " * 12) and entry is not None:
            entry["lines"].append(raw.strip())
        elif raw.strip():
            entry = {"time": None, "label": raw.strip(), "lines": []}
            cur["entries"].append(entry)
    return blocks


def spans(text):
    out = []
    for part in text.split(","):
        a, _, b = part.strip().partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def raw_folder(block):
    m = re.search(r"Raw folder `([^`]+)`", "\n".join(block["text"]))
    if not m:
        return BASE
    return (
        os.path.join(BASE, m.group(1))
        if os.path.isdir(os.path.join(BASE, m.group(1)))
        else glob.glob(os.path.join(BASE, "**", m.group(1)), recursive=True)[0]
    )


def check_block(block):
    head = block["heading"].split(":")[0]
    folder = raw_folder(block)
    rec, t0, trial_line = None, None, None
    if KIND == "langgraph":
        for n, line in enumerate(open(os.path.join(folder, "trials.jsonl"), encoding="utf-8"), 1):
            t = json.loads(line)
            if t["trial"] == head:
                rec, t0, trial_line = t, t["t_stop"], n
    elif KIND in ("n8n", "temporal"):
        rec = json.load(open(os.path.join(folder, "record.json"), encoding="utf-8"))
        t0 = rec["intervention"]["t_stop"]["mono"]
    else:
        rec = json.load(open(os.path.join(folder, "record.json"), encoding="utf-8"))
    for e in block["entries"]:
        where = f"{head}, {e['label'][:70]}" + (f" at {e['time']:+.3f} s" if e["time"] is not None else "")
        CHECKED[0] += 1
        label = e["label"]
        um = UNTIMED_LOG.match(label)
        lm = LINE_REF.search(label)
        if um:  # an untimed log line, maybe with repeats
            path = os.path.join(folder, um.group(1))
            first = file_line(path, int(um.group(2)))
            if first is None or not same_shown(e["lines"][0], first):
                fail(where, f"page shows {e['lines'][0][:80]!r}, file has {str(first)[:80]!r}")
            for n in spans(um.group(3)) if um.group(3) else []:
                if file_line(path, n) != first:
                    fail(where, f"line {n} is not the same line")
        elif lm and not lm.group(1).endswith("trials.jsonl"):
            path = os.path.join(folder, lm.group(1))
            full = file_line(path, int(lm.group(2)))
            shown = " ".join(e["lines"])
            if full is None or not same_shown(shown, full):
                fail(where, f"page shows {shown[:80]!r}, file has {str(full)[:80]!r}")
                continue
            j = as_json(full)
            if isinstance(j, dict) and e["time"] is not None:
                stamp = j.get("mono", j.get("smono")) if KIND != "openclaw" else j.get("t_wall")
                if stamp is not None and t0 is not None and abs(stamp - t0 - e["time"]) > 0.0006:
                    fail(where, f"time should be {stamp - t0:+.3f} s")
        else:
            check_record(where, label, e, rec, trial_line, folder)
        if KIND == "openclaw" and e["time"] == 0.0 and "Gateway's log" in label:
            t0 = None  # the anchor is the Gateway line itself; ping times are checked against it below
    if KIND == "openclaw":
        anchor = [e for e in block["entries"] if e["time"] == 0.0]
        if anchor:
            m = re.match(r"(\d{4}-\d\d-\d\dT[\d:.]+)\+00:00", anchor[0]["lines"][0])
            import datetime

            ta = (
                datetime.datetime.strptime(m.group(1)[:26], "%Y-%m-%dT%H:%M:%S.%f")
                .replace(tzinfo=datetime.UTC)
                .timestamp()
            )
            for e in block["entries"]:
                j = as_json(" ".join(e["lines"])) if e["lines"] else None
                if isinstance(j, dict) and "t_wall" in j and abs(j["t_wall"] - ta - e["time"]) > 0.0006:
                    fail(f"{head}, {e['label'][:60]}", f"time should be {j['t_wall'] - ta:+.3f} s")
    check_paragraph(block)


def subset(shown_text, obj):
    a = as_json(shown_text)
    if shown_text.endswith(" ..."):
        return json.dumps(obj).startswith(shown_text[:-4])
    return isinstance(a, dict) and isinstance(obj, dict) and all(k in obj and obj[k] == v for k, v in a.items())


def describe(part):
    d = json.loads(part["stdout"])
    pend = [
        {k: a.get(k) for k in ("activityId", "state", "attempt", "lastWorkerIdentity")}
        for a in d.get("pendingActivities") or []
    ]
    return {"workflowExecutionInfo.status": d["workflowExecutionInfo"].get("status"), "pendingActivities": pend}


def check_record(where, label, e, rec, trial_line, folder):
    lines = e["lines"]
    if KIND == "langgraph":
        m = re.search(r"trials\.jsonl:(\d+) fields? ([\w, ]+)$", label)
        if not m or int(m.group(1)) != trial_line:
            return fail(where, "cannot find the trial line")
        fields = [x.strip() for x in m.group(2).split(",")]
        if lines[0].startswith("{"):
            obj = rec if len(fields) > 1 else rec.get(fields[0])
            if not subset(lines[0], obj):
                fail(where, "fields differ from trials.jsonl")
        else:
            for line in lines:
                k, _, v = line.partition(": ")
                if json.loads(v) != (rec.get(fields[0]) or {}).get(k):
                    fail(where, f"{k} differs from trials.jsonl")
        return
    if KIND == "n8n":
        iv = rec["intervention"]
        rm = re.search(r"reads\[([^\]]+)\]", label)
        if rm and "stoppedAt" in label:
            rd = next(x for x in rec["reads"] if x["label"] == rm.group(1))
            row = next(x for x in rd["pg_rows"] if x["id"] == "2")
            if json.loads(lines[0]) != row["stoppedAt"]:
                fail(where, "stoppedAt differs")
        elif rm:
            rd = next(x for x in rec["reads"] if x["label"] == rm.group(1))
            if abs(rd["mono"] - iv["t_stop"]["mono"] - e["time"]) > 0.0006:
                fail(where, f"time should be {rd['mono'] - iv['t_stop']['mono']:+.3f} s")
            row = [x for x in rd.get("pg_rows") or [] if x.get("id") == "2"]
            want = {
                'exec_status["2"]': (rd.get("exec_status") or {}).get("2"),
                "pg_rows, id 2": row[0] if row else None,
                "pg_rows, ids present": [x.get("id") for x in rd.get("pg_rows") or []],
                "redis.queues.active": ((rd.get("redis") or {}).get("queues") or {}).get("active"),
                "timings": rd.get("timings"),
            }
            for line in lines:
                k, _, v = line.partition(": ")
                if k not in want or json.loads(v) != want[k]:
                    fail(where, f"{k} differs from record.json")
        elif "intervention.reply" in label:
            if (
                json.loads(lines[0]) != iv["reply"]
                or abs(iv["t_return"]["mono"] - iv["t_stop"]["mono"] - e["time"]) > 0.0006
            ):
                fail(where, "reply or its time differs")
        elif "intervention.ops" in label:
            m = re.search(r"ops\[(\d+)\](?:\.attempts\[(\d+)\])?", label)
            op = iv["ops"][int(m.group(1))]
            a = op["attempts"][int(m.group(2))] if m.group(2) else op
            if not subset(lines[0], a) or abs(a["mono"] - iv["t_stop"]["mono"] - e["time"]) > 0.0006:
                fail(where, "op or its time differs")
        elif "intervention.restarts" in label:
            rs = iv["restarts"][int(re.search(r"restarts\[(\d+)\]", label).group(1))]
            if not subset(lines[0], rs):
                fail(where, "restart fields differ")
        elif "intervention.t_stop" in label:
            if json.loads(lines[0]) != {"condition": rec["condition"], "t_stop": iv["t_stop"]}:
                fail(where, "stop fields differ")
        else:
            fail(where, "no check for this source")
        return
    if KIND == "temporal":
        t0 = rec["intervention"]["t_stop"]["mono"]
        if "intervention.actions" in label:
            a = rec["intervention"]["actions"][int(re.search(r"actions\[(\d+)\]", label).group(1))]
            if not subset(lines[0], a):
                fail(where, "action fields differ")
        elif "show_target" in label:
            rd = next(x for x in rec["reads"] if x["label"] == re.search(r"reads\[([^\]]+)\]", label).group(1))
            ev = {x["eventId"]: x for x in json.loads(rd["parts"]["show_target"]["stdout"])["events"]}
            shown = json.loads(lines[0])
            if not subset(lines[0], ev.get(shown["eventId"])):
                fail(where, "history event differs")
        elif "claim_poll" in label or "reads[" in label:
            if "claim_poll" in label:
                part = rec["claim_poll"][int(re.search(r"claim_poll\[(\d+)\]", label).group(1))]["parts"][
                    "describe_target"
                ]
            else:
                rd = next(x for x in rec["reads"] if x["label"] == re.search(r"reads\[([^\]]+)\]", label).group(1))
                part = rd["parts"]["describe_target"]
            want = describe(part)
            for line in lines:
                k, _, v = line.partition(": ")
                if json.loads(v) != want.get(k):
                    fail(where, f"{k} differs from record.json")
            if abs(part["t_end"] - t0 - e["time"]) > 0.0006:
                fail(where, f"time should be {part['t_end'] - t0:+.3f} s")
        else:
            fail(where, "no check for this source")
        return
    if KIND == "openclaw":
        shown = lines[0]
        if "cmds/0020.out" in label:
            sessions = json.load(open(os.path.join(folder, "cmds", "0020.out"), encoding="utf-8"))["sessions"]
            key = json.loads(shown)["key"]
            if not subset(shown, next(s for s in sessions if s["key"] == key)):
                fail(where, "session fields differ")
        elif "cmds/0019.out" in label:
            if json.loads(shown) != json.load(open(os.path.join(folder, "cmds", "0019.out"), encoding="utf-8")):
                fail(where, "reply differs")
        elif label.startswith("observations."):
            name = label.split()[0].split(".", 1)[1]
            if not subset(shown, rec["observations"][name]):
                fail(where, "observation fields differ")
        elif label.startswith("gateway_stop"):
            if not subset(shown, rec["gateway_stop"]):
                fail(where, "gateway_stop fields differ")
        else:
            fail(where, "no check for this source")


def check_paragraph(block):
    para = [x for x in block["text"] if x.startswith("What to look at.")]
    if not para:
        return
    pool = [f"{e['time']:+.3f}" for e in block["entries"] if e["time"] is not None]
    pool += re.findall(r"[+-]?\d+\.\d+", " ".join(" ".join(e["lines"]) for e in block["entries"]))
    pool += re.findall(r"[+-]?\d+\.\d+", "\n".join(x for x in block["text"] if not x.startswith("What to look at.")))
    values = [float(x) for x in pool]
    for num in re.findall(r"(?<![\w.:-])([+-]?\d+\.\d+)(?= s\b| s,| s\.| s\)|\)|,| UTC)", para[0]):
        CHECKED[0] += 1
        dec = len(num.split(".")[1])
        x = float(num)
        # a number written with its sign must match with that sign; an unsigned one may match either
        signed = num[0] in "+-"
        if not any(abs((v - x) if signed else (abs(v) - abs(x))) <= 0.5 * 10**-dec + 1e-9 for v in values):
            fail(f"{block['heading'].split(':')[0]}, paragraph", f"{num} not found in its block")


for b in parse(PAGE):
    check_block(b)
for f in FAIL:
    print("FAIL", f)
print(f"{os.path.basename(PAGE)}: {CHECKED[0]} items checked, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
