"""Continuous-corner shapes and the rounded widgets built on them."""
import tkinter as tk

import pytest
from conftest import pump

from silenceplayer.ui import rounded
from silenceplayer.ui.rounded import (
    EXTENT,
    RoundButton,
    RoundList,
    RoundScrollbar,
    RoundSlider,
    capsule,
    continuous_rect,
    max_radius,
    round_box,
    round_corners,
    shape_image,
)

# ── geometry ─────────────────────────────────────────────────────────────────


def test_corner_reaches_1_528_radius_along_each_edge():
    pts = continuous_rect(200, 100, 10)
    xs_on_top = [x for x, y in pts if abs(y) < 1e-9]
    ys_on_left = [y for x, y in pts if abs(x) < 1e-9]
    # the straight top edge runs between the two corners' far ends
    assert min(xs_on_top) == pytest.approx(10 * EXTENT)
    assert max(xs_on_top) == pytest.approx(200 - 10 * EXTENT)
    assert min(ys_on_left) == pytest.approx(10 * EXTENT)
    assert max(ys_on_left) == pytest.approx(100 - 10 * EXTENT)


def test_curve_is_continuous_not_a_circle():
    """Apple's corner sits outside a same-radius circle near the edges (it
    starts bending earlier, more gently) and stays inside the bounds."""
    r = 20
    pts = continuous_rect(200, 200, r)
    assert all(0 <= x <= 200 and 0 <= y <= 200 for x, y in pts)
    # a point on the curve just past where a circle would begin (x = r) is
    # still close to the edge: the bend is gradual
    near = [y for x, y in pts if r * 0.95 <= x <= r * 1.05 and y < r]
    assert near and max(near) < r * 0.2
    # and the curve already left the edge before x = r (a circle wouldn't)
    early = [y for x, y in pts if r * 1.2 <= x <= r * 1.45 and y < r]
    assert early and max(early) > 0


def test_radius_is_limited_so_corners_never_overlap():
    assert max_radius(40, 20) == pytest.approx(10 / EXTENT)
    pts = continuous_rect(40, 20, 50)
    assert all(0 <= x <= 40 and 0 <= y <= 20 for x, y in pts)
    assert continuous_rect(40, 20, 0) == [(0, 0), (40, 0), (40, 20), (0, 20)]


def test_capsule_has_round_ends():
    pts = capsule(100, 10)
    assert min(x for x, y in pts) == pytest.approx(0, abs=1e-9)
    assert max(x for x, y in pts) == pytest.approx(100, abs=1e-9)
    assert max(y for x, y in pts) == pytest.approx(10, abs=1e-9)
    vertical = capsule(10, 60)
    assert max(y for x, y in vertical) == pytest.approx(60, abs=1e-9)


def test_shape_is_anti_aliased_and_exact_size():
    img = shape_image(60, 30, "#ffffff", "#000000", 8)
    assert img.size == (60, 30)
    assert img.getpixel((30, 15)) == (255, 255, 255)        # inside
    assert img.getpixel((0, 0)) == (0, 0, 0)                # outside the corner
    greys = {img.getpixel((x, y)) for x in range(12) for y in range(12)}
    assert any(0 < p[0] < 255 for p in greys), "edges must blend, not jag"


def test_transparent_shape_keeps_its_colour_at_the_edges():
    img = shape_image(20, 6, "#89b4fa", None, kind="capsule")
    assert img.mode == "RGBA"
    edge = [img.getpixel((x, y)) for x in range(4) for y in range(6)]
    partial = [p for p in edge if 64 <= p[3] < 255]
    assert partial, "the ends must have soft (partly transparent) pixels"
    for r, g, b, _a in partial:                               # no dark fringe
        assert abs(r - 0x89) <= 12 and abs(g - 0xB4) <= 12 and abs(b - 0xFA) <= 12


# ── widgets ──────────────────────────────────────────────────────────────────


