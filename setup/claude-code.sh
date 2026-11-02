#!/bin/bash
# Installs what the Claude Code binding needs on a fresh Ubuntu 24.04 machine, as root:
#   bash setup/claude-code.sh [VERSION]        (default 2.1.310, the version of the client probe of 25 Oct 2026)
# Claude Code from npm into /opt/stopkit-claude-code/<version>, with Node.js v24.21.0 (setup/n8n.sh installs the same
# Node; this script installs it if it is missing). Root owns it; the test account can run it but not change it. No
# account, key or model is set up: the binding runs Claude Code against the kit's scripted model stand-in.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
VER=${1:-2.1.310}
NODE_VER=v24.21.0
if [ ! -x "/opt/node-$NODE_VER/bin/node" ]; then
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" | tar -xJ --no-same-owner -C /opt
  mv "/opt/node-$NODE_VER-linux-x64" "/opt/node-$NODE_VER"
fi
export PATH="/opt/node-$NODE_VER/bin:$PATH"
D=/opt/stopkit-claude-code/$VER
if [ ! -e "$D/node_modules/@anthropic-ai/claude-code/package.json" ]; then
  mkdir -p "$D"
  (cd "$D" && npm init -y > /dev/null && npm install --no-audit --no-fund --loglevel=error "@anthropic-ai/claude-code@$VER")
fi
chown -R root:root /opt/stopkit-claude-code
chmod -R a+rX,go-w /opt/stopkit-claude-code
ls -l "$D/node_modules/@anthropic-ai/claude-code/bin/" 2>/dev/null || true
echo "claude-code $(node -p "require('$D/node_modules/@anthropic-ai/claude-code/package.json').version")"
echo "Claude Code binding set-up done"
