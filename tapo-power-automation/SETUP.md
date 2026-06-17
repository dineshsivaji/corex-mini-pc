# Tapo Power Automation Setup

## Architecture (current)

```
┌─────────────────── UTILITY POWER LINE ──────────────────┐
│                                                          │
│   ESP32 (192.168.1.32)         WiFi router             │
│     - watchdog + Tapo control    (separate 2hr UPS)     │
│                                                          │
│   Zeb SP110 (192.168.1.110)                             │
│     - "power beacon" (Mini PC pings to detect outage)   │
│                                                          │
└──────────────────────────────────────────────────────────┘

┌─────────────────── UPS BATTERY-BACKED LINE ─────────────┐
│   (10 min runtime)                                       │
│                                                          │
│   Mini PC (192.168.1.50)                                │
│   Tapo P110 (192.168.1.111) — controls HDD power       │
│   3.5" HDD (USB-SATA via Tapo)                         │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**Why two power domains?** ESP32 must stay alive during outages so it
can detect grid restoration and turn the Tapo back on. The Mini PC and
HDD bridge the gap on UPS battery and are gracefully shut down if power
doesn't return within 5 min.

---

## Project Structure

```
tapo-power-automation/
├── minipc/
│   ├── power_daemon.py            # Mini PC: power-cut detection + shutdown
│   ├── power-daemon.service       # systemd unit
│   ├── power-daemon.env.example   # Env file template (copy to /etc/default/)
│   └── requirements.txt
└── esp32/
    └── main.py                    # MicroPython: KLAP v2 + watchdog + heartbeat
```

---

## Mini PC Setup

### 1. Install dependencies

```bash
sudo pip3 install -r minipc/requirements.txt
sudo apt install -y hdparm  # if not already installed
```

### 2. Install daemon files

```bash
sudo mkdir -p /opt/power-daemon
sudo cp minipc/power_daemon.py /opt/power-daemon/
sudo cp minipc/power-daemon.service /etc/systemd/system/
```

### 3. Configure environment

```bash
sudo cp minipc/power-daemon.env.example /etc/default/power-daemon
sudo chmod 600 /etc/default/power-daemon
sudo chown root:root /etc/default/power-daemon
sudo nano /etc/default/power-daemon   # fill in real Tapo credentials, IPs
```

The daemon reads all settings from this env file (overrides defaults
in `power_daemon.py`). Required keys are listed in
`power-daemon.env.example`.

### 4. Allow passwordless privileged commands

Create `/etc/sudoers.d/power-daemon` (use `sudo visudo -f`):

```
hgd469 ALL=(ALL) NOPASSWD: /sbin/shutdown
hgd469 ALL=(ALL) NOPASSWD: /sbin/hdparm
hgd469 ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop docker
hgd469 ALL=(ALL) NOPASSWD: /usr/bin/umount /mnt/storage
```

(Replace `hgd469` with the user the systemd unit runs as.)

### 5. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable power-daemon
sudo systemctl start power-daemon
sudo journalctl -u power-daemon -f
```

#### Boot-ordering note

The unit declares `After=docker.service wait-for-storage.service` and an
`ExecStartPre` that polls `localhost:8123` (Home Assistant) every 5s for
up to 5 minutes before launching the daemon. This guarantees the
recovery WhatsApp notification — which the daemon fires on every
startup — actually reaches a live HA endpoint instead of a half-booted
host.

Expect the first launch after a cold boot to take an extra ~30-90s
(while HA finishes coming up inside its container). Watch in real time:

```bash
sudo journalctl -u power-daemon -f
# Look for: "HA reachable after Xs" → daemon then enters its watchdog loop
```

If HA never becomes reachable within 5 minutes, the daemon proceeds
anyway (logs `HA never reachable in 300s, starting daemon anyway`) so
power monitoring is never permanently blocked by an HA outage.

### 6. BIOS configuration

Set: **Restore on AC Power Loss → Power On**

This ensures the Mini PC boots automatically when the Tapo plug
restores power.

### 7. fstab — non-blocking HDD mount

Add `nofail,x-systemd.device-timeout=60s` to the `/mnt/storage` entry
so a delayed-spin-up HDD doesn't hang boot:

```
UUID=xxxxx /mnt/storage ext4 defaults,nofail,x-systemd.device-timeout=60s 0 2
```

---

## Home Assistant Setup

