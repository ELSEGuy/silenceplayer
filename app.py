import tkinter as tk
from tkinter import filedialog, messagebox
import tkinter.font as tkfont
import threading
import time
import json
import os
import sys
import glob
import bisect
import collections
import logging
import queue
import tempfile
import copy
import math
import re
import webbrowser
from logging.handlers import RotatingFileHandler
from typing import NamedTuple

import numpy as np
import psutil
from pycaw.pycaw import IAudioMeterInformation
import pystray
from PIL import Image, ImageDraw, ImageTk

APP_NAME    = "SilencePlayer"
APP_VERSION = "1.4.0-dev"

log = logging.getLogger("silenceplayer")

# ProcTap is only needed by the Mirroring Fix. If its
# native extension can't load, the rest of the app must still work.
try:
    from proctap import ProcessAudioCapture
    PROCTAP_IMPORT_ERROR = None
except Exception as _e:  # ImportError, or OSError from the native .pyd
    ProcessAudioCapture = None
    PROCTAP_IMPORT_ERROR = _e


if ProcessAudioCapture is not None:
    class QuietCapture(ProcessAudioCapture):
        """ProcTap's per-process capture with a gentler reader thread.

        ProcTap's own reader asks for new audio every millisecond, about
        2% of a CPU core per watched app even when nothing plays. Windows
        keeps a 1-second buffer, and each read returns one ~10 ms packet, so
        this reader wakes every POLL_SECONDS, empties the buffer and hands
        all of it to the callback in one go.
        """

        POLL_SECONDS = 0.02
        MAX_CHUNKS = 64               # hand over at least every ~0.6 s of audio

        def _worker(self):
            backend = getattr(self, "_backend", None)
            stop = getattr(self, "_stop_event", None)
            if backend is None or stop is None:     # a ProcTap version we don't know
                return super()._worker()
            while not stop.is_set():
                chunks = []
                while len(chunks) < self.MAX_CHUNKS:
                    try:
                        data = backend.read()
                    except Exception:
                        log_throttled(("proctap_read", id(self)), logging.WARNING,
                                      "ProcTap read failed", exc_info=True, interval=300)
                        data = None
                    if not data:
                        break
                    chunks.append(data)
                callback = getattr(self, "_on_data", None)
                if chunks and callback is not None:
                    try:
                        callback(b"".join(chunks), -1)
                    except Exception:
                        log_throttled(("proctap_cb", id(self)), logging.WARNING,
                                      "Audio callback failed", exc_info=True, interval=300)
                if len(chunks) < self.MAX_CHUNKS:
                    stop.wait(self.POLL_SECONDS)
else:
    QuietCapture = None

# Always use bundled VLC libraries
_vlc_dll = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libvlc.dll")
_vlc_plugins = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
if hasattr(sys, '_MEIPASS'):
    _vlc_dll = os.path.join(sys._MEIPASS, "libvlc.dll")
    _vlc_plugins = os.path.join(sys._MEIPASS, "plugins")
os.environ['PYTHON_VLC_LIB_PATH'] = _vlc_dll
os.environ['PYTHON_VLC_MODULE_PATH'] = _vlc_plugins

import vlc

if hasattr(sys, '_MEIPASS'):
    CONFIG_FILE = os.path.join(os.path.dirname(sys.executable), "config.json")
else:
    CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

LOG_MAX_BYTES    = 1_000_000
LOG_BACKUP_COUNT = 3


def log_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


class _ThrottleFilter(logging.Filter):
    """Lets the same message template through at most once per `interval` seconds.

    Only applies to loggers under `prefix` (ProcTap), whose worker loops can
    log the same error many times per second. Attached to handlers, because
    logger-level filters don't see records from child loggers.
    """

    def __init__(self, prefix, interval=60.0):
        super().__init__()
        self.prefix = prefix
        self.interval = interval
        self._last = {}
        self._lock = threading.Lock()

    def filter(self, record):
        if not (record.name == self.prefix or record.name.startswith(self.prefix + ".")):
            return True
        key = (record.name, record.levelno, record.msg)
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.interval:
                return False
            self._last[key] = now
        return True


_throttle_lock = threading.Lock()
_throttle_last = {}


def log_throttled(key, level, msg, *args, interval=60.0, **kwargs):
    """Log at most once per `interval` seconds for a given key.

    The monitor loop runs every 500 ms, so a persistent failure (for example
    pycaw breaking) must not write two log lines per second forever.
    """
    now = time.monotonic()
    with _throttle_lock:
        last, suppressed = _throttle_last.get(key, (None, 0))
        if last is not None and now - last < interval:
            _throttle_last[key] = (last, suppressed + 1)
            return
        _throttle_last[key] = (now, 0)
    if suppressed:
        msg = msg + " (%d similar messages suppressed)"
        args = args + (suppressed,)
    log.log(level, msg, *args, **kwargs)


def setup_logging():
    """Configure a rotating log file. Never raises: logging must fail safely."""
    if getattr(setup_logging, "_done", False):
        return
    setup_logging._done = True

    logging.raiseExceptions = False   # a broken log file must never crash the app
    log.setLevel(logging.DEBUG if os.environ.get("SILENCEPLAYER_DEBUG") else logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s")

    handlers = []
    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(d, "silenceplayer.log"),
                                 maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
                                 encoding="utf-8", delay=True)
        handlers.append(fh)
    except Exception:
        pass
    if not hasattr(sys, "_MEIPASS"):
        # Running from source: also keep a copy next to app.py for easy access.
        try:
            handlers.append(RotatingFileHandler(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "silenceplayer.log"),
                maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8", delay=True))
        except Exception:
            pass
        if sys.stderr is not None:
            handlers.append(logging.StreamHandler(sys.stderr))
    if not handlers:
        handlers.append(logging.NullHandler())

    throttle = _ThrottleFilter("proctap", 60.0)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(throttle)
        log.addHandler(h)

    # ProcTap logs through its own loggers; keep its warnings/errors, throttled.
    pt_log = logging.getLogger("proctap")
    pt_log.setLevel(logging.WARNING)
    pt_log.propagate = False
    for h in handlers:
        pt_log.addHandler(h)


def install_exception_hooks():
    def _sys_hook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.critical("Unhandled exception in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

SILENCE_MIN, SILENCE_MAX = 1, 3600
VOLUME_MIN,  VOLUME_MAX  = 0, 100
DUCK_MIN,    DUCK_MAX    = 0, 100

VALID_MODES          = ("single", "playlist")
VALID_SINGLE_LOOP    = ("loop", "stop")
VALID_PLAYLIST_LOOP  = ("loop_song", "stop", "loop_playlist")

# Apps on the Mirroring Fix list out of the box, grouped by the release that
# added them. Existing configs get the newer groups added once (see
# sanitize_config), so an app a user removed never comes back.
MIRROR_DEFAULTS = [
    # v1: first set
    ["discord.exe",              # mirrors desktop audio while streaming
     "signalrgb.exe",            # audio-reactive RGB lighting
     "wallpaper64.exe",          # Wallpaper Engine (audio-reactive wallpapers)
     "wallpaper32.exe"],
    # v2: capture/recording software (GitHub issue #1) and audio visualizers
    ["obs64.exe",                # OBS Studio
     "streamlabs obs.exe",       # Streamlabs (older versions)
     "streamlabs desktop.exe",   # Streamlabs Desktop
     "medal.exe",                # Medal clip recorder
     "rainmeter.exe",            # Rainmeter audio-visualizer skins
     "icue.exe"],                # Corsair iCUE audio-reactive lighting
    # v3: more streaming software and audio-reactive apps
    ["xsplit.core.exe",          # XSplit Broadcaster
     "xsplit.gamecaster.exe",    # XSplit Gamecaster (older)
     "lively.exe",               # Lively Wallpaper (audio-reactive wallpapers)
     "lively.playerwebview2.exe",   # Lively's wallpaper players
     "lively.playercefsharp.exe",
     "fluentflyout.exe"],        # FluentFlyout (media flyout / audio visualizer)
]
MIRROR_DEFAULTS_VERSION = len(MIRROR_DEFAULTS)

DEFAULT_CONFIG = {
    "mp3_path": "",
    "playlist_folder": "",
    "silence_seconds": 30,
    "max_volume": 80,
    "fade_enabled": True,
    "duck_percent": 0,
    "mode": "single",
    "single_loop_mode": "loop",
    "playlist_loop_mode": "loop_playlist",
    "excluded_apps": [],
    "mirror_fix_enabled": True,
    "mirror_apps": [{"name": n, "enabled": True} for group in MIRROR_DEFAULTS for n in group],
    "mirror_defaults_version": MIRROR_DEFAULTS_VERSION,
    "check_updates": True,
}


class ValidationError(ValueError):
    """A user-facing validation problem. str(e) is shown to the user."""


def parse_int_field(text, label, lo, hi, unit=""):
    """Parse a typed whole number and check its range, with a clear message."""
    s = "" if text is None else str(text).strip()
    suffix = f" {unit}" if unit else ""
    rule = f"{label} must be a whole number between {lo} and {hi}{suffix}"
    if not s:
        raise ValidationError(f"{rule}. The field is empty.")
    try:
        value = int(s)
    except ValueError:
        raise ValidationError(f"{rule}. \"{s}\" is not a whole number.") from None
    if not lo <= value <= hi:
        raise ValidationError(f"{rule}. {value} is out of range.")
    return value


def display_name(path):
    """What the Settings window shows for a file or folder: just its name.

    'C:/Users/me/Music/asmr/Animal Crossing Office.m4a' → 'Animal Crossing Office.m4a'
    'C:/Users/me/Music/asmr/'                          → 'asmr'
    'D:/'                                              → 'D:/' (a drive has no name)
    """
    if not path:
        return ""
    name = os.path.basename(os.path.normpath(path))
    return name or path


def _coerce_int(value, lo, hi):
    if isinstance(value, bool):
        raise TypeError("bool is not a number")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("not a whole number")
        value = int(value)
    elif isinstance(value, str):
        value = int(value.strip())
    elif not isinstance(value, int):
        raise TypeError(type(value).__name__)
    if not lo <= value <= hi:
        raise ValueError(f"{value} out of range {lo}-{hi}")
    return value


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    raise TypeError(type(value).__name__)


def _coerce_str(value):
    if isinstance(value, str):
        return value
    raise TypeError(type(value).__name__)


def _coerce_choice(choices):
    def check(value):
        if value in choices:
            return value
        raise ValueError(f"{value!r} not in {choices}")
    return check


def normalize_app_name(text):
    """'SignalRGB' → 'signalrgb.exe' (process names are matched lower-case)."""
    name = (text or "").strip().strip("\"'").strip().lower()
    if name and not name.endswith(".exe"):
        name += ".exe"
    return name


def _coerce_mirror_apps(value):
    if not isinstance(value, list):
        raise TypeError(type(value).__name__)
    out, seen = [], set()
    for item in value:
        if isinstance(item, str):
            name, enabled = item, True
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            name, enabled = item["name"], item.get("enabled", True)
            if not isinstance(enabled, bool):
                enabled = True
        else:
            continue
        name = normalize_app_name(name)
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "enabled": enabled})
    return out


def enabled_mirror_apps(cfg):
    """Names of the apps the Mirroring Fix should check. Never raises."""
    try:
        return frozenset(a["name"] for a in _coerce_mirror_apps(cfg.get("mirror_apps", []))
                         if a["enabled"])
    except (TypeError, ValueError):
        return frozenset()


def _coerce_app_list(value):
    if not isinstance(value, list):
        raise TypeError(type(value).__name__)
    out = []
    for item in value:
        if isinstance(item, str):
            name = item.strip().lower()
            if name and name not in out:
                out.append(name)
    return out


_CONFIG_CHECKS = {
    "mp3_path":           _coerce_str,
    "playlist_folder":    _coerce_str,
    "silence_seconds":    lambda v: _coerce_int(v, SILENCE_MIN, SILENCE_MAX),
    "max_volume":         lambda v: _coerce_int(v, VOLUME_MIN, VOLUME_MAX),
    "fade_enabled":       _coerce_bool,
    "duck_percent":       lambda v: _coerce_int(v, DUCK_MIN, DUCK_MAX),
    "mode":               _coerce_choice(VALID_MODES),
    "single_loop_mode":   _coerce_choice(VALID_SINGLE_LOOP),
    "playlist_loop_mode": _coerce_choice(VALID_PLAYLIST_LOOP),
    "excluded_apps":      _coerce_app_list,
    "mirror_fix_enabled": _coerce_bool,
    "mirror_apps":        _coerce_mirror_apps,
    "mirror_defaults_version": lambda v: _coerce_int(v, 0, 10_000),
    "check_updates":      _coerce_bool,
}


def sanitize_config(raw):
    """Return (config, problems). Invalid values fall back to defaults.

    Unknown keys are kept so a config written by a newer version survives a
    round trip through this one.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    problems = []
    if not isinstance(raw, dict):
        return cfg, ["config file does not contain a JSON object"]
    for key, value in raw.items():
        check = _CONFIG_CHECKS.get(key)
        if check is None:
            cfg[key] = value
            continue
        try:
            cfg[key] = check(value)
        except (TypeError, ValueError) as e:
            problems.append(f"{key}={value!r} is invalid ({e}); using default {DEFAULT_CONFIG[key]!r}")

    # Add Mirroring Fix defaults introduced since this config was written.
    if "mirror_apps" in raw and isinstance(raw.get("mirror_apps"), list):
        seen = raw.get("mirror_defaults_version", 1)
        if not isinstance(seen, int) or isinstance(seen, bool):
            seen = 1
        names = {a["name"] for a in cfg["mirror_apps"]}
        for group in MIRROR_DEFAULTS[max(0, seen):]:
            for name in group:
                if name not in names:
                    cfg["mirror_apps"].append({"name": name, "enabled": True})
                    names.add(name)
    cfg["mirror_defaults_version"] = MIRROR_DEFAULTS_VERSION
    return cfg, problems


def load_config(path=None):
    """Load and sanitize the config. Returns (config, user_notice_or_None)."""
    path = path or CONFIG_FILE
    if not os.path.exists(path):
        log.info("No config file at %s; using defaults", path)
        return sanitize_config({})[0], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        # Keep the broken file instead of silently overwriting it on next save.
        backup = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(path, backup)
            log.error("Config file %s is corrupt (%s); moved it to %s", path, e, backup)
            notice = (f"Your settings file was unreadable and has been reset. "
                      f"The old file was kept as {os.path.basename(backup)}.")
        except OSError:
            log.exception("Config file %s is corrupt and could not be backed up", path)
            notice = "Your settings file was unreadable; default settings are being used."
        return sanitize_config({})[0], notice
    except OSError:
        log.exception("Could not read config file %s", path)
        return sanitize_config({})[0], "Your settings file could not be read; default settings are being used."

    cfg, problems = sanitize_config(raw)
    for p in problems:
        log.warning("Config: %s", p)
    notice = None
    if problems:
        notice = "Some saved settings were invalid and have been reset to their defaults."
    return cfg, notice


_save_lock = threading.Lock()


def save_config(cfg, path=None):
    """Atomically write the config: temp file in the same folder, then replace.

    A crash mid-write leaves either the old file or the new one, never a
    truncated mix. Returns True on success; failures are logged, not raised.
    """
    path = path or CONFIG_FILE
    folder = os.path.dirname(os.path.abspath(path))
    with _save_lock:
        try:
            data = json.dumps(dict(cfg), indent=2)
            fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                # Antivirus/indexers can briefly lock the target on Windows.
                for attempt in range(5):
                    try:
                        os.replace(tmp, path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except Exception:
            log_throttled("save_config", logging.ERROR,
                          "Could not save config to %s", path, exc_info=True)
            return False


class Settings(NamedTuple):
    silence_secs: float
    fade_secs: float
    duck_percent: float
    max_vol: float
    excluded: list
    mirror_fix: bool
    mirror_apps: frozenset


FADE_SECONDS    = 2.0
NO_FADE_SECONDS = 0.01


def fade_seconds(cfg):
    return FADE_SECONDS if cfg.get("fade_enabled", True) is not False else NO_FADE_SECONDS


def runtime_settings(cfg):
    """Read the values worker threads need. Never raises on bad values."""
    def num(key, lo, hi):
        try:
            v = float(cfg.get(key, DEFAULT_CONFIG[key]))
        except (TypeError, ValueError):
            return float(DEFAULT_CONFIG[key])
        if v != v or not lo <= v <= hi:   # NaN or out of range
            return float(DEFAULT_CONFIG[key])
        return v

    excluded = cfg.get("excluded_apps", [])
    if not isinstance(excluded, list):
        excluded = []
    return Settings(
        silence_secs=num("silence_seconds", SILENCE_MIN, SILENCE_MAX),
        fade_secs=fade_seconds(cfg),
        duck_percent=num("duck_percent", DUCK_MIN, DUCK_MAX),
        max_vol=num("max_volume", VOLUME_MIN, VOLUME_MAX),
        excluded=excluded,
        mirror_fix=cfg.get("mirror_fix_enabled", True) is True,
        mirror_apps=enabled_mirror_apps(cfg),
    )


OWN_PROCESSES = {"python.exe", "pythonw.exe", "py.exe", "python3.exe", "silenceplayer.exe"}
SUPPORTED_EXTENSIONS = ("*.mp3", "*.opus", "*.m4a", "*.flac", "*.mp4")
SUPPORTED_LABEL = "MP3, OPUS, M4A, FLAC, MP4"
# How long after the ambient sound starts a *new* "external" sound counts as
# suspicious (an app reacting to our own sound), on top of the start cooldown.
MIRROR_SUSPECT_SECONDS = 6.0
# Mirroring apps keep showing a level for a moment after our sound has ended.
MIRROR_TAIL_SECONDS = 1.5


def collect_playlist(folder):
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(glob.glob(os.path.join(folder, ext)))
    # dict.fromkeys de-duplicates while keeping order (glob can be case-insensitive)
    return sorted(dict.fromkeys(f for f in files if os.path.isfile(f)))


def validate_ambient_source(cfg):
    """Return None if the selected ambient source is playable, else a message."""
    mode = cfg.get("mode", "single")
    if mode == "playlist":
        folder = cfg.get("playlist_folder", "")
        if not folder:
            return "Please choose a playlist folder first."
        if not os.path.isdir(folder):
            return f"The playlist folder could not be found:\n{folder}"
        if not collect_playlist(folder):
            return f"The playlist folder doesn't contain any supported audio files ({SUPPORTED_LABEL})."
        return None
    path = cfg.get("mp3_path", "")
    if not path:
        return "Please choose an ambient sound file first."
    if os.path.isdir(path):
        return "The selected path is a folder. Use Playlist mode to play a folder."
    if not os.path.isfile(path):
        return f"The ambient sound file could not be found:\n{path}"
    return None


def same_path(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ─────────────────────────────────────────────────────────────────────────────
# Resume state
# ─────────────────────────────────────────────────────────────────────────────

class ResumeState(NamedTuple):
    mode: str        # "single" or "playlist"
    source: str      # the file (single) or folder (playlist) that was selected
    path: str        # the file that was actually playing
    index: int       # playlist index of `path` (0 in single mode)
    position: float  # seconds into `path`


def plan_single_start(path, resume):
    """Seconds to start `path` at. Only resume if it is the same file."""
    if resume is not None and resume.mode == "single" and same_path(resume.path, path):
        return max(0.0, float(resume.position))
    return 0.0


def plan_playlist_start(folder, files, resume):
    """Return (index, seconds) to start a playlist at.

    - Same folder and the saved track still exists → that track at the saved time.
    - Same folder but the saved track is gone → the track that now sits in its
      place in sort order, from the beginning (never someone else's timestamp).
    - Different folder, other mode, or nothing saved → first track.
    """
    if not files:
        return 0, 0.0
    if resume is None or resume.mode != "playlist" or not same_path(resume.source, folder):
        return 0, 0.0
    for i, f in enumerate(files):
        if same_path(f, resume.path):
            return i, max(0.0, float(resume.position))
    if resume.path:
        i = bisect.bisect_left(files, resume.path)
    else:
        i = resume.index
    if not 0 <= i < len(files):
        i = 0
    return i, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Windows audio sessions
# ─────────────────────────────────────────────────────────────────────────────

class PlayingApps(set):
    """Names of the apps producing sound right now.

    `excluded_audible` holds the *excluded* apps that are audible too. They
    don't count as playing, but they are something a mirroring app (Discord,
    SignalRGB, ...) can be reacting to, which the Mirroring Fix needs to know.
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.excluded_audible = set()


