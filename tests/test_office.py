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
from PySide6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402

_app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])

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


if __name__ == "__main__":
    unittest.main()
