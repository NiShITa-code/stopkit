#!/bin/bash
# Prepares a fresh Ubuntu 24.04 machine for the stop test kit, as root:
#   bash setup/base.sh [INSTALL_DIR]        (default /opt/stopkit)
# It installs the commands the kit uses (at and cron, which it needs running; nftables, which keeps a third-party agent
# off the network during its runs; python3-venv), copies the kit to INSTALL_DIR, owned by root and not writable by
# anyone else (the agent under test runs code as the test account, which must not be able to change the kit), and
# makes the MCP binding's environment: the official Python SDK, mcp 2.3.0, with the exact versions of the MCP cancel
# study (setup/mcp-2.3.0.txt), at /var/lib/stopkit/venv-mcp. Then, from INSTALL_DIR: python3 -B -m stopkit setup.
# The platform bindings each have their own script here (n8n.sh, langgraph.sh, temporal.sh, claude-code.sh,
# openclaw.sh).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
KIT=$(cd "$HERE/.." && pwd)
DEST=${1:-/opt/stopkit}
export DEBIAN_FRONTEND=noninteractive
apt-get update -q > /dev/null
apt-get install -y -q at cron nftables python3 python3-venv util-linux iproute2 procps > /dev/null
systemctl enable --now atd cron > /dev/null 2>&1
if [ "$KIT" = "$DEST" ]; then
  echo "install the kit into a folder other than the repository, such as /opt/stopkit"; exit 1
fi
# The package goes to $DEST/stopkit, so that the kit runs with `cd $DEST && python3 -B -m stopkit ...`.
mkdir -p "$DEST"
cp -a "$KIT/src/stopkit" "$KIT/setup" "$KIT/tests" "$KIT/docs" "$KIT/examples" "$DEST/"
cp "$KIT/README.md" "$DEST/"
chown -R root:root "$DEST"
chmod -R go-w "$DEST"
find "$DEST" -name __pycache__ -prune -exec rm -rf {} +
install -d -m 755 /var/lib/stopkit
if [ ! -x /var/lib/stopkit/venv-mcp/bin/python ]; then
  python3 -m venv /var/lib/stopkit/venv-mcp
fi
/var/lib/stopkit/venv-mcp/bin/pip install -q --no-deps --no-cache-dir -r "$HERE/mcp-2.3.0.txt"
chmod -R a+rX,go-w /var/lib/stopkit/venv-mcp
echo "python $(python3 --version | cut -d' ' -f2), systemd $(systemctl --version | head -1 | cut -d' ' -f2), $(nft --version)"
/var/lib/stopkit/venv-mcp/bin/python -c 'import importlib.metadata as m; print("mcp", m.version("mcp"), "anyio", m.version("anyio"))'
echo "the kit is in $DEST; next: cd $DEST && python3 -B -m stopkit setup"
