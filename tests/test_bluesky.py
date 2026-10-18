"""Bluesky adapter, against a scripted fake of the AppView and a PDS (no network)."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import _Redactor, app
from bot_farm_silence_tracker.models import Account, content_hash
from bot_farm_silence_tracker.seeds import SeedSet, load_seeds
from bot_farm_silence_tracker.sources.base import (
    AccountUnavailable,
    RateLimited,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.bluesky import (
    DEFAULT_PDS,
    HANDLE_ENV,
    PASSWORD_ENV,
    USER_AGENT,
    BlueskySource,
    _stray_allowance,
    parse_seed,
)
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.registry import collect_credentials, load_source_class

from .helpers import CUTOFF, NOW

runner = CliRunner()

PUBLIC_HOST = "public.api.bsky.app"
PDS_HOST = "bsky.social"
LOGIN = {HANDLE_ENV: "researcher.example", PASSWORD_ENV: "abcd-efgh-ijkl-mnop"}
FEED_URI = "at://did:plc:feedowner/app.bsky.feed.generator/topic"
LIST_URI = "at://did:plc:listowner/app.bsky.graph.list/members"


def z(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def post_view(
    did: str,
    indexed: datetime,
    *,
    text: str = "hello",
    created: datetime | None = None,
    reply: bool = False,
    quote: bool = False,
    n: int = 0,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": z(created or indexed),
    }
    if reply:
        record["reply"] = {"root": {"uri": "at://x"}, "parent": {"uri": "at://x"}}
    if quote:
        record["embed"] = {"$type": "app.bsky.embed.record", "record": {"uri": "at://y"}}
    return {
        "uri": f"at://{did}/app.bsky.feed.post/{n}-{indexed.timestamp():.0f}",
        "cid": "bafy",
        "author": {"did": did, "handle": f"{did.rsplit(':', 1)[-1]}.bsky.social"},
        "record": record,
        "indexedAt": z(indexed),
    }


def repost_item(by: str, original: dict[str, Any], when: datetime) -> dict[str, Any]:
    return {
        "post": original,
        "reason": {
            "$type": "app.bsky.feed.defs#reasonRepost",
            "by": {"did": by, "handle": "x"},
            "indexedAt": z(when),
        },
    }


def sort_key(item: dict[str, Any]) -> datetime:
    reason = item.get("reason")
    if reason:
        return parse(reason["indexedAt"])
    post = item["post"]
    return min(parse(post["indexedAt"]), parse(post["record"]["createdAt"]))


def xrpc_error(status: int, error: str, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": error, "message": message})


class Bsky:
    """A fake AppView plus PDS.

    ``feeds`` maps a DID to its author-feed items, ``profiles`` maps a DID to a
    profile body or an error response, ``posts`` are the searchable posts, and
    ``generators`` maps a feed or list URI to its items. ``script`` responses are
    served first, and ``hook(request)`` may override any answer.
    """

    def __init__(
        self,
        *,
        feeds: dict[str, list[dict[str, Any]]] | None = None,
        profiles: dict[str, Any] | None = None,
        posts: list[dict[str, Any]] | None = None,
        generators: dict[str, list[dict[str, Any]]] | None = None,
        lists: dict[str, tuple[str, list[dict[str, Any]]]] | None = None,
        threads: dict[str, list[dict[str, Any]]] | None = None,
        starter_packs: dict[str, str] | None = None,
        search_needs_auth: bool = True,
        script: list[httpx.Response] | None = None,
        hook: Callable[[httpx.Request], httpx.Response | None] | None = None,
    ):
        self.feeds = feeds or {}
        self.profiles = profiles or {}
        self.posts = posts or []
        self.generators = generators or {}
        self.lists = lists or {}
        self.threads = threads or {}
        self.starter_packs = starter_packs or {}
        self.search_needs_auth = search_needs_auth
        self.script = list(script or [])
        self.hook = hook
        self.requests: list[httpx.Request] = []
        self.sessions = 0
        self.refreshes = 0
        self.access = ""
        self.expire_next = False

    def method(self, index: int) -> str:
        return self.requests[index].url.path.rsplit(".", 1)[-1]

    def methods(self) -> list[str]:
        return [r.url.path.rsplit(".", 1)[-1] for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if self.hook is not None:
            answer = self.hook(request)
            if answer is not None:
                return answer
        method = request.url.path.rsplit(".", 1)[-1]
        params = request.url.params
        if method == "createSession":
            body = json.loads(request.content)
            if body != {"identifier": LOGIN[HANDLE_ENV], "password": LOGIN[PASSWORD_ENV]}:
                return xrpc_error(401, "AuthenticationRequired", "Invalid identifier or password")
            self.sessions += 1
            self.access = f"access-{self.sessions}"
            return httpx.Response(
                200,
                json={
                    "accessJwt": self.access,
                    "refreshJwt": f"refresh-{self.sessions}",
                    "did": "did:plc:me",
                    "handle": LOGIN[HANDLE_ENV],
                },
            )
        if method == "refreshSession":
            if request.headers.get("authorization") != f"Bearer refresh-{self.sessions}":
                return xrpc_error(400, "ExpiredToken")
            self.refreshes += 1
            self.access = f"access-{self.sessions}-r{self.refreshes}"
            return httpx.Response(
                200, json={"accessJwt": self.access, "refreshJwt": f"refresh-{self.sessions}"}
            )
        if method == "searchPosts":
            authed = request.headers.get("authorization")
            if self.search_needs_auth and not authed:
                return httpx.Response(403, text="<html><title>403 Forbidden</title></html>")
            if authed:
                if self.expire_next:
                    self.expire_next = False
                    return xrpc_error(400, "ExpiredToken", "Token has expired")
                if authed != f"Bearer {self.access}":
                    return xrpc_error(401, "InvalidToken")
            since, until = parse(params["since"]), parse(params["until"])
            hits = [
                p
                for p in self.posts
                if since <= min(parse(p["indexedAt"]), parse(p["record"]["createdAt"])) < until
                and params["q"].lstrip("#").lower() in p["record"]["text"].lower()
            ]
            hits.sort(key=lambda p: p["indexedAt"], reverse=True)
            return httpx.Response(200, json={"posts": hits[: int(params.get("limit", 25))]})
        if method == "getProfile":
            actor = params["actor"]
            found = self.profiles.get(actor)
            if isinstance(found, httpx.Response):
                return found
            if found is None:
                return xrpc_error(400, "InvalidRequest", "Profile not found")
            return httpx.Response(200, json=found)
        if method == "getAuthorFeed":
            actor = params["actor"]
            if actor not in self.feeds:
                return xrpc_error(400, "InvalidRequest", "Profile not found")
            return self.page(sorted(self.feeds[actor], key=sort_key, reverse=True), params)
        if method == "getList":
            uri = params["list"]
            if uri not in self.lists:
                return xrpc_error(400, "InvalidRequest", "List not found")
            purpose, members = self.lists[uri]
            begin = int(params.get("cursor") or 0)
            limit = int(params.get("limit", 50))
            listing: dict[str, Any] = {
                "list": {"uri": uri, "name": "a list", "purpose": purpose},
                "items": [
                    {"uri": f"{uri}/item{begin + k}", "subject": m}
                    for k, m in enumerate(members[begin : begin + limit])
                ],
            }
            if begin + limit < len(members):
                listing["cursor"] = str(begin + limit)
            return httpx.Response(200, json=listing)
        if method == "getPostThread":
            uri = params["uri"]
            if uri not in self.threads:
                return xrpc_error(400, "NotFound", "Post not found")
            thread = {
                "$type": "app.bsky.feed.defs#threadViewPost",
                "post": {"uri": uri},
                "replies": self.threads[uri],
            }
            return httpx.Response(200, json={"thread": thread})
        if method == "getStarterPack":
            uri = params["starterPack"]
            if uri not in self.starter_packs:
                return xrpc_error(400, "NotFound", "Starter pack not found")
            pack = {"uri": uri, "list": {"uri": self.starter_packs[uri], "name": "pack"}}
            return httpx.Response(200, json={"starterPack": pack})
        if method in ("getFeed", "getListFeed"):
            uri = params["feed"] if method == "getFeed" else params["list"]
            if uri not in self.generators:
                return xrpc_error(400, "UnknownFeed" if method == "getFeed" else "UnknownList")
            return self.page(self.generators[uri], params)
        return httpx.Response(404, json={"error": "MethodNotImplemented"})

    @staticmethod
    def page(items: list[dict[str, Any]], params: httpx.QueryParams) -> httpx.Response:
        start = int(params.get("cursor") or 0)
        limit = int(params.get("limit", 50))
        chunk = items[start : start + limit]
        body: dict[str, Any] = {"feed": chunk}
        if start + limit < len(items):
            body["cursor"] = str(start + limit)
        return httpx.Response(200, json=body)


def make_source(
    fake: Callable[[httpx.Request], httpx.Response],
    *,
    credentials: dict[str, str] | None = None,
    baseline_days: int = 30,
    seed: int | None = 1,
    max_requests: int | None = None,
    sleeps: list[float] | None = None,
    wall: float = 1_000_000.0,
    concurrency: int = 1,
    **options: object,
) -> BlueskySource:
    http = HttpClient(
        requests_per_second=BlueskySource.default_rps,
        burst=BlueskySource.default_burst,
        transport=httpx.MockTransport(fake),
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
        clock=lambda: 0.0,
        max_requests=max_requests,
    )
    config = SourceConfig(
        http=http,
        credentials=credentials or {},
        now=NOW,
        cutoff=CUTOFF,
        seed=seed,
        options=options,
        baseline_start=CUTOFF - timedelta(days=baseline_days),
        concurrency=concurrency,
    )
    return BlueskySource(config, wall_clock=lambda: wall)


def account(did: str = "did:plc:alice") -> Account:
    return Account("bluesky", did, "alice.bsky.social")


# -- seeds, options and registration ---------------------------------------------------


def test_registry_seeds_and_credentials() -> None:
    cls = load_source_class("bluesky")
    assert cls is BlueskySource
    assert cls.env_platform == "BSKY"
    assert set(cls.optional_credentials) == {HANDLE_ENV, PASSWORD_ENV}
    assert collect_credentials(cls, {}) == {}
    assert collect_credentials(cls, LOGIN) == LOGIN
    seeds = load_seeds("bluesky")
    assert "#Ukraine" in seeds.items and "Ukraine war" in seeds.items


def test_parse_seed_variants() -> None:
    assert parse_seed("#NATO").kind == "query"
    assert parse_seed(FEED_URI).kind == "feed"
    assert parse_seed(LIST_URI).uri == LIST_URI
    feed = parse_seed("https://bsky.app/profile/someone.example/feed/topic")
    assert (feed.kind, feed.uri) == ("feed", "at://someone.example/app.bsky.feed.generator/topic")
    lst = parse_seed("https://bsky.app/profile/did:plc:abc/lists/xyz")
    assert (lst.kind, lst.uri) == ("list", "at://did:plc:abc/app.bsky.graph.list/xyz")
    with pytest.raises(ValueError):
        parse_seed("at://did:plc:abc/app.bsky.feed.post/1")
    with pytest.raises(ValueError):
        parse_seed("https://bsky.app/profile/abc")


@pytest.mark.parametrize(
    "options",
    [
        {"app_password": "x"},
        {"handle": "someone"},
        {"appview_url": "https://public.api.bsky.app"},
        {"appview_url": "https://appview.example"},
        {"freshness_feed": "at://did:plc:x/app.bsky.feed.generator/y"},
        {"pds_url": "https://bsky.social/xrpc"},
        {"page_size": "101"},
        {"max_rps": "50"},
        {"seed_feeds": LIST_URI},
    ],
)
def test_bad_options_are_refused(options: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        make_source(Bsky(), **options)


def test_half_a_login_is_refused() -> None:
    with pytest.raises(ValueError, match="both"):
        make_source(Bsky(), credentials={HANDLE_ENV: "someone"})


def test_secret_option_names_are_filtered_from_storage() -> None:
    from bot_farm_silence_tracker.pipeline import is_secret_option

    for key in BlueskySource.secret_options:
        assert is_secret_option(key, BlueskySource.secret_options)


# -- activity and pagination ----------------------------------------------------------


def busy_feed(did: str, count: int, *, step_hours: float = 5.0) -> list[dict[str, Any]]:
    start = NOW - timedelta(hours=1)
    return [
        {"post": post_view(did, start - timedelta(hours=step_hours * i), text=f"post {i}", n=i)}
        for i in range(count)
    ]


def test_fetch_activity_pages_back_to_since_and_stops() -> None:
    did = "did:plc:alice"
    fake = Bsky(feeds={did: busy_feed(did, 400)})  # 400 posts, 5 h apart: ~83 days.
    source = make_source(fake)
    since, until = CUTOFF - timedelta(days=30), NOW
    acts = source.fetch_activity(account(did), since, until)
    expected = [i for i in range(400) if since <= NOW - timedelta(hours=1 + 5 * i) < until]
    assert len(acts) == len(expected)
    pages = [r for r in fake.requests if r.url.path.endswith("getAuthorFeed")]
    # 4 days after the cutoff + 30 days back = 34 days = ~164 posts: two pages of 100.
    assert len(pages) == 2
    assert pages[1].url.params["cursor"] == "100"
    assert all(r.url.params["filter"] == "posts_with_replies" for r in pages)
    assert all(r.url.host == PUBLIC_HOST for r in fake.requests)
    assert all(r.headers["user-agent"] == USER_AGENT for r in fake.requests)
    assert all("authorization" not in r.headers for r in fake.requests)
    assert account(did).meta == {}
    assert {a.kind for a in acts} == {"post"}
    assert acts[0].content_hash == content_hash("post 0")


def test_fetch_activity_truncates_at_max_pages() -> None:
    did = "did:plc:alice"
    fake = Bsky(feeds={did: busy_feed(did, 400)})
    source = make_source(fake, max_pages="1")
    acct = account(did)
    acts = source.fetch_activity(acct, CUTOFF - timedelta(days=30), NOW)
    assert len(acts) == 100
    assert acct.meta["truncated"] is True


def test_kinds_reposts_and_indexed_time() -> None:
    did = "did:plc:alice"
    other = post_view("did:plc:bob", CUTOFF - timedelta(days=3), text="bob's post")
    spoofed = post_view(
        did,
        CUTOFF - timedelta(days=2),
        text="claims to be from the future",
        created=CUTOFF + timedelta(days=300),
        n=9,
    )
    items = [
        {"post": post_view(did, CUTOFF - timedelta(days=1), text="a reply", reply=True, n=1)},
        {"post": post_view(did, CUTOFF - timedelta(days=1, hours=1), text="a quote", quote=True)},
        repost_item(did, other, CUTOFF - timedelta(days=1, hours=2)),
        {"post": spoofed},
        {"post": post_view(did, CUTOFF - timedelta(days=4), text="plain", n=3)},
    ]
    fake = Bsky(feeds={did: items})
    since, until = CUTOFF - timedelta(days=10), NOW
    acts = make_source(fake).fetch_activity(account(did), since, until)
    by_kind = {a.kind: a for a in acts}
    assert set(by_kind) == {"reply", "quote", "post"}
    # The future createdAt is ignored: the post counts when the AppView indexed it.
    assert CUTOFF - timedelta(days=2) in {a.ts for a in acts}
    assert len(acts) == 4

    with_reposts = make_source(Bsky(feeds={did: items}), include_reposts="true")
    acts = with_reposts.fetch_activity(account(did), since, until)
    reposts = [a for a in acts if a.kind == "repost"]
    assert [r.ts for r in reposts] == [CUTOFF - timedelta(days=1, hours=2)]
    assert reposts[0].content_hash is None  # Not the account's own words.


def test_fetch_activity_keeps_pages_on_a_mid_walk_error() -> None:
    did = "did:plc:alice"
    calls = {"n": 0}

    def hook(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("getAuthorFeed"):
            calls["n"] += 1
            if calls["n"] == 2:
                return xrpc_error(400, "InternalServerError", "boom")
        return None

    fake = Bsky(feeds={did: busy_feed(did, 400)}, hook=hook)
    with pytest.raises(SourceError) as info:
        make_source(fake).fetch_activity(account(did), CUTOFF - timedelta(days=30), NOW)
    assert info.value.partial is not None and len(info.value.partial) == 100
    assert did not in str(info.value)


# -- account status and hydration -----------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (xrpc_error(400, "AccountTakedown", "Account has been suspended"), "suspended"),
        (xrpc_error(400, "AccountDeactivated", "Account is deactivated"), "deleted"),
        (xrpc_error(400, "InvalidRequest", "Profile not found"), "deleted"),
    ],
)
def test_hydrate_maps_account_states(answer: httpx.Response, status: str) -> None:
    did = "did:plc:gone"
    source = make_source(Bsky(profiles={did: answer}))
    with pytest.raises(AccountUnavailable) as info:
        source.hydrate(account(did))
    assert info.value.status == status


def test_hydrate_reads_created_at_once() -> None:
    did = "did:plc:alice"
    profile = {"did": did, "handle": "alice.new", "createdAt": "2026-09-01T12:00:00.000Z"}
    fake = Bsky(profiles={did: profile})
    source = make_source(fake)
    acct = source.hydrate(account(did))
    assert acct.created_at == datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert acct.handle == "alice.new"
    source.hydrate(acct)
    no_date = Account("bluesky", did, "x")
    fake.profiles[did] = {"did": did, "handle": "alice.new"}
    source.hydrate(no_date)
    source.hydrate(no_date)  # Profile already read: no second request.
    assert fake.methods().count("getProfile") == 2
    assert no_date.created_at is None


def test_author_feed_not_found_is_resolved_through_the_profile() -> None:
    did = "did:plc:banned"
    fake = Bsky(profiles={did: xrpc_error(400, "AccountTakedown", "Account has been suspended")})
    with pytest.raises(AccountUnavailable) as info:
        make_source(fake).fetch_activity(account(did), CUTOFF - timedelta(days=30), NOW)
    assert info.value.status == "suspended"
    assert fake.methods() == ["getAuthorFeed", "getProfile"]


# -- discovery ------------------------------------------------------------------------


def search_world(authors: int = 60) -> list[dict[str, Any]]:
    """Posts mentioning #topic, one author every ~12 hours across 35 days."""
    start = CUTOFF - timedelta(days=31)
    out = []
    for i in range(authors * 2):
        when = start + timedelta(hours=12 * i + 3)
        out.append(post_view(f"did:plc:a{i % authors}", when, text="about #topic", n=i))
    return out


