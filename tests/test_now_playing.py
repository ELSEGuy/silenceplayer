import threading

import pytest
from conftest import make_app, pump, wait_until
from test_monitor import Clock, make_monitor

FILE = "C:/Music/asmr/ANIMAL CROSSING OFFICE.opus"


@pytest.fixture
def cfg(make_config):
    return make_config(mode="single", mp3_path=FILE)


def describe(appmod, cfg, phase=None, track=None, monitoring=True, **info):
    mi = appmod.MonitorInfo(phase, **info) if phase else None
    return appmod.describe_now_playing(monitoring, mi, track, cfg)


# ── what each state shows ────────────────────────────────────────────────────

def test_playing(appmod, cfg):
    np = describe(appmod, cfg, "playing", appmod.TrackInfo(FILE, 376.4, 724.0))
    assert (np.badge, np.kind, np.title) == ("Playing", "playing", "ANIMAL CROSSING OFFICE.opus")
    assert np.detail == "6:16 / 12:04"
    assert np.progress == pytest.approx(376.4 / 724)
    assert np.tooltip == "Silence Player\nPlaying: ANIMAL CROSSING OFFICE.opus"
    assert np.menu == "Playing: ANIMAL CROSSING OFFICE.opus"


def test_playing_before_vlc_knows_the_time(appmod, cfg):
    np = describe(appmod, cfg, "playing", appmod.TrackInfo(FILE))
    assert np.detail == "Starting…" and np.progress is None


def test_ducked_names_the_app(appmod, cfg):
    np = describe(appmod, cfg, "ducked", appmod.TrackInfo(FILE, 401, 724),
                  apps=("discord.exe",), duck_percent=50)
    assert np.badge == "Ducked 50%" and np.kind == "ducked"
    assert np.detail == "6:41 / 12:04 · discord.exe is playing"
    assert np.menu == "Quieter: ANIMAL CROSSING OFFICE.opus"


def test_ducked_while_coming_back_up(appmod, cfg):
    np = describe(appmod, cfg, "ducked", appmod.TrackInfo(FILE, 10, 724),
                  remaining=7.2, duck_percent=50)
    assert np.detail.endswith("back to full volume in 8s")


def test_waiting_countdown(appmod, cfg):
    np = describe(appmod, cfg, "waiting", remaining=17.4)
    assert np.badge == "Waiting" and np.detail == "Plays in 18s if it stays silent"
    assert np.tooltip == "Silence Player\nAmbient in 18s"
    assert np.menu == "Waiting for silence"
    assert np.title == "ANIMAL CROSSING OFFICE.opus"       # the chosen file, shown dimmed


def test_other_audio_playing(appmod, cfg):
    np = describe(appmod, cfg, "audio", apps=("discord.exe", "spotify.exe"))
    assert np.detail == "discord.exe and spotify.exe is playing"
    np = describe(appmod, cfg, "audio", apps=("a.exe", "b.exe", "c.exe"))
    assert np.detail == "a.exe and 2 more is playing"


def test_monitoring_off(appmod, cfg):
    np = describe(appmod, cfg, "playing", monitoring=False)
    assert (np.badge, np.kind, np.detail) == ("Off", "off", "Monitoring is off")


def test_no_file_chosen(appmod, make_config):
    np = appmod.describe_now_playing(False, None, None, make_config(mp3_path=""))
    assert np.title == "No file chosen"


def test_long_names_fit_windows_limits(appmod, make_config):
    long = "C:/x/" + "very long ambient name " * 10 + ".opus"
    cfg = make_config(mp3_path=long)
    np = describe(appmod, cfg, "playing", appmod.TrackInfo(long, 1, 2))
    assert len(np.tooltip) <= appmod.TRAY_TIP_MAX and np.tooltip.endswith("…")
    assert len(np.menu) <= appmod.TRAY_MENU_MAX


@pytest.mark.parametrize("secs,text", [(0, "0:00"), (65, "1:05"), (3725, "1:02:05"),
                                       (-3, "0:00")])
def test_format_clock(appmod, secs, text):
    assert appmod.format_clock(secs) == text


# ── the monitor reports its phase ────────────────────────────────────────────

