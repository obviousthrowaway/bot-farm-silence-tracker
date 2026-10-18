"""Opt-in two-phase ("early-exit") baseline sampling: ``--baseline-sample-rate P``.

Most sampled accounts can never be cliffs, yet a census scan pays for the whole
baseline history of every one of them. With a sample rate ``P`` (``0 < P <= 1``) the
scan runs in two phases.

Phase 1, the pre-check, asks the source only whether each account has activity in
``[cutoff - cliff_window_hours, now)`` and how recent it is: one ``sort=desc,
limit=1`` existence query per kind (:meth:`Source.newest_activity`). Comments are
asked first; when the newest comment is at or after the cutoff, the posts query is
skipped. The account goes into a stratum:

* ``A``: activity at or after the cutoff. It cannot be a cliff.
* ``B``: its newest activity is in ``[cutoff - cliff_window_hours, cutoff)``: a cliff
  candidate. An existence query that could not answer its whole window (it failed,
  or the source had to narrow it) also lands here, so an account is never ruled out
  on incomplete data.
* ``C``: no activity in the window at all. It cannot be a cliff either (it stopped
  earlier, or was not recently active).
* ``U``: suspended, deleted or private. The pre-check already settles it.

Phase 2 fetches the full baseline ``[baseline_start, now)``, exactly as a census
does, for every ``B`` account and for a Bernoulli(``P``) subsample of ``A`` and
``C``. Each account's draw is a hash of the run's sampling salt (``--seed`` when
given) and its user id, so the subsample is reproducible and never changes on a
resume. ``B`` accounts are fetched first, then the selected ``A`` and ``C`` accounts
in ascending order of their draw.

Stopping early
--------------
Because the selected ``A`` and ``C`` accounts are fetched in draw order, a run that
stops part-way (budget, rate limit, Ctrl-C) still holds an exact Bernoulli sample at
a lower rate. ``P_eff`` is the draw of the first selected account, in draw order,
whose phase-2 fetch is not done (``P`` when there is none): every selected account
with a lower draw has finished. Accounts with a draw at or above ``P_eff`` are
treated as not sampled, even when a concurrent worker already finished them, and
every ``A`` and ``C`` account then has inclusion probability ``P_eff``. ``P_eff`` is
defined by the draw order, never by the order in which fetches completed, so it is
the same with ``--concurrency``. A resume (``--from-run``) continues where the run
stopped and raises ``P_eff`` back towards ``P``.

A fetch that was interrupted (``partial_reason`` ``budget`` or ``error``) is not
done: it is pending, like one that never started.

Nonresponse
-----------
A selected account whose phase-2 fetch failed (status ``error``, or skipped by the
adapter) has an unknown outcome, and failures are not ordered by draw. They are
handled conservatively instead of being dropped:

* A failed ``A`` or ``C`` account with a draw below ``P_eff`` is counted as having
  the property for the upper end of every interval and as not having it for the
  lower end, so the interval contains the one a full response would have given.
  The point estimate assumes the failed accounts look like the ones that responded
  (missing at random within the sample).
* A ``B`` account whose phase-2 fetch failed or is still pending (a cliff candidate
  not yet fully fetched) is a certainty unit with an unknown outcome. The cliff
  count becomes a lower bound, each total's upper bound grows by the number of such
  accounts, and the cliff rate is marked incomplete.

Changed data on a resume
------------------------
A resume reuses the stratum the chain stored for an account, without asking again,
only when it does not refresh the pre-check. An account found unavailable always
becomes ``U``. A fresh pre-check (``--refresh``) decides the stratum anew, so an
account it now finds in ``[cutoff - cliff_window_hours, cutoff)`` with nothing after
is a new ``B`` with inclusion probability 1: that is the phase-1 classification.

Phase-2 data never changes the design of a sampled ``A`` or ``C`` account. When its
full history shows a cliff-candidate pattern the pre-check missed (the data changed
after phase 1, for example late ingestion), it keeps its stratum, its draw and its
inclusion probability ``P_eff``, so it still stands for ``1 / P_eff`` accounts; the
same holds for such an account fetched past the stop point (``after_stop``). Making
it ``B`` with ``pi = 1`` would count it once where it represents ``1 / P_eff``
accounts, and bias every estimate. The scan warns about such accounts.

Estimation
----------
Cliffs are counted exactly, for the pre-check snapshot, when every ``B`` account
was fully fetched and no sampled ``A`` or ``C`` account disagreed with its
pre-check, because ``B`` is a census and, per the pre-check, only ``B`` can hold
cliffs (so synchronized-stop clustering, which only looks at cliffs, is unaffected
too). When phase-2 data shows any mover (a sampled or ``after_stop`` ``A``/``C``
account whose full history is a cliff candidate's, cliff or not), the pre-check no
longer rules out cliffs among the unsampled accounts, so the cliff count is an HT
estimate instead: the exact ``B`` part plus the sampled cliffs at weight ``1 /
P_eff`` (the HT interval even when there are none), with its lower bound raised to
every cliff seen, ``after_stop`` ones included, and ``cliffs_exact`` is False.
Every run with an unsampled ``A``/``C`` account also reports ``precheck_drift``:
how many unsampled accounts may have changed unseen, at 95% (see
:func:`precheck_drift`). Totals over eligible accounts (the
cliff-rate denominator, eligible accounts silent since the cutoff but not cliffs,
and eligible accounts active after it) are Horvitz-Thompson estimates: every fully
fetched account counts ``1 / pi``.

Every non-certainty unit has the same ``pi = P_eff`` and is drawn independently, so
the number ``m`` of sampled units with the property is exactly Binomial(``M``,
``P_eff``), where ``M`` is the unknown number of such units among the non-certainty
accounts. The 95% interval for ``M`` inverts the two one-sided binomial tests (the
Clopper-Pearson construction applied to the binomial ``n`` with known ``p``), clipped
to what is logically possible. The interval for a total adds the exactly known
certainty part. The cliff rate is ``cliffs / eligible``; when its numerator has no
sampling error, its interval is the image of the eligible-total interval (the
Fieller interval when the numerator variance is zero). When the cliff count is
estimated too, the rate's interval is Bonferroni: both totals' intervals at level
97.5% each, combined as ``[cliffs_lo / eligible_hi, cliffs_hi / eligible_lo]``.

What the intervals cover: the phase-2 subsampling only. They are conditional on the
phase-1 classification (the pre-check is taken as correct) and on the discovery
sample, so they bound what a full census of the same sample would have reported had
the data not changed since the pre-check, not the discovery sample's own error.
The intervals are conservative: coverage is at least 95%. Nonresponse widens them
as described above.
Standard errors from the unbiased HT variance estimator ``sum(y (1 - pi) / pi**2)``
(delta method for the rate) are reported alongside for reference.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from bot_farm_silence_tracker.models import (
    STATUS_ACTIVE,
    STATUS_ERROR,
    UNAVAILABLE_STATUSES,
    Account,
    Activity,
)
from bot_farm_silence_tracker.scoring import (
    AccountResult,
    ScoringParams,
    score_account,
    score_accounts,
)
from bot_farm_silence_tracker.sources.errors import (
    FATAL_SOURCE_ERRORS,
    AccountSkipped,
    AccountUnavailable,
    SourceError,
)
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat

if TYPE_CHECKING:
    from bot_farm_silence_tracker.sources.base import Source

STRATUM_ACTIVE_AFTER = "A"
STRATUM_CANDIDATE = "B"
STRATUM_SILENT = "C"
STRATUM_UNAVAILABLE = "U"
STRATA = (STRATUM_ACTIVE_AFTER, STRATUM_CANDIDATE, STRATUM_SILENT, STRATUM_UNAVAILABLE)
#: Strata subsampled at rate P; the others are certainty strata (pi = 1).
SAMPLED_STRATA = (STRATUM_ACTIVE_AFTER, STRATUM_SILENT)

#: ``AccountResult.fetch_status`` of an account that was pre-checked only (an ``A``
#: or ``C`` account outside the phase-2 subsample). It is not scored and is not
#: counted as observed in any denominator.
FETCH_PRECHECK_ONLY = "precheck_only"
#: ``AccountResult.fetch_status`` of an account selected for phase 2 whose phase-2
#: fetch has not finished (the run stopped first, or the fetch was interrupted).
#: ``--from-run`` fetches it. For ``A`` and ``C`` accounts it also marks a failed
#: fetch with a draw at or above ``P_eff``: such an account is simply not sampled.
FETCH_PHASE2_PENDING = "phase2_pending"
#: ``AccountResult.fetch_status`` of a selected account whose phase-2 fetch failed
#: (status ``error`` or skipped) and that counts as nonresponse (see the module doc).
FETCH_PHASE2_FAILED = "phase2_failed"
#: ``AccountResult.fetch_status`` of a selected ``A`` or ``C`` account that was fully
#: fetched but whose draw is at or above ``P_eff``: a concurrent worker finished it
#: after an earlier account in draw order was cut short. It is left out of the
#: estimates (it is not part of the Bernoulli(``P_eff``) sample) until a resume.
FETCH_AFTER_STOP = "after_stop"

#: Phase-2 state of a selected account.
PHASE2_COMPLETE = "complete"
PHASE2_FAILED = "failed"
PHASE2_PENDING = "pending"

#: Two-sided level of the reported intervals.
CI_LEVEL = 0.95

CI_METHOD = (
    "Horvitz-Thompson totals (weight 1/pi); 95% interval for each total from the exact "
    "binomial distribution of the sampled count (every A and C account has pi = P_eff and "
    "an independent draw; P_eff is the sample rate actually reached in draw order), "
    "clipped to the possible range; the cliff count is exact for the pre-check snapshot "
    "when every cliff candidate was fully fetched and no sampled A/C account disagreed "
    "with its pre-check, so the "
    "cliff-rate interval is the image of the eligible-total interval (otherwise the cliff "
    "count is an HT estimate too and the rate interval combines both totals' 97.5% "
    "intervals, Bonferroni). The intervals cover the phase-2 subsampling only and are "
    "conditional on the phase-1 classification and the discovery sample. Nonresponse is handled "
    "conservatively: a failed A/C account counts toward the upper bound only (the point "
    "estimate treats failures as missing at random), and a cliff candidate not fully "
    "fetched makes the cliff count a lower bound and widens every upper bound."
)


def phase1_start(params: ScoringParams) -> datetime:
    """Where the phase-1 window starts: the silence window's start
    (:attr:`~bot_farm_silence_tracker.scoring.ScoringParams.window_start`,
    ``cutoff - cliff_window_hours`` without markers), never before the baseline start."""
    start = ensure_utc(params.window_start)
    return max(start, ensure_utc(params.baseline_start))


def window_stratum(stamps: Iterable[datetime], params: ScoringParams) -> str:
    """The stratum ``A``, ``B`` or ``C`` that activity at ``stamps`` gives an account.

    Only timestamps in the phase-1 window ``[cutoff - cliff_window_hours, now]``
    matter: any at or after the cutoff is ``A``, else any before it is ``B``, else
    ``C``.
    """
    cutoff = ensure_utc(params.cutoff)
    start = phase1_start(params)
    real_now = ensure_utc(params.now)
    candidate = False
    for ts in stamps:
        ts = ensure_utc(ts)
        if cutoff <= ts <= real_now:
            return STRATUM_ACTIVE_AFTER
        if start <= ts < cutoff:
            candidate = True
    return STRATUM_CANDIDATE if candidate else STRATUM_SILENT


@dataclass(frozen=True)
class Precheck:
    """The outcome of one account's phase-1 pre-check."""

    stratum: str
    #: Newest activity seen in the phase-1 window, or None.
    newest: datetime | None = None
    #: Every existence query answered its whole window. False puts the account in B.
    complete: bool = True
    #: Existence queries sent.
    queries: int = 0


