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


def _packbits(row: bytes) -> bytes:
    """Encode one scanline the way Photoshop does — literals only.

    A literal-only stream is legal PackBits and exercises the same decoder
    path; the repeat opcode gets its own row in the fixtures below.
    """
    out = bytearray()
    for i in range(0, len(row), 128):
        chunk = row[i:i + 128]
        out += bytes([len(chunk) - 1]) + chunk
    return bytes(out)


def build_psd(w, h, planes, *, mode=3, depth=8, psb=False, rle=True,
              palette=b"", thumb=None, repeat_rows=False):
    """A minimal but real .psd/.psb, built from one bytes object per channel.

    Photoshop files are not something to commit as binaries, and the parser
    under test is the only thing here that reads one — so the fixtures are
    written by hand, one section at a time, the same way the other suites
    synthesise their zips and PDFs.
    """
    def u(n, v):
        return v.to_bytes(n, "big")

    channels = len(planes)
    out = bytearray(b"8BPS" + u(2, 2 if psb else 1) + b"\0" * 6)
    out += u(2, channels) + u(4, h) + u(4, w) + u(2, depth) + u(2, mode)
    out += u(4, len(palette)) + palette

    resources = bytearray()
    if thumb is not None:
        body = (u(4, 1) + u(4, 0) + u(4, 0) + u(4, 0) + u(4, 0) + u(4, 0)
                + u(2, 24) + u(2, 1) + thumb)
        resources += b"8BIM" + u(2, 1036) + b"\0\0" + u(4, len(body)) + body
        if len(body) & 1:
            resources += b"\0"
    out += u(4, len(resources)) + resources
    out += u(8 if psb else 4, 0)        # no layers at all

    sample = depth // 8
    row_bytes = w * sample
    if not rle:
        out += u(2, 0) + b"".join(planes)
        return bytes(out)

    counts, body = [], bytearray()
    for plane in planes:
        for y in range(h):
            row = plane[y * row_bytes:(y + 1) * row_bytes]
            if repeat_rows and len(set(row)) == 1:
                enc = bytes([257 - len(row)]) + row[:1] if len(row) > 1 else b"\0" + row
            else:
                enc = _packbits(row)
            counts.append(len(enc))
            body += enc
    out += u(2, 1)
    for c in counts:
        out += u(4 if psb else 2, c)
    return bytes(out + body)


def gradient_plane(w, h, base):
    return bytes(bytearray((base + x + y) & 0xFF for y in range(h) for x in range(w)))


