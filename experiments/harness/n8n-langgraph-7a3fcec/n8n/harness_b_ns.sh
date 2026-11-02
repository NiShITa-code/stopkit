#!/bin/bash
# Launch ONE disposable, unscored trial, or a trial authorized by run_expB.py.
# Never delete an existing namespace. A shared lock prevents n8ntest cleanup
# from interfering with a concurrent StopCheck run.
set -uo pipefail
COND="${1:?condition required (e.g. B0)}"
TRIAL="${2:?trial id required (e.g. XB-B0-t1)}"
NS="${3:-stopcheck-b-$$}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# STOPCHECK_PY (documented, and used by the harness for its helpers) also selects the harness interpreter.
# STOPCHECK_PYTHON may name it instead, but must be the same interpreter: the harness refuses any other.
PYTHON_BIN="${STOPCHECK_PYTHON:-${STOPCHECK_PY:-/root/pilot-venv/bin/python}}"
LOCK_FILE="${STOPCHECK_LOCK_FILE:-/run/stopcheck-n8n.lock}"
if [ "$(id -u)" -ne 0 ]; then
    printf 'Run this launcher as root in the disposable Ubuntu test environment.\n' >&2
    exit 2
fi
# The trial id rule is harness_b.main's: 1-64 letters, digits or hyphens, starting with a letter or digit.
if [[ ! "$COND" =~ ^[A-Za-z0-9_-]+$ ]] || [[ ! "$TRIAL" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,63}$ ]] ||
   [[ ! "$NS" =~ ^[A-Za-z0-9_.-]{1,63}$ ]]; then
    printf 'Invalid condition, trial id, or namespace name.\n' >&2
    exit 2
fi
if [ ! -x "$PYTHON_BIN" ]; then
    printf 'Python executable unavailable: %s\n' "$PYTHON_BIN" >&2
    exit 2
fi
exec 9>"$LOCK_FILE" || exit 2
if ! flock -n 9; then
    printf 'Another StopCheck n8n trial owns the lock; do not run concurrently.\n' >&2
    exit 2
fi
existing="$(ip netns list)" || exit 2
while read -r name rest; do
    if [ "$name" = "$NS" ]; then
        printf 'Namespace already exists: %s. It was not modified.\n' "$NS" >&2
        exit 2
    fi
done <<< "$existing"
created=0
child_pid=""
cleanup() {
    status=$?
    trap - EXIT INT TERM
    if [ "$created" -eq 1 ]; then
        if ! ip netns del "$NS"; then
            printf 'Failed to remove owned namespace: %s\n' "$NS" >&2
            if [ "$status" -eq 0 ]; then status=2; fi
        fi
    fi
    exit "$status"
}
interrupted() {
    status="$2"
    if [ -n "$child_pid" ]; then
        # A background child of a non-interactive shell starts with SIGINT ignored, so forwarding INT does
        # nothing. Send SIGTERM for INT and TERM alike; the harness turns it into its interrupt cleanup.
        kill -s TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'interrupted INT 130' INT
trap 'interrupted TERM 143' TERM
if ! ip netns add "$NS"; then exit 2; fi
created=1
if ! ip netns exec "$NS" ip link set lo up; then exit 2; fi
STOPCHECK_NETNS="$NS" ip netns exec "$NS" "$PYTHON_BIN" -B -u "$HERE/harness_b.py" "$COND" "$TRIAL" &
child_pid=$!
wait "$child_pid"
status=$?
child_pid=""
exit "$status"
