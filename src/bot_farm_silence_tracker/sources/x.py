"""X (formerly Twitter) adapter using the official API v2 with an app-only bearer token.

X bills per resource returned (pay-per-use): ``$0.005`` per post and ``$0.010`` per
user. This adapter therefore enforces a hard USD cap that must be set explicitly
(``max_usd``, or ``--max-usd`` on the CLI). Money is tracked in integer micro-USD.

How the budget guard works:

* Before every attempt, retries included (the HTTP client's ``before_attempt``
  hook), the adapter computes the request's worst case (``max_results`` posts, plus
  one user per post when the request expands authors) and raises
  :class:`BudgetExceeded` instead of sending a request that could push the
  estimate past the cap.
* With a spend ledger (``SourceConfig.ledger``), that check is atomic: the worst
  case is reserved with :meth:`SpendLedger.reserve` in one database transaction,
  so two scans sharing a database cannot both pass the cap. Each reservation is
  settled exactly once with :meth:`Reservation.settle`: with the actual charge when
  a response arrives, at the worst case after a transport error once the request
  may have reached X (or any other failure that leaves that unknown), and at 0
  when the request was never sent or the response returned nothing billable (429,
  402, 5xx). Without a ledger (library use), the same checks and charges run in
  memory, for this process only.
* Post and user ids are charged once per UTC day, matching X's same-day
  de-duplication; the pre-checks and reservations stay worst case.
* By default the cap holds per UTC day across reruns (``budget_scope=day``).
* Post and user ids are de-duplicated against the set of the UTC day the request
  was sent (X bills on that day). A response charged after UTC midnight is charged
  in full and seeds nothing for the new day.
* If a response returns more billable resources than its reserved worst case, the
  actual cost is recorded (never less), a warning is logged, and the scan stops at
  the next request once the cap is passed.
* Discovery has its own cap: 30% of the budget left in the scope when discovery
  starts, or ``max_discovery_usd`` when that is lower. Account lookups made during
  discovery count toward it. When it is reached, discovery stops with a warning and
  the scan goes on to fetch timelines for the pool found so far.
* Before an account's first timeline page, its projected pages (from
  ``public_metrics.tweet_count`` and age) are checked against what is left. An
  account that does not fit is recorded with status ``skipped_budget`` and the scan
  goes on; the scan stops only when not even one worst-case page fits. Pages paid
  for before a mid-account stop are cached, flagged truncated, with their real
  coverage (from the oldest post fetched); a later run fetches only the older part.
* Timelines that cannot matter are skipped at zero cost: accounts whose lifetime
  ``tweet_count`` is below ``--min-posts``, and accounts created after the cutoff.
  They are recorded with status ``skipped`` and ``meta["skipped"]``, and no activity
  is cached for them.

Options (``--source-option key=value``, all optional except a cap):

``max_usd``
    Hard spend cap in USD. Required for a paid scan; without it every request is
    refused and the CLI asks for one.
``max_discovery_usd``
    Cap on discovery spend (search and account lookups) in this run, in USD. The
    cap in force is the lower of this and 30% of the budget left when discovery
    starts, so this option can lower the default but not raise it.
``budget_scope``
    ``day`` (default): the cap covers everything recorded in the ledger for X on the
    current UTC day, across runs. ``run``: this run and every run it continues
    with ``--from-run`` (its resume chain), so resuming does not reset the cap.
``post_price`` / ``user_price``
    Override :data:`POST_PRICE_USD` / :data:`USER_PRICE_USD` (must be ``>= 0``).
``field_param``
    ``"tweet.fields"`` (default) or ``"post.fields"``; the docs disagree on the name.
``lang``
    If set, ``" lang:<code>"`` is appended to every discovery query.
``exclude_retweets``
    If true, ``" -is:retweet"`` is appended to every discovery query.
``full_archive``
    If true, discovery uses ``/2/tweets/search/all`` instead of recent search.
``placebo_discovery``
    If true (with ``full_archive``), each placebo cutoff gets a sample of its own,
    discovered by full-archive search in the window moved back by the same weeks and
    then fetched: that repeats the billed discovery and fetches ``--placebo-weeks``
    times. Off by default, so X runs have no placebo comparison unless it is asked
    for.

``sleep`` and ``clock`` (``time.sleep`` / ``time.time`` replacements) are constructor
keyword arguments, used by tests.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from bot_farm_silence_tracker.ledger import SPEND_SCOPES, Reservation
from bot_farm_silence_tracker.models import (
    PARTIAL_BUDGET,
    PARTIAL_ERROR,
    PARTIAL_FETCH_KEY,
    PARTIAL_PLATFORM_CAP,
    STATUS_SKIPPED,
    STATUS_SKIPPED_BUDGET,
    UNAVAILABLE_STATUSES,
    Account,
    Activity,
    content_hash,
)
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import (
    AccountSkipped,
    AccountUnavailable,
    BudgetExceeded,
    CredentialUse,
    HostUse,
    Source,
    SourceAuthError,
    SourceConfig,
    SourceError,
    SpendPlan,
)
from bot_farm_silence_tracker.sources.http import Attempt, reset_at_epoch
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, parse_datetime, utcnow

log = logging.getLogger(__name__)

API_BASE = "https://api.x.com/2"
BEARER_ENV = "BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN"

#: Micro-USD per USD. All money in this module is tracked as integer micro-USD.
MICRO = 1_000_000
#: USD per post resource returned (pay-per-use pricing, 2026-02).
POST_PRICE_USD = 0.005
#: USD per user resource returned, including users returned by an expansion.
USER_PRICE_USD = 0.010
#: Share of ``max_usd`` that discovery may spend when ``max_discovery_usd`` is unset.
DEFAULT_DISCOVERY_SHARE = 0.30

#: Largest page the adapter requests.
PAGE_SIZE = 100
#: The timeline may only reach back this many posts (UNVERIFIED).
TIMELINE_DEPTH_CAP = 3200
#: Most timeline pages fetched for one account. X stops at about
#: ``TIMELINE_DEPTH_CAP`` posts, which is this many full pages, plus a margin for
#: short pages; a timeline that keeps paging past it is cut there.
MAX_TIMELINE_PAGES = TIMELINE_DEPTH_CAP // PAGE_SIZE + 8
#: On a gap fetch whose newer posts cannot be estimated (no ``tweet_count`` change
#: to go on), the history counts as cut by the cap this many posts early.
TIMELINE_CAP_MARGIN = 160
#: X timestamps have whole-second precision. A timeline stopped part-way may hold only
#: some of the posts at its oldest second (see :meth:`XSource.fetch_activity`).
TIMESTAMP_RESOLUTION = timedelta(seconds=1)

#: Discovery projection: posts returned per new author found (repeat authors).
EXPECTED_POSTS_PER_NEW_AUTHOR = 1.5
#: Rough timeline projection before a sample exists: posts per account per day.
ASSUMED_POSTS_PER_DAY = 1.0
#: The high estimate assumes this many times the expected posts per account.
HEADROOM_FACTOR = 2.0
#: Suggested caps are the projection times this, rounded up to whole tens of USD.
SUGGESTION_FACTOR = 1.25

#: Recent search covers this many days back from now.
RECENT_SEARCH_DAYS = 7
#: Safety margin at the start of the recent-search window.
SEARCH_WINDOW_MARGIN = timedelta(hours=1)
#: Consecutive unexplained 403s on user timelines before the app is assumed refused.
MAX_UNEXPLAINED_403 = 3

FIELD_PARAMS = ("tweet.fields", "post.fields")
USER_FIELDS = "created_at,public_metrics,username"
TWEET_FIELDS = "created_at,in_reply_to_user_id,referenced_tweets"
#: Query-string name used to continue a search. UNVERIFIED: the docs also show
#: ``pagination_token`` for this endpoint.
SEARCH_PAGE_PARAM = "next_token"
MAX_QUERY_CHARS = 512

#: Markers of an app-level refusal (project not enrolled, wrong auth type).
APP_LEVEL_MARKERS = (
    "client-not-enrolled",
    "client-forbidden",
    "client forbidden",
    "unsupported-authentication",
    "unsupported authentication",
)
#: Markers of an exhausted usage cap or credit balance in an error body.
USAGE_CAP_MARKERS = (
    "usage-capped",
    "usagecapexceeded",
    "usage cap",
    "creditsdepleted",
    "credits depleted",
)

SKIP_LOW_TWEET_COUNT = "tweet_count_below_min_posts"
SKIP_CREATED_AFTER_CUTOFF = "created_after_cutoff"
SKIP_OVER_BUDGET = "projected_cost_exceeds_budget"
#: Meta keys a skip writes; removed again when the account is fetched.
SKIP_META_KEYS = ("skipped", "skip_min_posts", "skip_cutoff", "projected_posts", "budget_left_usd")
#: plan_spend asks for --yes when the budget left cannot cover discovery plus the
#: expected timeline cost of at least this share of the sample.
MIN_TIMELINE_SHARE = 0.10


def to_micro(usd: float) -> int:
    """Convert USD to integer micro-USD."""
    return round(usd * MICRO)


def fmt_usd(micro: int) -> str:
    """Format micro-USD as dollars with cents."""
    return f"${micro / MICRO:,.2f}"


def _round_up_usd(micro: float, step_usd: int = 10) -> int:
    """Round micro-USD up to a whole multiple of ``step_usd`` dollars (at least one)."""
    step = step_usd * MICRO
    return max(step_usd, math.ceil(micro / step) * step_usd)


@dataclass(frozen=True)
class EndpointLimit:
    """A per-app rate limit: ``calls`` per ``window`` seconds, at least ``min_interval`` apart."""

    calls: int
    window: float = 900.0
    min_interval: float = 0.0


#: Per-endpoint limits (requests per 15 minutes unless noted).
ENDPOINT_LIMITS: dict[str, EndpointLimit] = {
    "search_recent": EndpointLimit(450),
    "search_all": EndpointLimit(300, min_interval=1.0),  # Also 1 request per second.
    "timeline": EndpointLimit(10000),
    "users": EndpointLimit(300),
}


class UsdBudget:
    """Prices and running totals in integer micro-USD. The cap may be unset (``None``)."""

    def __init__(
        self,
        cap: float | None = None,
        post_price: float = POST_PRICE_USD,
        user_price: float = USER_PRICE_USD,
        discovery_cap: float | None = None,
    ) -> None:
        for label, value in (
            ("max_usd", cap),
            ("post_price", post_price),
            ("user_price", user_price),
            ("max_discovery_usd", discovery_cap),
        ):
            if value is not None and (value < 0 or math.isnan(value)):
                raise ValueError(f"{label} must be a number >= 0, got {value!r}")
        self.cap: int | None = None if cap is None else to_micro(cap)
        self.post_price = to_micro(post_price)
        self.user_price = to_micro(user_price)
        #: ``max_discovery_usd`` as given, or ``None``.
        self.explicit_discovery_cap: int | None = (
            None if discovery_cap is None else to_micro(discovery_cap)
        )
        #: The discovery cap in force; :meth:`XSource.discover` recomputes it from the
        #: spend already in the budget scope when discovery starts.
        self.discovery_cap: int | None = self.discovery_cap_for(0)
        self.spent = 0  # This process, micro-USD.
        self.discovery_spent = 0
        self.posts = 0
        self.users = 0
        self.repeats = 0  # Ids seen earlier the same UTC day, not charged again.
        self.failed_attempts = 0  # Transport failures charged at the worst case.
        self.overshoots = 0  # Responses that cost more than their reserved worst case.

    def discovery_cap_for(self, spent: int) -> int | None:
        """Discovery cap when ``spent`` micro-USD is already used in the budget scope.

        ``min(max_discovery_usd, share x (cap - spent))``: discovery never takes more
        than :data:`DEFAULT_DISCOVERY_SHARE` of the budget that is actually left, so
        the timelines keep the rest. ``max_discovery_usd`` can only lower it.
        """
        if self.cap is None:
            return self.explicit_discovery_cap
        share = round(max(0, self.cap - spent) * DEFAULT_DISCOVERY_SHARE)
        explicit = self.explicit_discovery_cap
        return share if explicit is None else min(explicit, share)

    def cost(self, posts: float = 0, users: float = 0) -> int:
        """Micro-USD for ``posts`` post resources and ``users`` user resources."""
        return round(posts * self.post_price + users * self.user_price)


class RateGate:
    """Sleeps as needed to respect per-endpoint request counts.

    ``x-rate-limit-reset`` is handled by the shared HTTP client's ``reset_wait``
    hook on a 429, not here.
    """

    def __init__(
        self,
        limits: Mapping[str, EndpointLimit] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.limits = dict(limits if limits is not None else ENDPOINT_LIMITS)
        self._sleep = sleep
        self._clock = clock
        self._calls: dict[str, deque[float]] = {}

    def wait(self, endpoint: str) -> None:
        """Block until a request to ``endpoint`` is allowed, then record it."""
        limit = self.limits.get(endpoint)
        history = self._calls.setdefault(endpoint, deque())
        now = self._clock()
        delay = 0.0
        if limit is not None:
            while history and history[0] <= now - limit.window:
                history.popleft()
            if len(history) >= limit.calls:
                delay = max(delay, history[0] + limit.window - now)
            if history and limit.min_interval:
                delay = max(delay, history[-1] + limit.min_interval - now)
        if delay > 0:
            self._sleep(delay)
            now = self._clock()
        history.append(now)


@dataclass(frozen=True)
class CostEstimate:
    """Projected cost of fetching timelines for a set of sampled accounts (micro-USD)."""

    accounts: int
    to_fetch: int
    skipped: int  # Skipped at zero cost (tweet_count below --min-posts, or too new).
    unavailable: int
    unknown_volume: int  # Accounts with no tweet_count or created_at.
    posts: int
    high_posts: int
    requests: int
    timeline_micro: int
    high_timeline_micro: int
    spent_micro: int
    cap_micro: int | None
    scope: str

    @property
    def total_micro(self) -> int:
        """Spend already recorded in the budget scope plus the expected timeline cost."""
        return self.spent_micro + self.timeline_micro

    @property
    def high_total_micro(self) -> int:
        return self.spent_micro + self.high_timeline_micro

    def verdict(self) -> str:
        if self.cap_micro is None:
            return "no cap set: pass --max-usd"
        if self.high_total_micro <= self.cap_micro:
            return "fits under the cap even at the high estimate"
        if self.total_micro <= self.cap_micro:
            return "may reach the cap: the scan could stop early with partial results"
        return "EXCEEDS the cap: the scan would stop early with partial results"

    def render(self) -> str:
        cap = fmt_usd(self.cap_micro) if self.cap_micro is not None else "not set"
        return "\n".join(
            [
                "X API cost projection (no timelines were fetched)",
                f"  accounts sampled:          {self.accounts:,} ({self.to_fetch:,} to fetch, "
                f"{self.skipped:,} skipped at zero cost, {self.unavailable:,} unavailable; "
                f"{self.unknown_volume:,} without a volume estimate)",
                f"  expected timeline posts:   {self.posts:,} (~{self.requests:,} requests); "
                f"high estimate {self.high_posts:,}",
                f"  expected timeline cost:    {fmt_usd(self.timeline_micro)} "
                f"(high {fmt_usd(self.high_timeline_micro)})",
                f"  already spent ({self.scope}): {fmt_usd(self.spent_micro)}",
                f"  expected total:            {fmt_usd(self.total_micro)} "
                f"(high {fmt_usd(self.high_total_micro)})",
                f"  spend cap:                 {cap} -> {self.verdict()}",
                "  Based on lifetime tweet_count and account age; recent volume may differ. "
                f"The high estimate assumes {HEADROOM_FACTOR:g}x the expected posts.",
            ]
        )


def _seen_now() -> str:
    """When a ``tweet_count`` was seen, for ``meta["tweet_count_at"]``.

    Second precision, like the cached window's fetch time it is compared with, so
    a count seen in the same second as the fetch never counts as seen after it.
    """
    return isoformat(utcnow())


def _parse_time(value: Any) -> datetime | None:
    """Parse a stored timestamp, or ``None`` when absent or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_datetime(value)
    except ValueError:
        return None


