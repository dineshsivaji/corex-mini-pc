# Deployment Guide — Power Automation (End-to-End)

This walks you from the source tree in this repo to a fully deployed
system. Run each phase and verify before moving to the next.

---

## Phase 0 — Pre-flight checks

### 0.1 Confirm IP addresses match your network

Verify these match your actual setup. If any differ, fix them in the
config files **before** proceeding:

```bash
ping -c 1 192.168.1.110   # Zeb SP110 should respond
ping -c 1 192.168.1.111   # Tapo P110 should respond
ping -c 1 192.168.1.32    # ESP32 should respond (after Phase 3)
hostname -I               # Mini PC's own IP — should be 192.168.1.50
```

### 0.2 Verify WhatsApp API is running

```bash
curl -X POST http://localhost:3001/send \
  -H "Content-Type: application/json" \
  -d '{"to":"YOUR_PHONE","message":"setup test"}'
```

You should receive a WhatsApp message. If not, fix the WhatsApp service
first.

### 0.3 Verify HDD device path

```bash
lsblk
# Confirm /dev/sda is your 3.5" HDD with /mnt/storage mountpoint
```

If your HDD is on a different device (`/dev/sdb`, etc.), note it for
step 1.4.

---

## Phase 1 — Mini PC daemon deployment

### 1.1 Install system dependencies

```bash
sudo apt update
sudo apt install -y hdparm python3-venv
```

`python3-venv` is needed to create the virtual environment in step 1.4.
We deliberately do **not** install `python-kasa` system-wide — it goes
into a dedicated venv to avoid polluting the system Python and to
sidestep PEP 668 ("externally-managed-environment") on Ubuntu 24.04+.

### 1.2 Stop existing daemon (if running)

```bash
sudo systemctl stop power-daemon 2>/dev/null
sudo systemctl disable power-daemon 2>/dev/null
```

### 1.3 Deploy daemon files

```bash
sudo mkdir -p /opt/power-daemon
sudo cp tapo-power-automation/minipc/power_daemon.py /opt/power-daemon/
sudo cp tapo-power-automation/minipc/power-daemon.service /etc/systemd/system/
sudo chmod 755 /opt/power-daemon/power_daemon.py
```

### 1.4 Create dedicated venv and install python-kasa

```bash
sudo python3 -m venv /opt/power-daemon/venv
sudo /opt/power-daemon/venv/bin/pip install --upgrade pip
sudo /opt/power-daemon/venv/bin/pip install \
  -r tapo-power-automation/minipc/requirements.txt
```

Verify the install:

```bash
sudo /opt/power-daemon/venv/bin/python -c "from kasa import Discover; print('OK')"
```

Should print `OK`.

To upgrade `python-kasa` later:

```bash
sudo /opt/power-daemon/venv/bin/pip install --upgrade python-kasa
sudo systemctl restart power-daemon
```

To wipe and rebuild the venv:

```bash
sudo rm -rf /opt/power-daemon/venv
# then repeat step 1.4
```

### 1.5 Create env file with real credentials

```bash
sudo cp tapo-power-automation/minipc/power-daemon.env.example /etc/default/power-daemon
sudo chmod 600 /etc/default/power-daemon
sudo chown root:root /etc/default/power-daemon
sudo nano /etc/default/power-daemon
```

Set real values:

```bash
POWER_DAEMON_TAPO_EMAIL=your_actual_tapo_email@example.com
POWER_DAEMON_TAPO_PASSWORD=your_actual_tapo_password
POWER_DAEMON_HDD_DEVICE=/dev/sda            # adjust if needed
POWER_DAEMON_STORAGE_MOUNT=/mnt/storage     # adjust if needed
# Other defaults are fine
```

### 1.6 Configure sudoers (passwordless privileged commands)

```bash
sudo visudo -f /etc/sudoers.d/power-daemon
```

Paste:

