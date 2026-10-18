"""Regression tests for the six low-severity findings of the core review."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import bot_farm_silence_tracker.storage as storage_mod
from bot_farm_silence_tracker.cli import app, resolve_resume
from bot_farm_silence_tracker.pipeline import (
    has_url_userinfo,
    is_secret_option,
    run_scan,
    storable_options,
    unstored_options,
)
from bot_farm_silence_tracker.storage import Storage, fallback_lock, load_spend_fallback_all
from bot_farm_silence_tracker.timeutil import isoformat

from .test_pipeline import SEEDS, make_source, settings

DAY = datetime(2026, 10, 11, 12, tzinfo=UTC)


def _record(**kw: object) -> str:
    base = {"what": "add", "platform": "x", "day": "2026-10-11", "run_id": 1, "micro_usd": 5}
    base.update(kw)
    return json.dumps(base) + "\n"


# -- 1. fallback reads: active file first, then the archive listing -------------------


def test_fallback_read_survives_rotation_between_read_and_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = tmp_path / "c.db.spend-fallback.jsonl"
    active.write_text(_record(db_id="d1"))
    real = storage_mod.fallback_archives

    def rotate_then_list(path: Path) -> list[Path]:
        # Another process renames the active file right before the archives are listed.
        if path.exists():
            path.rename(path.with_name(path.name + ".merged-20261011T000000000000Z-deadbeef"))
        return real(path)

    monkeypatch.setattr(storage_mod, "fallback_archives", rotate_then_list)
    assert len(load_spend_fallback_all(active)) == 1


# -- 2. read-only open ----------------------------------------------------------------


def _make_db(path: Path) -> None:
    with Storage(path):
        pass


def _fail_mode_ro(monkeypatch: pytest.MonkeyPatch, message: str) -> list[str]:
    real = Storage._connect_uri
    seen: list[str] = []

    def connect(uri: str) -> sqlite3.Connection:
        seen.append(uri)
        if "mode=ro" in uri:
            raise sqlite3.OperationalError(message)
        return real(uri)

    monkeypatch.setattr(Storage, "_connect_uri", staticmethod(connect))
    return seen


def test_read_only_falls_back_to_immutable_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "c.db"
    _make_db(db)
    seen = _fail_mode_ro(monkeypatch, "unable to open database file")
    with caplog.at_level(logging.INFO), Storage(db, read_only=True) as store:
        assert store.read_only
    assert any("immutable=1" in u for u in seen)
    assert any(
        r.levelno == logging.WARNING and "immutable=1" in r.getMessage() for r in caplog.records
    )


def test_read_only_falls_back_on_a_readonly_directory_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "c.db"
    _make_db(db)
    seen = _fail_mode_ro(monkeypatch, "attempt to write a readonly database")
    with Storage(db, read_only=True) as store:
        assert store.read_only
    assert any("immutable=1" in u for u in seen)


def test_read_only_does_not_hide_a_non_empty_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "c.db"
    _make_db(db)
    (tmp_path / "c.db-wal").write_bytes(b"pending frames")
    seen = _fail_mode_ro(monkeypatch, "unable to open database file")
    with pytest.raises(sqlite3.OperationalError):
        Storage(db, read_only=True)
    assert not any("immutable=1" in u for u in seen)


def test_read_only_does_not_fall_back_on_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "c.db"
    _make_db(db)
    seen = _fail_mode_ro(monkeypatch, "database is locked")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        Storage(db, read_only=True)
    assert not any("immutable=1" in u for u in seen)


# -- 3. fallback_lock warns once per process ------------------------------------------


def test_fallback_lock_warns_once_when_the_lock_cannot_be_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(storage_mod, "_LOCK_WARNED", False)

    def deny(*_a: object, **_k: object) -> None:
        raise OSError("no locks here")

    monkeypatch.setattr(storage_mod, "_lock_exclusive", deny)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with fallback_lock(tmp_path / "c.db.spend-fallback.jsonl"):
                pass
    warned = [r for r in caplog.records if "Could not lock" in r.getMessage()]
    assert len(warned) == 1
    assert warned[0].levelno == logging.WARNING


def test_fallback_lock_warns_when_the_lock_file_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(storage_mod, "_LOCK_WARNED", False)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    with caplog.at_level(logging.WARNING), fallback_lock(blocker / "c.db.spend-fallback.jsonl"):
        pass
    assert any("Could not lock" in r.getMessage() for r in caplog.records)


# -- 4. window-end write failure does not fail the scan -------------------------------


def test_window_end_write_failure_is_a_warning_not_a_failed_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(self: Storage, *_a: object, **_k: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Storage, "set_sample_window_ends", busy)
    with Storage(tmp_path / "c.db") as store:
        outcome = run_scan(make_source(), SEEDS, store, settings())
    assert outcome.status == "complete"
    assert outcome.results
    assert any("window ends" in w for w in outcome.warnings)


# -- 5. archive growth ----------------------------------------------------------------


def test_old_done_archives_are_pruned_and_recent_ones_kept(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        base = store.spend_fallback_path
        old = base.with_name(base.name + ".merged-20260101T000000000000Z-aaaaaaaa.done")
        fresh = base.with_name(base.name + ".merged-20261001T000000000000Z-bbbbbbbb.done")
        for f in (old, fresh):
            f.write_text(_record(db_id="other"))
        aged = (DAY - timedelta(days=31)).timestamp()
        os.utime(old, (aged, aged))
        recent = (DAY - timedelta(days=2)).timestamp()
        os.utime(fresh, (recent, recent))
        # An active file is needed for reconcile to do any work.
        base.write_text(_record(db_id=store.db_id))
        store.reconcile_fallback(now=DAY)
    assert not old.exists()
    assert fresh.exists()


def test_active_file_with_only_carried_legacy_lines_is_not_rotated(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        path = store.spend_fallback_path
        legacy = _record(when=isoformat(DAY - timedelta(hours=36)))  # No db_id.
        path.write_text(legacy)
        store.reconcile_fallback(now=DAY)
        assert path.read_text() == legacy
        assert not list(tmp_path.glob("*.merged-*"))
        store.reconcile_fallback(now=DAY)
        assert not list(tmp_path.glob("*.merged-*"))


# -- 6. secret option rule ------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["pem", "tls_pem", "totp_seed", "totpSeed", "otp_seed", "otpSeed", "seed_phrase",
            "seedPhrase", "mnemonic", "wallet_mnemonic", "hmac", "hmac_key", "dsn", "sentry_dsn",
            "database_url", "DATABASE_URL", "databaseUrl", "hotp_seed", "hotpSeed", "hotpseed",
            "mfa_seed", "mfaSeed", "mfaseed", "2fa_seed", "2faSeed", "twofa_seed", "twofaSeed",
            "twoFaSeed", "recovery_seed", "recoverySeed", "wallet_seed", "walletSeed",
            "seed_words", "seedWords", "seedwords"]
)  # fmt: skip
def test_new_secret_keys_are_dropped(key: str) -> None:
    assert is_secret_option(key)
    assert key not in storable_options({key: "value"})


@pytest.mark.parametrize(
    "key", ["seed", "seeds", "world_seed", "worldSeed", "seed_count", "random_seed",
            "database_name", "url", "population"]
)  # fmt: skip
def test_near_miss_keys_are_still_stored(key: str) -> None:
    # A bare ``seed`` segment is a random seed, not a secret.
    assert not is_secret_option(key)
    assert key in storable_options({key: 1})


@pytest.mark.parametrize(
    "value",
    [
        "http://u:p@[::1", "http://u:p@h]:80", "//u:p@h", "//u@h/x",
        " //u:p@h", "\t//u:p@h", "/\t/u:p@h", " http://u:p@[h", "\n//u:p@h",
        "http:\\\\u:p@h", " http://u:p@h",
        "http:/u:p@h", "http:u:p@h", "http:///u:p@h", "https:/u:p@h", "https:u:p@h",
        "ftp:u:p@h", "ws:/u:p@h", "wss:///u:p@h", "HTTP:u:p@h", "http:u@h",
    ],
)  # fmt: skip
def test_malformed_or_scheme_relative_userinfo_urls_are_not_stored(value: str) -> None:
    assert has_url_userinfo(value)
    assert storable_options({"proxy": value}) == {}
    assert unstored_options({"proxy": value}) == ["proxy"]


@pytest.mark.parametrize(
    "value",
    [
        "//host/path", "http://[::1", "http://h]:80/a@b", "a@b.com", "user@example.com",
        "@handle", "mailto:a@b.com", "user:pass@host", "http://h/p@x", "https://h?x=a@b",
        "ftp://h#@x", "//h/@x",
        "C:\\Users\\me@corp\\x", "D:\\me@x", "\\\\server\\share\\me@x",
        "domain\\user@host", "foo\\@bar", "http:h/p@x", "http:///h?x=a@b", "wss:h#@x",
    ],
)  # fmt: skip
def test_malformed_urls_without_userinfo_are_stored(value: str) -> None:
    assert not has_url_userinfo(value)
    assert storable_options({"proxy": value}) == {"proxy": value}


def test_run_params_record_unstored_key_names_only() -> None:
    cfg = settings(source_options={"proxy": "http://u:hunter2@h:1", "api_key": "k", "n": "3"})
    params = cfg.to_dict()
    assert params["unstored_option_keys"] == ["api_key", "proxy"]
    assert "hunter2" not in json.dumps(params)
    assert params["source_options"] == {"n": "3"}


def _resume_warnings(stored: dict, options: dict, capsys: pytest.CaptureFixture[str]) -> str:
    resolve_resume(1, stored, {}, options, force=False)
    return capsys.readouterr().err


def test_resume_warns_once_per_omitted_unstored_key(capsys: pytest.CaptureFixture[str]) -> None:
    stored = {"source_options": {}, "unstored_option_keys": ["api_key", "proxy"]}
    err = _resume_warnings(stored, {}, capsys)
    assert err.count("'api_key'") == 1
    assert err.count("'proxy'") == 1


def test_unstored_keys_carry_forward_through_a_chain_of_resumes_via_the_cli(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    db = tmp_path / "c.db"
    common = ["scan", "-p", "fake", "--db", str(db), "--now", "2026-10-14T00:00:00Z"]
    run1 = runner.invoke(
        app,
        [*common, "--out", str(tmp_path / "1.csv"),
         "--source-option", "api_token=SECRET1",
         "--source-option", "proxy=https://usr:PW@h.example"],
    )  # fmt: skip
    assert run1.exit_code == 0, run1.output
    outputs = [run1.output]
    for number in (2, 3):
        resumed = runner.invoke(
            app, [*common, "--from-run", str(number - 1), "--out", str(tmp_path / f"{number}.csv")]
        )
        assert resumed.exit_code == 0, resumed.output
        assert "'api_token'" in resumed.output and "'proxy'" in resumed.output
        outputs.append(resumed.output)
    with Storage(db) as store:
        for number in (1, 2, 3):
            keys = store.get_run(number)["params"]["unstored_option_keys"]  # type: ignore[index]
            assert keys == ["api_token", "proxy"], number
    with closing(sqlite3.connect(str(db))) as conn:
        dump = "\n".join(conn.iterdump())
    for needle in ("SECRET1", "PW", "usr:"):
        assert needle not in dump
        assert all(needle not in out for out in outputs)


def test_resume_with_a_credential_free_value_for_an_unstored_key_is_not_a_conflict(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    db = tmp_path / "c.db"
    common = ["scan", "-p", "fake", "--db", str(db), "--now", "2026-10-14T00:00:00Z"]
    first = runner.invoke(
        app,
        [*common, "--out", str(tmp_path / "1.csv"),
         "--source-option", "proxy=https://usr:PW@h.example"],
    )  # fmt: skip
    assert first.exit_code == 0, first.output
    resumed = runner.invoke(
        app,
        [*common, "--from-run", "1", "--out", str(tmp_path / "2.csv"),
         "--source-option", "proxy=https://h.example"],
    )  # fmt: skip
    assert resumed.exit_code == 0, resumed.output
    assert "<unset>" not in resumed.output


@pytest.mark.parametrize("key", ["sofa_seed", "sofaSeed", "fa_count", "fa", "sofa", "fantasy_seed"])
def test_fa_qualifier_does_not_flag_innocent_keys(key: str) -> None:
    assert not is_secret_option(key)


def test_resume_does_not_warn_when_the_key_is_passed_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stored = {"source_options": {}, "unstored_option_keys": ["api_key"]}
    assert _resume_warnings(stored, {"api_key": "s3cret"}, capsys) == ""


def test_resume_warning_never_contains_values(capsys: pytest.CaptureFixture[str]) -> None:
    stored = {"source_options": {"proxy": "http://***@h:1"}, "unstored_option_keys": ["api_key"]}
    err = _resume_warnings(stored, {}, capsys)
    assert "***" not in err


def test_resume_drops_legacy_redacted_value_with_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stored = {"source_options": {"proxy": "http://***@proxy.example:8080", "world_seed": "7"}}
    _, options, _ = resolve_resume(1, stored, {}, {}, force=False)
    assert options == {"world_seed": "7"}
    assert "'proxy'" in capsys.readouterr().err


def test_url_userinfo_values_are_not_stored() -> None:
    options = {
        "endpoint": "https://alice:hunter2@example.com:8443/path?q=1",
        "proxy": "http://bob@proxy.example:8080",
        "mirror": "https://example.com/plain",
        "note": "mail me at bob@example.com",
    }
    stored = storable_options(options)
    assert "endpoint" not in stored
    assert "proxy" not in stored
    assert "hunter2" not in json.dumps(stored)
    assert stored["mirror"] == "https://example.com/plain"
    assert stored["note"] == "mail me at bob@example.com"
    assert unstored_options(options) == ["endpoint", "proxy"]


def test_world_seed_is_stored_and_inherited_on_resume() -> None:
    stored = {"source_options": storable_options({"world_seed": "7"})}
    assert stored["source_options"] == {"world_seed": "7"}
    _, options, conflicts = resolve_resume(1, stored, {}, {}, force=False)
    assert options == {"world_seed": "7"}
    assert conflicts == []
    _, options, _ = resolve_resume(1, stored, {}, {"world_seed": "7"}, force=False)
    assert options == {"world_seed": "7"}


def test_a_different_world_seed_on_resume_is_a_conflict() -> None:
    stored = {"source_options": storable_options({"world_seed": "7"})}
    with pytest.raises(ValueError, match="world_seed"):
        resolve_resume(1, stored, {}, {"world_seed": "99"}, force=False)


def test_url_userinfo_value_is_taken_on_resume_without_a_conflict() -> None:
    proxy = "http://u:p@proxy.example:8080"
    stored = {"source_options": storable_options({"proxy": proxy, "world_seed": "7"})}
    assert "proxy" not in stored["source_options"]
    _, options, conflicts = resolve_resume(1, stored, {}, {"proxy": proxy}, force=False)
    assert options == {"proxy": proxy, "world_seed": "7"}
    assert conflicts == []


def test_redacted_url_stored_by_an_older_run_is_not_inherited() -> None:
    # Older versions stored URL values with the credentials replaced by ``***@``.
    stored = {"source_options": {"proxy": "http://***@proxy.example:8080", "world_seed": "7"}}
    _, options, _ = resolve_resume(1, stored, {}, {}, force=False)
    assert options == {"world_seed": "7"}
    proxy = "http://u:p@proxy.example:8080"
    _, options, conflicts = resolve_resume(1, stored, {}, {"proxy": proxy}, force=False)
    assert options["proxy"] == proxy
    assert conflicts == []


# -- NIT: merged_fallback_hashes only asks for the hashes it needs --------------------


def test_merged_fallback_hashes_filters_to_the_candidates(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        for digest in ("h1", "h2", "h3"):
            store._mark_merged(digest, DAY)
        store._conn.commit()
        assert store.merged_fallback_hashes(["h2", "zz"]) == {"h2"}
        assert store.merged_fallback_hashes([]) == set()
        assert store.merged_fallback_hashes() == {"h1", "h2", "h3"}
        many = [f"x{i}" for i in range(1200)] + ["h3"]
        assert store.merged_fallback_hashes(many) == {"h3"}


# -- 7. legacy expiry does not drop a resumed chain's own records ----------------------


def test_old_legacy_records_of_the_chain_still_count(tmp_path: Path) -> None:
    from bot_farm_silence_tracker.ledger import SpendLedger

    with Storage(tmp_path / "c.db") as store:
        ledger = SpendLedger(store, "x", clock=lambda: DAY)
        run_id = store.create_run("x", {}, DAY - timedelta(days=10))
        ledger.bind_run(run_id)
        old = DAY - timedelta(days=9)
        mine = {"what": "add", "platform": "x", "run_id": run_id, "day": "2026-10-02",
                "when": old.isoformat(), "micro_usd": 300_000}  # fmt: skip
        other = {**mine, "run_id": run_id + 100, "micro_usd": 900_000}
        ledger.fallback_path.write_text(json.dumps(mine) + "\n" + json.dumps(other) + "\n")
        assert ledger.spent_this_chain() == pytest.approx(0.3)
        # The day scope has no chain, so the age rule still applies there.
        assert ledger.spent_today() == 0


# -- 8. FATAL_SOURCE_ERRORS lives with the other source errors -------------------------


def test_fatal_source_errors_live_in_sources_errors() -> None:
    from bot_farm_silence_tracker import pipeline
    from bot_farm_silence_tracker.sources import errors

    assert (
        errors.SourceAuthError,
        errors.BudgetExceeded,
        errors.RateLimited,
    ) == errors.FATAL_SOURCE_ERRORS
    assert pipeline.FATAL_SOURCE_ERRORS is errors.FATAL_SOURCE_ERRORS


# -- Round 2: reconcile keeps a resumable chain's old legacy lines ---------------------


def _old_legacy(run_id: int, micro: int) -> dict[str, object]:
    old = DAY - timedelta(days=9)
    return {"what": "add", "platform": "x", "run_id": run_id, "day": "2026-10-02",
            "when": old.isoformat(), "micro_usd": micro}  # fmt: skip


def _finish(store: Storage, run_id: int, status: str) -> None:
    finished = DAY - timedelta(days=9)
    store.finish_run(run_id, status=status, account_ids=[], summary={}, finished_at=finished)


def test_reconcile_keeps_old_legacy_lines_of_a_resumable_chain(tmp_path: Path) -> None:
    from bot_farm_silence_tracker.ledger import SpendLedger

    with Storage(tmp_path / "c.db") as store:
        ledger = SpendLedger(store, "x", clock=lambda: DAY)
        run_id = store.create_run("x", {}, DAY - timedelta(days=10))
        _finish(store, run_id, "partial")
        ledger.bind_run(run_id)
        other = run_id + 100  # Not a run of this database.
        ledger.fallback_path.write_text(
            json.dumps(_old_legacy(run_id, 300_000)) + "\n"
            + json.dumps(_old_legacy(other, 900_000)) + "\n"
        )  # fmt: skip
        store.reconcile_fallback(now=DAY)
        assert ledger.spent_this_chain() == pytest.approx(0.3)
        archives = sorted(tmp_path.glob("*.merged-*"))
        text = ledger.fallback_path.read_text()
        assert f'"run_id": {run_id}' in text
        assert f'"run_id": {other}' not in text
        # A second reconcile leaves the kept line in place instead of re-archiving it.
        store.reconcile_fallback(now=DAY)
        assert ledger.spent_this_chain() == pytest.approx(0.3)
        assert sorted(tmp_path.glob("*.merged-*")) == archives


def test_reconcile_drops_old_legacy_lines_once_the_chain_is_complete(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        run_id = store.create_run("x", {}, DAY - timedelta(days=10))
        _finish(store, run_id, "complete")
        store.spend_fallback_path.write_text(json.dumps(_old_legacy(run_id, 300_000)) + "\n")
        store.reconcile_fallback(now=DAY)
        assert not store.spend_fallback_path.exists() or not store.spend_fallback_path.read_text()


# -- Round 2: immutable fallback only for CANTOPEN and READONLY_DIRECTORY ---------------


def _fail_mode_ro_with_code(monkeypatch: pytest.MonkeyPatch, code: int) -> list[str]:
    real = Storage._connect_uri
    seen: list[str] = []

    def connect(uri: str) -> sqlite3.Connection:
        seen.append(uri)
        if "mode=ro" in uri:
            exc = sqlite3.OperationalError("attempt to write a readonly database")
            exc.sqlite_errorcode = code  # type: ignore[attr-defined]
            raise exc
        return real(uri)

    monkeypatch.setattr(Storage, "_connect_uri", staticmethod(connect))
    return seen


def test_read_only_falls_back_on_readonly_directory_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "c.db"
    _make_db(db)
    seen = _fail_mode_ro_with_code(monkeypatch, storage_mod.SQLITE_READONLY_DIRECTORY)
    with Storage(db, read_only=True) as store:
        assert store.read_only
    assert any("immutable=1" in u for u in seen)


@pytest.mark.parametrize("code", [sqlite3.SQLITE_READONLY, 776, 264, 520])  # ROLLBACK, others.
def test_read_only_does_not_fall_back_on_other_readonly_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    db = tmp_path / "c.db"
    _make_db(db)
    seen = _fail_mode_ro_with_code(monkeypatch, code)
    with pytest.raises(sqlite3.OperationalError):
        Storage(db, read_only=True)
    assert not any("immutable=1" in u for u in seen)


def test_read_only_does_not_hide_a_hot_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "c.db"
    _make_db(db)
    (tmp_path / "c.db-journal").write_bytes(b"rollback pages")
    seen = _fail_mode_ro(monkeypatch, "unable to open database file")
    with pytest.raises(sqlite3.OperationalError):
        Storage(db, read_only=True)
    assert not any("immutable=1" in u for u in seen)


def test_read_only_falls_back_with_an_empty_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "c.db"
    _make_db(db)
    (tmp_path / "c.db-journal").write_bytes(b"")
    seen = _fail_mode_ro(monkeypatch, "unable to open database file")
    with Storage(db, read_only=True) as store:
        assert store.read_only
    assert any("immutable=1" in u for u in seen)


# -- Round 2: a failed window-end write is rolled back ---------------------------------


def test_partial_window_end_write_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def half_then_busy(self: Storage, run_id: int, ends: object) -> None:
        # Apply one row, uncommitted, then fail like a busy executemany would.
        self._conn.execute(
            "UPDATE run_sample SET window_end = '2026-10-11T00:00:00Z' WHERE run_id = ?",
            (run_id,),
        )
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Storage, "set_sample_window_ends", half_then_busy)
    with Storage(tmp_path / "c.db") as store:
        outcome = run_scan(make_source(), SEEDS, store, settings())
        stored = store._conn.execute(
            "SELECT COUNT(*) FROM run_sample WHERE run_id = ? AND window_end IS NOT NULL",
            (outcome.run_id,),
        ).fetchone()[0]
    assert outcome.status == "complete"
    assert stored == 0
    assert any("current cached window end" in w for w in outcome.warnings)


# -- Round 2: a .done archive's prune age counts from the merge --------------------------


def test_done_archive_mtime_is_the_merge_time(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        base = store.spend_fallback_path
        archive = base.with_name(base.name + ".merged-20260901T000000000000Z-cccccccc")
        archive.write_text(_record(db_id=store.db_id))
        rotated = (DAY - timedelta(days=40)).timestamp()
        os.utime(archive, (rotated, rotated))
        store.reconcile_fallback(now=DAY)
        done = archive.with_name(archive.name + ".done")
        assert done.exists()
        assert done.stat().st_mtime == pytest.approx(DAY.timestamp())
