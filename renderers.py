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

    PNG whenever the image carries alpha, because JPEG has none and a
    transparent logo would come back on a black square. Qt writes the
    "QuickView:OrigSize" tEXt chunk into a JPEG comment marker, so the
    dimensions the cache-hit path reads survive either way.
    """
    if img.hasAlphaChannel():
        return _encode(img)
    return _encode(img, "JPG", JPEG_QUALITY)


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
            for index in range(max(start, 0), count):
                page = QImage(page_w, page_h, QImage.Format.Format_RGB32)
                page.fill(Qt.GlobalColor.white)
                painter = QPainter(page)
                painter.translate(0, -index * page_h)
                painter.setClipRect(QRectF(0, index * page_h, page_w, page_h))
                doc.drawContents(
                    painter, QRectF(0, index * page_h, page_w, page_h)
                )
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
