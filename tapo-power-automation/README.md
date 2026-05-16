# Tapo KLAP v2 Protocol: MicroPython Implementation

## Overview

This project implements the TP-Link Tapo KLAP v2 authentication and control protocol in MicroPython for ESP32, enabling direct device control without cloud dependencies. The primary use case is an automated power restoration system where the ESP32 turns on a Tapo P110 smart plug when mains power returns after an outage.

## The Problem

Controlling a Tapo P110 smart plug from an ESP32 running MicroPython requires implementing the KLAP v2 (Key-Length-Authentication Protocol) handshake from scratch. Unlike higher-level Python libraries (e.g., `python-kasa`) that abstract this away behind `aiohttp` and automatic connection management, MicroPython on ESP32 has:

- No `asyncio`-based HTTP client with cookie jars
- No `aiohttp` or `requests` library
- Only raw sockets and `ucryptolib` for AES
- Limited RAM (~520KB) — no room for heavyweight protocol stacks

The implementation hit **three distinct protocol-level bugs** before successfully authenticating, each producing the same symptom: `HTTP 400 Bad Request` on the handshake2 step.

---

## KLAP v2 Protocol Summary

The Tapo KLAP v2 protocol establishes an encrypted session in three phases:

```
┌──────────┐                          ┌──────────────┐
│  Client  │                          │  Tapo Device │
└────┬─────┘                          └──────┬───────┘
     │                                       │
     │  POST /app/handshake1                 │
     │  Body: local_seed (16 random bytes)   │
     │──────────────────────────────────────>│
     │                                       │
     │  200 OK                               │
     │  Set-Cookie: TP_SESSIONID=xxx         │
     │  Body: remote_seed(16) + server_hash(32)
     │<──────────────────────────────────────│
     │                                       │
     │  [Client verifies server_hash]        │
     │                                       │
     │  POST /app/handshake2                 │
     │  Cookie: TP_SESSIONID=xxx             │
     │  Body: client_hash (32 bytes)         │
     │──────────────────────────────────────>│
     │                                       │
     │  200 OK (session established)         │
     │<──────────────────────────────────────│
     │                                       │
     │  POST /app/request?seq=N              │
     │  Cookie: TP_SESSIONID=xxx             │
     │  Body: AES-CBC encrypted command      │
     │──────────────────────────────────────>│
     │                                       │
     │  200 OK + encrypted response          │
     │<──────────────────────────────────────│
     │                                       │
```

### Cryptographic Details

```
auth_hash       = SHA256(SHA1(email) + SHA1(password))
server_hash     = SHA256(local_seed + remote_seed + auth_hash)
client_hash     = SHA256(remote_seed + local_seed + auth_hash)
encryption_key  = SHA256("lsk" + local_seed + remote_seed + auth_hash)[:16]
iv_seed         = SHA256("iv" + local_seed + remote_seed + auth_hash)[:12]
seq_start       = int(SHA256("seq" + local_seed + remote_seed + auth_hash)[-4:])
```

---

## Issues Encountered & Solutions

### Issue 1: Wrong Hash Algorithm for Credentials

**Symptom:** Handshake1 returned 200 but server hash verification failed.

**Root Cause:** KLAP v2 uses `SHA256(SHA1(email) + SHA1(password))` for the auth hash. Initial implementation used `SHA256(SHA256(email) + SHA256(password))`.

The confusion arises because KLAP **v1** (used by older Kasa devices) uses `MD5(MD5(username) + MD5(password))`, and many online references mix up v1 and v2.

**Fix:**

```python
# WRONG (v1-style thinking applied to v2)
auth_hash = sha256(sha256(email.encode()) + sha256(password.encode()))

# CORRECT (KLAP v2)
auth_hash = sha256(sha1(email.encode()) + sha1(password.encode()))
```

**How we identified it:** Server hash verification (`SHA256(local_seed + remote_seed + auth_hash) == response[16:48]`) failed consistently, proving the `auth_hash` itself was wrong. Cross-referencing `python-kasa`'s `KlapTransportV2.generate_auth_hash()` confirmed the SHA1 construction.

---

### Issue 2: Wrong Byte Order in Handshake2 Payload

