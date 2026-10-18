"""Wait policy of the reddit-archive adapter (timeout vs load 422s, the escalation guard,
reset waits), the 422 classifier, the request log's wait columns and privacy, and
``--active-within-days`` on resume and in reports. Against the scripted fake archive."""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import RateLimited, SourceConfig, SourceError
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.reddit_archive import (
    MIN_RPS,
    RedditArchiveSource,
    SlowQueryError,
)
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF, NOW, MidRandom
from .test_archive_throughput import (
    Clock,
    build,
    patch_client,
    scan,
    stored_params,
)
from .test_reddit_archive import (
    LOAD,
    OK_EMPTY,
    TIMEOUT,
    Archive,
    build_world,
    comment,
    fetch,
    make_source,
    pulse,
    runner,
    slow,
)


class Ticking:
    """Wrap a fake archive so the injected clock advances on every request."""

    def __init__(self, archive: Archive, clock: Clock, step: float = 60.0) -> None:
        self.archive, self.clock, self.step = archive, clock, step
        self.requests = archive.requests

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.clock.now += self.step
        return self.archive(request)


# -- 422 classifier --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(422, json=TIMEOUT), "timeout"),  # The ambiguous live message.
        (httpx.Response(422, json={"error": "Query timed out"}), "timeout"),
        (httpx.Response(422, json={"error": "TIMEOUT"}), "timeout"),
        (httpx.Response(422, json={"error": "Please slow down"}), "load"),
        (httpx.Response(422, json={"error": {"message": "Timed Out"}}), "timeout"),
        (httpx.Response(422, json={"error": ["slow down"]}), "load"),
        (httpx.Response(422, json={"error": 42}), None),
        (httpx.Response(422, json={"error": None}), None),
        (httpx.Response(422, json={"detail": "invalid parameter"}), None),
        (httpx.Response(422, json=["timeout"]), "timeout"),  # Not a dict: read as text.
        (httpx.Response(422, text="Request timed out"), "timeout"),  # Not JSON.
        (httpx.Response(422, text="bad request"), None),
    ],
)
def test_slow_kind(response: httpx.Response, kind: str | None) -> None:
    assert RedditArchiveSource._slow_kind(response) == kind


# -- timeout 422 waits ---------------------------------------------------------------


def test_timeout_wait_is_jittered_backoff_and_ignores_the_reset() -> None:
    archive = Archive([pulse()], script=[OK_EMPTY, slow(reset="50")])
    src, sleeps, _ = build(archive, slowdown_wait="10")
    src._jitter = random.Random(7)  # A real random source: 10 s x U(0.75, 1.25).
    fetch(src)
    assert len(sleeps) == 1 and 7.5 <= sleeps[0] <= 12.5
    assert src.http.rate[0] == 0.5  # Not lowered.


def test_timeout_wait_doubles_per_step_and_caps_at_120_s() -> None:
    clock = Clock()  # A minute between requests, so the guard never escalates.
    archive = Archive([pulse()], script=[slow()] * 4)
    src, sleeps, _ = build(
        Ticking(archive, clock), clock=clock, slowdown_wait="40", max_slowdown_retries="4"
    )
    fetch(src)
    assert sleeps == [40.0, 80.0, 120.0, 120.0]  # Capped (jitter factor 1.0 here).


def test_timeout_422_resets_the_success_streak() -> None:
    archive = Archive([pulse()], script=[slow(LOAD)])
    src, _, _ = make_source(archive, slowdown_wait="0", recovery_streak="3")
    fetch(src, "a")  # Load 422 (0.25 rps), then 2 successes: streak 2.
    assert src._ok_streak == 2
    archive.script = [slow()]
    fetch(src, "b")  # A timeout resets the streak; then 2 more successes.
    assert src._ok_streak == 2
    assert src.http.rate[0] == 0.25  # Recovery needs 3 clean requests in a row.


# -- escalation guard ----------------------------------------------------------------


