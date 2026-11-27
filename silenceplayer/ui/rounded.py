"""Apple-style continuous ("squircle") corners for Tkinter.

Tk can only draw rectangles, and its canvas shapes aren't anti-aliased on
Windows. So every rounded shape here is drawn with Pillow at 4x size and
reduced by area averaging (smooth edges), always at its exact pixel size, never
a stretched image, and shown as a Tk image.

The corner is Apple's continuous corner (iOS 7 and later; in code
CALayerCornerCurve.continuous) with the control points PaintCode measured from
UIKit. A plain rounded corner is a quarter circle that meets the straight edge
abruptly; this one starts bending 1.52866 x the radius away from the corner and
tightens gradually (three cubic Bezier curves per corner), so there is no
visible point where the edge turns into a curve.
https://www.paintcodeapp.com/news/code-for-ios-7-rounded-rectangles

Pills (slider tracks, the progress bar, knobs) are true capsules with
semicircle ends, as Apple draws capsules.
"""
from __future__ import annotations

import math
import tkinter as tk
from functools import lru_cache

from PIL import Image, ImageColor, ImageDraw, ImageTk

from . import theme

SUPERSAMPLE = 4
EXTENT = 1.52866483          # how far each corner reaches along the edges, x radius

# One radius scale for the whole app (pixels), so every corner matches.
RADIUS_CARD = 14             # cards and banners
RADIUS_CONTROL = 8           # buttons, tabs, toggles, fields, lists
RADIUS_SMALL = 5             # the small status badge

# Top-left corner (the corner itself at 0,0; unit radius), from the left edge
# to the top edge. "C" = cubic Bezier (control 1, control 2, end), "L" = line.
_CORNER = (
    ("C", (0.0, 1.08849299), (0.0, 0.86840701), (0.06549600, 0.66993397)),
    ("L", (0.07491100, 0.63149399)),
    ("C", (0.16906001, 0.37282401), (0.37282401, 0.16906001), (0.63149399, 0.07491100)),
    ("L", (0.66993397, 0.06549600)),
    ("C", (0.86840701, 0.0), (1.08849299, 0.0), (EXTENT, 0.0)),
)


@lru_cache(maxsize=8)
def _unit_corner(steps=16):
    cur = (0.0, EXTENT)
    pts = [cur]
    for seg in _CORNER:
        if seg[0] == "L":
            cur = seg[1]
            pts.append(cur)
            continue
        (x1, y1), (x2, y2), (x3, y3) = seg[1:]
        x0, y0 = cur
        for i in range(1, steps + 1):
            t = i / steps
            m = 1 - t
            pts.append((m * m * m * x0 + 3 * m * m * t * x1 + 3 * m * t * t * x2 + t * t * t * x3,
                        m * m * m * y0 + 3 * m * m * t * y1 + 3 * m * t * t * y2 + t * t * t * y3))
        cur = (x3, y3)
    return tuple(pts)


def max_radius(w, h):
    """Largest radius whose corners still fit (they reach EXTENT x radius)."""
    return min(w, h) / 2 / EXTENT


def continuous_rect(w, h, radius):
    """Outline of a w x h rectangle with continuous corners, clockwise."""
    r = max(0.0, min(float(radius), max_radius(w, h)))
    if r <= 0:
        return [(0, 0), (w, 0), (w, h), (0, h)]
    tl = [(x * r, y * r) for x, y in _unit_corner()]       # left edge → top edge
    tr = [(w - x, y) for x, y in reversed(tl)]             # top edge → right edge
    br = [(w - x, h - y) for x, y in tl]                   # right edge → bottom edge
    bl = [(x, h - y) for x, y in reversed(tl)]             # bottom edge → left edge
    return tl + tr + br + bl


