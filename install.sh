#!/bin/bash
# Installs the conveyor controller as a service and sets the touchscreen
# to open the control page full screen at login.
# Run from this folder:  sudo bash install.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash install.sh"; exit 1
fi
APP_USER="${SUDO_USER:-pi}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"

echo "== Installing packages"
apt-get update
apt-get install -y python3-flask python3-paho-mqtt python3-gpiozero python3-lgpio uhubctl avahi-daemon git
# answers http://<hostname>.local, so the page is still reachable if the IP changes
systemctl enable --now avahi-daemon

# the screen's boot resolution helper; root-owned so the app can't change what it does
install -o root -g root -m 755 "$APP_DIR/conveyor-video-mode" /usr/local/sbin/conveyor-video-mode

# lets the service switch the Pi's USB port power (for motors plugged into the Pi),
# restart itself after an update, and set the screen's boot resolution
cat > /etc/sudoers.d/conveyor-uhubctl <<EOF
$APP_USER ALL=(root) NOPASSWD: /usr/sbin/uhubctl
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart conveyor
$APP_USER ALL=(root) NOPASSWD: /usr/local/sbin/conveyor-video-mode
EOF
chmod 440 /etc/sudoers.d/conveyor-uhubctl
visudo -cf /etc/sudoers.d/conveyor-uhubctl

if [ ! -f "$APP_DIR/config.json" ]; then
  cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
  echo "Created config.json from the example. Edit it with your printer details."
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/config.json"
chmod 600 "$APP_DIR/config.json"
usermod -aG gpio "$APP_USER"

echo "== Creating the conveyor service"
cat > /etc/systemd/system/conveyor.service <<EOF
[Unit]
Description=Poop conveyor controller
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/conveyor.py
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable conveyor.service
systemctl restart conveyor.service

echo "== Setting the screen to open the control page at login"
if [ -x /usr/bin/chromium ]; then
  chmod +x "$APP_DIR/kiosk.sh"
  AUTOSTART="$USER_HOME/.config/labwc/autostart"
  mkdir -p "$(dirname "$AUTOSTART")"
  touch "$AUTOSTART"
  sed -i '/conveyor-kiosk/d' "$AUTOSTART"
  echo "$APP_DIR/kiosk.sh & # conveyor-kiosk" >> "$AUTOSTART"
  # no HDMI power-off: this Elecrow panel shows white without a signal. The
  # control page goes black after 5 idle minutes instead.
  sed -i '/conveyor-blank/d' "$AUTOSTART"

  # touch follows the screen it belongs to: the 5" HDMI screen's resistive touch
  # (ADS7846) and the 4.3" DSI screen's touch (ft5x06)
  RC="$USER_HOME/.config/labwc/rc.xml"
  if [ ! -f "$RC" ]; then
    cp /etc/xdg/labwc/rc.xml "$RC" 2>/dev/null ||
      printf '<?xml version="1.0"?>\n<openbox_config xmlns="http://openbox.org/3.4/rc">\n</openbox_config>\n' > "$RC"
  fi
  for T in 'ADS7846 Touchscreen|HDMI-A-1' '10-0038 generic ft5x06 (00)|DSI-1'; do
    NAME="${T%|*}"; OUT="${T#*|}"
    if ! grep -qF "deviceName=\"$NAME\"" "$RC"; then
      sed -i "s|</openbox_config>|  <touch deviceName=\"$NAME\" mapToOutput=\"$OUT\" mouseEmulation=\"yes\"/>\n</openbox_config>|" "$RC"
    fi
  done
  chown -R "$APP_USER:$APP_USER" "$USER_HOME/.config/labwc"

  echo "== Adding the Conveyor Control desktop icon"
  mkdir -p "$USER_HOME/Desktop"
  cat > "$USER_HOME/Desktop/conveyor-control.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Conveyor Control
Comment=Open the conveyor controls full screen (restarts them if needed)
Exec=$APP_DIR/kiosk.sh
Icon=applications-engineering
Terminal=false
EOF
  chmod +x "$USER_HOME/Desktop/conveyor-control.desktop"
  # open desktop icons on tap without the "Execute / Open" question
  LIBFM="$USER_HOME/.config/libfm/libfm.conf"
  mkdir -p "$(dirname "$LIBFM")"
  if ! grep -q '^quick_exec=1' "$LIBFM" 2>/dev/null; then
    if grep -q '^\[config\]' "$LIBFM" 2>/dev/null; then
      sed -i '/^quick_exec=/d; /^\[config\]/a quick_exec=1' "$LIBFM"
    else
      printf '[config]\nquick_exec=1\n' >> "$LIBFM"
    fi
  fi
  chown -R "$APP_USER:$APP_USER" "$USER_HOME/Desktop" "$USER_HOME/.config/libfm"
else
  echo "Chromium not found (Lite OS?). Skipping the full-screen setup."
fi

echo
echo "Done. Status:  systemctl status conveyor"
echo "Logs:          journalctl -u conveyor -f"
