#!/bin/bash
# Set up a fresh Ubuntu 24.04 machine to run the LangGraph trials (experiments/langgraph-controls), as root:
#   bash experiments/setup/langgraph-controls.sh
# It installs what the harness needs: iproute2 (ip, ss), util-linux (unshare, setsid), procps (ps), cron (the
# agent adds a line to root's crontab, and two of the harness's controls are crontab lines), tmux and git, and a Python
# environment at /root/pilot-venv, the interpreter the harness expects, with the packages that the 9 October trials ran: langgraph
# 1.2.12, langgraph-api 0.15.1, langgraph-cli 0.4.32, langgraph-runtime-inmem 0.35.1, langgraph-sdk 0.4.5,
# matplotlib 3.11.2, and every other package at its version (experiments/langgraph-controls/code/requirements.txt and
# constraints.txt), checked at the end against the SHA-256 of `pip freeze --all` that the trial records hold. The
# harness runs in place, from experiments/langgraph-controls/code/langgraph: the code of the 9 October trials
# (commit cd26851) with the changes listed in its CHANGES.md.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
CODE=$REPO/experiments/langgraph-controls/code
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
apt-get update -q > /dev/null
apt-get install -y -q iproute2 util-linux procps cron tmux git python3 python3-venv ca-certificates > /dev/null
systemctl enable --now cron > /dev/null 2>&1 || service cron start > /dev/null 2>&1 || true
if [ ! -x /root/pilot-venv/bin/langgraph ]; then
  [ -x /root/pilot-venv/bin/python ] || python3 -m venv /root/pilot-venv
  /root/pilot-venv/bin/pip install -q pip==26.2.1
  /root/pilot-venv/bin/pip install -q -r "$CODE/requirements.txt" -c "$CODE/constraints.txt"
fi
/root/pilot-venv/bin/python - << 'PY'
import hashlib, platform, subprocess, sys
from importlib.metadata import version
pkgs = ("langgraph", "langgraph-api", "langgraph-cli", "langgraph-runtime-inmem", "langgraph-sdk", "matplotlib")
print("Python", platform.python_version(), "with", ", ".join(f"{p} {version(p)}" for p in pkgs))
h = hashlib.sha256(subprocess.run([sys.executable, "-m", "pip", "freeze", "--all"], capture_output=True).stdout)
same = h.hexdigest()[:16] == "721b3c3bc63a8fd9"  # guards.pip_freeze_sha256 in the trials' run_meta.json
print(f"pip freeze --all: sha256 {h.hexdigest()[:16]},", "the same environment as the 9 October trials" if same
      else "not the environment of the 9 October trials (721b3c3bc63a8fd9)")
sys.exit(0 if same else 1)
PY
pgrep -x cron > /dev/null || { echo "cron is not running: start it (service cron start) before a trial"; exit 1; }
echo
echo "Ready. From $CODE/langgraph, one trial, the cancel L1 on the agent version parent_x (about three minutes):"
echo "  bash run_trial_ns.sh --graph parent_x --cond XA-X --levers L1 --trials 1 --outdir results/rerun"
echo "  /root/pilot-venv/bin/python analyze_x.py --res results/rerun"
echo "or all 33 trials, in the order of 9 October (about two hours; run it inside tmux):"
echo "  /root/pilot-venv/bin/python run_expA.py --rounds 1"
echo "See experiments/langgraph-controls/README.md."
