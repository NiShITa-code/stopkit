#!/bin/bash
# Unscored trials only: FAST smoke runs by default, or full-length pilots with STOPCHECK_FAST=0.
# Never a screen. Stop at the first failed or untested trial. Defaults are B0 then PC.
set -uo pipefail
export STOPCHECK_FAST="${STOPCHECK_FAST:-1}"
# The harness accepts FAST timing only as smoke and full timing only as pilot (or a runner-registered screen).
if [ "$STOPCHECK_FAST" = 1 ]; then kind=smoke; else kind=pilot; fi
export STOPCHECK_RUN_KIND="${STOPCHECK_RUN_KIND:-$kind}"
if [ "$STOPCHECK_RUN_KIND" != "$kind" ]; then
    printf 'Preflight runs %s trials when STOPCHECK_FAST=%s, never %s.\n' "$kind" "$STOPCHECK_FAST" "$STOPCHECK_RUN_KIND" >&2
    exit 2
fi
# Preflight trials belong to no freeze or batch.
unset STOPCHECK_FROZEN_MANIFEST STOPCHECK_BATCH
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" -eq 0 ]; then set -- B0 PC; fi
# Trial work directories are never reused or deleted, so every run needs a new trial ID.
STAMP="$(date -u +%Y%m%d-%H%M%S)"
for condition in "$@"; do
    printf 'Preflight %s\n' "$condition"
    # No pipe: preserve the harness exit code, including 3 (untested).
    bash "$HERE/harness_b_ns.sh" "$condition" "XB-preflight-$STAMP-$condition"
    status=$?
    if [ "$status" -ne 0 ]; then
        printf 'Preflight stopped: %s returned %s. Inspect its raw record.\n' "$condition" "$status" >&2
        exit "$status"
    fi
done
printf 'Preflight completed. These runs are excluded from research counts.\n'
