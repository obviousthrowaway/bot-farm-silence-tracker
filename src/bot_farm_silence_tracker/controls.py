"""Signals of automation, the permutation null of stop-time clusters, and placebo runs.

The outage confound
-------------------
The outage that took the bot infrastructure offline also cut off ordinary people who
used the affected services, and its main collapse came at 03:30 local time, when
most people in that time zone were asleep. So a shared stop near a marker or the
cutoff first of all means "affected by the outage": it is what an ordinary user of
the affected networks would show too. Nothing here treats it as evidence of
automation on its own.

Signals
-------
:func:`fired_signals` returns the signals that fired for a cliff account:

* ``synchronized_stop`` (timing): the account is in a stop-time cluster of at least
  :data:`CLUSTER_MIN` accounts whose adjusted permutation p-value is at most
  :data:`CLUSTER_P_MAX`. It depends on the outage's timing, so it is marked
  ``independent=False``.
* ``regular_timing``: the inter-arrival coefficient of variation of the baseline is
  at most :data:`REGULAR_CV_MAX`.
* ``round_the_clock``: the hour-of-day entropy of the baseline, with the
  Miller-Madow correction, is at least :data:`ROUND_THE_CLOCK_BITS` over at least
  :data:`ROUND_THE_CLOCK_MIN_POSTS` posts.

The last two are measured on the baseline before the outage and do not depend on its
timing. A registry ``candidate`` needs at least one of them.

The permutation null of a stop-time cluster
-------------------------------------------
:func:`cluster_tests` asks how often a cluster at least as large as an observed one
appears when the stop times are drawn again from what each account's own posting
habits make likely:

* Each account's stop time is redrawn in the silence window with probability
  proportional to the account's own baseline posting by UTC hour of day (plus
  :data:`PROFILE_PSEUDOCOUNT` per hour), so a night-time collapse is compared with
  the hours the accounts really post at, not with a uniform clock.
* Accounts discovered through the same thread (``Account.meta["discovery_thread"]``,
  recorded by the adapters that discover through threads) form one unit: the unit's
  first stop is redrawn from the unit's pooled profile and the others keep their
  offsets to it (wrapping around the window), so people who stopped together because
  they were in one conversation stay together.
* The test statistic of a permutation is the largest number of stops inside any
  ``sync_minutes`` window of the whole run, so the position of the window is part of
  the test. A cluster's p-value is ``(1 + #{permutations with a maximum >= its
  size}) / (1 + permutations)``.
* Multiplicity: every cluster of a run is compared with the null distribution of the
  run's largest cluster (a single-step maximum statistic). That already controls the
  chance of any false cluster among all of the run's clusters, so no further
  correction (such as Holm's) is applied on top: it would only be doubly
  conservative. ``p_adjusted`` is this p-value; ``p_permutation`` is kept and equals
  it.
* :data:`PERMUTATIONS` is 9999, so the smallest p-value is 0.0001, well below
  :data:`CLUSTER_P_MAX`.
* The observed clusters are found greedily (windows anchored on the earliest
  unassigned stop, see :func:`bot_farm_silence_tracker.scoring.find_clusters`). A
  greedy window never holds more stops than the best sliding window, which is what
  the null measures, so the test is valid but conservative.

The draws are seeded from the run's cutoff and its cliff accounts, so a run always
gets the same p-values. A significant cluster shows that the stops are more
synchronized than the accounts' habits explain; the outage explains that as well as
shared automation does.

Placebo runs
------------
A placebo cutoff ``c_k`` lies ``k`` weeks before the real one (the same weekday and
hour), for ``k = 1 ...`` :attr:`~bot_farm_silence_tracker.scoring.ScoringParams.placebo_weeks`.
Discovery only finds accounts with activity in its window, and that window ends at
the real cutoff. An account that stopped for good at ``c_k`` has no activity there, so
the run's own sample can never hold it: re-scoring that sample at ``c_k`` would count
only accounts that paused and came back, push the placebo rate toward zero and make
almost any real rate look "clearly above". So the run's own sample is never used for
a placebo cutoff.

Instead every placebo cutoff gets a sample of its own (see
:func:`bot_farm_silence_tracker.pipeline.draw_placebo_runs`): the same discovery
procedure, the same seeds and sample size, a seed derived from the run's seed and
``k`` (:func:`placebo_seed`), and the discovery window, ``now``, the markers and the
fetch window all moved back by ``k`` weeks. Every pass, real and placebo, then
selects its accounts the same way relative to its own cutoff. A platform that cannot
discover in earlier weeks (``Source.placebo_discovery_problem``) has no placebo
comparison, and every output says why.

Each pass compares the accounts of its own sample that are active, whose history the
platform did not cap and whose data reaches back to that pass's baseline. For the
cliff rate (cliff accounts per eligible account) and each signal's firing rate (cliff
accounts with that signal per eligible account), the comparison gives the real rate
``r``, the placebo mean ``m`` and standard deviation ``s`` over the ``K`` placebo
samples, and the excess ``r - m`` with a 95% interval
``excess ± t(K - 1) * sqrt(max(s², v_p) / K + max(s², v_r))``. That is the prediction
interval of one more independent sample: ``s²`` estimates the variance of one
sample's rate (sampling noise and week-to-week change), and it is never taken below
the binomial variance of a placebo rate (``v_p``, at the pooled placebo rate) for the
placebo mean, nor below the binomial variance of the real rate (``v_r``) for the real
rate. Both binomial rates use the ``(x + 0.5) / (n + 1)`` adjustment, so a rate of
zero still has noise. The excess is "clearly above the placebo" only when the whole
interval lies above zero; otherwise the difference from ordinary weeks was not
resolved, which is not the same as no difference.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import accumulate
from typing import Any

from bot_farm_silence_tracker.models import PARTIAL_PLATFORM_CAP, STATUS_ACTIVE, Account, Activity
from bot_farm_silence_tracker.scoring import (
    AccountResult,
    Marker,
    ScoringParams,
    nearest_marker,
    partial_reason_of,
    score_accounts,
)
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat

#: ``regular_timing`` fires at or below this inter-arrival coefficient of variation
#: (about 1 is Poisson-like human posting; scheduled posting is far lower).
REGULAR_CV_MAX = 0.5
#: ``round_the_clock`` fires at or above this hour-of-day entropy, in bits, after
#: the Miller-Madow bias correction (24 equally used hours give log2(24), about
#: 4.58; people sleep, and a person posting at random waking hours stays near 4).
ROUND_THE_CLOCK_BITS = 4.3
#: ``round_the_clock`` needs at least this many baseline posts: with fewer, a
#: person's hours look spread out by chance.
ROUND_THE_CLOCK_MIN_POSTS = 50
#: ``synchronized_stop`` fires for a stop-time cluster of at least this many
#: accounts whose adjusted permutation p-value is at most :data:`CLUSTER_P_MAX`.
CLUSTER_MIN = 3
CLUSTER_P_MAX = 0.01
#: Permutations of the cluster null. With 9999 the smallest p-value is 0.0001, well
#: below :data:`CLUSTER_P_MAX`, however many clusters a run has (the maximum statistic
#: needs no per-cluster correction; see the module notes).
PERMUTATIONS = 9999
#: Added to every hour of an account's profile, so an hour it never posted at is
#: unlikely but possible.
PROFILE_PSEUDOCOUNT = 0.5
#: Signals that depend on the outage's timing. A registry candidate needs another one.
TIMING_SIGNALS = frozenset({"synchronized_stop"})
#: ``Account.meta`` key of the thread an account was discovered through.
DISCOVERY_THREAD_KEY = "discovery_thread"
#: Two-sided 95% quantiles of Student's t, by degrees of freedom (placebo runs - 1).
_T_975 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
}
#: Placebo weeks of a scan by default, the fewest it may use (besides 0, off), and the
#: most.
DEFAULT_PLACEBO_WEEKS = 3
MIN_PLACEBO_WEEKS = 3
MAX_PLACEBO_WEEKS = 8
#: The rates the placebo comparison reports, in order.
CLIFF_RATE = "cliff"
INDEPENDENT_RATE = "cliff_with_independent_signal"


@dataclass(frozen=True)
class Signal:
    """One automation hint that fired for an account, with its value."""

    name: str
    value: float
    threshold: str
    meaning: str
    #: Whether the signal is measured independently of the outage's timing. A shared
    #: stop is not (see the module notes).
    independent: bool = True


@dataclass(frozen=True)
class ClusterTest:
    """The permutation test of one stop-time cluster of at least :data:`CLUSTER_MIN`."""

    cluster_id: int
    size: int
    first_stop: datetime
    #: The p-value against the null distribution of the run's largest cluster. It is
    #: already adjusted for the run's many clusters, so ``p_adjusted`` equals it.
    p_permutation: float
    p_adjusted: float
    #: The moment of the outage (a marker id, or ``cutoff``) nearest to the first stop,
    #: and the hours from it (negative: before it).
    nearest: str
    nearest_hours: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "size": self.size,
            "first_stop": isoformat(self.first_stop),
            "p_permutation": float(f"{self.p_permutation:.4g}"),
            "p_adjusted": float(f"{self.p_adjusted:.4g}"),
            "nearest": self.nearest,
            "nearest_hours": round(self.nearest_hours, 2),
        }


@dataclass
class SignalContext:
    """What :func:`fired_signals` needs besides the account: the run's parameters and
    the permutation tests of its clusters."""

    params: ScoringParams
    clusters: dict[int, ClusterTest] = field(default_factory=dict)
    permutations: int = PERMUTATIONS


def miller_madow_bits(entropy_bits: float, bins: int, n: int) -> float:
    """Plug-in entropy (bits) with the Miller-Madow correction ``(m - 1) / 2n`` nats."""
    if n <= 0:
        return entropy_bits
    return entropy_bits + max(0, bins - 1) / (2 * n * math.log(2))


def outage_anchors(params: ScoringParams) -> tuple[Marker, ...]:
    """The moments of the outage: the main outage and the markers (the main outage
    alone for an event without markers)."""
    if params.anchors:
        return params.anchors
    return (Marker("cutoff", "Main outage (cutoff)", ensure_utc(params.cutoff)),)


def _near_text(test: ClusterTest, params: ScoringParams) -> str:
    names = {a.id: a.name for a in outage_anchors(params)}
    name = names.get(test.nearest, test.nearest)
    hours = test.nearest_hours
    side = "after" if hours >= 0 else "before"
    return f"the group's first stop is {abs(hours):.1f} hours {side} the {name} (as reported)"


def fired_signals(result: AccountResult, context: SignalContext) -> list[Signal]:
    """The signals that fired for ``result``, in a fixed order (see the module notes)."""
    params = context.params
    out: list[Signal] = []
    test = context.clusters.get(result.cluster_id) if result.cluster_id is not None else None
    if result.cluster_size >= CLUSTER_MIN and test is not None and test.p_adjusted <= CLUSTER_P_MAX:
        out.append(
            Signal(
                "synchronized_stop",
                float(result.cluster_size),
                f">= {CLUSTER_MIN} accounts stopping within {params.sync_minutes:g} minutes, "
                f"adjusted permutation p-value <= {CLUSTER_P_MAX:g}",
                f"stopped within {params.sync_minutes:g} minutes of "
                f"{result.cluster_size - 1} other cliff accounts (permutation p-value "
                f"{test.p_adjusted:.2g}, against the largest cluster of each permutation); "
                f"{_near_text(test, params)}. A shared stop at the outage is expected of "
                "anyone the outage affected, so it is not evidence of automation on its own",
                independent=False,
            )
        )
    if result.interarrival_cv is not None and result.interarrival_cv <= REGULAR_CV_MAX:
        out.append(
            Signal(
                "regular_timing",
                result.interarrival_cv,
                f"inter-arrival CV <= {REGULAR_CV_MAX:g}",
                "gaps between posts are unusually regular",
            )
        )
    if result.hour_entropy is not None and result.baseline_posts >= ROUND_THE_CLOCK_MIN_POSTS:
        corrected = miller_madow_bits(result.hour_entropy, result.hour_bins, result.baseline_posts)
        if corrected >= ROUND_THE_CLOCK_BITS:
            out.append(
                Signal(
                    "round_the_clock",
                    corrected,
                    f"hour entropy (Miller-Madow) >= {ROUND_THE_CLOCK_BITS:g} bits, "
                    f">= {ROUND_THE_CLOCK_MIN_POSTS} baseline posts",
                    "posts spread evenly over all 24 hours",
                )
            )
    return out


def independent(signals: Iterable[Signal]) -> list[Signal]:
    """The signals that do not depend on the outage's timing."""
    return [s for s in signals if s.independent]


