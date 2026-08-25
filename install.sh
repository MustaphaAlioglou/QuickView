#!/usr/bin/env bash
# Installs QuickView for the current user: dependencies, the Dolphin
# service menu, a launcher on PATH and the background daemon.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    "$SYS_PY" -m venv "$DIR/.venv"
fi

if ! "$PY" -c "import PySide6" >/dev/null 2>&1; then
    echo "Installing PySide6 into .venv (a few hundred MB on first run)..."
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install PySide6
fi

chmod +x "$DIR/bin/quickview"

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
