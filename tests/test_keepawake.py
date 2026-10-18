"""keep_awake on each operating system's path, with ctypes and subprocess mocked.

No test here creates a real power assertion or starts a real helper process.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker import cli, keepawake
from bot_farm_silence_tracker import config as user_config

from .test_fake_and_cli import scan_args


def fake_iokit(status: int = 0) -> tuple[MagicMock, MagicMock]:
    iokit, core = MagicMock(name="IOKit"), MagicMock(name="CoreFoundation")
    iokit.IOPMAssertionCreateWithName.return_value = status
    iokit.IOPMAssertionRelease.return_value = 0
    core.CFStringCreateWithCString.side_effect = [1111, 2222]
    return iokit, core


def running_process() -> MagicMock:
    process = MagicMock(name="Popen instance")
    process.wait.side_effect = [subprocess.TimeoutExpired("helper", 0.3), 0]
    return process


# -- macOS ---------------------------------------------------------------------------


def test_macos_makes_and_releases_an_iokit_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    iokit, core = fake_iokit()
    monkeypatch.setattr(keepawake, "_load_macos_libraries", lambda: (iokit, core))
    popen = MagicMock()
    monkeypatch.setattr(keepawake.subprocess, "Popen", popen)
    with keepawake.keep_awake(platform="darwin") as active:
        assert active is True
        iokit.IOPMAssertionCreateWithName.assert_called_once()
        iokit.IOPMAssertionRelease.assert_not_called()
        kind, level, name, _ = iokit.IOPMAssertionCreateWithName.call_args.args
        assert (kind, level, name) == (1111, 255, 2222)
    created = [call.args[1] for call in core.CFStringCreateWithCString.call_args_list]
    assert created == [b"PreventUserIdleSystemSleep", keepawake.REASON.encode()]
    assert keepawake.REASON == "Bot Farm Silence Tracker scan running"
    iokit.IOPMAssertionRelease.assert_called_once()
    assert core.CFRelease.call_count == 2  # The strings are given back.
    popen.assert_not_called()


@pytest.mark.parametrize("failure", ["status", "load"])
def test_macos_falls_back_to_caffeinate(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    if failure == "status":
        iokit, core = fake_iokit(status=-536870212)
        monkeypatch.setattr(keepawake, "_load_macos_libraries", lambda: (iokit, core))
    else:

        def broken() -> Any:
            raise OSError("no IOKit")

        monkeypatch.setattr(keepawake, "_load_macos_libraries", broken)
    process = running_process()
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(keepawake.subprocess, "Popen", popen)
    with keepawake.keep_awake(platform="darwin") as active:
        assert active is True
        argv = popen.call_args.args[0]
        assert argv[:3] == ["/usr/bin/caffeinate", "-i", "-w"]
        assert argv[3].isdigit()
        process.terminate.assert_not_called()
    process.terminate.assert_called_once()


def test_macos_with_nothing_available_warns_once_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> Any:
        raise OSError("no IOKit")

    monkeypatch.setattr(keepawake, "_load_macos_libraries", broken)
    monkeypatch.setattr(keepawake.subprocess, "Popen", MagicMock(side_effect=OSError("nope")))
    with (
        caplog.at_level(logging.DEBUG, logger="bot_farm_silence_tracker"),
        keepawake.keep_awake(platform="darwin") as active,
    ):
        assert active is False
        ran = True
    assert ran
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Sleep prevention is unavailable" in warnings[0].getMessage()


# -- Windows -------------------------------------------------------------------------


def test_windows_sets_and_resets_the_thread_execution_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = MagicMock()
    kernel32.SetThreadExecutionState.return_value = 0x80000000
    monkeypatch.setattr(keepawake, "_kernel32", lambda: kernel32)
    with keepawake.keep_awake(platform="win32") as active:
        assert active is True
        kernel32.SetThreadExecutionState.assert_called_once_with(0x80000000 | 0x00000001)
    assert [c.args[0] for c in kernel32.SetThreadExecutionState.call_args_list] == [
        0x80000001,
        0x80000000,  # ES_CONTINUOUS alone clears the request.
    ]


def test_windows_failure_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    kernel32 = MagicMock()
    kernel32.SetThreadExecutionState.return_value = 0
    monkeypatch.setattr(keepawake, "_kernel32", lambda: kernel32)
    with (
        caplog.at_level(logging.WARNING, logger="bot_farm_silence_tracker"),
        keepawake.keep_awake(platform="win32") as active,
    ):
        assert active is False
    kernel32.SetThreadExecutionState.assert_called_once()  # Nothing to reset.
    assert "Sleep prevention is unavailable" in caplog.text


# -- Linux ---------------------------------------------------------------------------


def test_linux_holds_a_systemd_inhibitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: f"/usr/bin/{name}")
    process = running_process()
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(keepawake.subprocess, "Popen", popen)
    with keepawake.keep_awake(platform="linux") as active:
        assert active is True
        argv = popen.call_args.args[0]
        assert argv[0] == "/usr/bin/systemd-inhibit"
        assert "--what=sleep" in argv
        assert f"--why={keepawake.REASON}" in argv
    process.terminate.assert_called_once()


def test_linux_without_systemd_inhibit_does_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: None)
    popen = MagicMock()
    monkeypatch.setattr(keepawake.subprocess, "Popen", popen)
    with (
        caplog.at_level(logging.WARNING, logger="bot_farm_silence_tracker"),
        keepawake.keep_awake(platform="linux") as active,
    ):
        assert active is False
    popen.assert_not_called()
    assert caplog.text.count("Sleep prevention is unavailable") == 1


def test_a_helper_that_exits_at_once_counts_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: f"/usr/bin/{name}")
    process = MagicMock()
    process.wait.return_value = 1  # For example: no session bus.
    monkeypatch.setattr(keepawake.subprocess, "Popen", MagicMock(return_value=process))
    with keepawake.keep_awake(platform="linux") as active:
        assert active is False


# -- Release and opting out ----------------------------------------------------------


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
def test_the_request_is_released_when_the_block_raises(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    iokit, core = fake_iokit()
    monkeypatch.setattr(keepawake, "_load_macos_libraries", lambda: (iokit, core))
    with pytest.raises(type(error)), keepawake.keep_awake(platform="darwin"):
        raise error
    iokit.IOPMAssertionRelease.assert_called_once()


def test_a_cancelled_scan_releases_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot_farm_silence_tracker.sources.errors import FetchCancelled

    iokit, core = fake_iokit()
    monkeypatch.setattr(keepawake, "_load_macos_libraries", lambda: (iokit, core))
    with pytest.raises(FetchCancelled), keepawake.keep_awake(platform="darwin"):
        raise FetchCancelled("the scan is stopping")
    iokit.IOPMAssertionRelease.assert_called_once()


def test_a_failing_release_does_not_hide_the_scan_error(monkeypatch: pytest.MonkeyPatch) -> None:
    iokit, core = fake_iokit()
    iokit.IOPMAssertionRelease.side_effect = OSError("gone")
    monkeypatch.setattr(keepawake, "_load_macos_libraries", lambda: (iokit, core))
    with pytest.raises(RuntimeError, match="scan failed"), keepawake.keep_awake(platform="darwin"):
        raise RuntimeError("scan failed")


def test_a_helper_that_ignores_terminate_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: f"/usr/bin/{name}")
    process = MagicMock()
    process.wait.side_effect = [
        subprocess.TimeoutExpired("h", 0.3),
        subprocess.TimeoutExpired("h", 5),
        0,
    ]
    monkeypatch.setattr(keepawake.subprocess, "Popen", MagicMock(return_value=process))
    with keepawake.keep_awake(platform="linux"):
        pass
    process.kill.assert_called_once()


def test_disabled_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    acquire = MagicMock()
    monkeypatch.setattr(keepawake, "_acquire", acquire)
    with keepawake.keep_awake(False) as active:
        assert active is False
    acquire.assert_not_called()


def test_an_unknown_platform_is_unavailable_not_an_error() -> None:
    with keepawake.keep_awake(platform="plan9") as active:
        assert active is False


# -- The command line and the config -------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "expected"), [([], True), (["--allow-sleep"], False)]
)  # fmt: skip
def test_scan_keeps_awake_unless_allow_sleep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: list[str], expected: bool
) -> None:
    import contextlib

    seen: list[bool] = []

    @contextlib.contextmanager
    def record(enabled: bool = True, **_kwargs: object):  # type: ignore[no-untyped-def]
        seen.append(enabled)
        yield enabled

    monkeypatch.setattr(cli, "keep_awake", record)
    result = CliRunner().invoke(cli.app, scan_args(tmp_path, "--n", "20", *extra))
    assert result.exit_code == 0, result.output
    assert seen == [expected]


def test_the_keep_awake_choice_is_saved_in_the_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert user_config.load_config(path).keep_awake_enabled is True  # On by default.
    user_config.set_platforms(path, ("reddit-archive", "bluesky"))
    user_config.set_keep_awake(path, False)
    loaded = user_config.load_config(path)
    assert loaded.keep_awake is False and loaded.keep_awake_enabled is False
    assert loaded.enabled == ("reddit-archive", "bluesky")
    # Other changes keep it.
    user_config.set_default(path, "bluesky")
    assert user_config.load_config(path).keep_awake is False
    user_config.set_keep_awake(path, True)
    assert user_config.load_config(path).keep_awake is True


def test_a_keep_awake_that_is_not_a_boolean_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('keep_awake = "yes"\n', encoding="utf-8")
    with pytest.raises(user_config.ConfigError, match="keep_awake"):
        user_config.load_config(path)
