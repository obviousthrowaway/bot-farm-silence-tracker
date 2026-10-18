"""Source.data_horizon: effective now, lag line, stale-source abort, --allow-stale-source."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.output import write_json
from bot_farm_silence_tracker.pipeline import run_scan
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.sources.base import (
    BudgetExceeded,
    RateLimited,
    Source,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.errors import StaleSourceError
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF, NOW
from .test_fake_and_cli import scan_args
from .test_pipeline import SEEDS, ScriptedSource, make_source, settings

runner = CliRunner()
FRESH = NOW - timedelta(hours=12)  # Cutoff + 3.5 days: enough silence to judge.
STALE = CUTOFF + timedelta(days=2)  # Before cutoff + min_silence_days (3).


class HorizonSource(ScriptedSource):
    """ScriptedSource with a configurable data horizon, recording call order."""

    horizon: datetime | Exception | None = None

    def __init__(self, config: SourceConfig, **kw: object) -> None:
        super().__init__(config, **kw)  # type: ignore[arg-type]
        self.events: list[str] = []

    def data_horizon(self) -> datetime | None:
        self.events.append("horizon")
        if isinstance(self.horizon, Exception):
            raise self.horizon
        return self.horizon

    def discover(self, seeds, pool_limit):  # type: ignore[no-untyped-def]
        self.events.append("discover")
        yield from super().discover(seeds, pool_limit)


def horizon_source(horizon: datetime | Exception | None, **kw: object) -> HorizonSource:
    src: HorizonSource = make_source(cls=HorizonSource, **kw)
    src.horizon = horizon
    return src


def test_base_source_is_live() -> None:
    assert make_source(cls=FakeSource).data_horizon() is None


def test_scoring_params_effective_now_and_round_trip() -> None:
    params = ScoringParams(cutoff=CUTOFF, now=NOW, horizon=FRESH)
    assert params.effective_now == FRESH
    assert params.days_since_cutoff == pytest.approx(3.5)
    assert (
        ScoringParams(cutoff=CUTOFF, now=NOW, horizon=NOW + timedelta(days=1)).effective_now == NOW
    )
    data = params.to_dict()
    assert data["effective_now"].startswith("2026-10-13T12:00:00")
    assert ScoringParams.from_dict(data) == params
    legacy = ScoringParams(cutoff=CUTOFF, now=NOW).to_dict()
    del legacy["horizon"], legacy["effective_now"], legacy["force_provisional"]
    assert ScoringParams.from_dict(legacy).horizon is None


def test_live_source_scores_with_now(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        outcome = run_scan(horizon_source(None), SEEDS, store, settings())
        assert outcome.params.effective_now == NOW
        assert outcome.summary["horizon"] is None
        assert outcome.summary["effective_now"].startswith("2026-10-14T00:00:00")


def test_lagging_source_scores_with_the_horizon_and_logs_the_lag_first(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bot_farm_silence_tracker")
    with Storage(tmp_path / "c.db") as store:
        src = horizon_source(FRESH)
        outcome = run_scan(src, SEEDS, store, settings())
        assert src.events[:2] == ["horizon", "discover"]  # Checked before discovery.
        assert outcome.params.effective_now == FRESH
        assert outcome.summary["horizon"].startswith("2026-10-13T12:00:00")
        lag = [r for r in caplog.records if "hours behind now" in r.getMessage()]
        assert lag and lag[0].levelno == logging.WARNING and "12.0 hours" in lag[0].getMessage()
        silent = [r for r in outcome.results if r.last_activity and r.silence_hours]
        assert silent
        for r in silent:
            assert r.silence_hours == pytest.approx(
                (FRESH - r.last_activity).total_seconds() / 3600  # type: ignore[operator]
            )
        assert not any(r.provisional for r in outcome.results)  # 3.5 days >= 3.
        stored = store.get_run(outcome.run_id)["params"]["scoring"]  # type: ignore[index]
        assert stored["horizon"].startswith("2026-10-13T12:00:00")
        out = tmp_path / "o.json"
        write_json(out, outcome, "fake")
        assert '"effective_now": "2026-10-13T12:00:00' in out.read_text()


def test_stale_source_aborts_before_sampling(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = horizon_source(STALE)
        with pytest.raises(StaleSourceError, match="--allow-stale-source"):
            run_scan(src, SEEDS, store, settings())
        assert src.events == ["horizon"] and src.fetches == []
        run = store.get_run(1)
        assert run is not None and run["status"] == "failed"
        assert store.get_run_sample(1) == []


def test_allow_stale_source_forces_provisional(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = horizon_source(STALE)
        outcome = run_scan(src, SEEDS, store, settings(allow_stale_source=True))
        assert outcome.params.force_provisional and outcome.params.provisional
        assert outcome.candidates and all(r.provisional for r in outcome.candidates)
        assert any("every result is provisional" in w for w in outcome.warnings)


def test_unknown_horizon_of_a_reporting_source_is_stale(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = horizon_source(SourceError("archive down"))
        with pytest.raises(StaleSourceError, match=r"data horizon unknown.*--allow-stale-source"):
            run_scan(src, SEEDS, store, settings())
        assert src.events == ["horizon"] and src.fetches == []
        run = store.get_run(1)
        assert run is not None and run["status"] == "failed"
        assert store.get_run_sample(1) == []


def test_unknown_horizon_with_allow_stale_source_forces_provisional(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bot_farm_silence_tracker")
    with Storage(tmp_path / "c.db") as store:
        src = horizon_source(SourceError("archive down"))
        outcome = run_scan(src, SEEDS, store, settings(allow_stale_source=True))
        assert outcome.params.force_provisional and outcome.params.horizon is None
        assert outcome.candidates and all(r.provisional for r in outcome.candidates)
        first = next(r for r in caplog.records if "data horizon unknown" in r.getMessage())
        assert first.levelno == logging.WARNING
        assert src.events[:2] == ["horizon", "discover"]


class FallbackSource(HorizonSource):
    """A source that knows how far its data reaches although its horizon check failed."""

    def fallback_horizon(self) -> datetime | None:
        return FRESH


def test_unknown_horizon_with_a_fallback_scores_as_of_the_fallback(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = make_source(cls=FallbackSource)
        src.horizon = SourceError("coverage check failed")
        with pytest.raises(StaleSourceError, match="data horizon unknown"):
            run_scan(src, SEEDS, store, settings())
    with Storage(tmp_path / "d.db") as store:
        src = make_source(cls=FallbackSource)
        src.horizon = SourceError("coverage check failed")
        outcome = run_scan(src, SEEDS, store, settings(allow_stale_source=True))
        assert outcome.params.force_provisional and outcome.params.horizon == FRESH
        assert outcome.params.effective_now == FRESH
        assert outcome.candidates and all(r.provisional for r in outcome.candidates)
        assert any("last known data horizon" in w for w in outcome.warnings)


def test_a_fallback_before_the_silence_window_warns_like_a_known_horizon(
    tmp_path: Path,
) -> None:
    class ShortFallback(HorizonSource):
        def fallback_horizon(self) -> datetime | None:
            return STALE

    with Storage(tmp_path / "c.db") as store:
        src = make_source(cls=ShortFallback)
        src.horizon = SourceError("coverage check failed")
        outcome = run_scan(src, SEEDS, store, settings(allow_stale_source=True))
    assert outcome.params.horizon == STALE and outcome.params.force_provisional
    joined = " ".join(outcome.warnings)
    assert "last known data horizon" in joined
    assert "before the cutoff plus --min-silence-days" in joined


@pytest.mark.parametrize(
    "error", [BudgetExceeded("cap"), RateLimited(5.0), SourceAuthError("expired")]
)
def test_fatal_errors_from_the_horizon_are_reraised(tmp_path: Path, error: SourceError) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = horizon_source(error)
        with pytest.raises(type(error)):
            run_scan(src, SEEDS, store, settings(allow_stale_source=True))
        assert src.events == ["horizon"]
        assert store.get_run(1)["status"] == "failed"  # type: ignore[index]


def test_source_without_a_horizon_override_uses_now(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        src = make_source()
        assert type(src).data_horizon is Source.data_horizon
        outcome = run_scan(src, SEEDS, store, settings())
        assert outcome.params.effective_now == NOW and not outcome.params.force_provisional


def test_horizon_after_now_warns_and_scores_with_now(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        outcome = run_scan(horizon_source(NOW + timedelta(days=1)), SEEDS, store, settings())
        assert outcome.params.effective_now == NOW
        assert any("after the scan's reference time" in w for w in outcome.warnings)


def test_resume_checks_the_horizon_before_fetching(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = make_source(fail={5: BudgetExceeded("cap")})
        partial = run_scan(first, SEEDS, store, settings())
        stale = horizon_source(STALE)
        with pytest.raises(StaleSourceError):
            run_scan(stale, SEEDS, store, settings(), from_run=partial.run_id)
        assert stale.events == ["horizon"] and stale.fetches == []
        fresh = horizon_source(FRESH)
        resumed = run_scan(fresh, SEEDS, store, settings(), from_run=partial.run_id)
        assert fresh.events[0] == "horizon" and "discover" not in fresh.events
        assert resumed.params.effective_now == FRESH


def test_cli_stale_source_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeSource, "data_horizon", lambda self: STALE)
    refused = runner.invoke(app, scan_args(tmp_path, "--out", str(tmp_path / "a.csv")))
    assert refused.exit_code == 4, refused.output
    assert "--allow-stale-source" in refused.output and "Nothing was sampled" in refused.output
    allowed = runner.invoke(
        app, scan_args(tmp_path, "--allow-stale-source", "--out", str(tmp_path / "b.csv"))
    )
    assert allowed.exit_code == 0, allowed.output
    assert "PROVISIONAL" in allowed.output
    assert "provisional=True" in (tmp_path / "b.csv").read_text()
