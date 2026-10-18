#!/bin/sh
# End-to-end test of install.sh against a fake GitHub release served on this computer.
#
#   sh tests/installer/test_install_sh.sh [PORT]
#
# Runs from the repository root. Needs python3 (for the fake server) and curl or wget.
# A fake "gh" on PATH writes to stderr, the way the real one does, and fake "uv" and
# "uname" commands let the uv fallback be checked without installing anything. The fake
# program records "config set-platforms" calls in $HOME, like the real one would write its
# config, so the tests can see what the installer asked for. Installer runs have no
# controlling terminal (so no prompt) unless a test uses run_in_pty.py.
set -eu

PORT="${1:-8765}"
PYTHON="${PYTHON:-python3}"
ROOT="$(pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/bfst-installer-test.XXXXXX")"
pids=""
trap 'for p in $pids; do kill "$p" 2>/dev/null || true; done; rm -rf "$work"' EXIT
failures=0
PTY_ENV=""

# check MESSAGE COMMAND...: report whether COMMAND succeeds.
check() {
    message="$1"
    shift
    if "$@"; then
        echo "  ok: $message"
    else
        echo "  FAIL: $message"
        failures=$((failures + 1))
    fi
}

lacks() { ! grep -q "$@"; }
# no_read_in FUNCTION: install.sh's FUNCTION never calls read.
no_read_in() { ! sed -n "/^$1()/,/^}/p" "$ROOT/install.sh" | grep -qw read; }
runs() { "$1" --version | grep -q 0.0.1-test; }
# clean_lines FILE: in the raw terminal output in FILE, from the first prompt on, every
# line feed has a carriage return right before or right after it, so no line starts
# part way across the screen.
clean_lines() {
    "$PYTHON" - "$1" <<'PY'
import sys

data = open(sys.argv[1], "rb").read()
data = data[data.index(b"Choice [1]: "):]
for i, byte in enumerate(data):
    if byte == 10 and data[i - 1 : i] != b"\r" and data[i + 1 : i + 2] != b"\r":
        sys.exit("line feed without a carriage return at %d: %r" % (i, data[max(0, i - 40) : i + 40]))
PY
}

mkdir -p "$work/bin" "$work/uv-bin"
# The "program" in the release: prints a version, like the real one.
cat >"$work/program" <<'EOF'
#!/bin/sh
case "$1" in
    config)
        # FAKE_CONFIG_UNSUPPORTED=1 plays an older pinned build with no config command.
        [ "${FAKE_CONFIG_UNSUPPORTED:-0}" != "1" ] || exit 2
        [ "$2" = "--help" ] && exit 0
        echo "$*" >>"$HOME/config-calls"
        if [ "$2" = "set-platforms" ]; then
            for a in "$@"; do last="$a"; done
            printf '%s\n' "$last" >"$HOME/config-platforms"
            [ "${FAKE_CONFIG_FAIL:-0}" != "1" ] || exit 2
        fi
        ;;
    *) echo "bot-farm-silence-tracker 0.0.1-test" ;;
esac
EOF
# A wheel stand-in; the fake uv does not open it.
printf 'not really a wheel\n' >"$work/bot_farm_silence_tracker-0.0.1-py3-none-any.whl"
cat >"$work/bin/gh" <<'EOF'
#!/bin/sh
echo "$*" >>"$FAKE_GH_LOG"
echo "Loaded digest sha256:abc for file" >&2
if [ "$1 $2" = "auth status" ] && [ "${FAKE_GH_AUTH:-ok}" = "fail" ]; then
    echo "You are not logged into any GitHub hosts." >&2
    exit 1
fi
if [ "$1 $2" = "attestation verify" ]; then
    case "${FAKE_GH_VERIFY:-ok}" in
        none) echo "Error: no attestations found for subject sha256:abc" >&2; exit 1 ;;
        bad) echo "Error: verification failed: signature mismatch" >&2; exit 1 ;;
    esac
