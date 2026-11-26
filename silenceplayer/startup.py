"""Start with Windows: the per-user Run registry entry.
"""
import os
import sys

from .logs import log
from .paths import LAUNCHER


class _WinRegistry:
    """String values under HKEY_CURRENT_USER. Windows only (needs winreg)."""

    def __init__(self):
        import winreg                      # ImportError off Windows
        self._w = winreg

    def get(self, path, name):
        """The value (str or bytes), or None if the key or value doesn't exist."""
        w = self._w
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, path, 0, w.KEY_READ) as key:
                return w.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, path, name, value):
        w = self._w
        with w.CreateKeyEx(w.HKEY_CURRENT_USER, path, 0, w.KEY_SET_VALUE) as key:
            w.SetValueEx(key, name, 0, w.REG_SZ, value)

    def delete(self, path, name):
        w = self._w
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, path, 0, w.KEY_SET_VALUE) as key:
                w.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def startup_command():
    """The command line Windows should run at sign-in."""
    exe = sys.executable
    if hasattr(sys, "_MEIPASS"):                 # the packaged EXE
        return f'"{exe}"'
    # Running from source: use pythonw so no console window opens.
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.basename(exe).lower() == "python.exe" and os.path.isfile(pythonw):
        exe = pythonw
    return f'"{exe}" "{LAUNCHER}"'


class StartupEntry:
    """Turns "start with Windows" on and off (the per-user Run registry key).

    No admin rights needed, and it only affects the current Windows user.
    Windows also lets people switch startup apps off in Settings → Apps →
    Startup or Task Manager; that is stored separately (StartupApproved) and
    wins over the Run entry, so it is respected when reading and cleared when
    the user turns the option on again here.
    """

    RUN_KEY      = r"Software\Microsoft\Windows\CurrentVersion\Run"
    APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
    VALUE_NAME   = "SilencePlayer"

    def __init__(self, registry=None, command=None):
        if registry is None:
            try:
                registry = _WinRegistry()
            except ImportError:
                registry = None
        self._reg = registry
        self._command = command or startup_command

    @property
    def available(self):
        return self._reg is not None

    def _registered_command(self):
        value = self._reg.get(self.RUN_KEY, self.VALUE_NAME)
        return value if isinstance(value, str) and value.strip() else None

    def _blocked_by_windows(self):
        """True if the user switched it off in Windows' startup settings."""
        data = self._reg.get(self.APPROVED_KEY, self.VALUE_NAME)
        return isinstance(data, (bytes, bytearray)) and len(data) > 0 and bool(data[0] & 1)

    def status(self):
        """'unavailable', 'off', 'on', or 'blocked' (registered, but switched off in Windows)."""
        if not self.available:
            return "unavailable"
        if self._registered_command() is None:
            return "off"
        return "blocked" if self._blocked_by_windows() else "on"

    def is_enabled(self):
        return self.status() == "on"

    def set_enabled(self, enabled):
        """Raises OSError if Windows refuses; the caller reports it."""
        if not self.available:
            raise OSError("Start with Windows is only available on Windows")
        if enabled:
            self._reg.set(self.RUN_KEY, self.VALUE_NAME, self._command())
            self._reg.delete(self.APPROVED_KEY, self.VALUE_NAME)   # clear a "disabled" mark
            log.info("Start with Windows: on (%s)", self._command())
        else:
            self._reg.delete(self.RUN_KEY, self.VALUE_NAME)
            self._reg.delete(self.APPROVED_KEY, self.VALUE_NAME)
            log.info("Start with Windows: off")

    def repair_path(self):
        """If the packaged EXE was moved or replaced, point the entry at its new
        location so Start with Windows keeps working. Returns True if changed."""
        if not self.available or not hasattr(sys, "_MEIPASS"):
            return False
        registered = self._registered_command()
        wanted = self._command()
        if registered is None or registered == wanted:
            return False
        self._reg.set(self.RUN_KEY, self.VALUE_NAME, wanted)
        log.info("Start with Windows: updated the path (%s → %s)", registered, wanted)
        return True
