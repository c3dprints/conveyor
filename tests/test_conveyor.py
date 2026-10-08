"""Simulated test of conveyor.py: fake printer messages, setup API, discovery."""
import json, os, shutil, sys, tempfile, time, types
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp())
cfg0 = json.loads((SRC / "config.example.json").read_text())
cfg0["printers"] = []
(tmp / "config.json").write_text(json.dumps(cfg0))
os.environ["CONVEYOR_CONFIG"] = str(tmp / "config.json")
sys.path.insert(0, str(SRC))
import conveyor as c

cfg = c.load_config()
s = cfg["settings"]
ctl = c.Controller(cfg)
m = ctl.motors[0]

# --- trigger logic on a fake (unconnected) printer ---
p = c.Printer.__new__(c.Printer)
p.name, p.motor, p.settings = "X1C", m, s
p.connected, p.error, p.gcode_state, p.stage, p.percent, p.last_periodic = True, "", "UNKNOWN", None, None, 0.0
p.client = types.SimpleNamespace(loop_stop=lambda: None, disconnect=lambda: None)
ctl.printers = [p]

def msg(d):
    p._on_message(None, None, types.SimpleNamespace(payload=json.dumps({"print": d}).encode()))

msg({"gcode_state": "IDLE", "stg_cur": -1}); assert not m.running
msg({"gcode_state": "RUNNING", "stg_cur": 1})
p.tick(); assert not m.running, "interval should not fire immediately"
p.last_periodic -= s["interval_minutes"] * 60
p.tick(); assert m.running, "interval should fire"; m.stop()
msg({"stg_cur": 4}); msg({"stg_cur": 0})
time.sleep(5.5); assert m.running, "filament change should fire"; m.stop()
msg({"gcode_state": "FINISH"}); assert m.running, "finish should fire"; m.stop()
s["enabled"] = False
msg({"gcode_state": "RUNNING"}); msg({"gcode_state": "FINISH"}); assert not m.running
s["enabled"] = True

# printer without a conveyor must not crash
p2 = c.Printer.__new__(c.Printer); p2.__dict__.update(p.__dict__); p2.motor = None
p2._run(5, "x"); assert p2.status()["motor"] is None

disc = c.Discovery.__new__(c.Discovery); disc.found = {}; disc.lock = c.threading.Lock()
disc.scanning = set(); disc.networks = cfg["scan_networks"]

# serial from a certificate: issuer CN first, subject CN (serial) second
def cn(v): return c.CN_OID + b"\x0c" + bytes([len(v)]) + v
der = b"\x30\x82junk" + cn(b"BBL Device CA") + b"\x30more" + cn(b"01S00A000000001") + b"\x30\x0dtail"
assert c.cert_serial(der) == "01S00A000000001"
assert c.cert_serial(b"nothing here") is None
assert str(c.parse_network("192.168.2")) == "192.168.2.0/24"
assert str(c.parse_network("192.168.2.77")) == "192.168.2.0/24"
for badnet in ["8.8.8.0/24", "10.0.0.0/8", "hello"]:
    try:
        c.parse_network(badnet); raise AssertionError(badnet)
    except ValueError:
        pass
disc._add("01S00A000000001", "192.168.2.18", None, "P1P")
assert disc.found["01S00A000000001"]["name"] == "P1P at 192.168.2.18"
disc.found.clear()
disc._parse(b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nServer: Buildroot/2018.02-rc3 UPnP/1.0 ssdp/1.0.0\r\n"
            b"Location: 192.168.1.41\r\nNT: urn:bambulab-com:device:3dprinter:1\r\nUSN: 01s00c000000001\r\n"
            b"DevModel.bambu.com: C11\r\nDevName.bambu.com: P1P-1\r\nDevConnect.bambu.com: lan\r\n\r\n", ("192.168.1.41", 2021))
disc._parse(b"M-SEARCH * HTTP/1.1\r\nST: ssdp:all\r\n\r\n", ("192.168.1.9", 1990))  # not a printer
found = disc.list()
assert len(found) == 1 and found[0]["serial"] == "01S00C000000001" and found[0]["model"] == "P1P" and found[0]["ip"] == "192.168.1.41"

