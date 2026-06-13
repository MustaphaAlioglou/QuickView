# QuickView vs. macOS Quick Look — gap analysis

- **Date:** 2026-06-12
- **Baseline:** working tree after the 2026-06-12 review fixes (ipc.py, guard
  timer, prune counter, install restart)
- **Question:** what does macOS Quick Look do that QuickView doesn't, and
  what's worth building?

## Easy wins (a day or less each, mostly already in Qt)

- **Rendered Markdown** — Quick Look shows `.md` as plain text, but
  `QTextDocument.setMarkdown()` is built into Qt, so QuickView could beat
  macOS here for nearly free. Same for rendered HTML via `QTextBrowser`.
- **Syntax highlighting for code** — macOS needs a third-party plugin
  (QLColorCode); dropping Pygments into the venv covers it in the existing
  text view.
- **Archive contents** — stdlib `zipfile`/`tarfile` can list a zip/tar like
  the existing folder view (name, size, count). macOS only shows an icon
  without plugins.
- **Font previews** — `QFontDatabase.addApplicationFont` + a specimen string
  ("The quick brown fox…" at several sizes). macOS has this natively; it's a
  visible gap.
- **"Open with Gwenview" instead of "Open"** — Quick Look's button names the
  default app. On KDE, resolve via `xdg-mime query default` plus the
  `.desktop` file's `Name=`.
- **Resizable, size-remembering window** — Quick Look windows resize freely
  and remember their size. The title bar already uses `startSystemMove()`;
  `startSystemResize()` on edge hit-zones gives frameless resizing, and
  last-size can persist in `QSettings`.

## Medium (the interactions that make Quick Look feel polished)

- **Zoom and pan in images** — scroll-wheel/pinch zoom and drag-to-pan.
  Today the pixmap is static and capped at 85% of the screen, so a large
  image can never be viewed 1:1. Probably the most-felt daily difference.
  Catch: render.py downscales before caching, so true 1:1 zoom needs the
  helper to also return (or cache) a larger decode.
- **Full-screen toggle** — macOS has ⌥Space / the expand button. An `F` key
  flipping `showFullScreen()` is cheap once resizing works.
- **Index/grid view for multi-selection** — ⌘Return in Quick Look shows all
  selected files as a thumbnail grid. The disk PNG cache already exists, so
  a `QListView` in IconMode over cached thumbnails is realistic.
- **Open/close animation** — Quick Look zooms out from the file icon. The
  icon's screen position isn't available from Dolphin, but a quick
  fade+scale-in (`QPropertyAnimation` on opacity/geometry) would remove the
  current "pop".
- **HEIC and camera RAW** — iPhone photos and RAW previews work out of the
  box on macOS. On Arch this is mostly packaging: `kimageformats` /
  `qt6-imageformats` give QImageReader the plugins, and the sandboxed helper
  picks them up automatically.

## Hard / judgment calls

- **Preview follows the file-manager selection** — in Finder, arrow keys
  move the selection *and* the open preview follows. Biggest structural gap
  and there's no clean fix: Dolphin exposes no selection-changed signal over
  DBus. The ←/→ paging is the honest workaround; document it as such and
  don't chase this.
- **Office/iWork documents** — macOS previews .docx/.xlsx natively. The
  realistic route is `libreoffice --headless --convert-to pdf` into the
  cache: slow on first view (seconds) but cacheable, and a large new attack
  surface relative to the current sandbox story.
- **Markup/edit actions** (rotate, crop, annotate, trim) and the **share
  button** — these lean on system frameworks on macOS; on KDE they'd be
  mini-apps rebuilt from scratch. Skip; `Enter` to open the real app covers
  it.

## Suggested order (by daily impact)

1. Image zoom/pan
2. Resizable + size-remembering window
3. Markdown rendering + syntax highlighting (cheap, visible)
4. "Open with <app>" button label
5. HEIC/RAW via kimageformats packaging
