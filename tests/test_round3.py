"""Round-3 review fixes: fallback records tied to their database, reconcile and
reclaim at run start, settles across midnight, post-horizon activity, horizon-cut
cache windows, cancelled requests, reset-wait cap, secret names, read-only report."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app, resume_command
from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.pipeline import is_secret_option, run_scan, storable_options
from bot_farm_silence_tracker.scoring import ScoringParams, score_account
from bot_farm_silence_tracker.sources.errors import BudgetExceeded, RateLimited, StaleSourceError
from bot_farm_silence_tracker.sources.http import CancelledRequest, reset_at_epoch
from bot_farm_silence_tracker.storage import SPEND_RESERVED, SPEND_SETTLED, Storage

from .helpers import CUTOFF, EVENT, NOW, at, steady
from .test_data_horizon import FRESH, STALE, HorizonSource, horizon_source
from .test_fake_and_cli import scan_args
from .test_http import client
from .test_pipeline import SEEDS, N, make_source, settings

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


def _failing_settles(store: Storage) -> Any:
    real = store.settle_spend

    def bad(*a: Any, **k: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    store.settle_spend = bad  # type: ignore[method-assign]
    return real


# -- M-1: fallback records belong to one database ------------------------------------


def _remove_db(db: Path) -> None:
    db.unlink()
    for extra in db.parent.glob(db.name + "-*"):
        extra.unlink()


def test_fallback_of_a_deleted_database_cannot_lower_the_new_total(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "c.db"
    store = Storage(db)
    ledger = _ledger(store)
    reservation = ledger.reserve(5.0, 100.0)
    real = _failing_settles(store)
    reservation.settle(0.5)  # Lands in the fallback file for event 1.
    store.settle_spend = real  # type: ignore[method-assign]
    assert ledger.spent_today() == pytest.approx(0.5)
    record = json.loads(ledger.fallback_path.read_text().splitlines()[-1])
    assert record["db_id"] == store.db_id
    store.close()
    _remove_db(db)

    fresh = Storage(db)
    assert fresh.db_id != record["db_id"]
    caplog.set_level(logging.WARNING, logger="bot_farm_silence_tracker")
    new = _ledger(fresh)
    first = new.reserve(9.0, 10.0)
    assert first.event_id == 1  # Same id as the old record.
    assert new.spent_today() == pytest.approx(9.0)
    first.settle(9.0)
    assert new.spent_today() == pytest.approx(9.0)
    with pytest.raises(BudgetExceeded):
        new.reserve(5.0, 10.0)
    new.spent_today()
    foreign = [r for r in caplog.records if "another database" in r.getMessage()]
    assert len(foreign) == 1  # Warned once.
    fresh.close()


def test_released_reservation_of_a_deleted_database_does_not_open_the_cap(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    store = Storage(db)
    ledger = _ledger(store)
    reservation = ledger.reserve(5.0, 100.0)
    real = _failing_settles(store)
    reservation.settle(0.0)
    store.settle_spend = real  # type: ignore[method-assign]
    store.close()
    _remove_db(db)
    fresh = Storage(db)
    new = _ledger(fresh)
    new.reserve(8.0, 10.0).settle(8.0)
    with pytest.raises(BudgetExceeded):
        new.reserve(6.0, 10.0)
    assert fresh.spend_today("x", now=DAY) == pytest.approx(8.0)
    fresh.close()


def test_settle_record_without_a_row_never_lowers_the_total(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    ledger = _ledger(store)
    ledger.reserve(2.0, 100.0)
    base = {"what": "settle", "platform": "x", "run_id": ledger.run_id, "day": "2026-10-11",
            "event_id": 999, "seq": 1, "reserved_micro_usd": 5_000_000,
            "db_id": store.db_id}  # fmt: skip
    ledger.fallback_path.write_text(json.dumps({**base, "settled_micro_usd": 0}) + "\n")
    assert ledger.spent_today() == pytest.approx(2.0)
    ledger.fallback_path.write_text(json.dumps({**base, "settled_micro_usd": 1_000_000}) + "\n")
    assert ledger.spent_today() == pytest.approx(3.0)
    store.close()


def test_legacy_settle_records_never_lower_the_total(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    ledger = _ledger(store)
    ledger.reserve(5.0, 100.0)  # Event 1, held at 5.
    legacy = {"what": "settle", "platform": "x", "run_id": ledger.run_id, "day": "2026-10-11",
              "event_id": 1, "seq": 1, "settled_micro_usd": 500_000}  # fmt: skip
    ledger.fallback_path.write_text(json.dumps(legacy) + "\n")
    assert ledger.spent_today() == pytest.approx(5.0)  # The decrease is ignored.
    legacy["settled_micro_usd"] = 7_000_000
    ledger.fallback_path.write_text(json.dumps(legacy) + "\n")
    assert ledger.spent_today() == pytest.approx(7.0)  # The increase counts.
    store.close()


def test_memory_databases_get_unique_fallback_paths() -> None:
    a, b = Storage(":memory:"), Storage(":memory:")
    pa, pb = SpendLedger(a, "x").fallback_path, SpendLedger(b, "x").fallback_path
    assert pa != pb
    assert ".cache" not in str(pa) and a.db_id is not None and a.db_id in pa.name
    a.close()
    b.close()


# -- L-6: reconcile the fallback file at run start -------------------------------------


def test_reconcile_merges_this_databases_records_once_and_rotates(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    ledger = _ledger(store)
    reservation = ledger.reserve(5.0, 100.0)
    real = _failing_settles(store)
    reservation.settle(1.5)
    store.settle_spend = real  # type: ignore[method-assign]
    real_add = store.add_spend_micro
    store.add_spend_micro = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        sqlite3.OperationalError("locked")
    )
    ledger.add(0.25)
    store.add_spend_micro = real_add  # type: ignore[method-assign]
    legacy = {"what": "add", "platform": "x", "run_id": ledger.run_id, "day": "2026-10-11",
              "micro_usd": 100_000}  # fmt: skip
    with ledger.fallback_path.open("a") as fh:
        fh.write(json.dumps(legacy, sort_keys=True) + "\n")
    before = ledger.spent_today()
    assert before == pytest.approx(1.85)
    assert store.spend_today("x", now=DAY) == pytest.approx(5.0)

    assert store.reconcile_fallback(now=DAY) == 2
    assert store.spend_today("x", now=DAY) == pytest.approx(1.75)  # 1.5 settled + 0.25 add.
    assert ledger.spent_today() == pytest.approx(before)  # Nothing counted twice.
    lines = ledger.fallback_path.read_text().splitlines()
    assert [json.loads(x) for x in lines] == [legacy]  # Only the legacy record stays.
    assert list(tmp_path.glob("c.db.spend-fallback.jsonl.merged-*"))
    assert store.reconcile_fallback(now=DAY) == 0  # Idempotent.
    assert ledger.spent_today() == pytest.approx(before)
    store.close()


def test_run_scan_reconciles_the_fallback_file(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        path = store.spend_fallback_path
        rec = {"what": "add", "platform": "fake", "run_id": 0, "day": "2026-10-11",
               "micro_usd": 250_000, "db_id": store.db_id}  # fmt: skip
        path.write_text(json.dumps(rec, sort_keys=True) + "\n")
        run_scan(make_source(), SEEDS, store, settings())
        assert not path.exists()
        assert store.spend_on("fake", "2026-10-11") == pytest.approx(0.25)


# -- L-1: stale reservations --------------------------------------------------------------


def test_stale_reservations_of_finished_runs_are_closed_at_their_reserved_amount(
    tmp_path: Path,
) -> None:
    store = Storage(tmp_path / "c.db")
    clock = Clock(DAY - timedelta(hours=2))
    ledger = _ledger(store, clock)
    old = ledger.reserve(3.0, 100.0)
    clock.when = DAY - timedelta(minutes=10)
    recent = ledger.reserve(1.0, 100.0)
    open_run = _ledger(store, Clock(DAY - timedelta(hours=2)))
    other = open_run.reserve(2.0, 100.0)  # Its run never finished.
    store.finish_run(ledger.run_id, status="failed", account_ids=[], summary={},  # type: ignore[arg-type]
                     finished_at=DAY)  # fmt: skip
    assert store.reclaim_stale_reservations(now=DAY) == 1
    rows = store.spend_events([old.event_id, recent.event_id, other.event_id])
    assert rows[old.event_id]["state"] == SPEND_SETTLED
    assert rows[old.event_id]["micro_usd"] == 3_000_000  # Conservative: worst case kept.
    assert rows[recent.event_id]["state"] == SPEND_RESERVED
    assert rows[other.event_id]["state"] == SPEND_RESERVED
    assert store.spend_today("x", now=DAY) == pytest.approx(6.0)
    store.close()


# -- Coordinator L2: a settle after midnight counts on both days -------------------------


def test_settle_across_midnight_counts_on_both_days(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    clock = Clock(datetime(2026, 10, 11, 23, 59, 59, tzinfo=UTC))
    ledger = _ledger(store, clock)
    reservation = ledger.reserve(2.0, 100.0)
    clock.when = datetime(2026, 10, 12, 0, 0, 1, tzinfo=UTC)
    reservation.settle(1.5)
    assert store.spend_on("x", "2026-10-11") == pytest.approx(1.5)
    assert store.spend_on("x", "2026-10-12") == pytest.approx(1.5)
    assert ledger.spent_today() == pytest.approx(1.5)  # The new day's cap sees it.
    assert ledger.spent_this_run() == pytest.approx(1.5)  # Counted once per run.
    assert ledger.spent("run") == pytest.approx(1.5)
    reservation.settle(1.75)  # Settling again replaces, never adds.
    assert store.spend_on("x", "2026-10-12") == pytest.approx(1.75)
    assert ledger.spent_this_run() == pytest.approx(1.75)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(1.0, 2.5)
    store.close()


def test_an_older_settle_never_overwrites_a_newer_one(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    ledger = _ledger(store)
    event = ledger.reserve(5.0, 100.0).event_id
    store.settle_spend(event, 2_000_000, seq=2)
    store.settle_spend(event, 1_000_000, seq=1)
    assert store.spend_events([event])[event]["micro_usd"] == 2_000_000
    store.close()


# -- M-2: activity after the horizon still rules out a cliff ------------------------------


def test_post_after_the_horizon_is_still_evidence() -> None:
    acts = steady("u1", at(-30), at(-0.1), per_day=4)
    late = steady("u1", at(2), at(2.1), per_day=4)[:1]  # After the horizon below.
    account = Account("fake", "u1", "u1", at(-400))
    live = score_account(account, acts + late, ScoringParams(cutoff=CUTOFF, now=NOW))
    lagged = score_account(
        account, acts + late, ScoringParams(cutoff=CUTOFF, now=NOW, horizon=at(1))
    )
    for result in (live, lagged):
        assert not result.cliff and result.posts_after_cutoff == 1
    assert lagged.silence_hours is not None and lagged.silence_hours >= 0
    # Without the late post, the lagging source still scores silence to the horizon.
    quiet = score_account(account, acts, ScoringParams(cutoff=CUTOFF, now=NOW, horizon=at(3.5)))
    assert quiet.cliff
    assert quiet.silence_hours == pytest.approx((at(3.5) - acts[-1].ts).total_seconds() / 3600)


# -- M-3: windows cut by the horizon are refetched once the source catches up ------------


def test_stale_run_then_caught_up_resume_refetches(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        stale_src = horizon_source(STALE)
        first = run_scan(stale_src, SEEDS, store, settings(allow_stale_source=True))
        sampled = store.get_run_sample(first.run_id)
        uid = sampled[0][0].user_id
        window = store.activity_window("fake", uid)
        assert window is not None and window[1] == STALE  # min(until, horizon).
        still = horizon_source(STALE)
        again = run_scan(
            still, SEEDS, store, settings(allow_stale_source=True), from_run=first.run_id
        )
        assert still.fetches == []  # Same horizon: nothing new to see, reused.
        caught_up: HorizonSource = horizon_source(FRESH)
        resumed = run_scan(caught_up, SEEDS, store, settings(), from_run=again.run_id)
        assert len(caught_up.fetches) == N  # Every horizon-cut window is refetched.
        assert not resumed.params.force_provisional
        assert store.activity_window("fake", uid)[1] == FRESH  # type: ignore[index]
        assert not any("look silent" in w for w in resumed.warnings)


def test_stale_abort_summary_records_spend(tmp_path: Path) -> None:
    class PaidHorizon(HorizonSource):
        def data_horizon(self) -> datetime | None:
            assert self.config.ledger is not None
            self.config.ledger.add(0.05)
            return STALE

    with Storage(tmp_path / "c.db") as store:
        src = make_source(store, cls=PaidHorizon)
        with pytest.raises(StaleSourceError):
            run_scan(src, SEEDS, store, settings())
        summary = store.get_run(1)["summary"]  # type: ignore[index]
        assert summary["spend_usd"] == pytest.approx(0.05)
        assert summary["chain_spend_usd"] == pytest.approx(0.05)


def test_resume_command_repeats_allow_stale_source() -> None:
    values = {"cutoff": EVENT.cutoff_text}
    plain = resume_command("fake", 3, None, values, {}, event_id=EVENT.id)
    assert "--allow-stale-source" not in plain
    stale = resume_command("fake", 3, None, values, {}, event_id=EVENT.id, allow_stale_source=True)
    assert f"--from-run 3 --event {EVENT.id} --allow-stale-source" in stale


# -- L-2: cancelled and unbuildable requests --------------------------------------------


def test_interrupt_mid_send_reports_a_cancelled_request_with_its_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    http, _ = client(handler)
    errors: list[BaseException] = []
    http.configure(on_attempt_failed=lambda a, e: errors.append(e))
    with pytest.raises(KeyboardInterrupt):
        http.get("https://api.example.test/x?q=1")
    (error,) = errors
    assert isinstance(error, CancelledRequest) and isinstance(error, httpx.RequestError)
    assert error.request.url.path == "/x"  # Never raises.
    assert isinstance(error.cause, KeyboardInterrupt)


def test_unencodable_payload_is_not_sent() -> None:
    sent: list[httpx.Request] = []
    http, _ = client(lambda r: sent.append(r) or httpx.Response(200))  # type: ignore[func-returns-value]
    log: list[str] = []
    http.configure(
        before_attempt=lambda a: log.append("reserve"),
        on_attempt_failed=lambda a, e: log.append("failed"),
        on_attempt_not_sent=lambda a, e: log.append("not_sent"),
    )
    with pytest.raises(TypeError):
        http.request("POST", "https://api.example.test/x", json={"a": object()})
    assert log == ["reserve", "not_sent"] and sent == [] and http.requests_made == 0


# -- L-4: reset waits have their own total ------------------------------------------------


def test_reset_header_waits_are_capped_per_call() -> None:
    now = 1_000_000.0
    http, sleeps = client(
        lambda r: httpx.Response(429, headers={"x-rate-limit-reset": str(int(now + 700))})
    )
    http.configure(reset_wait=reset_at_epoch("x-rate-limit-reset", clock=lambda: now))
    with pytest.raises(RateLimited, match="reset wait budget of 1800s"):
        http.get("https://api.example.test/x")
    assert sleeps == [701.0, 701.0]


# -- L-3: secret option names ---------------------------------------------------------------

STORED = ("sessionLength", "auth_mode", "pass_rate", "token_budget", "passThrough", "max_usd",
          "max_discovery_usd", "budget_scope", "keywords", "max_keys", "authorName",
          "apiKeyword", "maxTokens", "tokenizer")  # fmt: skip
DROPPED = ("XAPIKey", "passphrase", "pwd", "private_key", "privatekey", "privateKey",
           "sessionId", "session_token", "auth_token", "authorization", "keyId", "api_key",
           "oauth_token", "clientSecret", "x-api-key", "cookieJar", "credentialsFile",
           "APIKey", "token", "key")  # fmt: skip


@pytest.mark.parametrize("key", STORED)
def test_benign_option_names_are_stored(key: str) -> None:
    assert not is_secret_option(key)
    assert storable_options({key: "v"}) == {key: "v"}


@pytest.mark.parametrize("key", DROPPED)
def test_secret_option_names_are_dropped(key: str) -> None:
    assert is_secret_option(key)


# -- L-5: concurrent migration and read-only report ---------------------------------------


class _NoColumns:
    """Connection proxy whose ``PRAGMA table_info`` reports no columns."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.startswith("PRAGMA table_info"):
            return iter(())
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_migration_tolerates_a_column_added_by_another_process(tmp_path: Path) -> None:
    store = Storage(tmp_path / "c.db")
    real = store._conn
    store._conn = _NoColumns(real)  # type: ignore[assignment]
    store._migrate()  # Every ALTER hits "duplicate column"; none raises.
    store._conn = real
    store.close()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs file permissions")
def test_report_works_on_a_read_only_database(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    result = runner.invoke(app, scan_args(tmp_path, "--out", str(tmp_path / "a.csv")))
    assert result.exit_code == 0, result.output
    db.chmod(stat.S_IRUSR)
    try:
        store = Storage(db)
        assert store.read_only and store.db_id is not None
        store.close()
        report = runner.invoke(
            app, ["report", "--run-id", "1", "--db", str(db), "--out", str(tmp_path / "r.csv")]
        )
        assert report.exit_code == 0, report.output
        assert (tmp_path / "r.csv").exists()
    finally:
        db.chmod(stat.S_IRUSR | stat.S_IWUSR)
