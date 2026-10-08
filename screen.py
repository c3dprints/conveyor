"""The Pi's touchscreen: which screen (5" HDMI or 4.3" DSI), resolution,
zoom and rotation.

The desktop applies kanshi's config at login, so the app writes it in full
from these settings; changes are also applied live with wlr-randr. The boot
resolution (video= in cmdline.txt) needs root, so it goes through the small
conveyor-video-mode helper that install.sh puts in /usr/local/sbin.
"""
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("conveyor")

KANSHI_CONFIG = Path.home() / ".config" / "kanshi" / "config"
VIDEO_HELPER = "/usr/local/sbin/conveyor-video-mode"
TRANSFORMS = ["normal", "90", "180", "270"]
# fixed: the panel's own resolution, for screens that can't change it
SCREENS = {
    "5": {"label": '5" HDMI screen', "output": "HDMI-A-1", "fixed": None},
    "4.3": {"label": '4.3" DSI screen (ribbon cable)', "output": "DSI-1", "fixed": "800x480"},
}
RESOLUTIONS = ["480x272", "480x320", "640x480", "800x480", "1024x600", "1280x720", "1280x800"]
DEFAULT_SCREEN = {"size": "5", "resolution": "800x480", "zoom": 0}  # zoom 0 = automatic
ZOOM_LIMITS = (50, 200)
CONFIRM_SECONDS = 20


def parse_resolution(text):
    m = re.fullmatch(r"\s*(\d{3,4})\s*[xX×]\s*(\d{3,4})\s*", str(text or ""))
    if not m:
        raise ValueError("Use a size like 800x480.")
    w, h = int(m.group(1)), int(m.group(2))
    if not (320 <= w <= 1920 and 240 <= h <= 1200):
        raise ValueError("Pick a width of 320-1920 and a height of 240-1200.")
    return f"{w}x{h}"


def auto_zoom(size, resolution):
    """Keep the controls about as big as on a 5" 800x480 screen: smaller
    screens and higher resolutions get a bigger zoom."""
    w, h = (int(n) for n in resolution.split("x"))
    zoom = 100 * min(w, h) / 480 * 5 / float(size)
    return max(ZOOM_LIMITS[0], min(ZOOM_LIMITS[1], int(round(zoom / 5) * 5)))


def _wayland_env():
    env = dict(os.environ)
    uid = os.getuid() if hasattr(os, "getuid") else 1000  # no getuid on Windows (tests)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    return env


def _randr(*args):
    return subprocess.run(["wlr-randr", *args], env=_wayland_env(), check=True,
                          timeout=10, capture_output=True, text=True)


def connected_outputs():
    """Names of the screens the desktop sees right now ([] if unknown)."""
    try:
        out = _randr().stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []
    return re.findall(r'^(\S+) "', out, re.M)


def detect_size(outputs):
    """A DSI screen only shows up when one is plugged in; HDMI can be forced
    on by the boot setting even with nothing attached."""
    for size, info in SCREENS.items():
        if info["fixed"] and info["output"] in outputs:
            return size
    return None


def read_display(config=KANSHI_CONFIG):
    """{"output", "resolution", "transform"} of the first enabled output in
    kanshi's config; None where unknown."""
    try:
        text = config.read_text()
    except OSError:
        text = ""
    line = re.search(r"^\s*output\s+(\S+)\s+enable\b.*$", text, re.M)
    if not line:
        return {"output": None, "resolution": None, "transform": None}
    mode = re.search(r"\bmode\s+(?:--custom\s+)?(\d+x\d+)", line.group(0))
    turn = re.search(r"\btransform\s+(\S+)", line.group(0))
    return {"output": line.group(1), "resolution": mode.group(1) if mode else None,
            "transform": turn.group(1) if turn else "normal"}


def write_kanshi(config, size, resolution, transform):
    """One profile for the chosen screen alone, one with the other screen
    connected too (turned off), so the controls never land on the wrong one."""
    info = SCREENS[size]
    mode = "" if info["fixed"] else f" mode --custom {resolution}@60"
    line = f"\toutput {info['output']} enable scale 1.000000{mode} position 0,0 transform {transform}"
    others = [s["output"] for k, s in SCREENS.items() if k != size]
    text = ("# Written by the conveyor app (Screen settings).\n"
            f"profile conveyor {{\n{line}\n}}\n")
    for other in others:
        text += f"profile conveyor-{other.lower()}-off {{\n{line}\n\toutput {other} disable\n}}\n"
    config.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, config)


def rotate(config=KANSHI_CONFIG):
    """Turn the screen 90 degrees clockwise, right away and for the next boot.
    Touch follows, since labwc maps it to the output."""
    cur = read_display(config)
    if not cur["output"]:
        raise ValueError(f"no screen set up in {config}")
    turn = cur["transform"]
    new = TRANSFORMS[(TRANSFORMS.index(turn) + 1) % 4] if turn in TRANSFORMS else "normal"
    _randr("--output", cur["output"], "--transform", new)
    text = config.read_text()
    text = re.sub(r"(\boutput\s+\S+\s+enable\b[^\n]*?)\btransform\s+\S+", rf"\g<1>transform {new}", text)
    if "transform" not in text:
        text = re.sub(r"(\boutput\s+\S+\s+enable\b[^\n]*)", rf"\g<1> transform {new}", text)
    tmp = config.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, config)
    log.info("screen rotated to %s", new)
    return new


