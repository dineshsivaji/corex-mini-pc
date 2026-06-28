#!/usr/bin/env python3
"""
Power failure detection daemon for Mini PC.

Architecture:
    [Utility line]              [UPS-backed line]
        Zeb SP110 (beacon)         Mini PC (this daemon)
        ESP32                      Tapo P110 (smart switch)
                                   3.5" HDD
        WiFi router on its own UPS (2 hr).

Detection:
    Mini PC pings Zeb SP110 every 60s. If Zeb is unreachable for 5 min:
      1. Sanity-check by pinging the gateway. If gateway is also down, treat
         as a network issue and skip shutdown (avoid false positives).
      2. Otherwise: confirmed grid failure → start shutdown sequence.

Notification transport:
    Daemon publishes pre-formatted WhatsApp messages to NATS JetStream
    (subject `notify.whatsapp`). The WhatsApp server's in-process NATS
    consumer pulls them and calls Baileys' sendMessage directly. JetStream
    persists the message on the boot SSD so a Mini PC reboot or a
    temporarily-disconnected WhatsApp socket does not lose the alert.

Shutdown sequence:
    1. Publish "power cut" message to NATS (sync — wait for JetStream ack)
    2. sync filesystem
    3. Set Tapo countdown(60s, off) — verify response, retry once on failure
    4. systemctl stop docker (graceful service shutdown)
    5. umount /mnt/storage (best-effort)
    6. shutdown -P now (Tapo cuts power 60s later)
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import nats
from kasa import Credentials, Discover
from nats.errors import TimeoutError as NatsTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/power-daemon.log", mode="a"),
    ],
)
log = logging.getLogger("power-daemon")


@dataclass
class Config:
    # Network targets
    zeb_sp110_ip: str = "192.168.1.111"
    gateway_ip: str = "192.168.1.1"
    tapo_ip: str = "192.168.1.110"

    # Tapo credentials (override via env in production)
    tapo_email: str = "your_tapo_email@example.com"
    tapo_password: str = "your_tapo_password"

    # NATS JetStream — replaces direct HA webhook calls. The WhatsApp
    # server's in-process consumer pulls from `notify.whatsapp` and
    # forwards to Baileys.
    nats_url: str = "nats://127.0.0.1:4222"
    nats_subject: str = "notify.whatsapp"
    nats_publish_timeout_sec: int = 5

    # WhatsApp recipient (jid or group id). Used as the `to` field in the
    # NATS payload that the consumer forwards verbatim to sendMessage().
    whatsapp_to: str = "your_group_or_jid_here"

    # Path to the YAML file with the 100 "Mini PC is X" status lines.
    # Daemon now owns rotation (previously HA's counter.recovery_index).
    quote_lines_path: str = "/opt/power-daemon/quote_lines.yaml"

    # State directory (overridden by systemd's STATE_DIRECTORY env var).
    # Holds the managed-shutdown marker + the recovery-quote cursor.
    state_dir: str = "/var/lib/power-daemon"

    # Shutdown sequence
    hdd_device: str = "/dev/sda"
    storage_mount: str = "/mnt/storage"

    # Timing
    ping_interval_sec: int = 60
    failure_threshold_sec: int = 300  # 5 minutes
    tapo_countdown_sec: int = 60
    ping_timeout_sec: int = 5
    ping_packet_count: int = 5  # Multiple packets to handle flaky WiFi smart plugs


# --- Network checks ---


async def ping(ip: str, timeout: int, count: int = 1) -> bool:
    """
    ICMP ping. Returns True if at least one of `count` packets gets a reply.

    Sending multiple packets handles WiFi-power-saved smart plugs that miss
    the first ICMP (radio is asleep) but respond to subsequent ones.
    Uses 0.5s interval to keep total time bounded (~count*0.5s + timeout).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ping", "-c", str(count), "-i", "0.5",
            "-W", str(timeout), ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip() or stdout.decode().strip()
            if err:
                log.warning(f"ping {ip} rc={proc.returncode}: {err}")
        return proc.returncode == 0
    except Exception as e:
        log.error(f"Ping {ip} exception: {e}")
        return False


