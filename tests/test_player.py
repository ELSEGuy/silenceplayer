import os
import time

from conftest import wait_until


def _wait_loaded(mp, n, player=None):
    def ready():
        if not (len(mp.loads) >= n and mp.is_playing()):
            return False
        if player is None:
            return True
        sess = player._session
        return sess is not None and sess.track_started.is_set()
    assert wait_until(ready), f"expected {n} loads, got {mp.loads}"


def _wait_finished(player):
    assert wait_until(lambda: player._session is None), "session did not finish"


# ── Priority 2: playlist resume ──────────────────────────────────────────────

def test_playlist_resume_restores_track_and_position(player_factory, make_config,
                                                     audio_files, fast_timing):
    folder, (rain, forest, fireplace) = audio_files(
        "01_rain.mp3", "02_forest.mp3", "03_fireplace.mp3")
    cfg = make_config(mode="playlist", playlist_folder=folder,
                      playlist_loop_mode="loop_playlist", fade_enabled=False)
    app, player, mp = player_factory(cfg)

    assert player.play(cfg)
    _wait_loaded(mp, 1, player)
    mp.finish()
    _wait_loaded(mp, 2, player)
    mp.finish()
    _wait_loaded(mp, 3, player)
    assert mp.current_path() == fireplace

    mp.position_ms = 440_000            # fireplace.mp3 @ 07:20
    player.stop()
    _wait_finished(player)
    assert player.resume_state.path == fireplace
    assert player.resume_state.index == 2

    assert player.play(cfg)
    _wait_loaded(mp, 4, player)
    assert mp.loads[-1] == fireplace, "resumed the wrong track"
    assert mp.seeks[-1] == 440_000, "resumed at the wrong position"


def test_playlist_resume_saved_track_deleted(player_factory, make_config,
                                             audio_files, fast_timing):
    folder, (rain, forest, fireplace) = audio_files(
        "01_rain.mp3", "02_forest.mp3", "03_fireplace.mp3")
    cfg = make_config(mode="playlist", playlist_folder=folder, fade_enabled=False)
    app, player, mp = player_factory(cfg)

    player.play(cfg)
    _wait_loaded(mp, 1, player)
    mp.finish()
    _wait_loaded(mp, 2, player)                 # forest
    mp.position_ms = 50_000
    player.stop()
    _wait_finished(player)

    os.remove(forest)
    seeks_before = len(mp.seeks)
    player.play(cfg)
    _wait_loaded(mp, 3, player)
    # The next track in order, from its beginning: never forest's timestamp.
    assert mp.loads[-1] == fireplace
    assert len(mp.seeks) == seeks_before


def test_playlist_resume_ignored_for_other_folder(player_factory, make_config,
                                                  audio_files, fast_timing):
    folder_a, (a1, a2) = audio_files("a1.mp3", "a2.mp3", folder="A")
    folder_b, (b1, b2) = audio_files("b1.mp3", "b2.mp3", folder="B")
    cfg = make_config(mode="playlist", playlist_folder=folder_a, fade_enabled=False)
    app, player, mp = player_factory(cfg)

    player.play(cfg)
    _wait_loaded(mp, 1, player)
    mp.finish()
    _wait_loaded(mp, 2, player)
    mp.position_ms = 30_000
    player.stop()
    _wait_finished(player)

    cfg["playlist_folder"] = folder_b
    player.play(cfg)
    _wait_loaded(mp, 3, player)
    assert mp.loads[-1] == b1
    assert mp.seeks == []


