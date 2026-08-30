# Changelog

Notable changes to QuickView. Format follows [Keep a Changelog][kac]; the
project has no version tags yet, so entries are dated.

[kac]: https://keepachangelog.com/en/1.1.0/

## 2026-08-30

### Added

- **A Breeze panel theme**, behind `panel_theme = breeze` in the new
  `[appearance]` section. The panel has always been a Quick Look impression
  — a fixed dark window, close button top left, indifferent to the Plasma
  colour scheme — and that is still the default and still the point. The
  alternative reads every colour from the running `QPalette`, so it follows
  the user's own scheme, light or dark, accent included, rather than being a
  second hardcoded theme that happens to resemble today's Breeze. It also
  squares the corners and moves the close button to the right.

  The colours used to be written into eleven stylesheets as some thirty
  literals. They now go through one set of tokens in `theme.py`, which is
  what makes a second theme possible at all — and the default palette is
  those same literals, so the default look is unchanged: a test compares the
  built stylesheet against the previous one declaration by declaration.

- **Playback speed and mute** on video and audio, at 0.25x through 2x. Both
  are new ops on the jailed player, and the rate is clamped worker-side.

- **Photoshop previews (`.psd`, `.psb`).** Qt ships no PSD handler, so these
  used to fail with "unsupported or corrupt" — a .psd matches `image/*` and
  went to the image path, which could not read it. QuickView now parses the
  format itself, in the jail with every other parser and using nothing but
  the standard library.

  It reads the flattened composite Photoshop already stores next to the
  layers and never looks at a layer, so the expensive half of the format is
  the half a preview skips. The compressed image data is preceded by a table
  of every scanline's length, which means rows can be *seeked past* rather
  than decoded — so the cost tracks the size of the preview rather than the
  size of the document, the same bargain the image path already strikes with
  libjpeg's DCT scaling. On a 6000x4000 file, 95 ms against 281 ms for the
  same decode undecimated, and the gap widens with the document. How busy the
  artwork is matters more than how big it is: dithered or high-frequency
  two-colour scanlines code as thousands of short PackBits opcodes rather
  than dozens of long ones and cost around 25x as much to walk, so the same
  12 megapixels range from 95 ms to 733 ms.

  RGB, greyscale, indexed, CMYK and duotone all decode, at 8 or 16 bits, in
  PSD and PSB, RLE-compressed or raw. CMYK is converted by drawing the black
  plate over the other three with a multiply blend, which is exactly the
  formula and leaves the per-pixel work to Qt. A file saved without "Maximize
  Compatibility" has no composite at all and falls back to the JPEG thumbnail
  in its image resources; Lab and 1-bit bitmaps do the same.

  Because the fallback lives inside `decode_image` rather than in a branch of
  its own, PSD inherits the two-tier cache, the raw-ARGB32 wire frame, the
  neighbour prefetch and the document's real dimensions in the titlebar
  without any of them knowing the format exists.

- **Krita and OpenRaster previews (`.kra`, `.ora`).** Both are a zip holding
  the flattened image as an ordinary PNG, which Krita writes precisely so a
  thumbnailer never has to understand a layer stack. Reading that one member
  is the whole decoder, and it is handed back to the image path so it gets
  the scaled read and the dimensions like any other PNG. `preview.png` is the
  fallback for a file saved without the merged image.

- **Illustrator previews (`.ai`).** An `.ai` has been a PDF since Illustrator
  9, so it is handed to the PDF page view and streams, scrolls, searches and
  caches identically. The daemon reads four bytes to check for `%PDF` first —
  a sniff, not a parse, so no decoder moves back into the daemon — and a
  PostScript-only `.ai` from before that still gets the metadata card.

### Fixed

- **Clicking the seek bar seeks there.** A QSlider's groove is a page-step
  control by default, so a click moved the handle a fixed nudge towards the
  cursor and dragging it was the only way to get anywhere. The whole
  timeline is now the target, as it is in every media player. Position
  reports from the player are also ignored while the handle is held, which
  stops it snapping back mid-drag.

### Security

