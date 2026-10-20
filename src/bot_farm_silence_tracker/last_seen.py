"""The optional "last seen in the archive" check of ``report --last-seen``.

For accounts that scored silent after the cutoff, it asks the archive when each
account's newest post or comment was, from the end of the scored window up to the
time of the check. That separates an account that went quiet for good from one that
paused and came back.

It is not a closure verdict. The archive keeps no account state: activity after the
window proves the account was not closed then, and no activity means the account may
be closed, suspended, deleted or simply abandoned, which the archive cannot tell
apart. It also cannot see data that was never archived or that was removed on request.

The check is read-only. It never opens the run's database for writing; the only file
it writes is an optional answer cache (a JSON Lines file) so an interrupted check can
continue without asking again. It uses the same existence query as the two-phase
pre-check (:meth:`~bot_farm_silence_tracker.sources.base.Source.newest_activity`,
``sort=desc``, ``limit=1``), so it costs two requests per account (one per kind) and
goes through the adapter's own rate limit, back-off and User-Agent. The check's
requests share one pooled budget, and the check stops when the archive fails every
query for several accounts in a row.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.models import STATUS_ACTIVE, Account
from bot_farm_silence_tracker.scoring import AccountResult, ScoringParams
from bot_farm_silence_tracker.sources.base import Source
from bot_farm_silence_tracker.sources.errors import FATAL_SOURCE_ERRORS, SourceError
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat

#: Platforms the check supports: the archive answers the existence query with one request.
PLATFORMS = ("reddit-archive",)

ACTIVE_AFTER_WINDOW = "active_after_window"
NO_ACTIVITY_FOUND = "no_activity_found"
INCONCLUSIVE = "inconclusive"
STATUSES = (ACTIVE_AFTER_WINDOW, NO_ACTIVITY_FOUND, INCONCLUSIVE)

SCOPE_SILENT = "silent"
SCOPE_ALL = "all"
SCOPES = (SCOPE_SILENT, SCOPE_ALL)

#: The columns added to the report, after every other column.
COLUMNS = ["newest_activity_utc", "newest_activity_kind", "last_seen_status", "scored_silent"]

#: A cached answer is reused for this long; after that the account is asked again.
REUSE_FOR = timedelta(hours=24)

#: The check stops after this many accounts in a row with a failed query and no activity
#: found (for example HTTP 500 or 503 from the archive, even for one kind only),
#: whatever the adapter's own guard does.
MAX_FAILED_IN_A_ROW = 3

NOTICE = (
    "LAST SEEN: newest_activity_utc is the newest post or comment the archive holds for "
    "the account from the end of the scored window to the time of the check. This is not a "
    "closure verdict, because the archive keeps no account state. Activity after the window "
    "means the account was not closed then. No activity means the account may be closed, "
    "suspended, deleted or simply abandoned, and the archive cannot tell which; it also "
    "cannot show data that was never archived or was removed on request, and its newest "
    "days may not be loaded yet. inconclusive means the archive did not answer, which says "
    "nothing about the account. Rows with an empty last_seen_status were not checked."
)


@dataclass(frozen=True)
class LastSeen:
    """The answer for one account."""

    user_id: str
    status: str
    #: The newest post or comment found, or ``None``.
    newest: datetime | None
    #: ``post`` or ``comment`` (the newer of the two; ``comment`` on a tie), or ``None``.
    kind: str | None
    #: Where the search started: the end of the data the score used.
    since: datetime
    #: When the archive was asked.
    checked: datetime

    def columns(self) -> dict[str, Any]:
        return {
            "newest_activity_utc": isoformat(self.newest) if self.newest else None,
            "newest_activity_kind": self.kind,
            "last_seen_status": self.status,
        }

    def to_json(self, run_id: int) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "user_id": self.user_id,
            "status": self.status,
            "newest": isoformat(self.newest) if self.newest else None,
            "kind": self.kind,
            "since": isoformat(self.since),
            "checked": isoformat(self.checked),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LastSeen:
        def when(key: str) -> datetime | None:
            value = data.get(key)
            return ensure_utc(datetime.fromisoformat(value)) if value else None

        since, checked = when("since"), when("checked")
        if since is None or checked is None:
            raise ValueError("missing time")
        status = data["status"]
        if status not in STATUSES:
            raise ValueError("unknown status")
        return cls(str(data["user_id"]), status, when("newest"), data.get("kind"), since, checked)


class AnswerCache:
    """Answers of an earlier, possibly interrupted check, in a JSON Lines file.

    Each answer is appended as soon as it is known. Only conclusive answers of the same
    run in the same database (``db_id``), for the same start, and not older than
    :data:`REUSE_FOR` are reused. An answer that claims to be from the future is not
    reused. A damaged line is skipped, so the file can be cut off at any point. The file
    is not compacted on load: it only grows by the answers of each check.
    """

    def __init__(self, path: Path, run_id: int, db_id: str | None = None) -> None:
        self.path = path
        self.run_id = run_id
        self.db_id = db_id or ""

    def ensure_writable(self) -> None:
        """Open the file for appending, so an unwritable path fails before any request.

        Raises:
            OSError: the file or its folder cannot be created or written.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n"):
            pass

    def load(self, now: datetime) -> dict[str, LastSeen]:
        found: dict[str, LastSeen] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return found
        for line in lines:
            try:
                data = json.loads(line)
                if data.get("run_id") != self.run_id or (data.get("db_id") or "") != self.db_id:
                    continue
                answer = LastSeen.from_json(data)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if answer.status != INCONCLUSIVE and timedelta(0) <= now - answer.checked <= REUSE_FOR:
                found[answer.user_id] = answer
        return found

    def add(self, answer: LastSeen) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {**answer.to_json(self.run_id), "db_id": self.db_id}
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record) + "\n")


