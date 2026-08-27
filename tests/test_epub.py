# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""The EPUB reader: the package document, the table of contents, and the
chapter-to-page mapping the sidebar is built from.

Books are assembled in memory here. The awkward ones matter most — a
container that points at nothing, a chapter full of HTML entities no XML
parser knows, a table of contents aimed at fragments inside one file —
because those are the books that used to fall back to a metadata card.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402

_app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])

import renderers  # noqa: E402

CONTAINER = (
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/book.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def opf(items: str, spine: str, toc_attr: str = "", title: str = "A Book"):
    return (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>%s</dc:title></metadata>"
        "<manifest>%s</manifest><spine%s>%s</spine></package>"
        % (title, items, toc_attr, spine)
    )


def chapter(body: str) -> str:
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title>'
        "</head><body>%s</body></html>" % body
    )


def build_epub(members: dict, container: str = CONTAINER) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        if container is not None:
            zf.writestr("META-INF/container.xml", container)
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def paged(data: bytes, page_w: int = 700, max_pages: int = 50):
    """Run the whole path, the way the worker does: fd in, pages out."""
    with tempfile.TemporaryFile() as fh:
        fh.write(data)
        fh.flush()
        info = None
        pages = 0
        for info, _png in renderers.epub_pages(
            fh.fileno(), "book.epub", page_w, max_pages
        ):
            pages += 1
        return info, pages


def opened(data: bytes):
    """The parse only — spine, table of contents and title."""
    with tempfile.TemporaryFile() as fh:
        fh.write(data)
        fh.flush()
        with renderers._rewound(fh.fileno()) as raw:
            with zipfile.ZipFile(raw) as zf:
                return renderers._epub_parts(zf, set(zf.namelist()))


PROSE = "<p>%s</p>" % ("The harbour road was empty at that hour. " * 40)

SIMPLE = {
    "OEBPS/book.opf": opf(
        '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"'
        ' properties="nav"/>',
        '<itemref idref="c1"/><itemref idref="c2"/>',
    ),
    "OEBPS/ch1.xhtml": chapter("<h1>One</h1>" + PROSE),
    "OEBPS/ch2.xhtml": chapter("<h1>Two</h1>" + PROSE),
    "OEBPS/nav.xhtml": chapter(
        '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>'
        '<li><a href="ch1.xhtml">One</a></li>'
        '<li><a href="ch2.xhtml">Two</a></li></ol></nav>'
    ),
}


class Package(unittest.TestCase):
    def test_spine_order_and_title(self):
        spine, toc, title = opened(build_epub(SIMPLE))
        self.assertEqual(spine, ["OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml"])
        self.assertEqual(title, "A Book")
        self.assertEqual([e["title"] for e in toc], ["One", "Two"])

    def test_container_that_points_nowhere_falls_back_to_the_opf(self):
        book = dict(SIMPLE)
        broken = CONTAINER.replace("OEBPS/book.opf", "does/not/exist.opf")
        spine, _toc, _title = opened(build_epub(book, container=broken))
        self.assertEqual(spine, ["OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml"])

    def test_a_book_with_no_package_document_is_refused(self):
        with self.assertRaises(RuntimeError):
            opened(build_epub({"OEBPS/ch1.xhtml": chapter("<p>hi</p>")},
                              container=None))

    def test_non_document_spine_items_are_left_out(self):
        # A cover declared as SVG has nothing to lay out; the spine may
        # still list it, and following it would raise mid-book.
        book = dict(SIMPLE)
        book["OEBPS/book.opf"] = opf(
            '<item id="cover" href="cover.svg" media-type="image/svg+xml"/>'
            '<item id="c1" href="ch1.xhtml"'
            ' media-type="application/xhtml+xml"/>',
            '<itemref idref="cover"/><itemref idref="c1"/>',
        )
        book["OEBPS/cover.svg"] = "<svg xmlns='http://www.w3.org/2000/svg'/>"
        spine, _toc, _title = opened(build_epub(book))
        self.assertEqual(spine, ["OEBPS/ch1.xhtml"])


