#!/bin/bash
# Run the Experiment B feasibility rerun (spike2.py) in a fresh network namespace with only loopback.
#   wsl.exe -d Ubuntu --exec bash /mnt/c/work/agent-shutdown-test/stopcheck/n8n/spike2_ns.sh
set -euo pipefail
exec unshare --net -- bash -c 'ip link set lo up && exec /root/pilot-venv/bin/python -u /mnt/c/work/agent-shutdown-test/stopcheck/n8n/spike2.py'
