"""The placebo comparison under a pure null, with discovery selection applied.

The fake source's churn world has no outage: Poisson posters that stop for good at a
constant rate. Discovery yields only accounts with activity in its window, the way
the real adapters discover. Every pass, real and placebo, draws its own sample through
:func:`bot_farm_silence_tracker.pipeline.draw_sample_pass`, the code a scan uses for
its placebo samples. Under the null the comparison may call the real cliff rate
"clearly above the placebo" only about as often as its 95% interval allows; a
synchronized stop at the real cutoff (the positive control) must be found.

The review's simulation showed that re-scoring the run's own sample at the placebo
cutoffs called every such null "clearly above" when discovery covered only the last
2 or 7 days.

The long version (200 replicates per configuration) runs with RUN_SLOW_TESTS=1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot_farm_silence_tracker import controls as C
from bot_farm_silence_tracker.pipeline import ScanSettings, draw_sample_pass, run_scan
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import SourceConfig
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.storage import Storage

CUT = datetime(2026, 10, 11, 0, 30, tzinfo=UTC)
SEEDS = SeedSet("fake", ("a", "b"))
#: Days from the cutoff to ``now``, and days of discovery before the cutoff.
OFFSETS = (3, 21)
WINDOWS = (2, 7, 30)
#: The false "clearly above" rate allowed for a 95% interval (one-sided, so the
#: nominal rate is about 2.5%).
MAX_FALSE_RATE = 0.07


def _source(seed: int, params: ScoringParams, window: int, **options: object) -> FakeSource:
    http = HttpClient(requests_per_second=1e6, burst=1e6, sleep=lambda _s: None)
    days_after = (params.now - params.cutoff).total_seconds() / 86400
    config = SourceConfig(
        http=http,
        credentials={},
        now=params.now,
        cutoff=params.cutoff,
        seed=seed,
        options={"world": "churn", "world_seed": seed, **options},
        baseline_start=params.baseline_start,
        # --active-within-days counts back from now; discovery then covers the last
        # ``window`` days before the cutoff.
        active_within_days=days_after + window,
    )
    return FakeSource(config)


def replicate(
    seed: int,
    offset: int,
    window: int,
    *,
    population: int = 800,
    n: int = 250,
    outage_share: float = 0.0,
) -> C.PlaceboComparison:
    """One null (or, with ``outage_share``, outage) world, compared as a scan compares."""
    params = ScoringParams(cutoff=CUT, now=CUT + timedelta(days=offset), placebo_weeks=3)
    source = _source(seed, params, window, population=population, outage_share=outage_share)
    settings = ScanSettings(platform="fake", scoring=params, n=n, seed=seed)
    real = draw_sample_pass(source, SEEDS, settings, params, 0, permutations=19)
    placebos = [
        draw_sample_pass(source, SEEDS, settings, params, k, permutations=19)
        for k in range(1, params.placebo_weeks + 1)
    ]
    return C.PlaceboComparison(
        params.placebo_weeks, accounts=real.accounts, real=real, placebos=placebos
    )


def false_rate(replicates: int, **kwargs: object) -> tuple[int, int]:
    hits = total = 0
    for offset in OFFSETS:
        for window in WINDOWS:
            for seed in range(replicates):
                comparison = replicate(1000 * offset + 10 * window + seed, offset, window, **kwargs)
                assert comparison.available, comparison.unavailable
                hits += comparison.clearly_above
                total += 1
    return hits, total


def test_the_churn_world_selects_by_activity_in_the_discovery_window() -> None:
    params = ScoringParams(cutoff=CUT, now=CUT + timedelta(days=3))
    source = _source(5, params, 2, population=300)
    start, end = CUT - timedelta(days=2), CUT
    found = list(source.discover(SEEDS, 1000))
    assert found
    for account in found:
        times = source._churn_account(int(account.user_id.removeprefix("fake_")))[1]
        assert any(start <= t < end for t in times)
    # Moved back a week, discovery selects by activity in the moved window.
    with source.shifted(timedelta(weeks=1), now=params.now - timedelta(weeks=1), seed=7):
        moved = list(source.discover(SEEDS, 1000))
    week = timedelta(weeks=1)
    for account in moved:
        times = source._churn_account(int(account.user_id.removeprefix("fake_")))[1]
        assert any(start - week <= t < end - week for t in times)
    assert {a.user_id for a in moved} != {a.user_id for a in found}


def test_a_pure_null_is_rarely_clearly_above_the_placebo() -> None:
    hits, total = false_rate(4)
    assert total == 24
    assert hits / total <= MAX_FALSE_RATE + 1 / total, f"{hits} of {total} nulls clearly above"


@pytest.mark.slow
def test_a_pure_null_is_rarely_clearly_above_the_placebo_long() -> None:
    hits, total = false_rate(200 // len(OFFSETS) // len(WINDOWS) + 1)
    assert hits / total <= MAX_FALSE_RATE, f"{hits} of {total} nulls clearly above"


def test_a_synchronized_permanent_stop_at_the_real_cutoff_is_detected() -> None:
    for seed, (offset, window) in enumerate(((3, 2), (3, 7), (21, 30))):
        comparison = replicate(seed, offset, window, outage_share=0.15)
        assert comparison.clearly_above, C.headline(comparison)


def test_a_fake_churn_scan_compares_with_placebo_samples_end_to_end(tmp_path: Path) -> None:
    outcomes = {}
    for share in (0.0, 0.2):
        params = ScoringParams(cutoff=CUT, now=CUT + timedelta(days=3), placebo_weeks=3)
        source = _source(11, params, 7, population=600, outage_share=share)
        settings = ScanSettings(
            platform="fake", scoring=params, n=200, seed=11, active_within_days=10
        )
        with Storage(tmp_path / f"null{share}.db") as storage:
            outcomes[share] = run_scan(source, SEEDS, storage, settings)
    null, outage = outcomes[0.0].placebo, outcomes[0.2].placebo
    assert null is not None and null.available and not null.clearly_above
    assert outage is not None and outage.available and outage.clearly_above
    assert null.requests > 0
    assert "NOT CLEARLY ABOVE THE PLACEBO" in C.headline(null)


def test_a_placebo_sample_sees_no_more_after_its_cutoff_than_the_run_does() -> None:
    # The run's own data ends at the source's horizon, three days before now. A
    # placebo sample's fetch must end at the horizon moved back too: data after it
    # would show resumes the run's own pass can never see.
    now = CUT + timedelta(days=6)
    params = ScoringParams(cutoff=CUT, now=now, horizon=now - timedelta(days=3), placebo_weeks=3)
    source = _source(3, params, 7, population=300)
    ends: list[datetime] = []
    fetch = source.fetch_activity

    def spy(account, since, until):  # type: ignore[no-untyped-def]
        ends.append(until)
        return fetch(account, since, until)

    source.fetch_activity = spy  # type: ignore[method-assign]
    settings = ScanSettings(platform="fake", scoring=params, n=50, seed=3)
    for k in (1, 2):
        ends.clear()
        draw_sample_pass(source, SEEDS, settings, params, k, permutations=19)
        assert ends
        assert max(ends) <= params.effective_now - timedelta(weeks=k)