class PsdDecoding(unittest.TestCase):
    """Photoshop files, which Qt cannot read and renderers.py parses itself."""

    def decoded(self, data, max_w=400, max_h=400):
        path = os.path.join(self.tmp, "f.psd")
        with open(path, "wb") as fh:
            fh.write(data)
        return renderers.decode_image(path, max_w, max_h)

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = self._dir.name
        self.addCleanup(self._dir.cleanup)

    def test_an_rgb_composite_decodes_with_its_true_size(self):
        w, h = 16, 12
        planes = [bytes([200] * w * h), bytes([100] * w * h), bytes([50] * w * h)]
        img, orig = self.decoded(build_psd(w, h, planes))
        self.assertEqual(orig, "16×12")
        self.assertEqual((img.width(), img.height()), (16, 12))
        self.assertEqual(img.pixelColor(3, 4).getRgb()[:3], (200, 100, 50))

    def test_raw_and_rle_agree(self):
        w, h = 20, 9
        planes = [gradient_plane(w, h, b) for b in (0, 90, 180)]
        rle, _ = self.decoded(build_psd(w, h, planes, rle=True))
        raw, _ = self.decoded(build_psd(w, h, planes, rle=False))
        self.assertEqual(renderers.raw_frame(rle), renderers.raw_frame(raw))

    def test_the_repeat_opcode_decodes(self):
        w, h = 24, 4
        planes = [bytes([7] * w * h), bytes([8] * w * h), bytes([9] * w * h)]
        img, _ = self.decoded(build_psd(w, h, planes, repeat_rows=True))
        self.assertEqual(img.pixelColor(11, 2).getRgb()[:3], (7, 8, 9))

    def test_a_fourth_channel_is_transparency(self):
        w, h = 8, 8
        planes = [bytes([10] * w * h)] * 3 + [bytes([64] * w * h)]
        img, _ = self.decoded(build_psd(w, h, planes))
        self.assertEqual(img.pixelColor(1, 1).alpha(), 64)

    def test_greyscale_decodes(self):
        w, h = 8, 8
        img, _ = self.decoded(build_psd(w, h, [bytes([77] * w * h)], mode=1))
        self.assertEqual(img.pixelColor(4, 4).getRgb()[:3], (77, 77, 77))

    def test_indexed_colour_goes_through_the_palette(self):
        w, h = 6, 6
        palette = bytearray(768)
        palette[3], palette[256 + 3], palette[512 + 3] = 11, 22, 33
        img, _ = self.decoded(
            build_psd(w, h, [bytes([3] * w * h)], mode=2, palette=bytes(palette))
        )
        self.assertEqual(img.pixelColor(2, 2).getRgb()[:3], (11, 22, 33))

    def test_cmyk_is_multiplied_back_to_rgb(self):
        # Stored CMYK is inverted, so 255 is no ink at all: pure cyan is
        # C=0 with the rest wide open, and must come back as (0, 255, 255).
        w, h = 4, 4
        planes = [bytes([0] * w * h)] + [bytes([255] * w * h)] * 3
        img, _ = self.decoded(build_psd(w, h, planes, mode=4))
        self.assertEqual(img.pixelColor(1, 1).getRgb()[:3], (0, 255, 255))

    def test_sixteen_bit_files_take_the_high_byte(self):
        w, h = 8, 8
        planes = [bytes([v, 0xFF] * (w * h)) for v in (12, 34, 56)]
        img, _ = self.decoded(build_psd(w, h, planes, depth=16))
        self.assertEqual(img.pixelColor(3, 3).getRgb()[:3], (12, 34, 56))

    def test_psb_widens_the_row_table_and_still_decodes(self):
        w, h = 10, 5
        planes = [gradient_plane(w, h, b) for b in (0, 40, 80)]
        img, orig = self.decoded(build_psd(w, h, planes, psb=True))
        self.assertEqual(orig, "10×5")
        self.assertEqual((img.width(), img.height()), (10, 5))

    def test_a_large_canvas_is_decimated_not_decoded_whole(self):
        # The point of the row table: a canvas far bigger than the box asked
        # for costs a fraction of its rows. Correctness proxy — the result
        # still fits the box and keeps the document's own dimensions.
        w, h = 400, 300
        planes = [gradient_plane(w, h, b) for b in (0, 60, 120)]
        img, orig = self.decoded(build_psd(w, h, planes), max_w=100, max_h=100)
        self.assertEqual(orig, "400×300")
        self.assertLessEqual(img.width(), 100)
        self.assertLessEqual(img.height(), 100)

    def test_the_thumbnail_covers_a_missing_composite(self):
        # A file saved without "Maximize Compatibility" has no merged image
        # at all; the embedded JPEG is all there is to show.
        w, h = 32, 32
        jpeg = renderers._encode(solid(w, h, QColor(9, 9, 200)), "JPEG")
        data = bytearray(build_psd(w, h, [bytes([1] * w * h)] * 3, thumb=jpeg))
        img, orig = self.decoded(bytes(data[:-40]))
        self.assertEqual(orig, "32×32")
        self.assertFalse(img.isNull())

    def test_a_truncated_file_is_an_error_not_a_hang(self):
        data = build_psd(8, 8, [bytes([1] * 64)] * 3)
        with self.assertRaises(RuntimeError):
            self.decoded(data[:30])

    def test_an_absurd_canvas_is_refused_before_allocating(self):
        header = (b"8BPS" + b"\0\1" + b"\0" * 6 + b"\0\3"
                  + (0xFFFF).to_bytes(4, "big") + (0xFFFF).to_bytes(4, "big")
                  + b"\0\x08" + b"\0\3")
        with self.assertRaises(RuntimeError):
            self.decoded(header + b"\0" * 64)

    def test_a_lying_layer_length_is_refused(self):
        data = bytearray(build_psd(8, 8, [bytes([1] * 64)] * 3))
        at = data.index(b"8BPS") + 26 + 4 + 4      # colour mode + resources
        data[at:at + 4] = (1 << 30).to_bytes(4, "big")
        with self.assertRaises(RuntimeError):
            self.decoded(bytes(data))

    def test_a_non_psd_still_reports_qt_s_error(self):
        with self.assertRaises(RuntimeError):
            self.decoded(b"not an image at all")


