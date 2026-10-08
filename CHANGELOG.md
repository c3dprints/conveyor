# Changelog

## 1.0.3 (2026-10-08)

- Added: Shut down Pi button with an on-screen "Are you sure?" box. The belt is switched
  off as the Pi shuts down.
- Added: "Made by c3dprints.com" in Settings and on the setup page.
- Release notes: run `sudo bash install.sh` once after updating (allows the shutdown).

## 1.0.2 (2026-10-08)

- Added: Screen settings page (on the Pi and at `/screen`): pick the 5" HDMI or 4.3" DSI
  screen, the resolution (HDMI), zoom and rotation. Switching screens turns the other output
  off and updates the boot setting; a change switches back after 20 seconds unless you press
  Keep.
- Added: updates from GitHub. The Pi checks for tagged releases every 6 hours, shows an
  Install button, and can install by itself when no belt is running. A release that doesn't
  compile is rolled back.
- Added: Printer setup and Screen settings buttons on the Pi's control page.
- Added: touch mapping for the 4.3" DSI screen (ft5x06).
- Release notes: run `sudo bash install.sh` once after updating from 1.0.1 (installs the
  screen helper and git).

## 1.0.1 (2026-10-08)

- Fixed: the belt could stay powered after a run. A hung `uhubctl` call crashed the motor
  thread; switching errors are now caught and logged, and "off" is re-sent every 30 seconds
  while the belt is idle.
- Fixed: Screen off, Rotate and Exit to desktop didn't respond to touch after drag-to-scroll
  was added. Drags that start on a button are now left alone.
- Added: version number, shown at the bottom of the control page and in `/api/status`.
- Added: Rotate 90° button (turns the screen and saves the position).
- Added: drag to scroll on the Pi's touchscreen.
- Added: `http://conveyor.local` address (avahi), shown on the screen with the IP.
- Changed: default belt run is 60 seconds every 5 minutes while printing.

## 1.0.0 (2026-10-07)

- First release: Bambu Lab printers over local MQTT, Pi USB power switching, touchscreen
  controls with Run 30s / On / Off, printer setup page with network discovery, screen off
  after 5 minutes, desktop icon.