async def is_real_outage(config: Config) -> bool:
    """
    Sanity check: distinguish power cut from network glitch.

    Zeb down + gateway up   → real power cut (Zeb lost utility power)
    Zeb down + gateway down → network issue (don't trigger shutdown)
    """
    gateway_alive = await ping(
        config.gateway_ip, config.ping_timeout_sec, config.ping_packet_count
    )
    if not gateway_alive:
        log.warning(
            f"Zeb is down BUT gateway {config.gateway_ip} is also down → "
            f"network issue, NOT a power cut. Skipping shutdown."
        )
        return False
    log.info(f"Gateway {config.gateway_ip} reachable → confirmed power cut")
    return True


# --- Quote rotation (moved from HA's counter.recovery_index) ---


def load_quotes(path: str) -> list[str]:
    """
    Parse the bundled quote_lines.yaml. Same file that HA used to !include,
    but with a stdlib-only parser so we don't pull PyYAML into the daemon.
    Format expected: `- "text"` per line, plus comments / blanks.
    """
    quotes: list[str] = []
    try:
        with open(path) as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or not s.startswith("- "):
                    continue
                v = s[2:].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                quotes.append(v)
    except Exception as e:
        log.warning(f"Could not load quotes from {path}: {e}")
    if not quotes:
        quotes = ["Mini PC is back online"]
    return quotes


def next_quote(config: Config, quotes: list[str]) -> str:
    """Read cursor from state_dir/recovery_cursor, return quotes[cursor % N], advance."""
    cursor_path = os.path.join(config.state_dir, "recovery_cursor")
    cursor = 0
    try:
        with open(cursor_path) as f:
            cursor = int(f.read().strip() or "0")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"Cursor read error ({cursor_path}): {e}")
    idx = cursor % len(quotes)
    try:
        with open(cursor_path, "w") as f:
            f.write(str(cursor + 1))
    except Exception as e:
        log.warning(f"Cursor write error ({cursor_path}): {e}")
    return quotes[idx]


# --- NATS publish ---------------------------------------------------------


async def publish(js, config: Config, text: str, kind: str, dedup_key: str) -> bool:
    """
    Publish a WhatsApp notification to JetStream. Blocks until the server
    acks the publish (which is what makes this durable). Returns False on
    timeout/error; the caller should log but continue — losing one alert
    is preferable to blocking shutdown forever.

    `dedup_key` uses NATS' built-in deduplication window (stream config:
    dupe-window 2m) so a retry storm during shutdown doesn't double-send.
    """
    payload = json.dumps({"to": config.whatsapp_to, "text": text}).encode()
    msg_id = f"{kind}-{dedup_key}"
    try:
        await asyncio.wait_for(
            js.publish(
                config.nats_subject,
                payload,
                headers={"Nats-Msg-Id": msg_id, "X-Kind": kind},
            ),
            timeout=config.nats_publish_timeout_sec,
        )
        log.info(f"NATS publish ok [kind={kind} msg_id={msg_id}]")
        return True
    except (NatsTimeoutError, asyncio.TimeoutError):
        log.error(f"NATS publish timeout [kind={kind} msg_id={msg_id}]")
        return False
    except Exception as e:
        log.error(f"NATS publish failed [kind={kind} msg_id={msg_id}]: {e}")
        return False


async def maybe_send_recovery_notification(js, config: Config, quotes: list[str]) -> None:
    """
    On daemon startup, publish a "Mini PC back online" message to NATS.
    If a managed-shutdown marker exists with a fresh-but-not-too-fresh mtime
    (30s..24h), include the outage duration. Otherwise just announce presence.

    Retries for up to 2 minutes since the publish itself can race with NATS
    container warm-up after a power-restore boot. Once published, the
    WhatsApp server's consumer + JetStream redelivery handle the rest.
    """
    marker = os.path.join(config.state_dir, "managed-shutdown")
    duration_min: int | None = None
    marker_mtime: float | None = None

    try:
        marker_mtime = os.stat(marker).st_mtime
        elapsed = time.time() - marker_mtime
        if elapsed < 30:
            log.info("Marker too fresh (daemon restart) — skipping recovery notification")
            return
        if 30 <= elapsed <= 86400:
            duration_min = int(elapsed / 60)
        try:
            os.remove(marker)
        except Exception as e:
            log.warning(f"Could not delete marker {marker}: {e}")
    except FileNotFoundError:
        pass  # Manual reboot / first boot
    except Exception as e:
        log.warning(f"State marker read error: {e}")

    flavor = next_quote(config, quotes)
    now = time.strftime("%H:%M")
    if duration_min is not None:
        text = f"🟢 {flavor} at {now} (after {duration_min} min outage)."
    else:
        text = f"🟢 {flavor} at {now}."

    # Dedup key: marker mtime if present (one notification per outage),
    # else daemon-start time bucketed to the second.
    dedup_key = str(int(marker_mtime if marker_mtime else time.time()))

    deadline = time.time() + 120
    while time.time() < deadline:
        if await publish(js, config, text, "recovered", dedup_key):
            return
        await asyncio.sleep(5)
    log.warning("Recovery NATS publish never succeeded within 120s; giving up")