def capsule(w, h, steps=32):
    """Outline of a pill: semicircle ends on the short sides."""
    r = min(w, h) / 2
    pts = []
    if w >= h:
        for i in range(steps + 1):                         # right end
            a = -math.pi / 2 + math.pi * i / steps
            pts.append((w - r + r * math.cos(a), r + r * math.sin(a)))
        for i in range(steps + 1):                         # left end
            a = math.pi / 2 + math.pi * i / steps
            pts.append((r + r * math.cos(a), r + r * math.sin(a)))
    else:
        for i in range(steps + 1):                         # bottom end
            a = math.pi * i / steps
            pts.append((r + r * math.cos(a), h - r + r * math.sin(a)))
        for i in range(steps + 1):                         # top end
            a = math.pi + math.pi * i / steps
            pts.append((r + r * math.cos(a), r + r * math.sin(a)))
    return pts


@lru_cache(maxsize=1024)
def shape_image(w, h, fill, outer=None, radius=0, kind="continuous"):
    """Anti-aliased shape as a Pillow image. outer=None: transparent around it
    (RGBA, for canvas items that overlap); otherwise drawn on that colour."""
    w, h = max(1, int(w)), max(1, int(h))
    ss = SUPERSAMPLE
    rgb = ImageColor.getrgb(fill)[:3]
    if outer is None:
        # Transparent pixels carry the fill colour so averaging doesn't darken edges.
        img = Image.new("RGBA", (w * ss, h * ss), rgb + (0,))
        colour = rgb + (255,)
    else:
        img = Image.new("RGB", (w * ss, h * ss), ImageColor.getrgb(outer)[:3])
        colour = rgb
    pts = capsule(w, h) if kind == "capsule" else continuous_rect(w, h, radius)
    ImageDraw.Draw(img).polygon([(x * ss, y * ss) for x, y in pts], fill=colour)
    return img.reduce(ss)


def tk_colour(widget, colour):
    """`colour` as #rrggbb. Tk also accepts names PIL doesn't know, like
    Windows' "SystemButtonFace" (a plain tk.Tk()'s background)."""
    if colour is None or (isinstance(colour, str) and colour.startswith("#")):
        return colour
    try:
        r, g, b = widget.winfo_rgb(colour)
    except tk.TclError:
        return colour
    return f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x}"


def _cache(widget):
    root = widget._root()
    cache = root.__dict__.get("_rounded_images")
    if cache is None or len(cache) > 3000:
        # Widgets keep their own reference to the image they show, so dropping
        # the cache never blanks anything on screen.
        cache = root.__dict__["_rounded_images"] = {}
    return root, cache


def photo(widget, w, h, fill, outer=None, radius=0, kind="continuous"):
    """Tk image of shape_image(), cached per Tk interpreter."""
    root, cache = _cache(widget)
    fill, outer = tk_colour(widget, fill), tk_colour(widget, outer)
    key = ("shape", int(w), int(h), fill, outer, radius, kind)
    img = cache.get(key)
    if img is None:
        img = cache[key] = ImageTk.PhotoImage(
            shape_image(int(w), int(h), fill, outer, radius, kind), master=root)
    return img


def corner_photo(widget, radius, fill, outer, corner):
    """One corner piece ('nw', 'ne', 'sw', 'se'): outer colour outside the
    curve, fill inside. Four of them round a rectangle of any size."""
    root, cache = _cache(widget)
    fill, outer = tk_colour(widget, fill), tk_colour(widget, outer)
    key = ("corner", radius, fill, outer, corner)
    img = cache.get(key)
    if img is None:
        e = math.ceil(radius * EXTENT)
        size = 2 * e + 2
        full = shape_image(size, size, fill, outer, radius)
        box = {"nw": (0, 0, e, e), "ne": (size - e, 0, size, e),
               "sw": (0, size - e, e, size), "se": (size - e, size - e, size, size)}[corner]
        img = cache[key] = ImageTk.PhotoImage(full.crop(box), master=root)
    return img


