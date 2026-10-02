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
TAPO_EMAIL = "007007dinesh@gmail.com"
TAPO_PASSWORD = "DEayyE3s2pr@cUbM"

MINI_PC_IP = "192.168.1.50"
MINI_PC_PORT = 2222

# Zeb SP110 — used as "is mains stable?" check before turning Tapo on.
# If unreachable, mains may be flickering; defer the turn-on attempt.
# We try multiple ports because Tuya plugs vary in what they expose.
ZEB_SP110_IP = "192.168.1.110"
ZEB_SP110_PORTS = (80, 6668)  # web admin, Tuya local protocol

HA_BASE_URL = "http://192.168.1.50:8123"
HA_HEARTBEAT_URL = HA_BASE_URL + "/api/webhook/esp32_heartbeat"
HA_MINI_PC_STUCK_URL = HA_BASE_URL + "/api/webhook/esp32_mini_pc_stuck"

BOOT_DELAY_SEC = 60
NORMAL_SLEEP_SEC = 60
DEBOUNCE_SLEEP_SEC = 30
POST_ACTION_SLEEP_SEC = 60
FAILURE_THRESHOLD = 3
WIFI_RETRY_INTERVAL = 10
WIFI_MAX_RETRIES = 30  # 5 minutes
HEARTBEAT_INTERVAL_SEC = 3600  # 1 hour
STUCK_ALERT_THRESHOLD_SEC = 600  # 10 min: Tapo ON + Mini PC down → boot failure
STUCK_RECOVER_SEC = 300
WDT_TIMEOUT_MS = 120000  # 2 minutes
NTP_RETRY_DELAY_SEC = 5
IST_OFFSET_SEC = 5 * 3600 + 30 * 60  # India Standard Time = UTC+5:30
WEBHOOK_TIMEOUT_SEC = 3
HALTED_MAX_MW = 4000         # TUNE from step 1
MAX_RECOVERY_CYCLES = 2      # per down-episode
CYCLE_OFF_SEC = 10

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


# Module-level WDT handle so cooperative_sleep can feed it.
_WDT = None


def cooperative_sleep(seconds):
    """
    Sleep in 1-second chunks, feeding the WDT each chunk.
    Prevents WDT timeout during long waits and keeps WebREPL responsive.
    Falls back to plain time.sleep if WDT not yet initialized.
    """
    if _WDT is None or seconds <= 1:
        time.sleep(seconds)
        return
    remaining = int(seconds)
    while remaining > 0:
        _WDT.feed()
        chunk = 1 if remaining > 1 else remaining
        time.sleep(chunk)
        remaining -= chunk


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


# --- HTTP / KLAP transport ---
#
# The KLAP encryption session is kept independently from the TCP connection.
# This is closer to python-kasa: the TP_SESSIONID + derived crypto state are
# reusable, while each HTTP request gets a fresh short-lived TCP socket.  The
# latter is intentional on MicroPython: it avoids reusing a half-closed/stale
# keep-alive socket after the Tapo has silently closed it.

KLAP_HTTP_TIMEOUT_SEC = 10
KLAP_RETRY_DELAY_SEC = 1
KLAP_DEFAULT_SESSION_SEC = 24 * 60 * 60
KLAP_SESSION_EXPIRY_BUFFER_SEC = 20 * 60

_KLAP_SESSION = None


def parse_http_response(raw):
    """Parse an HTTP response into (status, headers, body)."""
    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        raise OSError("HTTP response missing header terminator")

    header_part = raw[:header_end]
    body = raw[header_end + 4:]
    lines = header_part.split(b"\r\n")
    if not lines or len(lines[0].split(b" ")) < 2:
        raise OSError("Malformed HTTP status line")

    try:
        status_code = int(lines[0].split(b" ")[1])
    except (ValueError, IndexError):
        raise OSError("Invalid HTTP status code")

    headers = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        k, v = line.split(b":", 1)
        key = k.strip().lower()
        value = v.strip()
        if key in headers:
            headers[key] = headers[key] + b", " + value
        else:
            headers[key] = value

    return status_code, headers, body


