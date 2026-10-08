# Pi Poop Conveyor

Version 1.0.2

Runs a Bambu Lab purge ("poop") conveyor belt from a Raspberry Pi 4. The Pi watches your
printers over their local MQTT connection and switches the belt on when a printer needs it:
every few minutes while printing, after a filament change, and when a print ends. You can also
run it by hand from a 5" HDMI or 4.3" DSI touchscreen or from any phone or PC on your network.

It replaces the ESP32 + MOSFET board from the MakerWorld "WiFi smart poop conveyor"
(model 2594649): the 5 V USB gear motor plugs straight into the Pi, and the app switches the
Pi's USB power with `uhubctl`.

**Full setup guide:** [docs/Pi-Poop-Conveyor-Setup-Guide.pdf](docs/Pi-Poop-Conveyor-Setup-Guide.pdf)

## Quick start

On a Raspberry Pi 4 running Raspberry Pi OS (64-bit, desktop):

```bash
git clone https://github.com/c3dprints/conveyor.git ~/poop-conveyor-pi
cd ~/poop-conveyor-pi
sudo bash install.sh
sudo reboot
```

Then open `http://conveyor.local/setup` (or `http://<pi-ip>/setup`) to add printers. Each
printer needs its IP address, serial number (both found automatically on the Pi's network)
and the access code from the printer's screen.

## Files

| File | What it is |
|---|---|
| `conveyor.py` | The app: printer connections, belt control, web pages on port 80 |
| `screen.py` | Screen choice (5" HDMI / 4.3" DSI), resolution, zoom and rotation |
| `updater.py` | Checks this repo for new releases and installs them |
| `conveyor-video-mode` | Root helper (installed to /usr/local/sbin) that sets the HDMI boot resolution |
| `install.sh` | Installs packages, the `conveyor` service, the full-screen kiosk and the desktop icon |
| `kiosk.sh` | Opens the controls full screen (used at login and by the desktop icon) |
| `config.example.json` | Starting config. `install.sh` copies it to `config.json` |
| `tests/test_conveyor.py` | Simulated tests; run on any PC with Flask and paho-mqtt installed |
| `docs/` | Setup guide (PDF, and the HTML it's printed from) |

`config.json` holds printer access codes. It is git-ignored; never commit it.

## Releases and updates

The Pi installs **tagged releases** only (`v1.0.2`, `v1.0.3`, ...): it checks every 6 hours and shows an
Install button, or installs by itself if *Install updates automatically* is on. To publish one, bump
`__version__` in `conveyor.py`, add a CHANGELOG entry, then:

```bash
git tag v1.0.3
git push origin main --tags
```

## Notes

- Tested on a Raspberry Pi 4 Model B with Raspberry Pi OS trixie (labwc).
- On a Pi 4, all four USB ports switch together, so anything else plugged into them loses
  power while the belt is idle.
- The web page has no login. Keep it on your home network; use Tailscale for remote access
  instead of opening a port.
- Not affiliated with Bambu Lab.