def test_monitor_phases(appmod, make_config):
    clock = Clock()
    apps = set()
    app, mon = make_monitor(appmod, make_config, lambda ex: set(apps), clock=clock,
                            silence_seconds=5, duck_percent=50)
    state, ev = appmod._MonitorState(), threading.Event()
    assert mon.info.phase == "starting"
    mon._tick(state, ev)
    assert mon.info.phase == "waiting" and mon.info.remaining == pytest.approx(5)
    clock.t += 6
    mon._tick(state, ev)
    assert mon.info.phase == "playing"
    clock.t += 10
    apps.add("discord.exe")
    mon._tick(state, ev)
    assert mon.info == appmod.MonitorInfo("ducked", apps=("discord.exe",), duck_percent=50)
    apps.clear()
    mon._tick(state, ev)
    assert mon.info.phase == "ducked" and mon.info.remaining == pytest.approx(5)
    clock.t += 6
    mon._tick(state, ev)
    assert mon.info.phase == "playing"


def test_monitor_reports_other_audio(appmod, make_config):
    clock = Clock()
    app, mon = make_monitor(appmod, make_config, lambda ex: {"spotify.exe"}, clock=clock)
    mon._tick(appmod._MonitorState(), threading.Event())
    assert mon.info == appmod.MonitorInfo("audio", apps=("spotify.exe",))


# ── the player reports the track ─────────────────────────────────────────────

def test_player_track_info(player_factory, make_config, audio_files, fast_timing):
    folder, (rain,) = audio_files("rain.opus")
    c = make_config(mode="single", mp3_path=rain, fade_enabled=False)
    app, player, mp = player_factory(c)
    assert player.track_info() is None
    player.play(c)
    assert wait_until(lambda: player._session is not None
                      and player._session.track_started.is_set())
    mp.position_ms = 90_500
    info = player.track_info()
    assert info.path == rain and info.position == pytest.approx(90.5)
    assert info.length == pytest.approx(600)
    player.stop(wait=True, timeout=3)
    assert wait_until(lambda: player.track_info() is None)


# ── in the app ───────────────────────────────────────────────────────────────

class RecordingTray:
    def __init__(self):
        self.title = None
        self.menu_updates = 0

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, *a):
        pass

    def stop(self):
        pass


def test_card_tooltip_and_menu_follow_the_state(appmod, root, cfg_path, fast_timing,
                                                monkeypatch):
    monkeypatch.setattr(appmod.App, "NOW_PLAYING_MS", 20)
    a = make_app(appmod, root)
    a.config["mp3_path"] = FILE
    a.tray = tray = RecordingTray()
    track = {"t": appmod.TrackInfo(FILE, 30, 120)}
    a.player.track_info = lambda: track["t"]
    a.monitoring = True
    a.monitor.info = appmod.MonitorInfo("playing")
    pump(root, 0.15)
    assert a.np_badge.cget("text") == "Playing"
    assert a.np_title_var.get() == "ANIMAL CROSSING OFFICE.opus"
    assert a.np_detail.cget("text") == "0:30 / 2:00"
    assert tray.title == "Silence Player\nPlaying: ANIMAL CROSSING OFFICE.opus"
    menus = tray.menu_updates
    assert menus >= 1

    track["t"] = appmod.TrackInfo(FILE, 31, 120)            # time moves on
    pump(root, 0.1)
    assert a.np_detail.cget("text") == "0:31 / 2:00"
    assert tray.menu_updates == menus, "menu is rebuilt only when its text changes"

    a.monitor.info = appmod.MonitorInfo("ducked", apps=("discord.exe",), duck_percent=50)
    pump(root, 0.1)
    assert a.np_badge.cget("text") == "Ducked 50%"
    assert tray.title.endswith("Quieter: ANIMAL CROSSING OFFICE.opus")
    assert tray.menu_updates == menus + 1
    a.monitor.stop(wait=True)


def test_title_is_not_rewritten_every_refresh(appmod, root, cfg_path, fast_timing,
                                              monkeypatch):
    """Rewriting the name would restart its scrolling twice a second."""
    monkeypatch.setattr(appmod.App, "NOW_PLAYING_MS", 20)
    a = make_app(appmod, root)
    writes = []
    a.np_title_var.trace_add("write", lambda *x: writes.append(1))
    a.monitoring = True
    a.monitor.info = appmod.MonitorInfo("playing")
    t = {"pos": 1}
    a.player.track_info = lambda: appmod.TrackInfo(FILE, t["pos"], 100)
    for i in range(5):
        t["pos"] = i + 1
        pump(root, 0.05)
    assert len(writes) == 1
    a.monitor.stop(wait=True)


