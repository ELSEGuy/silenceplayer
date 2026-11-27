"""Single instance: only one Silence Player per Windows session.
"""
import sys
import threading

from .logs import log


class SingleInstance:
    """Allows one Silence Player per Windows login session.

    Uses a named kernel mutex: Windows destroys it automatically when the
    owning process exits or crashes, so there is no stale lock to clean up.
    A second launch signals a named event, which makes the running instance
    open its Settings window, and then exits.
    """

    MUTEX_NAME = "Local\\SilencePlayer.SingleInstance"
    EVENT_NAME = "Local\\SilencePlayer.ShowSettings"
    ERROR_ALREADY_EXISTS = 183
    ERROR_ACCESS_DENIED  = 5
    WAIT_OBJECT_0        = 0

    def __init__(self):
        self._k32 = None
        self._mutex = None
        self._event = None
        self._stop = threading.Event()
        self._listener = None

    def _kernel32(self):
        if self._k32 is None:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CreateEventW.restype = wintypes.HANDLE
            k32.SetEvent.argtypes = [wintypes.HANDLE]
            k32.SetEvent.restype = wintypes.BOOL
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            self._ctypes = ctypes
            self._k32 = k32
        return self._k32

    def acquire(self):
        """True if this process is the only instance (or the check isn't possible)."""
        if sys.platform != "win32":
            return True
        try:
            k32 = self._kernel32()
            handle = k32.CreateMutexW(None, False, self.MUTEX_NAME)
            err = self._ctypes.get_last_error()
            if not handle:
                if err == self.ERROR_ACCESS_DENIED:
                    # Exists, created by an instance running with other rights.
                    self._signal_existing()
                    return False
                log.error("CreateMutexW failed (error %s); continuing without "
                          "single-instance protection", err)
                return True
            if err == self.ERROR_ALREADY_EXISTS:
                k32.CloseHandle(handle)
                self._signal_existing()
                return False
            self._mutex = handle
            self._event = k32.CreateEventW(None, False, False, self.EVENT_NAME)
            if not self._event:
                log.warning("CreateEventW failed (error %s); a second launch won't "
                            "be able to open Settings", self._ctypes.get_last_error())
            return True
        except Exception:
            log.exception("Single-instance check failed; continuing")
            return True

    def _signal_existing(self):
        try:
            k32 = self._kernel32()
            ev = k32.CreateEventW(None, False, False, self.EVENT_NAME)
            if ev:
                k32.SetEvent(ev)
                k32.CloseHandle(ev)
        except Exception:
            log.exception("Could not signal the running instance")

    def listen(self, on_show_requested):
        """Call `on_show_requested()` (from a worker thread) when a second launch happens."""
        if not self._event:
            return
        def loop():
            k32 = self._k32
            while not self._stop.is_set():
                try:
                    if k32.WaitForSingleObject(self._event, 500) == self.WAIT_OBJECT_0:
                        if not self._stop.is_set():
                            on_show_requested()
                except Exception:
                    log.exception("Single-instance listener failed")
                    return
        self._listener = threading.Thread(target=loop, daemon=True, name="InstanceListener")
        self._listener.start()

    def release(self):
        self._stop.set()
        if self._listener is not None and self._listener is not threading.current_thread():
            self._listener.join(timeout=2.0)
        k32 = self._k32
        if k32 is None:
            return
        for attr in ("_event", "_mutex"):
            h = getattr(self, attr)
            if h:
                try:
                    k32.CloseHandle(h)
                except Exception:
                    pass
                setattr(self, attr, None)
