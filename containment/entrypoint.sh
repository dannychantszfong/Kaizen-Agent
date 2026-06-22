#!/bin/sh
# Kaizen container entrypoint - runs as root, hands off to the watchdog.
#
# It is deliberately tiny. The agent<->substrate permission boundary is encoded
# at image-build time (immortal tree root:root r-x; agent/, state/, journal/,
# .git owned by `agent`) and inherited by the named volume on first run. We do
# NOT chown/chmod here: re-applying the boundary at runtime would require the
# CAP_CHOWN / CAP_DAC_OVERRIDE the container intentionally drops. This script
# only sanity-checks the tree and execs the supervisor.
set -eu

LINEAGE="${KAIZEN_ROOT:-/lineage}"

if [ ! -d "$LINEAGE/substrate" ] || [ ! -f "$LINEAGE/watchdog.py" ]; then
    echo "[entrypoint] FATAL: $LINEAGE is not a Kaizen tree (missing substrate/ or watchdog.py)." >&2
    echo "[entrypoint] The named volume must initialize from the image; do not mount an empty host dir over it." >&2
    exit 1
fi

# These must exist and be agent-owned BEFORE the watchdog's ensure_dirs() runs:
# if the root watchdog created them they would be root-owned and the agent runner
# could not write into them. The image bakes them; a fresh volume inherits them.
for d in "$LINEAGE/substrate/state" "$LINEAGE/substrate/journal"; do
    if [ ! -d "$d" ]; then
        echo "[entrypoint] FATAL: missing writable dir $d (volume not initialized from image?)." >&2
        exit 1
    fi
done

echo "[entrypoint] starting watchdog as $(id -un); runner prefix='${KAIZEN_RUNNER_PREFIX:-}'"

# exec: the watchdog becomes tini's supervised child (effectively PID 1's child).
# It runs as root and drops to the `agent` uid per generation via
# KAIZEN_RUNNER_PREFIX="gosu agent". Extra args ("$@") pass through to the
# watchdog (e.g. --max-generations, --budget-cap).
exec python "$LINEAGE/watchdog.py" --root "$LINEAGE" "$@"
