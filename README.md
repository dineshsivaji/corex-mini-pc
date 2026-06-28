This repo captures the docs specific to challenges I have faced setting up the mini pc
and how I have tackled it by applying certain changes to it.

## Quick deploy

After pulling fresh changes, push them to the live Mini PC paths with:

```bash
chmod +x deploy.sh                  # one-time
./deploy.sh ha                      # HA only (most common)
./deploy.sh daemon                  # power-daemon (also pushes quote_lines.yaml)
./deploy.sh nats                    # NATS server + stream/consumer setup
./deploy.sh storage-wait            # wait-for-storage systemd setup
./deploy.sh all                     # everything in the right order
```

What it does:

| Target | Files refreshed |
|---|---|
| `daemon` | `/opt/power-daemon/power_daemon.py`, `/opt/power-daemon/quote_lines.yaml`, `/etc/systemd/system/power-daemon.service`. Restarts `power-daemon`. |
| `nats` | `/opt/nats/docker-compose.yml`, `/opt/nats/setup-streams.sh`. `docker compose up -d` + idempotent stream/consumer config. |
| `storage-wait` | `/usr/local/bin/wait-for-mount.sh`, `/etc/systemd/system/wait-for-storage.service`, `/etc/systemd/system/docker.service.d/wait-for-storage.conf`. Effective on next reboot. |
| `ha` | `/opt/homeassistant/config/configuration.yaml`, `quote_lines.yaml`. Smart-merges `automations.yaml` (preserves your water/bathroom/bulb automations, refreshes the power-automation block). Restarts the HA container. |

What it **never** touches:
- `/etc/default/power-daemon` (holds Tapo password)
- `/opt/homeassistant/config/secrets.yaml`
- Non-power automations in `automations.yaml` (water reminder, bathroom, bulb, etc.)
- Your WhatsApp server (`server.js`) — see `whatsapp-api/README.md` for the in-process NATS consumer integration.

The script backs up every file before overwriting (`*.bak.<timestamp>`).

## Notification pipeline

```
power_daemon ──nats.publish──► [NATS JetStream NOTIFY]
                                          │
                                          │ pull-consumer (in-process)
                                          ▼
                                whatsapp-api (server.js)
                                  ├── HTTP /send  (HA still uses this)
                                  ├── HTTP /health
                                  └── NATS subscriber  ← new module
                                          │
                                          ▼
                                sendMessage(to, text) → Baileys
```

The daemon formats the final WhatsApp text (including the rotating
"Mini PC is X" status line — cursor in `/var/lib/power-daemon/recovery_cursor`)
and publishes to `notify.whatsapp`. The WhatsApp server's in-process
NATS consumer (see `whatsapp-api/`) pulls and forwards directly to
Baileys. No separate bridge process.

HA still owns:
- Non-power alerts (`Dead ESP32`, `Mini PC Stuck`) via
  `shell_command.whatsapp_alert → curl /send`.
- ESP32 heartbeat tracking via `input_datetime.esp32_last_seen`.

HA no longer owns:
- The 3 power-event automations (`Power Cut Imminent`, `Tapo Command
  Failed`, `Mini PC Recovered`) — the daemon publishes those directly.
- Quote-line rotation — moved into the daemon.

### Sending a message through NATS

Anyone (CLI, Python service, Node service) can publish to
`notify.whatsapp` and the WhatsApp server's in-process consumer will
deliver it. Quick CLI test:

```bash
docker exec -it nats nats pub notify.whatsapp \
  '{"to":"<your jid or group>","text":"hello via NATS"}'
```

Full publish recipes — Python (`nats-py`), Node (`nats`), Nats-Msg-Id
dedup, subject conventions, failure semantics — live in
[`nats/README.md`](nats/README.md#sending-a-message). The canonical
Python pattern is `publish(...)` in
[`tapo-power-automation/minipc/power_daemon.py`](tapo-power-automation/minipc/power_daemon.py).

### Verification after `ha`

Set `HA_TOKEN` so the script can confirm the automation registered:

```bash
export HA_TOKEN="<long-lived-token-from-HA-profile>"
./deploy.sh ha
```

Without the token, you'll get a hint at the end with the curl command to verify manually.
