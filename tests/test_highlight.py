# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Lexer choice for text previews.

Mostly about the `#!` fallback: people keep personal scripts extensionless
and rely on the shebang to say what they are, and Pygments' own content
guessing is both slow and wrong too often to use (see _shebang_lexer).
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renderers

try:
    import pygments  # noqa: F401
except ImportError:  # highlighting is optional, and so is testing it
    pygments = None


def lexer_for(name, text):
    """The lexer name highlight_text() settles on, or None."""
    return renderers.highlight_text(
        io.BytesIO(text.encode()), name, 1 << 20
    )["lexer"]


@unittest.skipIf(pygments is None, "Pygments is not installed")
class Shebangs(unittest.TestCase):
    def test_plain_interpreters(self):
        for line, want in [
            ("#!/bin/bash", "Bash"),
            ("#!/bin/sh", "Bash"),
            ("#!/usr/bin/perl -w", "Perl"),
            ("#!/usr/bin/awk -f", "Awk"),
            ("#!/usr/bin/lua", "Lua"),
        ]:
            with self.subTest(line=line):
                self.assertEqual(lexer_for("script", line + "\nbody\n"), want)

    def test_env_forms(self):
        # `env` itself is never the interpreter; the first token that is
        # neither a flag nor an assignment is.
        for line, want in [
            ("#!/usr/bin/env python3", "Python"),
            ("#!/usr/bin/env -S deno run --allow-net", "TypeScript"),
            ("#!/usr/bin/env FOO=bar ruby", "Ruby"),
        ]:
            with self.subTest(line=line):
                self.assertEqual(lexer_for("script", line + "\nbody\n"), want)

    def test_a_versioned_interpreter_falls_back_to_the_base_name(self):
        self.assertEqual(lexer_for("s", "#!/usr/bin/python3.12\nx = 1\n"),
                         "Python")

    def test_names_pygments_would_get_wrong_are_mapped(self):
        # get_lexer_by_name has no "node", and resolves "v" to Verilog.
        self.assertEqual(lexer_for("s", "#!/usr/bin/env node\nlet x = 1;\n"),
                         "JavaScript")
        self.assertIsNone(lexer_for("s", "#!/usr/bin/v\nfn main() {}\n"))

    def test_a_known_extension_still_wins(self):
        # Otherwise a stray shebang would recolour a file whose extension
        # already says exactly what it is.
        self.assertEqual(lexer_for("keep.py", "#!/bin/bash\nimport os\n"),
                         "Python")

    def test_no_shebang_stays_plain(self):
        self.assertIsNone(lexer_for("notes", "just some prose\n"))

    def test_malformed_shebangs_do_not_raise(self):
        for text in ("#!\n", "#!   \n", "#!/\n", "#!/usr/bin/nonesuch\n",
                     "#!" + "x" * 5000 + "\n", "#!/usr/bin/env\n",
                     "#!/usr/bin/env -S\n", "#!/usr/bin/env FOO=bar\n"):
            with self.subTest(text=text[:20]):
                self.assertIsNone(lexer_for("script", text))

    def test_a_shebang_actually_produces_colour(self):
        got = renderers.highlight_text(
            io.BytesIO(b'#!/bin/bash\nfor f in *; do echo "$f"; done\n'),
            "deploy", 1 << 20,
        )
        self.assertEqual(got["lexer"], "Bash")
        self.assertTrue(got["spans"])
        self.assertTrue(got["styles"])

    def test_spans_stay_inside_the_text(self):
        # The daemon validates these too, but an off-by-one here would mean
        # every line of a shebang script is coloured one character over.
        got = renderers.highlight_text(
            io.BytesIO(b"#!/usr/bin/env python3\nimport os\nprint(os.name)\n"),
            "runner", 1 << 20,
        )
        for start, length, index in got["spans"]:
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(start + length, len(got["text"]))
            self.assertIn(index, range(len(got["styles"])))


@unittest.skipIf(pygments is None, "Pygments is not installed")
class Extensions(unittest.TestCase):
    def test_a_known_extension_needs_no_shebang(self):
        self.assertEqual(lexer_for("a.py", "import os\n"), "Python")

    def test_an_unknown_extension_and_no_shebang_is_plain(self):
        self.assertIsNone(lexer_for("a.zzz", "whatever\n"))

    def test_a_bad_style_name_degrades_to_plain_text(self):
        # Must not read as "unknown extension" and must not raise.
        got = renderers.highlight_text(
            io.BytesIO(b"import os\n"), "a.py", 1 << 20, "no-such-style"
        )
        self.assertEqual(got["spans"], [])
        self.assertEqual(got["text"], "import os\n")


if __name__ == "__main__":
    unittest.main()