# --- Tapo control ---


async def set_tapo_countdown(config: Config) -> bool:
    """
    Set Tapo P110 countdown to turn OFF after N seconds.
    Returns True on confirmed success, False otherwise.
    """
    try:
        dev = await Discover.discover_single(
            config.tapo_ip,
            credentials=Credentials(config.tapo_email, config.tapo_password),
        )
        await dev.update()
        result = await dev._query_helper("add_countdown_rule", {
            "delay": config.tapo_countdown_sec,
            "desired_states": {"on": False},
            "enable": True,
            "remain": config.tapo_countdown_sec,
        })
        rule_id = result.get("add_countdown_rule", {}).get("id")
        if not rule_id:
            raise Exception(f"Unexpected response (no rule id): {result}")
        log.info(
            f"Tapo countdown set: rule_id={rule_id}, "
            f"cuts in {config.tapo_countdown_sec}s"
        )
        return True
    except Exception as e:
        log.error(f"Failed to set Tapo countdown: {e}")
        return False


async def set_tapo_countdown_with_retry(js, config: Config, event_ts: int) -> bool:
    """Attempt Tapo countdown twice. Alert via NATS on persistent failure."""
    for attempt in (1, 2):
        if await set_tapo_countdown(config):
            return True
        log.warning(f"Tapo countdown attempt {attempt}/2 failed")
        if attempt == 1:
            await asyncio.sleep(3)

    log.critical(
        "Tapo countdown failed after retries — UPS may drain. "
        "Notifying via NATS and proceeding with shutdown anyway."
    )
    text = (
        "⚠️ Tapo countdown command FAILED. UPS may drain — Mini PC shutdown "
        "will proceed but HDD power won't auto-cut."
    )
    await publish(js, config, text, "tapo_failed", str(event_ts))
    return False


# --- Shutdown sequence ---


