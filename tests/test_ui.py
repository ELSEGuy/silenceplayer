"""Smoke tests for the Tk App. Skipped where Tk or a display isn't available."""
import json
import threading
import time

import pytest

tk = pytest.importorskip("tkinter")
if not hasattr(tk, "Tk"):
    pytest.skip("tkinter placeholder module", allow_module_level=True)


from conftest import fits_on_screen, make_app, pump  # noqa: E402


def test_first_run_keeps_settings_visible(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    assert a.tabs.get() == "main" and a.tabs.label_of("main") == "Player"
    assert root.state() != "withdrawn"
    assert not a.monitoring
    assert "Welcome" in a.status_var.get()
    a.monitor.stop(wait=True)


def test_valid_config_starts_hidden_and_monitoring(appmod, root, cfg_path, tmp_path,
                                                    fast_timing):
    song = tmp_path / "rain.mp3"
    song.write_bytes(b"")
    cfg_path.write_text(json.dumps({"mp3_path": str(song)}))
    a = make_app(appmod, root)
    assert a.monitoring and a.monitor.running
    assert root.state() == "withdrawn"
    a.monitor.stop(wait=True)


def test_only_save_settings_writes_config(appmod, root, cfg_path, fast_timing,
                                          monkeypatch):
    monkeypatch.setattr(appmod.messagebox, "showerror", lambda *a, **k: None)
    a = make_app(appmod, root)
    assert not cfg_path.exists()
    a.silence_var.set("45")
    a.np_vol.set(60)                           # the volume slider
    pump(root, 0.05)
    a._commit_inputs_quietly()                 # applies to the running app...
    assert a.config["silence_seconds"] == 45 and a.config["max_volume"] == 60
    a._toggle_monitoring()                     # (fails: no file) – must not save either
    a._on_window_close()
    assert not cfg_path.exists(), "closing must not save"
    a._show_window()
    a._save_settings()                         # ...but is only written here
    saved = json.loads(cfg_path.read_text())
    assert saved["silence_seconds"] == 45
    assert saved["max_volume"] == 60
    a.monitor.stop(wait=True)


def test_invalid_value_on_close_is_reverted(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a.silence_var.set("abc")
    a._on_window_close()
    assert root.state() == "withdrawn"
    assert a.silence_var.get() == "30"          # back to the value in use
    assert a.config["silence_seconds"] == 30
    assert not cfg_path.exists()
    a.monitor.stop(wait=True)


def test_stop_monitoring_does_not_block_ui(appmod, root, cfg_path, tmp_path,
                                           monkeypatch, fast_timing):
    monkeypatch.setattr(appmod, "FADE_SECONDS", 1.5)
    song = tmp_path / "rain.mp3"
    song.write_bytes(b"")
    cfg_path.write_text(json.dumps({"mp3_path": str(song), "fade_enabled": True}))
    a = make_app(appmod, root)
    a.player.play(a.config)
    mp = a.player.media_player
    deadline = time.monotonic() + 3
    while mp.volume != 80 and time.monotonic() < deadline:
        pump(root, 0.05)
    t0 = time.monotonic()
    a._toggle_monitoring()                       # Stop Monitoring, 1.5 s fade
    assert time.monotonic() - t0 < 0.1
    assert not a.monitoring
    assert a.start_btn.cget("text") == "Start Monitoring"
    a.monitor.stop(wait=True)
    a.player.stop(wait=True, timeout=3)


def test_start_with_missing_file_shows_clear_error(appmod, root, cfg_path, monkeypatch,
                                                   fast_timing):
    a = make_app(appmod, root)
    shown = []
    monkeypatch.setattr(appmod.messagebox, "showerror",
                        lambda title, msg, **kw: shown.append(msg))
    a.config["mp3_path"] = "C:/nope/missing.mp3"
    a._refresh_path_fields()
    a.silence_var.set("0")
    a._toggle_monitoring()
    assert not a.monitoring
    assert "1 and 3600" in shown[0] and "could not be found" in shown[0]


def test_worker_status_updates_reach_ui(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    threading.Thread(target=a.set_status, args=("from a worker",)).start()
    pump(root, 0.2)
    assert a.status_var.get() == "from a worker"
    a.monitor.stop(wait=True)


def test_quit_shuts_everything_down(appmod, root, cfg_path, tmp_path, fast_timing):
    song = tmp_path / "rain.mp3"
    song.write_bytes(b"")
    cfg_path.write_text(json.dumps({"mp3_path": str(song)}))
    a = make_app(appmod, root)
    a.player.play(a.config)
    a.silence_var.set("12")
    a._quit()
    deadline = time.monotonic() + 5
    while a._ui_alive and time.monotonic() < deadline:
        try:
            pump(root, 0.05)
        except tk.TclError:
            break
    assert not a._ui_alive
    assert not a.monitor.running
    assert a.player._session is None
    # Quitting doesn't save: the file still has the value from before.
    assert json.loads(cfg_path.read_text()).get("silence_seconds", 30) != 12


def test_mirror_suggestion_one_click(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a.on_suspected_mirror(["audioviz.exe"])
    pump(root, 0.2)
    assert a.suggest_row.winfo_ismapped()
    assert "audioviz.exe" in a.suggest_label.cget("text")
    a._accept_mirror_suggestion()
    pump(root, 0.1)
    assert not a.suggest_row.winfo_ismapped()
    assert not cfg_path.exists()                            # saved only via Save Settings
    if appmod.PROCTAP_IMPORT_ERROR is None:
        assert {"name": "audioviz.exe", "enabled": True} in a.config["mirror_apps"]
    else:
        assert "audioviz.exe" in a.config["excluded_apps"]
    a.on_suspected_mirror(["audioviz.exe"])            # already handled → no new prompt
    pump(root, 0.2)
    assert not a.suggest_row.winfo_ismapped()
    a._stop_mirror_fix(wait=True)
    a.monitor.stop(wait=True)


def test_mirror_list_toggle_and_add(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    assert a.mirror_listbox.size() == len(appmod.DEFAULT_CONFIG["mirror_apps"])
    a.mirror_entry_var.set("OBS64")
    a.mirror_listbox.selection_clear(0, "end")
    # "Add" is the first button in the add row; call the same path directly.
    a._set_mirror_app(appmod.normalize_app_name(a.mirror_entry_var.get()), True)
    a._apply_mirror_state()
    a._refresh_mirror_ui()
    assert "obs64.exe" in a._mirror_names
    a._set_mirror_app("discord.exe", False)
    a._apply_mirror_state()
    assert "discord.exe" not in a._mirror_names
    a._stop_mirror_fix(wait=True)
    a.monitor.stop(wait=True)


def test_path_fields_show_only_names(appmod, root, cfg_path, tmp_path, monkeypatch,
                                     fast_timing):
    song = tmp_path / "asmr" / "Animal Crossing Office.m4a"
    song.parent.mkdir()
    song.write_bytes(b"")
    a = make_app(appmod, root)
    assert a.mp3_var.get() == "Click to choose a file…"
    monkeypatch.setattr(appmod.filedialog, "askopenfilename", lambda **kw: str(song))
    monkeypatch.setattr(appmod.filedialog, "askdirectory", lambda **kw: str(song.parent))
    a._browse_mp3()
    a._browse_playlist()
    assert a.mp3_var.get() == "Animal Crossing Office.m4a"
    assert a.playlist_var.get() == "asmr"
    a._save_settings()
    saved = json.loads(cfg_path.read_text())
    assert saved["mp3_path"] == str(song)                   # full path still saved
    assert saved["playlist_folder"] == str(song.parent)
    assert isinstance(a.mp3_entry, appmod.MarqueeLabel)
    a.monitor.stop(wait=True)


def test_marquee_scrolls_only_long_names(appmod, root, fast_timing):
    short, long = tk.StringVar(master=root, value="asmr"), tk.StringVar(master=root)
    long.set("Tomodachi Life - Living The Dream (Ambience and SFX from SOUNDTRACK).mp3")
    a = appmod.MarqueeLabel(root, short, 22, "#313244", "#cdd6f4", animate=True)
    b = appmod.MarqueeLabel(root, long, 22, "#313244", "#cdd6f4", animate=True)
    a.pack(); b.pack()
    b.START_PAUSE_MS = 50
    b.reset()
    pump(root, 0.1)
    assert not a.scrolling and b.scrolling
    x0 = b.coords(b._items[0])[0]
    pump(root, 0.5)
    assert b.coords(b._items[0])[0] < x0, "long text should move left"
    assert a.coords(a._items[0])[0] == a.PAD_X, "short text must stay still"
    b._set_hover(True)
    x1 = b.coords(b._items[0])[0]
    pump(root, 0.4)
    assert b.coords(b._items[0])[0] == x1, "hover pauses"
    long.set("short.mp3")
    pump(root, 0.05)
    assert not b.scrolling


def test_marquee_without_animations_uses_ellipsis(appmod, root):
    var = tk.StringVar(master=root, value="A very very long ambient file name that won't fit.opus")
    m = appmod.MarqueeLabel(root, var, 22, "#313244", "#cdd6f4", animate=False)
    m.pack()
    pump(root, 0.05)
    shown = m.itemcget(m._items[0], "text")
    assert not m.scrolling and shown.endswith("…") and len(shown) < len(var.get())


# ── Start with Windows ───────────────────────────────────────────────────────

class _UiRegistry:
    store = {}
    fail = False

    def get(self, path, name):
        return self.store.get((path, name))

    def set(self, path, name, value):
        if self.fail:
            raise PermissionError("denied")
        self.store[(path, name)] = value

    def delete(self, path, name):
        if self.fail:
            raise PermissionError("denied")
        self.store.pop((path, name), None)


@pytest.fixture
def fake_registry(appmod, monkeypatch):
    _UiRegistry.store = {}
    _UiRegistry.fail = False
    monkeypatch.setattr(appmod, "_WinRegistry", _UiRegistry)
    return _UiRegistry


def _run_value(appmod, reg):
    return reg.store.get((appmod.StartupEntry.RUN_KEY, appmod.StartupEntry.VALUE_NAME))


def test_start_with_windows_is_only_written_by_save_settings(appmod, root, cfg_path,
                                                             fake_registry, fast_timing):
    a = make_app(appmod, root)
    assert not a.startup_switch.get() and not a.startup_var.get()
    a.startup_switch.invoke()                 # flipping the switch
    assert a.startup_switch.get() and a.startup_var.get()
    assert _run_value(appmod, fake_registry) is None, "toggling alone must not change Windows"
    a._on_window_close()
    assert _run_value(appmod, fake_registry) is None, "closing must not change it either"
    a._show_window()
    a._save_settings()
    assert _run_value(appmod, fake_registry) == appmod.startup_command()
    pump(root, 0.1)                       # status arrives via the UI queue
    assert a.status_var.get() == "Settings saved!"
    a._toggle_startup_choice()
    a._save_settings()
    assert _run_value(appmod, fake_registry) is None
    a.monitor.stop(wait=True)


def test_checkbox_reflects_an_existing_entry(appmod, root, cfg_path, fake_registry, fast_timing):
    fake_registry.store[(appmod.StartupEntry.RUN_KEY, appmod.StartupEntry.VALUE_NAME)] = '"x.exe"'
    a = make_app(appmod, root)
    assert a.startup_var.get() and a.startup_switch.get()
    a.monitor.stop(wait=True)


def test_start_with_windows_failure_is_reported_and_reverted(appmod, root, cfg_path,
                                                              fake_registry, fast_timing):
    a = make_app(appmod, root)
    a._toggle_startup_choice()
    fake_registry.fail = True
    a._save_settings()
    pump(root, 0.1)                       # status arrives via the UI queue
    assert "Start with Windows could not be changed" in a.status_var.get()
    assert not a.startup_var.get() and not a.startup_switch.get()
    assert cfg_path.exists(), "the other settings are still saved"
    a.monitor.stop(wait=True)


def test_start_with_windows_unavailable_off_windows(appmod, root, cfg_path, monkeypatch,
                                                    fast_timing):
    def no_winreg():
        raise ImportError("winreg")

    monkeypatch.setattr(appmod, "_WinRegistry", no_winreg)
    a = make_app(appmod, root)
    assert a.startup_switch.cget("state") == "disabled"
    assert a.startup_hint.cget("text") == "Only available on Windows."
    a.startup_switch.invoke()                 # ignored while disabled
    a._toggle_startup_choice()
    assert not a.startup_var.get() and not a.startup_switch.get()
    a.monitor.stop(wait=True)


def test_playlist_is_locked_while_monitoring(appmod, root, cfg_path, tmp_path,
                                             monkeypatch, fast_timing):
    folder = tmp_path / "ambient"
    folder.mkdir()
    (folder / "rain.mp3").write_bytes(b"")
    cfg_path.write_text(json.dumps({"mode": "playlist", "playlist_folder": str(folder)}))
    a = make_app(appmod, root)
    asked = []
    monkeypatch.setattr(appmod.filedialog, "askdirectory",
                        lambda **k: asked.append(True) or "C:/Other")

    # a playable playlist starts monitoring at launch, so it's locked from the start
    assert a.monitoring
    assert a.playlist_browse_btn.cget("state") == "disabled"
    a._browse_playlist()                         # also what clicking the folder name does
    pump(root, 0.05)
    assert asked == [] and a.config["playlist_folder"] == str(folder)
    assert a.status_var.get() == "Stop Monitoring to change the playlist."

    a.set_status("")
    pump(root, 0.05)
    a.playlist_browse_btn.event_generate("<Button-1>")   # the greyed-out button
    pump(root, 0.05)
    assert a.status_var.get() == "Stop Monitoring to change the playlist."
    assert asked == []

    a._toggle_monitoring()                       # Stop Monitoring
    assert not a.monitoring
    assert a.playlist_browse_btn.cget("state") == "normal"
    a._browse_playlist()
    assert asked == [True] and a.config["playlist_folder"] == "C:/Other"

    a.config["playlist_folder"] = str(folder)
    a._toggle_monitoring()                       # Start Monitoring again
    assert a.monitoring
    assert a.playlist_browse_btn.cget("state") == "disabled"

    # monitoring that stops by itself unlocks it too
    a.monitor.stop(wait=True)
    a._handle_auto_stop("stopped")
    assert a.playlist_browse_btn.cget("state") == "normal"
    a.player.stop(wait=True, timeout=3)


def test_mode_is_locked_while_monitoring(appmod, root, cfg_path, tmp_path, fast_timing):
    song = tmp_path / "rain.mp3"
    song.write_bytes(b"")
    cfg_path.write_text(json.dumps({"mode": "single", "mp3_path": str(song)}))
    a = make_app(appmod, root)
    assert a.monitoring                          # a playable file starts monitoring
    pill = a.mode_pill
    assert pill.state_of("single") == "normal"           # the current mode stays lit
    assert pill.state_of("playlist") == "disabled"
    x, y = pill.center_of("playlist")
    pill.event_generate("<ButtonPress-1>", x=x, y=y)     # clicking the greyed-out mode
    pill.event_generate("<ButtonRelease-1>", x=x, y=y)
    pump(root, 0.05)
    assert a.status_var.get() == "Stop Monitoring to change the mode."
    assert a.config["mode"] == "single" and pill.get() == "single"

    a._toggle_monitoring()                       # Stop Monitoring
    assert pill.state_of("playlist") == "normal"
    pill.event_generate("<ButtonPress-1>", x=x, y=y)
    pill.event_generate("<ButtonRelease-1>", x=x, y=y)
    assert a.config["mode"] == "playlist" and a.mode_var.get() == "playlist"
    pill.invoke("single")
    assert a.config["mode"] == "single"
    a.player.stop(wait=True, timeout=3)
    a.monitor.stop(wait=True)


def test_settings_layout(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a._show_window()
    pump(root, 0.1)
    # Silence Timeout and Fade share one row
    assert a.silence_entry.master.master.master is a.fade_switch.master.master   # same card
    assert abs(a.silence_entry.winfo_rooty() - a.fade_switch.winfo_rooty()) < 12
    # Start with Windows lives in the Advanced tab only
    w = a.startup_switch
    while w is not None and w not in (a.tab_main, a.tab_advanced):
        w = w.master
    assert w is a.tab_advanced

    def texts(widget):
        out = []
        for child in widget.winfo_children():
            if isinstance(child, appmod.tk.Label):
                out.append(child.cget("text"))
            out.extend(texts(child))
        return out

    main = texts(a.tab_main)
    assert "Ambient Sound File" in main
    assert not any("MP3, OPUS" in t for t in main)
    assert not any("Start with Windows" in t for t in main)
    assert any("10-90% = Duck volume" in t for t in main)
    assert any(t == "Start with Windows" for t in texts(a.tab_advanced))
    a.monitor.stop(wait=True)


# ── one window height, Advanced layout, loop pills ───────────────────────────

def test_every_tab_fits_one_height_and_tabs_never_resize(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a._show_window()
    pump(root, 0.1)
    height = a.WINDOW_H
    for tab in ("main", "exclude", "advanced"):
        a.tabs.invoke(tab)
        pump(root, 0.05)
        assert root.winfo_reqheight() <= height, f"{tab} tab doesn't fit"
        assert root.winfo_height() == height, "switching tabs must not resize the window"
    # a banner is the one thing that grows the window (by its own height)
    a.update_label.config(text="Silence Player v9.9 is available.")
    a.update_row.pack(fill="x", padx=10, pady=(8, 0), before=a.status_label)
    a._fit_window()
    pump(root, 0.1)
    grown = height + a.update_row.winfo_reqheight() + a.BAR_GAP
    if fits_on_screen(root, grown):
        assert root.winfo_height() == grown
    a.update_row.pack_forget()
    a._fit_window()
    pump(root, 0.1)
    assert root.winfo_height() == height
    a.monitor.stop(wait=True)


def test_advanced_tab_is_compact(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    labels = [w.cget("text") for w in a.tab_advanced.winfo_children() if isinstance(w, appmod.tk.Label)]
    assert "Advanced Settings" not in labels
    assert a.mirror_listbox.winfo_reqheight() == 4 * a.mirror_listbox._row_h
    # updates and Start with Windows share one card
    assert a.update_switch.master.master is a.startup_switch.master.master
    a.monitor.stop(wait=True)


def test_loop_pills_change_the_setting(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a.single_loop_pill.invoke("stop")
    assert a.config["single_loop_mode"] == "stop" and a.single_loop_var.get() == "stop"
    a.playlist_loop_pill.invoke("loop_song")
    assert a.config["playlist_loop_mode"] == "loop_song"
    a.fade_switch.invoke()
    assert a.config["fade_enabled"] is False
    assert not cfg_path.exists(), "nothing is saved before Save Settings"
    a.monitor.stop(wait=True)


# ── Exclude Apps: Playing now ────────────────────────────────────────────────

def _chips(a):
    return [w.cget("text") for w in a.playing_chips.winfo_children()]


def test_playing_now_chips_add_an_app_in_one_click(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    assert _chips(a) == ["nothing right now"]
    a._update_playing_now(["discord.exe", "chrome.exe"])
    assert _chips(a) == ["+  chrome.exe", "+  discord.exe"]
    chip = a.playing_chips.winfo_children()[1]
    chip.invoke()                                        # click "+ discord.exe"
    assert "discord.exe" in a.exclude_listbox.get(0, "end")
    assert a.config["excluded_apps"][-1] == "discord.exe"
    assert _chips(a) == ["+  chrome.exe"], "an excluded app isn't offered again"
    # an app that went quiet stays listed for a few seconds, then goes
    a._update_playing_now([])
    assert _chips(a) == ["+  chrome.exe"]
    a._playing_seen["chrome.exe"] -= a.PLAYING_KEEP_SECONDS + 1
    a._update_playing_now([])
    assert _chips(a) == ["nothing right now"]
    a.monitor.stop(wait=True)


def test_playing_now_only_polls_while_the_tab_is_open(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a._show_window()
    calls = []

    def source(excluded):
        calls.append(list(excluded))
        return {"steam.exe", "spotify.exe"}

    a._playing_source = source
    a.PLAYING_POLL_SECONDS = 0.05
    assert a._playing_thread is None
    a.config["excluded_apps"] = ["spotify.exe"]
    a.tabs.invoke("exclude")
    pump(root, 0.3)
    assert calls and _chips(a) == ["+  steam.exe"], "already excluded apps aren't offered"
    a.tabs.invoke("main")
    pump(root, 0.15)
    n = len(calls)
    pump(root, 0.3)
    assert len(calls) == n, "stops polling on another tab"
    a.tabs.invoke("exclude")
    pump(root, 0.2)
    a._on_window_close()                                 # hidden in the tray: stops too
    pump(root, 0.15)
    n = len(calls)
    pump(root, 0.3)
    assert len(calls) == n
    a.monitor.stop(wait=True)


def test_playing_now_survives_a_broken_audio_list(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a._show_window()

    def broken(excluded):
        raise OSError("audio service not running")

    a._playing_source = broken
    a.PLAYING_POLL_SECONDS = 0.05
    a.tabs.invoke("exclude")
    pump(root, 0.3)
    assert _chips(a) == ["nothing right now"]
    a._stop_playing_now()
    a.monitor.stop(wait=True)


# ── no flashes: stacked tabs, invisible redraw when reopening ────────────────

def test_tabs_are_stacked_and_switching_only_raises(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a._show_window()
    pump(root, 0.1)
    tabs = (a.tab_main, a.tab_exclude, a.tab_advanced)
    assert all(t.winfo_manager() == "grid" for t in tabs), "all tabs stay laid out"
    for name in ("exclude", "advanced", "main"):
        a.tabs.invoke(name)
        pump(root, 0.05)
        assert all(t.winfo_ismapped() for t in tabs), "nothing is hidden and re-shown"
    assert a.mode_pill.label_of("single") == "Single"
    a.monitor.stop(wait=True)


def test_scrolling_names_pause_on_other_tabs(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a._show_window()
    a.tabs.invoke("advanced")
    assert a.np_title._suspended and a.mp3_entry._suspended
    a.tabs.invoke("main")
    assert not a.np_title._suspended and not a.mp3_entry._suspended
    a.monitor.stop(wait=True)


def test_reopening_draws_before_showing(appmod, root, cfg_path, fast_timing, monkeypatch):
    a = make_app(appmod, root)
    alphas = []
    monkeypatch.setattr(a, "_set_alpha", alphas.append)
    root.withdraw()
    a._show_window()
    assert root.state() != "withdrawn"
    assert alphas == [0.0], "shown invisible first"
    pump(root, (a.REVEAL_MS + 60) / 1000)
    assert alphas == [0.0, a.OPAQUE], "then made visible once drawn"
    a._show_window()                                  # already shown: no blink
    assert alphas == [0.0, a.OPAQUE]
    a.monitor.stop(wait=True)
