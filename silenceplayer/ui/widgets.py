"""Small reusable widgets.
"""
import time
import tkinter as tk
import tkinter.font as tkfont

from PIL import Image, ImageTk


class MarqueeLabel(tk.Canvas):
    """A one-line label that scrolls its text when it doesn't fit.

    - Fits → stays still.
    - Too long → waits, glides left at a slow constant speed, loops seamlessly
      (a second copy follows after a gap) and pauses again at the start of
      every cycle. Edges fade out instead of cutting letters in half.
    - Pauses while hovered (so anyone can stop it to read it), and while not
      visible (hidden tab/window).
    - Scrolls even when Windows "Animation effects" is off: people turn that
      off for the whole system, not to lose this. animate=False shows a still
      name ending in "…" instead.
    """

    SPEED_PX_PER_S = 30
    START_PAUSE_MS = 2500
    FRAME_MS       = 16
    GAP_PX         = 48
    PAD_X          = 8
    FADE_PX        = 18

    def __init__(self, parent, textvariable, width_chars, bg, fg,
                 font=("Segoe UI", 10), on_click=None, animate=True):
        self._font = tkfont.Font(root=parent, family=font[0], size=font[1])
        width = self._font.measure("0") * width_chars + 16
        height = self._font.metrics("linespace") + 14
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2" if on_click else "")
        self._width, self._height = width, height
        self._bg, self._var = bg, textvariable
        self._animate = animate
        self._hover = False
        self._after = None
        self._offset = 0.0
        self._text_w = 0
        self._scrolling = False
        self._last = None
        y = height / 2
        self._items = [self.create_text(self.PAD_X, y, anchor="w", fill=fg, font=self._font)
                       for _ in range(2)]
        self._fade_left, self._fade_right = self._make_fades()
        self._fade_items = [self.create_image(0, 0, anchor="nw", image=self._fade_left),
                            self.create_image(width - self.FADE_PX, 0, anchor="nw",
                                              image=self._fade_right)]
        if on_click:
            self.bind("<Button-1>", lambda e: on_click())
        self.bind("<Enter>", lambda e: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda e: self._set_hover(False), add="+")
        self.bind("<Destroy>", lambda e: self._cancel(), add="+")
        textvariable.trace_add("write", lambda *a: self.reset())
        self.reset()

    @property
    def text(self):
        return self._var.get()

    @property
    def scrolling(self):
        return self._scrolling

    def set_fg(self, color):
        for item in self._items:
            self.itemconfig(item, fill=color)

    def _make_fades(self):
        r, g, b = (v // 256 for v in self.winfo_rgb(self._bg))
        right = Image.new("RGBA", (self.FADE_PX, self._height))
        for x in range(self.FADE_PX):
            alpha = int(255 * (x + 1) / self.FADE_PX)
            for y in range(self._height):
                right.putpixel((x, y), (r, g, b, alpha))
        left = right.transpose(Image.FLIP_LEFT_RIGHT)
        return ImageTk.PhotoImage(left, master=self), ImageTk.PhotoImage(right, master=self)

    def _cancel(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _set_hover(self, on):
        self._hover = on
        self._last = None   # don't jump forward by the paused time

    def reset(self):
        """Re-measure the text and restart from the beginning."""
        self._cancel()
        text = self._var.get()
        avail = self._width - 2 * self.PAD_X
        self._text_w = self._font.measure(text)
        self._offset = 0.0
        self._last = None
        overflow = self._text_w > avail
        self._scrolling = overflow and self._animate
        shown = text
        if overflow and not self._animate:
            while shown and self._font.measure(shown + "…") > avail:
                shown = shown[:-1]
            shown = shown.rstrip() + "…"
        self.itemconfigure(self._items[0], text=shown)
        self.itemconfigure(self._items[1], text=text if self._scrolling else "")
        self._place()
        self.itemconfigure(self._fade_items[1], state="normal" if self._scrolling else "hidden")
        self.itemconfigure(self._fade_items[0], state="hidden")
        if self._scrolling:
            self._after = self.after(self.START_PAUSE_MS, self._tick)

    def _place(self):
        x = self.PAD_X - self._offset
        y = self._height / 2
        self.coords(self._items[0], x, y)
        self.coords(self._items[1], x + self._text_w + self.GAP_PX, y)

    def _tick(self):
        self._after = None
        if not self._scrolling:
            return
        if self._hover or not self.winfo_viewable():
            self._last = None
            self._after = self.after(200, self._tick)
            return
        now = time.monotonic()
        if self._last is not None:
            # Time-based, so the speed stays constant even if frames are late.
            self._offset += self.SPEED_PX_PER_S * (now - self._last)
        self._last = now
        cycle = self._text_w + self.GAP_PX
        if self._offset >= cycle:
            self._offset = 0.0
            self._last = None
            self._place()
            self.itemconfigure(self._fade_items[0], state="hidden")
            self._after = self.after(self.START_PAUSE_MS, self._tick)
            return
        self._place()
        self.itemconfigure(self._fade_items[0], state="normal")
        self._after = self.after(self.FRAME_MS, self._tick)
