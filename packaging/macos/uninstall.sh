#!/bin/sh
# Removes Bot Farm Silence Tracker installed by the macOS installer package (.pkg).
# The package installs this script as ~/.local/bin/bot-farm-silence-tracker-uninstall,
# because macOS has no list of installed programs to uninstall from.
#
#   bot-farm-silence-tracker-uninstall           asks first, keeps your scans and settings
#   bot-farm-silence-tracker-uninstall --yes     does not ask
#   bot-farm-silence-tracker-uninstall --purge   also deletes your scan data and settings
#
# It removes ~/Applications/Bot Farm Silence Tracker.app, the command-line program and
# this script from ~/.local/bin, and the installer's records (receipts). The line that
# adds ~/.local/bin to your PATH is removed only when nothing else is left in that folder,
# because other programs often use it too.
set -eu

TOOL_NAME="bot-farm-silence-tracker"
APP="$HOME/Applications/Bot Farm Silence Tracker.app"
BIN_DIR="$HOME/.local/bin"
# XDG_CONFIG_HOME and ZDOTDIR count only when absolute, as in the program's own config
# lookup (config.py) and in install.sh and the package scripts.
case "${XDG_CONFIG_HOME:-}" in
    /*) CONFIG_HOME="$XDG_CONFIG_HOME" ;;
    *) CONFIG_HOME="$HOME/.config" ;;
esac
case "${ZDOTDIR:-}" in
    /*) ZSH_DIR="$ZDOTDIR" ;;
    *) ZSH_DIR="$HOME" ;;
esac
CONFIG_DIR="$CONFIG_HOME/$TOOL_NAME"
DATA_DIR="$HOME/.cache/$TOOL_NAME"
# Every component package of the installer leaves a receipt: the app, the command-line
# program, one per platform choice, and the one that saves the settings.
PACKAGE_IDS="$TOOL_NAME.app $TOOL_NAME.cli $TOOL_NAME.platform.reddit-archive $TOOL_NAME.platform.bluesky $TOOL_NAME.platform.x $TOOL_NAME.platform.reddit $TOOL_NAME.finish"
MARKER="# Added by the Bot Farm Silence Tracker installer"

yes=0
purge=0
for arg in "$@"; do
    case "$arg" in
        -y | --yes) yes=1 ;;
        --purge) purge=1 ;;
        -h | --help)
            sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'error: unknown option: %s (see --help)\n' "$arg" >&2
            exit 2
            ;;
    esac
done

say() { printf '%s\n' "$*"; }

say "This removes Bot Farm Silence Tracker:"
say "  $APP"
say "  $BIN_DIR/$TOOL_NAME"
say "  $BIN_DIR/$TOOL_NAME-uninstall"
if [ "$purge" = 1 ]; then
    say "and, because of --purge, your scan data and settings:"
    say "  $DATA_DIR"
    say "  $CONFIG_DIR"
else
    say "Your scan data ($DATA_DIR) and settings ($CONFIG_DIR) are kept."
fi
if [ "$yes" = 0 ]; then
    if [ -t 0 ]; then
        printf 'Continue? [y/N] '
        answer=""
        read -r answer || :
        case "$answer" in
            y | Y | yes | YES | Yes) ;;
            *)
                say "Nothing was removed."
                exit 1
                ;;
        esac
    else
        say "error: no terminal to ask on. Rerun with --yes to remove without asking." >&2
        exit 1
    fi
fi

rm -rf "$APP"
rm -f "$BIN_DIR/$TOOL_NAME"
for id in $PACKAGE_IDS; do
    pkgutil --volume "$HOME" --forget "$id" >/dev/null 2>&1 || :
done
if [ "$purge" = 1 ]; then
    rm -rf "$DATA_DIR" "$CONFIG_DIR"
fi

# Remove this script last; the shell has already read it.
rm -f "$BIN_DIR/$TOOL_NAME-uninstall"

# Take ~/.local/bin off the PATH only when the folder is now empty, so other programs
# that live there keep working.
if [ -d "$BIN_DIR" ] && [ -z "$(ls -A "$BIN_DIR")" ]; then
    rmdir "$BIN_DIR"
    rm -f "$CONFIG_HOME/fish/conf.d/$TOOL_NAME.fish"
    for profile in "$ZSH_DIR/.zshrc" "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.profile"; do
        if [ ! -f "$profile" ] || ! grep -qxF "$MARKER" "$profile"; then
            continue
        fi
        tmp="$profile.$TOOL_NAME-uninstall.$$"
        # Drop the marker comment, the PATH line after it and the blank line before it.
        awk -v marker="$MARKER" -v line="export PATH=\"$BIN_DIR:\$PATH\"" '
            { lines[NR] = $0 }
            END {
                n = 0
                for (i = 1; i <= NR; i++) {
                    if (lines[i] == marker && lines[i + 1] == line) {
                        if (n > 0 && out[n] == "") n--
                        i++
                        continue
                    }
                    out[++n] = lines[i]
                }
                for (i = 1; i <= n; i++) print out[i]
            }' "$profile" >"$tmp"
        cat "$tmp" >"$profile"
        rm -f "$tmp"
        say "Removed $BIN_DIR from PATH in $profile"
    done
fi
say "Bot Farm Silence Tracker was removed."
