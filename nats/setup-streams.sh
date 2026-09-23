#!/usr/bin/env bash
# setup-streams.sh — Create/update the NOTIFY stream and whatsapp-bridge
# consumer. Idempotent: run as many times as you want; converges to the
# definition below.
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
# `--defaults` accepts defaults for any options we don't pin explicitly,
# so we don't get prompted for niche fields. We try `add` first; if the
# stream already exists, fall through to `update`.

stream_args=(
    --subjects "notify.>"
    --storage file
    --retention work
    --max-age 24h
    --max-msgs 10000
    --max-msg-size 16384
    --max-bytes=-1
    --max-consumers=-1
    --discard old
    --replicas 1
    --dupe-window 2m
    --defaults
)

if ! nats_cli stream info NOTIFY >/dev/null 2>&1; then
    nats_cli stream add NOTIFY "${stream_args[@]}"
else
    nats_cli stream update NOTIFY "${stream_args[@]}"
fi

# --- Consumer -------------------------------------------------------------
#
# `whatsapp-bridge` is a durable pull consumer. The WhatsApp server's
# in-process subscriber binds to it, so messages persist across server
# restarts.

consumer_add_args=(
    --filter "notify.whatsapp"
    --ack explicit
    --pull
    --deliver all
    --max-deliver 10
    --wait 30s
    --replay instant
    --defaults
)

consumer_update_args=(
    --filter "notify.whatsapp"
    --ack explicit
    --max-deliver 10
    --wait 30s
    --replay instant
    --defaults
)

if ! nats_cli consumer info NOTIFY whatsapp-bridge >/dev/null 2>&1; then
    nats_cli consumer add NOTIFY whatsapp-bridge "${consumer_add_args[@]}"
else
    nats_cli consumer update NOTIFY whatsapp-bridge "${consumer_update_args[@]}"
fi

echo "NOTIFY stream + whatsapp-bridge consumer ready."
nats_cli stream info NOTIFY
