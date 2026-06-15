# Home Assistant config snippets

## Audio reminders

Place mp3 files in `/opt/homeassistant/config/www/audio` for the
water/bathroom reminder automations.

## Power automation

Files:
- `configuration.yaml` — adds `input_datetime.esp32_last_seen` and
  `shell_command.whatsapp_alert`.
- `power_automation.yaml` — six automations covering heartbeat,
  dead-ESP32 alert, power-cut, Tapo failure, power-restored, and
  Mini PC stuck-on-boot.

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
