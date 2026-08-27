#!/usr/bin/env bash
# Removes what install.sh put on this machine. The repo itself is left
# alone — delete the directory yourself when you are done with it.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KEEP_SETTINGS=1
PURGE_VENV=0
for arg in "$@"; do
    case "$arg" in
        --purge)  KEEP_SETTINGS=0; PURGE_VENV=1 ;;
        -h|--help)
            cat <<'USAGE'
Usage: ./uninstall.sh [--purge]

Removes the daemon, the Dolphin service menu, the launcher on PATH, and the
preview cache and logs.

  --purge   also delete ~/.config/quickview (your settings) and the
            project's .venv, which is the ~400 MB PySide6 install.
USAGE
            exit 0 ;;
        *) echo "uninstall: unknown option $arg (try --help)" >&2; exit 2 ;;
    esac
done

# The daemon first: disabling before the unit file goes away is what stops
# systemd leaving a dangling symlink in graphical-session.target.wants.
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now quickview.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/quickview.service"
    systemctl --user daemon-reload 2>/dev/null || true
fi
# The XDG autostart entry, used on systemd-less setups.
rm -f "$HOME/.config/autostart/quickview-daemon.desktop"
# Any daemon still resident. Matching on "--daemon" is not enough: when the
# launcher finds no daemon it starts quickview.py with the *file* as its
# argument, and that instance becomes the daemon — so match the checkout.
pkill -f "^$DIR/.venv/bin/python $DIR/quickview.py" 2>/dev/null || true
# The socket outlives a killed daemon, and a stale one makes the next start
# think an instance is already running.
rm -f "${XDG_RUNTIME_DIR:-/tmp}/quickview-$(id -u)"

rm -f "$HOME/.local/share/kio/servicemenus/quickview-servicemenu.desktop"

# Only our own symlink: someone else's quickview on PATH is not ours to
# delete, so check where it points before removing it.
LINK="$HOME/.local/bin/quickview"
if [ -L "$LINK" ] && [ "$(readlink -f "$LINK")" = "$DIR/bin/quickview" ]; then
    rm -f "$LINK"
elif [ -e "$LINK" ]; then
    echo "left $LINK alone — it is not a link into this checkout"
fi

rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/quickview"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/quickview"
rm -f "$DIR/bin/quickview-client"

if [ "$KEEP_SETTINGS" -eq 0 ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/quickview"
else
    echo "kept your settings in ${XDG_CONFIG_HOME:-$HOME/.config}/quickview"
    echo "  (./uninstall.sh --purge removes those too)"
fi
if [ "$PURGE_VENV" -eq 1 ]; then
    rm -rf "$DIR/.venv"
fi

echo "Uninstalled. The checkout at $DIR was not touched."
