#!/bin/sh
# Bot Farm Silence Tracker installer for macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | sh
#
# What it does:
#   1. Downloads the prebuilt program for your system from the latest GitHub Release.
#   2. Checks its SHA256 checksum (and, when the GitHub CLI "gh" is installed and logged
#      in, its build provenance attestation).
#   3. Runs it once ("--version") to confirm it works on this system.
#   4. Puts it in ~/.local/bin and, if needed, adds that folder to your shell PATH.
#   5. Records which social platforms you want to scan, by running the installed program's
#      "config set-platforms" command (this script never writes the config file itself).
#
# No Python and no sudo are needed. Only your home directory is written to.
#
# When there is no prebuilt program for your system (for example musl-based Linux such
# as Alpine, or a release that does not have your platform yet), it can install the
# program with uv (https://docs.astral.sh/uv/) instead: from the Python package (wheel)
# attached to the same release, checked against SHA256SUMS.txt, or, when there is no
# release yet, from the source code on GitHub (that needs git). If uv is not installed,
# it asks before running Astral's uv installer. A failed download or a checksum mismatch
# never switches to uv: the installer stops with an error instead.
#
# Options (arguments or environment variables):
#   --platforms LIST or BOT_FARM_SILENCE_TRACKER_PLATFORMS
#       The platforms to scan, as a comma list: reddit-archive, bluesky, x, reddit.
#       Default: reddit-archive (Reddit through the Arctic Shift archive, no account
#       needed). Given a terminal and no list, the installer offers a short menu. For a
#       platform that needs credentials (x, reddit) it prints which environment variables
#       to set and where to get them; it never asks for or stores credentials.
#   SOURCE argument or BOT_FARM_SILENCE_TRACKER_SOURCE
#       Skip the prebuilt program and "uv tool install" this instead: a git URL
#       (git+https://...), a PyPI name, a local checkout or a wheel file.
#   BOT_FARM_SILENCE_TRACKER_VERSION       Release tag to install, e.g. v0.1.0 (default: latest).
#   BOT_FARM_SILENCE_TRACKER_INSTALL_DIR   Where to put the program (default: ~/.local/bin).
#   BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH=1   Do not edit shell profile files.
#   BOT_FARM_SILENCE_TRACKER_NO_UV=1       Never use uv; fail if there is no prebuilt program.
#   BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1    Allow installing uv without asking (needed when
#                                    there is no terminal to ask on).
#   BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET=1   Allow BusyBox wget as the only downloader. It
#                                    may not validate TLS certificates, so by default the
#                                    installer refuses; install curl instead.
#   BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL   Download the release files from this folder URL
#                                    instead of GitHub (https only).
#   BOT_FARM_SILENCE_TRACKER_GITHUB_URL    For tests: use this server in place of
#                                    https://github.com (https only).
#   BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1    For tests only: allow http:// URLs.
#
# A copy whose REPO below still holds the owner placeholder has not been published (see
# "Publishing your copy" in the README). The installer then refuses to run, unless
# BOT_FARM_SILENCE_TRACKER_GITHUB_URL or BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL replaces
# GitHub as the download base, and even then it skips the attestation check with a warning.
#
# To remove it later: rm ~/.local/bin/bot-farm-silence-tracker
# (if it was installed through uv: uv tool uninstall bot-farm-silence-tracker)

set -eu
# pipefail is not available in every sh; enable it where the shell supports it.
# shellcheck disable=SC3040
(set -o pipefail) 2>/dev/null && set -o pipefail

REPO="obviousthrowaway/bot-farm-silence-tracker"
# The owner placeholder in REPO above, written in two parts so that the replace command
# in "Publishing your copy" (README) rewrites REPO but leaves this check intact.
OWNER_PLACEHOLDER="_""_OWNER__"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
TOOL_NAME="bot-farm-silence-tracker"
NL='
'

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# True while REPO still holds the owner placeholder: this copy has not been published,
# so REPO names no real repository and must not be used for downloads or attestation.
unpublished() {
    case "$REPO" in
        *"$OWNER_PLACEHOLDER"*) return 0 ;;
    esac
    return 1
}

insecure_test() { [ "${BOT_FARM_SILENCE_TRACKER_INSECURE_TEST:-0}" = "1" ]; }

