# whatsapp-api/

Drop-in module that adds a NATS JetStream subscriber to your existing
WhatsApp (Baileys) server. The server stays the same single Node
process — `nats_consumer.js` runs in-process alongside the Express
routes (`/`, `/send`, `/health`).

## Why in-process instead of a separate bridge?

For a single notification channel (WhatsApp), a dedicated bridge
service is overhead: another venv, another systemd unit, another
restart story, and an extra HTTP self-call hop. The embedded
consumer reuses the existing `sendMessage()` directly. When future
channels (Discord, SMS) materialise, *they* each get their own
in-process subscriber inside their own service — there's no central
bridge to maintain.

The module is intentionally **loosely coupled** to Baileys: it
accepts `sendMessage(to, text)` and `isReady()` as injected
functions. `nats_consumer.js` does not import `@whiskeysockets/baileys`
itself.

## Files

| File | Purpose |
|---|---|
| `nats_consumer.js` | Pull-consumer loop with ack / term / nak-with-backoff semantics |

## Wire protocol

The daemon publishes JSON on subject `notify.whatsapp`:

```json
{
  "to": "<jid or group id>",
  "text": "<message body>"
}
```

The consumer forwards exactly these two fields to `sendMessage()`.

## Integration recipe

### 1. Install the NATS client

```bash
cd <your whatsapp-api repo>
npm install nats
```

### 2. Copy `nats_consumer.js` next to `server.js`

```bash
cp /tmp/corex-mini-pc/whatsapp-api/nats_consumer.js .
```

### 3. Expose `sendMessage` and `isReady` from `server.js`

Refactor your `/send` handler so it calls a `sendMessage(to, text)`
helper that's also exportable. Define `isReady()` based on whatever
condition your `/health` endpoint already uses (typically
`!!(sock && sock.user)`).

```js
// server.js  (sketch — adapt to your actual structure)

let sock = null;
// ...your existing Baileys init...

export function isReady() {
    return !!(sock && sock.user);
}

export async function sendMessage(to, text) {
    if (!isReady()) {
        throw new Error("WhatsApp not connected");
    }
    const jid = formatJid(to);            // your existing helper
    await sock.sendMessage(jid, { text }); // Baileys call you already have
}

// existing /send route — now just delegates
app.post("/send", async (req, res) => {
    try {
        await sendMessage(req.body.to, req.body.message);
        res.json({ status: "ok" });
    } catch (err) {
        res.status(503).json({ status: "error", error: err.message });
    }
});

// existing /health route — uses isReady()
app.get("/health", (req, res) => {
    if (isReady()) return res.json({ status: "ok", connected: true });
    return res.status(503).json({ status: "unavailable", connected: false });
});
```

### 4. Start the NATS subscriber once Baileys is connected

```js
import { startNatsConsumer } from "./nats_consumer.js";

// Inside your connection.update handler, once you see connection === "open":
sock.ev.on("connection.update", async ({ connection }) => {
    if (connection === "open" && !natsStarted) {
        natsStarted = true;
        startNatsConsumer({ sendMessage, isReady }).catch(err => {
            console.error("NATS consumer crashed:", err);
            // Don't kill the HTTP server — /send keeps working via HA.
        });
    }
});
```

Use a `natsStarted` flag so reconnects don't spawn multiple consumers.

### 5. Environment variables (optional, defaults shown)

| Var | Default | |
|---|---|---|
| `NATS_URL` | `nats://127.0.0.1:4222` | Where to reach the NATS server |
| `NATS_STREAM` | `NOTIFY` | Stream name (matches `nats/setup-streams.sh`) |
| `NATS_CONSUMER` | `whatsapp-bridge` | Durable consumer name (don't change after first run — the name pins to its state in JetStream) |

If your container runs on the host network (same as HA does), the
defaults work as-is. If on a Docker bridge network, set `NATS_URL=nats://nats:4222`
and join the NATS compose network.

## Verify

```bash
# WhatsApp connected; subscriber bound
# Server log should show:
#   [nats] connected to nats://127.0.0.1:4222
#   [nats] bound to NOTIFY/whatsapp-bridge

# Send a test from the host
docker exec -it nats nats pub notify.whatsapp \
  '{"to":"<your group/jid>","text":"embedded consumer test"}'
# → WhatsApp message arrives
# → server log: "[nats] delivered msg_id=... delivered=1"
```

## Publishing to this consumer

Anything that publishes to `notify.whatsapp` reaches this consumer.
CLI, Python (`nats-py`), Node (`nats`), and a future HA→NATS shim are
all covered in [`nats/README.md`](../nats/README.md#sending-a-message).
The wire format is:

```json
{ "to": "<jid or group id>", "text": "<message>" }
```

Use the `Nats-Msg-Id` header for dedup if your producer might retry.

## Failure semantics quick-reference

| Situation | Action | Effect |
|---|---|---|
| Bad payload (not JSON or missing keys) | `msg.term()` | dropped, no retry |
| Sock not ready (`isReady() === false`) | `msg.nak(30s)` | retry in 30s |
| `sendMessage()` throws | `msg.nak(backoff)` | retry with `[5s,15s,30s,1m,5m,10m,30m]` capped at last |
| Process crash mid-delivery | message unacked | JetStream redelivers when subscriber returns |

`max-deliver` on the consumer (10, configured in
`nats/setup-streams.sh`) bounds total attempts so a permanently-bad
message can't loop forever.
