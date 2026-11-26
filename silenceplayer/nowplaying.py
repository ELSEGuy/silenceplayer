"""What the Now playing card, the tray tooltip and the tray menu show.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, NamedTuple

from .sources import display_name

if TYPE_CHECKING:          # only for the type hints
    from .monitor import MonitorInfo
    from .player import TrackInfo

TRAY_TIP_MAX = 127      # Windows cuts tray tooltips at 127 characters


TRAY_MENU_MAX = 60


class NowPlaying(NamedTuple):
    badge: str          # "Playing", "Ducked 50%", "Waiting", "Off"
    kind: str           # "playing", "ducked", "waiting", "off": picks the colours
    title: str          # file name
    detail: str         # time, countdown or reason
    progress: float | None  # 0..1, or None for no bar
    tooltip: str        # tray icon tooltip
    menu: str           # first (greyed) line of the plain Windows tray menu (fallback)
    menu_head: str = "" # Silence Player's own tray menu: small coloured line...
    menu_line: str = "" # ...and the line under it


def format_clock(seconds: float) -> str:
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


def describe_now_playing(monitoring: bool, info: MonitorInfo | None,
                         track: TrackInfo | None, config: dict) -> NowPlaying:
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

    def listening():
        return NowPlaying("Waiting", "waiting", name, "Listening…", None,
                          tip("Listening…"), "Waiting for silence",
                          "Waiting for silence", "Listening…")

    if not monitoring:
        return NowPlaying("Off", "off", name, "Monitoring is off", None,
                          tip("Monitoring is off"), "Monitoring is off",
                          "Monitoring is off", head(name))
    if info is None:                     # the monitor hasn't looked yet
        return listening()
    phase = info.phase

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
    return listening()
