"""CLI wiring of the core hooks: options, resume, pacer, spend line, truncation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app, parse_source_options
from bot_farm_silence_tracker.models import Account, Activity
from bot_farm_silence_tracker.sources.base import SourceAuthError, SourceConfig
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF
from .test_fake_and_cli import read_csv, scan_args

runner = CliRunner()
SEEN: dict[str, object] = {}


class ProbeSource(FakeSource):
    """Records what the CLI handed it and reports a cost line."""

    default_rps: ClassVar[float] = 50.0
    default_burst: ClassVar[float] = 7.0
    optional_credentials: ClassVar[tuple[str, ...]] = ("BOT_FARM_SILENCE_TRACKER_FAKE_PASSWORD",)

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        SEEN["options"] = dict(config.options)
        SEEN["credentials"] = dict(config.credentials)
        SEEN["rate"] = self.http.rate
        SEEN["ledger"] = config.ledger
        SEEN["baseline_start"] = config.baseline_start
        self.fetch_count = 0

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        self.fetch_count += 1
        if self.option_bool("fail_auth") and self.fetch_count > 3:
            raise SourceAuthError("token revoked")
        return super().fetch_activity(account, since, until)

    def cost_summary(self) -> str | None:
        return f"Estimated probe spend: ${self.fetch_count * 0.01:.2f}"


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> None:
    SEEN.clear()
    monkeypatch.setitem(SOURCE_REGISTRY, "fake", "tests.test_cli_core:ProbeSource")


def test_parse_source_options() -> None:
    assert parse_source_options(["qpm=100", "lang = en", "q=a=b", "qpm=60"]) == {
        "qpm": "60",
        "lang": "en",
        "q": "a=b",
    }
    assert parse_source_options(None) == {}
    for bad in ("novalue", "=x"):
        with pytest.raises(ValueError):
            parse_source_options([bad])


def test_source_options_credentials_pacer_and_cost_line(
    tmp_path: Path, probe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_FARM_SILENCE_TRACKER_FAKE_PASSWORD", "hunter2")
    out = tmp_path / "r.csv"
    args = scan_args(
        tmp_path,
        "--out",
        str(out),
        "--source-option",
        "population=800",
        "--source-option",
        "lang=en",
    )
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert SEEN["options"] == {"population": "800", "lang": "en"}
    assert SEEN["credentials"] == {"BOT_FARM_SILENCE_TRACKER_FAKE_PASSWORD": "hunter2"}
    assert SEEN["rate"] == (50.0, 7.0)  # HttpClient built from the class attributes.
    assert SEEN["ledger"] is not None
    assert SEEN["baseline_start"] is not None
    assert "Estimated probe spend: $" in result.output
    assert "hunter2" not in result.output
    _, rows = read_csv(out)
    assert rows and "truncated" in rows[0]


def test_bad_source_option_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, scan_args(tmp_path, "--source-option", "population"))
    assert result.exit_code == 2
    assert "key=value" in result.output
    bad_value = runner.invoke(app, scan_args(tmp_path, "--source-option", "population=lots"))
    assert bad_value.exit_code == 2
    assert "invalid adapter configuration" in bad_value.output


def test_cost_line_prints_on_failure_and_partial_run_is_saved(tmp_path: Path, probe: None) -> None:
    result = runner.invoke(app, scan_args(tmp_path, "--source-option", "fail_auth=true"))
    assert result.exit_code == 3
    assert "authentication failed" in result.output
    assert "Estimated probe spend: $0.04" in result.output
    with Storage(tmp_path / "c.db") as store:
        run = store.get_run(1)
    assert run is not None and run["status"] == "failed"
    assert len(run["account_ids"]) >= 3
    rep = runner.invoke(app, ["report", "--run-id", "1", "--db", str(tmp_path / "c.db")])
    assert rep.exit_code == 0, rep.output


def test_from_run_resumes_without_discovery(tmp_path: Path) -> None:
    first = runner.invoke(
        app, scan_args(tmp_path, "--out", str(tmp_path / "a.csv"), "--max-requests", "40")
    )
    assert first.exit_code == 0, first.output
    assert "partial" in first.output
    assert "--from-run 1" in first.output
    second = runner.invoke(
        app,
        scan_args(
            tmp_path,
            "--out",
            str(tmp_path / "b.csv"),
            "--from-run",
            "1",
            "--max-requests",
            "5000",
            "--force",
        ),
    )
    assert second.exit_code == 0, second.output
    assert "--force override, --max-requests" in second.output
    assert "Reusing the 300-account sample of run 1" in second.output
    with Storage(tmp_path / "c.db") as store:
        first_ids = [a.user_id for a, _ in store.get_run_sample(1)]
        second_ids = [a.user_id for a, _ in store.get_run_sample(2)]
        run2 = store.get_run(2)
    assert first_ids == second_ids
    assert run2 is not None and run2["status"] == "complete"
    assert run2["summary"]["scored"] == 300


def test_from_run_errors(tmp_path: Path) -> None:
    missing = runner.invoke(app, scan_args(tmp_path, "--from-run", "5"))
    assert missing.exit_code == 2
    assert "no run with id 5" in missing.output
    with Storage(tmp_path / "c.db") as store:
        store.create_run("x", {}, CUTOFF)
    wrong = runner.invoke(app, scan_args(tmp_path, "--from-run", "1"))
    assert wrong.exit_code == 2
    assert "cannot be resumed" in wrong.output
