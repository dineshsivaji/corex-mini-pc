This repo captures the docs specific to challenges I have faced setting up the mini pc
and how I have tackled it by applying certain changes to it.

## Quick deploy

After pulling fresh changes, push them to the live Mini PC paths with:

```bash
chmod +x deploy.sh                  # one-time
./deploy.sh ha                      # HA only (most common)
./deploy.sh daemon                  # power-daemon only
./deploy.sh storage-wait            # wait-for-storage systemd setup
./deploy.sh all                     # everything
```

What it does:

| Target | Files refreshed |
|---|---|
| `daemon` | `/opt/power-daemon/power_daemon.py`, `/etc/systemd/system/power-daemon.service`. Restarts `power-daemon`. |
| `storage-wait` | `/usr/local/bin/wait-for-mount.sh`, `/etc/systemd/system/wait-for-storage.service`, `/etc/systemd/system/docker.service.d/wait-for-storage.conf`. Effective on next reboot. |
| `ha` | `/opt/homeassistant/config/configuration.yaml`, `quote_lines.yaml`. Smart-merges `automations.yaml` (preserves your water/bathroom/bulb automations, refreshes the power-automation block). Restarts the HA container. |

What it **never** touches:
- `/etc/default/power-daemon` (holds Tapo password)
- `/opt/homeassistant/config/secrets.yaml`
- Non-power automations in `automations.yaml` (water reminder, bathroom, bulb, etc.)

The script backs up every file before overwriting (`*.bak.<timestamp>`).

### Verification after `ha`

Set `HA_TOKEN` so the script can confirm the automation registered:

```bash
export HA_TOKEN="<long-lived-token-from-HA-profile>"
./deploy.sh ha
```

Without the token, you'll get a hint at the end with the curl command to verify manually.
