"""Run a CLI command in this process and stream its output as lines.

The desktop app runs scans through the very same ``scan`` command the terminal uses
(the same checks, engine, pacer and host allowlist), on a worker thread. Its console
output is turned into lines for the app's log. No Tk here.
"""

from __future__ import annotations

import contextlib
import io
import threading
from collections.abc import Callable, Sequence

from typer.exceptions import TyperException

from bot_farm_silence_tracker.cli import ScanHooks, app, scan_hooks

#: Only one in-process command at a time: standard output is redirected for the
#: whole process while it runs.
_LOCK = threading.Lock()


class LineWriter(io.TextIOBase):
    """A text stream that hands every complete line to ``sink``."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__()
        self._sink = sink
        self._buffer = ""
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        with self._lock:
            self._buffer += text
            *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            self._sink(line.rstrip("\r"))
        return len(text)

    def flush(self) -> None:
        with self._lock:
            rest, self._buffer = self._buffer, ""
        if rest:
            self._sink(rest)


def run_cli(
    argv: Sequence[str], sink: Callable[[str], None], hooks: ScanHooks | None = None
) -> int:
    """Run ``bot-farm-silence-tracker <argv>`` here; return its exit code.

    Output (standard output and standard error) goes to ``sink`` line by line.
    Exceptions the command does not handle itself (an interrupted scan raises
    :class:`~bot_farm_silence_tracker.sources.errors.FetchCancelled`) propagate.
    """
    writer = LineWriter(sink)
    with (
        _LOCK,
        contextlib.redirect_stdout(writer),
        contextlib.redirect_stderr(writer),
        scan_hooks(hooks or ScanHooks()),
    ):
        try:
            # Not standalone: an exit code is returned instead of raising SystemExit.
            result = app(list(argv), prog_name="bot-farm-silence-tracker", standalone_mode=False)
        except TyperException as exc:  # A usage error, such as a bad option value.
            format_message = getattr(exc, "format_message", None)
            message = format_message() if callable(format_message) else str(exc)
            writer.write(f"error: {message}\n")
            result = getattr(exc, "exit_code", 2)
        finally:
            writer.flush()
    return int(result or 0)