def test_single_resume_only_for_same_file(player_factory, make_config,
                                          audio_files, fast_timing):
    folder, (rain, forest) = audio_files("rain.mp3", "forest.mp3")
    cfg = make_config(mode="single", mp3_path=rain, fade_enabled=False)
    app, player, mp = player_factory(cfg)

    player.play(cfg)
    _wait_loaded(mp, 1, player)
    mp.position_ms = 763_000            # rain.mp3 @ 12:43
    mp.length_ms = 900_000
    player.stop()
    _wait_finished(player)

    player.play(cfg)                    # same file → resume
    _wait_loaded(mp, 2, player)
    assert mp.seeks[-1] == 763_000

    mp.position_ms = 100_000
    player.stop()
    _wait_finished(player)
    cfg["mp3_path"] = forest            # user picked another file
    n_seeks = len(mp.seeks)
    player.play(cfg)
    _wait_loaded(mp, 3, player)
    assert mp.loads[-1] == forest
    assert len(mp.seeks) == n_seeks, "another file's timestamp was applied"


def test_resume_past_end_starts_from_zero(player_factory, make_config,
                                          audio_files, fast_timing):
    folder, (rain,) = audio_files("rain.mp3")
    cfg = make_config(mode="single", mp3_path=rain, fade_enabled=False)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    mp.position_ms = 599_800
    player.stop()
    _wait_finished(player)
    mp.length_ms = 300_000              # file was replaced by a shorter one
    player.play(cfg)
    _wait_loaded(mp, 2, player)
    assert mp.seeks == []


# ── Priority 3: Loop Song ────────────────────────────────────────────────────

def test_loop_song_loads_once_per_repeat(player_factory, make_config,
                                         audio_files, fast_timing):
    folder, (rain, forest) = audio_files("rain.mp3", "forest.mp3")
    cfg = make_config(mode="playlist", playlist_folder=folder,
                      playlist_loop_mode="loop_song", fade_enabled=False)
    app, player, mp = player_factory(cfg)

    player.play(cfg)
    _wait_loaded(mp, 1, player)
    for repeat in range(3):
        mp.finish()
        _wait_loaded(mp, repeat + 2, player)
        time.sleep(0.05)                # give a redundant reload time to show up
        assert len(mp.loads) == repeat + 2, f"redundant restart: {mp.loads}"
    assert set(mp.loads) == {forest}    # sorted: forest.mp3 before rain.mp3


def test_stop_mode_ends_after_track(player_factory, make_config, audio_files, fast_timing):
    folder, files = audio_files("a.mp3", "b.mp3")
    cfg = make_config(mode="playlist", playlist_folder=folder,
                      playlist_loop_mode="stop", fade_enabled=False)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    mp.finish()
    _wait_finished(player)
    assert not player.playing
    assert len(mp.loads) == 1
    assert player.resume_state is None   # natural end: next start is from the top


# ── Priority 5: non-blocking stop, no fighting fades ─────────────────────────

def test_stop_does_not_block_during_fade(player_factory, make_config, audio_files,
                                         fast_timing, monkeypatch, appmod):
    monkeypatch.setattr(appmod, "FADE_SECONDS", 1.0)
    folder, (rain,) = audio_files("rain.mp3")
    cfg = make_config(mode="single", mp3_path=rain, fade_enabled=True)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    assert wait_until(lambda: mp.volume == 80, timeout=3)   # fade-in finished

    t0 = time.monotonic()
    player.stop()
    assert time.monotonic() - t0 < 0.1, "stop() blocked the caller"
    assert not player.playing
    # The fade-out still happens, on the playback thread.
    assert wait_until(lambda: ("stop",) in mp.events, timeout=3)
    fade_out = [v for v in mp.volumes[-60:] if v < 80]
    assert fade_out, "no fade-out volume steps"


def test_play_during_fade_out_waits_for_it(player_factory, make_config, audio_files,
                                           fast_timing, monkeypatch, appmod):
    monkeypatch.setattr(appmod, "FADE_SECONDS", 0.4)
    folder, (rain,) = audio_files("rain.mp3")
    cfg = make_config(mode="single", mp3_path=rain, fade_enabled=True)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    assert wait_until(lambda: mp.volume == 80)

    player.stop()
    assert player.play(cfg)             # supersedes while the fade-out is running
    _wait_loaded(mp, 2, player)
    kinds = [e[0] for e in mp.events if e[0] in ("load", "stop")]
    # The old session stops VLC before the new one loads: never two owners.
    assert kinds[-2:] == ["stop", "load"], mp.events