class Contents(unittest.TestCase):
    def test_nav_nesting_becomes_levels(self):
        book = dict(SIMPLE)
        book["OEBPS/nav.xhtml"] = chapter(
            '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc">'
            '<ol><li><a href="ch1.xhtml">One</a>'
            '<ol><li><a href="ch1.xhtml#mid">One and a half</a></li></ol>'
            '</li><li><a href="ch2.xhtml">Two</a></li></ol></nav>'
        )
        _spine, toc, _title = opened(build_epub(book))
        self.assertEqual(
            [(e["title"], e["level"], e["target"]) for e in toc],
            [("One", 0, "OEBPS/ch1.xhtml"),
             ("One and a half", 1, "OEBPS/ch1.xhtml#mid"),
             ("Two", 0, "OEBPS/ch2.xhtml")],
        )

    def test_an_epub_2_book_reads_its_ncx(self):
        book = dict(SIMPLE)
        del book["OEBPS/nav.xhtml"]
        book["OEBPS/book.opf"] = opf(
            '<item id="c1" href="ch1.xhtml"'
            ' media-type="application/xhtml+xml"/>'
            '<item id="c2" href="ch2.xhtml"'
            ' media-type="application/xhtml+xml"/>'
            '<item id="ncx" href="toc.ncx"'
            ' media-type="application/x-dtbncx+xml"/>',
            '<itemref idref="c1"/><itemref idref="c2"/>',
            toc_attr=' toc="ncx"',
        )
        book["OEBPS/toc.ncx"] = (
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
            "<navPoint><navLabel><text>One</text></navLabel>"
            '<content src="ch1.xhtml"/>'
            "<navPoint><navLabel><text>Deeper</text></navLabel>"
            '<content src="ch1.xhtml#mid"/></navPoint></navPoint>'
            "<navPoint><navLabel><text>Two</text></navLabel>"
            '<content src="ch2.xhtml"/></navPoint></navMap></ncx>'
        )
        _spine, toc, _title = opened(build_epub(book))
        self.assertEqual(
            [(e["title"], e["level"]) for e in toc],
            [("One", 0), ("Deeper", 1), ("Two", 0)],
        )

    def test_hrefs_resolve_relative_to_the_file_that_names_them(self):
        self.assertEqual(
            renderers._zip_path("OEBPS/text", "../images/a%20b.png"),
            "OEBPS/images/a b.png",
        )
        self.assertEqual(
            renderers._zip_target("OEBPS", "ch1.xhtml#part%202"),
            "OEBPS/ch1.xhtml#part 2",
        )
        self.assertEqual(renderers._zip_target("OEBPS", "ch1.xhtml"),
                         "OEBPS/ch1.xhtml")


class Chapters(unittest.TestCase):
    """_epub_chunks: XHTML in, the subset QTextDocument lays out back."""

    def chunks(self, body: str, wanted=(), member="OEBPS/ch1.xhtml"):
        data = build_epub({member: chapter(body)}, container=None)
        with tempfile.TemporaryFile() as fh:
            fh.write(data)
            fh.flush()
            with renderers._rewound(fh.fileno()) as raw:
                with zipfile.ZipFile(raw) as zf:
                    return renderers._epub_chunks(
                        zf, member, {}, set(wanted), {}
                    )

    def test_html_entities_do_not_abort_the_chapter(self):
        # &nbsp; is an HTML entity, not an XML one: unresolved, it takes the
        # whole chapter down with it.
        chunks = self.chunks("<p>one&nbsp;two&mdash;three &amp; four</p>")
        self.assertIn("one\xa0two—three &amp; four", chunks[0][1])

    def test_script_and_style_never_reach_the_page(self):
        chunks = self.chunks(
            "<style>p { color: red }</style><script>alert(1)</script>"
            "<p>visible</p>"
        )
        html = "".join(c[1] for c in chunks)
        self.assertNotIn("color: red", html)
        self.assertNotIn("alert", html)
        self.assertIn("visible", html)

    def test_markup_is_translated_not_passed_through(self):
        chunks = self.chunks(
            '<div><h3>Head</h3><p>a <em>b</em> c<br/>d</p>'
            "<ul><li>one</li></ul></div>"
        )
        html = "".join(c[1] for c in chunks)
        self.assertIn("<h3>Head</h3>", html)
        self.assertIn("<i>b</i>", html)   # em -> i
        self.assertIn("<br>", html)
        self.assertIn("<li>one</li>", html)
        self.assertNotIn("<div>", html)   # a container, not a rendered tag

    def test_text_is_escaped(self):
        chunks = self.chunks("<p>1 &lt; 2 &amp; 3</p>")
        self.assertIn("1 &lt; 2 &amp; 3", chunks[0][1])

    def test_a_file_splits_only_at_fragments_the_contents_names(self):
        body = ('<h1 id="top">Top</h1><p>a</p>'
                '<h2 id="mid">Mid</h2><p>b</p>'
                '<h2 id="other">Other</h2><p>c</p>')
        one = self.chunks(body)
        self.assertEqual(len(one), 1)
        two = self.chunks(body, wanted=("OEBPS/ch1.xhtml#mid",))
        self.assertEqual([anchor for anchor, _html in two],
                         ["OEBPS/ch1.xhtml", "OEBPS/ch1.xhtml#mid"])
        self.assertIn("Mid", two[1][1])
        self.assertNotIn("Mid", two[0][1])


