"""
ESP32 MicroPython watchdog for power automation.

Continuously monitors the Mini PC via TCP connect (port 22).
If the Mini PC is unreachable AND the Tapo plug is OFF, turns on the Tapo.
Handles WiFi disconnections with blocking retry + machine reset.

KLAP v2 protocol for Tapo P110 communication (single TCP connection required).

Reliability features:
- Hardware watchdog (machine.WDT) auto-resets on hang.
- Tighter exception handling (OSError only, not bare except).
- NTP-synced wall-clock timestamps in logs.
- Direct tuple connect (skip DNS lookup for local IPs).

Visibility (HA webhooks):
- Hourly heartbeat → input_datetime.esp32_last_seen.
- Notification on Tapo turn-on (power restored).
- Notification when Mini PC stays down despite Tapo being ON for >10 min
  (boot failure).
"""

import network
import time
import socket
import hashlib
import json
import os
import machine
import struct
import cryptolib
import ntptime
import urequests

# --- Configuration ---
WIFI_SSID = "wifi_ssd"
WIFI_PASSWORD = "wifi_pass"

TAPO_IP = "192.168.1.110"
TAPO_EMAIL = "email"
TAPO_PASSWORD = "pass"

MINI_PC_IP = "192.168.1.50"
MINI_PC_PORT = 22

# Zeb SP110 — used as "is mains stable?" check before turning Tapo on.
# If unreachable, mains may be flickering; defer the turn-on attempt.
# We try multiple ports because Tuya plugs vary in what they expose.
ZEB_SP110_IP = "192.168.1.110"
ZEB_SP110_PORTS = (80, 6668)  # web admin, Tuya local protocol

HA_BASE_URL = "http://192.168.1.50:8123"
HA_HEARTBEAT_URL = HA_BASE_URL + "/api/webhook/esp32_heartbeat"
HA_POWER_RESTORED_URL = HA_BASE_URL + "/api/webhook/esp32_power_restored"
HA_MINI_PC_STUCK_URL = HA_BASE_URL + "/api/webhook/esp32_mini_pc_stuck"

BOOT_DELAY_SEC = 60
NORMAL_SLEEP_SEC = 180
DEBOUNCE_SLEEP_SEC = 30
POST_ACTION_SLEEP_SEC = 60
FAILURE_THRESHOLD = 3
WIFI_RETRY_INTERVAL = 10
WIFI_MAX_RETRIES = 30  # 5 minutes
HEARTBEAT_INTERVAL_SEC = 3600  # 1 hour
STUCK_ALERT_THRESHOLD_SEC = 600  # 10 min: Tapo ON + Mini PC down → boot failure
WDT_TIMEOUT_MS = 120000  # 2 minutes
NTP_RETRY_DELAY_SEC = 5
IST_OFFSET_SEC = 5 * 3600 + 30 * 60  # India Standard Time = UTC+5:30
WEBHOOK_TIMEOUT_SEC = 3

# Pre-computed auth hash (constant for given credentials)
AUTH_HASH = hashlib.sha256(
    hashlib.sha1(TAPO_EMAIL.encode()).digest() +
    hashlib.sha1(TAPO_PASSWORD.encode()).digest()
).digest()


# --- Logging ---

def _format_ts():
    """Return [YYYY-MM-DD HH:MM:SS IST] timestamp string."""
    try:
        t = time.localtime(time.time() + IST_OFFSET_SEC)
        return "[{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d} IST]".format(
            t[0], t[1], t[2], t[3], t[4], t[5]
        )
    except Exception:
        return "[ts?]"


def log(msg):
    print(_format_ts(), msg)


# --- WiFi ---

def ensure_wifi():
    """Block until WiFi is connected. Reset after 5 minutes of failure."""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        return

    log("Connecting to {}...".format(WIFI_SSID))
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    retries = 0
    while not wlan.isconnected():
        time.sleep(WIFI_RETRY_INTERVAL)
        retries += 1
        if retries >= WIFI_MAX_RETRIES:
            log("WiFi failed for 5 minutes. Resetting...")
            machine.reset()

    log("Connected: {}".format(wlan.ifconfig()))


