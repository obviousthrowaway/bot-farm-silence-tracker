"""``--concurrency``: several accounts fetched at once behind one shared pacer.

Covers the thread-safe pacer and budget of the shared HTTP client, the global pause
on a load signal, the per-thread state of the reddit-archive adapter, and the
concurrent fetch loop of the pipeline (same results as the sequential loop, exact
``--max-requests``, clean cancellation). Everything runs against the scripted fake
archive; no network.
"""

from __future__ import annotations

import itertools
import json
import random
import threading
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot_farm_silence_tracker import pipeline
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.pipeline import ScanSettings, run_scan
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import SourceConfig
from bot_farm_silence_tracker.sources.errors import (
    BudgetExceeded,
    FetchCancelled,
    RateLimited,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import HttpClient, TokenBucket
from bot_farm_silence_tracker.sources.reddit_archive import RedditArchiveSource, SlowQueryError
from bot_farm_silence_tracker.storage import SAMPLE_FETCHED, SAMPLE_PENDING, Storage

from .helpers import CUTOFF, NOW, MidRandom
from .test_reddit_archive import (
    LOAD,
    Archive,
    build_world,
    comment,
    fetch,
    post_pulse,
    pulse,
    runner,
    slow,
)


class VirtualClock:
    """A shared monotonic clock that only the injected sleep moves forward."""

    def __init__(self) -> None:
        self.now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += max(0.0, seconds)


def make(
    archive: Any,
    *,
    concurrency: int = 2,
    clock: Any = None,
    sleep: Any = None,
    rps: float = RedditArchiveSource.default_rps,
    max_requests: int | None = None,
    max_retries: int = 4,
    **options: object,
) -> tuple[RedditArchiveSource, list[float]]:
    """A reddit-archive source built for ``concurrency`` workers, and its HTTP sleeps."""
    options.setdefault("coverage_threshold", "0")
    sleeps: list[float] = []
    lock = threading.Lock()

    def record(seconds: float) -> None:
        with lock:
            sleeps.append(seconds)
        if sleep is not None:
            sleep(seconds)

    http = HttpClient(
        requests_per_second=rps,
        burst=RedditArchiveSource.default_burst,
        transport=httpx.MockTransport(archive),
        sleep=record,
        clock=clock or (lambda: 0.0),
        max_requests=max_requests,
        max_retries=max_retries,
    )
    config = SourceConfig(
        http=http,
        credentials={},
        now=NOW,
        cutoff=CUTOFF,
        seed=1,
        options=options,
        baseline_start=CUTOFF - timedelta(days=2),
        concurrency=concurrency,
    )
    return RedditArchiveSource(config, rng=MidRandom()), sleeps


def many_authors(count: int, per_author: int = 3) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [pulse(), post_pulse()]
    for a in range(count):
        for i in range(per_author):
            when = CUTOFF - timedelta(hours=3 * i + a % 7 + 1)
            items.append(comment(f"u{a}-{i}", f"user{a}", "worldnews", when, body=f"{a}/{i}"))
    return items


def run_threads(count: int, target: Any) -> list[BaseException]:
    """Run ``target(i)`` in ``count`` threads; return the exceptions they raised."""
    errors: list[BaseException] = []
    lock = threading.Lock()

    def body(i: int) -> None:
        try:
            target(i)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=body, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive(), "a worker thread hung"
    return errors


# -- the shared pacer ----------------------------------------------------------------


@pytest.mark.parametrize(("workers", "rate"), [(1, 0.5), (2, 0.5), (3, 0.5), (3, 1.0)])
def test_the_pacer_keeps_the_total_rate_with_many_threads(workers: int, rate: float) -> None:
    clock = VirtualClock()
    bucket = TokenBucket(rate, 1.0, clock=clock, sleep=clock.sleep)
    grants: list[float] = []
    log_lock = threading.Lock()

    def take(_: int) -> None:
        for _ in range(15):
            # Log the grant time before any other caller can sleep the clock forward.
            with log_lock:
                bucket.acquire()
                grants.append(clock())

    assert run_threads(workers, take) == []
    assert len(grants) == 15 * workers
    gaps = [b - a for a, b in itertools.pairwise(grants)]
    assert min(gaps) >= 1 / rate - 1e-9
    # The total rate is the pacer's, not the pacer's times the number of workers.
    assert (len(grants) - 1) / (grants[-1] - grants[0]) <= rate + 1e-9


def test_the_pacer_serializes_real_threads_at_its_rate() -> None:
    bucket = TokenBucket(50.0, 1.0)  # Real clock and sleep: 20 ms per token.
    stamps: list[float] = []
    lock = threading.Lock()

    def take(_: int) -> None:
        for _ in range(10):
            bucket.acquire()
            with lock:
                stamps.append(time.monotonic())

    started = time.monotonic()
    assert run_threads(3, take) == []
    # 30 tokens at 50 per second (the first one free) take at least 29 intervals.
    assert time.monotonic() - started >= 29 / 50 - 0.01


def test_a_pause_reaches_a_caller_already_waiting_for_its_token() -> None:
    clock = VirtualClock()
    entered, release = threading.Event(), threading.Event()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            entered.set()
            assert release.wait(5)
        clock.sleep(seconds)

    bucket = TokenBucket(0.5, 1.0, clock=clock, sleep=sleep)
    bucket.acquire()  # The free token; the next caller waits 2 s for its own.
    waiter = threading.Thread(target=bucket.acquire)
    waiter.start()
    assert entered.wait(5)
    bucket.hold_until(30.0)  # A load signal while the caller sleeps for its token.
    release.set()
    waiter.join(5)
    assert not waiter.is_alive()
    # Token wait, the rest of the pause, then a full interval: the bucket is empty
    # when the pause ends, so the first token after it is not free.
    assert sleeps == [2.0, 28.0, 2.0]
    assert clock() == 32.0


def test_a_pause_is_waited_once_even_when_the_clock_does_not_move() -> None:
    sleeps: list[float] = []
    bucket = TokenBucket(0.5, 1.0, clock=lambda: 0.0, sleep=sleeps.append)
    bucket.hold_until(12.0)
    assert bucket.acquire() == 12.0
    assert bucket.acquire() == 12.0
    # Each call waits the pause once, then a full token interval (the bucket is empty
    # after a pause); no loop on a frozen test clock.
    assert sleeps == [12.0, 2.0, 12.0, 2.0]


# -- the shared budget and cancellation -----------------------------------------------


def test_max_requests_is_exact_with_many_threads() -> None:
    archive = Archive(many_authors(40))
    src, _ = make(archive, concurrency=3, max_requests=37)
    accounts = iter(range(40))
    lock = threading.Lock()

    def work(_: int) -> None:
        while True:
            with lock:
                index = next(accounts, None)
            if index is None:
                return
            try:
                fetch(src, f"user{index}")
            except BudgetExceeded:
                return

    assert run_threads(3, work) == []
    assert len(archive.requests) == 37
    assert src.http.requests_made == 37
    assert src.http.requests_remaining == 0
    assert src.request_summary()["requests"] == 37  # type: ignore[index]


def test_spend_never_passes_the_budget_from_many_threads() -> None:
    http = HttpClient(max_requests=500, transport=httpx.MockTransport(lambda r: None))  # type: ignore[arg-type,return-value]

    def spend(_: int) -> None:
        for _ in range(400):
            try:
                http.spend()
            except BudgetExceeded:
                return

    assert run_threads(4, spend) == []
    assert http.requests_made == 500


def test_cancel_ends_waits_and_attempts_and_can_be_reset() -> None:
    http = HttpClient(requests_per_second=0.01, unrestricted=True, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})
    ))  # fmt: skip
    http.get("https://example.invalid/a")  # The free token.
    started = time.monotonic()
    result: list[BaseException] = []

    def blocked() -> None:
        try:
            http.get("https://example.invalid/b")  # Would wait 100 s for its token.
        except BaseException as exc:
            result.append(exc)

    worker = threading.Thread(target=blocked)
    worker.start()
    time.sleep(0.05)
    http.cancel()
    worker.join(5)
    assert not worker.is_alive()
    assert time.monotonic() - started < 5
    assert len(result) == 1 and isinstance(result[0], FetchCancelled)
    assert not isinstance(result[0], SourceError)  # Never mistaken for an account error.
    with pytest.raises(FetchCancelled):
        http.get("https://example.invalid/c")
    assert http.requests_made == 1
    http.reset_cancel()
    assert not http.cancelled


