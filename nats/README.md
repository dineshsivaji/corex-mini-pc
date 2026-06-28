# nats/

NATS JetStream as the durable transport for notifications on the Mini PC.

## Why

Pre-NATS path was fire-and-forget HTTP from `power-daemon` to HA
webhooks, then HA's `shell_command.whatsapp_alert` curl'd the
WhatsApp server. Two race windows during boot (HA warmup, WhatsApp
Baileys socket negotiation) could drop the recovery message silently.

NATS JetStream gives us:
- **Durability** — messages persist to disk; survive Mini PC reboots,
  WhatsApp container restarts, and broker restarts.
- **At-least-once delivery** — JetStream redelivers on `nak` with
  configurable backoff, up to `max-deliver` attempts.
- **Deduplication** — `Nats-Msg-Id` header within the stream's
  `dupe-window` (2m) suppresses duplicate publishes from retry loops.

Consumers live *inside* each notification-channel service (currently
just WhatsApp; see `whatsapp-api/nats_consumer.js`). There is no
separate bridge process.

## Pieces

| File | Role |
|---|---|
| `docker-compose.yml` | NATS container, JetStream enabled, `127.0.0.1:4222` only, persistent `nats-data` volume |
| `setup-streams.sh` | Idempotent `nats stream add` / `nats consumer add` |

## Stream design

| Stream | Value | Reason |
|---|---|---|
| Name | `NOTIFY` | |
| Subjects | `notify.>` | room for `notify.whatsapp`, `notify.discord`, … |
| Storage | `file` | survives reboots |
| Retention | `WorkQueue` | deleted on first ack — keeps disk near zero |
| Max age | `24h` | unacked messages expire after a day |
| Max msgs | `10000` | hard cap if a consumer loops |
| Dupe window | `2m` | `Nats-Msg-Id` retries within this window are suppressed |

| Consumer | Value | Reason |
|---|---|---|
| Name | `whatsapp-bridge` | role-based — describes the delivery target |
| Filter | `notify.whatsapp` | this consumer only handles WhatsApp |
| Ack policy | `explicit` | the consumer controls when a message is "done" |
| Max deliver | `10` | give up after 10 attempts |
| Ack wait | `30s` | retry quickly after a transient failure |

## Deploy

From the repo root:

```bash
./deploy.sh nats          # docker compose up + stream/consumer setup
```

`nats` is idempotent — re-running just no-ops if the stream/consumer
already match.

---

# Sending a message

## Wire protocol

All notification subjects under `notify.<channel>` carry the same
shape:

```json
{
  "to":   "<recipient>",
  "text": "<message body>"
}
```

`to` is opaque to NATS — each channel's consumer interprets it
(WhatsApp: phone-with-country-code, jid, or group id).

**Header `Nats-Msg-Id`** is optional but recommended. If two
publishes within the 2-minute dupe window use the same `Nats-Msg-Id`,
JetStream stores the message exactly once.

Suggested format: `<kind>-<event-timestamp>`, e.g. `power_cut-1719567890`.

## From the CLI (handy for testing and one-off alerts)

The `nats:alpine` server image doesn't include the `nats` management
CLI. Run it via a throwaway `natsio/nats-box` container. Easiest is
a shell alias on the host:

```bash
alias nats='docker run --rm --network host natsio/nats-box nats --server nats://127.0.0.1:4222'
```

Then:

```bash
# Send a one-off
nats pub notify.whatsapp '{"to":"<your jid or group>","text":"hello from CLI"}'

# With a Nats-Msg-Id (dedup-safe — repeating the command in <2m no-ops)
nats pub notify.whatsapp '{"to":"<jid>","text":"only once"}' \
  -H "Nats-Msg-Id:manual-$(date +%s)"

# Watch the consumer drain it
nats consumer info NOTIFY whatsapp-bridge
```

## From Python (`nats-py`) — pattern used by `power_daemon.py`

```python
import asyncio, json, nats

async def main():
    nc = await nats.connect("nats://127.0.0.1:4222", name="my-producer")
    js = nc.jetstream()

    payload = json.dumps({
        "to":   "919999999999",
        "text": "🔔 alert from my service",
    }).encode()

    ack = await js.publish(
        "notify.whatsapp",
        payload,
        headers={"Nats-Msg-Id": "my_service-event-1719567890"},
    )
    print(f"stored as stream={ack.stream} seq={ack.seq}")

    await nc.drain()

asyncio.run(main())
```