- The PSD reader is the first format QuickView parses by hand, so every
  length field in it is checked against the real file size before anything is
  allocated or skipped, and the canvas, channel count, resource walk,
  thumbnail resource and decimated result are each capped. The zip member a
  `.kra` preview comes from is bounded both by its declared size and by what
  it actually yields, so a lying header buys nothing. A malformed file still
  only takes down its own throwaway worker.

## 2026-08-27

### Added

- **EPUB previews.** Books used to get the metadata card. They are now laid
  out as pages by the same pipeline PDFs and office documents use: the spine
  is converted to the HTML subset `QTextDocument` understands and inserted
  chapter by chapter through a cursor, with each spine document starting a
  new page the way a chapter does in print. No reader engine is involved and
  nothing new runs outside the jail.
- **Markdown renders**, on the same titlebar button HTML uses: rendered
  pages by default, the highlighted source behind **Code**, and the choice
  is remembered for the session. Qt parses Markdown itself (md4c, behind
  `QTextDocument.setMarkdown`), so this needs no library and no HTML
  engine — the parse runs in the jail like every other one, a `markdown`
  op streams page images, and they scroll and cache exactly like a PDF's.
  Tables, task lists and fenced code all render, and the page palette
  (`book_theme`) applies here too, so Markdown is themed with the books.

  **Its headings are the sidebar**, nested by their hashes and carrying the
  page each lands on, on the same ☰ button as a PDF's bookmarks and a
  book's chapters. A Markdown file has no table of contents of its own, so
  its headings are it.

  Three things happen to a document before it is painted, all of them
  fixes for things that looked broken:

  - **Raw HTML tags are stripped outside code.** Qt hands an HTML block to
    its rich-text importer mid-parse, and an unbalanced one — a
    `<p align="center">` wrapping two `<img>` tags, the way half of
    GitHub's READMEs open — swallows the rest of the document. This
    project's own README imported as 2,420 characters of the 22,222 it
    has, with every paragraph after the block silently gone. Fenced blocks
    and inline code keep their tags, so a README documenting HTML still
    shows it.
  - **Images are replaced by a note naming them.** A Markdown file's images
    sit next to it on disk and the jail has no disk — the file arrives as a
    descriptor. Qt resolves those relative paths against the worker's
    working directory, so an image loaded only when the document happened
    to live inside this application's own folder: right for the project's
    README, wrong for every file a person actually previews.
  - **Fenced code is allowed to wrap.** Qt imports it as unbreakable lines,
    and a page cannot scroll sideways: a long line was simply cut off at
    the margin. Wrapping reads slightly wrong; losing half the line reads
    as a bug.

### Fixed

- **Clicking a contents entry scrolled to the page, not to the heading.** A
  rendered page here is routinely taller than the window — 2326 px against
  a 1451 px viewport on a 3072×1728 screen — so an entry that carried only
  a page number scrolled to the top of that page: every section of one
  chapter went to the same place, and clicking one whose heading was
  already on screen did nothing at all. Entries now carry where on the page
  they sit, taken from the bookmark's own destination for a PDF and from
  the layout that produced the pages for a book or a Markdown file, and the
  view scrolls there. Contents cached by the previous version are
  superseded rather than reused, since they have no offsets in them.
- **A cached document with no cached contents re-rendered on every open.**
  The contents entry is now written even when a document has no headings at
  all, so "none" is stored as an answer rather than read back as "unknown".
  Affected books and Markdown files alike.

- **Ctrl+F works in EPUB previews**, with the same highlighting, the same
  Enter / Shift+Enter stepping and the same match counter as a PDF. The
  route there is different: a book has no text layer to extract, so a new
  `epubsearch` op lays the book out again in the jail — same page width,
  same style sheet, so the same layout the pages were painted from — and
  measures each match against it line by line. What comes back is what the
  PDF search returns, `{"matches": [{"page", "rects"}], "capped"}`, so the
  daemon paints both with the code it already had. A match that wraps gets
  one rectangle per line rather than one slab across the gap, and a match
  on a page past the render cap is dropped rather than highlighted where
  nobody can scroll.

  Laying the book out twice (once to render, once to search) is deliberate:
  a rectangle only means something in a layout, and the alternative — the
  daemon holding a parsed book — is exactly what the jail exists to
  prevent. Office documents still have no find, for the same reason they
  never did: nothing re-lays them out to search.
