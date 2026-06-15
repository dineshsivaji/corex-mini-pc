#!/usr/bin/env bash
# deploy.sh — Apply repo changes to live Mini PC paths
#
# Targets:
#   daemon         — power-daemon Python + systemd unit (NOT the env file)
#   storage-wait   — wait-for-mount script + systemd unit + docker drop-in
#   ha             — HA configuration + quote_lines + smart-merge automations.yaml
#   all            — every target above
#
# Usage:
#   ./deploy.sh <target>
#   ./deploy.sh ha
#
# Environment overrides (optional):
#   REPO_DIR=/path/to/repo
#   HA_CONFIG_DIR=/opt/homeassistant/config
#   DAEMON_DIR=/opt/power-daemon
#   HA_CONTAINER=homeassistant
#   HA_TOKEN=<long-lived-token>     # used for post-deploy verification
#
# What's NOT touched:
#   /etc/default/power-daemon       — contains your Tapo password
#   /opt/homeassistant/config/secrets.yaml
#   User automations in automations.yaml that aren't power-related
#
# Side effects:
#   - Backs up files before overwriting (.bak.<timestamp>)
#   - Restarts power-daemon.service and the HA container

set -euo pipefail

# ---------- Config ----------

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/opt/homeassistant/config}"
DAEMON_DIR="${DAEMON_DIR:-/opt/power-daemon}"
HA_CONTAINER="${HA_CONTAINER:-homeassistant}"
HA_TOKEN="${HA_TOKEN:-}"

TS="$(date +%s)"

# ---------- Helpers ----------

log()  { printf '[\033[1;36m%s\033[0m] %s\n' "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '[\033[1;32mok\033[0m]      %s\n' "$*"; }
warn() { printf '[\033[1;33mwarn\033[0m]    %s\n' "$*"; }
err()  { printf '[\033[1;31mERROR\033[0m]   %s\n' "$*" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] && return
    sudo -n true 2>/dev/null || err "Need sudo (run with sudo or configure NOPASSWD)"
}

backup_file() {
    local f="$1"
    [ -f "$f" ] || return 0
    sudo cp -p "$f" "$f.bak.$TS"
    log "backed up $f -> $f.bak.$TS"
}

# ---------- Targets ----------

deploy_daemon() {
    log "Target: power-daemon"
    require_root

    [ -f "$REPO_DIR/tapo-power-automation/minipc/power_daemon.py" ] \
        || err "power_daemon.py not found in repo"

    sudo systemctl stop power-daemon 2>/dev/null || true

    sudo install -m 755 -o root -g root \
        "$REPO_DIR/tapo-power-automation/minipc/power_daemon.py" \
        "$DAEMON_DIR/power_daemon.py"
    ok "deployed $DAEMON_DIR/power_daemon.py"

    sudo install -m 644 -o root -g root \
        "$REPO_DIR/tapo-power-automation/minipc/power-daemon.service" \
        /etc/systemd/system/power-daemon.service
    ok "deployed /etc/systemd/system/power-daemon.service"

    # Hint about env file (managed manually because it holds creds)
    if [ ! -f /etc/default/power-daemon ]; then
        warn "/etc/default/power-daemon is missing"
        warn "  copy and edit:  sudo cp $REPO_DIR/tapo-power-automation/minipc/power-daemon.env.example /etc/default/power-daemon"
        warn "                 sudo chmod 600 /etc/default/power-daemon"
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable power-daemon 2>/dev/null || true
    sudo systemctl start power-daemon
    sleep 2

    if sudo systemctl is-active --quiet power-daemon; then
        ok "power-daemon active"
    else
        warn "power-daemon failed to start — check 'journalctl -u power-daemon'"
    fi
}

deploy_storage_wait() {
    log "Target: storage-wait"
    require_root

    [ -d "$REPO_DIR/storage-wait" ] || err "storage-wait/ not found in repo"

    sudo install -m 755 -o root -g root \
        "$REPO_DIR/storage-wait/wait-for-mount.sh" \
        /usr/local/bin/wait-for-mount.sh
    ok "deployed /usr/local/bin/wait-for-mount.sh"

    sudo install -m 644 -o root -g root \
        "$REPO_DIR/storage-wait/wait-for-storage.service" \
        /etc/systemd/system/wait-for-storage.service
    ok "deployed /etc/systemd/system/wait-for-storage.service"

    sudo mkdir -p /etc/systemd/system/docker.service.d
    sudo install -m 644 -o root -g root \
        "$REPO_DIR/storage-wait/docker-wait-for-storage.conf" \
        /etc/systemd/system/docker.service.d/wait-for-storage.conf
    ok "deployed /etc/systemd/system/docker.service.d/wait-for-storage.conf"

    sudo systemctl daemon-reload
    sudo systemctl enable wait-for-storage.service 2>/dev/null || true
    ok "wait-for-storage enabled (takes effect on next reboot)"
}

