#!/bin/bash
# Run an Experiment A script inside a fresh Linux network namespace that has only the
# loopback interface, so nothing the experiment starts can reach the internet.
# Based on pilot-02-validation/run_pilot.sh; folder changed to stopcheck/langgraph.
# Usage (from Windows):
#   wsl.exe -d Ubuntu --exec bash -c 'bash /mnt/c/work/agent-shutdown-test/stopcheck/langgraph/run_trial_ns.sh [harness args]'
# Needs root (the WSL default user here is root). Keep a wsl.exe session open during
# batches: WSL stops an idle distro after 15 s and the VM after 60 s.
# On a Linux machine, as root: bash run_trial_ns.sh [harness args]
set -euo pipefail
P=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)  # this script's own folder (was the laptop's path)
PY=${PY:-/root/pilot-venv/bin/python}
SCRIPT=${SCRIPT:-harness.py}
exec unshare --net -- bash -c 'ip link set lo up && exec "$0" -u "$1/$2" "${@:3}"' "$PY" "$P" "$SCRIPT" "$@"
