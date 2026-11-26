"""Silence Player's colours: Catppuccin Frappé.

The official Frappé palette (https://github.com/catppuccin/catppuccin), and the
role each colour plays in the app, following Catppuccin's style guide
(docs/style-guide.md): Base for the background pane, Mantle/Crust for secondary
panes, Surface 0-2 for surface elements, Text for body copy, Subtext for labels,
Base for text on accent colours, Blue for links and pills, Green for success,
Yellow for warnings and Red for errors.
"""

# ── Catppuccin Frappé palette ────────────────────────────────────────────────
ROSEWATER = "#f2d5cf"
FLAMINGO  = "#eebebe"
PINK      = "#f4b8e4"
MAUVE     = "#ca9ee6"
RED       = "#e78284"
MAROON    = "#ea999c"
PEACH     = "#ef9f76"
YELLOW    = "#e5c890"
GREEN     = "#a6d189"
TEAL      = "#81c8be"
SKY       = "#99d1db"
SAPPHIRE  = "#85c1dc"
BLUE      = "#8caaee"
LAVENDER  = "#babbf1"
TEXT      = "#c6d0f5"
SUBTEXT1  = "#b5bfe2"
SUBTEXT0  = "#a5adce"
OVERLAY2  = "#949cbb"
OVERLAY1  = "#838ba7"
OVERLAY0  = "#737994"
SURFACE2  = "#626880"
SURFACE1  = "#51576d"
SURFACE0  = "#414559"
BASE      = "#303446"
MANTLE    = "#292c3c"
CRUST     = "#232634"

# ── Silence Player's own colour (not Catppuccin) ─────────────────────────────
BRAND     = "#d279e5"   # the light purple of the app/tray icon (the ghost's body)

# ── roles ────────────────────────────────────────────────────────────────────
BG        = BASE        # window background (background pane)
PANE      = MANTLE      # status bar along the bottom (secondary pane)
CARD      = SURFACE0    # cards and banners (surface elements)
CONTROL   = SURFACE1    # buttons, fields, lists, slider tracks on those cards
PRESSED   = SURFACE2    # a button while it's held down
LABEL     = SUBTEXT0    # descriptions, captions, secondary text
SUBTLE    = OVERLAY2    # dimmed text: "off" states (Overlay 2 rather than the
                        # guide's Overlay 1 so it stays readable on Surface 0/1)
DISABLED  = OVERLAY0    # text of a control that can't be used right now
ON_ACCENT = BASE        # text on an accent-coloured button or badge
ACCENT    = BLUE        # selected tab/mode, links, highlights, pills
SUCCESS   = GREEN
WARNING   = YELLOW
ERROR     = RED
