#!/usr/bin/env bash
# setup-streams.sh — Create/update the NOTIFY stream and its consumers.
# Idempotent: safe to run repeatedly.
#
# The nats:alpine server image does NOT include the `nats` management CLI.
# That CLI lives in the separate `natsio/nats-box` image. We pull a tiny
# one-shot box container and point it at the host's 127.0.0.1:4222 (which
# the nats compose maps locally).

set -euo pipefail

NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
NATS_BOX_IMAGE="${NATS_BOX_IMAGE:-natsio/nats-box:latest}"
NATS_HEALTH_URL="${NATS_HEALTH_URL:-http://127.0.0.1:8222/healthz}"

# Wait for the server to be reachable on the host.
for i in $(seq 1 30); do
    if curl -fsS "$NATS_HEALTH_URL" >/dev/null 2>&1; then
        break
    fi
    [ "$i" -eq 30 ] && { echo "NATS not ready after 30s ($NATS_HEALTH_URL)" >&2; exit 1; }
    sleep 1
done

# Run the nats CLI inside a throwaway nats-box container on the host
# network so it reaches 127.0.0.1:4222.
nats_cli() {
    docker run --rm --network host "$NATS_BOX_IMAGE" \
        nats --server "$NATS_URL" "$@"
}

# --- Stream ---------------------------------------------------------------
#
# `--storage` is a CREATE-time-only property; `nats stream update` rejects
# it ("unknown long flag '--storage'"). So the create path sets storage and
# the update path passes only mutable fields (which includes --max-msg-size,
# the whole reason we re-run this — to raise it to 8 MB for media).

stream_add_args=(
    --subjects "notify.>"
    --storage file
    --retention work
    --max-age 24h
    --max-msgs 10000
    --max-msg-size 8388608
    --max-bytes=-1
    --max-consumers=-1
    --discard old
    --replicas 1
    --dupe-window 2m
    --defaults
)

stream_update_args=(
    --subjects "notify.>"
    --retention work
    --max-age 24h
    --max-msgs 10000
    --max-msg-size 8388608
    --max-bytes=-1
    --max-consumers=-1
    --discard old
    --replicas 1
    --dupe-window 2m
    -f
)

if ! nats_cli stream info NOTIFY >/dev/null 2>&1; then
    echo "Creating stream NOTIFY..."
    nats_cli stream add NOTIFY "${stream_add_args[@]}"
else
    echo "Updating stream NOTIFY (mutable fields only)..."
    nats_cli stream update NOTIFY "${stream_update_args[@]}"
fi

# --- Consumers ------------------------------------------------------------
#
# Consumer config is largely immutable after creation (ack policy, replay,
# deliver, filter subject, pull/push). Rather than risk an "unknown flag" or
# "cannot change" error on update, we create-if-missing and otherwise leave
# the consumer as-is. To change a consumer's fixed config, delete + re-run.
#
#   whatsapp-bridge → notify.whatsapp        (text)
#   whatsapp-media  → notify.whatsapp.media  (binary attachments)
# NATS subject matching is exact, so `notify.whatsapp` does NOT capture
# `notify.whatsapp.media` — hence two distinct consumers.

ensure_consumer() {
    local name="$1" filter="$2"
    if nats_cli consumer info NOTIFY "$name" >/dev/null 2>&1; then
        echo "Consumer $name already exists — leaving as-is."
        return 0
    fi
    echo "Creating consumer $name (filter: $filter)..."
    nats_cli consumer add NOTIFY "$name" \
        --filter "$filter" \
        --ack explicit \
        --pull \
        --deliver all \
        --max-deliver 10 \
        --wait 30s \
        --replay instant \
        --defaults
}

ensure_consumer whatsapp-bridge "notify.whatsapp"
ensure_consumer whatsapp-media  "notify.whatsapp.media"

echo "NOTIFY stream + whatsapp-bridge + whatsapp-media consumers ready."
nats_cli stream info NOTIFY
