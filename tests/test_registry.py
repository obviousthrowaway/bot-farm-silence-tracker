"""The shared registry: format, merge semantics, validation, add, skip, evidence,
report drafts and export-ids."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.controls import ClusterTest, SignalContext
from bot_farm_silence_tracker.registry import build as build_module
from bot_farm_silence_tracker.registry import records as records_module
from bot_farm_silence_tracker.registry.build import (
    exclusion_reason,
    fired_signals,
    is_candidate,
    registered_ids,
    run_records,
)
from bot_farm_silence_tracker.registry.evidence import parse_evidence
from bot_farm_silence_tracker.registry.records import (
    SCHEMA_FILE,
    STATUS_CANDIDATE,
    STATUS_CLEARED,
    STATUS_SCANNED,
    STATUS_VERIFIED,
    b64d,
    b64e,
    canonical_line,
    load_records,
    merge_records,
    record_errors,
    record_key,
    record_path,
    schema_text,
    shard_of,
    validate_registry,
    write_records,
)
from bot_farm_silence_tracker.registry.reports import build_drafts, render_bluesky, render_reddit
from bot_farm_silence_tracker.scoring import AccountResult, ScoringParams
from bot_farm_silence_tracker.storage import Storage
from bot_farm_silence_tracker.timeutil import isoformat

from .helpers import CUTOFF, EVENT, EVENTS, NOW

runner = CliRunner()
NOW_ARG = "2026-10-14T00:00:00Z"
LATER_ARG = "2026-10-16T00:00:00Z"
REPO = Path(__file__).resolve().parent.parent
#: "Now" for the registry's time rules: after every record in these tests.
CLOCK = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(records_module, "current_time", lambda: CLOCK)


def relabel(db: Path, platform: str = "reddit-archive") -> None:
    """Make a fake scan look like a ``platform`` scan: the registry refuses ``fake``.

    Handles become the user IDs, as the archive stores them (the identity rule).
    """
    conn = sqlite3.connect(db)
    for table in ("accounts", "activities", "runs", "run_sample"):
        conn.execute(f"UPDATE {table} SET platform = ? WHERE platform = 'fake'", (platform,))
    conn.execute("UPDATE accounts SET handle = user_id")
    conn.commit()
    conn.close()


def record(**changes: Any) -> dict[str, Any]:
    base = {
        "event_id": EVENT.id,
        "platform": "reddit-archive",
        "id_b64": b64e("some_author"),
        "username_b64": b64e("some_author"),
        "first_scanned_at": "2026-10-14T00:00:00+00:00",
        "last_scanned_at": "2026-10-14T00:00:00+00:00",
        "last_activity_at": isoformat(CUTOFF - timedelta(hours=4)),
        "scan_window_end": "2026-10-13T23:00:00+00:00",
        "baseline_posts": 120,
        "baseline_active_days": 30,
        "cliff": True,
        "tool_version": "0.1.0",
        "status": STATUS_CANDIDATE,
        "evidence": None,
        "status_updated_at": "2026-10-14T00:00:00+00:00",
    }
    base.update(changes)
    return base


@pytest.fixture(scope="module")
def scanned_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fake scan (run 1) and a later rescan of the same sample (run 2)."""
    root = tmp_path_factory.mktemp("scan")
    db = root / "c.db"
    for now in (NOW_ARG,):
        result = runner.invoke(
            app,
            [
                "scan", "--platform", "fake", "--n", "300", "--seed", "1", "--now", now,
                "--db", str(db), "--out", str(root / "o.csv"),
            ],
        )  # fmt: skip
        assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [
            "scan", "--platform", "fake", "--from-run", "1", "--refresh", "--now", LATER_ARG,
            "--db", str(db), "--out", str(root / "o2.csv"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    relabel(db)
    return db


@pytest.fixture
def db(scanned_db: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "c.db"
    shutil.copy(scanned_db, copy)
    return copy


# -- format --------------------------------------------------------------------------


def test_base64_round_trip_is_urlsafe_without_padding() -> None:
    for text in ("a", "ab", "abc", "did:plc:abc123", "名前_x", "1234567890"):
        encoded = b64e(text)
        assert "=" not in encoded and "+" not in encoded and "/" not in encoded
        assert b64d(encoded) == text
    with pytest.raises(ValueError):
        b64d("not base64!")


def test_shard_is_a_hash_prefix_that_spreads_bluesky_dids() -> None:
    import hashlib

    assert shard_of("AbCdef") == hashlib.sha256(b"AbCdef").hexdigest()[:2]
    assert record_path(EVENT.id, "bluesky", "ZGlk") == (
        f"events/{EVENT.id}/bluesky/{shard_of('ZGlk')}.jsonl"
    )
    # Every DID encodes to the prefix ZGlk; the hash still spreads them out.
    dids = {shard_of(b64e(f"did:plc:{i:024d}")) for i in range(200)}
    assert len(dids) > 100


def test_schema_file_in_the_repository_matches_the_code() -> None:
    assert (REPO / "registry" / SCHEMA_FILE).read_text(encoding="utf-8") == schema_text()
    schema = json.loads(schema_text())
    assert "score" not in schema["properties"]
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "bot"}, "status"),
        ({"cliff": 1}, "cliff"),
        ({"baseline_posts": True}, "baseline_posts"),
        ({"baseline_posts": -1}, "minimum"),
        ({"score": 0.9}, "unknown field 'score'"),
        ({"last_activity_at": CUTOFF.date().isoformat()}, "last_activity_at"),
        ({"event_id": "Not An Event"}, "event_id"),
        ({"status": STATUS_VERIFIED}, "needs an evidence file"),
        ({"evidence": "../secrets.md"}, "evidence"),
        ({"evidence": "evidence/reddit-archive/other.md"}, "evidence must be"),
        ({"id_b64": "c29tZQ=="}, "id_b64"),
        ({"first_scanned_at": "2026-10-15T00:00:00+00:00"}, "first_scanned_at"),
        ({"scan_window_end": "2026-10-15T00:00:00+00:00"}, "scan_window_end"),
        ({"last_activity_at": "2026-10-14T00:00:00+00:00"}, "last_activity_at"),
    ],
)
def test_record_errors_catch_bad_records(changes: dict[str, Any], message: str) -> None:
    errors = record_errors(record(**changes))
    assert any(message in e for e in errors), errors