fi
echo ok
EOF
cat >"$work/bin/uv" <<EOF
#!/bin/sh
echo "FAKEUV \$*" >>"$work/uv.log"
case "\$1 \$2" in
    "tool dir") echo "$work/uv-bin" ;;
    "tool install") cp "$work/program" "$work/uv-bin/bot-farm-silence-tracker"
        chmod 755 "$work/uv-bin/bot-farm-silence-tracker" ;;
esac
EOF
mkdir -p "$work/riscv"
cat >"$work/riscv/uname" <<'EOF'
#!/bin/sh
case "$1" in -s) echo Linux ;; -m) echo riscv64 ;; *) command -p uname "$@" ;; esac
EOF
chmod 755 "$work/program" "$work/bin/gh" "$work/bin/uv" "$work/riscv/uname"

# The installer under test is a published copy whose REPO names a test repository. A
# copy that still holds the owner placeholder (written in two parts here, so that
# publishing this copy with the README's replace command leaves the test intact) must
# refuse to run unless a download base override replaces GitHub.
OWNER_PLACEHOLDER="_""_OWNER__"
TEST_REPO="example-owner/bot-farm-silence-tracker"
UNPUBLISHED_REPO="$OWNER_PLACEHOLDER/bot-farm-silence-tracker"
INSTALLER="$work/install.sh"
UNPUBLISHED="$work/install-unpublished.sh"
sed "s|^REPO=.*|REPO=\"$TEST_REPO\"|" "$ROOT/install.sh" >"$INSTALLER"
sed "s|^REPO=.*|REPO=\"$UNPUBLISHED_REPO\"|" "$ROOT/install.sh" >"$UNPUBLISHED"
grep -q "^REPO=\"$TEST_REPO\"\$" "$INSTALLER" || { echo "could not make the published copy" >&2; exit 1; }
REFUSAL="This copy has not been published yet: replace $OWNER_PLACEHOLDER with the GitHub account that hosts it (see Publishing your copy in the README)."

files=""
for name in bot-farm-silence-tracker-linux-x64 bot-farm-silence-tracker-linux-arm64 \
    bot-farm-silence-tracker-macos-arm64 bot-farm-silence-tracker-macos-x64; do
    files="$files --file $name=$work/program"
done
# shellcheck disable=SC2086 # $files is a list of arguments
"$PYTHON" tests/installer/fake_release_server.py --port "$PORT" --repo "$TEST_REPO" --tag v0.0.1-test $files \
    --file "bot_farm_silence_tracker-0.0.1-py3-none-any.whl=$work/bot_farm_silence_tracker-0.0.1-py3-none-any.whl" \
    >"$work/server1.log" 2>&1 &
pids="$pids $!"
"$PYTHON" tests/installer/fake_release_server.py --port "$((PORT + 1))" --repo "$TEST_REPO" >"$work/server2.log" 2>&1 &
pids="$pids $!"
# A release of the unpublished repository, for the download base override checks.
# shellcheck disable=SC2086 # $files is a list of arguments
"$PYTHON" tests/installer/fake_release_server.py --port "$((PORT + 5))" --repo "$UNPUBLISHED_REPO" \
    --tag v0.0.1-test $files >"$work/server-unpublished.log" 2>&1 &
pids="$pids $!"
# Servers whose releases/latest answers with an error or a page instead of a redirect.
n=2
for mode in 503 429 200-html; do
    "$PYTHON" tests/installer/fake_release_server.py --port "$((PORT + n))" --repo "$TEST_REPO" \
        --tag v0.0.1-test --latest-mode "$mode" --file "bot-farm-silence-tracker-linux-x64=$work/program" \
        --file "bot-farm-silence-tracker-macos-arm64=$work/program" \
        --file "bot-farm-silence-tracker-linux-arm64=$work/program" \
        --file "bot-farm-silence-tracker-macos-x64=$work/program" >"$work/server-$mode.log" 2>&1 &
    pids="$pids $!"
    n=$((n + 1))
