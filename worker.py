#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.
"""Warm, jailed preview worker for QuickView.

Usage: worker.py <socket-fd>

The daemon spawns this inside bubblewrap *before* it is needed, so PySide6
is already imported by the time a file shows up — that import, not the jail
(~3 ms), is what made the old spawn-per-file helpers cost ~150 ms each.

Because the file arrives as a file descriptor over SCM_RIGHTS, the jail
needs no access to the user's filesystem at all: nothing but /usr, this app
dir and the fonts are visible in here.

Wire protocol on the socket, both directions framed as 4-byte big-endian
length + payload:

    daemon -> worker   one JSON request {"op": ..., ...}, with the target
                       file's fd attached as ancillary data

The "text" op is the one that answers with JSON rather than pixels: the file's
text plus the colour spans of its syntax (see renderers.highlight_text).
    worker -> daemon   one JSON header {"ok": true, "count": N} (or
                       {"ok": false, "error": "..."}), then N frames, each
                       raw PNG bytes, each flushed as it is produced, then
                       a zero-length frame as the end-of-stream marker

The marker is what tells the daemon a stream ended because it was finished
rather than because the worker died: once the header has gone out there is
no way to take it back, so a failure after that point closes the socket
without a marker and explains itself on stderr (which the daemon collects).

One worker handles exactly one file and then exits: that keeps per-file
isolation identical to the old throwaway helpers while the daemon hides the
startup cost behind a pre-spawned spare.
"""

import array
import json
import os
import socket
import struct
import sys

import renderers

# Animation bounds — a GIF header may claim far more frames than it has.
ANIM_MAX_FRAMES = 512
ANIM_MAX_BYTES = 256 * 1024 * 1024


def send(sock: socket.socket, payload: bytes):
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def recv_request(sock: socket.socket) -> tuple[dict, int]:
    """Read the one request, returning (job, fd). Raises on a short read."""
    fds = array.array("i")
    header = b""
    # The fd rides on the first recvmsg; the JSON may need more reads.
    chunk, anc, _flags, _addr = sock.recvmsg(4, socket.CMSG_SPACE(4))
    for level, type_, data in anc:
        if level == socket.SOL_SOCKET and type_ == socket.SCM_RIGHTS:
            fds.frombytes(data[: len(data) - (len(data) % fds.itemsize)])
    if len(chunk) < 4 or not fds:
        raise RuntimeError("truncated request")
    (size,) = struct.unpack(">I", chunk)
    while len(header) < size:
        part = sock.recv(size - len(header))
        if not part:
            raise RuntimeError("truncated request body")
        header += part
    return json.loads(header), fds[0]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    sock = socket.socket(fileno=int(sys.argv[1]))

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Imported before the socket read so the whole Qt boot happens while the
    # worker is still a spare — the point of the pool.
    from PySide6.QtCore import QFile, QIODevice
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([sys.argv[0]])  # noqa: F841 — the parsers need it

    job, fd = recv_request(sock)
    src = QFile()
    if not src.open(fd, QIODevice.OpenModeFlag.ReadOnly):
        send(sock, json.dumps({"ok": False, "error": "cannot open fd"}).encode())
        return 1

    op = job.get("op")
    state = {"header": False}

    def header(count: int):
        state["header"] = True
        send(sock, json.dumps({"ok": True, "count": count}).encode())

    try:
        if op == "image":
            png = renderers.render_image(src, job["max_w"], job["max_h"])
            header(1)
            send(sock, png)
        elif op == "pdf":
            first = True
            for count, png in renderers.render_pdf(
                src, job["page_w"], job["max_pages"], job.get("start", 0)
            ):
                if first:
                    header(count)
                    first = False
                send(sock, png)
            if first:  # a zero-page document
                header(0)
        elif op == "text":
            doc = renderers.highlight_text(
                src, job.get("name", ""), job["limit"],
                job.get("style", "one-dark"),
            )
            header(1)
            send(sock, json.dumps(doc).encode())
        elif op == "archive":
            # Takes the raw descriptor, not the QFile: zipfile/tarfile want a
            # Python file object and the external listers read /dev/fd.
            header(1)
            send(sock, json.dumps(
                renderers.list_archive(fd, job.get("name", ""))
            ).encode())
        elif op == "office":
            # Two shapes of answer, and the header says which: laid-out
            # page images when the document can be rendered, or a JSON
            # payload (embedded thumbnail plus text) when it cannot, which
            # is what happens for a slide deck.
            first = True
            try:
                for count, png in renderers.office_pages(
                    fd, job.get("name", ""), job["page_w"],
                    job["max_pages"], job.get("start", 0),
                ):
                    if first:
                        state["header"] = True
                        send(sock, json.dumps(
                            {"ok": True, "count": count, "kind": "pages"}
                        ).encode())
                        first = False
                    send(sock, png)
            except Exception as exc:
                if not first:
                    raise  # already streaming: the trailer's absence reports it
                print("office_pages: %s" % exc, file=sys.stderr)
            if first:  # nothing rendered — fall back to thumbnail and text
                doc = renderers.office_preview(
                    fd, job.get("name", ""), job["limit"]
                )
                state["header"] = True
                send(sock, json.dumps(
                    {"ok": True, "count": 1, "kind": "doc"}
                ).encode())
                send(sock, json.dumps(doc).encode())
        elif op == "anim":
            first = True
            for count, delay, png in renderers.render_anim(
                src, job["max_w"], job["max_h"], ANIM_MAX_FRAMES, ANIM_MAX_BYTES
            ):
                if first:
                    header(count)
                    first = False
                # Frame delay rides in front of the PNG: 4 bytes, then bytes.
                send(sock, struct.pack(">I", delay) + png)
            if first:
                raise RuntimeError("no frames decoded")
        else:
            raise RuntimeError(f"unknown op: {op!r}")
        send(sock, b"")  # end of stream: everything above got through
    except Exception as exc:  # a hostile file must not look like a crash
        if state["header"]:
            # Too late for {"ok": false}: the daemon has taken the header
            # and reads anything further as a frame. Closing without the
            # marker is what tells it this stream is truncated.
            print(exc, file=sys.stderr)
        else:
            try:
                send(sock, json.dumps({"ok": False, "error": str(exc)}).encode())
            except OSError:
                pass  # daemon moved on and closed the socket
        return 1
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
