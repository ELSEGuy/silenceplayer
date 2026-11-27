"""The ambient sound source: supported files, playlists, and where to resume.
"""
from __future__ import annotations

import bisect
import glob
import os
from typing import NamedTuple


def display_name(path: str) -> str:
    """What the Settings window shows for a file or folder: just its name.

    'C:/Users/me/Music/asmr/Animal Crossing Office.m4a' → 'Animal Crossing Office.m4a'
    'C:/Users/me/Music/asmr/'                          → 'asmr'
    'D:/'                                              → 'D:/' (a drive has no name)
    """
    if not path:
        return ""
    name = os.path.basename(os.path.normpath(path))
    return name or path


SUPPORTED_EXTENSIONS = ("*.mp3", "*.opus", "*.m4a", "*.flac", "*.mp4")


SUPPORTED_LABEL = "MP3, OPUS, M4A, FLAC, MP4"


def collect_playlist(folder: str) -> list[str]:
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(glob.glob(os.path.join(folder, ext)))
    # dict.fromkeys de-duplicates while keeping order (glob can be case-insensitive)
    return sorted(dict.fromkeys(f for f in files if os.path.isfile(f)))


def validate_ambient_source(cfg: dict) -> str | None:
    """Return None if the selected ambient source is playable, else a message."""
    mode = cfg.get("mode", "single")
    if mode == "playlist":
        folder = cfg.get("playlist_folder", "")
        if not folder:
            return "Please choose a playlist folder first."
        if not os.path.isdir(folder):
            return f"The playlist folder could not be found:\n{folder}"
        if not collect_playlist(folder):
            return f"The playlist folder doesn't contain any supported audio files ({SUPPORTED_LABEL})."
        return None
    path = cfg.get("mp3_path", "")
    if not path:
        return "Please choose an ambient sound file first."
    if os.path.isdir(path):
        return "The selected path is a folder. Use Playlist mode to play a folder."
    if not os.path.isfile(path):
        return f"The ambient sound file could not be found:\n{path}"
    return None


def same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class ResumeState(NamedTuple):
    mode: str        # "single" or "playlist"
    source: str      # the file (single) or folder (playlist) that was selected
    path: str        # the file that was actually playing
    index: int       # playlist index of `path` (0 in single mode)  # type: ignore[assignment]
    position: float  # seconds into `path`


def plan_single_start(path: str, resume: ResumeState | None) -> float:
    """Seconds to start `path` at. Only resume if it is the same file."""
    if resume is not None and resume.mode == "single" and same_path(resume.path, path):
        return max(0.0, float(resume.position))
    return 0.0


def plan_playlist_start(folder: str, files: list[str],
                        resume: ResumeState | None) -> tuple[int, float]:
    """Return (index, seconds) to start a playlist at.

    - Same folder and the saved track still exists → that track at the saved time.
    - Same folder but the saved track is gone → the track that now sits in its
      place in sort order, from the beginning (never someone else's timestamp).
    - Different folder, other mode, or nothing saved → first track.
    """
    if not files:
        return 0, 0.0
    if resume is None or resume.mode != "playlist" or not same_path(resume.source, folder):
        return 0, 0.0
    for i, f in enumerate(files):
        if same_path(f, resume.path):
            return i, max(0.0, float(resume.position))
    if resume.path:
        i = bisect.bisect_left(files, resume.path)
    else:
        i = resume.index
    if not 0 <= i < len(files):
        i = 0
    return i, 0.0
