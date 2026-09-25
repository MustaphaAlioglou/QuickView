#!/usr/bin/env bash
# Installs QuickView for the current user: dependencies, the Dolphin
# service menu, a launcher on PATH and the background daemon.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --pip-qt forces the old behaviour: a private PySide6 inside .venv, even
# when the system has a perfectly good one. The escape hatch for a system
# copy that turns out to be broken or too old.
PIP_QT=0
for arg in "$@"; do
    case "$arg" in
        --pip-qt) PIP_QT=1 ;;
        -h|--help)
            echo "usage: ./install.sh [--pip-qt]"
            echo "  --pip-qt  install a private PySide6 into .venv (~650 MB)"
            echo "            instead of using the system one"
            exit 0 ;;
        *) echo "quickview: unknown option $arg" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------- dependencies
# bubblewrap is not optional. Every parser runs inside the jail it
# provides, and without it QuickView refuses to decode anything rather
# than fall back to parsing untrusted files in its own process.
if ! command -v bwrap >/dev/null 2>&1; then
    cat >&2 <<'MSG'
quickview: bubblewrap (bwrap) is required but was not found.

Every file format is parsed inside a bubblewrap jail; without it
QuickView will refuse to decode anything. Install it, then re-run:

  Arch          sudo pacman -S bubblewrap
  Debian/Ubuntu sudo apt install bubblewrap
  Fedora        sudo dnf install bubblewrap
  openSUSE      sudo zypper install bubblewrap
MSG
    exit 1
fi

PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
    # Built with the *system* Python on purpose: Miniconda's bundled
    # Kerberos libraries conflict with Qt's networking libraries, which
    # shows up as an import error deep inside PySide6.
    SYS_PY=/usr/bin/python3
    [ -x "$SYS_PY" ] || SYS_PY="$(command -v python3 || true)"
    if [ -z "$SYS_PY" ]; then
        echo "quickview: no python3 found — install Python 3.10 or newer." >&2
        exit 1
    fi
    if ! "$SYS_PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        echo "quickview: Python 3.10 or newer is required ($SYS_PY is older)." >&2
        exit 1
    fi
    echo "Creating the virtualenv in .venv (using $SYS_PY)..."
    # --system-site-packages so a distro PySide6/Pygments is visible from
    # inside. Where the system has them, this turns a ~650 MB private copy
    # into a ~20 KB stub, and the Qt libraries end up shared with Plasma's
    # own processes instead of being a second, private mapping of the same
    # code. The jail can reach them: sandbox_flags() in quickview.py binds
    # /usr read-only, which covers system site-packages. It does *not* cover
    # ~/.local/lib, which this flag also exposes — hence the location test
    # in reachable_in_jail() below, since importing here proves nothing
    # about importing in there.
    "$SYS_PY" -m venv --system-site-packages "$DIR/.venv"
fi

# An older install made the venv without --system-site-packages. The flag is
# read from pyvenv.cfg at every interpreter start, so flipping this line is
# enough — no rebuild, and anything already pip-installed inside still wins.
if grep -qi '^include-system-site-packages *= *false' "$DIR/.venv/pyvenv.cfg" 2>/dev/null; then
    sed -i 's/^include-system-site-packages *= *false/include-system-site-packages = true/I' \
        "$DIR/.venv/pyvenv.cfg"
fi

# ------------------------------------------------------------ PySide6
# Probed by module, not by a bare `import PySide6`: QuickView imports eight
# Qt modules and distributions split them up differently. Four are needed
# before the daemon can draw anything; the rest are imported lazily and
# cost one file format each, so a missing one is a warning, not a reason to
# download a whole private Qt.

# Where a module actually resolves. This matters more than it looks:
# --system-site-packages also puts the *user* site directory on sys.path,
# ahead of /usr, so a stray `pip install --user PySide6` satisfies every
# probe below. The jail binds only /usr and APP_DIR (sandbox_flags() in
# quickview.py), so such a copy works for the daemon and then vanishes
# inside bubblewrap — every worker dies on import and no file previews at
# all, from an install that reported success. Only /usr and the venv count.
# Always succeeds, printing nothing when the module is missing: under set -e
# a failing `WHERE="$(module_dir …)"` would end the script right there,
# silently, on exactly the distros (Ubuntu, Mint) that ship no PySide6.
module_dir() {
    "$PY" -c "import os, $1 as m; print(os.path.dirname(m.__file__))" 2>/dev/null || true
}

