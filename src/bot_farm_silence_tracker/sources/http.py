"""Shared HTTP helper for source adapters.

Wraps :class:`httpx.Client` with:

* a token-bucket rate limiter (the only pacer: the CLI sizes it from
  ``Source.default_rps`` and ``Source.default_burst``),
* retries with exponential backoff on 429 and 5xx responses and on transport
  errors, honoring ``Retry-After`` and, when that header is missing, a
  platform-specific rate-limit reset header read by an adapter hook,
* a request counter that enforces the ``--max-requests`` budget,
* per-attempt hooks so an adapter can check or charge a cost budget before every
  attempt, retries included, and
* a polite, identifying ``User-Agent``, and
* a destination check: every request must use ``https`` on the default port and go
  to a host on the allowlist (every :class:`~bot_farm_silence_tracker.sources.base.Source`
  installs its own; a client without one refuses everything unless it was built with
  ``unrestricted=True``). Requests that carry credentials may go only to the hosts
  allowed to receive them, and each credential value bound with
  :meth:`HttpClient.bind_credential_values` only to its own hosts. A refused request
  raises :class:`DisallowedHostError` and is never sent. Redirects are never
  followed, and a redirect to a destination that is not allowed raises
  :class:`DisallowedHostError` too.
* no environment configuration by default: proxy variables (``HTTP_PROXY``,
  ``HTTPS_PROXY``, ``ALL_PROXY``), ``SSL_CERT_FILE``, ``SSL_CERT_DIR`` and netrc are
  ignored, and TLS certificates are checked against certifi's CA bundle, unless the
  client was built with ``trust_env=True``.

Every attempt, including retries, counts against the budget, because that is what
the platform bills or throttles. Adapters that bypass :meth:`HttpClient.request`
must call :meth:`HttpClient.spend` themselves.

Hooks
-----
An adapter registers client-wide hooks once, usually in its ``__init__``, with
:meth:`HttpClient.configure`. ``before_attempt``, ``on_response`` and
``on_attempt_failed`` and ``on_attempt_not_sent`` can also be passed to a single
:meth:`HttpClient.request` call; the client-wide hook runs first, then the per-call
one.

``reset_wait(response) -> float | None``
    Called on a 429 that has no ``Retry-After``. Returns the seconds to wait until
    the platform's window resets, or ``None`` to fall back to exponential backoff.
    Use :func:`reset_in_seconds` (Reddit's ``X-Ratelimit-Reset``) or
    :func:`reset_at_epoch` (X's ``x-rate-limit-reset``) to build one.
``classify_error(response) -> SourceError | None``
    Called on every 429 and 402 before any retry logic. Return an exception (for
    example :class:`BudgetExceeded` for a usage-cap body) to raise it at once, or
    ``None`` to let the default handling run.
``before_attempt(attempt)``
    Called before every attempt, retries included, after the request-budget check
    and before the request is sent. Raise :class:`BudgetExceeded` to refuse it.
``on_response(attempt, response)``
    Called for every response received, including 429 and 5xx responses that are
    about to be retried.
``on_attempt_failed(attempt, error)``
    Called when an ``httpx`` error is raised after the request may have reached the
    server (anything except connect errors, connect and pool timeouts, unsupported
    protocols, local protocol errors and invalid URLs), so the adapter can charge
    its worst case for a request that may have been billed without a usable
    response. This includes undecodable bodies (``httpx.DecodingError``) and
    redirect loops. An interruption during the send (Ctrl-C) is reported here too,
    as a :class:`CancelledRequest` (an ``httpx.RequestError`` whose ``.request`` is
    set and whose ``.cause`` is the interruption), because the request may have
    been billed.
``on_attempt_not_sent(attempt, error)``
    Called when an attempt whose ``before_attempt`` hook ran was never sent: a
    connect-level error from :data:`NOT_SENT_ERRORS`, an invalid URL, a request
    that could not be built (for example a ``TypeError`` from a JSON payload that
    cannot be encoded; it is re-raised), or a later ``before_attempt`` hook
    raising. Release the reservation here.

Every attempt whose ``before_attempt`` hook ran gets exactly one terminal callback
at that level: ``on_response``, ``on_attempt_failed`` or ``on_attempt_not_sent``.
When the per-call ``before_attempt`` raises, the client-wide hook already ran, so
the client-wide ``on_attempt_not_sent`` is called (the per-call one is not, since
its ``before_attempt`` did not finish). When a terminal hook raises, the remaining
hooks of the same kind still run and the first error is re-raised.

Waiting
-------
One server-requested wait (``Retry-After`` on a 429 or 5xx, or the ``reset_wait``
hook) longer than ``max_retry_wait`` raises :class:`RateLimited` instead of
sleeping. A ``reset_wait`` built with a ``margin`` may exceed ``max_retry_wait``
by that margin, so a reset exactly at the cap is still waited for. All waits of
one :meth:`HttpClient.request` call (``Retry-After`` and backoff) together may not
exceed ``max_total_wait`` (1800 seconds by default); the wait that would cross it
raises :class:`RateLimited` too. Waits driven by the ``reset_wait`` hook do not
count toward that total; they have their own per-call total, ``max_reset_wait_total``
(1800 seconds by default), and the reset wait that would cross it raises
:class:`RateLimited`. Error messages name the URL without its query string, which
may carry sensitive parameters.

Destinations
------------
:meth:`HttpClient.restrict_hosts` installs the host allowlist: a set of exact host
names (no wildcards, no subdomains), plus the subset of them that may receive
credentials. Only the scheme's default port is accepted (443 for https), so a host
on the list with any other port is refused. Until an allowlist is installed, every
request is refused, unless the client was built with ``unrestricted=True`` (for
tests and tools that do not run an adapter).

Once an allowlist is installed, every :meth:`HttpClient.request` must say whether it
carries credentials with ``sends_credentials=True`` or ``False``; leaving it out
raises ``TypeError``. The client also detects credentials itself, and a request
counts as carrying them when either says so. Detected are: ``auth`` (per call or
client-wide), an ``Authorization``, ``Proxy-Authorization`` or ``Cookie`` header, a
header whose name looks like an API key or token (``X-Api-Key``, ``X-Auth-Token``
and so on), a user name or password in the URL, a query parameter whose name looks
like a token or key (``access_token``, ``api_key``, ``token`` and so on), and a
form or JSON body whose top-level keys include a password or secret.

:meth:`HttpClient.bind_credential_values` adds per-credential enforcement: each
secret credential value from the environment (passwords, app passwords, client
secrets, bearer tokens; not public identifiers such as a handle, a user name or an
OAuth client id) is bound to the hosts declared to receive it, and a request to any
other host that contains that value is refused, whatever the adapter declared. Only
these forms are recognized, in the URL, the headers and the body: the value as is,
URL-encoded (``quote`` and ``quote_plus``), JSON-escaped with and without
``ensure_ascii``, an item of an ``auth=(user, password)`` tuple, and the decoded
credentials of a ``Basic`` ``Authorization`` header. Anything else is not
recognized: base64 outside a ``Basic`` header, hex, a changed case, double
encoding, or a value split across parts. A value that starts or ends with a letter
or digit is matched on token boundaries only, and values shorter than
:data:`MIN_BOUND_CREDENTIAL_LENGTH` are not bound. ``auth`` must be such a tuple
(anything else raises ``TypeError``), and client-wide ``auth`` is refused the same
way. An adapter binds a session token it obtains at run time the same way when it
may go to fewer than all credential hosts (Reddit's access token); otherwise a token
is held to the credential hosts.

Every attempt, retries included, has its destination checked before the pacer, the
budget and the hooks run, so a request to a host that is not allowed (or that may
not receive credentials) costs nothing and calls no hook. The request as built (its
final URL, headers and body) is checked again just before it is sent, and that is
where bound credential values are looked for: that check runs after the pacer, the
budget reservation and the ``before_attempt`` hooks, so a refusal there releases
the reservation and calls ``on_attempt_not_sent``. Plain ``http`` is refused unless
the client was built with ``allow_insecure_http=True``, which exists for tests only.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import email.utils
import json as jsonlib
import logging
import re
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote, quote_plus

import httpx

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.sources.errors import (
    BudgetExceeded,
    DisallowedHostError,
    FetchCancelled,
    RateLimited,
    ReadTimedOut,
    SourceAuthError,
    SourceError,
    TransportFailure,
)

log = logging.getLogger(__name__)

#: Waits longer than this many seconds are logged at INFO before sleeping.
LOG_WAIT_THRESHOLD = 5.0
#: Default upper bound on one server-requested wait (Retry-After or reset header).
DEFAULT_MAX_RETRY_WAIT = 900.0
#: Default upper bound on the sum of all waits inside one ``request`` call.
DEFAULT_MAX_TOTAL_WAIT = 1800.0
#: Default upper bound on the sum of reset-header waits inside one ``request`` call.
DEFAULT_MAX_RESET_WAIT_TOTAL = 1800.0

#: The project's page, named as the contact in the User-Agent. "Publishing your copy" in
#: docs/USAGE.md replaces the owner placeholder in it with the account that hosts the copy.
PROJECT_URL = "https://github.com/obviousthrowaway/bot-farm-silence-tracker"
#: The owner placeholder, written in two parts so that the replace command of docs/USAGE.md
#: rewrites PROJECT_URL but leaves this check intact.
OWNER_PLACEHOLDER = "_" + "_OWNER__"


def published_project_url(project_url: str = PROJECT_URL) -> str | None:
    """Return ``project_url``, or None while it still holds the owner placeholder.

    An unpublished copy has no project page, and a User-Agent must not name one that
    someone else could own, so callers then leave the URL out.
    """
    return None if OWNER_PLACEHOLDER in project_url else project_url


def research_user_agent(project_url: str = PROJECT_URL) -> str:
    """The short User-Agent of the read-only research adapters (Bluesky, Arctic Shift)."""
    url = published_project_url(project_url)
    if url is None:
        return f"bot-farm-silence-tracker/{__version__} (research tool; unpublished copy)"
    return f"bot-farm-silence-tracker/{__version__} (research; +{url})"


def default_user_agent(project_url: str = PROJECT_URL) -> str:
    """The User-Agent that :class:`HttpClient` sends unless a source sets its own."""
    url = published_project_url(project_url)
    return (
        f"bot-farm-silence-tracker/{__version__} (disinformation research tool; official "
        "platform APIs and documented research-archive APIs only, no scraping; "
        f"{url if url is not None else 'unpublished copy'})"
    )


DEFAULT_USER_AGENT = default_user_agent()


#: Transport errors raised before any byte of the request could reach the server.
NOT_SENT_ERRORS: tuple[type[httpx.TransportError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


class CancelledRequest(httpx.RequestError):
    """An attempt interrupted while it was being sent (Ctrl-C, or a bug).

    Passed to ``on_attempt_failed``, because the request may have reached the
    server. ``request`` is set to the request being sent (so ``error.request``
    never raises), and ``cause`` holds the interrupting exception, which the
    client re-raises after the hooks ran.
    """

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, request=request)
        self.cause = cause


def _normal_host(host: str) -> str:
    """A host name as the allowlist compares it: lowercase, no surrounding space."""
    return str(host).strip().lower()


#: The only port accepted for each scheme.
DEFAULT_PORTS = {"https": 443, "http": 80}

#: Header names that carry credentials, compared lowercase.
CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
#: Header names that look like an API key or token (``X-Api-Key``, ``X-Auth-Token``).
_CREDENTIAL_HEADER_RE = re.compile(
    r"(?:^|[-_])(?:api[-_]?key|apikey|auth[-_]?token|access[-_]?token|token|secret|session)"
    r"(?:$|[-_])",
    re.IGNORECASE,
)
#: Query parameter and body field names that carry credentials, compared lowercase.
CREDENTIAL_PARAMS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "token",
        "api_key",
        "apikey",
        "api-key",
        "client_secret",
        "password",
        "passwd",
        "secret",
    }
)
#: Credential values shorter than this are not bound (they would match too much).
MIN_BOUND_CREDENTIAL_LENGTH = 8


def _is_credential_header(name: str) -> bool:
    lowered = str(name).strip().lower()
    return lowered in CREDENTIAL_HEADERS or _CREDENTIAL_HEADER_RE.search(lowered) is not None


def _credential_keys(keys: Iterable[Any]) -> bool:
    return any(str(k).strip().lower() in CREDENTIAL_PARAMS for k in keys)


def _encoded_forms(value: str) -> set[str]:
    """``value`` as it may appear in a request: plain, URL-encoded or JSON-escaped.

    JSON-escaped both ways: with non-ASCII characters escaped (``\\u00e9``, what
    ``httpx`` sends for ``json=``) and kept as they are.
    """
    return {
        value,
        quote(value, safe=""),
        quote_plus(value),
        jsonlib.dumps(value)[1:-1],
        jsonlib.dumps(value, ensure_ascii=False)[1:-1],
    }


def _bound_pattern(value: str) -> re.Pattern[str]:
    """A pattern that finds any encoded form of ``value`` on token boundaries.

    A form that starts (or ends) with a letter or digit matches only where the
    character before (or after) it is not a letter or digit, so a secret is not
    found inside a longer word (a password ``newsroom1`` does not match
    ``/r/newsroom1s``). A form whose edge is punctuation matches anywhere.
    """
    alternatives = []
    for form in sorted(_encoded_forms(value), key=len, reverse=True):
        head = r"(?<![^\W_])" if form[:1].isalnum() else ""
        tail = r"(?![^\W_])" if form[-1:].isalnum() else ""
        alternatives.append(head + re.escape(form) + tail)
    return re.compile("|".join(alternatives))


def certifi_ssl_context() -> ssl.SSLContext:
    """A TLS context that trusts certifi's CA bundle only (no environment overrides)."""
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


