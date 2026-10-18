"""PyInstaller entry point of the desktop app (the macOS app bundle and the Windows
GUI executable). It opens the window directly; ``--selftest`` builds the window,
renders every screen with offline sample data and exits (used by CI)."""

import sys

from bot_farm_silence_tracker.gui import launch

if __name__ == "__main__":
    sys.exit(launch(selftest="--selftest" in sys.argv[1:]))
