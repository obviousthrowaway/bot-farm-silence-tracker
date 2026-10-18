#!/bin/sh
# Build the macOS installer package (.pkg) for one architecture.
#
#   packaging/macos/build_pkg.sh VERSION ARCH CLI_BINARY APP_ZIP OUT_DIR
#
# VERSION is the release version without the "v" (for example 0.1.0), ARCH is arm64 or
# x64, CLI_BINARY is the release's standalone command-line program for that ARCH, and
# APP_ZIP is the zipped "Bot Farm Silence Tracker.app". The result is
# OUT_DIR/bot-farm-silence-tracker-VERSION-macos-ARCH.pkg.
#
# The package is built with pkgbuild and productbuild and is not signed. It holds:
#   app.pkg       the app, installed to ~/Applications
#   cli.pkg       the command-line program and the uninstall script, to ~/.local/bin;
#                 its scripts check that the program runs and add ~/.local/bin to PATH
#   platform-*.pkg  one payload-free package per platform checkbox on the Customize step
#   finish.pkg    a payload-free package that saves the ticked platforms through the
#                 program's "config set-platforms" command
set -eu

[ "$#" -eq 5 ] || {
    echo "usage: build_pkg.sh VERSION ARCH CLI_BINARY APP_ZIP OUT_DIR" >&2
    exit 2
}
version="$1"
arch="$2"
cli="$3"
app_zip="$4"
out_dir="$5"

case "$version" in
    "" | *[!0-9A-Za-z.+-]*) echo "error: bad version: $version" >&2; exit 2 ;;
esac
case "$arch" in
    arm64) host_arch="arm64" ;;
    x64) host_arch="x86_64" ;;
    *) echo "error: ARCH must be arm64 or x64, not $arch" >&2; exit 2 ;;
esac
[ -f "$cli" ] || { echo "error: no such file: $cli" >&2; exit 1; }
[ -f "$app_zip" ] || { echo "error: no such file: $app_zip" >&2; exit 1; }

here="$(cd "$(dirname "$0")" && pwd)"
TOOL_NAME="bot-farm-silence-tracker"
APP_NAME="Bot Farm Silence Tracker.app"
PLATFORMS="reddit-archive bluesky x reddit"

work="$(mktemp -d "${TMPDIR:-/tmp}/bfst-pkg.XXXXXX")"
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM
mkdir -p "$work/pkgs" "$out_dir"

# The app. Each payload root is the folder it installs into, so the folder's own
# permissions (755) are the usual ones for ~/Applications and ~/.local/bin.
mkdir -p "$work/app-root"
ditto -x -k "$app_zip" "$work/app-root"
[ -d "$work/app-root/$APP_NAME" ] || {
    echo "error: $app_zip does not contain $APP_NAME at its top level" >&2
    exit 1
}
xattr -cr "$work/app-root"
chmod 755 "$work/app-root"
# Install the app where the package says, even when a copy with the same bundle
# identifier exists elsewhere (Installer would otherwise "relocate" the update there).
pkgbuild --analyze --root "$work/app-root" "$work/app.plist" >/dev/null
i=0
while plutil -extract "$i" xml1 -o - "$work/app.plist" >/dev/null 2>&1; do
    plutil -replace "$i.BundleIsRelocatable" -bool NO "$work/app.plist"
    i=$((i + 1))
done
pkgbuild --quiet --root "$work/app-root" --component-plist "$work/app.plist" \
    --identifier "$TOOL_NAME.app" --version "$version" \
    --install-location /Applications "$work/pkgs/app.pkg"

# The command-line program and the uninstall script.
mkdir -p "$work/cli-root" "$work/cli-scripts"
cp "$cli" "$work/cli-root/$TOOL_NAME"
cp "$here/uninstall.sh" "$work/cli-root/$TOOL_NAME-uninstall"
chmod 755 "$work/cli-root" "$work/cli-root/$TOOL_NAME" "$work/cli-root/$TOOL_NAME-uninstall"
xattr -cr "$work/cli-root"
cp "$here/scripts/common.sh" "$work/cli-scripts/common.sh"
cp "$here/scripts/cli-preinstall" "$work/cli-scripts/preinstall"
cp "$here/scripts/cli-postinstall" "$work/cli-scripts/postinstall"
pkgbuild --quiet --root "$work/cli-root" --scripts "$work/cli-scripts" \
    --identifier "$TOOL_NAME.cli" --version "$version" \
    --install-location /.local/bin "$work/pkgs/cli.pkg"

# One payload-free package per platform checkbox.
for platform in $PLATFORMS; do
    dir="$work/platform-$platform-scripts"
    mkdir -p "$dir"
    cp "$here/scripts/common.sh" "$dir/common.sh"
    cp "$here/scripts/platform-postinstall" "$dir/postinstall"
    printf '%s\n' "$platform" >"$dir/platform"
    pkgbuild --quiet --nopayload --scripts "$dir" \
        --identifier "$TOOL_NAME.platform.$platform" --version "$version" \
        "$work/pkgs/platform-$platform.pkg"
done

mkdir -p "$work/finish-scripts"
cp "$here/scripts/common.sh" "$work/finish-scripts/common.sh"
cp "$here/scripts/finish-postinstall" "$work/finish-scripts/postinstall"
pkgbuild --quiet --nopayload --scripts "$work/finish-scripts" \
    --identifier "$TOOL_NAME.finish" --version "$version" "$work/pkgs/finish.pkg"

sed -e "s/@VERSION@/$version/g" -e "s/@HOST_ARCH@/$host_arch/g" \
    "$here/distribution.xml.in" >"$work/distribution.xml"
out="$out_dir/$TOOL_NAME-$version-macos-$arch.pkg"
productbuild --quiet --distribution "$work/distribution.xml" \
    --resources "$here/resources" --package-path "$work/pkgs" "$out"
echo "Built $out"
