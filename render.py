#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.
"""One-shot sandboxed image decoder for QuickView.

Usage: render.py <file> <max_w> <max_h>

Decodes one image and writes it to stdout as PNG, downscaled to fit
max_w x max_h. The original dimensions travel along in a
"QuickView:OrigSize" tEXt chunk.

The daemon no longer uses this: it talks to a warm worker.py inside the
same jail instead, which avoids paying Qt's import cost per file. This
script is the standalone equivalent — handy for reproducing a render by
hand, and for checking what the sandbox sees:

    bwrap ... -- ./render.py suspicious.jpg 800 600 > out.png
"""

import os
import sys

import renderers


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    path, max_w, max_h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([sys.argv[0]])  # noqa: F841 — the decoder needs it

    try:
        png = renderers.render_image(path, max_w, max_h)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    sys.stdout.buffer.write(png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