# Expand a leading "~", make the path absolute, and refuse characters that would break
# the PATH line written to shell profiles.
resolve_install_dir() {
    dir="$1"
    # shellcheck disable=SC2088 # matching a literal "~", not expanding it
    case "$dir" in
        "~") dir="$HOME" ;;
        "~/"*) dir="$HOME/${dir#"~/"}" ;;
    esac
    case "$dir" in
        *'"'* | *'$'* | *'`'* | *"$NL"*)
            die "the install folder must not contain \", \$, \` or a line break: $dir"
            ;;
    esac
    case "$dir" in
        /*) ;;
        *) dir="$(pwd)/$dir" ;;
    esac
    printf '%s' "$dir"
}

require_https() {
    case "$1" in
        https://*) ;;
        *)
            insecure_test ||
                die "refusing a non-https URL: $1 (set BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1 only for local tests)"
            ;;
    esac
}

# fetch URL FILE: download URL to FILE and print the HTTP status code. A non-zero exit
# means the request did not complete (network, TLS or DNS error, or interrupted).
fetch() {
    if command -v curl >/dev/null 2>&1; then
        if insecure_test; then
            curl -sSL -o "$2" -w '%{http_code}' "$1"
        else
            curl --proto '=https' --tlsv1.2 -sSL -o "$2" -w '%{http_code}' "$1"
        fi
    elif command -v wget >/dev/null 2>&1; then
        set -- "$1" "$2" "$2.headers"
        rc=0
        if wget_https_only; then
            wget -q -S --https-only --secure-protocol=TLSv1_2 -O "$2" "$1" 2>"$3" || rc=$?
        else
            wget -q -S -O "$2" "$1" 2>"$3" || rc=$?
        fi
        # GNU wget exits with 8 when the server answered with an error, and BusyBox wget
        # exits with 1 and prints "server returned error". Either way the status is in
        # the headers. Any other failure is a network, TLS or DNS error.
        if [ "$rc" = "0" ] || [ "$rc" = "8" ] ||
            { [ "$rc" = "1" ] && grep -q 'server returned error: HTTP/' "$3"; }; then
            awk '$1 ~ /^HTTP\// { code = $2 } END { print (code == "" ? "000" : code) }' "$3"
            return 0
        fi
        return "$rc"
    else
        die "curl or wget is required to download $TOOL_NAME."
    fi
}

# Whether wget can be limited to HTTPS (GNU wget can; BusyBox wget cannot).
wget_https_only() {
    ! insecure_test && wget --help 2>&1 | grep -q -- '--https-only'
}

# Whether the wget on PATH is BusyBox's, which may not validate TLS certificates.
wget_is_busybox() {
    # BusyBox wget exits non-zero for --help, so keep the output out of the pipeline.
    help_text="$(wget --help 2>&1 || true)"
    printf '%s\n' "$help_text" | head -n 3 | grep -qi busybox
}

# Print the tag of the latest GitHub Release, or nothing when the repository has no
# release yet. GitHub answers /releases/latest with a redirect (301, 302, 303, 307 or
# 308) to /releases/tag/<tag>, or to /releases when there is none. Any other answer (an
# error status such as 429 or 503, or a page from a proxy) is not "no release": the
# installer stops instead of guessing. Exits through die on every failure.
latest_tag() {
    url="$GITHUB/$REPO/releases/latest"
    unreachable="could not look up the latest release at $url (network or TLS error). Nothing was installed."
    if command -v curl >/dev/null 2>&1; then
        if insecure_test; then
            out="$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' "$url")" || die "$unreachable"
        else
            out="$(curl --proto '=https' --tlsv1.2 -sS -o /dev/null -w '%{http_code} %{redirect_url}' "$url")" ||
                die "$unreachable"
        fi
        code="${out%% *}"
        location="${out#* }"
    else
        if wget_https_only; then
            headers="$(wget -q -S --spider --https-only --secure-protocol=TLSv1_2 "$url" 2>&1)" || true
        else
            headers="$(wget -q -S --spider "$url" 2>&1)" || true
        fi
        # wget follows the redirect itself; the first status line and the first Location
        # header are GitHub's own answer.
        code="$(printf '%s\n' "$headers" | awk '$1 ~ /^HTTP\// { print $2; exit }')"
        [ -n "$code" ] || die "$unreachable"
        location="$(printf '%s\n' "$headers" | awk 'tolower($1) == "location:" { print $2; exit }')"
    fi
    location="$(printf '%s' "$location" | tr -d '\r')"
    case "$code" in
        301 | 302 | 303 | 307 | 308) ;;
        *)
            die "GitHub answered HTTP $code for $url instead of a redirect, so the latest release is unknown (rate limit, outage or a proxy in the way). Nothing was installed. Try again later, or set BOT_FARM_SILENCE_TRACKER_VERSION to a release tag."
            ;;
    esac
    case "$location" in
        */releases/tag/?*) printf '%s' "${location##*/releases/tag/}" ;;
        */releases) ;;
        *)
            die "GitHub redirected $url to an unexpected place (${location:-no Location header}), so the latest release is unknown. Nothing was installed."
            ;;
    esac
    return 0
}

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    else
        die "sha256sum or shasum is required to verify the download."
    fi
}

