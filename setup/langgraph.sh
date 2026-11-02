#!/bin/bash
# Installs what the LangGraph binding needs on a fresh Ubuntu 24.04 machine, as root:
#   bash setup/langgraph.sh
# A Python 3.12 environment at /opt/stopkit-langgraph/venv with LangGraph 1.2.12 and its development server
# (langgraph-cli 0.4.32, langgraph-api 0.15.1, the in-memory runtime 0.35.1): the versions of the earlier experiments
# and of the demo, every other package pinned by setup/langgraph-constraints.txt. Root owns it; the test account can run
# it but not change it. langgraph-api is under the Elastic License 2.0: whether results about it may be published is the
# user's decision (FRAMEWORK.md section 5).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
VENV=/opt/stopkit-langgraph/venv
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q python3-venv > /dev/null
if [ ! -x "$VENV/bin/langgraph" ]; then
  mkdir -p /opt/stopkit-langgraph
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --no-cache-dir -r "$HERE/langgraph-requirements.txt" -c "$HERE/langgraph-constraints.txt"
fi
chown -R root:root /opt/stopkit-langgraph
chmod -R a+rX,go-w /opt/stopkit-langgraph
"$VENV/bin/python" -c 'import importlib.metadata as m; print(" ".join(f"{p} {m.version(p)}" for p in ("langgraph", "langgraph-api", "langgraph-cli", "langgraph-runtime-inmem", "langgraph-sdk")))'
echo "LangGraph binding set-up done"
