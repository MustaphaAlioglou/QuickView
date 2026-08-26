#!/usr/bin/env python3
# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""User settings, read from ~/.config/quickview/quickview.conf.

Deliberately small. Everything here is a *preference* — something a person
might reasonably want different. The many other constants in quickview.py
are safety bounds on untrusted input (frame sizes, frame counts, timeouts);
exposing those would turn a config file into a way to defeat the sandbox's
limits, so they stay in the code.

INI rather than TOML because tomllib is Python 3.11 and this project
supports 3.10. Precedence is environment variable, then file, then default:
the env var stays useful for trying a setting without editing anything.

Read once at startup. The daemon is resident, so changing the file takes
effect on:

    systemctl --user restart quickview.service
"""

import configparser
import os

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "quickview",
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "quickview.conf")

# name -> (section, kind, default, env var, minimum, maximum)
# The bounds are there so a typo degrades to something usable instead of a
# daemon that will not start or a cache that eats the disk.
_SETTINGS = {
    "code_style": ("preview", str, "one-dark", "QUICKVIEW_CODE_STYLE",
                   None, None),
    "text_limit_kb": ("preview", int, 1024, "QUICKVIEW_TEXT_LIMIT_KB",
                      1, 1024 * 64),
    "pdf_max_pages": ("preview", int, 50, "QUICKVIEW_PDF_MAX_PAGES", 1, 2000),
    "disk_cache_mb": ("cache", int, 256, "QUICKVIEW_DISK_CACHE_MB", 0, 65536),
    "memory_cache_mb": ("cache", int, 96, "QUICKVIEW_MEMORY_CACHE_MB",
                        8, 8192),
}

DEFAULT_FILE = """\
# QuickView settings. Restart the daemon to apply changes:
#     systemctl --user restart quickview.service
#
# Every value below is the built-in default, shown for reference. Delete a
# line to go back to it. Each one can also be overridden for a single run
# with the matching QUICKVIEW_* environment variable.

[preview]
# Pygments style for source files: dracula, gruvbox-dark, nord, monokai,
# native, solarized-dark … anything Pygments ships.
code_style = one-dark

# How much of a text file to read, in KiB. Bigger files are shown
# truncated — the highlighter's cost climbs with length.
text_limit_kb = 1024

# How many pages of a PDF or office document to render at most.
pdf_max_pages = 50

[cache]
# Rendered previews kept on disk, in MiB. 0 disables the disk cache.
disk_cache_mb = 256

# Decoded pixmaps kept in memory, in MiB. This is on top of the ~300 MB the
# resident Qt process costs.
memory_cache_mb = 96
"""


def _clamp(value, low, high):
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def load(path: str = CONFIG_FILE) -> dict:
    """Every setting, resolved. Never raises: bad input falls back."""
    parsed = configparser.ConfigParser()
    try:
        parsed.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        parsed = configparser.ConfigParser()  # unreadable or malformed

    out = {}
    for name, (section, kind, default, env, low, high) in _SETTINGS.items():
        raw = os.environ.get(env)
        if raw is None:
            raw = parsed.get(section, name, fallback=None)
        if raw is None:
            out[name] = default
            continue
        raw = raw.strip()
        if kind is int:
            try:
                out[name] = _clamp(int(raw), low, high)
            except ValueError:
                out[name] = default
        else:
            out[name] = raw or default
    return out


def write_default_if_missing(path: str = CONFIG_FILE) -> bool:
    """Drop a commented template next to the user's other settings.

    Only ever creates; an existing file is never rewritten, so nothing a
    person edited can be lost to an upgrade.
    """
    if os.path.exists(path):
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(DEFAULT_FILE)
        return True
    except OSError:
        return False  # read-only home, or a race with another daemon
