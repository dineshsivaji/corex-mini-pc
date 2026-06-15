
⏺ End-to-End Setup Guide

  This walks you from the current /tmp/corex-mini-pc/ source tree to a fully deployed system. Run each step and verify before moving on.

  ---
  Phase 0 — Pre-flight checks

  0.1 Confirm IP addresses match your network

  Verify these match your actual setup. If any differ, fix them in the config files before proceeding:

  # What your network shows:
  ping -c 1 192.168.1.110   # Zeb SP110 should respond
  ping -c 1 192.168.1.111   # Tapo P110 should respond
  ping -c 1 192.168.1.32    # ESP32 should respond
  hostname -I               # Mini PC's own IP — should be 192.168.1.50

  0.2 Verify WhatsApp API is running

  curl -X POST http://localhost:3001/send \
    -H "Content-Type: application/json" \
    -d '{"to":"YOUR_PHONE","message":"setup test"}'

  You should receive a WhatsApp message. If not, fix the WhatsApp service first.

  0.3 Verify HDD device path

  lsblk
  # Confirm /dev/sda is your 3.5" HDD with /mnt/storage mountpoint

  If your HDD is on a different device (/dev/sdb, etc.), note it for step 2.4.

  ---
  Phase 1 — Mini PC daemon deployment

  1.1 Install dependencies

  sudo apt update
  sudo apt install -y hdparm python3-pip
  sudo pip3 install python-kasa

  1.2 Stop existing daemon (if running)

  sudo systemctl stop power-daemon 2>/dev/null
  sudo systemctl disable power-daemon 2>/dev/null

  1.3 Deploy daemon files

  sudo mkdir -p /opt/power-daemon
  sudo cp /tmp/corex-mini-pc/tapo-power-automation/minipc/power_daemon.py /opt/power-daemon/
  sudo cp /tmp/corex-mini-pc/tapo-power-automation/minipc/power-daemon.service /etc/systemd/system/
  sudo chmod 755 /opt/power-daemon/power_daemon.py

  1.4 Create env file with real credentials

  sudo cp /tmp/corex-mini-pc/tapo-power-automation/minipc/power-daemon.env.example /etc/default/power-daemon
  sudo chmod 600 /etc/default/power-daemon
  sudo chown root:root /etc/default/power-daemon
  sudo nano /etc/default/power-daemon

  In the editor, set real values:
  POWER_DAEMON_TAPO_EMAIL=your_actual_tapo_email@example.com
  POWER_DAEMON_TAPO_PASSWORD=your_actual_tapo_password
  POWER_DAEMON_HDD_DEVICE=/dev/sda            # adjust if needed
  POWER_DAEMON_STORAGE_MOUNT=/mnt/storage     # adjust if needed
  # Other defaults are fine

  1.5 Configure sudoers (passwordless privileged commands)

  sudo visudo -f /etc/sudoers.d/power-daemon

  Paste:
  dinesh ALL=(ALL) NOPASSWD: /sbin/shutdown
  dinesh ALL=(ALL) NOPASSWD: /sbin/hdparm
  dinesh ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop docker
  dinesh ALL=(ALL) NOPASSWD: /usr/bin/umount /mnt/storage

  (Save: Ctrl+O, Enter, Ctrl+X.)

  Verify it parsed correctly (visudo would have refused if syntax was bad).

  1.6 Update fstab for non-blocking HDD mount

  sudo cp /etc/fstab /etc/fstab.bak
  sudo nano /etc/fstab

  Find the /mnt/storage line and ensure options include nofail,x-systemd.device-timeout=60s:

  UUID=xxxx /mnt/storage ext4 defaults,nofail,x-systemd.device-timeout=60s 0 2

  1.7 Verify daemon imports cleanly

  sudo -u dinesh python3 -c "import sys; sys.path.insert(0, '/opt/power-daemon'); import power_daemon; print('OK')"

  Should print OK. If kasa import fails, run sudo pip3 install python-kasa again.

  1.8 Start the daemon

  sudo systemctl daemon-reload
  sudo systemctl enable power-daemon
  sudo systemctl start power-daemon
  sudo systemctl status power-daemon

  Status should show active (running).

  1.9 Watch logs — verify it's pinging Zeb

  sudo journalctl -u power-daemon -f

  You should see (within 60s):
  Power daemon started. Beacon=192.168.1.110 gateway=192.168.1.1 threshold=300s
  Zeb back online after Xs   # or no message if Zeb stays up

  If you see "Zeb offline" repeatedly while Zeb is actually up, the IP is wrong — fix /etc/default/power-daemon and sudo systemctl restart power-daemon.

  ---
  Phase 2 — Home Assistant integration

  2.1 Backup current HA config

  docker exec homeassistant cp /config/configuration.yaml /config/configuration.yaml.bak
  docker exec homeassistant cp /config/automations.yaml /config/automations.yaml.bak 2>/dev/null

  2.2 Add whatsapp_phone to secrets.yaml

  sudo nano /opt/homeassistant/config/secrets.yaml

  Add (or append):
  whatsapp_phone: "919XXXXXXXXX"   # your WhatsApp number with country code

  Save and exit.

  2.3 Update configuration.yaml

  sudo nano /opt/homeassistant/config/configuration.yaml

  At the end, add the two new blocks (only the new parts — keep your existing config intact):

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

  If you already have input_datetime: or shell_command: blocks, merge the keys instead of duplicating the headers.

  2.4 Append automations

  sudo nano /opt/homeassistant/config/automations.yaml

  Append the entire contents of /tmp/corex-mini-pc/homeassitant/power_automation.yaml to the end of automations.yaml. (Don't paste the comment lines starting with # if your existing file
  format is strict; the YAML list items are what matter.)

  2.5 Validate HA config

  docker exec homeassistant python3 -m homeassistant --script check_config -c /config

  Look for Configuration valid!. If errors appear, they'll point to the exact line — fix and re-validate.

  2.6 Restart HA

  docker restart homeassistant
  sleep 30
  docker logs homeassistant --tail 50

  No errors about webhook, input_datetime, or shell_command.

  2.7 Test the WhatsApp action manually

  In HA UI: Developer Tools → Actions → select shell_command.whatsapp_alert → Service Data:

  to: !secret whatsapp_phone
  message: "HA WhatsApp test"

  Click Call Service. You should receive a WhatsApp message within 2 seconds.

  2.8 Trigger each webhook manually to verify automations

  # From Mini PC (curl localhost since HA is on host network):
  curl -X POST http://localhost:8123/api/webhook/esp32_heartbeat -d '{}'
  curl -X POST http://localhost:8123/api/webhook/power_cut_imminent -d '{}'
  curl -X POST http://localhost:8123/api/webhook/tapo_command_failed -d '{}'
  curl -X POST http://localhost:8123/api/webhook/esp32_power_restored -d '{}'
  curl -X POST http://localhost:8123/api/webhook/esp32_mini_pc_stuck -d '{}'

  You should receive five WhatsApp messages (one for each — except heartbeat which only updates the timestamp). Verify with:

  # Check input_datetime got updated
  curl -s http://localhost:8123/api/states/input_datetime.esp32_last_seen \
    -H "Authorization: Bearer <YOUR_LONG_LIVED_TOKEN>"

  (Or check via HA UI → Developer Tools → States → search esp32_last_seen.)

  ---
  Phase 3 — ESP32 firmware update

  3.1 Edit credentials in main.py

  The file at /tmp/corex-mini-pc/tapo-power-automation/esp32/main.py has placeholders. Edit on your dev machine:

  nano /tmp/corex-mini-pc/tapo-power-automation/esp32/main.py

  Update the constants at the top:
  WIFI_SSID = "your_actual_ssid"
  WIFI_PASSWORD = "your_actual_wifi_password"

  TAPO_IP = "192.168.1.111"
  TAPO_EMAIL = "your_tapo_email@example.com"
  TAPO_PASSWORD = "your_tapo_password"

  MINI_PC_IP = "192.168.1.50"
  HA_BASE_URL = "http://192.168.1.50:8123"

  3.2 Connect ESP32 via USB

  ls /dev/tty.usb*           # macOS
  # or
  ls /dev/ttyUSB*            # Linux

  Note the device path (e.g., /dev/tty.usbserial-0001).

  3.3 Backup current main.py from ESP32 (safety)

  mpremote connect /dev/tty.usbserial-0001 cp :main.py main_backup_$(date +%Y%m%d).py

  3.4 Upload new main.py

  mpremote connect /dev/tty.usbserial-0001 cp /tmp/corex-mini-pc/tapo-power-automation/esp32/main.py :main.py

  3.5 Reset ESP32 and watch logs

  mpremote connect /dev/tty.usbserial-0001 reset
  mpremote connect /dev/tty.usbserial-0001 repl

  You should see:
  Connecting to your_actual_ssid...
  Connected: ('192.168.1.32', ...)
  NTP sync OK ([2026-06-14 ...])
  Boot delay: waiting 60s...

  After 60s, the watchdog loop starts. Disconnect from REPL with Ctrl+].

  3.6 Verify heartbeat received

  Wait ~10 seconds after boot delay, then check on Mini PC:

  # input_datetime.esp32_last_seen should show recent timestamp
  docker exec homeassistant grep esp32_last_seen /config/.storage/core.restore_state 2>/dev/null
  # or check via HA UI

  The timestamp should be within the last minute.

  ---
  Phase 4 — End-to-end power-cut test

  4.1 Dry run — verify gateway sanity check

  Block Zeb SP110 from network temporarily by powering it off in the Tapo app:

  sudo journalctl -u power-daemon -f

  Watch logs over the next minute:
  Zeb offline 60s / 300s
  Zeb offline 120s / 300s
  ...
  Zeb offline 300s / 300s
  Gateway 192.168.1.1 reachable → confirmed power cut
  === POWER FAILURE: starting shutdown sequence ===

  Stop the test before shutdown actually happens! Run:
  sudo systemctl restart power-daemon

  This proves the detection path works without committing to a full shutdown.

  4.2 Full live test — actual breaker trip

  ⚠️ Only do this when you can afford 5-10 min downtime.

  1. Trip the upstream breaker that feeds the utility-line socket (where Zeb SP110 + ESP32 are plugged in)
  2. Watch on phone:
    - Zeb SP110 should show "Offline" in Tapo app within 30s
  3. Mini PC daemon should detect after ~5 min:
    - You receive WhatsApp: "🔌 Power cut detected. Mini PC shutting down..."
    - Mini PC shuts down (you can ssh in if you're fast)
  4. ~60s later: Tapo P110 cuts power → HDD silent (no "krik")
  5. Restore the breaker
  6. Within ~3 min:
    - ESP32 boots, sees Mini PC down, turns on Tapo
    - You receive WhatsApp: "✅ Power restored..."
  7. Mini PC boots → daemon resumes
  8. You can SSH back in normally

  4.3 Post-test verification

  sudo journalctl -u power-daemon -n 100
  # Look for full sequence:
  #   Step 1/7: Notifying HA webhook
  #   Step 2/7: sync
  #   ...
  #   Step 7/7: shutdown -h now

  sudo smartctl -A /dev/sda | grep -E "Power-Off_Retract|Power_Cycle"
  # Power-Off_Retract should NOT have incremented (HDD parked cleanly)

  ---
  Phase 5 — Verify long-term monitoring

  5.1 Wait 1 hour, then check ESP32 heartbeat

  # Check input_datetime.esp32_last_seen via HA UI or:
  curl -s http://localhost:8123/api/states/input_datetime.esp32_last_seen \
    -H "Authorization: Bearer <TOKEN>" | python3 -m json.tool

  Timestamp should be within the last hour.

  5.2 Test dead-ESP32 alert (optional)

  To verify the alert fires, you can temporarily fast-forward by:
  - Manually setting input_datetime.esp32_last_seen to 4 hours ago via HA UI (Developer Tools → States)
  - Wait up to 30 min for the time_pattern trigger
  - WhatsApp should arrive: "⚠️ ESP32 watchdog dead..."

  After test, reset by triggering an actual ESP32 heartbeat.

  5.3 Test Tapo unreachable alert

  # Temporarily block Tapo from network (via router/firewall)
  # Then trigger threshold by powering off Zeb in app
  # Wait ~5 min
  # Watch logs: should see "Tapo countdown attempt 1/2 failed"
  # Should receive WhatsApp: "⚠️ Tapo countdown command FAILED..."

  ---
  Phase 6 — Lock it in

  6.1 Commit the working config

  cd /tmp/corex-mini-pc
  git add -A
  git status                # review
  git commit -m "Power automation: Zeb beacon + gateway sanity + hdparm + HA visibility"

  6.2 Document what you did

  In your homelab notes, record:
  - Date of deployment
  - IP assignments confirmed
  - Test results from Phase 4
  - Any deviations from this guide

  6.3 Set a calendar reminder

  - 1 week: spot-check journal logs for false positives
  - 1 month: review SMART data for HDD; verify heartbeats
  - 3 months: full breaker-trip test again to ensure nothing regressed

  ---
  Rollback (if anything breaks badly)

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

  ---
  Common issues & fixes

  ┌───────────────────────────────────────────┬──────────────────────────────────────┬───────────────────────────────────────────────────────────────┐
  │                  Symptom                  │             Likely cause             │                              Fix                              │
  ├───────────────────────────────────────────┼──────────────────────────────────────┼───────────────────────────────────────────────────────────────┤
  │ Daemon logs "Zeb offline" but Zeb is up   │ Wrong IP in env file                 │ Fix /etc/default/power-daemon, systemctl restart power-daemon │
  ├───────────────────────────────────────────┼──────────────────────────────────────┼───────────────────────────────────────────────────────────────┤
  │ WhatsApp not arriving for HA webhook      │ secrets.yaml missing whatsapp_phone  │ Add it, restart HA                                            │
  ├───────────────────────────────────────────┼──────────────────────────────────────┼───────────────────────────────────────────────────────────────┤
  │ shell_command.whatsapp_alert returns rc=1 │ WhatsApp API not running on :3001    │ curl http://localhost:3001/health to verify; start the API    │
  ├───────────────────────────────────────────┼──────────────────────────────────────┼───────────────────────────────────────────────────────────────┤
  │ ESP32 reboots randomly                    │ WDT firing — code might be hanging   │ Check serial output via mpremote repl                         │
  ├───────────────────────────────────────────┼──────────────────────────────────────┼───────────────────────────────────────────────────────────────┤
  │ umount /mnt/storage fails during shutdown │ Docker still has files open          │ This is non-fatal; daemon proceeds; hdparm still runs         │
  ├───────────────────────────────────────────┼──────────────────────────────────────┼───────────────────────────────────────────────────────────────┤
  │ Mini PC won't boot after Tapo restored    │ BIOS not set to "Restore on AC Loss" │ Reboot, enter BIOS, set option, save                          │
  └───────────────────────────────────────────┴──────────────────────────────────────┴───────────────────────────────────────────────────────────────┘

  ---
  That's the full setup. Take it one phase at a time — verify each before moving to the next. Ping me if anything goes sideways.

