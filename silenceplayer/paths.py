"""Where Silence Player's files are.

APP_DIR     folder of silenceplayer.exe (installed or portable), or of app.py
            when running from source. config.json lives here.
BUNDLE_DIR  where the bundled files are (VLC, icons): PyInstaller's
            _internal folder in the EXE build, the source folder otherwise.
LAUNCHER    app.py, the script "Start with Windows" runs from source.
"""
import os
import sys

from . import APP_NAME

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys, "_MEIPASS"):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    BUNDLE_DIR = sys._MEIPASS
else:
    APP_DIR = SOURCE_DIR
    BUNDLE_DIR = SOURCE_DIR

LAUNCHER = os.path.join(SOURCE_DIR, "app.py")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")


def resource_path(filename):
    """A file that ships with Silence Player (icons, VLC)."""
    return os.path.join(BUNDLE_DIR, filename)


def log_dir():
    """%LOCALAPPDATA%\\SilencePlayer: the log and the update check's memory."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)
