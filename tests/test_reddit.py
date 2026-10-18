"""Reddit adapter tests. All traffic goes through httpx.MockTransport; nothing sleeps."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.models import Account, content_hash
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources import reddit as reddit_mod
from bot_farm_silence_tracker.sources.base import (
    AccountUnavailable,
    BudgetExceeded,
    RateLimited,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.reddit import RedditSource, is_skippable_author

from .helpers import CUTOFF, NOW

EPOCH = CUTOFF.timestamp()
CREDS = {
    "BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID": "cid",
    "BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET": "s3cr3t-value",
    "BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME": "test_research",
}
PASSWORD_ENV = "BOT_FARM_SILENCE_TRACKER_REDDIT_PASSWORD"
PRE = EPOCH - 3600  # An hour before the cutoff.
POST = EPOCH + 3600  # An hour after the cutoff.
SINCE = CUTOFF - timedelta(days=30)


class FakeClock:
    """Monotonic clock for token expiry; tests move it by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t


Handler = Callable[[httpx.Request], httpx.Response]


class Env:
    """A source wired to a mock transport, with request logging."""

    def __init__(
        self,
        handler: Handler,
        *,
        credentials: dict[str, str] | None = None,
        baseline_start: datetime | None = None,
        now: datetime = NOW,
        max_requests: int | None = None,
        retries: int = 0,
        **options: Any,
    ) -> None:
        self.clock = FakeClock()
        self.requests: list[httpx.Request] = []
        self.http_sleeps: list[float] = []

        def logged(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        self.http = HttpClient(
            transport=httpx.MockTransport(logged),
            requests_per_second=1000,
            burst=1000,
            sleep=self.http_sleeps.append,
            max_retries=retries,
            max_requests=max_requests,
        )
        self.source = RedditSource(
            SourceConfig(
                http=self.http,
                credentials=credentials or CREDS,
                now=now,
                cutoff=CUTOFF,
                options=options,
                baseline_start=baseline_start,
            ),
            clock=self.clock.now,
        )

    def api(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "oauth.reddit.com"]

    def tokens(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/api/v1/access_token"]


def token_response(token: str = "tok1", expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": token, "expires_in": expires_in})


def about_ok(**data: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": {"created_utc": 5, **data}})


def listing(items: list[dict[str, Any]], after: str | None = None) -> httpx.Response:
    body = {"data": {"children": [{"data": i} for i in items], "after": after}}
    return httpx.Response(200, json=body)


def post(
    author: str, sub: str = "s", ts: float = PRE, title: str = "t", text: str = ""
) -> dict[str, Any]:
    return {
        "author": author,
        "subreddit": sub,
        "created_utc": ts,
        "title": title,
        "selftext": text,
        "author_fullname": f"t2_{author}",
    }


def comment(author: str, sub: str = "s", ts: float = PRE, body: str = "hello") -> dict[str, Any]:
    return {"author": author, "subreddit": sub, "created_utc": ts, "body": body}


def route(table: dict[str, Handler | httpx.Response]) -> Handler:
    """Dispatch on the request path; unknown paths fail the test loudly."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response()
        entry = table.get(request.url.path)
        if entry is None:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return entry(request) if callable(entry) else entry

    return handler


def acct(name: str = "alice", created: datetime | None = None) -> Account:
    return Account("reddit", name.lower(), name, created)


# --------------------------------------------------------------------- auth


def test_token_request_uses_basic_auth_client_credentials_and_user_agent() -> None:
    env = Env(route({"/user/alice/about": about_ok()}))
    env.source.hydrate(acct())
    (tok,) = env.tokens()
    expected = base64.b64encode(b"cid:s3cr3t-value").decode()
    assert tok.headers["Authorization"] == f"Basic {expected}"
    assert parse_qs(tok.content.decode()) == {"grant_type": ["client_credentials"]}
    ua = f"python:bot-farm-silence-tracker:{__version__} (by /u/test_research)"
    assert tok.headers["User-Agent"] == ua
    (call,) = env.api()
    assert call.headers["Authorization"] == "bearer tok1"
    assert call.headers["User-Agent"] == ua
    assert call.url.params["raw_json"] == "1"


def test_username_prefix_is_stripped_from_user_agent() -> None:
    creds = {**CREDS, "BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME": "/u/test"}
    http = HttpClient(transport=httpx.MockTransport(lambda r: token_response()))
    src = RedditSource(SourceConfig(http=http, credentials=creds, now=NOW, cutoff=CUTOFF))
    assert src.user_agent.endswith("(by /u/test)")


def test_password_grant_is_used_when_configured() -> None:
    env = Env(route({"/user/alice/about": about_ok()}), credentials={**CREDS, PASSWORD_ENV: "pw"})
    env.source.hydrate(acct())
    form = parse_qs(env.tokens()[0].content.decode())
    assert form == {"grant_type": ["password"], "username": ["test_research"], "password": ["pw"]}


def test_password_is_an_optional_credential_and_not_an_option() -> None:
    assert RedditSource.optional_credentials == (PASSWORD_ENV,)
    assert all(
        name.startswith("BOT_FARM_SILENCE_TRACKER_REDDIT_")
        for name in RedditSource.required_credentials
    )
    env = Env(route({"/user/alice/about": about_ok()}), password="ignored")
    env.source.hydrate(acct())
    assert "grant_type=client_credentials" in env.tokens()[0].content.decode()


def test_token_is_cached_then_refreshed_before_expiry() -> None:
    tokens = iter(["tok1", "tok2"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response(next(tokens), expires_in=3600)
        return about_ok()

    env = Env(handler)
    env.source.hydrate(acct("a"))
    env.source.hydrate(acct("b"))
    assert len(env.tokens()) == 1  # Cached.
    env.clock.t += 3600 - reddit_mod.TOKEN_REFRESH_MARGIN + 1  # Inside the refresh margin.
    env.source.hydrate(acct("c"))
    assert len(env.tokens()) == 2
    assert [r.headers["Authorization"] for r in env.api()] == [
        "bearer tok1",
        "bearer tok1",
        "bearer tok2",
    ]


@pytest.mark.parametrize("status", [401, 403])
def test_token_401_raises_source_auth_error(status: int) -> None:
    env = Env(lambda r: httpx.Response(status, json={"message": "Unauthorized"}))
    with pytest.raises(SourceAuthError):
        env.source.hydrate(acct())
    assert env.api() == []


def test_token_200_without_access_token_raises_source_auth_error() -> None:
    env = Env(lambda r: httpx.Response(200, json={"error": "invalid_grant"}))
    with pytest.raises(SourceAuthError, match="invalid_grant"):
        env.source.hydrate(acct())


def test_api_401_refreshes_token_once_then_succeeds() -> None:
    tokens = iter(["old", "new"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response(next(tokens))
        if request.headers["Authorization"] == "bearer old":
            return httpx.Response(401)
        return about_ok()

    env = Env(handler)
    assert env.source.hydrate(acct()).created_at == datetime.fromtimestamp(5, tz=UTC)
    assert len(env.api()) == 2 and len(env.tokens()) == 2


def test_api_401_twice_raises_source_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response()
        return httpx.Response(401)

    env = Env(handler)
    with pytest.raises(SourceAuthError):
        env.source.hydrate(acct())


# ---------------------------------------------------------------- discovery


def sub_handler(subs: dict[str, dict[str, list[dict[str, Any]]]]) -> Handler:
    """Serve /r/{sub}/new and /r/{sub}/comments, one page each."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response()
        _, _, sub, kind = request.url.path.split("/")
        return listing(subs[sub][kind])

    return handler


def test_discover_dedups_skips_bots_and_records_meta() -> None:
    subs = {
        "a": {
            "new": [post("Alice"), post("[deleted]"), post("AutoModerator"), post("helperbot")],
            "comments": [comment("alice"), comment("Bob")],
        }
    }
    env = Env(sub_handler(subs), discovery="listing")
    found = list(env.source.discover(SeedSet("reddit", ("a",)), 10))
    assert [a.handle for a in found] == ["Alice", "Bob"]
    first = found[0]
    assert first.platform == "reddit" and first.user_id == "alice" and first.created_at is None
    assert first.meta == {"discovered_in": "a", "discovered_via": "new", "fullname": "t2_Alice"}
    assert found[1].meta["discovered_via"] == "comments"


def test_discover_interleaves_subreddits_round_robin() -> None:
    subs = {
        "big": {"new": [post(f"big{i}") for i in range(50)], "comments": []},
        "tiny": {"new": [post("t1"), post("t2")], "comments": []},
        "small": {"new": [post("s1")], "comments": []},
    }
    env = Env(sub_handler(subs), discovery="listing")
    found = list(env.source.discover(SeedSet("reddit", ("big", "tiny", "small")), 6))
    assert [a.handle for a in found] == ["big0", "t1", "s1", "big1", "t2", "big2"]


def test_discover_stops_at_pool_limit_and_stays_lazy() -> None:
    subs = {f"s{i}": {"new": [post(f"u{i}")], "comments": []} for i in range(5)}
    env = Env(sub_handler(subs), discovery="listing")
    found = list(env.source.discover(SeedSet("reddit", tuple(subs)), 2))
    assert len(found) == 2
    fetched = {r.url.path.split("/")[2] for r in env.api()}
    assert fetched == {"s0", "s1"}  # Later subreddits were never fetched.


def test_discover_follows_after_cursor_and_skips_forbidden_subreddit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response()
        if "/r/private/" in request.url.path:
            return httpx.Response(403)
        if request.url.path.endswith("/comments"):
            return listing([])
        if request.url.params.get("after") == "t3_p1":
            return listing([post("second")])
        return listing([post("first")], after="t3_p1")

    env = Env(handler, discovery="listing")
    found = list(env.source.discover(SeedSet("reddit", ("private", "open")), 10))
    assert [a.handle for a in found] == ["first", "second"]
    warnings = env.source.drain_warnings()
    assert any("r/private" in w and "403" in w for w in warnings)


def test_discover_returns_fewer_when_pool_is_exhausted() -> None:
    env = Env(
        sub_handler({"a": {"new": [post("x")], "comments": [comment("x")]}}), discovery="listing"
    )
    assert len(list(env.source.discover(SeedSet("reddit", ("a",)), 100))) == 1


@pytest.mark.parametrize(
    ("name", "skip"),
    [
        ("AutoModerator", True),
        ("[deleted]", True),
        ("RemindMeBot", True),
        ("sneakpeekbot", True),
        ("", True),
        (None, True),
        ("alice", False),
        ("robotics_fan", False),
    ],
)
def test_is_skippable_author(name: str | None, skip: bool) -> None:
    assert is_skippable_author(name) is skip


# ------------------------------------------------------------------ hydrate


def test_hydrate_sets_created_at_and_karma() -> None:
    about = about_ok(created_utc=1700000000.0, link_karma=12, comment_karma=345)
    env = Env(route({"/user/Alice/about": about}))
    account = env.source.hydrate(acct("Alice"))
    assert account.created_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert account.meta["link_karma"] == 12 and account.meta["comment_karma"] == 345


def test_hydrate_is_a_noop_when_created_at_is_known() -> None:
    env = Env(route({}))
    account = acct(created=CUTOFF)
    assert env.source.hydrate(account) is account
    assert env.requests == []


def test_hydrate_suspended_raises() -> None:
    about = httpx.Response(200, json={"data": {"name": "alice", "is_suspended": True}})
    env = Env(route({"/user/alice/about": about}))
    with pytest.raises(AccountUnavailable) as err:
        env.source.hydrate(acct())
    assert err.value.status == "suspended"


def test_hydrate_404_is_deleted() -> None:
    env = Env(route({"/user/alice/about": httpx.Response(404, json={"error": 404})}))
    with pytest.raises(AccountUnavailable) as err:
        env.source.hydrate(acct())
    assert err.value.status == "deleted"


def test_hydrate_without_created_utc_is_a_source_error() -> None:
    env = Env(route({"/user/alice/about": httpx.Response(200, json={"data": {}})}))
    with pytest.raises(SourceError):
        env.source.hydrate(acct())


# ----------------------------------------------------------------- activity


def overview_pages(pages: list[list[dict[str, Any]]]) -> Handler:
    """Serve ``pages`` in order, chained by ``after`` cursors p1, p2, ..."""

    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        index = 0 if after is None else int(after.removeprefix("p"))
        nxt = f"p{index + 1}" if index + 1 < len(pages) else None
        return listing(pages[index], after=nxt)

    return handler


def test_fetch_activity_maps_posts_comments_and_hashes_content() -> None:
    items = [
        post("alice", "worldnews", EPOCH - 100, "Big Title", "Some  body"),
        comment("alice", "ukraine", EPOCH - 200, "a Reply"),
        comment("alice", "ukraine", EPOCH - 300, "[removed]"),
    ]
    env = Env(route({"/user/alice/overview": overview_pages([items])}))
    acts = env.source.fetch_activity(acct(), SINCE, NOW)
    assert [(a.kind, a.community) for a in acts] == [
        ("post", "worldnews"),
        ("comment", "ukraine"),
        ("comment", "ukraine"),
    ]
    assert acts[0].content_hash == content_hash("Big Title Some body")
    assert acts[1].content_hash == content_hash("a reply")
    assert acts[2].content_hash is None
    assert all(a.ts.tzinfo is not None and a.user_id == "alice" for a in acts)
    params = env.api()[0].url.params
    assert params["limit"] == "100" and params["sort"] == "new" and params["raw_json"] == "1"


def test_fetch_activity_paginates_and_stops_once_older_than_since() -> None:
    page1 = [comment("alice", ts=EPOCH - i * 60) for i in range(100)]
    page2 = [comment("alice", ts=EPOCH - 5 * 86400), comment("alice", ts=SINCE.timestamp() - 10)]
    page3 = [comment("alice", ts=SINCE.timestamp() - 20)]
    env = Env(route({"/user/alice/overview": overview_pages([page1, page2, page3])}))
    account = acct()
    acts = env.source.fetch_activity(account, SINCE, NOW)
    assert len(acts) == 101  # 100 + the one at -5d; both older items dropped.
    assert len(env.api()) == 2  # Page 3 was never requested.
    assert env.api()[1].url.params["after"] == "p1"
    assert account.meta["truncated"] is False


def test_fetch_activity_respects_until_and_since_bounds() -> None:
    items = [
        comment("alice", ts=(NOW + timedelta(hours=1)).timestamp()),
        comment("alice", ts=NOW.timestamp()),
        comment("alice", ts=EPOCH),
        comment("alice", ts=SINCE.timestamp()),
    ]
    env = Env(route({"/user/alice/overview": overview_pages([items])}))
    acts = env.source.fetch_activity(acct(), SINCE, NOW)
    assert sorted(a.ts for a in acts) == [SINCE, CUTOFF]  # since inclusive, until exclusive.


def test_fetch_activity_flags_truncation_at_the_listing_cap() -> None:
    pages = [[comment("alice", ts=EPOCH - (p * 100 + i)) for i in range(100)] for p in range(10)]
    env = Env(route({"/user/alice/overview": overview_pages(pages)}))
    account = acct()
    acts = env.source.fetch_activity(account, SINCE, NOW)
    assert len(acts) == 1000  # All newer than `since`, and then the listing ended.
    assert account.meta["truncated"] is True
    assert len(env.api()) == 10


def test_fetch_activity_short_history_is_not_truncated() -> None:
    env = Env(route({"/user/alice/overview": overview_pages([[comment("alice")]])}))
    account = acct()
    assert len(env.source.fetch_activity(account, SINCE, NOW)) == 1
    assert account.meta["truncated"] is False


def test_fetch_activity_empty_history() -> None:
    env = Env(route({"/user/alice/overview": overview_pages([[]])}))
    assert env.source.fetch_activity(acct(), SINCE, NOW) == []


def test_fetch_activity_404_is_deleted() -> None:
    env = Env(route({"/user/alice/overview": httpx.Response(404)}))
    with pytest.raises(AccountUnavailable) as err:
        env.source.fetch_activity(acct(), SINCE, NOW)
    assert err.value.status == "deleted"


def test_fetch_activity_403_is_private_unless_about_says_suspended() -> None:
    env = Env(route({"/user/alice/overview": httpx.Response(403), "/user/alice/about": about_ok()}))
    with pytest.raises(AccountUnavailable) as err:
        env.source.fetch_activity(acct(), SINCE, NOW)
    assert err.value.status == "private"

    suspended = httpx.Response(200, json={"data": {"is_suspended": True}})
    env = Env(route({"/user/alice/overview": httpx.Response(403), "/user/alice/about": suspended}))
    with pytest.raises(AccountUnavailable) as err:
        env.source.fetch_activity(acct(), SINCE, NOW)
    assert err.value.status == "suspended"


def test_fetch_activity_unexpected_status_is_a_source_error() -> None:
    env = Env(route({"/user/alice/overview": httpx.Response(418)}))
    with pytest.raises(SourceError):
        env.source.fetch_activity(acct(), SINCE, NOW)


def test_http_429_surfaces_as_rate_limited_through_shared_client() -> None:
    env = Env(route({"/user/alice/overview": httpx.Response(429, headers={"Retry-After": "9999"})}))
    with pytest.raises(RateLimited):
        env.source.fetch_activity(acct(), SINCE, NOW)


# ----------------------------------------------------------- pacing and 429s


def test_pacer_is_the_shared_clients_and_sized_from_class_attributes() -> None:
    assert RedditSource.default_rps == pytest.approx(100 / 60)
    assert RedditSource.default_burst == 10
    env = Env(route({}))
    assert env.source.qpm == 100
    for leftover in ("_pace", "_min_interval", "_last_call"):
        assert not hasattr(env.source, leftover)


def test_qpm_option_resizes_the_shared_pacer_and_keeps_the_burst() -> None:
    env = Env(route({}), qpm="30")  # A string, as the CLI passes it.
    assert env.http.rate == (pytest.approx(0.5), 1000)
    assert env.source.qpm == 30


@pytest.mark.parametrize("bad", ["0", "-5", "fast"])
def test_invalid_qpm_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="qpm"):
        Env(route({}), qpm=bad)


def _limited_then_ok(headers: dict[str, str]) -> Handler:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response()
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers=headers)
        return about_ok()

    return handler


def test_429_without_retry_after_waits_for_the_ratelimit_reset_header() -> None:
    env = Env(_limited_then_ok({"X-Ratelimit-Reset": "7"}), retries=2)
    assert env.source.hydrate(acct()).created_at is not None
    assert env.http_sleeps == [7.0]  # Not the 1 second exponential backoff.
    assert len(env.api()) == 2


def test_429_retry_after_wins_over_the_reset_header() -> None:
    env = Env(_limited_then_ok({"Retry-After": "3", "X-Ratelimit-Reset": "7"}), retries=2)
    env.source.hydrate(acct())
    assert env.http_sleeps == [3.0]


def test_429_reset_beyond_the_cap_raises_rate_limited() -> None:
    env = Env(_limited_then_ok({"X-Ratelimit-Reset": "5000"}), retries=2)
    with pytest.raises(RateLimited):
        env.source.hydrate(acct())
    assert env.http_sleeps == []


def test_429_without_any_header_falls_back_to_backoff() -> None:
    env = Env(_limited_then_ok({}), retries=2)
    env.source.hydrate(acct())
    assert env.http_sleeps == [1.0]


# ------------------------------------------- cutoff-aware discovery (listings)


def paged(pages: list[list[dict[str, Any]]]) -> Handler:
    """Serve ``pages`` in order, chained by ``after`` cursors p1, p2, ..."""
    return overview_pages(pages)


def discover_handler(table: dict[str, Handler | httpx.Response]) -> Handler:
    """Like ``route`` but any path that is not listed is an empty listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response()
        entry = table.get(request.url.path)
        if entry is None:
            return listing([])
        return entry(request) if callable(entry) else entry

    return handler


def test_discover_skips_post_cutoff_items_and_pages_past_them() -> None:
    late = [post(f"late{i}", ts=POST + i) for i in range(100)]
    mixed = [post("late_tail", ts=POST), post("early1", ts=PRE), post("early2", ts=PRE - 60)]
    env = Env(discover_handler({"/r/a/new": paged([late, mixed])}), discovery="listing")
    found = list(env.source.discover(SeedSet("reddit", ("a",)), 10))
    assert [a.handle for a in found] == ["early1", "early2"]
    new_calls = [r for r in env.api() if r.url.path == "/r/a/new"]
    assert len(new_calls) == 2  # It kept paging until the cutoff.


def test_discover_treats_the_cutoff_itself_as_post_cutoff() -> None:
    env = Env(
        discover_handler(
            {"/r/a/new": paged([[post("at", ts=EPOCH), post("before", ts=EPOCH - 1)]])}
        ),
        discovery="listing",
    )
    assert [a.handle for a in env.source.discover(SeedSet("reddit", ("a",)), 10)] == ["before"]


def test_discover_warns_when_the_cap_is_hit_before_the_cutoff() -> None:
    pages = [[post(f"late{p}_{i}", ts=POST + p * 100 + i) for i in range(100)] for p in range(10)]
    env = Env(discover_handler({"/r/big/new": paged(pages)}), discovery="listing")
    assert list(env.source.discover(SeedSet("reddit", ("big",)), 10)) == []
    (warning,) = env.source.drain_warnings()
    assert "r/big new" in warning and "listing cap" in warning and "0 pre-cutoff" in warning
    assert len([r for r in env.api() if r.url.path == "/r/big/new"]) == 10


def test_discover_does_not_warn_when_a_small_subreddit_simply_has_no_old_items() -> None:
    env = Env(
        discover_handler({"/r/tiny/new": paged([[post("late", ts=POST)]])}), discovery="listing"
    )
    assert list(env.source.discover(SeedSet("reddit", ("tiny",)), 10)) == []
    assert env.source.drain_warnings() == []


def test_discover_prefers_the_baseline_window_and_stops_before_it() -> None:
    start = CUTOFF - timedelta(days=30)
    items = [
        post("in_window", ts=PRE),
        post("edge", ts=start.timestamp()),
        post("too_old", ts=start.timestamp() - 1),
        post("older", ts=start.timestamp() - 86400),
    ]
    env = Env(
        discover_handler({"/r/a/new": paged([items, [post("never_read", ts=PRE)]])}),
        baseline_start=start,
        discovery="listing",
    )
    found = [a.handle for a in env.source.discover(SeedSet("reddit", ("a",)), 10)]
    assert found == ["in_window", "edge"]
    assert len([r for r in env.api() if r.url.path == "/r/a/new"]) == 1


def test_discover_comment_listing_is_cutoff_aware_too() -> None:
    items = [comment("late", ts=POST), comment("early", ts=PRE)]
    env = Env(discover_handler({"/r/a/comments": paged([items])}), discovery="listing")
    found = list(env.source.discover(SeedSet("reddit", ("a",)), 10))
    assert [(a.handle, a.meta["discovered_via"]) for a in found] == [("early", "comments")]


def test_discovery_option_validation_and_default() -> None:
    assert Env(route({})).source.discovery == "both"
    assert Env(route({}), discovery="THREADS").source.discovery == "threads"
    with pytest.raises(ValueError, match="discovery"):
        Env(route({}), discovery="everything")
    with pytest.raises(ValueError, match="thread_window"):
        Env(route({}), thread_window="decade")
    with pytest.raises(ValueError, match="discovery_budget"):
        Env(route({}), discovery_budget="2")


def test_discovery_stops_at_its_share_of_the_request_budget() -> None:
    pages = [[post(f"late{p}_{i}", ts=POST) for i in range(100)] for p in range(10)]
    env = Env(
        discover_handler({"/r/big/new": paged(pages)}),
        max_requests=20,
        discovery_budget="0.25",
        discovery="listing",
    )
    assert list(env.source.discover(SeedSet("reddit", ("big",)), 10)) == []
    # Allowed: int(20 * 0.25) = 5 requests sent, and the token request is one of them.
    assert len([r for r in env.api() if r.url.path == "/r/big/new"]) == 4
    assert len(env.requests) == 5
    assert any("discovery budget" in w for w in env.source.drain_warnings())


# --------------------------------------------- cutoff-aware discovery (threads)


def thread(tid: str, author: str, ts: float) -> dict[str, Any]:
    return {"id": tid, "author": author, "created_utc": ts, "title": "t", "subreddit": "a"}


def node(author: str, ts: float, replies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"author": author, "created_utc": ts, "body": "x"}
    data["replies"] = {"data": {"children": replies}} if replies else ""
    return {"kind": "t1", "data": data}


def comments_response(children: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json=[{"data": {"children": []}}, {"data": {"children": children}}])


def test_thread_discovery_reads_pre_cutoff_threads_and_comment_trees() -> None:
    top = [thread("t1", "op1", PRE), thread("t2", "op2", POST), thread("t3", "op3", PRE - 60)]
    tree = [
        node("c1", PRE, [node("c2", POST), node("c3", PRE - 5), {"kind": "more", "data": {}}]),
        node("c4", POST),
        node("AutoModerator", PRE),
    ]
    env = Env(
        discover_handler(
            {
                "/r/a/top": listing(top),
                "/comments/t1": comments_response(tree),
                "/comments/t3": comments_response([node("c5", PRE)]),
            }
        ),
        discovery="threads",
    )
    found = list(env.source.discover(SeedSet("reddit", ("a",)), 20))
    assert [a.handle for a in found] == ["op1", "c1", "c3", "op3", "c5"]
    assert {a.meta["discovered_via"] for a in found} == {"threads"}
    paths = [r.url.path for r in env.api()]
    assert paths == ["/r/a/top", "/comments/t1", "/comments/t3"]  # t2 is post-cutoff.
    top_params = env.api()[0].url.params
    assert top_params["t"] == "week" and top_params["limit"] == "100"
    comment_params = env.api()[1].url.params
    assert comment_params["limit"] == "200" and comment_params["depth"] == "3"


def test_thread_discovery_limits_threads_per_sub_and_is_lazy() -> None:
    top = [thread(f"t{i}", f"op{i}", PRE - i) for i in range(10)]
    table: dict[str, Handler | httpx.Response] = {"/r/a/top": listing(top)}
    table.update({f"/comments/t{i}": comments_response([]) for i in range(10)})
    env = Env(discover_handler(table), discovery="threads", threads_per_sub=2)
    assert len(list(env.source.discover(SeedSet("reddit", ("a",)), 50))) == 2
    assert len([r for r in env.api() if r.url.path.startswith("/comments/")]) == 2


def test_thread_authors_before_the_baseline_are_ignored() -> None:
    start = CUTOFF - timedelta(days=30)
    old = start.timestamp() - 100
    env = Env(
        discover_handler(
            {
                "/r/a/top": listing([thread("t1", "op", PRE)]),
                "/comments/t1": comments_response([node("old_c", old), node("ok_c", PRE)]),
            }
        ),
        discovery="threads",
        baseline_start=start,
    )
    assert [a.handle for a in env.source.discover(SeedSet("reddit", ("a",)), 10)] == [
        "op",
        "ok_c",
    ]


def test_thread_window_auto_picks_the_shortest_window_that_covers_the_baseline() -> None:
    start = CUTOFF - timedelta(days=30)
    env = Env(discover_handler({}), discovery="threads", baseline_start=start)
    list(env.source.discover(SeedSet("reddit", ("a",)), 5))
    assert env.api()[0].url.params["t"] == "month"
    env = Env(discover_handler({}), discovery="threads", thread_window="year")
    list(env.source.discover(SeedSet("reddit", ("a",)), 5))
    assert env.api()[0].url.params["t"] == "year"


def test_thread_window_that_cannot_reach_the_cutoff_warns_and_reads_nothing() -> None:
    late = CUTOFF + timedelta(days=40)
    env = Env(discover_handler({}), discovery="threads", thread_window="week", now=late)
    assert list(env.source.discover(SeedSet("reddit", ("a",)), 5)) == []
    assert env.api() == []
    assert any("t=week" in w for w in env.source.drain_warnings())


def test_thread_discovery_skips_a_forbidden_subreddit_with_a_warning() -> None:
    env = Env(discover_handler({"/r/a/top": httpx.Response(403)}), discovery="threads")
    assert list(env.source.discover(SeedSet("reddit", ("a",)), 5)) == []
    assert any("r/a threads" in w and "403" in w for w in env.source.drain_warnings())


def test_both_mode_dedups_across_listings_and_threads() -> None:
    env = Env(
        discover_handler(
            {
                "/r/a/new": paged([[post("shared")]]),
                "/r/a/top": listing([thread("t1", "shared", PRE)]),
                "/comments/t1": comments_response([node("only_thread", PRE)]),
            }
        )
    )
    found = list(env.source.discover(SeedSet("reddit", ("a",)), 10))
    assert sorted(a.handle for a in found) == ["only_thread", "shared"]


# ------------------------------------------------------ history fetch details


def test_pinned_item_older_than_since_does_not_end_the_history_fetch() -> None:
    old = SINCE.timestamp() - 90 * 86400
    page = [
        {**comment("alice", ts=old), "stickied": True},
        {**post("alice", ts=old - 5), "pinned": True},
        comment("alice", ts=EPOCH - 60),
        comment("alice", ts=EPOCH - 120),
        comment("alice", ts=SINCE.timestamp() - 10),  # The real end of the window.
    ]
    env = Env(route({"/user/alice/overview": overview_pages([page])}))
    account = acct()
    acts = env.source.fetch_activity(account, SINCE, NOW)
    assert len(acts) == 2
    assert account.meta["truncated"] is False


def test_pinned_item_inside_the_window_is_still_recorded() -> None:
    page = [{**post("alice", ts=EPOCH - 600), "pinned": True}, comment("alice", ts=EPOCH - 60)]
    env = Env(route({"/user/alice/overview": overview_pages([page])}))
    acts = env.source.fetch_activity(acct(), SINCE, NOW)
    assert sorted(a.kind for a in acts) == ["comment", "post"]


def test_pinned_old_item_does_not_count_as_reaching_since_across_pages() -> None:
    old = SINCE.timestamp() - 90 * 86400
    pages = [
        [
            {**comment("alice", ts=old), "stickied": True},
            *[comment("alice", ts=PRE - i) for i in range(99)],
        ],
        [comment("alice", ts=PRE - 1000 - i) for i in range(5)],
    ]
    env = Env(route({"/user/alice/overview": overview_pages(pages)}))
    assert len(env.source.fetch_activity(acct(), SINCE, NOW)) == 104
    assert len(env.api()) == 2


@pytest.mark.parametrize(("count", "truncated"), [(899, False), (900, True)])
def test_truncation_boundary_is_at_900_items(count: int, truncated: bool) -> None:
    items = [comment("alice", ts=EPOCH - 60 - i) for i in range(count)]
    pages = [items[i : i + 100] for i in range(0, count, 100)]
    env = Env(route({"/user/alice/overview": overview_pages(pages)}))
    account = acct()
    assert len(env.source.fetch_activity(account, SINCE, NOW)) == count
    assert account.meta["truncated"] is truncated


def test_reaching_since_is_never_truncated_even_with_many_items() -> None:
    items = [comment("alice", ts=EPOCH - 60 - i) for i in range(950)]
    items.append(comment("alice", ts=SINCE.timestamp() - 1))
    pages = [items[i : i + 100] for i in range(0, len(items), 100)]
    env = Env(route({"/user/alice/overview": overview_pages(pages)}))
    account = acct()
    env.source.fetch_activity(account, SINCE, NOW)
    assert account.meta["truncated"] is False


def test_user_listing_does_not_send_a_time_filter() -> None:
    env = Env(route({"/user/alice/overview": overview_pages([[comment("alice")]])}))
    env.source.fetch_activity(acct(), SINCE, NOW)
    assert "t" not in env.api()[0].url.params


def test_removed_content_hashes_are_consistent() -> None:
    items = [
        post("alice", title="Hello", text="[removed]"),
        post("alice", title="Hello", text="[deleted]"),
        post("alice", title="Hello", text=""),
        post("alice", title="Hello", text="  "),
        post("alice", title="[removed]", text=""),
        comment("alice", body="[removed]"),
        comment("alice", body=" [deleted] "),
    ]
    env = Env(route({"/user/alice/overview": overview_pages([items])}))
    acts = env.source.fetch_activity(acct(), SINCE, NOW)
    assert [a.content_hash for a in acts] == [content_hash("Hello")] * 4 + [None] * 3


def test_403_after_hydrate_skips_the_second_about_call() -> None:
    table = {
        "/user/alice/about": about_ok(),
        "/user/alice/overview": httpx.Response(403),
    }
    env = Env(route(table))
    account = env.source.hydrate(acct())
    assert account.meta["is_suspended"] is False
    with pytest.raises(AccountUnavailable) as err:
        env.source.fetch_activity(account, SINCE, NOW)
    assert err.value.status == "private"
    assert [r.url.path for r in env.api()].count("/user/alice/about") == 1


# --------------------------------------------------- budget, secrets, warnings


def test_budget_counts_the_token_request_and_the_api_call() -> None:
    env = Env(route({"/user/alice/about": about_ok()}), max_requests=10)
    env.source.hydrate(acct())
    assert env.http.requests_made == 2
    assert len(env.tokens()) == 1 and len(env.api()) == 1


def test_budget_counts_both_token_requests_and_the_401_retry() -> None:
    tokens = iter(["old", "new"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return token_response(next(tokens))
        if request.headers["Authorization"] == "bearer old":
            return httpx.Response(401)
        return about_ok()

    env = Env(handler, max_requests=10)
    env.source.hydrate(acct())
    assert env.http.requests_made == 4  # token, 401, token, 200.


def test_budget_exhausted_by_the_token_request_refuses_the_api_call() -> None:
    env = Env(route({"/user/alice/about": about_ok()}), max_requests=1)
    with pytest.raises(BudgetExceeded):
        env.source.hydrate(acct())
    assert env.api() == []


def test_secrets_never_appear_in_repr_logs_or_error_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets = ["s3cr3t-value", "hunter2-pass"]
    creds = {**CREDS, PASSWORD_ENV: "hunter2-pass"}
    caplog.set_level(logging.DEBUG)
    env = Env(lambda r: httpx.Response(401, json={"message": "no"}), credentials=creds)
    with pytest.raises(SourceAuthError) as err:
        env.source.hydrate(acct())
    env.source.warn("something happened")
    with pytest.raises(SourceAuthError):
        list(env.source.discover(SeedSet("reddit", ("a",)), 1))
    haystack = " ".join([repr(env.source), repr(env.source.config), str(err.value), caplog.text])
    for secret in secrets:
        assert secret not in haystack
    assert "cid" not in repr(env.source.config)


# ----------------------------------------------------------------------- CLI

cli_runner = CliRunner()


def test_cli_scan_reddit_end_to_end_with_mock_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ts(days: float) -> float:
        return (CUTOFF + timedelta(days=days)).timestamp()

    history = {
        "alice": [ts(-5), ts(-3), ts(-1.2)],  # Steady, then silent: a cliff candidate.
        "bob": [ts(-5), ts(-3), ts(2)],  # Still posting after the cutoff.
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/access_token":
            return token_response()
        if path.startswith("/r/"):
            if path.endswith("/new"):
                return listing([post("alice", "worldnews"), post("bob", "worldnews")])
            return listing([])
        _, _, name, what = path.split("/")
        if what == "about":
            return about_ok(created_utc=ts(-400))
        return listing([comment(name, "worldnews", t) for t in sorted(history[name], reverse=True)])

    for name, value in CREDS.items():
        monkeypatch.setenv(name, value)
    built: list[tuple[float, float]] = []

    def make_client(
        max_requests: int | None = None,
        requests_per_second: float = 1.0,
        burst: float = 1.0,
        timeout: float | httpx.Timeout = 30.0,
        trust_env: bool = False,
    ) -> HttpClient:
        built.append((requests_per_second, burst))
        return HttpClient(
            max_requests=max_requests,
            transport=httpx.MockTransport(handler),
            requests_per_second=1000,
            burst=1000,
            sleep=lambda _s: None,
        )

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", make_client)
    out = tmp_path / "out.json"
    result = cli_runner.invoke(
        app,
        [
            "scan", "--platform", "reddit", "--subreddit", "worldnews", "--n", "10",
            "--seed", "1", "--now", "2026-10-14T00:00:00Z", "--min-active-days", "1",
            "--min-posts", "3", "--max-weekly-cv", "2", "--max-requests", "100",
            "--db", str(tmp_path / "c.db"),
            "--out", str(out),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [r["handle"] for r in data["candidates"]] == ["alice"]
    assert built == [(RedditSource.default_rps, RedditSource.default_burst)]


def test_cli_scan_reddit_without_credentials_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in CREDS:
        monkeypatch.delenv(name, raising=False)
    result = cli_runner.invoke(
        app, ["scan", "--platform", "reddit", "--db", str(tmp_path / "c.db")]
    )
    assert result.exit_code == 2
    assert "BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID" in result.output
    assert "BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME" in result.output


# ------------------------------------------------ round-2 critic findings


def _auth_ok(request: httpx.Request) -> httpx.Response | None:
    return token_response() if request.url.path == "/api/v1/access_token" else None


def test_discovery_budget_counts_requests_actually_sent_including_429s() -> None:
    sent = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if (early := _auth_ok(request)) is not None:
            return early
        sent[0] += 1
        if sent[0] % 2 == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return listing([post(f"l{sent[0]}_{i}", ts=POST) for i in range(100)], after=f"p{sent[0]}")

    env = Env(handler, max_requests=40, discovery_budget="0.25", discovery="listing", retries=3)
    list(env.source.discover(SeedSet("reddit", ("big",)), 10))
    # int(40 * 0.25) = 10 sent requests, 429s and the token call included. The check runs
    # before each call, so the last call may overshoot by its own retries (at most 3).
    assert 9 <= len(env.requests) <= 13


def test_hydrate_between_yields_does_not_shrink_the_discovery_pool() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (early := _auth_ok(request)) is not None:
            return early
        if request.url.path.startswith("/user/"):
            return about_ok(created_utc=EPOCH - 86400 * 400)
        k = int(request.url.params.get("after", "p0")[1:])
        if k >= 60:
            return listing([])
        return listing([post(f"u{k}_{i}") for i in range(10)], after=f"p{k + 1}")

    def count(hydrate: bool) -> int:
        env = Env(handler, max_requests=1000, discovery="listing")
        got = 0
        for account in env.source.discover(SeedSet("reddit", ("a",)), 1000):
            if hydrate:
                env.source.hydrate(account)
            got += 1
        return got

    assert count(True) == count(False) == 600


def test_tiny_budget_still_allows_one_discovery_request() -> None:
    env = Env(
        discover_handler({"/r/a/new": paged([[post("x")]]), "/r/b/new": paged([[post("y")]])}),
        max_requests=3,
        discovery="listing",
    )
    # int(3 * 0.3) = 0, floored at 1: the first page is read, the next subreddit is not.
    found = [a.handle for a in env.source.discover(SeedSet("reddit", ("a", "b")), 5)]
    assert found == ["x"]
    assert any("discovery budget" in w for w in env.source.drain_warnings())


def test_budget_warning_is_emitted_once_across_streams() -> None:
    env = Env(discover_handler({}), max_requests=4, discovery="both", discovery_budget="0.5")
    list(env.source.discover(SeedSet("reddit", ("a", "b", "c")), 10))
    assert len([w for w in env.source.drain_warnings() if "discovery budget" in w]) == 1


def test_listing_5xx_on_one_subreddit_skips_it_and_scans_the_rest() -> None:
    env = Env(
        discover_handler({"/r/bad/new": httpx.Response(503), "/r/good/new": listing([post("ok")])}),
        discovery="listing",
        retries=2,
    )
    found = [a.handle for a in env.source.discover(SeedSet("reddit", ("bad", "good")), 5)]
    assert found == ["ok"]
    assert any("r/bad new" in w for w in env.source.drain_warnings())


def test_html_200_on_a_thread_skips_the_thread_and_keeps_going() -> None:
    env = Env(
        discover_handler(
            {
                "/r/a/top": listing([thread("t1", "op1", PRE), thread("t2", "op2", PRE - 1)]),
                "/comments/t1": httpx.Response(200, content=b"<html>"),
                "/comments/t2": comments_response([node("c2", PRE)]),
                "/r/b/top": listing([thread("t3", "op3", PRE)]),
                "/comments/t3": comments_response([node("c3", PRE)]),
            }
        ),
        discovery="threads",
    )
    found = [a.handle for a in env.source.discover(SeedSet("reddit", ("a", "b")), 20)]
    assert sorted(found) == ["c2", "c3", "op1", "op2", "op3"]
    assert any("skipped a thread in r/a" in w for w in env.source.drain_warnings())


def test_run_fatal_errors_still_abort_discovery() -> None:
    env = Env(
        discover_handler({"/r/a/new": httpx.Response(401)}),
        discovery="listing",
    )
    with pytest.raises(SourceAuthError):
        list(env.source.discover(SeedSet("reddit", ("a",)), 5))


def test_threads_are_filtered_to_the_window_before_the_per_sub_slice() -> None:
    start = CUTOFF - timedelta(days=30)
    recent = [thread(f"new{i}", f"n{i}", POST + i) for i in range(5)]
    table: dict[str, Handler | httpx.Response] = {
        "/r/a/top": listing([*recent, thread("good", "op", PRE)]),
        "/comments/good": comments_response([]),
    }
    env = Env(
        discover_handler(table),
        discovery="threads",
        baseline_start=start,
        now=CUTOFF + timedelta(days=20),
    )
    found = [a.handle for a in env.source.discover(SeedSet("reddit", ("a",)), 5)]
    assert env.api()[0].url.params["t"] == "year"
    assert found == ["op"]  # Not starved by the five post-cutoff threads.


def test_warns_when_no_thread_qualifies() -> None:
    env = Env(
        discover_handler({"/r/a/top": listing([thread("t1", "op", POST)])}), discovery="threads"
    )
    assert list(env.source.discover(SeedSet("reddit", ("a",)), 5)) == []
    assert any("no top threads" in w for w in env.source.drain_warnings())


def test_non_numeric_timestamps_are_skipped() -> None:
    bad = {**post("bad"), "created_utc": "soon"}
    env = Env(discover_handler({"/r/a/new": paged([[bad, post("fine")]])}), discovery="listing")
    assert [a.handle for a in env.source.discover(SeedSet("reddit", ("a",)), 5)] == ["fine"]


def test_non_numeric_expires_in_is_a_source_error() -> None:
    env = Env(lambda r: httpx.Response(200, json={"access_token": "x", "expires_in": "later"}))
    with pytest.raises(SourceError, match="expires_in"):
        env.source.hydrate(acct())


def test_username_prefix_is_stripped_case_insensitively() -> None:
    creds = {**CREDS, "BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME": "/U/Test"}
    http = HttpClient(transport=httpx.MockTransport(lambda r: token_response()))
    src = RedditSource(SourceConfig(http=http, credentials=creds, now=NOW, cutoff=CUTOFF))
    assert src.user_agent.endswith("(by /u/Test)")


def test_reddit_has_no_placebo_samples() -> None:
    http = HttpClient(transport=httpx.MockTransport(lambda r: token_response()))
    src = RedditSource(SourceConfig(http=http, credentials=CREDS, now=NOW, cutoff=CUTOFF))
    problem = src.placebo_discovery_problem(SeedSet("reddit", ("a",)))
    assert problem is not None and "1000 items" in problem and "reddit-archive" in problem