def test_one_querys_retry_ladder_does_not_escalate_by_itself() -> None:
    archive = Archive([pulse()], script=[slow(reset="20")] * 6)
    src, sleeps, _ = make_source(archive, slowdown_wait="1")
    fetch(src)
    # Six timeouts within a minute, all of one account's posts query: retry (1 s),
    # split (4 s), retry and split again, and so on. None is escalated, because the
    # guard counts one timeout per logical query.
    assert sleeps == [1.0, 4.0, 1.0, 4.0, 1.0, 4.0]
    assert src.http.rate[0] == 0.5
    stats = src.request_summary()
    assert stats is not None
    assert stats["status_422_timeout"] == 6 and stats["status_422_escalated"] == 0


def test_timeouts_of_three_different_queries_escalate_the_next_to_load() -> None:
    archive = Archive([pulse()])
    src, sleeps, _ = make_source(archive, slowdown_wait="1")
    for handle in ("a", "b", "c"):  # Three posts queries time out once each.
        archive.script = [slow(reset="20")]
        fetch(src, handle)
    assert src.request_summary()["status_422_escalated"] == 0  # type: ignore[index]
    archive.script = [slow(reset="20")]
    archive.requests.clear()
    sleeps.clear()
    fetch(src, "d")
    # The fourth timeout is escalated: the rate is halved and the wait is the reset
    # plus 1 s and 2.5 s of jitter.
    assert sleeps == [23.5]
    assert src.http.rate[0] == 0.25
    stats = src.request_summary()
    assert stats is not None
    assert stats["status_422_timeout"] == 4 and stats["status_422_escalated"] == 1
    assert stats["rate_halvings"] == 1
    assert any("several timeout 422s" in w for w in src.drain_warnings())
    # The identical query is not retried: the next request asks for half the window.
    first, second = (r.url.params for r in archive.requests[:2])
    assert int(second["before"]) - int(second["after"]) < (
        int(first["before"]) - int(first["after"])
    )


def test_an_escalated_timeout_with_limit_auto_goes_straight_to_limit_100() -> None:
    archive = Archive([pulse()])
    src, _, _ = make_source(archive, slowdown_wait="1", history_limit="auto")
    for handle in ("a", "b", "c"):
        archive.script = [slow(reset="20")]
        fetch(src, handle)
    archive.script = [slow(reset="20")]
    archive.requests.clear()
    fetch(src, "d")
    first, second = (r.url.params for r in archive.requests[:2])
    assert first["limit"] == "auto" and second["limit"] == "100"
    assert (second["after"], second["before"]) == (first["after"], first["before"])


def test_an_escalated_timeout_without_a_narrower_query_is_still_retried() -> None:
    archive = Archive([pulse()])
    src, _, _ = make_source(archive, slowdown_wait="1")
    for handle in ("a", "b", "c"):
        archive.script = [slow(reset="20")]
        fetch(src, handle)
    archive.script = [slow(reset="20")]
    archive.requests.clear()
    # A coverage-style query has no narrower window: the plain retry still runs.
    params = {"subreddit": "worldnews", "after": 0, "before": 10, "limit": 1}
    rows = src._run_op(lambda op: src._narrowing(op, "/api/comments/search", params, None), "x")
    assert rows == [] and len(archive.requests) == 2
    assert src.request_summary()["status_422_escalated"] == 1  # type: ignore[index]


def test_timeouts_spread_over_more_than_a_minute_do_not_escalate() -> None:
    clock = Clock()

    archive = Archive([pulse()], script=[slow(reset="20")] * 4)
    src, sleeps, _ = build(Ticking(archive, clock, 61.0), clock=clock, slowdown_wait="1")
    fetch(src)
    assert 23.5 not in sleeps
    assert src.http.rate[0] == 0.5
    assert src.request_summary()["status_422_escalated"] == 0  # type: ignore[index]