done
for p in "$PORT" "$((PORT + 1))" "$((PORT + 2))" "$((PORT + 3))" "$((PORT + 4))" "$((PORT + 5))"; do
    repo="$TEST_REPO"
    [ "$p" != "$((PORT + 5))" ] || repo="$UNPUBLISHED_REPO"
    i=0
    until curl -fsS -o /dev/null "http://127.0.0.1:$p/$repo/releases" 2>/dev/null ||
        wget -q -O /dev/null "http://127.0.0.1:$p/$repo/releases" 2>/dev/null; do
        i=$((i + 1))
        [ "$i" -lt 50 ] || { echo "The fake release server on port $p did not start." >&2; exit 1; }
        sleep 0.2
    done
done

# Run a command with no controlling terminal, so /dev/tty cannot be opened.
notty() {
    "$PYTHON" -c 'import os, sys
try:
    os.setsid()
except OSError:
    pass
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
}

# ARGS holds extra install.sh arguments for the next run (word-split on purpose), and
# SCRIPT the installer copy it runs.
ARGS=""
SCRIPT="$INSTALLER"

# run NAME PORT PATH_PREFIX [VAR=VALUE...]: run install.sh in a clean environment.
run() {
    name="$1" port="$2" prefix="$3"
    shift 3
    echo "--- $name"
    mkdir -p "$work/home-$name"
    code=0
    # shellcheck disable=SC2086 # $ARGS is a list of arguments
    notty env -i HOME="$work/home-$name" PATH="$prefix$work/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        SHELL=/bin/sh TMPDIR="$work" FAKE_GH_LOG="$work/gh-$name.log" \
        BOT_FARM_SILENCE_TRACKER_GITHUB_URL="http://127.0.0.1:$port" BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1 \
        BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH=1 \
        BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET="${BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET:-0}" "$@" sh "$SCRIPT" $ARGS </dev/null >"$work/out-$name.txt" 2>&1 ||
        code=$?
    sed 's/^/    | /' "$work/out-$name.txt"
    out="$work/out-$name.txt"
    exe="$work/home-$name/.local/bin/bot-farm-silence-tracker"
    home="$work/home-$name"
    ARGS=""
    SCRIPT="$INSTALLER"
}

# saved NAME: the platform list the installer asked the program to save in run NAME.
saved() { cat "$work/home-$1/config-platforms" 2>/dev/null || echo "(nothing saved)"; }
saved_is() { [ "$(saved "$1")" = "$2" ]; }

run latest-with-gh "$PORT" ""
check "installs the latest release when gh is logged in" [ "$code" = 0 ]
check "finds the latest tag through the releases/latest redirect" grep -q "v0.0.1-test" "$out"
check "calls gh attestation verify" grep -q "attestation verify" "$work/gh-latest-with-gh.log"
check "the installed program runs" runs "$exe"
check "the default writes reddit-archive" saved_is latest-with-gh reddit-archive
check "the default keeps platforms saved earlier (--if-unset)" grep -q \
    "^config set-platforms --if-unset reddit-archive\$" "$work/home-latest-with-gh/config-calls"
check "does not prompt without a terminal" lacks "Which social platforms" "$out"
check "prints no credential guidance for reddit-archive" lacks "BEARER_TOKEN" "$out"

ARGS="--platforms bluesky,reddit-archive"
run platforms-flag "$PORT" ""
check "installs with --platforms" [ "$code" = 0 ]
check "--platforms bluesky,reddit-archive is written" saved_is platforms-flag bluesky,reddit-archive
check "an explicit list replaces earlier ones (no --if-unset)" lacks -- "--if-unset" "$work/home-platforms-flag/config-calls"
check "does not prompt with --platforms" lacks "Which social platforms" "$out"

ARGS="--platforms=Bluesky,BLUESKY"
run platforms-equals "$PORT" ""
check "--platforms=LIST is lowercased and de-duplicated" saved_is platforms-equals bluesky

run platforms-env "$PORT" "" BOT_FARM_SILENCE_TRACKER_PLATFORMS=bluesky
check "BOT_FARM_SILENCE_TRACKER_PLATFORMS is written" saved_is platforms-env bluesky

