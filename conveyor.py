#!/usr/bin/env python3
"""Poop conveyor controller for Bambu Lab printers, running on a Raspberry Pi.

Each printer is watched over its local MQTT connection (the same one Bambu
Studio uses on your network). The conveyor it is assigned to runs:
  - every few minutes while the printer is printing,
  - shortly after a filament change (AMS purge),
  - once more after the print finishes or fails.

Web pages (port 80):
  /       controls, laid out for the 5" portrait touchscreen (works on a phone)
  /setup  add printers (auto-discovered on the network) and conveyors

Hardware test:  python3 conveyor.py --test
"""
import ipaddress
import json
import logging
import os
import re
import select
import socket
import ssl
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
import warnings
from pathlib import Path

import paho.mqtt.client as mqtt
from flask import Flask, jsonify, request, Response

import screen
import updater

try:
    from gpiozero import PWMOutputDevice
except Exception:  # not on a Pi (testing on a PC): motors are simulated
    PWMOutputDevice = None

warnings.filterwarnings("ignore", message="Callback API version 1 is deprecated")

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONVEYOR_CONFIG", BASE / "config.json"))
WEB_PORT = int(os.environ.get("CONVEYOR_PORT", "80"))
DISCOVERY_PORTS = (2021, 1990)  # Bambu printers announce themselves here (SSDP)

# GPIO -> physical pin, limited to pins 27-40, which the 5" screen leaves free
SAFE_PINS = {5: 29, 6: 31, 12: 32, 13: 33, 19: 35, 16: 36, 26: 37, 20: 38, 21: 40}
USB_PIN = "usb"  # a conveyor plugged straight into the Pi's USB ports
UHUBCTL = "/usr/sbin/uhubctl"

# stg_cur values Bambu printers report around filament changes / AMS purges
FILAMENT_STAGES = {4, 22, 24}
STAGE_NAMES = {
    -1: "idle", 0: "printing", 1: "bed leveling", 2: "heating bed",
    3: "vibration test", 4: "changing filament", 7: "heating nozzle",
    8: "calibrating extrusion", 9: "scanning bed", 13: "homing",
    14: "cleaning nozzle", 22: "unloading filament", 24: "loading filament",
    255: "idle",
}
MODEL_NAMES = {
    "BL-P001": "X1C", "BL-P002": "X1", "C13": "X1E", "C11": "P1P",
    "C12": "P1S", "N1": "A1 mini", "N2S": "A1",
}
# first characters of the serial number, for printers found by scanning
SERIAL_MODELS = {"00M": "X1C", "03W": "X1E", "01S": "P1P", "01P": "P1S",
                 "030": "A1 mini", "039": "A1"}
CN_OID = b"\x06\x03\x55\x04\x03"  # commonName in a DER certificate

DEFAULT_SETTINGS = {
    "enabled": True,
    "interval_minutes": 5,
    "run_seconds": 8,
    "filament_change_seconds": 10,
    "after_print_seconds": 15,
    "speed_percent": 100,
    "auto_update": False,
}
SETTING_LIMITS = {
    "interval_minutes": (1, 120),
    "run_seconds": (1, 120),
    "filament_change_seconds": (0, 120),
    "after_print_seconds": (0, 300),
    "speed_percent": (20, 100),
}
CONNECT_ERRORS = {
    1: "printer refused the connection (protocol)",
    4: "access code rejected",
    5: "access code rejected (not authorized)",
}

log = logging.getLogger("conveyor")
__version__ = "1.0.2"
STARTED = str(time.time())  # the screen reloads itself when this changes (after an update)


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text())
    cfg["settings"] = {**DEFAULT_SETTINGS, **cfg.get("settings", {})}
    cfg.setdefault("motors", [])
    cfg.setdefault("printers", [])
    cfg.setdefault("scan_networks", [])
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)  # holds printer access codes
    tmp.replace(CONFIG_PATH)


class PiUsbPower:
    """The Pi 4's own USB ports as a motor switch. All four share one power
    switch, so every motor plugged into the Pi runs together (on/off only)."""

    HUBS = ("2", "1-1")  # USB3 and USB2 sides; power drops only when both are off

    REASSERT_SECONDS = 30

    def __init__(self):
        self._value = None
        self._sent = 0.0
        self.value = 0

    def _uhubctl(self, hub, action):
        self._sent = time.monotonic()
        try:
            r = subprocess.run(["sudo", "-n", UHUBCTL, "-l", hub, "-a", action],
                               capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("uhubctl %s %s failed: %s", hub, action, e)
            return
        if r.returncode != 0:
            log.warning("uhubctl %s %s failed (%s): %s", hub, action, r.returncode,
                        (r.stderr or r.stdout).strip()[-300:])

    def keep_off(self):
        """The kernel can quietly power the ports back up (a device re-attaching),
        so while the belt should be idle, send "off" again every 30 seconds."""
        if self._value == 0 and time.monotonic() - self._sent >= self.REASSERT_SECONDS:
            for hub in self.HUBS:
                self._uhubctl(hub, "off")

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, level):
        on = level > 0
        if self._value is not None and (self._value > 0) == on:
            self._value = level
            return
        self._value = level
        hubs = reversed(self.HUBS) if on else self.HUBS
        for hub in hubs:
            self._uhubctl(hub, "on" if on else "off")

    def close(self):
        self.value = 0


def make_switch(pin):
    if not PWMOutputDevice:
        return None  # simulated on a PC
    if pin == USB_PIN:
        return PiUsbPower()
    return PWMOutputDevice(pin, frequency=1000)


class Motor:
    def __init__(self, name, pin, settings):
        self.name = name
        self.pin = pin
        self.settings = settings
        self.dev = make_switch(pin)
        self.lock = threading.Lock()
        self.until = 0.0
        self.closed = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def run(self, seconds, reason):
        if seconds <= 0 or self.closed:
            return
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)
        log.info("%s: run %ss (%s)", self.name, seconds, reason)

    def stop(self):
        with self.lock:
            self.until = 0.0
        log.info("%s: stop", self.name)

    @property
    def running(self):
        return time.monotonic() < self.until

    def close(self):
        self.closed = True
        self.thread.join(timeout=2)
        if self.dev:
            self.dev.close()

    def _loop(self):
        on = False
        while not self.closed:
            want = self.running
            if self.dev:
                level = self.settings["speed_percent"] / 100 if want else 0
                try:
                    if self.dev.value != level:
                        self.dev.value = level
                    elif not want and hasattr(self.dev, "keep_off"):
                        self.dev.keep_off()
                except Exception:
                    # never let one failed switch kill this loop: that once
                    # left the belt powered with nothing left to turn it off
                    log.exception("%s: switching failed, retrying", self.name)
                    time.sleep(2)
            if want != on:
                on = want
                log.info("%s %s", self.name, "ON" if on else "OFF")
            time.sleep(0.1)
        if self.dev:
            self.dev.value = 0