# verify_checksum FILE NAME: check FILE against the entry for NAME in SHA256SUMS.txt.
verify_checksum() {
    say "==> Verifying SHA256"
    expected="$(awk -v f="$2" '$2 == f || $2 == "*" f { print $1; exit }' "$tmp/SHA256SUMS.txt")"
    [ -n "$expected" ] || die "SHA256SUMS.txt has no entry for $2. Nothing was installed."
    actual="$(sha256 "$1")"
    [ "$expected" = "$actual" ] ||
        die "checksum mismatch for $2 (expected $expected, got $actual). Nothing was installed."
    say "    ok: $actual"
}

# fetch_sums: download SHA256SUMS.txt from the release and print the HTTP status code.
fetch_sums() {
    fetch "$base/SHA256SUMS.txt" "$tmp/SHA256SUMS.txt" ||
        die "could not download $base/SHA256SUMS.txt (network or TLS error). Nothing was installed."
}

# Ask before running Astral's uv installer. Reads the answer from the terminal, because
# stdin is this script when it is piped into sh.
confirm_uv_install() {
    say "uv is not installed. It can be installed with Astral's official installer:"
    say "    $UV_INSTALLER_URL"
    if [ "${BOT_FARM_SILENCE_TRACKER_ALLOW_UV:-0}" = "1" ]; then
        say "BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1 is set, so installing it."
        return 0
    fi
    if (exec </dev/tty) 2>/dev/null && (: >/dev/tty) 2>/dev/null; then
        printf 'Install uv now? [y/N] ' >/dev/tty
        answer=""
        # At end of input read fails but keeps what was typed; use that, do not discard it.
        read -r answer </dev/tty || :
        case "$answer" in
            y | Y | yes | YES | Yes) return 0 ;;
        esac
        die "uv was not installed. Install it yourself (https://docs.astral.sh/uv/) and rerun, or rerun with BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1."
    fi
    die "there is no terminal to ask whether to install uv. Rerun with BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1 to allow it, or install uv yourself (https://docs.astral.sh/uv/)."
}

# ---------------------------------------------------------------- platform choice
DEFAULT_PLATFORM="reddit-archive"

# Add one platform name to $PLATFORMS (a comma list) unless it is already there.
add_platform() {
    case ",$PLATFORMS," in
        *",$1,"*) ;;
        *) PLATFORMS="${PLATFORMS:+$PLATFORMS,}$1" ;;
    esac
}

# normalize_platforms LIST: check LIST (comma or space separated) against the platforms
# this installer knows and set PLATFORMS to the lowercase, de-duplicated comma list. Runs
# before anything is downloaded, so a typo installs nothing.
normalize_platforms() {
    PLATFORMS=""
    set -f
    for item in $(printf '%s' "$1" | tr ',' ' '); do
        item="$(printf '%s' "$item" | tr '[:upper:]' '[:lower:]')"
        case "$item" in
            reddit-archive | bluesky | x | reddit) add_platform "$item" ;;
            *)
                set +f
                die "unknown platform '$item'. Choose from: reddit-archive, bluesky, x, reddit. Nothing was installed."
                ;;
        esac
    done
    set +f
    [ -n "$PLATFORMS" ] || die "--platforms needs at least one platform (for example reddit-archive). Nothing was installed."
}

# Whether there is a terminal to ask on. stdin is this script when it is piped into sh,
# so the terminal is opened directly.
have_tty() {
    (exec </dev/tty) 2>/dev/null && (: >/dev/tty) 2>/dev/null
}

