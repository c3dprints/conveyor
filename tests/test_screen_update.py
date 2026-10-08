"""Tests for screen settings and GitHub updates, with no Pi needed: display
commands are faked, and updates install from a local git repo."""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
import screen, updater

tmp = Path(tempfile.mkdtemp())
REAL_RUN = subprocess.run  # screen.subprocess is the shared module: restore from this

# --- screen: resolution parsing and automatic zoom ---
assert screen.parse_resolution(" 800 x 480 ") == "800x480"
assert screen.parse_resolution("1024×600") == "1024x600"
for bad in ("800", "abcxdef", "100x100", "9999x480", None):
    try:
        screen.parse_resolution(bad); raise AssertionError(bad)
    except ValueError:
        pass
assert screen.auto_zoom("5", "800x480") == 100
assert screen.auto_zoom("4.3", "800x480") == 115
assert screen.auto_zoom("4.3", "480x272") == 65
assert screen.auto_zoom("5", "1280x800") == 165

# --- screen: changes go to kanshi, wlr-randr and the boot helper ---
calls = []
LIVE = ["HDMI-A-1", "DSI-1"]  # what wlr-randr reports as plugged in
def fake_run(cmd, **k):
    calls.append(cmd)
    out = "".join(f'{o} "(null)"\n  Enabled: yes\n' for o in LIVE) if cmd == ["wlr-randr"] else ""
    return subprocess.CompletedProcess(cmd, 0, out, "")
screen.subprocess.run = fake_run
kanshi = tmp / "kanshi"
kanshi.write_text("profile {\n\toutput HDMI-A-1 enable scale 1.000000 mode --custom 800x480@60 position 0,0 transform 270\n}\n")
assert screen.read_display(kanshi) == {"output": "HDMI-A-1", "resolution": "800x480", "transform": "270"}
assert screen.connected_outputs() == ["HDMI-A-1", "DSI-1"]
assert screen.detect_size(["HDMI-A-1", "DSI-1"]) == "4.3" and screen.detect_size(["HDMI-A-1"]) is None

# first run with a DSI screen plugged in picks the 4.3" screen (setting only)
cfg, saves = {}, []
s = screen.Screen(cfg, lambda: saves.append(json.dumps(cfg)), config=kanshi)
assert cfg["screen"]["size"] == "4.3" and cfg["screen"]["resolution"] == "800x480" and s.zoom() == 115
LIVE = ["HDMI-A-1"]
cfg, saves = {}, []
s = screen.Screen(cfg, lambda: saves.append(json.dumps(cfg)), config=kanshi)
assert cfg["screen"]["size"] == "5" and s.zoom() == 100

s.set_zoom(130); assert s.zoom() == 130 and saves
s.set_zoom(999); assert s.zoom() == 200
s.set_zoom(0); assert s.zoom() == 100
for bad in ("big", None):
    try:
        s.set_zoom(bad); raise AssertionError(bad)
    except ValueError:
        pass
try:
    s.try_display(size="7"); raise AssertionError("size 7")
except ValueError:
    pass

screen.CONFIRM_SECONDS = 0.5
calls.clear()
notes = s.try_display(resolution="1024x600")
assert notes == [], notes
assert ["sudo", "-n", screen.VIDEO_HELPER, "1024x600"] in calls
assert any(c[:1] == ["wlr-randr"] and "1024x600@60Hz" in c for c in calls)
assert "output HDMI-A-1 enable scale 1.000000 mode --custom 1024x600@60 position 0,0 transform 270" in kanshi.read_text()
assert "output DSI-1 disable" in kanshi.read_text()
st = s.state()
assert st["resolution"] == "1024x600" and st["pending"] is not None and st["rotation"] == "270" and st["fixed"] is None
time.sleep(1.0)  # not kept: switches back by itself
assert s.state()["resolution"] == "800x480" and s.state()["pending"] is None
assert "mode --custom 800x480@60" in kanshi.read_text()

s.try_display(resolution="480x272"); s.keep(); time.sleep(0.8)
assert s.state()["resolution"] == "480x272" and s.state()["pending"] is None
s.try_display(resolution="800x480"); s.try_display(resolution="1024x600"); s.revert()
assert s.state()["resolution"] == "480x272", "switch back goes to the last kept resolution"
s.try_display(resolution="800x480"); s.keep()

# switch to the 4.3" DSI screen: HDMI off live, no forced HDMI at boot
LIVE = ["HDMI-A-1", "DSI-1"]
calls.clear()
s.try_display(size="4.3"); s.keep()
st = s.state()
assert st["size"] == "4.3" and st["resolution"] == "800x480" and st["fixed"] == "800x480" and st["detected"] == "4.3"
assert ["sudo", "-n", screen.VIDEO_HELPER, "none"] in calls
randr = next(c for c in calls if c[:1] == ["wlr-randr"] and "--on" in c)
assert randr[randr.index("--output") + 1] == "DSI-1" and "--custom-mode" not in randr
assert randr[-3:] == ["--output", "HDMI-A-1", "--off"]
text = kanshi.read_text()
assert "output DSI-1 enable scale 1.000000 position 0,0 transform 270" in text
assert "output HDMI-A-1 disable" in text and "mode" not in text
try:
    s.try_display(resolution="1024x600"); raise AssertionError("DSI resolution changed")
except ValueError as e:
    assert "800x480" in str(e)
assert screen.read_display(kanshi)["output"] == "DSI-1"

