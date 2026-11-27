#!/usr/bin/env bash
# Start chatgpt_api.py on a headless server using a virtual display (Xvfb on :99).
# To watch / solve a CAPTCHA:  x11vnc -display :99 -localhost -nopw   then VNC via SSH tunnel.
set -euo pipefail
cd "$(dirname "$0")"

# Optional settings (HA_TOKEN etc.) from .env next to this script
if [ -f .env ]; then set -a; . ./.env; set +a; fi

export DISPLAY=:99
if ! pgrep -f "Xvfb :99" >/dev/null; then
  Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
  sleep 1
fi
pgrep -f "Xvfb :99" >/dev/null || { echo "Xvfb failed to start" >&2; exit 1; }
echo "Virtual display :99 is up"

for v in .venv venv; do
  if [ -x "$v/bin/python" ]; then exec "$v/bin/python" chatgpt_api.py; fi
done
echo "No Python venv found (.venv or venv) in $(pwd)" >&2
exit 1
