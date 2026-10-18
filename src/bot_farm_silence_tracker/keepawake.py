"""Keep the computer from going to sleep on its own while a scan runs.

A scan of a few hundred accounts can take hours. When the computer falls asleep in the
middle, the network drops, requests fail, and the scan only moves during the brief
maintenance wake-ups the system schedules. :func:`keep_awake` asks the operating system
not to start an *idle* system sleep while the block runs. The screen may still turn off
and lock; only the sleep of the whole computer is prevented.

* macOS: an IOKit power assertion of type ``PreventUserIdleSystemSleep`` (made with
  ``ctypes``, released with ``IOPMAssertionRelease``). When IOKit cannot be used, the
  system's ``/usr/bin/caffeinate -i -w <pid>`` is started instead; ``-w`` makes it exit
  by itself if this process dies.
* Windows: ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`` on the calling
  thread, reset with ``ES_CONTINUOUS`` on the same thread. The state belongs to the thread,
  so enter and leave the block on the thread that runs the scan.
* Linux: ``systemd-inhibit`` holding a ``sleep`` inhibitor, when it is installed;
  otherwise nothing is done.

Preventing sleep is a request, not a guarantee. Closing the lid of a laptop that is on
battery, choosing Sleep from the menu, a low-battery shutdown and enterprise power
policies still put the computer to sleep, and no program can prevent that.

Every failure is non-fatal: the scan runs as it would without this module, and one
warning line says that sleep prevention is unavailable. Nothing is left behind: the
request is released when the block ends normally, by an exception, by a cancelled scan or
by Ctrl-C.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator

log = logging.getLogger(__name__)

#: The name other programs show for the request (``pmset -g assertions``, the Windows
#: ``powercfg /requests``, ``systemd-inhibit --list``).
REASON = "Bot Farm Silence Tracker scan running"

#: Calling this gives the request back.
Release = Callable[[], None]

# macOS (IOKit and CoreFoundation).
_IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"
_COREFOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_ASSERTION_TYPE = "PreventUserIdleSystemSleep"  # kIOPMAssertionTypePreventUserIdleSystemSleep
_ASSERTION_LEVEL_ON = 255  # kIOPMAssertionLevelOn
_CF_UTF8 = 0x08000100  # kCFStringEncodingUTF8
_CAFFEINATE = "/usr/bin/caffeinate"

# Windows (winbase.h).
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

#: How long a helper process gets to fail at once (say, no session bus) before it counts
#: as running.
_STARTUP_CHECK_SECONDS = 0.3
_STOP_WAIT_SECONDS = 5.0


class Unavailable(Exception):
    """Sleep prevention cannot be set up here; the message says why."""


# -- macOS ---------------------------------------------------------------------------


def _load_macos_libraries() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    """IOKit and CoreFoundation (replaced in tests)."""
    return ctypes.CDLL(_IOKIT_PATH), ctypes.CDLL(_COREFOUNDATION_PATH)


def _acquire_iokit(reason: str) -> Release:
    iokit, core = _load_macos_libraries()
    core.CFStringCreateWithCString.restype = ctypes.c_void_p
    core.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    core.CFRelease.restype = None
    core.CFRelease.argtypes = [ctypes.c_void_p]
    iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32
    iokit.IOPMAssertionCreateWithName.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IOPMAssertionRelease.restype = ctypes.c_int32
    iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]

    kind = core.CFStringCreateWithCString(None, _ASSERTION_TYPE.encode(), _CF_UTF8)
    name = core.CFStringCreateWithCString(None, reason.encode(), _CF_UTF8)
    try:
        if not kind or not name:
            raise Unavailable("CoreFoundation could not make the assertion strings")
        assertion_id = ctypes.c_uint32(0)
        status = iokit.IOPMAssertionCreateWithName(
            kind, _ASSERTION_LEVEL_ON, name, ctypes.byref(assertion_id)
        )
        if status != 0:
            raise Unavailable(f"IOPMAssertionCreateWithName returned {status}")
    finally:
        # IOKit copies what it needs; the strings are ours to give back.
        for string in (kind, name):
            if string:
                core.CFRelease(string)
    held = assertion_id.value

    def release() -> None:
        status = iokit.IOPMAssertionRelease(held)
        if status != 0:
            log.debug("IOPMAssertionRelease returned %s", status)

    return release


def _spawn_holder(argv: list[str]) -> subprocess.Popen[bytes]:
    """Start a helper process that holds the request, and check that it stays up."""
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise Unavailable(f"cannot start {argv[0]}: {exc}") from exc
    try:
        code = process.wait(timeout=_STARTUP_CHECK_SECONDS)
    except subprocess.TimeoutExpired:
        return process  # Still running: it holds the request.
    raise Unavailable(f"{argv[0]} exited at once with status {code}")


def _stop_holder(process: subprocess.Popen[bytes]) -> None:
    """End a helper process and wait for it; never raises."""
    try:
        process.terminate()
        try:
            process.wait(timeout=_STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_STOP_WAIT_SECONDS)
    except Exception as exc:  # Release must never break the scan or hide its error.
        log.debug("could not stop the sleep-prevention helper: %s", exc)


def _acquire_caffeinate(reason: str) -> Release:
    # -i: prevent idle system sleep (the display may still sleep); -w: end with this process.
    process = _spawn_holder([_CAFFEINATE, "-i", "-w", str(os.getpid())])
    return lambda: _stop_holder(process)


def _acquire_macos(reason: str) -> Release:
    try:
        return _acquire_iokit(reason)
    except Exception as exc:  # ctypes or IOKit trouble: use the system tool instead.
        log.debug("IOKit assertion failed (%s); trying caffeinate", exc)
        return _acquire_caffeinate(reason)


# -- Windows -------------------------------------------------------------------------


def _kernel32() -> ctypes.CDLL:
    """kernel32 with ``SetThreadExecutionState`` typed (replaced in tests)."""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
    kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    return kernel32  # type: ignore[no-any-return]


def _acquire_windows(reason: str) -> Release:
    kernel32 = _kernel32()
    if kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED) == 0:
        raise Unavailable("SetThreadExecutionState failed")

    def release() -> None:
        # The state is per thread: this runs on the thread that acquired it.
        kernel32.SetThreadExecutionState(_ES_CONTINUOUS)

    return release


# -- Linux ---------------------------------------------------------------------------

#: Waits for the program to end (checked every few seconds), so an inhibitor never
#: outlives a scan that was killed without the chance to release it.
_WAIT_FOR_PARENT = 'while kill -0 "$1" 2>/dev/null; do sleep 5; done'


def _acquire_linux(reason: str) -> Release:
    inhibit = shutil.which("systemd-inhibit")
    if inhibit is None:
        raise Unavailable("systemd-inhibit is not installed")
    process = _spawn_holder(
        [
            inhibit,
            "--what=sleep",
            "--who=Bot Farm Silence Tracker",
            f"--why={reason}",
            "--mode=block",
            "sh",
            "-c",
            _WAIT_FOR_PARENT,
            "sh",
            str(os.getpid()),
        ]
    )
    return lambda: _stop_holder(process)


# -- The context manager -------------------------------------------------------------


def _acquire(reason: str, platform: str) -> Release:
    if platform == "darwin":
        return _acquire_macos(reason)
    if platform == "win32":
        return _acquire_windows(reason)
    if platform.startswith("linux"):
        return _acquire_linux(reason)
    raise Unavailable(f"no method for {platform}")


@contextlib.contextmanager
def keep_awake(
    enabled: bool = True, *, reason: str = REASON, platform: str | None = None
) -> Iterator[bool]:
    """Keep the computer from idle sleep inside the block; yield whether that is in effect.

    With ``enabled`` false nothing is done and ``False`` is yielded. A failure to set
    the request up is logged as one warning line and ``False`` is yielded; it never
    raises. The request is released on every way out of the block, Ctrl-C included, and
    a failure to release is only logged. ``platform`` (default: ``sys.platform``) is for
    tests.
    """
    release: Release | None = None
    if enabled:
        system = sys.platform if platform is None else platform
        try:
            release = _acquire(reason, system)
        except Exception as exc:
            log.warning(
                "Sleep prevention is unavailable (%s); the computer may sleep during a long scan.",
                exc,
            )
    try:
        yield release is not None
    finally:
        if release is not None:
            try:
                release()
            except Exception as exc:
                log.debug("could not release the sleep request: %s", exc)