def build_kra(members):
    """A .kra/.ora is a zip; only the member names matter to the decoder."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/x-krita")
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class KraDecoding(unittest.TestCase):
    """Krita and OpenRaster files, read as the zips they are."""

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = self._dir.name

    def decoded(self, data, name="f.kra", max_w=400, max_h=400):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return renderers.decode_image(path, max_w, max_h)

    def png(self, w, h, colour):
        return renderers._encode(solid(w, h, colour))

    def test_the_merged_image_is_the_preview(self):
        img, orig = self.decoded(
            build_kra({"mergedimage.png": self.png(24, 18, QColor(3, 240, 7))})
        )
        self.assertEqual(orig, "24×18")
        self.assertEqual(img.pixelColor(5, 5).getRgb()[:3], (3, 240, 7))

    def test_preview_covers_a_missing_merged_image(self):
        img, _ = self.decoded(
            build_kra({"preview.png": self.png(16, 16, QColor(1, 2, 250))})
        )
        self.assertEqual(img.pixelColor(2, 2).getRgb()[:3], (1, 2, 250))

    def test_the_merged_image_wins_over_the_preview(self):
        img, _ = self.decoded(build_kra({
            "mergedimage.png": self.png(30, 30, QColor(200, 0, 0)),
            "preview.png": self.png(8, 8, QColor(0, 0, 200)),
        }))
        self.assertEqual(img.pixelColor(1, 1).getRgb()[:3], (200, 0, 0))

    def test_a_corrupt_merged_image_falls_back_to_the_preview(self):
        img, _ = self.decoded(build_kra({
            "mergedimage.png": b"\x89PNG\r\n\x1a\n" + b"junk" * 20,
            "preview.png": self.png(8, 8, QColor(0, 190, 0)),
        }))
        self.assertEqual(img.pixelColor(2, 2).getRgb()[:3], (0, 190, 0))

    def test_an_openraster_file_takes_the_same_path(self):
        img, orig = self.decoded(
            build_kra({"mergedimage.png": self.png(12, 9, QColor(9, 9, 9))}),
            name="f.ora",
        )
        self.assertEqual(orig, "12×9")

    def test_a_zip_bomb_is_refused_by_its_declared_size(self):
        # One member that inflates to far more than the cap. Nothing may be
        # decompressed to find that out — the zip header says so.
        bomb = build_kra({"mergedimage.png": b"\0" * (128 * 1024 * 1024)})
        self.assertLess(len(bomb), 1024 * 1024)     # it really is a bomb
        with self.assertRaises(RuntimeError):
            self.decoded(bomb)

    def test_a_zip_with_nothing_to_show_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.decoded(build_kra({"layers/layer1.png": b"nope"}))


class PsdBounds(unittest.TestCase):
    """The guards that stand between a header field and an allocation."""

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = self._dir.name

    def decoded(self, data, max_w=400, max_h=400):
        path = os.path.join(self.tmp, "f.psd")
        with open(path, "wb") as fh:
            fh.write(data)
        return renderers.decode_image(path, max_w, max_h)

    def test_a_degenerate_canvas_is_refused_before_the_buffer(self):
        # One pixel wide and millions tall: under the pixel cap, and no
        # whole-number stride can bring it inside the box, so the interleave
        # would allocate hundreds of MB for something unshowable.
        def u(n, v):
            return v.to_bytes(n, "big")

        w, h = 1, 20_000_000
        header = (b"8BPS" + u(2, 1) + b"\0" * 6 + u(2, 3)
                  + u(4, h) + u(4, w) + u(2, 8) + u(2, 3))
        with self.assertRaises(RuntimeError):
            self.decoded(header + u(4, 0) + u(4, 0) + u(4, 0) + u(2, 0))

    def test_an_oversized_thumbnail_resource_is_not_read(self):
        w, h = 8, 8
        jpeg = renderers._encode(solid(w, h, QColor(1, 1, 1)), "JPEG")
        data = build_psd(w, h, [bytes([1] * 64)] * 3, thumb=jpeg)
        # Restate the resource's length as something far past the cap. The
        # section bound rejects it, so no 9 MiB read is ever attempted.
        at = data.index(b"8BIM")
        blown = bytearray(data)
        blown[at + 8:at + 12] = (renderers.PSD_MAX_THUMB_BYTES + 1).to_bytes(4, "big")
        # The composite is still intact, so the file previews anyway — what
        # matters is that the decoder never tries to honour the stated size.
        img, _ = self.decoded(bytes(blown))
        self.assertFalse(img.isNull())


class PackBitsTolerance(unittest.TestCase):
    """PackBits allows a bad encoder, so the decoder has to as well."""

    def test_a_literal_opcode_per_byte_still_decodes(self):
        # Valid, just wasteful — twice the size of the row it codes. The
        # scanline bound has to leave room for it.
        row = bytes(range(200))
        naive = b"".join(b"\x00" + bytes([b]) for b in row)
        self.assertEqual(len(naive), 2 * len(row))
        self.assertEqual(renderers._unpack_bits(naive, len(row)), row)

    def test_an_absurdly_long_scanline_is_still_refused(self):
        with self.assertRaises(renderers._PsdError):
            renderers._unpack_bits(b"\x00\x01" * 5000, 100)
