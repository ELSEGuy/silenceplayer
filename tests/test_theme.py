"""The app's colours are Catppuccin Frappé, by role."""
import re
from pathlib import Path

from silenceplayer.ui import theme

# Official Frappé palette (github.com/catppuccin/catppuccin)
FRAPPE = {
    "ROSEWATER": "#f2d5cf", "FLAMINGO": "#eebebe", "PINK": "#f4b8e4", "MAUVE": "#ca9ee6",
    "RED": "#e78284", "MAROON": "#ea999c", "PEACH": "#ef9f76", "YELLOW": "#e5c890",
    "GREEN": "#a6d189", "TEAL": "#81c8be", "SKY": "#99d1db", "SAPPHIRE": "#85c1dc",
    "BLUE": "#8caaee", "LAVENDER": "#babbf1", "TEXT": "#c6d0f5", "SUBTEXT1": "#b5bfe2",
    "SUBTEXT0": "#a5adce", "OVERLAY2": "#949cbb", "OVERLAY1": "#838ba7",
    "OVERLAY0": "#737994", "SURFACE2": "#626880", "SURFACE1": "#51576d",
    "SURFACE0": "#414559", "BASE": "#303446", "MANTLE": "#292c3c", "CRUST": "#232634",
}
MOCHA = {"#1e1e2e", "#181825", "#313244", "#45475a", "#585b70", "#6c7086", "#7f849c",
         "#cdd6f4", "#a6adc8", "#89b4fa", "#a6e3a1", "#f38ba8", "#f9e2af", "#2a2a3e"}


def test_palette_is_official_frappe():
    for name, value in FRAPPE.items():
        assert getattr(theme, name) == value, name


def test_roles_follow_the_style_guide():
    assert theme.BG == theme.BASE                  # background pane
    assert theme.PANE == theme.MANTLE              # secondary pane
    assert theme.CARD in (theme.SURFACE0, theme.SURFACE1, theme.SURFACE2)
    assert theme.CONTROL in (theme.SURFACE0, theme.SURFACE1, theme.SURFACE2)
    assert theme.ON_ACCENT == theme.BASE           # text on accent colours
    assert theme.ACCENT == theme.BLUE
    assert (theme.SUCCESS, theme.WARNING, theme.ERROR) == (theme.GREEN, theme.YELLOW, theme.RED)


def test_no_mocha_colours_left_in_the_ui():
    ui = Path(theme.__file__).parent
    for path in ui.glob("*.py"):
        found = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", path.read_text("utf-8"))}
        assert not found & MOCHA, f"{path.name}: {sorted(found & MOCHA)}"


def test_app_name_uses_the_icon_purple(appmod, root, cfg_path, fast_timing):
    from conftest import make_app
    a = make_app(appmod, root)
    assert theme.BRAND == "#d279e5"
    assert a.title_label.cget("text") == "Silence Player"
    assert a.title_label.cget("fg") == theme.BRAND
    a.monitor.stop(wait=True)
