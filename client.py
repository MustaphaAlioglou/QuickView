#!/usr/bin/env python3
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