def round_corners(frame, radius=RADIUS_CARD, outer=None):
    """Give a tk.Frame continuous corners. Four small corner pieces are placed
    in its corners, under its other children (which are inset by padding), so
    the frame keeps working like any frame and can be any size."""
    fill = frame.cget("bg")
    outer = outer or frame.master.cget("bg")
    pieces = []
    for corner, relx, rely in (("nw", 0, 0), ("ne", 1, 0), ("sw", 0, 1), ("se", 1, 1)):
        img = corner_photo(frame, radius, fill, outer, corner)
        piece = tk.Label(frame, image=img, bd=0, highlightthickness=0, padx=0, pady=0, bg=fill)
        piece.image = img
        piece.place(relx=relx, rely=rely, anchor=corner, bordermode="outside")
        piece.lower()
        pieces.append(piece)
    frame.round_corner_pieces = pieces
    return frame


def round_box(parent, fill, radius=RADIUS_CONTROL, padx=6, pady=6):
    """A rounded box to put an Entry or a list in (give those bd=0)."""
    box = tk.Frame(parent, bg=fill, padx=padx, pady=pady)
    round_corners(box, radius, outer=parent.cget("bg"))
    return box


def _text_size(widget, font, text):
    tkcall = widget.tk.call
    return int(tkcall("font", "measure", font, text)), int(tkcall("font", "metrics", font,
                                                                    "-linespace"))


def mix(colour, toward, amount):
    """Blend `colour` toward `toward` by `amount` (0-1)."""
    a = ImageColor.getrgb(colour)[:3]
    b = ImageColor.getrgb(toward)[:3]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * amount) for x, y in zip(a, b, strict=True))


HOVER = 0.12       # how much lighter a control gets under the mouse...
PRESS = 0.25       # ...and while it's held down


def hover_colour(colour):
    return mix(colour, theme.TEXT, HOVER)


def press_colour(colour):
    return mix(colour, theme.TEXT, PRESS)