def read_response(s):
    """Read one HTTP response robustly from a short-lived TCP connection."""
    response = b""

    # Read until the complete HTTP header is present.
    while b"\r\n\r\n" not in response:
        chunk = s.recv(1024)
        if not chunk:
            raise OSError("Tapo closed socket before HTTP headers")
        response += chunk
        if len(response) > 8192:
            raise OSError("HTTP headers too large")

    header_end = response.find(b"\r\n\r\n")
    headers_raw = response[:header_end]
    body = response[header_end + 4:]

    # KLAP normally uses Content-Length.  Keep a close-delimited fallback so
    # an otherwise valid response is not discarded merely because the device
    # omitted the header.
    content_length = None
    chunked = False
    for line in headers_raw.split(b"\r\n")[1:]:
        lower = line.lower()
        if lower.startswith(b"content-length:"):
            try:
                content_length = int(line.split(b":", 1)[1].strip())
            except (ValueError, IndexError):
                raise OSError("Invalid Content-Length")
        elif lower.startswith(b"transfer-encoding:") and b"chunked" in lower:
            chunked = True

    if chunked:
        # Tapo KLAP is not expected to use chunked encoding.  Do not attempt
        # to guess at framing because doing so could corrupt the next packet.
        raise OSError("Chunked HTTP response is unsupported")

    if content_length is not None:
        while len(body) < content_length:
            chunk = s.recv(1024)
            if not chunk:
                raise OSError("Tapo closed socket during HTTP body")
            body += chunk
        body = body[:content_length]
    else:
        # Requests below use Connection: close.  If there is no length, read
        # until the peer closes the socket.
        while True:
            chunk = s.recv(1024)
            if not chunk:
                break
            body += chunk

    return parse_http_response(headers_raw + b"\r\n\r\n" + body)


def http_post(tapo_ip, path, body=b"", headers=None):
    """POST one HTTP request using a fresh TCP connection."""
    if headers is None:
        headers = {}

    s = None
    try:
        s = socket.socket()
        s.settimeout(KLAP_HTTP_TIMEOUT_SEC)
        s.connect((tapo_ip, 80))

        req = "POST {} HTTP/1.1\r\nHost: {}\r\nContent-Length: {}\r\n".format(
            path, tapo_ip, len(body)
        )
        req += "Connection: close\r\n"
        for k, v in headers.items():
            req += "{}: {}\r\n".format(k, v)
        req += "\r\n"

        s.send(req.encode() + body)
        return read_response(s)
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _ticks_seconds():
    """Monotonic-ish seconds suitable for session expiry on MicroPython."""
    return time.ticks_ms() // 1000


def _session_expired(session):
    if session is None:
        return True
    return time.ticks_diff(session["expires_at"], time.ticks_ms()) <= 0


def _extract_cookie(headers, name):
    """Extract a named cookie from Set-Cookie without comma-splitting values."""
    raw = headers.get(b"set-cookie", b"")
    if not raw:
        return None

    # Multiple Set-Cookie values can be combined by our parser.  Searching for
    # the cookie name at a cookie boundary is safer than splitting on commas.
    marker = (name + "=").encode()
    start = raw.find(marker)
    while start >= 0:
        if start == 0 or raw[start - 1:start] in (b" ", b",", b";"):
            end = raw.find(b";", start)
            if end < 0:
                end = len(raw)
            value = raw[start:end].strip()
            if value:
                return value.decode()
        start = raw.find(marker, start + len(marker))
    return None


def _cookie_int(headers, name, default):
    value = _extract_cookie(headers, name)
    if value is None:
        return default
    try:
        return int(value.split("=", 1)[1])
    except (ValueError, IndexError):
        return default


def klap_reset_session(reason=None):
    """Invalidate KLAP state so the next request performs a fresh handshake."""
    global _KLAP_SESSION
    if reason:
        log("  KLAP session reset: {}".format(reason))
    _KLAP_SESSION = None


