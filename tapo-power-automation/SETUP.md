# Tapo Power Automation Setup

## Project Structure

```
tapo-power-automation/
├── minipc/
│   ├── power_daemon.py        # Power failure detection + shutdown daemon
│   ├── power-daemon.service   # systemd unit file
│   └── requirements.txt
└── esp32/
    └── main.py                # MicroPython boot script (Tapo ON on grid restore)
```

## Mini PC Setup

### 1. Install dependencies

```bash
sudo pip3 install -r minipc/requirements.txt
```

### 2. Configure

Edit `power_daemon.py` and update the `Config` dataclass:

```python
@dataclass
class Config:
    esp32_ip: str = "192.168.1.100"        # Your ESP32's static IP
    tapo_ip: str = "192.168.1.101"         # Your Tapo P110's IP
    tapo_email: str = "your_email"         # Tapo account email
    tapo_password: str = "your_password"   # Tapo account password
    ping_interval_sec: int = 60            # How often to ping ESP32
    failure_threshold_sec: int = 600       # 10 min before triggering shutdown
    tapo_countdown_sec: int = 120          # Tapo cuts power 120s after shutdown starts
```

### 3. Install as systemd service

```bash
sudo cp minipc/power_daemon.py /opt/power-daemon/power_daemon.py
sudo cp minipc/power-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable power-daemon
sudo systemctl start power-daemon
```

### 4. Allow passwordless shutdown

Add to `/etc/sudoers.d/power-daemon`:

```
your_username ALL=(ALL) NOPASSWD: /sbin/shutdown
```

### 5. BIOS configuration

Set: **Restore on AC Power Loss → Power On**

This ensures the Mini PC boots automatically when the Tapo plug restores power.

---

## ESP32 Setup

### 1. Flash MicroPython

Download the latest MicroPython firmware for ESP32 from https://micropython.org/download/esp32/

```bash
# Erase flash
esptool.py --chip esp32 erase_flash

# Flash MicroPython
esptool.py --chip esp32 write_flash -z 0x1000 esp32-xxx.bin
```

### 2. Configure

Edit `esp32/main.py` and update:

```python
WIFI_SSID = "Airtel_Dinesh_5GHz"
WIFI_PASSWORD = "your_wifi_password"
TAPO_IP = "192.168.1.101"
TAPO_EMAIL = "your_tapo_email@example.com"
TAPO_PASSWORD = "your_tapo_password"
BOOT_DELAY_SEC = 60
```

### 3. Upload to ESP32

Using `mpremote` or `ampy`:

```bash
pip install mpremote
mpremote connect /dev/ttyUSB0 cp esp32/main.py :main.py
```

### 4. Assign static IP to ESP32

In your router's DHCP settings, reserve a static IP for the ESP32's MAC address.

---

## Network Prerequisites

- Router must be on UPS (so WiFi stays up during outage)
- Static IPs assigned to: ESP32, Tapo P110, Mini PC
- All devices on the same subnet

---

## Operational Flow

```
Normal:     Mini PC pings ESP32 every 60s ✓
Grid fails: ESP32 dies → Mini PC starts 10-min countdown
10 min:     Mini PC → Tapo "cut power in 120s" → shutdown -h now
Grid back:  ESP32 boots → waits 60s → Tapo "turn ON" → Mini PC boots
```

---

## Testing

### Test the daemon (without actual shutdown)

Comment out the `initiate_shutdown()` call and `set_tapo_countdown()` in `power_daemon.py`, then unplug the ESP32 and watch the logs:

```bash
sudo journalctl -u power-daemon -f
```

### Test ESP32 → Tapo ON

Power cycle the ESP32 and confirm the Tapo plug turns on after 60s.

### Test full cycle

1. Unplug ESP32 from wall
2. Wait 10 minutes (watch daemon logs)
3. Verify Tapo countdown is set
4. Verify Mini PC shuts down
5. Verify Tapo cuts power after 120s
6. Plug ESP32 back in
7. Verify Tapo turns on → Mini PC boots
