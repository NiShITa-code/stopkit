#!/bin/bash
# Installs what the Temporal binding needs on a fresh Ubuntu 24.04 machine (x86_64), as root:
#   bash setup/temporal.sh
# The Temporal CLI 1.9.1, whose development server is Temporal Server 1.32.0, from its release archive checked against
# its SHA-256, at /opt/stopkit-temporal/bin/temporal; and a Python environment at /opt/stopkit-temporal/venv with
# temporalio 1.34.0, every other package pinned by setup/temporal-constraints.txt: the versions of the Temporal
# experiment C (14 Oct 2026). Release archives keep their builders' user IDs, so the archive is unpacked with root as
# the owner. Root owns everything: the test account can run it but not change it.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "the pinned CLI archive is for x86_64 (linux_amd64)"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
DEST=/opt/stopkit-temporal
CLI_VER=1.9.1
CLI_SHA256=09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5
export DEBIAN_FRONTEND=noninteractive
apt-get update -q > /dev/null
apt-get install -y -q curl ca-certificates python3-venv > /dev/null
mkdir -p "$DEST/bin"
if [ ! -x "$DEST/bin/temporal" ]; then
  T=$(mktemp -d)
  curl -fsSL -o "$T/cli.tgz" "https://github.com/temporalio/cli/releases/download/v$CLI_VER/temporal_cli_${CLI_VER}_linux_amd64.tar.gz"
  echo "$CLI_SHA256  $T/cli.tgz" | sha256sum -c - > /dev/null
  tar -xzf "$T/cli.tgz" --no-same-owner -C "$T" temporal
  install -m 755 -o root -g root "$T/temporal" "$DEST/bin/temporal"
  rm -rf "$T"
fi
if [ ! -x "$DEST/venv/bin/python3" ]; then
  python3 -m venv "$DEST/venv"
  "$DEST/venv/bin/pip" install -q --no-cache-dir temporalio==1.34.0 -c "$HERE/temporal-constraints.txt"
fi
chown -R root:root "$DEST"
chmod -R a+rX,go-w "$DEST"
"$DEST/bin/temporal" --version
"$DEST/venv/bin/python3" -c 'import importlib.metadata as m, sys; v = m.version("temporalio"); print("temporalio", v, "protobuf", m.version("protobuf")); sys.exit(v != "1.34.0")'
echo "Temporal binding set-up done"
