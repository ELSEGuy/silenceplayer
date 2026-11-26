"""Logging: the rotating log file and helpers that keep repeated errors quiet.
"""
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from .paths import APP_DIR, log_dir


log = logging.getLogger("silenceplayer")


LOG_MAX_BYTES    = 1_000_000


LOG_BACKUP_COUNT = 3


class _ThrottleFilter(logging.Filter):
    """Lets the same message template through at most once per `interval` seconds.

    Only applies to loggers under `prefix` (ProcTap), whose worker loops can
    log the same error many times per second. Attached to handlers, because
    logger-level filters don't see records from child loggers.
    """

    def __init__(self, prefix, interval=60.0):
        super().__init__()
        self.prefix = prefix
        self.interval = interval
        self._last = {}
        self._lock = threading.Lock()

    def filter(self, record):
        if not (record.name == self.prefix or record.name.startswith(self.prefix + ".")):
            return True
        key = (record.name, record.levelno, record.msg)
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.interval:
                return False
            self._last[key] = now
        return True


_throttle_lock = threading.Lock()


_throttle_last = {}


def log_throttled(key, level, msg, *args, interval=60.0, **kwargs):
    """Log at most once per `interval` seconds for a given key.

    The monitor loop runs every 500 ms, so a persistent failure (for example
    pycaw breaking) must not write two log lines per second forever.
    """
    now = time.monotonic()
    with _throttle_lock:
        last, suppressed = _throttle_last.get(key, (None, 0))
        if last is not None and now - last < interval:
            _throttle_last[key] = (last, suppressed + 1)
            return
        _throttle_last[key] = (now, 0)
    if suppressed:
        msg = msg + " (%d similar messages suppressed)"
        args = args + (suppressed,)
    log.log(level, msg, *args, **kwargs)


def setup_logging():
    """Configure a rotating log file. Never raises: logging must fail safely."""
    if getattr(setup_logging, "_done", False):
        return
    setup_logging._done = True

    logging.raiseExceptions = False   # a broken log file must never crash the app
    log.setLevel(logging.DEBUG if os.environ.get("SILENCEPLAYER_DEBUG") else logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s")

    handlers = []
    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(d, "silenceplayer.log"),
                                 maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
                                 encoding="utf-8", delay=True)
        handlers.append(fh)
    except Exception:
        pass
    if not hasattr(sys, "_MEIPASS"):
        # Running from source: also keep a copy next to app.py for easy access.
        try:
            handlers.append(RotatingFileHandler(
                os.path.join(APP_DIR, "silenceplayer.log"),
                maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8", delay=True))
        except Exception:
            pass
        if sys.stderr is not None:
            handlers.append(logging.StreamHandler(sys.stderr))
    if not handlers:
        handlers.append(logging.NullHandler())

    throttle = _ThrottleFilter("proctap", 60.0)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(throttle)
        log.addHandler(h)

    # ProcTap logs through its own loggers; keep its warnings/errors, throttled.
    pt_log = logging.getLogger("proctap")
    pt_log.setLevel(logging.WARNING)
    pt_log.propagate = False
    for h in handlers:
        pt_log.addHandler(h)


def install_exception_hooks():
    def _sys_hook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.critical("Unhandled exception in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
