#!/usr/bin/env python3
"""Fast path for QuickView: hand the file path to the running daemon.

Pure stdlib — no Qt import — so it runs in tens of milliseconds. Exits 0
if a daemon accepted the path, 1 otherwise (the launcher then falls back
to starting quickview.py, which becomes the daemon).
"""

import os
import socket
import sys


def socket_path() -> str:
    # QLocalServer creates the socket in QDir::tempPath() ($TMPDIR or /tmp).
    tmp = os.environ.get("TMPDIR", "/tmp").rstrip("/") or "/tmp"
    return f"{tmp}/quickview-{os.getuid()}"


def main() -> int:
    args = sys.argv[1:]
    if not args or any(a.startswith("-") for a in args):
        return 1  # no file, or a flag (--daemon, --clear-cache): quickview.py handles it
    paths = []
    for raw in args:
        if raw.startswith("file://"):
            from urllib.parse import unquote, urlparse
            raw = unquote(urlparse(raw).path)
        paths.append(os.path.abspath(raw))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(socket_path())
            s.sendall("\n".join(paths).encode("utf-8"))
        return 0
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
