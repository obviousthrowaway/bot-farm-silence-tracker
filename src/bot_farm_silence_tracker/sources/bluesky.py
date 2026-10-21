"""Bluesky source, read through the public atproto XRPC API.

Bluesky's AppView serves the public network data over documented XRPC endpoints.
This adapter only reads; it never writes records, follows, likes or posts. It sends
an honest ``User-Agent`` that names the tool and links to its project page.

API facts this adapter relies on (lexicons in ``bluesky-social/atproto``,
``lexicons/app/bsky/...``; host and rate-limit guidance in the Bluesky developer
docs, "API Hosts and Auth" and "Rate Limits"; AppView behavior in
``packages/bsky/src``; all checked on 2026-10-12):

* ``https://public.api.bsky.app`` is the documented, cached host for unauthenticated
  reads. ``app.bsky.actor.getProfile``, ``app.bsky.feed.getAuthorFeed``,
  ``app.bsky.feed.getFeed`` and ``app.bsky.feed.getListFeed`` work there without a
  login.
* ``app.bsky.feed.searchPosts`` "may require authentication" (its lexicon says so).
  On ``public.api.bsky.app`` it currently answers HTTP 403 with an HTML page from the
  CDN, which Bluesky describes as deliberate load shedding. This adapter does not
  route around that block: logged-out requests only ever go to
  ``public.api.bsky.app``, and no option points them at another AppView. Search
  therefore needs the optional login
  (``BOT_FARM_SILENCE_TRACKER_BSKY_HANDLE`` and ``BOT_FARM_SILENCE_TRACKER_BSKY_APP_PASSWORD``):
  a session from ``com.atproto.server.createSession`` on the account's PDS or the
  ``https://bsky.social`` entryway, which proxies the search to the AppView. Without
  a login, discovery uses feed and list seeds only.
* ``app.bsky.graph.getList`` (``list``, ``limit`` 1 to 100, ``cursor``) works on the
  public host without a login. It returns the list view (with its ``purpose``:
  ``app.bsky.graph.defs#curatelist``, ``#modlist`` or ``#referencelist``, the last one
  being a starter pack's list) and the list's members as profile views.
* Feed generators keep only recent posts: in the live check of 2026-10-12 most
  political feeds held about a day of history, so a feed read more than a day after
  the cutoff rarely reaches the window. ``getListFeed`` pages back through the
  members' posts without that limit, but an active list holds a day or two of posts
  in every seven pages, so reaching back 30 days costs hundreds of requests. List
  seeds are therefore read as member lists (see :meth:`BlueskySource.discover`).
* ``app.bsky.feed.getPostThread`` (``uri``, ``depth``, ``parentHeight``) works on the
  public host without a login. With ``depth=1`` and ``parentHeight=0`` it returns the
  post as an ``app.bsky.feed.defs#threadViewPost`` whose ``replies`` are the direct
  replies, each a thread view (or a ``#notFoundPost`` / ``#blockedPost``) whose
  ``post.author`` is a basic profile view that carries the author's ``labels``,
  ``!no-unauthenticated`` included, so opted-out repliers can be left out without a
  profile request. An unknown post answers HTTP 400. ``getAuthorFeed`` post views
  carry ``replyCount``.
* ``app.bsky.graph.getStarterPack`` (``starterPack``, an AT-URI) works without a
  login and names the pack's list in ``starterPack.list.uri`` (purpose
  ``#referencelist``).
* ``searchPosts`` takes ``q`` (required), ``sort`` (``latest`` or ``top``),
  ``since`` (inclusive) and ``until`` (exclusive), both on the post's ``sortAt``,
  and ``limit`` 1 to 100. ``getAuthorFeed`` takes ``actor``, ``limit`` 1 to 100,
  ``cursor`` and ``filter`` (default ``posts_with_replies``), and returns the author's
  posts, replies and reposts newest first, ordered by ``sortAt``.
* For a post the AppView stores ``sortAt = min(indexedAt, createdAt)``.
  ``createdAt`` is written by the client and can be set to any value, so activity
  is timed by ``indexedAt``, the moment the AppView first saw the record. That makes
  a backdated or future-dated post count when it was really published. One limit
  follows: a post whose ``createdAt`` was backdated before the window is listed at
  its ``createdAt`` position, past the point where paging stops, and is missed.
* ``getAuthorFeed`` takes ``includePins``, which defaults to false; this adapter never
  sets it, so pins are not requested.
* ``getProfile`` reports account states as HTTP 400 errors: ``AccountTakedown``
  ("Account has been suspended", which covers takedowns and suspensions),
  ``AccountDeactivated`` ("Account is deactivated"), and ``InvalidRequest`` "Profile
  not found" (deleted or unknown). ``getAuthorFeed`` answers "Profile not found" for
  all three, so the adapter asks ``getProfile`` to tell them apart.
* The Bluesky AppView publishes no request limits ("generous rate-limits"); the PDS
  allows 3000 requests per 5 minutes per IP and ``createSession`` 30 times per 5
  minutes per account. Responses may carry ``RateLimit-Remaining`` and
  ``RateLimit-Reset`` (epoch seconds), and a limit is signalled by HTTP 429.

Accounts labelled ``!no-unauthenticated`` asked not to be shown to logged-out viewers.
Discovery never pools them. ``hydrate``, or the author feed when it shows the label on
the account's own posts (a cached or resumed account whose profile is not read
again), raises :class:`AccountUnavailable` with status ``opted_out`` and replaces the
account's handle by its DID first. The core then stores only the DID with the flag,
so a resumed run can skip it, and never writes the account to any output: it is
counted only.

Account statuses: ``AccountTakedown`` is recorded as ``suspended``. A deactivated
account and a deleted one are both recorded as ``deleted``, because the core has no
separate status for a deactivation; a deactivated account may come back, so it also
gets ``meta["deactivated"]``, which the output shows in the ``deactivated`` column.

Data horizon: the AppView is live, so the adapter reports none (the core scores as of
now). An earlier version probed a feed for its newest post; a failed probe then
stopped the whole scan as stale, which a live source never is.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from bot_farm_silence_tracker.models import STATUS_OPTED_OUT, Account, Activity, content_hash
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import (
    AccountUnavailable,
    BudgetExceeded,
    CredentialUse,
    HostUse,
    NewestActivity,
    RateLimited,
    Source,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import (
    PROJECT_URL,
    Attempt,
    parse_retry_after,
    redact_userinfo,
    research_user_agent,
)
from bot_farm_silence_tracker.timeutil import isoformat, utcnow

#: Names the project page as the contact, or none while this copy is unpublished.
USER_AGENT = research_user_agent(PROJECT_URL)

#: The documented, cached host for unauthenticated AppView reads.
PUBLIC_APPVIEW = "https://public.api.bsky.app"
#: The entryway that creates sessions for accounts hosted by Bluesky.
DEFAULT_PDS = "https://bsky.social"
#: Service proxying target for authenticated ``app.bsky.*`` requests.
APPVIEW_PROXY = "did:web:api.bsky.app#bsky_appview"

HANDLE_ENV = "BOT_FARM_SILENCE_TRACKER_BSKY_HANDLE"
PASSWORD_ENV = "BOT_FARM_SILENCE_TRACKER_BSKY_APP_PASSWORD"

GET_PROFILE = "app.bsky.actor.getProfile"
GET_AUTHOR_FEED = "app.bsky.feed.getAuthorFeed"
SEARCH_POSTS = "app.bsky.feed.searchPosts"
GET_FEED = "app.bsky.feed.getFeed"
GET_LIST_FEED = "app.bsky.feed.getListFeed"
GET_LIST = "app.bsky.graph.getList"
GET_POST_THREAD = "app.bsky.feed.getPostThread"
GET_STARTER_PACK = "app.bsky.graph.getStarterPack"
CREATE_SESSION = "com.atproto.server.createSession"
REFRESH_SESSION = "com.atproto.server.refreshSession"

FEED_COLLECTION = "app.bsky.feed.generator"
LIST_COLLECTION = "app.bsky.graph.list"
STARTERPACK_COLLECTION = "app.bsky.graph.starterpack"
THREAD_VIEW = "app.bsky.feed.defs#threadViewPost"
REPOST_REASON = "app.bsky.feed.defs#reasonRepost"
QUOTE_EMBEDS = frozenset({"app.bsky.embed.record", "app.bsky.embed.recordWithMedia"})
#: List purposes accepted as member seeds: curated lists and starter-pack lists.
MEMBER_LIST_PURPOSES = frozenset(
    {"app.bsky.graph.defs#curatelist", "app.bsky.graph.defs#referencelist"}
)
#: Moderation (mute or block) lists are refused as seeds: their members were picked for
#: being reported or blocked, which would bias the sample toward suspensions.
MODLIST_PURPOSE = "app.bsky.graph.defs#modlist"
#: Self-applied label: the account asked not to be shown to logged-out viewers.
NO_UNAUTHENTICATED = "!no-unauthenticated"

#: Largest ``limit`` the endpoints accept.
MAX_PAGE = 100
#: Polite default request rate, in requests per second. Bluesky publishes no AppView
#: limit; one request a second keeps a scan well below the PDS's 3000 per 5 minutes.
DEFAULT_RPS = 1.0
#: Highest rate the ``max_rps`` option may ask for.
MAX_RPS_CEILING = 5.0
#: The lowest rate the adapter drops to after load signals.
MIN_RPS = 0.1
#: Additive recovery: after this many successful requests in a row, a lowered rate
#: rises by ``RECOVERY_STEP`` requests per second, up to ``max_rps``.
RECOVERY_STREAK = 20
RECOVERY_STEP = 0.1
#: Statuses that mean the server is throttling us or overloaded: the rate is halved.
LOAD_STATUSES = frozenset({429, 502, 503, 504})
#: Seconds added to a ``RateLimit-Reset`` before the next request.
RESET_MARGIN = 1.0
MAX_CONCURRENCY = 3
#: Discovery searches a one-day slice; its end is drawn from ``[window start, cutoff +
#: slice)`` and clamped to the cutoff, so every moment of the window is equally likely
#: to be inside the slice (uniform in time; heavier posters are more likely to be drawn).
DISCOVERY_SLICE = timedelta(days=1)
#: A seed whose page failed is retried this many times, with a growing pause, before
#: it is dropped from discovery.
SEED_RETRIES = 2
#: Pause before the first seed retry, in seconds; doubled for each further retry.
SEED_RETRY_BACKOFF = 5.0
#: A seed whose pages yield no new author this many times in a row is done.
DRY_PAGES = 5
#: Pages read from one feed or list seed at most, not counting pages that lie entirely
#: after the cutoff (those are capped by ``MAX_PAGES_AHEAD``).
FEED_PAGES = 20
#: Pages read from one feed or list seed while its items are still newer than the
#: cutoff, before the seed gives up on reaching the window (``max_pages_ahead``).
MAX_PAGES_AHEAD = 30
#: A feed page is placed in time by the bulk of its items, not by its oldest item:
#: it counts as "ahead of the window" when at least this share of its items is at or
#: after the cutoff, and as "past the window start" when at least this share is before
#: the start. Feeds are not strictly ordered by time (pinned or boosted posts, odd
#: ``sortAt`` values), so one stray old item must not end a seed.
PAGE_BULK = 0.8
#: How long feed generators typically keep posts. When the cutoff is further back
#: than this, feed seeds are warned about up front.
FEED_HISTORY = timedelta(days=1)
#: Pages of members read from one list at most (``list_member_pages``), 100 each.
LIST_MEMBER_PAGES = 50
#: How list seeds are read: ``repliers`` (the default: the lists' members are anchors,
#: and the pool is the accounts that replied to their posts in the window),
#: ``members`` (``getList``: the members themselves) or ``feed`` (``getListFeed``, the
#: members' recent posts, like a feed seed).
LIST_MODES = ("repliers", "members", "feed")
#: Repliers mode: anchor accounts drawn at random from the lists' members
#: (``anchor_accounts``).
ANCHOR_ACCOUNTS = 40
#: Repliers mode: window posts drawn at random from each anchor, whose direct
#: replies are read (``anchor_posts_per_account``).
ANCHOR_POSTS_PER_ACCOUNT = 3
#: Repliers mode: reply authors kept at most from one thread, drawn at random from
#: the thread's direct replies in the window (``thread_reply_limit``).
THREAD_REPLY_LIMIT = 10
#: Repliers mode: author-feed pages read from one anchor at most, looking for its
#: posts in the window (``anchor_feed_pages``).
ANCHOR_FEED_PAGES = 10
#: Repliers mode: discovery stops reading anchors once it has used this share of the
#: requests that ``--max-requests`` left when it began, so scoring keeps the rest.
REPLIERS_BUDGET_SHARE = 0.5
#: Said wherever the sampling frame is the members of curated lists.
NO_BOT_ESTIMATE = (
    "This frame cannot estimate how many bot accounts went dark; bots are rarely on "
    "curated lists of officials and outlets."
)
#: Pages read from one author feed at most (``max_pages`` option).
DEFAULT_MAX_PAGES = 50

_FATAL = (BudgetExceeded, RateLimited, SourceAuthError)
_DAY_S = 86400.0
_DAY = timedelta(days=1)

#: Options that earlier versions had and that are refused now, with the reason.
REMOVED_OPTIONS: dict[str, str] = {
    "appview_url": (
        "logged-out reads always go to https://public.api.bsky.app; this tool does not "
        "point them at another AppView to get around Bluesky's block on logged-out search"
    ),
    "freshness_feed": "the adapter no longer probes a feed: the AppView is treated as live",
}

SEARCH_NEEDS_LOGIN = (
    "Bluesky post search needs a login: the public AppView host refuses unauthenticated "
    f"searches. Set {HANDLE_ENV} and {PASSWORD_ENV} (an app password, never the account "
    "password), or seed discovery from public feeds or lists with --source-option "
    "seed_feeds=at://... or seed_lists=at://... (see the Bluesky section of docs/USAGE.md)."
)


def _parse_time(value: object) -> datetime | None:
    """Parse an atproto datetime (RFC 3339) into aware UTC, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _opted_out(actor: object) -> bool:
    """True when ``actor`` carries the ``!no-unauthenticated`` label."""
    labels = actor.get("labels") if isinstance(actor, dict) else None
    return isinstance(labels, list) and any(
        isinstance(label, dict) and label.get("val") == NO_UNAUTHENTICATED for label in labels
    )


