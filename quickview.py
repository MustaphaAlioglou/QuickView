#!/usr/bin/env python3
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

import faulthandler
import hashlib
import html
import logging
import logging.handlers
import os
import shutil
import sys
from collections import OrderedDict

import ipc

from PySide6.QtCore import (
    Qt, QUrl, QSize, QProcess, QTimer, QFileInfo, QMimeDatabase,
    QStandardPaths,
)
from PySide6.QtGui import (
    QAction, QFont, QGuiApplication, QImage, QKeySequence,
    QMovie, QPainter, QPixmap, QShortcut, QColor, QDesktopServices,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QFileIconProvider, QFrame, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QSizePolicy, QSlider, QStackedLayout,
    QVBoxLayout, QWidget, QGraphicsDropShadowEffect,
)

SOCKET_PATH = ipc.socket_path()
TEXT_PREVIEW_LIMIT = 1024 * 1024  # 1 MiB

TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".log", ".ini", ".cfg", ".conf", ".toml",
    ".yaml", ".yml", ".json", ".xml", ".html", ".htm", ".css", ".js",
    ".ts", ".py", ".r", ".sh", ".bash", ".zsh", ".c", ".h", ".cpp",
    ".hpp", ".rs", ".go", ".java", ".kt", ".rb", ".pl", ".lua", ".sql",
    ".csv", ".tsv", ".tex", ".bib", ".desktop", ".service", ".env",
}


APP_DIR = os.path.dirname(os.path.abspath(__file__))
RENDER_HELPER = os.path.join(APP_DIR, "render.py")

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

def cache_key(path: str, st: os.stat_result, max_w: int, max_h: int) -> str:
    raw = f"{path}\0{st.st_mtime_ns}\0{st.st_size}\0{max_w}x{max_h}"
    return hashlib.sha256(raw.encode()).hexdigest() + ".png"


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


