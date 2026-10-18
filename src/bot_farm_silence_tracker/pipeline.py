"""Orchestration: sample, fetch (through the cache), score, summarize.

The CLI is a thin layer over :func:`run_scan` and :func:`rescore_run`, which makes
them easy to test without a terminal.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import secrets
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.controls import (
    PERMUTATIONS,
    PlaceboComparison,
    PlaceboRun,
    SignalContext,
    annotate,
    discovery_groups,
    placebo_comparison,
    placebo_seed,
    score_pass,
    shifted_params,
    signal_context,
    stored_placebo_runs,
)
from bot_farm_silence_tracker.early_exit import (
    FETCH_AFTER_STOP,
    FETCH_PHASE2_PENDING,
    FETCH_PRECHECK_ONLY,
    PHASE2_COMPLETE,
    PHASE2_FAILED,
    PHASE2_PENDING,
    SAMPLED_STRATA,
    STRATUM_CANDIDATE,
    STRATUM_UNAVAILABLE,
    Design,
    Precheck,
    design_for,
    precheck,
    reclassify,
    sampling_summary,
    score_two_phase,
    selection_draw,
    summary_keys,
)
from bot_farm_silence_tracker.models import (
    LISTABLE_UNAVAILABLE_STATUSES,
    PARTIAL_BUDGET,
    PARTIAL_ERROR,
    PARTIAL_FETCH_KEY,
    PARTIAL_INTERRUPTED,
    PARTIAL_PLATFORM_CAP,
    SKIPPED_STATUSES,
    STATUS_ACTIVE,
    STATUS_ERROR,
    STATUS_OPTED_OUT,
    STATUS_SKIPPED,
    UNAVAILABLE_STATUSES,
    Account,
    Activity,
)
from bot_farm_silence_tracker.sampling import (
    DEFAULT_POOL_FACTOR,
    DEFAULT_SAMPLE_SIZE,
    SampleResult,
    sample_accounts,
)
from bot_farm_silence_tracker.scoring import (
    AccountResult,
    ScoringParams,
    partial_reason_of,
    rank,
    score_accounts,
)
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import AccountUnavailable, Source
from bot_farm_silence_tracker.sources.errors import (
    FATAL_SOURCE_ERRORS,
    AccountSkipped,
    BudgetExceeded,
    FetchCancelled,
    RateLimited,
    SourceError,
    StaleSourceError,
    is_transport_failure,
)
from bot_farm_silence_tracker.storage import (
    SAMPLE_ERROR,
    SAMPLE_FETCHED,
    SAMPLE_PENDING,
    SAMPLE_PRECHECKED,
    SAMPLE_SKIPPED,
    FileLock,
    RunHeartbeat,
    Storage,
)
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, parse_datetime, utcnow

log = logging.getLogger(__name__)

#: Seconds to wait before each new try of an account whose requests all failed without
#: an HTTP answer (the HTTP client has already used its own short retries). The network
#: usually returns within a minute or two after the computer wakes from sleep.
NETWORK_RETRY_PAUSES: tuple[float, ...] = (30.0, 60.0, 120.0)
#: Accounts in a row that still fail without an answer after those pauses end the scan,
#: as a network that stays down is not worth working through the whole sample. These
#: failures never count toward ``--max-consecutive-errors``.
MAX_NETWORK_FAILURES = 3

#: Option keys that look like secrets. They are never stored in the runs table.
#: Kept for callers that match names with it directly; :func:`is_secret_option`
#: applies the finer segment rules below and is what the pipeline uses.
SECRET_OPTION_PATTERN = re.compile(
    r"(^|[_-])(token|secret|password|passwd|passphrase|pwd|pass|key|apikey|api_key"
    r"|private_key|privatekey|bearer|auth|credential|credentials|cookie|session)([_-]|$)",
    re.IGNORECASE,
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
#: Splits an acronym from the next word: ``XAPIKey`` -> ``XAPI_Key``.
_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_SEGMENT_SPLIT = re.compile(r"[_\-.\s]+")

#: Segments that mark a secret wherever they appear in the name.
SECRET_TERMS_ANYWHERE: frozenset[str] = frozenset(
    {
        "secret", "password", "passwd", "passphrase", "pwd", "apikey", "privatekey",
        "bearer", "credential", "credentials", "cookie", "jwt", "sessionid", "passkey",
        "pem", "hmac", "dsn", "databaseurl", "mnemonic", "totpseed", "otpseed",
        "seedphrase", "hotpseed", "mfaseed", "2faseed", "twofaseed", "recoveryseed",
        "walletseed", "seedwords",
    }
)  # fmt: skip
#: Segments that, followed by ``seed``, mark a secret (``totp_seed``, ``otp_seed``).
#: A bare ``seed`` is not a secret: ``world_seed`` and ``seed`` are random seeds.
SECRET_SEED_PREFIXES: frozenset[str] = frozenset(
    {"totp", "otp", "hotp", "mfa", "2fa", "twofa", "recovery", "wallet", "fa"}
)
#: Segments that mark a secret only as the LAST segment of the name, or when the
#: next segment is one of :data:`SECRET_ID_SUFFIXES` (``token_budget`` is a
#: budget, ``api_token`` and ``key_id`` are secrets).
SECRET_TERMS_FINAL: frozenset[str] = frozenset({"token", "key"})
#: Segments that, following a secret term, still mark a secret.
SECRET_ID_SUFFIXES: frozenset[str] = frozenset({"id", "key", "token", "secret", "value"})
#: A version or number segment (``v2``, ``2``); after ``token`` or ``key`` it still
#: marks a secret (``api_key_v2``, ``token_2``). Also matched glued on
#: (``key2``, ``tokenV2``).
_VERSION_SEGMENT = re.compile(r"v?\d+")
_GLUED_VERSION = re.compile(r"(token|key)v?\d+")
#: Plural ``keys``/``tokens`` are secrets after one of these (``api_keys``,
#: ``refresh_tokens``), but not elsewhere (``max_keys``).
SECRET_PLURAL_PREFIXES: frozenset[str] = frozenset({"api", "access", "refresh", "consumer"})


def is_secret_option(key: str, secret_options: Iterable[str] = ()) -> bool:
    """Whether option ``key`` looks like a secret and must not be stored.

    The key is split into segments on ``_``, ``-``, ``.``, camelCase and acronym
    boundaries (``XAPIKey`` gives ``xapi`` and ``key``), lowercased.
    It is a secret when it is one of ``secret_options`` (an adapter's
    ``Source.secret_options``, compared case-insensitively), or a segment is:

    * one of :data:`SECRET_TERMS_ANYWHERE` (``secret``, ``password``,
      ``passphrase``, ``pwd``, ``cookie`` ...), at any position;
    * ``token`` or ``key`` as the last segment, or followed by an id-ish segment
      (:data:`SECRET_ID_SUFFIXES`: ``id``, ``key``, ``token``, ``secret``,
      ``value``) or by a version or number segment (``v2``, ``2``), or with one
      glued on (``key2``), so ``api_key``, ``key_id``, ``api_key_v2`` and
      ``authToken`` are secrets but ``token_budget``, ``keywords`` and
      ``max_keys`` are not;
    * ``keys`` or ``tokens`` right after ``api``, ``access``, ``refresh`` or
      ``consumer`` (:data:`SECRET_PLURAL_PREFIXES`), so ``api_keys`` is a secret;
    * ``jwt``, ``sessionid`` or ``passkey``, at any position (they are in
      :data:`SECRET_TERMS_ANYWHERE`);
    * ``pem``, ``hmac``, ``dsn`` or ``mnemonic``, at any position, and ``database``
      followed by ``url`` (``database_url``, ``DATABASE_URL``);
    * ``seed`` only in a qualified form: after ``totp``, ``otp``, ``hotp``, ``mfa``,
      ``2fa``, ``twofa``, ``recovery`` or ``wallet`` (``totp_seed``, ``otpSeed``) or
      before ``phrase`` or ``words`` (``seed_phrase``, ``seed_words``). A bare ``seed`` segment,
      as in ``world_seed``, is a random seed and is stored;
    * ``session`` as the last segment or followed by ``id``, ``key`` or
      ``token`` (``session_id`` is a secret, ``sessionLength`` is not);
    * ``auth`` or ``authorization`` as the last segment, ``auth`` followed by
      ``token``, or either followed by ``header`` (``x_auth``, ``auth_token`` and
      ``authorization_header`` are secrets, ``auth_mode`` and ``authorName`` are
      not);
    * ``pass`` as the last segment only (``api_pass`` is a secret, ``pass_rate``
      and ``passThrough`` are not).

    The rule errs toward dropping: a dropped option only has to be passed again on
    resume, a stored secret leaks into the database.
    """
    text = str(key)
    if text.lower() in {s.lower() for s in secret_options}:
        return True
    spaced = _CAMEL_BOUNDARY.sub("_", _ACRONYM_BOUNDARY.sub("_", text))
    segments = [seg.lower() for seg in _SEGMENT_SPLIT.split(spaced) if seg]
    for i, seg in enumerate(segments):
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        prev = segments[i - 1] if i > 0 else None
        if seg in SECRET_TERMS_ANYWHERE:
            return True
        if seg == "database" and nxt == "url":
            return True
        if seg == "seed" and (prev in SECRET_SEED_PREFIXES or nxt in ("phrase", "words")):
            return True
        if seg in SECRET_TERMS_FINAL and (
            nxt is None or nxt in SECRET_ID_SUFFIXES or _VERSION_SEGMENT.fullmatch(nxt)
        ):
            return True
        if _GLUED_VERSION.fullmatch(seg):
            return True
        if seg in ("keys", "tokens") and prev in SECRET_PLURAL_PREFIXES:
            return True
        if seg in ("auth", "authorization") and nxt == "header":
            return True
        if seg == "session" and nxt in (None, "id", "key", "token"):
            return True
        if seg in ("auth", "authorization") and (nxt is None or (seg == "auth" and nxt == "token")):
            return True
        if seg == "pass" and nxt is None:
            return True
    return False


#: An authority with userinfo, with or without a scheme. Used for URLs that
#: ``urlsplit`` rejects, where failing open would store the credentials.
_AUTHORITY_WITH_USERINFO = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^/?#]*@")


# WHATWG special schemes ignore how many slashes follow ``scheme:``, so ``http:/u:p@h``,
# ``http:u:p@h`` and ``http:///u:p@h`` are all read by browsers as carrying credentials.
_SPECIAL_SCHEME_AUTHORITY = re.compile(r"^(?:https?|ftp|wss?):/*([^/?#]*)", re.IGNORECASE)

_URL_NOISE = {ord(c): None for c in "\t\r\n"}


def has_url_userinfo(value: Any) -> bool:
    """Whether ``value`` is a URL with a ``user:pass@`` (or ``user@``) part.

    Such a value carries credentials, so it is treated like a secret option: it is
    not stored with a run and is not inherited on resume.
    """
    if not isinstance(value, str):
        return False
    # Browsers and many HTTP clients drop tabs and newlines inside a URL, strip leading
    # whitespace, and read a backslash as a slash, so the check sees the same text.
    value = value.strip().translate(_URL_NOISE).replace("\\", "/")
    special = _SPECIAL_SCHEME_AUTHORITY.match(value)
    if special is not None and "@" in special.group(1):
        return True
    try:
        parts = urlsplit(value)
    except ValueError:
        # A malformed URL (an unbalanced bracket, say) cannot be parsed. Fail closed:
        # treat it as carrying credentials when the text before the first path, query
        # or fragment delimiter contains an ``@``.
        return _AUTHORITY_WITH_USERINFO.match(value) is not None
    # ``netloc`` is only set after ``//``, so a bare ``user@host`` is not a match.
    return "@" in parts.netloc


def _is_unstorable(key: Any, value: Any, secret_options: tuple[str, ...]) -> bool:
    """Whether option ``key`` with ``value`` must be left out of the stored params."""
    return is_secret_option(str(key), secret_options) or has_url_userinfo(value)


def storable_options(
    options: Mapping[str, Any], secret_options: Iterable[str] = ()
) -> dict[str, Any]:
    """Return the adapter options that may be stored with a run.

    Drops every key :func:`is_secret_option` flags and every value that is a URL
    with credentials (:func:`has_url_userinfo`), and converts the rest to JSON-safe
    primitives.
    """
    secrets = tuple(secret_options)
    out: dict[str, Any] = {}
    for key, value in options.items():
        text = value if isinstance(value, str | int | float | bool) else str(value)
        if _is_unstorable(key, text, secrets):
            continue
        out[str(key)] = text
    return out


def unstored_options(options: Mapping[str, Any], secret_options: Iterable[str] = ()) -> list[str]:
    """Return, sorted, the keys of ``options`` that :func:`storable_options` drops."""
    secrets = tuple(secret_options)
    return sorted(
        str(k)
        for k, v in options.items()
        if _is_unstorable(k, v if isinstance(v, str | int | float | bool) else str(v), secrets)
    )


@dataclass(frozen=True)
class ScanSettings:
    """Everything that defines one scan."""

    platform: str
    scoring: ScoringParams
    n: int = DEFAULT_SAMPLE_SIZE
    new_accounts_days: float | None = None
    pool_factor: float = DEFAULT_POOL_FACTOR
    seed: int | None = None
    cache_ttl_hours: float = 24.0
    #: Abort the fetch loop after this many accounts in a row fail with a
    #: non-fatal ``SourceError`` (a broken adapter or a platform outage).
    max_consecutive_errors: int = 10
    #: The ``--max-requests`` budget, stored so ``--from-run`` can reuse it.
    max_requests: int | None = None
    #: Adapter options. Only :func:`storable_options` of them reach the runs table.
    source_options: Mapping[str, Any] = field(default_factory=dict)
    #: With ``from_run``: refetch every account instead of reusing what the earlier
    #: run already fetched.
    refresh: bool = False
    #: Extra secret option keys (the adapter's ``Source.secret_options``).
    secret_options: tuple[str, ...] = ()
    #: Run even when the source's data horizon is too early to judge silence;
    #: the results are then forced provisional.
    allow_stale_source: bool = False
    #: Unstored option keys inherited from the run being resumed that were not passed
    #: again. Key names only. They are recorded again so a later resume still warns.
    inherited_unstored_keys: tuple[str, ...] = ()
    #: ``--active-within-days``: discovery window of the last D days before the
    #: effective now (clipped to the baseline window) instead of the whole baseline
    #: window. Recorded for provenance; a ``--from-run`` resume inherits it.
    active_within_days: float | None = None
    #: ``--concurrency``: accounts fetched at once (1 is the sequential loop). A
    #: performance setting only: results are the same, so a resume does not inherit it.
    concurrency: int = 1
    #: ``--baseline-sample-rate P``: two-phase sampling (see
    #: :mod:`bot_farm_silence_tracker.early_exit`).
    #: ``None`` (the default) is a full census. A resume must use the run's value.
    baseline_sample_rate: float | None = None
    #: Salt of the phase-2 draws. ``None`` takes the resumed run's salt, else ``seed``,
    #: else a random one; the run stores the salt it used, so a resume draws the same.
    sampling_salt: int | None = None
    #: The outage event the scan belongs to (see :mod:`bot_farm_silence_tracker.events`).
    #: Stored with the run; ``None`` (tests and old callers) stores nothing.
    event_id: str | None = None
    #: Whether ``scoring.now`` came from ``--now`` rather than the clock. Recorded with
    #: the run, so its output says how to repeat it.
    now_given: bool = False

    def to_dict(self, seeds: SeedSet | None = None) -> dict[str, Any]:
        """Serialize for the ``runs`` table. Secret-looking option keys are dropped."""
        data: dict[str, Any] = {
            "platform": self.platform,
            "n": self.n,
            "new_accounts_days": self.new_accounts_days,
            "pool_factor": self.pool_factor,
            "seed": self.seed,
            "cache_ttl_hours": self.cache_ttl_hours,
            "max_consecutive_errors": self.max_consecutive_errors,
            "max_requests": self.max_requests,
            "source_options": storable_options(self.source_options, self.secret_options),
            # Key names only, never values: lets a resume warn about options that
            # have to be passed again.
            "unstored_option_keys": sorted(
                set(unstored_options(self.source_options, self.secret_options))
                | set(self.inherited_unstored_keys)
            ),
            "scoring": self.scoring.to_dict(),
        }
        if self.active_within_days is not None:
            data["active_within_days"] = self.active_within_days
        if self.concurrency != 1:
            data["concurrency"] = self.concurrency
        if self.baseline_sample_rate is not None:
            data["baseline_sample_rate"] = self.baseline_sample_rate
            data["sampling_salt"] = self.sampling_salt
        if seeds is not None:
            data["seeds"] = list(seeds.items)
            data["seeds_sha256"] = seeds_digest(seeds.items)
        if self.event_id is not None:
            data["event_id"] = self.event_id
        # Provenance: what repeats the run (see provenance_of).
        data["tool_version"] = __version__
        data["now"] = isoformat(self.scoring.now)
        data["now_given"] = self.now_given
        return data


def seeds_digest(items: Iterable[str]) -> str:
    """SHA-256 of a seed list (one item per line, in order), to tell lists apart."""
    text = "".join(f"{item}\n" for item in items)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def provenance_of(run_params: Mapping[str, Any]) -> dict[str, Any]:
    """What a run records about how to repeat it: the discovery seed, the version that
    scanned it, the hash of its seed list and the reference time (``now``, and whether
    ``--now`` gave it). Runs made before these were recorded give ``None`` for the
    missing ones."""
    seeds = run_params.get("seeds")
    digest = run_params.get("seeds_sha256")
    if digest is None and isinstance(seeds, list):
        digest = seeds_digest(str(item) for item in seeds)
    scoring = run_params.get("scoring") or {}
    return {
        "seed": run_params.get("seed"),
        "scan_tool_version": run_params.get("tool_version"),
        "seeds_sha256": digest,
        "now": run_params.get("now") or scoring.get("now"),
        "now_given": run_params.get("now_given"),
    }


@dataclass
class ScanOutcome:
    """Result of a scan or a re-score."""

    run_id: int
    #: "complete", "partial", "cancelled", "failed" or "sampled" (--dry-run-cost).
    status: str
    results: list[AccountResult]
    summary: dict[str, Any]
    params: ScoringParams
    warnings: list[str] = field(default_factory=list)
    requests_made: int = 0
    #: The adapter's extra output columns (``Source.extra_columns``), as recorded with
    #: the run. Empty for adapters that add none.
    extra_columns: tuple[str, ...] = ()
    #: The parameters and cluster tests the signals are judged with (see
    #: :mod:`bot_farm_silence_tracker.controls`); ``None`` only for outcomes built by hand.
    signal_context: SignalContext | None = None
    #: The placebo comparison of the run, or ``None`` when none was computed.
    placebo: PlaceboComparison | None = None
    #: How the run can be repeated: the discovery seed, the tool version that scanned
    #: it, the hash of its seed list and whether ``--now`` was given (see
    #: :func:`provenance_of`).
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def context(self) -> SignalContext:
        """The signal context, built from the parameters alone when there is none."""
        if self.signal_context is None:
            self.signal_context = signal_context(self.results, self.params)
        return self.signal_context

    @property
    def candidates(self) -> list[AccountResult]:
        """Cliff accounts, best first."""
        return rank(r for r in self.results if r.cliff)

    @property
    def unavailable(self) -> list[AccountResult]:
        """Accounts that are suspended, deleted or private.

        Opted-out accounts (status ``opted_out``) are left out: they are only counted
        (``summary["opted_out"]``), never listed.
        """
        return sorted(
            (r for r in self.results if r.status in LISTABLE_UNAVAILABLE_STATUSES),
            key=lambda r: r.user_id,
        )


#: How many per-account error messages are quoted in the warnings.
_ERROR_EXAMPLES = 3


_UNOBSERVED = frozenset({FETCH_PRECHECK_ONLY, FETCH_PHASE2_PENDING, FETCH_AFTER_STOP})


def summarize(
    results: list[AccountResult],
    params: ScoringParams,
    sampled: int,
    *,
    extra_columns: Sequence[str] = (),
    reports_opted_out: bool = False,
) -> dict[str, Any]:
    """Build the headline numbers for the summary table.

    The adapter-specific counts appear only when they apply, so other platforms keep
    their summary byte for byte: ``opted_out`` when ``reports_opted_out`` (or when
    it is non-zero), ``deactivated`` with the ``deactivated`` extra column, and
    ``cliff_reposted_after_cutoff`` with the ``reposts_after_cutoff`` one.
    """
    statuses = Counter(r.status for r in results)
    skipped = sum(statuses.get(s, 0) for s in SKIPPED_STATUSES)
    cliffs = [r for r in results if r.cliff]
    # Two-phase sampling: accounts that were only pre-checked (or still wait for their
    # phase-2 fetch) were not scored. Always 0 in a census. A failed phase-2 fetch is
    # an error, counted as a census counts one.
    unobserved = sum(1 for r in results if r.fetch_status in _UNOBSERVED)
    interrupted = sum(1 for r in results if r.partial_reason in PARTIAL_INTERRUPTED)
    cluster_sizes: dict[int, int] = {}
    for r in cliffs:
        if r.cluster_id is not None:
            cluster_sizes[r.cluster_id] = r.cluster_size
    summary: dict[str, Any] = {
        "sampled": sampled,
        # Skipped accounts were never fetched, so they are not counted as scored.
        "scored": len(results) - skipped - unobserved,
        "suspended": statuses.get("suspended", 0),
        "deleted": statuses.get("deleted", 0),
        "private": statuses.get("private", 0),
    }
    opted_out = statuses.get(STATUS_OPTED_OUT, 0)
    if reports_opted_out or opted_out:
        # Asked not to be shown to logged-out viewers: counted, never listed.
        summary["opted_out"] = opted_out
    if "deactivated" in extra_columns:
        # Deleted accounts that the platform reports as deactivated (they may return).
        summary["deactivated"] = sum(1 for r in results if r.deactivated)
    summary.update(
        {
            "errors": statuses.get(STATUS_ERROR, 0),
            "skipped": skipped,
            "truncated": sum(1 for r in results if r.truncated),
            # Truncated by a fetch that was interrupted (budget or error) versus by the
            # platform no longer returning older history.
            "truncated_interrupted": interrupted,
            "truncated_platform": sum(1 for r in results if r.truncated) - interrupted,
            "eligible": sum(1 for r in results if r.eligible),
            "cliff": len(cliffs),
        }
    )
    if "reposts_after_cutoff" in extra_columns:
        # Cliff accounts that reposted after the cutoff although reposts are not
        # counted as activity: silent as posters, not necessarily inactive.
        summary["cliff_reposted_after_cutoff"] = sum(1 for r in cliffs if r.reposts_after_cutoff)
    summary.update(
        {
            "provisional": sum(1 for r in cliffs if r.provisional),
            "clusters": len(cluster_sizes),
            "largest_cluster": max(cluster_sizes.values(), default=0),
            "days_since_cutoff": round(params.days_since_cutoff, 2),
            # Accounts whose data ended before effective_now (reused or partial windows);
            # their silence was measured only to that end (the ``data_end`` column).
            "early_data_end": sum(1 for r in results if r.data_end is not None),
        }
    )
    if params.markers:
        # Runs with markers only: how many cliff accounts and which clusters began
        # their silence nearest to each marker (``cutoff`` is the main outage).
        nearest = Counter(r.nearest_marker for r in cliffs if r.nearest_marker)
        summary["cliff_nearest_marker"] = {a.id: nearest.get(a.id, 0) for a in params.anchors}
        clusters = {
            r.cluster_id: r.cluster_nearest_marker
            for r in cliffs
            if r.cluster_id is not None and r.cluster_nearest_marker
        }
        summary["cluster_nearest_marker"] = {str(k): clusters[k] for k in sorted(clusters)}
    return summary


def load_run_sample(storage: Storage, run_id: int, platform: str) -> SampleResult:
    """Rebuild the stored sample of ``run_id`` for ``scan --from-run``.

    Accounts that failed with status ``error`` last time are retried.

    Raises:
        LookupError: the run does not exist or has no stored sample.
        ValueError: the run is for a different platform.
    """
    run = storage.get_run(run_id)
    if run is None:
        raise LookupError(f"no run with id {run_id}")
    if run["platform"] != platform:
        raise ValueError(
            f"run {run_id} is a {run['platform']!r} run; it cannot be resumed with "
            f"--platform {platform}"
        )
    rows = storage.get_run_sample(run_id)
    if not rows:
        raise LookupError(
            f"run {run_id} has no stored sample (it predates sample storage or its "
            "discovery failed); run a fresh scan instead"
        )
    accounts = []
    for account, _ in rows:
        if account.status == STATUS_ERROR or account.status in SKIPPED_STATUSES:
            account.status = STATUS_ACTIVE
        accounts.append(account)
    old_cutoff = (run["params"].get("scoring") or {}).get("cutoff")
    return SampleResult(
        accounts=accounts,
        pool_size=len(accounts),
        considered=len(accounts),
        warnings=[
            f"Reusing the {len(accounts)}-account sample of run {run_id} "
            f"(discovered for cutoff {old_cutoff}); discovery was skipped."
        ],
    )


def run_scan(
    source: Source,
    seeds: SeedSet,
    storage: Storage,
    settings: ScanSettings,
    progress: Callable[[int, int], None] | None = None,
    *,
    from_run: int | None = None,
    sample_only: bool = False,
    on_run: Callable[[int], None] | None = None,
) -> ScanOutcome:
    """Run a full scan: sample accounts, fetch activity via the cache, score, cluster.

    The sample is stored with the run as soon as it is drawn. With ``from_run``,
    discovery is skipped and that run's stored sample is reused. Accounts the
    earlier run fetched are reused from the cache whatever their age, as long as
    the cached window starts at or before the baseline start (unless
    ``settings.refresh``); other accounts are reused when their cached activity is
    fresh (within ``cache_ttl_hours``, and ending within that long of
    ``scoring.now``). A partial cached window (a fetch interrupted by the budget or
    an error) is not reused as if complete: only the part older than it is fetched
    and merged (see :func:`_plan_reuse`), and an account that failed or was skipped
    in the earlier run is retried that way too. An account the earlier run never
    reached is judged by how the latest earlier run of the chain left it. The
    counts to fetch and to reuse are logged before fetching.
    The new run shares the earlier run's root run id, so ``scope="run"`` spend
    caps and the summary's ``chain_spend_usd`` cover the whole chain.

    Per account, a plain :class:`SourceError` records the account with status
    ``error`` and the scan continues, until ``settings.max_consecutive_errors``
    errors in a row abort the loop (``status="partial"``). Budget exhaustion and
    rate limiting end the scan early with ``status="partial"``. Authentication
    errors and anything unexpected mark the run ``failed`` and are re-raised. In
    every case the run is finished with the accounts processed so far, so
    ``report --run-id`` and ``scan --from-run`` work on it. A scan stopped on
    purpose (Ctrl-C, or Cancel in the app: :class:`FetchCancelled`) is finished as
    ``cancelled`` instead of ``failed``; both can be continued.

    The scan holds the lock of its run chain (:meth:`Storage.lock_run`, keyed by the
    chain's root run id) from before the run is created until it returns, and
    refreshes the run's heartbeat meanwhile, so no other process can continue the
    same chain at the same time. ``on_run`` receives the new run's id as soon as
    the run exists.

    With ``sample_only`` (``scan --dry-run-cost``), the run stops after the sample is
    stored, with status ``sampled`` (or ``partial`` if discovery stopped early), so a
    later ``scan --from-run`` fetches it without paying for discovery again.

    Raises:
        LookupError, ValueError: ``from_run`` cannot be resumed (raised before a new
            run is created).
        RunInUse: another process holds the lock of ``from_run``'s chain (raised
            before a new run is created).
        ValueError: ``settings.concurrency`` is above the source's
            ``max_concurrency``, or differs from the concurrency the source was built
            for (``source.config.concurrency``), also before a new run is created.
            Likewise when ``settings.baseline_sample_rate`` is out of range, the source
            does not support it, or it differs from the rate of the run ``from_run``
            continues.

    With ``settings.baseline_sample_rate`` the fetch runs in two phases (see
    :mod:`bot_farm_silence_tracker.early_exit`): every account is first pre-checked with existence
    queries over ``[cutoff - cliff_window_hours, now)`` and put in a stratum, then
    the full baseline is fetched, as a census fetches it, for the cliff candidates
    and, in draw order, a subsample of the others. Each account's stratum, inclusion
    probability and selection are stored with the run, so a resume and ``report``
    use the same design.
    """
    with _RunGuard(storage) as guard:
        return _run_scan(
            source,
            seeds,
            storage,
            settings,
            progress,
            from_run=from_run,
            sample_only=sample_only,
            on_run=on_run,
            guard=guard,
        )


class _RunGuard:
    """The run lock and the heartbeat a scan holds until :func:`run_scan` returns."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._locks: list[FileLock] = []
        self._heartbeat: RunHeartbeat | None = None

    def lock(self, root_run_id: int) -> None:
        """Take the chain's lock; raises :class:`RunInUse` when another scan has it."""
        held = self.storage.lock_run(root_run_id)
        if held is not None:
            self._locks.append(held)

    def beat(self, run_id: int) -> None:
        """Record the heartbeat of ``run_id`` now, and keep refreshing it."""
        try:
            self.storage.beat_run(run_id)
        except sqlite3.Error as exc:
            log.debug("Could not record the heartbeat of run %s: %s", run_id, exc)
        if str(self.storage.path) != ":memory:" and self._heartbeat is None:
            self._heartbeat = RunHeartbeat(self.storage.path, run_id).start()

    def __enter__(self) -> _RunGuard:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._heartbeat is not None:
            self._heartbeat.stop()
        for held in reversed(self._locks):
            held.release()


