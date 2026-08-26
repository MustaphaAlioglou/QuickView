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

Clients send their arguments *raw* and let the daemon normalize them.
That keeps normalize_arg a single implementation even though one of the
clients (client.rs) is not written in Python, and it keeps the one process
that touches attacker-controlled filenames — they arrive as argv, and a
filename inside a downloaded archive is not trustworthy — on the memory
safe side of the wire. The C client never looks at the bytes it forwards.
"""

import os


def socket_path() -> str:
    # XDG_RUNTIME_DIR is stable for the whole session; TMPDIR is not (it
    # may be set in a terminal but not in the systemd unit, or vice versa).
    run = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(run, f"quickview-{os.getuid()}")


def encode_request(cwd: str, raw_args: list) -> bytes:
    # NUL-joined, NUL being the one byte POSIX forbids in a path — newlines
    # are legal in filenames, and argv strings are NUL-terminated so no
    # field can contain the separator. Field 0 is the sender's working
    # directory: the daemon resolves relative arguments against it, since
    # its own cwd (systemd, typically $HOME) is not the client's. End-of-
    # message is the connection close, so no length framing is needed.
    return "\0".join([cwd, *raw_args]).encode("utf-8", "surrogateescape")


def decode_request(data: bytes) -> list:
    """Split a client message and normalize it into absolute paths."""
    # surrogateescape, not replace: a filename is bytes, and not every
    # filename is valid UTF-8. replace() turned an undecodable byte into
    # U+FFFD, i.e. a path that does not exist — the file silently failed to
    # open. surrogateescape round-trips those bytes through the lone
    # surrogates that sys.argv and open() already speak.
    text = data.decode("utf-8", "surrogateescape")
    fields = text.split("\0")
    if not fields:
        return []
    cwd, raw_args = fields[0], fields[1:]
    return [normalize_arg(a, cwd) for a in raw_args if a.strip()]


def normalize_arg(raw: str, cwd: str = "") -> str:
    # The daemon's toggle compares paths by string, so every entry point
    # must decode a file:// URL and absolutize a path identically — which
    # is why this is the only copy of the rule.
    if raw.startswith("file://"):
        # Imported here, not at module scope: client.py's fast path only
        # needs socket_path() and encode_request(), and urllib.parse costs
        # ~6 ms of interpreter startup — a third of that path's budget.
        from urllib.parse import unquote, urlparse

        # surrogateescape for the same reason as decode_request: %FF in a
        # file:// URL is a real byte in a real filename, not an error.
        raw = unquote(urlparse(raw).path, errors="surrogateescape")
    # join() returns raw unchanged when raw is already absolute, so this is
    # a no-op for the common case and the fix for `quickview hostname` run
    # from a directory the daemon knows nothing about.
    return os.path.abspath(os.path.join(cwd, raw))