# Put the terminal back in its normal line mode: typed text is echoed, Enter ends the
# answer, and every new line starts at the left edge ("onlcr"). Another program sharing
# the terminal (one that was stopped while it had the terminal in raw mode, for
# example) can leave these off. Then the Enter after the answer, and every line the
# installer prints after it, does not return to the left edge, so the output runs
# diagonally across the screen as one long, padded line. The installer cannot rely on
# the terminal being in a good state, so it sets these before the menu and again after
# the answer. A terminal that is already in line mode is not changed.
tty_line_mode() {
    stty icanon echo icrnl opost onlcr </dev/tty >/dev/tty 2>/dev/null || :
}

# Offer the platform menu on the terminal. Enter picks the default. Sets PLATFORMS.
prompt_platforms() {
    tty_line_mode
    {
        printf '\n%s\n' "Which social platforms do you want to scan?"
        printf '%s\n' "  [1] Reddit (Arctic Shift archive, no account needed) - default"
        printf '%s\n' "  [2] Bluesky (no account needed for the default mode)"
        printf '%s\n' "  [3] X (needs your own paid API token)"
        printf '%s\n' "  [4] Reddit official API (needs approved API credentials)"
        printf '%s\n' "Several are fine, for example 1,2. You can change this later with:"
        printf '%s\n' "  $TOOL_NAME config set-platforms reddit-archive,bluesky"
    } >/dev/tty
    tries=0
    while :; do
        tty_line_mode
        printf 'Choice [1]: ' >/dev/tty
        answer=""
        # At end of input read fails but keeps what was typed; use that, do not discard
        # it. The terminal echoed no Enter then, so end the line here.
        read -r answer </dev/tty || printf '\n' >/dev/tty
        tty_line_mode
        # Back to the left edge, in case the Enter was echoed while line mode was off.
        printf '\r' >/dev/tty
        PLATFORMS=""
        bad=""
        set -f
        for tok in $(printf '%s' "$answer" | tr ',' ' '); do
            case "$tok" in
                1) add_platform reddit-archive ;;
                2) add_platform bluesky ;;
                3) add_platform x ;;
                4) add_platform reddit ;;
                *) bad="$tok" ;;
            esac
        done
        set +f
        if [ -z "$bad" ]; then
            if [ -z "$PLATFORMS" ]; then
                PLATFORMS="$DEFAULT_PLATFORM"
                PLATFORMS_FROM="default"
            fi
            return 0
        fi
        printf '%s\n' "'$bad' is not one of 1, 2, 3 or 4." >/dev/tty
        tries=$((tries + 1))
        [ "$tries" -lt 5 ] || die "no valid platform choice. Rerun with --platforms reddit-archive. Nothing was installed."
    done
}

# Set PLATFORMS and PLATFORMS_FROM ("flag", "menu" or "default") from --platforms, the
# environment variable, the menu (only with a terminal) or the default.
choose_platforms() {
    if [ -n "$PLATFORMS_ARG" ]; then
        normalize_platforms "$PLATFORMS_ARG"
        PLATFORMS_FROM="flag"
    elif have_tty; then
        PLATFORMS_FROM="menu"
        prompt_platforms
    else
        PLATFORMS="$DEFAULT_PLATFORM"
        PLATFORMS_FROM="default"
    fi
}

# save_platforms EXE: have the installed program record the choice. The program owns the
# config file format; this script only passes the list. A choice that was not made (the
# default) does not overwrite platforms the user saved earlier. A program without the
# config command (an older pinned build) is skipped quietly.
save_platforms() {
    # Builds pinned to a release from before the config command cannot save a choice.
    "$1" config --help </dev/null >/dev/null 2>&1 || return 0
    say "==> Saving your platform choice: $PLATFORMS"
    if [ "$PLATFORMS_FROM" = "default" ]; then
        "$1" config set-platforms --if-unset "$PLATFORMS" </dev/null ||
            warn "could not save the platform choice. Run: $TOOL_NAME config set-platforms $PLATFORMS"
    else
        "$1" config set-platforms "$PLATFORMS" </dev/null ||
            warn "could not save the platform choice. Run: $TOOL_NAME config set-platforms $PLATFORMS"
    fi
}

