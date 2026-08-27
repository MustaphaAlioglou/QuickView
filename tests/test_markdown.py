# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Rendered Markdown: what survives the import, and what it looks like.

The import is the part worth testing. Qt parses Markdown itself, and the
one thing that reliably breaks it — a raw HTML block, which is how half the
READMEs on GitHub open — used to cost most of the document silently.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QGuiApplication, QImage, QTextDocument  # noqa: E402

_app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])

import renderers  # noqa: E402

GITHUB = QTextDocument.MarkdownFeature.MarkdownDialectGitHub


def imported(text: str) -> str:
    """The plain text Qt keeps after parsing — what a reader would see."""
    doc = QTextDocument()
    doc.setMarkdown(renderers._strip_markdown_html(text), GITHUB)
    return doc.toPlainText()


def render(text: str, page_w: int = 400, max_pages: int = 50,
           theme: str = "gruvbox-dark") -> list:
    with tempfile.TemporaryFile() as fh:
        fh.write(text.encode("utf-8"))
        fh.flush()
        return list(renderers.markdown_pages(
            fh.fileno(), "doc.md", page_w, max_pages, 0, theme
        ))


class RawHtml(unittest.TestCase):
    def test_an_html_block_does_not_swallow_the_document(self):
        # The shape this project's own README opens with. Before the tags
        # were stripped, Qt imported 2,420 of its 22,222 characters and
        # every paragraph after the block was silently gone.
        text = (
            "# Title\n\n"
            '<p align="center">\n  <img src="a.png" width="49%">\n</p>\n\n'
            "A paragraph that must survive.\n\n"
            "- a list item that must survive\n"
        )
        body = imported(text)
        self.assertIn("A paragraph that must survive.", body)
        self.assertIn("a list item that must survive", body)

    def test_code_keeps_its_tags(self):
        kept = renderers._strip_markdown_html(
            "```html\n<img src='x'>\n```\n\nuse `<div>` inline <b>not</b>\n"
        )
        self.assertIn("<img src='x'>", kept)   # fenced block untouched
        self.assertIn("`<div>`", kept)         # inline code untouched
        self.assertNotIn("<b>", kept)          # prose tag removed
        self.assertIn("not", kept)             # its text stays

    def test_a_tilde_fence_counts_too(self):
        kept = renderers._strip_markdown_html("~~~\n<b>x</b>\n~~~\n")
        self.assertIn("<b>x</b>", kept)


class Images(unittest.TestCase):
    def test_an_image_becomes_a_note_naming_it(self):
        # Nothing in the jail can open the file next to the document, so
        # the preview says what it would have shown.
        doc = QTextDocument()
        doc.setMarkdown("Before\n\n![alt](docs/shot.png)\n\nAfter\n", GITHUB)
        renderers._label_markdown_images(doc, renderers.book_theme("paper"))
        body = doc.toPlainText()
        self.assertIn("[image: shot.png]", body)
        self.assertNotIn("￼", body)  # the object replacement character


class Theming(unittest.TestCase):
    def build(self, text: str, theme: str = "gruvbox-dark"):
        palette = renderers.book_theme(theme)
        doc = QTextDocument()
        doc.setMarkdown(text, GITHUB)
        renderers._theme_markdown(doc, palette)
        return doc, palette

    def colours(self, doc) -> dict:
        """{block text: foreground colour name} for every block."""
        out = {}
        block = doc.begin()
        while block.isValid():
            for fragment in renderers._fragments(block):
                out.setdefault(
                    fragment.text().strip(),
                    fragment.charFormat().foreground().color().name(),
                )
            block = block.next()
        return out

    def test_headings_body_and_links_take_the_palette(self):
        doc, palette = self.build(
            "# Head\n\nBody text.\n\n[a link](https://example.com)\n"
        )
        seen = self.colours(doc)
        self.assertEqual(seen["Head"], palette["head"])
        self.assertEqual(seen["Body text."], palette["fg"])
        self.assertEqual(seen["a link"], palette["head"])

    def test_a_blockquote_is_muted(self):
        doc, palette = self.build("> quoted\n")
        self.assertEqual(self.colours(doc)["quoted"], palette["muted"])

    def test_inline_code_gets_a_background(self):
        doc, palette = self.build("Some `code` here.\n")
        block = doc.begin()
        backgrounds = {
            f.text(): f.charFormat().background().color().name()
            for f in renderers._fragments(block)
        }
        self.assertNotEqual(backgrounds.get("code"), palette["bg"])


