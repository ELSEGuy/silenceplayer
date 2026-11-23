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
- 🖥️ Runs quietly in the Windows system tray
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

The latest packaged Windows build is available from the [GitHub Releases](../../releases/latest) page.

If you use a packaged release, you do not need to install Python manually.

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

It comes with Discord, SignalRGB and Wallpaper Engine on the list, and you can add any other app. If an app starts "playing" right after your ambient sound does, Silence Player notices and offers to add it in one click.

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
  `config.json` next to `silenceplayer.exe`, and only then.
  If that file is ever damaged, Silence Player keeps a copy as
  `config.json.corrupt-<date>` and starts with default settings.
- A diagnostic log is written to `%LOCALAPPDATA%\SilencePlayer\silenceplayer.log`
  (rotated at 1 MB, last 3 kept). It never contains audio. If something misbehaves,
  this file is the most useful thing to attach to an issue.
- Only one Silence Player runs at a time. Launching it again opens the Settings
  window of the copy that is already running.

## 🛠️ Building from source

Requirements: Windows 10 20H1 or later (x64) and Python 3.12.

```powershell
git clone https://github.com/ELSEGuy/silenceplayer
cd silenceplayer
powershell -ExecutionPolicy Bypass -File build.ps1
```

`build.ps1` creates a clean `.venv-build`, installs the pinned versions from
`requirements.txt` and `requirements-dev.txt`, runs the tests, and builds
`dist\silenceplayer.exe` with `silenceplayer.spec`. It prints the size and SHA-256
and writes the exact package versions to `dist\build-freeze.txt`.

The VLC engine is **not** downloaded during the build. The repo contains the exact
copy it comes from: `libvlc.dll`, `libvlccore.dll` and `plugins\` from
**VLC 3.0.23 (x64)**. Only the plugins needed for local audio playback are bundled
(listed in `silenceplayer.spec`); the repo keeps the full set.

To run from source instead: `pip install -r requirements.txt`, then `python app.py`.
Tests: `pip install -r requirements-dev.txt`, then `python -m pytest`. The tests use
fakes for VLC and the Windows audio APIs, so they run anywhere.

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
