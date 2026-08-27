# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""The client↔daemon wire format. No Qt, so this runs anywhere."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ipc


class RoundTrip(unittest.TestCase):
    def rt(self, cwd, args):
        return ipc.decode_request(ipc.encode_request(cwd, args))

    def test_absolute_paths_survive(self):
        self.assertEqual(self.rt("/tmp", ["/a/b.txt"]), ["/a/b.txt"])

    def test_relative_resolved_against_the_senders_cwd(self):
        # Not the daemon's: under systemd its cwd is $HOME, which is why the
        # request carries the client's.
        self.assertEqual(self.rt("/etc", ["hostname"]), ["/etc/hostname"])

    def test_file_url_is_decoded(self):
        self.assertEqual(
            self.rt("/tmp", ["file:///tmp/a%20b%25c.txt"]), ["/tmp/a b%c.txt"]
        )

    def test_newline_in_a_filename(self):
        # Legal in a path; only NUL is not, which is why NUL is the separator.
        self.assertEqual(self.rt("/tmp", ["we ird\nname"]), ["/tmp/we ird\nname"])

    def test_dotdot_is_collapsed_lexically(self):
        self.assertEqual(self.rt("/tmp/x", ["../y.txt"]), ["/tmp/y.txt"])

    def test_multiple_arguments(self):
        self.assertEqual(self.rt("/tmp", ["a", "/b"]), ["/tmp/a", "/b"])

    def test_non_utf8_filename_round_trips(self):
        # The regression that mattered: errors="replace" turned an
        # undecodable byte into U+FFFD, i.e. a path that does not exist, and
        # the file silently failed to open.
        name = "qv-\udcff.txt"
        self.assertEqual(self.rt("/tmp", [name]), ["/tmp/" + name])

    def test_non_utf8_in_a_file_url(self):
        self.assertEqual(
            self.rt("/tmp", ["file:///tmp/qv-%FF.txt"]), ["/tmp/qv-\udcff.txt"]
        )

    def test_empty_and_whitespace_arguments_are_dropped(self):
        self.assertEqual(self.rt("/tmp", ["", "   ", "a"]), ["/tmp/a"])

    def test_a_message_with_no_arguments(self):
        self.assertEqual(ipc.decode_request(b"/tmp"), [])

    def test_normalize_is_idempotent(self):
        # quickview.py forwards already-normalized paths through the same
        # request format, so re-normalizing must be a no-op.
        once = ipc.normalize_arg("file:///tmp/a%20b.txt")
        self.assertEqual(ipc.normalize_arg(once, "/somewhere/else"), once)


class SocketPath(unittest.TestCase):
    def test_uses_xdg_runtime_dir(self):
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/4242"
        self.assertEqual(
            ipc.socket_path(), f"/run/user/4242/quickview-{os.getuid()}"
        )

    def test_falls_back_to_tmp(self):
        os.environ.pop("XDG_RUNTIME_DIR", None)
        self.assertEqual(ipc.socket_path(), f"/tmp/quickview-{os.getuid()}")


if __name__ == "__main__":
    unittest.main()
