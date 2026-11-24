# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
# SPDX-License-Identifier: GPL-3.0-or-later

"""Opt-in native Wayland smoke test.

Run with QUICKVIEW_TEST_WAYLAND=1 against a real or isolated compositor.
The child uses Wayland even if the regular unit suite uses offscreen Qt.
It opens only its own temporary image/text fixtures; no compositor-specific
commands, configuration, or external application windows are required.
"""

import os
import subprocess
import sys
import unittest


SCENARIO = r'''
import json
import os
import tempfile
from unittest.mock import patch
from PySide6.QtCore import QElapsedTimer, QEvent, QObject
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import quickview

app = QApplication([])
app.setStyle("Fusion")
app.setQuitOnLastWindowClosed(False)
with patch.dict(quickview.SETTINGS, panel_theme="quicklook"):
    window = quickview.QuickView()
window._prefetch_neighbors = lambda: None
pix = QPixmap(1200, 1200)
pix.fill()
painted = []
class Observer(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Paint:
            assert not window._placement_pending
            assert window._overlay_screen is not None
            area = window._overlay_screen.availableGeometry()
            assert window.size() == area.size()
            assert window.windowHandle().size() == area.size()
            panel = window.panel.geometry()
            assert panel.x() == (window.width() - panel.width()) // 2
            assert panel.y() == (window.height() - panel.height()) // 2
            painted.append((window.current_path, panel.width(), panel.height()))
        return False
observer = Observer(window)
window.panel.installEventFilter(observer)
try:
    with tempfile.TemporaryDirectory() as directory, \
         patch.object(window, "show_image", side_effect=lambda path: window._display_image(path, pix, "1200x1200")), \
         patch.object(window, "show_text", side_effect=lambda path: window._show_text_widget("Wayland preview")):
        image = os.path.join(directory, "image.png")
        text = os.path.join(directory, "text.txt")
        pix.save(image)
        with open(text, "w") as source:
            source.write("Wayland preview")
        for path in (image, text, image, text):
            previous = len(painted)
            window.show_file(path)
            elapsed = QElapsedTimer()
            elapsed.start()
            while len(painted) == previous and elapsed.elapsed() < 3000:
                QTest.qWait(10)
            assert len(painted) > previous, "preview was never revealed"
            window.dismiss()
            QTest.qWait(20)
        for path in (image, text):
            sizes = {(width, height) for shown, width, height in painted if shown == path}
            assert len(sizes) == 1, (path, sizes)
        print(json.dumps({"screen": app.primaryScreen().name(), "visible_paints": len(painted), "passed": True}))
finally:
    window.dismiss()
    window.pool.shutdown()
'''


class NativeWayland(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("QUICKVIEW_TEST_WAYLAND") == "1" and os.environ.get("WAYLAND_DISPLAY"),
        "requires explicit QUICKVIEW_TEST_WAYLAND=1 and a compositor",
    )
    def test_first_paint_and_repeated_content_layout(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="wayland")
        result = subprocess.run(
            [sys.executable, "-c", SCENARIO], env=environment,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"passed": true', result.stdout)
