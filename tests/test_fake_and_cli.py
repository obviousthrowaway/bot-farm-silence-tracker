from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.models import Activity
from bot_farm_silence_tracker.output import NOTICE
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import SourceConfig
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY

from .helpers import CUTOFF, NOW, at

runner = CliRunner()
NOW_ARG = "2026-10-14T00:00:00Z"


def fake_source(**options: object) -> FakeSource:
    return FakeSource(
        SourceConfig(http=HttpClient(), credentials={}, now=NOW, cutoff=CUTOFF, options=options)
    )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    comments = [ln for ln in lines if ln.startswith("#")]
    body = [ln for ln in lines if not ln.startswith("#")]
    return comments, list(csv.DictReader(body))


def scan_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "scan", "--platform", "fake", "--n", "300", "--seed", "1",
        "--now", NOW_ARG, "--db", str(tmp_path / "c.db"), *extra,
    ]  # fmt: skip


def test_fake_source_is_deterministic_and_covers_archetypes() -> None:
    a, b = fake_source(population=500), fake_source(population=500)
    seeds = SeedSet("fake", ("c1", "c2"))
    ids_a = [x.user_id for x in a.discover(seeds, 200)]
    assert ids_a == [x.user_id for x in b.discover(seeds, 200)]
    assert len(ids_a) == len(set(ids_a)) == 200
    kinds = {a.archetype(i) for i in range(500)}
    assert {"cliff_bot", "human_irregular", "human_break", "new_account", "suspended"} <= kinds
    acct = next(iter(a.discover(seeds, 1)))
    one = a.fetch_activity(acct, at(-30), NOW) if acct.meta["archetype"] != "suspended" else []
    two = b.fetch_activity(acct, at(-30), NOW) if acct.meta["archetype"] != "suspended" else []
    assert one == two
    assert all(isinstance(x, Activity) and x.ts.tzinfo for x in one)


def test_cli_scan_fake_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "results.csv"
    result = runner.invoke(app, scan_args(tmp_path, "--out", str(out)))
    assert result.exit_code == 0, result.output
    assert NOTICE in result.output
    comments, rows = read_csv(out)
    assert comments[0] == f"# {NOTICE}"
    assert rows, "expected at least one cliff candidate"
    assert float(rows[0]["score"]) >= float(rows[-1]["score"])
    assert all(r["provisional"] == "False" for r in rows)  # 4 days after cutoff.
    clustered = [r for r in rows if r["cluster_id"]]
    assert clustered and all(int(r["cluster_size"]) >= 2 for r in clustered)

    # Ground truth from the generator: no irregular human or still-active account is flagged.
    src = fake_source()
    kinds = [src.archetype(int(r["user_id"].removeprefix("fake_"))) for r in rows]
    assert not {"steady_active", "long_dormant", "new_account"} & set(kinds)
    # A rare lucky-regular human or a vacationer is a realistic false positive; bots dominate.
    assert kinds.count("cliff_bot") / len(kinds) >= 0.6

    # Re-scoring from cache reproduces the candidates without a source.
    json_out = tmp_path / "again.json"
    rep = runner.invoke(
        app, ["report", "--run-id", "1", "--db", str(tmp_path / "c.db"), "--out", str(json_out)]
    )
    assert rep.exit_code == 0, rep.output
    payload = json.loads(json_out.read_text())
    assert payload["notice"] == NOTICE
    assert [c["user_id"] for c in payload["candidates"]] == [r["user_id"] for r in rows]
    assert payload["summary"]["cliff"] == len(rows)


def test_cli_rerun_hits_cache_and_is_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    # The placebo samples are not cached (they are drawn again each scan), so this
    # test of the run's own cache turns them off.
    off = ("--max-requests", "10000", "--placebo-weeks", "0")
    runner.invoke(app, scan_args(tmp_path, "--out", str(first), *off))
    again = runner.invoke(app, scan_args(tmp_path, "--out", str(second), *off))
    assert again.exit_code == 0, again.output
    _, rows_a = read_csv(first)
    _, rows_b = read_csv(second)
    assert [r["user_id"] for r in rows_a] == [r["user_id"] for r in rows_b]
    # The second run needs only discovery pages: activity and hydration come from cache.
    used = int(again.output.split("Requests used: ")[1].split("/")[0])
    assert used < 20


def test_cli_provisional_warning(tmp_path: Path) -> None:
    out = tmp_path / "p.csv"
    args = scan_args(tmp_path, "--out", str(out))
    args[args.index(NOW_ARG)] = "2026-10-11T00:00:00Z"
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert "PROVISIONAL" in result.output
    comments, rows = read_csv(out)
    assert any("PROVISIONAL" in c for c in comments)
    assert rows and all(r["provisional"] == "True" for r in rows)


def test_cli_budget_guard_yields_partial_run(tmp_path: Path) -> None:
    out = tmp_path / "budget.csv"
    result = runner.invoke(app, scan_args(tmp_path, "--out", str(out), "--max-requests", "30"))
    assert result.exit_code == 0, result.output
    assert "partial" in result.output
    assert "Requests used: 30/30" in result.output


def test_cli_new_accounts_mode(tmp_path: Path) -> None:
    out = tmp_path / "new.json"
    result = runner.invoke(
        app,
        scan_args(tmp_path, "--new-accounts-days", "60", "--pool-factor", "5", "--out", str(out)),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["summary"]["sampled"] > 0


def test_cli_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point a platform at a module that does not exist, so this stays valid once real
    # adapters land.
    monkeypatch.setitem(
        SOURCE_REGISTRY, "reddit", "bot_farm_silence_tracker.sources.nonexistent:Nope"
    )
    missing_adapter = runner.invoke(
        app, ["scan", "--platform", "reddit", "--db", str(tmp_path / "d")]
    )
    assert missing_adapter.exit_code == 2
    assert "not installed yet" in missing_adapter.output
    bad_platform = runner.invoke(app, ["scan", "--platform", "mastodon"])
    assert bad_platform.exit_code == 2
    wrong_flag = runner.invoke(app, ["scan", "--platform", "x", "--subreddit", "foo"])
    assert wrong_flag.exit_code == 2
    future = runner.invoke(app, ["scan", "--platform", "fake", "--cutoff", "2999-01-01"])
    assert future.exit_code == 2
    no_run = runner.invoke(app, ["report", "--run-id", "42", "--db", str(tmp_path / "e.db")])
    assert no_run.exit_code == 2