# -- the permutation null -----------------------------------------------------------


def _slots(start: datetime, span_s: float) -> list[tuple[float, float, int]]:
    """The window ``[start, start + span_s)`` cut at UTC hour boundaries, as
    ``(offset seconds, length seconds, UTC hour)``."""
    out: list[tuple[float, float, int]] = []
    offset = 0.0
    while offset < span_s:
        here = start + timedelta(seconds=offset)
        top = here.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        length = min((top - here).total_seconds(), span_s - offset)
        out.append((offset, length, here.astimezone(UTC).hour))
        offset += length
    return out


def _null_seed(results: Sequence[AccountResult], params: ScoringParams) -> int:
    parts = [isoformat(params.cutoff), f"{params.sync_minutes:g}"]
    parts += sorted(
        f"{r.platform}:{r.user_id}:{isoformat(r.last_activity)}"
        for r in results
        if r.cliff and r.last_activity is not None
    )
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _max_in_window(times: list[float], width: float) -> int:
    """The most of the sorted ``times`` inside one window ``[t, t + width]``."""
    best = 0
    left = 0
    for right, value in enumerate(times):
        while value - times[left] > width:
            left += 1
        best = max(best, right - left + 1)
    return best


def cluster_tests(
    results: Sequence[AccountResult],
    params: ScoringParams,
    groups: Mapping[str, str] | None = None,
    *,
    permutations: int = PERMUTATIONS,
    seed: int | None = None,
) -> dict[int, ClusterTest]:
    """The permutation test of every stop-time cluster of at least :data:`CLUSTER_MIN`
    accounts in ``results`` (see the module notes), by cluster id.

    ``groups`` maps a user id to the thread it was discovered through; accounts of
    one thread stay together. ``seed`` replaces the run's own seed (tests).
    """
    clusters: dict[int, list[AccountResult]] = {}
    for r in results:
        if r.cliff and r.cluster_id is not None and r.last_activity is not None:
            clusters.setdefault(r.cluster_id, []).append(r)
    tested = {cid: rows for cid, rows in clusters.items() if len(rows) >= CLUSTER_MIN}
    if not tested:
        return {}
    start = ensure_utc(params.window_start)
    span = (ensure_utc(params.cutoff) - start).total_seconds()
    anchors = outage_anchors(params)
    firsts = {
        cid: min(ensure_utc(r.last_activity) for r in rows)  # type: ignore[arg-type]
        for cid, rows in tested.items()
    }
    if span <= 0 or permutations <= 0:
        raw = dict.fromkeys(tested, 1.0)
    else:
        raw = _permutation_pvalues(
            results,
            params,
            groups or {},
            {cid: len(rows) for cid, rows in tested.items()},
            start,
            span,
            permutations,
            _null_seed(results, params) if seed is None else seed,
        )
    out: dict[int, ClusterTest] = {}
    for cid in sorted(tested):
        found = nearest_marker(firsts[cid], anchors)
        assert found is not None
        out[cid] = ClusterTest(
            cluster_id=cid,
            size=len(tested[cid]),
            first_stop=firsts[cid],
            p_permutation=raw[cid],
            p_adjusted=raw[cid],
            nearest=found.id,
            nearest_hours=found.offset_hours,
        )
    return out