```
dinesh ALL=(ALL) NOPASSWD: /sbin/shutdown
dinesh ALL=(ALL) NOPASSWD: /sbin/hdparm
dinesh ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop docker
dinesh ALL=(ALL) NOPASSWD: /usr/bin/umount /mnt/storage
```

(Save: `Ctrl+O`, `Enter`, `Ctrl+X`.) `visudo` refuses to save bad
syntax — if you exit cleanly, it parsed fine.

### 1.7 Update fstab for non-blocking HDD mount

```bash
sudo cp /etc/fstab /etc/fstab.bak
sudo nano /etc/fstab
```

Find the `/mnt/storage` line and ensure options include
`nofail,x-systemd.device-timeout=60s`:

```
UUID=xxxx /mnt/storage ext4 defaults,nofail,x-systemd.device-timeout=60s 0 2
```

### 1.8 Verify daemon imports cleanly

```bash
sudo -u dinesh /opt/power-daemon/venv/bin/python -c \
  "import sys; sys.path.insert(0, '/opt/power-daemon'); import power_daemon; print('OK')"
```

Should print `OK`. If kasa import fails, re-run step 1.4.

### 1.9 Start the daemon

```bash
sudo systemctl daemon-reload
sudo systemctl enable power-daemon
sudo systemctl start power-daemon
sudo systemctl status power-daemon
```

Status should show `active (running)`.

### 1.10 Watch logs — verify it's pinging Zeb

```bash
sudo journalctl -u power-daemon -f
```

Within 60s you should see something like:

```
Power daemon started. Beacon=192.168.1.110 gateway=192.168.1.1 threshold=300s
```

If you see "Zeb offline" repeatedly while Zeb is actually up, the IP is
wrong — fix `/etc/default/power-daemon` and run
`sudo systemctl restart power-daemon`.

---

## Phase 2 — Home Assistant integration

### 2.1 Backup current HA config

```bash
docker exec homeassistant cp /config/configuration.yaml /config/configuration.yaml.bak
docker exec homeassistant cp /config/automations.yaml /config/automations.yaml.bak 2>/dev/null
```

### 2.2 Add `whatsapp_phone` to secrets.yaml

```bash
sudo nano /opt/homeassistant/config/secrets.yaml
```

Add (or append):

```yaml
whatsapp_phone: "919XXXXXXXXX"   # your WhatsApp number with country code
```

### 2.3 Update `configuration.yaml`

```bash
sudo nano /opt/homeassistant/config/configuration.yaml
```

At the end, add the two new blocks (only the **new** parts — keep your
existing config intact):

```yaml
input_datetime:
  esp32_last_seen:
    name: ESP32 Last Heartbeat
    has_date: true
    has_time: true

shell_command:
  whatsapp_alert: >-
    curl -s -X POST http://localhost:3001/send
    -H "Content-Type: application/json"
    -d '{"to": "{{ to }}", "message": "{{ message }}"}'
```

If you already have `input_datetime:` or `shell_command:` blocks, merge
the keys instead of duplicating the headers.

### 2.4 Append automations

```bash
sudo nano /opt/homeassistant/config/automations.yaml
```

Append the entire contents of `homeassitant/power_automation.yaml` to
the end of `automations.yaml`. The YAML list items are what matter —
comments are optional.

### 2.5 Validate HA config

```bash
docker exec homeassistant python3 -m homeassistant --script check_config -c /config
```

Look for `Configuration valid!`. If errors appear, they'll point to
the exact line — fix and re-validate.

### 2.6 Restart HA

```bash
docker restart homeassistant
sleep 30
docker logs homeassistant --tail 50
```

Confirm no errors about `webhook`, `input_datetime`, or
`shell_command`.

### 2.7 Test the WhatsApp action manually

In HA UI: **Developer Tools → Actions** → select
`shell_command.whatsapp_alert` → Service Data:

```yaml
to: !secret whatsapp_phone
message: "HA WhatsApp test"
```

Click **Call Service**. You should receive a WhatsApp message within
2 seconds.

### 2.8 Trigger each webhook manually to verify automations

