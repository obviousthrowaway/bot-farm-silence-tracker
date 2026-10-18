"""Resume semantics: stored options, reuse of fetched accounts, chains, failures, truncation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker import pipeline
from bot_farm_silence_tracker.cli import (
    _print_summary,
    app,
    join_command,
    resolve_resume,
    resume_command,
)
from bot_farm_silence_tracker.events import custom_event_id
from bot_farm_silence_tracker.models import Account, Activity
from bot_farm_silence_tracker.pipeline import ScanSettings, rescore_run, run_scan, storable_options
from bot_farm_silence_tracker.sampling import sample_accounts
from bot_farm_silence_tracker.sources.base import BudgetExceeded, SourceError
from bot_farm_silence_tracker.storage import Storage, event_db_path

from .helpers import CUTOFF, EVENT, NOW, make_account, steady
from .test_fake_and_cli import scan_args
from .test_pipeline import (
    SEEDS,
    ScriptedSource,
    SpendingSource,
    TruncatingSource,
    make_source,
    settings,
)

runner = CliRunner()
#: A custom cutoff one day before the fixture event's, and its local-only event.
CUSTOM_DAY = CUTOFF - timedelta(days=1)
CUSTOM = CUSTOM_DAY.strftime("%Y-%m-%d")
CUSTOM_ID = custom_event_id(CUSTOM_DAY)


# -- H1: stored options -----------------------------------------------------------


def test_secret_looking_options_are_never_stored() -> None:
    opts = {"population": "800", "api_key": "k", "Token": "t", "client_secret": "s",
            "password": "p", "lang": "en"}  # fmt: skip
    assert storable_options(opts) == {"population": "800", "lang": "en"}
    stored = ScanSettings(
        platform="fake", scoring=settings().scoring, source_options=opts, max_requests=9
    ).to_dict()
    assert stored["source_options"] == {"population": "800", "lang": "en"}
    assert stored["max_requests"] == 9


def _first_run(tmp_path: Path) -> Any:
    return runner.invoke(
        app,
        scan_args(
            tmp_path,
            "--cutoff", CUSTOM,
            "--min-posts", "15",
            "--sync-minutes", "45",
            "--max-requests", "40",
            "--source-option", "population=800",
            "--source-option", "api_key=hunter2",
            "--out", str(tmp_path / "a.csv"),
        ),
    )  # fmt: skip


def test_from_run_takes_the_stored_options(tmp_path: Path) -> None:
    first = _first_run(tmp_path)
    assert first.exit_code == 0, first.output
    with Storage(tmp_path / "c.db") as store:
        params1 = store.get_run(1)["params"]  # type: ignore[index]
    assert params1["source_options"] == {"population": "800"}
    assert "hunter2" not in str(params1)
    # The printed resume command carries --db (not the default) and the options.
    line = next(ln for ln in first.output.splitlines() if "Continue this sample" in ln)
    assert f"--db {tmp_path / 'c.db'}" in line
    expected = (
        "--from-run 1",
        f"--event {CUSTOM_ID}",
        "--min-posts 15",
        "--sync-minutes 45",
        "--max-requests 40",
        "--source-option population=800",
        "--n 300",
        "--seed 1",
    )
    for part in expected:
        assert part in line, part
    assert "api_key" not in line and "hunter2" not in line

    second = runner.invoke(
        app, ["scan", "--platform", "fake", "--from-run", "1", "--db", str(tmp_path / "c.db"),
              "--now", "2026-10-14T00:00:00Z", "--out", str(tmp_path / "b.csv")],
    )  # fmt: skip
    assert second.exit_code == 0, second.output
    with Storage(tmp_path / "c.db") as store:
        params2 = store.get_run(2)["params"]  # type: ignore[index]
    for key in ("cutoff", "baseline_days", "min_posts", "sync_minutes", "min_active_days"):
        assert params2["scoring"][key] == params1["scoring"][key], key
    assert params2["max_requests"] == 40
    assert params2["source_options"] == {"population": "800"}
    assert params2["n"] == 300 and params2["seed"] == 1


def test_from_run_refuses_conflicts_unless_forced(tmp_path: Path) -> None:
    assert _first_run(tmp_path).exit_code == 0
    base = ["scan", "--platform", "fake", "--from-run", "1", "--db", str(tmp_path / "c.db"),
            "--now", "2026-10-14T00:00:00Z", "--out", str(tmp_path / "b.csv")]  # fmt: skip
    same = runner.invoke(
        app, [*base, "--min-posts", "15", "--cutoff", CUSTOM_DAY.strftime("%Y-%m-%dT%H:%MZ")]
    )
    assert same.exit_code == 0, same.output  # Matching values are not conflicts.
    # Another event is refused, even with --force: the cutoff belongs to the event.
    for other in (["--cutoff", (CUTOFF - timedelta(days=9)).strftime("%Y-%m-%d")],
                  ["--event", EVENT.id], ["--event", EVENT.id, "--force"]):  # fmt: skip
        refused = runner.invoke(app, [*base, *other])
        assert refused.exit_code == 2, (other, refused.output)
        assert f"belongs to the outage event {CUSTOM_ID}" in refused.output
    for extra in (["--min-posts", "10"],
                  ["--source-option", "population=900"], ["--source-option", "lang=en"],
                  ["--max-requests", "41"]):  # fmt: skip
        result = runner.invoke(app, [*base, *extra])
        assert result.exit_code == 2, (extra, result.output)
        assert "--force" in result.output and "differ from run 1" in result.output
    forced = runner.invoke(app, [*base, "--min-posts", "10", "--force"])
    assert forced.exit_code == 0, forced.output
    assert "--force override, --min-posts" in forced.output
    with Storage(tmp_path / "c.db") as store:
        assert store.get_run(3)["params"]["scoring"]["min_posts"] == 10  # type: ignore[index]
    # A secret-looking option is never stored, so passing it again is not a conflict.
    secret = runner.invoke(app, [*base, "--source-option", "api_key=hunter2"])
    assert secret.exit_code == 0, secret.output


def test_resolve_resume_and_resume_command_units() -> None:
    stored = {"n": 50, "max_requests": None, "scoring": {"cutoff": EVENT.cutoff_text,
              "min_posts": 20}, "source_options": {"qpm": "60"}}  # fmt: skip
    values, options, overrides = resolve_resume(4, stored, {}, {}, force=False)
    assert values["n"] == 50 and values["min_posts"] == 20 and options == {"qpm": "60"}
    assert overrides == []
    with pytest.raises(ValueError, match="--n: run 4 used 50, got 60"):
        resolve_resume(4, stored, {"n": 60}, {}, force=False)
    values, _, overrides = resolve_resume(4, stored, {"n": 60}, {}, force=True)
    assert values["n"] == 60 and len(overrides) == 1
    # Keys an older run did not store are taken as given.
    values, _, _ = resolve_resume(4, {"scoring": {}}, {"min_posts": 7}, {}, force=False)
    assert values["min_posts"] == 7
    cmd = resume_command(
        "x", 9, None, values, {"qpm": "60", "bearer_token": "t"}, event_id=EVENT.id
    )
    assert "--db" not in cmd and "token" not in cmd and "--source-option qpm=60" in cmd
    assert f"--from-run 9 --event {EVENT.id}" in cmd and "--cutoff" not in cmd
    own = event_db_path(EVENT.id)
    assert "--db" not in resume_command("x", 9, own, values, {}, event_id=EVENT.id)
    resolved = Path("/tmp/my db.sqlite").resolve()  # A symlinked temp dir on macOS.
    quoted = join_command([str(resolved)])  # Single quotes on POSIX, double on Windows.
    mine = resume_command("x", 9, Path("/tmp/my db.sqlite"), values, {}, event_id=EVENT.id)
    assert f"--db {quoted}" in mine
    assert quoted != str(resolved)


# -- M1: reuse what the earlier run fetched ----------------------------------------


def test_resume_reuses_fetched_accounts_regardless_of_ttl(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = make_source(fail={10: BudgetExceeded("cap")})
        partial = run_scan(first, SEEDS, store, settings(cache_ttl_hours=0.0))
        fetched_before = set(first.fetches[:10])
        second = make_source()
        with caplog.at_level(logging.INFO, logger="bot_farm_silence_tracker.pipeline"):
            run_scan(second, SEEDS, store, settings(cache_ttl_hours=0.0), from_run=partial.run_id)
        assert not set(second.fetches) & fetched_before  # TTL 0, yet nothing refetched.
        plan = [r.getMessage() for r in caplog.records if "reused from the cache" in r.getMessage()]
        assert plan and plan[-1].startswith(f"{len(second.fetches)} accounts to fetch")

        third = make_source()
        run_scan(
            third,
            SEEDS,
            store,
            settings(cache_ttl_hours=0.0, refresh=True),
            from_run=partial.run_id,
        )
        assert set(third.fetches) >= fetched_before  # --refresh refetches them.


def test_errored_status_is_written_to_the_sample_snapshot(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = make_source(fail={3: SourceError("boom")})
        outcome = run_scan(first, SEEDS, store, settings())
        snap = {a.user_id: (a.status, s) for a, s in store.get_run_sample(outcome.run_id)}
        assert snap[first.fetches[3]] == ("error", "error")
        resumed = pipeline.load_run_sample(store, outcome.run_id, "fake")
        retried = next(a for a in resumed.accounts if a.user_id == first.fetches[3])
        assert retried.status == "active"  # The reset is no longer dead code.


# -- M2: spend across the chain ----------------------------------------------------


class CappedSpendingSource(SpendingSource):
    def __init__(self, config: Any, stop_after: int | None = None) -> None:
        super().__init__(config)
        self.stop_after = stop_after

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        if self.stop_after is not None and len(self.fetches) >= self.stop_after:
            raise BudgetExceeded("cap")
        return super().fetch_activity(account, since, until)


def test_run_scope_spend_carries_across_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = make_source(store, cls=CappedSpendingSource, stop_after=5)
        one = run_scan(first, SEEDS, store, settings())
        second = make_source(store, cls=CappedSpendingSource, stop_after=5)
        two = run_scan(second, SEEDS, store, settings(), from_run=one.run_id)
        third = make_source(store, cls=CappedSpendingSource)
        three = run_scan(third, SEEDS, store, settings(), from_run=two.run_id)
        assert store.get_run(three.run_id)["root_run_id"] == one.run_id  # type: ignore[index]
        assert third.config.ledger.root_run_id == one.run_id
        total = 0.25 * (5 + 5 + len(third.fetches))
        assert third.config.ledger.spent("run") == pytest.approx(total)
        assert three.summary["chain_spend_usd"] == pytest.approx(total)
        assert three.summary["spend_usd"] == pytest.approx(0.25 * len(third.fetches))
        assert three.summary["root_run_id"] == one.run_id
    _print_summary(three, "fake")
    assert f"resume chain from run {one.run_id}" in capsys.readouterr().out


# -- L4, L5, L6, L7 --------------------------------------------------------------


class BrokenHydrate(ScriptedSource):
    def hydrate(self, account: Account) -> Account:
        raise SourceError("lookup down")


def test_hydrate_errors_in_discovery_use_the_consecutive_cap() -> None:
    src = make_source(cls=BrokenHydrate)
    result = sample_accounts(
        src, SEEDS, n=10, new_accounts_days=60, now=NOW, max_consecutive_errors=3
    )
    assert result.truncated and result.hydrate_errors == 3
    assert any("3 consecutive" in w for w in result.warnings)
    uncapped = sample_accounts(make_source(cls=BrokenHydrate), SEEDS, n=10, new_accounts_days=60,
                               now=NOW)  # fmt: skip
    assert not uncapped.truncated and uncapped.hydrate_errors > 3


def test_scoring_failure_marks_the_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise ZeroDivisionError("scoring bug")

    monkeypatch.setattr(pipeline, "score_accounts", explode)
    with Storage(tmp_path / "c.db") as store:
        with pytest.raises(ZeroDivisionError):
            run_scan(make_source(), SEEDS, store, settings())
        run = store.get_run(1)
        assert run is not None and run["status"] == "failed" and run["finished_at"]


def test_cache_must_reach_the_new_reference_time(tmp_path: Path) -> None:
    acct = make_account("a")
    with Storage(tmp_path / "c.db") as store:
        store.upsert_account(acct, NOW)
        start = NOW - timedelta(days=40)
        store.save_activities(acct, steady("a", start, NOW), start, NOW, fetched_at=NOW)
        ttl = timedelta(hours=24)
        assert store.activity_cache_fresh("fake", "a", start, NOW, ttl, until=NOW)
        later = NOW + timedelta(hours=30)
        # Fetched recently by the wall clock, but the window stops 30h before --now.
        assert store.activity_cache_fresh("fake", "a", start, NOW, ttl)
        assert not store.activity_cache_fresh("fake", "a", start, NOW, ttl, until=later)
        assert store.activity_cache_covers("fake", "a", start)
        assert not store.activity_cache_covers("fake", "a", start - timedelta(days=1))


def test_resume_warns_when_reused_cache_stops_before_now(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        partial = run_scan(make_source(fail={10: BudgetExceeded("cap")}), SEEDS, store, settings())
        later = settings(scoring=pipeline.ScoringParams(
            cutoff=settings().scoring.cutoff, now=NOW + timedelta(days=3)))  # fmt: skip
        resumed = run_scan(make_source(), SEEDS, store, later, from_run=partial.run_id)
        assert any("--refresh" in w and "look silent" in w for w in resumed.warnings)
        fresh = run_scan(make_source(), SEEDS, store, later, from_run=partial.run_id)
        assert any("--refresh" in w for w in fresh.warnings)
        refreshed = run_scan(
            make_source(), SEEDS, store, settings(scoring=later.scoring, refresh=True),
            from_run=partial.run_id,
        )  # fmt: skip
        assert not any("--refresh" in w for w in refreshed.warnings)


class NeverTruncated(ScriptedSource):
    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        out = super().fetch_activity(account, since, until)
        account.meta["truncated"] = False
        return out


def test_report_uses_the_runs_own_truncation(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        first = run_scan(make_source(cls=TruncatingSource), SEEDS, store, settings(n=200))
        flagged = first.summary["truncated"]
        assert flagged > 0
        per_run = store.get_run_truncated(first.run_id)
        assert sum(per_run.values()) == flagged
        # A later fetch of the same accounts overwrites the accounts table's meta ...
        run_scan(
            make_source(cls=NeverTruncated), SEEDS, store, settings(n=200, refresh=True),
            from_run=first.run_id,
        )  # fmt: skip
        assert rescore_run(store, first.run_id + 1).summary["truncated"] == 0
        # ... but the first run's report still shows its own truncation.
        assert rescore_run(store, first.run_id).summary["truncated"] == flagged


def test_partial_resume_prints_hint_for_unstored_options(tmp_path: Path) -> None:
    first = runner.invoke(
        app,
        scan_args(
            tmp_path,
            "--source-option", "api_key=K",
            "--max-requests", "40",
            "--out", str(tmp_path / "a.csv"),
        ),
    )  # fmt: skip
    assert first.exit_code == 0, first.output
    assert "Continue this sample" in first.output
    second = runner.invoke(
        app,
        ["scan", "--platform", "fake", "--from-run", "1", "--db", str(tmp_path / "c.db"),
         "--now", "2026-10-14T00:00:00Z", "--out", str(tmp_path / "b.csv")],
    )  # fmt: skip
    assert second.exit_code == 0, second.output
    assert "Continue this sample" in second.output
    hint = next(ln for ln in second.output.splitlines() if "pass them again" in ln)
    assert "carry credentials" in hint
    assert hint.endswith("api_key")