app = c.build_app(cfg, ctl, disc).test_client()
assert b"Conveyor Control" in app.get("/").data
assert b"Printer setup" in app.get("/setup").data
assert app.get("/api/discover").get_json()["printers"][0]["name"] == "P1P-1"
assert not app.post("/api/scan", json={"network": "8.8.8.0/24"}).get_json()["ok"]
disc.scan = lambda net, wait=False: None  # don't hit the real network from the test
assert app.post("/api/scan", json={"network": "192.168.2"}).get_json()["ok"]
assert app.get("/api/discover").get_json()["networks"] == ["192.168.2.0/24"]
assert json.loads((tmp / "config.json").read_text())["scan_networks"] == ["192.168.2.0/24"]
assert app.post("/api/run", json={"printer": 0, "seconds": 5}).status_code == 200 and m.running
app.post("/api/stop", json={"printer": 0}); assert not m.running
assert app.post("/api/run", json={"printer": 9}).status_code == 400
assert app.post("/api/run", json={"motor": 0, "seconds": 3600}).status_code == 200 and m.running
st = app.get("/api/status").get_json()["motors"][0]
assert st["running"] and 3500 < st["left"] <= 3600, st
app.post("/api/stop", json={"motor": 0}); assert not m.running
assert app.post("/api/run", json={"motor": 7}).status_code == 400
assert app.post("/api/run", json={"motor": 0, "seconds": 99999}).status_code == 200
assert m.until - c.time.monotonic() <= 3600; m.stop()
r = app.post("/api/settings", json={"run_seconds": 999, "speed_percent": "x"}).get_json()
assert r["settings"]["run_seconds"] == 120 and r["settings"]["speed_percent"] == 100

# --- setup validation ---
good = {"motors": [{"name": "Belt A", "pin": 5}, {"name": "Belt B", "pin": 6}],
        "printers": [{"name": "P1P 1", "ip": "127.0.0.1", "serial": "01s00c000000001", "access_code": "abcd1234", "motor": 0},
                     {"name": "P1P 2", "ip": "127.0.0.1", "serial": "01S00C000000002", "access_code": "zzzz9999", "motor": 1}]}
def bad(mut, expect):
    d = json.loads(json.dumps(good)); mut(d)
    r = app.post("/api/setup", json=d).get_json()
    assert not r["ok"] and expect in r["error"], r
bad(lambda d: d["printers"][0].update(ip="192.168.1"), "IP address")
bad(lambda d: d["printers"][0].update(serial="x!"), "serial")
bad(lambda d: d["printers"][1].update(serial="01S00C000000001"), "twice")
bad(lambda d: d["printers"][0].update(access_code=""), "access code")
bad(lambda d: d["motors"][1].update(pin=5), "already used")
bad(lambda d: d["motors"][0].update(pin=17), "pick a connection")
assert app.post("/api/setup", json=good).get_json()["ok"]
assert len(ctl.printers) == 2 and ctl.printers[1].motor.name == "Belt B"
saved = json.loads((tmp / "config.json").read_text())
assert saved["printers"][0]["serial"] == "01S00C000000001" and saved["printers"][0]["access_code"] == "abcd1234"

# access codes never go to the browser; blank keeps the saved one
g = app.get("/api/setup").get_json()
assert "abcd1234" not in json.dumps(g) and g["printers"][0]["has_code"] is True
g["printers"][0]["motor"] = -1
assert app.post("/api/setup", json={"motors": g["motors"], "printers": g["printers"]}).get_json()["ok"]
saved = json.loads((tmp / "config.json").read_text())
assert saved["printers"][0]["access_code"] == "abcd1234" and saved["printers"][1]["access_code"] == "zzzz9999"
assert ctl.printers[0].motor is None

# test-connection endpoint: bad input and an unreachable printer
assert "valid IP" in app.post("/api/test", json={"ip": "nope"}).get_json()["message"]
t = app.post("/api/test", json={"ip": "127.0.0.1", "serial": "01S00C000000001"}).get_json()
assert not t["ok"] and "127.0.0.1" in t["message"], t

# --- Pi USB ports as a conveyor ---
calls = []
c.PiUsbPower._uhubctl = lambda self, hub, action: calls.append((hub, action))
u = c.PiUsbPower()
assert calls == [("2", "off"), ("1-1", "off")], calls  # starts off
calls.clear(); u.value = 0.8; u.value = 1.0
assert calls == [("1-1", "on"), ("2", "on")], calls  # on once, USB2 side first
calls.clear(); u.value = 0; u.value = 0
assert calls == [("2", "off"), ("1-1", "off")], calls  # off once
g = app.get("/api/setup").get_json()
assert g["pins"][0][0] == "usb"
usb = {"motors": [{"name": "Pi USB", "pin": "usb"}],
       "printers": [{"name": "A", "ip": "127.0.0.1", "serial": "01S00C000000001", "access_code": "", "motor": 0},
                    {"name": "B", "ip": "127.0.0.1", "serial": "01S00C000000002", "access_code": "", "motor": 0}]}
r = app.post("/api/setup", json=usb).get_json(); assert r["ok"], r
assert ctl.printers[0].motor is ctl.printers[1].motor  # two printers share the one belt
assert json.loads((tmp / "config.json").read_text())["motors"] == [{"name": "Pi USB", "pin": "usb"}]
two = json.loads(json.dumps(usb)); two["motors"].append({"name": "again", "pin": "usb"})
r = app.post("/api/setup", json=two).get_json(); assert not r["ok"] and "USB ports are already" in r["error"], r
bogus = json.loads(json.dumps(usb)); bogus["motors"][0]["pin"] = "hdmi"
assert not app.post("/api/setup", json=bogus).get_json()["ok"]

for pr in ctl.printers:
    pr.close()
print("ALL TESTS PASSED")