def make_client():
    client_id = f"conveyor-{uuid.uuid4().hex[:8]}"
    # paho-mqtt 2.x needs the callback API version; 1.x does not have it
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    else:
        client = mqtt.Client(client_id=client_id)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # printers use a self-signed cert
    client.tls_set_context(ctx)
    return client


def pushall(client, serial):
    client.subscribe(f"device/{serial}/report")
    client.publish(
        f"device/{serial}/request",
        json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
    )


class Printer:
    def __init__(self, cfg, motor, settings):
        self.name = cfg["name"]
        self.ip = cfg["ip"]
        self.serial = cfg["serial"].strip().upper()
        self.motor = motor
        self.settings = settings
        self.connected = False
        self.error = ""
        self.gcode_state = "UNKNOWN"
        self.stage = None
        self.percent = None
        self.last_periodic = 0.0

        self.client = make_client()
        self.client.username_pw_set("bblp", str(cfg["access_code"]).strip())
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=2, max_delay=30)
        self.client.connect_async(self.ip, 8883, keepalive=60)
        self.client.loop_start()

    def _run(self, seconds, reason):
        if self.motor:
            self.motor.run(seconds, reason)

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.error = CONNECT_ERRORS.get(rc, f"connect refused (code {rc})")
            log.warning("%s: %s", self.name, self.error)
            return
        self.connected = True
        self.error = ""
        log.info("%s: connected to %s", self.name, self.ip)
        pushall(client, self.serial)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            self.error = "printer not reachable, retrying"
            log.warning("%s: disconnected (code %s), retrying", self.name, rc)

    def _on_message(self, client, userdata, msg):
        try:
            p = json.loads(msg.payload).get("print")
        except ValueError:
            return
        if not isinstance(p, dict):
            return
        # P1 series only sends fields that changed, so keep the old values
        old_state, old_stage = self.gcode_state, self.stage
        self.gcode_state = p.get("gcode_state", self.gcode_state)
        self.stage = p.get("stg_cur", self.stage)
        self.percent = p.get("mc_percent", self.percent)
        self._react(old_state, old_stage)

    def _react(self, old_state, old_stage):
        s = self.settings
        if old_state != self.gcode_state:
            log.info("%s: %s -> %s", self.name, old_state, self.gcode_state)
            if self.gcode_state == "RUNNING" and old_state not in ("RUNNING", "PAUSE"):
                self.last_periodic = time.monotonic()
            if (self.gcode_state in ("FINISH", "FAILED")
                    and old_state in ("RUNNING", "PAUSE") and s["enabled"]):
                self._run(s["after_print_seconds"], f"{self.name} print ended")
        if (old_stage in FILAMENT_STAGES and self.stage not in FILAMENT_STAGES
                and s["enabled"]):
            # give the purge a few seconds to drop before running the belt
            threading.Timer(
                5, self._run,
                (s["filament_change_seconds"], f"{self.name} filament change"),
            ).start()

    def tick(self):
        s = self.settings
        if not s["enabled"] or self.gcode_state != "RUNNING":
            return
        now = time.monotonic()
        if now - self.last_periodic >= s["interval_minutes"] * 60:
            self.last_periodic = now
            self._run(s["run_seconds"], f"{self.name} interval")

    def status(self):
        return {
            "name": self.name,
            "connected": self.connected,
            "error": self.error,
            "state": self.gcode_state,
            "stage": STAGE_NAMES.get(self.stage, self.stage),
            "percent": self.percent,
            "motor": self.motor.name if self.motor else None,
            "motor_running": bool(self.motor and self.motor.running),
        }

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def test_printer(ip, serial, code, timeout=10):
    """One-off connection check used by the setup page."""
    result = {"ok": False, "message": ""}
    connected = threading.Event()
    done = threading.Event()

    def on_connect(client, userdata, flags, rc):
        if rc != 0:
            result["message"] = CONNECT_ERRORS.get(rc, f"connect refused (code {rc})")
            done.set()
            return
        connected.set()
        pushall(client, serial)

    def on_message(client, userdata, msg):
        try:
            p = json.loads(msg.payload).get("print") or {}
        except ValueError:
            return
        if "gcode_state" in p:
            result["ok"] = True
            result["message"] = f"Connected. Printer is {p['gcode_state']}."
            done.set()

    client = make_client()
    client.username_pw_set("bblp", code)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(ip, 8883, keepalive=30)
    except Exception as e:
        return {"ok": False, "message": f"Could not reach {ip} ({e}). Check the IP and that the printer is on."}
    client.loop_start()
    done.wait(timeout)
    client.loop_stop()
    client.disconnect()
    if not done.is_set():
        result["message"] = (
            "Connected, but the printer sent no data. Check the serial number."
            if connected.is_set() else
            "No answer from the printer. Check the IP address."
        )
    return result


def cert_serial(der):
    """A Bambu printer's TLS certificate carries its serial as the commonName."""
    for i in range(len(der) - 7, -1, -1):  # subject comes after issuer: search backwards
        if der[i:i + 5] == CN_OID:
            length = der[i + 6]
            value = der[i + 7:i + 7 + length].decode(errors="replace")
            if re.fullmatch(r"[0-9A-Z]{8,20}", value):
                return value
    return None


def probe_printer(ip):
    """Return the serial of a Bambu printer at ip, or None."""
    try:
        sock = socket.create_connection((ip, 8883), timeout=0.8)
    except OSError:
        return None
    try:
        sock.settimeout(10)  # P1 printers are slow to finish the TLS handshake
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(sock, server_hostname=ip) as tls:
            return cert_serial(tls.getpeercert(binary_form=True) or b"")
    except (OSError, ssl.SSLError):
        return None
    finally:
        sock.close()


def parse_network(text):
    """Accept '192.168.2', '192.168.2.0' or '192.168.2.0/24'; private ranges up to /22."""
    text = str(text).strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", text):
        text += ".0/24"
    elif "/" not in text:
        text += "/24"
    net = ipaddress.IPv4Network(text, strict=False)
    if not net.is_private or net.prefixlen < 22:
        raise ValueError("Use a home network range like 192.168.2.0/24.")
    return net