def build_render_command(path: str, max_w: int, max_h: int) -> list | None:
    """Command line that decodes an image out of process (None = refused).

    The daemon never decodes untrusted *static images* in-process (animated
    GIFs, PDFs and audio/video are different — see the README): render.py
    runs under bubblewrap with read-only /usr + this app dir + the single
    target file, no network (--unshare-all) and no writes — the PNG comes
    back on stdout and the *daemon* writes the cache. Without bwrap we
    refuse, unless QUICKVIEW_ALLOW_UNSANDBOXED=1 (still out of process,
    NOT sandboxed).
    """
    global _warned_no_bwrap
    helper = [sys.executable, RENDER_HELPER, path, str(max_w), str(max_h)]
    bwrap = shutil.which("bwrap")
    if bwrap:
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
            "--ro-bind", path, path,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--setenv", "QT_QPA_PLATFORM", "offscreen",
            "--setenv", "HOME", "/tmp",
            "--setenv", "XDG_RUNTIME_DIR", "/tmp",
            "--",
        ] + helper
    if os.environ.get("QUICKVIEW_ALLOW_UNSANDBOXED") == "1":
        return helper
    if not _warned_no_bwrap:
        _warned_no_bwrap = True
        log.warning(
            "bwrap not found — refusing to decode untrusted files "
            "(install bubblewrap, or set QUICKVIEW_ALLOW_UNSANDBOXED=1 "
            "to accept the risk)"
        )
    return None


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

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.addWidget(self.close_btn)
        lay.addWidget(self.title, 1)
        lay.addWidget(self.open_btn)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._window.windowHandle().startSystemMove()
        super().mousePressEvent(event)


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
        self.movie = None
        self.player = None
        self.audio_out = None
        self._mem_cache = OrderedDict()  # key -> (pixmap, dims, nbytes)
        self._render_proc = None

        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 160))
        self.panel.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 32)
        outer.addWidget(self.panel)

        self.titlebar = TitleBar(self)
        self.content = QStackedLayout()
        self.content.setContentsMargins(0, 0, 0, 0)

        panel_lay = QVBoxLayout(self.panel)
        panel_lay.setContentsMargins(1, 0, 1, 1)
        panel_lay.setSpacing(0)
        panel_lay.addWidget(self.titlebar)
        panel_lay.addLayout(self.content, 1)

        self.setStyleSheet("""
            #panel {
                background-color: rgba(34, 34, 38, 245);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 30);
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
                background-color: rgba(255, 255, 255, 26); color: #e8e8ea;
                border: none; border-radius: 6px; padding: 4px 14px;
                font-size: 12px;
            }
            #openBtn:hover { background-color: rgba(255, 255, 255, 45); }
            QPlainTextEdit {
                background-color: transparent; color: #dcdcde;
                border: none; padding: 8px 14px;
                font-family: monospace; font-size: 12px;
            }
            QLabel { color: #dcdcde; }
            QSlider::groove:horizontal {
                height: 4px; background: rgba(255,255,255,40); border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 12px; margin: -4px 0; border-radius: 6px; background: #e8e8ea;
            }
        """)

        for keys, fn in (
            (Qt.Key_Space, self.dismiss),
            (Qt.Key_Escape, self.dismiss),
            (Qt.Key_Q, self.dismiss),
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

    def dismiss(self):
        """Hide the preview but keep the process resident for instant reuse."""
        self.clear_content()
        self.hide()

    def closeEvent(self, event):
        event.ignore()
        self.dismiss()

    # ---------------------------------------------------------------- helpers

    def screen_avail(self) -> QSize:
        screen = self.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry().size()

    def set_panel_size(self, w: int, h: int):
        avail = self.screen_avail()
        w = min(max(w, 480), int(avail.width() * 0.85))
        h = min(max(h, 320), int(avail.height() * 0.85))
        self.resize(w + 48, h + 56 + 40)
        self.center_on_screen()

    def center_on_screen(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width() - self.width()) // 2,
            geo.y() + (geo.height() - self.height()) // 2,
        )

    def _cancel_render(self):
        proc = self._render_proc
        self._render_proc = None
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            proc.kill()

    def clear_content(self):
        self._cancel_render()
        if self.player is not None:
            self.player.stop()
            # The media pipeline keeps emitting positionChanged after stop();
            # sever every connection so a queued emission can't reach the
            # slider/label lambdas after deleteLater destroys their widgets.
            self.player.disconnect()
            self.player.deleteLater()
            self.player = None
            self.audio_out = None
        if self.movie is not None:
            self.movie.stop()
            self.movie = None
        while self.content.count():
            w = self.content.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

    def open_externally(self):
        if self.current_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.current_path))
            self.dismiss()

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

        if not os.path.exists(path):
            self.show_message(f"File not found:\n{path}")
        elif os.path.isdir(path):
            self.show_folder(path)
        else:
            mime = self.mime_db.mimeTypeForFile(path).name()
            ext = os.path.splitext(path)[1].lower()
            # This routing is the sandbox enforcement point. Every branch
            # below either decodes out of process (show_image), reads plain
            # bytes (text/fallback), or uses an in-process parser — GIF
            # (QMovie), PDF (QtPdf), media (QtMultimedia) — which only
            # `inprocess` permits. With QUICKVIEW_STRICT_SANDBOX=1 a GIF
            # falls through to the sandboxed still-image path and PDF/media
            # fall through to the metadata card.
            inprocess = os.environ.get("QUICKVIEW_STRICT_SANDBOX") != "1"
            if mime == "image/gif" and inprocess:
                self.show_gif(path)
            elif mime.startswith("image/"):
                self.show_image(path)
            elif mime == "application/pdf" and inprocess:
                self.show_pdf(path)
            elif mime.startswith(("video/", "audio/")) and inprocess:
                self.show_media(path, video=mime.startswith("video/"))
            elif mime.startswith("text/") or ext in TEXT_EXTENSIONS:
                self.show_text(path)
            else:
                self.show_fallback(path, mime)

        self.show()
        self.raise_()
        self.activateWindow()

    def show_message(self, text: str):
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        self.content.addWidget(label)
        self.set_panel_size(520, 320)

    def show_image(self, path: str):
        avail = self.screen_avail()
        max_w, max_h = int(avail.width() * 0.85) - 48, int(avail.height() * 0.85) - 96
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

        cmd = build_render_command(path, max_w, max_h)
        if cmd is None:
            # Decode refused (no bwrap) — metadata card.
            self.show_fallback(path, self.mime_db.mimeTypeForFile(path).name())
            return

        # Decode asynchronously: a slow or hostile file must not freeze the
        # event loop — keys, the close button and the daemon socket stay
        # live while the sandboxed helper works.
        self.show_message("Loading preview…")
        proc = QProcess(self)
        self._render_proc = proc
        watchdog = QTimer(proc)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(proc.kill)

        def finished(code, _status):
            stale = self._render_proc is not proc
            if not stale:
                self._render_proc = None
            png = bytes(proc.readAllStandardOutput())
            err = bytes(proc.readAllStandardError()).decode(
                "utf-8", errors="replace"
            )
            proc.deleteLater()
            if stale or path != self.current_path:
                return  # the user moved on while we rendered
            img = (
                QImage.fromData(png)
                if code == 0 and png[:4] == PNG_MAGIC
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
                log.warning(
                    "render helper failed (rc=%s): %s",
                    code, err.strip()[:500],
                )
                self.clear_content()
                self.show_fallback(
                    path, self.mime_db.mimeTypeForFile(path).name()
                )

        proc.finished.connect(finished)
        proc.start(cmd[0], cmd[1:])
        watchdog.start(20000)

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

    def show_gif(self, path: str):
        self.movie = QMovie(path)
        if not self.movie.isValid():
            self.show_image(path)
            return
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        label.setMovie(self.movie)
        self.movie.start()
        size = self.movie.frameRect().size()
        self.content.addWidget(label)
        self.set_panel_size(size.width() + 24, size.height() + 24)

    def show_pdf(self, path: str):
        from PySide6.QtPdf import QPdfDocument
        from PySide6.QtPdfWidgets import QPdfView

        view = QPdfView()
        # Parent the document to the view, not the window: clear_content()
        # deletes the view, and the loaded document must go with it instead
        # of accumulating on the long-lived daemon window.
        doc = QPdfDocument(view)
        doc.load(path)
        view.setDocument(doc)
        view.setPageMode(QPdfView.PageMode.MultiPage)
        view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        view.setStyleSheet("QPdfView { border: none; background-color: #2a2a2e; }")
        self.content.addWidget(view)
        avail = self.screen_avail()
        self.set_panel_size(int(avail.width() * 0.55), int(avail.height() * 0.85))
        self.set_title(f"{os.path.basename(path)}  —  {doc.pageCount()} pages")

    def show_media(self, path: str, video: bool):
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget

        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.setSpacing(6)

        # The closures below capture `player` directly: clear_content() nulls
        # self.player while late signal emissions may still be in flight, and
        # an AttributeError on None is not an acceptable way to find out.
        player = QMediaPlayer(self)
        self.player = player
        self.audio_out = QAudioOutput(self)
        player.setAudioOutput(self.audio_out)

        if video:
            surface = QVideoWidget()
            player.setVideoOutput(surface)
            lay.addWidget(surface, 1)
        else:
            icon = self.icon_provider.icon(QFileInfo(path))
            art = QLabel()
            art.setAlignment(Qt.AlignCenter)
            art.setPixmap(icon.pixmap(128, 128))
            lay.addWidget(art, 1)

        controls = QHBoxLayout()
        controls.setContentsMargins(14, 0, 14, 0)
        play_btn = QPushButton("▶")
        play_btn.setObjectName("openBtn")
        play_btn.setFixedWidth(40)
        slider = QSlider(Qt.Horizontal)
        time_lbl = QLabel("0:00 / 0:00")
        controls.addWidget(play_btn)
        controls.addWidget(slider, 1)
        controls.addWidget(time_lbl)
        lay.addLayout(controls)

        def fmt(ms):
            s = ms // 1000
            return f"{s // 60}:{s % 60:02d}"

        def toggle():
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                player.pause()
                play_btn.setText("▶")
            else:
                player.play()
                play_btn.setText("⏸")

        play_btn.clicked.connect(toggle)
        player.durationChanged.connect(lambda d: slider.setRange(0, d))
        player.positionChanged.connect(
            lambda p: (
                slider.blockSignals(True),
                slider.setValue(p),
                slider.blockSignals(False),
                time_lbl.setText(f"{fmt(p)} / {fmt(player.duration())}"),
            )
        )
        slider.sliderMoved.connect(player.setPosition)

        self.content.addWidget(wrap)
        avail = self.screen_avail()
        if video:
            self.set_panel_size(int(avail.width() * 0.6), int(avail.height() * 0.65))
        else:
            self.set_panel_size(520, 320)

        player.setSource(QUrl.fromLocalFile(path))
        player.play()
        play_btn.setText("⏸")

    def show_text(self, path: str):
        try:
            with open(path, "rb") as fh:
                data = fh.read(TEXT_PREVIEW_LIMIT + 1)
        except OSError as exc:
            self.show_message(str(exc))
            return
        truncated = len(data) > TEXT_PREVIEW_LIMIT
        text = data[:TEXT_PREVIEW_LIMIT].decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[... truncated ...]"
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(text)
        edit.setFrameShape(QFrame.NoFrame)
        self.content.addWidget(edit)
        avail = self.screen_avail()
        self.set_panel_size(int(avail.width() * 0.5), int(avail.height() * 0.75))

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
    sock.write(ipc.encode_paths(paths))
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
            new_paths = ipc.decode_paths(bytes(buf))
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
                viewer.dismiss()
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
    if paths:
        viewer.show_files(paths)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
