"""Run samples and the persistent spend ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF, at, make_account


def test_run_sample_roundtrip_and_status(tmp_path: Path) -> None:
    a = make_account("a", created_at=at(-100))
    a.meta = {"community": "c1"}
    b = make_account("b")
    with Storage(tmp_path / "c.db") as store:
        run_id = store.create_run("fake", {}, CUTOFF)
        assert store.get_run_sample(run_id) == []
        store.save_run_sample(run_id, [b, a])
        store.set_sample_status(run_id, a, "fetched")
        store.set_sample_status(run_id, b, "error")
    with Storage(tmp_path / "c.db") as store:
        rows = store.get_run_sample(run_id)
    assert [(acct.user_id, status) for acct, status in rows] == [("b", "error"), ("a", "fetched")]
    assert rows[1][0] == a  # The snapshot keeps handle, created_at and meta.


def test_spend_per_platform_day_and_run(tmp_path: Path) -> None:
    day1 = datetime(2026, 10, 11, 23, 59, tzinfo=UTC)
    day2 = day1 + timedelta(minutes=2)
    with Storage(tmp_path / "c.db") as store:
        store.add_spend("x", 1.25, run_id=1, when=day1)
        store.add_spend("x", 0.75, run_id=1, when=day1)
        store.add_spend("x", 2.0, run_id=2, when=day1)
        store.add_spend("x", 4.0, run_id=2, when=day2)
        store.add_spend("reddit", 9.0, when=day1)
    with Storage(tmp_path / "c.db") as store:  # Persists across connections, so reruns see it.
        assert store.spend_today("x", now=day1) == pytest.approx(4.0)
        assert store.spend_today("x", now=day2) == pytest.approx(4.0)
        assert store.spend_on("x", "2026-10-11") == pytest.approx(4.0)
        assert store.spend_for_run(1) == pytest.approx(2.0)
        assert store.spend_for_run(2, "x") == pytest.approx(6.0)
        assert store.spend_for_run(0, "reddit") == pytest.approx(9.0)  # No run id -> run 0.
        assert store.spend_today("mastodon", now=day1) == 0.0


def test_spend_ledger_scopes(tmp_path: Path) -> None:
    clock = [datetime(2026, 10, 11, 12, tzinfo=UTC)]
    with Storage(tmp_path / "c.db") as store:
        earlier = SpendLedger(store, "x", clock=lambda: clock[0])
        earlier.bind_run(7)
        earlier.add(3.0)
        ledger = SpendLedger(store, "x", clock=lambda: clock[0])
        assert ledger.spent_this_run() == 0.0  # Not bound yet.
        ledger.bind_run(8)
        ledger.add(1.5)
        ledger.add(0.0)
        ledger.add(-1.0)  # Ignored.
        assert ledger.spent("run") == pytest.approx(1.5)
        assert ledger.spent("day") == ledger.spent() == pytest.approx(4.5)
        clock[0] += timedelta(days=1)
        assert ledger.spent("day") == 0.0  # A new UTC day starts fresh.
        assert ledger.spent("run") == pytest.approx(1.5)
        with pytest.raises(ValueError):
            ledger.spent("week")
        assert "'x'" in repr(ledger)