# -- the reddit-archive adapter under threads ----------------------------------------


def test_concurrency_is_validated() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        make(Archive([]), concurrency=4)
    with pytest.raises(ValueError, match="concurrency"):
        make(Archive([]), concurrency=0)


@pytest.mark.parametrize("value", ["9", "1"])
def test_history_limit_below_ten_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="history_limit"):
        make(Archive([]), concurrency=1, history_limit=value)


def remaining_zero() -> httpx.Response:
    return httpx.Response(
        200, json={"data": []}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "20"}
    )


LOAD_SIGNALS = {
    # kind: (scripted responses, the pause every worker must wait)
    "429": ([httpx.Response(429, headers={"X-RateLimit-Reset": "7"})], 7 + 1 + 2.5),
    "load422": ([slow(LOAD, reset="9")], 9 + 1 + 2.5),
    "503": ([httpx.Response(503, headers={"Retry-After": "4"})], 4.0),
    "remaining0": ([remaining_zero()], 20 + 1 + 2.5),
}


@pytest.mark.parametrize("kind", sorted(LOAD_SIGNALS))
def test_a_load_signal_pauses_every_worker(kind: str) -> None:
    script, pause = LOAD_SIGNALS[kind]
    archive = Archive([pulse()], script=list(script))
    src, _ = make(archive, concurrency=2, slowdown_wait="0")
    fetch(src, "a")
    assert src.http.paused_until == pytest.approx(pause)
    # Another worker's next request waits the pause in the shared pacer first.
    other: list[float] = []
    src.http._bucket._sleep = other.append  # type: ignore[attr-defined]
    errors = run_threads(1, lambda _: fetch(src, "b"))
    assert errors == []
    assert other and other[0] == pytest.approx(pause)


def test_an_escalated_timeout_pauses_every_worker() -> None:
    archive = Archive([pulse()])
    src, _ = make(archive, concurrency=2, slowdown_wait="1")
    # At K = 2 the guard needs 2 x 3 timeouts of different queries in 60 s.
    for handle in ("a", "b", "c", "e", "f", "g"):
        archive.script = [slow(reset="20")]
        fetch(src, handle)
    assert src.http.paused_until is None  # Plain timeouts pause only their own query.
    archive.script = [slow(reset="20")]
    fetch(src, "d")
    assert src.request_summary()["status_422_escalated"] == 1  # type: ignore[index]
    assert src.http.paused_until == pytest.approx(20 + 1 + 2.5)


@pytest.mark.parametrize("kind", sorted(LOAD_SIGNALS))
def test_a_sequential_scan_never_pauses_the_pacer(kind: str) -> None:
    script, _ = LOAD_SIGNALS[kind]
    archive = Archive([pulse()], script=list(script))
    src, _ = make(archive, concurrency=1, slowdown_wait="0")
    fetch(src, "a")
    assert src.http.paused_until is None


def test_the_escalation_guard_counts_each_threads_query_once() -> None:
    """Two workers each time out twice on their own query: two counts, not four."""
    seen: Counter[str] = Counter()
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    def hook(q: dict[str, str]) -> httpx.Response | None:
        author = q.get("author")
        if author is None or "title" not in q.get("fields", ""):
            return None  # Only each author's posts query times out.
        with lock:
            seen[author] += 1
            count = seen[author]
        if count == 1:
            barrier.wait()  # Both workers are inside their first query at once.
        return slow() if count <= 2 else None

    archive = Archive([pulse()], hook=hook)
    src, _ = make(archive, concurrency=2, slowdown_wait="0")
    assert run_threads(2, lambda i: fetch(src, f"w{i}")) == []
    assert src.request_summary()["status_422_timeout"] == 4  # type: ignore[index]
    assert src.request_summary()["status_422_escalated"] == 0  # type: ignore[index]
    assert len(src._timeouts) == 2
    assert sum(1 for _, status in src._recent if status == 422) == 2


def test_rows_are_tagged_with_each_threads_phase() -> None:
    archive = Archive(many_authors(6))
    src, _ = make(archive, concurrency=3)
    assert run_threads(3, lambda i: [fetch(src, f"user{i}"), fetch(src, f"user{i + 3}")]) == []
    rows = src.drain_request_log(final=True)
    assert len(rows) == len(archive.requests)
    assert {row["phase"] for row in rows} == {"fetch"}


def test_stress_many_accounts_on_three_threads_match_one_thread() -> None:
    items = many_authors(60, per_author=4)
    rng = random.Random(7)
    lock = threading.Lock()

    def jittery(q: dict[str, str]) -> httpx.Response | None:
        with lock:
            delay, timeout = rng.random() / 500, rng.random() < 0.05
        time.sleep(delay)
        return slow() if timeout and "author" in q else None

    single, _ = make(Archive(items), concurrency=1, slowdown_wait="0")
    expected = {f"user{i}": sorted(a.content_hash or "" for a in fetch(single, f"user{i}")[1])
                for i in range(60)}  # fmt: skip

    archive = Archive(items, hook=jittery)
    src, _ = make(archive, concurrency=3, slowdown_wait="0", max_slow_queries="1000")
    got: dict[str, list[str]] = {}
    queue = iter(range(60))

    def work(_: int) -> None:
        while True:
            with lock:
                index = next(queue, None)
            if index is None:
                return
            handle = f"user{index}"
            activities = fetch(src, handle)[1]
            with lock:
                got[handle] = sorted(a.content_hash or "" for a in activities)

    assert run_threads(3, work) == []
    assert got == expected
    summary = src.request_summary()
    assert summary is not None
    rows = src.drain_request_log(final=True)
    assert summary["requests"] == len(archive.requests) == len(rows)
    assert summary["status_422"] == sum(1 for r in rows if r["status"] == 422)


# -- the concurrent fetch loop -------------------------------------------------------


