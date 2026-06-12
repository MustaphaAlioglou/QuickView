#!/usr/bin/env bash
# Installs QuickView's Dolphin integration for the current user.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

chmod +x "$DIR/bin/quickview" "$DIR/quickview-servicemenu.desktop"

# Dolphin context-menu entry ("Quick Look"). KDE requires the .desktop
# file in servicemenus to carry the executable bit.
mkdir -p "$HOME/.local/share/kio/servicemenus"
ln -sf "$DIR/quickview-servicemenu.desktop" \
       "$HOME/.local/share/kio/servicemenus/quickview-servicemenu.desktop"

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

[Service]
ExecStart=$DIR/bin/quickview --daemon
Restart=on-failure

[Install]
WantedBy=graphical-session.target
EOF
    rm -f "$HOME/.config/autostart/quickview-daemon.desktop"
    systemctl --user daemon-reload
    systemctl --user enable --now quickview.service
else
    mkdir -p "$HOME/.config/autostart"
    cat > "$HOME/.config/autostart/quickview-daemon.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=QuickView Daemon
Exec=$DIR/bin/quickview --daemon
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
