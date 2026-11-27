"""Which VLC files Silence Player ships (read by silenceplayer.spec and
tools/update_vlc.py, so the list lives in one place)."""

VLC_VERSION = "3.0.24"

# Only what's needed to play local audio files; see silenceplayer.spec.
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


def vlc_files():
    """Paths of every shipped VLC file, relative to the repo root (forward slashes)."""
    files = ["libvlc.dll", "libvlccore.dll"]
    for category, names in VLC_PLUGINS.items():
        files += [f"plugins/{category}/lib{name}_plugin.dll" for name in names]
    return files