def precheck(source: Source, account: Account, params: ScoringParams) -> Precheck:
    """Phase 1 for one account: existence queries per kind, newest first.

    Asks :meth:`Source.newest_activity` for each of ``source.precheck_kinds`` in
    order (comments, then posts) over ``[phase1_start, now)``. The first activity at
    or after the cutoff settles ``A`` and skips the remaining kinds. A query that
    fails with a per-account :class:`SourceError` (for example one the archive kept
    timing out on) or that the source had to narrow is incomplete, which makes the
    account ``B`` unless another kind proves ``A``. The failures are then reported to
    :meth:`Source.precheck_finished` once for the account.

    Touches no database, so the concurrent loop can run it in a worker thread.

    Raises:
        AccountUnavailable, AccountSkipped: recorded by the caller (``U``, skipped).
        SourceAuthError, BudgetExceeded, RateLimited: end the scan; the account stays
            pending and a resume pre-checks it again. ``RateLimited`` also comes from
            the source's overload guard in :meth:`Source.precheck_finished`.
    """
    if account.status in UNAVAILABLE_STATUSES:
        return Precheck(STRATUM_UNAVAILABLE)
    start = phase1_start(params)
    cutoff = ensure_utc(params.cutoff)
    until = ensure_utc(params.now)
    newest: datetime | None = None
    complete = True
    queries = 0
    errors: list[SourceError] = []
    for kind in source.precheck_kinds:
        queries += 1
        try:
            found = source.newest_activity(account, kind, start, until)
        except (AccountUnavailable, AccountSkipped, *FATAL_SOURCE_ERRORS):
            raise
        except SourceError as exc:
            complete = False
            errors.append(exc)
            continue
        if ensure_utc(found.covered_since) > start:
            complete = False
        if found.newest is None:
            continue
        ts = ensure_utc(found.newest)
        if cutoff <= ts <= until:
            source.precheck_finished(account, errors)
            return Precheck(STRATUM_ACTIVE_AFTER, ts, complete, queries)
        if start <= ts < cutoff and (newest is None or ts > newest):
            newest = ts
    # The account counts once toward the source's overload guard, whichever kind failed.
    source.precheck_finished(account, errors)
    if newest is not None or not complete:
        return Precheck(STRATUM_CANDIDATE, newest, complete, queries)
    return Precheck(STRATUM_SILENT, None, True, queries)


