#!/bin/sh
# Smoke-test a standalone executable built by packaging/build_binary.sh.
# Usage: packaging/smoke_test.sh PATH_TO_BINARY
#
# Checks that it starts, that a fake scan writes output, that HTTPS works from inside
# the frozen program (CA bundle present, TLS handshake succeeds), and on macOS that the
# minimum macOS version it declares is not newer than MACOS_MAX_MINOS (default 12.0).
# Set SMOKE_SKIP_NETWORK=1 to skip the HTTPS check when there is no network.
set -eu

bin="${1:?usage: smoke_test.sh PATH_TO_BINARY}"
work="$(mktemp -d "${TMPDIR:-/tmp}/bfst-smoke.XXXXXX")"
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM

echo "==> --version"
"$bin" --version
echo "==> --help"
"$bin" --help >/dev/null
echo "==> bundled events"
"$bin" events list
echo "==> fake scan"
"$bin" scan --platform fake --n 50 --seed 1 --db "$work/smoke.db" --out "$work/smoke.csv"
test -s "$work/smoke.csv"

if [ "${SMOKE_SKIP_NETWORK:-0}" = "1" ]; then
    echo "==> HTTPS check skipped (SMOKE_SKIP_NETWORK=1)"
else
    echo "==> HTTPS check"
    "$bin" selftest-tls
fi

if [ "$(uname -s)" = "Darwin" ]; then
    max="${MACOS_MAX_MINOS:-12.0}"
    minos="$(otool -l "$bin" | awk '/LC_BUILD_VERSION/ { found = 1 } found && $1 == "minos" { print $2; exit }')"
    if [ -z "$minos" ]; then
        # Older binaries carry LC_VERSION_MIN_MACOSX with a "version" field instead.
        minos="$(otool -l "$bin" | awk '/LC_VERSION_MIN_MACOSX/ { found = 1 } found && $1 == "version" { print $2; exit }')"
    fi
    echo "==> macOS minimum version: ${minos:-unknown} (must be <= $max)"
    [ -n "$minos" ] || { echo "error: could not read the minimum macOS version" >&2; exit 1; }
    newest="$(printf '%s\n%s\n' "$minos" "$max" | sort -t . -k 1,1n -k 2,2n -k 3,3n | tail -n 1)"
    if [ "$newest" != "$max" ] && [ "$minos" != "$max" ]; then
        echo "error: the binary needs macOS $minos, newer than the allowed $max" >&2
        exit 1
    fi
fi
echo "==> smoke test passed"
