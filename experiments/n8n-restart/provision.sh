#!/bin/bash
# Installs what the B.11 reproductions need on a fresh Ubuntu 24.04 VM. Written 21 Oct 2026: the 13-14 Oct VMs were
# built from an image that has since been deleted. Uses no StopCheck code.
# Usage, as root: bash provision.sh 2.41.7 [2.44.5 ...]   (one or more n8n versions)
# Installs: Postgres 17 from the PostgreSQL project's apt repository, Ubuntu's Redis, Node v24.21.0 (the version the
# 14 Oct run used), each requested n8n version from npm into /opt/n8n-<version>, and the unprivileged user n8ntest.
# The packaged Postgres and Redis services are stopped: setup.sh starts private instances of its own.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: provision.sh N8N_VERSION [N8N_VERSION ...]"; exit 1; }
NODE_VER=v24.21.0
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get install -y -q curl ca-certificates gnupg postgresql-common redis-server python3 build-essential > /dev/null
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y > /dev/null
apt-get install -y -q postgresql-17 > /dev/null
systemctl disable --now postgresql redis-server > /dev/null 2>&1 || true
id n8ntest > /dev/null 2>&1 || useradd -m -s /bin/bash n8ntest

if [ ! -x "/opt/node-$NODE_VER/bin/node" ]; then
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" | tar -xJ -C /opt
  mv "/opt/node-$NODE_VER-linux-x64" "/opt/node-$NODE_VER"
fi
export PATH="/opt/node-$NODE_VER/bin:$PATH"

for v in "$@"; do
  [ -x "/opt/n8n-$v/node_modules/n8n/bin/n8n" ] && continue
  mkdir -p "/opt/n8n-$v"
  (cd "/opt/n8n-$v" && npm init -y > /dev/null && npm install --no-audit --no-fund --loglevel=error "n8n@$v")
done

echo "node $(node --version)"
/usr/lib/postgresql/17/bin/postgres --version
redis-server --version
for v in "$@"; do echo "n8n $(node -p "require('/opt/n8n-$v/node_modules/n8n/package.json').version")"; done
echo "provision done"
