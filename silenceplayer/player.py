"""Ambient playback with VLC: fades, ducking, playlists and resume.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import NamedTuple

from .config import fade_seconds, runtime_settings
from .logs import log, log_throttled
from .paths import BUNDLE_DIR
from .sources import (
    SUPPORTED_LABEL,
    ResumeState,
    collect_playlist,
    plan_playlist_start,
    plan_single_start,
    validate_ambient_source,
)
from .winaudio import list_sessions

# Use the VLC that ships with Silence Player, never one installed on the PC.
# python-vlc reads these two settings when it is imported, so they come first.
os.environ["PYTHON_VLC_LIB_PATH"] = os.path.join(BUNDLE_DIR, "libvlc.dll")
os.environ["PYTHON_VLC_MODULE_PATH"] = os.path.join(BUNDLE_DIR, "plugins")

import vlc  # noqa: E402


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
    position: float | None = None
    length: float | None = None


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
