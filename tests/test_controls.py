"""The method's controls: the permutation null of stop-time clusters and its
maximum statistic, placebo samples, the stricter steady test, the candidate rule, the live
profile check, the 21-day verified threshold, draft refusals and the draft wording."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot_farm_silence_tracker import controls as C
from bot_farm_silence_tracker.events import parse_events
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.registry import live as L
from bot_farm_silence_tracker.registry import records as R
from bot_farm_silence_tracker.registry.build import is_candidate
from bot_farm_silence_tracker.registry.records import b64e, evidence_path, write_records
from bot_farm_silence_tracker.registry.reports import build_drafts, render_reddit
from bot_farm_silence_tracker.registry.review import review_problems
from bot_farm_silence_tracker.registry.verify import plan_changes
from bot_farm_silence_tracker.scoring import (
    AccountResult,
    ScoringParams,
    apply_clusters,
    score_account,
    weekly_cv,
)
from bot_farm_silence_tracker.sources.http import HttpClient

from .helpers import CUTOFF, EVENT, EVENTS, NOW, steady

PARAMS = ScoringParams(cutoff=CUTOFF, now=NOW, cliff_window_hours=24.0)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the registry's clock after the test records' scans."""
    monkeypatch.setattr(R, "current_time", lambda: CUTOFF + timedelta(days=60))


def cliff(
    user_id: str, last: datetime, profile: dict[int, int] | None = None, **fields: Any
) -> AccountResult:
    """A cliff account that stopped at ``last``, with an hour-of-day profile."""
    hours = profile or dict.fromkeys(range(24), 4)
    return AccountResult(
        "fake",
        user_id,
        f"h_{user_id}",
        "active",
        eligible=True,
        cliff=True,
        last_activity=last,
        hour_profile=tuple(hours.get(h, 0) for h in range(24)),
        **fields,
    )


def clustered(results: list[AccountResult], params: ScoringParams = PARAMS) -> list[AccountResult]:
    apply_clusters(results, params.sync_minutes)
    return results


# -- the permutation null -------------------------------------------------------------


def test_a_tight_cluster_of_accounts_posting_all_day_is_significant() -> None:
    # Eight accounts that post at every hour stop within ten minutes; four others stop
    # hours apart. Under the null their stops spread over the whole 24-hour window.
    stop = CUTOFF - timedelta(hours=6)
    results = [cliff(f"c{i}", stop + timedelta(minutes=i)) for i in range(8)]
    results += [cliff(f"s{i}", CUTOFF - timedelta(hours=10 + 3 * i)) for i in range(4)]
    tests = C.cluster_tests(clustered(results), PARAMS, permutations=499)
    assert list(tests) == [1]
    test = tests[1]
    assert test.size == 8
    assert test.p_permutation == pytest.approx(1 / 500)
    assert test.p_adjusted == test.p_permutation  # Already adjusted.


def test_night_owls_who_stop_in_their_usual_hour_are_not_significant() -> None:
    # Six accounts that post only at 23:00 UTC stop in that hour. A uniform clock would
    # find that remarkable; their own hour-of-day profiles do not.
    night = datetime(2026, 10, 9, 23, 0, tzinfo=UTC)
    params = ScoringParams(cutoff=night + timedelta(hours=12), now=NOW, cliff_window_hours=24.0)
    profile = {23: 200}
    results = [cliff(f"n{i}", night + timedelta(minutes=4 * i), profile) for i in range(6)]
    uniform = [cliff(f"u{i}", night + timedelta(minutes=4 * i)) for i in range(6)]
    owls = C.cluster_tests(clustered(results, params), params, permutations=499)
    day = C.cluster_tests(clustered(uniform, params), params, permutations=499)
    assert owls[1].p_permutation > 0.05
    assert day[1].p_permutation < 0.01


def test_accounts_found_in_one_thread_stay_together_under_the_null() -> None:
    stop = CUTOFF - timedelta(hours=6)
    results = [cliff(f"t{i}", stop + timedelta(minutes=i)) for i in range(5)]
    results += [cliff(f"s{i}", CUTOFF - timedelta(hours=10 + 3 * i)) for i in range(4)]
    clustered(results)
    alone = C.cluster_tests(results, PARAMS, permutations=199)
    threads = {f"t{i}": "t3_thread" for i in range(5)}
    together = C.cluster_tests(results, PARAMS, threads, permutations=199)
    assert alone[1].p_permutation < 0.02
    # One conversation moved as a block holds its five stops (unless the block wraps
    # around the window's end): no evidence.
    assert together[1].p_permutation > 0.9


