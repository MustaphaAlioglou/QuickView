# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Word-processor previews: the LibreOffice path and the built-in layout.

The LibreOffice path is tested against stand-in executables, so its
fallbacks — no suite, a suite that fails, a suite that hangs — run on every
machine. One test converts for real, and only where LibreOffice exists.
Documents are assembled in memory, as in test_sheets.
"""

import io
import os
import stat
import sys
import tempfile
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([sys.argv[0]])

import renderers  # noqa: E402

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
)
REL_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
CONTENT_TYPES = (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
ROOT_RELS = (
    '<Relationships %s><Relationship Id="rId1" Target="word/document.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument"/></Relationships>' % REL_NS
)


def png_bytes() -> bytes:
    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def build_docx(body: str, media=None, styles: str = "") -> bytes:
    """body: the XML inside <w:body>. media: {rId: (member, bytes)}."""
    media = media or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # The two parts a real reader insists on, so LibreOffice opens it.
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("word/document.xml",
                    "<w:document %s><w:body>%s</w:body></w:document>" % (NS, body))
        if media:
            zf.writestr("word/_rels/document.xml.rels",
                        "<Relationships %s>%s</Relationships>" % (REL_NS, "".join(
                            '<Relationship Id="%s" Target="%s" Type="x"/>'
                            % (rid, member[len("word/"):])
                            for rid, (member, _data) in media.items())))
            for member, data in media.values():
                zf.writestr(member, data)
        if styles:
            zf.writestr("word/styles.xml", "<w:styles %s>%s</w:styles>" % (NS, styles))
    return buf.getvalue()


def to_html(data: bytes, page_w: int = 827) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        html, _images = renderers._docx_html(zf, set(zf.namelist()), page_w)
    return html


def para(text: str, rpr: str = "", ppr: str = "") -> str:
    return "<w:p>%s<w:r>%s<w:t>%s</w:t></w:r></w:p>" % (
        "<w:pPr>%s</w:pPr>" % ppr if ppr else "",
        "<w:rPr>%s</w:rPr>" % rpr if rpr else "", text)


def drawing(rid: str, cx: int, cy: int) -> str:
    return (
        '<w:r><w:drawing><wp:inline><wp:extent cx="%d" cy="%d"/>'
        '<a:graphic><a:graphicData><a:blip r:embed="%s"/></a:graphicData>'
        '</a:graphic></wp:inline></w:drawing></w:r>' % (cx, cy, rid)
    )


def pages(data: bytes, max_pages: int = 20) -> list:
    """The whole office path, the way the worker runs it: fd in, pages out."""
    with tempfile.TemporaryFile() as fh:
        fh.write(data)
        fh.flush()
        return list(renderers.office_pages(fh.fileno(), "doc.docx", 600, max_pages))


class BuiltInLayout(unittest.TestCase):
    """What a machine without LibreOffice sees."""

    def test_table_text_appears_once(self):
        body = ("<w:tbl><w:tr><w:tc>%s</w:tc></w:tr></w:tbl>" % para("CELL"))
        self.assertEqual(to_html(build_docx(body)).count("CELL"), 1)

    def test_a_switched_off_toggle_is_off(self):
        html = to_html(build_docx(
            para("plain", '<w:b w:val="0"/><w:i w:val="false"/>')
            + para("strong", "<w:b/>")))
        self.assertNotIn("<b>plain", html)
        self.assertNotIn("<i>plain", html)
        self.assertIn("<b>strong", html)

    def test_images_keep_the_size_the_document_gives_them(self):
        # page_w 827 over an 8.27 in page is 100 px to the inch.
        media = {"rId1": ("word/media/a.png", png_bytes())}
        body = "<w:p>%s</w:p>" % drawing("rId1", 914400, 457200)  # 1 x 0.5 in
        html = to_html(build_docx(body, media))
        self.assertIn("width='100' height='50'", html)

    def test_an_image_wider_than_the_column_is_shrunk_in_proportion(self):
        media = {"rId1": ("word/media/a.png", png_bytes())}
        body = "<w:p>%s</w:p>" % drawing("rId1", 914400 * 20, 914400 * 10)
        html = to_html(build_docx(body, media))
        w = int(html.split("width='")[1].split("'")[0])
        h = int(html.split("height='")[1].split("'")[0])
        self.assertLess(w, 827)
        self.assertAlmostEqual(w / h, 2.0, delta=0.02)

    def test_vector_images_qt_cannot_read_are_left_out(self):
        media = {"rId1": ("word/media/logo.emf", b"\x01\x00\x00\x00")}
        body = "<w:p>%s</w:p>" % drawing("rId1", 914400, 914400)
        self.assertNotIn("<img", to_html(build_docx(body, media)))

    def test_headings_are_found_by_style_name_not_id(self):
        # What a Greek-language Word writes: the id says nothing.
        styles = ('<w:style w:type="paragraph" w:styleId="1">'
                  '<w:name w:val="heading 1"/></w:style>')
        html = to_html(build_docx(
            para("Εισαγωγή", ppr='<w:pStyle w:val="1"/>'), styles=styles))
        self.assertIn("<h1>Εισαγωγή</h1>", html)

    def test_a_page_break_starts_a_new_page(self):
        body = ('<w:p><w:r><w:t>before</w:t><w:br w:type="page"/>'
                '<w:t>after</w:t></w:r></w:p>')
        html = to_html(build_docx(body))
        self.assertRegex(html, r'page-break-before:always">after')

    def test_justified_paragraphs_stay_justified(self):
        html = to_html(build_docx(para("x", ppr='<w:jc w:val="both"/>')))
        self.assertIn("text-align:justify", html)

    def test_a_text_box_is_read_once_not_once_per_fallback(self):
        box = ("<w:txbxContent>%s</w:txbxContent>" % para("BOXED"))
        body = (
            "<w:p><w:r><mc:AlternateContent>"
            "<mc:Choice><w:drawing>%s</w:drawing></mc:Choice>"
            "<mc:Fallback><w:pict>%s</w:pict></mc:Fallback>"
            "</mc:AlternateContent></w:r></w:p>" % (box, box)
        )
        self.assertEqual(to_html(build_docx(body)).count("BOXED"), 1)

    def test_body_size_follows_the_document_defaults(self):
        styles = ('<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="24"/>'
                  '</w:rPr></w:rPrDefault></w:docDefaults>')
        # 12 pt at 100 px to the inch.
        html = to_html(build_docx(para("x"), styles=styles))
        self.assertIn("font-size:17px", html)


class SuiteStandIn(unittest.TestCase):
    """office_pages with a fake soffice in place of LibreOffice."""

    def setUp(self):
        self._saved = (renderers.OFFICE_SUITES, renderers.OFFICE_SUITE_TIMEOUT)
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = build_docx(para("hello") + para("world"))

    def tearDown(self):
        renderers.OFFICE_SUITES, renderers.OFFICE_SUITE_TIMEOUT = self._saved
        self.tmp.cleanup()

    def stand_in(self, script: str) -> str:
        path = os.path.join(self.tmp.name, "soffice")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + script)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        renderers.OFFICE_SUITES = (path,)
        return path

    def built_in_count(self) -> int:
        renderers.OFFICE_SUITES = ()
        return len(pages(self.doc))

    def test_no_suite_uses_the_built_in_layout(self):
        renderers.OFFICE_SUITES = ("/nonexistent/soffice",)
        self.assertEqual(renderers.office_suite(), "")
        self.assertGreater(len(pages(self.doc)), 0)

    def test_a_failing_suite_falls_back_without_repeating_pages(self):
        expected = self.built_in_count()
        self.stand_in("exit 1\n")
        self.assertEqual(len(pages(self.doc)), expected)

    def test_a_hanging_suite_is_killed_with_its_children(self):
        # soffice is a launcher that forks the converter: the timeout has
        # to take down the child as well, or it runs on in the jail.
        pidfile = os.path.join(self.tmp.name, "child.pid")
        renderers.OFFICE_SUITE_TIMEOUT = 1
        expected = self.built_in_count()
        self.stand_in("sleep 60 &\necho $! > %s\nwait\n" % pidfile)
        started = time.monotonic()
        self.assertEqual(len(pages(self.doc)), expected)
        self.assertLess(time.monotonic() - started, 10)
        with open(pidfile) as fh:
            child = int(fh.read())
        time.sleep(0.2)
        try:
            with open("/proc/%d/status" % child) as fh:
                state = next(l for l in fh if l.startswith("State:"))
            self.assertIn("Z", state, "converter child still running")
        except FileNotFoundError:
            pass  # gone entirely: what we want

    def test_the_builtin_setting_never_starts_the_suite(self):
        # office_engine = builtin: faster previews even with LibreOffice
        # installed. The stand-in leaves a mark if it is ever run.
        mark = os.path.join(self.tmp.name, "ran")
        expected = self.built_in_count()
        self.stand_in("touch %s\nexit 1\n" % mark)
        with tempfile.TemporaryFile() as fh:
            fh.write(self.doc)
            fh.flush()
            got = list(renderers.office_pages(
                fh.fileno(), "doc.docx", 600, 20, engine="builtin"))
        self.assertEqual(len(got), expected)
        self.assertFalse(os.path.exists(mark), "LibreOffice was started")

    def test_spreadsheets_are_not_sent_to_the_suite(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
        buf.seek(0)
        self.assertEqual(renderers._office_suite_kind(buf), "")

    def test_odt_is_recognised_from_its_mimetype_member(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            zf.writestr("content.xml", "<x/>")
        buf.seek(0)
        self.assertEqual(renderers._office_suite_kind(buf), ".odt")


def ole(entries=(), body=b"") -> io.BytesIO:
    """An OLE2 compound file reduced to what the kind check reads: the
    signature, then directory entries at 128-byte boundaries — name in
    UTF-16, zero-padded, its byte length at offset 64 — then body."""
    import struct

    data = bytearray(renderers._OLE_MAGIC + bytes(504))
    for name in entries:
        raw = name.encode("utf-16-le") + b"\0\0"
        data += raw.ljust(64, b"\0") + struct.pack("<H", len(raw)) + bytes(62)
    return io.BytesIO(bytes(data) + body)


class SuiteKind(unittest.TestCase):
    """Which files go to LibreOffice, and under which extension."""

    def zipped(self, members) -> io.BytesIO:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        buf.seek(0)
        return buf

    def kind(self, fh) -> str:
        return renderers._office_suite_kind(fh)

    def test_a_pptx_is_recognised_from_its_presentation_part(self):
        fh = self.zipped({"ppt/presentation.xml": "<p:presentation/>"})
        self.assertEqual(self.kind(fh), ".pptx")

    def test_an_odp_is_recognised_from_its_mimetype_member(self):
        fh = self.zipped({"mimetype": "application/vnd.oasis.opendocument.presentation",
                          "content.xml": "<x/>"})
        self.assertEqual(self.kind(fh), ".odp")

    def test_an_ods_is_still_left_to_the_sheets_view(self):
        fh = self.zipped({"mimetype": "application/vnd.oasis.opendocument.spreadsheet",
                          "content.xml": "<x/>"})
        self.assertEqual(self.kind(fh), "")

    def test_rtf_is_recognised_from_its_first_bytes(self):
        self.assertEqual(self.kind(io.BytesIO(b"{\\rtf1\\ansi hello}")), ".rtf")

    def test_the_legacy_formats_are_told_apart_by_their_main_stream(self):
        for stream, ext in (("WordDocument", ".doc"),
                            ("PowerPoint Document", ".ppt"),
                            ("Workbook", ".xls"), ("Book", ".xls")):
            with self.subTest(stream=stream):
                self.assertEqual(self.kind(ole(["Root Entry", stream])), ext)

    def test_a_stream_name_in_the_text_does_not_decide_the_format(self):
        # A spreadsheet whose cells mention "WordDocument": the name occurs,
        # UTF-16 and terminated, but not as a directory entry.
        decoy = b"x" + "WordDocument".encode("utf-16-le") + b"\0\0" + bytes(64)
        self.assertEqual(self.kind(ole(["Root Entry", "Workbook"], decoy)), ".xls")

    def test_an_ole_file_of_another_kind_is_not_sent(self):
        # Outlook messages and MSI installers are OLE2 compound files too.
        self.assertEqual(self.kind(ole(["Root Entry", "__properties_version1.0"])), "")

    def test_the_read_position_does_not_matter(self):
        # The workbook reader asks after zipfile.is_zipfile, which leaves
        # the position at the end of the file on Pythons before 3.13.
        fh = ole(["Root Entry", "Workbook"])
        fh.seek(0, io.SEEK_END)
        self.assertEqual(self.kind(fh), ".xls")

    def test_the_name_is_not_consulted(self):
        # Not an office file at all, whatever it is called.
        self.assertEqual(self.kind(io.BytesIO(b"plain text, called deck.pptx")), "")


class DecksWithoutTheSuite(unittest.TestCase):
    """No LibreOffice: a deck has no page layout, so the worker shows its
    thumbnail instead — which happens only if the page path refuses it."""

    def setUp(self):
        self._saved = renderers.OFFICE_SUITES
        renderers.OFFICE_SUITES = ()

    def tearDown(self):
        renderers.OFFICE_SUITES = self._saved

    def refused(self, members):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        with self.assertRaises(RuntimeError):
            pages(buf.getvalue())

    def test_a_pptx_is_refused(self):
        self.refused({"ppt/presentation.xml": "<p:presentation/>"})

    def test_an_odp_is_refused_rather_than_read_as_text(self):
        self.refused({
            "mimetype": "application/vnd.oasis.opendocument.presentation",
            "content.xml": '<office:document-content xmlns:office='
                           '"urn:oasis:names:tc:opendocument:xmlns:office:1.0">'
                           '<office:body/></office:document-content>',
            "styles.xml": "<x/>",
        })


class Routing(unittest.TestCase):
    """The daemon's choice of view for each office format, with and without
    LibreOffice. Only the choice: the views are recorded, not built."""

    def setUp(self):
        import quickview

        self.qv = quickview
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.window = quickview.QuickView()
        self.addCleanup(self.window.deleteLater)
        self.chosen = []
        for view in ("show_office", "show_fallback", "show_text", "show_sheets"):
            setattr(self.window, view,
                    lambda *_a, view=view: self.chosen.append(view))
        for call in ("fit_overlay", "show", "raise_", "activateWindow"):
            setattr(self.window, call, lambda: None)

    def route(self, name, data, suite=True, engine="libreoffice"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as fh:
            fh.write(data)
        saved = (self.qv.office_suite, self.qv.OFFICE_ENGINE)
        self.qv.office_suite = lambda: "/usr/bin/soffice" if suite else ""
        self.qv.OFFICE_ENGINE = engine
        try:
            self.chosen.clear()
            self.window.show_file(path)
        finally:
            self.qv.office_suite, self.qv.OFFICE_ENGINE = saved
        return self.chosen[-1]

    def deck(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ppt/presentation.xml", "<p:presentation/>")
        return buf.getvalue()

    def test_a_deck_always_goes_to_the_office_view(self):
        # Without LibreOffice that view shows the deck's own thumbnail.
        for suite in (True, False):
            with self.subTest(suite=suite):
                self.assertEqual(self.route("deck.pptx", self.deck(), suite),
                                 "show_office")

    def test_legacy_formats_and_rtf_need_the_suite(self):
        legacy = ole(["Root Entry", "WordDocument"]).getvalue()
        for name, data, view in (("memo.doc", legacy, "show_office"),
                                 ("deck.ppt", legacy, "show_office"),
                                 # a workbook gets the grid, with its tabs
                                 ("sheet.xls", legacy, "show_sheets"),
                                 ("note.rtf", b"{\\rtf1\\ansi hello}",
                                  "show_office")):
            with self.subTest(name=name):
                self.assertEqual(self.route(name, data, suite=True), view)
                self.assertEqual(self.route(name, data, suite=False), "show_fallback")
                self.assertEqual(self.route(name, data, engine="builtin"),
                                 "show_fallback")


class WorkbookPayload(unittest.TestCase):
    """valid_workbook: what the daemon will build a table from. A cached
    workbook comes from a directory anything running as the user can write."""

    def setUp(self):
        import quickview

        self.valid = quickview.valid_workbook

    def book(self, **sheet):
        base = {"name": "S", "rows": [["a", "1"]], "cols": 2, "align": ["l", "r"],
                "clipped": False, "first_col": 0, "first_row": 1}
        base.update(sheet)
        return {"sheets": [base], "clipped": False}

    def test_a_reader_payload_passes(self):
        self.assertTrue(self.valid(self.book()))

    def test_out_of_bounds_or_misshapen_payloads_are_refused(self):
        for label, book in (
            ("not a dict", []),
            ("a billion columns", self.book(cols=10 ** 9)),
            ("too many rows", self.book(rows=[["x"]] * 100_000)),
            ("a row too wide", self.book(rows=[["x"] * 1000])),
            ("a cell that is not text", self.book(rows=[[{"x": 1}]])),
            ("a huge cell", self.book(rows=[["x" * 10 ** 6]])),
            ("a negative origin", self.book(first_row=-5)),
            ("a boolean count", self.book(cols=True)),
            ("too many sheets", {"sheets": [self.book()["sheets"][0]] * 100}),
        ):
            with self.subTest(label):
                self.assertFalse(self.valid(book))


@unittest.skipUnless(renderers.office_suite(), "LibreOffice not installed")
class RealSuite(unittest.TestCase):
    def test_a_docx_converts_to_real_pages(self):
        # With the built-in layout disabled, pages can only have come from
        # LibreOffice — a fallback must not be able to pass this.
        def refuse(*_args, **_kw):
            raise AssertionError("fell back to the built-in layout")
            yield  # pragma: no cover — makes this a generator

        saved = renderers._pages_via_qtextdocument
        renderers._pages_via_qtextdocument = refuse
        self.addCleanup(setattr, renderers, "_pages_via_qtextdocument", saved)
        doc = build_docx(para("hello " * 50) + para("x", ppr="<w:pageBreakBefore/>"))
        out = pages(doc)
        self.assertEqual(len(out), 2)
        img = QImage.fromData(out[0][1])
        self.assertFalse(img.isNull())
        self.assertEqual(img.text("QuickView:PageCount"), "2")


# Three slides, as flat ODF XML: one file LibreOffice can open and save as
# any of the deck formats, so the tests carry no binary samples.
FLAT_DECK = """<?xml version="1.0" encoding="UTF-8"?>
<office:document
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 office:version="1.3"
 office:mimetype="application/vnd.oasis.opendocument.presentation">
 <office:body><office:presentation>%s</office:presentation></office:body>