def test_five_422s_in_the_last_ten_responses_escalate_a_timeout() -> None:
    clock = Clock()

    # Five load 422s (two failed accounts and one retried one), then one timeout,
    # twenty seconds apart: never 3 timeouts within a minute, all within 120 seconds.
    archive = Archive([pulse()], script=[slow(LOAD)] * 5 + [slow(reset="30")])
    src, sleeps, _ = build(
        Ticking(archive, clock, 20.0), clock=clock, slowdown_wait="0", max_slow_queries="10"
    )
    for handle in ("a", "b"):
        with pytest.raises(SlowQueryError):
            fetch(src, handle)
    fetch(src, "c")  # Load 422, retry: escalated timeout, retry ok.
    stats = src.request_summary()
    assert stats is not None and stats["status_422_escalated"] == 1
    assert sleeps[-1] == 33.5  # The reset (30 s) plus 1 s and 2.5 s of jitter.


@pytest.mark.parametrize(("gap", "escalated"), [(119.0, 1), (121.0, 0)])
def test_responses_older_than_120_s_leave_the_recent_count(gap: float, escalated: int) -> None:
    clock = Clock()
    archive = Archive([pulse()], script=[slow(LOAD)] * 5)
    src, _, _ = build(archive, clock=clock, slowdown_wait="0", max_slow_queries="10")
    for handle in ("a", "b"):
        with pytest.raises(SlowQueryError):
            fetch(src, handle)
    fetch(src, "c")  # Five load 422s at clock 0.
    assert all(at == 0.0 for at, _ in src._recent)
    clock.now = gap
    archive.script = [slow(reset="30")]
    fetch(src, "d")
    assert src.request_summary()["status_422_escalated"] == escalated  # type: ignore[index]


def test_escalation_starts_over_after_it_fires() -> None:
    archive = Archive([pulse()])
    src, _, _ = make_source(archive, slowdown_wait="0")
    for handle in ("a", "b", "c", "d", "e", "f"):
        archive.script = [slow(reset="5")]
        fetch(src, handle)
    # The fourth query's timeout escalates and clears the counts; the next two
    # different queries' timeouts are not enough to escalate again.
    assert src.request_summary()["status_422_escalated"] == 1  # type: ignore[index]


# -- reset waits -----------------------------------------------------------------------


def test_429_retry_after_wins_over_the_reset() -> None:
    limited = httpx.Response(429, headers={"Retry-After": "4", "X-RateLimit-Reset": "50"})
    archive = Archive([pulse()], script=[limited])
    src, _, http_sleeps = make_source(archive)
    fetch(src)
    assert 4.0 in http_sleeps and 53.5 not in http_sleeps
    row = src.drain_request_log()[0]
    assert (row["wait_s"], row["reset_s"], row["retry_after_s"]) == (4.0, 50.0, 4.0)


def test_load_422_waits_for_the_reset_with_jitter() -> None:
    archive = Archive([pulse()], script=[slow(LOAD, reset="12")])
    src, sleeps, _ = make_source(archive, slowdown_wait="1")
    fetch(src)
    assert sleeps == [15.5]
    assert src.http.rate[0] == 0.25


def test_exhausted_remaining_waits_for_the_reset_before_the_next_request() -> None:
    drained = httpx.Response(
        200, json={"data": []}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9"}
    )
    archive = Archive([pulse()], script=[drained])
    src, sleeps, _ = make_source(archive)
    fetch(src)
    assert sleeps == [12.5]  # Before the comments request: 9 s + 1 s + 2.5 s.
    rows = src.drain_request_log()
    assert rows[0]["wait_s"] == 12.5 and rows[1]["wait_s"] is None
    assert src.request_summary()["reset_wait_s"] == 12.5  # type: ignore[index]


