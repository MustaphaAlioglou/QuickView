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

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
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
        with patch.object(window, "fit_overlay", wraps=window.fit_overlay) as fit, \
             patch.object(window, "_schedule_overlay_fit", wraps=window._schedule_overlay_fit) as scheduled:
            window.show()
            self.assertGreater(fit.call_count, 0)
            window.hide()
            window.show()
            fit.reset_mock()
            scheduled.reset_mock()
            screen = QApplication.primaryScreen()
            window.windowHandle().screenChanged.emit(screen)
            scheduled.assert_called_once_with(screen)
            QApplication.processEvents()
            self.assertGreaterEqual(fit.call_count, 1)
        window.hide()

    def test_screen_change_refits_after_platform_restores_old_geometry(self):
        window = self.window
        window.show()
        portrait = self.screen(QRect(-1080, -240, 1080, 1920))
        with patch.object(window, "screen", return_value=portrait):
            window._schedule_overlay_fit(portrait)
            # QtWayland finishes processing the output change after emitting
            # the signal, and can overwrite a synchronous resize with the old
            # landscape geometry. The queued fit must run after that step.
            window.resize(2560, 1440)
            self.assertEqual(window.size(), QSize(2560, 1440))
            QApplication.processEvents()
            self.assertEqual(window.size(), QSize(1080, 1920))
            self.assert_panel_centred()
        window.hide()

    def test_wayland_dismiss_recreates_native_window_but_keeps_warm_state(self):
        window = self.window
        window.show()
        pool = window.pool
        window._mem_cache["cached-preview"] = object()
        window._overlay_fit_timer.start(0)
        with patch.object(quickview.QGuiApplication, "platformName", return_value="wayland"):
            window.dismiss()
        self.assertIsNone(window.windowHandle())
        self.assertIsNone(window._overlay_handle)
        self.assertIsNone(window._overlay_screen)
        self.assertFalse(window._overlay_fit_timer.isActive())
        window.show()
        self.assertIsNotNone(window.windowHandle())
        self.assertIs(window._overlay_handle, window.windowHandle())
        self.assertIs(window.pool, pool)
        self.assertIn("cached-preview", window._mem_cache)
        self.assert_panel_centred()
        window.hide()

    def test_wayland_remaps_with_the_final_native_size_after_output_assignment(self):
        window = self.window
        window.show()
        portrait = self.screen(QRect(-1080, -240, 1080, 1920))
        with patch.object(quickview.QGuiApplication, "platformName", return_value="wayland"), \
             patch.object(window, "hide", wraps=window.hide) as hide:
            window._schedule_overlay_fit(portrait)
            window.windowHandle().resize(2560, 1440)
            QApplication.processEvents()
            hide.assert_called_once_with()
            self.assertTrue(window.isVisible())
            self.assertEqual(window.size(), QSize(1080, 1920))
            self.assertEqual(window.windowHandle().size(), QSize(1080, 1920))
            self.assert_panel_centred()
        window.hide()

    def test_decode_box_does_not_depend_on_the_previously_used_output(self):
        primary = self.screen(QRect(0, 0, 2560, 1440))
        portrait = self.screen(QRect(-1080, -240, 1080, 1920))
        with patch.object(quickview.QGuiApplication, "screens", return_value=[primary, portrait]):
            for previous in (primary, portrait, primary):
                with patch.object(self.window, "screen", return_value=previous):
                    self.assertEqual(self.window.image_fit_box(), (2128, 1536))

    def test_image_panel_shrinks_both_axes_to_match_the_image(self):
        window = self.window
        pix = QPixmap(1152, 1152)
        pix.fill(QColor("red"))
        for geometry in (QRect(-1080, -240, 1080, 1920), QRect(0, 0, 2560, 1440)):
            with self.subTest(geometry=geometry):
                with patch.object(window, "screen", return_value=self.screen(geometry)), \
                     patch.object(window, "_prefetch_neighbors"):
                    window.fit_overlay()
                    window._display_image("/tmp/test.png", pix, "1200x1200")
                    self.assertEqual(window.panel.width() - 24, window.panel.height() - 64)
                    self.assertGreater(window.panel.width(), 800)
                    self.assert_panel_centred()

    def test_small_images_keep_the_original_minimum_panel_size(self):
        window = self.window
        pix = QPixmap(100, 100)
        pix.fill(QColor("red"))
        with patch.object(window, "_prefetch_neighbors"):
            window.fit_overlay(self.screen(QRect(0, 0, 2560, 1440)))
            window._display_image("/tmp/small.png", pix, "100x100")
        self.assertEqual(window.panel.size(), QSize(480, 360))

    def test_repeated_image_layout_is_independent_of_old_panel_request(self):
        window = self.window
        pix = QPixmap(1200, 1200)
        pix.fill(QColor("red"))
        with patch.object(window, "_prefetch_neighbors"):
            window.fit_overlay(self.screen(QRect(-1080, -240, 1080, 1920)))
            window._display_image("/tmp/square.png", pix, "1200x1200")
        first = window.panel.geometry()
        window.set_panel_size(500, 500)
        self.assertEqual(window.panel.geometry(), first)

    def test_wayland_first_panel_paint_waits_for_final_output_placement(self):
        from PySide6.QtTest import QTest

        window = self.window
        painted_sizes = []

        class PaintObserver(QObject):
            def eventFilter(self, watched, event):
                if event.type() == QEvent.Paint:
                    painted_sizes.append(window.size())
                return False

        observer = PaintObserver(window)
        window.panel.installEventFilter(observer)
        with patch.object(quickview.QGuiApplication, "platformName", return_value="wayland"):
            window.show_file("/nonexistent/quickview-placement.txt")
            QApplication.processEvents()
            self.assertTrue(window.panel.isHidden())
            self.assertEqual(painted_sizes, [])
            portrait = self.screen(QRect(-1080, -240, 1080, 1920))
            window._schedule_overlay_fit(portrait)
            QApplication.processEvents()
            self.assertEqual(painted_sizes, [])
            QTest.qWait(100)
            self.assertTrue(window.panel.isVisible())
            self.assertTrue(painted_sizes)
            self.assertTrue(all(size == QSize(1080, 1920) for size in painted_sizes))
            self.assert_panel_centred()
            window.dismiss()

    def test_dismiss_cancels_a_pending_reveal(self):
        from PySide6.QtTest import QTest

        window = self.window
        with patch.object(quickview.QGuiApplication, "platformName", return_value="wayland"):
            window.show_file("/nonexistent/quickview-placement.txt")
            self.assertTrue(window._placement_pending)
            window.dismiss()
            QTest.qWait(100)
            self.assertFalse(window.isVisible())
            self.assertFalse(window._placement_pending)
            self.assertFalse(window._panel_reveal_timer.isActive())

    def test_navigation_in_an_open_preview_does_not_hide_the_panel(self):
        from PySide6.QtTest import QTest

        window = self.window
        with patch.object(quickview.QGuiApplication, "platformName", return_value="wayland"):
            window.show_file("/nonexistent/quickview-first.txt")
            QTest.qWait(100)
            self.assertTrue(window.panel.isVisible())
            window.show_file("/nonexistent/quickview-next.txt")
            self.assertTrue(window.panel.isVisible())
            self.assertFalse(window._placement_pending)
            self.assertFalse(window._panel_reveal_timer.isActive())
            window.dismiss()

