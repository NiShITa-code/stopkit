#!/bin/bash
# E1 batch runner (21 Oct 2026): N repeats of setup -> P1 -> teardown for one n8n version; archives kept per repeat.
# Usage, as root: N8N_VER=2.41.7 bash run_e1.sh 3
set -uo pipefail
N=${1:-3}; V=${N8N_VER:?set N8N_VER}
mkdir -p /root/e1
for i in $(seq 1 "$N"); do
  echo "=== $V repeat $i start $(date -u +%FT%TZ)"
  rm -f /root/n8n-repro-*.tgz
  ( cd /root/repro && N8N_VER=$V bash setup.sh && N8N_VER=$V bash p1_stop_after_restart.sh ); rc=$?
  ( cd /root/repro && bash teardown.sh ) > /dev/null 2>&1
  d=/root/e1/$V-r$i; mkdir -p "$d"; mv /root/n8n-repro-*.tgz "$d/" 2>/dev/null
  echo "=== $V repeat $i end $(date -u +%FT%TZ) rc=$rc archive=$(ls $d)"
done