def test_rapid_play_stop_never_two_sessions(player_factory, make_config, audio_files,
                                            fast_timing, appmod):
    import threading
    folder, (rain,) = audio_files("rain.mp3")
    cfg = make_config(mode="single", mp3_path=rain, fade_enabled=False)
    app, player, mp = player_factory(cfg)
    for _ in range(30):
        player.play(cfg)
        player.stop()
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    time.sleep(0.2)
    alive = [t for t in threading.enumerate() if t.name == "AmbientPlayback" and t.is_alive()]
    assert len(alive) == 1
    assert player.playing


def test_duck_survives_track_change(player_factory, make_config, audio_files, fast_timing):
    folder, files = audio_files("a.mp3", "b.mp3")
    cfg = make_config(mode="playlist", playlist_folder=folder, fade_enabled=False,
                      max_volume=80)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    player.duck(20)
    assert wait_until(lambda: mp.volume == 20)
    mp.finish()
    _wait_loaded(mp, 2, player)
    time.sleep(0.1)
    assert mp.volume == 20, "next track ignored the duck and played at full volume"
    player.unduck(80)
    assert wait_until(lambda: mp.volume == 80)


def test_duck_then_unduck_last_one_wins(player_factory, make_config, audio_files,
                                        fast_timing):
    folder, (rain,) = audio_files("rain.mp3")
    cfg = make_config(mode="single", mp3_path=rain, fade_enabled=False, max_volume=80)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    player.duck(10)
    player.unduck(80)
    time.sleep(0.4)
    assert mp.volume == 80


# ── robustness ───────────────────────────────────────────────────────────────

def test_deleted_file_in_loop_mode_stops_cleanly(player_factory, make_config,
                                                 audio_files, fast_timing):
    folder, (rain,) = audio_files("rain.mp3")
    cfg = make_config(mode="single", mp3_path=rain, single_loop_mode="loop",
                      fade_enabled=False)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    os.remove(rain)
    mp.finish()
    _wait_finished(player)
    assert not player.playing
    assert "could not play" in (player.last_error or "").lower()


def test_unplayable_playlist_does_not_spin(player_factory, make_config, audio_files,
                                           fast_timing):
    folder, files = audio_files("a.mp3", "b.mp3")
    cfg = make_config(mode="playlist", playlist_folder=folder, fade_enabled=False)
    app, player, mp = player_factory(cfg)
    mp.fail_paths.update(files)
    player.play(cfg)
    _wait_finished(player)
    assert "none of the files" in player.last_error.lower()
    assert len(mp.loads) == 2


def test_play_rejects_missing_source(player_factory, make_config):
    cfg = make_config(mode="single", mp3_path="")
    app, player, mp = player_factory(cfg)
    assert player.play(cfg) is False
    assert player.last_error
    assert not player.playing


def test_shutdown_releases_and_refuses_new_play(player_factory, make_config,
                                                audio_files, fast_timing):
    folder, (rain,) = audio_files("rain.mp3")
    cfg = make_config(mode="single", mp3_path=rain, fade_enabled=False)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    player.shutdown(timeout=3)
    assert player._session is None
    assert player.play(cfg) is False


# ── GitHub issue #2: fade-in on the first play of .opus files ────────────────

