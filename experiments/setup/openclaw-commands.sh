#!/bin/bash
# Set up a fresh Ubuntu 24.04 machine for the unscored OpenClaw runs (experiments/openclaw-commands), as root:
#   bash experiments/setup/openclaw-commands.sh
# It installs what the frozen runner (experiments/harness/openclaw-b8fd1e4/openclaw) expects, at the paths of the runs of 14 Oct
# 2026, without changing any frozen file: tmux and iptables; Node.js 24.21.0 at /opt/node-v24.21.0, checked against
# the SHA-256 that nodejs.org publishes; the user ocspike, with no password, no SSH key and no sudo; OpenClaw 2026.10.2
# installed from npm as that user under /home/ocspike/.npm-global, with
#   npm install -g openclaw@2026.10.2 --allow-scripts=openclaw
# after checking that the registry's tarball and its recorded integrity match the pinned sha512 (npm checks the
# tarball it installs against that same recorded value); the runner at /opt/stopcheck-openclaw/openclaw; and the run
# folder /var/lib/stopcheck-openclaw/runs, which only ocspike can write. OpenClaw runs shell commands for the model, so
# use a machine you can throw away. Everything is downloaded here, before outbound traffic is closed; the run steps
# are in experiments/openclaw-commands/README.md.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
source "$REPO/setup/common.sh"
prepare_machine
NODE=/opt/node-v24.21.0
NODE_SHA256=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
OC_VERSION=2026.10.2
OC_TARBALL=https://registry.npmjs.org/openclaw/-/openclaw-2026.10.2.tgz
OC_INTEGRITY='sha512-FuLLF+WI12ypwQ0r+pcKDcmB+kXus5N4QClaKEnaAn6GN4I1AitkOhwv0GFl2rVKpvuUBDQWIfglHNy6Trv/EQ=='
OC_SHASUM=27956be5ad49487209be231788fe6b89835185c9
PREFIX=/home/ocspike/.npm-global
RUNNER=/opt/stopcheck-openclaw/openclaw
fail() { echo "SETUP FAILED: $*" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=600 update -q > /dev/null
apt-get -o DPkg::Lock::Timeout=600 install -y -q tmux iptables python3 curl ca-certificates xz-utils openssl \
  util-linux procps > /dev/null

if [ ! -x "$NODE/bin/node" ]; then
  T=$(mktemp -d)
  curl -fsSL -o "$T/node.tar.xz" https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz
  echo "$NODE_SHA256  $T/node.tar.xz" | sha256sum -c -
  mkdir -p "$NODE" && tar -xJf "$T/node.tar.xz" -C "$NODE" --strip-components=1 --no-same-owner && rm -rf "$T"
fi
echo "node $("$NODE/bin/node" -v), npm $("$NODE/bin/node" "$NODE/bin/npm" -v)"

# The runner's user. useradd with no -G, so it is in no sudo-granting group; no password and no SSH key. On Google
# Cloud, never add an SSH key for ocspike to the metadata: the guest agent would put it in google-sudoers.
id ocspike > /dev/null 2>&1 || useradd --create-home --shell /bin/bash ocspike
if id -nG ocspike | tr ' ' '\n' | grep -qxE 'sudo|admin|wheel|google-sudoers'; then
  fail "ocspike is in a sudo group: $(id ocspike)"
fi
sudo -l -U ocspike 2>&1 | grep -q 'not allowed to run sudo' || fail "ocspike may use sudo: $(sudo -l -U ocspike 2>&1)"
echo "$(id ocspike); $(sudo -l -U ocspike 2>&1)"

as_ocspike() {
  runuser -u ocspike -- env -i -C /home/ocspike HOME=/home/ocspike PATH="$NODE/bin:$PREFIX/bin:/usr/bin:/bin" \
    LANG=C.UTF-8 npm_config_update_notifier=false "$@"
}
if [ ! -x "$PREFIX/bin/openclaw" ]; then
  as_ocspike npm config set prefix "$PREFIX"
  [ "$(as_ocspike npm view "openclaw@$OC_VERSION" dist.integrity)" = "$OC_INTEGRITY" ] \
    || fail "the registry's integrity for openclaw@$OC_VERSION is not the pinned sha512"
  [ "$(as_ocspike npm view "openclaw@$OC_VERSION" dist.tarball)" = "$OC_TARBALL" ] \
    || fail "the registry's tarball for openclaw@$OC_VERSION is not $OC_TARBALL"
  T=$(mktemp -d)
  curl -fsSL -o "$T/openclaw.tgz" "$OC_TARBALL"
  [ "sha512-$(openssl dgst -sha512 -binary "$T/openclaw.tgz" | base64 -w0)" = "$OC_INTEGRITY" ] \
    || fail "the downloaded $OC_TARBALL does not match the pinned sha512"
  echo "$OC_SHASUM  $T/openclaw.tgz" | sha1sum -c - > /dev/null || fail "the downloaded tarball's sha1 is not $OC_SHASUM"
  echo "openclaw-$OC_VERSION.tgz: $(stat -c %s "$T/openclaw.tgz") bytes, sha512 and sha1 match the pins"
  rm -rf "$T"
  as_ocspike npm install -g "openclaw@$OC_VERSION" --allow-scripts=openclaw
fi
V=$(as_ocspike "$PREFIX/bin/openclaw" --version)
[ "$V" = "OpenClaw $OC_VERSION (b8952d4)" ] || fail "openclaw --version printed '$V'"
echo "$V, installed at $(readlink -f "$PREFIX/bin/openclaw")"

# The runner, read-only for ocspike, at the path of the runs of 14 Oct 2026.
install -d -m 0755 "$RUNNER"
for f in SPIKE-F.md egress_f.py procfs_f.py spike_f.py stub_completions.py; do
  install -m 0644 "$REPO/harness/openclaw-b8fd1e4/openclaw/$f" "$RUNNER/$f"
done
chmod 0755 "$RUNNER/egress_f.py" "$RUNNER/spike_f.py" "$RUNNER/stub_completions.py"
(cd "$RUNNER" && sha256sum SPIKE-F.md egress_f.py procfs_f.py spike_f.py stub_completions.py)
install -d -m 0755 /var/lib/stopcheck-openclaw
install -d -o ocspike -g ocspike -m 0700 /var/lib/stopcheck-openclaw/runs

echo
echo "Ready. Next, as in experiments/openclaw-commands/README.md: close outbound traffic with"
echo "  python3 -I $RUNNER/egress_f.py close --user ocspike --out-dir /var/lib/stopcheck-openclaw/egress"
echo "check that a fresh SSH login still works, then start the run as ocspike inside tmux."