def _permutation_pvalues(
    results: Sequence[AccountResult],
    params: ScoringParams,
    groups: Mapping[str, str],
    sizes: Mapping[int, int],
    start: datetime,
    span: float,
    permutations: int,
    seed: int,
) -> dict[int, float]:
    slots = _slots(start, span)
    units: dict[str, list[AccountResult]] = {}
    for r in results:
        if not r.cliff or r.last_activity is None:
            continue
        thread = groups.get(r.user_id)
        key = f"thread:{thread}" if thread else f"account:{r.platform}:{r.user_id}"
        units.setdefault(key, []).append(r)
    rng = random.Random(seed)
    # Per unit: the cumulative hour weights over the window's slots, and the offsets
    # of its members' stops from its first stop.
    drawn: list[tuple[list[float], float, list[float]]] = []
    for key in sorted(units):
        members = sorted(units[key], key=lambda r: ensure_utc(r.last_activity))  # type: ignore[arg-type]
        first = ensure_utc(members[0].last_activity)  # type: ignore[arg-type]
        offsets = [(ensure_utc(m.last_activity) - first).total_seconds() for m in members]  # type: ignore[arg-type]
        profile = [0.0] * 24
        for m in members:
            for hour, count in enumerate(m.hour_profile or ()):
                profile[hour] += count
        weights = [(profile[hour] + PROFILE_PSEUDOCOUNT) * length for _, length, hour in slots]
        cum = list(accumulate(weights))
        drawn.append((cum, cum[-1], offsets))
    width = params.sync_minutes * 60
    last = len(slots) - 1
    maxima: list[int] = []
    # One permutation at a time, so memory does not grow with the permutations.
    for _ in range(permutations):
        stops: list[float] = []
        for cum, total, offsets in drawn:
            index = min(last, bisect_right(cum, rng.random() * total))
            offset, length, _hour = slots[index]
            first_stop = offset + rng.random() * length
            stops.extend((first_stop + off) % span for off in offsets)
        stops.sort()
        maxima.append(_max_in_window(stops, width))
    counts = Counter(maxima)
    at_least: dict[int, int] = {}
    for size in set(sizes.values()):
        at_least[size] = sum(n for value, n in counts.items() if value >= size)
    return {cid: (1 + at_least[size]) / (1 + permutations) for cid, size in sizes.items()}


