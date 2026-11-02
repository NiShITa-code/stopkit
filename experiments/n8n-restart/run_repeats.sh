#!/bin/bash
# Repeats runner: N repeats of setup -> stop after a main restart (p1_stop_after_restart.sh) -> teardown for one n8n version; archives kept per repeat.
# Adapted from run_e1.sh, which ran these repeats on 21 Oct 2026 (results/2026-10-21-fresh-vm): the only change is
# that it runs the scripts from its own folder instead of /root/repro.
# Usage, as root: N8N_VER=2.41.7 bash run_repeats.sh 3
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
N=${1:-3}; V=${N8N_VER:?set N8N_VER}
mkdir -p /root/n8n-restart-repeats
for i in $(seq 1 "$N"); do
  echo "=== $V repeat $i start $(date -u +%FT%TZ)"
  rm -f /root/n8n-repro-*.tgz
  ( cd "$HERE" && N8N_VER=$V bash setup.sh && N8N_VER=$V bash p1_stop_after_restart.sh ); rc=$?
  ( cd "$HERE" && bash teardown.sh ) > /dev/null 2>&1
  d=/root/n8n-restart-repeats/$V-r$i; mkdir -p "$d"; mv /root/n8n-repro-*.tgz "$d/" 2>/dev/null
  echo "=== $V repeat $i end $(date -u +%FT%TZ) rc=$rc archive=$(ls $d)"
done
