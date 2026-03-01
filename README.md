# Silence Player
### Created by ELSEGuy — 2026
Built with the assistance of Claude AI by Anthropic.

## What it does
Silence Player monitors your PC audio and automatically plays 
ambient sound when no audio has been detected for a set amount of time.
The ambient sound stops the moment any external app plays audio.

## Features
- Single file or playlist mode
- Supports MP3, OPUS, M4A, FLAC, MP4
- Fade in and fade out toggle
- Loop, Stop, or Loop Playlist modes
- Duck volume when external audio detected
- Exclude specific apps from detection
- Discord Mirroring Fix (experimental)
- Runs silently in the system tray
- Saves all settings automatically

## License
This project is licensed under CC BY-NC 4.0.
You may not use this for commercial purposes without permission.

## Dependencies
- [ProcTap](https://github.com/m96-chan/ProcTap) — per-process audio capture (MIT License)
- [python-vlc](https://github.com/oaubert/python-vlc) — VLC audio engine
- [pycaw](https://github.com/AndreMiras/pycaw) — Windows audio session control
- [pystray](https://github.com/moses-palmer/pystray) — system tray support