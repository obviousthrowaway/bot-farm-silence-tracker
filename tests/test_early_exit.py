"""Two-phase ("early-exit") baseline sampling: --baseline-sample-rate."""

from __future__ import annotations

import csv
import json
import random
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.early_exit import (
    FETCH_AFTER_STOP,
    FETCH_PHASE2_FAILED,
    FETCH_PHASE2_PENDING,
    FETCH_PRECHECK_ONLY,
    PHASE2_COMPLETE,
    PHASE2_FAILED,
    PHASE2_PENDING,
    STRATUM_ACTIVE_AFTER,
    STRATUM_CANDIDATE,
    STRATUM_SILENT,
    STRATUM_UNAVAILABLE,
    Design,
    binomial_n_interval,
    design_for,
    drift_line,
    effective_rate,
    estimate_rate,
    estimate_total,
    format_estimate_lines,
    phase1_start,
    phase2_label,
    precheck,
    precheck_drift,
    sampling_summary,
    selection_draw,
    window_stratum,
)
from bot_farm_silence_tracker.models import Account, Activity
from bot_farm_silence_tracker.output import COLUMNS, SAMPLING_COLUMNS
from bot_farm_silence_tracker.pipeline import (
    ScanOutcome,
    ScanSettings,
    _drift_warning,
    rescore_run,
    run_scan,
)
from bot_farm_silence_tracker.scoring import METHOD_COLUMNS, AccountResult, ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import (
    AccountUnavailable,
    BudgetExceeded,
    NewestActivity,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.storage import SAMPLE_PRECHECKED, Storage

from .helpers import CUTOFF, NOW, at

SEEDS = SeedSet("fake", ("c1", "c2"))
PARAMS = ScoringParams(cutoff=CUTOFF, now=NOW)
SAMPLING_KEYS = set(SAMPLING_COLUMNS)
SALT = 11  # The --seed of settings(), which is the sampling salt.


class RecordingSource(FakeSource):
    """FakeSource that records every request and can change the world between runs.

    * ``stop_at``: the phase-2 fetch (``fetch_activity`` call) that raises
      BudgetExceeded instead; ``stop_pre_at`` likewise for existence queries.
    * ``gone``: accounts that are now suspended.
    * ``fail``: accounts whose full fetch fails with a per-account error.
    * ``interrupt``: accounts whose full fetch stops part-way (BudgetExceeded with the
      pages already fetched).
    * ``backfill``: extra activity timestamps the archive now has (seen by both the
      pre-check and the full fetch); ``fetch_backfill`` only by the full fetch.
    """

    def __init__(
        self,
        config: SourceConfig,
        *,
        stop_at: int | None = None,
        stop_pre_at: int | None = None,
    ) -> None:
        super().__init__(config)
        self.windows: list[tuple[str, datetime, datetime]] = []
        self.queries: list[tuple[str, str]] = []
        self.stop_at = stop_at
        self.stop_pre_at = stop_pre_at
        self.gone: set[str] = set()
        self.fail: set[str] = set()
        self.interrupt: set[str] = set()
        self.backfill: dict[str, list[datetime]] = {}
        self.fetch_backfill: dict[str, list[datetime]] = {}

    def _extra(self, account: Account, since: datetime, until: datetime, *maps: Any) -> list:
        stamps = [ts for m in maps for ts in m.get(account.user_id, ())]
        return [
            Activity("fake", account.user_id, ts, "comment", "c1", f"bf{ts.timestamp()}")
            for ts in stamps
            if since <= ts < until
        ]

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        if self.stop_at is not None and len(self.windows) >= self.stop_at:
            raise BudgetExceeded("cap")
        self.windows.append((account.user_id, since, until))
        if account.user_id in self.gone:
            raise AccountUnavailable("suspended")
        if account.user_id in self.fail:
            raise SourceError("boom")
        out = super().fetch_activity(account, since, until)
        out += self._extra(account, since, until, self.backfill, self.fetch_backfill)
        if account.user_id in self.interrupt:
            exc = BudgetExceeded("cap mid-account")
            exc.partial = sorted(out, key=lambda a: a.ts)[-3:]
            raise exc
        return out

    def newest_activity(
        self, account: Account, kind: str, since: datetime, until: datetime
    ) -> NewestActivity:
        if self.stop_pre_at is not None and len(self.queries) >= self.stop_pre_at:
            raise BudgetExceeded("cap")
        self.queries.append((account.user_id, kind))
        if account.user_id in self.gone:
            raise AccountUnavailable("suspended")
        found = super().newest_activity(account, kind, since, until)
        extra = [a.ts for a in self._extra(account, since, until, self.backfill)]
        if kind == "comment" and extra:
            return NewestActivity(max([*extra, *([found.newest] if found.newest else [])]), since)
        return found


def make_source(cls: type[FakeSource] = RecordingSource, concurrency: int = 1, **kw: Any) -> Any:
    config = SourceConfig(
        http=HttpClient(),
        credentials={},
        now=NOW,
        cutoff=CUTOFF,
        options={"population": 1500},
        concurrency=concurrency,
    )
    return cls(config, **kw)


def settings(rate: float | None = None, **kw: Any) -> ScanSettings:
    base: dict[str, Any] = {
        "platform": "fake",
        "scoring": PARAMS,
        "n": 300,
        "seed": SALT,
        "baseline_sample_rate": rate,
    }
    base.update(kw)
    return ScanSettings(**base)


def census_rows(outcome: ScanOutcome) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in r.to_row().items() if k not in SAMPLING_KEYS} for r in outcome.results
    ]


def scan(tmp_path: Path, rate: float | None, name: str = "c.db", **kw: Any) -> ScanOutcome:
    concurrency = kw.pop("concurrency", 1)
    source = kw.pop("source", None) or make_source(concurrency=concurrency)
    with Storage(tmp_path / name) as store:
        return run_scan(source, SEEDS, store, settings(rate, concurrency=concurrency, **kw))


def draw(user_id: str, salt: int = SALT) -> float:
    return selection_draw(salt, "fake", user_id)


def by_id(outcome: ScanOutcome) -> dict[str, AccountResult]:
    return {r.user_id: r for r in outcome.results}


# -- pre-check -------------------------------------------------------------------------


class Scripted(FakeSource):
    """Answers existence queries from a script: kind -> NewestActivity or exception."""

    def __init__(self, answers: dict[str, Any]) -> None:
        super().__init__(SourceConfig(http=HttpClient(), credentials={}, now=NOW, cutoff=CUTOFF))
        self.answers = answers
        self.asked: list[str] = []

    def newest_activity(
        self, account: Account, kind: str, since: datetime, until: datetime
    ) -> NewestActivity:
        self.asked.append(kind)
        answer = self.answers.get(kind, NewestActivity(None, since))
        if isinstance(answer, BaseException):
            raise answer
        return answer


START = phase1_start(PARAMS)


def found(days: float | None, since: datetime = START) -> NewestActivity:
    return NewestActivity(None if days is None else at(days), since)


@pytest.mark.parametrize(
    ("answers", "stratum", "asked", "complete"),
    [
        ({"comment": found(1.0)}, STRATUM_ACTIVE_AFTER, ["comment"], True),  # Posts skipped.
        ({"comment": found(0.0)}, STRATUM_ACTIVE_AFTER, ["comment"], True),  # At the cutoff.
        ({"comment": found(-1.0), "post": found(2.0)}, STRATUM_ACTIVE_AFTER, None, True),
        ({"comment": found(-1.0)}, STRATUM_CANDIDATE, None, True),
        ({"post": found(-1.99)}, STRATUM_CANDIDATE, None, True),  # Inside the 48 h window.
        ({}, STRATUM_SILENT, None, True),
        # Narrowed to the cutoff: nothing after it, but the window before is unknown.
        ({"comment": found(None, CUTOFF)}, STRATUM_CANDIDATE, None, False),
        ({"comment": SourceError("slow")}, STRATUM_CANDIDATE, None, False),
        ({"comment": SourceError("slow"), "post": found(1.0)}, STRATUM_ACTIVE_AFTER, None, False),
    ],
)
def test_precheck_strata(
    answers: dict[str, Any], stratum: str, asked: list[str] | None, complete: bool
) -> None:
    source = Scripted(answers)
    result = precheck(source, Account("fake", "fake_000001", "h"), PARAMS)
    assert result.stratum == stratum
    assert source.asked == (asked or ["comment", "post"])
    if stratum != STRATUM_ACTIVE_AFTER:
        assert result.complete is complete


