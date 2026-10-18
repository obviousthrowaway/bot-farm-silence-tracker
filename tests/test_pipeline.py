"""Pipeline resilience, resume from a stored sample, warnings, truncation and spend."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import Account, Activity
from bot_farm_silence_tracker.output import TRUNCATED_NOTICE, write_csv, write_json
from bot_farm_silence_tracker.pipeline import ScanSettings, load_run_sample, rescore_run, run_scan
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import (
    BudgetExceeded,
    RateLimited,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF, NOW

SEEDS = SeedSet("fake", ("c1", "c2"))
N = 30


class ScriptedSource(FakeSource):
    """FakeSource whose ``fetch_activity`` can fail on chosen calls."""

    def __init__(self, config: SourceConfig, fail: dict[int, Exception] | None = None) -> None:
        super().__init__(config)
        self.fail = fail or {}
        self.fetches: list[str] = []
        self.discover_calls = 0

    def archetype(self, index: int) -> str:
        # No unavailable accounts, so every sampled account reaches fetch_activity and
        # call numbers line up with sample positions.
        kind = super().archetype(index)
        return "human_irregular" if kind in ("suspended", "deleted", "private") else kind

    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        self.discover_calls += 1
        yield from super().discover(seeds, pool_limit)

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        call = len(self.fetches)
        self.fetches.append(account.user_id)
        error = self.fail.get(call)
        if error is not None:
            raise error
        return super().fetch_activity(account, since, until)


def make_source(
    storage: Storage | None = None, cls: type[FakeSource] = ScriptedSource, **kw: Any
) -> Any:
    config = SourceConfig(
        http=HttpClient(),
        credentials={},
        now=NOW,
        cutoff=CUTOFF,
        options={"population": 400},
        ledger=SpendLedger(storage, "fake") if storage is not None else None,
    )
    return cls(config, **kw) if kw else cls(config)


def settings(**kw: Any) -> ScanSettings:
    base: dict[str, Any] = {
        "platform": "fake",
        "scoring": ScoringParams(cutoff=CUTOFF, now=NOW),
        "n": N,
        "pool_factor": 1.0,
        "seed": 3,
    }
    base.update(kw)
    return ScanSettings(**base)


def test_per_account_errors_are_recorded_and_the_scan_continues(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = make_source(fail={2: SourceError("boom"), 5: SourceError("bang")})
        outcome = run_scan(src, SEEDS, store, settings())
        assert outcome.status == "complete"
        assert outcome.summary["errors"] == 2
        assert outcome.summary["scored"] == N
        errored = [r for r in outcome.results if r.status == "error"]
        assert {r.user_id for r in errored} == {src.fetches[2], src.fetches[5]}
        assert not any(r.eligible for r in errored)
        assert any("2 accounts failed" in w and "boom" in w for w in outcome.warnings)
        # Failed accounts are not cached, so the next run fetches them again.
        assert store.get_account("fake", src.fetches[5]) is None
        statuses = {a.user_id: s for a, s in store.get_run_sample(outcome.run_id)}
        assert statuses[src.fetches[2]] == "error"
        assert list(statuses.values()).count("fetched") == N - 2
        again = rescore_run(store, outcome.run_id)
        assert again.summary["errors"] == 2
        assert again.summary["cliff"] == outcome.summary["cliff"]


def test_consecutive_errors_abort_the_scan(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = make_source(fail={i: SourceError("down") for i in range(1, 100)})
        outcome = run_scan(src, SEEDS, store, settings(max_consecutive_errors=4))
        assert outcome.status == "partial"
        assert len(src.fetches) == 5  # One success, then four failures in a row.
        assert any("Aborted after 4 consecutive" in w for w in outcome.warnings)
        assert store.get_run(outcome.run_id)["status"] == "partial"  # type: ignore[index]


def test_errors_that_are_not_consecutive_do_not_abort(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = make_source(fail={i: SourceError("flaky") for i in range(0, N, 2)})
        outcome = run_scan(src, SEEDS, store, settings(max_consecutive_errors=2))
        assert outcome.status == "complete"
        assert len(src.fetches) == N


def test_auth_error_mid_run_finishes_the_run_with_what_was_fetched(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = make_source(fail={7: SourceAuthError("token revoked")})
        with pytest.raises(SourceAuthError):
            run_scan(src, SEEDS, store, settings())
        run = store.get_run(1)
        assert run is not None
        assert run["status"] == "failed"
        assert run["account_ids"] == src.fetches[:7]
        assert run["summary"]["scored"] == 7
        assert run["finished_at"] is not None
        report = rescore_run(store, 1)  # `report --run-id` works on the partial run.
        assert report.summary["scored"] == 7


@pytest.mark.parametrize("error", [BudgetExceeded("usd cap"), RateLimited(900.0, "slow down")])
def test_budget_or_rate_limit_mid_run_is_partial(tmp_path: Path, error: Exception) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = make_source(fail={4: error})
        outcome = run_scan(src, SEEDS, store, settings())
        assert outcome.status == "partial"
        run = store.get_run(outcome.run_id)
        assert run is not None and run["account_ids"] == src.fetches[:4]
        assert any(f"--from-run {outcome.run_id}" in w for w in outcome.warnings)


def test_from_run_reuses_the_sample_and_fetches_only_missing(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = make_source(fail={10: BudgetExceeded("cap")})
        partial = run_scan(first, SEEDS, store, settings())
        assert partial.status == "partial"
        sample_ids = [a.user_id for a, _ in store.get_run_sample(partial.run_id)]
        assert len(sample_ids) == N

        second = make_source()
        resumed = run_scan(second, SEEDS, store, settings(), from_run=partial.run_id)
        assert second.discover_calls == 0  # Discovery skipped.
        assert resumed.status == "complete"
        assert resumed.summary["scored"] == N
        assert resumed.summary["from_run"] == partial.run_id
        # Only accounts without fresh cached activity were fetched (unavailable ones
        # never reach fetch_activity).
        assert set(second.fetches) <= set(sample_ids[10:])
        assert not set(second.fetches) & set(first.fetches[:10])
        assert [a.user_id for a, _ in store.get_run_sample(resumed.run_id)] == sample_ids
        assert store.get_run(resumed.run_id)["params"]["from_run"] == partial.run_id  # type: ignore[index]

        third = make_source()
        run_scan(third, SEEDS, store, settings(), from_run=resumed.run_id)
        assert third.fetches == [] and third.discover_calls == 0  # Everything is cached.


def test_from_run_retries_errored_accounts(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = make_source(fail={3: SourceError("boom")})
        outcome = run_scan(first, SEEDS, store, settings())
        second = make_source()
        resumed = run_scan(second, SEEDS, store, settings(), from_run=outcome.run_id)
        assert second.fetches == [first.fetches[3]]
        assert resumed.summary["errors"] == 0


def test_load_run_sample_errors(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        with pytest.raises(LookupError, match="no run"):
            load_run_sample(store, 9, "fake")
        run_id = store.create_run("x", {}, CUTOFF)
        with pytest.raises(ValueError, match="'x' run"):
            load_run_sample(store, run_id, "fake")
        empty = store.create_run("fake", {}, CUTOFF)
        with pytest.raises(LookupError, match="no stored sample"):
            load_run_sample(store, empty, "fake")


class WarningSource(ScriptedSource):
    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        self.warn("seed c9 is banned; skipped")
        self.warn("discovery reached only 2026-10-12, not the cutoff")
        yield from super().discover(seeds, pool_limit)

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        self.warn("history capped for some accounts")
        return super().fetch_activity(account, since, until)


def test_source_warnings_reach_the_outcome_and_the_run_summary(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        outcome = run_scan(make_source(cls=WarningSource), SEEDS, store, settings())
        for text in ("seed c9 is banned", "not the cutoff", "history capped"):
            assert sum(text in w for w in outcome.warnings) == 1
        stored = store.get_run(outcome.run_id)["summary"]["warnings"]  # type: ignore[index]
        assert stored == outcome.warnings
        assert rescore_run(store, outcome.run_id).warnings == outcome.warnings


class TruncatingSource(ScriptedSource):
    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        out = super().fetch_activity(account, since, until)
        account.meta["truncated"] = len(self.fetches) % 3 == 0
        return out


def test_truncation_is_surfaced(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        outcome = run_scan(make_source(cls=TruncatingSource), SEEDS, store, settings(n=200))
        flagged = [r for r in outcome.results if r.truncated]
        assert flagged and outcome.summary["truncated"] == len(flagged)
        assert rescore_run(store, outcome.run_id).summary["truncated"] == len(flagged)
    csv_path = tmp_path / "out.csv"
    write_csv(csv_path, outcome, "fake", include_unavailable=True)
    text = csv_path.read_text()
    assert "TRUNCATED:" in text and "false negatives" in text
    assert (
        "truncated"
        in text.splitlines()[
            next(i for i, ln in enumerate(text.splitlines()) if not ln.startswith("#"))
        ]
    )
    json_path = tmp_path / "out.json"
    write_json(json_path, outcome, "fake")
    assert "false negatives" in json_path.read_text()
    assert TRUNCATED_NOTICE.format(count=len(flagged)) in text


class SpendingSource(ScriptedSource):
    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        assert self.config.ledger is not None
        self.config.ledger.add(0.25)
        return super().fetch_activity(account, since, until)


def test_ledger_is_bound_to_the_run_and_summarized(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = make_source(store, cls=SpendingSource)
        outcome = run_scan(src, SEEDS, store, settings())
        assert src.config.ledger.run_id == outcome.run_id
        expected = 0.25 * len(src.fetches)
        assert outcome.summary["spend_usd"] == pytest.approx(expected)
        assert store.spend_for_run(outcome.run_id) == pytest.approx(expected)
        # A second run on the same day sees the cumulative daily total.
        src2 = make_source(store, cls=SpendingSource)
        run_scan(src2, SEEDS, store, settings(seed=4))
        assert src2.config.ledger.spent("day") == pytest.approx(expected + 0.25 * len(src2.fetches))


def test_sample_only_stores_the_sample_without_fetching(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        dry = make_source()
        outcome = run_scan(dry, SEEDS, store, settings(), sample_only=True)
        assert outcome.status == "sampled"
        assert dry.fetches == []
        assert len(store.get_run_sample(outcome.run_id)) == N
        assert store.get_run(outcome.run_id)["status"] == "sampled"  # type: ignore[index]

        real = make_source()
        resumed = run_scan(real, SEEDS, store, settings(), from_run=outcome.run_id)
        assert real.discover_calls == 0
        assert resumed.summary["scored"] == N


def test_free_sources_report_no_spend_plan_or_projection() -> None:
    src = make_source()
    assert src.plan_spend(SEEDS, n=10, pool_limit=10, resume=False) is None
    assert src.cost_projection([], CUTOFF, NOW) is None
    assert type(src).default_pool_factor is None