@dataclass
class LastSeenReport:
    """The answers of one check, and what it did."""

    scope: str
    #: The end of the search: when the archive was asked.
    until: datetime
    answers: dict[str, LastSeen] = field(default_factory=dict)
    #: Requests sent to the archive by this check (answers reused from the cache cost none).
    requests: int = 0
    reused: int = 0
    #: Why the check stopped before it reached every account, or ``None``.
    stopped: str | None = None

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for a in self.answers.values() if a.status == s) for s in STATUSES}

    def to_dict(self) -> dict[str, Any]:
        """The check as the JSON report records it (no account names)."""
        return {
            "scope": self.scope,
            "checked_until": isoformat(self.until),
            "accounts_checked": len(self.answers),
            "requests": self.requests,
            "answers_reused": self.reused,
            "stopped": self.stopped,
            **self.counts(),
        }


def validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"--last-seen-scope must be {' or '.join(SCOPES)}, got {scope!r}")
    return scope


def select_targets(results: Iterable[AccountResult], scope: str) -> list[AccountResult]:
    """The accounts to ask about: the silent ones, or every account the scan observed.

    An opted-out account is never asked about, and neither is one the platform
    reported as unavailable or the scan could not fetch: only ``active`` accounts have
    a history to compare with.
    """
    chosen = [r for r in results if r.status == STATUS_ACTIVE and (scope == SCOPE_ALL or r.cliff)]
    return sorted(chosen, key=lambda r: r.user_id)


def window_end(result: AccountResult, params: ScoringParams) -> datetime:
    """Where the data behind the account's score ended: the search starts there."""
    return ensure_utc(result.data_end or params.effective_now)


class StoppedAfterAnswer(Exception):
    """A fatal error ended the check of one account after activity was already found.

    Found activity always wins, so the answer is kept; the caller records it and then
    stops the check with ``error`` as the reason.
    """

    def __init__(self, answer: LastSeen, error: BaseException) -> None:
        super().__init__(str(error))
        self.answer = answer
        self.error = error


def _check(
    source: Source, account: Account, since: datetime, until: datetime, checked: datetime
) -> tuple[LastSeen, bool]:
    """:func:`check_account`, and whether the account counts as failed: a query errored and
    no activity was found, so the answer is inconclusive. Found activity never counts."""
    newest: datetime | None = None
    kind: str | None = None
    errors: list[SourceError] = []
    partial = False
    fatal: SourceError | None = None
    for query_kind in source.precheck_kinds:
        try:
            found = source.newest_activity(account, query_kind, since, until)
        except FATAL_SOURCE_ERRORS as exc:
            fatal = exc
            break
        except SourceError as exc:  # Includes SlowQueryError and read timeouts.
            errors.append(exc)
            continue
        if ensure_utc(found.covered_since) > since:
            partial = True
        if found.newest is not None:
            stamp = ensure_utc(found.newest)
            if newest is None or stamp > newest:
                newest, kind = stamp, query_kind
        elif found.unreadable:
            # A row came back that cannot be read: that is not "no activity".
            errors.append(SourceError("the archive returned a row without a usable timestamp"))
    if fatal is None:
        # The adapter's overload guard counts an account once, whichever query failed;
        # it raises (a fatal error) when too many accounts in a row failed.
        try:
            source.precheck_finished(account, errors)
        except FATAL_SOURCE_ERRORS as exc:
            fatal = exc
    if newest is not None:
        status = ACTIVE_AFTER_WINDOW
    elif errors or partial or fatal is not None:
        status = INCONCLUSIVE
    else:
        status = NO_ACTIVITY_FOUND
    answer = LastSeen(account.user_id, status, newest, kind, since, checked)
    if fatal is not None:
        if newest is None:
            raise fatal
        raise StoppedAfterAnswer(answer, fatal) from fatal
    return answer, bool(errors) and newest is None


