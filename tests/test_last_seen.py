"""``report --last-seen``: the archive's newest activity of silent accounts.

Everything runs against the scripted fake archive of ``test_reddit_archive``; no
network, no real sleeping.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot_farm_silence_tracker import last_seen as ls
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.models import STATUS_ACTIVE, STATUS_OPTED_OUT, Account
from bot_farm_silence_tracker.scoring import AccountResult, ScoringParams
from bot_farm_silence_tracker.sources.errors import BudgetExceeded
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


def test_scope_all_lists_the_other_observed_accounts_after_the_silent_ones(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    archive, db, _ = scanned
    before = len(archive.requests)
    out = tmp_path / "all.json"
    assert report(db, out, "--last-seen", "--last-seen-scope", "all").exit_code == 0
    assert len(archive.requests) - before == 24  # 12 observed accounts.
    rows = json.loads(out.read_text())["candidates"]
    assert [r["scored_silent"] for r in rows] == [True] * 6 + [False] * 6
    assert [r["rank"] for r in rows[:6]] == [1, 2, 3, 4, 5, 6]
    assert {r["rank"] for r in rows[6:]} == {None}
    human = next(r for r in rows if r["user_id"] == "human_acct_0")
    assert human["last_seen_status"] == "active_after_window"


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


def test_the_database_is_not_written_even_when_it_cannot_be_written(
    scanned: tuple[Archive, Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, db, _ = scanned
    for suffix in ("-wal", "-shm"):  # A finished scan leaves no write-ahead log behind.
        assert not Path(f"{db}{suffix}").exists() or Path(f"{db}{suffix}").stat().st_size == 0
    before = digest(db)
    assert report(db, tmp_path / "a.csv", "--last-seen").exit_code == 0
    assert digest(db) == before
    # A read-only copy in a read-only folder still works, so nothing needs a write.
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    copy = ro_dir / "run.db"
    copy.write_bytes(db.read_bytes())
    cache = tmp_path / "answers.jsonl"
    copy.chmod(stat.S_IRUSR)
    ro_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(ro_dir, os.W_OK):  # Running as a user the mode does not bind.
            pytest.skip("the file system does not enforce read-only modes here")
        out = tmp_path / "ro.csv"
        result_ = report(copy, out, "--last-seen", "--last-seen-cache", str(cache))
        assert result_.exit_code == 0, result_.output
    finally:
        ro_dir.chmod(stat.S_IRWXU)
        copy.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert digest(copy) == before and cache.exists()


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