# ── Silence Player's own tray menu ───────────────────────────────────────────

def test_menu_header_lines(appmod, cfg):
    np = describe(appmod, cfg, "playing", appmod.TrackInfo(FILE, 1, 2))
    assert (np.menu_head, np.menu_line) == ("Now playing", "ANIMAL CROSSING OFFICE.opus")
    np = describe(appmod, cfg, "ducked", appmod.TrackInfo(FILE, 1, 2),
                  apps=("discord.exe",), duck_percent=50)
    assert np.menu_head == "Ducked to 50% for discord.exe"
    np = describe(appmod, cfg, "waiting", remaining=17.2)
    assert (np.menu_head, np.menu_line) == ("Waiting for silence", "Ambient starts in 18s")
    np = describe(appmod, cfg, "audio", apps=("spotify.exe",))
    assert np.menu_line == "spotify.exe is playing"
    np = describe(appmod, cfg, monitoring=False)
    assert np.menu_head == "Monitoring is off"


def test_right_click_opens_our_menu_and_left_click_is_unchanged(appmod, monkeypatch):
    calls = []

    class FakeWin32Icon:
        def __init__(self, *a):
            pass

        def _on_notify(self, wparam, lparam):
            calls.append(("pystray", lparam))

    monkeypatch.setattr(appmod.pystray, "Icon", FakeWin32Icon)
    icon = appmod.make_tray_icon("x", None, "tip", None,
                                 on_right_click=lambda: calls.append("ours"))
    icon._on_notify(0, 0x0205)                 # right button up
    icon._on_notify(0, 0x0202)                 # left button up
    assert calls == ["ours", ("pystray", 0x0202)]


def test_windows_menu_is_the_fallback(appmod, monkeypatch):
    calls = []

    class FakeWin32Icon:
        def __init__(self, *a):
            pass

        def _on_notify(self, wparam, lparam):
            calls.append(("pystray", lparam))

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(appmod.pystray, "Icon", FakeWin32Icon)
    icon = appmod.make_tray_icon("x", None, "tip", None, on_right_click=broken)
    icon._on_notify(0, 0x0205)
    assert calls == [("pystray", 0x0205)]


def _labels(widget):
    import tkinter as tk
    out = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            out.append(child)
        out.extend(_labels(child))
    return out


