#!/usr/bin/env python3
"""Sandboxed image decoder for QuickView.

Usage: render.py <file> <max_w> <max_h>

Decodes one image and writes it to stdout as PNG, downscaled to fit
max_w x max_h. The original dimensions travel along in a
"QuickView:OrigSize" tEXt chunk. quickview.py runs this under bubblewrap
(read-only /usr + this app + the one target file, no network, no writes),
so a malicious file can only take down this throwaway process, never the
daemon — see render_image() there.
"""

import os
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    path, max_w, max_h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImageReader

    app = QGuiApplication([sys.argv[0]])  # noqa: F841 — QImageReader needs it

    reader = QImageReader(path)
    reader.setAutoTransform(True)
    img = reader.read()
    if img.isNull():
        print(f"unsupported or corrupt: {reader.errorString()}", file=sys.stderr)
        return 1

    orig = f"{img.width()}×{img.height()}"
    if img.width() > max_w or img.height() > max_h:
        img = img.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    img.setText("QuickView:OrigSize", orig)

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        print("PNG encode failed", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(bytes(buf.data()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