def test_precheck_unavailable_and_fatal() -> None:
    source = Scripted({})
    gone = Account("fake", "u", "h", status="suspended")
    assert precheck(source, gone, PARAMS).stratum == STRATUM_UNAVAILABLE
    assert source.asked == []
    with pytest.raises(BudgetExceeded):
        precheck(Scripted({"comment": BudgetExceeded("cap")}), Account("fake", "u", "h"), PARAMS)
    with pytest.raises(AccountUnavailable):
        precheck(
            Scripted({"comment": AccountUnavailable("deleted")}), Account("fake", "u", "h"), PARAMS
        )


def test_window_stratum_and_phase1_start() -> None:
    assert window_stratum([at(-0.5), at(1.0)], PARAMS) == STRATUM_ACTIVE_AFTER
    assert window_stratum([at(-1.5)], PARAMS) == STRATUM_CANDIDATE
    assert window_stratum([at(-2.5)], PARAMS) == STRATUM_SILENT  # Before the window.
    assert window_stratum([], PARAMS) == STRATUM_SILENT
    assert phase1_start(PARAMS) == CUTOFF - timedelta(hours=48)


def test_design_probabilities() -> None:
    assert design_for(STRATUM_CANDIDATE, 0.2, 1, "fake", "u").inclusion_prob == 1.0
    assert design_for(STRATUM_CANDIDATE, 0.2, 1, "fake", "u").selected
    assert not design_for(STRATUM_UNAVAILABLE, 0.2, 1, "fake", "u").selected
    d = design_for(STRATUM_SILENT, 0.25, 3, "fake", "u7")
    assert d.draw == selection_draw(3, "fake", "u7") and d.selected == (d.draw < 0.25)
    picks = [design_for(STRATUM_SILENT, 0.25, 3, "fake", f"u{i}").selected for i in range(8000)]
    assert abs(sum(picks) / len(picks) - 0.25) < 0.02
    again = [design_for(STRATUM_SILENT, 0.25, 3, "fake", f"u{i}").selected for i in range(8000)]
    other = [design_for(STRATUM_SILENT, 0.25, 4, "fake", f"u{i}").selected for i in range(8000)]
    assert picks == again and picks != other


def test_effective_rate_follows_draw_order_not_completion_order() -> None:
    designs = {
        "a": Design("A", 0.5, True, 0.10),
        "b": Design("C", 0.5, True, 0.20),
        "c": Design("A", 0.5, True, 0.30),
        "d": Design("A", 0.5, True, 0.40),
        "x": Design("A", 0.5, False, 0.70),
        "k": Design("B", 1.0, True),
    }
    states = {"a": PHASE2_COMPLETE, "b": PHASE2_FAILED, "d": PHASE2_COMPLETE, "k": PHASE2_PENDING}
    # "c" is pending: the failure of "b" does not stop the walk, the B account does not
    # count, and "d" finished out of order but lies above P_eff.
    p_eff = effective_rate(designs, states, 0.5)
    assert p_eff == 0.30
    assert phase2_label(designs["a"], PHASE2_COMPLETE, p_eff) == (None, 0.30)
    assert phase2_label(designs["b"], PHASE2_FAILED, p_eff) == (FETCH_PHASE2_FAILED, 0.30)
    assert phase2_label(designs["c"], None, p_eff) == (FETCH_PHASE2_PENDING, 0.30)
    assert phase2_label(designs["d"], PHASE2_COMPLETE, p_eff) == (FETCH_AFTER_STOP, 0.30)
    assert phase2_label(designs["x"], None, p_eff) == (FETCH_PRECHECK_ONLY, 0.30)
    assert phase2_label(designs["k"], PHASE2_PENDING, p_eff) == (FETCH_PHASE2_PENDING, 1.0)
    assert effective_rate(designs, {**states, "c": PHASE2_COMPLETE}, 0.5) == 0.5


# -- estimator -------------------------------------------------------------------------


Unit = tuple[str, bool, bool, bool]  # (stratum, eligible, cliff, active after the cutoff)


def synthetic_population() -> list[Unit]:
    """A fixed population: 175 eligible accounts, 20 cliffs."""
    pop: list[Unit] = []
    for stratum, count, eligible, cliffs, after in (
        ("B", 60, 30, 20, False),
        ("A", 350, 120, 0, True),
        ("C", 190, 25, 0, False),
        ("U", 20, 0, 0, False),
    ):
        pop += [(stratum, i < eligible, i < cliffs, after) for i in range(count)]
    return pop


TRUE_ELIGIBLE = 175
TRUE_SILENT = 10 + 25  # Eligible non-cliffs in B, plus eligible C.
TRUE_RATE = 20 / TRUE_ELIGIBLE


def simulated_summary(
    pop: list[Unit], rate: float, salt: int, states_of: Any = None
) -> dict[str, Any]:
    """Run the estimator on ``pop``. ``states_of(designs) -> {uid: state}`` decides how
    far phase 2 got; by default every selected account is complete."""
    designs = {f"u{i}": design_for(u[0], rate, salt, "fake", f"u{i}") for i, u in enumerate(pop)}
    if states_of is None:
        states = {uid: PHASE2_COMPLETE for uid, d in designs.items() if d.selected}
    else:
        states = states_of(designs)
    p_eff = effective_rate(designs, states, rate)
    results = []
    for i, (_, eligible, cliff, after) in enumerate(pop):
        uid = f"u{i}"
        design = designs[uid]
        fetch_status, prob = phase2_label(design, states.get(uid), p_eff)
        r = AccountResult(
            "fake",
            uid,
            f"h{i}",
            "active",
            posts_after_cutoff=int(after),
            stratum=design.stratum,
            inclusion_prob=prob,
            fetch_status=fetch_status,
        )
        # A cliff in A/C is a mover: its pre-check missed it (ingestion drift).
        moved = cliff and design.stratum in ("A", "C")
        if fetch_status is None:
            r.eligible, r.cliff, r.weight = eligible, cliff, 1 / prob
            r.moved_after_precheck = moved
        elif fetch_status == FETCH_PHASE2_FAILED:
            r.status = "error"
        elif fetch_status == FETCH_AFTER_STOP:
            r.moved_after_precheck, r.cliff_after_stop = moved, cliff
        results.append(r)
    return sampling_summary(results, rate, p_eff, PARAMS)


def coverage(summaries: list[dict[str, Any]]) -> dict[str, float]:
    hits = {"eligible": 0, "silent": 0, "rate": 0}
    for s in summaries:
        est = s["estimates"]
        lo, hi = est["eligible"]["ci95"]
        hits["eligible"] += lo <= TRUE_ELIGIBLE <= hi
        lo, hi = est["eligible_silent_not_cliff"]["ci95"]
        hits["silent"] += lo <= TRUE_SILENT <= hi
        lo, hi = est["cliff_rate"]["ci95"]
        hits["rate"] += lo <= TRUE_RATE <= hi
    return {k: v / len(summaries) for k, v in hits.items()}


def assert_unbiased(summaries: list[dict[str, Any]]) -> None:
    estimates = [s["estimates"]["eligible"]["estimate"] for s in summaries]
    mean = statistics.fmean(estimates)
    stderr = statistics.stdev(estimates) / len(estimates) ** 0.5
    assert abs(mean - TRUE_ELIGIBLE) < 3 * stderr + 0.5, (mean, stderr)