# ── COM (Windows' component system, which the audio API is built on) ──

_MTA_COOKIE = None


def ensure_mta():
    """Keep COM's multithreaded apartment (MTA) open for the whole process.

    The monitor, the Mirroring Fix and the fades call the Windows audio API
    from their own threads without setting up COM there. Windows allows that
    only while some thread keeps the MTA open (the "implicit MTA"). Before
    1.4 a library happened to do it; CoIncrementMTAUsage (Windows 8+) holds
    it open on purpose for as long as Silence Player runs.
    """
    global _MTA_COOKIE
    if sys.platform != "win32" or _MTA_COOKIE is not None:
        return _MTA_COOKIE is not None
    try:
        import ctypes
        cookie = ctypes.c_void_p()
        ctypes.oledll.ole32.CoIncrementMTAUsage(ctypes.byref(cookie))   # raises on failure
        _MTA_COOKIE = cookie                                            # never released
        log.info("COM multithreaded apartment kept open for worker threads")
        return True
    except Exception:
        log.warning("CoIncrementMTAUsage failed; worker threads rely on the implicit MTA",
                    exc_info=True)
        return False


# ── audio sessions on every output device ──

DEVICE_REFRESH_SECONDS = 5.0


class _DeviceCache(threading.local):
    """Session managers of the active output devices, cached per thread (COM
    objects are kept to the thread that made them)."""

    def __init__(self):
        self.managers = None
        self.loaded_at = float("-inf")


_device_cache = _DeviceCache()


def _active_session_managers():
    """IAudioSessionManager2 of every active output device, not just the default
    one: an app can play on a headset while the speakers are the default."""
    cache = _device_cache
    now = time.monotonic()
    if cache.managers is not None and now - cache.loaded_at < DEVICE_REFRESH_SECONDS:
        return cache.managers
    import comtypes
    from pycaw.constants import CLSID_MMDeviceEnumerator, DEVICE_STATE, EDataFlow
    from pycaw.pycaw import IAudioSessionManager2, IMMDeviceEnumerator
    enumerator = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                           comtypes.CLSCTX_INPROC_SERVER)
    collection = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value,
                                               DEVICE_STATE.ACTIVE.value)
    managers = []
    for i in range(collection.GetCount()):
        try:
            device = collection.Item(i)
            iface = device.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
            managers.append(iface.QueryInterface(IAudioSessionManager2))
        except Exception as e:
            log_throttled(("device_activate", i), logging.WARNING,
                          "Skipping an audio output device: %s", e, interval=600)
    if not managers:
        raise OSError("No active audio output device could be opened")
    cache.managers, cache.loaded_at = managers, now
    return managers


def list_sessions():
    """Audio sessions (pycaw AudioSession) on every active output device.

    Raises if they can't be listed, so callers never mistake "couldn't
    look" for "nothing is playing".
    """
    from pycaw.pycaw import AudioSession, IAudioSessionControl2
    try:
        sessions = []
        for manager in _active_session_managers():
            enumerator = manager.GetSessionEnumerator()
            for i in range(enumerator.GetCount()):
                ctl = enumerator.GetSession(i)
                if ctl is None:
                    continue
                ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                if ctl2 is not None:
                    sessions.append(AudioSession(ctl2))
        return sessions
    except Exception:
        _device_cache.managers = None      # a device went away: list them again next time
        raise


class ProcessNames:
    """pid → lower-case process name, remembered so the monitor doesn't ask
    Windows about every audio session twice a second."""

    TTL = 30.0

    def __init__(self, lookup=None, clock=time.monotonic):
        self._lookup = lookup or (lambda pid: psutil.Process(pid).name())
        self._clock = clock
        self._names = {}                 # pid → (name, when looked up)
        self._lock = threading.Lock()

    def get(self, pid):
        now = self._clock()
        with self._lock:
            hit = self._names.get(pid)
            if hit is not None and now - hit[1] < self.TTL:
                return hit[0]
        try:
            name = self._lookup(pid).lower()
        except Exception:
            return None                  # exited, or not allowed to look
        with self._lock:
            self._names[pid] = (name, now)
        return name

    def forget_missing(self, live_pids):
        """Drop processes that no longer have an audio session (a new process
        may get the same pid later)."""
        with self._lock:
            for pid in [p for p in self._names if p not in live_pids]:
                del self._names[pid]


process_names = ProcessNames()


def get_playing_apps(excluded=None):
    """Names of processes currently producing audio.

    Raises if the session list itself can't be read: the monitor must not
    mistake "pycaw is broken" for "the PC is silent" and start playing over
    whatever is actually going on.
    """
    playing = PlayingApps()
    excluded_set = set(e.lower() for e in (excluded or []))
    own_pid = os.getpid()
    live = set()
    for session in list_sessions():
        try:
            pid = session.ProcessId
            if not pid:                  # 0 = Windows system sounds
                continue
            live.add(pid)
            # Our own VLC output, whatever the interpreter/EXE is called
            # (e.g. pythonw3.12.exe from the Microsoft Store Python).
            if pid == own_pid:
                continue
            name = process_names.get(pid)
            if name is None or name in OWN_PROCESSES:
                continue
            meter = session._ctl.QueryInterface(IAudioMeterInformation)
            peak = meter.GetPeakValue()
            if peak > 0.001:
                if name in excluded_set:
                    playing.excluded_audible.add(name)
                else:
                    playing.add(name)
        except Exception as e:
            # Processes exit between listing and querying; that's normal.
            log_throttled("session_query", logging.DEBUG,
                          "Skipping an audio session: %s", e, interval=300)
    process_names.forget_missing(live)
    return playing


def get_app_pids(names):
    """{pid: process name} for every audio session owned by one of `names`.

    Raises on failure so the caller can keep its existing taps instead of
    treating a transient pycaw error as "the app went away".
    """
    found = {}
    for session in list_sessions():
        try:
            pid = session.ProcessId
            if not pid:
                continue
            name = process_names.get(pid)
            if name in names:
                found[pid] = name
        except Exception:
            continue
    return found


def resource_path(filename):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def create_tray_icon():
    try:
        img = Image.open(resource_path("trayicon.png")).resize((64, 64))
        return img
    except Exception:
        log.warning("Could not load trayicon.png; using the built-in icon", exc_info=True)
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([0, 0, 63, 63], fill="#1e1e2e")
        draw.rectangle([24, 18, 30, 42], fill="#89b4fa")
        draw.rectangle([30, 18, 44, 26], fill="#89b4fa")
        draw.rectangle([38, 26, 44, 42], fill="#89b4fa")
        draw.ellipse([18, 38, 32, 48], fill="#89b4fa")
        draw.ellipse([32, 38, 46, 48], fill="#89b4fa")
        return img


# ─────────────────────────────────────────────────────────────────────────────
# Single instance (Windows named mutex)
# ─────────────────────────────────────────────────────────────────────────────

class SingleInstance:
    """Allows one Silence Player per Windows login session.

    Uses a named kernel mutex: Windows destroys it automatically when the
    owning process exits or crashes, so there is no stale lock to clean up.
    A second launch signals a named event, which makes the running instance
    open its Settings window, and then exits.
    """

    MUTEX_NAME = "Local\\SilencePlayer.SingleInstance"
    EVENT_NAME = "Local\\SilencePlayer.ShowSettings"
    ERROR_ALREADY_EXISTS = 183
    ERROR_ACCESS_DENIED  = 5
    WAIT_OBJECT_0        = 0

    def __init__(self):
        self._k32 = None
        self._mutex = None
        self._event = None
        self._stop = threading.Event()
        self._listener = None

    def _kernel32(self):
        if self._k32 is None:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CreateEventW.restype = wintypes.HANDLE
            k32.SetEvent.argtypes = [wintypes.HANDLE]
            k32.SetEvent.restype = wintypes.BOOL
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            self._ctypes = ctypes
            self._k32 = k32
        return self._k32

    def acquire(self):
        """True if this process is the only instance (or the check isn't possible)."""
        if sys.platform != "win32":
            return True
        try:
            k32 = self._kernel32()
            handle = k32.CreateMutexW(None, False, self.MUTEX_NAME)
            err = self._ctypes.get_last_error()
            if not handle:
                if err == self.ERROR_ACCESS_DENIED:
                    # Exists, created by an instance running with other rights.
                    self._signal_existing()
                    return False
                log.error("CreateMutexW failed (error %s); continuing without "
                          "single-instance protection", err)
                return True
            if err == self.ERROR_ALREADY_EXISTS:
                k32.CloseHandle(handle)
                self._signal_existing()
                return False
            self._mutex = handle
            self._event = k32.CreateEventW(None, False, False, self.EVENT_NAME)
            if not self._event:
                log.warning("CreateEventW failed (error %s); a second launch won't "
                            "be able to open Settings", self._ctypes.get_last_error())
            return True
        except Exception:
            log.exception("Single-instance check failed; continuing")
            return True

    def _signal_existing(self):
        try:
            k32 = self._kernel32()
            ev = k32.CreateEventW(None, False, False, self.EVENT_NAME)
            if ev:
                k32.SetEvent(ev)
                k32.CloseHandle(ev)
        except Exception:
            log.exception("Could not signal the running instance")

    def listen(self, on_show_requested):
        """Call `on_show_requested()` (from a worker thread) when a second launch happens."""
        if not self._event:
            return
        def loop():
            k32 = self._k32
            while not self._stop.is_set():
                try:
                    if k32.WaitForSingleObject(self._event, 500) == self.WAIT_OBJECT_0:
                        if not self._stop.is_set():
                            on_show_requested()
                except Exception:
                    log.exception("Single-instance listener failed")
                    return
        self._listener = threading.Thread(target=loop, daemon=True, name="InstanceListener")
        self._listener.start()

    def release(self):
        self._stop.set()
        if self._listener is not None and self._listener is not threading.current_thread():
            self._listener.join(timeout=2.0)
        k32 = self._k32
        if k32 is None:
            return
        for attr in ("_event", "_mutex"):
            h = getattr(self, attr)
            if h:
                try:
                    k32.CloseHandle(h)
                except Exception:
                    pass
                setattr(self, attr, None)


# ─────────────────────────────────────────────────────────────────────────────
# Start with Windows
# ─────────────────────────────────────────────────────────────────────────────

class _WinRegistry:
    """String values under HKEY_CURRENT_USER. Windows only (needs winreg)."""

    def __init__(self):
        import winreg                      # ImportError off Windows
        self._w = winreg

    def get(self, path, name):
        """The value (str or bytes), or None if the key or value doesn't exist."""
        w = self._w
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, path, 0, w.KEY_READ) as key:
                return w.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, path, name, value):
        w = self._w
        with w.CreateKeyEx(w.HKEY_CURRENT_USER, path, 0, w.KEY_SET_VALUE) as key:
            w.SetValueEx(key, name, 0, w.REG_SZ, value)

    def delete(self, path, name):
        w = self._w
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, path, 0, w.KEY_SET_VALUE) as key:
                w.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def startup_command():
    """The command line Windows should run at sign-in."""
    exe = sys.executable
    if hasattr(sys, "_MEIPASS"):                 # the packaged EXE
        return f'"{exe}"'
    # Running from source: use pythonw so no console window opens.
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.basename(exe).lower() == "python.exe" and os.path.isfile(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


class StartupEntry:
    """Turns "start with Windows" on and off (the per-user Run registry key).

    No admin rights needed, and it only affects the current Windows user.
    Windows also lets people switch startup apps off in Settings → Apps →
    Startup or Task Manager; that is stored separately (StartupApproved) and
    wins over the Run entry, so it is respected when reading and cleared when
    the user turns the option on again here.
    """

    RUN_KEY      = r"Software\Microsoft\Windows\CurrentVersion\Run"
    APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
    VALUE_NAME   = "SilencePlayer"

    def __init__(self, registry=None, command=None):
        if registry is None:
            try:
                registry = _WinRegistry()
            except ImportError:
                registry = None
        self._reg = registry
        self._command = command or startup_command

    @property
    def available(self):
        return self._reg is not None

    def _registered_command(self):
        value = self._reg.get(self.RUN_KEY, self.VALUE_NAME)
        return value if isinstance(value, str) and value.strip() else None

    def _blocked_by_windows(self):
        """True if the user switched it off in Windows' startup settings."""
        data = self._reg.get(self.APPROVED_KEY, self.VALUE_NAME)
        return isinstance(data, (bytes, bytearray)) and len(data) > 0 and bool(data[0] & 1)

    def status(self):
        """'unavailable', 'off', 'on', or 'blocked' (registered, but switched off in Windows)."""
        if not self.available:
            return "unavailable"
        if self._registered_command() is None:
            return "off"
        return "blocked" if self._blocked_by_windows() else "on"

    def is_enabled(self):
        return self.status() == "on"

    def set_enabled(self, enabled):
        """Raises OSError if Windows refuses; the caller reports it."""
        if not self.available:
            raise OSError("Start with Windows is only available on Windows")
        if enabled:
            self._reg.set(self.RUN_KEY, self.VALUE_NAME, self._command())
            self._reg.delete(self.APPROVED_KEY, self.VALUE_NAME)   # clear a "disabled" mark
            log.info("Start with Windows: on (%s)", self._command())
        else:
            self._reg.delete(self.RUN_KEY, self.VALUE_NAME)
            self._reg.delete(self.APPROVED_KEY, self.VALUE_NAME)
            log.info("Start with Windows: off")

    def repair_path(self):
        """If the packaged EXE was moved or replaced, point the entry at its new
        location so Start with Windows keeps working. Returns True if changed."""
        if not self.available or not hasattr(sys, "_MEIPASS"):
            return False
        registered = self._registered_command()
        wanted = self._command()
        if registered is None or registered == wanted:
            return False
        self._reg.set(self.RUN_KEY, self.VALUE_NAME, wanted)
        log.info("Start with Windows: updated the path (%s → %s)", registered, wanted)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Update check
# ─────────────────────────────────────────────────────────────────────────────

GITHUB_REPO    = "ELSEGuy/silenceplayer"
UPDATE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_URL   = f"https://github.com/{GITHUB_REPO}/releases/latest"
# For testing the notice: run with this set to an older version (e.g. 1.2.0).
PRETEND_VERSION_ENV = "SILENCEPLAYER_PRETEND_VERSION"

_VERSION_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", re.IGNORECASE)


def parse_version(text):
    """'v1.3.0' → ((1, 3, 0), True). The flag is False for anything with a suffix
    ('1.4.0-dev', 'v1.3-beta'): those are not stable releases. None if unreadable."""
    m = _VERSION_RE.match(text or "") if isinstance(text, str) else None
    if not m:
        return None
    nums = tuple(int(g) if g else 0 for g in m.groups()[:3])
    return nums, m.group(4).strip() == ""


def is_newer_release(tag, current):
    """True if `tag` is a stable release newer than the running `current` version.
    A test build like 1.4.0-dev counts as older than the 1.4.0 release."""
    latest, cur = parse_version(tag), parse_version(current)
    if latest is None or cur is None or not latest[1]:
        return False
    return latest[0] + (1,) > cur[0] + (1 if cur[1] else 0,)


def current_version():
    """APP_VERSION, unless a pretend version is set for testing the update notice."""
    return os.environ.get(PRETEND_VERSION_ENV, "").strip() or APP_VERSION


def update_state_file():
    return os.path.join(log_dir(), "update.json")


class ReleaseInfo(NamedTuple):
    version: str   # as shown to the user, e.g. "1.4.1"
    tag: str       # e.g. "v1.4.1"
    url: str       # the release page on GitHub


def fetch_latest_release(timeout=10.0):
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

    def skip(self, tag):
        self.skipped = tag
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump({"skipped": tag}, f)
        except Exception:
            log.warning("Could not save the skipped version", exc_info=True)
        log.info("User skipped update %s", tag)

    # ── checking ──

    def check(self, manual=False):
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

    def check_now(self):
        """Ask the background thread to check right away (e.g. "Check now")."""
        self._manual = True
        self._wake.set()

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="UpdateCheck")
        self._thread.start()

    def stop(self):
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