def signal_context(
    results: Sequence[AccountResult],
    params: ScoringParams,
    groups: Mapping[str, str] | None = None,
    *,
    permutations: int = PERMUTATIONS,
) -> SignalContext:
    """The run's :class:`SignalContext`: its parameters and cluster tests."""
    return SignalContext(
        params, cluster_tests(results, params, groups, permutations=permutations), permutations
    )


def annotate(results: Iterable[AccountResult], context: SignalContext) -> None:
    """Set each cliff account's ``signals``, ``independent_signals`` and
    ``cluster_p_adjusted`` in place."""
    for r in results:
        r.signals = ()
        r.independent_signals = 0
        r.cluster_p_adjusted = None
        if not r.cliff:
            continue
        fired = fired_signals(r, context)
        r.signals = tuple(s.name for s in fired)
        r.independent_signals = len(independent(fired))
        test = context.clusters.get(r.cluster_id) if r.cluster_id is not None else None
        r.cluster_p_adjusted = test.p_adjusted if test is not None else None


def discovery_groups(accounts: Iterable[Account]) -> dict[str, str]:
    """``{user_id: thread}`` for the accounts that record their discovery thread."""
    out: dict[str, str] = {}
    for account in accounts:
        thread = account.meta.get(DISCOVERY_THREAD_KEY)
        if isinstance(thread, str) and thread:
            out[account.user_id] = thread
    return out