#: ``https://`` plus a DNS host name, optionally ``:443`` and a trailing slash. Nothing
#: else: no user name or password, no other port, path, query or fragment, and no
#: character a URL parser could read as the end of the host (a backslash, ``@``, ``#``).
_HTTPS_ORIGIN = re.compile(
    r"https://((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?::443)?/?",
    re.IGNORECASE,
)


def _https_origin(key: str, value: str) -> str:
    """``value`` as ``https://host`` (lowercase), if it is an https origin on port 443.

    Raises ``ValueError`` for anything else, with any user name and password in the
    value redacted from the message.
    """
    text = value.strip()
    match = _HTTPS_ORIGIN.fullmatch(text)
    if match is not None:
        host = match.group(1).lower()
        try:
            # The client compares decoded host names; an IDN (xn--) host would differ.
            same = httpx.URL(f"https://{host}").host == host
        except (httpx.InvalidURL, ValueError):
            same = False
        if not same:
            match = None
    if match is None:
        raise ValueError(
            f"option {key!r} must be an https:// origin (an ASCII host name, no user "
            f"name, password, port other than 443, path, query or fragment), "
            f"got {redact_userinfo(text)!r}"
        )
    return f"https://{host}"


def _header_float(response: httpx.Response, name: str) -> float | None:
    try:
        return float(response.headers[name])
    except (KeyError, ValueError):
        return None