class RoundButton(tk.Label):
    """A button with continuous corners, used like tk.Button.

    Supports what Silence Player uses: text, command, bg/fg, activebackground,
    disabledforeground, state, font, padx/pady, width (in characters), cursor,
    invoke() and cget(). The shape is redrawn at its exact size whenever the
    text, colours or state change. tk.Button's relief/border options are
    accepted and ignored (the shape is the border). Without a command it is a
    rounded label (the status badge): no press effect.
    """

    EXTRA_X = 3   # what tk.Button adds around the text besides padx/pady
    EXTRA_Y = 3
    _IGNORED = {"relief", "bd", "borderwidth", "overrelief", "highlightthickness",
                "takefocus", "default", "activeforeground"}

    def __init__(self, master, text="", command=None, bg=theme.CONTROL, fg=theme.TEXT,
                 font=("Segoe UI", 10, "bold"), padx=8, pady=5, width=0,
                 radius=RADIUS_CONTROL, activebackground=None, disabledforeground=theme.DISABLED,
                 state="normal", cursor=None, border=None, **kw):
        for key in list(kw):
            if key in self._IGNORED:
                kw.pop(key)
        for alias in ("background", "foreground"):
            if alias in kw:
                if alias == "background":
                    bg = kw.pop(alias)
                else:
                    fg = kw.pop(alias)
        self._outer = master.cget("bg")
        self._fill, self._fg = bg, fg
        self._active_fill = activebackground
        self._disabled_fg = disabledforeground
        self._command = command
        self._state = str(state)
        self._padx, self._pady, self._width = int(padx), int(pady), int(width or 0)
        self._radius = radius
        # border: room around the text besides padx/pady (tk.Button's own is ~3px)
        self._extra = (self.EXTRA_X, self.EXTRA_Y) if border is None else (border, border)
        self._pressed = False
        self._hover = False
        if cursor is None:
            cursor = "hand2" if command is not None else ""
        super().__init__(master, text=text, font=font, fg=fg, bg=self._outer, bd=0,
                         highlightthickness=0, padx=0, pady=0, compound="center",
                         cursor=cursor, **kw)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._render()

    # ── tk.Button-like API ──

    def invoke(self):
        if self._state != "disabled" and self._command is not None:
            return self._command()
        return None

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        elif cnf is not None:
            return self.cget(cnf)
        if not kw:
            return super().configure()
        rerender = False
        for key in list(kw):
            value = kw[key]
            if key in ("bg", "background"):
                self._fill = value
            elif key in ("fg", "foreground"):
                self._fg = value
            elif key == "activebackground":
                self._active_fill = value
            elif key == "disabledforeground":
                self._disabled_fg = value
            elif key == "command":
                self._command = value
            elif key == "state":
                self._state = str(value)
                if self._state == "disabled":
                    self._pressed = self._hover = False
            elif key == "padx":
                self._padx = int(value)
            elif key == "pady":
                self._pady = int(value)
            elif key == "width":
                self._width = int(value or 0)
            elif key in ("text", "font"):
                rerender = True
                continue                      # Tk keeps these
            elif key in self._IGNORED:
                pass
            else:
                continue
            kw.pop(key)
            rerender = True
        if kw:
            super().configure(**kw)
        if rerender:
            self._render()
        return None

    config = configure

    def __setitem__(self, key, value):
        self.configure({key: value})

    def cget(self, key):
        key = {"background": "bg", "foreground": "fg"}.get(key, key)
        own = {"bg": self._fill, "fg": self._fg, "activebackground": self._active_fill,
               "disabledforeground": self._disabled_fg, "command": self._command,
               "state": self._state, "padx": self._padx, "pady": self._pady,
               "width": self._width}
        if key in own:
            return own[key]
        return super().cget(key)

    __getitem__ = cget

    # ── drawing and clicks ──

    def _render(self):
        font = super().cget("font")
        text = super().cget("text")
        text_w, line_h = _text_size(self, font, text)
        if self._width:
            text_w = max(text_w, self._width * _text_size(self, font, "0")[0])
        w = text_w + 2 * (self._padx + self._extra[0])
        h = line_h + 2 * (self._pady + self._extra[1])
        fill = self._fill
        if self._pressed:
            fill = self._active_fill or press_colour(self._fill)
        elif self._hover and self._pressable():
            fill = hover_colour(self._fill)
        fg = self._disabled_fg if self._state == "disabled" else self._fg
        img = photo(self, w, h, fill, self._outer, self._radius)
        self._image = img
        super().configure(image=img, fg=fg, activeforeground=fg)

    def _pressable(self):
        return self._command is not None and self._state != "disabled"

    def _on_press(self, event):
        if self._pressable():
            self._pressed = True
            self._render()

    def _on_release(self, event):
        if not self._pressed:
            return
        self._pressed = False
        self._render()
        if 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.invoke()

    def _on_enter(self, event):
        if self._pressable() and not self._hover:
            self._hover = True
            self._render()

    def _on_leave(self, event):
        if self._pressed or self._hover:
            self._pressed = self._hover = False
            self._render()


class RoundScrollbar(tk.Canvas):
    """A slim scrollbar: a pill-shaped thumb, no arrows. Works with a list's
    yscrollcommand / yview like tk.Scrollbar."""

    MIN_THUMB = 24

    def __init__(self, master, thumb=theme.OVERLAY0, command=None, width=6, pad=2):
        bg = master.cget("bg")
        # height=1: follow the list's height (a canvas would ask for 267 px).
        super().__init__(master, width=width + 2 * pad, height=1, bg=bg,
                         highlightthickness=0, bd=0)
        self._thumb, self._command, self._thumb_w, self._pad = thumb, command, width, pad
        self._range = (0.0, 1.0)
        self._drag = None
        self._item = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        if "command" in kw:
            self._command = kw.pop("command")
        return super().configure(**kw) if kw else None

    config = configure

    def set(self, first, last):
        self._range = (float(first), float(last))
        self._draw()

    def get(self):
        return self._range

    def _geometry(self):
        h = max(1, self.winfo_height() - 2 * self._pad)
        lo, hi = self._range
        size = max(self.MIN_THUMB, round((hi - lo) * h))
        top = self._pad + round(lo * h) if hi - lo < 1 else self._pad
        top = min(top, self._pad + h - size)
        return top, size, h

    def _draw(self):
        if self._item is not None:
            self.delete(self._item)
            self._item = None
        lo, hi = self._range
        if hi - lo >= 0.999:
            return                                    # everything fits: no thumb
        top, size, _h = self._geometry()
        img = photo(self, self._thumb_w, size, self._thumb, None, kind="capsule")
        self._image = img
        self._item = self.create_image(self._pad, top, anchor="nw", image=img)

    def _on_press(self, event):
        if self._command is None or self._range[1] - self._range[0] >= 0.999:
            return
        top, size, _h = self._geometry()
        if top <= event.y <= top + size:
            self._drag = (event.y, self._range[0])
        else:
            self._command("scroll", -1 if event.y < top else 1, "pages")

    def _on_drag(self, event):
        if self._drag is None or self._command is None:
            return
        _top, _size, h = self._geometry()
        y0, lo0 = self._drag
        self._command("moveto", max(0.0, lo0 + (event.y - y0) / h))