# -- placebo runs --------------------------------------------------------------------


#: Why a comparison has no placebo samples when the caller passed none.
NO_SAMPLES = (
    "no placebo samples were drawn. A placebo cutoff needs a sample of its own, "
    "discovered in a window moved back by the same number of weeks; the run's own "
    "sample is never re-scored at an earlier cutoff, because it holds no account that "
    "stopped for good before discovery"
)


def placebo_seed(seed: int | None, weeks_before: int) -> int:
    """The seed of the placebo sample ``weeks_before`` weeks back, derived from the
    run's seed the same way for every ``k``."""
    text = f"placebo\0{seed if seed is not None else 'none'}\0{weeks_before}"
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % 2**31


@dataclass(frozen=True)
class RateComparison:
    """One rate at the real cutoff against the placebo cutoffs."""

    name: str
    real: float
    placebo: tuple[float, ...]
    mean: float
    sd: float
    excess: float
    low: float
    high: float

    @property
    def clearly_above(self) -> bool:
        """Whether the whole 95% interval of the excess lies above zero."""
        return self.low > 0

    def to_dict(self) -> dict[str, Any]:
        def r(value: float) -> float:
            return round(value, 4)

        return {
            "real": r(self.real),
            "placebo": [r(v) for v in self.placebo],
            "placebo_mean": r(self.mean),
            "placebo_sd": r(self.sd),
            "excess": r(self.excess),
            "excess_low": r(self.low),
            "excess_high": r(self.high),
            "clearly_above": self.clearly_above,
        }


