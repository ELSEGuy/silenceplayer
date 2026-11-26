"""The Settings window (Player, Exclude Apps and Advanced tabs).
"""
import logging
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import filedialog, messagebox

import pystray

from .. import APP_VERSION
from ..config import SILENCE_MAX, SILENCE_MIN, VOLUME_MAX, VOLUME_MIN, ValidationError, _coerce_mirror_apps, enabled_mirror_apps, load_config, normalize_app_name, parse_int_field, save_config
from ..logs import log, log_throttled
from ..mirror import MirrorFix, PROCTAP_IMPORT_ERROR
from ..monitor import AudioMonitor
from ..nowplaying import describe_now_playing
from ..paths import resource_path
from ..player import AmbientPlayer
from ..sources import display_name, validate_ambient_source
from ..startup import StartupEntry
from ..ui.tray import TrayMenu, create_tray_icon, make_tray_icon
from ..ui.widgets import MarqueeLabel
from ..updates import RELEASES_URL, UpdateChecker, current_version


class App:
    WINDOW_W, WINDOW_H = 520, 760
    UI_POLL_MS = 50

    def __init__(self, root, instance=None, config_notice=None, config=None):
        self.root = root
        self.root.title("Silence Player")
        self.root.geometry(f"{self.WINDOW_W}x{self.WINDOW_H}")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.root.report_callback_exception = self._report_tk_exception

        try:
            self.root.iconphoto(True, tk.PhotoImage(file=resource_path("icon.png")))
        except Exception:
            log.warning("Could not load icon.png", exc_info=True)

        self._ui_queue   = queue.Queue()
        self._ui_alive   = True
        self._quitting   = False

        if config is None:
            config, config_notice = load_config()
        self.config      = config
        self.monitor     = AudioMonitor(self)
        self.player      = AmbientPlayer(self)
        self.mirror_fix  = None
        self._mirror_names = enabled_mirror_apps(config)
        self._dismissed_suspects = set()
        self._notified_suspects = set()
        self._suggested  = []
        self.monitoring  = False
        self._status     = "Ready."
        self.tray        = None
        self.instance    = instance
        self._startup    = StartupEntry()
        try:
            self._startup.repair_path()
        except Exception:
            log.warning("Could not check the Start with Windows entry", exc_info=True)
        self._version    = current_version()
        if self._version != APP_VERSION:
            log.warning("Pretending to be version %s to test the update notice", self._version)
        self._now = None                # last NowPlaying shown
        self._tray_menu = None          # created with the UI
        self._tray_tip = None
        self._update_info  = None       # a newer release, while one is known
        self._update_later = set()      # tags the user said "Later" to (this session)
        self._update_notified = set()
        self.updater = UpdateChecker(
            self._version,
            on_result=lambda info, err, manual: self.call_in_ui(
                self._on_update_result, info, err, manual),
            enabled=lambda: self.config.get("check_updates", True) is not False)

        self._build_ui()
        self.root.after(self.UI_POLL_MS, self._drain_ui_queue)
        threading.Thread(target=self._build_tray, daemon=True, name="Tray").start()
        self.root.after(0, self._refresh_now_playing)
        self.updater.start()
        if instance is not None:
            instance.listen(lambda: self.call_in_ui(self._show_window))

        problem = validate_ambient_source(self.config)
        if problem is None:
            self._start_monitoring()
            if config_notice:
                self.set_status(config_notice)
            else:
                self.root.after(100, self.root.withdraw)
        else:
            # First run (or the saved file/folder is gone): keep Settings open.
            log.info("No playable ambient source configured; showing Settings (%s)",
                     problem.replace("\n", " "))
            first_run = not self.config.get("mp3_path") and not self.config.get("playlist_folder")
            if first_run:
                msg = ("Welcome! Choose an ambient sound file (or a playlist folder), "
                       "then click Start Monitoring.")
            else:
                msg = problem.replace("\n", " ") + " Choose a new one, then click Start Monitoring."
            self.set_status(config_notice + " " + msg if config_notice else msg)
            self._show_window()

    # ── thread-safe UI access ──

    def call_in_ui(self, fn, *args):
        """Run fn(*args) on the Tk thread. Safe to call from any thread."""
        if self._ui_alive:
            self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        if not self._ui_alive:
            return
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    log.exception("UI callback failed")
        except queue.Empty:
            pass
        try:
            self.root.after(self.UI_POLL_MS, self._drain_ui_queue)
        except Exception:
            pass

    def _report_tk_exception(self, exc, val, tb):
        log.error("Unhandled Tk callback error", exc_info=(exc, val, tb))

    def set_status(self, msg):
        if msg == self._status:
            return
        self._status = msg
        self.call_in_ui(self.status_var.set, msg)

    # ── config persistence ──
    #
    # Changes apply to the running app immediately, but config.json is only
    # written when the user clicks "Save Settings" (see _save_settings).
    # Closing, quitting or starting monitoring never saves.

    def _save_now(self):
        return save_config(self.config)

    # ── Mirroring Fix ──

    def _apply_mirror_state(self):
        """Start or stop the Mirroring Fix to match the config."""
        self._mirror_names = enabled_mirror_apps(self.config)
        if self.config.get("mirror_fix_enabled", True) and PROCTAP_IMPORT_ERROR is None:
            if self.mirror_fix is None:
                self.mirror_fix = MirrorFix(apps_source=lambda: self._mirror_names)
                self.mirror_fix.start()
        else:
            if PROCTAP_IMPORT_ERROR is not None and self.config.get("mirror_fix_enabled", True):
                log_throttled("mirror_unavailable", logging.WARNING,
                              "Mirroring Fix unavailable: %s", PROCTAP_IMPORT_ERROR,
                              interval=3600)
            self._stop_mirror_fix()

    def _stop_mirror_fix(self, wait=False):
        if self.mirror_fix is not None:
            fix = self.mirror_fix
            self.mirror_fix = None  # set None first so monitor stops using it
            if wait:
                fix.stop()
            else:
                threading.Thread(target=fix.stop, daemon=True, name="MirrorStop").start()

    def _set_mirror_app(self, name, enabled=True):
        """Add `name` to the mirroring list (or change its toggle)."""
        apps = _coerce_mirror_apps(self.config.get("mirror_apps", []))
        for a in apps:
            if a["name"] == name:
                a["enabled"] = enabled
                break
        else:
            apps.append({"name": name, "enabled": enabled})
        self.config["mirror_apps"] = apps

    def on_suspected_mirror(self, names):
        """Called from the monitor thread."""
        self.call_in_ui(self._show_mirror_suggestion, list(names))

    def _show_mirror_suggestion(self, names):
        excluded = set(self.config.get("excluded_apps", []))
        names = [n for n in names
                 if n not in self._dismissed_suspects and n not in self._mirror_names
                 and n not in excluded]
        if not names:
            return
        self._suggested = names
        label = ", ".join(names)
        self.suggest_label.config(
            text=f"{label} started playing right after your ambient sound. "
                 f"It may only be reacting to it (mirroring).")
        self.suggest_btn.config(
            text="Treat as mirroring app" if PROCTAP_IMPORT_ERROR is None else "Exclude it")
        if not self._bar_shown(self.suggest_row):
            self.suggest_row.pack(fill="x", before=self.status_label)
            self._fit_window()
        new = [n for n in names if n not in self._notified_suspects]
        if new and self.tray is not None and self.root.state() == "withdrawn":
            self._notified_suspects.update(new)
            try:
                self.tray.notify(f"{', '.join(new)} may be mirroring your ambient sound. "
                                 f"Open Settings to fix it in one click.", "Silence Player")
            except Exception:
                log.debug("Tray notification failed", exc_info=True)

    def _hide_mirror_suggestion(self):
        if self._bar_shown(self.suggest_row):
            self.suggest_row.pack_forget()
            self._fit_window()
        self._suggested = []

    # ── bars above the status line ──

    @staticmethod
    def _bar_shown(bar):
        # winfo_ismapped() is False while the window is hidden in the tray.
        return bar.winfo_manager() == "pack"

    def _fit_window(self):
        """Grow the (fixed-size) window so shown bars don't cover the settings."""
        self.root.update_idletasks()
        extra = sum(bar.winfo_reqheight() for bar in (self.update_row, self.suggest_row)
                    if self._bar_shown(bar))
        self.root.geometry(f"{self.WINDOW_W}x{self.WINDOW_H + extra}")

    # ── update notice ──

    def _on_update_result(self, info, error, manual):
        if info is not None:
            self._update_info = info
            if manual or info.tag not in self._update_later:
                self._show_update_bar(info)
        elif error is None:
            # Up to date (or the newer release was skipped / withdrawn).
            self._update_info = None
            self._hide_update_bar()
            if manual:
                self.set_status(f"You have the latest version (v{self._version}).")
        elif manual:
            self.set_status("Couldn't check for updates. Check your internet connection "
                            "and try again.")
        self._refresh_update_status()
        self._refresh_tray_menu()

    def _show_update_bar(self, info):
        self.update_label.config(
            text=f"Silence Player v{info.version} is available. You have v{self._version}.")
        if not self._bar_shown(self.update_row):
            self.update_row.pack(fill="x", before=self.status_label)
            self._fit_window()
        if (info.tag not in self._update_notified and self.tray is not None
                and self.root.state() == "withdrawn"):
            self._update_notified.add(info.tag)
            try:
                self.tray.notify(f"Version {info.version} is available. Right-click the "
                                 f"tray icon to download it.", "Silence Player update")
            except Exception:
                log.debug("Tray notification failed", exc_info=True)

    def _hide_update_bar(self):
        if self._bar_shown(self.update_row):
            self.update_row.pack_forget()
            self._fit_window()

    def _open_update(self):
        info = self._update_info
        url = info.url if info is not None else RELEASES_URL
        log.info("Opening the release page: %s", url)
        try:
            webbrowser.open(url)
            self.set_status("Opened the download page in your browser.")
        except Exception:
            log.warning("Could not open the browser", exc_info=True)
            self.set_status(f"Couldn't open your browser. The download is at {url}")
        self._hide_update_bar()

    def _skip_update(self):
        info = self._update_info
        if info is not None:
            self.updater.skip(info.tag)
            self.set_status(f"OK, you won't be reminded about v{info.version}. "
                            f"Advanced > Check now still shows it.")
        self._update_info = None
        self._hide_update_bar()
        self._refresh_update_status()
        self._refresh_tray_menu()

    def _update_later_clicked(self):
        if self._update_info is not None:
            self._update_later.add(self._update_info.tag)
        self._hide_update_bar()

    def _check_updates_now(self):
        self.update_status.config(text="Checking…", fg=self._DIM)
        self.updater.check_now()

    def _refresh_update_status(self):
        on = self.config.get("check_updates", True) is not False
        self.update_toggle.config(bg=self._GOOD if on else "#313244",
                                  fg="#1e1e2e" if on else "#6c7086",
                                  text="Enabled" if on else "Disabled")
        u = self.updater
        if self._update_info is not None:
            text, color = f"● v{self._update_info.version} is available", self._GOOD
        elif u.last_checked is None:
            text = ("● Checks GitHub for new versions" if on
                    else "● Off — Silence Player won't check GitHub")
            color = self._DIM
        elif u.last_error is not None:
            text, color = "● Couldn't check (no internet?)", "#f9e2af"
        else:
            when = time.strftime("%H:%M", time.localtime(u.last_checked))
            text, color = f"● Up to date (checked at {when})", self._DIM
        if self._version != APP_VERSION:
            text += f" · test mode: acting as v{self._version}"
        self.update_status.config(text=text, fg=color)

    def _dismiss_mirror_suggestion(self):
        self._dismissed_suspects.update(self._suggested)
        self._hide_mirror_suggestion()

    def _accept_mirror_suggestion(self):
        names = list(self._suggested)
        self._hide_mirror_suggestion()
        if not names:
            return
        label = ", ".join(names)
        if PROCTAP_IMPORT_ERROR is None:
            for n in names:
                self._set_mirror_app(n, True)
            if not self.config.get("mirror_fix_enabled", True):
                self.config["mirror_fix_enabled"] = True
                self.mirror_fix_var.set(True)
            self._apply_mirror_state()
            self._refresh_mirror_ui()
            log.info("User added %s to the Mirroring Fix", label)
            self.set_status(f"{label} added to the Mirroring Fix (ignored unless it really "
                            f"plays sound). Click Save Settings to keep it.")
        else:
            apps = list(self.config.get("excluded_apps", []))
            for n in names:
                if n not in apps:
                    apps.append(n)
                    self.exclude_listbox.insert(tk.END, n)
            self.config["excluded_apps"] = apps
            log.info("User excluded %s", label)
            self.set_status(f"{label} added to Exclude Apps. Click Save Settings to keep it.")

    # ── tray ──

    def _build_tray(self):
        try:
            icon_image = create_tray_icon()
            menu = pystray.Menu(
                pystray.MenuItem(lambda item: self._now.menu if self._now else "Silence Player",
                                 lambda icon, item: None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Settings", self._tray_open, default=True),
                pystray.MenuItem(
                    lambda item: (f"Download v{self._update_info.version}"
                                  if self._update_info is not None else "Download update"),
                    self._tray_update,
                    visible=lambda item: self._update_info is not None),
                pystray.MenuItem(
                    lambda item: "Stop Monitoring" if self.monitoring else "Start Monitoring",
                    self._tray_toggle),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._tray_quit),
            )
            tip = self._now.tooltip if self._now else "Silence Player"
            self.tray = make_tray_icon("SilencePlayer", icon_image, tip, menu,
                                       on_right_click=lambda: self.call_in_ui(self._open_tray_menu))
            self._tray_tip = tip
            self.tray.run()
        except Exception:
            log.exception("System tray icon failed")
            # Without a tray icon the user could never reopen a hidden window.
            self.call_in_ui(self._show_window)

    def _refresh_tray_menu(self):
        tray = self.tray
        if tray is not None:
            try:
                tray.update_menu()
            except Exception:
                log.debug("Tray menu refresh failed", exc_info=True)

    def _tray_open(self, icon=None, item=None):
        self.call_in_ui(self._show_window)

    def _open_tray_menu(self, at=None):
        if self._tray_menu is None:
            self._tray_menu = TrayMenu(self.root)
        now = self._now or describe_now_playing(self.monitoring, self.monitor.info,
                                                self.player.track_info(), self.config)
        items = [("Open Settings", self._show_window)]
        if self._update_info is not None:
            items.append((f"Download v{self._update_info.version}", self._open_update))
        items.append(("Stop Monitoring" if self.monitoring else "Start Monitoring",
                      self._toggle_monitoring))
        items += [None, ("Quit", self._quit)]
        self._tray_menu.show(now.menu_head, now.menu_line, now.kind, items, at=at)

    def _tray_update(self, icon=None, item=None):
        self.call_in_ui(self._open_update)

    def _tray_toggle(self, icon=None, item=None):
        self.call_in_ui(self._toggle_monitoring)

    def _tray_quit(self, icon=None, item=None):
        self.call_in_ui(self._quit)

    # ── window ──

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_window_close(self):
        """The X button hides to tray. Nothing is saved (see _save_settings)."""
        if self._commit_inputs():
            self._revert_invalid_fields()   # invalid typed values never applied
        self.root.withdraw()

    def _quit(self):
        if self._quitting:
            return
        self._quitting = True
        log.info("Quit requested")
        self.monitoring = False
        self.root.withdraw()

        self.updater.stop()
        if self._tray_menu is not None:
            self._tray_menu.close()

        def shutdown():
            try:
                self.monitor.stop(wait=True)
                self.player.shutdown()
                self._stop_mirror_fix(wait=True)
            except Exception:
                log.exception("Error during shutdown")
            finally:
                if self.tray:
                    try:
                        self.tray.stop()
                    except Exception:
                        log.warning("Stopping tray icon failed", exc_info=True)
                self.call_in_ui(self._finish_quit)

        threading.Thread(target=shutdown, daemon=True, name="Shutdown").start()

    def _finish_quit(self):
        self._ui_alive = False
        try:
            self.root.destroy()
        except Exception:
            pass

    # ── UI construction ──

    def _build_ui(self):
        BG     = "#1e1e2e"
        CARD   = "#2a2a3e"
        FG     = "#cdd6f4"
        ACC    = "#89b4fa"
        BTN_BG = "#313244"
        DIM    = "#a6adc8"
        self._FG = FG
        self._ERR = "#f38ba8"

        def label(parent, text, size=10, bold=False, color=FG):
            return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                            font=("Segoe UI", size, "bold" if bold else "normal"))

        def entry(parent, textvariable, width=24):
            return tk.Entry(parent, textvariable=textvariable, width=width,
                            bg=BTN_BG, fg=FG, insertbackground=FG,
                            relief="flat", font=("Segoe UI", 10), bd=6)

        def button(parent, text, command, color=ACC):
            return tk.Button(parent, text=text, command=command,
                             bg=BTN_BG, fg=color, relief="flat",
                             font=("Segoe UI", 10, "bold"),
                             padx=8, pady=5, cursor="hand2",
                             activebackground="#45475a", activeforeground=color)

        label(self.root, "Silence Player", size=16, bold=True, color=ACC).pack(pady=(16, 2))
        label(self.root, "Plays ambient sound when your PC is silent", size=9, color=DIM).pack()

        tab_bar = tk.Frame(self.root, bg=BG)
        tab_bar.pack(pady=(10, 0))

        self.tab_main     = tk.Frame(self.root, bg=BG)
        self.tab_exclude  = tk.Frame(self.root, bg=BG)
        self.tab_advanced = tk.Frame(self.root, bg=BG)

        def switch_tab(tab):
            self.tab_main.pack_forget()
            self.tab_exclude.pack_forget()
            self.tab_advanced.pack_forget()
            self.tab_btn_main.config(bg=BTN_BG, fg=DIM)
            self.tab_btn_exclude.config(bg=BTN_BG, fg=DIM)
            self.tab_btn_advanced.config(bg=BTN_BG, fg=DIM)
            if tab == "main":
                self.tab_main.pack(fill="both", expand=True)
                self.tab_btn_main.config(bg=ACC, fg="#1e1e2e")
            elif tab == "exclude":
                self.tab_exclude.pack(fill="both", expand=True)
                self.tab_btn_exclude.config(bg=ACC, fg="#1e1e2e")
            else:
                self.tab_advanced.pack(fill="both", expand=True)
                self.tab_btn_advanced.config(bg=ACC, fg="#1e1e2e")

        self.tab_btn_main = tk.Button(tab_bar, text="Player",
                                      command=lambda: switch_tab("main"),
                                      relief="flat", font=("Segoe UI", 10, "bold"),
                                      padx=16, pady=5, cursor="hand2",
                                      bg=ACC, fg="#1e1e2e")
        self.tab_btn_main.pack(side="left", padx=3)

        self.tab_btn_exclude = tk.Button(tab_bar, text="Exclude Apps",
                                         command=lambda: switch_tab("exclude"),
                                         relief="flat", font=("Segoe UI", 10, "bold"),
                                         padx=16, pady=5, cursor="hand2",
                                         bg=BTN_BG, fg=DIM)
        self.tab_btn_exclude.pack(side="left", padx=3)

        self.tab_btn_advanced = tk.Button(tab_bar, text="Advanced",
                                          command=lambda: switch_tab("advanced"),
                                          relief="flat", font=("Segoe UI", 10, "bold"),
                                          padx=16, pady=5, cursor="hand2",
                                          bg=BTN_BG, fg=DIM)
        self.tab_btn_advanced.pack(side="left", padx=3)

        self._build_main_tab(self.tab_main, BG, CARD, FG, ACC, BTN_BG, DIM, label, entry, button)
        self._build_exclude_tab(self.tab_exclude, BG, CARD, FG, ACC, BTN_BG, DIM)
        self._build_advanced_tab(self.tab_advanced, BG, CARD, FG, ACC, BTN_BG, DIM)

        self.tab_main.pack(fill="both", expand=True)

        status_frame = tk.Frame(self.root, bg="#181825")
        status_frame.pack(fill="x", side="bottom")

        # "A new version is available" (hidden until needed).
        self.update_row = tk.Frame(status_frame, bg="#313244", padx=10, pady=6)
        self.update_label = tk.Label(self.update_row, bg="#313244", fg=FG,
                                     font=("Segoe UI", 9), wraplength=490, justify="left")
        self.update_label.pack(anchor="w")
        update_buttons = tk.Frame(self.update_row, bg="#313244")
        update_buttons.pack(anchor="w", pady=(4, 0))
        self.update_btn = tk.Button(update_buttons, text="Download", command=self._open_update,
                                    bg="#a6e3a1", fg="#1e1e2e", relief="flat",
                                    font=("Segoe UI", 9, "bold"), padx=10, pady=3,
                                    cursor="hand2")
        self.update_btn.pack(side="left")
        for text, cmd in (("Skip this version", self._skip_update),
                          ("Later", self._update_later_clicked)):
            tk.Button(update_buttons, text=text, command=cmd,
                      bg=BTN_BG, fg=DIM, relief="flat", font=("Segoe UI", 9, "bold"),
                      padx=10, pady=3, cursor="hand2").pack(side="left", padx=(6, 0))

        # One-click fix for an app that seems to mirror our sound (hidden until needed).
        self.suggest_row = tk.Frame(status_frame, bg="#313244", padx=10, pady=6)
        self.suggest_label = tk.Label(self.suggest_row, bg="#313244", fg=FG,
                                      font=("Segoe UI", 9), wraplength=490, justify="left")
        self.suggest_label.pack(anchor="w")
        suggest_buttons = tk.Frame(self.suggest_row, bg="#313244")
        suggest_buttons.pack(anchor="w", pady=(4, 0))
        self.suggest_btn = tk.Button(suggest_buttons, text="Treat as mirroring app",
                                     command=self._accept_mirror_suggestion,
                                     bg=ACC, fg="#1e1e2e", relief="flat",
                                     font=("Segoe UI", 9, "bold"), padx=10, pady=3,
                                     cursor="hand2")
        self.suggest_btn.pack(side="left")
        tk.Button(suggest_buttons, text="Dismiss", command=self._dismiss_mirror_suggestion,
                  bg=BTN_BG, fg=DIM, relief="flat", font=("Segoe UI", 9, "bold"),
                  padx=10, pady=3, cursor="hand2").pack(side="left", padx=(6, 0))

        # Version number, tucked away bottom-left (handy for bug reports). The
        # status text keeps clear of it on both sides, however long it is.
        version_text = f"v{APP_VERSION}"
        try:
            version_w = tkfont.Font(root=self.root, family="Segoe UI", size=8).measure(version_text)
        except Exception:
            version_w = 7 * len(version_text)
        side_pad = max(52, version_w + 20)
        self.status_var = tk.StringVar(value=self._status)
        self.status_label = tk.Label(status_frame, textvariable=self.status_var,
                                     bg="#181825", fg=DIM, font=("Segoe UI", 9), pady=8,
                                     padx=side_pad,
                                     wraplength=max(240, self.WINDOW_W - 2 * side_pad - 16))
        self.status_label.pack(fill="x")
        tk.Label(status_frame, text=version_text, bg="#181825", fg="#585b70",
                 font=("Segoe UI", 8)).place(in_=self.status_label, relx=0, rely=0.5,
                                             x=10, anchor="w")

    def _build_main_tab(self, parent, BG, CARD, FG, ACC, BTN_BG, DIM, label, entry, button):
        self._build_now_playing(parent, CARD, FG, DIM)

        mode_frame = tk.Frame(parent, bg=BG)
        mode_frame.pack(pady=(8, 0))
        label(mode_frame, "Mode:", bold=True).pack(side="left", padx=(0, 8))

        self.mode_var = tk.StringVar(value=self.config.get("mode", "single"))

        def mode_btn(text, val):
            def on_click():
                self.mode_var.set(val)
                self.config["mode"] = val
                _refresh_mode()
            b = tk.Button(mode_frame, text=text, command=on_click,
                          relief="flat", font=("Segoe UI", 10, "bold"),
                          padx=12, pady=5, cursor="hand2")
            b.pack(side="left", padx=3)
            return b

        self.btn_single   = mode_btn("Single File", "single")
        self.btn_playlist = mode_btn("Playlist",    "playlist")

        def _refresh_mode():
            m = self.mode_var.get()
            if m == "single":
                self.btn_single.config(bg=ACC, fg="#1e1e2e")
                self.btn_playlist.config(bg=BTN_BG, fg=DIM)
                single_card.pack(fill="x", padx=24, pady=(8, 4))
                playlist_card.pack_forget()
            else:
                self.btn_playlist.config(bg=ACC, fg="#1e1e2e")
                self.btn_single.config(bg=BTN_BG, fg=DIM)
                playlist_card.pack(fill="x", padx=24, pady=(8, 4))
                single_card.pack_forget()

        single_card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        label(single_card, "Ambient Sound File (MP3, OPUS, M4A, FLAC, MP4)", bold=True).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.mp3_var = tk.StringVar()
        self.mp3_entry = self._path_field(single_card, self.mp3_var, self._browse_mp3, BTN_BG, FG)
        self.mp3_entry.grid(row=1, column=0, sticky="w")
        button(single_card, "Browse", self._browse_mp3).grid(row=1, column=1, padx=(6, 10))

        self.single_loop_var = tk.StringVar(value=self.config.get("single_loop_mode", "loop"))
        single_toggle_frame = tk.Frame(single_card, bg=CARD)
        single_toggle_frame.grid(row=1, column=2, sticky="w")

        def make_single_toggle(text, val):
            def on_click():
                self.single_loop_var.set(val)
                self.config["single_loop_mode"] = val
                _refresh_single_toggle()
            b = tk.Button(single_toggle_frame, text=text, command=on_click,
                          relief="flat", font=("Segoe UI", 9, "bold"),
                          padx=8, pady=4, cursor="hand2")
            b.pack(side="left", padx=2)
            return b

        self.s_btn_loop = make_single_toggle("Loop", "loop")
        self.s_btn_stop = make_single_toggle("Stop", "stop")

        def _refresh_single_toggle():
            v = self.single_loop_var.get()
            self.s_btn_loop.config(bg=ACC if v == "loop" else BTN_BG,
                                   fg="#1e1e2e" if v == "loop" else DIM)
            self.s_btn_stop.config(bg="#f38ba8" if v == "stop" else BTN_BG,
                                   fg="#1e1e2e" if v == "stop" else DIM)

        _refresh_single_toggle()

        playlist_card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        label(playlist_card, "Ambient Sound Playlist (Folder)", bold=True).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.playlist_var = tk.StringVar()
        self.playlist_entry = self._path_field(playlist_card, self.playlist_var,
                                               self._browse_playlist, BTN_BG, FG)
        self.playlist_entry.grid(row=1, column=0, sticky="w")
        button(playlist_card, "Browse", self._browse_playlist).grid(row=1, column=1, padx=(6, 10))

        self.playlist_loop_var = tk.StringVar(value=self.config.get("playlist_loop_mode", "loop_playlist"))
        playlist_toggle_frame = tk.Frame(playlist_card, bg=CARD)
        playlist_toggle_frame.grid(row=1, column=2, sticky="w")

        def make_playlist_toggle(text, val):
            def on_click():
                self.playlist_loop_var.set(val)
                self.config["playlist_loop_mode"] = val
                _refresh_playlist_toggle()
            b = tk.Button(playlist_toggle_frame, text=text, command=on_click,
                          relief="flat", font=("Segoe UI", 9, "bold"),
                          padx=8, pady=4, cursor="hand2")
            b.pack(side="left", padx=2)
            return b

        self.p_btn_loop_song = make_playlist_toggle("Loop Song",     "loop_song")
        self.p_btn_stop      = make_playlist_toggle("Stop",          "stop")
        self.p_btn_loop_pl   = make_playlist_toggle("Loop Playlist", "loop_playlist")

        def _refresh_playlist_toggle():
            v = self.playlist_loop_var.get()
            self.p_btn_loop_song.config(bg=ACC if v == "loop_song" else BTN_BG,
                                        fg="#1e1e2e" if v == "loop_song" else DIM)
            self.p_btn_stop.config(bg="#f38ba8" if v == "stop" else BTN_BG,
                                   fg="#1e1e2e" if v == "stop" else DIM)
            self.p_btn_loop_pl.config(bg="#a6e3a1" if v == "loop_playlist" else BTN_BG,
                                      fg="#1e1e2e" if v == "loop_playlist" else DIM)

        _refresh_playlist_toggle()

        shared_card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        shared_card.pack(fill="x", padx=24, pady=4)

        self.silence_var = tk.StringVar(value=str(self.config["silence_seconds"]))
        self.vol_var     = tk.StringVar(value=str(self.config["max_volume"]))

        label(shared_card, "Silence Timeout (seconds)", bold=True).grid(row=0, column=0, sticky="w")
        label(shared_card, "Max Volume (0-100)",         bold=True).grid(row=0, column=1, sticky="w", padx=(20, 0))
        self.silence_entry = entry(shared_card, self.silence_var, width=12)
        self.silence_entry.grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.vol_entry = entry(shared_card, self.vol_var, width=12)
        self.vol_entry.grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(2, 8))

        # Typed values are committed when the field loses focus or Enter is pressed
        # (and when the window is closed), so "settings save automatically" holds.
        for widget in (self.silence_entry, self.vol_entry):
            widget.bind("<FocusOut>", lambda e: self._commit_inputs_quietly())
            widget.bind("<Return>",   lambda e: self._commit_inputs_quietly())

        fade_row = tk.Frame(shared_card, bg=CARD)
        fade_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        label(fade_row, "Fade-in / Fade-out", bold=True, color=FG).pack(side="left", padx=(0, 12))

        self.fade_enabled_var = tk.BooleanVar(value=self.config.get("fade_enabled", True))

        def toggle_fade():
            v = self.fade_enabled_var.get()
            self.config["fade_enabled"] = v
            fade_btn.config(
                bg="#a6e3a1" if v else BTN_BG,
                fg="#1e1e2e" if v else DIM,
                text="Enabled" if v else "Disabled")

        fade_btn = tk.Button(fade_row,
                             command=lambda: [
                                 self.fade_enabled_var.set(not self.fade_enabled_var.get()),
                                 toggle_fade()],
                             relief="flat", font=("Segoe UI", 9, "bold"),
                             padx=12, pady=4, cursor="hand2")
        fade_btn.pack(side="left")
        toggle_fade()

        startup_row = tk.Frame(shared_card, bg=CARD)
        startup_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        label(startup_row, "Start with Windows", bold=True, color=FG).pack(side="left", padx=(0, 12))

        self.startup_var = tk.BooleanVar(value=self._startup.is_enabled())
        self.startup_btn = tk.Button(startup_row, command=self._toggle_startup_choice,
                                     relief="flat", font=("Segoe UI", 9, "bold"),
                                     padx=12, pady=4, cursor="hand2")
        self.startup_btn.pack(side="left")
        self.startup_hint = label(shared_card, "", size=8, color=DIM)
        self.startup_hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._refresh_startup_ui()

        duck_card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        duck_card.pack(fill="x", padx=24, pady=4)

        duck_top = tk.Frame(duck_card, bg=CARD)
        duck_top.pack(fill="x")
        label(duck_top, "When External Audio Detected", bold=True).pack(side="left")
        self.duck_label = tk.Label(duck_top, bg=CARD, fg=ACC, font=("Segoe UI", 10, "bold"))
        self.duck_label.pack(side="right")

        label(duck_card, "0% = Stop ambient   |   1-99% = Duck volume   |   100% = Keep playing",
              size=8, color=DIM).pack(anchor="w", pady=(2, 6))

        self.duck_var = tk.IntVar(value=int(self.config.get("duck_percent", 0)))

        def on_duck_change(val):
            v = int(float(val))
            self.config["duck_percent"] = v
            if v == 0:
                self.duck_label.config(text="Stop (0%)")
            elif v == 100:
                self.duck_label.config(text="Keep Playing (100%)")
            else:
                self.duck_label.config(text=f"Duck to {v}%")

        tk.Scale(duck_card, from_=0, to=100, orient="horizontal",
                 variable=self.duck_var, command=on_duck_change,
                 bg=CARD, fg=FG, troughcolor=BTN_BG,
                 highlightthickness=0, sliderrelief="flat",
                 length=460, showvalue=False).pack(fill="x")
        on_duck_change(self.duck_var.get())

        btn_frame = tk.Frame(parent, bg=BG)
        btn_frame.pack(pady=6)
        self.start_btn = button(btn_frame, "Start Monitoring", self._toggle_monitoring, color="#a6e3a1")
        self.start_btn.pack(side="left", padx=6)
        button(btn_frame, "Save Settings", self._save_settings, color="#f9e2af").pack(side="left", padx=6)

        _refresh_mode()
        self._refresh_path_fields()

    # ── Now playing ──

    NOW_COLORS = {             # badge background, badge text, title colour
        "playing": ("#a6e3a1", "#1e1e2e", "#cdd6f4"),
        "ducked":  ("#f9e2af", "#1e1e2e", "#cdd6f4"),
        "waiting": ("#313244", "#a6adc8", "#7f849c"),
        "off":     ("#313244", "#6c7086", "#7f849c"),
    }

    def _build_now_playing(self, parent, CARD, FG, DIM):
        card = tk.Frame(parent, bg=CARD, padx=8, pady=10)
        card.pack(fill="x", padx=24, pady=(10, 0))
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=8)
        tk.Label(head, text="Now playing", bg=CARD, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.np_badge = tk.Label(head, text="Off", font=("Segoe UI", 8, "bold"), padx=7, pady=1)
        self.np_badge.pack(side="right")
        self.np_title_var = tk.StringVar(value="")
        self.np_title = MarqueeLabel(card, self.np_title_var, width_chars=48, bg=CARD, fg=FG,
                                     font=("Segoe UI", 11))
        self.np_title.pack(anchor="w", pady=(4, 0))
        self.np_detail = tk.Label(card, text="", bg=CARD, fg=DIM, font=("Segoe UI", 9),
                                  anchor="w")
        self.np_detail.pack(fill="x", padx=8)
        self.np_bar = tk.Canvas(card, height=4, bg="#313244", highlightthickness=0, bd=0)
        self.np_bar.pack(fill="x", padx=8, pady=(8, 0))
        self._np_fill = self.np_bar.create_rectangle(0, 0, 0, 4, width=0, fill="#a6e3a1")
        self.np_bar.bind("<Configure>", lambda e: self._draw_np_bar())

    def _draw_np_bar(self):
        now = self._now
        frac = now.progress if now is not None and now.progress is not None else 0.0
        color = self.NOW_COLORS.get(now.kind if now else "off")[0]
        width = self.np_bar.winfo_width()
        self.np_bar.coords(self._np_fill, 0, 0, round(width * frac), 4)
        self.np_bar.itemconfig(self._np_fill, fill=color)

    def _refresh_now_playing(self):
        if not self._ui_alive:
            return
        try:
            now = describe_now_playing(self.monitoring, self.monitor.info,
                                       self.player.track_info(), self.config)
            self._show_now_playing(now)
        except Exception:
            log_throttled("now_playing", logging.WARNING, "Now playing refresh failed",
                          exc_info=True, interval=300)
        self.root.after(self.NOW_PLAYING_MS, self._refresh_now_playing)

    NOW_PLAYING_MS = 500

    def _show_now_playing(self, now):
        old, self._now = self._now, now
        if now == old:
            return
        badge_bg, badge_fg, title_fg = self.NOW_COLORS[now.kind]
        self.np_badge.config(text=now.badge, bg=badge_bg, fg=badge_fg)
        if self.np_title_var.get() != now.title:     # writing restarts the scroll
            self.np_title_var.set(now.title)
        self.np_title.set_fg(title_fg)
        self.np_detail.config(text=now.detail)
        self._draw_np_bar()
        if self._tray_menu is not None and self._tray_menu.is_open:
            self._tray_menu.update_header(now.menu_head, now.menu_line, now.kind)
        tray = self.tray
        if tray is not None:
            if now.tooltip != self._tray_tip:
                self._tray_tip = now.tooltip
                try:
                    tray.title = now.tooltip
                except Exception:
                    log.debug("Tray tooltip update failed", exc_info=True)
            if old is None or now.menu != old.menu:
                self._refresh_tray_menu()

    # ── Start with Windows (applied when Save Settings is clicked) ──

    def _toggle_startup_choice(self):
        if not self._startup.available:
            return
        self.startup_var.set(not self.startup_var.get())
        self._refresh_startup_ui()

    def _refresh_startup_ui(self):
        status = self._startup.status()
        on = bool(self.startup_var.get())
        if status == "unavailable":
            self.startup_btn.config(text="Windows only", state="disabled", bg="#313244",
                                    fg="#585b70", cursor="arrow")
            self.startup_hint.config(text="")
            return
        self.startup_btn.config(state="normal", cursor="hand2",
                                bg="#a6e3a1" if on else "#313244",
                                fg="#1e1e2e" if on else "#6c7086",
                                text="Enabled" if on else "Disabled")
        if status == "blocked" and not on:
            hint = "Turned off in Windows (Settings > Apps > Startup). Enable it here to undo that."
        else:
            hint = "Starts in the tray and begins monitoring. Click Save Settings to apply."
        self.startup_hint.config(text=hint)

    def _apply_startup_choice(self):
        """Write the Start with Windows choice. Returns an error message or None."""
        if not self._startup.available:
            return None
        want = bool(self.startup_var.get())
        if want == self._startup.is_enabled():
            return None
        try:
            self._startup.set_enabled(want)
        except Exception:
            log.exception("Could not change Start with Windows")
            self.startup_var.set(self._startup.is_enabled())
            self._refresh_startup_ui()
            return "Settings saved, but Start with Windows could not be changed. See the log file."
        self._refresh_startup_ui()
        return None

    def _path_field(self, parent, var, on_click, bg, fg):
        """File/folder name (scrolls if too long); click to choose, hover for the full path."""
        field = MarqueeLabel(parent, var, width_chars=22, bg=bg, fg=fg, on_click=on_click)
        field.bind("<Enter>", lambda e: self._show_path_tip(field), add="+")
        field.bind("<Leave>", lambda e: self._hide_path_tip(), add="+")
        return field

    def _refresh_path_fields(self):
        file_path = self.config.get("mp3_path", "")
        folder = self.config.get("playlist_folder", "")
        self.mp3_var.set(display_name(file_path) or "Click to choose a file…")
        self.playlist_var.set(display_name(folder) or "Click to choose a folder…")
        self._path_tips = {self.mp3_entry: file_path, self.playlist_entry: folder}

    def _show_path_tip(self, field):
        self._hide_path_tip()
        full = getattr(self, "_path_tips", {}).get(field)
        if not full:
            return
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{field.winfo_rootx()}+{field.winfo_rooty() + field.winfo_height() + 4}")
        tk.Label(tip, text=os.path.normpath(full), bg="#313244", fg="#cdd6f4",
                 font=("Segoe UI", 9), padx=8, pady=4, relief="solid", bd=1).pack()
        self._path_tip = tip

    def _hide_path_tip(self):
        tip = getattr(self, "_path_tip", None)
        if tip is not None:
            try:
                tip.destroy()
            except Exception:
                pass
            self._path_tip = None

    def _build_exclude_tab(self, parent, BG, CARD, FG, ACC, BTN_BG, DIM):
        card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        card.pack(fill="both", padx=24, pady=12, expand=True)

        tk.Label(card, text="Exclude Apps", bg=CARD, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 2))
        tk.Label(card, text="Sound from these apps will be ignored by Silence Player.",
                 bg=CARD, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))

        list_frame = tk.Frame(card, bg=CARD)
        list_frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")

        self.exclude_listbox = tk.Listbox(
            list_frame, yscrollcommand=scrollbar.set,
            bg=BTN_BG, fg=FG,
            selectbackground=ACC, selectforeground="#1e1e2e",
            relief="flat", font=("Segoe UI", 10),
            height=8, borderwidth=0)
        self.exclude_listbox.pack(fill="both", expand=True)
        scrollbar.config(command=self.exclude_listbox.yview)

        for app_name in self.config.get("excluded_apps", []):
            self.exclude_listbox.insert(tk.END, app_name)

        add_frame = tk.Frame(card, bg=CARD)
        add_frame.pack(fill="x", pady=(8, 0))

        self.exclude_entry_var = tk.StringVar()
        add_entry = tk.Entry(add_frame, textvariable=self.exclude_entry_var,
                             bg=BTN_BG, fg=FG, insertbackground=FG,
                             relief="flat", font=("Segoe UI", 10), bd=6, width=22)
        add_entry.pack(side="left", fill="x", expand=True)

        tk.Label(add_frame, text="e.g. discord.exe", bg=CARD, fg=DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))

        def add_app():
            name = normalize_app_name(self.exclude_entry_var.get())
            if not name:
                return
            if name in list(self.exclude_listbox.get(0, tk.END)):
                return
            self.exclude_listbox.insert(tk.END, name)
            self.exclude_entry_var.set("")
            self._sync_excluded_apps()

        def remove_app():
            selected = self.exclude_listbox.curselection()
            if selected:
                self.exclude_listbox.delete(selected[0])
                self._sync_excluded_apps()

        btn_row = tk.Frame(card, bg=CARD)
        btn_row.pack(fill="x", pady=(6, 0))

        tk.Button(btn_row, text="Add", command=add_app,
                  bg=ACC, fg="#1e1e2e", relief="flat",
                  font=("Segoe UI", 10, "bold"),
                  padx=16, pady=5, cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Remove Selected", command=remove_app,
                  bg=BTN_BG, fg="#f38ba8", relief="flat",
                  font=("Segoe UI", 10, "bold"),
                  padx=16, pady=5, cursor="hand2").pack(side="left")

        add_entry.bind("<Return>", lambda e: add_app())

    def _build_advanced_tab(self, parent, BG, CARD, FG, ACC, BTN_BG, DIM):
        self._DIM, self._GOOD = DIM, "#a6e3a1"
        tk.Label(parent, text="Advanced Settings", bg=BG, fg=ACC,
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 2))
        tk.Label(parent, text="Fixes for apps that confuse silence detection.",
                 bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(pady=(0, 8))

        card = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        card.pack(fill="x", padx=24, pady=4)

        title_row = tk.Frame(card, bg=CARD)
        title_row.pack(fill="x")
        tk.Label(title_row, text="Mirroring Fix",
                 bg=CARD, fg=FG, font=("Segoe UI", 10, "bold")).pack(side="left")

        self.mirror_fix_var = tk.BooleanVar(value=self.config.get("mirror_fix_enabled", True))
        self.mirror_status = tk.Label(card, bg=CARD, font=("Segoe UI", 9, "bold"))

        def toggle_mirror_fix():
            self.mirror_fix_var.set(not self.mirror_fix_var.get())
            self.config["mirror_fix_enabled"] = bool(self.mirror_fix_var.get())
            self._apply_mirror_state()
            self._refresh_mirror_ui()

        self.mirror_btn = tk.Button(title_row, command=toggle_mirror_fix,
                                    relief="flat", font=("Segoe UI", 9, "bold"),
                                    padx=12, pady=4, cursor="hand2")
        self.mirror_btn.pack(side="right")

        tk.Label(card,
                 text="Some apps listen to your PC's sound and show a fake level in\n"
                      "the Windows mixer (Discord while streaming, SignalRGB,\n"
                      "Wallpaper Engine). For the apps below, Silence Player checks\n"
                      "whether they really make sound, and ignores them when\n"
                      "they're only reacting to your ambient sound.",
                 bg=CARD, fg=DIM, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", pady=(6, 6))

        list_frame = tk.Frame(card, bg=CARD)
        list_frame.pack(fill="x")
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.mirror_listbox = tk.Listbox(
            list_frame, yscrollcommand=scrollbar.set,
            bg=BTN_BG, fg=FG, selectbackground=ACC, selectforeground="#1e1e2e",
            relief="flat", font=("Segoe UI", 10), height=5, borderwidth=0,
            activestyle="none")
        self.mirror_listbox.pack(fill="x", expand=True)
        scrollbar.config(command=self.mirror_listbox.yview)

        def selected_name():
            sel = self.mirror_listbox.curselection()
            if not sel:
                return None
            apps = _coerce_mirror_apps(self.config.get("mirror_apps", []))
            return apps[sel[0]]["name"] if sel[0] < len(apps) else None

        def toggle_selected(event=None):
            name = selected_name()
            if name is None:
                return
            apps = _coerce_mirror_apps(self.config.get("mirror_apps", []))
            current = next(a["enabled"] for a in apps if a["name"] == name)
            self._set_mirror_app(name, not current)
            self._apply_mirror_state()
            self._refresh_mirror_ui(select=name)

        def remove_selected():
            name = selected_name()
            if name is None:
                return
            self.config["mirror_apps"] = [a for a in _coerce_mirror_apps(
                self.config.get("mirror_apps", [])) if a["name"] != name]
            self._apply_mirror_state()
            self._refresh_mirror_ui()

        def add_app(event=None):
            name = normalize_app_name(self.mirror_entry_var.get())
            if not name:
                return
            self._set_mirror_app(name, True)
            self.mirror_entry_var.set("")
            self._apply_mirror_state()
            self._refresh_mirror_ui(select=name)

        self.mirror_listbox.bind("<Double-Button-1>", toggle_selected)

        add_row = tk.Frame(card, bg=CARD)
        add_row.pack(fill="x", pady=(6, 0))
        self.mirror_entry_var = tk.StringVar()
        add_entry = tk.Entry(add_row, textvariable=self.mirror_entry_var,
                             bg=BTN_BG, fg=FG, insertbackground=FG,
                             relief="flat", font=("Segoe UI", 10), bd=6, width=18)
        add_entry.pack(side="left")
        add_entry.bind("<Return>", add_app)
        for text, cmd, color in (("Add", add_app, ACC),
                                 ("On / Off", toggle_selected, FG),
                                 ("Remove", remove_selected, "#f38ba8")):
            tk.Button(add_row, text=text, command=cmd, bg=BTN_BG, fg=color,
                      relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                      cursor="hand2").pack(side="left", padx=(6, 0))

        self.mirror_status.pack(anchor="w", pady=(8, 0))

        self._refresh_mirror_ui()
        self._apply_mirror_state()

        # Update check
        update_card = tk.Frame(parent, bg=CARD, padx=16, pady=10)
        update_card.pack(fill="x", padx=24, pady=4)
        top = tk.Frame(update_card, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text="Check for updates", bg=CARD, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")

        def toggle_updates():
            on = not (self.config.get("check_updates", True) is not False)
            self.config["check_updates"] = on
            if on and self.updater.last_checked is None:
                self.updater.check_now()
            self._refresh_update_status()

        self.update_toggle = tk.Button(top, command=toggle_updates, relief="flat",
                                       font=("Segoe UI", 9, "bold"), padx=12, pady=4,
                                       cursor="hand2")
        self.update_toggle.pack(side="right")
        tk.Button(top, text="Check now", command=self._check_updates_now, bg=BTN_BG, fg=ACC,
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                  cursor="hand2").pack(side="right", padx=(0, 6))
        self.update_status = tk.Label(update_card, bg=CARD, fg=DIM, font=("Segoe UI", 9),
                                      anchor="w")
        self.update_status.pack(anchor="w", fill="x", pady=(6, 0))
        self._refresh_update_status()

    def _refresh_mirror_ui(self, select=None):
        on = bool(self.mirror_fix_var.get())
        self.mirror_btn.config(bg=self._GOOD if on else "#313244",
                               fg="#1e1e2e" if on else self._DIM,
                               text="Enabled" if on else "Disabled")
        apps = _coerce_mirror_apps(self.config.get("mirror_apps", []))
        self.mirror_listbox.delete(0, tk.END)
        for i, a in enumerate(apps):
            self.mirror_listbox.insert(tk.END, f"{'●' if a['enabled'] else '○'}  {a['name']}"
                                       + ("" if a["enabled"] else "   (off)"))
            if a["name"] == select:
                self.mirror_listbox.selection_set(i)
        count = sum(1 for a in apps if a["enabled"])
        if on and PROCTAP_IMPORT_ERROR is not None:
            self.mirror_status.config(
                text="● Unavailable — ProcTap could not be loaded (see log)", fg="#f38ba8")
        elif on:
            self.mirror_status.config(
                text=f"● Active — checking {count} app{'s' if count != 1 else ''}",
                fg=self._GOOD)
        else:
            self.mirror_status.config(text="● Off", fg=self._DIM)

    def _sync_excluded_apps(self):
        self.config["excluded_apps"] = list(self.exclude_listbox.get(0, tk.END))

    def _browse_mp3(self):
        path = filedialog.askopenfilename(
            title="Select ambient audio file",
            filetypes=[("Audio files", "*.mp3 *.opus *.m4a *.flac *.mp4"),
                       ("All files", "*.*")])
        if path:
            self.config["mp3_path"] = path
            self._refresh_path_fields()

    def _browse_playlist(self):
        folder = filedialog.askdirectory(title="Select folder with audio files")
        if folder:
            self.config["playlist_folder"] = folder
            self._refresh_path_fields()

    def _save_settings(self):
        errors = self._commit_inputs()
        if errors:
            messagebox.showerror("Invalid settings", "\n\n".join(errors), parent=self.root)
            return
        startup_problem = self._apply_startup_choice()
        if not self._save_now():
            self.set_status("Could not save settings — see the log file for details.")
        else:
            self.set_status(startup_problem or "Settings saved!")

    # ── typed inputs ──

    def _commit_inputs(self):
        """Copy UI values into self.config. Valid values are applied; invalid
        ones are left out and returned as user-facing messages."""
        errors = []
        try:
            self.config["silence_seconds"] = parse_int_field(
                self.silence_var.get(), "Silence Timeout", SILENCE_MIN, SILENCE_MAX, "seconds")
            self.silence_entry.config(fg=self._FG)
        except ValidationError as e:
            errors.append(str(e))
            self.silence_entry.config(fg=self._ERR)
        try:
            self.config["max_volume"] = parse_int_field(
                self.vol_var.get(), "Max Volume", VOLUME_MIN, VOLUME_MAX)
            self.vol_entry.config(fg=self._FG)
        except ValidationError as e:
            errors.append(str(e))
            self.vol_entry.config(fg=self._ERR)

        # File/folder paths are set by Browse (the fields only show names).

        self.config["fade_enabled"]       = bool(self.fade_enabled_var.get())
        self.config["mode"]               = self.mode_var.get()
        self.config["single_loop_mode"]   = self.single_loop_var.get()
        self.config["playlist_loop_mode"] = self.playlist_loop_var.get()
        self.config["duck_percent"]       = int(self.duck_var.get())
        return errors

    def _commit_inputs_quietly(self):
        errors = self._commit_inputs()
        if errors:
            self.set_status(errors[0])

    def _revert_invalid_fields(self):
        self.silence_var.set(str(self.config["silence_seconds"]))
        self.vol_var.set(str(self.config["max_volume"]))
        self.silence_entry.config(fg=self._FG)
        self.vol_entry.config(fg=self._FG)

    # ── monitoring ──

    def _set_monitoring_ui(self, on):
        if on:
            self.start_btn.config(text="Stop Monitoring", fg="#f38ba8")
        else:
            self.start_btn.config(text="Start Monitoring", fg="#a6e3a1")
        self._refresh_tray_menu()

    def _start_monitoring(self):
        self.monitoring = True
        self.monitor.start()
        self._set_monitoring_ui(True)

    def _toggle_monitoring(self):
        if self.monitoring:
            self.monitoring = False
            self.monitor.stop()
            self.player.stop()          # fades out on the playback thread
            self.set_status("Monitoring stopped.")
            self._set_monitoring_ui(False)
            return

        errors = self._commit_inputs()
        problem = validate_ambient_source(self.config)
        if problem:
            errors.append(problem)
        if errors:
            self._show_window()
            messagebox.showerror("Can't start monitoring", "\n\n".join(errors), parent=self.root)
            return
        self._start_monitoring()
        self.set_status("Monitoring... waiting for silence.")

    def on_monitor_auto_stopped(self, reason):
        """Called from the monitor thread when it stops itself."""
        self.call_in_ui(self._handle_auto_stop, reason)

    def _handle_auto_stop(self, reason):
        if self.monitor.running:
            return   # the user already started monitoring again
        self.monitoring = False
        self._set_monitoring_ui(False)
        self.set_status(reason)

    # ── called by the monitor thread ──

    def play_ambient(self):
        self.set_status("Silence reached — playing ambient sound...")
        return self.player.play(self.config)

    def duck_ambient(self, target_vol):
        self.player.duck(target_vol)

    def unduck_ambient(self, target_vol):
        self.player.unduck(target_vol)

    def stop_ambient(self):
        self.player.stop()