class RoundList(tk.Canvas):
    """A list whose selected row is a rounded highlight. Supports the part of
    the tk.Listbox API Silence Player uses: insert, delete, get, size,
    curselection, selection_set/clear, see, yview and yscrollcommand."""

    def __init__(self, master, fg, selectbackground, selectforeground,
                 font=("Segoe UI", 10), height=8, yscrollcommand=None, row_pad=3, inset=2):
        bg = master.cget("bg")
        _w, line = _text_size(master, font, "Ag")
        self._row_h = line + 2 * row_pad
        super().__init__(master, bg=bg, height=height * self._row_h, highlightthickness=0,
                         bd=0, width=200)
        self._bg, self._fg, self._font = bg, fg, font
        self._sel_bg, self._sel_fg = selectbackground, selectforeground
        self._inset = inset
        self._items = []
        self._selected = None
        self._top = 0                 # scrolled down by this many pixels
        self._yscroll = yscrollcommand
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_click)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self.yview("scroll", -1, "units"))
        self.bind("<Button-5>", lambda e: self.yview("scroll", 1, "units"))

    # ── Listbox-like API ──

    def _index(self, index, for_insert=False):
        if index in (tk.END, "end"):
            return len(self._items) if for_insert else len(self._items) - 1
        return int(index)

    def size(self):
        return len(self._items)

    def insert(self, index, *texts):
        i = self._index(index, for_insert=True)
        for offset, text in enumerate(texts):
            self._items.insert(i + offset, str(text))
        if self._selected is not None and self._selected >= i:
            self._selected += len(texts)
        self._redraw()

    def delete(self, first, last=None):
        a = self._index(first)
        b = a if last is None else self._index(last)
        if a < 0 or not self._items:
            return
        del self._items[a:b + 1]
        if self._selected is not None:
            if a <= self._selected <= b:
                self._selected = None
            elif self._selected > b:
                self._selected -= b - a + 1
        self._clamp_scroll()
        self._redraw()

    def get(self, first, last=None):
        if last is None:
            i = self._index(first)
            return self._items[i] if 0 <= i < len(self._items) else ""
        return tuple(self._items[self._index(first):self._index(last) + 1])

    def curselection(self):
        return () if self._selected is None else (self._selected,)

    def selection_set(self, first, last=None):
        i = self._index(first)
        if 0 <= i < len(self._items):
            self._selected = i
            self._redraw()

    def selection_clear(self, first=0, last=None):
        self._selected = None
        self._redraw()

    def see(self, index):
        i = self._index(index)
        view_h = max(1, self.winfo_height())
        y = i * self._row_h
        if y < self._top:
            self._top = y
        elif y + self._row_h > self._top + view_h:
            self._top = y + self._row_h - view_h
        self._clamp_scroll()
        self._redraw()

    def yview(self, *args):
        total = max(1, len(self._items) * self._row_h)
        view_h = max(1, self.winfo_height())
        if not args:
            return (self._top / total, min(1.0, (self._top + view_h) / total))
        if args[0] == "moveto":
            self._top = float(args[1]) * total
        elif args[0] == "scroll":
            step = self._row_h if args[2] == "units" else view_h
            self._top += int(args[1]) * step
        self._clamp_scroll()
        self._redraw()
        return None

    # ── internals ──

    def _clamp_scroll(self):
        total = len(self._items) * self._row_h
        view_h = max(1, self.winfo_height())
        self._top = int(max(0, min(self._top, total - view_h)))

    def _on_click(self, event):
        i = int((event.y + self._top) // self._row_h)
        if 0 <= i < len(self._items):
            self._selected = i
            self._redraw()

    def _on_wheel(self, event):
        self.yview("scroll", -1 if event.delta > 0 else 1, "units")

    def _redraw(self):
        tk.Canvas.delete(self, "all")      # (self.delete removes list rows)
        width = max(1, self.winfo_width())
        view_h = max(1, self.winfo_height())
        first = self._top // self._row_h
        last = min(len(self._items), (self._top + view_h) // self._row_h + 1)
        for i in range(first, last):
            y = i * self._row_h - self._top
            selected = i == self._selected
            if selected:
                img = photo(self, width - 2 * self._inset, self._row_h, self._sel_bg, self._bg,
                            RADIUS_CONTROL - 2)
                self._sel_image = img
                self.create_image(self._inset, y, anchor="nw", image=img)
            self.create_text(self._inset + 8, y + self._row_h / 2, anchor="w",
                             text=self._items[i], font=self._font,
                             fill=self._sel_fg if selected else self._fg)
        if self._yscroll is not None:
            self._yscroll(*self.yview())


class RoundSlider(tk.Canvas):
    """A slider with a pill-shaped track and a round knob, used like tk.Scale
    (from_, to, resolution, variable, command; set/get/cget)."""

    TRACK_H = 6
    KNOB = 16

    def __init__(self, master, from_=0, to=100, resolution=1, variable=None, command=None,
                 track=theme.CONTROL, fill=theme.ACCENT, knob=theme.TEXT, length=150):
        bg = master.cget("bg")
        super().__init__(master, width=length, height=self.KNOB + 2, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._from, self._to, self._res = from_, to, resolution
        self._var = variable if variable is not None else tk.IntVar(value=from_)
        self._command = command
        self._colours = (track, fill, knob)
        self._images = []
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_mouse)
        self.bind("<B1-Motion>", self._on_mouse)
        self._var.trace_add("write", lambda *a: self._redraw())

    def cget(self, key):
        own = {"from": self._from, "to": self._to, "resolution": self._res,
               "variable": self._var, "command": self._command}
        return own[key] if key in own else super().cget(key)

    def _snap(self, value):
        value = max(self._from, min(self._to, float(value)))
        steps = round((value - self._from) / self._res)
        value = self._from + steps * self._res
        return int(value) if float(self._res).is_integer() else value

    def get(self):
        try:
            return self._snap(self._var.get())
        except (tk.TclError, ValueError):
            return self._from

    def set(self, value):
        value = self._snap(value)
        changed = value != self.get() or value != self._var.get()
        self._var.set(value)
        if changed and self._command is not None:
            self.after_idle(self._command, str(value))

    def _knob_span(self):
        width = max(self.KNOB, self.winfo_width())
        return self.KNOB / 2, width - self.KNOB / 2

    def _on_mouse(self, event):
        left, right = self._knob_span()
        frac = (event.x - left) / max(1, right - left)
        self.set(self._from + max(0.0, min(1.0, frac)) * (self._to - self._from))

    def _redraw(self):
        self.delete("all")
        track, fill, knob = self._colours
        width = max(self.KNOB, self.winfo_width())
        height = self.KNOB + 2
        left, right = self._knob_span()
        frac = (self.get() - self._from) / max(1e-9, self._to - self._from)
        x = left + frac * (right - left)
        y = (height - self.TRACK_H) / 2
        track_img = photo(self, width, self.TRACK_H, track, None, kind="capsule")
        fill_w = max(self.TRACK_H, round(x))
        fill_img = photo(self, fill_w, self.TRACK_H, fill, None, kind="capsule")
        knob_img = photo(self, self.KNOB, self.KNOB, knob, None, kind="capsule")
        self._images = [track_img, fill_img, knob_img]
        self.create_image(0, round(y), anchor="nw", image=track_img)
        self.create_image(0, round(y), anchor="nw", image=fill_img)
        self.create_image(round(x - self.KNOB / 2), 1, anchor="nw", image=knob_img)


class Switch(tk.Canvas):
    """An on/off switch: a pill with a sliding knob. Mauve (theme.ACTIVE) with a
    dark knob when on, grey when off (Catppuccin: Base on an accent colour).

    value / get() / set(on) change it without calling `command`; a click (or
    invoke()) flips it and calls command(new_value). state="disabled" greys it
    out and ignores clicks.
    """

    W, H, KNOB = 44, 24, 18
    SLIDE_MS, SLIDE_STEPS = 120, 6

    def __init__(self, master, value=False, command=None, on=theme.ACTIVE, off=theme.SURFACE2,
                 knob_on=theme.BASE, knob_off=theme.OVERLAY2):
        super().__init__(master, width=self.W, height=self.H, bg=master.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self._value = bool(value)
        self._command = command
        self._colours = (on, off, knob_on, knob_off)
        self._state = "normal"
        self._hover = self._pressed = False
        self._pos = 1.0 if self._value else 0.0      # knob position 0..1 (for the slide)
        self._after = None
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Destroy>", lambda e: self._cancel(), add="+")
        self._draw()

    @property
    def value(self):
        return self._value

    def get(self):
        return self._value

    def set(self, value):
        self._value = bool(value)
        self._cancel()
        self._pos = 1.0 if self._value else 0.0
        self._draw()

    def invoke(self):
        if self._state == "disabled":
            return None
        self._value = not self._value
        self._slide()
        if self._command is not None:
            return self._command(self._value)
        return None

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        redraw = False
        if "state" in kw:
            self._state = str(kw.pop("state"))
            redraw = True
            super().configure(cursor="arrow" if self._state == "disabled" else "hand2")
        if "command" in kw:
            self._command = kw.pop("command")
        if kw:
            super().configure(**kw)
        if redraw:
            self._draw()

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "command":
            return self._command
        return super().cget(key)

    def _set_hover(self, on):
        self._hover = on and self._state != "disabled"
        if not on:
            self._pressed = False
        self._draw()

    def _on_press(self, event):
        if self._state != "disabled":
            self._pressed = True

    def _on_release(self, event):
        if self._pressed and 0 <= event.x < self.W and 0 <= event.y < self.H:
            self._pressed = False
            self.invoke()
        self._pressed = False

    def _cancel(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _slide(self):
        self._cancel()
        target = 1.0 if self._value else 0.0
        step = (target - self._pos) / self.SLIDE_STEPS

        def tick(left=self.SLIDE_STEPS):
            self._pos = target if left <= 1 else self._pos + step
            self._draw()
            self._after = self.after(self.SLIDE_MS // self.SLIDE_STEPS, tick, left - 1) \
                if left > 1 else None
        tick()

    def _draw(self):
        self.delete("all")
        on, off, knob_on, knob_off = self._colours
        if self._state == "disabled":
            track_c, knob_c = theme.CONTROL, theme.DISABLED
        else:
            track_c = on if self._value else off
            knob_c = knob_on if self._value else knob_off
            if self._hover:
                track_c = hover_colour(track_c)
        track = photo(self, self.W, self.H, track_c, None, kind="capsule")
        knob = photo(self, self.KNOB, self.KNOB, knob_c, None, kind="capsule")
        self._images = (track, knob)
        self.create_image(0, 0, anchor="nw", image=track)
        pad = (self.H - self.KNOB) // 2
        x = pad + self._pos * (self.W - self.KNOB - 2 * pad)
        self.create_image(round(x), pad, anchor="nw", image=knob)


class SegmentedControl(tk.Canvas):
    """A pill of options with the selected one highlighted inside it (like
    Apple's segmented controls). options: [(label, value, highlight colour)].

    get()/set(value) change the selection without calling `command`; a click
    (or invoke(value)) selects and calls command(value). Disabled options are
    drawn dimmed; clicking one calls on_locked(value) instead.
    """

    def __init__(self, master, options, value, command=None, track=theme.SURFACE0,
                 font=("Segoe UI", 10, "bold"), pad_x=14, height=32, inset=3, equal=True,
                 on_locked=None):
        bg = master.cget("bg")
        widths = [_text_size(master, font, label)[0] + 2 * pad_x for label, _v, _c in options]
        if equal:
            widths = [max(widths)] * len(widths)
        self._total = sum(widths) + 2 * inset
        super().__init__(master, width=self._total, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bg, self._choices, self._widths = bg, list(options), widths
        self._font, self._h, self._inset, self._track = font, height, inset, track
        self._value = value
        self._command, self._on_locked = command, on_locked
        self._disabled = set()
        self._hover = self._pressed = None
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self._draw()

    # ── API ──

    def get(self):
        return self._value

    def set(self, value):
        self._value = value
        self._draw()

    def invoke(self, value):
        if value in self._disabled:
            if self._on_locked is not None:
                self._on_locked(value)
            return None
        if value == self._value:
            return None
        self._value = value
        self._draw()
        if self._command is not None:
            return self._command(value)
        return None

    def set_disabled(self, values):
        self._disabled = set(values)
        self._draw()

    def state_of(self, value):
        return "disabled" if value in self._disabled else "normal"

    def label_of(self, value):
        return next(label for label, v, _c in self._choices if v == value)

    def center_of(self, value):
        x = self._inset
        for (_label, v, _c), w in zip(self._choices, self._widths, strict=True):
            if v == value:
                return int(x + w / 2), self._h // 2
            x += w
        raise KeyError(value)

    # ── mouse ──

    def _value_at(self, x):
        left = self._inset
        for (_label, v, _c), w in zip(self._choices, self._widths, strict=True):
            if left <= x < left + w:
                return v
            left += w
        return None

    def _on_press(self, event):
        value = self._value_at(event.x)
        if value in self._disabled:
            self._pressed = None
            if self._on_locked is not None:
                self._on_locked(value)
            return
        self._pressed = value

    def _on_release(self, event):
        value = self._value_at(event.x) if 0 <= event.y < self._h else None
        if value is not None and value == self._pressed:
            self.invoke(value)
        self._pressed = None

    def _on_motion(self, event):
        value = self._value_at(event.x)
        self.configure(cursor="arrow" if value in self._disabled or value == self._value
                       else "hand2")
        self._set_hover(value)

    def _set_hover(self, value):
        if value != self._hover:
            self._hover = value
            self._draw()

    def _draw(self):
        self.delete("all")
        track = photo(self, self._total, self._h, self._track, self._bg, RADIUS_CONTROL)
        images = [track]
        self.create_image(0, 0, anchor="nw", image=track)
        x = self._inset
        inner_h = self._h - 2 * self._inset
        for (label, value, colour), w in zip(self._choices, self._widths, strict=True):
            selected = value == self._value
            disabled = value in self._disabled
            if selected or (value == self._hover and not disabled):
                fill = colour if selected else hover_colour(self._track)
                pill = photo(self, w, inner_h, fill, None, RADIUS_CONTROL - 2)
                images.append(pill)
                self.create_image(x, self._inset, anchor="nw", image=pill)
            if selected:
                text_c = theme.ON_ACCENT
            elif disabled:
                text_c = theme.DISABLED
            elif value == self._hover:
                text_c = theme.TEXT
            else:
                text_c = theme.LABEL
            self.create_text(x + w / 2, self._h / 2, text=label, font=self._font, fill=text_c)
            x += w
        self._images = images
