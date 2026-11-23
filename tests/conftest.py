"""Test setup.

The app imports Windows/native packages at module level (pycaw, python-vlc
with the bundled libvlc.dll, pystray, ProcTap). Tests replace them with small
fakes so the logic (threading, resume, config, Discord classification) can be
tested on any OS without audio hardware.
"""
import enum
import os
import sys
import threading
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── fake python-vlc ──────────────────────────────────────────────────────────

class State(enum.IntEnum):
    NothingSpecial = 0
    Opening = 1
    Buffering = 2
    Playing = 3
    Paused = 4
    Stopped = 5
    Ended = 6
    Error = 7


class FakeMedia:
    def __init__(self, path):
        self.path = path

    def release(self):
        pass


class FakeMediaPlayer:
    """Plays instantly; a test ends the current track with finish()."""

    def __init__(self):
        self.lock = threading.Lock()
        self.media = None
        self.events = []          # ("load", path) / ("stop",) / ("seek", ms)
        self.loads = []
        self.seeks = []
        self.volumes = []
        self.volume = 100
        self.state = State.NothingSpecial
        self.position_ms = 0
        self.length_ms = 600_000
        self.fail_paths = set()

    # API used by the app
    def set_media(self, media):
        with self.lock:
            self.media = media
            self.loads.append(media.path)
            self.events.append(("load", media.path))
            self.position_ms = 0

    def play(self):
        with self.lock:
            if self.media is not None and self.media.path in self.fail_paths:
                self.state = State.Error
            else:
                self.state = State.Playing

    def stop(self):
        with self.lock:
            self.state = State.Stopped
            self.events.append(("stop",))

    def is_playing(self):
        with self.lock:
            return 1 if self.state == State.Playing else 0

    def get_state(self):
        with self.lock:
            return self.state

    def get_time(self):
        with self.lock:
            return self.position_ms

    def set_time(self, ms):
        with self.lock:
            self.position_ms = ms
            self.seeks.append(ms)
            self.events.append(("seek", ms))

    def get_length(self):
        return self.length_ms

    def audio_set_volume(self, v):
        with self.lock:
            self.volume = v
            self.volumes.append(v)
        return 0

    def audio_get_volume(self):
        return self.volume

    def release(self):
        pass

    # test helpers
    def finish(self):
        with self.lock:
            self.state = State.Ended

    def current_path(self):
        with self.lock:
            return self.media.path if self.media else None


class FakeInstance:
    def __init__(self, *args):
        self.players = []

    def media_player_new(self):
        p = FakeMediaPlayer()
        self.players.append(p)
        return p

    def media_new(self, path):
        return FakeMedia(path)

    def release(self):
        pass


vlc_mod = types.ModuleType("vlc")
vlc_mod.Instance = FakeInstance
vlc_mod.State = State
sys.modules["vlc"] = vlc_mod


# ── fake pycaw / pystray / proctap ───────────────────────────────────────────

class FakeAudioUtilities:
    sessions = []

    @classmethod
    def GetAllSessions(cls):
        return list(cls.sessions)


pycaw_pkg = types.ModuleType("pycaw")
pycaw_mod = types.ModuleType("pycaw.pycaw")
pycaw_mod.AudioUtilities = FakeAudioUtilities
pycaw_mod.IAudioMeterInformation = object()
pycaw_pkg.pycaw = pycaw_mod
sys.modules["pycaw"] = pycaw_pkg
sys.modules["pycaw.pycaw"] = pycaw_mod


class _Anything:
    SEPARATOR = None

    def __init__(self, *a, **k):
        pass

    def run(self):
        pass

    def stop(self):
        pass

    def update_menu(self):
        pass


pystray_mod = types.ModuleType("pystray")
pystray_mod.Menu = _Anything
pystray_mod.MenuItem = _Anything
pystray_mod.Icon = _Anything
sys.modules["pystray"] = pystray_mod


class _UnavailableCapture:
    def __init__(self, pid):
        raise RuntimeError("no ProcTap in tests")


proctap_mod = types.ModuleType("proctap")
proctap_mod.ProcessAudioCapture = _UnavailableCapture
sys.modules["proctap"] = proctap_mod

# tkinter isn't always installed on headless Linux; the App class isn't
# exercised by these tests, so a placeholder module is enough to import app.py.
try:
    import tkinter  # noqa: F401
except ImportError:
    tk_mod = types.ModuleType("tkinter")
    tk_mod.filedialog = types.ModuleType("tkinter.filedialog")
    tk_mod.messagebox = types.ModuleType("tkinter.messagebox")
    tk_mod.font = types.ModuleType("tkinter.font")
    tk_mod.Canvas = object
    sys.modules["tkinter"] = tk_mod
    sys.modules["tkinter.font"] = tk_mod.font
    sys.modules["tkinter.filedialog"] = tk_mod.filedialog
    sys.modules["tkinter.messagebox"] = tk_mod.messagebox


import app as app_module  # noqa: E402


# ── shared fixtures ──────────────────────────────────────────────────────────

class FakeApp:
    """The bits of App that the monitor and player call."""

    def __init__(self, config):
        self.config = config
        self.statuses = []
        self.auto_stops = []
        self.mirror_fix = None
        self.player = None
        self.suspects = []

    def set_status(self, msg):
        self.statuses.append(msg)

    def on_monitor_auto_stopped(self, reason):
        self.auto_stops.append(reason)

    def on_suspected_mirror(self, names):
        self.suspects.append(list(names))

    def play_ambient(self):
        return self.player.play(self.config)

    def duck_ambient(self, v):
        self.player.duck(v)

    def unduck_ambient(self, v):
        self.player.unduck(v)

    def stop_ambient(self):
        self.player.stop()


def wait_until(predicate, timeout=3.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def appmod():
    return app_module


@pytest.fixture
def fast_timing(monkeypatch):
    """Short fades and polls so threading tests run quickly."""
    monkeypatch.setattr(app_module, "FADE_SECONDS", 0.3)
    monkeypatch.setattr(app_module.AmbientPlayer, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(app_module.AmbientPlayer, "DUCK_STEP_SECONDS", 0.005)
    monkeypatch.setattr(app_module.AmbientPlayer, "AUDIO_START_TIMEOUT", 0.05)
    monkeypatch.setattr(app_module.AudioMonitor, "POLL_INTERVAL", 0.01)


@pytest.fixture
def make_config():
    def make(**overrides):
        cfg = dict(app_module.DEFAULT_CONFIG)
        cfg["excluded_apps"] = []
        cfg.update(overrides)
        return cfg
    return make


@pytest.fixture
def audio_files(tmp_path):
    """Creates empty files the fake VLC can 'play'."""
    def make(*names, folder="ambient"):
        d = tmp_path / folder
        d.mkdir(exist_ok=True)
        paths = []
        for n in names:
            p = d / n
            p.write_bytes(b"")
            paths.append(str(p))
        return str(d), paths
    return make


@pytest.fixture
def player_factory():
    created = []

    def make(config):
        fake_app = FakeApp(config)
        player = app_module.AmbientPlayer(fake_app)
        fake_app.player = player
        created.append(player)
        return fake_app, player, player.media_player

    yield make
    for p in created:
        try:
            p.stop(wait=True, timeout=3)
        except Exception:
            pass