def test_the_request_after_a_reset_wait_is_paced_again() -> None:
    clock = Clock()
    sent: list[float] = []

    def sleep(seconds: float) -> None:
        clock.now += seconds

    drained = httpx.Response(
        200, json={"data": []}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "44"}
    )
    archive = Archive([pulse()], script=[drained])

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(clock.now)
        clock.now += 0.5  # Latency.
        return archive(request)

    http = HttpClient(
        requests_per_second=RedditArchiveSource.default_rps,
        burst=RedditArchiveSource.default_burst,
        transport=httpx.MockTransport(transport),
        sleep=sleep,
        clock=clock,
    )
    config = SourceConfig(
        http=http,
        credentials={},
        now=NOW,
        cutoff=CUTOFF,
        seed=1,
        options={"coverage_threshold": "0"},
        baseline_start=CUTOFF - timedelta(days=2),
    )
    src = RedditArchiveSource(config, sleep=sleep, rng=MidRandom())
    fetch(src, "a")  # Posts drain the window; comments wait 47.5 s for the reset.
    fetch(src, "b")
    assert sent[1] - sent[0] >= 47.5
    # Without restarting the pacer, the bucket refilled during the reset wait and the
    # next request went out right behind the delayed one (0.5 s later).
    assert sent[2] - sent[1] >= 1 / RedditArchiveSource.default_rps


def test_an_exhausted_window_with_a_reset_over_the_cap_raises_rate_limited() -> None:
    drained = httpx.Response(
        200,
        json={"data": []},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000"},
    )
    archive = Archive([pulse()], script=[drained])
    src, sleeps, _ = make_source(archive)
    assert src.http.max_retry_wait < 1000
    with pytest.raises(RateLimited):
        fetch(src)
    assert sleeps == [] and len(archive.requests) == 1  # Never slept, never sent again.


def test_a_429_whose_retry_after_is_over_the_cap_logs_no_wait() -> None:
    limited = httpx.Response(429, headers={"Retry-After": "5000"})
    archive = Archive([pulse()], script=[limited])
    src, _, http_sleeps = make_source(archive)
    with pytest.raises(RateLimited):
        fetch(src)
    assert http_sleeps == []
    row = src.drain_request_log()[0]
    assert (row["status"], row["wait_s"], row["retry_after_s"]) == (429, None, 5000.0)


def test_remaining_above_zero_does_not_wait() -> None:
    fine = httpx.Response(
        200, json={"data": []}, headers={"X-RateLimit-Remaining": "3", "X-RateLimit-Reset": "9"}
    )
    src, sleeps, _ = make_source(Archive([pulse()], script=[fine]))
    fetch(src)
    assert sleeps == []


def test_503_halves_and_waits_backoff_not_the_reset() -> None:
    busy = httpx.Response(503, headers={"X-RateLimit-Reset": "40"})
    archive = Archive([pulse()], script=[busy])
    src, sleeps, http_sleeps = make_source(archive)
    fetch(src)
    assert src.http.rate[0] == 0.25
    assert 1.0 in http_sleeps  # The client's first backoff.
    assert all(s < 40 for s in http_sleeps) and sleeps == []
    assert src.drain_request_log()[0]["wait_s"] == 1.0


def test_every_attempt_passes_the_pacer() -> None:
    archive = Archive(
        [pulse()], script=[slow(), slow(LOAD, reset="3"), httpx.Response(503), OK_EMPTY]
    )
    src, _, _ = make_source(archive, slowdown_wait="0")
    bucket = src.http._bucket
    calls: list[float] = []
    original = bucket.acquire

    def counted() -> float:
        calls.append(bucket.rate)
        return original()

    bucket.acquire = counted  # type: ignore[method-assign]
    with pytest.raises(SlowQueryError):
        fetch(src)
    archive.script = []
    fetch(src, "b")
    assert len(calls) == len(archive.requests)
    assert max(calls) <= 0.5


# -- request log -------------------------------------------------------------------------


