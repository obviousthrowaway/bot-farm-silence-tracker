"""``report --last-seen``: the archive's newest activity of silent accounts.

Everything runs against the scripted fake archive of ``test_reddit_archive``; no
network, no real sleeping.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shlex
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot_farm_silence_tracker import cli
from bot_farm_silence_tracker import last_seen as ls
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.models import STATUS_ACTIVE, STATUS_OPTED_OUT, Account
from bot_farm_silence_tracker.scoring import AccountResult, ScoringParams
from bot_farm_silence_tracker.sources.errors import BudgetExceeded, RateLimited
from bot_farm_silence_tracker.sources.reddit_archive import USER_AGENT, SlowQueryError

from .helpers import CUTOFF, NOW
from .test_concurrency import cli_scan, patch_client
from .test_reddit_archive import (
    Archive,
    build_world,
    comment,
    make_source,
    post,
    pulse,
    runner,
    slow,
    ts,
)

SINCE = NOW  # The end of the scored window in the unit tests.
CHECKED = NOW + timedelta(days=30)
PARAMS = ScoringParams(cutoff=CUTOFF, now=NOW)


def account(handle: str = "alice") -> Account:
    return Account("reddit-archive", handle, handle)


def result(handle: str, *, cliff: bool = True, status: str = STATUS_ACTIVE) -> AccountResult:
    return AccountResult(
        platform="reddit-archive", user_id=handle, handle=handle, status=status, cliff=cliff
    )


# -- one account ------------------------------------------------------------------------


def test_activity_after_the_window_is_reported_with_its_kind() -> None:
    archive = Archive(
        [
            pulse(),
            comment("1", "alice", "a", SINCE + timedelta(days=3)),
            post("p", "alice", "a", SINCE + timedelta(days=8)),
            comment("0", "alice", "a", SINCE - timedelta(days=1)),  # In the window, not after.
        ]
    )
    src, _, _ = make_source(archive)
    answer = ls.check_account(src, account(), SINCE, CHECKED, CHECKED)
    assert answer.status == ls.ACTIVE_AFTER_WINDOW
    assert answer.newest == SINCE + timedelta(days=8) and answer.kind == "post"
    assert answer.columns() == {
        "newest_activity_utc": "2026-10-22T00:00:00+00:00",
        "newest_activity_kind": "post",
        "last_seen_status": "active_after_window",
    }
    # Two existence queries (the tool's own shape), one per kind, and nothing else.
    queries = archive.author_params()
    assert len(queries) == 2 == len(archive.requests)
    for q in queries:
        assert q["sort"] == "desc" and q["limit"] == "1" and q["fields"] == "created_utc"
        assert int(q["after"]) == ts(SINCE) - 1 and int(q["before"]) == ts(CHECKED)
    assert {r.url.path.rsplit("/", 2)[-2] for r in archive.requests} == {"comments", "posts"}
    assert all(r.headers["user-agent"] == USER_AGENT for r in archive.requests)


def test_the_newer_kind_wins_and_a_tie_is_a_comment() -> None:
    when = SINCE + timedelta(days=2)
    archive = Archive([pulse(), comment("1", "alice", "a", when), post("p", "alice", "a", when)])
    src, _, _ = make_source(archive)
    assert ls.check_account(src, account(), SINCE, CHECKED, CHECKED).kind == "comment"


def test_no_activity_found() -> None:
    archive = Archive([pulse(), comment("1", "alice", "a", SINCE - timedelta(days=1))])
    src, _, _ = make_source(archive)
    answer = ls.check_account(src, account(), SINCE, CHECKED, CHECKED)
    assert answer.status == ls.NO_ACTIVITY_FOUND
    assert answer.newest is None and answer.kind is None
    assert answer.columns()["newest_activity_utc"] is None
    assert len(archive.author_params()) == 2


def test_a_slow_archive_is_inconclusive_never_no_activity() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if "author" in q else None

    src, _, _ = make_source(Archive([pulse()], hook=hook), slowdown_wait=0)
    with pytest.raises(SlowQueryError):  # The adapter does fail; the check absorbs it.
        src.newest_activity(account(), "comment", SINCE, CHECKED)
    answer = ls.check_account(src, account(), SINCE, CHECKED, CHECKED)
    assert answer.status == ls.INCONCLUSIVE and answer.newest is None


def test_one_failed_kind_is_inconclusive_unless_the_other_found_activity() -> None:
    def comments_slow(archive: Archive) -> Any:
        def hook(q: dict[str, str]) -> httpx.Response | None:
            path = archive.requests[-1].url.path
            return slow() if "author" in q and path.endswith("/comments/search") else None

        return hook

    archive = Archive([pulse()])
    archive.hook = comments_slow(archive)
    src, _, _ = make_source(archive, slowdown_wait=0)
    assert ls.check_account(src, account(), SINCE, CHECKED, CHECKED).status == ls.INCONCLUSIVE

    archive = Archive([pulse(), post("p", "alice", "a", SINCE + timedelta(days=1))])
    archive.hook = comments_slow(archive)
    src, _, _ = make_source(archive, slowdown_wait=0)
    answer = ls.check_account(src, account(), SINCE, CHECKED, CHECKED)
    assert answer.status == ls.ACTIVE_AFTER_WINDOW and answer.kind == "post"


def test_a_query_narrowed_to_a_later_start_cannot_say_no_activity() -> None:
    def only_from_the_cutoff(q: dict[str, str]) -> httpx.Response | None:
        return slow() if "author" in q and int(q["after"]) < ts(CUTOFF) - 1 else None

    # Search from before the cutoff, as the pre-check does, so the adapter narrows.
    archive = Archive([pulse()], hook=only_from_the_cutoff)
    src, _, _ = make_source(archive, slowdown_wait=0)
    early = CUTOFF - timedelta(days=1)
    assert ls.check_account(src, account(), early, CHECKED, CHECKED).status == ls.INCONCLUSIVE


def test_the_adapter_backs_off_and_paces_the_queries_as_in_a_scan() -> None:
    archive = Archive(
        [pulse(), comment("1", "alice", "a", SINCE + timedelta(days=1))],
        script=[slow()],  # The first query times out; the ladder waits and retries.
    )
    src, adapter_sleeps, pacing = make_source(archive, slowdown_wait=7)
    answer = ls.check_account(src, account(), SINCE, CHECKED, CHECKED)
    assert answer.status == ls.ACTIVE_AFTER_WINDOW
    assert len(archive.requests) == 3  # Timed out, retried, then the post query.
    assert adapter_sleeps == [7.0]  # The slow-query ladder's own wait.
    assert pacing == [2.0, 2.0]  # The shared pacer spaces requests at the archive's 0.5 per second.
    assert src.http.rate[0] == 0.5  # The archive's polite default, not a faster one.


# -- many accounts ----------------------------------------------------------------------


def test_the_scopes_pick_silent_or_all_observed_accounts() -> None:
    results = [
        result("b_silent"),
        result("a_silent"),
        result("c_back", cliff=False),
        result("d_optout", status=STATUS_OPTED_OUT),
        result("e_error", status="error"),
    ]
    assert [r.user_id for r in ls.select_targets(results, ls.SCOPE_SILENT)] == [
        "a_silent",
        "b_silent",
    ]
    assert [r.user_id for r in ls.select_targets(results, ls.SCOPE_ALL)] == [
        "a_silent",
        "b_silent",
        "c_back",
    ]
    with pytest.raises(ValueError, match="--last-seen-scope"):
        ls.validate_scope("everyone")


def test_a_used_up_budget_marks_the_rest_inconclusive_and_keeps_the_answers(
    tmp_path: Path,
) -> None:
    archive = Archive([pulse(), comment("1", "a1", "a", SINCE + timedelta(days=1))])
    src, _, _ = make_source(archive, max_requests=2)
    cache = ls.AnswerCache(tmp_path / "answers.jsonl", 1)
    report = ls.run_check(
        src,
        PARAMS,
        [result("a1"), result("a2"), result("a3")],
        scope=ls.SCOPE_SILENT,
        until=CHECKED,
        cache=cache,
        requests_made=lambda: src.http.requests_made,
    )
    assert report.answers["a1"].status == ls.ACTIVE_AFTER_WINDOW
    assert report.answers["a2"].status == report.answers["a3"].status == ls.INCONCLUSIVE
    assert report.stopped is not None and BudgetExceeded.__name__ in report.stopped
    assert report.requests == len(archive.requests) == 2
    assert set(cache.load(CHECKED)) == {"a1"}  # Inconclusive answers are never saved.


def test_the_overload_guard_stops_the_check_instead_of_reporting_silence() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if "author" in q else None

    src, _, _ = make_source(Archive([pulse()], hook=hook), slowdown_wait=0)
    report = ls.run_check(
        src,
        PARAMS,
        [result(f"u{i}") for i in range(6)],
        scope=ls.SCOPE_SILENT,
        until=CHECKED,
    )
    assert {a.status for a in report.answers.values()} == {ls.INCONCLUSIVE}
    assert report.stopped is not None and "RateLimited" in report.stopped
    assert ls.NO_ACTIVITY_FOUND not in report.counts() or report.counts()[ls.NO_ACTIVITY_FOUND] == 0


def test_a_rerun_uses_the_saved_answers_and_asks_nothing_again(tmp_path: Path) -> None:
    cache = ls.AnswerCache(tmp_path / "answers.jsonl", 1)
    targets = [result("a1"), result("a2")]

    def check(archive: Archive, when: datetime = CHECKED, run_id: int = 1) -> ls.LastSeenReport:
        src, _, _ = make_source(archive)
        return ls.run_check(
            src,
            PARAMS,
            targets,
            scope=ls.SCOPE_SILENT,
            until=when,
            cache=ls.AnswerCache(cache.path, run_id),
        )

    first = Archive([pulse(), comment("1", "a1", "a", SINCE + timedelta(days=1))])
    one = check(first)
    assert len(first.requests) == 4 and one.reused == 0

    second = Archive([pulse()])
    two = check(second, CHECKED + timedelta(hours=2))
    assert not second.requests and two.reused == 2
    assert {k: v.status for k, v in two.answers.items()} == {
        "a1": ls.ACTIVE_AFTER_WINDOW,
        "a2": ls.NO_ACTIVITY_FOUND,
    }

    stale = Archive([pulse()])  # Older than a day: ask again.
    check(stale, CHECKED + timedelta(days=2))
    assert len(stale.requests) == 4
    other_run = Archive([pulse()])  # Another run's answers do not apply.
    check(other_run, run_id=2)
    assert len(other_run.requests) == 4


def test_a_damaged_cache_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "answers.jsonl"
    good = ls.LastSeen("a1", ls.NO_ACTIVITY_FOUND, None, None, SINCE, CHECKED).to_json(1)
    path.write_text("not json\n" + json.dumps(good) + '\n{"run_id": 1, "status": "x"}\n{"run_')
    assert set(ls.AnswerCache(path, 1).load(CHECKED)) == {"a1"}
    assert ls.AnswerCache(tmp_path / "missing.jsonl", 1).load(CHECKED) == {}


# -- the report command -----------------------------------------------------------------


def later_world() -> list[dict[str, Any]]:
    """The CLI world, plus what the silent accounts did after the scan's window."""
    return [
        *build_world(),
        comment("late-0", "bot_acct_0", "politics", NOW + timedelta(days=5)),
        post("late-1", "bot_acct_1", "politics", NOW + timedelta(days=9)),
        comment("late-2", "bot_acct_2", "politics", NOW + timedelta(days=2)),
        post("late-2p", "bot_acct_2", "politics", NOW + timedelta(days=3)),
        # A human who is not silent, with an ordinary later comment.
        comment("late-h", "human_acct_0", "politics", NOW + timedelta(days=4)),
    ]