def test_fade_in_waits_until_sound_actually_starts(player_factory, make_config,
                                                   audio_files, fast_timing,
                                                   monkeypatch, appmod):
    """VLC says 'playing' ~830 ms before Opus audio is heard. The fade must
    start when the clock moves, and the volume must stay 0 until then."""
    monkeypatch.setattr(appmod, "FADE_SECONDS", 0.3)
    monkeypatch.setattr(appmod.AmbientPlayer, "AUDIO_START_TIMEOUT", 3.0)
    folder, (song,) = audio_files("office.opus")
    cfg = make_config(mode="single", mp3_path=song, fade_enabled=True)
    app, player, mp = player_factory(cfg)

    mp.volume = 100                       # a fresh VLC player starts at 100 %
    audio_delay = 0.4
    started = {}
    real_play, real_get_time = mp.play, mp.get_time

    def play():
        started["t"] = time.monotonic()
        real_play()

    def get_time():
        if "t" in started and time.monotonic() - started["t"] >= audio_delay:
            return real_get_time() + 1     # clock running → sound is coming out
        return real_get_time()

    mp.play, mp.get_time = play, get_time
    player.play(cfg)

    time.sleep(audio_delay * 0.75)        # VLC "playing", but no sound yet
    assert mp.is_playing()
    assert mp.volume == 0, "volume must stay at 0 until sound starts"
    assert wait_until(lambda: mp.volume == 80, timeout=3)
    first_nonzero = next(i for i, v in enumerate(mp.volumes) if v > 0)
    assert all(v == 0 for v in mp.volumes[:first_nonzero])
    ramp = mp.volumes[first_nonzero:]
    assert ramp == sorted(ramp) and ramp[0] <= 10, "fade-in must start from 0"


class FakeSimpleVolume:
    def __init__(self):
        self.levels = []

    def SetMasterVolume(self, level, ctx):
        self.levels.append(level)


class FakeSession:
    def __init__(self, pid, control):
        self.ProcessId = pid
        self.SimpleAudioVolume = control


def test_fades_use_windows_mixer_volume_when_available(player_factory, make_config,
                                                       audio_files, fast_timing,
                                                       monkeypatch, appmod):
    """Issue #2: VLC defers volume changes while its output holds a lock, so a
    fade turns into jumps. Fades go to the Windows mixer session instead."""
    import os
    control = FakeSimpleVolume()
    sessions = [FakeSession(os.getpid() + 1, FakeSimpleVolume()),
                FakeSession(os.getpid(), control)]
    monkeypatch.setattr(appmod, "list_sessions", lambda: list(sessions))
    folder, (song,) = audio_files("office.opus")
    cfg = make_config(mode="single", mp3_path=song, fade_enabled=True, max_volume=80)
    app, player, mp = player_factory(cfg)
    player.play(cfg)
    assert wait_until(lambda: control.levels and abs(control.levels[-1] - 0.8 ** 3) < 1e-9)
    assert mp.volumes == [], "VLC volume must not be used when the mixer session works"
    rising = control.levels[control.levels.index(0.0, -60):]
    assert rising == sorted(rising) and len(rising) > 20      # many small steps
    player.stop()
    assert wait_until(lambda: control.levels[-1] == 0.0)


def test_session_volume_falls_back_to_vlc(appmod):
    calls = []

    class Broken:
        def SetMasterVolume(self, level, ctx):
            calls.append(level)
            raise OSError("device gone")

    sv = appmod._SessionVolume(pid=42, sessions_source=lambda: [FakeSession(42, Broken())])
    assert sv.set(50) is False           # failure → caller uses VLC volume
    assert calls == [0.125]              # VLC's mapping: (50 %)³
    none = appmod._SessionVolume(pid=42, sessions_source=lambda: [])
    assert none.set(50) is False


def test_audible_until_fade_out_has_finished(player_factory, make_config, audio_files,
                                             fast_timing, monkeypatch, appmod):
    """The monitor keeps the Mirroring Fix on while our own sound can be heard."""
    monkeypatch.setattr(appmod, "FADE_SECONDS", 0.5)
    folder, (rain,) = audio_files("rain.mp3")
    cfg = make_config(mode="single", mp3_path=rain, fade_enabled=True)
    app, player, mp = player_factory(cfg)
    assert not player.audible
    player.play(cfg)
    _wait_loaded(mp, 1, player)
    assert player.audible
    player.stop()
    assert not player.playing and player.audible       # fading out
    _wait_finished(player)
    assert not player.audible


