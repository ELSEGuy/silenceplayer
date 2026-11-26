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


class FakeSession:
    """What list_sessions() returns: a pid and a control with a peak meter."""

    def __init__(self, pid, peak):
        self.ProcessId = pid
        self._ctl = self

    def QueryInterface(self, iface):
        return self

    def GetPeakValue(self):
        return self.peak


def fake_sessions(monkeypatch, appmod, rows):
    """rows: (pid, name, peak). Patches list_sessions and the name lookup."""
    sessions = []
    names = {}
    for pid, name, peak in rows:
        sess = FakeSession(pid, peak)
        sess.peak = peak
        sessions.append(sess)
        names[pid] = name
    monkeypatch.setattr(appmod, "list_sessions", lambda: list(sessions))
    monkeypatch.setattr(appmod, "process_names", appmod.ProcessNames(lookup=names.__getitem__))
    return sessions


def test_get_playing_apps_ignores_own_process_by_pid(appmod, monkeypatch):
    import os
    base = os.getpid()
    fake_sessions(monkeypatch, appmod, [
        (base, "pythonw3.12.exe", 0.5),      # ourselves, unusual name
        (base + 1, "spotify.exe", 0.5),
        (base + 2, "discord.exe", 0.5),
        (0, "system", 0.9),                  # Windows system sounds (pid 0) are ignored
    ])
    assert appmod.get_playing_apps(["discord.exe"]) == {"spotify.exe"}


def test_quiet_sessions_and_unknown_processes_are_ignored(appmod, monkeypatch):
    import os
    base = os.getpid()
    sessions = fake_sessions(monkeypatch, appmod, [
        (base + 1, "spotify.exe", 0.0005),   # below the 0.001 peak threshold
        (base + 2, "zen.exe", 0.2),
    ])
    sessions.append(FakeSession(base + 3, 0.5))   # process exited: name lookup fails
    sessions[-1].peak = 0.5
    assert appmod.get_playing_apps([]) == {"zen.exe"}


def test_get_app_pids_finds_every_session_of_an_app(appmod, monkeypatch):
    fake_sessions(monkeypatch, appmod, [
        (11, "discord.exe", 0.0), (12, "discord.exe", 0.3), (13, "zen.exe", 0.3), (0, "x", 0)])
    assert appmod.get_app_pids({"discord.exe"}) == {11: "discord.exe", 12: "discord.exe"}


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


# ── Mirroring Fix only applies when there is something to mirror ─────────────
# Bug: watching someone's Discord stream. The Mirroring Fix judged Discord
# "mirroring only" even though its stream audio was real, so the silence timer
# was never reset and ambient started over the stream.

class FlexFix:
    """Verdicts can change during a test, like real ProcTap readings."""

    def __init__(self, real=None):
        self.real = dict(real or {})
        self.asked = []

    def is_real_audio(self, name):
        self.asked.append(name)
        return self.real.get(name, True)


def _playing(appmod, names, excluded_audible=()):
    result = appmod.PlayingApps(names)
    result.excluded_audible = set(excluded_audible)
    return result


class AudiblePlayer(StubPlayer):
    """Like AmbientPlayer: still audible while fading out after stop()."""

    def __init__(self):
        super().__init__()
        self.audible = False

    def play(self, config):
        self.audible = True
        return super().play(config)


def test_stream_audio_resets_timer_even_if_taps_read_silent(appmod, make_config):
    clock = Clock()
    apps = {"discord.exe"}
    app, mon = make_monitor(appmod, make_config, lambda ex: _playing(appmod, apps),
                            clock=clock, silence_seconds=5)
    app.mirror_fix = FlexFix({"discord.exe": False})      # the tap wrongly says "silent"
    state, ev = appmod._MonitorState(), threading.Event()
    for _ in range(120):                                  # a minute of streaming
        mon._tick(state, ev)
        clock.t += 0.5
    assert app.player.plays == 0, "ambient started over the stream"
    assert state.silence_start is None
    assert app.mirror_fix.asked == [], "nothing to mirror: no need to ask the tap"


def test_discord_stream_scenario_from_the_bug_report(appmod, make_config):
    """Stream plays → stream ends → ambient starts → stream audio returns →
    ambient stops → the stream keeps playing: the timer must keep resetting."""
    clock = Clock()
    apps = set()
    app, mon = make_monitor(appmod, make_config, lambda ex: _playing(appmod, apps),
                            clock=clock, silence_seconds=5, duck_percent=0)
    app.player = AudiblePlayer()
    app.mirror_fix = FlexFix({"discord.exe": True})
    state, ev = appmod._MonitorState(), threading.Event()

    def run(seconds):
        for _ in range(int(seconds * 2)):
            mon._tick(state, ev)
            clock.t += 0.5

    run(1)
    run(6)                                                # silence → ambient starts
    assert app.player.plays == 1 and state.ambient_triggered
    run(5)                                                # past the start cooldown
    apps.add("discord.exe")                               # stream audio arrives
    run(1)
    assert app.player.stops == 1 and not state.ambient_triggered

    app.player.audible = False                            # fade-out is over
    app.mirror_fix.real["discord.exe"] = False            # taps now read "silent"
    run(60)                                               # stream keeps playing
    assert app.player.plays == 1, "ambient restarted while the stream was playing"
    assert state.silence_start is None


def test_mirror_app_still_ignored_while_an_excluded_app_plays(appmod, make_config):
    """SignalRGB mirroring an excluded app's sound must not block ambient."""
    clock = Clock()
    app, mon = make_monitor(
        appmod, make_config,
        lambda ex: _playing(appmod, {"signalrgb.exe"}, excluded_audible={"spotify.exe"}),
        clock=clock, silence_seconds=5)
    app.mirror_fix = FlexFix({"signalrgb.exe": False})
    state, ev = appmod._MonitorState(), threading.Event()
    mon._tick(state, ev)
    clock.t += 6
    mon._tick(state, ev)
    assert app.player.plays == 1


def test_mirror_filter_stays_on_while_ambient_fades_out(appmod, make_config):
    clock = Clock()
    apps = set()
    app, mon = make_monitor(appmod, make_config, lambda ex: _playing(appmod, apps),
                            clock=clock, silence_seconds=5, duck_percent=0)
    app.player = AudiblePlayer()
    app.mirror_fix = FlexFix({"signalrgb.exe": False})
    state, ev = appmod._MonitorState(), threading.Event()
    mon._tick(state, ev)
    clock.t += 6
    mon._tick(state, ev)                                  # ambient starts
    clock.t += 5
    apps.add("spotify.exe")
    mon._tick(state, ev)                                  # stops → fading out
    assert not state.ambient_triggered and app.player.audible

    apps.clear()
    apps.add("signalrgb.exe")                             # still mirroring the fade-out
    clock.t += 0.5
    mon._tick(state, ev)
    assert state.silence_start is not None                # ignored

    app.player.audible = False                            # fade-out over, tail passes
    clock.t += appmod.MIRROR_TAIL_SECONDS + 0.5
    mon._tick(state, ev)
    assert state.silence_start is None                    # now it counts as real


def test_get_playing_apps_reports_audible_excluded_apps(appmod, monkeypatch):
    import os
    base = os.getpid()
    fake_sessions(monkeypatch, appmod, [
        (base + 1, "spotify.exe", 0.5),      # excluded and audible
        (base + 2, "vlc.exe", 0.0),          # excluded, silent
        (base + 3, "discord.exe", 0.4),
    ])
    result = appmod.get_playing_apps(["spotify.exe", "vlc.exe"])
    assert result == {"discord.exe"}
    assert result.excluded_audible == {"spotify.exe"}
