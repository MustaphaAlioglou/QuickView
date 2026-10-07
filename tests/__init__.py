# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Every Qt test module creates a QApplication, never a QGuiApplication.

The widget tests need a QApplication in particular. A process gets exactly
one application object, and a QGuiApplication made first cannot be upgraded
later — so a module that made one would, by being imported first, abort every
widget test after it. Since QApplication is a QGuiApplication, the modules
that need only the latter lose nothing by asking for the former, and the
suite then works however it is started: discovered from the repo root or
from inside tests/, or a single file run directly.
"""
