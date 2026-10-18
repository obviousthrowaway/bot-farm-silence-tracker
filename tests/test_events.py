"""Outage events: the events file, custom events, event selection in the CLI, the
per-event cache databases and their migration, and the gate's event policy."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker import events as E
from bot_farm_silence_tracker import storage as storage_module
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.config import load_config
from bot_farm_silence_tracker.pipeline import ScanSettings, run_scan
from bot_farm_silence_tracker.registry import records as R
from bot_farm_silence_tracker.registry import verify as V
from bot_farm_silence_tracker.registry.gate import PullRequest, render_gate, run_gate
from bot_farm_silence_tracker.registry.reports import build_drafts
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.storage import (
    BACKUP_SUFFIX,
    MIGRATION_LOG,
    Storage,
    event_db_path,
    legacy_db_path,
    migrate_legacy_db,
)
from bot_farm_silence_tracker.timeutil import isoformat

from . import test_registry_integrity as T
from .helpers import CUTOFF, EVENT, EVENTS, FIXTURE_EVENTS, NOW, SECOND
from .test_pipeline import SEEDS, make_source
from .test_registry_integrity import LAG, GateRepo, arch, honest

runner = CliRunner()
REPO_EVENTS = Path(__file__).resolve().parent.parent / "events.toml"
NOW_ARG = isoformat(NOW)
#: "Now" for the time rules of the events file and the registry records.
CLOCK = datetime(2026, 10, 12, tzinfo=UTC)


def toml_event(**fields: Any) -> str:
    """One ``[[event]]`` table; ``None`` leaves a field out."""
    values = {
        "id": "2026-10-other",
        "name": "Another outage",
        "cutoff": "2026-10-05T00:00:00Z",
        **fields,
    }
    lines = ["[[event]]"]
    for key, value in values.items():
        if value is None:
            continue
        lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def events_text(*tables: str, default: str = "2026-10-other") -> str:
    return f'default = "{default}"\n\n' + "\n".join(tables)


# -- the events file ---------------------------------------------------------------------


def test_the_repository_events_file_is_valid_and_its_default_keeps_the_algorithm() -> None:
    catalog = E.load_events_file(REPO_EVENTS)
    default = catalog.default
    assert catalog.legacy_id == default.id
    algorithm = ScoringParams(cutoff=default.cutoff)
    assert default.baseline_days == algorithm.baseline_days
    assert default.min_silence_days == algorithm.min_silence_days
    assert default.cliff_window_hours == algorithm.cliff_window_hours
    assert not default.custom and default.id.startswith(default.cutoff.strftime("%Y-%m-"))


def test_the_fixture_events_parse() -> None:
    assert EVENTS.ids == ("fixture-main", "fixture-strikes", "fixture-second")
    assert EVENT.id == "fixture-main" and EVENT.baseline_days == 30
    assert (SECOND.baseline_days, SECOND.min_silence_days, SECOND.cliff_window_hours) == (
        14,
        1.0,
        24.0,
    )
    assert SECOND.min_silence == timedelta(days=1)


def test_windows_default_to_the_algorithm() -> None:
    catalog, problems = E.parse_events(events_text(toml_event()), now=CLOCK)
    assert problems == [] and catalog is not None
    event = catalog.default
    assert (event.baseline_days, event.min_silence_days, event.cliff_window_hours) == (30, 3, 48)
    assert event.scoring_defaults()["cutoff"] == "2026-10-05T00:00:00+00:00"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("extra = 1\n" + events_text(toml_event()), "unknown top-level key"),
        (events_text(toml_event(colour="red")), "unknown key 'colour'"),
        (events_text(toml_event(id="Bad_ID")), "id must match"),
        (events_text(toml_event(id="ab")), "id must match"),
        (events_text(toml_event(id="custom-x1")), "reserved"),
        (events_text(toml_event(), toml_event()), "duplicate id"),
        (events_text(toml_event(name=None)), "name must be"),
        (events_text(toml_event(name="[link](https://x.invalid)")), "name must be"),
        (events_text(toml_event(name="a`b")), "name must be"),
        (events_text(toml_event(notes="# heading `code`")), "notes must be"),
        (events_text(toml_event(cutoff="2026-10-05")), "cutoff must be a UTC"),
        (events_text(toml_event(cutoff="2026-10-05T00:00:00+02:00")), "cutoff must be a UTC"),
        (events_text(toml_event(cutoff="2026-10-45T00:00:00Z")), "not a valid date"),
        (events_text(toml_event(cutoff="2026-10-05T00:00:30Z")), "to the minute"),
        (events_text(toml_event(cutoff="2026-10-30T00:00:00Z")), "in the future"),
        (
            events_text(
                toml_event(id="old-event", cutoff="2001-01-01T00:00:00Z"), default="old-event"
            ),
            "before",
        ),
        (events_text(toml_event(cutoff="2026-09-30T00:00:00Z")), "the id starts with 2026-10"),
        (
            events_text(
                toml_event(id="2026-10-later", cutoff="2026-10-08T00:00:00Z"),
                toml_event(id="2026-10-earlier", cutoff="2026-10-01T00:00:00Z"),
                default="2026-10-later",
            ),
            "cutoff order",
        ),
        (events_text(toml_event(baseline_days=0)), "baseline_days must be in"),
        (events_text(toml_event(baseline_days=2.5)), "whole number"),
        (events_text(toml_event(baseline_days=True)), "must be a number"),
        (events_text(toml_event(min_silence_days=0)), "min_silence_days must be in"),
        (events_text(toml_event(min_silence_days=0.5)), "min_silence_days must be in [1, 90]"),
        (events_text(toml_event(cliff_window_hours=721)), "cliff_window_hours must be in"),
        (events_text(toml_event(), default="nope"), "is not listed"),
        ("default = 3\n" + toml_event(), "'default' must name"),
        (events_text(toml_event()) + 'legacy_event = "nope"\n', "legacy_event"),
        ('default = "x"\n', "at least one [[event]]"),
        ("default = [\n", "not valid TOML"),
        ("[" * 5000 + "\n", "not valid TOML"),
    ],
)
def test_invalid_events_files_are_rejected(text: str, message: str) -> None:
    catalog, problems = E.parse_events(text, now=CLOCK)
    assert catalog is None
    assert any(message in p for p in problems), problems


def test_events_file_size_limits_apply_before_parsing() -> None:
    catalog, problems = E.parse_events(b"#" * (E.MAX_EVENTS_FILE_BYTES + 1), now=CLOCK)
    assert catalog is None and "larger than" in problems[0]
    many = events_text(
        *(
            toml_event(id=f"2026-10-e{i:03d}", name=f"Event {i}", notes="")
            for i in range(E.MAX_EVENTS + 1)
        ),
        default="2026-10-e000",
    )
    catalog, problems = E.parse_events(many, now=CLOCK)
    assert catalog is None and any("more than" in p for p in problems)
    catalog, problems = E.parse_events(b"\xff\xfe", now=CLOCK)
    assert catalog is None and "not UTF-8" in problems[0]


def test_load_events_reads_the_file_the_environment_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert E.default_events_path() == FIXTURE_EVENTS
    assert E.load_events().default_id == EVENT.id
    broken = tmp_path / "events.toml"
    broken.write_text("default = [\n")
    monkeypatch.setenv(E.EVENTS_ENV, str(broken))
    with pytest.raises(E.EventError, match="not valid TOML"):
        E.load_events()
    monkeypatch.delenv(E.EVENTS_ENV)
    assert E.default_events_path() == REPO_EVENTS  # A source checkout reads its own.


# -- custom events -------------------------------------------------------------------------


def test_custom_events_hold_their_cutoff_in_the_id() -> None:
    when = CUTOFF + timedelta(days=30, hours=6, minutes=5)
    event = E.custom_event(when)
    assert event.custom and event.id == "custom-" + when.strftime("%Y%m%dT%H%MZ")
    assert E.custom_event_from_id(event.id) == event
    assert EVENTS.resolve(event.id) == event
    assert E.parse_cutoff_option(when.strftime("%Y-%m-%dT%H:%MZ")) == event
    assert E.valid_event_id(event.id) and E.valid_event_id(EVENT.id)
    assert not E.valid_event_id("../escape") and not E.valid_event_id("Custom-2026")
    with pytest.raises(E.EventError, match="minute precision"):
        E.custom_event(when + timedelta(seconds=7))
    with pytest.raises(E.EventError, match="not a custom event id"):
        E.custom_event_from_id("custom-yesterday")
    with pytest.raises(E.EventError, match="valid date"):
        E.custom_event_from_id("custom-20261345T0000Z")
    with pytest.raises(E.EventError, match="--cutoff must be"):
        E.parse_cutoff_option("soon")
    with pytest.raises(E.EventError, match="unknown event"):
        EVENTS.resolve("nope")


def test_runs_made_before_events_are_matched_by_their_cutoff() -> None:
    assert E.event_of_run({"scoring": {"cutoff": EVENT.cutoff_text}}, EVENTS) == EVENT
    other = E.event_of_run({"scoring": {"cutoff": isoformat(CUTOFF - timedelta(days=1))}}, EVENTS)
    assert other.custom and other.cutoff == CUTOFF - timedelta(days=1)
    assert E.event_of_run({"event_id": SECOND.id, "scoring": {}}, EVENTS) == SECOND
    assert E.event_of_run({}, EVENTS) == EVENT
    with pytest.raises(E.EventError):
        E.event_of_run({"event_id": "gone-event"}, EVENTS)


def test_compare_events_tells_additions_from_guarded_changes() -> None:
    base, _ = E.parse_events(events_text(toml_event()), now=CLOCK)
    assert base is not None

    def changes(*tables: str, default: str = "2026-10-other") -> E.EventChanges:
        head, problems = E.parse_events(events_text(*tables, default=default), now=CLOCK)
        assert head is not None, problems
        return E.compare_events(base, head)

    added = changes(toml_event(), toml_event(id="2026-10-new", cutoff="2026-10-06T00:00:00Z"))
    assert [e.id for e in added.added] == ["2026-10-new"] and added.guarded == []
    moved = changes(toml_event(cutoff="2026-10-04T00:00:00Z"))
    assert len(moved.guarded) == 1 and "cutoff" in moved.guarded[0]
    windows = changes(toml_event(baseline_days=29, min_silence_days=2.0))
    assert "baseline_days" in windows.guarded[0] and "min_silence_days" in windows.guarded[0]
    assert changes(toml_event(name="Renamed outage")).guarded
    renamed = changes(toml_event(id="2026-10-renamed"), default="2026-10-renamed")
    assert renamed.removed and "removed or renamed" in renamed.guarded[0]
    notes = changes(toml_event(notes="New notes."))
    assert notes.guarded == [] and notes.notes_changed == ["2026-10-other"]
    same = changes(toml_event(baseline_days=30))  # The default, written out.
    assert not same.any
    first = E.compare_events(None, base)
    assert first.added and not first.guarded and not first.default_changed


# -- event selection in the CLI ----------------------------------------------------------


def _scan(*extra: str, db: Path | None = None) -> Any:
    args = ["scan", "--platform", "fake", "--n", "60", "--seed", "1", "--now", NOW_ARG]
    if db is not None:
        args += ["--db", str(db)]
    return runner.invoke(app, [*args, *extra, "--out", "o.csv"])


def _run_params(db: Path, run_id: int = 1) -> dict[str, Any]:
    with Storage(db) as store:
        run = store.get_run(run_id)
    assert run is not None
    return run["params"]


def test_events_list_and_show() -> None:
    listed = runner.invoke(app, ["events", "list"])
    assert listed.exit_code == 0, listed.output
    assert f"* {EVENT.id}  {EVENT.cutoff_text}" in listed.output
    assert f"  {SECOND.id}  {SECOND.cutoff_text}" in listed.output
    shown = runner.invoke(app, ["events", "show", SECOND.id])
    assert shown.exit_code == 0, shown.output
    assert "baseline_days: 14" in shown.output and "cliff_window_hours: 24" in shown.output
    assert f"registry folder: registry/events/{SECOND.id}/" in shown.output
    assert str(event_db_path(SECOND.id)) in shown.output
    custom = runner.invoke(app, ["events", "show", "custom-20260901T0000Z"])
    assert custom.exit_code == 0 and "local only" in custom.output
    unknown = runner.invoke(app, ["events", "show", "nope"])
    assert unknown.exit_code == 2 and "unknown event" in unknown.output


def test_scan_uses_the_default_event_and_its_own_database() -> None:
    result = _scan()
    assert result.exit_code == 0, result.output
    assert f"Event: {EVENT.id} (outage event: {EVENT.name})" in result.output
    db = event_db_path(EVENT.id)
    params = _run_params(db)
    assert params["event_id"] == EVENT.id
    assert params["scoring"]["cutoff"] == EVENT.cutoff_text
    assert not legacy_db_path().exists()


def test_scan_with_an_event_takes_its_cutoff_windows_and_database() -> None:
    result = _scan("--event", SECOND.id)
    assert result.exit_code == 0, result.output
    params = _run_params(event_db_path(SECOND.id))
    assert params["event_id"] == SECOND.id
    scoring = params["scoring"]
    assert scoring["cutoff"] == SECOND.cutoff_text
    assert (scoring["baseline_days"], scoring["min_silence_days"]) == (14, 1.0)
    assert scoring["cliff_window_hours"] == 24.0
    assert not event_db_path(EVENT.id).exists()
    # A window given on the command line still wins over the event's.
    wider = _scan("--event", SECOND.id, "--baseline-days", "20")
    assert wider.exit_code == 0, wider.output
    assert _run_params(event_db_path(SECOND.id), 2)["scoring"]["baseline_days"] == 20
    assert f"report --run-id 2 --event {SECOND.id}" in wider.output


def test_a_custom_cutoff_makes_a_local_event_with_its_own_database() -> None:
    day = (CUTOFF - timedelta(days=2)).strftime("%Y-%m-%d")
    custom = E.parse_cutoff_option(day)
    result = _scan("--cutoff", day)
    assert result.exit_code == 0, result.output
    assert "custom event, local only" in result.output
    assert _run_params(event_db_path(custom.id))["event_id"] == custom.id
    assert not event_db_path(EVENT.id).exists()
    same = _scan("--cutoff", day, "--event", custom.id)
    assert same.exit_code == 0, same.output
    both = _scan("--cutoff", day, "--event", EVENT.id)
    assert both.exit_code == 2 and "give one of them" in both.output
    seconds = _scan("--cutoff", f"{day}T00:00:30Z")
    assert seconds.exit_code == 2 and "minute precision" in seconds.output
    future = _scan("--cutoff", (NOW + timedelta(days=1)).strftime("%Y-%m-%d"))
    assert future.exit_code == 2 and "in the past" in future.output


def test_unknown_events_are_refused() -> None:
    result = _scan("--event", "nope")
    assert result.exit_code == 2 and "unknown event 'nope'" in result.output


def test_config_set_default_event_changes_the_default_and_keeps_the_platforms() -> None:
    assert runner.invoke(app, ["config", "set-platforms", "bluesky,reddit"]).exit_code == 0
    result = runner.invoke(app, ["config", "set-default-event", SECOND.id])
    assert result.exit_code == 0, result.output
    assert f"default_event: {SECOND.id} (cutoff {SECOND.cutoff_text})" in result.output
    loaded = load_config()
    assert loaded.default_event == SECOND.id and loaded.platforms == ("bluesky", "reddit")
    assert runner.invoke(app, ["config", "set-default", "reddit"]).exit_code == 0
    assert load_config().default_event == SECOND.id  # Kept by other config changes.
    assert f"default_event: {SECOND.id}" in runner.invoke(app, ["config", "show"]).output
    listed = runner.invoke(app, ["events", "list"])
    assert f"* {SECOND.id}" in listed.output
    scanned = _scan()
    assert scanned.exit_code == 0, scanned.output
    assert _run_params(event_db_path(SECOND.id))["event_id"] == SECOND.id
    refused = runner.invoke(app, ["config", "set-default-event", "nope"])
    assert refused.exit_code == 2 and "unknown event" in refused.output


def test_a_config_default_event_that_is_gone_falls_back_with_a_warning() -> None:
    path = Path(os.environ["BOT_FARM_SILENCE_TRACKER_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'platforms = ["fake"]\ndefault_platform = "fake"\ndefault_event = "gone-event"\n'
    )
    result = _scan()
    assert result.exit_code == 0, result.output
    assert "default_event 'gone-event' is not an event" in result.output
    assert _run_params(event_db_path(EVENT.id))["event_id"] == EVENT.id


def test_report_and_export_refuse_a_run_of_another_event(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    assert _scan("--event", SECOND.id, db=db).exit_code == 0
    own = runner.invoke(app, ["report", "--run-id", "1", "--db", str(db), "--out", "r.csv"])
    assert own.exit_code == 0, own.output  # Without --event: the run's own event.
    assert f"Event: {SECOND.id}" in own.output
    other = runner.invoke(
        app, ["report", "--run-id", "1", "--db", str(db), "--event", EVENT.id, "--out", "r.csv"]
    )
    assert other.exit_code == 2 and f"belongs to the outage event {SECOND.id}" in other.output
    out = tmp_path / "private" / "ids.csv"
    export = runner.invoke(
        app,
        ["export-ids", "--run-id", "1", "--db", str(db), "--event", EVENT.id, "--out", str(out)],
    )
    assert export.exit_code == 2 and "belongs to the outage event" in export.output
    fine = runner.invoke(
        app,
        ["export-ids", "--run-id", "1", "--event", SECOND.id, "--db", str(db), "--out", str(out)],
    )
    assert fine.exit_code == 0, fine.output


# -- custom events never reach the registry ----------------------------------------------


def _relabel(db: Path) -> None:
    conn = sqlite3.connect(db)
    for table in ("accounts", "activities", "runs", "run_sample"):
        conn.execute(f"UPDATE {table} SET platform = 'reddit-archive' WHERE platform = 'fake'")
    conn.execute("UPDATE accounts SET handle = user_id")
    conn.commit()
    conn.close()


def test_custom_events_are_isolated_from_the_registry(tmp_path: Path) -> None:
    day = (CUTOFF - timedelta(days=2)).strftime("%Y-%m-%d")
    custom = E.parse_cutoff_option(day)
    db = tmp_path / "c.db"
    assert _scan("--cutoff", day, db=db).exit_code == 0
    _relabel(db)
    root = tmp_path / "registry"
    for args in (
        ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)],
        ["registry", "add", "--run-id", "1", "--event", custom.id, "--db", str(db),
         "--registry", str(root)],
        ["registry", "evidence", "--run-id", "1", "--account", "x", "--db", str(db),
         "--registry", str(root)],
        ["report", "--registry", str(root), "--event", custom.id, "--platform", "reddit-archive",
         "--out", str(tmp_path / "drafts")],
    ):  # fmt: skip
        root.mkdir(exist_ok=True)
        result = runner.invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
        assert "custom event" in result.output, result.output
    assert not any(p.is_file() for p in root.rglob("*"))
    skip = _scan("--cutoff", day, "--skip-registered-within-days", "5", "--registry", str(root))
    assert skip.exit_code == 2 and "no records of the custom event" in skip.output
    # A record that claims a custom event is invalid.
    record = arch("someone", status="scanned", cliff=False, event_id=custom.id)
    assert any("event_id" in e for e in R.record_errors(record, events=EVENTS))


def test_registry_add_puts_a_run_in_its_events_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(R, "current_time", lambda: NOW + timedelta(days=1))
    db = tmp_path / "c.db"
    assert _scan("--event", SECOND.id, db=db).exit_code == 0
    _relabel(db)
    root = tmp_path / "registry"
    refused = runner.invoke(
        app,
        ["registry", "add", "--run-id", "1", "--db", str(db), "--event", EVENT.id,
         "--registry", str(root)],
    )  # fmt: skip
    assert refused.exit_code == 2 and "belongs to the outage event" in refused.output
    added = runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    assert added.exit_code == 0, added.output
    assert f"event {SECOND.id}:" in added.output
    files = [p.relative_to(root).as_posix() for p in root.rglob("*.jsonl")]
    assert files and all(f.startswith(f"events/{SECOND.id}/reddit-archive/") for f in files)
    records = R.load_records(root, events=EVENTS)
    assert {key[0] for key in records} == {SECOND.id}
    assert R.validate_registry(root, now=NOW + timedelta(days=1)) == []


# -- the same account in two events ---------------------------------------------------------


def test_one_account_has_a_record_per_event_and_drafts_say_so(tmp_path: Path) -> None:
    first = arch(
        "someone",
        status="verified_inauthentic",
        evidence=R.evidence_path("reddit-archive", R.b64e("someone")),
    )
    second = {**arch("someone", status="scanned", cliff=False), "event_id": SECOND.id}
    root = tmp_path / "registry"
    R.write_records(root, [first, second])
    evidence = root / R.registry_evidence_path(EVENT.id, "reddit-archive", R.b64e("someone"))
    evidence.parent.mkdir(parents=True)
    evidence.write_text("# Evidence\n")
    assert R.validate_registry(root, now=CLOCK) == []
    records = R.load_records(root, events=EVENTS, now=CLOCK)
    assert set(records) == {
        (EVENT.id, "reddit-archive", R.b64e("someone")),
        (SECOND.id, "reddit-archive", R.b64e("someone")),
    }
    drafts = build_drafts(
        root, "verified_inauthentic", "reddit-archive", tmp_path / "out",
        event_id=EVENT.id, events=EVENTS,
    )  # fmt: skip
    assert len(drafts) == 1 and drafts[0].also_in == (SECOND.id,)
    text = drafts[0].path.read_text(encoding="utf-8")
    assert f"also appears in event {R.md_code(SECOND.id)}" in text
    assert drafts[0].path.parent.parent.name == EVENT.id


def test_validate_flags_records_in_the_wrong_folder_and_unknown_events(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    record = arch("someone", status="scanned", cliff=False)
    R.write_records(root, [record])
    shard = root / R.record_path(EVENT.id, "reddit-archive", record["id_b64"])
    moved = root / R.record_path(SECOND.id, "reddit-archive", record["id_b64"])
    moved.parent.mkdir(parents=True)
    shard.rename(moved)
    problems = R.validate_registry(root, now=CLOCK)
    assert any("does not match its folder" in p for p in problems), problems
    unknown = root / "events" / "2026-10-nowhere" / "reddit-archive" / moved.name
    unknown.parent.mkdir(parents=True)
    moved.rename(unknown)
    problems = R.validate_registry(root, now=CLOCK)
    assert any("which is not an event" in p for p in problems), problems
    # Files of the layout before events are unexpected.
    (root / "reddit-archive").mkdir()
    (root / "reddit-archive" / "00.jsonl").write_text(R.canonical_line(record) + "\n")
    assert any("unexpected file" in p for p in R.validate_registry(root, now=CLOCK))


# -- the cache databases and their migration ------------------------------------------------


def _legacy_db_with_a_run() -> Path:
    """An old single database with one run made before events (no event_id)."""
    legacy = legacy_db_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    with Storage(legacy) as store:
        settings = ScanSettings(
            platform="fake", scoring=ScoringParams(cutoff=CUTOFF, now=NOW), n=40
        )
        run_scan(make_source(), SEEDS, store, settings)
        fallback = store.spend_fallback_path
    fallback.write_text('{"note": "fallback"}\n')
    return legacy


def test_the_old_database_moves_into_the_default_events_folder(tmp_path: Path) -> None:
    legacy = _legacy_db_with_a_run()
    out = tmp_path / "r.csv"
    result = runner.invoke(app, ["report", "--run-id", "1", "--out", str(out)])
    assert result.exit_code == 0, result.output
    target = event_db_path(EVENT.id)
    assert target.is_file() and not legacy.exists()
    assert not list(legacy.parent.glob(legacy.name + "*"))
    assert (target.parent / (target.name + ".spend-fallback.jsonl")).is_file()
    assert "Moved the cache database" in result.output and "atomic rename" in result.output
    note = (legacy.parent / MIGRATION_LOG).read_text(encoding="utf-8")
    assert str(legacy) in note and str(target) in note
    assert f"Event: {EVENT.id}" in result.output  # The old run is matched by its cutoff.
    # The moved database is the same file: it reads as before.
    with Storage(target) as store:
        assert store.get_run(1) is not None and "event_id" not in store.get_run(1)["params"]
    again = runner.invoke(app, ["report", "--run-id", "1", "--out", str(out)])
    assert again.exit_code == 0 and "Moved" not in again.output


def test_another_event_never_takes_the_old_database() -> None:
    legacy = _legacy_db_with_a_run()
    assert _scan("--event", SECOND.id).exit_code == 0
    assert legacy.is_file() and not event_db_path(EVENT.id).exists()


def test_a_move_across_devices_copies_and_keeps_a_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_db_with_a_run()
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if Path(src).parent != Path(dst).parent:  # Another folder stands for another device.
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", replace)
    target = event_db_path(EVENT.id)
    assert migrate_legacy_db(legacy, target) == target
    backup = legacy.with_name(legacy.name + BACKUP_SUFFIX)
    assert backup.is_file() and not legacy.exists()
    assert target.read_bytes() == backup.read_bytes()
    assert not list(target.parent.glob("*.migrating"))
    note = (legacy.parent / MIGRATION_LOG).read_text(encoding="utf-8")
    assert "by a copy (another device)" in note and BACKUP_SUFFIX in note
    with Storage(target) as store:
        assert store.get_run(1) is not None
    # The backup is not migrated again.
    assert migrate_legacy_db(legacy, target) == target


def test_a_database_that_cannot_be_moved_is_used_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_db_with_a_run()
    target = event_db_path(EVENT.id)
    real = os.replace
    calls: list[str] = []

    def failing(src: Any, dst: Any) -> None:
        calls.append(Path(src).name)
        if Path(src) == legacy:
            raise OSError(errno.EACCES, "Permission denied")
        real(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", failing)
    assert migrate_legacy_db(legacy, target) == legacy
    # What was moved before the failure is put back.
    assert legacy.is_file() and not target.exists()
    assert legacy.with_name(legacy.name + ".spend-fallback.jsonl").is_file()
    not_a_db = legacy.parent / "other.db"
    not_a_db.write_text("not a database")
    assert migrate_legacy_db(not_a_db, legacy.parent / "events" / "x" / "y.db") == not_a_db


def test_event_db_paths_stay_inside_the_cache_folder() -> None:
    with pytest.raises(ValueError):
        event_db_path("../../etc")
    assert event_db_path(EVENT.id).parent.parent.parent == legacy_db_path().parent


# -- the gate's event policy -------------------------------------------------------------------


def _write_events(repo: GateRepo, text: str) -> None:
    (repo.path / "events.toml").write_text(text, encoding="utf-8")


def _fixture_text() -> str:
    return FIXTURE_EVENTS.read_text(encoding="utf-8")


def _proven(repo: GateRepo, head: str) -> PullRequest:
    """A pull request whose override label has both proofs (see the gate module)."""
    return PullRequest(
        7,
        repo.base,
        head,
        frozenset({V.OVERRIDE_LABEL}),
        action="labeled",
        event_label=V.OVERRIDE_LABEL,
        author="contributor",
        head_reviewed=True,
    )


THIRD = """
[[event]]
id = "2026-10-third"
name = "Fixture third outage"
cutoff = "2026-10-11T00:00:00Z"
baseline_days = 30
min_silence_days = 3.0
cliff_window_hours = 48.0
notes = "Added by a pull request."
"""


def test_gate_passes_a_new_event_without_the_label(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    second = '[[event]]\nid = "fixture-second"'
    _write_events(repo, _fixture_text().replace(second, THIRD.strip() + "\n\n" + second))
    result = repo.gate()
    assert result.ok, render_gate(result)
    summary = render_gate(result)
    assert "### Outage events" in summary and "Added events (no label needed)" in summary
    assert R.md_code("2026-10-third") in summary and "High-impact" not in summary


def test_gate_passes_a_new_event_with_its_first_records(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    # Listed last, so its cutoff may not be before the second event's.
    _write_events(repo, _fixture_text() + THIRD.replace("2026-10-11", "2026-10-12"))
    record = {**arch("someone", status="scanned", cliff=False), "event_id": "2026-10-third"}
    R.write_records(repo.path / "registry", [record])
    result = repo.gate(honest(record))
    assert result.ok, render_gate(result)
    assert result.report is not None and result.report.checked == 1


@pytest.mark.parametrize(
    ("change", "what"),
    [
        (
            lambda t: t.replace(
                'cutoff = "2026-10-12T00:00:00Z"', 'cutoff = "2026-10-11T00:00:00Z"'
            ),
            "cutoff",
        ),
        (
            lambda t: t.replace("min_silence_days = 1.0", "min_silence_days = 2.0"),
            "min_silence_days",
        ),
        (lambda t: t.replace("baseline_days = 14", "baseline_days = 15"), "baseline_days"),
        (
            lambda t: t.replace("cliff_window_hours = 24.0", "cliff_window_hours = 12.0"),
            "cliff_window_hours",
        ),
        (
            lambda t: t.replace('name = "Fixture second outage"', 'name = "Fixture other outage"'),
            "name",
        ),
        (
            lambda t: t.replace('id = "fixture-second"', 'id = "fixture-renamed"'),
            "removed or renamed",
        ),
        (lambda t: t[: t.index('[[event]]\nid = "fixture-second"')], "removed or renamed"),
    ],
    ids=["cutoff", "silence", "baseline", "cliff-window", "name", "rename", "delete"],
)
def test_gate_guards_changes_to_existing_events(tmp_path: Path, change: Any, what: str) -> None:
    repo = GateRepo(tmp_path / "r", [])
    _write_events(repo, change(_fixture_text()))
    result = repo.gate()
    assert not result.ok, render_gate(result)
    assert any("changes to existing outage events" in f and what in f for f in result.failures)
    summary = render_gate(result)
    assert "**High-impact event changes** (need the `registry-override` label" in summary
    # The label alone, without an approving review of this head, is not enough.
    head = repo.commit()
    unreviewed = PullRequest(
        7, repo.base, head, frozenset({V.OVERRIDE_LABEL}), action="labeled",
        event_label=V.OVERRIDE_LABEL, author="contributor",
    )  # fmt: skip
    assert not run_gate(repo.path, unreviewed, honest(), now=CLOCK, lag=LAG).ok
    # A label that was added for an earlier head is not enough either.
    stale = PullRequest(
        7, repo.base, head, frozenset({V.OVERRIDE_LABEL}), action="edited",
        author="contributor", head_reviewed=True,
    )  # fmt: skip
    assert not run_gate(repo.path, stale, honest(), now=CLOCK, lag=LAG).ok
    # Both proofs: allowed, and still listed as high-impact.
    allowed = run_gate(repo.path, _proven(repo, head), honest(), now=CLOCK, lag=LAG)
    assert allowed.ok, render_gate(allowed)
    assert allowed.events_overridden
    assert "allowed by the `registry-override` label" in render_gate(allowed)


def test_gate_fails_records_in_the_wrong_folder_or_of_an_unknown_event(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    record = arch("someone", status="scanned", cliff=False)
    R.write_records(repo.path / "registry", [record])
    shard = repo.path / "registry" / R.record_path(EVENT.id, "reddit-archive", record["id_b64"])
    wrong = repo.path / "registry" / R.record_path(SECOND.id, "reddit-archive", record["id_b64"])
    wrong.parent.mkdir(parents=True)
    shard.rename(wrong)
    result = repo.gate(honest(record))
    assert not result.ok and any("does not match its folder" in p for p in result.validation)

    other = GateRepo(tmp_path / "o", [])
    stray = {**record, "event_id": "2026-10-nowhere"}
    R.write_records(other.path / "registry", [stray])
    result = other.gate(honest(stray))
    assert not result.ok
    assert any("not an event" in p for p in result.validation), result.validation
    labelled = other.gate(honest(stray), labels=(V.OVERRIDE_LABEL,))
    assert not labelled.ok  # No label makes an unknown event valid.


def test_gate_fails_an_invalid_or_missing_events_file(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    _write_events(
        repo, _fixture_text().replace('cutoff = "2026-10-12T00:00:00Z"', 'cutoff = "soon"')
    )
    result = repo.gate()
    assert not result.ok and any("cutoff must be" in p for p in result.validation)
    assert "events.toml" in render_gate(result)
    gone = GateRepo(tmp_path / "g", [])
    (gone.path / "events.toml").unlink()
    result = gone.gate(labels=(V.OVERRIDE_LABEL,))
    assert not result.ok and any("events.toml is missing" in p for p in result.validation)
    linked = GateRepo(tmp_path / "l", [])
    (linked.path / "events.toml").unlink()
    (linked.path / "events.toml").symlink_to("/etc/hosts")
    result = linked.gate()
    assert not result.ok and any("mode 120000" in p for p in result.validation)


def test_gate_fails_an_events_change_together_with_code(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    _write_events(repo, _fixture_text() + THIRD.replace("2026-10-11", "2026-10-12"))
    (repo.path / "README.md").write_text("changed\n")
    result = repo.gate()
    assert not result.ok
    assert any("events.toml" in f and "separate pull requests" in f for f in result.failures)


def test_gate_spot_checks_with_the_events_cutoff(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    record = {**arch("someone", status="scanned", cliff=False), "event_id": SECOND.id}
    R.write_records(repo.path / "registry", [record])
    seen: list[Any] = []

    class Probe:
        requests_made = 0

        def use_events(self, events: E.EventCatalog | None) -> None:
            seen.append(events)

        def newest(self, rec: dict[str, Any], since: datetime, until: datetime) -> Any:
            return honest(record).newest(rec, since, until)

    result = repo.gate(Probe())  # type: ignore[arg-type]
    assert result.ok, render_gate(result)
    assert seen and seen[0] is not None and seen[0].get(SECOND.id) == SECOND


def test_every_package_ships_the_events_file() -> None:
    root = REPO_EVENTS.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"events.toml" = "bot_farm_silence_tracker/_data/events.toml"' in pyproject
    build = (root / "packaging" / "build_binary.sh").read_text(encoding="utf-8")
    assert '--add-data "${ROOT}/events.toml${SEP}bot_farm_silence_tracker/_data"' in build
    gui = (root / "packaging" / "build_gui.sh").read_text(encoding="utf-8")
    assert '--add-data "${ROOT}/events.toml${SEP}bot_farm_silence_tracker/_data"' in gui
    assert "COPY events.toml ./" in (root / "Dockerfile").read_text(encoding="utf-8")
    owners = (root / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "/events.toml @obviousthrowaway" in owners


# -- review follow-ups -----------------------------------------------------------------------


def test_the_default_windows_are_the_algorithms_and_shared_files_spell_them_out() -> None:
    algorithm = ScoringParams(cutoff=CUTOFF)
    assert {
        "baseline_days": algorithm.baseline_days,
        "min_silence_days": algorithm.min_silence_days,
        "cliff_window_hours": algorithm.cliff_window_hours,
    } == E.DEFAULT_WINDOWS
    # The registry reads the file strictly: a window left to the code's default could
    # be moved by a code-only pull request, which the gate does not judge.
    catalog, problems = E.parse_events(events_text(toml_event()), now=CLOCK, require_windows=True)
    assert catalog is None and any("must set baseline_days" in p for p in problems)
    for path in (REPO_EVENTS, FIXTURE_EVENTS):
        catalog, problems = E.parse_events(path.read_bytes(), now=CLOCK, require_windows=True)
        assert catalog is not None, problems


def test_gate_fails_a_new_event_that_leaves_its_windows_to_the_code(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    bare = THIRD.replace("baseline_days = 30\n", "").replace("2026-10-11", "2026-10-12")
    _write_events(repo, _fixture_text() + bare)
    result = repo.gate()
    assert not result.ok and any("must set baseline_days" in p for p in result.validation)


def test_old_runs_keep_their_event_when_the_default_changes() -> None:
    text = FIXTURE_EVENTS.read_text(encoding="utf-8").replace(
        'default = "fixture-main"', 'default = "fixture-second"'
    )
    catalog, problems = E.parse_events(text, now=CLOCK)
    assert catalog is not None, problems
    assert catalog.default_id == SECOND.id and catalog.legacy_id == EVENT.id
    assert catalog.for_cutoff(CUTOFF).id == EVENT.id
    assert catalog.for_cutoff(SECOND.cutoff).id == SECOND.id
    moved = text.replace('legacy_event = "fixture-main"', 'legacy_event = "fixture-second"')
    head, _ = E.parse_events(moved, now=CLOCK)
    assert head is not None
    changes = E.compare_events(catalog, head)
    assert any("legacy_event changes" in item for item in changes.guarded)


def test_gate_guards_a_move_of_the_legacy_event(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    _write_events(
        repo,
        _fixture_text().replace('legacy_event = "fixture-main"', 'legacy_event = "fixture-second"'),
    )
    result = repo.gate()
    assert not result.ok and any("legacy_event" in f for f in result.failures)
    head = repo.commit()
    assert run_gate(repo.path, _proven(repo, head), honest(), now=CLOCK, lag=LAG).ok


def test_registry_add_refuses_a_run_scored_with_another_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(R, "current_time", lambda: NOW + timedelta(days=1))
    db = tmp_path / "c.db"
    assert _scan(db=db).exit_code == 0
    _relabel(db)
    # The registry's events file has moved the event's cutoff since the scan.
    root = tmp_path / "repo" / "registry"
    root.mkdir(parents=True)
    moved = FIXTURE_EVENTS.read_text(encoding="utf-8").replace(
        'cutoff = "2026-10-10T00:00:00Z"', 'cutoff = "2026-10-09T00:00:00Z"'
    )
    (root.parent / "events.toml").write_text(moved, encoding="utf-8")
    result = runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    assert result.exit_code == 2 and "was scored with the cutoff" in result.output
    # Windows that differ from the event's are pointed out.
    other = tmp_path / "d.db"
    assert _scan("--baseline-days", "20", db=other).exit_code == 0
    _relabel(other)
    noted = runner.invoke(
        app,
        ["registry", "add", "--run-id", "1", "--db", str(other), "--registry",
         str(tmp_path / "registry")],
    )  # fmt: skip
    assert noted.exit_code == 0, noted.output
    assert f"note: run 1 used baseline_days 20, the event {EVENT.id} 30" in noted.output


def test_export_all_runs_with_an_event_keeps_only_its_runs(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    assert _scan(db=db).exit_code == 0
    assert _scan("--event", SECOND.id, db=db).exit_code == 0
    out = tmp_path / "private" / "ids.json"
    result = runner.invoke(
        app,
        ["export-ids", "--all-runs", "--event", SECOND.id, "--db", str(db), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "Left out 1 runs of other events" in result.output
    assert {row["run_id"] for row in json.loads(out.read_text())["rows"]} == {2}


def test_a_concurrent_move_is_finished_by_one_process_only() -> None:
    import threading

    from bot_farm_silence_tracker.storage import MIGRATION_LOCK, fallback_lock

    legacy = _legacy_db_with_a_run()
    target = event_db_path(EVENT.id)
    holding, release, results = threading.Event(), threading.Event(), []

    def winner() -> None:
        with fallback_lock(legacy.parent / MIGRATION_LOCK):
            holding.set()
            release.wait(5)
            target.parent.mkdir(parents=True, exist_ok=True)
            for path in sorted(legacy.parent.glob(legacy.name + "*")):
                os.replace(path, target.with_name(target.name + path.name[len(legacy.name) :]))

    first = threading.Thread(target=winner)
    first.start()
    assert holding.wait(5)
    second = threading.Thread(target=lambda: results.append(migrate_legacy_db(legacy, target)))
    second.start()
    second.join(0.5)
    assert second.is_alive()  # It waits for the lock instead of racing.
    release.set()
    first.join(5)
    second.join(5)
    assert results == [target]  # Never the old path, which no longer exists.
    assert not legacy.exists()


def test_a_database_that_reappears_at_the_old_path_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    legacy = _legacy_db_with_a_run()
    target = event_db_path(EVENT.id)
    assert migrate_legacy_db(legacy, target) == target
    # An older version opens the old path again (this version follows the move).
    sqlite3.connect(legacy).execute("CREATE TABLE runs (run_id INTEGER)").connection.close()
    with caplog.at_level("WARNING", logger="bot_farm_silence_tracker.storage"):
        assert migrate_legacy_db(legacy, target) == target
    assert any("exists at the old path" in r.getMessage() for r in caplog.records)


# -- second review ----------------------------------------------------------------------------


def test_the_shared_file_names_its_legacy_event_so_a_new_default_needs_no_label(
    tmp_path: Path,
) -> None:
    catalog, problems = E.parse_events(
        _fixture_text().replace('legacy_event = "fixture-main"\n', ""),
        now=CLOCK,
        require_windows=True,
    )
    assert catalog is None and any("must set 'legacy_event'" in p for p in problems)
    # Adding an event and making it the default: no label, the legacy event stays.
    repo = GateRepo(tmp_path / "r", [])
    text = _fixture_text().replace('default = "fixture-main"', 'default = "2026-10-third"')
    _write_events(repo, text + THIRD.replace("2026-10-11", "2026-10-12"))
    result = repo.gate()
    assert result.ok, render_gate(result)
    summary = render_gate(result)
    assert "the default event changes from" in summary and "no label needed" in summary


def test_skip_registered_reads_records_with_their_events_windows(tmp_path: Path) -> None:
    from bot_farm_silence_tracker.registry.build import registered_ids

    root = tmp_path / "registry"
    # A cliff of the second event, silent for 1.5 days: valid there (min 1 day),
    # though less than the 3 days of the default event.
    short = {
        **arch(
            "someone",
            status="candidate",
            scan_window_end=isoformat(T.T + timedelta(days=1, hours=12)),
            last_scanned_at=isoformat(T.T + timedelta(days=1, hours=12)),
            first_scanned_at=isoformat(T.T + timedelta(days=1, hours=12)),
            status_updated_at=isoformat(T.T + timedelta(days=1, hours=12)),
        ),
        "event_id": SECOND.id,
    }
    assert R.record_errors(short, events=EVENTS, now=CLOCK) == []
    R.write_records(root, [short])
    found = registered_ids(root, "reddit-archive", CLOCK, 60, SECOND.id)
    assert found == {"someone"}
    assert registered_ids(root, "reddit-archive", CLOCK, 60, EVENT.id) == set()


def test_a_resume_command_printed_before_events_still_works(tmp_path: Path) -> None:
    legacy = _legacy_db_with_a_run()
    day = CUTOFF.strftime("%Y-%m-%d")
    old_style = ["scan", "--platform", "fake", "--from-run", "1", "--cutoff", day,
                 "--now", NOW_ARG, "--out", str(tmp_path / "o.csv")]  # fmt: skip
    result = runner.invoke(app, old_style)
    assert result.exit_code == 0, result.output
    assert f"is the cutoff of the outage event {EVENT.id}" in result.output
    assert not legacy.exists() and event_db_path(EVENT.id).is_file()
    assert not event_db_path(E.custom_event(CUTOFF).id).exists()  # No empty custom store.
    assert _run_params(event_db_path(EVENT.id), 2)["event_id"] == EVENT.id
    # The same with --db naming a database made before events.
    other = tmp_path / "old.db"
    with Storage(other) as store:
        settings = ScanSettings(
            platform="fake", scoring=ScoringParams(cutoff=CUTOFF, now=NOW), n=40
        )
        run_scan(make_source(), SEEDS, store, settings)
    with_db = runner.invoke(app, [*old_style, "--db", str(other)])
    assert with_db.exit_code == 0, with_db.output
    assert _run_params(other, 2)["event_id"] == EVENT.id


def test_no_move_without_the_migration_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = _legacy_db_with_a_run()

    def refuse(handle: Any) -> None:
        raise OSError("locking is not available")

    monkeypatch.setattr(storage_module, "_lock_exclusive", refuse)
    target = event_db_path(EVENT.id)
    assert migrate_legacy_db(legacy, target) == legacy
    assert legacy.is_file() and not target.exists()


def test_the_installers_platform_setup_keeps_the_default_event() -> None:
    # The .pkg postinstall, Setup.exe and install.sh run these two commands.
    assert runner.invoke(app, ["config", "set-default-event", SECOND.id]).exit_code == 0
    unset = runner.invoke(app, ["config", "set-platforms", "--if-unset", "reddit-archive"])
    assert unset.exit_code == 0, unset.output
    assert load_config().default_event == SECOND.id
    assert load_config().platforms == ("reddit-archive",)
    kept = runner.invoke(app, ["config", "set-platforms", "--if-unset", "bluesky"])
    assert kept.exit_code == 0 and "Kept the platforms" in kept.output
    chosen = runner.invoke(app, ["config", "set-platforms", "reddit-archive,bluesky"])
    assert chosen.exit_code == 0, chosen.output
    loaded = load_config()
    assert loaded.default_event == SECOND.id and loaded.platforms == ("reddit-archive", "bluesky")