def test_the_null_is_seeded_by_the_run() -> None:
    stop = CUTOFF - timedelta(hours=6)
    results = clustered([cliff(f"c{i}", stop + timedelta(minutes=9 * i)) for i in range(3)])
    first = C.cluster_tests(results, PARAMS, permutations=99)
    again = C.cluster_tests(results, PARAMS, permutations=99)
    assert first == again


def test_every_cluster_is_compared_with_the_null_of_the_run_s_largest_cluster() -> None:
    # Two clusters of three: both are measured against the distribution of the largest
    # cluster of each permutation, which already adjusts for the run's many clusters,
    # so the adjusted p-value is the permutation p-value itself (no Holm on top).
    stop = CUTOFF - timedelta(hours=4)
    results = [cliff(f"a{i}", stop + timedelta(minutes=i)) for i in range(3)]
    results += [cliff(f"b{i}", stop - timedelta(hours=8, minutes=-i)) for i in range(3)]
    tests = C.cluster_tests(clustered(results), PARAMS, permutations=199)
    assert sorted(tests) == [1, 2]
    for test in tests.values():
        assert test.p_adjusted == test.p_permutation
    # Equal sizes, equal p-values: the null is the run's largest cluster, not each one's.
    assert tests[1].p_permutation == tests[2].p_permutation
    assert not hasattr(C, "holm")


def test_a_tight_cluster_stays_significant_in_a_run_with_many_clusters() -> None:
    # With 999 permutations and Holm over more than ten clusters no cluster could reach
    # 0.01. Twelve loose clusters of three and one tight cluster of eight: the tight one
    # is significant with the default permutations, the loose ones are not.
    assert C.PERMUTATIONS >= 9999
    stop = CUTOFF - timedelta(hours=1)
    results = [cliff(f"t{i}", stop + timedelta(minutes=i)) for i in range(8)]
    for c in range(12):
        base = CUTOFF - timedelta(hours=3 + 1.7 * c)
        results += [cliff(f"l{c}_{i}", base + timedelta(minutes=12 * i)) for i in range(3)]
    tests = C.cluster_tests(clustered(results), PARAMS)
    assert len(tests) == 13
    tight = next(t for t in tests.values() if t.size == 8)
    assert tight.p_adjusted <= C.CLUSTER_P_MAX
    assert tight.p_adjusted >= 1 / (C.PERMUTATIONS + 1)
    assert all(t.p_adjusted > C.CLUSTER_P_MAX for t in tests.values() if t.size == 3)


def test_a_synchronized_stop_alone_never_makes_a_candidate() -> None:
    stop = CUTOFF - timedelta(hours=6)
    results = [cliff(f"c{i}", stop + timedelta(minutes=i)) for i in range(8)]
    results += [cliff(f"s{i}", CUTOFF - timedelta(hours=10 + 3 * i)) for i in range(4)]
    context = C.signal_context(clustered(results), PARAMS, permutations=499)
    signals = C.fired_signals(results[0], context)
    assert [s.name for s in signals] == ["synchronized_stop"]
    assert signals[0].independent is False
    assert "not evidence of automation on its own" in signals[0].meaning
    assert not is_candidate(results[0], signals)
    regular = cliff("r", stop, interarrival_cv=0.2)
    both = [*signals, *C.fired_signals(regular, context)]
    assert is_candidate(regular, both)
    C.annotate(results, context)
    assert results[0].signals == ("synchronized_stop",)
    assert results[0].independent_signals == 0
    assert results[0].cluster_p_adjusted == context.clusters[1].p_adjusted


# -- the steady test --------------------------------------------------------------


def test_weekly_cv_measures_whole_weeks() -> None:
    end = CUTOFF
    even = [end - timedelta(days=d, hours=1) for d in range(28)]
    assert weekly_cv(even, end, 30) == pytest.approx(0.0)
    burst = [end - timedelta(days=27, minutes=m) for m in range(40)]
    assert weekly_cv(burst, end, 30) == pytest.approx(3**0.5)
    assert weekly_cv(even, end, 10) is None  # One whole week: nothing to compare.