deploy_ha() {
    log "Target: HA config"
    require_root

    [ -d "$HA_CONFIG_DIR" ] || err "HA_CONFIG_DIR ($HA_CONFIG_DIR) does not exist"
    [ -f "$REPO_DIR/homeassitant/configuration.yaml" ] \
        || err "homeassitant/configuration.yaml not found in repo"

    backup_file "$HA_CONFIG_DIR/configuration.yaml"
    backup_file "$HA_CONFIG_DIR/automations.yaml"

    sudo cp "$REPO_DIR/homeassitant/configuration.yaml" \
            "$HA_CONFIG_DIR/configuration.yaml"
    ok "deployed configuration.yaml"

    sudo cp "$REPO_DIR/homeassitant/quote_lines.yaml" \
            "$HA_CONFIG_DIR/quote_lines.yaml"
    ok "deployed quote_lines.yaml"

    log "Smart-merging automations.yaml (preserves user automations, refreshes power-automation block)"

    sudo python3 - "$REPO_DIR/homeassitant/power_automation.yaml" "$HA_CONFIG_DIR/automations.yaml" <<'PYEOF'
import re, sys, pathlib

src_path  = pathlib.Path(sys.argv[1])
live_path = pathlib.Path(sys.argv[2])

src     = src_path.read_text()
content = live_path.read_text() if live_path.exists() else ""

BEGIN = "# === BEGIN power_automation (managed by deploy.sh) ==="
END   = "# === END power_automation ==="

# Aliases owned by power_automation.yaml (current + legacy names).
# Anything from this list is removed from the live file before we
# re-append the freshly managed block.
managed = [
    "ESP32 Heartbeat Receiver",
    "Dead ESP32 Alert",
    "Power Cut Imminent",
    "Tapo Command Failed",
    "Mini PC Stuck Alert",
    "Mini PC Recovered",
    "Power Restored",        # legacy
]

# 1. Strip any prior managed block (idempotency)
content = re.sub(
    r"\n*" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n*",
    "\n",
    content,
    flags=re.DOTALL,
)

# 2. Strip standalone managed automations (legacy / first-run cleanup).
#    Matches: optional preceding comment lines + "- alias: NAME" + body
#    until the next "- alias:" or end-of-file.
for alias in managed:
    pat = re.compile(
        r"(?ms)^(?:#[^\n]*\n)*- alias:\s*" + re.escape(alias)
        + r"\b.*?(?=^- alias:|\Z)"
    )
    content = pat.sub("", content)

# 3. Trim trailing whitespace, ensure clean separator
content = content.rstrip() + "\n\n"

# 4. Append managed block with markers
content += BEGIN + "\n"
content += "# Managed by deploy.sh — edit homeassitant/power_automation.yaml in repo, then re-run.\n"
content += src.rstrip() + "\n"
content += END + "\n"

live_path.write_text(content)
print(f"merged: {live_path}")
PYEOF

    ok "automations.yaml merged"

    log "Validating HA config"
    if docker exec "$HA_CONTAINER" python3 -m homeassistant \
            --script check_config -c /config 2>&1 | tee /tmp/ha_check.log | grep -q "Successful config"; then
        ok "HA config valid"
    else
        warn "HA config check returned warnings — see /tmp/ha_check.log"
        tail -10 /tmp/ha_check.log
    fi

    log "Restarting HA container"
    docker restart "$HA_CONTAINER" >/dev/null
    log "Waiting 30s for HA to boot..."
    sleep 30

    if [ -n "$HA_TOKEN" ]; then
        log "Verifying automation.mini_pc_recovered registered"
        if curl -s -f \
              -H "Authorization: Bearer $HA_TOKEN" \
              http://localhost:8123/api/states/automation.mini_pc_recovered \
              | grep -q '"entity_id": "automation.mini_pc_recovered"'; then
            ok "automation.mini_pc_recovered loaded by HA"
        else
            warn "automation.mini_pc_recovered NOT found — check HA logs"
        fi
    else
        warn "HA_TOKEN not set — skipping verification step"
        warn "  to verify: curl -s http://localhost:8123/api/states/automation.mini_pc_recovered \\"
        warn "             -H \"Authorization: Bearer \$HA_TOKEN\""
    fi
}

# ---------- Dispatch ----------

usage() {
    sed -n '2,18p' "$0"
    exit 1
}

[ $# -ge 1 ] || usage

case "$1" in
    daemon)        deploy_daemon ;;
    storage-wait)  deploy_storage_wait ;;
    ha)            deploy_ha ;;
    all)           deploy_daemon; deploy_storage_wait; deploy_ha ;;
    -h|--help)     usage ;;
    *)             err "unknown target: $1 (try: daemon, storage-wait, ha, all)" ;;
esac

ok "Done."
