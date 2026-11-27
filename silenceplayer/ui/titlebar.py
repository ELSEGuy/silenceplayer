"""The window's title bar in Silence Player's colours (Windows).

Windows draws the title bar, not Tk, so Tk windows get the system's default
(usually light) bar over a dark app. Windows 11 lets an app pick the bar's
background, text and border colours (DwmSetWindowAttribute with
DWMWA_CAPTION_COLOR / DWMWA_TEXT_COLOR / DWMWA_BORDER_COLOR). Windows 10 only
knows "dark mode" (DWMWA_USE_IMMERSIVE_DARK_MODE), which at least makes the bar
dark. Elsewhere this does nothing.
"""
import sys

from ..logs import log
from . import theme

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19     # Windows 10 before version 20H1
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


def colorref(colour):
    """'#rrggbb' → Windows COLORREF (0x00bbggrr)."""
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def color_title_bar(win, caption=theme.BG, text=theme.TEXT, border=theme.CARD):
    """Colour `win`'s title bar. Returns True if Windows accepted the colours
    (Windows 11), False otherwise (older Windows: dark bar only, or not Windows)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        win.update_idletasks()                                 # the window must exist
        hwnd = user32.GetAncestor(win.winfo_id(), 2)           # GA_ROOT: the real frame
        dark = ctypes.c_int(1)
        for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark),
                                         ctypes.sizeof(dark)) == 0:
                break
        # Keep Windows 11's rounded window corners (the window is see-through
        # capable for a flash-free reveal, which could otherwise drop them).
        corner = ctypes.c_int(DWMWCP_ROUND)
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner),
                                  ctypes.sizeof(corner))
        accepted = True
        for attr, colour in ((DWMWA_CAPTION_COLOR, caption), (DWMWA_TEXT_COLOR, text),
                             (DWMWA_BORDER_COLOR, border)):
            value = ctypes.c_uint(colorref(colour))
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                         ctypes.sizeof(value)) != 0:
                accepted = False                               # before Windows 11
        return accepted
    except Exception:
        log.debug("Could not colour the title bar", exc_info=True)
        return False
