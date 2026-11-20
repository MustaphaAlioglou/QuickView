# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Panel themes.

The point of most of these is the same: every stylesheet has to be fillable
by every theme. A token added to one palette and forgotten in the other
raises at the moment a user opens the thing that uses it, which is exactly
the failure a test should catch instead.
"""

import os
import sys
import tempfile
from unittest.mock import patch
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([sys.argv[0]])

import config  # noqa: E402
import quickview  # noqa: E402
import theme  # noqa: E402

THEMES = ("quicklook", "breeze")


def palette_for(name):
    return theme.load(name, QApplication.palette())


class Palettes(unittest.TestCase):
    def test_both_themes_define_exactly_the_same_tokens(self):
        # The one that actually bites: a token added to the default palette
        # and not to breeze fails only when someone opens a spreadsheet.
        keys = [set(palette_for(name)) for name in THEMES]
        self.assertEqual(keys[0], keys[1])

    def test_an_unknown_theme_falls_back_to_the_default(self):
        self.assertEqual(palette_for("nonsense"), theme.QUICKLOOK)

    def test_breeze_without_a_palette_falls_back(self):
        # load() is called before the application exists in some paths.
        self.assertEqual(theme.load("breeze", None), theme.QUICKLOOK)

    def test_every_colour_token_is_a_colour(self):
        from PySide6.QtGui import QColor

        for name in THEMES:
            for key, value in palette_for(name).items():
                if key.startswith("radius") or key == "close_side":
                    continue
                with self.subTest(theme=name, token=key):
                    self.assertTrue(QColor(value).isValid(), f"{key}={value}")

    def test_geometry_tokens_carry_their_units(self):
        for name in THEMES:
            values = palette_for(name)
            for key in ("radius_panel", "radius_btn", "radius_close"):
                self.assertTrue(values[key].endswith("px"), f"{name}.{key}")
            self.assertIn(values["close_side"], ("left", "right"))


class Stylesheets(unittest.TestCase):
    def test_every_stylesheet_fills_from_every_theme(self):
        sheets = {
            "PANEL_STYLE": quickview.PANEL_STYLE,
            "TOC_STYLE": quickview.QuickView.TOC_STYLE,
            "SHEET_STYLE": quickview.QuickView.SHEET_STYLE,
        }
        for name in THEMES:
            values = palette_for(name)
            for label, template in sheets.items():
                with self.subTest(theme=name, sheet=label):
                    css = template.substitute(values)
                    self.assertNotIn("$", css)


class DefaultLookUnchanged(unittest.TestCase):
    """The default is the look the project is named after; pin it."""

    def test_the_signature_colours_are_what_they_always_were(self):
        values = theme.QUICKLOOK
        self.assertEqual(values["bg"], "#222226")
        self.assertEqual(values["close_hover"], "#ff5f57")  # traffic-light red
        self.assertEqual(values["radius_panel"], "12px")
        self.assertEqual(values["close_side"], "left")

    def test_the_default_setting_is_the_default_theme(self):
        self.assertEqual(
            config.load("/nonexistent/quickview.conf")["panel_theme"],
            "quicklook",
        )

    def test_a_typo_in_the_setting_does_not_leave_it_unpainted(self):
        os.environ["QUICKVIEW_PANEL_THEME"] = "breezy"
        self.addCleanup(os.environ.pop, "QUICKVIEW_PANEL_THEME", None)
        self.assertEqual(
            config.load("/nonexistent/quickview.conf")["panel_theme"],
            "quicklook",
        )

    def test_the_setting_is_documented_in_the_template_file(self):
        # Both names have to appear, or the file the user is handed on first
        # run does not mention the option at all.
        for name in THEMES:
            self.assertIn(name, config.DEFAULT_FILE)


class BuildsWindows:
    """Builds a QuickView under a named theme. A mixin rather than a base
    TestCase: subclassing a TestCase to reach a helper re-runs every test
    it declares."""

    def build(self, name, scheme_path=None):
        if scheme_path is None:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            scheme_path = os.path.join(directory.name, "kdeglobals")
        mock_path = patch.object(theme, "kdeglobals_path", return_value=scheme_path)
        mock_path.start()
        self.addCleanup(mock_path.stop)
        original = quickview.SETTINGS["panel_theme"]
        quickview.SETTINGS["panel_theme"] = name
        try:
            window = quickview.QuickView()
        finally:
            quickview.SETTINGS["panel_theme"] = original
        self.addCleanup(window.deleteLater)
        return window


class ThemedWindow(BuildsWindows, unittest.TestCase):
    """The widgets built with .format(**theme) rather than a Template."""

    def test_the_find_bar_builds_under_both_themes(self):
        for name in THEMES:
            with self.subTest(theme=name):
                self.build(name)._build_find_bar()

    def test_the_close_button_swaps_sides(self):
        left = self.build("quicklook").titlebar
        right = self.build("breeze").titlebar
        self.assertIs(left.layout().itemAt(0).widget(), left.close_btn)
        last = right.layout().count() - 1
        self.assertIs(right.layout().itemAt(last).widget(), right.close_btn)

    def test_the_title_still_takes_the_space_in_both(self):
        for name in THEMES:
            with self.subTest(theme=name):
                bar = self.build(name).titlebar
                stretch = [
                    i for i in range(bar.layout().count())
                    if bar.layout().stretch(i) == 1
                ]
                self.assertEqual(len(stretch), 1)
                self.assertIs(
                    bar.layout().itemAt(stretch[0]).widget(), bar.title
                )


class ASchemeChange(BuildsWindows, unittest.TestCase):
    """The daemon is resident: breeze has to follow a live scheme change."""

    def send_palette_change(self, window, palette):
        app = QApplication.instance()
        original = app.palette()
        app.setPalette(palette)
        self.addCleanup(app.setPalette, original)
        # Delivered by Qt, not handed to the window directly: the whole
        # question is whether the handler is on the method Qt actually
        # calls, and posting the event by hand answers it the wrong way.
        QCoreApplication.processEvents()

    def dark_palette(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#1b1e20"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#fcfcfc"))
        return palette

    def test_breeze_repaints_from_the_new_scheme(self):
        window = self.build("breeze")
        before = window.theme["bg"]
        self.send_palette_change(window, self.dark_palette())
        self.assertNotEqual(window.theme["bg"], before)
        self.assertEqual(window.theme["bg"], "#1b1e20")

    def test_quicklook_ignores_it(self):
        # The whole point of the default theme is that it looks the same
        # whatever the desktop is set to.
        window = self.build("quicklook")
        before = dict(window.theme)
        self.send_palette_change(window, self.dark_palette())
        self.assertEqual(window.theme, before)


class SavedDesktopScheme(BuildsWindows, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "kdeglobals")

    def write_scheme(self, dark, atomic=False):
        path = self.path + ".new" if atomic else self.path
        bg, text = ("32,35,38", "252,252,252") if dark else ("239,240,241", "35,38,41")
        with open(path, "w") as out:
            out.write(f"[Colors:Window]\nBackgroundNormal={bg}\nForegroundNormal={text}\n"
                      f"ForegroundInactive=127,128,129\n"
                      f"[Colors:View]\nBackgroundNormal={bg}\nForegroundNormal={text}\n"
                      f"[Colors:Button]\nBackgroundNormal={bg}\nForegroundNormal={text}\n")
        if atomic:
            os.replace(path, self.path)

    def wait_for_background(self, window, expected):
        from PySide6.QtTest import QTest
        for _ in range(100):
            QTest.qWait(20)
            if window.theme["bg"] == expected:
                return
        self.assertEqual(window.theme["bg"], expected)

    def test_starts_with_saved_dark_colours_even_with_a_light_qt_palette(self):
        self.write_scheme(True)
        window = self.build("breeze", self.path)
        self.assertEqual(window.theme["bg"], "#202326")
        self.assertEqual(window.theme["text"], "#fcfcfc")

    def test_watches_repeated_atomic_replacements_while_hidden(self):
        self.write_scheme(True)
        window = self.build("breeze", self.path)
        for dark, expected in ((False, "#eff0f1"), (True, "#202326"), (False, "#eff0f1")):
            self.write_scheme(dark, atomic=True)
            self.wait_for_background(window, expected)
        self.assertFalse(window.isVisible())

    def test_recolours_open_content_without_rebuilding_it(self):
        self.write_scheme(True)
        window = self.build("breeze", self.path)
        bar = window._build_find_bar()
        window.content.addWidget(bar)
        window._find_input.setText("keep this query")
        window._show_sheets_widget(
            [{"rows": [["Name", "Count"], ["Example", "12"]], "cols": 2}], False
        )
        table = window.findChild(quickview.QTableWidget)
        table.setCurrentCell(1, 1)
        self.write_scheme(False, atomic=True)
        self.wait_for_background(window, "#eff0f1")
        self.assertEqual(window._find_input.text(), "keep this query")
        self.assertIn("#232629", window._find_input.styleSheet())
        self.assertEqual(table.currentRow(), 1)
        self.assertEqual(table.currentColumn(), 1)
        self.assertEqual(table.item(0, 0).foreground().color().name(), "#232629")

    def test_missing_or_malformed_file_keeps_qt_colours(self):
        palette = QApplication.palette()
        self.assertEqual(theme.desktop_palette(palette, self.path), palette)
        with open(self.path, "w") as out:
            out.write("not an ini file")
        self.assertEqual(theme.desktop_palette(palette, self.path), palette)

    def test_bad_colour_keeps_the_corresponding_qt_role(self):
        with open(self.path, "w") as out:
            out.write("[Colors:Window]\nBackgroundNormal=999,0,0\nForegroundNormal=1,2,3\n")
        palette = QApplication.palette()
        result = theme.desktop_palette(palette, self.path)
        self.assertEqual(result.color(QPalette.Window), palette.color(QPalette.Window))
        self.assertEqual(result.color(QPalette.WindowText).name(), "#010203")

    def test_fixed_quicklook_still_ignores_saved_colours(self):
        self.write_scheme(False)
        window = self.build("quicklook", self.path)
        self.assertEqual(window.theme, theme.QUICKLOOK)


if __name__ == "__main__":
    unittest.main()
