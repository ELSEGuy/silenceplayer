"""Smoke tests for the Tk App. Skipped where Tk or a display isn't available."""
import json
import threading
import time

import pytest

tk = pytest.importorskip("tkinter")
if not hasattr(tk, "Tk"):
    pytest.skip("tkinter placeholder module", allow_module_level=True)


from conftest import make_app, pump  # noqa: E402


def test_first_run_keeps_settings_visible(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    assert a.tab_btn_main.cget("text") == "Player"
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
    assert a.startup_btn.cget("text") == "Disabled" and not a.startup_var.get()
    a._toggle_startup_choice()
    assert a.startup_btn.cget("text") == "Enabled"
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
    assert a.startup_var.get() and a.startup_btn.cget("text") == "Enabled"
    a.monitor.stop(wait=True)


def test_start_with_windows_failure_is_reported_and_reverted(appmod, root, cfg_path,
                                                              fake_registry, fast_timing):
    a = make_app(appmod, root)
    a._toggle_startup_choice()
    fake_registry.fail = True
    a._save_settings()
    pump(root, 0.1)                       # status arrives via the UI queue
    assert "Start with Windows could not be changed" in a.status_var.get()
    assert not a.startup_var.get() and a.startup_btn.cget("text") == "Disabled"
    assert cfg_path.exists(), "the other settings are still saved"
    a.monitor.stop(wait=True)


def test_start_with_windows_unavailable_off_windows(appmod, root, cfg_path, monkeypatch,
                                                    fast_timing):
    def no_winreg():
        raise ImportError("winreg")

    monkeypatch.setattr(appmod, "_WinRegistry", no_winreg)
    a = make_app(appmod, root)
    assert a.startup_btn.cget("state") == "disabled"
    a._toggle_startup_choice()
    assert not a.startup_var.get()
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
    assert a.btn_single.cget("state") == "normal"        # the current mode stays lit
    assert a.btn_playlist.cget("state") == "disabled"
    a.btn_playlist.event_generate("<Button-1>")
    pump(root, 0.05)
    assert a.status_var.get() == "Stop Monitoring to change the mode."
    a.btn_single.invoke()                        # clicking the current mode: nothing
    assert a.config["mode"] == "single"

    a._toggle_monitoring()                       # Stop Monitoring
    assert a.btn_playlist.cget("state") == "normal"
    a.btn_playlist.invoke()
    assert a.config["mode"] == "playlist" and a.mode_var.get() == "playlist"
    a.btn_single.invoke()
    assert a.config["mode"] == "single"
    a.player.stop(wait=True, timeout=3)
    a.monitor.stop(wait=True)


def test_settings_layout(appmod, root, cfg_path, fast_timing):
    a = make_app(appmod, root)
    a._show_window()
    pump(root, 0.1)
    # Silence Timeout and Fade share one row
    assert a.silence_entry.master.master is a.fade_btn.master.master
    assert abs(a.silence_entry.winfo_rooty() - a.fade_btn.winfo_rooty()) < 12
    # Start with Windows lives in the Advanced tab only
    w = a.startup_btn
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
