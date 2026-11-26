<p align="center">
  <img src="icon.png" alt="Silence Player" width="160">
</p>

<h1 align="center">Silence Player</h1>

<p align="center">
  <strong>Ambient audio that automatically fills the silence.</strong>
</p>

<p align="center">
  A lightweight Windows app that plays your chosen ambient audio when your PC goes quiet
  and gets out of the way as soon as another application starts making sound.
</p>

<p align="center">
  Windows • Python • VLC
</p>

---

## ✨ Features

- 🎵 Play a single audio file or an entire playlist
- 🔇 Automatically detects when your PC becomes silent
- ⏱️ Customizable silence delay
- 🌊 Optional fade in and fade out
- 🦆 Duck ambient audio when external audio is detected
- 🔁 Loop, stop, and playlist looping modes
- 🚫 Exclude specific applications from audio detection
- 💬 Mirroring Fix for Discord, SignalRGB, Wallpaper Engine and more
- 🖥️ Runs quietly in the Windows system tray, and can start with Windows
- 🔔 Tells you when a new version is out
- ▶️ Shows what's playing, in the window and on the tray icon
- 💾 Saves your settings when you click Save Settings
- 🎧 Supports MP3, M4A, OPUS, FLAC and MP4

## 💡 How it works

Silence Player monitors active audio sessions on Windows.

When no external application has produced audio for your chosen amount of time:

**Silence → Silence Player starts your ambient audio**

When another application begins playing audio again:

**External audio detected → Silence Player fades, ducks or stops**

This makes it useful for ambient sound, rain sounds, white noise, room tone, focus audio, and anything else you want automatically playing during quiet moments.

## 📥 Download

Get the latest version from the [GitHub Releases](../../releases/latest) page:

- **SilencePlayer-Setup-x.y.z.exe**: the installer. It installs for your Windows
  user only (no admin rights needed), adds a Start menu shortcut, and can be
  uninstalled from Windows Settings. Installing a newer version keeps your settings.
- **SilencePlayer-x.y.z-portable.zip**: the same app as a folder. Unzip it anywhere
  and run `silenceplayer.exe`.

You don't need to install Python. Silence Player tells you when a new version is out.

## 🎵 Supported formats

| Format | Status |
| --- | :---: |
| MP3 | ✅ Supported |
| M4A | ✅ Supported |
| FLAC | ✅ Supported |
| MP4 | ✅ Supported |
| OPUS | ✅ Supported |

Playback is powered by VLC.

## 💬 Mirroring Fix

Some apps listen to everything your PC plays and show a fake level in the Windows mixer while doing it, without making any sound themselves. Discord does this while streaming; audio-reactive apps like SignalRGB and Wallpaper Engine do it all the time. Without help, Silence Player would see them "playing" the moment your ambient sound starts, and stop it.

The Mirroring Fix (on by default, in the **Advanced** tab) checks what these apps really output. If they make real sound (someone talks on Discord, a video wallpaper has audio), Silence Player reacts as usual. If they're only reacting to your sound, they're ignored.

It comes with Discord, SignalRGB, Wallpaper Engine, Lively Wallpaper, OBS, Streamlabs, XSplit, Medal, Rainmeter, iCUE and FluentFlyout on the list, and you can add any other app.

Silence Player watches every output device (speakers, headset, virtual devices). Audio routing apps such as NVIDIA Broadcast, Voicemeeter, SteelSeries Sonar, Elgato Wave Link, FxSound and Krisp are ignored on their own: they only re-play other apps' sound, and those apps are detected directly. If an app starts "playing" right after your ambient sound does, Silence Player notices and offers to add it in one click.

> Needs Windows 10 20H1 or later. If the check can't run on a PC, those apps simply count as playing, like before.

## 🚧 Silence Player 2.0

The current version uses the original Silence Player interface.

A larger **2.0 update** is planned with a complete UX/UI redesign and refreshed visual identity.

### Planned for 2.0

- [ ] Complete UX/UI redesign
- [ ] Refreshed visual identity
- [ ] Improved settings experience
- [ ] Cleaner application architecture
- [ ] Improved installation experience
- [ ] General usability improvements

The current version will remain available while 2.0 is being developed.

## 🩺 Settings and logs

- Changes apply right away. Click **Save Settings** to keep them: they are saved to
  `config.json` next to `silenceplayer.exe`, and only then. (If Windows doesn't allow
  writing there, for example in Program Files, Silence Player uses
  `%APPDATA%\SilencePlayer\config.json` instead.)
  If that file is ever damaged, Silence Player keeps a copy as
  `config.json.corrupt-<date>` and starts with default settings.