def sync_ntp():
    """Sync RTC via NTP. Best-effort; logs and returns on failure."""
    for attempt in range(3):
        try:
            ntptime.settime()
            log("NTP sync OK ({})".format(_format_ts()))
            return
        except OSError as e:
            log("NTP attempt {}/3 failed: {}".format(attempt + 1, e))
            time.sleep(NTP_RETRY_DELAY_SEC)
    log("NTP sync gave up; using boot-relative time")


# --- Host check ---

def check_host(ip, port=22, timeout=3):
    """TCP connect check. Returns True if host is reachable. No DNS lookup."""
    s = None
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def check_zeb_alive():
    """
    Returns True if Zeb SP110 responds on any of ZEB_SP110_PORTS.
    Used as a "mains is stable" indicator before turning Tapo back on.
    """
    for port in ZEB_SP110_PORTS:
        if check_host(ZEB_SP110_IP, port, timeout=3):
            return True
    return False


# --- Crypto helpers ---

def sha256(data):
    return hashlib.sha256(data).digest()


# --- HTTP over persistent socket ---

def parse_http_response(raw):
    """Parse HTTP response into status code, headers dict, and body."""
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        return 0, {}, b""

    header_part = raw[:header_end]
    body = raw[header_end + 4:]

    lines = header_part.split(b"\r\n")
    status_code = int(lines[0].split(b" ")[1])

    headers = {}
    for line in lines[1:]:
        if b": " in line:
            k, v = line.split(b": ", 1)
            key = k.lower()
            if key in headers:
                headers[key] = headers[key] + b", " + v
            else:
                headers[key] = v

    return status_code, headers, body


def read_response(s):
    """Read HTTP response from a keep-alive socket."""
    response = b""

    while b"\r\n\r\n" not in response:
        chunk = s.recv(1024)
        if not chunk:
            break
        response += chunk

    header_end = response.find(b"\r\n\r\n")
    if header_end == -1:
        return 0, {}, b""

    headers_raw = response[:header_end]
    body = response[header_end + 4:]

    cl = 0
    for line in headers_raw.split(b"\r\n"):
        if line.lower().startswith(b"content-length"):
            cl = int(line.split(b": ")[1])
            break

    while len(body) < cl:
        chunk = s.recv(1024)
        if not chunk:
            break
        body += chunk

    return parse_http_response(headers_raw + b"\r\n\r\n" + body[:cl])


def send_on_socket(s, host, path, body=b"", headers=None):
    """Send POST on existing socket, return (status, headers, body)."""
    if headers is None:
        headers = {}

    req = "POST {} HTTP/1.1\r\nHost: {}\r\nContent-Length: {}\r\n".format(
        path, host, len(body)
    )
    for k, v in headers.items():
        req += "{}: {}\r\n".format(k, v)
    req += "\r\n"

    s.send(req.encode() + body)
    return read_response(s)


# --- KLAP v2 session ---

def klap_session(tapo_ip):
    """
    Establish a KLAP v2 session with the Tapo device.
    Returns (socket, cookie, key, iv, sig_key, seq) or None on failure.
    """
    local_seed = os.urandom(16)

    s = socket.socket()
    s.settimeout(10)

    try:
        s.connect((tapo_ip, 80))

        # Handshake 1
        status, headers, body = send_on_socket(s, tapo_ip, "/app/handshake1", body=local_seed)
        if status != 200:
            s.close()
            return None

        # Extract TP_SESSIONID cookie
        cookie = ""
        cookie_header = headers.get(b"set-cookie", b"")
        if cookie_header:
            for part in cookie_header.decode().split(","):
                part = part.strip()
                if "TP_SESSIONID" in part:
                    cookie = part.split(";")[0]
                    break

        if not cookie or len(body) < 48:
            s.close()
            return None

        remote_seed = body[:16]
        server_hash = body[16:48]

        # Verify server hash
        if server_hash != sha256(local_seed + remote_seed + AUTH_HASH):
            s.close()
            return None

        # Handshake 2
        hs2_payload = sha256(remote_seed + local_seed + AUTH_HASH)
        status, headers, body = send_on_socket(
            s, tapo_ip, "/app/handshake2",
            body=hs2_payload,
            headers={"Cookie": cookie},
        )
        if status != 200:
            s.close()
            return None

        # Derive keys
        key = sha256(b"lsk" + local_seed + remote_seed + AUTH_HASH)[:16]
        full_iv = sha256(b"iv" + local_seed + remote_seed + AUTH_HASH)
        iv = full_iv[:12]
        seq = int.from_bytes(full_iv[-4:], "big", True)
        sig_key = sha256(b"ldk" + local_seed + remote_seed + AUTH_HASH)[:28]

        return s, cookie, key, iv, sig_key, seq

    except OSError:
        try:
            s.close()
        except OSError:
            pass
        return None


