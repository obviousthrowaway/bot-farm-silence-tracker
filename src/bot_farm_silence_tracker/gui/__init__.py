"""The desktop app: ``bot-farm-silence-tracker gui``.

Nothing here imports Tk at module level, so the command-line program never loads it.
:mod:`.presenter` holds the logic and has no Tk imports; :mod:`.view` is the thin
CustomTkinter layer, imported only by :func:`launch`.
"""

from __future__ import annotations

import contextlib
import os
import sys

#: Shown when the "gui" extra (CustomTkinter) is not installed.
MISSING_EXTRA = (
    "The desktop app needs CustomTkinter, which is not installed. Install the program "
    'with its "gui" extra, for example:\n'
    "    uv tool install 'bot-farm-silence-tracker[gui]'\n"
    "    pip install 'bot-farm-silence-tracker[gui]'\n"
    "or download a standalone build, which always includes it. Every feature is also "
    "available on the command line: run `bot-farm-silence-tracker --help`."
)
#: Shown when Python was built without Tk.
MISSING_TK = (
    "The desktop app needs Tk (the tkinter module), which this Python does not have. "
    "Install a Python that includes Tk (python.org and uv-managed Pythons do; on "
    "Debian or Ubuntu install python3-tk), or use the command line: run "
    "`bot-farm-silence-tracker --help`."
)
#: The oldest Tk the app supports. CustomTkinter needs 8.6; the app is checked with
#: 8.6 and 9.0 (one window per process, see :class:`.view.App`).
MIN_TK = 8.6
OLD_TK = (
    "The desktop app needs Tk 8.6 or newer, but this Python has Tk {version}. Install a "
    "newer Python, or use the command line: run `bot-farm-silence-tracker --help`."
)
#: Shown when no display is available (a server or an SSH session).
NO_DISPLAY = (
    "The desktop app could not open a window ({detail}). It needs a graphical "
    "desktop; on a server or over SSH use the command line instead: run "
    "`bot-farm-silence-tracker --help`."
)


def _ensure_streams() -> None:
    """A windowed build has no console, so ``sys.stdout`` and ``sys.stderr`` are
    ``None``; give them a sink so nothing fails when it prints."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def _say(message: str) -> None:
    with contextlib.suppress(Exception):  # A stream that cannot be written.
        print(message, file=sys.stderr)


def launch(*, selftest: bool = False) -> int:
    """Open the app (or run its self-test). Returns the exit code."""
    _ensure_streams()
    try:
        import tkinter
    except ImportError:
        _say(MISSING_TK)
        return 1
    if tkinter.TkVersion < MIN_TK:
        _say(OLD_TK.format(version=tkinter.TkVersion))
        return 1
    try:
        import customtkinter  # noqa: F401
    except ImportError:
        _say(MISSING_EXTRA)
        return 1
    from bot_farm_silence_tracker.gui import view

    try:
        return view.run_app(selftest=selftest)
    except tkinter.TclError as exc:
        _say(NO_DISPLAY.format(detail=str(exc).strip() or "no display"))
        return 1
    except RuntimeError as exc:  # A second window in one process (see view.App).
        _say(str(exc))
        return 1
