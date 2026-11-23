import threading
import time

from conftest import FakeApp, wait_until


class StubPlayer:
    def __init__(self):
        self.playing = False
        self.last_error = None
        self.plays = 0
        self.stops = 0

    def play(self, config):
        self.plays += 1
        self.playing = True
        return True

    def stop(self):
        self.stops += 1
        self.playing = False

    def duck(self, v):
        pass

    def unduck(self, v):
        pass


def make_monitor(appmod, make_config, source, clock=None, **cfg):
    app = FakeApp(make_config(**cfg))
    app.player = StubPlayer()
    kwargs = {"playing_apps_source": source}
    if clock is not None:
        kwargs["clock"] = clock
    return app, appmod.AudioMonitor(app, **kwargs)


# ── Priority 1: Start/Stop/Start race ────────────────────────────────────────

def test_start_stop_start_never_leaves_two_workers(appmod, make_config, fast_timing):
    callers = []
    lock = threading.Lock()

    def source(excluded):
        with lock:
            callers.append((time.monotonic(), threading.get_ident()))
        time.sleep(0.003)
        return {"spotify.exe"}          # never silent, so nothing plays

    app, mon = make_monitor(appmod, make_config, source)
    for _ in range(200):
        mon.start()
        mon.stop()
        mon.start()
    time.sleep(0.3)

    with lock:
        cutoff = time.monotonic() - 0.15
        recent = {ident for t, ident in callers if t >= cutoff}
    assert len(recent) == 1, f"{len(recent)} monitor workers active"
    alive = [t for t in threading.enumerate() if t.name == "AudioMonitor" and t.is_alive()]
    assert len(alive) == 1
    mon.stop(wait=True)
    assert not mon.running


def test_old_worker_exits_after_stop_then_immediate_start(appmod, make_config, fast_timing):
    """The exact v1.3-beta race: worker sleeping → Stop → Start → worker wakes."""
    ticks = []
    app, mon = make_monitor(appmod, make_config,
                            lambda ex: ticks.append(threading.get_ident()) or {"x.exe"})
    mon.start()
    assert wait_until(lambda: len(ticks) >= 2)
    old = mon.thread
    mon.stop()
    mon.start()                          # immediately, while `old` may be mid-sleep
    new = mon.thread
    assert old is not new
    assert wait_until(lambda: not old.is_alive(), timeout=1.0), "old worker survived"
    n = len(ticks)
    assert wait_until(lambda: len(ticks) >= n + 3)
    assert set(ticks[n:]) == {new.ident}
    mon.stop(wait=True)


def test_repeated_start_and_stop_are_safe(appmod, make_config, fast_timing):
    app, mon = make_monitor(appmod, make_config, lambda ex: set())
    assert mon.start() is True
    assert mon.start() is False         # already running
    mon.stop()
    mon.stop()
    mon.stop(wait=True)
    assert not mon.running
    assert mon.start() is True
    mon.stop(wait=True)
    assert not mon.thread.is_alive()


def test_stop_wait_terminates_worker(appmod, make_config, fast_timing):
    app, mon = make_monitor(appmod, make_config, lambda ex: {"x.exe"})
    mon.start()
    t = mon.thread
    mon.stop(wait=True, timeout=2)
    assert not t.is_alive()


def test_old_worker_self_stop_does_not_stop_new_worker(appmod, make_config, fast_timing):
    app, mon = make_monitor(appmod, make_config, lambda ex: {"x.exe"})
    mon.start()
    old_event = mon._stop_event
    mon.stop()
    mon.start()
    mon._stop_from_worker(old_event, "old worker finished")
    assert mon.running
    assert app.auto_stops == []
    mon.stop(wait=True)


# ── monitor behaviour ────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_plays_after_silence_and_stops_on_external_audio(appmod, make_config):
    clock = Clock()
    apps = set()
    app, mon = make_monitor(appmod, make_config, lambda ex: set(apps), clock=clock,
                            silence_seconds=5, duck_percent=0)
    state = appmod._MonitorState()
    ev = threading.Event()

    mon._tick(state, ev)
    clock.t += 4
    mon._tick(state, ev)
    assert app.player.plays == 0
    clock.t += 1.5
    mon._tick(state, ev)
    assert app.player.plays == 1 and state.ambient_triggered

    clock.t += 10                        # past the cooldown
    apps.add("spotify.exe")
    mon._tick(state, ev)
    assert app.player.stops == 1
    assert not state.ambient_triggered


