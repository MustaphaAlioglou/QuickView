#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.
"""Fast path for QuickView: hand the file path to the running daemon.

Pure stdlib — no Qt import — so it runs in tens of milliseconds. Exits 0
if a daemon accepted the path, 1 otherwise (the launcher then falls back
to starting quickview.py, which becomes the daemon).
"""

import socket
import sys

import ipc


def main() -> int:
    args = sys.argv[1:]
    if not args or any(a.startswith("-") for a in args):
        return 1  # no file, or a flag (--daemon, --clear-cache): quickview.py handles it
    paths = [ipc.normalize_arg(raw) for raw in args]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(ipc.socket_path())
            s.sendall(ipc.encode_paths(paths))
        return 0
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
