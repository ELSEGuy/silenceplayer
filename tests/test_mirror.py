import numpy as np
import pytest


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeTap:
    instances = []

    def __init__(self, pid):
        self.pid = pid
        self.callback = None
        self.started = False
        self.stopped = False
        self.is_running = True
        FakeTap.instances.append(self)

    def set_callback(self, cb):
        self.callback = cb

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.is_running = False

    def feed(self, amplitude, n=960):
        pcm = (np.full(n * 2, amplitude, dtype=np.float32)).tobytes()
        self.callback(pcm, -1)


@pytest.fixture
def fix_env(appmod):
    FakeTap.instances = []
    clock = Clock()
    pids = {"current": {111: "discord.exe"}}

    def source(names):
        if isinstance(pids["current"], Exception):
            raise pids["current"]
        return {p: n for p, n in pids["current"].items() if n in names}

    fix = appmod.MirrorFix(apps_source=lambda: {"discord.exe", "signalrgb.exe"},
                           pid_source=source, capture_factory=FakeTap, clock=clock)
    return fix, clock, pids


def test_no_taps_is_conservative(fix_env):
    fix, clock, pids = fix_env
    assert fix.is_real_audio("discord.exe") is True


def test_warmup_counts_as_real_then_expires(fix_env):
    fix, clock, pids = fix_env
    fix._sync_taps()
    clock.t += 0.5
    assert fix.is_real_audio("discord.exe") is True      # warming up, no data yet
    clock.t += 2.0
    assert fix.is_real_audio("discord.exe") is False     # warm-up over, never produced audio


def test_real_stream_detected_during_warmup(fix_env):
    fix, clock, pids = fix_env
    fix._sync_taps()
    tap = FakeTap.instances[0]
    tap.feed(0.1)                                    # one sample is enough if it's loud
    assert fix.is_real_audio("discord.exe") is True


def test_stale_samples_expire(fix_env):
    fix, clock, pids = fix_env
    fix._sync_taps()
    tap = FakeTap.instances[0]
    clock.t += 3                                     # past warm-up
    for _ in range(5):
        tap.feed(0.1)
    assert fix.is_real_audio("discord.exe") is True
    clock.t += fix.STALE_AFTER_SECONDS + 0.1         # callbacks stopped, PID alive
    assert fix.is_real_audio("discord.exe") is False


def test_fresh_silence_is_mirroring(fix_env):
    fix, clock, pids = fix_env
    fix._sync_taps()
    tap = FakeTap.instances[0]
    clock.t += 3
    for _ in range(10):
        tap.feed(0.0)
    assert fix.is_real_audio("discord.exe") is False


def test_rms_uses_float32(fix_env, appmod):
    fix, clock, pids = fix_env
    fix._sync_taps()
    tap = FakeTap.instances[0]
    tap.feed(0.5)
    state = fix._taps[111]
    assert state.samples[-1][1] == pytest.approx(0.5, rel=1e-6)


def test_pid_cleanup_removes_state_and_stops_tap(fix_env):
    fix, clock, pids = fix_env
    fix._sync_taps()
    tap = FakeTap.instances[0]
    tap.feed(0.1)
    pids["current"] = {}
    fix._sync_taps()
    assert 111 not in fix._taps
    assert tap.stopped
    tap.feed(0.2)                                    # late callback after removal
    assert fix._taps == {}


def test_pycaw_error_keeps_existing_taps(fix_env):
    fix, clock, pids = fix_env
    fix._sync_taps()
    pids["current"] = OSError("pycaw failed")
    with pytest.raises(OSError):
        fix._sync_taps()
    assert 111 in fix._taps


def test_failed_tap_is_conservative_and_backs_off(appmod):
    clock = Clock()
    attempts = []

    def factory(pid):
        attempts.append(clock.t)
        raise OSError("access denied")

    fix = appmod.MirrorFix(apps_source=lambda: {"discord.exe"},
                           pid_source=lambda names: {222: "discord.exe"},
                           capture_factory=factory, clock=clock)
    fix._sync_taps()
    assert fix.is_real_audio("discord.exe") is True
    clock.t += 1
    fix._sync_taps()
    assert len(attempts) == 1                        # waiting for retry
    clock.t += fix.RETRY_BASE_SECONDS
    fix._sync_taps()
    assert len(attempts) == 2


