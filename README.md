# QuickView

A macOS **Quick Look**–style file previewer for KDE Plasma. Select a file in
Dolphin, press **Space**, and a floating dark preview panel appears. Press
Space again (or Esc) to dismiss it.

![A PDF previewed by QuickView](docs/screenshots/pdf.png)

<p align="center">
  <img src="docs/screenshots/image.png" width="49%" alt="An image preview, with its dimensions in the titlebar">
  <img src="docs/screenshots/code.png" width="49%" alt="A source file preview">
</p>

Multi-select works too: select **several files**, trigger QuickView, and
**← / →** pages through them — the title shows your position (e.g. `2/5`),
just like Quick Look on a multi-file selection.

## What it previews

| Type                | How                                        |
|---------------------|--------------------------------------------|
| Images (PNG, JPG, WebP, SVG, …) | Scaled to fit, shows dimensions |
| Animated GIFs       | Played inline                              |
| PDFs                | Scrollable multi-page view                 |
| HTML                | Rendered page (JS off, network blocked) — titlebar button flips to source view |
| Video / audio       | Plays inline with a seek bar               |
| Text / code / CSV   | Monospace view with syntax highlighting (first 1 MiB) |
| Folders             | Item count and contents listing            |
| Everything else     | Icon, type, size and modified date         |

## Keys (while the preview is open)

| Key             | Action                                   |
|-----------------|-------------------------------------------|
| Space / Esc / Q | Close the preview                         |
| ← / →           | Previous / next file — pages through the selection if several files were passed, otherwise folder siblings |
| Enter           | Open the file in its default application  |
| Ctrl+Q          | Quit the background daemon entirely       |

## Syntax highlighting

Source files are coloured by [Pygments][pyg], which runs **inside the jail**
with everything else — its lexers are regexes, and a file written to make one
backtrack should take down a throwaway worker, not the daemon that owns your
window. What comes back is the text plus a list of colour spans; the daemon
paints ranges and parses nothing.

Files over 256 KiB are shown unhighlighted (lexing 1 MiB takes ~1.5 s, which
is slower than the preview it decorates). Any Pygments style works:

```bash
QUICKVIEW_CODE_STYLE=dracula   # gruvbox-dark, nord, monokai, native, …
```

Set it in the systemd unit's `Environment=` to make it stick. Without Pygments
installed the preview is plain text — nothing breaks.

[pyg]: https://pygments.org/

## Preview cache

Image decodes are cached in two tiers (the `quicklookd` model):

- **Memory** — recently shown pixmaps stay in the daemon; re-showing or
  paging back with ← → skips decoding entirely.
- **Disk** — `~/.cache/quickview/previews/` (PNG entries, capped at 256 MiB,
  pruned oldest-first). Keys embed the file's mtime + size, so a changed
  file re-renders automatically — no stale previews.

Clear it with `quickview --clear-cache`.

## Sandboxed rendering

**Every format QuickView decodes itself is parsed outside the daemon**,
inside a bubblewrap jail: still images, PDFs, animations, audio, video, and
the syntax highlighter. The daemon holds the window, the socket and the
cache — it does not hold a decoder. HTML is the one exception, and it is
covered under *What still runs in the daemon* below.

The jail has read-only `/usr`, this app folder and the font config, no
network (`--unshare-all`), no capabilities (`--cap-drop ALL`), no writes,
and — this is the part that matters — **no access to your files at all**.
The file arrives as a file descriptor passed over the worker socket, so
there is nothing to bind-mount and nothing for a compromised parser to go
looking for. A malformed file can only take down its own throwaway worker.

**It is also faster than parsing in-process was.** Entering the jail costs
about 3 ms; what used to cost ~150 ms per preview was importing Qt in a
fresh helper. So workers are booted *before* they are needed and parked on
a socket, and each one still handles exactly one file and then exits — the
same per-file isolation as a throwaway helper, with the startup paid by a
spare instead of by you. Measured on a 1-page PDF: 583 ms before, ~120 ms
now; a cached preview is ~2 ms.

- **Images** — decoded to a PNG that the daemon caches and displays.
- **PDFs** — pages stream one at a time, so page 1 appears while the rest
  (capped at 50) render; each page is cached individually.
- **Animations** (GIF/APNG) — every frame is decoded in the jail and
  streamed here; playback is a timer cycling pixmaps the daemon already
  holds, so no animation parser runs in this process.
- **Text and code** — read and lexed in the jail; what comes back is the
  text plus colour spans, so no lexer runs in this process.
- **Audio and video** — `media_worker.py` runs the whole pipeline in the
  jail and plays the audio itself through PipeWire (the one extra socket
  bound in). Because it owns the audio clock, Qt does A/V sync in there;
  video frames land in a shared memfd and the daemon just blits them.
  Play/pause/seek are messages, not method calls.

If `bwrap` is not installed, previews that need a parser fall back to the
metadata card (secure by default). Override at your own risk with
`QUICKVIEW_ALLOW_UNSANDBOXED=1` (out of process, but **not** sandboxed).

