#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.
"""One-shot sandboxed PDF page renderer for QuickView.

Usage: render_pdf.py <file> <page_w> <max_pages> [start]

Renders pages start (default 0) through max_pages-1 at page_w pixels wide
and streams them to stdout: first a 4-byte big-endian count of the pages
the document has under the cap, then per page a 4-byte length followed by
the PNG bytes. Every page carries the document's real page count in a
"QuickView:PageCount" tEXt chunk (it can exceed the streamed count when the
cap truncates).

Like render.py, this is the standalone form of what worker.py does for the
daemon — kept for reproducing a render by hand outside the app.
"""

import os
import struct
import sys

import renderers


def main() -> int:
    if len(sys.argv) not in (4, 5):
        print(__doc__, file=sys.stderr)
        return 2
    path, page_w, max_pages = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    start = int(sys.argv[4]) if len(sys.argv) == 5 else 0

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([sys.argv[0]])  # noqa: F841 — rendering needs it

    out = sys.stdout.buffer
    wrote_count = False
    try:
        for count, png in renderers.render_pdf(path, page_w, max_pages, start):
            if not wrote_count:
                out.write(struct.pack(">I", count))
                wrote_count = True
            out.write(struct.pack(">I", len(png)))
            out.write(png)
            out.flush()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not wrote_count:  # a zero-page document
        out.write(struct.pack(">I", 0))
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
