"""A network that drops (a computer waking from sleep) is not an account error."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot_farm_silence_tracker.gui import presenter as P
from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.pipeline import (
    MAX_NETWORK_FAILURES,
    NETWORK_RETRY_PAUSES,
    run_scan,
)
from bot_farm_silence_tracker.sources.base import SourceConfig, SourceError
from bot_farm_silence_tracker.sources.errors import TransportFailure, is_transport_failure
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF, NOW
from .test_http import client
from .test_pipeline import SEEDS, ScriptedSource, settings


def source_with_sleeps(
    storage: Storage, fail: dict[int, Exception]
) -> tuple[ScriptedSource, list[float]]:
    sleeps: list[float] = []
    config = SourceConfig(
        http=HttpClient(sleep=sleeps.append, unrestricted=True),
        credentials={},
        now=NOW,
        cutoff=CUTOFF,
        options={"population": 400},
        ledger=SpendLedger(storage, "fake"),
    )
    return ScriptedSource(config, fail=fail), sleeps


def down() -> TransportFailure:
    return TransportFailure("transport error for https://example.test: All connections failed")


# -- the HTTP client -----------------------------------------------------------------


def test_the_client_retries_transport_errors_with_its_backoff_then_raises_transport_failure() -> (
    None
):
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("network is unreachable", request=request)

    http, sleeps = client(handler, max_retries=3, backoff_base=1.0)
    with pytest.raises(TransportFailure) as raised:
        http.get("https://example.test")
    assert len(attempts) == 4  # The first attempt and the three retries.
    assert sleeps == [1.0, 2.0, 4.0]
    assert http.last_response_at is None  # No response ever came in.
    assert isinstance(raised.value, SourceError)  # Existing handlers still catch it.


def test_the_client_recovers_when_the_network_returns_during_its_retries() -> None:
    calls = iter([True, True, False])

    def handler(request: httpx.Request) -> httpx.Response:
        if next(calls):
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(200, json={})

    http, sleeps = client(handler)
    assert http.get("https://example.test").status_code == 200
    assert sleeps == [1.0, 2.0]
    assert http.last_response_at is not None


def test_a_transport_failure_is_recognised_through_a_wrapping_error() -> None:
    try:
        try:
            raise down()
        except TransportFailure as exc:
            raise SourceError("could not read the archive") from exc
    except SourceError as wrapped:
        assert is_transport_failure(wrapped)
    assert not is_transport_failure(SourceError("HTTP 400"))
    assert not is_transport_failure(None)


# -- the scan ------------------------------------------------------------------------


def test_a_transport_failure_is_tried_again_after_a_pause(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src, sleeps = source_with_sleeps(store, {2: down()})
        outcome = run_scan(src, SEEDS, store, settings())
        assert outcome.status == "complete"
        assert outcome.summary["errors"] == 0
        assert sleeps == [NETWORK_RETRY_PAUSES[0]]
        # The failed account was fetched again, not skipped.
        assert src.fetches[2] == src.fetches[3]


def test_failures_that_outlast_the_pauses_do_not_count_toward_the_error_limit(
    tmp_path: Path,
) -> None:
    attempts = len(NETWORK_RETRY_PAUSES) + 1
    # Accounts 2 and 4 each fail on every one of their tries, with a limit of one error.
    fail = {call: down() for call in range(2, 2 + attempts)}
    fail.update({call: down() for call in range(2 + attempts + 2, 2 + 2 * attempts + 2)})
    with Storage(tmp_path / "c.db") as store:
        src, sleeps = source_with_sleeps(store, fail)
        outcome = run_scan(src, SEEDS, store, settings(max_consecutive_errors=1))
        assert outcome.status == "complete"  # Not "Aborted after 1 consecutive account errors".
        assert not any("Aborted after" in w for w in outcome.warnings)
        assert outcome.summary["errors"] == 2
        assert sleeps == list(NETWORK_RETRY_PAUSES) * 2
        statuses = [s for _, s in store.get_run_sample(outcome.run_id)]
        assert statuses.count("error") == 2  # Both are left for --from-run.


def test_other_errors_still_count_and_a_transport_failure_does_not_reset_them(
    tmp_path: Path,
) -> None:
    attempts = len(NETWORK_RETRY_PAUSES) + 1
    fail: dict[int, Exception] = {1: SourceError("bad"), 3: SourceError("bad")}
    # Account 2 (calls 2..) is the network failing, between two ordinary errors.
    fail = {1: SourceError("bad")}
    fail.update({call: down() for call in range(2, 2 + attempts)})
    fail[2 + attempts] = SourceError("bad")
    with Storage(tmp_path / "c.db") as store:
        src, _ = source_with_sleeps(store, fail)
        outcome = run_scan(src, SEEDS, store, settings(max_consecutive_errors=2))
        assert outcome.status == "partial"
        assert any("Aborted after 2 consecutive account errors" in w for w in outcome.warnings)


def test_a_network_that_stays_down_stops_the_scan_with_a_resume_hint(tmp_path: Path) -> None:
    fail = {call: down() for call in range(1, 1000)}
    with Storage(tmp_path / "c.db") as store:
        src, _ = source_with_sleeps(store, fail)
        outcome = run_scan(src, SEEDS, store, settings(max_consecutive_errors=50))
        assert outcome.status == "partial"
        warning = next(w for w in outcome.warnings if "network did not answer" in w)
        assert f"--from-run {outcome.run_id}" in warning
        assert f"Stopped after {MAX_NETWORK_FAILURES} accounts" in warning
        assert not any("Aborted after" in w for w in outcome.warnings)


# -- the stall hint ------------------------------------------------------------------

T0 = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("idle", "expected"),
    [
        (timedelta(seconds=30), None),
        (timedelta(minutes=2), None),  # Exactly two minutes is not "more than".
        (timedelta(minutes=2, seconds=1), "No progress for 2 minutes"),
        (timedelta(minutes=9, seconds=40), "No progress for 9 minutes"),
        (timedelta(minutes=185), "No progress for 3 hours"),
    ],
)
def test_stall_hint_appears_after_two_minutes(idle: timedelta, expected: str | None) -> None:
    hint = P.stall_hint(T0, T0 + idle)
    if expected is None:
        assert hint is None
    else:
        assert hint == (
            f"{expected} — the archive may be slow, or this computer may have been asleep."
        )


def test_no_stall_hint_without_a_start_time() -> None:
    assert P.stall_hint(None, T0) is None


def test_the_last_request_is_shown_in_local_time() -> None:
    text = P.last_request_text(T0)
    assert text == f"Last request completed at {T0.astimezone():%H:%M:%S}."
    assert P.last_request_text(None) == "No request has completed yet."


def test_a_job_measures_the_stall_from_its_last_request_or_its_start(tmp_path: Path) -> None:
    job = P.ScanJob(["scan"], db=tmp_path / "c.db")
    assert job.stall_hint(T0) is None  # Not running.

    class Alive:
        def is_alive(self) -> bool:
            return True

    job._thread = Alive()  # type: ignore[assignment]
    job.started_at = T0
    assert job.stall_hint(T0 + timedelta(minutes=3)) == (
        "No progress for 3 minutes — the archive may be slow, or this computer may "
        "have been asleep."
    )
    http = HttpClient(unrestricted=True)
    job._on_client(http)
    http.last_response_at = (T0 + timedelta(minutes=2, seconds=30)).timestamp()
    assert job.last_request_at == T0 + timedelta(minutes=2, seconds=30)
    assert job.stall_hint(T0 + timedelta(minutes=3)) is None  # A request came in meanwhile.
    assert job.stall_hint(T0 + timedelta(minutes=9)) is not None


# -- the app's keep-awake choice -----------------------------------------------------


def test_the_app_passes_allow_sleep_only_when_the_box_is_unchecked(tmp_path: Path) -> None:
    on, problems = P.validate_form(P.ScanForm(platform="fake", n="50"))
    assert on is not None, problems
    assert on.keep_awake and "--allow-sleep" not in P.scan_argv(on, out=tmp_path / "o.csv")
    off, problems = P.validate_form(P.ScanForm(platform="fake", n="50", keep_awake=False))
    assert off is not None, problems
    assert "--allow-sleep" in P.scan_argv(off, out=tmp_path / "o.csv")
    assert "--allow-sleep" not in P.resume_argv("fake", 3, out=tmp_path / "o.csv")
    assert "--allow-sleep" in P.resume_argv("fake", 3, out=tmp_path / "o.csv", keep_awake=False)


def test_the_app_remembers_the_checkbox(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert P.load_keep_awake(path) is True
    P.save_keep_awake(False, path)
    assert P.load_keep_awake(path) is False
    P.save_keep_awake(True, path)
    assert P.load_keep_awake(path) is True
    path.write_text("this is not toml [", encoding="utf-8")
    assert P.load_keep_awake(path) is True  # An unreadable file means the default.


def _unused(*_a: Any) -> None:  # pragma: no cover
    pass
