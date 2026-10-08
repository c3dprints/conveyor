"""Updates from GitHub. Each release is a tag like v1.0.2 on the repo; the Pi
checks the tags, and installing one checks it out over the app folder (the
git-ignored config.json is never touched), compiles it, then restarts.
"""
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("conveyor")

REPO_URL = "https://github.com/c3dprints/conveyor.git"
CHECK_EVERY = 6 * 3600
RETRY_EVERY = 600  # auto-install waits while a belt is running


def version_key(text):
    nums = re.findall(r"\d+", str(text or ""))[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


class Updater:
    def __init__(self, current, base, restart, repo=REPO_URL):
        self.current = current
        self.base = Path(base)
        self.restart = restart
        self.repo = repo
        self.latest = None
        self.checked = None  # time.time() of the last good check
        self.error = ""
        self.busy = ""  # "checking" / "installing" while running
        self.failed = None  # release that failed to install: not retried automatically
        self.lock = threading.Lock()

    def _git(self, *args, timeout=60):
        r = subprocess.run(["git", "-C", str(self.base), *args],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            lines = (r.stderr or r.stdout).strip().splitlines()
            raise RuntimeError(lines[-1] if lines else "git failed")
        return r.stdout

    @property
    def available(self):
        return bool(self.latest) and version_key(self.latest) > version_key(self.current)

    def status(self):
        return {"current": self.current, "latest": (self.latest or "").lstrip("v") or None,
                "available": self.available, "checked": self.checked,
                "error": self.error, "busy": self.busy}

    def check(self):
        if not self.lock.acquire(blocking=False):
            return self.status()
        self.busy = "checking"
        try:
            out = subprocess.run(["git", "ls-remote", "--tags", "--refs", self.repo],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                raise RuntimeError("couldn't reach GitHub")
            tags = re.findall(r"refs/tags/(v\d+\.\d+\.\d+)$", out.stdout, re.M)
            self.latest = max(tags, key=version_key) if tags else None
            self.checked = time.time()
            self.error = ""
            if self.available:
                log.info("update available: %s", self.latest)
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            self.error = f"Update check failed: {e}"
            log.warning(self.error)
        finally:
            self.busy = ""
            self.lock.release()
        return self.status()

    def install(self):
        """Fetch and check out the latest release, then restart the service.
        If the new code doesn't compile, the old version is put back."""
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("An update is already running.")
        self.busy = "installing"
        tag = self.latest
        try:
            if not tag:
                raise RuntimeError("No release found yet. Check for updates first.")
            if not (self.base / ".git").exists():  # first update of a copied-in folder
                self._git("init", "-q")
                self._git("remote", "add", "origin", self.repo)
            else:
                self._git("remote", "set-url", "origin", self.repo)
            self._git("fetch", "-q", "--force", "--tags", "origin", timeout=180)
            try:
                previous = self._git("rev-parse", "HEAD").strip()
            except RuntimeError:
                previous = None
            self._git("checkout", "-q", "-f", tag)
            files = [str(p) for p in self.base.glob("*.py")]
            r = subprocess.run([sys.executable, "-m", "py_compile", *files],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                if previous:
                    self._git("checkout", "-q", "-f", previous)
                raise RuntimeError("the new version didn't compile, kept the old one")
            log.info("updated to %s, restarting", tag)
            self.error = ""
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            self.failed = tag
            self.error = f"Update failed: {e}"
            log.warning(self.error)
            raise RuntimeError(self.error)
        finally:
            self.busy = ""
            self.lock.release()
        self.restart()

    def start(self, auto_install, belts_idle):
        """Check now and every CHECK_EVERY seconds; with auto-install on,
        install a new release once no belt is running."""
        def loop():
            next_check = 0.0
            while True:
                if time.monotonic() >= next_check:
                    self.check()
                    next_check = time.monotonic() + CHECK_EVERY
                if (self.available and self.latest != self.failed
                        and auto_install() and belts_idle()):
                    try:
                        self.install()
                    except RuntimeError:
                        pass
                time.sleep(RETRY_EVERY)
        threading.Thread(target=loop, daemon=True).start()