_USERINFO = re.compile(r"^(\s*[A-Za-z][A-Za-z0-9+.-]*:[/\\]*)[^/?#]*@")


def redact_userinfo(value: Any) -> str:
    """``value`` with any ``user:password@`` part of a URL replaced by ``***@``.

    Conservative: everything up to the last ``@`` before the first ``/``, ``?`` or
    ``#`` after the scheme is hidden, backslashes included, so no credential survives
    however a parser would split it. Used in error messages that echo an option.
    """
    return _USERINFO.sub(r"\1***@", str(value))


def redact_url(url: str | httpx.URL) -> str:
    """Return ``url`` without its query string and fragment, for error messages."""
    try:
        parsed = httpx.URL(str(url))
    except (httpx.InvalidURL, TypeError, ValueError):
        return str(url).split("?", 1)[0].split("#", 1)[0]
    return str(parsed.copy_with(query=None, fragment=None))


class TokenBucket:
    """A token bucket that blocks (by sleeping) until a token is available.

    Safe to share between threads. Callers take turns: while one waits for its
    token, the others wait for their turn, so the bucket grants at most ``rate``
    tokens per second (plus the burst) however many threads ask. The rate, the
    tokens and the pause are guarded by a second lock that is never held while
    sleeping, so :meth:`set_rate`, :meth:`restart` and :meth:`hold_until` never wait
    for a sleeping caller.

    :meth:`hold_until` sets a shared "not before" time: every caller waits until then
    before it takes a token, including one already waiting for its token. The
    concurrent fetch loop uses it so a load signal seen by one worker pauses all of
    them. Each pause is waited for at most once per call, so a clock that a test's
    sleep does not advance cannot make a caller loop. A pause ends with an empty
    bucket, so the first token after it is one full interval away, and
    :meth:`acquire` returns how long it waited for pauses.
    """

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = max(capacity, 1.0)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._last = clock()
        #: Clock time before which no token is granted, or None.
        self._not_before: float | None = None
        #: Serializes callers of :meth:`acquire`; held while one waits for its token.
        self._turn = threading.Lock()
        #: Guards the rate, the tokens and the pause; never held while sleeping.
        self._state = threading.Lock()

    def acquire(self) -> float:
        """Take one token, sleeping first if the bucket is empty or paused.

        Returns the seconds spent waiting for a pause (see :meth:`hold_until`), not
        counting the ordinary wait for a token, so a caller can account for it. After
        a pause the bucket starts empty, as if a request had been sent the moment the
        pause ended: the first token after it is one full interval away, so callers
        released by the pause do not go out back to back.
        """
        paused = 0.0
        with self._turn:
            honoured: float | None = None
            while True:
                with self._state:
                    now = self._clock()
                    target = self._not_before
                    pausing = target is not None and target != honoured and target > now
                    if pausing:
                        wait = target - now  # type: ignore[operator]
                    else:
                        self._tokens = min(
                            self.capacity, self._tokens + (now - self._last) * self.rate
                        )
                        self._last = now
                        if self._tokens >= 1.0:
                            self._tokens -= 1.0
                            return paused
                        wait = (1.0 - self._tokens) / self.rate
                self._sleep(wait)
                if pausing:
                    paused += wait
                    honoured = target
                    with self._state:
                        # The pause ends with an empty bucket, never a free token.
                        self._tokens = 0.0
                        self._last = self._clock()
                    continue
                with self._state:
                    late = self._not_before
                    if late is not None and late != honoured and late > self._clock():
                        continue  # A pause began while this caller waited for its token.
                    self._last = self._clock()
                    self._tokens = 0.0
                    return paused

    def set_rate(self, rate: float) -> None:
        """Change the refill rate in place, keeping the tokens already earned.

        Tokens accrued so far are credited at the old rate first, so a rate change
        never hands out a fresh burst (replacing the bucket would start it full).
        """
        if rate <= 0:
            raise ValueError("rate must be positive")
        with self._state:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            self.rate = rate

    def restart(self) -> None:
        """Empty the bucket as of now, so the next token is one full interval away.

        Call it after a long wait that happened between taking a token and sending
        the request: the bucket refilled during that wait, and without a restart the
        request after the delayed one would go out right behind it.
        """
        with self._state:
            self._tokens = 0.0
            self._last = self._clock()

    def hold_until(self, when: float) -> None:
        """Grant no token before clock time ``when`` (a later pause wins)."""
        with self._state:
            if self._not_before is None or when > self._not_before:
                self._not_before = when

    @property
    def not_before(self) -> float | None:
        """The clock time no token is granted before, or None if never paused."""
        with self._state:
            return self._not_before


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP-date) into seconds."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