# ─────────────────────────────────────────────────────────────────────────────
# Mirroring Fix
# ─────────────────────────────────────────────────────────────────────────────

class _MirrorTap:
    """Per-PID capture state. Removing the object removes all of it at once."""

    __slots__ = ("pid", "name", "tap", "samples", "started_at", "last_sample_time")

    def __init__(self, pid, name, started_at, window):
        self.pid = pid
        self.name = name
        self.tap = None
        self.samples = collections.deque(maxlen=window)   # (time, rms)
        self.started_at = started_at
        self.last_sample_time = None


class MirrorFix:
    """
    Some apps listen to everything the PC plays and show a fake level in the
    Windows mixer while doing it, without making any sound themselves:

      - Discord while streaming/screen sharing (its capture session mirrors
        desktop audio; its voice/UI sessions are real output)
      - SignalRGB, Wallpaper Engine and other audio-reactive apps

    pycaw then reports them as "playing" the moment our ambient sound starts.

    For every app in the list, each of its audio PIDs is tapped with ProcTap,
    which captures what that process really renders. If ANY of an app's PIDs
    has RMS > 0, the app is making real sound. If ALL are silent, the level in
    the mixer is only mirroring → ignore it.

    pycaw decides that an app is active; this class only answers whether that
    activity is real output. When it can't tell (no tap yet, a tap failed, a
    tap is still warming up) it answers True, i.e. the app counts as playing.
    """

    # An app that only mirrors renders nothing: its tap reads exact digital
    # silence (0.0000 in real logs). A stream played at low volume reads
    # 0.0004-0.0016, so a threshold of 0.001 (the level pycaw's *peak* meter
    # uses) cut right through it and the verdict flipped back and forth.
    # Anything clearly above zero is real sound.
    REAL_AUDIO_THRESHOLD = 0.0001
    WINDOW_SIZE          = 256    # enough chunks to cover STALE_AFTER_SECONDS
    MIN_SAMPLES          = 3      # samples needed to judge a tap during warm-up
    WARMUP_SECONDS       = 2.0    # a new tap counts as "unknown" (→ real) at most this long
    STALE_AFTER_SECONDS  = 2.0    # PCM older than this is not current audio
    WATCH_INTERVAL       = 1.0
    RETRY_BASE_SECONDS   = 5.0
    RETRY_MAX_SECONDS    = 60.0

    def __init__(self, apps_source, pid_source=None, capture_factory=None, clock=time.monotonic):
        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._started     = False
        self._taps        = {}   # pid → _MirrorTap
        self._failed      = {}   # pid → (next_retry_time, failure_count, name)
        self._watch_thread = None
        self._apps_source = apps_source            # () → names to watch
        self._pid_source  = pid_source or get_app_pids
        self._capture_factory = capture_factory or QuietCapture
        self._clock       = clock
        self._last_verdict = {}

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        log.info("Mirroring Fix started")
        self._watch_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="MirrorWatch")
        self._watch_thread.start()

    def stop(self, timeout=3.0):
        self._stop_event.set()
        thread = self._watch_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Mirror watch thread did not exit within %.1fs", timeout)
        with self._lock:
            states = list(self._taps.values())
            self._taps.clear()
            self._failed.clear()
        for st in states:
            self._stop_tap(st)
        log.info("Mirroring Fix stopped")

    @staticmethod
    def _stop_tap(state):
        if state.tap is None:
            return
        try:
            state.tap.stop()
        except Exception:
            log.warning("Stopping ProcTap for %s (PID %s) failed", state.name, state.pid,
                        exc_info=True)

    def _make_callback(self, state):
        def on_data(pcm, frames):
            try:
                n = len(pcm) // 4
                if n == 0:
                    return
                # ProcTap on Windows delivers 48 kHz stereo float32.
                samples = np.frombuffer(pcm, dtype=np.float32, count=n)
                rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
                if not np.isfinite(rms):
                    return
                now = self._clock()
                with self._lock:
                    # Only record data for the tap that is currently registered.
                    if self._taps.get(state.pid) is state:
                        state.samples.append((now, rms))
                        state.last_sample_time = now
            except Exception:
                log_throttled(("mirror_cb", state.pid), logging.WARNING,
                              "PCM callback failed for %s (PID %s)", state.name, state.pid,
                              exc_info=True)
        return on_data

    def _watch_loop(self):
        while not self._stop_event.is_set():
            try:
                self._sync_taps()
            except Exception:
                log_throttled("mirror_watch", logging.WARNING,
                              "Mirror watch cycle failed", exc_info=True)
            self._stop_event.wait(self.WATCH_INTERVAL)

    def _sync_taps(self):
        names = frozenset(self._apps_source())
        current = dict(self._pid_source(names)) if names else {}
        now = self._clock()

        with self._lock:
            removed = []
            for pid in list(self._taps):
                st = self._taps[pid]
                if current.get(pid) != st.name:
                    removed.append(self._taps.pop(pid))     # gone, or removed from the list
                elif st.tap is not None and getattr(st.tap, "is_running", True) is False:
                    log.warning("ProcTap for %s (PID %s) stopped running; reopening",
                                st.name, pid)
                    removed.append(self._taps.pop(pid))
            for pid in list(self._failed):
                if current.get(pid) != self._failed[pid][2]:
                    del self._failed[pid]
            to_open = []
            for pid, name in current.items():
                if pid in self._taps:
                    continue
                failed = self._failed.get(pid)
                if failed is not None and now < failed[0]:
                    continue
                to_open.append((pid, name))

        for st in removed:
            self._stop_tap(st)
        for pid, name in to_open:
            if self._stop_event.is_set():
                break
            self._open_tap(pid, name)

    def _open_tap(self, pid, name):
        state = _MirrorTap(pid, name, self._clock(), self.WINDOW_SIZE)
        tap = None
        try:
            if self._capture_factory is None:
                raise RuntimeError(f"ProcTap is unavailable: {PROCTAP_IMPORT_ERROR}")
            tap = self._capture_factory(pid)
            tap.set_callback(self._make_callback(state))
            tap.start()
        except Exception as e:
            with self._lock:
                count = self._failed.get(pid, (0, 0, name))[1] + 1
                delay = min(self.RETRY_MAX_SECONDS, self.RETRY_BASE_SECONDS * (2 ** (count - 1)))
                self._failed[pid] = (self._clock() + delay, count, name)
            if count == 1:
                log.warning("Could not open ProcTap for %s (PID %s): %s", name, pid, e,
                            exc_info=True)
            else:
                log_throttled(("mirror_open", pid), logging.WARNING,
                              "Still can't open ProcTap for %s (PID %s, attempt %d): %s",
                              name, pid, count, e)
            if tap is not None:
                try:
                    tap.stop()
                except Exception:
                    pass
            return

        state.tap = tap
        state.started_at = self._clock()
        with self._lock:
            stopped = self._stop_event.is_set()
            if not stopped:
                self._taps[pid] = state
                self._failed.pop(pid, None)
        if stopped:
            # stop() ran while this tap was opening; don't leave it orphaned.
            self._stop_tap(state)
        else:
            log.info("Tapping %s (PID %s)", name, pid)

    def _classify(self, state, now):
        """'active', 'silent', or 'warming_up' for one tap."""
        fresh = [rms for (t, rms) in state.samples if now - t <= self.STALE_AFTER_SECONDS]
        warming_up = now - state.started_at < self.WARMUP_SECONDS
        if warming_up and len(fresh) < self.MIN_SAMPLES:
            if any(r > self.REAL_AUDIO_THRESHOLD for r in fresh):
                return "active"
            return "warming_up"
        if not fresh:
            # Nothing rendered recently (or the stream went quiet): not current audio.
            return "silent"
        # Any recent sound counts, like the peak meter pycaw reads. Averaging
        # the last few chunks made quiet or bursty audio (a stream with gaps in
        # its sound) flip between "real" and "mirroring only".
        return "active" if max(fresh) > self.REAL_AUDIO_THRESHOLD else "silent"

    def _describe(self, taps, now):
        """One line per-tap levels for the log: PID, chunks heard recently, loudest."""
        parts = []
        for st in taps:
            fresh = [rms for (t, rms) in st.samples if now - t <= self.STALE_AFTER_SECONDS]
            loudest = max(fresh) if fresh else 0.0
            parts.append(f"PID {st.pid}: {len(fresh)} chunks, loudest {loudest:.5f}")
        return "; ".join(parts) if parts else "no taps"

    def is_real_audio(self, name):
        """
        True  → `name` is outputting real audio, or we can't tell yet → react
        False → all of its sessions are silent → mirroring only → ignore
        """
        now = self._clock()
        with self._lock:
            taps = [st for st in self._taps.values() if st.name == name]
            failed = any(f[2] == name for f in self._failed.values())
            if not taps:
                verdict, why = True, "not tapped (yet)"        # safe default
            elif failed:
                verdict, why = True, "a tap could not be opened"
            else:
                verdict, why = False, "all taps silent"
                for st in taps:
                    c = self._classify(st, now)
                    if c != "silent":
                        verdict, why = True, f"PID {st.pid} {c}"
                        break
            changed = verdict != self._last_verdict.get(name)
            detail = self._describe(taps, now) if changed else ""
        if changed:
            self._last_verdict[name] = verdict
            log.info("%s classified as %s (%s; %s)", name,
                     "real audio" if verdict else "mirroring only", why, detail)
        return verdict


# ─────────────────────────────────────────────────────────────────────────────
# Audio monitor
# ─────────────────────────────────────────────────────────────────────────────

class _MonitorState:
    def __init__(self):
        self.silence_start     = None
        self.ambient_triggered = False
        self.cooldown_until    = 0.0
        self.ducked            = False
        self.unduck_start      = None
        self.ambient_started_at = 0.0
        self.ambient_audible_at = float("-inf")    # last time our own sound could be heard
        self.mirror_note       = (frozenset(), frozenset())   # what we last logged


class MonitorInfo(NamedTuple):
    """What the monitor is doing, for the "Now playing" display.

    phase: "starting", "waiting" (silence countdown), "audio" (other apps are
    playing), "playing", or "ducked" (playing quieter).
    """
    phase: str
    remaining: float = None     # seconds left of a countdown (waiting / unducking)
    apps: tuple = ()            # apps that are playing (audio / ducked)
    duck_percent: int = 0


class AudioMonitor:
    """Watches system audio and starts/stops/ducks the ambient player.

    Every worker thread gets its own stop Event. A stopped worker can never be
    revived by a later start(), and a new worker waits for the previous one
    to exit before doing anything, so there is never more than one active.
    """

    POLL_INTERVAL      = 0.5
    PREVIOUS_JOIN_SECS = 5.0

    def __init__(self, app, playing_apps_source=None, clock=time.time):
        self.app = app
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = None
        self._get_playing_apps = playing_apps_source or get_playing_apps
        self._clock = clock
        self.info = MonitorInfo("starting")

    @property
    def running(self):
        with self._lock:
            return (self._thread is not None and self._stop_event is not None
                    and not self._stop_event.is_set() and self._thread.is_alive())

    @property
    def thread(self):
        return self._thread

    def start(self):
        """Start monitoring. Safe to call repeatedly; returns False if already running."""
        with self._lock:
            if (self._thread is not None and self._stop_event is not None
                    and not self._stop_event.is_set() and self._thread.is_alive()):
                return False
            previous = self._thread
            stop_event = threading.Event()
            thread = threading.Thread(target=self._run, args=(stop_event, previous),
                                      daemon=True, name="AudioMonitor")
            self._stop_event = stop_event
            self._thread = thread
            thread.start()
        return True

    def stop(self, wait=False, timeout=2.0):
        """Stop monitoring. Safe to call repeatedly and from any thread."""
        with self._lock:
            thread, stop_event = self._thread, self._stop_event
        if stop_event is not None:
            stop_event.set()
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Monitor worker did not exit within %.1fs", timeout)

    def _stop_from_worker(self, stop_event, reason):
        """A worker decided to stop itself (e.g. playback finished)."""
        with self._lock:
            is_current = self._stop_event is stop_event
        stop_event.set()
        if is_current:
            self.app.on_monitor_auto_stopped(reason)

    def _run(self, stop_event, previous):
        if previous is not None and previous.is_alive():
            previous.join(self.PREVIOUS_JOIN_SECS)
            if previous.is_alive():
                log.warning("Previous monitor worker still busy after %.1fs",
                            self.PREVIOUS_JOIN_SECS)
        if stop_event.is_set():
            return
        log.info("Monitoring started")
        self.info = MonitorInfo("starting")
        state = _MonitorState()
        self.app.set_status("Monitoring... waiting for silence.")
        while not stop_event.wait(self.POLL_INTERVAL):
            try:
                self._tick(state, stop_event)
            except Exception:
                # Unknown audio state: don't count this time as silence.
                state.silence_start = None
                log_throttled("monitor_tick", logging.ERROR,
                              "Monitor cycle failed", exc_info=True)
        log.info("Monitoring stopped")

    @staticmethod
    def _ambient_audible(player):
        """True while our own sound can be heard, fade-out after stop() included."""
        audible = getattr(player, "audible", None)
        return bool(player.playing if audible is None else audible)

    def _apply_mirror_fix(self, raw_apps, settings, can_mirror=True):
        """Drop apps that only mirror other sound.

        A mirroring app can only show a level when there is something to
        mirror: our ambient sound, or an excluded app that is playing. If
        neither is audible, its level is real (for example someone's Discord
        stream), so the app is not checked and counts as playing. Checking it
        anyway made stream audio look like "mirroring" whenever the tap
        happened to read silence.
        """
        fix = self.app.mirror_fix
        if not (settings.mirror_fix and fix is not None and settings.mirror_apps
                and can_mirror):
            return raw_apps
        # Apps on the mirroring list only count if they really output sound.
        return {name for name in raw_apps
                if name not in settings.mirror_apps or fix.is_real_audio(name)}

    @staticmethod
    def _log_mirror_decision(s, raw_apps, current_apps, settings, can_mirror):
        """Log (on change only) when a mirroring-list app is ignored or let through."""
        listed = frozenset(n for n in raw_apps if n in settings.mirror_apps)
        ignored = listed - frozenset(current_apps)
        unchecked = listed if (settings.mirror_fix and not can_mirror) else frozenset()
        note = (ignored, unchecked)
        if note == s.mirror_note:
            return
        s.mirror_note = note
        if ignored:
            log.info("Ignoring %s: it only mirrors other sound", ", ".join(sorted(ignored)))
        if unchecked:
            log.info("%s is making sound and there is nothing for it to mirror; "
                     "counting it as playing", ", ".join(sorted(unchecked)))

    def _check_suspects(self, s, current_apps, settings, now):
        """An app that starts "playing" right after our ambient sound starts is
        probably reacting to it. Tell the UI so it can offer a one-click fix."""
        if now - s.ambient_started_at > settings.fade_secs + 2.0 + MIRROR_SUSPECT_SECONDS:
            return
        suspects = sorted(n for n in current_apps if n not in settings.mirror_apps)
        if suspects:
            log.info("Audio from %s started right after ambient; may be mirroring",
                     ", ".join(suspects))
            self.app.on_suspected_mirror(suspects)

    def _tick(self, s, stop_event):
        app = self.app
        settings = runtime_settings(app.config)
        raw_apps = self._get_playing_apps(settings.excluded)
        if stop_event.is_set():
            return
        player = app.player
        now = self._clock()
        if s.ambient_triggered or self._ambient_audible(player):
            s.ambient_audible_at = now
        excluded_audible = getattr(raw_apps, "excluded_audible", None)
        can_mirror = bool(excluded_audible) or (
            now - s.ambient_audible_at <= MIRROR_TAIL_SECONDS)
        current_apps = self._apply_mirror_fix(raw_apps, settings, can_mirror)
        self._log_mirror_decision(s, raw_apps, current_apps, settings, can_mirror)
        names = ", ".join(sorted(current_apps))
        apps = tuple(sorted(current_apps))

        if s.ambient_triggered:
            if now < s.cooldown_until:
                self.info = MonitorInfo("playing")
                app.set_status("Playing ambient sound...")
                return

            if not player.playing and not s.ducked:
                s.ambient_triggered = False
                s.silence_start = None
                reason = player.last_error or "Playback finished — monitoring stopped."
                log.info("Ambient playback ended; stopping monitoring (%s)", reason)
                self._stop_from_worker(stop_event, reason)
                return

            if current_apps:
                if settings.duck_percent == 0:
                    s.ducked = False
                    s.unduck_start = None
                    log.info("External audio detected (%s); stopping ambient", names)
                    self._check_suspects(s, current_apps, settings, now)
                    app.stop_ambient()
                    s.ambient_triggered = False
                    s.silence_start = None
                    self.info = MonitorInfo("audio", apps=apps)
                    app.set_status(f"External audio detected ({names}) — ambient stopped.")
                else:
                    if not s.ducked:
                        s.ducked = True
                        s.unduck_start = None
                        duck_vol = (settings.duck_percent / 100.0) * settings.max_vol
                        log.info("External audio detected (%s); ducking to %d%%",
                                 names, int(settings.duck_percent))
                        self._check_suspects(s, current_apps, settings, now)
                        app.duck_ambient(duck_vol)
                    else:
                        s.unduck_start = None
                    self.info = MonitorInfo("ducked", apps=apps,
                                            duck_percent=int(settings.duck_percent))
                    app.set_status(
                        f"External audio detected — ambient ducked to {int(settings.duck_percent)}%.")
            else:
                if s.ducked:
                    if s.unduck_start is None:
                        s.unduck_start = now
                    remaining = settings.silence_secs - (now - s.unduck_start)
                    if remaining > 0:
                        self.info = MonitorInfo("ducked", remaining=remaining,
                                                duck_percent=int(settings.duck_percent))
                        app.set_status(f"Silence returned — fading back up in {remaining:.0f}s")
                    else:
                        s.ducked = False
                        s.unduck_start = None
                        log.info("Silence returned; restoring ambient volume")
                        app.unduck_ambient(settings.max_vol)
                        self.info = MonitorInfo("playing")
                        app.set_status("Playing ambient sound...")
                else:
                    self.info = MonitorInfo("playing")
                    app.set_status("Playing ambient sound...")
        else:
            if current_apps:
                s.silence_start = None
                self.info = MonitorInfo("audio", apps=apps)
                app.set_status(f"Audio playing ({names}). Monitoring...")
            else:
                if s.silence_start is None:
                    s.silence_start = now
                elapsed = now - s.silence_start
                if elapsed >= settings.silence_secs:
                    log.info("Silent for %.0fs; starting ambient", elapsed)
                    if app.play_ambient():
                        self.info = MonitorInfo("playing")
                        s.ambient_triggered = True
                        s.ambient_started_at = now
                        s.cooldown_until = now + settings.fade_secs + 2.0
                        s.ducked = False
                        s.unduck_start = None
                    else:
                        s.silence_start = None
                        reason = player.last_error or "Could not start ambient sound — monitoring stopped."
                        self._stop_from_worker(stop_event, reason)
                else:
                    remaining = settings.silence_secs - elapsed
                    self.info = MonitorInfo("waiting", remaining=remaining)
                    app.set_status(f"Silence detected... playing in {remaining:.0f}s")


