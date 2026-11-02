#!/bin/bash
# Set up a fresh Ubuntu 24.04 machine to run the Temporal harness (experiments/temporal-activities), as root:
#   bash experiments/setup/temporal-activities.sh
# It installs what the frozen harness expects at the paths it expects, without changing any frozen file: the Temporal
# CLI 1.9.1 (server 1.32.0) at /opt/temporal-cli-1.9.1, a Python environment at /root/temporal-venv with temporalio
# 1.34.0 and the exact versions of its dependencies that ran, tmux, and the harness itself at /root/stopcheck-temporal:
# a git repository holding experiments/harness/temporal-627df0b, the code at the frozen commit 627df0b.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
source "$REPO/setup/common.sh"
prepare_machine
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=600 update -q > /dev/null
apt-get -o DPkg::Lock::Timeout=600 install -y -q tmux iproute2 util-linux python3 python3-venv curl ca-certificates git > /dev/null
if [ ! -x /opt/temporal-cli-1.9.1/temporal ]; then
  T=$(mktemp -d)
  curl -fsSL -o "$T/cli.tgz" https://github.com/temporalio/cli/releases/download/v1.9.1/temporal_cli_1.9.1_linux_amd64.tar.gz
  echo "09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5  $T/cli.tgz" | sha256sum -c -
  mkdir -p /opt/temporal-cli-1.9.1 && tar -xzf "$T/cli.tgz" -C /opt/temporal-cli-1.9.1 temporal && rm -rf "$T"
fi
/opt/temporal-cli-1.9.1/temporal --version
if [ ! -x /root/temporal-venv/bin/python ]; then
  python3 -m venv /root/temporal-venv
  /root/temporal-venv/bin/pip install -q temporalio==1.34.0 -c "$REPO/setup/constraints-temporal.txt"
fi
if [ -e /root/stopcheck-temporal ]; then
  echo "/root/stopcheck-temporal already exists: left as it is"
else
  W=/root/stopcheck-temporal
  git init -q -b main "$W"
  # The rule of the repository the trials ran from: every file is text with Unix line endings.
  printf '* text eol=lf\n' > "$W/.gitattributes"
  cp -a "$REPO/harness/temporal-627df0b/." "$W/"
  git -C "$W" add -A
  git -C "$W" -c user.name=setup -c user.email=setup@stopcheck.test commit -q -m "the harness at commit 627df0b"
fi
fix_clock
echo
echo "Ready. A practice trial, from /root/stopcheck-temporal:"
echo "  STOPCHECK_HOST_PROFILE=linux-vm python3 -B temporal/run_expC.py --pilot --conditions C3"
echo "On WSL leave out STOPCHECK_HOST_PROFILE=linux-vm. See experiments/temporal-activities/README.md for the full batch."
