#!/bin/bash
# P1 reproduction outside the harness (RULES-expB.md B.11; finding P1 in cloud-results/cb1013/RESULTS-cb1013.md).
# Question: after n8n's main process restarts, does POST /api/v1/executions/{id}/stop change only the database row
# while the queue job goes on running?
# Uses curl, systemctl, read-only psql and redis-cli queries, and the standalone sink. No StopCheck code.
# Run setup.sh first, as root. The output is written to $R/out/p1-<time>.txt.
#
# Rule, fixed before the first run:
#   Phase A (control): the stop goes to the main that enqueued the run. Expected: HTTP 200, and no tick from that
#     run reaches the sink later than 5 s after the stop.
#   P1 reproduces only if phase A behaves as expected AND, in phase B (main restarted between the run's start and
#   the stop), all of these hold:
#     1. the stop replies HTTP 200 with status "canceled";
#     2. at +5 s both the public API and the Postgres row read "canceled";
#     3. the sink receives ticks from the run between +30 s and +60 s;
#     4. the run's job is still in Bull's active list at +60 s.
#   If phase A also keeps ticking, the stop does not act in this set-up at all: that is not P1, so P1 is not
#   reproduced.
#   Recorded but outside the verdict: a second stop on the phase B run (predicted HTTP 409), and a read after
#   n8n's default 300 s task timeout has passed (does the loop end; does the row still read "canceled").
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/lib.sh"
source "$R/key.env"
OUT="$R/out/p1-$(date -u +%Y%m%dT%H%M%SZ).txt"
exec > >(tee "$OUT") 2>&1

