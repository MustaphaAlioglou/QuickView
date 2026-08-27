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
# Cache-only, and only for images with no alpha — see encode_cached.
JPEG_QUALITY = 85

# Above this, a file is shown as plain text. Pygments is a regex machine and
# the cost climbs with size: measured here, 83 KiB of Python lexes in 109 ms,
# but 1 MiB takes 1.5 s and yields a quarter of a million tokens — slower
# than the preview it is meant to decorate, and a payload bigger than the
# file itself.
HIGHLIGHT_MAX_BYTES = 256 * 1024

# An archive listing is a preview, not a file manager: enough entries to see
# what is in there, and a count for the rest.
ARCHIVE_MAX_ENTRIES = 500
# Nothing here decompresses an entry, so a zip bomb is inert — except in the
# office path, which does read one XML member. That read is bounded by this.
OFFICE_MAX_MEMBER_BYTES = 16 * 1024 * 1024
OFFICE_MAX_THUMB_BYTES = 8 * 1024 * 1024
# A rendered page, in pixels. Width comes from the caller (the panel width);
# the ratio is A4's, which is what these documents are written for.
PAGE_RATIO = 297 / 210
# Archive helpers, by absolute path: the jail runs --clearenv, so there is no
# PATH to search, and the user's own PATH may point at a conda build that
# does not exist inside the jail.
ARCHIVE_TOOLS = (
    ("/usr/bin/bsdtar", ["-tf"]),
    ("/usr/bin/7z", ["l", "-ba", "-slt"]),
    ("/usr/bin/unrar", ["lb"]),
)

# A PDF may declare any page geometry it likes, and the height is derived
# from the requested width: a 1 pt x 10000 pt page asks for a ten-million
# pixel column, i.e. tens of GB of QImage. The jail has no memory limit of
# its own, so the bound has to be here.
PDF_MAX_PAGE_PX = 10000
# A search that matches half the document is not a useful answer, and every
# hit costs a getSelectionAtIndex call and a rectangle on the wire.
PDF_MAX_HITS = 500
# Outline bounds. A generated PDF can carry a bookmark per paragraph, and a
# sidebar with ten thousand rows in it is not a table of contents.
PDF_MAX_OUTLINE = 2000
OUTLINE_MAX_TITLE = 200


def _encode(img, fmt: str = "PNG", quality: int = PNG_QUALITY) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, fmt, quality):
        raise RuntimeError(f"{fmt} encode failed")
    return bytes(buf.data())


def encode_cached(img) -> bytes:
    """Encode a preview for the daemon's disk cache.

    JPEG for photographs: a preview-sized image costs ~24 ms to write and
    ~11 ms to read back against PNG's ~111 and ~37, and lands in a tenth of
    the space, so the fixed cache holds ten times as many previews before
    it starts evicting. The loss at this quality is invisible at preview
    size, and this is a cache — the original is never touched.

    PNG whenever the image really is transparent somewhere, because JPEG
    has no alpha and a transparent logo would come back on a black square.
    Qt writes the "QuickView:OrigSize" tEXt chunk into a JPEG comment
    marker, so the dimensions the cache-hit path reads survive either way.
    """
    if img.hasAlphaChannel() and not _is_opaque(img):
        return _encode(img)
    return _encode(img, "JPG", JPEG_QUALITY)


def _is_opaque(img) -> bool:
    """Whether every pixel is fully opaque.

    hasAlphaChannel() answers about the *format*, not the pixels, and a
    great many opaque images decode to ARGB32 anyway — screenshots and most
    PNGs among them. Trusting it alone cached those as multi-megabyte PNGs
    when a 200 KB JPEG would do. The scan is ~2 ms on a preview-sized image
    against the ~80 ms PNG encode it avoids, and it runs in the worker
    after the picture is already on screen.
    """
    from PySide6.QtGui import QImage

    if img.format() != QImage.Format.Format_ARGB32:
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = img.width(), img.height()
    row_len = width * 4
    stride = img.bytesPerLine()
    data = bytes(img.constBits())
    if stride == row_len:  # no row padding: one slice does the whole image
        return data[3::4].count(0xFF) == width * height
    # Qt pads rows to a 4-byte boundary; the padding is not pixel data and
    # must not be counted, so walk row by row.
    for y in range(height):
        start = y * stride
        if data[start:start + row_len][3::4].count(0xFF) != width:
            return False
    return True


def raw_frame(img) -> tuple:
    """(pixel bytes, geometry) for handing an image over a local socket.

    PNG costs ~100 ms to write and ~30 ms to read back for a preview-sized
    photo — more than the decode it follows — and buys nothing between two
    processes on the same machine. Raw ARGB32 is ~0.4 ms each way. BMP is
    the obvious middle ground and is wrong: Qt's BMP writer drops the alpha
    channel, so transparent PNGs and SVGs would come back opaque.

    The geometry travels beside the bytes because the receiver cannot infer
    stride: Qt pads rows, so bytesPerLine is not always width * 4.
    """
    from PySide6.QtGui import QImage

    if img.format() != QImage.Format.Format_ARGB32:
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
    return bytes(img.constBits()), {
        "w": img.width(), "h": img.height(),
        "stride": img.bytesPerLine(), "fmt": "argb32",
    }