- A diagnostic log is written to `%LOCALAPPDATA%\SilencePlayer\silenceplayer.log`
  (rotated at 1 MB, last 3 kept). It never contains audio. If something misbehaves,
  this file is the most useful thing to attach to an issue.
- Only one Silence Player runs at a time. Launching it again opens the Settings
  window of the copy that is already running.
- The update check asks GitHub (`api.github.com`) which release is the newest. Nothing
  about you or your PC is sent. A version you skip is remembered in
  `%LOCALAPPDATA%\SilencePlayer\update.json`. To try the notice, start Silence Player
  with the environment variable `SILENCEPLAYER_PRETEND_VERSION` set to an older
  version, e.g. `1.2.0`.

## 🛠️ Building from source

Requirements: Windows 10 20H1 or later (x64) and Python 3.12. For the installer,
also [Inno Setup](https://jrsoftware.org/isinfo.php).

```powershell
git clone https://github.com/ELSEGuy/silenceplayer
cd silenceplayer
powershell -ExecutionPolicy Bypass -File build.ps1              # app folder + portable zip
powershell -ExecutionPolicy Bypass -File build.ps1 -Installer   # ...and the installer
```

`build.ps1` creates a clean `.venv-build`, installs the pinned versions from
`requirements.txt` and `requirements-dev.txt`, runs the tests and builds:

- `dist\SilencePlayer\` the app folder (`silenceplayer.exe`, with Python and VLC in `_internal`)
- `dist\SilencePlayer-<version>-portable.zip` that folder, zipped
- `dist\SilencePlayer-Setup-<version>.exe` the installer (with `-Installer`)
- `dist\SHA256SUMS.txt` and `dist\build-freeze.txt` (checksums, exact package versions)

The VLC engine is **not** downloaded during the build: the repo contains
`libvlc.dll`, `libvlccore.dll` and the plugins Silence Player uses, from the official
**VLC 3.0.24 (x64)** zip. To move to a newer VLC, run
`python tools\update_vlc.py <version>`: it downloads the official zip, checks it
against VideoLAN's published SHA-256, and replaces those files.

To run from source instead: `pip install -r requirements.txt`, then `python app.py`.
Tests: `pip install -r requirements-dev.txt`, then `python -m pytest` (they use fakes
for VLC and the Windows audio APIs, so they run anywhere). Check the code with
`python -m ruff check .`.

GitHub Actions runs the code check and the tests on every push, builds the installer
and portable zip on every push to `master` or a `v*` branch, and turns a pushed tag
like `v1.4.0` into a **draft** release with the files attached. See
[RELEASING.md](RELEASING.md).

### How the code is organised

`app.py` only starts the app. The code is in `silenceplayer/`, one job per file:

| File | What it does |
|---|---|
| `main.py` | Program start: logging, the single-instance check, then the window |
| `paths.py` | Where files are: the app folder, bundled files, `config.json`, logs |
| `config.py` | Settings: defaults, checking and repairing values, loading and saving |
| `sources.py` | The ambient file or playlist, and where to resume |
| `winaudio.py` | Which apps are making sound, on every output device |
| `mirror.py` | The Mirroring Fix (tells mirroring apps apart from real sound) |
| `monitor.py` | Waits for silence, then starts, stops or ducks the ambient sound |
| `player.py` | VLC playback: fades, ducking, playlists |
| `nowplaying.py` | What the Now playing card, tray tooltip and tray menu say |
| `updates.py` | The update check against GitHub releases |
| `startup.py` | Start with Windows |
| `instance.py` | Only one Silence Player at a time |
| `ui/window.py` | The Settings window and its tabs |
| `ui/tray.py`, `ui/widgets.py` | The tray icon and menu, the scrolling name label |
| `ui/rounded.py` | Apple-style continuous corners: rounded cards, buttons, fields, lists and sliders |

## 🧩 Built with

Silence Player uses several open-source projects:

- [ProcTap](https://github.com/m96-chan/ProcTap) — per-process audio capture (needs NumPy and SciPy)
- [python-vlc](https://github.com/oaubert/python-vlc) and [VLC](https://www.videolan.org/) — audio playback
- [pycaw](https://github.com/AndreMiras/pycaw) — Windows audio session monitoring (with comtypes and psutil)
- [pystray](https://github.com/moses-palmer/pystray) — system tray integration
- [Pillow](https://python-pillow.org/) — tray icon images
- [NumPy](https://numpy.org/) — audio level analysis for the Mirroring Fix
- [PyInstaller](https://pyinstaller.org/) — packaging

## 📜 License

Silence Player is licensed under the **Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0)**.

You may share and modify the project for non-commercial purposes with appropriate attribution.

Commercial use requires permission from the author.

## Credits

Created by **ELSEGuy**.

Development was assisted by a personal AI agent.