- **`book_theme`, a page palette for EPUB previews**: `paper` (the default),
  `sepia`, `dark`, `gruvbox-dark` and `gruvbox-light`. The worker paints the
  page background, body text, headings and quotations from the chosen
  palette — only the page, never the panel around it, which is application
  chrome. Books are also laid out in a serif at 11pt now, matching the
  office path. The theme name goes into the page cache key, so switching
  themes re-renders instead of serving back the old theme's pages, and an
  unknown name in the config file reads as the default rather than as a
  book with no colours at all.
- **A contents sidebar, on a ☰ button top left.** It lists a PDF's bookmarks
  or a book's chapters — nested, with page numbers — and clicking an entry
  scrolls to that page. The button appears only for documents that have a
  table of contents, and the sidebar stays open across files once opened.

  The two sources are different but the sidebar is not. A PDF's outline
  comes from a new `pdfoutline` op that walks Qt's bookmark model in the
  jail; a book's chapters come out of the layout pass that produced its
  pages (the cursor position where each chapter was inserted is what makes
  a page number possible), so they ride on the render's header instead of
  costing a second parse. Both are cached next to the page images, so
  reopening a document brings its sidebar back without a worker. Entries
  pointing past the last rendered page are dropped rather than shown as
  rows that scroll nowhere.

  Both kinds of EPUB table of contents are read — the EPUB 3 navigation
  document and the EPUB 2 NCX — and a book with neither still gets a
  chapter list, built from each spine file's first heading. A contents
  entry pointing at a fragment *inside* a file splits that file at the
  fragment, so books that keep several chapters in one document still land
  on the right page.

- **Spreadsheets are previewed as a grid, with a tab per sheet.** `.xlsx`,
  `.xlsm` and `.ods` used to be poured into the office page view, which
  turned a workbook into an undifferentiated wall of bordered rows labelled
  "Sheet 1", "Sheet 2" — the sheets' real names were never read. They now go
  to a new `sheets` worker op that reads the cells in the jail, and the
  daemon shows them in a table: the sheet names on tabs along the bottom,
  the workbook's own column letters and row numbers in the gutters, numeric
  columns aligned right, and the header row picked out when the first row
  looks like one. Hidden sheets are skipped, as they are in Excel.

  Cells are now placed by their own reference rather than by the order they
  appear in, so a sparse row — one that omits its empty cells, which is what
  the format writes — keeps its columns instead of sliding every value to
  the left. Dates and percentages in xlsx are ordinary numbers wearing a
  number format, so `styles.xml` is read as well, including custom format
  codes; without it every date in a workbook showed as a five-digit serial.
  Floats print rounded (`15.7`, not `15.700000000000001`).

  Each sheet is bounded — 2000 rows, 64 columns, 512 characters a cell, 24
  sheets — so a million-row workbook costs what a small one does. A file
  that will not parse as a grid still falls back to the office page view,
  and from there to the metadata card.

## 2026-08-26

### Added

- **A test suite**: `python -m unittest discover -s tests`. 51 checks in
  about a second, no display required — the wire format (including the
  non-UTF-8 filenames that used to fail silently), the settings parser, the
  raw-frame bounds guard, cache encoding and keys, and the PDF search's text
  flattening and geometry.
- **`uninstall.sh`**, which reverses what `install.sh` did: the daemon and
  its unit, the Dolphin service menu, the launcher on `PATH`, the cache and
  the logs. Settings are kept unless `--purge` is passed, and the checkout is
  never touched.
- **A settings file at `~/.config/quickview/quickview.conf`**, written with
  its defaults commented in the first time the daemon starts and never
  rewritten afterwards. It carries the syntax-highlighting style, the text
  preview limit, the PDF page cap and both cache sizes; each still has a
  `QUICKVIEW_*` environment variable that wins over the file. Previously the
  one setting that existed had to go in the systemd unit's `Environment=`,
  which `install.sh` regenerates on every run — so it was silently lost on
  the next upgrade. Out-of-range values are clamped and an unparseable file
  falls back to the defaults. The many other constants are safety bounds on
  untrusted input and stay in the code deliberately.
