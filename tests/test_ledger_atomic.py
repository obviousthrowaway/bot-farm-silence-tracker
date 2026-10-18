"""Ledger atomicity, micro-USD storage, run chains, migration and the fallback file."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.sources.errors import BudgetExceeded
from bot_farm_silence_tracker.storage import Storage

DAY = datetime(2026, 10, 11, 12, tzinfo=UTC)


def _clock() -> datetime:
    return DAY


def _reserve_until_capped(db: str, platform: str, cap: float, step: float, out: str) -> None:
    """Child process: reserve ``step`` against ``cap`` until refused; report the count."""
    granted = 0
    with Storage(db) as store:
        ledger = SpendLedger(store, platform, clock=_clock)
        ledger.bind_run(store.create_run(platform, {}, DAY))
        while True:
            try:
                reservation = ledger.reserve(step, cap, "day")
            except BudgetExceeded:
                break
            reservation.settle(step)
            granted += 1
    Path(out).write_text(str(granted))


def test_connection_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        conn = store._conn
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_reserve_settle_and_cap(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        ledger = SpendLedger(store, "x", clock=_clock)
        ledger.bind_run(store.create_run("x", {}, DAY))
        first = ledger.reserve(0.60, cap=1.00)
        assert ledger.spent() == pytest.approx(0.60)  # A reservation counts at once.
        with pytest.raises(BudgetExceeded, match=r"\$1.00 per day"):
            ledger.reserve(0.50, cap=1.00)
        first.settle(0.25)  # Actual cost was lower.
        assert first.settled
        assert ledger.spent() == pytest.approx(0.25)
        second = ledger.reserve(0.75, cap=1.00)  # Exactly at the cap is allowed.
        second.settle(0.0)  # Nothing billed: released.
        assert ledger.spent() == pytest.approx(0.25)
        ledger.add(0.10)  # The non-atomic API still works alongside.
        assert ledger.spent("day") == pytest.approx(0.35)
        with pytest.raises(ValueError):
            ledger.reserve(0.1, 1.0, "week")


def test_micro_usd_storage_has_no_float_drift(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        ledger = SpendLedger(store, "x", clock=_clock)
        ledger.bind_run(store.create_run("x", {}, DAY))
        for _ in range(10_000):
            ledger.add(0.0001)
        assert ledger.spent() == 1.0  # Exact, not approximately.
        kinds = {r[0] for r in store._conn.execute("SELECT typeof(micro_usd) FROM spend_events")}
        assert kinds == {"integer"}


def test_two_processes_never_overshoot_the_cap(tmp_path: Path) -> None:
    db = str(tmp_path / "c.db")
    Storage(db).close()  # Create the schema before the children race.
    ctx = multiprocessing.get_context("spawn")
    outs = [str(tmp_path / f"out{i}") for i in range(2)]
    procs = [
        ctx.Process(target=_reserve_until_capped, args=(db, "x", 1.0, 0.01, out)) for out in outs
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    granted = [int(Path(out).read_text()) for out in outs]
    assert sum(granted) == 100  # 100 x $0.01 fits a $1.00 cap exactly; no more.
    with Storage(db) as store:
        assert store.spend_on("x", "2026-10-11") == pytest.approx(1.0)
        assert store.spend_on("x", "2026-10-11") <= 1.0


def test_run_scope_sums_the_resume_chain(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        root = store.create_run("x", {}, DAY)
        child = store.create_run("x", {"from_run": root}, DAY, root_run_id=root)
        grandchild = store.create_run("x", {"from_run": child}, DAY, root_run_id=root)
        other = store.create_run("x", {}, DAY)
        assert store.get_run(root)["root_run_id"] == root  # type: ignore[index]
        assert store.get_run(grandchild)["root_run_id"] == root  # type: ignore[index]
        assert store.run_chain(root) == [root, child, grandchild]
        assert store.root_run_id(other) == other
        for run, usd in ((root, 1.0), (child, 2.0), (other, 8.0)):
            ledger = SpendLedger(store, "x", clock=_clock)
            ledger.bind_run(run)
            ledger.add(usd)
        ledger = SpendLedger(store, "x", clock=_clock)
        ledger.bind_run(grandchild)  # Root looked up from the runs table.
        assert ledger.root_run_id == root
        assert ledger.spent_this_run() == 0.0
        assert ledger.spent("run") == pytest.approx(3.0)
        assert ledger.spent_this_chain() == pytest.approx(3.0)
        with pytest.raises(BudgetExceeded):
            ledger.reserve(2.5, cap=5.0, scope="run")
        ledger.reserve(2.0, cap=5.0, scope="run").settle(2.0)
        assert store.spend_for_chain(root) == pytest.approx(5.0)


def test_legacy_spend_ledger_table_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, platform TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
            params_json TEXT NOT NULL, account_ids_json TEXT NOT NULL DEFAULT '[]',
            summary_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE run_sample (run_id INTEGER NOT NULL, position INTEGER NOT NULL,
            platform TEXT NOT NULL, user_id TEXT NOT NULL, account_json TEXT NOT NULL,
            fetch_status TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY (run_id, platform, user_id));
        CREATE TABLE spend_ledger (platform TEXT NOT NULL, day TEXT NOT NULL,
            run_id INTEGER NOT NULL DEFAULT 0, usd REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL, PRIMARY KEY (platform, day, run_id));
        INSERT INTO runs (started_at, platform, params_json) VALUES ('2026-10-11', 'x', '{}');
        INSERT INTO spend_ledger VALUES ('x', '2026-10-11', 1, 1.234567, '2026-10-11');
        """
    )
    conn.commit()
    conn.close()
    with Storage(path) as store:
        assert store.spend_on("x", "2026-10-11") == pytest.approx(1.234567)
        assert store.get_run(1)["root_run_id"] == 1  # type: ignore[index]
        assert store.run_chain(1) == [1]
    with Storage(path) as store:  # Idempotent.
        assert store.spend_on("x", "2026-10-11") == pytest.approx(1.234567)


