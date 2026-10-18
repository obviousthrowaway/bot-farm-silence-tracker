"""Reddit archive adapter, against a scripted fake of the Arctic Shift API (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.models import Account, content_hash
from bot_farm_silence_tracker.seeds import SeedSet, load_seeds
from bot_farm_silence_tracker.sources.base import (
    BudgetExceeded,
    RateLimited,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import PROJECT_URL, HttpClient, research_user_agent
from bot_farm_silence_tracker.sources.reddit_archive import (
    COMMENTS_PATH,
    MAX_SLOW_QUERY_WAIT,
    USER_AGENT,
    RedditArchiveSource,
    SlowQueryError,
    is_skipped_author,
)
from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY, load_source_class

from .helpers import CUTOFF, NOW, MidRandom

runner = CliRunner()
TIMEOUT = {"error": "Timeout. Maybe slow down a bit", "data": None}
LOAD = {"error": "Please slow down", "data": None}
AUTO_PAGE = 100  # What the fake returns for limit=auto.
OK_EMPTY = httpx.Response(200, json={"data": []})


def ts(when: datetime) -> int:
    return int(when.timestamp())


def comment(i: str, author: str, sub: str, when: datetime, body: str = "hello") -> dict[str, Any]:
    return {
        "kind": "comments", "id": i, "author": author, "subreddit": sub,
        "created_utc": ts(when), "body": body,
    }  # fmt: skip


def post(i: str, author: str, sub: str, when: datetime, title: str = "t", text: str = "") -> dict:
    return {
        "kind": "posts", "id": i, "author": author, "subreddit": sub,
        "created_utc": ts(when), "title": title, "selftext": text,
    }  # fmt: skip


def slow(body: dict[str, Any] = TIMEOUT, reset: str | None = None) -> httpx.Response:
    headers = {"X-RateLimit-Reset": reset} if reset is not None else {}
    return httpx.Response(422, json=body, headers=headers)


class Archive:
    """A fake Arctic Shift: filters, sorts and pages like the real search endpoints.

    ``before`` and ``after`` are both exclusive, as on the live service. Only the
    requested ``fields`` are returned. ``script`` is a list of canned responses served
    before the normal behavior, and ``hook(params)`` may return a response to override
    the normal behavior.
    """

    def __init__(
        self,
        items: list[dict[str, Any]],
        script: list[httpx.Response] | None = None,
        hook: Callable[[dict[str, str]], httpx.Response | None] | None = None,
    ):
        self.items = items
        self.script = list(script or [])
        self.hook = hook
        self.requests: list[httpx.Request] = []

    def params(self, index: int) -> dict[str, str]:
        return dict(self.requests[index].url.params)

    def author_params(self) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests if "author" in r.url.params]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        q = dict(request.url.params)
        if self.hook is not None:
            override = self.hook(q)
            if override is not None:
                return override
        kind = "posts" if request.url.path.endswith("/posts/search") else "comments"
        rows = [i for i in self.items if i["kind"] == kind]
        if "author" in q:
            rows = [r for r in rows if r["author"].casefold() == q["author"].casefold()]
        if "subreddit" in q:
            rows = [r for r in rows if r["subreddit"].casefold() == q["subreddit"].casefold()]
        if "after" in q:
            rows = [r for r in rows if r["created_utc"] > int(q["after"])]
        if "before" in q:
            rows = [r for r in rows if r["created_utc"] < int(q["before"])]
        rows.sort(key=lambda r: r["created_utc"], reverse=q.get("sort") == "desc")
        limit = AUTO_PAGE if q.get("limit") == "auto" else min(int(q.get("limit", 25)), 100)
        fields = q["fields"].split(",") if "fields" in q else list(rows[0]) if rows else []
        data = [{f: r[f] for f in fields if f in r} for r in rows[:limit]]
        return httpx.Response(200, json={"data": data}, headers={"X-RateLimit-Reset": "30"})


def make_source(
    archive: Callable[[httpx.Request], httpx.Response],
    *,
    baseline_days: int = 2,
    seed: int | None = 1,
    now: datetime = NOW,
    cutoff: datetime = CUTOFF,
    max_requests: int | None = None,
    **options: object,
) -> tuple[RedditArchiveSource, list[float], list[float]]:
    """Return the source, the adapter's sleeps and the HTTP client's sleeps.

    The coverage check is off unless a test passes ``coverage_threshold``; the tests
    for it build worldnews traffic of their own.
    """
    options.setdefault("coverage_threshold", "0")
    adapter_sleeps: list[float] = []
    http_sleeps: list[float] = []
    http = HttpClient(
        requests_per_second=RedditArchiveSource.default_rps,
        burst=RedditArchiveSource.default_burst,
        transport=httpx.MockTransport(archive),
        sleep=http_sleeps.append,
        clock=lambda: 0.0,
        max_requests=max_requests,
    )
    config = SourceConfig(
        http=http,
        credentials={},
        now=now,
        cutoff=cutoff,
        seed=seed,
        options=options,
        baseline_start=cutoff - timedelta(days=baseline_days),
    )
    source = RedditArchiveSource(config, sleep=adapter_sleeps.append, rng=MidRandom())
    return source, adapter_sleeps, http_sleeps


def pulse(sub: str = "worldnews") -> dict[str, Any]:
    """A fresh comment so the archive does not look stale."""
    return comment(f"pulse-{sub}", "someone", sub, NOW - timedelta(hours=1))


def post_pulse(sub: str = "worldnews", ago: timedelta = timedelta(hours=1)) -> dict[str, Any]:
    """A fresh post, so the post horizon is readable too."""
    return post(f"post-pulse-{sub}", "someone", sub, NOW - ago)


def traffic(
    start: datetime,
    end: datetime,
    every: timedelta = timedelta(minutes=10),
    sub: str = "worldnews",
    kind: str = "comments",
) -> list[dict[str, Any]]:
    """Steady background comments (or posts) by ``[deleted]``, which discovery skips.

    They give the coverage check a baseline without adding accounts to the pool.
    """
    out: list[dict[str, Any]] = []
    moment, n = start, 0
    make = post if kind == "posts" else comment
    while moment < end:
        out.append(make(f"bg-{kind}-{sub}-{n}", "[deleted]", sub, moment))
        moment += every
        n += 1
    return out


def fetch(src: RedditArchiveSource, handle: str = "alice") -> tuple[Account, list[Any]]:
    account = Account("reddit-archive", handle, handle)
    return account, src.fetch_activity(account, CUTOFF - timedelta(days=30), NOW)


# -- basics ---------------------------------------------------------------------------


def test_registered_and_credential_free() -> None:
    assert SOURCE_REGISTRY["reddit-archive"].endswith("RedditArchiveSource")
    cls = load_source_class("reddit-archive")
    assert cls.name == "reddit-archive"
    assert cls.required_credentials == () and cls.optional_credentials == ()
    assert cls.default_rps <= 0.5 and cls.default_burst == 1.0
    assert "research" in USER_AGENT
    assert research_user_agent(PROJECT_URL) == USER_AGENT


def test_reddit_seeds_are_reused() -> None:
    assert load_seeds("reddit-archive").items == load_seeds("reddit").items
    assert load_seeds("reddit-archive", override=["r/Foo", "foo", "bar"]).items == ("Foo", "bar")


@pytest.mark.parametrize(
    ("name", "skipped"),
    [
        ("[deleted]", True), ("AutoModerator", True), ("RemindMeBot", True),
        ("sneaky_BOT", True), ("my-bot", True), ("bot", True), ("helperbot", True),
        (None, True), ("", True), ("alice", False), ("bottle", False),
        ("Talbot", False), ("jane_cabot", False), ("FriendlyRobot", False), ("abbot", False),
        ("jane_talbot_bot", True), ("robot-bot", True),
    ],
)  # fmt: skip
def test_skipped_authors(name: str | None, skipped: bool) -> None:
    assert is_skipped_author(name) is skipped


@pytest.mark.parametrize(
    "options",
    [
        {"page_size": "0"}, {"page_size": "101"}, {"page_size": "-5"},
        {"max_items_per_kind": "0"}, {"max_slowdown_retries": "-1"}, {"slowdown_wait": "-1"},
        {"max_slow_queries": "0"}, {"coverage_threshold": "1"},
        {"coverage_threshold": "-0.1"}, {"coverage_samples_per_day": "0"},
        {"coverage_samples_per_day": "25"},
    ],
)  # fmt: skip
def test_invalid_options_are_rejected(options: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="option"):
        make_source(Archive([]), **options)


def test_requests_send_the_honest_user_agent_and_only_needed_fields() -> None:
    archive = Archive([pulse(), comment("1", "alice", "worldnews", CUTOFF - timedelta(hours=1))])
    src, _, _ = make_source(archive, baseline_days=1)
    list(src.discover(SeedSet("reddit-archive", ("worldnews",)), 5))
    assert {r.headers["user-agent"] for r in archive.requests} == {USER_AGENT}
    fields = {archive.params(i).get("fields") for i in range(len(archive.requests))}
    assert fields == {"id,author,created_utc"}


# -- discover -------------------------------------------------------------------------


def test_discover_filters_dedups_and_skips_bots() -> None:
    inside = CUTOFF - timedelta(hours=5)
    items = [
        comment("1", "alice", "a", inside),
        comment("2", "Alice", "b", inside - timedelta(hours=1)),  # Same person, other case.
        comment("3", "bob", "b", inside - timedelta(hours=2)),
        comment("4", "[deleted]", "a", inside),
        comment("5", "AutoModerator", "a", inside),
        comment("6", "helperbot", "a", inside),
        post("7", "carol", "a", inside),
        comment("8", "late_only", "a", CUTOFF + timedelta(hours=3)),  # After the cutoff.
        comment("9", "at_cutoff", "a", CUTOFF),  # Exactly at the cutoff is not before it.
        comment("10", "too_old", "a", CUTOFF - timedelta(days=5)),  # Before the baseline.
        pulse("a"),
    ]
    archive = Archive(items)
    src, _, _ = make_source(archive, baseline_days=1, dry_pages=40)
    accounts = list(src.discover(SeedSet("reddit-archive", ("a", "b")), 50))
    names = sorted(a.handle.casefold() for a in accounts)
    assert names == ["alice", "bob", "carol"]
    assert all(a.platform == "reddit-archive" and a.created_at is None for a in accounts)
    assert {a.meta["community"] for a in accounts} <= {"a", "b"}
    start = ts(CUTOFF - timedelta(days=1))
    pages = [p for p in map(archive.params, range(len(archive.requests))) if "after" in p]
    assert pages
    assert all(start - 1 <= int(p["after"]) < int(p["before"]) <= ts(CUTOFF) for p in pages)


def spread_world(subs: tuple[str, ...], days: int = 10, per_hour: int = 2) -> list[dict]:
    """Every sub has distinct authors posting around the clock for ``days`` days."""
    items: list[dict[str, Any]] = [pulse(s) for s in subs]
    for sub in subs:
        for h in range(days * 24):
            for k in range(per_hour):
                when = CUTOFF - timedelta(hours=h, minutes=30 * k + 1)
                items.append(comment(f"{sub}-{h}-{k}", f"{sub}_u{h}_{k}", sub, when))
    return items


def test_discovery_at_n30_spreads_over_seeds_and_hours() -> None:
    subs = ("a", "b", "c", "d", "e")
    archive = Archive(spread_world(subs))
    src, _, _ = make_source(archive, baseline_days=10, page_size=20)
    accounts = list(src.discover(SeedSet("reddit-archive", subs), 30))
    assert len(accounts) == 30
    per_sub: dict[str, int] = {}
    for a in accounts:
        per_sub[a.meta["community"]] = per_sub.get(a.meta["community"], 0) + 1
    assert len(per_sub) > 2
    assert max(per_sub.values()) <= 6  # ceil(30 / 5)
    # Authors are named after the hour they posted in; the sample covers many of them.
    hours = {int(a.handle.split("_u")[1].split("_")[0]) for a in accounts}
    assert len(hours) >= 10
    befores = [int(p["before"]) for p in map(archive.params, range(len(archive.requests)))
               if "after" in p]  # fmt: skip
    assert len({(b // 3600) % 24 for b in befores}) >= 4  # Pages start at varied hours.
    assert all(ts(CUTOFF - timedelta(days=10)) < b <= ts(CUTOFF) for b in befores)


def test_discovery_shuffles_the_seed_order_each_round() -> None:
    subs = ("a", "b", "c", "d", "e")
    archive = Archive(spread_world(subs, days=3))
    src, _, _ = make_source(archive, baseline_days=3, page_size=5)
    list(src.discover(SeedSet("reddit-archive", subs), 100))
    order = [p["subreddit"] for p in map(archive.params, range(len(archive.requests)))
             if "after" in p]  # fmt: skip
    rounds = [tuple(order[i : i + 5]) for i in range(0, 20, 5)]
    assert all(sorted(r) == list(subs) for r in rounds)  # Every seed once per round.
    assert len(set(rounds)) > 1  # The order changes between rounds.


def test_discovery_cap_is_relaxed_when_other_seeds_run_dry() -> None:
    tiny = [pulse("tiny"), comment("t1", "tiny_user", "tiny", CUTOFF - timedelta(hours=3))]
    items = [*spread_world(("big",), days=2), *tiny]
    src, _, _ = make_source(Archive(items), baseline_days=2, page_size=20, dry_pages=3)
    accounts = list(src.discover(SeedSet("reddit-archive", ("big", "tiny")), 20))
    assert len(accounts) == 20  # cap is 10 per seed, but "tiny" has only 0 or 1 authors.
    assert sum(a.meta["community"] == "big" for a in accounts) >= 19


def test_discover_stops_at_pool_limit_without_reading_everything() -> None:
    items = [pulse("a")] + [
        comment(str(i), f"user{i}", "a", CUTOFF - timedelta(minutes=10 * i + 1)) for i in range(40)
    ]
    archive = Archive(items)
    src, _, _ = make_source(archive, baseline_days=1, page_size=10, dry_pages=50)
    accounts = list(src.discover(SeedSet("reddit-archive", ("a",)), 7))
    assert len(accounts) == 7
    assert len(archive.requests) <= 30


def test_discover_drops_a_failing_seed_and_keeps_the_others() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("subreddit") == "broken" and "after" in q:
            return httpx.Response(400, json={"error": "bad subreddit"})
        return None

    items = [pulse("good"), comment("1", "alice", "good", CUTOFF - timedelta(hours=2))]
    archive = Archive(items, hook=hook)
    src, _, _ = make_source(archive, baseline_days=1, dry_pages=30)
    accounts = list(src.discover(SeedSet("reddit-archive", ("good", "broken")), 10))
    assert [a.handle for a in accounts] == ["alice"]
    warnings = " ".join(src.drain_warnings())
    assert "r/broken was dropped from discovery" in warnings
    assert "r/broken returned no archived items" not in warnings
    broken_pages = [p for p in archive.author_params() if p.get("subreddit") == "broken"]
    assert broken_pages == []


def load_hook(
    sub: str, fail_first: int | None
) -> Callable[[dict[str, str]], httpx.Response | None]:
    """422 'slow down' for the first ``fail_first`` discovery requests of ``sub`` (all if None)."""
    seen = {"n": 0}

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("subreddit") == sub and "after" in q:
            seen["n"] += 1
            if fail_first is None or seen["n"] <= fail_first:
                return slow(LOAD)
        return None

    return hook


def test_a_seed_gets_two_slow_queries_before_it_is_dropped() -> None:
    items = [pulse("good")] + [
        comment(str(i), f"user{i}", "good", CUTOFF - timedelta(hours=i + 1)) for i in range(10)
    ]
    items.append(comment("s1", "slowpoke", "s", CUTOFF - timedelta(hours=1)))
    # Two queries fail (two requests each), the third works: the seed survives.
    archive = Archive(items, hook=load_hook("s", 4))
    src, _, _ = make_source(archive, baseline_days=1, dry_pages=30, slowdown_wait=0)
    accounts = list(src.discover(SeedSet("reddit-archive", ("good", "s")), 11))
    assert "slowpoke" in {a.handle for a in accounts}
    assert "dropped from discovery" not in " ".join(src.drain_warnings())


def test_a_seed_is_dropped_on_its_third_slow_query() -> None:
    items = [pulse("good")] + [
        comment(str(i), f"user{i}", "good", CUTOFF - timedelta(hours=i + 1)) for i in range(10)
    ]
    archive = Archive(items, hook=load_hook("s", None))
    src, _, _ = make_source(archive, baseline_days=1, dry_pages=30, slowdown_wait=0)
    list(src.discover(SeedSet("reddit-archive", ("good", "s")), 10))
    assert "r/s was dropped from discovery after 3 slow" in " ".join(src.drain_warnings())
    s_requests = [
        r for r in archive.requests
        if r.url.params.get("subreddit") == "s" and "after" in r.url.params
    ]  # fmt: skip
    assert len(s_requests) == 6  # Three queries, each asked once and retried once.


def test_the_cap_is_lifted_once_the_yield_collapses() -> None:
    # "rich" has plenty of authors; "echo" has one author posting all the time, so its
    # pages hold rows but never a new author. With cap 5, "rich" stops at five until
    # the yield of the last three pages is under two, which lifts the cap long before
    # "echo" would be done after 50 dry pages.
    items = [pulse("rich"), pulse("echo")]
    items += [
        comment(f"r{i}", f"rich_u{i}", "rich", CUTOFF - timedelta(minutes=30 * i + 1))
        for i in range(40)
    ]
    items += [
        comment(f"e{i}", "echo_user", "echo", CUTOFF - timedelta(minutes=30 * i + 1))
        for i in range(48)
    ]
    archive = Archive(items)
    src, _, _ = make_source(archive, baseline_days=1, page_size=10, dry_pages=50, seed=2)
    accounts = list(src.discover(SeedSet("reddit-archive", ("rich", "echo")), 10))
    assert len(accounts) == 10
    assert sum(a.meta["community"] == "rich" for a in accounts) > 5  # Over the cap of 5.
    echo_pages = [
        r for r in archive.requests
        if r.url.params.get("subreddit") == "echo" and "after" in r.url.params
    ]  # fmt: skip
    assert len(echo_pages) < 20


def test_discover_raises_rate_limited_when_every_seed_times_out() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if "after" in q else None

    archive = Archive([pulse("a")], hook=hook)
    src, _, _ = make_source(archive, slowdown_wait=0)
    with pytest.raises(RateLimited):
        list(src.discover(SeedSet("reddit-archive", ("a", "b", "c", "d")), 10))


def test_discover_warns_about_empty_seeds_and_short_pools() -> None:
    items = [pulse("a"), comment("1", "alice", "a", CUTOFF - timedelta(hours=20))]
    src, _, _ = make_source(Archive(items), baseline_days=1, dry_pages=30)
    accounts = list(src.discover(SeedSet("reddit-archive", ("a", "ghost")), 10))
    assert [a.handle for a in accounts] == ["alice"]
    warnings = " ".join(src.drain_warnings())
    assert "r/ghost" in warnings and "r/a " not in warnings
    assert "only 1 distinct" in warnings


# -- hydrate --------------------------------------------------------------------------


def test_discovery_reaches_the_first_second_of_the_baseline() -> None:
    """`after` is exclusive, so a page must ask for after = baseline_start - 1."""
    start = CUTOFF - timedelta(days=1)
    items = [pulse("a"), comment("edge", "edge_user", "a", start)]
    src, _, _ = make_source(Archive(items), baseline_days=1, dry_pages=3)
    accounts = list(src.discover(SeedSet("reddit-archive", ("a",)), 5))
    assert [a.handle for a in accounts] == ["edge_user"]


def test_hydrate_is_a_no_request_noop_and_warns_only_while_sampling() -> None:
    archive = Archive([pulse("a"), comment("1", "alice", "a", CUTOFF - timedelta(hours=2))])
    src, _, _ = make_source(archive, baseline_days=1, dry_pages=30)
    gen = src.discover(SeedSet("reddit-archive", ("a",)), 5)
    first = next(gen)
    used = len(archive.requests)
    assert src.hydrate(first).created_at is None
    assert len(archive.requests) == used
    assert any("--new-accounts-days" in w for w in src.drain_warnings())
    gen.close()
    src.hydrate(Account("reddit-archive", "x", "x"))
    assert src.drain_warnings() == []


# -- fetch_activity -------------------------------------------------------------------


def test_fetch_activity_pages_fully_and_hashes_content() -> None:
    since, until = CUTOFF - timedelta(days=30), NOW
    items = [pulse()]
    items += [
        comment(
            f"c{i}",
            "alice",
            "worldnews" if i % 2 else "europe",
            since + timedelta(hours=2 * i + 1),
            body=f"raw-secret-{i}",
        )
        for i in range(250)
    ]
    items += [post("p1", "alice", "europe", CUTOFF - timedelta(days=1), "Title", "Body text")]
    items += [post("p2", "alice", "europe", CUTOFF - timedelta(days=2), "", "")]
    items += [post("p3", "alice", "europe", CUTOFF - timedelta(days=3), "Kept", "[removed]")]
    items += [comment("gone", "alice", "europe", CUTOFF - timedelta(days=4), body="[deleted]")]
    items += [comment("x", "someone_else", "europe", CUTOFF - timedelta(days=1))]
    archive = Archive(items)
    src, _, _ = make_source(archive)
    account = Account("reddit-archive", "alice", "alice")
    activities = src.fetch_activity(account, since, until)
    comments = [a for a in activities if a.kind == "comment"]
    posts = [a for a in activities if a.kind == "post"]
    assert len(comments) == 251 and len({a.ts for a in comments}) == 251
    assert len(posts) == 3
    assert account.meta["truncated"] is False
    assert {a.community for a in comments} == {"worldnews", "europe"}
    by_ts = {a.ts: a for a in activities}
    assert by_ts[since + timedelta(hours=1)].content_hash == content_hash("raw-secret-0")
    assert by_ts[CUTOFF - timedelta(days=1)].content_hash == content_hash("Title\nBody text")
    assert by_ts[CUTOFF - timedelta(days=2)].content_hash is None
    assert by_ts[CUTOFF - timedelta(days=3)].content_hash == content_hash("Kept")
    assert by_ts[CUTOFF - timedelta(days=4)].content_hash is None  # "[deleted]" is no text.
    assert all("raw-secret" not in repr(a) for a in activities)  # Raw text is never kept.
    # 251 comments: 100, then 100 rows with one repeat (before = oldest + 1), then 53.
    author_requests = archive.author_params()
    assert len(author_requests) == 4
    assert {p["fields"] for p in author_requests} == {
        "id,created_utc,subreddit,body",
        "id,created_utc,subreddit,title,selftext",
    }
    assert all(p["sort"] == "desc" for p in author_requests)


def test_fetch_activity_respects_the_window() -> None:
    since, until = CUTOFF - timedelta(days=3), CUTOFF + timedelta(days=1)
    items = [
        pulse(),
        comment("below", "alice", "a", since - timedelta(seconds=1)),
        comment("at_since", "alice", "a", since),
        comment("inside", "alice", "a", CUTOFF),
        comment("at_until", "alice", "a", until),  # Window is half open.
        comment("above", "alice", "a", until + timedelta(seconds=1)),
    ]
    archive = Archive(items)
    src, _, _ = make_source(archive)
    account = Account("reddit-archive", "alice", "alice")
    got = {a.ts for a in src.fetch_activity(account, since, until)}
    assert got == {since, CUTOFF}
    params = archive.author_params()
    # `after` is exclusive, so the first second of the window is asked for as since - 1.
    assert all(int(p["after"]) == ts(since) - 1 and int(p["before"]) == ts(until) for p in params)


def test_same_second_ties_across_a_page_boundary_are_not_lost() -> None:
    # 150 comments, 7 per second: the first page of 100 ends two items into a second.
    base = CUTOFF - timedelta(days=1)
    items = [pulse()] + [
        comment(f"c{i}", "alice", "a", base - timedelta(seconds=i // 7)) for i in range(150)
    ]
    archive = Archive(items)
    src, _, _ = make_source(archive)
    _, got = fetch(src)
    assert len(got) == 150
    comment_pages = [p for p in archive.author_params() if p["fields"].endswith("body")]
    first_oldest = ts(base) - 14  # Page 1 holds 100 items: seconds 0..14 back.
    assert int(comment_pages[1]["before"]) == first_oldest + 1


def test_a_page_of_one_second_steps_past_it() -> None:
    # 120 comments in a single second, then 10 older ones: the second page repeats only
    # known ids, so the third asks for `before = oldest` and moves on.
    at = CUTOFF - timedelta(days=1)
    items = [pulse()] + [comment(f"s{i}", "alice", "a", at) for i in range(120)]
    items += [comment(f"o{i}", "alice", "a", at - timedelta(hours=i + 1)) for i in range(10)]
    archive = Archive(items)
    src, _, _ = make_source(archive)
    _, got = fetch(src)
    comment_pages = [p for p in archive.author_params() if p["fields"].endswith("body")]
    assert [int(p["before"]) for p in comment_pages[1:]] == [ts(at) + 1, ts(at)]
    assert len(got) == 110  # 20 items past the page in that one second cannot be reached.


def test_fetch_activity_truncates_at_the_cap_and_keeps_the_newest() -> None:
    since = CUTOFF - timedelta(days=30)
    items = [pulse()] + [
        comment(f"c{i}", "alice", "a", CUTOFF - timedelta(minutes=10 * i + 1)) for i in range(250)
    ]
    src, _, _ = make_source(Archive(items), max_items_per_kind=150)
    account = Account("reddit-archive", "alice", "alice")
    activities = src.fetch_activity(account, since, NOW)
    assert account.meta["truncated"] is True
    assert len(activities) == 150  # Exactly the cap.
    assert max(a.ts for a in activities) == CUTOFF - timedelta(minutes=1)
    assert min(a.ts for a in activities) == CUTOFF - timedelta(minutes=10 * 149 + 1)

    exact, _, _ = make_source(Archive(items[:51]), max_items_per_kind=50)
    again = Account("reddit-archive", "alice", "alice")
    assert len(exact.fetch_activity(again, since, NOW)) == 50
    assert again.meta["truncated"] is False  # Exactly the cap and nothing more.


# -- slow-query ladder (HTTP 422) -----------------------------------------------------


def test_step1_backs_off_ignoring_the_reset_header_then_succeeds() -> None:
    archive = Archive(
        [pulse(), comment("1", "alice", "a", CUTOFF - timedelta(hours=1))],
        script=[OK_EMPTY, slow(reset="47")],  # The probe, then one timeout.
    )
    src, sleeps, _ = make_source(archive, slowdown_wait=10)
    before = src.http.rate[0]
    _, got = fetch(src)
    assert len(got) == 1
    # A timeout is not a rate limit: the 10 s backoff (x the midpoint jitter of 1.0),
    # not the 47 s reset.
    assert sleeps == [10.0]
    assert src.http.rate[0] == before  # A query timeout is not a load signal.
    assert any("Timeout" in w and "not lowered" in w for w in src.drain_warnings())


def test_step2_lowers_limit_auto_to_100_and_keeps_it() -> None:
    items = [pulse()] + [
        comment(f"c{i}", "alice", "a", CUTOFF - timedelta(minutes=i + 1)) for i in range(150)
    ]
    archive = Archive(items, script=[OK_EMPTY, slow(), slow()])  # Posts pass.
    src, sleeps, _ = make_source(archive, slowdown_wait=10, history_limit="auto")
    _, got = fetch(src)
    assert len(got) == 150
    limits = [p["limit"] for p in archive.author_params() if p["fields"].endswith("body")]
    assert limits == ["auto", "auto", "100", "100"]  # Retry, lowered, next page stays low.
    assert sleeps == [10.0, 20.0]


def test_step3_splits_the_window_until_queries_are_fast_enough() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if "author" in q and int(q["before"]) - int(q["after"]) > 10 * 86400:
            return slow()
        return None

    since = CUTOFF - timedelta(days=30)
    items = [pulse()] + [
        comment(f"c{d}", "alice", "a", since + timedelta(days=d, hours=1)) for d in range(34)
    ]
    archive = Archive(items, hook=hook)
    src, sleeps, _ = make_source(archive, slowdown_wait=1)
    account, got = fetch(src)
    assert len(got) == 34 and account.meta["truncated"] is False
    ok = [
        p for p in archive.author_params()
        if p["fields"].endswith("body") and int(p["before"]) - int(p["after"]) <= 10 * 86400
    ]  # fmt: skip
    assert len(ok) == 4  # 34 days -> 17 -> 8.5: four windows answered.
    befores = [int(p["before"]) for p in ok]
    assert befores == sorted(befores, reverse=True)  # Newer half first.
    assert sleeps and sum(sleeps) <= src.http.max_total_wait


def test_a_split_window_keeps_the_boundary_seconds() -> None:
    """Both bounds are exclusive, so the halves must overlap at `mid` (regression)."""

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if "author" in q and int(q["before"]) - int(q["after"]) > 4 * 86400:
            return slow()
        return None

    since, until = CUTOFF - timedelta(days=2), NOW
    after, before = ts(since) - 1, ts(until)
    mid = after + (before - after) // 2
    seconds = [ts(since), mid - 1, mid, mid + 1, before - 1]
    items = [pulse()] + [
        comment(f"b{i}", "alice", "a", datetime.fromtimestamp(sec, UTC))
        for i, sec in enumerate(seconds)
    ]
    archive = Archive(items, hook=hook)
    src, _, _ = make_source(archive, slowdown_wait=0)
    got = src.fetch_activity(Account("reddit-archive", "alice", "alice"), since, until)
    assert sorted(ts(a.ts) for a in got) == seconds
    narrow = [
        (int(p["after"]), int(p["before"])) for p in archive.author_params()
        if p["fields"].endswith("body") and int(p["before"]) - int(p["after"]) <= 4 * 86400
    ]  # fmt: skip
    assert (mid, before) in narrow and (after, mid + 1) in narrow


def test_split_windows_keep_the_lowered_limit() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if "author" in q and int(q["before"]) - int(q["after"]) > 10 * 86400:
            return slow()
        return None

    since = CUTOFF - timedelta(days=30)
    items = [pulse()] + [
        comment(f"c{d}", "alice", "a", since + timedelta(days=d, hours=1)) for d in range(34)
    ]
    archive = Archive(items, hook=hook)
    src, _, _ = make_source(archive, slowdown_wait=0)
    fetch(src)
    narrow = [
        p for p in archive.author_params()
        if p["fields"].endswith("body") and int(p["before"]) - int(p["after"]) <= 10 * 86400
    ]  # fmt: skip
    assert narrow and {p["limit"] for p in narrow} == {"100"}


def test_a_server_that_ignores_before_is_an_account_error() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if "author" not in q or "body" not in q["fields"]:
            return None
        late = ts(NOW) + 5000  # Always newer than `before`, whatever was asked.
        rows = [{"id": f"x{i}", "created_utc": late, "body": "b"} for i in range(100)]
        return httpx.Response(200, json={"data": rows})

    archive = Archive([pulse()], hook=hook)
    src, _, _ = make_source(archive)
    with pytest.raises(SourceError, match="archive ignored 'before'") as info:
        fetch(src)
    assert not isinstance(info.value, SlowQueryError)
    assert len(archive.author_params()) <= 6  # It stops, instead of stepping back by 1s.


def test_step4_raises_a_per_account_error_at_the_smallest_window() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if "author" in q else None

    archive = Archive([pulse()], hook=hook)
    src, _, _ = make_source(archive, slowdown_wait=0)
    with pytest.raises(SlowQueryError) as info:
        fetch(src)
    assert not isinstance(info.value, RateLimited) and isinstance(info.value, SourceError)
    last = archive.author_params()[-1]
    assert int(last["before"]) - int(last["after"]) <= 6 * 3600


def test_three_different_slow_queries_in_a_row_become_rate_limited() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if q.get("author", "").startswith("slow") else None

    archive = Archive([pulse()], hook=hook)
    src, _, _ = make_source(archive, slowdown_wait=0)
    for handle in ("slow1", "slow2"):
        with pytest.raises(SlowQueryError):
            fetch(src, handle)
    fetch(src, "fast")  # A success resets the streak.
    for handle in ("slow3", "slow4"):
        with pytest.raises(SlowQueryError):
            fetch(src, handle)
    with pytest.raises(RateLimited):
        fetch(src, "slow5")


def comment_queries_only(q: dict[str, str]) -> bool:
    return q.get("author", "").startswith("slow") and "body" in q.get("fields", "")


def test_posts_then_comments_of_one_account_count_as_one_failure() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if comment_queries_only(q) else None

    src, _, _ = make_source(Archive([pulse()], hook=hook), slowdown_wait=0)
    for handle in ("slow1", "slow2"):  # Posts succeed, comments fail: still a failure.
        with pytest.raises(SlowQueryError):
            fetch(src, handle)
    with pytest.raises(RateLimited, match="3 different fetch queries"):
        fetch(src, "slow3")


def test_slow_failure_streaks_are_kept_per_phase() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if comment_queries_only(q) else None

    items = [comment("n", "x", "worldnews", NOW), post("pn", "x", "worldnews", NOW), pulse()]
    items.append(comment("w1", "walker", "worldnews", CUTOFF - timedelta(hours=1)))
    src, _, _ = make_source(Archive(items, hook=hook), baseline_days=1, slowdown_wait=0)
    for handle in ("slow1", "slow2"):
        with pytest.raises(SlowQueryError):
            fetch(src, handle)
    src.data_horizon()  # A successful probe does not reset the fetch streak.
    list(src.discover(SeedSet("reddit-archive", ("worldnews",)), 1))  # Nor does discovery.
    with pytest.raises(RateLimited):
        fetch(src, "slow3")


def test_load_422_skips_the_limit_and_split_steps() -> None:
    archive = Archive([pulse()], script=[slow(LOAD)] * 5)
    src, sleeps, _ = make_source(archive, slowdown_wait=5)
    with pytest.raises(SlowQueryError):
        fetch(src)
    assert [p["limit"] for p in archive.author_params()] == ["100", "100"]
    assert sleeps == [5.0]


def test_ladder_waits_share_the_http_total_wait_budget() -> None:
    archive = Archive([pulse()], script=[slow()] * 10)
    src, sleeps, _ = make_source(archive, slowdown_wait=100)
    src.http.max_total_wait = 200
    with pytest.raises(SlowQueryError) as info:
        fetch(src)
    assert sleeps == [100.0]  # 100 fits; the next wait (120, the cap) would pass 200 s.
    assert "budget" in str(info.value)


def test_other_422_is_an_account_error_not_a_slowdown() -> None:
    archive = Archive([pulse()], script=[httpx.Response(422, json={"error": "bad"})])
    src, sleeps, _ = make_source(archive)
    with pytest.raises(SourceError) as info:
        fetch(src, "a")
    assert not isinstance(info.value, (RateLimited, SlowQueryError)) and sleeps == []


def test_rate_recovers_additively_after_a_streak_of_successes() -> None:
    archive = Archive([pulse()], script=[slow(LOAD), slow(LOAD)])
    src, _, _ = make_source(archive, slowdown_wait=0, max_slowdown_retries=2)
    base = src.http.rate[0]
    fetch(src, "u0")  # Two load 422s on one query halve the rate once.
    assert src.http.rate[0] == base / 2
    # u0 ended with 2 successes (posts retried, then comments): 8 more reach 10.
    for i in range(1, 5):  # 2 requests each.
        fetch(src, f"u{i}")
    assert src.http.rate[0] == pytest.approx(base / 2 + 0.1)
    for i in range(5, 15):  # 20 more successes: two more steps, capped at the base.
        fetch(src, f"u{i}")
    assert src.http.rate[0] == pytest.approx(base)
    for i in range(15, 40):
        fetch(src, f"u{i}")
    assert src.http.rate[0] == pytest.approx(base)  # Never above max_rps.


def test_429_waits_for_the_reset_header() -> None:
    archive = Archive(
        [pulse()],
        script=[httpx.Response(429, headers={"X-RateLimit-Reset": "7"}, json={"error": "limit"})],
    )
    src, adapter_sleeps, http_sleeps = make_source(archive)
    fetch(src, "a")
    assert 10.5 in http_sleeps  # Reset seconds plus the 1 s margin and 2.5 s of jitter.
    assert adapter_sleeps == []


# -- archive horizon -----------------------------------------------------------------


def probe_with_newest(newest: datetime | None, post_newest: datetime | None = ...) -> Archive:  # type: ignore[assignment]
    """worldnews archive with a newest comment and (by default the same) newest post."""
    post_at = newest if post_newest is ... else post_newest
    items: list[dict[str, Any]] = []
    if newest:
        items.append(comment("n", "x", "worldnews", newest))
    if post_at:
        items.append(post("pn", "x", "worldnews", post_at))
    return Archive(items)


def probed(archive: Archive, path: str) -> list[str]:
    return [r.url.params["subreddit"] for r in archive.requests if r.url.path == path]


def test_data_horizon_is_the_newest_archived_item_and_is_cached() -> None:
    archive = probe_with_newest(NOW - timedelta(hours=30))
    src, _, _ = make_source(archive)
    assert src.data_horizon() == NOW - timedelta(hours=30)
    assert src.archive_newest == NOW - timedelta(hours=30)
    assert src.data_horizon() == NOW - timedelta(hours=30)
    assert len(archive.requests) == 2  # worldnews comments + worldnews posts, once.


def test_adapter_no_longer_warns_about_lag_itself() -> None:
    """The core pipeline owns the lag message; the adapter must not double-warn."""
    src, _, _ = make_source(probe_with_newest(NOW - timedelta(hours=30)))
    list(src.discover(SeedSet("reddit-archive", ("worldnews",)), 1))
    fetch(src)
    assert not any("cannot be trusted" in w or "lag is unknown" in w for w in src.drain_warnings())


def test_probes_are_worldnews_first_then_up_to_three_seeds() -> None:
    items = [
        comment("a", "x", "a", NOW - timedelta(hours=1)),
        comment("b", "x", "b", NOW - timedelta(hours=20)),
        comment("c", "x", "c", NOW - timedelta(hours=3)),
        comment("d", "x", "d", NOW - timedelta(days=9)),  # Fourth seed: not probed.
        comment("w", "x", "worldnews", NOW - timedelta(seconds=30)),
        post("pa", "x", "a", NOW - timedelta(hours=2)),
        post("pw", "x", "worldnews", NOW - timedelta(seconds=40)),
    ]
    archive = Archive(items)
    src, _, _ = make_source(archive)
    src.note_seeds(SeedSet("reddit-archive", ("a", "b", "c", "d")))
    # b is 19.99h older than the newest probe: inside the 24h band, so it counts.
    assert src.data_horizon() == NOW - timedelta(hours=20)
    assert src.data_horizon() == NOW - timedelta(hours=20)  # Cached.
    assert probed(archive, "/api/comments/search") == ["worldnews", "a", "b", "c"]
    assert probed(archive, "/api/posts/search") == ["worldnews", "a", "b", "c"]


def test_a_dead_sub_does_not_drag_the_horizon_down() -> None:
    dead = datetime(2020, 4, 11, tzinfo=NOW.tzinfo)  # r/The_Donald, banned.
    items = [
        comment("w", "x", "worldnews", NOW - timedelta(seconds=30)),
        post("pw", "x", "worldnews", NOW - timedelta(minutes=2)),
        comment("d", "x", "The_Donald", dead),
        post("pd", "x", "The_Donald", dead),
    ]
    src, _, _ = make_source(Archive(items))
    src.note_seeds(SeedSet("reddit-archive", ("The_Donald", "worldnews")))
    assert src.data_horizon() == NOW - timedelta(minutes=2)
    warnings = " ".join(src.drain_warnings())
    assert warnings.count("r/The_Donald was ignored") == 2  # Once per kind.
    assert "2020-04-11" in warnings


def test_horizon_is_the_older_of_the_comment_and_post_horizons() -> None:
    src, _, _ = make_source(
        probe_with_newest(NOW - timedelta(hours=1), post_newest=NOW - timedelta(hours=10))
    )
    assert src.data_horizon() == NOW - timedelta(hours=10)


def test_a_missing_post_horizon_makes_the_horizon_unreadable() -> None:
    src, _, _ = make_source(probe_with_newest(NOW - timedelta(hours=1), post_newest=None))
    with pytest.raises(SourceError, match="newest post"):
        src.data_horizon()


def test_unreadable_probes_retry_once_after_a_backoff_then_raise() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return httpx.Response(400, json={"error": "nope"}) if "author" not in q else None

    archive = Archive([comment("1", "alice", "a", CUTOFF - timedelta(hours=1))], hook=hook)
    src, sleeps, _ = make_source(archive, slowdown_wait=7)
    with pytest.raises(SourceError):
        src.data_horizon()
    with pytest.raises(SourceError):  # The failure is cached, not retried.
        src.data_horizon()
    assert sleeps == [7.0]
    assert len(archive.requests) == 2  # worldnews comments, backoff, worldnews comments.
    account, got = fetch(src)  # A failed probe never fails an account.
    assert len(got) == 1 and account.meta["truncated"] is False


def test_adapter_waits_with_the_http_clients_sleep_by_default() -> None:
    """Without its own sleep the adapter must not call time.sleep (CLI tests rely on it)."""

    def hook(q: dict[str, str]) -> httpx.Response | None:
        return httpx.Response(400, json={"error": "nope"}) if "author" not in q else None

    http_sleeps: list[float] = []
    http = HttpClient(
        transport=httpx.MockTransport(Archive([], hook=hook)),
        sleep=http_sleeps.append,
        clock=lambda: 0.0,
    )
    config = SourceConfig(http=http, credentials={}, now=NOW, cutoff=CUTOFF, options={})
    src = RedditArchiveSource(config)
    # The client wraps the injected sleep so a cancel can end it; the adapter uses that.
    assert src._sleep == http.sleep
    with pytest.raises(SourceError):
        src.data_horizon()
    assert 10.0 in http_sleeps  # The probe retry backoff (slowdown_wait) went through it.


def test_the_probe_retry_recovers_from_a_transient_error() -> None:
    archive = Archive(
        [comment("n", "x", "worldnews", NOW), post("p", "x", "worldnews", NOW)],
        script=[httpx.Response(400, json={"error": "hiccup"})],
    )
    src, sleeps, _ = make_source(archive, slowdown_wait=3)
    assert src.data_horizon() == NOW
    assert sleeps == [3.0]


def test_auth_errors_propagate_from_the_horizon() -> None:
    class Auth(RedditArchiveSource):
        def _newest_in(self, subreddit: str, path: str) -> datetime | None:
            raise SourceAuthError("denied")

    base, _, _ = make_source(Archive([]))
    src = Auth(base.config)
    with pytest.raises(SourceAuthError):
        src.data_horizon()
    with pytest.raises(SourceAuthError):  # Fatal errors are not cached as a stale answer.
        src.data_horizon()


def test_rate_limited_propagates_from_the_horizon_instead_of_failing_open() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow(LOAD, reset="9999") if "author" not in q else None

    archive = Archive([], hook=hook)
    src, _, _ = make_source(archive, slowdown_wait=0)
    with pytest.raises(RateLimited):
        src.data_horizon()
    assert len(archive.requests) == 1  # No retry round, no fallback probes.


def test_budget_exceeded_propagates_from_the_horizon() -> None:
    class Budget(RedditArchiveSource):
        def _newest_in(self, subreddit: str, path: str) -> datetime | None:
            raise BudgetExceeded("out of requests")

    base, _, _ = make_source(Archive([]))
    with pytest.raises(BudgetExceeded):
        Budget(base.config).data_horizon()


def test_slow_probes_never_become_rate_limited_but_leave_the_horizon_stale() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if "author" not in q else None

    src, _, _ = make_source(Archive([], hook=hook), slowdown_wait=0)
    src.note_seeds(SeedSet("reddit-archive", ("a", "b")))
    with pytest.raises(SourceError, match="could not read") as raised:
        src.data_horizon()
    assert not isinstance(raised.value, RateLimited)
    assert src._slow_failures["probe"] == 0


def test_three_slow_probes_do_not_stop_the_scan_before_the_fourth_is_tried() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if "author" not in q and q.get("subreddit") in ("worldnews", "a", "b"):
            return slow()
        return None

    good = NOW - timedelta(hours=2)
    archive = Archive(
        [comment("n", "x", "c", good), post("p", "x", "c", good)],
        hook=hook,
    )
    src, _, _ = make_source(archive, slowdown_wait=0)
    src.note_seeds(SeedSet("reddit-archive", ("a", "b", "c")))
    assert src.data_horizon() == good
    assert probed(archive, "/api/comments/search").count("c") == 1
    assert src._slow_failures["probe"] == 0


def test_empty_archive_horizon_is_unknown() -> None:
    src, _, _ = make_source(probe_with_newest(None))
    with pytest.raises(SourceError, match="no archived comments"):
        src.data_horizon()


def test_horizon_probe_uses_the_noted_seeds_and_skips_empty_ones() -> None:
    archive = Archive(
        [
            comment("n", "x", "europe", NOW - timedelta(hours=1)),
            post("p", "x", "europe", NOW - timedelta(hours=1)),
        ]
    )
    src, _, _ = make_source(archive)
    src.note_seeds(SeedSet("reddit-archive", ("europe", "worldnews")))
    assert src.data_horizon() == NOW - timedelta(hours=1)  # Empty worldnews is skipped.
    assert probed(archive, "/api/comments/search") == ["worldnews", "europe"]


def test_freshness_subreddit_option_beats_the_noted_seed() -> None:
    archive = Archive(
        [
            comment("n", "x", "europe", NOW - timedelta(hours=1)),
            post("p", "x", "europe", NOW - timedelta(hours=1)),
        ]
    )
    src, _, _ = make_source(archive, freshness_subreddit="europe")
    src.note_seeds(SeedSet("reddit-archive", ("other",)))
    src.data_horizon()
    assert probed(archive, "/api/comments/search") == ["worldnews", "europe"]


def test_slow_query_budget_is_capped_below_the_core_total_wait() -> None:
    archive = Archive([pulse()], script=[slow()] * 40)
    src, sleeps, _ = make_source(archive, slowdown_wait=100)
    assert src.http.max_total_wait == 1800.0  # The core default; the adapter caps itself.
    with pytest.raises(SlowQueryError, match="900s wait budget"):
        fetch(src)
    assert 0 < sum(sleeps) <= MAX_SLOW_QUERY_WAIT == 900.0


# -- coverage check -------------------------------------------------------------------


def coverage_world(
    gap: tuple[datetime, datetime] | None = None,
    gap_every: timedelta | None = None,
    gap_kinds: tuple[str, ...] = ("comments", "posts"),
) -> list[dict[str, Any]]:
    """Fresh probes plus steady worldnews comments and posts.

    ``gap`` thins (``gap_every``) or empties one span, for the kinds in ``gap_kinds``.
    """
    items: list[dict[str, Any]] = [pulse(), post_pulse()]
    start, end = CUTOFF - timedelta(days=8), NOW - timedelta(hours=1)
    for kind in ("comments", "posts"):
        if gap is None or kind not in gap_kinds:
            items += traffic(start, end, kind=kind)
            continue
        items += traffic(start, gap[0], kind=kind) + traffic(gap[1], end, kind=kind)
        if gap_every is not None:
            items += traffic(gap[0], gap[1], every=gap_every, kind=kind)
    return items


def coverage_requests(archive: Archive) -> list[dict[str, str]]:
    return [dict(r.url.params) for r in archive.requests if r.url.params.get("sort") == "asc"]


def test_coverage_check_keeps_a_complete_horizon_and_stays_cheap() -> None:
    archive = Archive(coverage_world())
    src, _, _ = make_source(archive, coverage_threshold="0.25")
    assert src.data_horizon() == NOW - timedelta(hours=1)
    assert not src.drain_warnings()
    checks = coverage_requests(archive)
    # 3 reference days + 6 checked days (cutoff - 2d up to the horizon): 4 comment
    # samples a day and 1 post sample a day.
    assert len(checks) == (4 + 1) * (3 + 6)
    assert all(p["limit"] == "100" and p["subreddit"] == "worldnews" for p in checks)
    assert {r.url.path for r in archive.requests} <= {
        "/api/comments/search", "/api/posts/search"
    }  # Never the aggregate endpoint.  # fmt: skip


@pytest.mark.parametrize("gap_every", [None, timedelta(minutes=100)])
def test_coverage_gap_pulls_the_horizon_back_to_the_start_of_that_day(
    gap_every: timedelta | None,
) -> None:
    gap = (CUTOFF + timedelta(days=1), CUTOFF + timedelta(days=2))
    src, _, _ = make_source(Archive(coverage_world(gap, gap_every)), coverage_threshold="0.25")
    assert src.data_horizon() == CUTOFF + timedelta(days=1)
    assert src.archive_newest == NOW - timedelta(hours=1)  # The probes saw further.
    warnings = " ".join(src.drain_warnings())
    assert "day starting 2026-10-11 00:00 UTC" in warnings
    assert "ingestion gap" in warnings


def test_a_half_rate_day_is_not_a_gap() -> None:
    gap = (CUTOFF + timedelta(days=1), CUTOFF + timedelta(days=2))
    world = coverage_world(gap, timedelta(minutes=20))
    src, _, _ = make_source(Archive(world), coverage_threshold="0.25")
    assert src.data_horizon() == NOW - timedelta(hours=1)


def test_coverage_threshold_is_a_source_option() -> None:
    gap = (CUTOFF + timedelta(days=1), CUTOFF + timedelta(days=2))
    world = coverage_world(gap, timedelta(minutes=20))  # About half the usual rate.
    src, _, _ = make_source(Archive(world), coverage_threshold="0.6")
    assert src.data_horizon() == CUTOFF + timedelta(days=1)


def dip_at(*points: int) -> Callable[[dict[str, str]], httpx.Response | None]:
    """Answer the samples that start exactly at ``points`` with a single stray item."""

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "asc" and int(q["after"]) + 1 in points:
            row = {"id": "stray", "created_utc": int(q["after"]) + 1000}
            return httpx.Response(200, json={"data": [row]})
        return None

    return hook


DIP = ts(CUTOFF + timedelta(days=1, hours=6))
DAY_START = CUTOFF + timedelta(days=1)


def test_a_single_low_sample_followed_by_a_normal_one_is_not_a_gap() -> None:
    quiet = Archive(coverage_world())
    make_source(quiet, coverage_threshold="0.25")[0].data_horizon()
    archive = Archive(coverage_world(), hook=dip_at(DIP))
    src, _, _ = make_source(archive, coverage_threshold="0.25")
    assert src.data_horizon() == NOW - timedelta(hours=1)
    assert not src.drain_warnings()
    # The dip cost exactly one confirming request, an hour after the sample.
    assert len(coverage_requests(archive)) == len(coverage_requests(quiet)) + 1
    assert DIP + 3600 in {int(p["after"]) + 1 for p in coverage_requests(archive)}


def test_two_low_samples_an_hour_apart_are_a_gap() -> None:
    archive = Archive(coverage_world(), hook=dip_at(DIP, DIP + 3600))
    src, _, _ = make_source(archive, coverage_threshold="0.25")
    assert src.data_horizon() == DAY_START
    warning = " ".join(src.drain_warnings())
    assert "day starting 2026-10-11 00:00 UTC" in warning
    assert "again at 2026-10-11 07:00 UTC" in warning


def test_a_low_sample_with_no_room_for_a_later_one_is_a_gap() -> None:
    """The last sample point has under an hour of archive left, so it cannot be confirmed."""
    last_day = CUTOFF + timedelta(days=3)  # The horizon is at 23:00 on this day.
    point = ts(last_day + timedelta(hours=22))
    archive = Archive(coverage_world(), hook=dip_at(point))
    src, _, _ = make_source(archive, coverage_threshold="0.25", coverage_samples_per_day=24)
    assert src.data_horizon() == last_day
    assert point + 3600 not in {int(p["after"]) + 1 for p in coverage_requests(archive)}


def test_a_gap_confirmed_by_nothing_leaves_out_the_again_clause() -> None:
    last_day = CUTOFF + timedelta(days=3)
    point = ts(last_day + timedelta(hours=22))
    archive = Archive(coverage_world(), hook=dip_at(point))
    src, _, _ = make_source(archive, coverage_threshold="0.25", coverage_samples_per_day=24)
    src.data_horizon()
    warning = " ".join(src.drain_warnings())
    assert "ingestion gap" in warning
    assert "and again at" not in warning


def test_a_confirmation_on_the_next_sample_time_reuses_that_sample() -> None:
    archive = Archive(coverage_world(), hook=dip_at(DIP))
    src, _, _ = make_source(archive, coverage_threshold="0.25", coverage_samples_per_day=24)
    assert src.data_horizon() == NOW - timedelta(hours=1)
    starts = [int(p["after"]) + 1 for p in coverage_requests(archive)]
    assert starts.count(DIP + 3600) == 1  # Once for the confirmation and the sample alike.
    assert starts.count(DIP) == 1


def test_a_stale_probe_value_is_cleared_when_the_horizon_is_recomputed() -> None:
    src, _, _ = make_source(Archive(coverage_world()), coverage_threshold="0.25")

    def rate_limited(_: str) -> datetime:
        raise RateLimited(1.0)

    def unreadable(_: str) -> datetime:
        raise SourceError("probe failed")

    real_coverage = src._coverage_horizon
    src._coverage_horizon = lambda horizon: rate_limited("")  # type: ignore[method-assign]
    with pytest.raises(RateLimited):
        src.data_horizon()
    assert src.fallback_horizon() == NOW - timedelta(hours=1)
    src._coverage_horizon = real_coverage  # type: ignore[method-assign]
    src._kind_horizon = unreadable  # type: ignore[method-assign]
    with pytest.raises(SourceError, match="probe failed"):
        src.data_horizon()
    assert src.fallback_horizon() is None


@pytest.mark.parametrize(("remaining", "attempts"), [(51, 0), (54, 0), (55, 5)])
def test_the_coverage_budget_guard_leaves_room_for_http_retries(
    remaining: int, attempts: int
) -> None:
    """With 4 retries, a request needs 5 spare attempts on top of the 50 kept for discovery."""
    archive = Archive([], hook=lambda q: httpx.Response(429))
    src, _, _ = make_source(archive, max_requests=remaining)
    src._guard_budget = True
    with pytest.raises((SourceError, RateLimited)) as raised:
        src._request("/api/comments/search", {})
    assert len(archive.requests) == attempts
    if attempts == 0:
        assert "keeps 50 for discovery" in str(raised.value)
    assert src.http.requests_remaining == remaining - attempts
    assert src.http.requests_remaining >= 50


def test_unreadable_coverage_sample_fails_closed() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "asc" and int(q["after"]) > ts(CUTOFF):
            return httpx.Response(400, json={"error": "nope"})
        return None

    src, _, _ = make_source(Archive(coverage_world(), hook=hook), coverage_threshold="0.25")
    with pytest.raises(SourceError, match="coverage check could not read"):
        src.data_horizon()
    with pytest.raises(SourceError):  # Cached like a failed probe.
        src.data_horizon()


def read_timeout_on_coverage(timeouts: int) -> Callable[[dict[str, str]], httpx.Response | None]:
    """A hook whose first ``timeouts`` coverage samples (sort=asc) read-time-out."""
    seen = [0]

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "asc" and int(q["after"]) > ts(CUTOFF):
            seen[0] += 1
            if seen[0] <= timeouts:
                raise httpx.ReadTimeout("slow")
        return None

    return hook


def test_a_coverage_sample_is_retried_up_to_three_times_after_read_timeouts() -> None:
    # Each failed sample is two requests (the ladder retries once), so six time out.
    hook = read_timeout_on_coverage(6)
    src, sleeps, _ = make_source(
        Archive(coverage_world(), hook=hook),
        coverage_threshold="0.25",
        slowdown_wait=4,
    )
    assert src.data_horizon() == NOW - timedelta(hours=1)
    # Per failed sample: the ladder's backoff (4 s), then the retry pause, which doubles
    # (4, 8, 16 s; the fake jitter is neutral).
    assert sleeps == [4.0, 4.0, 4.0, 8.0, 4.0, 16.0]
    assert src._slow_failures["coverage"] == 0  # A success resets the guard.


def test_a_coverage_sample_gives_up_after_the_fourth_read_timeout() -> None:
    hook = read_timeout_on_coverage(8)
    src, _, _ = make_source(
        Archive(coverage_world(), hook=hook),
        coverage_threshold="0.25",
        slowdown_wait=0,
    )
    with pytest.raises(SourceError, match="coverage check could not read"):
        src.data_horizon()


def test_coverage_read_timeouts_do_not_trip_the_slow_query_guard() -> None:
    archive = Archive(coverage_world(), hook=read_timeout_on_coverage(10_000))
    src, _, _ = make_source(archive, coverage_threshold="0.25", slowdown_wait=0)
    with pytest.raises(SourceError, match="coverage check could not read") as info:
        src.data_horizon()
    assert not isinstance(info.value, RateLimited)
    # The first sample after the cutoff: 4 tries (the original and 3 retries), each 2
    # requests, because the ladder retries once. The baseline samples before it all read.
    coverage = [
        r
        for r in archive.requests
        if r.url.params.get("sort") == "asc" and int(r.url.params["after"]) > ts(CUTOFF)
    ]
    assert len(coverage) == 8


def test_coverage_check_without_reference_comments_fails_closed() -> None:
    items = [
        pulse(),
        post_pulse(),
        *traffic(CUTOFF - timedelta(days=2), NOW - timedelta(hours=1)),
        *traffic(CUTOFF - timedelta(days=2), NOW - timedelta(hours=1), kind="posts"),
    ]
    src, _, _ = make_source(Archive(items), coverage_threshold="0.25")
    with pytest.raises(SourceError, match="no r/worldnews comments in the baseline"):
        src.data_horizon()


def test_rate_limit_in_the_coverage_check_propagates() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "asc":
            return httpx.Response(429, headers={"X-RateLimit-Reset": "99999"})
        return None

    src, _, _ = make_source(Archive(coverage_world(), hook=hook), coverage_threshold="0.25")
    with pytest.raises(RateLimited):
        src.data_horizon()


# -- coverage check: sample times, kinds, retries, budget -------------------------------

SIX_HOURS = timedelta(hours=6)


def burst_world(
    gaps: tuple[tuple[datetime, datetime, tuple[str, ...]], ...] = (),
    *,
    cutoff: datetime = CUTOFF,
    now: datetime = NOW,
    start_days: int = 10,
) -> list[dict[str, Any]]:
    """Worldnews comments and posts, one a minute for 2 hours from every 6 hours.

    The bursts start at the cutoff's time of day, so every coverage sample point meets
    the dense traffic that real samples see (a page of 100 items covers under 2 hours).
    Items inside a gap span are left out, for the kinds the gap names.
    """
    items: list[dict[str, Any]] = [pulse(), post_pulse()]
    moment = cutoff - timedelta(days=start_days)
    while moment < now - timedelta(hours=1):
        for minute in range(120):
            when = moment + timedelta(minutes=minute)
            for kind in ("comments", "posts"):
                if any(a <= when < b and kind in kinds for a, b, kinds in gaps):
                    continue
                make = post if kind == "posts" else comment
                items.append(make(f"b-{kind}-{ts(when)}", "[deleted]", "worldnews", when))
        moment += SIX_HOURS
    return items


def synthetic_archive(now: datetime) -> Archive:
    """An archive with a steady item every minute, answered without storing any rows."""

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "desc":
            rows = [{"created_utc": ts(now - timedelta(hours=1))}]
        else:
            first = int(q["after"]) + 1
            rows = [
                {"id": str(i), "created_utc": first + 60 * i}
                for i in range(100)
                if first + 60 * i < int(q["before"])
            ]
        return httpx.Response(200, json={"data": rows})

    return Archive([], hook=hook)


def sample_day_starts(archive: Archive, cutoff: datetime = CUTOFF) -> list[int]:
    """Whole days after ``cutoff - 2 days`` that the checked (not reference) samples hit."""
    first = ts(cutoff - timedelta(days=2))
    days = {(int(p["after"]) + 1 - first) // 86400 for p in coverage_requests(archive)}
    return sorted(d for d in days if d >= 0)


def test_coverage_samples_four_times_a_day_six_hours_apart() -> None:
    archive = Archive(burst_world())
    src, _, _ = make_source(archive, coverage_threshold="0.25")
    assert src.data_horizon() == NOW - timedelta(hours=1)
    assert not src.drain_warnings()
    points = {int(p["after"]) + 1 for p in coverage_requests(archive)}
    assert {point % 86400 for point in points} == {0, 6 * 3600, 12 * 3600, 18 * 3600}


def test_samples_per_day_is_a_source_option() -> None:
    archive = Archive(burst_world())
    src, _, _ = make_source(archive, coverage_threshold="0.25", coverage_samples_per_day=2)
    src.data_horizon()
    points = {int(p["after"]) + 1 for p in coverage_requests(archive)}
    assert {point % 86400 for point in points} == {0, 12 * 3600}
    assert len(coverage_requests(archive)) == (2 + 1) * (3 + 6)


def test_a_gap_between_the_old_two_sample_times_is_now_caught() -> None:
    """Nine dark hours that the sample times 06:00 and 18:00 of the old check straddled."""
    gap = (CUTOFF + timedelta(days=1, hours=9), CUTOFF + timedelta(days=1, hours=18))
    world = burst_world(((*gap, ("comments", "posts")),))
    src, _, _ = make_source(Archive(world), coverage_threshold="0.25")
    assert src.data_horizon() == CUTOFF + timedelta(days=1)
    src, _, _ = make_source(Archive(world), coverage_threshold="0.25", coverage_samples_per_day=1)
    assert src.data_horizon() == NOW - timedelta(hours=1)  # One sample a day misses it.


def test_sample_times_follow_a_cutoff_that_is_not_at_midnight() -> None:
    cutoff = CUTOFF + timedelta(hours=7, minutes=30)
    gap = (cutoff + timedelta(days=1, hours=5), cutoff + timedelta(days=2))
    world = burst_world(((*gap, ("comments", "posts")),), cutoff=cutoff)
    archive = Archive(world)
    src, _, _ = make_source(archive, cutoff=cutoff, coverage_threshold="0.25")
    assert src.data_horizon() == cutoff + timedelta(days=1)
    warning = " ".join(src.drain_warnings())
    assert "day starting 2026-10-11 07:30 UTC" in warning
    offsets = {(int(p["after"]) + 1 - ts(cutoff)) % 21600 for p in coverage_requests(archive)}
    assert offsets == {0, 3600}  # The samples, and the confirmation an hour later.


@pytest.mark.parametrize("kind", ["comments", "posts"])
def test_a_gap_in_either_kind_pulls_the_horizon_back_and_names_the_kind(kind: str) -> None:
    gap = (CUTOFF + timedelta(days=1), CUTOFF + timedelta(days=2))
    src, _, _ = make_source(
        Archive(coverage_world(gap, gap_kinds=(kind,))), coverage_threshold="0.25"
    )
    assert src.data_horizon() == CUTOFF + timedelta(days=1)
    warning = " ".join(src.drain_warnings())
    assert f"r/worldnews {kind} at" in warning
    assert ("posts" in warning) is (kind == "posts")


def test_each_kind_has_its_own_reference_median() -> None:
    """Posts are far rarer than comments; a post rate must not be judged by comments."""
    items = [pulse(), post_pulse()]
    start, end = CUTOFF - timedelta(days=8), NOW - timedelta(hours=1)
    items += traffic(start, end, every=timedelta(minutes=1))
    items += traffic(start, end, every=timedelta(minutes=30), kind="posts")
    src, _, _ = make_source(Archive(items), coverage_threshold="0.25")
    assert src.data_horizon() == end
    assert not src.drain_warnings()


def test_a_missing_post_reference_fails_closed() -> None:
    items = [pulse(), post_pulse(), *traffic(CUTOFF - timedelta(days=8), NOW)]
    src, _, _ = make_source(Archive(items), coverage_threshold="0.25")
    with pytest.raises(SourceError, match="no r/worldnews posts in the baseline"):
        src.data_horizon()


@pytest.mark.parametrize("real_gap", [False, True])
def test_gaps_inside_reference_days_do_not_move_the_median(real_gap: bool) -> None:
    """Two of five reference days are dark at 06:00; the median stays at the normal rate."""
    both = ("comments", "posts")
    dark = timedelta(hours=5), timedelta(hours=11)
    gaps = [
        (CUTOFF - timedelta(days=5) + dark[0], CUTOFF - timedelta(days=5) + dark[1], both),
        (CUTOFF - timedelta(days=7) + dark[0], CUTOFF - timedelta(days=7) + dark[1], both),
    ]
    if real_gap:
        gaps.append(
            (CUTOFF + timedelta(days=1) + dark[0], CUTOFF + timedelta(days=1) + dark[1], both)
        )
    src, _, _ = make_source(
        Archive(burst_world(tuple(gaps))), baseline_days=8, coverage_threshold="0.25"
    )
    if real_gap:
        assert src.data_horizon() == CUTOFF + timedelta(days=1)
    else:
        assert src.data_horizon() == NOW - timedelta(hours=1)
        assert not src.drain_warnings()


def test_a_failed_sample_is_retried_once_after_a_backoff() -> None:
    seen: list[int] = []

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "asc" and int(q["after"]) > ts(CUTOFF) and not seen:
            seen.append(int(q["after"]))
            return httpx.Response(400)
        return None

    archive = Archive(coverage_world(), hook=hook)
    src, sleeps, _ = make_source(archive, coverage_threshold="0.25", slowdown_wait=4)
    assert src.data_horizon() == NOW - timedelta(hours=1)
    assert sleeps == [4.0]
    same = [
        r
        for r in archive.requests
        if r.url.path == COMMENTS_PATH and r.url.params.get("after") == str(seen[0])
    ]
    assert len(same) == 2


def test_a_sample_that_fails_twice_fails_closed_after_one_backoff() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "asc" and int(q["after"]) > ts(CUTOFF):
            return httpx.Response(400)
        return None

    archive = Archive(coverage_world(), hook=hook)
    src, sleeps, _ = make_source(archive, coverage_threshold="0.25", slowdown_wait=4)
    with pytest.raises(SourceError, match="coverage check could not read"):
        src.data_horizon()
    assert sleeps == [4.0]
    failing = [p for p in coverage_requests(archive) if int(p["after"]) > ts(CUTOFF)]
    assert len(failing) == 2 and failing[0] == failing[1]


def rows_at(*stamps: object) -> Callable[[dict[str, str]], httpx.Response | None]:
    rows = [{"id": str(i), "created_utc": stamp} for i, stamp in enumerate(stamps)]

    def hook(q: dict[str, str]) -> httpx.Response | None:
        return httpx.Response(200, json={"data": rows}) if q.get("sort") == "asc" else None

    return hook


def test_a_full_page_inside_one_second_has_a_finite_rate() -> None:
    start = ts(CUTOFF)
    src, _, _ = make_source(Archive([], hook=rows_at(*[start] * 100)))
    assert src._sample_rate(COMMENTS_PATH, start, start + 3600) == 100.0
    src, _, _ = make_source(Archive([], hook=rows_at(*[start + 9] * 100)))
    assert src._sample_rate(COMMENTS_PATH, start, start + 3600) == 10.0


@pytest.mark.parametrize(
    "stamps",
    [[], [5, 6], [ts(CUTOFF) + 7200], [None, "x", "inf", "1e30", "nan"]],
)
def test_a_page_without_stamps_in_range_is_a_zero_rate(stamps: list[object]) -> None:
    src, _, _ = make_source(Archive([], hook=rows_at(*stamps)))
    assert src._sample_rate(COMMENTS_PATH, ts(CUTOFF), ts(CUTOFF) + 3600) == 0.0


def test_stamps_outside_the_window_do_not_count() -> None:
    start = ts(CUTOFF)
    src, _, _ = make_source(Archive([], hook=rows_at(start - 5, start + 10, start + 4000)))
    assert src._sample_rate(COMMENTS_PATH, start, start + 3600) == 1 / 3600


def test_an_ignored_window_is_treated_as_a_gap_not_an_error() -> None:
    base = coverage_world()
    bad_day = ts(CUTOFF + timedelta(days=1))

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "asc" and bad_day <= int(q["after"]) < bad_day + 86400:
            return httpx.Response(200, json={"data": [{"id": "x", "created_utc": 5}]})
        return None

    src, _, _ = make_source(Archive(base, hook=hook), coverage_threshold="0.25")
    assert src.data_horizon() == CUTOFF + timedelta(days=1)


def test_old_cutoffs_are_sampled_every_day_then_every_third_day() -> None:
    now = CUTOFF + timedelta(days=30)
    archive = synthetic_archive(now)
    src, _, _ = make_source(archive, now=now, baseline_days=30, coverage_threshold="0.25")
    assert src.data_horizon() == now - timedelta(hours=1)
    days = [d for d in sample_day_starts(archive) if d >= 0]
    assert days[:14] == list(range(14))
    assert days[14:] == [14, 17, 20, 23, 26, 29]
    assert len(coverage_requests(archive)) == (4 + 1) * (7 + 20)


def test_request_estimates_for_recent_and_old_cutoffs() -> None:
    for days, expected in ((3, (4 + 1) * (7 + 5)), (30, (4 + 1) * (7 + 20))):
        now = CUTOFF + timedelta(days=days)
        archive = synthetic_archive(now)
        src, _, _ = make_source(archive, now=now, baseline_days=30, coverage_threshold="0.25")
        src.data_horizon()
        assert len(coverage_requests(archive)) == expected


#: The pre-flight need of coverage_world(): 45 samples, each with one retry, 6 confirmations
#: with one retry each, 50 requests kept for discovery, and the HTTP layer's 4 retries.
NEEDED = 2 * ((4 + 1) * (3 + 6) + 6) + 50 + 4


def test_coverage_check_that_would_exceed_the_request_budget_fails_closed() -> None:
    archive = Archive(coverage_world())
    src, _, _ = make_source(archive, coverage_threshold="0.25", max_requests=2 + NEEDED - 1)
    with pytest.raises(SourceError, match="remain of --max-requests") as raised:
        src.data_horizon()
    assert not isinstance(raised.value, BudgetExceeded)
    assert not coverage_requests(archive)  # Refused before the first sample.
    with pytest.raises(SourceError, match="remain of --max-requests"):
        src.data_horizon()  # Cached like any stale source.


def test_coverage_check_that_exactly_fits_the_request_budget_runs() -> None:
    src, _, _ = make_source(
        Archive(coverage_world()), coverage_threshold="0.25", max_requests=2 + NEEDED
    )
    assert src.data_horizon() == NOW - timedelta(hours=1)


def test_coverage_check_never_runs_the_budget_dry_when_queries_are_slow() -> None:
    """422 answers and retries stop at the discovery headroom with a plain SourceError."""

    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow(LOAD) if q.get("sort") == "asc" else None

    archive = Archive(coverage_world(), hook=hook)
    src, _, _ = make_source(
        archive,
        coverage_threshold="0.25",
        max_requests=2 + NEEDED,
        max_slowdown_retries="500",
        slowdown_wait="0",
    )
    with pytest.raises(SourceError, match="keeps 50 for discovery") as raised:
        src.data_horizon()
    assert not isinstance(raised.value, (BudgetExceeded, RateLimited))
    assert src.http.requests_remaining == 50 + src.http.max_retries  # Room for every retry.
    assert src.archive_newest == NOW - timedelta(hours=1)
    assert src.fallback_horizon() == NOW - timedelta(hours=1)


def test_fallback_horizon_is_unknown_before_the_probes_succeed() -> None:
    src, _, _ = make_source(Archive([]))
    assert src.fallback_horizon() is None


def test_cli_a_too_small_request_budget_makes_the_source_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, archive, out = run_stale_scan(tmp_path, monkeypatch, "--max-requests", "20")
    assert result.exit_code != 0
    assert "--max-requests" in result.output and "--allow-stale-source" in result.output
    assert not out.exists()
    assert not coverage_requests(archive)


# -- CLI end to end -------------------------------------------------------------------


def build_world() -> list[dict[str, Any]]:
    """Six steady posters that stop 2 hours before the cutoff, six that carry on."""
    items: list[dict[str, Any]] = [pulse(), post_pulse()]
    start = CUTOFF - timedelta(days=30)
    for who, stop in [(f"bot_acct_{i}", CUTOFF - timedelta(hours=2)) for i in range(6)] + [
        (f"human_acct_{i}", NOW - timedelta(hours=1)) for i in range(6)
    ]:
        moment, n = start + timedelta(minutes=7 * len(items)), 0
        while moment < stop:
            sub = "worldnews"
            items.append(comment(f"{who}-{n}", who, sub, moment, body=f"{who} says {n % 3}"))
            moment += timedelta(hours=5)
            n += 1
    items += traffic(start, NOW - timedelta(hours=1), kind="posts")  # Coverage check.
    return items


def test_cli_scan_reddit_archive_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(build_world())

    def patched_client(**kwargs: Any) -> HttpClient:
        return HttpClient(
            **kwargs,
            transport=httpx.MockTransport(archive),
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", patched_client)
    out = tmp_path / "out.json"
    result = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--subreddit", "worldnews", "--n", "12",
            "--pool-factor", "1", "--seed", "3", "--now", "2026-10-14T00:00:00Z",
            "--db", str(tmp_path / "c.db"), "--out", str(out), "--max-requests", "500",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    flagged = {c["user_id"] for c in payload["candidates"]}
    assert flagged == {f"bot_acct_{i}" for i in range(6)}
    assert payload["summary"]["sampled"] == 12
    assert all(r.headers["user-agent"] == USER_AGENT for r in archive.requests)
    assert {r.url.host for r in archive.requests} == {"arctic-shift.photon-reddit.com"}

    # Re-running with the cache needs no activity fetches, so the sample is reproducible.
    again = runner.invoke(app, ["report", "--run-id", "1", "--db", str(tmp_path / "c.db")])
    assert again.exit_code == 0, again.output


def test_cli_rejects_subreddit_flag_for_x() -> None:
    result = runner.invoke(app, ["scan", "--platform", "x", "--subreddit", "foo"])
    assert result.exit_code != 0


def test_cli_resume_probes_with_the_stored_seeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = build_world()
    for item in items:
        item["subreddit"] = "europe"
    items.append(post_pulse())  # worldnews is always probed as well.
    items += traffic(CUTOFF - timedelta(days=31), NOW - timedelta(hours=1))  # Coverage check.
    items += traffic(CUTOFF - timedelta(days=31), NOW - timedelta(hours=1), kind="posts")
    archive = Archive(items)

    def patched_client(**kwargs: Any) -> HttpClient:
        return HttpClient(
            **kwargs,
            transport=httpx.MockTransport(archive),
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", patched_client)
    db = str(tmp_path / "c.db")
    first = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--subreddit", "europe", "--n", "12",
            "--pool-factor", "1", "--seed", "3", "--now", "2026-10-14T00:00:00Z",
            "--db", db, "--max-requests", "500",
        ],
    )  # fmt: skip
    assert first.exit_code == 0, first.output
    archive.requests.clear()
    again = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--from-run", "1", "--refresh",
            "--now", "2026-10-14T00:00:00Z", "--db", db,
        ],
    )  # fmt: skip
    assert again.exit_code == 0, again.output
    probes = [r for r in archive.requests if "author" not in r.url.params]
    assert probes
    assert {p.url.params["subreddit"] for p in probes} == {"europe", "worldnews"}


def stale_world() -> list[dict[str, Any]]:
    """An archive whose newest comment is one day after the cutoff (minimum is three)."""
    horizon = CUTOFF + timedelta(days=1)
    items: list[dict[str, Any]] = [
        comment("pulse", "someone", "worldnews", horizon),
        post("ppulse", "someone", "worldnews", horizon),
    ]
    items += traffic(CUTOFF - timedelta(days=31), horizon)  # The coverage check passes.
    items += traffic(CUTOFF - timedelta(days=31), horizon, kind="posts")
    for who in (f"acct_{i}" for i in range(4)):
        moment, n = CUTOFF - timedelta(days=20), 0
        while moment < CUTOFF - timedelta(hours=2):
            items.append(comment(f"{who}-{n}", who, "worldnews", moment))
            moment += timedelta(hours=9)
            n += 1
    return items


def run_stale_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
    archive: Archive | None = None,
) -> tuple[Any, Archive, Path]:
    archive = archive or Archive(stale_world())

    def patched_client(**kwargs: Any) -> HttpClient:
        return HttpClient(
            **kwargs,
            transport=httpx.MockTransport(archive),
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", patched_client)
    out = tmp_path / "out.json"
    result = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--subreddit", "worldnews", "--n", "4",
            "--pool-factor", "1", "--seed", "3", "--now", "2026-10-14T00:00:00Z",
            "--db", str(tmp_path / "c.db"), "--out", str(out), "--max-requests", "500", *extra,
        ],
    )  # fmt: skip
    return result, archive, out


def test_cli_stale_archive_aborts_before_any_discovery_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, archive, out = run_stale_scan(tmp_path, monkeypatch)
    assert result.exit_code != 0
    assert "--allow-stale-source" in result.output
    assert not out.exists()
    # Only the horizon probes and the coverage check ran: no discovery, no fetches.
    assert archive.requests
    for request in archive.requests:
        assert "author" not in request.url.params
        assert "author" not in request.url.params["fields"]
        assert request.url.params["subreddit"] == "worldnews"


def test_cli_allow_stale_source_runs_and_forces_provisional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, archive, out = run_stale_scan(tmp_path, monkeypatch, "--allow-stale-source")
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert payload["run"]["provisional"] is True
    assert payload["run"]["effective_now"].startswith("2026-10-11")
    assert payload["summary"]["sampled"] > 0
    assert len(archive.requests) > 1


def unreadable_horizon_world() -> Archive:
    """A fresh archive whose horizon probes (newest-first, limit 1) all fail with a 400."""

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("sort") == "desc" and q.get("limit") == "1" and "author" not in q:
            return httpx.Response(400, json={"error": "nope"})
        return None

    return Archive(stale_world(), hook=hook)


def test_cli_unreadable_horizon_aborts_before_any_discovery_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, archive, out = run_stale_scan(tmp_path, monkeypatch, archive=unreadable_horizon_world())
    assert result.exit_code != 0
    assert "--allow-stale-source" in result.output
    assert not out.exists()
    # Only the failing horizon probes (plus their one retry) ran: no discovery, no fetches.
    assert archive.requests
    for request in archive.requests:
        assert "after" not in request.url.params
        assert "author" not in request.url.params
        assert request.url.params["limit"] == "1"


def test_cli_unreadable_horizon_with_allow_stale_source_is_all_provisional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, archive, out = run_stale_scan(
        tmp_path, monkeypatch, "--allow-stale-source", archive=unreadable_horizon_world()
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert payload["run"]["provisional"] is True
    assert payload["summary"]["sampled"] > 0
    assert any("after" in r.url.params or "author" in r.url.params for r in archive.requests)


def test_cli_coverage_gap_after_the_cutoff_makes_the_run_provisional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day missing from the archive after the cutoff must not count as silence."""
    gap_start, gap_end = ts(CUTOFF + timedelta(days=1)), ts(CUTOFF + timedelta(days=2))
    world = [i for i in build_world() if not gap_start <= i["created_utc"] < gap_end]
    first, _, out = run_stale_scan(tmp_path / "a", monkeypatch, archive=Archive(world))
    assert first.exit_code != 0
    assert "2026-10-11T00:00" in first.output and "--allow-stale-source" in first.output
    assert not out.exists()
    again, _, out = run_stale_scan(
        tmp_path / "b", monkeypatch, "--allow-stale-source", archive=Archive(world)
    )
    assert again.exit_code == 0, again.output
    assert "day starting 2026-10-11 00:00 UTC" in again.output
    payload = json.loads(out.read_text())
    assert payload["run"]["provisional"] is True
    assert payload["run"]["effective_now"].startswith("2026-10-11T00:00")


