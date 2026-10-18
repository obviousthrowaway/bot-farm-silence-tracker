"""Reddit adapter: official OAuth API only, through the shared HTTP client.

Auth is the app-only ``client_credentials`` grant, or the ``password`` grant when
``BOT_FARM_SILENCE_TRACKER_REDDIT_PASSWORD`` is set. The token is cached and refreshed shortly
before it expires. All API calls go to ``https://oauth.reddit.com`` with
``raw_json=1`` and a Reddit-style User-Agent that names the operator:
``python:bot-farm-silence-tracker:<version> (by /u/<username>)``. The User-Agent is sent as a
per-request header, which overrides the shared client's default one.

Credentials come from ``config.credentials``:
``BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID``,
``BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET`` and
``BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME`` are required,
``BOT_FARM_SILENCE_TRACKER_REDDIT_PASSWORD`` is optional. Non-secret options come from
``--source-option key=value``:

* ``qpm``: queries per minute, default 100. Resizes the shared client's pacer.
* ``discovery``: ``listing``, ``threads`` or ``both`` (default). See :meth:`discover`.
* ``thread_window``: ``auto`` (default), ``week``, ``month`` or ``year``; the ``t``
  parameter of ``/r/{sub}/top`` for thread discovery.
* ``threads_per_sub``: threads whose comments are read per subreddit, default 5.
* ``thread_comment_limit``: comments requested per thread, default 200.
* ``thread_depth``: reply depth requested per thread, default 3.
* ``discovery_budget``: share of ``--max-requests`` that discovery may use, default 0.3.

The pacer is the shared client's token bucket (``default_rps``); a 429 without
``Retry-After`` waits for ``X-Ratelimit-Reset``. The clock is injectable
(``clock=``) so tests never wait.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.models import Account, Activity, content_hash
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import (
    AccountUnavailable,
    BudgetExceeded,
    CredentialUse,
    HostUse,
    RateLimited,
    Source,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import reset_in_seconds

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
PAGE_SIZE = 100
#: Reddit stops paging a listing at about this many items.
LISTING_CAP = 1000
#: A listing that ended with at least this many items is treated as having hit the cap,
#: because Reddit often returns slightly fewer than 1000 (filtered items are dropped).
TRUNCATION_THRESHOLD = LISTING_CAP - PAGE_SIZE
DEFAULT_QPM = 100.0
DEFAULT_BURST = 10.0
#: Refresh the token this many seconds before it expires.
TOKEN_REFRESH_MARGIN = 60.0

DISCOVERY_MODES = ("listing", "threads", "both")
#: ``/top`` time windows and their length in days, shortest first.
THREAD_WINDOWS = {"week": 7, "month": 31, "year": 365}
DEFAULT_THREADS_PER_SUB = 5
DEFAULT_THREAD_COMMENT_LIMIT = 200
DEFAULT_THREAD_DEPTH = 3
DEFAULT_DISCOVERY_BUDGET = 0.3

PASSWORD_ENV = "BOT_FARM_SILENCE_TRACKER_REDDIT_PASSWORD"
CLIENT_ID_ENV = "BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID"
CLIENT_SECRET_ENV = "BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET"
USERNAME_ENV = "BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME"

#: Authors that are never interesting. Compared lowercase.
SKIP_AUTHORS = frozenset({"[deleted]", "automoderator", "reddit", "[removed]"})
_REMOVED_BODIES = frozenset({"[removed]", "[deleted]"})


def is_skippable_author(name: str | None) -> bool:
    """True for deleted authors, AutoModerator and any name ending in ``bot``.

    The suffix rule is deliberately broad: it also drops a few real people (for
    example a user called ``abbot``). That loss is accepted for a random sample.
    """
    if not name:
        return True
    lowered = name.lower()
    return lowered in SKIP_AUTHORS or lowered.endswith("bot")


def _clean(text: str | None) -> str:
    """Return ``text`` stripped, or ``""`` when Reddit replaced it with a removal marker."""
    stripped = (text or "").strip()
    return "" if stripped in _REMOVED_BODIES else stripped


class _ListingError(Exception):
    """Internal: a listing request returned a non-200 status."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"HTTP {status} for {path}")
        self.status = status