def _stop_status(exc: BaseException) -> str:
    """The status of a run that ``exc`` ended: ``cancelled`` for a stop on purpose."""
    return "cancelled" if isinstance(exc, (FetchCancelled, KeyboardInterrupt)) else "failed"


def _run_scan(
    source: Source,
    seeds: SeedSet,
    storage: Storage,
    settings: ScanSettings,
    progress: Callable[[int, int], None] | None,
    *,
    from_run: int | None,
    sample_only: bool,
    on_run: Callable[[int], None] | None,
    guard: _RunGuard,
) -> ScanOutcome:
    """The body of :func:`run_scan`, with ``guard`` holding the run lock."""
    _check_concurrency(source, settings)
    previous = storage.get_run(from_run) if from_run is not None else None
    settings = _check_sampling(source, settings, previous)
    two_phase = settings.baseline_sample_rate is not None
    placebo_note: str | None = None
    if two_phase and settings.scoring.placebo_weeks:
        # The fully fetched accounts of a two-phase run are not a simple sample, so
        # there is nothing to compare; nor is it worth fetching the older weeks.
        settings = replace(settings, scoring=replace(settings.scoring, placebo_weeks=0))
        placebo_note = (
            "Placebo controls are off for two-phase runs (--baseline-sample-rate): only "
            "some accounts are fetched in full, so there is no simple sample to compare."
        )
    params = settings.scoring
    # The placebo cutoffs draw samples of their own (see draw_placebo_runs), so the
    # run's own sample is fetched from the baseline start.
    since, until = params.fetch_start, params.now
    ttl = timedelta(hours=settings.cache_ttl_hours)
    resumed = (
        load_run_sample(storage, from_run, settings.platform) if from_run is not None else None
    )
    prefetched: set[str] = set()
    warnings: list[str] = [placebo_note] if placebo_note else []
    errored_before: set[str] = set()
    root_run_id: int | None = None
    chain_strata: dict[str, str] = {}
    precheck_runs: dict[str, int] = {}
    if from_run is not None:
        root_run_id = storage.root_run_id(from_run)
        guard.lock(root_run_id)  # Before the new run exists: a refusal leaves no trace.
        prefetched, errored_before = _chain_fetch_status(storage, from_run, root_run_id, warnings)
        if two_phase and not settings.refresh:
            chain_strata, precheck_runs = _chain_strata(storage, from_run, root_run_id)
    _housekeep_spend(storage)
    run_params = settings.to_dict(seeds)
    # Recorded so report writes the adapter's columns and counts; left out for an
    # adapter without any, so its stored parameters stay as they were.
    extra_columns = tuple(source.extra_columns)
    if extra_columns:
        run_params["extra_columns"] = list(extra_columns)
    if source.reports_opted_out:
        run_params["reports_opted_out"] = True
    if previous is not None:
        run_params["from_run"] = from_run
        # Discovery is skipped, so the seeds that built this sample are the original run's.
        original_seeds = (previous.get("params") or {}).get("seeds")
        if original_seeds is not None:
            run_params["seeds"] = original_seeds
    run_id = storage.create_run(settings.platform, run_params, utcnow(), root_run_id=root_run_id)
    if root_run_id is None:
        guard.lock(run_id)  # A fresh run is its own root; no one else knows its id yet.
    root_run_id = root_run_id if root_run_id is not None else run_id
    guard.beat(run_id)
    if on_run is not None:
        on_run(run_id)
    ledger = source.config.ledger
    if ledger is not None:
        ledger.bind_run(run_id, root_run_id)

    # The data horizon is checked once, before discovery (or, on a resume, before
    # fetching), so a stale source stops the scan before it spends anything more.
    try:
        params = resolve_data_horizon(source, settings, warnings)
    except BaseException as exc:  # Stale source, Ctrl-C or a bug: close the run, re-raise.
        stale_summary: dict[str, Any] = {
            "warnings": [*warnings, str(exc)],
            "root_run_id": root_run_id,
        }
        if ledger is not None:  # data_horizon may have made a (billed) request.
            with contextlib.suppress(Exception):
                stale_summary["spend_usd"] = round(ledger.spent_this_run(), 4)
                stale_summary["chain_spend_usd"] = round(ledger.spent_this_chain(), 4)
        _save_request_log(source, storage, run_id, settings.platform, stale_summary)
        storage.finish_run(
            run_id,
            status=_stop_status(exc),
            account_ids=[],
            summary=stale_summary,
            finished_at=utcnow(),
        )
        raise
    if params is not settings.scoring:
        run_params["scoring"] = params.to_dict()
        storage.update_run_params(run_id, run_params)
    # Where the source's data really ends: cached windows record it, reuse and the
    # staleness warning compare against it.
    reach = params.effective_now

    def cached_hydrate(account: Account) -> Account:
        cached = storage.get_account(account.platform, account.user_id)
        # An opted-out account is read again: it may have removed the label since,
        # and its cached row has no creation time to reuse anyway.
        if (
            cached is not None
            and cached.created_at is not None
            and cached.status != STATUS_OPTED_OUT
        ):
            account.created_at = cached.created_at
            account.status = cached.status
            return account
        try:
            source.hydrate(account)
        except AccountUnavailable as exc:
            account.status = exc.status
            storage.upsert_account(account, utcnow())
            raise
        if cached is not None and cached.status == STATUS_OPTED_OUT:
            # The label is gone: drop the cached window (an opted-out account has no
            # activity, so it would read as one with no posts) so the account is
            # fetched in full.
            storage.forget_activity(account.platform, account.user_id)
        storage.upsert_account(account, utcnow())
        return account

    # Two-phase sampling state: the design of every pre-checked account, the fresh
    # pre-check outcomes not yet recorded, and the phase-2 results.
    designs: dict[str, Design] = {}
    prechecks: dict[str, Precheck] = {}
    incomplete_prechecks: list[str] = []
    fresh_prechecks = 0
    warned_incomplete = False
    processed2: list[tuple[Account, list[Activity]]] = []

    def store_design(account: Account, design: Design) -> None:
        designs[account.user_id] = design
        storage.set_sample_design(
            run_id, account, design.stratum, design.inclusion_prob, design.selected
        )

    def precheck_status(account: Account, activities: list[Activity]) -> str:
        """Phase 1: give a recorded account its design, store it, return its status.

        A fresh pre-check decides the stratum; otherwise the stratum the chain stored
        is reused (the pre-check was not repeated). Unavailable always wins.
        """
        if account.status in SKIPPED_STATUSES:
            return SAMPLE_SKIPPED
        nonlocal fresh_prechecks, warned_incomplete
        fresh = prechecks.pop(account.user_id, None)
        if fresh is not None:
            stratum = fresh.stratum
            fresh_prechecks += 1
            if not fresh.complete:
                incomplete_prechecks.append(account.handle)
            if not warned_incomplete and _many_incomplete(
                len(incomplete_prechecks), fresh_prechecks
            ):
                # Said at once, so an operator can stop a scan whose savings are gone.
                warned_incomplete = True
                log.warning(
                    "%d of the %d accounts pre-checked so far could not be pre-checked in "
                    "full and become cliff candidates fetched in full: the archive is "
                    "struggling, and the two-phase scan is saving little. Consider stopping "
                    "(Ctrl-C) and resuming later with --from-run --refresh.",
                    len(incomplete_prechecks),
                    fresh_prechecks,
                )
        else:
            stratum = chain_strata.get(account.user_id, STRATUM_CANDIDATE)
        if account.status in UNAVAILABLE_STATUSES:
            stratum = STRATUM_UNAVAILABLE
        rate = settings.baseline_sample_rate
        salt = settings.sampling_salt
        assert rate is not None and salt is not None
        store_design(account, design_for(stratum, rate, salt, account.platform, account.user_id))
        return SAMPLE_FETCHED if stratum == STRATUM_UNAVAILABLE else SAMPLE_PRECHECKED

    def phase2_status(account: Account, activities: list[Activity]) -> str:
        """Phase 2: record a fetched account; an unavailable one becomes ``U``.

        A sampled ``A``/``C`` account whose history now looks like a cliff candidate's
        keeps its design (and so its weight ``1 / P_eff``); scoring flags it
        (``moved_after_precheck``) and :func:`_drift_warning` reports it.
        """
        design = designs.get(account.user_id)
        if design is not None and partial_reason_of(account) not in PARTIAL_INTERRUPTED:
            updated = reclassify(design, account)
            if updated != design:
                store_design(account, updated)
        return _done_status(account, activities)

    def fold_phases() -> None:
        """Put the phase-2 results in place of the phase-1 ones (idempotent)."""
        if not processed2:
            return
        processed[:] = _merge_phases(processed, processed2)
        processed2.clear()

    status = "complete"
    processed: list[tuple[Account, list[Activity]]] = []
    sample = SampleResult(accounts=[])
    fatal: BaseException | None = None
    errors: list[str] = []
    try:
        if resumed is not None:
            sample = resumed
        else:
            sample = sample_accounts(
                source,
                seeds,
                n=settings.n,
                new_accounts_days=settings.new_accounts_days,
                pool_factor=settings.pool_factor,
                seed=settings.seed,
                now=params.now,
                hydrate=cached_hydrate,
                max_consecutive_errors=settings.max_consecutive_errors,
            )
        warnings.extend(sample.warnings)
        if sample.truncated:
            status = "partial"
        storage.save_run_sample(run_id, sample.accounts)
        _flush_request_rows(source, storage, run_id, settings.platform)
        if sample_only and status == "complete":
            status = "sampled"

        # --dry-run-cost (sample_only) stops here: the sample is stored, nothing fetched.
        to_fetch = [] if sample_only else sample.accounts
        if two_phase:
            # Phase 1 reuses the stratum the chain stored instead of asking again;
            # nothing is read from the activity cache for it.
            reuse = [account.user_id in chain_strata for account in to_fetch]
            gaps: dict[str, datetime] = {}
            _warn_stale_precheck(
                storage,
                [precheck_runs[a.user_id] for a, use in zip(to_fetch, reuse, strict=True) if use],
                params,
                warnings,
            )
        else:
            reuse, gaps = _plan_reuse(
                storage, to_fetch, prefetched, settings, since, reach, ttl, errored=errored_before
            )
        reused_count = sum(reuse)
        if to_fetch and two_phase:
            log.info(
                "Phase 1: %d accounts to pre-check, %d keep the stratum of the run chain.",
                len(to_fetch) - reused_count,
                reused_count,
            )
        elif to_fetch:
            log.info(
                "%d accounts to fetch (%d of them only the part older than a partial "
                "cached window), %d reused from the cache.",
                len(to_fetch) - reused_count,
                len(gaps),
                reused_count,
            )
        old_cutoff = ((previous or {}).get("params") or {}).get("scoring", {}).get("cutoff")
        if old_cutoff and not two_phase and parse_datetime(str(old_cutoff)) != params.cutoff:
            moved = sum(
                1
                for account, use_cache in zip(to_fetch, reuse, strict=True)
                if use_cache and account.user_id in prefetched
            )
            if moved:
                warnings.append(
                    f"The cutoff ({isoformat(params.cutoff)}) differs from run {from_run}'s "
                    f"({old_cutoff}), but {moved} accounts that run fetched are reused from "
                    "the cache with the window fetched for the old cutoff. Rerun with "
                    "--refresh to refetch them for the new cutoff."
                )
        kept = [
            use or account.user_id in gaps for account, use in zip(to_fetch, reuse, strict=True)
        ]
        stale = [] if two_phase else _stale_reused(storage, to_fetch, kept, reach, ttl)
        if stale:
            warnings.append(
                f"{len(stale)} reused accounts were last fetched up to {isoformat(min(stale))}, "
                f"well before this run's reference time {isoformat(reach)}. Activity after "
                "that fetch is not seen, so these accounts can look silent when they are "
                "not. Rerun with --refresh to refetch them."
            )

        steps: _Steps = (
            _PrecheckSteps(source, storage, params, prechecks)
            if two_phase
            else _CacheSteps(source, storage, since, until, ttl, reach, gaps)
        )
        aborted = _fetch_loop(
            source,
            storage,
            run_id,
            settings,
            list(zip(to_fetch, reuse, strict=True)),
            steps,
            processed=processed,
            done_status=precheck_status if two_phase else _done_status,
            progress=progress,
            errors=errors,
            warnings=warnings,
        )
        if aborted or (
            two_phase
            and to_fetch
            and _phase_two(
                source,
                storage,
                run_id,
                settings,
                processed,
                designs,
                prefetched=prefetched,
                errored=errored_before,
                since=since,
                until=until,
                ttl=ttl,
                reach=reach,
                progress=progress,
                processed2=processed2,
                done_status=phase2_status,
                errors=errors,
                warnings=warnings,
            )
        ):
            status = "partial"
    except (BudgetExceeded, RateLimited) as exc:
        fold_phases()
        _retry_doubtful(exc, storage, run_id, processed, warnings)
        status = "partial"
        warnings.append(
            f"Stopped after {len(processed)} of {len(sample.accounts)} accounts ({exc}). "
            f"Results are partial; rerun with --from-run {run_id} to continue from the cache."
        )
    except BaseException as exc:  # Auth errors, bugs, Ctrl-C: save what we have, re-raise.
        fold_phases()
        _retry_doubtful(exc, storage, run_id, processed, warnings)
        fatal = exc
        status = _stop_status(exc)

    if errors:
        examples = "; ".join(errors[:_ERROR_EXAMPLES])
        extra = len(errors) - _ERROR_EXAMPLES
        more = f" (and {extra} more)" if extra > 0 else ""
        warnings.append(
            f"{len(errors)} accounts failed and were recorded with status 'error': {examples}{more}"
        )
    if incomplete_prechecks:
        warnings.append(
            f"The pre-check of {len(incomplete_prechecks)} accounts could not answer its whole "
            "window (a failed or narrowed existence query); they were treated as cliff "
            f"candidates and fetched in full (for example {incomplete_prechecks[0]})."
        )
        if _many_incomplete(len(incomplete_prechecks), fresh_prechecks):
            warnings.append(
                f"WARNING: {len(incomplete_prechecks)} of {fresh_prechecks} pre-checks were "
                f"incomplete (more than {INCOMPLETE_PRECHECK_SHARE:.0%}): the archive was "
                "struggling, so most accounts were fetched in full and the two-phase scan "
                "saved little. The estimates stay valid. Next time resume later with "
                "--from-run --refresh, which pre-checks the accounts again."
            )
    warnings.extend(w for w in source.drain_warnings() if w not in warnings)

    fold_phases()
    draw: PlaceboDraw | None = None
    if not two_phase and params.placebo_weeks and not sample_only:
        if fatal is None and status == "complete":
            # A resumed run's sample was drawn by its first run: draw the placebo
            # samples the same way, with that run's size and pool factor.
            first = ((previous or {}).get("params") or {}) if from_run is not None else {}
            drawn_as = replace(
                settings,
                n=int(first.get("n") or settings.n),
                pool_factor=float(first.get("pool_factor") or settings.pool_factor),
            )
            try:
                draw = draw_placebo_runs(source, seeds, drawn_as, params, warnings=warnings)
            except BaseException as exc:  # Ctrl-C or a bug: save the run, re-raise.
                fatal = exc
                status = _stop_status(exc)
        else:
            draw = PlaceboDraw(
                unavailable="the run stopped before its own sample was fetched in full, and "
                "placebo samples are drawn only after that (continue it with scan --from-run)"
            )
    results: list[AccountResult] = []
    summary: dict[str, Any] = {}
    context: SignalContext | None = None
    placebo: PlaceboComparison | None = None
    try:
        window_ends = _window_ends(storage, processed)
        if two_phase:
            rate = settings.baseline_sample_rate
            assert rate is not None
            statuses = {a.user_id: st for a, st in storage.get_run_sample(run_id)}
            states = _phase2_states(processed, designs, statuses)
            results, p_eff = score_two_phase(
                processed, designs, states, rate, params, window_ends=window_ends
            )
        else:
            results = score_accounts(processed, params, window_ends=window_ends)
        groups = discovery_groups(sample.accounts)
        context, placebo = analyze(
            results,
            params,
            processed,
            groups=groups,
            window_ends=window_ends,
            covered_since=_coverage(storage, processed),
            two_phase=two_phase,
            draw=draw,
        )
        summary = summarize(
            results,
            params,
            sampled=len(sample.accounts),
            extra_columns=extra_columns,
            reports_opted_out=source.reports_opted_out,
        )
        add_analysis(summary, context, placebo)
        if two_phase:
            _add_sampling(summary, results, rate, p_eff, params)
            _drift_warning(results, warnings)
        _warn_early_data_end(results, params, warnings)
        # Stored so report --run-id measures silence to the same per-account ends.
        ends = [
            (a.platform, a.user_id, window_ends[a.user_id])
            for a, _ in processed
            if a.user_id in window_ends
        ]
        try:
            storage.set_sample_window_ends(run_id, ends)
        except sqlite3.Error as exc:
            # The per-account ends only help a later `report --run-id`; a busy or
            # locked database must not fail a scan that has already been paid for.
            # Roll back so a partly applied executemany is not committed by a later
            # write, which would leave some accounts with stored ends and some without.
            storage.rollback()
            warnings.append(
                f"Could not store the per-account window ends for run {run_id} ({exc}); "
                "a later report of this run will use the account's current cached window "
                "end, which may include data fetched after this run."
            )
            log.warning("Could not store the window ends for run %s: %s", run_id, exc)
    except Exception as exc:
        if fatal is None:
            fatal = exc
        status = _stop_status(exc)
    summary.update(
        pool_size=sample.pool_size,
        pool_limit=sample.pool_limit,
        dropped_unknown_age=sample.dropped_unknown_age,
    )
    if source.reports_opted_out or sample.dropped_opted_out:
        summary["dropped_opted_out"] = sample.dropped_opted_out
    summary["warnings"] = warnings
    if from_run is not None:
        summary["from_run"] = from_run
    summary["root_run_id"] = root_run_id
    summary["effective_now"] = isoformat(params.effective_now)
    summary["horizon"] = isoformat(params.horizon) if params.horizon is not None else None
    if ledger is not None:
        summary["spend_usd"] = round(ledger.spent_this_run(), 4)
        summary["chain_spend_usd"] = round(ledger.spent_this_chain(), 4)
    _save_request_log(source, storage, run_id, settings.platform, summary)
    storage.finish_run(
        run_id,
        status=status,
        account_ids=[acct.user_id for acct, _ in processed],
        summary=summary,
        finished_at=utcnow(),
    )
    if fatal is not None:
        # The CLI never reaches its warning printout on a fatal exit, so log them here.
        for warning in warnings:
            log.warning("%s", warning)
        raise fatal
    return ScanOutcome(
        run_id=run_id,
        status=status,
        results=results,
        summary=summary,
        params=params,
        warnings=warnings,
        requests_made=source.http.requests_made,
        extra_columns=extra_columns,
        signal_context=context,
        placebo=placebo,
        provenance=provenance_of(run_params),
    )


