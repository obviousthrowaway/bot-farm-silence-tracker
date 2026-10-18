"""Budget edge cases of the X adapter found in review: UTC midnight, the discovery
share of the budget left, over-budget and skipped accounts, partial pages and
doubtful 403s. No network: every request goes through httpx.MockTransport."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app, resume_command
from bot_farm_silence_tracker.events import custom_event
from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.pipeline import rescore_run, run_scan
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.errors import BudgetExceeded, SourceAuthError
from bot_farm_silence_tracker.storage import SAMPLE_ERROR, Storage
from bot_farm_silence_tracker.timeutil import isoformat, parse_datetime

from .helpers import CUTOFF, EVENT, NOW
from .test_x_source import (  # noqa: F401  (x_paths is a fixture)
    OLD,
    SEARCH,
    SINCE,
    UNTIL,
    j,
    make_source,
    scripted_x,
    search_page,
    tweet,
    user,
    x_args,
    x_paths,
    x_settings,
)

MIDNIGHT = datetime(2026, 10, 15, tzinfo=UTC).timestamp()


def _page(ids: list[str], next_token: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "data": [tweet(i, "2026-10-01T10:00:00Z") for i in ids],
        "meta": {"result_count": len(ids)},
    }
    if next_token:
        body["meta"]["next_token"] = next_token
    return body


# ------------------------------------------------------------------- H1 midnight


@pytest.fixture(autouse=True)
def _cli_x_clock_frozen(frozen_cli_x_clock: None) -> None:
    """Every CLI test here runs with the X wall clock on NOW (see the fixture in
    conftest.py), so no test in this module depends on the date the suite runs on."""


def test_response_straddling_midnight_is_charged_in_full_and_again_the_next_day() -> None:
    """X bills page 1 on day D (send time); it is charged after midnight (D+1). The
    same ids come back on D+1, where X bills them again: both must be charged."""
    ids = [f"p{i}" for i in range(100)]
    clocks: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        clocks[0].now = MIDNIGHT + 0.1  # The response is processed after midnight.
        return j(_page(ids))

    source, clock, _ = make_source(handler)
    clocks.append(clock)
    clock.now = MIDNIGHT - 0.1
    acct = Account("x", "1", "a", OLD)
    source.fetch_activity(acct, SINCE, UNTIL)
    assert source.budget.posts == 100  # Charged in full although charged on D+1.
    source.fetch_activity(acct, SINCE, UNTIL)  # Sent and charged on D+1.
    assert source.budget.posts == 200  # X bills them again on D+1.
    assert source.budget.repeats == 0
    source.fetch_activity(acct, SINCE, UNTIL)  # Same day as the previous one: deduped.
    assert source.budget.posts == 200
    assert source.budget.repeats == 100


# ------------------------------------------------------- M1 discovery and the plan


def test_discovery_cap_is_a_share_of_the_budget_left(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        storage.add_spend("x", 40.0, run_id=1)
        ledger = SpendLedger(storage, "x")
        ledger.bind_run(2)
        pages = iter(range(10_000))

        def handler(request: httpx.Request) -> httpx.Response:
            k = next(pages)
            return j(search_page([f"{k}a", f"{k}b"], "more", prefix=f"s{k}"))

        source, _, _ = make_source(handler, ledger=ledger, max_usd="50")
        list(source.discover(SeedSet("x", ("q",)), 10_000))
        assert source.budget.discovery_cap == 3_000_000  # 30% of the $10 left.
        assert 0 < source.budget.discovery_spent <= 3_000_000
        assert any("discovery stopped at its cap of $3.00" in w for w in source.drain_warnings())


def test_plan_spend_asks_for_yes_when_the_budget_left_is_too_small(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        storage.add_spend("x", 40.0, run_id=1)
        source, _, _ = make_source(lambda r: j({}), ledger=SpendLedger(storage, "x"), max_usd="50")
        plan = source.plan_spend(SeedSet("x", ("q",)), n=1000, pool_limit=1000, resume=False)
        assert plan.refuse is None
        assert plan.confirm is not None
        assert "Only $10.00 of the $50.00" in plan.confirm
        assert "$40.00 already spent" in plan.confirm
        assert "10% of the 1,000-account sample" in plan.confirm
        text = "\n".join(plan.lines)
        assert "$10.00 of it left; discovery may use $3.00" in text
        small = source.plan_spend(SeedSet("x", ("q",)), n=10, pool_limit=10, resume=False)
        assert small.confirm is None


def test_cli_resume_refuses_without_yes_when_the_budget_left_is_too_small(
    x_paths: list[str],  # noqa: F811
    tmp_path: Path,
) -> None:
    dry = CliRunner().invoke(app, x_args(tmp_path, "--max-usd", "10", "--dry-run-cost", "--yes"))
    assert dry.exit_code == 0, dry.output
    with Storage(tmp_path / "c.db") as storage:
        storage.add_spend("x", 9.99, run_id=1)
    x_paths.clear()
    resumed = CliRunner().invoke(app, x_args(tmp_path, "--from-run", "1"))
    assert resumed.exit_code == 2, resumed.output
    assert "--yes" in resumed.output
    assert "X timelines: 3 accounts" in resumed.output  # The stored sample's size.
    assert x_paths == []


def test_cli_warns_when_max_usd_overrides_the_source_option(
    x_paths: list[str],  # noqa: F811
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        x_args(tmp_path, "--max-usd", "10", "--source-option", "max_usd=20", "--dry-run-cost"),
    )
    assert "--max-usd 10 overrides --source-option max_usd=20" in result.output


def test_resume_command_names_the_event_instead_of_the_cutoff() -> None:
    values = {"cutoff": EVENT.cutoff_text}
    command = resume_command("x", 1, None, values, {}, event_id=EVENT.id)
    assert f"--event {EVENT.id}" in command and "--cutoff" not in command
    custom = custom_event((CUTOFF + timedelta(hours=6, minutes=30)).replace(tzinfo=UTC))
    command = resume_command("x", 1, None, {"cutoff": custom.cutoff_text}, {}, event_id=custom.id)
    assert f"--event {custom.id}" in command and "--cutoff" not in command


# ------------------------------------------------------------------- L1 overshoot


def test_response_above_its_worst_case_is_charged_in_full_and_stops_the_scan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    body = _page([f"r{i}" for i in range(100)])
    body["includes"] = {"tweets": [{"id": f"inc{i}"} for i in range(100)]}
    with Storage(tmp_path / "c.db") as storage:
        ledger = SpendLedger(storage, "x")
        ledger.bind_run(1)
        source, _, seen = make_source(lambda r: j(body), ledger=ledger, max_usd="0.8")
        with caplog.at_level(logging.WARNING):
            source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
        # 200 posts returned for a 100-post worst case: the actual $1.00 is charged.
        assert source.budget.spent == 1_000_000
        assert ledger.spent("day") == pytest.approx(1.0)
        assert source.budget.overshoots == 1
        assert "more billable resources" in caplog.text
        assert any("more billable resources" in w for w in source.drain_warnings())
        with pytest.raises(BudgetExceeded):
            source.fetch_activity(Account("x", "2", "h", OLD), SINCE, UNTIL)
        assert len(seen) == 1


# ------------------------------------------------------------ L4 discovery lookups


def test_lookups_during_discovery_count_toward_the_discovery_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            return j({"data": [{"id": "t1", "author_id": "9"}], "meta": {"next_token": "n"}})
        return j({"data": [user(request.url.params["ids"])]})

    source, _, _ = make_source(handler)
    stream = source.discover(SeedSet("x", ("q",)), 5)
    acct = next(stream)
    assert acct.created_at is None
    source.hydrate(acct)
    assert source.budget.discovery_spent == 5_000 + 10_000
    source.budget.discovery_spent = source.budget.discovery_cap or 0
    with pytest.raises(BudgetExceeded, match="account lookups"):
        source.hydrate(Account("x", "8", "8"))
    stream.close()
    before = source.budget.discovery_spent
    source.hydrate(Account("x", "7", "7"))  # After discovery: not discovery spend.
    assert source.budget.discovery_spent == before


# --------------------------------------------------------- pipeline: L3, L5, L7


def _one_author_x(timeline: Any, tweets: int = 500) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            body = search_page(["a"], None)
            body["includes"]["users"] = [user("a", tweets=tweets)]
            return j(body)
        return timeline(request)

    return handler


def test_skipped_accounts_are_not_cached_as_empty_and_report_warns(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        source, _, seen = make_source(
            _one_author_x(lambda r: j({"meta": {}}), tweets=5),
            ledger=SpendLedger(storage, "x"),
            min_posts=20,
        )
        outcome = run_scan(source, SeedSet("x", ("q",)), storage, x_settings(n=1))
        assert [r.status for r in outcome.results] == ["skipped"]
        assert outcome.summary["skipped"] == 1
        assert not any(r.url.path.endswith("/tweets") for r in seen)
        cached = storage.get_account("x", "a")
        assert cached is not None and cached.status == "skipped"
        assert cached.meta["skipped"] == "tweet_count_below_min_posts"
        assert cached.meta["skip_min_posts"] == 20
        assert storage.activity_window("x", "a") is None  # Not cached as "no posts".
        lower = rescore_run(storage, outcome.run_id, min_posts=3)
        assert any("cannot score them" in w for w in lower.warnings)
        same = rescore_run(storage, outcome.run_id, min_posts=20)
        assert not any("cannot score them" in w for w in same.warnings)


def test_pages_paid_before_a_mid_account_stop_are_cached_and_reused(tmp_path: Path) -> None:
    counter = iter(range(10_000))

    def endless(request: httpx.Request) -> httpx.Response:
        k = next(counter)
        return j(_page([f"a-{k}-{i}" for i in range(100)], f"more-{k}"))

    db = tmp_path / "c.db"
    with Storage(db) as storage:
        source, _, seen = make_source(
            _one_author_x(endless), ledger=SpendLedger(storage, "x"), max_usd="6"
        )
        first = run_scan(source, SeedSet("x", ("q",)), storage, x_settings(n=1))
        pages = sum(1 for r in seen if r.url.path.endswith("/tweets"))
        assert first.status == "partial"
        assert pages >= 2
        assert len(first.results) == 1 and first.results[0].truncated
        assert len(storage.get_activities("x", "a")) == pages * 100
        assert storage.get_account("x", "a").meta["truncated"] is True  # type: ignore[union-attr]
        ledger_total = SpendLedger(storage, "x").spent("day")
        assert ledger_total <= 6.0

    with Storage(db) as storage:
        source, _, seen = make_source(
            _one_author_x(endless), ledger=SpendLedger(storage, "x"), max_usd="20"
        )
        again = run_scan(
            source, SeedSet("x", ("q",)), storage, x_settings(n=1), from_run=first.run_id
        )
        # Every post is at 10:00:00, so the honest window starts there. The resume
        # fetches only the part older than it (plus the one-second overlap), never
        # the newer range already paid for.
        window = storage.activity_window("x", "a")
        assert window is not None
        assert isoformat(window[0]) == "2026-10-01T10:00:00+00:00"
        timeline = [r for r in seen if r.url.path.endswith("/tweets")]
        assert timeline
        assert {r.url.params["end_time"] for r in timeline} == {"2026-10-01T10:00:01Z"}
        assert again.results[0].truncated


def test_over_budget_account_is_skipped_and_the_scan_goes_on(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            body = search_page(["big", "small"], None)
            body["includes"]["users"] = [user("big", tweets=900_000), user("small", tweets=50)]
            return j(body)
        return j({"data": [tweet("x1", "2026-10-01T10:00:00Z")], "meta": {}})

    with Storage(tmp_path / "c.db") as storage:
        source, _, seen = make_source(handler, ledger=SpendLedger(storage, "x"), max_usd="6")
        outcome = run_scan(source, SeedSet("x", ("q",)), storage, x_settings(n=2))
    statuses = {r.user_id: r.status for r in outcome.results}
    assert statuses == {"big": "skipped_budget", "small": "active"}
    assert outcome.status == "complete"
    timelines = [r.url.path for r in seen if r.url.path.endswith("/tweets")]
    assert timelines == ["/2/users/small/tweets"]


def test_unexplained_403s_before_an_auth_stop_are_retried_on_resume(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            return j(search_page(["a", "b", "c"], None))
        return j({"title": "Forbidden", "detail": "Forbidden"}, 403)

    with Storage(tmp_path / "c.db") as storage:
        source, _, _ = make_source(handler, ledger=SpendLedger(storage, "x"))
        with pytest.raises(SourceAuthError, match="3 unexplained 403"):
            run_scan(source, SeedSet("x", ("q",)), storage, x_settings(n=3))
        sample = storage.get_run_sample(1)
        errored = [acct.user_id for acct, status in sample if status == SAMPLE_ERROR]
        assert len(errored) == 2
        for user_id in errored:
            cached = storage.get_account("x", user_id)
            assert cached is not None and cached.status == "active"  # Not "private".
            assert storage.activity_window("x", user_id) is None
        run = storage.get_run(1)
        assert run is not None
        assert any("marked 'error'" in w for w in run["summary"]["warnings"])


def test_a_lone_unexplained_403_stays_private(tmp_path: Path) -> None:
    calls = iter(range(100))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            return j(search_page(["a", "b"], None))
        if next(calls) == 0:
            return j({"title": "Forbidden", "detail": "Forbidden"}, 403)
        return j({"meta": {}})

    with Storage(tmp_path / "c.db") as storage:
        source, _, _ = make_source(handler, ledger=SpendLedger(storage, "x"))
        scoring = ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=0)
        settings = replace(x_settings(n=2), scoring=scoring)
        outcome = run_scan(source, SeedSet("x", ("q",)), storage, settings)
    assert sorted(r.status for r in outcome.results) == ["active", "private"]


# ------------------------------------------------- failed run: warnings are kept


def test_failed_x_run_logs_and_stores_the_source_warnings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A fatal error mid-timelines skips the CLI's warning printout, so the pipeline
    logs every warning (the adapter's included) and stores them with the failed run."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            return scripted(request)
        return j({"title": "Unauthorized", "detail": "token revoked"}, 401)

    scripted = scripted_x(100)
    with Storage(tmp_path / "c.db") as storage:
        source, _, seen = make_source(handler, max_usd="10", max_discovery_usd="1.6")
        with (
            caplog.at_level(logging.WARNING, logger="bot_farm_silence_tracker.pipeline"),
            pytest.raises(SourceAuthError),
        ):
            run_scan(source, SeedSet("x", ("q",)), storage, x_settings())
        run = storage.get_run(1)
    assert sum(1 for r in seen if r.url.path != SEARCH) == 1  # Stopped at the first 401.
    assert run is not None and run["status"] == "failed"
    stored = run["summary"]["warnings"]
    assert any("discovery stopped at its cap" in w for w in stored)
    logged = [
        r.getMessage() for r in caplog.records if r.name == "bot_farm_silence_tracker.pipeline"
    ]
    assert any("discovery stopped at its cap" in m for m in logged)
    assert all(w in logged for w in stored)


# ----------------------------------------- budget stop at ``since``: split second


def _ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _isoformat_z(value: datetime) -> str:
    """The form the adapter sends as ``start_time`` and ``end_time``."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _timeline_server(posts: list[dict[str, Any]], seen: list[tuple[str, str]]) -> Any:
    """A timeline that honours start_time, end_time and pagination (newest first)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            return j(search_page(["7"]))
        q = request.url.params
        lo, hi = parse_datetime(q["start_time"]), parse_datetime(q["end_time"])
        seen.append((q["start_time"], q["end_time"]))
        chosen = [p for p in posts if lo <= parse_datetime(p["created_at"]) < hi]
        offset = int(q.get("pagination_token") or 0)
        page = chosen[offset : offset + int(q["max_results"])]
        meta: dict[str, Any] = {"result_count": len(page)}
        if offset + len(page) < len(chosen):
            meta["next_token"] = str(offset + len(page))
        return j({"data": page, "meta": meta})

    return handler


def test_budget_stop_whose_oldest_post_is_at_since_claims_coverage_from_the_next_second() -> None:
    at_since = [tweet(f"s{i}", _ts(SINCE)) for i in range(3)]
    newer = [tweet(f"n{i}", _ts(SINCE + timedelta(hours=99 - i))) for i in range(99)]
    seen: list[tuple[str, str]] = []
    # One page fits the cap: page 1 holds 99 newer posts and 1 of the 3 at ``since``.
    source, _, _ = make_source(_timeline_server(newer + at_since, seen), max_usd="0.9")
    acct = Account("x", "7", "h", OLD)
    with pytest.raises(BudgetExceeded) as err:
        source.fetch_activity(acct, SINCE, UNTIL)
    assert err.value.partial is not None
    assert len(err.value.partial) == 99  # The post at the split second is dropped.
    assert all(a.ts > SINCE for a in err.value.partial)
    assert acct.meta["partial_fetch"] == {
        "reason": "budget",
        "since": isoformat(SINCE + timedelta(seconds=1)),
        "posts": 100,
    }


def _same_second_server(count: int) -> Any:
    posts = [tweet(f"s{i}", _ts(SINCE)) for i in range(count)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            return j(search_page(["7"]))
        q = request.url.params
        offset = int(q.get("pagination_token") or 0)
        page = posts[offset : offset + int(q["max_results"])]
        meta: dict[str, Any] = {"result_count": len(page)}
        if offset + len(page) < len(posts):
            meta["next_token"] = str(offset + len(page))
        return j({"data": page, "meta": meta})

    return handler


def test_budget_stop_never_claims_coverage_past_until() -> None:
    """Tests source-layer behavior: when the shifted start would pass ``until``, it is
    clamped to ``until``. Since the recorded start is truncated to the whole second, a
    sub-second ``until`` records ``since``. The pipeline never passes a window shorter
    than 1s (``--baseline-days`` is at least 1 and gap fetches span more than 1s), so
    nothing is claimed and the recorded window is never inverted."""
    until = SINCE + timedelta(milliseconds=500)
    # One page fits the cap, so the stop comes after the first page.
    source, _, _ = make_source(_same_second_server(150), max_usd="0.9")
    acct = Account("x", "7", "h", OLD)
    with pytest.raises(BudgetExceeded) as err:
        source.fetch_activity(acct, SINCE, until)
    # Stored to the second, like every recorded timestamp.
    assert acct.meta["partial_fetch"]["since"] == isoformat(until)
    assert err.value.partial == []


def test_budget_stop_with_until_exactly_one_second_after_since_claims_nothing() -> None:
    """The edge of the clamp: ``until == since + 1s`` also leaves nothing claimed, and
    a resume refetches every post at ``since``."""
    until = SINCE + timedelta(seconds=1)
    source, _, _ = make_source(_same_second_server(150), max_usd="0.9")
    acct = Account("x", "7", "h", OLD)
    with pytest.raises(BudgetExceeded) as err:
        source.fetch_activity(acct, SINCE, until)
    assert acct.meta["partial_fetch"]["since"] == isoformat(until)
    assert err.value.partial == []
    # Nothing was claimed, so the resume asks for the whole window again.
    source, _, _ = make_source(_same_second_server(150))
    resumed = source.fetch_activity(Account("x", "7", "h", OLD), SINCE, until)
    assert len(resumed) == 150


@pytest.mark.parametrize("gap", [False, True], ids=["full-fetch", "gap-fetch"])
def test_resume_refetches_posts_split_at_since_by_a_budget_stop(tmp_path: Path, gap: bool) -> None:
    """A budget stop whose oldest fetched second is ``since`` holds only some of the
    posts at that second. The resume must fetch the rest, not read the window as
    complete. ``gap``: the stop happens on the gap fetch of an earlier partial window."""
    settings = replace(x_settings(n=1), cache_ttl_hours=0.0)
    # The fetch window starts placebo_weeks before the baseline (see controls).
    since = settings.scoring.fetch_start
    at_since = [tweet(f"s{i}", _ts(since)) for i in range(3)]
    if gap:
        # Run 2 stops after 100 posts ending 10 days in; the gap fetch of run 3 gets
        # the overlap post, 98 older ones and 1 of the 3 at ``since``, then stops.
        top = since + timedelta(days=10)
        upper = [tweet(f"u{i}", _ts(top + timedelta(hours=99 - i))) for i in range(100)]
        lower = [tweet(f"l{i}", _ts(since + timedelta(hours=98 - i))) for i in range(98)]
        posts = upper + lower + at_since
        tight_runs = 2
    else:
        posts = [tweet(f"n{i}", _ts(since + timedelta(hours=99 - i))) for i in range(99)]
        posts += at_since
        tight_runs = 1
    seen: list[tuple[str, str]] = []
    handler = _timeline_server(posts, seen)
    db = tmp_path / "c.db"
    with Storage(db) as storage:
        source, _, _ = make_source(handler)
        run_id = run_scan(source, SeedSet("x", ("q",)), storage, settings, sample_only=True).run_id
    for _ in range(tight_runs):
        with Storage(db) as storage:
            source, _, _ = make_source(handler, max_usd="0.9")
            outcome = run_scan(source, SeedSet("x", ("q",)), storage, settings, from_run=run_id)
            assert outcome.status == "partial"
            run_id = outcome.run_id
            window = storage.activity_window("x", "7")
            assert window is not None and window[0] > since  # Never claimed as complete.
    with Storage(db) as storage:
        source, _, _ = make_source(handler)
        seen.clear()
        outcome = run_scan(source, SeedSet("x", ("q",)), storage, settings, from_run=run_id)
        assert outcome.status == "complete"
        # Only the split second (plus the GAP_OVERLAP second) is bought again.
        assert seen == [(_isoformat_z(since), _isoformat_z(since + timedelta(seconds=2)))]
        rows = storage.get_activities("x", "7", since, UNTIL + timedelta(days=30))
        window = storage.activity_window("x", "7")
        account = storage.get_account("x", "7")
    assert window is not None and window[0] == since
    assert sorted(a.ts for a in rows if a.ts == since) == [since] * 3
    assert len(rows) == len(posts)
    assert account is not None
    assert "partial_fetch" not in account.meta
    assert account.meta.get("truncated") is False