def apply_display(size, resolution, config=KANSHI_CONFIG):
    """Switch to a screen and resolution live, at login and at boot. Returns
    notes about the parts that didn't work (the rest still applies)."""
    info = SCREENS[size]
    transform = read_display(config)["transform"] or "270"
    notes = []
    args = ["--output", info["output"], "--on", "--pos", "0,0", "--transform", transform]
    if not info["fixed"]:
        args += ["--custom-mode", f"{resolution}@60Hz"]
    live = connected_outputs()
    for k, other in SCREENS.items():
        if k != size and other["output"] in live:
            args += ["--output", other["output"], "--off"]
    try:
        _randr(*args)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("live screen change failed: %s", e)
        notes.append("The screen changes after the Pi restarts.")
    write_kanshi(config, size, resolution, transform)
    # HDMI is forced on at boot so a screen without proper EDID gets its mode;
    # with a DSI screen that boot setting only adds a phantom screen
    boot = "none" if info["fixed"] else resolution
    try:
        r = subprocess.run(["sudo", "-n", VIDEO_HELPER, boot],
                           capture_output=True, text=True, timeout=15, check=False)
        failed = r.returncode != 0
    except (OSError, subprocess.SubprocessError):
        failed = True
    if failed:
        log.warning("boot screen setting not updated (helper missing?)")
        notes.append("The boot setting wasn't updated: run sudo bash install.sh once.")
    log.info("screen set to %s at %s", info["label"], resolution)
    return notes


class Screen:
    """Screen settings saved in config.json under "screen". A new screen or
    resolution switches back by itself after CONFIRM_SECONDS unless it is
    kept, so a choice the panel can't show never leaves it blank."""

    def __init__(self, cfg, save, config=KANSHI_CONFIG):
        self.s = {**DEFAULT_SCREEN, **cfg.get("screen", {})}
        if "screen" not in cfg:  # first run: start from what is plugged in now
            self.s["size"] = detect_size(connected_outputs()) or self.s["size"]
            self.s["resolution"] = (SCREENS[self.s["size"]]["fixed"]
                                    or read_display(config)["resolution"] or self.s["resolution"])
        cfg["screen"] = self.s
        self.save = save
        self.config = config
        self.lock = threading.Lock()
        self.previous = None  # (size, resolution) to switch back to while unconfirmed
        self.deadline = 0.0
        self.timer = None

    def zoom(self):
        return self.s["zoom"] or auto_zoom(self.s["size"], self.s["resolution"])

    def state(self):
        left = max(0, int(self.deadline - time.monotonic() + 0.99)) if self.previous else None
        live = connected_outputs()
        return {**self.s, "auto_zoom": auto_zoom(self.s["size"], self.s["resolution"]),
                "effective_zoom": self.zoom(),
                "sizes": {k: v["label"] for k, v in SCREENS.items()},
                "fixed": SCREENS[self.s["size"]]["fixed"], "resolutions": RESOLUTIONS,
                "detected": detect_size(live), "rotation": read_display(self.config)["transform"],
                "pending": left}

    def set_zoom(self, value):
        try:
            zoom = int(value)
        except (TypeError, ValueError):
            raise ValueError("Zoom must be a number.")
        with self.lock:
            self.s["zoom"] = 0 if zoom == 0 else max(ZOOM_LIMITS[0], min(ZOOM_LIMITS[1], zoom))
            self.save()

    def try_display(self, size=None, resolution=None):
        """Switch screen and/or resolution, pending confirmation."""
        size = self.s["size"] if size is None else str(size)
        if size not in SCREENS:
            raise ValueError("Unknown screen size.")
        fixed = SCREENS[size]["fixed"]
        if fixed:
            if resolution is not None and parse_resolution(resolution) != fixed:
                raise ValueError(f"This screen always runs at {fixed}.")
            resolution = fixed
        elif resolution is None:
            resolution = self.s["resolution"] if not SCREENS[self.s["size"]]["fixed"] else "800x480"
        else:
            resolution = parse_resolution(resolution)
        with self.lock:
            notes = apply_display(size, resolution, self.config)
            if self.previous is None:
                self.previous = (self.s["size"], self.s["resolution"])
            self.s["size"], self.s["resolution"] = size, resolution
            self.save()
            if self.timer:
                self.timer.cancel()
            if self.previous == (size, resolution):  # back to the original: nothing to confirm
                self.previous = self.timer = None
                return notes
            self.deadline = time.monotonic() + CONFIRM_SECONDS
            self.timer = threading.Timer(CONFIRM_SECONDS, self.revert)
            self.timer.daemon = True
            self.timer.start()
            return notes

    def keep(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.previous = self.timer = None

    def revert(self):
        with self.lock:
            if self.previous is None:
                return
            (size, resolution), self.previous = self.previous, None
            if self.timer:
                self.timer.cancel()
            self.timer = None
            apply_display(size, resolution, self.config)
            self.s["size"], self.s["resolution"] = size, resolution
            self.save()
            log.info("screen switched back to %s at %s", SCREENS[size]["label"], resolution)