# Print which environment variables the chosen platforms need and where to get them. This
# only prints: credentials are never asked for, read or stored here.
credential_help() {
    case ",$PLATFORMS," in
        *,x,* | *,reddit,*) ;;
        *) return 0 ;;
    esac
    say ""
    say "Some of the platforms you chose need credentials. Set them in your shell or your"
    say "environment yourself; this installer never asks for or stores them."
    case ",$PLATFORMS," in
        *,x,*)
            say ""
            say "  X (--platform x):"
            say "    BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN"
            say "    Get an app bearer token at https://developer.x.com (pay-per-use credits)."
            say "    X bills per post, so every scan needs an explicit --max-usd."
            ;;
    esac
    case ",$PLATFORMS," in
        *,reddit,*)
            say ""
            say "  Reddit official API (--platform reddit):"
            say "    BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID"
            say "    BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET"
            say "    BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME"
            say "    (optional) BOT_FARM_SILENCE_TRACKER_REDDIT_PASSWORD"
            say "    Reddit needs to approve API access first (Responsible Builder Policy),"
            say "    then create an app at https://www.reddit.com/prefs/apps."
            ;;
    esac
}

# The command shown as the first run: the first chosen platform that needs no credentials.
first_run_hint() {
    case ",$PLATFORMS," in
        *,reddit-archive,*) say "  $TOOL_NAME scan --platform reddit-archive --n 30" ;;
        *,bluesky,*) say "  $TOOL_NAME scan --platform bluesky --n 30" ;;
        *) say "  $TOOL_NAME scan --platform $DEFAULT_PLATFORM --n 30   (after setting the credentials above, use your platform)" ;;
    esac
}

# ---------------------------------------------------------------- uv path
install_with_uv() {
    src="$1"
    [ "${BOT_FARM_SILENCE_TRACKER_NO_UV:-0}" = "1" ] &&
        die "BOT_FARM_SILENCE_TRACKER_NO_UV=1 is set, so uv is not used. Nothing was installed."
    # Local paths become absolute so uv resolves them the same way from any folder.
    if [ -d "$src" ]; then
        src="$(cd "$src" && pwd)"
    elif [ -f "$src" ]; then
        src="$(cd "$(dirname "$src")" && pwd)/$(basename "$src")"
    fi
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
    export PATH
    if command -v uv >/dev/null 2>&1; then
        say "==> uv found: $(command -v uv)"
    else
        confirm_uv_install
        uv_script="$tmp/uv-install.sh"
        code="$(fetch "$UV_INSTALLER_URL" "$uv_script")" ||
            die "could not download the uv installer from $UV_INSTALLER_URL."
        [ "$code" = "200" ] || die "could not download the uv installer (HTTP $code)."
        sh "$uv_script"
        command -v uv >/dev/null 2>&1 ||
            die "uv was installed but is not on PATH. Open a new terminal and rerun this script."
    fi
    say "==> Installing $TOOL_NAME with uv from: $src"
    uv tool install --force "$src"
    tool_bin="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
    say "==> Verifying"
    [ -x "$tool_bin/$TOOL_NAME" ] || die "$TOOL_NAME was not found in $tool_bin after install."
    "$tool_bin/$TOOL_NAME" --version
    save_platforms "$tool_bin/$TOOL_NAME"
    if ! PATH="$ORIG_PATH" command -v "$TOOL_NAME" >/dev/null 2>&1; then
        say ""
        say "Note: $tool_bin may not be on the PATH of new terminals yet."
        say "Run \"uv tool update-shell\" and open a new terminal, or call $tool_bin/$TOOL_NAME directly."
    fi
    credential_help
    say ""
    say "Done. Try it:"
    first_run_hint
    say ""
    say "Upgrade later: uv tool upgrade $TOOL_NAME   (or rerun this script)"
    say "Uninstall:      uv tool uninstall $TOOL_NAME"
}

# Download the Python package (wheel) attached to the release and check it against
# SHA256SUMS.txt. Sets wheel_file and returns 0, or returns 1 when the release has no
# wheel. A failed download or a checksum mismatch stops the installer.
release_wheel() {
    code="$(fetch_sums)" || exit 1
    case "$code" in
        200) ;;
        404) return 1 ;;
        *) die "downloading $base/SHA256SUMS.txt failed with HTTP $code. Nothing was installed." ;;
    esac
    name="$(awk '{ f = $2; sub(/^\*/, "", f) }
        f ~ /^bot_farm_silence_tracker-[A-Za-z0-9._+]+-py3-none-any\.whl$/ { print f; exit }' \
        "$tmp/SHA256SUMS.txt")"
    [ -n "$name" ] || return 1
    say "==> Downloading $name"
    say "    $base/$name"
    code="$(fetch "$base/$name" "$tmp/$name")" ||
        die "could not download $base/$name (network or TLS error). Nothing was installed."
    [ "$code" = "200" ] || die "downloading $base/$name failed with HTTP $code. Nothing was installed."
    verify_checksum "$tmp/$name" "$name"
    verify_attestation "$tmp/$name"
    wheel_file="$tmp/$name"
}