class Layout(unittest.TestCase):
    """The whole path: a book in, page images and page numbers out."""

    def test_chapters_land_on_their_own_pages(self):
        info, pages = paged(build_epub(SIMPLE))
        self.assertEqual(pages, info["count"])
        self.assertEqual(info["title"], "A Book")
        self.assertEqual([c["title"] for c in info["chapters"]],
                         ["One", "Two"])
        # A new spine document starts a new page, so the second chapter
        # cannot share a page with the first.
        self.assertEqual(info["chapters"][0]["page"], 0)
        self.assertGreater(info["chapters"][1]["page"], 0)

    def test_chapters_carry_where_on_the_page_they_start(self):
        page_w = 700
        page_h = int(page_w * renderers.PAGE_RATIO)
        info, _pages = paged(build_epub(SIMPLE), page_w=page_w)
        for chapter in info["chapters"]:
            self.assertIn("y", chapter)
            self.assertGreaterEqual(chapter["y"], 0)
            self.assertLess(chapter["y"], page_h)

    def test_a_book_with_no_contents_lists_its_own_chapters(self):
        book = dict(SIMPLE)
        del book["OEBPS/nav.xhtml"]
        book["OEBPS/book.opf"] = opf(
            '<item id="c1" href="ch1.xhtml"'
            ' media-type="application/xhtml+xml"/>'
            '<item id="c2" href="ch2.xhtml"'
            ' media-type="application/xhtml+xml"/>',
            '<itemref idref="c1"/><itemref idref="c2"/>',
        )
        info, _pages = paged(build_epub(book))
        # Falls back to each file's first heading.
        self.assertEqual([c["title"] for c in info["chapters"]],
                         ["One", "Two"])

    def test_one_broken_chapter_does_not_break_the_book(self):
        book = dict(SIMPLE)
        book["OEBPS/ch1.xhtml"] = "<html><body><p>unclosed"
        info, pages = paged(build_epub(book))
        self.assertGreater(pages, 0)
        self.assertEqual([c["title"] for c in info["chapters"]], ["Two"])

    def test_pages_stop_at_the_cap(self):
        long_book = dict(SIMPLE)
        long_book["OEBPS/ch2.xhtml"] = chapter(
            "<h1>Two</h1>" + PROSE * 60
        )
        info, pages = paged(build_epub(long_book), max_pages=3)
        self.assertEqual(pages, 3)
        self.assertEqual(info["count"], 3)
        # Every chapter the sidebar lists is a page the reader can reach.
        for entry in info["chapters"]:
            self.assertLess(entry["page"], 3)

    def test_a_zip_that_is_not_a_book_is_refused(self):
        with self.assertRaises(RuntimeError):
            paged(build_epub({"hello.txt": "not a book"}, container=None))

    def test_plain_bytes_are_refused(self):
        with self.assertRaises(RuntimeError):
            paged(b"not a zip at all")


class Search(unittest.TestCase):
    """Ctrl+F over a book: the same answer shape as the PDF search, since
    the daemon paints both with the same code."""

    def find(self, query: str, page_w: int = 500, max_pages: int = 50,
             max_hits: int = renderers.PDF_MAX_HITS) -> dict:
        with tempfile.TemporaryFile() as fh:
            fh.write(build_epub(SIMPLE))
            fh.flush()
            return renderers.search_epub(
                fh.fileno(), query, page_w, max_pages, max_hits=max_hits
            )

    def test_a_phrase_is_found_and_measured_on_its_page(self):
        page_w, out = 500, self.find("harbour road")
        page_h = int(page_w * renderers.PAGE_RATIO)
        self.assertTrue(out["matches"])
        for match in out["matches"]:
            self.assertGreaterEqual(match["page"], 0)
            self.assertTrue(match["rects"])
            for x, y, w, h in match["rects"]:
                # Inside the page it claims to be on, or the daemon paints
                # a highlight over nothing.
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + w, page_w)
                self.assertLessEqual(y + h, page_h)
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)

    def test_search_ignores_case(self):
        self.assertEqual(len(self.find("HARBOUR ROAD")["matches"]),
                         len(self.find("harbour road")["matches"]))

    def test_a_phrase_that_wraps_gets_one_rectangle_per_line(self):
        # Where a line breaks depends on the page width, so this asks
        # several widths rather than assuming one of them wraps: the claim
        # under test is that a wrapped match is measured line by line, not
        # that any particular width wraps.
        wrapped = []
        for page_w in (300, 420, 500):
            out = self.find("road was empty at that hour", page_w=page_w)
            self.assertTrue(out["matches"])
            wrapped += [m for m in out["matches"] if len(m["rects"]) > 1]
        self.assertTrue(wrapped, "no match wrapped at any width")
        for match in wrapped:
            # One box per line, never one slab covering the gap between
            # them: the rectangles sit on different rows.
            tops = {rect[1] for rect in match["rects"]}
            self.assertEqual(len(tops), len(match["rects"]))

    def test_nothing_matches_nothing(self):
        self.assertEqual(self.find("zebra unicorn")["matches"], [])
        self.assertEqual(self.find("   ")["matches"], [])

    def test_hits_are_capped(self):
        out = self.find("harbour", max_hits=4)
        self.assertTrue(out["capped"])
        self.assertEqual(len(out["matches"]), 4)

    def test_matches_past_the_rendered_pages_are_dropped(self):
        # Same rule as the outline: a highlight on a page the reader cannot
        # scroll to is worse than no highlight.
        out = self.find("harbour", max_pages=1)
        self.assertTrue(all(m["page"] == 0 for m in out["matches"]))


