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

Shutdown sequence:
    1. Notify HA via webhook (fire-and-forget) → WhatsApp alert
    2. sync filesystem
    3. Set Tapo countdown(60s, off) — verify response, retry once on failure
    4. systemctl stop docker (graceful service shutdown)
    5. umount /mnt/storage (best-effort)
    6. hdparm -Y /dev/sda (park HDD heads cleanly — no "krik" sound)
    7. shutdown -h now (Tapo cuts power 60s later)
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

from kasa import Credentials, Discover

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
    zeb_sp110_ip: str = "192.168.1.110"
    gateway_ip: str = "192.168.1.1"
    tapo_ip: str = "192.168.1.111"

    # Tapo credentials (override via env in production)
    tapo_email: str = "your_tapo_email@example.com"
    tapo_password: str = "your_tapo_password"

    # HA webhook (local — same host)
    ha_webhook_power_cut: str = "http://localhost:8123/api/webhook/power_cut_imminent"
    ha_webhook_tapo_failed: str = "http://localhost:8123/api/webhook/tapo_command_failed"

    # Shutdown sequence
    hdd_device: str = "/dev/sda"
    storage_mount: str = "/mnt/storage"

    # Timing
    ping_interval_sec: int = 60
    failure_threshold_sec: int = 300  # 5 minutes
    tapo_countdown_sec: int = 60
    ping_timeout_sec: int = 10
    webhook_timeout_sec: int = 2


# --- Network checks ---


async def ping(ip: str, timeout: int) -> bool:
    """ICMP ping. Returns True if host responds within timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(timeout), ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
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
    gateway_alive = await ping(config.gateway_ip, config.ping_timeout_sec)
    if not gateway_alive:
        log.warning(
            f"Zeb is down BUT gateway {config.gateway_ip} is also down → "
            f"network issue, NOT a power cut. Skipping shutdown."
        )
        return False
    log.info(f"Gateway {config.gateway_ip} reachable → confirmed power cut")
    return True


# --- HA webhook (fire-and-forget) ---


def fire_webhook(url: str, payload: dict, timeout: int) -> bool:
    """POST to HA webhook. Returns True on 2xx, False otherwise. Never raises."""
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.warning(f"Webhook {url} failed: {e}")
        return False


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
        # python-kasa returns the unwrapped result; treat absence of exception as success
        log.info(
            f"Tapo countdown set: power cut in {config.tapo_countdown_sec}s "
            f"(response: {result})"
        )
        return True
    except Exception as e:
        log.error(f"Failed to set Tapo countdown: {e}")
        return False


async def set_tapo_countdown_with_retry(config: Config) -> bool:
    """Attempt Tapo countdown twice. Alert HA on persistent failure."""
    for attempt in (1, 2):
        if await set_tapo_countdown(config):
            return True
        log.warning(f"Tapo countdown attempt {attempt}/2 failed")
        if attempt == 1:
            await asyncio.sleep(3)

    log.critical(
        "Tapo countdown failed after retries — UPS may drain. "
        "Notifying HA and proceeding with shutdown anyway."
    )
    fire_webhook(
        config.ha_webhook_tapo_failed,
        {"reason": "countdown_command_failed_twice"},
        config.webhook_timeout_sec,
    )
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


async def execute_shutdown_sequence(config: Config) -> None:
    """Pre-shutdown sequence + final shutdown command. Does not return."""
    log.critical("=== POWER FAILURE: starting shutdown sequence ===")

    # 1. Notify HA → WhatsApp (fire-and-forget)
    log.info("Step 1/7: Notifying HA webhook")
    fire_webhook(
        config.ha_webhook_power_cut,
        {"reason": "zeb_unreachable_5min"},
        config.webhook_timeout_sec,
    )

    # 2. Sync filesystem
    log.info("Step 2/7: sync")
    run_cmd(["sync"], "sync")

    # 3. Set Tapo countdown (with retry)
    log.info(f"Step 3/7: Tapo countdown {config.tapo_countdown_sec}s")
    await set_tapo_countdown_with_retry(config)

    # 4. Stop Docker (graceful container shutdown)
    log.info("Step 4/7: systemctl stop docker")
    run_cmd(["sudo", "systemctl", "stop", "docker"], "stop docker")

    # 5. Unmount HDD (best-effort)
    log.info(f"Step 5/7: umount {config.storage_mount}")
    run_cmd(["sudo", "umount", config.storage_mount], "umount")

    # 6. Park HDD heads
    log.info(f"Step 6/7: hdparm -Y {config.hdd_device}")
    run_cmd(["sudo", "hdparm", "-Y", config.hdd_device], "hdparm -Y")

    # 7. Final shutdown
    log.critical("Step 7/7: shutdown -h now")
    subprocess.run(["sudo", "shutdown", "-h", "now"])


# --- Main loop ---


async def run(config: Config) -> None:
    consecutive_failures = 0
    log.info(
        f"Power daemon started. Beacon={config.zeb_sp110_ip} "
        f"gateway={config.gateway_ip} threshold={config.failure_threshold_sec}s"
    )

    while True:
        alive = await ping(config.zeb_sp110_ip, config.ping_timeout_sec)

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
                # Sanity check before initiating shutdown
                if await is_real_outage(config):
                    await execute_shutdown_sequence(config)
                    return  # shutdown called; exit loop
                # Network glitch detected — reset counter, keep monitoring
                log.info("Resetting counter due to network sanity check")
                consecutive_failures = 0

        await asyncio.sleep(config.ping_interval_sec)


def load_config_from_env() -> Config:
    """Override defaults from environment variables if present."""
    config = Config()
    for field_name in (
        "zeb_sp110_ip", "gateway_ip", "tapo_ip", "tapo_email", "tapo_password",
        "ha_webhook_power_cut", "ha_webhook_tapo_failed",
        "hdd_device", "storage_mount",
    ):
        env_key = f"POWER_DAEMON_{field_name.upper()}"
        if env_key in os.environ:
            setattr(config, field_name, os.environ[env_key])
    return config


def main() -> None:
    config = load_config_from_env()
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        log.info("Daemon stopped by user.")


if __name__ == "__main__":
    main()
