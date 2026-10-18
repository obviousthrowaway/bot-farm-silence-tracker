"""Tests for the X adapter. No network: every request goes through httpx.MockTransport."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker import cli
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import Account, content_hash
from bot_farm_silence_tracker.pipeline import ScanSettings, run_scan, storable_options
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import SourceConfig
from bot_farm_silence_tracker.sources.errors import (
    AccountSkipped,
    AccountUnavailable,
    BudgetExceeded,
    RateLimited,
    SourceAuthError,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.registry import load_source_class
from bot_farm_silence_tracker.sources.x import (
    ENDPOINT_LIMITS,
    MICRO,
    POST_PRICE_USD,
    SKIP_CREATED_AFTER_CUTOFF,
    SKIP_LOW_TWEET_COUNT,
    USER_PRICE_USD,
    EndpointLimit,
    RateGate,
    UsdBudget,
    XSource,
)
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF, NOW

TOKEN = "test-token-SECRET-123"
CRED = {"BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN": TOKEN}
SEARCH = "/2/tweets/search/recent"
BASELINE = CUTOFF - timedelta(days=30)
OLD = datetime(2020, 1, 1, tzinfo=UTC)  # Created long before the cutoff.


class Clock:
    """A fake wall clock whose ``sleep`` advances time."""

    def __init__(self) -> None:
        self.now = NOW.timestamp()
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


Handler = Callable[[httpx.Request], httpx.Response]


def make_http(handler: Handler, sleep: Callable[[float], None] | None = None, **kw: Any):
    return HttpClient(
        transport=httpx.MockTransport(handler),
        sleep=sleep or (lambda _s: None),
        clock=lambda: 0.0,
        requests_per_second=1000.0,
        burst=1000.0,
        **kw,
    )


def make_source(
    handler: Handler,
    *,
    ledger: SpendLedger | None = None,
    now: datetime = NOW,
    baseline_start: datetime | None = BASELINE,
    min_posts: int | None = None,
    http_sleep: Callable[[float], None] | None = None,
    **options: Any,
) -> tuple[XSource, Clock, list[httpx.Request]]:
    """Build an XSource. ``max_usd`` defaults to "250"; pass ``max_usd=None`` to omit it."""
    clock = Clock()
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    options.setdefault("max_usd", "250")
    if options["max_usd"] is None:
        del options["max_usd"]
    config = SourceConfig(
        http=make_http(recording, http_sleep),
        credentials=CRED,
        now=now,
        cutoff=CUTOFF,
        options=options,
        baseline_start=baseline_start,
        ledger=ledger,
        min_posts=min_posts,
    )
    return XSource(config, sleep=clock.sleep, clock=clock.time), clock, seen


def user(uid: str, created: str | None = "2020-01-02T03:04:05.000Z", tweets: int = 500) -> dict:
    out: dict[str, Any] = {
        "id": uid,
        "username": f"user{uid}",
        "public_metrics": {"tweet_count": tweets},
    }
    if created:
        out["created_at"] = created
    return out


def search_page(authors: list[str], next_token: str | None = None, prefix: str = "t") -> dict:
    body: dict[str, Any] = {
        "data": [{"id": f"{prefix}{i}-{a}", "author_id": a} for i, a in enumerate(authors)],
        "includes": {"users": [user(a) for a in dict.fromkeys(authors)]},
        "meta": {"result_count": len(authors)},
    }
    if next_token:
        body["meta"]["next_token"] = next_token
    return body


def tweet(tid: str, ts: str, text: str = "hello", **extra: Any) -> dict:
    return {"id": tid, "created_at": ts, "text": text, **extra}


def j(body: dict, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def usd(source: XSource) -> float:
    return source.budget.spent / MICRO


# --------------------------------------------------------------------- registry


def test_registry_loads_x_source() -> None:
    cls = load_source_class("x")
    assert cls is XSource
    assert cls.name == "x"
    assert cls.required_credentials == ("BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN",)
    assert cls.default_rps == 1.0
    assert cls.default_pool_factor == 1.0


def test_bearer_header_is_sent() -> None:
    source, _, seen = make_source(lambda r: j(search_page(["1"])))
    list(source.discover(SeedSet("x", ("q",)), 1))
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------- discover


def test_discover_round_robin_dedup_and_expansion_created_at() -> None:
    pages = {
        ("A", None): search_page(["1", "2"], "a2", "a"),
        ("B", None): search_page(["2", "3"], "b2", "b"),
        ("A", "a2"): search_page(["4", "1"], None, "c"),
        ("B", "b2"): search_page(["5"], None, "d"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        return j(pages[(q, request.url.params.get("next_token"))])

    source, _, seen = make_source(handler)
    accounts = list(source.discover(SeedSet("x", ("A", "B")), 100))

    assert [a.user_id for a in accounts] == ["1", "2", "3", "4", "5"]  # De-duplicated.
    order = [(r.url.params["query"], r.url.params.get("next_token")) for r in seen]
    assert order == [("A", None), ("B", None), ("A", "a2"), ("B", "b2")]  # Round robin.
    first = accounts[0]
    assert first.platform == "x"
    assert first.handle == "user1"
    assert first.created_at == datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert first.meta["tweet_count"] == 500
    assert set(first.meta) == {"tweet_count", "tweet_count_at"}
    assert len(seen) == 4  # created_at came from the expansion: no hydrate calls.
    params = seen[0].url.params
    assert params["max_results"] == "100"
    assert params["expansions"] == "author_id"
    assert params["user.fields"] == "created_at,public_metrics,username"
    assert seen[0].url.path == SEARCH


def _z(when: datetime) -> str:
    """A time as the X API takes it."""
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_search_ends_at_the_cutoff_and_starts_at_baseline_when_in_window() -> None:
    # The baseline start (30 days back) is outside the 7-day window: no start_time.
    source, _, seen = make_source(lambda r: j(search_page(["1"])))
    list(source.discover(SeedSet("x", ("q",)), 1))
    assert seen[0].url.params["end_time"] == _z(CUTOFF)
    assert "start_time" not in seen[0].url.params

    inside = CUTOFF - timedelta(days=1)
    source, _, seen = make_source(lambda r: j(search_page(["1"])), baseline_start=inside)
    list(source.discover(SeedSet("x", ("q",)), 1))
    assert seen[0].url.params["start_time"] == _z(inside)

    source, _, seen = make_source(lambda r: j(search_page(["1"])), full_archive="true")
    list(source.discover(SeedSet("x", ("q",)), 1))
    assert seen[0].url.path == "/2/tweets/search/all"
    assert seen[0].url.params["start_time"] == _z(CUTOFF - timedelta(days=30))
    assert seen[0].url.params["end_time"] == _z(CUTOFF)


def test_recent_search_window_after_cutoff_warns_and_spends_nothing() -> None:
    # The 7-day window is measured from the wall clock, not from --now.
    source, clock, seen = make_source(lambda r: j(search_page(["1"])))
    clock.now = (CUTOFF + timedelta(days=9)).timestamp()
    assert list(source.discover(SeedSet("x", ("q",)), 5)) == []
    assert seen == []
    (warning,) = source.drain_warnings()
    assert "full_archive=true" in warning


def test_recent_search_window_ignores_a_pinned_now() -> None:
    # --now pinned 9 days after the cutoff, but the real time is 4 days after it:
    # recent search still reaches the cutoff, so discovery runs.
    source, _, seen = make_source(lambda r: j(search_page(["1"])), now=CUTOFF + timedelta(days=9))
    assert len(list(source.discover(SeedSet("x", ("q",)), 5))) == 1
    assert len(seen) == 1


def test_discover_stops_lazily_at_pool_limit() -> None:
    source, _, seen = make_source(lambda r: j(search_page(["1", "2", "3"], "more")))
    stream = source.discover(SeedSet("x", ("A", "B")), 2)
    assert [a.user_id for a in stream] == ["1", "2"]
    assert len(seen) == 1  # Stopped mid-page: no second query, no next page.


def test_discover_ends_when_all_queries_are_exhausted() -> None:
    source, _, seen = make_source(lambda r: j(search_page(["1"])))
    assert len(list(source.discover(SeedSet("x", ("A", "B")), 50))) == 1
    assert len(seen) == 2


def test_query_building_options_use_option_bool() -> None:
    source, _, seen = make_source(
        lambda r: j(search_page(["1"])), lang="uk", exclude_retweets="true"
    )
    list(source.discover(SeedSet("x", ("#news",)), 1))
    assert seen[0].url.params["query"] == "#news -is:retweet lang:uk"
    # The string "false" must mean false (bool("false") is True).
    plain, _, seen2 = make_source(
        lambda r: j(search_page(["1"])), exclude_retweets="false", full_archive="false"
    )
    list(plain.discover(SeedSet("x", ("#news",)), 1))
    assert seen2[0].url.params["query"] == "#news"
    assert seen2[0].url.path == SEARCH
    with pytest.raises(ValueError, match="exclude_retweets"):
        make_source(lambda r: j({}), exclude_retweets="maybe")


def test_overlong_query_is_skipped_with_a_warning() -> None:
    source, _, seen = make_source(lambda r: j(search_page(["1"])))
    accounts = list(source.discover(SeedSet("x", ("x" * 600, "ok")), 5))
    assert [a.user_id for a in accounts] == ["1"]
    assert [r.url.params["query"] for r in seen] == ["ok"]
    assert any("longer than 512" in w for w in source.drain_warnings())


def test_discovery_cap_stops_discovery_with_a_warning() -> None:
    pages = iter(range(100))

    def handler(request: httpx.Request) -> httpx.Response:
        k = next(pages)
        return j(search_page([f"{k}a", f"{k}b"], "more", prefix=f"p{k}"))

    # Each page costs $0.03 but is checked at its $1.50 worst case, so with a $1.60
    # discovery cap a page is sent only while at most $0.10 has been spent: 4 pages.
    source, _, seen = make_source(handler, max_discovery_usd="1.6")
    accounts = list(source.discover(SeedSet("x", ("q",)), 1000))
    assert len(seen) == 4
    assert len(accounts) == 8
    assert source.budget.discovery_spent == 4 * (2 * 5_000 + 2 * 10_000)
    assert any("discovery stopped at its cap of $1.60" in w for w in source.drain_warnings())


def test_discovery_cap_defaults_to_30_percent_of_max_usd() -> None:
    source, _, _ = make_source(lambda r: j({}), max_usd="100")
    assert source.budget.discovery_cap == 30 * MICRO
    source, _, _ = make_source(lambda r: j({}), max_usd="10", max_discovery_usd="50")
    assert source.budget.discovery_cap == 3 * MICRO  # The option can lower the share only.
    source, _, _ = make_source(lambda r: j({}), max_usd="10", max_discovery_usd="1")
    assert source.budget.discovery_cap == 1 * MICRO


# ----------------------------------------------------------------------- hydrate


def test_hydrate_is_a_noop_when_created_at_is_set() -> None:
    source, _, seen = make_source(lambda r: j({}))
    acct = Account("x", "1", "a", NOW)
    assert source.hydrate(acct) is acct
    assert seen == []


def test_hydrate_looks_up_only_the_account_and_maps_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            body = search_page(["1", "2", "3", "4"])
            body["includes"]["users"] = []  # No expansion: hydrate must do the work.
            return j(body)
        assert request.url.path == "/2/users"
        ids = request.url.params["ids"].split(",")
        assert len(ids) == 1  # No padding with unsampled ids.
        uid = ids[0]
        if uid == "3":
            return j(
                {
                    "errors": [
                        {
                            "title": "Not Found Error",
                            "resource_type": "user",
                            "resource_id": "3",
                            "detail": "Could not find user with id: [3].",
                        }
                    ]
                }
            )
        if uid == "4":
            return j(
                {
                    "errors": [
                        {
                            "title": "Forbidden",
                            "resource_type": "user",
                            "resource_id": "4",
                            "detail": "User has been suspended: [4].",
                        }
                    ]
                }
            )
        return j({"data": [user(uid, "2024-05-06T00:00:00.000Z")]})

    source, _, seen = make_source(handler)
    accounts = {a.user_id: a for a in source.discover(SeedSet("x", ("q",)), 10)}
    assert all(a.created_at is None for a in accounts.values())
    source.hydrate(accounts["2"])
    assert accounts["2"].created_at == datetime(2024, 5, 6, tzinfo=UTC)
    assert accounts["2"].handle == "user2"
    assert accounts["2"].meta["tweet_count"] == 500
    for uid, status in (("3", "deleted"), ("4", "suspended")):
        with pytest.raises(AccountUnavailable) as err:
            source.hydrate(accounts[uid])
        assert err.value.status == status
    assert sum(r.url.path == "/2/users" for r in seen) == 3  # "1" was never looked up.
    source.hydrate(accounts["2"])  # Cached: no new request.
    assert sum(r.url.path == "/2/users" for r in seen) == 3


def test_prefetch_was_removed() -> None:
    assert not hasattr(XSource, "prefetch")


# ----------------------------------------------------------------- fetch_activity

SINCE = datetime(2026, 9, 1, tzinfo=UTC)
UNTIL = datetime(2026, 10, 14, 12, 30, 5, 250000, tzinfo=UTC)


def test_timeline_pagination_kind_mapping_and_params() -> None:
    page1 = {
        "data": [
            tweet("1", "2026-10-01T10:00:00.000Z", "plain"),
            tweet(
                "2",
                "2026-10-01T11:00:00.000Z",
                "a reply",
                in_reply_to_user_id="9",
                referenced_tweets=[{"type": "replied_to", "id": "88"}],
            ),
            tweet(
                "3",
                "2026-10-01T12:00:00.000Z",
                "RT @x: hi",
                referenced_tweets=[{"type": "retweeted", "id": "77"}],
            ),
        ],
        "meta": {"result_count": 3, "next_token": "p2"},
    }
    page2 = {
        "data": [
            tweet(
                "4",
                "2026-10-02T10:00:00.000Z",
                "quoting",
                referenced_tweets=[{"type": "quoted", "id": "5"}],
            ),
            tweet("5", "2026-08-01T10:00:00.000Z", "too old"),  # Outside the window.
        ],
        "meta": {"result_count": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2/users/42/tweets"
        return j(page2 if request.url.params.get("pagination_token") == "p2" else page1)

    source, _, seen = make_source(handler)
    acct = Account("x", "42", "h", OLD)
    acts = source.fetch_activity(acct, SINCE, UNTIL)

    assert [a.kind for a in acts] == ["tweet", "reply", "repost", "quote"]
    assert acts[0].community is None
    assert acts[0].content_hash == content_hash("plain")
    assert acts[0].ts == datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert all(a.platform == "x" and a.user_id == "42" for a in acts)
    assert len(seen) == 2
    p = seen[0].url.params
    assert p["start_time"] == "2026-09-01T00:00:00Z"
    assert p["end_time"] == "2026-10-14T12:30:06Z"  # Rounded up, not truncated.
    assert p["max_results"] == "100"
    assert p["tweet.fields"] == "created_at,in_reply_to_user_id,referenced_tweets"
    assert "pagination_token" not in p
    assert seen[1].url.params["pagination_token"] == "p2"
    assert source.budget.posts == 5
    assert acct.meta["truncated"] is False


def test_truncated_flag_when_depth_limit_stops_before_since() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("pagination_token") or 0)
        data = [
            tweet(f"{page}-{i}", (UNTIL - timedelta(minutes=page * 100 + i + 1)).isoformat())
            for i in range(100)
        ]
        meta: dict[str, Any] = {"result_count": 100}
        if page < 31:
            meta["next_token"] = str(page + 1)
        return j({"data": data, "meta": meta})

    source, _, seen = make_source(handler, max_usd="100")
    acct = Account("x", "1", "h")
    acts = source.fetch_activity(acct, SINCE, UNTIL)
    assert len(seen) == 32 and len(acts) == 3200
    assert acct.meta["truncated"] is True


def test_reply_detected_from_in_reply_to_user_id_alone() -> None:
    body = {"data": [tweet("1", "2026-10-01T10:00:00Z", in_reply_to_user_id="3")]}
    source, _, _ = make_source(lambda r: j(body))
    (act,) = source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    assert act.kind == "reply"


def test_empty_timeline() -> None:
    source, _, _ = make_source(lambda r: j({"meta": {"result_count": 0}}))
    assert source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL) == []


def test_field_param_is_configurable() -> None:
    source, _, seen = make_source(lambda r: j({"meta": {}}), field_param="post.fields")
    source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    assert seen[0].url.params["post.fields"] == "created_at,in_reply_to_user_id,referenced_tweets"
    assert "tweet.fields" not in seen[0].url.params
    with pytest.raises(ValueError):
        make_source(lambda r: j({}), field_param="bogus.fields")


def test_low_tweet_count_and_new_accounts_are_skipped_at_zero_cost() -> None:
    source, _, seen = make_source(lambda r: j({"meta": {}}), min_posts=20)
    few = Account("x", "1", "h", datetime(2020, 1, 1, tzinfo=UTC), {"tweet_count": 19})
    new = Account("x", "2", "n", CUTOFF + timedelta(hours=1), {"tweet_count": 900})
    for acct in (few, new):
        with pytest.raises(AccountSkipped) as err:
            source.fetch_activity(acct, SINCE, UNTIL)
        assert err.value.status == "skipped"
    assert seen == []
    assert few.meta["skipped"] == SKIP_LOW_TWEET_COUNT
    assert few.meta["skip_min_posts"] == 20
    assert new.meta["skipped"] == SKIP_CREATED_AFTER_CUTOFF
    assert source.budget.spent == 0
    assert "2 timelines skipped at zero cost" in source.cost_summary()
    assert any("zero cost" in w for w in source.drain_warnings())
    enough = Account("x", "3", "e", datetime(2020, 1, 1, tzinfo=UTC), {"tweet_count": 20})
    source.fetch_activity(enough, SINCE, UNTIL)
    assert len(seen) == 1


def test_projected_account_cost_is_checked_before_the_first_page() -> None:
    source, _, seen = make_source(lambda r: j({"meta": {}}), max_usd="5")
    busy = Account("x", "1", "b", NOW - timedelta(days=100), {"tweet_count": 3000})
    with pytest.raises(AccountSkipped, match="projected") as err:
        source.fetch_activity(busy, SINCE, UNTIL)
    assert err.value.status == "skipped_budget"
    assert busy.meta["skipped"] == "projected_cost_exceeds_budget"
    assert seen == []
    # A smaller account still fits, so the scan goes on with it.
    small = Account("x", "2", "s", NOW - timedelta(days=100), {"tweet_count": 50})
    assert source.fetch_activity(small, SINCE, UNTIL) == []
    assert len(seen) == 1
    assert "skipped" not in small.meta
    assert "1 timelines skipped as over budget" in source.cost_summary()


def test_scan_stops_only_when_not_even_one_page_fits() -> None:
    source, _, seen = make_source(lambda r: j({"meta": {}}), max_usd="0.49")
    small = Account("x", "2", "s", NOW - timedelta(days=100), {"tweet_count": 50})
    with pytest.raises(BudgetExceeded, match="timeline page"):
        source.fetch_activity(small, SINCE, UNTIL)
    assert seen == []


# ----------------------------------------------------------------------- errors


def test_user_error_in_200_body_means_unavailable() -> None:
    cases = [
        ("Forbidden", "User has been suspended: [1].", "suspended"),
        ("Not Found Error", "gone", "deleted"),
        ("Authorization Error", "Sorry, you are not authorized to see the user.", "private"),
    ]
    for title, detail, status in cases:
        body = {"errors": [{"title": title, "resource_type": "user", "detail": detail}]}
        source, _, _ = make_source(lambda r, b=body: j(b))
        with pytest.raises(AccountUnavailable) as err:
            source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
        assert err.value.status == status


def test_auth_error_in_200_body_is_fatal() -> None:
    body = {"errors": [{"title": "Unauthorized", "detail": "bad token"}]}
    source, _, _ = make_source(lambda r: j(body))
    with pytest.raises(SourceAuthError):
        list(source.discover(SeedSet("x", ("q",)), 1))


def test_benign_error_in_200_body_is_warned_not_raised() -> None:
    body = {
        "data": [tweet("1", "2026-10-01T10:00:00Z")],
        "errors": [{"title": "Not Found Error", "resource_type": "tweet", "detail": "x"}],
    }
    source, _, _ = make_source(lambda r: j(body))
    assert len(source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)) == 1
    assert len(source.ignored_errors) == 1
    assert any("partial errors" in w for w in source.drain_warnings())


def test_401_raises_source_auth_error() -> None:
    source, _, _ = make_source(lambda r: j({"title": "Unauthorized", "detail": "no"}, 401))
    with pytest.raises(SourceAuthError):
        list(source.discover(SeedSet("x", ("q",)), 1))
    with pytest.raises(SourceAuthError):
        source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)


@pytest.mark.parametrize(
    "body",
    [
        {"title": "Client Forbidden", "reason": "client-not-enrolled", "detail": "Not enrolled"},
        {"title": "Forbidden", "type": "https://api.x.com/2/problems/client-forbidden"},
        {"title": "Unsupported Authentication", "detail": "unsupported-authentication"},
    ],
)
def test_403_with_app_level_marker_is_auth_error(body: dict) -> None:
    source, _, _ = make_source(lambda r: j(body, 403))
    with pytest.raises(SourceAuthError):
        source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    with pytest.raises(SourceAuthError):
        list(source.discover(SeedSet("x", ("q",)), 1))


def test_user_level_404_and_403_on_timeline_mean_unavailable() -> None:
    acct = Account("x", "1", "h", OLD)
    source, _, _ = make_source(lambda r: j({"title": "Not Found Error", "detail": "no"}, 404))
    with pytest.raises(AccountUnavailable) as err:
        source.fetch_activity(acct, SINCE, UNTIL)
    assert err.value.status == "deleted"

    body = {"title": "Forbidden", "detail": "User has been suspended: [1]."}
    source, _, _ = make_source(lambda r: j(body, 403))
    with pytest.raises(AccountUnavailable) as err:
        source.fetch_activity(acct, SINCE, UNTIL)
    assert err.value.status == "suspended"


def test_unexplained_403_means_private_until_three_in_a_row() -> None:
    responses = iter(
        [
            j({"title": "Forbidden", "detail": "Forbidden"}, 403),
            j({"title": "Forbidden", "detail": "Forbidden"}, 403),
            j({"meta": {}}),  # A success resets the streak.
            j({"title": "Forbidden", "detail": "Forbidden"}, 403),
            j({"title": "Forbidden", "detail": "Forbidden"}, 403),
            j({"title": "Forbidden", "detail": "Forbidden"}, 403),
        ]
    )
    source, _, _ = make_source(lambda r: next(responses))
    acct = Account("x", "1", "h", OLD)
    for _ in range(2):
        with pytest.raises(AccountUnavailable) as err:
            source.fetch_activity(acct, SINCE, UNTIL)
        assert err.value.status == "private"
    assert source.fetch_activity(acct, SINCE, UNTIL) == []
    for _ in range(2):
        with pytest.raises(AccountUnavailable):
            source.fetch_activity(acct, SINCE, UNTIL)
    with pytest.raises(SourceAuthError, match="3 unexplained 403"):
        source.fetch_activity(acct, SINCE, UNTIL)


def test_bare_403_outside_user_scope_is_auth_error() -> None:
    source, _, _ = make_source(lambda r: j({"title": "Forbidden", "detail": "Forbidden"}, 403))
    with pytest.raises(SourceAuthError):
        list(source.discover(SeedSet("x", ("q",)), 1))


def test_other_client_errors_and_402() -> None:
    source, _, _ = make_source(lambda r: j({"title": "Invalid Request", "detail": "bad"}, 400))
    with pytest.raises(SourceError, match="400"):
        source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    source, _, seen = make_source(lambda r: j({"title": "CreditsDepleted", "detail": "no"}, 402))
    with pytest.raises(BudgetExceeded, match="402"):
        source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    assert len(seen) == 1


def test_429_usage_cap_is_budget_exceeded_without_retry() -> None:
    body = {
        "title": "UsageCapExceeded",
        "detail": "Usage cap exceeded: Monthly product cap",
        "type": "https://api.x.com/2/problems/usage-capped",
    }
    source, _, seen = make_source(lambda r: j(body, 429))
    with pytest.raises(BudgetExceeded, match="usage cap"):
        source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    assert len(seen) == 1


def test_429_waits_for_the_epoch_reset_header() -> None:
    http_sleeps: list[float] = []
    box: list[Clock] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not responses:
            responses.append(j({"title": "Too Many Requests"}, 429))
            reset = str(int(box[0].now) + 30)
            return j({"title": "Too Many Requests"}, 429, {"x-rate-limit-reset": reset})
        return j({"meta": {}})

    source, clock, seen = make_source(handler, http_sleep=http_sleeps.append)
    box.append(clock)
    source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    assert len(seen) == 2
    assert http_sleeps == [pytest.approx(31.0)]  # Reset in 30s plus a 1s margin.


def test_429_with_a_reset_beyond_the_cap_stops_with_rate_limited() -> None:
    box: list[Clock] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reset = str(int(box[0].now) + 5000)
        return j({"title": "Too Many Requests"}, 429, {"x-rate-limit-reset": reset})

    source, clock, _ = make_source(handler)
    box.append(clock)
    with pytest.raises(RateLimited):
        source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)


def test_transport_failure_is_charged_at_worst_case() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return j({"data": [tweet("1", "2026-10-01T10:00:00Z")], "meta": {}})

    source, _, _ = make_source(handler)
    source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    # Worst case of the failed attempt (100 posts) plus the one post returned.
    assert source.budget.spent == 100 * 5_000 + 5_000
    assert source.budget.failed_attempts == 1
    assert "failed attempts charged at the worst case" in source.cost_summary()


def test_search_retry_after_a_failed_attempt_respects_the_discovery_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    # The failed attempt is charged $1.50, leaving no room for a retry under $2.
    source, _, seen = make_source(handler, max_usd="10", max_discovery_usd="2")
    with pytest.raises(BudgetExceeded, match="discovery cap"):
        list(source.discover(SeedSet("x", ("q",)), 5))
    assert len(seen) == 1
    assert source.budget.discovery_spent == 1_500_000


def test_connect_error_is_not_charged() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return j({"meta": {}})

    source, _, _ = make_source(handler)
    source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    assert source.budget.spent == 0


def test_token_never_appears_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    source, _, _ = make_source(handler)
    with pytest.raises(SourceError) as err:
        source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
    assert TOKEN not in str(err.value)
    assert TOKEN not in repr(source)
    assert TOKEN not in repr(source.config)
    for body, status in (({"detail": "no"}, 401), ({"detail": "x"}, 403), ({}, 400)):
        bad, _, _ = make_source(lambda r, b=body, s=status: j(b, s))
        with pytest.raises(SourceError) as err:
            bad.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
        assert TOKEN not in str(err.value)
    assert TOKEN not in caplog.text


# ----------------------------------------------------------------------- budget


def test_cost_math_counts_posts_and_expansion_users() -> None:
    body = search_page(["1", "2", "3"])  # 3 posts, 3 distinct users.
    source, _, _ = make_source(lambda r: j(body))
    list(source.discover(SeedSet("x", ("q",)), 10))
    assert source.budget.posts == 3
    assert source.budget.users == 3
    assert source.budget.spent == 3 * 5_000 + 3 * 10_000  # Integer micro-USD.
    assert isinstance(source.budget.spent, int)
    assert "$0.04" in source.cost_summary()


def test_same_day_repeats_are_charged_once() -> None:
    pages = iter([search_page(["1", "2"], "n", "a"), search_page(["2", "3"], None, "b")])
    source, _, _ = make_source(lambda r: j(next(pages)))
    list(source.discover(SeedSet("x", ("q",)), 10))
    assert source.budget.users == 3  # User 2 appeared twice but is billed once.
    assert source.budget.repeats == 1
    # A lookup of an already-billed user costs nothing more today.
    lookup, _, _ = make_source(lambda r: j({"data": [user("2")]}))
    lookup._charged_day = None
    lookup._lookup_users(["2"])
    before = lookup.budget.spent
    lookup._lookup_users(["2"])
    assert lookup.budget.spent == before


def test_repeat_dedup_resets_on_a_new_utc_day() -> None:
    source, clock, _ = make_source(lambda r: j({"data": [user("1")]}))
    source._lookup_users(["1"])
    source._lookup_users(["1"])
    assert source.budget.users == 1
    clock.now += 86400
    source._lookup_users(["1"])
    assert source.budget.users == 2


def test_prices_are_constants_validated_and_overridable() -> None:
    assert POST_PRICE_USD == 0.005
    assert USER_PRICE_USD == 0.010
    budget = UsdBudget(10.0, post_price=0.02, user_price=0.5)
    assert budget.cost(posts=10, users=2) == 1_200_000
    for bad in ({"post_price": -0.01}, {"user_price": -1.0}, {"cap": -5.0}):
        with pytest.raises(ValueError, match=">= 0"):
            UsdBudget(**({"cap": 1.0} | bad))
    with pytest.raises(ValueError):
        make_source(lambda r: j({}), post_price="-0.1")
    source, _, _ = make_source(
        lambda r: j(search_page(["1"])), post_price="0.1", user_price="1.0", max_usd="1000"
    )
    list(source.discover(SeedSet("x", ("q",)), 1))
    assert usd(source) == pytest.approx(1.1)


def test_no_cap_refuses_every_paid_request() -> None:
    source, _, seen = make_source(lambda r: j(search_page(["1"])), max_usd=None)
    with pytest.raises(BudgetExceeded, match="--max-usd"):
        list(source.discover(SeedSet("x", ("q",)), 1))
    assert seen == []


def test_budget_refuses_request_whose_worst_case_exceeds_cap() -> None:
    # Worst case for one timeline page: 100 posts = $0.50 > $0.40.
    source, _, seen = make_source(lambda r: j({"meta": {}}), max_usd="0.4")
    with pytest.raises(BudgetExceeded, match="raise --max-usd"):
        source.fetch_activity(Account("x", "1", "h"), SINCE, UNTIL)
    assert seen == []  # Refused before any request was sent.
    assert source.budget.spent == 0
    # In discovery the (smaller) discovery cap ends the stream with a warning instead.
    source, _, seen = make_source(lambda r: j(search_page(["1"])), max_usd="1.0")
    assert list(source.discover(SeedSet("x", ("q",)), 5)) == []
    assert seen == []
    assert any("discovery stopped" in w for w in source.drain_warnings())


def test_budget_stops_before_overspend_mid_timeline() -> None:
    hundred = [tweet(str(i), "2026-10-01T10:00:00Z") for i in range(100)]
    body = {"data": hundred, "meta": {"result_count": 100, "next_token": "more"}}
    source, _, seen = make_source(lambda r: j(body), max_usd="0.60")
    with pytest.raises(BudgetExceeded):
        source.fetch_activity(Account("x", "1", "h"), SINCE, UNTIL)
    assert len(seen) == 1
    assert usd(source) == pytest.approx(0.50)


def test_budget_can_spend_exactly_to_the_cap() -> None:
    hundred = [tweet(str(i), "2026-10-01T10:00:00Z") for i in range(100)]
    source, _, seen = make_source(lambda r: j({"data": hundred, "meta": {}}), max_usd="0.5")
    source.fetch_activity(Account("x", "1", "h"), SINCE, UNTIL)
    assert source.budget.spent == source.budget.cap
    with pytest.raises(BudgetExceeded):
        source.fetch_activity(Account("x", "2", "h"), SINCE, UNTIL)
    assert len(seen) == 1


def test_ledger_day_scope_counts_earlier_runs(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        storage.add_spend("x", 9.0, run_id=1)
        day = SpendLedger(storage, "x")
        day.bind_run(2)
        source, _, seen = make_source(lambda r: j(search_page(["1"])), ledger=day, max_usd="10")
        # $1 left: discovery may use 30% of it, less than one worst-case page.
        assert list(source.discover(SeedSet("x", ("q",)), 1)) == []
        assert source.budget.discovery_cap == 300_000
        assert seen == []
        storage.add_spend("x", 0.6, run_id=1)  # $0.40 left: not even one timeline page.
        with pytest.raises(BudgetExceeded, match="today across runs"):
            source.fetch_activity(Account("x", "1", "h", OLD), SINCE, UNTIL)
        assert seen == []

        run = SpendLedger(storage, "x")
        run.bind_run(3)
        source, _, seen = make_source(
            lambda r: j(search_page(["1"])), ledger=run, max_usd="10", budget_scope="run"
        )
        list(source.discover(SeedSet("x", ("q",)), 1))
        assert len(seen) == 1
        assert storage.spend_for_run(3, "x") == pytest.approx(0.015)
    with pytest.raises(ValueError, match="budget_scope"):
        make_source(lambda r: j({}), budget_scope="week")


# ------------------------------------------------------------------ rate limits


def test_endpoint_constants() -> None:
    assert ENDPOINT_LIMITS["search_recent"] == EndpointLimit(450, 900.0)
    assert ENDPOINT_LIMITS["timeline"] == EndpointLimit(10000, 900.0)
    assert ENDPOINT_LIMITS["users"] == EndpointLimit(300, 900.0)
    assert ENDPOINT_LIMITS["search_all"].min_interval == 1.0


def test_gate_enforces_window_limit_with_injected_sleep() -> None:
    clock = Clock()
    gate = RateGate({"e": EndpointLimit(2, 100.0)}, sleep=clock.sleep, clock=clock.time)
    gate.wait("e")
    gate.wait("e")
    assert clock.sleeps == []
    gate.wait("e")  # Third call within the window waits for the first to expire.
    assert clock.sleeps == [pytest.approx(100.0)]


def test_gate_enforces_one_request_per_second_for_full_archive() -> None:
    clock = Clock()
    gate = RateGate(sleep=clock.sleep, clock=clock.time)
    gate.wait("search_all")
    gate.wait("search_all")
    assert clock.sleeps == [pytest.approx(1.0)]
    gate.wait("timeline")  # Other endpoints are unaffected.
    assert len(clock.sleeps) == 1


# --------------------------------------------------------------------- estimate


def test_estimate_cost_from_public_metrics() -> None:
    source, _, seen = make_source(lambda r: j({}), max_usd="100", min_posts=20)
    window_days = (UNTIL - SINCE).total_seconds() / 86400
    old = Account("x", "1", "a", NOW - timedelta(days=1000), {"tweet_count": 2000})
    young = Account("x", "2", "b", NOW - timedelta(days=10), {"tweet_count": 25})
    unknown = Account("x", "3", "c", None, {})
    tiny = Account("x", "4", "d", NOW - timedelta(days=1000), {"tweet_count": 5})
    gone = Account("x", "5", "e", NOW - timedelta(days=1000), {"tweet_count": 900})
    gone.status = "suspended"
    est = source.estimate_cost([old, young, unknown, tiny, gone], SINCE, UNTIL)

    old_posts = 2000 / 1000 * window_days
    young_posts = min(25, 25 / 10 * window_days)  # Capped at lifetime tweet_count.
    mean = (old_posts + young_posts) / 2
    assert est.posts == round(old_posts + young_posts + mean)
    high_old = old_posts * 2
    high_young = 25.0
    assert est.high_posts == round(high_old + high_young + max(high_old, high_young))
    assert est.timeline_micro == est.posts * 5_000
    assert (est.accounts, est.to_fetch, est.skipped, est.unavailable) == (5, 3, 1, 1)
    assert est.unknown_volume == 1
    assert est.requests == 3  # One page each.
    assert seen == []  # No requests at all.
    text = est.render()
    assert "no timelines were fetched" in text and "fits under the cap" in text


def test_estimate_without_any_volume_uses_the_assumed_rate_not_zero() -> None:
    source, _, _ = make_source(lambda r: j({}))
    est = source.estimate_cost([Account("x", str(i), "h") for i in range(10)], SINCE, UNTIL)
    assert est.posts > 0
    assert est.high_posts == 2 * est.posts


def test_estimate_total_includes_ledger_spend_and_flags_overrun(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        storage.add_spend("x", 0.5, run_id=1)
        ledger = SpendLedger(storage, "x")
        source, _, _ = make_source(lambda r: j({}), max_usd="1.0", ledger=ledger)
        busy = [
            Account("x", str(i), "h", NOW - timedelta(days=100), {"tweet_count": 10000})
            for i in range(10)
        ]
        est = source.estimate_cost(busy, SINCE, UNTIL)
        assert est.spent_micro == 500_000
        assert est.total_micro == 500_000 + est.timeline_micro
        assert "EXCEEDS" in est.render()
        small = [Account("x", "1", "h", NOW - timedelta(days=100), {"tweet_count": 100})]
        est = source.estimate_cost(small, SINCE, UNTIL)
        assert "may reach the cap" in est.render() or "fits" in est.render()


def test_plan_spend_refuses_without_cap_and_suggests_a_value() -> None:
    source, _, seen = make_source(lambda r: j({}), max_usd=None)
    plan = source.plan_spend(SeedSet("x", ("q",)), n=100, pool_limit=100, resume=False)
    assert plan.refuse is not None
    assert "--max-usd " in plan.refuse and "projection" in plan.refuse
    assert plan.paid_discovery
    assert seen == []


def test_plan_spend_with_cap_projects_discovery() -> None:
    source, _, _ = make_source(lambda r: j({}), max_usd="20")
    plan = source.plan_spend(SeedSet("x", ("q",)), n=3000, pool_limit=3000, resume=False)
    assert plan.refuse is None
    text = "\n".join(plan.lines)
    assert "X discovery (billed)" in text and "$52.50" in text  # 3000 x $0.0175.
    assert "stops at its cap of $6.00" in text
    assert "probably stop at its cap after about 342 accounts" in text
    resumed = source.plan_spend(SeedSet("x", ("q",)), n=3000, pool_limit=3000, resume=True)
    assert not any("discovery" in line.lower() for line in resumed.lines[:1])


# ----------------------------------------------------------- end to end (pipeline)


def scripted_x(pages: int, authors_per_page: int = 2) -> Handler:
    """Search returns ``pages`` pages of new authors, each with an endless next_token."""
    counter = iter(range(10_000))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH:
            k = next(counter)
            authors = [f"{k}-{i}" for i in range(authors_per_page)]
            return j(search_page(authors, "more", prefix=f"s{k}"))
        uid = request.url.path.split("/")[3]
        return j({"data": [tweet(f"{uid}-1", "2026-10-01T10:00:00Z")], "meta": {}})

    return handler


def x_settings(n: int = 50) -> ScanSettings:
    return ScanSettings(
        platform="x", scoring=ScoringParams(cutoff=CUTOFF, now=NOW), n=n, pool_factor=1.0
    )


def test_run_scan_discovery_cap_stops_discovery_and_timelines_still_run(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        ledger = SpendLedger(storage, "x")
        source, _, seen = make_source(
            scripted_x(100), ledger=ledger, max_usd="10", max_discovery_usd="1.6"
        )
        outcome = run_scan(source, SeedSet("x", ("q",)), storage, x_settings())
    searches = [r for r in seen if r.url.path == SEARCH]
    timelines = [r for r in seen if r.url.path.endswith("/tweets") and "users" in r.url.path]
    assert len(searches) == 4
    assert len(timelines) == 8  # All 8 discovered accounts were fetched.
    assert outcome.status == "complete"
    assert outcome.summary["sampled"] == 8
    assert any("discovery stopped at its cap" in w for w in outcome.warnings)
    assert outcome.summary["spend_usd"] == pytest.approx(usd(source))


def test_ledger_cap_holds_across_two_runs_on_the_same_db(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with Storage(db) as storage:
        source, _, _ = make_source(scripted_x(1), ledger=SpendLedger(storage, "x"), max_usd="10")
        first = run_scan(source, SeedSet("x", ("q",)), storage, x_settings(n=2))
        spent_first = first.summary["spend_usd"]
    assert first.status == "complete" and spent_first > 0

    with Storage(db) as storage:
        # Refetch the same sample with a $0.51 cap. On its own this run fits (two
        # $0.50 worst-case pages that each return one $0.005 post), but the daily
        # ledger already holds the first run's spend, so the first page is refused.
        assert spent_first > 0.02
        cap = "0.51"
        source, _, seen = make_source(scripted_x(1), ledger=SpendLedger(storage, "x"), max_usd=cap)
        settings = replace(x_settings(n=2), cache_ttl_hours=0.0, refresh=True)
        second = run_scan(source, SeedSet("x", ("q",)), storage, settings, from_run=first.run_id)
    assert seen == []
    assert second.status == "partial"
    assert any("today across runs" in w for w in second.warnings)

    with Storage(db) as storage:
        # budget_scope=run covers the whole resume chain, so the first run's spend
        # still counts and the same cap refuses the refetch too.
        source, _, seen = make_source(
            scripted_x(1), ledger=SpendLedger(storage, "x"), max_usd=cap, budget_scope="run"
        )
        settings = replace(x_settings(n=2), cache_ttl_hours=0.0, refresh=True)
        third = run_scan(source, SeedSet("x", ("q",)), storage, settings, from_run=first.run_id)
    assert seen == []
    assert third.status == "partial"
    assert any("resume chain" in w for w in third.warnings)


# -------------------------------------------------------------------------- CLI


def fake_x_transport(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == SEARCH:
        return j(search_page(["11", "12", "13"]))
    if path.endswith("/tweets"):
        uid = path.split("/")[3]
        return j({"data": [tweet(f"{uid}-1", "2026-10-01T10:00:00Z", "hi")], "meta": {}})
    return j({}, 404)


@pytest.fixture
def x_paths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN", TOKEN)
    paths: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return fake_x_transport(request)

    def factory(**kwargs: Any) -> HttpClient:
        kwargs.update(requests_per_second=1000.0, burst=1000.0)
        return HttpClient(transport=httpx.MockTransport(spy), sleep=lambda _s: None, **kwargs)

    monkeypatch.setattr(cli, "HttpClient", factory)
    return paths


@pytest.fixture(autouse=True)
def _cli_x_clock_frozen(frozen_cli_x_clock: None) -> None:
    """Every CLI test here runs with the X wall clock on NOW (see the fixture in
    conftest.py), so no test in this module depends on the date the suite runs on."""


def x_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "scan", "--platform", "x", "--query", "q", "--n", "3",
        "--now", "2026-10-14T00:00:00Z", "--db", str(tmp_path / "c.db"), *extra,
    ]  # fmt: skip


def test_cli_scan_requires_an_explicit_cap(x_paths: list[str], tmp_path: Path) -> None:
    result = CliRunner().invoke(app, x_args(tmp_path, "--yes"))
    assert result.exit_code == 2, result.output
    assert "explicit spend cap" in result.output
    assert "--max-usd " in result.output and "projection" in result.output
    assert x_paths == []


def test_cli_paid_discovery_requires_yes(x_paths: list[str], tmp_path: Path) -> None:
    result = CliRunner().invoke(app, x_args(tmp_path, "--max-usd", "10"))
    assert result.exit_code == 2, result.output
    assert "X discovery (billed)" in result.output
    assert "--yes" in result.output
    assert x_paths == []


def test_cli_scan_prints_estimated_spend(x_paths: list[str], tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, x_args(tmp_path, "--max-usd", "10", "--yes", "--out", str(tmp_path / "o.csv"))
    )
    assert result.exit_code == 0, result.output
    assert "Estimated X API spend:" in result.output
    assert "of $10.00 cap" in result.output
    with Storage(tmp_path / "c.db") as storage:
        run = storage.get_run(1)
    assert run is not None
    assert run["params"]["pool_factor"] == 1.0  # The X default, not the core's 3.
    assert run["summary"]["pool_limit"] == 3


def test_cli_dry_run_stores_the_sample_and_from_run_reuses_it(
    x_paths: list[str], tmp_path: Path
) -> None:
    dry = CliRunner().invoke(app, x_args(tmp_path, "--max-usd", "10", "--dry-run-cost"))
    assert dry.exit_code == 2  # Discovery is billed: --yes is required.
    assert x_paths == []

    dry = CliRunner().invoke(app, x_args(tmp_path, "--max-usd", "10", "--dry-run-cost", "--yes"))
    assert dry.exit_code == 0, dry.output
    assert "X API cost projection" in dry.output
    assert "--from-run 1" in dry.output
    assert x_paths == [SEARCH]  # Discovery only; no timelines.
    with Storage(tmp_path / "c.db") as storage:
        run = storage.get_run(1)
        assert run is not None and run["status"] == "sampled"
        assert len(storage.get_run_sample(1)) == 3
        spent_dry = storage.spend_for_run(1, "x")
    assert spent_dry > 0

    x_paths.clear()
    resumed = CliRunner().invoke(
        app,
        x_args(tmp_path, "--max-usd", "10", "--from-run", "1", "--out", str(tmp_path / "o.csv")),
    )
    assert resumed.exit_code == 0, resumed.output
    assert SEARCH not in x_paths  # No second discovery spend.
    assert sorted(x_paths) == [f"/2/users/{u}/tweets" for u in ("11", "12", "13")]
    with Storage(tmp_path / "c.db") as storage:
        assert storage.spend_for_run(1, "x") == pytest.approx(spent_dry)
        assert storage.spend_for_run(2, "x") == pytest.approx(3 * 0.005)

    x_paths.clear()
    projection = CliRunner().invoke(
        app, x_args(tmp_path, "--max-usd", "10", "--from-run", "1", "--dry-run-cost")
    )
    assert projection.exit_code == 0, projection.output
    assert "X API cost projection" in projection.output
    assert x_paths == []


def test_cli_max_usd_rejected_for_other_platforms(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["scan", "--platform", "fake", "--max-usd", "5", "--db", str(tmp_path / "c.db")]
    )
    assert result.exit_code == 2
    assert "only applies to --platform x" in result.output


def test_cli_missing_token_message(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, x_args(tmp_path))
    assert result.exit_code == 2
    assert "BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN" in result.output


def test_cli_output_never_shows_the_token(x_paths: list[str], tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, x_args(tmp_path, "--max-usd", "10", "--yes", "--out", str(tmp_path / "o.csv"))
    )
    assert TOKEN not in result.output


def test_json_is_serializable_meta() -> None:
    source, _, _ = make_source(lambda r: j(search_page(["1"])))
    (acct,) = source.discover(SeedSet("x", ("q",)), 1)
    json.dumps(acct.meta)


# ------------------------------------------------------- ledger reserve / settle


def _reserved_rows(storage: Storage) -> int:
    return storage._conn.execute(
        "SELECT COUNT(*) FROM spend_events WHERE state = 'reserved'"
    ).fetchone()[0]


def _discover_with_ledger(
    tmp_path: Path, handler: Handler, **options: Any
) -> tuple[XSource, Storage, SpendLedger, BaseException | None]:
    storage = Storage(tmp_path / "c.db")
    ledger = SpendLedger(storage, "x")
    source, _, _ = make_source(handler, ledger=ledger, **options)
    error: BaseException | None = None
    try:
        list(source.discover(SeedSet("x", ("q",)), 2))
    except Exception as exc:
        error = exc
    return source, storage, ledger, error


def _scripted(*steps: Any) -> Handler:
    """Return each step in turn: an exception instance is raised, anything else returned."""
    queue = list(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, BaseException):
            raise step
        return step

    return handler


ONE_PAGE = j(search_page(["1", "2"]))
ACTUAL_ONE_PAGE = 2 * 0.005 + 2 * 0.010  # Two posts and two users.


def test_reserve_settle_matches_actual_charges(tmp_path: Path) -> None:
    source, storage, ledger, error = _discover_with_ledger(tmp_path, _scripted(ONE_PAGE))
    with storage:
        assert error is None
        assert usd(source) == pytest.approx(ACTUAL_ONE_PAGE)
        assert ledger.spent("day") == pytest.approx(ACTUAL_ONE_PAGE)
        assert _reserved_rows(storage) == 0


def test_retried_5xx_and_unsent_attempts_release_their_reservations(tmp_path: Path) -> None:
    handler = _scripted(j({}, 503), httpx.ConnectError("refused"), ONE_PAGE)
    source, storage, ledger, error = _discover_with_ledger(tmp_path, handler)
    with storage:
        assert error is None
        assert source.budget.failed_attempts == 0
        assert ledger.spent("day") == pytest.approx(ACTUAL_ONE_PAGE)
        assert usd(source) == pytest.approx(ACTUAL_ONE_PAGE)
        assert _reserved_rows(storage) == 0


def test_sent_transport_failure_settles_at_the_worst_case(tmp_path: Path) -> None:
    worst_src, worst_store, _, worst_error = _discover_with_ledger(
        tmp_path / "w", _scripted(RuntimeError("boom"))
    )
    worst_store.close()
    assert isinstance(worst_error, RuntimeError)
    worst = usd(worst_src)
    assert worst > ACTUAL_ONE_PAGE

    handler = _scripted(httpx.ReadTimeout("slow"), ONE_PAGE)
    source, storage, ledger, error = _discover_with_ledger(tmp_path, handler)
    with storage:
        assert error is None
        assert source.budget.failed_attempts == 1
        assert ledger.spent("day") == pytest.approx(worst + ACTUAL_ONE_PAGE)
        assert usd(source) == pytest.approx(worst + ACTUAL_ONE_PAGE)
        assert _reserved_rows(storage) == 0


def test_no_reservation_leaks_on_exceptions(tmp_path: Path) -> None:
    # Never sent on any attempt: everything is released.
    source, storage, ledger, error = _discover_with_ledger(
        tmp_path / "a", _scripted(httpx.ConnectError("refused"))
    )
    with storage:
        assert isinstance(error, SourceError)
        assert ledger.spent("day") == 0 and usd(source) == 0
        assert _reserved_rows(storage) == 0

    # An unexpected error while sending: unknown whether X billed it, so worst case.
    source, storage, ledger, error = _discover_with_ledger(
        tmp_path / "b", _scripted(RuntimeError("boom"))
    )
    with storage:
        assert isinstance(error, RuntimeError)
        assert ledger.spent("day") == pytest.approx(usd(source)) and usd(source) > 0
        assert _reserved_rows(storage) == 0

    # An error after the response arrived but before it was charged: worst case.
    storage = Storage(tmp_path / "c.db")
    with storage:
        ledger = SpendLedger(storage, "x")
        source, _, _ = make_source(_scripted(ONE_PAGE), ledger=ledger)

        def broken(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("charge failed")

        source._charge_items = broken  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            list(source.discover(SeedSet("x", ("q",)), 2))
        assert ledger.spent("day") == pytest.approx(usd(source)) and usd(source) > 0
        assert _reserved_rows(storage) == 0

    # A refused reservation leaves nothing behind and sends nothing.
    storage = Storage(tmp_path / "d.db")
    with storage:
        ledger = SpendLedger(storage, "x")
        source, _, seen = make_source(_scripted(ONE_PAGE), ledger=ledger, max_usd="0.01")
        list(source.discover(SeedSet("x", ("q",)), 2))
        assert seen == []
        assert ledger.spent("day") == 0
        assert _reserved_rows(storage) == 0


def _tracking_reservations(source: XSource) -> list[Any]:
    """Record every reservation the source takes, to check each is settled once."""
    taken: list[Any] = []
    original = source._reserve

    def reserve(worst: int, what: str) -> Any:
        reservation = original(worst, what)
        if reservation is not None:
            taken.append(reservation)
        return reservation

    source._reserve = reserve  # type: ignore[method-assign]
    return taken


@pytest.mark.parametrize(
    ("failure", "error_type", "billed"),
    [
        (httpx.ConnectError("refused"), SourceError, False),  # on_attempt_not_sent
        (httpx.ConnectTimeout("slow connect"), SourceError, False),  # on_attempt_not_sent
        (httpx.DecodingError("bad gzip"), SourceError, True),  # on_attempt_failed
        (httpx.TooManyRedirects("loop"), SourceError, True),  # on_attempt_failed
        (KeyboardInterrupt(), KeyboardInterrupt, True),  # Ctrl-C mid-send: failed
    ],
)
def test_every_reservation_is_settled_exactly_once_by_the_core_hooks(
    tmp_path: Path, failure: BaseException, error_type: type[BaseException], billed: bool
) -> None:
    storage = Storage(tmp_path / "c.db")
    with storage:
        ledger = SpendLedger(storage, "x")
        source, _, _ = make_source(_scripted(failure), ledger=ledger)
        taken = _tracking_reservations(source)
        with pytest.raises(error_type):
            list(source.discover(SeedSet("x", ("q",)), 2))
        assert taken
        assert [r.settle_count for r in taken] == [1] * len(taken)
        assert _reserved_rows(storage) == 0
        if billed:
            assert source.budget.failed_attempts == len(taken) == 1  # No retry after.
            assert usd(source) > 0
            assert ledger.spent("day") == pytest.approx(usd(source))
        else:
            assert source.budget.failed_attempts == 0
            assert usd(source) == 0 and ledger.spent("day") == 0


def test_mixed_retries_settle_each_attempt_once(tmp_path: Path) -> None:
    handler = _scripted(
        j({}, 503), httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ONE_PAGE
    )
    storage = Storage(tmp_path / "c.db")
    with storage:
        ledger = SpendLedger(storage, "x")
        source, _, _ = make_source(handler, ledger=ledger)
        taken = _tracking_reservations(source)
        list(source.discover(SeedSet("x", ("q",)), 2))
        assert len(taken) == 4
        assert [r.settle_count for r in taken] == [1, 1, 1, 1]
        assert source.budget.failed_attempts == 1  # Only the read timeout was sent.
        assert ledger.spent("day") == pytest.approx(usd(source))
        assert _reserved_rows(storage) == 0


def test_without_a_ledger_the_in_memory_budget_still_charges() -> None:
    handler = _scripted(httpx.ReadTimeout("slow"), ONE_PAGE)
    source, _, _ = make_source(handler)
    list(source.discover(SeedSet("x", ("q",)), 2))
    assert source.budget.failed_attempts == 1
    assert usd(source) > ACTUAL_ONE_PAGE


def test_x_budget_options_survive_the_segment_secret_matcher() -> None:
    assert XSource.secret_options == ()
    options = {
        "max_usd": "10",
        "max_discovery_usd": "2",
        "budget_scope": "run",
        "post_price": "0.005",
        "user_price": "0.01",
        "lang": "en",
        "field_param": "x",
        "full_archive": "true",
        "exclude_retweets": "false",
    }
    assert storable_options(options, XSource.secret_options) == options
    assert storable_options({**options, "bearer_token": "t"}, XSource.secret_options) == options


def test_budget_options_are_stored_and_not_taken_for_secrets() -> None:
    options = {
        "max_usd": "10",
        "max_discovery_usd": "2",
        "budget_scope": "run",
        "api_key": "x",
        "bearer_token": "y",
    }
    assert storable_options(options) == {
        "max_usd": "10",
        "max_discovery_usd": "2",
        "budget_scope": "run",
    }


def test_cli_resume_inherits_max_usd(x_paths: list[str], tmp_path: Path) -> None:
    dry = CliRunner().invoke(
        app,
        x_args(
            tmp_path,
            "--max-usd", "10",
            "--source-option", "budget_scope=run",
            "--dry-run-cost", "--yes",
        ),
    )  # fmt: skip
    assert dry.exit_code == 0, dry.output
    assert "--max-usd 10.0" in dry.output  # The printed resume command carries the cap.
    assert "max_usd=" not in dry.output

    x_paths.clear()
    resumed = CliRunner().invoke(
        app, x_args(tmp_path, "--from-run", "1", "--out", str(tmp_path / "o.csv"))
    )
    assert resumed.exit_code == 0, resumed.output  # No "explicit spend cap" refusal.
    assert "of $10.00 cap" in resumed.output
    assert len(x_paths) == 3
    with Storage(tmp_path / "c.db") as storage:
        run = storage.get_run(2)
    assert run is not None
    assert run["params"]["source_options"]["max_usd"] == "10.0"
    assert run["params"]["source_options"]["budget_scope"] == "run"

    # The same cap spelled differently is not a conflict; a different cap is.
    same = CliRunner().invoke(
        app, x_args(tmp_path, "--from-run", "1", "--source-option", "max_usd=10",
                    "--dry-run-cost"),
    )  # fmt: skip
    assert same.exit_code == 0, same.output
    other = CliRunner().invoke(app, x_args(tmp_path, "--from-run", "1", "--max-usd", "20"))
    assert other.exit_code == 2
    assert "--source-option max_usd" in other.output and "--force" in other.output


def test_unsent_attempt_is_released_when_the_request_budget_stops_the_retry(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "c.db")
    with storage:
        ledger = SpendLedger(storage, "x")
        handler = _scripted(httpx.ConnectError("refused"))
        source, _, _ = make_source(handler, ledger=ledger)
        source.http.max_requests = 1  # The retry after the connect error is refused.
        with pytest.raises(BudgetExceeded):
            list(source.discover(SeedSet("x", ("q",)), 2))
        assert ledger.spent("day") == 0 and usd(source) == 0
        assert _reserved_rows(storage) == 0


def test_placebo_samples_on_x_need_full_archive_and_an_opt_in() -> None:
    seeds = SeedSet("x", ("topic",))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    recent, _, seen = make_source(refuse)
    problem = recent.placebo_discovery_problem(seeds)
    assert problem is not None and "7 days" in problem
    archive, _, _ = make_source(refuse, full_archive="true")
    problem = archive.placebo_discovery_problem(seeds)
    assert problem is not None and "placebo_discovery=true" in problem
    opted, _, _ = make_source(refuse, full_archive="true", placebo_discovery="true")
    assert opted.placebo_discovery_problem(seeds) is None
    assert seen == []
