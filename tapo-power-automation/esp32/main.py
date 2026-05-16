"""
ESP32 MicroPython boot script for power restoration.

When the grid returns and ESP32 powers on:
1. Connects to WiFi
2. Waits 60 seconds (guards against grid flicker)
3. Sends "Turn ON" command to Tapo P110
4. Goes idle (just responds to pings from Mini PC daemon)

The Tapo P110 uses the KLAP protocol (firmware 1.3+). This script
implements the minimal handshake needed to send a single "turn on" command.
"""

import network
import time
import socket
import hashlib
import json
import os


WIFI_SSID = "Wifi_Name"
WIFI_PASSWORD = "Wifi_Pass"

TAPO_IP = "192.168.1.x"
TAPO_EMAIL = "your_email"
TAPO_PASSWORD = "your_pass"


BOOT_DELAY_SEC = 60


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        print(f"Already connected: {wlan.ifconfig()}")
        return True

    print(f"Connecting to {WIFI_SSID}...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    timeout = 30
    while not wlan.isconnected() and timeout > 0:
        time.sleep(1)
        timeout -= 1

    if wlan.isconnected():
        print(f"Connected: {wlan.ifconfig()}")
        return True
    else:
        print("WiFi connection failed")
        return False


def sha1(data):
    return hashlib.sha1(data).digest()


def sha256(data):
    return hashlib.sha256(data).digest()


def parse_http_response(raw):
    """Parse HTTP response into status code, headers dict, and body."""
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        return 0, {}, b""

    header_part = raw[:header_end]
    body = raw[header_end + 4:]

    lines = header_part.split(b"\r\n")
    status_line = lines[0]
    status_code = int(status_line.split(b" ")[1])

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


def read_response(s, content_length=None):
    """Read HTTP response from a keep-alive socket."""
    response = b""

    # Read until we have full headers
    while b"\r\n\r\n" not in response:
        chunk = s.recv(1024)
        if not chunk:
            break
        response += chunk

    header_end = response.find(b"\r\n\r\n")
    headers_raw = response[:header_end]
    body = response[header_end + 4:]

    # Get content-length from headers
    cl = 0
    for line in headers_raw.split(b"\r\n"):
        if line.lower().startswith(b"content-length"):
            cl = int(line.split(b": ")[1])
            break

    # Read remaining body
    while len(body) < cl:
        chunk = s.recv(1024)
        if not chunk:
            break
        body += chunk

    return parse_http_response(response[:header_end] + b"\r\n\r\n" + body[:cl])


def send_on_socket(s, host, path, body=b"", headers=None):
    """Send POST on existing socket, return (status, headers, body)."""
    if headers is None:
        headers = {}

    req = f"POST {path} HTTP/1.1\r\n"
    req += f"Host: {host}\r\n"
    req += f"Content-Length: {len(body)}\r\n"
    for k, v in headers.items():
        req += f"{k}: {v}\r\n"
    req += "\r\n"

    s.send(req.encode() + body)
    return read_response(s)


def send_turn_on(tapo_ip, email, password):
    """Send turn-on command to Tapo device via KLAP v2 (single TCP connection)."""
    local_seed = os.urandom(16)
    auth_hash = sha256(sha1(email.encode()) + sha1(password.encode()))

    # Open ONE persistent connection for the entire session
    addr = socket.getaddrinfo(tapo_ip, 80)[0][-1]
    s = socket.socket()
    s.settimeout(10)
    s.connect(addr)

    try:
        # --- Handshake 1 ---
        status, headers, body = send_on_socket(s, tapo_ip, "/app/handshake1", body=local_seed)
        if status != 200:
            print(f"Handshake1 failed: HTTP {status}")
            return False

        # Extract TP_SESSIONID cookie
        cookie = ""
        cookie_header = headers.get(b"set-cookie", b"")
        if cookie_header:
            parts = cookie_header.decode().split(",")
            for part in parts:
                part = part.strip()
                if "TP_SESSIONID" in part:
                    cookie = part.split(";")[0]
                    break

        if not cookie:
            print("No TP_SESSIONID cookie")
            return False

        print(f"Handshake1 OK, cookie: {cookie[:20]}...")

        if len(body) < 48:
            print(f"Handshake1 body too short: {len(body)} bytes")
            return False

        remote_seed = body[:16]
        server_hash = body[16:48]

        # Verify server hash
        expected = sha256(local_seed + remote_seed + auth_hash)
        if server_hash != expected:
            print("Server hash mismatch")
            return False

        print("Server hash verified OK")

        # --- Handshake 2 (same connection) ---
        hs2_payload = sha256(remote_seed + local_seed + auth_hash)
        status, headers, body = send_on_socket(
            s, tapo_ip, "/app/handshake2",
            body=hs2_payload,
            headers={"Cookie": cookie},
        )
        if status != 200:
            print(f"Handshake2 failed: HTTP {status}")
            return False

        print("Handshake2 OK — session established")

        # --- Derive keys ---
        key = sha256(b"lsk" + local_seed + remote_seed + auth_hash)[:16]
        full_iv = sha256(b"iv" + local_seed + remote_seed + auth_hash)
        iv = full_iv[:12]
        seq = int.from_bytes(full_iv[-4:], "big", True)  # SIGNED int
        sig_key = sha256(b"ldk" + local_seed + remote_seed + auth_hash)[:28]

        import ucryptolib
        import struct

        def encrypt_and_send(s, tapo_ip, cookie, key, iv, sig_key, seq, payload_dict):
            """Encrypt a command and send it. Returns (seq, status, response_body)."""
            seq += 1
            cmd = json.dumps(payload_dict).encode()

            # PKCS7 padding
            pad_len = 16 - (len(cmd) % 16)
            padded = cmd + bytes([pad_len] * pad_len)

            # AES-128-CBC: IV = iv(12 bytes) + seq(4 bytes signed big-endian)
            seq_bytes = struct.pack(">l", seq)
            cipher_iv = iv + seq_bytes

            cipher = ucryptolib.aes(key, 2, cipher_iv)
            ciphertext = cipher.encrypt(padded)

            # Signature: SHA256(sig_key + seq_bytes + ciphertext)
            signature = sha256(sig_key + seq_bytes + ciphertext)

            # Wire format: signature(32) + ciphertext
            request_body = signature + ciphertext

            status, headers, body = send_on_socket(
                s, tapo_ip, f"/app/request?seq={seq}",
                body=request_body,
                headers={"Cookie": cookie},
            )
            return seq, status, body

        def decrypt_response(key, iv, sig_key, seq, body):
            """Decrypt a KLAP response body."""
            if len(body) <= 32:
                return None
            ciphertext = body[32:]
            seq_bytes = struct.pack(">l", seq)
            cipher_iv = iv + seq_bytes
            decipher = ucryptolib.aes(key, 2, cipher_iv)
            padded = decipher.decrypt(ciphertext)
            # Remove PKCS7 padding
            pad_len = padded[-1]
            plaintext = padded[:-pad_len]
            return json.loads(plaintext)

        # --- Check device state first ---
        seq, status, body = encrypt_and_send(
            s, tapo_ip, cookie, key, iv, sig_key, seq,
            {"method": "get_device_info"}
        )

        if status == 200 and body:
            info = decrypt_response(key, iv, sig_key, seq, body)
            if info and info.get("result", {}).get("device_on"):
                print("Tapo is already ON — skipping")
                return True
            else:
                print("Tapo is OFF — turning on...")
        else:
            print(f"get_device_info failed (HTTP {status}), attempting turn on anyway...")

        # --- Send turn ON command ---
        seq, status, body = encrypt_and_send(
            s, tapo_ip, cookie, key, iv, sig_key, seq,
            {"method": "set_device_info", "params": {"device_on": True}}
        )

        if status == 200:
            print("Tapo turned ON successfully!")
            return True
        else:
            print(f"Request failed: HTTP {status}")
            return False

    finally:
        s.close()


def main():
    if not connect_wifi():
        print("Cannot proceed without WiFi. Resetting in 30s...")
        time.sleep(30)
        import machine
        machine.reset()

    print(f"Waiting {BOOT_DELAY_SEC}s before sending Tapo ON (grid flicker guard)...")
    time.sleep(BOOT_DELAY_SEC)

    # Verify WiFi is still connected (grid didn't flicker)
    wlan = network.WLAN(network.STA_IF)
    if not wlan.isconnected():
        print("WiFi lost during delay — grid likely flickered. Resetting...")
        import machine
        machine.reset()

    # Retry up to 3 times
    for attempt in range(3):
        print(f"Attempt {attempt + 1}: Sending Tapo ON command...")
        if send_turn_on(TAPO_IP, TAPO_EMAIL, TAPO_PASSWORD):
            break
        time.sleep(5)
    else:
        print("All attempts failed")

    # Go idle — just stay connected for ping responses
    print("Entering idle mode (responding to pings)...")
    while True:
        time.sleep(300)
        # Periodic WiFi check
        if not wlan.isconnected():
            print("WiFi disconnected, reconnecting...")
            connect_wifi()


main()