ARGS="--platforms reddit-archive,myspace"
run platforms-invalid "$PORT" ""
check "an invalid platform name fails" [ "$code" != 0 ]
check "names the invalid platform" grep -q "unknown platform 'myspace'" "$out"
check "fails before downloading anything" lacks "Downloading" "$out"
check "installs nothing then" [ ! -e "$exe" ]
check "saves nothing then" [ ! -e "$home/config-calls" ]

run platforms-invalid-env "$PORT" "" BOT_FARM_SILENCE_TRACKER_PLATFORMS=nope
check "an invalid BOT_FARM_SILENCE_TRACKER_PLATFORMS fails before installing" [ "$code" != 0 ]
check "installs nothing for a bad env value" [ ! -e "$exe" ]

ARGS="--platforms"
run platforms-missing-value "$PORT" ""
check "--platforms without a value fails" [ "$code" != 0 ]

ARGS="--bogus"
run unknown-option "$PORT" ""
check "an unknown option fails" [ "$code" != 0 ]
check "installs nothing for an unknown option" [ ! -e "$exe" ]

ARGS="--platforms x,reddit"
run platforms-credentials "$PORT" ""
check "installs with credential platforms" [ "$code" = 0 ]
check "saves x,reddit" saved_is platforms-credentials x,reddit
check "names the X token variable" grep -q "BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN" "$out"
check "says where to get the X token" grep -q "https://developer.x.com" "$out"
check "names the Reddit variables" grep -q "BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET" "$out"
check "says where to get Reddit access" grep -q "https://www.reddit.com/prefs/apps" "$out"
check "says it never asks for credentials" grep -q "never asks for or stores" "$out"
check "does not prompt for credentials" lacks -i "token:\|password:\|enter your" "$out"
check "the credential help code never reads input" no_read_in credential_help

run platforms-save-fails "$PORT" "" FAKE_CONFIG_FAIL=1
check "a failed config save only warns" [ "$code" = 0 ]
check "tells how to save it by hand" grep -q "config set-platforms reddit-archive" "$out"

run platforms-old-build "$PORT" "" FAKE_CONFIG_UNSUPPORTED=1
check "a build without the config command still installs" [ "$code" = 0 ]
check "skips the platform save quietly" lacks "Saving your platform choice" "$out"
check "with no warning about it" lacks "could not save" "$out"
check "and asks the program to save nothing" [ ! -e "$home/config-calls" ]
check "still installs the program" runs "$exe"

run gh-not-logged-in "$PORT" "" FAKE_GH_AUTH=fail
check "installs when gh is installed but not logged in" [ "$code" = 0 ]
check "skips the attestation check then" lacks "attestation verify" "$work/gh-gh-not-logged-in.log"

run no-attestation "$PORT" "" FAKE_GH_VERIFY=none
check "installs with a warning when the release has no attestation" [ "$code" = 0 ]
check "says there is no attestation" grep -q "no build provenance attestation" "$out"
check "installs the program then" runs "$exe"

run bad-attestation "$PORT" "" FAKE_GH_VERIFY=bad
check "fails when the attestation does not verify" [ "$code" != 0 ]
check "says the attestation did not verify" grep -q "did not verify" "$out"
check "installs nothing then" [ ! -e "$exe" ]

run missing-version "$PORT" "" BOT_FARM_SILENCE_TRACKER_VERSION=v9.9.9
check "fails for a version that does not exist" [ "$code" != 0 ]
check "says the file was not found" grep -q "HTTP 404" "$out"
check "installs nothing then" [ ! -e "$exe" ]

run no-release "$((PORT + 1))" "" BOT_FARM_SILENCE_TRACKER_NO_UV=1
check "stops when there is no release and uv is not allowed" [ "$code" != 0 ]
check "says there is no release yet" grep -q "no release yet" "$out"