def test_a_burst_that_went_quiet_before_the_onset_is_not_steady() -> None:
    account = Account("fake", "u", "h")
    params = ScoringParams(cutoff=CUTOFF, now=NOW)
    start = params.baseline_start
    # 20 active days and 80 posts, all in the first weeks; nothing in the last week.
    burst = steady("u", start, start + timedelta(days=20), per_day=4)
    result = score_account(account, burst, params)
    assert result.active_days >= params.min_active_days and result.baseline_posts >= 20
    assert result.weekly_cv is not None and result.weekly_cv <= params.max_weekly_cv
    assert result.recent_posts == 0
    assert not result.eligible
    assert score_account(account, burst, replace(params, recent_days=0)).eligible
    # Steady through the whole baseline: eligible, and the numbers are recorded.
    full = steady("u", start, CUTOFF - timedelta(hours=2), per_day=4)
    good = score_account(account, full, params)
    assert good.eligible and good.cliff
    assert good.recent_posts == 28 and good.weekly_cv == pytest.approx(0.0, abs=0.05)
    row = good.to_row()
    assert row["recent_posts"] == 28 and "weekly_cv" in row


def test_uneven_weeks_fail_the_dispersion_cap() -> None:
    account = Account("fake", "u", "h")
    params = ScoringParams(cutoff=CUTOFF, now=NOW)
    start = params.baseline_start
    stamps = steady("u", start, start + timedelta(days=12), per_day=1)
    # Every day for twelve days, then a flood in the last week.
    stamps += steady("u", CUTOFF - timedelta(days=6), CUTOFF - timedelta(hours=1), per_day=24)
    result = score_account(account, stamps, params)
    assert result.weekly_cv is not None and result.weekly_cv > params.max_weekly_cv
    assert not result.eligible


# -- placebo runs --------------------------------------------------------------------


def _people(
    n: int, params: ScoringParams, stop_at: Callable[[int], datetime | None], prefix: str = "p"
) -> list:
    """``n`` steady posters from the baseline start; account ``i`` stops at ``stop_at(i)``."""
    items = []
    for i in range(n):
        uid = f"{prefix}{i:03d}"
        end = stop_at(i) or params.now
        acts = steady(uid, params.baseline_start + timedelta(minutes=7 * i), end, per_day=3)
        items.append((Account("fake", uid, f"h{uid}"), acts))
    return items


def _covered(items: list, params: ScoringParams) -> dict[str, datetime]:
    return {a.user_id: params.baseline_start for a, _ in items}


def _placebos(
    params: ScoringParams, n: int, stop_at: Callable[[int, ScoringParams], datetime | None]
) -> list[C.PlaceboRun]:
    """One placebo pass per placebo week, each on a sample of its own."""
    runs = []
    for k in range(1, params.placebo_weeks + 1):
        moved = C.shifted_params(params, k)
        own = _people(n, moved, lambda i, m=moved: stop_at(i, m), prefix=f"k{k}_")
        runs.append(C.score_pass(k, own, moved, covered_since=_covered(own, moved), permutations=9))
    return runs


def test_placebo_cutoffs_move_by_whole_weeks_with_their_markers() -> None:
    event = EVENTS.get("fixture-strikes") or EVENT
    params = ScoringParams(
        cutoff=CUTOFF,
        now=NOW,
        markers=event.scoring_markers(),
        marker_margin_hours=event.scoring_marker_margin(),
    )
    moved = C.shifted_params(params, 2)
    assert moved.cutoff == CUTOFF - timedelta(weeks=2)
    assert moved.cutoff.weekday() == CUTOFF.weekday() and moved.cutoff.hour == CUTOFF.hour
    assert moved.now == NOW - timedelta(weeks=2)
    assert [m.time for m in moved.markers] == [m.time - timedelta(weeks=2) for m in params.markers]
    # The run's own sample needs no older weeks: the placebo cutoffs have samples of
    # their own.
    assert params.fetch_start == params.baseline_start
    assert moved.fetch_start == params.baseline_start - timedelta(weeks=2)