uv_fallback() {
    say "$1"
    if [ "${BOT_FARM_SILENCE_TRACKER_NO_UV:-0}" = "1" ]; then
        die "BOT_FARM_SILENCE_TRACKER_NO_UV=1 is set, so not falling back to uv. Nothing was installed."
    fi
    if [ -n "$base" ] && release_wheel; then
        say "Installing the Python package from the release with uv instead."
        install_with_uv "$wheel_file"
        return 0
    fi
    # Only a test server can stand in for the repository of an unpublished copy.
    if unpublished && [ -z "${BOT_FARM_SILENCE_TRACKER_GITHUB_URL:-}" ]; then
        die "This copy has not been published yet: replace $OWNER_PLACEHOLDER with the GitHub account that hosts it (see Publishing your copy in the README)."
    fi
    src="git+$GITHUB/$REPO"
    if [ -n "$tag" ]; then
        src="$src@$tag"
    fi
    say "Installing from source with uv instead. This needs git; on macOS, the first use of"
    say "git may ask to install the Command Line Tools."
    install_with_uv "$src"
}

is_musl() {
    if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
        return 0
    fi
    for f in /lib/ld-musl-*; do
        [ -e "$f" ] && return 0
    done
    return 1
}

detect_asset() {
    os="$(uname -s)"
    arch="$(uname -m)"
    if [ "$os" = "Darwin" ] && [ "$arch" = "x86_64" ] &&
        [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = "1" ]; then
        # A shell running under Rosetta on Apple silicon: the native build is faster.
        arch="arm64"
    fi
    case "$os/$arch" in
        Linux/x86_64 | Linux/amd64) is_musl || printf '%s' "$TOOL_NAME-linux-x64" ;;
        Linux/aarch64 | Linux/arm64) is_musl || printf '%s' "$TOOL_NAME-linux-arm64" ;;
        Darwin/arm64 | Darwin/aarch64) printf '%s' "$TOOL_NAME-macos-arm64" ;;
        Darwin/x86_64) printf '%s' "$TOOL_NAME-macos-x64" ;;
    esac
    return 0
}

verify_attestation() {
    # Optional extra check: only when the GitHub CLI is installed and logged in, and the
    # file came from GitHub rather than a test server.
    # A copy that has not been published has no repository to check the attestation
    # against. main() only gets here then when a download base override replaces GitHub.
    if unpublished; then
        warn "this copy has not been published (the repository $REPO still holds the owner placeholder), so the build provenance attestation was not checked; only the SHA256 checksum was verified."
        return 0
    fi
    [ -z "${BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL:-}" ] || return 0
    command -v gh >/dev/null 2>&1 || return 0
    gh auth status </dev/null >/dev/null 2>&1 || return 0
    say "==> Verifying build provenance with gh attestation verify"
    if verify_output="$(gh attestation verify "$1" --repo "$REPO" </dev/null 2>&1)"; then
        say "    ok"
        return 0
    fi
    # A release made while the repository is private has no attestations (they need a
    # public repository or GitHub Enterprise Cloud). That is not a failed check, so warn
    # and carry on; the SHA256 checksum was already verified. Any other failure stops.
    if printf '%s\n' "$verify_output" | grep -qi 'no attestations found'; then
        warn "$(basename "$1") has no build provenance attestation, so only its SHA256 checksum was verified."
        return 0
    fi
    die "the build provenance attestation for $(basename "$1") did not verify. Nothing was installed."
}

