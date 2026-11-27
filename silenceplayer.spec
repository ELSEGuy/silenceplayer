# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build for Silence Player (one folder, no console).
#
#   pyinstaller --clean --noconfirm silenceplayer.spec
#
# Or run build.ps1, which also creates a clean venv, runs the tests, zips the
# portable version and builds the installer.
#
# Output: dist\SilencePlayer\silenceplayer.exe, with the Python runtime, VLC
# and the icons in dist\SilencePlayer\_internal. A folder starts faster than
# a single self-extracting EXE and is less likely to be flagged by antivirus;
# the installer (installer\SilencePlayer.iss) copies this folder.
from pathlib import Path

ROOT = Path(SPECPATH)

# The VLC engine that gets bundled: VLC 3.0.24 (x64), from the official
# win64 zip (see tools\update_vlc.py), committed to this repo.
# Listed explicitly so the build never picks up a system VLC install.
# silenceplayer\player.py points python-vlc at these files.
#
# Only the plugins needed to play local audio are bundled (no video, network,
# discs, GUI, Lua...). The list comes from tracing which modules VLC loads for
# MP3, Ogg Opus, WebM Opus, M4A (AAC), FLAC, MP4 and WAV, plus Windows audio
# outputs, resamplers/channel mixers and AAC/ALAC/AC-3 fallbacks (avcodec).
# The repo only keeps these files. To support another format, add its plugin
# here and run tools\update_vlc.py again.
import sys
sys.path.insert(0, str(ROOT / "tools"))
from vlc_files import vlc_files   # the list of shipped VLC files (one place)

VLC_FILES = [(str(ROOT / rel), str(Path(rel).parent)) for rel in vlc_files()]

for src, _ in VLC_FILES:
    if not Path(src).exists():
        raise SystemExit(f"Missing bundled VLC file: {src}")

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "icon.png"), "."),
        (str(ROOT / "trayicon.png"), "."),
        *VLC_FILES,
    ],
    hiddenimports=[
        "pystray._win32",            # pystray picks its backend at runtime
        "proctap.backends.windows",  # ProcTap loads its backend lazily
        "proctap._native",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # scipy comes with ProcTap but is only used if Windows can't capture in
    # 48 kHz float32; then that one app's capture fails safely (it just counts
    # as playing) instead of bundling ~30 MB of scipy.
    excludes=["scipy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="silenceplayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SilencePlayer",
)
