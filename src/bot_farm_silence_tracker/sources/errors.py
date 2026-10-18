"""Exceptions that source adapters raise and the pipeline handles."""

from __future__ import annotations

from typing import Any

from bot_farm_silence_tracker.models import SKIPPED_STATUSES, UNAVAILABLE_STATUSES


class SourceError(Exception):
    """Base class for all source and HTTP errors.

    Two optional attributes let ``fetch_activity`` hand the pipeline what it knows
    when it fails part-way:

    * ``partial``: the activities of the pages already fetched (and paid for). The
      pipeline caches them with ``meta["truncated"] = True`` before it handles the
      error, so a rerun does not pay for them again.
    * ``retry_user_ids``: accounts recorded earlier in this run whose result this
      error makes doubtful (for example 403s that turn out to be an app-level
      refusal). The pipeline marks them ``error`` and drops their cached activity,
      so ``--from-run`` fetches them again.
    """

    partial: list[Any] | None = None
    retry_user_ids: tuple[str, ...] = ()


class SourceAuthError(SourceError):
    """Credentials are missing, invalid, expired or lack the needed scope.

    This is fatal for the whole scan: the pipeline stops and tells the user.
    """


class DisallowedHostError(SourceAuthError):
    """A request was refused before sending because its destination is not allowed.

    Raised by the shared HTTP client when a URL is not ``https``, when its host is
    not in the adapter's host allowlist, when a request that carries credentials
    targets a host that may not receive them, or when a response redirects to such
    a destination. Nothing is sent for a refused request. It subclasses
    :class:`SourceAuthError` so that it is fatal for the whole scan, like an auth
    failure, instead of being recorded as a per-account error.
    """


class RateLimited(SourceError):
    """The platform is throttling us and the HTTP helper's retries are exhausted.

    ``retry_after`` is the number of seconds the platform asked us to wait, or
    ``None`` if it did not say. The pipeline stops the scan and keeps what it has.
    """

    def __init__(self, retry_after: float | None = None, message: str = "") -> None:
        self.retry_after = retry_after
        detail = message or "rate limited"
        if retry_after is not None:
            detail += f" (retry after {retry_after:g}s)"
        super().__init__(detail)


class TransportFailure(SourceError):
    """Requests kept failing without any HTTP answer: no connection, a reset, a timeout.

    Raised by the shared HTTP client once its retries for transport errors are used up.
    The usual cause is the network going away, most often because the computer went to
    sleep, not anything about the account. The pipeline therefore waits and tries the
    account again, and does not count the failure toward ``--max-consecutive-errors``.
    """


def is_transport_failure(error: BaseException | None) -> bool:
    """Whether ``error``, or an error it was raised from, is a :class:`TransportFailure`.

    Adapters sometimes catch a failure and raise their own :class:`SourceError` ``from``
    it; following the chain keeps those recognised.
    """
    seen = 0
    while error is not None and seen < 10:
        if isinstance(error, TransportFailure):
            return True
        error = error.__cause__
        seen += 1
    return False


class AccountUnavailable(SourceError):
    """The account exists but its activity cannot be read.

    ``status`` is ``suspended``, ``deleted``, ``private`` or ``opted_out`` (the account
    asked not to be shown to logged-out viewers; see
    :data:`bot_farm_silence_tracker.models.STATUS_OPTED_OUT`). The pipeline records the
    status on the account instead of treating this as a failure, because a
    suspension is itself a signal. Adapters should raise this from ``hydrate`` or
    ``fetch_activity`` whenever the platform reports such a state.
    """

    def __init__(self, status: str, message: str = "") -> None:
        if status not in UNAVAILABLE_STATUSES:
            raise ValueError(f"invalid unavailable status: {status!r}")
        self.status = status
        super().__init__(message or f"account is {status}")


class AccountSkipped(SourceError):
    """The adapter decided not to fetch this account's activity. Not an error.

    ``status`` is ``skipped`` (the account cannot matter, for example its lifetime
    post count is below ``--min-posts``) or ``skipped_budget`` (its projected cost
    does not fit the remaining spend budget). ``meta`` is merged into
    ``Account.meta`` and should say why (``meta["skipped"]``). The pipeline records
    the status, caches no activity for the account (so it is never mistaken for an
    account with no posts) and goes on with the next one.
    """

    def __init__(self, status: str, message: str = "", meta: dict[str, Any] | None = None) -> None:
        if status not in SKIPPED_STATUSES:
            raise ValueError(f"invalid skipped status: {status!r}")
        self.status = status
        self.meta = dict(meta or {})
        super().__init__(message or f"account was {status}")


class BudgetExceeded(SourceError):
    """The ``--max-requests`` budget is used up.

    The pipeline stops the scan, keeps what it has and marks the run partial.
    """


class ReadTimedOut(SourceError):
    """A sent request got no answer within the client's read timeout.

    Raised by :meth:`HttpClient.request` at once, without a retry, only for a client
    configured with ``retry_read_timeouts=False``. An adapter turns it into its own
    handling of a slow query (the reddit-archive adapter narrows the query, as for a
    timeout 422); anywhere else it is an ordinary per-account error.
    """


class StaleSourceError(SourceError):
    """The source's data ends too soon after the cutoff to judge silence.

    Raised by the pipeline before any sampling when ``Source.data_horizon`` is
    earlier than ``cutoff + min_silence_days`` and ``--allow-stale-source`` was not
    given.
    """


#: Errors that end the whole scan rather than one account.
FATAL_SOURCE_ERRORS: tuple[type[SourceError], ...] = (SourceAuthError, BudgetExceeded, RateLimited)


class FetchCancelled(Exception):
    """A request or a wait was cancelled because the scan is stopping.

    Raised by the shared HTTP client after :meth:`HttpClient.cancel`, from its
    waits and before any further attempt. The concurrent fetch loop cancels the
    client when one account ends the scan (budget, rate limit, Ctrl-C), so the other
    workers stop instead of finishing their accounts. It is deliberately not a
    :class:`SourceError`: no adapter may treat it as a per-account failure, and the
    pipeline leaves the interrupted account unfetched so a resume fetches it again.
    """
