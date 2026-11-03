#!/bin/bash
# Installs what the Codex config file (examples/codex/agent.toml) needs on a fresh Ubuntu 24.04 machine, as root:
#   bash setup/codex.sh
# The Codex CLI 0.164.0 for Linux on x86-64 into /opt/stopkit-codex/0.164.0: npm's package for that platform,
# @openai/codex@0.164.0-linux-x64, which holds Codex's own executable, downloaded from the npm registry and checked
# against the pinned sha512 (the integrity that the registry and the client probe's lock file record,
# experiments/claude-code-codex/clients/codex-0.164.0/package-lock.json at commit 19544dd); then the executable is
# checked against the SHA-256 that the client probe recorded on 25 October 2026. The kit runs that executable, as the client probe did, so
# npm's launcher (a Node.js script that starts it) and Node.js are not needed. Root owns it; the test account can run
# it but not change it. Codex runs shell commands for its model; the kit runs it against its scripted model stand-in,
# with the agent account kept off the network.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
VERSION=0.164.0
TARBALL=https://registry.npmjs.org/@openai/codex/-/codex-$VERSION-linux-x64.tgz
INTEGRITY='sha512-XLAoaWsGOT2khzl+P4FYDPTkzAr9W0VFXFaVkqlFdkM0soa1gH/4S0wSD57Ek3HHwkirAEGz2/Mwehseh+l5Mg=='
CODEX_SHA256=b93f6811d3f653f0fb83be4050d857fb9abbb111dfa2ce160563327daff7f306
D=/opt/stopkit-codex/$VERSION
CODEX=$D/vendor/x86_64-unknown-linux-musl/bin/codex
fail() { echo "SETUP FAILED: $*" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q curl ca-certificates openssl > /dev/null
if [ ! -x "$CODEX" ]; then
  T=$(mktemp -d)
  curl -fsSL -o "$T/codex.tgz" "$TARBALL"
  [ "sha512-$(openssl dgst -sha512 -binary "$T/codex.tgz" | openssl base64 -A)" = "$INTEGRITY" ] || fail "the download's sha512 is not the pinned one"
  mkdir -p "$D"
  tar -xzf "$T/codex.tgz" -C "$D" --strip-components=1 --no-same-owner
  rm -rf "$T"
fi
echo "$CODEX_SHA256  $CODEX" | sha256sum -c --quiet - || fail "$CODEX is not the executable the client probe recorded"
chown -R root:root /opt/stopkit-codex
chmod -R a+rX,go-w /opt/stopkit-codex
V=$("$CODEX" --version)
[ "$V" = "codex-cli $VERSION" ] || fail "codex --version printed '$V'"
echo "$V, at $CODEX"
echo "Codex set-up done"
