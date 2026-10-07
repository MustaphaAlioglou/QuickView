# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Ctrl+F: text flattening, and that search geometry matches the renderer."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([sys.argv[0]])

import renderers  # noqa: E402
import samplepdf  # noqa: E402


class Flatten(unittest.TestCase):
    """The index map is what lets a match be located in the real text."""

    def test_the_map_points_back_at_the_source(self):
        text = "Hello\r\nWorld"
        flat, back = renderers._flatten(text)
        self.assertEqual(flat, "hello world")
        for i, ch in enumerate(flat):
            if ch != " ":
                self.assertEqual(text[back[i]].casefold(), ch)

    def test_a_run_of_whitespace_becomes_one_space(self):
        self.assertEqual(renderers._flatten("a  \r\n\t b")[0], "a b")

    def test_leading_and_trailing_whitespace_goes(self):
        self.assertEqual(renderers._flatten("  \r\n a \t ")[0], "a")

    def test_drop_spaces_removes_them_entirely(self):
        # For PDFs that position glyphs instead of emitting spaces.
        self.assertEqual(renderers._flatten("soft skills", True)[0], "softskills")

    def test_the_map_stays_aligned_when_casefold_expands(self):
        # ß casefolds to "ss": two output characters from one source one.
        flat, back = renderers._flatten("straße")
        self.assertEqual(flat, "strasse")
        self.assertEqual(len(back), len(flat))
        self.assertTrue(all(0 <= i < len("straße") for i in back))

    def test_empty_input(self):
        self.assertEqual(renderers._flatten("")[0], "")
        self.assertEqual(renderers._flatten("   ")[0], "")


# Searched in a PDF built by tests/samplepdf.py, whose text is known, so the
# expected counts come from what was written rather than from one person's
# document — and the checks run everywhere, CI included.
class Search(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = samplepdf.write(os.path.join(cls.tmp.name, "sample.pdf"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def hits(self, query, **kw):
        return renderers.search_pdf(self.pdf, query, 1645, 50, **kw)

    def test_a_single_word(self):
        self.assertEqual(len(self.hits(samplepdf.WORD)["matches"]),
                         samplepdf.count(samplepdf.WORD))

    def test_case_is_ignored(self):
        self.assertEqual(len(self.hits(samplepdf.WORD.upper())["matches"]),
                         samplepdf.count(samplepdf.WORD))

    def test_a_phrase_across_a_line_break(self):
        # Reads as continuous on the page; the text layer has \r\n in it.
        out = self.hits(samplepdf.PHRASE)
        self.assertEqual(len(out["matches"]), 1)
        self.assertFalse(out["loose"])
        # One rectangle per line the match spans, not one slab over both.
        self.assertEqual(len(out["matches"][0]["rects"]), 2)

    def test_query_whitespace_is_tolerated(self):
        padded = "  " + "   ".join(samplepdf.PHRASE.split()) + " "
        self.assertEqual(
            len(self.hits(padded)["matches"]),
            len(self.hits(samplepdf.PHRASE)["matches"]),
        )

    def test_no_match(self):
        out = self.hits("zzzznotpresentanywhere")
        self.assertEqual(out["matches"], [])
        self.assertFalse(out["loose"])

    def test_an_empty_query(self):
        self.assertEqual(self.hits("")["matches"], [])

    def test_the_hit_cap(self):
        self.assertGreater(samplepdf.count("the"), 10)
        out = self.hits("the", max_hits=10)
        self.assertTrue(out["capped"])
        self.assertEqual(len(out["matches"]), 10)

    def test_only_the_shown_pages_are_searched(self):
        # A hit on a page the viewer never renders is worse than silence.
        out = renderers.search_pdf(self.pdf, samplepdf.WORD, 1645, 2)
        self.assertTrue(out["matches"])
        self.assertTrue(all(m["page"] < 2 for m in out["matches"]))

    def test_rectangles_land_inside_the_page(self):
        doc = renderers._open_pdf(self.pdf)
        for hit in self.hits(samplepdf.WORD)["matches"]:
            w, h = renderers._page_px(doc, hit["page"], 1645)
            for x, y, rw, rh in hit["rects"]:
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + rw, w)
                self.assertLessEqual(y + rh, h)

    def test_each_highlight_covers_the_text_it_reports(self):
        # Read back what lies under every rectangle. A match whose offsets
        # are not mapped back through _flatten's index still produces
        # rectangles of about the right size — one character to the side
        # per line break above it — so only the text under them shows it.
        from PySide6.QtCore import QPointF

        doc = renderers._open_pdf(self.pdf)
        for query in (samplepdf.WORD, samplepdf.PHRASE):
            for hit in self.hits(query)["matches"]:
                w, h = renderers._page_px(doc, hit["page"], 1645)
                pt = doc.pagePointSize(hit["page"])
                sx, sy = w / pt.width(), h / pt.height()
                under = []
                for x, y, rw, rh in hit["rects"]:
                    mid = (y + rh / 2) / sy
                    sel = doc.getSelection(hit["page"], QPointF((x + 1) / sx, mid),
                                           QPointF((x + rw - 1) / sx, mid))
                    under.append(sel.text())
                self.assertEqual(renderers._flatten(" ".join(under))[0],
                                 renderers._flatten(query)[0],
                                 f"page {hit['page']}: {under}")

    def test_search_geometry_matches_what_the_renderer_emits(self):
        # The drift that would put every highlight in the wrong place.
        from PySide6.QtGui import QImage

        doc = renderers._open_pdf(self.pdf)
        for page in (0, 7):
            _count, png = next(renderers.render_pdf(self.pdf, 1645, page + 1, page))
            img = QImage()
            img.loadFromData(png)
            self.assertEqual(
                (img.width(), img.height()), renderers._page_px(doc, page, 1645)
            )

if __name__ == "__main__":
    unittest.main()
