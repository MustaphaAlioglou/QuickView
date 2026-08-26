# Changelog

Notable changes to QuickView. Format follows [Keep a Changelog][kac]; the
project has no version tags yet, so entries are dated.

[kac]: https://keepachangelog.com/en/1.1.0/

## 2026-08-26

### Added

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
