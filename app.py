import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import time
import json
import os
import sys
import glob
import collections

import numpy as np
from proctap import ProcessAudioCapture
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
import pystray
from PIL import Image, ImageDraw

# Always use bundled VLC libraries
_vlc_dll = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libvlc.dll")
_vlc_plugins = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
if hasattr(sys, '_MEIPASS'):
    _vlc_dll = os.path.join(sys._MEIPASS, "libvlc.dll")
    _vlc_plugins = os.path.join(sys._MEIPASS, "plugins")
os.environ['PYTHON_VLC_LIB_PATH'] = _vlc_dll
os.environ['PYTHON_VLC_MODULE_PATH'] = _vlc_plugins

import vlc

if hasattr(sys, '_MEIPASS'):
    CONFIG_FILE = os.path.join(os.path.dirname(sys.executable), "config.json")
else:
    CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "mp3_path": "",
    "playlist_folder": "",
    "silence_seconds": 30,
    "max_volume": 80,
    "fade_enabled": True,
    "duck_percent": 0,
    "mode": "single",
    "single_loop_mode": "loop",
    "playlist_loop_mode": "loop_playlist",
    "excluded_apps": [],
    "discord_mirror_fix": False,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

OWN_PROCESSES = {"python.exe", "pythonw.exe", "py.exe", "python3.exe", "silenceplayer.exe"}
SUPPORTED_EXTENSIONS = ("*.mp3", "*.opus", "*.m4a", "*.flac", "*.mp4")
FINGERPRINT_APPS = {"discord.exe"}

def get_playing_apps(excluded=None):
    playing = set()
    excluded_set = set(e.lower() for e in (excluded or []))
    try:
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process:
                name = session.Process.name().lower()
                if name in OWN_PROCESSES:
                    continue
                if name in excluded_set:
                    continue
                try:
                    meter = session._ctl.QueryInterface(IAudioMeterInformation)
                    peak = meter.GetPeakValue()
                    if peak > 0.001:
                        playing.add(name)
                except Exception:
                    pass
    except Exception:
        pass
    return playing

def get_all_discord_pids():
    """Returns all unique PIDs associated with discord.exe audio sessions."""
    pids = set()
    try:
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process:
                if session.Process.name().lower() == "discord.exe":
                    pids.add(session.Process.pid)
    except Exception:
        pass
    return pids

def resource_path(filename):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

def create_tray_icon():
    try:
        img = Image.open(resource_path("trayicon.png")).resize((64, 64))
        return img
    except Exception:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([0, 0, 63, 63], fill="#1e1e2e")
        draw.rectangle([24, 18, 30, 42], fill="#89b4fa")
        draw.rectangle([30, 18, 44, 26], fill="#89b4fa")
        draw.rectangle([38, 26, 44, 42], fill="#89b4fa")
        draw.ellipse([18, 38, 32, 48], fill="#89b4fa")
        draw.ellipse([32, 38, 46, 48], fill="#89b4fa")
        return img


