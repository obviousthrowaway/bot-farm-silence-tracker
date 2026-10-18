"""Credential-free Reddit source backed by the Arctic Shift public archive.

Arctic Shift (https://github.com/ArthurHeitmann/arctic_shift) is a community archive of
public Reddit posts and comments with a free, keyless search API. This adapter exists
because Reddit's own unauthenticated endpoints are blocked and Bot Farm Silence Tracker does not
work around that: it never talks to reddit.com, never spoofs a browser, and never uses
proxies. It sends an honest ``User-Agent`` that names the tool and links to its
project page.

API facts this adapter relies on (https://github.com/ArthurHeitmann/arctic_shift,
``api/README.md``, sections "Posts and comments / Search", "Date", "Selectable fields",
"Rate limiting" and "Query timeout", plus behavior checked against the live service):

* ``GET /api/posts/search`` and ``GET /api/comments/search`` accept ``author``,
  ``subreddit``, ``after``, ``before`` (epoch seconds are accepted), ``sort`` (by
  ``created_utc``), ``limit`` (1 to 100, or ``auto`` for 100 to 1000 depending on
  server capacity) and ``fields`` (comma separated). The body is ``{"data": [...]}``.
* ``before`` and ``after`` are both exclusive (checked against the live service), so
  a request for ``[t0, t1)`` sends ``after = t0 - 1, before = t1``. There is no
  cursor, and many items share one second, so pagination re-requests
  ``before = oldest + 1`` and drops ids it has already seen (see
  :meth:`RedditArchiveSource._fetch_kind`).
* A 429 carries ``X-RateLimit-Reset`` (seconds until the window resets). The docs'
  "Rate limiting" section says a 429 means the client must wait, so the shared client
  waits for ``Retry-After`` or, without it, ``X-RateLimit-Reset + 1 + U(0, 5)`` seconds
  through the ``reset_wait`` hook (the jitter keeps throttled clients from returning in
  the same second).
* The server answers HTTP 422 with an ``error`` message such as ``"Timeout. Maybe slow
  down a bit"`` when a query is too slow for it, even on the first request of a scan.
  Such a 422 also carries ``X-RateLimit-Reset``. The docs' "Query timeout" section says
  a timeout means the parameter combination is too slow, and the fix is to retry or to
  narrow the query (a smaller ``limit`` or a narrower time window). So a timeout 422 is
  not a rate limit: it waits ``Retry-After`` when present, else a short jittered
  backoff, and never ``X-RateLimit-Reset``; it never lowers the request rate; then it
  runs the retry, narrow and split ladder of :meth:`RedditArchiveSource._attempt`.
  Only load signals lower the rate and wait for the reset: a 429, a ``"slow down"``
  422, a timeout 422 escalated by the guard in :meth:`RedditArchiveSource._escalate`,
  or a 502, 503 or 504 (which wait ``Retry-After`` or the exponential backoff, not the
  reset). Any response with ``X-RateLimit-Remaining`` at or below 0 also waits for the
  reset before the next request. See :meth:`RedditArchiveSource._backoff`.

Limits that cannot be fixed here (see the README section for the full list):

* The archive lags real time by hours to days, and its ingestion can have gaps.
  :meth:`RedditArchiveSource.data_horizon` reports how far it reaches, pulled back to
  the start of the first day whose ``worldnews`` comment volume looks like a gap. The
  core pipeline calls it once, before discovery (or before fetching on a resume),
  scores with ``min(now, horizon)`` and refuses a source whose data ends before
  ``cutoff + min_silence_days`` unless ``--allow-stale-source`` is given, because
  silence after that point is a gap in the archive, not a gap in the account.
* The archive does not know whether an account was suspended. A suspended account just
  looks silent, so this adapter never raises ``AccountUnavailable``.
* Account creation dates are not exposed, so ``hydrate`` leaves ``created_at`` empty
  and ``--new-accounts-days`` cannot be used with this source.
"""

from __future__ import annotations