run no-release-uv "$((PORT + 1))" ""
check "installs from source with uv when there is no release" [ "$code" = 0 ]
check "installs main from git then" grep -q \
    "tool install --force git+http://127.0.0.1:$((PORT + 1))/$TEST_REPO\$" "$work/uv.log"
check "the uv path writes the default too" saved_is no-release-uv reddit-archive

ARGS="--platforms bluesky"
run no-release-uv-platforms "$((PORT + 1))" ""
check "the uv path writes --platforms" saved_is no-release-uv-platforms bluesky

ARGS="--platforms bluesky"
run source-uv-platforms "$((PORT + 1))" "" BOT_FARM_SILENCE_TRACKER_SOURCE=git+https://example.invalid/x.git
check "a SOURCE install writes --platforms" saved_is source-uv-platforms bluesky

run no-binary-wheel "$PORT" "$work/riscv:"
check "uses the wheel from the release when there is no program for the system" [ "$code" = 0 ]
check "installs the downloaded wheel with uv" grep -q \
    "tool install --force .*bot_farm_silence_tracker-0.0.1-py3-none-any.whl" "$work/uv.log"

# Every answer that is not a redirect must stop the installer, never fall back to main.
n=2
for mode in 503 429 200-html; do
    : >"$work/uv.log"
    run "latest-$mode" "$((PORT + n))" ""
    check "fails when the latest-release lookup answers $mode" [ "$code" != 0 ]
    check "says the answer was not a redirect ($mode)" grep -q "instead of a redirect" "$out"
    check "does not treat $mode as no release" lacks "no release yet" "$out"
    check "does not install from main with uv after $mode" [ ! -s "$work/uv.log" ]
    check "installs nothing after $mode" [ ! -e "$exe" ]
    n=$((n + 1))
done

# An unpublished copy refuses to run before it downloads anything: no download base
# override is set, and every downloader and tool on PATH logs its calls.
mkdir -p "$work/logbin"
for tool in curl wget gh uv; do
    printf '#!/bin/sh\necho "%s $*" >>"%s"\nexit 1\n' "$tool" "$work/calls.log" >"$work/logbin/$tool"
    chmod 755 "$work/logbin/$tool"
done
for case in plain source; do
    echo "--- unpublished-refused-$case"
    mkdir -p "$work/home-unpublished-$case"
    source_var=""
    [ "$case" = plain ] || source_var="BOT_FARM_SILENCE_TRACKER_SOURCE=git+https://example.invalid/x.git"
    code=0
    # shellcheck disable=SC2086 # $source_var is empty or one VAR=VALUE word
    env -i HOME="$work/home-unpublished-$case" PATH="$work/logbin:/usr/bin:/bin:/usr/sbin:/sbin" \
        SHELL=/bin/sh TMPDIR="$work" BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH=1 $source_var \
        sh "$UNPUBLISHED" --platforms bluesky </dev/null >"$work/out-unpublished-$case.txt" 2>&1 || code=$?
    sed 's/^/    | /' "$work/out-unpublished-$case.txt"
    out="$work/out-unpublished-$case.txt"
    check "an unpublished copy refuses to run ($case)" [ "$code" != 0 ]
    check "it prints the publishing message ($case)" grep -qF "$REFUSAL" "$out"
    check "it looks nothing up ($case)" lacks "Looking up" "$out"
    check "it calls no downloader, gh or uv ($case)" [ ! -e "$work/calls.log" ]
    check "it installs nothing ($case)" [ ! -e "$work/home-unpublished-$case/.local/bin/bot-farm-silence-tracker" ]
done

# A test server in place of GitHub lets an unpublished copy run, but it then has no
# repository to check the attestation against, so it skips that check and says so.
SCRIPT="$UNPUBLISHED"
run unpublished-github-url "$((PORT + 5))" ""
check "an unpublished copy installs from a test server" [ "$code" = 0 ]
check "it does not refuse then" lacks "has not been published yet:" "$out"
check "it warns that the attestation was not checked" grep -q "attestation was not checked" "$out"
check "it does not call gh attestation verify" lacks -s "attestation verify" "$work/gh-unpublished-github-url.log"
check "the program from the test server runs" runs "$exe"