The daemon and ESP32 fire webhooks at HA for visibility. See
`../homeassitant/README.md` for the configuration changes (input_datetime,
shell_command, six automations) and the required `secrets.yaml` entry
(`whatsapp_phone`).

---

## ESP32 Setup

### 1. Flash MicroPython

Download the latest MicroPython firmware for ESP32 from
https://micropython.org/download/esp32/

```bash
esptool.py --chip esp32 erase_flash
esptool.py --chip esp32 write_flash -z 0x1000 esp32-xxx.bin
```

### 2. Configure

Edit `esp32/main.py` and update the constants block at the top:

```python
WIFI_SSID = "your_ssid"
WIFI_PASSWORD = "your_wifi_password"
TAPO_IP = "192.168.1.111"
TAPO_EMAIL = "your_tapo_email@example.com"
TAPO_PASSWORD = "your_tapo_password"
MINI_PC_IP = "192.168.1.50"
HA_BASE_URL = "http://192.168.1.50:8123"
```

### 3. Upload to ESP32

```bash
pip install mpremote
mpremote connect /dev/ttyUSB0 cp esp32/main.py :main.py
mpremote connect /dev/ttyUSB0 reset
```

### 4. Assign static IP to ESP32

In your router's DHCP settings, reserve a static IP for the ESP32's
MAC address (planned: 192.168.1.32).

---

## Network Prerequisites

- WiFi router on its own UPS (so WiFi stays up during outage)
- Static IPs assigned to: ESP32, Zeb SP110, Tapo P110, Mini PC
- All devices on the same subnet
- Mini PC has line-of-sight to gateway (used for sanity check)

---

## Operational Flow

### Normal

```
Mini PC pings Zeb SP110 every 60s ✓
ESP32 pings Mini PC every 180s ✓
ESP32 sends hourly heartbeat to HA ✓
```

### Power cut

```
1. Zeb SP110 loses utility power → unreachable
2. Mini PC accumulates failures for 5 min
3. Mini PC pings gateway (sanity check):
   - gateway down → network issue, NOT power cut → daemon resets, keeps watching
   - gateway up   → confirmed outage → start shutdown sequence
4. Mini PC: HA webhook → WhatsApp "power cut detected"
5. Mini PC: sync → set Tapo countdown(60s, off) → systemctl stop docker
6. Mini PC: umount /mnt/storage → hdparm -Y /dev/sda → shutdown -h now
7. Tapo cuts power 60s after countdown set → HDD silently powers off
```

### Power restored

```
1. Zeb SP110 boots, becomes reachable
2. ESP32 still reachable (always-powered) sees Mini PC unreachable
3. ESP32 checks Tapo state → OFF → turns on Tapo
4. Mini PC boots (BIOS "Restore on AC Loss") → daemon resumes
5. ESP32: HA webhook → WhatsApp "power restored"
```

### Mini PC stuck

If Tapo is ON but Mini PC stays unreachable for >10 min, ESP32 fires
the `esp32_mini_pc_stuck` webhook → WhatsApp alert.

---

## Testing

### Component tests

```bash
# Tapo countdown (without shutdown)
python3 -c "
import asyncio
from minipc.power_daemon import set_tapo_countdown, Config
asyncio.run(set_tapo_countdown(Config(tapo_email='...', tapo_password='...')))
"

# hdparm dry run
sync && sudo hdparm -Y /dev/sda && sudo hdparm -C /dev/sda
# Expect: 'drive state is: standby'
```

### End-to-end (live test)

1. Unplug the Zeb SP110 from utility line (simulates power cut)
2. Watch logs: `sudo journalctl -u power-daemon -f`
3. Verify gateway sanity check passes (real outage detected)
4. Wait 5 min — Mini PC should:
   - Send WhatsApp via HA
   - Set Tapo countdown
   - Stop services
   - Park HDD (no "krik" sound)
   - Shut down
5. Tapo cuts power 60s later
6. Plug Zeb SP110 back in
7. Wait — ESP32 should turn Tapo back on, Mini PC boots
8. Verify second WhatsApp: "power restored"

### Failure injection

- Manually power off Zeb in app while gateway stays up → daemon resets
  counter (not a real outage signature; this exact path is not protected
  unless gateway also drops).
- Block Tapo network access → daemon retries, fires WhatsApp alert,
  proceeds with shutdown anyway.
- Stop HA mid-test → daemon webhook calls fail silently, shutdown still
  completes.