def klap_session(tapo_ip):
    """Establish and cache a KLAP v2 encryption session."""
    local_seed = os.urandom(16)

    try:
        # Handshake 1 is a standalone HTTP transaction.  The response also
        # supplies the TP_SESSIONID cookie used by handshake 2 and requests.
        status, headers, body = http_post(
            tapo_ip, "/app/handshake1", body=local_seed
        )
        if status != 200:
            log("  KLAP handshake1 HTTP {}".format(status))
            return None

        if len(body) < 48:
            log("  KLAP handshake1 response too short ({})".format(len(body)))
            return None

        cookie = _extract_cookie(headers, "TP_SESSIONID")
        if not cookie:
            log("  KLAP handshake1 missing TP_SESSIONID")
            return None

        remote_seed = body[:16]
        server_hash = body[16:48]

        expected_hash = sha256(local_seed + remote_seed + AUTH_HASH)
        if server_hash != expected_hash:
            log("  KLAP handshake1 server hash mismatch")
            return None

        # Kasa reads the TIMEOUT cookie and expires the local session early.
        timeout_sec = _cookie_int(headers, "TIMEOUT", KLAP_DEFAULT_SESSION_SEC)
        if timeout_sec <= KLAP_SESSION_EXPIRY_BUFFER_SEC:
            expiry_sec = max(60, timeout_sec)
        else:
            expiry_sec = timeout_sec - KLAP_SESSION_EXPIRY_BUFFER_SEC

        # Handshake 2 is another independent HTTP transaction.  Do not depend
        # on the handshake1 TCP socket remaining open.
        hs2_payload = sha256(remote_seed + local_seed + AUTH_HASH)
        status, _, _ = http_post(
            tapo_ip,
            "/app/handshake2",
            body=hs2_payload,
            headers={"Cookie": cookie},
        )
        if status != 200:
            log("  KLAP handshake2 HTTP {}".format(status))
            return None

        key = sha256(b"lsk" + local_seed + remote_seed + AUTH_HASH)[:16]
        full_iv = sha256(b"iv" + local_seed + remote_seed + AUTH_HASH)
        iv = full_iv[:12]
        seq = int.from_bytes(full_iv[-4:], "big", True)
        sig_key = sha256(b"ldk" + local_seed + remote_seed + AUTH_HASH)[:28]

        global _KLAP_SESSION
        _KLAP_SESSION = {
            "cookie": cookie,
            "key": key,
            "iv": iv,
            "sig_key": sig_key,
            "seq": seq,
            "expires_at": time.ticks_add(time.ticks_ms(), expiry_sec * 1000),
        }
        return _KLAP_SESSION

    except OSError as e:
        log("  KLAP handshake transport error: {}".format(e))
        klap_reset_session()
        return None
    except Exception as e:
        # Keep unexpected protocol/parser failures from killing the watchdog.
        log("  KLAP handshake error: {}".format(e))
        klap_reset_session()
        return None


def klap_get_session(tapo_ip):
    """Return a valid cached session, or create one."""
    if _session_expired(_KLAP_SESSION):
        if _KLAP_SESSION is not None:
            log("  KLAP session expired; performing fresh handshake")
        klap_reset_session()
        return klap_session(tapo_ip)
    return _KLAP_SESSION


def klap_request(tapo_ip, payload_dict):
    """Send one KLAP command using cached session state.

    Returns (status, decrypted_response).  A transport/protocol failure raises
    OSError so callers can invalidate the session and retry from scratch.
    """
    global _KLAP_SESSION

    session = klap_get_session(tapo_ip)
    if session is None:
        raise OSError("KLAP session unavailable")

    seq = session["seq"] + 1
    cmd = json.dumps(payload_dict).encode()

    pad_len = 16 - (len(cmd) % 16)
    padded = cmd + bytes([pad_len] * pad_len)
    seq_bytes = struct.pack(">l", seq)

    cipher = cryptolib.aes(session["key"], 2, session["iv"] + seq_bytes)
    ciphertext = cipher.encrypt(padded)
    signature = sha256(session["sig_key"] + seq_bytes + ciphertext)
    request_body = signature + ciphertext

    status, _, body = http_post(
        tapo_ip,
        "/app/request?seq={}".format(seq),
        body=request_body,
        headers={"Cookie": session["cookie"]},
    )

    # The sequence belongs to the current encryption session even when the
    # device returns an application/security error.
    session["seq"] = seq

    if status == 403:
        # python-kasa explicitly invalidates the KLAP handshake after 403 so
        # the next operation gets a new TP_SESSIONID and sequence state.
        klap_reset_session("device returned HTTP 403")
        return status, None

    if status != 200:
        return status, None

    if len(body) <= 32:
        raise OSError("KLAP response too short ({})".format(len(body)))

    try:
        resp_ciphertext = body[32:]
        decipher = cryptolib.aes(
            session["key"], 2, session["iv"] + seq_bytes
        )
        decrypted = decipher.decrypt(resp_ciphertext)
        if not decrypted:
            raise ValueError("empty decrypted response")
        pad = decrypted[-1]
        if pad < 1 or pad > 16 or pad > len(decrypted):
            raise ValueError("invalid PKCS#7 padding")
        response = json.loads(decrypted[:-pad])
    except Exception as e:
        raise OSError("KLAP response decrypt/JSON failed: {}".format(e))

    return status, response


# --- Tapo control ---

