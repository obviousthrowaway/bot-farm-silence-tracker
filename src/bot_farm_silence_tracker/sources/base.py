"""The platform adapter interface.

To add a platform, subclass :class:`Source`, set ``name`` and
``required_credentials``, implement the three abstract methods, and register the
``module:Class`` path in :mod:`bot_farm_silence_tracker.sources.registry`. The CLI constructs the
adapter as ``YourSource(config)`` where ``config`` is a :class:`SourceConfig`.

Rules for adapters:

* Use official platform APIs or openly published research archives through their
  documented APIs, within their terms. Never scrape platform websites, spoof user
  agents, or circumvent access controls or rate limits. Make every request
  through ``self.http`` (the shared :class:`~bot_farm_silence_tracker.sources.http.HttpClient`)
  so rate limits and the request budget are enforced.
* Do not add your own pacer. Set ``default_rps`` / ``default_burst`` and the CLI
  sizes the shared client's token bucket from them. For platform reset headers,
  register a ``reset_wait`` hook with ``self.http.configure(...)`` instead of
  sleeping yourself.
* Never keep raw content. Hash it with :func:`bot_farm_silence_tracker.models.content_hash`.
* Raise the exceptions from :mod:`bot_farm_silence_tracker.sources.errors`; do not swallow them.
  A plain :class:`SourceError` from ``hydrate`` or ``fetch_activity`` is treated as
  a per-account failure (the account is recorded with status ``error`` and the scan
  continues). :class:`SourceAuthError`, :class:`BudgetExceeded` and
  :class:`RateLimited` stop the scan, which is saved as a partial or failed run.
* Report anything the user should know but that is not an error (a skipped or
  invalid seed, discovery that could not page back to the cutoff, ignored API
  warnings) with :meth:`Source.warn`. Warnings are shown after the scan and stored
  in the run summary.
* Keep timestamps timezone-aware UTC.
* Do not read ``os.environ`` yourself. Credentials arrive in ``config.credentials``.
* Declare every host the adapter contacts in ``network_hosts`` (see "Network"
  below). The shared client refuses every other host.
* Pass ``sends_credentials=True`` or ``False`` on every ``self.http.request``,
  ``get`` and ``get_json`` call: ``True`` whenever the request carries a credential
  or a session token in any form (an ``Authorization`` header, HTTP Basic auth, a
  login body, a key in the URL). Leaving it out raises ``TypeError``. The client
  also detects the common forms itself (see "Network"), but the flag is what an
  adapter promises; never rely on the detection alone.
* Name every option that decides where credentials are sent (Bluesky's
  ``pds_url``) in ``credential_destination_options``. Such an option is never
  inherited by ``--from-run``; it has to be passed again.

Environment variables
---------------------
Every credential environment variable is named ``BOT_FARM_SILENCE_TRACKER_<PLATFORM>_<NAME>``,
for example ``BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID`` or
``BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN``. List the must-have ones in
``required_credentials`` and the nice-to-have ones (such as
``BOT_FARM_SILENCE_TRACKER_REDDIT_PASSWORD``) in ``optional_credentials``; the CLI reads both
and passes the values that are set in ``config.credentials``, keyed by the full
variable name.
An adapter may set ``env_platform`` to use a short platform name in its variables
(Bluesky uses ``BSKY``, so ``BOT_FARM_SILENCE_TRACKER_BSKY_HANDLE``).

Options
-------
Non-secret adapter settings arrive in ``config.options``. From the CLI they come
from the repeatable ``--source-option key=value`` flag, so every value is a
string; use :meth:`Source.option_bool`, :meth:`Source.option_float`,
:meth:`Source.option_int` and :meth:`Source.option_str` to read them. Do not
accept secrets as options (they would show up in shell history); use
``optional_credentials`` for those.

Network
-------
``network_hosts`` lists every host the adapter sends requests to, as
:class:`HostUse` entries: the exact host name, what it is used for, which
credentials (by environment variable name) and which session tokens are sent to it,
and whether it is contacted only with a login. ``allowed_hosts`` is derived from it.
:meth:`Source.__init__` installs the allowlist on the shared client
(:meth:`HttpClient.restrict_hosts`), so a request to any other host, a plain
``http`` URL, or a credential-carrying request to a host that does not receive
credentials raises :class:`DisallowedHostError` and is never sent. An adapter whose
hosts depend on its options (Bluesky's ``pds_url``) overrides
:meth:`Source.host_uses`. The ``network`` command prints the same data, so the
documentation cannot drift from what is enforced.

Enforcement is per host and, for secret credentials from the environment, per
credential: :meth:`Source.__init__` also binds each secret credential's value (a
:class:`CredentialUse` with ``secret=True``, the default) to the hosts whose
:class:`HostUse` names it (:meth:`HttpClient.bind_credential_values`), so a request
to another host that contains the value is refused even if that host receives other
credentials. Session tokens obtained at run time are held to the credential hosts;
an adapter with more than one credential host binds each token to its own host as
soon as it has it (Reddit's access token).

Only https on the default port 443 is accepted. Before an allowlist is installed the
client refuses every request, unless it was built with ``unrestricted=True`` (tests
and tools only). The client ignores proxy and CA environment variables unless it was
built with ``trust_env=True`` (the CLI's ``--trust-env``).

Spend
-----
A paid adapter records its estimated spend in ``config.ledger`` (a
:class:`bot_farm_silence_tracker.ledger.SpendLedger`, or ``None`` without storage) so a USD cap
holds across reruns and resumed runs. Prefer :meth:`SpendLedger.reserve` before a
billed request and :meth:`Reservation.settle` after it: the check and the record are
one atomic step, so two concurrent scans cannot both pass the cap. The adapter
returns a one-line summary from :meth:`Source.cost_summary`,
which the CLI prints on every exit path.

Before any request, the CLI asks the adapter for a :class:`SpendPlan` with
:meth:`Source.plan_spend`. A paid adapter uses it to print its projected cost, to
refuse a scan that has no explicit spend cap, and to say that discovery is billed,
in which case the CLI requires ``--yes`` before a scan that runs discovery.
``scan --dry-run-cost`` stores the sample without fetching activity and prints
:meth:`Source.cost_projection` for it.
"""