@dataclass(frozen=True)
class PlaceboRun:
    """The counts of one scoring pass: the real cutoff (``weeks_before`` 0) on the
    run's sample, or a placebo cutoff on its own sample."""

    weeks_before: int
    cutoff: datetime
    eligible: int
    cliffs: int
    #: Cliff accounts per signal name, and with any signal independent of the timing.
    signals: dict[str, int]
    #: Accounts compared in this pass, and the sampled accounts left out (not active,
    #: a capped history, or data that does not reach back to the pass's baseline).
    accounts: int = 0
    excluded: int = 0

    def rate(self, name: str) -> float:
        return self.count(name) / self.eligible if self.eligible else 0.0

    def count(self, name: str) -> int:
        return self.cliffs if name == CLIFF_RATE else self.signals.get(name, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weeks_before": self.weeks_before,
            "cutoff": isoformat(self.cutoff),
            "accounts": self.accounts,
            "excluded": self.excluded,
            "eligible": self.eligible,
            "cliff": self.cliffs,
            "signals": dict(sorted(self.signals.items())),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PlaceboRun:
        """The inverse of :meth:`to_dict` (a stored run's summary)."""
        signals = data.get("signals") or {}
        return cls(
            weeks_before=int(data["weeks_before"]),
            cutoff=ensure_utc(datetime.fromisoformat(str(data["cutoff"]))),
            eligible=int(data["eligible"]),
            cliffs=int(data["cliff"]),
            signals={str(k): int(v) for k, v in dict(signals).items()},
            accounts=int(data.get("accounts", 0)),
            excluded=int(data.get("excluded", 0)),
        )


#: Signal names in the comparison, in order.
SIGNAL_NAMES = ("synchronized_stop", "regular_timing", "round_the_clock")


def _binomial_variance(count: int, total: float, n: float) -> float:
    """The binomial variance of one rate measured on ``n`` accounts, at the rate
    ``(count + 0.5) / (total + 1)``, so that a rate of zero still has noise."""
    if n <= 0:
        return 0.25
    p = (count + 0.5) / (total + 1.0)
    return p * (1 - p) / n


@dataclass
class PlaceboComparison:
    """The real cutoff against :attr:`ScoringParams.placebo_weeks` placebo cutoffs,
    each scored on a sample of its own (see the module notes)."""

    weeks: int
    #: Accounts of the run's sample compared at the real cutoff, and the scored
    #: accounts left out of it.
    accounts: int = 0
    excluded: int = 0
    real: PlaceboRun | None = None
    placebos: list[PlaceboRun] = field(default_factory=list)
    #: Why there is no comparison, or ``None``.
    unavailable: str | None = None
    #: Requests the placebo samples' discovery and fetches made beyond the run's own.
    requests: int = 0

    @property
    def available(self) -> bool:
        return self.unavailable is None and self.real is not None and len(self.placebos) >= 2

    def compare(self, name: str) -> RateComparison | None:
        if not self.available:
            return None
        assert self.real is not None
        rates = tuple(run.rate(name) for run in self.placebos)
        k = len(rates)
        mean = statistics.fmean(rates)
        sd = statistics.stdev(rates) if k > 1 else 0.0
        # The spread of K placebo samples can be 0 by chance (no placebo cliff at all).
        # It is never taken below the binomial noise of a placebo rate (for the mean)
        # or of the real rate (for the real rate).
        placebo_n = statistics.fmean(run.eligible for run in self.placebos)
        v_placebo = _binomial_variance(
            sum(run.count(name) for run in self.placebos),
            sum(run.eligible for run in self.placebos),
            placebo_n,
        )
        v_real = _binomial_variance(self.real.count(name), self.real.eligible, self.real.eligible)
        variance = max(sd * sd, v_placebo) / k + max(sd * sd, v_real)
        half = _T_975.get(k - 1, 1.96) * math.sqrt(variance)
        real = self.real.rate(name)
        excess = real - mean
        return RateComparison(name, real, rates, mean, sd, excess, excess - half, excess + half)

    def rates(self) -> list[RateComparison]:
        found = [self.compare(name) for name in (CLIFF_RATE, *SIGNAL_NAMES, INDEPENDENT_RATE)]
        return [c for c in found if c is not None]

    @property
    def clearly_above(self) -> bool:
        cliff = self.compare(CLIFF_RATE)
        return cliff is not None and cliff.clearly_above

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "weeks": self.weeks,
            "design": "separate placebo samples, discovered in windows moved back by k weeks",
            "accounts": self.accounts,
            "excluded": self.excluded,
            "unavailable": self.unavailable,
            "requests": self.requests,
            "headline": headline(self),
        }
        if self.real is not None:
            data["real"] = self.real.to_dict()
            data["placebos"] = [run.to_dict() for run in self.placebos]
        if self.available:
            data["rates"] = {c.name: c.to_dict() for c in self.rates()}
            data["clearly_above"] = self.clearly_above
        return data