**Symptom:** Handshake1 succeeded, server hash verified, but handshake2 returned HTTP 400.

**Root Cause:** The handshake2 payload has a specific byte order that differs from the server hash verification. Multiple incorrect orderings were attempted:

| Attempt | Payload | Result |
|---------|---------|--------|
| 1 | `SHA256(remote_seed + auth_hash + local_seed)` | 400 |
| 2 | `SHA256(local_seed + auth_hash + remote_seed)` | 400 |
| 3 | `SHA256(remote_seed + local_seed + auth_hash)` | 400* |

*Attempt 3 was actually the **correct** byte order, but still failed due to Issue 3 below.

**The correct payload** (from `python-kasa` source, `KlapTransportV2.handshake2_seed_auth_hash()`):

```python
client_hash = SHA256(remote_seed + local_seed + auth_hash)
```

Note the asymmetry in the protocol:
- **Server proves identity:** `SHA256(local_seed + remote_seed + auth_hash)` — local first
- **Client proves identity:** `SHA256(remote_seed + local_seed + auth_hash)` — remote first

This asymmetry prevents replay attacks — each side proves knowledge of the auth_hash in a different context.

---

### Issue 3: TCP Connection Affinity (The Critical Discovery)

**Symptom:** Handshake1 succeeded, server hash verified, correct handshake2 payload computed — but handshake2 still returned HTTP 400. Every attempt failed identically regardless of payload content.

**Root Cause:** The Tapo device **binds the session cookie to the originating TCP connection**. When handshake2 was sent on a new TCP socket (as our MicroPython code did — opening a fresh `socket.connect()` for each request), the device rejected the `TP_SESSIONID` cookie as invalid, returning 400.

This is a form of connection-pinned session management. The device's HTTP server (`SHIP 2.0`) associates the `TP_SESSIONID` with internal state tied to the specific TCP file descriptor. A valid cookie presented on a different socket is treated as an attack/replay attempt and rejected.

**Why this was hard to find:**

1. `python-kasa` uses `aiohttp`, which maintains a connection pool and automatically reuses TCP connections via HTTP/1.1 keep-alive. The library never explicitly documents this requirement because `aiohttp` handles it transparently.

2. The HTTP 400 response contains no body or error message — just `Content-Length: 0`. There's no way to distinguish "wrong hash" from "wrong connection" from the response alone.

3. The `Connection: close` header in our initial implementation forced the server to close the TCP connection after handshake1, making connection reuse impossible. Even removing that header didn't help because we opened a new socket for handshake2.

**How we identified it:**

After exhausting all possible byte orderings for the handshake2 payload (all returned 400), we examined how `python-kasa` manages connections and noticed that `aiohttp`'s default keep-alive behavior was the only meaningful difference between our implementation and theirs.

A diagnostic test was written (`test_klap_keepalive.py`) that performed both handshakes on a single persistent socket:

```python
# Open ONE connection
s = socket.socket()
s.connect((TAPO_IP, 80))

# Handshake 1 on this socket
send_request(s, "/app/handshake1", local_seed)  # → 200 OK

# Handshake 2 on THE SAME socket
send_request(s, "/app/handshake2", payload, cookie)  # → 200 OK ✓
```

This immediately succeeded, confirming the connection-affinity hypothesis.

**Fix — single persistent socket for entire session:**

```python
def send_turn_on(tapo_ip, email, password):
    # Open ONE persistent connection
    s = socket.socket()
    s.settimeout(10)
    s.connect(socket.getaddrinfo(tapo_ip, 80)[0][-1])

    try:
        # Handshake 1 (on this socket)
        send_on_socket(s, "/app/handshake1", local_seed)

        # Handshake 2 (SAME socket)
        send_on_socket(s, "/app/handshake2", hs2_payload, cookie)

        # Encrypted command (SAME socket)
        send_on_socket(s, f"/app/request?seq={seq}", encrypted_body, cookie)
    finally:
        s.close()
```

---

### Additional Implementation Detail: The Cookie Trap

The Tapo device sends its cookie in a single `Set-Cookie` header with an unusual format:

```
Set-Cookie: TP_SESSIONID=513B4AA5DDC057AB3F23A7C75A769AE0;TIMEOUT=86400
```