```bash
curl -X POST http://localhost:8123/api/webhook/esp32_heartbeat -d '{}'
curl -X POST http://localhost:8123/api/webhook/power_cut_imminent -d '{}'
curl -X POST http://localhost:8123/api/webhook/tapo_command_failed -d '{}'
curl -X POST http://localhost:8123/api/webhook/esp32_power_restored -d '{}'
curl -X POST http://localhost:8123/api/webhook/esp32_mini_pc_stuck -d '{}'
```

You should receive four WhatsApp messages (heartbeat only updates the
timestamp). Verify the timestamp updated:

```bash
# Check via HA UI → Developer Tools → States → search esp32_last_seen
# OR via API:
curl -s http://localhost:8123/api/states/input_datetime.esp32_last_seen \
  -H "Authorization: Bearer <YOUR_LONG_LIVED_TOKEN>"
```

---

## Phase 3 — ESP32 firmware update

### 3.1 Edit credentials in main.py

The file at `tapo-power-automation/esp32/main.py` has placeholders.
Edit on your dev machine:

```bash
nano tapo-power-automation/esp32/main.py
```

Update the constants at the top:

```python
WIFI_SSID = "your_actual_ssid"
WIFI_PASSWORD = "your_actual_wifi_password"

TAPO_IP = "192.168.1.111"
TAPO_EMAIL = "your_tapo_email@example.com"
TAPO_PASSWORD = "your_tapo_password"

MINI_PC_IP = "192.168.1.50"
HA_BASE_URL = "http://192.168.1.50:8123"
```

### 3.2 Connect ESP32 via USB

```bash
ls /dev/tty.usb*           # macOS
# or
ls /dev/ttyUSB*            # Linux
```

Note the device path (e.g., `/dev/tty.usbserial-0001`).

### 3.3 Backup current main.py from ESP32 (safety)

```bash
mpremote connect /dev/tty.usbserial-0001 cp :main.py main_backup_$(date +%Y%m%d).py
```

### 3.4 Upload new main.py

```bash
mpremote connect /dev/tty.usbserial-0001 cp \
  tapo-power-automation/esp32/main.py :main.py
```

### 3.5 Reset ESP32 and watch logs

```bash
mpremote connect /dev/tty.usbserial-0001 reset
mpremote connect /dev/tty.usbserial-0001 repl
```

You should see:

```
Connecting to your_actual_ssid...
Connected: ('192.168.1.32', ...)
NTP sync OK ([2026-06-14 ...])
Boot delay: waiting 60s...
```

After 60s, the watchdog loop starts. Disconnect from REPL with `Ctrl+]`.

### 3.6 Verify heartbeat received

Wait ~10 seconds after boot delay, then check on Mini PC:

```bash
# In HA UI → Developer Tools → States → search esp32_last_seen
# Timestamp should be within the last minute
```

---

## Phase 4 — End-to-end power-cut test

### 4.1 Dry run — verify gateway sanity check

Power off Zeb SP110 in the Tapo app to simulate a Zeb-unreachable
state. In another terminal:

```bash
sudo journalctl -u power-daemon -f
```

Watch over the next 5 minutes:

```
Zeb offline 60s / 300s
Zeb offline 120s / 300s
...
Zeb offline 300s / 300s
Gateway 192.168.1.1 reachable → confirmed power cut
=== POWER FAILURE: starting shutdown sequence ===
```

**Stop the test before shutdown actually happens!** Run:

```bash
sudo systemctl restart power-daemon
```

Then turn Zeb back on in the Tapo app.

This proves the detection path works without committing to a full
shutdown.

### 4.2 Full live test — actual breaker trip

⚠️ **Only do this when you can afford 5–10 min downtime.**

1. **Trip the upstream breaker** that feeds the utility-line socket
   (where Zeb SP110 + ESP32 are plugged in).
2. Watch on phone:
   - Zeb SP110 should show "Offline" in Tapo app within 30s.