class FlakyStorage(Storage):
    """Storage whose spend writes fail a set number of times."""

    failures = 0

    def add_spend_micro(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        if self.failures > 0:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        super().add_spend_micro(*args, **kwargs)  # type: ignore[arg-type]

    def settle_spend(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        if self.failures > 0:
            self.failures -= 1
            raise sqlite3.OperationalError("disk I/O error")
        super().settle_spend(*args, **kwargs)  # type: ignore[arg-type]


def test_add_retries_then_succeeds(tmp_path: Path) -> None:
    sleeps: list[float] = []
    with FlakyStorage(tmp_path / "c.db") as store:
        store.failures = 2
        ledger = SpendLedger(store, "x", clock=_clock, sleep=sleeps.append)
        ledger.bind_run(store.create_run("x", {}, DAY))
        ledger.add(0.5)
        assert store.spend_today("x", now=DAY) == pytest.approx(0.5)
        assert sleeps == [0.2, 0.4]
        assert not ledger.fallback_path.exists()


def test_billed_spend_falls_back_to_a_file_and_still_counts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with FlakyStorage(tmp_path / "c.db") as store:
        ledger = SpendLedger(store, "x", clock=_clock, sleep=lambda s: None)
        run = store.create_run("x", {}, DAY)
        ledger.bind_run(run)
        store.failures = 99
        ledger.add(0.7)
        assert store.spend_today("x", now=DAY) == 0.0  # Not in the database ...
        assert ledger.fallback_path == tmp_path / "c.db.spend-fallback.jsonl"
        record = json.loads(ledger.fallback_path.read_text().splitlines()[0])
        assert record["micro_usd"] == 700_000 and record["run_id"] == run
        assert ledger.spent("day") == pytest.approx(0.7)  # ... but still counted.
        assert ledger.spent("run") == pytest.approx(0.7)
        assert any("spend-fallback" in r.getMessage() for r in caplog.records)
        store.failures = 0
        with pytest.raises(BudgetExceeded):
            ledger.reserve(0.5, cap=1.0)  # The cap sees the fallback amount.
        reservation = ledger.reserve(0.3, cap=1.0)
        store.failures = 99
        reservation.settle(0.1)  # Settling fails: the correction goes to the file.
        assert ledger.spent("day") == pytest.approx(0.8)