reachable_in_jail() {
    case "$1" in
        /usr/*|"$DIR"/.venv/*) return 0 ;;
        *) return 1 ;;
    esac
}

# 6.4 is where the QtPdf Python bindings landed, which render_pdf() needs.
# Raise this deliberately if something newer gets used.
qt_usable() {
    reachable_in_jail "$(module_dir PySide6)" || return 1
    "$PY" - <<'EOF' >/dev/null 2>&1
import sys
from PySide6 import __version__ as v
if tuple(int(p) for p in v.split(".")[:2]) < (6, 4):
    sys.exit(1)
import PySide6.QtCore, PySide6.QtGui, PySide6.QtWidgets, PySide6.QtNetwork
EOF
}

# --ignore-installed because pip, inside a --system-site-packages venv,
# treats an importable system copy as satisfying the requirement and does
# nothing at all — which would silently turn both callers below into no-ops:
# --pip-qt could not honour its own promise, and the too-old-Qt fallback
# would leave the daemon on the version it just rejected.
pip_install_qt() {
    "$PY" -m pip install --quiet --upgrade pip
    PIP_USER=0 "$PY" -m pip install --ignore-installed PySide6
}

if [ "$PIP_QT" = 1 ]; then
    echo "Installing PySide6 into .venv (--pip-qt; a few hundred MB)..."
    pip_install_qt
elif qt_usable; then
    QT_WHERE="$("$PY" -c 'import PySide6,os;print(os.path.dirname(PySide6.__file__))')"
    QT_VER="$("$PY" -c 'import PySide6;print(PySide6.__version__)')"
    case "$QT_WHERE" in
        "$DIR"/.venv/*)
            echo "Using the PySide6 already in .venv ($QT_VER)."
            # Only worth mentioning if the system could take over.
            if /usr/bin/python3 -c "import PySide6" >/dev/null 2>&1; then
                SYS_VER="$(/usr/bin/python3 -c 'import PySide6;print(PySide6.__version__)')"
                SIZE="$(du -sh "$QT_WHERE" 2>/dev/null | cut -f1)"
                echo "  .venv holds a private PySide6 ($SIZE); your system has $SYS_VER."
                echo "  Reclaim that space with: rm -rf .venv && ./install.sh"
            fi ;;
        *)
            echo "Using system PySide6 $QT_VER ($QT_WHERE)." ;;
    esac

    # The lazily-imported modules. Each one is a file format, not the app.
    MISSING=""
    "$PY" -c "import PySide6.QtPdf" >/dev/null 2>&1 || MISSING="$MISSING QtPdf"
    "$PY" -c "import PySide6.QtWebEngineWidgets" >/dev/null 2>&1 || MISSING="$MISSING QtWebEngine"
    "$PY" -c "import PySide6.QtMultimedia" >/dev/null 2>&1 || MISSING="$MISSING QtMultimedia"
    if [ -n "$MISSING" ]; then
        echo
        for mod in $MISSING; do
            case "$mod" in
                QtPdf)        echo "  QtPdf missing       → PDF, office and .ai previews disabled" ;;
                QtWebEngine)  echo "  QtWebEngine missing → HTML previews disabled" ;;
                QtMultimedia) echo "  QtMultimedia missing → audio and video previews disabled" ;;
            esac
        done
        # Debian splits PySide6 into dotted per-module packages; the others
        # ship one package, so point at a search rather than invent a name
        # that may not exist on the running release.
        if command -v apt >/dev/null 2>&1; then
            echo "  Install e.g. python3-pyside6.qtpdf, python3-pyside6.qtwebenginewidgets"
        elif command -v dnf >/dev/null 2>&1; then
            echo "  Try: sudo dnf install python3-pyside6   (dnf search pyside6 for parts)"
        elif command -v zypper >/dev/null 2>&1; then
            echo "  Try: sudo zypper install python3-pyside6"
        elif command -v pacman >/dev/null 2>&1; then
            # pyside6 only optionally depends on these, and it is the Qt
            # libraries they carry (libQt6Pdf is in qt6-webengine) that are
            # missing — reinstalling pyside6 would change nothing.
            echo "  Try: sudo pacman -S qt6-webengine qt6-multimedia"
        fi
        echo "  Or re-run with --pip-qt for a private PySide6 with everything (~650 MB)."
        echo
    fi
else
    WHERE="$(module_dir PySide6)"
    if [ -n "$WHERE" ] && ! reachable_in_jail "$WHERE"; then
        # Importable, but from somewhere the sandbox cannot reach.
        echo "Ignoring the PySide6 in $WHERE — the sandbox binds only /usr,"
        echo "so the jailed workers could not import it."
    fi
    echo "No usable system PySide6 — installing a private one into .venv"
    echo "(a few hundred MB on first run)..."
    pip_install_qt
fi

# Optional: syntax highlighting for the text preview. A missing Pygments
# costs colour, nothing else, so a failure here is not fatal. With
# --system-site-packages this now finds a distro Pygments too, so on most
# machines it installs nothing.
# Same location test as PySide6: a ~/.local Pygments would import here and
# then be missing inside the jail, so highlight_text() would take its
# ImportError path and every source file would preview grey, silently.
if ! reachable_in_jail "$(module_dir pygments)"; then
    echo "Installing Pygments (syntax highlighting)..."
    PIP_USER=0 "$PY" -m pip install --ignore-installed Pygments ||
        echo "  (skipped — previews will be plain text)"
fi

chmod +x "$DIR/bin/quickview"

# Optional: the compiled fast-path client. It hands a path to the running
# daemon in under a millisecond, where starting a Python interpreter to do
# the same costs ~15 ms — the whole of the perceived delay on a warm
# preview. No crates, so plain rustc builds it without Cargo; a missing
# toolchain costs those milliseconds and nothing else, so this is not fatal.
# Cleared first, unconditionally: bin/quickview prefers this binary whenever
# it is executable, so a leftover from a previous install would outrank
# client.py — and keep talking the old wire format — if the build below never
# replaced it. rustc writes a fresh one on success.
rm -f "$DIR/bin/quickview-client"
if command -v rustc >/dev/null 2>&1; then
    echo "Building the fast-path client..."
    rustc -O -C strip=symbols -C panic=abort --edition 2021 \
        -o "$DIR/bin/quickview-client" "$DIR/client.rs" ||
        echo "  (build failed — falling back to client.py, ~15 ms slower per preview)"
else
    echo "rustc not found — using client.py (previews stay correct, ~15 ms slower)."
fi

# Dolphin context-menu entry ("Quick Look"), rendered from the template so
# the repo can live at any path. KDE requires the .desktop file in
# servicemenus to carry the executable bit.
mkdir -p "$HOME/.local/share/kio/servicemenus"
MENU_DEST="$HOME/.local/share/kio/servicemenus/quickview-servicemenu.desktop"
# Remove any prior entry first. An older install symlinked this path back to
# the repo template; without this rm, the redirect below would follow that
# symlink and truncate its own source before sed could read it.
rm -f "$MENU_DEST"
sed "s|@DIR@|$DIR|" "$DIR/quickview-servicemenu.desktop" > "$MENU_DEST"
chmod +x "$MENU_DEST"

# Command on PATH (handy for terminal use: `quickview somefile`).
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/bin/quickview" "$HOME/.local/bin/quickview"

# Background daemon, so previews open instantly (no Qt startup). Prefer a
# systemd user service (starts at login, restarts on failure); fall back to
# XDG autostart on systemd-less setups.
if command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/quickview.service" <<EOF
[Unit]
Description=QuickView resident previewer (warm daemon for instant previews)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart="$DIR/bin/quickview" --daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF
    rm -f "$HOME/.config/autostart/quickview-daemon.desktop"
    systemctl --user daemon-reload
    systemctl --user enable quickview.service
    # restart, not `enable --now`: --now leaves an already-running daemon
    # untouched, so an upgrade that changes the socket path or wire
    # protocol would strand the old daemon on the old socket while new
    # invocations spawn a second one. restart also starts a stopped unit.
    systemctl --user restart quickview.service
else
    mkdir -p "$HOME/.config/autostart"
    cat > "$HOME/.config/autostart/quickview-daemon.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=QuickView Daemon
Exec="$DIR/bin/quickview" --daemon
X-KDE-StartupNotify=false
NoDisplay=true
EOF
    # Start the daemon now if it isn't running yet (setsid detaches it from
    # this shell so it survives the terminal closing).
    setsid "$DIR/bin/quickview" --daemon >/dev/null 2>&1 < /dev/null &
fi

echo "Installed."
echo
echo "To get macOS-style Space previews, bind the shortcut in Dolphin:"
echo "  1. Open Dolphin"
echo "  2. Menu > Configure > Configure Keyboard Shortcuts..."
echo "  3. Search for 'Quick Look'"
echo "  4. Click it, choose Custom, and press Space"