def test_never_sent_attempts_are_not_logged() -> None:
    state = {"failed": False}
    inner = Archive([pulse()])

    def transport(request: httpx.Request) -> httpx.Response:
        if not state["failed"]:
            state["failed"] = True
            raise httpx.ConnectError("refused", request=request)
        return inner(request)

    src, _, _ = make_source(transport)
    fetch(src)
    rows = src.drain_request_log()
    assert len(rows) == len(inner.requests) == 2
    stats = src.request_summary()
    assert stats is not None and stats["requests"] == 2 and stats["not_sent"] == 1


def test_no_handle_or_query_value_reaches_the_request_log(tmp_path: Path) -> None:
    handle = "Secret_Handle_4821"
    sub = "privatesubname"
    archive = Archive(
        [pulse(), comment("1", handle, sub, CUTOFF - timedelta(hours=1))],
        script=[slow(reset="3")],
    )
    src, _, _ = make_source(archive, slowdown_wait="0")
    list(src.discover(SeedSet("reddit-archive", (sub,)), 3))
    fetch(src, handle)
    rows = src.drain_request_log()
    with Storage(tmp_path / "c.db") as storage:
        storage.save_request_log(1, "reddit-archive", rows)
        stored = storage.get_request_log(1)
    sensitive: set[str] = {handle, sub}
    for request in archive.requests:
        for key, value in request.url.params.items():
            if key != "limit":  # The page limit is stored on purpose (page_limit).
                sensitive.add(value)
    text = json.dumps(rows) + json.dumps(stored)
    for value in sensitive:
        assert value not in text, value


def test_request_rows_carry_the_wait_columns(tmp_path: Path) -> None:
    archive = Archive([pulse()], script=[slow(LOAD, reset="6")])
    src, _, _ = make_source(archive)
    fetch(src)
    rows = src.drain_request_log()
    assert (rows[0]["wait_s"], rows[0]["reset_s"], rows[0]["retry_after_s"]) == (9.5, 6.0, None)
    with Storage(tmp_path / "c.db") as storage:
        storage.save_request_log(1, "reddit-archive", rows)
        first = storage.get_request_log(1)[0]
    assert (first["wait_s"], first["reset_s"], first["retry_after_s"]) == (9.5, 6.0, None)


def test_an_older_request_log_table_gains_the_wait_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE request_log (run_id INTEGER NOT NULL, seq INTEGER NOT NULL, platform"
        " TEXT NOT NULL, at TEXT NOT NULL, phase TEXT, endpoint TEXT, status INTEGER,"
        " latency_ms REAL, page_limit TEXT, window_s INTEGER, rps REAL, slow_kind TEXT,"
        " attempt INTEGER, PRIMARY KEY (run_id, seq))"
    )
    conn.execute(
        "INSERT INTO request_log (run_id, seq, platform, at, status) VALUES (1, 0, 'r', 'x', 200)"
    )
    conn.commit()
    conn.close()
    with Storage(path) as storage:
        storage.save_request_log(1, "r", [{"status": 422, "wait_s": 3.0, "reset_s": 2.0}])
        rows = storage.get_request_log(1)
    assert [(r["status"], r["wait_s"], r["reset_s"]) for r in rows] == [
        (200, None, None),
        (422, 3.0, 2.0),
    ]


# -- clocks and the floor ----------------------------------------------------------------


def test_observed_rps_uses_wall_clock_time() -> None:
    clock, wall = Clock(), Clock()
    src, _, _ = build(Archive([pulse()]), clock=clock, wall=wall)
    fetch(src)
    clock.now, wall.now = 10.0, 40.0  # The host slept for 30 s: monotonic time stopped.
    stats = src.request_summary()
    assert stats is not None
    assert (stats["elapsed_s"], stats["wall_elapsed_s"]) == (10.0, 40.0)
    assert stats["observed_rps"] == round(2 / 40.0, 3)


def test_floor_comparison_uses_the_same_epsilon_everywhere() -> None:
    src, _, _ = make_source(Archive([]), max_rps=str(MIN_RPS))
    assert src.request_summary()["time_at_floor_s"] == 0.0  # type: ignore[index]
    assert src._floor_since is not None  # Starts at the floor.
    src._set_rps(MIN_RPS + 1e-12)  # Rounding noise: still at the floor.
    assert src._floor_since is not None
    src._note_load()
    assert src._stats["halvings"] == 0


