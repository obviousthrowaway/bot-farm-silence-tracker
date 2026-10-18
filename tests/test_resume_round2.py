"""Secret-option segment matching, resume command round trips, fatal-path warnings,
original seeds on resume and the moved-cutoff warning."""

from __future__ import annotations

import logging
import shlex
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app, resolve_resume, resume_command
from bot_farm_silence_tracker.pipeline import (
    ScanSettings,
    is_secret_option,
    run_scan,
    storable_options,
    unstored_options,
)
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import BudgetExceeded, Source, SourceAuthError
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF, EVENT, NOW
from .test_fake_and_cli import scan_args
from .test_pipeline import SEEDS, make_source, settings

runner = CliRunner()

STORED = ("keywords", "max_keys", "max_usd", "max_discovery_usd", "budget_scope", "monkey",
          "population", "keyspace", "passes")  # fmt: skip
DROPPED = ("bearer", "auth", "api_pass", "cookie", "api_key", "apikey", "access_token",
           "client-secret", "password", "passwd", "session", "credentials", "authToken",
           "Bearer_Token", "x_auth")  # fmt: skip


@pytest.mark.parametrize("key", STORED)
def test_ordinary_options_are_stored(key: str) -> None:
    assert not is_secret_option(key)
    assert storable_options({key: "v"}) == {key: "v"}


@pytest.mark.parametrize("key", DROPPED)
def test_secret_options_are_dropped(key: str) -> None:
    assert is_secret_option(key)
    assert storable_options({key: "v"}) == {}


def test_adapter_secret_options_are_dropped_too() -> None:
    class Custom(Source):
        secret_options: ClassVar[tuple[str, ...]] = ("handshake",)

    assert Source.secret_options == ()
    opts = {"handshake": "h", "keywords": "k"}
    assert storable_options(opts, Custom.secret_options) == {"keywords": "k"}
    assert unstored_options(opts, Custom.secret_options) == ["handshake"]
    stored = ScanSettings(
        platform="fake",
        scoring=ScoringParams(cutoff=CUTOFF, now=NOW),
        source_options=opts,
        secret_options=Custom.secret_options,
    ).to_dict()
    assert stored["source_options"] == {"keywords": "k"}
    # Not stored, so resuming with it is not a conflict.
    stored_params = {"scoring": {"cutoff": EVENT.cutoff_text}, "source_options": {"keywords": "k"}}
    _, options, conflicts = resolve_resume(
        1, stored_params, {}, {"handshake": "h2"}, force=False, secret_options=("handshake",)
    )
    assert options["handshake"] == "h2" and conflicts == []


def test_resume_output_lists_options_that_were_not_stored(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        scan_args(tmp_path, "--max-requests", "40", "--source-option", "population=800",
                  "--source-option", "api_key=hunter2", "--source-option", "bearer=b",
                  "--out", str(tmp_path / "a.csv")),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    line = next(ln for ln in result.output.splitlines() if "were not stored" in ln)
    assert line.endswith("api_key, bearer")
    assert "hunter2" not in result.output


def test_resume_command_floats_round_trip() -> None:
    stored = {"scoring": {"cutoff": EVENT.cutoff_text, "min_silence_days": 3.0},
              "n": 100, "pool_factor": 1.2345678, "source_options": {}}  # fmt: skip
    values, opts, _ = resolve_resume(1, stored, {}, {}, force=False)
    argv = shlex.split(resume_command("fake", 2, None, values, opts, event_id=EVENT.id))
    pool = argv[argv.index("--pool-factor") + 1]
    assert pool == "1.2345678"
    resolve_resume(2, stored, {"pool_factor": float(pool)}, {}, force=False)  # No conflict.


def test_bool_and_number_options_compare_normalized() -> None:
    stored = {"scoring": {}, "source_options": {"strict": True, "population": 2000, "lang": "en"}}
    for given in ({"strict": "true"}, {"strict": "yes"}, {"population": "2000.0"}):
        _, _, conflicts = resolve_resume(1, stored, {}, given, force=False)
        assert conflicts == [], given
    for given in ({"strict": "false"}, {"population": "2001"}, {"lang": "de"}):
        with pytest.raises(ValueError, match="differ from run 1"):
            resolve_resume(1, stored, {}, given, force=False)
    with pytest.raises(ValueError, match="'<unset>'"):  # A new option needs --force.
        resolve_resume(1, stored, {}, {"region": "eu"}, force=False)
    _, options, conflicts = resolve_resume(1, stored, {}, {"region": "eu"}, force=True)
    assert options["region"] == "eu" and len(conflicts) == 1


@pytest.mark.parametrize(
    "error", [SourceAuthError("bad token"), RuntimeError("bug"), KeyboardInterrupt()]
)
def test_warnings_are_logged_on_fatal_exits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error: BaseException
) -> None:
    caplog.set_level(logging.WARNING, logger="bot_farm_silence_tracker")
    with Storage(tmp_path / "c.db") as store:
        src = make_source(fail={2: error})
        src.warn("seed c2 was skipped")
        with pytest.raises(type(error)):
            run_scan(src, SEEDS, store, settings())
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "seed c2 was skipped" in messages


def test_cli_prints_warnings_on_an_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bot_farm_silence_tracker.sources.fake import FakeSource

    def broken(self, account, since, until):  # type: ignore[no-untyped-def]
        self.warn("seed c9 was skipped")
        raise SourceAuthError("token expired")

    monkeypatch.setattr(FakeSource, "fetch_activity", broken)
    result = runner.invoke(app, scan_args(tmp_path, "--out", str(tmp_path / "a.csv")))
    assert result.exit_code == 3, result.output
    assert "WARNING: seed c9 was skipped" in result.output


def test_resumed_run_stores_the_original_seeds(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        partial = run_scan(make_source(fail={5: BudgetExceeded("cap")}), SEEDS, store, settings())
        other = SeedSet("fake", ("zz",))
        resumed = run_scan(make_source(), other, store, settings(), from_run=partial.run_id)
        params = store.get_run(resumed.run_id)["params"]  # type: ignore[index]
        assert params["seeds"] == list(SEEDS.items)


def test_moved_cutoff_on_resume_warns_about_reused_windows(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        partial = run_scan(make_source(fail={5: BudgetExceeded("cap")}), SEEDS, store, settings())
        moved = settings(scoring=ScoringParams(cutoff=CUTOFF + timedelta(hours=12), now=NOW))
        resumed = run_scan(make_source(), SEEDS, store, moved, from_run=partial.run_id)
        assert any("differs from run" in w and "--refresh" in w for w in resumed.warnings)
        same = run_scan(make_source(), SEEDS, store, settings(), from_run=partial.run_id)
        assert not any("differs from run" in w for w in same.warnings)
