#!/usr/bin/env python3
"""
Power failure detection daemon for Mini PC.

Monitors an ESP32 on the raw wall socket. When the ESP32 goes offline
for a sustained period (grid failure), this daemon:
1. Sets a hardware countdown on the Tapo P110 to cut power
2. Initiates a graceful OS shutdown

The Tapo countdown ensures zero phantom drain on the UPS after the
Mini PC has fully halted.
"""

import asyncio
import subprocess
import logging
import sys
from dataclasses import dataclass
from kasa import Discover, Credentials

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
    esp32_ip: str = "192.168.1.100"
    tapo_ip: str = "192.168.1.101"
    tapo_email: str = "your_tapo_email@example.com"
    tapo_password: str = "your_tapo_password"
    ping_interval_sec: int = 60
    failure_threshold_sec: int = 600  # 10 minutes
    tapo_countdown_sec: int = 120
    ping_timeout_sec: int = 5


async def ping_esp32(config: Config) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(config.ping_timeout_sec), config.esp32_ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception as e:
        log.error(f"Ping exception: {e}")
        return False


async def set_tapo_countdown(config: Config) -> bool:
    try:
        dev = await Discover.discover_single(
            config.tapo_ip,
            credentials=Credentials(config.tapo_email, config.tapo_password),
        )
        await dev.update()
        await dev._query_helper("add_countdown_rule", {
            "delay": config.tapo_countdown_sec,
            "desired_states": {"on": False},
            "enable": True,
            "remain": config.tapo_countdown_sec,
        })
        log.info(f"Tapo countdown set: power cut in {config.tapo_countdown_sec}s")
        return True
    except Exception as e:
        log.error(f"Failed to set Tapo countdown: {e}")
        return False


def initiate_shutdown():
    log.info("Initiating graceful OS shutdown...")
    subprocess.run(["sudo", "shutdown", "-h", "now"])


async def run(config: Config):
    consecutive_failures = 0
    log.info(
        f"Power daemon started. Monitoring ESP32 at {config.esp32_ip}, "
        f"threshold={config.failure_threshold_sec}s"
    )

    while True:
        alive = await ping_esp32(config)

        if alive:
            if consecutive_failures > 0:
                log.info(
                    f"ESP32 back online after {consecutive_failures * config.ping_interval_sec}s"
                )
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            elapsed = consecutive_failures * config.ping_interval_sec
            log.warning(f"ESP32 offline for {elapsed}s / {config.failure_threshold_sec}s")

            if elapsed >= config.failure_threshold_sec:
                log.critical("Power failure threshold reached. Starting shutdown sequence.")
                await set_tapo_countdown(config)
                initiate_shutdown()
                return

        await asyncio.sleep(config.ping_interval_sec)


def main():
    config = Config()
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        log.info("Daemon stopped by user.")


if __name__ == "__main__":
    main()
