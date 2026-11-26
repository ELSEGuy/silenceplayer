"""Where Silence Player's files are.

APP_DIR     folder of silenceplayer.exe (installed or portable), or of app.py
            when running from source.
BUNDLE_DIR  where the bundled files are (VLC, icons): PyInstaller's
            _internal folder in the EXE build, the source folder otherwise.
LAUNCHER    app.py, the script "Start with Windows" runs from source.
CONFIG_FILE config.json next to the app, like before. Only if Windows won't
            let Silence Player write there (for example if someone copies it
            into Program Files) does it use %APPDATA%\\SilencePlayer instead.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

from . import APP_NAME

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys, "_MEIPASS"):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    BUNDLE_DIR = sys._MEIPASS
else:
    APP_DIR = SOURCE_DIR
    BUNDLE_DIR = SOURCE_DIR

LAUNCHER = os.path.join(SOURCE_DIR, "app.py")


def user_data_dir() -> str:
    """%APPDATA%\\SilencePlayer: settings, when the app's own folder is read-only."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def is_writable(folder: str) -> bool:
    """True if a file can be created (and removed) in `folder`."""
    try:
        fd, probe = tempfile.mkstemp(prefix=".write-test-", dir=folder)
        os.close(fd)
        os.remove(probe)
        return True
    except OSError:
        return False


def choose_config_file(app_dir: str | None = None, data_dir: str | None = None) -> str:
    """Where config.json is read from and saved to (see the module docstring)."""
    app_dir = app_dir or APP_DIR
    beside = os.path.join(app_dir, "config.json")
    if is_writable(app_dir):
        return beside
    fallback = os.path.join(data_dir or user_data_dir(), "config.json")
    if os.path.isfile(beside) and not os.path.exists(fallback):
        try:                                    # keep the settings that came with it
            os.makedirs(os.path.dirname(fallback), exist_ok=True)
            shutil.copy2(beside, fallback)
        except OSError:
            pass
    return fallback


CONFIG_FILE = choose_config_file()


def resource_path(filename: str) -> str:
    """A file that ships with Silence Player (icons, VLC)."""
    return os.path.join(BUNDLE_DIR, filename)


def log_dir() -> str:
    """%LOCALAPPDATA%\\SilencePlayer: the log and the update check's memory."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)