def test_discovery_without_login_needs_a_feed_or_fails_clearly() -> None:
    fake = Bsky(posts=search_world())
    source = make_source(fake)
    with pytest.raises(SourceAuthError) as info:
        list(source.discover(SeedSet("bluesky", ("#topic",)), 10))
    assert HANDLE_ENV in str(info.value) and PASSWORD_ENV in str(info.value)
    assert fake.requests == []  # Refused up front: nothing was sent.


def test_discovery_falls_back_to_feed_seeds_without_login() -> None:
    did_items = [
        {"post": post_view(f"did:plc:f{i}", CUTOFF - timedelta(hours=6 * i + 1), n=i)}
        for i in range(150)
    ] + [{"post": post_view("did:plc:late", CUTOFF + timedelta(hours=2))}]
    did_items.sort(key=sort_key, reverse=True)
    fake = Bsky(posts=search_world(), generators={FEED_URI: did_items})
    source = make_source(fake, seed_feeds=FEED_URI, page_size="50")
    pool = list(source.discover(SeedSet("bluesky", ("#topic",)), 40))
    assert len(pool) == 40
    assert "did:plc:late" not in {a.user_id for a in pool}
    assert all(a.meta["community"] == FEED_URI for a in pool)
    warnings = source.drain_warnings()
    assert any("search seed was skipped" in w for w in warnings)


def test_discovery_with_login_samples_the_whole_window() -> None:
    fake = Bsky(posts=search_world(), search_needs_auth=True)
    source = make_source(fake, credentials=LOGIN, page_size="5", seed=7)
    pool = list(source.discover(SeedSet("bluesky", ("#topic",)), 30))
    assert len(pool) == 30
    assert len({a.user_id for a in pool}) == 30
    searches = [r for r in fake.requests if r.url.path.endswith("searchPosts")]
    start = CUTOFF - timedelta(days=30)
    untils = [parse(r.url.params["until"]) for r in searches]
    assert all(start < u <= CUTOFF for u in untils)
    assert all(parse(r.url.params["since"]) >= start for r in searches)
    # Draws spread over the window rather than piling into the last days.
    assert min(untils) < start + timedelta(days=10) and max(untils) > CUTOFF - timedelta(days=10)
    assert all(r.url.host == PDS_HOST for r in searches)
    assert all(r.headers["authorization"] == "Bearer access-1" for r in searches)
    assert all(r.headers["atproto-proxy"] == "did:web:api.bsky.app#bsky_appview" for r in searches)
    assert fake.sessions == 1
    # The same seed gives the same pool.
    again = make_source(
        Bsky(posts=search_world()), credentials=LOGIN, page_size="5", seed=7
    ).discover(SeedSet("bluesky", ("#topic",)), 30)
    assert [a.user_id for a in again] == [a.user_id for a in pool]


def test_discovery_caps_each_seed_and_resolves_handles_in_urls() -> None:
    items_a = [
        {"post": post_view(f"did:plc:x{i}", CUTOFF - timedelta(hours=i + 1), n=i)}
        for i in range(40)
    ]
    items_b = [
        {"post": post_view(f"did:plc:y{i}", CUTOFF - timedelta(hours=i + 1), n=i)}
        for i in range(40)
    ]
    owner = {"did": "did:plc:listowner", "handle": "owner.example"}
    fake = Bsky(
        generators={FEED_URI: items_a, LIST_URI: items_b},
        profiles={"owner.example": owner},
    )
    source = make_source(fake, page_size="10", list_mode="feed")
    seeds = SeedSet("bluesky", (FEED_URI, "https://bsky.app/profile/owner.example/lists/members"))
    pool = list(source.discover(seeds, 10))
    sources = [a.meta["community"] for a in pool]
    assert sources.count(FEED_URI) == 5 and len(pool) == 10
    list_calls = [r for r in fake.requests if r.url.path.endswith("getListFeed")]
    assert list_calls and all(r.url.params["list"] == LIST_URI for r in list_calls)
    assert fake.methods().count("getProfile") == 1


def test_bad_feed_seed_is_warned_and_dropped() -> None:
    items = [{"post": post_view("did:plc:ok", CUTOFF - timedelta(hours=3))}]
    fake = Bsky(generators={FEED_URI: items})
    source = make_source(fake)
    bad = "at://did:plc:nobody/app.bsky.feed.generator/missing"
    pool = list(source.discover(SeedSet("bluesky", (bad, FEED_URI, "at://broken")), 5))
    assert [a.user_id for a in pool] == ["did:plc:ok"]
    warnings = " ".join(source.drain_warnings())
    assert "UnknownFeed" in warnings and "at://broken" in warnings


# -- auth -----------------------------------------------------------------------------


def test_expired_session_is_refreshed_once() -> None:
    fake = Bsky(posts=search_world())
    source = make_source(fake, credentials=LOGIN, page_size="5")
    pool = source.discover(SeedSet("bluesky", ("#topic",)), 50)
    next(pool)
    fake.expire_next = True
    for _ in range(5):
        next(pool)
    assert fake.sessions == 1 and fake.refreshes == 1
    assert fake.methods().count("refreshSession") == 1
    refresh = next(r for r in fake.requests if r.url.path.endswith("refreshSession"))
    assert refresh.headers["authorization"] == "Bearer refresh-1"
    assert "atproto-proxy" not in refresh.headers


