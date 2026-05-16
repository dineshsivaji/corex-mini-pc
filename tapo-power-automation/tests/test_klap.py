"""
Test KLAP v2 handshake with raw sockets (mimicking MicroPython behavior).
Run this on a machine that can reach the Tapo device.
"""
import socket
import hashlib
import os


def sha1(data):
    return hashlib.sha1(data).digest()


def sha256(data):
    return hashlib.sha256(data).digest()


TAPO_IP = "192.168.1.x"
TAPO_EMAIL = "your_email"
TAPO_PASSWORD = "your_pass"


def http_post(host, port, path, body=b"", headers=None):
    if headers is None:
        headers = {}

    addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][-1]
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(addr)

    req = f"POST {path} HTTP/1.1\r\n"
    req += f"Host: {host}\r\n"
    req += f"Content-Length: {len(body)}\r\n"
    for k, v in headers.items():
        req += f"{k}: {v}\r\n"
    req += "Connection: close\r\n"
    req += "\r\n"

    s.send(req.encode() + body)

    response = b""
    while True:
        try:
            chunk = s.recv(4096)
            if not chunk:
                break
            response += chunk
        except socket.timeout:
            break

    s.close()
    return response


def main():
    local_seed = os.urandom(16)
    auth_hash = sha256(sha1(TAPO_EMAIL.encode()) + sha1(TAPO_PASSWORD.encode()))

    print(f"Local seed: {local_seed.hex()}")
    print(f"Auth hash: {auth_hash.hex()}")

    # Handshake 1
    print("\n--- Handshake 1 ---")
    resp = http_post(TAPO_IP, 80, "/app/handshake1", body=local_seed)
    print(f"Raw response (first 500 bytes):\n{resp[:500]}")

    # Parse response
    header_end = resp.find(b"\r\n\r\n")
    headers_raw = resp[:header_end]
    body = resp[header_end + 4:]

    print(f"\nHeaders:\n{headers_raw.decode()}")
    print(f"\nBody length: {len(body)}")
    print(f"Body hex: {body.hex()}")

    # Extract ALL Set-Cookie headers
    cookies = []
    for line in headers_raw.split(b"\r\n"):
        if line.lower().startswith(b"set-cookie"):
            cookies.append(line.decode())
    print(f"\nCookies found: {cookies}")

    # Get just TP_SESSIONID
    session_cookie = ""
    for c in cookies:
        val = c.split(": ", 1)[1]
        if "TP_SESSIONID" in val:
            session_cookie = val.split(";")[0]
            break
    print(f"Session cookie: {session_cookie}")

    # Parse body
    remote_seed = body[:16]
    server_hash = body[16:48]
    print(f"\nRemote seed: {remote_seed.hex()}")
    print(f"Server hash: {server_hash.hex()}")

    # Verify
    expected = sha256(local_seed + remote_seed + auth_hash)
    print(f"Expected:    {expected.hex()}")
    print(f"Match: {server_hash == expected}")

    # Handshake 2
    print("\n--- Handshake 2 ---")
    hs2_payload = sha256(remote_seed + local_seed + auth_hash)
    print(f"Payload: {hs2_payload.hex()}")
    print(f"Payload len: {len(hs2_payload)}")
    print(f"Cookie header: Cookie: {session_cookie}")

    resp = http_post(
        TAPO_IP, 80, "/app/handshake2",
        body=hs2_payload,
        headers={"Cookie": session_cookie},
    )
    print(f"\nRaw response (first 500 bytes):\n{resp[:500]}")


if __name__ == "__main__":
    main()