def test_a_real_excess_is_clearly_above_the_placebo() -> None:
    params = ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=3)
    # A third of the accounts stop just before the real cutoff; nobody in the placebo
    # samples stops before theirs.
    items = _people(60, params, lambda i: CUTOFF - timedelta(hours=3) if i % 3 == 0 else None)
    comparison = C.placebo_comparison(
        items,
        params,
        covered_since=_covered(items, params),
        permutations=49,
        placebos=_placebos(params, 60, lambda i, m: None),
        requests=123,
    )
    assert comparison.available and len(comparison.placebos) == 3
    assert [run.weeks_before for run in comparison.placebos] == [1, 2, 3]
    cliff = comparison.compare(C.CLIFF_RATE)
    assert cliff is not None and cliff.real == pytest.approx(20 / 60)
    assert cliff.mean == 0.0 and cliff.clearly_above
    headline = C.headline(comparison)
    assert "clearly above the placebo" in headline and "placebo samples of their own" in headline
    data = comparison.to_dict()
    assert data["rates"]["cliff"]["clearly_above"] is True
    assert set(data["rates"]) == {"cliff", *C.SIGNAL_NAMES, C.INDEPENDENT_RATE}
    assert data["requests"] == 123
    assert any("123 requests beyond the run's own" in line for line in C.placebo_lines(comparison))
    # The stored counts come back for report --run-id.
    runs, reason, requests = C.stored_placebo_runs(data)
    assert reason is None and requests == 123 and runs == comparison.placebos


def test_no_excess_says_so_prominently() -> None:
    params = ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=3)

    # The same share of accounts stops for good before every cutoff, real and placebo
    # alike, each pass on a sample of its own.
    def stop(i: int, moved: ScoringParams) -> datetime | None:
        return moved.cutoff - timedelta(hours=2) if i % 5 == 0 else None

    items = _people(50, params, lambda i: stop(i, params))
    comparison = C.placebo_comparison(
        items,
        params,
        covered_since=_covered(items, params),
        permutations=19,
        placebos=_placebos(params, 50, stop),
    )
    cliff = comparison.compare(C.CLIFF_RATE)
    assert cliff is not None and not cliff.clearly_above
    assert cliff.low < 0 < cliff.high
    headline = C.headline(comparison)
    assert "NOT CLEARLY ABOVE THE PLACEBO" in headline
    # It does not claim the rates are equal, only that the difference was not resolved.
    assert "not clearly higher than in ordinary weeks" in headline
    assert "the difference was not resolved" in headline
    assert "as many steady accounts" not in headline


def test_the_run_s_own_sample_is_never_re_scored_at_a_placebo_cutoff() -> None:
    # The bias the review found: the run's sample was discovered by activity just
    # before the real cutoff, so an account that stopped for good at a placebo cutoff
    # can never be in it. Without placebo samples of their own there is no comparison.
    params = ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=3)
    items = _people(30, params, lambda i: CUTOFF - timedelta(hours=3) if i < 3 else None)
    comparison = C.placebo_comparison(items, params, covered_since=_covered(items, params))
    assert not comparison.available and comparison.unavailable == C.NO_SAMPLES
    assert comparison.real is None and not comparison.clearly_above
    assert "no placebo comparison" in C.headline(comparison)


def test_the_interval_counts_the_real_rate_s_own_noise() -> None:
    # 3 cliffs in 300 against placebo samples with none. A floor at the placebo rate's
    # noise alone called this clearly above; the real rate's binomial noise is as large
    # as the excess, so it is not resolved.
    def run(k: int, cliffs: int) -> C.PlaceboRun:
        return C.PlaceboRun(k, CUTOFF - timedelta(weeks=k), 300, cliffs, {}, accounts=300)

    comparison = C.PlaceboComparison(3, accounts=300, real=run(0, 3))
    comparison.placebos = [run(k, 0) for k in (1, 2, 3)]
    cliff = comparison.compare(C.CLIFF_RATE)
    assert cliff is not None and cliff.excess == pytest.approx(0.01)
    assert not cliff.clearly_above and cliff.low < 0
    # A large excess on the same sizes is resolved.
    comparison.real = run(0, 30)
    assert comparison.clearly_above


def test_each_pass_uses_only_accounts_with_data_back_to_its_baseline() -> None:
    params = ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=3)
    items = _people(6, params, lambda i: None)
    late = {a.user_id: params.baseline_start + timedelta(days=1) for a, _ in items}
    comparison = C.placebo_comparison(
        items,
        params,
        covered_since=late,
        permutations=9,
        placebos=_placebos(params, 6, lambda i, m: None),
    )
    assert not comparison.available and comparison.excluded == 6
    assert "no scored account of the run has data back to" in (comparison.unavailable or "")
    assert "no placebo comparison" in C.headline(comparison)
    off = C.placebo_comparison(
        items, ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=0), covered_since=late
    )
    assert "turned off" in (off.unavailable or "")
    # A run stored before placebo samples existed is not compared again.
    old = {"weeks": 3, "placebos": [], "unavailable": None}
    assert "rescan" in (C.stored_placebo_runs(old)[1] or "")


