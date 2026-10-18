"""Round-6 fixes for the X adapter's round-5 review findings: skipped or unreached
accounts keep their paid partial windows on resume, a stale tweet_count no longer
hides posts newer than the cached window, the boundary second of a budget-stopped
refetch keeps its older rows, and scan and report read the same rows."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import PARTIAL_PLATFORM_CAP, Account, Activity
from bot_farm_silence_tracker.pipeline import _plan_reuse, rescore_run, run_scan
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.sources.x import XSource
from bot_farm_silence_tracker.storage import SAMPLE_FETCHED, SAMPLE_PENDING, SAMPLE_SKIPPED, Storage
from bot_farm_silence_tracker.timeutil import isoformat, utcnow

from .helpers import CUTOFF, NOW
from .test_round5 import SEEDS, GlobalCapTimeline, age_cache, every, hourly, settings
from .test_x_source import make_source

SINCE = CUTOFF - timedelta(days=30)
#: Hourly posts in the 30-day baseline window ``[CUTOFF - 30d, NOW)``.
WINDOW_POSTS = 34 * 24


def budget_partial(s: Storage, posts: list, *, max_usd: str, **kw) -> int:
    """Store a sample at no timeline cost, then fetch it until the budget stops it.

    Returns the id of the budget-stopped run. The chain has spent $0.015 on
    discovery, so each extra $0.50 under ``max_usd`` buys one more timeline page.
    """
    src, _, _ = make_source(GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"))
    sampled = run_scan(src, SEEDS, s, settings(), sample_only=True)
    tl = GlobalCapTimeline(posts, tweets=900)
    src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd=max_usd, budget_scope="run")
    a = run_scan(src, SEEDS, s, settings(**kw), from_run=sampled.run_id)
    assert a.status == "partial"
    assert a.results[0].partial_reason == "budget"
    return a.run_id


def test_resume_after_a_skip_fetches_only_the_gap_of_an_old_partial_window(tmp_path) -> None:
    posts = hourly()
    with Storage(tmp_path / "c.db") as s:
        budget_partial(s, posts, max_usd="1.5")
        assert len(s.get_activities("x", "a")) == 200
        # A new run skips the account for the budget; its partial window stays cached.
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900_000),
            ledger=SpendLedger(s, "x"),
            max_usd="6",
            budget_scope="run",
        )
        b = run_scan(src, SEEDS, s, settings())
        assert [st for _, st in s.get_run_sample(b.run_id)] == [SAMPLE_SKIPPED]
        age_cache(s)  # The resume comes more than a day later.
        tl = GlobalCapTimeline(posts, tweets=900_000)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd="500", budget_scope="run")
        c = run_scan(src, SEEDS, s, settings(), from_run=b.run_id)
        # The gap below the 200 paid posts, plus the post at their oldest second.
        assert tl.served == WINDOW_POSTS - 200 + 1
        assert not c.results[0].truncated
        assert len(s.get_activities("x", "a", SINCE, NOW)) == WINDOW_POSTS


def test_legacy_fetched_skip_keeps_the_paid_partial_window(tmp_path) -> None:
    posts = hourly()
    with Storage(tmp_path / "c.db") as s:
        budget_partial(s, posts, max_usd="1.5")
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900_000),
            ledger=SpendLedger(s, "x"),
            max_usd="6",
            budget_scope="run",
        )
        b = run_scan(src, SEEDS, s, settings())
        # How runs made before SAMPLE_SKIPPED recorded the skip.
        s.set_sample_status(b.run_id, s.get_run_sample(b.run_id)[0][0], SAMPLE_FETCHED)
        age_cache(s)
        tl = GlobalCapTimeline(posts, tweets=900_000)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd="500", budget_scope="run")
        run_scan(src, SEEDS, s, settings(), from_run=b.run_id)
        assert tl.served == WINDOW_POSTS - 200 + 1


def test_resume_carries_the_fetch_status_of_an_unreached_account_along_the_chain(
    tmp_path,
) -> None:
    posts = hourly()
    with Storage(tmp_path / "c.db") as s:
        a = budget_partial(s, posts, max_usd="1.5")
        # Run B stops on the budget before it reaches the account: its row stays pending.
        tl = GlobalCapTimeline(posts, tweets=900)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd="1.2", budget_scope="run")
        b = run_scan(src, SEEDS, s, settings(), from_run=a)
        assert not tl.calls
        assert [st for _, st in s.get_run_sample(b.run_id)] == [SAMPLE_PENDING]
        age_cache(s)
        tl = GlobalCapTimeline(posts, tweets=900)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd="500", budget_scope="run")
        run_scan(src, SEEDS, s, settings(), from_run=b.run_id)
        assert tl.served == WINDOW_POSTS - 200 + 1
        assert len(s.get_activities("x", "a", SINCE, NOW)) == WINDOW_POSTS


def test_stale_tweet_count_does_not_hide_newer_posts_from_the_cap(tmp_path) -> None:
    # 900 posts a day. A budget-stopped run caches the newest 1,000 of the 30-day
    # window; a day later 900 more posts push older history past X's 3,200-post cap.
    # The tweet_count in the sample was seen before that fetch, so it says nothing
    # about the newer posts: the cached posting rate stands in for it.
    step = timedelta(seconds=96)
    posts = every(CUTOFF - timedelta(days=31), NOW, step)
    with Storage(tmp_path / "c.db") as s:
        a = budget_partial(s, posts, max_usd="5.4")
        assert len(s.get_activities("x", "a")) == 1000
        later = NOW + timedelta(days=1)
        newer = every(NOW, later, step, prefix="n")
        assert len(newer) == 900
        tl = GlobalCapTimeline(posts + newer, tweets=900)
        src, _, _ = make_source(
            tl, ledger=SpendLedger(s, "x"), max_usd="500", budget_scope="run", now=later
        )
        later_settings = replace(
            settings(),
            scoring=ScoringParams(cutoff=CUTOFF, now=later, baseline_days=30, placebo_weeks=0),
        )
        c = run_scan(src, SEEDS, s, later_settings, from_run=a)
        assert tl.calls, "the gap was fetched"
        r = c.results[0]
        assert r.truncated
        assert r.partial_reason == PARTIAL_PLATFORM_CAP


def test_tweet_count_records_when_it_was_seen() -> None:
    src, _, _ = make_source(GlobalCapTimeline(hourly()))
    before = utcnow().replace(microsecond=0)
    acct = src._account_from_user({"id": "a", "public_metrics": {"tweet_count": 5}})
    assert acct.meta["tweet_count"] == 5
    assert isinstance(acct.meta["tweet_count_at"], str)
    assert acct.meta["tweet_count_at"] >= isoformat(before)
    hydrated = Account("x", "a", "a")
    src.hydrate(hydrated)
    assert isinstance(hydrated.meta["tweet_count"], int)
    assert isinstance(hydrated.meta["tweet_count_at"], str)


def test_newer_posts_known_only_from_a_count_seen_after_the_cached_fetch() -> None:
    fetched = datetime(2026, 10, 11, 12, 0, 0, tzinfo=NOW.tzinfo)
    prior = {
        "cached_until": isoformat(NOW),
        "cached_fetched_at": isoformat(fetched),
        "cached_tweet_count": 50,
        "cached_rate": 240.0,  # Posts per day.
        "until": isoformat(NOW + timedelta(hours=12)),
    }
    acct = Account("x", "a", "h", CUTOFF - timedelta(days=900))
    acct.meta.update(tweet_count=70, tweet_count_at=isoformat(fetched + timedelta(minutes=5)))
    assert XSource._newer_posts(acct, prior) == (20, True)
    # LOW-1: a lower count (deleted posts) says nothing about newer posts.
    acct.meta["tweet_count"] = 40
    assert XSource._newer_posts(acct, prior) == (0, False)
    # Seen before the cached fetch: estimated from the cached rate, flagged unknown.
    acct.meta.update(tweet_count=70, tweet_count_at=isoformat(fetched - timedelta(minutes=5)))
    assert XSource._newer_posts(acct, prior) == (120, False)
    acct.meta.pop("tweet_count_at")
    assert XSource._newer_posts(acct, prior) == (120, False)


def test_budget_stopped_refresh_keeps_the_boundary_second(tmp_path) -> None:
    # 50 posts share one second across the second page's end. A complete window is
    # cached; a --refresh resume stops on the budget after two pages. The older,
    # complete rows keep the boundary second.
    base = hourly()
    tie_ts = sorted(base, key=lambda p: p[1], reverse=True)[180][1]
    posts = base + [(f"tie{i}", tie_ts) for i in range(50)]
    truth = [p for p in posts if SINCE <= p[1] < NOW]
    assert len(truth) == WINDOW_POSTS + 50
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"), budget_scope="run"
        )
        a = run_scan(src, SEEDS, s, settings())
        spent = a.summary["chain_spend_usd"]
        tl = GlobalCapTimeline(posts, tweets=900)
        src, _, _ = make_source(
            tl, ledger=SpendLedger(s, "x"), max_usd=f"{spent + 1.2:.4f}", budget_scope="run"
        )
        b = run_scan(src, SEEDS, s, settings(refresh=True), from_run=a.run_id)
        assert len(tl.calls) == 2
        assert len(s.get_activities("x", "a", SINCE, NOW)) == len(truth)
        assert b.results[0].baseline_posts == rescore_run(s, b.run_id).results[0].baseline_posts


def test_scan_and_report_read_the_same_rows_of_a_reused_capped_window(tmp_path) -> None:
    posts = every(CUTOFF - timedelta(days=35), NOW, timedelta(minutes=10))
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=50_000), ledger=SpendLedger(s, "x"), max_usd="100"
        )
        a = run_scan(src, SEEDS, s, settings())
        assert a.results[0].partial_reason == PARTIAL_PLATFORM_CAP
        window = s.activity_window("x", "a")
        assert window is not None and window[0] > SINCE
        # A stray row below the capped window, left by some other fetch.
        stray = Activity("x", "a", SINCE + timedelta(hours=1), "post")
        s._conn.execute(
            "INSERT INTO activities (platform, user_id, ts, kind, community, content_hash,"
            " fetched_at) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
            ("x", "a", isoformat(stray.ts), stray.kind, isoformat(utcnow())),
        )
        s._conn.commit()
        tl = GlobalCapTimeline(posts, tweets=50_000)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd="100")
        b = run_scan(src, SEEDS, s, settings())
        assert not [r for r in tl.calls if r.url.path.endswith("/tweets")]
        scanned = b.results[0].baseline_posts
        assert scanned == rescore_run(s, b.run_id).results[0].baseline_posts


def test_window_starting_at_or_after_until_is_fetched_in_full(tmp_path) -> None:
    with Storage(tmp_path / "c.db") as s:
        acct = Account("x", "a", "h", CUTOFF - timedelta(days=900))
        now = utcnow()
        s.upsert_account(acct, now)
        start = NOW + timedelta(days=1)
        acts = [Activity("x", "a", start + timedelta(hours=1), "post")]
        s.save_activities(acct, acts, start, NOW + timedelta(days=2), now)
        ttl = timedelta(hours=24)
        for prior in (set(), {"a"}):
            reuse, gaps = _plan_reuse(s, [acct], prior, settings(), SINCE, NOW, ttl, errored=set())
            assert reuse == [False]
            assert gaps == {}


@pytest.mark.parametrize("nties", [3, 5, 8])
def test_posts_tied_at_the_old_window_end_survive_a_budget_stopped_refetch(
    tmp_path, nties: int
) -> None:
    """A budget stop at the old window end can split the tied posts across a page
    boundary. The run is partial and the resume refetches the boundary second, so
    every tied post ends up cached."""
    later = NOW + timedelta(hours=97)
    base = every(SINCE, NOW, timedelta(hours=1))
    ties = [(f"tie{i}", NOW) for i in range(nties)]
    newer = every(NOW + timedelta(hours=1), later, timedelta(hours=1), prefix="n")
    posts = base + ties + newer + [("extra", NOW)]
    truth = [p for p in posts if SINCE <= p[1] < later]
    assert len(truth) == 913 + nties
    ls = replace(
        settings(refresh=True),
        scoring=ScoringParams(cutoff=CUTOFF, now=later, baseline_days=30, placebo_weeks=0),
    )
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"), budget_scope="run"
        )
        a = run_scan(src, SEEDS, s, settings())
        assert a.status == "complete"
        spent = a.summary["chain_spend_usd"]
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900),
            ledger=SpendLedger(s, "x"),
            max_usd=f"{spent + 0.7:.4f}",
            budget_scope="run",
            now=later,
        )
        b = run_scan(src, SEEDS, s, ls, from_run=a.run_id)
        assert b.results[0].truncated is True
        assert b.results[0].partial_reason == "budget"
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900),
            ledger=SpendLedger(s, "x"),
            max_usd="500",
            budget_scope="run",
            now=later,
        )
        c = run_scan(src, SEEDS, s, ls, from_run=b.run_id)
        assert c.results[0].truncated is False
        got = s.get_activities("x", "a", SINCE, later)
        assert len(got) == len(truth)
        assert len([x for x in got if x.ts == NOW]) == nties + 1


def test_resuming_from_a_run_that_is_not_the_newest_warns(tmp_path) -> None:
    posts = hourly()
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"))
        a = run_scan(src, SEEDS, s, settings())
        src, _, _ = make_source(GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"))
        b = run_scan(src, SEEDS, s, settings(), from_run=a.run_id)
        src, _, _ = make_source(GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"))
        c = run_scan(src, SEEDS, s, settings(), from_run=a.run_id)
        text = f"Run {a.run_id} is not the newest run of its chain; newer runs: {b.run_id}"
        assert any(text in w for w in c.summary["warnings"])