add_to_path() {
    case ":$ORIG_PATH:" in
        *":$INSTALL_DIR:"*) return 0 ;;
    esac
    if [ "${BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH:-0}" = "1" ]; then
        say "Note: $INSTALL_DIR is not on your PATH. Add it, or run $INSTALL_DIR/$TOOL_NAME directly."
        return 0
    fi
    case "${SHELL:-}" in
        */fish)
            # XDG_CONFIG_HOME and ZDOTDIR count only when absolute, as in the program's own
            # config lookup; a relative value would point into whatever folder this runs in.
            case "${XDG_CONFIG_HOME:-}" in /*) xdg_dir="$XDG_CONFIG_HOME" ;; *) xdg_dir="$HOME/.config" ;; esac
            profile="$xdg_dir/fish/conf.d/$TOOL_NAME.fish"
            line="fish_add_path -g \"$INSTALL_DIR\""
            reload="source \"$profile\""
            ;;
        */zsh)
            case "${ZDOTDIR:-}" in /*) zsh_dir="$ZDOTDIR" ;; *) zsh_dir="$HOME" ;; esac
            profile="$zsh_dir/.zshrc"
            line="export PATH=\"$INSTALL_DIR:\$PATH\""
            ;;
        */bash)
            if [ "$(uname -s)" = "Darwin" ]; then profile="$HOME/.bash_profile"; else profile="$HOME/.bashrc"; fi
            line="export PATH=\"$INSTALL_DIR:\$PATH\""
            ;;
        *)
            profile="$HOME/.profile"
            line="export PATH=\"$INSTALL_DIR:\$PATH\""
            ;;
    esac
    [ -n "${reload:-}" ] || reload=". \"$profile\""
    if grep -qsxF "$line" "$profile"; then
        say "Note: $profile already adds $INSTALL_DIR to PATH. Open a new terminal to use it."
        return 0
    fi
    mkdir -p "$(dirname "$profile")"
    printf '\n# Added by the Bot Farm Silence Tracker installer\n%s\n' "$line" >>"$profile"
    say "==> Added $INSTALL_DIR to PATH in $profile"
    say "    Open a new terminal (or run: $reload) for it to take effect."
}

