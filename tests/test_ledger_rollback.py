"""Failed commits are rolled back, settles are idempotent, reserve fails closed, and
run spend includes the fallback file."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.sources.errors import BudgetExceeded
from bot_farm_silence_tracker.storage import Storage

DAY = datetime(2026, 10, 11, 12, tzinfo=UTC)


class FlakyConn:
    """Wraps a real connection; ``commit`` fails ``fail_commits`` times, and
    ``BEGIN IMMEDIATE`` fails ``fail_begins`` times."""

    def __init__(self, real: sqlite3.Connection, fail_commits: int = 0, fail_begins: int = 0):
        self._real = real
        self.fail_commits = fail_commits
        self.fail_begins = fail_begins

    def commit(self) -> None:
        if self.fail_commits > 0:
            self.fail_commits -= 1
            raise sqlite3.OperationalError("database is locked (simulated at commit)")
        self._real.commit()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.startswith("BEGIN IMMEDIATE") and self.fail_begins > 0:
            self.fail_begins -= 1
            raise sqlite3.OperationalError("database is locked (simulated at begin)")
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _setup(tmp_path: Path) -> tuple[Storage, sqlite3.Connection, SpendLedger]:
    store = Storage(tmp_path / "c.db")
    ledger = SpendLedger(store, "x", clock=lambda: DAY, sleep=lambda s: None)
    ledger.bind_run(store.create_run("x", {}, DAY))
    return store, store._conn, ledger


def _flaky(store: Storage, real: sqlite3.Connection, **kw: int) -> FlakyConn:
    conn = FlakyConn(real, **kw)
    store._conn = conn  # type: ignore[assignment]
    return conn


def test_one_failed_commit_is_retried_without_double_counting(tmp_path: Path) -> None:
    store, real, ledger = _setup(tmp_path)
    _flaky(store, real, fail_commits=1)
    ledger.add(1.0)
    store._conn = real
    assert ledger.spent_today() == pytest.approx(1.0)
    assert not ledger.fallback_path.exists()
    real.commit()  # An unrelated later commit must not add a stale INSERT.
    assert ledger.spent_today() == pytest.approx(1.0)
    store.close()


def test_fallback_after_failed_commits_counts_once(tmp_path: Path) -> None:
    store, real, ledger = _setup(tmp_path)
    ledger.add(1.0)
    _flaky(store, real, fail_commits=3)
    ledger.add(1.0)
    store._conn = real
    assert ledger.fallback_path.exists()
    assert ledger.spent_today() == pytest.approx(2.0)
    real.commit()
    assert ledger.spent_today() == pytest.approx(2.0)
    store.close()


def test_failed_settles_do_not_go_negative(tmp_path: Path) -> None:
    store, real, ledger = _setup(tmp_path)
    reservation = ledger.reserve(5.0, 100.0)
    _flaky(store, real, fail_commits=3)
    reservation.settle(1.0)
    store._conn = real
    assert ledger.spent_today() == pytest.approx(1.0)
    real.commit()
    assert ledger.spent_today() == pytest.approx(1.0)
    record = json.loads(ledger.fallback_path.read_text().splitlines()[-1])
    assert record["settled_micro_usd"] == 1_000_000 and record["event_id"] == reservation.event_id
    store.close()


def test_settle_is_idempotent_across_the_database_and_the_fallback(tmp_path: Path) -> None:
    store, real, ledger = _setup(tmp_path)
    reservation = ledger.reserve(5.0, 100.0)
    reservation.settle(2.0)
    reservation.settle(2.0)  # Twice in the database: still 2.
    assert ledger.spent_today() == pytest.approx(2.0)

    _flaky(store, real, fail_commits=3)
    reservation.settle(1.0)  # To the fallback file.
    store._conn = real
    assert ledger.spent_today() == pytest.approx(1.0)

    reservation.settle(1.5)  # Database again, newer than the fallback record.
    assert ledger.spent_today() == pytest.approx(1.5)

    _flaky(store, real, fail_commits=3)
    reservation.settle(3.0)  # Fallback again, newest.
    store._conn = real
    assert ledger.spent_today() == pytest.approx(3.0)
    assert ledger.spent_this_run() == pytest.approx(3.0)
    assert ledger.spent("run") == pytest.approx(3.0)
    store.close()


def test_legacy_settle_correction_records_only_raise_the_total(tmp_path: Path) -> None:
    # Records without a db_id cannot be matched to this database, so a negative
    # correction is ignored and a positive one counts.
    store, _, ledger = _setup(tmp_path)
    ledger.reserve(0.5, 100.0)
    legacy = {"what": "settle", "platform": "x", "run_id": ledger.run_id, "day": "2026-10-11",
              "micro_usd": -200_000, "event_id": 1}  # fmt: skip
    ledger.fallback_path.write_text(json.dumps(legacy) + "\n")
    assert ledger.spent_today() == pytest.approx(0.5)
    legacy["micro_usd"] = 200_000
    ledger.fallback_path.write_text(json.dumps(legacy) + "\n")
    assert ledger.spent_today() == pytest.approx(0.7)
    store.close()


def test_reserve_fails_closed_on_a_database_error(tmp_path: Path) -> None:
    store, real, ledger = _setup(tmp_path)
    _flaky(store, real, fail_begins=1)
    with pytest.raises(BudgetExceeded, match="could not be reserved"):
        ledger.reserve(1.0, 100.0)
    store._conn = real
    assert ledger.spent_today() == 0.0
    ledger.reserve(1.0, 100.0).settle(1.0)  # The connection is usable again.
    assert ledger.spent_today() == pytest.approx(1.0)
    store.close()


def test_run_spend_includes_this_runs_fallback_amounts_only(tmp_path: Path) -> None:
    store, real, ledger = _setup(tmp_path)
    ledger.add(0.25)
    _flaky(store, real, fail_commits=3)
    ledger.add(0.5)  # Fallback, this run.
    store._conn = real
    other = SpendLedger(store, "x", clock=lambda: DAY, sleep=lambda s: None)
    other.bind_run(store.create_run("x", {}, DAY))
    _flaky(store, real, fail_commits=3)
    other.add(2.0)  # Fallback, another run.
    store._conn = real
    assert ledger.spent_this_run() == pytest.approx(0.75)
    assert other.spent_this_run() == pytest.approx(2.0)
    assert ledger.spent_today() == pytest.approx(2.75)
    store.close()