@dataclass(frozen=True)
class Attempt:
    """One attempt of a request, passed to the per-attempt hooks.

    ``attempt`` is 0 for the first try and counts up on each retry.
    """

    method: str
    url: str
    attempt: int
    params: Mapping[str, Any] | None = None
    #: Seconds this attempt waited in the pacer for a shared pause (see
    #: :meth:`HttpClient.hold`) before it was granted, not counting the ordinary
    #: wait for a token. 0 when no pause was in force.
    paused: float = 0.0


BeforeAttemptHook = Callable[[Attempt], None]
ResponseHook = Callable[[Attempt, httpx.Response], None]
AttemptFailedHook = Callable[[Attempt, httpx.HTTPError], None]
AttemptNotSentHook = Callable[[Attempt, BaseException], None]
ResetWaitHook = Callable[[httpx.Response], float | None]
ErrorClassifier = Callable[[httpx.Response], SourceError | None]


def reset_in_seconds(header: str, *, margin: float = 0.0) -> ResetWaitHook:
    """Build a ``reset_wait`` hook for a header that holds seconds until the reset.

    Reddit's ``X-Ratelimit-Reset`` works this way.
    """

    def hook(response: httpx.Response) -> float | None:
        try:
            value = float(response.headers[header])
        except (KeyError, ValueError):
            return None
        return max(0.0, value + margin)

    hook.margin = margin  # type: ignore[attr-defined]
    return hook


