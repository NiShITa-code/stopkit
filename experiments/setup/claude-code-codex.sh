#!/bin/bash
# Set up a fresh Ubuntu 24.04 machine to run the client probe (experiments/claude-code-codex), as root:
#   bash experiments/setup/claude-code-codex.sh
# It installs what the probe's code expects at the paths it expects, without changing any of its files: Node.js 24.22.1
# at /root/.local/node-v24.22.1 (checked against its published SHA-256), and Claude Code 2.1.308 and 2.1.310 and the Codex
# CLI 0.164.0 and 0.165.0-alpha.1, each in its own folder under /root/mcp-clients, installed by npm from the lock files the
# study used (experiments/claude-code-codex/clients/; npm checks every package against the lock file's hash). It then
# checks each client's executable against the SHA-256 that the 25 October batch recorded. It also installs tmux and makes
# an empty folder /mnt/c, because the harness reads the free disk space there (under WSL, where the probe first ran, that
# is the Windows drive). The harness, the test MCP server (probe_server.py) and the stand-in model (stub_model.py) need
# only Ubuntu's own Python 3 and its standard library.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
EXP="$REPO/experiments/claude-code-codex"
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -q > /dev/null
apt-get install -y -q tmux python3 curl ca-certificates xz-utils > /dev/null
NODE_VERSION=24.22.1
NODE_SHA256=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6
NODE=/root/.local/node-v$NODE_VERSION
if [ ! -x "$NODE/bin/node" ]; then
  T=$(mktemp -d)
  curl -fsSL -o "$T/node.tar.xz" "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz"
  echo "$NODE_SHA256  $T/node.tar.xz" | sha256sum -c -
  mkdir -p "$NODE" && tar -xJf "$T/node.tar.xz" -C "$NODE" --strip-components=1 --no-same-owner && rm -rf "$T"
fi
"$NODE/bin/node" --version
# The folder names are the ones the harness looks for (run_probe.py, CLIENTS).
for pair in claude-code-2.1.308:_anthropic-ai_claude-code_2.1.308 claude-code-2.1.310:_anthropic-ai_claude-code_2.1.310 \
            codex-0.164.0:_openai_codex_0.164.0 codex-0.165.0-alpha.1:_openai_codex_0.165.0-alpha.1; do
  src="$EXP/clients/${pair%%:*}" dir="/root/mcp-clients/${pair#*:}"
  if [ -d "$dir/node_modules" ]; then
    echo "$dir already exists: left as it is"
  else
    mkdir -p "$dir" && cp "$src/package.json" "$src/package-lock.json" "$dir/"
    (cd "$dir" && PATH="$NODE/bin:$PATH" npm ci --no-audit --no-fund --no-update-notifier --loglevel=error)
  fi
done
sha256sum -c - << 'EOF'
356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58  /root/mcp-clients/_anthropic-ai_claude-code_2.1.308/node_modules/@anthropic-ai/claude-code/bin/claude.exe
b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56  /root/mcp-clients/_anthropic-ai_claude-code_2.1.310/node_modules/@anthropic-ai/claude-code/bin/claude.exe
b93f6811d3f653f0fb83be4050d857fb9abbb111dfa2ce160563327daff7f306  /root/mcp-clients/_openai_codex_0.164.0/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
bddea38d3a643c13c415b03f83bf5162d1cc27a54c6b03a3e8d9e7f8d707de5e  /root/mcp-clients/_openai_codex_0.165.0-alpha.1/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
EOF
mkdir -p /mnt/c
echo
echo "Ready. One run of each client (about a minute each), as root, from $EXP:"
echo "  python3 run_probe.py smoke claude-2.1.310 esc"
echo "  python3 run_probe.py smoke codex-0.165.0-alpha.1 esc"
echo "See experiments/claude-code-codex/README.md for the full batch of 58 runs."
