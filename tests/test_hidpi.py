# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Page images on a scaled screen: more pixels, same geometry.

On a 1.25x or 1.5x display a page rendered at its logical width gets
stretched by the compositor, which is what made PDFs blurry next to Okular.
The fix renders with the screen's ratio; what must *not* change is the
logical geometry that search highlights, the outline and the chapter list
are measured in.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMarginsF, QSizeF  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QFont, QGuiApplication, QImage, QPageSize, QPainter, QPdfWriter,
    QTextDocument,
)

_app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])

import renderers  # noqa: E402


def write_pdf(path: str, pages: int = 2):
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(20, 20, 20, 20))
    painter = QPainter(writer)
    painter.setFont(QFont("serif", 11))
    for n in range(pages):
        if n:
            writer.newPage()
        for line in range(40):
            painter.drawText(0, 300 + line * 250, "Page %d, line %d: the quick brown fox" % (n, line))
    painter.end()


def pdf_pages(scale: float, page_w: int = 600) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.pdf")
        write_pdf(path)
        return [QImage.fromData(png)
                for _count, png in renderers.render_pdf(path, page_w, 5, 0, scale)]


def detail(img: QImage) -> int:
    """Hard edges across the whole page: blur turns them into ramps."""
    grey = img.convertToFormat(QImage.Format.Format_Grayscale8)
    stride, w = grey.bytesPerLine(), grey.width()
    data = bytes(grey.constBits())
    edges = 0
    for y in range(grey.height()):
        row = data[y * stride:y * stride + w]
        edges += sum(1 for a, b in zip(row, row[1:]) if abs(a - b) > 100)
    return edges


class PdfScale(unittest.TestCase):
    def test_a_scaled_page_has_the_pixels_and_says_so(self):
        one, = pdf_pages(1.0)[:1]
        big, = pdf_pages(1.5)[:1]
        self.assertEqual(one.width(), 600)
        self.assertEqual(big.width(), 900)
        self.assertAlmostEqual(big.height() / one.height(), 1.5, delta=0.01)
        self.assertEqual(float(big.text("QuickView:Scale")), 1.5)
        self.assertEqual(float(one.text("QuickView:Scale")), 1.0)

    def test_the_extra_pixels_are_real_detail_not_an_upscale(self):
        # A page stretched 1.5x keeps the 1x page's edge count; one rendered
        # at 1.5x has sharp edges at the finer spacing and so more of them.
        from PySide6.QtCore import Qt

        one = pdf_pages(1.0)[0]
        big = pdf_pages(1.5)[0]
        # What the compositor used to do with the 1x page.
        stretched = one.scaled(big.width(), big.height(),
                               Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        self.assertGreater(detail(big), detail(stretched) * 3)

    def test_the_ratio_is_clamped(self):
        self.assertEqual(renderers._scaled_px(100, 200, 100)[2], 4.0)
        self.assertEqual(renderers._scaled_px(100, 200, 0.2)[2], 1.0)
        self.assertEqual(renderers._scaled_px(100, 200, "junk")[2], 1.0)
        # Never past the page cap, and the ratio reported is the one used.
        w, h, used = renderers._scaled_px(4000, 8000, 2.0)
        self.assertLessEqual(max(w, h), renderers.PDF_MAX_PAGE_PX)
        self.assertAlmostEqual(used, renderers.PDF_MAX_PAGE_PX / 8000)

    def test_search_geometry_is_unchanged_by_the_scale(self):
        # Highlights are drawn in logical pixels over a page whose logical
        # size must be what it was before: page_w, not page_w * scale.
        one = pdf_pages(1.0)[0]
        big = pdf_pages(1.5)[0]
        big.setDevicePixelRatio(float(big.text("QuickView:Scale")))
        self.assertEqual(big.deviceIndependentSize().toSize(), one.size())


class LaidOutScale(unittest.TestCase):
    """Books, Markdown and office pages: scale the painting, not the layout."""

    def pages(self, scale: float) -> list:
        doc = QTextDocument()
        doc.setDocumentMargin(30)
        doc.setPlainText("A line of text that wraps. " * 400)
        doc.setPageSize(QSizeF(500, 700))
        count = doc.pageCount()
        return [QImage.fromData(png) for _n, png in renderers._document_pages(
            doc, 500, 700, count, 0, "#ffffff", scale)]

    def test_same_pages_more_pixels(self):
        one = self.pages(1.0)
        big = self.pages(1.25)
        # Same page count: the text was not re-flowed into a wider page.
        self.assertEqual(len(one), len(big))
        self.assertEqual((big[0].width(), big[0].height()), (625, 875))
        self.assertEqual(float(big[0].text("QuickView:Scale")), 1.25)
        self.assertGreater(detail(big[0]), detail(one[0]))


if __name__ == "__main__":
    unittest.main()
