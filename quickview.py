#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.
"""QuickView — a macOS Quick Look style previewer for KDE.

Usage: quickview <file> [file ...]
       quickview --daemon       run resident (systemd user service does this)
       quickview --clear-cache  empty the disk preview cache

Keys:
  Space / Esc / Q   close the preview
  Left / Right      previous / next file (the selection if several files
                    were passed, otherwise siblings in the same folder)
  Enter             open the file in its default application

A second invocation while a preview is open is forwarded to the running
instance: same file toggles the window closed (like Quick Look), a
different file switches the preview.
"""

import array
import base64
import faulthandler
import hashlib
import html
import json
import logging
import logging.handlers
import mmap
import os
import shutil
import socket
import struct
import subprocess
import sys
from collections import OrderedDict

import ipc

from PySide6.QtCore import (
    Qt, QUrl, QPoint, QSize, QObject, QSocketNotifier, QThreadPool,
    QTimer, QFileInfo, QMimeDatabase, QStandardPaths, Signal,
)
from PySide6.QtGui import (
    QAction, QFont, QGuiApplication, QIcon, QImage, QKeySequence, QRegion,
    QPainter, QPixmap, QShortcut, QColor, QDesktopServices,
    QTextCharFormat, QTextCursor,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QFileIconProvider, QFrame, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QStackedLayout, QVBoxLayout, QWidget, QGraphicsDropShadowEffect,
)

SOCKET_PATH = ipc.socket_path()
TEXT_PREVIEW_LIMIT = 1024 * 1024  # 1 MiB
# Any Pygments style name: dracula, gruvbox-dark, nord, monokai, native…
# one-dark's own background (#282C34) is a shade off the panel's #222226,
# so its palette sits in this panel without recolouring anything.
CODE_STYLE = os.environ.get("QUICKVIEW_CODE_STYLE", "one-dark")

# Listed by container, not by application: a preview of any of these is a
# listing of what is inside, never an extraction.
ARCHIVE_MIMES = (
    "application/zip", "application/vnd.rar", "application/x-7z-compressed",
    "application/x-compressed-tar", "application/x-tar", "application/gzip",
    "application/x-xz-compressed-tar", "application/x-bzip-compressed-tar",
    "application/vnd.debian.binary-package", "application/x-cd-image",
)
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".tgz", ".txz", ".tbz2"}
# OOXML and ODF documents that paginate: zip containers full of XML, laid
# out with the standard library alone. Slide decks are deliberately absent —
# their content is absolutely positioned graphics that nothing here can lay
# out, and half a preview is worse than the honest metadata card. The legacy
# binary formats (.doc/.xls/.ppt) are absent for the same reason.
OFFICE_MIMES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
)

TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".log", ".ini", ".cfg", ".conf", ".toml",
    ".yaml", ".yml", ".json", ".xml", ".html", ".htm", ".css", ".js",
    ".ts", ".py", ".r", ".sh", ".bash", ".zsh", ".c", ".h", ".cpp",
    ".hpp", ".rs", ".go", ".java", ".kt", ".rb", ".pl", ".lua", ".sql",
    ".csv", ".tsv", ".tex", ".bib", ".desktop", ".service", ".env",
}


APP_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_HELPER = os.path.join(APP_DIR, "worker.py")
# Cap on rendered pages so a 2000-page (or hostile) PDF can't grind the
# helper for minutes; the title says when the preview is truncated.
PDF_MAX_PAGES = 50

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "quickview", "previews",
)
CACHE_CAP_BYTES = 256 * 1024 * 1024
# The memory tier holds screen-sized pixmaps (~25 MB each on a 4K display),
# so it must be bounded by bytes, not entry count, in a process that never
# exits.
MEM_CACHE_BYTES = 96 * 1024 * 1024

DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "quickview",
)
LOG_FILE = os.path.join(DATA_DIR, "quickview.log")
LOG_MAX_BYTES = 5 * 1024 * 1024

PNG_MAGIC = b"\x89PNG"
# Sanity bound on one frame of a helper's stdout stream. A screen-sized PNG
# page is a couple of MB; anything past this is a broken or hostile helper.
MAX_FRAME_BYTES = 64 * 1024 * 1024
# Frame slots the media worker cycles through in shared memory.
MEDIA_SLOTS = 2
# The worker caps frames too, but it is the untrusted side of the socket:
# 512 screen-sized pixmaps is gigabytes of daemon memory, so the frames a
# preview will actually hold are bounded here as well.
# Formats show_file() treats as animations by default: a still WebP would
# otherwise pay a decode round trip to learn it has one frame.
ANIM_MIMES = ("image/gif", "image/apng")
ANIM_MAX_FRAMES = 512
ANIM_MAX_PIXMAP_BYTES = 256 * 1024 * 1024

log = logging.getLogger("quickview")


def setup_logging():
    os.makedirs(DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_h = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=1
    )
    file_h.setFormatter(fmt)
    err_h = logging.StreamHandler()
    err_h.setFormatter(fmt)
    log.addHandler(file_h)
    log.addHandler(err_h)
    log.setLevel(logging.DEBUG)

    # Native crashes (a segfault inside a Qt decoder, etc.) can't be caught
    # by Python's exception machinery — faulthandler dumps a traceback to
    # crash.log on SIGSEGV/SIGABRT/SIGBUS/SIGFPE/SIGILL before exiting.
    crash_fh = open(os.path.join(DATA_DIR, "crash.log"), "a")
    faulthandler.enable(file=crash_fh, all_threads=True)
    sys.excepthook = lambda *exc: log.critical(
        "unhandled exception", exc_info=exc
    )


# ------------------------------------------------------------------ cache
# Two tiers, like macOS quicklookd: decoded pixmaps stay in the daemon's
# memory (see QuickView._mem_cache), and rendered PNGs persist on disk keyed
# by path + mtime + size, so a changed file re-renders and a repeat view of
# an unchanged one skips decoding entirely.

def cache_key(
    path: str, st: os.stat_result, max_w: int, max_h: int, variant: str = ""
) -> str:
    raw = f"{path}\0{st.st_mtime_ns}\0{st.st_size}\0{max_w}x{max_h}"
    if variant:  # e.g. "pdf3v2" — page 3 of a PDF at this width
        raw += f"\0{variant}"
    return hashlib.sha256(raw.encode()).hexdigest() + ".png"


def png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG's IHDR, without decoding the image."""
    if len(data) < 24 or data[:4] != PNG_MAGIC or data[12:16] != b"IHDR":
        return None
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return (w, h) if w > 0 and h > 0 else None


def cache_read_head(key: str, n: int = 24) -> bytes | None:
    """The first n bytes of a cached file — enough for a PNG's IHDR."""
    try:
        with open(os.path.join(CACHE_DIR, key), "rb") as fh:
            return fh.read(n)
    except OSError:
        return None