from __future__ import annotations

import abc
import contextlib
import logging
import random
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, ClassVar

import httpx

from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import Account, Activity
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.errors import (
    AccountSkipped,
    AccountUnavailable,
    BudgetExceeded,
    DisallowedHostError,
    RateLimited,
    SourceAuthError,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import HttpClient, redact_userinfo

__all__ = [
    "ENV_PREFIX",
    "AccountSkipped",
    "AccountUnavailable",
    "BudgetExceeded",
    "CredentialUse",
    "DisallowedHostError",
    "HostUse",
    "NewestActivity",
    "RateLimited",
    "Source",
    "SourceAuthError",
    "SourceConfig",
    "SourceError",
    "SpendPlan",
]

log = logging.getLogger(__name__)

#: Prefix of every environment variable Bot Farm Silence Tracker reads.
ENV_PREFIX = "BOT_FARM_SILENCE_TRACKER_"

_TRUE = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE = frozenset({"0", "false", "no", "off", "n", "f"})


@dataclass(frozen=True)
class SourceConfig:
    """Everything the CLI hands to an adapter's constructor.

    ``credentials`` and ``options`` are excluded from ``repr`` so secrets never end
    up in logs or tracebacks.

    Attributes:
        http: Shared HTTP client (rate limited, budgeted, retrying). Use it for
            every network call.
        credentials: Values for the adapter's ``required_credentials`` and any set
            ``optional_credentials``, keyed by environment variable name, for example
            ``{"BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN": "..."}``.
        now: The scan's reference time (UTC). Use it as the upper bound of lookups.
        cutoff: The scan's cutoff time (UTC). Discovery must use it: see
            :meth:`Source.discover`.
        seed: The ``--seed`` value, if any. Adapters may use it to make any internal
            randomness reproducible.
        options: Free-form extras from ``--source-option key=value`` (string values)
            or from library callers (any type), for example ``{"population": 2000}``
            for the fake source.
        baseline_start: Start of the scoring baseline window, ``cutoff -
            baseline_days``. Discovery should prefer accounts active in
            ``[baseline_start, cutoff)``. ``None`` when the caller does not know it.
        ledger: Persistent spend ledger for paid platforms, or ``None``.
        min_posts: The scoring ``--min-posts`` threshold, or ``None`` when unknown. A
            paid adapter may skip fetching an account whose lifetime post count
            already shows it cannot reach it.
        active_within_days: ``--active-within-days D``: discovery samples authors
            active in the last D days before the scan's effective now, clipped to
            ``[baseline_start, cutoff)``, instead of in that whole window. ``None``
            (the default) keeps the baseline window. Only adapters with
            ``supports_active_within`` honor it; the CLI refuses it for the others.
        concurrency: ``--concurrency K``: how many accounts the pipeline fetches at
            once, each in its own worker thread. 1 (the default) is the sequential
            path. The CLI refuses more than the adapter's ``max_concurrency``. An
            adapter that allows more than 1 must make ``hydrate``, ``fetch_activity``
            and every hook it registers on ``http`` safe to call from several threads
            at once; it may read this to switch on behavior that only matters then.
    """

    http: HttpClient
    credentials: Mapping[str, str] = field(repr=False)
    now: datetime
    cutoff: datetime
    seed: int | None = None
    options: Mapping[str, Any] = field(default_factory=dict, repr=False)
    baseline_start: datetime | None = None
    ledger: SpendLedger | None = None
    min_posts: int | None = None
    active_within_days: float | None = None
    concurrency: int = 1


@dataclass(frozen=True)
class CredentialUse:
    """A credential sent to a host: its environment variable name and how it is sent.

    Attributes:
        env: The environment variable that holds the credential.
        how: How it is sent, in a few words.
        secret: Whether the value is a secret (a password, an app password, a client
            secret, a bearer, access or refresh token). Only secrets are bound to
            their hosts (see :meth:`HttpClient.bind_credential_values`). A public
            identifier (a handle, a user name, an OAuth client id) is not a secret:
            it may legitimately appear in requests to other hosts, for example as
            the account being scanned, so binding it would refuse ordinary requests.
    """

    env: str
    how: str
    secret: bool = True


@dataclass(frozen=True)
class HostUse:
    """One host an adapter contacts. The host allowlist and ``network`` read these.

    Attributes:
        host: The exact host name (no scheme, port or path). Always reached over https.
        purpose: What the adapter uses the host for, in a few words.
        credentials: Credentials from the environment that are sent to this host.
        tokens: Session tokens sent to this host that were obtained with those
            credentials (they never come from the environment).
        login_only: The host is contacted only when login credentials are set.
    """

    host: str
    purpose: str
    credentials: tuple[CredentialUse, ...] = ()
    tokens: tuple[str, ...] = ()
    login_only: bool = False

    @property
    def receives_credentials(self) -> bool:
        """Whether requests to this host may carry credentials or session tokens."""
        return bool(self.credentials or self.tokens)


@dataclass(frozen=True)
class SpendPlan:
    """What a paid adapter expects a scan to cost, returned before any request.

    Attributes:
        lines: Human-readable projection lines, printed before the scan starts.
        paid_discovery: Discovery will spend money. The CLI then requires ``--yes``
            for any scan that runs discovery (that is, without ``--from-run``).
        refuse: When set, the CLI refuses to start and prints this message (for
            example because no spend cap was given).
        confirm: When set, the CLI prints this warning and refuses to start unless
            ``--yes`` is given (for example because the budget left cannot cover
            the projected scan).
    """

    lines: tuple[str, ...] = ()
    paid_discovery: bool = False
    refuse: str | None = None
    confirm: str | None = None


@dataclass(frozen=True)
class NewestActivity:
    """The answer to an existence query (:meth:`Source.newest_activity`).

    Attributes:
        newest: The timestamp of the newest matching activity in ``[covered_since,
            until)``, or ``None`` when there is none.
        covered_since: Where the answer starts. It equals the requested ``since``
            when the whole window was answered, and is later when the source had to
            narrow the query (then ``None`` says nothing about ``[since,
            covered_since)``).
    """

    newest: datetime | None
    covered_since: datetime


class Source(abc.ABC):
    """Abstract platform adapter."""

    #: Platform key: ``"reddit"``, ``"x"`` or ``"fake"``. Matches ``Account.platform``.
    name: ClassVar[str]

    #: Environment variable names that must be set (``BOT_FARM_SILENCE_TRACKER_`` prefix). The CLI
    #: reads them from the environment, fails with a helpful message if any is
    #: missing, and passes the values in ``config.credentials``.
    required_credentials: ClassVar[tuple[str, ...]] = ()

    #: Environment variable names that are read when set and skipped when not (for
    #: example a password that switches Reddit to the password grant).
    optional_credentials: ClassVar[tuple[str, ...]] = ()

    #: The ``<PLATFORM>`` part of the adapter's environment variable names, when it
    #: differs from ``name`` upper-cased (for example ``"BSKY"`` for ``bluesky``).
    env_platform: ClassVar[str | None] = None

    #: Every host the adapter contacts, with what is sent to it. The shared client
    #: refuses every other host (see "Network" in the module docstring).
    network_hosts: ClassVar[tuple[HostUse, ...]] = ()

    #: The hosts of ``network_hosts`` with the default options. Derived; do not set.
    allowed_hosts: ClassVar[frozenset[str]] = frozenset()

    #: Options that decide where credentials are sent (Bluesky's ``pds_url``). They
    #: are never inherited by ``--from-run``: a stored value is refused while logged
    #: in and ignored while logged out, so it has to be passed again.
    credential_destination_options: ClassVar[frozenset[str]] = frozenset()

    #: Sustained requests per second for the shared HTTP client's token bucket.
    #: The CLI builds the client from this; it is the only pacer.
    default_rps: ClassVar[float] = 1.0

    #: Token-bucket capacity: requests allowed back to back before pacing kicks in.
    default_burst: ClassVar[float] = 1.0

    #: The shared HTTP client's per-request timeout: seconds, or an
    #: :class:`httpx.Timeout` with separate connect and read limits. The CLI builds
    #: the client with it. A request on the wire is never interrupted, so this also
    #: bounds how long Ctrl-C waits for an in-flight request.
    default_timeout: ClassVar[float | httpx.Timeout] = 30.0

    #: Extra ``--source-option`` keys that hold secrets and must never be stored
    #: with a run, on top of the names
    #: :data:`bot_farm_silence_tracker.pipeline.SECRET_OPTION_PATTERN` already catches
    #: (``token``, ``key``, ``auth`` and so on as name segments).
    secret_options: ClassVar[tuple[str, ...]] = ()

    #: Discovery oversampling used when ``--pool-factor`` is not given, or ``None``
    #: for the core default. Paid platforms set it low because discovery is billed.
    default_pool_factor: ClassVar[float | None] = None

    #: The adapter honors ``SourceConfig.active_within_days`` in discovery.
    supports_active_within: ClassVar[bool] = False

    #: The adapter supports ``--baseline-sample-rate`` (two-phase sampling, see
    #: :mod:`bot_farm_silence_tracker.early_exit`). The mode only saves anything when the pre-check
    #: is cheap: :meth:`newest_activity` must answer an existence query for a time
    #: window with about one request, so the source must query by time range and sort
    #: (Reddit's own API lists about the newest 1000 items and has no time bounds).
    #: The CLI refuses the flag for the others.
    supports_baseline_sampling: ClassVar[bool] = False

    #: Activity kinds the two-phase pre-check asks :meth:`newest_activity` about, in
    #: order. The first kind with activity at or after the cutoff ends the pre-check,
    #: so the kind that is usually the most recent comes first.
    precheck_kinds: ClassVar[tuple[str, ...]] = ("comment", "post")

    #: Highest ``--concurrency`` the adapter allows. 1 means accounts are fetched one
    #: at a time. An adapter that raises it must be thread safe in ``hydrate``,
    #: ``fetch_activity`` and its HTTP hooks (see ``SourceConfig.concurrency``). The
    #: request rate stays bounded by the one shared pacer however many workers run.
    max_concurrency: ClassVar[int] = 1

    #: Output columns beyond the common ones that this adapter fills, in order. They
    #: are appended after ``data_end`` (and before the two-phase sampling columns),
    #: and each adds its summary count: ``deactivated`` adds ``summary["deactivated"]``
    #: and ``reposts_after_cutoff`` adds ``summary["cliff_reposted_after_cutoff"]``.
    #: Recorded in the run's parameters, so ``report`` writes the same columns. An
    #: adapter that leaves this empty keeps the common output byte for byte. Known
    #: names are in :data:`bot_farm_silence_tracker.scoring.EXTRA_COLUMNS`.
    extra_columns: ClassVar[tuple[str, ...]] = ()

    #: The adapter can report accounts that opted out of logged-out views (status
    #: ``opted_out``). Its summary then always carries ``opted_out`` and
    #: ``dropped_opted_out``; other adapters show them only if they are non-zero.
    #: Recorded in the run's parameters, like :attr:`extra_columns`.
    reports_opted_out: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.allowed_hosts = frozenset(use.host for use in cls.network_hosts)

    @classmethod
    def host_uses(cls, options: Mapping[str, Any] | None = None) -> tuple[HostUse, ...]:
        """The hosts this adapter contacts with these options, login-only ones included.

        The default returns ``network_hosts``. Raises ``ValueError`` for an option
        that names a host that cannot be allowed (for example a non-https URL).
        """
        return cls.network_hosts

    @classmethod
    def hosts_for(
        cls, options: Mapping[str, Any] | None = None, *, logged_in: bool
    ) -> tuple[HostUse, ...]:
        """The hosts a scan with these options may contact, with or without a login."""
        return tuple(u for u in cls.host_uses(options) if logged_in or not u.login_only)

    @classmethod
    def has_login(cls, credentials: Mapping[str, str]) -> bool:
        """Whether any of the adapter's credentials is set in ``credentials``."""
        names = (*cls.required_credentials, *cls.optional_credentials)
        return any(credentials.get(name) for name in names)

    @classmethod
    def credential_destinations(cls, options: Mapping[str, Any] | None = None) -> frozenset[str]:
        """The hosts that receive credentials from the environment with these options."""
        return frozenset(u.host for u in cls.host_uses(options) if u.credentials)

    @classmethod
    def login_requirement(cls) -> str:
        """``"required"``, ``"optional"`` or ``"none"``: whether the adapter needs a login."""
        if cls.required_credentials:
            return "required"
        if cls.optional_credentials:
            return "optional"
        return "none"

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
        """Why ``--active-within-days days`` can never give a usable window, or None.

        The CLI calls this before the scan sends any request, so a lookback that is
        bound to be refused costs nothing. Adapters that honor
        ``supports_active_within`` override it; the default accepts every value.
        """
        return None

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self.http = config.http
        self.credentials = config.credentials
        uses = self.hosts_for(config.options, logged_in=self.has_login(config.credentials))
        # Installed before the adapter can send anything; it can never be widened.
        self.http.restrict_hosts(
            {u.host for u in uses},
            credential_hosts={u.host for u in uses if u.receives_credentials},
        )
        # Each secret credential's value may reach only the hosts declared for it.
        # Public identifiers (secret=False) are not bound: they may name an account.
        bindings: dict[str, set[str]] = {}
        for name in (*self.required_credentials, *self.optional_credentials):
            value = config.credentials.get(name)
            named = [(u.host, c) for u in uses for c in u.credentials if c.env == name]
            if value and any(c.secret for _, c in named):
                bindings.setdefault(value, set()).update(host for host, _ in named)
        self.http.bind_credential_values(bindings)
        self._warnings: list[str] = []
        self._warnings_lock = threading.Lock()

    # -- helpers for adapters ---------------------------------------------------

    def warn(self, message: str) -> None:
        """Record a user-facing warning (duplicates are kept once).

        Use it for skipped or invalid seeds, discovery that could not reach the
        cutoff, and other conditions that make results less complete without being
        errors. The pipeline collects warnings into ``SampleResult.warnings`` (during
        discovery) and the run summary, and the CLI prints them once at the end of
        the scan. The message is also logged, at DEBUG, when it is recorded.
        """
        with self._warnings_lock:
            if message in self._warnings:
                return
            self._warnings.append(message)
        log.debug("%s: %s", self.name, message)

    def drain_warnings(self) -> list[str]:
        """Return and clear the warnings recorded so far. Called by the pipeline."""
        with self._warnings_lock:
            out, self._warnings = self._warnings, []
        return out

    def drain_request_log(self, *, final: bool = False) -> list[dict[str, Any]]:
        """Return and clear per-request instrumentation rows. Called by the pipeline.

        An adapter that records its requests returns one dict per HTTP attempt with
        the keys of the ``request_log`` table (see :meth:`Storage.save_request_log`).
        ``final`` is true for the last call of a run: everything must be returned
        then. Before that, an adapter that still adds to a row (the wait chosen after
        a response) may keep it back. The default records nothing.
        """
        return []

    def request_summary(self) -> dict[str, Any] | None:
        """Totals over this adapter's requests for the run summary, or ``None``."""
        return None

    def option_str(self, key: str, default: str | None = None) -> str | None:
        """Return option ``key`` as a stripped string, or ``default`` when unset or blank."""
        value = self.config.options.get(key)
        if value is None:
            return default
        text = str(value).strip()
        return text or default

    def option_float(self, key: str, default: float | None = None) -> float | None:
        """Return option ``key`` as a float. Raises ``ValueError`` naming the option."""
        value = self.config.options.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"option {key!r} must be a number, got {redact_userinfo(value)!r}"
            ) from None

    def option_int(self, key: str, default: int | None = None) -> int | None:
        """Return option ``key`` as an int. Raises ``ValueError`` naming the option."""
        value = self.config.options.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"option {key!r} must be an integer, got {redact_userinfo(value)!r}"
            ) from None

    def option_bool(self, key: str, default: bool = False) -> bool:
        """Return option ``key`` as a bool, accepting true/false, yes/no, on/off, 1/0.

        Never use ``bool(options[key])`` on CLI options: ``bool("false")`` is True.
        An unset or blank value returns ``default``.
        """
        value = self.config.options.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"option {key!r} must be true or false, got {redact_userinfo(value)!r}")

    def cost_summary(self) -> str | None:
        """Return one line describing spend so far, or ``None`` if the platform is free.

        The CLI prints it on every exit path, including errors and interrupts, so it
        must not raise or make requests.
        """
        return None

    def data_horizon(self) -> datetime | None:
        """Return the newest moment this source's data covers, or ``None`` if live.

        An archive that lags the live platform (for example a research archive
        that ingests with a delay) returns how far its data reaches, so silence
        after that moment is not mistaken for accounts going dark. The pipeline
        calls this once per scan, before discovery (on a ``--from-run`` resume,
        before fetching), and scores with ``min(now, horizon)``. It may make one
        cheap request. Raise :class:`SourceError` when the horizon cannot be
        determined. An overriding source that raises a plain ``SourceError`` is
        treated as stale: the scan stops with ``StaleSourceError`` before sampling,
        unless ``--allow-stale-source`` is given, which runs it with every result
        provisional. ``SourceAuthError``, ``BudgetExceeded`` and ``RateLimited``
        are re-raised and end the scan like they do anywhere else.
        """
        return None

    def fallback_horizon(self) -> datetime | None:
        """Return a horizon to score with when :meth:`data_horizon` raised, or ``None``.

        Used only with ``--allow-stale-source``, where every result is provisional
        anyway. A source that learned how far its data reaches before the failure may
        return that moment (for example the archive's newest item, when only the
        ingestion-gap check could not finish), so scoring does not count the lag as
        silence. It must not make requests.
        """
        return None

    def plan_spend(
        self, seeds: SeedSet, *, n: int, pool_limit: int, resume: bool
    ) -> SpendPlan | None:
        """Return the projected spend of a scan before it starts, or ``None`` if free.

        ``n`` is the sample size, ``pool_limit`` the discovery pool size and
        ``resume`` is true for ``--from-run`` (no discovery). Must not make requests.
        """
        return None

    def cost_projection(
        self, accounts: Sequence[Account], since: datetime, until: datetime
    ) -> str | None:
        """Return a projection of what fetching ``accounts`` would cost, or ``None``.

        Used by ``scan --dry-run-cost`` after sampling. Must not make requests.
        """
        return None

    # -- the interface ----------------------------------------------------------

    @abc.abstractmethod
    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        """Yield accounts that were active in the seed communities BEFORE the cutoff.

        This builds the pool that sampling draws from. An account seen only after
        ``config.cutoff`` can never be a cliff (a cliff has no activity at or after
        the cutoff), so it wastes a sample slot and, on paid platforms, money.
        Adapters must therefore:

        * prefer items timestamped before ``config.cutoff``, ideally in
          ``[config.baseline_start, config.cutoff)``: filter listings to those
          items, or page (or set a search ``end_time``) back to the cutoff;
        * skip authors whose only discovered item is at or after the cutoff;
        * call :meth:`warn` when the platform cannot reach back to the cutoff (a
          listing cap, a 7-day search window), saying how many pre-cutoff items
          were found, so the user knows the pool is biased toward later posters;
        * call :meth:`warn` for each seed that is skipped or invalid (banned,
          private, malformed query) instead of failing the whole scan.

        Yield each account at most once (the sampler also de-duplicates by
        ``user_id``), and stop after ``pool_limit`` accounts. ``created_at`` may be
        ``None``; ``hydrate`` fills it in. Put anything ``fetch_activity`` needs into
        ``Account.meta``. Should be lazy: the pipeline may stop consuming early.

        Raises:
            SourceAuthError: credentials are bad.
            RateLimited: throttled beyond the HTTP helper's retries.
            BudgetExceeded: the request or spend budget is used up.
        """

    @abc.abstractmethod
    def hydrate(self, account: Account) -> Account:
        """Return ``account`` with ``created_at`` filled in if it was missing.

        Must be a no-op (cheap, no request) when ``created_at`` is already set.
        May mutate and return the same object.

        Raises:
            AccountUnavailable: the account is suspended, deleted or private.
            SourceError: anything else account-specific; the account is recorded
                with status ``error`` and the scan continues.
        """

    @abc.abstractmethod
    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        """Return the account's public activity with ``since <= ts < until``.

        Both bounds are timezone-aware UTC. Order does not matter. Return an empty
        list for an account with no activity in the window. If the platform caps how
        far back history goes, return what is available and set
        ``account.meta["truncated"] = True`` (and ``False`` when the history did
        reach ``since``), so the output can flag a possible false negative.

        Raises:
            AccountUnavailable: suspended, deleted or private. The pipeline records
                the status; this is not an error.
            AccountSkipped: the adapter chose not to fetch the account (it cannot
                matter, or it does not fit the spend budget). Recorded with that
                status and no cached activity; the scan continues.
            SourceAuthError: credentials are bad.
            RateLimited: throttled beyond the HTTP helper's retries.
            BudgetExceeded: the request or spend budget is used up.
            SourceError: anything else account-specific; recorded as status
                ``error``, and the scan continues.

        Set ``partial`` on the error (see :class:`SourceError`) when pages were
        already fetched, so the pipeline caches them instead of paying again.
        """

    def live_profile_status(self, account: Account) -> str | None:
        """The account's public profile status on the live platform now: ``active``,
        ``suspended`` or ``deleted``, or ``None`` when the adapter cannot read it from a
        logged-out public endpoint (the default).

        The registry asks before an account becomes a ``candidate``, because an
        archive cannot tell silence from removal. Only the adapter's own hosts and
        logged-out endpoints may be used: no other identity, user agent or proxy.

        Raises:
            SourceError: the check failed (the status is then "not checked").
        """
        return None

    def newest_activity(
        self, account: Account, kind: str, since: datetime, until: datetime
    ) -> NewestActivity:
        """Return the newest activity of ``kind`` with ``since <= ts < until``.

        The existence query of the two-phase pre-check (``--baseline-sample-rate``,
        see :mod:`bot_farm_silence_tracker.early_exit`). An adapter with
        ``supports_baseline_sampling`` should answer it with a single cheap request
        (``sort=desc, limit=1``). When the window is too slow to query it may narrow
        it to a later start and say so in ``covered_since``.

        The default fetches the whole window with :meth:`fetch_activity`, which is
        correct but costs as much as phase 2 would.

        Raises:
            The same exceptions as :meth:`fetch_activity`. A plain
            :class:`SourceError` makes the pre-check conservative (the account is a
            cliff candidate) instead of failing the account.
        """
        stamps = [a.ts for a in self.fetch_activity(account, since, until) if a.kind == kind]
        return NewestActivity(max(stamps) if stamps else None, since)

    def precheck_finished(self, account: Account, errors: Sequence[SourceError]) -> None:
        """Called once per account after its pre-check queries (:meth:`newest_activity`).

        ``errors`` are the per-account :class:`SourceError` the queries raised (empty
        when every query answered). An adapter that guards against an overloaded
        backend counts the account here, once, whatever number of its queries failed.
        The default does nothing.

        Raises:
            RateLimited: the adapter's guard decided the backend is overloaded.
        """
        del account, errors  # No guard by default.

    # -- placebo samples ----------------------------------------------------------

    def placebo_discovery_problem(self, seeds: SeedSet) -> str | None:
        """Why discovery cannot be repeated for a cutoff in an earlier week, or ``None``.

        A placebo cutoff ``k`` weeks back needs a sample of its own, discovered in the
        scan's discovery window moved back by ``k`` weeks (see
        :mod:`bot_farm_silence_tracker.controls`). An adapter whose platform can search
        that far back by time returns ``None``; the default says the platform cannot,
        so its runs have no placebo comparison. Must not make requests.
        """
        return (
            f"{self.name} discovery cannot be repeated for earlier weeks, so there is no "
            "placebo sample selected the way the run's own sample was"
        )

    @contextlib.contextmanager
    def shifted(self, shift: timedelta, *, now: datetime, seed: int) -> Iterator[None]:
        """Discover and fetch as if the scan's cutoff, baseline start and ``now`` were
        ``shift`` earlier (``now`` is given: the scan's effective now moved back).

        The adapter's ``_rng`` (a :class:`random.Random`, when it has one) is seeded
        with ``seed`` inside and restored after, so a placebo sample's draws depend on
        its own seed only. Everything that reads ``self.config`` inside the block sees
        the moved times.
        """
        saved = self.config
        rng = getattr(self, "_rng", None)
        state = rng.getstate() if isinstance(rng, random.Random) else None
        self.config = replace(
            saved,
            cutoff=saved.cutoff - shift,
            now=now,
            baseline_start=None if saved.baseline_start is None else saved.baseline_start - shift,
            seed=seed,
        )
        if isinstance(rng, random.Random):
            rng.seed(seed)
        try:
            yield
        finally:
            self.config = saved
            if isinstance(rng, random.Random) and state is not None:
                rng.setstate(state)

    def close(self) -> None:
        """Release resources. The CLI calls this once at the end of the scan."""
        self.http.close()