def decode_image(source, max_w: int, max_h: int) -> tuple:
    """(QImage, "W×H") — the decode alone, without choosing a wire format.

    Split out of render_image so a caller that wants both the raw pixels
    and an encoded copy (the worker: pixels to show now, a cache entry to
    keep) pays for one decode instead of two.
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
    return img, orig


def render_image(source, max_w: int, max_h: int) -> bytes:
    """Decode one image, downscaled to fit max_w x max_h, as PNG bytes.

    The original dimensions travel along in a "QuickView:OrigSize" tEXt
    chunk. `source` is a path or a QIODevice.
    """
    img, orig = decode_image(source, max_w, max_h)
    img.setText("QuickView:OrigSize", orig)
    return _encode(img)


def _open_pdf(source):
    """Load a PDF, raising rather than returning a half-built document."""
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    # The path overload returns an Error; the QIODevice one returns None and
    # reports through error()/status() instead. Ask the document either way.
    err = doc.load(source)
    if err is None:
        err = doc.error()
    if err != QPdfDocument.Error.None_:
        raise RuntimeError(f"pdf load failed: {err}")
    return doc


def _page_px(doc, i: int, page_w: int) -> tuple:
    """(width, height) in pixels for page i rendered at page_w.

    Shared by render_pdf and search_pdf on purpose. Search maps a match's
    bounding rectangle from points into this pixel space, so the two must
    agree exactly — if they drift, every highlight lands in the wrong place,
    and only on the page shapes that hit the clamps below.
    """
    pt = doc.pagePointSize(i)
    w = max(min(page_w, PDF_MAX_PAGE_PX), 1)
    h = max(1, round(w * pt.height() / max(pt.width(), 1)))
    if h > PDF_MAX_PAGE_PX:
        # Absurdly tall page: keep the aspect ratio and let it be narrow
        # rather than allocating by its declared height.
        h = PDF_MAX_PAGE_PX
        w = max(1, round(h * pt.width() / max(pt.height(), 1)))
    return w, h


def _flatten(text: str, drop_spaces: bool = False) -> tuple:
    """Casefold and normalize whitespace, keeping a map back to the original.

    A PDF's text layer breaks lines with \r\n, so a phrase the reader sees
    as continuous ("degree of Master") is not continuous in the string.
    Collapsing every run of whitespace to one space makes the phrase
    findable; the returned list maps each flattened index back to its index
    in `text`, which is what getSelectionAtIndex needs to place the match.

    drop_spaces removes whitespace altogether, for the fallback described in
    search_pdf: some PDFs position their glyphs instead of emitting spaces,
    and Qt's extractor hands those back as "accordingtoyoursoftskills".
    """
    out, back, space = [], [], False
    for i, ch in enumerate(text):
        if ch.isspace():
            if drop_spaces:
                continue
            if out and not space:  # one space per run, never a leading one
                out.append(" ")
                back.append(i)
                space = True
            continue
        space = False
        low = ch.casefold()
        # casefold can expand (ß -> ss); every produced char points at the
        # single source character it came from, so the map stays aligned.
        out.append(low)
        back.extend([i] * len(low))
    while out and out[-1] == " ":
        out.pop()
        back.pop()
    return "".join(out), back


def search_pdf(source, query: str, page_w: int, max_pages: int,
               max_hits: int = PDF_MAX_HITS) -> dict:
    """Case-insensitive substring search, as rectangles in page pixels.

    Returns {"matches": [{"page": n, "rects": [[x, y, w, h], ...]}, ...],
    "capped": bool} — one entry per match, and one rectangle per line it
    spans. The
    daemon holds no PDF parser, so the text extraction and the geometry both
    happen in here and it receives something it can paint directly.

    Only the pages the viewer actually shows are searched: a hit reported on
    page 73 of a document that stops rendering at 50 is worse than silence.
    """
    if not _flatten(query)[0]:
        return {"matches": [], "capped": False}

    doc = _open_pdf(source)
    pages = [doc.getAllText(i).text() for i in range(min(doc.pageCount(),
                                                        max_pages))]
    out = _scan(doc, pages, query, page_w, max_hits, drop_spaces=False)
    # Nothing found and the query has a space in it? Try again ignoring
    # spaces entirely. Qt's getAllText does not always reconstruct the gaps
    # between glyphs, so a page that reads "soft skills" can extract as
    # "softskills" and no amount of whitespace *collapsing* will match it.
    # Only as a fallback, and only for multi-word queries: matching without
    # spaces would otherwise let "the rap" hit "therapy".
    if not out["matches"] and any(c.isspace() for c in query.strip()):
        loose = _scan(doc, pages, query, page_w, max_hits, drop_spaces=True)
        if loose["matches"]:
            loose["loose"] = True
            return loose
    return out


def _scan(doc, pages: list, query: str, page_w: int, max_hits: int,
          drop_spaces: bool) -> dict:
    """One search pass over already-extracted page text."""
    needle = _flatten(query, drop_spaces)[0]
    matches = []
    if not needle:
        return {"matches": [], "capped": False, "loose": False}
    for i, page_text in enumerate(pages):
        hay, back = _flatten(page_text, drop_spaces)
        if not hay:
            continue
        w_px, h_px = _page_px(doc, i, page_w)
        pt = doc.pagePointSize(i)
        sx = w_px / max(pt.width(), 1)
        sy = h_px / max(pt.height(), 1)
        at = hay.find(needle)
        while at != -1:
            # back[] maps flattened offsets to real ones: a phrase broken
            # over a line is contiguous in `hay` but not in the page text.
            start = back[at]
            end = back[at + len(needle) - 1]
            sel = doc.getSelectionAtIndex(i, start, end - start + 1)
            if sel.isValid():
                # bounds(), not boundingRectangle(): a match crossing a line
                # gets one polygon per line, where the single bounding box
                # would be a slab covering both lines and the gap between.
                rects = []
                for poly in sel.bounds():
                    r = poly.boundingRect()
                    rects.append([
                        round(r.x() * sx), round(r.y() * sy),
                        max(1, round(r.width() * sx)),
                        max(1, round(r.height() * sy)),
                    ])
                if rects:
                    matches.append({"page": i, "rects": rects})
                    if len(matches) >= max_hits:
                        return {"matches": matches, "capped": True,
                                "loose": False}
            at = hay.find(needle, at + 1)
    return {"matches": matches, "capped": False, "loose": False}


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

    doc = _open_pdf(source)
    total = doc.pageCount()
    count = min(total, max_pages)
    for i in range(max(start, 0), count):
        w, h = _page_px(doc, i, page_w)
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


def pdf_outline(source, page_w: int, max_pages: int,
                max_entries: int = PDF_MAX_OUTLINE) -> list:
    """The document's bookmarks, flattened: [{title, level, page, y}, ...].

    Qt builds the outline as a tree model, which is walked here rather than
    in the daemon: the daemon owns no PDF parser, and shipping a model over
    a socket is not a thing. Entries past the last rendered page are
    dropped — an outline row that cannot scroll anywhere is a dead link.

    y is where on its page the destination sits, in the pixels the page will
    be rendered at. A section is not a page: five subsections of one chapter
    share page 7 of the sample this was built against, and without y all
    five scroll to the same place — which, on a page taller than the window,
    looks like the sidebar doing nothing at all.
    """
    from PySide6.QtCore import QModelIndex
    from PySide6.QtPdf import QPdfBookmarkModel

    doc = _open_pdf(source)
    model = QPdfBookmarkModel()
    model.setDocument(doc)
    role = QPdfBookmarkModel.Role
    limit = min(doc.pageCount(), max_pages)
    out = []

    def walk(parent, level: int):
        for row in range(model.rowCount(parent)):
            if len(out) >= max_entries:
                return
            index = model.index(row, 0, parent)
            title = (model.data(index, role.Title.value) or "").strip()
            try:
                page = int(model.data(index, role.Page.value) or 0)
            except (TypeError, ValueError):
                page = 0
            if title and 0 <= page < limit:
                out.append({
                    "title": title[:OUTLINE_MAX_TITLE],
                    "level": min(level, 5),
                    "page": page,
                    "y": _outline_y(doc, page, page_w,
                                    model.data(index, role.Location.value)),
                })
            walk(index, level + 1)

    walk(QModelIndex(), 0)
    return out


def _outline_y(doc, page: int, page_w: int, location) -> int:
    """A bookmark's destination as an offset down the rendered page.

    The model gives it in points from the top of the page; the daemon
    scrolls in the pixels the page was rendered at.
    """
    if location is None:
        return 0
    _w, h_px = _page_px(doc, page, page_w)
    height_pt = doc.pagePointSize(page).height()
    if height_pt <= 0:
        return 0
    try:
        y_pt = float(location.y())
    except (AttributeError, TypeError, ValueError):
        return 0
    return max(0, min(h_px - 1, round(y_pt * h_px / height_pt)))


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


def highlight_text(source, name: str, limit: int, style_name: str = "one-dark") -> dict:
    """Read a text file and return it with colour spans for its syntax.

    Returns {"text", "truncated", "spans", "styles", "lexer"}. `spans` is a
    list of [start, length, style_index] over the *characters* of "text";
    only tokens that differ from the default colour are listed, and adjacent
    runs sharing a colour are merged, which is what keeps the result far
    smaller than the file.

    This runs in the jail on purpose. Pygments lexers are regexes, and a file
    crafted to make one backtrack would otherwise wedge the daemon that owns
    the window and the IPC socket; in here the worker's watchdog kills it and
    the daemon shows the file unhighlighted.
    """
    data = source.read(limit + 1) if hasattr(source, "read") else open(source, "rb").read(limit + 1)
    data = bytes(data)
    truncated = len(data) > limit
    text = data[:limit].decode("utf-8", errors="replace")
    # Normalised here, before anything measures an offset: the daemon's
    # QTextDocument stores "\n" line ends, so lexing "\r\n" text would put
    # every span one character further off with each line.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    out = {"text": text, "truncated": truncated, "spans": [], "styles": [],
           "lexer": None}
    if len(text) > HIGHLIGHT_MAX_BYTES:
        return out
    try:
        from pygments import lex
        from pygments.lexers import get_lexer_for_filename
        from pygments.styles import get_style_by_name
        from pygments.token import Token
        from pygments.util import ClassNotFound
    except ImportError:
        return out  # highlighting is optional; plain text is a fine preview

    try:
        # By filename only. guess_lexer() runs every lexer's analyse_text()
        # over the content, which is both slow and a lot more regex surface.
        # ensurenl=False keeps the text identical to what the daemon shows,
        # so the offsets below stay valid.
        lexer = get_lexer_for_filename(name, ensurenl=False, stripnl=False)
        style = get_style_by_name(style_name)
    except ClassNotFound:
        return out  # unknown extension, or a style name that does not exist

    default = style.style_for_token(Token.Text).get("color")
    palette, styles, spans = {}, [], []
    # style_for_token() walks the token hierarchy on every call, which costs
    # more than the lexing does; there are only a handful of distinct token
    # types in a file, so ask once each.
    colour_of = {}
    pos = 0
    for ttype, value in lex(text, lexer):
        length = len(value)
        colour = colour_of.get(ttype)
        if colour is None:
            colour = colour_of[ttype] = style.style_for_token(ttype).get("color") or ""
        if colour and colour != default and value.strip():
            index = palette.get(colour)
            if index is None:
                index = palette[colour] = len(styles)
                styles.append("#" + colour)
            if spans and spans[-1][2] == index and spans[-1][0] + spans[-1][1] == pos:
                spans[-1][1] += length  # one run, not two
            else:
                spans.append([pos, length, index])
        pos += length

    out["spans"], out["styles"], out["lexer"] = spans, styles, lexer.name
    return out


def _rewound(fd: int):
    """A private, rewound file object over an inherited descriptor."""
    import os

    os.lseek(fd, 0, os.SEEK_SET)
    return os.fdopen(os.dup(fd), "rb")


def list_archive(fd: int, name: str, max_entries: int = ARCHIVE_MAX_ENTRIES) -> dict:
    """List an archive's entries without extracting anything.

    Reads headers only — no member is ever decompressed — so an archive that
    expands to terabytes costs nothing here. zip and tar are handled by the
    standard library; everything else (rar, 7z, iso) goes to whichever of
    bsdtar/7z/unrar exists, reading the archive through /dev/fd rather than a
    path, because the jail has the descriptor and no filesystem.
    """
    import os
    import subprocess
    import tarfile
    import zipfile

    # count_exact says whether "count" and "total" describe the whole
    # archive. The tar branch stops reading early on a huge member list, and
    # a floor reported as a total is worse than an honest "500+".
    out = {"entries": [], "count": 0, "total": 0, "truncated": False,
           "count_exact": True, "tool": None}

    with _rewound(fd) as fh:
        if zipfile.is_zipfile(fh):
            fh.seek(0)
            with zipfile.ZipFile(fh) as zf:
                infos = zf.infolist()
                out["tool"] = "zipfile"
                out["count"] = len(infos)
                out["total"] = sum(i.file_size for i in infos)
                out["entries"] = [
                    [i.filename, i.file_size] for i in infos[:max_entries]
                ]
                out["truncated"] = len(infos) > max_entries
                return out
        fh.seek(0)
        try:
            with tarfile.open(fileobj=fh, mode="r:*") as tf:
                members = []
                capped = False
                for member in tf:  # streamed: no full scan up front
                    if len(members) >= max_entries * 4:
                        capped = True  # stop before a tar with millions of
                        break          # members costs a full scan
                    members.append(member)
                out["tool"] = "tarfile"
                out["count"] = len(members)
                out["total"] = sum(m.size for m in members)
                out["count_exact"] = not capped
                out["entries"] = [[m.name, m.size] for m in members[:max_entries]]
                out["truncated"] = capped or len(members) > max_entries
                return out
        except tarfile.TarError:
            pass

    # Not a zip or a tar: hand the descriptor to an external lister.
    os.lseek(fd, 0, os.SEEK_SET)
    for tool, args in ARCHIVE_TOOLS:
        if not os.path.exists(tool):
            continue
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            proc = subprocess.run(
                [tool] + args + ["/dev/fd/%d" % fd],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=(fd,), timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0 or not proc.stdout:
            continue  # encrypted, corrupt, or a format this tool refuses
        names = _parse_listing(tool, proc.stdout)
        if not names:
            continue
        out["tool"] = os.path.basename(tool)
        out["count"] = len(names)
        out["entries"] = [[n, None] for n in names[:max_entries]]
        out["truncated"] = len(names) > max_entries
        return out

    raise RuntimeError("no archive lister available for this format")


def _parse_listing(tool: str, data: bytes) -> list:
    """Entry names out of a lister's stdout. Names only: the long formats
    differ per tool and per locale, and a preview does not need sizes."""
    text = data.decode("utf-8", errors="replace")
    if tool.endswith("7z"):
        # -slt prints "Path = name" records.
        return [
            line[7:].strip() for line in text.splitlines()
            if line.startswith("Path = ")
        ]
    return [line for line in text.splitlines() if line.strip()]


def office_preview(fd: int, name: str, limit: int) -> dict:
    """Preview an OOXML or ODF document without an office suite.

    Both are zip containers, so the standard library is enough. Prefers the
    thumbnail the authoring application already embedded — that is its own
    rendering of page one for the cost of unzipping one member — and falls
    back to the document's text.
    """
    import zipfile

    out = {"kind": "empty", "image_b64": None, "image_type": None,
           "text": "", "truncated": False}
    with _rewound(fd) as fh:
        if not zipfile.is_zipfile(fh):
            raise RuntimeError("not an office document (no zip container)")
        fh.seek(0)
        with zipfile.ZipFile(fh) as zf:
            names = set(zf.namelist())
            thumb = _embedded_thumbnail(zf, names)
            if thumb is not None:
                import base64

                data, kind = thumb
                out["kind"] = "image"
                out["image_b64"] = base64.b64encode(data).decode("ascii")
                out["image_type"] = kind
            text = _office_text(zf, names, limit)
            if text:
                out["text"] = text[:limit]
                out["truncated"] = len(text) > limit
                if out["kind"] != "image":
                    out["kind"] = "text"
    if out["kind"] == "empty":
        raise RuntimeError("nothing previewable in this document")
    return out


def _embedded_thumbnail(zf, names):
    for member, kind in (
        ("docProps/thumbnail.jpeg", "jpeg"),
        ("docProps/thumbnail.jpg", "jpeg"),
        ("docProps/thumbnail.png", "png"),
        ("Thumbnails/thumbnail.png", "png"),
    ):
        if member not in names:
            continue
        info = zf.getinfo(member)
        if info.file_size > OFFICE_MAX_THUMB_BYTES:
            continue
        with zf.open(member) as fh:
            return fh.read(OFFICE_MAX_THUMB_BYTES), kind
    return None


def _member_xml(zf, member: str):
    """Parse one zip member as XML, bounded, with entities left unresolved."""
    from xml.etree import ElementTree as ET

    info = zf.getinfo(member)
    if info.file_size > OFFICE_MAX_MEMBER_BYTES:
        raise RuntimeError("%s is implausibly large" % member)
    with zf.open(member) as fh:
        data = fh.read(OFFICE_MAX_MEMBER_BYTES)
    # ET's parser does not expand external entities and caps internal ones,
    # so a billion-laughs document fails here rather than eating the worker.
    return ET.fromstring(data)


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


def _office_text(zf, names, limit: int) -> str:
    if "word/document.xml" in names:
        root = _member_xml(zf, "word/document.xml")
        return "\n".join(
            "".join(node.text or "" for node in para.iter(W + "t"))
            for para in root.iter(W + "p")
        )
    if any(n.startswith("ppt/slides/slide") for n in names):
        slides = sorted(
            n for n in names
            if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        )
        chunks, size = [], 0
        for i, slide in enumerate(slides, 1):
            root = _member_xml(zf, slide)
            body = " ".join(
                node.text or "" for node in root.iter(A + "t")
            ).strip()
            chunks.append("—— slide %d ——\n%s" % (i, body))
            size += len(chunks[-1])
            if size > limit:
                break
        return "\n\n".join(chunks)
    if "xl/workbook.xml" in names:
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = _member_xml(zf, "xl/sharedStrings.xml")
            shared = [
                "".join(node.text or "" for node in item.iter(SS + "t"))
                for item in root.iter(SS + "si")
            ]
        sheets = sorted(
            n for n in names
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        rows, size = [], 0
        for sheet in sheets:
            root = _member_xml(zf, sheet)
            for row in root.iter(SS + "row"):
                cells = []
                for cell in row.iter(SS + "c"):
                    value = cell.find(SS + "v")
                    if value is None or value.text is None:
                        cells.append("")
                    elif cell.get("t") == "s":
                        # A shared-string cell should hold an integer index;
                        # a malformed one must cost that cell, not the whole
                        # document's preview.
                        try:
                            index = int(value.text)
                        except ValueError:
                            cells.append("")
                            continue
                        cells.append(
                            shared[index] if 0 <= index < len(shared) else ""
                        )
                    else:
                        cells.append(value.text)
                rows.append("\t".join(cells))
                size += len(rows[-1])
                if size > limit:
                    return "\n".join(rows)
        return "\n".join(rows)
    if "content.xml" in names:  # ODF
        root = _member_xml(zf, "content.xml")
        return "\n".join(
            "".join(node.itertext()).strip()
            for node in root.iter(ODF_TEXT + "p")
        )
    return ""


def office_pages(fd: int, name: str, page_w: int, max_pages: int, start: int = 0):
    """Yield (page_count, png_bytes) for an office document, as page images.

    Word-processor and spreadsheet documents are converted to the HTML subset
    QTextDocument understands and laid out here: no office suite, no
    subprocess, ~8 ms to convert and ~7 ms a page. Slide decks have no path
    through this — their content is absolutely positioned graphics, which
    QTextDocument cannot lay out — so they raise, and the caller falls back
    to the thumbnail the deck embeds plus its text.
    """
    yield from _pages_via_qtextdocument(fd, name, page_w, max_pages, start)


def _pages_via_qtextdocument(fd: int, name: str, page_w: int, max_pages: int,
                             start: int = 0):
    import zipfile

    from PySide6.QtCore import QRectF, QSizeF, Qt, QUrl
    from PySide6.QtGui import QImage, QPainter, QTextDocument

    page_h = int(page_w * PAGE_RATIO)
    with _rewound(fd) as fh:
        if not zipfile.is_zipfile(fh):
            raise RuntimeError("not an office document")
        fh.seek(0)
        with zipfile.ZipFile(fh) as zf:
            names = set(zf.namelist())
            body, images = _office_html(zf, names)
            if not body:
                raise RuntimeError("no layout for this document type")

            doc = QTextDocument()
            doc.setDocumentMargin(page_w * 0.06)
            for key, data in images.items():
                img = QImage.fromData(data)
                if not img.isNull():
                    doc.addResource(
                        QTextDocument.ResourceType.ImageResource,
                        QUrl(key), img,
                    )
            doc.setHtml(body)
            doc.setPageSize(QSizeF(page_w, page_h))
            count = min(doc.pageCount(), max_pages)
            yield from _document_pages(doc, page_w, page_h, count, start)


def _document_pages(doc, page_w: int, page_h: int, count: int, start: int,
                    background: str = "#ffffff"):
    """Yield (count, png) for a laid-out QTextDocument, page by page.

    Shared by the office and EPUB paths: both end up with one long document
    that is sliced into page-sized images here, so a book and a report are
    cut, painted and counted by the same code.
    """
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter

    colour = QColor(background)
    if not colour.isValid():
        colour = QColor("#ffffff")
    for index in range(max(start, 0), count):
        page = QImage(page_w, page_h, QImage.Format.Format_RGB32)
        page.fill(colour)
        painter = QPainter(page)
        painter.translate(0, -index * page_h)
        painter.setClipRect(QRectF(0, index * page_h, page_w, page_h))
        doc.drawContents(painter, QRectF(0, index * page_h, page_w, page_h))
        painter.end()
        page.setText("QuickView:PageCount", str(doc.pageCount()))
        yield count, _encode(page)


def _office_html(zf, names) -> tuple:
    """(html, images) for the document types QTextDocument can lay out."""
    if "word/document.xml" in names:
        return _docx_html(zf, names)
    if "xl/workbook.xml" in names:
        return _xlsx_html(zf, names), {}
    if "content.xml" in names and "styles.xml" in names:
        return _odf_html(zf, names)
    return "", {}  # slide decks land here: nothing to lay out, only a thumbnail


_HTML_HEAD = (
    "<body style='font-family:serif; font-size:11pt; color:#111;'>"
)


def _escape(text: str) -> str:
    import html

    return html.escape(text or "")


def _docx_html(zf, names) -> tuple:
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    rels, images = {}, {}
    if "word/_rels/document.xml.rels" in names:
        root = _member_xml(zf, "word/_rels/document.xml.rels")
        for rel in root:
            target = rel.get("Target", "")
            member = "word/" + target.lstrip("./")
            if member in names and member.startswith("word/media/"):
                rels[rel.get("Id")] = member

    root = _member_xml(zf, "word/document.xml")
    parts = []
    for block in root.iter():
        if block.tag == W + "p":
            parts.append(_docx_paragraph(block, rels, images, zf))
        elif block.tag == W + "tbl":
            parts.append(_docx_table(block, rels, images, zf))
    return _HTML_HEAD + "".join(p for p in parts if p) + "</body>", images


def _docx_paragraph(para, rels, images, zf) -> str:
    style_node = para.find(W + "pPr/" + W + "pStyle")
    style = style_node.get(W + "val", "") if style_node is not None else ""
    pieces = []
    for run in para.iter(W + "r"):
        text = "".join(node.text or "" for node in run.iter(W + "t"))
        if text:
            props = run.find(W + "rPr")
            markup = _escape(text)
            if props is not None:
                if props.find(W + "b") is not None:
                    markup = "<b>%s</b>" % markup
                if props.find(W + "i") is not None:
                    markup = "<i>%s</i>" % markup
                if props.find(W + "u") is not None:
                    markup = "<u>%s</u>" % markup
            pieces.append(markup)
        for blip in run.iter(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
        ):
            member = rels.get(blip.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/"
                "relationships}embed"
            ))
            if member and _stash_image(zf, member, images):
                pieces.append("<img src='%s' width='420'>" % member)
    body = "".join(pieces)
    if not body:
        return "<p>&nbsp;</p>"
    if style.startswith("Title"):
        return "<h1 style='text-align:center'>%s</h1>" % body
    if style.startswith("Heading"):
        level = style[-1] if style[-1].isdigit() else "2"
        return "<h%s>%s</h%s>" % (min(int(level), 4), body, min(int(level), 4))
    if style.startswith("ListParagraph"):
        return "<p style='margin-left:24px'>• %s</p>" % body
    return "<p>%s</p>" % body


def _docx_table(table, rels, images, zf) -> str:
    rows = []
    for row in table.iter(W + "tr"):
        cells = []
        for cell in row.iter(W + "tc"):
            inner = "".join(
                _docx_paragraph(para, rels, images, zf)
                for para in cell.iter(W + "p")
            )
            cells.append("<td style='padding:4px'>%s</td>" % inner)
        rows.append("<tr>%s</tr>" % "".join(cells))
    return (
        "<table border='1' cellspacing='0' width='100%%'>%s</table>"
        % "".join(rows)
    )


def _stash_image(zf, member: str, images: dict) -> bool:
    if member in images:
        return True
    try:
        info = zf.getinfo(member)
    except KeyError:
        return False
    if info.file_size > OFFICE_MAX_THUMB_BYTES:
        return False
    with zf.open(member) as fh:
        images[member] = fh.read(OFFICE_MAX_THUMB_BYTES)
    return True


def _xlsx_html(zf, names) -> str:
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = _member_xml(zf, "xl/sharedStrings.xml")
        shared = [
            "".join(node.text or "" for node in item.iter(SS + "t"))
            for item in root.iter(SS + "si")
        ]
    tables = []
    sheets = sorted(
        n for n in names
        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
    )
    for number, sheet in enumerate(sheets, 1):
        root = _member_xml(zf, sheet)
        rows = []
        for row in root.iter(SS + "row"):
            cells = []
            for cell in row.iter(SS + "c"):
                value = cell.find(SS + "v")
                if value is None or value.text is None:
                    text = ""
                elif cell.get("t") == "s":
                    try:  # see _office_text: one bad cell, not one bad file
                        index = int(value.text)
                    except ValueError:
                        index = -1
                    text = shared[index] if 0 <= index < len(shared) else ""
                else:
                    text = value.text
                cells.append(
                    "<td style='padding:3px 6px'>%s</td>" % _escape(text)
                )
            rows.append("<tr>%s</tr>" % "".join(cells))
            if len(rows) > 500:  # a preview, not the whole workbook
                break
        if rows:
            tables.append(
                "<h3>Sheet %d</h3><table border='1' cellspacing='0'>%s</table>"
                % (number, "".join(rows))
            )
    return _HTML_HEAD + "".join(tables) + "</body>" if tables else ""


def _odf_html(zf, names) -> tuple:
    ODF_H = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}h"
    ODF_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    images = {}
    root = _member_xml(zf, "content.xml")
    parts = []
    for node in root.iter():
        if node.tag == ODF_TEXT + "p":
            text = _escape("".join(node.itertext()).strip())
            parts.append("<p>%s</p>" % (text or "&nbsp;"))
        elif node.tag == ODF_H:
            parts.append("<h2>%s</h2>" % _escape("".join(node.itertext())))
        elif node.tag == ODF_TABLE + "table":
            rows = []
            for row in node.iter(ODF_TABLE + "table-row"):
                cells = [
                    "<td style='padding:3px 6px'>%s</td>"
                    % _escape("".join(cell.itertext()))
                    for cell in row.iter(ODF_TABLE + "table-cell")
                ]
                rows.append("<tr>%s</tr>" % "".join(cells))
            parts.append(
                "<table border='1' cellspacing='0'>%s</table>" % "".join(rows)
            )
    return _HTML_HEAD + "".join(parts) + "</body>", images


# ------------------------------------------------------------- spreadsheets
# A workbook is a grid, not a page, so it gets a grid: the cells come back as
# text and the daemon puts them in a table with one tab per sheet. Only the
# XML containers are handled (xlsx/ods) — the same standard-library-only path
# the rest of the office code takes, with no office suite anywhere near it.
SHEET_MAX_SHEETS = 24
SHEET_MAX_ROWS = 2000
SHEET_MAX_COLS = 64
SHEET_MAX_CELL_CHARS = 512
# Excel's epoch is 1899-12-30: 1900 is treated as a leap year by the format,
# so counting from the 30th makes the off-by-one come out right for every
# date a preview will ever show.
_EXCEL_EPOCH_ORDINAL = 693594  # date(1899, 12, 30).toordinal()

ODF_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
ODF_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
)


def read_workbook(fd: int, name: str = "") -> dict:
    """Read a spreadsheet's cells as text, sheet by sheet.

    Returns {"sheets": [{"name", "rows", "cols", "align", "clipped"}, ...],
    "clipped": bool}. Every sheet is bounded (rows, columns and the length of
    one cell), so a workbook with a million-row sheet costs the same as a
    small one: the daemon is showing a preview, not opening the file.
    """
    import zipfile

    with _rewound(fd) as fh:
        if not zipfile.is_zipfile(fh):
            raise RuntimeError("not a spreadsheet")
        fh.seek(0)
        with zipfile.ZipFile(fh) as zf:
            names = set(zf.namelist())
            if "xl/workbook.xml" in names:
                sheets = _xlsx_sheets(zf, names)
            elif "content.xml" in names:
                sheets = _ods_sheets(zf)
            else:
                raise RuntimeError("no spreadsheet part in this container")

    clipped = len(sheets) > SHEET_MAX_SHEETS
    sheets = sheets[:SHEET_MAX_SHEETS]
    if not any(sheet["rows"] for sheet in sheets):
        raise RuntimeError("no cells to show")
    return {"sheets": sheets, "clipped": clipped}


def _column_alignment(rows: list, cols: int) -> list:
    """Per column: "r" when its body reads as numbers, "l" otherwise.

    Alignment is decided by column rather than by cell because that is what
    a spreadsheet looks like — one stray text cell in a column of figures
    should not knock a single number out of line. The first row is skipped:
    it is usually a header, and a header is not evidence about the data.
    """
    align = []
    for col in range(cols):
        numbers = other = 0
        for row in rows[1:]:
            text = row[col] if col < len(row) else ""
            if not text:
                continue
            if _looks_numeric(text):
                numbers += 1
            else:
                other += 1
        align.append("r" if numbers > other else "l")
    return align


def _looks_numeric(text: str) -> bool:
    stripped = text.strip().lstrip("+-").replace(",", "")
    for suffix in ("%", "€", "$", "£"):
        stripped = stripped.rstrip(suffix)
    if not stripped:
        return False
    try:
        float(stripped)
    except ValueError:
        return False
    return True


def _sheet_payload(sheet_name: str, grid: dict, clipped: bool) -> dict:
    """Turn {(row, col): text} into a rectangle of rows, trimmed of the
    empty edges a spreadsheet almost always carries around its data."""
    if not grid:
        return {"name": sheet_name, "rows": [], "cols": 0, "align": [],
                "clipped": clipped}
    top = min(r for r, _c in grid)
    bottom = max(r for r, _c in grid)
    left = min(c for _r, c in grid)
    right = max(c for _r, c in grid)
    cols = min(right - left + 1, SHEET_MAX_COLS)
    clipped = clipped or right - left + 1 > cols
    rows = []
    for r in range(top, min(bottom, top + SHEET_MAX_ROWS - 1) + 1):
        rows.append([grid.get((r, left + c), "") for c in range(cols)])
    clipped = clipped or bottom - top + 1 > len(rows)
    return {
        "name": sheet_name,
        "rows": rows,
        "cols": cols,
        "align": _column_alignment(rows, cols),
        "clipped": clipped,
        "first_col": left,   # so the daemon can letter the columns truthfully
        "first_row": top,
    }


def _col_index(ref: str) -> int:
    """"BC12" -> 54. The letters are base-26 with no zero digit."""
    index = 0
    for ch in ref:
        if not ch.isalpha():
            break
        index = index * 26 + (ord(ch.upper()) - 64)
    return index - 1


def _xlsx_sheets(zf, names) -> list:
    """Sheet name and grid per visible worksheet, in workbook order."""
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = _member_xml(zf, "xl/sharedStrings.xml")
        shared = [
            "".join(node.text or "" for node in item.iter(SS + "t"))
            for item in root.iter(SS + "si")
        ]
    date_styles = _xlsx_date_styles(zf, names)

    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        root = _member_xml(zf, "xl/_rels/workbook.xml.rels")
        for rel in root.iter(REL + "Relationship"):
            target = rel.get("Target", "").lstrip("./")
            member = target if target.startswith("xl/") else "xl/" + target
            rels[rel.get("Id")] = member

    entries = []
    book = _member_xml(zf, "xl/workbook.xml")
    for node in book.iter(SS + "sheet"):
        if node.get("state") in ("hidden", "veryHidden"):
            continue  # hidden in Excel, hidden here
        member = rels.get(node.get(DOC_REL + "id"))
        if member in names:
            entries.append((node.get("name") or "Sheet", member))
    if not entries:  # no rels, or a workbook part that lied about them
        entries = [
            (n.rsplit("/", 1)[-1][:-4], n) for n in sorted(
                n for n in names
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
        ]

    sheets = []
    for sheet_name, member in entries[:SHEET_MAX_SHEETS]:
        grid, clipped = _xlsx_grid(zf, member, shared, date_styles)
        sheets.append(_sheet_payload(sheet_name, grid, clipped))
    return sheets


def _xlsx_grid(zf, member: str, shared: list, date_styles: dict) -> tuple:
    """{(row, col): text} for one worksheet, plus whether it was cut short.

    Cells are placed by their own reference rather than by the order they
    appear in: a sparse row omits the empty cells entirely, so counting them
    off would slide every value left of where it belongs.
    """
    root = _member_xml(zf, member)
    grid, clipped = {}, False
    for index, row in enumerate(root.iter(SS + "row")):
        try:
            number = int(row.get("r") or index + 1)
        except ValueError:
            number = index + 1
        for position, cell in enumerate(row.iter(SS + "c")):
            ref = cell.get("r") or ""
            col = _col_index(ref) if ref else position
            if col < 0:
                continue
            text = _xlsx_cell_text(cell, shared, date_styles)
            if text:
                grid[(number, col)] = text[:SHEET_MAX_CELL_CHARS]
        if len(grid) > SHEET_MAX_ROWS * SHEET_MAX_COLS:
            clipped = True
            break
    return grid, clipped


def _xlsx_cell_text(cell, shared: list, date_styles: dict) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        node = cell.find(SS + "is")
        return "".join(t.text or "" for t in node.iter(SS + "t")) if (
            node is not None
        ) else ""
    value = cell.find(SS + "v")
    if value is None or value.text is None:
        return ""
    raw = value.text
    if kind == "s":
        try:  # see _office_text: one bad cell, not one bad file
            index = int(raw)
        except ValueError:
            return ""
        return shared[index] if 0 <= index < len(shared) else ""
    if kind == "b":
        return "TRUE" if raw.strip() not in ("0", "") else "FALSE"
    if kind in ("str", "e"):
        return raw
    style = date_styles.get(cell.get("s") or "0")
    if style == "percent":
        try:
            return _trim_number(repr(float(raw) * 100)) + "%"
        except ValueError:
            return raw
    if style:
        formatted = _serial_to_date(raw, style)
        if formatted:
            return formatted
    return _trim_number(raw)


def _trim_number(raw: str) -> str:
    """15.700000000000001 -> "15.7". Spreadsheets store binary floats and
    print them rounded; showing the stored digits reads as a bug."""
    try:
        number = float(raw)
    except ValueError:
        return raw
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return ("%.10g" % number)


def _serial_to_date(raw: str, style: str) -> str:
    import datetime

    try:
        serial = float(raw)
    except ValueError:
        return ""
    if not 0 < serial < 2958466:  # 1900-01-01 .. 9999-12-31
        return ""
    days = int(serial)
    try:
        day = datetime.date.fromordinal(_EXCEL_EPOCH_ORDINAL + days)
    except (ValueError, OverflowError):
        return ""
    if style == "time":
        seconds = round((serial - days) * 86400)
        return "%02d:%02d:%02d" % (
            seconds // 3600 % 24, seconds // 60 % 60, seconds % 60
        )
    text = day.isoformat()
    if style == "datetime":
        seconds = round((serial - days) * 86400)
        text += " %02d:%02d" % (seconds // 3600 % 24, seconds // 60 % 60)
    return text


def _xlsx_date_styles(zf, names) -> dict:
    """Cell-format index -> "date" / "datetime" / "time".

    A date in xlsx is an ordinary number wearing a number format, so without
    this every date in the file shows up as a five-digit serial.
    """
    if "xl/styles.xml" not in names:
        return {}
    try:
        root = _member_xml(zf, "xl/styles.xml")
    except Exception:
        return {}
    builtin = {
        **{str(i): "date" for i in (14, 15, 16, 17, 30, 34, 35)},
        **{str(i): "datetime" for i in (22,)},
        **{str(i): "time" for i in (18, 19, 20, 21, 45, 46, 47)},
        **{str(i): "percent" for i in (9, 10)},
    }
    codes = {}
    for node in root.iter(SS + "numFmt"):
        codes[node.get("numFmtId")] = node.get("formatCode", "")
    styles, index = {}, 0
    for xfs in root.iter(SS + "cellXfs"):
        for xf in xfs.iter(SS + "xf"):
            fmt_id = xf.get("numFmtId", "0")
            kind = builtin.get(fmt_id)
            if kind is None and fmt_id in codes:
                kind = _classify_format(codes[fmt_id])
            if kind:
                styles[str(index)] = kind
            index += 1
    return styles


def _classify_format(code: str) -> str:
    """Read a custom number-format code well enough to spot dates.

    Only the date/time letters outside quoted literals matter here; "m" is
    minutes or months depending on its neighbours, which is why a code with
    hours *and* a day is a datetime rather than either alone.
    """
    body, quoted = [], False
    for ch in code:
        if ch in ('"', "'"):
            quoted = not quoted
        elif not quoted:
            body.append(ch)
    text = "".join(body).lower()
    if "%" in text:
        return "percent"
    has_day = "y" in text or "d" in text
    has_time = "h" in text or "s" in text
    if has_day and has_time:
        return "datetime"
    if has_day:
        return "date"
    if has_time:
        return "time"
    return ""


def _ods_sheets(zf) -> list:
    """Same shape from an OpenDocument spreadsheet.

    ODF writes the displayed text alongside the value and run-length encodes
    repeats, so the text is taken straight from the cell and the repeat
    counts are expanded — bounded, since an empty trailing cell may claim to
    repeat a thousand times.
    """
    root = _member_xml(zf, "content.xml")
    sheets = []
    for table in root.iter(ODF_TABLE + "table"):
        grid, clipped = {}, False
        row_number = 1  # spreadsheets count rows from one, and so does the
        for row in table.iter(ODF_TABLE + "table-row"):  # preview's gutter
            repeat = _int_attr(row, ODF_TABLE + "number-rows-repeated", 1)
            cells, col = [], 0
            for cell in row:
                if cell.tag not in (
                    ODF_TABLE + "table-cell", ODF_TABLE + "covered-table-cell"
                ):
                    continue
                span = _int_attr(cell, ODF_TABLE + "number-columns-repeated", 1)
                text = " ".join(
                    "".join(p.itertext()).strip()
                    for p in cell.iter(ODF_TEXT + "p")
                ).strip()
                if text:
                    for offset in range(min(span, SHEET_MAX_COLS)):
                        cells.append((col + offset, text))
                col += span
                if col > SHEET_MAX_COLS * 4:
                    break
            # A row repeated a thousand times is padding unless it has
            # content, and padding is not worth a thousand rows of preview.
            repeat = min(repeat, 100 if cells else 1)
            for line in range(repeat):
                for column, text in cells:
                    grid[(row_number + line, column)] = (
                        text[:SHEET_MAX_CELL_CHARS]
                    )
            row_number += repeat
            if len(grid) > SHEET_MAX_ROWS * SHEET_MAX_COLS:
                clipped = True
                break
        sheets.append(_sheet_payload(
            table.get(ODF_TABLE + "name") or "Sheet", grid, clipped
        ))
    return sheets


def _int_attr(node, attr: str, default: int) -> int:
    try:
        return max(int(node.get(attr, default)), 1)
    except (TypeError, ValueError):
        return default


# -------------------------------------------------------------------- epub
# An EPUB is a zip of XHTML documents with a reading order and a table of
# contents, which is almost exactly what QTextDocument lays out — so a book
# is previewed by the same page pipeline as an office document, with no
# reader engine anywhere. What the office path cannot do is say *where* a
# chapter starts, and a book without that is a wall of pages: the spine is
# inserted chunk by chunk through a cursor, and the cursor position at each
# chunk's start is what turns the table of contents into page numbers.
# How a book is painted. A preview of a novel is something a person reads
# for minutes rather than glances at, so the page colours are a setting —
# see config.book_theme. Only the page is themed: the panel around it is
# application chrome and stays as it is.
BOOK_THEMES = {
    "paper": {"bg": "#ffffff", "fg": "#141414", "head": "#000000",
              "muted": "#555555"},
    "sepia": {"bg": "#f4ecd8", "fg": "#4a3f35", "head": "#33291f",
              "muted": "#7a6a58"},
    "dark": {"bg": "#1e1e21", "fg": "#d6d6da", "head": "#ffffff",
             "muted": "#9a9aa2"},
    # gruvbox, from the palette itself: bg0/fg1 for the page, bright yellow
    # (dark) and neutral orange (light) for headings, gray for quotations.
    "gruvbox-dark": {"bg": "#282828", "fg": "#ebdbb2", "head": "#fabd2f",
                     "muted": "#a89984"},
    "gruvbox-light": {"bg": "#fbf1c7", "fg": "#3c3836", "head": "#af3a03",
                      "muted": "#7c6f64"},
}
DEFAULT_BOOK_THEME = "paper"

EPUB_MAX_CHARS = 1_500_000     # markup pulled from the spine, total
EPUB_MAX_SPINE_ITEMS = 300
EPUB_MAX_IMAGES = 40
EPUB_MAX_TOC = 500
# Tags QTextDocument understands, mapped from the ones books actually use.
# Anything absent contributes its text and its children but no markup of its
# own — a <section> or a <span> is a container, not something to render.
_EPUB_TAGS = {
    "h1": ("<h2>", "</h2>"), "h2": ("<h2>", "</h2>"),
    "h3": ("<h3>", "</h3>"), "h4": ("<h3>", "</h3>"),
    "h5": ("<h4>", "</h4>"), "h6": ("<h4>", "</h4>"),
    "p": ("<p>", "</p>"), "blockquote": ("<blockquote>", "</blockquote>"),
    "ul": ("<ul>", "</ul>"), "ol": ("<ol>", "</ol>"), "li": ("<li>", "</li>"),
    "b": ("<b>", "</b>"), "strong": ("<b>", "</b>"),
    "i": ("<i>", "</i>"), "em": ("<i>", "</i>"), "u": ("<u>", "</u>"),
    "sup": ("<sup>", "</sup>"), "sub": ("<sub>", "</sub>"),
    "table": ("<table border='1' cellspacing='0' width='100%'>", "</table>"),
    "tr": ("<tr>", "</tr>"), "td": ("<td>", "</td>"), "th": ("<th>", "</th>"),
    "pre": ("<pre>", "</pre>"), "code": ("<code>", "</code>"),
    "hr": ("<hr>", ""), "br": ("<br>", ""),
}
# Never rendered: script and style would print as text, and the navigation
# document is the table of contents, not a chapter.
_EPUB_SKIP = {"script", "style", "head", "svg", "iframe", "object", "video",
              "audio", "nav", "template"}
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def book_theme(name: str) -> dict:
    """The named palette, or the default. An unknown name is a typo in a
    config file, which must not stop a book from opening."""
    return BOOK_THEMES.get(
        (name or "").strip().lower(), BOOK_THEMES[DEFAULT_BOOK_THEME]
    )


def _book_css(palette: dict) -> str:
    """The document style sheet a themed book is laid out with.

    Selectors rather than a body rule: the spine is inserted as HTML
    fragments through a cursor, and a fragment has no body element for a
    body rule to reach.
    """
    return (
        "p, li, td, th, pre, code { font-family: serif; font-size: 11pt;"
        " color: %(fg)s; }"
        "h1, h2, h3, h4 { font-family: serif; color: %(head)s; }"
        "blockquote { font-family: serif; font-size: 11pt;"
        " color: %(muted)s; }"
        "hr { color: %(muted)s; }"
    ) % palette


def epub_pages(fd: int, name: str, page_w: int, max_pages: int,
               start: int = 0, theme: str = DEFAULT_BOOK_THEME):
    """Yield (info, png_bytes) for a book, as page images.

    info is the same dict on every page — {"count", "chapters", "title"} —
    so the caller can build its sidebar from the first frame it receives
    without waiting for the book to finish rendering.
    """
    page_h = int(page_w * PAGE_RATIO)
    book = _epub_document(fd, page_w, theme)
    doc = book["doc"]
    count = min(doc.pageCount(), max_pages)
    info = {
        "count": count,
        "title": book["title"],
        "chapters": _epub_chapters(
            doc, book["toc"], book["positions"], book["headings"],
            page_h, count,
        ),
    }
    for _n, png in _document_pages(
        doc, page_w, page_h, count, start, book["palette"]["bg"]
    ):
        yield info, png


def _epub_document(fd: int, page_w: int, theme: str) -> dict:
    """Lay a book out once: the document, and what was needed to build it.

    Rendering and searching both need the same laid-out document — a match
    rectangle is only meaningful in the layout the pages were painted from —
    so the whole of it lives here rather than in either caller.
    """
    import zipfile

    from PySide6.QtCore import Qt, QSizeF, QUrl
    from PySide6.QtGui import (
        QImage, QTextBlockFormat, QTextCursor, QTextDocument, QTextFormat,
    )

    page_h = int(page_w * PAGE_RATIO)
    with _rewound(fd) as fh:
        if not zipfile.is_zipfile(fh):
            raise RuntimeError("not an epub")
        fh.seek(0)
        with zipfile.ZipFile(fh) as zf:
            names = set(zf.namelist())
            spine, toc, title = _epub_parts(zf, names)
            if not spine:
                raise RuntimeError("no readable spine")

            # Converted before anything is inserted: an image resource has
            # to be registered with the document before the HTML naming it
            # is laid out, and which images a book uses is only known once
            # its markup has been walked.
            wanted = {e["target"] for e in toc if "#" in e["target"]}
            images, headings, chunks, chars = {}, {}, [], 0
            for member in spine[:EPUB_MAX_SPINE_ITEMS]:
                try:
                    part = _epub_chunks(zf, member, images, wanted, headings)
                except Exception as exc:  # noqa: BLE001 — see below
                    # One chapter that will not parse (a broken XHTML file,
                    # an encoding nothing here can read) must cost that
                    # chapter, not the book.
                    print("epub: skipping %s (%s)" % (member, exc),
                          file=_stderr())
                    continue
                for anchor, html in part:
                    if not html.strip():
                        continue
                    chunks.append((anchor, html))
                    chars += len(html)
                if chars > EPUB_MAX_CHARS:
                    break

            palette = book_theme(theme)
            doc = QTextDocument()
            doc.setDefaultStyleSheet(_book_css(palette))
            doc.setDocumentMargin(page_w * 0.06)
            doc.setPageSize(QSizeF(page_w, page_h))
            for key, data in images.items():
                img = QImage.fromData(data)
                if img.isNull():
                    continue
                # Scaled here rather than with a width attribute in the
                # markup: QTextDocument draws an image at its natural size
                # and lets it run off the page, and a book's illustrations
                # are routinely wider than the page they are printed on.
                limit = int(page_w * 0.8)
                if img.width() > limit:
                    img = img.scaledToWidth(limit, Qt.SmoothTransformation)
                doc.addResource(
                    QTextDocument.ResourceType.ImageResource, QUrl(key), img,
                )
            cursor = QTextCursor(doc)
            positions = {}
            for anchor, html in chunks:
                if cursor.position():
                    # A new spine document starts a new page, which is what
                    # a reader does with a chapter and what makes the page
                    # numbers in the sidebar mean something. A chunk split
                    # out of the *middle* of a file (a fragment the table of
                    # contents points at) does not: the book's own author
                    # chose to keep those on one page.
                    fmt = QTextBlockFormat()
                    if "#" not in anchor:
                        fmt.setPageBreakPolicy(
                            QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore
                        )
                    cursor.insertBlock(fmt)
                # setdefault: two chapters may point at the same place (a
                # part title and its first section), and the first one to
                # claim a position is the one the reader means.
                positions.setdefault(anchor, cursor.position())
                cursor.insertHtml(html)
            if not doc.characterCount() > 1:
                raise RuntimeError("nothing to lay out")

            doc.setPageSize(QSizeF(page_w, page_h))
            return {"doc": doc, "positions": positions, "toc": toc,
                    "headings": headings, "title": title, "palette": palette}


def search_epub(fd: int, query: str, page_w: int, max_pages: int,
                theme: str = DEFAULT_BOOK_THEME,
                max_hits: int = PDF_MAX_HITS) -> dict:
    """Case-insensitive search over a book, as rectangles in page pixels.

    The same answer shape as search_pdf, so the daemon paints both with the
    same code: {"matches": [{"page": n, "rects": [[x, y, w, h], ...]}, ...]}.

    The book is laid out again here rather than searched as flat text. A
    rectangle only means something in a layout, and this is the same layout
    the pages were painted from — same width, same style sheet — so a match
    lands where the reader can see it.
    """
    from PySide6.QtGui import QTextDocument

    if not query.strip():
        return {"matches": [], "capped": False}

    page_h = int(page_w * PAGE_RATIO)
    doc = _epub_document(fd, page_w, theme)["doc"]
    # Also forces the layout: a block's lines do not exist until something
    # asks for them, and without them every match measures as no rectangle
    # at all.
    count = min(doc.pageCount(), max_pages)
    matches, at = [], 0
    while True:
        cursor = doc.find(query, at, QTextDocument.FindFlag(0))
        if cursor.isNull():
            break
        # Guard against a zero-width match looping forever on the same spot.
        at = max(cursor.selectionEnd(), cursor.selectionStart() + 1)
        rects = _selection_rects(
            doc, cursor.selectionStart(), cursor.selectionEnd(),
            page_w, page_h, count,
        )
        for page, boxes in rects.items():
            matches.append({"page": page, "rects": boxes})
        if len(matches) >= max_hits:
            return {"matches": matches[:max_hits], "capped": True}
    return {"matches": matches, "capped": False}


def _selection_rects(doc, start: int, end: int, page_w: int, page_h: int,
                     count: int) -> dict:
    """{page: [[x, y, w, h], ...]} for one selection, in page pixels.

    A match that wraps gets one rectangle per line it occupies, the way the
    PDF path does it — a single bounding box would be a slab covering the
    gap between the lines as well.
    """
    layout_of = doc.documentLayout()
    out = {}
    block = doc.findBlock(start)
    while block.isValid() and block.position() < end:
        layout = block.layout()
        # The block's own origin in document coordinates. cursorToX is
        # measured from the *layout*, so without this every rectangle lands
        # one page margin to the left of the words it belongs to.
        origin = layout_of.blockBoundingRect(block).topLeft()
        first = max(start - block.position(), 0)
        last = min(end - block.position(), block.length() - 1)
        line = layout.lineForTextPosition(first)
        if not line.isValid():
            block = block.next()
            continue
        for number in range(line.lineNumber(), layout.lineCount()):
            current = layout.lineAt(number)
            line_start = current.textStart()
            line_end = line_start + current.textLength()
            if line_start >= last:
                break
            begin, finish = max(first, line_start), min(last, line_end)
            if finish <= begin:
                continue
            x1 = origin.x() + current.cursorToX(begin)[0]
            x2 = origin.x() + current.cursorToX(finish)[0]
            y = origin.y() + current.y()
            page = int(y // page_h)
            if not 0 <= page < count:
                continue
            height = current.height()
            y_in_page = y - page * page_h
            # A line sitting on a page boundary is drawn on one page only;
            # clamp rather than paint half a highlight off the bottom.
            height = min(height, page_h - y_in_page)
            if height <= 0:
                continue
            out.setdefault(page, []).append([
                round(min(x1, x2)), round(y_in_page),
                max(1, round(abs(x2 - x1))), max(1, round(height)),
            ])
        block = block.next()
    return out


def _stderr():
    import sys

    return sys.stderr


def _epub_chapters(doc, toc: list, positions: dict, headings: dict,
                   page_h: int, count: int) -> list:
    """Table of contents entries as [{title, level, page}, ...].

    A target that was never laid out (past the size budget, or a file the
    spine does not include) is dropped rather than guessed at, and so is one
    that lands past the last rendered page: a sidebar row that scrolls
    nowhere is worse than no row.
    """
    layout = doc.documentLayout()

    def place(position: int) -> tuple:
        """(page, offset down that page) for a document position."""
        block = doc.findBlock(position)
        if not block.isValid():
            return -1, 0
        top = layout.blockBoundingRect(block).top()
        page = int(top // page_h)
        return page, int(top - page * page_h)

    entries = []
    for entry in toc[:EPUB_MAX_TOC]:
        target = entry["target"]
        position = positions.get(target)
        if position is None:  # a fragment inside a chapter we did not split
            position = positions.get(target.split("#", 1)[0])
        if position is None:
            continue
        page, y = place(position)
        if 0 <= page < count:
            entries.append({
                "title": entry["title"][:OUTLINE_MAX_TITLE],
                "level": min(entry["level"], 5), "page": page, "y": y,
            })
    if entries:
        return entries
    # No usable table of contents — most books have one, but a hand-rolled
    # EPUB may not. The spine still gives a chapter list: each file's first
    # heading, or its name when it has none.
    for member, position in positions.items():
        page, y = place(position)
        if 0 <= page < count:
            entries.append({
                "title": (headings.get(member)
                          or member.rsplit("/", 1)[-1])[:OUTLINE_MAX_TITLE],
                "level": 0, "page": page, "y": y,
            })
    return entries[:EPUB_MAX_TOC]


def _epub_parts(zf, names) -> tuple:
    """(spine members, toc entries, book title) for an open EPUB."""
    import posixpath

    opf = _epub_opf_path(zf, names)
    base = posixpath.dirname(opf)
    root = _member_xml(zf, opf)

    manifest, title = {}, ""
    for node in root.iter():
        tag = _tag(node)
        if tag == "item":
            manifest[node.get("id")] = (
                _zip_path(base, node.get("href", "")),
                node.get("media-type", ""),
                node.get("properties", "") or "",
            )
        elif tag == "title" and not title and node.text:
            title = node.text.strip()[:200]

    spine, toc_id = [], None
    for node in root.iter():
        if _tag(node) != "spine":
            continue
        toc_id = node.get("toc")
        for ref in node:
            if _tag(ref) != "itemref":
                continue
            item = manifest.get(ref.get("idref"))
            # Only the readable documents: a spine may also list an SVG
            # cover, which has no text to lay out.
            if item and item[0] in names and "html" in item[1]:
                spine.append(item[0])
        break

    return spine, _epub_toc(zf, names, manifest, toc_id), title


def _epub_toc(zf, names, manifest: dict, toc_id) -> list:
    """[{title, level, target}, ...] from the EPUB 3 nav doc or the EPUB 2
    NCX, whichever the book carries. Targets are "member" or "member#id"."""
    nav = next(
        (path for path, _media, props in manifest.values()
         if "nav" in props.split() and path in names),
        None,
    )
    if nav:
        try:
            return _epub_nav_toc(zf, nav)
        except Exception as exc:  # noqa: BLE001
            print("epub: unreadable nav (%s)" % exc, file=_stderr())
    ncx = None
    if toc_id and toc_id in manifest:
        ncx = manifest[toc_id][0]
    if ncx is None:
        ncx = next(
            (path for path, media, _props in manifest.values()
             if "dtbncx" in media and path in names),
            None,
        )
    if ncx and ncx in names:
        try:
            return _epub_ncx_toc(zf, ncx)
        except Exception as exc:  # noqa: BLE001
            print("epub: unreadable ncx (%s)" % exc, file=_stderr())
    return []


def _epub_nav_toc(zf, member: str) -> list:
    """EPUB 3: <nav epub:type="toc"><ol><li><a href=…>Title</a>…"""
    import posixpath

    root = _epub_dom(zf, member)
    base = posixpath.dirname(member)
    navs = [n for n in root.iter() if _tag(n) == "nav"]
    chosen = next(
        (n for n in navs
         if "toc" in (n.get("{http://www.idpf.org/2007/ops}type") or "")),
        navs[0] if navs else None,
    )
    if chosen is None:
        return []
    out = []

    def walk(node, level: int):
        for item in node:
            if _tag(item) != "li" or len(out) >= EPUB_MAX_TOC:
                continue
            link = next((c for c in item.iter() if _tag(c) == "a"), None)
            if link is not None and link.get("href"):
                text = " ".join("".join(link.itertext()).split())
                if text:
                    out.append({
                        "title": text, "level": level,
                        "target": _zip_target(base, link.get("href")),
                    })
            for child in item:
                if _tag(child) in ("ol", "ul"):
                    walk(child, level + 1)

    for node in chosen:
        if _tag(node) in ("ol", "ul"):
            walk(node, 0)
    return out


def _epub_ncx_toc(zf, member: str) -> list:
    """EPUB 2: <navMap><navPoint><navLabel><text>…, nested for subsections."""
    import posixpath

    root = _member_xml(zf, member)
    base = posixpath.dirname(member)
    out = []

    def walk(node, level: int):
        for point in node:
            if _tag(point) != "navpoint" or len(out) >= EPUB_MAX_TOC:
                continue
            label = next(
                (c for c in point.iter() if _tag(c) == "text"), None
            )
            content = next(
                (c for c in point.iter() if _tag(c) == "content"), None
            )
            if label is not None and content is not None and content.get("src"):
                text = " ".join("".join(label.itertext()).split())
                if text:
                    out.append({
                        "title": text, "level": level,
                        "target": _zip_target(base, content.get("src")),
                    })
            walk(point, level + 1)

    for node in root.iter():
        if _tag(node) == "navmap":
            walk(node, 0)
            break
    return out


def _epub_opf_path(zf, names) -> str:
    """The package document, from META-INF/container.xml."""
    if "META-INF/container.xml" in names:
        root = _member_xml(zf, "META-INF/container.xml")
        for node in root.iter():
            if _tag(node) == "rootfile" and node.get("full-path") in names:
                return node.get("full-path")
    # A container that does not say (or lies): take the one package
    # document in the archive rather than refusing to open the book.
    opfs = sorted(n for n in names if n.lower().endswith(".opf"))
    if opfs:
        return opfs[0]
    raise RuntimeError("no package document")


def _tag(node) -> str:
    """Local name of an element, namespace and case dropped."""
    tag = node.tag
    if not isinstance(tag, str):  # a comment or a processing instruction
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _zip_path(base: str, href: str) -> str:
    """An href inside the book, as the zip member it names."""
    import posixpath
    import urllib.parse

    href = urllib.parse.unquote((href or "").split("#", 1)[0])
    if not href:
        return ""
    path = posixpath.normpath(posixpath.join(base, href))
    return path.lstrip("./")


def _zip_target(base: str, href: str) -> str:
    """Same, but keeping the fragment: "text/ch1.xhtml#part2"."""
    import urllib.parse

    raw, _, fragment = (href or "").partition("#")
    path = _zip_path(base, raw)
    return path + "#" + urllib.parse.unquote(fragment) if fragment else path


def _epub_dom(zf, member: str):
    """Parse one XHTML member, with HTML named entities resolved first.

    Books are full of &nbsp; and &mdash;, which are HTML entities that no
    XML parser is required to know: left alone they abort the parse of an
    otherwise perfectly good chapter.
    """
    import re
    from html.entities import html5
    from xml.etree import ElementTree as ET

    info = zf.getinfo(member)
    if info.file_size > OFFICE_MAX_MEMBER_BYTES:
        raise RuntimeError("%s is implausibly large" % member)
    with zf.open(member) as fh:
        text = fh.read(OFFICE_MAX_MEMBER_BYTES).decode(
            "utf-8", errors="replace"
        )

    def entity(match):
        name = match.group(1)
        if name in ("amp;", "lt;", "gt;", "quot;", "apos;"):
            return match.group(0)  # XML knows these five
        value = html5.get(name)
        return value if value is not None else ""

    text = re.sub(r"&([a-zA-Z][a-zA-Z0-9]{1,31};)", entity, text)
    # A DOCTYPE pointing at an external DTD is common and harmless, but ET
    # will not fetch it — and must not be asked to, in here.
    return ET.fromstring(text)


def _epub_chunks(zf, member: str, images: dict, wanted: set,
                 headings: dict) -> list:
    """[(anchor, html), ...] for one chapter.

    Normally one chunk per file, anchored on the file itself. A book that
    keeps several chapters in one XHTML document and points its table of
    contents at fragments inside it gets one chunk per referenced id, so
    those entries still land on the page they name rather than at the top of
    the file.
    """
    import posixpath

    root = _epub_dom(zf, member)
    body = next((n for n in root.iter() if _tag(n) == "body"), root)
    base = posixpath.dirname(member)
    chunks, buf, current = [], [], member

    def flush(anchor: str):
        nonlocal current
        chunks.append((current, "".join(buf)))
        buf.clear()
        current = anchor

    def walk(node):
        tag = _tag(node)
        if tag in _EPUB_SKIP:
            return
        ident = node.get("id")
        if ident:
            key = "%s#%s" % (member, ident)
            if key in wanted and buf:
                flush(key)
        if tag == "img" or tag == "image":
            _epub_image(zf, base, node, images, buf)
            return
        open_tag, close_tag = _EPUB_TAGS.get(tag, ("", ""))
        buf.append(open_tag)
        if node.text:
            buf.append(_escape(node.text))
        if tag in _HEADINGS and member not in headings:
            text = " ".join("".join(node.itertext()).split())
            if text:
                headings[member] = text
        for child in node:
            walk(child)
            if child.tail:
                buf.append(_escape(child.tail))
        buf.append(close_tag)

    walk(body)
    flush("")
    return chunks


def _epub_image(zf, base: str, node, images: dict, buf: list):
    """Embed a chapter image, bounded in count and size."""
    href = (
        node.get("src")
        or node.get("{http://www.w3.org/1999/xlink}href")
        or node.get("href")
    )
    member = _zip_path(base, href or "")
    if not member or len(images) >= EPUB_MAX_IMAGES:
        return
    if member not in images and not _stash_image(zf, member, images):
        return
    buf.append("<img src='%s'>" % member)


# ---------------------------------------------------------------- markdown
# Qt parses Markdown itself (md4c, behind QTextDocument.setMarkdown), so a
# rendered preview needs no library and no HTML engine — but the parse is a
# parse, and it happens in here rather than in the daemon like every other
# one. What comes back is page images, so Markdown scrolls, caches and
# streams exactly like a PDF.
MARKDOWN_MAX_BYTES = 2 * 1024 * 1024


def markdown_pages(fd: int, name: str, page_w: int, max_pages: int,
                   start: int = 0, theme: str = DEFAULT_BOOK_THEME):
    """Yield (info, png_bytes) for a Markdown file, rendered as pages.

    info is {"count", "chapters"} and is the same dict on every page, like
    the EPUB path: the headings are a property of the layout that produced
    the pages, so the sidebar can be built from the first frame.
    """
    from PySide6.QtCore import QSizeF
    from PySide6.QtGui import QTextDocument

    with _rewound(fd) as fh:
        data = fh.read(MARKDOWN_MAX_BYTES)
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        raise RuntimeError("empty document")

    page_h = int(page_w * PAGE_RATIO)
    palette = book_theme(theme)
    doc = QTextDocument()
    doc.setDocumentMargin(page_w * 0.06)
    font = doc.defaultFont()
    font.setPointSize(11)
    doc.setDefaultFont(font)
    # The GitHub dialect is the one people actually write: tables, task
    # lists, strikethrough, fenced code.
    doc.setMarkdown(
        _strip_markdown_html(text),
        QTextDocument.MarkdownFeature.MarkdownDialectGitHub,
    )
    _theme_markdown(doc, palette)
    _label_markdown_images(doc, palette)
    doc.setPageSize(QSizeF(page_w, page_h))
    count = min(doc.pageCount(), max_pages)
    info = {"count": count, "chapters": _markdown_headings(doc, page_h, count)}
    for _n, png in _document_pages(doc, page_w, page_h, count, start,
                                   palette["bg"]):
        yield info, png


def _markdown_headings(doc, page_h: int, count: int) -> list:
    """The document's headings as sidebar entries, with page numbers.

    A Markdown file has no table of contents of its own — its headings are
    the table of contents, which is what every reader that shows one does
    with them. Levels come straight from the hashes, so ## sits under #.
    """
    layout = doc.documentLayout()
    out = []
    block = doc.begin()
    while block.isValid() and len(out) < EPUB_MAX_TOC:
        level = block.blockFormat().headingLevel()
        title = " ".join(block.text().split())
        if level and title:
            top = layout.blockBoundingRect(block).top()
            page = int(top // page_h)
            if 0 <= page < count:
                out.append({
                    "title": title[:OUTLINE_MAX_TITLE],
                    "level": min(level - 1, 5),  # "#" is the top level
                    "page": page,
                    # Where on the page, not just which page: a page is
                    # taller than the window, so a heading halfway down one
                    # must scroll halfway down it.
                    "y": int(top - page * page_h),
                })
        block = block.next()
    return out


def _strip_markdown_html(text: str) -> str:
    """Remove raw HTML tags, outside code, before Qt parses the document.

    Qt hands an HTML block to its own rich-text importer mid-parse, and an
    unbalanced one — a `<p align="center">` wrapping two `<img>` tags is the
    canonical README opening — swallows the rest of the file: this project's
    own README imported as 2,420 characters of the 22,222 it has, with every
    paragraph after the block silently gone. Dropping the tags costs the
    badges and centred images (which could not be loaded in here anyway,
    see _label_markdown_images) and keeps the document.

    Fenced blocks and inline code are left alone: a README that documents
    HTML should still show it.
    """
    import re

    tag = re.compile(r"<[^>\n]{0,300}>")
    out, fence = [], None
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence is not None:
            out.append(line)
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            out.append(line)
            continue
        # Odd-numbered segments are inside `backticks` and keep their text.
        parts = line.split("`")
        out.append("`".join(
            part if index % 2 else tag.sub("", part)
            for index, part in enumerate(parts)
        ))
    return "\n".join(out)


def _theme_markdown(doc, palette: dict):
    """Recolour a parsed Markdown document to the page palette.

    setMarkdown builds the document directly rather than through the CSS
    engine, so a default style sheet does not reach it — the colours have to
    be merged onto the text afterwards. Without this the document renders in
    whatever the worker's Qt palette happens to be, which on a dark page is
    black text on a dark background.
    """
    from PySide6.QtGui import (
        QColor, QTextBlockFormat, QTextCharFormat, QTextCursor,
        QTextFrameFormat, QTextLength,
    )

    body = QTextCharFormat()
    body.setForeground(QColor(palette["fg"]))
    everything = QTextCursor(doc)
    everything.select(QTextCursor.SelectionType.Document)
    everything.mergeCharFormat(body)

    code_bg = _mix(palette["bg"], palette["muted"], 0.18)
    block = doc.begin()
    while block.isValid():
        fmt = block.blockFormat()
        whole = QTextCursor(block)
        whole.select(QTextCursor.SelectionType.BlockUnderCursor)
        if fmt.nonBreakableLines():
            # Fenced code is imported as unbreakable lines, which a page
            # cannot scroll sideways to show: a long line is simply cut off
            # at the margin. Wrapping it reads slightly wrong; losing half
            # of it reads as a bug.
            wrapped = QTextBlockFormat(fmt)
            wrapped.setNonBreakableLines(False)
            QTextCursor(block).setBlockFormat(wrapped)
        if fmt.headingLevel():
            heading = QTextCharFormat()
            heading.setForeground(QColor(palette["head"]))
            whole.mergeCharFormat(heading)
        elif fmt.leftMargin() > 0 and block.textList() is None:
            # An indented block that is not a list item is a blockquote —
            # the importer marks it with a margin and nothing else.
            quote = QTextCharFormat()
            quote.setForeground(QColor(palette["muted"]))
            quote.setFontItalic(True)
            whole.mergeCharFormat(quote)
        for fragment in _fragments(block):
            char = fragment.charFormat()
            span = QTextCursor(doc)
            span.setPosition(fragment.position())
            span.setPosition(
                fragment.position() + fragment.length(),
                QTextCursor.MoveMode.KeepAnchor,
            )
            if char.isAnchor():
                # Links keep their underline but take the page's accent:
                # Qt's default blue is unreadable on half of these palettes.
                link = QTextCharFormat()
                link.setForeground(QColor(palette["head"]))
                link.setFontUnderline(True)
                span.mergeCharFormat(link)
            elif char.fontFixedPitch():
                code = QTextCharFormat()
                code.setBackground(code_bg)
                span.mergeCharFormat(code)
        block = block.next()

    # Markdown tables arrive with no width of their own and collapse to
    # whatever their content forces, which is unreadable at any page size.
    for frame in doc.rootFrame().childFrames():
        if not hasattr(frame, "columns"):
            continue  # not a table
        table = frame.format().toTableFormat()
        table.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
        table.setCellPadding(6)
        table.setCellSpacing(0)
        table.setBorder(1)
        table.setBorderBrush(QColor(palette["muted"]))
        table.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
        frame.setFormat(table)


def _label_markdown_images(doc, palette: dict):
    """Replace every image with a note naming it.

    A Markdown file's images sit next to it on disk, and the jail has no
    disk — the file arrives as a descriptor and nothing else. Qt would
    resolve those relative paths against the worker's working directory,
    which means an image loads only when the document happens to live
    inside this application's own folder: right for the project's README,
    wrong for every file a person actually previews. Naming the image is
    the honest version of that, and it is the same everywhere.

    Applied after the recolouring above, whose select-all would otherwise
    take the colour back off these labels.
    """
    from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor

    found = []
    block = doc.begin()
    while block.isValid():
        for fragment in _fragments(block):
            if fragment.charFormat().isImageFormat():
                name = fragment.charFormat().toImageFormat().name() or ""
                found.append((fragment.position(), fragment.length(), name))
        block = block.next()

    label = QTextCharFormat()
    label.setForeground(QColor(palette["muted"]))
    label.setFontItalic(True)
    # Back to front: every replacement moves the positions after it.
    for position, length, name in reversed(found):
        cursor = QTextCursor(doc)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(
            "[image: %s]" % (name.rsplit("/", 1)[-1] or "unnamed"), label
        )


def _fragments(block) -> list:
    """The text fragments of one block, as a list (the iterator is a C++
    one and does not survive the formatting done to what it points at)."""
    out = []
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        if fragment.isValid():
            out.append(fragment)
        it += 1
    return out


def _mix(base: str, other: str, ratio: float):
    """base blended ratio-of-the-way towards other."""
    from PySide6.QtGui import QColor

    first, second = QColor(base), QColor(other)
    return QColor(
        round(first.red() * (1 - ratio) + second.red() * ratio),
        round(first.green() * (1 - ratio) + second.green() * ratio),
        round(first.blue() * (1 - ratio) + second.blue() * ratio),
    )