import math
import random
import re
import statistics
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from bot_farm_silence_tracker.models import Account, Activity, content_hash
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import (
    BudgetExceeded,
    HostUse,
    NewestActivity,
    RateLimited,
    Source,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.errors import ReadTimedOut
from bot_farm_silence_tracker.sources.http import (
    PROJECT_URL,
    Attempt,
    parse_retry_after,
    research_user_agent,
)
from bot_farm_silence_tracker.timeutil import isoformat, utcnow

BASE_URL = "https://arctic-shift.photon-reddit.com"
POSTS_PATH = "/api/posts/search"
COMMENTS_PATH = "/api/comments/search"
#: Names the project page as the contact, or none while this copy is unpublished.
USER_AGENT = research_user_agent(PROJECT_URL)

#: ``limit=auto`` returns between 100 and 1000 rows and an explicit limit is at most
#: 100, so a page of fewer than this many rows is the last one either way.
AUTO_MIN_PAGE = 100
#: Largest explicit ``limit`` the API accepts.
MAX_EXPLICIT_LIMIT = 100
#: Upper bound on one computed backoff delay after a 422, before its jitter (a reset
#: header can ask for more).
MAX_SLOWDOWN_WAIT = 120.0
#: Seconds added to the server's ``X-RateLimit-Reset`` before retrying.
RESET_MARGIN = 1.0
#: A reset wait also adds a uniform random 0 to this many seconds, so clients that were
#: throttled together do not all come back in the same second.
RESET_JITTER = 5.0
#: A timeout 422's computed backoff is multiplied by a uniform factor in this range.
TIMEOUT_JITTER = (0.75, 1.25)
#: Escalation guard: when at least ``ESCALATE_TIMEOUTS`` timeout 422s arrived within
#: ``ESCALATE_WINDOW`` seconds, or at least ``ESCALATE_RECENT_422`` of the last
#: ``ESCALATE_RECENT`` responses were 422s, the next timeout 422 is treated as load.
ESCALATE_TIMEOUTS = 3
ESCALATE_WINDOW = 60.0
ESCALATE_RECENT = 10
ESCALATE_RECENT_422 = 5
#: Responses older than this many seconds no longer count toward the "last
#: ``ESCALATE_RECENT`` responses" part of the escalation guard.
ESCALATE_RECENT_MAX_AGE = 120.0
#: Rates within this of ``MIN_RPS`` count as being at the floor.
FLOOR_EPSILON = 1e-9
#: The lowest request rate the adapter will drop to after load signals.
MIN_RPS = 0.1
#: Highest request rate the ``max_rps`` option may ask for. ``max_rps`` itself defaults
#: to the pacer's rate (``http.rate[0]``: ``default_rps``, 0.5, unless the caller built
#: the client with another rate); the archive's docs call a couple per second fine.
MAX_RPS_CEILING = 2.0
#: Additive recovery: after this many successful requests in a row, a lowered rate
#: rises by ``RECOVERY_STEP`` requests per second, up to ``max_rps``.
RECOVERY_STREAK = 10
RECOVERY_STEP = 0.1
#: Statuses that mean the server is overloaded or throttling us, so the rate is halved.
LOAD_STATUSES = frozenset({429, 502, 503, 504})
#: Default ``limit`` of author history queries (``history_limit`` option). ``auto`` asks
#: the server for up to 1000 rows and times out far more often on heavy authors.
DEFAULT_HISTORY_LIMIT = MAX_EXPLICIT_LIMIT
#: Smallest ``history_limit`` allowed. A page whose new ids all share its oldest second
#: steps on with ``before = oldest`` and skips the rest of that second, so a tiny limit
#: could silently drop rows of a busy second (see :meth:`RedditArchiveSource._fetch_kind`).
MIN_HISTORY_LIMIT = 10
#: Highest ``--concurrency`` (accounts fetched at once). More workers never raise the
#: request rate (one shared pacer); they only overlap one account's waits with
#: another's requests, and two or three are enough for that.
MAX_CONCURRENCY = 3
#: Seconds to connect to the archive, and to wait for each read of its answer.
ARCHIVE_CONNECT_TIMEOUT = 10.0
ARCHIVE_READ_TIMEOUT = 20.0
#: A query that still times out is split down to windows of about this length.
MIN_SPLIT = timedelta(hours=6)
#: Shortest discovery window ``--active-within-days`` may leave after clipping. A
#: shorter one cannot hold even one discovery page and is refused before any request.
MIN_DISCOVERY_WINDOW = timedelta(hours=1)
#: Longest total wait for the 422 ladder of one logical query. The shared client's
#: ``max_total_wait`` (1800 s by default) is for ordinary retries; a slow archive query
#: that has not recovered after this long is recorded as a per-account error instead.
MAX_SLOW_QUERY_WAIT = 900.0
#: How many seed subreddits the archive horizon probe reads, besides the fallback.
PROBE_SEEDS = 3
#: Always probed first: a high-traffic sub whose newest item is close to the real horizon.
DEFAULT_PROBE_SUBREDDIT = "worldnews"
#: A probe whose newest item is this much older than the newest probe is a quiet or dead
#: sub (or a banned one), not evidence about the archive, and is ignored.
PROBE_OUTLIER = timedelta(hours=24)
#: How often an unreadable horizon probe set is retried (after one backoff each).
PROBE_RETRIES = 1
#: Coverage check: a sampled day whose ``worldnews`` comment or post rate is below this share of
#: the reference median counts as an ingestion gap (``coverage_threshold`` option).
COVERAGE_THRESHOLD = 0.25
#: The coverage check starts this long before the cutoff and runs up to the horizon.
COVERAGE_LEAD = timedelta(days=2)
#: Comment sample times per day (``coverage_samples_per_day`` option), evenly spaced
#: from the start of each day, which is the cutoff's time of day. A page of 100
#: ``worldnews`` comments covers only about 4 to 22 minutes. A sample reads low only
#: when the gap runs about 12 to 66 minutes (three page spans) past the sample time,
#: and detection also needs the confirmation an hour later to read low. The chance of
#: catching a total comment gap is about (gap - 2 hours) / 6 hours; a gap under about
#: 1 to 2 hours is never caught (3 h about 17%, 6 h about 67%, 8 h or more always).
#: Posts are sampled once a day, at the start of the day: a page of 100 posts covers
#: 13 to 17 hours, so each post sample sees most of a day and only a nearly total gap
#: of about 10 hours or more is caught, however often it is read.
COVERAGE_SAMPLES_PER_DAY = 4
#: A sample below the threshold is confirmed by one more sample this long afterwards;
#: a gap is declared only when both are below it.
COVERAGE_CONFIRM_DELAY = timedelta(hours=1)
#: Retries of one coverage sample that read-timed out (it cannot narrow, so it only waits).
COVERAGE_READ_RETRIES = 3
#: Confirmation samples the pre-flight budget check reserves room for.
COVERAGE_CONFIRM_ALLOWANCE = 6
#: Requests that must remain for discovery and fetching after the coverage check. The
#: check refuses to start without this headroom and stops (as a stale source, never as
#: a budget error) rather than spend into it.
COVERAGE_DISCOVERY_MINIMUM = 50
#: The first days after ``cutoff - COVERAGE_LEAD`` are all sampled; after that only
#: every ``COVERAGE_SPARSE_STEP``-th day, to bound the request count of old cutoffs.
COVERAGE_DENSE_DAYS = 14
COVERAGE_SPARSE_STEP = 3
#: Reference days sampled from the baseline before the checked range, and the fewest
#: used even when the baseline is shorter (they then reach back before the baseline).
COVERAGE_REFERENCE_DAYS = 7
COVERAGE_MIN_REFERENCE_DAYS = 3
#: A sample point needs at least this much archived time after it, before the horizon.
COVERAGE_MIN_SPAN = timedelta(hours=1)
#: A seed may fail this many slow queries in discovery before it is dropped.
SEED_SLOW_ALLOWANCE = 2
#: Cap lifting: if this many discovery pages in a row (pages with at least one row in
#: the window) yield fewer than ``YIELD_FLOOR`` new authors in total, the per-seed cap
#: is lifted for every seed.
YIELD_PAGES = 3
YIELD_FLOOR = 2
#: A seed whose discovery pages yield no new author this many times in a row is done.
DRY_PAGES = 5

_SKIP_AUTHORS = frozenset({"[deleted]", "[removed]", "automoderator"})
#: Names that end in "bot" but are ordinary words or surnames.
_BOT_LOOKALIKES = ("abbot", "cabot", "jabot", "robot", "sabot", "talbot", "turbot")
_BOT_NAME = re.compile(r"(?i)[-_]?bot$")
_PLACEHOLDER_TEXT = frozenset({"[removed]", "[deleted]"})
_DAY = timedelta(days=1)


def is_skipped_author(name: object) -> bool:
    """Return True for authors that are never useful sample members.

    Skips deleted authors, AutoModerator, and names that match ``(?i)[-_]?bot$``, such
    as ``RemindMeBot`` or ``sneaky_bot``: self-declared bots are not the accounts this
    tool looks for, and they crowd the pool. A name ending in a common word or surname
    that happens to end in "bot" (``talbot``, ``abbot``, ``cabot``, ``robot`` and a
    few more, see ``_BOT_LOOKALIKES``) is kept unless a ``-`` or ``_`` sits right
    before the "bot". It is a heuristic and still drops the rare person whose name
    just ends that way.
    """
    if not isinstance(name, str) or not name.strip():
        return True
    lowered = name.strip().casefold()
    if lowered in _SKIP_AUTHORS or lowered.startswith("["):
        return True
    match = _BOT_NAME.search(lowered)
    if match is None:
        return False
    if match.group(0)[0] in "-_":
        return True
    return not lowered.endswith(_BOT_LOOKALIKES)


def _header_float(response: httpx.Response, name: str) -> float | None:
    """Return a numeric header as a float, or ``None`` when absent or not a number."""
    try:
        value = float(response.headers[name])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _at_floor(rps: float) -> bool:
    return rps <= MIN_RPS + FLOOR_EPSILON


def _thread_of(row: dict[str, Any], path: str) -> str | None:
    """The thread a discovery row belongs to: a comment's ``link_id``, or ``t3_<id>``
    for a post. ``None`` when the row does not say."""
    if path == POSTS_PATH:
        post_id = row.get("id")
        return f"t3_{post_id}" if isinstance(post_id, str) and post_id.isalnum() else None
    link = row.get("link_id")
    return link if isinstance(link, str) and re.fullmatch(r"t3_[A-Za-z0-9]{1,16}", link) else None


def _as_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# -- internal signals --------------------------------------------------------------


class _Slow(Exception):
    """A 422 that asks for a slower or smaller query. ``kind`` is timeout or load.

    ``retry_after`` and ``reset`` are the ``Retry-After`` and ``X-RateLimit-Reset``
    headers in seconds (``None`` when absent). ``escalated`` marks a timeout 422 that the
    escalation guard turned into a load signal. ``read_timeout`` marks a request that
    got no answer within the read timeout: it is handled as a timeout 422 without
    headers.
    """

    def __init__(
        self,
        kind: str,
        *,
        retry_after: float | None = None,
        reset: float | None = None,
        escalated: bool = False,
        read_timeout: bool = False,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retry_after = retry_after
        self.reset = reset
        self.escalated = escalated
        self.read_timeout = read_timeout

    @property
    def is_load(self) -> bool:
        """True for a load 422 or an escalated timeout 422."""
        return self.kind == "load" or self.escalated


class _CoverageBudget(SourceError):
    """The coverage check would spend into the headroom kept for discovery."""


class _QueryTooSlow(Exception):
    """The query still times out after backing off and lowering the limit."""

    def __init__(self, slow: _Slow) -> None:
        super().__init__("query timed out")
        self.slow = slow


class SlowQueryError(SourceError):
    """One query kept timing out or being refused for load. A per-account error.

    Several of these in a row for different queries become :class:`RateLimited`.
    ``read_timeout`` is True when the last failed answer was a read timeout.
    """

    read_timeout: bool = False


def _too_slow(message: str, slow: _Slow) -> SlowQueryError:
    """A :class:`SlowQueryError` that remembers whether the last answer was a read timeout."""
    error = SlowQueryError(message)
    error.read_timeout = slow.read_timeout
    return error


@dataclass
class _Op:
    """Wait budget and 422 counters for one logical query and its retries."""

    waited: float = 0.0
    slow: int = 0
    halved: bool = False
    #: Whether a timeout 422 of this query already counts toward the escalation guard.
    timed_out: bool = False


class _ThreadState(threading.local):
    """What belongs to the current thread's request: with ``--concurrency`` each fetch
    worker runs its own logical queries, so these must never be shared."""

    #: The phase of the logical query this thread runs (see ``_run_op``).
    phase: str = "other"
    #: The logical query this thread runs, or None.
    op: _Op | None = None
    #: ``time.perf_counter`` when this thread's current attempt was sent.
    sent_at: float | None = None
    #: The pacer's rate when that attempt was sent.
    sent_rps: float = 0.0
    #: The row of this thread's last response, so the wait chosen after it can be added.
    last_row: dict[str, Any] | None = None
    #: Whether the guard escalated this thread's last timeout 422.
    last_escalated: bool = False
    #: The jittered reset wait chosen for this thread's current 429.
    pending_429_wait: float | None = None
    #: Order in which this thread's current attempt was sent, over every thread.
    send_seq: int | None = None
    #: Seconds of a shared pause waited before this thread's current attempt that no
    #: earlier row of this thread could take; added to the attempt's own row.
    pending_pause: float = 0.0
    #: Clock time of the latest shared pause this thread set itself, or None. The
    #: thread's own share of a pacer pause is accounted where it set the pause, so
    #: only the part of a pause past this time counts as one another worker set.
    own_hold: float | None = None

    def __init__(self) -> None:
        super().__init__()
        #: Different slow queries failed in a row, counted per phase, for this thread
        #: only: each fetch worker runs its own sequence of accounts, so
        #: ``max_slow_queries`` means the same as in a sequential scan.
        self.slow_failures: Counter[str] = Counter()


def _thread_attr(name: str) -> property:
    """A property that stores ``name`` in the instance's per-thread state."""

    def get(self: RedditArchiveSource) -> Any:
        return getattr(self._tls, name)

    def put(self: RedditArchiveSource, value: Any) -> None:
        setattr(self._tls, name, value)

    return property(get, put)


_FATAL = (BudgetExceeded, RateLimited, SourceAuthError)


def _window_problem(
    days: float,
    asked: tuple[datetime, datetime],
    window: tuple[datetime, datetime],
    cutoff: datetime,
) -> str | None:
    """Why the clipped lookback ``window`` cannot be used for discovery, or None."""
    start, end = window
    head = (
        f"--active-within-days {days:g} asks for authors active from "
        f"{isoformat(asked[0])} to {isoformat(asked[1])}"
    )
    if start >= end:
        return (
            f"{head}, which does not overlap the baseline window before the cutoff "
            f"({isoformat(cutoff)}). An author seen only after the cutoff can never be a "
            "cliff, so discovery would only bias the sample. Raise --active-within-days so "
            "the window reaches back before the cutoff, or drop it to discover from the "
            "baseline window."
        )
    if end - start < MIN_DISCOVERY_WINDOW:
        minutes = (end - start).total_seconds() / 60
        return (
            f"{head}, which overlaps the baseline window before the cutoff "
            f"({isoformat(cutoff)}) for only {minutes:.0f} minutes, less than the "
            f"{MIN_DISCOVERY_WINDOW.total_seconds() / 3600:g} hour discovery needs. An "
            "author seen only after the cutoff can never be a cliff. Raise "
            "--active-within-days so the window reaches further back before the cutoff, "
            "or drop it to discover from the baseline window."
        )
    return None


class RedditArchiveSource(Source):
    """Reddit activity from the Arctic Shift archive, with no credentials."""

    name: ClassVar[str] = "reddit-archive"
    required_credentials: ClassVar[tuple[str, ...]] = ()
    network_hosts: ClassVar[tuple[HostUse, ...]] = (
        HostUse(httpx.URL(BASE_URL).host, "Arctic Shift archive API (no credentials)"),
    )
    #: Conservative on purpose: the service is free, volunteer-run and rate sensitive.
    default_rps: ClassVar[float] = 0.5
    default_burst: ClassVar[float] = 1.0
    #: Hydration drops no account (the archive has no creation dates to filter on), so
    #: a 3x discovery pool is wasted requests; 1.2 leaves room for the sampler only.
    default_pool_factor: ClassVar[float | None] = 1.2
    supports_active_within: ClassVar[bool] = True
    #: Searches take ``after``, ``before``, ``sort`` and ``limit``, so the pre-check is a
    #: one-row existence query per kind (:meth:`newest_activity`).
    supports_baseline_sampling: ClassVar[bool] = True
    max_concurrency: ClassVar[int] = MAX_CONCURRENCY
    #: Connect within 10 s and get each read within 20 s. The archive answers a query
    #: it finds too slow with a 422 well before that, and a request on the wire is
    #: never interrupted, so a shorter read timeout bounds how long Ctrl-C waits for the
    #: in-flight requests of the fetch workers. A read timeout is not retried by the
    #: shared client: it is a slow query, handled like a timeout 422 (see _request).
    default_timeout: ClassVar[float | httpx.Timeout] = httpx.Timeout(
        ARCHIVE_READ_TIMEOUT, connect=ARCHIVE_CONNECT_TIMEOUT
    )

    # Per-thread request state (see _ThreadState).
    _phase = _thread_attr("phase")
    _op = _thread_attr("op")
    _sent_at = _thread_attr("sent_at")
    _sent_rps = _thread_attr("sent_rps")
    _last_row = _thread_attr("last_row")
    _last_escalated = _thread_attr("last_escalated")
    _pending_429_wait = _thread_attr("pending_429_wait")
    _send_seq = _thread_attr("send_seq")
    _own_hold = _thread_attr("own_hold")
    _pending_pause = _thread_attr("pending_pause")
    _slow_failures = _thread_attr("slow_failures")

    def __init__(
        self,
        config: SourceConfig,
        *,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        """Build the adapter.

        ``sleep`` replaces the client's sleep for the adapter's own waits. ``rng`` drives
        only the wait jitter (never discovery, which uses ``--seed``), and ``wall_clock``
        (default ``time.time``) times the run in wall-clock seconds; both are injectable
        so tests are deterministic.
        """
        super().__init__(config)
        #: Guards every piece of rate and instrumentation state shared by the fetch
        #: workers: the rate and its streak, the escalation guard, the owed reset, the
        #: failure streaks, the stats and the request log. Never held while sleeping.
        self._lock = threading.RLock()
        self._tls = _ThreadState()
        if not 1 <= config.concurrency <= MAX_CONCURRENCY:
            raise ValueError(
                f"concurrency must be between 1 and {MAX_CONCURRENCY}, got {config.concurrency}"
            )
        #: With more than one fetch worker, load signals pause every worker through
        #: the shared pacer (see :meth:`_pause_all`).
        self._concurrent = config.concurrency > 1
        self._sleep = sleep or self.http.sleep
        self._jitter = rng if rng is not None else random.Random()
        self._wall_clock = wall_clock or time.time
        self.page_size = self._positive_int("page_size", MAX_EXPLICIT_LIMIT)
        if self.page_size > MAX_EXPLICIT_LIMIT:
            raise ValueError(
                f"option 'page_size' must be at most {MAX_EXPLICIT_LIMIT}, got {self.page_size}"
            )
        self.max_items = self._positive_int("max_items_per_kind", 5000)
        self.max_slowdowns = self._non_negative("max_slowdown_retries", 1)
        self.load_failure_limit = self._positive_int("max_slow_queries", 3)
        self.dry_pages = self._positive_int("dry_pages", DRY_PAGES)
        wait = self.option_float("slowdown_wait", 10.0)
        if wait is None or wait < 0:
            raise ValueError(f"option 'slowdown_wait' must be zero or more, got {wait!r}")
        self.slowdown_wait = wait
        threshold = self.option_float("coverage_threshold", COVERAGE_THRESHOLD)
        if threshold is None or not 0 <= threshold < 1:
            raise ValueError(
                f"option 'coverage_threshold' must be at least 0 and below 1, got {threshold!r}"
            )
        self.coverage_threshold = threshold
        samples = self._positive_int("coverage_samples_per_day", COVERAGE_SAMPLES_PER_DAY)
        if samples > 24:
            raise ValueError(f"option 'coverage_samples_per_day' must be at most 24, got {samples}")
        self.coverage_samples_per_day = samples
        base_rps = self.http.rate[0]
        max_rps = self.option_float("max_rps", base_rps)
        if max_rps is None or not MIN_RPS <= max_rps <= MAX_RPS_CEILING:
            raise ValueError(
                f"option 'max_rps' must be between {MIN_RPS:g} and {MAX_RPS_CEILING:g}, "
                f"got {max_rps!r}"
            )
        self.max_rps = max_rps
        step = self.option_float("recovery_step", RECOVERY_STEP)
        if step is None or step <= 0:
            raise ValueError(f"option 'recovery_step' must be positive, got {step!r}")
        self.recovery_step = step
        self.recovery_streak = self._positive_int("recovery_streak", RECOVERY_STREAK)
        history = (self.option_str("history_limit") or str(DEFAULT_HISTORY_LIMIT)).strip()
        if history == "auto":
            self.history_limit: int | str = "auto"
        else:
            try:
                self.history_limit = int(history)
            except ValueError:
                self.history_limit = 0
            if not MIN_HISTORY_LIMIT <= int(self.history_limit) <= MAX_EXPLICIT_LIMIT:
                raise ValueError(
                    f"option 'history_limit' must be 'auto' or {MIN_HISTORY_LIMIT} to "
                    f"{MAX_EXPLICIT_LIMIT}, got {history!r}"
                )
        # The scan starts at max_rps. It defaults to the pacer's own rate (http.rate[0],
        # which the CLI sizes from default_rps), so the default start is unchanged.
        # Load signals halve it from there.
        self._rps = max_rps
        if self._rps != base_rps:
            self.http.adjust_rate(self._rps)
        self._ok_streak = 0
        # -- instrumentation: one row per HTTP attempt, drained by the pipeline.
        self._sent_rps = self._rps
        self._log: list[dict[str, Any]] = []
        #: Ids of the rows that are some fetch worker's last row (concurrent mode
        #: only): a wait may still be added to them, so a drain keeps them back.
        self._open_rows: set[int] = set()
        self._stats: Counter[str] = Counter()
        self._latency_ms_total = 0.0
        #: Seconds slept by the 422 ladder (the shared client's own waits not included).
        self._backoff_total = 0.0
        #: Seconds slept before a request because ``X-RateLimit-Remaining`` reached 0.
        self._reset_wait_total = 0.0
        self._started = self.http.clock()
        self._wall_started = self._wall_clock()
        self._floor_time = 0.0
        self._floor_since: float | None = self._started if _at_floor(self._rps) else None
        #: Escalation guard state: clock times of recent timeout 422s (one per logical
        #: query), and the clock time and status of recent responses.
        self._timeouts: deque[float] = deque()
        # The guard's thresholds scale with --concurrency: K workers run K logical
        # queries at once, so K times as many timeouts arrive per minute for the same
        # per-query load. Scaling keeps the signal per query what it is at K = 1.
        workers = config.concurrency
        self._escalate_timeouts = ESCALATE_TIMEOUTS * workers
        self._escalate_recent_422 = ESCALATE_RECENT_422 * workers
        self._recent: deque[tuple[float, int | None]] = deque(maxlen=ESCALATE_RECENT * workers)
        #: Order of the attempts sent so far, over every worker (see ``send_seq``).
        self._send_counter = 0
        #: Seconds the attempts waited in the pacer for a pause another worker set.
        self._pause_total = 0.0
        #: A reset wait owed before the next request (``X-RateLimit-Remaining`` <= 0),
        #: as ``(X-RateLimit-Reset header, jittered wait)``.
        self._pending_reset: tuple[float, float] | None = None
        self._rng = random.Random(config.seed)
        self._probe_subreddits: tuple[str, ...] = ()
        self._horizon_done = False
        self._horizon: datetime | None = None
        self._horizon_error: SourceError | None = None
        self._discovering = False
        #: True while the coverage check runs: requests then stop short of the headroom
        #: kept for discovery instead of ever exhausting the budget.
        self._guard_budget = False
        #: Archive horizon found by the probes, or None: the minimum of the comment and
        #: post horizons (see :meth:`_kind_horizon`), before the coverage check may pull
        #: the reported :meth:`data_horizon` back further.
        self.archive_newest: datetime | None = None

        def reset_wait(response: httpx.Response) -> float | None:
            # Called by the shared client on a 429 without Retry-After: wait for the
            # reset plus the margin and jitter chosen in _on_response.
            wait, self._pending_429_wait = self._pending_429_wait, None
            if wait is None:
                reset = _header_float(response, "X-RateLimit-Reset")
                if reset is not None:
                    with self._lock:
                        wait = self._capped_reset_delay(reset)
                    self._pause_all(wait)
            return wait

        # The raw reset is capped in _capped_reset_delay; the client's own cap check then
        # allows the margin and jitter added after it to pass max_retry_wait by this much.
        reset_wait.margin = RESET_MARGIN + RESET_JITTER  # type: ignore[attr-defined]
        self.http.configure(
            reset_wait=reset_wait,
            before_attempt=self._before_attempt,
            on_response=self._on_response,
            on_attempt_failed=self._on_attempt_failed,
            on_attempt_not_sent=self._on_attempt_not_sent,
            # A read timeout is a slow query: _request hands it to the 422 ladder.
            retry_read_timeouts=False,
        )

    def _positive_int(self, key: str, default: int) -> int:
        value = self.option_int(key, default)
        if value is None or value <= 0:
            raise ValueError(f"option {key!r} must be a positive integer, got {value!r}")
        return value

    def _non_negative(self, key: str, default: int) -> int:
        value = self.option_int(key, default)
        if value is None or value < 0:
            raise ValueError(f"option {key!r} must be zero or more, got {value!r}")
        return value

    # -- requests ----------------------------------------------------------------

    @staticmethod
    def _slow_kind(response: httpx.Response) -> str | None:
        """Classify a 422: ``timeout`` (query too slow), ``load`` (slow down) or None.

        A message containing "timeout" or "timed out" (any case) is a timeout, even
        when it also says "slow down": the live service's usual message is ``"Timeout.
        Maybe slow down a bit"``, and the docs' "Query timeout" section treats it as a
        slow query. Repeated timeouts still count toward the escalation guard (see
        :meth:`_escalate`). Only a message that says "slow down" without a timeout is
        load. The ``error`` field may be any JSON value; a non-string one is read as
        text. A body that is not JSON is read as text too.
        """
        try:
            body = response.json()
        except ValueError:
            message = response.text
        else:
            if isinstance(body, dict):
                error = body.get("error")
                message = error if isinstance(error, str) else ("" if error is None else str(error))
            else:
                message = response.text
        lowered = message.lower()
        if "timeout" in lowered or "timed out" in lowered:
            return "timeout"
        if "slow down" in lowered:
            return "load"
        return None

    def _request(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Send one search request. Raise :class:`_Slow` on a slow-query 422.

        A request that got no answer within the read timeout (``ARCHIVE_READ_TIMEOUT``)
        raises :class:`_Slow` too, as a timeout: the archive is still working on a
        query too slow for it, so the ladder narrows the query instead of the shared
        client sending the same one again. A connect timeout stays a transport error.
        """
        if self._guard_budget:
            remaining = self.http.requests_remaining
            # The HTTP layer may retry this request (each attempt counts against the
            # budget), so leave room for every attempt as well as for discovery.
            if remaining is not None and remaining <= (
                COVERAGE_DISCOVERY_MINIMUM + self.http.max_retries
            ):
                raise _CoverageBudget(
                    f"only {remaining} of --max-requests remain, and the coverage check "
                    f"keeps {COVERAGE_DISCOVERY_MINIMUM} for discovery"
                )
        try:
            response = self.http.get(
                BASE_URL + path,
                params=params,
                headers={"User-Agent": USER_AGENT},
                sends_credentials=False,
            )
        except ReadTimedOut:
            raise _Slow("timeout", escalated=self._last_escalated, read_timeout=True) from None
        status = response.status_code
        if status == 200:
            try:
                body = response.json()
            except ValueError as exc:
                raise SourceError(f"invalid JSON from the archive for {path}") from exc
            rows = body.get("data") if isinstance(body, dict) else body
            if not isinstance(rows, list):
                raise SourceError(f"unexpected response shape from the archive for {path}")
            self._note_ok()
            return [row for row in rows if isinstance(row, dict)]
        if status == 422:
            kind = self._slow_kind(response)
            if kind is not None:
                raise _Slow(
                    kind,
                    retry_after=parse_retry_after(response.headers.get("Retry-After")),
                    reset=_header_float(response, "X-RateLimit-Reset"),
                    escalated=kind == "timeout" and self._last_escalated,
                )
        raise SourceError(f"the archive returned HTTP {status} for {path}")

    # -- rate control --------------------------------------------------------------

    def _set_rps(self, rps: float) -> None:
        """Change the pacer's rate in place and track the time spent at the floor."""
        now = self.http.clock()
        at_floor = _at_floor(rps)
        if self._floor_since is not None and not at_floor:
            self._floor_time += now - self._floor_since
            self._floor_since = None
        elif self._floor_since is None and at_floor:
            self._floor_since = now
        self._rps = rps
        self.http.adjust_rate(rps)

    def _note_ok(self) -> None:
        """Count a success; every ``recovery_streak`` in a row, raise the rate a step.

        Recovery is additive (``recovery_step``, default 0.1 rps, per
        ``recovery_streak``, default 10, successes in a row) and stops at ``max_rps``.
        """
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= self.recovery_streak:
                self._ok_streak = 0
                if self._rps < self.max_rps:
                    self._set_rps(min(self.max_rps, round(self._rps + self.recovery_step, 6)))

    def _note_load(self) -> None:
        """Halve the request rate (never below ``MIN_RPS``) after a load signal.

        Load signals are a 429, a 502, 503 or 504, a 422 whose message asks to slow
        down, and a timeout 422 escalated by :meth:`_escalate`. A plain query timeout is
        not one: it means the query was too slow.
        """
        with self._lock:
            self._ok_streak = 0
            if not _at_floor(self._rps):
                self._stats["halvings"] += 1
                self._set_rps(max(MIN_RPS, self._rps / 2))
        self.warn(
            "The archive signalled load (HTTP 429, a 'slow down' 422, a burst of timeout "
            "422s, or a 502, 503 or 504), so Bot Farm Silence Tracker halved its request rate "
            "(never "
            f"below {MIN_RPS:g} per second). It rises again by {self.recovery_step:g} per "
            f"second after every {self.recovery_streak} successful requests in a row."
        )

    def _note_slow(self, op: _Op, slow: _Slow) -> None:
        """Count a slow-query 422; only a load 422 lowers the rate, once per query.

        A timeout 422 resets the success streak (so recovery waits for clean requests)
        but leaves the rate alone. A load 422 or an escalated timeout halves the rate,
        at most once per logical query.
        """
        op.slow += 1
        with self._lock:
            self._ok_streak = 0
        if slow.is_load:
            if slow.escalated:
                what = (
                    "had several read timeouts"
                    if slow.read_timeout
                    else "answered several timeout 422s"
                )
                self.warn(
                    f"The archive {what} in a short time, so "
                    "Bot Farm Silence Tracker treated the next one as a load signal: it halved the "
                    "request rate and waited for the rate-limit reset."
                )
            if not op.halved:
                op.halved = True
                self._note_load()
            return
        if slow.read_timeout:
            self.warn(
                f"The archive did not answer at least one query within {ARCHIVE_READ_TIMEOUT:g} "
                "seconds. Bot Farm Silence Tracker treated it like a timeout 422: it backed off "
                "and retried with a smaller query; the request rate was not lowered."
            )
            return
        self.warn(
            "The archive answered HTTP 422 'Timeout' for at least one query, which means "
            "the query was too slow, not that Bot Farm Silence Tracker asked too often. "
            "Bot Farm Silence Tracker backed off and retried with a smaller query; the request "
            "rate was not lowered."
        )

    def _escalate(self) -> bool:
        """Escalation guard for a timeout 422 that just arrived; True makes it load.

        A timeout 422 is normally a slow query, not throttling. But when at least
        ``ESCALATE_TIMEOUTS`` (3) timeout 422s arrived within ``ESCALATE_WINDOW`` (60)
        seconds, or at least ``ESCALATE_RECENT_422`` (5) of the last ``ESCALATE_RECENT``
        (10) responses were 422s, the server is struggling in general, so the next
        timeout 422 is treated as load: the rate is halved once and the wait is the
        reset (see :meth:`_backoff`). Both counts look at the responses before this one.
        After an escalation they start over, so the guard does not fire on every 422
        of the same burst. The ambiguous ``"Timeout. Maybe slow down a bit"`` counts
        here like any other timeout.

        Both counts take at most one timeout 422 per logical query (see
        :meth:`_run_op`), so the retry ladder of one heavy query cannot escalate by
        itself; it takes timeouts of several different queries. Responses older than
        ``ESCALATE_RECENT_MAX_AGE`` (120) seconds drop out of the second count.

        With ``--concurrency K`` the counts are shared by every worker (the server's
        state is shared), and each worker's logical query is its own, so a query is
        still counted once. The thresholds are scaled by K (``3 K`` timeouts in 60
        seconds, or ``5 K`` 422s among the last ``10 K`` responses): K workers run K
        queries at once, so the same per-query timeout rate brings K times as many
        timeouts, and unscaled thresholds would escalate about K times as often. The
        caller holds ``self._lock`` across the check, the clearing and
        :meth:`_note_guard`, so two workers cannot both escalate one burst.
        """
        now = self.http.clock()
        while self._timeouts and now - self._timeouts[0] > ESCALATE_WINDOW:
            self._timeouts.popleft()
        while self._recent and now - self._recent[0][0] > ESCALATE_RECENT_MAX_AGE:
            self._recent.popleft()
        # A read timeout is stored with status None and counts like a timeout 422.
        recent_422 = sum(1 for _, status in self._recent if status in (422, None))
        return (
            len(self._timeouts) >= self._escalate_timeouts
            or recent_422 >= self._escalate_recent_422
        )

    def _note_guard(self, status: int | None, kind: str | None) -> None:
        """Feed one response that was not escalated to the escalation guard.

        A timeout 422 of a logical query that already timed out once is left out of
        both counts: it says the same query is still slow, not that the server is.
        A read timeout comes with ``status`` None and ``kind`` timeout, and counts as a
        timeout 422 in both counts.
        """
        if kind == "timeout":
            op = self._op
            if op is not None and op.timed_out:
                return
            if op is not None:
                op.timed_out = True
        now = self.http.clock()
        self._recent.append((now, status))
        if kind == "timeout":
            self._timeouts.append(now)

    def _reset_delay(self, reset: float) -> float:
        """``X-RateLimit-Reset + 1 + U(0, 5)`` seconds (the docs: a 429 means wait)."""
        with self._lock:
            jitter = self._jitter.uniform(0.0, RESET_JITTER)
        return max(0.0, reset) + RESET_MARGIN + jitter

    def _pause_all(self, seconds: float | None) -> None:
        """With ``--concurrency``, keep every worker from sending for ``seconds``.

        Called for load signals only: a 429, a 502, 503 or 504, a load 422, an
        escalated timeout 422 and an exhausted ``X-RateLimit-Remaining``. The worker
        that saw the signal waits on its own as before; the others wait in the shared
        pacer before their next attempt (see :meth:`HttpClient.hold`). A plain
        timeout 422 pauses only its own query. Sequential scans never pause the
        pacer, so their waits are exactly what they were.
        """
        if self._concurrent and seconds is not None and seconds > 0:
            until = self.http.clock() + seconds
            own = self._own_hold
            self._own_hold = until if own is None else max(own, until)
            self.http.hold_until(until)

    def _capped_reset_delay(self, reset: float) -> float:
        """:meth:`_reset_delay`, after checking the raw reset against ``max_retry_wait``.

        Raises:
            RateLimited: the raw reset is longer than the client's ``max_retry_wait``.
                The margin and jitter are added only after this check, the same as for an
                owed reset and a load 422.
        """
        if reset > self.http.max_retry_wait:
            raise RateLimited(reset, "the archive asked for a rate-limit reset longer than the cap")
        return self._reset_delay(reset)

    def _owed_reset(self) -> float | None:
        """The reset wait owed because ``X-RateLimit-Remaining`` reached 0, or None.

        Raises:
            RateLimited: the owed reset is longer than the client's ``max_retry_wait``
                (the same cap a 429 or a load 422 gets). The debt is cleared first.
        """
        if self._pending_reset is None:
            return None
        reset, delay = self._pending_reset
        if reset > self.http.max_retry_wait:
            self._pending_reset = None
            raise RateLimited(
                reset,
                "the archive's rate-limit window is used up and its reset is longer than the cap",
            )
        return delay

    def _retry_pause(self, step: int | None = None) -> None:
        """Sleep before a probe or coverage sample is retried.

        The wait is ``slowdown_wait``. With a ``step`` (a coverage sample that read-timed
        out) it is the jittered timeout backoff of :meth:`_backoff_delay` instead.
        """
        wait = self.slowdown_wait
        with self._lock:
            if step is not None:
                wait = min(wait * (2**step), MAX_SLOWDOWN_WAIT)
                wait *= self._jitter.uniform(*TIMEOUT_JITTER)
            self._backoff_total += wait
            self._note_wait(wait)
        self._sleep(wait)

    def _note_wait(self, seconds: float) -> None:
        """Add a wait chosen after this thread's last response to that response's row."""
        row = self._last_row
        if row is not None:
            with self._lock:
                row["wait_s"] = round((row.get("wait_s") or 0.0) + seconds, 3)

    def _set_last_row(self, row: dict[str, Any] | None) -> None:
        """Make ``row`` this thread's last row; with ``--concurrency`` keep it open.

        The caller holds ``self._lock``. An open row is not drained yet (see
        :meth:`drain_request_log`), because the wait chosen after it is added later.
        """
        if self._concurrent:
            previous = self._last_row
            if previous is not None:
                self._open_rows.discard(id(previous))
            if row is not None:
                self._open_rows.add(id(row))
        self._last_row = row

    # -- instrumentation -----------------------------------------------------------

    def _before_attempt(self, attempt: Attempt) -> None:
        # Every attempt has already passed the shared pacer (at most max_rps). A reset
        # owed because X-RateLimit-Remaining reached 0 is waited for on top of that.
        # The pacer refilled during that wait, so it is restarted afterwards: without
        # that, the request after this one would go out right behind it.
        with self._lock:
            wait = self._owed_reset()
            if wait is not None:
                self._pending_reset = None
                self._reset_wait_total += wait
                self._note_wait(wait)
        if wait is not None:
            self._sleep(wait)
            self.http.restart_pacer()
        with self._lock:
            paused = self._others_pause(attempt.paused)
            if paused > 0:
                self._note_pause(paused)
            self._sent_rps = self._rps
            self._send_counter += 1
            self._send_seq = self._send_counter
        self._sent_at = time.perf_counter()

    def _others_pause(self, paused: float) -> float:
        """The part of a pacer pause of ``paused`` seconds that another worker set.

        A pause this thread set itself (an exhausted ``X-RateLimit-Remaining``, whose
        reset this thread then waits for in the pacer) is already accounted where it
        was set, as a reset wait on the row that asked for it. Only the time past the
        end of this thread's own pause, which another worker's later pause added, is a
        pause another worker set. The attempt has passed the pacer, so this thread's
        own pause is over and is forgotten.
        """
        own, self._own_hold = self._own_hold, None
        if paused <= 0 or own is None:
            return max(0.0, paused)
        # The pause ended at the latest hold's time (the pacer's interval after it is
        # not part of it); a hold set since this attempt passed the pacer is later
        # than now, so now bounds it.
        end = self.http.paused_until
        end = self.http.clock() if end is None else min(end, self.http.clock())
        others = min(paused, max(0.0, end - own))
        return others if others > 1e-9 else 0.0

    def _note_pause(self, seconds: float) -> None:
        """Account a shared pause this thread waited in the pacer. Holds ``self._lock``.

        It counts toward the wait budget of this thread's logical query and the
        ``pause_wait_s`` total, and is added to the ``wait_s`` of this thread's last
        row, or, when this is the thread's first request of an account, to the row of
        the attempt it delayed.
        """
        self._pause_total += seconds
        op = self._op
        if op is not None:
            op.waited += seconds
        if self._last_row is not None:
            self._note_wait(seconds)
        else:
            self._pending_pause += seconds

    def _record(
        self,
        attempt: Attempt,
        status: int | None,
        slow_kind: str | None,
        *,
        reset: float | None = None,
        retry_after: float | None = None,
    ) -> dict[str, Any]:
        """Append one request row. Only the page ``limit`` and the window span are
        derived from the query; no handle, subreddit or other parameter value is kept."""
        latency = (
            (time.perf_counter() - self._sent_at) * 1000.0 if self._sent_at is not None else None
        )
        self._sent_at = None
        params = attempt.params or {}
        window: int | None = None
        try:
            if "after" in params and "before" in params:
                window = int(params["before"]) - int(params["after"])
        except (TypeError, ValueError):
            window = None
        url = str(attempt.url)
        endpoint = (
            "posts"
            if url.endswith(POSTS_PATH)
            else "comments"
            if url.endswith(COMMENTS_PATH)
            else "other"
        )
        limit = params.get("limit")
        row: dict[str, Any] = {
            "at": isoformat(utcnow()),
            "phase": self._phase,
            "endpoint": endpoint,
            "status": status,
            "latency_ms": None if latency is None else round(latency, 1),
            "page_limit": None if limit is None else str(limit),
            "window_s": window,
            "rps": self._sent_rps,
            "slow_kind": slow_kind,
            "attempt": attempt.attempt,
            "wait_s": None,
            "reset_s": reset,
            "retry_after_s": retry_after,
            "send_seq": self._send_seq,
        }
        self._send_seq = None
        if self._pending_pause:
            row["wait_s"] = round(self._pending_pause, 3)
            self._pending_pause = 0.0
        with self._lock:
            self._log.append(row)
            self._set_last_row(row)
            self._stats["requests"] += 1
            if latency is not None:
                self._stats["timed"] += 1
                self._latency_ms_total += latency
            if status is None:
                self._stats["failed"] += 1
                if slow_kind == "read_timeout":
                    self._stats["read_timeout"] += 1
            elif status == 422:
                self._stats["422"] += 1
                self._stats[f"422_{slow_kind or 'other'}"] += 1
            elif status in LOAD_STATUSES:
                self._stats["load"] += 1
        return row

    def _on_response(self, attempt: Attempt, response: httpx.Response) -> None:
        status = response.status_code
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        reset = _header_float(response, "X-RateLimit-Reset")
        kind = self._slow_kind(response) if status == 422 else None
        with self._lock:
            if kind == "timeout":
                escalated = self._guard_timeout(status, "422_escalated")
            else:
                escalated = False
                self._note_guard(status, kind)
        self._last_escalated = escalated
        with self._lock:
            self._respond(attempt, status, kind, reset, retry_after, response)

    def _guard_timeout(self, status: int | None, stat: str) -> bool:
        """Feed a timeout to the escalation guard; True when it escalated to load.

        ``status`` is 422 for a timeout 422 and None for a read timeout; ``stat`` is
        the counter an escalation adds to. The caller holds ``self._lock``: one step
        for every worker, the guard's check, its clearing and the count.
        """
        escalated = self._escalate()
        if escalated:
            if self._op is not None:
                self._op.timed_out = True  # This query must not count toward the next.
            self._stats[stat] += 1
            self._timeouts.clear()
            self._recent.clear()
        else:
            self._note_guard(status, "timeout")
        return escalated

    def _respond(
        self,
        attempt: Attempt,
        status: int,
        kind: str | None,
        reset: float | None,
        retry_after: float | None,
        response: httpx.Response,
    ) -> None:
        """Record a response and choose the waits it asks for. Holds ``self._lock``."""
        row = self._record(attempt, status, kind, reset=reset, retry_after=retry_after)
        retried = attempt.attempt < self.http.max_retries
        cap = self.http.max_retry_wait
        if status == 429:
            # The shared client retries a 429 after Retry-After, else after the reset
            # (the reset_wait hook returns the jittered wait chosen here). A wait over
            # the client's cap is never slept (the client raises RateLimited), so the
            # row then records no wait.
            wait = retry_after
            if wait is None and reset is not None:
                if reset > cap:
                    wait = reset  # Over the cap: the reset_wait hook raises RateLimited.
                else:
                    wait = self._pending_429_wait = self._reset_delay(reset)
                    cap += RESET_MARGIN + RESET_JITTER  # The client's margin for a reset.
            if retried and (wait is None or wait <= cap):
                if wait is None:
                    wait = self._client_backoff(attempt)
                # Added: the row may already hold a pause waited before this attempt.
                row["wait_s"] = round((row.get("wait_s") or 0.0) + wait, 3)
                self._pause_all(wait)
            else:
                # The client raises RateLimited (retries exhausted, or a wait over the
                # cap). The scan is stopping; pause the other workers anyway so none
                # of them sends again before the stop reaches it.
                self._pause_all(wait if wait is not None else self._client_backoff(attempt))
        elif status >= 500 and retried and (retry_after is None or retry_after <= cap):
            # 502, 503 and 504 halve the rate below, then the client waits Retry-After
            # or its exponential backoff, not the reset.
            wait = retry_after if retry_after is not None else self._client_backoff(attempt)
            row["wait_s"] = round((row.get("wait_s") or 0.0) + wait, 3)
            self._pause_all(wait)
        if status in LOAD_STATUSES:
            self._note_load()
        remaining = _header_float(response, "X-RateLimit-Remaining")
        if status != 429 and remaining is not None and remaining <= 0 and reset is not None:
            # The window is used up: wait for its reset before the next request.
            delay = self._reset_delay(reset)
            if self._concurrent and reset <= self.http.max_retry_wait:
                # Every worker waits for it in the shared pacer, not just the next
                # request of this one. A reset over the cap stays owed, so the next
                # attempt of any worker raises RateLimited as it would sequentially.
                self._pause_all(delay)
                self._reset_wait_total += delay
                row["wait_s"] = round((row.get("wait_s") or 0.0) + delay, 3)
            else:
                self._pending_reset = (reset, delay)

    def _client_backoff(self, attempt: Attempt) -> float:
        """The shared client's exponential backoff for ``attempt`` (for the log only)."""
        return min(self.http.backoff_max, self.http.backoff_base * (2**attempt.attempt))

    def _on_attempt_failed(self, attempt: Attempt, error: BaseException) -> None:
        if isinstance(error, httpx.ReadTimeout):
            # A slow query (see _request): the guard counts it like a timeout 422.
            with self._lock:
                escalated = self._guard_timeout(None, "read_timeout_escalated")
            self._last_escalated = escalated
            self._record(attempt, None, "read_timeout")
            return
        self._record(attempt, None, None)

    def _on_attempt_not_sent(self, attempt: Attempt, error: BaseException) -> None:
        # Never sent (a connect error, or a refused attempt): not a request, no row.
        self._sent_at = None
        self._send_seq = None
        with self._lock:
            self._stats["not_sent"] += 1

    def drain_request_log(self, *, final: bool = False) -> list[dict[str, Any]]:
        """Return the per-request rows recorded since the last call, and forget them.

        With ``--concurrency``, a row that is still some worker's last row is kept
        back until that worker moves on (a wait may still be added to it), unless
        ``final`` is set. Rows are returned in the order their requests were sent
        (``send_seq``, stamped under the lock right before each send), which with
        several workers can differ from the order their responses were recorded in.
        """
        with self._lock:
            if final or not self._open_rows:
                rows, self._log = self._log, []
                self._open_rows.clear()
            else:
                rows = [row for row in self._log if id(row) not in self._open_rows]
                self._log = [row for row in self._log if id(row) in self._open_rows]
        rows.sort(key=lambda row: (row.get("send_seq") is None, row.get("send_seq") or 0))
        return rows

    def request_summary(self) -> dict[str, Any] | None:
        """Totals over every request this adapter made: counts, latency and time at floor.

        ``elapsed_s`` and ``time_at_floor_s`` use the monotonic clock, which on macOS
        stops while the host sleeps. ``wall_elapsed_s`` is wall-clock time, and
        ``observed_rps`` is based on it, so it is what a person waiting would see.
        """
        with self._lock:
            return self._summary()

    def _summary(self) -> dict[str, Any]:
        now = self.http.clock()
        floor = self._floor_time
        if self._floor_since is not None:
            floor += now - self._floor_since
        elapsed = max(0.0, now - self._started)
        wall = max(0.0, self._wall_clock() - self._wall_started)
        timed = self._stats["timed"]
        return {
            "requests": self._stats["requests"],
            "not_sent": self._stats["not_sent"],
            "status_422": self._stats["422"],
            "status_422_timeout": self._stats["422_timeout"],
            "status_422_load": self._stats["422_load"],
            "status_422_escalated": self._stats["422_escalated"],
            "load_responses": self._stats["load"],
            "failed": self._stats["failed"],
            "read_timeouts": self._stats["read_timeout"],
            "read_timeouts_escalated": self._stats["read_timeout_escalated"],
            "mean_latency_ms": round(self._latency_ms_total / timed, 1) if timed else None,
            "elapsed_s": round(elapsed, 1),
            "wall_elapsed_s": round(wall, 1),
            "observed_rps": round(self._stats["requests"] / wall, 3) if wall > 0 else None,
            "time_at_floor_s": round(floor, 1),
            "rate_halvings": self._stats["halvings"],
            "backoff_wait_s": round(self._backoff_total, 1),
            "reset_wait_s": round(self._reset_wait_total, 1),
            "pause_wait_s": round(self._pause_total, 1),
            "final_rps": self._rps,
            "max_rps": self.max_rps,
        }

    def _backoff(self, op: _Op, slow: _Slow, step: int) -> None:
        """Sleep after a slow-query 422, within the shared wait budget of one query.

        Following the Arctic Shift ``api/README.md``: its "Query timeout" section says a
        timeout means the query is too slow and should be retried or narrowed, and its
        "Rate limiting" section says a 429 means wait. So:

        * ``Retry-After``, when present, is waited for (a longer one than the client's
          ``max_retry_wait`` raises :class:`RateLimited`).
        * A timeout 422 otherwise waits ``min(slowdown_wait * 2**step, 120) * U(0.75,
          1.25)`` seconds (step 0 for a plain retry, then one more per ladder step of
          the same page). ``X-RateLimit-Reset`` is ignored: a timeout is not a rate
          limit, and its reset (often close to a minute) was the bulk of the wait.
        * A load 422, or a timeout escalated by :meth:`_escalate`, waits
          ``X-RateLimit-Reset + 1 + U(0, 5)`` seconds, or the jittered backoff above
          when it has no reset header.

        A reset owed because ``X-RateLimit-Remaining`` reached 0 is folded in (the
        longer of the two is waited; one longer than ``max_retry_wait`` raises
        :class:`RateLimited`). All waits of one logical query (retries, the
        lowered limit and every split window) share ``min(http.max_total_wait,
        MAX_SLOW_QUERY_WAIT)``; the wait that would pass it raises
        :class:`SlowQueryError`.
        """
        with self._lock:
            delay = self._backoff_delay(op, slow, step)
            if slow.is_load:
                self._pause_all(delay)
        self._sleep(delay)

    def _backoff_delay(self, op: _Op, slow: _Slow, step: int) -> float:
        """Choose and account the wait of :meth:`_backoff`. Holds ``self._lock``."""
        cap = self.http.max_retry_wait
        too_long = "the archive asked for a wait longer than the cap"
        if slow.retry_after is not None:
            if slow.retry_after > cap:
                raise RateLimited(slow.retry_after, too_long)
            delay = slow.retry_after
        elif slow.is_load and slow.reset is not None:
            if slow.reset > cap:
                raise RateLimited(slow.reset, too_long)
            delay = self._reset_delay(slow.reset)
        else:
            delay = min(self.slowdown_wait * (2**step), MAX_SLOWDOWN_WAIT)
            delay *= self._jitter.uniform(*TIMEOUT_JITTER)
        owed = self._owed_reset()
        if owed is not None:
            delay = max(delay, owed)
        budget = min(self.http.max_total_wait, MAX_SLOW_QUERY_WAIT)
        if op.waited + delay > budget:
            # A reset still owed stays pending, so the next request waits for it.
            raise SlowQueryError(
                f"the archive kept timing out; waiting {delay:g}s more would pass the "
                f"{budget:g}s wait budget for one query"
            )
        self._pending_reset = None
        op.waited += delay
        self._backoff_total += delay
        self._note_wait(delay)
        return delay

    def _attempt(
        self, op: _Op, path: str, params: dict[str, Any], *, can_narrow: bool = True
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Run one page request through the first two steps of the slow-query ladder.

        On a slow-query 422 (each wait as in :meth:`_backoff`):

        1. wait and retry the same query ``max_slowdown_retries`` times (default 1);
        2. for a timeout with ``limit=auto``, wait and retry with ``limit=100``;
        3. otherwise raise :class:`_QueryTooSlow` (a timeout, escalated or not), for
           the caller to narrow the time window, or :class:`SlowQueryError` (a load
           refusal, where a smaller query would not help).

        A timeout that the escalation guard turned into load skips step 1: after
        several timeouts in a short time, the identical query is the least likely to
        get through, so the next attempt is the narrower one (step 2, else step 3).
        ``can_narrow`` says whether the caller has a narrower window to try; without
        one (and without ``limit=auto``) step 1 still runs.

        Returns the rows and the parameters that worked (the caller keeps a lowered
        limit for the rest of the window).
        """
        retries = 0
        step = 0
        while True:
            try:
                return self._request(path, params), params
            except _Slow as slow:
                self._note_slow(op, slow)
                narrower = can_narrow or params.get("limit") == "auto"
                skip = slow.kind == "timeout" and slow.escalated and narrower
                if retries < self.max_slowdowns and not skip:
                    retries += 1
                elif slow.kind == "timeout" and params.get("limit") == "auto":
                    params = {**params, "limit": MAX_EXPLICIT_LIMIT}
                elif slow.kind == "timeout":
                    raise _QueryTooSlow(slow) from None
                else:
                    raise SlowQueryError(
                        f"the archive kept asking to slow down for {path} "
                        f"({op.slow} answers of HTTP 422)"
                    ) from None
                self._backoff(op, slow, step)
                step += 1

    def _slow_failed(self, phase: str, exc: SlowQueryError) -> None:
        """Count a failed query of ``phase``; enough in a row become :class:`RateLimited`."""
        with self._lock:
            self._slow_failures[phase] += 1
            failed = self._slow_failures[phase] >= self.load_failure_limit
            if failed:
                self._slow_failures[phase] = 0
        if failed:
            raise RateLimited(
                None,
                f"the archive timed out or refused {self.load_failure_limit} different "
                f"{phase} queries in a row (last: {exc})",
            ) from exc

    def _run_op[T](self, fn: Callable[[_Op], T], phase: str, *, count: bool = True) -> T:
        """Run one logical query; escalate repeated slow-query failures.

        A query that fails the whole ladder raises :class:`SlowQueryError`, a plain
        per-account error. When ``max_slow_queries`` (default 3) different queries of
        the same ``phase`` fail that way in a row, the
        archive is overloaded rather than one query being slow, so this raises
        :class:`RateLimited` and the scan stops as partial. A success resets only its
        own phase. Only the discovery, fetch and precheck phases count. The probe and
        coverage phases pass ``count=False``: their callers try every target and
        report an unreadable one as a stale source. ``fetch_activity`` counts the posts
        and comments queries of one account as one, and so does the pre-check, through
        :meth:`precheck_finished`.
        """
        op = _Op()
        previous, self._phase = self._phase, phase
        outer, self._op = self._op, op
        try:
            result = fn(op)
        except SlowQueryError as exc:
            if count:
                self._slow_failed(phase, exc)
            raise
        finally:
            self._phase = previous
            self._op = outer
        if count:
            with self._lock:
                self._slow_failures[phase] = 0
        return result

    def _narrowing(
        self, op: _Op, path: str, params: dict[str, Any], narrower_after: int | None
    ) -> list[dict[str, Any]]:
        """Run a single-page query; on a timeout, retry once with a later ``after``."""
        return self._narrowing_bound(op, path, params, narrower_after)[0]

    def _narrowing_bound(
        self, op: _Op, path: str, params: dict[str, Any], narrower_after: int | None
    ) -> tuple[list[dict[str, Any]], bool]:
        """:meth:`_narrowing`, also saying whether the narrower window was used."""
        can_narrow = narrower_after is not None and narrower_after > int(params.get("after", 0))
        try:
            return self._attempt(op, path, params, can_narrow=can_narrow)[0], False
        except _QueryTooSlow as slow:
            if not can_narrow:
                raise _too_slow(
                    f"the archive query for {path} kept timing out", slow.slow
                ) from None
            self._backoff(op, slow.slow, self.max_slowdowns + 1)
            try:
                narrowed = {**params, "after": narrower_after}
                return self._attempt(op, path, narrowed, can_narrow=False)[0], True
            except _QueryTooSlow as again:
                raise _too_slow(
                    f"the archive query for {path} kept timing out, even for a narrower window",
                    again.slow,
                ) from None

    # -- archive horizon ---------------------------------------------------------

    def note_seeds(self, seeds: SeedSet) -> None:
        """Remember the seeds, so the horizon probe can use them.

        ``discover`` calls this itself. A resumed scan (``--from-run``) skips
        discovery, so the CLI calls it with the seeds stored on the earlier run.
        """
        if seeds.items and not self._probe_subreddits:
            self._probe_subreddits = tuple(seeds.items[:PROBE_SEEDS])

    def _probe_targets(self) -> tuple[str, ...]:
        """``worldnews`` first, then up to three configured or noted seeds."""
        option = self.option_str("freshness_subreddit")
        names: tuple[str, ...] = ()
        if option:
            names = tuple(n.strip().removeprefix("r/") for n in option.split(",") if n.strip())
        names = names or self._probe_subreddits
        fallback = DEFAULT_PROBE_SUBREDDIT
        rest = [n for n in dict.fromkeys(names) if n.casefold() != fallback]
        return (fallback, *rest[:PROBE_SEEDS])

    def _newest_in(self, subreddit: str, path: str) -> datetime | None:
        now = math.floor(self.config.now.timestamp())
        params = {
            "subreddit": subreddit,
            "sort": "desc",
            "limit": 1,
            "fields": "created_utc",
            "before": now + 1,
        }
        narrower = now - int(timedelta(days=3).total_seconds())
        # Every probe target is tried, even after one fails. A failed probe is not counted
        # toward the slow-query guard: the caller records the problems, and a stale source
        # is reported by it.
        rows = self._run_op(
            lambda op: self._narrowing(op, path, params, narrower), "probe", count=False
        )
        return _as_datetime(rows[0].get("created_utc")) if rows else None

    def _probe_kind(self, path: str) -> tuple[dict[str, datetime], list[str]]:
        """Newest item per probed sub for one kind, plus a note per sub that gave none."""
        found: dict[str, datetime] = {}
        problems: list[str] = []
        label = "posts" if path == POSTS_PATH else "comments"
        for sub in self._probe_targets():
            try:
                newest = self._newest_in(sub, path)
            except _FATAL:
                raise
            except SourceError as exc:
                problems.append(f"r/{sub} {label}: {exc}")
                continue
            if newest is None:
                problems.append(f"r/{sub}: no archived {label}")
            else:
                found[sub] = newest
        return found, problems

    def _kind_horizon(self, path: str) -> datetime:
        """Horizon of one kind: the oldest newest-item among the probes that agree.

        The newest item of every probed sub is read. A sub that is quiet, banned or
        dead (r/The_Donald's newest comment is from 2020) says nothing about archive
        lag, so any probe more than 24 hours older than the newest probe is dropped,
        with a warning that names it. Of the rest the oldest is used, which stays
        conservative inside that 24 hour band. An unreadable probe set is retried once
        after a backoff; if it is still unreadable this raises :class:`SourceError`.
        """
        label = "posts" if path == POSTS_PATH else "comments"
        problems: list[str] = []
        found: dict[str, datetime] = {}
        for attempt in range(PROBE_RETRIES + 1):
            found, problems = self._probe_kind(path)
            if found:
                break
            if attempt < PROBE_RETRIES:
                self._retry_pause()
        if not found:
            raise SourceError(
                f"could not read the archive's newest {label[:-1]} (" + "; ".join(problems) + ")"
            )
        newest = max(found.values())
        kept: list[datetime] = []
        for sub, when in found.items():
            if newest - when > PROBE_OUTLIER:
                self.warn(
                    f"r/{sub} was ignored for the archive horizon: its newest {label[:-1]} "
                    f"({when:%Y-%m-%d %H:%M} UTC) is more than 24 hours older than the "
                    f"newest probe ({newest:%Y-%m-%d %H:%M} UTC), so it is quiet or dead, "
                    "not evidence about archive lag."
                )
            else:
                kept.append(when)
        return min(kept)

    def data_horizon(self) -> datetime | None:
        """Return how far the archive reaches, for comments and posts alike.

        Probes ``worldnews`` plus up to three seed subreddits (the
        ``freshness_subreddit`` option, which may list several separated by commas,
        else the noted seeds) for their newest comment and their newest post, and
        takes the older of the two kinds' horizons (see :meth:`_kind_horizon`). Then
        :meth:`_coverage_horizon` checks the days from two days before the cutoff up
        to that horizon for ingestion gaps and pulls the horizon back to the start of
        the first day that looks like one. The answer is computed once and cached.

        Raises:
            SourceError: no probe could be read for one of the kinds, even after a
                retry, or the coverage check could not complete. The core treats that
                as a stale source: the scan stops, or with ``--allow-stale-source``
                every result is provisional. A rate limit, budget or auth error is
                re-raised as is and not cached.
        """
        if not self._horizon_done:
            self._horizon_done = True
            self.archive_newest = None  # Never leave a probe value from an earlier attempt.
            try:
                probed = min(self._kind_horizon(COMMENTS_PATH), self._kind_horizon(POSTS_PATH))
                self.archive_newest = probed
                self._guard_budget = True
                horizon = self._coverage_horizon(probed)
            except _FATAL:
                self._horizon_done = False
                raise
            except SourceError as exc:
                self._horizon_error = exc
            else:
                self._horizon = horizon
            finally:
                self._guard_budget = False
        if self._horizon_error is not None:
            raise self._horizon_error
        return self._horizon

    def fallback_horizon(self) -> datetime | None:
        """Return the probed archive horizon when :meth:`data_horizon` raised, else ``None``.

        The core uses it with ``--allow-stale-source`` after a failed coverage check, so
        scoring stops at the newest archived item instead of at ``now``.
        """
        return self.archive_newest

    # -- coverage check ----------------------------------------------------------

    def _sample_rate(self, path: str, start: int, end: int) -> float:
        """Estimate ``worldnews`` items per second from ``start`` on, in one request.

        Reads the first page (at most 100 items of the kind at ``path``) at or after
        ``start`` and before ``end``. A full page gives its items over the time they
        span, at least one second; a short page gives every item over the whole
        ``[start, end)`` range. Only timestamps inside ``[start, end)`` count, and a
        page without any is a rate of 0, which is a gap. A coverage sample is retried
        once, but one whose reads time out is retried up to ``COVERAGE_READ_RETRIES``
        (3) times with jittered backoff. Coverage failures do not count toward
        ``max_slow_queries``. Raises :class:`SourceError` when the request fails or
        the page cannot be read.
        """
        params = {
            "subreddit": DEFAULT_PROBE_SUBREDDIT,
            "after": start - 1,  # `after` is exclusive.
            "before": end,
            "sort": "asc",
            "limit": MAX_EXPLICIT_LIMIT,
            "fields": "id,created_utc",
        }
        # A failed coverage sample is not counted toward the slow-query guard: it is retried
        # on its own and, if it stays unreadable, the SourceError below reports a stale
        # source. Counting it would trip RateLimited first and hide that outcome.
        rows = self._run_op(
            lambda op: self._narrowing(op, path, params, None), "coverage", count=False
        )
        try:
            stamps = [
                stamp
                for stamp in (
                    math.floor(when.timestamp())
                    for when in (_as_datetime(row.get("created_utc")) for row in rows)
                    if when is not None
                )
                if start <= stamp < end
            ]
            if not stamps:
                return 0.0
            if len(rows) >= MAX_EXPLICIT_LIMIT:
                return len(stamps) / max(1, max(stamps) - start + 1)
            return len(stamps) / max(1, end - start)
        except (ArithmeticError, ValueError, TypeError) as exc:
            raise SourceError(f"the archive returned an unreadable page for {path}") from exc

    def _coverage_plan(
        self, check_start: int, horizon_s: int, offsets: list[int]
    ) -> list[tuple[int, int, int]]:
        """List the ``(day start, sample time, day end)`` triples the check reads.

        Every day is sampled for the first ``COVERAGE_DENSE_DAYS`` days from
        ``check_start``, then every ``COVERAGE_SPARSE_STEP``-th day. A sample needs
        ``COVERAGE_MIN_SPAN`` of archived time after it.
        """
        day = int(_DAY.total_seconds())
        span = int(COVERAGE_MIN_SPAN.total_seconds())
        plan: list[tuple[int, int, int]] = []
        index = 0
        start = check_start
        while start < horizon_s:
            end = min(start + day, horizon_s)
            sampled = (
                index < COVERAGE_DENSE_DAYS
                or (index - COVERAGE_DENSE_DAYS) % (COVERAGE_SPARSE_STEP) == 0
            )
            if sampled:
                plan.extend((start, start + o, end) for o in offsets if start + o + span <= end)
            start += day
            index += 1
        return plan

    def _coverage_horizon(self, horizon: datetime) -> datetime:
        """Pull ``horizon`` back to the start of the first day that looks like a gap.

        The archive can miss hours or days of ingestion in the middle of its range, and
        such a gap would look like every account going silent at once. So the days from
        ``cutoff - 2 days`` up to the horizon are checked against the baseline.
        ``worldnews`` comments are sampled at ``coverage_samples_per_day`` (default 4)
        evenly spaced times of each day, counted from the cutoff's time of day, and
        ``worldnews`` posts once a day at the start of the day. Each sample is one cheap
        request with ``limit=100`` (never the aggregate endpoint, which answers 0). Up
        to seven reference days spread over the baseline before that range are sampled
        the same way, and every sample is compared with the reference median of the same
        kind at the same time of day. A sample below ``coverage_threshold`` (default
        0.25) of that median is confirmed by one more sample an hour later; only when
        both are below is it a gap: a warning names the kind and the day and the horizon
        moves back to the start of that day. The core then scores as of that moment, and
        refuses the scan (or with ``--allow-stale-source`` makes every result
        provisional) when that is earlier than the cutoff plus ``--min-silence-days``.
        ``coverage_threshold=0`` turns the check off.

        The first 14 days are checked every day and later days every third day, which
        bounds the request count. The check is coarse. A comment sample reads only 4 to 22
        minutes of traffic. A sample reads low only when the gap runs about 12 to 66
        minutes (three page spans) past the sample time, and detection also needs the
        confirmation an hour later to read low. The chance of catching a total comment
        gap is about (gap - 2 hours) / 6 hours; a gap under about 1 to 2 hours is never
        caught (3 h about 17%, 6 h about 67%, 8 h or more always). A post sample reads
        13 to 17 hours, so only nearly total gaps of about 10 hours or more show. A
        partial gap, where some items are still present, is not detected. A sample is
        retried once after a backoff.

        Raises:
            SourceError: a sample could not be read (even after its retry), the reference
                days hold no ``worldnews`` comments or posts, or the remaining
                ``--max-requests`` budget cannot cover the checks plus
                ``COVERAGE_DISCOVERY_MINIMUM`` requests for discovery, so coverage is
                unknown (fail closed). Never :class:`BudgetExceeded`: the check stops
                short of the budget instead.
        """
        if self.coverage_threshold <= 0:
            return horizon
        day = int(_DAY.total_seconds())
        span = int(COVERAGE_MIN_SPAN.total_seconds())
        check_start = math.floor((self.config.cutoff - COVERAGE_LEAD).timestamp())
        horizon_s = math.floor(horizon.timestamp())
        if horizon_s <= check_start:
            return horizon  # Far too stale anyway; the core refuses it on its own.
        baseline_start = self.config.baseline_start or self.config.cutoff - timedelta(days=30)
        reference_span = (check_start - math.floor(baseline_start.timestamp())) // day
        reference_days = max(COVERAGE_MIN_REFERENCE_DAYS, reference_span)
        count = min(COVERAGE_REFERENCE_DAYS, reference_days)
        picks = sorted(
            {1 + round(i * (reference_days - 1) / max(1, count - 1)) for i in range(count)}
        )
        comment_offsets = [
            day * i // self.coverage_samples_per_day for i in range(self.coverage_samples_per_day)
        ]
        offsets = {COMMENTS_PATH: comment_offsets, POSTS_PATH: [0]}
        kinds = ((COMMENTS_PATH, "comments"), (POSTS_PATH, "posts"))
        full_plan = self._coverage_plan(check_start, horizon_s, comment_offsets)
        plans = {
            COMMENTS_PATH: full_plan,
            POSTS_PATH: [item for item in full_plan if item[1] == item[0]],
        }

        samples = sum(len(picks) * len(offsets[path]) + len(plans[path]) for path, _ in kinds)
        # Worst case: every sample is retried once, and a few dips are confirmed.
        needed = (
            2 * (samples + COVERAGE_CONFIRM_ALLOWANCE)
            + COVERAGE_DISCOVERY_MINIMUM
            + self.http.max_retries
        )
        remaining = self.http.requests_remaining
        if remaining is not None and needed > remaining:
            raise SourceError(
                f"the archive coverage check needs at least {needed} requests (its samples "
                f"with one retry each, though a sample whose reads time out is retried up "
                f"to {COVERAGE_READ_RETRIES} times, plus {COVERAGE_DISCOVERY_MINIMUM} kept for "
                f"discovery and {self.http.max_retries} for HTTP retries) but only "
                f"{remaining} remain of --max-requests, so "
                "ingestion gaps after the cutoff cannot be ruled out; raise "
                "--max-requests or lower --source-option coverage_samples_per_day"
            )

        def label(start: int) -> str:
            return f"{datetime.fromtimestamp(start, UTC):%Y-%m-%d %H:%M} UTC"

        rates: dict[tuple[str, int, int], float] = {}

        def rate(path: str, name: str, start: int, end: int) -> float:
            # A confirmation can land on the next sample time (24 samples a day): reuse it.
            if (path, start, end) in rates:
                return rates[path, start, end]
            attempt = 0
            while True:
                try:
                    rates[path, start, end] = self._sample_rate(path, start, end)
                    return rates[path, start, end]
                except (*_FATAL, _CoverageBudget):
                    raise
                except SourceError as exc:
                    # A coverage query cannot narrow, so a read timeout (the archive is slow,
                    # not refusing) gets up to COVERAGE_READ_RETRIES jittered retries.
                    timed_out = isinstance(exc, SlowQueryError) and exc.read_timeout
                    if attempt < (COVERAGE_READ_RETRIES if timed_out else 1):
                        self._retry_pause(attempt if timed_out else None)
                        attempt += 1
                        continue
                    raise SourceError(
                        f"the archive coverage check could not read r/{DEFAULT_PROBE_SUBREDDIT} "
                        f"{name} at {label(start)} ({exc}), so ingestion gaps after the "
                        "cutoff cannot be ruled out"
                    ) from exc

        medians: dict[tuple[str, int], float] = {}
        for path, name in kinds:
            for offset in offsets[path]:
                median = statistics.median(
                    rate(path, name, check_start - k * day + offset, check_start - (k - 1) * day)
                    for k in picks
                )
                if median <= 0:
                    raise SourceError(
                        f"the archive coverage check found no r/{DEFAULT_PROBE_SUBREDDIT} "
                        f"{name} in the baseline reference days, so ingestion gaps after the "
                        "cutoff cannot be ruled out"
                    )
                medians[path, offset] = median

        confirm_delay = int(COVERAGE_CONFIRM_DELAY.total_seconds())
        for start, point, end in full_plan:
            for path, name in kinds:
                if path == POSTS_PATH and point != start:
                    continue
                median = medians[path, point - start]
                share = rate(path, name, point, end) / median
                if share >= self.coverage_threshold:
                    continue
                # One dip is not a gap: look again an hour later, as far as the day allows.
                again = min(point + confirm_delay, end - span)
                if again > point:
                    second = rate(path, name, again, end) / median
                    if second >= self.coverage_threshold:
                        continue
                else:
                    second = share
                gap = datetime.fromtimestamp(start, UTC)
                repeat = f", and again at {label(again)} at {second:.0%}" if again != point else ""
                self.warn(
                    f"The archive looks incomplete on the day starting {label(start)}: "
                    f"r/{DEFAULT_PROBE_SUBREDDIT} {name} at {label(point)} ran at "
                    f"{share:.0%} of the baseline median{repeat} "
                    f"(threshold {self.coverage_threshold:.0%}), which looks like "
                    "an ingestion gap. The data horizon is pulled back to "
                    f"{label(start)}, so silence after it is not counted."
                )
                return min(horizon, gap)
        return horizon

    # -- Source interface --------------------------------------------------------

    def _lookback(self) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
        """The asked-for and the effective ``--active-within-days`` windows.

        Asked for: ``[now' - D, now')``, where ``now'`` is the scan's effective now
        (``now``, pulled back to the archive horizon when :meth:`data_horizon`, or after
        a failed coverage check the probed horizon, knows an earlier one, because
        nothing after it is archived). Effective: that window clipped to the baseline,
        ``[max(now' - D, baseline_start), min(now', cutoff))``. The end stops at the
        cutoff because an author seen only after it can never be a cliff: pooling such
        authors would only shrink the share of cliffs, biasing the rate low. The start
        stops at the baseline start because activity before it is never scored.
        """
        days = self.config.active_within_days
        assert days is not None
        cutoff = self.config.cutoff
        baseline_start = self.config.baseline_start or cutoff - timedelta(days=30)
        now = self.config.now
        for known in (self._horizon, self.archive_newest):
            if known is not None and known < now:
                now = known
        asked = (now - timedelta(days=days), now)
        return asked, (max(asked[0], baseline_start), min(now, cutoff))

    @classmethod
    def active_within_problem(
        cls,
        *,
        days: float,
        now: datetime,
        cutoff: datetime,
        baseline_start: datetime,
        min_silence_days: float,
        allow_stale_source: bool,
    ) -> str | None:
        """Refuse a lookback whose clipped window is empty or too short however the
        archive horizon turns out, before any request is sent.

        The scan's effective now is ``now``, or the archive horizon when that is
        earlier, and the horizon is only known after the probe. Without
        ``allow_stale_source`` the scan stops unless that horizon is at least
        ``cutoff + min_silence_days``, so the effective now lies in ``[cutoff +
        min_silence_days, now]``; with it, the effective now can be anything up to
        ``now``. The clipped window ``[max(t - days, baseline_start), min(t, cutoff))``
        has a length that is a concave function of the effective now ``t``, so its
        best case over that range is at an end of the range or at one of the two
        breakpoints, ``cutoff`` and ``baseline_start + days``. When even the best case
        is shorter than ``MIN_DISCOVERY_WINDOW`` (1 hour), the window is refused here.
        Otherwise discovery checks the window it actually gets (see
        :meth:`discovery_window`).

        Without ``allow_stale_source`` this refuses, for example, any ``days`` up to
        ``min_silence_days``: the window can only overlap the baseline if the horizon
        is less than ``days`` after the cutoff, and such a scan is stopped as stale.
        """
        lookback = timedelta(days=days)
        earliest = None if allow_stale_source else cutoff + timedelta(days=min_silence_days)
        if earliest is not None and earliest > now:
            return None  # Every horizon is too early: the scan is stopped as stale anyway.
        candidates = [now, cutoff, baseline_start + lookback]
        if earliest is not None:
            candidates.append(earliest)
        usable = [t for t in candidates if t <= now and (earliest is None or t >= earliest)]

        def clipped(t: datetime) -> tuple[datetime, datetime]:
            return max(t - lookback, baseline_start), min(t, cutoff)

        best = max(usable, key=lambda t: clipped(t)[1] - clipped(t)[0])
        problem = _window_problem(days, (best - lookback, best), clipped(best), cutoff)
        if problem is None:
            return None
        if best == now:
            context = ""
        elif earliest is not None and best == earliest:
            context = (
                f" That is the best case: without --allow-stale-source the scan only runs "
                f"when the archive reaches at least the cutoff plus --min-silence-days "
                f"({isoformat(earliest)}), so the D days end no earlier than that."
            )
        else:
            context = " That is the best case over every archive horizon the scan could find."
        return f"{problem}{context} Nothing was sent to the archive."

    def discovery_window(self) -> tuple[datetime, datetime]:
        """The ``[start, end)`` window discovery samples authors from.

        By default that is the baseline, ``[baseline_start, cutoff)``. With
        ``config.active_within_days`` (``--active-within-days D``) it is the last D
        days before the scan's effective now, clipped to the baseline (see
        :meth:`_lookback`), so it never reaches past the cutoff.

        Raises:
            SourceError: the clipped ``--active-within-days`` window is empty (the D
                days lie wholly after the cutoff, or before the baseline) or shorter
                than ``MIN_DISCOVERY_WINDOW`` (1 hour). The CLI refuses a lookback that
                is bound to end that way before any request (see
                :meth:`active_within_problem`); this is the backstop for one that only
                the probed horizon rules out.
        """
        cutoff = self.config.cutoff
        if self.config.active_within_days is None:
            return self.config.baseline_start or cutoff - timedelta(days=30), cutoff
        asked, window = self._lookback()
        problem = _window_problem(self.config.active_within_days, asked, window, cutoff)
        if problem is not None:
            raise SourceError(problem)
        return window

    def placebo_discovery_problem(self, seeds: SeedSet) -> str | None:
        """The archive searches by time (``after`` / ``before``), so discovery can be
        repeated in any earlier window."""
        del seeds
        return None

    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        """Yield authors active in :meth:`discovery_window` in the seed subreddits.

        Each round visits the seeds in a fresh random order. Each visit reads one page
        of posts or comments (picked at random) of the one-day window that ends at a
        ``before`` drawn uniformly from the discovery window (by default
        ``[baseline_start, cutoff)``, with ``--active-within-days D`` the last D days
        clipped to that baseline window),
        so the pool spreads across the whole window and every hour of the day instead
        of piling into the newest hours. A page that times out is retried for a
        six-hour window, so a long window never turns into one slow query. No seed
        may supply more than ``ceil(pool_limit / len(seeds))`` accounts while other
        seeds still have new authors; once every remaining seed is at its share (the
        others ran dry or failed) the cap is lifted. Posts or comments of a
        seed whose pages yield no new author ``dry_pages`` (default 5) times in a row
        are done; a seed is done when both are. A seed whose query fails is warned
        about and dropped. All randomness comes from ``--seed``.
        Each page is a slice of the real activity, so the pool is a sample, not a
        census.
        """
        self._discovering = True
        try:
            yield from self._discover(seeds, pool_limit)
        finally:
            self._discovering = False

    def _discovery_page(self, sub: str, path: str, before: int, start: int) -> list[dict[str, Any]]:
        # `after` is exclusive, so `start - 1` still asks for the first second.
        after = max(start - 1, before - int(_DAY.total_seconds()))
        params: dict[str, Any] = {
            "subreddit": sub,
            "after": after,
            "before": before,
            "sort": "desc",
            "limit": self.page_size,
            "fields": "id,author,created_utc",
        }
        narrower = max(start - 1, before - int(MIN_SPLIT.total_seconds()))
        return self._run_op(lambda op: self._narrowing(op, path, params, narrower), "discovery")

    def _discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        if pool_limit <= 0 or not seeds.items:
            return
        start, cutoff = self.discovery_window()  # `cutoff` is the window's end here.
        lookback = self.config.active_within_days is not None
        if lookback:
            asked, _ = self._lookback()
            if (start, cutoff) != asked:
                self.warn(
                    f"--active-within-days {self.config.active_within_days:g} asked for "
                    f"authors active from {isoformat(asked[0])} to {isoformat(asked[1])}; "
                    f"discovery uses {isoformat(start)} to {isoformat(cutoff)} instead, the "
                    "part inside the baseline window before the cutoff. Authors seen only "
                    "after the cutoff can never be cliffs, and activity before the baseline "
                    "is never scored."
                )
        self.note_seeds(seeds)

        start_s = math.ceil(start.timestamp())
        cutoff_s = math.floor(cutoff.timestamp())
        cap = math.ceil(pool_limit / len(seeds.items))
        active = list(seeds.items)
        taken: Counter[str] = Counter()
        dry: Counter[tuple[str, str]] = Counter()
        rows_per_sub: Counter[str] = Counter()
        failed: set[str] = set()
        slow_seen: Counter[str] = Counter()
        recent: deque[int] = deque(maxlen=YIELD_PAGES)
        relaxed = False
        seen: set[str] = set()
        yielded = 0
        while active:
            order = list(active)
            self._rng.shuffle(order)
            if not relaxed and all(taken[sub] >= cap for sub in order):
                relaxed = True
            for sub in order:
                if not relaxed and taken[sub] >= cap:
                    continue
                # `before` is exclusive, so start_s + 1 still covers the first second.
                before = self._rng.randint(start_s + 1, cutoff_s)
                kinds = [p for p in (POSTS_PATH, COMMENTS_PATH) if dry[sub, p] < self.dry_pages]
                path = self._rng.choice(kinds)
                try:
                    rows = self._discovery_page(sub, path, before, start_s)
                except _FATAL:
                    raise
                except SlowQueryError as exc:
                    slow_seen[sub] += 1
                    if slow_seen[sub] <= SEED_SLOW_ALLOWANCE:
                        continue  # Try this seed again in a later round.
                    self.warn(
                        f"r/{sub} was dropped from discovery after {slow_seen[sub]} slow "
                        f"archive queries ({exc}); the pool comes from the other seeds."
                    )
                    failed.add(sub)
                    active.remove(sub)
                    continue
                except SourceError as exc:
                    self.warn(
                        f"r/{sub} was dropped from discovery after an archive error ({exc}); "
                        "the pool comes from the other seeds."
                    )
                    failed.add(sub)
                    active.remove(sub)
                    continue
                new = 0
                in_window = 0
                for row in rows:
                    ts = _as_datetime(row.get("created_utc"))
                    if ts is None or not start <= ts < cutoff:
                        continue
                    in_window += 1
                    rows_per_sub[sub] += 1
                    author = row.get("author")
                    if is_skipped_author(author):
                        continue
                    key = str(author).casefold()
                    if key in seen:
                        continue
                    if not relaxed and taken[sub] >= cap:
                        break
                    seen.add(key)
                    taken[sub] += 1
                    new += 1
                    yielded += 1
                    meta: dict[str, object] = {"community": sub}
                    thread = _thread_of(row, path)
                    if thread:
                        # The permutation null keeps accounts of one thread together.
                        meta["discovery_thread"] = thread
                    yield Account(self.name, str(author), str(author), None, meta)
                    if yielded >= pool_limit:
                        return
                dry[sub, path] = 0 if new else dry[sub, path] + 1
                if in_window:  # An empty page says nothing about how many authors are left.
                    recent.append(new)
                    if len(recent) == YIELD_PAGES and sum(recent) < YIELD_FLOOR:
                        relaxed = True  # Yield collapsed: capped seeds are needed after all.
                if all(dry[sub, p] >= self.dry_pages for p in (POSTS_PATH, COMMENTS_PATH)):
                    active.remove(sub)

        for sub in seeds.items:
            if rows_per_sub[sub] == 0 and sub not in failed:
                self.warn(
                    f"r/{sub} returned no archived items inside the discovery window, so it "
                    "contributed no accounts (quiet, private, banned or not archived)."
                )
        if yielded < pool_limit:
            what = (
                f"authors active in the clipped last {self.config.active_within_days:g} days"
                if lookback
                else "pre-cutoff authors"
            )
            self.warn(
                f"Discovery found only {yielded} distinct {what} (wanted "
                f"{pool_limit}); random pages of the discovery window stopped turning up "
                "new authors."
            )

    def hydrate(self, account: Account) -> Account:
        """Leave ``created_at`` empty: the archive's search API does not expose it.

        This makes no request. Because ``--new-accounts-days`` needs the creation
        date, it will drop every account with this source, and the warning says so
        when that filter is in play (hydration during sampling).
        """
        if account.created_at is None and self._discovering:
            self.warn(
                "The Reddit archive does not expose account creation dates, so "
                "--new-accounts-days cannot filter accounts with this source: accounts of "
                "unknown age are dropped."
            )
        return account

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        """Return the author's posts and comments with ``since <= ts < until``.

        Each kind is paged backward from ``until`` until ``since`` is reached or
        ``max_items_per_kind`` (default 5000) items were kept. In the second case
        ``meta["truncated"]`` is set, and the oldest baseline days are the ones missing.
        """
        truncated = False
        activities: list[Activity] = []
        try:
            for path, kind in ((POSTS_PATH, "post"), (COMMENTS_PATH, "comment")):
                items, cut = self._run_op(
                    lambda op, p=path, k=kind: self._fetch_kind(op, account, p, k, since, until),
                    "fetch",
                    count=False,
                )
                activities.extend(items)
                truncated = truncated or cut
        except SlowQueryError as exc:
            self._slow_failed("fetch", exc)  # One account counts once, whichever kind failed.
            raise
        finally:
            if self._concurrent:
                # This worker's last row gets no more waits: let the next drain take it.
                with self._lock:
                    self._set_last_row(None)
        with self._lock:
            self._slow_failures["fetch"] = 0
        account.meta["truncated"] = truncated
        return activities

    def newest_activity(
        self, account: Account, kind: str, since: datetime, until: datetime
    ) -> NewestActivity:
        """The two-phase pre-check's existence query: one ``sort=desc, limit=1`` search.

        Asks for the author's newest post or comment in ``[since, until)``. It runs
        through the same slow-query ladder as every query; when the archive keeps
        timing out on the whole window, the query is narrowed once to start at the
        scan's cutoff, which still tells activity after the cutoff apart, and
        ``covered_since`` says so (the pre-check then treats the account as a cliff
        candidate unless it finds activity after the cutoff). A query that fails the
        whole ladder raises :class:`SlowQueryError` and counts toward the slow-query
        guard of the ``precheck`` phase, so an overloaded archive stops the scan
        instead of making every account a candidate. The guard counts accounts, not
        queries (see :meth:`precheck_finished`), as ``fetch_activity`` does.
        """
        path = COMMENTS_PATH if kind == "comment" else POSTS_PATH
        after = math.floor(since.timestamp()) - 1
        params = {
            "author": account.handle,
            "after": after,
            "before": math.ceil(until.timestamp()),
            "sort": "desc",
            "limit": 1,
            "fields": "created_utc",
        }
        # Both bounds are exclusive: ``after = floor(cutoff) - 1`` starts at the cutoff's
        # second, so activity at the cutoff itself is still seen.
        narrower = math.floor(self.config.cutoff.timestamp()) - 1
        try:
            rows, narrowed = self._run_op(
                lambda op: self._narrowing_bound(
                    op, path, params, narrower if narrower > after else None
                ),
                "precheck",
                count=False,
            )
        finally:
            if self._concurrent:
                with self._lock:
                    self._set_last_row(None)
        stamps = [ts for row in rows if (ts := _as_datetime(row.get("created_utc"))) is not None]
        covered = datetime.fromtimestamp(narrower + 1, tz=UTC) if narrowed else since
        newest = max((ts for ts in stamps if since <= ts < until), default=None)
        return NewestActivity(newest, covered)

    def precheck_finished(self, account: Account, errors: Sequence[SourceError]) -> None:
        """Count one account's pre-check toward the ``precheck`` slow-query guard.

        One account counts once: as a failure when any of its existence queries
        failed the whole slow-query ladder, whichever kind it was, and as a success
        (which resets the guard) when every query answered. Counting each query on
        its own would let a kind that keeps failing alternate with one that answers
        and never trip the guard. A pre-check that failed for another reason neither
        counts nor resets.

        Raises:
            RateLimited: ``max_slow_queries`` accounts in a row had a failed query.
        """
        slow = next((e for e in errors if isinstance(e, SlowQueryError)), None)
        if slow is not None:
            self._slow_failed("precheck", slow)
        elif not errors:
            with self._lock:
                self._slow_failures["precheck"] = 0

    def _fetch_kind(
        self,
        op: _Op,
        account: Account,
        path: str,
        kind: str,
        since: datetime,
        until: datetime,
    ) -> tuple[list[Activity], bool]:
        """Page one kind of the author's history, newest first.

        Pagination: ``before`` is exclusive and many items share a second, so the next
        page asks for ``before = oldest + 1`` and skips ids already seen. A page that
        adds no new id moves on with ``before = oldest`` (at most a page of items that
        share the oldest second can be lost that way, only past 100 such items).

        Both ``after`` and ``before`` are exclusive on the live service, so a window
        ``(after, before)`` asks for the seconds strictly between them, and the first
        window starts at ``floor(since) - 1`` to include the second ``since`` falls in.

        When a page still times out after the ladder in :meth:`_attempt`, the rest of
        its window is split in half at ``mid`` into ``(after, mid + 1)`` and
        ``(mid, before)``. The halves overlap by one exclusive bound, so second ``mid``
        is requested by the older half; id dedup drops any duplicates. Each half is
        paged on its own, newer half first, down to windows of about six hours. A
        window that small that still times out raises :class:`SlowQueryError`.
        """
        if kind == "post":
            fields = "id,created_utc,subreddit,title,selftext"
        else:
            fields = "id,created_utc,subreddit,body"
        out: list[Activity] = []
        seen: set[object] = set()
        limit: int | str = self.history_limit
        min_split = int(MIN_SPLIT.total_seconds())
        # A stack of open (after, before) windows; the newest window is on top.
        windows = [(math.floor(since.timestamp()) - 1, math.ceil(until.timestamp()))]
        while windows:
            after, before = windows.pop()
            while before > after + 1:
                params = {
                    "author": account.handle,
                    "after": after,
                    "before": before,
                    "sort": "desc",
                    "limit": limit,
                    "fields": fields,
                }
                try:
                    rows, used = self._attempt(
                        op, path, params, can_narrow=before - after > min_split
                    )
                except _QueryTooSlow as slow:
                    if before - after <= min_split:
                        raise SlowQueryError(
                            f"the archive kept timing out for {kind}s of this author, even "
                            f"for a {(before - after) / 3600:.1f}-hour window"
                        ) from None
                    limit = MAX_EXPLICIT_LIMIT  # Split windows must not restart at auto.
                    mid = after + (before - after) // 2
                    windows.append((after, mid + 1))  # Both bounds are exclusive.
                    windows.append((mid, before))
                    self._backoff(op, slow.slow, self.max_slowdowns + 1)
                    break
                limit = used["limit"]
                oldest: int | None = None
                new = 0
                for row in rows:
                    ts = _as_datetime(row.get("created_utc"))
                    if ts is None:
                        continue
                    epoch = math.floor(ts.timestamp())
                    oldest = epoch if oldest is None else min(oldest, epoch)
                    activity = Activity(
                        self.name,
                        account.user_id,
                        ts,
                        kind,
                        str(row["subreddit"]) if row.get("subreddit") else None,
                        self._hash(kind, row),
                    )
                    row_id = row.get("id")
                    key = row_id if row_id is not None else (epoch, activity.content_hash)
                    if key in seen:
                        continue
                    seen.add(key)
                    new += 1
                    if not since <= ts < until:
                        continue
                    if len(out) >= self.max_items:
                        return out, True
                    out.append(activity)
                page_size = limit if isinstance(limit, int) else AUTO_MIN_PAGE
                if len(rows) < page_size or oldest is None:
                    break  # A short page: this window is done.
                next_before = oldest + 1 if new else oldest
                if next_before >= before and not new:
                    raise SourceError("archive ignored 'before'")
                before = next_before
        return out, False

    @staticmethod
    def _hash(kind: str, row: dict[str, Any]) -> str | None:
        """Hash title+selftext (posts) or body (comments); the text itself is dropped.

        ``[removed]`` and ``[deleted]`` placeholders are not content, so they hash to
        ``None`` (a removed post keeps the hash of its title).
        """

        def text_of(key: str) -> str:
            value = str(row.get(key) or "").strip()
            return "" if value in _PLACEHOLDER_TEXT else value

        if kind == "post":
            text = f"{text_of('title')}\n{text_of('selftext')}".strip()
        else:
            text = text_of("body")
        return content_hash(text) if text else None