### What still runs in the daemon

Honest list — these are the reads that never reach a jail:

- **HTML** (`QtWebEngine`) — hardened rather than jailed by us: JavaScript
  and plugins disabled, every request outside `file:`/`data:` blocked (no
  phoning home), off-the-record profile. Untrusted markup is parsed in
  Chromium's own renderer process, which has its own sandbox; putting it in
  ours would be strictly worse. `QUICKVIEW_STRICT_SANDBOX=1` drops HTML to
  the plain source view if you would rather not rely on that.
- **Text** — only as a fallback. Text normally goes through the jail like
  everything else (that is where the highlighter runs); when the sandbox is
  unavailable the daemon reads the bytes itself, capped at 1 MiB, on a pool
  thread so a stalled network mount can't freeze the window. No parser is
  involved on that path — the file is shown uncoloured.
- **MIME sniffing** and the file-type icon — Qt reads magic bytes to decide
  which of the above applies. Bounded matching, no decoding.
- **The workers' own PNG output**, which the daemon decodes to display. A
  compromised worker could aim a malformed PNG at the daemon's decoder —
  one hardened format instead of every format Qt supports, but not zero.
  The same decoder runs on disk cache entries, and those live in
  `~/.cache/quickview/previews/` where anything running as you can rewrite
  them, so it is the same exposure by a second route.

## Logging & crash diagnostics

- `~/.local/share/quickview/quickview.log` — timestamped events (rotated
  past ~5 MiB); also echoed to stderr (the journal, under systemd):
  `journalctl --user -u quickview -e -f`
- `~/.local/share/quickview/crash.log` — faulthandler tracebacks if a
  native crash (e.g. inside a Qt decoder) takes the process down.

## Requirements

- **KDE Plasma** with Dolphin (Wayland or X11). Nothing else in the desktop
  is assumed — the Space binding is a Dolphin service menu.
- **bubblewrap** (`bwrap`) — mandatory, not optional: every parser runs
  inside the jail it provides. `pacman -S bubblewrap`,
  `apt install bubblewrap`, `dnf install bubblewrap`.
- **Python 3.10+**. `install.sh` creates a virtualenv in `.venv/` and
  installs PySide6 into it; no system packages are touched.
- **Pygments**, optional — installed into the same virtualenv by
  `install.sh`. Without it, code previews are plain text.
- PipeWire or PulseAudio, if you want sound in video/audio previews.

## Install

```bash
git clone https://github.com/MustaphaAlioglou/QuickView.git
cd QuickView
./install.sh
```

`install.sh` checks for `bwrap`, builds the virtualenv, installs PySide6
(a few hundred MB on first run), registers the Dolphin service menu, puts a
`quickview` launcher on your `PATH`, and enables the background daemon as a
systemd user service.

Then bind Space in Dolphin (one-time):
**Menu → Configure → Configure Keyboard Shortcuts… → search "Quick Look" → Custom → Space**

## How it works

- `quickview.py` — the PySide6 app; runs as a resident daemon (systemd user
  service `quickview.service`, started at login) so previews open in ~20 ms
- `client.py` — fast path: sends the path(s) to the daemon over a Unix
  socket without loading Qt
- `worker.py` — the jailed preview worker: images, PDF pages and animation
  frames, decoding from a passed file descriptor
- `media_worker.py` — the jailed player: decodes and plays audio/video, and
  streams video frames back through shared memory
- `renderers.py` — the decoders themselves, shared by the workers and the
  standalone scripts
- `render.py`, `render_pdf.py` — standalone one-shot versions of the image
  and PDF renderers, kept for reproducing a render by hand
- `bin/quickview` — launcher: fast path first, full launch as fallback
- `quickview-servicemenu.desktop` — Dolphin service menu ("Quick Look")
- `.venv/` — self-contained PySide6 install, created by `install.sh` (no
  system packages touched)

The daemon idles at ~300 MB, which is the price of keeping Qt loaded — and
keeping Qt loaded is what makes previews open in ~20 ms instead of ~1 s.

## Notes

- The venv must be built with the **system** Python (`/usr/bin/python3`),
  not Miniconda — conda's bundled Kerberos libraries conflict with Qt's
  networking libraries on Arch.
- Uninstall: `systemctl --user disable --now quickview.service`, then
  delete this folder plus
  `~/.config/systemd/user/quickview.service`,
  `~/.local/share/kio/servicemenus/quickview-servicemenu.desktop`,
  `~/.local/bin/quickview`, and the cache/log dirs
  `~/.cache/quickview` and `~/.local/share/quickview`.

## Contributing

Issues and pull requests are welcome. Two things worth knowing before you
open one:

- **Nothing that parses an untrusted file may run in the daemon.** New
  formats go in `renderers.py` and are reached through a worker op; the
  daemon only ever sees PNG bytes or decoded frames it asked for.
- The daemon is long-lived. Anything allocated per preview has to be freed
  per preview — it is expected to run for weeks without restarting.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

The screenshots show KDE's default *Next* wallpaper and Ghostscript's
manual, both from the system's own packages.
