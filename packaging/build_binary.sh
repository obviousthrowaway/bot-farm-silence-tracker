#!/bin/sh
# Build a single-file executable with PyInstaller. Run from the repository root.
# Usage: packaging/build_binary.sh [OUTPUT_NAME]
# The result is dist-bin/<OUTPUT_NAME> (default: bot-farm-silence-tracker).
#
# The build uses a uv-managed CPython 3.12 (never the system Python, whose macOS
# deployment target and OpenSSL vary by machine) and the exact dependency versions in
# uv.lock. PyInstaller itself is pinned here because it is a build tool, not a
# dependency of the package. The binary includes the "gui" extra, so
# `bot-farm-silence-tracker gui` works; Tk is only loaded by that command.
set -eu

NAME="${1:-bot-farm-silence-tracker}"
PYINSTALLER_VERSION="6.22.3"

UV_PYTHON=3.12
UV_PYTHON_PREFERENCE=only-managed
export UV_PYTHON UV_PYTHON_PREFERENCE

# PyInstaller separates --add-data source and destination with ":" on Unix and ";" on Windows.
# Python on Windows needs a Windows-style path, which "pwd -W" gives in Git Bash.
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*)
        SEP=";"
        ROOT="$(pwd -W)"
        ;;
    *)
        SEP=":"
        ROOT="$(pwd)"
        ;;
esac

uv run --locked --no-dev --extra gui --with "pyinstaller==${PYINSTALLER_VERSION}" pyinstaller \
    --onefile \
    --clean \
    --noconfirm \
    --name "$NAME" \
    --distpath dist-bin \
    --workpath build-bin \
    --specpath build-bin \
    --paths src \
    --collect-submodules bot_farm_silence_tracker \
    --collect-data certifi \
    --collect-data customtkinter \
    --add-data "${ROOT}/config/seeds.yaml${SEP}bot_farm_silence_tracker/_data" \
    --add-data "${ROOT}/events.toml${SEP}bot_farm_silence_tracker/_data" \
    packaging/entry.py