def test_tap_opened_during_stop_is_not_orphaned(appmod):
    clock = Clock()
    holder = {}

    class RacingTap(FakeTap):
        def start(self):
            super().start()
            holder["fix"]._stop_event.set()          # stop() lands mid-open

    fix = appmod.MirrorFix(apps_source=lambda: {"discord.exe"},
                           pid_source=lambda names: {333: "discord.exe"},
                           capture_factory=RacingTap, clock=clock)
    holder["fix"] = fix
    FakeTap.instances = []
    fix._open_tap(333, "discord.exe")
    assert fix._taps == {}
    assert FakeTap.instances[0].stopped


def test_dead_tap_is_reopened(fix_env):
    fix, clock, pids = fix_env
    fix._sync_taps()
    first = FakeTap.instances[0]
    first.is_running = False
    fix._sync_taps()                                 # removes the dead one
    fix._sync_taps()                                 # (reopen happens in the same pass)
    assert fix._taps[111].tap is not first
    assert first.stopped


def test_start_stop_thread(appmod, monkeypatch):
    monkeypatch.setattr(appmod.MirrorFix, "WATCH_INTERVAL", 0.01)
    FakeTap.instances = []
    fix = appmod.MirrorFix(apps_source=lambda: {"discord.exe"},
                           pid_source=lambda names: {1: "discord.exe", 2: "discord.exe"},
                           capture_factory=FakeTap)
    fix.start()
    fix.start()                                      # idempotent
    import time
    deadline = time.monotonic() + 2
    while len(fix._taps) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    fix.stop()
    assert not fix._watch_thread.is_alive()
    assert fix._taps == {}
    assert all(t.stopped for t in FakeTap.instances)


# ── several apps ─────────────────────────────────────────────────────────────

def test_apps_are_judged_separately(fix_env):
    fix, clock, pids = fix_env
    pids["current"] = {111: "discord.exe", 222: "signalrgb.exe"}
    fix._sync_taps()
    discord, signal = sorted(FakeTap.instances, key=lambda t: t.pid)
    clock.t += 3
    for _ in range(5):
        discord.feed(0.1)                            # someone talking on Discord
    assert fix.is_real_audio("discord.exe") is True
    assert fix.is_real_audio("signalrgb.exe") is False   # only listening, renders nothing


def test_removing_app_from_list_stops_its_taps(appmod):
    clock = Clock()
    watched = {"names": {"discord.exe", "signalrgb.exe"}}
    FakeTap.instances = []
    fix = appmod.MirrorFix(apps_source=lambda: watched["names"],
                           pid_source=lambda names: {p: n for p, n in
                                                     {1: "discord.exe", 2: "signalrgb.exe"}.items()
                                                     if n in names},
                           capture_factory=FakeTap, clock=clock)
    fix._sync_taps()
    assert set(fix._taps) == {1, 2}
    watched["names"] = {"discord.exe"}
    fix._sync_taps()
    assert set(fix._taps) == {1}
    assert [t.stopped for t in sorted(FakeTap.instances, key=lambda t: t.pid)] == [False, True]


# ── quiet / bursty audio ─────────────────────────────────────────────────────

def test_one_loud_chunk_in_two_seconds_counts_as_real(fix_env):
    """A stream with gaps in its sound must not flip to 'mirroring only'."""
    fix, clock, pids = fix_env
    fix._sync_taps()
    tap = FakeTap.instances[0]
    clock.t += 3
    tap.feed(0.05)
    for _ in range(50):                               # silence after the burst
        clock.t += 0.02
        tap.feed(0.0)
    assert fix.is_real_audio("discord.exe") is True
    clock.t += fix.STALE_AFTER_SECONDS + 0.1          # the burst is now old
    tap.feed(0.0)
    assert fix.is_real_audio("discord.exe") is False


def test_window_covers_the_whole_stale_period(fix_env):
    fix, clock, pids = fix_env
    fix._sync_taps()
    tap = FakeTap.instances[0]
    clock.t += 3
    tap.feed(0.1)
    for _ in range(100):                              # 100 quiet 10 ms chunks follow
        clock.t += 0.01
        tap.feed(0.0)
    assert fix.is_real_audio("discord.exe") is True


def test_verdict_log_includes_levels(fix_env, appmod):
    import logging

    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Grab(level=logging.INFO)
    appmod.log.addHandler(handler)          # the app logger doesn't propagate to root
    try:
        fix, clock, pids = fix_env
        fix._sync_taps()
        tap = FakeTap.instances[0]
        clock.t += 3
        tap.feed(0.0)
        fix.is_real_audio("discord.exe")
    finally:
        appmod.log.removeHandler(handler)
    text = " ".join(records)
    assert "mirroring only" in text and "PID 111" in text and "loudest" in text