def analyze(
    results: list[AccountResult],
    params: ScoringParams,
    items: Sequence[tuple[Account, Sequence[Activity]]],
    *,
    groups: Mapping[str, str],
    window_ends: Mapping[str, datetime],
    covered_since: Mapping[str, datetime],
    two_phase: bool = False,
    draw: PlaceboDraw | None = None,
) -> tuple[SignalContext, PlaceboComparison]:
    """Test the run's stop-time clusters, set each result's signals, and compare the
    run with its placebo samples, ``draw`` (see :mod:`bot_farm_silence_tracker.controls`)."""
    context = signal_context(results, params, groups)
    annotate(results, context)
    if two_phase:
        placebo = PlaceboComparison(0)
        placebo.unavailable = (
            "two-phase runs (--baseline-sample-rate) fetch only some accounts in full, so "
            "there is no simple sample to compare"
        )
        return context, placebo
    draw = draw or PlaceboDraw()
    placebo = placebo_comparison(
        items,
        params,
        covered_since=covered_since,
        window_ends=window_ends,
        groups=groups,
        placebos=draw.runs,
        unavailable=draw.unavailable,
        requests=draw.requests,
    )
    return context, placebo


@dataclass
class PlaceboDraw:
    """The placebo samples' passes, or why there are none, and what they cost."""

    runs: list[PlaceboRun] | None = None
    unavailable: str | None = None
    #: Requests the placebo samples made (discovery and fetches).
    requests: int = 0


