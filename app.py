import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import time
import json
import os
import sys
import glob

import pygame
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
import pystray
from PIL import Image, ImageDraw

os.environ["SDL_AUDIODRIVER"] = "directsound"

if hasattr(sys, '_MEIPASS'):
    CONFIG_FILE = os.path.join(os.path.dirname(sys.executable), "config.json")
else:
    CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "mp3_path": "",
    "playlist_folder": "",
    "silence_seconds": 30,
    "max_volume": 80,
    "fade_in_seconds": 5,
    "mode": "single",
    "single_loop_mode": "loop",
    "playlist_loop_mode": "loop_playlist",
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

OWN_PROCESSES = {"silenceplayer.exe"}

def get_playing_apps():
    playing = set()
    try:
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process:
                name = session.Process.name().lower()
                if name in OWN_PROCESSES:
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
        silence_start = None
        ambient_triggered = False
        cooldown_until = 0

        self.app.set_status("Monitoring... waiting for silence.")

        while self.running:
            time.sleep(0.5)
            silence_secs = float(self.app.config["silence_seconds"])
            fade_secs = float(self.app.config["fade_in_seconds"])
            now = time.time()
            current_apps = get_playing_apps()

            if ambient_triggered:
                if now < cooldown_until:
                    self.app.set_status("Playing ambient sound...")
                    continue

                # Check if ambient finished on its own (stop mode)
                if not self.app.player.playing:
                    ambient_triggered = False
                    silence_start = None
                    self.app.stop_monitoring()
                    continue

                if current_apps:
                    self.app.stop_ambient()
                    ambient_triggered = False
                    silence_start = None
                    self.app.set_status(f"External audio detected ({', '.join(current_apps)}) — ambient stopped.")
                else:
                    self.app.set_status("Playing ambient sound...")

            else:
                if current_apps:
                    silence_start = None
                    self.app.set_status(f"Audio playing ({', '.join(current_apps)}). Monitoring...")
                else:
                    if silence_start is None:
                        silence_start = now
                    elapsed = now - silence_start
                    if elapsed >= silence_secs:
                        ambient_triggered = True
                        cooldown_until = now + fade_secs + 2.0
                        self.app.play_ambient()
                    else:
                        remaining = silence_secs - elapsed
                        self.app.set_status(f"Silence detected... playing in {remaining:.0f}s")