Note: `TIMEOUT=86400` looks like a second cookie but is actually part of the same header value (separated by `;`, not `,`). Naively splitting on `;` and sending back `TP_SESSIONID=xxx;TIMEOUT=86400` or just `TIMEOUT=86400` will fail.

The correct extraction:

```python
# The full value is: "TP_SESSIONID=xxx;TIMEOUT=86400"
# We want just: "TP_SESSIONID=xxx"
cookie = header_value.split(";")[0]  # → "TP_SESSIONID=xxx"
```

From `python-kasa` source comments:
> "The device returns a TIMEOUT cookie on handshake1 which it doesn't like to get back"

---

## Encryption (Post-Handshake)

After the handshake, commands are sent AES-128-CBC encrypted:

```python
seq += 1
seq_bytes = seq.to_bytes(4, "big")

# IV = iv_seed(12 bytes) + seq_number(4 bytes)
full_iv = iv_seed + seq_bytes

# AES-128-CBC encrypt with PKCS7 padding
cipher = AES(key, CBC, full_iv)
encrypted = cipher.encrypt(pkcs7_pad(plaintext))

# Wire format: seq(4) + ciphertext + SHA256(seq + ciphertext)
payload = seq_bytes + encrypted + SHA256(seq_bytes + encrypted)

# Send as: POST /app/request?seq={seq_number}
```

The signature (`SHA256(seq + ciphertext)`) provides integrity verification. The sequence number prevents replay attacks and must be passed as a query parameter.

---

## Architecture: Power Automation System

```
    [ MAIN UTILITY GRID ]
             │
             ├───► [ ESP32 ] (raw wall socket — dies on power cut)
             │
             └───► [ UPS ]
                     │
                     └───► [ Tapo P110 ] ───► [ Mini PC + HDDs ]
```

**Normal operation:** Mini PC pings ESP32 every 60s.

**Power failure:** ESP32 dies → Mini PC detects 10 minutes of failed pings → sets Tapo hardware countdown (120s to cut power) → shuts down gracefully → Tapo cuts power → UPS preserved.

**Power restored:** ESP32 boots → connects WiFi → waits 60s (flicker guard) → sends KLAP "turn ON" to Tapo → Mini PC boots via BIOS "Restore on AC Loss" setting.

---

## File Structure

```
tapo-power-automation/
├── esp32/
│   └── main.py                 # MicroPython: KLAP v2 implementation + boot logic
├── minipc/
│   ├── power_daemon.py         # Python: ping monitor + shutdown orchestrator
│   ├── power-daemon.service    # systemd unit
│   └── requirements.txt
├── test_klap.py                # Diagnostic: separate connections (demonstrates failure)
├── test_klap_keepalive.py      # Diagnostic: persistent connection (demonstrates fix)
├── SETUP.md                    # Deployment instructions
└── README.md                   # This file
```

---

## Key Takeaways

1. **Protocol implementations are only as good as your understanding of the transport layer.** The KLAP crypto was correct for hours before we realized the HTTP connection model was the actual problem. High-level libraries hide transport details that become critical in bare-metal implementations.

2. **HTTP 400 is not always a payload problem.** The Tapo device returns 400 for connection-state violations, not just malformed requests. Without response bodies or error codes, debugging requires systematic isolation of variables.

3. **"Works in Python" ≠ "Works in MicroPython."** `aiohttp`'s connection pooling is invisible but essential. When porting to raw sockets, you must explicitly replicate every implicit behavior of the original HTTP client.

4. **Minimal test cases are essential.** The `test_klap_keepalive.py` script that isolated the connection-reuse hypothesis to a single variable took 30 seconds to write and immediately identified a bug that had consumed hours of hash-order permutation debugging.

---

## References

- [python-kasa KLAP transport](https://github.com/python-kasa/python-kasa/blob/master/kasa/transports/klaptransport.py) — reference implementation
- [python-kasa HTTP client](https://github.com/python-kasa/python-kasa/blob/master/kasa/httpclient.py) — cookie jar with `quote_cookie=False`
- KLAP v2 is undocumented by TP-Link; protocol details were reverse-engineered by the community