3. After ~5 min:
   - WhatsApp arrives: "🔌 Power cut detected. Mini PC shutting down..."
   - Mini PC shuts down (you can `ssh` in if you're fast).
4. ~60s later: Tapo P110 cuts power → HDD silent (no "krik" sound).
5. **Restore the breaker.**
6. Within ~3 min:
   - ESP32 boots, sees Mini PC down, turns on Tapo.
   - WhatsApp arrives: "✅ Power restored..."
7. Mini PC boots → daemon resumes.
8. SSH back in normally.

### 4.3 Post-test verification

```bash
sudo journalctl -u power-daemon -n 100
# Look for full sequence:
#   Step 1/7: Notifying HA webhook
#   Step 2/7: sync
#   ...
#   Step 7/7: shutdown -h now

sudo smartctl -A /dev/sda | grep -E "Power-Off_Retract|Power_Cycle"
# Power-Off_Retract should NOT have incremented
```

---

## Phase 5 — Long-term monitoring checks

### 5.1 Wait 1 hour, then check ESP32 heartbeat

In HA UI → Developer Tools → States → `input_datetime.esp32_last_seen`.
Timestamp should be within the last hour.

### 5.2 Test dead-ESP32 alert (optional)

- In HA UI → Developer Tools → States, manually set
  `input_datetime.esp32_last_seen` to 4 hours ago.
- Wait up to 30 min for the time_pattern trigger.
- WhatsApp should arrive: "⚠️ ESP32 watchdog dead...".
- Reset by triggering an actual heartbeat (`curl` in step 2.8).

### 5.3 Test Tapo unreachable alert (optional)

- Temporarily block Tapo via router/firewall.
- Power off Zeb to trigger threshold.
- Watch logs: "Tapo countdown attempt 1/2 failed".
- WhatsApp arrives: "⚠️ Tapo countdown command FAILED...".
- Restore Tapo network access.

---

## Phase 6 — Lock it in

### 6.1 Commit the working config

```bash
git add -A
git status              # review
git commit -m "Power automation: Zeb beacon + gateway sanity + hdparm + HA visibility"
```

### 6.2 Document what you did

In your homelab notes, record:

- Date of deployment
- IP assignments confirmed
- Test results from Phase 4
- Any deviations from this guide

### 6.3 Set a calendar reminder

- 1 week — spot-check journal logs for false positives
- 1 month — review HDD SMART data; verify heartbeats
- 3 months — full breaker-trip test again to ensure nothing regressed

---

## Rollback (if anything breaks badly)

```bash
# Mini PC daemon
sudo systemctl stop power-daemon
sudo cp /etc/fstab.bak /etc/fstab

# HA
docker exec homeassistant cp /config/configuration.yaml.bak /config/configuration.yaml
docker exec homeassistant cp /config/automations.yaml.bak /config/automations.yaml
docker restart homeassistant

# ESP32
mpremote connect /dev/tty.usbserial-0001 cp main_backup_YYYYMMDD.py :main.py
mpremote connect /dev/tty.usbserial-0001 reset
```

---

## Common issues & fixes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Daemon logs "Zeb offline" but Zeb is up | Wrong IP in env file | Fix `/etc/default/power-daemon`, `systemctl restart power-daemon` |
| WhatsApp not arriving for HA webhook | `secrets.yaml` missing `whatsapp_phone` | Add it, restart HA |
| `shell_command.whatsapp_alert` returns rc=1 | WhatsApp API not running on :3001 | `curl http://localhost:3001/health` to verify; start the API |
| ESP32 reboots randomly | WDT firing — code might be hanging | Check serial output via `mpremote repl` |
| `umount /mnt/storage` fails during shutdown | Docker still has files open | Non-fatal; daemon proceeds; `hdparm` still runs |
| Mini PC won't boot after Tapo restored | BIOS not set to "Restore on AC Loss" | Reboot, enter BIOS, set option, save |

---

That's the full setup. Take it one phase at a time — verify each before
moving to the next.