def klap_request(s, tapo_ip, cookie, key, iv, sig_key, seq, payload_dict):
    """Encrypt and send a KLAP command. Returns (seq, status, decrypted_response)."""
    seq += 1
    cmd = json.dumps(payload_dict).encode()

    pad_len = 16 - (len(cmd) % 16)
    padded = cmd + bytes([pad_len] * pad_len)

    seq_bytes = struct.pack(">l", seq)
    cipher = cryptolib.aes(key, 2, iv + seq_bytes)
    ciphertext = cipher.encrypt(padded)

    signature = sha256(sig_key + seq_bytes + ciphertext)
    request_body = signature + ciphertext

    status, headers, body = send_on_socket(
        s, tapo_ip, "/app/request?seq={}".format(seq),
        body=request_body,
        headers={"Cookie": cookie},
    )

    # Decrypt response if present
    response = None
    if status == 200 and len(body) > 32:
        resp_ciphertext = body[32:]
        decipher = cryptolib.aes(key, 2, iv + seq_bytes)
        decrypted = decipher.decrypt(resp_ciphertext)
        pad = decrypted[-1]
        response = json.loads(decrypted[:-pad])

    return seq, status, response


# --- Tapo control ---

def check_tapo_state(tapo_ip):
    """Check if Tapo is ON. Returns True/False/None (on error). Retries once."""
    for _ in range(2):
        session = klap_session(tapo_ip)
        if not session:
            time.sleep(3)
            continue

        s, cookie, key, iv, sig_key, seq = session
        try:
            seq, status, response = klap_request(
                s, tapo_ip, cookie, key, iv, sig_key, seq,
                {"method": "get_device_info"}
            )
            if status == 200 and response:
                return response.get("result", {}).get("device_on", False)
        except OSError as e:
            log("  KLAP get_device_info OSError: {}".format(e))
        finally:
            try:
                s.close()
            except OSError:
                pass
        time.sleep(3)

    return None


def turn_on_tapo(tapo_ip):
    """Turn on the Tapo plug. Returns True on success."""
    session = klap_session(tapo_ip)
    if not session:
        log("  KLAP session failed")
        return False

    s, cookie, key, iv, sig_key, seq = session
    try:
        seq, status, response = klap_request(
            s, tapo_ip, cookie, key, iv, sig_key, seq,
            {"method": "set_device_info", "params": {"device_on": True}}
        )
        log ("status : {}".format(status))
        log("response: {}".format(response))
        if status == 200:
            log("  Tapo turned ON!")
            return True
        log("  Turn ON failed: HTTP {}".format(status))
        return False
    except OSError as e:
        log("  Turn ON OSError: {}".format(e))
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


# --- HA webhooks (fire-and-forget) ---

def fire_webhook(url, payload):
    """POST JSON to HA webhook. Best-effort; never raises."""
    r = None
    try:
        r = urequests.post(
            url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=WEBHOOK_TIMEOUT_SEC,
        )
        return 200 <= r.status_code < 300
    except Exception as e:
        log("  Webhook {} failed: {}".format(url, e))
        return False
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def send_heartbeat():
    """Hourly heartbeat to HA. Includes uptime and WiFi RSSI."""
    try:
        wlan = network.WLAN(network.STA_IF)
        rssi = wlan.status("rssi") if wlan.isconnected() else None
    except Exception:
        rssi = None

    fire_webhook(
        HA_HEARTBEAT_URL,
        {"uptime_sec": time.time(), "wifi_rssi": rssi},
    )


# --- Main watchdog loop ---