def _iso_z(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fmt_time(when: datetime | None) -> str:
    return "unknown" if when is None else when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _plural(count: int, noun: str, plural: str | None = None) -> str:
    """``"1 seed"``, ``"2 seeds"``: a count with its noun in the right number."""
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


#: One discovery row: ``(did, handle, indexedAt or None, community or None)``. List
#: members carry no time (they are not found through a post), and a row's community
#: defaults to its seed's label.
_Row = tuple[str, str, datetime | None, str | None]


@dataclass
class _Seed:
    """One discovery seed: a search query, a feed generator, a list, the shuffled
    members of the list seeds (kind ``members``), or the accounts that replied to the
    list members' posts (kind ``repliers``)."""

    label: str
    kind: str  # "query", "feed", "list", "members" or "repliers"
    uri: str | None = None
    #: Given with ``seed_feeds`` or ``seed_lists`` rather than in the seed list.
    explicit: bool = False
    #: A list's display name, read from its first ``getList`` page.
    name: str | None = None
    cursor: str | None = None
    pages: int = 0
    #: Pages that lay entirely after the cutoff (see ``PAGE_BULK``).
    ahead: int = 0
    last_ahead: bool = False
    dry: int = 0
    in_window: int = 0
    taken: int = 0
    errors: int = 0
    reached_start: bool = False
    #: Oldest ``sortAt`` seen on any page of the seed.
    oldest: datetime | None = None
    #: Why the seed stopped: "start", "exhausted", "ahead_cap", "page_cap" or "dry".
    end: str | None = None
    failed: bool = False
    done: bool = False
    #: For a ``members`` seed: the rows still to hand out, in random order.
    queue: list[_Row] | None = None
    #: For a ``repliers`` seed: the anchors still to read, as ``(did, list name)``, in
    #: random order, and every anchor-list member (never pooled as a replier).
    anchors: list[tuple[str, str]] | None = None
    members: frozenset[str] = frozenset()


def parse_seed(item: str) -> _Seed:
    """Classify a seed string: an AT-URI or bsky.app URL of a feed or list, else a query.

    ``at://<did>/app.bsky.feed.generator/<rkey>`` and
    ``https://bsky.app/profile/<actor>/feed/<rkey>`` are feeds;
    ``at://<did>/app.bsky.graph.list/<rkey>`` and
    ``https://bsky.app/profile/<actor>/lists/<rkey>`` are lists. A starter pack,
    ``at://<did>/app.bsky.graph.starterpack/<rkey>`` or
    ``https://bsky.app/starter-pack/<actor>/<rkey>``, is a list too: its list is looked
    up with ``getStarterPack`` before the first request. A bsky.app URL with a handle
    instead of a DID keeps the handle; it is resolved before the first request.
    """
    text = item.strip()
    if text.startswith("at://"):
        parts = text[len("at://") :].split("/")
        if len(parts) == 3 and all(parts):
            if parts[1] == FEED_COLLECTION:
                return _Seed(text, "feed", text)
            if parts[1] in (LIST_COLLECTION, STARTERPACK_COLLECTION):
                return _Seed(text, "list", text)
        raise ValueError(f"seed {item!r} is not a feed, list or starter-pack AT-URI")
    if text.startswith(("https://bsky.app/", "http://bsky.app/")):
        parts = urlsplit(text).path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "profile" and parts[1] and parts[3]:
            if parts[2] == "feed":
                return _Seed(text, "feed", f"at://{parts[1]}/{FEED_COLLECTION}/{parts[3]}")
            if parts[2] == "lists":
                return _Seed(text, "list", f"at://{parts[1]}/{LIST_COLLECTION}/{parts[3]}")
        if len(parts) == 3 and parts[0] == "starter-pack" and parts[1] and parts[2]:
            return _Seed(text, "list", f"at://{parts[1]}/{STARTERPACK_COLLECTION}/{parts[2]}")
        raise ValueError(f"seed {item!r} is not a bsky.app feed, list or starter-pack URL")
    return _Seed(text, "query")


class _XrpcError(SourceError):
    """An XRPC error answer: HTTP status plus the lexicon error name."""

    def __init__(self, method: str, status: int, error: str | None, message: str | None):
        self.method = method
        self.status = status
        self.error = error
        self.message = message or ""
        # The server's message can name an account, so only the error name is kept.
        super().__init__(f"Bluesky {method} returned HTTP {status} ({error or 'no error name'})")


class _SearchBlocked(SourceError):
    """``searchPosts`` was refused without a login (the public host's 403 page)."""


class _RefusedSeed(SourceError):
    """A seed that must not be used (a moderation list); it is dropped, not retried."""


def _stray_allowance(count: int) -> int:
    """How many items of a ``count``-item page may lie outside its bulk on either side.

    ``ceil((1 - PAGE_BULK) * count)``, capped at ``(count - 1) // 2`` so that the
    bulk is always a strict majority of the page. A page of one or two items
    therefore has no stray allowance, and pages of three to five items tolerate
    exactly one stray item.
    """
    return min(math.ceil(round((1 - PAGE_BULK) * count, 9)), (count - 1) // 2)


def _bulk_times(times: list[datetime]) -> tuple[datetime, datetime] | None:
    """The low and high ends of the bulk of ``times`` (see :func:`_stray_allowance`).

    All items but the stray allowance are at or after the first value, and all but
    the stray allowance are at or before the second.
    """
    if not times:
        return None
    ordered = sorted(times)
    stray = _stray_allowance(len(ordered))
    return ordered[stray], ordered[len(ordered) - 1 - stray]


class _ThreadState(threading.local):
    def __init__(self) -> None:
        self.phase = "other"
        self.sent_at: float | None = None
        self.sent_rps: float = 0.0
        #: ``indexedAt`` of the uncounted reposts the current author-feed walk saw.
        self.reposts: list[datetime] = []


class BlueskySource(Source):
    """Bluesky accounts and activity from the public atproto XRPC API."""

    name: ClassVar[str] = "bluesky"
    required_credentials: ClassVar[tuple[str, ...]] = ()
    optional_credentials: ClassVar[tuple[str, ...]] = (HANDLE_ENV, PASSWORD_ENV)
    env_platform: ClassVar[str | None] = "BSKY"
    #: ``pds_url`` decides where the handle and app password go, so ``--from-run``
    #: never inherits it.
    credential_destination_options: ClassVar[frozenset[str]] = frozenset({"pds_url"})
    #: The PDS entry is ``bsky.social`` unless ``pds_url`` names another https origin
    #: (see :meth:`host_uses`); it is contacted only with a login.
    network_hosts: ClassVar[tuple[HostUse, ...]] = (
        HostUse(
            httpx.URL(PUBLIC_APPVIEW).host,
            "logged-out reads: profiles, author feeds, feeds, lists, threads (no credentials)",
        ),
        HostUse(
            httpx.URL(DEFAULT_PDS).host,
            "login (createSession), session refresh and logged-in search via the PDS",
            credentials=(
                CredentialUse(HANDLE_ENV, "createSession JSON body", secret=False),
                CredentialUse(PASSWORD_ENV, "createSession JSON body"),
            ),
            tokens=("session access and refresh tokens (Authorization: Bearer header)",),
            login_only=True,
        ),
    )
    default_rps: ClassVar[float] = DEFAULT_RPS
    default_burst: ClassVar[float] = 1.0
    default_timeout: ClassVar[float | httpx.Timeout] = httpx.Timeout(20.0, connect=10.0)
    #: Login secrets belong in the environment. These option names are refused, and
    #: never stored with a run should one slip through.
    secret_options: ClassVar[tuple[str, ...]] = (
        "handle",
        "bsky_handle",
        "identifier",
        "app_password",
        "password",
    )
    #: The pre-check reads the newest page of the author feed, which is newest first,
    #: so it answers "any activity since T?" with one request for most accounts.
    supports_baseline_sampling: ClassVar[bool] = True
    #: One feed holds posts, replies (and reposts when enabled), so one query answers
    #: for every kind; the kind name is only a label here.
    precheck_kinds: ClassVar[tuple[str, ...]] = ("any",)
    max_concurrency: ClassVar[int] = MAX_CONCURRENCY
    #: Reposts are left out of activity by default, so cliff accounts carry the reposts
    #: they made after the cutoff; deleted accounts carry the deactivated flag.
    extra_columns: ClassVar[tuple[str, ...]] = ("reposts_after_cutoff", "deactivated")
    reports_opted_out: ClassVar[bool] = True

    def __init__(
        self,
        config: SourceConfig,
        *,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(config)
        for key in self.secret_options:
            if key in config.options:
                raise ValueError(
                    f"option {key!r} looks like a login secret; set {HANDLE_ENV} and "
                    f"{PASSWORD_ENV} in the environment instead"
                )
        for key, why in REMOVED_OPTIONS.items():
            if key in config.options:
                raise ValueError(f"option {key!r} is not supported: {why}")
        if not 1 <= config.concurrency <= MAX_CONCURRENCY:
            raise ValueError(
                f"concurrency must be between 1 and {MAX_CONCURRENCY}, got {config.concurrency}"
            )
        handle = self.credentials.get(HANDLE_ENV, "")
        password = self.credentials.get(PASSWORD_ENV, "")
        if bool(handle) != bool(password):
            raise ValueError(f"set both {HANDLE_ENV} and {PASSWORD_ENV}, or neither")
        self._login: tuple[str, str] | None = (handle, password) if handle else None
        self._wall_clock = wall_clock or time.time
        self._lock = threading.RLock()
        self._session_lock = threading.Lock()
        self._tls = _ThreadState()

        #: Logged-out reads go here and nowhere else (no option changes it).
        self.appview = PUBLIC_APPVIEW
        self.pds = self._https_option("pds_url", DEFAULT_PDS)
        self.page_size = self._int_option("page_size", MAX_PAGE, 1, MAX_PAGE)
        self.max_pages = self._int_option("max_pages", DEFAULT_MAX_PAGES, 1, 10_000)
        self.feed_pages = self._int_option("feed_pages", FEED_PAGES, 1, 1000)
        self.dry_pages = self._int_option("dry_pages", DRY_PAGES, 1, 1000)
        self.max_pages_ahead = self._int_option("max_pages_ahead", MAX_PAGES_AHEAD, 0, 1000)
        self.list_member_pages = self._int_option("list_member_pages", LIST_MEMBER_PAGES, 1, 10_000)
        #: True when the user chose ``list_mode``; with a login and the bundled seeds,
        #: only then are the bundled lists used next to search (see :meth:`discover`).
        self.list_mode_given = bool((self.option_str("list_mode") or "").strip())
        self.list_mode = (self.option_str("list_mode") or LIST_MODES[0]).strip().lower()
        if self.list_mode not in LIST_MODES:
            raise ValueError(
                f"option 'list_mode' must be one of {', '.join(LIST_MODES)}, got {self.list_mode!r}"
            )
        self.anchor_accounts = self._int_option("anchor_accounts", ANCHOR_ACCOUNTS, 1, 10_000)
        self.anchor_posts = self._int_option(
            "anchor_posts_per_account", ANCHOR_POSTS_PER_ACCOUNT, 1, 1000
        )
        self.thread_reply_limit = self._int_option(
            "thread_reply_limit", THREAD_REPLY_LIMIT, 1, 10_000
        )
        self.anchor_feed_pages = self._int_option("anchor_feed_pages", ANCHOR_FEED_PAGES, 1, 1000)
        self.include_reposts = self.option_bool("include_reposts", False)
        self.lang = self.option_str("lang")
        base_rps = self.http.rate[0]
        max_rps = self.option_float("max_rps", base_rps)
        if max_rps is None or not MIN_RPS <= max_rps <= MAX_RPS_CEILING:
            raise ValueError(
                f"option 'max_rps' must be between {MIN_RPS:g} and {MAX_RPS_CEILING:g}, "
                f"got {max_rps!r}"
            )
        self.max_rps = max_rps
        self._rps = max_rps
        if self._rps != base_rps:
            self.http.adjust_rate(self._rps)
        self._extra_seeds: list[_Seed] = []
        for key, kind in (("seed_feeds", "feed"), ("seed_lists", "list")):
            for raw in (self.option_str(key) or "").split(","):
                if raw.strip():
                    seed = parse_seed(raw)
                    if seed.kind != kind:
                        raise ValueError(f"option {key!r}: {raw.strip()!r} is not a {kind}")
                    seed.explicit = True
                    self._extra_seeds.append(seed)

        self._access: str | None = None
        self._refresh: str | None = None
        self._search_blocked = False
        self._ok_streak = 0
        self._log: list[dict[str, Any]] = []
        self._stats: Counter[str] = Counter()
        self._latency_ms_total = 0.0
        self._send_counter = 0
        self._started = self.http.clock()
        self._wall_started = self._wall_clock()
        self._rng = random.Random(config.seed)
        self._resolved: dict[str, str] = {}
        self._noted: SeedSet | None = None
        #: Repliers-mode tallies, reported in one warning when discovery ends.
        self._replier_stats: Counter[str] = Counter()
        #: The thread each replier was first found in (see the permutation null).
        self._reply_threads: dict[str, str] = {}
        self._replies_per_post: list[int] = []
        self._replier_budget_floor: int | None = None
        self._replier_reported = False
        #: A budget or rate-limit stop met while reading the anchors, raised once the
        #: repliers found before it were handed out.
        self._replier_pending: BudgetExceeded | RateLimited | None = None
        self._replier_stop: str | None = None

        def reset_wait(response: httpx.Response) -> float | None:
            # A 429 without Retry-After: wait until RateLimit-Reset (epoch seconds).
            reset = _header_float(response, "RateLimit-Reset")
            if reset is None:
                return None
            return max(0.0, reset - self._wall_clock()) + RESET_MARGIN

        reset_wait.margin = RESET_MARGIN  # type: ignore[attr-defined]
        self.http.configure(
            reset_wait=reset_wait,
            before_attempt=self._before_attempt,
            on_response=self._on_response,
            on_attempt_failed=self._on_attempt_failed,
            on_attempt_not_sent=self._on_attempt_not_sent,
        )

    # -- options -------------------------------------------------------------------

    def _int_option(self, key: str, default: int, low: int, high: int) -> int:
        value = self.option_int(key, default)
        if value is None or not low <= value <= high:
            raise ValueError(f"option {key!r} must be between {low} and {high}, got {value!r}")
        return value

    def _https_option(self, key: str, default: str) -> str:
        return _https_origin(key, self.option_str(key, default) or default)

    @classmethod
    def host_uses(cls, options: Mapping[str, Any] | None = None) -> tuple[HostUse, ...]:
        """The hosts contacted, with the PDS host taken from ``pds_url`` when it is set."""
        raw = str((options or {}).get("pds_url") or "").strip()
        if not raw:
            return cls.network_hosts
        host = httpx.URL(_https_origin("pds_url", raw)).host
        return tuple(
            HostUse(host, u.purpose, u.credentials, u.tokens, u.login_only)
            if u.host == httpx.URL(DEFAULT_PDS).host
            else u
            for u in cls.network_hosts
        )

    @property
    def logged_in(self) -> bool:
        """True when login credentials were given (a session is made on first use)."""
        return self._login is not None

    # -- instrumentation and rate control -------------------------------------------

    def _before_attempt(self, attempt: Attempt) -> None:
        with self._lock:
            self._send_counter += 1
            self._tls.sent_rps = self._rps
        self._tls.sent_at = time.perf_counter()

    def _endpoint(self, attempt: Attempt) -> str:
        path = urlsplit(str(attempt.url)).path
        # The method name only, for example "getAuthorFeed".
        return path.rsplit(".", 1)[-1] if "/xrpc/" in path else "other"

    def _record(
        self, attempt: Attempt, status: int | None, response: httpx.Response | None
    ) -> None:
        """Append one request row. No handle, DID, query text or token is kept."""
        sent = self._tls.sent_at
        self._tls.sent_at = None
        latency = None if sent is None else (time.perf_counter() - sent) * 1000.0
        params = attempt.params or {}
        window: int | None = None
        since, until = _parse_time(params.get("since")), _parse_time(params.get("until"))
        if since is not None and until is not None:
            window = int((until - since).total_seconds())
        limit = params.get("limit")
        row: dict[str, Any] = {
            "at": isoformat(utcnow()),
            "phase": self._tls.phase,
            "endpoint": self._endpoint(attempt),
            "status": status,
            "latency_ms": None if latency is None else round(latency, 1),
            "page_limit": None if limit is None else str(limit),
            "window_s": window,
            "rps": self._tls.sent_rps,
            "slow_kind": None,
            "attempt": attempt.attempt,
            "wait_s": None,
            "reset_s": None if response is None else _header_float(response, "RateLimit-Reset"),
            "retry_after_s": (
                None if response is None else parse_retry_after(response.headers.get("Retry-After"))
            ),
            "send_seq": None,
        }
        with self._lock:
            row["send_seq"] = self._stats["requests"]
            self._log.append(row)
            self._stats["requests"] += 1
            if latency is not None:
                self._stats["timed"] += 1
                self._latency_ms_total += latency
            if status is None:
                self._stats["failed"] += 1
            elif status in LOAD_STATUSES:
                self._stats["load"] += 1

    def _on_response(self, attempt: Attempt, response: httpx.Response) -> None:
        status = response.status_code
        self._record(attempt, status, response)
        with self._lock:
            if status in LOAD_STATUSES:
                self._ok_streak = 0
                lowered = max(MIN_RPS, self._rps / 2)
                if lowered < self._rps:
                    self._stats["halvings"] += 1
                    self._rps = lowered
                    self.http.adjust_rate(lowered)
            elif status < 400:
                self._ok_streak += 1
                if self._ok_streak >= RECOVERY_STREAK and self._rps < self.max_rps:
                    self._ok_streak = 0
                    self._rps = min(self.max_rps, self._rps + RECOVERY_STEP)
                    self.http.adjust_rate(self._rps)
        remaining = _header_float(response, "RateLimit-Remaining")
        reset = _header_float(response, "RateLimit-Reset")
        if status != 429 and remaining is not None and remaining <= 0 and reset is not None:
            # The window is used up: pause every worker until it resets, instead of
            # sending a request that is bound to be refused.
            wait = min(self.http.max_retry_wait, max(0.0, reset - self._wall_clock()))
            if wait > 0:
                with self._lock:
                    self._stats["reset_holds"] += 1
                self.http.hold(wait + RESET_MARGIN)

    def _on_attempt_failed(self, attempt: Attempt, error: BaseException) -> None:
        del error
        self._record(attempt, None, None)

    def _on_attempt_not_sent(self, attempt: Attempt, error: BaseException) -> None:
        del attempt, error
        self._tls.sent_at = None
        with self._lock:
            self._stats["not_sent"] += 1

    def drain_request_log(self, *, final: bool = False) -> list[dict[str, Any]]:
        """Return the per-request rows recorded since the last call, and forget them."""
        del final
        with self._lock:
            rows, self._log = self._log, []
        return rows

    def request_summary(self) -> dict[str, Any] | None:
        """Totals over every request: counts, latency, rate changes and elapsed time."""
        with self._lock:
            wall = max(0.0, self._wall_clock() - self._wall_started)
            timed = self._stats["timed"]
            return {
                "requests": self._stats["requests"],
                "not_sent": self._stats["not_sent"],
                "failed": self._stats["failed"],
                "load_responses": self._stats["load"],
                "rate_halvings": self._stats["halvings"],
                "reset_holds": self._stats["reset_holds"],
                "session_creates": self._stats["session_creates"],
                "session_refreshes": self._stats["session_refreshes"],
                "mean_latency_ms": round(self._latency_ms_total / timed, 1) if timed else None,
                "elapsed_s": round(max(0.0, self.http.clock() - self._started), 1),
                "wall_elapsed_s": round(wall, 1),
                "observed_rps": round(self._stats["requests"] / wall, 3) if wall > 0 else None,
                "final_rps": self._rps,
                "max_rps": self.max_rps,
                "logged_in": self.logged_in,
            }

    # -- XRPC ----------------------------------------------------------------------

    def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        phase: str,
        authed: bool = False,
        http_method: str = "GET",
        json: Any = None,
        bearer: str | None = None,
        to_pds: bool = False,
    ) -> dict[str, Any]:
        """Send one XRPC request and return its JSON object.

        With ``authed``, ``bearer`` is the session's access token and the request goes
        through the PDS to the AppView. Without it, ``bearer`` (a refresh token) or
        ``to_pds`` sends the request to the PDS; anything else goes to the public
        AppView host, the only host that logged-out reads use.

        Raises :class:`_XrpcError` for an XRPC error answer and :class:`_SearchBlocked`
        for the public host's refusal page on ``searchPosts``.
        """
        self._tls.phase = phase
        headers = {"User-Agent": USER_AGENT}
        if authed:
            assert bearer is not None
            headers["atproto-proxy"] = APPVIEW_PROXY
            headers["Authorization"] = f"Bearer {bearer}"
            base = self.pds
        elif bearer is not None or to_pds:
            if bearer is not None:
                headers["Authorization"] = f"Bearer {bearer}"
            base = self.pds
        else:
            base = self.appview
        response = self.http.request(
            http_method,
            f"{base}/xrpc/{method}",
            params=params,
            headers=headers,
            json=json,
            # Every PDS request carries the login, a session token or both.
            sends_credentials=base == self.pds,
        )
        status = response.status_code
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = None
        if status == 200 and isinstance(body, dict):
            return body
        if status == 200:
            raise SourceError(f"Bluesky {method} returned a body that is not a JSON object")
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            message = body.get("message")
            raise _XrpcError(
                method, status, body["error"], message if isinstance(message, str) else None
            )
        if method == SEARCH_POSTS and status == 403 and not authed:
            raise _SearchBlocked(f"Bluesky {method} returned HTTP 403 without a login")
        if status in (401, 403):
            raise SourceAuthError(f"Bluesky {method} returned HTTP {status}")
        raise SourceError(f"Bluesky {method} returned HTTP {status}")

    def _call_authed(self, method: str, params: dict[str, Any], *, phase: str) -> dict[str, Any]:
        """An authenticated call that refreshes an expired session once.

        The token the failed request carried is the one reported stale, so when
        several workers fail with the same expired token, only the first refreshes
        the session and the others reuse the new token.
        """
        used = self._access_token()
        try:
            return self._call(method, params, phase=phase, authed=True, bearer=used)
        except _XrpcError as exc:
            if exc.error not in ("ExpiredToken", "InvalidToken") and exc.status != 401:
                raise
        self._renew_session(used)
        try:
            return self._call(method, params, phase=phase, authed=True, bearer=self._access_token())
        except _XrpcError as exc:
            if exc.error in ("ExpiredToken", "InvalidToken") or exc.status == 401:
                raise SourceAuthError(
                    "Bluesky refused the session right after it was renewed"
                ) from None
            raise

    # -- session -------------------------------------------------------------------

    def _access_token(self) -> str:
        with self._session_lock:
            if self._access is None:
                self._create_session()
            assert self._access is not None
            return self._access

    def _create_session(self) -> None:
        """Log in with the app password. Never logs or stores the secrets."""
        if self._login is None:
            raise SourceAuthError(SEARCH_NEEDS_LOGIN)
        identifier, password = self._login
        try:
            body = self._call(
                CREATE_SESSION,
                phase="auth",
                http_method="POST",
                to_pds=True,
                json={"identifier": identifier, "password": password},
            )
        except _XrpcError as exc:
            if exc.error == "AuthFactorTokenRequired":
                raise SourceAuthError(
                    "Bluesky asked for an email sign-in code; use an app password "
                    f"in {PASSWORD_ENV} (Settings > Privacy and security > App passwords)"
                ) from None
            if exc.error == "AccountTakedown":
                raise SourceAuthError("the Bluesky login account has been taken down") from None
            raise SourceAuthError(
                f"Bluesky login failed ({exc.error or exc.status}); check {HANDLE_ENV} "
                f"and {PASSWORD_ENV}. An account hosted outside Bluesky (a third-party "
                f"PDS) must also set --source-option pds_url=https://its.pds.example; the "
                f"login was sent to {self.pds}"
            ) from None
        access, refresh = body.get("accessJwt"), body.get("refreshJwt")
        if not isinstance(access, str) or not access:
            raise SourceAuthError("Bluesky login returned no access token")
        self._access = access
        self._refresh = refresh if isinstance(refresh, str) and refresh else None
        with self._lock:
            self._stats["session_creates"] += 1

    def _renew_session(self, stale: str) -> None:
        """Refresh the session (or log in again) unless another thread already did.

        ``stale`` is the access token the failed request carried. When the current
        token differs, another thread has already renewed the session.
        """
        with self._session_lock:
            if self._access is not None and self._access != stale:
                return
            refresh, self._access = self._refresh, None
            if refresh is not None:
                try:
                    body = self._call(
                        REFRESH_SESSION, phase="auth", http_method="POST", bearer=refresh
                    )
                except _XrpcError:
                    body = None
                if body is not None and isinstance(body.get("accessJwt"), str):
                    self._access = body["accessJwt"]
                    new_refresh = body.get("refreshJwt")
                    self._refresh = new_refresh if isinstance(new_refresh, str) else None
                    with self._lock:
                        self._stats["session_refreshes"] += 1
                    return
            self._create_session()

    # -- discovery -------------------------------------------------------------------

    def note_seeds(self, seeds: SeedSet) -> None:
        """Remember the scan's seeds, so :meth:`sampling_frame` can describe the frame."""
        self._noted = seeds

    def _plan_seeds(self, seeds: SeedSet, *, warn: bool) -> list[_Seed]:
        """Parse the seeds, add the option seeds, and keep frames from mixing unasked.

        With a login and the bundled seeds, discovery uses search only: the bundled
        list and feed seeds would add other sampling frames, so they are used only
        when the user sets ``list_mode``. Seeds the user chose (a ``--seeds`` file,
        ``--query``, ``seed_feeds`` or ``seed_lists``) are always used, and
        :meth:`discover` warns when they mix frames. No request is sent.
        """
        plan: list[_Seed] = []
        for item in seeds.items:
            try:
                plan.append(parse_seed(item))
            except ValueError as exc:
                if warn:
                    self.warn(f"Bluesky seed skipped: {exc}.")
        plan += [_Seed(s.label, s.kind, s.uri, explicit=True) for s in self._extra_seeds]
        queries = sum(1 for s in plan if s.kind == "query")
        if self.logged_in and seeds.bundled and queries and not self.list_mode_given:
            unused = {id(s) for s in plan if s.kind != "query" and not s.explicit}
            if unused:
                plan = [s for s in plan if id(s) not in unused]
                if warn:
                    self.warn(
                        "Logged in with the bundled seeds: discovery uses search only, so "
                        "the sampling frame is the accounts with a post in the baseline "
                        f"window that matches one of the {_plural(queries, 'search seed')}. "
                        f"The bundled list and feed seeds ({len(unused)}) were not used. "
                        "Set --source-option list_mode=repliers (or members, or feed) to "
                        "use them as well; the frames are then mixed, and a warning says "
                        "how."
                    )
        return plan

    def _frame_parts(
        self, active: Sequence[_Seed], *, members: int | None = None, lists: int | None = None
    ) -> list[str]:
        """One description per sampling frame that ``active`` draws from."""
        queries = sum(1 for s in active if s.kind == "query")
        feeds = sum(1 for s in active if s.kind == "feed")
        list_seeds = sum(1 for s in active if s.kind == "list")
        if lists is None:
            lists = list_seeds
        parts: list[str] = []
        if queries:
            parts.append(
                "window posters: accounts with a post in the baseline window that matches "
                f"one of {_plural(queries, 'search seed')}"
            )
        as_feeds = list_seeds if self.list_mode == "feed" else 0
        if feeds + as_feeds:
            parts.append(
                "feed posters: accounts with a post in the baseline window in "
                f"{_plural(feeds + as_feeds, 'feed seed')}"
                + (f" ({as_feeds} of them lists read as feeds)" if as_feeds else "")
            )
        if self.list_mode == "repliers" and (lists or any(s.kind == "repliers" for s in active)):
            parts.append(
                "repliers: accounts that replied in the baseline window to posts by "
                f"members of {_plural(lists, 'list')} (the anchor accounts)"
            )
        if self.list_mode == "members" and (lists or any(s.kind == "members" for s in active)):
            size = "" if members is None else f"{members} distinct "
            parts.append(
                f"list members: the {size}members of {_plural(lists, 'list')}, whether or "
                "not they posted in the window"
            )
        return parts

    def sampling_frame(self) -> str | None:
        """A one-line description of the sampling frame, before discovery (no request).

        The CLI prints it instead of its generic line. None when the seeds are not
        known yet or none would be used.
        """
        if self._noted is None:
            return None
        plan = self._plan_seeds(self._noted, warn=False)
        if not self.logged_in and any(s.kind != "query" for s in plan):
            plan = [s for s in plan if s.kind != "query"]
        parts = self._frame_parts(plan)
        if not parts:
            return None
        if len(parts) == 1:
            return (
                f"Sampling frame: {parts[0]}. Sampling is random within this frame, not "
                "among all users."
            )
        return (
            f"The sampling frames are mixed: {'; '.join(parts)}. Each seed takes an equal "
            "share of the pool (see the warning after discovery), so sampling is random "
            "within this blend, not among all users."
        )

    def placebo_discovery_problem(self, seeds: SeedSet) -> str | None:
        """Search (``since`` / ``until``) and list seeds can reach earlier weeks; feed
        generators keep only about a day of posts, so a run with a feed seed cannot
        discover the accounts active in earlier weeks. No request is sent."""
        plan = self._plan_seeds(seeds, warn=False)
        feeds = [
            s for s in plan if s.kind == "feed" or (s.kind == "list" and self.list_mode == "feed")
        ]
        if feeds:
            return (
                f"{_plural(len(feeds), 'Bluesky feed seed')} cannot be read back to earlier "
                "weeks (feed generators keep only about a day of posts), so there is no "
                "placebo sample selected the way the run's own sample was"
            )
        return None

    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        """Yield discovery accounts: window posters from queries and feeds, and the
        repliers to (or the members of) the list seeds.

        Query seeds are searched (``searchPosts``, ``sort=latest``) over a one-day
        slice whose end is drawn from ``[start, cutoff + slice)`` and clamped to the
        cutoff, a fresh draw for every page, so the pool spreads over the whole
        baseline window: the draw is uniform in time, and heavier posters are more
        likely to be drawn. Query and feed seeds yield authors with a post indexed in
        ``[baseline_start, cutoff)``. Search needs a login; with a login and the
        bundled seeds, search is the only frame (see :meth:`_plan_seeds`).

        Feed seeds (and list seeds with ``list_mode=feed``) are read newest first,
        page by page. Pages whose items lie at or after the cutoff (see
        ``PAGE_BULK``) are skipped over without counting as dry, up to
        ``max_pages_ahead`` of them (0 ends the seed at the first such page); after
        that, at most ``feed_pages`` more pages are read, back to the window start.
        A page has reached the window start when its bulk is older than the start,
        so one stray old item (a pinned or boosted post) does not end the seed.

        List seeds are read with ``getList``: every member of every list is
        enumerated (at most ``list_member_pages`` pages of 100 per list) and
        duplicates across lists are dropped. Moderation lists are refused.

        * ``list_mode=repliers`` (the default) treats the members as anchor accounts,
          not as the sample. ``anchor_accounts`` anchors are drawn at random from the
          union; for each, the author feed (own posts, no replies, at most
          ``anchor_feed_pages`` pages) gives its posts in the window that have
          replies, ``anchor_posts_per_account`` of them are drawn at random, and
          ``getPostThread`` (``depth=1``, ``parentHeight=0``) gives their direct
          replies. Reply authors with a reply indexed in the window, at most
          ``thread_reply_limit`` per thread, form the pool; anchors (every list
          member) and opted-out accounts are left out. Every drawn anchor is read
          before any replier is yielded, and the de-duplicated repliers are shuffled,
          so a pool limit cuts a uniform random subset of the whole frame. The frame
          is "accounts that replied in the window to the anchors' posts": busy threads
          contribute more accounts, but each of their repliers is less likely to be
          kept, repliers of prolific anchors are under-represented, and taken-down or
          deleted repliers are never found (threads are read after the cutoff).
          Discovery stops reading anchors once it has used half of the request budget
          that was left when it began.
        * ``list_mode=members`` (opt-in) shuffles the union with ``--seed``, so the
          pool is a uniform random draw from the members, whenever they last posted.
          Its frame is "members of these lists", the prominent accounts themselves.

        Seeds are visited in a fresh random order each round (all list seeds together
        count as one seed), and no seed may supply more than its share of the pool
        while others still have new authors. A seed whose pages yield no new author
        ``dry_pages`` times in a row is done (not the repliers seed, whose anchors
        often have no window posts). A page that fails is retried ``SEED_RETRIES``
        times with a growing pause before its seed is dropped. Feed and list URIs that
        name their owner by handle are resolved to DIDs first, and starter packs to
        their lists, so the same seed given twice is read once. When the seeds draw
        from more than one frame, a warning says which and how they are mixed.
        Randomness comes from ``--seed``.

        Raises:
            SourceAuthError: search needs a login and no feed or list seed is left.
        """
        if pool_limit <= 0:
            return
        start = self.config.baseline_start or self.config.cutoff - timedelta(days=30)
        cutoff = self.config.cutoff
        plan = self._plan_seeds(seeds, warn=True)
        unique: dict[tuple[str, str], _Seed] = {}
        for seed in plan:
            if seed.uri is not None:
                try:
                    seed.uri = self._seed_uri(seed.uri)
                except _FATAL:
                    raise
                except SourceError as exc:
                    self.warn(f"Bluesky seed {seed.label!r} skipped: {exc}.")
                    continue
            unique.setdefault((seed.kind, seed.uri or seed.label), seed)
        active = list(unique.values())
        if not active:
            raise SourceError("no usable Bluesky seeds")
        has_feeds = any(seed.kind != "query" for seed in active)
        if not self.logged_in and any(seed.kind == "query" for seed in active):
            # The public host refuses search without a login: drop the query seeds
            # before any searchPosts request is sent.
            self._drop_query_seeds(active, has_feeds)
        self._warn_feed_depth(active)
        members: int | None = None
        lists_read: int | None = None
        if self.list_mode in ("members", "repliers"):
            lists = [seed for seed in active if seed.kind == "list"]
            if lists:
                active = [seed for seed in active if seed.kind != "list"]
                union, lists_read = self._read_lists(lists, start, cutoff)
                if lists_read:
                    members = len(union)
                    if self.list_mode == "members":
                        active.append(self._member_seed(union, lists_read))
                    else:
                        active.append(self._replier_seed(union, lists_read))
                if not active:
                    raise SourceError("no usable Bluesky seeds: no list could be read")
        cap = -(-pool_limit // len(active))
        self._warn_mixed_frames(active, cap, members=members, lists=lists_read)
        relaxed = False
        seen: set[str] = set()
        yielded = 0
        try:
            while active:
                order = list(active)
                self._rng.shuffle(order)
                if not relaxed and all(s.taken >= cap for s in order):
                    relaxed = True
                for seed in order:
                    if seed.done or (not relaxed and seed.taken >= cap):
                        continue
                    try:
                        rows = self._seed_page(seed, start, cutoff)
                    except _SearchBlocked:
                        self._drop_query_seeds(active, has_feeds)
                        continue
                    new = 0
                    for did, handle, ts, community in rows:
                        if ts is not None and not start <= ts < cutoff:
                            continue
                        seed.in_window += 1
                        if did in seen:
                            continue
                        if not relaxed and seed.taken >= cap:
                            break
                        seen.add(did)
                        seed.taken += 1
                        new += 1
                        yielded += 1
                        if yielded >= pool_limit:
                            # The caller stops pulling after this account.
                            self._report_repliers()
                        meta = {"community": community or seed.label}
                        thread = self._reply_threads.get(did)
                        if thread:
                            meta["discovery_thread"] = thread
                        yield Account(self.name, did, handle, None, meta)
                        if yielded >= pool_limit:
                            return
                    # A page entirely after the cutoff is not dry: the window is still
                    # ahead. Such pages are capped by max_pages_ahead instead. The
                    # repliers seed reads one anchor per page, and many anchors have no
                    # post in the window, so it ends only when its anchors run out.
                    if new:
                        seed.dry = 0
                    elif not seed.last_ahead and seed.kind != "repliers":
                        seed.dry += 1
                    if seed.dry >= self.dry_pages and not seed.done:
                        seed.done = True
                        seed.end = "dry"
                    if seed.done and seed in active:
                        self._finish_seed(seed, start)
                        active.remove(seed)
            if yielded < pool_limit:
                self.warn(
                    f"Bluesky discovery found only {_plural(yielded, 'distinct account')} "
                    f"(wanted {pool_limit}); the seeds ran out of new accounts (see the "
                    "warnings about each seed for the reason)."
                )
        finally:
            self._report_repliers()

    def _warn_mixed_frames(
        self, active: Sequence[_Seed], cap: int, *, members: int | None, lists: int | None
    ) -> None:
        """Say exactly how the pool mixes frames, when the seeds draw from several."""
        parts = self._frame_parts(active, members=members, lists=lists or 0)
        if len(parts) < 2:
            return
        grouped = any(s.kind in ("members", "repliers") for s in active)
        self.warn(
            f"The pool mixes {len(parts)} sampling frames: {'; '.join(parts)}. Discovery "
            f"visits the {_plural(len(active), 'seed')} in turn and lets each take at most "
            f"{cap} accounts (an equal share of the pool) while others still have new "
            "accounts"
            + ("; all list seeds together count as one seed" if grouped else "")
            + ". The mix therefore follows the number of seeds, not the size of each "
            "frame, and the results describe this blend, not any one frame."
        )

    def _warn_feed_depth(self, active: Sequence[_Seed]) -> None:
        """Warn up front when feed seeds are unlikely to reach back to the cutoff."""
        feeds = [
            s for s in active if s.kind == "feed" or (s.kind == "list" and self.list_mode == "feed")
        ]
        behind = self.config.now - self.config.cutoff
        if not feeds or behind <= FEED_HISTORY:
            return
        self.warn(
            f"{_plural(len(feeds), 'Bluesky feed seed')} "
            f"{'is' if len(feeds) == 1 else 'are'} read {behind / _DAY:.1f} days after the "
            "cutoff, but feed generators usually keep only about a day of posts, so "
            f"{'this feed' if len(feeds) == 1 else 'these feeds'} will probably not reach "
            "the baseline window and find almost no one. Use list seeds instead (the "
            "bluesky.lists entries in config/seeds.yaml, or --source-option "
            "seed_lists=at://...): the default list_mode=repliers samples the accounts "
            "that replied to the lists' members in the window."
        )

    def _read_lists(
        self, lists: Sequence[_Seed], start: datetime, cutoff: datetime
    ) -> tuple[dict[str, tuple[str, str, str]], int]:
        """Enumerate the members of ``lists``: ``({did: (handle, label, name)}, read)``.

        Each list is read to its end (or ``list_member_pages`` pages). Members are
        de-duplicated by DID, keeping the first list that names them; opted-out
        accounts are left out. ``read`` counts the lists that gave members, including
        one that failed partway, which is counted and warned about: its unread members
        cannot be drawn, so the draw is not uniform over that whole list.
        """
        union: dict[str, tuple[str, str, str]] = {}
        read = 0
        for seed in lists:
            got = 0
            while not seed.done:
                for did, handle, _ts, _community in self._seed_page(seed, start, cutoff):
                    got += 1
                    union.setdefault(did, (handle, seed.label, seed.name or seed.label))
            if not seed.failed:
                read += 1
            elif seed.pages:
                read += 1
                self.warn(
                    f"Bluesky list {seed.label!r} was only partly read: it failed after "
                    f"{_plural(seed.pages, 'page')} ({_plural(got, 'member')} read). It "
                    "is counted as read, but its unread members cannot be drawn, so the "
                    "draw is not uniform over the whole list."
                )
        return union, read

    def _member_seed(self, union: dict[str, tuple[str, str, str]], read: int) -> _Seed:
        """The list members as one seed, shuffled (``list_mode=members``).

        The union is sorted by DID and then shuffled with the scan's random generator,
        so a given ``--seed`` draws the same pool whatever order the lists are given in.
        Each member counts once, however many lists name it.
        """
        rows: list[_Row] = [(did, handle, None, label) for did, (handle, label, _) in union.items()]
        rows.sort(key=lambda row: row[0])
        self._rng.shuffle(rows)
        members = _Seed(f"members of {_plural(read, 'Bluesky list')}", "members", queue=rows)
        self.warn(
            "Bluesky list seeds (list_mode=members, the prominent-accounts frame): the "
            f"sampling frame is the {_plural(len(rows), 'distinct member')} of "
            f"{_plural(read, 'list')}, drawn uniformly at random, not the accounts that "
            "posted in the baseline window. Members who were not regular posters before "
            f"the cutoff are counted as not eligible. {NO_BOT_ESTIMATE}"
        )
        return members

    def _replier_seed(self, union: dict[str, tuple[str, str, str]], read: int) -> _Seed:
        """The repliers to the list members' posts, as one seed (``list_mode=repliers``).

        ``anchor_accounts`` anchors are drawn from the union sorted by DID, so a given
        ``--seed`` draws the same anchors whatever order the lists are given in. Each
        anchor carries the name of the first list that names it, which becomes the
        community of the repliers found through it.
        """
        dids = sorted(union)
        chosen = self._rng.sample(dids, min(self.anchor_accounts, len(dids)))
        seed = _Seed(
            f"repliers to members of {_plural(read, 'Bluesky list')}",
            "repliers",
            anchors=[(did, union[did][2]) for did in chosen],
            members=frozenset(union),
        )
        remaining = self.http.requests_remaining
        if remaining is not None:
            self._replier_budget_floor = remaining - int(remaining * REPLIERS_BUDGET_SHARE)
        self._replier_stats["lists"] = read
        self._replier_stats["members"] = len(union)
        self._replier_stats["anchors_drawn"] = len(chosen)
        self.warn(
            "Bluesky list seeds (list_mode=repliers): the sampling frame is the accounts "
            "that replied in the baseline window to posts by members of "
            f"{_plural(read, 'list')}. The {_plural(len(union), 'distinct member')} are "
            f"anchors, not the sample: {len(chosen)} of them were drawn at random, up to "
            f"{self.anchor_posts} of each one's window posts with replies are read, and up "
            f"to {self.thread_reply_limit} reply authors per thread are kept. An account's "
            "chance of entering the pool is roughly the sum, over the threads it replied "
            "to, of (posts drawn from that anchor / that anchor's window posts) times "
            f"min(1, {self.thread_reply_limit} / that thread's repliers): busy threads "
            "contribute more accounts, but each of their repliers is less likely to be "
            "drawn, and repliers of prolific anchors are under-represented. The frame "
            "leans toward accounts that reply to prominent political accounts. Threads "
            "are read after the cutoff, so repliers who were taken down or deleted their "
            "accounts (or replies) by then are never discovered: suspended and deleted "
            "counts are near 0 by design, while cliffs (silent but still present) are "
            "measurable. Anchors and opted-out accounts are never pooled."
        )
        return seed

    def _replier_budget_spent(self) -> bool:
        """True once repliers discovery has used its share of ``--max-requests``."""
        floor = self._replier_budget_floor
        if floor is None:
            return False
        remaining = self.http.requests_remaining
        return remaining is not None and remaining <= floor

    def _replier_page(self, seed: _Seed, start: datetime, cutoff: datetime) -> list[_Row]:
        """One page of the repliers seed, handed out from its shuffled pool.

        The first call reads every drawn anchor and its threads (see
        :meth:`_collect_repliers`); this call and later ones hand out ``page_size``
        rows at a time. Reading the whole frame before handing anything out keeps the
        pool a uniform draw from it: a pool limit then cuts a random subset of all the
        repliers found, not the repliers of the first few anchors. A budget or
        rate-limit stop while reading is raised only after the repliers read before it
        were handed out.
        """
        if seed.queue is None:
            seed.queue = self._collect_repliers(seed, start, cutoff)
        if not seed.queue and self._replier_pending is not None:
            pending, self._replier_pending = self._replier_pending, None
            seed.done = True
            seed.end = "exhausted"
            raise pending
        chunk, seed.queue = seed.queue[: self.page_size], seed.queue[self.page_size :]
        if not seed.queue and self._replier_pending is None:
            seed.done = True
            seed.end = "exhausted"
        return chunk

    def _collect_repliers(self, seed: _Seed, start: datetime, cutoff: datetime) -> list[_Row]:
        """Read every drawn anchor's threads: the de-duplicated repliers, shuffled.

        Anchors are read until they run out or discovery has used its share of the
        request budget. A budget or rate-limit error stops the reading; it is kept and
        raised by :meth:`_replier_page` once the repliers found so far were handed out.
        The rows are sorted by DID and then shuffled with the scan's random generator,
        so a given ``--seed`` gives the same order. Each replier keeps the community of
        the first thread that named it.
        """
        stats = self._replier_stats
        anchors = seed.anchors or []
        threads: list[list[_Row]] = []
        try:
            while anchors and not stats["budget_stop"]:
                if self._replier_budget_spent():
                    stats["budget_stop"] = 1
                    break
                did, community = anchors.pop(0)
                seed.pages += 1
                self._read_anchor(seed, did, community, start, cutoff, threads)
        except (BudgetExceeded, RateLimited) as exc:
            self._replier_pending = exc
            self._replier_stop = str(exc)
        if stats["budget_stop"] or self._replier_pending is not None:
            stats["anchors_unread"] = len(anchors)
        anchors.clear()
        pooled: dict[str, _Row] = {}
        for rows in threads:
            for row in rows:
                pooled.setdefault(row[0], row)
        stats["pool_found"] = len(pooled)
        stats["threads_contributing"] = sum(1 for rows in threads if rows)
        stats["largest_thread"] = max((len(rows) for rows in threads), default=0)
        found = sorted(pooled.values(), key=lambda row: row[0])
        self._rng.shuffle(found)
        return found

    def _read_anchor(
        self,
        seed: _Seed,
        did: str,
        community: str,
        start: datetime,
        cutoff: datetime,
        threads: list[list[_Row]],
    ) -> None:
        """Read one anchor's window posts, then the direct replies to a few.

        Each thread read appends its rows to ``threads`` at once, so the threads read
        before a fatal error are kept. An anchor whose feed cannot be read, or a thread
        that cannot be read, is skipped and counted; fatal errors are raised. Each
        thread keeps at most ``thread_reply_limit`` distinct reply authors, drawn at
        random.
        """
        stats = self._replier_stats
        try:
            posts = self._anchor_posts(did, start, cutoff)
        except _FATAL:
            raise
        except SourceError:
            stats["anchors_failed"] += 1
            return
        stats["anchors_read"] += 1
        stats["window_posts"] += len(posts)
        if not posts:
            stats["anchors_without_posts"] += 1
            return
        picked = self._rng.sample(posts, min(self.anchor_posts, len(posts)))
        for uri in picked:
            if self._replier_budget_spent():
                stats["budget_stop"] = 1
                break
            try:
                replies = self._direct_replies(uri)
            except _FATAL:
                raise
            except SourceError:
                stats["threads_failed"] += 1
                continue
            stats["threads_read"] += 1
            self._replies_per_post.append(len(replies))
            for author, *_rest in replies:
                # The permutation null keeps accounts found in one thread together.
                self._reply_threads.setdefault(author, uri)
            kept: dict[str, _Row] = {}
            for author, handle, ts, opted_out in replies:
                if not start <= ts < cutoff:
                    continue
                stats["replies_in_window"] += 1
                if author in seed.members:
                    stats["replies_by_anchors"] += 1
                elif opted_out:
                    stats["replies_opted_out"] += 1
                else:
                    kept.setdefault(author, (author, handle, ts, community))
            chosen = sorted(kept.values(), key=lambda row: row[0])
            self._rng.shuffle(chosen)
            threads.append(chosen[: self.thread_reply_limit])

    def _anchor_posts(self, did: str, start: datetime, cutoff: datetime) -> list[str]:
        """URIs of the anchor's own posts in ``[start, cutoff)`` that may have replies.

        Reads the author feed (``filter=posts_no_replies``) newest first until a post
        is older than ``start``, the feed ends, or ``anchor_feed_pages`` pages were
        read. A post counts when both its ``sortAt`` and its ``indexedAt`` are in the
        window; one whose ``replyCount`` is 0 is skipped, saving a thread request.
        Reposts and pinned posts are not the anchor's own window posts and are skipped.
        """
        found: list[str] = []
        cursor: str | None = None
        for _page in range(self.anchor_feed_pages):
            params: dict[str, Any] = {
                "actor": did,
                "limit": self.page_size,
                "filter": "posts_no_replies",
            }
            if cursor:
                params["cursor"] = cursor
            body = self._call(GET_AUTHOR_FEED, params, phase="discovery")
            items = [i for i in body.get("feed") or [] if isinstance(i, dict)]
            reached = False
            for item in items:
                if item.get("reason") is not None:
                    continue
                post = item.get("post")
                key = self._sort_key(post)
                if key is None or not isinstance(post, dict):
                    continue
                if key < start:
                    reached = True
                author = post.get("author")
                if not isinstance(author, dict) or author.get("did") != did:
                    continue
                indexed = _parse_time(post.get("indexedAt"))
                uri = post.get("uri")
                if indexed is None or not isinstance(uri, str):
                    continue
                if start <= key < cutoff and start <= indexed < cutoff:
                    if post.get("replyCount") == 0:
                        self._replier_stats["posts_without_replies"] += 1
                    else:
                        found.append(uri)
            next_cursor = body.get("cursor")
            if reached or not items or not isinstance(next_cursor, str) or not next_cursor:
                return found
            cursor = next_cursor
        self._replier_stats["anchors_page_capped"] += 1
        return found

    def _direct_replies(self, uri: str) -> list[tuple[str, str, datetime, bool]]:
        """The direct replies to ``uri``: ``(did, handle, indexedAt, opted_out)`` each.

        One ``getPostThread`` with ``depth=1`` and ``parentHeight=0``. Replies that
        are not plain thread views (deleted or blocked posts) are skipped.
        """
        try:
            body = self._call(
                GET_POST_THREAD,
                {"uri": uri, "depth": 1, "parentHeight": 0},
                phase="discovery",
            )
        except _XrpcError as exc:
            raise SourceError(f"the thread could not be read ({exc.error})") from None
        thread = body.get("thread")
        if not isinstance(thread, dict) or thread.get("$type", THREAD_VIEW) != THREAD_VIEW:
            return []
        out: list[tuple[str, str, datetime, bool]] = []
        for reply in thread.get("replies") or []:
            if not isinstance(reply, dict) or reply.get("$type", THREAD_VIEW) != THREAD_VIEW:
                continue
            post = reply.get("post")
            author = post.get("author") if isinstance(post, dict) else None
            if not isinstance(author, dict):
                continue
            did, handle = author.get("did"), author.get("handle")
            ts = _parse_time(post.get("indexedAt")) if isinstance(post, dict) else None
            if not isinstance(did, str) or not did.startswith("did:") or ts is None:
                continue
            name = handle if isinstance(handle, str) and handle else did
            out.append((did, name, ts, _opted_out(author)))
        return out

    def _report_repliers(self) -> None:
        """Warn once with the repliers-mode tallies (numbers only, no account)."""
        stats = self._replier_stats
        if self._replier_reported or "anchors_drawn" not in stats:
            return
        self._replier_reported = True
        counts = self._replies_per_post
        if counts:
            ordered = sorted(counts)
            spread = (
                f"min {ordered[0]}, median {ordered[len(ordered) // 2]}, max {ordered[-1]} "
                f"(each thread: {', '.join(str(c) for c in counts)})"
            )
        else:
            spread = "none read"
        unread = stats["anchors_drawn"] - stats["anchors_read"] - stats["anchors_failed"]
        found = stats["pool_found"]
        share = f"{stats['largest_thread'] / found:.0%} of the frame" if found else "none found"
        text = (
            f"Bluesky repliers discovery: {stats['anchors_read']} of "
            f"{_plural(stats['anchors_drawn'], 'anchor')} read ({stats['anchors_failed']} "
            f"failed, {unread} not needed or not reached); "
            f"{stats['anchors_without_posts']} had no window post with replies, and "
            f"{stats['anchors_page_capped']} reached the anchor_feed_pages limit before the "
            "window start (their posts lean toward the cutoff, so their repliers are drawn "
            "from closer to it, which nudges the cliff share up). "
            f"{stats['window_posts']} "
            f"window posts with replies found ({stats['posts_without_replies']} without "
            f"replies skipped); {_plural(stats['threads_read'], 'thread')} read "
            f"({stats['threads_failed']} failed). Direct replies per thread: {spread}. "
            f"{stats['replies_in_window']} replies were in the window; "
            f"{stats['replies_by_anchors']} by anchors and {stats['replies_opted_out']} by "
            "opted-out accounts were left out. "
            f"{_plural(stats['threads_contributing'], 'thread')} contributed the "
            f"{_plural(stats['pool_found'], 'distinct replier')} found; the largest gave "
            f"{stats['largest_thread']} of them ({share}). The pool is a random subset of "
            "these repliers when it is smaller."
        )
        if stats["budget_stop"]:
            text += (
                f" Discovery stopped reading anchors after reading {stats['anchors_read']} "
                f"of the {stats['anchors_drawn']} drawn, having used half of the request "
                "budget left when it began, to keep the rest for scoring; the pool comes "
                "from those anchors only. Raise --max-requests for a fuller frame."
            )
        if self._replier_stop is not None:
            text += (
                f" Discovery stopped after reading {stats['anchors_read']} of the "
                f"{stats['anchors_drawn']} drawn anchors ({self._replier_stop}); the pool "
                "comes from those anchors only."
            )
        self.warn(text)

    def _seed_page(self, seed: _Seed, start: datetime, cutoff: datetime) -> list[_Row]:
        """One discovery page of ``seed``, retried after a transient error.

        A plain :class:`SourceError` is retried ``SEED_RETRIES`` times in a row, after
        ``SEED_RETRY_BACKOFF`` seconds and then twice as long each time; after that
        the seed is dropped with a warning and no rows. A refused seed (a moderation
        list) is dropped at once. Fatal errors and a blocked search are raised.
        """
        while True:
            try:
                rows = self._discovery_page(seed, start, cutoff)
            except (*_FATAL, _SearchBlocked):
                raise
            except _RefusedSeed as exc:
                self.warn(f"Bluesky seed {seed.label!r} was refused: {exc}.")
                seed.done = seed.failed = True
                return []
            except SourceError as exc:
                seed.errors += 1
                if seed.errors > SEED_RETRIES:
                    self.warn(
                        f"Bluesky seed {seed.label!r} was dropped from discovery after "
                        f"{seed.errors} failed attempts in a row (last: {exc}); the pool "
                        "comes from the other seeds."
                    )
                    seed.done = seed.failed = True
                    return []
                self.http.sleep(SEED_RETRY_BACKOFF * 2 ** (seed.errors - 1))
                continue
            # The retry budget is for consecutive failures: a page that worked
            # starts the count again.
            seed.errors = 0
            return rows

    def _drop_query_seeds(self, active: list[_Seed], has_feeds: bool) -> None:
        """Search needs a login: go on with feed and list seeds only.

        Called before discovery when no login was given, so no search is sent, and
        again if a search is refused anyway.

        Raises :class:`SourceAuthError` only when the scan has no feed or list seed at
        all; feeds that were already read to the end still supplied the pool.
        """
        self._search_blocked = True
        queries = [s for s in active if s.kind == "query"]
        for seed in queries:
            active.remove(seed)
        if not has_feeds:
            raise SourceAuthError(SEARCH_NEEDS_LOGIN)
        self.warn(
            f"{_plural(len(queries), 'Bluesky search seed')} "
            f"{'was' if len(queries) == 1 else 'were'} skipped: post search needs a login "
            f"({HANDLE_ENV} and {PASSWORD_ENV}). The pool comes from feed and list seeds only."
        )

    def _finish_seed(self, seed: _Seed, start: datetime) -> None:
        """Warn when a seed contributed nothing or only part of the window, with the cause."""
        if seed.failed or seed.kind in ("members", "repliers"):
            return
        cutoff = self.config.cutoff
        what = "seed" if seed.kind == "query" else seed.kind
        name = f"Bluesky {what} {seed.label!r}"
        pages = _plural(seed.pages, "page")
        if seed.in_window == 0:
            if seed.kind == "query":
                cause = "no items in window: search found no posts in the baseline window"
            elif seed.end == "exhausted" and (seed.oldest is None or seed.oldest >= cutoff):
                cause = (
                    "feed history ends before the window (the feed only keeps recent posts): "
                    f"its oldest post is from {_fmt_time(seed.oldest)}, after the cutoff. "
                    "Use list seeds for a cutoff more than a day back"
                )
            elif seed.end in ("ahead_cap", "page_cap"):
                cause = (
                    f"page limit reached: {pages} were read (oldest post "
                    f"{_fmt_time(seed.oldest)}) without reaching the window before the "
                    "cutoff. Raise --source-option max_pages_ahead or use list seeds"
                )
            else:
                cause = "no items in window: its posts skip from after the cutoff to before it"
            self.warn(f"{name} contributed no accounts: {cause}.")
        elif seed.kind != "query" and not seed.reached_start:
            if seed.end == "exhausted":
                cause = (
                    "feed history ends inside the window (the feed only keeps recent posts; "
                    f"oldest post {_fmt_time(seed.oldest)})"
                )
            elif seed.end in ("ahead_cap", "page_cap"):
                cause = f"page limit reached after {pages}"
            else:
                cause = f"no new authors in {_plural(self.dry_pages, 'page')} in a row"
            self.warn(
                f"{name} did not reach back to the window start ({isoformat(start)}): "
                f"{cause}; its authors lean toward the days just before the cutoff."
            )

    def _discovery_page(self, seed: _Seed, start: datetime, cutoff: datetime) -> list[_Row]:
        """One page of rows for ``seed``."""
        seed.last_ahead = False
        if seed.kind == "members":
            queue = seed.queue or []
            chunk, seed.queue = queue[: self.page_size], queue[self.page_size :]
            seed.pages += 1
            if not seed.queue:
                seed.done = True
                seed.end = "exhausted"
            return chunk
        if seed.kind == "repliers":
            return self._replier_page(seed, start, cutoff)
        if seed.kind == "query":
            if self._search_blocked and not self.logged_in:
                raise _SearchBlocked("search needs a login")
            # The slice end is drawn from [start, cutoff + slice) and clamped to the
            # cutoff: every moment of the window is then inside the slice equally
            # often (drawing from [start, cutoff) would under-sample the final day).
            span = (cutoff - start + DISCOVERY_SLICE).total_seconds()
            until = start + timedelta(seconds=self._rng.uniform(0.0, span))
            until = max(start + timedelta(seconds=1), min(cutoff, until))
            since = max(start, until - DISCOVERY_SLICE)
            params: dict[str, Any] = {
                "q": seed.label,
                "sort": "latest",
                "since": _iso_z(since),
                "until": _iso_z(until),
                "limit": self.page_size,
            }
            if self.lang:
                params["lang"] = self.lang
            if self.logged_in:
                body = self._call_authed(SEARCH_POSTS, params, phase="discovery")
            else:
                body = self._call(SEARCH_POSTS, params, phase="discovery")
            posts = body.get("posts") or []
            seed.pages += 1
            return [(*row, None) for post in posts if (row := self._author_row(post))]
        assert seed.uri is not None
        uri = self._resolve_uri(seed.uri)
        if seed.kind == "list" and self.list_mode != "feed":
            return self._member_page(seed, uri)
        # A feed, or a list read as a feed: page newest first.
        method, key = (GET_FEED, "feed") if seed.kind == "feed" else (GET_LIST_FEED, "list")
        params = {key: uri, "limit": self.page_size}
        if seed.cursor:
            params["cursor"] = seed.cursor
        try:
            body = self._call(method, params, phase="discovery")
        except _XrpcError as exc:
            raise SourceError(f"the {seed.kind} could not be read ({exc.error})") from None
        seed.pages += 1
        items = body.get("feed") or []
        rows: list[_Row] = []
        times: list[datetime] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            post = item.get("post")
            sort_key = self._sort_key(post)
            if sort_key is not None:
                times.append(sort_key)
            row = self._author_row(post)
            if row is not None:
                rows.append((*row, None))
        if times:
            oldest = min(times)
            seed.oldest = oldest if seed.oldest is None else min(seed.oldest, oldest)
        bulk = _bulk_times(times)
        if bulk is not None:
            low, high = bulk
            seed.last_ahead = low >= cutoff
            if high < start:
                seed.reached_start = True
        if seed.last_ahead:
            seed.ahead += 1
        cursor = body.get("cursor")
        seed.cursor = cursor if isinstance(cursor, str) and cursor else None
        if seed.reached_start:
            seed.end = "start"
        elif seed.cursor is None or not items:
            seed.end = "exhausted"
        elif seed.last_ahead and seed.ahead >= self.max_pages_ahead:
            seed.end = "ahead_cap"
        elif seed.pages - seed.ahead >= self.feed_pages:
            seed.end = "page_cap"
        if seed.end is not None:
            seed.done = True
        return rows

    def _member_page(self, seed: _Seed, uri: str) -> list[_Row]:
        """One ``getList`` page of the list's members (the first page checks its purpose)."""
        params: dict[str, Any] = {"list": uri, "limit": MAX_PAGE}
        if seed.cursor:
            params["cursor"] = seed.cursor
        try:
            body = self._call(GET_LIST, params, phase="discovery")
        except _XrpcError as exc:
            raise SourceError(f"the list could not be read ({exc.error})") from None
        if seed.pages == 0:
            view = body.get("list")
            purpose = view.get("purpose") if isinstance(view, dict) else None
            if purpose == MODLIST_PURPOSE:
                raise _RefusedSeed(
                    "it is a moderation (mute or block) list, whose members were picked for "
                    "being reported or blocked; that would bias the sample toward "
                    "suspensions. Use curated lists"
                )
            if purpose not in MEMBER_LIST_PURPOSES:
                raise _RefusedSeed(f"it is not a curated list (purpose {purpose!r})")
            name = view.get("name") if isinstance(view, dict) else None
            seed.name = name.strip() if isinstance(name, str) and name.strip() else None
        seed.pages += 1
        items = body.get("items") or []
        rows: list[_Row] = []
        for item in items:
            subject = item.get("subject") if isinstance(item, dict) else None
            if not isinstance(subject, dict):
                continue
            did, handle = subject.get("did"), subject.get("handle")
            if not isinstance(did, str) or not did.startswith("did:"):
                continue
            if _opted_out(subject):
                continue  # Asked not to be shown to logged-out viewers: never pooled.
            rows.append((did, handle if isinstance(handle, str) and handle else did, None, None))
        cursor = body.get("cursor")
        seed.cursor = cursor if isinstance(cursor, str) and cursor else None
        if seed.cursor is None or not items:
            seed.done = True
            seed.end = "exhausted"
        elif seed.pages >= self.list_member_pages:
            seed.done = True
            seed.end = "page_cap"
            self.warn(
                f"Bluesky list {seed.label!r} has more members than "
                f"{_plural(seed.pages, 'page')} of {MAX_PAGE}; only those were read, so the "
                "draw is uniform over the members read, not the whole list (raise "
                "--source-option list_member_pages)."
            )
        return rows

    def _seed_uri(self, uri: str) -> str:
        """The seed's AT-URI with its owner as a DID; a starter pack becomes its list."""
        uri = self._resolve_uri(uri)
        _did, _, rest = uri[len("at://") :].partition("/")
        if not rest.startswith(STARTERPACK_COLLECTION + "/"):
            return uri
        try:
            body = self._call(GET_STARTER_PACK, {"starterPack": uri}, phase="discovery")
        except _XrpcError as exc:
            raise SourceError(f"the starter pack could not be read ({exc.error})") from None
        pack = body.get("starterPack")
        listed = pack.get("list") if isinstance(pack, dict) else None
        list_uri = listed.get("uri") if isinstance(listed, dict) else None
        if not isinstance(list_uri, str) or f"/{LIST_COLLECTION}/" not in list_uri:
            raise SourceError("the starter pack names no list")
        return self._resolve_uri(list_uri)

    def _resolve_uri(self, uri: str) -> str:
        """Replace a handle authority in an AT-URI with the DID (one cached lookup)."""
        authority, _, rest = uri[len("at://") :].partition("/")
        if authority.startswith("did:"):
            return uri
        did = self._resolved.get(authority)
        if did is None:
            try:
                body = self._call(GET_PROFILE, {"actor": authority}, phase="discovery")
            except _XrpcError as exc:
                raise SourceError(f"the seed's owner could not be resolved ({exc.error})") from None
            found = body.get("did")
            if not isinstance(found, str) or not found.startswith("did:"):
                raise SourceError("the seed's owner could not be resolved")
            did = self._resolved[authority] = found
        return f"at://{did}/{rest}"

    @staticmethod
    def _author_row(post: object) -> tuple[str, str, datetime] | None:
        if not isinstance(post, dict):
            return None
        author = post.get("author")
        if not isinstance(author, dict):
            return None
        did, handle = author.get("did"), author.get("handle")
        ts = _parse_time(post.get("indexedAt"))
        if not isinstance(did, str) or not did.startswith("did:") or ts is None:
            return None
        if _opted_out(author):
            return None  # Asked not to be shown to logged-out viewers: never pooled.
        return did, handle if isinstance(handle, str) and handle else did, ts

    @staticmethod
    def _sort_key(post: object) -> datetime | None:
        """The AppView's ``sortAt`` of a post: ``min(indexedAt, createdAt)``."""
        if not isinstance(post, dict):
            return None
        indexed = _parse_time(post.get("indexedAt"))
        record = post.get("record")
        created = _parse_time(record.get("createdAt")) if isinstance(record, dict) else None
        if indexed is None:
            return created
        return indexed if created is None else min(indexed, created)

    # -- accounts -------------------------------------------------------------------

    def _unavailable(self, exc: _XrpcError) -> AccountUnavailable | None:
        if exc.error == "AccountTakedown":
            return AccountUnavailable("suspended", "Bluesky reports the account as suspended")
        if exc.error == "AccountDeactivated":
            return AccountUnavailable("deleted", "Bluesky reports the account as deactivated")
        if (
            exc.status == 400
            and exc.error == "InvalidRequest"
            and "not found" in exc.message.lower()
        ):
            return AccountUnavailable("deleted", "Bluesky reports no such profile")
        return None

    def _profile(self, account: Account, phase: str) -> dict[str, Any]:
        """Read the profile; an unavailable account raises :class:`AccountUnavailable`.

        ``meta["deactivated"]`` is set for a deactivated account and cleared otherwise,
        so an account that came back is no longer shown as deactivated.
        """
        try:
            profile = self._call(GET_PROFILE, {"actor": account.user_id}, phase=phase)
        except _XrpcError as exc:
            unavailable = self._unavailable(exc)
            if unavailable is not None:
                if exc.error == "AccountDeactivated":
                    account.meta["deactivated"] = True
                else:
                    account.meta.pop("deactivated", None)
                raise unavailable from None
            raise
        account.meta.pop("deactivated", None)
        return profile

    @staticmethod
    def _withhold(account: Account) -> AccountUnavailable:
        """Forget an opted-out account's handle, creation time and meta; return the
        error to raise.

        The account keeps only its DID and the ``opted_out`` flag, so nothing that
        identifies it beyond the DID is stored, and the core never lists it.
        """
        account.handle = account.user_id
        account.created_at = None
        account.meta.clear()
        account.meta["opted_out"] = True
        return AccountUnavailable(
            STATUS_OPTED_OUT, "the account asked not to be shown to logged-out viewers"
        )

    def live_profile_status(self, account: Account) -> str | None:
        """``active``, ``suspended`` or ``deleted``, from one logged-out
        ``app.bsky.actor.getProfile`` on the public AppView (by DID).

        A deactivated account counts as ``deleted`` (it may come back); an account that
        opted out of logged-out views still exists, so it is ``active``.
        """
        try:
            self._profile(account, "live_check")
        except AccountUnavailable as exc:
            return "suspended" if exc.status == "suspended" else "deleted"
        return "active"

    def hydrate(self, account: Account) -> Account:
        """Fill ``created_at`` from the profile's ``createdAt`` (one ``getProfile``).

        No request when ``created_at`` is set or the profile was already read. Raises
        :class:`AccountUnavailable` for a suspended, deactivated or deleted account, and
        as ``opted_out`` for one labelled ``!no-unauthenticated`` (it asked not to be
        shown to logged-out viewers, so its activity is not read and its handle is
        replaced by its DID before the error is raised).
        """
        if account.created_at is not None or account.meta.get("profile_read"):
            return account
        profile = self._profile(account, "hydrate")
        if _opted_out(profile):
            raise self._withhold(account)
        account.created_at = _parse_time(profile.get("createdAt"))
        handle = profile.get("handle")
        if isinstance(handle, str) and handle and handle != "handle.invalid":
            account.handle = handle
        posts = profile.get("postsCount")
        if isinstance(posts, int):
            account.meta["posts_count"] = posts
        account.meta["profile_read"] = True
        return account

    def _activity(
        self, account: Account, item: dict[str, Any]
    ) -> tuple[Activity | None, datetime | None]:
        """Map one author-feed item to ``(activity or None, its sortAt)``.

        A repost the adapter does not count (``include_reposts`` off) comes back as
        ``(None, ts)`` and is tallied in ``self._tls.reposts`` for the caller.

        Raises:
            AccountUnavailable: ``opted_out``, when the account's own profile view in
                the item carries the ``!no-unauthenticated`` label. A cached or
                resumed account is not hydrated again, so this is where a label added
                since then is noticed.
        """
        post = item.get("post")
        reason = item.get("reason")
        if isinstance(reason, dict) and reason.get("$type") == REPOST_REASON:
            ts = _parse_time(reason.get("indexedAt"))
            by = reason.get("by")
            mine = isinstance(by, dict) and by.get("did") == account.user_id
            if mine and _opted_out(by):
                raise self._withhold(account)
            if mine and ts is not None and not self.include_reposts:
                self._tls.reposts.append(ts)
            if not (self.include_reposts and mine and ts is not None):
                return None, ts
            return Activity(self.name, account.user_id, ts, "repost"), ts
        if isinstance(reason, dict):
            return None, None  # A pinned post, or a reason this adapter does not know.
        if not isinstance(post, dict):
            return None, None
        sort_key = self._sort_key(post)
        author = post.get("author")
        ts = _parse_time(post.get("indexedAt"))
        if not isinstance(author, dict) or author.get("did") != account.user_id:
            return None, sort_key
        if _opted_out(author):
            raise self._withhold(account)
        if ts is None:
            return None, sort_key
        raw_record = post.get("record")
        record: dict[str, Any] = raw_record if isinstance(raw_record, dict) else {}
        raw_embed = record.get("embed")
        embed: dict[str, Any] = raw_embed if isinstance(raw_embed, dict) else {}
        if record.get("reply"):
            kind = "reply"
        elif embed.get("$type") in QUOTE_EMBEDS:
            kind = "quote"
        else:
            kind = "post"
        text = record.get("text")
        digest = content_hash(text) if isinstance(text, str) and text.strip() else None
        return Activity(self.name, account.user_id, ts, kind, None, digest), sort_key

    def _feed_page(
        self, account: Account, cursor: str | None, limit: int, phase: str
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "actor": account.user_id,
            "limit": limit,
            "filter": "posts_with_replies",
        }
        if cursor:
            params["cursor"] = cursor
        try:
            return self._call(GET_AUTHOR_FEED, params, phase=phase)
        except _XrpcError as exc:
            if exc.status == 400 and exc.error == "InvalidRequest":
                # Deleted, suspended and deactivated all read "Profile not found" here;
                # the profile endpoint tells them apart (and raises AccountUnavailable).
                self._profile(account, phase)
            raise

    def _walk(
        self,
        account: Account,
        since: datetime,
        until: datetime,
        *,
        limit: int,
        max_pages: int,
        phase: str,
        stop_at_first: bool = False,
    ) -> tuple[list[Activity], bool, datetime | None]:
        """Page the author feed back to ``since``.

        Returns the activities in ``[since, until)``, whether the page cap stopped the
        walk before ``since``, and the oldest ``sortAt`` reached.
        """
        found: list[Activity] = []
        cursor: str | None = None
        oldest: datetime | None = None
        pages = 0
        self._tls.reposts = []
        while True:
            if pages >= max_pages:
                return found, True, oldest
            try:
                body = self._feed_page(account, cursor, limit, phase)
            except SourceError as exc:
                if found and not isinstance(exc, (*_FATAL, AccountUnavailable)):
                    exc.partial = list(found)
                raise
            pages += 1
            items = [i for i in body.get("feed") or [] if isinstance(i, dict)]
            reached = False
            for item in items:
                activity, key = self._activity(account, item)
                if key is not None:
                    oldest = key if oldest is None else min(oldest, key)
                    if key < since:
                        reached = True
                if activity is not None and since <= activity.ts < until:
                    found.append(activity)
                    if stop_at_first:
                        return found, False, oldest
            next_cursor = body.get("cursor")
            if reached or not items or not isinstance(next_cursor, str) or not next_cursor:
                return found, False, oldest
            cursor = next_cursor

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        """Return the account's posts, replies and quotes (and reposts if enabled).

        Pages ``getAuthorFeed`` newest first until an item's ``sortAt`` is before
        ``since`` or ``max_pages`` pages (default 50, 5000 items) were read; in the
        second case ``meta["truncated"]`` is set. Activity is timed by ``indexedAt``.

        Reposts that are not counted (``include_reposts`` off) are tallied: a fetch
        that reaches ``now`` stores how many were indexed after the cutoff in
        ``meta["reposts_after_cutoff"]``, so the output can show that a silent poster
        still reposted.
        """
        found, truncated, _ = self._walk(
            account, since, until, limit=self.page_size, max_pages=self.max_pages, phase="fetch"
        )
        account.meta["truncated"] = truncated
        if until >= self.config.now and since <= self.config.cutoff:
            cutoff = self.config.cutoff
            account.meta["reposts_after_cutoff"] = sum(
                1 for ts in self._tls.reposts if cutoff <= ts < until
            )
        return found

    def newest_activity(
        self, account: Account, kind: str, since: datetime, until: datetime
    ) -> NewestActivity:
        """The pre-check's existence query: the author's newest activity in the window.

        Reads the author feed newest first and stops at the first own activity in
        ``[since, until)``, normally on the first page. ``kind`` is ignored: one feed
        holds every kind. When ``max_pages`` pages were read without reaching
        ``since``, ``covered_since`` says how far the answer reaches.
        """
        del kind
        found, capped, oldest = self._walk(
            account,
            since,
            until,
            limit=min(self.page_size, 25),
            max_pages=self.max_pages,
            phase="precheck",
            stop_at_first=True,
        )
        newest = max((a.ts for a in found), default=None)
        covered = since
        if capped and newest is None and oldest is not None and oldest > since:
            covered = oldest
        return NewestActivity(newest, covered)

    def precheck_finished(self, account: Account, errors: Sequence[SourceError]) -> None:
        """No overload guard beyond the shared client's: 429s already end the scan."""
        del account, errors
