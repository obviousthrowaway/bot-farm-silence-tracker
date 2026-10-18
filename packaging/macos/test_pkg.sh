#!/bin/sh
# Install the macOS installer package for the current user, check the result, then
# uninstall it. Used by CI on a throwaway runner.
#
#   packaging/macos/test_pkg.sh PATH_TO_PKG
#
# This really installs into your home folder, so it only runs when CI=true.
set -eu

pkg="${1:?usage: test_pkg.sh PATH_TO_PKG}"
if [ "${CI:-}" != "true" ]; then
    echo "error: this installs into your home folder; it only runs in CI (CI=true)." >&2
    exit 2
fi

TOOL_NAME="bot-farm-silence-tracker"
bin="$HOME/.local/bin/$TOOL_NAME"
uninstaller="$HOME/.local/bin/$TOOL_NAME-uninstall"
app="$HOME/Applications/Bot Farm Silence Tracker.app"
config="$HOME/.config/$TOOL_NAME/config.toml"
work="$(mktemp -d "${TMPDIR:-/tmp}/bfst-pkgtest.XXXXXX")"
trap 'rm -rf "$work"' EXIT

fail() {
    echo "error: $*" >&2
    echo "---- last lines of /var/log/install.log ----" >&2
    tail -n 60 /var/log/install.log >&2 || :
    exit 1
}

echo "==> choices offered on the Customize step"
installer -showChoicesXML -pkg "$pkg" -target CurrentUserHomeDirectory >"$work/choices.xml"
for id in program reddit-archive bluesky x reddit finish; do
    grep -q "<string>$id</string>" "$work/choices.xml" || fail "choice $id is missing"
done

echo "==> install with the default choices (no administrator rights)"
[ "$(id -u)" != "0" ] || fail "run this as a normal user, not root"
installer -pkg "$pkg" -target CurrentUserHomeDirectory || fail "installer failed"
"$bin" --version || fail "$bin does not run"
[ -d "$app" ] || fail "$app is missing"
[ -x "$uninstaller" ] || fail "$uninstaller is missing"
[ "$(stat -f %Su "$bin")" = "$(id -un)" ] || fail "$bin is not owned by $(id -un)"
"$bin" config show | tee "$work/show.txt"
grep -qx 'platforms: reddit-archive' "$work/show.txt" || fail "the default platform was not saved"
line="export PATH=\"$HOME/.local/bin:\$PATH\""
found=""
for profile in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    if grep -qxF "$line" "$profile" 2>/dev/null; then found="$profile"; fi
done
[ -n "$found" ] || fail "no shell startup file adds ~/.local/bin to PATH"
echo "PATH line in $found"
[ ! -e "$HOME/Library/Caches/$TOOL_NAME-installer/platforms" ] || fail "the choices file was left behind"

echo "==> upgrade in place with Bluesky and X ticked as well"
cat >"$work/changes.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array>
    <dict>
        <key>attributeSetting</key><integer>1</integer>
        <key>choiceAttribute</key><string>selected</string>
        <key>choiceIdentifier</key><string>bluesky</string>
    </dict>
    <dict>
        <key>attributeSetting</key><integer>1</integer>
        <key>choiceAttribute</key><string>selected</string>
        <key>choiceIdentifier</key><string>x</string>
    </dict>
</array>
</plist>
XML
installer -pkg "$pkg" -applyChoiceChangesXML "$work/changes.xml" -target CurrentUserHomeDirectory ||
    fail "installer failed on the upgrade"
"$bin" config show | tee "$work/show.txt"
grep -qx 'platforms: reddit-archive, bluesky, x' "$work/show.txt" || fail "the ticked platforms were not saved"
[ "$(grep -cxF "$line" "$found")" = "1" ] || fail "the PATH line was added twice"

echo "==> reinstall with only the default ticked keeps the saved platforms"
installer -pkg "$pkg" -target CurrentUserHomeDirectory || fail "installer failed on the reinstall"
"$bin" config show | grep -qx 'platforms: reddit-archive, bluesky, x' ||
    fail "a reinstall with the default choices overwrote the saved platforms"

echo "==> receipts"
pkgutil --volume "$HOME" --pkgs 2>/dev/null | grep "$TOOL_NAME" || echo "(no receipts listed for the home folder)"

echo "==> uninstall (keeps settings)"
"$uninstaller" --yes
[ ! -e "$bin" ] || fail "$bin is still there"
[ ! -e "$uninstaller" ] || fail "$uninstaller is still there"
[ ! -e "$app" ] || fail "$app is still there"
[ -f "$config" ] || fail "the uninstaller deleted the settings without --purge"
if pkgutil --volume "$HOME" --pkgs 2>/dev/null | grep -q "^$TOOL_NAME\.app$"; then
    fail "the receipt is still there"
fi
echo "==> macOS installer package test passed"
