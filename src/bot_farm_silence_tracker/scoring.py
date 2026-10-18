"""Per-account metrics, the cliff test, the composite score and cluster detection.

Definitions
-----------
* Baseline window: ``[baseline_end - baseline_days, baseline_end)``. Without markers
  ``baseline_end`` is the cutoff.
* Eligible ("steady"): active on at least ``min_active_days`` distinct UTC days of
  the baseline window, at least ``min_posts`` activities in it, at least one activity
  in its last ``recent_days`` (the days just before the baseline end, which is the
  onset for an event with markers), and weekly counts whose coefficient of variation
  is at most ``max_weekly_cv`` (see :func:`weekly_cv`). The last two rules keep out an
  account that posted a burst early in the baseline and had already gone quiet.
* Cliff: eligible, zero activity at or after the cutoff, last activity in the silence
  window ``[window_start, cutoff)``. Without markers ``window_start`` is
  ``cutoff - cliff_window_hours``.

Markers
-------
An outage event can carry markers, earlier moments inside the same outage such as
strikes before the main collapse (see :mod:`bot_farm_silence_tracker.events`). With
``onset = earliest marker - marker_margin_hours``:

* ``baseline_end = min(cutoff, onset)``: the baseline ends before the earliest
  marker, so a decline caused by an earlier strike does not pollute it;
* ``window_start = min(cutoff - cliff_window_hours, onset)``: a silence that began at
  any marker is a cliff, not only one in the ``cliff_window_hours`` before the cutoff.

Each cliff account is labelled with the marker, or the main outage (``cutoff``), that
its last activity is nearest to (:func:`nearest_marker`), and each stop-time cluster
with the one nearest to its first stop. With markers, ``abruptness`` measures the
distance to that nearest moment instead of to the cutoff.
* Provisional: a cliff whose silence since the cutoff is shorter than
  ``min_silence_days``. The result is still computed and flagged, but it is weak
  evidence: the account may simply not have posted yet.
* ``p_silence = exp(-rate * silence_days)`` where ``rate`` is the baseline
  activities per day and ``silence_days`` is the time from the last activity to
  ``now``. It is the Poisson probability of seeing no activity for that long if the
  account kept its baseline rate. Lower means more anomalous.

Composite score
---------------
The score is in ``[0, 1]`` and is only non-zero for cliff accounts (everything
else scores 0). It is the weighted sum of the components below, each in ``[0, 1]``,
using :data:`SCORE_WEIGHTS`:

* ``surprise``: ``-log10(p_silence)`` scaled so 1.0 is reached at
  ``SURPRISE_SATURATION_LOG10``.
* ``abruptness``: how close to the cutoff the last activity was
  (1.0 at the cutoff, 0.0 at ``cliff_window_hours`` before it).
* ``active_ratio``: share of baseline days with activity.
* ``regularity``: ``1 - cv / REGULARITY_CV_CEILING`` of the inter-arrival times;
  scheduled automation tends to be more regular than human posting.
* ``round_the_clock``: hour-of-day entropy over ``log2(24)``; people sleep, so
  uniform 24-hour activity is a weak automation hint.
* ``volume``: baseline posts over ``VOLUME_SATURATION_POSTS``.
* ``cluster``: size of the synchronized-stop cluster the account belongs to,
  ``(size - 1) / CLUSTER_SATURATION`` capped at 1. Many accounts going silent
  within the same few minutes suggests shared infrastructure.

Truncated histories
-------------------
An adapter sets ``Account.meta["truncated"]`` when the platform stopped returning
history before the baseline start (for example Reddit's ~1000-item listing cap).
Such an account is scored on what was returned, and ``AccountResult.truncated`` is
set. Missing baseline activity makes an account look less regular, so a truncated
history can turn a real cliff into a miss (a false negative). It cannot create a
false cliff on its own when the platform returns the newest activity first.
A fetch interrupted part-way (budget or error) is truncated too;
``AccountResult.partial_reason`` tells the two apart (``platform_cap``, ``budget``
or ``error``), and a later run fetches the missing older part of an interrupted one.

Per-account data end
--------------------
Each account is scored against its own data end: ``min(effective_now, window_end)``,
where ``window_end`` is where its cached activity window stops (see
:func:`score_accounts`). A resumed run reuses windows fetched for the earlier
run's ``now``, and a cache hit within ``cache_ttl_hours`` or an adapter's partial
fetch can also stop early. Measuring silence past the end of the data would turn
"not fetched yet" into "silent", a false cliff; so silence, ``p_silence`` and the
provisional flag use the account's own end. Activity seen up to ``now`` still rules
out a cliff. Results whose data ends more than :data:`DATA_END_TOLERANCE` before
``effective_now`` carry ``data_end``.

None of this is a determination that an account is a bot. It ranks candidates for
manual review.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from bot_farm_silence_tracker.models import (
    PARTIAL_FETCH_KEY,
    PARTIAL_PLATFORM_CAP,
    STATUS_ACTIVE,
    Account,
    Activity,
)
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, parse_datetime, utcnow

#: Weights of the composite score components. They must sum to 1.0.
SCORE_WEIGHTS: dict[str, float] = {
    "surprise": 0.30,
    "abruptness": 0.15,
    "active_ratio": 0.15,
    "regularity": 0.10,
    "round_the_clock": 0.10,
    "volume": 0.05,
    "cluster": 0.15,
}

SURPRISE_SATURATION_LOG10 = 4.0  # p_silence of 1e-4 or lower gets full marks.
REGULARITY_CV_CEILING = 1.5  # CV at or above this gets zero regularity credit.
VOLUME_SATURATION_POSTS = 100  # Baseline activity count that gets full marks.
CLUSTER_SATURATION = 5  # Cluster of this many accounts beyond the first gets full marks.
_MAX_ENTROPY_BITS = math.log2(24)
#: An account's data end this much (or less) before ``effective_now`` is treated as
#: reaching it; only an earlier end is reported in ``AccountResult.data_end``.
DATA_END_TOLERANCE = timedelta(minutes=1)


#: The id of the main outage among the moments a silence is labelled with.
MAIN_OUTAGE = "cutoff"


@dataclass(frozen=True)
class Marker:
    """A moment inside an outage that silences are labelled with (see the module notes)."""

    id: str
    name: str
    time: datetime
    approximate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "time": isoformat(self.time),
            "approximate": self.approximate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Marker:
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            time=ensure_utc(parse_datetime(str(data["time"]))),
            approximate=bool(data.get("approximate", False)),
        )


@dataclass(frozen=True)
class NearestMarker:
    """The moment a silence began nearest to, and how far after it (negative: before)."""

    marker: Marker
    offset_hours: float

    @property
    def id(self) -> str:
        return self.marker.id

    @property
    def approximate(self) -> bool:
        return self.marker.approximate

    def label(self) -> str:
        """``Name, +2.5 h`` with ``(approx.)`` for an approximate marker."""
        return marker_label(self.marker, self.offset_hours)


def marker_label(marker: Marker, offset_hours: float | None = None) -> str:
    """How people read a marker: its name, ``(approx.)`` and the offset in hours."""
    text = marker.name + (" (approx.)" if marker.approximate else "")
    if offset_hours is None:
        return text
    return f"{text}, {offset_hours:+.1f} h"


def nearest_marker(when: datetime, anchors: Sequence[Marker]) -> NearestMarker | None:
    """The anchor (marker or main outage) nearest to ``when``.

    Ties: at the same distance the earlier anchor wins (a silence follows its cause,
    not the other way round); at the same time the one listed first wins, and
    :attr:`ScoringParams.anchors` lists the main outage first. ``None`` without
    anchors.
    """
    if not anchors:
        return None
    moment = ensure_utc(when)
    best = min(
        range(len(anchors)),
        key=lambda i: (abs(moment - ensure_utc(anchors[i].time)), ensure_utc(anchors[i].time), i),
    )
    marker = anchors[best]
    offset = (moment - ensure_utc(marker.time)).total_seconds() / 3600
    return NearestMarker(marker, offset)


@dataclass(frozen=True)
class ScoringParams:
    """All thresholds for one scoring pass.

    ``cutoff`` has no default: it comes from the scan's outage event (see
    :mod:`bot_farm_silence_tracker.events`). The window defaults below are the
    algorithm's own, which an event that does not set its windows uses.
    """

    cutoff: datetime
    now: datetime = field(default_factory=utcnow)
    baseline_days: int = 30
    min_silence_days: float = 3.0
    min_active_days: int = 12
    min_posts: int = 20
    cliff_window_hours: float = 48.0
    sync_minutes: float = 30.0
    #: The steady test also needs activity in the last ``recent_days`` before the
    #: baseline end, and weekly counts with a coefficient of variation of at most
    #: ``max_weekly_cv`` (see the module notes).
    recent_days: float = 7.0
    max_weekly_cv: float = 1.0
    #: How many placebo cutoffs, one, two, ... weeks before the real one, a run is
    #: compared with (see :mod:`bot_farm_silence_tracker.controls`). Each placebo
    #: cutoff gets its own sample, discovered and fetched in windows moved back by the
    #: same number of weeks, so every pass selects its accounts the same way. 0 turns
    #: the comparison off. It is 0 by default here because the placebo samples repeat
    #: discovery and fetching; the CLI (and so the app) passes ``--placebo-weeks``,
    #: which defaults to :data:`bot_farm_silence_tracker.controls.DEFAULT_PLACEBO_WEEKS`.
    placebo_weeks: int = 0
    #: Newest moment the source's data covers (``Source.data_horizon``), or ``None``
    #: when the source is live or did not say. Scoring uses :attr:`effective_now`.
    horizon: datetime | None = None
    #: Mark every cliff provisional whatever the silence length, for example when
    #: ``--allow-stale-source`` let a scan run on data that ends too early (or
    #: whose horizon is unknown). It is stored with the run's scoring params, so it
    #: is sticky for that run: ``bot-farm-silence-tracker report --run-id`` re-scores it
    #: provisional too. A ``scan --from-run`` resume does not inherit it; the new
    #: run checks the data horizon again and sets it only if the source is still
    #: stale.
    force_provisional: bool = False
    #: The event's markers (see the module notes), in time order. Empty: the windows
    #: are the classic ones and no output carries marker columns.
    markers: tuple[Marker, ...] = ()
    #: How long before the earliest marker the silence window starts.
    marker_margin_hours: float = 0.0

    @property
    def onset(self) -> datetime | None:
        """``earliest marker - marker_margin_hours``, or ``None`` without markers."""
        if not self.markers:
            return None
        earliest = min(ensure_utc(m.time) for m in self.markers)
        return earliest - timedelta(hours=self.marker_margin_hours)

    @property
    def baseline_end(self) -> datetime:
        """End of the baseline window (exclusive): the cutoff, or the onset when earlier."""
        onset = self.onset
        return self.cutoff if onset is None else min(self.cutoff, onset)

    @property
    def window_start(self) -> datetime:
        """Start of the silence window: ``cutoff - cliff_window_hours``, or the onset
        when earlier."""
        start = self.cutoff - timedelta(hours=self.cliff_window_hours)
        onset = self.onset
        return start if onset is None else min(start, onset)

    @property
    def window_hours(self) -> float:
        """Length of the silence window in hours (``cliff_window_hours`` without markers)."""
        if not self.markers:
            return self.cliff_window_hours
        return (ensure_utc(self.cutoff) - ensure_utc(self.window_start)).total_seconds() / 3600

    @property
    def anchors(self) -> tuple[Marker, ...]:
        """The main outage (id :data:`MAIN_OUTAGE`) and then the markers; empty
        without markers, so classic runs carry no labels."""
        if not self.markers:
            return ()
        main = Marker(MAIN_OUTAGE, "Main outage (cutoff)", ensure_utc(self.cutoff), False)
        return (main, *self.markers)

    @property
    def effective_now(self) -> datetime:
        """``now``, or the source's data horizon when that is earlier."""
        if self.horizon is None:
            return self.now
        return min(ensure_utc(self.now), ensure_utc(self.horizon))

    @property
    def provisional(self) -> bool:
        """Whether cliff results are provisional: too little silence, or forced."""
        return self.force_provisional or self.days_since_cutoff < self.min_silence_days

    def provisional_at(self, data_end: datetime | None) -> bool:
        """Whether a cliff scored against data ending at ``data_end`` is provisional.

        ``data_end`` ``None`` (or later than :attr:`effective_now`) means
        :attr:`effective_now`, which gives :attr:`provisional`.
        """
        if self.force_provisional:
            return True
        end = self.effective_now if data_end is None else min(self.effective_now, data_end)
        days = (ensure_utc(end) - ensure_utc(self.cutoff)).total_seconds() / 86400
        return days < self.min_silence_days

    @property
    def baseline_start(self) -> datetime:
        """Start of the baseline window (inclusive)."""
        return self.baseline_end - timedelta(days=self.baseline_days)

    @property
    def fetch_start(self) -> datetime:
        """Where a scan's fetch window starts: the baseline start. The placebo
        cutoffs draw samples of their own (see :mod:`bot_farm_silence_tracker.controls`),
        so the real sample needs no older data."""
        return self.baseline_start

    @property
    def days_since_cutoff(self) -> float:
        """Days from the cutoff to :attr:`effective_now` (negative if the cutoff is later)."""
        return (self.effective_now - self.cutoff).total_seconds() / 86400

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe primitives."""
        data = asdict(self)
        data["cutoff"] = isoformat(self.cutoff)
        data["now"] = isoformat(self.now)
        data["horizon"] = isoformat(self.horizon) if self.horizon is not None else None
        data["effective_now"] = isoformat(self.effective_now)
        if self.markers:
            data["markers"] = [m.to_dict() for m in self.markers]
        else:
            # Runs without markers store exactly what they stored before markers.
            data.pop("markers")
            data.pop("marker_margin_hours")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScoringParams:
        """Inverse of :meth:`to_dict`."""
        values = dict(data)
        values["cutoff"] = parse_datetime(values["cutoff"])
        values["now"] = parse_datetime(values["now"])
        values.pop("effective_now", None)  # Derived; stored for readers of the run.
        if values.get("horizon"):
            values["horizon"] = parse_datetime(values["horizon"])
        else:
            values.pop("horizon", None)
        values["markers"] = tuple(Marker.from_dict(m) for m in values.get("markers") or ())
        return cls(**values)


@dataclass
class AccountResult:
    """Metrics, flags and score for one account."""

    platform: str
    user_id: str
    handle: str
    status: str
    baseline_posts: int = 0
    active_days: int = 0
    active_day_ratio: float = 0.0
    posts_per_day: float = 0.0
    interarrival_cv: float | None = None
    last_activity: datetime | None = None
    silence_hours: float | None = None
    posts_after_cutoff: int = 0
    account_age_days_at_cutoff: float | None = None
    hour_entropy: float | None = None
    #: Distinct UTC hours of day with baseline activity (for the Miller-Madow bias
    #: correction of ``hour_entropy``).
    hour_bins: int = 0
    p_silence: float | None = None
    eligible: bool = False
    cliff: bool = False
    provisional: bool = False
    score: float = 0.0
    cluster_id: int | None = None
    cluster_size: int = 1
    truncated: bool = False
    #: Why a truncated history is short: ``platform_cap`` (the platform stopped
    #: returning older history), ``budget`` or ``error`` (the fetch was interrupted;
    #: a later run fetches the rest). ``None`` when not truncated.
    partial_reason: str | None = None
    #: Where this account's data ends, when that is more than
    #: :data:`DATA_END_TOLERANCE` before ``effective_now`` (a reused or partial
    #: window). Silence was measured to it. ``None`` when the data reaches
    #: ``effective_now``.
    data_end: datetime | None = None
    #: The platform reports the account as deactivated (status ``deleted``; it may
    #: come back), as opposed to deleted outright or taken down. From
    #: ``Account.meta["deactivated"]``.
    deactivated: bool = False
    #: Reposts after the cutoff that were not counted as activity (an adapter that
    #: leaves reposts out by default records them in
    #: ``Account.meta["reposts_after_cutoff"]``). A cliff account with reposts here
    #: stopped posting but not necessarily stopped acting. ``None`` when unknown.
    reposts_after_cutoff: int | None = None
    components: dict[str, float] = field(default_factory=dict)
    #: Two-phase sampling only (``--baseline-sample-rate``, see
    #: :mod:`bot_farm_silence_tracker.early_exit`): the account's stratum (``A``, ``B``, ``C`` or
    #: ``U``), its inclusion probability, its Horvitz-Thompson weight (``1 /
    #: inclusion_prob`` when fully fetched, ``None`` otherwise) and, when it was not
    #: fully fetched, why (``precheck_only`` or ``phase2_pending``). All ``None`` in a
    #: census run.
    stratum: str | None = None
    inclusion_prob: float | None = None
    weight: float | None = None
    fetch_status: str | None = None
    #: Two-phase sampling only: a sampled ``A``/``C`` account (in the sample or fetched
    #: past the stop point) whose full phase-2 history disagrees with its pre-check by
    #: showing a cliff-candidate pattern (see
    #: :func:`bot_farm_silence_tracker.early_exit.moved_to_candidate`). Recomputed from the cached
    #: history whenever a run is scored, so ``report`` sees it too.
    moved_after_precheck: bool = False
    #: Two-phase sampling only: an account fetched past the stop point
    #: (``fetch_status`` ``after_stop``) whose full history scores as a cliff. It is
    #: not in the estimates, but it is a known cliff of the frame, so it raises the
    #: lower bound of the cliff count.
    cliff_after_stop: bool = False
    #: Runs with markers only: the marker (or ``cutoff``, the main outage) the
    #: account's silence began nearest to, the hours from it to the last activity
    #: (negative: before it), and whether that marker's time is approximate. ``None``
    #: for accounts that are not cliffs.
    nearest_marker: str | None = None
    nearest_marker_offset_hours: float | None = None
    nearest_marker_approximate: bool | None = None
    #: Runs with markers only: the nearest marker of the account's stop-time cluster
    #: (of the cluster's first stop). ``None`` outside a cluster.
    cluster_nearest_marker: str | None = None
    #: The steady test's numbers: the coefficient of variation of the weekly baseline
    #: counts (``None`` for a baseline shorter than a week) and the activities in the
    #: last ``recent_days`` before the baseline end.
    weekly_cv: float | None = None
    recent_posts: int = 0
    #: Baseline activities per UTC hour of day (24 counts). Not an output column: the
    #: permutation null of the stop-time clusters draws stop times from it.
    hour_profile: tuple[int, ...] = ()
    #: Set after scoring (:func:`bot_farm_silence_tracker.controls.annotate`): the
    #: names of the signals that fired, how many of them do not depend on the outage's
    #: timing, and the stop-time cluster's p-value under the permutation null of the
    #: run's largest cluster, which is already adjusted for the run's many clusters
    #: (``None`` outside a tested cluster).
    signals: tuple[str, ...] = ()
    independent_signals: int = 0
    cluster_p_adjusted: float | None = None

    def to_row(
        self, extra_columns: Sequence[str] = (), *, marker_columns: bool = False
    ) -> dict[str, Any]:
        """Flatten to the output row used by the CSV and JSON writers.

        ``extra_columns`` are the adapter's extra columns (``Source.extra_columns``,
        names from :data:`EXTRA_COLUMNS`). They follow ``data_end`` and are left out
        when not declared, so other platforms keep their columns. ``marker_columns``
        (a run with markers) appends :data:`MARKER_COLUMNS` at the very end, so the
        output of a run without markers keeps its columns byte for byte.
        """

        def rounded(value: float | None, digits: int = 4) -> float | None:
            return None if value is None else round(value, digits)

        row = {
            "handle": self.handle,
            "user_id": self.user_id,
            "platform": self.platform,
            "score": round(self.score, 4),
            "provisional": self.provisional,
            "cluster_id": self.cluster_id,
            "cluster_size": self.cluster_size,
            "status": self.status,
            "baseline_posts": self.baseline_posts,
            "active_days": self.active_days,
            "active_day_ratio": rounded(self.active_day_ratio),
            "posts_per_day": rounded(self.posts_per_day),
            "interarrival_cv": rounded(self.interarrival_cv),
            "last_activity": isoformat(self.last_activity) if self.last_activity else None,
            "silence_hours": rounded(self.silence_hours, 2),
            "p_silence": None if self.p_silence is None else float(f"{self.p_silence:.4g}"),
            "posts_after_cutoff": self.posts_after_cutoff,
            "account_age_days_at_cutoff": rounded(self.account_age_days_at_cutoff, 1),
            "hour_entropy": rounded(self.hour_entropy),
            "truncated": self.truncated,
            "partial_reason": self.partial_reason,
            "data_end": isoformat(self.data_end) if self.data_end else None,
        }
        extras = {
            "reposts_after_cutoff": self.reposts_after_cutoff,
            "deactivated": self.deactivated,
        }
        for name in extra_columns:
            if name not in extras:
                raise ValueError(f"unknown extra output column {name!r}")
            row[name] = extras[name]
        if self.stratum is not None:
            # Only two-phase runs carry these, so census output keeps its columns.
            row.update(
                stratum=self.stratum,
                inclusion_prob=rounded(self.inclusion_prob, 6),
                weight=rounded(self.weight, 6),
                fetch_status=self.fetch_status,
            )
        # The method's columns (see METHOD_COLUMNS) come after every adapter and
        # sampling column, so those keep their places, and before the marker columns.
        row.update(
            weekly_cv=rounded(self.weekly_cv),
            recent_posts=self.recent_posts,
            cluster_p_adjusted=None
            if self.cluster_p_adjusted is None
            else float(f"{self.cluster_p_adjusted:.4g}"),
            signals=";".join(self.signals),
            independent_signals=self.independent_signals,
        )
        if marker_columns:
            row.update(
                nearest_marker=self.nearest_marker,
                nearest_marker_offset_hours=rounded(self.nearest_marker_offset_hours, 2),
                nearest_marker_approximate=self.nearest_marker_approximate,
                cluster_nearest_marker=self.cluster_nearest_marker,
            )
        return row


#: Output columns an adapter may add with ``Source.extra_columns``.
EXTRA_COLUMNS = frozenset({"reposts_after_cutoff", "deactivated"})
#: Output columns of the method's controls: the steady test's numbers, the cluster's
#: permutation p-value adjusted for the run's many clusters, and the signals that
#: fired (``;``-separated) with the count of those independent of the outage's timing.
#: They follow the adapter's and the sampling columns.
METHOD_COLUMNS = (
    "weekly_cv",
    "recent_posts",
    "cluster_p_adjusted",
    "signals",
    "independent_signals",
)
#: Output columns of a run with markers, appended after every other column.
MARKER_COLUMNS = (
    "nearest_marker",
    "nearest_marker_offset_hours",
    "nearest_marker_approximate",
    "cluster_nearest_marker",
)


def _meta_count(account: Account, key: str) -> int | None:
    """A non-negative integer from ``account.meta[key]``, or ``None``."""
    value = account.meta.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def p_silence(rate_per_day: float, silence_days: float) -> float:
    """Return ``exp(-rate * silence_days)``, the Poisson chance of zero events."""
    return math.exp(-max(0.0, rate_per_day) * max(0.0, silence_days))


def hour_entropy(timestamps: Sequence[datetime]) -> float | None:
    """Shannon entropy in bits of the UTC hour-of-day distribution (0 to log2(24))."""
    if not timestamps:
        return None
    counts = Counter(ts.astimezone(UTC).hour for ts in timestamps)
    total = len(timestamps)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def weekly_counts(timestamps: Sequence[datetime], end: datetime, days: int) -> list[int]:
    """Activities per whole week, counted back from ``end`` over ``days`` days.

    Only whole weeks count (``days // 7`` of them), so every count covers the same
    length of time; the oldest ``days % 7`` days are left out.
    """
    weeks = days // 7
    counts = [0] * weeks
    stop = ensure_utc(end)
    for ts in timestamps:
        age = (stop - ensure_utc(ts)).total_seconds() / 86400
        if 0 < age <= weeks * 7:
            counts[min(weeks - 1, int(age // 7))] += 1
        elif age == 0 and weeks:
            counts[0] += 1
    return counts


def weekly_cv(timestamps: Sequence[datetime], end: datetime, days: int) -> float | None:
    """Coefficient of variation of :func:`weekly_counts` (population standard deviation
    over the mean). ``None`` with fewer than two whole weeks or no activity in them.

    About 0 means the same number of posts every week; a burst in one week and
    nothing in the others gives about 1.7 over four weeks.
    """
    counts = weekly_counts(timestamps, end, days)
    if len(counts) < 2:
        return None
    mean = statistics.fmean(counts)
    if mean <= 0:
        return None
    return statistics.pstdev(counts) / mean


def interarrival_cv(timestamps: Sequence[datetime]) -> float | None:
    """Coefficient of variation of gaps between consecutive timestamps.

    0 means perfectly regular; about 1 is Poisson-like; above 1 is bursty. Needs at
    least three timestamps and a non-zero mean gap, otherwise ``None``.
    """
    if len(timestamps) < 3:
        return None
    ordered = sorted(timestamps)
    gaps = [(b - a).total_seconds() for a, b in pairwise(ordered)]
    mean = statistics.fmean(gaps)
    if mean <= 0:
        return None
    return statistics.pstdev(gaps) / mean


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _weighted_score(components: dict[str, float]) -> float:
    return _clamp01(sum(SCORE_WEIGHTS[name] * components.get(name, 0.0) for name in SCORE_WEIGHTS))


def partial_reason_of(account: Account) -> str | None:
    """Why ``account``'s history is truncated, or ``None`` when it is not.

    Read from ``meta["partial_fetch"]["reason"]``; a truncated history without it
    (an adapter that only sets ``meta["truncated"]``) is a platform cap.
    """
    if not account.meta.get("truncated"):
        return None
    info = account.meta.get(PARTIAL_FETCH_KEY)
    reason = info.get("reason") if isinstance(info, dict) else None
    return str(reason) if reason else PARTIAL_PLATFORM_CAP


def score_account(
    account: Account,
    activities: Iterable[Activity],
    params: ScoringParams,
    *,
    window_end: datetime | None = None,
) -> AccountResult:
    """Compute metrics, the cliff flag and the pre-cluster score for one account.

    ``window_end`` is where this account's data ends (its cached window end).
    Silence, ``p_silence`` and the provisional flag are measured to
    ``min(params.effective_now, window_end)``, so data that stops early is never
    read as silence. ``None`` means the data reaches ``effective_now``.
    """
    cutoff, now = ensure_utc(params.cutoff), ensure_utc(params.effective_now)
    data_end: datetime | None = None
    if window_end is not None and ensure_utc(window_end) < now:
        if now - ensure_utc(window_end) > DATA_END_TOLERANCE:
            data_end = ensure_utc(window_end)
        now = ensure_utc(window_end)
    # Activity up to the real ``now`` is evidence, whatever the data horizon or the
    # account's data end says: a post after the cutoff rules out a cliff even when
    # it is past them. The account's own end (``now`` here) is used only for the
    # silence length, p_silence and the provisional flag.
    real_now = ensure_utc(params.now)
    baseline_start = ensure_utc(params.baseline_start)
    baseline_end = ensure_utc(params.baseline_end)
    # Only the baseline window and after are scored; a cached history may hold older
    # rows fetched by other runs.
    stamps = sorted(a.ts for a in activities if baseline_start <= a.ts <= real_now)

    baseline = [ts for ts in stamps if baseline_start <= ts < baseline_end]
    recent_start = baseline_end - timedelta(days=params.recent_days)
    recent = sum(1 for ts in baseline if ts >= recent_start)
    weekly = weekly_cv(baseline, baseline_end, params.baseline_days)
    after = [ts for ts in stamps if ts >= cutoff]
    last = stamps[-1] if stamps else None

    active_days = len({ts.astimezone(UTC).date() for ts in baseline})
    rate = len(baseline) / params.baseline_days
    silence_hours = max(0.0, (now - last).total_seconds() / 3600) if last else None

    result = AccountResult(
        platform=account.platform,
        user_id=account.user_id,
        handle=account.handle,
        status=account.status,
        baseline_posts=len(baseline),
        active_days=active_days,
        # A baseline that does not start at midnight UTC touches baseline_days + 1
        # dates, so the share is capped at 1.
        active_day_ratio=min(1.0, active_days / params.baseline_days),
        posts_per_day=rate,
        interarrival_cv=interarrival_cv(baseline),
        last_activity=last,
        silence_hours=silence_hours,
        posts_after_cutoff=len(after),
        account_age_days_at_cutoff=(
            (cutoff - ensure_utc(account.created_at)).total_seconds() / 86400
            if account.created_at
            else None
        ),
        hour_entropy=hour_entropy(baseline),
        hour_bins=len({ts.astimezone(UTC).hour for ts in baseline}),
        truncated=bool(account.meta.get("truncated")),
        partial_reason=partial_reason_of(account),
        data_end=data_end,
        deactivated=bool(account.meta.get("deactivated")),
        reposts_after_cutoff=_meta_count(account, "reposts_after_cutoff"),
        p_silence=(
            p_silence(rate, silence_hours / 24) if silence_hours is not None and rate > 0 else None
        ),
        weekly_cv=weekly,
        recent_posts=recent,
        hour_profile=tuple(
            Counter(ts.astimezone(UTC).hour for ts in baseline).get(hour, 0) for hour in range(24)
        ),
    )

    result.eligible = (
        active_days >= params.min_active_days
        and len(baseline) >= params.min_posts
        and account.status == STATUS_ACTIVE
        and (params.recent_days <= 0 or recent > 0)
        # A baseline shorter than two weeks has no weekly spread to test.
        and (weekly is None or weekly <= params.max_weekly_cv)
    )
    gap_hours = (cutoff - last).total_seconds() / 3600 if last else math.inf
    result.cliff = result.eligible and not after and 0 <= gap_hours <= params.window_hours
    result.provisional = result.cliff and params.provisional_at(now)
    nearest = nearest_marker(last, params.anchors) if result.cliff and last else None
    if nearest is not None:
        result.nearest_marker = nearest.id
        result.nearest_marker_offset_hours = nearest.offset_hours
        result.nearest_marker_approximate = nearest.approximate
        # With markers, abruptness is the closeness to the nearest moment of the
        # outage: a stop right at an earlier strike is as abrupt as one at the cutoff.
        gap_hours = abs(nearest.offset_hours)

    if result.cliff:
        surprise = 0.0
        if result.p_silence is not None and result.p_silence > 0:
            surprise = -math.log10(result.p_silence) / SURPRISE_SATURATION_LOG10
        elif result.p_silence == 0:
            surprise = 1.0
        cv = result.interarrival_cv
        result.components = {
            "surprise": _clamp01(surprise),
            "abruptness": _clamp01(1 - gap_hours / params.cliff_window_hours)
            if params.cliff_window_hours > 0
            else 1.0,
            "active_ratio": _clamp01(result.active_day_ratio),
            "regularity": 0.0 if cv is None else _clamp01(1 - cv / REGULARITY_CV_CEILING),
            "round_the_clock": _clamp01((result.hour_entropy or 0.0) / _MAX_ENTROPY_BITS),
            "volume": _clamp01(len(baseline) / VOLUME_SATURATION_POSTS),
            "cluster": 0.0,
        }
        result.score = _weighted_score(result.components)
    return result


def find_clusters(results: Sequence[AccountResult], sync_minutes: float) -> list[list[int]]:
    """Group cliff accounts whose last activity falls in the same sync window.

    Windows are greedy and anchored on data: sort by last activity, open a window at
    the earliest unassigned account, and take every account within ``sync_minutes``
    of that anchor. This avoids the boundary artifacts of fixed clock buckets.
    Returns lists of indexes into ``results``; singletons are included.

    A greedy window is never larger than the best sliding window over the same stops,
    which is the statistic the permutation null of
    :func:`bot_farm_silence_tracker.controls.cluster_tests` uses. Comparing an observed
    cluster with that null is therefore valid but conservative.
    """
    indexed = sorted(
        (i for i, r in enumerate(results) if r.cliff and r.last_activity is not None),
        key=lambda i: results[i].last_activity,  # type: ignore[arg-type,return-value]
    )
    window = timedelta(minutes=sync_minutes)
    groups: list[list[int]] = []
    anchor: datetime | None = None
    for i in indexed:
        ts = results[i].last_activity
        assert ts is not None
        if anchor is None or ts - anchor > window:
            groups.append([i])
            anchor = ts
        else:
            groups[-1].append(i)
    return groups


def apply_clusters(results: list[AccountResult], sync_minutes: float) -> int:
    """Assign ``cluster_id`` and ``cluster_size`` in place; return the cluster count.

    Only groups of two or more accounts get a ``cluster_id`` (numbered from 1 in
    order of stopping time). Singletons keep ``cluster_id = None`` and
    ``cluster_size = 1``. The cluster component is added to each member's score.
    """
    next_id = 1
    for group in find_clusters(results, sync_minutes):
        if len(group) < 2:
            continue
        for i in group:
            res = results[i]
            res.cluster_id = next_id
            res.cluster_size = len(group)
            res.components["cluster"] = _clamp01((len(group) - 1) / CLUSTER_SATURATION)
            res.score = _weighted_score(res.components)
        next_id += 1
    return next_id - 1


def score_accounts(
    items: Iterable[tuple[Account, Sequence[Activity]]],
    params: ScoringParams,
    *,
    window_ends: Mapping[str, datetime] | None = None,
) -> list[AccountResult]:
    """Score every account, then detect synchronized-stop clusters.

    ``window_ends`` maps a user id to where that account's data ends (see
    :func:`score_account`); accounts not in it are scored to ``effective_now``.
    Returns results in input order; use :func:`rank` to sort for output.
    """
    ends = window_ends or {}
    results = [
        score_account(account, acts, params, window_end=ends.get(account.user_id))
        for account, acts in items
    ]
    apply_clusters(results, params.sync_minutes)
    label_clusters(results, params)
    return results


def label_clusters(results: Sequence[AccountResult], params: ScoringParams) -> None:
    """Set ``cluster_nearest_marker`` in place: the marker nearest to the first stop of
    each stop-time cluster. Does nothing without markers."""
    anchors = params.anchors
    if not anchors:
        return
    first: dict[int, datetime] = {}
    for r in results:
        if r.cluster_id is not None and r.last_activity is not None:
            seen = first.get(r.cluster_id)
            first[r.cluster_id] = r.last_activity if seen is None else min(seen, r.last_activity)
    labels = {cid: nearest_marker(ts, anchors) for cid, ts in first.items()}
    for r in results:
        found = labels.get(r.cluster_id) if r.cluster_id is not None else None
        r.cluster_nearest_marker = found.id if found is not None else None


def rank(results: Iterable[AccountResult]) -> list[AccountResult]:
    """Sort by score descending, breaking ties by user id for stable output."""
    return sorted(results, key=lambda r: (-r.score, r.user_id))


def with_params(params: ScoringParams, **changes: Any) -> ScoringParams:
    """Return a copy of ``params`` with some fields replaced."""
    return replace(params, **changes)
