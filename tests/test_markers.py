"""Markers inside an outage event: the events file schema, the silence window rule,
nearest-marker labels, their output columns, the app's timeline and event details,
runs of an earlier event definition, and the registry gate's marker policy."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker import events as E
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.export import EXPORT_COLUMNS, export_rows, write_export
from bot_farm_silence_tracker.gui import presenter as P
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.registry.gate import render_events
from bot_farm_silence_tracker.registry.records import md_code
from bot_farm_silence_tracker.registry.reports import render_reddit
from bot_farm_silence_tracker.scoring import (
    MAIN_OUTAGE,
    MARKER_COLUMNS,
    METHOD_COLUMNS,
    Marker,
    ScoringParams,
    nearest_marker,
    score_account,
    score_accounts,
)
from bot_farm_silence_tracker.storage import Storage, event_db_path
from bot_farm_silence_tracker.timeutil import isoformat

from .helpers import EVENTS, FIXTURE_EVENTS, steady

runner = CliRunner()
REPO_EVENTS = Path(__file__).resolve().parent.parent / "events.toml"
V020_EVENTS = Path(__file__).parent / "fixtures" / "events_v0.2.0.toml"
STRIKES = EVENTS.get("fixture-strikes")
assert STRIKES is not None
FIRST, SECOND_STRIKE = STRIKES.markers
#: Four days after the strikes event's cutoff: not provisional.
NOW = STRIKES.cutoff + timedelta(days=4)
CLOCK = datetime(2026, 10, 12, tzinfo=UTC)


def _params(**changes: Any) -> ScoringParams:
    base = ScoringParams(
        cutoff=STRIKES.cutoff,
        now=NOW,
        markers=STRIKES.scoring_markers(),
        marker_margin_hours=STRIKES.marker_margin_hours,
    )
    return replace(base, **changes)


def _event_text(*markers: str, margin: str | None = "marker_margin_hours = 12.0\n") -> str:
    return (
        'default = "2026-10-strikes"\nlegacy_event = "2026-10-strikes"\n\n[[event]]\n'
        'id = "2026-10-strikes"\nname = "Strikes"\ncutoff = "2026-10-11T00:30:00Z"\n'
        "baseline_days = 30\nmin_silence_days = 3.0\ncliff_window_hours = 48.0\n"
        + (margin or "")
        + "".join(markers)
    )


def _marker(**fields: Any) -> str:
    values = {
        "id": "first-strike",
        "name": "First strike",
        "time": "2026-10-08T06:56:00Z",
        "approximate": False,
        **fields,
    }
    lines = ["\n[[event.markers]]"]
    for key, value in values.items():
        if value is not None:
            lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def _parse(text: str, **kwargs: Any) -> tuple[E.EventCatalog | None, list[str]]:
    return E.parse_events(text, now=CLOCK, **kwargs)


# -- the schema ----------------------------------------------------------------------


def test_the_repository_default_event_has_the_reported_strikes_as_markers() -> None:
    catalog = E.load_events_file(REPO_EVENTS, require_windows=True)
    event = catalog.default
    assert event.id == "2026-10-takedown" and catalog.legacy_id == event.id
    assert event.cutoff_text == "2026-10-11T00:30:00+00:00"
    assert [m.id for m in event.markers] == ["sasovo-hub-strike", "kaluga-region-strike"]
    sasovo, kaluga = event.markers
    assert sasovo.time_text == "2026-10-08T06:56:00+00:00" and not sasovo.approximate
    assert "first statements" in sasovo.time_note and "overnight" in sasovo.time_note
    assert kaluga.time_text == "2026-10-09T00:00:00+00:00" and kaluga.approximate
    assert kaluga.sources == ("TASS", "Reuters")
    # The earliest marker is about 65.5 hours before the cutoff.
    assert (event.cutoff - sasovo.time) / timedelta(hours=1) == pytest.approx(65.567, abs=0.01)
    assert event.marker_margin_hours == 12.0


def test_a_valid_marker_parses() -> None:
    catalog, problems = _parse(
        _event_text(
            _marker(time_note="Reported.", notes="Some notes.", sources=["Agency A"]),
            _marker(id="second", name="Second", time="2026-10-09T00:00:00Z", approximate=True),
        ),
        require_windows=True,
    )
    assert problems == [] and catalog is not None
    first, second = catalog.default.markers
    assert first.sources == ("Agency A",) and first.time_note == "Reported."
    assert second.approximate and second.label().endswith("approx.)")


@pytest.mark.parametrize(
    ("markers", "problem"),
    [
        ((_marker(), _marker(name="Again")), "duplicate marker id"),
        ((_marker(id="cutoff"),), "reserved for the main outage"),
        ((_marker(id="Bad Id"),), "id must match"),
        ((_marker(time="2026-09-01T00:00:00Z"),), "must lie between the baseline start"),
        ((_marker(time="2026-10-14T01:00:00Z"),), "must lie between the baseline start"),
        ((_marker(time="2026-10-08T06:56:30Z"),), "to the minute"),
        ((_marker(approximate="yes"),), "approximate must be true or false"),
        ((_marker(colour="red"),), "unknown key 'colour'"),
        ((_marker(name="<b>x</b>"),), "name must be"),
        ((_marker(notes="**bold**"),), "notes must be"),
        ((_marker(time_note="a\nb"),), "time_note must be"),
        ((_marker(sources="TASS"),), "sources must be a list"),
        ((_marker(sources=["x"] * 11),), "sources must be a list"),
        (
            (
                _marker(time="2026-10-09T00:00:00Z"),
                _marker(id="later", time="2026-10-08T00:00:00Z"),
            ),
            "time order",
        ),
        (tuple(_marker(id=f"m-{i:02d}") for i in range(21)), "more than 20 markers"),
    ],
)
def test_bad_markers_are_refused(markers: tuple[str, ...], problem: str) -> None:
    catalog, problems = _parse(_event_text(*markers))
    assert catalog is None
    assert any(problem in p for p in problems), problems


def test_a_marker_in_the_future_is_refused() -> None:
    _, problems = E.parse_events(_event_text(_marker()), now=datetime(2026, 10, 8, tzinfo=UTC))
    assert any("time is in the future" in p for p in problems)


def test_the_shared_file_must_write_out_the_margin_of_an_event_with_markers() -> None:
    _, problems = _parse(_event_text(_marker(), margin=None), require_windows=True)
    assert any("must set marker_margin_hours" in p for p in problems)
    catalog, problems = _parse(_event_text(_marker(), margin=None))
    assert problems == [] and catalog is not None
    assert catalog.default.marker_margin_hours == E.DEFAULT_MARKER_MARGIN_HOURS
    _, problems = _parse(_event_text(_marker(), margin="marker_margin_hours = 73.0\n"))
    assert any("marker_margin_hours must be in [0, 72]" in p for p in problems)


# -- the window rule -----------------------------------------------------------------


def test_an_event_without_markers_keeps_the_classic_windows() -> None:
    params = ScoringParams(cutoff=STRIKES.cutoff, now=NOW)
    assert params.onset is None and params.anchors == ()
    assert params.baseline_end == STRIKES.cutoff
    assert params.baseline_start == STRIKES.cutoff - timedelta(days=30)
    assert params.window_start == STRIKES.cutoff - timedelta(hours=48)
    assert params.window_hours == 48.0
    # And stores exactly what it stored before markers existed.
    assert "markers" not in params.to_dict() and "marker_margin_hours" not in params.to_dict()


def test_markers_start_the_silence_window_and_end_the_baseline_before_the_earliest() -> None:
    params = _params()
    onset = FIRST.time - timedelta(hours=12)
    assert params.onset == onset
    assert params.baseline_end == params.window_start == onset
    assert params.baseline_start == onset - timedelta(days=30)
    assert params.window_hours == pytest.approx(65.567 + 12, abs=0.01)
    assert E.OutageEvent.windows(STRIKES) == (onset - timedelta(days=30), onset, onset)


def test_a_late_marker_leaves_the_classic_silence_window_but_ends_the_baseline() -> None:
    late = Marker("late", "Late", STRIKES.cutoff - timedelta(hours=10))
    params = _params(markers=(late,), marker_margin_hours=2.0)
    assert params.window_start == STRIKES.cutoff - timedelta(hours=48)
    assert params.baseline_end == STRIKES.cutoff - timedelta(hours=12)
    assert params.window_hours == 48.0


def test_the_params_round_trip_with_their_markers() -> None:
    params = _params()
    data = params.to_dict()
    assert data["markers"][1] == {
        "id": "second-strike",
        "name": "Second strike",
        "time": isoformat(SECOND_STRIKE.time),
        "approximate": True,
    }
    assert ScoringParams.from_dict(json.loads(json.dumps(data))) == params


def _account(user_id: str) -> Account:
    return Account("fake", user_id, f"handle_{user_id}", STRIKES.cutoff - timedelta(days=400))


def test_a_silence_that_began_at_the_first_strike_is_a_cliff_only_with_markers() -> None:
    stop = FIRST.time - timedelta(hours=1)
    acts = steady("u1", stop - timedelta(days=40), stop, per_day=4)
    with_markers = score_account(_account("u1"), acts, _params())
    without = score_account(_account("u1"), acts, _params(markers=(), marker_margin_hours=0.0))
    assert with_markers.cliff and not without.cliff
    assert with_markers.nearest_marker == FIRST.id
    assert with_markers.last_activity is not None
    offset = (with_markers.last_activity - FIRST.time) / timedelta(hours=1)
    assert with_markers.nearest_marker_offset_hours == pytest.approx(offset) and offset < 0
    assert with_markers.nearest_marker_approximate is False
    # Abruptness is measured to the nearest marker, not to the cutoff 66 hours later.
    assert with_markers.components["abruptness"] == pytest.approx(1 + offset / 48)
    assert without.nearest_marker is None


def test_a_decline_after_the_first_strike_does_not_pollute_the_baseline() -> None:
    onset = _params().baseline_end
    before = steady("u2", onset - timedelta(days=31), onset, per_day=4)
    after = steady("u2", onset, STRIKES.cutoff - timedelta(hours=2), per_day=1)
    result = score_account(_account("u2"), before + after, _params())
    assert result.baseline_posts == len([a for a in before if a.ts >= onset - timedelta(days=30)])
    assert result.cliff and result.nearest_marker == MAIN_OUTAGE


# -- nearest-marker labels -------------------------------------------------------------


def test_nearest_marker_labels_and_ties() -> None:
    params = _params()
    anchors = params.anchors
    assert [a.id for a in anchors] == [MAIN_OUTAGE, FIRST.id, SECOND_STRIKE.id]
    found = nearest_marker(SECOND_STRIKE.time + timedelta(hours=3), anchors)
    assert found is not None and found.id == SECOND_STRIKE.id and found.approximate
    assert found.offset_hours == pytest.approx(3.0)
    assert found.label() == "Second strike (approx.), +3.0 h"
    # Exactly halfway between two moments: the earlier one wins.
    middle = FIRST.time + (SECOND_STRIKE.time - FIRST.time) / 2
    tie = nearest_marker(middle, anchors)
    assert tie is not None and tie.id == FIRST.id
    # Two moments at the same time: the one listed first (the main outage) wins.
    same = Marker("same", "Same", STRIKES.cutoff)
    found = nearest_marker(STRIKES.cutoff - timedelta(hours=1), (anchors[0], same))
    assert found is not None and found.id == MAIN_OUTAGE
    assert nearest_marker(STRIKES.cutoff, ()) is None


def test_clusters_are_labelled_by_their_first_stop() -> None:
    items = []
    for i, minutes in enumerate((0, 10, 20)):
        stop = SECOND_STRIKE.time + timedelta(minutes=minutes)
        items.append((_account(f"c{i}"), steady(f"c{i}", stop - timedelta(days=40), stop)))
    results = score_accounts(items, _params())
    assert {r.cluster_id for r in results} == {1}
    assert {r.cluster_nearest_marker for r in results} == {SECOND_STRIKE.id}
    row = results[0].to_row(marker_columns=True)
    assert list(row)[-4:] == list(MARKER_COLUMNS)
    assert row["nearest_marker_approximate"] is True
    assert list(results[0].to_row())[-1] == METHOD_COLUMNS[-1]


# -- scans, output columns, drafts ----------------------------------------------------


def _scan(*extra: str, db: Path, out: str = "o.csv") -> Any:
    args = ["scan", "--platform", "fake", "--n", "120", "--seed", "1", "--db", str(db)]
    return runner.invoke(app, [*args, "--now", isoformat(NOW), *extra, "--out", out])


def test_a_scan_of_an_event_with_markers_labels_its_output(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    result = _scan("--event", STRIKES.id, db=db)
    assert result.exit_code == 0, result.output
    assert "Markers (times as reported): First strike" in result.output
    assert "silence began nearest to Main outage (cutoff)" in result.output
    assert "silence began nearest to Second strike (approx.)" in result.output
    lines = Path("o.csv").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("# MARKERS:") for line in lines)
    rows = list(csv.DictReader(line for line in lines if not line.startswith("#")))
    header = list(rows[0])
    assert header[-4:] == list(MARKER_COLUMNS)
    assert all(r["nearest_marker"] in {MAIN_OUTAGE, FIRST.id, SECOND_STRIKE.id} for r in rows)
    with Storage(db) as storage:
        stored = storage.get_run(1)
        exported = export_rows(storage, [1])
    assert stored is not None and len(stored["params"]["scoring"]["markers"]) == 2
    # The export appends the marker columns after its own.
    write_export(tmp_path / "x.csv", exported, "csv")
    with (tmp_path / "x.csv").open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == [*EXPORT_COLUMNS, *MARKER_COLUMNS]
    report = runner.invoke(app, ["report", "--run-id", "1", "--db", str(db), "--out", "r.json"])
    assert report.exit_code == 0, report.output
    payload = json.loads(Path("r.json").read_text(encoding="utf-8"))
    assert payload["run"]["window_start"] == isoformat(_params().window_start)
    assert set(payload["summary"]["cliff_nearest_marker"]) == {
        MAIN_OUTAGE,
        FIRST.id,
        SECOND_STRIKE.id,
    }


def test_an_export_without_markers_keeps_its_columns(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    assert _scan(db=db).exit_code == 0
    with Storage(db) as storage:
        exported = export_rows(storage, [1])
    write_export(tmp_path / "x.csv", exported, "csv")
    with (tmp_path / "x.csv").open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == EXPORT_COLUMNS


def test_events_show_lists_the_markers() -> None:
    result = runner.invoke(app, ["events", "show", STRIKES.id])
    assert result.exit_code == 0, result.output
    assert "second-strike: Second strike" in result.output and "(approx.)" in result.output
    assert "sources: Agency B, Agency C" in result.output
    assert "silence window: " + isoformat(_params().window_start) in result.output
    listed = runner.invoke(app, ["events", "list"])
    assert f"{STRIKES.id}  {STRIKES.cutoff_text}  {STRIKES.name} (2 markers)" in listed.output


def _record(last: datetime) -> dict[str, Any]:
    from bot_farm_silence_tracker.registry.records import b64e

    return {
        "platform": "reddit",
        "id_b64": b64e("someone"),
        "username_b64": b64e("someone"),
        "baseline_posts": 90,
        "baseline_active_days": 28,
        "last_activity_at": isoformat(last),
        "scan_window_end": isoformat(NOW),
        "last_scanned_at": isoformat(NOW),
    }


def test_drafts_name_the_nearest_marker() -> None:
    text = render_reddit(_record(SECOND_STRIKE.time + timedelta(hours=2)), None, STRIKES)
    assert f"nearest to the marker {md_code('Second strike (approx.)')}" in text
    assert "+2.0 hours" in text and "as reported, approximate" in text
    assert "near the reported time of Second strike" in text
    assert "during a reported outage (event" in text
    main = render_reddit(_record(STRIKES.cutoff - timedelta(hours=1)), None, STRIKES)
    assert "nearest to the main outage at the cutoff" in main
    assert f"just before a reported outage (event {STRIKES.id}" in main
    plain = render_reddit(_record(STRIKES.cutoff), None, EVENTS.default)
    assert "nearest to" not in plain


# -- the app ---------------------------------------------------------------------------


def test_presenter_timeline_lines_and_event_details(tmp_path: Path) -> None:
    params = _params()
    start = params.baseline_start
    lines = P.timeline_lines(params, start)
    assert [(line.id, line.kind, line.approximate) for line in lines] == [
        (MAIN_OUTAGE, "cutoff", False),
        (FIRST.id, "marker", False),
        (SECOND_STRIKE.id, "marker", True),
    ]
    assert lines[0].day == pytest.approx((params.cutoff - start) / timedelta(days=1))
    assert lines[1].day < lines[2].day < lines[0].day
    assert "approx." in lines[2].label and "approx." not in lines[1].label
    plain = P.timeline_lines(ScoringParams(cutoff=STRIKES.cutoff, now=NOW), start)
    assert [line.id for line in plain] == [MAIN_OUTAGE]
    details = P.event_details(STRIKES, params)
    assert "The second fixture strike." in details and "Sources: Agency B, Agency C" in details
    assert "Time: Early morning; exact time unknown." in details and "as reported" in details


def test_results_carry_marker_labels_lines_and_a_marker_filter(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    assert _scan("--event", STRIKES.id, db=db).exit_code == 0
    results = P.load_results(1, db=db)
    assert [line.id for line in results.lines] == [MAIN_OUTAGE, FIRST.id, SECOND_STRIKE.id]
    assert all(a.marker and a.marker_text for a in results.accounts)
    assert "The first fixture strike." in results.details
    choices = P.filter_choices(results)["marker"]
    assert choices[0] == P.ALL and len(choices) >= 2
    for name in choices[1:]:
        shown = P.filter_accounts(results.accounts, P.Filters(marker=name))
        assert shown and all(a.marker == name for a in shown)
    assert all(c.marker for c in results.clusters)


# -- runs of an earlier definition ----------------------------------------------------


def _move_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> E.OutageEvent:
    """Point the program at an events file whose default event has a later cutoff."""
    moved = FIXTURE_EVENTS.read_text(encoding="utf-8").replace(
        'cutoff = "2026-10-10T00:00:00Z"', 'cutoff = "2026-10-10T06:00:00Z"', 1
    )
    path = tmp_path / "events.toml"
    path.write_text(moved, encoding="utf-8")
    monkeypatch.setenv(E.EVENTS_ENV, str(path))
    return E.load_events(path).default


def test_runs_of_an_earlier_cutoff_map_to_a_labelled_legacy_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = EVENTS.default
    old_now = old.cutoff + timedelta(days=4)
    args = ["scan", "--platform", "fake", "--n", "60", "--seed", "1", "--now", isoformat(old_now)]
    first = runner.invoke(app, [*args, "--max-requests", "20", "--out", "a.csv"])
    assert first.exit_code == 0, first.output
    store = event_db_path(old.id)
    with Storage(store) as storage:
        run = storage.get_run(1)
    assert run is not None and run["params"]["event_id"] == old.id
    new = _move_cutoff(tmp_path, monkeypatch)
    catalog = E.load_events()
    mapped = E.event_of_run(run["params"], catalog)
    assert mapped.id == "custom-20261010T0000Z" and mapped.id != new.id
    assert mapped.custom and mapped.legacy_of == old.id
    assert mapped.cutoff == old.cutoff and "Earlier definition of fixture-main" in mapped.name
    # A run from before events (no stored event id) is never the new default either.
    bare = {k: v for k, v in run["params"].items() if k != "event_id"}
    assert E.event_of_run(bare, catalog).id == "custom-20261010T0000Z"
    # It stays readable from the event's store...
    report = runner.invoke(app, ["report", "--run-id", "1", "--out", "r.json"])
    assert report.exit_code == 0, report.output
    assert json.loads(Path("r.json").read_text(encoding="utf-8"))["run"]["cutoff"] == (
        old.cutoff_text
    )
    assert [r["event_id"] for r in P.list_runs()] == ["custom-20261010T0000Z"]
    # ...and an old resume command (naming the shared event) keeps the run's cutoff.
    resumed = runner.invoke(
        app, ["scan", "--platform", "fake", "--from-run", "1", "--event", old.id,
              "--now", isoformat(old_now), "--out", "b.csv"],
    )  # fmt: skip
    assert resumed.exit_code == 0, resumed.output
    assert "an earlier definition of fixture-main" in resumed.output
    assert "--event custom-20261010T0000Z" in resumed.output
    with Storage(store) as storage:
        again = storage.get_run(2)
    assert again is not None
    assert again["params"]["scoring"]["cutoff"] == old.cutoff_text
    assert again["params"]["event_id"] == "custom-20261010T0000Z"
    assert "markers" not in again["params"]["scoring"]
    # The registry never takes it.
    refused = runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--registry", str(tmp_path / "registry")]
    )
    assert refused.exit_code == 2
    assert "custom-20261010T0000Z is a local-only custom event" in refused.output


# -- the registry gate ----------------------------------------------------------------


def _strikes_text() -> str:
    return FIXTURE_EVENTS.read_text(encoding="utf-8")


def _changes(head: str) -> E.EventChanges:
    base, _ = _parse(_strikes_text())
    after, problems = _parse(head)
    assert after is not None, problems
    return E.compare_events(base, after)


EXTRA = """
[[event.markers]]
id = "third-strike"
name = "Third strike"
time = "2026-10-09T12:00:00Z"
approximate = false
"""


def _add_marker(text: str, table: str = EXTRA) -> str:
    anchor = '\n[[event]]\nid = "fixture-second"'
    return text.replace(anchor, table + anchor)


def test_gate_adding_a_marker_inside_the_window_needs_no_label() -> None:
    changes = _changes(_add_marker(_strikes_text()))
    assert changes.guarded == []
    assert [(e, m.id) for e, m in changes.markers_added] == [("fixture-strikes", "third-strike")]
    summary = "\n".join(render_events(changes))
    assert "Added markers (no label needed" in summary and "third-strike" in summary


def test_gate_adding_a_marker_that_moves_the_window_is_guarded() -> None:
    early = EXTRA.replace("2026-10-09T12:00:00Z", "2026-10-07T00:00:00Z")
    text = _strikes_text().replace(
        '[[event.markers]]\nid = "first-strike"',
        early.strip() + '\n\n[[event.markers]]\nid = "first-strike"',
    )
    changes = _changes(text)
    assert any("silence window start" in item for item in changes.guarded)
    assert any("baseline end" in item for item in changes.guarded)


@pytest.mark.parametrize(
    ("change", "what"),
    [
        (
            lambda t: t.replace('time = "2026-10-08T18:00:00Z"', 'time = "2026-10-08T20:00:00Z"'),
            "marker second-strike time",
        ),
        (
            lambda t: t.replace("approximate = true", "approximate = false"),
            "marker second-strike approximate",
        ),
        (
            lambda t: (
                t[: t.index('\n[[event.markers]]\nid = "second-strike"')]
                + t[t.index('\n[[event]]\nid = "fixture-second"') :]
            ),
            "marker second-strike",
        ),
        (
            lambda t: t.replace("marker_margin_hours = 12.0", "marker_margin_hours = 6.0"),
            "marker_margin_hours",
        ),
    ],
    ids=["time", "approximate", "removed", "margin"],
)
def test_gate_guards_marker_changes_that_can_relabel(change: Any, what: str) -> None:
    changes = _changes(change(_strikes_text()))
    assert any(what in item for item in changes.guarded), changes.guarded


def test_gate_marker_descriptions_change_freely() -> None:
    text = (
        _strikes_text()
        .replace('name = "Second strike"', 'name = "Second strike, renamed"')
        .replace('notes = "The second fixture strike."', 'notes = "New notes."')
        .replace('sources = ["Agency B", "Agency C"]', 'sources = ["Agency D"]')
    )
    changes = _changes(text)
    assert changes.guarded == [] and changes.markers_described == [
        ("fixture-strikes", "second-strike")
    ]
    assert "Marker names, time notes, notes or sources changed" in "\n".join(render_events(changes))


def test_this_change_to_the_repository_events_file_is_high_impact() -> None:
    """The change from v0.2.0's events.toml to the current one moves the default
    event's cutoff and adds markers that move its windows: the gate needs the
    override label and an approving review for it."""
    base = E.load_events_file(V020_EVENTS, require_windows=True)
    head = E.load_events_file(REPO_EVENTS, require_windows=True)
    changes = E.compare_events(base, head)
    guarded = "\n".join(changes.guarded)
    assert "cutoff '2026-10-10T00:00:00+00:00' -> '2026-10-11T00:30:00+00:00'" in guarded
    assert "name" in guarded
    assert [m.id for _, m in changes.markers_added] == ["sasovo-hub-strike", "kaluga-region-strike"]
    assert not changes.default_changed and not changes.legacy_changed