# A release folder override (with no test server) lets it run too, without attestation.
SCRIPT="$UNPUBLISHED"
run unpublished-release-base "$PORT" "" BOT_FARM_SILENCE_TRACKER_GITHUB_URL= \
    BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL="http://127.0.0.1:$PORT/$TEST_REPO/releases/download/v0.0.1-test"
check "an unpublished copy installs from a release folder override" [ "$code" = 0 ]
check "it does not refuse with a release folder" lacks "has not been published yet:" "$out"
check "it warns that the attestation was not checked with a release folder" grep -q "attestation was not checked" "$out"
check "it does not call gh attestation verify with a release folder" lacks -s "attestation verify" "$work/gh-unpublished-release-base.log"
check "the program from the release folder runs" runs "$exe"

# With a terminal, the installer offers the menu. run_in_pty.py gives it one and types
# the answers (each time "Choice" is printed).
# pty_run NAME ANSWER...: run install.sh on a pseudo-terminal.
pty_run() {
    name="$1"
    shift
    echo "--- $name"
    mkdir -p "$work/home-$name"
    code=0
    # shellcheck disable=SC2086 # $ARGS and $PTY_ENV are lists of words
    env -i HOME="$work/home-$name" PATH="$work/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        SHELL=/bin/sh TMPDIR="$work" FAKE_GH_LOG="$work/gh-$name.log" \
        BOT_FARM_SILENCE_TRACKER_GITHUB_URL="http://127.0.0.1:$PORT" BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1 \
        BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH=1 \
        BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET="${BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET:-0}" $PTY_ENV \
        "$PYTHON" "$ROOT/tests/installer/run_in_pty.py" "$@" -- sh "$INSTALLER" $ARGS \
        >"$work/out-$name.txt" 2>&1 || code=$?
    sed 's/^/    | /' "$work/out-$name.txt"
    out="$work/out-$name.txt"
    exe="$work/home-$name/.local/bin/bot-farm-silence-tracker"
    ARGS=""
    PTY_ENV=""
}

pty_run menu-default ""
check "the menu run installs" [ "$code" = 0 ]
check "the menu lists the Reddit archive as the default" grep -q "\[1\] Reddit (Arctic Shift archive, no account needed)" "$out"
check "the menu lists Bluesky" grep -q "\[2\] Bluesky" "$out"
check "the menu lists X" grep -q "\[3\] X (needs your own paid API token)" "$out"
check "the menu lists the Reddit official API" grep -q "\[4\] Reddit official API" "$out"
check "Enter picks reddit-archive" saved_is menu-default reddit-archive
check "Enter keeps platforms saved earlier" grep -q -- "--if-unset" "$work/home-menu-default/config-calls"

pty_run menu-choice "2,1"
check "menu choice 2,1 is written as bluesky,reddit-archive" saved_is menu-choice bluesky,reddit-archive
check "a menu choice replaces earlier ones" lacks -- "--if-unset" "$work/home-menu-choice/config-calls"

pty_run menu-retry "9" "3"
check "a bad menu answer asks again" grep -q "'9' is not one of" "$out"
check "the second answer is used" saved_is menu-retry x

pty_run menu-eof "2^D"
check "a partly typed answer is kept at end of input" saved_is menu-eof bluesky

pty_run menu-credentials "3,4"
check "menu choices 3,4 are written as x,reddit" saved_is menu-credentials x,reddit
check "prints the X variable after a menu choice" grep -q "BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN" "$out"
check "prompts only once, never for a credential" [ "$(grep -c "Choice \[1\]" "$out")" = 1 ]

