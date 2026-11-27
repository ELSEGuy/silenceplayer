"""Program start: logging, single-instance check, then the window.
"""
import os
import sys
import tkinter as tk
from tkinter import messagebox

from . import APP_VERSION
from .instance import SingleInstance
from .logs import install_exception_hooks, log, setup_logging
from .mirror import PROCTAP_IMPORT_ERROR
from .paths import log_dir
from .ui.window import App
from .winaudio import ensure_mta

# The Tk window and the App, alive until exit (see main).
_KEEP_ALIVE = []


def main():
    setup_logging()
    install_exception_hooks()
    log.info("Silence Player %s starting (python %s, frozen=%s)",
             APP_VERSION, sys.version.split()[0], hasattr(sys, "_MEIPASS"))
    ensure_mta()
    if PROCTAP_IMPORT_ERROR is not None:
        log.warning("ProcTap unavailable; Mirroring Fix disabled: %s", PROCTAP_IMPORT_ERROR)

    instance = SingleInstance()
    if not instance.acquire():
        log.info("Another instance is already running; asked it to open Settings")
        return 0
    try:
        root = tk.Tk()
        try:
            # Kept referenced until the process ends: if Python's garbage
            # collector ever freed the window on a worker thread (it runs on
            # whichever thread triggers it), Tcl would abort the process
            # ("Tcl_AsyncDelete: async handler deleted by the wrong thread").
            _KEEP_ALIVE.extend((root, App(root, instance)))
        except Exception as e:
            log.critical("Startup failed", exc_info=True)
            root.withdraw()
            messagebox.showerror(
                "Silence Player could not start",
                f"{e}\n\nDetails were written to:\n{os.path.join(log_dir(), 'silenceplayer.log')}")
            root.destroy()
            return 1
        root.mainloop()
    finally:
        instance.release()
        log.info("Silence Player exited")
    return 0