@pytest.fixture
def scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Archive, Path, dict[str, Any]]:
    archive = Archive(later_world())
    patch_client(monkeypatch, archive)
    monkeypatch.setattr("bot_farm_silence_tracker.cli.utcnow", lambda: CHECKED)
    baseline = cli_scan(tmp_path, "one")
    return archive, tmp_path / "one.db", baseline


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report(db: Path, out: Path, *extra: str) -> Any:
    return runner.invoke(
        app, ["report", "--run-id", "1", "--db", str(db), "--out", str(out), *extra]
    )


def test_report_without_the_flag_sends_no_request_and_adds_no_column(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, db, _ = scanned
    before = len(archive.requests)
    out = tmp_path / "plain.csv"
    result_ = report(db, out)
    assert result_.exit_code == 0, result_.output
    assert len(archive.requests) == before
    text = out.read_text()
    assert "last_seen" not in text and "newest_activity" not in text
    header = next(csv.reader(line for line in text.splitlines() if not line.startswith("#")))
    assert header[-1] != "scored_silent" and not set(ls.COLUMNS) & set(header)
    payload = json.loads(baseline_report(db, tmp_path))
    assert "last_seen" not in payload and "last_seen_notice" not in payload


def baseline_report(db: Path, tmp_path: Path) -> str:
    out = tmp_path / "plain.json"
    assert report(db, out).exit_code == 0
    return out.read_text()


def test_last_seen_adds_the_columns_to_the_csv(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, db, _ = scanned
    before = len(archive.requests)
    out = tmp_path / "seen.csv"
    result_ = report(db, out, "--last-seen")
    assert result_.exit_code == 0, result_.output
    assert len(archive.requests) - before == 12  # 6 silent accounts, 2 requests each.
    text = out.read_text()
    assert "not a closure verdict" in text
    rows = list(csv.DictReader(line for line in text.splitlines() if not line.startswith("#")))
    assert [r["user_id"] for r in rows] and len(rows) == 6
    by_id = {r["user_id"]: r for r in rows}
    assert by_id["bot_acct_0"]["last_seen_status"] == "active_after_window"
    assert by_id["bot_acct_0"]["newest_activity_kind"] == "comment"
    assert by_id["bot_acct_0"]["newest_activity_utc"] == "2026-10-19T00:00:00+00:00"
    assert by_id["bot_acct_1"]["newest_activity_kind"] == "post"
    assert by_id["bot_acct_2"]["newest_activity_kind"] == "post"  # The later of two kinds.
    for name in ("bot_acct_3", "bot_acct_4", "bot_acct_5"):
        assert by_id[name]["last_seen_status"] == "no_activity_found"
        assert by_id[name]["newest_activity_utc"] == ""
    assert {r["scored_silent"] for r in rows} == {"True"}
    header = next(csv.reader(line for line in text.splitlines() if not line.startswith("#")))
    assert header[-4:] == ls.COLUMNS  # After every other column.
    # The two requests of an account use the tool's own politeness and identity.
    assert all(r.headers["user-agent"] == USER_AGENT for r in archive.requests)


def test_last_seen_json_has_the_notice_and_the_counts_but_no_names(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, db, _ = scanned
    plain = json.loads(baseline_report(db, tmp_path))
    out = tmp_path / "seen.json"
    assert report(db, out, "--last-seen").exit_code == 0
    payload = json.loads(out.read_text())
    assert "not a closure verdict" in payload["last_seen_notice"]
    meta = payload["last_seen"]
    assert meta["scope"] == "silent" and meta["accounts_checked"] == 6
    assert meta["checked_until"] == "2026-11-13T00:00:00+00:00"
    assert meta["active_after_window"] == 3 and meta["no_activity_found"] == 3
    assert meta["inconclusive"] == 0 and meta["requests"] == 12 and meta["stopped"] is None
    assert "bot_acct" not in json.dumps(meta)
    # Apart from the new keys, the report is the one without the flag.
    stripped = [{k: v for k, v in c.items() if k not in ls.COLUMNS} for c in payload["candidates"]]
    assert stripped == [{k: v for k, v in c.items()} for c in plain["candidates"]]
    assert payload["summary"] == plain["summary"]


def test_scope_all_keeps_the_other_accounts_out_of_the_candidates(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, db, _ = scanned
    plain = json.loads(baseline_report(db, tmp_path))
    before = len(archive.requests)
    out = tmp_path / "all.json"
    assert report(db, out, "--last-seen", "--last-seen-scope", "all").exit_code == 0
    assert len(archive.requests) - before == 24  # 12 observed accounts.
    payload = json.loads(out.read_text())
    rows = payload["candidates"]
    assert [r["user_id"] for r in rows] == [c["user_id"] for c in plain["candidates"]]
    assert [r["scored_silent"] for r in rows] == [True] * 6
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5, 6]
    others = payload["last_seen_other_accounts"]
    assert [r["scored_silent"] for r in others] == [False] * 6
    assert {r["rank"] for r in others} == {None}
    human = next(r for r in others if r["user_id"] == "human_acct_0")
    assert human["last_seen_status"] == "active_after_window"
    # Without the flag, and with the default scope, the key is absent or empty.
    only_silent = tmp_path / "silent.json"
    assert report(db, only_silent, "--last-seen").exit_code == 0
    assert json.loads(only_silent.read_text())["last_seen_other_accounts"] == []
    assert "last_seen_other_accounts" not in plain


def test_scope_all_csv_says_the_other_rows_are_not_candidates(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, db, _ = scanned
    out = tmp_path / "all.csv"
    assert report(db, out, "--last-seen", "--last-seen-scope", "all").exit_code == 0
    text = out.read_text()
    assert "scored_silent False" in text and "not candidates" in text
    rows = list(csv.DictReader(line for line in text.splitlines() if not line.startswith("#")))
    assert [r["scored_silent"] for r in rows] == ["True"] * 6 + ["False"] * 6
    quiet = tmp_path / "silent.csv"
    assert report(db, quiet, "--last-seen").exit_code == 0
    assert "scored_silent False" not in quiet.read_text()


def test_an_archive_that_times_out_gives_inconclusive_rows(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, db, _ = scanned
    archive.hook = lambda q: slow() if "author" in q and int(q["after"]) >= ts(NOW) - 3700 else None
    out = tmp_path / "slow.csv"
    result_ = report(db, out, "--last-seen")
    assert result_.exit_code == 0, result_.output
    rows = list(csv.DictReader(line for line in out.read_text().splitlines() if line[0] != "#"))
    assert len(rows) == 6
    assert {r["last_seen_status"] for r in rows} == {"inconclusive"}
    assert {r["newest_activity_utc"] for r in rows} == {""}
    assert "no_activity_found" not in "".join(r["last_seen_status"] for r in rows)


def snapshot(folder: Path) -> dict[str, tuple[str, int]]:
    """Every file in ``folder`` with its content hash and modification time."""
    return {
        p.name: (digest(p), p.stat().st_mtime_ns) for p in sorted(folder.iterdir()) if p.is_file()
    }


def test_the_database_is_not_written_even_when_it_cannot_be_written(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, db, _ = scanned
    work = tmp_path / "work"
    work.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for p in list(tmp_path.glob("one.db*")) + list(tmp_path.glob("spend.db*")):
        (work / p.name).write_bytes(p.read_bytes())
    copy_db = work / "one.db"
    before = snapshot(work)
    assert report(copy_db, out_dir / "a.csv", "--last-seen").exit_code == 0
    after = snapshot(work)

    # The database and every other file are unchanged and spend.db was not created or
    # synced. SQLite itself may create an empty -wal and a -shm when it reads a database in
    # WAL mode, and it updates a -shm that exists; those hold no data of the run.
    def stable(files: dict[str, tuple[str, int]]) -> dict[str, tuple[str, int]]:
        return {k: v for k, v in files.items() if not k.endswith("-shm")}

    assert {k: v for k, v in stable(after).items() if k in before} == stable(before)
    assert set(after) - set(before) <= {"one.db-wal", "one.db-shm"}
    assert "spend.db" not in after
    if "one.db-wal" not in before and "one.db-wal" in after:
        assert Path(f"{copy_db}-wal").stat().st_size == 0
    # A read-only copy in a read-only folder still works, so nothing needs a write.
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    copy = ro_dir / "run.db"
    copy.write_bytes(db.read_bytes())
    cache = tmp_path / "answers.jsonl"
    original = digest(db)
    copy.chmod(stat.S_IRUSR)
    ro_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(ro_dir, os.W_OK):  # Running as a user the mode does not bind.
            pytest.skip("the file system does not enforce read-only modes here")
        out = tmp_path / "ro.csv"
        result_ = report(copy, out, "--last-seen", "--last-seen-cache", str(cache))
        assert result_.exit_code == 0, result_.output
        assert sorted(p.name for p in ro_dir.iterdir()) == ["run.db"]
    finally:
        ro_dir.chmod(stat.S_IRWXU)
        copy.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert digest(copy) == original and cache.exists()


def test_a_live_write_ahead_log_database_is_left_untouched(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    import sqlite3

    _, db, _ = scanned
    work = tmp_path / "live"
    work.mkdir()
    live = work / "live.db"
    live.write_bytes(db.read_bytes())
    holder = sqlite3.connect(live)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("CREATE TABLE IF NOT EXISTS scratch (x)")
        holder.execute("INSERT INTO scratch VALUES (1)")
        holder.commit()  # Leaves a -wal file that the holder keeps open.
        assert Path(f"{live}-wal").exists()
        before = snapshot(work)
        assert report(live, tmp_path / "live.csv", "--last-seen").exit_code == 0
        after = snapshot(work)
    finally:
        holder.close()
    # The -shm file is SQLite's shared-memory index, which every reader of a live WAL
    # database updates; it is the one file allowed to change. The database, the log and
    # the folder's file list stay as they were, and no spend.db appears.
    assert set(after) == set(before) == {"live.db", "live.db-wal", "live.db-shm"}
    for name in ("live.db", "live.db-wal"):
        assert after[name] == before[name]


def test_the_default_cache_sits_next_to_the_output_and_a_rerun_reuses_it(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, db, _ = scanned
    out = tmp_path / "seen.csv"
    assert report(db, out, "--last-seen").exit_code == 0
    cache = tmp_path / "seen.csv.last-seen.jsonl"
    assert len(cache.read_text().splitlines()) == 6
    sent = len(archive.requests)
    again = report(db, out, "--last-seen")
    assert again.exit_code == 0 and len(archive.requests) == sent
    assert "6 answers reused" in again.output


def test_the_options_are_checked(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, db, _ = scanned
    before = len(archive.requests)
    out = tmp_path / "x.csv"
    for extra, message in (
        (("--last-seen-scope", "all"), "need --last-seen"),
        (("--last-seen-cache", str(tmp_path / "c")), "need --last-seen"),
        (("--last-seen", "--last-seen-scope", "some"), "--last-seen-scope must be"),
    ):
        result_ = report(db, out, *extra)
        assert result_.exit_code == 2 and message in result_.output
    both = runner.invoke(app, ["report", "--registry", str(tmp_path), "--last-seen"])
    assert both.exit_code == 2 and "--run-id" in both.output
    assert len(archive.requests) == before and not out.exists()


def test_other_platforms_are_refused_before_any_request(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    scan = runner.invoke(
        app, ["scan", "--platform", "fake", "--n", "30", "--seed", "1", "--db", str(db)]
    )
    assert scan.exit_code == 0, scan.output
    result_ = report(db, tmp_path / "f.csv", "--last-seen")
    assert result_.exit_code == 2 and "reddit-archive" in result_.output
    assert not (tmp_path / "f.csv").exists()


# -- findings of the review of the first version ----------------------------------------


def run(
    src: Any,
    targets: list[AccountResult],
    cache: ls.AnswerCache | None = None,
    params: ScoringParams = PARAMS,
) -> ls.LastSeenReport:
    return ls.run_check(
        src,
        params,
        targets,
        scope=ls.SCOPE_SILENT,
        until=CHECKED,
        cache=cache,
        requests_made=lambda: src.http.requests_made,
    )


def test_activity_found_before_a_budget_stop_is_kept(tmp_path: Path) -> None:
    archive = Archive([pulse(), comment("1", "a1", "a", SINCE + timedelta(days=1))])
    src, _, _ = make_source(archive, max_requests=1)  # Only the comment query fits.
    with pytest.raises(ls.StoppedAfterAnswer) as stopped:
        ls.check_account(src, account("a1"), SINCE, CHECKED, CHECKED)
    assert stopped.value.answer.status == ls.ACTIVE_AFTER_WINDOW
    assert isinstance(stopped.value.error, BudgetExceeded)

    src, _, _ = make_source(archive, max_requests=1)
    cache = ls.AnswerCache(tmp_path / "answers.jsonl", 1)
    report_ = run(src, [result("a1"), result("a2")], cache)
    assert report_.answers["a1"].status == ls.ACTIVE_AFTER_WINDOW
    assert report_.answers["a1"].kind == "comment"
    assert report_.answers["a2"].status == ls.INCONCLUSIVE
    assert report_.stopped is not None and "BudgetExceeded" in report_.stopped
    assert set(cache.load(CHECKED)) == {"a1"}


def test_activity_found_before_the_overload_guard_trips_is_kept() -> None:
    archive = Archive([pulse(), comment("1", "a1", "a", SINCE + timedelta(days=1))])
    src, _, _ = make_source(archive)

    def guard(account_: Account, errors: Any) -> None:
        raise RateLimited(None, "the archive is overloaded")

    src.precheck_finished = guard  # type: ignore[method-assign]
    report_ = run(src, [result("a1"), result("a2")])
    assert report_.answers["a1"].status == ls.ACTIVE_AFTER_WINDOW
    assert report_.answers["a2"].status == ls.INCONCLUSIVE
    assert report_.stopped is not None and "RateLimited" in report_.stopped
    assert len(archive.requests) == 2  # Nothing was asked after the stop.

    quiet = Archive([pulse()])  # Without activity, the same error leaves the answer unknown.
    src, _, _ = make_source(quiet)
    src.precheck_finished = guard  # type: ignore[method-assign]
    report_ = run(src, [result("a1")])
    assert report_.answers["a1"].status == ls.INCONCLUSIVE


def server_error(q: dict[str, str]) -> httpx.Response | None:
    return httpx.Response(500) if "author" in q else None


def test_a_check_continuing_after_a_fatal_stop_would_send_more_requests() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if "author" in q else None

    sent = []
    for count in (4, 12):
        archive = Archive([pulse()], hook=hook)
        src, _, _ = make_source(archive, slowdown_wait=0)
        report_ = run(src, [result(f"u{i}") for i in range(count)])
        assert report_.stopped is not None and "RateLimited" in report_.stopped
        sent.append(len(archive.requests))
    assert sent[0] == sent[1]  # The accounts after the stop cost nothing.


def test_failing_every_query_for_three_accounts_in_a_row_stops_the_check() -> None:
    sent = []
    for count in (3, 12):
        archive = Archive([pulse()], hook=server_error)
        src, _, _ = make_source(archive)
        report_ = run(src, [result(f"u{i:02}") for i in range(count)])
        assert {a.status for a in report_.answers.values()} == {ls.INCONCLUSIVE}
        assert report_.stopped is not None and "3 accounts in a row" in report_.stopped
        sent.append(len(archive.requests))
    assert sent[0] == sent[1] > 0


def one_kind_down(archive: Archive, path_end: str) -> Any:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if "author" in q and archive.requests[-1].url.path.endswith(path_end):
            return httpx.Response(500)
        return None

    return hook


@pytest.mark.parametrize("down", ["/comments/search", "/posts/search"])
def test_one_kind_of_query_failing_for_three_accounts_in_a_row_stops_the_check(down: str) -> None:
    sent = []
    for count in (3, 12):
        archive = Archive([pulse()])
        archive.hook = one_kind_down(archive, down)
        src, _, _ = make_source(archive)
        report_ = run(src, [result(f"u{i:02}") for i in range(count)])
        assert {a.status for a in report_.answers.values()} == {ls.INCONCLUSIVE}
        assert report_.stopped is not None and "3 accounts in a row" in report_.stopped
        sent.append(len(archive.requests))
    assert sent[0] == sent[1] > 0


def test_an_account_that_answered_everything_resets_the_streak() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return None if q.get("author", "").startswith("ok") else server_error(q)

    archive = Archive([pulse()], hook=hook)
    src, _, _ = make_source(archive)
    handles = ["f1", "f2", "ok1", "f3", "f4", "ok2"]
    report_ = run(src, [result(h) for h in handles])
    assert report_.stopped is None
    assert report_.answers["ok1"].status == ls.NO_ACTIVITY_FOUND
    assert report_.answers["f4"].status == ls.INCONCLUSIVE


def test_found_activity_does_not_count_as_a_failed_account() -> None:
    items = [pulse()] + [
        post(f"p{i}", f"u{i:02}", "a", SINCE + timedelta(days=1)) for i in range(8)
    ]
    archive = Archive(items)
    archive.hook = one_kind_down(archive, "/comments/search")
    src, _, _ = make_source(archive)
    report_ = run(src, [result(f"u{i:02}") for i in range(8)])
    assert report_.stopped is None
    assert {a.status for a in report_.answers.values()} == {ls.ACTIVE_AFTER_WINDOW}


@pytest.mark.parametrize(
    "body", [{"data": [{"created_utc": "x"}]}, {"data": [None]}, {"data": [{}]}]
)
def test_a_returned_row_that_cannot_be_read_is_inconclusive(body: dict[str, Any]) -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return httpx.Response(200, json=body) if "author" in q else None

    src, _, _ = make_source(Archive([pulse()], hook=hook))
    found = src.newest_activity(account(), "comment", SINCE, CHECKED)
    assert found.newest is None and found.unreadable  # The adapter says so ...
    answer = ls.check_account(src, account(), SINCE, CHECKED, CHECKED)
    assert answer.status == ls.INCONCLUSIVE  # ... and the check never calls it silence.


def test_an_empty_answer_is_still_no_activity_and_the_pre_check_is_unchanged() -> None:
    src, _, _ = make_source(Archive([pulse()]))
    found = src.newest_activity(account(), "comment", SINCE, CHECKED)
    assert found.newest is None and not found.unreadable
    assert ls.check_account(src, account(), SINCE, CHECKED, CHECKED).status == (
        ls.NO_ACTIVITY_FOUND
    )


def test_the_search_starts_where_the_accounts_data_ended() -> None:
    data_end = SINCE - timedelta(days=2)
    early = comment("1", "a1", "a", SINCE - timedelta(days=1))  # After data_end, before NOW.
    archive = Archive([pulse(), early])
    src, _, _ = make_source(archive)
    ended = AccountResult(
        platform="reddit-archive",
        user_id="a1",
        handle="a1",
        status=STATUS_ACTIVE,
        cliff=True,
        data_end=data_end,
    )
    report_ = run(src, [ended])
    assert report_.answers["a1"].since == data_end
    assert report_.answers["a1"].status == ls.ACTIVE_AFTER_WINDOW
    assert {int(q["after"]) for q in archive.author_params()} == {ts(data_end) - 1}
    assert ls.window_end(ended, PARAMS) == data_end
    assert ls.window_end(result("a2"), PARAMS) == PARAMS.effective_now


def test_a_saved_answer_for_another_start_is_not_reused(tmp_path: Path) -> None:
    cache = ls.AnswerCache(tmp_path / "answers.jsonl", 1)
    cache.add(
        ls.LastSeen("a1", ls.NO_ACTIVITY_FOUND, None, None, SINCE - timedelta(days=1), CHECKED)
    )
    archive = Archive([pulse(), comment("1", "a1", "a", SINCE + timedelta(days=1))])
    src, _, _ = make_source(archive)
    report_ = run(src, [result("a1")], cache)
    assert report_.reused == 0 and len(archive.requests) == 2
    assert report_.answers["a1"].status == ls.ACTIVE_AFTER_WINDOW


def test_an_inconclusive_saved_answer_is_never_reused(tmp_path: Path) -> None:
    path = tmp_path / "answers.jsonl"
    cache = ls.AnswerCache(path, 1)
    path.write_text(
        json.dumps(ls.LastSeen("a1", ls.INCONCLUSIVE, None, None, SINCE, CHECKED).to_json(1)) + "\n"
    )
    assert cache.load(CHECKED) == {}
    archive = Archive([pulse()])
    src, _, _ = make_source(archive)
    report_ = run(src, [result("a1")], cache)
    assert report_.reused == 0 and len(archive.requests) == 2
    assert report_.answers["a1"].status == ls.NO_ACTIVITY_FOUND


def test_saved_answers_are_bound_to_the_database_and_not_to_the_future(tmp_path: Path) -> None:
    path = tmp_path / "answers.jsonl"
    one = ls.AnswerCache(path, 1, "db-one")
    one.add(ls.LastSeen("a1", ls.NO_ACTIVITY_FOUND, None, None, SINCE, CHECKED))
    assert set(one.load(CHECKED)) == {"a1"}
    assert ls.AnswerCache(path, 1, "db-two").load(CHECKED) == {}  # Another event's run 1.
    assert ls.AnswerCache(path, 1).load(CHECKED) == {}
    assert one.load(CHECKED - timedelta(hours=1)) == {}  # Claims to be from the future.


def test_the_cache_path_is_checked_before_any_request(tmp_path: Path) -> None:
    folder = tmp_path / "is_a_folder"
    folder.mkdir()
    with pytest.raises(OSError):
        ls.AnswerCache(folder, 1).ensure_writable()
    ls.AnswerCache(tmp_path / "new" / "answers.jsonl", 1).ensure_writable()
    assert (tmp_path / "new" / "answers.jsonl").exists()


def capture_clients(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the keyword arguments of every HttpClient the CLI builds."""
    seen: list[dict[str, Any]] = []
    build = cli.HttpClient

    def recording(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return build(**kwargs)

    monkeypatch.setattr(cli, "HttpClient", recording)
    return seen


def test_the_cli_budget_is_pooled_and_a_small_one_stops_the_check(
    scanned: tuple[Archive, Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, db, _ = scanned
    assert cli.LAST_SEEN_REQUESTS_PER_ACCOUNT == 6
    seen = capture_clients(monkeypatch)
    before = len(archive.requests)
    out = tmp_path / "budget.json"
    assert report(db, out, "--last-seen").exit_code == 0
    assert seen[-1]["max_requests"] == 6 * 6 + 10
    assert len(archive.requests) - before == 12
    # With a tiny per-account allowance only the pooled extra remains.
    monkeypatch.setattr(cli, "LAST_SEEN_REQUESTS_PER_ACCOUNT", 0)
    before = len(archive.requests)
    out = tmp_path / "tiny.json"
    result_ = report(db, out, "--last-seen", "--last-seen-cache", str(tmp_path / "tiny.cache"))
    assert result_.exit_code == 0, result_.output
    meta = json.loads(out.read_text())["last_seen"]
    assert seen[-1]["max_requests"] == 10
    assert len(archive.requests) - before == 10 == meta["requests"]
    assert meta["stopped"] is not None and "BudgetExceeded" in meta["stopped"]
    assert meta["inconclusive"] >= 1
    assert "stopped early" in result_.output


def test_the_budget_counts_only_the_accounts_that_need_asking(
    scanned: tuple[Archive, Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, db, _ = scanned
    out = tmp_path / "seen.csv"
    assert report(db, out, "--last-seen").exit_code == 0
    seen = capture_clients(monkeypatch)
    again = report(db, out, "--last-seen")
    assert again.exit_code == 0 and "asking the archive about 0 of 6" in again.output
    assert seen[-1]["max_requests"] == 10


def test_the_csv_comment_line_quotes_a_stop_reason_with_spaces(
    scanned: tuple[Archive, Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, db, _ = scanned
    monkeypatch.setattr(cli, "LAST_SEEN_REQUESTS_PER_ACCOUNT", 0)
    out = tmp_path / "stop.csv"
    assert report(db, out, "--last-seen").exit_code == 0
    line = next(x for x in out.read_text().splitlines() if x.startswith("# last_seen "))
    fields = dict(token.split("=", 1) for token in shlex.split(line[2:])[1:])
    assert fields["stopped"].startswith("BudgetExceeded: ") and fields["requests"] == "10"
    assert fields["scope"] == "silent"


def test_trust_env_is_honoured_like_in_a_scan(
    scanned: tuple[Archive, Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, db, _ = scanned
    seen = capture_clients(monkeypatch)
    plain = report(db, tmp_path / "a.csv", "--last-seen")
    assert plain.exit_code == 0 and seen[-1]["trust_env"] is False
    assert "--trust-env is on" not in plain.output
    flagged = report(db, tmp_path / "b.csv", "--last-seen", "--trust-env")
    assert flagged.exit_code == 0 and seen[-1]["trust_env"] is True
    assert "warning: --trust-env is on" in flagged.output
    monkeypatch.setenv("BOT_FARM_SILENCE_TRACKER_TRUST_ENV", "1")
    from_env = report(db, tmp_path / "c.csv", "--last-seen")
    assert from_env.exit_code == 0 and seen[-1]["trust_env"] is True
    assert "warning: --trust-env is on" in from_env.output


def test_a_stored_faster_rate_does_not_speed_up_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(later_world())
    clients = patch_client(monkeypatch, archive)
    monkeypatch.setattr("bot_farm_silence_tracker.cli.utcnow", lambda: CHECKED)
    cli_scan(tmp_path, "fast", "--source-option", "max_rps=2")
    scan_rate = clients[-1].rate[0]
    out = tmp_path / "fast.csv"
    assert report(tmp_path / "fast.db", out, "--last-seen").exit_code == 0
    assert scan_rate == 2.0  # The stored option did speed up the scan itself.
    assert clients[-1].rate[0] == 0.5  # The check stays at the archive's default.


def test_an_unwritable_cache_path_stops_the_command_before_any_request(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, db, _ = scanned
    before = len(archive.requests)
    folder = tmp_path / "a_folder"
    folder.mkdir()
    result_ = report(db, tmp_path / "x.csv", "--last-seen", "--last-seen-cache", str(folder))
    assert result_.exit_code == 2 and "--last-seen-cache" in result_.output
    assert len(archive.requests) == before and not (tmp_path / "x.csv").exists()


def test_last_seen_opens_the_database_read_only_and_plain_report_does_not(
    scanned: tuple[Archive, Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, db, _ = scanned
    opened: list[Any] = []
    storage_class = cli.Storage

    def recording(*args: Any, **kwargs: Any) -> Any:
        storage = storage_class(*args, **kwargs)
        opened.append(storage)
        return storage

    monkeypatch.setattr(cli, "Storage", recording)
    assert report(db, tmp_path / "seen.csv", "--last-seen").exit_code == 0
    assert opened and all(s.read_only for s in opened)  # No pragma, schema, migration or sync.
    opened.clear()
    assert report(db, tmp_path / "plain.csv").exit_code == 0
    assert opened and not any(s.read_only for s in opened)  # Without the flag nothing changes.


def test_two_databases_sharing_a_cache_file_do_not_share_answers(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, first, _ = scanned
    cli_scan(tmp_path, "two")
    second = tmp_path / "two.db"
    cache = tmp_path / "shared.jsonl"
    assert (
        report(first, tmp_path / "a.csv", "--last-seen", "--last-seen-cache", str(cache)).exit_code
        == 0
    )
    sent = len(archive.requests)
    again = report(second, tmp_path / "b.csv", "--last-seen", "--last-seen-cache", str(cache))
    assert again.exit_code == 0, again.output
    assert len(archive.requests) - sent == 12 and "0 answers reused" in again.output
    same = report(first, tmp_path / "c.csv", "--last-seen", "--last-seen-cache", str(cache))
    assert "6 answers reused" in same.output  # Each database still reuses its own.
