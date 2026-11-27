"""Windows audio sessions: which apps are making sound, on every output device.

Also keeps COM (Windows' component system) set up for worker threads.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

import psutil
from pycaw.pycaw import IAudioMeterInformation

from .logs import log, log_throttled

OWN_PROCESSES = {"python.exe", "pythonw.exe", "py.exe", "python3.exe", "silenceplayer.exe"}

# Audio routing apps: other apps play into their virtual device and they play
# the result on your real speakers or headset. Their own session only carries
# other apps' sound (our ambient sound too), and those apps are already seen
# on the virtual device, so routers are never counted as playing.
AUDIO_ROUTERS = {
    "nvidia broadcast.exe", "nvidia rtx voice.exe",
    "voicemeeter.exe", "voicemeeter_x64.exe", "voicemeeterpro.exe", "voicemeeterpro_x64.exe",
    "voicemeeter8.exe", "voicemeeter8x64.exe",
    "steelseriessonar.exe", "wavelink.exe", "fxsound.exe", "krisp.exe", "boom3d.exe",
}


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


def ensure_mta() -> bool:
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
    from pycaw.constants import DEVICE_STATE, CLSID_MMDeviceEnumerator, EDataFlow
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


def list_sessions() -> list:
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

    def get(self, pid: int) -> str | None:
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

    def forget_missing(self, live_pids: set[int]) -> None:
        """Drop processes that no longer have an audio session (a new process
        may get the same pid later)."""
        with self._lock:
            for pid in [p for p in self._names if p not in live_pids]:
                del self._names[pid]


process_names = ProcessNames()


def get_playing_apps(excluded: list[str] | None = None) -> PlayingApps:
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
            if name is None or name in OWN_PROCESSES or name in AUDIO_ROUTERS:
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


def get_app_pids(names: set[str] | frozenset[str]) -> dict[int, str]:
    """{pid: process name} for every audio session owned by one of `names`.

    Raises on failure so the caller can keep its existing taps instead of
    treating a transient pycaw error as "the app went away".
    """
    found: dict[int, str] = {}
    for session in list_sessions():
        try:
            pid = session.ProcessId
            if not pid:
                continue
            name = process_names.get(pid)
            if name is not None and name in names:
                found[pid] = name
        except Exception:
            continue
    return found
