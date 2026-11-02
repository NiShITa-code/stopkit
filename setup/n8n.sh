#!/bin/bash
# Installs what the n8n binding needs on a fresh Ubuntu 24.04 machine, as root:
#   bash setup/n8n.sh [N8N_VERSION]        (default 2.41.7)
# Postgres 17 from the PostgreSQL project's apt repository, Ubuntu's Redis, Node.js v24.21.0 and n8n from npm into
# /opt/n8n-<version>. The packaged Postgres and Redis services are stopped and disabled: the binding runs private
# instances of its own on 127.0.0.1 (ports 5434 and 6381). Adapted from the n8n finding-1 reproduction's provision.sh
# (checking-agent-shutdowns/experiments/n8n-finding1), which built the fresh machine of 21 Oct 2026; archives are
# unpacked with root as their owner, so no account the kit creates can own a program root runs.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
N8N_VER=${1:-2.41.7}
NODE_VER=v24.21.0
export DEBIAN_FRONTEND=noninteractive
apt-get update -q > /dev/null
apt-get install -y -q curl ca-certificates gnupg postgresql-common redis-server python3 build-essential > /dev/null
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y > /dev/null
apt-get install -y -q postgresql-17 > /dev/null
systemctl disable --now postgresql redis-server > /dev/null 2>&1 || true
if [ ! -x "/opt/node-$NODE_VER/bin/node" ]; then
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" | tar -xJ --no-same-owner -C /opt
  mv "/opt/node-$NODE_VER-linux-x64" "/opt/node-$NODE_VER"
fi
export PATH="/opt/node-$NODE_VER/bin:$PATH"
if [ ! -x "/opt/n8n-$N8N_VER/node_modules/n8n/bin/n8n" ]; then
  mkdir -p "/opt/n8n-$N8N_VER"
  (cd "/opt/n8n-$N8N_VER" && npm init -y > /dev/null && npm install --no-audit --no-fund --loglevel=error "n8n@$N8N_VER")
fi
chown -R root:root "/opt/node-$NODE_VER" "/opt/n8n-$N8N_VER"
chmod -R go-w "/opt/node-$NODE_VER" "/opt/n8n-$N8N_VER"
echo "node $(node --version)"
/usr/lib/postgresql/17/bin/postgres --version
redis-server --version
echo "n8n $(node -p "require('/opt/n8n-$N8N_VER/node_modules/n8n/package.json').version")"
echo "n8n binding set-up done"
