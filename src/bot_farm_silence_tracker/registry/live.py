"""The live profile check: is the account still there on the platform?

An archive cannot tell silence from removal: an account the platform suspended or
deleted stops posting just as an account that went quiet does. So before an account
becomes a registry ``candidate`` its public profile status on the live platform is
checked, and the result goes into its evidence file and the report drafts.

* ``bluesky``: one logged-out ``app.bsky.actor.getProfile`` on the public AppView
  (``public.api.bsky.app``), by DID, through the adapter and its host allowlist.
* ``reddit-archive`` and ``reddit``: Reddit has no logged-out API that works without
  credentials, so nothing is requested and the status is
  :data:`ARCHIVE_ONLY`. The human verifier must resolve it on the live site and
  record it in the evidence file ("live profile checked: active / suspended /
  deleted / not checked").
* ``x``: reading a profile needs credentials (and is billed), so nothing is requested
  and the status is :data:`NEEDS_CREDENTIALS`, resolved by the verifier the same way.

No check spoofs a user agent, uses a proxy or another identity: the adapters send
their own user agent to their own allowlisted hosts.

A ``suspended`` or ``deleted`` account does not become a candidate (removal is not
silence), and neither does one whose check failed (:data:`NOT_CHECKED`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.sources.base import Source, SourceConfig
from bot_farm_silence_tracker.sources.errors import SourceError
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.registry import load_source_class
from bot_farm_silence_tracker.timeutil import isoformat, utcnow

ACTIVE = "active"
SUSPENDED = "suspended"
DELETED = "deleted"
NOT_CHECKED = "not checked"
ARCHIVE_ONLY = "unknown (archive only)"
NEEDS_CREDENTIALS = "unknown (needs credentials)"
#: What a human verifier records in an evidence file.
REVIEWER_CHOICES = (ACTIVE, SUSPENDED, DELETED, NOT_CHECKED)
#: Statuses that keep an account from becoming a candidate.
BLOCKING = frozenset({SUSPENDED, DELETED, NOT_CHECKED})
#: How each platform is checked.
METHODS = {
    "bluesky": "app.bsky.actor.getProfile on public.api.bsky.app, logged out",
    "reddit-archive": "none: Reddit has no logged-out API without credentials",
    "reddit": "none: Reddit has no logged-out API without credentials",
    "x": "none: reading an X profile needs credentials",
}
#: Most requests one checker sends, and the request rate it keeps to.
DEFAULT_MAX_REQUESTS = 200
MAX_RPS = 0.5


@dataclass(frozen=True)
class LiveStatus:
    """The result of one live profile check."""

    status: str
    method: str
    checked_at: datetime | None = None
    #: Why the check failed, for a :data:`NOT_CHECKED` status.
    detail: str = ""

    @property
    def blocks_candidate(self) -> bool:
        return self.status in BLOCKING

    @property
    def needs_verifier(self) -> bool:
        """Whether a person must resolve the status on the live site."""
        return self.status not in (ACTIVE, SUSPENDED, DELETED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "method": self.method,
            "checked_at": isoformat(self.checked_at) if self.checked_at else None,
            "detail": self.detail,
        }


def offline_status(platform: str) -> LiveStatus:
    """The status when no request is made: unknown on platforms that cannot be checked
    without credentials, and not checked on Bluesky."""
    method = METHODS.get(platform, "none")
    if platform in ("reddit-archive", "reddit"):
        return LiveStatus(ARCHIVE_ONLY, method)
    if platform == "x":
        return LiveStatus(NEEDS_CREDENTIALS, method)
    return LiveStatus(NOT_CHECKED, method, detail="no live check was made")


class LiveChecker:
    """Checks profiles through the adapters, one shared request budget, politely.

    ``client_factory`` builds the HTTP client (tests pass a mocked one); the adapter
    restricts it to its own hosts. Without network access every Bluesky check comes
    back :data:`NOT_CHECKED`.
    """

    def __init__(
        self,
        *,
        now: datetime | None = None,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        client_factory: Callable[..., HttpClient] = HttpClient,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.now = now or utcnow()
        self.max_requests = max_requests
        self._factory = client_factory
        self._clock = clock
        self._sources: dict[str, Source] = {}

    @property
    def requests_made(self) -> int:
        return sum(s.http.requests_made for s in self._sources.values())

    def _source(self, platform: str) -> Source:
        source = self._sources.get(platform)
        if source is not None:
            return source
        cls = load_source_class(platform)
        http = self._factory(
            max_requests=max(1, self.max_requests - self.requests_made),
            requests_per_second=min(MAX_RPS, cls.default_rps),
            burst=1.0,
            timeout=cls.default_timeout,
            max_retry_wait=60.0,
            max_total_wait=120.0,
            max_reset_wait_total=120.0,
        )
        config = SourceConfig(
            http=http,
            credentials={},
            now=self.now,
            # A distant past cutoff: the check reads a profile, not a window.
            cutoff=datetime(1970, 1, 1, tzinfo=UTC),
        )
        source = cls(config)
        self._sources[platform] = source
        return source

    def check(self, platform: str, user_id: str, handle: str) -> LiveStatus:
        """The live status of one account (see the module notes). Never raises for a
        failed check: that is :data:`NOT_CHECKED` with the reason."""
        if platform != "bluesky":
            return offline_status(platform)
        method = METHODS[platform]
        try:
            source = self._source(platform)
            status = source.live_profile_status(Account(platform, user_id, handle))
        except (SourceError, OSError, ValueError) as exc:
            reason = f"the check failed ({type(exc).__name__})"
            return LiveStatus(NOT_CHECKED, method, detail=reason)
        if status is None:
            return offline_status(platform)
        return LiveStatus(status, method, self._clock())

    def close(self) -> None:
        for source in self._sources.values():
            source.close()
        self._sources.clear()


#: A function that checks one account: ``(platform, user_id, handle) -> LiveStatus``.
Check = Callable[[str, str, str], LiveStatus]


def no_live_check(platform: str, user_id: str, handle: str) -> LiveStatus:
    """A check that makes no request (see :func:`offline_status`)."""
    del user_id, handle
    return offline_status(platform)