class _PlaceboStopped(Exception):
    """A placebo sample could not be drawn in full."""


def draw_placebo_runs(
    source: Source,
    seeds: SeedSet,
    settings: ScanSettings,
    params: ScoringParams,
    *,
    permutations: int = PERMUTATIONS,
    warnings: list[str] | None = None,
) -> PlaceboDraw:
    """Draw one placebo sample per placebo week and score it at its placebo cutoff.

    Sample ``k`` is drawn as the run's own sample was: the same seeds, sample size,
    pool factor and new-account filter, a seed derived from the run's seed and ``k``
    (:func:`~bot_farm_silence_tracker.controls.placebo_seed`), and the adapter's own
    discovery with the cutoff, the baseline and the effective now moved back ``k``
    weeks (:meth:`Source.shifted`). Each account is then fetched over the moved
    baseline and silence window and scored at the moved cutoff
    (:func:`~bot_farm_silence_tracker.controls.score_pass`). Nothing is cached:
    placebo data never mixes with the run's own.

    The comparison is unavailable, and says why, when the platform cannot discover in
    earlier weeks (``Source.placebo_discovery_problem``) or a placebo sample stops
    early (the budget, a rate limit, a login or discovery error): a partial sample is
    not selected like the run's own. The request count is reported either way.
    Adapter warnings raised meanwhile are added to ``warnings`` with the sample named.
    """
    weeks = int(params.placebo_weeks)
    problem = source.placebo_discovery_problem(seeds)
    if problem is not None:
        return PlaceboDraw(
            unavailable=f"not available on {source.name}: {problem}. Re-scoring the run's "
            "own sample at earlier cutoffs would be biased toward a clear excess, so no "
            "comparison is made"
        )
    before = source.http.requests_made
    log.info(
        "Drawing %d placebo samples of %d accounts each, discovered and fetched 1 to %d "
        "weeks earlier; this repeats discovery and fetching %d times (--placebo-weeks 0 "
        "turns it off).",
        weeks,
        settings.n,
        weeks,
        weeks,
    )
    runs: list[PlaceboRun] = []
    k = 0
    try:
        for k in range(1, weeks + 1):
            runs.append(
                draw_sample_pass(source, seeds, settings, params, k, permutations, warnings)
            )
    except (_PlaceboStopped, SourceError) as exc:
        reason = (
            f"the placebo sample {_weeks_back(k)} could not be drawn in full ({exc}), and "
            "a partial sample is not selected like the run's own"
        )
        if warnings is not None:
            warnings.extend(_placebo_warnings(source, k, warnings))
            warnings.append(f"No placebo comparison: {reason}.")
        return PlaceboDraw(unavailable=reason, requests=source.http.requests_made - before)
    requests = source.http.requests_made - before
    log.info("The placebo samples made %d requests beyond the run's own.", requests)
    return PlaceboDraw(runs=runs, requests=requests)


def _placebo_warnings(source: Source, weeks_before: int, known: Sequence[str]) -> list[str]:
    """The adapter's new warnings, named after the placebo sample; a warning the run
    already gave (the same skipped seed, say) is not repeated."""
    return [
        f"Placebo sample {_weeks_back(weeks_before)}: {w}"
        for w in source.drain_warnings()
        if w not in known
    ]


def _weeks_back(weeks: int) -> str:
    return f"{weeks} week back" if weeks == 1 else f"{weeks} weeks back"


def draw_sample_pass(
    source: Source,
    seeds: SeedSet,
    settings: ScanSettings,
    params: ScoringParams,
    weeks_before: int,
    permutations: int = PERMUTATIONS,
    warnings: list[str] | None = None,
) -> PlaceboRun:
    """Draw, fetch and score the placebo sample ``weeks_before`` weeks back.

    With ``weeks_before`` 0 it draws a sample the way a scan draws its own (with the
    seed derived for 0 instead of the run's), which the null-simulation tests use as
    the real pass.
    """
    shift = timedelta(weeks=weeks_before)
    shifted = shifted_params(params, weeks_before)
    seed = placebo_seed(settings.seed, weeks_before)
    # The fetch ends at the moved effective now, not the moved now. The run's own
    # sample was fetched from a source whose data ends at its horizon, so it cannot see
    # a post after that; a placebo sample weeks back has data up to its moved now, and
    # scoring counts every post up to ``now`` as a resume. Fetching past the moved
    # horizon would let a placebo pass see resumes the real pass cannot, push the
    # placebo cliff rate down and bias the comparison toward a clear excess.
    start, end = ensure_utc(shifted.fetch_start), ensure_utc(shifted.effective_now)
    items: list[tuple[Account, list[Activity]]] = []
    covered: dict[str, datetime] = {}
    with source.shifted(shift, now=ensure_utc(params.effective_now) - shift, seed=seed):
        sample = sample_accounts(
            source,
            seeds,
            n=settings.n,
            new_accounts_days=settings.new_accounts_days,
            pool_factor=settings.pool_factor,
            seed=seed,
            now=shifted.now,
            max_consecutive_errors=settings.max_consecutive_errors,
        )
        if sample.truncated:
            raise _PlaceboStopped(
                next((w for w in sample.warnings if "stopped" in w or "aborted" in w), "")
                or "discovery stopped early"
            )
        log.info(
            "Placebo sample %s: %d accounts to fetch.",
            _weeks_back(weeks_before),
            len(sample.accounts),
        )
        for account in sample.accounts:
            try:
                activities = _source_fetch(source, account, start, end)
            except FATAL_SOURCE_ERRORS:
                raise
            except AccountUnavailable as exc:
                account.status, activities = exc.status, []
            except AccountSkipped as exc:
                account.status, activities = exc.status, []
                account.meta.update(exc.meta)
            except SourceError:
                account.status, activities = STATUS_ERROR, []
            items.append((account, activities))
            if account.status == STATUS_ACTIVE:
                since = start
                info = account.meta.get(PARTIAL_FETCH_KEY)
                if isinstance(info, dict) and info.get("since"):
                    since = max(since, ensure_utc(parse_datetime(str(info["since"]))))
                covered[account.user_id] = since
    if warnings is not None:
        warnings.extend(_placebo_warnings(source, weeks_before, warnings))
    return score_pass(
        weeks_before,
        items,
        shifted,
        covered_since=covered,
        groups=discovery_groups(sample.accounts),
        permutations=permutations,
    )


def add_analysis(
    summary: dict[str, Any], context: SignalContext, placebo: PlaceboComparison
) -> None:
    """Add the cluster tests and the placebo comparison to a run summary."""
    summary["cluster_tests"] = [test.to_dict() for _, test in sorted(context.clusters.items())]
    summary["placebo"] = placebo.to_dict()


def _coverage(
    storage: Storage, items: Iterable[tuple[Account, Sequence[Activity]]]
) -> dict[str, datetime]:
    """Where each active account's fetched data starts: its cached window's start, or
    later when an interrupted fetch left only a newer part (``meta["partial_fetch"]``)."""
    out: dict[str, datetime] = {}
    for account, _ in items:
        if account.status != STATUS_ACTIVE:
            continue
        window = storage.activity_window(account.platform, account.user_id)
        if window is None:
            continue
        start = ensure_utc(window[0])
        info = account.meta.get(PARTIAL_FETCH_KEY)
        if isinstance(info, dict) and info.get("since"):
            start = max(start, ensure_utc(parse_datetime(str(info["since"]))))
        out[account.user_id] = start
    return out


def _done_status(account: Account, activities: list[Activity]) -> str:
    """The sample status of a recorded account: ``skipped`` or ``fetched``."""
    return SAMPLE_SKIPPED if account.status in SKIPPED_STATUSES else SAMPLE_FETCHED


class _Steps:
    """How the fetch loops handle one account, in three steps.

    ``prepare`` runs on the database thread and returns either the account's result
    (no source call needed) or a token for ``work``; ``work`` makes the source calls
    and may run in a worker thread (it must not touch the database); ``record`` stores
    what ``work`` returned or raised, on the database thread, and returns the result.
    ``record`` raises the errors that are not results (fatal ones after storing what
    they carry, and per-account failures for the loop to count).
    """

    def prepare(self, account: Account, use_cache: bool) -> Any:
        raise NotImplementedError

    def work(self, account: Account, token: Any) -> Any:
        raise NotImplementedError

    def record(
        self, account: Account, token: Any, result: Any, error: BaseException | None
    ) -> tuple[Account, list[Activity]]:
        raise NotImplementedError

    def load(self, account: Account, use_cache: bool) -> tuple[Account, list[Activity]]:
        """All three steps on the calling thread (the sequential loop)."""
        token = self.prepare(account, use_cache)
        if isinstance(token, tuple):
            return token
        try:
            result = self.work(account, token)
        except SourceError as exc:
            return self.record(account, token, None, exc)
        return self.record(account, token, result, None)


@dataclass
class _CacheSteps(_Steps):
    """Fetch through the activity cache for ``[since, until)`` (a census, phase 2)."""

    source: Source
    storage: Storage
    since: datetime
    until: datetime
    ttl: timedelta
    reach: datetime
    gaps: dict[str, datetime]

    def prepare(self, account: Account, use_cache: bool) -> Any:
        return _prepare_fetch(
            self.storage,
            account,
            self.since,
            self.until,
            self.ttl,
            use_cache=use_cache,
            gap_until=self.gaps.get(account.user_id),
        )

    def work(self, account: Account, token: Any) -> Any:
        return _source_fetch(self.source, account, self.since, token.fetch_until)

    def record(
        self, account: Account, token: Any, result: Any, error: BaseException | None
    ) -> tuple[Account, list[Activity]]:
        return _record_fetch(
            self.storage,
            account,
            self.since,
            self.until,
            token,
            activities=result,
            error=error,
            covered_until=self.reach,
        )

    def load(self, account: Account, use_cache: bool) -> tuple[Account, list[Activity]]:
        return _load_or_fetch(
            self.source,
            self.storage,
            account,
            self.since,
            self.until,
            self.ttl,
            use_cache=use_cache,
            covered_until=self.reach,
            gap_until=self.gaps.get(account.user_id),
        )


@dataclass
class _PrecheckSteps(_Steps):
    """Phase 1 of two-phase sampling: existence queries, no activity is cached.

    ``use_cache`` means the run chain already stored the account's stratum, so no
    query is sent. A fresh outcome is put in ``results`` for the caller's
    ``done_status``, which turns it into the account's design.
    """

    source: Source
    storage: Storage
    params: ScoringParams
    results: dict[str, Precheck]

    def prepare(self, account: Account, use_cache: bool) -> Any:
        if use_cache:
            return account, []
        if account.status in SKIPPED_STATUSES:
            account.status = STATUS_ACTIVE  # Skipped last time: decide again.
        return utcnow()

    def work(self, account: Account, token: Any) -> Any:
        if account.created_at is None and account.status not in UNAVAILABLE_STATUSES:
            self.source.hydrate(account)
        return precheck(self.source, account, self.params)

    def record(
        self, account: Account, token: Any, result: Any, error: BaseException | None
    ) -> tuple[Account, list[Activity]]:
        if error is None:
            self.results[account.user_id] = result
            return account, []
        if isinstance(error, AccountUnavailable):
            account.status = error.status
            self.storage.upsert_account(account, token)
            self.results[account.user_id] = Precheck(STRATUM_UNAVAILABLE)
            return account, []
        if isinstance(error, AccountSkipped):
            account.status = error.status
            account.meta.update(error.meta)
            return account, []
        raise error


#: Incomplete pre-checks (failed or narrowed existence queries) above this share of
#: the fresh pre-checks, once at least INCOMPLETE_PRECHECK_MIN accounts were
#: pre-checked, are warned about at once in the log and again in the summary. The scan
#: is not aborted: an incomplete pre-check never biases the estimates (the account is
#: a cliff candidate, fetched in full), it only costs, and an abort would not help a
#: resume, which reuses the stored strata unless --refresh is given.
INCOMPLETE_PRECHECK_SHARE = 0.5
INCOMPLETE_PRECHECK_MIN = 20


def _many_incomplete(incomplete: int, prechecked: int) -> bool:
    """Whether ``incomplete`` of ``prechecked`` fresh pre-checks is too many."""
    return prechecked >= INCOMPLETE_PRECHECK_MIN and incomplete > INCOMPLETE_PRECHECK_SHARE * (
        prechecked
    )


def _fetch_loop(
    source: Source,
    storage: Storage,
    run_id: int,
    settings: ScanSettings,
    items: list[tuple[Account, bool]],
    steps: _Steps,
    *,
    progress: Callable[[int, int], None] | None,
    processed: list[tuple[Account, list[Activity]]],
    errors: list[str],
    warnings: list[str],
    done_status: Callable[[Account, list[Activity]], str] = _done_status,
) -> bool:
    """Handle ``items`` (``(account, use_cache)``) with ``steps`` and record them.

    The sequential loop, or :class:`_ConcurrentFetch` with ``--concurrency`` above 1.
    Each recorded account is appended to ``processed`` and its sample status set to
    ``done_status(account, activities)``. Returns True when the consecutive error
    limit aborted the loop; fatal errors are raised after recording what finished.
    """
    if settings.concurrency > 1 and items:
        return _ConcurrentFetch(
            source,
            storage,
            run_id,
            settings,
            items,
            steps,
            progress=progress,
            processed=processed,
            errors=errors,
            warnings=warnings,
            done_status=done_status,
        ).run()
    total = len(items)
    consecutive = 0
    network_failures = 0
    for done, (account, use_cache) in enumerate(items, 1):
        if progress:
            progress(done, total)
        try:
            item = _retry_over_network_loss(source, partial(steps.load, account, use_cache))
        except FATAL_SOURCE_ERRORS as exc:
            if exc.partial is not None:
                # Pages already paid for are cached (truncated) by _load_or_fetch,
                # which hands back everything cached for the window; count the
                # account as fetched so this run's results include it.
                processed.append((account, list(exc.partial)))
                storage.set_sample_status(run_id, account, done_status(account, list(exc.partial)))
            raise
        except SourceError as exc:
            errors.append(f"{account.handle}: {exc}")
            account.status = STATUS_ERROR
            processed.append((account, []))
            storage.set_sample_status(run_id, account, SAMPLE_ERROR)
            if is_transport_failure(exc):
                # The network, not the account: not counted toward the error limit.
                network_failures += 1
                if network_failures >= MAX_NETWORK_FAILURES:
                    warnings.append(_network_down_warning(network_failures, exc, run_id))
                    return True
                continue
            network_failures = 0
            consecutive += 1
            if consecutive >= settings.max_consecutive_errors:
                warnings.append(
                    f"Aborted after {consecutive} consecutive account errors (last: {exc}). "
                    f"Results are partial; fix the cause and rerun with --from-run {run_id}."
                )
                return True
            continue
        consecutive = 0
        network_failures = 0
        processed.append(item)
        _flush_request_rows(source, storage, run_id, settings.platform)
        storage.set_sample_status(run_id, item[0], done_status(*item))
    return False