def main():
    ensure_wifi()
    sync_ntp()

    log("Boot delay: waiting {}s (grid flicker guard)...".format(BOOT_DELAY_SEC))
    time.sleep(BOOT_DELAY_SEC)

    # Verify WiFi survived the delay
    wlan = network.WLAN(network.STA_IF)
    if not wlan.isconnected():
        log("WiFi lost during boot delay. Resetting...")
        machine.reset()

    # Hardware watchdog: auto-reset if loop hangs > WDT_TIMEOUT_MS
    wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)

    log("Entering watchdog loop...")
    consecutive_failures = 0
    last_heartbeat = 0
    stuck_since = 0  # timestamp when "Tapo on but Mini PC down" first observed
    stuck_alerted = False

    # Send initial heartbeat
    send_heartbeat()
    last_heartbeat = time.time()

    while True:
        wdt.feed()

        # Hourly heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            send_heartbeat()
            last_heartbeat = time.time()

        # Ensure WiFi is up
        if not network.WLAN(network.STA_IF).isconnected():
            log("WiFi disconnected. Reconnecting...")
            ensure_wifi()

        # Check Mini PC
        if check_host(MINI_PC_IP, MINI_PC_PORT):
            if consecutive_failures > 0:
                log("Mini PC back online (was down for {} checks)".format(
                    consecutive_failures))
            else:
                log("Mini PC is UP")
            consecutive_failures = 0
            stuck_since = 0
            stuck_alerted = False
            time.sleep(NORMAL_SLEEP_SEC)
            continue

        consecutive_failures += 1
        log("Mini PC unreachable ({}/{})".format(
            consecutive_failures, FAILURE_THRESHOLD))

        if consecutive_failures < FAILURE_THRESHOLD:
            time.sleep(DEBOUNCE_SLEEP_SEC)
            continue

        # Threshold reached — check Tapo state
        log("Checking Tapo state...")
        tapo_on = check_tapo_state(TAPO_IP)

        if tapo_on is True:
            # Smart detection: Tapo is ON, so Mini PC is rebooting / hung,
            # not powered off. Don't turn anything on.
            now = time.time()
            if stuck_since == 0:
                stuck_since = now
                log("Tapo is ON but Mini PC down — assume rebooting; tracking.")
            elif (now - stuck_since) >= STUCK_ALERT_THRESHOLD_SEC and not stuck_alerted:
                log("Mini PC stuck for {}s while Tapo ON → alerting HA".format(
                    int(now - stuck_since)))
                fire_webhook(HA_MINI_PC_STUCK_URL, {
                    "stuck_for_sec": int(now - stuck_since),
                })
                stuck_alerted = True
            consecutive_failures = 0
            time.sleep(NORMAL_SLEEP_SEC)
            continue

        if tapo_on is None:
            if consecutive_failures > FAILURE_THRESHOLD + 5:
                log("Could not reach Tapo after multiple tries. Backing off...")
                consecutive_failures = 0
                time.sleep(NORMAL_SLEEP_SEC)
            else:
                log("Could not reach Tapo, will retry...")
                time.sleep(DEBOUNCE_SLEEP_SEC)
            continue

        # Tapo is OFF + Mini PC is down → confirm mains is stable before powering on
        log("Tapo is OFF. Verifying Zeb {} is reachable...".format(ZEB_SP110_IP))
        if not check_zeb_alive():
            log("Zeb unreachable — mains may be unstable / just restored. "
                "Holding off Tapo turn-on; will retry next cycle.")
            consecutive_failures = 0
            time.sleep(NORMAL_SLEEP_SEC)
            continue
        log("Zeb is reachable → mains stable.")

        log("Mini PC down + Tapo OFF → turning ON Tapo")
        turn_on_success = False
        for attempt in range(10):
            log("  Attempt {}/10...".format(attempt + 1))
            if turn_on_tapo(TAPO_IP):
                turn_on_success = True
                break
            time.sleep(5)
        else:
            log("  All attempts failed")

        if turn_on_success:
            fire_webhook(HA_POWER_RESTORED_URL, {"action": "tapo_turned_on"})

        # Reset counter and give Mini PC time to boot
        consecutive_failures = 0
        stuck_since = 0
        stuck_alerted = False
        log("Waiting for Mini PC to boot...")
        time.sleep(NORMAL_SLEEP_SEC)


main()