def _rfc3339(value: datetime, *, round_up: bool = False) -> str:
    """Format as RFC 3339 seconds; ``round_up`` moves a fractional second up, not down."""
    value = ensure_utc(value)
    if round_up and value.microsecond:
        value = value.replace(microsecond=0) + timedelta(seconds=1)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _text_of(error: Mapping[str, Any]) -> str:
    keys = ("title", "detail", "type", "message", "reason")
    return " ".join(str(error.get(k, "")) for k in keys).lower()


def _status_from(text: str) -> str:
    """Map error text to an unavailable status."""
    lowered = text.lower()
    if "suspend" in lowered:
        return "suspended"
    if "protected" in lowered or "not authorized" in lowered or "private" in lowered:
        return "private"
    return "deleted"


def _is_user_error(error: Mapping[str, Any]) -> bool:
    return error.get("resource_type") == "user"


def _is_auth_error(error: Mapping[str, Any]) -> bool:
    if _is_user_error(error):
        return False
    text = _text_of(error)
    return any(
        key in text
        for key in (
            "unauthorized",
            "invalid token",
            "authentication",
            *APP_LEVEL_MARKERS,
        )
    )


def _body_text(body: Mapping[str, Any]) -> str:
    """All error text in a body, lower-cased, for marker matching."""
    parts = [_text_of(body)]
    parts += [_text_of(e) for e in body.get("errors") or [] if isinstance(e, dict)]
    return " ".join(parts)


