"""Byte-for-byte regression test of the fake source's ``scan`` and ``report`` output.

Two sets of golden files, written by ``tests/golden/generate.py``:

* ``tests/golden/legacy`` was first generated from ``main`` at 456deb0, before the
  Bluesky adapter, with the default event's first definition (cutoff
  2026-10-10T00:00Z, no markers), and regenerated in the pull request that gave the
  method its controls (:func:`test_what_the_method_controls_change`), so it is not
  the old versions' bytes: from them it keeps the flagged accounts and every earlier
  column of their rows. That definition must reproduce the regenerated files exactly,
  as a custom event of its cutoff and as the fixture event with the same windows.
* ``tests/golden/census`` is the output of the repository's current default event
  (the strikes and the main outage of October 2026, with markers). It was
  regenerated on purpose when the event changed; :func:`test_what_the_new_default_
  event_changes` documents how it differs.

An adapter-specific change (extra columns, extra summary keys) must leave every
other platform's output exactly as it was; these tests fail on any byte that differs.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from bot_farm_silence_tracker.events import EVENTS_ENV, load_events_file
from bot_farm_silence_tracker.scoring import MARKER_COLUMNS

from .conftest import FIXTURE_EVENTS
from .golden.generate import DEFAULT_NOW, LEGACY_ARGS, generate, golden_names

GOLDEN = Path(__file__).parent / "golden" / "census"
LEGACY = Path(__file__).parent / "golden" / "legacy"
#: The repository's own events file: these tests check the real default event.
REPO_EVENTS = Path(__file__).resolve().parent.parent / "events.toml"


@pytest.fixture(autouse=True)
def _repository_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan with the repository's events.toml, not the tests' fixture events: the
    default event must reproduce the output of the version before events."""
    monkeypatch.setenv(EVENTS_ENV, str(REPO_EVENTS))


def test_the_golden_files_use_the_default_event_of_events_toml() -> None:
    catalog = load_events_file(REPO_EVENTS)
    golden = (GOLDEN / "census_k1_scan.json").read_text(encoding="utf-8")
    assert f'"cutoff": "{catalog.default.cutoff_text}"' in golden
    assert f'"baseline_days": {catalog.default.baseline_days},' in golden
    assert f'"min_silence_days": {catalog.default.min_silence_days},' in golden
    assert f'"cliff_window_hours": {catalog.default.cliff_window_hours},' in golden


def _differing(generated: Path, golden: Path) -> list[str]:
    names = golden_names()
    assert sorted(p.name for p in golden.iterdir()) == sorted(names)
    assert sorted(p.name for p in generated.iterdir()) == sorted(names)
    return [
        name for name in names if (generated / name).read_bytes() != (golden / name).read_bytes()
    ]


def test_fake_census_output_matches_the_golden_files(tmp_path: Path) -> None:
    generate(tmp_path, (), DEFAULT_NOW)
    differing = _differing(tmp_path, GOLDEN)
    assert differing == [], (
        f"output differs from the golden files: {differing}. If the change is intended, "
        "regenerate them with tests/golden/generate.py --default and review the diff."
    )


def test_the_old_definition_reproduces_the_legacy_output_as_a_custom_event(
    tmp_path: Path,
) -> None:
    """The first definition of the default event, scanned as the custom event of its
    cutoff, gives the legacy golden files byte for byte (regenerated with the method's
    controls; the flagged accounts and earlier columns are the old versions')."""
    assert LEGACY_ARGS == ("--cutoff", "2026-10-10T00:00Z")
    generate(tmp_path)
    assert _differing(tmp_path, LEGACY) == []