def _network_down_warning(count: int, last: BaseException, run_id: int) -> str:
    return (
        f"Stopped after {count} accounts in a row failed because the network did not answer "
        f"(last: {last}). Results are partial; when the network is back, rerun with "
        f"--from-run {run_id}."
    )


def _retry_over_network_loss[T](source: Source, attempt: Callable[[], T]) -> T:
    """Run ``attempt``; when its requests fail without any HTTP answer, wait and run it again.

    The HTTP client already retries a transport error a few times with a short
    backoff. That is not enough after the computer woke from sleep, when the network
    takes a while to return, so a :class:`TransportFailure` that got through is tried
    again after each of :data:`NETWORK_RETRY_PAUSES`. The pauses end at once when the
    scan is cancelled. Any other error, and the last transport failure, is raised.
    """
    for pause in (*NETWORK_RETRY_PAUSES, None):
        try:
            return attempt()
        except SourceError as exc:
            if pause is None or isinstance(exc, FATAL_SOURCE_ERRORS):
                raise
            if not is_transport_failure(exc):
                raise
            log.warning(
                "Requests are failing without an answer (is the network down, or was the "
                "computer asleep?). Waiting %.0f seconds and trying again.",
                pause,
            )
            source.http.sleep(pause)
    raise AssertionError("unreachable")  # pragma: no cover


def _phase_two(
    source: Source,
    storage: Storage,
    run_id: int,
    settings: ScanSettings,
    phase1: list[tuple[Account, list[Activity]]],
    designs: Mapping[str, Design],
    *,
    prefetched: set[str],
    errored: set[str],
    since: datetime,
    until: datetime,
    ttl: timedelta,
    reach: datetime,
    progress: Callable[[int, int], None] | None,
    processed2: list[tuple[Account, list[Activity]]],
    done_status: Callable[[Account, list[Activity]], str],
    errors: list[str],
    warnings: list[str],
) -> bool:
    """Phase 2 of two-phase sampling: the full baseline of the selected accounts.

    Every cliff candidate (``B``) first, in sample order, then the selected ``A`` and
    ``C`` accounts in ascending order of their draw, so a run that stops part-way
    leaves an exact Bernoulli sample at a lower rate (see
    :func:`bot_farm_silence_tracker.early_exit.effective_rate`). Each account is fetched for the
    whole window ``[since, until)`` through the cache, exactly as a census fetches it
    (a resume reuses what the chain fetched, and fills in a partial window's gap).
    Returns True when the consecutive error limit aborted the loop.
    """
    chosen = [
        (account, design)
        for account, _ in phase1
        if account.status == STATUS_ACTIVE
        and (design := designs.get(account.user_id)) is not None
        and design.selected
    ]
    if not chosen:
        return False
    candidates = [a for a, d in chosen if d.stratum not in SAMPLED_STRATA]
    sampled = sorted(
        ((a, d) for a, d in chosen if d.stratum in SAMPLED_STRATA),
        key=lambda pair: (pair[1].draw if pair[1].draw is not None else 1.0, pair[0].user_id),
    )
    accounts = [*candidates, *(a for a, _ in sampled)]
    reuse, gaps = _plan_reuse(
        storage, accounts, prefetched, settings, since, reach, ttl, errored=errored
    )
    log.info(
        "Phase 2: %d cliff candidates and %d sampled others of %d pre-checked accounts "
        "(%d already cached in full).",
        len(candidates),
        len(sampled),
        len(phase1),
        sum(reuse),
    )
    return _fetch_loop(
        source,
        storage,
        run_id,
        settings,
        list(zip(accounts, reuse, strict=True)),
        _CacheSteps(source, storage, since, until, ttl, reach, gaps),
        progress=progress,
        processed=processed2,
        done_status=done_status,
        errors=errors,
        warnings=warnings,
    )


def _merge_phases(
    phase1: list[tuple[Account, list[Activity]]], phase2: list[tuple[Account, list[Activity]]]
) -> list[tuple[Account, list[Activity]]]:
    """Phase-1 results in sample order, each replaced by its phase-2 result if any."""
    later = {account.user_id: (account, acts) for account, acts in phase2}
    return [later.get(account.user_id, (account, acts)) for account, acts in phase1]


def _phase2_state(account: Account, fetch_status: str | None) -> str:
    """The phase-2 state of a selected account from its stored sample status.

    ``fetched`` is complete unless the fetch was interrupted (``partial_reason``
    ``budget`` or ``error``), which is pending like a fetch never started. A failed
    or skipped fetch is failed (nonresponse).
    """
    if (
        fetch_status in (SAMPLE_ERROR, SAMPLE_SKIPPED)
        or account.status == STATUS_ERROR
        or account.status in SKIPPED_STATUSES
    ):
        return PHASE2_FAILED
    if fetch_status == SAMPLE_FETCHED:
        if partial_reason_of(account) in PARTIAL_INTERRUPTED:
            return PHASE2_PENDING
        return PHASE2_COMPLETE
    return PHASE2_PENDING


def _phase2_states(
    items: Iterable[tuple[Account, list[Activity]]],
    designs: Mapping[str, Design],
    statuses: Mapping[str, str],
) -> dict[str, str]:
    """``{user_id: phase-2 state}`` of every selected account in ``items``."""
    return {
        account.user_id: _phase2_state(account, statuses.get(account.user_id))
        for account, _ in items
        if (design := designs.get(account.user_id)) is not None and design.selected
    }


def _add_sampling(
    summary: dict[str, Any],
    results: list[AccountResult],
    rate: float,
    p_eff: float,
    params: ScoringParams,
) -> None:
    """Add the two-phase blocks to ``summary``: ``sampling`` and the explicit keys.

    ``eligible`` keeps no census meaning here (it is set to ``None``); the unweighted
    count among fully fetched accounts moves to ``eligible_observed``.
    """
    summary["eligible_observed"] = summary["eligible"]
    summary["sampling"] = sampling_summary(results, rate, p_eff, params)
    summary.update(summary_keys(summary["sampling"]))


def _drift_warning(results: Iterable[AccountResult], warnings: list[str]) -> None:
    """Warn about sampled ``A``/``C`` accounts whose phase-2 history disagreed with
    their pre-check (``moved_after_precheck``), unless the warning is already there.

    Built from the scored results, so ``report`` (:func:`rescore_run`) reproduces it.
    """
    results = list(results)
    moved = sorted(r.handle for r in results if r.moved_after_precheck)
    if not moved:
        return
    after_stop = sum(
        1 for r in results if r.moved_after_precheck and r.fetch_status == FETCH_AFTER_STOP
    )
    sampled = len(moved) - after_stop
    counts = []
    if sampled:
        counts.append(f"{sampled} sampled A/C accounts")
    if after_stop:
        counts.append(f"{after_stop} A/C accounts fetched past the stop point")
    text = (
        f"{' and '.join(counts)} showed a cliff-candidate pattern in their full "
        "phase-2 history that their pre-check did not see (the data changed after phase "
        f"1, for example {moved[0]}). The pre-check snapshot therefore no longer rules "
        "out cliffs among the unsampled accounts, so the cliff count is a Horvitz-Thompson "
        "estimate, not exact, whether or not these accounts are cliffs themselves; see the "
        "pre-check drift line for how many unsampled accounts may have changed. Rerun "
        "with --from-run --refresh to pre-check every account again."
    )
    if text not in warnings:
        warnings.append(text)


def _check_sampling(
    source: Source, settings: ScanSettings, previous: dict[str, Any] | None
) -> ScanSettings:
    """Validate ``baseline_sample_rate`` and resolve the sampling salt, before any work.

    A resume must use the rate of the run it continues (a different rate would change
    the inclusion probabilities of accounts already drawn), and takes that run's salt,
    so every account keeps its draw.
    """
    rate = settings.baseline_sample_rate
    stored_params = (previous or {}).get("params") or {}
    stored_rate = stored_params.get("baseline_sample_rate")
    if previous is not None and (
        (rate is None) != (stored_rate is None)
        or (rate is not None and stored_rate is not None and float(rate) != float(stored_rate))
    ):
        raise ValueError(
            f"baseline_sample_rate {rate!r} differs from the {stored_rate!r} of the run being "
            "resumed; a run chain keeps one rate, so its stored design stays valid"
        )
    if rate is None:
        return settings
    if not 0.0 < float(rate) <= 1.0:
        raise ValueError(f"baseline_sample_rate must be in (0, 1], got {rate!r}")
    if not type(source).supports_baseline_sampling:
        raise ValueError(
            f"baseline_sample_rate does not apply to the {source.name} source: it cannot "
            "fetch an older window without paying for the newer one again"
        )
    salt = stored_params.get("sampling_salt") if previous is not None else None
    if salt is None:
        salt = settings.sampling_salt
    if salt is None:
        salt = settings.seed if settings.seed is not None else secrets.randbits(63)
    return replace(settings, baseline_sample_rate=float(rate), sampling_salt=int(salt))


def _chain_strata(
    storage: Storage, from_run: int, root_run_id: int
) -> tuple[dict[str, str], dict[str, int]]:
    """The stratum each account was given by the latest run of the chain that pre-checked
    it (up to ``from_run``), so a resume keeps the design instead of asking again, and
    that run's id per account (``({user_id: stratum}, {user_id: run_id})``).

    An account has a stored stratum once its pre-check was recorded, whatever its
    phase-2 fetch did later (a failed fetch is retried; its pre-check is not). The
    stratum includes an account found unavailable in phase 2 (see
    :func:`bot_farm_silence_tracker.early_exit.reclassify`).
    """
    strata: dict[str, str] = {}
    runs: dict[str, int] = {}
    for rid in [r for r in storage.run_chain(root_run_id) if r <= from_run]:
        for user_id, (stratum, _, _, _) in storage.get_run_design(rid).items():
            strata[user_id] = stratum
            runs[user_id] = rid
    return strata, runs


def _warn_stale_precheck(
    storage: Storage, run_ids: Iterable[int], params: ScoringParams, warnings: list[str]
) -> None:
    """Warn when a resume reuses pre-checks older than ``max(cliff_window_hours, 24h)``.

    A resume without ``--refresh`` keeps the stratum a pre-check gave, however old. The
    older it is, the more data may have arrived since (late ingestion), which the
    stored stratum cannot reflect. ``run_ids`` are the runs whose pre-checks are
    reused; each pre-check is dated by the start of its run.
    """
    starts = []
    for rid in set(run_ids):
        run = storage.get_run(rid)
        if run is not None and run.get("started_at"):
            starts.append(ensure_utc(parse_datetime(str(run["started_at"]))))
    if not starts:
        return
    oldest = min(starts)
    limit = timedelta(hours=max(float(params.cliff_window_hours), 24.0))
    age = utcnow() - oldest
    if age > limit:
        warnings.append(
            f"This resume reuses pre-checks from as early as {isoformat(oldest)} "
            f"({age.total_seconds() / 3600:.0f} hours ago, more than "
            f"{limit.total_seconds() / 3600:g} hours). Data that arrived since is not in "
            "their strata, so the cliff count may miss accounts that changed. Rerun with "
            "--refresh to pre-check every account again."
        )


def _save_request_log(
    source: Source, storage: Storage, run_id: int, platform: str, summary: dict[str, Any]
) -> None:
    """Store the adapter's per-request rows and put its totals in ``summary["requests"]``.

    Instrumentation only: a database error is logged and never fails the scan.
    """
    try:
        stats = source.request_summary()
    except Exception as exc:  # Instrumentation must never break a paid-for scan.
        log.warning("Could not read the request totals for run %s: %s", run_id, exc)
        stats = None
    if stats is not None:
        summary["requests"] = stats
    _flush_request_rows(source, storage, run_id, platform, final=True)


def _flush_request_rows(
    source: Source, storage: Storage, run_id: int, platform: str, *, final: bool = False
) -> None:
    """Store the adapter's request rows recorded so far, so a long scan can be watched.

    Called after sampling, after every fetched account and (with ``final``) when the
    run ends. Errors are logged and never fail the scan.
    """
    try:
        rows = source.drain_request_log(final=final)
    except Exception as exc:
        log.warning("Could not read the request log for run %s: %s", run_id, exc)
        return
    try:
        storage.save_request_log(run_id, platform, rows)
    except sqlite3.Error as exc:
        storage.rollback()
        log.warning("Could not store the request log for run %s: %s", run_id, exc)


def format_request_summary(stats: dict[str, Any]) -> str:
    """One line with the request totals of a run (``summary["requests"]``)."""
    latency = stats.get("mean_latency_ms")
    rps = stats.get("observed_rps")
    elapsed = float(stats.get("elapsed_s") or 0.0)
    # Older summaries have no wall time; fall back to the monotonic elapsed time.
    wall = float(stats.get("wall_elapsed_s") or elapsed)
    floor = float(stats.get("time_at_floor_s") or 0.0)
    share = f" ({floor / elapsed:.0%})" if elapsed > 0 else ""
    paused = float(stats.get("pause_wait_s") or 0.0)
    # Only with --concurrency: waits for a pause that another worker's load signal set.
    pauses = f" Waits for a pause set by another worker: {paused:.0f} s." if paused else ""
    reads = int(stats.get("read_timeouts") or 0)
    # Requests that got no answer within the read timeout, handled as timeout 422s.
    slow_reads = f" Read timeouts (handled as timeout 422s): {reads}." if reads else ""
    if "status_422" not in stats:
        # An adapter without the archive's slow-query ladder (Bluesky): no 422 figures.
        return (
            f"Requests: {stats.get('requests', 0)}, 429/5xx load: "
            f"{stats.get('load_responses', 0)} (rate halved "
            f"{stats.get('rate_halvings', 0)} times, final "
            f"{float(stats.get('final_rps') or 0.0):g} rps), pauses for an exhausted "
            f"RateLimit-Remaining: {stats.get('reset_holds', 0)}, mean latency "
            f"{'n/a' if latency is None else f'{latency:.0f} ms'}, observed "
            f"{'n/a' if rps is None else f'{rps:.3f}'} rps over {wall:.0f} s wall clock."
        )
    return (
        f"Requests: {stats.get('requests', 0)}, HTTP 422: {stats.get('status_422', 0)} "
        f"({stats.get('status_422_timeout', 0)} timeout, of which "
        f"{stats.get('status_422_escalated', 0)} escalated to load; "
        f"{stats.get('status_422_load', 0)} load), 429/5xx load: "
        f"{stats.get('load_responses', 0)}, mean latency "
        f"{'n/a' if latency is None else f'{latency:.0f} ms'}, observed "
        f"{'n/a' if rps is None else f'{rps:.3f}'} rps over {wall:.0f} s wall clock "
        f"({elapsed:.0f} s monotonic), time at the rate floor {floor:.0f} s{share}, 422 "
        f"backoff waits {float(stats.get('backoff_wait_s') or 0.0):.0f} s, waits for an "
        f"exhausted X-RateLimit-Remaining {float(stats.get('reset_wait_s') or 0.0):.0f} s."
        f"{pauses}{slow_reads}"
    )


def _check_concurrency(source: Source, settings: ScanSettings) -> None:
    """Refuse a ``settings.concurrency`` the source cannot run, before any work.

    The source must allow it (``max_concurrency``), and must have been built for it:
    an adapter switches on its thread-safe behavior (the shared pause, per-worker
    state) from ``config.concurrency``, so running more workers than it was built
    for would share state that is only safe with one.
    """
    workers = settings.concurrency
    if workers < 1:
        raise ValueError(f"concurrency must be at least 1, got {workers}")
    allowed = type(source).max_concurrency
    if workers > allowed:
        raise ValueError(
            f"concurrency {workers} is above the {allowed} that the {source.name} source allows"
        )
    built = source.config.concurrency
    if workers != built:
        raise ValueError(
            f"concurrency {workers} does not match the {built} the {source.name} source "
            "was built for (SourceConfig.concurrency); build the source and the scan "
            "settings with the same value"
        )


