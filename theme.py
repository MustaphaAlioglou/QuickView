#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Panel palettes.

The window has always been a Quick Look impression: a fixed dark panel with
a circular close button top left, drawn the same on every desktop and
ignoring the Plasma colour scheme entirely. That is still the default and
still the point — it is what the project set out to look like.

`breeze` is the alternative for people who would rather the previewer
matched the rest of their desktop. It takes its colours from the running
QPalette, which under Plasma is the user's own scheme, so it follows a
light theme, a dark one and an accent colour without being told.

Both themes are the same set of tokens, so there is one stylesheet in
quickview.py rather than two to keep in step. The quicklook values below
are the literals that were previously written into those stylesheets, which
is what makes the default look byte-identical to what it replaced.

Tokens are substituted with string.Template ($name), not str.format: CSS is
mostly braces, and doubling every one of them to escape it is a bug waiting
to be written.
"""

# Every colour token, with the values the panel has always used. Some of
# these are a shade apart from each other (#2a2a2e against #2b2b30) and a
# tidier palette would merge them — but merging would change the default
# look, and the default look is not what this change is for.
QUICKLOOK = {
    # Surfaces, back to front.
    "bg": "#222226",            # the panel itself
    "bg_alt": "#26262b",        # sidebar, alternating table rows
    "surface": "#2b2b30",       # menus, table headers, tabs
    "surface_alt": "#2a2a2e",   # text inputs, an unrendered page
    "surface_head": "#2f2f36",  # a spreadsheet's own header row
    "border": "#3c3c40",        # panel and menu outline
    "line": "#34343a",          # gridlines, dividers
    # Text, brightest first.
    "text_bright": "#f2f2f5",
    "text": "#e8e8ea",
    "text_dim": "#dcdcde",
    "text_soft": "#d0d0d4",
    "text_faint": "#9a9aa2",
    "text_head": "#8e8e98",
    "text_status": "#8e8e96",
    "text_muted": "#7e7e88",
    "tree_text": "#c8c8ce",
    "tab_text": "#b6b6bd",
    # Buttons.
    "btn": "#38383c",
    "btn_hover": "#4a4a4d",
    "close_bg": "#5a5a5f",
    "close_hover": "#ff5f57",       # the traffic-light red this imitates
    "close_hover_text": "#4b0d0a",
    "input_border": "#3a3a40",
    "input_hover": "#35353b",
    # Selection and accent.
    "accent": "#4f6a99",
    "accent_text": "#ffffff",
    "sel_tree": "#3f4f70",
    "sel_table": "#3a4a63",
    "tree_hover": "#303036",
    "tab_sel": "#3c3c44",
    "tab_sel_text": "#ffffff",
    # Furniture.
    "scroll_handle": "#4a4a51",
    "slider_groove": "#454548",
    "slider_handle": "#e8e8ea",
    # Geometry, and where the close button goes. Quick Look puts it top
    # left, which is the one piece of this that is layout rather than paint.
    "radius_panel": "12px",
    "radius_btn": "6px",
    "radius_close": "12px",     # half of the 24px button: a circle
    "close_side": "left",
}


def _hex(colour) -> str:
    return colour.name()


def _mix(a, b, weight: float):
    """`weight` of b over a."""
    from PySide6.QtGui import QColor

    return QColor(
        round(a.red() * (1 - weight) + b.red() * weight),
        round(a.green() * (1 - weight) + b.green() * weight),
        round(a.blue() * (1 - weight) + b.blue() * weight),
    )


def _shift(colour, amount: int, dark: bool):
    """Move a colour away from the background: lighter on a dark scheme."""
    from PySide6.QtGui import QColor

    delta = amount if dark else -amount
    return QColor(
        min(max(colour.red() + delta, 0), 255),
        min(max(colour.green() + delta, 0), 255),
        min(max(colour.blue() + delta, 0), 255),
    )


def breeze(palette) -> dict:
    """The palette the desktop is already using.

    Everything is read from QPalette rather than named, so this follows the
    user's Plasma colour scheme — light or dark, and whatever accent they
    picked — instead of being a second hardcoded theme that happens to look
    like today's Breeze.
    """
    from PySide6.QtGui import QPalette

    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    win = palette.color(role.Window)
    text = palette.color(role.WindowText)
    base = palette.color(role.Base)
    alt = palette.color(role.AlternateBase)
    button = palette.color(role.Button)
    btn_text = palette.color(role.ButtonText)
    highlight = palette.color(role.Highlight)
    highlight_text = palette.color(role.HighlightedText)
    mid = palette.color(role.Mid)
    faint = palette.color(group.Disabled, role.WindowText)
    dark = win.lightness() < 128

    return {
        "bg": _hex(win),
        "bg_alt": _hex(alt),
        "surface": _hex(button),
        "surface_alt": _hex(base),
        "surface_head": _hex(_shift(button, 8, dark)),
        "border": _hex(mid),
        "line": _hex(_mix(win, mid, 0.6)),

        "text_bright": _hex(text),
        "text": _hex(text),
        "text_dim": _hex(text),
        "text_soft": _hex(btn_text),
        "text_faint": _hex(faint),
        "text_head": _hex(faint),
        "text_status": _hex(faint),
        "text_muted": _hex(faint),
        "tree_text": _hex(text),
        "tab_text": _hex(faint),

        "btn": _hex(button),
        # Breeze tints a hovered control towards the accent rather than
        # simply lightening it, which is most of why its buttons feel
        # like Breeze buttons.
        "btn_hover": _hex(_mix(button, highlight, 0.3)),
        "close_bg": _hex(button),
        # Plasma's own close button goes red on hover too; this is Breeze's
        # negative colour rather than the macOS traffic light.
        "close_hover": "#da4453",
        "close_hover_text": "#ffffff",
        "input_border": _hex(mid),
        "input_hover": _hex(_mix(base, highlight, 0.2)),

        "accent": _hex(highlight),
        "accent_text": _hex(highlight_text),
        "sel_tree": _hex(highlight),
        "sel_table": _hex(highlight),
        "tree_hover": _hex(_mix(alt, highlight, 0.2)),
        "tab_sel": _hex(base),
        "tab_sel_text": _hex(text),

        "scroll_handle": _hex(_mix(win, text, 0.35)),
        "slider_groove": _hex(mid),
        "slider_handle": _hex(highlight),

        # Breeze is a squarer look than Quick Look's: 3px on controls, a
        # small radius on the window, and the close button on the right
        # where every Plasma window decoration puts it.
        "radius_panel": "6px",
        "radius_btn": "3px",
        "radius_close": "3px",
        "close_side": "right",
    }


def load(name: str, palette=None) -> dict:
    """The named theme, falling back to the default one."""
    if name == "breeze" and palette is not None:
        return breeze(palette)
    return dict(QUICKLOOK)
