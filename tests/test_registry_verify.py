"""The registry spot-check with mocked sources."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.registry import records as records_module
from bot_farm_silence_tracker.registry import verify as verify_module
from bot_farm_silence_tracker.registry.records import b64d, b64e, record_errors, write_records
from bot_farm_silence_tracker.registry.verify import (
    FAILED,
    INCONCLUSIVE,
    UNVERIFIED,
    VERIFIED,
    SourceProbe,
    Stop,
    changed_records,
    check_record,
    load_side,
    render_summary,
    run_verify,
    sample_records,
)
from bot_farm_silence_tracker.sources.base import NewestActivity
from bot_farm_silence_tracker.sources.errors import AccountUnavailable
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.timeutil import isoformat

from .helpers import CUTOFF, EVENT, EVENTS, FIXTURE_EVENTS, SECOND

runner = CliRunner()
SCANNED = CUTOFF + timedelta(days=4)
LAST = CUTOFF - timedelta(hours=4)
NOW = CUTOFF + timedelta(days=10)
LAG = timedelta(hours=6)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The records are dated after the cutoff; pin "now" after them."""
    monkeypatch.setattr(records_module, "current_time", lambda: NOW)


def rec(name: str, platform: str = "reddit-archive", **changes: Any) -> dict[str, Any]:
    base = {
        "event_id": EVENT.id,
        "platform": platform,
        "id_b64": b64e(name),
        "username_b64": b64e(name),
        "first_scanned_at": isoformat(SCANNED),
        "last_scanned_at": isoformat(SCANNED),
        "last_activity_at": isoformat(LAST),
        "scan_window_end": isoformat(SCANNED - timedelta(hours=1)),
        "baseline_posts": 100,
        "baseline_active_days": 30,
        "cliff": True,
        "tool_version": "0.1.0",
        "status": "candidate",
        "evidence": None,
        "status_updated_at": isoformat(SCANNED),
    }
    base.update(changes)
    return base


class FakeProbe:
    """The live source, as a map from decoded user ID to activity times."""

    def __init__(self, activity: dict[str, list[datetime]], *, budget: int = 1000) -> None:
        self.activity = activity
        self.calls = 0
        self.budget = budget
        self.requests_made = 0
        self.unavailable: set[str] = set()

    def newest(self, record: dict[str, Any], since: datetime, until: datetime) -> NewestActivity:
        if self.calls >= self.budget:
            raise Stop("the request budget is used up")
        self.calls += 1
        self.requests_made += 1
        user = b64d(record["id_b64"])
        if user in self.unavailable:
            raise AccountUnavailable("suspended", "gone")
        stamps = [t for t in self.activity.get(user, []) if since <= t < until]
        return NewestActivity(max(stamps, default=None), since)


def history(*extra: datetime) -> list[datetime]:
    """Steady posts every 6 hours up to LAST, plus ``extra``."""
    return [LAST - timedelta(hours=6 * i) for i in range(40)] + list(extra)


def check(record: dict[str, Any], probe: FakeProbe) -> Any:
    return check_record(record, probe, now=NOW, lag=LAG)


def test_honest_record_is_verified() -> None:
    out = check(rec("honest"), FakeProbe({"honest": history()}))
    assert out.verdict == VERIFIED, out.reasons
    assert out.checks == {"A": VERIFIED, "B": VERIFIED}


def test_activity_newer_than_the_scanned_data_passes() -> None:
    after = SCANNED + timedelta(hours=2)
    in_unscanned_hour = SCANNED - timedelta(minutes=30)  # After the data end, before the scan.
    probe = FakeProbe({"recent": history(after, in_unscanned_hour)})
    assert check(rec("recent"), probe).verdict == VERIFIED


def test_fake_activity_fails_check_a() -> None:
    # The record claims a last activity where the source has none, and none before.
    probe = FakeProbe({"faked": []})
    record = rec("faked", last_activity_at=isoformat(LAST + timedelta(hours=3)))
    out = check(record, probe)
    assert out.verdict == FAILED
    assert out.checks["A"] == FAILED and out.checks["B"] == VERIFIED


def test_missing_last_activity_with_earlier_activity_is_inconclusive() -> None:
    # The owner may have deleted the last post: inconclusive, not a failure.
    probe = FakeProbe({"deleter": history()})
    record = rec("deleter", last_activity_at=isoformat(LAST + timedelta(hours=3)))
    out = check(record, probe)
    assert out.checks == {"A": INCONCLUSIVE, "B": VERIFIED}
    assert any("deleted" in r for r in out.reasons)