</office:document>
""" % "".join(
    '<draw:page draw:name="s%d"><draw:frame svg:x="2cm" svg:y="2cm" '
    'svg:width="20cm" svg:height="3cm"><draw:text-box><text:p>Slide %d'
    '</text:p></draw:text-box></draw:frame></draw:page>' % (n, n)
    for n in (1, 2, 3)
)


@unittest.skipUnless(renderers.office_suite(), "LibreOffice not installed")
class RealSuiteFormats(unittest.TestCase):
    """Decks, the legacy binary formats and RTF, through the real suite.

    The samples are written by LibreOffice itself from plain sources, once
    for the class. A format this LibreOffice cannot write — a Writer-only
    install has no Impress — skips its test rather than failing it.
    """

    @classmethod
    def setUpClass(cls):
        import subprocess

        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        sources = {
            "deck.fodp": FLAT_DECK,
            "memo.txt": "A memo, written as plain text.\n",
            "sheet.csv": "a,b\n1,2\n",
        }
        for name, text in sources.items():
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)
        with open(os.path.join(d, "note.rtf"), "w") as fh:
            fh.write("{\\rtf1\\ansi{\\fonttbl\\f0 Helvetica;}\\f0 A note.\\par}\n")
        for target, source in (("pptx", "deck.fodp"), ("odp", "deck.fodp"),
                               ("ppt", "deck.fodp"), ("doc", "memo.txt"),
                               ("xls", "sheet.csv")):
            subprocess.run(
                [renderers.office_suite(), "--headless", "--norestore",
                 "-env:UserInstallation=file://%s/profile" % d,
                 "--convert-to", target, "--outdir", d, os.path.join(d, source)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={"HOME": d, "PATH": "/usr/bin:/bin",
                     "SAL_USE_VCLPLUGIN": "svp"},
                timeout=60,
            )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def pages_of(self, name):
        path = os.path.join(self.tmp.name, name)
        if not os.path.exists(path):
            self.skipTest("this LibreOffice cannot write %s" % name)
        # The built-in layout has no path for any of these, so pages can
        # only have come from LibreOffice.
        with open(path, "rb") as fh:
            out = list(renderers.office_pages(fh.fileno(), name, 600, 20))
        images = [QImage.fromData(png) for _count, png in out]
        self.assertTrue(images and not any(i.isNull() for i in images), name)
        return images

    def assert_a_deck(self, name):
        slides = self.pages_of(name)
        self.assertEqual(len(slides), 3, "one page per slide")
        for slide in slides:
            self.assertGreater(slide.width(), slide.height(), "slides are landscape")

    def test_a_pptx_is_one_page_per_slide(self):
        self.assert_a_deck("deck.pptx")

    def test_an_odp_is_one_page_per_slide(self):
        self.assert_a_deck("deck.odp")

    def test_a_legacy_ppt_is_one_page_per_slide(self):
        self.assert_a_deck("deck.ppt")

    def test_a_legacy_doc_is_laid_out(self):
        page = self.pages_of("memo.doc")[0]
        self.assertGreater(page.height(), page.width())

    def test_a_legacy_xls_is_laid_out(self):
        self.pages_of("sheet.xls")

    def test_a_legacy_xls_reads_as_a_grid(self):
        path = os.path.join(self.tmp.name, "sheet.xls")
        if not os.path.exists(path):
            self.skipTest("this LibreOffice cannot write sheet.xls")
        with open(path, "rb") as fh:
            book = renderers.read_workbook(fh.fileno(), "sheet.xls")
        self.assertEqual(book["sheets"][0]["rows"], [["a", "b"], ["1", "2"]])

    def test_the_builtin_setting_keeps_a_legacy_xls_from_the_suite(self):
        path = os.path.join(self.tmp.name, "sheet.xls")
        if not os.path.exists(path):
            self.skipTest("this LibreOffice cannot write sheet.xls")
        with open(path, "rb") as fh, self.assertRaises(RuntimeError):
            renderers.read_workbook(fh.fileno(), "sheet.xls", engine="builtin")

    def test_rtf_is_laid_out(self):
        page = self.pages_of("note.rtf")[0]
        self.assertGreater(page.height(), page.width())


if __name__ == "__main__":
    unittest.main()
