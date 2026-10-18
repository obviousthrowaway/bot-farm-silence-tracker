from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from bot_farm_silence_tracker.models import Activity
from bot_farm_silence_tracker.storage import Storage

from .helpers import CUTOFF, NOW, at, make_account


def test_account_and_activity_roundtrip(tmp_path: Path) -> None:
    account = make_account("u1", created_at=at(-40))
    account.meta = {"community": "r/test", "n": 3}
    acts = [
        Activity("fake", "u1", at(-2), "post", "r/test", "abc"),
        Activity("fake", "u1", at(-1), "comment", None, None),
    ]
    with Storage(tmp_path / "c.db") as store:
        store.upsert_account(account, NOW)
        store.save_activities(account, acts, at(-30), NOW, NOW)
    with Storage(tmp_path / "c.db") as store:
        got = store.get_account("fake", "u1")
        assert got is not None
        assert got.created_at == account.created_at
        assert got.meta == account.meta
        assert store.get_activities("fake", "u1") == acts
        assert store.get_activities("fake", "u1", since=at(-1)) == acts[1:]
        assert store.get_account("fake", "nope") is None
        assert store.account_fetched_at("fake", "u1") == NOW


def test_save_activities_replaces_window(tmp_path: Path) -> None:
    account = make_account("u1")
    old = Activity("fake", "u1", at(-5), "post")
    with Storage(tmp_path / "c.db") as store:
        store.upsert_account(account, NOW)
        store.save_activities(account, [old], at(-30), NOW, NOW)
        store.save_activities(account, [], at(-30), NOW, NOW)
        assert store.get_activities("fake", "u1") == []


def test_cache_freshness(tmp_path: Path) -> None:
    account = make_account("u1")
    ttl = timedelta(hours=24)
    with Storage(tmp_path / "c.db") as store:
        assert not store.activity_cache_fresh("fake", "u1", at(-30), NOW, ttl)
        store.upsert_account(account, NOW)
        assert not store.activity_cache_fresh("fake", "u1", at(-30), NOW, ttl)
        store.save_activities(account, [], at(-30), NOW, NOW)
        assert store.activity_cache_fresh("fake", "u1", at(-30), NOW, ttl)
        assert store.activity_cache_fresh("fake", "u1", at(-20), NOW, ttl)  # Narrower is covered.
        assert not store.activity_cache_fresh("fake", "u1", at(-40), NOW, ttl)  # Wider is not.
        assert not store.activity_cache_fresh("fake", "u1", at(-30), NOW + timedelta(days=2), ttl)


def test_upsert_keeps_known_created_at(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        store.upsert_account(make_account("u1", created_at=at(-9)), NOW)
        store.upsert_account(make_account("u1", created_at=None), NOW)
        got = store.get_account("fake", "u1")
        assert got is not None and got.created_at == at(-9)


def test_runs(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as store:
        run_id = store.create_run("fake", {"n": 5}, CUTOFF)
        assert store.get_run(run_id)["status"] == "running"  # type: ignore[index]
        store.finish_run(
            run_id,
            status="complete",
            account_ids=["a", "b"],
            summary={"cliff": 1},
            finished_at=NOW,
        )
        run = store.get_run(run_id)
        assert run is not None
        assert run["params"] == {"n": 5}
        assert run["account_ids"] == ["a", "b"]
        assert run["summary"] == {"cliff": 1}
        assert run["status"] == "complete"
        assert store.get_run(999) is None


def test_opted_out_upsert_scrubs_every_trace_of_the_account(tmp_path: Path) -> None:
    """Storing an account as ``opted_out`` keeps only its ID: the accounts row loses the
    handle, creation time, meta and activity window, its cached activity is deleted,
    and every run's sample snapshot of it is scrubbed. Other accounts are untouched."""
    shy = make_account("shy", created_at=at(-200))
    shy.meta = {"community": "r/test", "profile_read": True}
    other = make_account("other", created_at=at(-100))
    other.meta = {"community": "r/test"}
    with Storage(tmp_path / "c.db") as store:
        for acct in (shy, other):
            store.upsert_account(acct, NOW)
            store.save_activities(acct, [Activity("fake", acct.user_id, at(-2), "post")],
                                  at(-30), NOW, NOW)  # fmt: skip
        first = store.create_run("fake", {}, NOW)
        store.save_run_sample(first, [shy, other])
        store.set_sample_status(first, shy, "fetched")
        second = store.create_run("fake", {}, NOW)
        store.save_run_sample(second, [shy, other])

        shy.status = "opted_out"
        store.upsert_account(shy, NOW)

        cached = store.get_account("fake", "shy")
        assert cached is not None
        assert cached.handle == "shy"
        assert cached.created_at is None
        assert cached.meta == {"opted_out": True}
        assert cached.status == "opted_out"
        assert store.get_activities("fake", "shy") == []
        assert store.activity_window("fake", "shy") is None
        for run_id in (first, second):
            snapshots = {a.user_id: a for a, _ in store.get_run_sample(run_id)}
            assert snapshots["shy"].handle == "shy"
            assert snapshots["shy"].created_at is None
            assert snapshots["shy"].meta == {"opted_out": True}
            # The other account keeps everything.
            assert snapshots["other"].handle == "handle_other"
            assert snapshots["other"].meta == {"community": "r/test"}
        raw = "\n".join(
            row[0] for row in store._conn.execute("SELECT account_json FROM run_sample")
        )
        assert "handle_shy" not in raw and "profile_read" not in raw
        kept = store.get_account("fake", "other")
        assert kept is not None and kept.handle == "handle_other"
        assert kept.created_at == other.created_at
        assert len(store.get_activities("fake", "other")) == 1


def test_opted_out_snapshot_written_for_a_run_is_scrubbed(tmp_path: Path) -> None:
    """``set_sample_status`` never writes an opted-out account's handle, creation time
    or meta into the run's snapshot."""
    shy = make_account("shy", created_at=at(-200))
    shy.meta = {"community": "r/test"}
    with Storage(tmp_path / "c.db") as store:
        run_id = store.create_run("fake", {}, NOW)
        store.save_run_sample(run_id, [shy])
        shy.status = "opted_out"
        store.set_sample_status(run_id, shy, "fetched")
        ((snapshot, status),) = store.get_run_sample(run_id)
    assert status == "fetched"
    assert snapshot.handle == "shy" and snapshot.created_at is None
    assert snapshot.meta == {"opted_out": True}