def test_bad_login_is_a_scan_ending_auth_error_without_secrets() -> None:
    fake = Bsky(posts=search_world())
    creds = {HANDLE_ENV: "wrong.example", PASSWORD_ENV: "zzzz-zzzz-zzzz-zzzz"}
    source = make_source(fake, credentials=creds)
    with pytest.raises(SourceAuthError) as info:
        list(source.discover(SeedSet("bluesky", ("#topic",)), 5))
    text = str(info.value)
    assert "wrong.example" not in text and "zzzz" not in text
    assert HANDLE_ENV in text
    assert fake.methods() == ["createSession"]


def test_secrets_never_reach_logs_rows_or_summaries(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    fake = Bsky(posts=search_world())
    source = make_source(fake, credentials=LOGIN, page_size="5")
    list(source.discover(SeedSet("bluesky", ("#topic",)), 10))
    rows = source.drain_request_log(final=True)
    assert rows and rows[0]["endpoint"] == "createSession" and rows[0]["phase"] == "auth"
    blob = json.dumps(
        [rows, source.request_summary(), source.drain_warnings(), repr(source.config)]
    )
    blob += caplog.text
    for secret in (*LOGIN.values(), "access-1", "refresh-1", "#topic"):
        assert secret not in blob
    create = fake.requests[0]
    assert create.url.host == PDS_HOST and create.method == "POST"
    assert str(create.url) == f"{DEFAULT_PDS}/xrpc/com.atproto.server.createSession"


# -- rate limits ----------------------------------------------------------------------


def test_429_waits_for_ratelimit_reset_and_halves_the_rate() -> None:
    did = "did:plc:alice"
    wall = 1_000_000.0
    sleeps: list[float] = []
    limited = httpx.Response(
        429,
        json={"error": "RateLimitExceeded"},
        headers={"RateLimit-Remaining": "0", "RateLimit-Reset": str(int(wall + 30))},
    )
    fake = Bsky(feeds={did: busy_feed(did, 3)}, script=[limited])
    source = make_source(fake, sleeps=sleeps, wall=wall)
    acts = source.fetch_activity(account(did), CUTOFF - timedelta(days=30), NOW)
    assert len(acts) == 3
    assert 31.0 in sleeps  # Reset in 30 s plus the 1 s margin.
    summary = source.request_summary()
    assert summary is not None
    assert summary["rate_halvings"] == 1 and summary["final_rps"] == 0.5
    assert summary["load_responses"] == 1
    statuses = [row["status"] for row in source.drain_request_log(final=True)]
    assert statuses == [429, 200]


def test_exhausted_window_pauses_before_the_next_request() -> None:
    did = "did:plc:alice"
    wall = 1_000_000.0
    headers = {"RateLimit-Remaining": "0", "RateLimit-Reset": str(int(wall + 12))}

    def hook(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("getProfile"):
            body = {"did": did, "handle": "a", "createdAt": "2026-01-01T00:00:00Z"}
            return httpx.Response(200, json=body, headers=headers)
        return None

    fake = Bsky(feeds={did: busy_feed(did, 3)}, hook=hook)
    source = make_source(fake, wall=wall)
    acct = source.hydrate(account(did))
    assert source.http.paused_until is not None
    source.fetch_activity(acct, CUTOFF - timedelta(days=30), NOW)
    summary = source.request_summary()
    assert summary is not None and summary["reset_holds"] == 1


def test_persistent_429_ends_the_scan() -> None:
    did = "did:plc:alice"
    fake = Bsky(hook=lambda _r: httpx.Response(429, headers={"Retry-After": "2"}))
    source = make_source(fake)
    with pytest.raises(RateLimited):
        source.fetch_activity(account(did), CUTOFF - timedelta(days=30), NOW)
    summary = source.request_summary()
    assert summary is not None and summary["final_rps"] < 1.0


# -- horizon and pre-check ------------------------------------------------------------


def test_source_is_live_and_sends_no_probe() -> None:
    fake = Bsky()
    source = make_source(fake)
    assert source.data_horizon() is None
    assert source.fallback_horizon() is None
    assert fake.requests == []


def test_newest_activity_answers_with_one_request() -> None:
    did = "did:plc:alice"
    fake = Bsky(feeds={did: busy_feed(did, 400)})
    source = make_source(fake)
    found = source.newest_activity(account(did), "any", CUTOFF - timedelta(days=2), NOW)
    assert found.newest == NOW - timedelta(hours=1)
    assert found.covered_since == CUTOFF - timedelta(days=2)
    assert len(fake.requests) == 1 and fake.requests[0].url.params["limit"] == "25"

    silent = "did:plc:quiet"
    old = [{"post": post_view(silent, CUTOFF - timedelta(days=5))}]
    fake = Bsky(feeds={silent: old})
    found = make_source(fake).newest_activity(
        account(silent), "any", CUTOFF - timedelta(days=2), NOW
    )
    assert found.newest is None and len(fake.requests) == 1


# -- CLI end to end ---------------------------------------------------------------------


def world() -> Bsky:
    """Twelve accounts: six go silent together at the cutoff, six keep posting."""
    feeds: dict[str, list[dict[str, Any]]] = {}
    profiles: dict[str, Any] = {}
    feed_items: list[dict[str, Any]] = []
    for i in range(12):
        did = f"did:plc:acct{i}"
        bot = i < 6
        end = CUTOFF - timedelta(minutes=10 + i) if bot else NOW - timedelta(hours=2)
        items = []
        moment = CUTOFF - timedelta(days=31) + timedelta(minutes=13 * i)
        n = 0
        while moment < end:
            items.append({"post": post_view(did, moment, text=f"{did} {n % 5}", n=n)})
            moment += timedelta(hours=5)
            n += 1
        feeds[did] = items
        profiles[did] = {
            "did": did,
            "handle": f"acct{i}.bsky.social",
            "createdAt": z(CUTOFF - timedelta(days=200)),
        }
        feed_items.append(
            {"post": post_view(did, CUTOFF - timedelta(hours=30 + i), text="acct news", n=999)}
        )
    feed_items.sort(key=sort_key, reverse=True)
    return Bsky(
        feeds=feeds,
        profiles=profiles,
        generators={FEED_URI: feed_items},
        posts=[i["post"] for items in feeds.values() for i in items],
    )


def patch_client(monkeypatch: pytest.MonkeyPatch, fake: Bsky) -> None:
    def patched_client(**kwargs: Any) -> HttpClient:
        return HttpClient(
            **kwargs,
            transport=httpx.MockTransport(fake),
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", patched_client)


SCAN = [
    "scan", "--platform", "bluesky", "--n", "12", "--pool-factor", "1", "--seed", "3",
    "--now", "2026-10-14T00:00:00Z", "--max-requests", "500",
]  # fmt: skip


def test_cli_scan_bluesky_from_a_feed_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = world()
    patch_client(monkeypatch, fake)
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    out = tmp_path / "out.json"
    result = runner.invoke(
        app,
        [*SCAN, "--query", FEED_URI, "--db", str(tmp_path / "c.db"), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert {c["user_id"] for c in payload["candidates"]} == {f"did:plc:acct{i}" for i in range(6)}
    assert payload["summary"]["sampled"] == 12
    assert {r.url.host for r in fake.requests} == {PUBLIC_HOST}
    assert all(r.headers["user-agent"] == USER_AGENT for r in fake.requests)


def test_cli_scan_bluesky_with_login_keeps_secrets_out_of_the_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = world()
    patch_client(monkeypatch, fake)
    for name, value in LOGIN.items():
        monkeypatch.setenv(name, value)
    db = tmp_path / "c.db"
    out = tmp_path / "out.csv"
    result = runner.invoke(app, [*SCAN, "--query", "acct", "--db", str(db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert fake.sessions == 1
    assert any(r.url.host == PDS_HOST for r in fake.requests)
    for secret in (*LOGIN.values(), "access-1", "refresh-1"):
        assert secret not in result.output
        assert secret.encode() not in db.read_bytes()
        assert secret not in out.read_text()
    with sqlite3.connect(db) as conn:
        endpoints = {row[0] for row in conn.execute("SELECT endpoint FROM request_log")}
    assert {"createSession", "searchPosts", "getAuthorFeed"} <= endpoints


def test_cli_scan_bluesky_two_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = world()
    patch_client(monkeypatch, fake)
    out = tmp_path / "out.json"
    result = runner.invoke(
        app,
        [
            *SCAN, "--query", FEED_URI, "--baseline-sample-rate", "0.5", "--concurrency", "2",
            "--db", str(tmp_path / "c.db"), "--out", str(out),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert {c["user_id"] for c in payload["candidates"]} == {f"did:plc:acct{i}" for i in range(6)}


def test_cli_rejects_more_than_three_workers() -> None:
    result = runner.invoke(app, ["scan", "--platform", "bluesky", "--concurrency", "4"])
    assert result.exit_code != 0
    assert "at most 3" in result.output


def test_request_summary_line_has_no_archive_figures() -> None:
    from bot_farm_silence_tracker.pipeline import format_request_summary

    source = make_source(Bsky())
    summary = source.request_summary()
    assert summary is not None
    line = format_request_summary(summary)
    assert "422" not in line and "X-RateLimit" not in line
    assert line.startswith("Requests: 0, 429/5xx load: 0")


def test_accounts_that_opted_out_of_logged_out_views_are_not_read() -> None:
    label = [{"val": "!no-unauthenticated", "src": "did:plc:shy"}]
    shy = post_view("did:plc:shy", CUTOFF - timedelta(hours=2))
    shy["author"]["labels"] = label
    items = [{"post": shy}, {"post": post_view("did:plc:ok", CUTOFF - timedelta(hours=3))}]
    profile = {"did": "did:plc:shy", "handle": "shy.example", "labels": label}
    fake = Bsky(generators={FEED_URI: items}, profiles={"did:plc:shy": profile})
    source = make_source(fake)
    pool = list(source.discover(SeedSet("bluesky", (FEED_URI,)), 5))
    assert [a.user_id for a in pool] == ["did:plc:ok"]
    with pytest.raises(AccountUnavailable) as info:
        source.hydrate(account("did:plc:shy"))
    assert info.value.status == "opted_out"
    assert "getAuthorFeed" not in fake.methods()


# -- review fixes ------------------------------------------------------------------------

LABEL = [{"val": "!no-unauthenticated", "src": "did:plc:x"}]


def test_appview_option_is_refused_with_the_reason() -> None:
    with pytest.raises(ValueError, match="does not point them at another AppView"):
        make_source(Bsky(), appview_url="https://appview.example")


def test_logged_out_discovery_sends_no_search_request() -> None:
    """Without a login the query seeds are dropped before discovery starts, so no
    searchPosts request reaches the public host that would refuse it."""
    items = [{"post": post_view("did:plc:ok", CUTOFF - timedelta(hours=3))}]
    fake = Bsky(posts=search_world(), generators={FEED_URI: items})
    source = make_source(fake, seed_feeds=FEED_URI)
    pool = list(source.discover(SeedSet("bluesky", ("#topic", "#other")), 5))
    assert [a.user_id for a in pool] == ["did:plc:ok"]
    assert "searchPosts" not in fake.methods()
    assert any("2 Bluesky search seeds were skipped" in w for w in source.drain_warnings())
    assert {r.url.host for r in fake.requests} == {PUBLIC_HOST}
    assert source.appview == "https://public.api.bsky.app"


def test_hydrate_withholds_an_opted_out_account() -> None:
    did = "did:plc:shy"
    fake = Bsky(profiles={did: {"did": did, "handle": "shy.example", "labels": LABEL}})
    acct = Account("bluesky", did, "shy.example", meta={"community": "#topic"})
    with pytest.raises(AccountUnavailable) as info:
        make_source(fake).hydrate(acct)
    assert info.value.status == "opted_out"
    assert "shy" not in str(info.value)
    assert acct.handle == did and acct.meta == {"opted_out": True}


@pytest.mark.parametrize("repost", [False, True])
def test_walk_stops_when_own_posts_show_the_opt_out_label(repost: bool) -> None:
    did = "did:plc:shy"
    items = busy_feed(did, 300)
    if repost:
        other = post_view("did:plc:bob", CUTOFF - timedelta(days=3))
        item = repost_item(did, other, NOW - timedelta(minutes=5))
        item["reason"]["by"]["labels"] = LABEL
        items.insert(0, item)
    else:
        items[0]["post"]["author"]["labels"] = LABEL
    fake = Bsky(feeds={did: items})
    source = make_source(fake)
    acct = Account(
        "bluesky", did, "shy.example", CUTOFF - timedelta(days=200), {"community": "#topic"}
    )
    with pytest.raises(AccountUnavailable) as info:
        source.fetch_activity(acct, CUTOFF - timedelta(days=30), NOW)
    assert info.value.status == "opted_out"
    assert info.value.partial is None
    assert acct.handle == did and acct.meta == {"opted_out": True}
    assert acct.created_at is None
    assert fake.methods() == ["getAuthorFeed"]  # Stopped on the first page.
    precheck = Account("bluesky", did, "shy.example")
    with pytest.raises(AccountUnavailable):
        source.newest_activity(precheck, "any", CUTOFF - timedelta(days=2), NOW)


def test_backdated_posts_are_timed_by_indexing_and_bound_the_walk() -> None:
    did = "did:plc:alice"
    since = CUTOFF - timedelta(days=30)
    # Indexed after the cutoff but backdated to before it: listed at its createdAt.
    inside = post_view(
        did, CUTOFF + timedelta(days=1), created=CUTOFF - timedelta(days=5), text="in", n=1
    )
    # Backdated to before the window: listed past the point where paging stops.
    outside = post_view(
        did, CUTOFF + timedelta(days=2), created=since - timedelta(days=3), text="out", n=2
    )
    plain = [
        {"post": post_view(did, CUTOFF - timedelta(days=d), text=f"p{d}", n=10 + d)}
        for d in (1, 10, 20, 29, 31, 40, 50)
    ]
    fake = Bsky(feeds={did: [{"post": inside}, {"post": outside}, *plain]})
    source = make_source(fake, page_size="3")
    acts = source.fetch_activity(account(did), since, NOW)
    stamps = {a.ts for a in acts}
    assert CUTOFF + timedelta(days=1) in stamps  # Timed by indexedAt, not createdAt.
    assert CUTOFF - timedelta(days=5) not in stamps
    assert CUTOFF + timedelta(days=2) not in stamps  # The documented miss.
    pages = fake.methods().count("getAuthorFeed")
    # Page 2 holds the first sortAt before ``since`` (day 31), so the walk stops there.
    assert pages == 2

    # The pre-check stops at the first in-window activity on the first page.
    fake = Bsky(feeds={did: [{"post": inside}, {"post": outside}, *plain]})
    found = make_source(fake, page_size="3").newest_activity(
        account(did), "any", CUTOFF - timedelta(days=2), NOW
    )
    assert found.newest == CUTOFF - timedelta(days=1)
    assert fake.methods() == ["getAuthorFeed"]


def test_concurrent_expired_token_is_refreshed_exactly_once() -> None:
    import threading

    fake = Bsky(posts=search_world())
    source = make_source(fake, credentials=LOGIN, concurrency=3)
    source._access_token()  # Log in first: access-1.
    assert fake.access == "access-1"
    fake.access = "expired"  # Every request with access-1 now gets 401.
    barrier = threading.Barrier(4, timeout=5)
    lock = threading.Lock()
    refreshes = {"n": 0}

    def hook(request: httpx.Request) -> httpx.Response | None:
        method = request.url.path.rsplit(".", 1)[-1]
        if method == "searchPosts" and request.headers["authorization"] == "Bearer access-1":
            barrier.wait()  # All four fail with the same expired token.
            return xrpc_error(401, "ExpiredToken", "Token has expired")
        if method == "refreshSession":
            with lock:
                refreshes["n"] += 1
            return httpx.Response(200, json={"accessJwt": "fresh", "refreshJwt": "r2"})
        if method == "searchPosts" and request.headers["authorization"] == "Bearer fresh":
            return httpx.Response(200, json={"posts": []})
        return None

    fake.hook = hook
    params = {"q": "x", "since": z(CUTOFF - timedelta(days=1)), "until": z(CUTOFF)}
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            source._call_authed("app.bsky.feed.searchPosts", params, phase="discovery")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert refreshes["n"] == 1
    assert fake.sessions == 1
    summary = source.request_summary()
    assert summary is not None and summary["session_refreshes"] == 1


def test_a_transient_seed_error_is_retried_before_the_seed_is_dropped() -> None:
    items = [
        {"post": post_view(f"did:plc:f{i}", CUTOFF - timedelta(hours=i + 1), n=i)} for i in range(5)
    ]
    failures = {"n": 2}

    def hook(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("getFeed") and failures["n"] > 0:
            failures["n"] -= 1
            return httpx.Response(502, text="bad gateway")
        return None

    sleeps: list[float] = []
    fake = Bsky(generators={FEED_URI: items}, hook=hook)
    source = make_source(fake, sleeps=sleeps)
    source.http.max_retries = 0  # One HTTP attempt per call: the seed retry is tested.
    pool = list(source.discover(SeedSet("bluesky", (FEED_URI,)), 5))
    assert len(pool) == 5
    assert not any("dropped" in w for w in source.drain_warnings())
    assert 5.0 in sleeps and 10.0 in sleeps

    failures["n"] = 99
    fake = Bsky(generators={FEED_URI: items}, hook=hook)
    source = make_source(fake, sleeps=[])
    source.http.max_retries = 0
    pool = list(source.discover(SeedSet("bluesky", (FEED_URI, LIST_URI)), 5))
    assert pool == []
    warnings = " ".join(source.drain_warnings())
    assert "after 3 failed attempts" in warnings
    assert fake.methods().count("getFeed") == 3


def test_a_seed_survives_failures_spread_over_many_pages() -> None:
    """The retry budget counts failures in a row: a page that works resets it, so two
    failures before each of several good pages never drop the seed."""
    items = [
        {"post": post_view(f"did:plc:f{i}", CUTOFF - timedelta(hours=i + 1), n=i)} for i in range(8)
    ]
    calls = {"n": 0}

    def hook(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("getFeed"):
            calls["n"] += 1
            if calls["n"] % 3:  # Two failures, then one good page, again and again.
                return httpx.Response(502, text="bad gateway")
        return None

    fake = Bsky(generators={FEED_URI: items}, hook=hook)
    source = make_source(fake, sleeps=[], page_size=2)
    source.http.max_retries = 0  # One HTTP attempt per call: the seed retry is tested.
    pool = list(source.discover(SeedSet("bluesky", (FEED_URI,)), 8))
    assert len(pool) == 8
    assert not any("dropped" in w for w in source.drain_warnings())
    assert fake.methods().count("getFeed") >= 12  # At least four pages, each after two 502s.


def test_search_slices_cover_the_final_day() -> None:
    fake = Bsky(posts=search_world())
    source = make_source(fake, credentials=LOGIN, page_size="1", seed=11, dry_pages="1000")
    list(source.discover(SeedSet("bluesky", ("#topic",)), 60))
    searches = [r for r in fake.requests if r.url.path.endswith("searchPosts")]
    untils = [parse(r.url.params["until"]) for r in searches]
    start = CUTOFF - timedelta(days=30)
    assert all(start < u <= CUTOFF for u in untils)
    # Draws past the cutoff are clamped to it, so the final day is not under-sampled.
    assert any(u == CUTOFF for u in untils)


def test_seeds_are_deduplicated_after_their_handles_are_resolved() -> None:
    items = [
        {"post": post_view(f"did:plc:x{i}", CUTOFF - timedelta(hours=i + 1), n=i)}
        for i in range(10)
    ]
    owner = {"did": "did:plc:feedowner", "handle": "owner.example"}
    fake = Bsky(generators={FEED_URI: items}, profiles={"owner.example": owner})
    source = make_source(fake)
    seeds = SeedSet("bluesky", (FEED_URI, "https://bsky.app/profile/owner.example/feed/topic"))
    pool = list(source.discover(seeds, 20))
    assert len(pool) == 10
    first_pages = [
        r for r in fake.requests if r.url.path.endswith("getFeed") and "cursor" not in r.url.params
    ]
    assert len(first_pages) == 1
    assert {a.meta["community"] for a in pool} == {FEED_URI}


def test_login_failure_mentions_pds_url() -> None:
    fake = Bsky(posts=search_world())
    creds = {HANDLE_ENV: "someone.thirdparty.example", PASSWORD_ENV: "zzzz-zzzz-zzzz-zzzz"}
    with pytest.raises(SourceAuthError, match="pds_url"):
        list(make_source(fake, credentials=creds).discover(SeedSet("bluesky", ("#t",)), 5))


def test_reposts_after_the_cutoff_are_counted_but_not_activity() -> None:
    did = "did:plc:alice"
    other = post_view("did:plc:bob", CUTOFF - timedelta(days=3))
    items = [
        repost_item(did, other, CUTOFF + timedelta(days=1)),
        repost_item(did, other, CUTOFF + timedelta(days=2)),
        repost_item(did, other, CUTOFF - timedelta(days=1)),
        {"post": post_view(did, CUTOFF - timedelta(days=2))},
    ]
    acct = account(did)
    acts = make_source(Bsky(feeds={did: items})).fetch_activity(
        acct, CUTOFF - timedelta(days=30), NOW
    )
    assert [a.kind for a in acts] == ["post"]
    assert acct.meta["reposts_after_cutoff"] == 2


def test_deactivated_clears_when_the_profile_reads_again() -> None:
    did = "did:plc:back"
    fake = Bsky(profiles={did: xrpc_error(400, "AccountDeactivated", "Account is deactivated")})
    source = make_source(fake)
    acct = account(did)
    with pytest.raises(AccountUnavailable):
        source.hydrate(acct)
    assert acct.meta["deactivated"] is True
    fake.profiles[did] = {"did": did, "handle": "back.example", "createdAt": z(CUTOFF)}
    source.hydrate(acct)
    assert "deactivated" not in acct.meta


def output_world() -> Bsky:
    """``world()`` plus: acct0 (silent) reposts after the cutoff, acct8 opted out,
    acct9 is deactivated."""
    fake = world()
    other = post_view("did:plc:bob", CUTOFF - timedelta(days=3))
    fake.feeds["did:plc:acct0"].append(
        repost_item("did:plc:acct0", other, NOW - timedelta(hours=5))
    )
    fake.profiles["did:plc:acct8"]["labels"] = LABEL
    fake.profiles["did:plc:acct9"] = xrpc_error(400, "AccountDeactivated", "Account is deactivated")
    del fake.feeds["did:plc:acct9"]
    return fake


def test_cli_outputs_never_list_opted_out_accounts_and_show_new_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = output_world()
    patch_client(monkeypatch, fake)
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    db = tmp_path / "c.db"
    out_json, out_csv = tmp_path / "out.json", tmp_path / "out.csv"
    base = [*SCAN, "--query", FEED_URI, "--db", str(db), "--include-unavailable"]
    result = runner.invoke(app, [*base, "--out", str(out_json)])
    assert result.exit_code == 0, result.output
    payload = json.loads(out_json.read_text())
    rows = {c["user_id"]: c for c in payload["candidates"]}
    assert "did:plc:acct8" not in rows
    assert payload["summary"]["opted_out"] == 1
    assert rows["did:plc:acct9"]["deactivated"] is True
    assert rows["did:plc:acct9"]["status"] == "deleted"
    assert payload["summary"]["deactivated"] == 1
    assert rows["did:plc:acct0"]["reposts_after_cutoff"] == 1
    assert rows["did:plc:acct1"]["reposts_after_cutoff"] == 0
    assert payload["summary"]["cliff_reposted_after_cutoff"] == 1
    assert any("REPOSTS: 1" in n for n in payload["account_notices"])
    for text in (out_json.read_text(), result.output):
        assert "acct8" not in text

    result = runner.invoke(app, [*base, "--out", str(out_csv)])
    assert result.exit_code == 0, result.output
    text = out_csv.read_text()
    assert "acct8" not in text and "OPTED OUT: 1" in text
    header = next(line for line in text.splitlines() if not line.startswith("#"))
    # Bluesky's own columns come right after the common ones, then the method's.
    assert ",data_end,reposts_after_cutoff,deactivated," in header
    assert header.endswith(",independent_signals")
    run_id = json.loads(out_json.read_text())["run"]["run_id"] + 1
    reported = tmp_path / "report.csv"
    rep = runner.invoke(
        app,
        ["report", "--run-id", str(run_id), "--db", str(db), "--out", str(reported),
         "--include-unavailable"],
    )  # fmt: skip
    assert rep.exit_code == 0, rep.output
    rows_of = lambda t: [ln for ln in t.splitlines() if not ln.startswith("#")]  # noqa: E731
    assert rows_of(reported.read_text()) == rows_of(text)  # report reproduces the columns.
    assert "deactivated" in rep.output and "Opted out, counted only" in rep.output

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT handle, status, meta_json FROM accounts WHERE user_id = 'did:plc:acct8'"
        ).fetchone()
        cached = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE user_id = 'did:plc:acct8'"
        ).fetchone()[0]
        samples = [r[0] for r in conn.execute("SELECT account_json FROM run_sample")]
    assert row == ("did:plc:acct8", "opted_out", '{"opted_out": true}')
    assert cached == 0
    assert all("acct8.bsky.social" not in s for s in samples)


def test_cli_resumed_account_that_opted_out_since_is_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = world()
    patch_client(monkeypatch, fake)
    db = tmp_path / "c.db"
    out = tmp_path / "out.json"
    first = runner.invoke(app, [*SCAN, "--query", FEED_URI, "--db", str(db), "--out", str(out)])
    assert first.exit_code == 0, first.output
    assert "did:plc:acct2" in out.read_text()
    run_id = json.loads(out.read_text())["run"]["run_id"]
    # acct2 adds the label after the first run. Its cached profile is not read again,
    # so the author feed has to notice it.
    for item in fake.feeds["did:plc:acct2"]:
        item["post"]["author"]["labels"] = LABEL
    fake.requests.clear()
    again = runner.invoke(
        app,
        [
            "scan", "--platform", "bluesky", "--from-run", str(run_id), "--refresh",
            "--now", "2026-10-14T00:00:00Z", "--db", str(db), "--out", str(out),
            "--include-unavailable",
        ],
    )  # fmt: skip
    assert again.exit_code == 0, again.output
    assert "getProfile" not in {r.url.path.rsplit(".", 1)[-1] for r in fake.requests}
    payload = json.loads(out.read_text())
    assert payload["summary"]["opted_out"] == 1
    assert "acct2" not in out.read_text() and "acct2" not in again.output
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT handle, status FROM accounts WHERE user_id = 'did:plc:acct2'"
        ).fetchone()
        cached = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE user_id = 'did:plc:acct2'"
        ).fetchone()[0]
    assert row == ("did:plc:acct2", "opted_out")
    assert cached == 0


def test_a_cached_opt_out_is_read_again_so_removing_the_label_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery's hydration (``--new-accounts-days``) reads an account cached as
    ``opted_out`` again instead of trusting the cache: once the label is gone, the
    account is sampled with its handle."""
    fake = world()
    fake.profiles["did:plc:acct8"]["labels"] = LABEL
    patch_client(monkeypatch, fake)
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    db = tmp_path / "c.db"
    out = tmp_path / "out.json"
    args = [
        *SCAN, "--query", FEED_URI, "--db", str(db), "--out", str(out),
        "--new-accounts-days", "365",
    ]  # fmt: skip
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert json.loads(out.read_text())["summary"]["dropped_opted_out"] == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT handle, status FROM accounts WHERE user_id = 'did:plc:acct8'"
        ).fetchone()
        # As a row stored before opted-out rows lost their creation time would be:
        # the status alone must send the account back to the profile.
        conn.execute(
            "UPDATE accounts SET created_at = '2026-01-01T00:00:00+00:00'"
            " WHERE user_id = 'did:plc:acct8'"
        )
    assert row == ("did:plc:acct8", "opted_out")

    del fake.profiles["did:plc:acct8"]["labels"]
    fake.requests.clear()
    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.output
    profiles = [r.url.params["actor"] for r in fake.requests if r.url.path.endswith("getProfile")]
    assert profiles == ["did:plc:acct8"]  # Only the opted-out one; the rest are cached.
    payload = json.loads(out.read_text())
    assert payload["summary"]["dropped_opted_out"] == 0
    assert payload["summary"]["sampled"] == 12
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT handle, status FROM accounts WHERE user_id = 'did:plc:acct8'"
        ).fetchone()
    assert row == ("acct8.bsky.social", "active")


def test_discovery_counts_opted_out_accounts_on_their_own() -> None:
    from bot_farm_silence_tracker.sampling import sample_accounts

    fake = world()
    fake.profiles["did:plc:acct8"]["labels"] = LABEL
    source = make_source(fake)
    sample = sample_accounts(
        source,
        SeedSet("bluesky", (FEED_URI,)),
        n=12,
        new_accounts_days=365,
        pool_factor=1,
        seed=3,
        now=NOW,
        hydrate=source.hydrate,
    )
    assert sample.dropped_opted_out == 1
    assert sample.dropped_unknown_age == 0
    assert "did:plc:acct8" not in {a.user_id for a in sample.accounts}


def test_an_opt_out_found_at_fetch_is_fetched_in_full_once_the_label_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An account found opted out while its activity is fetched keeps no window, and
    once the label is gone, discovery's re-read sends it back to a full fetch inside
    the cache TTL instead of scoring it on an empty cached window."""
    fake = world()
    fake.profiles["did:plc:acct8"]["labels"] = LABEL
    patch_client(monkeypatch, fake)
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    db = tmp_path / "c.db"
    out = tmp_path / "out.json"
    args = [*SCAN, "--query", FEED_URI, "--db", str(db), "--out", str(out)]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert json.loads(out.read_text())["summary"]["opted_out"] == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, activity_since, activity_until, activity_fetched_at"
            " FROM accounts WHERE user_id = 'did:plc:acct8'"
        ).fetchone()
    assert row == ("opted_out", None, None, None)
    eligible_before = json.loads(out.read_text())["summary"]["eligible"]

    del fake.profiles["did:plc:acct8"]["labels"]
    fake.requests.clear()
    second = runner.invoke(app, [*args, "--new-accounts-days", "365"])
    assert second.exit_code == 0, second.output
    feeds = [r.url.params["actor"] for r in fake.requests if r.url.path.endswith("getAuthorFeed")]
    assert "did:plc:acct8" in feeds
    summary = json.loads(out.read_text())["summary"]
    assert summary["opted_out"] == 0
    assert summary["eligible"] == eligible_before + 1
    with sqlite3.connect(db) as conn:
        status, since = conn.execute(
            "SELECT status, activity_since FROM accounts WHERE user_id = 'did:plc:acct8'"
        ).fetchone()
        cached = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE user_id = 'did:plc:acct8'"
        ).fetchone()[0]
    assert status == "active" and since is not None
    assert cached > 0  # The posts in the window are cached and counted.


def test_an_opted_out_handle_is_not_left_in_the_database_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``secure_delete`` overwrites the deleted rows, so the handle is not readable
    from free pages of the file once the account opts out."""
    fake = world()
    patch_client(monkeypatch, fake)
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    db = tmp_path / "c.db"
    out = tmp_path / "out.json"
    args = [*SCAN, "--query", FEED_URI, "--db", str(db), "--out", str(out)]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    fake.profiles["did:plc:acct2"]["labels"] = LABEL
    for item in fake.feeds["did:plc:acct2"]:
        item["post"]["author"]["labels"] = LABEL
    second = runner.invoke(app, [*args, "--cache-ttl-hours", "0"])
    assert second.exit_code == 0, second.output
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    files = [p for p in tmp_path.iterdir() if p.name.startswith("c.db")]
    assert files
    for path in files:
        assert b"acct2.bsky" not in path.read_bytes(), path.name


# -- discovery depth, causes and list members ---------------------------------------------


def feed_items(
    prefix: str, count: int, first: datetime, *, step: timedelta = timedelta(hours=1)
) -> list[dict[str, Any]]:
    """``count`` posts by distinct authors, newest at ``first``, one every ``step``."""
    return [
        {"post": post_view(f"did:plc:{prefix}{i}", first - step * i, n=i)} for i in range(count)
    ]


def test_a_feed_entirely_after_the_cutoff_keeps_paging_within_the_cap() -> None:
    # 120 posts by the same few authors after the cutoff (12 pages of 10), then the
    # window. The old rule counted those pages as dry and stopped after 5.
    after = [
        {"post": post_view(f"did:plc:late{i % 3}", NOW - timedelta(minutes=10 * i), n=i)}
        for i in range(120)
    ]
    inside = feed_items("w", 30, CUTOFF - timedelta(hours=1))
    fake = Bsky(generators={FEED_URI: after + inside})
    source = make_source(fake, seed_feeds=FEED_URI, page_size="10", max_pages_ahead="20")
    pool = list(source.discover(SeedSet("bluesky", (FEED_URI,)), 20))
    assert len(pool) == 20
    assert all(a.user_id.startswith("did:plc:w") for a in pool)
    # With a lower cap the seed gives up before the window, and says why.
    capped = make_source(
        Bsky(generators={FEED_URI: after + inside}),
        seed_feeds=FEED_URI,
        page_size="10",
        max_pages_ahead="4",
    )
    assert list(capped.discover(SeedSet("bluesky", (FEED_URI,)), 20)) == []
    warnings = " ".join(capped.drain_warnings())
    assert "page limit reached" in warnings and "max_pages_ahead" in warnings
    requests = [r for r in capped._log if r["endpoint"] == "getFeed"]
    assert len(requests) == 4


def test_a_single_stray_old_item_does_not_end_the_seed() -> None:
    items = feed_items("a", 40, CUTOFF - timedelta(hours=1))
    # A pinned or boosted post from long before the window, at the top of page 1.
    items.insert(0, {"post": post_view("did:plc:old", CUTOFF - timedelta(days=400))})
    fake = Bsky(generators={FEED_URI: items})
    source = make_source(fake, page_size="10")
    pool = list(source.discover(SeedSet("bluesky", (FEED_URI,)), 40))
    assert len(pool) == 40
    assert fake.methods().count("getFeed") >= 4
    # A page that is mostly older than the start does end it.
    deep = feed_items("b", 10, CUTOFF - timedelta(hours=1)) + feed_items(
        "c", 30, CUTOFF - timedelta(days=40)
    )
    fake = Bsky(generators={FEED_URI: deep})
    source = make_source(fake, page_size="10")
    pool = list(source.discover(SeedSet("bluesky", (FEED_URI,)), 40))
    assert len(pool) == 10
    assert fake.methods().count("getFeed") == 2
    assert not any("did not reach back" in w for w in source.drain_warnings())


def test_seed_warnings_name_the_real_cause() -> None:
    short = feed_items("s", 15, NOW - timedelta(hours=1))  # Only recent posts.
    gap = feed_items("g", 5, NOW - timedelta(hours=1)) + feed_items(
        "o", 10, CUTOFF - timedelta(days=60)
    )
    deep = feed_items("d", 100, NOW - timedelta(minutes=1), step=timedelta(minutes=1))
    feed_b = "at://did:plc:feedowner/app.bsky.feed.generator/gap"
    feed_c = "at://did:plc:feedowner/app.bsky.feed.generator/deep"
    fake = Bsky(generators={FEED_URI: short, feed_b: gap, feed_c: deep})
    source = make_source(fake, page_size="10", max_pages_ahead="3")
    assert list(source.discover(SeedSet("bluesky", (FEED_URI, feed_b, feed_c)), 10)) == []
    warnings = source.drain_warnings()
    by_seed = {
        uri: next(w for w in warnings if repr(uri) in w) for uri in (FEED_URI, feed_b, feed_c)
    }
    assert "feed history ends before the window" in by_seed[FEED_URI]
    assert "only keeps recent posts" in by_seed[FEED_URI]
    assert "no items in window" in by_seed[feed_b]
    assert "page limit reached" in by_seed[feed_c]
    # Read four days after the cutoff: an up-front warning suggests list seeds.
    assert any("about a day of posts" in w and "list seeds" in w for w in warnings)


def test_no_up_front_feed_warning_when_the_cutoff_is_recent() -> None:
    fake = Bsky(generators={FEED_URI: feed_items("a", 5, CUTOFF - timedelta(hours=1))})
    http = make_source(fake).config.http
    config = SourceConfig(
        http=http,
        credentials={},
        now=CUTOFF + timedelta(hours=12),
        cutoff=CUTOFF,
        seed=1,
        options={},
        baseline_start=CUTOFF - timedelta(days=30),
    )
    source = BlueskySource(config)
    list(source.discover(SeedSet("bluesky", (FEED_URI,)), 5))
    assert not any("about a day of posts" in w for w in source.drain_warnings())


CURATE = "app.bsky.graph.defs#curatelist"
MODLIST = "app.bsky.graph.defs#modlist"
LIST_B = "at://did:plc:listowner/app.bsky.graph.list/second"
LIST_MOD = "at://did:plc:listowner/app.bsky.graph.list/blocks"


def member(i: int, *, opted_out: bool = False) -> dict[str, Any]:
    view: dict[str, Any] = {"did": f"did:plc:m{i}", "handle": f"m{i}.bsky.social"}
    if opted_out:
        view["labels"] = [{"val": "!no-unauthenticated", "src": f"did:plc:m{i}"}]
    return view


def test_list_members_are_paged_deduplicated_and_shuffled() -> None:
    first = [member(i) for i in range(250)]
    second = [member(i) for i in range(200, 320)] + [member(999, opted_out=True)]
    fake = Bsky(lists={LIST_URI: (CURATE, first), LIST_B: (CURATE, second)})
    source = make_source(fake, list_mode="members")
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI, LIST_B)), 1000))
    ids = [a.user_id for a in pool]
    assert len(ids) == len(set(ids)) == 320
    assert "did:plc:m999" not in ids
    calls = [r for r in fake.requests if r.url.path.endswith("getList")]
    assert [r.url.params["list"] for r in calls] == [LIST_URI] * 3 + [LIST_B] * 2
    assert all(r.url.host == PUBLIC_HOST for r in calls)
    communities = {a.user_id: a.meta["community"] for a in pool}
    assert communities["did:plc:m210"] == LIST_URI and communities["did:plc:m300"] == LIST_B
    # Shuffled, not in list order; the same --seed draws the same pool, whatever the
    # order the lists are given in.
    assert ids[:20] != [f"did:plc:m{i}" for i in range(20)]
    again = make_source(
        Bsky(lists={LIST_URI: (CURATE, first), LIST_B: (CURATE, second)}), list_mode="members"
    )
    swapped = [a.user_id for a in again.discover(SeedSet("bluesky", (LIST_B, LIST_URI)), 1000)]
    assert swapped == ids
    warnings = " ".join(source.drain_warnings())
    assert "320 distinct members of 2 lists" in warnings
    assert "not the accounts that posted in the baseline window" in warnings


def test_list_member_draw_is_uniform_over_the_union() -> None:
    # Members 10..19 are on both lists; a per-list draw would favour them.
    first = [member(i) for i in range(20)]
    second = [member(i) for i in range(10, 30)]
    lists = {LIST_URI: (CURATE, first), LIST_B: (CURATE, second)}
    counts: dict[str, int] = dict.fromkeys((f"did:plc:m{i}" for i in range(30)), 0)
    runs = 600
    for seed in range(runs):
        source = make_source(Bsky(lists=lists), seed=seed, list_mode="members")
        for a in source.discover(SeedSet("bluesky", (LIST_URI, LIST_B)), 10):
            counts[a.user_id] += 1
    expected = runs * 10 / 30
    shared = sum(counts[f"did:plc:m{i}"] for i in range(10, 20)) / 10
    single = sum(counts[f"did:plc:m{i}"] for i in (*range(10), *range(20, 30))) / 20
    assert abs(shared - expected) < 0.1 * expected
    assert abs(single - expected) < 0.1 * expected
    assert all(0.6 * expected < c < 1.4 * expected for c in counts.values())


def test_moderation_lists_are_refused_and_curated_lists_kept() -> None:
    fake = Bsky(
        lists={LIST_URI: (CURATE, [member(1), member(2)]), LIST_MOD: (MODLIST, [member(3)])}
    )
    source = make_source(fake, list_mode="members")
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI, LIST_MOD)), 10))
    assert {a.user_id for a in pool} == {"did:plc:m1", "did:plc:m2"}
    warnings = " ".join(source.drain_warnings())
    assert repr(LIST_MOD) in warnings and "moderation" in warnings
    # Only a moderation list: nothing usable is left.
    only_mod = make_source(Bsky(lists={LIST_MOD: (MODLIST, [member(3)])}), list_mode="members")
    with pytest.raises(SourceError, match="no list could be read"):
        list(only_mod.discover(SeedSet("bluesky", (LIST_MOD,)), 10))


def test_list_member_pages_cap_is_warned() -> None:
    fake = Bsky(lists={LIST_URI: (CURATE, [member(i) for i in range(250)])})
    source = make_source(fake, list_member_pages="2", list_mode="members")
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI,)), 1000))
    assert len(pool) == 200
    assert any("list_member_pages" in w for w in source.drain_warnings())


def test_list_and_feed_seeds_mix_and_queries_are_dropped_without_login() -> None:
    fake = Bsky(
        lists={LIST_URI: (CURATE, [member(i) for i in range(30)])},
        generators={FEED_URI: feed_items("f", 30, CUTOFF - timedelta(hours=1))},
    )
    source = make_source(fake, page_size="10", list_mode="members")
    pool = list(source.discover(SeedSet("bluesky", ("#topic", LIST_URI, FEED_URI)), 20))
    sources = [a.meta["community"] for a in pool]
    assert sources.count(FEED_URI) == 10 and sources.count(LIST_URI) == 10
    assert "searchPosts" not in fake.methods()


def test_bad_list_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="list_mode"):
        make_source(Bsky(), list_mode="posts")


def test_default_seeds_carry_public_curated_lists() -> None:
    items = load_seeds("bluesky").items
    lists = [parse_seed(i) for i in items if "app.bsky.graph.list" in i]
    assert len(lists) >= 5 and all(s.kind == "list" for s in lists)
    assert "#Ukraine" in items


def test_cli_hide_handles_shows_row_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    shown = {}
    for hide in (False, True):
        patch_client(monkeypatch, world())
        args = [*SCAN, "--query", FEED_URI, "--db", str(tmp_path / f"{hide}.db")]
        args += ["--out", str(tmp_path / f"{hide}.json")]
        result = runner.invoke(app, [*args, "--hide-handles"] if hide else args)
        assert result.exit_code == 0, result.output
        shown[hide] = result.output
    assert "acct" in shown[False] and "Top candidates" in shown[False]
    assert "Top candidates" in shown[True] and "#1" in shown[True]
    assert "acct" not in shown[True] and "bsky.social" not in shown[True]
    # Output files still name the accounts.
    payload = json.loads((tmp_path / "True.json").read_text())
    assert all(c["handle"].startswith("acct") for c in payload["candidates"])


# -- repliers mode (the default for list seeds) ---------------------------------------

THREAD = "app.bsky.feed.defs#threadViewPost"
#: Anchor post times: two after the cutoff, four in the window, one before the start.
ANCHOR_POST_TIMES = (
    NOW - timedelta(hours=1),
    CUTOFF + timedelta(hours=2),
    CUTOFF - timedelta(days=1),
    CUTOFF - timedelta(days=3),
    CUTOFF - timedelta(days=5),
    CUTOFF - timedelta(days=10),
    CUTOFF - timedelta(days=40),
)


def reply_view(did: str, when: datetime, *, n: int = 0, opted_out: bool = False) -> dict[str, Any]:
    post = post_view(did, when, reply=True, n=n)
    if opted_out:
        post["author"]["labels"] = [{"val": "!no-unauthenticated", "src": did}]
    return {"$type": THREAD, "post": post}


def replier_world(anchors: int = 6, *, list_uris: tuple[str, ...] = (LIST_URI,)) -> Bsky:
    """``anchors`` list members, each with posts around the window and replies to them.

    Every in-window anchor post gets three replies in the window by its own repliers
    (``did:plc:r<anchor>_<post>_<k>``), one by ``did:plc:regular`` (on every thread),
    one after the cutoff, one by another anchor and one by an opted-out account.
    Posts outside the window get replies by ``did:plc:late...``, which must never be
    read.
    """
    feeds: dict[str, list[dict[str, Any]]] = {}
    threads: dict[str, list[dict[str, Any]]] = {}
    for i in range(anchors):
        did = f"did:plc:m{i}"
        items = []
        for k, when in enumerate(ANCHOR_POST_TIMES):
            post = post_view(did, when, n=k)
            items.append({"post": post})
            inside = CUTOFF - timedelta(days=30) <= when < CUTOFF
            if not inside:
                threads[post["uri"]] = [reply_view(f"did:plc:late{i}_{k}", when, n=1)]
                continue
            reply_at = when + timedelta(minutes=30)
            threads[post["uri"]] = [
                *(reply_view(f"did:plc:r{i}_{k}_{j}", reply_at, n=j) for j in range(3)),
                reply_view("did:plc:regular", reply_at, n=7),
                reply_view(f"did:plc:after{i}_{k}", CUTOFF + timedelta(hours=1), n=8),
                reply_view(f"did:plc:m{(i + 1) % anchors}", reply_at, n=9),
                reply_view("did:plc:hidden", reply_at, n=10, opted_out=True),
                {"$type": "app.bsky.feed.defs#notFoundPost", "uri": "at://gone", "notFound": True},
            ]
        # The anchor's repost of someone else's post is not its own window post.
        items.append(
            repost_item(
                did,
                post_view("did:plc:other", CUTOFF - timedelta(days=2)),
                CUTOFF - timedelta(days=2),
            )
        )
        feeds[did] = items
    members = [member(i) for i in range(anchors)]
    lists = {uri: (CURATE, members) for uri in list_uris}
    return Bsky(feeds=feeds, threads=threads, lists=lists)


def thread_calls(fake: Bsky) -> list[httpx.Request]:
    return [r for r in fake.requests if r.url.path.endswith("getPostThread")]


def test_repliers_is_the_default_and_pools_window_repliers_only() -> None:
    fake = replier_world()
    source = make_source(fake)
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI,)), 1000))
    ids = [a.user_id for a in pool]
    assert ids and len(ids) == len(set(ids))  # De-duplicated by DID.
    assert set(ids) <= {
        f"did:plc:r{i}_{k}_{j}" for i in range(6) for k in range(7) for j in range(3)
    } | {"did:plc:regular"}
    assert "did:plc:regular" in ids  # Replied on every thread, pooled once.
    assert not any(i.startswith(("did:plc:m", "did:plc:late", "did:plc:after")) for i in ids)
    assert "did:plc:hidden" not in ids
    assert {a.meta["community"] for a in pool} == {"a list"}  # The anchor list's name.
    # Threads are read only for the anchors' posts in the window, one level deep.
    window_posts = {
        item["post"]["uri"]
        for items in fake.feeds.values()
        for item in items
        if "reason" not in item
        and CUTOFF - timedelta(days=30) <= parse(item["post"]["indexedAt"]) < CUTOFF
    }
    calls = thread_calls(fake)
    assert calls and {r.url.params["uri"] for r in calls} <= window_posts
    assert all(r.url.params["depth"] == "1" and r.url.params["parentHeight"] == "0" for r in calls)
    # At most anchor_posts_per_account (3) threads per anchor, of its 4 window posts.
    per_anchor = Counter(r.url.params["uri"].split("/")[2] for r in calls)
    assert set(per_anchor.values()) == {3}
    feeds = [r for r in fake.requests if r.url.path.endswith("getAuthorFeed")]
    assert all(r.url.params["filter"] == "posts_no_replies" for r in feeds)
    assert {r.url.host for r in fake.requests} == {PUBLIC_HOST}
    warnings = " ".join(source.drain_warnings())
    assert "list_mode=repliers" in warnings and "busy threads contribute more" in warnings
    assert "never discovered" in warnings and "near 0 by design" in warnings
    assert "Direct replies per thread" in warnings and "6 of 6 anchors read" in warnings
    assert "mixes" not in warnings


def test_repliers_anchor_sampling_bounds_and_determinism() -> None:
    fake = replier_world(anchors=10)
    source = make_source(
        fake, anchor_accounts="2", anchor_posts_per_account="1", thread_reply_limit="2"
    )
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI,)), 1000))
    actors = {r.url.params["actor"] for r in fake.requests if r.url.path.endswith("getAuthorFeed")}
    assert len(actors) == 2
    calls = thread_calls(fake)
    assert len(calls) == 2  # One window post per anchor.
    assert len(pool) <= 2 * 2  # At most thread_reply_limit authors per thread.
    # The same --seed draws the same anchors and pool, whatever order the lists are in.
    lists = (LIST_URI, "at://did:plc:listowner/app.bsky.graph.list/second")
    runs = []
    for order in (lists, lists[::-1], lists):
        again = make_source(replier_world(anchors=10, list_uris=lists), seed=5, anchor_accounts="3")
        runs.append([a.user_id for a in again.discover(SeedSet("bluesky", order), 1000)])
    assert runs[0] == runs[1] == runs[2]
    other = make_source(replier_world(anchors=10, list_uris=lists), seed=6, anchor_accounts="3")
    assert [a.user_id for a in other.discover(SeedSet("bluesky", lists), 1000)] != runs[0]


def test_repliers_stop_at_half_the_request_budget() -> None:
    fake = replier_world(anchors=30)
    source = make_source(fake, max_requests=40)
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI,)), 1000))
    assert pool
    # One getList, then at most half of the 39 requests left: 19.
    assert len(fake.requests) <= 1 + 19 + 1
    assert source.http.requests_remaining >= 19
    report = next(w for w in source.drain_warnings() if "half of the request budget" in w)
    assert "stopped reading anchors after reading" in report and "of the 30 drawn" in report


def test_repliers_skip_unreadable_anchors_threads_and_posts_without_replies() -> None:
    fake = replier_world(anchors=3)
    del fake.feeds["did:plc:m0"]  # Its feed answers "Profile not found".
    for item in fake.feeds["did:plc:m1"]:
        if "reason" not in item:
            item["post"]["replyCount"] = 0
    broken = next(
        u for u in fake.threads if "/did:plc:m2/" in u and "late" not in str(fake.threads[u])
    )
    del fake.threads[broken]
    source = make_source(fake, anchor_posts_per_account="4")
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI,)), 1000))
    assert pool and all("did:plc:r2_" in a.user_id or a.user_id == "did:plc:regular" for a in pool)
    assert not any("/did:plc:m1/" in r.url.params["uri"] for r in thread_calls(fake))
    warnings = " ".join(source.drain_warnings())
    assert "2 of 3 anchors read (1 failed" in warnings and "(1 failed)" in warnings
    assert "4 without replies skipped" in warnings


def test_repliers_read_every_anchor_before_the_pool_limit_and_report_once() -> None:
    fake = replier_world(anchors=20)
    source = make_source(fake)
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI,)), 5))
    assert len(pool) == 5
    actors = {r.url.params["actor"] for r in fake.requests if r.url.path.endswith("getAuthorFeed")}
    # A small pool limit does not stop discovery at the first anchor: the whole frame
    # is read, so the limit cuts a random subset of it.
    assert len(actors) == 20
    reports = [w for w in source.drain_warnings() if "repliers discovery" in w]
    assert len(reports) == 1 and "20 of 20 anchors read" in reports[0]
    assert "60 threads contributed the 181 distinct repliers found" in reports[0]
    assert "the largest gave 4 of them (2% of the frame)" in reports[0]


def test_repliers_rate_limit_midway_hands_out_the_repliers_found_then_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = replier_world(anchors=6)
    source = make_source(fake)
    real = source._direct_replies
    calls = [0]

    def limited(uri: str) -> list[tuple[str, str, datetime, bool]]:
        calls[0] += 1
        if calls[0] > 4:
            raise RateLimited(30.0, "rate limited")
        return real(uri)

    monkeypatch.setattr(source, "_direct_replies", limited)
    pool: list[Account] = []
    with pytest.raises(RateLimited):
        for account in source.discover(SeedSet("bluesky", (LIST_URI,)), 10_000):
            pool.append(account)
    assert len(pool) == 13  # Four threads of three repliers, plus the regular one.
    report = next(w for w in source.drain_warnings() if "repliers discovery" in w)
    assert "Discovery stopped after reading 2 of the 6 drawn anchors" in report


def _anchor_of(did: str) -> int:
    return int(did.removeprefix("did:plc:r").split("_")[0])


def test_repliers_pool_does_not_depend_on_where_the_pool_limit_cuts() -> None:
    # With a fixed --seed, a small pool is the head of the same shuffled frame that a
    # large pool hands out in full.
    full = [
        a.user_id
        for a in make_source(replier_world(anchors=12), seed=3).discover(
            SeedSet("bluesky", (LIST_URI,)), 10_000
        )
    ]
    small = [
        a.user_id
        for a in make_source(replier_world(anchors=12), seed=3).discover(
            SeedSet("bluesky", (LIST_URI,)), 8
        )
    ]
    assert small == full[:8]
    # Over many draws, a pool of 8 spreads over the anchors instead of filling up
    # from the first anchor read: each anchor's share stays near 1/12.
    counts: Counter[int] = Counter()
    for seed in range(60):
        source = make_source(replier_world(anchors=12), seed=seed)
        ids = [a.user_id for a in source.discover(SeedSet("bluesky", (LIST_URI,)), 8)]
        anchors = [_anchor_of(i) for i in ids if i != "did:plc:regular"]
        # One anchor's threads alone hold 9 repliers, enough to fill the pool; a
        # pool cut from the whole frame mixes several anchors in every run.
        assert len(set(anchors)) >= 3
        counts.update(anchors)
    total = sum(counts.values())
    assert set(counts) == set(range(12))
    assert max(counts.values()) / total < 0.2


def test_repliers_cap_a_busy_threads_share_of_the_pool() -> None:
    fake = replier_world(anchors=4)
    busy = next(
        u for u in fake.threads if "/did:plc:m0/" in u and "late" not in str(fake.threads[u])
    )
    reply_at = CUTOFF - timedelta(days=2)
    fake.threads[busy] = [reply_view(f"did:plc:busy{j}", reply_at, n=j) for j in range(200)]
    source = make_source(fake, anchor_posts_per_account="4")
    pool = [a.user_id for a in source.discover(SeedSet("bluesky", (LIST_URI,)), 10_000)]
    busy_ids = [i for i in pool if i.startswith("did:plc:busy")]
    assert len(busy_ids) == 10  # thread_reply_limit defaults to 10.
    assert len(busy_ids) / len(pool) < 0.25
    report = next(w for w in source.drain_warnings() if "repliers discovery" in w)
    assert "the largest gave 10 of them" in report


# -- sampling frames: selection and mixing warnings -------------------------------------


def test_logged_in_with_bundled_seeds_uses_search_only() -> None:
    fake = Bsky(posts=search_world(), lists={LIST_URI: (CURATE, [member(1)])})
    source = make_source(fake, credentials=LOGIN)
    seeds = SeedSet("bluesky", ("#topic", LIST_URI), bundled=True)
    source.note_seeds(seeds)
    frame = source.sampling_frame() or ""
    assert frame.startswith("Sampling frame: window posters") and "mixed" not in frame
    pool = list(source.discover(seeds, 10))
    assert pool and "getList" not in fake.methods()
    warnings = " ".join(source.drain_warnings())
    assert "search only" in warnings and "list_mode=repliers" in warnings
    assert "mixes" not in warnings


def test_asking_for_lists_next_to_search_warns_that_frames_mix() -> None:
    fake = replier_world(anchors=3)
    fake.posts = search_world()
    source = make_source(fake, credentials=LOGIN, list_mode="repliers")
    seeds = SeedSet("bluesky", ("#topic", LIST_URI), bundled=True)
    source.note_seeds(seeds)
    assert (source.sampling_frame() or "").startswith("The sampling frames are mixed")
    list(source.discover(seeds, 20))
    assert "getList" in fake.methods() and "searchPosts" in fake.methods()
    mixed = [w for w in source.drain_warnings() if "mixes" in w]
    assert len(mixed) == 1
    assert "2 sampling frames" in mixed[0] and "window posters" in mixed[0]
    assert "repliers" in mixed[0] and "2 seeds" in mixed[0] and "at most 10 accounts" in mixed[0]
    assert "all list seeds together count as one seed" in mixed[0]
    # Seeds the user chose are combined without list_mode, with the same warning.
    fake2 = replier_world(anchors=3)
    fake2.posts = search_world()
    user = make_source(fake2, credentials=LOGIN)
    list(user.discover(SeedSet("bluesky", ("#topic", LIST_URI)), 20))
    assert any("mixes 2 sampling frames" in w for w in user.drain_warnings())


def test_members_frame_is_opt_in_and_says_it_cannot_count_bots() -> None:
    fake = Bsky(
        lists={LIST_URI: (CURATE, [member(i) for i in range(30)])},
        generators={FEED_URI: feed_items("f", 30, CUTOFF - timedelta(hours=1))},
    )
    source = make_source(fake, page_size="10", list_mode="members")
    source.note_seeds(SeedSet("bluesky", (LIST_URI, FEED_URI)))
    frame = source.sampling_frame() or ""
    assert "list members" in frame and "feed posters" in frame
    list(source.discover(SeedSet("bluesky", (LIST_URI, FEED_URI)), 20))
    warnings = source.drain_warnings()
    members = next(w for w in warnings if "list_mode=members" in w)
    assert "prominent-accounts frame" in members
    assert "cannot estimate how many bot accounts went dark" in members
    mixed = next(w for w in warnings if "mixes" in w)
    assert "feed posters" in mixed and "the 30 distinct members of 1 list" in mixed


def test_logged_out_bundled_frame_is_repliers() -> None:
    source = make_source(Bsky())
    source.note_seeds(load_seeds("bluesky"))
    frame = source.sampling_frame() or ""
    assert frame.startswith("Sampling frame: repliers") and "7 lists" in frame


def test_cli_prints_the_bluesky_frame_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    patch_client(monkeypatch, world())
    result = runner.invoke(
        app,
        [
            *SCAN,
            "--query",
            FEED_URI,
            "--db",
            str(tmp_path / "c.db"),
            "--out",
            str(tmp_path / "o.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Sampling frame: feed posters" in result.output
    assert "active in the seed communities" not in result.output


# -- partial lists, page bulk, plurals, starter packs ----------------------------------


def test_a_list_that_fails_partway_is_counted_and_warned() -> None:
    def hook(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("getList") and request.url.params.get("cursor"):
            return httpx.Response(502, text="bad gateway")
        return None

    fake = Bsky(lists={LIST_URI: (CURATE, [member(i) for i in range(250)])}, hook=hook)
    source = make_source(fake, list_mode="members", sleeps=[])
    source.http.max_retries = 0
    pool = list(source.discover(SeedSet("bluesky", (LIST_URI,)), 1000))
    assert len(pool) == 100
    warnings = " ".join(source.drain_warnings())
    assert "only partly read" in warnings and "1 page (100 members read)" in warnings
    assert "not uniform over the whole list" in warnings
    assert "100 distinct members of 1 list," in warnings


@pytest.mark.parametrize(
    ("count", "stray"), [(1, 0), (2, 0), (3, 1), (4, 1), (5, 1), (6, 2), (10, 2), (100, 20)]
)
def test_page_bulk_stray_allowance_rounds_up(count: int, stray: int) -> None:
    assert _stray_allowance(count) == stray


def test_a_short_page_tolerates_one_stray_old_item() -> None:
    # Four items: three in the window, one stray far older than the window start.
    items = [
        {"post": post_view(f"did:plc:s{i}", CUTOFF - timedelta(hours=i + 1), n=i)} for i in range(3)
    ] + [{"post": post_view("did:plc:old", CUTOFF - timedelta(days=90), n=9)}]
    items += feed_items("t", 4, CUTOFF - timedelta(hours=10))
    fake = Bsky(generators={FEED_URI: items})
    source = make_source(fake, page_size="4")
    pool = list(source.discover(SeedSet("bluesky", (FEED_URI,)), 50))
    assert len([r for r in fake.requests if r.url.path.endswith("getFeed")]) == 2
    assert {a.user_id for a in pool} >= {"did:plc:t0", "did:plc:t3"}


def test_one_feed_seed_reads_in_the_singular_and_ahead_cap_zero() -> None:
    after = [
        {"post": post_view("did:plc:late", NOW - timedelta(minutes=i), n=i)} for i in range(20)
    ]
    fake = Bsky(generators={FEED_URI: after + feed_items("w", 5, CUTOFF - timedelta(hours=1))})
    source = make_source(fake, page_size="10", max_pages_ahead="0")
    assert list(source.discover(SeedSet("bluesky", (FEED_URI,)), 10)) == []
    assert len(fake.requests) == 1  # max_pages_ahead=0: the first page after the cutoff ends it.
    warnings = " ".join(source.drain_warnings())
    assert "1 Bluesky feed seed is read" in warnings and "this feed will probably" in warnings
    assert "page limit reached: 1 page was" not in warnings
    assert "page limit reached: 1 page " in warnings


def test_starter_packs_are_read_as_their_lists() -> None:
    pack = "at://did:plc:packer/app.bsky.graph.starterpack/abc"
    url = parse_seed("https://bsky.app/starter-pack/packer.example/abc")
    assert (url.kind, url.uri) == ("list", "at://packer.example/app.bsky.graph.starterpack/abc")
    assert parse_seed(pack).kind == "list"
    fake = Bsky(
        lists={LIST_URI: ("app.bsky.graph.defs#referencelist", [member(1), member(2)])},
        starter_packs={pack: LIST_URI},
        profiles={"packer.example": {"did": "did:plc:packer"}},
    )
    source = make_source(fake, list_mode="members")
    seeds = SeedSet("bluesky", ("https://bsky.app/starter-pack/packer.example/abc", pack))
    pool = list(source.discover(seeds, 10))
    assert {a.user_id for a in pool} == {"did:plc:m1", "did:plc:m2"}
    assert fake.methods().count("getList") == 1  # The pack given twice is read once.
    assert [
        r.url.params["starterPack"] for r in fake.requests if r.url.path.endswith("getStarterPack")
    ] == [pack, pack]


# -- --hide-handles redaction ------------------------------------------------------------


def test_redactor_hides_names_dids_and_curator_handles() -> None:
    redactor = _Redactor()
    redactor.add("alice.bsky.social")
    redactor.add("did:plc:alice")
    redactor.add("bob.bsky.social", "#2")
    text = redactor(
        "alice.bsky.social: failed; for example bob.bsky.social. Seed "
        "'https://bsky.app/profile/curator.example/lists/x', at://curator.example/app.bsky."
        "graph.list/x, did:plc:zzz and https://bsky.app/starter-pack/packer.example/abc; "
        "public.api.bsky.app stays."
    )
    for name in ("alice", "curator.example", "packer.example", "did:plc:zzz", "bob.bsky"):
        assert name not in text
    assert "#2." in text and "[hidden]: failed" in text and "public.api.bsky.app" in text


def test_cli_hide_handles_redacts_every_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    seed_url = "https://bsky.app/profile/feedowner.example/feed/topic"
    gone_url = "https://bsky.app/profile/curator.example/feed/gone"  # Its owner is unknown.
    shown = {}
    for hide in (False, True):
        fake = world()
        fake.profiles["feedowner.example"] = {"did": "did:plc:feedowner"}
        broken = "did:plc:acct7"

        def hook(request: httpx.Request, broken: str = broken) -> httpx.Response | None:
            if request.url.path.endswith("getAuthorFeed") and request.url.params["actor"] == broken:
                return xrpc_error(400, "InternalError", "boom")
            return None

        fake.hook = hook
        patch_client(monkeypatch, fake)
        args = [*SCAN, "--query", seed_url, "--query", gone_url]
        args += ["--db", str(tmp_path / f"{hide}.db")]
        args += ["--out", str(tmp_path / f"{hide}.json")]
        result = runner.invoke(app, [*args, "--hide-handles"] if hide else args)
        assert result.exit_code == 0, result.output
        shown[hide] = result.output
    # Without the flag the warnings name the failed account and the seed's curator.
    assert "acct7.bsky.social: Bluesky app.bsky.feed.getAuthorFeed" in shown[False]
    assert "curator.example" in shown[False]
    assert "accounts failed" in shown[True] and "skipped" in shown[True]
    for name in ("bsky.social", "curator.example", "feedowner.example", "did:plc:"):
        assert name not in shown[True]
    assert "[hidden]: Bluesky app.bsky.feed.getAuthorFeed" in shown[True]
    assert "https://bsky.app/profile/[hidden]/feed/gone" in shown[True]
    # report --hide-handles re-scores the run with the same redaction.
    report = runner.invoke(
        app,
        [
            "report",
            "--run-id",
            "1",
            "--db",
            str(tmp_path / "True.db"),
            "--hide-handles",
            "--out",
            str(tmp_path / "r.json"),
        ],
    )
    assert report.exit_code == 0, report.output
    assert "#1" in report.output
    for name in ("bsky.social", "did:plc:"):
        assert name not in report.output


@pytest.mark.parametrize("extra", [[], ["--dry-run-cost"]])
def test_cli_hide_handles_leaves_no_name_in_a_repliers_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    """The dry-run resume command and every other line are redacted as well."""
    for name in LOGIN:
        monkeypatch.delenv(name, raising=False)
    patch_client(monkeypatch, replier_world(anchors=8))
    args = [*SCAN, "--query", LIST_URI, "--hide-handles", *extra]
    args += ["--source-option", "seed_lists=at://curator.example/app.bsky.graph.list/x"]
    args += ["--db", str(tmp_path / "a.db"), "--out", str(tmp_path / "a.json")]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    if extra:
        assert "Fetch it without new discovery" in result.output
    for leak in ("did:", ".bsky.social", "curator", "listowner"):
        assert leak not in result.output, leak


def test_live_log_lines_are_redacted_too(capsys: pytest.CaptureFixture[str]) -> None:
    """A fatal stop logs its warnings before the CLI prints anything else."""
    from bot_farm_silence_tracker import cli

    redactor = cli._start_hiding(True)
    assert redactor is not None
    try:
        redactor.add("alice.bsky.social")
        record = logging.LogRecord(
            "bot_farm_silence_tracker", logging.WARNING, "", 0, "%s", None, None
        )
        record.args = ("1 accounts failed: alice.bsky.social: boom (did:plc:alice)",)
        cli._EchoHandler().emit(record)
    finally:
        cli._start_hiding(False)
    err = capsys.readouterr().err
    assert "alice" not in err and "[hidden]: boom ([hidden])" in err


def test_cli_resume_never_sends_the_login_to_a_stored_pds_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pds_url written into the database is never where the login goes on a resume."""
    fake = world()
    patch_client(monkeypatch, fake)
    for name, value in LOGIN.items():
        monkeypatch.setenv(name, value)
    db = tmp_path / "c.db"
    out = tmp_path / "out.json"
    first = runner.invoke(app, [*SCAN, "--query", "acct", "--db", str(db), "--out", str(out)])
    assert first.exit_code == 0, first.output
    run_id = json.loads(out.read_text())["run"]["run_id"]
    # Someone who can write the database points the run at their own server.
    with sqlite3.connect(db) as conn:
        params = json.loads(
            conn.execute("SELECT params_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
        )
        params.setdefault("source_options", {})["pds_url"] = "https://evil.example"
        conn.execute(
            "UPDATE runs SET params_json = ? WHERE run_id = ?", (json.dumps(params), run_id)
        )
    resume = [
        "scan", "--platform", "bluesky", "--from-run", str(run_id), "--refresh",
        "--now", "2026-10-14T00:00:00Z", "--db", str(db), "--out", str(out),
    ]  # fmt: skip
    fake.requests.clear()
    refused = runner.invoke(app, resume)
    assert refused.exit_code == 2, refused.output
    assert "pds_url=https://evil.example" in refused.output
    assert "Pass it again" in refused.output
    assert fake.requests == []

    # Passed again, the command line value is used without --force.
    again = runner.invoke(app, [*resume, "--source-option", f"pds_url=https://{PDS_HOST}"])
    assert again.exit_code == 0, again.output
    assert fake.requests
    assert {r.url.host for r in fake.requests} <= {PUBLIC_HOST, PDS_HOST}

    # Logged out, the stored value is not needed and is ignored.
    for name in LOGIN:
        monkeypatch.delenv(name)
    fake.requests.clear()
    logged_out = runner.invoke(app, resume)
    assert logged_out.exit_code == 0, logged_out.output
    assert {r.url.host for r in fake.requests} <= {PUBLIC_HOST}


def test_cli_announces_a_login_sent_to_another_pds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = world()
    patch_client(monkeypatch, fake)
    args = [*SCAN, "--query", "acct", "--db", str(tmp_path / "c.db")]
    args += ["--out", str(tmp_path / "o.csv"), "--source-option", "pds_url=https://pds.example.org"]
    logged_out = runner.invoke(app, args)
    assert "login is sent to" not in logged_out.output
    for name, value in LOGIN.items():
        monkeypatch.setenv(name, value)
    result = runner.invoke(app, args)
    assert "note: the bluesky login is sent to pds.example.org" in result.output
    default = runner.invoke(app, args[:-2])
    assert "login is sent to" not in default.output


def test_placebo_samples_need_seeds_that_reach_earlier_weeks() -> None:
    fake = Bsky(posts=search_world(), search_needs_auth=True)
    searching = make_source(fake, credentials=LOGIN)
    assert searching.placebo_discovery_problem(SeedSet("bluesky", ("#topic",))) is None
    # Feed generators keep about a day of posts: no placebo sample from them.
    feeds = make_source(fake, seed_feeds=FEED_URI)
    problem = feeds.placebo_discovery_problem(SeedSet("bluesky", ("#topic",)))
    assert problem is not None and "feed" in problem