class Pages(unittest.TestCase):
    def test_a_page_is_painted_in_the_theme(self):
        for name, palette in renderers.BOOK_THEMES.items():
            with self.subTest(theme=name):
                pages = render("# Head\n\nBody.\n", max_pages=1, theme=name)
                img = QImage.fromData(pages[0][1])
                self.assertFalse(img.isNull())
                self.assertEqual(QColor(img.pixel(2, 2)).name(), palette["bg"])

    def test_pages_stop_at_the_cap(self):
        pages = render("# Head\n\n" + ("Long paragraph. " * 400 + "\n\n") * 8,
                       max_pages=2)
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0][0]["count"], 2)


class Contents(unittest.TestCase):
    """A Markdown file's headings are its table of contents."""

    def headings(self, text: str, **kw) -> list:
        return render(text, **kw)[0][0]["chapters"]

    def test_headings_become_entries_with_their_levels(self):
        entries = self.headings(
            "# Title\n\nBody.\n\n## One\n\nBody.\n\n### Deeper\n\n"
            "Body.\n\n## Two\n\nBody.\n"
        )
        self.assertEqual(
            [(e["title"], e["level"]) for e in entries],
            [("Title", 0), ("One", 1), ("Deeper", 2), ("Two", 1)],
        )

    def test_a_heading_carries_the_page_it_lands_on(self):
        entries = self.headings(
            "# First\n\n" + "Long paragraph. " * 500 + "\n\n## Later\n\nEnd.\n"
        )
        self.assertEqual(entries[0]["page"], 0)
        self.assertGreater(entries[1]["page"], 0)

    def test_headings_past_the_cap_are_dropped(self):
        entries = self.headings(
            "# First\n\n" + "Long paragraph. " * 900 + "\n\n## Later\n\nEnd.\n",
            max_pages=1,
        )
        self.assertEqual([e["title"] for e in entries], ["First"])

    def test_headings_sharing_a_page_get_their_own_offsets(self):
        # The bug this fixes: a page is taller than the window, so entries
        # that only carry a page number all scroll to the same place and
        # clicking them looks like nothing happening.
        entries = self.headings(
            "# One\n\nShort.\n\n## Two\n\nShort.\n\n## Three\n\nShort.\n"
        )
        self.assertEqual({e["page"] for e in entries}, {0})
        offsets = [e["y"] for e in entries]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(len(set(offsets)), len(offsets))

    def test_an_offset_stays_inside_its_page(self):
        page_w = 400
        page_h = int(page_w * renderers.PAGE_RATIO)
        entries = self.headings(
            "# One\n\n" + ("Long paragraph. " * 300)
            + "\n\n## Two\n\nEnd.\n", page_w=page_w
        )
        for entry in entries:
            self.assertGreaterEqual(entry["y"], 0)
            self.assertLess(entry["y"], page_h)

    def test_a_document_with_no_headings_has_no_contents(self):
        # Which is an answer, not a failure: the button stays hidden.
        self.assertEqual(self.headings("Just a paragraph.\n"), [])

    def test_code_that_looks_like_a_heading_is_not_one(self):
        entries = self.headings("# Real\n\n```sh\n# not a heading\n```\n")
        self.assertEqual([e["title"] for e in entries], ["Real"])


class CodeWrapping(unittest.TestCase):
    def test_a_long_code_line_wraps_instead_of_being_cut(self):
        # A page cannot scroll sideways, so an unbreakable line is simply
        # lost past the margin.
        from PySide6.QtCore import QSizeF

        text = "```ini\n%s\n```\n" % ("setting = value  # " * 12)
        doc = QTextDocument()
        doc.setDocumentMargin(30)
        doc.setMarkdown(text, GITHUB)
        doc.setPageSize(QSizeF(400, 560))
        before = doc.idealWidth()
        renderers._theme_markdown(doc, renderers.book_theme("paper"))
        doc.setPageSize(QSizeF(400, 560))
        self.assertGreater(before, 400)
        self.assertLessEqual(doc.idealWidth(), 400)

    def test_an_empty_document_is_refused(self):
        # Nothing to render falls back to the source view, not a blank page.
        with self.assertRaises(RuntimeError):
            render("   \n\n")

    def test_tables_are_widened_to_the_page(self):
        from PySide6.QtGui import QTextLength

        doc = QTextDocument()
        doc.setMarkdown("| a | b |\n|---|---|\n| 1 | 2 |\n", GITHUB)
        renderers._theme_markdown(doc, renderers.book_theme("paper"))
        table = doc.rootFrame().childFrames()[0].format().toTableFormat()
        self.assertEqual(
            table.width().type(), QTextLength.Type.PercentageLength
        )
        self.assertEqual(table.width().rawValue(), 100)


if __name__ == "__main__":
    unittest.main()
