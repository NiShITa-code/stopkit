#!/bin/bash
# Recording wrapper (15 Oct 2026): runs the existing P1 reproduction unchanged (setup.sh, p1_stop_after_restart.sh,
# teardown.sh from stopcheck/n8n/repro) and, so the screen shows the agent's activity live, prints one line for each
# tick the sink receives. Display only: it reads the sink's log and changes nothing.
cd /root/repro-rec || exit 1
R=/srv/n8n-repro
echo "### $(date -u +%FT%TZ)  recording: n8n 2.41.7, stop after a main restart (reproduction of finding 1)"
echo "### step 1: set up a private n8n (main + worker), Postgres, Redis and the sink"
bash setup.sh || exit 1
echo "### step 2: run the reproduction; lines starting with 'tick' are the agent's signals arriving at the sink"
tail -q -n0 -F "$R/logs/sink.jsonl" 2>/dev/null | python3 -u -c '
import json, sys, datetime
for line in sys.stdin:
    try:
        r = json.loads(line)
    except ValueError:
        continue
    t = datetime.datetime.fromtimestamp(r["t"], datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    print("    tick  %s  from execution %s  (step %s)" % (t, r["body"].get("exec"), r["body"].get("i")), flush=True)
' &
WATCH=$!
bash p1_stop_after_restart.sh
kill "$WATCH" 2>/dev/null; pkill -f "tail -q -n0 -F $R/logs/sink.jsonl" 2>/dev/null
echo "### step 3: tear down and archive"
bash teardown.sh
echo "### $(date -u +%FT%TZ)  recording finished"