def test_a_good_record_has_no_errors() -> None:
    assert record_errors(record()) == []
    evidence = f"evidence/reddit-archive/{b64e('some_author')}.md"
    assert record_errors(record(status=STATUS_VERIFIED, evidence=evidence)) == []
    missing = dict(record())
    del missing["evidence"]
    assert "missing field 'evidence'" in record_errors(missing)


def test_write_is_deterministic_sorted_and_minimal(tmp_path: Path) -> None:
    rows = [record(id_b64=b64e(f"author{i}"), username_b64=b64e(f"author{i}")) for i in range(30)]
    first = write_records(tmp_path / "a", rows)
    write_records(tmp_path / "b", list(reversed(rows)))
    files = sorted(p.relative_to(tmp_path / "a").as_posix() for p in (tmp_path / "a").rglob("*.*"))
    assert files == sorted(first)
    for rel in first:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()
    # A second write of the same records changes nothing.
    assert write_records(tmp_path / "a", rows) == []
    # Changing one record touches only its shard.
    rows[3] = {**rows[3], "baseline_posts": 999}
    assert write_records(tmp_path / "a", rows) == [
        record_path(EVENT.id, "reddit-archive", rows[3]["id_b64"])
    ]
    assert list((tmp_path / "a").glob("*/*.jsonl")) == []  # No records outside events/.
    for file in (tmp_path / "a").glob("events/*/*/*.jsonl"):
        text = file.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert text.endswith("\n") and "\r" not in text
        ids = [json.loads(line)["id_b64"] for line in lines]
        assert ids == sorted(ids)
        assert all(line == canonical_line(json.loads(line)) for line in lines)


# -- merge semantics -----------------------------------------------------------------


def test_merge_newer_scan_updates_facts_and_keeps_extremes() -> None:
    old = record(status=STATUS_SCANNED, cliff=False, last_activity_at="2026-10-11T00:00:00+00:00")
    new = record(
        first_scanned_at="2026-10-16T00:00:00+00:00",
        last_scanned_at="2026-10-16T00:00:00+00:00",
        scan_window_end="2026-10-16T00:00:00+00:00",
        last_activity_at=isoformat(CUTOFF - timedelta(hours=4)),
        baseline_posts=5,
        status=STATUS_CANDIDATE,
        status_updated_at="2026-10-16T00:00:00+00:00",
    )
    merged = merge_records(old, new)
    assert merged == merge_records(new, old)  # Symmetric.
    assert merged["first_scanned_at"] == old["first_scanned_at"]
    assert merged["last_scanned_at"] == new["last_scanned_at"]
    assert merged["last_activity_at"] == "2026-10-11T00:00:00+00:00"  # The max.
    assert merged["baseline_posts"] == 5
    assert merged["status"] == STATUS_CANDIDATE
    assert merged["status_updated_at"] == "2026-10-16T00:00:00+00:00"
    assert record_errors(merged) == []


def test_merge_an_older_scan_changes_only_first_scanned_at() -> None:
    current = record(first_scanned_at="2026-10-14T00:00:00+00:00")
    older = record(
        first_scanned_at="2026-10-12T00:00:00+00:00",
        last_scanned_at="2026-10-12T00:00:00+00:00",
        scan_window_end="2026-10-12T00:00:00+00:00",
        status=STATUS_SCANNED,
        status_updated_at="2026-10-12T00:00:00+00:00",
    )
    merged = merge_records(current, older)
    assert merged == {**current, "first_scanned_at": "2026-10-12T00:00:00+00:00"}


@pytest.mark.parametrize("human", [STATUS_VERIFIED, STATUS_CLEARED])
def test_merge_never_changes_a_human_status(human: str) -> None:
    evidence = f"evidence/reddit-archive/{b64e('some_author')}.md"
    reviewed = record(
        status=human, evidence=evidence, status_updated_at="2026-10-15T00:00:00+00:00"
    )
    rescan = record(
        last_scanned_at="2026-10-20T00:00:00+00:00",
        scan_window_end="2026-10-20T00:00:00+00:00",
        status=STATUS_SCANNED,
        status_updated_at="2026-10-20T00:00:00+00:00",
    )
    merged = merge_records(reviewed, rescan)
    assert merged["status"] == human
    assert merged["evidence"] == evidence
    assert merged["status_updated_at"] == "2026-10-15T00:00:00+00:00"
    assert merged["last_scanned_at"] == "2026-10-20T00:00:00+00:00"


