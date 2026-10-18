"""Shared fixtures."""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Any

import pytest

#: The tests' own outage events (see tests/fixtures/events.toml and helpers.py).
FIXTURE_EVENTS = Path(__file__).parent / "fixtures" / "events.toml"


@pytest.fixture(scope="session", autouse=True)
def _session_environment(tmp_path_factory: pytest.TempPathFactory) -> object:
    """The same clean environment for fixtures of a wider scope (module fixtures run
    before the function-scoped ones below): no exported credentials, the tests' own
    events file, and a temporary cache folder and config file."""
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name.startswith("BOT_FARM_SILENCE_TRACKER_"):
                patch.delenv(name, raising=False)
        patch.setenv("BOT_FARM_SILENCE_TRACKER_EVENTS", str(FIXTURE_EVENTS))
        root = tmp_path_factory.mktemp("session")
        patch.setenv("BOT_FARM_SILENCE_TRACKER_CACHE_DIR", str(root / "cache"))
        patch.setenv("BOT_FARM_SILENCE_TRACKER_CONFIG", str(root / "config.toml"))
        yield


@pytest.fixture(autouse=True)
def _no_real_credentials(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep tests independent of whatever credentials the developer has exported.

    Then use the tests' own events file, and point the user config file and every
    cache database (the per-event ones included) at temporary paths, so a test
    never reads or writes the developer's own files. They are set here, after the
    clean-up, in one fixture: pytest does not promise an order between autouse
    fixtures of the same scope, and a clean-up that ran later would remove them.
    """
    for name in list(os.environ):
        if name.startswith("BOT_FARM_SILENCE_TRACKER_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_FARM_SILENCE_TRACKER_EVENTS", str(FIXTURE_EVENTS))
    monkeypatch.setenv("BOT_FARM_SILENCE_TRACKER_CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.setenv(
        "BOT_FARM_SILENCE_TRACKER_CONFIG", str(tmp_path_factory.mktemp("config") / "config.toml")
    )


@pytest.fixture(autouse=True)
def _run_in_tmp_path(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run every test in a fresh temporary directory, so output files written to the
    working directory (such as the default ``bot-farm-silence-tracker-run-<id>.csv``) never land in
    the repository."""
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def _no_real_sleep_assertions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan run by a test must not create a real power assertion (or a helper process)
    on the developer's computer. tests/test_keepawake.py tests the module itself with
    mocks, and tests/test_keepawake_manual.py is the one opt-in test with a real one."""
    import contextlib

    from bot_farm_silence_tracker import cli

    @contextlib.contextmanager
    def no_assertion(enabled: bool = True, **_kwargs: object):  # type: ignore[no-untyped-def]
        yield False

    monkeypatch.setattr(cli, "keep_awake", no_assertion)


class RealNetworkBlocked(RuntimeError):
    """A unit test tried to open a connection to a host other than this computer."""


def _is_local(host: object) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> object:
    """Fail any test that connects to a real host: only loopback addresses (the fake
    servers some tests start) and Unix sockets are allowed.

    The connection is refused with :class:`RealNetworkBlocked`, which is not an
    ``OSError``, so the code under test cannot mistake it for a network failure and
    carry on; in case it swallows it anyway, the test is failed at teardown too.
    """
    attempts: list[Any] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(address[0]):
            attempts.append(address)
            raise RealNetworkBlocked(f"a unit test tried to connect to {address!r}")

    def connect(self: socket.socket, address: Any) -> None:
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield attempts
    if attempts:
        pytest.fail(f"real network connections were attempted: {attempts!r}")


@pytest.fixture(autouse=True)
def _no_live_profile_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """``registry add`` and ``registry evidence`` check live profiles. In tests that check
    never reaches the network: its client cannot be built, so a Bluesky profile comes
    back "not checked". Tests of the check itself pass their own mocked client."""
    from bot_farm_silence_tracker import cli
    from bot_farm_silence_tracker.registry.live import LiveChecker

    def refuse(**_kwargs: object) -> object:
        raise OSError("tests make no live profile request")

    monkeypatch.setattr(cli, "_live_checker", lambda: LiveChecker(client_factory=refuse))


@pytest.fixture
def frozen_cli_x_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """X sources the CLI builds see ``NOW``, not the real wall clock.

    X recent search reaches back from the real time on purpose, and ``--now``
    cannot pull archived days back into reach, so a CLI test that discovers
    accounts against the fixture cutoff (2026-10-10) starts finding none once
    the real clock passes a week after it, and the suite would depend on the
    date it runs on. Only sources the CLI builds (``cli.load_source_class``) are
    wrapped: a source a test constructs itself keeps control of its own clock,
    and the spend ledger keeps the real clock, so a test's own
    ``storage.add_spend`` records stay on the day the ledger reads.
    """
    from bot_farm_silence_tracker import cli
    from bot_farm_silence_tracker.sources.base import SourceConfig
    from bot_farm_silence_tracker.sources.x import XSource

    from .helpers import NOW

    real_load_source_class = cli.load_source_class

    class NowXSource(XSource):
        """``XSource`` with the tests' moment as its wall clock."""

        def __init__(self, config: SourceConfig, **kwargs: Any) -> None:
            super().__init__(config, clock=lambda: NOW.timestamp(), **kwargs)

    def frozen_load_source_class(platform: str) -> Any:
        cls = real_load_source_class(platform)
        return NowXSource if cls is XSource else cls

    monkeypatch.setattr(cli, "load_source_class", frozen_load_source_class)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the tests marked ``slow`` unless RUN_SLOW_TESTS=1 is set."""
    if os.environ.get("RUN_SLOW_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="slow; set RUN_SLOW_TESTS=1 to run it")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