class ImageFitsViewport(unittest.TestCase):
    def setUp(self):
        pix = QPixmap(200, 100)
        pix.fill(QColor("red"))
        painter = QPainter(pix)
        painter.fillRect(100, 0, 100, 50, QColor("green"))
        painter.fillRect(0, 50, 100, 50, QColor("blue"))
        painter.fillRect(100, 50, 100, 50, QColor("yellow"))
        painter.end()
        self.label = quickview.ImageLabel()
        self.label.setPixmap(pix)
        self.addCleanup(self.label.deleteLater)

    def assert_corners(self, points):
        image = self.label.grab().toImage()
        for point, colour in zip(points, ("red", "green", "blue", "yellow")):
            self.assertEqual(image.pixelColor(*point), QColor(colour))

    def test_narrow_portrait_view_keeps_all_four_image_corners(self):
        self.label.resize(100, 200)
        self.assert_corners(((20, 86), (80, 86), (20, 112), (80, 112)))

    def test_reexpanding_uses_the_original_decoded_pixels(self):
        self.label.resize(100, 200)
        self.label.grab()
        self.label.resize(200, 100)
        self.assertEqual(self.label.pixmap().size(), QSize(200, 100))
        self.assert_corners(((35, 20), (165, 20), (35, 80), (165, 80)))

    def test_a_hidpi_image_keeps_its_logical_size(self):
        pix = self.label.pixmap()
        pix.setDevicePixelRatio(2)
        self.label.setPixmap(pix)
        self.label.resize(100, 100)
        self.assert_corners(((20, 36), (80, 36), (20, 62), (80, 62)))


if __name__ == "__main__":
    unittest.main()
