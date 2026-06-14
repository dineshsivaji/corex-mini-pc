#!/bin/bash
# Polls until $1 is a real mountpoint, or until $2 seconds pass.
# Defaults: /mnt/storage, 120s.
# Exit 0 when mounted, exit 1 on timeout.

set -eu

MOUNT="${1:-/mnt/storage}"
TIMEOUT="${2:-120}"
ELAPSED=0
INTERVAL=2

while ! mountpoint -q "$MOUNT"; do
    if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
        echo "wait-for-mount: TIMEOUT after ${TIMEOUT}s waiting for $MOUNT" >&2
        exit 1
    fi
    sleep "$INTERVAL"
    ELAPSED=$((ELAPSED + INTERVAL))
done

echo "wait-for-mount: $MOUNT ready after ${ELAPSED}s"