def test_hidden_activity_framing_fails_check_b() -> None:
    # The account kept posting until a day before the scan; the record claims it stopped.
    later = [LAST + timedelta(hours=6 * i) for i in range(1, 12)]
    out = check(rec("framed"), FakeProbe({"framed": history(*later)}))
    assert out.verdict == FAILED
    assert out.checks == {"A": VERIFIED, "B": FAILED}
    assert any("hidden activity" in r for r in out.reasons)


def test_activity_within_the_ingestion_lag_is_inconclusive() -> None:
    near_end = SCANNED - timedelta(hours=3)  # Inside (E - lag, E].
    out = check(rec("laggy"), FakeProbe({"laggy": history(near_end)}))
    assert out.verdict == INCONCLUSIVE
    assert out.checks["B"] == INCONCLUSIVE


def test_a_recent_last_activity_not_yet_archived_is_inconclusive() -> None:
    last = NOW - timedelta(hours=2)
    record = rec(
        "fresh",
        last_activity_at=isoformat(last),
        last_scanned_at=isoformat(NOW),
        scan_window_end=isoformat(NOW),
        first_scanned_at=isoformat(NOW),
    )
    out = check(record, FakeProbe({"fresh": history()}))
    assert out.checks["A"] == INCONCLUSIVE
    assert out.verdict == INCONCLUSIVE


def test_unavailable_accounts_and_short_windows_are_inconclusive() -> None:
    probe = FakeProbe({"gone": history()})
    probe.unavailable.add("gone")
    assert check(rec("gone"), probe).verdict == INCONCLUSIVE
    # A record cannot shorten its window to hide activity: the rules reject it.
    short = rec("short", scan_window_end=isoformat(LAST + timedelta(minutes=1)))
    assert any("days between" in e for e in record_errors(short))


def test_sampling_is_deterministic_and_capped() -> None:
    records = [rec(f"user{i}") for i in range(40)]
    one = sample_records(records, 10, "abc123")
    assert len(one) == 10
    assert one == sample_records(list(reversed(records)), 10, "abc123")
    assert one != sample_records(records, 10, "def456")
    assert [r["id_b64"] for r in one] == sorted(r["id_b64"] for r in one)
    assert sample_records(records[:3], 10, "abc123") == sorted(
        records[:3], key=lambda r: r["id_b64"]
    )


def test_changed_records_are_new_or_different() -> None:
    a, b, c = rec("aaa"), rec("bbb"), rec("ccc")
    base = {("reddit-archive", a["id_b64"]): a, ("reddit-archive", b["id_b64"]): b}
    b2 = {**b, "status": "verified_inauthentic"}
    head = {
        ("reddit-archive", a["id_b64"]): a,
        ("reddit-archive", b["id_b64"]): b2,
        ("reddit-archive", c["id_b64"]): c,
    }
    assert changed_records(base, head) == sorted([b2, c], key=lambda r: r["id_b64"])


def test_run_verify_counts_and_summary_name_only_id_b64() -> None:
    records = [
        rec("honest_one"),
        rec("honest_two"),
        rec("faked", last_activity_at=isoformat(LAST + timedelta(hours=3))),
        rec("framed"),
        rec("xuser", platform="x"),
        rec("officialreddit", platform="reddit"),
    ]
    activity = {
        "honest_one": history(),
        "honest_two": history(SCANNED + timedelta(days=1)),
        "faked": [],
        "framed": history(LAST + timedelta(days=2)),
    }
    head = {(r["event_id"], r["platform"], r["id_b64"]): r for r in records}
    report = run_verify({}, head, FakeProbe(activity), seed="sha", now=NOW, lag=LAG)
    assert (report.count(VERIFIED), report.count(FAILED)) == (2, 2)
    assert report.count(UNVERIFIED) == 2 and report.count(INCONCLUSIVE) == 0
    assert not report.ok
    text = render_summary(report)
    for name in activity:
        assert name not in text
    assert b64e("faked") in text and b64e("framed") in text
    assert "| 2 | 2 | 0 | 2 |" in text


def test_budget_stop_leaves_the_rest_inconclusive() -> None:
    records = [rec(f"user{i}") for i in range(5)]
    head = {(r["event_id"], r["platform"], r["id_b64"]): r for r in records}
    probe = FakeProbe({f"user{i}": history() for i in range(5)}, budget=4)
    report = run_verify({}, head, probe, seed="s", now=NOW, lag=LAG)
    assert report.stopped
    assert report.count(VERIFIED) >= 1
    assert report.count(INCONCLUSIVE) >= 3
    # Candidates that could not be checked fail the change.
    assert not report.ok
    assert any("inconclusive" in f for f in report.failures)


