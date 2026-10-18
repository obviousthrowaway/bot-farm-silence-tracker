#!/bin/sh
# Make a stand-in "Bot Farm Silence Tracker.app" (zipped) from the command-line program,
# so the installer package can be built and tested in pull requests, without the real
# desktop app.
#
#   packaging/macos/make_placeholder_app.sh CLI_BINARY OUT_ZIP
#
# The stand-in app only runs the command-line program. The installers workflow builds it
# only outside a release, under a ".standin" file name, and a release downloads the real
# desktop app instead and fails if that is missing; the stand-in is never published.
set -eu
[ "$#" -eq 2 ] || { echo "usage: make_placeholder_app.sh CLI_BINARY OUT_ZIP" >&2; exit 2; }
work="$(mktemp -d "${TMPDIR:-/tmp}/bfst-app.XXXXXX")"
trap 'rm -rf "$work"' EXIT
app="$work/Bot Farm Silence Tracker.app"
mkdir -p "$app/Contents/MacOS"
cp "$1" "$app/Contents/MacOS/bot-farm-silence-tracker"
chmod 755 "$app/Contents/MacOS/bot-farm-silence-tracker"
cat >"$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>bot-farm-silence-tracker</string>
    <key>CFBundleIdentifier</key><string>bot-farm-silence-tracker.placeholder</string>
    <key>CFBundleName</key><string>Bot Farm Silence Tracker</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.0.0</string>
    <key>CFBundleVersion</key><string>0.0.0</string>
</dict>
</plist>
PLIST
mkdir -p "$(dirname "$2")"
rm -f "$2"
ditto -c -k --keepParent "$app" "$2"
echo "Built placeholder $2"
