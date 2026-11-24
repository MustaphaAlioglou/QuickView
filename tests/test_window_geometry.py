# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Panel placement on outputs with different sizes and desktop offsets."""

import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QSize  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([sys.argv[0]])

import quickview  # noqa: E402


class WindowGeometry(unittest.TestCase):
    def setUp(self):
        with patch.dict(quickview.SETTINGS, panel_theme="quicklook"):
            self.window = quickview.QuickView()
        self.addCleanup(self.window.deleteLater)

    def screen(self, geometry):
        return Mock(availableGeometry=Mock(return_value=geometry))

    def assert_panel_fits(self):
        window = self.window
        panel = window.panel.geometry()
        left, top, right, bottom = window._margins
        self.assertGreaterEqual(panel.left(), left)
        self.assertGreaterEqual(panel.top(), top)
        self.assertLessEqual(panel.x() + panel.width() + right, window.width())
        self.assertLessEqual(panel.y() + panel.height() + bottom, window.height())

    def assert_panel_centred(self):
        window = self.window
        self.assertEqual(window.panel.x(), (window.width() - window.panel.width()) // 2)
        self.assertEqual(window.panel.y(), (window.height() - window.panel.height()) // 2)
        self.assert_panel_fits()

    def test_centres_independently_of_global_window_position(self):
        # Wayland may report (0, 0) even for an output at (-1080, -240).
        # Assert that layout never needs that unreliable global mapping.
        layouts = (
            QRect(-1080, -240, 1080, 1920),
            QRect(2560, -240, 1080, 1920),
            QRect(0, -1920, 1080, 1920),
            QRect(0, 30, 2560, 1410),
        )
        for geometry in layouts:
            with self.subTest(geometry=geometry):
                screen = self.screen(geometry)
                with patch.object(self.window, "screen", return_value=screen), \
                     patch.object(self.window, "mapFromGlobal", side_effect=AssertionError("global mapping")):
                    self.window.fit_overlay()
                    self.assertEqual(self.window.size(), geometry.size())
                    self.assert_panel_centred()

    def test_new_output_overrides_the_hidden_windows_old_screen(self):
        primary = self.screen(QRect(0, 30, 2560, 1410))
        portrait = self.screen(QRect(-1080, -240, 1080, 1920))
        window = self.window
        with patch.object(window, "screen", return_value=primary):
            window.fit_overlay()
            window.set_panel_size(1800, 900)
            # The signal's output takes precedence over the stale screen.
            window.fit_overlay(portrait)
        self.assertEqual(window.size(), QSize(1080, 1920))
        self.assert_panel_centred()

    def test_portrait_to_landscape_restores_requested_panel_size(self):
        window = self.window
        window._panel_size = QSize(1800, 1000)
        window.fit_overlay(self.screen(QRect(-1080, -240, 1080, 1920)))
        self.assert_panel_centred()
        window.fit_overlay(self.screen(QRect(0, 30, 2560, 1410)))
        self.assertEqual(window.panel.width(), 1800 - 48)
        self.assert_panel_centred()

    def test_drag_position_is_clamped_when_output_becomes_narrower(self):
        window = self.window
        window.fit_overlay(self.screen(QRect(0, 30, 2560, 1410)))
        window.move_panel(QPoint(1700, 300))
        window.fit_overlay(self.screen(QRect(-1080, -240, 1080, 1920)))
        self.assertIsNotNone(window._panel_pos)
        self.assert_panel_fits()

    def test_refitting_repositions_even_without_a_resize_event(self):
        window = self.window
        screen = self.screen(QRect(-1080, -240, 1080, 1920))
        window.fit_overlay(screen)
        window.panel.move(0, 0)
        window.fit_overlay(screen)
        self.assert_panel_centred()

    def test_show_refits_and_connects_screen_changes_only_once(self):
        window = self.window
        with patch.object(window, "fit_overlay", wraps=window.fit_overlay) as fit:
            window.show()
            self.assertGreater(fit.call_count, 0)
            window.hide()
            window.show()
            fit.reset_mock()
            screen = QApplication.primaryScreen()
            window.windowHandle().screenChanged.emit(screen)
            QApplication.processEvents()
            fit.assert_called_once_with()
        window.hide()

    def test_screen_change_refits_after_platform_restores_old_geometry(self):
        window = self.window
        window.show()
        portrait = self.screen(QRect(-1080, -240, 1080, 1920))
        with patch.object(window, "screen", return_value=portrait):
            window.windowHandle().screenChanged.emit(QApplication.primaryScreen())
            # QtWayland finishes processing the output change after emitting
            # the signal, and can overwrite a synchronous resize with the old
            # landscape geometry. The queued fit must run after that step.
            window.resize(2560, 1440)
            self.assertEqual(window.size(), QSize(2560, 1440))
            QApplication.processEvents()
            self.assertEqual(window.size(), QSize(1080, 1920))
            self.assert_panel_centred()
        window.hide()


if __name__ == "__main__":
    unittest.main()