assert screen.rotate(kanshi) == "normal"
assert kanshi.read_text().count("transform normal") == 2 and "transform 270" not in kanshi.read_text()
assert any(c[:1] == ["wlr-randr"] and "DSI-1" in c and "normal" in c for c in calls)
s.try_display(size="5", resolution="800x480"); s.keep()
assert screen.read_display(kanshi) == {"output": "HDMI-A-1", "resolution": "800x480", "transform": "normal"}

def failing_run(cmd, **k):
    if cmd[0] == "sudo":
        return subprocess.CompletedProcess(cmd, 1, "", "no sudo")
    raise FileNotFoundError("wlr-randr")
screen.subprocess.run = failing_run
notes = screen.apply_display("5", "640x480", kanshi)
assert len(notes) == 2 and "640x480" in kanshi.read_text(), notes
screen.subprocess.run = REAL_RUN

# --- updater: version order ---
assert updater.version_key("v1.0.10") > updater.version_key("v1.0.9") > updater.version_key("1.0")

# --- updater: install a release from a local git repo ---
def git(cwd, *a):
    REAL_RUN(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)

repo = tmp / "repo"; repo.mkdir()
git(repo, "init", "-q"); git(repo, "config", "user.email", "t@example.com"); git(repo, "config", "user.name", "t")
(repo / ".gitignore").write_text("config.json\n")
(repo / "conveyor.py").write_text('__version__ = "1.0.2"\n')
git(repo, "add", "."); git(repo, "commit", "-qm", "1.0.2"); git(repo, "tag", "v1.0.2")
(repo / "conveyor.py").write_text('__version__ = "1.0.3"\n')
git(repo, "commit", "-qam", "1.0.3"); git(repo, "tag", "v1.0.3")

app = tmp / "app"; app.mkdir()  # a copied-in install, not a git clone yet
(app / "conveyor.py").write_text('__version__ = "1.0.1"\n')
(app / "config.json").write_text('{"secret": "keep me"}')
restarts = []
u = updater.Updater("1.0.1", app, lambda: restarts.append(1), repo=str(repo))
st = u.check()
assert st["latest"] == "1.0.3" and st["available"] and not st["error"], st
u.install()
assert restarts == [1]
assert '"1.0.3"' in (app / "conveyor.py").read_text()
assert json.loads((app / "config.json").read_text()) == {"secret": "keep me"}, "config must survive"

# a release that doesn't compile is rolled back and not restarted
(repo / "conveyor.py").write_text("def broken(:\n")
git(repo, "commit", "-qam", "bad"); git(repo, "tag", "v1.0.4")
u2 = updater.Updater("1.0.3", app, lambda: restarts.append(2), repo=str(repo))
assert u2.check()["latest"] == "1.0.4"
try:
    u2.install(); raise AssertionError("broken release installed")
except RuntimeError as e:
    assert "didn't compile" in str(e)
assert '"1.0.3"' in (app / "conveyor.py").read_text() and restarts == [1]
assert u2.failed == "v1.0.4"

u3 = updater.Updater("1.0.1", app, lambda: None, repo=str(tmp / "missing"))
assert "failed" in u3.check()["error"] and not u3.available

# --- web API wiring ---
os.environ["CONVEYOR_CONFIG"] = str(tmp / "config.json")
(tmp / "config.json").write_text(json.dumps({"motors": [], "printers": [], "settings": {}}))
import conveyor
screen.subprocess.run = fake_run
ccfg = conveyor.load_config()
assert ccfg["settings"]["auto_update"] is False
ctl = conveyor.Controller(ccfg)
scr = screen.Screen(ccfg, lambda: conveyor.save_config(ccfg), config=kanshi)
upd = updater.Updater(conveyor.__version__, app, lambda: None, repo=str(repo))
web = conveyor.build_app(ccfg, ctl, None, scr, upd).test_client()
assert web.get("/screen").status_code == 200
assert "location.href='/setup'" in web.get("/").get_data(as_text=True)
assert web.get("/api/status").get_json()["zoom"] == scr.zoom()
r = web.post("/api/screen", json={"size": "4.3"}).get_json()
assert r["ok"] and r["screen"]["size"] == "4.3"
assert json.loads((tmp / "config.json").read_text())["screen"]["size"] == "4.3"
r = web.post("/api/screen", json={"resolution": "1024x600"}).get_json()
assert not r["ok"] and "always runs at 800x480" in r["message"]
r = web.post("/api/screen", json={"size": "5", "resolution": "nope"}).get_json()
assert not r["ok"] and "800x480" in r["message"]
r = web.post("/api/screen", json={"size": "5", "resolution": "480x320"}).get_json()
assert r["ok"] and r["screen"]["pending"] is not None and r["screen"]["resolution"] == "480x320"
r = web.post("/api/screen", json={"zoom": 120}).get_json()
assert r["ok"] and r["screen"]["zoom"] == 120 and r["screen"]["pending"] is not None
assert web.post("/api/screen/keep", json={}).get_json()["screen"]["pending"] is None
web.post("/api/settings", json={"auto_update": True})
assert web.get("/api/update").get_json()["auto"] is True
assert web.post("/api/update/install", json={}).get_json()["message"] == "Already up to date."
nocfg = conveyor.build_app(ccfg, ctl).test_client()
assert nocfg.get("/api/screen").status_code == 503
assert nocfg.get("/api/status").get_json()["update"] is None
for m in ctl.motors:
    m.close()
print("ALL SCREEN/UPDATE TESTS PASSED")