# ─────────────────────────────────────────────────────────────────────────────
# Ambient player
# ─────────────────────────────────────────────────────────────────────────────

class _SessionVolume:
    """Sets this process's own volume in the Windows mixer (ISimpleAudioVolume).

    Why not VLC's audio_set_volume()? VLC 3.0 doesn't apply a volume change
    immediately: it needs the audio output lock (aout_VolumeSet), and while
    the WASAPI output waits for buffer space it holds that lock and sleeps for
    half the buffer length (wasapi.c). Requests made meanwhile pile up and
    only the last one is applied when the lock is released. A 2 s fade then
    plays as a few big jumps (GitHub issue #2: seen with .opus files).

    VLC's Windows output ends up changing this same control (mmdevice.c,
    with level = volume³), so calling it directly gives identical loudness,
    applied instantly. Falls back to VLC when the session can't be found
    (before the first stream exists, or if pycaw fails).
    """

    LOOKUP_INTERVAL = 0.1

    def __init__(self, pid=None, sessions_source=None):
        self._pid = pid or os.getpid()
        self._sessions_source = sessions_source or (lambda: list_sessions())
        self._controls = []
        self._last_lookup = float("-inf")
        self._lock = threading.Lock()

    def refresh(self):
        with self._lock:
            self._last_lookup = float("-inf")
            if not self._controls:
                self._lookup_locked()

    def _lookup_locked(self):
        now = time.monotonic()
        if now - self._last_lookup < self.LOOKUP_INTERVAL:
            return
        self._last_lookup = now
        controls = []
        try:
            for session in self._sessions_source():
                try:
                    if session.ProcessId == self._pid:
                        controls.append(session.SimpleAudioVolume)
                except Exception:
                    continue
        except Exception:
            log_throttled("session_volume_lookup", logging.WARNING,
                          "Could not list audio sessions for volume control", exc_info=True)
        if controls and not self._controls:
            log.info("Using Windows mixer volume for fades (%d session%s)",
                     len(controls), "s" if len(controls) != 1 else "")
        self._controls = controls

    def set(self, percent):
        """Set volume (0-100, VLC scale). Returns False if not possible right now."""
        level = max(0.0, min(1.0, percent / 100.0)) ** 3
        with self._lock:
            if not self._controls:
                self._lookup_locked()
                if not self._controls:
                    return False
            try:
                for control in self._controls:
                    control.SetMasterVolume(level, None)
                return True
            except Exception:
                log_throttled("session_volume_set", logging.WARNING,
                              "Setting Windows mixer volume failed; using VLC volume",
                              exc_info=True)
                self._controls = []
                return False


class TrackInfo(NamedTuple):
    path: str
    position: float = None
    length: float = None


class _PlaybackSession:
    """One play() call. Owns the VLC player from start until its thread exits."""

    def __init__(self, mode, source):
        self.mode = mode
        self.source = source
        self.stop_event = threading.Event()   # set by stop(); never cleared
        self.track_started = threading.Event()
        self.ever_started = False
        self.finished = False
        self.thread = None
        self.current_path = None
        self.current_index = 0
        self.pending_pos = 0.0                # start position of the track being loaded
        self.error = None


class AmbientPlayer:
    """VLC playback with fades, ducking and resume.

    Threading rules:
      - Each play() creates a session with its own stop Event and thread. A
        new session waits for the previous session's thread (and its fade-out)
        to finish before touching VLC, so two sessions never drive it at once.
      - stop() never blocks: it records the resume point and signals the
        session, whose own thread fades out and stops VLC.
      - Every volume change (fade in/out, duck, unduck) takes a new ramp
        generation; an older ramp sees the generation change and stops, so
        fades can't fight each other.
    """

    FADE_STEPS        = 50
    DUCK_STEPS        = 20
    DUCK_STEP_SECONDS = 0.05
    LOAD_TIMEOUT      = 5.0
    AUDIO_START_TIMEOUT = 3.0   # max wait for sound to actually begin before fading in
    POLL_SECONDS      = 0.3

    def __init__(self, app):
        self.app = app
        self._lock = threading.RLock()
        self._session = None
        self._ramp_gen = 0
        self._resume = None
        self._applied_vol = 0       # last volume we gave VLC
        self._target_vol = 0        # where volume should settle (max or ducked)
        self._closed = False
        self.last_error = None
        self.vlc_instance = vlc.Instance("--quiet", "--no-video")
        if self.vlc_instance is None:
            raise RuntimeError("VLC could not be initialised. libvlc.dll or the "
                               "plugins folder may be missing or incomplete.")
        self.media_player = self.vlc_instance.media_player_new()
        self._session_volume = _SessionVolume()

    # ── state ──

    @property
    def playing(self):
        with self._lock:
            s = self._session
            return s is not None and not s.finished and not s.stop_event.is_set()

    def track_info(self):
        """(path, position_s, length_s) of what is playing, or None. Times are
        None until VLC knows them."""
        with self._lock:
            s = self._session
            if s is None:
                return None
            path, started = s.current_path, s.track_started.is_set()
        pos = length = None
        if started:
            try:
                ms = self.media_player.get_time()
                pos = ms / 1000.0 if ms is not None and ms >= 0 else None
                ms = self.media_player.get_length()
                length = ms / 1000.0 if ms is not None and ms > 0 else None
            except Exception:
                log_throttled("vlc_track_info", logging.DEBUG, "VLC time query failed",
                              exc_info=True, interval=300)
        return TrackInfo(path, pos, length)

    @property
    def audible(self):
        """True while our sound may still be coming out, including the fade-out
        after stop() (the session ends only once the fade is over)."""
        with self._lock:
            return self._session is not None

    @property
    def resume_state(self):
        with self._lock:
            return self._resume

    def _get_fade_secs(self):
        return fade_seconds(self.app.config)

    # ── VLC helpers ──

    def _set_volume(self, vol_percent):
        vol = max(0, min(100, int(vol_percent)))
        self._applied_vol = vol
        if self._session_volume.set(vol):
            return
        try:
            self.media_player.audio_set_volume(vol)
        except Exception:
            log_throttled("vlc_volume", logging.WARNING, "VLC audio_set_volume failed",
                          exc_info=True)

    def _is_playing(self):
        try:
            return bool(self.media_player.is_playing())
        except Exception:
            log_throttled("vlc_is_playing", logging.WARNING, "VLC is_playing failed",
                          exc_info=True)
            return False

    def _media_stop(self):
        try:
            self.media_player.stop()
        except Exception:
            log.warning("VLC stop failed", exc_info=True)

    # ── volume ramps ──

    def _new_ramp(self):
        with self._lock:
            self._ramp_gen += 1
            return self._ramp_gen

    def _ramp(self, gen, target, secs, steps, abort_event=None, start=None,
              stop_when_not_playing=False):
        """Move volume to `target` over `secs`. Returns False if superseded/aborted."""
        with self._lock:
            begin = self._applied_vol if start is None else start
        step_time = secs / steps if steps else 0
        for i in range(1, steps + 1):
            with self._lock:
                if gen != self._ramp_gen:
                    return False
                if abort_event is not None and abort_event.is_set():
                    return False
                if stop_when_not_playing and not self._is_playing():
                    return True
                self._set_volume(begin + (target - begin) * (i / steps))
            if step_time > 0:
                time.sleep(step_time)
        return True

    def _start_level_ramp(self, target_vol, secs, steps, what):
        with self._lock:
            s = self._session
            if s is None or s.finished or s.stop_event.is_set():
                return
            self._target_vol = max(0, min(100, target_vol))
            if not s.track_started.is_set():
                return   # the track's fade-in will go to the new target
            gen = self._new_ramp()
        threading.Thread(target=self._ramp,
                         args=(gen, target_vol, secs, steps),
                         kwargs={"abort_event": s.stop_event},
                         daemon=True, name=f"Volume-{what}").start()

    def duck(self, target_vol):
        self._start_level_ramp(target_vol, self.DUCK_STEPS * self.DUCK_STEP_SECONDS,
                               self.DUCK_STEPS, "duck")

    def unduck(self, target_vol):
        self._start_level_ramp(target_vol, self._get_fade_secs(), self.FADE_STEPS, "unduck")

    # ── play / stop ──

    def play(self, config):
        """Start playback in the background. Returns False if nothing can be played."""
        with self._lock:
            if self._closed:
                return False
            if self.playing:
                return True

        # File-system checks happen outside the lock: a slow network folder
        # must not stall a stop() coming from the UI.
        mode = config.get("mode", "single")
        error = validate_ambient_source(config)
        files = []
        if error is None and mode == "playlist":
            files = collect_playlist(config["playlist_folder"])
            if not files:
                error = f"The playlist folder doesn't contain any supported audio files ({SUPPORTED_LABEL})."
        if error:
            self.last_error = error.replace("\n", " ")
            log.warning("Cannot start ambient: %s", self.last_error)
            self.app.set_status(self.last_error)
            return False

        with self._lock:
            if self._closed:
                return False
            if self.playing:
                return True
            resume, self._resume = self._resume, None
            max_vol = runtime_settings(config).max_vol
            if mode == "single":
                path = config["mp3_path"]
                s = _PlaybackSession("single", path)
                start_pos = plan_single_start(path, resume)
                s.current_path, s.current_index, s.pending_pos = path, 0, start_pos
                target, args = self._run_single, (s, path, max_vol,
                                                  config.get("single_loop_mode", "loop"))
            else:
                folder = config["playlist_folder"]
                s = _PlaybackSession("playlist", folder)
                index, start_pos = plan_playlist_start(folder, files, resume)
                s.current_path, s.current_index, s.pending_pos = files[index], index, start_pos
                target, args = self._run_playlist, (s, files, index, start_pos, max_vol)

            if resume is not None:
                log.info("Resume point %s @ %.1fs → starting %s @ %.1fs",
                         os.path.basename(resume.path or "?"), resume.position,
                         os.path.basename(s.current_path), s.pending_pos)

            previous = self._session.thread if self._session is not None else None
            self.last_error = None
            self._session = s
            s.thread = threading.Thread(target=self._run_session,
                                        args=(s, previous, target, args),
                                        daemon=True, name="AmbientPlayback")
            s.thread.start()
            return True

    def stop(self, wait=False, timeout=None):
        """Stop playback with a fade-out. Never blocks unless wait=True."""
        with self._lock:
            s = self._session
            if s is None:
                return
            if not s.finished and not s.stop_event.is_set():
                self._resume = self._capture_resume(s)
                log.info("Stopping ambient; resume point %s @ %.1fs",
                         os.path.basename(self._resume.path or "?"), self._resume.position)
                s.stop_event.set()
                if s.ever_started:
                    # Cancel this session's duck/unduck now. (If it never touched
                    # VLC, a previous session may still be fading out; leave it.)
                    self._new_ramp()
            thread = s.thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def shutdown(self, timeout=None):
        """Stop (with fade), wait for the playback thread, release VLC."""
        if timeout is None:
            timeout = self._get_fade_secs() + self.LOAD_TIMEOUT + 2.0
        self.stop(wait=True, timeout=timeout)
        with self._lock:
            self._closed = True
        try:
            self.media_player.stop()
            self.media_player.release()
            self.vlc_instance.release()
        except Exception:
            log.warning("Releasing VLC failed", exc_info=True)

    def _capture_resume(self, s):
        pos = s.pending_pos
        if s.track_started.is_set():
            try:
                ms = self.media_player.get_time()
                if ms is not None and ms >= 0:
                    pos = ms / 1000.0
            except Exception:
                log.warning("VLC get_time failed; resuming at track start", exc_info=True)
        return ResumeState(s.mode, s.source, s.current_path, s.current_index, float(pos))

    # ── session thread ──

    def _run_session(self, s, previous, target, args):
        try:
            if previous is not None and previous.is_alive():
                previous.join(self._get_fade_secs() + self.LOAD_TIMEOUT + 2.0)
                if previous.is_alive():
                    log.error("Previous playback thread is stuck; starting anyway")
            if not s.stop_event.is_set():
                target(*args)
        except Exception as e:
            log.exception("Playback error")
            s.error = f"Playback error: {e}"
            self.app.set_status(s.error)
        finally:
            try:
                if s.ever_started:
                    if s.stop_event.is_set():
                        gen = self._new_ramp()
                        self._ramp(gen, 0, self._get_fade_secs(), self.FADE_STEPS,
                                   stop_when_not_playing=True)
                    self._media_stop()
            except Exception:
                log.exception("Error while stopping playback")
                self._media_stop()
            with self._lock:
                s.finished = True
                if s.error:
                    self.last_error = s.error
                if self._session is s:
                    self._session = None

    def _load_and_play(self, s, path, start_pos=0.0, wait_for_audio=False):
        """Load `path` and wait until VLC is actually playing it.

        With wait_for_audio, also wait until sound really comes out (see
        _wait_for_audio_start) while holding the volume at 0.
        """
        if not os.path.isfile(path):
            log.warning("Audio file missing: %s", path)
            return False
        with self._lock:
            s.ever_started = True   # from here on this session must stop VLC on exit
        media = self.vlc_instance.media_new(path)
        self.media_player.set_media(media)
        try:
            media.release()   # the player keeps its own reference
        except Exception:
            pass
        t_play = time.monotonic()
        self.media_player.play()

        deadline = time.monotonic() + self.LOAD_TIMEOUT
        while not self._is_playing():
            if s.stop_event.wait(0.1):
                return False
            try:
                state = self.media_player.get_state()
            except Exception:
                state = None
            if state == vlc.State.Error:
                log.warning("VLC could not play %s", path)
                return False
            if time.monotonic() > deadline:
                log.warning("VLC did not start %s within %.0fs (state %s)",
                            path, self.LOAD_TIMEOUT, state)
                return False

        if start_pos > 0.5:
            try:
                length_ms = self.media_player.get_length()
            except Exception:
                length_ms = -1
            if length_ms and length_ms > 0 and start_pos * 1000 >= length_ms - 1000:
                log.info("Resume point %.1fs is past the end of %s; starting from 0",
                         start_pos, os.path.basename(path))
            else:
                self.media_player.set_time(int(start_pos * 1000))
        t_playing = time.monotonic()
        if wait_for_audio:
            if not self._wait_for_audio_start(s):
                return False
            log.info("Started %s: VLC 'playing' after %.0f ms, sound after %.0f ms",
                     os.path.basename(path), (t_playing - t_play) * 1000,
                     (time.monotonic() - t_play) * 1000)
        return True

    def _wait_for_audio_start(self, s):
        """Wait until VLC's playback clock starts moving, i.e. sound is coming out.

        VLC reports "playing" well before the first audio is heard: about
        270 ms for MP3 and about 830 ms for Opus (measured with VLC 3.0), plus
        the time Windows needs to open the audio device the first time. A
        fade-in started at "playing" is therefore partly spent in silence and
        the sound then jumps in at a high volume (GitHub issue #2). After a
        seek, get_time() reports the target at once and only counts up once
        audio really plays, so "moved past its first value" works for both.
        Holds the mixer volume at 0 meanwhile, in case VLC re-applies an older
        volume when it opens the audio output.
        """
        try:
            baseline = max(0, self.media_player.get_time() or 0)
        except Exception:
            return True
        deadline = time.monotonic() + self.AUDIO_START_TIMEOUT
        while time.monotonic() < deadline:
            if s.stop_event.wait(0.01):
                return False
            # Mixer session only: a VLC volume request made now could be
            # deferred by VLC and land in the middle of the fade (see
            # _SessionVolume). VLC itself was set to 0 before play().
            self._session_volume.set(0)
            try:
                if (self.media_player.get_time() or 0) > baseline:
                    # The audio stream (and its mixer session) exists now.
                    self._session_volume.refresh()
                    self._session_volume.set(0)
                    return True
            except Exception:
                return True
        log.info("Playback clock didn't start within %.1fs; fading in anyway",
                 self.AUDIO_START_TIMEOUT)
        return True

    def _start_track(self, s, path, start_pos, index, fade):
        with self._lock:
            if s.stop_event.is_set():
                return False
            s.track_started.clear()
            s.current_path, s.current_index, s.pending_pos = path, index, start_pos
            self._new_ramp()
            # Start silent when fading in (no blip at the old volume).
            self._set_volume(0 if fade else self._target_vol)
        if not self._load_and_play(s, path, start_pos, wait_for_audio=fade):
            return False
        with self._lock:
            if s.stop_event.is_set():
                return False        # the session thread fades out and stops VLC
            s.track_started.set()
            if not fade:
                self._set_volume(self._target_vol)
                return True
            gen = self._new_ramp()
            target = self._target_vol
        self._ramp(gen, target, self._get_fade_secs(), self.FADE_STEPS,
                   abort_event=s.stop_event, start=0)
        return not s.stop_event.is_set()

    def _wait_for_track_end(self, s):
        while not s.stop_event.is_set() and self._is_playing():
            s.stop_event.wait(self.POLL_SECONDS)

    def _run_single(self, s, path, max_vol, loop_mode):
        with self._lock:
            self._target_vol = max_vol
        if not self._start_track(s, path, s.pending_pos, 0, fade=True):
            if not s.stop_event.is_set():
                s.error = f"Could not play {os.path.basename(path)}."
                self.app.set_status(s.error)
            return
        while not s.stop_event.is_set():
            self._wait_for_track_end(s)
            if s.stop_event.is_set() or loop_mode != "loop":
                return
            if not self._start_track(s, path, 0.0, 0, fade=False):
                if not s.stop_event.is_set():
                    s.error = f"Could not play {os.path.basename(path)} — the file may have been moved or deleted."
                    self.app.set_status(s.error)
                return

    def _run_playlist(self, s, files, index, start_pos, max_vol):
        with self._lock:
            self._target_vol = max_vol
        failures = 0
        while not s.stop_event.is_set():
            loop_mode = self.app.config.get("playlist_loop_mode", "loop_playlist")
            if index >= len(files):
                if loop_mode == "loop_playlist":
                    index = 0
                else:
                    return
            path = files[index]
            self.app.set_status(f"Playing: {os.path.basename(path)}")
            if not self._start_track(s, path, start_pos, index, fade=True):
                if s.stop_event.is_set():
                    return
                failures += 1
                if failures >= len(files):
                    s.error = "None of the files in the playlist could be played."
                    self.app.set_status(s.error)
                    return
                index += 1
                start_pos = 0.0
                continue
            failures = 0
            start_pos = 0.0
            self._wait_for_track_end(s)
            if s.stop_event.is_set():
                return
            current_mode = self.app.config.get("playlist_loop_mode", "loop_playlist")
            if current_mode == "loop_song":
                continue            # same index: the next iteration loads it once
            if current_mode == "stop":
                return
            index += 1


# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Now playing
# ─────────────────────────────────────────────────────────────────────────────

TRAY_TIP_MAX = 127      # Windows cuts tray tooltips at 127 characters
TRAY_MENU_MAX = 60


class NowPlaying(NamedTuple):
    badge: str          # "Playing", "Ducked 50%", "Waiting", "Off"
    kind: str           # "playing", "ducked", "waiting", "off": picks the colours
    title: str          # file name
    detail: str         # time, countdown or reason
    progress: float     # 0..1, or None for no bar
    tooltip: str        # tray icon tooltip
    menu: str           # first (greyed) line of the plain Windows tray menu (fallback)
    menu_head: str = "" # Silence Player's own tray menu: small coloured line...
    menu_line: str = "" # ...and the line under it


def format_clock(seconds):
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _shorten(text, limit):
    return text if len(text) <= limit else text[:max(1, limit - 1)].rstrip() + "…"


def _apps_text(apps):
    apps = list(apps)
    if not apps:
        return ""
    if len(apps) <= 2:
        return " and ".join(apps)
    return f"{apps[0]} and {len(apps) - 1} more"


def describe_now_playing(monitoring, info, track, config):
    """What the tray tooltip, tray menu and the window's card should show."""
    if track is not None and track.path:
        name = display_name(track.path)
    elif config.get("mode", "single") == "playlist":
        name = display_name(config.get("playlist_folder", ""))
    else:
        name = display_name(config.get("mp3_path", ""))
    name = name or "No file chosen"

    def tip(line):
        head = "Silence Player\n"
        return head + _shorten(line, TRAY_TIP_MAX - len(head))

    def menu(prefix):
        return prefix + _shorten(name, TRAY_MENU_MAX - len(prefix))

    def clock_and_bar():
        if track is None or track.position is None:
            return "Starting…", None
        if track.length:
            frac = max(0.0, min(1.0, track.position / track.length))
            return f"{format_clock(track.position)} / {format_clock(track.length)}", frac
        return format_clock(track.position), None

    def head(text):
        return _shorten(text, TRAY_MENU_MAX)

    if not monitoring:
        return NowPlaying("Off", "off", name, "Monitoring is off", None,
                          tip("Monitoring is off"), "Monitoring is off",
                          "Monitoring is off", head(name))
    phase = info.phase if info is not None else "starting"

    if phase == "playing":
        detail, bar = clock_and_bar()
        return NowPlaying("Playing", "playing", name, detail, bar,
                          tip(f"Playing: {name}"), menu("Playing: "),
                          "Now playing", head(name))
    if phase == "ducked":
        clock, bar = clock_and_bar()
        if info.apps:
            why = f"{_apps_text(info.apps)} is playing"
            menu_head = f"Ducked to {info.duck_percent}% for {_apps_text(info.apps)}"
        elif info.remaining is not None:
            secs = math.ceil(info.remaining)
            why = f"back to full volume in {secs}s"
            menu_head = f"Ducked to {info.duck_percent}%, back up in {secs}s"
        else:
            why = "another app is playing"
            menu_head = f"Ducked to {info.duck_percent}%"
        return NowPlaying(f"Ducked {info.duck_percent}%", "ducked", name, f"{clock} · {why}",
                          bar, tip(f"Quieter: {name}"), menu("Quieter: "),
                          head(menu_head), head(name))
    if phase == "audio":
        apps = _apps_text(info.apps)
        return NowPlaying("Waiting", "waiting", name, f"{apps} is playing", None,
                          tip(f"Waiting: {apps} is playing"), "Waiting for silence",
                          "Waiting for silence", head(f"{apps} is playing"))
    if phase == "waiting" and info.remaining is not None:
        secs = max(1, math.ceil(info.remaining))
        return NowPlaying("Waiting", "waiting", name, f"Plays in {secs}s if it stays silent",
                          None, tip(f"Ambient in {secs}s"), "Waiting for silence",
                          "Waiting for silence", f"Ambient starts in {secs}s")
    return NowPlaying("Waiting", "waiting", name, "Listening…", None,
                      tip("Listening…"), "Waiting for silence",
                      "Waiting for silence", "Listening…")


def make_tray_icon(name, image, title, menu, on_right_click=None):
    """A pystray icon whose right-click opens Silence Player's own menu.

    Windows draws the standard tray menu itself (no colours, no two-line
    header), so a right-click calls `on_right_click` instead. Left-click and
    the rest are pystray's. If `on_right_click` fails, the standard menu built
    from `menu` is shown, so the tray never becomes unusable.
    """
    base = pystray.Icon

    class TrayIcon(base):
        WM_RBUTTONUP = 0x0205

        def _on_notify(self, wparam, lparam):
            if on_right_click is not None and lparam == self.WM_RBUTTONUP:
                try:
                    on_right_click()
                    return None
                except Exception:
                    log.exception("Custom tray menu failed; showing the Windows menu")
            return super()._on_notify(wparam, lparam)

    return TrayIcon(name, image, title, menu)


