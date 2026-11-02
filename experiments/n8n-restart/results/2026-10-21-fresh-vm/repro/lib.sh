# Shared constants and helpers for the B.11 reproductions (sourced, not run). No StopCheck code.
R=/srv/n8n-repro
NODE=/opt/node-v24.21.0/bin/node
N8N_VER=${N8N_VER:-2.41.7}   # 21 Oct: the version is a setting so the same scripts run on newer releases
N8N=/opt/n8n-$N8N_VER/node_modules/n8n/bin/n8n
PGBIN=/usr/lib/postgresql/17/bin
PG_PORT=5434
REDIS_PORT=6381
SINK_PORT=9911
BASE=http://127.0.0.1:5678
SINK_LOG=$R/logs/sink.jsonl

ts() { date -u +%H:%M:%S.%3N; }

# start_unit main|worker: run n8n as a systemd transient unit under the unprivileged n8ntest user.
start_unit() {
  local cmd=start
  [ "$1" = worker ] && cmd=worker
  systemd-run --unit="n8n-repro-$1" --collect --quiet -p User=n8ntest -p EnvironmentFile="$R/$1.env" \
    -p WorkingDirectory="$R/n8n-$1" -p StandardOutput="append:$R/logs/$1.log" \
    -p StandardError="append:$R/logs/$1.log" -- "$NODE" "$N8N" "$cmd"
}

# wait_ready BASE_URL SECONDS: wait until n8n's readiness endpoint answers 200.
wait_ready() {
  local i
  for ((i = 0; i < $2; i++)); do
    curl -sf -o /dev/null "$1/healthz/readiness" && return 0
    sleep 1
  done
  return 1
}
