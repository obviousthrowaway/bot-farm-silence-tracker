"""Round-5 fixes for the X adapter's round-4 review findings, rebuilt from the critic's
probes: the whole timeline counts toward X's post cap, skipped accounts are fetched
on resume, re-scoring reads only the scored window, and a budget-stopped refetch
keeps the rows below it."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import httpx

from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import PARTIAL_FETCH_KEY, PARTIAL_PLATFORM_CAP, Account
from bot_farm_silence_tracker.pipeline import rescore_run, run_scan
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.x import TIMELINE_CAP_MARGIN, TIMELINE_DEPTH_CAP, XSource
from bot_farm_silence_tracker.storage import SAMPLE_FETCHED, SAMPLE_SKIPPED, Storage
from bot_farm_silence_tracker.timeutil import isoformat, parse_datetime, utcnow

from .helpers import CUTOFF, NOW
from .test_x_source import SEARCH, j, make_source, search_page, tweet, user, x_settings

SEEDS = SeedSet("x", ("q",))


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def every(start: datetime, end: datetime, step: timedelta, prefix: str = "p") -> list:
    out = []
    t = start
    while t < end:
        out.append((f"{prefix}{len(out)}", t))
        t += step
    return out


class GlobalCapTimeline:
    """X-like: only the newest ``cap`` posts overall are visible; ``end_time`` is exclusive."""

    def __init__(self, posts, *, cap=TIMELINE_DEPTH_CAP, tweets=2000, fail=frozenset(), gone=False):
        self.posts = sorted(posts, key=lambda p: p[1], reverse=True)  # (id, ts)
        self.cap = cap
        self.tweets = tweets
        self.fail = fail
        self.gone = gone
        self.calls: list[httpx.Request] = []
        self.served = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            body = search_page(["a"], None)
            body["includes"]["users"] = [user("a", tweets=self.tweets)]
            return j(body)
        if not request.url.path.endswith("/tweets"):
            return j({"data": [user("a", tweets=self.tweets)]})
        n = len(self.calls)
        self.calls.append(request)
        if self.gone:
            return j({"title": "Not Found Error", "detail": "no"}, 404)
        if n in self.fail:
            return j({"title": "boom"}, 500)
        start = parse_datetime(request.url.params["start_time"])
        end = parse_datetime(request.url.params["end_time"])
        visible = self.posts[: self.cap]
        chosen = [p for p in visible if start <= p[1] < end]
        off = int(request.url.params.get("pagination_token") or 0)
        page = chosen[off : off + 100]
        meta: dict[str, Any] = {"result_count": len(page)}
        if off + 100 < len(chosen):
            meta["next_token"] = str(off + 100)
        self.served += len(page)
        return j({"data": [tweet(pid, iso(ts)) for pid, ts in page], "meta": meta})


def settings(baseline_days: int = 30, **kw: Any):
    # placebo_weeks=0: these tests pin the cache's coverage of the baseline window
    # itself; the placebo extension of the fetch window is tested in test_controls.
    scoring = ScoringParams(cutoff=CUTOFF, now=NOW, baseline_days=baseline_days, placebo_weeks=0)
    return replace(x_settings(n=1), scoring=scoring, **kw)


def hourly() -> list:
    return every(CUTOFF - timedelta(days=35), NOW, timedelta(hours=1))


def age_cache(s: Storage) -> None:
    s._conn.execute("UPDATE accounts SET activity_fetched_at = '2026-01-01T00:00:00Z'")
    s._conn.commit()


def test_same_second_page_boundary(tmp_path) -> None:
    # Hourly posts, plus 50 posts at the same second straddling a page boundary.
    base = hourly()
    tie_ts = sorted(base, key=lambda p: p[1], reverse=True)[180][1]
    posts = base + [(f"tie{i}", tie_ts) for i in range(50)]
    db = tmp_path / "c.db"
    with Storage(db) as s:
        tl = GlobalCapTimeline(posts, fail=frozenset(range(2, 99)))
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"))
        first = run_scan(src, SEEDS, s, settings())
    with Storage(db) as s:
        src, _, _ = make_source(GlobalCapTimeline(posts), ledger=SpendLedger(s, "x"))
        run_scan(src, SEEDS, s, settings(), from_run=first.run_id)
        since = CUTOFF - timedelta(days=30)
        truth = [p for p in posts if since <= p[1] < NOW]
        assert len(s.get_activities("x", "a")) == len(truth)


def test_baseline_extended_gap_hits_platform_cap(tmp_path) -> None:
    # 72 posts a day. A 30-day run caches a complete 34-day window (2,448 posts);
    # a 60-day run then fetches only the gap, which X cuts at 3,200 posts in all.
    posts = every(CUTOFF - timedelta(days=70), NOW, timedelta(minutes=20))
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=50_000), ledger=SpendLedger(s, "x"), max_usd="100"
        )
        a = run_scan(src, SEEDS, s, settings(30))
        assert not a.results[0].truncated
        tl = GlobalCapTimeline(posts, tweets=50_000)
        src, _, _ = make_source(
            tl,
            ledger=SpendLedger(s, "x"),
            max_usd="100",
            baseline_start=CUTOFF - timedelta(days=60),
        )
        b = run_scan(src, SEEDS, s, settings(60))
        r = b.results[0]
        assert tl.calls, "the gap was fetched"
        assert r.truncated
        assert r.partial_reason == PARTIAL_PLATFORM_CAP
        window = s.activity_window("x", "a")
        assert window is not None and window[0] > CUTOFF - timedelta(days=60)
        assert len(s.get_activities("x", "a")) == TIMELINE_DEPTH_CAP


def test_gap_fetch_counts_posts_newer_than_the_cached_window(tmp_path, monkeypatch) -> None:
    # After the 30-day run, 500 more posts appear after its window end (tweet_count
    # rises by 500). They count toward X's cap too, so the gap holds only 252.
    posts = every(CUTOFF - timedelta(days=70), NOW, timedelta(minutes=20))
    newer = every(NOW, NOW + timedelta(days=1), timedelta(minutes=1), prefix="n")[:500]
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=50_000), ledger=SpendLedger(s, "x"), max_usd="100"
        )
        run_scan(src, SEEDS, s, settings(30))
        # The 60-day run sees the new count a minute after the cached window's fetch.
        later = utcnow() + timedelta(minutes=1)
        monkeypatch.setattr("bot_farm_silence_tracker.sources.x.utcnow", lambda: later)
        tl = GlobalCapTimeline(posts + newer, tweets=50_500)
        src, _, _ = make_source(
            tl,
            ledger=SpendLedger(s, "x"),
            max_usd="100",
            baseline_start=CUTOFF - timedelta(days=60),
        )
        b = run_scan(src, SEEDS, s, settings(60))
        # Plus the post at the cached window's first second (GAP_OVERLAP).
        assert tl.served == TIMELINE_DEPTH_CAP - 500 - 2448 + 1
        assert b.results[0].truncated
        assert b.results[0].partial_reason == PARTIAL_PLATFORM_CAP


def test_gap_fetch_without_tweet_count_uses_the_margin() -> None:
    since, until = CUTOFF - timedelta(days=60), CUTOFF - timedelta(days=30)
    acct = Account("x", "a", "h", CUTOFF - timedelta(days=900))
    earlier = TIMELINE_DEPTH_CAP - TIMELINE_CAP_MARGIN - 10
    acct.meta[PARTIAL_FETCH_KEY] = {"posts": earlier, "cached_until": isoformat(NOW)}
    stamps = [until - timedelta(hours=h + 1) for h in range(10)]

    def handler(request: httpx.Request) -> httpx.Response:
        data = [tweet(f"t{i}", iso(ts)) for i, ts in enumerate(stamps)]
        return j({"data": data, "meta": {"result_count": 10}})

    source, _, _ = make_source(handler)
    source.fetch_activity(acct, since, until)
    assert acct.meta["truncated"] is True
    assert acct.meta[PARTIAL_FETCH_KEY]["reason"] == PARTIAL_PLATFORM_CAP
    assert XSource._newer_posts(acct, {"cached_until": "x"}) == (0, False)
    acct.meta.update(tweet_count=70, tweet_count_at=isoformat(NOW))
    known = {
        "cached_until": "x",
        "cached_tweet_count": 50,
        "cached_fetched_at": isoformat(NOW - timedelta(minutes=1)),
    }
    assert XSource._newer_posts(acct, known) == (20, True)
    assert XSource._newer_posts(acct, {"posts": 5}) == (0, True)


def test_skipped_budget_account_is_fetched_on_resume(tmp_path) -> None:
    posts = hourly()
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"))
        run_scan(src, SEEDS, s, settings())
        age_cache(s)
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900_000),
            ledger=SpendLedger(s, "x"),
            max_usd="6",
            budget_scope="run",
        )
        b = run_scan(src, SEEDS, s, settings())
        assert [r.status for r in b.results] == ["skipped_budget"]
        assert [st for _, st in s.get_run_sample(b.run_id)] == [SAMPLE_SKIPPED]
        assert [r.status for r in rescore_run(s, b.run_id).results] == ["skipped_budget"]
        tl = GlobalCapTimeline(posts, tweets=900_000)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd="500", budget_scope="run")
        c = run_scan(src, SEEDS, s, settings(), from_run=b.run_id)
        assert tl.calls, "skipped_budget account was not fetched on resume"
        assert [r.status for r in c.results] == ["active"]


def test_skip_recorded_as_fetched_by_older_runs_is_fetched_on_resume(tmp_path) -> None:
    posts = hourly()
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"))
        run_scan(src, SEEDS, s, settings())
        age_cache(s)
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900_000),
            ledger=SpendLedger(s, "x"),
            max_usd="6",
            budget_scope="run",
        )
        b = run_scan(src, SEEDS, s, settings())
        # How runs made before SAMPLE_SKIPPED recorded the skip.
        account = s.get_run_sample(b.run_id)[0][0]
        s.set_sample_status(b.run_id, account, SAMPLE_FETCHED)
        assert [r.status for r in rescore_run(s, b.run_id).results] == ["skipped_budget"]
        tl = GlobalCapTimeline(posts, tweets=900_000)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd="500", budget_scope="run")
        run_scan(src, SEEDS, s, settings(), from_run=b.run_id)
        assert tl.calls


def test_rescore_reads_only_the_window_the_run_scored(tmp_path) -> None:
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(GlobalCapTimeline(hourly()), ledger=SpendLedger(s, "x"))
        a = run_scan(src, SEEDS, s, settings())
        assert a.results[0].posts_after_cutoff == 96
        # As if the run scored a window ending a day early and a later run merged
        # the last day into the shared cache.
        s.set_sample_window_ends(a.run_id, [("x", "a", NOW - timedelta(days=1))])
        assert rescore_run(s, a.run_id).results[0].posts_after_cutoff == 72


def test_reused_partial_window_covering_since_is_not_truncated(tmp_path) -> None:
    # A 60-day run is cut by X's cap about 40 days back; a 30-day run reuses it.
    posts = every(CUTOFF - timedelta(days=70), NOW, timedelta(minutes=20))
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=50_000),
            ledger=SpendLedger(s, "x"),
            max_usd="100",
            baseline_start=CUTOFF - timedelta(days=60),
        )
        a = run_scan(src, SEEDS, s, settings(60))
        assert a.results[0].truncated
        tl = GlobalCapTimeline(posts, tweets=50_000)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), max_usd="100")
        b = run_scan(src, SEEDS, s, settings(30))
        assert not tl.calls
        assert not b.results[0].truncated
        assert b.results[0].partial_reason is None
        cached = s.get_account("x", "a")
        assert cached is not None and cached.meta["truncated"] is True  # Stored row kept.


def test_unavailable_on_gap_fetch_clears_truncated(tmp_path) -> None:
    posts = hourly()
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(
            GlobalCapTimeline(posts, fail=frozenset(range(2, 99))), ledger=SpendLedger(s, "x")
        )
        a = run_scan(src, SEEDS, s, settings())
        cached = s.get_account("x", "a")
        assert cached is not None and cached.meta["truncated"] is True
        tl = GlobalCapTimeline(posts, gone=True)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"))
        b = run_scan(src, SEEDS, s, settings(), from_run=a.run_id)
        assert tl.calls
        assert b.results[0].status == "deleted"
        assert not b.results[0].truncated
        cached = s.get_account("x", "a")
        assert cached is not None and not cached.meta.get("truncated")


def test_full_refetch_stopped_by_budget_keeps_rows_below(tmp_path) -> None:
    # A complete cached window is refetched in full; the budget stops it after two
    # pages. The older rows still cached below are kept, not bought again.
    posts = hourly()
    since = CUTOFF - timedelta(days=30)
    truth = [p for p in posts if since <= p[1] < NOW]
    with Storage(tmp_path / "c.db") as s:
        src, _, _ = make_source(
            GlobalCapTimeline(posts, tweets=900), ledger=SpendLedger(s, "x"), budget_scope="run"
        )
        a = run_scan(src, SEEDS, s, settings())
        spent = a.summary["chain_spend_usd"]
        tl = GlobalCapTimeline(posts, tweets=900)
        src, _, _ = make_source(
            tl,
            ledger=SpendLedger(s, "x"),
            max_usd=f"{spent + 1.2:.4f}",
            budget_scope="run",
        )
        b = run_scan(src, SEEDS, s, settings(refresh=True), from_run=a.run_id)
        assert b.status == "partial"
        assert len(tl.calls) == 2
        window = s.activity_window("x", "a")
        assert window is not None and window[0] == since
        assert not b.results[0].truncated
        assert len(s.get_activities("x", "a", since, NOW)) == len(truth)
        tl = GlobalCapTimeline(posts, tweets=900)
        src, _, _ = make_source(tl, ledger=SpendLedger(s, "x"), budget_scope="run")
        run_scan(src, SEEDS, s, settings(), from_run=b.run_id)
        assert not tl.calls
