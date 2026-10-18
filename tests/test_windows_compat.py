"""Portability: command quoting, the default database path, file locks and renames."""

from __future__ import annotations

import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_farm_silence_tracker import cli as cli_mod
from bot_farm_silence_tracker import storage as storage_mod
from bot_farm_silence_tracker.cli import join_command
from bot_farm_silence_tracker.storage import fallback_lock

PARTS = [
    "bot-farm-silence-tracker",
    "scan",
    "--db",
    r"C:\Users\A User\c.db",
    "--source-option",
    "a=b",
]


def test_posix_quoting_round_trips_through_shlex() -> None:
    line = join_command(PARTS, windows=False)
    assert shlex.split(line) == PARTS
    assert "'C:\\Users\\A User\\c.db'" in line


def test_windows_quoting_single_quotes_for_powershell_and_keeps_backslashes() -> None:
    line = join_command(PARTS, windows=True)
    assert line == r"bot-farm-silence-tracker scan --db 'C:\Users\A User\c.db' --source-option a=b"


#: Arguments with characters PowerShell (or cmd.exe) would otherwise interpret,
#: paired with the PowerShell quoting each one must get.
POWERSHELL_CASES = [
    (r"C:\Data(1)\c.db", r"'C:\Data(1)\c.db'"),
    (r"C:\R&D\c.db", r"'C:\R&D\c.db'"),
    (r"C:\a;b\c.db", r"'C:\a;b\c.db'"),
    (r"C:\a{b}\c.db", r"'C:\a{b}\c.db'"),
    ("x=$home", "'x=$home'"),
    ("O'Brien", "'O''Brien'"),
    ("%TEMP%", "'%TEMP%'"),
    (r"C:\Users\A User\c.db", r"'C:\Users\A User\c.db'"),
    ("O\u2019Brien", "'O\u2019\u2019Brien'"),
    ("", "''"),
]


@pytest.mark.parametrize(("arg", "quoted"), POWERSHELL_CASES)
def test_windows_quoting_protects_powershell_metacharacters(arg: str, quoted: str) -> None:
    assert join_command(["x", "--db", arg], windows=True) == f"x --db {quoted}"


_ARGV_ECHO = "import json, sys; sys.stdout.write(json.dumps(sys.argv[1:]))"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell (pwsh) is not installed")
def test_windows_quoting_round_trips_through_powershell(tmp_path: Path) -> None:
    helper = tmp_path / "argv_echo.py"
    helper.write_text(_ARGV_ECHO, encoding="utf-8")
    args = [arg for arg, _ in POWERSHELL_CASES] + ["--source-option", "a=b", "v1.5,x:y@z/w"]
    # The call operator runs the quoted program path; the rest is join_command's output.
    line = "& " + join_command([sys.executable, str(helper), *args], windows=True)
    result = subprocess.run(
        [str(shutil.which("pwsh")), "-NoProfile", "-NonInteractive", "-Command", line],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert json.loads(result.stdout.decode("utf-8")) == args


def test_windows_quoting_leaves_a_plain_path_bare() -> None:
    line = join_command(["x", "--db", r"C:\Temp\c.db"], windows=True)
    assert line == r"x --db C:\Temp\c.db"


def test_default_quoting_follows_the_platform() -> None:
    expected = join_command(PARTS, windows=os.name == "nt")
    assert join_command(PARTS) == expected


def _set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Point the home directory at ``home``; returns the POSIX-style database path there."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home / ".cache" / "bot-farm-silence-tracker" / "bot-farm-silence-tracker.db"


def test_default_db_path_on_posix_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", "/should/not/be/used")
    expected = Path("~/.cache/bot-farm-silence-tracker/bot-farm-silence-tracker.db").expanduser()
    assert storage_mod._default_db_path(windows=False) == expected


def test_default_db_path_on_windows_uses_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    expected = tmp_path / "bot-farm-silence-tracker" / "bot-farm-silence-tracker.db"
    assert storage_mod._default_db_path(windows=True) == expected


def test_default_db_path_on_windows_without_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = _set_home(monkeypatch, tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert storage_mod._default_db_path(windows=True) == expected


def test_fallback_lock_excludes_a_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "c.db.spend-fallback.jsonl"
    entered = threading.Event()

    def contend() -> None:
        with fallback_lock(path):
            entered.set()

    with fallback_lock(path):
        worker = threading.Thread(target=contend)
        worker.start()
        time.sleep(0.2)
        assert not entered.is_set()
    worker.join(5)
    assert entered.is_set()


def _windows_error(winerror: int, message: str) -> PermissionError:
    """A :class:`PermissionError` carrying a Windows error code, on any platform."""
    exc = PermissionError(message)
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize("winerror", [5, 32])
def test_sharing_violation_is_retried_on_windows(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, winerror: int
) -> None:
    monkeypatch.setattr(storage_mod, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage_mod, "_WINDOWS_RETRY_FIRST_WAIT", 0.0)
    calls: list[int] = []

    def flaky(value: int) -> int:
        calls.append(value)
        if len(calls) < 3:
            raise _windows_error(winerror, "in use by another process")
        return value * 2

    with caplog.at_level(logging.DEBUG, logger="bot_farm_silence_tracker.storage"):
        assert storage_mod._retry_on_sharing_violation(flaky, 21) == 42
    assert len(calls) == 3
    retries = [r for r in caplog.records if "retrying" in r.getMessage()]
    assert len(retries) == 2
    assert "flaky" in retries[0].getMessage()


@pytest.mark.parametrize("winerror", [None, 1224])
def test_other_permission_errors_are_not_retried_on_windows(
    monkeypatch: pytest.MonkeyPatch, winerror: int | None
) -> None:
    monkeypatch.setattr(storage_mod, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage_mod, "_WINDOWS_RETRY_FIRST_WAIT", 0.0)
    calls: list[None] = []

    def denied() -> None:
        calls.append(None)
        if winerror is None:
            raise PermissionError("no code")
        raise _windows_error(winerror, "mapped section is open")

    with pytest.raises(PermissionError):
        storage_mod._retry_on_sharing_violation(denied)
    assert len(calls) == 1


def test_sharing_violation_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_mod, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage_mod, "_WINDOWS_RETRY_FIRST_WAIT", 0.0)
    calls: list[None] = []

    def always() -> None:
        calls.append(None)
        raise _windows_error(32, "still in use")

    with pytest.raises(PermissionError):
        storage_mod._retry_on_sharing_violation(always)
    assert len(calls) == storage_mod._WINDOWS_RETRY_ATTEMPTS


def test_permission_errors_are_not_retried_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_mod, "_IS_WINDOWS", False)
    calls: list[None] = []

    def denied() -> None:
        calls.append(None)
        raise _windows_error(32, "read-only")

    with pytest.raises(PermissionError):
        storage_mod._retry_on_sharing_violation(denied)
    assert len(calls) == 1