def check_tapo_state(tapo_ip):
    """Check if Tapo is ON. Returns True/False/None on failure."""
    # Keep the existing two-attempt behavior, but make each retry a genuinely
    # fresh KLAP session after any transport/protocol failure.
    for attempt in range(2):
        try:
            status, response = klap_request(
                tapo_ip, {"method": "get_device_info"}
            )
            if status == 200 and response:
                return response.get("result", {}).get("device_on", False)

            if status == 403:
                log("  KLAP get_device_info rejected (HTTP 403); retrying handshake")
            else:
                log("  KLAP get_device_info failed: HTTP {}".format(status))
        except OSError as e:
            log("  KLAP get_device_info transport error: {}".format(e))

        klap_reset_session("get_device_info failed")
        if attempt == 0:
            time.sleep(KLAP_RETRY_DELAY_SEC)

    return None


def tapo_query(tapo_ip, payload):
    """Send a KLAP command and return its decrypted response, or None on failure."""
    try:
        status, response = klap_request(tapo_ip, payload)
        if status == 200:
            return response
        log("  Tapo query failed: HTTP {}".format(status))
    except OSError as e:
        log("  Tapo query transport error: {}".format(e))

    klap_reset_session("tapo_query failed")
    return None


def tapo_draw_mw(tapo_ip):
    """Return P110 current power draw in mW, or None if unavailable."""
    response = tapo_query(tapo_ip, {"method": "get_energy_usage"})
    try:
        return response["result"]["current_power"]
    except (TypeError, KeyError):
        log("  Tapo energy response missing current_power: {}".format(response))
        return None


def turn_off_tapo(tapo_ip):
    """Turn off the Tapo plug. Returns True only when the command succeeds."""
    response = tapo_query(
        tapo_ip,
        {"method": "set_device_info", "params": {"device_on": False}},
    )
    if response is not None:
        log("  Tapo turned OFF for recovery cycle")
        return True
    log("  Failed to turn Tapo OFF for recovery cycle")
    return False


def turn_on_tapo(tapo_ip):
    """Turn on the Tapo plug. Returns True on success."""
    # Keep the original one-attempt contract.  The existing main loop already
    # performs three attempts with its 5-second delay.  A failed request here
    # invalidates the KLAP session so the next outer-loop attempt handshakes
    # from scratch, just like python-kasa does after a retryable failure.
    try:
        status, response = klap_request(
            tapo_ip,
            {"method": "set_device_info", "params": {"device_on": True}},
        )
        if status == 200:
            log("  Tapo turned ON!")
            return True

        if status == 403:
            log("  Turn ON rejected (HTTP 403); resetting KLAP session")
        else:
            log("  Turn ON failed: HTTP {}".format(status))
    except OSError as e:
        log("  Turn ON transport error: {}".format(e))

    klap_reset_session("turn_on_tapo failed")
    return False


