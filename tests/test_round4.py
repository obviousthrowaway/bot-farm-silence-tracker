"""Round-4 review fixes: per-account data ends, fallback rotation races, read-only
report on old or WAL databases, settle-day mirrors from the fallback file, secret
names, legacy record expiry and per-record nonces."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import bot_farm_silence_tracker.storage as storage_module
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import Account, Activity
from bot_farm_silence_tracker.pipeline import is_secret_option, rescore_run, run_scan
from bot_farm_silence_tracker.scoring import DATA_END_TOLERANCE, ScoringParams, score_account
from bot_farm_silence_tracker.sources.errors import BudgetExceeded
from bot_farm_silence_tracker.storage import Storage, fallback_lock
from bot_farm_silence_tracker.timeutil import isoformat

from .helpers import CUTOFF, make_account, steady
from .test_fake_and_cli import scan_args
from .test_pipeline import SEEDS, ScriptedSource, make_source, settings

runner = CliRunner()
DAY = datetime(2026, 10, 11, 12, tzinfo=UTC)


class Clock:
    def __init__(self, when: datetime) -> None:
        self.when = when

    def __call__(self) -> datetime:
        return self.when


def _ledger(store: Storage, clock: Any = None) -> SpendLedger:
    ledger = SpendLedger(store, "x", clock=clock or (lambda: DAY), sleep=lambda s: None)
    ledger.bind_run(store.create_run("x", {}, DAY))
    return ledger


def _locked(*a: Any, **k: Any) -> None:
    raise sqlite3.OperationalError("database is locked")


# -- HIGH-1: score every account against its own data end -------------------------------


class Paused(ScriptedSource):
    """A person: posts steadily, pauses an hour before the cutoff, posts again 1.5 days
    after it. Data fetched up to cutoff + 1 day shows a pause that looks like a cliff."""

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        call = len(self.fetches)
        self.fetches.append(account.user_id)
        if call in self.fail:
            raise self.fail[call]
        acts = steady(account.user_id, CUTOFF - timedelta(days=30), CUTOFF - timedelta(hours=1))
        acts += steady(account.user_id, CUTOFF + timedelta(days=1.5), CUTOFF + timedelta(days=20))
        return [a for a in acts if since <= a.ts < until]


def test_resume_scores_reused_windows_to_their_own_end_not_the_new_now(tmp_path: Path) -> None:
    now1, now2 = CUTOFF + timedelta(days=1), CUTOFF + timedelta(days=5)
    with Storage(tmp_path / "c.db") as store:
        s1 = settings(scoring=ScoringParams(cutoff=CUTOFF, now=now1))
        first = run_scan(
            make_source(cls=Paused, fail={10: BudgetExceeded("cap")}), SEEDS, store, s1
        )
        assert first.status == "partial"
        assert all(r.provisional for r in first.results if r.cliff)

        src = make_source(cls=Paused)
        s2 = settings(scoring=ScoringParams(cutoff=CUTOFF, now=now2))
        second = run_scan(src, SEEDS, store, s2, from_run=first.run_id)
        reused = [r for r in second.results if r.user_id not in set(src.fetches)]
        fetched = [r for r in second.results if r.user_id in set(src.fetches)]
        assert len(reused) == 10 and len(fetched) == 20
        # The false accusation the review found: none of these is a definite cliff.
        assert sum(r.cliff and not r.provisional for r in second.results) == 0
        assert all(r.provisional for r in reused if r.cliff)
        assert not any(r.cliff for r in fetched)  # Their post-cutoff posts were seen.
        for r in reused:
            assert r.data_end == now1
            assert r.last_activity is not None
            assert r.silence_hours == pytest.approx((now1 - r.last_activity).total_seconds() / 3600)
        assert all(r.data_end is None for r in fetched)
        assert second.summary["early_data_end"] == 10
        assert any("scored against data that ends before" in w for w in second.warnings)
        row = reused[0].to_row()
        assert row["data_end"] == isoformat(now1)

        # report --run-id uses the stored per-account ends, even after the cache
        # window of those accounts was moved on by something else.
        for r in reused:
            store._conn.execute(
                "UPDATE accounts SET activity_until = ? WHERE user_id = ?",
                (isoformat(now2), r.user_id),
            )
        store._conn.commit()
        again = rescore_run(store, second.run_id)
        assert sum(r.cliff and not r.provisional for r in again.results) == 0
        assert again.summary["early_data_end"] == 10
        assert {r.user_id: r.data_end for r in again.results if r.data_end} == {
            r.user_id: now1 for r in reused
        }


def test_rescore_of_a_run_without_stored_ends_falls_back_to_the_cached_window(
    tmp_path: Path,
) -> None:
    now1, now2 = CUTOFF + timedelta(days=1), CUTOFF + timedelta(days=5)
    with Storage(tmp_path / "c.db") as store:
        s1 = settings(scoring=ScoringParams(cutoff=CUTOFF, now=now1))
        first = run_scan(
            make_source(cls=Paused, fail={10: BudgetExceeded("cap")}), SEEDS, store, s1
        )
        s2 = settings(scoring=ScoringParams(cutoff=CUTOFF, now=now2))
        second = run_scan(make_source(cls=Paused), SEEDS, store, s2, from_run=first.run_id)
        store._conn.execute("UPDATE run_sample SET window_end = NULL")  # As an older version.
        store._conn.commit()
        again = rescore_run(store, second.run_id)
        assert sum(r.cliff and not r.provisional for r in again.results) == 0
        assert again.summary["early_data_end"] == 10


def test_fresh_scan_cache_hit_within_ttl_is_scored_to_its_window_end(tmp_path: Path) -> None:
    now1, now2 = CUTOFF + timedelta(days=4), CUTOFF + timedelta(days=4, hours=12)
    with Storage(tmp_path / "c.db") as store:
        run_scan(make_source(), SEEDS, store, settings(scoring=ScoringParams(CUTOFF, now1)))
        src = make_source()
        later = run_scan(src, SEEDS, store, settings(scoring=ScoringParams(CUTOFF, now2)))
        assert src.fetches == []  # Every account came from the cache (24-hour TTL).
        assert later.results and all(r.data_end == now1 for r in later.results)
        for r in later.results:
            if r.last_activity is not None and r.silence_hours is not None:
                expected = (now1 - r.last_activity).total_seconds() / 3600
                assert r.silence_hours == pytest.approx(max(0.0, expected))


def test_score_account_measures_silence_to_the_window_end() -> None:
    params = ScoringParams(cutoff=CUTOFF, now=CUTOFF + timedelta(days=10))
    acct = make_account("bot")
    acts = steady("bot", CUTOFF - timedelta(days=30), CUTOFF - timedelta(hours=1))
    full = score_account(acct, acts, params)
    assert full.cliff and not full.provisional and full.data_end is None

    end = CUTOFF + timedelta(days=1)
    early = score_account(acct, acts, params, window_end=end)
    assert early.cliff and early.provisional and early.data_end == end
    assert early.silence_hours == pytest.approx(30.0)  # Last post 6 hours before the cutoff.
    assert early.p_silence is not None and full.p_silence is not None
    assert early.p_silence > full.p_silence
    assert early.score < full.score

    # Enough silence inside the window: a definite cliff, still marked with data_end.
    long_end = CUTOFF + timedelta(days=5)
    ok = score_account(acct, acts, params, window_end=long_end)
    assert ok.cliff and not ok.provisional and ok.data_end == long_end

    # Within the tolerance of effective_now: no data_end.
    near = params.effective_now - DATA_END_TOLERANCE / 2
    assert score_account(acct, acts, params, window_end=near).data_end is None
    # A window end after effective_now changes nothing.
    assert score_account(acct, acts, params, window_end=params.now + timedelta(days=1)) == full


def test_post_cutoff_activity_up_to_now_still_rules_out_a_cliff() -> None:
    params = ScoringParams(cutoff=CUTOFF, now=CUTOFF + timedelta(days=10))
    acct = make_account("human")
    acts = steady("human", CUTOFF - timedelta(days=30), CUTOFF - timedelta(hours=1))
    acts.append(Activity("fake", "human", CUTOFF + timedelta(days=3), "post"))
    result = score_account(acct, acts, params, window_end=CUTOFF + timedelta(days=1))
    assert not result.cliff and result.posts_after_cutoff == 1


def test_force_provisional_still_applies_with_a_window_end() -> None:
    params = ScoringParams(cutoff=CUTOFF, now=CUTOFF + timedelta(days=10), force_provisional=True)
    acts = steady("bot", CUTOFF - timedelta(days=30), CUTOFF - timedelta(hours=1))
    result = score_account(make_account("bot"), acts, params, window_end=CUTOFF + timedelta(days=6))
    assert result.cliff and result.provisional
    assert params.provisional_at(None) and params.provisional_at(CUTOFF)


# -- HIGH-2: records appended while reconcile runs are never lost -------------------------


def test_records_appended_during_reconcile_are_kept_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = Storage(tmp_path / "c.db"), Storage(tmp_path / "c.db")
    ledger_a = SpendLedger(a, "x", sleep=lambda s: None)
    a.add_spend_micro = _locked  # type: ignore[method-assign]
    ledger_a.add(1.0)  # Billed; the database refused it, so it is in the fallback file.
    assert ledger_a.spent_today() == pytest.approx(1.0)

    real_load = storage_module.load_spend_fallback
    appended: list[bool] = []

    def load_then_append(path: Path) -> list[tuple[str, dict[str, Any]]]:
        out = real_load(path)
        if not appended:
            appended.append(True)
            ledger_a.add(2.0)  # A second billed charge lands while B is merging.
        return out

    monkeypatch.setattr(storage_module, "load_spend_fallback", load_then_append)
    assert b.reconcile_fallback() == 1
    monkeypatch.setattr(storage_module, "load_spend_fallback", real_load)
    assert appended
    assert ledger_a.spent_today() == pytest.approx(3.0)
    assert SpendLedger(b, "x").spent_today() == pytest.approx(3.0)
    assert a.spend_today("x") == pytest.approx(1.0)  # Only the first one merged so far.

    assert b.reconcile_fallback() == 1  # The next run start merges the second.
    assert b.spend_today("x") == pytest.approx(3.0)
    assert SpendLedger(b, "x").spent_today() == pytest.approx(3.0)
    assert b.reconcile_fallback() == 0
    assert SpendLedger(b, "x").spent_today() == pytest.approx(3.0)
    a.close()
    b.close()


def test_unmerged_records_in_an_archive_still_count_and_merge_later(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    ledger = _ledger(store)
    store.add_spend_micro = _locked  # type: ignore[method-assign]
    ledger.add(0.5)
    del store.add_spend_micro  # Back to the real method.
    # A reconcile moved the file aside and then crashed before its merge committed.
    path = ledger.fallback_path
    path.rename(path.with_name(path.name + ".merged-20261011T000000000000Z-deadbeef"))
    assert not path.exists()
    assert ledger.spent_today() == pytest.approx(0.5)
    assert store.reconcile_fallback(now=DAY) == 1
    assert store.spend_today("x", now=DAY) == pytest.approx(0.5)
    assert ledger.spent_today() == pytest.approx(0.5)  # Not twice.
    assert list(tmp_path.glob("*.merged-*.done"))
    store.close()


def test_a_fallback_append_waits_for_the_rotation_lock(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    ledger = _ledger(store)
    store.add_spend_micro = _locked  # type: ignore[method-assign]
    done = threading.Event()

    def write() -> None:
        ledger.add(1.0)
        done.set()

    with fallback_lock(ledger.fallback_path):
        worker = threading.Thread(target=write)
        worker.start()
        time.sleep(0.2)
        assert not done.is_set()  # Blocked while the lock is held.
    worker.join(5)
    assert done.is_set()
    assert ledger.spent_today() == pytest.approx(1.0)
    store.close()


# -- LOW-1: a fallback settle across midnight counts on the settle day --------------------


def test_fallback_settle_after_midnight_counts_on_the_settle_day(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    clock = Clock(datetime(2026, 10, 13, 23, 59, tzinfo=UTC))
    ledger = _ledger(store, clock)
    reservation = ledger.reserve(5.0, 100.0)
    clock.when = datetime(2026, 10, 14, 0, 1, tzinfo=UTC)
    store.settle_spend = _locked  # type: ignore[method-assign]
    reservation.settle(5.0)
    del store.settle_spend
    record = json.loads(ledger.fallback_path.read_text().splitlines()[-1])
    assert record["settle_day"] == "2026-10-14"
    assert ledger.spent_today() == pytest.approx(5.0)  # As the database path would give.
    assert ledger.spent_this_run() == pytest.approx(5.0)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(1.0, 5.5)

    assert store.reconcile_fallback(now=clock.when) == 1
    assert store.spend_on("x", "2026-10-14") == pytest.approx(5.0)
    assert store.spend_on("x", "2026-10-13") == pytest.approx(5.0)
    assert ledger.spent_today() == pytest.approx(5.0)
    assert ledger.spent_this_run() == pytest.approx(5.0)  # Counted once per run.
    store.close()


def test_fallback_resettle_to_the_reserve_day_drops_the_mirror(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    clock = Clock(datetime(2026, 10, 13, 23, 59, tzinfo=UTC))
    ledger = _ledger(store, clock)
    reservation = ledger.reserve(5.0, 100.0)
    clock.when = datetime(2026, 10, 14, 0, 1, tzinfo=UTC)
    reservation.settle(3.0)  # In the database, with a mirror on 10-14.
    assert store.spend_on("x", "2026-10-14") == pytest.approx(3.0)
    store.settle_spend = _locked  # type: ignore[method-assign]
    reservation.settle(0.0)  # Released, via the fallback file.
    del store.settle_spend
    assert ledger.spent_today() == pytest.approx(0.0)
    store.reconcile_fallback(now=clock.when)
    assert store.spend_on("x", "2026-10-14") == pytest.approx(0.0)
    assert store.spend_on("x", "2026-10-13") == pytest.approx(0.0)
    store.close()


# -- LOW-3 and NIT-2: legacy expiry, legacy carried forward, nonces -----------------------


def test_legacy_records_expire_after_seven_days(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = Storage(tmp_path / "c.db")
    ledger = _ledger(store)
    old = {"what": "add", "platform": "x", "run_id": 0, "day": "2026-10-11",
           "when": isoformat(DAY - timedelta(days=8)), "micro_usd": 400_000}  # fmt: skip
    new = {"what": "add", "platform": "x", "run_id": 0, "day": "2026-10-11",
           "when": isoformat(DAY - timedelta(hours=1)), "micro_usd": 100_000}  # fmt: skip
    ledger.fallback_path.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
    with caplog.at_level(logging.WARNING):
        assert ledger.spent_today() == pytest.approx(0.1)
    assert any("more than 7 days old" in r.getMessage() for r in caplog.records)

    store.reconcile_fallback(now=DAY)
    lines = ledger.fallback_path.read_text().splitlines()
    assert [json.loads(x) for x in lines] == [new]  # Expired one not carried forward.
    assert lines[0] == json.dumps(new)  # Copied verbatim: same hash as the original.
    assert ledger.spent_today() == pytest.approx(0.1)  # Counted once, not per file.
    store.reconcile_fallback(now=DAY)
    assert ledger.fallback_path.read_text().splitlines() == lines
    assert ledger.spent_today() == pytest.approx(0.1)
    store.close()


def test_identical_fallback_charges_are_two_records(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    ledger = _ledger(store)  # Fixed clock: both records have the same time.
    store.add_spend_micro = _locked  # type: ignore[method-assign]
    ledger.add(0.25)
    ledger.add(0.25)
    del store.add_spend_micro
    records = [json.loads(x) for x in ledger.fallback_path.read_text().splitlines()]
    assert len({r["nonce"] for r in records}) == 2
    assert ledger.spent_today() == pytest.approx(0.5)
    assert store.reconcile_fallback(now=DAY) == 2
    assert store.spend_today("x", now=DAY) == pytest.approx(0.5)
    store.close()


# -- LOW-2: secret option names ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "api_key_v2", "token_2", "tokenV2", "key2", "apiKeyV3", "auth_header",
        "authorization_header", "authHeader", "jwt", "user_jwt", "sessionid", "passkey",
        "api_keys", "accessTokens", "refresh_tokens", "consumer_keys",
    ],
)  # fmt: skip
def test_more_secret_names_are_dropped(name: str) -> None:
    assert is_secret_option(name)


@pytest.mark.parametrize(
    "name",
    [
        "max_usd", "max_discovery_usd", "budget_scope", "keywords", "max_keys",
        "token_budget", "sessionLength", "auth_mode", "pass_rate", "keyword_v2", "tokens",
    ],
)  # fmt: skip
def test_ordinary_option_names_are_kept(name: str) -> None:
    assert not is_secret_option(name)


# -- MEDIUM-1: read-only report on an unmigrated or WAL database --------------------------

_MAIN_SCHEMA = """
CREATE TABLE accounts (
    platform TEXT NOT NULL, user_id TEXT NOT NULL, handle TEXT NOT NULL, created_at TEXT,
    status TEXT NOT NULL DEFAULT 'active', meta_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL, activity_since TEXT, activity_until TEXT,
    activity_fetched_at TEXT, PRIMARY KEY (platform, user_id)
);
CREATE TABLE activities (
    platform TEXT NOT NULL, user_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
    community TEXT, content_hash TEXT, fetched_at TEXT NOT NULL
);
CREATE TABLE runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
    platform TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
    params_json TEXT NOT NULL, account_ids_json TEXT NOT NULL DEFAULT '[]',
    summary_json TEXT NOT NULL DEFAULT '{}'
);
"""

_posix_perms = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs file permissions"
)


@_posix_perms
def test_report_on_an_unmigrated_main_schema_database_read_only(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    assert runner.invoke(app, scan_args(src, "--out", str(src / "a.csv"))).exit_code == 0
    db = tmp_path / "main.db"
    conn = sqlite3.connect(db)  # The schema main writes: no migrations, rollback journal.
    conn.executescript(_MAIN_SCHEMA)
    conn.execute("ATTACH DATABASE ? AS src", (str(src / "c.db"),))
    conn.execute(
        "INSERT INTO accounts SELECT platform, user_id, handle, created_at, status, meta_json,"
        " fetched_at, activity_since, activity_until, activity_fetched_at FROM src.accounts"
    )
    conn.execute("INSERT INTO activities SELECT * FROM src.activities")
    conn.execute(
        "INSERT INTO runs SELECT run_id, started_at, finished_at, platform, status, params_json,"
        " account_ids_json, summary_json FROM src.runs"
    )
    conn.commit()
    conn.execute("DETACH DATABASE src")
    conn.close()
    db.chmod(stat.S_IRUSR)
    try:
        out = tmp_path / "r.csv"
        result = runner.invoke(app, ["report", "--run-id", "1", "--db", str(db), "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        with Storage(db) as store:
            assert store.read_only
            assert store.get_run_sample(1) == []
            assert store.get_run_truncated(1) == {}
            assert store.get_run_window_ends(1) == {}
            assert store.get_run(1)["root_run_id"] == 1  # type: ignore[index]
    finally:
        db.chmod(stat.S_IRUSR | stat.S_IWUSR)


@_posix_perms
def test_report_on_a_wal_database_in_a_read_only_directory(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    assert runner.invoke(app, scan_args(src, "--out", str(src / "a.csv"))).exit_code == 0
    assert not (src / "c.db-shm").exists()  # Closed cleanly; reopening must create it.
    src.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        out = tmp_path / "r.csv"
        result = runner.invoke(
            app, ["report", "--run-id", "1", "--db", str(src / "c.db"), "--out", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
    finally:
        src.chmod(stat.S_IRWXU)
