#!/bin/bash
# Installs what the OpenClaw binding needs on a fresh Ubuntu 24.04 machine, as root:
#   bash setup/openclaw.sh
# OpenClaw 2026.10.2 (commit b8952d4) from npm into /opt/stopkit-openclaw/2026.10.2, after checking that the registry's
# tarball and its recorded integrity match the pinned sha512 (npm checks the tarball it installs against that same
# value); Node.js v24.21.0 if it is missing. Adapted from the OpenClaw spike's set-up
# (checking-agent-shutdowns/setup/openclaw-spike.sh, the runs of 14 Oct 2026). Root owns it; the test account can run
# it but not change it. OpenClaw runs shell commands for its model; the binding runs it against the kit's scripted
# model stand-in, with the agent account kept off the network.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
NODE=/opt/node-v24.21.0
OC_VERSION=2026.10.2
OC_TARBALL=https://registry.npmjs.org/openclaw/-/openclaw-2026.10.2.tgz
OC_INTEGRITY='sha512-FuLLF+WI12ypwQ0r+pcKDcmB+kXus5N4QClaKEnaAn6GN4I1AitkOhwv0GFl2rVKpvuUBDQWIfglHNy6Trv/EQ=='
D=/opt/stopkit-openclaw/$OC_VERSION        # a global install into its own prefix, as the spike installed it
fail() { echo "SETUP FAILED: $*" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q curl ca-certificates xz-utils openssl > /dev/null
if [ ! -x "$NODE/bin/node" ]; then
  curl -fsSL "https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz" | tar -xJ --no-same-owner -C /opt
  mv /opt/node-v24.21.0-linux-x64 "$NODE"
fi
export PATH="$NODE/bin:$PATH" npm_config_update_notifier=false
if [ ! -x "$D/bin/openclaw" ]; then
  [ "$(npm view "openclaw@$OC_VERSION" dist.integrity)" = "$OC_INTEGRITY" ] || fail "the registry's integrity is not the pinned sha512"
  [ "$(npm view "openclaw@$OC_VERSION" dist.tarball)" = "$OC_TARBALL" ] || fail "the registry's tarball is not $OC_TARBALL"
  mkdir -p "$D"
  npm install -g --prefix "$D" --no-audit --no-fund --loglevel=error "openclaw@$OC_VERSION" --allow-scripts=openclaw
fi
chown -R root:root /opt/stopkit-openclaw
chmod -R a+rX,go-w /opt/stopkit-openclaw
V=$("$D/bin/openclaw" --version)
[ "$V" = "OpenClaw $OC_VERSION (b8952d4)" ] || fail "openclaw --version printed '$V'"
echo "$V, at $(readlink -f "$D/bin/openclaw")"
echo "OpenClaw binding set-up done"
