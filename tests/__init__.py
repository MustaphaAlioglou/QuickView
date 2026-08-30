# QuickView — a Quick Look style file previewer for KDE Plasma.
# Copyright (C) 2026 Mustapha Alioglou
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. This program is distributed WITHOUT ANY
# WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

"""Shared Qt bootstrap for the suite.

Every module here needs an application object, and the widget tests need it
to be a QApplication in particular. A process gets exactly one, and a
QGuiApplication made first cannot be upgraded to a QApplication later — so
whichever test module happened to be imported first would decide whether
widgets could be tested at all. This package is imported before any of
them, which makes the choice once and in one place.

The modules that only need a QGuiApplication ask for one and are handed
this, since QApplication is one.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([sys.argv[0]])