def power_cycle_tapo(tapo_ip):
    """Power-cycle Tapo, leaving it ON if the recovery succeeds."""
    if not turn_off_tapo(tapo_ip):
        return False

    # The old KLAP session is no longer needed after changing power state.
    # Force a fresh handshake when we turn the plug back on.
    klap_reset_session("power-cycle: Tapo turned OFF")
    cooperative_sleep(CYCLE_OFF_SEC)

    for attempt in range(3):
        log("  Recovery power-on attempt {}/3...".format(attempt + 1))
        if turn_on_tapo(tapo_ip):
            return True
        if attempt < 2:
            cooperative_sleep(5)

    log("  Recovery power-cycle could not turn Tapo back ON")
    return False


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

    # Start WebREPL — gives browser-based access to live print() output
    # and a remote REPL. Password is read from /webrepl_cfg.py
    # (created by `import webrepl_setup` from USB).
    try:
        import webrepl
        webrepl.start()
        log("WebREPL started — connect from http://micropython.org/webrepl/")
    except Exception as e:
        log("WebREPL not started: {}".format(e))

    log("Boot delay: waiting {}s (grid flicker guard)...".format(BOOT_DELAY_SEC))
    time.sleep(BOOT_DELAY_SEC)

    # Verify WiFi survived the delay
    wlan = network.WLAN(network.STA_IF)
    if not wlan.isconnected():
        log("WiFi lost during boot delay. Resetting...")
        machine.reset()

    # Hardware watchdog: auto-reset if loop hangs > WDT_TIMEOUT_MS
    wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)
    global _WDT
    _WDT = wdt

    log("Entering watchdog loop...")
    consecutive_failures = 0
    last_heartbeat = 0
    stuck_since = 0  # timestamp when "Tapo on but Mini PC down" first observed
    stuck_alerted = False
    recovery_cycles = 0  # power-cycles performed during the current down episode

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
            recovery_cycles = 0
            cooperative_sleep(NORMAL_SLEEP_SEC)
            continue

        consecutive_failures += 1
        log("Mini PC unreachable ({}/{})".format(
            consecutive_failures, FAILURE_THRESHOLD))

        if consecutive_failures < FAILURE_THRESHOLD:
            cooperative_sleep(DEBOUNCE_SLEEP_SEC)
            continue

        # Threshold reached — check Tapo state
        log("Checking Tapo state...")
        tapo_on = check_tapo_state(TAPO_IP)

        if tapo_on is True:
            # Tapo is ON while the Mini PC is unreachable. Initially this may
            # simply be a reboot/boot sequence. After STUCK_RECOVER_SEC, use
            # the P110 live wattage to distinguish a low-power halted PC from
            # a normally booting/running machine. Only the low-power case is
            # allowed to trigger a hard power-cycle.
            now = time.time()
            if stuck_since == 0:
                stuck_since = now
                log("Tapo is ON but Mini PC down — assume rebooting; tracking.")

            stuck_for = now - stuck_since

            if (stuck_for >= STUCK_ALERT_THRESHOLD_SEC and not stuck_alerted):
                log("Mini PC stuck for {}s while Tapo ON → alerting HA".format(
                    int(stuck_for)))
                fire_webhook(HA_MINI_PC_STUCK_URL, {
                    "stuck_for_sec": int(stuck_for),
                })
                stuck_alerted = True

            if (stuck_for >= STUCK_RECOVER_SEC and
                    recovery_cycles < MAX_RECOVERY_CYCLES):
                mw = tapo_draw_mw(TAPO_IP)
                log("Tapo draw: {} mW (halt threshold: {} mW)".format(
                    mw, HALTED_MAX_MW))

                if mw is not None and mw < HALTED_MAX_MW:
                    recovery_cycles += 1
                    log("PC looks halted ({} mW) → power-cycling Tapo "
                        "(cycle {}/{})".format(
                            mw, recovery_cycles, MAX_RECOVERY_CYCLES))
                    if not power_cycle_tapo(TAPO_IP):
                        log("Recovery power-cycle failed; will retry on next cycle")
                    else:
                        log("Recovery power-cycle complete; waiting for Mini PC boot")
                    # Give the PSU/BIOS a full boot window before measuring again.
                    consecutive_failures = 0
                    stuck_since = 0
                    stuck_alerted = False
                    cooperative_sleep(NORMAL_SLEEP_SEC)
                    continue
                elif mw is None:
                    log("Could not read Tapo power draw; no recovery cycle performed")
                else:
                    log("PC draw is above halted threshold; no power-cycle")

            if recovery_cycles >= MAX_RECOVERY_CYCLES:
                log("Maximum recovery cycles reached for this down episode; "
                    "leaving Tapo ON")

            consecutive_failures = 0
            cooperative_sleep(NORMAL_SLEEP_SEC)
            continue

        if tapo_on is None:
            if consecutive_failures > FAILURE_THRESHOLD + 5:
                log("Could not reach Tapo after multiple tries. Backing off...")
                consecutive_failures = 0
                cooperative_sleep(NORMAL_SLEEP_SEC)
            else:
                log("Could not reach Tapo, will retry...")
                cooperative_sleep(DEBOUNCE_SLEEP_SEC)
            continue

        # Tapo is OFF + Mini PC is down → confirm mains is stable before powering on
        log("Tapo is OFF. Verifying Zeb {} is reachable...".format(ZEB_SP110_IP))
        if not check_zeb_alive():
            log("Zeb unreachable — mains may be unstable / just restored. "
                "Holding off Tapo turn-on; will retry next cycle.")
            consecutive_failures = 0
            cooperative_sleep(NORMAL_SLEEP_SEC)
            continue
        log("Zeb is reachable → mains stable.")

        log("Mini PC down + Tapo OFF → turning ON Tapo")
        for attempt in range(3):
            log("  Attempt {}/3...".format(attempt + 1))
            if turn_on_tapo(TAPO_IP):
                break
            time.sleep(5)
        else:
            log("  All attempts failed")

        # Note: no HA notification fired here. Mini PC + HA are still booting,
        # so the webhook would silently fail. The Mini PC's power-daemon sends
        # the recovery notification once it comes up.

        # Reset counter and give Mini PC time to boot
        consecutive_failures = 0
        stuck_since = 0
        stuck_alerted = False
        log("Waiting for Mini PC to boot...")
        cooperative_sleep(NORMAL_SLEEP_SEC)


main()