- **Ctrl+F finds text in a PDF preview.** Matches are highlighted in place
  and Enter / Shift+Enter step through them, scrolling each into view. The
  daemon owns no PDF parser, so the query goes to a jailed worker (a new
  `pdfsearch` op) that extracts the text and answers with match rectangles
  already in page pixels — the daemon only paints them. Multi-word phrases
  work, including across a line break: a PDF's text layer breaks lines with
  `\r\n`, so the page text is matched with whitespace collapsed and each
  match is highlighted with one rectangle per line it spans rather than one
  box swallowing the gap between them. Some PDFs position their glyphs
  instead of emitting spaces, and Qt extracts those as
  "accordingtoyoursoftskills"; when an exact multi-word search finds
  nothing, it retries ignoring whitespace altogether and marks the result
  `≈` so an approximate hit is never mistaken for a literal one. Search
  covers the pages the viewer actually shows (the first 50), because a hit
  reported on page 73 of a document that stops rendering at 50 is worse
  than silence. Office documents share the PDF view but are laid out through
  `QTextDocument`, so they do not get the find row.

### Performance

- **Photo previews open about 2.4x faster: ~338 ms to ~127 ms** (median of
  15 cold renders across five photos, measured from the daemon's own
  `preview:` -> `rendered:` log). The worker used to PNG-compress every
  decoded image to hand it down a Unix socket to a process that immediately
  decompressed it — ~107 ms to write and ~31 ms to read back, more than the
  JPEG decode in front of it. It now sends raw ARGB32 pixels (~0.4 ms each
  way) and the daemon paints as soon as they land. The copy the disk cache
  wants still gets made, but in the jailed worker *after* the image is on
  screen, so it is off the user-visible path. BMP would have been the
  obvious middle ground and is wrong: Qt's BMP writer drops the alpha
  channel, so transparent PNGs and SVGs would have come back opaque.
- **Reopening a photo is ~3.4x faster: ~54 ms to ~16 ms**, and the disk
  cache holds ten times as much. Preview images with no alpha are cached as
  JPEG rather than PNG — ~24 ms to write and ~11 ms to read back against
  ~111 and ~37, at a tenth of the size, so the 256 MB cap now fits over a
  thousand previews instead of ~110. Images that carry alpha stay PNG,
  because JPEG has none and a transparent logo would come back on a black
  square. The `QuickView:OrigSize` tEXt chunk survives as a JPEG comment
  marker, so the cache-hit path is unchanged, and existing PNG entries stay
  readable.
- Prefetching a neighbouring image no longer ships pixels it throws away —
  it warms the disk cache and displays nothing, so it asks for the encoded
  frame alone.
- **The fast path is now a compiled client (`client.rs`).** Handing a path
  to the warm daemon cost ~29 ms, essentially all of it Python interpreter
  startup — `urllib.parse` alone was ~6 ms of it. The Rust client does the
  same work in ~0.8 ms, so `bin/quickview somefile` now returns in ~4 ms
  end to end instead of ~40 ms. No crates, so `rustc` builds it without
  Cargo; `install.sh` skips it with a note when there is no toolchain, and
  `client.py` stays as the fallback (itself now ~15 ms, having shed its
  `urllib` import).

### Security

- **The fast-path client no longer parses anything.** Percent-decoding and
  path normalization moved from the client to `ipc.py` on the daemon side,
  so the one QuickView process that runs *outside* the bubblewrap jail now
  forwards its arguments byte for byte and never inspects them. Filenames
  are untrusted input — a crafted name inside a downloaded archive is a
  plausible vector — and this keeps every parser on the memory-safe side of
  the wire. It also leaves `normalize_arg` with a single implementation,
  which is what its docstring always asked for.

### Fixed