def test_tray_menu_items_and_actions(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a.config["mp3_path"] = FILE
    picked = []
    a._show_window = lambda: picked.append("settings")
    a.monitoring = True
    a.monitor.info = appmod.MonitorInfo("playing")
    a.player.track_info = lambda: appmod.TrackInfo(FILE, 1, 2)
    a._update_info = appmod.ReleaseInfo("1.4.1", "v1.4.1", appmod.RELEASES_URL)
    pump(root, 0.6)
    a._open_tray_menu(at=(100, 100))
    pump(root, 0.05)
    menu = a._tray_menu
    assert menu.is_open
    texts = [l.cget("text") for l in _labels(menu.win)]
    assert texts == ["Now playing", "ANIMAL CROSSING OFFICE.opus", "Open Settings",
                     "Pause", "Download v1.4.1", "Stop Monitoring", "Quit"]
    assert _labels(menu.win)[0].cget("fg") == menu.HEAD_COLORS["playing"]

    # the header follows the state while the menu is open
    a.monitor.info = appmod.MonitorInfo("ducked", apps=("discord.exe",), duck_percent=50)
    pump(root, 0.6)
    assert _labels(menu.win)[0].cget("text") == "Ducked to 50% for discord.exe"

    open_settings = _labels(menu.win)[2]
    open_settings.event_generate("<ButtonRelease-1>")
    pump(root, 0.05)
    assert picked == ["settings"] and not menu.is_open
    a.monitor.stop(wait=True)


def test_tray_menu_closes_on_escape(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a._open_tray_menu(at=(100, 100))
    pump(root, 0.05)
    win = a._tray_menu.win
    win.event_generate("<Escape>")
    pump(root, 0.05)
    assert not a._tray_menu.is_open
    assert not win.winfo_exists()
    a.monitor.stop(wait=True)


def test_tray_menu_stays_on_screen(appmod, root):
    menu = appmod.TrayMenu(root)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    menu.show("Now playing", "x.opus", "playing", [("Quit", lambda: None)], at=(sw - 5, sh - 5))
    root.update()
    w = menu.win
    assert w.winfo_x() + w.winfo_width() <= sw and w.winfo_y() + w.winfo_height() <= sh
    menu.close()


def test_paused(appmod, cfg):
    track = appmod.TrackInfo(FILE, 65, 130, paused=True)
    for phase in ("playing", "ducked"):
        now = describe(appmod, cfg, phase, track)
        assert now.kind == "paused" and now.badge == "Paused"
        assert now.detail == "1:05 / 2:10" and now.progress == pytest.approx(0.5)
        assert "Paused: " in now.tooltip
        assert now.menu_head == "Paused"


def _card_app(appmod, root, monkeypatch, track):
    monkeypatch.setattr(appmod.App, "NOW_PLAYING_MS", 20)
    a = make_app(appmod, root)
    a.config["mp3_path"] = FILE
    a.monitoring = True
    a.monitor.info = appmod.MonitorInfo("playing")
    calls = []

    class Controls:
        def __init__(self):
            self.track = track
            self.paused_flag = False

        def track_info(self):
            return self.track

        def toggle_pause(self):
            calls.append("toggle")

        def seek(self, secs):
            calls.append(("seek", round(secs, 1)))

        def set_level(self, v):
            calls.append(("level", round(v, 1)))

    ctl = Controls()
    for name in ("track_info", "toggle_pause", "seek", "set_level"):
        monkeypatch.setattr(a.player, name, getattr(ctl, name))
    pump(root, 0.1)
    return a, ctl, calls


def test_card_controls(appmod, root, cfg_path, fast_timing, monkeypatch):
    a, ctl, calls = _card_app(appmod, root, monkeypatch, appmod.TrackInfo(FILE, 60, 600))
    assert a.np_play_btn.cget("text") == "\u275a\u275a"
    a.np_play_btn.invoke()
    a.np_fwd_btn.invoke()
    a.np_back_btn.invoke()
    assert calls == ["toggle", ("seek", 70), ("seek", 50)]

    ctl.track = appmod.TrackInfo(FILE, 60, 600, paused=True)
    pump(root, 0.1)
    assert a.np_badge.cget("text") == "Paused"
    assert a.np_play_btn.cget("text") == "▶"

    # click a quarter of the way along the bar → 150 s of the 600 s track
    calls.clear()
    a.np_bar.update_idletasks()
    w = a.np_bar.winfo_width()
    a.np_bar.event_generate("<ButtonPress-1>", x=w // 4, y=5)
    a.np_bar.event_generate("<ButtonRelease-1>", x=w // 4, y=5)
    pump(root, 0.05)
    assert len(calls) == 1 and calls[0][0] == "seek"
    assert calls[0][1] == pytest.approx(150, abs=3)
    a.monitor.stop(wait=True)


def test_play_button_when_nothing_is_playing(appmod, root, cfg_path, fast_timing,
                                             monkeypatch):
    a, ctl, calls = _card_app(appmod, root, monkeypatch, None)
    a.monitor.info = appmod.MonitorInfo("waiting", remaining=20)
    asked = []
    monkeypatch.setattr(a.monitor, "play_now", lambda: asked.append(True))
    a.np_play_btn.invoke()
    assert asked == [True] and calls == []

    a.monitoring = False
    a.np_play_btn.invoke()
    pump(root, 0.05)
    assert asked == [True]
    assert "Start Monitoring" in a.status_var.get()
    a.monitor.stop(wait=True)


def test_volume_and_duck_apply_live(appmod, root, cfg_path, fast_timing, monkeypatch):
    a, ctl, calls = _card_app(appmod, root, monkeypatch, appmod.TrackInfo(FILE, 60, 600))
    a.config["duck_percent"] = 50
    a.np_vol.set(40)
    pump(root, 0.05)
    assert a.config["max_volume"] == 40 and a.vol_var.get() == "40"
    assert calls[-1] == ("level", 40)

    a.monitor.info = appmod.MonitorInfo("ducked", apps=("discord.exe",), duck_percent=50)
    a.duck_var.set(25)
    a._on_duck_change(25)
    assert a.config["duck_percent"] == 25
    assert calls[-1] == ("level", 10)     # 25% of 40

    a.vol_var.set("80")                   # typed in the Max Volume field
    a._commit_inputs_quietly()
    assert a.np_vol_var.get() == 80
    assert calls[-1] == ("level", 20)     # still ducked: 25% of 80
    assert not cfg_path.exists(), "live changes are not saved until Save Settings"
    a.monitor.stop(wait=True)
