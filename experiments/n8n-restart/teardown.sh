#!/bin/bash
# Stop everything setup.sh started, archive the logs and outputs to /root/n8n-repro-<time>.tgz, and remove $R.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/lib.sh"
[ "$R" = /srv/n8n-repro ] || { echo "unexpected R=$R"; exit 1; }
systemctl stop n8n-repro-worker n8n-repro-main 2> /dev/null
[ -f "$R/sink.pid" ] && kill "$(cat "$R/sink.pid")" 2> /dev/null
redis-cli -p "$REDIS_PORT" shutdown nosave > /dev/null 2>&1
[ -d "$R/pg" ] && runuser -u postgres -- "$PGBIN/pg_ctl" -D "$R/pg" -m fast -w stop > /dev/null
ARCHIVE=/root/n8n-repro-$(date -u +%Y%m%dT%H%M%SZ).tgz
tar -czf "$ARCHIVE" -C "$R" logs out agent.json
rm -rf /srv/n8n-repro
echo "archived to $ARCHIVE"
