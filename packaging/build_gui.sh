#!/bin/sh
# Build the desktop app with PyInstaller. Run from the repository root, on macOS or
# Windows (Git Bash). Usage: packaging/build_gui.sh [OUTPUT_NAME]
#
# macOS:   dist-bin/Bot Farm Silence Tracker.app, a windowed app bundle whose
#          executable opens the desktop app. Zip it with
#          ditto -c -k --keepParent "dist-bin/Bot Farm Silence Tracker.app" OUT.app.zip
# Windows: dist-bin/<OUTPUT_NAME>.exe (default bot-farm-silence-tracker-gui), a
#          single-file GUI-subsystem executable (no console window) that opens the app.
#
# Both run "--selftest" to build the window, render every screen with offline sample
# data and exit 0. The command-line binary is built separately by build_binary.sh.
# Like build_binary.sh this uses a uv-managed CPython 3.12 and the exact versions in
# uv.lock, with the "gui" extra (CustomTkinter).
set -eu

NAME="${1:-bot-farm-silence-tracker-gui}"
APP_NAME="Bot Farm Silence Tracker"
PYINSTALLER_VERSION="6.22.3"

UV_PYTHON=3.12
UV_PYTHON_PREFERENCE=only-managed
export UV_PYTHON UV_PYTHON_PREFERENCE

case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*)
        SEP=";"
        ROOT="$(pwd -W)"
        set -- --onefile --name "$NAME"
        ;;
    Darwin)
        SEP=":"
        ROOT="$(pwd)"
        set -- --onedir --name "$APP_NAME" \
            --osx-bundle-identifier "io.github.obviousthrowaway.bot-farm-silence-tracker"
        ;;
    *)
        echo "build_gui.sh builds the macOS app bundle or the Windows executable" >&2
        exit 1
        ;;
esac

uv run --locked --no-dev --extra gui --with "pyinstaller==${PYINSTALLER_VERSION}" pyinstaller \
    "$@" \
    --windowed \
    --clean \
    --noconfirm \
    --distpath dist-bin \
    --workpath build-gui \
    --specpath build-gui \
    --paths src \
    --collect-submodules bot_farm_silence_tracker \
    --collect-data certifi \
    --collect-data customtkinter \
    --add-data "${ROOT}/config/seeds.yaml${SEP}bot_farm_silence_tracker/_data" \
    --add-data "${ROOT}/events.toml${SEP}bot_farm_silence_tracker/_data" \
    packaging/gui_entry.py