def test_cli_failed_coverage_check_with_allow_stale_source_scores_as_of_the_archive_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probed horizon survives a coverage check that cannot run; all stays provisional."""
    archive = Archive(build_world())
    result, _, out = run_stale_scan(tmp_path, monkeypatch, "--max-requests", "120", archive=archive)
    assert result.exit_code != 0
    assert "coverage check needs at least" in result.output
    assert not out.exists()
    archive = Archive(build_world())
    result, _, out = run_stale_scan(
        tmp_path / "b",
        monkeypatch,
        "--allow-stale-source",
        "--max-requests",
        "120",
        archive=archive,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert payload["run"]["provisional"] is True
    # The newest archived item is one hour before now, so scoring stops there, not at now.
    assert payload["run"]["effective_now"].startswith("2026-10-13T23:00")
    assert payload["candidates"]
    assert all(c["provisional"] for c in payload["candidates"])


def test_missing_seed_list_names_the_shared_reddit_section(tmp_path: Path) -> None:
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text("x:\n  queries: [a]\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"'reddit\.subreddits'"):
        load_seeds("reddit-archive", seeds)


def test_placebo_discovery_searches_the_window_moved_back() -> None:
    # The archive searches by time, so a placebo sample k weeks back is discovered with
    # `after` / `before` inside the baseline window moved back by k weeks.
    week = timedelta(weeks=1)
    inside = CUTOFF - week - timedelta(hours=5)
    items = [
        comment("1", "earlier", "a", inside),
        comment("2", "later_only", "a", CUTOFF - timedelta(hours=5)),
        pulse("a"),
    ]
    archive = Archive(items)
    src, _, _ = make_source(archive, baseline_days=1, dry_pages=40)
    seeds = SeedSet("reddit-archive", ("a",))
    assert src.placebo_discovery_problem(seeds) is None
    with src.shifted(week, now=NOW - week, seed=5):
        accounts = list(src.discover(seeds, 50))
    assert [a.handle for a in accounts] == ["earlier"]
    pages = [p for p in map(archive.params, range(len(archive.requests))) if "after" in p]
    start, end = ts(CUTOFF - week - timedelta(days=1)), ts(CUTOFF - week)
    assert pages and all(start - 1 <= int(p["after"]) < int(p["before"]) <= end for p in pages)
    assert src.config.cutoff == CUTOFF  # Restored.
