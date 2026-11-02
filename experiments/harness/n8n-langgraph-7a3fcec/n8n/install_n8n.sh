#!/bin/bash
# Install a pinned, isolated n8n test set-up in WSL (Ubuntu 24.04, run as root). No Docker.
#   Postgres 16 and Valkey 7.2 (Ubuntu now ships Valkey, the Redis-compatible fork, in place of
#   redis-server; n8n's queue mode needs only the Redis protocol) from Ubuntu's repositories, not started at boot (start them only
#   for n8n batches, so they cannot disturb other trials).
#   Node.js v24.21.0 (LTS "Krypton", 7 Sep 2026) from nodejs.org, checksum-verified, in its own folder.
#   n8n 2.41.7 (5 Oct 2026; needs Node >= 24) in its own prefix, installed with that Node's npm,
#   never the Windows npm that WSL interop puts on the PATH.
# Approved by the user on 9 Oct 2026 (downloads of Linux Node.js, n8n, Postgres and Redis).
set -euo pipefail
NODE_V=v24.21.0
N8N_V=2.41.7
NODE_DIR=/opt/node-$NODE_V
N8N_DIR=/opt/n8n-$N8N_V
LOG=/root/n8n-install.log
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date -Is) install start"

free_gb=$(df -BG --output=avail /mnt/c | tail -1 | tr -dc 0-9)
if [ "$free_gb" -lt 12 ]; then echo "C: has only ${free_gb} GB free; stopping"; exit 3; fi

echo "--- 1. Postgres and Valkey (Ubuntu repositories)"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q postgresql=16+257build1.1 valkey-server=7.2.13+dfsg1-0ubuntu0.1
systemctl disable --now postgresql valkey-server || true

echo "--- 2. Node.js $NODE_V (nodejs.org, checksum verified)"
mkdir -p /opt/dl/node-$NODE_V && cd /opt/dl/node-$NODE_V
curl -fsSLO "https://nodejs.org/dist/$NODE_V/node-$NODE_V-linux-x64.tar.xz"
curl -fsSLO "https://nodejs.org/dist/$NODE_V/SHASUMS256.txt"
grep " node-$NODE_V-linux-x64.tar.xz\$" SHASUMS256.txt | sha256sum -c -
mkdir -p "$NODE_DIR"
tar -xJf "node-$NODE_V-linux-x64.tar.xz" -C "$NODE_DIR" --strip-components=1

echo "--- 3. n8n $N8N_V (own prefix, Linux npm only)"
export PATH="$NODE_DIR/bin:/usr/sbin:/usr/bin:/sbin:/bin"
hash -r
echo "node: $(command -v node) $(node --version); npm: $(command -v npm) $(npm --version)"
mkdir -p "$N8N_DIR" && cd "$N8N_DIR"
[ -f package.json ] || npm init -y >/dev/null
npm install --no-fund --no-audit --save-exact "n8n@$N8N_V"
"$N8N_DIR/node_modules/.bin/n8n" --version
npm ls --all --json > "$N8N_DIR/npm-ls-all.json" || true
sha256sum package-lock.json > package-lock.sha256

echo "--- versions"
psql --version; valkey-server --version; node --version
echo "=== $(date -Is) install done"