# -- --active-within-days on resume and in reports --------------------------------------


def resume(db: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--from-run", "1",
            "--now", "2026-10-14T00:00:00Z", "--db", str(db), *extra,
        ],
    )  # fmt: skip


def test_resume_inherits_active_within_days_and_reports_show_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    db = tmp_path / "c.db"
    first = scan(db, "--active-within-days", "60")
    assert first.exit_code == 0, first.output
    assert "Discovery: --active-within-days 60:" in first.output
    again = resume(db)
    assert again.exit_code == 0, again.output
    assert stored_params(db, 2)["active_within_days"] == 60
    assert "--active-within-days 60 (inherited from run 1)" in again.output
    report = runner.invoke(app, ["report", "--run-id", "2", "--db", str(db)])
    assert report.exit_code == 0, report.output
    assert "--active-within-days 60 (inherited from run 1)" in report.output
    plain = runner.invoke(app, ["report", "--run-id", "1", "--db", str(db)])
    assert "Discovery: --active-within-days 60:" in plain.output


def test_a_run_without_the_flag_reports_no_lookback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    db = tmp_path / "c.db"
    assert scan(db).exit_code == 0
    assert "active_within_days" not in stored_params(db, 1)
    report = runner.invoke(app, ["report", "--run-id", "1", "--db", str(db)])
    assert "--active-within-days" not in report.output


def test_cli_refuses_a_lookback_wholly_after_the_cutoff_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(build_world())
    patch_client(monkeypatch, archive)
    result = scan(tmp_path / "c.db", "--active-within-days", "2")  # now is cutoff + 4 days.
    assert result.exit_code == 2, result.output
    assert "does not overlap the baseline window" in result.output
    assert "Nothing was sent to the archive" in result.output
    assert archive.requests == []  # No probe, no coverage check.


def test_cli_refuses_a_lookback_shorter_than_an_hour_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(build_world())
    patch_client(monkeypatch, archive)
    # 3 days and 30 minutes: even at the earliest horizon the scan accepts (cutoff + 3
    # days) the window overlaps the baseline for 30 minutes only.
    result = scan(tmp_path / "c.db", "--active-within-days", "3.0208333")
    assert result.exit_code == 2, result.output
    assert "for only 30 minutes" in result.output
    assert archive.requests == []


DAYS = timedelta(days=1)


@pytest.mark.parametrize(
    ("days", "now", "allow_stale", "refused"),
    [
        (3.0, CUTOFF + 4 * DAYS, False, True),  # D <= min silence: always empty.
        (1.0, CUTOFF + 40 * DAYS, False, True),
        (3.5, CUTOFF + 4 * DAYS, False, False),  # A horizon at cutoff + 3 leaves 12 h.
        # now - D is past the cutoff + min silence, but a horizon right at cutoff + 3
        # days still leaves a 7-day window: not refused.
        (10.0, CUTOFF + 30 * DAYS, False, False),
        (2.0, CUTOFF + 4 * DAYS, True, False),  # A stale horizon may fall before it.
        (0.01, CUTOFF + 4 * DAYS, True, True),  # Shorter than an hour in any case.
        (2.0, CUTOFF + 2 * DAYS, False, False),  # Stale whatever happens: left to the probe.
        (60.0, CUTOFF + 4 * DAYS, False, False),
    ],
)
def test_active_within_preflight(days: float, now: Any, allow_stale: bool, refused: bool) -> None:
    problem = RedditArchiveSource.active_within_problem(
        days=days,
        now=now,
        cutoff=CUTOFF,
        baseline_start=CUTOFF - 30 * DAYS,
        min_silence_days=3.0,
        allow_stale_source=allow_stale,
    )
    assert (problem is not None) == refused, problem