def run_cmd(cmd: list[str], description: str) -> bool:
    """Run a subprocess command, log result. Returns True on success."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info(f"{description}: OK")
            return True
        log.warning(
            f"{description}: rc={result.returncode} stderr={result.stderr.strip()}"
        )
        return False
    except subprocess.TimeoutExpired:
        log.error(f"{description}: timeout after 30s")
        return False
    except Exception as e:
        log.error(f"{description}: exception {e}")
        return False


async def execute_shutdown_sequence(js, config: Config) -> None:
    """Pre-shutdown sequence + final shutdown command. Does not return."""
    log.critical("=== POWER FAILURE: starting shutdown sequence ===")
    event_ts = int(time.time())

    # Step 0: drop a marker so the next boot can announce the outage
    # duration via the recovery notification.
    marker = os.path.join(config.state_dir, "managed-shutdown")
    try:
        with open(marker, "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
        log.info(f"State marker written: {marker}")
    except Exception as e:
        log.warning(f"Could not write state marker {marker}: {e}")

    # 1. Publish "power cut" to NATS — blocks until JetStream acks.
    # Durable: even if the WhatsApp socket is down right now, the
    # message survives the reboot and gets delivered when it returns.
    log.info("Step 1/6: NATS publish power_cut")
    await publish(
        js,
        config,
        "🔌 Power cut detected. Mini PC shutting down (Tapo cuts in 60s).",
        "power_cut",
        str(event_ts),
    )

    # 2. Sync filesystem
    log.info("Step 2/6: sync")
    run_cmd(["sync"], "sync")

    # 3. Set Tapo countdown (with retry; tapo_failed alert via NATS inside)
    log.info(f"Step 3/6: Tapo countdown {config.tapo_countdown_sec}s")
    await set_tapo_countdown_with_retry(js, config, event_ts)

    # 4. Stop Docker (graceful container shutdown)
    log.info("Step 4/6: systemctl stop docker")
    run_cmd(["sudo", "systemctl", "stop", "docker"], "stop docker")

    # 5. Unmount HDD (best-effort)
    log.info(f"Step 5/6: umount {config.storage_mount}")
    run_cmd(["sudo", "umount", config.storage_mount], "umount")

    # 6. Final shutdown
    log.critical("Step 6/6: shutdown -P now — system going down")
    # Flush log handlers so the above line reaches journald before the OS halts
    for handler in log.handlers:
        handler.flush()
    time.sleep(1)
    subprocess.run(["sudo", "shutdown", "-P", "now"])


# --- Main loop ---


async def connect_nats(config: Config):
    """
    Connect to NATS with retries. Returns (nc, js). NATS' client auto-
    reconnects after this, so we only handle the first connection.
    """
    deadline = time.time() + 120
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            nc = await nats.connect(
                servers=[config.nats_url],
                name="power-daemon",
                max_reconnect_attempts=-1,
                reconnect_time_wait=2,
            )
            js = nc.jetstream()
            log.info(f"Connected to NATS at {config.nats_url}")
            return nc, js
        except Exception as e:
            last_err = e
            log.warning(f"NATS connect failed ({e!r}); retrying in 5s")
            await asyncio.sleep(5)
    raise RuntimeError(f"NATS unreachable for 120s: {last_err!r}")


async def run(config: Config) -> None:
    consecutive_failures = 0
    log.info(
        f"Power daemon starting. Beacon={config.zeb_sp110_ip} "
        f"gateway={config.gateway_ip} threshold={config.failure_threshold_sec}s"
    )

    quotes = load_quotes(config.quote_lines_path)
    log.info(f"Loaded {len(quotes)} status lines from {config.quote_lines_path}")

    nc, js = await connect_nats(config)
    try:
        # Recovery / startup notification — runs in the background so the
        # watchdog loop starts immediately.
        asyncio.ensure_future(maybe_send_recovery_notification(js, config, quotes))

        while True:
            alive = await ping(
                config.zeb_sp110_ip, config.ping_timeout_sec, config.ping_packet_count
            )

            if alive:
                if consecutive_failures > 0:
                    log.info(
                        f"Zeb back online after "
                        f"{consecutive_failures * config.ping_interval_sec}s"
                    )
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                elapsed = consecutive_failures * config.ping_interval_sec
                log.warning(
                    f"Zeb offline {elapsed}s / {config.failure_threshold_sec}s"
                )

                if elapsed >= config.failure_threshold_sec:
                    if await is_real_outage(config):
                        await execute_shutdown_sequence(js, config)
                        return  # shutdown called; exit loop
                    log.info("Resetting counter due to network sanity check")
                    consecutive_failures = 0

            await asyncio.sleep(config.ping_interval_sec)
    finally:
        try:
            await nc.drain()
        except Exception:
            pass


def load_config_from_env() -> Config:
    """Override defaults from environment variables if present."""
    config = Config()
    string_fields = (
        "zeb_sp110_ip", "gateway_ip", "tapo_ip", "tapo_email", "tapo_password",
        "nats_url", "nats_subject", "whatsapp_to", "quote_lines_path",
        "hdd_device", "storage_mount", "state_dir",
    )
    int_fields = (
        "ping_interval_sec", "failure_threshold_sec", "tapo_countdown_sec",
        "ping_timeout_sec", "ping_packet_count", "nats_publish_timeout_sec",
    )
    for field_name in string_fields:
        env_key = f"POWER_DAEMON_{field_name.upper()}"
        if env_key in os.environ:
            setattr(config, field_name, os.environ[env_key].split("#")[0].strip())
    for field_name in int_fields:
        env_key = f"POWER_DAEMON_{field_name.upper()}"
        if env_key in os.environ:
            try:
                raw = os.environ[env_key].split("#")[0].strip()
                setattr(config, field_name, int(raw))
            except ValueError:
                log.warning(f"Ignoring invalid int in {env_key}={os.environ[env_key]!r}")
    # systemd's StateDirectory= sets this — prefer it over the configured default.
    if "STATE_DIRECTORY" in os.environ:
        config.state_dir = os.environ["STATE_DIRECTORY"]
    return config


def main() -> None:
    config = load_config_from_env()
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        log.info("Daemon stopped by user.")


if __name__ == "__main__":
    main()