def test_a_fake_scan_reports_the_placebo_everywhere(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from bot_farm_silence_tracker.cli import app

    db, out = tmp_path / "c.db", tmp_path / "o.json"
    result = CliRunner().invoke(
        app,
        ["scan", "--platform", "fake", "--n", "200", "--seed", "3", "--now",
         NOW.isoformat(), "--db", str(db), "--out", str(out)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "PLACEBO: cliff rate" in result.output
    assert "3 placebo samples of 200 accounts each are discovered and fetched" in result.output
    assert "requests beyond the run's own" in result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["placebo_notice"][0].startswith("PLACEBO: cliff rate")
    assert payload["summary"]["placebo"]["weeks"] == 3
    run = payload["run"]
    assert run["seed"] == 3 and run["placebo_weeks"] == 3 and run["now_given"] is True
    assert run["fetch_start"] < run["cutoff"] and run["scan_tool_version"]
    assert len(run["seeds_sha256"]) == 64
    csv_out = tmp_path / "o.csv"
    report = CliRunner().invoke(
        app, ["report", "--run-id", "1", "--db", str(db), "--out", str(csv_out)]
    )
    assert report.exit_code == 0, report.output
    assert "# PLACEBO: cliff rate" in csv_out.read_text(encoding="utf-8")


def test_a_scan_without_a_seed_records_a_random_one(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from bot_farm_silence_tracker.cli import app
    from bot_farm_silence_tracker.storage import Storage

    db = tmp_path / "c.db"
    result = CliRunner().invoke(
        app,
        ["scan", "--platform", "fake", "--n", "30", "--now", NOW.isoformat(), "--db",
         str(db), "--out", str(tmp_path / "o.csv"), "--placebo-weeks", "0"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Storage(db) as storage:
        params = storage.get_run(1)["params"]  # type: ignore[index]
    assert isinstance(params["seed"], int)
    assert f"repeat with --seed {params['seed']}" in result.output
    assert params["tool_version"] and params["seeds_sha256"]
    assert params["scoring"]["placebo_weeks"] == 0
    bad = CliRunner().invoke(app, ["scan", "--platform", "fake", "--placebo-weeks", "2"])
    assert bad.exit_code != 0 and "at least 3" in bad.output


# -- the live profile check ----------------------------------------------------------


def _checker(answers: dict[str, httpx.Response], seen: list[httpx.Request]) -> L.LiveChecker:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers[request.url.params["actor"]]

    def factory(**kwargs: Any) -> HttpClient:
        return HttpClient(**kwargs, transport=httpx.MockTransport(handler), sleep=lambda _s: None)

    return L.LiveChecker(client_factory=factory, now=NOW, clock=lambda: NOW)


def test_bluesky_live_check_reads_the_public_profile_logged_out() -> None:
    seen: list[httpx.Request] = []
    answers = {
        "did:plc:alive": httpx.Response(200, json={"did": "did:plc:alive", "handle": "a.b.c"}),
        "did:plc:gone": httpx.Response(400, json={"error": "AccountTakedown"}),
        "did:plc:left": httpx.Response(400, json={"error": "AccountDeactivated"}),
        "did:plc:down": httpx.Response(502, text="bad gateway"),
    }
    checker = _checker(answers, seen)
    try:
        assert checker.check("bluesky", "did:plc:alive", "a.b.c").status == L.ACTIVE
        assert checker.check("bluesky", "did:plc:gone", "g.b.c").status == L.SUSPENDED
        assert checker.check("bluesky", "did:plc:left", "l.b.c").status == L.DELETED
        failed = checker.check("bluesky", "did:plc:down", "d.b.c")
    finally:
        checker.close()
    assert failed.status == L.NOT_CHECKED and failed.blocks_candidate
    assert {r.url.host for r in seen} == {"public.api.bsky.app"}
    assert {r.url.path for r in seen} == {"/xrpc/app.bsky.actor.getProfile"}
    assert all("authorization" not in r.headers for r in seen)
    ok = L.LiveStatus(L.ACTIVE, "m", NOW)
    assert not ok.blocks_candidate and ok.to_dict()["checked_at"] == NOW.isoformat()


def test_reddit_is_never_requested_and_needs_the_verifier() -> None:
    seen: list[httpx.Request] = []
    checker = _checker({}, seen)
    status = checker.check("reddit-archive", "some_author", "some_author")
    assert status.status == L.ARCHIVE_ONLY and status.needs_verifier
    assert not status.blocks_candidate
    assert checker.check("x", "123", "abc").status == L.NEEDS_CREDENTIALS
    assert seen == []
    assert L.no_live_check("bluesky", "did:plc:a", "a.b.c").status == L.NOT_CHECKED


# -- the 21-day verified threshold, resumed records and draft refusals -----------------


def _record(**changes: Any) -> dict[str, Any]:
    last = CUTOFF - timedelta(hours=3)
    end = last + timedelta(days=25)
    record = {
        "event_id": EVENT.id,
        "platform": "reddit-archive",
        "id_b64": b64e("some_author"),
        "username_b64": b64e("some_author"),
        "first_scanned_at": end.isoformat(),
        "last_scanned_at": end.isoformat(),
        "last_activity_at": last.isoformat(),
        "scan_window_end": end.isoformat(),
        "baseline_posts": 90,
        "baseline_active_days": 28,
        "cliff": True,
        "tool_version": "0.2.0",
        "status": "candidate",
        "evidence": None,
        "status_updated_at": end.isoformat(),
    }
    record.update(changes)
    return record


def _key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (record["event_id"], record["platform"], record["id_b64"])


def test_verified_needs_the_longer_silence_of_the_event() -> None:
    assert EVENT.verified_min_silence_days == 21.0
    base = _record()
    path = evidence_path("reddit-archive", base["id_b64"])
    for days, ok in ((10, False), (22, True)):
        end = (CUTOFF - timedelta(hours=3) + timedelta(days=days)).isoformat()
        head = _record(
            status="verified_inauthentic",
            evidence=path,
            scan_window_end=end,
            last_scanned_at=end,
            first_scanned_at=end,
            status_updated_at=end,
        )
        plan = plan_changes({_key(base): base}, {_key(head): head}, seed="s", events=EVENTS)
        failed = any("verified_min_silence_days" in f for f in plan.failures)
        assert failed is not ok


def test_events_validate_the_verified_threshold() -> None:
    text = """
default = "e-one"
legacy_event = "e-one"
[[event]]
id = "e-one"
name = "One"
cutoff = "2026-01-01T00:00:00Z"
baseline_days = 30
min_silence_days = 3.0
cliff_window_hours = 48.0
verified_min_silence_days = {value}
"""
    catalog, problems = parse_events(text.format(value="28.0"), require_windows=True)
    assert problems == [] and catalog is not None
    assert catalog.default.verified_min_silence == timedelta(days=28)
    _, low = parse_events(text.format(value="2.0"), require_windows=True)
    assert any("at least min_silence_days" in p for p in low)
    _, bad = parse_events(text.format(value='"many"'), require_windows=True)
    assert any("verified_min_silence_days must be a number" in p for p in bad)
    plain, _ = parse_events(text.replace("verified_min_silence_days = {value}\n", ""))
    assert plain is not None and plain.default.verified_min_silence_days == 21.0


def test_the_gate_flags_verified_records_that_resumed() -> None:
    path = evidence_path("reddit-archive", b64e("some_author"))
    base = _record(status="verified_inauthentic", evidence=path)
    later = (CUTOFF + timedelta(days=2)).isoformat()
    head = dict(base, last_activity_at=later, cliff=False)
    plan = plan_changes({_key(base): base}, {_key(head): head}, seed="s", events=EVENTS)
    assert [key for key, _ in plan.resumed] == [_key(head)]
    assert "no longer a cliff" in plan.resumed[0][1]
    into = plan_changes({}, {_key(head): head}, seed="s", events=EVENTS)
    assert any("although it is no longer a cliff" in f for f in into.failures)


def _write_registry(root: Path, record: dict[str, Any], evidence: dict[str, Any] | None) -> None:
    write_records(root, [record])
    if record["evidence"]:
        target = root / "events" / record["event_id"] / record["evidence"]
        target.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(evidence or {}, indent=2)
        target.write_text(f"# Evidence\n\n```json\n{body}\n```\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"cliff": False}, "posted again"),
        ({"last_activity_at": (CUTOFF + timedelta(hours=1)).isoformat()}, "posted again"),
        ({"scan_window_end": (CUTOFF + timedelta(days=5)).isoformat()}, "shorter than"),
    ],
)
def test_drafts_refuse_records_that_resumed_or_are_too_short(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    path = evidence_path("reddit-archive", b64e("some_author"))
    record = _record(status="verified_inauthentic", evidence=path, **changes)
    if "scan_window_end" in changes:
        record.update(last_scanned_at=changes["scan_window_end"])
        record.update(first_scanned_at=changes["scan_window_end"])
        record.update(status_updated_at=changes["scan_window_end"])
    root, out = tmp_path / "registry", tmp_path / "drafts"
    _write_registry(root, record, {"facts": {"last_activity_at": record["last_activity_at"]}})
    refused: list[Any] = []
    drafts = build_drafts(
        root,
        "verified_inauthentic",
        "reddit-archive",
        out,
        event_id=EVENT.id,
        events=EVENTS,
        refused=refused,
    )
    assert drafts == [] and not out.exists()
    assert len(refused) == 1 and any(reason in r for r in refused[0].reasons)


def test_a_draft_needs_evidence_of_the_current_facts(tmp_path: Path) -> None:
    path = evidence_path("reddit-archive", b64e("some_author"))
    record = _record(status="verified_inauthentic", evidence=path)
    root, out = tmp_path / "registry", tmp_path / "drafts"
    stale = {"facts": {"last_activity_at": (CUTOFF - timedelta(days=4)).isoformat()}}
    _write_registry(root, record, stale)
    refused: list[Any] = []
    kwargs: dict[str, Any] = {"event_id": EVENT.id, "events": EVENTS, "refused": refused}
    assert build_drafts(root, "verified_inauthentic", "reddit-archive", out, **kwargs) == []
    assert "older last activity" in refused[0].reasons[0]
    _write_registry(root, record, {"facts": {"last_activity_at": record["last_activity_at"]}})
    refused.clear()
    drafts = build_drafts(root, "verified_inauthentic", "reddit-archive", out, **kwargs)
    assert len(drafts) == 1 and refused == []


# -- the wording ------------------------------------------------------------------


def test_drafts_state_observations_not_conclusions() -> None:
    evidence = {
        "cluster": {"size": 4, "sync_minutes": 30},
        "steady": {
            "baseline_days": 30,
            "recent_posts": 9,
            "recent_days": 7,
            "weekly_cv": 0.12,
        },
        "placebo": {
            "headline": "ignored: [a link](https://example.invalid)",
            "rates": {
                "cliff": {
                    "real": 0.14,
                    "placebo_mean": 0.11,
                    "placebo_sd": 0.03,
                    "excess": 0.03,
                    "excess_low": -0.05,
                    "excess_high": 0.11,
                }
            },
        },
        "live_profile": {"status": L.ARCHIVE_ONLY, "checked_at": None},
    }
    text = render_reddit(_record(status="verified_inauthentic"), evidence, EVENT)
    lowered = text.lower()
    assert "coordinated inauthentic network" not in lowered
    assert "p-value" not in lowered
    assert "data center strikes" not in lowered.replace("`october 2026", "")
    assert "stopped posting at" in text and "together with 3 similar accounts" in text
    assert "posted on 28 of the 30 baseline days (90 posts or comments)" in text
    assert "9 of them in the last 7 days" in text
    assert "reported outage" in text
    assert "cliff rate 14% vs placebo 11% ± 3% (excess +3 points" in text
    assert "NOT CLEARLY ABOVE THE PLACEBO" in text and "example.invalid" not in text
    assert "not clearly higher than in ordinary earlier weeks" in text
    assert f"Live profile status: {L.ARCHIVE_ONLY}" in text


def test_the_inauthentic_box_needs_a_finding_beyond_the_silence() -> None:
    blank = "\n".join(
        [
            "- [x] The behavior is inauthentic, not a person who stopped posting.",
            "- Live profile checked: not checked",
            "- Content evidence: ",
            "- Network evidence: ",
            "- Profile evidence: ",
            "- Other evidence: ",
        ]
    )
    problems = review_problems(blank)
    assert any("live profile" in p for p in problems)
    assert any("beyond the silence" in p for p in problems)
    filled = blank.replace("not checked", "active").replace(
        "Content evidence: ", "Content evidence: the same text as 40 other accounts"
    )
    assert review_problems(filled) == []
    unticked = filled.replace("[x]", "[ ]")
    assert review_problems(unticked) == ['the box "The behavior is inauthentic" is not ticked']


def test_placebo_samples_are_discovered_and_fetched_in_moved_windows(tmp_path: Path) -> None:
    from bot_farm_silence_tracker.pipeline import ScanSettings, run_scan
    from bot_farm_silence_tracker.seeds import SeedSet
    from bot_farm_silence_tracker.sources.base import SourceConfig
    from bot_farm_silence_tracker.sources.fake import FakeSource
    from bot_farm_silence_tracker.storage import Storage

    windows: list[tuple[datetime, datetime]] = []
    discovered: list[tuple[datetime, datetime | None, int | None]] = []

    class Recording(FakeSource):
        def discover(self, seeds: SeedSet, pool_limit: int) -> Any:
            discovered.append((self.config.cutoff, self.config.baseline_start, self.config.seed))
            return super().discover(seeds, pool_limit)

        def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list:
            windows.append((since, until))
            return super().fetch_activity(account, since, until)

    for weeks in (3, 0):
        windows.clear()
        discovered.clear()
        params = ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=weeks)
        http = HttpClient(requests_per_second=1000, burst=1000, sleep=lambda _s: None)
        source = Recording(
            SourceConfig(
                http=http,
                credentials={},
                now=NOW,
                cutoff=CUTOFF,
                seed=1,
                baseline_start=params.baseline_start,
            )
        )
        with Storage(tmp_path / f"c{weeks}.db") as storage:
            outcome = run_scan(
                source,
                SeedSet("fake", ("a",)),
                storage,
                ScanSettings(platform="fake", scoring=params, n=40, seed=1),
            )
        expected = {
            (params.baseline_start - timedelta(weeks=k), NOW - timedelta(weeks=k))
            for k in range(weeks + 1)
        }
        assert set(windows) == expected
        # One discovery per pass, each with its cutoff, baseline and seed moved.
        assert [d[0] for d in discovered] == [CUTOFF - timedelta(weeks=k) for k in range(weeks + 1)]
        assert [d[1] for d in discovered] == [
            params.baseline_start - timedelta(weeks=k) for k in range(weeks + 1)
        ]
        assert [d[2] for d in discovered] == [1] + [
            C.placebo_seed(1, k) for k in range(1, weeks + 1)
        ]
        assert source.config.cutoff == CUTOFF  # Restored after the placebo samples.
        assert outcome.placebo is not None
        assert outcome.placebo.available is (weeks == 3)
        assert outcome.provenance["seed"] == 1
        if weeks:
            assert outcome.placebo.requests > 0
            assert all(run.accounts + run.excluded == 40 for run in outcome.placebo.placebos)


def test_two_phase_runs_have_no_placebo_and_fetch_only_the_baseline(tmp_path: Path) -> None:
    from bot_farm_silence_tracker.pipeline import ScanSettings, run_scan
    from bot_farm_silence_tracker.seeds import SeedSet
    from bot_farm_silence_tracker.sources.base import SourceConfig
    from bot_farm_silence_tracker.sources.fake import FakeSource
    from bot_farm_silence_tracker.storage import Storage

    http = HttpClient(requests_per_second=1000, burst=1000, sleep=lambda _s: None)
    source = FakeSource(SourceConfig(http=http, credentials={}, now=NOW, cutoff=CUTOFF))
    settings = ScanSettings(
        platform="fake",
        scoring=ScoringParams(cutoff=CUTOFF, now=NOW, placebo_weeks=3),
        n=40,
        seed=1,
        baseline_sample_rate=0.5,
    )
    with Storage(tmp_path / "c.db") as storage:
        outcome = run_scan(source, SeedSet("fake", ("a",)), storage, settings)
    assert outcome.params.placebo_weeks == 0
    assert outcome.placebo is not None and not outcome.placebo.available
    assert "two-phase" in (outcome.placebo.unavailable or "")
    assert any("Placebo controls are off for two-phase runs" in w for w in outcome.warnings)
