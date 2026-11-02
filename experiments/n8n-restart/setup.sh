#!/bin/bash
# Set-up for the B.11 reproductions (RULES-expB.md B.11). Uses no StopCheck code. Run as root on a StopCheck VM.
#
# Starts a private Postgres 17 and Redis, the standalone sink (sink.py), and n8n $N8N_VER (default 2.41.7) in queue mode (one main,
# one worker) as systemd transient units. n8n keeps its defaults except for these settings:
#   - database and queue connection settings, and a freshly generated encryption key (queue mode needs them);
#   - a separate user folder for each process, a second task-broker port for the worker, and the worker's health
#     server on port 5680 (two n8n processes on one host; the health server tells us when the worker is ready);
#   - N8N_LOG_LEVEL=debug, so n8n prints its stop and abort messages (it changes no behaviour);
#   - telemetry and version checks off, and N8N_SECURE_COOKIE=false (this set-up logs in over plain http).
# Left at n8n's defaults: task timeout 300 s, graceful shutdown 30 s, worker concurrency 10, node exclusions,
# Redis persistence. The owner login and the encryption key are generated here and kept in $R (mode 600).
# teardown.sh archives the logs and outputs and removes everything.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/lib.sh"

[ -e "$R" ] && { echo "$R already exists; run teardown.sh first"; exit 1; }
for p in 5678 5679 5680 5682 $PG_PORT $REDIS_PORT $SINK_PORT; do
  if ss -Hltn "sport = :$p" | grep -q .; then echo "port $p is in use"; exit 1; fi
done

install -d -m 755 "$R"
install -d -m 1777 "$R/logs"
install -d -m 700 "$R/out"
install -d -o postgres "$R/pg" "$R/pgsock"
install -d -o redis "$R/redis"
install -d -o n8ntest "$R/n8n-main" "$R/n8n-worker"

{
  echo "n8n $(python3 -c 'import json; print(json.load(open("/opt/n8n-'"$N8N_VER"'/node_modules/n8n/package.json"))["version"])')"
  echo "node $("$NODE" --version)"
  "$PGBIN/postgres" --version
  redis-server --version
  echo "kernel $(uname -r); host $(hostname)"
} > "$R/out/versions.txt"

# Postgres and Redis, private to this set-up.
runuser -u postgres -- "$PGBIN/initdb" -D "$R/pg" -A trust -U postgres > /dev/null
runuser -u postgres -- "$PGBIN/pg_ctl" -D "$R/pg" -l "$R/logs/postgres.log" -w \
  -o "-p $PG_PORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=$R/pgsock" start > /dev/null
"$PGBIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U postgres n8n
runuser -u redis -- redis-server --bind 127.0.0.1 --port "$REDIS_PORT" --dir "$R/redis" --daemonize yes \
  --pidfile "$R/redis/redis.pid" --logfile "$R/logs/redis.log"
for ((i = 0; i < 30; i++)); do redis-cli -p "$REDIS_PORT" ping 2> /dev/null | grep -q PONG && break; sleep 0.5; done

# The sink.
touch "$SINK_LOG"
setsid nohup python3 -I "$HERE/sink.py" "$SINK_PORT" "$SINK_LOG" > "$R/logs/sink.out" 2>&1 &
echo $! > "$R/sink.pid"

# Generated throwaway values; never printed.
umask 077
OWNER_EMAIL=owner@repro.test
OWNER_PW="Rp1$(openssl rand -hex 12)"
ENC_KEY=$(openssl rand -hex 24)
printf 'OWNER_EMAIL=%s\nOWNER_PW=%s\n' "$OWNER_EMAIL" "$OWNER_PW" > "$R/owner.env"

