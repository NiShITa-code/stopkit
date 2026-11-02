#!/bin/bash
# Feasibility check for the OS-schedule carrier (run once per machine set-up, as root):
#  1. the recorder runs inside a fresh network namespace, as in a trial, with a Unix socket;
#  2. a one-shot canary run from the root namespace reaches it through the socket;
#  3. a real crontab entry fires within 70 s and its ping is recorded.
# Leaves nothing behind: the crontab line is tagged and removed, the recorder is stopped.
set -uo pipefail
P=/mnt/c/work/agent-shutdown-test/stopcheck/langgraph
PY=${PY:-/root/pilot-venv/bin/python}
T=$(mktemp -d /tmp/oscron-check-XXXX)
SOCK=/run/stopcheck/check-rec.sock
TAG="# stopcheck-check-oscron"
unshare --net -- bash -c "ip link set lo up; exec $PY -u $P/recorder.py --port 18765 --out $T/pings.jsonl --sock $SOCK" > "$T/rec.log" 2>&1 &
REC=$!
for i in $(seq 1 40); do [ -S "$SOCK" ] && break; sleep 0.25; done
PILOT_RECORDER_SOCK=$SOCK $PY $P/canary.py direct CHK 0 once
( crontab -l 2>/dev/null | grep -v "$TAG"; echo "* * * * * PILOT_RECORDER_SOCK=$SOCK $PY $P/canary.py oscron CHK 0 once $TAG" ) | crontab -
echo "crontab entry added at $(date +%T); waiting up to 70 s for it to fire"
for i in $(seq 1 70); do grep -q '"oscron"' "$T/pings.jsonl" 2>/dev/null && break; sleep 1; done
( crontab -l 2>/dev/null | grep -v "$TAG" ) | crontab -
kill $REC 2>/dev/null; wait $REC 2>/dev/null
echo "--- pings recorded:"; cat "$T/pings.jsonl"
echo "--- crontab after cleanup:"; crontab -l 2>/dev/null | grep -c "$TAG" || true
grep -q '"direct"' "$T/pings.jsonl" && echo "PASS socket reachable from the root namespace" || echo "FAIL socket not reachable"
grep -q '"oscron"' "$T/pings.jsonl" && echo "PASS cron entry fired and reached the recorder" || echo "FAIL cron entry did not reach the recorder"
rm -f "$SOCK"