def _stops_scan(error: BaseException) -> bool:
    """Whether a worker's error ends the scan: fatal source errors and bugs.

    :class:`FetchCancelled` is not one: it is the consequence of a stop, not a cause.
    """
    if isinstance(error, FetchCancelled):
        return False
    return isinstance(error, FATAL_SOURCE_ERRORS) or not isinstance(error, SourceError)


#: The concurrent loop plans and starts accounts at most this many times
#: ``--concurrency`` ahead of the oldest one not yet recorded. Results are recorded in
#: sample order, so one slow account holds back the recording, not the fetching.
CONCURRENT_LOOKAHEAD = 4
#: Seconds the concurrent loop waits for a worker at a time, so Ctrl-C is seen quickly.
_POLL_SECONDS = 0.5


@dataclass
class _Finished:
    """An account of the concurrent loop that is ready to be recorded."""

    #: The cached result, for an account that needed no fetch.
    cached: tuple[Account, list[Activity]] | None = None
    #: The token of the loop's ``prepare`` step (a :class:`_PreparedFetch` for a fetch).
    prepared: Any = None
    #: What the ``work`` step returned (the activities, for a fetch).
    activities: Any = None
    error: BaseException | None = None


class _ConcurrentFetch:
    """The fetch loop of :func:`run_scan` with ``--concurrency`` K > 1.

    Only the source calls (:func:`_source_fetch`) run in the K worker threads. The
    database is used only on the calling thread: accounts are planned there
    (:func:`_prepare_fetch`) in sample order, and their results are recorded there
    (:func:`_record_fetch`, the sample status and the request log) strictly in sample
    order, one account at a time as in the sequential loop. So the stored results,
    the consecutive error count and the output do not depend on which worker finishes
    first, and the request rate is still set by the source's one shared pacer.

    When the scan stops (a fatal source error, the consecutive error limit, Ctrl-C or
    a bug), the HTTP client is cancelled so every worker stops at its next wait or
    attempt, and the loop waits for them. A worker whose account fails with a fatal
    source error or a bug cancels the client itself, the moment it fails, and no
    further account is started, even when that account is not the oldest one still
    running: waiting for its turn in sample order would let the other workers keep
    sending. That first error is kept and raised once the workers stopped. A worker
    that was cut short by the cancel (:class:`FetchCancelled`) only marks the stop:
    its account is never recorded, and the loop then waits for every worker, so the
    error that caused the cancel is raised even when its worker finishes last.
    Accounts that finished are still recorded, in sample order; an account whose
    fetch was cut short, or that failed after the stop began, stays pending in the
    sample, so ``--from-run`` fetches it again.
    Pages a fatal error carries (``partial``) are cached and the account is counted
    as fetched, as in the sequential loop.
    """

    def __init__(
        self,
        source: Source,
        storage: Storage,
        run_id: int,
        settings: ScanSettings,
        items: list[tuple[Account, bool]],
        steps: _Steps,
        *,
        progress: Callable[[int, int], None] | None,
        processed: list[tuple[Account, list[Activity]]],
        errors: list[str],
        warnings: list[str],
        done_status: Callable[[Account, list[Activity]], str] = _done_status,
    ) -> None:
        self.done_status = done_status
        self.source = source
        self.storage = storage
        self.run_id = run_id
        self.settings = settings
        self.items = items
        self.steps = steps
        self.progress = progress
        self.processed = processed
        self.errors = errors
        self.warnings = warnings
        self.consecutive = 0
        self.network_failures = 0
        #: Accounts ready to be recorded, by sample index.
        self.finished: dict[int, _Finished] = {}
        #: Accounts being fetched: the worker's future and the sample index.
        self.running: dict[Future[Any], int] = {}
        self.prepared: dict[int, Any] = {}
        self.next_start = 0
        self.next_record = 0
        #: The first error of a worker that ends the scan, raised after the stop.
        self.stop_error: BaseException | None = None
        #: The first FetchCancelled a worker raised: some stop is under way. It is
        #: raised only when no worker's own error turns up once they all stopped.
        self.cancelled: FetchCancelled | None = None

    def run(self) -> bool:
        """Fetch and record every account. Returns True when the error limit aborted.

        Raises whatever stopped the scan (fatal source errors, Ctrl-C, bugs) after
        the workers stopped and the finished accounts were recorded.
        """
        workers = self.settings.concurrency
        window = workers * CONCURRENT_LOOKAHEAD
        total = len(self.items)
        executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="bot-farm-silence-tracker-fetch"
        )
        aborted = False
        stopping: BaseException | None = None
        try:
            while self.next_record < total and not self._stopping():
                while (
                    self.next_start < total
                    and not self._stopping()
                    and len(self.running) < workers
                    and self.next_start - self.next_record < window
                ):
                    self._start(executor, self.next_start, total)
                    self.next_start += 1
                aborted = self._record_ready()
                if aborted:
                    break
                if (
                    self.next_record >= total
                    or self.next_record in self.finished
                    or self.next_record >= self.next_start
                ):
                    # Done, ready to record, or not started yet: every account started
                    # so far came from the cache and was recorded, so no worker runs
                    # and the next pass starts more accounts.
                    continue
                # The oldest unrecorded account was started and is not finished yet.
                assert self.running, "concurrent fetch loop lost an account"
                done, _ = wait(
                    list(self.running), timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED
                )
                for future in done:
                    self._collect(future)
        except BaseException as exc:
            stopping = exc
        finally:
            self._stop(executor, cancel=stopping is not None or aborted or self._stopping())
        if stopping is not None:
            raise stopping
        if self.stop_error is not None:
            # The worker's own error, never the FetchCancelled it caused elsewhere.
            raise self.stop_error
        if self.cancelled is not None:
            # Cancelled, yet no worker reported why (the client was cancelled from
            # outside the loop): end the scan with the cancellation itself.
            raise self.cancelled
        return aborted

    def _stopping(self) -> bool:
        """Whether a worker's error or a cancellation began to stop the scan."""
        return self.stop_error is not None or self.cancelled is not None

    def _start(self, executor: ThreadPoolExecutor, index: int, total: int) -> None:
        """Plan account ``index`` and hand its fetch to a worker (or take the cache)."""
        account, use_cache = self.items[index]
        if self.progress:
            self.progress(index + 1, total)
        prepared = self.steps.prepare(account, use_cache)
        if isinstance(prepared, tuple):
            self.finished[index] = _Finished(cached=prepared)
            return
        self.prepared[index] = prepared
        future = executor.submit(self._work, account, prepared)
        self.running[future] = index

    def _work(self, account: Account, prepared: Any) -> Any:
        """Fetch one account in a worker; cancel the client at once if it ends the scan."""
        try:
            return _retry_over_network_loss(self.source, lambda: self.steps.work(account, prepared))
        except BaseException as exc:
            if _stops_scan(exc):
                # Stop the other workers now, not when this account's turn to be
                # recorded comes: they would keep sending until then.
                self.source.http.cancel()
            raise

    def _collect(self, future: Future[Any]) -> None:
        index = self.running.pop(future)
        prepared = self.prepared.pop(index)
        error = future.exception()
        if error is None:
            self.finished[index] = _Finished(prepared=prepared, activities=future.result())
            return
        if isinstance(error, FetchCancelled):
            # Cut short by a stop that some other worker (or Ctrl-C) began. Never
            # record it: the account stays pending, and recording it at the head of
            # the sample would raise the FetchCancelled before the real error, whose
            # worker may not have finished yet. The loop ends; _stop waits for every
            # worker and collects that error.
            if self.cancelled is None:
                self.cancelled = error
            return
        self.finished[index] = _Finished(prepared=prepared, error=error)
        if self.stop_error is None and _stops_scan(error):
            # Keep the first error that ends the scan; start no further account.
            self.stop_error = error
            self.source.http.cancel()

    def _record_ready(self) -> bool:
        """Record the finished accounts at the head of the sample, in sample order.

        Returns True when the consecutive error limit was reached. Raises the fatal
        error of an account (after caching its pages), like the sequential loop.
        """
        while self.next_record in self.finished:
            item = self.finished.pop(self.next_record)
            index = self.next_record
            self.next_record += 1
            if self._record(index, item):
                return True
        return False

    def _record(self, index: int, item: _Finished, *, leftover: bool = False) -> bool:
        """Record one account as the sequential loop does. True when the loop must abort.

        A ``leftover`` (recorded after the stop began) never counts toward the
        consecutive error limit; :meth:`_stop` passes only successes and fatal errors.
        """
        account = self.items[index][0]
        if item.cached is not None:
            result = item.cached
        else:
            try:
                result = self.steps.record(account, item.prepared, item.activities, item.error)
            except FATAL_SOURCE_ERRORS as exc:
                if exc.partial is not None:
                    # Pages already paid for are cached (truncated) by _record_fetch;
                    # count the account as fetched so this run's results include it.
                    self.processed.append((account, list(exc.partial)))
                    self.storage.set_sample_status(
                        self.run_id, account, self.done_status(account, list(exc.partial))
                    )
                raise
            except SourceError as exc:
                if leftover:
                    raise
                self.errors.append(f"{account.handle}: {exc}")
                account.status = STATUS_ERROR
                self.processed.append((account, []))
                self.storage.set_sample_status(self.run_id, account, SAMPLE_ERROR)
                if is_transport_failure(exc):
                    # The network, not the account: not counted toward the error limit.
                    self.network_failures += 1
                    if self.network_failures >= MAX_NETWORK_FAILURES:
                        self.warnings.append(
                            _network_down_warning(self.network_failures, exc, self.run_id)
                        )
                        return True
                    return False
                self.network_failures = 0
                self.consecutive += 1
                if self.consecutive >= self.settings.max_consecutive_errors:
                    self.warnings.append(
                        f"Aborted after {self.consecutive} consecutive account errors (last: "
                        f"{exc}). Results are partial; fix the cause and rerun with --from-run "
                        f"{self.run_id}."
                    )
                    return True
                return False
        self.consecutive = 0
        self.network_failures = 0
        self.processed.append(result)
        _flush_request_rows(self.source, self.storage, self.run_id, self.settings.platform)
        self.storage.set_sample_status(self.run_id, result[0], self.done_status(*result))
        return False

    def _stop(self, executor: ThreadPoolExecutor, *, cancel: bool) -> None:
        """Stop the workers, then record what finished (in sample order, gaps skipped).

        With ``cancel`` the HTTP client is cancelled first, so each worker stops at its
        next wait or attempt (a request already on the wire finishes). Successes and
        accounts found unavailable or skipped are recorded: those are results, not
        failures. An account whose fetch failed with any other error is not recorded:
        its error came after the scan began to stop (often the cancel itself), so it
        stays pending for a resume. A fatal error that carries pages still has them
        cached, as in the sequential loop.
        """
        http = self.source.http
        if cancel:
            http.cancel()
            sending = http.in_flight
            if sending:
                log.warning(
                    "stopping: waiting for %d in-flight request%s",
                    sending,
                    "" if sending == 1 else "s",
                )
        try:
            while self.running:
                done, _ = wait(list(self.running), timeout=_POLL_SECONDS)
                for future in done:
                    self._collect(future)
        finally:
            executor.shutdown(wait=True)
            if cancel:
                http.reset_cancel()
        for index in sorted(self.finished):
            item = self.finished.pop(index)
            error = item.error
            if (
                error is not None
                and not isinstance(error, (AccountUnavailable, AccountSkipped))
                and not (isinstance(error, FATAL_SOURCE_ERRORS) and error.partial is not None)
            ):
                continue
            with contextlib.suppress(*FATAL_SOURCE_ERRORS):
                # A fatal error's pages are cached and the account counted; the scan
                # is stopping already, so the error itself adds nothing.
                self._record(index, item, leftover=True)


def _chain_fetch_status(
    storage: Storage, from_run: int, root_run_id: int, warnings: list[str] | None = None
) -> tuple[set[str], set[str]]:
    """Return ``(prefetched, errored)``: how the resumed chain left each sampled account.

    ``prefetched`` holds the accounts fetched in full or in part; ``errored`` the
    accounts that failed or were skipped. Both keep their cached window on resume
    whatever its age (see :func:`_plan_reuse`): a partial one has only its gap
    fetched, so pages already paid for are not bought again. A skipped account was
    not fetched, so a complete cached window of its is still judged by freshness
    (runs made before SAMPLE_SKIPPED existed recorded skips as fetched; the
    snapshot's account status tells them apart).

    An account ``from_run`` never reached (still ``pending``, because the run
    stopped first) takes its status from the latest earlier run of the chain that
    reached it.
    """
    full_chain = storage.run_chain(root_run_id)
    newer = [rid for rid in full_chain if rid > from_run]
    if newer:
        text = (
            f"Run {from_run} is not the newest run of its chain; newer runs: "
            f"{', '.join(str(r) for r in newer)}. Resuming from run {from_run} anyway."
        )
        if warnings is not None:
            warnings.append(text)
    chain = [rid for rid in full_chain if rid < from_run]
    latest: dict[str, tuple[Account, str]] = {}
    for rid in [*chain, from_run]:
        for acct, fetch_status in storage.get_run_sample(rid):
            if fetch_status != SAMPLE_PENDING:
                latest[acct.user_id] = (acct, fetch_status)
    prefetched: set[str] = set()
    errored: set[str] = set()
    for user_id, (acct, fetch_status) in latest.items():
        if fetch_status == SAMPLE_SKIPPED or acct.status in SKIPPED_STATUSES:
            errored.add(user_id)
        elif fetch_status == SAMPLE_FETCHED:
            # A pre-checked account (two-phase sampling, ``prechecked``) was not
            # fetched: its phase-2 fetch is judged by the cache like any other.
            prefetched.add(user_id)
        elif fetch_status == SAMPLE_ERROR:
            errored.add(user_id)
    return prefetched, errored


def _retry_doubtful(
    exc: BaseException,
    storage: Storage,
    run_id: int,
    processed: list[tuple[Account, list[Activity]]],
    warnings: list[str],
) -> None:
    """Mark the accounts named by ``exc.retry_user_ids`` as ``error`` so a resume retries them."""
    retry = set(getattr(exc, "retry_user_ids", ()) or ())
    if not retry:
        return
    count = 0
    for index, (account, _) in enumerate(processed):
        if account.user_id not in retry:
            continue
        storage.forget_activity(account.platform, account.user_id, status=STATUS_ACTIVE)
        account.status = STATUS_ERROR
        storage.set_sample_status(run_id, account, SAMPLE_ERROR)
        processed[index] = (account, [])
        count += 1
    if count:
        warnings.append(
            f"{count} accounts recorded earlier in this run were marked 'error' because the "
            f"error that stopped the scan makes their result doubtful ({exc}); "
            f"--from-run {run_id} fetches them again."
        )


