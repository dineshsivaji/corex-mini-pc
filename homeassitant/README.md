# Home Assistant config snippets

## Audio reminders

Place mp3 files in `/opt/homeassistant/config/www/audio` for the
water/bathroom reminder automations.

## Power automation

Files:
- `configuration.yaml` — adds `input_datetime.esp32_last_seen`,
  `shell_command.whatsapp_alert`, and `counter.recovery_index`.
- `power_automation.yaml` — six automations covering heartbeat,
  dead-ESP32 alert, power-cut, Tapo failure, Mini PC stuck-on-boot,
  and Mini PC recovery.
- `quote_lines.yaml` — 100 funny status descriptors used by the
  recovery WhatsApp. Loaded via `!include` from `power_automation.yaml`.

### Required `secrets.yaml` entry

The WhatsApp phone number is loaded from secrets. In your HA config
directory, ensure `secrets.yaml` contains:

```yaml
whatsapp_phone: "919XXXXXXXXX"   # WhatsApp number including country code
```

`secrets.yaml` should NOT be committed.

### Wiring `power_automation.yaml` into HA

The contents of `power_automation.yaml` are a list of automations. You can
either:

- Append them to your existing `automations.yaml`, or
- Switch `configuration.yaml` to `automation: !include_dir_merge_list automations/`
  and drop the file into the `automations/` directory.

### WhatsApp service prerequisite

The `shell_command.whatsapp_alert` posts to `http://localhost:3001/send`
(WhatsApp HTTP API running on the Mini PC). It expects a JSON body
`{"to": "...", "message": "..."}`. HA must be on `network_mode: host`
(already configured in `docker-compose.yml`) so that `localhost` resolves
to the Mini PC.

### Webhook URLs (for reference)

- `POST /api/webhook/esp32_heartbeat` — ESP32 hourly heartbeat
- `POST /api/webhook/power_cut_imminent` — Mini PC daemon, pre-shutdown
- `POST /api/webhook/tapo_command_failed` — Mini PC daemon, Tapo retry exhausted
- `POST /api/webhook/esp32_mini_pc_stuck` — ESP32 when Tapo on but Mini PC down >10 min
- `POST /api/webhook/minipc_recovered` — power-daemon on startup (includes `outage_minutes` after a managed shutdown; bare on manual reboot)

### Recovery message rotation

The "Mini PC Recovered" alert appends one of 100 status lines from
`quote_lines.yaml`, picked sequentially via `counter.recovery_index`.
The counter persists across HA restarts (`restore: true`), so the cycle
continues normally after reboots.

To add or edit lines, just edit `quote_lines.yaml` and reload HA
automations:

```bash
curl -X POST http://localhost:8123/api/services/automation/reload \
  -H "Authorization: Bearer <TOKEN>"
```

The modulo math (`counter % len(lines)`) adapts to whatever length the
list ends up at.

To reset the cursor to the top of the list:

```bash
curl -X POST http://localhost:8123/api/services/counter/reset \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "counter.recovery_index"}'
```