def test_the_old_definition_reproduces_the_legacy_output_as_a_fixture_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared event without markers (the fixture event with the old definition)
    gives the same bytes too: markers change nothing for events that have none."""
    monkeypatch.setenv(EVENTS_ENV, str(FIXTURE_EVENTS))
    old = load_events_file(FIXTURE_EVENTS).get("fixture-main")
    assert old is not None and not old.markers and old.cutoff_text.startswith("2026-10-10T00:00")
    generate(tmp_path, ("--event", "fixture-main"))
    assert _differing(tmp_path, LEGACY) == []


def _rows(path: Path) -> list[dict[str, str]]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line[:1] != "#"]
    return list(csv.DictReader(lines))


def test_what_the_new_default_event_changes() -> None:
    """Documents, on the fake source, how the default event's new definition (the
    reported start of the major outage as cutoff, and the two earlier strikes as
    markers) changes its output against the legacy golden files."""
    old = json.loads((LEGACY / "census_k1_scan.json").read_text(encoding="utf-8"))
    new = json.loads((GOLDEN / "census_k1_scan.json").read_text(encoding="utf-8"))
    catalog = load_events_file(REPO_EVENTS)
    event = catalog.default
    # The cutoff moved to the reported start of the major outage.
    assert old["run"]["cutoff"] == "2026-10-10T00:00:00+00:00"
    assert new["run"]["cutoff"] == event.cutoff_text == "2026-10-11T00:30:00+00:00"
    # The silence window starts marker_margin_hours before the earliest marker, and
    # the baseline ends there too; the legacy run had neither key.
    baseline_start, baseline_end, window_start = event.windows()
    assert new["run"]["window_start"] == new["run"]["baseline_end"] == "2026-10-07T18:56:00+00:00"
    assert window_start == baseline_end and baseline_start.isoformat().startswith("2026-09-07")
    assert "window_start" not in old["run"] and "markers" not in old["run"]
    assert new["run"]["markers"] == (
        "sasovo-hub-strike@2026-10-08T06:56:00+00:00,"
        "kaluga-region-strike@2026-10-09T00:00:00+00:00~approx"
    )
    assert "markers_notice" in new and "markers_notice" not in old
    # The marker columns are appended; every earlier column keeps its place.
    old_rows = _rows(LEGACY / "census_k1_scan.csv")
    new_rows = _rows(GOLDEN / "census_k1_scan.csv")
    old_columns, new_columns = list(old_rows[0]), list(new_rows[0])
    assert new_columns == [*old_columns, *MARKER_COLUMNS]
    # The wider silence window flags more fake accounts (people on a break that
    # began inside it), and each flagged account names its nearest marker.
    assert (old["summary"]["cliff"], new["summary"]["cliff"]) == (11, 14)
    assert new["summary"]["cliff_nearest_marker"] == {
        "cutoff": 5,
        "sasovo-hub-strike": 0,
        "kaluga-region-strike": 9,
    }
    assert sum(new["summary"]["cliff_nearest_marker"].values()) == new["summary"]["cliff"]
    assert {r["nearest_marker"] for r in new_rows} <= {"cutoff", *[m.id for m in event.markers]}
    approximate = {r["nearest_marker_approximate"] for r in new_rows if r["nearest_marker"]}
    assert approximate == {"True", "False"}


def test_what_the_method_controls_change() -> None:
    """Documents, on the fake source, what the placebo controls (each placebo cutoff on
    a sample of its own), the permutation null and the stricter steady test changed in
    the golden files. The flagged accounts and every earlier column of their rows are
    the same as before; the stricter steady test leaves out accounts that were eligible
    but are no cliffs either way."""
    for folder, eligible, cliffs in ((LEGACY, 53, 11), (GOLDEN, 52, 14)):
        data = json.loads((folder / "census_k1_scan.json").read_text(encoding="utf-8"))
        summary = data["summary"]
        # Before the steady test needed recent activity and even weeks: 61 and 59.
        assert (summary["eligible"], summary["cliff"]) == (eligible, cliffs)
        assert data["placebo_notice"][0].startswith("PLACEBO: cliff rate")
        assert summary["placebo"]["weeks"] == 3 and summary["placebo"]["accounts"] > 0
        # Each cluster is measured against the run's largest cluster: already adjusted.
        assert all(t["p_adjusted"] == t["p_permutation"] for t in summary["cluster_tests"])
        # Every placebo cutoff was scored on a sample of its own.
        assert summary["placebo"]["design"].startswith("separate placebo samples")
        assert len(summary["placebo"]["placebos"]) == 3 and summary["placebo"]["requests"] > 0
        assert data["run"]["placebo_weeks"] == 3 and data["run"]["seed"] == 1
        for row in data["candidates"]:
            assert row["recent_posts"] > 0 and row["weekly_cv"] <= 1.0
            # A synchronized stop is never counted as an independent signal.
            named = [s for s in row["signals"].split(";") if s and s != "synchronized_stop"]
            assert row["independent_signals"] == len(named)
        rows = _rows(folder / "census_k1_scan.csv")
        columns = list(rows[0])
        at = columns.index("data_end")
        assert columns[at + 1 : at + 6] == [
            "weekly_cv", "recent_posts", "cluster_p_adjusted", "signals", "independent_signals",
        ]  # fmt: skip


def test_fake_summary_table_has_no_bluesky_rows(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from bot_farm_silence_tracker.cli import app

    from .golden.generate import NOW_ARG

    result = CliRunner().invoke(
        app,
        [
            "scan", "--platform", "fake", "--n", "100", "--seed", "1", "--now", NOW_ARG,
            "--db", str(tmp_path / "c.db"), "--out", str(tmp_path / "o.csv"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    for label in ("deactivated", "Opted out", "reposted after the cutoff"):
        assert label not in result.output
