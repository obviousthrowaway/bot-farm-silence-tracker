"""The reddit-archive pre-check: existence queries for --baseline-sample-rate."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from bot_farm_silence_tracker.early_exit import (
    STRATUM_ACTIVE_AFTER,
    STRATUM_CANDIDATE,
    STRATUM_SILENT,
    phase1_start,
    precheck,
)
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.scoring import ScoringParams
from bot_farm_silence_tracker.sources.base import RateLimited
from bot_farm_silence_tracker.sources.reddit_archive import SlowQueryError

from .helpers import CUTOFF, NOW
from .test_reddit_archive import Archive, comment, make_source, post, pulse, slow, ts

PARAMS = ScoringParams(cutoff=CUTOFF, now=NOW)
START = phase1_start(PARAMS)


def account(handle: str = "alice") -> Account:
    return Account("reddit-archive", handle, handle)


def test_newest_activity_is_one_sorted_single_row_query() -> None:
    archive = Archive(
        [
            pulse(),
            comment("1", "alice", "a", CUTOFF - timedelta(hours=30)),
            comment("2", "alice", "a", CUTOFF - timedelta(hours=5)),
            comment("3", "alice", "a", CUTOFF - timedelta(days=5)),  # Before the window.
        ]
    )
    src, _, _ = make_source(archive)
    found = src.newest_activity(account(), "comment", START, NOW)
    assert found.newest == CUTOFF - timedelta(hours=5) and found.covered_since == START
    (q,) = archive.author_params()
    assert q["sort"] == "desc" and q["limit"] == "1" and q["fields"] == "created_utc"
    assert int(q["after"]) == ts(START) - 1 and int(q["before"]) == ts(NOW)
    assert archive.requests[-1].url.path.endswith("/comments/search")
    assert src.newest_activity(account(), "post", START, NOW).newest is None
    assert archive.requests[-1].url.path.endswith("/posts/search")


@pytest.mark.parametrize(
    ("items", "stratum", "queries"),
    [
        ([comment("1", "alice", "a", CUTOFF + timedelta(hours=3))], STRATUM_ACTIVE_AFTER, 1),
        (
            [
                comment("1", "alice", "a", CUTOFF - timedelta(hours=3)),
                post("p", "alice", "a", CUTOFF + timedelta(hours=1)),
            ],
            STRATUM_ACTIVE_AFTER,
            2,
        ),
        ([post("p", "alice", "a", CUTOFF - timedelta(hours=40))], STRATUM_CANDIDATE, 2),
        ([comment("1", "alice", "a", CUTOFF - timedelta(days=3))], STRATUM_SILENT, 2),
    ],
)
def test_precheck_on_the_archive(items: list, stratum: str, queries: int) -> None:
    archive = Archive([pulse(), *items])
    src, _, _ = make_source(archive)
    result = precheck(src, account(), PARAMS)
    assert result.stratum == stratum and result.complete
    assert len(archive.author_params()) == queries


def too_slow_before_cutoff(q: dict[str, str]) -> httpx.Response | None:
    """The archive times out on the whole window but answers from the cutoff on."""
    if "author" in q and int(q["after"]) < ts(CUTOFF) - 1:
        return slow()
    return None


def test_a_slow_window_is_narrowed_to_the_cutoff_and_stays_conservative() -> None:
    archive = Archive(
        [pulse(), comment("1", "alice", "a", CUTOFF - timedelta(hours=3))],
        hook=too_slow_before_cutoff,
    )
    src, _, _ = make_source(archive, slowdown_wait=0)
    found = src.newest_activity(account(), "comment", START, NOW)
    assert found.newest is None and found.covered_since == CUTOFF
    assert int(archive.author_params()[-1]["after"]) == ts(CUTOFF) - 1
    result = precheck(src, account(), PARAMS)
    assert result.stratum == STRATUM_CANDIDATE and not result.complete


def test_a_narrowed_query_still_proves_activity_after_the_cutoff() -> None:
    archive = Archive(
        [pulse(), comment("1", "alice", "a", CUTOFF + timedelta(hours=3))],
        hook=too_slow_before_cutoff,
    )
    src, _, _ = make_source(archive, slowdown_wait=0)
    result = precheck(src, account(), PARAMS)
    assert result.stratum == STRATUM_ACTIVE_AFTER
    assert len({q["after"] for q in archive.author_params()}) == 2  # Full, then narrowed.


def test_a_query_that_never_answers_makes_a_candidate_until_the_guard_trips() -> None:
    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if "author" in q else None

    src, _, _ = make_source(Archive([pulse()], hook=hook), slowdown_wait=0)
    with pytest.raises(SlowQueryError):
        src.newest_activity(account(), "comment", START, NOW)
    # The guard counts accounts, not queries: one account's two failed queries count
    # once, so the third account in a row trips the default limit of 3.
    for handle in ("alice", "bob"):
        result = precheck(src, account(handle), PARAMS)
        assert result.stratum == STRATUM_CANDIDATE and not result.complete
    with pytest.raises(RateLimited, match="precheck"):
        precheck(src, account("carol"), PARAMS)
    src2, _, _ = make_source(Archive([pulse()], hook=hook), slowdown_wait=0, max_slow_queries=5)
    for handle in ("alice", "bob", "carol", "dave"):
        result = precheck(src2, account(handle), PARAMS)
        assert result.stratum == STRATUM_CANDIDATE and not result.complete


def test_alternating_kinds_cannot_defeat_the_slow_query_guard() -> None:
    """Comments always time out, posts always answer: each account still counts once."""

    archive = Archive([pulse()])

    def comments_slow(q: dict[str, str]) -> httpx.Response | None:
        path = archive.requests[-1].url.path if archive.requests else ""
        return slow() if "author" in q and path.endswith("/comments/search") else None

    archive.hook = comments_slow
    src, _, _ = make_source(archive, slowdown_wait=0)
    for handle in ("alice", "bob"):
        result = precheck(src, account(handle), PARAMS)
        assert result.stratum == STRATUM_CANDIDATE and not result.complete
    with pytest.raises(RateLimited, match="precheck"):
        precheck(src, account("carol"), PARAMS)


def test_a_complete_precheck_resets_the_guard() -> None:
    failing = {"alice", "bob", "dave", "erin"}

    def hook(q: dict[str, str]) -> httpx.Response | None:
        return slow() if q.get("author") in failing else None

    src, _, _ = make_source(Archive([pulse()], hook=hook), slowdown_wait=0)
    for handle in ("alice", "bob", "carol", "dave", "erin"):
        precheck(src, account(handle), PARAMS)  # carol answers and resets the count.
    with pytest.raises(RateLimited, match="precheck"):
        failing.add("frank")
        precheck(src, account("frank"), PARAMS)
