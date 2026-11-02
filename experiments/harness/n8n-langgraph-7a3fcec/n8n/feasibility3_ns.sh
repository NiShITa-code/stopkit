#!/bin/bash
# Run the Experiment B setup checks (feasibility3.py) in a named network namespace with only loopback.
# The name is needed so systemd units can join it (NetworkNamespacePath=/run/netns/stopcheck-f3).
#   wsl.exe -d Ubuntu --exec bash /mnt/c/work/agent-shutdown-test/stopcheck/n8n/feasibility3_ns.sh
set -uo pipefail
NS=stopcheck-f3
ip netns del "$NS" 2>/dev/null
ip netns add "$NS"
ip netns exec "$NS" ip link set lo up
ip netns exec "$NS" /root/pilot-venv/bin/python -u /mnt/c/work/agent-shutdown-test/stopcheck/n8n/feasibility3.py
rc=$?
ip netns del "$NS"
exit $rc
