# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""A small PDF with bookmarks and known text, written out by hand.

Qt can write a PDF but not its outline, and the outline and search checks
need both — so the file is built here from raw PDF syntax instead of being
checked in or borrowed from someone's documents. Everything the tests
assert about it is derived from the constants below, so changing the text
cannot leave a count in a test silently wrong.

Not a test module: the name keeps it out of discovery.
"""

PAGE_W, PAGE_H = 612, 792          # US Letter, in points
FONT_SIZE, LEADING = 12, 16
TOP = 720                          # baseline of a page's first line

WORD = "quokka"                    # the single-word search target

# A phrase that reads as continuous but is broken over two lines, so the
# text layer has a line break inside it.
PHRASE = "degree of Master"
_BREAK = ("She was awarded the degree of", "Master of quokka studies in the spring.")

_PROSE = (
    "The quokka is a small marsupial found on the islands off the west.",
    "It is the size of a domestic cat and feeds on the leaves of shrubs.",
    "Visitors to the island photograph the animal for its apparent smile.",
    "In the dry season the quokka can survive for a month without water.",
)


def _pages():
    """Each page as a list of text lines."""
    pages = [[] for _ in range(9)]
    pages[0] += ["Chapter 1 Introduction", *_PROSE]
    # Three subsections on one page: an outline with page numbers alone
    # would scroll all three to the same place.
    for n in (1, 2, 3):
        pages[1] += [f"1.{n} Section {n}", *_PROSE[:2]]
    pages[2] += [*_PROSE[:2], *_BREAK, *_PROSE[2:]]
    for i in range(3, 9):
        pages[i] += [f"Page {i + 1}.", *_PROSE]
    pages[4][0:0] = ["Chapter 2 Habitat"]
    pages[4] += ["2.1 Islands", *_PROSE[:1]]
    pages[7][0:0] = ["Chapter 3 Diet"]
    return pages


PAGES = _pages()


def _line_index(page, text):
    return PAGES[page].index(text)


# (title, level, page, line the destination points at)
OUTLINE = (
    ("Chapter 1 Introduction", 0, 0, _line_index(0, "Chapter 1 Introduction")),
    ("1.1 Section 1", 1, 1, _line_index(1, "1.1 Section 1")),
    ("1.2 Section 2", 1, 1, _line_index(1, "1.2 Section 2")),
    ("1.3 Section 3", 1, 1, _line_index(1, "1.3 Section 3")),
    ("Chapter 2 Habitat", 0, 4, _line_index(4, "Chapter 2 Habitat")),
    ("2.1 Islands", 1, 4, _line_index(4, "2.1 Islands")),
    ("Chapter 3 Diet", 0, 7, _line_index(7, "Chapter 3 Diet")),
)


def count(word):
    """Case-insensitive occurrences of word, the way the search counts."""
    word = word.casefold()
    total = 0
    for lines in PAGES:
        text = " ".join(lines).casefold()
        at = text.find(word)
        while at != -1:
            total += 1
            at = text.find(word, at + 1)
    return total


def _text(s):
    return "(" + s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def build():
    """The PDF as bytes."""
    objs = {}                      # number -> body
    n_pages = len(PAGES)
    catalog, pages_obj, font, outlines = 1, 2, 3, 4
    page_nums = [5 + 2 * i for i in range(n_pages)]
    item_base = 5 + 2 * n_pages

    objs[font] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica " \
                 "/Encoding /WinAnsiEncoding >>"
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    objs[pages_obj] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>"

    for i, lines in enumerate(PAGES):
        ops = [f"BT /F1 {FONT_SIZE} Tf {LEADING} TL 72 {TOP} Td"]
        for j, line in enumerate(lines):
            ops.append(("" if j == 0 else "T* ") + _text(line) + " Tj")
        ops.append("ET")
        stream = "\n".join(ops)
        objs[page_nums[i] + 1] = (f"<< /Length {len(stream.encode('latin-1'))} >>\n"
                                  f"stream\n{stream}\nendstream")
        objs[page_nums[i]] = (
            f"<< /Type /Page /Parent {pages_obj} 0 R "
            f"/MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> "
            f"/Contents {page_nums[i] + 1} 0 R >>"
        )

    # The outline: a list of top-level items, each with its children.
    tree = []
    for k, (title, level, page, line) in enumerate(OUTLINE):
        node = {"num": item_base + k, "title": title, "page": page,
                "line": line, "kids": []}
        (tree if level == 0 else tree[-1]["kids"]).append(node)

    def link(siblings, parent):
        for k, node in enumerate(siblings):
            # /XYZ's top is the line's ascent, a font size above its baseline.
            top = TOP - node["line"] * LEADING + FONT_SIZE
            parts = [f"/Title {_text(node['title'])}", f"/Parent {parent} 0 R",
                     f"/Dest [{page_nums[node['page']]} 0 R /XYZ 72 {top} 0]"]
            if k:
                parts.append(f"/Prev {siblings[k - 1]['num']} 0 R")
            if k + 1 < len(siblings):
                parts.append(f"/Next {siblings[k + 1]['num']} 0 R")
            if node["kids"]:
                parts += [f"/First {node['kids'][0]['num']} 0 R",
                          f"/Last {node['kids'][-1]['num']} 0 R",
                          f"/Count {len(node['kids'])}"]
                link(node["kids"], node["num"])
            objs[node["num"]] = "<< " + " ".join(parts) + " >>"

    link(tree, outlines)
    objs[outlines] = (f"<< /Type /Outlines /First {tree[0]['num']} 0 R "
                      f"/Last {tree[-1]['num']} 0 R /Count {len(OUTLINE)} >>")
    objs[catalog] = (f"<< /Type /Catalog /Pages {pages_obj} 0 R "
                     f"/Outlines {outlines} 0 R /PageMode /UseOutlines >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objs[num]}\nendobj\n".encode("latin-1")
    xref = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {size} /Root {catalog} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def write(path):
    with open(path, "wb") as f:
        f.write(build())
    return path