class Themes(unittest.TestCase):
    """Page colours are a setting, so the setting and the palettes have to
    agree, and an unknown name has to end in a readable book anyway."""

    def page(self, theme: str):
        from PySide6.QtGui import QColor, QImage

        with tempfile.TemporaryFile() as fh:
            fh.write(build_epub(SIMPLE))
            fh.flush()
            for _info, png in renderers.epub_pages(
                fh.fileno(), "book.epub", 400, 1, theme=theme
            ):
                img = QImage.fromData(png)
                self.assertFalse(img.isNull())
                return QColor(img.pixel(2, 2)).name()
        return ""

    def test_a_theme_paints_its_own_page(self):
        for name, palette in renderers.BOOK_THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(self.page(name), palette["bg"])

    def test_an_unknown_theme_falls_back_to_the_default(self):
        default = renderers.BOOK_THEMES[renderers.DEFAULT_BOOK_THEME]
        self.assertEqual(renderers.book_theme("nonsense"), default)
        self.assertEqual(renderers.book_theme(""), default)
        self.assertEqual(self.page("nonsense"), default["bg"])

    def test_the_settings_list_matches_the_palettes(self):
        # config.py names the themes a person may type; renderers.py owns
        # what they look like. A palette with no setting is unreachable, and
        # a setting with no palette renders as the default with no warning.
        import config

        self.assertEqual(
            set(config._CHOICES["book_theme"]), set(renderers.BOOK_THEMES)
        )
        self.assertIn(renderers.DEFAULT_BOOK_THEME,
                      config._CHOICES["book_theme"])


# Any PDF that carries bookmarks, named by the environment — see
# tests/test_pdf_search.py for the same arrangement and why.
SAMPLE_PDF = os.path.expanduser(os.environ.get("QUICKVIEW_TEST_PDF", ""))


@unittest.skipUnless(
    SAMPLE_PDF and os.path.exists(SAMPLE_PDF), "no QUICKVIEW_TEST_PDF"
)
class PdfOutline(unittest.TestCase):
    def test_bookmarks_come_back_flattened_with_pages(self):
        from PySide6.QtCore import QFile, QIODevice

        src = QFile(SAMPLE_PDF)
        src.open(QIODevice.OpenModeFlag.ReadOnly)
        entries = renderers.pdf_outline(src, 700, max_pages=50)
        self.assertTrue(entries)
        page_h = int(700 * renderers.PAGE_RATIO)
        for entry in entries:
            self.assertTrue(entry["title"])
            self.assertGreaterEqual(entry["page"], 0)
            self.assertGreaterEqual(entry["level"], 0)
            # Inside the page it names, in that page's rendered pixels.
            self.assertGreaterEqual(entry["y"], 0)
            self.assertLess(entry["y"], page_h * 1.05)

    def test_sections_sharing_a_page_scroll_to_different_places(self):
        # The sample's chapter 1 puts five subsections on one page. With a
        # page number alone they all scroll to the top of it.
        from PySide6.QtCore import QFile, QIODevice

        src = QFile(SAMPLE_PDF)
        src.open(QIODevice.OpenModeFlag.ReadOnly)
        entries = renderers.pdf_outline(src, 700, max_pages=50)
        by_page = {}
        for entry in entries:
            by_page.setdefault(entry["page"], []).append(entry["y"])
        shared = [ys for ys in by_page.values() if len(ys) > 1]
        self.assertTrue(shared, "sample has no page with two entries")
        for offsets in shared:
            self.assertEqual(len(set(offsets)), len(offsets))
        self.assertTrue(any(e["level"] > 0 for e in entries),
                        "sample has subsections; nesting should survive")

    def test_entries_past_the_rendered_pages_are_dropped(self):
        from PySide6.QtCore import QFile, QIODevice

        src = QFile(SAMPLE_PDF)
        src.open(QIODevice.OpenModeFlag.ReadOnly)
        entries = renderers.pdf_outline(src, 700, max_pages=2)
        self.assertTrue(all(e["page"] < 2 for e in entries))


if __name__ == "__main__":
    unittest.main()