def stored_placebo_runs(
    data: Mapping[str, Any] | None,
) -> tuple[list[PlaceboRun] | None, str | None, int]:
    """The placebo samples' counts, the reason there were none, and the extra requests,
    as a run's summary stored them (``summary["placebo"]``)."""
    if not isinstance(data, Mapping):
        return None, None, 0
    requests = int(data.get("requests") or 0)
    if data.get("design") is None:
        return (
            None,
            (
                "the run was scanned before placebo cutoffs had samples of their own, and "
                "re-scoring its own sample at earlier cutoffs is biased; rescan to compare"
            ),
            requests,
        )
    runs = data.get("placebos")
    if data.get("unavailable") or not isinstance(runs, list) or not runs:
        return None, str(data.get("unavailable") or NO_SAMPLES), requests
    return [PlaceboRun.from_dict(run) for run in runs], None, requests


def _percent(value: float) -> str:
    return f"{100 * value:.0f}%"


def _points(value: float) -> str:
    return f"{100 * value:+.0f}"


def rate_text(c: RateComparison) -> str:
    """``14% vs placebo 11% ± 3% (excess +3 points, 95% interval -5 to +11)``."""
    return (
        f"{_percent(c.real)} vs placebo {_percent(c.mean)} ± {_percent(c.sd)} (excess "
        f"{_points(c.excess)} points, 95% interval {_points(c.low)} to {_points(c.high)})"
    )


def headline(comparison: PlaceboComparison | None) -> str:
    """One line about the placebo comparison, for every output.

    It says prominently when the cliff rate is not clearly above the placebo, and why
    there is no comparison when there is none.
    """
    if comparison is None:
        return "PLACEBO: no placebo comparison for this run."
    if not comparison.available:
        return f"PLACEBO: no placebo comparison: {comparison.unavailable or 'too few runs'}."
    cliff = comparison.compare(CLIFF_RATE)
    assert cliff is not None
    size = round(statistics.fmean(run.accounts for run in comparison.placebos))
    text = (
        f"PLACEBO: cliff rate {rate_text(cliff)}, over {comparison.accounts} accounts at the "
        f"real cutoff and {len(comparison.placebos)} placebo samples of their own (about "
        f"{size} accounts each), discovered and scored 1 to {len(comparison.placebos)} "
        "weeks earlier at the same weekday and hour."
    )
    if cliff.clearly_above:
        return text + " The cliff rate is clearly above the placebo."
    return (
        text + " NOT CLEARLY ABOVE THE PLACEBO: the cliff rate is not clearly higher than in "
        "ordinary weeks (the difference was not resolved), so these cliffs alone are not "
        "evidence of the outage, let alone of automation."
    )


def placebo_lines(comparison: PlaceboComparison | None) -> list[str]:
    """The headline, then one line per signal, then the placebo samples' cost."""
    lines = [headline(comparison)]
    if comparison is None:
        return lines
    if comparison.available:
        for c in comparison.rates():
            if c.name != CLIFF_RATE:
                lines.append(f"  {c.name} rate {rate_text(c)}")
    if comparison.requests:
        lines.append(
            f"  The placebo samples made {comparison.requests} requests beyond the run's own."
        )
    return lines


def shifted_params(params: ScoringParams, weeks: int) -> ScoringParams:
    """``params`` moved ``weeks`` weeks earlier: the cutoff, the markers, ``now`` and
    the data horizon (the same weekday and hour)."""
    shift = timedelta(weeks=weeks)
    return replace(
        params,
        cutoff=ensure_utc(params.cutoff) - shift,
        now=ensure_utc(params.now) - shift,
        horizon=None if params.horizon is None else ensure_utc(params.horizon) - shift,
        markers=tuple(replace(m, time=ensure_utc(m.time) - shift) for m in params.markers),
    )