def check_account(
    source: Source, account: Account, since: datetime, until: datetime, checked: datetime
) -> LastSeen:
    """Ask for the newest post and the newest comment of one account after ``since``.

    Found activity always wins: if either kind of query found a post or comment, the
    answer is ``active_after_window``. Otherwise the answer is ``inconclusive`` when a
    query failed (for example the archive timed out on every retry), when a returned
    row could not be read, or when a query had to be narrowed to a later start and so
    did not cover the whole span. It is ``no_activity_found`` only when every query ran
    over the whole span and found nothing.

    Raises:
        FATAL_SOURCE_ERRORS (budget used up, authentication, rate-limit guard) when no
            activity was found yet: the caller stops the check; the answers so far stay
            valid.
        StoppedAfterAnswer: the same errors after activity was found. The answer is in
            the exception, because found activity always wins.
    """
    return _check(source, account, since, until, checked)[0]


def uncached_count(
    targets: Sequence[AccountResult],
    params: ScoringParams,
    cache: AnswerCache,
    until: datetime,
) -> int:
    """How many targets the cache cannot answer (the ones that cost requests)."""
    cached = cache.load(ensure_utc(until))
    return sum(
        1
        for r in targets
        if (prev := cached.get(r.user_id)) is None or prev.since != window_end(r, params)
    )


def run_check(
    source: Source,
    params: ScoringParams,
    targets: Sequence[AccountResult],
    *,
    scope: str,
    until: datetime,
    cache: AnswerCache | None = None,
    requests_made: Callable[[], int] | None = None,
) -> LastSeenReport:
    """Check every target, one after another, and return the answers.

    ``requests_made`` is a callable giving the number of requests sent so far.
    When a fatal error ends the check early (the request budget, the overload guard),
    or :data:`MAX_FAILED_IN_A_ROW` accounts in a row had a failed query and no activity,
    the accounts not reached are recorded as ``inconclusive`` and ``stopped`` says why.
    An account for which activity was found before the error keeps that answer.
    """
    until = ensure_utc(until)
    report = LastSeenReport(scope=scope, until=until)
    cached = cache.load(until) if cache is not None else {}
    stop: str | None = None
    failed_in_a_row = 0
    for result in targets:
        since = window_end(result, params)
        previous = cached.get(result.user_id)
        if previous is not None and previous.since == since:
            report.answers[result.user_id] = previous
            report.reused += 1
            continue
        if stop is not None or since >= until:
            report.answers[result.user_id] = LastSeen(
                result.user_id, INCONCLUSIVE, None, None, since, until
            )
            continue
        account = Account(result.platform, result.user_id, result.handle)
        try:
            answer, failed_without_activity = _check(source, account, since, until, until)
        except StoppedAfterAnswer as stopped:
            stop = f"{type(stopped.error).__name__}: {stopped.error}"
            report.answers[result.user_id] = stopped.answer
            if cache is not None:
                cache.add(stopped.answer)
            continue
        except FATAL_SOURCE_ERRORS as exc:
            stop = f"{type(exc).__name__}: {exc}"
            report.answers[result.user_id] = LastSeen(
                result.user_id, INCONCLUSIVE, None, None, since, until
            )
            continue
        report.answers[result.user_id] = answer
        if cache is not None and answer.status != INCONCLUSIVE:
            cache.add(answer)
        failed_in_a_row = failed_in_a_row + 1 if failed_without_activity else 0
        if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
            stop = (
                f"the archive failed a query for {MAX_FAILED_IN_A_ROW} accounts in a row "
                "(it may be down or overloaded)"
            )
    report.stopped = stop
    if requests_made is not None:
        report.requests = requests_made()
    return report