class Discovery:
    """Finds Bambu printers: listens for their broadcasts on the Pi's own
    network, and scans other networks (like 192.168.2.x) on request."""

    def __init__(self, networks=()):
        self.found = {}
        self.lock = threading.Lock()
        self.scanning = set()
        self.networks = networks  # saved networks, rescanned every 15 minutes
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._rescan, daemon=True).start()

    def _rescan(self):
        while True:
            for net in list(self.networks):
                self.scan(net, wait=True)
            time.sleep(900)

    def scan(self, network, wait=False):
        net = str(parse_network(network))
        with self.lock:
            if net in self.scanning:
                return
            self.scanning.add(net)
        t = threading.Thread(target=self._scan, args=(net,), daemon=True)
        t.start()
        if wait:
            t.join()

    def _scan(self, net):
        from concurrent.futures import ThreadPoolExecutor
        try:
            hosts = [str(h) for h in ipaddress.IPv4Network(net).hosts()]
            with ThreadPoolExecutor(64) as pool:
                for ip, serial in zip(hosts, pool.map(probe_printer, hosts)):
                    if serial:
                        self._add(serial, ip, None, SERIAL_MODELS.get(serial[:3], "Bambu printer"))
            log.info("scan of %s done", net)
        finally:
            with self.lock:
                self.scanning.discard(net)

    def _add(self, serial, ip, name, model):
        with self.lock:
            old = self.found.get(serial, {})
            self.found[serial] = {
                "serial": serial,
                "ip": ip,
                "name": name or old.get("name") or f"{model} at {ip}",
                "model": model,
                "seen": time.time(),
            }

    def _sockets(self):
        socks = []
        for port in DISCOVERY_PORTS:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
                try:
                    mreq = struct.pack("4s4s", socket.inet_aton("239.255.255.250"),
                                       socket.inet_aton("0.0.0.0"))
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError:
                    pass
                socks.append(s)
            except OSError as e:
                log.warning("discovery: cannot listen on port %s (%s)", port, e)
        return socks

    def _listen(self):
        socks = self._sockets()
        while socks:
            ready, _, _ = select.select(socks, [], [], 5)
            for s in ready:
                try:
                    data, addr = s.recvfrom(4096)
                except OSError:
                    continue
                self._parse(data, addr)

    def _parse(self, data, addr):
        text = data.decode(errors="replace")
        if "bambu" not in text.lower():
            return
        headers = {}
        for line in re.split(r"\r?\n", text)[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        serial = headers.get("usn", "").strip().upper()
        if not serial:
            return
        m = re.search(r"\d+\.\d+\.\d+\.\d+", headers.get("location", ""))
        model = headers.get("devmodel.bambu.com", "")
        self._add(serial, m.group(0) if m else addr[0],
                  headers.get("devname.bambu.com", ""),
                  MODEL_NAMES.get(model, model or "Bambu printer"))

    def list(self):
        cutoff = time.time() - 1800
        with self.lock:
            return sorted((d for d in self.found.values() if d["seen"] > cutoff),
                          key=lambda d: ipaddress.IPv4Address(d["ip"]))


class Controller:
    """Owns the motors and printer connections; rebuilt when setup changes."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.motors = []
        self.printers = []
        self.build()
        threading.Thread(target=self._ticker, daemon=True).start()

    def build(self):
        with self.lock:
            for p in self.printers:
                p.close()
            for m in self.motors:
                m.close()
            s = self.cfg["settings"]
            self.motors = [Motor(m["name"], m["pin"], s) for m in self.cfg["motors"]]
            self.printers = [
                Printer(p, self.motors[p["motor"]] if 0 <= p["motor"] < len(self.motors) else None, s)
                for p in self.cfg["printers"]
            ]

    def _ticker(self):
        while True:
            for p in list(self.printers):
                p.tick()
            time.sleep(1)

    def printer(self, i):
        printers = self.printers
        if isinstance(i, int) and 0 <= i < len(printers):
            return printers[i]
        return None

    def apply_setup(self, data):
        """Validate the setup page's printers and conveyors, then save and rebuild."""
        motors_in, printers_in = data.get("motors"), data.get("printers")
        if not isinstance(motors_in, list) or not isinstance(printers_in, list):
            raise ValueError("Bad request.")

        motors, pins = [], set()
        for n, m in enumerate(motors_in, 1):
            name = str(m.get("name", "")).strip()[:40] or f"Conveyor {n}"
            pin = m.get("pin")
            if pin != USB_PIN:
                try:
                    pin = int(pin)
                except (TypeError, ValueError):
                    pin = None
                if pin not in SAFE_PINS:
                    raise ValueError(f"{name}: pick a connection from the list.")
            if pin in pins:
                where = "the Pi's USB ports are" if pin == USB_PIN else f"GPIO{pin} is"
                raise ValueError(f"{name}: {where} already used by another conveyor.")
            pins.add(pin)
            motors.append({"name": name, "pin": pin})

        old_codes = {p["serial"].upper(): p["access_code"] for p in self.cfg["printers"]}
        printers, serials = [], set()
        for n, p in enumerate(printers_in, 1):
            name = str(p.get("name", "")).strip()[:40] or f"Printer {n}"
            try:
                ip = str(ipaddress.IPv4Address(str(p.get("ip", "")).strip()))
            except ValueError:
                raise ValueError(f"{name}: the IP address doesn't look right (like 192.168.1.40).")
            serial = str(p.get("serial", "")).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{8,20}", serial):
                raise ValueError(f"{name}: the serial number doesn't look right.")
            if serial in serials:
                raise ValueError(f"{name}: this serial number is listed twice.")
            serials.add(serial)
            code = str(p.get("access_code", "")).strip() or old_codes.get(serial, "")
            if not re.fullmatch(r"\S{4,32}", code):
                raise ValueError(f"{name}: enter the printer's access code.")
            try:
                motor = int(p.get("motor", -1))
            except (TypeError, ValueError):
                motor = -1
            if not -1 <= motor < len(motors):
                motor = -1
            printers.append({"name": name, "ip": ip, "serial": serial,
                             "access_code": code, "motor": motor})

        with self.lock:
            self.cfg["motors"] = motors
            self.cfg["printers"] = printers
            save_config(self.cfg)
            self.build()
        log.info("setup saved: %d printers, %d conveyors", len(printers), len(motors))

    def code_for(self, serial):
        for p in self.cfg["printers"]:
            if p["serial"].upper() == serial:
                return p["access_code"]
        return ""


STYLE = """
:root{--bg:#f4f6f8;--card:#fff;--text:#1d2329;--muted:#66727f;--line:#d9dee3;
--accent:#1f9d55;--warn:#c0392b;--btn:#e9edf1}
@media (prefers-color-scheme:dark){:root{--bg:#14181c;--card:#1e2329;--text:#e8ecef;
--muted:#9aa5b1;--line:#333b44;--btn:#2a3139}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0 auto;background:var(--bg);color:var(--text);
font:18px system-ui,sans-serif;padding:12px;max-width:640px}
h1{font-size:22px;margin:2px 0 12px}h2{font-size:19px;margin:0 0 6px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:12px;margin-bottom:12px}.row{display:flex;gap:8px;align-items:center}
.muted{color:var(--muted);font-size:15px}.dot{display:inline-block;width:12px;height:12px;
border-radius:50%;margin-right:8px;flex:none}.on{background:var(--accent)}.off{background:var(--warn)}
button{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--btn);
color:var(--text);border-radius:12px;padding:12px 8px;flex:1;min-height:52px;
touch-action:manipulation;cursor:pointer}
button:active{filter:brightness(.85)}button.go{background:var(--accent);color:#fff;border:0}
button:disabled{opacity:.5}
.belt{font-weight:700;color:var(--accent)}
"""

# Only on the Pi's own screen (localhost): apply the screen zoom, and drag to
# scroll, since its resistive touch arrives as a mouse and dragging a mouse
# doesn't scroll a page. Drags starting on a button or field are left alone:
# resistive touch jitters, so a tap there must stay a tap.
KIOSK_JS = """
function applyZoom(z){if(location.hostname==="localhost"&&z)document.documentElement.style.zoom=z/100}
if(location.hostname==="localhost"){fetch("/api/screen").then(r=>r.json()).then(d=>applyZoom(d.effective_zoom)).catch(()=>{});
let y0=null,s0=0,dragged=false;
addEventListener("pointerdown",e=>{dragged=false;s0=scrollY;
y0=e.pointerType==="mouse"&&!e.target.closest("button,a,input,select,label")?e.clientY:null},true);
addEventListener("pointermove",e=>{if(y0===null||!(e.buttons&1))return;const dy=e.clientY-y0;
if(Math.abs(dy)>20)dragged=true;if(dragged)scrollTo(0,s0-dy)},true);
addEventListener("pointerup",()=>{y0=null},true);
addEventListener("click",e=>{if(dragged){e.stopPropagation();e.preventDefault();dragged=false}},true)}
"""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Conveyor Control</title>
<style>""" + STYLE + """
html,body{user-select:none;-webkit-user-select:none}
.head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.head h2{display:flex;align-items:center;margin:0}.status{margin:4px 0 10px}
.set{display:flex;justify-content:space-between;align-items:center;padding:6px 0;
border-top:1px solid var(--line)}.set:first-of-type{border-top:0}
.step{display:flex;align-items:center;gap:6px}.step button{flex:0 0 52px;padding:0;font-size:24px}
.step span{min-width:52px;text-align:center;font-weight:700}
#auto{width:100%;margin-top:6px}#auto.on{background:var(--accent);color:#fff;border:0}
.foot{text-align:center;font-size:15px;margin:8px 0 16px}
#sleep{display:none;position:fixed;inset:0;background:#000;z-index:9;cursor:none}
</style></head><body>
<div id="sleep" onclick="wake()"></div>
<h1>Conveyor Control</h1>
<div id="update"></div>
<div id="belts"></div>
<div id="printers"></div>
<div class="card"><h2>Settings</h2>
<button id="auto" onclick="toggleAuto()">Automatic mode</button>
<div id="settings"></div></div>
<div class="foot muted">Add printers at <a href="/setup">printer setup</a></div>
<div class="foot muted" id="where"></div>
<div class="foot row" style="justify-content:center;flex-wrap:wrap">
<button style="flex:none;padding:8px 18px;min-height:44px;font-size:15px" onclick="sleepNow()">Screen off</button>
<button style="flex:none;padding:8px 18px;min-height:44px;font-size:15px"
onclick="location.href='/screen'">Screen settings</button>
<button style="flex:none;padding:8px 18px;min-height:44px;font-size:15px"
onclick="location.href='/setup'">Printer setup</button>
<button style="flex:none;padding:8px 18px;min-height:44px;font-size:15px"
onclick="api('/api/rotate',{}).then(d=>{if(!d.ok)document.getElementById('where').textContent=d.message})">Rotate 90&deg;</button>
<button style="flex:none;padding:8px 18px;min-height:44px;font-size:15px"
onclick="api('/api/exit-kiosk',{})">Exit to desktop</button></div>
<script>""" + KIOSK_JS + """
const FIELDS=[["interval_minutes","Run every (min)",1],["run_seconds","Run for (s)",1],
["filament_change_seconds","After filament change (s)",5],["after_print_seconds","After print ends (s)",5],
["speed_percent","Motor speed (%)",10]];
let S=null;
async function api(path,body){const r=await fetch(path,body?{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function drawSettings(){const a=document.getElementById("auto");
a.textContent="Automatic mode: "+(S.enabled?"ON":"OFF");a.className=S.enabled?"on":"";
document.getElementById("settings").innerHTML=FIELDS.map(([k,l,st])=>`<div class="set">
<div>${l}</div><div class="step"><button onclick="bump('${k}',-${st})">&minus;</button>
<span>${S[k]}</span><button onclick="bump('${k}',${st})">+</button></div></div>`).join("")}
async function save(b){S=(await api("/api/settings",b)).settings;drawSettings()}
function bump(k,d){save({[k]:S[k]+d})}
function toggleAuto(){save({enabled:!S.enabled})}
async function installUpdate(b){b.disabled=true;b.textContent="Installing...";await api("/api/update/install",{})}
function card(p,i){const st=p.connected?esc(p.state)+(p.stage!=null?" &middot; "+esc(p.stage):"")+
(p.percent!=null?" &middot; "+p.percent+"%":""):esc(p.error||"connecting...");
const belt=p.motor==null?"no conveyor":p.motor_running?'<span class="belt">RUNNING</span>':"stopped";
return `<div class="card"><div class="head"><h2><span class="dot ${p.connected?"on":"off"}"></span>${esc(p.name)}</h2>
<span class="muted">${p.motor?esc(p.motor)+": ":""}${belt}</span></div><div class="status muted">${st}</div></div>`}
function fmt(s){return s>=60?Math.ceil(s/60)+" min":s+"s"}
function belt(m,j){return `<div class="card"><div class="head"><h2>${esc(m.name)}</h2>
<span class="muted">${m.running?'<span class="belt">ON</span> &middot; '+fmt(m.left)+" left":"off"}</span></div>
<div class="row" style="margin-top:10px"><button class="go" onclick="api('/api/run',{motor:${j},seconds:30})">Run 30s</button>
<button onclick="api('/api/run',{motor:${j},seconds:3600}).then(refresh)">On</button>
<button onclick="api('/api/stop',{motor:${j}}).then(refresh)">Off</button></div></div>`}
let V=null;
async function refresh(){let d;try{d=await api("/api/status")}catch(e){return}
if(V&&d.version!=V){location.reload();return}V=d.version;applyZoom(d.zoom);
const u=d.update;document.getElementById("update").innerHTML=u&&(u.available||u.busy=="installing")?
`<div class="card"><div class="head"><h2>Update ${esc(u.latest)} available</h2>
<button class="go" style="flex:0 0 auto;padding:10px 18px" ${u.busy?"disabled":""} onclick="installUpdate(this)">
${u.busy=="installing"?"Installing...":"Install"}</button></div>
<div class="muted">${u.error?esc(u.error):"You have "+esc(u.current)+". The belt stops briefly while the app restarts."}</div></div>`:"";
document.getElementById("belts").innerHTML=d.motors.map(belt).join("");
document.getElementById("printers").innerHTML=d.printers.length?d.printers.map(card).join(""):
'<div class="card muted">No printers yet. Open <b>http://'+esc(d.name)+'/setup</b> on your PC or phone to add them.</div>';
document.getElementById("where").innerHTML='From your PC or phone: <b>http://'+esc(d.name)+'</b> or <b>http://'+esc(d.addr)+'</b> &middot; v'+esc(d.app_version);
if(!S){S=d.settings;drawSettings()}}
refresh();setInterval(refresh,2000);
// "screen off": after 5 idle minutes cover everything in black (this panel shows
// white if the HDMI signal stops). The waking tap lands here, not on a button.
const IDLE_MS=5*60*1000;let idleTimer;
function sleepNow(){document.getElementById("sleep").style.display="block"}
function wake(){setTimeout(()=>{document.getElementById("sleep").style.display="none"},300);resetIdle()}
function resetIdle(){clearTimeout(idleTimer);idleTimer=setTimeout(sleepNow,IDLE_MS)}
document.addEventListener("pointerdown",resetIdle,true);resetIdle();
</script></body></html>"""

SETUP_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Printer Setup</title>
<style>""" + STYLE + """
body{max-width:760px}
label{display:block;font-size:14px;color:var(--muted);margin:8px 0 3px}
input,select{font:inherit;width:100%;padding:9px;border-radius:9px;border:1px solid var(--line);
background:var(--bg);color:var(--text)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 10px}
@media (max-width:520px){.grid{grid-template-columns:1fr}}
.found{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;
border-top:1px solid var(--line)}.found:first-child{border-top:0}.found button{flex:0 0 auto;padding:10px 16px}
.small{flex:0 0 auto;padding:10px 16px;min-height:44px}
.result{font-size:15px;margin-top:8px}.ok{color:var(--accent)}.bad{color:var(--warn)}
.bar{position:sticky;bottom:0;background:var(--bg);padding:10px 0;display:flex;gap:10px;align-items:center}
.bar button{flex:0 0 200px}ul{margin:6px 0 0;padding-left:20px}li{margin:4px 0}
</style></head><body>
<p><a href="/">&larr; Back to controls</a></p>
<h1>Printer setup</h1>

<div class="card"><h2>What each printer needs</h2>
<ul class="muted">
<li><b>IP address</b> and <b>serial number</b>: filled in for you when the printer shows up below.</li>
<li><b>Access code</b>: on the printer's screen under <b>Settings &rarr; Network</b> (WLAN). It's an 8-character code.</li>
<li>If <b>Test</b> says the access code is rejected even though it's right, turn on <b>LAN Only</b> mode and
<b>Developer Mode</b> in the printer's network settings.</li>
</ul></div>

<div class="card"><h2>Printers found on your network</h2>
<div class="muted" style="margin-bottom:6px">Printers on the Pi's own network show up by themselves.
For printers on another network (like 192.168.2.x), scan it once here. It's rechecked every 15 minutes after that.</div>
<div class="row" style="margin-bottom:6px"><input id="net" placeholder="192.168.2.0/24">
<button class="small" onclick="scan()">Scan</button></div>
<div id="scanmsg" class="result muted"></div>
<div id="found" class="muted">Listening...</div></div>

<div class="card"><h2>Your printers</h2><div id="plist"></div>
<button class="small" onclick="addPrinter({})">+ Add a printer by hand</button></div>

<div class="card"><h2>Conveyors</h2>
<div class="muted">A conveyor is a motor the Pi can switch. Motors plugged into the Pi's own USB ports
all run together as one conveyor; several printers can share it. For separate belts, wire a MOSFET or relay
to a free pin (its GND to Pi pin 30, 34 or 39).</div>
<div id="mlist"></div>
<button class="small" onclick="addMotor()">+ Add conveyor</button></div>

<div class="card"><h2>Screen</h2>
<div class="muted">Screen size (5" or 4.3"), resolution, zoom and rotation for the Pi's touchscreen.</div>
<button class="small" style="margin-top:8px" onclick="location.href='/screen'">Screen settings</button></div>

<div class="card"><h2>Software</h2><div id="sw" class="muted">Checking...</div>
<div class="row" style="margin-top:8px;flex-wrap:wrap"><button class="small" onclick="upd('check')">Check for updates</button>
<button class="small go" id="inst" style="display:none" onclick="upd('install')">Install update</button></div>
<label style="display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--text);font-size:16px">
<input type="checkbox" id="autoupd" style="width:auto" onchange="api('/api/settings',{auto_update:this.checked})">
Install updates automatically (when no belt is running)</label></div>

<div class="bar"><button class="go" onclick="save()">Save changes</button><span id="msg" class="result"></span></div>

<script>""" + KIOSK_JS + """
let P=[],M=[],PINS={},FOUND=[];
async function api(path,body){const r=await fetch(path,body?{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function field(i,k,label,ph,type){return `<div><label>${label}</label><input type="${type||"text"}" value="${esc(P[i][k])}"
placeholder="${esc(ph)}" oninput="P[${i}].${k}=this.value" autocomplete="off"></div>`}
function drawPrinters(){document.getElementById("plist").innerHTML=P.length?P.map((p,i)=>`
<div class="card"><div class="grid">${field(i,"name","Name","e.g. P1P 1")}${field(i,"ip","IP address","192.168.1.40")}
${field(i,"serial","Serial number","")}${field(i,"access_code","Access code",p.has_code?"saved (leave blank to keep)":"from the printer screen","password")}
<div><label>Conveyor</label><select onchange="P[${i}].motor=+this.value"><option value="-1">None</option>
${M.map((m,j)=>`<option value="${j}" ${p.motor==j?"selected":""}>${esc(m.name)}</option>`).join("")}</select></div></div>
<div class="row" style="margin-top:10px"><button class="small" onclick="test(${i})">Test connection</button>
<button class="small" onclick="P.splice(${i},1);drawPrinters();drawFound()">Remove</button></div>
<div class="result" id="r${i}"></div></div>`).join(""):'<div class="muted">No printers yet.</div>'}
function drawMotors(){document.getElementById("mlist").innerHTML=M.map((m,j)=>`<div class="grid" style="margin-top:6px">
<div><label>Name</label><input value="${esc(m.name)}" oninput="M[${j}].name=this.value;drawPrinters()"></div>
<div><label>Connection</label><div class="row"><select onchange="M[${j}].pin=pinVal(this.value)">
${PINS.map(([k,label])=>`<option value="${k}" ${String(m.pin)==k?"selected":""}>${esc(label)}</option>`).join("")}
</select><button class="small" onclick="removeMotor(${j})">Remove</button></div></div></div>`).join("")}
function drawFound(){const el=document.getElementById("found");if(!FOUND.length){el.textContent=
"None heard yet. Keep this page open a minute, or add printers by hand.";return}
el.innerHTML=FOUND.map(f=>{const have=P.find(p=>(p.serial||"").toUpperCase()==f.serial);
const btn=!have?`<button onclick='addFound(${JSON.stringify(f.serial)})'>Add</button>`:
have.ip!=f.ip?`<button onclick='fixIp(${JSON.stringify(f.serial)})'>Update IP</button>`:'<span class="ok">Added</span>';
return `<div class="found"><div><b>${esc(f.name)}</b> &middot; ${esc(f.model)}<div class="muted">${esc(f.ip)} &middot; ${esc(f.serial)}</div></div>${btn}</div>`}).join("")}
function addPrinter(p){const used=new Set(P.map(x=>x.motor));let motor=M.findIndex((m,j)=>!used.has(j));
if(motor<0&&M.length)motor=0;
P.push({name:p.name||"",ip:p.ip||"",serial:p.serial||"",access_code:"",has_code:false,motor});drawPrinters();drawFound()}
function addFound(s){const f=FOUND.find(x=>x.serial==s);addPrinter(f);setTimeout(()=>{
const inputs=document.querySelectorAll('#plist input[type=password]');inputs[inputs.length-1].focus()},50)}
function fixIp(s){const f=FOUND.find(x=>x.serial==s);P.find(p=>p.serial.toUpperCase()==s).ip=f.ip;drawPrinters();drawFound()}
function pinVal(k){return k=="usb"?"usb":+k}
function addMotor(){const used=new Set(M.map(m=>String(m.pin)));const k=PINS.map(p=>p[0]).find(k=>!used.has(k));
const free=k==null?null:pinVal(k);
if(free==null){document.getElementById("msg").textContent="All free pins are in use.";return}
M.push({name:"Conveyor "+(M.length+1),pin:free});drawMotors();drawPrinters()}
function removeMotor(j){M.splice(j,1);P.forEach(p=>{if(p.motor==j)p.motor=-1;else if(p.motor>j)p.motor--});drawMotors();drawPrinters()}
async function test(i){const r=document.getElementById("r"+i);r.className="result muted";r.textContent="Testing... (up to 10 seconds)";
const d=await api("/api/test",{ip:P[i].ip,serial:P[i].serial,access_code:P[i].access_code});
r.className="result "+(d.ok?"ok":"bad");r.textContent=d.message}
async function save(){const m=document.getElementById("msg");m.className="result muted";m.textContent="Saving...";
const d=await api("/api/setup",{motors:M,printers:P});if(d.ok){m.className="result ok";m.textContent="Saved. Printers are connecting.";load()}
else{m.className="result bad";m.textContent=d.error}}
async function load(){const d=await api("/api/setup");P=d.printers;M=d.motors;PINS=d.pins;drawMotors();drawPrinters();drawFound()}
async function poll(){try{const d=await api("/api/discover");FOUND=d.printers;drawFound();
const m=document.getElementById("scanmsg");m.className="result muted";
m.textContent=d.scanning.length?"Scanning "+d.scanning.join(", ")+"... (about 20 seconds)":
d.networks.length?"Also checking: "+d.networks.join(", "):""}catch(e){}}
async function scan(){const d=await api("/api/scan",{network:document.getElementById("net").value});
if(!d.ok){const m=document.getElementById("scanmsg");m.className="result bad";m.textContent=d.error;return}poll()}
let SWV=null;
async function sw(){let d;try{d=await api("/api/update")}catch(e){return}
if(SWV&&d.current!=SWV){location.reload();return}SWV=d.current;
const when=d.checked?new Date(d.checked*1000).toLocaleString():"not yet";
document.getElementById("sw").innerHTML=`Version <b>${esc(d.current)}</b>`+
(d.busy?` &middot; ${esc(d.busy)}...`:d.available?` &middot; <b class="ok">${esc(d.latest)} is available</b>`:
d.latest?" &middot; up to date":"")+`<br>Last checked: ${esc(when)}`+(d.error?`<br><span class="bad">${esc(d.error)}</span>`:"");
document.getElementById("inst").style.display=d.available&&!d.busy?"":"none";
document.getElementById("autoupd").checked=!!d.auto}
async function upd(a){const d=await api("/api/update/"+a,{});if(d.message)document.getElementById("sw").textContent=d.message;
setTimeout(sw,a=="install"?3000:300)}
load();poll();sw();setInterval(poll,4000);setInterval(sw,5000);
</script></body></html>"""


SCREEN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Screen Settings</title>
<style>""" + STYLE + """
html,body{user-select:none;-webkit-user-select:none}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.opts button.sel{background:var(--accent);color:#fff;border:0}
.step{display:flex;align-items:center;gap:6px;margin-top:8px}.step button{flex:0 0 52px;padding:0;font-size:24px}
.step span{flex:1;text-align:center;font-weight:700}
input{font:inherit;width:100%;padding:9px;border-radius:9px;border:1px solid var(--line);
background:var(--bg);color:var(--text)}
.result{font-size:15px;margin-top:8px}.ok{color:var(--accent)}.bad{color:var(--warn)}
#confirm{display:none;position:sticky;bottom:8px;z-index:5;border:2px solid var(--accent)}
</style></head><body>
<p><a href="/">&larr; Back to controls</a></p>
<h1>Screen settings</h1>
<div class="card"><h2>Screen</h2>
<div class="muted">Pick the screen plugged into the Pi. After a change you have 20 seconds to press <b>Keep</b>,
or it switches back by itself. If the Pi's screen goes blank, just wait.</div>
<div id="detected" class="muted" style="margin-top:6px"></div>
<div class="opts" id="sizes"></div>
<div id="resmsg" class="result"></div></div>
<div class="card"><h2>Resolution</h2>
<div id="fixed" class="muted"></div>
<div id="resopts"><div class="muted">Must match your screen; most 5" HDMI screens for the Pi are 800&times;480.</div>
<div class="opts" id="res"></div>
<div class="row" style="margin-top:8px"><input id="custom" placeholder="Other size, e.g. 720x720">
<button style="flex:0 0 auto;padding:10px 16px;min-height:44px" onclick="setRes(document.getElementById('custom').value)">Use</button></div></div></div>
<div class="card"><h2>Zoom</h2>
<div class="muted">Makes everything on the Pi's screen bigger or smaller. Phones and PCs aren't affected.</div>
<div class="step"><button onclick="bumpZoom(-5)">&minus;</button><span id="zoom"></span><button onclick="bumpZoom(5)">+</button></div>
<button style="width:100%;margin-top:8px" onclick="save({zoom:0})">Automatic</button></div>
<div class="card"><h2>Rotation</h2><div class="muted" id="rot"></div>
<button style="width:100%;margin-top:8px" onclick="rotate()">Rotate 90&deg;</button></div>
<div class="card" id="confirm"><b>Keep this resolution?</b> <span id="left" class="muted"></span>
<div class="row" style="margin-top:8px"><button class="go" onclick="keep()">Keep</button>
<button onclick="setRes(null,true)">Switch back</button></div></div>
<script>""" + KIOSK_JS + """
let D=null;
async function api(path,body){const r=await fetch(path,body?{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function msg(t,cls){const m=document.getElementById("resmsg");m.className="result "+cls;m.textContent=t}
function draw(){
document.getElementById("sizes").innerHTML=Object.entries(D.sizes).map(([k,l])=>
`<button class="${D.size==k?"sel":""}" onclick="change({size:'${k}'})">${esc(l)}</button>`).join("");
document.getElementById("detected").textContent=D.detected?"Plugged in now: "+D.sizes[D.detected]:"";
document.getElementById("fixed").textContent=D.fixed?"This screen always runs at "+D.fixed.replace("x","\\u00d7")+".":"";
document.getElementById("resopts").style.display=D.fixed?"none":"";
document.getElementById("res").innerHTML=D.resolutions.map(r=>
`<button class="${D.resolution==r?"sel":""}" onclick="setRes('${r}')">${r.replace("x","&times;")}</button>`).join("")+
(D.resolutions.includes(D.resolution)?"":`<button class="sel">${esc(D.resolution)}</button>`);
document.getElementById("zoom").textContent=D.zoom?D.zoom+"%":"Automatic ("+D.auto_zoom+"%)";
document.getElementById("rot").textContent=D.rotation==null?"Unknown":D.rotation=="normal"?"Not turned":"Turned "+D.rotation+"\\u00b0";
const c=document.getElementById("confirm");c.style.display=D.pending!=null?"block":"none";
document.getElementById("left").textContent=D.pending!=null?"Switching back in "+D.pending+"s":"";
applyZoom(D.effective_zoom)}
async function load(){try{D=await api("/api/screen");draw()}catch(e){}}
async function save(b){const d=await api("/api/screen",b);D=d.screen;if(!d.ok)msg(d.message,"bad");draw()}
function bumpZoom(n){save({zoom:Math.max(50,Math.min(200,(D.zoom||D.auto_zoom)+n))})}
async function change(b,back){msg(back?"Switching back...":"Changing...","muted");
const d=await api(back?"/api/screen/revert":"/api/screen",back?{}:b);
D=d.screen;msg(d.message||"",d.ok?"ok":"bad");draw()}
function setRes(r,back){change({resolution:r},back)}
async function keep(){const d=await api("/api/screen/keep",{});D=d.screen;msg(d.message,"ok");draw()}
async function rotate(){const d=await api("/api/rotate",{});if(!d.ok)msg(d.message,"bad");load()}
load();setInterval(load,1000);
</script></body></html>"""


def lan_address():
    """The Pi's address on the home network (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "this Pi's IP address"


def _quiet(fn):
    """Run fn in a background thread; its failure is already logged and shown."""
    try:
        fn()
    except RuntimeError:
        pass


def restart_service():
    subprocess.Popen(["sudo", "-n", "systemctl", "restart", "conveyor"])


def local_name():
    """The Pi's name on the network (avahi answers <hostname>.local), which
    keeps working when the router hands the Pi a new IP address."""
    return socket.gethostname().split(".")[0] + ".local"


def build_app(cfg, ctl, discovery=None, scr=None, upd=None):
    app = Flask(__name__)
    settings = cfg["settings"]

    @app.get("/")
    def index():
        return Response(PAGE, mimetype="text/html")

    @app.get("/setup")
    def setup_page():
        return Response(SETUP_PAGE, mimetype="text/html")

    @app.get("/screen")
    def screen_page():
        return Response(SCREEN_PAGE, mimetype="text/html")

    @app.get("/api/status")
    def status():
        motors = [{"name": m.name, "running": m.running,
                   "left": max(0, int(m.until - time.monotonic()))} for m in ctl.motors]
        return jsonify(printers=[p.status() for p in ctl.printers], motors=motors,
                       settings=settings, version=STARTED, app_version=__version__,
                       addr=lan_address(), name=local_name(),
                       zoom=scr.zoom() if scr else 100,
                       update=upd.status() if upd else None)

    def pick_motor(data):
        if "motor" in data:
            i, motors = data["motor"], ctl.motors
            return motors[i] if isinstance(i, int) and 0 <= i < len(motors) else None
        p = ctl.printer(data.get("printer"))
        return p.motor if p else None

    @app.post("/api/run")
    def run():
        data = request.get_json(silent=True) or {}
        m = pick_motor(data)
        if m is None:
            return jsonify(error="unknown conveyor"), 400
        try:
            seconds = max(1, min(3600, int(data.get("seconds", 10))))
        except (TypeError, ValueError):
            return jsonify(error="bad seconds"), 400
        m.run(seconds, "manual")
        return jsonify(ok=True)

    @app.post("/api/stop")
    def stop():
        m = pick_motor(request.get_json(silent=True) or {})
        if m is None:
            return jsonify(error="unknown conveyor"), 400
        m.stop()
        return jsonify(ok=True)

    @app.post("/api/exit-kiosk")
    def exit_kiosk():
        # closes the full-screen browser on the Pi; the desktop icon brings it back
        subprocess.Popen(["pkill", "-f", "[c]hromium.*--kiosk"])
        return jsonify(ok=True)

    @app.post("/api/rotate")
    def rotate():
        try:
            return jsonify(ok=True, transform=screen.rotate(scr.config if scr else screen.KANSHI_CONFIG))
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log.warning("rotate failed: %s", e)
            return jsonify(ok=False, message=f"Couldn't rotate the screen: {e}")

    def no_screen():
        return jsonify(ok=False, message="Screen settings aren't available."), 503

    @app.get("/api/screen")
    def get_screen():
        return jsonify(scr.state()) if scr else no_screen()

    @app.post("/api/screen")
    def post_screen():
        if not scr:
            return no_screen()
        data = request.get_json(silent=True) or {}
        try:
            if "zoom" in data:
                scr.set_zoom(data["zoom"])
            if "size" in data or "resolution" in data:
                notes = scr.try_display(data.get("size"), data.get("resolution"))
                msg = " ".join(["Changed. Press Keep if the Pi's screen looks right."] + notes)
                return jsonify(ok=True, message=msg, screen=scr.state())
        except (OSError, ValueError) as e:
            return jsonify(ok=False, message=str(e), screen=scr.state())
        return jsonify(ok=True, screen=scr.state())

    @app.post("/api/screen/keep")
    def screen_keep():
        if not scr:
            return no_screen()
        scr.keep()
        return jsonify(ok=True, message="Kept.", screen=scr.state())

    @app.post("/api/screen/revert")
    def screen_revert():
        if not scr:
            return no_screen()
        try:
            scr.revert()
        except (OSError, ValueError) as e:
            return jsonify(ok=False, message=str(e), screen=scr.state())
        return jsonify(ok=True, message="Switched back.", screen=scr.state())

    @app.get("/api/update")
    def get_update():
        if not upd:
            return jsonify(current=__version__, available=False, auto=settings["auto_update"])
        return jsonify(**upd.status(), auto=settings["auto_update"])

    @app.post("/api/update/check")
    def update_check():
        if not upd:
            return jsonify(ok=False, message="Updates aren't available here.")
        return jsonify(ok=True, **upd.check())

    @app.post("/api/update/install")
    def update_install():
        if not upd:
            return jsonify(ok=False, message="Updates aren't available here.")
        if not upd.available:
            return jsonify(ok=False, message="Already up to date.")
        # install in the background: the service restarts when it's done
        threading.Thread(target=lambda: _quiet(upd.install), daemon=True).start()
        return jsonify(ok=True, message="Installing. The app restarts when it's done.")

    @app.post("/api/settings")
    def update_settings():
        data = request.get_json(silent=True) or {}
        with ctl.lock:
            for key in ("enabled", "auto_update"):
                if key in data:
                    settings[key] = bool(data[key])
            for key, (lo, hi) in SETTING_LIMITS.items():
                if key in data:
                    try:
                        settings[key] = max(lo, min(hi, int(data[key])))
                    except (TypeError, ValueError):
                        pass
            save_config(cfg)
        return jsonify(ok=True, settings=settings)

    @app.get("/api/setup")
    def get_setup():
        # access codes never leave the Pi; the page only learns whether one is saved
        printers = [{"name": p["name"], "ip": p["ip"], "serial": p["serial"],
                     "access_code": "", "has_code": bool(p["access_code"]),
                     "motor": p["motor"]} for p in cfg["printers"]]
        pins = [[USB_PIN, "Pi's own USB ports (all switch together)"]]
        pins += [[str(g), f"Pin {pin} (GPIO{g}) to a MOSFET or relay"] for g, pin in SAFE_PINS.items()]
        return jsonify(printers=printers, motors=cfg["motors"], pins=pins)

    @app.post("/api/setup")
    def post_setup():
        try:
            ctl.apply_setup(request.get_json(silent=True) or {})
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        return jsonify(ok=True)

    @app.post("/api/test")
    def test():
        data = request.get_json(silent=True) or {}
        try:
            ip = str(ipaddress.IPv4Address(str(data.get("ip", "")).strip()))
        except ValueError:
            return jsonify(ok=False, message="Enter a valid IP address first.")
        serial = str(data.get("serial", "")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{8,20}", serial):
            return jsonify(ok=False, message="Enter the serial number first.")
        code = str(data.get("access_code", "")).strip() or ctl.code_for(serial)
        if not code:
            return jsonify(ok=False, message="Enter the access code first.")
        return jsonify(test_printer(ip, serial, code))

    @app.get("/api/discover")
    def discover():
        if not discovery:
            return jsonify(printers=[], scanning=[], networks=[])
        return jsonify(printers=discovery.list(), scanning=sorted(discovery.scanning),
                       networks=cfg["scan_networks"])

    @app.post("/api/scan")
    def scan():
        try:
            net = str(parse_network((request.get_json(silent=True) or {}).get("network", "")))
        except ValueError as e:
            msg = str(e) if "home network" in str(e) else "Enter a network like 192.168.2.0/24."
            return jsonify(ok=False, error=msg)
        with ctl.lock:
            if net not in cfg["scan_networks"]:
                cfg["scan_networks"].append(net)
                save_config(cfg)
        discovery.scan(net)
        return jsonify(ok=True)

    return app


def hardware_test(cfg):
    if not PWMOutputDevice:
        sys.exit("gpiozero not available: run this on the Pi")
    for m in cfg["motors"]:
        dev = make_switch(m["pin"])
        where = "Pi USB ports" if m["pin"] == USB_PIN else f"GPIO{m['pin']}"
        print(f"{m['name']} ({where}) on for 3 seconds...")
        dev.value = 1
        time.sleep(3)
        dev.value = 0
        dev.close()
        time.sleep(1)
    print("Done.")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # skip per-request lines
    if not CONFIG_PATH.exists():
        sys.exit(f"No config found at {CONFIG_PATH}. "
                 "Copy config.example.json to config.json and fill it in.")
    cfg = load_config()
    if "--test" in sys.argv:
        hardware_test(cfg)
        return
    if not PWMOutputDevice:
        log.warning("gpiozero not available: motors are simulated")

    ctl = Controller(cfg)

    def shutdown(*_):
        # leave every belt off when the service stops (USB ports stay as last set)
        for m in ctl.motors:
            m.close()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def save():
        with ctl.lock:
            save_config(cfg)

    scr = screen.Screen(cfg, save)
    upd = updater.Updater(__version__, BASE, restart_service)
    upd.start(auto_install=lambda: cfg["settings"]["auto_update"],
              belts_idle=lambda: not any(m.running for m in ctl.motors))
    build_app(cfg, ctl, Discovery(cfg["scan_networks"]), scr, upd).run(
        host="0.0.0.0", port=WEB_PORT, threaded=True)


if __name__ == "__main__":
    main()