common_env() {
  cat << EOF
PATH=/opt/node-v24.21.0/bin:/usr/bin:/bin
DB_TYPE=postgresdb
DB_POSTGRESDB_HOST=127.0.0.1
DB_POSTGRESDB_PORT=$PG_PORT
DB_POSTGRESDB_DATABASE=n8n
DB_POSTGRESDB_USER=postgres
EXECUTIONS_MODE=queue
QUEUE_BULL_REDIS_HOST=127.0.0.1
QUEUE_BULL_REDIS_PORT=$REDIS_PORT
N8N_ENCRYPTION_KEY=$ENC_KEY
N8N_LISTEN_ADDRESS=127.0.0.1
N8N_LOG_LEVEL=debug
N8N_DIAGNOSTICS_ENABLED=false
N8N_VERSION_NOTIFICATIONS_ENABLED=false
N8N_SECURE_COOKIE=false
EOF
}
{ common_env; echo "HOME=$R/n8n-main"; echo "N8N_USER_FOLDER=$R/n8n-main"; } > "$R/main.env"
{
  common_env
  echo "HOME=$R/n8n-worker"
  echo "N8N_USER_FOLDER=$R/n8n-worker"
  echo "N8N_RUNNERS_BROKER_PORT=5682"
  echo "QUEUE_HEALTH_CHECK_ACTIVE=true"
  echo "QUEUE_HEALTH_CHECK_PORT=5680"
} > "$R/worker.env"
umask 022

start_unit main
wait_ready "$BASE" 300 || { echo "main did not become ready"; exit 1; }
start_unit worker
wait_ready http://127.0.0.1:5680 180 || { echo "worker did not become ready"; exit 1; }
echo "$(ts) main and worker ready"

# Owner, login and a public-API key with only the scopes the reproductions use.
json() { python3 -c 'import json, sys; a = sys.argv[1:]; print(json.dumps(dict(zip(a[::2], a[1::2]))))' "$@"; }
curl -sf -o /dev/null -X POST "$BASE/rest/owner/setup" -H 'Content-Type: application/json' \
  -d "$(json email "$OWNER_EMAIL" firstName Repro lastName Owner password "$OWNER_PW")"
curl -sf -o /dev/null -c "$R/cookies" -X POST "$BASE/rest/login" -H 'Content-Type: application/json' \
  -d "$(json emailOrLdapLoginId "$OWNER_EMAIL" password "$OWNER_PW")"
KEY=$(curl -sf -b "$R/cookies" -X POST "$BASE/rest/api-keys" -H 'Content-Type: application/json' \
  -d '{"label": "repro", "expiresAt": null, "scopes": ["workflow:create", "workflow:read", "workflow:activate",
       "execution:read", "execution:list", "execution:stop"]}' |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["data"]["rawApiKey"])')

# The workflow: a production webhook, then one Code node that posts a tick to the sink once a second, 600 times.
python3 -I - "$(cat /proc/sys/kernel/random/uuid)" "$SINK_PORT" > "$R/agent.json" << 'EOF'
import json, sys
uuid, port = sys.argv[1], sys.argv[2]
js = ("for (let i = 1; i <= 600; i++) {\n"
      "  try { await this.helpers.httpRequest({ method: 'POST', url: 'http://127.0.0.1:%s/tick',\n"
      "        body: { exec: $execution.id, i }, json: true, timeout: 3000 }); } catch (e) {}\n"
      "  await new Promise((r) => setTimeout(r, 1000));\n"
      "}\n"
      "return [{ json: { done: true } }];\n") % port
print(json.dumps({
    "name": "repro-agent", "settings": {"executionOrder": "v1"},
    "nodes": [
        {"id": "n1", "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [0, 0],
         "webhookId": uuid, "parameters": {"httpMethod": "POST", "path": "repro-agent", "responseMode": "onReceived"}},
        {"id": "n2", "name": "Loop", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [300, 0],
         "parameters": {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": js}}],
    "connections": {"Webhook": {"main": [[{"node": "Loop", "type": "main", "index": 0}]]}}}))
EOF
CREATED=$(curl -sf -X POST "$BASE/api/v1/workflows" -H "X-N8N-API-KEY: $KEY" -H 'Content-Type: application/json' \
  --data @"$R/agent.json")
WF=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])' <<< "$CREATED")
VID=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["versionId"])' <<< "$CREATED")
curl -sf -o /dev/null -X POST "$BASE/api/v1/workflows/$WF/publish" -H "X-N8N-API-KEY: $KEY" \
  -H 'Content-Type: application/json' -d "{\"versionId\": \"$VID\"}"

umask 077
printf 'KEY=%s\nWF=%s\n' "$KEY" "$WF" > "$R/key.env"
echo "$(ts) set-up done: workflow $WF published"