def round_window_corners(win, border="#3d3d3d"):
    """Windows 11: small rounded corners and a border colour, like system menus.
    Does nothing on Windows 10 or other systems."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        hwnd = user32.GetAncestor(win.winfo_id(), 2)          # GA_ROOT
        corner = ctypes.c_int(3)                                # DWMWCP_ROUNDSMALL
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        r, g, b = (int(border[i:i + 2], 16) for i in (1, 3, 5))
        color = ctypes.c_uint(r | (g << 8) | (b << 16))         # COLORREF
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))
    except Exception:
        log.debug("Could not round the menu corners", exc_info=True)


class TrayMenu:
    """Silence Player's own right-click menu for the tray icon.

    A borderless Tk window: dark, rounded on Windows 11, with a coloured
    "Now playing" header. It closes when an item is picked, on Escape, when it
    loses focus, or when the mouse is clicked anywhere outside it.
    """

    BG, BORDER, FG, HOVER, SEP = "#2c2c2c", "#3d3d3d", "#f0f0f0", "#383838", "#3d3d3d"
    HEAD_COLORS = {"playing": "#a6e3a1", "ducked": "#f9e2af",
                   "waiting": "#9a9a9a", "off": "#9a9a9a"}
    MIN_WIDTH = 230
    POLL_MS = 40

    def __init__(self, root):
        self.root = root
        self.win = None
        self._head = self._line = None

    @property
    def is_open(self):
        return self.win is not None

    def show(self, head, line, kind, items, at=None):
        """items: (text, callback) tuples, or None for a separator."""
        self.close()
        win = tk.Toplevel(self.root, bg=self.BORDER)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self.win = win
        body = tk.Frame(win, bg=self.BG, padx=5, pady=5)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=self.BG, padx=10, pady=6)
        header.pack(fill="x")
        self._head = tk.Label(header, bg=self.BG, font=("Segoe UI", 9), anchor="w")
        self._head.pack(fill="x")
        self._line = tk.Label(header, bg=self.BG, fg=self.FG, font=("Segoe UI", 10),
                              anchor="w")
        self._line.pack(fill="x")
        self.update_header(head, line, kind)
        self._separator(body)

        for item in items:
            if item is None:
                self._separator(body)
                continue
            text, callback = item
            lbl = tk.Label(body, text=text, bg=self.BG, fg=self.FG, font=("Segoe UI", 10),
                           anchor="w", padx=10, pady=5, cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Enter>", lambda e, w=lbl: w.config(bg=self.HOVER))
            lbl.bind("<Leave>", lambda e, w=lbl: w.config(bg=self.BG))
            lbl.bind("<ButtonRelease-1>", lambda e, cb=callback: self._pick(cb))

        win.update_idletasks()
        w = max(self.MIN_WIDTH, win.winfo_reqwidth())
        h = win.winfo_reqheight()
        px, py = at if at is not None else self.root.winfo_pointerxy()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # Like the Windows menu: open up and to the left of the pointer when
        # there's no room below/right of it (the taskbar is usually at the bottom).
        x = px - w if px + w > sw else px
        y = py - h if py + h > sh else py
        win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        win.deiconify()
        round_window_corners(win, self.BORDER)
        win.lift()
        win.focus_force()
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<FocusOut>", lambda e: self.root.after(60, self._close_if_unfocused))
        self.root.after(self.POLL_MS, self._watch_clicks)

    def _separator(self, parent):
        tk.Frame(parent, bg=self.SEP, height=1).pack(fill="x", pady=4)

    def update_header(self, head, line, kind):
        if self.win is None:
            return
        self._head.config(text=head, fg=self.HEAD_COLORS.get(kind, "#9a9a9a"))
        self._line.config(text=line)

    def _pick(self, callback):
        self.close()
        try:
            callback()
        except Exception:
            log.exception("Tray menu action failed")

    def _close_if_unfocused(self):
        win = self.win
        if win is None:
            return
        try:
            focus = win.focus_displayof()
            keep = focus is not None and focus.winfo_toplevel() is win
        except Exception:
            keep = False
        if not keep:
            self.close()

    def _watch_clicks(self):
        """Close on a click outside the menu (works even without keyboard focus)."""
        win = self.win
        if win is None:
            return
        if sys.platform == "win32":
            try:
                import ctypes
                state = ctypes.windll.user32.GetAsyncKeyState
                pressed = any(state(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))
                if pressed:
                    px, py = win.winfo_pointerxy()
                    x0, y0 = win.winfo_rootx(), win.winfo_rooty()
                    inside = x0 <= px < x0 + win.winfo_width() and y0 <= py < y0 + win.winfo_height()
                    if not inside:
                        self.close()
                        return
            except Exception:
                log.debug("Tray menu click watch failed", exc_info=True)
        self.root.after(self.POLL_MS, self._watch_clicks)

    def close(self):
        win, self.win = self.win, None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass


class MarqueeLabel(tk.Canvas):
    """A one-line label that scrolls its text when it doesn't fit.

    - Fits → stays still.
    - Too long → waits, glides left at a slow constant speed, loops seamlessly
      (a second copy follows after a gap) and pauses again at the start of
      every cycle. Edges fade out instead of cutting letters in half.
    - Pauses while hovered (so anyone can stop it to read it), and while not
      visible (hidden tab/window).
    - Scrolls even when Windows "Animation effects" is off: people turn that
      off for the whole system, not to lose this. animate=False shows a still
      name ending in "…" instead.
    """

    SPEED_PX_PER_S = 30
    START_PAUSE_MS = 2500
    FRAME_MS       = 16
    GAP_PX         = 48
    PAD_X          = 8
    FADE_PX        = 18

    def __init__(self, parent, textvariable, width_chars, bg, fg,
                 font=("Segoe UI", 10), on_click=None, animate=True):
        self._font = tkfont.Font(root=parent, family=font[0], size=font[1])
        width = self._font.measure("0") * width_chars + 16
        height = self._font.metrics("linespace") + 14
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2" if on_click else "")
        self._width, self._height = width, height
        self._bg, self._var = bg, textvariable
        self._animate = animate
        self._hover = False
        self._after = None
        self._offset = 0.0
        self._text_w = 0
        self._scrolling = False
        self._last = None
        y = height / 2
        self._items = [self.create_text(self.PAD_X, y, anchor="w", fill=fg, font=self._font)
                       for _ in range(2)]
        self._fade_left, self._fade_right = self._make_fades()
        self._fade_items = [self.create_image(0, 0, anchor="nw", image=self._fade_left),
                            self.create_image(width - self.FADE_PX, 0, anchor="nw",
                                              image=self._fade_right)]
        if on_click:
            self.bind("<Button-1>", lambda e: on_click())
        self.bind("<Enter>", lambda e: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda e: self._set_hover(False), add="+")
        self.bind("<Destroy>", lambda e: self._cancel(), add="+")
        textvariable.trace_add("write", lambda *a: self.reset())
        self.reset()

    @property
    def text(self):
        return self._var.get()

    @property
    def scrolling(self):
        return self._scrolling

    def set_fg(self, color):
        for item in self._items:
            self.itemconfig(item, fill=color)

    def _make_fades(self):
        r, g, b = (v // 256 for v in self.winfo_rgb(self._bg))
        right = Image.new("RGBA", (self.FADE_PX, self._height))
        for x in range(self.FADE_PX):
            alpha = int(255 * (x + 1) / self.FADE_PX)
            for y in range(self._height):
                right.putpixel((x, y), (r, g, b, alpha))
        left = right.transpose(Image.FLIP_LEFT_RIGHT)
        return ImageTk.PhotoImage(left, master=self), ImageTk.PhotoImage(right, master=self)

    def _cancel(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _set_hover(self, on):
        self._hover = on
        self._last = None   # don't jump forward by the paused time

    def reset(self):
        """Re-measure the text and restart from the beginning."""
        self._cancel()
        text = self._var.get()
        avail = self._width - 2 * self.PAD_X
        self._text_w = self._font.measure(text)
        self._offset = 0.0
        self._last = None
        overflow = self._text_w > avail
        self._scrolling = overflow and self._animate
        shown = text
        if overflow and not self._animate:
            while shown and self._font.measure(shown + "…") > avail:
                shown = shown[:-1]
            shown = shown.rstrip() + "…"
        self.itemconfigure(self._items[0], text=shown)
        self.itemconfigure(self._items[1], text=text if self._scrolling else "")
        self._place()
        self.itemconfigure(self._fade_items[1], state="normal" if self._scrolling else "hidden")
        self.itemconfigure(self._fade_items[0], state="hidden")
        if self._scrolling:
            self._after = self.after(self.START_PAUSE_MS, self._tick)

    def _place(self):
        x = self.PAD_X - self._offset
        y = self._height / 2
        self.coords(self._items[0], x, y)
        self.coords(self._items[1], x + self._text_w + self.GAP_PX, y)

    def _tick(self):
        self._after = None
        if not self._scrolling:
            return
        if self._hover or not self.winfo_viewable():
            self._last = None
            self._after = self.after(200, self._tick)
            return
        now = time.monotonic()
        if self._last is not None:
            # Time-based, so the speed stays constant even if frames are late.
            self._offset += self.SPEED_PX_PER_S * (now - self._last)
        self._last = now
        cycle = self._text_w + self.GAP_PX
        if self._offset >= cycle:
            self._offset = 0.0
            self._last = None
            self._place()
            self.itemconfigure(self._fade_items[0], state="hidden")
            self._after = self.after(self.START_PAUSE_MS, self._tick)
            return
        self._place()
        self.itemconfigure(self._fade_items[0], state="normal")
        self._after = self.after(self.FRAME_MS, self._tick)


class App:
    WINDOW_W, WINDOW_H = 520, 760
    UI_POLL_MS = 50

    def __init__(self, root, instance=None, config_notice=None, config=None):
        self.root = root
        self.root.title("Silence Player")
        self.root.geometry(f"{self.WINDOW_W}x{self.WINDOW_H}")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.root.report_callback_exception = self._report_tk_exception

        try:
            self.root.iconphoto(True, tk.PhotoImage(file=resource_path("icon.png")))
        except Exception:
            log.warning("Could not load icon.png", exc_info=True)

        self._ui_queue   = queue.Queue()
        self._ui_alive   = True
        self._quitting   = False

        if config is None:
            config, config_notice = load_config()
        self.config      = config
        self.monitor     = AudioMonitor(self)
        self.player      = AmbientPlayer(self)
        self.mirror_fix  = None
        self._mirror_names = enabled_mirror_apps(config)
        self._dismissed_suspects = set()
        self._notified_suspects = set()
        self._suggested  = []
        self.monitoring  = False
        self._status     = "Ready."
        self.tray        = None
        self.instance    = instance
        self._startup    = StartupEntry()
        try:
            self._startup.repair_path()
        except Exception:
            log.warning("Could not check the Start with Windows entry", exc_info=True)
        self._version    = current_version()
        if self._version != APP_VERSION:
            log.warning("Pretending to be version %s to test the update notice", self._version)
        self._now = None                # last NowPlaying shown
        self._tray_menu = None          # created with the UI
        self._tray_tip = None
        self._update_info  = None       # a newer release, while one is known
        self._update_later = set()      # tags the user said "Later" to (this session)
        self._update_notified = set()
        self.updater = UpdateChecker(
            self._version,
            on_result=lambda info, err, manual: self.call_in_ui(
                self._on_update_result, info, err, manual),
            enabled=lambda: self.config.get("check_updates", True) is not False)

        self._build_ui()
        self.root.after(self.UI_POLL_MS, self._drain_ui_queue)
        threading.Thread(target=self._build_tray, daemon=True, name="Tray").start()
        self.root.after(0, self._refresh_now_playing)
        self.updater.start()
        if instance is not None:
            instance.listen(lambda: self.call_in_ui(self._show_window))

        problem = validate_ambient_source(self.config)
        if problem is None:
            self._start_monitoring()
            if config_notice:
                self.set_status(config_notice)
            else:
                self.root.after(100, self.root.withdraw)
        else:
            # First run (or the saved file/folder is gone): keep Settings open.
            log.info("No playable ambient source configured; showing Settings (%s)",
                     problem.replace("\n", " "))
            first_run = not self.config.get("mp3_path") and not self.config.get("playlist_folder")
            if first_run:
                msg = ("Welcome! Choose an ambient sound file (or a playlist folder), "
                       "then click Start Monitoring.")
            else:
                msg = problem.replace("\n", " ") + " Choose a new one, then click Start Monitoring."
            self.set_status(config_notice + " " + msg if config_notice else msg)
            self._show_window()

    # ── thread-safe UI access ──

    def call_in_ui(self, fn, *args):
        """Run fn(*args) on the Tk thread. Safe to call from any thread."""
        if self._ui_alive:
            self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        if not self._ui_alive:
            return
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    log.exception("UI callback failed")
        except queue.Empty:
            pass
        try:
            self.root.after(self.UI_POLL_MS, self._drain_ui_queue)
        except Exception:
            pass

    def _report_tk_exception(self, exc, val, tb):
        log.error("Unhandled Tk callback error", exc_info=(exc, val, tb))

    def set_status(self, msg):
        if msg == self._status:
            return
        self._status = msg
        self.call_in_ui(self.status_var.set, msg)

    # ── config persistence ──
    #
    # Changes apply to the running app immediately, but config.json is only
    # written when the user clicks "Save Settings" (see _save_settings).
    # Closing, quitting or starting monitoring never saves.

    def _save_now(self):
        return save_config(self.config)

    # ── Mirroring Fix ──

    def _apply_mirror_state(self):
        """Start or stop the Mirroring Fix to match the config."""
        self._mirror_names = enabled_mirror_apps(self.config)
        if self.config.get("mirror_fix_enabled", True) and PROCTAP_IMPORT_ERROR is None:
            if self.mirror_fix is None:
                self.mirror_fix = MirrorFix(apps_source=lambda: self._mirror_names)
                self.mirror_fix.start()
        else:
            if PROCTAP_IMPORT_ERROR is not None and self.config.get("mirror_fix_enabled", True):
                log_throttled("mirror_unavailable", logging.WARNING,
                              "Mirroring Fix unavailable: %s", PROCTAP_IMPORT_ERROR,
                              interval=3600)
            self._stop_mirror_fix()

    def _stop_mirror_fix(self, wait=False):
        if self.mirror_fix is not None:
            fix = self.mirror_fix
            self.mirror_fix = None  # set None first so monitor stops using it
            if wait:
                fix.stop()
            else:
                threading.Thread(target=fix.stop, daemon=True, name="MirrorStop").start()

    def _set_mirror_app(self, name, enabled=True):
        """Add `name` to the mirroring list (or change its toggle)."""
        apps = _coerce_mirror_apps(self.config.get("mirror_apps", []))
        for a in apps:
            if a["name"] == name:
                a["enabled"] = enabled
                break
        else:
            apps.append({"name": name, "enabled": enabled})
        self.config["mirror_apps"] = apps

    def on_suspected_mirror(self, names):
        """Called from the monitor thread."""
        self.call_in_ui(self._show_mirror_suggestion, list(names))

    def _show_mirror_suggestion(self, names):
        excluded = set(self.config.get("excluded_apps", []))
        names = [n for n in names
                 if n not in self._dismissed_suspects and n not in self._mirror_names
                 and n not in excluded]
        if not names:
            return
        self._suggested = names
        label = ", ".join(names)
        self.suggest_label.config(
            text=f"{label} started playing right after your ambient sound. "
                 f"It may only be reacting to it (mirroring).")
        self.suggest_btn.config(
            text="Treat as mirroring app" if PROCTAP_IMPORT_ERROR is None else "Exclude it")
        if not self._bar_shown(self.suggest_row):
            self.suggest_row.pack(fill="x", before=self.status_label)
            self._fit_window()
        new = [n for n in names if n not in self._notified_suspects]
        if new and self.tray is not None and self.root.state() == "withdrawn":
            self._notified_suspects.update(new)
            try:
                self.tray.notify(f"{', '.join(new)} may be mirroring your ambient sound. "
                                 f"Open Settings to fix it in one click.", "Silence Player")
            except Exception:
                log.debug("Tray notification failed", exc_info=True)

    def _hide_mirror_suggestion(self):
        if self._bar_shown(self.suggest_row):
            self.suggest_row.pack_forget()
            self._fit_window()
        self._suggested = []

    # ── bars above the status line ──

    @staticmethod
    def _bar_shown(bar):
        # winfo_ismapped() is False while the window is hidden in the tray.
        return bar.winfo_manager() == "pack"

    def _fit_window(self):
        """Grow the (fixed-size) window so shown bars don't cover the settings."""
        self.root.update_idletasks()
        extra = sum(bar.winfo_reqheight() for bar in (self.update_row, self.suggest_row)
                    if self._bar_shown(bar))
        self.root.geometry(f"{self.WINDOW_W}x{self.WINDOW_H + extra}")

    # ── update notice ──

    def _on_update_result(self, info, error, manual):
        if info is not None:
            self._update_info = info
            if manual or info.tag not in self._update_later:
                self._show_update_bar(info)
        elif error is None:
            # Up to date (or the newer release was skipped / withdrawn).
            self._update_info = None
            self._hide_update_bar()
            if manual:
                self.set_status(f"You have the latest version (v{self._version}).")
        elif manual:
            self.set_status("Couldn't check for updates. Check your internet connection "
                            "and try again.")
        self._refresh_update_status()
        self._refresh_tray_menu()

    def _show_update_bar(self, info):
        self.update_label.config(
            text=f"Silence Player v{info.version} is available. You have v{self._version}.")
        if not self._bar_shown(self.update_row):
            self.update_row.pack(fill="x", before=self.status_label)
            self._fit_window()
        if (info.tag not in self._update_notified and self.tray is not None
                and self.root.state() == "withdrawn"):
            self._update_notified.add(info.tag)
            try:
                self.tray.notify(f"Version {info.version} is available. Right-click the "
                                 f"tray icon to download it.", "Silence Player update")
            except Exception:
                log.debug("Tray notification failed", exc_info=True)

    def _hide_update_bar(self):
        if self._bar_shown(self.update_row):
            self.update_row.pack_forget()
            self._fit_window()

    def _open_update(self):
        info = self._update_info
        url = info.url if info is not None else RELEASES_URL
        log.info("Opening the release page: %s", url)
        try:
            webbrowser.open(url)
            self.set_status("Opened the download page in your browser.")
        except Exception:
            log.warning("Could not open the browser", exc_info=True)
            self.set_status(f"Couldn't open your browser. The download is at {url}")
        self._hide_update_bar()

    def _skip_update(self):
        info = self._update_info
        if info is not None:
            self.updater.skip(info.tag)
            self.set_status(f"OK, you won't be reminded about v{info.version}. "
                            f"Advanced > Check now still shows it.")
        self._update_info = None
        self._hide_update_bar()
        self._refresh_update_status()
        self._refresh_tray_menu()

    def _update_later_clicked(self):
        if self._update_info is not None:
            self._update_later.add(self._update_info.tag)
        self._hide_update_bar()

    def _check_updates_now(self):
        self.update_status.config(text="Checking…", fg=self._DIM)
        self.updater.check_now()

    def _refresh_update_status(self):
        on = self.config.get("check_updates", True) is not False
        self.update_toggle.config(bg=self._GOOD if on else "#313244",
                                  fg="#1e1e2e" if on else "#6c7086",
                                  text="Enabled" if on else "Disabled")
        u = self.updater
        if self._update_info is not None:
            text, color = f"● v{self._update_info.version} is available", self._GOOD
        elif u.last_checked is None:
            text = ("● Checks GitHub for new versions" if on
                    else "● Off — Silence Player won't check GitHub")
            color = self._DIM
        elif u.last_error is not None:
            text, color = "● Couldn't check (no internet?)", "#f9e2af"
        else:
            when = time.strftime("%H:%M", time.localtime(u.last_checked))
            text, color = f"● Up to date (checked at {when})", self._DIM
        if self._version != APP_VERSION:
            text += f" · test mode: acting as v{self._version}"
        self.update_status.config(text=text, fg=color)

    def _dismiss_mirror_suggestion(self):
        self._dismissed_suspects.update(self._suggested)
        self._hide_mirror_suggestion()

    def _accept_mirror_suggestion(self):
        names = list(self._suggested)
        self._hide_mirror_suggestion()
        if not names:
            return
        label = ", ".join(names)
        if PROCTAP_IMPORT_ERROR is None:
            for n in names:
                self._set_mirror_app(n, True)
            if not self.config.get("mirror_fix_enabled", True):
                self.config["mirror_fix_enabled"] = True
                self.mirror_fix_var.set(True)
            self._apply_mirror_state()
            self._refresh_mirror_ui()
            log.info("User added %s to the Mirroring Fix", label)
            self.set_status(f"{label} added to the Mirroring Fix (ignored unless it really "
                            f"plays sound). Click Save Settings to keep it.")
        else:
            apps = list(self.config.get("excluded_apps", []))
            for n in names:
                if n not in apps:
                    apps.append(n)
                    self.exclude_listbox.insert(tk.END, n)
            self.config["excluded_apps"] = apps
            log.info("User excluded %s", label)
            self.set_status(f"{label} added to Exclude Apps. Click Save Settings to keep it.")

    # ── tray ──

    def _build_tray(self):
        try:
            icon_image = create_tray_icon()
            menu = pystray.Menu(
                pystray.MenuItem(lambda item: self._now.menu if self._now else "Silence Player",
                                 lambda icon, item: None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Settings", self._tray_open, default=True),
                pystray.MenuItem(
                    lambda item: (f"Download v{self._update_info.version}"
                                  if self._update_info is not None else "Download update"),
                    self._tray_update,
                    visible=lambda item: self._update_info is not None),
                pystray.MenuItem(
                    lambda item: "Stop Monitoring" if self.monitoring else "Start Monitoring",
                    self._tray_toggle),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._tray_quit),
            )
            tip = self._now.tooltip if self._now else "Silence Player"
            self.tray = make_tray_icon("SilencePlayer", icon_image, tip, menu,
                                       on_right_click=lambda: self.call_in_ui(self._open_tray_menu))
            self._tray_tip = tip
            self.tray.run()
        except Exception:
            log.exception("System tray icon failed")
            # Without a tray icon the user could never reopen a hidden window.
            self.call_in_ui(self._show_window)

    def _refresh_tray_menu(self):
        tray = self.tray
        if tray is not None:
            try:
                tray.update_menu()
            except Exception:
                log.debug("Tray menu refresh failed", exc_info=True)

    def _tray_open(self, icon=None, item=None):
        self.call_in_ui(self._show_window)

    def _open_tray_menu(self, at=None):
        if self._tray_menu is None:
            self._tray_menu = TrayMenu(self.root)
        now = self._now or describe_now_playing(self.monitoring, self.monitor.info,
                                                self.player.track_info(), self.config)
        items = [("Open Settings", self._show_window)]
        if self._update_info is not None:
            items.append((f"Download v{self._update_info.version}", self._open_update))
        items.append(("Stop Monitoring" if self.monitoring else "Start Monitoring",
                      self._toggle_monitoring))
        items += [None, ("Quit", self._quit)]
        self._tray_menu.show(now.menu_head, now.menu_line, now.kind, items, at=at)

    def _tray_update(self, icon=None, item=None):
        self.call_in_ui(self._open_update)

    def _tray_toggle(self, icon=None, item=None):
        self.call_in_ui(self._toggle_monitoring)

    def _tray_quit(self, icon=None, item=None):
        self.call_in_ui(self._quit)

    # ── window ──

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_window_close(self):
        """The X button hides to tray. Nothing is saved (see _save_settings)."""
        if self._commit_inputs():
            self._revert_invalid_fields()   # invalid typed values never applied
        self.root.withdraw()

    def _quit(self):
        if self._quitting:
            return
        self._quitting = True
        log.info("Quit requested")
        self.monitoring = False
        self.root.withdraw()

        self.updater.stop()
        if self._tray_menu is not None:
            self._tray_menu.close()

        def shutdown():
            try:
                self.monitor.stop(wait=True)
                self.player.shutdown()
                self._stop_mirror_fix(wait=True)
            except Exception:
                log.exception("Error during shutdown")
            finally:
                if self.tray:
                    try:
                        self.tray.stop()
                    except Exception:
                        log.warning("Stopping tray icon failed", exc_info=True)
                self.call_in_ui(self._finish_quit)

        threading.Thread(target=shutdown, daemon=True, name="Shutdown").start()

    def _finish_quit(self):
        self._ui_alive = False
        try:
            self.root.destroy()
        except Exception:
            pass

    # ── UI construction ──

    def _build_ui(self):
        BG     = "#1e1e2e"
        CARD   = "#2a2a3e"
        FG     = "#cdd6f4"
        ACC    = "#89b4fa"
        BTN_BG = "#313244"
        DIM    = "#a6adc8"
        self._FG = FG
        self._ERR = "#f38ba8"

        def label(parent, text, size=10, bold=False, color=FG):
            return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                            font=("Segoe UI", size, "bold" if bold else "normal"))

        def entry(parent, textvariable, width=24):
            return tk.Entry(parent, textvariable=textvariable, width=width,
                            bg=BTN_BG, fg=FG, insertbackground=FG,
                            relief="flat", font=("Segoe UI", 10), bd=6)

        def button(parent, text, command, color=ACC):
            return tk.Button(parent, text=text, command=command,
                             bg=BTN_BG, fg=color, relief="flat",
                             font=("Segoe UI", 10, "bold"),
                             padx=8, pady=5, cursor="hand2",
                             activebackground="#45475a", activeforeground=color)

        label(self.root, "Silence Player", size=16, bold=True, color=ACC).pack(pady=(16, 2))
        label(self.root, "Plays ambient sound when your PC is silent", size=9, color=DIM).pack()

        tab_bar = tk.Frame(self.root, bg=BG)
        tab_bar.pack(pady=(10, 0))

        self.tab_main     = tk.Frame(self.root, bg=BG)
        self.tab_exclude  = tk.Frame(self.root, bg=BG)
        self.tab_advanced = tk.Frame(self.root, bg=BG)

        def switch_tab(tab):
            self.tab_main.pack_forget()
            self.tab_exclude.pack_forget()
            self.tab_advanced.pack_forget()
            self.tab_btn_main.config(bg=BTN_BG, fg=DIM)
            self.tab_btn_exclude.config(bg=BTN_BG, fg=DIM)
            self.tab_btn_advanced.config(bg=BTN_BG, fg=DIM)
            if tab == "main":
                self.tab_main.pack(fill="both", expand=True)
                self.tab_btn_main.config(bg=ACC, fg="#1e1e2e")
            elif tab == "exclude":
                self.tab_exclude.pack(fill="both", expand=True)
                self.tab_btn_exclude.config(bg=ACC, fg="#1e1e2e")
            else:
                self.tab_advanced.pack(fill="both", expand=True)
                self.tab_btn_advanced.config(bg=ACC, fg="#1e1e2e")

        self.tab_btn_main = tk.Button(tab_bar, text="Player",
                                      command=lambda: switch_tab("main"),
                                      relief="flat", font=("Segoe UI", 10, "bold"),
                                      padx=16, pady=5, cursor="hand2",
                                      bg=ACC, fg="#1e1e2e")
        self.tab_btn_main.pack(side="left", padx=3)

        self.tab_btn_exclude = tk.Button(tab_bar, text="Exclude Apps",
                                         command=lambda: switch_tab("exclude"),
                                         relief="flat", font=("Segoe UI", 10, "bold"),
                                         padx=16, pady=5, cursor="hand2",
                                         bg=BTN_BG, fg=DIM)
        self.tab_btn_exclude.pack(side="left", padx=3)

        self.tab_btn_advanced = tk.Button(tab_bar, text="Advanced",
                                          command=lambda: switch_tab("advanced"),
                                          relief="flat", font=("Segoe UI", 10, "bold"),
                                          padx=16, pady=5, cursor="hand2",
                                          bg=BTN_BG, fg=DIM)
        self.tab_btn_advanced.pack(side="left", padx=3)

        self._build_main_tab(self.tab_main, BG, CARD, FG, ACC, BTN_BG, DIM, label, entry, button)
        self._build_exclude_tab(self.tab_exclude, BG, CARD, FG, ACC, BTN_BG, DIM)
        self._build_advanced_tab(self.tab_advanced, BG, CARD, FG, ACC, BTN_BG, DIM)

        self.tab_main.pack(fill="both", expand=True)

        status_frame = tk.Frame(self.root, bg="#181825")
        status_frame.pack(fill="x", side="bottom")

        # "A new version is available" (hidden until needed).
        self.update_row = tk.Frame(status_frame, bg="#313244", padx=10, pady=6)
        self.update_label = tk.Label(self.update_row, bg="#313244", fg=FG,
                                     font=("Segoe UI", 9), wraplength=490, justify="left")
        self.update_label.pack(anchor="w")
        update_buttons = tk.Frame(self.update_row, bg="#313244")
        update_buttons.pack(anchor="w", pady=(4, 0))
        self.update_btn = tk.Button(update_buttons, text="Download", command=self._open_update,
                                    bg="#a6e3a1", fg="#1e1e2e", relief="flat",
                                    font=("Segoe UI", 9, "bold"), padx=10, pady=3,
                                    cursor="hand2")
        self.update_btn.pack(side="left")
        for text, cmd in (("Skip this version", self._skip_update),
                          ("Later", self._update_later_clicked)):
            tk.Button(update_buttons, text=text, command=cmd,
                      bg=BTN_BG, fg=DIM, relief="flat", font=("Segoe UI", 9, "bold"),
                      padx=10, pady=3, cursor="hand2").pack(side="left", padx=(6, 0))

        # One-click fix for an app that seems to mirror our sound (hidden until needed).
        self.suggest_row = tk.Frame(status_frame, bg="#313244", padx=10, pady=6)
        self.suggest_label = tk.Label(self.suggest_row, bg="#313244", fg=FG,
                                      font=("Segoe UI", 9), wraplength=490, justify="left")
        self.suggest_label.pack(anchor="w")
        suggest_buttons = tk.Frame(self.suggest_row, bg="#313244")
        suggest_buttons.pack(anchor="w", pady=(4, 0))
        self.suggest_btn = tk.Button(suggest_buttons, text="Treat as mirroring app",
                                     command=self._accept_mirror_suggestion,
                                     bg=ACC, fg="#1e1e2e", relief="flat",
                                     font=("Segoe UI", 9, "bold"), padx=10, pady=3,
                                     cursor="hand2")
        self.suggest_btn.pack(side="left")
        tk.Button(suggest_buttons, text="Dismiss", command=self._dismiss_mirror_suggestion,
                  bg=BTN_BG, fg=DIM, relief="flat", font=("Segoe UI", 9, "bold"),
                  padx=10, pady=3, cursor="hand2").pack(side="left", padx=(6, 0))

        # Version number, tucked away bottom-left (handy for bug reports). The
        # status text keeps clear of it on both sides, however long it is.
        version_text = f"v{APP_VERSION}"
        try:
            version_w = tkfont.Font(root=self.root, family="Segoe UI", size=8).measure(version_text)
        except Exception:
            version_w = 7 * len(version_text)
        side_pad = max(52, version_w + 20)
        self.status_var = tk.StringVar(value=self._status)
        self.status_label = tk.Label(status_frame, textvariable=self.status_var,
                                     bg="#181825", fg=DIM, font=("Segoe UI", 9), pady=8,
                                     padx=side_pad,
                                     wraplength=max(240, self.WINDOW_W - 2 * side_pad - 16))
        self.status_label.pack(fill="x")
        tk.Label(status_frame, text=version_text, bg="#181825", fg="#585b70",
                 font=("Segoe UI", 8)).place(in_=self.status_label, relx=0, rely=0.5,
                                             x=10, anchor="w")

    def _build_main_tab(self, parent, BG, CARD, FG, ACC, BTN_BG, DIM, label, entry, button):
        self._build_now_playing(parent, CARD, FG, DIM)

        mode_frame = tk.Frame(parent, bg=BG)
        mode_frame.pack(pady=(8, 0))
        label(mode_frame, "Mode:", bold=True).pack(side="left", padx=(0, 8))

        self.mode_var = tk.StringVar(value=self.config.get("mode", "single"))

        def mode_btn(text, val):
            def on_click():
                self.mode_var.set(val)
                self.config["mode"] = val
                _refresh_mode()
            b = tk.Button(mode_frame, text=text, command=on_click,
                          relief="flat", font=("Segoe UI", 10, "bold"),
                          padx=12, pady=5, cursor="hand2")
            b.pack(side="left", padx=3)
            return b

        self.btn_single   = mode_btn("Single File", "single")
        self.btn_playlist = mode_btn("Playlist",    "playlist")

        def _refresh_mode():
            m = self.mode_var.get()
            if m == "single":
                self.btn_single.config(bg=ACC, fg="#1e1e2e")
                self.btn_playlist.config(bg=BTN_BG, fg=DIM)
                single_card.pack(fill="x", padx=24, pady=(8, 4))
                playlist_card.pack_forget()
            else:
                self.btn_playlist.config(bg=ACC, fg="#1e1e2e")
                self.btn_single.config(bg=BTN_BG, fg=DIM)
                playlist_card.pack(fill="x", padx=24, pady=(8, 4))
                single_card.pack_forget()

        single_card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        label(single_card, "Ambient Sound File (MP3, OPUS, M4A, FLAC, MP4)", bold=True).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.mp3_var = tk.StringVar()
        self.mp3_entry = self._path_field(single_card, self.mp3_var, self._browse_mp3, BTN_BG, FG)
        self.mp3_entry.grid(row=1, column=0, sticky="w")
        button(single_card, "Browse", self._browse_mp3).grid(row=1, column=1, padx=(6, 10))

        self.single_loop_var = tk.StringVar(value=self.config.get("single_loop_mode", "loop"))
        single_toggle_frame = tk.Frame(single_card, bg=CARD)
        single_toggle_frame.grid(row=1, column=2, sticky="w")

        def make_single_toggle(text, val):
            def on_click():
                self.single_loop_var.set(val)
                self.config["single_loop_mode"] = val
                _refresh_single_toggle()
            b = tk.Button(single_toggle_frame, text=text, command=on_click,
                          relief="flat", font=("Segoe UI", 9, "bold"),
                          padx=8, pady=4, cursor="hand2")
            b.pack(side="left", padx=2)
            return b

        self.s_btn_loop = make_single_toggle("Loop", "loop")
        self.s_btn_stop = make_single_toggle("Stop", "stop")

        def _refresh_single_toggle():
            v = self.single_loop_var.get()
            self.s_btn_loop.config(bg=ACC if v == "loop" else BTN_BG,
                                   fg="#1e1e2e" if v == "loop" else DIM)
            self.s_btn_stop.config(bg="#f38ba8" if v == "stop" else BTN_BG,
                                   fg="#1e1e2e" if v == "stop" else DIM)

        _refresh_single_toggle()

        playlist_card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        label(playlist_card, "Ambient Sound Playlist (Folder)", bold=True).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.playlist_var = tk.StringVar()
        self.playlist_entry = self._path_field(playlist_card, self.playlist_var,
                                               self._browse_playlist, BTN_BG, FG)
        self.playlist_entry.grid(row=1, column=0, sticky="w")
        button(playlist_card, "Browse", self._browse_playlist).grid(row=1, column=1, padx=(6, 10))

        self.playlist_loop_var = tk.StringVar(value=self.config.get("playlist_loop_mode", "loop_playlist"))
        playlist_toggle_frame = tk.Frame(playlist_card, bg=CARD)
        playlist_toggle_frame.grid(row=1, column=2, sticky="w")

        def make_playlist_toggle(text, val):
            def on_click():
                self.playlist_loop_var.set(val)
                self.config["playlist_loop_mode"] = val
                _refresh_playlist_toggle()
            b = tk.Button(playlist_toggle_frame, text=text, command=on_click,
                          relief="flat", font=("Segoe UI", 9, "bold"),
                          padx=8, pady=4, cursor="hand2")
            b.pack(side="left", padx=2)
            return b

        self.p_btn_loop_song = make_playlist_toggle("Loop Song",     "loop_song")
        self.p_btn_stop      = make_playlist_toggle("Stop",          "stop")
        self.p_btn_loop_pl   = make_playlist_toggle("Loop Playlist", "loop_playlist")

        def _refresh_playlist_toggle():
            v = self.playlist_loop_var.get()
            self.p_btn_loop_song.config(bg=ACC if v == "loop_song" else BTN_BG,
                                        fg="#1e1e2e" if v == "loop_song" else DIM)
            self.p_btn_stop.config(bg="#f38ba8" if v == "stop" else BTN_BG,
                                   fg="#1e1e2e" if v == "stop" else DIM)
            self.p_btn_loop_pl.config(bg="#a6e3a1" if v == "loop_playlist" else BTN_BG,
                                      fg="#1e1e2e" if v == "loop_playlist" else DIM)

        _refresh_playlist_toggle()

        shared_card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        shared_card.pack(fill="x", padx=24, pady=4)

        self.silence_var = tk.StringVar(value=str(self.config["silence_seconds"]))
        self.vol_var     = tk.StringVar(value=str(self.config["max_volume"]))

        label(shared_card, "Silence Timeout (seconds)", bold=True).grid(row=0, column=0, sticky="w")
        label(shared_card, "Max Volume (0-100)",         bold=True).grid(row=0, column=1, sticky="w", padx=(20, 0))
        self.silence_entry = entry(shared_card, self.silence_var, width=12)
        self.silence_entry.grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.vol_entry = entry(shared_card, self.vol_var, width=12)
        self.vol_entry.grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(2, 8))

        # Typed values are committed when the field loses focus or Enter is pressed
        # (and when the window is closed), so "settings save automatically" holds.
        for widget in (self.silence_entry, self.vol_entry):
            widget.bind("<FocusOut>", lambda e: self._commit_inputs_quietly())
            widget.bind("<Return>",   lambda e: self._commit_inputs_quietly())

        fade_row = tk.Frame(shared_card, bg=CARD)
        fade_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        label(fade_row, "Fade-in / Fade-out", bold=True, color=FG).pack(side="left", padx=(0, 12))

        self.fade_enabled_var = tk.BooleanVar(value=self.config.get("fade_enabled", True))

        def toggle_fade():
            v = self.fade_enabled_var.get()
            self.config["fade_enabled"] = v
            fade_btn.config(
                bg="#a6e3a1" if v else BTN_BG,
                fg="#1e1e2e" if v else DIM,
                text="Enabled" if v else "Disabled")

        fade_btn = tk.Button(fade_row,
                             command=lambda: [
                                 self.fade_enabled_var.set(not self.fade_enabled_var.get()),
                                 toggle_fade()],
                             relief="flat", font=("Segoe UI", 9, "bold"),
                             padx=12, pady=4, cursor="hand2")
        fade_btn.pack(side="left")
        toggle_fade()

        startup_row = tk.Frame(shared_card, bg=CARD)
        startup_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        label(startup_row, "Start with Windows", bold=True, color=FG).pack(side="left", padx=(0, 12))

        self.startup_var = tk.BooleanVar(value=self._startup.is_enabled())
        self.startup_btn = tk.Button(startup_row, command=self._toggle_startup_choice,
                                     relief="flat", font=("Segoe UI", 9, "bold"),
                                     padx=12, pady=4, cursor="hand2")
        self.startup_btn.pack(side="left")
        self.startup_hint = label(shared_card, "", size=8, color=DIM)
        self.startup_hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._refresh_startup_ui()

        duck_card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        duck_card.pack(fill="x", padx=24, pady=4)

        duck_top = tk.Frame(duck_card, bg=CARD)
        duck_top.pack(fill="x")
        label(duck_top, "When External Audio Detected", bold=True).pack(side="left")
        self.duck_label = tk.Label(duck_top, bg=CARD, fg=ACC, font=("Segoe UI", 10, "bold"))
        self.duck_label.pack(side="right")

        label(duck_card, "0% = Stop ambient   |   1-99% = Duck volume   |   100% = Keep playing",
              size=8, color=DIM).pack(anchor="w", pady=(2, 6))

        self.duck_var = tk.IntVar(value=int(self.config.get("duck_percent", 0)))

        def on_duck_change(val):
            v = int(float(val))
            self.config["duck_percent"] = v
            if v == 0:
                self.duck_label.config(text="Stop (0%)")
            elif v == 100:
                self.duck_label.config(text="Keep Playing (100%)")
            else:
                self.duck_label.config(text=f"Duck to {v}%")

        tk.Scale(duck_card, from_=0, to=100, orient="horizontal",
                 variable=self.duck_var, command=on_duck_change,
                 bg=CARD, fg=FG, troughcolor=BTN_BG,
                 highlightthickness=0, sliderrelief="flat",
                 length=460, showvalue=False).pack(fill="x")
        on_duck_change(self.duck_var.get())

        btn_frame = tk.Frame(parent, bg=BG)
        btn_frame.pack(pady=6)
        self.start_btn = button(btn_frame, "Start Monitoring", self._toggle_monitoring, color="#a6e3a1")
        self.start_btn.pack(side="left", padx=6)
        button(btn_frame, "Save Settings", self._save_settings, color="#f9e2af").pack(side="left", padx=6)

        _refresh_mode()
        self._refresh_path_fields()

    # ── Now playing ──

    NOW_COLORS = {             # badge background, badge text, title colour
        "playing": ("#a6e3a1", "#1e1e2e", "#cdd6f4"),
        "ducked":  ("#f9e2af", "#1e1e2e", "#cdd6f4"),
        "waiting": ("#313244", "#a6adc8", "#7f849c"),
        "off":     ("#313244", "#6c7086", "#7f849c"),
    }

    def _build_now_playing(self, parent, CARD, FG, DIM):
        card = tk.Frame(parent, bg=CARD, padx=8, pady=10)
        card.pack(fill="x", padx=24, pady=(10, 0))
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=8)
        tk.Label(head, text="Now playing", bg=CARD, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.np_badge = tk.Label(head, text="Off", font=("Segoe UI", 8, "bold"), padx=7, pady=1)
        self.np_badge.pack(side="right")
        self.np_title_var = tk.StringVar(value="")
        self.np_title = MarqueeLabel(card, self.np_title_var, width_chars=48, bg=CARD, fg=FG,
                                     font=("Segoe UI", 11))
        self.np_title.pack(anchor="w", pady=(4, 0))
        self.np_detail = tk.Label(card, text="", bg=CARD, fg=DIM, font=("Segoe UI", 9),
                                  anchor="w")
        self.np_detail.pack(fill="x", padx=8)
        self.np_bar = tk.Canvas(card, height=4, bg="#313244", highlightthickness=0, bd=0)
        self.np_bar.pack(fill="x", padx=8, pady=(8, 0))
        self._np_fill = self.np_bar.create_rectangle(0, 0, 0, 4, width=0, fill="#a6e3a1")
        self.np_bar.bind("<Configure>", lambda e: self._draw_np_bar())

    def _draw_np_bar(self):
        now = self._now
        frac = now.progress if now is not None and now.progress is not None else 0.0
        color = self.NOW_COLORS.get(now.kind if now else "off")[0]
        width = self.np_bar.winfo_width()
        self.np_bar.coords(self._np_fill, 0, 0, round(width * frac), 4)
        self.np_bar.itemconfig(self._np_fill, fill=color)

    def _refresh_now_playing(self):
        if not self._ui_alive:
            return
        try:
            now = describe_now_playing(self.monitoring, self.monitor.info,
                                       self.player.track_info(), self.config)
            self._show_now_playing(now)
        except Exception:
            log_throttled("now_playing", logging.WARNING, "Now playing refresh failed",
                          exc_info=True, interval=300)
        self.root.after(self.NOW_PLAYING_MS, self._refresh_now_playing)

    NOW_PLAYING_MS = 500

    def _show_now_playing(self, now):
        old, self._now = self._now, now
        if now == old:
            return
        badge_bg, badge_fg, title_fg = self.NOW_COLORS[now.kind]
        self.np_badge.config(text=now.badge, bg=badge_bg, fg=badge_fg)
        if self.np_title_var.get() != now.title:     # writing restarts the scroll
            self.np_title_var.set(now.title)
        self.np_title.set_fg(title_fg)
        self.np_detail.config(text=now.detail)
        self._draw_np_bar()
        if self._tray_menu is not None and self._tray_menu.is_open:
            self._tray_menu.update_header(now.menu_head, now.menu_line, now.kind)
        tray = self.tray
        if tray is not None:
            if now.tooltip != self._tray_tip:
                self._tray_tip = now.tooltip
                try:
                    tray.title = now.tooltip
                except Exception:
                    log.debug("Tray tooltip update failed", exc_info=True)
            if old is None or now.menu != old.menu:
                self._refresh_tray_menu()

    # ── Start with Windows (applied when Save Settings is clicked) ──

    def _toggle_startup_choice(self):
        if not self._startup.available:
            return
        self.startup_var.set(not self.startup_var.get())
        self._refresh_startup_ui()

    def _refresh_startup_ui(self):
        status = self._startup.status()
        on = bool(self.startup_var.get())
        if status == "unavailable":
            self.startup_btn.config(text="Windows only", state="disabled", bg="#313244",
                                    fg="#585b70", cursor="arrow")
            self.startup_hint.config(text="")
            return
        self.startup_btn.config(state="normal", cursor="hand2",
                                bg="#a6e3a1" if on else "#313244",
                                fg="#1e1e2e" if on else "#6c7086",
                                text="Enabled" if on else "Disabled")
        if status == "blocked" and not on:
            hint = "Turned off in Windows (Settings > Apps > Startup). Enable it here to undo that."
        else:
            hint = "Starts in the tray and begins monitoring. Click Save Settings to apply."
        self.startup_hint.config(text=hint)

    def _apply_startup_choice(self):
        """Write the Start with Windows choice. Returns an error message or None."""
        if not self._startup.available:
            return None
        want = bool(self.startup_var.get())
        if want == self._startup.is_enabled():
            return None
        try:
            self._startup.set_enabled(want)
        except Exception:
            log.exception("Could not change Start with Windows")
            self.startup_var.set(self._startup.is_enabled())
            self._refresh_startup_ui()
            return "Settings saved, but Start with Windows could not be changed. See the log file."
        self._refresh_startup_ui()
        return None

    def _path_field(self, parent, var, on_click, bg, fg):
        """File/folder name (scrolls if too long); click to choose, hover for the full path."""
        field = MarqueeLabel(parent, var, width_chars=22, bg=bg, fg=fg, on_click=on_click)
        field.bind("<Enter>", lambda e: self._show_path_tip(field), add="+")
        field.bind("<Leave>", lambda e: self._hide_path_tip(), add="+")
        return field

    def _refresh_path_fields(self):
        file_path = self.config.get("mp3_path", "")
        folder = self.config.get("playlist_folder", "")
        self.mp3_var.set(display_name(file_path) or "Click to choose a file…")
        self.playlist_var.set(display_name(folder) or "Click to choose a folder…")
        self._path_tips = {self.mp3_entry: file_path, self.playlist_entry: folder}

    def _show_path_tip(self, field):
        self._hide_path_tip()
        full = getattr(self, "_path_tips", {}).get(field)
        if not full:
            return
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{field.winfo_rootx()}+{field.winfo_rooty() + field.winfo_height() + 4}")
        tk.Label(tip, text=os.path.normpath(full), bg="#313244", fg="#cdd6f4",
                 font=("Segoe UI", 9), padx=8, pady=4, relief="solid", bd=1).pack()
        self._path_tip = tip

    def _hide_path_tip(self):
        tip = getattr(self, "_path_tip", None)
        if tip is not None:
            try:
                tip.destroy()
            except Exception:
                pass
            self._path_tip = None

    def _build_exclude_tab(self, parent, BG, CARD, FG, ACC, BTN_BG, DIM):
        card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        card.pack(fill="both", padx=24, pady=12, expand=True)

        tk.Label(card, text="Exclude Apps", bg=CARD, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 2))
        tk.Label(card, text="Sound from these apps will be ignored by Silence Player.",
                 bg=CARD, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))

        list_frame = tk.Frame(card, bg=CARD)
        list_frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")

        self.exclude_listbox = tk.Listbox(
            list_frame, yscrollcommand=scrollbar.set,
            bg=BTN_BG, fg=FG,
            selectbackground=ACC, selectforeground="#1e1e2e",
            relief="flat", font=("Segoe UI", 10),
            height=8, borderwidth=0)
        self.exclude_listbox.pack(fill="both", expand=True)
        scrollbar.config(command=self.exclude_listbox.yview)

        for app_name in self.config.get("excluded_apps", []):
            self.exclude_listbox.insert(tk.END, app_name)

        add_frame = tk.Frame(card, bg=CARD)
        add_frame.pack(fill="x", pady=(8, 0))

        self.exclude_entry_var = tk.StringVar()
        add_entry = tk.Entry(add_frame, textvariable=self.exclude_entry_var,
                             bg=BTN_BG, fg=FG, insertbackground=FG,
                             relief="flat", font=("Segoe UI", 10), bd=6, width=22)
        add_entry.pack(side="left", fill="x", expand=True)

        tk.Label(add_frame, text="e.g. discord.exe", bg=CARD, fg=DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))

        def add_app():
            name = normalize_app_name(self.exclude_entry_var.get())
            if not name:
                return
            if name in list(self.exclude_listbox.get(0, tk.END)):
                return
            self.exclude_listbox.insert(tk.END, name)
            self.exclude_entry_var.set("")
            self._sync_excluded_apps()

        def remove_app():
            selected = self.exclude_listbox.curselection()
            if selected:
                self.exclude_listbox.delete(selected[0])
                self._sync_excluded_apps()

        btn_row = tk.Frame(card, bg=CARD)
        btn_row.pack(fill="x", pady=(6, 0))

        tk.Button(btn_row, text="Add", command=add_app,
                  bg=ACC, fg="#1e1e2e", relief="flat",
                  font=("Segoe UI", 10, "bold"),
                  padx=16, pady=5, cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Remove Selected", command=remove_app,
                  bg=BTN_BG, fg="#f38ba8", relief="flat",
                  font=("Segoe UI", 10, "bold"),
                  padx=16, pady=5, cursor="hand2").pack(side="left")

        add_entry.bind("<Return>", lambda e: add_app())

    def _build_advanced_tab(self, parent, BG, CARD, FG, ACC, BTN_BG, DIM):
        self._DIM, self._GOOD = DIM, "#a6e3a1"
        tk.Label(parent, text="Advanced Settings", bg=BG, fg=ACC,
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 2))
        tk.Label(parent, text="Fixes for apps that confuse silence detection.",
                 bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(pady=(0, 8))

        card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        card.pack(fill="x", padx=24, pady=4)

        title_row = tk.Frame(card, bg=CARD)
        title_row.pack(fill="x")
        tk.Label(title_row, text="Mirroring Fix",
                 bg=CARD, fg=FG, font=("Segoe UI", 10, "bold")).pack(side="left")

        self.mirror_fix_var = tk.BooleanVar(value=self.config.get("mirror_fix_enabled", True))
        self.mirror_status = tk.Label(card, bg=CARD, font=("Segoe UI", 9, "bold"))

        def toggle_mirror_fix():
            self.mirror_fix_var.set(not self.mirror_fix_var.get())
            self.config["mirror_fix_enabled"] = bool(self.mirror_fix_var.get())
            self._apply_mirror_state()
            self._refresh_mirror_ui()

        self.mirror_btn = tk.Button(title_row, command=toggle_mirror_fix,
                                    relief="flat", font=("Segoe UI", 9, "bold"),
                                    padx=12, pady=4, cursor="hand2")
        self.mirror_btn.pack(side="right")

        tk.Label(card,
                 text="Some apps listen to your PC's sound and show a fake level in\n"
                      "the Windows mixer (Discord while streaming, SignalRGB,\n"
                      "Wallpaper Engine). For the apps below, Silence Player checks\n"
                      "whether they really make sound, and ignores them when\n"
                      "they're only reacting to your ambient sound.",
                 bg=CARD, fg=DIM, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", pady=(6, 6))

        list_frame = tk.Frame(card, bg=CARD)
        list_frame.pack(fill="x")
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.mirror_listbox = tk.Listbox(
            list_frame, yscrollcommand=scrollbar.set,
            bg=BTN_BG, fg=FG, selectbackground=ACC, selectforeground="#1e1e2e",
            relief="flat", font=("Segoe UI", 10), height=5, borderwidth=0,
            activestyle="none")
        self.mirror_listbox.pack(fill="x", expand=True)
        scrollbar.config(command=self.mirror_listbox.yview)

        def selected_name():
            sel = self.mirror_listbox.curselection()
            if not sel:
                return None
            apps = _coerce_mirror_apps(self.config.get("mirror_apps", []))
            return apps[sel[0]]["name"] if sel[0] < len(apps) else None

        def toggle_selected(event=None):
            name = selected_name()
            if name is None:
                return
            apps = _coerce_mirror_apps(self.config.get("mirror_apps", []))
            current = next(a["enabled"] for a in apps if a["name"] == name)
            self._set_mirror_app(name, not current)
            self._apply_mirror_state()
            self._refresh_mirror_ui(select=name)

        def remove_selected():
            name = selected_name()
            if name is None:
                return
            self.config["mirror_apps"] = [a for a in _coerce_mirror_apps(
                self.config.get("mirror_apps", [])) if a["name"] != name]
            self._apply_mirror_state()
            self._refresh_mirror_ui()

        def add_app(event=None):
            name = normalize_app_name(self.mirror_entry_var.get())
            if not name:
                return
            self._set_mirror_app(name, True)
            self.mirror_entry_var.set("")
            self._apply_mirror_state()
            self._refresh_mirror_ui(select=name)

        self.mirror_listbox.bind("<Double-Button-1>", toggle_selected)

        add_row = tk.Frame(card, bg=CARD)
        add_row.pack(fill="x", pady=(6, 0))
        self.mirror_entry_var = tk.StringVar()
        add_entry = tk.Entry(add_row, textvariable=self.mirror_entry_var,
                             bg=BTN_BG, fg=FG, insertbackground=FG,
                             relief="flat", font=("Segoe UI", 10), bd=6, width=18)
        add_entry.pack(side="left")
        add_entry.bind("<Return>", add_app)
        for text, cmd, color in (("Add", add_app, ACC),
                                 ("On / Off", toggle_selected, FG),
                                 ("Remove", remove_selected, "#f38ba8")):
            tk.Button(add_row, text=text, command=cmd, bg=BTN_BG, fg=color,
                      relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                      cursor="hand2").pack(side="left", padx=(6, 0))

        self.mirror_status.pack(anchor="w", pady=(8, 0))

        self._refresh_mirror_ui()
        self._apply_mirror_state()

        # Update check
        update_card = tk.Frame(parent, bg=CARD, padx=16, pady=10)
        update_card.pack(fill="x", padx=24, pady=4)
        top = tk.Frame(update_card, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text="Check for updates", bg=CARD, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")

        def toggle_updates():
            on = not (self.config.get("check_updates", True) is not False)
            self.config["check_updates"] = on
            if on and self.updater.last_checked is None:
                self.updater.check_now()
            self._refresh_update_status()

        self.update_toggle = tk.Button(top, command=toggle_updates, relief="flat",
                                       font=("Segoe UI", 9, "bold"), padx=12, pady=4,
                                       cursor="hand2")
        self.update_toggle.pack(side="right")
        tk.Button(top, text="Check now", command=self._check_updates_now, bg=BTN_BG, fg=ACC,
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                  cursor="hand2").pack(side="right", padx=(0, 6))
        self.update_status = tk.Label(update_card, bg=CARD, fg=DIM, font=("Segoe UI", 9),
                                      anchor="w")
        self.update_status.pack(anchor="w", fill="x", pady=(6, 0))
        self._refresh_update_status()

    def _refresh_mirror_ui(self, select=None):
        on = bool(self.mirror_fix_var.get())
        self.mirror_btn.config(bg=self._GOOD if on else "#313244",
                               fg="#1e1e2e" if on else self._DIM,
                               text="Enabled" if on else "Disabled")
        apps = _coerce_mirror_apps(self.config.get("mirror_apps", []))
        self.mirror_listbox.delete(0, tk.END)
        for i, a in enumerate(apps):
            self.mirror_listbox.insert(tk.END, f"{'●' if a['enabled'] else '○'}  {a['name']}"
                                       + ("" if a["enabled"] else "   (off)"))
            if a["name"] == select:
                self.mirror_listbox.selection_set(i)
        count = sum(1 for a in apps if a["enabled"])
        if on and PROCTAP_IMPORT_ERROR is not None:
            self.mirror_status.config(
                text="● Unavailable — ProcTap could not be loaded (see log)", fg="#f38ba8")
        elif on:
            self.mirror_status.config(
                text=f"● Active — checking {count} app{'s' if count != 1 else ''}",
                fg=self._GOOD)
        else:
            self.mirror_status.config(text="● Off", fg=self._DIM)

    def _sync_excluded_apps(self):
        self.config["excluded_apps"] = list(self.exclude_listbox.get(0, tk.END))

    def _browse_mp3(self):
        path = filedialog.askopenfilename(
            title="Select ambient audio file",
            filetypes=[("Audio files", "*.mp3 *.opus *.m4a *.flac *.mp4"),
                       ("All files", "*.*")])
        if path:
            self.config["mp3_path"] = path
            self._refresh_path_fields()

    def _browse_playlist(self):
        folder = filedialog.askdirectory(title="Select folder with audio files")
        if folder:
            self.config["playlist_folder"] = folder
            self._refresh_path_fields()

    def _save_settings(self):
        errors = self._commit_inputs()
        if errors:
            messagebox.showerror("Invalid settings", "\n\n".join(errors), parent=self.root)
            return
        startup_problem = self._apply_startup_choice()
        if not self._save_now():
            self.set_status("Could not save settings — see the log file for details.")
        else:
            self.set_status(startup_problem or "Settings saved!")

    # ── typed inputs ──

    def _commit_inputs(self):
        """Copy UI values into self.config. Valid values are applied; invalid
        ones are left out and returned as user-facing messages."""
        errors = []
        try:
            self.config["silence_seconds"] = parse_int_field(
                self.silence_var.get(), "Silence Timeout", SILENCE_MIN, SILENCE_MAX, "seconds")
            self.silence_entry.config(fg=self._FG)
        except ValidationError as e:
            errors.append(str(e))
            self.silence_entry.config(fg=self._ERR)
        try:
            self.config["max_volume"] = parse_int_field(
                self.vol_var.get(), "Max Volume", VOLUME_MIN, VOLUME_MAX)
            self.vol_entry.config(fg=self._FG)
        except ValidationError as e:
            errors.append(str(e))
            self.vol_entry.config(fg=self._ERR)

        # File/folder paths are set by Browse (the fields only show names).

        self.config["fade_enabled"]       = bool(self.fade_enabled_var.get())
        self.config["mode"]               = self.mode_var.get()
        self.config["single_loop_mode"]   = self.single_loop_var.get()
        self.config["playlist_loop_mode"] = self.playlist_loop_var.get()
        self.config["duck_percent"]       = int(self.duck_var.get())
        return errors

    def _commit_inputs_quietly(self):
        errors = self._commit_inputs()
        if errors:
            self.set_status(errors[0])

    def _revert_invalid_fields(self):
        self.silence_var.set(str(self.config["silence_seconds"]))
        self.vol_var.set(str(self.config["max_volume"]))
        self.silence_entry.config(fg=self._FG)
        self.vol_entry.config(fg=self._FG)

    # ── monitoring ──

    def _set_monitoring_ui(self, on):
        if on:
            self.start_btn.config(text="Stop Monitoring", fg="#f38ba8")
        else:
            self.start_btn.config(text="Start Monitoring", fg="#a6e3a1")
        self._refresh_tray_menu()

    def _start_monitoring(self):
        self.monitoring = True
        self.monitor.start()
        self._set_monitoring_ui(True)

    def _toggle_monitoring(self):
        if self.monitoring:
            self.monitoring = False
            self.monitor.stop()
            self.player.stop()          # fades out on the playback thread
            self.set_status("Monitoring stopped.")
            self._set_monitoring_ui(False)
            return

        errors = self._commit_inputs()
        problem = validate_ambient_source(self.config)
        if problem:
            errors.append(problem)
        if errors:
            self._show_window()
            messagebox.showerror("Can't start monitoring", "\n\n".join(errors), parent=self.root)
            return
        self._start_monitoring()
        self.set_status("Monitoring... waiting for silence.")

    def on_monitor_auto_stopped(self, reason):
        """Called from the monitor thread when it stops itself."""
        self.call_in_ui(self._handle_auto_stop, reason)

    def _handle_auto_stop(self, reason):
        if self.monitor.running:
            return   # the user already started monitoring again
        self.monitoring = False
        self._set_monitoring_ui(False)
        self.set_status(reason)

    # ── called by the monitor thread ──

    def play_ambient(self):
        self.set_status("Silence reached — playing ambient sound...")
        return self.player.play(self.config)

    def duck_ambient(self, target_vol):
        self.player.duck(target_vol)

    def unduck_ambient(self, target_vol):
        self.player.unduck(target_vol)

    def stop_ambient(self):
        self.player.stop()


def main():
    setup_logging()
    install_exception_hooks()
    log.info("Silence Player %s starting (python %s, frozen=%s)",
             APP_VERSION, sys.version.split()[0], hasattr(sys, "_MEIPASS"))
    ensure_mta()
    if PROCTAP_IMPORT_ERROR is not None:
        log.warning("ProcTap unavailable; Mirroring Fix disabled: %s", PROCTAP_IMPORT_ERROR)

    instance = SingleInstance()
    if not instance.acquire():
        log.info("Another instance is already running; asked it to open Settings")
        return 0
    try:
        root = tk.Tk()
        try:
            App(root, instance)
        except Exception as e:
            log.critical("Startup failed", exc_info=True)
            root.withdraw()
            messagebox.showerror(
                "Silence Player could not start",
                f"{e}\n\nDetails were written to:\n{os.path.join(log_dir(), 'silenceplayer.log')}")
            root.destroy()
            return 1
        root.mainloop()
    finally:
        instance.release()
        log.info("Silence Player exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