def cache_read(key: str) -> bytes | None:
    fp = os.path.join(CACHE_DIR, key)
    try:
        with open(fp, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    try:
        os.utime(fp)  # freshen so the pruner drops oldest-viewed first
    except OSError:
        pass  # the data is already read — a failed freshen is no reason to drop it
    return data


def cache_remove(key: str):
    try:
        os.unlink(os.path.join(CACHE_DIR, key))
    except OSError:
        pass


# Rescanning the whole cache dir on every write is O(entries); amortize by
# pruning only after enough new bytes have accumulated to matter. Starting
# at the threshold forces one prune on the first write of a session, which
# also cleans up an over-cap cache left behind by a previous run.
_PRUNE_EVERY_BYTES = CACHE_CAP_BYTES // 8
_unpruned_bytes = _PRUNE_EVERY_BYTES


def cache_write(key: str, png: bytes):
    global _unpruned_bytes
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = os.path.join(CACHE_DIR, f".{key}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, os.path.join(CACHE_DIR, key))
    except OSError as exc:
        log.warning("cache write failed: %s", exc)
        return
    _unpruned_bytes += len(png)
    # Reset the counter only after a successful prune — zeroing it before a
    # prune that fails would suppress the next attempt until another
    # _PRUNE_EVERY_BYTES of writes accumulates, leaving an over-cap cache
    # in place for hours on a cache-hit-heavy workload.
    if _unpruned_bytes >= _PRUNE_EVERY_BYTES and prune_cache():
        _unpruned_bytes = 0


def prune_cache() -> bool:
    entries, total = [], 0
    try:
        with os.scandir(CACHE_DIR) as it:
            for e in it:
                if e.is_file():
                    st = e.stat()
                    entries.append((st.st_mtime, st.st_size, e.path))
                    total += st.st_size
    except OSError as exc:
        log.warning("cache prune failed: %s", exc)
        return False
    entries.sort()
    for _mtime, size, fp in entries:
        if total <= CACHE_CAP_BYTES:
            break
        try:
            os.unlink(fp)
            total -= size
        except OSError:
            pass
    return True


def clear_cache():
    removed = 0
    try:
        with os.scandir(CACHE_DIR) as it:
            for e in it:
                if e.is_file():
                    os.unlink(e.path)
                    removed += 1
    except OSError:
        pass
    print(f"Cleared {removed} cached previews from {CACHE_DIR}")


# ---------------------------------------------------------------- sandbox

_warned_no_bwrap = False
_bwrap_path = ...  # ... = not looked up yet; None = not installed


def find_bwrap() -> str | None:
    """shutil.which("bwrap"), resolved once — every render asked before."""
    global _bwrap_path
    if _bwrap_path is ...:
        _bwrap_path = shutil.which("bwrap")
    return _bwrap_path


def _font_binds() -> list:
    """Read-only binds that let Qt find fonts *and* their prebuilt cache.

    Without these the jail has no /etc/fonts, so fontconfig rebuilds its
    index on every single spawn: measured 583 ms vs 148 ms for a one-page
    PDF. /etc/fonts alone is worse than neither (749 ms) — it turns on the
    scan without supplying the cache that makes it cheap — so the config
    is only bound when a cache is there to go with it.
    """
    # The jail runs with --clearenv and HOME=/tmp, so fontconfig looks for
    # a user cache in /tmp/.cache/fontconfig — binding ~/.cache/fontconfig
    # at its real path leaves it invisible in there, which is how a system
    # with no /var/cache/fontconfig ended up with /etc/fonts and no usable
    # cache: the 749 ms worst case above.
    caches = []
    if os.path.isdir("/var/cache/fontconfig"):
        caches.append(("/var/cache/fontconfig", "/var/cache/fontconfig"))
    user = os.path.expanduser("~/.cache/fontconfig")
    if os.path.isdir(user):
        caches.append((user, "/tmp/.cache/fontconfig"))
    if not caches or not os.path.isdir("/etc/fonts"):
        return []
    binds = ["--ro-bind", "/etc/fonts", "/etc/fonts"]
    for src, dest in caches:
        binds += ["--ro-bind", src, dest]
    return binds


def sandbox_flags(bwrap: str) -> list:
    """The jail every helper runs in: read-only /usr + this app dir, no
    network, no writes, no capabilities, its own everything."""
    return [
        bwrap,
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/bin", "/sbin",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--ro-bind", APP_DIR, APP_DIR,
        *_font_binds(),
        "--unshare-all",
        "--cap-drop", "ALL",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv", "QT_QPA_PLATFORM", "offscreen",
        "--setenv", "HOME", "/tmp",
        "--setenv", "XDG_RUNTIME_DIR", "/tmp",
    ]


# --------------------------------------------------------- worker pool
# The jail costs ~3 ms; importing PySide6 in the helper costs ~150 ms. So
# the helper is booted *before* it is needed and parked on a socket, and the
# file reaches it as a file descriptor (SCM_RIGHTS) rather than a bind
# mount — the jail then needs no access to the user's filesystem at all.
#
# Isolation is unchanged from the old throwaway helpers: a worker handles
# one file and exits. What changed is who waits for the boot — a spare, not
# the user.

WORKER_SPARES = 2
JOB_TIMEOUT_MS = 20000
MEDIA_HELPER = os.path.join(APP_DIR, "media_worker.py")


def build_worker_command(fd: int) -> list | None:
    """bwrap command for a warm worker, or None when we must refuse.

    Note what is *not* here: no --ro-bind of any user file. The worker sees
    /usr, this app dir and fonts, and gets its file as a descriptor.
    """
    helper = [sys.executable, WORKER_HELPER, str(fd)]
    bwrap = find_bwrap()
    if bwrap:
        return sandbox_flags(bwrap) + ["--"] + helper
    if os.environ.get("QUICKVIEW_ALLOW_UNSANDBOXED") == "1":
        return helper
    global _warned_no_bwrap
    if not _warned_no_bwrap:
        _warned_no_bwrap = True
        log.warning(
            "bwrap not found — refusing to decode untrusted files "
            "(install bubblewrap, or set QUICKVIEW_ALLOW_UNSANDBOXED=1 "
            "to accept the risk)"
        )
    return None


class Worker:
    """A booted, idle, jailed process waiting for its one job."""

    def __init__(self, proc, sock):
        self.proc = proc
        self.sock = sock
        self.err = bytearray()
        # Drained from the moment it is spawned, not from the moment it is
        # given a job: a spare that chatters during Qt's boot (a broken font
        # cache, QT_LOGGING_RULES) would otherwise fill the 64 KiB pipe
        # buffer and block before it ever reads its request.
        self._errnotifier = QSocketNotifier(
            proc.stderr.fileno(), QSocketNotifier.Type.Read
        )
        self._errnotifier.activated.connect(self._drain_err)

    def _drain_err(self):
        try:
            chunk = os.read(self.proc.stderr.fileno(), 4096)
        except OSError:
            self._errnotifier.setEnabled(False)
            return
        if not chunk:
            self._errnotifier.setEnabled(False)
            return
        # Bounded: a hostile file can make Qt chatter indefinitely and we
        # only ever log the first few hundred characters.
        if len(self.err) < 4096:
            self.err += chunk

    def kill(self):
        self._errnotifier.setEnabled(False)
        for close in (self.sock.close, self.proc.kill):
            try:
                close()
            except OSError:
                pass
        try:
            # Reap it. SIGKILL is already delivered, so this returns at once;
            # skipping the wait would leave one zombie per preview behind in
            # a daemon that runs for weeks.
            self.proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            log.warning("worker %d would not die", self.proc.pid)
        try:
            self.proc.stderr.close()
        except OSError:
            pass


class WorkerPool:
    """Keeps a couple of workers pre-booted so a preview never waits."""

    def __init__(self):
        self._spares = []

    def prime(self):
        """Top the pool back up. Cheap: fork+exec returns immediately and
        the Qt import happens in the child while the user reads."""
        while len(self._spares) < WORKER_SPARES:
            w = self._spawn()
            if w is None:
                return  # refused (no bwrap) — callers fall back
            self._spares.append(w)

    def take(self) -> Worker | None:
        while self._spares:
            w = self._spares.pop(0)
            if w.proc.poll() is None:  # still alive
                QTimer.singleShot(0, self.prime)
                return w
            log.debug("discarding dead spare worker")
            w.kill()
        w = self._spawn()  # pool was empty or stale — pay the boot cost
        QTimer.singleShot(0, self.prime)
        return w

    def shutdown(self):
        for w in self._spares:
            w.kill()
        self._spares.clear()

    def _spawn(self) -> Worker | None:
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        cmd = build_worker_command(child.fileno())
        if cmd is None:
            parent.close()
            child.close()
            return None
        try:
            os.set_inheritable(child.fileno(), True)
            proc = subprocess.Popen(
                cmd,
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            log.warning("worker spawn failed: %s", exc)
            parent.close()
            child.close()
            return None
        finally:
            child.close()
        return Worker(proc, parent)


def build_media_worker_command(fd: int) -> list | None:
    """Jail for the media player: the standard one plus an audio socket.

    Playing sound is the single capability the media worker has that the
    render workers don't. PipeWire (and its PulseAudio shim) are reached
    through sockets under the real XDG_RUNTIME_DIR, so they are bound into
    the jail's /tmp, which is where its XDG_RUNTIME_DIR points. Everything
    else — the filesystem, the network, the user's files — stays shut off.
    """
    helper = [sys.executable, MEDIA_HELPER, str(fd)]
    bwrap = find_bwrap()
    if not bwrap:
        if os.environ.get("QUICKVIEW_ALLOW_UNSANDBOXED") == "1":
            return helper
        return None
    run_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    audio = []
    for name in ("pipewire-0", "pulse"):
        src = os.path.join(run_dir, name)
        if os.path.exists(src):
            audio += ["--ro-bind", src, f"/tmp/{name}"]
    if os.path.isdir("/etc/pipewire"):
        audio += ["--ro-bind", "/etc/pipewire", "/etc/pipewire"]
    if not audio:
        log.warning("no PipeWire/PulseAudio socket found — preview is muted")
    return sandbox_flags(bwrap) + audio + ["--"] + helper


class MediaSession(QObject):
    """A jailed QMediaPlayer, driven over a socket.

    The daemon holds no decoder: it sends transport commands, receives
    status, and blits frames the worker has written into shared memory.
    """

    def __init__(self, window, path, max_w, max_h, handlers):
        super().__init__(window)
        self._handlers = handlers
        self._buf = bytearray()
        self._alive = False
        self._mm = None
        self.slot_bytes = max_w * max_h * 4

        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        cmd = build_media_worker_command(child.fileno())
        if cmd is None:
            parent.close()
            child.close()
            QTimer.singleShot(
                0, lambda: handlers["error"]("sandbox refused (no bwrap)")
            )
            return
        try:
            media_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as exc:
            parent.close()
            child.close()
            QTimer.singleShot(0, lambda: handlers["error"](str(exc)))
            return

        # A memfd is the frame transport: an anonymous shared buffer that
        # crosses the jail as a descriptor, so it works with --unshare-ipc
        # (SysV/POSIX shared memory would not).
        frame_fd = os.memfd_create("quickview-frames")
        os.ftruncate(frame_fd, self.slot_bytes * MEDIA_SLOTS)
        self._mm = mmap.mmap(frame_fd, self.slot_bytes * MEDIA_SLOTS)

        try:
            os.set_inheritable(child.fileno(), True)
            self._proc = subprocess.Popen(
                cmd,
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            for fd in (media_fd, frame_fd):
                os.close(fd)
            parent.close()
            child.close()
            QTimer.singleShot(0, lambda: handlers["error"](str(exc)))
            return
        finally:
            child.close()

        self._sock = parent
        self._alive = True
        self._notifier = QSocketNotifier(
            parent.fileno(), QSocketNotifier.Type.Read, self
        )
        self._notifier.activated.connect(self._readable)

        job = json.dumps({
            "op": "media", "max_w": max_w, "max_h": max_h,
            "slot_bytes": self.slot_bytes,
        }).encode()
        try:
            parent.sendmsg(
                [struct.pack(">I", len(job))],
                [(
                    socket.SOL_SOCKET, socket.SCM_RIGHTS,
                    array.array("i", [media_fd, frame_fd]),
                )],
            )
            parent.sendall(job)
        except OSError as exc:
            QTimer.singleShot(0, lambda: handlers["error"](str(exc)))
        finally:
            os.close(media_fd)
            os.close(frame_fd)  # the worker and our mmap keep it alive

    # ------------------------------------------------------------ control
    def send(self, msg: dict):
        if not self._alive:
            return
        payload = json.dumps(msg).encode()
        try:
            self._sock.sendall(struct.pack(">I", len(payload)) + payload)
        except OSError:
            self.stop()

    def stop(self):
        if not self._alive:
            return
        self._alive = False
        self._notifier.setEnabled(False)
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            self._proc.kill()
        except OSError:
            pass
        try:
            # Bounded, like Worker.kill(): SIGKILL is already delivered, so
            # this normally returns at once — but a worker wedged in an
            # uninterruptible read on a stalled mount must not take the
            # window, the shortcuts and the IPC socket down with it.
            self._proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            log.warning("media worker %d would not die", self._proc.pid)
        if self._mm is not None:
            self._mm.close()
            self._mm = None

    # ------------------------------------------------------------ reading
    def _readable(self):
        if not self._alive:
            return
        try:
            chunk = self._sock.recv(1 << 16)
        except OSError as exc:
            self._handlers["error"](str(exc))
            self.stop()
            return
        if not chunk:
            self.stop()
            self._handlers["error"]("player exited")
            return
        self._buf += chunk
        while len(self._buf) >= 4:
            (n,) = struct.unpack(">I", self._buf[:4])
            if n > MAX_FRAME_BYTES or len(self._buf) < 4 + n:
                if n > MAX_FRAME_BYTES:
                    self.stop()
                return
            try:
                msg = json.loads(bytes(self._buf[4:4 + n]))
            except ValueError:
                self.stop()
                return
            del self._buf[:4 + n]
            self._dispatch(msg)

    def _dispatch(self, msg: dict):
        kind = msg.get("t")
        if kind == "frame":
            img = self.read_frame(msg)
            if img is not None:
                self._handlers["frame"](img)
        elif kind in self._handlers:
            self._handlers[kind](msg)

    def read_frame(self, msg: dict):
        """Copy one frame out of shared memory as a QImage."""
        if self._mm is None:
            return None
        try:
            slot, w, h = int(msg["slot"]), int(msg["w"]), int(msg["h"])
            stride = int(msg["stride"])
        except (KeyError, TypeError, ValueError):
            return None
        if not 0 <= slot < MEDIA_SLOTS:
            return None  # no such slot to release either
        img = self._copy_frame(slot, w, h, stride)
        # The slot is free for the worker to fill again — either because
        # the pixels are copied out, or because the frame was rejected and
        # there is nothing left to preserve. Until this ack lands, the
        # worker holds off, so a daemon busy with layout can no longer be
        # handed a slot whose contents have already been replaced.
        self.send({"t": "ack", "slot": slot})
        return img

    def _copy_frame(self, slot: int, w: int, h: int, stride: int):
        # The worker is untrusted, so every number is checked before it
        # reaches QImage: a frame claiming w=100000, h=1, stride=4 passes a
        # size check on stride * h alone and then reads far past the slot.
        if w <= 0 or h <= 0:
            return None
        if stride < w * 4:  # Format_RGB32: four bytes a pixel, minimum
            return None
        need = stride * h
        if need <= 0 or need > self.slot_bytes:
            return None
        # Copied, not wrapped: the worker fills this slot again as soon as
        # it is acked, and QImage would still be pointing at it.
        off = slot * self.slot_bytes
        data = bytes(self._mm[off:off + need])
        if len(data) < need:  # a short mmap slice would be read past, too
            return None
        return QImage(data, w, h, stride, QImage.Format.Format_RGB32).copy()


class FileReader(QObject):
    """Reads a bounded slice of a file on a pool thread.

    Only used for the text preview, which parses nothing — but a file on a
    stalled network mount blocks just as hard as a hostile parser, and the
    daemon's socket has to keep answering.

    Deliberately not a QRunnable: the pool touches a runnable again *after*
    run() returns (to check autoDelete), so a reader whose last reference
    was dropped by the daemon in the meantime would be a use-after-free.
    Handing the pool a bound method instead leaves the lifetime to Python —
    the pool's own wrapper holds this object until run() has finished.
    """

    done = Signal(bytes, str)

    def __init__(self, path: str, limit: int):
        super().__init__()
        self._path = path
        self._limit = limit

    def run(self):
        try:
            with open(self._path, "rb") as fh:
                self.done.emit(fh.read(self._limit), "")
        except OSError as exc:
            self.done.emit(b"", str(exc))


class SandboxJob(QObject):
    """One file parsed by one jailed worker, asynchronously.

    on_frame(payload) fires per frame as it arrives (so PDF page 1 shows
    while page 2 renders); on_done(ok, error) fires exactly once. Frames are
    length-prefixed the same way the standalone helpers stream them.
    """

    def __init__(self, pool, path, job, on_frame, on_done, parent=None,
                 timeout_ms: int = JOB_TIMEOUT_MS):
        super().__init__(parent)
        self._on_frame = on_frame
        self._on_done = on_done
        self._buf = bytearray()
        self.header = {}  # the worker's reply header: {"ok": ..., "count": N}
        self._header = None
        self._err = bytearray()
        self._done = False
        # Set by the worker's end-of-stream marker. Without it, a worker
        # that dies halfway looks exactly like one that finished.
        self._complete = False
        self._worker = pool.take()
        if self._worker is None:
            QTimer.singleShot(0, lambda: self._finish(False, "sandbox refused"))
            return

        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as exc:
            self._worker.kill()
            self._worker = None
            QTimer.singleShot(0, lambda: self._finish(False, str(exc)))
            return

        sock = self._worker.sock
        self._notifier = QSocketNotifier(
            sock.fileno(), QSocketNotifier.Type.Read, self
        )
        self._notifier.activated.connect(self._readable)
        # stderr is drained by the Worker itself, from spawn time.
        # Inactivity watchdog, not a total budget: a 50-page PDF arrives over
        # several seconds, but each frame should come quickly.
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(
            lambda: self._finish(False, "timed out")
        )
        self._timeout_ms = timeout_ms
        self._watchdog.start(timeout_ms)

        payload = json.dumps(job).encode()
        try:
            sock.sendmsg(
                [struct.pack(">I", len(payload))],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
            )
            sock.sendall(payload)
        except OSError as exc:
            QTimer.singleShot(0, lambda: self._finish(False, str(exc)))
        finally:
            os.close(fd)  # the worker holds its own copy now

    # ------------------------------------------------------------ reading
    def _readable(self):
        if self._done:
            return
        try:
            chunk = self._worker.sock.recv(1 << 16)
        except OSError as exc:
            self._finish(False, str(exc))
            return
        if not chunk:  # worker exited
            if self._complete:
                self._finish(True)
            elif self._header is None:
                self._finish(False, "worker closed the socket")
            else:
                # Frames arrived but the end marker never did: the worker
                # died partway (a page it could not render, a kill). Saying
                # "ok" here is what let a PDF that stopped at page 7 of 50
                # be reported as a complete render.
                self._finish(False, "worker stopped before finishing")
            return
        self._watchdog.start(self._timeout_ms)
        self._buf += chunk
        while len(self._buf) >= 4:
            (n,) = struct.unpack(">I", self._buf[:4])
            if n > MAX_FRAME_BYTES:
                self._finish(False, f"absurd frame length {n}")
                return
            if len(self._buf) < 4 + n:
                return
            payload = bytes(self._buf[4:4 + n])
            del self._buf[:4 + n]
            if self._header is not None and n == 0:
                self._complete = True  # end-of-stream marker
                continue
            if self._header is None:
                try:
                    self._header = json.loads(payload)
                except ValueError:
                    self._finish(False, "malformed worker header")
                    return
                self.header = self._header
                if not self._header.get("ok"):
                    self._finish(False, self._header.get("error", "failed"))
                    return
                continue
            self._on_frame(payload)

    def _finish(self, ok: bool, error: str = ""):
        if self._done:
            return
        self._done = True
        self.cancel()
        if not ok and self._err:
            error = f"{error}: " + self._err.decode(
                "utf-8", errors="replace"
            ).strip()[:500]
        self._on_done(ok, error)
        # One job object per preview *and* per prefetch would otherwise pile
        # up as children of the window for the life of a daemon that runs
        # for weeks. Queued, so it outlives the callback above.
        self.deleteLater()

    def cancel(self):
        """Stop listening and kill the worker. Safe to call twice."""
        if self._done and self._worker is None:
            return  # already torn down; the C++ side may be gone with it
        self._done = True
        for attr in ("_notifier", "_watchdog"):
            obj = getattr(self, attr, None)
            if obj is not None:
                obj.setEnabled(False) if isinstance(
                    obj, QSocketNotifier
                ) else obj.stop()
        if self._worker is not None:
            self._err = self._worker.err  # collected since it was spawned
            self._worker.kill()
            self._worker = None
        # Also covers a job cancelled from outside, which never reaches
        # _finish. deleteLater() twice is harmless.
        self.deleteLater()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


class TitleBar(QWidget):
    """Quick Look style header: close button left, centered file name."""

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._drag_from = None    # cursor position where a drag started
        self._drag_origin = None  # panel position at that moment
        self.setFixedHeight(40)

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("closeBtn")
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.clicked.connect(window.close)

        self.title = QLabel("")
        self.title.setObjectName("titleLabel")
        self.title.setAlignment(Qt.AlignCenter)

        self.open_btn = QPushButton("Open")
        self.open_btn.setObjectName("openBtn")
        self.open_btn.clicked.connect(window.open_externally)

        # HTML only: flips between rendered preview and source view.
        self.mode_btn = QPushButton("Code")
        self.mode_btn.setObjectName("openBtn")
        self.mode_btn.clicked.connect(window.toggle_mode)
        self.mode_btn.hide()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.addWidget(self.close_btn)
        lay.addWidget(self.title, 1)
        lay.addWidget(self.mode_btn)
        lay.addWidget(self.open_btn)

    # The window is a full-screen overlay, so there is nothing for the
    # compositor to move: dragging the titlebar slides the panel inside it.
    # startSystemMove() would be a no-op here (and is ignored outright on
    # Wayland, which is what kept the panel off centre in the first place).
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_from = event.globalPosition().toPoint()
            self._drag_origin = self._window.panel.pos()
            # Accepted, not propagated: the press has to land here for the
            # move events of the drag to follow it.
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            delta = event.globalPosition().toPoint() - self._drag_from
            self._window.move_panel(self._drag_origin + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_from = None
        super().mouseReleaseEvent(event)


class QuickView(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Dialog
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("QuickView")

        self.mime_db = QMimeDatabase()
        self.icon_provider = QFileIconProvider()
        self.current_path = None
        self.selection = []
        self.sel_index = 0
        self.anim_timer = None
        self.media = None
        self._mem_cache = OrderedDict()  # key -> (pixmap, dims, nbytes)
        self.pool = WorkerPool()
        self._render_job = None
        self._prefetch_job = None
        self._prefetch_queue = []  # (path, key, job) awaiting a warm render
        self._pdf_gen = None  # token invalidating in-flight page appends
        self._pdf_labels = []  # one placeholder per page of the open PDF
        self._text_readers = set()  # readers still on a pool thread
        self._html_rendered = True  # HTML mode: rendered page vs. source
        self._office_text = False   # office mode: thumbnail vs. extracted text
        self._office_doc = None     # (path, payload) so the toggle is instant
        self._web_profile = None  # lazy; one hardened profile for all pages

        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 160))
        self.panel.setGraphicsEffect(shadow)

        # Room left around the panel for the drop shadow; the panel is
        # placed inside the overlay by _place_panel(), not by a layout.
        self._margins = (24, 24, 24, 32)  # left, top, right, bottom
        self._panel_size = QSize(520 + 48, 320 + 56 + 40)
        self._panel_pos = None  # None = centred; a point once dragged

        self.titlebar = TitleBar(self)
        self.content = QStackedLayout()
        self.content.setContentsMargins(0, 0, 0, 0)

        panel_lay = QVBoxLayout(self.panel)
        panel_lay.setContentsMargins(1, 0, 1, 1)
        panel_lay.setSpacing(0)
        panel_lay.addWidget(self.titlebar)
        panel_lay.addLayout(self.content, 1)

        # Fully opaque: every colour below is solid, including the ones that
        # used to be white-over-panel blends. WA_TranslucentBackground stays
        # on above — it is what lets the rounded corners and the drop shadow
        # composite against the desktop — but nothing shows through the panel
        # itself any more.
        self.setStyleSheet("""
            #panel {
                background-color: #222226;
                border-radius: 12px;
                border: 1px solid #3c3c40;
            }
            #titleLabel {
                color: #e8e8ea; font-size: 13px; font-weight: 600;
            }
            #closeBtn {
                background-color: #5a5a5f; color: #d0d0d4;
                border: none; border-radius: 12px;
                font-size: 11px; font-weight: bold;
            }
            #closeBtn:hover { background-color: #ff5f57; color: #4b0d0a; }
            #openBtn {
                background-color: #38383c; color: #e8e8ea;
                border: none; border-radius: 6px; padding: 4px 14px;
                font-size: 12px;
            }
            #openBtn:hover { background-color: #4a4a4d; }
            QPlainTextEdit {
                background-color: #222226; color: #dcdcde;
                border: none; padding: 8px 14px;
                font-family: monospace; font-size: 12px;
            }
            QLabel { color: #dcdcde; }
            QSlider::groove:horizontal {
                height: 4px; background: #454548; border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 12px; margin: -4px 0; border-radius: 6px; background: #e8e8ea;
            }
        """)

        for keys, fn in (
            (Qt.Key_Space, lambda: self.dismiss("space")),
            (Qt.Key_Escape, lambda: self.dismiss("escape")),
            (Qt.Key_Q, lambda: self.dismiss("q")),
            (Qt.Key_Left, lambda: self.step_sibling(-1)),
            (Qt.Key_Right, lambda: self.step_sibling(+1)),
            (Qt.Key_Return, self.open_externally),
            (Qt.Key_Enter, self.open_externally),
        ):
            QShortcut(QKeySequence(keys), self, activated=fn)
        QShortcut(
            QKeySequence(Qt.CTRL | Qt.Key_Q), self,
            activated=QApplication.instance().quit,
        )

    def dismiss(self, reason: str = "request"):
        """Hide the preview but keep the process resident for instant reuse."""
        if self.isVisible():
            log.debug("dismissed (%s)", reason)
        self.clear_content()
        self.hide()
        # The next preview starts centred again, whatever this one was
        # dragged to.
        self._panel_pos = None

    def closeEvent(self, event):
        event.ignore()
        self.dismiss("window closed")

    # ---------------------------------------------------------------- helpers

    def screen_avail(self) -> QSize:
        screen = self.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry().size()

    def set_panel_size(self, w: int, h: int):
        avail = self.screen_avail()
        w = min(max(w, 480), int(avail.width() * 0.85))
        h = min(max(h, 320), int(avail.height() * 0.85))
        # Sizes the panel, not the window: the window is a full-screen
        # overlay, so a preview that grows (a PDF swapping its "Loading…"
        # card for the page column) re-centres instead of drifting away
        # from wherever the compositor first put it.
        self._panel_size = QSize(w + 48, h + 56 + 40)
        self._place_panel()
        # Which caller sized the panel, and against which screen. A preview
        # that opens too small is either a fallback card sized 520x320 or a
        # screen this ran before the window had one.
        log.debug(
            "panel sized %dx%d (panel %dx%d at %d,%d in overlay %dx%d, "
            "screen %dx%d)",
            w, h, self.panel.width(), self.panel.height(),
            self.panel.x(), self.panel.y(), self.width(), self.height(),
            avail.width(), avail.height(),
        )

    def fit_overlay(self):
        """Cover the work area. Only the size lands on Wayland; that is
        enough, because the panel is centred against the screen below."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        self.setGeometry(screen.availableGeometry())

    def _place_panel(self):
        """Lay the panel out inside the overlay: centred unless dragged."""
        left, top, right, bottom = self._margins
        pw = min(self._panel_size.width(), self.width()) - left - right
        ph = min(self._panel_size.height(), self.height()) - top - bottom
        self.panel.resize(max(pw, 1), max(ph, 1))
        if self._panel_pos is None:
            self.center_panel()
        else:
            self.move_panel(self._panel_pos)
        self._apply_mask()

    def _apply_mask(self):
        """Take input only where the panel is.

        The overlay spans the work area so the panel can be positioned
        exactly, but it must not *behave* like a window that size: without
        this mask every click meant for Dolphin lands on the preview
        instead. Masked, the rest of the overlay is not even there as far
        as clicks are concerned — they go to whatever is underneath, as
        they did when the window was panel-sized.
        """
        handle = self.windowHandle()
        if handle is None:
            return  # not created yet; _place_panel runs again after show()
        left, top, right, bottom = self._margins
        # QWindow.setMask(), not QWidget.setMask(): the widget one clips
        # painting as well as input, so shrinking it left the previous,
        # larger panel's pixels on screen with the new panel drawn inside
        # them — a window within a window. The window one is an input hint
        # and nothing more.
        handle.setMask(
            QRegion(
                # Grown by the margins so the drop shadow stays clickable
                # rather than being cut out of the input region.
                self.panel.geometry().adjusted(-left, -top, right, bottom)
            )
        )
        self.clearMask()  # undo any widget-level mask from an older build
        self.update()     # repaint the whole overlay, stale frame included

    def center_panel(self):
        """Centre the panel on the screen itself and forget any drag.

        Centred in global coordinates, not in the overlay: a compositor
        that puts the overlay somewhere other than the work-area origin
        then still leaves the panel dead centre on screen.
        """
        self._panel_pos = None
        screen = self.screen() or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        target = QPoint(
            geo.x() + (geo.width() - self.panel.width()) // 2,
            geo.y() + (geo.height() - self.panel.height()) // 2,
        )
        self.panel.move(self._clamped(self.mapFromGlobal(target)))

    def move_panel(self, pos):
        """Move the panel within the overlay (a titlebar drag)."""
        self._panel_pos = self._clamped(pos)
        self.panel.move(self._panel_pos)

    def _clamped(self, pos) -> QPoint:
        """A panel position kept fully inside the overlay."""
        left, top, right, bottom = self._margins
        return QPoint(
            max(left, min(pos.x(), self.width() - self.panel.width() - right)),
            max(top, min(pos.y(), self.height() - self.panel.height() - bottom)),
        )

    def resizeEvent(self, event):
        # The overlay follows the screen (resolution change, another output).
        super().resizeEvent(event)
        self._place_panel()
        log.debug(
            "overlay %dx%d, panel %dx%d at %d,%d",
            self.width(), self.height(), self.panel.width(),
            self.panel.height(), self.panel.x(), self.panel.y(),
        )

    def _cancel_render(self):
        job = self._render_job
        self._render_job = None
        if job is not None:
            job.cancel()

    def clear_content(self):
        self._cancel_render()
        if self.media is not None:
            # Killing the worker is the whole teardown: the decoder, the
            # audio stream and the frame buffer all live in that process, so
            # no late signal can reach widgets we are about to destroy.
            self.media.stop()
            self.media.deleteLater()
            self.media = None
        if self.anim_timer is not None:
            self.anim_timer.stop()
            self.anim_timer.deleteLater()
            self.anim_timer = None
        self._clear_widgets()

    def _clear_widgets(self):
        # Widgets only — no process/player teardown. The PDF path swaps its
        # "Loading…" message for the page column while its helper is still
        # streaming, so it must not go through clear_content().
        self._pdf_gen = None
        self._pdf_labels = []  # they are about to be deleted with the view
        while self.content.count():
            w = self.content.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

    def open_externally(self):
        if self.current_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.current_path))
            self.dismiss("opened externally")

    def step_sibling(self, delta: int):
        # With a multi-file selection, ← → page through it (like Quick Look
        # on several selected files); otherwise walk the folder's siblings.
        if len(self.selection) > 1:
            self.sel_index = (self.sel_index + delta) % len(self.selection)
            self.show_file(self.selection[self.sel_index])
            return
        if not self.current_path:
            return
        folder = os.path.dirname(self.current_path) or "."
        try:
            names = sorted(
                (n for n in os.listdir(folder) if not n.startswith(".")),
                key=str.lower,
            )
        except OSError:
            return
        if not names:
            return
        cur = os.path.basename(self.current_path)
        idx = names.index(cur) if cur in names else 0
        nxt = names[(idx + delta) % len(names)]
        self.show_files([os.path.join(folder, nxt)])

    # ---------------------------------------------------------------- preview

    def set_title(self, text: str):
        if len(self.selection) > 1:
            text = f"{text}  ·  {self.sel_index + 1}/{len(self.selection)}"
        self.titlebar.title.setText(text)

    def show_files(self, paths, index: int = 0):
        self.selection = [os.path.abspath(p) for p in paths]
        self.sel_index = max(0, min(index, len(self.selection) - 1))
        self.show_file(self.selection[self.sel_index])

    def show_file(self, path: str):
        path = os.path.abspath(path)
        self.current_path = path
        self.clear_content()
        log.info("preview: %s", path)

        name = os.path.basename(path) or path
        self.set_title(name)
        self.titlebar.mode_btn.setVisible(False)  # show_html() re-enables

        if not os.path.exists(path):
            self.show_message(f"File not found:\n{path}")
        elif os.path.isdir(path):
            self.show_folder(path)
        else:
            mime = self.mime_db.mimeTypeForFile(path).name()
            ext = os.path.splitext(path)[1].lower()
            # This routing is the sandbox enforcement point. Every branch
            # below hands the file to a jailed worker (show_image, show_pdf,
            # show_anim, show_media) or reads plain bytes (text/fallback).
            # The one exception is HTML, which QtWebEngine parses in its own
            # Chromium renderer sandbox rather than in our jail;
            # QUICKVIEW_STRICT_SANDBOX=1 drops it to the source view for
            # anyone who would rather not rely on that.
            allow_webengine = os.environ.get("QUICKVIEW_STRICT_SANDBOX") != "1"
            if mime in ANIM_MIMES:
                self.show_anim(path)
            elif mime.startswith("image/"):
                self.show_image(path)
            elif mime == "application/pdf":
                self.show_pdf(path)
            elif (
                mime == "text/html" or ext in (".html", ".htm")
            ) and allow_webengine:
                self.show_html(path)
            elif mime.startswith(("video/", "audio/")):
                self.show_media(path, video=mime.startswith("video/"))
            elif mime in ARCHIVE_MIMES or ext in ARCHIVE_EXTENSIONS:
                self.show_archive(path, mime)
            elif mime in OFFICE_MIMES:
                self.show_office(path, mime)
            elif mime.startswith("text/") or ext in TEXT_EXTENSIONS:
                self.show_text(path)
            else:
                self.show_fallback(path, mime)

        # Transparent overlay the size of the work area, with the panel
        # centred inside it: the one way to put a preview at a chosen spot
        # under Wayland, which ignores move() outright. Deliberately *not*
        # fullscreen — KWin lowers an inactive fullscreen window below the
        # focused one, so a preview raised without an activation token
        # (from the daemon, not a click) would vanish behind other windows.
        self.fit_overlay()
        self.show()
        self.raise_()
        self.activateWindow()

    def show_message(self, text: str):
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        self.content.addWidget(label)
        self.set_panel_size(520, 320)

    def image_fit_box(self) -> tuple:
        avail = self.screen_avail()
        return int(avail.width() * 0.85) - 48, int(avail.height() * 0.85) - 96

    def show_image(self, path: str):
        max_w, max_h = self.image_fit_box()
        try:
            st = os.stat(path)
        except OSError as exc:
            self.show_message(str(exc))
            return
        key = cache_key(path, st, max_w, max_h)

        hit = self._mem_cache.get(key)
        if hit is not None:
            self._mem_cache.move_to_end(key)
            log.debug("memory cache hit: %s", path)
            pix, dims, _nbytes = hit
            self._display_image(path, pix, dims)
            return

        png = cache_read(key)
        if png is not None:
            img = QImage.fromData(png)
            if not img.isNull():
                log.debug("disk cache hit: %s", path)
                self._show_decoded(path, key, img)
                return
            # A bad entry would otherwise be served on every view until the
            # source file's mtime changes — drop it and fall through to a
            # fresh render.
            log.warning("dropping corrupt cache entry for %s", path)
            cache_remove(key)

        # Decode asynchronously in the jail: a slow or hostile file must not
        # freeze the event loop — keys, the close button and the daemon socket
        # stay live while the worker works.
        self.show_message("Loading preview…")
        got = {"png": None, "job": None}

        def on_frame(png: bytes):
            got["png"] = png

        def on_done(ok: bool, error: str):
            if self._render_job is not got["job"] or path != self.current_path:
                return  # superseded, or the user moved on while we rendered
            self._render_job = None
            png = got["png"]
            img = (
                QImage.fromData(png)
                if ok and png and png[:4] == PNG_MAGIC
                else QImage()
            )
            if not img.isNull():
                log.debug("rendered: %s", path)
                # Persist only after the full decode succeeds — a truncated
                # blob behind a valid magic must not become a sticky cache
                # entry.
                cache_write(key, png)
                self._show_decoded(path, key, img)
            else:
                log.warning("render failed: %s (%s)", path, error[:500])
                self.clear_content()
                self.show_fallback(
                    path, self.mime_db.mimeTypeForFile(path).name()
                )

        got["job"] = self._render_job = SandboxJob(
            self.pool, path,
            {"op": "image", "max_w": max_w, "max_h": max_h},
            on_frame, on_done, self,
        )

    def _show_decoded(self, path: str, key: str, img: QImage):
        """Display a successfully decoded preview and remember its pixmap."""
        dims = img.text("QuickView:OrigSize") or f"{img.width()}×{img.height()}"
        pix = QPixmap.fromImage(img)
        nbytes = pix.width() * pix.height() * max(pix.depth(), 1) // 8
        self._mem_cache.pop(key, None)
        self._mem_cache[key] = (pix, dims, nbytes)
        # The byte total is recomputed from the stored entries rather than
        # tracked in a separate counter: the cache holds a few dozen entries
        # at most, and derived state can't drift in a process that never
        # exits.
        while (
            sum(nb for *_, nb in self._mem_cache.values()) > MEM_CACHE_BYTES
            and len(self._mem_cache) > 1
        ):
            self._mem_cache.popitem(last=False)
        self._display_image(path, pix, dims)

    def _display_image(self, path: str, pix: QPixmap, dims: str):
        self.clear_content()
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        label.setPixmap(pix)
        self.content.addWidget(label)
        self.set_panel_size(pix.width() + 24, pix.height() + 24)
        self.set_title(f"{os.path.basename(path)}  —  {dims}")
        self._prefetch_neighbors()

    # ------------------------------------------------------------- prefetch
    # Warm the disk cache for the files ← → would show next, so paging
    # through a folder of photos never waits on a cold decode. Same sandboxed
    # helper, same cache key — show_image() then hits the disk tier.

    def _neighbor_paths(self) -> list:
        if len(self.selection) > 1:
            n = len(self.selection)
            return [
                self.selection[(self.sel_index + d) % n] for d in (1, -1)
            ]
        if not self.current_path:
            return []
        folder = os.path.dirname(self.current_path) or "."
        try:
            names = sorted(
                (n for n in os.listdir(folder) if not n.startswith(".")),
                key=str.lower,
            )
        except OSError:
            return []
        cur = os.path.basename(self.current_path)
        if cur not in names:
            return []
        idx = names.index(cur)
        return [
            os.path.join(folder, names[(idx + d) % len(names)])
            for d in (1, -1)
        ]

    def _prefetch_neighbors(self):
        max_w, max_h = self.image_fit_box()
        queue = []
        for p in self._neighbor_paths():
            if p == self.current_path:
                continue
            mime = self.mime_db.mimeTypeForFile(p).name()
            # Only what show_image() would render: animations take the
            # show_anim() path (whose frames this key is never read for)
            # and everything else has no cache tier to warm.
            if not mime.startswith("image/") or mime in ANIM_MIMES:
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = cache_key(p, st, max_w, max_h)
            if key in self._mem_cache or os.path.exists(
                os.path.join(CACHE_DIR, key)
            ):
                continue
            queue.append((p, key, max_w, max_h))
        self._prefetch_queue = queue
        self._start_next_prefetch()

    def _start_next_prefetch(self):
        # One worker at a time, so speculative work never competes with a
        # render the user is actually waiting on for a whole core.
        if self._prefetch_job is not None or not self._prefetch_queue:
            return
        path, key, max_w, max_h = self._prefetch_queue.pop(0)
        got = {"png": None}

        def on_done(ok: bool, _error: str):
            self._prefetch_job = None
            png = got["png"]
            if ok and png and png[:4] == PNG_MAGIC:
                log.debug("prefetched: %s", path)
                cache_write(key, png)
            self._start_next_prefetch()

        self._prefetch_job = SandboxJob(
            self.pool, path,
            {"op": "image", "max_w": max_w, "max_h": max_h},
            lambda png: got.__setitem__("png", png), on_done, self,
        )

    # ------------------------------------------------------- animation
    # QMovie decodes an untrusted GIF frame by frame for as long as the
    # window is open, so it never ran in the daemon safely. Instead the
    # jailed worker decodes every frame up front and streams them here as
    # PNGs; playback is then a timer cycling pixmaps the daemon already
    # holds — no animation parser in this process at all.

    def show_anim(self, path: str):
        max_w, max_h = self.image_fit_box()
        self.show_message("Loading preview…")
        state = {"frames": [], "bytes": 0, "job": None}

        def on_frame(payload: bytes):
            if self._render_job is not state["job"] or path != self.current_path:
                return
            if len(payload) < 4:  # the worker is untrusted: no short reads
                return
            delay = struct.unpack(">I", payload[:4])[0]
            img = QImage.fromData(payload[4:])
            if img.isNull():
                return
            pix = QPixmap.fromImage(img)
            state["bytes"] += pix.width() * pix.height() * 4
            state["frames"].append((pix, delay))
            if len(state["frames"]) == 1:
                self._begin_anim(path, state)
            if (
                len(state["frames"]) >= ANIM_MAX_FRAMES
                or state["bytes"] >= ANIM_MAX_PIXMAP_BYTES
            ):
                # Enough: play what we have rather than let a worker that
                # streams for ever fill the daemon's heap.
                log.debug(
                    "animation capped at %d frames: %s",
                    len(state["frames"]), path,
                )
                self._cancel_render()

        def on_done(ok: bool, error: str):
            if self._render_job is not state["job"] or path != self.current_path:
                return
            self._render_job = None
            if state["frames"]:
                return  # partial decodes still animate what arrived
            log.warning("animation decode failed: %s (%s)", path, error[:500])
            # Not an animation we can decode — a still frame is still useful.
            self.clear_content()
            self.show_image(path)

        state["job"] = self._render_job = SandboxJob(
            self.pool, path, {"op": "anim", "max_w": max_w, "max_h": max_h},
            on_frame, on_done, self,
        )

    def _begin_anim(self, path: str, state: dict):
        """Show frame 0 and start the cycle; later frames join as they land."""
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        pix, _delay = state["frames"][0]
        label.setPixmap(pix)
        self._clear_widgets()
        self.content.addWidget(label)
        self.set_panel_size(pix.width() + 24, pix.height() + 24)

        idx = {"i": 0}
        timer = QTimer(self)
        timer.setSingleShot(True)
        self.anim_timer = timer

        def tick():
            frames = state["frames"]
            if not frames or self.current_path != path:
                return
            idx["i"] = (idx["i"] + 1) % len(frames)
            pix, delay = frames[idx["i"]]
            label.setPixmap(pix)
            timer.start(delay)

        timer.timeout.connect(tick)
        timer.start(state["frames"][0][1])

    # -------------------------------------------------------------- pdf
    # PDFs render out of process like still images: render_pdf.py streams
    # page PNGs from inside the bubblewrap jail and the daemon shows page 1
    # the moment it arrives. Pages land in the disk cache individually, so
    # a repeat view never touches the PDF parser at all.

    def show_pdf(self, path: str):
        avail = self.screen_avail()
        page_w = max(int(avail.width() * 0.55) - 44, 400)
        try:
            st = os.stat(path)
        except OSError as exc:
            self.show_message(str(exc))
            return

        def page_key(i: int) -> str:
            return cache_key(path, st, page_w, 0, f"pdf{i}v2")

        png0 = cache_read(page_key(0))
        img0 = QImage.fromData(png0) if png0 is not None else QImage()
        total = 0
        if not img0.isNull():
            try:
                total = int(img0.text("QuickView:PageCount"))
            except ValueError:
                pass
        if total > 0:
            log.debug("disk cache hit (pdf): %s", path)
            self._pdf_show_cached(path, page_key, page_w, total, img0)
        else:
            if png0 is not None:
                cache_remove(page_key(0))
            self._pdf_render(path, page_key, page_w)

    def _pdf_begin_view(self, path: str, total: int, sizes: list):
        """Swap in the page column, at its full height from the start.

        Every page gets a placeholder of its real size before any pixels
        arrive, so the scrollable range is final on the first paint. Adding
        pages as they decoded meant the range grew for a second or two, and
        a reader who scrolled in that window was clamped to a two-page
        document and left near the top of a thirty-page one — which looks
        exactly like the view scrolling itself back up.
        """
        self._clear_widgets()
        col = QWidget()
        lay = QVBoxLayout(col)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)
        self._pdf_labels = []
        for w, h in sizes:
            label = QLabel()
            label.setFixedSize(w, h)
            label.setStyleSheet("background: #2a2a2e;")  # an unfilled page
            lay.addWidget(label, 0, Qt.AlignHCenter)
            self._pdf_labels.append(label)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: #222226;")
        scroll.setWidget(col)
        self.content.addWidget(scroll)
        avail = self.screen_avail()
        self.set_panel_size(int(avail.width() * 0.55), int(avail.height() * 0.85))
        pages = f"{total} pages"
        if total > PDF_MAX_PAGES:
            pages += f" (showing first {PDF_MAX_PAGES})"
        self.set_title(f"{os.path.basename(path)}  —  {pages}")
        return lay

    def _pdf_fill_page(self, i: int, img: QImage):
        """Put a decoded page into its placeholder."""
        if not 0 <= i < len(self._pdf_labels):
            return
        label = self._pdf_labels[i]
        if label.size() != img.size():  # an estimate that missed
            label.setFixedSize(img.size())
        label.setPixmap(QPixmap.fromImage(img))
        label.setStyleSheet("")

    def _pdf_show_cached(
        self, path: str, page_key, page_w: int, total: int, img0: QImage,
        op: str = "pdf", extra: dict = None
    ):
        # Fill cached pages one per event-loop turn: page 1 paints
        # immediately and a 50-page reopen never freezes input.
        count = min(total, PDF_MAX_PAGES)
        # Sizes come from the cached PNGs' headers — 24 bytes each, no
        # decode — so the column is the right height before any page is
        # decoded. A page missing from the cache falls back to page 1's
        # size; the resumed render corrects it when it lands.
        fallback = (img0.width(), img0.height())
        sizes = [fallback]
        for i in range(1, count):
            head = cache_read_head(page_key(i))
            sizes.append((head and png_size(head)) or fallback)
        lay = self._pdf_begin_view(path, total, sizes)
        gen = object()
        self._pdf_gen = gen
        state = {"i": 0}

        def step():
            if self._pdf_gen is not gen:
                return  # the view was cleared under us
            i = state["i"]
            if i >= count:
                return
            img = (
                img0 if i == 0
                else QImage.fromData(cache_read(page_key(i)) or b"")
            )
            if img.isNull():
                # A page is missing (the pruner dropped it, or an earlier
                # render was cut short when the panel closed). Resume the
                # render at that page and keep appending to the view we
                # already built: restarting from page 0 would throw away
                # what is on screen and jump the reader back to the top.
                log.debug("pdf cache incomplete at page %d: %s", i, path)
                self._pdf_gen = None  # stop this stepper; the job takes over
                self._pdf_render(
                    path, page_key, page_w, start=i, lay=lay, op=op, extra=extra
                )
                return
            self._pdf_fill_page(i, img)
            state["i"] = i + 1
            QTimer.singleShot(0, step)

        step()

    def _pdf_render(self, path: str, page_key, page_w: int, start: int = 0,
                    lay=None, op: str = "pdf", extra: dict = None,
                    on_doc=None):
        """Render pages start.. into the view, streaming from the jail.

        With lay given the pages append to an existing page column (a
        resumed partial cache); otherwise the column is created when the
        first page arrives.
        """
        self._cancel_render()
        if lay is None:
            self._clear_widgets()
            self.show_message("Loading preview…")
            self._pdf_labels = []
        # got: frames received, which is what fixes a page's number;
        # shown: pages actually on screen. They differ when a page fails to
        # decode, and the page number must not slide down to fill that gap —
        # caching the next page under the failed page's key would put every
        # later page one position too low, and a cache that wrong looks
        # complete on the next open.
        state = {"got": 0, "shown": 0, "lay": lay, "job": None}

        def on_frame(png: bytes):
            if self._render_job is not state["job"] or path != self.current_path:
                return
            if on_doc is not None and state["job"].header.get("kind") == "doc":
                # Not page images: a thumbnail-and-text payload, which is
                # what a slide deck answers with.
                on_doc(png)
                state["shown"] += 1
                return
            page = start + state["got"]
            state["got"] += 1
            img = QImage.fromData(png)
            if img.isNull():
                # Left uncached, so the next open resumes the render here.
                log.warning("pdf page %d decode failed: %s", page, path)
                return
            if state["lay"] is None:
                try:
                    total = int(img.text("QuickView:PageCount"))
                except ValueError:
                    # No usable tEXt chunk — fall back to what the worker
                    # said it would stream.
                    total = state["job"].header.get("count", page + 1)
                # Page 1's size stands in for the rest until they arrive:
                # pages of one document almost always match, and any that
                # does not is corrected as it lands. What matters is that
                # the column is its full height before the reader scrolls.
                count = min(total, PDF_MAX_PAGES)
                state["lay"] = self._pdf_begin_view(
                    path, total, [(img.width(), img.height())] * count
                )
            cache_write(page_key(page), png)
            self._pdf_fill_page(page, img)
            state["shown"] += 1

        def on_done(ok: bool, error: str):
            if self._render_job is not state["job"] or path != self.current_path:
                return
            self._render_job = None
            if state["shown"] == 0 and start == 0:
                log.warning("%s render failed: %s (%s)", op, path, error[:500])
                self._clear_widgets()
                self.show_fallback(
                    path, self.mime_db.mimeTypeForFile(path).name()
                )
            elif not ok:
                # Keep the pages that made it; just note the truncation.
                log.warning("pdf render truncated: %s (%s)", path, error[:500])

        job = {"op": op, "page_w": page_w, "max_pages": PDF_MAX_PAGES,
               "start": start}
        if extra:
            job.update(extra)
        state["job"] = self._render_job = SandboxJob(
            self.pool, path, job, on_frame, on_done, self,
        )

    # ------------------------------------------------------------ media
    # Audio and video are decoded by media_worker.py inside the jail, which
    # also owns the audio clock (so Qt keeps doing A/V sync in there). What
    # arrives here is finished RGB frames in shared memory plus position
    # updates — the daemon runs no demuxer and no codec.

    def show_media(self, path: str, video: bool):
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.setSpacing(6)

        avail = self.screen_avail()
        if video:
            surface = QLabel()
            surface.setAlignment(Qt.AlignCenter)
            surface.setStyleSheet("background: black;")
            surface.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
            lay.addWidget(surface, 1)
            max_w = int(avail.width() * 0.6)
            max_h = int(avail.height() * 0.65)
        else:
            surface = None
            icon = self.icon_provider.icon(QFileInfo(path))
            art = QLabel()
            art.setAlignment(Qt.AlignCenter)
            art.setPixmap(icon.pixmap(128, 128))
            lay.addWidget(art, 1)
            # Audio still needs a frame budget for cover art the decoder may
            # emit; keep it small.
            max_w, max_h = 640, 640

        controls = QHBoxLayout()
        controls.setContentsMargins(14, 0, 14, 0)
        play_btn = QPushButton("⏸")
        play_btn.setObjectName("openBtn")
        play_btn.setFixedWidth(40)
        slider = QSlider(Qt.Horizontal)
        time_lbl = QLabel("0:00 / 0:00")
        controls.addWidget(play_btn)
        controls.addWidget(slider, 1)
        controls.addWidget(time_lbl)
        lay.addLayout(controls)

        self.content.addWidget(wrap)
        if video:
            self.set_panel_size(int(avail.width() * 0.6), int(avail.height() * 0.65))
        else:
            self.set_panel_size(520, 320)

        def fmt(ms):
            s = max(int(ms), 0) // 1000
            return f"{s // 60}:{s % 60:02d}"

        state = {"duration": 0, "playing": True}

        def on_frame(img: QImage):
            if surface is None:
                return
            pix = QPixmap.fromImage(img)
            if pix.width() > surface.width() or pix.height() > surface.height():
                pix = pix.scaled(
                    surface.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            surface.setPixmap(pix)

        def on_meta(msg):
            state["duration"] = msg.get("duration", 0)
            slider.setRange(0, state["duration"])

        def on_position(msg):
            p = msg.get("position", 0)
            slider.blockSignals(True)
            slider.setValue(p)
            slider.blockSignals(False)
            time_lbl.setText(f"{fmt(p)} / {fmt(state['duration'])}")

        def on_eof(_msg=None):
            state["playing"] = False
            play_btn.setText("▶")

        def on_error(msg):
            error = msg if isinstance(msg, str) else msg.get("error", "failed")
            log.warning("media playback failed: %s (%s)", path, error[:500])
            if self.current_path != path:
                return
            self.clear_content()
            self.show_fallback(path, self.mime_db.mimeTypeForFile(path).name())

        self.media = MediaSession(
            self, path, max_w, max_h,
            {
                "frame": on_frame, "meta": on_meta, "position": on_position,
                "eof": on_eof, "error": on_error,
            },
        )

        def toggle():
            state["playing"] = not state["playing"]
            self.media.send({"t": "play" if state["playing"] else "pause"})
            play_btn.setText("⏸" if state["playing"] else "▶")

        play_btn.clicked.connect(toggle)
        slider.sliderMoved.connect(
            lambda p: self.media.send({"t": "seek", "position": p})
        )

    # ---------------------------------------------------------------- text
    # Text goes through the jail like everything else, for one reason: the
    # highlighter. Pygments lexers are regexes, and a file written to make
    # one backtrack would wedge whichever process runs it — which must not
    # be the one holding the window and the IPC socket. The worker sends
    # back the text plus colour spans; the daemon paints ranges and parses
    # nothing. If the sandbox is unavailable, show_text_direct() below reads
    # the bytes here instead, unhighlighted.

    def show_text(self, path: str):
        self.show_message("Loading preview…")
        # Size the window now, not when the text lands. show_message() sizes
        # the panel for a short message, and a window that is mapped small
        # and resized a moment later can keep the small size on Wayland —
        # which is what made the first open of a text file look cramped.
        self._size_for_text()
        state = {"job": None, "shown": False}

        def on_frame(payload: bytes):
            if self._render_job is not state["job"] or path != self.current_path:
                return
            try:
                doc = json.loads(payload)
                text = doc["text"]
                spans = doc.get("spans") or []
                styles = doc.get("styles") or []
            except (ValueError, TypeError, KeyError):
                log.warning("text worker sent a malformed payload: %s", path)
                return
            if doc.get("truncated"):
                text += "\n\n[... truncated ...]"
            self._clear_widgets()
            self._show_text_widget(text, spans, styles)
            state["shown"] = True

        def on_done(ok: bool, error: str):
            if self._render_job is not state["job"] or path != self.current_path:
                return
            self._render_job = None
            if not state["shown"]:
                log.debug(
                    "text worker unavailable (%s) — reading in-process", error[:200]
                )
                self.show_text_direct(path)

        state["job"] = self._render_job = SandboxJob(
            self.pool, path,
            {
                "op": "text", "name": os.path.basename(path),
                "limit": TEXT_PREVIEW_LIMIT, "style": CODE_STYLE,
            },
            on_frame, on_done, self,
        )

    def show_text_direct(self, path: str):
        """The unhighlighted fallback: read the bytes here, off the event
        loop. No parser is involved (plain bytes, capped at 1 MiB), but a
        file on a stalled NFS or FUSE mount would freeze the window and the
        daemon socket with it."""

        def show(data: bytes, error: str):
            if path != self.current_path:
                return  # the user moved on while the read was in flight
            if error:
                self._clear_widgets()
                self.show_message(error)
                return
            truncated = len(data) > TEXT_PREVIEW_LIMIT
            text = data[:TEXT_PREVIEW_LIMIT].decode("utf-8", errors="replace")
            if truncated:
                text += "\n\n[... truncated ...]"
            self._clear_widgets()
            self._show_text_widget(text)

        # Every in-flight reader is held, not just the newest: previewing a
        # second file while the first is still blocked on a stalled mount
        # used to drop the only reference to a running reader.
        reader = FileReader(path, TEXT_PREVIEW_LIMIT + 1)
        reader.done.connect(show)
        reader.done.connect(lambda *_: self._text_readers.discard(reader))
        self._text_readers.add(reader)
        QThreadPool.globalInstance().start(reader.run)

    def _size_for_text(self):
        avail = self.screen_avail()
        self.set_panel_size(int(avail.width() * 0.5), int(avail.height() * 0.75))

    def _show_text_widget(self, text: str, spans=(), styles=()):
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(text)
        edit.setFrameShape(QFrame.NoFrame)
        if spans and styles:
            self._paint_spans(edit, spans, styles)
        self.content.addWidget(edit)
        self._size_for_text()

    @staticmethod
    def _paint_spans(edit, spans, styles):
        """Colour [start, length, style] ranges over the document.

        The offsets come from a worker and are applied to a document this
        process built, so they are checked rather than trusted: a bad index
        or a range past the end is skipped, not clamped into something that
        paints the wrong text.
        """
        formats = []
        for colour in styles:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(colour))
            formats.append(fmt)
        doc = edit.document()
        end = doc.characterCount() - 1
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for span in spans:
            try:
                start, length, index = span
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(formats):
                continue
            if not 0 <= start < end or length <= 0 or start + length > end:
                continue
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.KeepAnchor)
            cursor.setCharFormat(formats[index])
        cursor.endEditBlock()

    # -------------------------------------------------------------- html
    # Rendered with QtWebEngine, hardened for untrusted files: JavaScript
    # and plugins off, every request outside file:/data: blocked before it
    # leaves the process (no phoning home), and an off-the-record profile
    # so nothing persists. Chromium's own multi-process sandbox still wraps
    # the renderer. The titlebar button flips to the plain source view;
    # QUICKVIEW_STRICT_SANDBOX=1 skips rendering entirely (code view only).

    def show_html(self, path: str):
        btn = self.titlebar.mode_btn
        btn.setVisible(True)
        if self._html_rendered:
            btn.setText("Code")
            self._show_html_rendered(path)
        else:
            btn.setText("Preview")
            self.show_text(path)

    def toggle_mode(self):
        """The titlebar's second button: HTML rendered/source, office
        thumbnail/text. Which one it means depends on what is open."""
        path = self.current_path
        if not path:
            return
        if self.mime_db.mimeTypeForFile(path).name() in OFFICE_MIMES:
            self._office_text = not self._office_text
            cached = self._office_doc
            if self._office_text and cached and cached[0] == path:
                self._render_office(path, cached[1])  # no second decode
                return
        else:
            self._html_rendered = not self._html_rendered
        self.show_file(path)

    def _show_html_rendered(self, path: str):
        from PySide6.QtWebEngineCore import (
            QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
            QWebEngineUrlRequestInterceptor,
        )
        from PySide6.QtWebEngineWidgets import QWebEngineView

        class LocalOnlyInterceptor(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                if info.requestUrl().scheme() not in ("file", "data"):
                    info.block(True)

        if self._web_profile is None:
            # One off-the-record profile for the daemon's lifetime, parented
            # to the window: pages (parented to their view) can never
            # outlive it, which a per-view profile can't guarantee — Qt
            # destroys siblings in creation order and warns "Expect
            # troubles!" when the profile goes first.
            self._web_profile = QWebEngineProfile(self)
            self._web_interceptor = LocalOnlyInterceptor(self._web_profile)
            self._web_profile.setUrlRequestInterceptor(self._web_interceptor)
            settings = self._web_profile.settings()
            for attr in (
                QWebEngineSettings.WebAttribute.JavascriptEnabled,
                QWebEngineSettings.WebAttribute.PluginsEnabled,
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            ):
                settings.setAttribute(attr, False)

        view = QWebEngineView()
        page = QWebEnginePage(self._web_profile, view)
        view.setPage(page)
        view.load(QUrl.fromLocalFile(path))
        self.content.addWidget(view)
        avail = self.screen_avail()
        self.set_panel_size(int(avail.width() * 0.6), int(avail.height() * 0.8))

    def show_folder(self, path: str):
        try:
            entries = sorted(
                (e for e in os.listdir(path) if not e.startswith(".")),
                key=str.lower,
            )
        except OSError as exc:
            self.show_message(str(exc))
            return
        icon = self.icon_provider.icon(QFileIconProvider.IconType.Folder)
        listing = "\n".join(entries[:200])
        if len(entries) > 200:
            listing += f"\n... and {len(entries) - 200} more"

        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        head = QLabel()
        head.setAlignment(Qt.AlignCenter)
        head.setPixmap(icon.pixmap(96, 96))
        sub = QLabel(f"{len(entries)} items")
        sub.setAlignment(Qt.AlignCenter)
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText(listing)
        body.setFrameShape(QFrame.NoFrame)
        lay.addWidget(head)
        lay.addWidget(sub)
        lay.addWidget(body, 1)
        self.content.addWidget(wrap)
        self.set_panel_size(520, 560)

    # ------------------------------------------------------------ archives
    # A listing, not an extraction: the worker reads headers only, so an
    # archive that expands to terabytes costs nothing. zip and tar go through
    # the standard library; rar, 7z and the rest through bsdtar/7z/unrar,
    # which read the archive from /dev/fd — the jail has the descriptor and
    # no filesystem to find a path in.

    def show_archive(self, path: str, mime: str):
        self.show_message("Loading preview…")
        state = {"job": None, "shown": False}

        def on_frame(payload: bytes):
            if self._render_job is not state["job"] or path != self.current_path:
                return
            try:
                listing = json.loads(payload)
                entries = listing["entries"]
            except (ValueError, TypeError, KeyError):
                log.warning("archive worker sent a malformed listing: %s", path)
                return
            self._clear_widgets()
            self._show_archive_widget(path, listing, entries)
            state["shown"] = True

        def on_done(ok: bool, error: str):
            if self._render_job is not state["job"] or path != self.current_path:
                return
            self._render_job = None
            if not state["shown"]:
                # Encrypted, corrupt, or a format nothing here can list.
                log.debug("no listing for %s (%s)", path, error[:200])
                self._clear_widgets()
                self.show_fallback(path, mime)

        state["job"] = self._render_job = SandboxJob(
            self.pool, path,
            {"op": "archive", "name": os.path.basename(path)},
            on_frame, on_done, self,
        )

    def _show_archive_widget(self, path: str, listing: dict, entries: list):
        rows = []
        for entry in entries:
            try:
                name, size = entry
            except (TypeError, ValueError):
                continue
            rows.append(
                f"{name}    {human_size(size)}" if size else str(name)
            )
        if listing.get("truncated"):
            # count is a floor, not a total, when the lister stopped early —
            # subtracting from it would invent a number.
            if listing.get("count_exact", True):
                rows.append(f"... and {listing.get('count', 0) - len(rows)} more")
            else:
                rows.append("... and more")

        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        head = QLabel()
        head.setAlignment(Qt.AlignCenter)
        # The themed mime icon (a package for archives), falling back to the
        # provider's generic one when the icon theme has nothing.
        mime = self.mime_db.mimeTypeForFile(path)
        themed = QIcon.fromTheme(mime.iconName())
        if themed.isNull():
            themed = QIcon.fromTheme(mime.genericIconName())
        if themed.isNull():
            themed = self.icon_provider.icon(QFileInfo(path))
        head.setPixmap(themed.pixmap(96, 96))
        count = listing.get("count", len(rows))
        exact = listing.get("count_exact", True)
        summary = f"{count} items" if exact else f"{count}+ items"
        if listing.get("total"):
            size = human_size(listing["total"])
            summary += f"  ·  {size} uncompressed" if exact else (
                f"  ·  over {size} uncompressed"
            )
        sub = QLabel(summary)
        sub.setAlignment(Qt.AlignCenter)
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText("\n".join(rows))
        body.setFrameShape(QFrame.NoFrame)
        lay.addWidget(head)
        lay.addWidget(sub)
        lay.addWidget(body, 1)
        self.content.addWidget(wrap)
        self.set_panel_size(640, 620)

    # -------------------------------------------------------------- office
    # OOXML and ODF are zip containers full of XML, so no office suite is
    # needed — or wanted, since one would have to run where the jail's
    # guarantees hold. The worker prefers the thumbnail the authoring
    # application embedded (its own rendering of page one, for the cost of
    # unzipping a member) and extracts text when there is none.

    def show_office(self, path: str, mime: str):
        # Laid out as pages, cached page by page, and shown by the same code
        # that shows a PDF — a document with pages gets the page view. Slide
        # decks have no layout path here, so for those the worker answers
        # with the thumbnail the deck embeds plus its text instead.
        if self._office_text and self._office_doc and self._office_doc[0] == path:
            self._render_office(path, self._office_doc[1])
            return
        avail = self.screen_avail()
        page_w = max(int(avail.width() * 0.55) - 44, 400)
        try:
            st = os.stat(path)
        except OSError as exc:
            self.show_message(str(exc))
            return

        def page_key(i: int) -> str:
            return cache_key(path, st, page_w, 0, f"off{i}v1")

        def on_doc(payload: bytes):
            try:
                doc = json.loads(payload)
            except ValueError:
                log.warning("office worker sent a malformed payload: %s", path)
                return
            self._office_doc = (path, doc)
            self._render_office(path, doc)

        png0 = cache_read(page_key(0))
        img0 = QImage.fromData(png0) if png0 is not None else QImage()
        total = 0
        if not img0.isNull():
            try:
                total = int(img0.text("QuickView:PageCount"))
            except ValueError:
                pass
        extra = {"name": os.path.basename(path), "limit": TEXT_PREVIEW_LIMIT}
        if total > 0:
            log.debug("disk cache hit (office): %s", path)
            self._pdf_show_cached(
                path, page_key, page_w, total, img0, op="office", extra=extra
            )
            return
        if png0 is not None:
            cache_remove(page_key(0))
        self._pdf_render(
            path, page_key, page_w, op="office", extra=extra, on_doc=on_doc
        )

    def _render_office(self, path: str, doc: dict):
        """Show the cached payload in whichever mode is selected."""
        image = doc.get("image_b64")
        text = doc.get("text") or ""
        btn = self.titlebar.mode_btn
        # The button only appears when there is something to switch to.
        btn.setVisible(bool(image) and bool(text))
        if image and not self._office_text:
            btn.setText("Text")
            img = QImage.fromData(base64.b64decode(image))
            if not img.isNull():
                max_w, max_h = self.image_fit_box()
                # Embedded thumbnails are small — 256x144 for a PowerPoint
                # deck — and a slide shown at that size reads as a mistake.
                # Enlarge to fill the panel, but never past 3x, beyond which
                # it stops looking like a slide and starts looking like mush.
                scale = min(
                    max_w / img.width(), max_h / img.height(), 3.0
                )
                if scale > 1.0 or img.width() > max_w or img.height() > max_h:
                    img = img.scaled(
                        int(img.width() * min(scale, 3.0)),
                        int(img.height() * min(scale, 3.0)),
                        Qt.KeepAspectRatio, Qt.SmoothTransformation,
                    )
                self._display_image(
                    path, QPixmap.fromImage(img), f"{img.width()}×{img.height()}"
                )
                btn.setVisible(bool(text))
                return
            log.warning("office thumbnail did not decode: %s", path)
        btn.setText("Preview")
        if doc.get("truncated"):
            text += "\n\n[... truncated ...]"
        self._clear_widgets()
        self._show_text_widget(text)
        self.set_title(os.path.basename(path))

    def show_fallback(self, path: str, mime: str):
        info = QFileInfo(path)
        icon = self.icon_provider.icon(info)
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setAlignment(Qt.AlignCenter)
        pic = QLabel()
        pic.setAlignment(Qt.AlignCenter)
        pic.setPixmap(icon.pixmap(128, 128))
        details = QLabel(
            f"<div align='center'>"
            f"<b>{html.escape(info.fileName())}</b><br><br>"
            f"{html.escape(mime)}<br>"
            f"{human_size(info.size())}<br>"
            f"Modified {info.lastModified().toString('yyyy-MM-dd hh:mm')}"
            f"</div>"
        )
        details.setTextFormat(Qt.RichText)
        lay.addWidget(pic)
        lay.addWidget(details)
        self.content.addWidget(wrap)
        self.set_panel_size(520, 360)


def connect_to_daemon() -> QLocalSocket | None:
    sock = QLocalSocket()
    sock.connectToServer(SOCKET_PATH)
    if not sock.waitForConnected(300):
        return None
    return sock


def forward_to_running_instance(paths: list) -> bool:
    sock = connect_to_daemon()
    if sock is None:
        return False
    # Already-normalized absolute paths, sent through the same request
    # format the other clients use; normalize_arg is idempotent on them,
    # so the daemon re-running it is a no-op.
    sock.write(ipc.encode_request(os.getcwd(), paths))
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def daemon_already_running() -> bool:
    sock = connect_to_daemon()
    if sock is None:
        return False
    sock.disconnectFromServer()
    return True


def main():
    args = sys.argv[1:]
    if "--clear-cache" in args:
        clear_cache()
        return 0
    daemon = "--daemon" in args
    args = [a for a in args if a != "--daemon"]

    paths = [ipc.normalize_arg(raw) for raw in args]
    if not paths and not daemon:
        print(__doc__)
        return 1

    # Fail with a clear message instead of letting QApplication abort —
    # e.g. the systemd unit started before the session env was imported,
    # or `systemctl --user start` from an SSH login. RestartSec in the
    # unit paces the retries so this can't trip the start limit.
    if not (
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("QT_QPA_PLATFORM")
    ):
        print(
            "quickview: no DISPLAY or WAYLAND_DISPLAY — graphical session "
            "not up yet?",
            file=sys.stderr,
        )
        return 1

    # QtWebEngine (HTML previews) is imported lazily on first use, which Qt
    # only allows if contexts are shareable from the start.
    QGuiApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("QuickView")
    # Stay resident after the window is dismissed so the next preview is
    # instant — Qt/Python startup only ever happens once.
    app.setQuitOnLastWindowClosed(False)

    # Single instance: forward the paths to a running viewer, which toggles
    # or switches the preview — like pressing Space again in Finder.
    if paths and forward_to_running_instance(paths):
        return 0
    if daemon and not paths and daemon_already_running():
        return 0

    setup_logging()
    log.info("daemon starting (pid %d), logging to %s", os.getpid(), LOG_FILE)

    # A name containing '/' makes QLocalServer use it as the literal socket
    # path instead of placing it in QDir::tempPath(), keeping the location
    # independent of TMPDIR and identical to what client.py computes.
    QLocalServer.removeServer(SOCKET_PATH)
    server = QLocalServer()
    if not server.listen(SOCKET_PATH):
        # Without the socket every later invocation spawns another daemon;
        # better to fail loudly and let systemd retry.
        log.error(
            "cannot listen on %s: %s", SOCKET_PATH, server.errorString()
        )
        return 1

    viewer = QuickView()

    def on_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return
        # A large selection arrives in several chunks; accumulate until the
        # client closes its end (that close is the message framing) instead
        # of acting on the first readyRead and truncating the path list.
        buf = bytearray()

        def on_ready():
            buf.extend(bytes(conn.readAll()))

        def on_done():
            buf.extend(bytes(conn.readAll()))
            conn.deleteLater()
            new_paths = ipc.decode_request(bytes(buf))
            if not new_paths:
                return
            if (
                len(new_paths) == 1
                and viewer.isVisible()
                and viewer.current_path
                # realpath, so re-triggering through a symlink still toggles
                and os.path.realpath(new_paths[0])
                == os.path.realpath(viewer.current_path)
            ):
                viewer.dismiss("same path sent again")
            else:
                viewer.show_files(new_paths)

        conn.readyRead.connect(on_ready)
        conn.disconnected.connect(on_done)

        def on_guard_timeout():
            # The message is incomplete by definition here — detach on_done
            # first so the forced close drops the buffer instead of acting
            # on a truncated path list.
            conn.disconnected.disconnect(on_done)
            conn.abort()
            conn.deleteLater()

        # A client that never closes must not hold the slot open forever;
        # the timer dies with conn, so it can't fire on a deleted socket.
        guard = QTimer(conn)
        guard.setSingleShot(True)
        guard.timeout.connect(on_guard_timeout)
        guard.start(2000)

    server.newConnection.connect(on_connection)
    # Boot the first workers now, while the user is still reaching for the
    # keyboard: the ~150 ms Qt import in the jail is what previews used to
    # wait on, and a resident daemon can pay it ahead of time.
    QTimer.singleShot(0, viewer.pool.prime)
    app.aboutToQuit.connect(viewer.pool.shutdown)
    if paths:
        viewer.show_files(paths)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
