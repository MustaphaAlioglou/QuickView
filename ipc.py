# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""The client↔daemon IPC contract, in one place.

Both client.py and quickview.py import this module, so the socket path,
the wire framing and the argument decoding cannot drift apart — a client
deriving a different path than the daemon silently misses it and spawns
a second instance. Pure stdlib: client.py imports this on its fast path,
so no Qt here.
"""

import os
from urllib.parse import unquote, urlparse


def socket_path() -> str:
    # XDG_RUNTIME_DIR is stable for the whole session; TMPDIR is not (it
    # may be set in a terminal but not in the systemd unit, or vice versa).
    run = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(run, f"quickview-{os.getuid()}")


def encode_paths(paths: list) -> bytes:
    # NUL-joined, NUL being the one byte POSIX forbids in a path — newlines
    # are legal in filenames. End-of-message is the connection close, so no
    # length framing is needed.
    return "\0".join(paths).encode("utf-8")


def decode_paths(data: bytes) -> list:
    text = data.decode("utf-8", errors="replace")
    return [p for p in text.split("\0") if p.strip()]


def normalize_arg(raw: str) -> str:
    # The daemon's toggle compares paths by string, so every entry point
    # must decode a file:// URL and absolutize a path identically.
    if raw.startswith("file://"):
        raw = unquote(urlparse(raw).path)
    return os.path.abspath(raw)
