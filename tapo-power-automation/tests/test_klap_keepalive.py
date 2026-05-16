"""
Test KLAP v2 handshake with a SINGLE persistent TCP connection.
The Tapo device likely binds the session to the TCP connection.
"""
import socket
import hashlib
import os
import time


def sha1(data):
    return hashlib.sha1(data).digest()


def sha256(data):
    return hashlib.sha256(data).digest()


TAPO_IP = "192.168.1.x"
TAPO_EMAIL = "your_email"
TAPO_PASSWORD = "your_pass"


def read_http_response(s):
    """Read a complete HTTP response from a socket."""
    response = b""

    # Read headers
    while b"\r\n\r\n" not in response:
        chunk = s.recv(4096)
        if not chunk:
            break
        response += chunk

    header_end = response.find(b"\r\n\r\n")
    headers_raw = response[:header_end]
    body_start = response[header_end + 4:]

    # Get Content-Length
    content_length = 0
    for line in headers_raw.split(b"\r\n"):
        if line.lower().startswith(b"content-length"):
            content_length = int(line.split(b": ")[1])
            break

    # Read remaining body if needed
    body = body_start
    while len(body) < content_length:
        chunk = s.recv(4096)
        if not chunk:
            break
        body += chunk

    return headers_raw, body[:content_length]


def send_request(s, host, path, body=b"", headers=None):
    """Send HTTP POST on existing socket, return (headers_raw, body)."""
    if headers is None:
        headers = {}

    req = f"POST {path} HTTP/1.1\r\n"
    req += f"Host: {host}\r\n"
    req += f"Content-Length: {len(body)}\r\n"
    for k, v in headers.items():
        req += f"{k}: {v}\r\n"
    req += "\r\n"

    s.send(req.encode() + body)
    return read_http_response(s)


def main():
    local_seed = os.urandom(16)
    auth_hash = sha256(sha1(TAPO_EMAIL.encode()) + sha1(TAPO_PASSWORD.encode()))

    print(f"Local seed: {local_seed.hex()}")
    print(f"Auth hash: {auth_hash.hex()}")

    # Open ONE persistent connection
    addr = socket.getaddrinfo(TAPO_IP, 80, socket.AF_INET, socket.SOCK_STREAM)[0][-1]
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(addr)
    print(f"Connected to {TAPO_IP}:80")

    # --- Handshake 1 (same connection) ---
    print("\n--- Handshake 1 ---")
    headers_raw, body = send_request(s, TAPO_IP, "/app/handshake1", body=local_seed)
    print(f"Headers:\n{headers_raw.decode()}")
    print(f"Body length: {len(body)}")

    status_line = headers_raw.split(b"\r\n")[0]
    if b"200" not in status_line:
        print(f"Handshake1 FAILED: {status_line}")
        s.close()
        return

    # Extract cookie
    session_cookie = ""
    for line in headers_raw.split(b"\r\n"):
        if line.lower().startswith(b"set-cookie"):
            val = line.split(b": ", 1)[1].decode()
            if "TP_SESSIONID" in val:
                session_cookie = val.split(";")[0]
                break
    print(f"Session cookie: {session_cookie}")

    remote_seed = body[:16]
    server_hash = body[16:48]
    print(f"Remote seed: {remote_seed.hex()}")

    # Verify
    expected = sha256(local_seed + remote_seed + auth_hash)
    print(f"Hash match: {server_hash == expected}")

    if server_hash != expected:
        print("Server hash mismatch!")
        s.close()
        return

    # --- Handshake 2 (SAME connection) ---
    print("\n--- Handshake 2 ---")
    hs2_payload = sha256(remote_seed + local_seed + auth_hash)
    print(f"Payload: {hs2_payload.hex()}")

    headers_raw, body = send_request(
        s, TAPO_IP, "/app/handshake2",
        body=hs2_payload,
        headers={"Cookie": session_cookie},
    )
    print(f"Headers:\n{headers_raw.decode()}")
    print(f"Body: {body.hex() if body else '(empty)'}")

    status_line = headers_raw.split(b"\r\n")[0]
    if b"200" not in status_line:
        print(f"\nHandshake2 FAILED: {status_line}")
        # Try alternative: without Connection: close, with keep-alive explicitly
        print("\n--- Retry with different approach ---")
        s.close()

        # New connection, try handshake2 with keep-alive header
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(addr)

        # Redo handshake1
        headers_raw, body = send_request(
            s, TAPO_IP, "/app/handshake1",
            body=local_seed,
            headers={"Connection": "keep-alive"},
        )
        status_line = headers_raw.split(b"\r\n")[0]
        print(f"Handshake1 retry: {status_line.decode()}")

        if b"200" in status_line:
            # Extract new cookie
            for line in headers_raw.split(b"\r\n"):
                if line.lower().startswith(b"set-cookie"):
                    val = line.split(b": ", 1)[1].decode()
                    if "TP_SESSIONID" in val:
                        session_cookie = val.split(";")[0]
                        break

            remote_seed = body[:16]
            # Recompute handshake2 payload with new remote_seed
            hs2_payload = sha256(remote_seed + local_seed + auth_hash)

            # Verify new server hash
            server_hash = body[16:48]
            expected = sha256(local_seed + remote_seed + auth_hash)
            print(f"Hash match: {server_hash == expected}")

            headers_raw, body = send_request(
                s, TAPO_IP, "/app/handshake2",
                body=hs2_payload,
                headers={"Cookie": session_cookie, "Connection": "keep-alive"},
            )
            print(f"Handshake2 retry headers:\n{headers_raw.decode()}")

        s.close()
        return

    print("\nHandshake2 SUCCESS!")
    s.close()


if __name__ == "__main__":
    main()