def selection_draw(salt: int, platform: str, user_id: str) -> float:
    """The account's uniform draw in ``[0, 1)``: a hash of the run's salt and its id."""
    # The tag is part of every stored design: a resume recomputes each account's draw
    # from it, so it must never change (it carries no project name for that reason).
    text = f"baseline-sample:v1:{salt}:{platform}:{user_id}"
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


@dataclass(frozen=True)
class Design:
    """How the two-phase design treats one account."""

    stratum: str
    #: Design inclusion probability: 1 for ``B`` and ``U``, else P. The estimates use
    #: ``P_eff`` for ``A`` and ``C`` instead (see :func:`effective_rate`).
    inclusion_prob: float
    #: Whether the account needs a phase-2 fetch (``B``, and the sampled ``A``/``C``).
    selected: bool
    #: The account's uniform draw (``A`` and ``C`` only), which orders phase 2.
    draw: float | None = None


def design_for(stratum: str, rate: float, salt: int, platform: str, user_id: str) -> Design:
    """Return the design of an account in ``stratum`` under sample rate ``rate``."""
    if stratum == STRATUM_UNAVAILABLE:
        return Design(stratum, 1.0, False)
    if stratum == STRATUM_CANDIDATE:
        return Design(stratum, 1.0, True)
    if stratum not in SAMPLED_STRATA:
        raise ValueError(f"unknown stratum {stratum!r}")
    draw = selection_draw(salt, platform, user_id)
    return Design(stratum, float(rate), draw < rate, draw)


def reclassify(design: Design, account: Account) -> Design:
    """The design after a full phase-2 fetch: an account now found unavailable is ``U``.

    Anything else keeps its design. In particular a sampled ``A`` or ``C`` account whose
    phase-2 history shows a cliff-candidate pattern (see :func:`moved_to_candidate`)
    keeps its stratum, its draw and its inclusion probability ``P_eff``: it was only
    fetched because its draw selected it, so it stands for ``1 / P_eff`` accounts of
    the frame, and an unsampled account may have changed the same way. Making it a
    certainty unit (``pi = 1``) would count it at the weight of one account and bias
    every total low. Its cliffs are estimated instead (see :func:`sampling_summary`).
    """
    if account.status in UNAVAILABLE_STATUSES:
        return Design(STRATUM_UNAVAILABLE, 1.0, design.selected, design.draw)
    return design


def moved_to_candidate(
    design: Design, account: Account, activities: Iterable[Activity], params: ScoringParams
) -> bool:
    """Whether a sampled ``A`` or ``C`` account's phase-2 history is a cliff candidate's.

    True when its full history shows activity in ``[cutoff - cliff_window_hours,
    cutoff)`` and none after it, which the pre-check did not see: the data changed
    after phase 1 (late ingestion, an edit of the archive) or the pre-check was wrong.
    The account keeps its design (see :func:`reclassify`); the caller warns.
    """
    if account.status != STATUS_ACTIVE or design.stratum not in SAMPLED_STRATA:
        return False
    return window_stratum((a.ts for a in activities), params) == STRATUM_CANDIDATE


