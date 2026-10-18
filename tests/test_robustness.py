"""Run locks, the shared spend ledger, a safe move of the old database, the
``cancelled`` status, X paging guards and the socket guard of the test suite."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker import storage as storage_module
from bot_farm_silence_tracker.cli import ScanHooks, app
from bot_farm_silence_tracker.gui import presenter as P
from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.pipeline import run_scan
from bot_farm_silence_tracker.sources.errors import BudgetExceeded
from bot_farm_silence_tracker.storage import (
    MIGRATION_STATE,
    RunInUse,
    Storage,
    access_lock_path,
    acquire_file_lock,
    event_db_path,
    legacy_db_path,
    migrate_legacy_db,
    shared_spend_path,
)
from bot_farm_silence_tracker.timeutil import isoformat, utcnow

from .conftest import RealNetworkBlocked
from .helpers import EVENT, NOW, SECOND
from .test_pipeline import SEEDS, make_source, settings
from .test_x_source import OLD, SINCE, UNTIL, j, tweet
from .test_x_source import make_source as make_x_source

runner = CliRunner()


def _interrupted_run(db: Path) -> int:
    """A stored run left ``running``, as a scan that was killed leaves it."""
    with Storage(db) as store:
        outcome = run_scan(make_source(), SEEDS, store, settings())
        store._conn.execute("UPDATE runs SET status = 'running', finished_at = NULL")
        store._conn.commit()
        return outcome.run_id


# -- the run lock --------------------------------------------------------------------------


def test_a_run_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with Storage(db) as first, Storage(db) as second:
        run_id = first.create_run("fake", {}, utcnow())
        held = first.lock_run(run_id)
        assert held is not None
        assert second.run_in_use(run_id)
        with pytest.raises(RunInUse):
            second.lock_run(run_id)
        held.release()
        assert not second.run_in_use(run_id)
        again = second.lock_run(run_id)
        assert again is not None
        again.release()


def test_resume_refuses_a_run_another_scan_holds(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    run_id = _interrupted_run(db)
    with Storage(db) as other, Storage(db) as store:
        other.beat_run(run_id)
        held = other.lock_run(run_id)  # Another scan is still on this run.
        assert held is not None
        with pytest.raises(RunInUse) as caught:
            run_scan(make_source(), SEEDS, store, settings(), from_run=run_id)
        assert caught.value.heartbeat is not None
        assert caught.value.heartbeat["pid"] > 0
        assert store.run_ids() == [run_id]  # Refused before a new run was created.
        held.release()
        # A run still marked `running` after a crash is free: it can be continued.
        outcome = run_scan(make_source(), SEEDS, store, settings(), from_run=run_id)
        assert outcome.status == "complete"


def test_cli_resume_refuses_while_the_run_is_locked(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    run_id = _interrupted_run(db)
    args = ["scan", "--platform", "fake", "--from-run", str(run_id), "--db", str(db)]
    with Storage(db) as other:
        held = other.lock_run(run_id)
        result = runner.invoke(app, [*args, "--out", str(tmp_path / "a.csv")])
        assert held is not None
        held.release()
    assert result.exit_code != 0
    assert "still being scanned by another process" in result.output
    resumed = runner.invoke(app, [*args, "--out", str(tmp_path / "b.csv")])
    assert resumed.exit_code == 0, resumed.output


def test_a_scan_records_its_heartbeat_and_reports_its_run_id(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    reported: list[int] = []
    with Storage(db) as store:
        outcome = run_scan(make_source(), SEEDS, store, settings(), on_run=reported.append)
        run = store.get_run(outcome.run_id)
    assert reported == [outcome.run_id]
    assert run is not None and run["heartbeat"] is not None
    assert set(run["heartbeat"]) == {"pid", "host", "last_seen"}
    # The lock is released when the scan returns.
    with Storage(db) as store:
        assert not store.run_in_use(outcome.run_id)


def test_a_database_without_the_heartbeat_column_is_migrated_in_place(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with Storage(db) as store:
        run_id = store.create_run("fake", {"n": 1}, utcnow())
    conn = sqlite3.connect(db)  # As the previous version left it: no heartbeat column.
    conn.execute("ALTER TABLE runs DROP COLUMN heartbeat_json")
    conn.commit()
    try:
        # The older version still has it open, in the middle of its live run.
        conn.execute("SELECT * FROM runs").fetchall()
        with Storage(db) as store:
            run = store.get_run(run_id)
            assert run is not None and run["status"] == "running"
            assert run["heartbeat"] is None and not store.run_in_use(run_id)
        conn.execute("UPDATE runs SET summary_json = '{\"x\": 1}'")  # It keeps writing.
        conn.commit()
    finally:
        conn.close()
    with Storage(db) as store:
        run = store.get_run(run_id)
    assert run is not None and run["status"] == "running" and run["summary"] == {"x": 1}


def test_the_app_marks_a_run_that_is_still_running_in_another_window(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    run_id = _interrupted_run(db)
    with Storage(db) as other:
        held = other.lock_run(run_id)
        (row,) = P.resumable_runs(db)
        assert row["in_use"] and P.run_in_use(row)
        assert "still running in another window" in P.run_label(row)
        assert held is not None
        held.release()
    (row,) = P.resumable_runs(db)
    assert not row["in_use"] and not P.run_in_use(row)
    assert "another window" in P.IN_USE_TEXT


def test_the_app_takes_the_run_id_from_the_scan_itself(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with Storage(db) as store:
        store.create_run("fake", {}, utcnow())

    def scan(argv: Any, sink: Any, hooks: ScanHooks) -> int:
        with Storage(db) as store:
            own = store.create_run("fake", {}, utcnow())
            assert hooks.on_run is not None
            hooks.on_run(own)
            store.create_run("fake", {}, utcnow())  # Another window starts a run after it.
            store.finish_run(own, status="complete", account_ids=[], summary={}, finished_at=NOW)
        return 0

    job = P.ScanJob(["scan"], db=db, runner=scan)
    job.start()
    job.join(10)
    (finished,) = [e for e in job.drain() if isinstance(e, P.Finished)]
    assert (finished.run_id, finished.run_status) == (2, "complete")


def test_cancelled_runs_are_resumable_and_old_failed_ones_still_are(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with Storage(db) as store:
        for status in ("cancelled", "failed"):
            run_id = store.create_run("fake", {}, utcnow())
            store.finish_run(run_id, status=status, account_ids=[], summary={}, finished_at=NOW)
    assert [r["status"] for r in P.resumable_runs(db)] == ["failed", "cancelled"]


# -- the shared spend ledger ---------------------------------------------------------------


def test_two_events_on_the_same_day_share_one_cap() -> None:
    with Storage(event_db_path(EVENT.id)) as first, Storage(event_db_path(SECOND.id)) as second:
        assert first.spend_shared and second.spend_shared
        one = SpendLedger(first, "x")
        one.bind_run(first.create_run("x", {}, utcnow()))
        two = SpendLedger(second, "x")
        two.bind_run(second.create_run("x", {}, utcnow()))
        one.reserve(6.0, 10.0, "day").settle(6.0)
        with pytest.raises(BudgetExceeded, match="per day reached"):
            two.reserve(6.0, 10.0, "day")
        two.reserve(4.0, 10.0, "day").settle(4.0)
        assert one.spent("day") == pytest.approx(10.0) == two.spent("day")
        # Run totals stay per database, although both runs have id 1.
        assert one.spent_this_run() == pytest.approx(6.0)
        assert two.spent_this_run() == pytest.approx(4.0)
    assert shared_spend_path().is_file()


def test_a_database_outside_the_cache_folder_keeps_its_own_spend(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as own, Storage(event_db_path(EVENT.id)) as shared:
        assert not own.spend_shared
        SpendLedger(own, "x").add(5.0)
        assert SpendLedger(shared, "x").spent("day") == 0.0
        assert SpendLedger(own, "x").spent("day") == pytest.approx(5.0)


def _own_spend_db(tmp_path: Path) -> Path:
    """An event database whose spend is in its own table, as an older version kept it."""
    outside = tmp_path / "old.db"
    with Storage(outside) as store:
        store.add_spend("x", 3.0, run_id=1)
        event, _ = store.reserve_spend(
            "x", 2_000_000, 50_000_000, scope="day", run_id=1, root_run_id=1
        )
    target = event_db_path(EVENT.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(outside, target)
    assert event == 2
    return target


def test_the_own_spend_of_an_event_is_copied_once_and_kept_in_step(tmp_path: Path) -> None:
    db = _own_spend_db(tmp_path)
    for _ in range(2):  # Copying again never counts it twice.
        with Storage(db) as store:
            assert store.spend_shared
            assert SpendLedger(store, "x").spent("day") == pytest.approx(5.0)
    # An older version still running settles its reservation in the own table.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE spend_events SET micro_usd = 500000, state = 'settled', settle_seq = 1,"
        " updated_at = ? WHERE event_id = 2",
        (isoformat(utcnow()),),
    )
    conn.commit()
    conn.close()
    with Storage(db) as store, Storage(event_db_path(SECOND.id)) as other:
        assert SpendLedger(store, "x").spent("day") == pytest.approx(3.5)
        assert SpendLedger(other, "x").spent("day") == pytest.approx(3.5)  # Shared cap.
        rows = store._conn.execute(
            "SELECT COUNT(*) FROM spend.spend_events WHERE db_id = ?", (store.db_id,)
        ).fetchone()[0]
    assert rows == 2


def test_an_older_fallback_settle_is_matched_to_the_copied_row(tmp_path: Path) -> None:
    db = _own_spend_db(tmp_path)
    with Storage(db) as store:
        fallback = store.spend_fallback_path
        record = {
            "what": "settle",
            "platform": "x",
            "run_id": 1,
            "db_id": store.db_id,
            "when": isoformat(utcnow()),
            "day": utcnow().strftime("%Y-%m-%d"),
            "nonce": "n1",
            "event_id": 2,  # Its id in the event's own table, not in the shared one.
            "settled_micro_usd": 1_000_000,
            "reserved_micro_usd": 2_000_000,
            "seq": 1,
            "settle_day": utcnow().strftime("%Y-%m-%d"),
            "settled_at": isoformat(utcnow()),
        }
        fallback.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        assert SpendLedger(store, "x").spent("day") == pytest.approx(4.0)
        assert store.reconcile_fallback() == 1
        assert SpendLedger(store, "x").spent("day") == pytest.approx(4.0)
        assert store.fallback_record_tags() == {"ledger": "shared"}


# -- the move of the old database ----------------------------------------------------------


def _legacy_with_fallback() -> Path:
    legacy = legacy_db_path()
    with Storage(legacy) as store:
        run_scan(make_source(), SEEDS, store, settings())
        fallback = store.spend_fallback_path
    fallback.write_text('{"note": "first"}\n{"note": "second"}\n', encoding="utf-8")
    return legacy


def _fallback_of(db: Path) -> Path:
    return db.with_name(db.name + ".spend-fallback.jsonl")


def test_the_move_is_skipped_while_the_old_database_is_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    legacy = _legacy_with_fallback()
    target = event_db_path(EVENT.id)
    with Storage(legacy), caplog.at_level("WARNING", logger="bot_farm_silence_tracker.storage"):
        assert migrate_legacy_db(legacy, target) == legacy
    assert any("open in another scan" in r.getMessage() for r in caplog.records)
    assert legacy.is_file() and not target.exists()
    assert migrate_legacy_db(legacy, target) == target
    assert not legacy.exists() and target.is_file()


def test_the_move_is_skipped_while_an_older_version_has_the_database_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Version 0.2.0 takes no lock, so only SQLite's own locks show that it is open."""
    legacy = _legacy_with_fallback()
    target = event_db_path(EVENT.id)
    code = (
        "import sqlite3, sys, time\n"
        "c = sqlite3.connect(sys.argv[1])\n"
        "c.execute('SELECT COUNT(*) FROM runs').fetchone()\n"  # Idle after this read.
        "print('open', flush=True)\n"
        "sys.stdin.read()\n"
    )
    old = subprocess.Popen(
        [sys.executable, "-c", code, str(legacy)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert old.stdout is not None and old.stdout.readline().strip() == "open"
        with caplog.at_level("WARNING", logger="bot_farm_silence_tracker.storage"):
            assert migrate_legacy_db(legacy, target) == legacy
        if sys.platform != "win32":  # Windows refuses the rename itself, with its own note.
            assert any("open in another process" in r.getMessage() for r in caplog.records)
        assert legacy.is_file() and not target.exists()
    finally:
        if old.stdin is not None:
            old.stdin.close()
        old.wait(timeout=30)
    assert migrate_legacy_db(legacy, target) == target
    assert not legacy.exists() and target.is_file()


def test_an_open_waits_for_a_move_and_follows_it() -> None:
    legacy = _legacy_with_fallback()
    target = event_db_path(EVENT.id)
    held = acquire_file_lock(access_lock_path(legacy), shared=False)
    assert held is not None
    opened: list[Path] = []

    def open_old_path() -> None:
        with Storage(legacy) as store:
            opened.append(store.path)

    thread = threading.Thread(target=open_old_path)
    thread.start()
    thread.join(0.5)
    assert thread.is_alive()  # It waits for the move instead of opening the file.
    assert storage_module._migrate_locked(legacy, target) == target
    held.release()
    thread.join(10)
    assert opened == [target]
    assert not legacy.exists()  # No empty database was created at the old path.


def test_an_open_by_the_old_path_after_the_move_uses_the_new_one() -> None:
    legacy = _legacy_with_fallback()
    target = event_db_path(EVENT.id)
    assert migrate_legacy_db(legacy, target) == target
    with Storage(legacy) as store:
        assert store.path == target and store.get_run(1) is not None
    assert not legacy.exists()


class Crash(BaseException):
    """The process dies here."""


def test_a_crash_before_the_main_file_moves_loses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_with_fallback()
    target = event_db_path(EVENT.id)
    real = storage_module._move_file

    def crash_on_main(src: Path, dst: Path) -> bool:
        if src == legacy:
            raise Crash
        return real(src, dst)

    monkeypatch.setattr(storage_module, "_move_file", crash_on_main)
    with pytest.raises(Crash):
        migrate_legacy_db(legacy, target)
    monkeypatch.setattr(storage_module, "_move_file", real)
    state = json.loads((legacy.parent / MIGRATION_STATE).read_text(encoding="utf-8"))
    assert state["state"] == "pending"
    # The old database is complete where it was, its fallback records included.
    assert legacy.is_file() and not target.exists()
    assert len(_fallback_of(legacy).read_text().splitlines()) == 2
    # An open of the old path finishes the move first.
    with Storage(legacy) as store:
        assert store.path == target and store.get_run(1) is not None
    assert not legacy.exists() and not _fallback_of(legacy).exists()
    assert _fallback_of(target).read_text().splitlines() == [
        '{"note": "first"}',
        '{"note": "second"}',
    ]
    done = json.loads((legacy.parent / MIGRATION_STATE).read_text(encoding="utf-8"))
    assert done["state"] == "done"


def test_a_crash_after_the_main_file_moved_is_finished_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_with_fallback()
    target = event_db_path(EVENT.id)
    real = storage_module._merge_copy

    def crash_after_main(src: Path, dst: Path) -> bool:
        if target.exists():
            raise Crash
        return real(src, dst)

    monkeypatch.setattr(storage_module, "_merge_copy", crash_after_main)
    with pytest.raises(Crash):
        migrate_legacy_db(legacy, target)
    monkeypatch.setattr(storage_module, "_merge_copy", real)
    assert target.is_file() and not legacy.exists()
    assert _fallback_of(legacy).is_file()  # Not yet tidied up, and nothing was lost.
    assert migrate_legacy_db(legacy, target) == target
    assert not _fallback_of(legacy).exists()
    assert len(_fallback_of(target).read_text().splitlines()) == 2


# -- X paging ------------------------------------------------------------------------------


def _timeline(next_token: Any) -> Any:
    counter = iter(range(10_000))

    def handler(request: httpx.Request) -> httpx.Response:
        k = next(counter)
        posts = [tweet(f"{k}-{i}", f"2026-10-0{1 + k % 9}T10:00:00Z") for i in range(10)]
        return j({"data": posts, "meta": {"result_count": 10, "next_token": next_token(k)}})

    return handler


def test_x_paging_stops_when_a_page_token_repeats() -> None:
    source, _, seen = make_x_source(_timeline(lambda k: "same"))
    account = Account("x", "42", "h", OLD)
    acts = source.fetch_activity(account, SINCE, UNTIL)
    assert len(seen) == 2 and len(acts) == 20
    assert account.meta["truncated"] is True
    assert account.meta["partial_fetch"]["reason"] == "error"
    assert any("repeated a page token" in w for w in source.drain_warnings())


def test_x_paging_stops_at_its_page_cap() -> None:
    from bot_farm_silence_tracker.sources.x import MAX_TIMELINE_PAGES

    source, _, seen = make_x_source(_timeline(lambda k: f"t{k}"))
    account = Account("x", "42", "h", OLD)
    source.fetch_activity(account, SINCE, UNTIL)
    assert len(seen) == MAX_TIMELINE_PAGES
    assert account.meta["truncated"] is True
    assert any(f"past {MAX_TIMELINE_PAGES} pages" in w for w in source.drain_warnings())


# -- the socket guard of the test suite ------------------------------------------------------


def test_unit_tests_cannot_reach_a_real_host(_no_real_network: list[Any]) -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        with socket.create_connection(server.getsockname(), timeout=5):
            pass  # Loopback, as the fake servers use, is allowed.
    finally:
        server.close()
    with pytest.raises(RealNetworkBlocked), socket.socket() as sock:
        sock.settimeout(0.1)
        sock.connect(("192.0.2.1", 443))  # TEST-NET-1: never a real host.
    assert _no_real_network == [("192.0.2.1", 443)]
    _no_real_network.clear()  # Expected here; any other test fails at teardown.
