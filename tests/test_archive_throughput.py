"""Throughput changes of the reddit-archive adapter: rate control, page limits, the
default pool factor, the ``--active-within-days`` discovery window and per-request
instrumentation. All against the scripted fake archive (no network)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.pipeline import format_request_summary
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import SourceConfig, SourceError
from bot_farm_silence_tracker.sources.http import HttpClient, TokenBucket
from bot_farm_silence_tracker.sources.reddit_archive import (
    MIN_RPS,
    RedditArchiveSource,
)
from bot_farm_silence_tracker.storage import Storage
from bot_farm_silence_tracker.timeutil import isoformat

from .helpers import CUTOFF, NOW, MidRandom
from .test_reddit_archive import (
    LOAD,
    Archive,
    build_world,
    comment,
    fetch,
    make_source,
    post_pulse,
    pulse,
    runner,
    slow,
)

DAY = timedelta(days=1)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def build(
    archive: Archive,
    *,
    active_within_days: float | None = None,
    clock: Clock | None = None,
    now: datetime = NOW,
    baseline_days: int = 2,
    wall: Clock | None = None,
    **options: object,
) -> tuple[RedditArchiveSource, list[float], list[float]]:
    options.setdefault("coverage_threshold", "0")
    adapter_sleeps: list[float] = []
    http_sleeps: list[float] = []
    http = HttpClient(
        requests_per_second=RedditArchiveSource.default_rps,
        burst=RedditArchiveSource.default_burst,
        transport=httpx.MockTransport(archive),
        sleep=http_sleeps.append,
        clock=clock or (lambda: 0.0),
    )
    config = SourceConfig(
        http=http,
        credentials={},
        now=now,
        cutoff=CUTOFF,
        seed=1,
        options=options,
        baseline_start=CUTOFF - timedelta(days=baseline_days),
        active_within_days=active_within_days,
    )
    source = RedditArchiveSource(
        config, sleep=adapter_sleeps.append, rng=MidRandom(), wall_clock=wall
    )
    return source, adapter_sleeps, http_sleeps


# -- rate control --------------------------------------------------------------------


def test_timeout_422_does_not_lower_the_rate_but_still_runs_the_ladder() -> None:
    archive = Archive([pulse()], script=[slow(), slow(), slow()])
    src, sleeps, _ = make_source(archive, slowdown_wait=3)
    base = src.http.rate[0]
    fetch(src)
    assert src.http.rate[0] == base
    assert sleeps  # Backed off (and split) as before.
    assert src.request_summary()["backoff_wait_s"] == sum(sleeps)  # type: ignore[index]
    assert src.request_summary()["rate_halvings"] == 0  # type: ignore[index]


def test_load_422_halves_once_per_query_and_floors_at_min_rps() -> None:
    archive = Archive([pulse()], script=[slow(LOAD)] * 2 * 8)
    src, _, _ = make_source(archive, slowdown_wait=0, max_slowdown_retries=1)
    rates = []
    for i in range(8):  # Each account: posts 422, retry 422, the query fails.
        with pytest.raises(Exception):  # noqa: B017 - SlowQueryError or RateLimited.
            fetch(src, f"u{i}")
        rates.append(src.http.rate[0])
    assert rates[0] == 0.25
    assert rates[1] == 0.125
    assert min(rates) == MIN_RPS  # Never below the floor.


def test_429_halves_the_rate_and_waits_for_the_reset_header() -> None:
    limited = httpx.Response(429, headers={"X-RateLimit-Reset": "7"})
    archive = Archive([pulse()], script=[limited])
    src, _, http_sleeps = make_source(archive)
    base = src.http.rate[0]
    fetch(src)
    assert src.http.rate[0] == base / 2
    assert 10.5 in http_sleeps  # 7 s reset plus the 1 s margin and 2.5 s of jitter.


def test_503_is_a_load_signal_but_500_is_not() -> None:
    archive = Archive([pulse()], script=[httpx.Response(503)])
    src, _, _ = make_source(archive)
    fetch(src)
    assert src.http.rate[0] == 0.25
    archive = Archive([pulse()], script=[httpx.Response(500)])
    src, _, _ = make_source(archive)
    fetch(src)
    assert src.http.rate[0] == 0.5


def test_retry_after_on_a_422_is_honored() -> None:
    timed_out = httpx.Response(422, json={"error": "Timeout"}, headers={"Retry-After": "33"})
    archive = Archive([pulse()], script=[timed_out])
    src, sleeps, _ = make_source(archive, slowdown_wait=1)
    fetch(src)
    assert sleeps == [33.0]  # Retry-After wins over the 1 s backoff.


def test_recovery_is_additive_and_stops_at_max_rps() -> None:
    archive = Archive([pulse()], script=[slow(LOAD)])
    src, _, _ = make_source(archive, slowdown_wait=0, max_rps="0.8", recovery_streak="2")
    assert src.http.rate[0] == 0.8  # The scan starts at max_rps.
    fetch(src)  # Load 422, retry ok, comments ok: 0.4 then +0.1 after 2 successes.
    assert src.http.rate[0] == pytest.approx(0.5)
    for i in range(10):
        fetch(src, f"u{i}")
    assert src.http.rate[0] == pytest.approx(0.8)  # Climbs back, never past max_rps.


def test_default_never_exceeds_half_a_request_per_second() -> None:
    src, _, _ = make_source(Archive([pulse()]))
    for i in range(30):
        fetch(src, f"u{i}")
    assert src.max_rps == 0.5
    assert src.http.rate[0] <= 0.5


@pytest.mark.parametrize(
    "options",
    [{"max_rps": "5"}, {"max_rps": "0.05"}, {"recovery_step": "0"}, {"recovery_streak": "0"}],
)
def test_rate_options_are_validated(options: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        make_source(Archive([]), **options)


def test_lower_max_rps_starts_lower() -> None:
    src, _, _ = make_source(Archive([]), max_rps="0.2")
    assert src.http.rate[0] == 0.2


def test_adjusting_the_rate_never_grants_a_fresh_burst() -> None:
    clock = Clock()
    sleeps: list[float] = []
    bucket = TokenBucket(0.5, 1.0, clock=clock, sleep=sleeps.append)
    bucket.acquire()  # Uses the initial token.
    bucket.set_rate(0.25)
    bucket.acquire()
    assert sleeps == [4.0]  # A replaced bucket would have started full (no wait).


def test_time_at_floor_is_tracked() -> None:
    clock = Clock()
    archive = Archive([pulse()], script=[slow(LOAD)] * 2 * 3)
    src, _, _ = build(archive, clock=clock, slowdown_wait="0", recovery_streak="1")
    for i in range(3):
        with pytest.raises(Exception):  # noqa: B017
            fetch(src, f"u{i}")
    assert src.http.rate[0] == MIN_RPS
    clock.now = 100.0
    fetch(src, "ok")  # Two successes: leaves the floor after the first.
    clock.now = 150.0
    stats = src.request_summary()
    assert stats is not None
    assert stats["time_at_floor_s"] == 100.0
    assert stats["elapsed_s"] == 150.0


# -- author history queries ----------------------------------------------------------


def test_history_starts_at_limit_100_and_keeps_the_text_fields() -> None:
    archive = Archive([pulse(), comment("1", "alice", "a", CUTOFF - DAY)])
    src, _, _ = make_source(archive)
    fetch(src)
    params = archive.author_params()
    assert {p["limit"] for p in params} == {"100"}
    # The text is hashed for the content-duplication signal, so it is still requested.
    assert {p["fields"] for p in params} == {
        "id,created_utc,subreddit,title,selftext",
        "id,created_utc,subreddit,body",
    }


@pytest.mark.parametrize("value", ["0", "101", "many"])
def test_history_limit_is_validated(value: str) -> None:
    with pytest.raises(ValueError):
        make_source(Archive([]), history_limit=value)


# -- instrumentation -----------------------------------------------------------------


def test_every_attempt_is_recorded() -> None:
    archive = Archive([pulse(), comment("1", "alice", "a", CUTOFF - DAY)], script=[slow()])
    src, _, _ = make_source(archive, slowdown_wait=0)
    fetch(src)
    rows = src.drain_request_log()
    assert len(rows) == len(archive.requests) == 3
    first = rows[0]
    assert first["status"] == 422 and first["slow_kind"] == "timeout"
    assert first["phase"] == "fetch" and first["endpoint"] == "posts"
    assert first["page_limit"] == "100" and first["rps"] == 0.5
    assert first["window_s"] == int((NOW - (CUTOFF - timedelta(days=30))).total_seconds()) + 1
    assert first["latency_ms"] is not None and first["latency_ms"] >= 0
    assert [r["status"] for r in rows[1:]] == [200, 200]
    assert rows[2]["endpoint"] == "comments"
    assert src.drain_request_log() == []  # Drained.
    stats = src.request_summary()
    assert stats is not None
    assert (stats["requests"], stats["status_422"], stats["status_422_timeout"]) == (3, 1, 1)
    assert stats["mean_latency_ms"] is not None


def test_probe_and_discovery_rows_are_tagged_with_their_phase() -> None:
    archive = Archive([pulse(), post_pulse(), comment("1", "alice", "worldnews", CUTOFF - DAY)])
    src, _, _ = make_source(archive)
    src.data_horizon()
    list(src.discover(SeedSet("reddit-archive", ("worldnews",)), 1))
    phases = {r["phase"] for r in src.drain_request_log()}
    assert phases == {"probe", "discovery"}


def test_storage_appends_request_rows_in_order(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        assert storage.save_request_log(1, "reddit-archive", [{"status": 200}]) == 1
        storage.save_request_log(1, "reddit-archive", [{"status": 422, "slow_kind": "load"}])
        rows = storage.get_request_log(1)
        assert [(r["seq"], r["status"], r["slow_kind"]) for r in rows] == [
            (0, 200, None),
            (1, 422, "load"),
        ]
        assert storage.get_request_log(2) == []


def test_an_older_database_gains_the_request_log_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    sqlite3.connect(path).execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)").close()
    with Storage(path) as storage:
        storage.save_request_log(3, "reddit-archive", [{"status": 200}])
        assert len(storage.get_request_log(3)) == 1


def test_request_summary_line() -> None:
    line = format_request_summary(
        {
            "requests": 40,
            "status_422": 3,
            "status_422_timeout": 2,
            "status_422_load": 1,
            "load_responses": 0,
            "mean_latency_ms": 812.4,
            "observed_rps": 0.42,
            "elapsed_s": 100.0,
            "wall_elapsed_s": 130.0,
            "time_at_floor_s": 25.0,
            "backoff_wait_s": 61.0,
            "reset_wait_s": 7.0,
            "status_422_escalated": 1,
        }
    )
    assert "Requests: 40" in line
    assert "HTTP 422: 3 (2 timeout, of which 1 escalated to load; 1 load)" in line
    assert "812 ms" in line and "0.420 rps over 130 s wall clock (100 s monotonic)" in line
    assert "25 s (25%)" in line
    assert "422 backoff waits 61 s" in line and "X-RateLimit-Remaining 7 s" in line


# -- discovery lookback --------------------------------------------------------------


def daily_world(sub: str = "europe", days: int = 90) -> list[dict[str, Any]]:
    """One distinct author per day at noon, going back ``days`` days from NOW."""
    start = NOW - timedelta(days=days)
    return [
        comment(f"d{i}", f"day_{i}", sub, start + i * DAY + timedelta(hours=12))
        for i in range(days)
    ]


def author_day(name: str, days: int = 90) -> datetime:
    return NOW - timedelta(days=days) + int(name.removeprefix("day_")) * DAY + timedelta(hours=12)


def test_default_discovery_window_is_the_baseline() -> None:
    src, _, _ = build(Archive([]))
    assert src.discovery_window() == (CUTOFF - timedelta(days=2), CUTOFF)


def horizon_archive(newest: datetime, *extra: dict[str, Any]) -> Archive:
    archive = Archive([comment("p", "someone", "worldnews", newest), *extra])
    archive.items.append({**archive.items[0], "kind": "posts", "id": "pp", "title": "t"})
    return archive


def test_lookback_window_is_clipped_to_the_cutoff_and_the_baseline() -> None:
    # Partial overlap: the last 60 days reach past the cutoff and before the baseline.
    archive = Archive([pulse(), *daily_world()])
    src, _, _ = build(archive, active_within_days=60, baseline_days=30)
    assert src.discovery_window() == (CUTOFF - timedelta(days=30), CUTOFF)
    list(src.discover(SeedSet("reddit-archive", ("europe",)), 1))
    warning = " ".join(src.drain_warnings())
    assert "--active-within-days 60 asked for" in warning
    start, end = isoformat(CUTOFF - timedelta(days=30)), isoformat(CUTOFF)
    assert f"discovery uses {start} to {end}" in warning


def test_lookback_window_end_only_clipped_to_the_cutoff() -> None:
    src, _, _ = build(Archive([]), active_within_days=10, baseline_days=30)
    assert src.discovery_window() == (NOW - timedelta(days=10), CUTOFF)


def test_lookback_window_inside_the_baseline_is_not_clipped() -> None:
    # The archive ends a day before the cutoff, so the last 10 days lie inside the baseline.
    newest = CUTOFF - DAY
    src, _, _ = build(horizon_archive(newest), active_within_days=10, baseline_days=30)
    assert src.discovery_window() == (NOW - timedelta(days=10), CUTOFF)  # Before any probe.
    src.data_horizon()
    assert src.discovery_window() == (newest - timedelta(days=10), newest)
    src.drain_warnings()
    list(src.discover(SeedSet("reddit-archive", ("europe",)), 1))
    assert not any("asked for" in w for w in src.drain_warnings())


def test_lookback_window_wholly_after_the_cutoff_is_refused() -> None:
    archive = Archive([pulse(), *daily_world()])
    src, _, _ = build(archive, active_within_days=3, baseline_days=30)  # NOW is cutoff + 4.
    with pytest.raises(SourceError, match="does not overlap the baseline window"):
        src.discovery_window()
    with pytest.raises(SourceError, match="can never be a cliff"):
        list(src.discover(SeedSet("reddit-archive", ("europe",)), 5))
    assert archive.requests == []  # Refused before any discovery request.


def test_lookback_discovery_stays_inside_the_window() -> None:
    archive = Archive([pulse(), *daily_world()])
    archive.items.append({**pulse(), "kind": "posts", "id": "pp", "title": "t"})
    src, _, _ = build(archive, active_within_days=20, baseline_days=30, dry_pages="50")
    src.data_horizon()
    start, end = src.discovery_window()
    # The archive horizon is the pulse, an hour before NOW.
    assert (start, end) == (NOW - timedelta(hours=1, days=20), CUTOFF)
    archive.requests.clear()
    found = list(src.discover(SeedSet("reddit-archive", ("europe",)), 40))
    assert found
    for account in found:
        assert start <= author_day(account.handle) < end < NOW
    for request in archive.requests:
        q = request.url.params
        assert int(q["after"]) >= int(start.timestamp()) - 1
        assert int(q["before"]) <= int(end.timestamp())
        assert int(q["before"]) - int(q["after"]) <= 86400 + 1  # One-day pages.


# -- CLI: pool factor default and --active-within-days ---------------------------------


def patch_client(monkeypatch: pytest.MonkeyPatch, archive: Archive) -> None:
    def patched(**kwargs: Any) -> HttpClient:
        return HttpClient(
            **kwargs,
            transport=httpx.MockTransport(archive),
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", patched)


def scan(db: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--subreddit", "worldnews", "--n", "5",
            "--seed", "3", "--now", "2026-10-14T00:00:00Z", "--db", str(db),
            "--max-requests", "500", *extra,
        ],
    )  # fmt: skip


def stored_params(db: Path, run_id: int) -> dict[str, Any]:
    with Storage(db) as storage:
        run = storage.get_run(run_id)
    assert run is not None
    return dict(run["params"])


def test_cli_reddit_archive_pool_factor_defaults_to_1_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    db = tmp_path / "c.db"
    result = scan(db)
    assert result.exit_code == 0, result.output
    assert stored_params(db, 1)["pool_factor"] == 1.2
    with Storage(db) as storage:
        assert storage.get_run(1)["summary"]["pool_limit"] == 6  # type: ignore[index]


def test_cli_explicit_pool_factor_overrides_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    db = tmp_path / "c.db"
    result = scan(db, "--pool-factor", "2")
    assert result.exit_code == 0, result.output
    assert stored_params(db, 1)["pool_factor"] == 2.0


def test_cli_resume_keeps_the_stored_pool_factor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    db = tmp_path / "c.db"
    assert scan(db, "--pool-factor", "1.5").exit_code == 0
    again = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--from-run", "1",
            "--now", "2026-10-14T00:00:00Z", "--db", str(db),
        ],
    )  # fmt: skip
    assert again.exit_code == 0, again.output
    assert stored_params(db, 2)["pool_factor"] == 1.5


def test_cli_scan_stores_request_rows_and_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(build_world())
    patch_client(monkeypatch, archive)
    db = tmp_path / "c.db"
    result = scan(db, "--active-within-days", "60")
    assert result.exit_code == 0, result.output
    assert stored_params(db, 1)["active_within_days"] == 60
    with Storage(db) as storage:
        rows = storage.get_request_log(1)
        summary = storage.get_run(1)["summary"]  # type: ignore[index]
    assert len(rows) == len(archive.requests)
    assert summary["requests"]["requests"] == len(archive.requests)
    assert {r["phase"] for r in rows} >= {"probe", "discovery", "fetch"}
    for request in archive.requests:
        q = request.url.params
        if "author" in q or q.get("fields") != "id,author,created_utc":
            continue
        assert int(q["after"]) >= int((NOW - timedelta(days=61)).timestamp())


def test_cli_rejects_active_within_days_elsewhere(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "scan", "--platform", "fake", "--active-within-days", "60",
            "--db", str(tmp_path / "c.db"),
        ],
    )  # fmt: skip
    assert result.exit_code != 0
    assert "does not apply" in result.output


def test_cli_rejects_active_within_days_on_a_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    db = tmp_path / "c.db"
    assert scan(db).exit_code == 0
    again = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--from-run", "1",
            "--active-within-days", "60", "--db", str(db),
        ],
    )  # fmt: skip
    assert again.exit_code != 0
    assert "--from-run skips" in again.output