def score_pass(
    weeks_before: int,
    items: Sequence[tuple[Account, Sequence[Activity]]],
    params: ScoringParams,
    *,
    covered_since: Mapping[str, datetime],
    window_ends: Mapping[str, datetime] | None = None,
    groups: Mapping[str, str] | None = None,
    permutations: int = PERMUTATIONS,
) -> PlaceboRun:
    """Score one pass's own sample at ``params`` (already moved back for a placebo).

    Only accounts that are active, whose history the platform did not cap and whose
    data (``covered_since``) reaches back to the pass's baseline are compared; the
    signals use the pass's own cluster tests.
    """
    baseline = ensure_utc(params.baseline_start)
    compared: list[tuple[Account, Sequence[Activity]]] = []
    excluded = 0
    for account, activities in items:
        if account.status != STATUS_ACTIVE:
            excluded += 1
            continue
        since = covered_since.get(account.user_id)
        capped = partial_reason_of(account) == PARTIAL_PLATFORM_CAP
        if since is None or ensure_utc(since) > baseline or capped:
            excluded += 1
            continue
        compared.append((account, activities))
    results = score_accounts(compared, params, window_ends=window_ends or {})
    context = signal_context(results, params, groups, permutations=permutations)
    signals: Counter[str] = Counter()
    for r in results:
        if not r.cliff or not r.eligible:
            continue
        fired = fired_signals(r, context)
        signals.update(s.name for s in fired)
        if independent(fired):
            signals[INDEPENDENT_RATE] += 1
    return PlaceboRun(
        weeks_before,
        ensure_utc(params.cutoff),
        sum(1 for r in results if r.eligible),
        sum(1 for r in results if r.cliff),
        {name: signals.get(name, 0) for name in (*SIGNAL_NAMES, INDEPENDENT_RATE)},
        accounts=len(compared),
        excluded=excluded,
    )


def placebo_comparison(
    items: Sequence[tuple[Account, Sequence[Activity]]],
    params: ScoringParams,
    *,
    covered_since: Mapping[str, datetime],
    window_ends: Mapping[str, datetime] | None = None,
    groups: Mapping[str, str] | None = None,
    permutations: int = PERMUTATIONS,
    placebos: Sequence[PlaceboRun] | None = None,
    unavailable: str | None = None,
    requests: int = 0,
) -> PlaceboComparison:
    """Compare the run's sample at the real cutoff with the placebo samples' passes.

    ``items`` is the run's own sample; ``placebos`` are the passes of the placebo
    samples (:func:`score_pass` on each, drawn by
    :func:`bot_farm_silence_tracker.pipeline.draw_placebo_runs`). Without them, or
    with ``unavailable`` (why they could not be drawn), there is no comparison: the
    run's own sample is never re-scored at an earlier cutoff (see the module notes).
    """
    weeks = max(0, int(params.placebo_weeks))
    out = PlaceboComparison(weeks, requests=requests)
    if weeks == 0:
        out.unavailable = "turned off for this run (--placebo-weeks 0)"
        return out
    if weeks < 2:
        out.unavailable = "a comparison needs at least 2 placebo weeks"
        return out
    if unavailable is not None or placebos is None:
        out.unavailable = unavailable or NO_SAMPLES
        return out
    real = score_pass(
        0,
        items,
        params,
        covered_since=covered_since,
        window_ends=window_ends,
        groups=groups,
        permutations=permutations,
    )
    out.accounts, out.excluded = real.accounts, real.excluded
    if not real.accounts:
        out.unavailable = (
            f"no scored account of the run has data back to {isoformat(params.baseline_start)}, "
            "the start of its baseline"
        )
        return out
    out.real = real
    out.placebos = sorted(placebos, key=lambda run: run.weeks_before)
    empty = [run.weeks_before for run in out.placebos if not run.eligible]
    if empty:
        out.unavailable = (
            "the placebo samples "
            + ", ".join(f"{k} week{'s' if k > 1 else ''} back" for k in empty)
            + " have no eligible account to compare"
        )
    return out
