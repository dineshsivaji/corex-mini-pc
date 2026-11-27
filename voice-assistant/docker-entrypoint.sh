#!/bin/sh
# Start a virtual display, then the API. Chromium runs "headed" on it because
# chatgpt.com bot-checks headless Chromium.
set -e

# :100 (not :99) so it never clashes with an Xvfb started on the host for
# run_chatgpt_api.sh - with host networking the X abstract socket is shared.
DISPLAY_NUM="${XVFB_DISPLAY:-100}"
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

Xvfb ":${DISPLAY_NUM}" -screen 0 1280x900x24 -nolisten tcp &
export DISPLAY=":${DISPLAY_NUM}"

i=0
while [ ! -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && [ $i -lt 50 ]; do
  i=$((i + 1)); sleep 0.1
done
echo "Virtual display ${DISPLAY} is up"

exec python /app/chatgpt_api.py
