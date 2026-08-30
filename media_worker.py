#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.
"""Jailed audio/video player for QuickView.

Usage: media_worker.py <socket-fd>

This is the process that made "sandbox everything" possible for media. The
daemon used to run QMediaPlayer itself, which meant FFmpeg demuxed and
decoded untrusted files inside the long-lived process that owns the window,
the socket and the cache.

Here, the whole pipeline runs in the jail:

  * the file arrives as a descriptor (SCM_RIGHTS) — the jail has no path to
    it, and no access to the user's filesystem at all;
  * audio goes straight out through PipeWire, whose socket is the one extra
    thing bound into this jail. Because the worker owns the audio clock, Qt
    does A/V sync in here exactly as it did in the daemon — the daemon never
    has to reconstruct it;
  * video frames are written into a memfd the daemon created and shared, and
    the daemon just blits them.

Wire protocol on the socket, both directions: 4-byte big-endian length +
JSON. The daemon sends {"op": "media", ...} once (with the media fd and the
frame memfd attached), then control messages ("play", "pause", "seek",
"volume", "ack"). This side replies with "meta", "frame", "position", "eof"
and "error" messages.
"""

import array
import json
import mmap
import os
import socket
import struct
import sys

# Two slots, alternating: the daemon reads the one just announced while this
# side fills the other, so a preview never shows a half-written frame. A
# slot is only reused once the daemon has acknowledged copying it out —
# without that, a daemon busy laying out a window is handed a slot whose
# pixels have already been replaced, and frames arrive out of order.
SLOTS = 2
POSITION_INTERVAL_MS = 200


def send(sock: socket.socket, msg: dict):
    payload = json.dumps(msg).encode()
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def recv_with_fds(sock: socket.socket, want_fds: int) -> tuple[dict, list]:
    fds = array.array("i")
    chunk, anc, _flags, _addr = sock.recvmsg(4, socket.CMSG_SPACE(4 * want_fds))
    for level, type_, data in anc:
        if level == socket.SOL_SOCKET and type_ == socket.SCM_RIGHTS:
            fds.frombytes(data[: len(data) - (len(data) % fds.itemsize)])
    if len(chunk) < 4:
        raise RuntimeError("truncated request")
    (size,) = struct.unpack(">I", chunk)
    body = b""
    while len(body) < size:
        part = sock.recv(size - len(body))
        if not part:
            raise RuntimeError("truncated request body")
        body += part
    return json.loads(body), list(fds)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    sock = socket.socket(fileno=int(sys.argv[1]))

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QFile, QIODevice, QSocketNotifier, Qt
    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

    app = QGuiApplication([sys.argv[0]])

    job, fds = recv_with_fds(sock, 2)
    if len(fds) < 2:
        send(sock, {"t": "error", "error": "missing descriptors"})
        return 1
    media_fd, frame_fd = fds[0], fds[1]
    max_w, max_h = job["max_w"], job["max_h"]
    slot_bytes = job["slot_bytes"]

    frames = mmap.mmap(frame_fd, slot_bytes * SLOTS)
    src = QFile()
    if not src.open(media_fd, QIODevice.OpenModeFlag.ReadOnly):
        send(sock, {"t": "error", "error": "cannot open media fd"})
        return 1

    player = QMediaPlayer()
    audio = QAudioOutput()
    player.setAudioOutput(audio)
    sink = QVideoSink()
    player.setVideoOutput(sink)

    state = {"free": list(range(SLOTS)), "sent_position": -POSITION_INTERVAL_MS}

    def safe_send(msg: dict) -> bool:
        """Send, treating a closed socket as "the preview is gone"."""
        try:
            send(sock, msg)
            return True
        except OSError:
            app.quit()  # daemon dismissed the preview and closed its end
            return False

    def on_frame(frame):
        if not frame.isValid():
            return
        if not state["free"]:
            return  # daemon is behind; drop this frame rather than race it
        img = frame.toImage()
        if img.isNull():
            return
        if img.width() > max_w or img.height() > max_h:
            img = img.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # One canonical layout so the daemon can wrap the bytes without
        # guessing: 32-bit RGB, tightly packed.
        img = img.convertToFormat(QImage.Format.Format_RGB32)
        stride = img.bytesPerLine()
        need = stride * img.height()
        if need > slot_bytes:
            return  # shouldn't happen: the daemon sized for max_w x max_h
        slot = state["free"].pop(0)
        frames.seek(slot * slot_bytes)
        frames.write(img.constBits().tobytes()[:need])
        if not safe_send({
            "t": "frame", "slot": slot, "w": img.width(),
            "h": img.height(), "stride": stride,
        }):
            state["free"].append(slot)

    sink.videoFrameChanged.connect(on_frame)

    def on_status(status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            safe_send({"t": "eof"})
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            safe_send({"t": "error", "error": "unsupported or corrupt media"})
            app.quit()

    def on_position(pos: int):
        # Qt6 dropped setNotifyInterval, so the rate is thrown here: the
        # daemon only draws a slider, and position updates every few ms are
        # a message per frame for nothing. A seek (backwards, or a jump)
        # always goes through.
        last = state["sent_position"]
        if last <= pos < last + POSITION_INTERVAL_MS:
            return
        state["sent_position"] = pos
        safe_send({"t": "position", "position": pos})

    player.mediaStatusChanged.connect(on_status)
    player.durationChanged.connect(
        lambda d: safe_send({"t": "meta", "duration": d})
    )
    player.positionChanged.connect(on_position)
    player.errorOccurred.connect(
        lambda _e, msg: safe_send({"t": "error", "error": msg})
    )

    # Control messages from the daemon, on the same socket.
    buf = bytearray()

    def on_control():
        try:
            chunk = sock.recv(1 << 16)
        except OSError:
            app.quit()
            return
        if not chunk:
            app.quit()
            return
        buf.extend(chunk)
        while len(buf) >= 4:
            (n,) = struct.unpack(">I", buf[:4])
            if len(buf) < 4 + n:
                return
            msg = json.loads(bytes(buf[4:4 + n]))
            del buf[:4 + n]
            op = msg.get("t")
            if op == "play":
                player.play()
            elif op == "pause":
                player.pause()
            elif op == "seek":
                player.setPosition(int(msg["position"]))
            elif op == "volume":
                audio.setVolume(float(msg["volume"]))
            elif op == "mute":
                # setMuted, not volume 0: the volume the user picked is
                # still there when they unmute.
                audio.setMuted(bool(msg.get("muted")))
            elif op == "rate":
                # Clamped here rather than trusted: this is the untrusted
                # side of the socket only in principle, but a rate of 0
                # stalls the player and a huge one spins the decoder.
                rate = float(msg.get("rate", 1.0))
                player.setPlaybackRate(min(max(rate, 0.1), 4.0))
            elif op == "ack":
                # The daemon has copied that slot out; it can be filled
                # again now, and not one frame earlier.
                slot = msg.get("slot")
                if isinstance(slot, int) and 0 <= slot < SLOTS:
                    if slot not in state["free"]:
                        state["free"].append(slot)
            elif op == "quit":
                app.quit()

    notifier = QSocketNotifier(sock.fileno(), QSocketNotifier.Type.Read)
    notifier.activated.connect(on_control)

    player.setSourceDevice(src)
    player.play()
    rc = app.exec()
    player.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
