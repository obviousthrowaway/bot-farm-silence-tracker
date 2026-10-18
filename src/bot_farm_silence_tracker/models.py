"""Core data models shared by sources, scoring, storage and the CLI.

Privacy note: activities carry only a hash of the content, never the raw text.
Adapters must hash content with :func:`content_hash` and discard the text.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_DELETED = "deleted"
STATUS_PRIVATE = "private"
#: The account asked the platform not to show it to logged-out viewers (Bluesky's
#: ``!no-unauthenticated`` label). Its activity is not read, and it is never written to
#: any output: only a count is reported. The cache keeps its ID, with the handle
#: replaced by the ID, so a resumed run can skip it without a lookup.
STATUS_OPTED_OUT = "opted_out"
#: Set by the pipeline when fetching an account failed with a non-fatal SourceError.
#: Never cached in the ``accounts`` table; it lives in the run's sample.
STATUS_ERROR = "error"
#: Set when an adapter decided not to fetch an account's activity: it cannot matter
#: (``skipped``, for example a lifetime post count below ``--min-posts``) or it does
#: not fit the remaining spend budget (``skipped_budget``). The reason is in
#: ``meta["skipped"]``. No activity is cached for the skip, so a later run fetches the
#: account again. Activity an earlier run cached for it is kept, and the skip is then
#: recorded in the run's sample only.
STATUS_SKIPPED = "skipped"
STATUS_SKIPPED_BUDGET = "skipped_budget"
SKIPPED_STATUSES = frozenset({STATUS_SKIPPED, STATUS_SKIPPED_BUDGET})

#: ``Account.meta`` key describing a timeline fetch that did not reach the window
#: start: ``{"reason": ..., "since": <ISO time the fetched data reaches back to>,
#: "posts": <posts returned>}``. The cached window then starts at ``since``.
PARTIAL_FETCH_KEY = "partial_fetch"
#: The platform stopped returning older history (refetching cannot help).
PARTIAL_PLATFORM_CAP = "platform_cap"
#: The fetch was interrupted by the spend budget; the rest can still be fetched.
PARTIAL_BUDGET = "budget"
#: The fetch was interrupted by an error; the rest can still be fetched.
PARTIAL_ERROR = "error"
#: Reasons whose missing older part a later run fetches (only the gap).
PARTIAL_INTERRUPTED = frozenset({PARTIAL_BUDGET, PARTIAL_ERROR})

#: Statuses that mean the account's activity cannot be read.
UNAVAILABLE_STATUSES = frozenset(
    {STATUS_SUSPENDED, STATUS_DELETED, STATUS_PRIVATE, STATUS_OPTED_OUT}
)
#: Unavailable statuses that ``--include-unavailable`` may list in the output. An
#: opted-out account is counted only, never listed.
LISTABLE_UNAVAILABLE_STATUSES = UNAVAILABLE_STATUSES - {STATUS_OPTED_OUT}
ALL_STATUSES = UNAVAILABLE_STATUSES | {STATUS_ACTIVE}

ACTIVITY_KINDS = frozenset({"post", "comment", "tweet", "reply", "repost", "quote"})


def content_hash(text: str) -> str:
    """Return a stable, truncated SHA-256 of normalized text.

    The text is NFKC-normalized, case-folded and whitespace-collapsed first so that
    trivial edits do not change the hash. The hash is only meant for duplicate
    detection; the raw text must never be stored.
    """
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


@dataclass
class Account:
    """A platform account.

    ``status`` is one of ``active``, ``suspended``, ``deleted``, ``private`` or
    ``opted_out``, or
    ``error`` when the pipeline could not fetch the account. Adapters should never
    need to set it directly; the pipeline sets it when it catches
    :class:`bot_farm_silence_tracker.sources.base.AccountUnavailable` or a per-account
    :class:`bot_farm_silence_tracker.sources.base.SourceError`.
    ``meta`` is free-form, JSON-serializable adapter data (for example the
    community the account was discovered in). It is persisted in the cache.
    Reserved keys the core reads:

    * ``meta["truncated"]``: set it to ``True`` in ``fetch_activity`` when the
      platform stopped returning history before ``since``. Scoring and output
      surface it, because a truncated baseline can hide a real cliff.
    * ``meta["skipped"]`` and ``meta["skip_min_posts"]``: why an account was not
      fetched (status ``skipped``) and, when the reason was a lifetime post count
      below ``--min-posts``, the threshold in force. ``report`` warns when it is
      run with a lower ``--min-posts``, because those accounts were never fetched.
    """

    platform: str
    user_id: str
    handle: str
    created_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_ACTIVE

    @property
    def key(self) -> tuple[str, str]:
        """Return the ``(platform, user_id)`` identity of this account."""
        return (self.platform, self.user_id)


@dataclass(frozen=True)
class Activity:
    """One public action by an account.

    ``ts`` must be timezone-aware UTC. ``kind`` is one of ``post``, ``comment``,
    ``tweet``, ``reply``, ``repost`` or ``quote``. ``content_hash`` is a hash of the content
    (see :func:`content_hash`), or ``None`` when the content is unavailable.
    """

    platform: str
    user_id: str
    ts: datetime
    kind: str
    community: str | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("Activity.ts must be timezone-aware")
        if self.kind not in ACTIVITY_KINDS:
            raise ValueError(f"unknown activity kind: {self.kind!r}")