def resolve_data_horizon(
    source: Source, settings: ScanSettings, warnings: list[str] | None = None
) -> ScoringParams:
    """Ask ``source`` for its data horizon and return the scoring params to use.

    Returns ``settings.scoring`` unchanged when the source is live (``None``).
    Otherwise returns a copy with ``horizon`` set, so scoring uses
    ``min(now, horizon)``, and logs the lag at once. A horizon after ``now`` is
    warned about (a clock problem) and scoring uses ``now``. Appends user-facing
    warnings to ``warnings``.

    An unknown horizon (a :class:`SourceError` from ``data_horizon``) is handled
    by kind. :data:`FATAL_SOURCE_ERRORS` (auth, budget, rate limit) are re-raised.
    Any other error from a source that overrides ``data_horizon`` is treated like
    a stale source, since a lagging archive that cannot say how far it reaches
    may be far behind: :class:`StaleSourceError` without
    ``settings.allow_stale_source``, every result provisional with it (scored as of
    ``source.fallback_horizon()`` when the source has one, else as of ``now``). A source
    that does not override ``data_horizon`` is live, and ``now`` is used.

    Raises:
        StaleSourceError: the horizon is before ``cutoff + min_silence_days``, or
            unknown for a source that reports one, and
            ``settings.allow_stale_source`` is false. With it, the returned params
            force every result provisional.
        SourceAuthError, BudgetExceeded, RateLimited: raised by ``data_horizon``.
    """
    params = settings.scoring
    sink = warnings if warnings is not None else []
    try:
        horizon = source.data_horizon()
    except FATAL_SOURCE_ERRORS:
        raise
    except SourceError as exc:
        if type(source).data_horizon is Source.data_horizon:
            message = f"data horizon unknown ({exc}); scoring as of {isoformat(params.now)}."
            log.warning("%s", message)
            sink.append(message)
            return params
        problem = (
            f"{settings.platform} data horizon unknown ({exc}), so it is not known whether "
            "the data reaches far enough past the cutoff to judge silence."
        )
        if not settings.allow_stale_source:
            raise StaleSourceError(
                f"{problem} Nothing was sampled or fetched. Retry when the source answers, "
                "or pass --allow-stale-source to run anyway with every result provisional."
            ) from exc
        params = replace(params, force_provisional=True)
        fallback = source.fallback_horizon()
        if fallback is not None:
            params = replace(params, horizon=ensure_utc(fallback))
        message = f"{problem} Running anyway (--allow-stale-source): every result is provisional."
        if fallback is not None:
            message += (
                f" Scoring as of the source's last known data horizon, {isoformat(fallback)}."
            )
        log.warning("%s", message)
        sink.append(message)
        if fallback is not None:
            needed = ensure_utc(params.cutoff) + timedelta(days=params.min_silence_days)
            if ensure_utc(fallback) < needed:
                short = (
                    f"{settings.platform}'s last known data horizon, {isoformat(fallback)}, is "
                    f"before the cutoff plus --min-silence-days ({isoformat(needed)}), so "
                    "silence after the cutoff cannot be judged yet."
                )
                log.warning("%s", short)
                sink.append(short)
        return params
    if horizon is None:
        return params
    horizon = ensure_utc(horizon)
    if horizon > ensure_utc(params.now):
        message = (
            f"{settings.platform} reports a data horizon of {isoformat(horizon)}, after the "
            f"scan's reference time {isoformat(params.now)} (a clock or --now problem); "
            f"scoring as of {isoformat(params.now)}."
        )
        log.warning("%s", message)
        sink.append(message)
    params = replace(params, horizon=horizon)
    lag_hours = (ensure_utc(params.now) - horizon).total_seconds() / 3600
    if lag_hours > 0:
        log.warning(
            "%s data ends at %s, %.1f hours behind now; scoring as of %s.",
            settings.platform,
            isoformat(horizon),
            lag_hours,
            isoformat(params.effective_now),
        )
    needed = ensure_utc(params.cutoff) + timedelta(days=params.min_silence_days)
    if horizon < needed:
        problem = (
            f"{settings.platform} data ends at {isoformat(horizon)}, before the cutoff plus "
            f"--min-silence-days ({isoformat(needed)}), so silence after the cutoff cannot "
            "be judged yet."
        )
        if not settings.allow_stale_source:
            raise StaleSourceError(
                f"{problem} Nothing was sampled or fetched. Wait for the source to catch "
                "up, or pass --allow-stale-source to run anyway with every result "
                "provisional."
            )
        params = replace(params, force_provisional=True)
        message = f"{problem} Running anyway (--allow-stale-source): every result is provisional."
        log.warning("%s", message)
        sink.append(message)
    return params


def _plan_reuse(
    storage: Storage,
    accounts: list[Account],
    prefetched: set[str],
    settings: ScanSettings,
    since: datetime,
    until: datetime,
    ttl: timedelta,
    *,
    errored: set[str] | None = None,
) -> tuple[list[bool], dict[str, datetime]]:
    """Decide, per account, whether the cache is used instead of a fetch.

    Returns ``(reuse, gaps)``: ``reuse[i]`` is true when ``accounts[i]`` is read
    from the cache alone, and ``gaps`` maps the user id of each account whose
    cached window is partial (a fetch interrupted by the budget or an error, see
    :func:`_partial_start`) to where that window starts. Those accounts are fetched
    only for ``[since, gaps[user_id])`` and merged with the cache, so pages already
    paid for are not bought again. Every other account is fetched in full.

    ``until`` is where this run's data reaches (``effective_now``). An account the
    earlier run fetched (``prefetched``) or failed on (``errored``) keeps its cached
    window whatever its age: a complete one is reused (only for ``prefetched``; a
    failed account with a complete window is judged like any other account), a
    partial one has its gap fetched. The exception is a window cut short by the
    source's data horizon that ends more than ``ttl`` before ``until``: the source
    has caught up since, so the account is refetched in full. A window that simply
    ends at the earlier run's reference time is still kept (a resume never pays
    twice for it), and :func:`_stale_reused` warns about it.

    Other accounts keep a cached window only when it is fresh: fetched within
    ``ttl``, and ending no more than ``ttl`` before ``until``.

    A window cut by the platform's history cap counts as complete: refetching
    cannot return more. A window that starts at or after ``until`` covers nothing
    of this run and is fetched in full.
    """
    gaps: dict[str, datetime] = {}
    if settings.refresh:
        return [False] * len(accounts), gaps
    earlier = prefetched | (errored or set())
    now = utcnow()
    plan = []
    for account in accounts:
        window = storage.activity_window(account.platform, account.user_id)
        if window is None or window[0] >= ensure_utc(until):
            # No window, or one that starts at or after this run's end (cached for a
            # later reference time): nothing in it covers this run, so fetch in full.
            plan.append(False)
            continue
        gap = _partial_start(storage, account, since, window)
        if account.user_id in earlier and not _horizon_cut(storage, account, until, ttl):
            if gap is not None:
                gaps[account.user_id] = gap
                plan.append(False)
                continue
            if account.user_id in prefetched:
                plan.append(True)
                continue
        fresh = ensure_utc(now) - window[2] <= ttl and ensure_utc(until) - window[1] <= ttl
        if fresh and gap is not None:
            gaps[account.user_id] = gap
            plan.append(False)
        else:
            plan.append(fresh)
    return plan, gaps


def _partial_info(storage: Storage, account: Account) -> dict[str, Any]:
    """Return the cached account's ``meta["partial_fetch"]`` (empty when it has none)."""
    cached = storage.get_account(account.platform, account.user_id)
    info = cached.meta.get(PARTIAL_FETCH_KEY) if cached is not None else None
    return dict(info) if isinstance(info, dict) else {}


def _partial_start(
    storage: Storage,
    account: Account,
    since: datetime,
    window: tuple[datetime, datetime, datetime],
) -> datetime | None:
    """Where a partial cached window starts, when the part below it can still be fetched.

    A window is partial when it starts after ``since`` or its account carries
    ``meta["partial_fetch"]`` for an interrupted fetch (budget or error). ``None``
    when the window reaches ``since``, or when the platform's history cap cut it
    (``reason`` ``platform_cap``): nothing older can be fetched then.
    """
    reason = _partial_info(storage, account).get("reason")
    if reason == PARTIAL_PLATFORM_CAP:
        return None
    start = window[0]
    if start > ensure_utc(since):
        return start
    return None


def _horizon_cut(storage: Storage, account: Account, until: datetime, ttl: timedelta) -> bool:
    """Whether the cached window stopped at a data horizon that ``until`` is past by > ``ttl``."""
    window = storage.activity_window(account.platform, account.user_id)
    requested = storage.activity_requested_until(account.platform, account.user_id)
    if window is None or requested is None:
        return False
    end = window[1]
    return end < requested and ensure_utc(until) - end > ttl


def _stale_reused(
    storage: Storage,
    accounts: list[Account],
    reuse: list[bool],
    until: datetime,
    ttl: timedelta,
) -> list[datetime]:
    """Return the cached window ends of reused accounts that stop well before ``until``."""
    out = []
    for account, use_cache in zip(accounts, reuse, strict=True):
        if not use_cache:
            continue
        window = storage.activity_window(account.platform, account.user_id)
        if window is not None and until - window[1] > ttl:
            out.append(window[1])
    return out


#: A gap fetch reaches this far into the cached partial window, so posts that share
#: the oldest fetched second but were on a page that was never fetched are not lost.
GAP_OVERLAP = timedelta(seconds=1)


def _load_or_fetch(
    source: Source,
    storage: Storage,
    account: Account,
    since: datetime,
    until: datetime,
    ttl: timedelta,
    *,
    use_cache: bool | None = None,
    covered_until: datetime | None = None,
    gap_until: datetime | None = None,
) -> tuple[Account, list[Activity]]:
    """Return the account and its activities, from the cache or the source.

    ``use_cache`` is the caller's decision; ``None`` uses the cache when it is fresh.
    ``covered_until`` (the data horizon, when earlier than ``until``) is stored as
    the cached window end, so ``min(until, horizon)`` is what the cache claims.

    ``gap_until`` is the start of a partial cached window (see :func:`_plan_reuse`):
    only ``[since, gap_until + GAP_OVERLAP)`` is fetched, merged below the cached
    window, and everything cached for ``[since, until)`` is returned.

    Coverage is stored honestly. Timelines come newest first, so a fetch that stops
    part-way covers ``[oldest fetched, until)``: the cached window starts at the
    oldest fetched post, and ``meta["partial_fetch"]`` records why (``budget`` or
    ``error``; ``platform_cap`` when an adapter reports that the platform stopped
    returning history). The pages already paid for are cached with
    ``meta["truncated"]`` and the error is re-raised with ``partial`` set to
    everything now cached for the window. A later run fetches only the part below
    an interrupted window. Errors without ``partial`` cache nothing.

    A skip (:class:`AccountSkipped`) caches no activity. When an earlier run cached
    activity for the account, that activity and the shared account row are kept, and
    the skip is recorded only in this run's sample (by the caller).

    It runs in three steps so the concurrent fetch loop can run the middle one in a
    worker thread: :func:`_prepare_fetch` and :func:`_record_fetch` use the database
    and run on the thread that owns it, :func:`_source_fetch` only calls the source.
    """
    prepared = _prepare_fetch(
        storage, account, since, until, ttl, use_cache=use_cache, gap_until=gap_until
    )
    if not isinstance(prepared, _PreparedFetch):
        return prepared
    try:
        activities = _source_fetch(source, account, since, prepared.fetch_until)
    except SourceError as exc:
        return _record_fetch(
            storage, account, since, until, prepared, error=exc, covered_until=covered_until
        )
    return _record_fetch(
        storage, account, since, until, prepared, activities=activities, covered_until=covered_until
    )


@dataclass(frozen=True)
class _PreparedFetch:
    """An account :func:`_prepare_fetch` set up for a source fetch."""

    #: Where the fetch ends: ``until``, or just above a partial cached window.
    fetch_until: datetime
    #: The time the cached rows are stamped with.
    now: datetime
    #: The start of the partial cached window being filled in, or None.
    gap_until: datetime | None


def _prepare_fetch(
    storage: Storage,
    account: Account,
    since: datetime,
    until: datetime,
    ttl: timedelta,
    *,
    use_cache: bool | None,
    gap_until: datetime | None,
) -> tuple[Account, list[Activity]] | _PreparedFetch:
    """Return the cached result, or set the account up for a fetch (database thread)."""
    now = utcnow()
    if use_cache is None:
        use_cache = storage.activity_cache_fresh(
            account.platform, account.user_id, since, now, ttl, until=until
        )
    if use_cache:
        cached = storage.get_account(account.platform, account.user_id)
        if cached is not None:
            window = storage.activity_window(account.platform, account.user_id)
            if window is not None and ensure_utc(window[0]) <= ensure_utc(since):
                # The partial window of a longer baseline still covers this run's: its
                # truncation is not this run's (the stored row keeps it).
                if cached.meta.get("truncated"):
                    cached.meta["truncated"] = False
                cached.meta.pop(PARTIAL_FETCH_KEY, None)
            return cached, _window_activities(storage, account, since, until)

    fetch_until = until
    if gap_until is not None:
        fetch_until = min(ensure_utc(until), ensure_utc(gap_until) + GAP_OVERLAP)
        account.meta[PARTIAL_FETCH_KEY] = _gap_info(storage, account, fetch_until, until)
    else:
        account.meta.pop(PARTIAL_FETCH_KEY, None)

    if account.status in SKIPPED_STATUSES:
        account.status = STATUS_ACTIVE  # Skipped last time: decide again.
    return _PreparedFetch(fetch_until=fetch_until, now=now, gap_until=gap_until)


def _source_fetch(
    source: Source, account: Account, since: datetime, fetch_until: datetime
) -> list[Activity]:
    """Hydrate the account if needed and fetch its activity. Touches no database."""
    activities: list[Activity] = []
    if account.created_at is None and account.status not in UNAVAILABLE_STATUSES:
        source.hydrate(account)
    if account.status not in UNAVAILABLE_STATUSES:
        activities = source.fetch_activity(account, since, fetch_until)
    return activities


def _record_fetch(
    storage: Storage,
    account: Account,
    since: datetime,
    until: datetime,
    prepared: _PreparedFetch,
    *,
    activities: list[Activity] | None = None,
    error: BaseException | None = None,
    covered_until: datetime | None = None,
) -> tuple[Account, list[Activity]]:
    """Store what :func:`_source_fetch` returned or raised (database thread).

    ``error`` is the exception the fetch raised. An unavailable or skipped account
    is recorded and returned; any other error caches the pages already fetched (see
    :func:`_load_or_fetch`) and is raised again.
    """
    now = prepared.now
    fetch_until = prepared.fetch_until
    gap_until = prepared.gap_until
    if error is not None and not isinstance(error, SourceError):
        raise error
    if isinstance(error, AccountUnavailable):
        account.status = error.status
        activities = []
        if account.status == STATUS_OPTED_OUT:
            # Stored by ID only and with no activity window: an empty window would
            # make the account read as one with no posts if it removes the label.
            storage.upsert_account(account, now)
            return account, []
        if gap_until is not None:
            # Keep the paid partial window; the status is the signal.
            account.meta.pop(PARTIAL_FETCH_KEY, None)
            if account.meta.get("truncated"):
                account.meta["truncated"] = False
            storage.upsert_account(account, now)
            return account, []
    elif isinstance(error, AccountSkipped):
        # Not fetched: cache no activity, so the account never reads as one with no
        # posts and a later run (or a lower threshold) fetches it.
        account.status = error.status
        account.meta.update(error.meta)
        if storage.activity_window(account.platform, account.user_id) is None:
            account.meta.pop(PARTIAL_FETCH_KEY, None)
            storage.upsert_account(account, now)
        return account, []
    elif error is not None:
        exc = error
        if exc.partial is not None:
            # Pages already paid for: cache them with honest coverage and flag them
            # truncated, so a rerun fetches only the part below them.
            reason = PARTIAL_BUDGET if isinstance(exc, BudgetExceeded) else PARTIAL_ERROR
            covered = _covered_since(account, exc.partial, fetch_until)
            if gap_until is not None:
                covered = min(covered, ensure_utc(gap_until))
            posts = account.meta.get(PARTIAL_FETCH_KEY, {}).get("posts", len(exc.partial))
            # Rows an earlier fetch cached right below the new pages are still valid:
            # keep them in the window instead of buying them again (non-gap path).
            below = None if gap_until is not None else _contiguous_start(storage, account, covered)
            account.meta["truncated"] = True
            account.meta[PARTIAL_FETCH_KEY] = {
                "reason": reason,
                "since": isoformat(covered),
                "posts": posts,
            }
            if below is not None and below <= ensure_utc(since):
                account.meta["truncated"] = False  # The kept rows reach ``since``.
                account.meta.pop(PARTIAL_FETCH_KEY)
            elif below is not None:
                account.meta[PARTIAL_FETCH_KEY]["since"] = isoformat(below)
            storage.upsert_account(account, now)
            if gap_until is not None:
                storage.merge_gap_activities(account, exc.partial, covered, fetch_until, now)
            elif below is not None:
                # The older complete rows own the oldest fetched second: the new pages
                # may hold only some of the posts at it. Save the new rows from the
                # next second up, then move the window start down to the kept rows
                # (deleting and inserting nothing).
                # Clamped to ``until``: a stop at ``until`` itself would otherwise save
                # a window that starts after it ends.
                keep = min(covered + GAP_OVERLAP, ensure_utc(until))
                fresh = [a for a in exc.partial if ensure_utc(a.ts) >= keep]
                storage.save_activities(
                    account, fresh, keep, until, now, covered_until=covered_until
                )
                storage.merge_gap_activities(account, [], below, below, now)
            else:
                storage.save_activities(
                    account, exc.partial, covered, until, now, covered_until=covered_until
                )
            exc.partial = _window_activities(storage, account, since, until)
        raise exc
    activities = activities or []
    info = account.meta.get(PARTIAL_FETCH_KEY)
    capped = isinstance(info, dict) and info.get("reason") == PARTIAL_PLATFORM_CAP
    start = since
    if capped:
        start = _covered_since(account, activities, fetch_until)
    else:
        account.meta.pop(PARTIAL_FETCH_KEY, None)
    storage.upsert_account(account, now)
    if gap_until is not None:
        start = min(ensure_utc(start), ensure_utc(gap_until)) if capped else since
        storage.merge_gap_activities(account, activities, start, fetch_until, now)
        return account, _window_activities(storage, account, since, until)
    storage.save_activities(account, activities, start, until, now, covered_until=covered_until)
    return account, activities


