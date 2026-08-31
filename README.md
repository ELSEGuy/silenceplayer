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
- 💬 Experimental Discord Mirroring Fix
- 🖥️ Runs quietly in the Windows system tray
- 💾 Saves your settings automatically
- 🎧 Supports MP3, M4A, FLAC and MP4
- 🚧 OPUS support is a work in progress

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
| OPUS | 🚧 Work in progress |

> OPUS support currently has known issues and is not considered fully supported yet.

Playback is powered by VLC.

## 💬 Discord Mirroring Fix

Silence Player includes an **experimental Discord Mirroring Fix**.

Discord can expose audio sessions used for screen sharing or audio capture even when they are not actually producing audible sound. Silence Player can use per-process audio monitoring to better distinguish real Discord audio from mirrored sessions.

For now, this experimental fix only targets **Discord**. Broader support for common recording and streaming software is planned for a future version.

> This feature is experimental and may not work perfectly on every setup.

## 🚧 Silence Player 2.0

The current version uses the original Silence Player interface.

A larger **2.0 update** is planned with a complete UX/UI redesign and refreshed visual identity.

### Planned for 2.0

- [ ] Complete UX/UI redesign
- [ ] Refreshed visual identity
- [ ] Improved settings experience
- [ ] Cleaner application architecture
- [ ] Improved installation experience
- [ ] Broader recording and streaming software support for mirroring detection
- [ ] General usability improvements

The current version will remain available while 2.0 is being developed.

## 🧩 Built with

Silence Player uses several open-source projects:

- [ProcTap](https://github.com/m96-chan/ProcTap) — per-process audio capture
- [python-vlc](https://github.com/oaubert/python-vlc) — VLC audio playback
- [pycaw](https://github.com/AndreMiras/pycaw) — Windows audio session monitoring
- [pystray](https://github.com/moses-palmer/pystray) — system tray integration

## 📜 License

Silence Player is licensed under the **Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0)**.

You may share and modify the project for non-commercial purposes with appropriate attribution.

Commercial use requires permission from the author.

## Credits

Created by **ELSEGuy**.

Development was assisted by a personal AI agent.
