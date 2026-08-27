# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""The spreadsheet grid reader: cell placement, formats, and its bounds.

Every workbook here is assembled in memory, so these are tests of the
parser rather than of any file that happens to sit on disk. The hostile
cases matter most: a preview reads workbooks nobody vouched for.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renderers  # noqa: E402

SS_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R_NS = ('xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships"')
REL_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def build_xlsx(sheets, shared=(), styles_xml=None, rels=True) -> bytes:
    """sheets: [(name, sheetData xml, state)] -> the bytes of a workbook."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        entries = "".join(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"%s/>' % (
                name, i, i, ' state="hidden"' if state else "",
            )
            for i, (name, _data, state) in enumerate(sheets, 1)
        )
        zf.writestr(
            "xl/workbook.xml",
            "<workbook %s %s><sheets>%s</sheets></workbook>"
            % (SS_NS, R_NS, entries),
        )
        if rels:
            zf.writestr(
                "xl/_rels/workbook.xml.rels",
                "<Relationships %s>%s</Relationships>" % (REL_NS, "".join(
                    '<Relationship Id="rId%d" Target="worksheets/sheet%d.xml"'
                    ' Type="x"/>' % (i, i)
                    for i in range(1, len(sheets) + 1)
                )),
            )
        for i, (_name, data, _state) in enumerate(sheets, 1):
            zf.writestr(
                "xl/worksheets/sheet%d.xml" % i,
                "<worksheet %s><sheetData>%s</sheetData></worksheet>"
                % (SS_NS, data),
            )
        if shared:
            zf.writestr(
                "xl/sharedStrings.xml",
                "<sst %s>%s</sst>" % (SS_NS, "".join(
                    "<si><t>%s</t></si>" % text for text in shared
                )),
            )
        if styles_xml:
            zf.writestr("xl/styles.xml", styles_xml)
    return buf.getvalue()


def read(data: bytes) -> dict:
    """read_workbook takes a descriptor, the way the worker gets one."""
    with tempfile.TemporaryFile() as fh:
        fh.write(data)
        fh.flush()
        return renderers.read_workbook(fh.fileno(), "book.xlsx")


def styles(*fmt_ids, codes=()) -> str:
    custom = "".join(
        '<numFmt numFmtId="%s" formatCode="%s"/>' % pair for pair in codes
    )
    xfs = "".join('<xf numFmtId="%s"/>' % f for f in fmt_ids)
    return (
        "<styleSheet %s><numFmts>%s</numFmts><cellXfs>%s</cellXfs>"
        "</styleSheet>" % (SS_NS, custom, xfs)
    )


class Placement(unittest.TestCase):
    def test_sparse_row_keeps_its_columns(self):
        """A row that omits its empty cells must not slide left."""
        book = read(build_xlsx([(
            "S", '<row r="1"><c r="A1" t="s"><v>0</v></c>'
                 '<c r="D1" t="s"><v>1</v></c></row>', False,
        )], shared=("left", "right")))
        self.assertEqual(book["sheets"][0]["rows"], [["left", "", "", "right"]])

    def test_empty_margin_is_trimmed_but_numbered(self):
        book = read(build_xlsx([(
            "S", '<row r="7"><c r="C7" t="s"><v>0</v></c></row>', False,
        )], shared=("value",)))
        sheet = book["sheets"][0]
        self.assertEqual(sheet["rows"], [["value"]])
        self.assertEqual((sheet["first_row"], sheet["first_col"]), (7, 2))

    def test_sheet_names_come_from_the_workbook_part(self):
        book = read(build_xlsx([
            ("Q1 Sales", '<row r="1"><c r="A1"><v>1</v></c></row>', False),
            ("Notes", '<row r="1"><c r="A1"><v>2</v></c></row>', False),
        ]))
        self.assertEqual(
            [s["name"] for s in book["sheets"]], ["Q1 Sales", "Notes"]
        )

    def test_hidden_sheets_are_skipped(self):
        book = read(build_xlsx([
            ("Visible", '<row r="1"><c r="A1"><v>1</v></c></row>', False),
            ("Secret", '<row r="1"><c r="A1"><v>2</v></c></row>', True),
        ]))
        self.assertEqual([s["name"] for s in book["sheets"]], ["Visible"])

    def test_missing_rels_still_finds_the_worksheets(self):
        book = read(build_xlsx(
            [("S", '<row r="1"><c r="A1"><v>5</v></c></row>', False)],
            rels=False,
        ))
        self.assertEqual(book["sheets"][0]["rows"], [["5"]])


class CellValues(unittest.TestCase):
    def test_shared_string_index_out_of_range_costs_one_cell(self):
        book = read(build_xlsx([(
            "S", '<row r="1"><c r="A1" t="s"><v>99</v></c>'
                 '<c r="B1" t="s"><v>nan</v></c>'
                 '<c r="C1" t="s"><v>0</v></c></row>', False,
        )], shared=("ok",)))
        # The empty margin is trimmed away, and first_col says where the
        # surviving cell really sits.
        self.assertEqual(book["sheets"][0]["rows"], [["ok"]])
        self.assertEqual(book["sheets"][0]["first_col"], 2)

    def test_inline_strings_and_booleans(self):
        book = read(build_xlsx([(
            "S", '<row r="1"><c r="A1" t="inlineStr"><is><t>hi</t></is></c>'
                 '<c r="B1" t="b"><v>1</v></c>'
                 '<c r="C1" t="b"><v>0</v></c></row>', False,
        )]))
        self.assertEqual(book["sheets"][0]["rows"], [["hi", "TRUE", "FALSE"]])

    def test_float_noise_is_not_shown(self):
        book = read(build_xlsx([(
            "S", '<row r="1"><c r="A1"><v>15.700000000000001</v></c>'
                 '<c r="B1"><v>14.0</v></c></row>', False,
        )]))
        self.assertEqual(book["sheets"][0]["rows"], [["15.7", "14"]])

    def test_date_styles_turn_serials_into_dates(self):
        book = read(build_xlsx(
            [("S", '<row r="1"><c r="A1" s="1"><v>46039</v></c>'
                   '<c r="B1" s="2"><v>0.223</v></c>'
                   '<c r="C1" s="0"><v>46039</v></c></row>', False)],
            styles_xml=styles("0", "14", "10"),
        ))
        self.assertEqual(
            book["sheets"][0]["rows"], [["2026-01-17", "22.3%", "46039"]]
        )

    def test_custom_date_format_code_is_recognised(self):
        book = read(build_xlsx(
            [("S", '<row r="1"><c r="A1" s="1"><v>46039.5</v></c></row>',
              False)],
            styles_xml=styles("0", "165", codes=(("165", "yyyy\\-mm\\-dd hh:mm"),)),
        ))
        self.assertEqual(book["sheets"][0]["rows"], [["2026-01-17 12:00"]])

    def test_a_quoted_literal_is_not_a_date_code(self):
        # "Days" in quotes is a label, not a d/y format — the number stays.
        self.assertEqual(renderers._classify_format('0" Days"'), "")
        self.assertEqual(renderers._classify_format("yyyy-mm-dd"), "date")


class Bounds(unittest.TestCase):
    """A preview must cost the same for a huge workbook as for a small one."""

    def test_rows_and_columns_are_capped(self):
        wide = "".join(
            '<c r="%s1"><v>%d</v></c>' % (_letters(c), c)
            for c in range(renderers.SHEET_MAX_COLS + 20)
        )
        tall = "".join(
            '<row r="%d"><c r="A%d"><v>%d</v></c></row>' % (r, r, r)
            for r in range(2, renderers.SHEET_MAX_ROWS + 200)
        )
        book = read(build_xlsx([("S", '<row r="1">%s</row>%s' % (wide, tall),
                                False)]))
        sheet = book["sheets"][0]
        self.assertEqual(sheet["cols"], renderers.SHEET_MAX_COLS)
        self.assertEqual(len(sheet["rows"]), renderers.SHEET_MAX_ROWS)
        self.assertTrue(sheet["clipped"])

    def test_one_enormous_cell_is_cut(self):
        book = read(build_xlsx([(
            "S", '<row r="1"><c r="A1" t="inlineStr"><is><t>%s</t></is></c>'
                 '</row>' % ("x" * 5000), False,
        )]))
        self.assertEqual(
            len(book["sheets"][0]["rows"][0][0]), renderers.SHEET_MAX_CELL_CHARS
        )

    def test_a_workbook_with_no_cells_is_refused(self):
        # Nothing to show is reported as such, so the daemon can fall back
        # to the office view rather than drawing an empty grid.
        with self.assertRaises(RuntimeError):
            read(build_xlsx([("S", "", False)]))

    def test_a_container_that_is_not_a_spreadsheet_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<document/>")
        with self.assertRaises(RuntimeError):
            read(buf.getvalue())

    def test_plain_bytes_are_refused(self):
        with self.assertRaises(RuntimeError):
            read(b"not a zip at all")


class Alignment(unittest.TestCase):
    def test_a_column_of_numbers_aligns_right(self):
        book = read(build_xlsx([(
            "S", '<row r="1"><c r="A1" t="s"><v>0</v></c>'
                 '<c r="B1" t="s"><v>1</v></c></row>'
                 '<row r="2"><c r="A2" t="s"><v>2</v></c>'
                 '<c r="B2"><v>12</v></c></row>'
                 '<row r="3"><c r="A3" t="s"><v>2</v></c>'
                 '<c r="B3"><v>34</v></c></row>', False,
        )], shared=("Region", "Revenue", "North")))
        self.assertEqual(book["sheets"][0]["align"], ["l", "r"])


class Ods(unittest.TestCase):
    def test_repeated_cells_expand_and_rows_count_from_one(self):
        content = (
            '<office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            '<table:table table:name="Sheet A">'
            '<table:table-row><table:table-cell table:number-columns-repeated'
            '="2"><text:p>x</text:p></table:table-cell>'
            '<table:table-cell><text:p>y</text:p></table:table-cell>'
            '</table:table-row>'
            '<table:table-row table:number-rows-repeated="500">'
            '<table:table-cell/></table:table-row>'
            '</table:table></office:document-content>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("content.xml", content)
        sheet = read(buf.getvalue())["sheets"][0]
        self.assertEqual(sheet["name"], "Sheet A")
        self.assertEqual(sheet["rows"], [["x", "x", "y"]])
        self.assertEqual(sheet["first_row"], 1)


def _letters(index: int) -> str:
    out = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        out = chr(65 + rest) + out
    return out


class ColumnLetters(unittest.TestCase):
    def test_reference_letters_round_trip(self):
        for index in (0, 25, 26, 51, 52, 701, 702):
            self.assertEqual(renderers._col_index(_letters(index)), index)


if __name__ == "__main__":
    unittest.main()