`js.publish()` blocks until JetStream durably stores the message
(returns a `PubAck`). That's what makes the publish reliable: when
the call returns successfully, the message is on disk and will be
delivered eventually.

`nc.drain()` flushes pending publishes before closing — important
for short-lived publishers.

See `tapo-power-automation/minipc/power_daemon.py` (function
`publish(...)`) for the production version with timeout handling.

## From Node (`nats`) — if a Node service ever needs to publish

```js
import { connect, headers as natsHeaders } from "nats";

const nc = await connect({ servers: "nats://127.0.0.1:4222" });
const js = nc.jetstream();

const h = natsHeaders();
h.set("Nats-Msg-Id", "my_service-event-1719567890");

const ack = await js.publish(
  "notify.whatsapp",
  new TextEncoder().encode(JSON.stringify({
    to:   "919999999999",
    text: "🔔 alert from a Node service",
  })),
  { headers: h }
);
console.log(`stored as stream=${ack.stream} seq=${ack.seq}`);

await nc.drain();
```

## From HA (`shell_command`) — not wired up yet

HA can't natively `nats pub`; the `nats` CLI isn't in the HA
container, and JetStream has no native HTTP publish endpoint. Three
viable options if you ever migrate HA's `Dead ESP32` / `Mini PC
Stuck` alerts off direct curl:

1. **HTTP→NATS shim** (~30 lines, FastAPI or Express). HA's
   `shell_command` curls the shim; the shim publishes.
2. **Custom HA image** with the `nats` CLI installed (one-line
   Dockerfile). `shell_command` becomes:
   `nats --server nats://host:4222 pub notify.whatsapp '{"to":..., "text":...}'`
3. **MQTT bridge** — NATS supports an MQTT adapter; HA has first-
   class MQTT integration. Heavier but plays nicely with future
   ESP32 → MQTT plans.

Until any of those exist, HA's two remaining WhatsApp automations
keep using `curl /send` directly (the unreliability we worried about
was on the daemon side; HA→curl is reliable once HA is up).

## Subject conventions

| Subject | Meaning |
|---|---|
| `notify.whatsapp` | WhatsApp delivery (consumed by `whatsapp-api/nats_consumer.js`) |
| `notify.discord` | *(future)* Discord delivery |
| `notify.sms` | *(future)* SMS delivery |
| `notify.email` | *(future)* email delivery |

The `notify.>` wildcard on the stream picks up anything new without
config changes. Add a new consumer per channel (in its own service)
with a `filter_subject` of just that channel's subject.

## Failure semantics quick reference

What happens to a published message:

| Scenario | What JetStream does |
|---|---|
| `js.publish` returns successfully | Message is on disk, durable. |
| Consumer `ack()`s | Message is deleted (WorkQueue retention). |
| Consumer `nak(delay)`s | Message redelivered after `delay`. Counts toward `max-deliver`. |
| Consumer `term()`s | Message dropped immediately, no redelivery. Use for bad payloads. |
| `max-deliver` (10) hit | Message dropped. With WorkQueue + no DLQ, it's just gone. |
| `max-age` (24h) hit | Message dropped regardless of ack state. |
| `Nats-Msg-Id` collision within `dupe-window` (2m) | Second publish silently no-ops. |

## Verify

```bash
# Broker healthy
docker ps | grep nats
curl -s http://localhost:8222/healthz                 # → "ok"

# Stream + consumer present (using the alias from above)
nats stream info NOTIFY
nats consumer info NOTIFY whatsapp-bridge

# Inspect pending / redelivered
nats consumer info NOTIFY whatsapp-bridge --json \
  | jq '{num_pending, num_redelivered, num_ack_pending}'

# Peek at messages (non-destructive)
nats stream view NOTIFY
```

## Where the data lives

Docker named volume `nats-data` → `/var/lib/docker/volumes/nats_nats-data/_data/`
on the boot SSD. Confirm with `docker volume inspect nats_nats-data`.

**Do not** put `nats-data` on `/mnt/storage` (USB-SATA HDD) — JetStream
does sync writes and the USB-SATA bridge has historically been a boot
race liability (see `storage-wait/`).
