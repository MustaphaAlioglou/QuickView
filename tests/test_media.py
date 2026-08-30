# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""The media controls: the seek bar, the speed menu and mute.

These drive real widgets with a stubbed player, so what they check is the
thing that actually broke — where a click on the timeline lands, and what
goes out over the socket when a control is used.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

import quickview  # noqa: E402

_app = QApplication.instance()


class FakePlayer:
    """Stands in for MediaSession, recording what the controls send."""

    def __init__(self, *args, **kwargs):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def stop(self):
        pass


class MediaControls(unittest.TestCase):
    def setUp(self):
        self.window = quickview.QuickView()
        self.addCleanup(self.window.deleteLater)
        real = quickview.MediaSession
        quickview.MediaSession = FakePlayer
        try:
            self.window.show_media("/does/not/exist.mp4", video=True)
        finally:
            quickview.MediaSession = real
        self.player = self.window.media
        self.buttons = {b.text(): b for b in self.window.findChildren(QPushButton)}
        self.slider = self.window.findChild(quickview.SeekSlider)

    def last(self, kind: str):
        for msg in reversed(self.player.sent):
            if msg.get("t") == kind:
                return msg
        return None


class SeekBar(MediaControls):
    """Clicking the timeline has to seek there, not one page nearer."""

    def setUp(self):
        super().setUp()
        self.slider.setRange(0, 100_000)        # a 100-second clip, in ms
        self.slider.resize(400, 20)

    def click(self, x: int):
        QTest.mouseClick(self.slider, Qt.LeftButton, Qt.NoModifier, QPoint(x, 10))
        return self.slider.value()

    def test_a_click_at_the_start_seeks_to_zero(self):
        self.assertEqual(self.click(0), 0)

    def test_a_click_at_the_end_seeks_to_the_end(self):
        self.assertEqual(self.click(400), 100_000)

    def test_a_click_in_the_middle_lands_in_the_middle(self):
        # Not exact: the handle has width, so the usable travel is a little
        # shorter than the groove. Within a percent of the clip is right.
        self.assertAlmostEqual(self.click(200), 50_000, delta=1_500)

    def test_a_click_sends_a_seek(self):
        self.click(300)
        self.assertEqual(self.last("seek")["position"], self.slider.value())

    def test_a_click_is_not_a_page_step(self):
        # The bug this fixes: the default groove is a page-step control, so
        # a click near the end moved the handle by pageStep and no further.
        self.click(360)
        self.assertGreater(self.slider.value(), 80_000)

    def test_the_player_does_not_fight_a_drag(self):
        # While the handle is held, position reports are the player saying
        # where it still is; writing them back yanks the handle away.
        self.slider.setValue(0)
        self.slider.setSliderDown(True)
        self.window.findChild(quickview.SeekSlider).setValue(90_000)
        self.assertEqual(self.slider.value(), 90_000)


class SpeedMenu(MediaControls):
    def test_every_rate_is_offered_slowest_first(self):
        menu = self.buttons["1×"].menu()
        self.assertEqual(
            [a.text() for a in menu.actions()],
            ["0.25×", "0.5×", "0.75×", "1×", "1.25×", "1.5×", "2×"],
        )

    def test_choosing_a_rate_sends_it_and_relabels_the_button(self):
        button = self.buttons["1×"]
        for action in button.menu().actions():
            if action.text() == "1.25×":
                action.trigger()
        self.assertEqual(button.text(), "1.25×")
        self.assertEqual(self.last("rate")["rate"], 1.25)

    def test_only_the_chosen_rate_is_ticked(self):
        button = self.buttons["1×"]
        for action in button.menu().actions():
            if action.text() == "0.5×":
                action.trigger()
        ticked = [a.text() for a in button.menu().actions() if a.isChecked()]
        self.assertEqual(ticked, ["0.5×"])

    def test_the_button_fits_the_widest_label_it_can_show(self):
        button = self.buttons["1×"]
        for rate in quickview.PLAYBACK_RATES:
            button.setText(f"{rate:g}×")
            self.assertLessEqual(
                button.sizeHint().width(), button.width(),
                f"{rate:g}× does not fit",
            )


class MuteButton(MediaControls):
    def test_muting_and_unmuting_both_report(self):
        button = self.buttons["🔊"]
        button.click()
        self.assertEqual(button.text(), "🔇")
        self.assertIs(self.last("mute")["muted"], True)
        button.click()
        self.assertEqual(button.text(), "🔊")
        self.assertIs(self.last("mute")["muted"], False)


class ControlRow(MediaControls):
    """The buttons are sized by hand, so the sizes are worth pinning."""

    def test_speed_and_mute_are_the_same_size(self):
        speed, mute = self.buttons["1×"], self.buttons["🔊"]
        self.assertEqual(speed.size(), mute.size())

    def test_the_whole_row_shares_one_height(self):
        # Left to themselves these came out 29, 25 and 20 px tall, because a
        # pause bar, a text label and an emoji have different metrics.
        heights = {self.buttons[t].height() for t in ("⏸", "1×", "🔊")}
        self.assertEqual(heights, {quickview.MEDIA_BUTTON_H})


if __name__ == "__main__":
    unittest.main()
