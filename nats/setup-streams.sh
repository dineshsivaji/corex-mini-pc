#!/usr/bin/env bash
# setup-streams.sh — Create/update the NOTIFY stream and whatsapp-bridge
# consumer. Idempotent: run as many times as you want; converges to the
# definition below.
#
# Runs inside the nats container (via `docker exec`) so we don't need
# the `nats` CLI on the host.

set -euo pipefail

CONTAINER="${NATS_CONTAINER:-nats}"

nats_cli() {
    docker exec "$CONTAINER" nats "$@"
}

# Wait for the server to be reachable inside the container.
for i in $(seq 1 30); do
    if docker exec "$CONTAINER" wget -qO- http://localhost:8222/healthz >/dev/null 2>&1; then
        break
    fi
    [ "$i" -eq 30 ] && { echo "NATS not ready after 30s" >&2; exit 1; }
    sleep 1
done

# --- Stream ---------------------------------------------------------------

nats_cli stream add NOTIFY \
    --subjects "notify.>" \
    --storage file \
    --retention work \
    --max-age 24h \
    --max-msgs 10000 \
    --max-msg-size 16384 \
    --max-bytes -1 \
    --max-consumers -1 \
    --discard old \
    --replicas 1 \
    --dupe-window 2m \
    --no-allow-rollup \
    --no-deny-delete \
    --no-deny-purge \
    --defaults 2>/dev/null \
  || nats_cli stream update NOTIFY \
    --subjects "notify.>" \
    --storage file \
    --retention work \
    --max-age 24h \
    --max-msgs 10000 \
    --max-msg-size 16384 \
    --max-bytes -1 \
    --max-consumers -1 \
    --discard old \
    --replicas 1 \
    --dupe-window 2m \
    --no-allow-rollup \
    --no-deny-delete \
    --no-deny-purge \
    --defaults

# --- Consumer -------------------------------------------------------------

# `whatsapp-bridge` is a durable pull consumer. The bridge service binds
# to it, so messages persist across bridge restarts.
nats_cli consumer add NOTIFY whatsapp-bridge \
    --filter "notify.whatsapp" \
    --ack explicit \
    --pull \
    --deliver all \
    --max-deliver 10 \
    --wait 30s \
    --replay instant \
    --no-headers-only \
    --defaults 2>/dev/null \
  || nats_cli consumer update NOTIFY whatsapp-bridge \
    --filter "notify.whatsapp" \
    --ack explicit \
    --max-deliver 10 \
    --wait 30s \
    --replay instant \
    --no-headers-only \
    --defaults

echo "NOTIFY stream + whatsapp-bridge consumer ready."
nats_cli stream info NOTIFY --json | head -40