def _decode(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def classify_error(response: httpx.Response) -> SourceError | None:
    """HTTP-client classifier: a 402 or a usage-cap body means the money ran out."""
    body = _decode(response)
    detail = str(body.get("detail") or body.get("title") or response.reason_phrase)
    if response.status_code == 402:
        return BudgetExceeded(f"X API reports no credits left (402): {detail}")
    if any(marker in _body_text(body) for marker in USAGE_CAP_MARKERS):
        return BudgetExceeded(f"X API usage cap reached ({response.status_code}): {detail}")
    return None


def _classify_kind(tweet: Mapping[str, Any]) -> str:
    types = {ref.get("type") for ref in tweet.get("referenced_tweets") or []}
    if "retweeted" in types:
        return "repost"
    if "replied_to" in types or tweet.get("in_reply_to_user_id"):
        return "reply"
    if "quoted" in types:
        return "quote"
    return "tweet"


class XSource(Source):
    """Adapter for the X API v2 (pay-per-use) with a hard USD budget."""

    name: ClassVar[str] = "x"
    required_credentials: ClassVar[tuple[str, ...]] = (BEARER_ENV,)
    network_hosts: ClassVar[tuple[HostUse, ...]] = (
        HostUse(
            httpx.URL(API_BASE).host,
            "X API v2 (every request, billed per use)",
            credentials=(CredentialUse(BEARER_ENV, "Authorization: Bearer header"),),
        ),
    )
    default_rps: ClassVar[float] = 1.0
    default_burst: ClassVar[float] = 1.0
    #: Discovery is billed, so the pool is the sample size unless asked otherwise.
    default_pool_factor: ClassVar[float | None] = 1.0
    #: No option holds a secret: the bearer token is a credential, read from the
    #: environment. The budget options (max_usd, max_discovery_usd, budget_scope)
    #: must be stored so a resume inherits the cap.
    secret_options: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        config: SourceConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(config)
        self._token = config.credentials[self.required_credentials[0]]
        self._clock = clock
        self.field_param = self.option_str("field_param", "tweet.fields") or "tweet.fields"
        if self.field_param not in FIELD_PARAMS:
            raise ValueError(f"field_param must be one of {FIELD_PARAMS}, got {self.field_param!r}")
        self.lang = self.option_str("lang")
        self.exclude_retweets = self.option_bool("exclude_retweets")
        self.full_archive = self.option_bool("full_archive")
        self.scope = self.option_str("budget_scope", "day") or "day"
        if self.scope not in SPEND_SCOPES:
            raise ValueError(f"budget_scope must be one of {SPEND_SCOPES}, got {self.scope!r}")
        self.budget = UsdBudget(
            self.option_float("max_usd"),
            self.option_float("post_price", POST_PRICE_USD) or 0.0,
            self.option_float("user_price", USER_PRICE_USD) or 0.0,
            self.option_float("max_discovery_usd"),
        )
        self.gate = RateGate(sleep=sleep, clock=clock)
        self.http.configure(
            reset_wait=reset_at_epoch("x-rate-limit-reset", clock=clock),
            classify_error=classify_error,
        )
        self.ignored_errors: list[dict[str, Any]] = []
        self.skipped: Counter[str] = Counter()
        self._user_cache: dict[str, dict[str, Any] | AccountUnavailable] = {}
        self._unexplained_403 = 0  # Current streak.
        #: Accounts recorded private after an unexplained 403 in this run. If the run
        #: ends in an auth stop they are doubtful, and the pipeline retries them.
        self._unexplained_ids: list[str] = []
        self._discovering = False  # Account lookups now count toward the discovery cap.
        self.skipped_budget = 0
        self._charged_day: str | None = None
        self._charged_posts: set[str] = set()
        self._charged_users: set[str] = set()

    def __repr__(self) -> str:
        return f"XSource(scope={self.scope!r})"

    # ------------------------------------------------------------------ budget

    @property
    def scope_label(self) -> str:
        return "today across runs" if self.scope == "day" else "this run's resume chain"

    @property
    def estimated_spend_usd(self) -> float:
        """Estimated USD charged by this process (posts and users returned)."""
        return self.budget.spent / MICRO

    def _spent_micro(self) -> int:
        """Spend in the budget scope: the ledger when attached, else this process."""
        ledger = self.config.ledger
        if ledger is None:
            return self.budget.spent
        return max(to_micro(ledger.spent(self.scope)), self.budget.spent)

    def _over_cap(self, cap: int, spent: int, worst: int, what: str) -> BudgetExceeded:
        advice = (
            "raise --max-usd, or continue on the next UTC day with --from-run"
            if self.scope == "day"
            else "raise --max-usd"
        )
        return BudgetExceeded(
            f"X spend cap of {fmt_usd(cap)} ({self.scope_label}) would be exceeded by "
            f"the next {what} (spent {fmt_usd(spent)}, worst case {fmt_usd(worst)}); "
            f"{advice}"
        )

    def _check(self, worst: int, what: str) -> None:
        """Raise :class:`BudgetExceeded` if ``worst`` more micro-USD could pass the cap.

        Not atomic across processes; :meth:`_reserve` is, when a ledger is attached.
        """
        cap = self.budget.cap
        if cap is None:
            raise BudgetExceeded(
                "no X spend cap is set; pass --max-usd D (or --source-option max_usd=D)"
            )
        spent = self._spent_micro()
        if spent + worst > cap:
            raise self._over_cap(cap, spent, worst, what)

    def _reserve(self, worst: int, what: str) -> Reservation | None:
        """Check the cap and reserve ``worst`` in the ledger atomically.

        Returns ``None`` without a ledger (the in-memory :meth:`_check` is all there
        is then). The caller must settle the reservation exactly once.
        """
        self._check(worst, what)
        ledger = self.config.ledger
        if ledger is None:
            return None
        cap = self.budget.cap
        assert cap is not None  # _check refuses a missing cap.
        try:
            return ledger.reserve(worst / MICRO, cap / MICRO, self.scope)
        except BudgetExceeded:
            spent = to_micro(ledger.spent(self.scope))
            raise self._over_cap(cap, spent, worst, what) from None

    def _record(
        self, micro: int, *, discovery: bool, reservation: Reservation | None = None
    ) -> None:
        """Count ``micro`` as charged: settle ``reservation`` with it, or add it to the ledger.

        A reservation is settled even for 0, which releases it.
        """
        micro = max(0, micro)
        self.budget.spent += micro
        if discovery:
            self.budget.discovery_spent += micro
        if reservation is not None:
            reservation.settle(micro / MICRO)
        elif micro and self.config.ledger is not None:
            self.config.ledger.add(micro / MICRO)

    def _utc_day(self) -> str:
        """The current UTC day on the wall clock, ``YYYY-MM-DD``."""
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")

    def _roll_day(self) -> str:
        day = self._utc_day()
        if day != self._charged_day:
            self._charged_day = day
            self._charged_posts.clear()
            self._charged_users.clear()
        return day

    def _charge_items(
        self,
        posts: Sequence[Mapping[str, Any]],
        users: Sequence[Mapping[str, Any]],
        *,
        discovery: bool,
        reservation: Reservation | None = None,
        send_day: str | None = None,
    ) -> int:
        """Charge returned resources, once per id per UTC day (settling ``reservation``).

        X bills on the day the request was sent (``send_day``). Ids are de-duplicated
        only against the set for that day. When the response is charged on a later
        day than it was sent (a request that straddles UTC midnight), every id is
        charged and none is added to the new day's set, because X bills them again
        when they come back that day. Returns the micro-USD charged.
        """
        today = self._roll_day()
        straddles = send_day is not None and send_day != today
        new_posts = new_users = 0
        for items, seen, kind in (
            (posts, self._charged_posts, "post"),
            (users, self._charged_users, "user"),
        ):
            for item in items:
                key = str(item.get("id") or "")
                if key and not straddles:
                    if key in seen:
                        self.budget.repeats += 1
                        continue
                    seen.add(key)
                if kind == "post":
                    new_posts += 1
                else:
                    new_users += 1
        self.budget.posts += new_posts
        self.budget.users += new_users
        micro = self.budget.cost(new_posts, new_users)
        self._record(micro, discovery=discovery, reservation=reservation)
        return micro

    def _overshoot(self, charged: int, worst: int, what: str) -> None:
        """A response cost more than its reserved worst case: say so loudly.

        The actual cost is already recorded (never the reservation). Once the cap is
        passed, every later request is refused by :meth:`_check`, so the scan stops
        with :class:`BudgetExceeded`.
        """
        self.budget.overshoots += 1
        cap = self.budget.cap
        spent = self._spent_micro()
        over = cap is not None and spent > cap
        log.warning(
            "X returned more billable resources than the %s asked for: charged %s against "
            "a reserved worst case of %s (spent %s of the %s cap)%s",
            what,
            fmt_usd(charged),
            fmt_usd(worst),
            fmt_usd(spent),
            fmt_usd(cap) if cap is not None else "unset",
            "; the cap is passed and the scan stops" if over else "",
        )
        self.warn(
            "X returned more billable resources than requested; the actual cost was "
            "recorded, so spend can pass the cap by up to that excess. The scan stops "
            "once the cap is passed. Check the cost summary and the X billing console."
        )

    def _charge_worst(
        self, worst: int, *, discovery: bool, reservation: Reservation | None = None
    ) -> None:
        """Charge a failed attempt that may have been billed without a response."""
        self.budget.failed_attempts += 1
        self._record(worst, discovery=discovery, reservation=reservation)

    def cost_summary(self) -> str:
        """One line for the CLI, printed on every exit path. Never raises."""
        try:
            b = self.budget
            line = f"Estimated X API spend: {fmt_usd(b.spent)} this run"
            if self.config.ledger is not None and self.scope == "day":
                line += f" ({fmt_usd(self._spent_micro())} today across runs)"
            line += f" of {fmt_usd(b.cap)} cap" if b.cap is not None else " (no cap set)"
            line += (
                f" ({b.posts:,} posts, {b.users:,} users billed; discovery "
                f"{fmt_usd(b.discovery_spent)}; {b.repeats:,} repeats on the same UTC day "
                "not billed again"
            )
            if b.failed_attempts:
                line += f"; {b.failed_attempts} failed attempts charged at the worst case"
            if b.overshoots:
                line += f"; {b.overshoots} responses cost more than their worst case"
            if self.skipped:
                line += f"; {sum(self.skipped.values()):,} timelines skipped at zero cost"
            if self.skipped_budget:
                line += f"; {self.skipped_budget:,} timelines skipped as over budget"
            return line + ")"
        except Exception:  # pragma: no cover - must never raise
            return "Estimated X API spend: unavailable"

    # --------------------------------------------------------------- transport

    def _call(
        self,
        endpoint: str,
        path: str,
        params: Mapping[str, Any],
        *,
        worst_posts: int = 0,
        worst_users: int = 0,
        users_payload: bool = False,
        user_scope: bool = False,
        discovery: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one budgeted, rate-limited GET and return the decoded body.

        ``users_payload`` marks ``data`` as a list of users (billed at the user price).
        ``user_scope`` marks a per-user request, where a 403 or 404 or a user-level
        error means the account is unavailable instead of an auth failure.
        ``discovery`` attributes the spend to the discovery cap. ``user_id`` names
        the account of a per-user request.
        """
        worst = self.budget.cost(worst_posts, worst_users)
        what = f"{endpoint} request"
        self._check(worst, what)
        self.gate.wait(endpoint)
        # Reservation state per attempt (ledger only), keyed by Attempt.attempt. Core
        # gives each attempt whose before_attempt ran exactly one terminal hook:
        # on_response (402, 429 and 5xx settle at 0 here; the final response is
        # charged below), on_attempt_failed (it may have reached X: worst case) or
        # on_attempt_not_sent (never sent: released at 0). ``settled`` is the primary
        # guard against a second settle; Reservation.settle is idempotent as a backstop.
        reservations: dict[int, Reservation] = {}
        send_days: dict[int, str] = {}  # UTC day each attempt was sent: X bills that day.
        settled: set[int] = set()
        last: list[int] = []  # Number of the latest attempt that reserved.

        def finish(number: int, micro: int | None) -> None:
            """Terminal callback for one attempt: ``None`` means charge the worst case."""
            if number in settled:
                return
            settled.add(number)
            reservation = reservations.get(number)
            if micro is None:
                self._charge_worst(worst, discovery=discovery, reservation=reservation)
            elif reservation is not None:
                reservation.settle(micro / MICRO)

        def before_attempt(attempt: Attempt) -> None:
            if discovery and attempt.attempt > 0 and not self._discovery_room(worst):
                raise BudgetExceeded(
                    f"X discovery cap of {fmt_usd(self.budget.discovery_cap or 0)} would be "
                    f"exceeded by retrying a failed {what}"
                )
            # Reserving is the last step, so a refusal above leaves nothing to release.
            reservation = self._reserve(worst, what)
            send_days[attempt.attempt] = self._utc_day()
            last[:] = [attempt.attempt]
            if reservation is not None:
                reservations[attempt.attempt] = reservation

        def on_response(attempt: Attempt, response: httpx.Response) -> None:
            # These return no billable resources; settle now, before any retry.
            status = response.status_code
            if status in (402, 429) or status >= 500:
                finish(attempt.attempt, 0)

        def on_attempt_failed(attempt: Attempt, _error: httpx.HTTPError) -> None:
            # Sent but no usable response (transport error, undecodable body, redirect
            # loop, Ctrl-C mid-send): it may have reached X, so assume it was billed.
            finish(attempt.attempt, None)

        def on_attempt_not_sent(attempt: Attempt, _error: BaseException) -> None:
            finish(attempt.attempt, 0)  # Never reached X: release the reservation.

        def finish_rest(micro: int | None) -> None:
            for number in list(reservations) + last:
                finish(number, micro)

        try:
            response = self.http.get(
                f"{API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
                sends_credentials=True,
                before_attempt=before_attempt,
                on_response=on_response,
                on_attempt_failed=on_attempt_failed,
                on_attempt_not_sent=on_attempt_not_sent,
            )
        except BaseException:
            # The hooks settle every attempt. One still open here means a hook path
            # core does not cover (for example a hook that raised): unknown whether X
            # billed it, so charge the worst case rather than leak or under-count.
            finish_rest(None)
            raise

        number = last[0] if last else 0
        charged = 0
        try:
            try:
                raw = response.json()
            except ValueError:
                raw = None
            body = raw if isinstance(raw, dict) else {}
            if not isinstance(raw, dict) and response.status_code < 400:
                finish(number, None)  # A success we cannot read may still have been billed.
                raise SourceError(f"invalid JSON from X API {path}")

            data = [d for d in body.get("data") or [] if isinstance(d, dict)]
            includes = body.get("includes") or {}
            posts = [t for t in includes.get("tweets") or [] if isinstance(t, dict)]
            users = [u for u in includes.get("users") or [] if isinstance(u, dict)]
            if users_payload:
                users += data
            else:
                posts += data
            if number not in settled:
                charged = self._charge_items(
                    posts,
                    users,
                    discovery=discovery,
                    reservation=reservations.get(number),
                    send_day=send_days.get(number),
                )
                settled.add(number)
        finally:
            # Only reached unsettled on an unexpected error before the charge: assume
            # the response was billed.
            finish_rest(None)
        if charged > worst:
            self._overshoot(charged, worst, what)
        try:
            return self._check_body(response, body, path, user_scope=user_scope, user_id=user_id)
        except SourceAuthError as exc:
            # The run ends in an auth stop: earlier unexplained 403s may have been the
            # app being refused, not private accounts. The pipeline retries them.
            exc.retry_user_ids = tuple(self._unexplained_ids)
            raise

    def _check_body(
        self,
        response: httpx.Response,
        body: dict[str, Any],
        path: str,
        *,
        user_scope: bool,
        user_id: str | None,
    ) -> dict[str, Any]:
        """Raise for an error status or error entries in ``body``; return ``body``."""
        errors = [e for e in body.get("errors") or [] if isinstance(e, dict)]
        detail = "; ".join(str(e.get("detail") or e.get("title") or "") for e in errors)
        detail = detail or str(body.get("detail") or body.get("title") or response.reason_phrase)
        status = response.status_code
        if status == 401:
            raise SourceAuthError(f"X API refused the request (401): {detail}")
        if status in (403, 404) and user_scope:
            self._user_scope_refusal(status, body, errors, detail, user_id)
        if status == 403:
            raise SourceAuthError(f"X API refused the request (403): {detail}")
        if status >= 400:
            raise SourceError(f"X API error {status} for {path}: {detail}")
        if user_scope:
            # A per-user request went through, so the app is not refused: the earlier
            # unexplained 403s were private accounts, not doubtful results.
            self._unexplained_403 = 0
            self._unexplained_ids.clear()

        for error in errors:
            if _is_auth_error(error):
                raise SourceAuthError(f"X API auth error: {_text_of(error)}")
            if _is_user_error(error):
                if user_scope:
                    raise AccountUnavailable(_status_from(_text_of(error)), _text_of(error))
            else:
                self.ignored_errors.append(error)
                title = str(error.get("title") or "error")
                self.warn(
                    "X API returned partial errors that were ignored (such as missing "
                    f"referenced posts); first one: {title}"
                )
        return body

    def _user_scope_refusal(
        self,
        status: int,
        body: Mapping[str, Any],
        errors: Sequence[Mapping[str, Any]],
        detail: str,
        user_id: str | None = None,
    ) -> None:
        """Map a 403 or 404 on a per-user request. Always raises."""
        text = _body_text(body)
        if status == 403 and any(marker in text for marker in APP_LEVEL_MARKERS):
            raise SourceAuthError(f"X API refused the app (403): {detail}")
        user_level = status == 404 or any(_is_user_error(e) for e in errors)
        user_level = user_level or any(
            word in text for word in ("suspend", "protected", "not authorized", "not found")
        )
        if user_level:
            raise AccountUnavailable(_status_from(text), detail)
        self._unexplained_403 += 1
        if self._unexplained_403 >= MAX_UNEXPLAINED_403:
            raise SourceAuthError(
                f"X API returned {self._unexplained_403} unexplained 403 responses in a row "
                f"for user timelines, which points at the app, not the accounts: {detail}"
            )
        if user_id is not None:
            self._unexplained_ids.append(user_id)
        raise AccountUnavailable("private", f"X API returned 403 without a reason: {detail}")

    # ----------------------------------------------------------------- discover

    def _build_query(self, seed: str) -> str | None:
        """Return the search query for ``seed``, or ``None`` (with a warning) if invalid."""
        query = seed.strip()
        if not query:
            self.warn("Skipped an empty X search query.")
            return None
        if self.exclude_retweets:
            query += " -is:retweet"
        if self.lang:
            query += f" lang:{self.lang}"
        if len(query) > MAX_QUERY_CHARS:
            self.warn(
                f"Skipped an X search query longer than {MAX_QUERY_CHARS} characters: "
                f"{seed[:40]}..."
            )
            return None
        return query

    def _search_window(self) -> tuple[datetime | None, datetime] | None:
        """Return ``(start_time, end_time)`` for discovery, or ``None`` if it cannot work.

        ``end_time`` is the cutoff, so every discovered author posted before it.
        ``start_time`` is the baseline start when the search window reaches it.
        """
        cutoff = ensure_utc(self.config.cutoff)
        start = self.config.baseline_start
        if self.full_archive:
            return (start, cutoff)
        # Recent search reaches back from the real time of the request, not --now.
        wall = datetime.fromtimestamp(self._clock(), UTC)
        earliest = wall - timedelta(days=RECENT_SEARCH_DAYS)
        earliest += SEARCH_WINDOW_MARGIN
        if cutoff <= earliest:
            self.warn(
                f"X recent search only reaches back {RECENT_SEARCH_DAYS} days (to "
                f"{earliest:%Y-%m-%d %H:%M} UTC), which is after the cutoff "
                f"{cutoff:%Y-%m-%d %H:%M} UTC, so it cannot find accounts that posted "
                "before the cutoff. Discovery was skipped at no cost. Use --source-option "
                "full_archive=true (needs full-archive access) or reuse an earlier sample "
                "with --from-run."
            )
            return None
        if start is not None and ensure_utc(start) < earliest:
            start = None  # Outside the window; X then starts at now minus 7 days.
        return (start, cutoff)

    def _search_page(
        self, query: str, token: str | None, window: tuple[datetime | None, datetime]
    ) -> tuple[list[Account], str | None]:
        start, end = window
        params: dict[str, Any] = {
            "query": query,
            "max_results": PAGE_SIZE,
            "expansions": "author_id",
            "user.fields": USER_FIELDS,
            "end_time": _rfc3339(end),
        }
        if start is not None:
            params["start_time"] = _rfc3339(start)
        if token:
            params[SEARCH_PAGE_PARAM] = token
        endpoint = "search_all" if self.full_archive else "search_recent"
        body = self._call(
            endpoint,
            "/tweets/search/all" if self.full_archive else "/tweets/search/recent",
            params,
            worst_posts=PAGE_SIZE,
            worst_users=PAGE_SIZE,  # Up to one distinct author per post.
            discovery=True,
        )
        users = {
            str(u["id"]): u for u in (body.get("includes") or {}).get("users") or [] if "id" in u
        }
        accounts: list[Account] = []
        for post in body.get("data") or []:
            author = str(post.get("author_id", ""))
            if not author:
                continue
            found = users.get(author)
            accounts.append(
                Account("x", author, author) if found is None else self._account_from_user(found)
            )
        return accounts, (body.get("meta") or {}).get("next_token")

    def _account_from_user(self, user: Mapping[str, Any]) -> Account:
        user_id = str(user["id"])
        created = user.get("created_at")
        meta: dict[str, Any] = {}
        count = (user.get("public_metrics") or {}).get("tweet_count")
        if isinstance(count, int):
            meta["tweet_count"] = count
            meta["tweet_count_at"] = _seen_now()
        return Account(
            "x",
            user_id,
            str(user.get("username") or user_id),
            parse_datetime(created) if created else None,
            meta,
        )

    def _discovery_room(self, worst: int | None = None) -> bool:
        """Whether ``worst`` more (default: a worst-case search page) fits the discovery cap."""
        cap = self.budget.discovery_cap
        if cap is None:
            return True  # The main cap check refuses the request instead.
        if worst is None:
            worst = self.budget.cost(PAGE_SIZE, PAGE_SIZE)
        return self.budget.discovery_spent + worst <= cap

    def placebo_discovery_problem(self, seeds: SeedSet) -> str | None:
        """Placebo samples need full-archive search, and the user's opt-in because
        every one is billed."""
        del seeds
        if not self.full_archive:
            return (
                f"X recent search reaches back only {RECENT_SEARCH_DAYS} days, so it cannot "
                "discover the accounts active in earlier weeks (full-archive search can, "
                "with --source-option full_archive=true and placebo_discovery=true)"
            )
        if not self.option_bool("placebo_discovery"):
            return (
                "placebo samples on X repeat the billed discovery and fetches once per "
                "placebo week, so they are off unless asked for with --source-option "
                "placebo_discovery=true"
            )
        return None

    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        """Yield unique pre-cutoff post authors, taking one page from each query in turn.

        Stops with a warning when the discovery cap is reached. The cap is set when
        discovery starts, from the budget left in the scope at that moment (see
        :meth:`UsdBudget.discovery_cap_for`). Account lookups made while discovery
        runs count toward it.
        """
        self.budget.discovery_cap = self.budget.discovery_cap_for(self._spent_micro())
        self._discovering = True
        try:
            yield from self._discover(seeds, pool_limit)
        finally:
            self._discovering = False

    def _discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        window = self._search_window()
        if window is None:
            return
        queries: list[list[Any]] = []  # [query, next_token]
        for item in seeds.items:
            query = self._build_query(item)
            if query is not None:
                queries.append([query, None])
        seen: set[str] = set()
        yielded = 0
        while queries and yielded < pool_limit:
            still_open = []
            for state in queries:
                if not self._discovery_room():
                    cap = fmt_usd(self.budget.discovery_cap or 0)
                    self.warn(
                        f"X discovery stopped at its cap of {cap}"
                        f" after {yielded:,} of {pool_limit:,} accounts (search spend "
                        f"{fmt_usd(self.budget.discovery_spent)}); the sample is drawn from "
                        "that pool and timelines are still fetched. Raise --source-option "
                        "max_discovery_usd=D for a larger pool."
                    )
                    return
                accounts, state[1] = self._search_page(state[0], state[1], window)
                if state[1]:
                    still_open.append(state)
                for account in accounts:
                    if account.user_id in seen:
                        continue
                    seen.add(account.user_id)
                    yield account
                    yielded += 1
                    if yielded >= pool_limit:
                        return
            queries = still_open

    # ------------------------------------------------------------------ hydrate

    def _lookup_users(self, ids: Sequence[str]) -> None:
        discovery = self._discovering
        worst = self.budget.cost(users=len(ids))
        if discovery and not self._discovery_room(worst):
            raise BudgetExceeded(
                f"X discovery cap of {fmt_usd(self.budget.discovery_cap or 0)} reached by "
                f"account lookups during discovery (search and lookups spent "
                f"{fmt_usd(self.budget.discovery_spent)})"
            )
        body = self._call(
            "users",
            "/users",
            {"ids": ",".join(ids), "user.fields": USER_FIELDS},
            worst_users=len(ids),
            users_payload=True,
            discovery=discovery,
        )
        found = {str(u["id"]): u for u in body.get("data") or [] if "id" in u}
        problems: dict[str, str] = {}
        for error in body.get("errors") or []:
            if isinstance(error, dict):
                key = str(error.get("resource_id") or error.get("value") or "")
                problems[key] = _text_of(error)
        for user_id in ids:
            if user_id in found:
                self._user_cache[user_id] = found[user_id]
            else:
                text = problems.get(user_id, "")
                self._user_cache[user_id] = AccountUnavailable(_status_from(text), text or "")

    def hydrate(self, account: Account) -> Account:
        """Fill ``created_at``. No request when it is already set.

        Looks up only this account: padding the batch with other discovered ids
        would pay for users that may never be sampled.
        """
        if account.created_at is not None:
            return account
        if account.user_id not in self._user_cache:
            self._lookup_users([account.user_id])
        entry = self._user_cache[account.user_id]
        if isinstance(entry, AccountUnavailable):
            raise AccountUnavailable(entry.status, str(entry))
        created = entry.get("created_at")
        if not created:
            raise AccountUnavailable("deleted", "X returned no created_at for this user")
        account.created_at = parse_datetime(created)
        if entry.get("username"):
            account.handle = str(entry["username"])
        count = (entry.get("public_metrics") or {}).get("tweet_count")
        if isinstance(count, int) and "tweet_count" not in account.meta:
            account.meta["tweet_count"] = count
            account.meta["tweet_count_at"] = _seen_now()
        return account

    # ----------------------------------------------------------- fetch_activity

    def _skip_reason(self, account: Account) -> str | None:
        """Why fetching this timeline cannot matter, or ``None`` to fetch it."""
        cutoff = ensure_utc(self.config.cutoff)
        if account.created_at is not None and ensure_utc(account.created_at) >= cutoff:
            return SKIP_CREATED_AFTER_CUTOFF
        count = account.meta.get("tweet_count")
        min_posts = self.config.min_posts
        if min_posts is not None and isinstance(count, int) and count < min_posts:
            return SKIP_LOW_TWEET_COUNT
        return None

    def _expected_posts(self, account: Account, window_days: float) -> float | None:
        """Expected posts in the window from lifetime ``tweet_count`` and age, or ``None``."""
        count = account.meta.get("tweet_count")
        if account.created_at is None or not isinstance(count, int):
            return None
        age = (ensure_utc(self.config.now) - ensure_utc(account.created_at)).total_seconds()
        age_days = max(1.0, age / 86400)
        return min(float(count), count / age_days * window_days, float(TIMELINE_DEPTH_CAP))

    @staticmethod
    def _newer_posts(account: Account, prior: Any) -> tuple[int, bool]:
        """Estimate the posts newer than a cached window, for a gap fetch's cap count.

        Returns ``(posts, known)``. Not a gap fetch (no ``cached_until`` in
        ``prior``): ``(0, True)``, the timeline is read from ``until`` down.

        On a gap fetch the count is known only when the account's ``tweet_count``
        was seen (``meta["tweet_count_at"]``) after the cached window was fetched
        (``prior["cached_fetched_at"]``): the estimate is then its rise over the
        stored ``cached_tweet_count``. A count that fell (deleted posts) says
        nothing: ``(0, False)``. Otherwise (a count seen before the fetch, as on a
        resume that reuses an old sample) the estimate is the cached window's
        posting rate times the time from its end to this run's ``until``, flagged
        unknown. Whenever ``known`` is false the caller also applies
        :data:`TIMELINE_CAP_MARGIN`, so the posts allowed for are never fewer than
        that margin; erring high only flags a history truncated. A cached rate of 0,
        or an empty cached window, falls back to the margin alone.
        """
        if not isinstance(prior, dict) or not prior.get("cached_until"):
            return 0, True
        before = prior.get("cached_tweet_count")
        now = account.meta.get("tweet_count")
        seen = _parse_time(account.meta.get("tweet_count_at"))
        fetched = _parse_time(prior.get("cached_fetched_at"))
        fresh = seen is not None and fetched is not None and seen > fetched
        if fresh and isinstance(before, int) and isinstance(now, int):
            if now < before:
                return 0, False
            return now - before, True
        rate = prior.get("cached_rate")
        cached_until = _parse_time(prior.get("cached_until"))
        until = _parse_time(prior.get("until"))
        if not isinstance(rate, int | float) or cached_until is None or until is None:
            return 0, False
        days = max(0.0, (until - cached_until).total_seconds() / 86400)
        return math.ceil(rate * days), False

    def _skip(self, account: Account, reason: str) -> AccountSkipped:
        """Record a zero-cost skip on ``account`` and return the error to raise."""
        meta: dict[str, Any] = {"skipped": reason}
        if reason == SKIP_LOW_TWEET_COUNT:
            meta["skip_min_posts"] = self.config.min_posts
        else:
            meta["skip_cutoff"] = isoformat(self.config.cutoff)
        account.meta.update(meta)
        self.skipped[reason] += 1
        self.warn(
            "Skipped some X timelines at zero cost because they cannot be cliffs "
            "(lifetime tweet_count below --min-posts, or created after the cutoff); "
            "they are recorded with status 'skipped' and no activity is cached for them."
        )
        return AccountSkipped(STATUS_SKIPPED, f"timeline not fetched: {reason}", meta)

    def _budget_gate(self, account: Account, since: datetime, until: datetime) -> None:
        """Decide before the first page whether this timeline fits the budget left.

        Raises :class:`BudgetExceeded` (the scan stops) when the budget left cannot
        pay for even one worst-case page, and :class:`AccountSkipped` with status
        ``skipped_budget`` (the scan goes on) when this account's projected pages do
        not fit but a smaller account still could.
        """
        page = self.budget.cost(PAGE_SIZE)
        cap = self.budget.cap
        if cap is None:
            self._check(page, "timeline request")  # Raises: no cap set.
            return
        spent = self._spent_micro()
        left = cap - spent
        if left < page:
            raise self._over_cap(cap, spent, page, "timeline page")
        expected = self._expected_posts(account, (until - since).total_seconds() / 86400)
        if expected is None:
            return
        projected = math.ceil(expected)
        # Finishing needs room for every full page plus the last page's worst case.
        need = self.budget.cost(max(1, math.ceil(projected / PAGE_SIZE)) * PAGE_SIZE)
        if need <= left:
            return
        meta = {
            "skipped": SKIP_OVER_BUDGET,
            "projected_posts": projected,
            "budget_left_usd": round(left / MICRO, 6),
        }
        account.meta.update(meta)
        self.skipped_budget += 1
        self.warn(
            "Skipped some X timelines whose projected cost does not fit the budget left "
            "(status 'skipped_budget'); the scan went on with smaller accounts. Raise "
            "--max-usd and continue with --from-run to fetch them."
        )
        raise AccountSkipped(
            STATUS_SKIPPED_BUDGET,
            f"timeline of {account.handle} (projected {projected:,} posts, worst case "
            f"{fmt_usd(need)}) does not fit the {fmt_usd(left)} left under the cap",
            meta,
        )

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        """Return the account's posts with ``since <= created_at < until``.

        Raises :class:`AccountSkipped` instead of fetching when the timeline cannot
        matter or does not fit the budget. When a later page fails (for example the
        cap is reached mid-account), the pages already paid for go with the error as
        ``partial`` and ``meta["truncated"]`` is set, so the pipeline caches them.

        Timelines come newest first, so a fetch that stops early covers
        ``[oldest returned, until)``. That start goes in ``meta["partial_fetch"]``
        (``since``, with ``posts`` returned and the ``reason``: ``budget``, ``error``
        or ``platform_cap`` when X stops at about 3,200 posts). The pipeline caches
        that coverage and later fetches only the older part, with ``until`` set to
        where the cached data starts.

        Posts at the oldest returned second may be split across the stop: some on
        the pages fetched, some on the page that was not. The pipeline refetches that
        second on resume (``GAP_OVERLAP``) whenever the cached window starts after
        ``since``. When the oldest returned second is ``since`` itself, a window
        starting there would read as complete and the unfetched posts at that second
        would never be fetched. So in that case the posts at ``since`` are dropped
        from ``partial`` and ``since`` in ``meta["partial_fetch"]`` is the next
        second: the resume buys ``[since, since + 2s)``, the split second plus the
        ``GAP_OVERLAP`` second, instead of losing posts. The shift is skipped when
        ``since`` plus one second would pass ``until``.

        On such a gap fetch, ``meta["partial_fetch"]
        ["posts"]`` (set by the pipeline: the cached posts above the gap) counts
        toward the 3,200-post cap, and so do the posts newer than the cached window
        (see :meth:`_newer_posts`).
        """
        self._discovering = False
        since, until = ensure_utc(since), ensure_utc(until)
        for key in SKIP_META_KEYS:
            account.meta.pop(key, None)
        reason = self._skip_reason(account)
        if reason is not None:
            raise self._skip(account, reason)
        self._budget_gate(account, since, until)
        prior = account.meta.pop(PARTIAL_FETCH_KEY, None)
        earlier_posts = int(prior.get("posts") or 0) if isinstance(prior, dict) else 0
        newer_posts, newer_known = self._newer_posts(account, prior)
        depth = TIMELINE_DEPTH_CAP - (0 if newer_known else TIMELINE_CAP_MARGIN)

        def mark_partial(reason: str, oldest: datetime | None, returned: int) -> None:
            account.meta[PARTIAL_FETCH_KEY] = {
                "reason": reason,
                "since": isoformat(oldest if oldest is not None else until),
                "posts": earlier_posts + returned,
            }

        params: dict[str, Any] = {
            "start_time": _rfc3339(since),
            "end_time": _rfc3339(until, round_up=True),
            "max_results": PAGE_SIZE,
            self.field_param: TWEET_FIELDS,
        }
        out: list[Activity] = []
        returned = 0
        pages = 0
        oldest: datetime | None = None
        token: str | None = None
        tokens_seen: set[str] = set()
        try:
            while True:
                if token:
                    params["pagination_token"] = token
                body = self._call(
                    "timeline",
                    f"/users/{account.user_id}/tweets",
                    params,
                    worst_posts=PAGE_SIZE,
                    user_scope=True,
                    user_id=account.user_id,
                )
                pages += 1
                for post in body.get("data") or []:
                    returned += 1
                    created = post.get("created_at")
                    if not created:
                        continue
                    ts = parse_datetime(created)
                    oldest = ts if oldest is None or ts < oldest else oldest
                    if not since <= ts < until:
                        continue
                    text = post.get("text")
                    out.append(
                        Activity(
                            "x",
                            account.user_id,
                            ts,
                            _classify_kind(post),
                            None,
                            content_hash(text) if isinstance(text, str) else None,
                        )
                    )
                token = (body.get("meta") or {}).get("next_token")
                why = None
                if token and token in tokens_seen:
                    # A page again: following it would loop, paying for every page.
                    why = "repeated a page token"
                elif token and pages >= MAX_TIMELINE_PAGES:
                    why = f"kept paging past {MAX_TIMELINE_PAGES} pages"
                if why is not None:
                    return self._stop_paging(
                        account, out, (since, until, oldest), returned, mark_partial, why
                    )
                if token:
                    tokens_seen.add(token)
                if not token:
                    # X stops at about the latest 3,200 posts: if it returned that many
                    # and the oldest is still after ``since``, the history did not reach.
                    # Errs toward "capped": ``returned`` counts every post X sent, and
                    # an unknown number of newer posts lowers the bar by a margin. A
                    # false "capped" only flags the result truncated (and stops a later
                    # run from buying the gap again); a missed one would score a cut
                    # history as complete.
                    seen = earlier_posts + newer_posts + returned
                    capped = seen >= depth and (oldest is None or oldest > since)
                    account.meta["truncated"] = capped
                    if capped:
                        mark_partial(PARTIAL_PLATFORM_CAP, oldest, returned)
                    return out
        except AccountUnavailable:
            raise
        except SourceError as exc:
            if pages:
                # Pages already paid for: hand them over, flagged truncated.
                account.meta["truncated"] = True
                reason = PARTIAL_BUDGET if isinstance(exc, BudgetExceeded) else PARTIAL_ERROR
                if oldest is not None and oldest == since:
                    # The posts at ``since`` may be split across the stop, and a window
                    # starting at ``since`` would never be resumed: claim coverage only
                    # from the next second, so the resume refetches all of them. The
                    # shift is clamped to ``until``: when the window is shorter than
                    # the timestamp resolution nothing is claimed (the recorded start
                    # equals ``until``, never past it, because the record is truncated
                    # to the second and the pipeline never passes a window shorter than
                    # 1s) and the resume fetches it all.
                    oldest = min(oldest + TIMESTAMP_RESOLUTION, until)
                    out = [a for a in out if a.ts >= oldest]
                mark_partial(reason, oldest, returned)
                exc.partial = list(out)
            raise

    def _stop_paging(
        self,
        account: Account,
        out: list[Activity],
        window: tuple[datetime, datetime, datetime | None],
        returned: int,
        mark_partial: Callable[[str, datetime | None, int], None],
        why: str,
    ) -> list[Activity]:
        """End a timeline that would page for ever: keep the pages already paid for.

        ``window`` is ``(since, until, oldest)``, the oldest being the oldest post
        returned. The history is flagged truncated and recorded as a partial fetch,
        like one stopped by an error (with the same shift when the oldest post is at
        ``since``), so a later run fetches the rest instead of scoring it as
        complete.
        """
        since, until, oldest = window
        if oldest is not None and oldest == since:
            oldest = min(oldest + TIMESTAMP_RESOLUTION, until)
            out = [a for a in out if a.ts >= oldest]
        self.warn(
            f"X timeline of account {account.user_id} {why}; stopped paging after "
            f"{returned:,} posts and kept them as a partial history."
        )
        account.meta["truncated"] = True
        mark_partial(PARTIAL_ERROR, oldest, returned)
        return out

    # ----------------------------------------------------------------- estimate

    def plan_spend(self, seeds: SeedSet, *, n: int, pool_limit: int, resume: bool) -> SpendPlan:
        """Project discovery and timeline cost before any request; refuse without a cap.

        Asks for ``--yes`` (``SpendPlan.confirm``) when the budget left in the scope
        cannot cover the projected discovery plus the expected timelines of at least
        :data:`MIN_TIMELINE_SHARE` of the sample.
        """
        b = self.budget
        baseline = self.config.baseline_start or ensure_utc(self.config.cutoff) - timedelta(days=30)
        window_days = (ensure_utc(self.config.now) - ensure_utc(baseline)).total_seconds() / 86400
        per_account = b.cost(EXPECTED_POSTS_PER_NEW_AUTHOR, 1)
        spent = self._spent_micro()
        discovery_cap = b.discovery_cap_for(spent)
        discovery = 0 if resume else per_account * pool_limit
        if discovery_cap is not None:
            discovery = min(discovery, discovery_cap)
        timelines = b.cost(n * window_days * ASSUMED_POSTS_PER_DAY)
        min_accounts = math.ceil(n * MIN_TIMELINE_SHARE)
        min_timelines = b.cost(min_accounts * window_days * ASSUMED_POSTS_PER_DAY)
        lines: list[str] = []
        if not resume:
            cap_note = (
                f", and it stops at its cap of {fmt_usd(discovery_cap)}"
                if discovery_cap is not None
                else ""
            )
            lines.append(
                f"X discovery (billed): a pool of up to {pool_limit:,} accounts from "
                f"{len(seeds.items)} queries, projected at about "
                f"{fmt_usd(per_account * pool_limit)} ({fmt_usd(per_account)} per account)"
                f"{cap_note}."
            )
            if discovery_cap is not None and per_account * pool_limit > discovery_cap:
                reach = discovery_cap // per_account if per_account else pool_limit
                lines.append(
                    f"  Discovery will probably stop at its cap after about {reach:,} accounts."
                )
        lines.append(
            f"X timelines: {n:,} accounts over {window_days:.0f} days, roughly "
            f"{fmt_usd(timelines)} at an assumed {ASSUMED_POSTS_PER_DAY:g} post per account "
            "per day. --dry-run-cost projects it from the real sample."
        )
        if spent:
            lines.append(f"X spend already in the ledger ({self.scope_label}): {fmt_usd(spent)}.")
        refuse = confirm = None
        page = b.cost(PAGE_SIZE, PAGE_SIZE)
        if b.cap is None:
            suggestion = _round_up_usd((discovery + timelines) * SUGGESTION_FACTOR)
            refuse = (
                "X scans need an explicit spend cap. The rough projection for this scan is "
                f"{fmt_usd(discovery + timelines)}; pass for example --max-usd {suggestion} "
                f"(or --source-option max_usd={suggestion}), or lower --n to spend less."
            )
        else:
            left = max(0, b.cap - spent)
            lines.append(
                f"X spend cap: {fmt_usd(b.cap)} ({self.scope_label}), {fmt_usd(left)} of it "
                f"left; discovery may use {fmt_usd(discovery_cap or 0)}."
            )
            if not resume and discovery_cap is not None and discovery_cap < page:
                refuse = (
                    f"The X discovery cap in force ({fmt_usd(discovery_cap)}: "
                    f"{DEFAULT_DISCOVERY_SHARE:.0%} of the {fmt_usd(left)} left under the "
                    f"{fmt_usd(b.cap)} cap, or max_discovery_usd if lower) is below one "
                    f"worst-case search page ({fmt_usd(page)}: {PAGE_SIZE} posts plus "
                    f"{PAGE_SIZE} authors), so discovery could not make a single request. "
                    "Raise --max-usd (or max_discovery_usd), or continue a stored sample "
                    "with --from-run RUN_ID."
                )
            elif left < discovery + min_timelines:
                what = "" if resume else f"the projected discovery ({fmt_usd(discovery)}) plus "
                confirm = (
                    f"Only {fmt_usd(left)} of the {fmt_usd(b.cap)} X spend cap is left "
                    f"({self.scope_label}, {fmt_usd(spent)} already spent). That cannot "
                    f"cover {what}the timelines of {MIN_TIMELINE_SHARE:.0%} of the "
                    f"{n:,}-account sample ({min_accounts:,} accounts, about "
                    f"{fmt_usd(min_timelines)}), so the scan would stop early with few "
                    "results. Raise --max-usd, lower --n, or wait for the next UTC day."
                )
        return SpendPlan(
            lines=tuple(lines),
            paid_discovery=bool(b.post_price or b.user_price),
            refuse=refuse,
            confirm=confirm,
        )

    def cost_projection(self, accounts: Sequence[Account], since: datetime, until: datetime) -> str:
        return self.estimate_cost(accounts, since, until).render()

    def estimate_cost(
        self, accounts: Sequence[Account], since: datetime, until: datetime
    ) -> CostEstimate:
        """Project the timeline cost from ``tweet_count`` and account age, with no requests.

        Expected posts per account in the window is ``tweet_count / age_days *
        window_days``, capped at ``tweet_count`` and :data:`TIMELINE_DEPTH_CAP`; the
        high estimate is :data:`HEADROOM_FACTOR` times that, under the same caps.
        Accounts missing either input are priced at the mean of the others (expected)
        and their maximum (high), or at :data:`ASSUMED_POSTS_PER_DAY` when no account
        has an estimate. Unavailable accounts and accounts that would be skipped cost
        nothing.
        """
        window_days = (ensure_utc(until) - ensure_utc(since)).total_seconds() / 86400
        expected: list[float] = []
        high: list[float] = []
        unknown = skipped = unavailable = 0
        for account in accounts:
            if account.status in UNAVAILABLE_STATUSES:
                unavailable += 1
                continue
            if self._skip_reason(account) is not None:
                skipped += 1
                continue
            posts = self._expected_posts(account, window_days)
            if posts is None:
                unknown += 1
                continue
            count = account.meta["tweet_count"]
            expected.append(posts)
            high.append(min(float(count), posts * HEADROOM_FACTOR, float(TIMELINE_DEPTH_CAP)))
        if expected:
            mean, top = sum(expected) / len(expected), max(high)
        else:
            mean = ASSUMED_POSTS_PER_DAY * window_days
            top = min(mean * HEADROOM_FACTOR, float(TIMELINE_DEPTH_CAP))
        per_expected = expected + [mean] * unknown
        per_high = high + [top] * unknown
        posts_total = round(sum(per_expected))
        high_total = round(sum(per_high))
        requests = sum(max(1, math.ceil(p / PAGE_SIZE)) for p in per_expected)
        return CostEstimate(
            accounts=len(accounts),
            to_fetch=len(per_expected),
            skipped=skipped,
            unavailable=unavailable,
            unknown_volume=unknown,
            posts=posts_total,
            high_posts=high_total,
            requests=requests,
            timeline_micro=self.budget.cost(posts_total),
            high_timeline_micro=self.budget.cost(high_total),
            spent_micro=self._spent_micro(),
            cap_micro=self.budget.cap,
            scope=self.scope_label,
        )
