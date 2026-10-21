"""Tests for the desktop app's presenter (no Tk needed)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot_farm_silence_tracker import config as user_config
from bot_farm_silence_tracker.gui import presenter as P
from bot_farm_silence_tracker.gui.runner import run_cli
from bot_farm_silence_tracker.network import format_network, network_report
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.storage import event_db_path
from bot_farm_silence_tracker.timeutil import ensure_utc

from .helpers import EVENT, SECOND

#: "Now" for validation: a few days after the default cutoff, whatever it is.
NOW = P.DEFAULT_CUTOFF + timedelta(days=3)
#: The tests' own cutoff, unrelated to any real event, with a time of day.
FIXTURE_CUT = datetime(2031, 3, 4, 5, 6, tzinfo=UTC)
FIXTURE_NOW = FIXTURE_CUT + timedelta(days=9)


def _run(job: P.ScanJob, *, cancel_at: int | None = None, timeout: float = 120) -> list[P.Event]:
    """Start ``job``, optionally cancel it once ``cancel_at`` accounts were reached, and
    return every event once it has finished."""
    events: list[P.Event] = []
    job.start()
    deadline = time.monotonic() + timeout
    while job.running or not job.events.empty():
        for event in job.drain():
            events.append(event)
            if (
                cancel_at is not None
                and isinstance(event, P.Progress)
                and event.done >= cancel_at
                and not job.cancel_requested
            ):
                job.cancel()
        if time.monotonic() >= deadline:
            progress = [e for e in events if isinstance(e, P.Progress)]
            last = f"{progress[-1].done} of {progress[-1].total}" if progress else "none"
            pytest.fail(f"the scan did not finish within {timeout:g} s (last progress: {last})")
        time.sleep(0.005)
    return events


def _finished(events: list[P.Event]) -> P.Finished:
    done = [e for e in events if isinstance(e, P.Finished)]
    assert len(done) == 1
    return done[0]


def _fake_request(n: str = "200") -> P.ScanRequest:
    request, problems = P.validate_form(P.ScanForm(platform="fake", n=n), now=NOW)
    assert request is not None, problems
    return request


@pytest.fixture
def fake_run(tmp_path: Path) -> tuple[Path, int]:
    """A complete fake scan of 200 accounts: (database, run id)."""
    db = tmp_path / "scan.db"
    argv = [*P.scan_argv(_fake_request(), out=tmp_path / "out.csv", db=db), "--seed", "1"]
    finished = _finished(_run(P.ScanJob(argv, db=db)))
    assert finished.code == 0 and finished.run_status == "complete"
    assert finished.run_id is not None
    return db, finished.run_id


# -- input validation and command lines ---------------------------------------------


def test_defaults_are_valid_and_match_the_cli() -> None:
    form = P.ScanForm()
    assert form.n == "30"
    assert form.cutoff == P.DEFAULT_CUTOFF == ensure_utc(EVENT.cutoff)
    assert form.event == P.DEFAULT_EVENT_ID == EVENT.id
    request, problems = P.validate_form(form, now=NOW)
    assert problems == []
    assert request is not None
    assert request.platform == user_config.DEFAULT_PLATFORM
    assert request.n == 30 and request.seeds == ()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"n": "0"}, "Sample size must be at least 1."),
        ({"n": "ten"}, "Sample size must be a whole number."),
        ({"cutoff": NOW + timedelta(minutes=1)}, "The cutoff must be in the past."),
        ({"cutoff": FIXTURE_CUT.replace(tzinfo=None)}, "The cutoff must carry a time zone"),
        ({"platform": "myspace"}, "Unknown platform"),
        ({"concurrency": "9"}, "Accounts fetched at once must be between 1 and"),
        ({"platform": "x"}, "X scans need a spend cap"),
        ({"platform": "x", "max_usd": "5"}, "X bills for discovery"),
        ({"max_requests": "-1"}, "Request limit must be at least 1."),
        ({"max_consecutive_errors": "0"}, "Errors in a row before stopping must be at least 1."),
    ],
)
def test_invalid_input_is_refused_with_a_plain_message(changes: dict, message: str) -> None:
    form = P.ScanForm(**{**P.ScanForm().__dict__, **changes})
    request, problems = P.validate_form(form, now=NOW)
    assert request is None
    assert any(p.startswith(message) for p in problems), problems


def test_concurrency_limit_follows_the_adapter() -> None:
    assert P.max_concurrency("fake") == FakeSource.max_concurrency
    ok, _ = P.validate_form(P.ScanForm(platform="fake", concurrency="3"), now=NOW)
    assert ok is not None and ok.concurrency == 3
    refused, problems = P.validate_form(P.ScanForm(platform="x", concurrency="2"), now=NOW)
    assert refused is None and "between 1 and 1" in problems[0]


def test_scan_argv_is_the_cli_command(tmp_path: Path) -> None:
    form = P.ScanForm(
        platform="reddit-archive", n="50", seeds="worldnews\n europe, worldnews",
        cutoff=FIXTURE_CUT.replace(hour=0, minute=0),
    )  # fmt: skip
    request, _ = P.validate_form(form, now=FIXTURE_NOW)
    assert request is not None
    out, db = tmp_path / "o.csv", tmp_path / "d.db"
    assert P.scan_argv(request, out=out, db=db, hide_handles=True) == [
        "scan", "--platform", "reddit-archive", "--n", "50", "--cutoff", "2031-03-04",
        "--subreddit", "worldnews", "--subreddit", "europe",
        "--out", str(out), "--db", str(db), "--hide-handles",
    ]  # fmt: skip


def test_scan_argv_for_x_and_advanced_options(tmp_path: Path) -> None:
    form = P.ScanForm(
        platform="x", n="10", seeds="query one", max_usd="2.5", confirm_billed=True,
        max_requests="100", max_consecutive_errors="4", cutoff=FIXTURE_CUT,
    )  # fmt: skip
    request, problems = P.validate_form(form, now=FIXTURE_NOW)
    assert request is not None, problems
    argv = P.scan_argv(request, out=tmp_path / "o.csv")
    assert argv[:9] == ["scan", "--platform", "x", "--n", "10",
                        "--cutoff", "2031-03-04T05:06:00+00:00",
                        "--query", "query one"]  # fmt: skip
    assert argv[9:] == [
        "--max-requests", "100", "--max-consecutive-errors", "4", "--max-usd", "2.5", "--yes",
        "--out", str(tmp_path / "o.csv"),
    ]  # fmt: skip


def test_resume_argv(tmp_path: Path) -> None:
    out = tmp_path / "o.csv"
    assert P.resume_argv("bluesky", 7, out=out) == [
        "scan", "--platform", "bluesky", "--from-run", "7", "--out", str(out),
    ]  # fmt: skip
    assert P.resume_argv("bluesky", 7, out=out, event_id=SECOND.id) == [
        "scan", "--platform", "bluesky", "--from-run", "7", "--event", SECOND.id,
        "--out", str(out),
    ]  # fmt: skip


def test_estimate_text() -> None:
    assert P.describe_duration(30) == "under a minute"
    assert P.describe_duration(600) == "about 10 minutes"
    assert P.describe_duration(3 * 3600) == "about 3 hours"
    assert P.estimate_seconds("reddit-archive", 3000) > P.estimate_seconds("reddit-archive", 30)
    assert P.estimate_text("fake", 30).startswith("Estimated time: under a minute")


# -- start, cancel, resume -------------------------------------------------------------


def test_start_runs_the_scan_and_reports_progress(fake_run: tuple[Path, int]) -> None:
    db, _run_id = fake_run
    assert P.list_runs(db)[0]["status"] == "complete"
    assert P.resumable_runs(db) == []


def test_progress_and_log_events(tmp_path: Path) -> None:
    db = tmp_path / "scan.db"
    argv = P.scan_argv(_fake_request("150"), out=tmp_path / "o.csv", db=db)
    events = _run(P.ScanJob(argv, db=db))
    progress = [e for e in events if isinstance(e, P.Progress)]
    assert progress[-1] == P.Progress(150, 150)
    lines = [e.text for e in events if isinstance(e, P.LogLine)]
    assert any("Candidates for manual review" in line for line in lines)
    assert any(line.startswith("Wrote ") for line in lines)
    assert (tmp_path / "o.csv").is_file()


@pytest.mark.parametrize("concurrency", ["1", "3"])
def test_cancel_leaves_a_resumable_run_and_resume_finishes_it(
    tmp_path: Path, concurrency: str
) -> None:
    db = tmp_path / "scan.db"
    # 300 accounts leave a wide margin after the cancel at 100 (the scan stops within a
    # few accounts of it) while keeping the resume small. The resume commits about three
    # times per account and each commit is synced to disk, which can take tens of
    # milliseconds on a slow Windows runner: with 1500 accounts the resume alone could
    # take longer than the two-minute limit in _run.
    n = 300
    request, _ = P.validate_form(
        P.ScanForm(platform="fake", n=str(n), concurrency=concurrency), now=NOW
    )
    assert request is not None
    argv = [*P.scan_argv(request, out=tmp_path / "a.csv", db=db), "--seed", "2"]
    events = _run(P.ScanJob(argv, db=db), cancel_at=100)
    stopped = _finished(events)
    assert stopped.cancelled
    assert stopped.run_id == 1
    assert stopped.run_status == "cancelled"  # What Ctrl-C leaves on the command line.
    assert "Resume" in stopped.message
    fetched = max(e.done for e in events if isinstance(e, P.Progress))
    assert fetched < n

    resumable = P.resumable_runs(db)
    assert [r["run_id"] for r in resumable] == [1]
    resume = P.resume_argv("fake", 1, out=tmp_path / "b.csv", db=db)
    events = _run(P.ScanJob(resume, db=db))
    done = _finished(events)
    assert (done.code, done.run_id, done.run_status, done.cancelled) == (0, 2, "complete", False)
    lines = [e.text for e in events if isinstance(e, P.LogLine)]
    # The resume reuses what the cancelled run fetched, as `scan --from-run` does.
    assert any("reused from the cache" in line for line in lines)
    assert P.resumable_runs(db) == []


def test_cancel_before_start_makes_no_requests(tmp_path: Path) -> None:
    db = tmp_path / "scan.db"
    job = P.ScanJob(P.scan_argv(_fake_request("300"), out=tmp_path / "o.csv", db=db), db=db)
    job.cancel()
    events = _run(job)
    finished = _finished(events)
    assert finished.cancelled
    assert not any(isinstance(e, P.Progress) and e.done > 1 for e in events)


def test_a_failing_command_is_reported_not_raised(tmp_path: Path) -> None:
    db = tmp_path / "scan.db"
    job = P.ScanJob(["scan", "--platform", "nope", "--db", str(db)], db=db)
    finished = _finished(_run(job))
    assert finished.code == 2 and finished.run_id is None
    assert finished.message.startswith("The scan could not start")


def test_runner_exceptions_become_a_finished_event(tmp_path: Path) -> None:
    def broken(argv, sink, hooks):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    finished = _finished(_run(P.ScanJob(["scan"], db=tmp_path / "x.db", runner=broken)))
    assert finished.code == 1 and not finished.cancelled


# -- credentials -------------------------------------------------------------------

SECRET = "s3cr3t-sentinel-value-0123456789"
TOKEN_ENV = "BOT_FARM_SILENCE_TRACKER_FAKE_TOKEN"


def _files_containing(root: Path, needle: bytes) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


def test_session_credentials_reach_the_adapter_and_are_never_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(FakeSource, "required_credentials", (TOKEN_ENV,))
    seen: list[dict[str, str]] = []
    original = FakeSource.__init__

    def recording_init(self, config):  # type: ignore[no-untyped-def]
        seen.append(dict(config.credentials))
        original(self, config)

    monkeypatch.setattr(FakeSource, "__init__", recording_init)
    caplog.set_level(logging.DEBUG)
    config_dir = user_config.default_config_path().parent

    store = P.SessionCredentials()
    store.set(TOKEN_ENV, SECRET)
    assert SECRET not in repr(store) and SECRET not in str(store)
    db = tmp_path / "scan.db"
    job = P.ScanJob(
        P.scan_argv(_fake_request("100"), out=tmp_path / "o.csv", db=db),
        store.snapshot(),
        db=db,
    )
    events = _run(job)
    finished = _finished(events)
    assert finished.run_status == "complete"
    assert seen == [{TOKEN_ENV: SECRET}]  # Handed over like an environment variable.

    assert SECRET not in os.environ.get(TOKEN_ENV, "")
    assert not any(SECRET in value for value in os.environ.values()), "secret in environ"
    assert all(SECRET not in str(e) for e in events)
    assert SECRET not in caplog.text
    assert SECRET not in " ".join(job.argv)
    assert job._credentials == {}  # The job forgets its copy when it ends.
    db_bytes = b"".join(p.read_bytes() for p in tmp_path.glob("scan.db*"))
    assert db_bytes and SECRET.encode() not in db_bytes
    assert _files_containing(tmp_path, SECRET.encode()) == []
    if config_dir.is_dir():
        assert _files_containing(config_dir, SECRET.encode()) == []
    store.clear()
    assert store.snapshot() == {}


def test_missing_credentials_fail_like_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeSource, "required_credentials", (TOKEN_ENV,))
    db = tmp_path / "scan.db"
    events = _run(P.ScanJob(P.scan_argv(_fake_request("50"), out=tmp_path / "o.csv", db=db), db=db))
    assert _finished(events).code == 2
    assert any(TOKEN_ENV in e.text for e in events if isinstance(e, P.LogLine))


def test_session_credentials_ignore_blank_values() -> None:
    store = P.SessionCredentials()
    store.set("A", "x")
    store.set("A", "   ")
    assert not store.has("A")


def test_credential_fields_name_the_receiving_hosts() -> None:
    fields = {f.env: f for f in P.credential_fields("x", environ={})}
    token = fields["BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN"]
    assert token.required and token.secret and not token.from_environment
    assert token.receivers and all(host == "api.x.com" for host, _ in token.receivers)
    assert P.credential_fields("fake") == []
    with_env = P.credential_fields("x", environ={"BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN": "t"})
    assert with_env[0].from_environment


# -- platforms config --------------------------------------------------------------


def test_platform_settings_save_through_the_config_code(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert P.load_platform_settings(path).enabled == (user_config.DEFAULT_PLATFORM,)
    saved = P.save_platform_settings(["reddit-archive", "bluesky"], "bluesky", path)
    assert saved.enabled == ("reddit-archive", "bluesky")
    assert saved.default == "bluesky"
    assert path.read_text() == user_config.render(("reddit-archive", "bluesky"), "bluesky")
    loaded = P.load_platform_settings(path)
    assert (loaded.enabled, loaded.default) == (("reddit-archive", "bluesky"), "bluesky")


@pytest.mark.parametrize(
    ("enabled", "default"),
    [([], "reddit-archive"), (["reddit-archive"], "bluesky"), (["myspace"], "myspace")],
)
def test_platform_settings_refuse_invalid_choices(
    tmp_path: Path, enabled: list[str], default: str
) -> None:
    with pytest.raises(user_config.ConfigError):
        P.save_platform_settings(enabled, default, tmp_path / "config.toml")
    assert not (tmp_path / "config.toml").exists()


def test_platform_settings_report_a_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("not = [toml")
    settings = P.load_platform_settings(path)
    assert settings.error is not None
    assert P.save_platform_settings(["bluesky"], "bluesky", path).enabled == ("bluesky",)


# -- profile and community links -------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "handle", "user_id", "url"),
    [
        ("reddit", "Some_User-1", "Some_User-1", "https://www.reddit.com/user/Some_User-1"),
        ("reddit-archive", "abc", "abc", "https://www.reddit.com/user/abc"),
        ("bluesky", "alice.bsky.social", "did:plc:abc123",
         "https://bsky.app/profile/alice.bsky.social"),
        ("bluesky", "", "did:plc:abc123", "https://bsky.app/profile/did:plc:abc123"),
        ("x", "some_name", "12345", "https://x.com/some_name"),
        ("x", "", "12345", "https://x.com/i/user/12345"),
    ],
)  # fmt: skip
def test_profile_urls_from_fixed_templates(
    platform: str, handle: str, user_id: str, url: str
) -> None:
    assert P.profile_url(platform, handle, user_id) == url


@pytest.mark.parametrize(
    ("platform", "handle", "user_id"),
    [
        ("reddit", "../../evil", "../../evil"),
        ("reddit", "a b", "a b"),
        ("reddit", "x?next=https://evil.example", ""),
        ("reddit", "javascript:alert(1)", ""),
        ("bluesky", "https://evil.example/", "did:plc:../x"),
        ("bluesky", "ALICE.bsky.social/#", "not-a-did"),
        ("x", "name/with/slash", "12ab"),
        ("x", "a" * 16, ""),
        ("fake", "fake_user_1", "fake_1"),
    ],
)
def test_profile_urls_refuse_invalid_names(platform: str, handle: str, user_id: str) -> None:
    assert P.profile_url(platform, handle, user_id) is None


@pytest.mark.parametrize(
    ("platform", "community", "url"),
    [
        ("reddit-archive", "worldnews", "https://www.reddit.com/r/worldnews"),
        ("reddit", "r/europe", "https://www.reddit.com/r/europe"),
        ("reddit", "worldnews/../x", None),
        ("reddit", "a", None),
        ("reddit", "evil.example", None),
        ("bluesky", "at://did:plc:abc/app.bsky.graph.list/3kxyz",
         "https://bsky.app/profile/did:plc:abc/lists/3kxyz"),
        ("bluesky", "at://did:plc:abc/app.bsky.feed.generator/news",
         "https://bsky.app/profile/did:plc:abc/feed/news"),
        ("bluesky", "at://did:plc:abc/app.bsky.graph.starterpack/3k",
         "https://bsky.app/starter-pack/did:plc:abc/3k"),
        ("bluesky", "https://bsky.app/profile/alice.bsky.social/lists/3kx",
         "https://bsky.app/profile/alice.bsky.social/lists/3kx"),
        ("bluesky", "https://bsky.app/profile/evil.example@x/lists/3kx", None),
        ("bluesky", "https://evil.example/profile/a.b/lists/x", None),
        ("bluesky", "at://did:plc:abc/app.bsky.feed.post/3k", None),
        ("bluesky", "some search words", None),
        ("x", "anything", None),
    ],
)  # fmt: skip
def test_community_urls(platform: str, community: str, url: str | None) -> None:
    assert P.community_url(platform, community) == url


def test_open_link_only_opens_https() -> None:
    opened: list[str] = []
    assert P.open_link("https://www.reddit.com/user/abc", opened.append)
    assert not P.open_link(None, opened.append)
    assert not P.open_link("file:///etc/passwd", opened.append)
    assert opened == ["https://www.reddit.com/user/abc"]


# -- results, graph, filters and timelines ------------------------------------------------

CUT = FIXTURE_CUT


def _account(
    rank: int, communities: tuple[str, ...], cluster: int | None, conf: str = "signals: 2",
    platform: str = "reddit",
) -> P.FlaggedAccount:  # fmt: skip
    timeline = P.daily_counts([], CUT - timedelta(days=3), CUT, CUT + timedelta(days=1))
    return P.FlaggedAccount(
        rank=rank, platform=platform, user_id=f"u{rank}", handle=f"user{rank}",
        label=f"user{rank}", score=1 - rank / 10, signals=conf, cluster_id=cluster,
        cluster_size=1, last_activity=CUT, posts_per_day=1.0, provisional=False,
        communities=communities, profile_url=None, timeline=timeline,
    )  # fmt: skip


def _results() -> P.Results:
    accounts = [
        _account(1, ("worldnews", "europe"), 1),
        _account(2, ("worldnews",), 1, "signals: 1"),
        _account(3, ("politics",), None, "signals: 0"),
        _account(4, ("europe",), 2, "signals: 2", platform="bluesky"),
    ]
    return P.Results(
        run_id=1, platform="reddit", status="complete", accounts=accounts,
        communities=P._community_index(accounts, "reddit", False),
        clusters=P._clusters(accounts, CUT), summary=[], notices=[], hide_handles=False, cutoff=CUT,
    )  # fmt: skip


def test_graph_nodes_edges_and_sizes() -> None:
    graph = P.build_graph(_results())
    assert [n.key for n in graph.accounts] == ["reddit:u1", "reddit:u2", "reddit:u3", "bluesky:u4"]
    assert [(n.key, n.size) for n in graph.communities] == [
        ("europe", 2), ("worldnews", 2), ("politics", 1),
    ]  # fmt: skip
    assert sorted(graph.edges) == sorted([
        ("reddit:u1", "worldnews"), ("reddit:u1", "europe"), ("reddit:u2", "worldnews"),
        ("reddit:u3", "politics"), ("bluesky:u4", "europe"),
    ])  # fmt: skip
    assert [n.cluster_id for n in graph.accounts] == [1, 1, None, 2]


def test_graph_follows_the_filters() -> None:
    results = _results()
    graph = P.build_graph(results, P.Filters(community="worldnews"))
    assert [n.key for n in graph.accounts] == ["reddit:u1", "reddit:u2"]
    # Other communities of the remaining accounts stay visible, sized by them.
    assert {(n.key, n.size) for n in graph.communities} == {("worldnews", 2), ("europe", 1)}
    assert P.build_graph(results, P.Filters(community="nowhere")).accounts == []


@pytest.mark.parametrize(
    ("filters", "ranks"),
    [
        (P.Filters(), [1, 2, 3, 4]),
        (P.Filters(platform="bluesky"), [4]),
        (P.Filters(community="europe"), [1, 4]),
        (P.Filters(cluster="1"), [1, 2]),
        (P.Filters(cluster="none"), [3]),
        (P.Filters(signals="signals: 2"), [1, 4]),
        (P.Filters(community="worldnews", signals="signals: 1"), [2]),
    ],
)
def test_filters(filters: P.Filters, ranks: list[int]) -> None:
    assert [a.rank for a in P.filter_accounts(_results().accounts, filters)] == ranks


def test_filter_choices_and_cluster_colors() -> None:
    results = _results()
    choices = P.filter_choices(results)
    assert choices["platform"] == [P.ALL, "bluesky", "reddit"]
    assert choices["community"] == [P.ALL, "europe", "worldnews", "politics"]
    assert choices["cluster"] == [P.ALL, "1", "2", "none"]
    assert choices["signals"] == [P.ALL, "signals: 0", "signals: 1", "signals: 2"]
    one = P.cluster_color(1, results.clusters)
    assert one is not None and one != P.cluster_color(2, results.clusters)
    assert P.cluster_color(None, results.clusters) is None
    assert [c.size for c in results.clusters] == [2, 1]


def test_timeline_series() -> None:
    start = FIXTURE_CUT - timedelta(days=9)
    stamps = [start + timedelta(hours=h) for h in (1, 2, 30, 50, 240, 300)]
    stamps.append(start - timedelta(hours=1))  # Before the window: left out.
    line = P.daily_counts(stamps, start, FIXTURE_CUT, FIXTURE_CUT + timedelta(days=2))
    assert line.counts == (2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1)
    assert line.cutoff_index == 9
    assert line.start == start


def test_load_results_from_a_fake_run(fake_run: tuple[Path, int]) -> None:
    db, run_id = fake_run
    results = P.load_results(run_id, db=db)
    assert results.accounts and results.status == "complete"
    first = results.accounts[0]
    assert first.label == first.handle
    assert first.communities and set(first.communities) <= set(results.communities)
    assert all(re.fullmatch(r"signals: [0-9]", a.signals) for a in results.accounts)
    assert first.profile_url is None  # Demo accounts have no public profile.
    # The cliff shows: posts before the cutoff, none after.
    assert sum(first.timeline.counts[: first.timeline.cutoff_index]) > 0
    assert sum(first.timeline.counts[first.timeline.cutoff_index + 1 :]) == 0
    assert results.notices[0].startswith("PLACEBO: ") and results.placebo == results.notices[0]
    assert results.notices[1] == P.NOT_PROOF
    assert sum(c.accounts for c in results.communities.values()) >= len(results.accounts)

    hidden = P.load_results(run_id, db=db, hide_handles=True)
    assert [a.label for a in hidden.accounts] == [f"#{a.rank}" for a in hidden.accounts]
    assert all(a.profile_url is None for a in hidden.accounts)
    assert all(m.startswith("#") for c in hidden.clusters for m in c.members)


def test_hidden_handles_disable_profile_links(
    fake_run: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, run_id = fake_run
    monkeypatch.setattr(P, "profile_url", lambda *a: "https://www.reddit.com/user/x")
    assert P.load_results(run_id, db=db).accounts[0].profile_url is not None
    assert P.load_results(run_id, db=db, hide_handles=True).accounts[0].profile_url is None


def test_export_csv_matches_the_export_ids_command(
    fake_run: tuple[Path, int], tmp_path: Path
) -> None:
    db, run_id = fake_run
    ours = tmp_path / "ours.csv"
    theirs = tmp_path / "theirs.csv"
    count = P.export_csv(run_id, ours, db=db)
    code = run_cli(
        ["export-ids", "--run-id", str(run_id), "--db", str(db), "--out", str(theirs)],
        lambda _line: None,
    )
    assert code == 0
    assert ours.read_bytes() == theirs.read_bytes()
    assert count == len(ours.read_text().splitlines()) - 1


def test_export_csv_refuses_a_git_repository(fake_run: tuple[Path, int], tmp_path: Path) -> None:
    db, run_id = fake_run
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(ValueError, match="inside the git repository"):
        P.export_csv(run_id, repo / "out.csv", db=db)
    assert not (repo / "out.csv").exists()


def test_open_command() -> None:
    folder = Path("/x")
    assert P.open_command(folder, "darwin") == ["open", str(folder)]
    assert P.open_command(folder, "linux") == ["xdg-open", str(folder)]
    assert P.open_command(Path("/x"), "win32") is None


def test_network_text_is_the_network_command_plus_the_browser_note() -> None:
    text = P.network_text()
    assert text.startswith(format_network(network_report()))
    assert text.endswith(P.BROWSER_NOTE)


def test_the_cli_never_imports_tk() -> None:
    code = (
        "import sys; import bot_farm_silence_tracker.cli, bot_farm_silence_tracker.gui; "
        "import bot_farm_silence_tracker.gui.presenter; "
        "bad = [m for m in ('tkinter', '_tkinter', 'customtkinter') if m in sys.modules]; "
        "sys.exit(f'imported {bad}' if bad else 0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_gui_without_the_extra_explains_how_to_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import types

    from bot_farm_silence_tracker import gui

    stub = types.ModuleType("tkinter")
    stub.TkVersion = 9.0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tkinter", stub)
    monkeypatch.setitem(sys.modules, "customtkinter", None)
    assert gui.launch() == 1
    err = capsys.readouterr().err
    assert "bot-farm-silence-tracker[gui]" in err and "command line" in err


def test_gui_refuses_an_old_tk(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import types

    from bot_farm_silence_tracker import gui

    stub = types.ModuleType("tkinter")
    stub.TkVersion = 8.5  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tkinter", stub)
    assert gui.launch() == 1
    assert "needs Tk 8.6 or newer" in capsys.readouterr().err


# -- cutoff picker and events ----------------------------------------------------------


def test_event_choices_start_from_the_cli_default() -> None:
    default, strikes, second, custom = P.event_choices()
    assert strikes.id == "fixture-strikes"
    assert default.id == P.DEFAULT_EVENT_ID == EVENT.id and default.cutoff == P.DEFAULT_CUTOFF
    assert P.format_cutoff(P.DEFAULT_CUTOFF) in default.label and EVENT.name in default.label
    assert second.id == SECOND.id and second.cutoff == SECOND.cutoff
    assert P.event_for_cutoff(SECOND.cutoff) == second
    assert custom == P.CUSTOM_EVENT and custom.cutoff is None
    assert P.event_by_label(default.label) == default
    assert P.event_by_label("anything else") == P.CUSTOM_EVENT
    assert P.event_for_cutoff(P.DEFAULT_CUTOFF) == default
    assert P.event_for_cutoff(FIXTURE_CUT) == P.CUSTOM_EVENT


def test_picker_round_trip() -> None:
    values = P.picker_values(FIXTURE_CUT)
    assert values == {
        "year": "2031", "month": "03 Mar", "day": "04", "hour": "05", "minute": "06",
    }  # fmt: skip
    assert P.cutoff_from_picker(values) == FIXTURE_CUT
    choices = P.picker_choices(now=FIXTURE_NOW)
    for key, value in values.items():
        assert value in choices[key]
    assert choices["year"][0] == str(FIXTURE_NOW.year)


@pytest.mark.parametrize(
    "values",
    [
        {"year": "2031", "month": "02 Feb", "day": "30", "hour": "00", "minute": "00"},
        {"year": "2031", "month": "02 Feb", "day": "", "hour": "00", "minute": "00"},
        {"year": "2031", "month": "02 Feb"},
    ],
)
def test_picker_refuses_dates_that_do_not_exist(values: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="cutoff"):
        P.cutoff_from_picker(values)


def test_a_custom_cutoff_reaches_the_scan_the_results_timeline_and_groups(
    tmp_path: Path,
) -> None:
    cutoff = (P.utcnow() - timedelta(days=12)).replace(second=0, microsecond=0)
    request, problems = P.validate_form(
        P.ScanForm(platform="fake", n="300", event=P.CUSTOM_EVENT.id, cutoff=cutoff)
    )
    assert request is not None, problems
    db = tmp_path / "scan.db"
    argv = [*P.scan_argv(request, out=tmp_path / "o.csv", db=db), "--seed", "3"]
    assert argv[argv.index("--cutoff") + 1] == P.isoformat(cutoff)
    finished = _finished(_run(P.ScanJob(argv, db=db)))
    assert finished.run_status == "complete" and finished.run_id is not None
    assert P.list_runs(db)[0]["cutoff"] == cutoff
    assert P.format_cutoff(cutoff) in P.run_label(P.list_runs(db)[0])
    results = P.load_results(finished.run_id, db=db)
    assert results.cutoff == cutoff
    assert results.accounts
    for account in results.accounts:
        index = account.timeline.cutoff_index
        assert account.timeline.start + timedelta(days=index) <= cutoff
        assert cutoff < account.timeline.start + timedelta(days=index + 1)
        assert sum(account.timeline.counts[index + 1 :]) == 0
    assert results.clusters
    for cluster in results.clusters:
        assert cluster.cutoff == cutoff
        assert cluster.hours_before_cutoff is not None and cluster.hours_before_cutoff >= 0


# -- credentials never show up in text -----------------------------------------------


def test_scan_hooks_and_state_never_show_credential_values() -> None:
    from dataclasses import fields

    from bot_farm_silence_tracker.cli import ScanHooks

    store = P.SessionCredentials()
    store.set(TOKEN_ENV, SECRET)
    hooks = ScanHooks(credentials=store.snapshot())
    state = P.AppState(credentials=store)
    job = P.ScanJob(["scan"], store.snapshot())
    objects = [hooks, state, store, store.snapshot(), job, hooks.credentials, job._credentials]
    for obj in objects:
        assert SECRET not in repr(obj) and SECRET not in str(obj), type(obj)
        assert SECRET not in f"{obj!r:>80}"
    assert TOKEN_ENV in repr(hooks) and TOKEN_ENV in repr(store)
    # A plain dict is wrapped, and the field is excluded from the generated repr too.
    assert SECRET not in repr(ScanHooks(credentials={TOKEN_ENV: SECRET}))
    assert [f.repr for f in fields(ScanHooks) if f.name == "credentials"] == [False]


def test_a_traceback_with_locals_from_the_scan_path_has_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceback

    from bot_farm_silence_tracker.cli import ScanHooks

    monkeypatch.setattr(FakeSource, "required_credentials", (TOKEN_ENV,))

    def explode(credentials):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom inside the scan path")

    monkeypatch.setattr(FakeSource, "has_login", staticmethod(explode))
    store = P.SessionCredentials()
    store.set(TOKEN_ENV, SECRET)
    hooks = ScanHooks(credentials=store.snapshot())
    argv = P.scan_argv(_fake_request("20"), out=tmp_path / "o.csv", db=tmp_path / "s.db")
    with pytest.raises(RuntimeError) as caught:
        run_cli(argv, lambda _line: None, hooks)
    text = "".join(
        traceback.TracebackException.from_exception(caught.value, capture_locals=True).format()
    )
    assert "boom inside the scan path" in text
    assert "credentials" in text  # The frames and their locals are there ...
    assert SECRET not in text  # ... but no value.


# -- hiding names in communities, and the confidence ----------------------------------

BSKY_LIST = "at://did:plc:curator123abc/app.bsky.graph.list/3kaaaa"
BSKY_FEED_URL = "https://bsky.app/profile/curator.example.com/feed/whats-hot"
BSKY_PACK = "https://bsky.app/starter-pack/curator.example.com/3kbbbb"


def test_hidden_bluesky_communities_do_not_name_the_curator() -> None:
    from bot_farm_silence_tracker.cli import _Redactor

    accounts = [
        _account(1, (BSKY_LIST, BSKY_FEED_URL), None, platform="bluesky"),
        _account(2, (BSKY_LIST, BSKY_PACK, "plain seed"), None, platform="bluesky"),
    ]
    hidden = P.hide_community_names(accounts, _Redactor())
    shown = [c for a in hidden for c in a.communities]
    for leak in ("curator", "did:", "at://", "bsky.app"):
        assert not any(leak in c for c in shown), leak
    # The same community has the same label in every row ...
    assert hidden[0].communities[0] == hidden[1].communities[0] == "Bluesky list 1"
    assert hidden[0].communities[1] == "Bluesky feed 1"
    assert hidden[1].communities[1] == "Bluesky starter pack 1"
    assert hidden[1].communities[2] == "plain seed"
    # ... and in the map and the filter choices.
    index = P._community_index(hidden, "bluesky", True)
    assert set(index) == set(shown) and all(c.url is None for c in index.values())
    graph = P.build_graph(
        P.Results(
            run_id=1,
            platform="bluesky",
            status="complete",
            accounts=hidden,
            communities=index,
            clusters=[],
            summary=[],
            notices=[],
            hide_handles=True,
            cutoff=CUT,
        )
    )
    assert {n.key for n in graph.communities} == set(shown)
    # Without hiding, nothing changes.
    assert accounts[0].communities == (BSKY_LIST, BSKY_FEED_URL)


def test_signals_count_only_signals_independent_of_the_outage(
    fake_run: tuple[Path, int],
) -> None:
    from bot_farm_silence_tracker.controls import independent
    from bot_farm_silence_tracker.registry.build import fired_signals
    from bot_farm_silence_tracker.storage import Storage

    db, run_id = fake_run
    results = P.load_results(run_id, db=db)
    with Storage(db) as storage:
        outcome = P.rescore_run(storage, run_id)
    by_id = {r.user_id: r for r in outcome.results}
    for account in results.accounts:
        fired = fired_signals(by_id[account.user_id], outcome.context)
        assert account.signals == f"signals: {len(independent(fired))}"
        assert "synchronized_stop" not in {s.name for s in independent(fired)}
    assert P.signals_label(2) == "signals: 2"


def test_export_warning_names_the_real_names() -> None:
    assert "real account names" in P.EXPORT_WARNING and "Cancel" in P.EXPORT_WARNING


# -- outage events ---------------------------------------------------------------------


def test_a_named_event_is_scanned_with_event_and_a_picked_date_with_cutoff(
    tmp_path: Path,
) -> None:
    named, problems = P.validate_form(
        P.ScanForm(platform="fake", event=SECOND.id, cutoff=SECOND.cutoff), now=NOW
    )
    assert named is not None, problems
    assert named.event_id == SECOND.id and not named.custom
    argv = P.scan_argv(named, out=tmp_path / "o.csv")
    assert argv[argv.index("--event") + 1] == SECOND.id and "--cutoff" not in argv
    # A named event whose cutoff the picker no longer shows is a custom date.
    moved = SECOND.cutoff - timedelta(hours=5)
    custom, _ = P.validate_form(P.ScanForm(platform="fake", event=SECOND.id, cutoff=moved), now=NOW)
    assert custom is not None and custom.custom
    assert custom.event_id == "custom-" + moved.strftime("%Y%m%dT%H%MZ")
    argv = P.scan_argv(custom, out=tmp_path / "o.csv")
    assert "--event" not in argv and argv[argv.index("--cutoff") + 1] == P.isoformat(moved)


def test_the_config_default_event_leads_the_menu(tmp_path: Path) -> None:
    user_config.set_default_event(user_config.default_config_path(), SECOND.id)
    assert P.default_choice().id == SECOND.id and SECOND.cutoff == P.DEFAULT_CUTOFF
    assert [c.id for c in P.event_choices()] == [
        SECOND.id,
        EVENT.id,
        "fixture-strikes",
        P.CUSTOM_EVENT.id,
    ]


def test_runs_and_results_come_from_the_events_store(tmp_path: Path) -> None:
    named, _ = P.validate_form(
        P.ScanForm(platform="fake", n="200", event=SECOND.id, cutoff=SECOND.cutoff), now=NOW
    )
    assert named is not None
    argv = [*P.scan_argv(named, out=tmp_path / "a.csv"), "--seed", "1", "--now", P.isoformat(NOW)]
    finished = _finished(_run(P.ScanJob(argv, event_id=SECOND.id)))
    assert finished.code == 0 and finished.run_id == 1
    assert event_db_path(SECOND.id).is_file() and not event_db_path(EVENT.id).exists()
    runs = P.list_runs(event_id=SECOND.id)
    assert [r["event_id"] for r in runs] == [SECOND.id]
    assert P.list_runs() == []  # The default event's store has no run.
    results = P.load_results(1, event_id=SECOND.id)
    assert results.event_id == SECOND.id and results.cutoff == SECOND.cutoff
    assert all(a.also_in == () for a in results.accounts)
    # The same accounts scanned for the default event: each result names the other.
    default, _ = P.validate_form(P.ScanForm(platform="fake", n="200"), now=NOW)
    assert default is not None
    argv = [*P.scan_argv(default, out=tmp_path / "b.csv"), "--seed", "1", "--now", P.isoformat(NOW)]
    assert _finished(_run(P.ScanJob(argv))).code == 0
    first = P.load_results(1, event_id=SECOND.id)
    other = P.load_results(1)
    assert other.event_id == EVENT.id
    assert any(a.also_in == (EVENT.id,) for a in first.accounts)
    assert any(a.also_in == (SECOND.id,) for a in other.accounts)
    out = tmp_path / "private" / "ids.csv"
    assert P.export_csv(1, out, event_id=SECOND.id) > 0


def test_a_finished_scan_names_the_store_its_run_is_in(tmp_path: Path) -> None:
    named, _ = P.validate_form(
        P.ScanForm(platform="fake", n="60", event=SECOND.id, cutoff=SECOND.cutoff), now=NOW
    )
    assert named is not None
    argv = [*P.scan_argv(named, out=tmp_path / "a.csv"), "--seed", "1", "--now", P.isoformat(NOW)]
    finished = _finished(_run(P.ScanJob(argv, event_id=SECOND.id)))
    assert finished.event_id == SECOND.id and finished.run_id == 1
    default, _ = P.validate_form(P.ScanForm(platform="fake", n="60"), now=NOW)
    assert default is not None
    argv = [*P.scan_argv(default, out=tmp_path / "b.csv"), "--seed", "1", "--now", P.isoformat(NOW)]
    assert _finished(_run(P.ScanJob(argv))).event_id == EVENT.id
    runs = P.list_runs(event_id=SECOND.id)
    assert runs[0]["store"] == event_db_path(SECOND.id) and runs[0]["store_event"] == SECOND.id
    # With an explicit database the user's other stores are not read.
    db = tmp_path / "own.db"
    argv = [*P.scan_argv(default, out=tmp_path / "c.csv", db=db), "--seed", "1"]
    assert _finished(_run(P.ScanJob(argv, db=db))).event_id is None
    assert all(a.also_in == () for a in P.load_results(1, db=db).accounts)
