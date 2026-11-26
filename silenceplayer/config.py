"""Settings: defaults, checking and repairing values, loading and saving config.json.
"""
import copy
import json
import logging
import os
import tempfile
import threading
import time
from typing import NamedTuple

from .logs import log, log_throttled
from .paths import CONFIG_FILE

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
            os.makedirs(folder, exist_ok=True)
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