def test_pycaw_failure_is_not_treated_as_silence(appmod, make_config, fast_timing):
    def broken(ex):
        raise OSError("COM failure")

    app, mon = make_monitor(appmod, make_config, broken, silence_seconds=1)
    mon.start()
    time.sleep(0.3)
    mon.stop(wait=True)
    assert app.player.plays == 0
    assert app.auto_stops == []


def test_failed_play_stops_monitoring_with_reason(appmod, make_config):
    clock = Clock()
    app, mon = make_monitor(appmod, make_config, lambda ex: set(), clock=clock,
                            silence_seconds=1)
    app.player.play = lambda cfg: False
    app.player.last_error = "The ambient sound file could not be found"
    mon.start()                          # so the worker counts as "current"
    ev = mon._stop_event
    state = appmod._MonitorState()
    mon._tick(state, ev)
    clock.t += 2
    mon._tick(state, ev)
    assert ev.is_set()
    assert app.auto_stops == ["The ambient sound file could not be found"]
    mon.stop(wait=True)


def test_bad_config_value_does_not_kill_worker(appmod, make_config, fast_timing):
    calls = []
    app, mon = make_monitor(appmod, make_config,
                            lambda ex: calls.append(1) or {"x.exe"})
    app.config["silence_seconds"] = "abc"
    mon.start()
    assert wait_until(lambda: len(calls) > 5)
    assert mon.running
    mon.stop(wait=True)


def test_get_playing_apps_ignores_own_process_by_pid(appmod, monkeypatch):
    import os

    class Proc:
        def __init__(self, pid, name):
            self.pid, self._name = pid, name

        def name(self):
            return self._name

    class Meter:
        def GetPeakValue(self):
            return 0.5

    class Ctl:
        def QueryInterface(self, iface):
            return Meter()

    class Session:
        def __init__(self, pid, name):
            self.Process, self._ctl = Proc(pid, name), Ctl()

    monkeypatch.setattr(appmod.AudioUtilities, "sessions", [
        Session(os.getpid(), "pythonw3.12.exe"),     # ourselves, unusual name
        Session(os.getpid() + 1, "spotify.exe"),
        Session(os.getpid() + 2, "discord.exe"),
    ], raising=False)
    assert appmod.get_playing_apps(["discord.exe"]) == {"spotify.exe"}


# ── Mirroring Fix in the monitor ─────────────────────────────────────────────

class FakeFix:
    def __init__(self, real):
        self.real = real

    def is_real_audio(self, name):
        return self.real.get(name, True)


def _start_ambient(appmod, mon, app, clock, apps):
    state = appmod._MonitorState()
    ev = threading.Event()
    mon._tick(state, ev)
    clock.t += 31
    mon._tick(state, ev)                 # starts ambient
    assert state.ambient_triggered
    return state, ev


def test_mirroring_app_is_ignored(appmod, make_config):
    clock = Clock()
    apps = set()
    app, mon = make_monitor(appmod, make_config, lambda ex: set(apps), clock=clock)
    app.mirror_fix = FakeFix({"signalrgb.exe": False})
    state, ev = _start_ambient(appmod, mon, app, clock, apps)
    clock.t += 5
    apps.add("signalrgb.exe")            # reacts to our ambient sound
    mon._tick(state, ev)
    assert app.player.stops == 0 and state.ambient_triggered


def test_unlisted_app_reacting_to_ambient_is_suggested(appmod, make_config):
    clock = Clock()
    apps = set()
    app, mon = make_monitor(appmod, make_config, lambda ex: set(apps), clock=clock)
    app.mirror_fix = FakeFix({})
    state, ev = _start_ambient(appmod, mon, app, clock, apps)
    clock.t += 5
    apps.add("audioviz.exe")
    mon._tick(state, ev)
    assert app.player.stops == 1
    assert app.suspects == [["audioviz.exe"]]


def test_audio_long_after_ambient_is_not_suggested(appmod, make_config):
    clock = Clock()
    apps = set()
    app, mon = make_monitor(appmod, make_config, lambda ex: set(apps), clock=clock)
    state, ev = _start_ambient(appmod, mon, app, clock, apps)
    clock.t += 120
    apps.add("spotify.exe")              # the user started music later: normal
    mon._tick(state, ev)
    assert app.player.stops == 1
    assert app.suspects == []