# ── user controls: pause / resume / seek / live level ────────────────────────

def _playing_single(player_factory, make_config, audio_files, **cfg):
    folder, (rain,) = audio_files("rain.mp3")
    c = make_config(mode="single", mp3_path=rain, fade_enabled=False, **cfg)
    app, player, mp = player_factory(c)
    player.play(c)
    _wait_loaded(mp, 1, player)
    return c, player, mp


def test_pause_fades_out_then_pauses_and_resume_fades_back(player_factory, make_config,
                                                           audio_files, fast_timing):
    c, player, mp = _playing_single(player_factory, make_config, audio_files, max_volume=80)
    assert wait_until(lambda: mp.volume == 80)
    assert player.pause()
    assert player.paused and not player.audible
    assert wait_until(lambda: ("pause", True) in mp.events)
    assert mp.volume == 0
    assert player.track_info().paused
    assert player.playing, "a paused track is still the current session"

    time.sleep(0.2)                       # the track must not be treated as ended
    assert player._session is not None and len(mp.loads) == 1

    assert player.resume()
    assert not player.paused and player.audible
    assert ("pause", False) in mp.events
    assert wait_until(lambda: mp.volume == 80)
    assert not player.track_info().paused
    player.stop(wait=True, timeout=3)


def test_toggle_pause_and_nothing_to_control(player_factory, make_config, audio_files,
                                             fast_timing):
    folder, (rain,) = audio_files("rain.mp3")
    c = make_config(mode="single", mp3_path=rain, fade_enabled=False)
    app, player, mp = player_factory(c)
    assert not player.pause() and not player.resume() and not player.seek(10)
    player.play(c)
    _wait_loaded(mp, 1, player)
    player.toggle_pause()
    assert player.paused
    player.toggle_pause()
    assert not player.paused
    player.stop(wait=True, timeout=3)


def test_stop_while_paused_ends_the_session(player_factory, make_config, audio_files,
                                            fast_timing):
    c, player, mp = _playing_single(player_factory, make_config, audio_files)
    player.pause()
    assert wait_until(lambda: ("pause", True) in mp.events)
    player.stop(wait=True, timeout=3)
    assert wait_until(lambda: player._session is None)
    assert not player.paused


def test_seek_is_clamped_to_the_track(player_factory, make_config, audio_files,
                                      fast_timing):
    c, player, mp = _playing_single(player_factory, make_config, audio_files)
    assert player.seek(42.5)
    assert mp.position_ms == 42_500
    player.seek(-30)
    assert mp.position_ms == 0
    player.seek(10_000)                   # past the end of the 600 s track
    assert mp.position_ms == 600_000 - 500
    player.stop(wait=True, timeout=3)


def test_set_level_moves_the_volume_while_playing(player_factory, make_config,
                                                  audio_files, fast_timing):
    c, player, mp = _playing_single(player_factory, make_config, audio_files, max_volume=80)
    assert wait_until(lambda: mp.volume == 80)
    player.set_level(35)
    assert wait_until(lambda: mp.volume == 35)
    player.duck(10)
    assert wait_until(lambda: mp.volume == 10)
    player.set_level(20)                  # the duck level changed while ducked
    assert wait_until(lambda: mp.volume == 20)
    player.stop(wait=True, timeout=3)


def test_set_level_while_paused_applies_on_resume(player_factory, make_config,
                                                  audio_files, fast_timing):
    c, player, mp = _playing_single(player_factory, make_config, audio_files, max_volume=80)
    player.pause()
    assert wait_until(lambda: ("pause", True) in mp.events)
    player.set_level(30)
    player.duck(15)                       # the monitor ducking while paused
    time.sleep(0.1)
    assert mp.volume == 0, "paused sound must stay silent"
    player.resume()
    assert wait_until(lambda: mp.volume == 15)
    player.stop(wait=True, timeout=3)
