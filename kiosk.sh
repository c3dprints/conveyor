#!/bin/bash
# Opens the conveyor controls full screen. Runs at login and from the
# "Conveyor Control" desktop icon; restarts the service if it isn't answering.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

if ! curl -s -o /dev/null --max-time 3 http://localhost/; then
  sudo -n systemctl restart conveyor
fi
for i in $(seq 1 30); do
  curl -s -o /dev/null --max-time 2 http://localhost/ && break
  sleep 1
done

pkill -f '[c]hromium.*--kiosk'
sleep 1
exec /usr/bin/chromium --kiosk --noerrdialogs --disable-infobars --incognito \
  --password-store=basic http://localhost