class _FakeMsvcrt:
    """Records ``msvcrt.locking`` calls in place of the Windows module."""

    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, fail_unlock: bool = False) -> None:
        self.calls: list[tuple[int, int]] = []
        self.fail_unlock = fail_unlock

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes))
        if mode == self.LK_UNLCK and self.fail_unlock:
            raise OSError("not locked")


@pytest.mark.parametrize("fail_unlock", [False, True])
def test_fallback_lock_unlocks_explicitly_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_unlock: bool
) -> None:
    fake = _FakeMsvcrt(fail_unlock=fail_unlock)
    monkeypatch.setattr(storage_mod, "_fcntl", None)
    monkeypatch.setattr(storage_mod, "_msvcrt", fake)
    with fallback_lock(tmp_path / "c.db.spend-fallback.jsonl"):
        assert fake.calls == [(fake.LK_NBLCK, 1)]
    assert fake.calls == [(fake.LK_NBLCK, 1), (fake.LK_UNLCK, 1)]


def _cp932_stream() -> io.TextIOWrapper:
    """A strict cp932 stream, like redirected output on a Japanese Windows system."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict")


def test_tolerate_unencodable_output_replaces_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, err = _cp932_stream(), _cp932_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(cli_mod, "os", SimpleNamespace(name="nt"))
    cli_mod._tolerate_unencodable_output()
    assert out.errors == err.errors == "replace"
    out.write("a \u2014 b")  # cp932 has no em dash; this must not raise.


def test_tolerate_unencodable_output_leaves_posix_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _cp932_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(cli_mod, "os", SimpleNamespace(name="posix"))
    cli_mod._tolerate_unencodable_output()
    assert out.errors == "strict"


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_entry_point_makes_help_and_version_encoding_safe(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    out, err = _cp932_stream(), _cp932_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "argv", ["bot-farm-silence-tracker", flag])
    monkeypatch.setattr(cli_mod, "os", SimpleNamespace(name="nt"))
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.run()
    # ``--version`` without a command currently ends in a usage error (exit 2) on
    # stderr rather than exit 0; either way, writing it must not raise
    # UnicodeEncodeError, and the box-drawn error panel has characters cp932 lacks.
    assert exc_info.value.code in (0, 2)
    assert out.errors == err.errors == "replace"
    out.flush()
    err.flush()
    written = out.buffer.getvalue() + err.buffer.getvalue()  # type: ignore[attr-defined]
    assert written
