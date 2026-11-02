#!/bin/bash
# Live demo: stopping a LangGraph agent, and what the stop leaves. Run as root on Linux, from a copy of this
# repository that only root can write (the agent runs code as the test account):
#   bash examples/langgraph/demo.sh           pauses at each step
#   bash examples/langgraph/demo.sh --auto    no pauses
# The first run makes LangGraph's environment in /opt/stopkit-demo/venv, where the test account can run it, with
# the versions the demo was run with (requirements.txt, constraints.txt). That needs the internet once, and
# Python's venv module (on Ubuntu: apt install python3-venv).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
VENV=/opt/stopkit-demo/venv
if [ ! -x "$VENV/bin/langgraph" ]; then
    echo "one-time set-up: making the LangGraph environment in $VENV"
    mkdir -p /opt/stopkit-demo
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt" -c "$HERE/constraints.txt"
    chmod -R a+rX /opt/stopkit-demo
fi
exec "$VENV/bin/python" -B "$HERE/demo.py" "$@"
