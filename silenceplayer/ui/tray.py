"""The tray icon and Silence Player's own right-click menu.
"""
import sys
import tkinter as tk

import pystray
from PIL import Image, ImageDraw

from ..logs import log
from ..paths import resource_path
from . import theme


def create_tray_icon():
    try:
        img = Image.open(resource_path("trayicon.png")).resize((64, 64))
        return img
    except Exception:
        log.warning("Could not load trayicon.png; using the built-in icon", exc_info=True)
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([0, 0, 63, 63], fill=theme.BASE)
        draw.rectangle([24, 18, 30, 42], fill=theme.BLUE)
        draw.rectangle([30, 18, 44, 26], fill=theme.BLUE)
        draw.rectangle([38, 26, 44, 42], fill=theme.BLUE)
        draw.ellipse([18, 38, 32, 48], fill=theme.BLUE)
        draw.ellipse([32, 38, 46, 48], fill=theme.BLUE)
        return img


def make_tray_icon(name, image, title, menu, on_right_click=None):
    """A pystray icon whose right-click opens Silence Player's own menu.

    Windows draws the standard tray menu itself (no colours, no two-line
    header), so a right-click calls `on_right_click` instead. Left-click and
    the rest are pystray's. If `on_right_click` fails, the standard menu built
    from `menu` is shown, so the tray never becomes unusable.
    """
    base = pystray.Icon

    class TrayIcon(base):
        WM_RBUTTONUP = 0x0205

        def _on_notify(self, wparam, lparam):
            if on_right_click is not None and lparam == self.WM_RBUTTONUP:
                try:
                    on_right_click()
                    return None
                except Exception:
                    log.exception("Custom tray menu failed; showing the Windows menu")
            return super()._on_notify(wparam, lparam)

    return TrayIcon(name, image, title, menu)


def round_window_corners(win, border="#3d3d3d"):
    """Windows 11: small rounded corners and a border colour, like system menus.
    Does nothing on Windows 10 or other systems."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        hwnd = user32.GetAncestor(win.winfo_id(), 2)          # GA_ROOT
        corner = ctypes.c_int(3)                                # DWMWCP_ROUNDSMALL
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        r, g, b = (int(border[i:i + 2], 16) for i in (1, 3, 5))
        color = ctypes.c_uint(r | (g << 8) | (b << 16))         # COLORREF
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))
    except Exception:
        log.debug("Could not round the menu corners", exc_info=True)


class TrayMenu:
    """Silence Player's own right-click menu for the tray icon.

    A borderless Tk window: dark, rounded on Windows 11, with a coloured
    "Now playing" header. It closes when an item is picked, on Escape, when it
    loses focus, or when the mouse is clicked anywhere outside it.
    """

    BG, BORDER, FG, HOVER, SEP = "#2c2c2c", "#3d3d3d", "#f0f0f0", "#383838", "#3d3d3d"
    HEAD_COLORS = {"playing": theme.ACTIVE, "ducked": theme.YELLOW, "paused": theme.BLUE,
                   "waiting": "#9a9a9a", "off": "#9a9a9a"}
    MIN_WIDTH = 230
    POLL_MS = 40

    def __init__(self, root):
        self.root = root
        self.win = None
        self._head = self._line = None

    @property
    def is_open(self):
        return self.win is not None

    def show(self, head, line, kind, items, at=None):
        """items: (text, callback) tuples, or None for a separator."""
        self.close()
        win = tk.Toplevel(self.root, bg=self.BORDER)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self.win = win
        body = tk.Frame(win, bg=self.BG, padx=5, pady=5)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=self.BG, padx=10, pady=6)
        header.pack(fill="x")
        self._head = tk.Label(header, bg=self.BG, font=("Segoe UI", 9), anchor="w")
        self._head.pack(fill="x")
        self._line = tk.Label(header, bg=self.BG, fg=self.FG, font=("Segoe UI", 10),
                              anchor="w")
        self._line.pack(fill="x")
        self.update_header(head, line, kind)
        self._separator(body)

        for item in items:
            if item is None:
                self._separator(body)
                continue
            text, callback = item
            lbl = tk.Label(body, text=text, bg=self.BG, fg=self.FG, font=("Segoe UI", 10),
                           anchor="w", padx=10, pady=5, cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Enter>", lambda e, w=lbl: w.config(bg=self.HOVER))
            lbl.bind("<Leave>", lambda e, w=lbl: w.config(bg=self.BG))
            lbl.bind("<ButtonRelease-1>", lambda e, cb=callback: self._pick(cb))

        win.update_idletasks()
        w = max(self.MIN_WIDTH, win.winfo_reqwidth())
        h = win.winfo_reqheight()
        px, py = at if at is not None else self.root.winfo_pointerxy()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # Like the Windows menu: open up and to the left of the pointer when
        # there's no room below/right of it (the taskbar is usually at the bottom).
        x = px - w if px + w > sw else px
        y = py - h if py + h > sh else py
        win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        win.deiconify()
        round_window_corners(win, self.BORDER)
        win.lift()
        win.focus_force()
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<FocusOut>", lambda e: self.root.after(60, self._close_if_unfocused))
        self.root.after(self.POLL_MS, self._watch_clicks)

    def _separator(self, parent):
        tk.Frame(parent, bg=self.SEP, height=1).pack(fill="x", pady=4)

    def update_header(self, head, line, kind):
        if self.win is None:
            return
        self._head.config(text=head, fg=self.HEAD_COLORS.get(kind, "#9a9a9a"))
        self._line.config(text=line)

    def _pick(self, callback):
        self.close()
        try:
            callback()
        except Exception:
            log.exception("Tray menu action failed")

    def _close_if_unfocused(self):
        win = self.win
        if win is None:
            return
        try:
            focus = win.focus_displayof()
            keep = focus is not None and focus.winfo_toplevel() is win
        except Exception:
            keep = False
        if not keep:
            self.close()

    def _watch_clicks(self):
        """Close on a click outside the menu (works even without keyboard focus)."""
        win = self.win
        if win is None:
            return
        if sys.platform == "win32":
            try:
                import ctypes
                state = ctypes.windll.user32.GetAsyncKeyState
                pressed = any(state(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))
                if pressed:
                    px, py = win.winfo_pointerxy()
                    x0, y0 = win.winfo_rootx(), win.winfo_rooty()
                    inside = x0 <= px < x0 + win.winfo_width() and y0 <= py < y0 + win.winfo_height()
                    if not inside:
                        self.close()
                        return
            except Exception:
                log.debug("Tray menu click watch failed", exc_info=True)
        self.root.after(self.POLL_MS, self._watch_clicks)

    def close(self):
        win, self.win = self.win, None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
