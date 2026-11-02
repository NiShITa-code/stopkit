#!/bin/bash
# Run the Experiment B feasibility spike in a fresh network namespace with only loopback (as run_trial_ns.sh).
#   wsl.exe -d Ubuntu --exec bash /mnt/c/work/agent-shutdown-test/stopcheck/n8n/spike_ns.sh
set -euo pipefail
exec unshare --net -- bash -c 'ip link set lo up && exec /root/pilot-venv/bin/python -u /mnt/c/work/agent-shutdown-test/stopcheck/n8n/spike.py'
