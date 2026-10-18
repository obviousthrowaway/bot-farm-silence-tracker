# Shared by the macOS installer package scripts (preinstall and postinstall). It is
# sourced, not run. Installer runs these scripts as the user who installs, because the
# package installs into that user's home folder ("Install for me only").
# shellcheck shell=sh

TOOL_NAME="bot-farm-silence-tracker"
# shellcheck disable=SC2034 # Used by the scripts that source this file.
DEFAULT_PLATFORM="reddit-archive"

log() { printf '%s installer: %s\n' "$TOOL_NAME" "$*"; }

# pkg_home LOCATION: the home folder being installed into. Installer passes the install
# location of the component package as $2 (for the command-line package that is
# ~/.local/bin, for the payload-free packages the home folder itself), so this walks
# back to the home folder. Without a usable location it falls back to $HOME.
pkg_home() {
    location="${1%/}"
    case "$location" in
        */.local/bin) location="${location%/.local/bin}" ;;
        */Applications) location="${location%/Applications}" ;;
    esac
    if [ -n "$location" ] && [ -d "$location" ]; then
        printf '%s' "$location"
    else
        printf '%s' "$HOME"
    fi
}

# The file where the platform packages record the user's choices until the last package
# saves them through the program. It lives only for the length of one installation.
choices_file() { printf '%s/Library/Caches/%s-installer/platforms' "$1" "$TOOL_NAME"; }

# The user's login shell, as the account records it ($SHELL can be unset here).
login_shell() {
    shell="$(dscl . -read "/Users/$(id -un)" UserShell 2>/dev/null | awk '{ print $2; exit }')"
    [ -n "$shell" ] || shell="${SHELL:-/bin/zsh}"
    printf '%s' "$shell"
}

# absolute_or VALUE FALLBACK: VALUE when it is an absolute path, else FALLBACK. Used for
# XDG_CONFIG_HOME and ZDOTDIR, which count only when absolute (as in the program's own
# config lookup and in install.sh).
absolute_or() {
    case "$1" in
        /*) printf '%s' "$1" ;;
        *) printf '%s' "$2" ;;
    esac
}

# add_to_path HOME DIR: add DIR to the PATH in the login shell's startup file, the same
# way install.sh does (same file choice, comment and line, so running both never adds it
# twice). tests/test_installer_scripts.py checks that the two agree.
add_to_path() {
    home="$1"
    dir="$2"
    case "$(login_shell)" in
        */fish)
            profile="$(absolute_or "${XDG_CONFIG_HOME:-}" "$home/.config")/fish/conf.d/$TOOL_NAME.fish"
            line="fish_add_path -g \"$dir\""
            ;;
        */bash)
            profile="$home/.bash_profile"
            line="export PATH=\"$dir:\$PATH\""
            ;;
        */zsh)
            profile="$(absolute_or "${ZDOTDIR:-}" "$home")/.zshrc"
            line="export PATH=\"$dir:\$PATH\""
            ;;
        *)
            profile="$home/.profile"
            line="export PATH=\"$dir:\$PATH\""
            ;;
    esac
    if grep -qsxF "$line" "$profile"; then
        log "$profile already adds $dir to PATH"
        return 0
    fi
    mkdir -p "$(dirname "$profile")"
    printf '\n# Added by the Bot Farm Silence Tracker installer\n%s\n' "$line" >>"$profile"
    log "added $dir to PATH in $profile"
}
