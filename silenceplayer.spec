# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build for Silence Player (one-file, no console).
#
#   pyinstaller --clean --noconfirm silenceplayer.spec
#
# Or run build.ps1, which also creates a clean venv and runs the tests.
# Output: dist\silenceplayer.exe  (the name app.py treats as "itself", and
# the name every release so far has used).
from pathlib import Path

ROOT = Path(SPECPATH)

# The VLC engine that gets bundled: VLC 3.0.23 (x64), committed to this repo.
# Listed explicitly so the build never picks up a system VLC install.
# app.py points python-vlc at these via PYTHON_VLC_LIB_PATH/MODULE_PATH.
#
# Only the plugins needed to play local audio are bundled (24 MB instead of
# the full 133 MB plugins/ folder: no video, network, discs, GUI, Lua...).
# The list comes from tracing which modules VLC loads for MP3, Ogg Opus,
# WebM Opus, M4A (AAC), FLAC, MP4 and WAV, plus Windows audio outputs,
# resamplers/channel mixers and AAC/ALAC/AC-3 fallbacks (avcodec).
# To support another format, add its plugin here; the repo keeps the full set.
VLC_PLUGINS = {
    "access":        ["filesystem"],
    "audio_output":  ["mmdevice", "wasapi", "directsound", "waveout", "adummy"],
    "audio_mixer":   ["float_mixer", "integer_mixer"],
    "audio_filter":  ["audio_format", "remap", "samplerate", "speex_resampler",
                      "ugly_resampler", "scaletempo", "simple_channel_mixer",
                      "trivial_channel_mixer", "headphone_channel_mixer"],
    "codec":         ["mpg123", "opus", "flac", "faad", "avcodec", "araw", "vorbis"],
    "demux":         ["es", "mp4", "ogg", "flacsys", "mkv", "wav"],
    "packetizer":    ["packetizer_mpegaudio", "packetizer_mpeg4audio", "packetizer_flac",
                      "packetizer_copy", "packetizer_a52"],
    "stream_filter": ["cache_read", "cache_block", "prefetch", "record", "skiptags"],
    "keystore":      ["memory_keystore"],
    "logger":        ["console_logger", "file_logger"],
}

VLC_FILES = [
    (str(ROOT / "libvlc.dll"), "."),
    (str(ROOT / "libvlccore.dll"), "."),
]
for category, names in VLC_PLUGINS.items():
    for name in names:
        VLC_FILES.append((str(ROOT / "plugins" / category / f"lib{name}_plugin.dll"),
                          f"plugins/{category}"))

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
    a.binaries,
    a.datas,
    [],
    name="silenceplayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon=str(ROOT / "icon.png"),   # converted to .ico by PyInstaller (needs Pillow)
)