main() {
    PLATFORMS_ARG="${BOT_FARM_SILENCE_TRACKER_PLATFORMS:-}"
    SOURCE_ARG=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h | --help)
                say "Usage: install.sh [--platforms LIST] [SOURCE]"
                say "Installs the prebuilt $TOOL_NAME from the latest GitHub Release into ~/.local/bin."
                say "With SOURCE (a git URL, PyPI name, local checkout or wheel file) it uses uv instead."
                say "--platforms LIST: the platforms to scan, from reddit-archive (default), bluesky, x, reddit."
                say "See the comments at the top of this script for the environment variables."
                exit 0
                ;;
            --platforms)
                [ "$#" -ge 2 ] || die "--platforms needs a value, for example --platforms reddit-archive,bluesky"
                PLATFORMS_ARG="$2"
                shift 2
                ;;
            --platforms=*)
                PLATFORMS_ARG="${1#--platforms=}"
                shift
                ;;
            -*) die "unknown option: $1 (see --help)" ;;
            *)
                [ -z "$SOURCE_ARG" ] || die "only one SOURCE can be given (got '$SOURCE_ARG' and '$1')"
                SOURCE_ARG="$1"
                shift
                ;;
        esac
    done
    # An unpublished copy would download from, and check attestations against, a
    # repository that does not exist. Refuse before anything is downloaded, unless a
    # download base override (a test server or a release folder) replaces GitHub.
    if unpublished && [ -z "${BOT_FARM_SILENCE_TRACKER_GITHUB_URL:-}" ] &&
        [ -z "${BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL:-}" ]; then
        die "This copy has not been published yet: replace $OWNER_PLACEHOLDER with the GitHub account that hosts it (see Publishing your copy in the README)."
    fi
    # Checked first, so a misspelled platform installs nothing.
    if [ -n "$PLATFORMS_ARG" ]; then normalize_platforms "$PLATFORMS_ARG"; fi

    if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
        warn "running as root. This installer does not need sudo; consider using your normal user."
    fi

    ORIG_PATH="$PATH"
    INSTALL_DIR="$(resolve_install_dir "${BOT_FARM_SILENCE_TRACKER_INSTALL_DIR:-$HOME/.local/bin}")"
    SOURCE="${SOURCE_ARG:-${BOT_FARM_SILENCE_TRACKER_SOURCE:-}}"
    version="${BOT_FARM_SILENCE_TRACKER_VERSION:-}"
    GITHUB="${BOT_FARM_SILENCE_TRACKER_GITHUB_URL:-https://github.com}"
    GITHUB="${GITHUB%/}"
    require_https "$GITHUB"

    tmp="$(mktemp -d "${TMPDIR:-/tmp}/bfst-install.XXXXXX")"
    staged=""
    trap 'rm -rf "$tmp"; if [ -n "$staged" ]; then rm -f "$staged"; fi' EXIT
    # Ctrl-C or a kill stops the installer; the EXIT trap then removes the temp folder.
    trap 'exit 130' INT TERM

    # Ask (only on a terminal) before downloading, so the rest runs unattended.
    choose_platforms

    if [ -n "$SOURCE" ]; then
        install_with_uv "$SOURCE"
        return 0
    fi

    command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 ||
        die "curl or wget is required to download $TOOL_NAME. Install one of them and rerun."

    # base is the folder URL of the release files and tag the release tag; either can be
    # empty (no release yet, or a RELEASE_BASE_URL without a version).
    if ! command -v curl >/dev/null 2>&1 && wget_is_busybox; then
        if [ "${BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET:-0}" != "1" ]; then
            die "the only downloader here is BusyBox wget, which may not validate TLS certificates, so a downloaded program could be tampered with. Install curl (for example: apk add curl, or apt-get install curl) and rerun. To accept the risk anyway, set BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET=1."
        fi
        warn "using BusyBox wget, which may not validate TLS certificates (BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET=1 is set). Install curl to avoid this."
    fi

    tag="$version"
    if [ -n "${BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL:-}" ]; then
        base="${BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL%/}"
        require_https "$base"
    else
        if [ -z "$tag" ]; then
            say "==> Looking up the latest release"
            tag="$(latest_tag)" || exit 1
            case "$tag" in
                *[!A-Za-z0-9._+-]*) die "unexpected release tag from GitHub: $tag" ;;
            esac
            if [ -n "$tag" ]; then say "    $tag"; else say "    there is no release yet"; fi
        fi
        base=""
        if [ -n "$tag" ]; then
            base="$GITHUB/$REPO/releases/download/$tag"
        fi
    fi

    asset="$(detect_asset)"
    if [ -z "$asset" ]; then
        uv_fallback "No prebuilt program for $(uname -s)/$(uname -m)$(is_musl && echo ' (musl)')."
        return 0
    fi
    if [ -z "$base" ]; then
        uv_fallback "There is no release yet, so there is no prebuilt program to download."
        return 0
    fi

    say "==> Downloading $asset"
    say "    $base/$asset"
    code="$(fetch "$base/$asset" "$tmp/$asset")" ||
        die "could not download $base/$asset (network or TLS error). Nothing was installed."
    case "$code" in
        200) ;;
        404)
            if [ -n "$version" ] || [ -n "${BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL:-}" ]; then
                die "$asset was not found at $base (HTTP 404). Check the version tag. Nothing was installed."
            fi
            uv_fallback "Release $tag has no prebuilt program for this system."
            return 0
            ;;
        *) die "downloading $base/$asset failed with HTTP $code. Nothing was installed." ;;
    esac
    code="$(fetch_sums)" || exit 1
    [ "$code" = "200" ] ||
        die "downloading $base/SHA256SUMS.txt failed with HTTP $code. Nothing was installed."
    verify_checksum "$tmp/$asset" "$asset"
    verify_attestation "$tmp/$asset"

    chmod 755 "$tmp/$asset"
    say "==> Checking that it runs on this system"
    "$tmp/$asset" --version </dev/null ||
        die "the downloaded program does not run on this system. Nothing was installed."

    mkdir -p "$INSTALL_DIR"
    # Copy next to the target first, then rename, so an existing install is replaced in
    # one step even when the temp folder is on another file system.
    staged="$INSTALL_DIR/.$TOOL_NAME.new.$$"
    cp "$tmp/$asset" "$staged"
    chmod 755 "$staged"
    mv -f "$staged" "$INSTALL_DIR/$TOOL_NAME"
    say "==> Installed to $INSTALL_DIR/$TOOL_NAME"

    add_to_path
    save_platforms "$INSTALL_DIR/$TOOL_NAME"
    credential_help

    say ""
    say "Done. Next, try a first run (use the full path if you have not opened a new terminal):"
    first_run_hint
    say ""
    say "Uninstall: rm $INSTALL_DIR/$TOOL_NAME"
}

main "$@"