class DiscordMirrorFix:
    """
    Monitors ALL Discord audio sessions via ProcTap.

    Discord uses multiple PIDs:
      - One for voice output (incoming voice from other users)
      - One for UI sounds (notifications, join/leave sounds)
      - One for capture/streaming (mirrors desktop audio — outputs nothing)

    ProcTap sees 0 RMS on the capture session because it never renders audio.
    By monitoring all Discord PIDs simultaneously, if ANY has RMS > 0
    then Discord is playing real audio. If ALL are 0 → mirroring only → ignore.
    """

    REAL_AUDIO_THRESHOLD = 0.001
    WINDOW_SIZE          = 10

    def __init__(self):
        self._lock        = threading.Lock()
        self._running     = False
        self._taps        = {}   # pid → ProcessAudioCapture
        self._rms_buffers = {}   # pid → deque of recent RMS values
        self._watch_thread = None

    def start(self):
        self._running = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop, daemon=True)
        self._watch_thread.start()

    def stop(self):
        self._running = False
        with self._lock:
            taps = dict(self._taps)
            self._taps.clear()
            self._rms_buffers.clear()
        for pid, tap in taps.items():
            try:
                tap.stop()
            except Exception:
                pass

    def _make_callback(self, pid):
        def on_data(pcm, frames):
            try:
                samples = np.frombuffer(pcm, dtype=np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2)))
                with self._lock:
                    if pid in self._rms_buffers:
                        self._rms_buffers[pid].append(rms)
            except Exception:
                pass
        return on_data

    def _watch_loop(self):
        while self._running:
            try:
                current_pids = get_all_discord_pids()

                with self._lock:
                    existing_pids = set(self._taps.keys())

                # Start taps for new PIDs
                for pid in current_pids - existing_pids:
                    try:
                        tap = ProcessAudioCapture(pid)
                        tap.set_callback(self._make_callback(pid))
                        tap.start()
                        with self._lock:
                            self._taps[pid] = tap
                            self._rms_buffers[pid] = collections.deque(
                                maxlen=self.WINDOW_SIZE)
                    except Exception:
                        pass

                # Stop taps for PIDs that disappeared
                for pid in existing_pids - current_pids:
                    with self._lock:
                        tap = self._taps.pop(pid, None)
                        self._rms_buffers.pop(pid, None)
                    if tap:
                        try:
                            tap.stop()
                        except Exception:
                            pass

            except Exception:
                pass

            time.sleep(1.0)

    def is_real_discord_audio(self):
        """
        True  → at least one Discord session is outputting real audio → react
        False → all Discord sessions silent → mirroring only → ignore
        """
        with self._lock:
            if not self._rms_buffers:
                return True  # No Discord sessions found → safe default
            for pid, buf in self._rms_buffers.items():
                if len(buf) < 3:
                    continue
                avg = float(np.mean(list(buf)))
                if avg > self.REAL_AUDIO_THRESHOLD:
                    return True
        return False


class AudioMonitor:
    def __init__(self, app):
        self.app = app
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _monitor_loop(self):
        silence_start   = None
        ambient_triggered = False
        cooldown_until  = 0
        ducked          = False
        unduck_start    = None

        self.app.set_status("Monitoring... waiting for silence.")

        while self.running:
            time.sleep(0.5)
            silence_secs = float(self.app.config["silence_seconds"])
            fade_enabled = self.app.config.get("fade_enabled", True)
            fade_secs    = 2.0 if fade_enabled else 0.01
            duck_percent = float(self.app.config.get("duck_percent", 0))
            max_vol      = float(self.app.config["max_volume"])
            excluded     = self.app.config.get("excluded_apps", [])
            mirror_fix   = self.app.config.get("discord_mirror_fix", False)
            now          = time.time()

            raw_apps = get_playing_apps(excluded)

            # Apply Discord mirror fix
            if mirror_fix and self.app.discord_fix:
                current_apps = set()
                for name in raw_apps:
                    if name in FINGERPRINT_APPS:
                        # Only include Discord if it's playing real audio
                        if self.app.discord_fix.is_real_discord_audio():
                            current_apps.add(name)
                    else:
                        current_apps.add(name)
            else:
                current_apps = raw_apps

            if ambient_triggered:
                if now < cooldown_until:
                    self.app.set_status("Playing ambient sound...")
                    continue

                if not self.app.player.playing and not ducked:
                    ambient_triggered = False
                    silence_start = None
                    self.app.stop_monitoring()
                    continue

                if current_apps:
                    if duck_percent == 0:
                        ducked = False
                        unduck_start = None
                        self.app.stop_ambient()
                        ambient_triggered = False
                        silence_start = None
                        self.app.set_status(
                            f"External audio detected ({', '.join(current_apps)}) — ambient stopped.")
                    else:
                        if not ducked:
                            ducked = True
                            unduck_start = None
                            duck_vol = (duck_percent / 100.0) * max_vol
                            self.app.duck_ambient(duck_vol)
                            self.app.set_status(
                                f"External audio detected — ambient ducked to {int(duck_percent)}%.")
                        else:
                            unduck_start = None
                            self.app.set_status(
                                f"External audio detected — ambient ducked to {int(duck_percent)}%.")
                else:
                    if ducked:
                        if unduck_start is None:
                            unduck_start = now
                        elapsed = now - unduck_start
                        remaining = silence_secs - elapsed
                        if remaining > 0:
                            self.app.set_status(
                                f"Silence returned — fading back up in {remaining:.0f}s")
                        else:
                            ducked = False
                            unduck_start = None
                            self.app.unduck_ambient(max_vol)
                            self.app.set_status("Playing ambient sound...")
                    else:
                        self.app.set_status("Playing ambient sound...")
            else:
                if current_apps:
                    silence_start = None
                    self.app.set_status(
                        f"Audio playing ({', '.join(current_apps)}). Monitoring...")
                else:
                    if silence_start is None:
                        silence_start = now
                    elapsed = now - silence_start
                    if elapsed >= silence_secs:
                        ambient_triggered = True
                        cooldown_until = now + fade_secs + 2.0
                        ducked = False
                        unduck_start = None
                        self.app.play_ambient()
                    else:
                        remaining = silence_secs - elapsed
                        self.app.set_status(
                            f"Silence detected... playing in {remaining:.0f}s")


