"""
ESP32 MicroPython watchdog for power automation.

Continuously monitors the Mini PC via TCP connect (port 22).
If the Mini PC is unreachable AND the Tapo plug is OFF, turns on the Tapo.
Handles WiFi disconnections with blocking retry + machine reset.

KLAP v2 protocol for Tapo P110 communication (single TCP connection required).
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

# --- Configuration ---
WIFI_SSID = "Airtel_Dinesh"
WIFI_PASSWORD = "Anjaney@4"

TAPO_IP = "192.168.1.110"
TAPO_EMAIL = "007007dinesh@gmail.com"
TAPO_PASSWORD = "DEayyE3s2pr@cUbM"

MINI_PC_IP = "192.168.1.50"
MINI_PC_PORT = 22

BOOT_DELAY_SEC = 60
NORMAL_SLEEP_SEC = 180
DEBOUNCE_SLEEP_SEC = 30
POST_ACTION_SLEEP_SEC = 60
FAILURE_THRESHOLD = 3
WIFI_RETRY_INTERVAL = 10
WIFI_MAX_RETRIES = 30  # 5 minutes

# Pre-computed auth hash (constant for given credentials)
AUTH_HASH = hashlib.sha256(
    hashlib.sha1(TAPO_EMAIL.encode()).digest() +
    hashlib.sha1(TAPO_PASSWORD.encode()).digest()
).digest()


# --- WiFi ---

def ensure_wifi():
    """Block until WiFi is connected. Reset after 5 minutes of failure."""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        return

    print(f"Connecting to {WIFI_SSID}...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    retries = 0
    while not wlan.isconnected():
        time.sleep(WIFI_RETRY_INTERVAL)
        retries += 1
        if retries >= WIFI_MAX_RETRIES:
            print("WiFi failed for 5 minutes. Resetting...")
            machine.reset()

    print(f"Connected: {wlan.ifconfig()}")


# --- Host check ---

def check_host(ip, port=22, timeout=3):
    """TCP connect check. Returns True if host is reachable."""
    try:
        addr = socket.getaddrinfo(ip, port)[0][-1]
        s = socket.socket()
        s.settimeout(timeout)
        s.connect(addr)
        s.close()
        return True
    except:
        try:
            s.close()
        except:
            pass
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

    req = f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(body)}\r\n"
    for k, v in headers.items():
        req += f"{k}: {v}\r\n"
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

    addr = socket.getaddrinfo(tapo_ip, 80)[0][-1]
    s = socket.socket()
    s.settimeout(10)

    try:
        s.connect(addr)

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

    except:
        s.close()
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
        s, tapo_ip, f"/app/request?seq={seq}",
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
        finally:
            s.close()
        time.sleep(3)

    return None


def turn_on_tapo(tapo_ip):
    """Turn on the Tapo plug. Returns True on success."""
    session = klap_session(tapo_ip)
    if not session:
        print("  KLAP session failed")
        return False

    s, cookie, key, iv, sig_key, seq = session
    try:
        seq, status, response = klap_request(
            s, tapo_ip, cookie, key, iv, sig_key, seq,
            {"method": "set_device_info", "params": {"device_on": True}}
        )
        if status == 200:
            print("  Tapo turned ON!")
            return True
        else:
            print(f"  Turn ON failed: HTTP {status}")
            return False
    finally:
        s.close()


# --- Main watchdog loop ---

def main():
    ensure_wifi()

    print(f"Boot delay: waiting {BOOT_DELAY_SEC}s (grid flicker guard)...")
    time.sleep(BOOT_DELAY_SEC)

    # Verify WiFi survived the delay
    wlan = network.WLAN(network.STA_IF)
    if not wlan.isconnected():
        print("WiFi lost during boot delay. Resetting...")
        machine.reset()

    print("Entering watchdog loop...")
    consecutive_failures = 0

    while True:
        # Ensure WiFi is up
        if not network.WLAN(network.STA_IF).isconnected():
            print("WiFi disconnected. Reconnecting...")
            ensure_wifi()

        # Check Mini PC
        if check_host(MINI_PC_IP, MINI_PC_PORT):
            if consecutive_failures > 0:
                print(f"Mini PC back online (was down for {consecutive_failures} checks)")
            else:
                print("Mini PC is UP")
            consecutive_failures = 0
            time.sleep(NORMAL_SLEEP_SEC)

        else:
            consecutive_failures += 1
            print(f"Mini PC unreachable ({consecutive_failures}/{FAILURE_THRESHOLD})")

            if consecutive_failures < FAILURE_THRESHOLD:
                time.sleep(DEBOUNCE_SLEEP_SEC)
                continue

            # Threshold reached — check Tapo state
            print("Checking Tapo state...")
            tapo_on = check_tapo_state(TAPO_IP)

            if tapo_on is True:
                print("Tapo is ON, Mini PC may be booting. Waiting...")
                consecutive_failures = 0
                time.sleep(NORMAL_SLEEP_SEC)
                continue

            if tapo_on is None:
                if consecutive_failures > FAILURE_THRESHOLD + 5:
                    print("Could not reach Tapo after multiple tries. Backing off...")
                    consecutive_failures = 0
                    time.sleep(NORMAL_SLEEP_SEC)
                else:
                    print("Could not reach Tapo, will retry...")
                    time.sleep(DEBOUNCE_SLEEP_SEC)
                continue

            # Tapo is OFF + Mini PC is down → turn on
            print("Mini PC down + Tapo OFF → turning ON Tapo")
            for attempt in range(3):
                print(f"  Attempt {attempt + 1}/3...")
                if turn_on_tapo(TAPO_IP):
                    break
                time.sleep(5)
            else:
                print("  All attempts failed")

            # Reset counter and give Mini PC time to boot
            consecutive_failures = 0
            print("Waiting for Mini PC to boot...")
            time.sleep(NORMAL_SLEEP_SEC)


main()