# Regression test: a terminal whose "onlcr" setting was turned off by something else
# (before the installer started, and again while it waited for the answer) made the
# answer and every later line run diagonally across the screen as one padded line. The
# installer now puts the terminal back in line mode, so every line starts at the left
# edge: each new line is preceded or followed by a carriage return.
PTY_ENV="PTY_CLEAR_ONLCR=1"
pty_run menu-terminal-reset "1,2"
check "the menu run on a terminal without onlcr installs" [ "$code" = 0 ]
check "the answer is used" saved_is menu-terminal-reset reddit-archive,bluesky
check "every line starts at the left edge on a terminal without onlcr" clean_lines "$out"
check "nothing prints a form feed or an escape sequence" lacks "$(printf '[\f\033]')" "$out"

# On a terminal in the normal state, the installer leaves the settings exactly as they were.
echo "--- menu-terminal-unchanged"
mkdir -p "$work/home-tty"
code=0
# shellcheck disable=SC2016 # The inner sh expands $1, $2 and $3.
env -i HOME="$work/home-tty" PATH="$work/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    SHELL=/bin/sh TMPDIR="$work" FAKE_GH_LOG="$work/gh-tty.log" \
    BOT_FARM_SILENCE_TRACKER_GITHUB_URL="http://127.0.0.1:$PORT" BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1 \
    BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH=1 \
    BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET="${BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET:-0}" \
    "$PYTHON" "$ROOT/tests/installer/run_in_pty.py" "2" -- sh -c \
    'stty -g >"$1" && sh "$2" && stty -g >"$3"' sh "$work/tty-before" "$INSTALLER" "$work/tty-after" \
    >"$work/out-tty.txt" 2>&1 || code=$?
check "the menu run installs" [ "$code" = 0 ]
check "the terminal settings are the same after the installer" cmp -s "$work/tty-before" "$work/tty-after"
check "every line starts at the left edge on a normal terminal" clean_lines "$work/out-tty.txt"

PTY_ENV="BOT_FARM_SILENCE_TRACKER_PLATFORMS=bluesky"
pty_run menu-skipped-by-env "should-not-be-typed"
check "the environment variable skips the menu even on a terminal" lacks "Which social platforms" "$out"
check "and its list is written" saved_is menu-skipped-by-env bluesky

ARGS="--platforms reddit-archive"
pty_run menu-skipped-by-flag "should-not-be-typed"
check "--platforms skips the menu even on a terminal" lacks "Which social platforms" "$out"

# BusyBox wget may not validate TLS certificates, so as the only downloader it is
# refused unless the user opts in. A stand-in "wget" that calls itself BusyBox is on a
# PATH that has no curl.
mkdir -p "$work/nocurl"
for tool in sh uname mktemp awk grep sed tr cut head tail cat rm mkdir mv cp chmod ls dirname \
    basename sha256sum shasum od wc sort id date env printf ln; do
    found="$(command -v "$tool" 2>/dev/null || true)"
    case "$found" in /*) ln -sf "$found" "$work/nocurl/$tool" ;; esac
done
printf '#!/bin/sh\necho "BusyBox v1.36.1 (fake) multi-call binary." >&2\nexit 1\n' >"$work/nocurl/wget"
chmod 755 "$work/nocurl/wget"
: >"$work/uv.log"
echo "--- busybox-refused"
code=0
env -i HOME="$work/home-bb" PATH="$work/nocurl" SHELL=/bin/sh TMPDIR="$work" \
    BOT_FARM_SILENCE_TRACKER_GITHUB_URL="http://127.0.0.1:$PORT" BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1 \
    BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH=1 "$work/nocurl/sh" "$INSTALLER" </dev/null \
    >"$work/out-bb.txt" 2>&1 || code=$?
sed 's/^/    | /' "$work/out-bb.txt"
check "refuses BusyBox wget as the only downloader without the opt-in" [ "$code" != 0 ]
check "explains the TLS risk and how to install curl" grep -q "may not validate TLS certificates" "$work/out-bb.txt"
check "mentions the opt-in variable" grep -q "BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET=1" "$work/out-bb.txt"
check "does not reach the network or uv then" [ ! -s "$work/uv.log" ]

if [ "$failures" -gt 0 ]; then
    echo "$failures check(s) failed."
    exit 1
fi
echo "All installer checks passed."
