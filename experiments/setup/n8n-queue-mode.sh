#!/bin/bash
# Set up a fresh Ubuntu 24.04 machine to run the n8n harness (experiments/n8n-queue-mode), as root:
#   bash experiments/setup/n8n-queue-mode.sh
# It installs what the frozen harness expects at the paths it expects, without changing any frozen file:
# Postgres 17, Redis, Node.js 24.21.0, n8n 2.41.7 and the user n8ntest (experiments/n8n-restart/provision.sh, the
# script that built the fresh machine of 21 Oct 2026), tmux, cron and iproute2, a Python environment at
# /root/pilot-venv (the harness needs only Python's standard library), and the harness itself at /root/stopcheck: a
# git repository holding experiments/harness/n8n-langgraph-7a3fcec, the code at the frozen commit 7a3fcec, on top of a commit that
# holds the scorer as it was at commit 3832d7c (experiments/harness/n8n-scorer-3832d7c), which the runner's mutation check reads.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
source "$REPO/setup/common.sh"
prepare_machine
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=600 update -q > /dev/null
apt-get -o DPkg::Lock::Timeout=600 install -y -q tmux cron iproute2 util-linux python3 python3-venv curl ca-certificates git openssl > /dev/null
systemctl enable --now cron > /dev/null 2>&1 || true
# n8n 2.41.7 with exactly the dependency versions the trials ran (experiments/setup/n8n-2.41.7/package-lock.json, whose SHA-256 the
# frozen harness checks before every trial). A plain `npm install n8n@2.41.7` would pick newer versions of n8n's own
# dependencies.
NODE_VER=v24.21.0
if [ ! -x "/opt/node-$NODE_VER/bin/node" ]; then
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" | tar -xJ -C /opt
  mv "/opt/node-$NODE_VER-linux-x64" "/opt/node-$NODE_VER"
fi
if [ ! -x /opt/n8n-2.41.7/node_modules/n8n/bin/n8n ]; then
  mkdir -p /opt/n8n-2.41.7
  cp "$REPO/setup/n8n-2.41.7/package.json" "$REPO/setup/n8n-2.41.7/package-lock.json" /opt/n8n-2.41.7/
  ( cd /opt/n8n-2.41.7 && PATH="/opt/node-$NODE_VER/bin:$PATH" npm ci --no-audit --no-fund --loglevel=error )
fi
# Postgres 17, Redis and the user n8ntest (the Node.js and n8n steps of provision.sh find them already installed)
bash "$REPO/experiments/n8n-restart/provision.sh" 2.41.7
[ -x /root/pilot-venv/bin/python ] || python3 -m venv /root/pilot-venv
if [ -e /root/stopcheck ]; then
  echo "/root/stopcheck already exists: left as it is"
else
  W=/root/stopcheck
  git init -q -b main "$W"
  # The rule of the repository the trials ran from: every file is text with Unix line endings.
  printf '* text eol=lf\n' > "$W/.gitattributes"
  # The first commit holds the scorer as it was at commit 3832d7c and is tagged with that name, so that the runner's
  # mutation check finds it with `git show 3832d7c:n8n/analyze_b.py`; the second holds the code at 7a3fcec.
  install -D -m 0644 "$REPO/harness/n8n-scorer-3832d7c/n8n/analyze_b.py" "$W/n8n/analyze_b.py"
  git -C "$W" add -A
  git -C "$W" -c user.name=setup -c user.email=setup@stopcheck.test commit -q -m "the scorer at commit 3832d7c"
  git -C "$W" tag 3832d7c
  cp -a "$REPO/harness/n8n-langgraph-7a3fcec/." "$W/"
  git -C "$W" add -A
  git -C "$W" -c user.name=setup -c user.email=setup@stopcheck.test commit -q -m "the harness at commit 7a3fcec"
  # The MANIFEST.sha256 of the LangGraph trials of 9 October was written on a Windows copy, where two derived summaries had Windows line
  # endings, and the runner's pre-trial check (reproduce.py --quick) compares them byte for byte. Give them back the
  # bytes the manifest records (only their line endings change).
  python3 - /root/stopcheck/langgraph/results/expA-round1 << 'PY'
import hashlib, os, sys
d = sys.argv[1]
man = dict(reversed(l.split()) for l in open(os.path.join(d, 'MANIFEST.sha256')) if l.strip())
for n in ('naive_checks.md', 'naive_checks.json'):
    p = os.path.join(d, n); b = open(p, 'rb').read()
    if hashlib.sha256(b).hexdigest() != man[n]:
        c = b.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        assert hashlib.sha256(c).hexdigest() == man[n], n
        open(p, 'wb').write(c)
print('LangGraph round-1 manifest: line endings restored')
PY
  # A throwaway owner login and encryption key for the disposable n8n that each trial creates (git-ignored; never leaves
  # the machine). Queue mode needs one encryption key shared by main and worker.
  ( umask 077; printf 'N8N_TEST_OWNER_EMAIL=owner@stopcheck.test\nN8N_TEST_OWNER_PASSWORD=Sc1%s\nN8N_ENCRYPTION_KEY=%s\n' \
      "$(openssl rand -hex 12)" "$(openssl rand -hex 24)" > /root/stopcheck/n8n/n8n-test.env )
  # The frozen runner (--freeze) requires a clean, committed tree. Git stores these two files with Unix line endings, so
  # once git has re-read them their content matches the commit and the tree reads clean.
  git -C /root/stopcheck add langgraph/results/expA-round1/naive_checks.md langgraph/results/expA-round1/naive_checks.json 2> /dev/null
  if git -C /root/stopcheck status --porcelain | grep -q .; then echo "unexpected changes in /root/stopcheck"; exit 1; fi
fi
fix_clock
echo
echo "Ready. A practice trial (stops B0, PC, B1 or B1r), from /root/stopcheck:"
echo "  STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --pilot --conditions B1"
echo "On WSL leave out STOPCHECK_HOST_PROFILE=linux-vm. See experiments/n8n-queue-mode/README.md for the full batch."