def patch_client(monkeypatch: pytest.MonkeyPatch, archive: Archive) -> list[HttpClient]:
    clients: list[HttpClient] = []

    def patched_client(**kwargs: Any) -> HttpClient:
        client = HttpClient(
            **kwargs,
            transport=httpx.MockTransport(archive),
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )
        clients.append(client)
        return client

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", patched_client)
    return clients


def cli_scan(tmp_path: Path, name: str, *extra: str) -> dict[str, Any]:
    out = tmp_path / f"{name}.json"
    result = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--subreddit", "worldnews", "--n", "12",
            "--pool-factor", "1", "--seed", "3", "--now", "2026-10-14T00:00:00Z",
            "--db", str(tmp_path / f"{name}.db"), "--out", str(out), "--max-requests", "500",
            *extra,
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    return json.loads(out.read_text())


def comparable(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload without the timing and request totals, which depend on the run."""
    summary = {k: v for k, v in payload["summary"].items() if k != "requests"}
    return {**payload, "summary": summary}


@pytest.mark.parametrize("workers", ["2", "3"])
def test_concurrent_scans_give_the_sequential_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: str
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    sequential = cli_scan(tmp_path, "one")
    clients = patch_client(monkeypatch, Archive(build_world()))
    concurrent = cli_scan(tmp_path, "many", "--concurrency", workers)
    assert comparable(concurrent) == comparable(sequential)
    assert {c["user_id"] for c in concurrent["candidates"]} == {f"bot_acct_{i}" for i in range(6)}
    assert not clients[0].cancelled
    with Storage(tmp_path / "many.db") as storage:
        run = storage.get_run(1)
        assert run is not None
        assert run["params"]["concurrency"] == int(workers)
        assert {s for _, s in storage.get_run_sample(1)} == {SAMPLE_FETCHED}
        assert len(storage.get_request_log(1)) == clients[0].requests_made


def test_concurrency_one_runs_the_sequential_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the concurrent loop must not run with --concurrency 1")

    monkeypatch.setattr(pipeline, "_ConcurrentFetch", refuse)
    patch_client(monkeypatch, Archive(build_world()))
    cli_scan(tmp_path, "one", "--concurrency", "1")
    with Storage(tmp_path / "one.db") as storage:
        run = storage.get_run(1)
        assert run is not None
        assert "concurrency" not in run["params"]  # Run params are unchanged at K=1.


def test_cli_refuses_concurrency_the_platform_does_not_allow(tmp_path: Path) -> None:
    db = str(tmp_path / "c.db")
    result = runner.invoke(
        app, ["scan", "--platform", "reddit-archive", "--concurrency", "4", "--db", db]
    )
    assert result.exit_code != 0
    assert "at most 3" in result.output
    result = runner.invoke(app, ["scan", "--platform", "fake", "--concurrency", "0", "--db", db])
    assert result.exit_code != 0


def test_the_fake_platform_gives_the_same_results_at_any_concurrency(tmp_path: Path) -> None:
    payloads = []
    for workers in ("1", "3"):
        out = tmp_path / f"fake{workers}.json"
        result = runner.invoke(
            app,
            [
                "scan", "--platform", "fake", "--n", "120", "--seed", "5",
                "--cutoff", "2026-10-01T00:00:00Z", "--now", "2026-10-11T00:00:00Z",
                "--db", str(tmp_path / f"fake{workers}.db"), "--out", str(out),
                "--concurrency", workers,
            ],
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        payloads.append(json.loads(out.read_text()))
    assert payloads[0] == payloads[1]


def scan_settings(concurrency: int, **kwargs: Any) -> ScanSettings:
    scoring = ScoringParams(cutoff=CUTOFF, now=NOW, baseline_days=2)
    return ScanSettings(
        platform="reddit-archive", scoring=scoring, n=12, pool_factor=1.0, seed=3,
        concurrency=concurrency, **kwargs,
    )  # fmt: skip


def archive_source(archive: Archive, concurrency: int, **http: Any) -> RedditArchiveSource:
    http.setdefault("sleep", lambda _s: None)
    http.setdefault("clock", lambda: 0.0)
    client = HttpClient(
        requests_per_second=RedditArchiveSource.default_rps,
        transport=httpx.MockTransport(archive),
        **http,
    )
    config = SourceConfig(
        http=client, credentials={}, now=NOW, cutoff=CUTOFF, seed=3,
        options={"coverage_threshold": "0"}, baseline_start=CUTOFF - timedelta(days=2),
        concurrency=concurrency,
    )  # fmt: skip
    return RedditArchiveSource(config, rng=MidRandom())


SEEDS = SeedSet(platform="reddit-archive", items=("worldnews",))


@pytest.mark.parametrize("workers", [1, 2, 3])
def test_max_requests_is_exact_in_a_concurrent_scan(tmp_path: Path, workers: int) -> None:
    full = Archive(build_world())
    with Storage(tmp_path / "full.db") as storage:
        run_scan(archive_source(full, 1), SEEDS, storage, scan_settings(1))
    cap = len(full.requests) - 7  # Runs out while several accounts are still left.
    archive = Archive(build_world())
    source = archive_source(archive, workers, max_requests=cap)
    with Storage(tmp_path / "c.db") as storage:
        outcome = run_scan(source, SEEDS, storage, scan_settings(workers, max_requests=cap))
        assert outcome.status == "partial"
        assert len(archive.requests) == cap == source.http.requests_made
        assert len(storage.get_request_log(outcome.run_id)) == cap
        statuses = Counter(s for _, s in storage.get_run_sample(outcome.run_id))
        assert statuses[SAMPLE_PENDING] > 0
        assert statuses[SAMPLE_FETCHED] == len(outcome.results)
        assert not source.http.cancelled  # Cancelled to stop the workers, then reset.


def test_ctrl_c_cancels_the_workers_and_keeps_the_finished_accounts(tmp_path: Path) -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        if "author" in q:
            time.sleep(0.05)  # Keep the workers busy when Ctrl-C hits.
        return None

    archive = Archive(build_world(), hook=hook)
    source = archive_source(archive, 2)
    started: list[int] = []

    def progress(done: int, total: int) -> None:
        started.append(done)
        if done == 5:
            raise KeyboardInterrupt

    begun = time.monotonic()
    with Storage(tmp_path / "c.db") as storage:
        with pytest.raises(KeyboardInterrupt):
            run_scan(source, SEEDS, storage, scan_settings(2), progress)
        assert time.monotonic() - begun < 10
        run = storage.get_run(1)
        assert run is not None and run["status"] == "cancelled"
        sample = storage.get_run_sample(1)
        statuses = Counter(s for _, s in sample)
        # Accounts 1-4 were started; account 5 never was. Those not finished stay
        # pending for a resume; none is marked fetched without its results.
        assert statuses[SAMPLE_PENDING] >= len(sample) - 4
        fetched = [a.user_id for a, s in sample if s == SAMPLE_FETCHED]
        assert all(storage.activity_window("reddit-archive", uid) for uid in fetched)
        # Every request made, the cancelled workers' included, reached the request log.
        assert len(storage.get_request_log(1)) == len(archive.requests)
    assert not source.http.cancelled


def test_a_fatal_error_stops_every_worker_and_resume_finishes(tmp_path: Path) -> None:
    world = build_world()

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("author") == "human_acct_2":
            # A reset longer than the cap: RateLimited stops the scan.
            return httpx.Response(429, headers={"X-RateLimit-Reset": "5000"})
        return None

    archive = Archive(world, hook=hook)
    source = archive_source(archive, 3)
    with Storage(tmp_path / "c.db") as storage:
        outcome = run_scan(source, SEEDS, storage, scan_settings(3))
        assert outcome.status == "partial"
        assert any("rerun with --from-run" in w for w in outcome.warnings)
        sample = storage.get_run_sample(outcome.run_id)
        failed = [s for a, s in sample if a.user_id == "human_acct_2"]
        assert failed == [SAMPLE_PENDING]

        # The resume fetches only what is left, concurrently, and completes.
        archive.hook = None
        archive.requests.clear()
        resumed = run_scan(
            archive_source(archive, 3), SEEDS, storage, scan_settings(3), from_run=outcome.run_id
        )
        assert resumed.status == "complete"
        authors = {r.url.params["author"] for r in archive.requests if "author" in r.url.params}
        assert "human_acct_2" in authors
        done = {a.user_id for a, s in sample if s == SAMPLE_FETCHED}
        assert not authors & done  # Accounts the first run fetched are not fetched again.
        assert len(resumed.results) == 12


def test_the_error_limit_aborts_a_concurrent_scan(tmp_path: Path) -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return httpx.Response(400, json={"error": "nope"}) if "author" in q else None

    archive = Archive(build_world(), hook=hook)
    source = archive_source(archive, 2)
    with Storage(tmp_path / "c.db") as storage:
        outcome = run_scan(source, SEEDS, storage, scan_settings(2, max_consecutive_errors=3))
        assert outcome.status == "partial"
        assert any("Aborted after 3 consecutive account errors" in w for w in outcome.warnings)
        statuses = [s for _, s in storage.get_run_sample(outcome.run_id)]
        # Recorded in sample order: exactly the first three are errors.
        assert statuses[:3] == ["error"] * 3
        assert set(statuses[3:]) <= {SAMPLE_PENDING}


def test_results_are_recorded_in_sample_order_whatever_finishes_first(tmp_path: Path) -> None:
    order: list[str] = []
    world = build_world()

    def hook(q: dict[str, str]) -> httpx.Response | None:
        author = q.get("author")
        if author is not None and author.startswith("bot_acct_0"):
            time.sleep(0.2)  # The first sampled accounts may be the slowest ones.
        return None

    archive = Archive(world, hook=hook)
    source = archive_source(archive, 3)
    original = pipeline._record_fetch

    def spy(storage: Storage, account: Account, *args: Any, **kwargs: Any) -> Any:
        order.append(account.user_id)
        return original(storage, account, *args, **kwargs)

    with Storage(tmp_path / "c.db") as storage, pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "_record_fetch", spy)
        outcome = run_scan(source, SEEDS, storage, scan_settings(3))
        sample = [a.user_id for a, _ in storage.get_run_sample(outcome.run_id)]
    assert order == sample


# -- review round: fatal errors, pauses, guard scaling, ordering --------------------


def sampled_handles(tmp_path: Path) -> list[str]:
    """The handles of the scan's sample, in sample order (the seed fixes it)."""
    with Storage(tmp_path / "order.db") as storage:
        outcome = run_scan(archive_source(Archive(build_world()), 1), SEEDS, storage,
                           scan_settings(1))  # fmt: skip
        return [a.handle for a, _ in storage.get_run_sample(outcome.run_id)]


def test_a_fatal_error_on_a_later_account_stops_the_others_at_once(tmp_path: Path) -> None:
    """The head account is slow; the second one hits a fatal 429. Nothing else is sent."""
    head, fatal = sampled_handles(tmp_path)[:2]
    clock = VirtualClock()
    head_entered, cancelled = threading.Event(), threading.Event()
    lock = threading.Lock()
    sent: list[str] = []

    def hook(q: dict[str, str]) -> httpx.Response | None:
        author = q.get("author")
        if author is None:
            return None
        with lock:
            sent.append(author)
        if author == head and not head_entered.is_set():
            head_entered.set()
            assert cancelled.wait(5), "the fatal error did not cancel the client"
            return None
        if author == fatal:
            assert head_entered.wait(5)
            return httpx.Response(429, headers={"X-RateLimit-Reset": "5000"})
        return None

    archive = Archive(build_world(), hook=hook)
    source = archive_source(archive, 2, clock=clock, sleep=clock.sleep)
    original = source.http.cancel

    def cancel() -> None:
        original()
        cancelled.set()

    source.http.cancel = cancel  # type: ignore[method-assign]
    with Storage(tmp_path / "c.db") as storage:
        outcome = run_scan(source, SEEDS, storage, scan_settings(2))
        assert outcome.status == "partial"
        assert any("rate" in w.lower() and "--from-run" in w for w in outcome.warnings)
        statuses = {a.handle: s for a, s in storage.get_run_sample(outcome.run_id)}
    # The fatal request is the last author request: the head's next one was cancelled
    # and no further account was started.
    assert sent[-1] == fatal
    assert sent.count(head) == 1
    assert set(sent) == {head, fatal}
    assert statuses[head] == SAMPLE_PENDING  # Cut short: a resume fetches it again.
    assert statuses[fatal] == SAMPLE_PENDING
    assert not source.http.cancelled


def test_a_worker_bug_is_raised_and_stops_the_others(tmp_path: Path) -> None:
    head, broken = sampled_handles(tmp_path)[:2]
    head_entered, cancelled = threading.Event(), threading.Event()
    sent: list[str] = []

    def hook(q: dict[str, str]) -> httpx.Response | None:
        author = q.get("author")
        if author is None:
            return None
        sent.append(author)
        if author == head and not head_entered.is_set():
            head_entered.set()
            assert cancelled.wait(5)
        elif author == broken:
            assert head_entered.wait(5)
            raise ZeroDivisionError("a bug in the adapter")
        return None

    archive = Archive(build_world(), hook=hook)
    source = archive_source(archive, 2)
    original = source.http.cancel

    def cancel() -> None:
        original()
        cancelled.set()

    source.http.cancel = cancel  # type: ignore[method-assign]
    with Storage(tmp_path / "c.db") as storage:
        with pytest.raises(ZeroDivisionError):  # The bug itself, not FetchCancelled.
            run_scan(source, SEEDS, storage, scan_settings(2))
        run = storage.get_run(1)
        assert run is not None and run["status"] == "failed"
    assert set(sent) == {head, broken}


def test_a_429_the_client_gives_up_on_still_pauses_every_worker() -> None:
    # Over the cap: the client raises RateLimited at once.
    huge = httpx.Response(429, headers={"X-RateLimit-Reset": "5000"})
    archive = Archive([pulse()], script=[huge])
    src, _ = make(archive, concurrency=2)
    with pytest.raises(RateLimited):
        fetch(src, "a")
    assert src.http.paused_until == pytest.approx(5000)
    # Retries exhausted: the last attempt's 429 pauses the others too.
    archive = Archive([pulse()], script=[httpx.Response(429, headers={"X-RateLimit-Reset": "7"})])
    src, _ = make(archive, concurrency=2, max_retries=0)
    with pytest.raises(RateLimited, match="retries exhausted"):
        fetch(src, "a")
    assert src.http.paused_until == pytest.approx(7 + 1 + 2.5)
    # A sequential scan still never pauses the pacer.
    archive = Archive([pulse()], script=[httpx.Response(429, headers={"X-RateLimit-Reset": "7"})])
    src, _ = make(archive, concurrency=1, max_retries=0)
    with pytest.raises(RateLimited):
        fetch(src, "a")
    assert src.http.paused_until is None


def capture(fn: Any, *args: Any) -> BaseException | None:
    try:
        fn(*args)
    except BaseException as exc:
        return exc
    return None


def test_max_slow_queries_counts_each_workers_accounts_on_their_own() -> None:
    from concurrent.futures import ThreadPoolExecutor

    def hook(q: dict[str, str]) -> httpx.Response | None:
        author = q.get("author", "")
        return slow() if author.startswith("bad") else None

    archive = Archive([pulse()], hook=hook)
    src, _ = make(archive, concurrency=2, slowdown_wait="0", max_slow_queries="3")
    workers = [ThreadPoolExecutor(1), ThreadPoolExecutor(1)]  # Two distinct threads.
    try:

        def on(worker: int, handle: str) -> BaseException | None:
            return workers[worker].submit(capture, fetch, src, handle).result(30)

        # Three failures in a row overall but two per worker: a shared count would
        # trip at bad2. Worker 1's success resets only worker 1's streak.
        for worker, handle in [(0, "bad0"), (1, "bad1"), (0, "bad2"), (1, "good"), (1, "bad3")]:
            error = on(worker, handle)
            if handle == "good":
                assert error is None
            else:
                assert isinstance(error, SlowQueryError) and not isinstance(error, RateLimited)
        # Worker 0's third failure in a row trips it, as three in a row do at K = 1.
        assert isinstance(on(0, "bad4"), RateLimited)
        error = on(1, "bad5")  # Worker 1 is at two in a row.
        assert isinstance(error, SlowQueryError) and not isinstance(error, RateLimited)
    finally:
        for worker in workers:
            worker.shutdown()


@pytest.mark.parametrize("workers", [1, 2, 3])
def test_the_escalation_guard_scales_with_concurrency(workers: int) -> None:
    archive = Archive([pulse()])
    src, _ = make(archive, concurrency=workers, slowdown_wait="1")
    assert src._recent.maxlen == 10 * workers
    assert src._escalate_recent_422 == 5 * workers
    for i in range(3 * workers):
        archive.script = [slow(reset="20")]
        fetch(src, f"q{i}")
    assert src.request_summary()["status_422_escalated"] == 0  # type: ignore[index]
    archive.script = [slow(reset="20")]
    fetch(src, "last")
    assert src.request_summary()["status_422_escalated"] == 1  # type: ignore[index]


def test_the_recent_422_count_scales_with_concurrency() -> None:
    archive = Archive([pulse()])
    src, _ = make(archive, concurrency=2, slowdown_wait="1")
    # Nine 422s among the last responses (other 422s, not timeouts): below 5 x 2.
    src._recent.extend((0.0, 422) for _ in range(9))
    assert not src._escalate()
    src._recent.append((0.0, 422))
    assert src._escalate()


@pytest.mark.parametrize(
    ("workers", "reads", "escalates"), [(1, 5, True), (1, 4, False), (2, 10, True), (2, 9, False)]
)
def test_read_timeouts_among_the_last_responses_escalate_a_timeout(
    workers: int, reads: int, escalates: bool
) -> None:
    clock = VirtualClock()
    src, _ = make(Archive([pulse()]), concurrency=workers, clock=clock, slowdown_wait="1")
    # `reads` read timeouts (status None) among the last 10 K responses, the rest plain
    # 200s. The 3-in-60-seconds timeout count stays empty, so only the recent-responses
    # count can escalate.
    total = 10 * workers
    for i in range(total):
        src._recent.append((clock() + i * 0.001, None if i < reads else 200))
    assert not src._timeouts
    assert src._escalate() is escalates


def test_the_own_hold_of_a_pause_is_not_counted_as_another_workers() -> None:
    class Ticking:
        """A clock that moves by a millisecond on every read, like a real one."""

        now = 0.0

        def __call__(self) -> float:
            self.now += 0.001
            return self.now

    clock = Ticking()
    src, _ = make(Archive([pulse()]), concurrency=2, clock=clock, slowdown_wait="1")
    src._pause_all(10.0)
    until = src._own_hold
    assert until is not None and src.http.paused_until == until  # One reading, one `until`.
    clock.now += 30.0  # The pacer wait is over.
    assert src._others_pause(10.0) == 0.0


def test_after_a_pause_the_first_token_waits_a_full_interval() -> None:
    clock = VirtualClock()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.sleep(seconds)

    bucket = TokenBucket(0.5, 1.0, clock=clock, sleep=sleep)
    bucket.hold_until(10.0)  # The bucket is full, but the pause must not hand it out.
    assert bucket.acquire() == 10.0
    assert sleeps == [10.0, 2.0]
    assert clock() == 12.0
    assert bucket.acquire() == 0.0  # No pause left: just the interval.
    assert sleeps == [10.0, 2.0, 2.0]


def test_time_paused_counts_as_waiting() -> None:
    clock = VirtualClock()
    holder: list[RedditArchiveSource] = []

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("author") == "a" and "title" in q.get("fields", ""):
            holder[0].http.hold(15)  # Another worker's load signal, during a's posts.
        return None

    archive = Archive([pulse()], hook=hook)
    src, _ = make(archive, concurrency=2, clock=clock, sleep=clock.sleep)
    holder.append(src)
    fetch(src, "a")
    rows = src.drain_request_log(final=True)
    assert [r["endpoint"] for r in rows] == ["posts", "comments"]
    # The pause delayed a's comments request: it is the wait after the posts row.
    assert rows[0]["wait_s"] == 15.0 and rows[1]["wait_s"] is None
    assert src.request_summary()["pause_wait_s"] == 15.0  # type: ignore[index]
    # A pause before an account's first request lands on that request's own row.
    src.http.hold(12)
    fetch(src, "b")
    rows = src.drain_request_log(final=True)
    assert rows[0]["wait_s"] == pytest.approx(12.0)
    assert src.request_summary()["pause_wait_s"] == pytest.approx(27.0)  # type: ignore[index]


def test_a_pause_uses_up_the_wait_budget_of_a_slow_query() -> None:
    clock = VirtualClock()
    holder: list[RedditArchiveSource] = []
    held: list[bool] = []

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("author") != "c":
            return None
        if not held:
            held.append(True)
            holder[0].http.hold(890)  # Nearly the whole 900 s budget of one query.
        return slow()

    archive = Archive([pulse()], hook=hook)
    src, _ = make(archive, concurrency=2, clock=clock, sleep=clock.sleep, slowdown_wait="1")
    holder.append(src)
    with pytest.raises(SlowQueryError, match="wait budget"):
        fetch(src, "c")


def test_run_scan_refuses_a_concurrency_the_source_cannot_run(tmp_path: Path) -> None:
    with Storage(tmp_path / "c.db") as storage:
        built_for_one = archive_source(Archive(build_world()), 1)
        with pytest.raises(ValueError, match="does not match the 1"):
            run_scan(built_for_one, SEEDS, storage, scan_settings(2))
        built_for_two = archive_source(Archive(build_world()), 2)
        with pytest.raises(ValueError, match="does not match the 2"):
            run_scan(built_for_two, SEEDS, storage, scan_settings(1))
        with pytest.raises(ValueError, match="above the 3"):
            run_scan(built_for_two, SEEDS, storage, scan_settings(4))
        assert storage.get_run(1) is None  # Refused before a run was created.


def test_resume_clamps_an_old_history_limit_to_the_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, Archive(build_world()))
    cli_scan(tmp_path, "old", "--source-option", "history_limit=10")
    db = tmp_path / "old.db"
    with Storage(db) as storage:
        run = storage.get_run(1)
        assert run is not None
        params = run["params"]
        params["source_options"]["history_limit"] = "5"  # Stored by an older version.
        storage.update_run_params(1, params)
    result = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--from-run", "1",
            "--now", "2026-10-14T00:00:00Z", "--db", str(db),
            "--out", str(tmp_path / "resumed.json"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "history_limit=5" in result.output and "uses history_limit=10" in result.output
    with Storage(db) as storage:
        resumed = storage.get_run(2)
        assert resumed is not None
        assert resumed["params"]["source_options"]["history_limit"] == "10"
    # Given explicitly, a limit below the minimum is still refused.
    result = runner.invoke(
        app,
        [
            "scan", "--platform", "reddit-archive", "--from-run", "1", "--force",
            "--now", "2026-10-14T00:00:00Z", "--db", str(db),
            "--source-option", "history_limit=5",
        ],
    )  # fmt: skip
    assert result.exit_code != 0
    assert "history_limit" in result.output


def test_rows_are_drained_in_send_order() -> None:
    a_sent = threading.Event()
    holder: list[RedditArchiveSource] = []

    def hook(q: dict[str, str]) -> httpx.Response | None:
        author, posts = q.get("author"), "title" in q.get("fields", "")
        if author == "a" and posts:
            a_sent.set()
            deadline = time.monotonic() + 5
            while not holder[0]._log and time.monotonic() < deadline:
                time.sleep(0.001)  # Hold a's answer until b's row is recorded.
        elif author == "b" and posts:
            assert a_sent.wait(5)
        return None

    archive = Archive([pulse()], hook=hook)
    src, _ = make(archive, concurrency=2)
    holder.append(src)
    assert run_threads(2, lambda i: fetch(src, "ab"[i])) == []
    recorded = [row["send_seq"] for row in src._log]
    assert recorded[0] == 2  # b's posts answer was recorded before a's.
    rows = src.drain_request_log(final=True)
    assert [row["send_seq"] for row in rows] == [1, 2, 3, 4]
    with Storage(":memory:") as storage:
        storage.save_request_log(1, "reddit-archive", rows)
        assert [r["send_seq"] for r in storage.get_request_log(1)] == [1, 2, 3, 4]


def test_an_old_request_log_table_gets_the_send_seq_column(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE request_log (run_id INTEGER NOT NULL, seq INTEGER NOT NULL, "
            "platform TEXT NOT NULL, at TEXT NOT NULL, phase TEXT, endpoint TEXT, "
            "status INTEGER, latency_ms REAL, page_limit TEXT, window_s INTEGER, rps REAL, "
            "slow_kind TEXT, attempt INTEGER, PRIMARY KEY (run_id, seq))"
        )
        conn.execute(
            "INSERT INTO request_log (run_id, seq, platform, at) VALUES (1, 0, 'x', 'then')"
        )
    with Storage(path) as storage:
        rows = storage.get_request_log(1)
        assert rows[0]["send_seq"] is None  # Rows from before the column existed.
        storage.save_request_log(1, "x", [{"send_seq": 7}])
        assert [r["send_seq"] for r in storage.get_request_log(1)] == [None, 7]


def test_in_flight_counts_requests_on_the_wire() -> None:
    entered, release = threading.Event(), threading.Event()

    def transport(request: httpx.Request) -> httpx.Response:
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={})

    http = HttpClient(
        transport=httpx.MockTransport(transport), sleep=lambda _s: None, unrestricted=True
    )
    worker = threading.Thread(target=http.get, args=("https://example.invalid/a",))
    worker.start()
    assert entered.wait(5)
    assert http.in_flight == 1
    release.set()
    worker.join(5)
    assert http.in_flight == 0


def test_ctrl_c_says_how_many_requests_it_waits_for(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    entered = threading.Event()

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if "author" in q and not entered.is_set():
            entered.set()
            time.sleep(0.3)  # On the wire when Ctrl-C hits.
        return None

    archive = Archive(build_world(), hook=hook)
    source = archive_source(archive, 2)

    def progress(done: int, total: int) -> None:
        if done == 2:
            assert entered.wait(5)
            raise KeyboardInterrupt

    with (
        Storage(tmp_path / "c.db") as storage,
        caplog.at_level("WARNING", "bot_farm_silence_tracker"),
        pytest.raises(KeyboardInterrupt),
    ):
        run_scan(source, SEEDS, storage, scan_settings(2), progress)
    assert "stopping: waiting for 1 in-flight request" in caplog.text


def test_the_archive_uses_a_short_read_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeout = RedditArchiveSource.default_timeout
    assert isinstance(timeout, httpx.Timeout)
    assert (timeout.connect, timeout.read) == (10.0, 20.0)
    seen: list[Any] = []
    archive = Archive(build_world())

    def patched_client(**kwargs: Any) -> HttpClient:
        seen.append(kwargs.get("timeout"))
        return HttpClient(
            **kwargs, transport=httpx.MockTransport(archive), sleep=lambda _s: None,
            clock=lambda: 0.0,
        )  # fmt: skip

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", patched_client)
    cli_scan(tmp_path, "t")
    assert seen == [timeout]


def test_the_scan_starts_at_max_rps_and_still_backs_off() -> None:
    src, _ = make(Archive([pulse()]), concurrency=2)
    assert src.http.rate[0] == 0.5  # Default unchanged.
    script = [httpx.Response(429, headers={"X-RateLimit-Reset": "1"}) for _ in range(5)]
    archive = Archive([pulse()], script=list(script[:1]))
    src, _ = make(archive, concurrency=2, max_rps="1.0")
    assert src.http.rate[0] == 1.0  # Starts at max_rps, not at the class default.
    assert src.max_rps == 1.0
    fetch(src, "a")
    assert src.http.rate[0] == 0.5  # A load signal halves it from the current rate.
    for i in range(4):
        archive.script = list(script[:1])
        fetch(src, f"b{i}")
    assert src.http.rate[0] == pytest.approx(0.1)  # Never below the floor.
    with pytest.raises(ValueError, match="max_rps"):
        make(Archive([]), max_rps="2.5")  # The ceiling stays 2.


def test_a_429_holds_the_other_workers_next_send_in_a_scan(tmp_path: Path) -> None:
    """run_scan at K = 2 on a virtual clock: one author's 429 pauses the other worker."""
    other, throttled = sampled_handles(tmp_path)[:2]
    clock = VirtualClock()
    sleeps: list[float] = []
    lock = threading.Lock()
    sends: list[tuple[str, float, int]] = []  # (author, clock, status sent back)
    other_entered = threading.Event()
    holder: list[RedditArchiveSource] = []

    def sleep(seconds: float) -> None:
        with lock:
            sleeps.append(seconds)
        clock.sleep(seconds)

    def hook(q: dict[str, str]) -> httpx.Response | None:
        author = q.get("author")
        if author is None:
            return None
        first_429 = author == throttled and not any(a == throttled for a, _, _ in sends)
        with lock:
            sends.append((author, clock(), 429 if first_429 else 200))
        if author == other and not other_entered.is_set():
            other_entered.set()
            deadline = time.monotonic() + 5
            while holder[0].http.paused_until is None and time.monotonic() < deadline:
                time.sleep(0.001)  # On the wire until the 429 has been seen.
            return None
        if first_429:
            assert other_entered.wait(5)
            return httpx.Response(429, headers={"X-RateLimit-Reset": "7"})
        return None

    archive = Archive(build_world(), hook=hook)
    source = archive_source(archive, 2, clock=clock, sleep=sleep)
    holder.append(source)
    with Storage(tmp_path / "c.db") as storage:
        outcome = run_scan(source, SEEDS, storage, scan_settings(2))
        assert outcome.status == "complete"
    at = next(i for i, (_, _, status) in enumerate(sends) if status == 429)
    sent_429 = sends[at][1]
    pause_end = sent_429 + 7 + 1 + 2.5
    assert source.http.paused_until == pytest.approx(pause_end)
    assert 7 + 1 + 2.5 in sleeps  # The throttled worker's own wait for the reset.
    later = [when for author, when, _ in sends[at + 1 :] if author == other]
    assert later, "the other worker sent nothing after the 429"
    assert min(later) >= pause_end - 1e-9  # Held until the pause was over.


# -- review round 2: the real error wins, pauses counted once, read timeouts -------


def test_the_real_error_wins_when_a_cancelled_head_finishes_first(tmp_path: Path) -> None:
    """A later account's fatal 429 cancels the client; the head account's worker
    finishes with FetchCancelled before the fatal worker's future is done. The scan
    still ends partial with the rate-limit error, not with the FetchCancelled."""
    head, fatal = sampled_handles(tmp_path)[:2]
    head_entered, cancelled = threading.Event(), threading.Event()

    def hook(q: dict[str, str]) -> httpx.Response | None:
        author = q.get("author")
        if author is None:
            return None
        if author == head and not head_entered.is_set():
            head_entered.set()
            assert cancelled.wait(5), "the fatal error did not cancel the client"
            return None
        if author == fatal:
            assert head_entered.wait(5)
            return httpx.Response(429, headers={"X-RateLimit-Reset": "5000"})
        return None

    archive = Archive(build_world(), hook=hook)
    source = archive_source(archive, 2)
    original = source.http.cancel

    def cancel() -> None:
        original()
        cancelled.set()
        if threading.current_thread().name.startswith("bot-farm-silence-tracker-fetch"):
            # The fatal worker is descheduled between cancel() and its raise, so the
            # head's FetchCancelled is collected first.
            time.sleep(0.5)

    source.http.cancel = cancel  # type: ignore[method-assign]
    with Storage(tmp_path / "c.db") as storage:
        outcome = run_scan(source, SEEDS, storage, scan_settings(2))
        assert outcome.status == "partial"
        # The rate-limit error itself is what the scan reports.
        assert any("reset longer than the cap" in w and "--from-run" in w for w in outcome.warnings)
        statuses = {a.handle: s for a, s in storage.get_run_sample(outcome.run_id)}
    assert statuses[head] == SAMPLE_PENDING
    assert statuses[fatal] == SAMPLE_PENDING
    assert not source.http.cancelled


def test_a_cancel_with_no_worker_error_raises_fetch_cancelled(tmp_path: Path) -> None:
    archive = Archive(build_world())
    source = archive_source(archive, 2)

    def progress(done: int, total: int) -> None:
        if done == 3:
            source.http.cancel()  # From outside the loop: no worker has an error.

    with Storage(tmp_path / "c.db") as storage:
        with pytest.raises(FetchCancelled):
            run_scan(source, SEEDS, storage, scan_settings(2), progress)
        run = storage.get_run(1)
        assert run is not None and run["status"] == "cancelled"
    assert not source.http.cancelled


@pytest.mark.parametrize("outcome_kind", ["unavailable", "skipped"])
def test_unavailable_and_skipped_accounts_behind_the_head_are_recorded_on_stop(
    tmp_path: Path, outcome_kind: str
) -> None:
    from bot_farm_silence_tracker.sources.base import AccountUnavailable
    from bot_farm_silence_tracker.sources.errors import AccountSkipped
    from bot_farm_silence_tracker.storage import SAMPLE_SKIPPED

    head, done, fatal = sampled_handles(tmp_path)[:3]
    cancelled, done_finished = threading.Event(), threading.Event()
    archive = Archive(build_world())
    source = archive_source(archive, 3)
    real_fetch = source.fetch_activity
    original_cancel = source.http.cancel

    def cancel() -> None:
        original_cancel()
        cancelled.set()

    def fetch_activity(account: Account, since: Any, until: Any) -> Any:
        if account.handle == head:
            assert cancelled.wait(5), "the fatal error did not cancel the client"
            return real_fetch(account, since, until)  # Raises FetchCancelled.
        if account.handle == done:
            done_finished.set()
            if outcome_kind == "unavailable":
                raise AccountUnavailable("suspended")
            raise AccountSkipped("skipped", meta={"skipped": "test"})
        if account.handle == fatal:
            assert done_finished.wait(5)
            raise RateLimited(None, "stop the scan")
        return real_fetch(account, since, until)

    source.http.cancel = cancel  # type: ignore[method-assign]
    source.fetch_activity = fetch_activity  # type: ignore[method-assign]
    with Storage(tmp_path / "c.db") as storage:
        outcome = run_scan(source, SEEDS, storage, scan_settings(3))
        assert outcome.status == "partial"
        sample = storage.get_run_sample(outcome.run_id)
        statuses = {a.handle: s for a, s in sample}
        recorded = {a.handle: a.status for a, _ in sample}
    assert statuses[head] == SAMPLE_PENDING
    assert statuses[fatal] == SAMPLE_PENDING
    if outcome_kind == "unavailable":
        assert statuses[done] == SAMPLE_FETCHED
        assert recorded[done] == "suspended"
    else:
        assert statuses[done] == SAMPLE_SKIPPED


def test_an_exhausted_remaining_is_counted_once_by_the_worker_that_saw_it() -> None:
    clock = VirtualClock()
    archive = Archive([pulse()], script=[remaining_zero()])
    src, _ = make(archive, concurrency=2, clock=clock, sleep=clock.sleep)
    fetch(src, "a")
    rows = src.drain_request_log(final=True)
    reset = 20 + 1 + 2.5
    assert rows[0]["wait_s"] == pytest.approx(reset)
    assert all(row["wait_s"] is None for row in rows[1:])
    summary = src.request_summary()
    assert summary is not None
    assert summary["reset_wait_s"] == pytest.approx(reset)
    assert summary["pause_wait_s"] == 0.0
    # The real time: the reset once, plus one pacer interval before each later request.
    assert clock() == pytest.approx(reset + 2.0 * (len(rows) - 1))


def test_another_workers_longer_pause_counts_only_its_extra_time() -> None:
    clock = VirtualClock()
    holder: list[RedditArchiveSource] = []
    first: list[bool] = []

    def hook(q: dict[str, str]) -> httpx.Response | None:
        if q.get("author") == "a" and not first:
            first.append(True)
            holder[0].http.hold(40)  # Another worker's pause, longer than the reset.
            return remaining_zero()
        return None

    archive = Archive([pulse()], hook=hook)
    src, _ = make(archive, concurrency=2, clock=clock, sleep=clock.sleep)
    holder.append(src)
    fetch(src, "a")
    rows = src.drain_request_log(final=True)
    reset = 20 + 1 + 2.5
    assert rows[0]["wait_s"] == pytest.approx(40.0)  # The reset, then the extra pause.
    summary = src.request_summary()
    assert summary is not None
    assert summary["reset_wait_s"] == pytest.approx(reset)
    assert summary["pause_wait_s"] == pytest.approx(40.0 - reset)
    assert clock() == pytest.approx(40.0 + 2.0 * (len(rows) - 1))


@pytest.mark.parametrize(
    ("response", "own_wait"),
    [
        (httpx.Response(429, headers={"X-RateLimit-Reset": "7"}), 7 + 1 + 2.5),
        (httpx.Response(503, headers={"Retry-After": "4"}), 4.0),
    ],
)
def test_a_pending_pause_is_kept_when_a_retried_response_adds_its_wait(
    response: httpx.Response, own_wait: float
) -> None:
    clock = VirtualClock()
    archive = Archive([pulse()], script=[response])
    src, _ = make(archive, concurrency=2, clock=clock, sleep=clock.sleep)
    src.http.hold(12)  # Another worker's pause before this account's first request.
    fetch(src, "b")
    rows = src.drain_request_log(final=True)
    assert rows[0]["wait_s"] == pytest.approx(12.0 + own_wait)
    assert src.request_summary()["pause_wait_s"] == pytest.approx(12.0)  # type: ignore[index]


def read_timeout_hook(
    timeouts: int, *, author: str | None = None, error: type[Exception] = httpx.ReadTimeout
) -> Any:
    """A hook whose first ``timeouts`` posts queries (of ``author``, or any) time out."""
    seen: Counter[str] = Counter()
    lock = threading.Lock()

    def hook(q: dict[str, str]) -> httpx.Response | None:
        name = q.get("author")
        if name is None or "title" not in q.get("fields", ""):
            return None
        if author is not None and name != author:
            return None
        with lock:
            seen[name] += 1
            count = seen[name]
        if count <= timeouts:
            raise error("timed out")
        return None

    return hook


@pytest.mark.parametrize("workers", [1, 2])
def test_a_read_timeout_is_a_slow_query_not_a_transport_retry(workers: int) -> None:
    archive = Archive([pulse()], hook=read_timeout_hook(1))
    src, sleeps = make(archive, concurrency=workers, slowdown_wait="10")
    _, activities = fetch(src, "a")
    assert activities is not None
    posts = [p for p in archive.author_params() if "title" in p.get("fields", "")]
    assert len(posts) == 2  # One timeout, one retry: the client did not retry on its own.
    rows = src.drain_request_log(final=True)
    assert rows[0]["status"] is None and rows[0]["slow_kind"] == "read_timeout"
    assert rows[0]["wait_s"] == pytest.approx(10.0)  # The timeout backoff, not 1 s.
    assert 1.0 not in sleeps  # No transport backoff.
    summary = src.request_summary()
    assert summary is not None
    assert summary["read_timeouts"] == 1 and summary["failed"] == 1
    assert summary["status_422"] == 0
    assert len(src._timeouts) == 1  # The guard counted it like a timeout 422.
    assert [status for _, status in src._recent].count(None) == 1
    assert any("did not answer" in w for w in src.drain_warnings())
    assert "Read timeouts (handled as timeout 422s): 1." in pipeline.format_request_summary(summary)


def test_read_timeouts_run_the_narrowing_ladder_and_fail_as_a_slow_query() -> None:
    archive = Archive([pulse()], hook=read_timeout_hook(10_000))
    src, _ = make(archive, concurrency=1, slowdown_wait="0")
    with pytest.raises(SlowQueryError):
        fetch(src, "a")
    rows = src.drain_request_log(final=True)
    windows = [row["window_s"] for row in rows if row["slow_kind"] == "read_timeout"]
    assert len(windows) > 2
    assert min(w for w in windows if w is not None) < max(w for w in windows if w is not None)
    assert src._slow_failures["fetch"] == 1


def test_three_slow_reads_in_a_row_stop_the_scan_as_rate_limited() -> None:
    archive = Archive([pulse()], hook=read_timeout_hook(10_000))
    src, _ = make(archive, concurrency=1, slowdown_wait="0", max_slow_queries="3")
    for handle in ("a", "b"):
        with pytest.raises(SlowQueryError):
            fetch(src, handle)
    with pytest.raises(RateLimited, match="in a row"):
        fetch(src, "c")


def test_read_timeouts_escalate_the_guard_like_timeout_422s() -> None:
    archive = Archive([pulse()])
    src, _ = make(archive, concurrency=1, slowdown_wait="1")
    for handle in ("a", "b", "c"):
        archive.hook = read_timeout_hook(1, author=handle)
        fetch(src, handle)
    assert src.request_summary()["read_timeouts_escalated"] == 0  # type: ignore[index]
    archive.hook = read_timeout_hook(1, author="d")
    fetch(src, "d")
    summary = src.request_summary()
    assert summary is not None
    assert summary["read_timeouts_escalated"] == 1
    assert summary["status_422_escalated"] == 0
    assert src.http.rate[0] == pytest.approx(0.25)  # Escalated to load: halved.
    warnings = src.drain_warnings()
    assert any("had several read timeouts in a short time" in w for w in warnings)
    assert not any("answered several timeout 422s" in w for w in warnings)


def test_a_connect_timeout_stays_a_transport_error() -> None:
    archive = Archive([pulse()], hook=read_timeout_hook(10_000, error=httpx.ConnectTimeout))
    src, _ = make(archive, concurrency=1, max_retries=2)
    with pytest.raises(SourceError, match="transport error") as info:
        fetch(src, "a")
    assert not isinstance(info.value, SlowQueryError)
    posts = [p for p in archive.author_params() if "title" in p.get("fields", "")]
    assert len(posts) == 3  # Retried by the shared client.
    assert src.request_summary()["read_timeouts"] == 0  # type: ignore[index]
