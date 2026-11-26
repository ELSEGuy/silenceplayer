"""Mirroring Fix: tells apps that only mirror other sound (Discord while
streaming, SignalRGB, ...) apart from apps that really play something.
"""
import collections
import logging
import threading
import time

import numpy as np

from .logs import log, log_throttled
from .winaudio import get_app_pids

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