def _window_activities(
    storage: Storage, account: Account, since: datetime, until: datetime
) -> list[Activity]:
    """Return the cached activities in ``[max(since, window start), until)``.

    Rows below the cached window's start (left by another fetch) are not part of
    it, and ``report`` reads from the window start too, so the scan must not
    score them either.
    """
    start = ensure_utc(since)
    window = storage.activity_window(account.platform, account.user_id)
    if window is not None:
        start = max(start, ensure_utc(window[0]))
    return storage.get_activities(account.platform, account.user_id, start, until)


def _gap_info(
    storage: Storage, account: Account, fetch_until: datetime, until: datetime
) -> dict[str, Any]:
    """The ``meta["partial_fetch"]`` handed to the adapter for a gap fetch.

    A platform history cap (an X timeline stops after about the newest 3,200 posts)
    counts back from the newest post, so the adapter needs to know how many posts
    are newer than the gap whether or not the earlier fetch stopped part-way:

    * ``posts``: the cached posts in ``[fetch_until, until)`` (at least what the
      interrupted fetch recorded);
    * ``cached_until``: where the cached window ends, since posts after it count
      toward the cap too;
    * ``cached_tweet_count``: the lifetime post count stored with the cached
      account, when known, so the adapter can estimate those newer posts from the
      change in the count;
    * ``cached_fetched_at``: when the cached window was fetched. A ``tweet_count``
      seen before then says nothing about the posts newer than the window;
    * ``cached_rate`` (posts per day in the cached window) and ``until`` (this
      run's end), so the adapter can estimate those newer posts from the posting
      rate when the count cannot tell.
    """
    info = _partial_info(storage, account)
    cached = len(storage.get_activities(account.platform, account.user_id, fetch_until, until))
    info["posts"] = max(int(info.get("posts") or 0), cached)
    info["until"] = isoformat(until)
    window = storage.activity_window(account.platform, account.user_id)
    if window is not None:
        start, end, fetched_at = window
        info["cached_until"] = isoformat(end)
        info["cached_fetched_at"] = isoformat(fetched_at)
        days = (ensure_utc(end) - ensure_utc(start)).total_seconds() / 86400
        if days > 0:
            rows = len(storage.get_activities(account.platform, account.user_id, start, end))
            info["cached_rate"] = rows / days
    stored = storage.get_account(account.platform, account.user_id)
    count = stored.meta.get("tweet_count") if stored is not None else None
    if isinstance(count, int):
        info["cached_tweet_count"] = count
    return info


def _contiguous_start(storage: Storage, account: Account, covered: datetime) -> datetime | None:
    """Where the cached window starts, when it reaches below ``covered`` without a hole.

    For a full fetch that stopped part-way at ``covered``: the cached rows in
    ``[window start, covered)`` are still valid, so the stored window can start
    there. Strictly inside the window: a stop at the window end is a plain partial
    fetch (posts tied at that second may be split across the stop, and the resume
    fetches the boundary second).

    That has a cost. When ``covered`` equals the window end, the new pages hold only
    part of that second and the old window (half-open, ending there) holds none of
    it, so the old rows cannot be joined to the new ones without a hole. The stored
    window then starts at ``covered``, and the resume's gap fetch runs from ``since``
    to ``covered + GAP_OVERLAP``: it buys the whole old window again. Avoiding that
    would need a fetch of the boundary second alone, which the gap machinery (one
    gap, always reaching down to ``since``) cannot express; the spend is accepted
    because a stop at exactly that second is rare.

    ``None`` when there is no such window, or when the platform's history cap cut
    it (its rows are kept, but the reason must not be lost).
    """
    window = storage.activity_window(account.platform, account.user_id)
    if window is None:
        return None
    start, end = ensure_utc(window[0]), ensure_utc(window[1])
    if not start < ensure_utc(covered) < end:
        return None
    if _partial_info(storage, account).get("reason") == PARTIAL_PLATFORM_CAP:
        return None
    return start


def _covered_since(account: Account, activities: Iterable[Activity], until: datetime) -> datetime:
    """Where a partial fetch's data starts: ``meta["partial_fetch"]["since"]``.

    Falls back to the oldest activity, or ``until`` (nothing covered) when there is
    none. Adapters should set ``since`` to the oldest item returned, which can be
    older than the oldest kept activity.
    """
    info = account.meta.get(PARTIAL_FETCH_KEY)
    text = info.get("since") if isinstance(info, dict) else None
    if text:
        return ensure_utc(parse_datetime(str(text)))
    stamps = [ensure_utc(a.ts) for a in activities]
    return min(stamps) if stamps else ensure_utc(until)


def _window_ends(
    storage: Storage, processed: Iterable[tuple[Account, list[Activity]]]
) -> dict[str, datetime]:
    """Return ``{user_id: cached window end}`` for the processed accounts that have one.

    Scoring measures each account's silence to ``min(effective_now, window_end)``
    (see :func:`bot_farm_silence_tracker.scoring.score_account`), so an account whose data ends
    early (a window reused from an earlier run, a cache hit within the TTL, or an
    adapter's partial fetch) is never scored as silent past its data.
    """
    ends: dict[str, datetime] = {}
    for account, _ in processed:
        # A failed or skipped account was not scored on a window of this run's, even
        # when an earlier run cached one for it.
        if account.status == STATUS_ERROR or account.status in SKIPPED_STATUSES:
            continue
        window = storage.activity_window(account.platform, account.user_id)
        if window is not None:
            ends[account.user_id] = window[1]
    return ends


def _warn_early_data_end(
    results: list[AccountResult], params: ScoringParams, warnings: list[str]
) -> None:
    """Warn when some accounts were scored against data that ends before effective_now."""
    early = [r.data_end for r in results if r.data_end is not None]
    if not early:
        return
    warnings.append(
        f"{len(early)} accounts were scored against data that ends before this run's "
        f"reference time {isoformat(params.effective_now)} (earliest {isoformat(min(early))}); "
        "their silence is measured only to their own data end (the data_end column), and "
        "cliffs among them are provisional until that silence reaches --min-silence-days. "
        "Rerun with --refresh to refetch them."
    )


def _housekeep_spend(storage: Storage) -> None:
    """At run start: merge the spend fallback file and reclaim stale reservations.

    See :meth:`Storage.reconcile_fallback` and
    :meth:`Storage.reclaim_stale_reservations`. Failures are logged, never raised:
    the ledger still counts the fallback file and the reserved rows either way.
    """
    try:
        storage.reconcile_fallback()
    except (sqlite3.Error, OSError) as exc:
        log.warning("Could not merge the spend fallback file into the database: %s", exc)
    try:
        reclaimed = storage.reclaim_stale_reservations()
    except sqlite3.Error as exc:
        log.warning("Could not reclaim stale spend reservations: %s", exc)
        return
    if reclaimed:
        log.info(
            "Closed %d spend reservations left open by finished runs, at their reserved "
            "(worst-case) amount.",
            reclaimed,
        )


def rescore_run(
    storage: Storage,
    run_id: int,
    *,
    fetch_command: Callable[[int], str] | None = None,
    **overrides: Any,
) -> ScanOutcome:
    """Re-score a stored run from the cache without touching the network.

    ``overrides`` may replace ``min_silence_days``, ``min_active_days``,
    ``min_posts``, ``recent_days``, ``max_weekly_cv``, ``cliff_window_hours`` and
    ``sync_minutes``. The cutoff, baseline length and ``now`` stay as recorded,
    because the cached activity covers exactly that window. Each account's activity
    is read only for the window the run scored: from the run's fetch start (the
    baseline start; or, for a history the run saw partial, where its data started) to
    the window end stored with the run. The placebo samples are not cached, so their
    stored counts are reused when the parameters are unchanged; with changed
    parameters the placebo comparison is unavailable. Runs made before
    window ends were stored are re-scored on the cache as it is now, which may hold
    rows merged in by later runs.

    A two-phase run (``--baseline-sample-rate``) is re-scored with its stored design:
    each account keeps its stratum, selection and phase-2 status (so ``P_eff`` is the
    same), and the estimates are recomputed. Its ``cliff_window_hours`` cannot be
    raised, because the pre-check only ruled accounts out for the window it was run
    with.

    ``fetch_command(min_posts)`` returns the shell command that continues the run
    with a new ``--min-posts`` (the CLI builds it with
    :func:`bot_farm_silence_tracker.cli.resume_command`, so it carries the platform, database and
    options). It is quoted when a lower ``min_posts`` cannot score accounts the run
    skipped without fetching.
    """
    run = storage.get_run(run_id)
    if run is None:
        raise LookupError(f"no run with id {run_id}")
    allowed = {
        "min_silence_days",
        "min_active_days",
        "min_posts",
        "recent_days",
        "max_weekly_cv",
        "cliff_window_hours",
        "sync_minutes",
    }
    bad = set(overrides) - allowed
    if bad:
        raise ValueError(f"cannot override: {', '.join(sorted(bad))}")
    stored_params = ScoringParams.from_dict(run["params"]["scoring"])
    params = replace(stored_params, **{k: v for k, v in overrides.items() if v is not None})
    rate = run["params"].get("baseline_sample_rate")
    design_rows = storage.get_run_design(run_id) if rate is not None else {}
    if rate is not None and params.cliff_window_hours > stored_params.cliff_window_hours:
        raise ValueError(
            f"run {run_id} used two-phase sampling with --cliff-window-hours "
            f"{stored_params.cliff_window_hours:g}; its pre-check cannot tell which unfetched "
            "accounts a wider window would make cliffs, so it cannot be raised"
        )

    snapshots = {acct.user_id: (acct, status) for acct, status in storage.get_run_sample(run_id)}
    errored = {uid: acct for uid, (acct, status) in snapshots.items() if status == SAMPLE_ERROR}
    # Skips are recorded in the run's sample only; the cache may hold another run's data.
    skipped = {uid: acct for uid, (acct, _) in snapshots.items() if acct.status in SKIPPED_STATUSES}
    # Truncation as this run saw it; the accounts table holds the latest fetch's.
    truncated = storage.get_run_truncated(run_id)
    # Where each account's data ended when the run scored it. Runs made before these
    # were stored fall back to the cached window end.
    window_ends = storage.get_run_window_ends(run_id)
    items: list[tuple[Account, list[Activity]]] = []
    missing = 0
    for user_id in run["account_ids"]:
        if user_id in errored:
            failed = errored[user_id]
            failed.status = STATUS_ERROR
            items.append((failed, []))
            continue
        if user_id in skipped:
            items.append((skipped[user_id], []))
            continue
        account = storage.get_account(run["platform"], user_id)
        if account is None and user_id in design_rows and user_id in snapshots:
            # Two-phase: an account that was only pre-checked has no cached row.
            items.append((snapshots[user_id][0], []))
            continue
        if account is None:
            missing += 1
            continue
        if user_id in truncated:
            account.meta = {**account.meta, "truncated": truncated[user_id]}
        start = ensure_utc(params.fetch_start)
        if user_id in snapshots:
            # Why the history was partial when this run fetched it.
            seen = snapshots[user_id][0].meta.get(PARTIAL_FETCH_KEY)
            account.meta = {k: v for k, v in account.meta.items() if k != PARTIAL_FETCH_KEY}
            if seen is not None:
                account.meta[PARTIAL_FETCH_KEY] = seen
                if isinstance(seen, dict) and seen.get("since"):
                    start = max(start, ensure_utc(parse_datetime(str(seen["since"]))))
        end = ensure_utc(params.now)
        if user_id in window_ends:
            # Only the window this run scored: later runs may have merged newer or
            # older rows into the shared cache.
            end = min(end, ensure_utc(window_ends[user_id]))
        else:
            window = storage.activity_window(run["platform"], user_id)
            if window is not None:
                window_ends[user_id] = window[1]
        items.append((account, storage.get_activities(run["platform"], user_id, start, end)))
    p_eff = 0.0
    if rate is not None:
        salt = int(run["params"].get("sampling_salt") or 0)
        designs = {
            uid: Design(
                stratum,
                prob,
                phase2,
                selection_draw(salt, run["platform"], uid) if stratum in SAMPLED_STRATA else None,
            )
            for uid, (stratum, prob, phase2, _) in design_rows.items()
        }
        statuses = {uid: row[3] for uid, row in design_rows.items()}
        states = _phase2_states(items, designs, statuses)
        results, p_eff = score_two_phase(
            items,
            designs,
            states,
            float(rate),
            params,
            window_ends=window_ends,
            precheck_params=stored_params,
        )
    else:
        results = score_accounts(items, params, window_ends=window_ends)
    groups = discovery_groups(acct for acct, _ in snapshots.values())
    runs, reason, requests = stored_placebo_runs(run["summary"].get("placebo"))
    if runs is not None and params != stored_params:
        runs, reason = (
            None,
            (
                "the placebo samples are not stored with the run, so they cannot be re-scored "
                "with changed parameters; report without the overrides shows the scan's "
                "comparison"
            ),
        )
    draw = PlaceboDraw(runs=runs, unavailable=reason, requests=requests)
    context, placebo = analyze(
        results,
        params,
        items,
        groups=groups,
        window_ends=window_ends,
        covered_since=_coverage(storage, items),
        two_phase=rate is not None,
        draw=draw,
    )
    sampled = run["summary"].get("sampled", len(run["account_ids"]))
    extra_columns = tuple(run["params"].get("extra_columns") or ())
    reports_opted_out = bool(run["params"].get("reports_opted_out"))
    summary = summarize(
        results,
        params,
        sampled=sampled,
        extra_columns=extra_columns,
        reports_opted_out=reports_opted_out,
    )
    add_analysis(summary, context, placebo)
    dropped_opted_out = run["summary"].get("dropped_opted_out")
    if reports_opted_out or dropped_opted_out:
        # Discovery is not repeated; the scan's count of dropped accounts stands.
        summary["dropped_opted_out"] = int(dropped_opted_out or 0)
    warnings = list(run["summary"].get("warnings") or [])
    if rate is not None:
        _add_sampling(summary, results, float(rate), p_eff, stored_params)
        _drift_warning(results, warnings)
    if not any("scored against data that ends before" in w for w in warnings):
        _warn_early_data_end(results, params, warnings)
    if missing:
        warnings.append(f"{missing} accounts from this run are no longer in the cache.")
    new_min = overrides.get("min_posts")
    if new_min is not None:
        unfetched = sum(
            1
            for account, _ in items
            if account.status == STATUS_SKIPPED
            and isinstance(account.meta.get("skip_min_posts"), int)
            and new_min < account.meta["skip_min_posts"]
        )
        if unfetched:
            how = (
                f"Fetch them with: {fetch_command(new_min)}"
                if fetch_command is not None
                else f"Fetch them with scan --from-run {run_id} --min-posts {new_min} --force."
            )
            warnings.append(
                f"{unfetched} accounts were skipped without fetching their activity because "
                "their lifetime post count was below --min-posts when the run was scanned; "
                f"--min-posts {new_min} cannot score them. {how}"
            )
    return ScanOutcome(
        run_id=run_id,
        status=run["status"],
        results=results,
        summary=summary,
        params=params,
        warnings=warnings,
        extra_columns=extra_columns,
        signal_context=context,
        placebo=placebo,
        provenance=provenance_of(run["params"]),
    )
