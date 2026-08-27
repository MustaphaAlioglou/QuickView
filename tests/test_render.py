# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Rendering and the raw-frame wire format. Needs Qt, but runs offscreen."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication, QColor, QImage  # noqa: E402

_app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])

import quickview  # noqa: E402
import renderers  # noqa: E402


def solid(w, h, colour=QColor(10, 200, 30, 255), fmt=None):
    img = QImage(w, h, fmt or QImage.Format.Format_ARGB32)
    img.fill(colour)
    return img


class RawFrameGuard(unittest.TestCase):
    """image_from_raw is fed by the process that just parsed a hostile file.

    Every rejection here is a buffer overread that Qt would otherwise
    perform, so these are the tests that matter most in this file.
    """

    def setUp(self):
        self.img = solid(40, 30, QColor(10, 200, 30, 128))
        self.raw, self.geom = renderers.raw_frame(self.img)

    def rebuilt(self, data=None, **override):
        return quickview.image_from_raw(
            self.raw if data is None else data, {**self.geom, **override}
        )

    def test_a_valid_frame_rebuilds(self):
        out = self.rebuilt()
        self.assertFalse(out.isNull())
        self.assertEqual((out.width(), out.height()), (40, 30))

    def test_alpha_survives(self):
        # The reason the wire format is raw ARGB32 and not BMP.
        self.assertEqual(self.rebuilt().pixelColor(5, 5).alpha(), 128)

    def test_oversized_stride_is_rejected(self):
        self.assertTrue(self.rebuilt(stride=self.geom["stride"] * 99).isNull())

    def test_short_buffer_is_rejected(self):
        self.assertTrue(self.rebuilt(self.raw[:-100]).isNull())

    def test_stride_narrower_than_a_row_is_rejected(self):
        self.assertTrue(self.rebuilt(stride=4).isNull())

    def test_negative_and_zero_dimensions_are_rejected(self):
        self.assertTrue(self.rebuilt(h=-5).isNull())
        self.assertTrue(self.rebuilt(w=0).isNull())

    def test_an_unknown_format_is_rejected(self):
        self.assertTrue(self.rebuilt(fmt="rgb565").isNull())

    def test_garbage_and_missing_fields_are_rejected(self):
        self.assertTrue(self.rebuilt(stride="abc").isNull())
        self.assertTrue(quickview.image_from_raw(self.raw, {}).isNull())
        self.assertTrue(quickview.image_from_raw(b"", self.geom).isNull())


class CacheEncoding(unittest.TestCase):
    def test_an_opaque_image_is_cached_as_jpeg(self):
        blob = renderers.encode_cached(solid(64, 64, QColor(200, 40, 40)))
        self.assertTrue(quickview.is_cache_blob(blob))
        self.assertEqual(blob[:3], quickview.JPEG_MAGIC)

    def test_alpha_forces_png(self):
        # JPEG has no alpha: a transparent logo would come back on black.
        img = solid(64, 64, QColor(255, 0, 0, 0))
        blob = renderers.encode_cached(img)
        self.assertEqual(blob[:4], quickview.PNG_MAGIC)
        back = QImage()
        back.loadFromData(blob)
        self.assertEqual(back.pixelColor(10, 10).alpha(), 0)

    def test_the_dimensions_chunk_survives_both_formats(self):
        # The disk cache-hit path reads it back off the cached image.
        for colour in (QColor(200, 40, 40), QColor(255, 0, 0, 0)):
            img = solid(64, 64, colour)
            img.setText("QuickView:OrigSize", "4000×3000")
            back = QImage()
            back.loadFromData(renderers.encode_cached(img))
            self.assertEqual(back.text("QuickView:OrigSize"), "4000×3000")

    def test_junk_is_not_mistaken_for_a_cache_entry(self):
        self.assertFalse(quickview.is_cache_blob(b"garbage"))
        self.assertFalse(quickview.is_cache_blob(b""))


class CacheKey(unittest.TestCase):
    def test_the_version_is_part_of_the_key(self):
        # Without it, changing a renderer leaves the old output served for
        # ever, because nothing else in the key describes how it was made.
        st = os.stat(__file__)
        first = quickview.cache_key(__file__, st, 100, 100)
        original = quickview.CACHE_VERSION
        quickview.CACHE_VERSION = original + 1
        try:
            self.assertNotEqual(quickview.cache_key(__file__, st, 100, 100), first)
        finally:
            quickview.CACHE_VERSION = original

    def test_the_fit_box_is_part_of_the_key(self):
        st = os.stat(__file__)
        self.assertNotEqual(
            quickview.cache_key(__file__, st, 100, 100),
            quickview.cache_key(__file__, st, 200, 100),
        )


if __name__ == "__main__":
    unittest.main()
