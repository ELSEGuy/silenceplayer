"""The monitor: waits for silence, then starts, stops or ducks the ambient sound.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import NamedTuple

from .config import runtime_settings
from .logs import log, log_throttled
from .winaudio import get_playing_apps

# How long after the ambient sound starts a *new* "external" sound counts as
# suspicious (an app reacting to our own sound), on top of the start cooldown.
MIRROR_SUSPECT_SECONDS = 6.0


# Mirroring apps keep showing a level for a moment after our sound has ended.
MIRROR_TAIL_SECONDS = 1.5


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
    remaining: float | None = None  # seconds left of a countdown (waiting / unducking)
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

    def _start_ambient(self, s, settings, now, stop_event):
        app, player = self.app, self.app.player
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

    def _tick(self, s, stop_event):
        app = self.app
        settings = runtime_settings(app.config)
        raw_apps = self._get_playing_apps(settings.excluded)
        if stop_event.is_set():
            return
        player = app.player
        now = self._clock()
        paused = bool(getattr(player, "paused", False))
        if (s.ambient_triggered and not paused) or self._ambient_audible(player):
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
                    self._start_ambient(s, settings, now, stop_event)
                else:
                    remaining = settings.silence_secs - elapsed
                    self.info = MonitorInfo("waiting", remaining=remaining)
                    app.set_status(f"Silence detected... playing in {remaining:.0f}s")