def test_round_corners_sit_exactly_in_the_corners(root):
    card = tk.Frame(root, bg="#2a2a3e", padx=16, pady=12, width=200, height=80)
    card.pack(padx=20, pady=20)
    tk.Label(card, text="hello", bg="#2a2a3e").pack()
    round_corners(card, 14)
    pump(root, 0.1)
    nw, ne, sw, se = card.round_corner_pieces
    w, h = card.winfo_width(), card.winfo_height()
    assert (nw.winfo_x(), nw.winfo_y()) == (0, 0)
    assert ne.winfo_x() + ne.winfo_width() == w and ne.winfo_y() == 0
    assert sw.winfo_x() == 0 and sw.winfo_y() + sw.winfo_height() == h
    assert se.winfo_x() + se.winfo_width() == w and se.winfo_y() + se.winfo_height() == h
    assert nw.winfo_width() == int(-(-14 * EXTENT // 1))      # ceil(14 * 1.5287)


def test_round_box_wraps_an_entry(root):
    holder = tk.Frame(root, bg="#2a2a3e")
    holder.pack()
    box = round_box(holder, "#313244")
    entry = tk.Entry(box, bd=0)
    entry.pack()
    box.pack()
    pump(root, 0.05)
    assert len(box.round_corner_pieces) == 4
    assert box.cget("bg") == "#313244"


def test_round_button_acts_like_a_button(root):
    clicks = []
    holder = tk.Frame(root, bg="#2a2a3e")
    holder.pack()
    b = RoundButton(holder, text="Save", command=lambda: clicks.append(1),
                    bg="#313244", fg="#f9e2af", relief="flat", padx=8, pady=5)
    b.pack()
    pump(root, 0.05)
    assert b.cget("text") == "Save" and b.cget("bg") == "#313244"
    assert b.cget("state") == "normal" and b.cget("cursor") == "hand2"
    b.invoke()
    assert clicks == [1]
    # a click is press + release inside the button
    b.event_generate("<ButtonPress-1>", x=5, y=5)
    b.event_generate("<ButtonRelease-1>", x=5, y=5)
    assert clicks == [1, 1]
    # released outside: no click (like tk.Button)
    b.event_generate("<ButtonPress-1>", x=5, y=5)
    b.event_generate("<ButtonRelease-1>", x=-50, y=5)
    assert clicks == [1, 1]

    b.config(state="disabled")
    assert b.cget("state") == "disabled"
    assert tk.Label.cget(b, "state") == "normal", "Tk would stipple a disabled image"
    b.invoke()
    assert clicks == [1, 1]
    b.config(state="normal", bg="#a6e3a1", text="Enabled")
    assert b.cget("bg") == "#a6e3a1" and b.cget("text") == "Enabled"
    assert b["bg"] == "#a6e3a1"


def test_round_button_size_follows_text_and_width(root):
    holder = tk.Frame(root, bg="#2a2a3e")
    holder.pack()
    short = RoundButton(holder, text="On", width=8)
    long = RoundButton(holder, text="On")
    for b in (short, long):
        b.pack()
    pump(root, 0.05)
    assert short.winfo_reqwidth() > long.winfo_reqwidth()
    before = long.winfo_reqwidth()
    long.config(text="Considerably longer label")
    pump(root, 0.05)
    assert long.winfo_reqwidth() > before


def test_badge_without_command_has_no_press_effect(root):
    holder = tk.Frame(root, bg="#2a2a3e")
    holder.pack()
    badge = RoundButton(holder, text="Playing", border=0, radius=rounded.RADIUS_SMALL)
    badge.pack()
    pump(root, 0.05)
    image = badge.cget("image")
    badge.event_generate("<ButtonPress-1>", x=2, y=2)
    assert badge.cget("image") == image and badge.cget("cursor") == ""


def test_round_list_behaves_like_a_listbox(root):
    holder = tk.Frame(root, bg="#313244")
    holder.pack()
    seen = []
    lst = RoundList(holder, fg="#cdd6f4", selectbackground="#89b4fa",
                    selectforeground="#1e1e2e", height=3,
                    yscrollcommand=lambda a, b: seen.append((a, b)))
    lst.pack()
    pump(root, 0.05)
    lst.insert(tk.END, "a.exe")
    lst.insert("end", "b.exe", "c.exe")
    lst.insert(0, "first.exe")
    assert lst.size() == 4
    assert lst.get(0, tk.END) == ("first.exe", "a.exe", "b.exe", "c.exe")
    assert lst.get(1) == "a.exe"
    assert lst.curselection() == ()
    lst.selection_set(2)
    assert lst.curselection() == (2,)
    lst.delete(0)                          # the selection moves with its row
    assert lst.curselection() == (1,) and lst.get(1) == "b.exe"
    lst.delete(1)
    assert lst.curselection() == ()
    lst.selection_set(0)
    lst.selection_clear(0, "end")
    assert lst.curselection() == ()
    lst.delete(0, tk.END)
    assert lst.size() == 0
    assert seen, "the scrollbar is told about the view"

    for i in range(20):
        lst.insert(tk.END, f"app{i}.exe")
    pump(root, 0.05)
    first, last = lst.yview()
    assert first == 0 and last < 1
    lst.yview("moveto", 1.0)
    assert lst.yview()[1] == pytest.approx(1.0)
    lst.yview("scroll", -1, "pages")
    assert lst.yview()[0] < 1


def test_clicking_a_row_selects_it(root):
    holder = tk.Frame(root, bg="#313244")
    holder.pack()
    lst = RoundList(holder, fg="#fff", selectbackground="#00f", selectforeground="#000",
                    height=4)
    lst.pack()
    for name in ("a", "b", "c"):
        lst.insert(tk.END, name)
    pump(root, 0.05)
    row_h = lst._row_h
    lst.event_generate("<ButtonPress-1>", x=10, y=int(row_h * 1.5))
    assert lst.curselection() == (1,)


def test_round_slider_snaps_clamps_and_reports(root):
    holder = tk.Frame(root, bg="#2a2a3e")
    holder.pack()
    var = tk.IntVar(value=50)
    calls = []
    s = RoundSlider(holder, from_=5, to=100, resolution=5, variable=var,
                    command=calls.append, length=200)
    s.pack()
    pump(root, 0.05)
    assert s.get() == 50 and s.cget("resolution") == 5
    s.set(63)
    pump(root, 0.05)
    assert var.get() == 65 and calls == ["65"]
    s.set(0)
    pump(root, 0.05)
    assert s.get() == 5 and calls[-1] == "5"
    s.set(5)                               # unchanged: no command
    pump(root, 0.05)
    assert calls.count("5") == 1
    # dragging to the far right end
    s.event_generate("<ButtonPress-1>", x=199, y=8)
    pump(root, 0.05)
    assert s.get() == 100 and calls[-1] == "100"
    var.set(30)                            # set from elsewhere: redraws, no command
    pump(root, 0.05)
    assert calls[-1] == "100"


def test_round_scrollbar_drives_its_list(root):
    holder = tk.Frame(root, bg="#313244")
    holder.pack()
    commands = []
    bar = RoundScrollbar(holder, command=lambda *a: commands.append(a))
    bar.pack(side="left", fill="y")
    tk.Frame(holder, height=200, width=10).pack(side="left")
    pump(root, 0.05)
    bar.set(0.0, 1.0)
    assert bar._item is None, "no thumb when everything fits"
    bar.set(0.0, 0.25)
    assert bar._item is not None
    h = bar.winfo_height()
    bar.event_generate("<ButtonPress-1>", x=3, y=h - 3)       # below the thumb
    assert commands[-1] == ("scroll", 1, "pages")
    bar.event_generate("<ButtonPress-1>", x=3, y=6)           # on the thumb
    bar.event_generate("<B1-Motion>", x=3, y=6 + h // 4)
    assert commands[-1][0] == "moveto" and commands[-1][1] > 0
    bar.config(command=None)
    assert bar._command is None


# ── switches, segmented pills, hover ─────────────────────────────────────────

from silenceplayer.ui import theme  # noqa: E402
from silenceplayer.ui.rounded import SegmentedControl, Switch, hover_colour  # noqa: E402


def test_switch_flips_and_reports(root):
    holder = tk.Frame(root, bg=theme.CARD)
    holder.pack()
    seen = []
    sw = Switch(holder, value=False, command=seen.append)
    sw.pack()
    pump(root, 0.05)
    assert sw.get() is False
    sw.event_generate("<ButtonPress-1>", x=10, y=10)
    sw.event_generate("<ButtonRelease-1>", x=10, y=10)
    assert sw.get() is True and seen == [True]
    pump(root, 0.3)                          # the knob slide finishes
    assert sw._pos == 1.0
    sw.set(False)                            # set() doesn't report
    assert sw.get() is False and seen == [True] and sw._pos == 0.0
    sw.config(state="disabled")
    sw.invoke()
    assert sw.get() is False and sw.cget("state") == "disabled"
    sw.config(state="normal")
    assert sw.invoke() is None and sw.get() is True and seen == [True, True]


def test_segmented_selects_reports_and_locks(root):
    holder = tk.Frame(root, bg=theme.BG)
    holder.pack()
    chosen, locked = [], []
    pill = SegmentedControl(holder, [("Single File", "single", theme.BLUE),
                                     ("Playlist", "playlist", theme.BLUE)], "single",
                            command=chosen.append, on_locked=locked.append)
    pill.pack()
    pump(root, 0.05)
    x, y = pill.center_of("playlist")
    pill.event_generate("<ButtonPress-1>", x=x, y=y)
    pill.event_generate("<ButtonRelease-1>", x=x, y=y)
    assert pill.get() == "playlist" and chosen == ["playlist"]
    pill.invoke("playlist")                  # already selected: nothing
    assert chosen == ["playlist"]
    pill.set("single")                       # set() doesn't report
    assert chosen == ["playlist"]
    pill.set_disabled({"playlist"})
    assert pill.state_of("playlist") == "disabled" and pill.state_of("single") == "normal"
    pill.event_generate("<ButtonPress-1>", x=x, y=y)
    pill.event_generate("<ButtonRelease-1>", x=x, y=y)
    assert pill.get() == "single" and locked == ["playlist"]
    pill.invoke("playlist")
    assert locked == ["playlist", "playlist"] and pill.get() == "single"
    assert pill.label_of("playlist") == "Playlist"
    # a release on another option than the press doesn't select anything
    pill.set_disabled(set())
    sx, sy = pill.center_of("single")
    pill.event_generate("<ButtonPress-1>", x=x, y=y)
    pill.event_generate("<ButtonRelease-1>", x=sx, y=sy)
    assert pill.get() == "single"


def test_equal_and_natural_segment_widths(root):
    holder = tk.Frame(root, bg=theme.CARD)
    holder.pack()
    opts = [("Loop Song", "a", theme.BLUE), ("Stop", "b", theme.RED)]
    equal = SegmentedControl(holder, opts, "a", equal=True)
    natural = SegmentedControl(holder, opts, "a", equal=False)
    assert equal._widths[0] == equal._widths[1]
    assert natural._widths[0] > natural._widths[1]


def test_buttons_light_up_on_hover(root):
    holder = tk.Frame(root, bg=theme.CARD)
    holder.pack()
    b = RoundButton(holder, text="Browse", command=lambda: None, bg=theme.CONTROL)
    b.pack()
    pump(root, 0.05)
    rest = b.cget("image")
    b.event_generate("<Enter>")
    hovered = b.cget("image")
    assert hovered != rest
    b.event_generate("<Leave>")
    assert b.cget("image") == rest
    b.config(state="disabled")
    b.event_generate("<Enter>")
    assert b.cget("image") == rest, "no hover on a disabled button"
    assert hover_colour(theme.SURFACE1) != theme.SURFACE1


def test_tk_colour_names_pil_does_not_know_still_work(root):
    # A plain tk.Tk() on Windows is "SystemButtonFace", which PIL can't read;
    # "gray50" is such a name everywhere.
    assert rounded.tk_colour(root, "gray50") == "#7f7f7f"
    assert rounded.tk_colour(root, "#123456") == "#123456"
    parent = tk.Frame(root, bg="gray50")
    parent.pack()
    card = tk.Frame(parent, bg="gray25", width=100, height=50)
    card.pack(padx=10, pady=10)
    rounded.round_corners(card, 14)          # used to raise "unknown color specifier"
    assert len(card.round_corner_pieces) == 4