class AmbientPlayer:
    def __init__(self, app):
        self.app = app
        self.playing = False
        self.fade_thread = None
        self.playlist = []
        self.playlist_index = 0
        self.playlist_thread = None
        self.saved_pos = 0.0
        try:
            pygame.mixer.init()
        except Exception:
            try:
                pygame.mixer.pre_init(44100, -16, 2, 2048)
                pygame.mixer.init()
            except Exception as e:
                print(f"Audio init error: {e}")

    def play(self, config):
        if self.playing:
            return
        self.playing = True
        mode = config.get("mode", "single")

        if mode == "single":
            path = config.get("mp3_path", "")
            loop_mode = config.get("single_loop_mode", "loop")
            if not path or not os.path.exists(path):
                self.app.set_status("No valid MP3 file selected!", error=True)
                self.playing = False
                return
            self.fade_thread = threading.Thread(
                target=self._play_single,
                args=(path, config["max_volume"], config["fade_in_seconds"], loop_mode),
                daemon=True
            )
            self.fade_thread.start()

        else:
            folder = config.get("playlist_folder", "")
            if not folder or not os.path.isdir(folder):
                self.app.set_status("No valid playlist folder selected!", error=True)
                self.playing = False
                return
            files = sorted(glob.glob(os.path.join(folder, "*.mp3")))
            if not files:
                self.app.set_status("No MP3 files found in folder!", error=True)
                self.playing = False
                return
            self.playlist = files
            self.playlist_index = 0
            loop_mode = config.get("playlist_loop_mode", "loop_playlist")
            self.fade_thread = threading.Thread(
                target=self._play_playlist,
                args=(config["max_volume"], config["fade_in_seconds"], loop_mode),
                daemon=True
            )
            self.fade_thread.start()

    def _fade_in(self, max_vol, fade_secs):
        """Fade volume from 0 to max_vol over fade_secs seconds."""
        steps = 50
        step_time = fade_secs / steps
        for i in range(steps + 1):
            if not self.playing:
                return False
            pygame.mixer.music.set_volume((i / steps) * max_vol)
            time.sleep(step_time)
        return True

    def _fade_out(self, fade_secs):
        """Fade volume from current to 0 over fade_secs seconds."""
        try:
            current_vol = pygame.mixer.music.get_volume()
            steps = 50
            step_time = fade_secs / steps
            for i in range(steps):
                if not pygame.mixer.music.get_busy():
                    break
                pygame.mixer.music.set_volume(current_vol * (1 - (i / steps)))
                time.sleep(step_time)
            pygame.mixer.music.stop()
        except Exception:
            pass

    def _play_single(self, path, max_vol_percent, fade_secs, loop_mode):
        try:
            max_vol = max_vol_percent / 100.0
            pygame.mixer.music.load(path)
            pygame.mixer.music.set_volume(0.0)
            loops = -1 if loop_mode == "loop" else 0
            pygame.mixer.music.play(loops, start=self.saved_pos)
            self.saved_pos = 0.0
            if not self._fade_in(max_vol, fade_secs):
                return
            # If stop mode, wait for song to finish
            if loop_mode == "stop":
                while pygame.mixer.music.get_busy() and self.playing:
                    time.sleep(0.2)
                self.playing = False
        except Exception as e:
            self.app.set_status(f"Playback error: {e}", error=True)
            self.playing = False

    def _play_playlist(self, max_vol_percent, fade_secs, loop_mode):
        try:
            max_vol = max_vol_percent / 100.0

            while self.playing:
                # Read loop mode live so toggle changes take effect immediately
                loop_mode = self.app.config["playlist_loop_mode"]

                if self.playlist_index >= len(self.playlist):
                    if loop_mode == "loop_playlist":
                        self.playlist_index = 0
                    else:
                        self.playing = False
                        break

                path = self.playlist[self.playlist_index]
                song_name = os.path.basename(path)
                self.app.set_status(f"Playing: {song_name}")

                pygame.mixer.music.load(path)
                pygame.mixer.music.set_volume(0.0)
                pygame.mixer.music.play(0, start=self.saved_pos)
                self.saved_pos = 0.0

                if not self._fade_in(max_vol, fade_secs):
                    return

                # Wait for song to finish, checking loop mode live every 0.2s
                while pygame.mixer.music.get_busy() and self.playing:
                    current_mode = self.app.config["playlist_loop_mode"]
                    if current_mode == "loop_song":
                        # Restart current song if it finishes
                        if not pygame.mixer.music.get_busy():
                            pygame.mixer.music.play(0)
                    time.sleep(0.2)

                if not self.playing:
                    return

                # Song finished — decide what to do based on current toggle
                current_mode = self.app.config["playlist_loop_mode"]
                if current_mode == "loop_song":
                    # Keep looping same song — don't advance index
                    continue
                elif current_mode == "stop":
                    self.playing = False
                    break
                else:
                    # loop_playlist — advance to next song
                    self.playlist_index += 1

        except Exception as e:
            self.app.set_status(f"Playback error: {e}", error=True)
            self.playing = False

    def stop(self):
        if not self.playing:
            return
        fade_secs = float(self.app.config["fade_in_seconds"])
        # Save position in seconds before stopping
        try:
            self.saved_pos = pygame.mixer.music.get_pos() / 1000.0
        except Exception:
            self.saved_pos = 0.0
        self.playing = False
        try:
            self._fade_out(fade_secs)
        except Exception:
            pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Silence Player")
        self.root.geometry("520x520")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._hide_window)

        try:
            self.root.iconphoto(True, tk.PhotoImage(file=resource_path("icon.png")))
        except Exception:
            pass

        self.config = load_config()
        self.monitor = AudioMonitor(self)
        self.player = AmbientPlayer(self)
        self.monitoring = False
        self._status = "Ready."
        self.tray = None

        self._build_ui()

        tray_thread = threading.Thread(target=self._build_tray, daemon=True)
        tray_thread.start()

        self._start_monitoring()
        self.root.after(100, self._hide_window)

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
        BG = "#1e1e2e"
        CARD = "#2a2a3e"
        FG = "#cdd6f4"
        ACC = "#89b4fa"
        BTN_BG = "#313244"
        DIM = "#a6adc8"

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

        # Title
        label(self.root, "Silence Player", size=16, bold=True, color=ACC).pack(pady=(16, 2))
        label(self.root, "Plays ambient sound when your PC is silent", size=9, color=DIM).pack()

        # Mode toggle
        mode_frame = tk.Frame(self.root, bg=BG)
        mode_frame.pack(pady=(10, 0))
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

        self.btn_single = mode_btn("Single File", "single")
        self.btn_playlist = mode_btn("Playlist", "playlist")

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

        # ── Single File Card ──────────────────────────────────────────
        single_card = tk.Frame(self.root, bg=CARD, padx=16, pady=12)

        label(single_card, "Ambient Sound File (MP3)", bold=True).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self.mp3_var = tk.StringVar(value=self.config["mp3_path"])
        entry(single_card, self.mp3_var, width=22).grid(row=1, column=0, sticky="w")
        button(single_card, "Browse", self._browse_mp3).grid(row=1, column=1, padx=(6, 10))

        # Single loop mode toggle
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

        # ── Playlist Card ─────────────────────────────────────────────
        playlist_card = tk.Frame(self.root, bg=CARD, padx=16, pady=12)

        label(playlist_card, "Ambient Sound Playlist (MP3 Folder)", bold=True).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self.playlist_var = tk.StringVar(value=self.config.get("playlist_folder", ""))
        entry(playlist_card, self.playlist_var, width=22).grid(row=1, column=0, sticky="w")
        button(playlist_card, "Browse", self._browse_playlist).grid(row=1, column=1, padx=(6, 10))

        # Playlist loop mode toggle
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

        self.p_btn_loop_song = make_playlist_toggle("Loop Song", "loop_song")
        self.p_btn_stop = make_playlist_toggle("Stop", "stop")
        self.p_btn_loop_pl = make_playlist_toggle("Loop Playlist", "loop_playlist")

        def _refresh_playlist_toggle():
            v = self.playlist_loop_var.get()
            self.p_btn_loop_song.config(bg=ACC if v == "loop_song" else BTN_BG,
                                        fg="#1e1e2e" if v == "loop_song" else DIM)
            self.p_btn_stop.config(bg="#f38ba8" if v == "stop" else BTN_BG,
                                   fg="#1e1e2e" if v == "stop" else DIM)
            self.p_btn_loop_pl.config(bg="#a6e3a1" if v == "loop_playlist" else BTN_BG,
                                      fg="#1e1e2e" if v == "loop_playlist" else DIM)

        _refresh_playlist_toggle()

        # ── Shared settings card ──────────────────────────────────────
        shared_card = tk.Frame(self.root, bg=CARD, padx=16, pady=12)
        shared_card.pack(fill="x", padx=24, pady=4)

        self.silence_var = tk.StringVar(value=str(self.config["silence_seconds"]))
        self.vol_var = tk.StringVar(value=str(self.config["max_volume"]))
        self.fade_var = tk.StringVar(value=str(self.config["fade_in_seconds"]))

        label(shared_card, "Silence Timeout (seconds)", bold=True).grid(row=0, column=0, sticky="w")
        label(shared_card, "Max Volume (0-100)", bold=True).grid(row=0, column=1, sticky="w", padx=(20, 0))
        entry(shared_card, self.silence_var, width=12).grid(row=1, column=0, sticky="w", pady=(2, 8))
        entry(shared_card, self.vol_var, width=12).grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(2, 8))

        label(shared_card, "Fade-in / Fade-out Duration (seconds)", bold=True).grid(
            row=2, column=0, columnspan=2, sticky="w")
        entry(shared_card, self.fade_var, width=12).grid(row=3, column=0, sticky="w", pady=(2, 0))

        # ── Buttons ───────────────────────────────────────────────────
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(pady=8)

        self.start_btn = button(btn_frame, "Start Monitoring", self._toggle_monitoring, color="#a6e3a1")
        self.start_btn.pack(side="left", padx=6)
        button(btn_frame, "Save Settings", self._save_settings, color="#f9e2af").pack(side="left", padx=6)

        # ── Status bar ────────────────────────────────────────────────
        self.status_var = tk.StringVar(value=self._status)
        tk.Label(self.root, textvariable=self.status_var,
                 bg="#181825", fg=DIM,
                 font=("Segoe UI", 9), pady=8, wraplength=500).pack(fill="x", side="bottom")

        # Init mode display
        _refresh_mode()

    def _browse_mp3(self):
        path = filedialog.askopenfilename(
            title="Select ambient MP3",
            filetypes=[("MP3 files", "*.mp3"), ("All files", "*.*")]
        )
        if path:
            self.mp3_var.set(path)
            self.config["mp3_path"] = path

    def _browse_playlist(self):
        folder = filedialog.askdirectory(title="Select folder with MP3 files")
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
            vol = int(self.vol_var.get())
            fade = float(self.fade_var.get())
            assert 1 <= silence <= 3600, "Silence timeout must be 1-3600 seconds"
            assert 0 <= vol <= 100, "Volume must be 0-100"
            assert 0.5 <= fade <= 60, "Fade-in must be 0.5-60 seconds"
        except (ValueError, AssertionError) as e:
            messagebox.showerror("Invalid input", str(e))
            return False
        self.config["mp3_path"] = self.mp3_var.get()
        self.config["playlist_folder"] = self.playlist_var.get()
        self.config["silence_seconds"] = silence
        self.config["max_volume"] = vol
        self.config["fade_in_seconds"] = fade
        self.config["mode"] = self.mode_var.get()
        self.config["single_loop_mode"] = self.single_loop_var.get()
        self.config["playlist_loop_mode"] = self.playlist_loop_var.get()
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
                messagebox.showerror("No file", "Please select a valid MP3 file first.")
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

    def stop_ambient(self):
        self.player.stop()

    def stop_monitoring(self):
        self.monitoring = False
        self.monitor.stop()
        self.set_status("Playback finished — monitoring stopped.")
        try:
            self.root.after(0, lambda: self.start_btn.config(text="Start Monitoring", fg="#a6e3a1"))
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
        save_config(self.config)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