@pytest.mark.parametrize("rate", [0.15, 0.4])
def test_ht_estimates_are_unbiased_and_intervals_cover(rate: float) -> None:
    summaries = [simulated_summary(synthetic_population(), rate, salt) for salt in range(600)]
    for s in summaries:
        assert s["cliffs"] == 20 and s["cliffs_exact"]
        assert s["effective_sample_rate"] == rate
    assert_unbiased(summaries)
    for share in coverage(summaries).values():
        assert 0.93 <= share <= 0.99


def stop_mid_phase2(seed: int, *, concurrent: bool) -> Any:
    """Phase 2 stopped after a random number of the selected A/C accounts (in draw
    order); with ``concurrent``, the account right after the stop finished anyway."""

    def states_of(designs: dict[str, Design]) -> dict[str, str]:
        rng = random.Random(seed)
        ordered = sorted(
            (d.draw, uid) for uid, d in designs.items() if d.selected and d.draw is not None
        )
        k = rng.randint(len(ordered) // 5, 4 * len(ordered) // 5)
        states = {uid: PHASE2_COMPLETE for uid, d in designs.items() if d.stratum == "B"}
        for j, (_, uid) in enumerate(ordered):
            done = j < k or (concurrent and j == k + 1 and rng.random() < 0.5)
            states[uid] = PHASE2_COMPLETE if done else PHASE2_PENDING
        return states

    return states_of


@pytest.mark.parametrize("concurrent", [False, True])
def test_a_stop_mid_phase2_stays_unbiased_and_covers(concurrent: bool) -> None:
    rate = 0.4
    summaries = [
        simulated_summary(
            synthetic_population(), rate, salt, stop_mid_phase2(salt, concurrent=concurrent)
        )
        for salt in range(600)
    ]
    assert all(s["effective_sample_rate"] < rate for s in summaries)
    assert all(s["phase2_pending"] > 0 and s["cliffs_exact"] for s in summaries)
    if concurrent:
        assert any(s["after_stop"] for s in summaries)
    assert_unbiased(summaries)
    for share in coverage(summaries).values():
        assert 0.93 <= share <= 0.99


def budget_stop(pop: list[Unit], seed: int, *, eligible_cost: int = 20) -> Any:
    """A budget-driven stop whose cost depends on the outcome.

    The selected A/C accounts are fetched in draw order; an eligible account (a long
    history) costs ``eligible_cost``, any other 1. The budget is a random share of
    what phase 2 would cost; the account that crosses it is cut short (pending). Up to
    8 accounts after it were in flight on other workers: the cheap ones finished
    anyway (``after_stop``), the expensive ones were cut short too.
    """

    def states_of(designs: dict[str, Design]) -> dict[str, str]:
        rng = random.Random(seed)
        ordered = sorted(
            (d.draw, uid) for uid, d in designs.items() if d.selected and d.draw is not None
        )
        cost = [eligible_cost if pop[int(uid[1:])][1] else 1 for _, uid in ordered]
        budget = rng.uniform(0.2, 0.8) * sum(cost)
        states = {uid: PHASE2_COMPLETE for uid, d in designs.items() if d.stratum == "B"}
        spent, stop = 0.0, len(ordered)
        for j, (_, uid) in enumerate(ordered):
            spent += cost[j]
            if spent > budget and stop == len(ordered):
                stop = j
            if j < stop:
                states[uid] = PHASE2_COMPLETE
            elif j == stop or j > stop + rng.randint(0, 8) or cost[j] > 1:
                states[uid] = PHASE2_PENDING
            else:
                states[uid] = PHASE2_COMPLETE  # In flight at the stop; finished anyway.
        return states

    return states_of


def test_an_outcome_dependent_budget_stop_stays_unbiased_and_covers() -> None:
    """The stop point depends on the outcomes (eligible accounts cost 20 times more)."""
    pop = synthetic_population()
    rate = 0.4
    reps = 2000
    summaries = [simulated_summary(pop, rate, salt, budget_stop(pop, salt)) for salt in range(reps)]
    assert all(s["effective_sample_rate"] < rate for s in summaries)
    assert sum(s["after_stop"] > 0 for s in summaries) > reps // 2
    estimates = [s["estimates"]["eligible"]["estimate"] for s in summaries]
    ses = [s["estimates"]["eligible"]["se"] for s in summaries]
    bias = statistics.fmean(estimates) - TRUE_ELIGIBLE
    # Bias against the typical standard error of one run: a stop that depends on the
    # outcome must not pull the estimate by a noticeable share of its own noise.
    assert abs(bias) / statistics.fmean(ses) < 0.1, (bias, statistics.fmean(ses))
    for share in coverage(summaries).values():
        assert 0.93 <= share <= 0.99


# Population with ingestion drift: 8 eligible C accounts gained activity just before
# the cutoff after the pre-check, so their phase-2 history makes them cliffs.
DRIFTED = 8


def drifted_population() -> list[Unit]:
    pop = synthetic_population()
    silent = [i for i, u in enumerate(pop) if u[0] == "C" and u[1]][:DRIFTED]
    for i in silent:
        pop[i] = ("C", True, True, False)
    return pop


@pytest.mark.parametrize("stopped", [False, True])
def test_ingestion_drift_keeps_the_estimates_unbiased(stopped: bool) -> None:
    """Sampled C accounts that phase-2 data moves to cliffs keep their weight 1/pi."""
    pop = drifted_population()
    true_cliffs = 20 + DRIFTED
    true_silent = TRUE_SILENT - DRIFTED
    rate = 0.4
    summaries = [
        simulated_summary(
            pop, rate, salt, stop_mid_phase2(salt, concurrent=True) if stopped else None
        )
        for salt in range(800)
    ]
    moved = [s for s in summaries if s["cliffs_sampled"]]
    assert len(moved) > len(summaries) // 2
    assert all(not s["cliffs_exact"] for s in moved)
    if stopped:
        assert any(s["after_stop"] for s in summaries)
    for key, truth in (
        ("cliffs", true_cliffs),
        ("eligible", TRUE_ELIGIBLE),
        ("eligible_silent_not_cliff", true_silent),
    ):
        values = [s["estimates"][key]["estimate"] for s in summaries]
        stderr = statistics.stdev(values) / len(values) ** 0.5
        assert abs(statistics.fmean(values) - truth) < 3 * stderr + 0.05, key
        # Coverage among the runs that saw the drift: a run whose sample holds none of
        # the moved accounts cannot know of them (the intervals are conditional on the
        # phase-1 classification), though its HT estimate still averages out.
        hits = sum(lo <= truth <= hi for lo, hi in (s["estimates"][key]["ci95"] for s in moved))
        assert 0.93 <= hits / len(moved) <= 1.0, key
    rates = [s["estimates"]["cliff_rate"] for s in summaries]
    truth = true_cliffs / TRUE_ELIGIBLE
    # A ratio estimator: unbiased to first order only, so the tolerance is a share of SE.
    mean_se = statistics.fmean(r["se"] for r in rates if r["se"])
    assert abs(statistics.fmean(r["estimate"] for r in rates) - truth) < 0.1 * mean_se
    moved_rates = [s["estimates"]["cliff_rate"]["ci95"] for s in moved]
    assert sum(lo <= truth <= hi for lo, hi in moved_rates) / len(moved) >= 0.95  # Bonferroni.
    # Treating the moved accounts as certainty units (pi = 1) would count each once.
    assert statistics.fmean(s["cliffs"] for s in summaries) < true_cliffs - 4


def test_nonresponse_widens_the_intervals_conservatively() -> None:
    """Failures that depend on the outcome (eligible accounts fail far more often)."""
    pop = synthetic_population()
    rate = 0.4

    def states_of(designs: dict[str, Design]) -> dict[str, str]:
        rng = random.Random(len(designs) + int(1e6 * next(iter(designs.values())).inclusion_prob))
        states = {}
        for uid, d in designs.items():
            if not d.selected:
                continue
            eligible = pop[int(uid[1:])][1]
            fails = d.stratum != "B" and rng.random() < (0.35 if eligible else 0.03)
            states[uid] = PHASE2_FAILED if fails else PHASE2_COMPLETE
        return states

    summaries = [simulated_summary(pop, rate, salt, states_of) for salt in range(400)]
    assert all(s["nonresponse"] > 0 for s in summaries)
    assert all(not s["estimates"]["eligible"]["complete"] for s in summaries)
    shares = coverage(summaries)
    assert shares["eligible"] >= 0.95 and shares["rate"] >= 0.95
    assert any("widened for incomplete fetches" in ln for ln in format_estimate_lines(summaries[0]))


def test_incomplete_candidates_make_the_cliff_count_a_lower_bound() -> None:
    pop = synthetic_population()

    def states_of(designs: dict[str, Design]) -> dict[str, str]:
        states = {uid: PHASE2_COMPLETE for uid, d in designs.items() if d.selected}
        states["u0"] = PHASE2_PENDING  # A cliff in B, not fetched yet.
        states["u1"] = PHASE2_FAILED  # Another one whose fetch failed.
        return states

    s = simulated_summary(pop, 0.4, 3, states_of)
    assert s["cliffs"] == 18 and not s["cliffs_exact"] and s["candidates_incomplete"] == 2
    lo, hi = s["estimates"]["eligible"]["ci95"]
    assert lo <= TRUE_ELIGIBLE <= hi
    rate = s["estimates"]["cliff_rate"]
    assert not rate["complete"] and rate["ci95"][1] >= 20 / hi
    lines = format_estimate_lines(s)
    assert any(ln.startswith("Cliff accounts (lower bound): 18; 2 cliff") for ln in lines)
    assert any("INCOMPLETE" in ln for ln in lines)
    assert not any("Cliff accounts (exact)" in ln for ln in lines)


def test_binomial_interval_edges() -> None:
    assert binomial_n_interval(0, 0.5, 40) == (0, 5)
    assert binomial_n_interval(7, 1.0, 40) == (7, 7)
    assert binomial_n_interval(3, 0.0, 40) == (3, 40)
    lo, hi = binomial_n_interval(10, 0.25, 1000)
    assert lo < 40 < hi
    assert binomial_n_interval(10, 0.25, 12)[1] == 12  # Clipped to what is possible.


# -- scans -----------------------------------------------------------------------------


@pytest.mark.parametrize("concurrency", [1, 2])
def test_rate_one_equals_census(tmp_path: Path, concurrency: int) -> None:
    census = scan(tmp_path, None, "census.db", concurrency=concurrency)
    full = scan(tmp_path, 1.0, "full.db", concurrency=concurrency)
    assert census_rows(full) == census_rows(census)
    keep = ("scored", "cliff", "clusters", "largest_cluster", "truncated")
    assert {k: full.summary[k] for k in keep} == {k: census.summary[k] for k in keep}
    assert full.summary["eligible_observed"] == census.summary["eligible"]
    est = full.summary["sampling"]["estimates"]
    assert est["eligible"]["ci95"] == [census.summary["eligible"]] * 2
    assert est["cliff_rate"]["estimate"] == pytest.approx(
        census.summary["cliff"] / census.summary["eligible"], abs=1e-5
    )
    assert "sampling" not in census.summary and "eligible_observed" not in census.summary
    assert all(r.stratum is None for r in census.results)


def test_two_phase_requests_and_strata(tmp_path: Path) -> None:
    source = make_source()
    out = scan(tmp_path, 0.3, source=source)
    census = scan(tmp_path, None, "census.db")
    truth = by_id(census)
    queries: dict[str, list[str]] = {}
    for uid, kind in source.queries:
        queries.setdefault(uid, []).append(kind)
    fetched = [uid for uid, _, _ in source.windows]
    # Phase 2 fetches exactly the census window, once per account, so the per-kind
    # item cap and truncation are the census's.
    assert all((s, u) == (PARAMS.baseline_start, NOW) for _, s, u in source.windows)
    assert len(fetched) == len(set(fetched))
    strata: dict[str, int] = {}
    for r in out.results:
        strata[r.stratum or "-"] = strata.get(r.stratum or "-", 0) + 1
        asked = queries.get(r.user_id, [])
        assert asked in (["comment"], ["comment", "post"]) or r.stratum == STRATUM_UNAVAILABLE
        if r.stratum == STRATUM_CANDIDATE:
            assert r.fetch_status is None and r.weight == 1.0 and r.user_id in fetched
        elif r.fetch_status == FETCH_PRECHECK_ONLY:
            assert r.user_id not in fetched and r.weight is None and not r.eligible
            assert not truth[r.user_id].cliff  # Ruled out correctly.
        elif r.stratum in ("A", "C"):
            assert r.weight == pytest.approx(1 / 0.3) and r.user_id in fetched
        if r.stratum == STRATUM_ACTIVE_AFTER and asked == ["comment"]:
            assert truth[r.user_id].posts_after_cutoff > 0
        if r.fetch_status is None:
            row = {k: v for k, v in r.to_row().items() if k not in SAMPLING_KEYS}
            assert row == truth[r.user_id].to_row()
    assert set(strata) >= {"A", "B", "C", "U"}
    # Phase 2 order: every B first, then the sampled A/C accounts by ascending draw.
    kinds = [by_id(out)[uid].stratum for uid in fetched]
    n_b = kinds.count("B")
    assert set(kinds[:n_b]) == {"B"} and "B" not in kinds[n_b:]
    draws = [draw(uid) for uid in fetched[n_b:]]
    assert draws == sorted(draws) and max(draws) < 0.3
    # Cliffs are exact; clustering only sees cliffs, so it matches the census.
    assert [r.user_id for r in out.candidates] == [r.user_id for r in census.candidates]
    assert [(r.cluster_id, r.cluster_size) for r in out.candidates] == [
        (r.cluster_id, r.cluster_size) for r in census.candidates
    ]
    sampled = [r for r in out.results if r.stratum in ("A", "C")]
    share = sum(r.fetch_status is None for r in sampled) / len(sampled)
    assert abs(share - 0.3) < 0.08
    assert out.summary["scored"] == sum(r.fetch_status is None for r in out.results)
    # Explicit keys: no unweighted count hides under the census key.
    s = out.summary
    assert s["eligible"] is None
    assert s["eligible_observed"] == sum(r.eligible for r in out.results)
    assert s["eligible_estimate"] == s["sampling"]["estimates"]["eligible"]["estimate"]
    assert s["eligible_ci95"] == s["sampling"]["estimates"]["eligible"]["ci95"]
    assert s["cliff_exact"] is True and s["sampling"]["effective_sample_rate"] == 0.3


def test_seed_stable_selection(tmp_path: Path) -> None:
    def chosen(out: ScanOutcome) -> set[str]:
        return {r.user_id for r in out.results if r.stratum in ("A", "C") and r.weight}

    one = scan(tmp_path, 0.4, "one.db")
    two = scan(tmp_path, 0.4, "two.db")
    three = scan(tmp_path, 0.4, "three.db", seed=12)
    assert chosen(one) == chosen(two)
    assert chosen(one) != chosen(three)


def test_concurrency_two_matches_sequential(tmp_path: Path) -> None:
    seq = scan(tmp_path, 0.5, "seq.db")
    par = scan(tmp_path, 0.5, "par.db", concurrency=2)
    assert [r.to_row() for r in par.results] == [r.to_row() for r in seq.results]
    assert par.summary["sampling"] == seq.summary["sampling"]


@pytest.mark.parametrize("rate", [0.5, None])
def test_a_concurrent_resume_of_cached_accounts_finishes(tmp_path: Path, rate: Any) -> None:
    """--concurrency 2 resumes whose accounts all come from the run chain or the cache.

    Every account a pass starts is recorded at once, so no worker is running when the
    loop looks for one; this used to fail an assertion (for census runs too).
    """
    with Storage(tmp_path / "c.db") as store:
        first = run_scan(make_source(concurrency=2), SEEDS, store, settings(rate, concurrency=2))
        assert first.status == "complete"
        source = make_source(concurrency=2)
        second = run_scan(
            source, SEEDS, store, settings(rate, concurrency=2), from_run=first.run_id
        )
        assert second.status == "complete"
        assert source.windows == [] and source.queries == []  # Nothing was asked again.
        assert [r.to_row() for r in second.results] == [r.to_row() for r in first.results]
        if rate is not None:
            assert second.summary["sampling"] == first.summary["sampling"]


def check_p_eff(out: ScanOutcome) -> float:
    """P_eff is the draw of the first selected A/C account, in draw order, that is not
    done; everything below it is observed and nothing at or above it is."""
    p_eff = out.summary["sampling"]["effective_sample_rate"]
    selected = sorted(
        (draw(r.user_id), r)
        for r in out.results
        if r.stratum in ("A", "C") and r.fetch_status != FETCH_PRECHECK_ONLY
    )
    first_pending = next(d for d, r in selected if r.fetch_status == FETCH_PHASE2_PENDING)
    assert p_eff == pytest.approx(first_pending, abs=1e-6)
    for d, r in selected:
        if d < p_eff:
            assert r.fetch_status is None and r.weight == pytest.approx(1 / p_eff, rel=1e-4)
        else:
            assert r.fetch_status in (FETCH_PHASE2_PENDING, FETCH_AFTER_STOP)
            assert r.weight is None
    assert all(
        r.inclusion_prob == pytest.approx(p_eff, abs=1e-6)
        for r in out.results
        if r.stratum in ("A", "C")
    )
    return p_eff


@pytest.mark.parametrize("concurrency", [1, 2])
def test_a_stop_in_phase2_keeps_the_frame_with_p_eff(tmp_path: Path, concurrency: int) -> None:
    full = scan(tmp_path, 0.4, "full.db")
    n_b = full.summary["sampling"]["strata"]["B"]["full_fetch"]
    source = make_source(concurrency=concurrency, stop_at=n_b + 12)
    out = scan(tmp_path, 0.4, "stop.db", source=source, concurrency=concurrency)
    assert out.status == "partial"
    p_eff = check_p_eff(out)
    assert 0 < p_eff < 0.4
    s = out.summary["sampling"]
    # Nothing is dropped: every pre-checked account is in the frame.
    assert s["frame"] == full.summary["sampling"]["frame"]
    assert s["cliffs_exact"] and s["cliffs"] == full.summary["sampling"]["cliffs"]
    observed = sum(r.fetch_status is None for r in out.results if r.stratum in ("A", "C"))
    assert observed <= 12
    if concurrency == 1:
        assert observed == 12 and s["after_stop"] == 0


def test_an_interrupted_phase2_fetch_is_pending(tmp_path: Path) -> None:
    full = scan(tmp_path, 0.4, "full.db")
    target = min(
        (r for r in full.results if r.stratum == "A" and r.fetch_status is None),
        key=lambda r: draw(r.user_id),
    )
    source = make_source()
    source.interrupt = {target.user_id}
    out = scan(tmp_path, 0.4, "cut.db", source=source)
    assert out.status == "partial"
    result = by_id(out)[target.user_id]
    assert result.fetch_status == FETCH_PHASE2_PENDING and result.weight is None
    # Counted as complete, P_eff would be the next account's draw.
    assert out.summary["sampling"]["effective_sample_rate"] == pytest.approx(
        draw(target.user_id), abs=1e-6
    )


def test_a_stop_among_the_candidates_gives_bounds_only(tmp_path: Path) -> None:
    out = scan(tmp_path, 0.4, source=make_source(stop_at=3))
    s = out.summary["sampling"]
    selected = [
        draw(r.user_id)
        for r in out.results
        if r.stratum in ("A", "C") and r.fetch_status != FETCH_PRECHECK_ONLY
    ]
    # No A/C account was reached: P_eff is the lowest draw, an empty Bernoulli sample.
    assert s["effective_sample_rate"] == pytest.approx(min(selected), abs=1e-6)
    assert not any(r.fetch_status is None for r in out.results if r.stratum in ("A", "C"))
    assert s["candidates_incomplete"] > 0
    assert not s["cliffs_exact"] and out.summary["cliff_exact"] is False
    assert not s["estimates"]["eligible"]["complete"]
    assert s["estimates"]["eligible"]["estimate"] is None  # Nothing drawn yet.
    assert s["estimates"]["cliff_rate"] is None
    lines = format_estimate_lines(s)
    assert any("Cliff accounts (lower bound)" in ln for ln in lines)
    assert any("Effective sample rate" in ln for ln in lines)
    assert any("not estimable yet" in ln for ln in lines)


def test_no_sample_at_all_is_not_estimable() -> None:
    est = estimate_total(5, 0, 40, 0.0, certain_incomplete=2)
    assert est["estimate"] is None and est["ci95"] == [5, 47] and not est["complete"]
    assert estimate_rate(3, est) is None
    sampling = {"baseline_sample_rate": 0.2, "estimates": {"eligible": est, "cliff_rate": None}}
    assert any("not estimable yet" in ln for ln in format_estimate_lines(sampling))


def test_failed_phase2_fetches_are_nonresponse_and_retried(tmp_path: Path) -> None:
    full = scan(tmp_path, 0.4, "full.db")
    picks = sorted(
        (r for r in full.results if r.stratum == "A" and r.fetch_status is None),
        key=lambda r: draw(r.user_id),
    )[:2]
    cand = next(r for r in full.results if r.stratum == "B" and r.cliff)
    db = tmp_path / "chain.db"
    with Storage(db) as store:
        source = make_source()
        source.fail = {cand.user_id, *(r.user_id for r in picks)}
        first = run_scan(source, SEEDS, store, settings(0.4))
        s = first.summary["sampling"]
        assert s["nonresponse"] == 2 and s["phase2_failed"] == 3
        assert s["candidates_incomplete"] == 1 and not s["cliffs_exact"]
        assert s["effective_sample_rate"] == 0.4  # Failures do not lower P_eff.
        assert by_id(first)[cand.user_id].fetch_status == FETCH_PHASE2_FAILED
        assert first.summary["errors"] == 3
        lo, hi = s["estimates"]["eligible"]["ci95"]
        full_lo, full_hi = full.summary["sampling"]["estimates"]["eligible"]["ci95"]
        assert lo <= full_lo and hi >= full_hi  # Widened, never narrowed.
        second = run_scan(make_source(), SEEDS, store, settings(0.4), from_run=first.run_id)
        assert [r.to_row() for r in second.results] == [r.to_row() for r in full.results]
        assert second.summary["sampling"] == full.summary["sampling"]


def test_resume_keeps_the_design_and_report_rescores(tmp_path: Path) -> None:
    full = scan(tmp_path, 0.3, "full.db")
    db = tmp_path / "chain.db"
    with Storage(db) as store:
        first = run_scan(make_source(stop_at=40), SEEDS, store, settings(0.3))
        assert first.status == "partial"
        assert first.summary["sampling"]["phase2_pending"] > 0
        assert any(r.fetch_status == FETCH_PHASE2_PENDING for r in first.results)
        assert any(s == SAMPLE_PRECHECKED for _, s in store.get_run_sample(first.run_id))
        with pytest.raises(ValueError, match="differs"):
            run_scan(make_source(), SEEDS, store, settings(0.5), from_run=first.run_id)
        with pytest.raises(ValueError, match="differs"):
            run_scan(make_source(), SEEDS, store, settings(None), from_run=first.run_id)
        source = make_source()
        second = run_scan(source, SEEDS, store, settings(0.3, seed=None), from_run=first.run_id)
        assert second.status == "complete"
        assert source.queries == []  # No pre-check was repeated.
        fetched_first = 40
        n_phase2 = sum(r.fetch_status is None for r in full.results if r.stratum in "ABC")
        assert len(source.windows) == n_phase2 - fetched_first  # The rest of phase 2 only.
        assert [r.to_row() for r in second.results] == [r.to_row() for r in full.results]
        assert second.summary["sampling"] == full.summary["sampling"]
        params = store.get_run(second.run_id)["params"]
        assert params["baseline_sample_rate"] == 0.3 and params["sampling_salt"] == SALT

        report = rescore_run(store, second.run_id)
        assert [r.to_row() for r in report.results] == [r.to_row() for r in second.results]
        assert report.summary["sampling"] == second.summary["sampling"]
        # The stopped run re-scores to the same P_eff and frame too.
        again = rescore_run(store, first.run_id)
        assert again.summary["sampling"] == first.summary["sampling"]
        narrower = rescore_run(store, second.run_id, cliff_window_hours=24.0)
        assert narrower.summary["sampling"]["cliffs"] <= second.summary["cliff"]
        with pytest.raises(ValueError, match="cannot be raised"):
            rescore_run(store, second.run_id, cliff_window_hours=72.0)


def test_a_stop_in_phase1_resumes_the_precheck(tmp_path: Path) -> None:
    full = scan(tmp_path, 0.3, "full.db")
    with Storage(tmp_path / "chain.db") as store:
        first = run_scan(make_source(stop_pre_at=100), SEEDS, store, settings(0.3))
        assert first.status == "partial"
        source = make_source()
        second = run_scan(source, SEEDS, store, settings(0.3), from_run=first.run_id)
        asked = {uid for uid, _ in source.queries}
        assert asked.isdisjoint({r.user_id for r in first.results})
        assert second.summary["sampling"] == full.summary["sampling"]


# -- fresh data wins on a resume (critic scenarios) --------------------------------------


def test_resume_a_candidate_now_suspended_becomes_u(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = run_scan(make_source(stop_at=0), SEEDS, store, settings(0.3))
        target = next(r for r in first.results if r.stratum == STRATUM_CANDIDATE)
        assert target.fetch_status == FETCH_PHASE2_PENDING
        source = make_source()
        source.gone = {target.user_id}
        second = run_scan(source, SEEDS, store, settings(0.3), from_run=first.run_id)
        result = by_id(second)[target.user_id]
        assert result.status == "suspended" and result.stratum == STRATUM_UNAVAILABLE
        assert result.fetch_status is None and result.inclusion_prob == 1.0
        assert store.get_run_design(second.run_id)[target.user_id][0] == STRATUM_UNAVAILABLE
        assert second.summary["sampling"]["candidates_incomplete"] == 0
        assert second.summary["sampling"]["cliffs_exact"]
        report = rescore_run(store, second.run_id)
        assert by_id(report)[target.user_id].stratum == STRATUM_UNAVAILABLE


def pick_silent(out: ScanOutcome, *, selected: bool) -> AccountResult:
    want = None if selected else FETCH_PRECHECK_ONLY
    return next(
        r
        for r in out.results
        if r.stratum == STRATUM_SILENT and r.fetch_status == want and r.status == "active"
    )


def test_resume_refresh_reclassifies_a_backfilled_c_as_certain_b(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = run_scan(make_source(), SEEDS, store, settings(0.3))
        target = pick_silent(first, selected=False)
        backfill = {target.user_id: [CUTOFF - timedelta(hours=2)]}
        # Without --refresh the stored stratum is reused unchanged: no query is sent.
        quiet = make_source()
        quiet.backfill = backfill
        kept = run_scan(quiet, SEEDS, store, settings(0.3), from_run=first.run_id)
        assert quiet.queries == []
        assert by_id(kept)[target.user_id].fetch_status == FETCH_PRECHECK_ONLY
        # A refreshed pre-check sees the new rows: a new B, forced into phase 2 at pi 1.
        fresh = make_source()
        fresh.backfill = backfill
        second = run_scan(fresh, SEEDS, store, settings(0.3, refresh=True), from_run=kept.run_id)
        result = by_id(second)[target.user_id]
        assert result.stratum == STRATUM_CANDIDATE and result.fetch_status is None
        assert result.inclusion_prob == 1.0 and result.weight == 1.0
        assert target.user_id in {uid for uid, _, _ in fresh.windows}


def test_phase2_data_moving_a_sampled_c_to_a_cliff_keeps_its_weight(tmp_path: Path) -> None:
    """Data that changed after the pre-check never turns a sampled unit into pi = 1."""
    probe = scan(tmp_path, 0.5, "probe.db")
    target = next(
        r
        for r in probe.results
        if r.stratum == STRATUM_SILENT and r.fetch_status is None and r.eligible
    )
    source = make_source()
    source.fetch_backfill = {target.user_id: [CUTOFF - timedelta(hours=2)]}
    with Storage(tmp_path / "c.db") as store:
        out = run_scan(source, SEEDS, store, settings(0.5))
        result = by_id(out)[target.user_id]
        assert result.cliff and result.stratum == STRATUM_SILENT
        assert result.inclusion_prob == 0.5 and result.weight == 2.0
        assert result.fetch_status is None
        assert store.get_run_design(out.run_id)[target.user_id][:2] == (STRATUM_SILENT, 0.5)
        s = out.summary["sampling"]
        assert s["cliffs_sampled"] == 1 and not s["cliffs_exact"]
        assert s["cliffs"] == probe.summary["sampling"]["cliffs"] + 1
        cliffs = s["estimates"]["cliffs"]
        assert cliffs["estimate"] == pytest.approx(s["cliffs"] - 1 + 2.0)
        assert cliffs["ci95"][0] <= s["cliffs"] - 1 + 1 <= cliffs["ci95"][1]
        assert out.summary["cliff_exact"] is False
        assert any("cliff-candidate pattern" in w for w in out.warnings)
        lines = format_estimate_lines(s)
        assert any(ln.startswith("Cliff accounts (estimate):") for ln in lines)
        assert not any("Cliff accounts (exact)" in ln for ln in lines)
        report = rescore_run(store, out.run_id)
        assert report.summary["sampling"] == out.summary["sampling"]


def mover_result(uid: str, stratum: str, fetch_status: str | None, prob: float, **kw: Any) -> Any:
    r = AccountResult(
        "fake",
        uid,
        f"h-{uid}",
        "active",
        stratum=stratum,
        inclusion_prob=prob,
        fetch_status=fetch_status,
    )
    if fetch_status is None:
        r.weight = 1 / prob
    for key, value in kw.items():
        setattr(r, key, value)
    return r


def frame(movers: list[AccountResult], *, sampled: int, unsampled: int, p: float) -> list[Any]:
    """Two certain cliffs in B, ``sampled`` fully fetched C accounts (non-movers),
    ``unsampled`` pre-check-only C accounts, plus ``movers``."""
    out = [mover_result(f"b{i}", "B", None, 1.0, eligible=True, cliff=True) for i in range(2)]
    out += [mover_result(f"s{i}", "C", None, p, eligible=True) for i in range(sampled)]
    out += [mover_result(f"u{i}", "C", FETCH_PRECHECK_ONLY, p) for i in range(unsampled)]
    return out + movers


def test_a_mover_that_is_not_a_cliff_still_makes_the_cliff_count_an_estimate() -> None:
    p = 0.25
    mover = mover_result("m", "C", None, p, moved_after_precheck=True)  # Not eligible.
    s = sampling_summary(frame([mover], sampled=9, unsampled=30, p=p), p, p, PARAMS)
    assert s["cliffs"] == 2 and s["cliffs_sampled"] == 0
    assert not s["cliffs_exact"]
    cliffs = s["estimates"]["cliffs"]
    # The HT interval with m = 0: the unsampled C accounts may hold cliffs too.
    assert cliffs == estimate_total(2, 0, 30, p, responded=10, failed=0, certain_incomplete=0)
    assert cliffs["ci95"][0] == 2 and cliffs["ci95"][1] > 2
    assert s["estimates"]["cliff_rate"]["ci95"][1] > 2 / s["estimates"]["eligible"]["estimate"]
    assert s["precheck_drift"] == {
        "moved_sampled": 1,
        "moved_after_stop": 0,
        "responded": 10,
        "unseen_upper95": binomial_n_interval(1, p, 31)[1] - 1,
        "unseen": 30,
    }
    lines = format_estimate_lines(s)
    assert any(ln.startswith("Cliff accounts (estimate): 2 (95% CI 2 to") for ln in lines)
    assert not any("exact for the pre-check snapshot" in ln for ln in lines)


def test_an_after_stop_mover_alone_makes_the_cliff_count_an_estimate() -> None:
    """A mover fetched past the stop point is not in the sample, but it is seen: the
    count is estimated, and its cliff raises the lower bound."""
    p = 0.25
    after = mover_result(
        "m", "C", FETCH_AFTER_STOP, p, moved_after_precheck=True, cliff_after_stop=True
    )
    s = sampling_summary(frame([after], sampled=10, unsampled=30, p=p), 0.5, p, PARAMS)
    assert not s["cliffs_exact"] and s["cliffs_after_stop"] == 1
    assert s["precheck_drift"]["moved_after_stop"] == 1
    assert s["precheck_drift"]["moved_sampled"] == 0
    lo, hi = s["estimates"]["cliffs"]["ci95"]
    assert lo == 3 and hi >= 3  # Two B cliffs plus the known after-stop cliff.
    line = drift_line(s)
    assert line is not None and "1 accounts fetched past the stop point disagreed" in line
    est_line = next(ln for ln in format_estimate_lines(s) if ln.startswith("Cliff accounts (est"))
    assert "1 A/C accounts fetched past the stop point disagreed with their pre-check" in est_line
    assert "sampled A/C accounts disagreed" not in est_line


def test_the_known_cliff_floor_also_lifts_a_narrow_upper_bound() -> None:
    p = 0.9
    afters = [
        mover_result(
            f"m{i}", "C", FETCH_AFTER_STOP, p, moved_after_precheck=True, cliff_after_stop=True
        )
        for i in range(3)
    ]
    s = sampling_summary(frame(afters, sampled=20, unsampled=0, p=p), 1.0, p, PARAMS)
    lo, hi = s["estimates"]["cliffs"]["ci95"]
    assert lo == 5 and hi >= 5


def test_the_known_floor_is_named_in_the_estimate_line_and_the_estimate_is_kept() -> None:
    p = 0.9
    afters = [
        mover_result(
            f"m{i}", "C", FETCH_AFTER_STOP, p, moved_after_precheck=True, cliff_after_stop=True
        )
        for i in range(3)
    ]
    s = sampling_summary(frame(afters, sampled=20, unsampled=0, p=p), 1.0, p, PARAMS)
    cliffs = s["estimates"]["cliffs"]
    # Unbiased HT estimate: B cliffs only, so it sits below its raised lower bound.
    assert cliffs["estimate"] == 2 and cliffs["ci95"][0] == 5
    line = next(ln for ln in format_estimate_lines(s) if ln.startswith("Cliff accounts (estim"))
    assert "lower bound includes 3 cliff accounts fetched past the stop point" in line
    # No floor applied: no sentence.
    plain = sampling_summary(frame([], sampled=20, unsampled=0, p=1.0), 1.0, 1.0, PARAMS)
    assert not any("fetched past the stop point" in ln for ln in format_estimate_lines(plain))


def test_unseen_upper95_is_at_least_the_after_stop_movers_known_among_the_unsampled() -> None:
    drift = precheck_drift(0, 3, 20, 0, 6, 0.85)
    assert binomial_n_interval(0, 0.85, 6)[1] < 3  # The computed bound alone contradicts.
    assert drift["unseen_upper95"] == 3
    line = drift_line({"precheck_drift": drift})
    assert line is not None and "up to 3 unsampled accounts" in line


def test_the_estimate_line_lists_sampled_and_after_stop_movers_apart() -> None:
    p = 0.25
    movers = [
        mover_result("m", "C", None, p, moved_after_precheck=True),
        mover_result("a", "C", FETCH_AFTER_STOP, p, moved_after_precheck=True),
        mover_result("b", "C", FETCH_AFTER_STOP, p, moved_after_precheck=True),
    ]
    s = sampling_summary(frame(movers, sampled=9, unsampled=30, p=p), 0.5, p, PARAMS)
    line = next(ln for ln in format_estimate_lines(s) if ln.startswith("Cliff accounts (est"))
    assert (
        "1 sampled A/C accounts and 2 A/C accounts fetched past the stop point disagreed "
        "with their pre-check"
    ) in line


def test_the_pipeline_drift_warning_counts_sampled_and_after_stop_movers_apart() -> None:
    p = 0.5
    both = [
        mover_result("a", "C", None, p, moved_after_precheck=True),
        mover_result("b", "A", FETCH_AFTER_STOP, p, moved_after_precheck=True),
        mover_result("c", "A", FETCH_AFTER_STOP, p, moved_after_precheck=True),
    ]
    warnings: list[str] = []
    _drift_warning(both, warnings)
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "1 sampled A/C accounts and 2 A/C accounts fetched past the stop point showed"
    )
    only_after: list[str] = []
    _drift_warning(both[1:], only_after)
    assert only_after[0].startswith("2 A/C accounts fetched past the stop point showed")
    assert "sampled A/C" not in only_after[0]


def test_the_drift_line_bounds_unseen_movers_with_k_zero_at_p_015() -> None:
    p = 0.15
    s = sampling_summary(frame([], sampled=40, unsampled=200, p=p), p, p, PARAMS)
    assert s["cliffs_exact"]  # No mover seen: exact for the pre-check snapshot.
    drift = s["precheck_drift"]
    # 0.85 ** 22 > 0.025 >= 0.85 ** 23: up to 22 movers could hide among the unsampled.
    assert drift == {
        "moved_sampled": 0,
        "moved_after_stop": 0,
        "responded": 40,
        "unseen_upper95": 22,
        "unseen": 200,
    }
    lines = format_estimate_lines(s)
    assert "Cliff accounts (exact for the pre-check snapshot): 2" in lines
    assert (
        "0 of 40 sampled A/C accounts disagreed with their pre-check; up to 22 unsampled "
        "accounts may also have changed (95%), so the cliff count could be low by at most 22."
    ) in lines
    # Capped by the number of unsampled accounts.
    small = sampling_summary(frame([], sampled=40, unsampled=5, p=p), p, p, PARAMS)
    assert small["precheck_drift"]["unseen_upper95"] == 5
    # Every A/C account seen: no line.
    census = sampling_summary(frame([], sampled=40, unsampled=0, p=1.0), 1.0, 1.0, PARAMS)
    assert census["precheck_drift"]["unseen_upper95"] == 0 and drift_line(census) is None


def test_the_drift_line_in_the_critics_scenario() -> None:
    """Drifted population at P = 0.15, a run whose sample saw none of the 8 movers."""
    pop = drifted_population()
    for salt in range(400):
        s = simulated_summary(pop, 0.15, salt)
        if s["precheck_drift"]["moved_sampled"] == 0:
            break
    else:
        pytest.fail("no run without a seen mover")
    assert s["cliffs_exact"] and s["cliffs"] == 20  # Low by the 8 unseen movers.
    assert s["precheck_drift"]["unseen_upper95"] == 22  # The line says so: 8 <= 22.
    assert any("could be low by at most 22" in ln for ln in format_estimate_lines(s))


def test_drift_bound_covers_the_unseen_movers() -> None:
    pop = drifted_population()
    true_cliffs = 20 + DRIFTED
    for rate in (0.15, 0.4):
        rows = [simulated_summary(pop, rate, salt) for salt in range(300)]
        covered = sum(
            true_cliffs
            <= s["estimates"]["cliffs"]["ci95"][1] + s["precheck_drift"]["unseen_upper95"]
            for s in rows
        )
        assert covered / len(rows) >= 0.95, rate


def test_a_moved_non_cliff_in_a_scan_and_its_report(tmp_path: Path) -> None:
    """A sampled C account the pre-check called silent, not eligible, whose phase-2 data
    shows activity just before the cutoff: a mover, not a cliff."""
    probe = scan(tmp_path, 0.5, "probe.db")
    target = next(
        r
        for r in probe.results
        if r.stratum == STRATUM_SILENT
        and r.fetch_status is None
        and not r.eligible
        and r.status == "active"
    )
    source = make_source()
    source.fetch_backfill = {target.user_id: [CUTOFF - timedelta(hours=2)]}
    with Storage(tmp_path / "c.db") as store:
        out = run_scan(source, SEEDS, store, settings(0.5))
        result = by_id(out)[target.user_id]
        assert result.moved_after_precheck and not result.cliff
        s = out.summary["sampling"]
        assert s["cliffs_sampled"] == 0 and not s["cliffs_exact"]
        assert s["precheck_drift"]["moved_sampled"] == 1
        assert out.summary["cliff_exact"] is False
        warning = next(w for w in out.warnings if "cliff-candidate pattern" in w)
        assert "whether or not these accounts are cliffs" in warning
        assert warning.startswith("1 sampled A/C accounts showed")
        assert "past the stop point" not in warning
        assert "any cliffs among them are estimated" not in warning
        # report recomputes the flag from the cache: same block, warning and lines.
        stored = store.get_run(out.run_id)["summary"]
        stored["warnings"] = [w for w in stored["warnings"] if w != warning]
        store._conn.execute(
            "UPDATE runs SET summary_json = ? WHERE run_id = ?", (json.dumps(stored), out.run_id)
        )
        store._conn.commit()
        report = rescore_run(store, out.run_id)
        assert by_id(report)[target.user_id].moved_after_precheck
        assert report.summary["sampling"] == s
        assert warning in report.warnings
        assert format_estimate_lines(report.summary["sampling"]) == format_estimate_lines(s)
        assert any("sampled A/C accounts disagreed" in ln for ln in format_estimate_lines(s))


def test_a_resume_warns_about_an_old_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_farm_silence_tracker.pipeline as pipeline

    with Storage(tmp_path / "c.db") as store:
        first = run_scan(make_source(stop_at=40), SEEDS, store, settings(0.3))
        fresh = run_scan(make_source(), SEEDS, store, settings(0.3), from_run=first.run_id)
        assert not any("reuses pre-checks" in w for w in fresh.warnings)
        real = pipeline.utcnow
        monkeypatch.setattr(pipeline, "utcnow", lambda: real() + timedelta(hours=49))
        late = run_scan(make_source(), SEEDS, store, settings(0.3), from_run=fresh.run_id)
        warning = next(w for w in late.warnings if "reuses pre-checks" in w)
        assert "more than 48 hours" in warning and "--refresh" in warning
        refreshed = run_scan(
            make_source(), SEEDS, store, settings(0.3, refresh=True), from_run=late.run_id
        )
        assert not any("reuses pre-checks" in w for w in refreshed.warnings)


class FlakyPrecheck(RecordingSource):
    """Existence queries for comments fail for the accounts in ``flaky``."""

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self.flaky: set[str] = set()

    def newest_activity(
        self, account: Account, kind: str, since: datetime, until: datetime
    ) -> NewestActivity:
        if kind == "comment" and account.user_id in self.flaky:
            self.queries.append((account.user_id, kind))
            raise SourceError("the archive kept timing out")
        return super().newest_activity(account, kind, since, until)


@pytest.mark.parametrize(("share", "loud"), [(0.3, False), (0.7, True)])
def test_many_incomplete_prechecks_are_warned_about_early(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, share: float, loud: bool
) -> None:
    probe = scan(tmp_path, 0.2, "probe.db")
    # Spread over the sample order, so every prefix has about the same share.
    ids = [r.user_id for r in probe.results]
    source = make_source(FlakyPrecheck)
    source.flaky = {uid for i, uid in enumerate(ids) if i % 10 < share * 10}
    with caplog.at_level("WARNING", logger="bot_farm_silence_tracker.pipeline"):
        out = scan(tmp_path, 0.2, source=source)
    assert out.status == "complete"  # Never aborted: incomplete pre-checks only cost.
    assert any("could not answer its whole window" in w for w in out.warnings)
    assert any(w.startswith("WARNING:") and "pre-checks were" in w for w in out.warnings) is loud
    early = [r for r in caplog.records if "could not be pre-checked in full" in r.getMessage()]
    assert len(early) == (1 if loud else 0)  # Logged once, as soon as the share is crossed.


def test_refuses_unsupported_source_and_bad_rate(tmp_path: Path) -> None:
    class NoSampling(FakeSource):
        supports_baseline_sampling = False

    with Storage(tmp_path / "c.db") as store:
        with pytest.raises(ValueError, match="does not apply"):
            run_scan(make_source(NoSampling), SEEDS, store, settings(0.5))
        with pytest.raises(ValueError, match=r"\(0, 1\]"):
            run_scan(make_source(), SEEDS, store, settings(1.5))


# -- CLI -------------------------------------------------------------------------------

runner = CliRunner()
NOW_ARG = "2026-10-14T00:00:00Z"


def cli_scan(tmp_path: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "scan",
            "--platform",
            "fake",
            "--n",
            "200",
            "--seed",
            "3",
            "--now",
            NOW_ARG,
            "--db",
            str(tmp_path / "c.db"),
            *extra,
        ],
    )


def read_csv(path: Path) -> tuple[list[str], list[str], list[dict[str, str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    comments = [ln for ln in lines if ln.startswith("#")]
    body = [ln for ln in lines if not ln.startswith("#")]
    reader = csv.DictReader(body)
    rows = list(reader)
    return comments, list(reader.fieldnames or []), rows


def test_cli_scan_and_report(tmp_path: Path) -> None:
    out = tmp_path / "s.csv"
    result = cli_scan(tmp_path, "--baseline-sample-rate", "0.4", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "Cliff rate among eligible (estimate)" in result.output
    assert "Eligible, fully fetched (unweighted" in result.output
    comments, header, rows = read_csv(out)
    assert header == [*COLUMNS, *SAMPLING_COLUMNS, *METHOD_COLUMNS]
    assert any("ESTIMATES" in c for c in comments)
    assert any("conditional on the phase-1 classification" in c for c in comments)
    assert any("95% CI" in c for c in comments)
    assert rows and all(r["stratum"] == "B" and float(r["weight"]) == 1.0 for r in rows)

    report_out = tmp_path / "r.json"
    result = runner.invoke(
        app, ["report", "--run-id", "1", "--db", str(tmp_path / "c.db"), "--out", str(report_out)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(report_out.read_text())
    summary = payload["summary"]
    assert summary["sampling"]["baseline_sample_rate"] == 0.4
    assert summary["eligible"] is None and isinstance(summary["eligible_observed"], int)
    assert summary["eligible_estimate"] == summary["sampling"]["estimates"]["eligible"]["estimate"]
    assert "nonresponse" in summary["sampling"]["ci_method"].lower()
    assert payload["estimates"] and payload["candidates"][0]["stratum"] == "B"
    result = runner.invoke(
        app,
        ["report", "--run-id", "1", "--db", str(tmp_path / "c.db"), "--cliff-window-hours", "96"],
    )
    assert result.exit_code == 2 and "cannot be raised" in result.output

    # A resume inherits the rate and refuses a different one, even with --force.
    result = cli_scan(tmp_path, "--from-run", "1", "--baseline-sample-rate", "0.5", "--force")
    assert result.exit_code == 2 and "keeps one rate" in result.output


def test_cli_census_output_unchanged_and_flag_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "c.csv"
    result = cli_scan(tmp_path, "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "estimate" not in result.output
    _, header, _ = read_csv(out)
    assert header == [*COLUMNS, *METHOD_COLUMNS]
    result = cli_scan(tmp_path, "--baseline-sample-rate", "0")
    assert result.exit_code == 2 and "(0, 1]" in result.output
    monkeypatch.setattr(FakeSource, "supports_baseline_sampling", False)
    result = cli_scan(tmp_path, "--baseline-sample-rate", "0.5")
    assert result.exit_code == 2 and "does not apply" in result.output
