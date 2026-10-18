from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterator
from datetime import timedelta
from typing import ClassVar

import pytest

from bot_farm_silence_tracker.models import Account, Activity
from bot_farm_silence_tracker.sampling import reservoir_sample, sample_accounts
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import AccountUnavailable, Source, SourceConfig
from bot_farm_silence_tracker.sources.http import HttpClient

from .helpers import CUTOFF, NOW

SEEDS = SeedSet("fake", ("a",))


class ListSource(Source):
    """A source over a fixed list, to test the sampler in isolation."""

    name: ClassVar[str] = "fake"

    def __init__(self, ages_days: list[float | str | None]) -> None:
        super().__init__(SourceConfig(http=HttpClient(), credentials={}, now=NOW, cutoff=CUTOFF))
        self.ages = ages_days
        self.pool_limits: list[int] = []

    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        self.pool_limits.append(pool_limit)
        for i in range(len(self.ages)):
            yield Account("fake", f"u{i}", f"h{i}")
        yield Account("fake", "u0", "h0")  # A duplicate that must be dropped.

    def hydrate(self, account: Account) -> Account:
        age = self.ages[int(account.user_id[1:])]
        if age == "suspended":
            raise AccountUnavailable("suspended")
        if isinstance(age, float):
            account.created_at = NOW - timedelta(days=age)
        return account

    def fetch_activity(self, account: Account, since, until) -> list[Activity]:  # type: ignore[no-untyped-def]
        return []


def test_reservoir_uniformity() -> None:
    trials, population, k = 2000, 100, 10
    counts: Counter[int] = Counter()
    for seed in range(trials):
        counts.update(reservoir_sample(range(population), k, random.Random(seed)))
    expected = trials * k / population  # 200
    # Binomial sd is about 13; allow 5 sd.
    assert all(abs(counts[i] - expected) < 67 for i in range(population))
    assert sum(counts.values()) == trials * k


def test_reservoir_small_stream_returns_everything() -> None:
    assert sorted(reservoir_sample(range(3), 10, random.Random(0))) == [0, 1, 2]
    assert reservoir_sample(range(5), 0, random.Random(0)) == []


def test_sampling_is_deterministic_with_seed() -> None:
    src = ListSource([100.0] * 200)
    a = sample_accounts(src, SEEDS, n=20, seed=7, now=NOW)
    b = sample_accounts(src, SEEDS, n=20, seed=7, now=NOW)
    c = sample_accounts(src, SEEDS, n=20, seed=8, now=NOW)
    ids = lambda r: [x.user_id for x in r.accounts]  # noqa: E731
    assert ids(a) == ids(b)
    assert ids(a) != ids(c)
    assert len(set(ids(a))) == 20


def test_pool_limit_uses_factor_and_duplicates_are_dropped() -> None:
    src = ListSource([100.0] * 10)
    result = sample_accounts(src, SEEDS, n=4, pool_factor=2.5, seed=1, now=NOW)
    assert src.pool_limits == [10]
    assert result.pool_size == 10  # The duplicate of u0 is not counted twice.


def test_new_accounts_filter_hydrates_before_filtering() -> None:
    ages: list[float | str | None] = [1.0, 5.0, 30.0, 90.0, None, "suspended", 2.0]
    src = ListSource(ages)
    result = sample_accounts(src, SEEDS, n=10, new_accounts_days=10, seed=3, now=NOW)
    assert sorted(a.user_id for a in result.accounts) == ["u0", "u1", "u6"]
    assert all(a.created_at is not None for a in result.accounts)
    # u4 has an unknown age and u5 is suspended with no age: both are dropped.
    assert result.dropped_unknown_age == 2
    assert result.hydrated == 6


def test_new_accounts_combines_with_n() -> None:
    src = ListSource([1.0] * 50 + [200.0] * 50)
    result = sample_accounts(src, SEEDS, n=8, new_accounts_days=10, seed=3, now=NOW)
    assert len(result.accounts) == 8
    assert all(int(a.user_id[1:]) < 50 for a in result.accounts)


def test_invalid_arguments() -> None:
    src = ListSource([1.0])
    with pytest.raises(ValueError):
        sample_accounts(src, SEEDS, n=0, now=NOW)
    with pytest.raises(ValueError):
        sample_accounts(src, SEEDS, n=1, pool_factor=0.5, now=NOW)
