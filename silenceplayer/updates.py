"""Update check: asks GitHub whether a newer stable release exists.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import NamedTuple

from . import APP_VERSION
from .logs import log, log_throttled
from .paths import log_dir

GITHUB_REPO    = "ELSEGuy/silenceplayer"


UPDATE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


RELEASES_URL   = f"https://github.com/{GITHUB_REPO}/releases/latest"


# For testing the notice: run with this set to an older version (e.g. 1.2.0).
PRETEND_VERSION_ENV = "SILENCEPLAYER_PRETEND_VERSION"


_VERSION_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", re.IGNORECASE)


def parse_version(text: str | None) -> tuple[tuple[int, ...], bool] | None:
    """'v1.3.0' → ((1, 3, 0), True). The flag is False for anything with a suffix
    ('1.4.0-dev', 'v1.3-beta'): those are not stable releases. None if unreadable."""
    m = _VERSION_RE.match(text or "") if isinstance(text, str) else None
    if not m:
        return None
    nums = tuple(int(g) if g else 0 for g in m.groups()[:3])
    return nums, m.group(4).strip() == ""


def is_newer_release(tag: str | None, current: str) -> bool:
    """True if `tag` is a stable release newer than the running `current` version.
    A test build like 1.4.0-dev counts as older than the 1.4.0 release."""
    latest, cur = parse_version(tag), parse_version(current)
    if latest is None or cur is None or not latest[1]:
        return False
    return latest[0] + (1,) > cur[0] + (1 if cur[1] else 0,)


def current_version() -> str:
    """APP_VERSION, unless a pretend version is set for testing the update notice."""
    return os.environ.get(PRETEND_VERSION_ENV, "").strip() or APP_VERSION


def update_state_file() -> str:
    return os.path.join(log_dir(), "update.json")


class ReleaseInfo(NamedTuple):
    version: str   # as shown to the user, e.g. "1.4.1"
    tag: str       # e.g. "v1.4.1"
    url: str       # the release page on GitHub


def fetch_latest_release(timeout: float = 10.0) -> ReleaseInfo | None:
    """The newest stable release on GitHub, or None if there isn't a usable one.

    GitHub's "latest" endpoint already skips drafts and pre-releases (like
    v1.3-beta). Raises on network or HTTP errors.
    """
    import urllib.request
    req = urllib.request.Request(UPDATE_API_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"SilencePlayer/{APP_VERSION}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read(1_000_000).decode("utf-8"))
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        return None
    tag = tag.strip()
    url = data.get("html_url")
    # Only ever open a page of this repository's releases.
    if not (isinstance(url, str) and url.startswith(f"https://github.com/{GITHUB_REPO}/releases/")):
        url = RELEASES_URL
    return ReleaseInfo(tag.lstrip("vV"), tag, url)


class UpdateChecker:
    """Checks GitHub for a newer stable release in the background.

    The first check runs shortly after start, then every few hours while the
    app runs. Nothing is downloaded or installed: `on_result(info, error,
    manual)` is called and the UI offers to open the release page.
    "Skip this version" is remembered in update.json next to the log (it is
    not a setting, so it doesn't wait for Save Settings).
    """

    FIRST_DELAY         = 15.0
    INTERVAL            = 6 * 3600.0
    RETRY_AFTER_FAILURE = 30 * 60.0

    def __init__(self, current, on_result, enabled=lambda: True, fetch=None,
                 state_path=None, clock=time.time):
        self.current = current
        self._on_result = on_result
        self._enabled = enabled
        self._fetch = fetch or (lambda: fetch_latest_release())
        self._state_path = state_path or update_state_file()
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._manual = False
        self._thread = None
        self.last_checked = None
        self.last_error = None
        self.skipped = self._load_skipped()

    # ── skipped version ──

    def _load_skipped(self):
        try:
            with open(self._state_path, encoding="utf-8") as f:
                value = json.load(f).get("skipped")
            return value if isinstance(value, str) else None
        except FileNotFoundError:
            return None
        except Exception:
            log.warning("Could not read %s; ignoring it", self._state_path, exc_info=True)
            return None

    def skip(self, tag: str) -> None:
        self.skipped = tag
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump({"skipped": tag}, f)
        except Exception:
            log.warning("Could not save the skipped version", exc_info=True)
        log.info("User skipped update %s", tag)

    # ── checking ──

    def check(self, manual: bool = False) -> bool:
        """Check once (blocking). Returns True if GitHub could be reached."""
        try:
            info, error = self._fetch(), None
        except Exception as e:
            info, error = None, e
            log_throttled("update_check", logging.INFO, "Update check failed: %s", e,
                          interval=3600)
        self.last_checked = self._clock()
        self.last_error = error
        newer = None
        if info is not None and is_newer_release(info.tag, self.current):
            if manual or info.tag != self.skipped:
                newer = info
                log.info("Update available: %s (running %s)", info.tag, self.current)
            else:
                log.info("Update %s available but skipped by the user", info.tag)
        elif error is None:
            log.info("No update: latest release is %s, running %s",
                     info.tag if info else "unknown", self.current)
        try:
            self._on_result(newer, error, manual)
        except Exception:
            log.exception("Update result handler failed")
        return error is None

    def check_now(self) -> None:
        """Ask the background thread to check right away (e.g. "Check now")."""
        self._manual = True
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="UpdateCheck")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self):
        delay = self.FIRST_DELAY
        while True:
            self._wake.wait(delay)
            if self._stop.is_set():
                return
            self._wake.clear()
            manual, self._manual = self._manual, False
            if manual or self._enabled():
                ok = self.check(manual=manual)
                delay = self.INTERVAL if ok else self.RETRY_AFTER_FAILURE
            else:
                delay = self.INTERVAL
