# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Settings parsing. A bad config file must never stop the daemon."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


class Loading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "quickview.conf")
        for _, (_s, _k, _d, env, _lo, _hi) in config._SETTINGS.items():
            os.environ.pop(env, None)  # a stray env var would mask the file

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(config.load(self.path)["code_style"], "one-dark")

    def test_written_template_matches_the_built_in_defaults(self):
        # Otherwise the file would silently change behaviour just by existing.
        config.write_default_if_missing(self.path)
        self.assertEqual(
            config.load(self.path), config.load(os.path.join(self.dir, "none"))
        )

    def test_template_is_never_overwritten(self):
        self.assertTrue(config.write_default_if_missing(self.path))
        self.write("[preview]\ncode_style = mine\n")
        self.assertFalse(config.write_default_if_missing(self.path))
        self.assertEqual(config.load(self.path)["code_style"], "mine")

    def test_values_are_read(self):
        self.write("[preview]\ncode_style = dracula\npdf_max_pages = 7\n")
        got = config.load(self.path)
        self.assertEqual(got["code_style"], "dracula")
        self.assertEqual(got["pdf_max_pages"], 7)

    def test_a_setting_with_a_fixed_list_rejects_anything_else(self):
        self.write("[preview]\nbook_theme = gruvbox-dark\n")
        self.assertEqual(config.load(self.path)["book_theme"], "gruvbox-dark")
        # A typo must not leave books rendering with no colours at all.
        self.write("[preview]\nbook_theme = grubvox\n")
        self.assertEqual(config.load(self.path)["book_theme"], "paper")
        self.write("[preview]\nbook_theme = GRUVBOX-LIGHT\n")
        self.assertEqual(config.load(self.path)["book_theme"], "gruvbox-light")

    def test_out_of_range_is_clamped(self):
        self.write("[preview]\npdf_max_pages = 9999999\n")
        self.assertEqual(config.load(self.path)["pdf_max_pages"], 2000)
        self.write("[preview]\npdf_max_pages = -5\n")
        self.assertEqual(config.load(self.path)["pdf_max_pages"], 1)

    def test_a_non_number_falls_back(self):
        self.write("[preview]\ntext_limit_kb = nonsense\n")
        self.assertEqual(config.load(self.path)["text_limit_kb"], 1024)

    def test_a_malformed_file_gives_every_default(self):
        self.write("this is not ini at all {{{")
        self.assertEqual(config.load(self.path), config.load("/nonexistent"))

    def test_zero_disables_the_disk_cache(self):
        self.write("[cache]\ndisk_cache_mb = 0\n")
        self.assertEqual(config.load(self.path)["disk_cache_mb"], 0)

    def test_environment_wins_over_the_file(self):
        self.write("[preview]\ncode_style = dracula\n")
        os.environ["QUICKVIEW_CODE_STYLE"] = "nord"
        try:
            self.assertEqual(config.load(self.path)["code_style"], "nord")
        finally:
            del os.environ["QUICKVIEW_CODE_STYLE"]

    def test_an_unreadable_file_does_not_raise(self):
        self.write("[preview]\ncode_style = dracula\n")
        os.chmod(self.path, 0o000)
        try:
            self.assertEqual(config.load(self.path)["code_style"], "one-dark")
        finally:
            os.chmod(self.path, 0o644)


if __name__ == "__main__":
    unittest.main()
