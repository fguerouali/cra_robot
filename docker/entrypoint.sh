#!/bin/sh
set -eu

DATA="${APP_DATA:-/app/data}"
mkdir -p "$DATA"
if [ ! -f "$DATA/config.json" ] && [ -f /app/config.json ]; then
  cp /app/config.json "$DATA/config.json"
fi

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb "${DISPLAY:-:99}" -screen 0 1280x900x24 -nolisten tcp -ac &
i=0
while [ "$i" -lt 50 ]; do
  if xdpyinfo -display "${DISPLAY:-:99}" >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 0.1
done
if ! xdpyinfo -display "${DISPLAY:-:99}" >/dev/null 2>&1; then
  echo "The sign-in screen failed to start" >&2
  exit 1
fi

openbox >/dev/null 2>&1 &
x11vnc -display "${DISPLAY:-:99}" -localhost -forever -shared -nopw -quiet -xkb -noxdamage -wait 10 &
websockify --web=/usr/share/novnc 127.0.0.1:6080 localhost:5900 >/dev/null 2>&1 &

# The app and the sign-in screen stay on localhost. proxy.py is the only public port.
export APP_HOST=127.0.0.1
export APP_PORT=8765
python -u /app/docker/proxy.py &

exec python -u server.py