api() { curl -s -H "X-N8N-API-KEY: $KEY" "$@"; }
plus() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.3f", a + b }'; }
wait_until() { sleep "$(awk -v t="$1" -v w="$2" -v n="$(date +%s.%N)" 'BEGIN { d = t + w - n; print (d > 0 ? d : 0) }')"; }
# status EXEC: status and stoppedAt from the public API
status() {
  api "$BASE/api/v1/executions/$1" |
    python3 -c 'import json, sys; d = json.load(sys.stdin); print(d.get("status"), d.get("stoppedAt"))' 2> /dev/null ||
    echo "no-reply"
}
# row EXEC: status and stoppedAt from Postgres (read only)
row() { psql -h 127.0.0.1 -p "$PG_PORT" -U postgres -d n8n -Atc "select status, \"stoppedAt\" from execution_entity where id = $1"; }
# active: execution ids of the jobs in Bull's active list
active() {
  local j
  for j in $(redis-cli -p "$REDIS_PORT" lrange bull:jobs:active 0 -1); do
    redis-cli -p "$REDIS_PORT" hget "bull:jobs:$j" data |
      python3 -c 'import json, sys; print(json.load(sys.stdin)["executionId"], end=" ")'
  done
  echo
}
# nticks EXEC FROM TO: number of sink ticks from that run that arrived in (FROM, TO], epoch seconds
nticks() {
  python3 -I -c '
import json, sys
ex, lo, hi = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
rows = (json.loads(line) for line in open(sys.argv[4]))
print(sum(1 for r in rows if str(r["body"].get("exec")) == ex and lo < r["t"] <= hi))' "$1" "$2" "$3" "$SINK_LOG"
}
# last_tick EXEC REF: the run's last tick number and its arrival time relative to REF
last_tick() {
  python3 -I -c '
import json, sys
ex, ref = sys.argv[1], float(sys.argv[2])
rows = [json.loads(line) for line in open(sys.argv[3])]
mine = [r for r in rows if str(r["body"].get("exec")) == ex]
print("i=%s at %+.2f s" % (mine[-1]["body"]["i"], mine[-1]["t"] - ref) if mine else "none")' "$1" "$2" "$SINK_LOG"
}
# fire [EXCLUDED_EXEC]: start one run through the production webhook; print its execution id, taken from the
# first new sink tick that does not come from EXCLUDED_EXEC
fire() {
  local n0 i id
  n0=$(wc -l < "$SINK_LOG")
  for ((i = 0; i < 60; i++)); do curl -sf -o /dev/null -X POST "$BASE/webhook/repro-agent" && break; sleep 1; done
  for ((i = 0; i < 60; i++)); do
    id=$(tail -n "+$((n0 + 1))" "$SINK_LOG" | python3 -I -c '
import json, sys
for line in sys.stdin:
    ex = str(json.loads(line)["body"].get("exec"))
    if ex != sys.argv[1]:
        print(ex); break' "${1:-none}")
    [ -n "$id" ] && { echo "$id"; return 0; }
    sleep 1
  done
  return 1
}
# show EXEC LABEL T_STOP: one read of every source
show() {
  echo "$(ts) [$2] exec $1 | API: $(status "$1") | Postgres: $(row "$1") | Bull active: $(active)| sink ticks since stop: $(nticks "$1" "$3" "$(date +%s.%N)"), last $(last_tick "$1" "$3")"
}

cat "$R/out/versions.txt"

echo "== $(ts) Phase A (control): the stop goes to the main that enqueued the run"
A=$(fire) || { echo "run A never ticked"; exit 1; }
echo "$(ts) run A is execution $A"
sleep 15
TA=$(date +%s.%N)
REPLY_A=$(api -w '\n%{http_code}' -X POST "$BASE/api/v1/executions/$A/stop")
echo "$(ts) stop A reply: $(head -1 <<< "$REPLY_A") [HTTP $(tail -1 <<< "$REPLY_A")]"
A_CODE=$(tail -1 <<< "$REPLY_A")
for w in 5 30 60; do
  wait_until "$TA" "$w"
  show "$A" "+${w}s" "$TA"
done
A_LATE=$(nticks "$A" "$(plus "$TA" 5)" "$(plus "$TA" 60)")

echo "== $(ts) Phase B: restart main, then stop"
B=$(fire "$A") || { echo "run B never ticked"; exit 1; }
FB=$(date +%s.%N)
echo "$(ts) run B is execution $B"
sleep 15
show "$B" "before restart" "$FB"
T0=$(date +%s.%N)
systemctl stop n8n-repro-main
T1=$(date +%s.%N)
start_unit main
wait_ready "$BASE" 300 || { echo "main did not come back"; exit 1; }
T2=$(date +%s.%N)
echo "$(ts) main stop took $(plus "$T1" "-$T0") s; start to ready $(plus "$T2" "-$T1") s"
show "$B" "after restart, before stop (ticks counted from run start)" "$FB"
TB=$(date +%s.%N)
REPLY_B=$(api -w '\n%{http_code}' -X POST "$BASE/api/v1/executions/$B/stop")
echo "$(ts) stop B reply: $(head -1 <<< "$REPLY_B") [HTTP $(tail -1 <<< "$REPLY_B")]"
B_CODE=$(tail -1 <<< "$REPLY_B")
B_STATUS=$(head -1 <<< "$REPLY_B" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("status"))' 2> /dev/null)
[ "$(awk -v a="$TB" -v b="$FB" 'BEGIN { print (a - b > 220) }')" = 1 ] &&
  echo "WARNING: the stop came more than 220 s after the run started; the +60 s window may overlap the 300 s task timeout"
for w in 5 30 60 120; do
  wait_until "$TB" "$w"
  show "$B" "+${w}s" "$TB"
  if [ "$w" = 5 ]; then B_API5=$(status "$B" | cut -d' ' -f1); B_ROW5=$(row "$B" | cut -d'|' -f1); fi
  if [ "$w" = 60 ]; then B_ACT60=$(active); fi
done
B_MID=$(nticks "$B" "$(plus "$TB" 30)" "$(plus "$TB" 60)")

REPLY_B2=$(api -w '\n%{http_code}' -X POST "$BASE/api/v1/executions/$B/stop")
echo "$(ts) second stop on B: $(head -1 <<< "$REPLY_B2") [HTTP $(tail -1 <<< "$REPLY_B2")]"

echo "== $(ts) waiting until 330 s after run B started (n8n's default task timeout is 300 s)"
wait_until "$FB" 330
show "$B" "run start +330s" "$TB"
echo "sink ticks from run B in the 30 s before this read: $(nticks "$B" "$(plus "$FB" 300)" "$(plus "$FB" 330)")"

echo "== worker log lines naming a stop, abort, cancel or task timeout"
grep -nE "[Ss]top|[Aa]bort|[Cc]ancel|[Tt]imeout|[Tt]imed out" "$R/logs/worker.log" | tail -60
echo "== main log lines naming recovery, crashed, stop or abort"
grep -nE "[Rr]ecover|crashed|[Ss]top|[Aa]bort" "$R/logs/main.log" | tail -60

yn() { if eval "$1"; then echo yes; else echo no; fi; }
C0=$(yn '[ "$A_CODE" = 200 ] && [ "$A_LATE" = 0 ]')
C1=$(yn '[ "$B_CODE" = 200 ] && [ "$B_STATUS" = canceled ]')
C2=$(yn '[ "$B_API5" = canceled ] && [ "$B_ROW5" = canceled ]')
C3=$(yn '[ "$B_MID" -gt 0 ]')
C4=$(yn 'grep -qw "$B" <<< "$B_ACT60"')
echo "== Verdict (rule at the top of this script)"
echo "control: stop A HTTP $A_CODE, ticks from A between +5 s and +60 s: $A_LATE -> $C0"
echo "1. stop B HTTP $B_CODE, status $B_STATUS -> $C1"
echo "2. at +5 s API $B_API5, Postgres $B_ROW5 -> $C2"
echo "3. ticks from B between +30 s and +60 s: $B_MID -> $C3"
echo "4. Bull active at +60 s: $B_ACT60-> $C4"
if [ "$C0$C1$C2$C3$C4" = yesyesyesyesyes ]; then echo "P1 REPRODUCED"; else echo "P1 NOT REPRODUCED"; fi
