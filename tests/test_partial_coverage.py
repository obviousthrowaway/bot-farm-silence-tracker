"""Round-3 review regressions: honest partial coverage, gap fetches, skips that keep
earlier paid data, and the small CLI and planning fixes.

P1-P4 are the reviewer's probes turned into tests. The X handler here honours
``start_time`` and ``end_time`` the way X does, so a gap fetch that sets ``end_time``
to where the cached data starts is billed only for the older posts.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import _print_summary, app
from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import PARTIAL_FETCH_KEY, Account
from bot_farm_silence_tracker.output import truncated_notice
from bot_farm_silence_tracker.pipeline import rescore_run, run_scan
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.x import TIMELINE_DEPTH_CAP
from bot_farm_silence_tracker.storage import Storage
from bot_farm_silence_tracker.timeutil import parse_datetime

from .helpers import CUTOFF, EVENT, NOW, make_account
from .test_x_source import BASELINE, SEARCH, j, make_source, search_page, tweet, user

SEEDS = SeedSet("x", ("q",))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _settings() -> Any:
    from .test_x_source import x_settings

    # placebo_weeks=0: the coverage of the baseline window itself is under test.
    scoring = ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=0)
    return replace(x_settings(n=1), scoring=scoring)


class Timeline:
    """One X account whose timeline honours start_time, end_time and pagination.

    Posts every ``step`` from five days before the baseline up to ``stop``, newest
    first. ``fail_calls`` timeline requests (by number, from 0) answer 500.
    ``depth_cap`` stops the timeline after that many posts in all, like X's cap.
    """

    def __init__(
        self,
        stop: datetime,
        *,
        tweets: int = 2000,
        step: timedelta = timedelta(hours=1),
        fail_calls: frozenset[int] = frozenset(),
        depth_cap: int | None = None,
    ) -> None:
        self.tweets = tweets
        self.fail_calls = fail_calls
        self.depth_cap = depth_cap
        self.calls: list[httpx.Request] = []
        posts = []
        t = BASELINE - timedelta(days=5)
        while t < stop:
            posts.append(t)
            t += step
        self.posts = sorted(posts, reverse=True)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            body = search_page(["a"], None)
            body["includes"]["users"] = [user("a", tweets=self.tweets)]
            return j(body)
        if not request.url.path.endswith("/tweets"):
            return j({"data": [user("a", tweets=self.tweets)]})
        number = len(self.calls)
        self.calls.append(request)
        if number in self.fail_calls:
            return j({"title": "boom"}, 500)
        start = parse_datetime(request.url.params["start_time"])
        end = parse_datetime(request.url.params["end_time"])
        chosen = [ts for ts in self.posts if start <= ts < end]
        if self.depth_cap is not None:
            chosen = chosen[: self.depth_cap]
        offset = int(request.url.params.get("pagination_token") or 0)
        page = chosen[offset : offset + 100]
        meta: dict[str, Any] = {"result_count": len(page)}
        if offset + 100 < len(chosen):
            meta["next_token"] = str(offset + 100)
        data = [tweet(f"a-{_iso(ts)}", _iso(ts)) for ts in page]
        return j({"data": data, "meta": meta})

    def in_window(self, start: datetime, end: datetime) -> int:
        return sum(1 for ts in self.posts if start <= ts < end)


def _gap_end(storage: Storage) -> str:
    window = storage.activity_window("x", "a")
    assert window is not None
    return (window[0] + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------- MEDIUM-1: coverage


def test_p1_error_mid_account_caches_honest_coverage_and_resume_fetches_only_the_gap(
    tmp_path: Path,
) -> None:
    db = tmp_path / "c.db"
    with Storage(db) as storage:
        failing = Timeline(NOW, fail_calls=frozenset(range(2, 50)))
        source, _, _ = make_source(failing, ledger=SpendLedger(storage, "x"))
        first = run_scan(source, SEEDS, storage, _settings())
        assert [r.status for r in first.results] == ["error"]
        window = storage.activity_window("x", "a")
        assert window is not None
        cached = storage.get_activities("x", "a")
        assert len(cached) == 200
        # Newest first: the cache covers [oldest fetched, until), not the whole window.
        assert window[0] == min(a.ts for a in cached) > BASELINE
        account = storage.get_account("x", "a")
        assert account is not None
        assert account.meta[PARTIAL_FETCH_KEY]["reason"] == "error"
        assert account.meta[PARTIAL_FETCH_KEY]["posts"] == 200
        gap_end = _gap_end(storage)
        oldest = window[0]

    with Storage(db) as storage:
        timeline = Timeline(NOW)
        source, _, _ = make_source(timeline, ledger=SpendLedger(storage, "x"))
        again = run_scan(source, SEEDS, storage, _settings(), from_run=first.run_id)
        # The failed account is retried, but only below the paid pages.
        assert timeline.calls
        assert {r.url.params["end_time"] for r in timeline.calls} == {gap_end}
        bought = timeline.in_window(BASELINE, oldest + timedelta(seconds=1))
        assert again.summary["spend_usd"] == pytest.approx(bought * 0.005)
        result = again.results[0]
        assert result.status == "active" and not result.truncated
        assert result.partial_reason is None
        assert result.baseline_posts == 30 * 24
        window = storage.activity_window("x", "a")
        assert window is not None and window[0] == BASELINE
        assert len(storage.get_activities("x", "a")) == timeline.in_window(BASELINE, NOW)
        account = storage.get_account("x", "a")
        assert account is not None and PARTIAL_FETCH_KEY not in account.meta


def test_p2_budget_stop_is_partial_not_a_cliff_and_resume_fills_the_gap(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    half_hourly = timedelta(minutes=30)  # 1,632 posts in the window: more than $5 buys.
    with Storage(db) as storage:
        source, _, _ = make_source(
            Timeline(NOW, step=half_hourly), ledger=SpendLedger(storage, "x"), max_usd="5"
        )
        first = run_scan(source, SEEDS, storage, _settings())
        assert first.status == "partial"
        result = first.results[0]
        assert not result.cliff and result.truncated
        assert result.partial_reason == "budget"
        assert first.summary["truncated_interrupted"] == 1
        assert first.summary["truncated_platform"] == 0
        note = truncated_notice(first)
        assert note is not None and "INTERRUPTED" in note and "TRUNCATED:" not in note
        assert SpendLedger(storage, "x").spent("day") <= 5.0
        gap_end = _gap_end(storage)

    with Storage(db) as storage:
        timeline = Timeline(NOW, step=half_hourly)
        source, _, _ = make_source(timeline, ledger=SpendLedger(storage, "x"), max_usd="20")
        again = run_scan(source, SEEDS, storage, _settings(), from_run=first.run_id)
        assert {r.url.params["end_time"] for r in timeline.calls} == {gap_end}
        result = again.results[0]
        assert not result.truncated and result.partial_reason is None
        assert len(storage.get_activities("x", "a")) == timeline.in_window(BASELINE, NOW)


def test_platform_cap_is_reused_without_refetching(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    every = timedelta(minutes=10)
    with Storage(db) as storage:
        capped = Timeline(NOW, tweets=100_000, step=every, depth_cap=TIMELINE_DEPTH_CAP)
        source, _, _ = make_source(capped, ledger=SpendLedger(storage, "x"), max_usd="50")
        first = run_scan(source, SEEDS, storage, _settings())
        result = first.results[0]
        assert result.truncated and result.partial_reason == "platform_cap"
        assert first.summary["truncated_platform"] == 1
        note = truncated_notice(first)
        assert note is not None and "platform stopped returning history" in note
        assert "INTERRUPTED" not in note
        window = storage.activity_window("x", "a")
        assert window is not None and window[0] > BASELINE

    with Storage(db) as storage:
        again_timeline = Timeline(NOW, tweets=100_000, step=every, depth_cap=TIMELINE_DEPTH_CAP)
        source, _, _ = make_source(again_timeline, ledger=SpendLedger(storage, "x"), max_usd="50")
        again = run_scan(source, SEEDS, storage, _settings(), from_run=first.run_id)
        assert again_timeline.calls == []  # Refetching cannot return more history.
        assert again.results[0].partial_reason == "platform_cap"
        fresh = run_scan(source, SEEDS, storage, _settings())  # A new run within the TTL.
        assert again_timeline.calls == []
        assert fresh.results[0].truncated


def test_gap_fetch_counts_earlier_posts_toward_the_x_depth_cap() -> None:
    timeline = Timeline(NOW)
    source, _, _ = make_source(timeline)
    account = Account("x", "a", "a", parse_datetime("2020-01-02T03:04:05Z"), {"tweet_count": 2000})
    account.meta[PARTIAL_FETCH_KEY] = {"reason": "budget", "posts": TIMELINE_DEPTH_CAP - 50}
    gap_end = NOW - timedelta(days=10)
    # Starts between two hourly posts, so the oldest post returned is after it.
    out = source.fetch_activity(account, BASELINE + timedelta(days=20, minutes=30), gap_end)
    assert len(out) > 50  # Fewer than 3,200 posts in this request, but 3,200 in all.
    assert account.meta["truncated"] is True
    assert account.meta[PARTIAL_FETCH_KEY]["reason"] == "platform_cap"
    assert account.meta[PARTIAL_FETCH_KEY]["posts"] == TIMELINE_DEPTH_CAP - 50 + len(out)


# --------------------------------------------------------- MEDIUM-2: skips


def test_p3_skip_in_a_later_run_keeps_the_earlier_runs_paid_data(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with Storage(db) as storage:
        source, _, _ = make_source(Timeline(CUTOFF, tweets=900), ledger=SpendLedger(storage, "x"))
        run_a = run_scan(source, SEEDS, storage, _settings())
        assert run_a.results[0].cliff
        before = [r.to_row() for r in rescore_run(storage, run_a.run_id).results]
        activities = len(storage.get_activities("x", "a"))
        window = storage.activity_window("x", "a")

    with Storage(db) as storage:
        source, _, _ = make_source(
            Timeline(CUTOFF, tweets=900), ledger=SpendLedger(storage, "x"), min_posts=1000
        )
        run_b = run_scan(source, SEEDS, storage, replace(_settings(), refresh=True))
        assert [r.status for r in run_b.results] == ["skipped"]
        assert run_b.summary["scored"] == 0 and run_b.summary["skipped"] == 1
        # Nothing of run A's was deleted or overwritten.
        assert len(storage.get_activities("x", "a")) == activities
        assert storage.activity_window("x", "a") == window
        account = storage.get_account("x", "a")
        assert account is not None and account.status == "active"
        assert [r.to_row() for r in rescore_run(storage, run_a.run_id).results] == before
        # The skip lives in run B's sample, with no window end for it.
        assert storage.get_run_window_ends(run_b.run_id) == {}
        rescored_b = rescore_run(storage, run_b.run_id)
        assert [r.status for r in rescored_b.results] == ["skipped"]
        assert rescored_b.results[0].baseline_posts == 0


def test_p4_skipped_budget_then_resume_fetches_the_timeline(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with Storage(db) as storage:
        source, _, _ = make_source(
            Timeline(NOW, tweets=900_000), ledger=SpendLedger(storage, "x"), max_usd="6"
        )
        first = run_scan(source, SEEDS, storage, _settings())
        assert [r.status for r in first.results] == ["skipped_budget"]
        assert storage.activity_window("x", "a") is None

    with Storage(db) as storage:
        timeline = Timeline(NOW, tweets=900_000)
        source, _, _ = make_source(timeline, ledger=SpendLedger(storage, "x"), max_usd="500")
        again = run_scan(source, SEEDS, storage, _settings(), from_run=first.run_id)
        assert [r.status for r in again.results] == ["active"]
        assert timeline.calls
        window = storage.activity_window("x", "a")
        assert window is not None and window[0] == BASELINE


# --------------------------------------------------------- LOW-2/3/4 and NITs


def test_plan_spend_refuses_a_discovery_cap_below_one_search_page() -> None:
    def no_requests(request: httpx.Request) -> httpx.Response:
        raise AssertionError("plan_spend must not send requests")

    small, _, _ = make_source(no_requests, max_usd="4")  # 30% = $1.20 < $1.50.
    plan = small.plan_spend(SEEDS, n=10, pool_limit=10, resume=False)
    assert plan.refuse is not None and "worst-case search page ($1.50" in plan.refuse
    assert small.plan_spend(SEEDS, n=10, pool_limit=10, resume=True).refuse is None
    enough, _, _ = make_source(no_requests, max_usd="5")  # 30% = $1.50.
    assert enough.plan_spend(SEEDS, n=10, pool_limit=10, resume=False).refuse is None
    explicit, _, _ = make_source(no_requests, max_usd="100", max_discovery_usd="1")
    assert explicit.plan_spend(SEEDS, n=10, pool_limit=10, resume=False).refuse is not None


def test_summary_table_shows_skipped_and_excludes_them_from_scored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Storage(tmp_path / "c.db") as storage:
        source, _, _ = make_source(
            Timeline(NOW, tweets=5), ledger=SpendLedger(storage, "x"), min_posts=20
        )
        outcome = run_scan(source, SEEDS, storage, _settings())
    capsys.readouterr()
    _print_summary(outcome, "x")
    lines = capsys.readouterr().out.splitlines()
    scored = next(line for line in lines if "Scored " in line and "earlier" not in line)
    skipped = next(line for line in lines if "Skipped (not fetched)" in line)
    assert scored.rstrip(" │|").split()[-1] == "0"
    assert skipped.rstrip(" │|").split()[-1] == "1"


def test_report_suggests_a_complete_resume_command(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with Storage(db) as storage:
        source, _, _ = make_source(
            Timeline(NOW, tweets=5), ledger=SpendLedger(storage, "x"), min_posts=20
        )
        outcome = run_scan(source, SEEDS, storage, _settings())
    out = tmp_path / "out.csv"
    args = ["report", "--run-id", str(outcome.run_id), "--db", str(db), "--min-posts", "3"]
    result = CliRunner().invoke(app, [*args, "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert f"bot-farm-silence-tracker scan --platform x --from-run {outcome.run_id}" in text
    assert f"--db {db.resolve()}" in text
    assert f"--event {EVENT.id}" in text and "--cutoff" not in text
    assert "--min-posts 3" in text and text.count("--force") >= 1


def test_forget_activity_clears_the_requested_until(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        account = make_account("u1")
        storage.upsert_account(account, NOW)
        storage.save_activities(account, [], BASELINE, NOW, NOW, covered_until=CUTOFF)
        assert storage.activity_requested_until("fake", "u1") == NOW
        storage.forget_activity("fake", "u1")
        assert storage.activity_requested_until("fake", "u1") is None
        assert storage.activity_window("fake", "u1") is None


def test_unexplained_403_ids_reset_with_the_streak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/users/p/" in request.url.path:
            return j({"title": "Forbidden"}, 403)
        return j({"data": [], "meta": {"result_count": 0}})

    source, _, _ = make_source(handler)
    created = parse_datetime("2020-01-02T03:04:05Z")
    private = Account("x", "p", "p", created, {"tweet_count": 10})
    with pytest.raises(Exception, match="403"):
        source.fetch_activity(private, BASELINE, NOW)
    assert source._unexplained_ids == ["p"]
    source.fetch_activity(Account("x", "ok", "ok", created, {"tweet_count": 10}), BASELINE, NOW)
    assert source._unexplained_ids == []
    assert source._unexplained_403 == 0