def test_a_clipped_window_shorter_than_an_hour_is_refused_not_crashed() -> None:
    archive = Archive([pulse(), *build_world()])
    # NOW is cutoff + 4 days, so 4 days and 30 minutes overlap the baseline by 30 min.
    src, _, _ = build(archive, active_within_days=4 + 30 / 1440)
    with pytest.raises(SourceError, match="for only 30 minutes"):
        src.discovery_window()
    with pytest.raises(SourceError, match="for only 30 minutes"):
        list(src.discover(SeedSet("reddit-archive", ("worldnews",)), 5))
    assert archive.requests == []


def test_resume_without_a_stored_pool_factor_records_the_platform_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    db = tmp_path / "c.db"
    assert scan(db).exit_code == 0
    with Storage(db) as storage:
        params = stored_params(db, 1)
        del params["pool_factor"]
        storage.update_run_params(1, params)
    again = resume(db)
    assert again.exit_code == 0, again.output
    assert stored_params(db, 2)["pool_factor"] == 1.2


def test_a_probe_retry_pause_is_logged_as_a_wait() -> None:
    archive = Archive([], hook=lambda q: slow() if "author" not in q else None)
    src, sleeps, _ = make_source(archive, slowdown_wait="0", max_slow_queries="50")
    src.slowdown_wait = 3.0
    with pytest.raises(SourceError):
        src.data_horizon()
    rows = src.drain_request_log()
    assert 3.0 in sleeps
    assert sum(r["wait_s"] or 0.0 for r in rows) == pytest.approx(sum(sleeps))


# -- review fixes: short history_limit, escalated timeouts, reset caps ------------------


def _thirty_comments() -> list[dict[str, Any]]:
    return [pulse()] + [
        comment(f"c{i}", "alice", "a", CUTOFF - timedelta(minutes=i + 1)) for i in range(30)
    ]


@pytest.mark.parametrize("limit", ["10", "30", "100", "auto"])
def test_a_history_limit_below_100_still_pages_through_every_comment(limit: str) -> None:
    src, _, _ = make_source(Archive(_thirty_comments()), history_limit=limit)
    account, got = fetch(src)
    assert len(got) == 30
    assert account.meta["truncated"] is False


def test_an_escalated_timeout_marks_its_query_so_it_cannot_count_again() -> None:
    archive = Archive([pulse()])
    src, _, _ = make_source(archive, slowdown_wait="1")
    for handle in ("a", "b", "c"):
        archive.script = [slow(reset="20")]
        fetch(src, handle)
    assert len(src._timeouts) == 3
    # The fourth query: an escalated timeout, then a timeout of the same query.
    archive.script = [slow(reset="20"), slow(reset="20")]
    fetch(src, "d")
    assert src.request_summary()["status_422_escalated"] == 1  # type: ignore[index]
    # The escalation cleared the count, and the same query's second timeout is not
    # added to it, so it takes three more queries to escalate again.
    assert len(src._timeouts) == 0


@pytest.mark.parametrize("kind", ["429", "load422"])
def test_a_reset_is_capped_raw_and_margin_and_jitter_come_after(kind: str) -> None:
    def build_script(reset: str) -> list[httpx.Response]:
        if kind == "429":
            return [httpx.Response(429, headers={"X-RateLimit-Reset": reset})]
        return [slow(LOAD, reset=reset)]

    archive = Archive([pulse()], script=build_script("10"))
    src, sleeps, http_sleeps = make_source(archive)
    src.http.max_retry_wait = 10.0
    fetch(src)  # A raw reset equal to the cap is waited for, plus 1 s and 2.5 s.
    assert 13.5 in sleeps + http_sleeps

    archive = Archive([pulse()], script=build_script("12"))
    src, sleeps, http_sleeps = make_source(archive)
    src.http.max_retry_wait = 10.0
    with pytest.raises(RateLimited):  # 12 > 10, though 12 + 3.5 is inside 10 + 6.
        fetch(src)
    assert sleeps == [] and http_sleeps == []
