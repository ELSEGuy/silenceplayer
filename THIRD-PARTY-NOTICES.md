# Third-party notices

Silence Player ships with the open-source software below. Each part keeps its
own license: Silence Player's license (`LICENSE`, or `LICENSE.txt` in the app
folder) doesn't apply to it.

In the app folder, next to `silenceplayer.exe`, this file is
`THIRD-PARTY-NOTICES.txt` and the full license texts are in the `licenses`
folder.

## VLC 3.0.24

The audio engine: `libvlc.dll`, `libvlccore.dll` and the audio plugins in the
`plugins` folder (listed in `tools/vlc_files.py` in the source code),
unmodified, from the official 64-bit build of VLC media player 3.0.24 by
VideoLAN (https://www.videolan.org/).

- `libvlc.dll`, `libvlccore.dll` and most plugins: GNU Lesser General Public
  License, version 2.1 or later (`licenses/VLC/COPYING.LIB.txt`).
- `libfaad_plugin.dll` (it contains the FAAD2 decoder),
  `libheadphone_channel_mixer_plugin.dll` and `libfile_logger_plugin.dll`:
  GNU General Public License, version 2 or later (`licenses/VLC/COPYING.txt`).
- The plugins also contain libraries such as FFmpeg, mpg123, FLAC, Opus,
  Vorbis and libmatroska, each under the LGPL or a permissive license.

Source code of this version: https://download.videolan.org/pub/videolan/vlc/3.0.24/vlc-3.0.24.tar.xz
(or the `3.0.24` tag of https://code.videolan.org/videolan/vlc). The libraries
built into the plugins are listed, with where to get them, in its `contrib/src`
folder, and VideoLAN offers them as tarballs on
https://www.videolan.org/vlc/download-sources.html.

## Python 3.12

The Python runtime, by the Python Software Foundation, under the PSF License
Agreement. `licenses/Python/LICENSE.txt` is Python's own license file; it also
covers what Python's Windows build includes (the Microsoft C runtime, OpenSSL,
libffi, bzip2 and Tcl/Tk).

## Python packages

| Package | Used for | License |
|---|---|---|
| [`comtypes`](https://github.com/enthought/comtypes) | the Windows audio APIs (COM) | MIT |
| [`numpy`](https://numpy.org/) (NumPy) | sound levels for the Mirroring Fix | BSD-3-Clause and other permissive licenses. Its Windows build also contains OpenBLAS and LAPACK (BSD-style) and the GCC runtime library (GPL 3 or later with the GCC Runtime Library Exception) |
| [`pillow`](https://python-pillow.org/) (Pillow) | the tray icon images | MIT-CMU. Its Windows build also contains image libraries under their own licenses, all listed in its license file |
| [`proc-tap`](https://github.com/m96-chan/ProcTap) (ProcTap) | per-app capture for the Mirroring Fix | MIT |
| [`psutil`](https://github.com/giampaolo/psutil) | app names | BSD-3-Clause |
| [`pycaw`](https://github.com/AndreMiras/pycaw) | which apps are making sound | MIT |
| [`pystray`](https://github.com/moses-palmer/pystray) | the tray icon | GNU LGPL, version 3 |
| [`python-vlc`](https://github.com/oaubert/python-vlc) | talking to VLC | GNU LGPL, version 2.1 or later |
| [`six`](https://github.com/benjaminp/six) | used by pystray | MIT |

Their license files, for the exact versions in the app, are in
`licenses/<package>-<version>`.

## PyInstaller

`silenceplayer.exe` is built with [`pyinstaller`](https://pyinstaller.org/)
(PyInstaller). The parts of it inside the app (the bootloader and loader) are
under the GNU GPL, version 2 or later, with the PyInstaller Bootloader
Exception, which allows distributing them with any program. Its run-time hooks
are under the Apache License 2.0. Both are in `licenses/pyinstaller-<version>`.
