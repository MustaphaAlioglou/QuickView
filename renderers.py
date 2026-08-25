#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.
"""Decoders for untrusted files, shared by the jailed worker and the CLI
helpers.

Nothing in here may run in the daemon: every function takes a QIODevice (an
fd the daemon passed over SCM_RIGHTS) or a path and hands it to a Qt parser.
worker.py calls these inside bubblewrap; render.py / render_pdf.py are the
standalone one-shot equivalents kept for debugging and for the no-worker
fallback path.
"""

# PNG quality 80 ≈ zlib level 2: encodes in roughly half the time of the
# default for ~20% larger files — the right trade for a preview a human is
# waiting on. Every encode below uses it.
PNG_QUALITY = 80

# A PDF may declare any page geometry it likes, and the height is derived
# from the requested width: a 1 pt x 10000 pt page asks for a ten-million
# pixel column, i.e. tens of GB of QImage. The jail has no memory limit of
# its own, so the bound has to be here.
PDF_MAX_PAGE_PX = 10000


def _encode(img) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG", PNG_QUALITY):
        raise RuntimeError("PNG encode failed")
    return bytes(buf.data())


def render_image(source, max_w: int, max_h: int) -> bytes:
    """Decode one image, downscaled to fit max_w x max_h, as PNG bytes.

    The original dimensions travel along in a "QuickView:OrigSize" tEXt
    chunk. `source` is a path or a QIODevice.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImageIOHandler, QImageReader

    reader = QImageReader(source)
    reader.setAutoTransform(True)

    # Ask the decoder for a preview-sized image up front instead of decoding
    # full-size and downscaling after: for JPEG this engages libjpeg's DCT
    # scaling and cuts decode time to a fraction. size() only parses the
    # header; it is invalid for formats whose handler can't report it, and
    # those fall back to the decode-then-scale path below.
    orig = None
    size = reader.size()
    if size.isValid():
        # EXIF rotations of 90°/270° swap the displayed axes; fit the box in
        # display orientation, then hand the decoder its pre-rotation size.
        rotated = bool(
            reader.transformation()
            & QImageIOHandler.Transformation.TransformationRotate90
        )
        shown = size.transposed() if rotated else size
        orig = f"{shown.width()}×{shown.height()}"
        if shown.width() > max_w or shown.height() > max_h:
            fit = shown.scaled(max_w, max_h, Qt.KeepAspectRatio)
            reader.setScaledSize(fit.transposed() if rotated else fit)

    img = reader.read()
    if img.isNull():
        raise RuntimeError(f"unsupported or corrupt: {reader.errorString()}")

    if orig is None:
        orig = f"{img.width()}×{img.height()}"
    if img.width() > max_w or img.height() > max_h:
        img = img.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    img.setText("QuickView:OrigSize", orig)
    return _encode(img)


def render_pdf(source, page_w: int, max_pages: int, start: int = 0):
    """Yield (page_count, png_bytes) for pages start..max_pages-1.

    Every yielded page carries the document's real page count in a
    "QuickView:PageCount" tEXt chunk — it can exceed the number of pages
    streamed when the cap truncates. Pages are yielded one at a time so the
    caller can show page 1 before the rest exist. start skips pages the
    caller already has (a partially cached document resumes there instead
    of re-rendering the prefix).
    """
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    # The path overload returns an Error; the QIODevice one returns None and
    # reports through error()/status() instead. Ask the document either way.
    err = doc.load(source)
    if err is None:
        err = doc.error()
    if err != QPdfDocument.Error.None_:
        raise RuntimeError(f"pdf load failed: {err}")

    total = doc.pageCount()
    count = min(total, max_pages)
    for i in range(max(start, 0), count):
        pt = doc.pagePointSize(i)
        w = max(min(page_w, PDF_MAX_PAGE_PX), 1)
        h = max(1, round(w * pt.height() / max(pt.width(), 1)))
        if h > PDF_MAX_PAGE_PX:
            # Absurdly tall page: keep the aspect ratio and let it be
            # narrow rather than allocating by its declared height.
            h = PDF_MAX_PAGE_PX
            w = max(1, round(h * pt.width() / max(pt.height(), 1)))
        rendered = doc.render(i, QSize(w, h))
        if rendered.isNull():
            raise RuntimeError(f"render failed on page {i}")
        # Qt renders onto transparency: a PDF paints its glyphs but almost
        # never its own page background, so the raw image is black text on
        # nothing. Composite it over white to get the sheet of paper the
        # reader expects (and drop the alpha channel while we're at it).
        img = QImage(rendered.size(), QImage.Format.Format_RGB32)
        img.fill(Qt.GlobalColor.white)
        painter = QPainter(img)
        painter.drawImage(0, 0, rendered)
        painter.end()
        img.setText("QuickView:PageCount", str(total))
        yield count, _encode(img)


def render_anim(source, max_w: int, max_h: int, max_frames: int, max_bytes: int):
    """Yield (frame_count, delay_ms, png_bytes) for an animation.

    Decoding every frame here is what lets the daemon animate a GIF without
    ever running QMovie — it just cycles pixmaps on a timer. Stops early at
    max_frames or once max_bytes of decoded RGB has been produced, so a
    header claiming 100k frames costs bounded memory; the caller falls back
    to a still image when nothing decodes.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QMovie

    movie = QMovie(source)
    if not movie.isValid():
        raise RuntimeError("not a valid animation")
    movie.setCacheMode(QMovie.CacheMode.CacheNone)
    # frameCount() is 0 for formats that don't declare one; treat that as
    # "stream until jumpToNextFrame fails".
    declared = movie.frameCount()
    count = min(declared, max_frames) if declared > 0 else max_frames

    movie.jumpToFrame(0)
    spent = 0
    for i in range(count):
        img = movie.currentImage()
        if img.isNull():
            break
        delay = max(movie.nextFrameDelay(), 10)
        if img.width() > max_w or img.height() > max_h:
            img = img.scaled(
                max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        if i == 0:
            img.setText("QuickView:OrigSize", f"{img.width()}×{img.height()}")
        spent += img.width() * img.height() * 4
        yield count, delay, _encode(img)
        if spent >= max_bytes or not movie.jumpToNextFrame():
            break