def reset_at_epoch(
    header: str, *, margin: float = 1.0, clock: Callable[[], float] = time.time
) -> ResetWaitHook:
    """Build a ``reset_wait`` hook for a header that holds the reset time as a Unix epoch.

    X's ``x-rate-limit-reset`` works this way. ``clock`` must return wall-clock
    seconds since the epoch (like ``time.time``), not a monotonic clock.
    """

    def hook(response: httpx.Response) -> float | None:
        try:
            value = float(response.headers[header])
        except (KeyError, ValueError):
            return None
        return max(0.0, value - clock() + margin)

    hook.margin = margin  # type: ignore[attr-defined]
    return hook


class HttpClient:
    """Rate-limited, budgeted, retrying HTTP client for adapters.

    Args:
        user_agent: Sent on every request. Defaults to an identifying string.
        requests_per_second: Sustained request rate.
        burst: Bucket capacity, the number of requests allowed back to back.
        max_requests: Global budget over the life of this client, or ``None`` for
            unlimited. Exceeding it raises :class:`BudgetExceeded`.
        max_retries: Retries after the first attempt for 429, 5xx and transport errors.
        backoff_base: First backoff delay in seconds; doubles each retry.
        backoff_max: Upper bound on a computed backoff delay.
        max_retry_wait: Longest single wait the server may ask for, through
            ``Retry-After`` (on 429 or 5xx) or the ``reset_wait`` hook (default 900
            seconds). A longer request raises :class:`RateLimited` instead of
            blocking, so the scan ends partial and can be resumed.
        max_total_wait: Longest total time one :meth:`request` call may spend
            waiting between attempts (default 1800 seconds), not counting waits
            driven by the ``reset_wait`` hook. The wait that would exceed it raises
            :class:`RateLimited`.
        max_reset_wait_total: Longest total time one :meth:`request` call may
            spend in waits driven by the ``reset_wait`` hook (default 1800 seconds).
            The reset wait that would exceed it raises :class:`RateLimited`.
        timeout: Per-request timeout in seconds, or an :class:`httpx.Timeout` with
            separate connect and read limits (see ``Source.default_timeout``).
        headers: Extra default headers (do not put secrets in the User-Agent).
        transport: Optional httpx transport, used by tests to avoid the network. The
            destination check runs on the URL whatever the transport.
        allowed_hosts: Host allowlist to install at once (see :meth:`restrict_hosts`),
            or ``None`` to wait for an adapter to install one. Until then every
            request is refused, unless ``unrestricted`` is set.
        credential_hosts: The allowed hosts that may receive credentials.
        unrestricted: Allow any https host until an allowlist is installed. For tests
            and tools that do not run an adapter; never set it for a scan.
        trust_env: Read proxy settings (``HTTP_PROXY``, ``HTTPS_PROXY``,
            ``ALL_PROXY``, ``NO_PROXY``), ``SSL_CERT_FILE``, ``SSL_CERT_DIR`` and
            netrc from the environment, like plain httpx. Off by default: the client
            then connects directly and checks certificates against certifi's bundle.
        allow_insecure_http: Accept ``http://`` URLs. For tests against a local
            server only; never set it in production code.
        sleep, clock: Injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        requests_per_second: float = 1.0,
        burst: float = 1.0,
        max_requests: int | None = None,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT,
        timeout: float | httpx.Timeout = 30.0,
        max_total_wait: float = DEFAULT_MAX_TOTAL_WAIT,
        max_reset_wait_total: float = DEFAULT_MAX_RESET_WAIT_TOTAL,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        allowed_hosts: Iterable[str] | None = None,
        credential_hosts: Iterable[str] = (),
        unrestricted: bool = False,
        trust_env: bool = False,
        allow_insecure_http: bool = False,
    ) -> None:
        self.max_requests = max_requests
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_retry_wait = max_retry_wait
        self.max_total_wait = max_total_wait
        self.max_reset_wait_total = max_reset_wait_total
        self._raw_sleep = sleep
        self._clock = clock
        self._requests_made = 0
        #: Attempts that passed the budget check but were not sent yet. Counted with
        #: the requests made, so threads cannot both take the last request.
        self._reserved = 0
        #: Requests being sent right now (on the wire, from any thread).
        self._in_flight = 0
        self._budget_lock = threading.Lock()
        self._cancel = threading.Event()
        #: Wall-clock time (``time.time()``) the latest response came in, or ``None``
        #: before the first. Any HTTP status counts; a transport error does not.
        self.last_response_at: float | None = None
        self._bucket = TokenBucket(requests_per_second, burst, clock=clock, sleep=self._sleep)
        self.reset_wait: ResetWaitHook | None = None
        self.classify_error: ErrorClassifier | None = None
        self.before_attempt: BeforeAttemptHook | None = None
        self.on_response: ResponseHook | None = None
        self.on_attempt_failed: AttemptFailedHook | None = None
        self.on_attempt_not_sent: AttemptNotSentHook | None = None
        #: Whether a read timeout is retried like any transport error. When False, it
        #: is reported to ``on_attempt_failed`` and raised at once as ReadTimedOut.
        self.retry_read_timeouts = True
        merged = {"User-Agent": user_agent, **(headers or {})}
        #: Hosts requests may go to, or None before an allowlist is installed.
        self._allowed_hosts: frozenset[str] | None = None
        #: Allowed hosts that may receive credentials.
        self._credential_hosts: frozenset[str] = frozenset()
        #: Credential value -> the hosts it may be sent to (see bind_credential_values).
        self._bound_values: dict[str, frozenset[str]] = {}
        #: Credential value -> the pattern that finds it in a request.
        self._bound_patterns: dict[str, re.Pattern[str]] = {}
        self._unrestricted = unrestricted
        self._allow_insecure_http = allow_insecure_http
        self.trust_env = trust_env
        if allowed_hosts is not None:
            self.restrict_hosts(allowed_hosts, credential_hosts=credential_hosts)
        # Redirects are never followed: a 3xx is returned (or refused, see request()).
        # Without trust_env, httpx ignores proxy variables, SSL_CERT_FILE/DIR and netrc;
        # certificates are then checked against certifi's bundle, set explicitly here.
        verify: ssl.SSLContext | bool = True
        if transport is None and not trust_env:
            verify = certifi_ssl_context()
        self._client = httpx.Client(
            headers=merged,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=trust_env,
            verify=verify,
        )

    # -- destinations ------------------------------------------------------------------

    def restrict_hosts(self, hosts: Iterable[str], *, credential_hosts: Iterable[str] = ()) -> None:
        """Install the host allowlist. Requests to any other host are refused.

        ``hosts`` are exact host names. ``credential_hosts`` must be a subset of them:
        only these may receive a request that carries credentials. Calling it again
        can only narrow the lists (the new lists are intersected with the old ones),
        so nothing can widen an allowlist once installed.
        """
        allowed = frozenset(_normal_host(h) for h in hosts)
        secret = frozenset(_normal_host(h) for h in credential_hosts)
        if not secret <= allowed:
            extra = ", ".join(sorted(secret - allowed))
            raise ValueError(f"credential hosts must also be allowed hosts: {extra}")
        if self._allowed_hosts is not None:
            allowed &= self._allowed_hosts
            secret &= self._credential_hosts
        self._allowed_hosts = allowed
        self._credential_hosts = secret

    def bind_credential_values(self, bindings: Mapping[str, Iterable[str]]) -> None:
        """Bind credential values to the hosts that may receive them.

        ``bindings`` maps a secret credential's value (from the environment, or a
        session token) to the hosts declared to receive it. A request to any other
        host that contains the value is refused. Recognized are the value as is,
        URL-encoded (``%20`` or ``+`` for a space), JSON-escaped (with non-ASCII
        characters escaped or not), and inside an ``auth=(user, password)`` tuple or
        a ``Basic`` ``Authorization`` header; a value that starts or ends with a
        letter or digit is matched only on token boundaries (see
        :func:`_bound_pattern`). Values shorter than
        :data:`MIN_BOUND_CREDENTIAL_LENGTH` are skipped. Binding a value again can only
        narrow its hosts.
        """
        for value, hosts in bindings.items():
            text = str(value)
            if len(text) < MIN_BOUND_CREDENTIAL_LENGTH:
                continue
            allowed = frozenset(_normal_host(h) for h in hosts)
            if text in self._bound_values:
                allowed &= self._bound_values[text]
            else:
                self._bound_patterns[text] = _bound_pattern(text)
            self._bound_values[text] = allowed

    @property
    def unrestricted(self) -> bool:
        """Whether requests are allowed before an allowlist is installed."""
        return self._unrestricted and self._allowed_hosts is None

    @property
    def allowed_hosts(self) -> frozenset[str] | None:
        """The host allowlist, or ``None`` when none is installed."""
        return self._allowed_hosts

    @property
    def credential_hosts(self) -> frozenset[str]:
        """The allowed hosts that may receive credentials."""
        return self._credential_hosts

    def check_destination(self, url: str | httpx.URL, *, credentials: bool = False) -> None:
        """Raise :class:`DisallowedHostError` unless a request to ``url`` may be sent.

        ``credentials`` says whether the request carries credentials. A URL with a
        user name or password counts as carrying them.
        """
        safe_url = redact_url(url)
        try:
            parsed = httpx.URL(str(url))
        except (httpx.InvalidURL, TypeError, ValueError) as exc:
            raise DisallowedHostError(f"refused to send a request to {safe_url}: {exc}") from None
        scheme = parsed.scheme.lower()
        if scheme != "https" and not (scheme == "http" and self._allow_insecure_http):
            raise DisallowedHostError(
                f"refused to send a request to {safe_url}: only https:// is allowed"
            )
        host = _normal_host(parsed.host)
        if not host:
            raise DisallowedHostError(f"refused to send a request to {safe_url}: no host")
        # Compare host and port: only the scheme's default port is ever allowed.
        port = parsed.port if parsed.port is not None else DEFAULT_PORTS[scheme]
        if port != DEFAULT_PORTS[scheme]:
            raise DisallowedHostError(
                f"refused to send a request to {host}:{port}: only the default port "
                f"{DEFAULT_PORTS[scheme]} is allowed"
            )
        if self._allowed_hosts is None:
            if self._unrestricted:
                return
            raise DisallowedHostError(
                f"refused to send a request to {host}: no host allowlist is installed"
            )
        if host not in self._allowed_hosts:
            allowed = ", ".join(sorted(self._allowed_hosts)) or "none"
            raise DisallowedHostError(
                f"refused to send a request to {host}: it is not on this platform's host "
                f"allowlist (allowed: {allowed})"
            )
        if (credentials or parsed.userinfo) and host not in self._credential_hosts:
            raise DisallowedHostError(
                f"refused to send credentials to {host}: this platform sends credentials "
                f"only to {', '.join(sorted(self._credential_hosts)) or 'no host'}"
            )

    def detect_credentials(
        self,
        url: str | httpx.URL,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        auth: object = None,
        data: Any = None,
        json: Any = None,
    ) -> bool:
        """Whether a request built from these arguments visibly carries credentials.

        See "Destinations" in the module docstring for what is detected.
        """
        if auth is not None or self._client.auth is not None:
            return True
        names = [*self._client.headers.keys(), *(headers or {}).keys()]
        if any(_is_credential_header(name) for name in names):
            return True
        if isinstance(params, Mapping) and _credential_keys(params.keys()):
            return True
        try:
            parsed = httpx.URL(str(url))
        except (httpx.InvalidURL, TypeError, ValueError):
            return False  # check_destination refuses it anyway.
        if parsed.userinfo or _credential_keys(parsed.params.keys()):
            return True
        return any(
            isinstance(body, Mapping) and _credential_keys(body.keys()) for body in (data, json)
        )

    def _check_bound_values(self, built: httpx.Request, auth: object, credentialed: bool) -> None:
        """Refuse ``built`` if it contains a bound credential value for another host."""
        if not self._bound_values:
            return
        host = _normal_host(built.url.host)
        parts = [str(built.url)]
        for key, value in built.headers.items():
            parts.append(f"{key}: {value}")
            scheme_and_value = value.split()
            if len(scheme_and_value) == 2 and scheme_and_value[0].lower() == "basic":
                with contextlib.suppress(binascii.Error, ValueError):
                    decoded = base64.b64decode(scheme_and_value[1])
                    parts.append(decoded.decode("utf-8", "replace"))
        if isinstance(auth, tuple):
            parts.extend(
                item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
                for item in auth
            )
        try:
            parts.append(built.read().decode("utf-8", "replace"))
        except httpx.RequestNotRead:  # pragma: no cover - adapters never stream bodies
            if credentialed:
                raise DisallowedHostError(
                    f"refused to send a streamed body with credentials to {host}"
                ) from None
        text = "\n".join(parts)
        for value, hosts in self._bound_values.items():
            if host in hosts:
                continue
            if self._bound_patterns[value].search(text):
                raise DisallowedHostError(
                    f"refused to send a credential to {host}: it is declared only for "
                    f"{', '.join(sorted(hosts)) or 'no host'}"
                )

    def _check_redirect(self, response: httpx.Response, url: str) -> None:
        """Refuse a redirect whose target may not be contacted (it is never followed)."""
        location = response.headers.get("Location")
        if location is None or not 300 <= response.status_code < 400:
            return
        try:
            target = response.request.url.join(location)
        except (httpx.InvalidURL, TypeError, ValueError):
            target = location
        try:
            self.check_destination(target)
        except DisallowedHostError as exc:
            raise DisallowedHostError(
                f"{redact_url(url)} redirected (HTTP {response.status_code}) to a "
                f"destination that is not allowed; not followed: {exc}"
            ) from None

    def configure(
        self,
        *,
        reset_wait: ResetWaitHook | None = None,
        classify_error: ErrorClassifier | None = None,
        before_attempt: BeforeAttemptHook | None = None,
        on_response: ResponseHook | None = None,
        on_attempt_failed: AttemptFailedHook | None = None,
        on_attempt_not_sent: AttemptNotSentHook | None = None,
        retry_read_timeouts: bool | None = None,
    ) -> None:
        """Register client-wide adapter hooks. Arguments left as ``None`` are unchanged.

        See the module docstring for what each hook receives and when it runs. With
        ``retry_read_timeouts=False``, an ``httpx.ReadTimeout`` (the request was sent
        but no answer came within the read timeout) is not retried: after
        ``on_attempt_failed`` it raises :class:`ReadTimedOut`, so an adapter can handle
        a slow query its own way. Connect timeouts are retried either way.
        """
        if retry_read_timeouts is not None:
            self.retry_read_timeouts = retry_read_timeouts
        if reset_wait is not None:
            self.reset_wait = reset_wait
        if classify_error is not None:
            self.classify_error = classify_error
        if before_attempt is not None:
            self.before_attempt = before_attempt
        if on_response is not None:
            self.on_response = on_response
        if on_attempt_failed is not None:
            self.on_attempt_failed = on_attempt_failed
        if on_attempt_not_sent is not None:
            self.on_attempt_not_sent = on_attempt_not_sent

    def set_rate(self, requests_per_second: float, burst: float | None = None) -> None:
        """Replace the pacer, for an adapter whose rate depends on its options.

        Prefer the ``Source.default_rps`` / ``default_burst`` class attributes, which
        the CLI uses to build this client. Call this only when the rate comes from
        runtime configuration (for example Reddit's ``qpm`` option).
        """
        capacity = self._bucket.capacity if burst is None else burst
        self._bucket = TokenBucket(
            requests_per_second, capacity, clock=self._clock, sleep=self._sleep
        )

    def adjust_rate(self, requests_per_second: float) -> None:
        """Change the pacer's rate in place, for adaptive rate control.

        Unlike :meth:`set_rate`, the bucket is not replaced, so a change never grants
        an immediate extra request: the tokens already earned are kept and only the
        refill speed changes.
        """
        self._bucket.set_rate(requests_per_second)

    def restart_pacer(self) -> None:
        """Restart the pacer's interval from now, as if a request had just been sent.

        A ``before_attempt`` hook that sleeps runs after the pacer granted the attempt,
        so the bucket refills while it sleeps. Calling this once the sleep is over keeps
        the next request at least ``1 / requests_per_second`` seconds after this one.
        """
        self._bucket.restart()

    def hold(self, seconds: float) -> None:
        """Send no request for ``seconds`` from now, from any thread (a pause for all).

        The concurrent fetch loop calls it on a load signal, so every worker waits
        before its next attempt instead of only the one that saw the signal.
        """
        self.hold_until(self._clock() + max(0.0, seconds))

    def hold_until(self, when: float) -> None:
        """Send no request before clock time ``when`` (a later pause wins)."""
        self._bucket.hold_until(when)

    @property
    def paused_until(self) -> float | None:
        """The clock time set by the latest :meth:`hold`, or None (it may be past)."""
        return self._bucket.not_before

    def cancel(self) -> None:
        """Stop every request and wait in progress, from any thread.

        After this, every wait of the client (the pacer, retry backoffs, reset waits
        and the adapter waits that use :attr:`sleep`) raises :class:`FetchCancelled`,
        and so does the next attempt of every request. A request already being sent
        finishes first. Undo it with :meth:`reset_cancel`.
        """
        self._cancel.set()

    def reset_cancel(self) -> None:
        """Let requests run again after :meth:`cancel`."""
        self._cancel.clear()

    @property
    def in_flight(self) -> int:
        """How many requests are being sent right now, from every thread."""
        with self._budget_lock:
            return self._in_flight

    @property
    def cancelled(self) -> bool:
        """Whether :meth:`cancel` was called (and not reset since)."""
        return self._cancel.is_set()

    def _sleep(self, seconds: float) -> None:
        """Sleep with the injected sleep, raising :class:`FetchCancelled` once cancelled.

        With the real ``time.sleep`` the wait itself ends as soon as :meth:`cancel` is
        called; an injected sleep (tests) runs as given and is checked afterwards.
        """
        if self._cancel.is_set():
            raise FetchCancelled("the scan is stopping")
        if self._raw_sleep is time.sleep:
            if self._cancel.wait(max(0.0, seconds)):
                raise FetchCancelled("the scan is stopping")
            return
        self._raw_sleep(seconds)
        if self._cancel.is_set():
            raise FetchCancelled("the scan is stopping")

    @property
    def clock(self) -> Callable[[], float]:
        """The monotonic clock this client paces with (injected in tests)."""
        return self._clock

    @property
    def rate(self) -> tuple[float, float]:
        """The pacer's ``(requests_per_second, burst)``."""
        return (self._bucket.rate, self._bucket.capacity)

    @property
    def sleep(self) -> Callable[[float], None]:
        """The sleep function this client waits with (injected in tests).

        Adapters that wait on their own, outside :meth:`request`, should use it too,
        so a test that injects a no-op sleep never waits for real.
        """
        return self._sleep

    @property
    def requests_made(self) -> int:
        """Number of requests (including retries) counted against the budget."""
        return self._requests_made

    @property
    def requests_remaining(self) -> int | None:
        """Requests left in the budget, or ``None`` if unlimited.

        Attempts that another thread has reserved but not sent yet count as used.
        """
        if self.max_requests is None:
            return None
        with self._budget_lock:
            return max(0, self.max_requests - self._requests_made - self._reserved)

    def _check_budget(self, count: int) -> None:
        # Callers hold _budget_lock.
        used = self._requests_made + self._reserved
        if self.max_requests is not None and used + count > self.max_requests:
            raise BudgetExceeded(
                f"request budget of {self.max_requests} exhausted ({self._requests_made} used)"
            )

    def _reserve(self) -> None:
        """Take one request of the budget for an attempt about to be sent."""
        with self._budget_lock:
            self._check_budget(1)
            self._reserved += 1

    def _release(self, *, sent: bool) -> None:
        """End a reservation: count it as a request made (``sent``) or give it back."""
        with self._budget_lock:
            self._reserved -= 1
            if sent:
                self._requests_made += 1

    def spend(self, count: int = 1) -> None:
        """Count ``count`` requests against the budget, raising when it is exhausted."""
        with self._budget_lock:
            self._check_budget(count)
            self._requests_made += count

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_max, self.backoff_base * (2**attempt))

    def _wait(
        self,
        seconds: float,
        reason: str,
        waited: list[float],
        url: str,
        *,
        counted: bool = True,
    ) -> None:
        """Sleep ``seconds``, unless that would push this request past ``max_total_wait``.

        ``waited`` holds the running totals for the current request: ordinary waits
        first, then reset-header waits (``counted=False``), which are checked
        against ``max_reset_wait_total`` instead of ``max_total_wait``.
        """
        if not counted:
            spent = waited[1] if len(waited) > 1 else 0.0
            if spent + seconds > self.max_reset_wait_total:
                raise RateLimited(
                    seconds,
                    f"{url}: waiting {seconds:g}s more for the rate-limit reset would exceed "
                    f"the per-request reset wait budget of {self.max_reset_wait_total:g}s "
                    f"({spent:g}s already waited)",
                )
            if len(waited) > 1:
                waited[1] += seconds
            if seconds > LOG_WAIT_THRESHOLD:
                log.info("Waiting %.0fs before retrying (%s).", seconds, reason)
            self._sleep(seconds)
            return
        if waited[0] + seconds > self.max_total_wait:
            raise RateLimited(
                seconds,
                f"{url}: waiting {seconds:g}s more would exceed the per-request wait "
                f"budget of {self.max_total_wait:g}s ({waited[0]:g}s already waited)",
            )
        if seconds > LOG_WAIT_THRESHOLD:
            log.info("Waiting %.0fs before retrying (%s).", seconds, reason)
        waited[0] += seconds
        self._sleep(seconds)

    def _server_wait(self, response: httpx.Response) -> tuple[float | None, str]:
        """Return the wait a 429 asked for and which header it came from."""
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            return retry_after, "Retry-After"
        if self.reset_wait is not None:
            reset = self.reset_wait(response)
            if reset is not None:
                return reset, "rate-limit reset header"
        return None, "backoff"

    def _reset_margin(self) -> float:
        """The margin the ``reset_wait`` hook adds, when it was built with one."""
        try:
            return max(0.0, float(getattr(self.reset_wait, "margin", 0.0)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _notify(hooks: list[Callable[..., None]], *args: Any) -> None:
        """Call every hook; when one raises, still call the rest, then re-raise the first."""
        first: BaseException | None = None
        for hook in hooks:
            try:
                hook(*args)
            except BaseException as exc:
                if first is None:
                    first = exc
        if first is not None:
            raise first

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Mapping[str, Any] | None = None,
        auth: tuple[str, str] | None = None,
        before_attempt: BeforeAttemptHook | None = None,
        on_response: ResponseHook | None = None,
        on_attempt_failed: AttemptFailedHook | None = None,
        on_attempt_not_sent: AttemptNotSentHook | None = None,
        sends_credentials: bool | None = None,
    ) -> httpx.Response:
        """Send a request with rate limiting, budgeting, hooks and retries.

        Every attempt is first checked against the host allowlist (see
        :meth:`check_destination`); a refused attempt raises
        :class:`DisallowedHostError` before the pacer, the budget or any hook, and
        nothing is sent. The request as built is checked again, bound credential
        values included, after the ``before_attempt`` hooks; a refusal there releases
        the budget reservation and calls ``on_attempt_not_sent``. ``auth`` must be a
        ``(user, password)`` tuple; anything else raises ``TypeError``.
        ``sends_credentials`` says whether the request carries credentials (a login
        body, a token header, an ``auth`` tuple); it is required once an allowlist is
        installed (``TypeError`` otherwise), and a request that carries credentials
        may only go to a credential host. The client also detects
        credentials itself (see :meth:`detect_credentials`); either one is enough. A
        3xx response is never followed; one that points at a destination that is not
        allowed raises :class:`DisallowedHostError`.

        Returns the final response for any status other than 429 and 5xx; callers
        handle 401, 402, 403, 404 and so on. Raises :class:`RateLimited` when 429
        persists, the server asks for a wait longer than ``max_retry_wait`` (on 429
        or 5xx), or the waits would exceed ``max_total_wait``,
        :class:`SourceError` when 5xx or transport errors persist or on any other
        ``httpx`` error (an undecodable body, a redirect loop, an invalid URL),
        :class:`ReadTimedOut` on the first read timeout when ``retry_read_timeouts``
        is off (see :meth:`configure`), :class:`BudgetExceeded` when the request
        budget runs out, and whatever the ``classify_error`` or ``before_attempt``
        hooks raise.

        The per-call ``before_attempt``, ``on_response``, ``on_attempt_failed`` and
        ``on_attempt_not_sent`` run after the client-wide hooks of the same name (see
        :meth:`configure`). Every attempt whose ``before_attempt`` ran gets exactly
        one terminal callback (see the module docstring).
        """
        levels = (
            (self.before_attempt, self.on_attempt_not_sent),
            (before_attempt, on_attempt_not_sent),
        )
        after = [h for h in (self.on_response, on_response) if h is not None]
        failed = [h for h in (self.on_attempt_failed, on_attempt_failed) if h is not None]
        not_sent = [h for _, h in levels if h is not None]
        last_error: SourceError | None = None
        safe_url = redact_url(url)
        waited = [0.0, 0.0]
        # Only a (user, password) tuple can be checked against the bound credential
        # values: an httpx.Auth object adds its credentials at send time, unseen.
        if auth is not None and not isinstance(auth, tuple):
            raise TypeError(
                "HttpClient.request() accepts auth only as a (user, password) tuple, "
                f"not {type(auth).__name__}: other auth objects cannot be checked "
                "against the bound credential values"
            )
        if self._client.auth is not None:
            raise TypeError(
                "HttpClient does not support client-wide auth: pass auth=(user, password) "
                "on each request so it can be checked against the bound credential values"
            )
        if sends_credentials is None:
            if self._allowed_hosts is not None:
                raise TypeError(
                    "HttpClient.request() needs sends_credentials=True or False once a "
                    "host allowlist is installed"
                )
            sends_credentials = False
        credentialed = sends_credentials or self.detect_credentials(
            url, headers=headers, params=params, auth=auth, data=data, json=json
        )
        for attempt in range(self.max_retries + 1):
            # Before anything else, every attempt: a refused request costs nothing.
            self.check_destination(url, credentials=credentialed)
            if self._cancel.is_set():
                raise FetchCancelled("the scan is stopping")
            paused = self._bucket.acquire() or 0.0
            if self._cancel.is_set():
                # Cancelled while this attempt waited in the pacer: send nothing more.
                raise FetchCancelled("the scan is stopping")
            info = Attempt(method, url, attempt, params, paused)
            # Reserved, not just checked: another thread cannot take the same request.
            self._reserve()
            armed: list[AttemptNotSentHook] = []
            try:
                for before_hook, not_sent_hook in levels:
                    if before_hook is not None:
                        before_hook(info)
                    if not_sent_hook is not None:
                        armed.append(not_sent_hook)
                if self._cancel.is_set():
                    # Cancelled while a before_attempt hook waited (an owed reset).
                    raise FetchCancelled("the scan is stopping")
            except BaseException as exc:
                self._release(sent=False)
                # The levels whose before_attempt finished must release what they took.
                self._notify(armed, info, exc)
                raise
            try:
                built = self._client.build_request(
                    method, url, params=params, headers=headers, json=json, data=data
                )
                # The built request is what goes out: check its final URL, headers and
                # body too (query parameters are merged into the URL here).
                credentialed_built = credentialed or self.detect_credentials(built.url)
                self.check_destination(built.url, credentials=credentialed_built)
                self._check_bound_values(built, auth, credentialed_built)
            except BaseException as exc:
                self._release(sent=False)
                # Never sent: a payload that cannot be encoded, a bad URL, Ctrl-C.
                self._notify(not_sent, info, exc)
                if isinstance(exc, httpx.InvalidURL):
                    raise SourceError(f"invalid URL {safe_url}: {exc}") from exc
                raise
            self._release(sent=True)
            with self._budget_lock:
                self._in_flight += 1
            try:
                try:
                    response = self._client.send(built, auth=auth)
                finally:
                    with self._budget_lock:
                        self._in_flight -= 1
            except (*NOT_SENT_ERRORS, httpx.InvalidURL) as exc:
                self._notify(not_sent, info, exc)
                if isinstance(exc, httpx.InvalidURL):
                    raise SourceError(f"invalid URL {safe_url}: {exc}") from exc
                last_error = TransportFailure(f"transport error for {safe_url}: {exc}")
                if attempt < self.max_retries:
                    self._wait(
                        self._backoff(attempt), f"transport error for {safe_url}", waited, safe_url
                    )
                    continue
                raise last_error from exc
            except httpx.TransportError as exc:
                self._notify(failed, info, exc)
                if isinstance(exc, httpx.ReadTimeout) and not self.retry_read_timeouts:
                    raise ReadTimedOut(
                        f"no answer within the read timeout from {safe_url}"
                    ) from exc
                last_error = TransportFailure(f"transport error for {safe_url}: {exc}")
                if attempt < self.max_retries:
                    self._wait(
                        self._backoff(attempt), f"transport error for {safe_url}", waited, safe_url
                    )
                    continue
                raise last_error from exc
            except httpx.HTTPError as exc:
                # Sent, but no usable response (undecodable body, redirect loop).
                self._notify(failed, info, exc)
                raise SourceError(f"HTTP error for {safe_url}: {exc}") from exc
            except BaseException as exc:
                # Interrupted mid-send (Ctrl-C) or a bug: the request may have been
                # billed, so report it as failed, then re-raise.
                self._notify(
                    failed,
                    info,
                    CancelledRequest(
                        f"request interrupted by {type(exc).__name__}", request=built, cause=exc
                    ),
                )
                raise

            self.last_response_at = time.time()
            self._notify(after, info, response)
            self._check_redirect(response, url)
            status = response.status_code
            if status in (402, 429) and self.classify_error is not None:
                classified = self.classify_error(response)
                if classified is not None:
                    raise classified
            if status == 429:
                wait, origin = self._server_wait(response)
                from_reset = origin == "rate-limit reset header"
                cap = self.max_retry_wait + (self._reset_margin() if from_reset else 0.0)
                if wait is not None and wait > cap:
                    raise RateLimited(
                        wait,
                        f"{safe_url}: server asked for a long wait via {origin} "
                        f"(cap {self.max_retry_wait:g}s)",
                    )
                if attempt < self.max_retries:
                    delay = self._backoff(attempt) if wait is None else wait
                    self._wait(
                        delay,
                        f"429 from {safe_url}, {origin}",
                        waited,
                        safe_url,
                        counted=not from_reset,
                    )
                    continue
                raise RateLimited(wait, f"{safe_url}: retries exhausted")
            if status >= 500:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                last_error = SourceError(f"server error {status} for {safe_url}")
                if retry_after is not None and retry_after > self.max_retry_wait:
                    raise RateLimited(
                        retry_after,
                        f"{safe_url}: server error {status} asked for a long wait via "
                        f"Retry-After (cap {self.max_retry_wait:g}s)",
                    )
                if attempt < self.max_retries:
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    self._wait(delay, f"server error {status}", waited, safe_url)
                    continue
                raise last_error
            return response
        raise last_error or SourceError("request failed")  # pragma: no cover

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """Shorthand for ``request("GET", url, ...)``."""
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET ``url`` and decode the JSON body.

        Never raises raw ``httpx`` errors. A 401 or 403 raises
        :class:`SourceAuthError`; any other 4xx, a redirect (redirects are not
        followed) and a body that is not valid JSON raise :class:`SourceError`.
        Plus everything :meth:`request` raises.
        """
        response = self.get(url, **kwargs)
        safe_url = redact_url(url)
        status = response.status_code
        if status in (401, 403):
            raise SourceAuthError(f"HTTP {status} from {safe_url}")
        if 300 <= status < 400:
            raise SourceError(f"unexpected redirect (HTTP {status}) from {safe_url}")
        if status >= 400:
            raise SourceError(f"HTTP {status} from {safe_url}")
        try:
            return response.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise SourceError(f"invalid JSON from {safe_url}") from exc

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