def test_merge_two_human_statuses_newer_wins_and_ties_clear() -> None:
    evidence = f"evidence/reddit-archive/{b64e('some_author')}.md"
    verified = record(status=STATUS_VERIFIED, evidence=evidence)
    cleared = record(status=STATUS_CLEARED)
    assert merge_records(verified, cleared)["status"] == STATUS_CLEARED
    later = {**verified, "status_updated_at": "2026-10-15T00:00:00+00:00"}
    assert merge_records(later, cleared)["status"] == STATUS_VERIFIED


# -- validate ------------------------------------------------------------------------


def test_validate_passes_a_written_registry_and_fails_on_problems(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    rows = [record(id_b64=b64e(f"author{i}"), username_b64=b64e(f"author{i}")) for i in range(5)]
    write_records(root, rows)
    assert validate_registry(root) == []

    # A duplicate in another shard file.
    other_shard = f"{(int(shard_of(rows[0]['id_b64']), 16) + 1) % 256:02x}"
    stray = root / "events" / EVENT.id / "reddit-archive" / f"{other_shard}.jsonl"
    stray.write_text(stray.read_text() if stray.exists() else "", encoding="utf-8")
    stray.write_text(stray.read_text() + canonical_line(rows[0]) + "\n", encoding="utf-8")
    problems = validate_registry(root)
    assert any("duplicate" in p for p in problems)
    assert any("belongs in" in p for p in problems)
    stray.unlink()

    # A schema error.
    shard = root / record_path(EVENT.id, "reddit-archive", rows[1]["id_b64"])
    bad = {**rows[1], "status": "bot"}
    shard.write_text(shard.read_text().replace(canonical_line(rows[1]), canonical_line(bad)))
    assert any("status" in p for p in validate_registry(root))
    write_records(root, rows)

    # verified_inauthentic whose evidence file does not exist.
    evidence = f"evidence/reddit-archive/{rows[2]['id_b64']}.md"
    rows[2] = {**rows[2], "status": STATUS_VERIFIED, "evidence": evidence}
    write_records(root, rows)
    assert any("does not exist" in p for p in validate_registry(root))
    (root / "events" / EVENT.id / evidence).parent.mkdir(parents=True)
    (root / "events" / EVENT.id / evidence).write_text("# Evidence\n", encoding="utf-8")
    assert validate_registry(root) == []

    # Unsorted lines are not canonical.
    shard = root / record_path(EVENT.id, "reddit-archive", rows[0]["id_b64"])
    lines = shard.read_text().splitlines()
    if len(lines) > 1:
        shard.write_text("\n".join(reversed(lines)) + "\n")
        assert any("canonical" in p for p in validate_registry(root))


def test_cli_validate_exit_codes(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    write_records(root, [record()])
    assert runner.invoke(app, ["registry", "validate", "--registry", str(root)]).exit_code == 0
    shard = root / record_path(EVENT.id, "reddit-archive", record()["id_b64"])
    shard.write_text(shard.read_text() + shard.read_text())
    result = runner.invoke(app, ["registry", "validate", "--registry", str(root)])
    assert result.exit_code == 1
    assert "duplicate" in result.output


def test_cli_merge_dedupes_across_shards_and_registries(tmp_path: Path) -> None:
    root, other = tmp_path / "registry", tmp_path / "other"
    write_records(root, [record()])
    newer = record(
        last_scanned_at="2026-10-16T00:00:00+00:00", scan_window_end="2026-10-16T00:00:00+00:00"
    )
    write_records(other, [newer, record(id_b64=b64e("xx2"), username_b64=b64e("xx2"))])
    # The same account again, in the wrong shard of the main registry.
    (root / "events" / EVENT.id / "reddit-archive" / "zz.jsonl").write_text(
        canonical_line(record()) + "\n"
    )
    result = runner.invoke(
        app, ["registry", "merge", "--registry", str(root), "--from", str(other)]
    )
    assert result.exit_code == 0, result.output
    assert validate_registry(root) == []
    records = load_records(root)
    assert len(records) == 2
    assert records[(EVENT.id, "reddit-archive", b64e("some_author"))]["last_scanned_at"] == (
        "2026-10-16T00:00:00+00:00"
    )


# -- add -----------------------------------------------------------------------------


def _context(
    params: ScoringParams, p_adjusted: float | None = None, size: int = 3
) -> SignalContext:
    """A context whose cluster 1 has the given adjusted p-value (none: untested)."""
    clusters = {}
    if p_adjusted is not None:
        clusters[1] = ClusterTest(1, size, CUTOFF, p_adjusted, p_adjusted, "cutoff", -1.0)
    return SignalContext(params, clusters)


def test_candidate_rule_and_signals() -> None:
    params = ScoringParams(cutoff=CUTOFF, now=NOW)
    context = _context(params)
    # A low p_silence alone is no signal any more: every old cliff has one.
    silent = AccountResult("reddit-archive", "u", "h", "active", cliff=True, p_silence=1e-9)
    assert fired_signals(silent, context) == []
    regular = AccountResult("reddit-archive", "u", "h", "active", cliff=True, interarrival_cv=0.2)
    signals = fired_signals(regular, context)
    assert [s.name for s in signals] == ["regular_timing"]
    assert is_candidate(regular, signals)
    provisional = AccountResult("fake", "u", "h", "active", cliff=True, provisional=True)
    assert not is_candidate(provisional, signals)


@pytest.mark.parametrize(
    ("size", "p_adjusted", "fires"),
    [
        (2, 0.001, False),  # A pair never fires (size >= 3).
        (3, 0.02, False),  # Above 0.01 after the adjustment.
        (3, 0.01, True),
        (6, None, False),  # Not tested: no fire.
    ],
)
def test_synchronized_stop_needs_three_and_a_small_permutation_p(
    size: int, p_adjusted: float | None, fires: bool
) -> None:
    params = ScoringParams(cutoff=CUTOFF, now=NOW)
    clustered = AccountResult("x", "u", "h", "active", cliff=True, cluster_size=size, cluster_id=1)
    signals = fired_signals(clustered, _context(params, p_adjusted, size))
    names = [s.name for s in signals]
    assert ("synchronized_stop" in names) is fires
    # A shared stop alone never makes a candidate: it depends on the outage's timing.
    assert not is_candidate(clustered, signals)


@pytest.mark.parametrize(
    ("entropy", "bins", "posts", "fires"),
    [
        (4.3, 24, 49, False),  # Too few posts, whatever the spread.
        (4.0, 20, 60, False),  # 4.0 + 19 / (120 ln 2) = 4.23 < 4.3.
        (4.1, 24, 60, True),  # 4.1 + 23 / (120 ln 2) = 4.38.
        (4.25, 24, 1000, False),  # Large n: almost no correction.
        (4.31, 24, 1000, True),
    ],
)
def test_round_the_clock_uses_miller_madow_and_a_minimum_n(
    entropy: float, bins: int, posts: int, fires: bool
) -> None:
    params = ScoringParams(cutoff=CUTOFF, now=NOW)
    result = AccountResult(
        "x", "u", "h", "active", cliff=True, hour_entropy=entropy, hour_bins=bins,
        baseline_posts=posts,
    )  # fmt: skip
    names = [s.name for s in fired_signals(result, _context(params))]
    assert ("round_the_clock" in names) is fires
    assert build_module.miller_madow_bits(4.0, 20, 60) == pytest.approx(
        4.0 + 19 / (120 * 0.693147), abs=1e-4
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("opted_out", "opted_out"),
        ("suspended", "unavailable"),
        ("deleted", "unavailable"),
        ("private", "unavailable"),
        ("error", "not_fully_scanned"),
        ("skipped", "not_fully_scanned"),
    ],
)
def test_exclusion_reasons(status: str, reason: str) -> None:
    assert exclusion_reason(AccountResult("fake", "u", "h", status)) == reason


def test_add_builds_valid_deterministic_records(db: Path, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    result = runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "added" in result.output and "unavailable" in result.output
    assert validate_registry(root) == []
    records = load_records(root)
    with Storage(db) as storage:
        built = run_records(storage, 1, EVENTS)
    assert len(records) == len(built.records) > 0
    statuses = {r["status"] for r in records.values()}
    assert statuses == {STATUS_SCANNED, STATUS_CANDIDATE}
    for rec in records.values():
        assert "score" not in rec
        result_ = built.results[b64d(rec["id_b64"])]
        if rec["status"] == STATUS_CANDIDATE:
            assert result_.cliff and not result_.provisional
    # Running it again changes no file.
    snapshot = {p: p.read_bytes() for p in root.rglob("*.jsonl")}
    again = runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    assert "0 files changed" in again.output
    assert {p: p.read_bytes() for p in root.rglob("*.jsonl")} == snapshot


def _flagged(built_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in built_records if r["cliff"] or r["status"] == STATUS_CANDIDATE]


def test_flagged_only_writes_cliff_and_candidate_records_and_counts_the_rest(
    db: Path, tmp_path: Path
) -> None:
    with Storage(db) as storage:
        built = run_records(storage, 1, EVENTS)
    flagged = _flagged(built.records)
    other = len(built.records) - len(flagged)
    assert flagged and other > 0  # The fixture has both kinds.
    root = tmp_path / "registry"
    result = runner.invoke(
        app,
        ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root),
         "--no-live-check", "--flagged-only"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert f"{len(flagged)} added" in result.output
    assert f"Skipped as unflagged (--flagged-only): {other} fully scanned accounts" in result.output
    assert "0 already have an unflagged record" in result.output
    records = load_records(root)
    assert {(r["platform"], r["id_b64"]) for r in records.values()} == {
        (r["platform"], r["id_b64"]) for r in flagged
    }
    assert all(r["cliff"] or r["status"] == STATUS_CANDIDATE for r in records.values())
    # The file format and the checks are the same as without the flag.
    assert validate_registry(root) == []


def test_without_flagged_only_every_fully_scanned_account_is_written(
    db: Path, tmp_path: Path
) -> None:
    with Storage(db) as storage:
        built = run_records(storage, 1, EVENTS)
    root = tmp_path / "registry"
    result = runner.invoke(
        app,
        ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root),
         "--no-live-check"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Skipped as unflagged" not in result.output
    assert len(load_records(root)) == len(built.records)


def _add_args(db: Path, root: Path, run_id: int) -> list[str]:
    return ["registry", "add", "--run-id", str(run_id), "--db", str(db), "--registry", str(root),
            "--no-live-check"]  # fmt: skip


def _unflagged_keys(db: Path, run_id: int) -> set[tuple[str, str, str]]:
    with Storage(db) as storage:
        built = run_records(storage, run_id, EVENTS)
    return {record_key(r) for r in built.records if r not in _flagged(built.records)}


def test_flagged_only_leaves_an_existing_unflagged_record_as_it_is(
    db: Path, tmp_path: Path
) -> None:
    root = tmp_path / "registry"
    assert runner.invoke(app, _add_args(db, root, 1)).exit_code == 0
    before = load_records(root)
    unflagged = _unflagged_keys(db, 2)
    assert unflagged and unflagged <= set(before)
    again = runner.invoke(app, [*_add_args(db, root, 2), "--flagged-only"])
    assert again.exit_code == 0, again.output
    assert f"{len(unflagged)} fully scanned accounts" in again.output
    assert f"of which {len(unflagged)} already have an unflagged record" in again.output
    after = load_records(root)
    for key in unflagged:
        assert after[key] == before[key]
        assert after[key]["last_scanned_at"] == "2026-10-14T00:00:00+00:00"
    # Without the flag the same second run does update them, so the check above bites.
    plain_root = tmp_path / "plain"
    assert runner.invoke(app, _add_args(db, plain_root, 1)).exit_code == 0
    assert runner.invoke(app, _add_args(db, plain_root, 2)).exit_code == 0
    plain = load_records(plain_root)
    assert any(plain[key]["last_scanned_at"] != before[key]["last_scanned_at"] for key in unflagged)


def test_flagged_only_still_demotes_an_existing_flagged_record(db: Path, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    assert runner.invoke(app, _add_args(db, root, 1)).exit_code == 0
    records = load_records(root)
    victim = sorted(_unflagged_keys(db, 2))[0]
    # The registry holds a stale accusation: run 2's scan of this account is unflagged.
    records[victim] = {
        **records[victim],
        "cliff": True,
        "status": STATUS_CANDIDATE,
        "last_activity_at": isoformat(
            datetime.fromisoformat(records[victim]["scan_window_end"]) - timedelta(days=4)
        ),
        "status_updated_at": "2026-10-14T00:00:00+00:00",
    }
    write_records(root, records.values())
    assert load_records(root)[victim]["status"] == STATUS_CANDIDATE
    result = runner.invoke(app, [*_add_args(db, root, 2), "--flagged-only"])
    assert result.exit_code == 0, result.output
    assert "1 back to scanned" in result.output
    demoted = load_records(root)[victim]
    assert demoted["status"] == STATUS_SCANNED
    assert demoted["cliff"] is False
    assert demoted["last_scanned_at"] == "2026-10-16T00:00:00+00:00"
    assert validate_registry(root) == []


def test_flagged_only_output_passes_registry_validate(db: Path, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    result = runner.invoke(
        app,
        ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root),
         "--no-live-check", "--flagged-only"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    validated = runner.invoke(app, ["registry", "validate", "--registry", str(root)])
    assert validated.exit_code == 0, validated.output


def test_add_excludes_opted_out_accounts(db: Path, tmp_path: Path) -> None:
    with Storage(db) as storage:
        built = run_records(storage, 1, EVENTS)
        victim = sorted(built.results)[0]
        storage._conn.execute(
            "UPDATE accounts SET status = 'opted_out', handle = user_id WHERE user_id = ?",
            (victim,),
        )
        storage._conn.commit()
        rebuilt = run_records(storage, 1, EVENTS)
    assert victim not in rebuilt.results
    assert rebuilt.excluded.get("opted_out") == 1
    assert b64e(victim) not in {r["id_b64"] for r in rebuilt.records}


def test_add_a_newer_run_updates_but_never_touches_human_statuses(db: Path, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    records = load_records(root)
    keys = sorted(records)
    verified_key, cleared_key = keys[0], keys[1]
    evidence = f"evidence/reddit-archive/{verified_key[2]}.md"
    records[verified_key] = {
        **records[verified_key],
        "status": STATUS_VERIFIED,
        "evidence": evidence,
    }
    records[cleared_key] = {**records[cleared_key], "status": STATUS_CLEARED}
    write_records(root, records.values())
    result = runner.invoke(
        app, ["registry", "add", "--run-id", "2", "--db", str(db), "--registry", str(root)]
    )
    assert result.exit_code == 0, result.output
    after = load_records(root)
    assert after[verified_key]["status"] == STATUS_VERIFIED
    assert after[verified_key]["evidence"] == evidence
    assert after[cleared_key]["status"] == STATUS_CLEARED
    updated = [r for r in after.values() if r["last_scanned_at"] == "2026-10-16T00:00:00+00:00"]
    assert updated
    assert all(r["first_scanned_at"] == "2026-10-14T00:00:00+00:00" for r in updated)
    for key, rec in after.items():
        if key in records:
            assert rec["last_activity_at"] >= records[key]["last_activity_at"]


def test_add_refuses_an_invalid_registry(db: Path, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    shard = root / "events" / EVENT.id / "reddit-archive" / "00.jsonl"
    shard.parent.mkdir(parents=True)
    shard.write_text("{not json\n")
    result = runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    assert result.exit_code == 2
    assert "invalid JSON" in result.output
    assert shard.read_text() == "{not json\n"


# -- skip registered -----------------------------------------------------------------


def test_scan_skips_accounts_registered_within_days(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "registry"
    runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    registered = registered_ids(root, "reddit-archive", NOW, 5, EVENT.id)
    assert registered and registered == {b64d(r["id_b64"]) for r in load_records(root).values()}
    assert registered_ids(root, "reddit-archive", NOW.replace(day=30), 5, EVENT.id) == set()
    assert registered_ids(root, "bluesky", NOW, 5, EVENT.id) == set()
    # Records of one event never count for another.
    assert registered_ids(root, "reddit-archive", NOW, 5, "fixture-second") == set()
    # The scan below runs on the fake platform; the registry holds the same accounts
    # as reddit-archive records.
    real = build_module.registered_ids
    monkeypatch.setattr(
        build_module,
        "registered_ids",
        lambda root, platform, now, days, event: real(root, "reddit-archive", now, days, event),
    )
    out = tmp_path / "o.json"
    result = runner.invoke(
        app,
        [
            "scan", "--platform", "fake", "--n", "100", "--seed", "3", "--now", NOW_ARG,
            "--db", str(tmp_path / "fresh.db"), "--out", str(out),
            "--skip-registered-within-days", "5", "--registry", str(root),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output
    with Storage(tmp_path / "fresh.db") as storage:
        sampled = {a.user_id for a, _ in storage.get_run_sample(1)}
    assert len(sampled) == 100
    assert not sampled & registered


def test_skip_registered_needs_a_registry_and_no_resume(db: Path, tmp_path: Path) -> None:
    base = ["scan", "--platform", "fake", "--n", "10", "--now", NOW_ARG, "--db", str(db)]
    missing = runner.invoke(
        app, [*base, "--skip-registered-within-days", "5", "--registry", str(tmp_path / "none")]
    )
    assert missing.exit_code == 2 and "no registry directory" in missing.output
    resume_args = [*base, "--from-run", "1", "--skip-registered-within-days", "5"]
    resume_args[2] = "reddit-archive"  # The run's platform (see relabel).
    resume = runner.invoke(app, resume_args)
    assert resume.exit_code == 2 and "--from-run" in resume.output
    lone = runner.invoke(app, [*base, "--registry", str(tmp_path)])
    assert lone.exit_code == 2 and "--skip-registered-within-days" in lone.output


# -- evidence ------------------------------------------------------------------------


def test_evidence_draft_has_the_facts_and_no_decoded_identity(db: Path, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    with Storage(db) as storage:
        built = run_records(storage, 1, EVENTS)
    clustered = next(r for r in built.results.values() if r.cliff and r.cluster_id)
    result = runner.invoke(
        app,
        [
            "registry", "evidence", "--run-id", "1", "--account", clustered.handle,
            "--db", str(db), "--registry", str(root),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    path = (
        root
        / "events"
        / EVENT.id
        / "evidence"
        / "reddit-archive"
        / (f"{b64e(clustered.user_id)}.md")
    )
    text = path.read_text(encoding="utf-8")
    assert clustered.handle not in text and clustered.user_id not in text
    assert clustered.handle not in result.output
    for heading in ("## Scan facts", "## Signals that fired", "## Stop-time cluster",
                    "## Hour profile (baseline, UTC)", "## Public links"):  # fmt: skip
        assert heading in text
    summary = parse_evidence(text)
    assert summary["id_b64"] == b64e(clustered.user_id)
    assert summary["event_id"] == EVENT.id and EVENT.name in text
    assert summary["cluster"]["size"] == clustered.cluster_size
    assert "p_value" not in summary["cluster"]  # The uniform null's p-value is gone.
    tested = summary["cluster"]["p_adjusted"] is not None
    assert tested is (clustered.cluster_size >= 3)
    if tested:
        assert 0 < summary["cluster"]["p_permutation"] <= summary["cluster"]["p_adjusted"] <= 1
    assert sum(summary["hour_profile_utc"]) == clustered.baseline_posts
    fires = tested and summary["cluster"]["p_adjusted"] <= 0.01
    assert ("synchronized_stop" in {s["name"] for s in summary["signals"]}) is fires
    assert summary["steady"]["recent_posts"] == clustered.recent_posts
    assert summary["placebo"]["headline"].startswith("PLACEBO:")
    assert summary["live_profile"]["status"] == "unknown (archive only)"
    assert "- Live profile checked: not checked" in text and "- Content evidence:" in text
    # The drafted evidence satisfies validate for a verified record.
    records = load_records(root)
    key = (EVENT.id, "reddit-archive", b64e(clustered.user_id))
    records[key] = {
        **records[key],
        "status": STATUS_VERIFIED,
        "evidence": f"evidence/reddit-archive/{key[2]}.md",
    }
    write_records(root, records.values())
    assert validate_registry(root) == []
    # By ID works too, and an unknown account fails cleanly.
    by_id = runner.invoke(
        app,
        ["registry", "evidence", "--run-id", "1", "--account", clustered.user_id, "--db", str(db),
         "--out", str(tmp_path / "e.md")],
    )  # fmt: skip
    assert by_id.exit_code == 0 and (tmp_path / "e.md").read_text(encoding="utf-8") == text
    unknown = runner.invoke(
        app, ["registry", "evidence", "--run-id", "1", "--account", "nobody", "--db", str(db)]
    )
    assert unknown.exit_code == 2


def test_parse_evidence_errors() -> None:
    with pytest.raises(ValueError):
        parse_evidence("# no summary")
    with pytest.raises(ValueError):
        parse_evidence("```json\n[1]\n```")


# -- report drafts -------------------------------------------------------------------


def _evidence_summary(platform: str) -> dict[str, Any]:
    return {
        "cluster": {"size": 4, "p_adjusted": 0.002, "sync_minutes": 30.0},
        "signals": [{"name": "regular_timing", "value": 0.2, "threshold": "<= 0.5"}],
        "communities": {"worldnews": 40, "politics": 12},
        "links": ["https://www.reddit.com/user/{username}/"] if platform != "bluesky" else
        ["https://bsky.app/profile/{id}"],
    }  # fmt: skip


#: A scan three weeks after the cutoff: long enough silence for a verified record.
VERIFIED_SCAN = {
    key: "2026-10-31T00:00:00+00:00"
    for key in ("first_scanned_at", "last_scanned_at", "scan_window_end", "status_updated_at")
}


def test_report_drafts_decode_names_only_locally(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    reddit = record(
        status=STATUS_VERIFIED,
        evidence=f"evidence/reddit-archive/{b64e('some_author')}.md",
        **VERIFIED_SCAN,
    )
    did = "did:plc:abcdefghijklmnop"
    bsky = record(
        platform="bluesky",
        id_b64=b64e(did),
        username_b64=b64e("someone.bsky.social"),
        status=STATUS_VERIFIED,
        evidence=f"evidence/bluesky/{b64e(did)}.md",
        **VERIFIED_SCAN,
    )
    other = record(id_b64=b64e("not_verified"), username_b64=b64e("not_verified"))
    write_records(root, [reddit, bsky, other])
    for rec in (reddit, bsky):
        path = root / "events" / EVENT.id / rec["evidence"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Evidence\n\n```json\n" + json.dumps(_evidence_summary(rec["platform"])) + "\n```\n"
        )
    assert validate_registry(root) == []
    out = tmp_path / "drafts"
    result = runner.invoke(
        app,
        ["report", "--registry", str(root), "--platform", "reddit-archive", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "Nothing was submitted" in result.output
    drafts = list(out.rglob("*.md"))
    assert len(drafts) == 1
    text = drafts[0].read_text(encoding="utf-8")
    assert "u/some_author" in text and "https://www.reddit.com/user/some_author/" in text
    assert "reddit.com/report" in text and "Spam" in text
    # Communities and links come from the platform templates, never the evidence file.
    assert "worldnews" not in text and "message/compose?to=/r/COMMUNITY" in text
    assert "Nothing was submitted" in text
    assert "not_verified" not in text
    # The draft names the event and its cutoff, in the summary and in the pasted text.
    assert drafts[0].parent.parent.name == EVENT.id
    assert f"Outage event (as reported): ` {EVENT.name} `" in text and EVENT.cutoff_text in text
    assert f"just before a reported outage (event {EVENT.id}, cutoff {EVENT.cutoff_text}" in text
    bluesky = render_bluesky(bsky, _evidence_summary("bluesky"))
    assert "Report account" in bluesky and f"https://bsky.app/profile/{did}" in bluesky
    assert "Spam" in bluesky and "Misleading" in bluesky
    # Registry files keep names encoded.
    assert "some_author" not in "".join(p.read_text() for p in root.rglob("*.jsonl"))


def test_report_drafts_refuse_bad_targets(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    write_records(root, [record()])
    inside = runner.invoke(
        app,
        [
            "report",
            "--registry",
            str(root),
            "--platform",
            "reddit-archive",
            "--out",
            str(root / "d"),
        ],
    )
    assert inside.exit_code == 2 and "outside the registry" in inside.output
    x = runner.invoke(
        app, ["report", "--registry", str(root), "--platform", "x", "--out", str(tmp_path / "d")]
    )
    assert x.exit_code == 2 and "no report draft template" in x.output
    both = runner.invoke(app, ["report", "--registry", str(root), "--run-id", "1"])
    assert both.exit_code == 2
    neither = runner.invoke(app, ["report"])
    assert neither.exit_code == 2
    with pytest.raises(ValueError):
        build_drafts(
            root, STATUS_VERIFIED, "fake", tmp_path / "d", event_id=EVENT.id, events=EVENTS
        )
    with pytest.raises(ValueError, match="custom events"):
        build_drafts(
            root, STATUS_VERIFIED, "reddit-archive", tmp_path / "d", event_id="nope", events=EVENTS
        )
    custom = runner.invoke(
        app,
        ["report", "--registry", str(root), "--platform", "reddit-archive", "--out",
         str(tmp_path / "d"), "--event", "custom-20261001T0000Z"],
    )  # fmt: skip
    assert custom.exit_code == 2 and "local-only custom event" in custom.output
    assert "u/some_author" in render_reddit(record(), None)


# -- export-ids ----------------------------------------------------------------------


def test_export_ids_csv_and_json(db: Path, tmp_path: Path) -> None:
    out = tmp_path / "private" / "ids.csv"
    result = runner.invoke(app, ["export-ids", "--run-id", "1", "--db", str(db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "contains real platform user IDs" in result.output
    lines = out.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    assert header[:3] == ["run_id", "handle", "user_id"] and "cliff" in header
    with Storage(db) as storage:
        built = run_records(storage, 1, EVENTS)
    some = next(iter(built.results.values()))
    assert any(f"{some.handle},{some.user_id}," in line for line in lines)
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    out_json = tmp_path / "ids.json"
    result = runner.invoke(
        app, ["export-ids", "--all-runs", "--db", str(db), "--out", str(out_json)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert "private" in payload["private"].lower() or "Keep it private" in payload["private"]
    assert {row["run_id"] for row in payload["rows"]} == {1, 2}


def test_export_ids_needs_exactly_one_selector(db: Path, tmp_path: Path) -> None:
    out = str(tmp_path / "x.csv")
    assert runner.invoke(app, ["export-ids", "--db", str(db), "--out", out]).exit_code == 2
    both = ["export-ids", "--run-id", "1", "--all-runs", "--db", str(db), "--out", out]
    assert runner.invoke(app, both).exit_code == 2
    missing = ["export-ids", "--run-id", "99", "--db", str(db), "--out", out]
    assert runner.invoke(app, missing).exit_code == 2


def test_the_live_check_keeps_removed_accounts_from_becoming_candidates(db: Path) -> None:
    from bot_farm_silence_tracker.registry.live import ARCHIVE_ONLY, LiveStatus

    with Storage(db) as storage:
        plain = run_records(storage, 1, EVENTS)
        candidates = [r for r in plain.records if r["status"] == STATUS_CANDIDATE]
        assert candidates  # Reddit's "unknown (archive only)" does not block.
        assert {s.status for s in plain.live.values()} == {ARCHIVE_ONLY}
        assert len(plain.live) == len(candidates) and plain.live_blocked == {}
        asked: list[tuple[str, str]] = []

        def suspended(platform: str, user_id: str, handle: str) -> LiveStatus:
            asked.append((platform, user_id))
            return LiveStatus("suspended", "test")

        removed = run_records(storage, 1, EVENTS, suspended)
    # Only accounts that would become candidates are checked.
    assert len(asked) == len(candidates)
    assert all(r["status"] == STATUS_SCANNED for r in removed.records)
    assert removed.live_blocked == {"suspended": len(candidates)}


def test_registry_add_reports_accounts_the_live_check_kept_scanned(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bot_farm_silence_tracker import cli
    from bot_farm_silence_tracker.registry.live import LiveStatus

    class Deleted:
        def check(self, platform: str, user_id: str, handle: str) -> LiveStatus:
            return LiveStatus("deleted", "test")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "_live_checker", Deleted)
    root = tmp_path / "registry"
    result = runner.invoke(
        app, ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "Kept scanned by the live profile check, not candidate:" in result.output
    assert all(r["status"] == STATUS_SCANNED for r in load_records(root).values())
    skipped = runner.invoke(
        app,
        ["registry", "add", "--run-id", "1", "--db", str(db), "--registry",
         str(tmp_path / "other"), "--no-live-check"],
    )  # fmt: skip
    assert skipped.exit_code == 0, skipped.output
    statuses = {r["status"] for r in load_records(tmp_path / "other").values()}
    assert STATUS_CANDIDATE in statuses


def test_an_evidence_draft_records_the_live_check_of_its_account_only(db: Path) -> None:
    from bot_farm_silence_tracker.registry.evidence import draft_evidence
    from bot_farm_silence_tracker.registry.live import LiveStatus

    asked: list[str] = []

    def active(platform: str, user_id: str, handle: str) -> LiveStatus:
        asked.append(user_id)
        return LiveStatus("active", "a test check", CLOCK)

    with Storage(db) as storage:
        built = run_records(storage, 1, EVENTS)
        account = next(r for r in built.results.values() if r.cliff)
        text, summary = draft_evidence(storage, 1, account.user_id, EVENTS, active)
    assert asked == [account.user_id]
    assert summary["live_profile"] == {
        "status": "active",
        "method": "a test check",
        "checked_at": isoformat(CLOCK),
        "detail": "",
    }
    assert f"Automatic check on {isoformat(CLOCK)}: active" in text