- **Opaque images are cached as JPEG again.** The format check used
  `hasAlphaChannel()`, which answers about the image *format* and not its
  pixels — so screenshots and most PNGs, which decode to ARGB32 while being
  fully opaque, were cached as multi-megabyte PNGs. The pixels are now
  actually checked (~2 ms, in the worker, after the image is on screen): one
  sample PNG's cache entry went from 190 KB to 76 KB.
- **Preview cache entries now carry a format version.** Keys were built from
  the source file's path, mtime, size and fit box — nothing about *how* it
  was rendered — so changing a renderer left the old output being served for
  ever. The PDF path had been working around this with hand-bumped `v2`
  strings in its variant names; a single `CACHE_VERSION` now covers every
  entry.
- **Filenames that are not valid UTF-8 now preview instead of silently
  failing.** The wire format decoded with `errors="replace"`, which turned
  an undecodable byte into U+FFFD — a path that does not exist. Both
  directions now use `surrogateescape`, the codec `sys.argv` and `open()`
  already speak, so those bytes round-trip intact.

### Changed

- The client→daemon message gained a leading field: the sender's working
  directory, which the daemon resolves relative arguments against (its own
  cwd, under systemd, is not the client's). An old daemon left running
  across an upgrade would misread the new format; `install.sh` already
  restarts the service rather than leaving a running one alone.

## 2026-08-25 — first public release

The release that took QuickView from a personal tool to a published one:
sandboxed rendering for every format, a window that behaves itself on
Wayland, and an installer that works on a machine other than the author's.

### Security

- **Every format QuickView decodes itself is now parsed outside the
  daemon.** Images, PDFs, animations, audio, video and the syntax
  highlighter all run in a bubblewrap jail (HTML is the exception, and
  stays in Chromium's own renderer sandbox); the daemon
  keeps the window, the socket and the cache, and holds no decoder. The
  previously documented exceptions — animated GIFs (`QMovie`) and
  audio/video (`QMediaPlayer`) — are gone.
- **The jail no longer has access to user files at all.** The target file
  arrives as a file descriptor over `SCM_RIGHTS`, so there is nothing to
  bind-mount. Verified from inside a worker: `/etc/passwd` and
  `$XDG_RUNTIME_DIR` unreachable, the home directory shows only the bind
  skeleton (1 visible entry where the real directory has 32), and
  `connect()` fails with `ENETUNREACH`.
- Added `--cap-drop ALL` to the jail.
- Bounded the frame-length field in the worker stream. It came from the
  helper and was buffered unchecked, so a compromised helper could ask the
  daemon to allocate 4 GiB; frames over 64 MiB now kill the worker.
- `QUICKVIEW_STRICT_SANDBOX=1` now only governs HTML (source view instead
  of QtWebEngine). GIF and media no longer need it — they are always
  jailed.

### Added

- **Archives and office documents preview instead of falling through to the
  metadata card.** Archives (zip, rar, 7z, tar and friends) show a contents
  listing with sizes; nothing is ever extracted, only headers are read, so a
  zip bomb is inert. zip and tar go through Python's standard library, and
  everything else through `bsdtar`/`7z`/`unrar` — which read the archive from
  `/dev/fd`, so the jail still gets a descriptor and no path, and no copy is
  made. Missing listers, encrypted headers and corrupt files fall back to the
  metadata card without an error.

  Office documents are **laid out as pages** and shown by the same view PDFs
  use, so they scroll, stream and cache page by page. docx, odt, xlsx and ods
  are converted in the jail to the HTML subset `QTextDocument` understands —
  headings, bold/italic, tables, embedded images — and rendered with Qt
  alone: no office suite, no QtWebEngine. Measured: a 7-page .docx converts
  in 8 ms and renders a page in 7 ms.

  Slide decks are deliberately not previewed: their content is absolutely
  positioned graphics with no path through QTextDocument, and the only
  thing that lays them out is a full office suite. Legacy binary
  .doc/.xls/.ppt are out for the same reason; both show the metadata card.
- **Syntax highlighting in the text preview**, via Pygments — and it runs in
  the jail, not here. Lexers are regexes; a file written to make one
  backtrack should cost a throwaway worker, not the process holding the
  window and the IPC socket. The worker returns the text plus
  `[start, length, colour]` spans, and the daemon paints ranges: no lexer
  and no HTML parser on this side. Text previews therefore go through a
  worker now, with the old in-process read (`show_text_direct`) kept as the
  fallback for when the sandbox is unavailable.

  Files over 256 KiB stay plain — 83 KiB of Python lexes in ~245 ms, but
  1 MiB takes ~1.5 s, slower than the preview it decorates. Pick any
  Pygments style with `QUICKVIEW_CODE_STYLE` (default `one-dark`); with
  Pygments absent the preview is simply uncoloured.
- `worker.py` — warm jailed worker for images, PDF pages and animation
  frames, decoding from a passed descriptor.
- `media_worker.py` — jailed audio/video player. It owns the audio clock
  (PipeWire socket bound into the jail), so Qt does A/V sync in there;
  video frames cross to the daemon through a shared `memfd` and the daemon
  only blits them. Play/pause/seek are socket messages.
- `renderers.py` — the decoders themselves, shared by the workers and the
  standalone scripts.
- Worker pool with pre-booted hot spares, so the ~150 ms Qt import is paid
  before a preview needs it rather than during one. Each worker still
  handles exactly one file and then exits.
- Animation playback without an animation parser in the daemon: frames are
  decoded in the jail and cycled as pixmaps on a timer, bounded at 512
  frames / 256 MiB.
- Debug log line recording who sized the preview window and against which
  screen, for diagnosing a window that opens at the wrong size.

### Changed

- **Previews are faster than before they were sandboxed.** Measured on this
  machine: PDF page 1 on a cache miss 583 ms → 122 ms, image on a cache
  miss ~150 ms → 56 ms, cache hit ~20 ms → 2 ms, prefetched neighbour 2 ms.
  Entering the jail costs ~3 ms; the old cost was importing Qt per file.
- The window is fully opaque. The panel was `rgba(34, 34, 38, 245)` and the
  hairline border was translucent white; both are now solid colours, as are
  the button, slider and text backgrounds. `WA_TranslucentBackground` stays
  on — it is what makes the rounded corners and drop shadow composite —
  but nothing shows through the panel.
- Text previews are read on a pool thread — now the fallback path, since
  text normally goes through the jail — so a file on a stalled NFS or
  FUSE mount can no longer freeze the window and the daemon socket. The
  window is sized for text up front instead of being resized after the read
  lands.
- `render.py` and `render_pdf.py` are now thin CLI wrappers over
  `renderers.py`, kept for reproducing a render by hand. The daemon talks
  to `worker.py` instead.
- README rewritten around the new boundary, including an honest list of
  what still runs in the daemon: HTML (in Chromium's own renderer sandbox),
  text bytes, MIME sniffing, and the daemon decoding the workers' PNG
  output.

### Fixed

- **A text preview could crash the daemon.** `FileReader` was a
  `QRunnable` the daemon held exactly one reference to, so previewing a
  second file while the first read was still blocked (the stalled-mount
  case the reader thread exists for) dropped the last reference to a
  running object — and the pool touches a runnable again after `run()`
  returns. It is a plain `QObject` now, handed to the pool as a bound
  method, and every in-flight reader is held until its signal lands.
- **Dismissing a media preview could hang the whole daemon.** `stop()`
  waited on the killed worker with no timeout on the GUI thread, so a
  worker wedged in an uninterruptible read took the window, the shortcuts
  and the IPC socket down with it. Bounded to 2 s, like `Worker.kill()`.
- **A truncated render was reported as a successful one.** Once the
  worker had sent its `{"ok": true}` header it could not take it back, so
  a PDF that died on page 7 of 50 looked complete: the daemon inferred
  success from "a header arrived". The worker now ends a stream with a
  zero-length frame, a failure after the header closes the socket without
  one and explains itself on stderr, and the daemon treats a stream with
  no end marker as truncated.
- **Video frames could arrive out of order.** The media worker recycled
  its two shared-memory slots per decoded frame with no acknowledgement,
  so a daemon busy with layout was handed a slot whose pixels had already
  been replaced. A slot is now reused only after the daemon acks copying
  it out, and the worker drops a frame rather than race for it.
- Hardened `MediaSession.read_frame()` against its own worker: `slot`,
  `w`, `h` and `stride` were only checked as `stride * h`, so a frame
  claiming `w=100000, h=1, stride=4` passed and then read ~400 KB past a
  4-byte buffer. Every field is now range-checked, `stride` against
  `w * 4`, and a negative slot is rejected.
- Bounded the pixel height of a rendered PDF page. A page declared as
  1 pt × 10000 pt asked for a ten-million-pixel column — tens of GB of
  QImage, in a jail that has no memory limit of its own.
- Bounded animation frames in the daemon as well as in the worker: 512
  screen-sized pixmaps is gigabytes of memory in the process that owns
  the window, and the job watchdog restarts on every frame, so a worker
  streaming for ever was never cut off.
- Fixed the font cache bind. The jail runs with `--clearenv` and
  `HOME=/tmp`, so fontconfig looks in `/tmp/.cache/fontconfig` and never
  saw `~/.cache/fontconfig` bound at its real path — which on a system
  with no `/var/cache/fontconfig` meant binding `/etc/fonts` with no
  usable cache, the 749 ms worst case the code set out to avoid.
- Spare workers now have their stderr drained from the moment they are
  spawned rather than from the moment they get a job: a worker that
  chattered during Qt's boot could fill the 64 KiB pipe buffer and block
  before ever reading its request, failing the job on the watchdog.
- `SandboxJob` objects are deleted when they finish. One per preview
  *and* one per prefetch accumulated as children of the window for the
  life of a daemon that is meant to run for weeks.
- Animation frames are no longer unpacked from a payload shorter than the
  4-byte delay header, and the prefetcher skips APNG as well as GIF (it
  was warming an image-op cache entry that the animation path never
  reads).
- Throttled position updates from the media worker to the 200 ms the
  constant always described: Qt 6 dropped `setNotifyInterval`, so the
  worker was sending a message per decoded frame to drive one slider.
- **The panel opened wherever the compositor felt like putting it.** The
  daemon is a native Wayland client and Wayland ignores `QWidget.move()`,
  so `center_on_screen()` had been doing nothing for the whole session:
  KWin placed the window, and every resize (a PDF swapping its "Loading…"
  card for the page column) grew it from that spot. The window is now a
  transparent overlay the size of the work area with the panel centred
  inside it — in *screen* coordinates, so the panel lands dead centre even
  if the compositor puts the overlay somewhere else. A preview now opens
  centred and stays centred as it resizes. Dragging the titlebar slides
  the panel within the overlay.

  Two things the overlay must *not* do, both learned the hard way. It is
  not fullscreen: KWin lowers an inactive fullscreen window below the
  focused one, which made a preview raised by the daemon — no activation
  token, since no click started it — disappear behind other windows about
  half the time. And its input region is set through `QWindow.setMask()`,
  never `QWidget.setMask()`: the widget one clips painting as well as
  input, so a panel that shrank (a PDF, then a text file) left the older,
  larger panel's pixels on screen with the new one drawn inside them — a
  window within a window. Either way the point of the mask is that clicks
  outside the panel go to whatever is underneath, exactly as they did when
  the window was panel-sized.
- **A PDF scrolled itself back to the top for the first second or two.**
  Pages were appended to the column as they decoded, so the scrollable
  range grew for as long as that took — on a 30-page document at 1689 px
  a little over two seconds. Scrolling in that window was clamped to the
  two pages that existed, and once the rest arrived the reader was left
  near the top of a 69499 px column, which looks exactly like the view
  scrolling up on its own. Every page now gets a placeholder of its real
  size before any pixels arrive (sizes come from the cached PNGs' IHDR
  headers, 24 bytes each, no decode), so the range is final on the first
  paint and an early scroll lands where it was aimed.
- **A partially cached PDF restarted from page 1 on every open**, throwing
  away the pages already on screen and jumping the reader back to the top —
  it looked like the preview closing and reopening. A missing page (the
  pruner dropped it, or an earlier render was cut short when the panel
  closed) now resumes the render at that page and appends into the view
  that is already up, so the scroll position survives and each open makes
  forward progress instead of re-rendering the same prefix. The worker's
  `pdf` op takes a `start` page for this.
- **PDF pages rendered as black text on a transparent background.**
  `QPdfDocument.render()` returns an ARGB image and a PDF paints glyphs but
  not its page, so nothing was behind the text. Pages are now composited
  onto white. Cached pages from before the fix are bypassed by a bumped
  cache variant (`pdf{i}v2`).
- Retired workers were left unreaped, leaking one zombie process per
  preview in a daemon that runs for weeks.
- Fontconfig had no config or cache inside the jail, so Qt rebuilt its font
  index on every single render — 583 ms instead of 148 ms for a one-page
  PDF. `/etc/fonts` and the fontconfig caches are now bound read-only.
  (Binding `/etc/fonts` alone is worse than neither, at 749 ms: it enables
  the scan without supplying the cache.)
- `shutil.which("bwrap")` ran on every render and every prefetch; it is
  resolved once.

### Known limitations

- **Slide decks (`.pptx`, `.odp`) are not previewed at all**, and the
  reason is worth recording so nobody re-treads it. Their content is
  absolutely positioned DrawingML, which `QTextDocument` cannot lay out,
  so the only faithful renderer is LibreOffice. That was implemented and
  then removed: `soffice --headless --convert-to pdf` **hangs inside the
  jail** — 90 s with zero bytes on stdout *and* stderr and no output file,
  where the identical command on the host converts a 149-slide deck in
  9 s. Binding `/etc/passwd` did not help; neither did giving it the
  network namespace back. Left enabled it was worse than useless, since
  every deck would wait out the watchdog before falling back. The
  remaining option is a DrawingML renderer of our own (shape geometry in
  EMU, placeholder positions from the slide layouts, pictures from the
  media parts) — no dependency, but charts and SmartArt would be missing.
- Spreadsheet sheets are labelled "Sheet 1", "Sheet 2" rather than by
  their real names; the names are in `xl/workbook.xml` and simply are not
  read yet.
- Office layout is `QTextDocument`'s approximation, not Word's: fonts,
  margins and page breaks differ from what the authoring application
  shows. It is a readable page, not a faithful reproduction.
- Archive entries listed through `bsdtar`/`7z`/`unrar` (rar, 7z, iso) show
  names only — no sizes, and no uncompressed total. The long-listing
  formats differ per tool and per locale, and a preview does not need
  them; zip and tar, which go through the standard library, do show sizes.
- Audio from the jailed player is wired and the socket is reachable
  (`pactl info` succeeds through it), but audible output has not been
  confirmed — this machine currently exposes no real sink (`auto_null`).
- The daemon still decodes the workers' PNG output, so a compromised worker
  could aim a malformed PNG at the daemon's own decoder. One hardened
  format instead of every format Qt supports, but not zero.

## 2026-06-13

- Sandboxed PDF rendering: pages are rendered by a bubblewrap helper and
  streamed one at a time, so page 1 appears while the rest (capped at 50)
  render, each page cached individually.

## 2026-06-12

- Async image rendering via `QProcess` with a 20 s watchdog, replacing a
  blocking `subprocess.run` on the GUI thread; in-flight renders are
  cancelled on navigation and stale results dropped.
- Out-of-process image decoding under bubblewrap (`render.py`), refusing
  untrusted decodes without `bwrap` unless `QUICKVIEW_ALLOW_UNSANDBOXED=1`.
- Two-tier preview cache: in-daemon pixmap LRU plus PNGs under
  `~/.cache/quickview/previews`, keyed by path + mtime + size, 256 MiB cap,
  oldest-first pruning, `--clear-cache`.
- Multi-file selection: the service menu passes `%U`, the daemon protocol
  takes newline-separated paths, and arrow keys page the selection with a
  folder-sibling fallback for single files.
- Rotating log at `~/.local/share/quickview/quickview.log` plus
  faulthandler crash tracebacks in `crash.log`.
- systemd user service (restart on failure) with XDG autostart as fallback;
  the service menu's `Exec` is templated by `install.sh` so the repo works
  from any path.