def effective_rate(designs: Mapping[str, Design], states: Mapping[str, str], rate: float) -> float:
    """``P_eff``: the draw of the first selected ``A``/``C`` account, in draw order,
    whose phase-2 fetch is pending, or ``rate`` when none is.

    ``states`` maps user ids to ``PHASE2_COMPLETE``, ``PHASE2_FAILED`` or
    ``PHASE2_PENDING``; an account missing from it is pending. Failed accounts do not
    stop the walk: they are nonresponse, not unsampled.
    """
    ordered = sorted(
        (d.draw, uid)
        for uid, d in designs.items()
        if d.stratum in SAMPLED_STRATA and d.selected and d.draw is not None
    )
    for draw, uid in ordered:
        if states.get(uid, PHASE2_PENDING) == PHASE2_PENDING:
            return min(float(rate), draw)
    return float(rate)


def phase2_label(design: Design, state: str | None, p_eff: float) -> tuple[str | None, float]:
    """``(fetch_status, inclusion_prob)`` of an account in the estimates.

    ``fetch_status`` is ``None`` for a fully observed account. ``state`` is the
    account's phase-2 state (``None`` when it was not selected).
    """
    if design.stratum == STRATUM_UNAVAILABLE:
        return None, 1.0
    if design.stratum == STRATUM_CANDIDATE:
        if state == PHASE2_COMPLETE:
            return None, 1.0
        return (FETCH_PHASE2_FAILED if state == PHASE2_FAILED else FETCH_PHASE2_PENDING), 1.0
    if not design.selected or design.draw is None:
        return FETCH_PRECHECK_ONLY, p_eff
    if design.draw < p_eff:
        if state == PHASE2_COMPLETE:
            return None, p_eff
        return FETCH_PHASE2_FAILED, p_eff  # Pending is impossible below P_eff.
    if state == PHASE2_COMPLETE:
        return FETCH_AFTER_STOP, p_eff
    return FETCH_PHASE2_PENDING, p_eff


def score_two_phase(
    items: Sequence[tuple[Account, Sequence[Activity]]],
    designs: Mapping[str, Design],
    states: Mapping[str, str],
    rate: float,
    params: ScoringParams,
    *,
    window_ends: Mapping[str, datetime] | None = None,
    precheck_params: ScoringParams | None = None,
) -> tuple[list[AccountResult], float]:
    """Score a two-phase run, in input order. Returns ``(results, P_eff)``.

    ``states`` holds the phase-2 state of the selected accounts (see
    :func:`effective_rate`). Fully observed accounts (``U``, complete ``B``, and the
    complete ``A``/``C`` accounts with a draw below ``P_eff``) are scored and
    clustered as in a census, with ``weight = 1 / inclusion_prob``. A failed fetch is
    scored as a census scores an error and labelled ``phase2_failed``. Every other
    pre-checked account gets ``fetch_status`` ``precheck_only``, ``phase2_pending``
    or ``after_stop``: it is never eligible or a cliff and has no weight. Accounts
    without a design (they failed or were skipped in phase 1) are scored as a census
    scores them.

    Every fully fetched sampled ``A``/``C`` account, in the sample or ``after_stop``,
    gets ``moved_after_precheck`` from :func:`moved_to_candidate`, judged with
    ``precheck_params`` (the parameters the pre-check ran with; ``params`` when not
    given). An ``after_stop`` account also gets ``cliff_after_stop`` when its full
    history scores as a cliff.
    """
    ends = window_ends or {}
    judged = precheck_params or params
    p_eff = effective_rate(designs, states, rate)
    observed: list[int] = []
    labels: dict[int, tuple[str | None, float]] = {}
    out: list[AccountResult | None] = [None] * len(items)
    for i, (account, _acts) in enumerate(items):
        design = designs.get(account.user_id)
        if design is None:
            observed.append(i)
            continue
        fetch_status, prob = phase2_label(design, states.get(account.user_id), p_eff)
        labels[i] = (fetch_status, prob)
        if fetch_status in (None, FETCH_PHASE2_FAILED):
            observed.append(i)
            continue
        unobserved = _unobserved(account, design, fetch_status, prob)
        if fetch_status == FETCH_AFTER_STOP:
            acts = items[i][1]
            unobserved.moved_after_precheck = moved_to_candidate(design, account, acts, judged)
            unobserved.cliff_after_stop = score_account(
                account, acts, params, window_end=ends.get(account.user_id)
            ).cliff
        out[i] = unobserved
    scored = score_accounts([items[i] for i in observed], params, window_ends=ends)
    for i, result in zip(observed, scored, strict=True):
        if i in labels:
            account = items[i][0]
            design = designs[account.user_id]
            fetch_status, prob = labels[i]
            result.stratum = design.stratum
            result.inclusion_prob = prob
            result.fetch_status = fetch_status
            result.weight = 1.0 / prob if fetch_status is None and prob > 0 else None
            if fetch_status is None:
                result.moved_after_precheck = moved_to_candidate(
                    design, account, items[i][1], judged
                )
        out[i] = result
    return [r for r in out if r is not None], p_eff


def _unobserved(account: Account, design: Design, fetch_status: str, prob: float) -> AccountResult:
    """The result of an account that was not fully observed: no metrics, no weight.

    Only the pre-check (or a partial fetch) was read for it, so nothing about its
    baseline is reported.
    """
    return AccountResult(
        platform=account.platform,
        user_id=account.user_id,
        handle=account.handle,
        status=account.status if account.status != STATUS_ERROR else STATUS_ACTIVE,
        stratum=design.stratum,
        inclusion_prob=prob,
        weight=None,
        fetch_status=fetch_status,
    )