class _DiscoveryBudgetSpent(Exception):
    """Internal: discovery used up its share of the request budget."""


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def _num(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not a usable number."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_run_fatal(exc: SourceError) -> bool:
    """True for errors that must stop the whole scan, not just skip one subreddit."""
    return isinstance(exc, SourceAuthError | BudgetExceeded | RateLimited)


class RedditSource(Source):
    """Reddit via the official OAuth API (``client_credentials`` or ``password`` grant)."""

    name: ClassVar[str] = "reddit"
    required_credentials: ClassVar[tuple[str, ...]] = (
        CLIENT_ID_ENV,
        CLIENT_SECRET_ENV,
        USERNAME_ENV,
    )
    optional_credentials: ClassVar[tuple[str, ...]] = (PASSWORD_ENV,)
    network_hosts: ClassVar[tuple[HostUse, ...]] = (
        HostUse(
            httpx.URL(TOKEN_URL).host,
            "OAuth token endpoint (/api/v1/access_token) only",
            credentials=(
                CredentialUse(CLIENT_ID_ENV, "HTTP Basic auth", secret=False),
                CredentialUse(CLIENT_SECRET_ENV, "HTTP Basic auth"),
                CredentialUse(
                    USERNAME_ENV,
                    "User-Agent header; form body with the password grant",
                    secret=False,
                ),
                CredentialUse(PASSWORD_ENV, "form body (password grant), only when set"),
            ),
        ),
        HostUse(
            httpx.URL(API_BASE).host,
            "Reddit API reads",
            credentials=(CredentialUse(USERNAME_ENV, "User-Agent header", secret=False),),
            tokens=("OAuth access token (Authorization: bearer header)",),
        ),
    )
    #: About 100 queries per minute per OAuth client, with a small burst.
    default_rps: ClassVar[float] = DEFAULT_QPM / 60
    default_burst: ClassVar[float] = DEFAULT_BURST

    def __init__(
        self,
        config: SourceConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(config)
        self._clock = clock
        qpm = self.option_float("qpm")
        if qpm is not None:
            if qpm <= 0:
                raise ValueError("option 'qpm' must be positive")
            self.http.set_rate(qpm / 60, self.http.rate[1])
        self.qpm = qpm if qpm is not None else DEFAULT_QPM
        self.discovery = (self.option_str("discovery", "both") or "both").lower()
        if self.discovery not in DISCOVERY_MODES:
            raise ValueError(f"option 'discovery' must be one of {', '.join(DISCOVERY_MODES)}")
        self.thread_window = (self.option_str("thread_window", "auto") or "auto").lower()
        if self.thread_window != "auto" and self.thread_window not in THREAD_WINDOWS:
            raise ValueError("option 'thread_window' must be auto, week, month or year")
        self.threads_per_sub = self._positive_int("threads_per_sub", DEFAULT_THREADS_PER_SUB)
        self.thread_comment_limit = self._positive_int(
            "thread_comment_limit", DEFAULT_THREAD_COMMENT_LIMIT
        )
        self.thread_depth = self._positive_int("thread_depth", DEFAULT_THREAD_DEPTH)
        share = self.option_float("discovery_budget", DEFAULT_DISCOVERY_BUDGET)
        assert share is not None
        if not 0 < share <= 1:
            raise ValueError("option 'discovery_budget' must be in (0, 1]")
        self.discovery_budget = share
        #: Requests sent inside discovery calls only (hydrate calls between yields excluded).
        self._discovery_spent = 0
        self._discovery_budget_warned = False
        # A 429 without Retry-After waits for Reddit's own reset header.
        self.http.configure(reset_wait=reset_in_seconds("X-Ratelimit-Reset"))
        username = self.credentials["BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME"].strip()
        username = username.removeprefix("/")
        if username[:2].lower() == "u/":
            username = username[2:]
        self._username = username
        self._password: str | None = self.credentials.get(PASSWORD_ENV) or None
        self.user_agent = f"python:bot-farm-silence-tracker:{__version__} (by /u/{username})"
        self._token: str | None = None
        self._token_expiry = 0.0

    def _positive_int(self, key: str, default: int) -> int:
        value = self.option_int(key, default)
        assert value is not None
        if value < 1:
            raise ValueError(f"option {key!r} must be at least 1")
        return value

    def __repr__(self) -> str:
        return f"RedditSource(user_agent={self.user_agent!r})"

    # ------------------------------------------------------------------ auth

    def _fetch_token(self) -> None:
        data = (
            {"grant_type": "password", "username": self._username, "password": self._password}
            if self._password
            else {"grant_type": "client_credentials"}
        )
        response = self.http.request(
            "POST",
            TOKEN_URL,
            data=data,
            auth=(
                self.credentials["BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID"],
                self.credentials["BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET"],
            ),
            headers={"User-Agent": self.user_agent},
            sends_credentials=True,
        )
        if response.status_code in (401, 403):
            raise SourceAuthError(
                f"Reddit rejected the client credentials (HTTP {response.status_code}). "
                "Check BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID and "
                "BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET"
                + (" and the password." if self._password else ".")
            )
        if response.status_code != 200:
            raise SourceError(f"token request failed with HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError("token response was not JSON") from exc
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            error = body.get("error") if isinstance(body, dict) else None
            raise SourceAuthError(f"Reddit returned no access token (error: {error!r}).")
        self._token = str(token)
        # The access token may go to the API host only, never back to the token host.
        self.http.bind_credential_values({self._token: {httpx.URL(API_BASE).host}})
        expires_in = _num(body.get("expires_in", 3600))
        if expires_in is None:
            raise SourceError("token response had a non-numeric expires_in")
        self._token_expiry = self._clock() + expires_in

    def _ensure_token(self) -> str:
        if self._token is None or self._clock() >= self._token_expiry - TOKEN_REFRESH_MARGIN:
            self._fetch_token()
        assert self._token is not None
        return self._token

    # --------------------------------------------------------------- requests

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET an API path. 401 refreshes the token once, then raises SourceAuthError.

        Pacing, retries, 429 waits and the request budget all live in the shared client.
        """
        query = {"raw_json": 1, **(params or {})}
        for attempt in range(2):
            token = self._ensure_token()
            response = self.http.get(
                f"{API_BASE}{path}",
                params=query,
                headers={"Authorization": f"bearer {token}", "User-Agent": self.user_agent},
                sends_credentials=True,
            )
            if response.status_code == 401:
                self._token = None
                if attempt == 0:
                    continue
                raise SourceAuthError("Reddit rejected the access token twice (HTTP 401).")
            return response
        raise SourceAuthError("Reddit rejected the access token.")  # pragma: no cover

    def _discovery_get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        """A discovery request: counted against discovery's share of the budget."""
        limit = self.http.max_requests
        if limit is not None:
            allowed = max(1, int(limit * self.discovery_budget))
            # Requests actually sent (retries and token calls included), not calls made.
            if self._discovery_spent >= allowed:
                raise _DiscoveryBudgetSpent
        before = self.http.requests_made
        try:
            return self._api_get(path, params)
        finally:
            self._discovery_spent += self.http.requests_made - before

    def _budget_spent(self) -> None:
        """Warn, once per run, that discovery used up its share of the request budget."""
        if not self._discovery_budget_warned:
            self._discovery_budget_warned = True
            self.warn(
                "discovery budget used up (discovery_budget of --max-requests); "
                "remaining subreddits and threads were not read"
            )

    @staticmethod
    def _json(response: httpx.Response, what: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"invalid JSON from Reddit for {what}") from exc

    @classmethod
    def _json_dict(cls, response: httpx.Response, what: str) -> dict[str, Any]:
        body = cls._json(response, what)
        if not isinstance(body, dict):
            raise SourceError(f"unexpected JSON shape from Reddit for {what}")
        return body

    def _listing_pages(
        self, path: str, params: dict[str, Any], *, discovery: bool = False
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield each page's ``children[].data`` dicts, following ``after`` lazily."""
        after: str | None = None
        seen_after: set[str] = set()
        while True:
            page_params = {"limit": PAGE_SIZE, **params}
            if after:
                page_params["after"] = after
            if discovery:
                response = self._discovery_get(path, page_params)
            else:
                response = self._api_get(path, page_params)
            if response.status_code != 200:
                raise _ListingError(response.status_code, path)
            data = self._json_dict(response, path).get("data") or {}
            children = [c.get("data") or {} for c in data.get("children") or []]
            yield children
            after = data.get("after")
            if not after or after in seen_after or not children:
                return
            seen_after.add(after)

    # -------------------------------------------------------------- discovery

    def _window(self) -> tuple[float, float]:
        """Return ``(low, cutoff)`` epochs: discovery keeps items with ``low <= ts < cutoff``."""
        cutoff = self.config.cutoff.timestamp()
        start = self.config.baseline_start
        return (start.timestamp() if start else float("-inf")), cutoff

    def _listing_stream(self, sub: str, kind: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield ``(author, item)`` for items of ``/new`` or ``/comments`` before the cutoff.

        Both listings are newest first and Reddit serves about 1000 items, so on a busy
        subreddit the first pages are all newer than the cutoff. Those are paged past,
        not yielded: an author seen only after the cutoff cannot be a cliff.
        """
        path = f"/r/{sub}/new" if kind == "new" else f"/r/{sub}/comments"
        low, cutoff = self._window()
        seen = 0
        reached_cutoff = False
        try:
            for page in self._listing_pages(path, {}, discovery=True):
                for item in page:
                    seen += 1
                    created = _num(item.get("created_utc"))
                    if created is None or created >= cutoff:
                        continue
                    reached_cutoff = True
                    if created < low:
                        return  # Newest first: nothing later is inside the baseline.
                    yield item.get("author") or "", item
                if seen >= LISTING_CAP:
                    break
        except _ListingError as exc:
            self.warn(f"skipped r/{sub} {kind} listing: HTTP {exc.status}")
            return
        except _DiscoveryBudgetSpent:
            self._budget_spent()
            return
        except SourceError as exc:
            if _is_run_fatal(exc):
                raise
            self.warn(f"skipped r/{sub} {kind} listing: {exc}")
            return
        if not reached_cutoff and seen >= TRUNCATION_THRESHOLD:
            self.warn(
                f"r/{sub} {kind}: the listing cap ({seen} items) was reached before the "
                "cutoff, so 0 pre-cutoff items were found there; the pool leans toward "
                "recent posters"
            )

    def _resolve_window(self) -> str | None:
        """Pick the ``/top`` time window, or ``None`` (after a warning) if none reaches.

        ``/top?t=`` counts back from now, not from the cutoff. An explicit window must
        reach the cutoff. ``auto`` takes the shortest window that reaches half-way into
        the baseline, so the threads are mostly from the baseline itself.
        """
        low, cutoff = self._window()
        now = self.config.now.timestamp()
        auto = self.thread_window == "auto"
        names = list(THREAD_WINDOWS) if auto else [self.thread_window]
        wanted = cutoff - (cutoff - low) / 2 if auto and low != float("-inf") else cutoff
        for name in names:
            if now - THREAD_WINDOWS[name] * 86400.0 <= wanted:
                return name
        label = "any window up to a year" if auto else f"t={self.thread_window}"
        self.warn(f"thread discovery: {label} does not reach back to the cutoff; no threads read")
        return None

    def _comment_nodes(self, children: Any) -> Iterator[dict[str, Any]]:
        """Flatten a comment tree (``t1`` nodes and their ``replies``), skipping ``more``."""
        stack = list(reversed(children or []))
        while stack:
            node = stack.pop()
            if not isinstance(node, dict) or node.get("kind") != "t1":
                continue
            data = node.get("data") or {}
            yield data
            replies = data.get("replies")
            if isinstance(replies, dict):
                stack.extend(reversed((replies.get("data") or {}).get("children") or []))

    def _thread_stream(self, sub: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield authors from threads created before the cutoff and their comment trees."""
        window = self._resolve_window()
        if window is None:
            return
        low, cutoff = self._window()
        try:
            response = self._discovery_get(f"/r/{sub}/top", {"t": window, "limit": PAGE_SIZE})
            if response.status_code != 200:
                raise _ListingError(response.status_code, f"/r/{sub}/top")
            data = self._json_dict(response, "top listing").get("data") or {}
            candidates = [c.get("data") or {} for c in data.get("children") or []]
        except _ListingError as exc:
            self.warn(f"skipped r/{sub} threads: HTTP {exc.status}")
            return
        except _DiscoveryBudgetSpent:
            self._budget_spent()
            return
        except SourceError as exc:
            if _is_run_fatal(exc):
                raise
            self.warn(f"skipped r/{sub} threads: {exc}")
            return
        # Filter to the baseline window before slicing, so old or new threads cannot
        # use up the per-subreddit quota.
        threads = []
        for t in candidates:
            created = _num(t.get("created_utc"))
            if t.get("id") and created is not None and low <= created < cutoff:
                threads.append(t)
        threads = threads[: self.threads_per_sub]
        if not threads:
            self.warn(f"r/{sub}: no top threads of the {window} were created inside the baseline")
            return
        for thread in threads:
            yield thread.get("author") or "", thread
            try:
                reply = self._discovery_get(
                    f"/comments/{thread['id']}",
                    {
                        "limit": self.thread_comment_limit,
                        "depth": self.thread_depth,
                        "sort": "old",
                    },
                )
                if reply.status_code != 200:
                    self.warn(f"skipped a thread in r/{sub}: HTTP {reply.status_code}")
                    continue
                body = self._json(reply, "thread")
            except _DiscoveryBudgetSpent:
                self._budget_spent()
                return
            except SourceError as exc:
                if _is_run_fatal(exc):
                    raise
                self.warn(f"skipped a thread in r/{sub}: {exc}")
                continue
            if not isinstance(body, list) or len(body) < 2 or not isinstance(body[1], dict):
                continue
            for comment in self._comment_nodes((body[1].get("data") or {}).get("children")):
                ts = _num(comment.get("created_utc"))
                if ts is not None and low <= ts < cutoff:
                    yield comment.get("author") or "", comment

    def _author_stream(self, sub: str, kind: str) -> Iterator[tuple[str, dict[str, Any]]]:
        if kind == "threads":
            return self._thread_stream(sub)
        return self._listing_stream(sub, kind)

    def placebo_discovery_problem(self, seeds: SeedSet) -> str | None:
        del seeds
        return (
            "Reddit's own API lists only about the newest 1000 items of a subreddit, with "
            "no time bounds, so it cannot discover the accounts active in earlier weeks "
            "(the reddit-archive platform can)"
        )

    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        """Yield authors with an item inside ``[baseline_start, cutoff)``.

        ``discovery=listing`` reads ``/r/{sub}/new`` and ``/r/{sub}/comments`` and pages
        past items newer than the cutoff (Reddit serves about 1000 per listing).
        ``discovery=threads`` reads ``/r/{sub}/top`` for threads created before the
        cutoff and then each thread's comment tree. ``both`` (default) does both.
        Discovery stops at ``discovery_budget`` of ``--max-requests``.
        """
        self._discovery_spent = 0
        self._discovery_budget_warned = False
        kinds = {
            "listing": ("new", "comments"),
            "threads": ("threads",),
            "both": ("new", "comments", "threads"),
        }[self.discovery]
        # One stream per (subreddit, source), pulled round-robin one author at a time so
        # a huge subreddit cannot crowd out small ones. Streams fetch pages lazily.
        streams = [
            (sub, kind, self._author_stream(sub, kind)) for sub in seeds.items for kind in kinds
        ]
        seen: set[str] = set()
        yielded = 0
        while streams and yielded < pool_limit:
            still_open = []
            for sub, kind, stream in streams:
                if yielded >= pool_limit:
                    return
                for author, item in stream:
                    key = author.lower()
                    if is_skippable_author(author) or key in seen:
                        continue
                    seen.add(key)
                    meta: dict[str, Any] = {"discovered_in": sub, "discovered_via": kind}
                    if item.get("author_fullname"):
                        meta["fullname"] = item["author_fullname"]
                    yield Account("reddit", key, author, None, meta)
                    yielded += 1
                    still_open.append((sub, kind, stream))
                    break
            streams = still_open

    # --------------------------------------------------------------- hydrate

    def _about(self, account: Account) -> dict[str, Any]:
        """Return ``/user/{name}/about`` data, raising AccountUnavailable if it is gone."""
        response = self._api_get(f"/user/{account.handle}/about")
        if response.status_code == 404:
            raise AccountUnavailable("deleted", f"{account.handle} not found (HTTP 404)")
        if response.status_code == 403:
            raise AccountUnavailable("private", f"{account.handle} is forbidden (HTTP 403)")
        if response.status_code != 200:
            raise SourceError(f"unexpected HTTP {response.status_code} for user {account.handle}")
        data = self._json_dict(response, "user about").get("data") or {}
        if data.get("is_suspended"):
            raise AccountUnavailable("suspended")
        return data

    def hydrate(self, account: Account) -> Account:
        if account.created_at is not None:
            return account
        data = self._about(account)
        created = _num(data.get("created_utc"))
        if created is None:
            raise SourceError(f"no usable created_utc in the profile of {account.handle}")
        account.created_at = _utc(created)
        account.meta["is_suspended"] = False  # Lets a later 403 skip a second /about call.
        for key in ("link_karma", "comment_karma"):
            if key in data:
                account.meta[key] = data[key]
        return account

    # -------------------------------------------------------------- activity

    def _activity_from(self, account: Account, item: dict[str, Any]) -> Activity | None:
        created = _num(item.get("created_utc"))
        if created is None:
            return None
        if "title" in item:  # A submission (t3).
            kind = "post"
            text = f"{_clean(item.get('title'))} {_clean(item.get('selftext'))}".strip()
        else:  # A comment (t1).
            kind, text = "comment", _clean(item.get("body"))
        return Activity(
            platform="reddit",
            user_id=account.user_id,
            ts=_utc(created),
            kind=kind,
            community=item.get("subreddit"),
            content_hash=content_hash(text) if text else None,
        )

    def _resolve_forbidden(self, account: Account) -> AccountUnavailable:
        """Decide what a 403 on a user listing means.

        ``hydrate`` records ``is_suspended=False`` in the account's meta, which answers the
        question without a request. Otherwise one ``about`` call decides.
        """
        if account.meta.get("is_suspended") is False:
            return AccountUnavailable(
                "private", f"{account.handle} listing is forbidden (HTTP 403)"
            )
        try:
            self._about(account)
        except AccountUnavailable as exc:
            return exc
        return AccountUnavailable("private", f"{account.handle} listing is forbidden (HTTP 403)")

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        out: list[Activity] = []
        seen_items = 0
        reached_since = False
        path = f"/user/{account.handle}/overview"
        try:
            for page in self._listing_pages(path, {"sort": "new"}):
                for item in page:
                    seen_items += 1
                    activity = self._activity_from(account, item)
                    if activity is None:
                        continue
                    if activity.ts < since:
                        # A pinned or stickied item sits at the top whatever its age, so
                        # it says nothing about where the history ends.
                        if item.get("pinned") or item.get("stickied"):
                            continue
                        reached_since = True
                        break
                    if activity.ts < until:
                        out.append(activity)
                if reached_since:
                    break
        except _ListingError as exc:
            if exc.status == 404:
                raise AccountUnavailable("deleted", f"{account.handle} not found") from None
            if exc.status == 403:
                raise self._resolve_forbidden(account) from None
            raise SourceError(f"unexpected HTTP {exc.status} for {path}") from None
        account.meta["truncated"] = not reached_since and seen_items >= TRUNCATION_THRESHOLD
        return out