class AmbientPlayer:
    def __init__(self, app):
        self.app = app
        self.playing = False
        self.saved_pos = 0.0
        self.playlist = []
        self.playlist_index = 0
        self._current_vol = 0
        self._stop_event = threading.Event()
        self.vlc_instance = vlc.Instance("--quiet", "--no-video")
        self.media_player = self.vlc_instance.media_player_new()

    def _get_fade_secs(self):
        return 2.0 if self.app.config.get("fade_enabled", True) else 0.01

    def _set_volume(self, vol_percent):
        vol = max(0, min(100, int(vol_percent)))
        self._current_vol = vol
        self.media_player.audio_set_volume(vol)

    def _get_volume(self):
        return self.media_player.audio_get_volume()

    def _is_playing(self):
        return self.media_player.is_playing()

    def _fade_in(self, target_vol):
        fade_secs = self._get_fade_secs()
        steps = 50
        step_time = fade_secs / steps
        self._set_volume(0)
        for i in range(steps + 1):
            if not self.playing or self._stop_event.is_set():
                return False
            self._set_volume((i / steps) * target_vol)
            time.sleep(step_time)
        return True

    def _fade_out(self):
        fade_secs = self._get_fade_secs()
        steps = 50
        step_time = fade_secs / steps
        current = self._get_volume()
        for i in range(steps):
            if not self._is_playing():
                break
            self._set_volume(current * (1 - (i / steps)))
            time.sleep(step_time)
        self.media_player.stop()

    def duck(self, target_vol):
        try:
            current = self._get_volume()
            for i in range(21):
                if not self.playing:
                    return
                self._set_volume(current + (target_vol - current) * (i / 20))
                time.sleep(0.05)
        except Exception:
            pass

    def unduck(self, target_vol):
        try:
            current = self._get_volume()
            fade_secs = self._get_fade_secs()
            steps = 50
            step_time = fade_secs / steps
            for i in range(steps + 1):
                if not self.playing:
                    return
                self._set_volume(current + (target_vol - current) * (i / steps))
                time.sleep(step_time)
        except Exception:
            pass

    def play(self, config):
        if self.playing:
            return
        self.playing = True
        self._stop_event.clear()
        mode = config.get("mode", "single")

        if mode == "single":
            path = config.get("mp3_path", "")
            loop_mode = config.get("single_loop_mode", "loop")
            if not path or not os.path.exists(path):
                self.app.set_status("No valid audio file selected!", error=True)
                self.playing = False
                return
            threading.Thread(
                target=self._play_single,
                args=(path, config["max_volume"], loop_mode),
                daemon=True).start()
        else:
            folder = config.get("playlist_folder", "")
            if not folder or not os.path.isdir(folder):
                self.app.set_status("No valid playlist folder selected!", error=True)
                self.playing = False
                return
            files = []
            for ext in SUPPORTED_EXTENSIONS:
                files.extend(glob.glob(os.path.join(folder, ext)))
            files = sorted(files)
            if not files:
                self.app.set_status("No supported audio files found in folder!", error=True)
                self.playing = False
                return
            self.playlist = files
            self.playlist_index = 0
            threading.Thread(
                target=self._play_playlist,
                args=(config["max_volume"], config.get("playlist_loop_mode", "loop_playlist")),
                daemon=True).start()

    def _load_and_play(self, path, start_pos=0.0):
        media = self.vlc_instance.media_new(path)
        self.media_player.set_media(media)
        self.media_player.play()
        for _ in range(20):
            time.sleep(0.1)
            if self.media_player.is_playing():
                break
        if start_pos > 0.5:
            self.media_player.set_time(int(start_pos * 1000))

    def _play_single(self, path, max_vol, loop_mode):
        try:
            self._load_and_play(path, self.saved_pos)
            self.saved_pos = 0.0
            if not self._fade_in(max_vol):
                return
            if loop_mode == "loop":
                while self.playing and not self._stop_event.is_set():
                    if not self._is_playing():
                        self._load_and_play(path, 0.0)
                        self._set_volume(max_vol)
                    time.sleep(0.3)
            else:
                while self.playing and not self._stop_event.is_set():
                    if not self._is_playing():
                        break
                    time.sleep(0.3)
                self.playing = False
        except Exception as e:
            self.app.set_status(f"Playback error: {e}", error=True)
            self.playing = False

    def _play_playlist(self, max_vol, loop_mode):
        try:
            first_song = True
            while self.playing and not self._stop_event.is_set():
                loop_mode = self.app.config["playlist_loop_mode"]
                if self.playlist_index >= len(self.playlist):
                    if loop_mode == "loop_playlist":
                        self.playlist_index = 0
                    else:
                        self.playing = False
                        break
                path = self.playlist[self.playlist_index]
                self.app.set_status(f"Playing: {os.path.basename(path)}")
                self._load_and_play(path, self.saved_pos if first_song else 0.0)
                self.saved_pos = 0.0
                first_song = False
                if not self._fade_in(max_vol):
                    return
                while self.playing and not self._stop_event.is_set():
                    if not self._is_playing():
                        break
                    time.sleep(0.3)
                if not self.playing or self._stop_event.is_set():
                    return
                current_mode = self.app.config["playlist_loop_mode"]
                if current_mode == "loop_song":
                    self._load_and_play(path, 0.0)
                    self._set_volume(max_vol)
                elif current_mode == "stop":
                    self.playing = False
                    break
                else:
                    self.playlist_index += 1
        except Exception as e:
            self.app.set_status(f"Playback error: {e}", error=True)
            self.playing = False

    def stop(self):
        if not self.playing:
            return
        try:
            self.saved_pos = self.media_player.get_time() / 1000.0
        except Exception:
            self.saved_pos = 0.0
        self._stop_event.set()
        self.playing = False
        try:
            self._fade_out()
        except Exception:
            self.media_player.stop()


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Silence Player")
        self.root.geometry("520x660")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._hide_window)

        try:
            self.root.iconphoto(True, tk.PhotoImage(file=resource_path("icon.png")))
        except Exception:
            pass

        self.config      = load_config()
        self.monitor     = AudioMonitor(self)
        self.player      = AmbientPlayer(self)
        self.discord_fix = None
        self.monitoring  = False
        self._status     = "Ready."
        self.tray        = None

        self._build_ui()
        threading.Thread(target=self._build_tray, daemon=True).start()
        self._start_monitoring()
        self.root.after(100, self._hide_window)

    def _start_discord_fix(self):
        if self.discord_fix is None:
            self.discord_fix = DiscordMirrorFix()
            self.discord_fix.start()

    def _stop_discord_fix(self):
        if self.discord_fix is not None:
            fix = self.discord_fix
            self.discord_fix = None  # set None first so monitor stops using it
            threading.Thread(target=fix.stop, daemon=True).start()

    def _build_tray(self):
        icon_image = create_tray_icon()
        menu = pystray.Menu(
            pystray.MenuItem("Open Settings", self._tray_open, default=True),
            pystray.MenuItem("Stop Monitoring", self._tray_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self.tray = pystray.Icon("SilencePlayer", icon_image, "Silence Player", menu)
        self.tray.run()

    def _tray_open(self, icon=None, item=None):
        self.root.after(0, self._show_window)

    def _tray_toggle(self, icon=None, item=None):
        self.root.after(0, self._toggle_monitoring)

    def _tray_quit(self, icon=None, item=None):
        self.monitor.stop()
        self.player.stop()
        self._stop_discord_fix()
        save_config(self.config)
        if self.tray:
            self.tray.stop()
        self.root.after(0, self.root.destroy)

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_window(self):
        save_config(self.config)
        self.root.withdraw()

    def _build_ui(self):
        BG     = "#1e1e2e"
        CARD   = "#2a2a3e"
        FG     = "#cdd6f4"
        ACC    = "#89b4fa"
        BTN_BG = "#313244"
        DIM    = "#a6adc8"

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

        self.tab_btn_main = tk.Button(tab_bar, text="Settings",
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

        self.status_var = tk.StringVar(value=self._status)
        tk.Label(self.root, textvariable=self.status_var,
                 bg="#181825", fg=DIM,
                 font=("Segoe UI", 9), pady=8, wraplength=500).pack(fill="x", side="bottom")

    def _build_main_tab(self, parent, BG, CARD, FG, ACC, BTN_BG, DIM, label, entry, button):
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
        self.mp3_var = tk.StringVar(value=self.config["mp3_path"])
        entry(single_card, self.mp3_var, width=22).grid(row=1, column=0, sticky="w")
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
        self.playlist_var = tk.StringVar(value=self.config.get("playlist_folder", ""))
        entry(playlist_card, self.playlist_var, width=22).grid(row=1, column=0, sticky="w")
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
        entry(shared_card, self.silence_var, width=12).grid(row=1, column=0, sticky="w", pady=(2, 8))
        entry(shared_card, self.vol_var,     width=12).grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(2, 8))

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
            name = self.exclude_entry_var.get().strip().lower()
            if not name:
                return
            if not name.endswith(".exe"):
                name += ".exe"
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
        tk.Label(parent, text="Advanced Settings", bg=BG, fg=ACC,
                 font=("Segoe UI", 12, "bold")).pack(pady=(16, 2))
        tk.Label(parent, text="Experimental features — may behave unexpectedly.",
                 bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(pady=(0, 12))

        card = tk.Frame(parent, bg=CARD, padx=16, pady=14)
        card.pack(fill="x", padx=24, pady=4)

        title_row = tk.Frame(card, bg=CARD)
        title_row.pack(fill="x")

        tk.Label(title_row, text="Discord Mirroring Fix",
                 bg=CARD, fg=FG, font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(title_row, text="EXPERIMENTAL",
                 bg="#f38ba8", fg="#1e1e2e",
                 font=("Segoe UI", 8, "bold"),
                 padx=6, pady=2).pack(side="left", padx=(8, 0))

        self.mirror_fix_var = tk.BooleanVar(value=self.config.get("discord_mirror_fix", False))
        mirror_status = tk.Label(card, bg=CARD, font=("Segoe UI", 9, "bold"))

        def toggle_mirror_fix():
            v = self.mirror_fix_var.get()
            self.config["discord_mirror_fix"] = v
            mirror_btn.config(
                bg="#a6e3a1" if v else BTN_BG,
                fg="#1e1e2e" if v else DIM,
                text="Enabled" if v else "Disabled")
            if v:
                self._start_discord_fix()
                mirror_status.config(
                    text="● Active — monitoring Discord's output stream",
                    fg="#a6e3a1")
            else:
                self._stop_discord_fix()
                mirror_status.config(text="● Off", fg=DIM)
            save_config(self.config)

        mirror_btn = tk.Button(title_row,
                               command=lambda: [
                                   self.mirror_fix_var.set(not self.mirror_fix_var.get()),
                                   toggle_mirror_fix()],
                               relief="flat", font=("Segoe UI", 9, "bold"),
                               padx=12, pady=4, cursor="hand2")
        mirror_btn.pack(side="right")

        tk.Label(card,
                 text="When Discord streams your desktop, it captures your\n"
                      "ambient sound and shows a fake peak in the mixer.\n\n"
                      "This fix taps Discord's actual speaker output directly.\n"
                      "If Discord outputs nothing → it's only capturing → ignored.\n"
                      "If Discord outputs audio → real sound → ambient stops.",
                 bg=CARD, fg=DIM, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", pady=(8, 6))

        mirror_status.pack(anchor="w")

        v = self.mirror_fix_var.get()
        mirror_btn.config(
            bg="#a6e3a1" if v else BTN_BG,
            fg="#1e1e2e" if v else DIM,
            text="Enabled" if v else "Disabled")
        mirror_status.config(
            text="● Active — monitoring Discord's output stream" if v else "● Off",
            fg="#a6e3a1" if v else DIM)
        if v:
            self._start_discord_fix()

    def _sync_excluded_apps(self):
        self.config["excluded_apps"] = list(self.exclude_listbox.get(0, tk.END))
        save_config(self.config)

    def _browse_mp3(self):
        path = filedialog.askopenfilename(
            title="Select ambient audio file",
            filetypes=[("Audio files", "*.mp3 *.opus *.m4a *.flac *.mp4"),
                       ("All files", "*.*")])
        if path:
            self.mp3_var.set(path)
            self.config["mp3_path"] = path

    def _browse_playlist(self):
        folder = filedialog.askdirectory(title="Select folder with audio files")
        if folder:
            self.playlist_var.set(folder)
            self.config["playlist_folder"] = folder

    def _save_settings(self):
        if not self._read_inputs():
            return
        save_config(self.config)
        self.set_status("Settings saved!")

    def _read_inputs(self):
        try:
            silence = int(self.silence_var.get())
            vol     = int(self.vol_var.get())
            assert 1 <= silence <= 3600
            assert 0 <= vol <= 100
        except (ValueError, AssertionError) as e:
            messagebox.showerror("Invalid input", str(e))
            return False
        self.config["mp3_path"]           = self.mp3_var.get()
        self.config["playlist_folder"]    = self.playlist_var.get()
        self.config["silence_seconds"]    = silence
        self.config["max_volume"]         = vol
        self.config["fade_enabled"]       = self.fade_enabled_var.get()
        self.config["mode"]               = self.mode_var.get()
        self.config["single_loop_mode"]   = self.single_loop_var.get()
        self.config["playlist_loop_mode"] = self.playlist_loop_var.get()
        self.config["duck_percent"]       = self.duck_var.get()
        return True

    def _start_monitoring(self):
        self.monitoring = True
        self.monitor.start()
        self.start_btn.config(text="Stop Monitoring", fg="#f38ba8")

    def _toggle_monitoring(self):
        if self.monitoring:
            self.monitoring = False
            self.monitor.stop()
            self.player.stop()
            self.set_status("Monitoring stopped.")
            self.start_btn.config(text="Start Monitoring", fg="#a6e3a1")
        else:
            if not self._read_inputs():
                return
            mode = self.config["mode"]
            if mode == "single" and not os.path.exists(self.config["mp3_path"]):
                messagebox.showerror("No file", "Please select a valid audio file first.")
                return
            if mode == "playlist" and not os.path.isdir(self.config["playlist_folder"]):
                messagebox.showerror("No folder", "Please select a valid playlist folder first.")
                return
            self.monitoring = True
            self.monitor.start()
            self.set_status("Monitoring... waiting for silence.")
            self.start_btn.config(text="Stop Monitoring", fg="#f38ba8")

    def play_ambient(self):
        self.set_status("Silence reached — playing ambient sound...")
        self.player.play(self.config)

    def duck_ambient(self, target_vol):
        threading.Thread(target=self.player.duck, args=(target_vol,), daemon=True).start()

    def unduck_ambient(self, target_vol):
        threading.Thread(target=self.player.unduck, args=(target_vol,), daemon=True).start()

    def stop_ambient(self):
        self.player.stop()

    def stop_monitoring(self):
        self.monitoring = False
        self.monitor.stop()
        self.set_status("Playback finished — monitoring stopped.")
        try:
            self.root.after(0, lambda: self.start_btn.config(
                text="Start Monitoring", fg="#a6e3a1"))
        except Exception:
            pass

    def set_status(self, msg, error=False):
        self._status = msg
        try:
            self.root.after(0, lambda: self.status_var.set(msg))
        except Exception:
            pass

    def _on_close(self):
        self.monitor.stop()
        self.player.stop()
        self._stop_discord_fix()
        save_config(self.config)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()