# -- estimation ------------------------------------------------------------------------


def _log_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )


def binom_cdf(k: int, n: int, p: float) -> float:
    """``P(X <= k)`` for ``X ~ Binomial(n, p)``."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return min(1.0, sum(math.exp(_log_pmf(j, n, p)) for j in range(k + 1)))


def binom_sf(k: int, n: int, p: float) -> float:
    """``P(X >= k)`` for ``X ~ Binomial(n, p)``."""
    return 1.0 - binom_cdf(k - 1, n, p)


def binomial_n_interval(m: int, p: float, upper: int, level: float = CI_LEVEL) -> tuple[int, int]:
    """Exact interval for the binomial ``n`` given ``m`` successes and known ``p``.

    Returns ``(lo, hi)`` with ``m <= lo <= hi <= upper``: ``lo`` is the smallest ``n``
    with ``P(X >= m) > alpha / 2`` and ``hi`` the largest with ``P(X <= m) > alpha / 2``,
    the two one-sided tests of the Clopper-Pearson construction.
    """
    upper = max(upper, m)
    if p >= 1.0:
        return m, m
    if p <= 0.0:
        return m, upper
    alpha = (1.0 - level) / 2
    lo = m
    if m > 0:
        a, b = m, upper  # P(X >= m | n) grows with n.
        if binom_sf(m, b, p) <= alpha:
            lo = b
        else:
            while a < b:
                mid = (a + b) // 2
                if binom_sf(m, mid, p) > alpha:
                    b = mid
                else:
                    a = mid + 1
            lo = a
    a, b = m, upper  # P(X <= m | n) falls with n.
    while a < b:
        mid = (a + b + 1) // 2
        if binom_cdf(m, mid, p) > alpha:
            a = mid
        else:
            b = mid - 1
    return lo, max(a, lo)


def estimate_total(
    certain: int,
    sampled: int,
    unsampled: int,
    rate: float,
    *,
    responded: int | None = None,
    failed: int = 0,
    certain_incomplete: int = 0,
    level: float = CI_LEVEL,
) -> dict[str, Any]:
    """HT estimate and interval (95% unless ``level`` says otherwise) of a count.

    ``certain`` is the exact count among fully fetched certainty accounts (``pi =
    1``), ``sampled`` the count among the fully fetched non-certainty accounts (``pi
    = rate``, which is ``P_eff``), ``responded`` how many non-certainty accounts were
    fully fetched (with the property or not; needed only with ``failed``), and
    ``unsampled`` how many non-certainty accounts were not sampled.

    ``failed`` sampled accounts have an unknown outcome: the point estimate scales the
    sampled count by ``(responded + failed) / responded``, the lower bound counts
    none of them, the upper bound all of them. ``certain_incomplete`` certainty
    accounts (cliff candidates not fully fetched) have an unknown outcome too and add
    to the upper bound only. ``complete`` is False when either is non-zero. With no
    sample at all (``rate`` 0, no account drawn yet, or every sampled account
    failed), ``estimate`` and ``se`` are ``None`` and the interval is the possible
    range.
    """
    possible = sampled + failed + unsampled
    complete = failed == 0 and certain_incomplete == 0
    drawn = (sampled if responded is None else responded) + failed
    if rate <= 0.0 or (failed and not responded) or (drawn == 0 and unsampled > 0):
        return {
            "estimate": None,
            "ci95": [certain, certain + certain_incomplete + possible],
            "se": None,
            "exact_part": certain,
            "sampled_count": sampled,
            "complete": False,
        }
    adjusted = float(sampled)
    if failed:
        assert responded is not None
        adjusted = sampled * (responded + failed) / responded
    point = certain + adjusted / rate
    se = math.sqrt(adjusted * (1.0 - rate)) / rate
    lo, _ = binomial_n_interval(sampled, rate, possible, level)
    _, hi = binomial_n_interval(sampled + failed, rate, possible, level)
    return {
        "estimate": round(point, 2),
        "ci95": [certain + lo, certain + hi + certain_incomplete],
        "se": round(se, 2),
        "exact_part": certain,
        "sampled_count": sampled,
        "complete": complete,
    }


def estimate_rate(
    cliffs: int, eligible: dict[str, Any], *, cliffs_incomplete: int = 0
) -> dict[str, Any] | None:
    """Cliff rate ``cliffs / eligible`` with the interval mapped from the eligible total.

    ``cliffs_incomplete`` cliff candidates were not fully fetched: the cliff count is
    then a lower bound, the upper end of the interval allows all of them to be
    cliffs, and ``complete`` is False.
    """
    if eligible["estimate"] is None:
        return None
    point_e = float(eligible["estimate"])
    if point_e <= 0:
        return None
    lo_e, hi_e = eligible["ci95"]
    rate = cliffs / point_e
    upper = cliffs + cliffs_incomplete
    return {
        "estimate": round(rate, 5),
        "ci95": [
            round(cliffs / hi_e, 5) if hi_e > 0 else 0.0,
            round(min(1.0, upper / lo_e), 5) if lo_e > 0 else 1.0 if upper else 0.0,
        ],
        # Delta method: the numerator is exact, so only the denominator's error counts.
        "se": round(rate * float(eligible["se"]) / point_e, 5),
        "complete": bool(eligible.get("complete", True)) and cliffs_incomplete == 0,
    }


def exact_cliffs(cliffs: int, cliffs_incomplete: int = 0) -> dict[str, Any]:
    """The cliff count when only certainty units hold cliffs: no sampling error."""
    return {
        "estimate": cliffs,
        "ci95": [cliffs, cliffs + cliffs_incomplete],
        "se": 0.0,
        "exact_part": cliffs,
        "sampled_count": 0,
        "complete": cliffs_incomplete == 0,
    }


def estimate_sampled_rate(
    cliffs: dict[str, Any],
    eligible: dict[str, Any],
    cliffs_wide: dict[str, Any],
    eligible_wide: dict[str, Any],
    *,
    sampled_cliffs: int,
    sampled_eligible: int,
    rate: float,
    scale: float = 1.0,
) -> dict[str, Any] | None:
    """Cliff rate when some cliffs are in the sampled strata, so both totals are estimated.

    The point estimate is the ratio of the two HT totals. The interval is Bonferroni:
    ``cliffs_wide`` and ``eligible_wide`` are the two totals' intervals at level
    ``1 - (1 - CI_LEVEL) / 2`` each, so both hold at once with probability at least
    ``CI_LEVEL``, and then the rate lies in ``[cliffs_lo / eligible_hi, cliffs_hi /
    eligible_lo]``. The standard error is the delta-method one of the ratio:
    ``sum over the sampled units of (1 - p) / p**2 * (cliff - R * eligible)**2``
    divided by the eligible total squared (every cliff is eligible), with the sampled
    counts scaled by ``scale`` for nonresponse as the totals are.
    """
    if cliffs["estimate"] is None or eligible["estimate"] is None:
        return None
    point_e = float(eligible["estimate"])
    if point_e <= 0:
        return None
    ratio = float(cliffs["estimate"]) / point_e
    lo_c, hi_c = cliffs_wide["ci95"]
    lo_e, hi_e = eligible_wide["ci95"]
    c = sampled_cliffs * scale
    other = max(0.0, (sampled_eligible - sampled_cliffs) * scale)
    var = (1.0 - rate) / rate**2 * (c * (1.0 - ratio) ** 2 + other * ratio**2)
    return {
        "estimate": round(ratio, 5),
        "ci95": [
            round(min(1.0, lo_c / hi_e), 5) if hi_e > 0 else 0.0,
            round(min(1.0, hi_c / lo_e), 5) if lo_e > 0 else 1.0,
        ],
        "se": round(math.sqrt(var) / point_e, 5),
        "complete": bool(eligible.get("complete", True)) and bool(cliffs.get("complete", True)),
    }


def sampling_summary(
    results: Sequence[AccountResult], rate: float, p_eff: float, params: ScoringParams
) -> dict[str, Any]:
    """The ``summary["sampling"]`` block of a two-phase run.

    The frame is every account with a stratum. Nothing in it is dropped: accounts
    that were not fully observed count as unsampled (``A``/``C``) or as incomplete
    certainty units (``B``), and failed fetches as nonresponse (see the module doc).
    """
    strata: dict[str, dict[str, int]] = {s: {"accounts": 0, "full_fetch": 0} for s in STRATA}
    counts = {
        FETCH_PRECHECK_ONLY: 0,
        FETCH_PHASE2_PENDING: 0,
        FETCH_PHASE2_FAILED: 0,
        FETCH_AFTER_STOP: 0,
    }
    cliffs = 0
    cliffs_sampled = 0
    candidates_incomplete = 0
    moved_sampled = moved_after_stop = cliffs_after_stop = 0
    responded = failed = unsampled = 0
    keys = ("eligible", "eligible_silent_not_cliff", "eligible_active_after_cutoff")
    certain = dict.fromkeys(keys, 0)
    sampled = dict.fromkeys(keys, 0)
    for r in results:
        if r.stratum is None:
            continue
        strata[r.stratum]["accounts"] += 1
        is_certain = r.inclusion_prob is not None and r.inclusion_prob >= 1.0
        if r.fetch_status is not None:
            counts[r.fetch_status] += 1
            if r.fetch_status == FETCH_AFTER_STOP:
                moved_after_stop += int(r.moved_after_precheck)
                cliffs_after_stop += int(r.cliff_after_stop)
            if is_certain:
                candidates_incomplete += 1
            elif r.fetch_status == FETCH_PHASE2_FAILED:
                failed += 1
            else:
                unsampled += 1
            continue
        strata[r.stratum]["full_fetch"] += 1
        cliffs += int(r.cliff)
        if not is_certain:
            responded += 1
            # Phase-2 data moved this A/C account to a cliff after the pre-check: it
            # keeps its weight 1 / P_eff, so the cliff count becomes an estimate.
            cliffs_sampled += int(r.cliff)
            moved_sampled += int(r.moved_after_precheck)
        flags = {
            "eligible": r.eligible,
            "eligible_silent_not_cliff": r.eligible and not r.cliff and r.posts_after_cutoff == 0,
            "eligible_active_after_cutoff": r.eligible and r.posts_after_cutoff > 0,
        }
        for key, flag in flags.items():
            if flag:
                (certain if is_certain else sampled)[key] += 1
    nonresponse = {
        "responded": responded,
        "failed": failed,
        "certain_incomplete": candidates_incomplete,
    }
    estimates = {
        key: estimate_total(certain[key], sampled[key], unsampled, p_eff, **nonresponse)
        for key in keys
    }
    cliffs_certain = cliffs - cliffs_sampled
    # Any observed mover means the pre-check snapshot no longer describes the frame:
    # unsampled A/C accounts may hold cliffs too, so the cliff count is estimated
    # (HT, even with no sampled cliff) instead of being taken as exact.
    movers = moved_sampled + moved_after_stop
    known_cliffs = cliffs + cliffs_after_stop
    if cliffs_sampled or movers:
        cliff_total = _with_known_floor(
            estimate_total(cliffs_certain, cliffs_sampled, unsampled, p_eff, **nonresponse),
            known_cliffs,
        )
        wide = 1.0 - (1.0 - CI_LEVEL) / 2  # Bonferroni: two intervals hold at once.
        cliff_rate = estimate_sampled_rate(
            cliff_total,
            estimates["eligible"],
            _with_known_floor(
                estimate_total(
                    cliffs_certain, cliffs_sampled, unsampled, p_eff, **nonresponse, level=wide
                ),
                known_cliffs,
            ),
            estimate_total(
                certain["eligible"],
                sampled["eligible"],
                unsampled,
                p_eff,
                **nonresponse,
                level=wide,
            ),
            sampled_cliffs=cliffs_sampled,
            sampled_eligible=sampled["eligible"],
            rate=p_eff,
            scale=(responded + failed) / responded if failed and responded else 1.0,
        )
    else:
        cliff_total = exact_cliffs(cliffs, candidates_incomplete)
        cliff_rate = estimate_rate(
            cliffs, estimates["eligible"], cliffs_incomplete=candidates_incomplete
        )
    framed = sum(v["accounts"] for v in strata.values())
    return {
        "baseline_sample_rate": rate,
        "effective_sample_rate": p_eff,
        "phase1_start": isoformat(phase1_start(params)),
        "strata": strata,
        "precheck_only": counts[FETCH_PRECHECK_ONLY],
        "phase2_pending": counts[FETCH_PHASE2_PENDING],
        "phase2_failed": counts[FETCH_PHASE2_FAILED],
        "after_stop": counts[FETCH_AFTER_STOP],
        "frame": framed,
        "cliffs": cliffs,
        "cliffs_sampled": cliffs_sampled,
        "cliffs_exact": candidates_incomplete == 0 and cliffs_sampled == 0 and movers == 0,
        "cliffs_after_stop": cliffs_after_stop,
        "precheck_drift": precheck_drift(
            moved_sampled, moved_after_stop, responded, failed, unsampled, p_eff
        ),
        "candidates_incomplete": candidates_incomplete,
        "nonresponse": failed,
        "estimates": {**estimates, "cliffs": cliff_total, "cliff_rate": cliff_rate},
        "ci_method": CI_METHOD,
    }


def _with_known_floor(total: dict[str, Any], known: int) -> dict[str, Any]:
    """``total`` with its interval raised to at least ``known``, the cliffs actually seen.

    Cliffs among accounts fetched past the stop point (``after_stop``) are not in the
    HT estimate, but they exist, so the count cannot be lower than every cliff seen.
    """
    lo, hi = total["ci95"]
    if lo >= known:
        return total
    # The estimate stays as it is (Horvitz-Thompson is unbiased); ``known_floor`` records
    # that the lower bound was raised, so the report can say why it exceeds the estimate.
    return {**total, "ci95": [known, max(hi, known)], "known_floor": known}


def precheck_drift(
    moved_sampled: int,
    moved_after_stop: int,
    responded: int,
    failed: int,
    unsampled: int,
    p_eff: float,
) -> dict[str, Any]:
    """How far the pre-check may have drifted from the data phase 2 saw.

    ``moved_sampled`` of the ``responded`` sampled ``A``/``C`` accounts disagreed with
    their pre-check (see :func:`moved_to_candidate`). They are a Bernoulli(``P_eff``)
    sample of the movers among the sampled strata, so the exact binomial interval
    bounds how many movers there are in all; ``unseen_upper95`` is its upper end minus
    the ones seen: how many of the ``unsampled`` (and ``failed``) accounts may also
    have changed, at 95%. Each could be a cliff the estimate never sees, so the cliff
    count could be low by at most that many. ``moved_after_stop`` movers were fetched
    past the stop point: they are among the unsampled accounts, so they are reported,
    not added.
    """
    unseen = unsampled + failed
    _, hi = binomial_n_interval(moved_sampled + failed, p_eff, moved_sampled + unseen)
    return {
        "moved_sampled": moved_sampled,
        "moved_after_stop": moved_after_stop,
        "responded": responded,
        # Movers fetched past the stop point are known to be among the unsampled, so
        # the bound cannot be below their number.
        "unseen_upper95": max(0, min(unseen, max(hi - moved_sampled, moved_after_stop))),
        # Unsampled plus failed A/C accounts: the ones the bound is about.
        "unseen": unseen,
    }


def summary_keys(sampling: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit top-level summary keys of a two-phase run.

    ``eligible`` is set to ``None``: in a census it is the exact number of eligible
    accounts, and no single number means that here. ``eligible_observed`` is the
    unweighted count among the fully fetched accounts, ``eligible_estimate`` and
    ``eligible_ci95`` the Horvitz-Thompson estimate and its interval.
    """
    est = sampling["estimates"]
    rate = est.get("cliff_rate") or {}
    return {
        "eligible": None,
        "eligible_estimate": est["eligible"]["estimate"],
        "eligible_ci95": list(est["eligible"]["ci95"]),
        "cliff_rate_estimate": rate.get("estimate"),
        "cliff_rate_ci95": list(rate["ci95"]) if rate else None,
        "cliff_exact": bool(sampling["cliffs_exact"]),
    }


