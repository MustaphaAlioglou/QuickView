# QuickView

A macOS **Quick Look**–style file previewer for KDE Plasma. Select a file in
Dolphin, press **Space**, and a floating dark preview panel appears. Press
Space again (or Esc) to dismiss it.

Multi-select works too: select **several files**, trigger QuickView, and
**← / →** pages through them — the title shows your position (e.g. `2/5`),
just like Quick Look on a multi-file selection.

## What it previews

| Type                | How                                        |
|---------------------|--------------------------------------------|
| Images (PNG, JPG, WebP, SVG, …) | Scaled to fit, shows dimensions |
| Animated GIFs       | Played inline                              |
| PDFs                | Scrollable multi-page view                 |
| Video / audio       | Plays inline with a seek bar               |
| Text / code / CSV   | Monospace text view (first 1 MiB)          |
| Folders             | Item count and contents listing            |
| Everything else     | Icon, type, size and modified date         |

## Keys (while the preview is open)

| Key             | Action                                   |
|-----------------|-------------------------------------------|
| Space / Esc / Q | Close the preview                         |
| ← / →           | Previous / next file — pages through the selection if several files were passed, otherwise folder siblings |
| Enter           | Open the file in its default application  |
| Ctrl+Q          | Quit the background daemon entirely       |

## Preview cache

Image decodes are cached in two tiers (the `quicklookd` model):

- **Memory** — recently shown pixmaps stay in the daemon; re-showing or
  paging back with ← → skips decoding entirely.
- **Disk** — `~/.cache/quickview/previews/` (PNG entries, capped at 256 MiB,
  pruned oldest-first). Keys embed the file's mtime + size, so a changed
  file re-renders automatically — no stale previews.

Clear it with `quickview --clear-cache`.

## Sandboxed rendering

The daemon never decodes an untrusted image itself. On a cache miss it runs
`render.py` under **bubblewrap** with read-only access to `/usr`, this app
folder, and the single target file — no network (`--unshare-all`), no write
access; the PNG streams back over stdout and the daemon writes the cache. A
malformed file can only crash the throwaway helper.

If `bwrap` is not installed, images fall back to the metadata card
(secure by default). Override at your own risk with
`QUICKVIEW_ALLOW_UNSANDBOXED=1` (out-of-process but **not** sandboxed).

Animated GIFs, audio/video and text stay in-process, same as the C++
version: live playback can't be handed to a throwaway process, and text is
plain file I/O capped at 1 MiB.

## Logging & crash diagnostics

- `~/.local/share/quickview/quickview.log` — timestamped events (rotated
  past ~5 MiB); also echoed to stderr (the journal, under systemd):
  `journalctl --user -u quickview -e -f`
- `~/.local/share/quickview/crash.log` — faulthandler tracebacks if a
  native crash (e.g. inside a Qt decoder) takes the process down.

## Install

```bash
./install.sh
```

Then bind Space in Dolphin (one-time):
**Menu → Configure → Configure Keyboard Shortcuts… → search "Quick Look" → Custom → Space**

## How it works

- `quickview.py` — the PySide6 app; runs as a resident daemon (systemd user
  service `quickview.service`, started at login) so previews open in ~20 ms
- `client.py` — fast path: sends the path(s) to the daemon over a Unix
  socket without loading Qt
- `render.py` — sandboxed image decoder, spawned under bubblewrap
- `bin/quickview` — launcher: fast path first, full launch as fallback
- `quickview-servicemenu.desktop` — Dolphin service menu ("Quick Look")
- `.venv/` — self-contained PySide6 install (no system packages touched)

The daemon idles at ~300 MB (Qt stays loaded — that's what makes previews
instant). A Rust alternative with a smaller footprint lives in
`~/Desktop/quickview-rs`, but on Wayland it cannot hide its window between
previews (a winit limitation), so this Qt version is the better daily
driver on Plasma Wayland.

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