def test_source_probe_is_polite_and_budgeted() -> None:
    made: list[dict[str, Any]] = []

    class Client(HttpClient):
        def __init__(self, **kwargs: Any) -> None:
            made.append(kwargs)
            super().__init__(**kwargs)

    probe = SourceProbe(max_requests=7, client_factory=Client)
    probe._source(EVENT.id, "bluesky")
    probe._source(EVENT.id, "reddit-archive")
    assert [m["requests_per_second"] for m in made] == [0.5, 0.5]
    assert [m["max_requests"] for m in made] == [7, 7]  # The first spent nothing.
    probe.close()
    exhausted = SourceProbe(max_requests=7, client_factory=Client)
    exhausted._spent_closed = 7
    with pytest.raises(Stop):
        exhausted._source(EVENT.id, "bluesky")
    late = SourceProbe(timeout=timedelta(seconds=0), client_factory=Client)
    with pytest.raises(Stop):
        late.newest(rec("aaa"), LAST, SCANNED)


def test_source_probe_uses_each_events_cutoff() -> None:
    probe = SourceProbe(max_requests=7)
    try:
        # Without events: a cutoff in the distant past, which never narrows a query.
        assert probe._source(EVENT.id, "reddit-archive").config.cutoff.year == 1970
        probe.close()
        probe = SourceProbe(max_requests=7)
        probe.use_events(EVENTS)
        assert probe._source(EVENT.id, "reddit-archive").config.cutoff == EVENT.cutoff
        assert probe._source(SECOND.id, "reddit-archive").config.cutoff == SECOND.cutoff
    finally:
        probe.close()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_load_side_reads_directories_and_git_refs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base_ref = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    write_records(repo / "registry", [rec("aaa"), rec("bbb")])
    (repo / "events.toml").write_bytes(FIXTURE_EVENTS.read_bytes())
    shard = repo / "registry" / records_module.record_path(EVENT.id, "reddit-archive", b64e("aaa"))
    shard.write_text(shard.read_text() + "{bad\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "head")
    base, _ = load_side(base_ref, repo=repo)
    head, problems = load_side("HEAD", repo=repo)
    assert base == {}
    assert len(head) == 2
    assert len(problems) == 1 and "invalid JSON" in problems[0]
    from_dir, dir_problems = load_side(str(repo / "registry"), repo=repo)
    assert from_dir == head and dir_problems == problems
    with pytest.raises(ValueError):
        load_side("no-such-ref", repo=repo)


def test_cli_verify_fails_on_a_framed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head = tmp_path / "base", tmp_path / "head"
    base.mkdir()
    write_records(head, [rec("honest"), rec("framed")])
    activity = {"honest": history(), "framed": history(LAST + timedelta(days=2))}

    class Probe(FakeProbe):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(activity)

        def close(self) -> None:
            pass

    monkeypatch.setattr(verify_module, "SourceProbe", Probe)
    summary = tmp_path / "summary.md"
    args = ["registry", "verify", "--base", str(base), "--head", str(head), "--seed", "x",
            "--summary", str(summary)]  # fmt: skip
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    text = summary.read_text(encoding="utf-8")
    assert "| 1 | 1 | 0 | 0 |" in text and b64e("framed") in text
    assert "framed`" not in text.replace(b64e("framed"), "")
    write_records(head, [rec("honest")])
    assert runner.invoke(app, args).exit_code == 0


def test_source_probe_queries_the_archive_adapter_by_handle() -> None:
    """End to end through the reddit-archive adapter and HttpClient, on a mock transport."""
    import httpx

    posts = {"framed": [*history(LAST + timedelta(days=2))], "honest": history()}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        q = request.url.params
        after, before = int(q["after"]), int(q["before"])
        stamps = [
            int(t.timestamp())
            for t in posts.get(q["author"], [])
            if after < t.timestamp() < before and request.url.path.endswith("comments/search")
        ]
        rows = [{"created_utc": max(stamps)}] if stamps else []
        return httpx.Response(200, json={"data": rows})

    def factory(**kwargs: Any) -> HttpClient:
        return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, **kwargs)

    probe = SourceProbe(max_requests=50, client_factory=factory, now=NOW)
    try:
        honest = check_record(rec("honest"), probe, now=NOW, lag=LAG)
        framed = check_record(rec("framed"), probe, now=NOW, lag=LAG)
    finally:
        probe.close()
    assert honest.verdict == VERIFIED, honest.reasons
    assert framed.verdict == FAILED and framed.checks["B"] == FAILED
    assert {r.url.host for r in seen} == {"arctic-shift.photon-reddit.com"}
    assert all(r.url.params["sort"] == "desc" and r.url.params["limit"] == "1" for r in seen)
    assert probe.requests_made == len(seen) <= 50