def drift_line(sampling: Mapping[str, Any]) -> str | None:
    """The pre-check drift line, or None when no ``A``/``C`` account went unseen.

    Printed in every two-phase run with an unsampled (or failed) ``A``/``C`` account,
    so a run that saw no mover still says how many it may have missed.
    """
    drift = sampling.get("precheck_drift")
    if not drift or int(drift.get("unseen", 0)) <= 0:
        return None
    k, n, upper = drift["moved_sampled"], drift["responded"], drift["unseen_upper95"]
    who = "unsampled or failed" if sampling.get("nonresponse") else "unsampled"
    text = (
        f"{k} of {n} sampled A/C accounts disagreed with their pre-check; up to {upper} "
        f"{who} accounts may also have changed (95%), so the cliff count could be low by "
        f"at most {upper}."
    )
    if drift.get("moved_after_stop"):
        text += (
            f" {drift['moved_after_stop']} accounts fetched past the stop point disagreed "
            "too (they are among the unsampled)."
        )
    return text


def format_estimate_lines(sampling: Mapping[str, Any]) -> list[str]:
    """Human-readable, clearly labelled estimate lines for the summary and the report."""
    est = sampling.get("estimates") or {}
    strata = sampling.get("strata") or {}
    rate = sampling.get("baseline_sample_rate")
    p_eff = sampling.get("effective_sample_rate", rate)
    lines = [
        f"ESTIMATES (two-phase sampling, --baseline-sample-rate {rate:g}; totals and the "
        "rate are Horvitz-Thompson estimates with conservative 95% intervals that cover the "
        "phase-2 subsampling only, conditional on the phase-1 classification):",
        "Strata (accounts / fully fetched): "
        + ", ".join(
            f"{name} {strata.get(name, {}).get('accounts', 0)}/"
            f"{strata.get(name, {}).get('full_fetch', 0)}"
            for name in STRATA
        )
        + " (A active after the cutoff, B cliff candidates, C silent through the window, "
        "U unavailable)",
    ]
    if p_eff is not None and rate is not None and p_eff < rate:
        lines.append(
            f"Effective sample rate: {p_eff:.4g} of the requested {rate:g} (the run stopped "
            "before phase 2 reached every selected account; continue it with --from-run)."
        )
    incomplete = int(sampling.get("candidates_incomplete", 0))
    cliffs = sampling.get("cliffs", 0)
    moved = int(sampling.get("cliffs_sampled", 0))
    drift = sampling.get("precheck_drift") or {}
    moved_sampled = int(drift.get("moved_sampled", 0))
    moved_after_stop = int(drift.get("moved_after_stop", 0))
    movers = moved_sampled + moved_after_stop
    mover_parts = []
    if moved_sampled:
        mover_parts.append(f"{moved_sampled} sampled A/C accounts")
    if moved_after_stop:
        mover_parts.append(f"{moved_after_stop} A/C accounts fetched past the stop point")
    after_stop_cliffs = int(sampling.get("cliffs_after_stop", 0))
    cliff_total = est.get("cliffs") or {}
    missing = f" {incomplete} cliff candidates were not fully fetched." if incomplete else ""
    if moved or movers:
        lo, hi = cliff_total["ci95"]
        seen = (
            f"{cliffs} observed in the sample, {moved} of them sampled A/C accounts; "
            + (
                f"{after_stop_cliffs} more fetched past the stop point; "
                if after_stop_cliffs
                else ""
            )
            + f"{' and '.join(mover_parts)} disagreed with their pre-check (data changed "
            "after phase 1), so the pre-check no longer rules out cliffs among the "
            "unsampled accounts and the cliff count is an estimate, not exact."
            + (
                f" The lower bound includes {after_stop_cliffs} cliff accounts fetched past "
                "the stop point."
                if cliff_total.get("known_floor") is not None and after_stop_cliffs
                else ""
            )
        )
        if cliff_total.get("estimate") is None:
            lines.append(
                f"Cliff accounts (estimate): n/a (95% CI {lo:g} to {hi:g}); {seen}{missing}"
            )
        else:
            lines.append(
                f"Cliff accounts (estimate): {cliff_total['estimate']:g} "
                f"(95% CI {lo:g} to {hi:g}); {seen}{missing}"
            )
    elif incomplete:
        lines.append(
            f"Cliff accounts (lower bound): {cliffs}; {incomplete} cliff candidates were not "
            "fully fetched, so the cliff count and the cliff rate are incomplete."
        )
    else:
        lines.append(f"Cliff accounts (exact for the pre-check snapshot): {cliffs}")
    line = drift_line(sampling)
    if line:
        lines.append(line)
    labels = (
        ("eligible", "Eligible accounts (estimate)"),
        (
            "eligible_silent_not_cliff",
            "Eligible, silent since the cutoff but not a cliff (estimate)",
        ),
        ("eligible_active_after_cutoff", "Eligible, active after the cutoff (estimate)"),
    )
    for key, label in labels:
        item = est.get(key)
        if not item:
            continue
        lo, hi = item["ci95"]
        point = "n/a" if item["estimate"] is None else f"{item['estimate']:g}"
        note = "" if item.get("complete", True) else ", widened for incomplete fetches"
        lines.append(f"{label}: {point} (95% CI {lo:g} to {hi:g}{note})")
    cliff_rate = est.get("cliff_rate")
    if cliff_rate:
        lo, hi = cliff_rate["ci95"]
        label = (
            "Cliff rate among eligible (estimate)"
            if cliff_rate.get("complete", True)
            else "Cliff rate among eligible (estimate, INCOMPLETE)"
        )
        lines.append(f"{label}: {cliff_rate['estimate']:.2%} (95% CI {lo:.2%} to {hi:.2%})")
    else:
        lines.append("Cliff rate among eligible: not estimable yet (no A/C account sampled).")
    if sampling.get("nonresponse"):
        lines.append(
            f"{sampling['nonresponse']} sampled accounts failed their phase-2 fetch; the "
            "intervals count them as unknown (conservative), and the point estimate assumes "
            "failed fetches resemble the responders (MAR); --from-run retries them."
        )
    if sampling.get("phase2_pending"):
        lines.append(
            f"{sampling['phase2_pending']} selected accounts still wait for their phase-2 "
            "fetch; continue the run with --from-run."
        )
    return lines
