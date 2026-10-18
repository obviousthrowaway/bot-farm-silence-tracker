from __future__ import annotations

import math
from datetime import timedelta

import pytest

from bot_farm_silence_tracker.models import Activity
from bot_farm_silence_tracker.scoring import (
    SCORE_WEIGHTS,
    ScoringParams,
    hour_entropy,
    interarrival_cv,
    p_silence,
    rank,
    score_account,
    score_accounts,
)

from .helpers import CUTOFF, NOW, at, make_account, steady


def params(**kw: object) -> ScoringParams:
    return ScoringParams(cutoff=CUTOFF, now=kw.pop("now", NOW), **kw)  # type: ignore[arg-type]


def test_weights_sum_to_one() -> None:
    assert sum(SCORE_WEIGHTS.values()) == pytest.approx(1.0)


def test_steady_then_cliff_is_flagged() -> None:
    # Posts every 6 hours for the 30 baseline days, last one 6 hours before the cutoff.
    acts = steady("u1", at(-30), at(0))
    res = score_account(make_account("u1"), acts, params())
    assert res.eligible and res.cliff
    assert not res.provisional
    assert res.posts_after_cutoff == 0
    assert res.active_days == 30
    assert res.baseline_posts == 120
    assert res.score > 0.5
    assert res.status == "active"


def test_irregular_human_is_not_flagged() -> None:
    # Active on 8 of 30 days only: below the 12 active-day bar.
    acts = [Activity("fake", "h", at(-30 + 4 * i) + timedelta(hours=14), "post") for i in range(8)]
    acts += [a for a in steady("h", at(-30), at(-29), per_day=24)]  # plenty of posts one day
    res = score_account(make_account("h"), acts, params())
    assert not res.eligible
    assert not res.cliff
    assert res.score == 0.0


def test_break_before_baseline_is_not_flagged() -> None:
    acts = steady("b", at(-100), at(-45))
    res = score_account(make_account("b"), acts, params())
    assert res.baseline_posts == 0
    assert not res.eligible and not res.cliff


def test_still_posting_is_not_flagged() -> None:
    acts = steady("s", at(-30), NOW)
    res = score_account(make_account("s"), acts, params())
    assert res.eligible
    assert res.posts_after_cutoff > 0
    assert not res.cliff
    assert res.score == 0.0


def test_break_starting_well_before_cutoff_is_not_a_cliff() -> None:
    # Regular for most of the baseline, but silent for the last five days before cutoff.
    acts = steady("v", at(-30), at(-5))
    res = score_account(make_account("v"), acts, params())
    assert res.eligible
    assert not res.cliff


def test_cliff_window_boundary() -> None:
    inside = steady("i", at(-30), at(-2) + timedelta(hours=1))  # ends 47h before cutoff
    outside = steady("o", at(-30), at(-2) - timedelta(hours=1))  # ends ~49h before
    assert score_account(make_account("i"), inside, params()).cliff
    assert not score_account(make_account("o"), outside, params()).cliff


def test_provisional_when_silence_too_short() -> None:
    acts = steady("p", at(-30), at(0))
    early = score_account(make_account("p"), acts, params(now=at(1)))
    assert early.cliff and early.provisional
    on_boundary = score_account(make_account("p"), acts, params(now=at(3)))
    assert on_boundary.cliff and not on_boundary.provisional


def test_too_few_posts_is_not_eligible() -> None:
    # Active on 15 days but only one post per day: 15 posts < 20.
    acts = [Activity("fake", "f", at(-30 + i) + timedelta(hours=10), "post") for i in range(15)]
    res = score_account(make_account("f"), acts, params())
    assert res.active_days == 15 and res.baseline_posts == 15
    assert not res.eligible


def test_unavailable_account_is_never_eligible() -> None:
    account = make_account("x")
    account.status = "suspended"
    res = score_account(account, steady("x", at(-30), at(0)), params())
    assert not res.eligible and not res.cliff
    assert res.status == "suspended"


def test_p_silence_math() -> None:
    assert p_silence(2.0, 1.5) == pytest.approx(math.exp(-3.0))
    assert p_silence(0.0, 10.0) == 1.0
    assert p_silence(5.0, 0.0) == 1.0

    acts = steady("m", at(-30), at(0), per_day=4)  # 120 posts, rate 4/day
    res = score_account(make_account("m"), acts, params(now=at(0) + timedelta(days=2)))
    # Last post 6h before cutoff, so silence is 2.25 days.
    assert res.p_silence == pytest.approx(math.exp(-4.0 * 2.25))
    assert res.silence_hours == pytest.approx(54.0)


def test_hour_entropy_and_cv() -> None:
    same_hour = [at(-d) + timedelta(hours=5) for d in range(1, 11)]
    assert hour_entropy(same_hour) == pytest.approx(0.0)
    all_hours = [at(-1) + timedelta(hours=h) for h in range(24)]
    assert hour_entropy(all_hours) == pytest.approx(math.log2(24))
    assert interarrival_cv(all_hours) == pytest.approx(0.0)
    assert interarrival_cv(all_hours[:2]) is None
    assert hour_entropy([]) is None


def test_account_age_at_cutoff() -> None:
    account = make_account("a", created_at=at(-100))
    res = score_account(account, [], params())
    assert res.account_age_days_at_cutoff == pytest.approx(100.0)
    assert score_account(make_account("n"), [], params()).account_age_days_at_cutoff is None


def _cliff_with_last(user_id: str, last: object) -> tuple[object, list[Activity]]:
    # Steady posts that end exactly at `last` (before the cutoff).
    step = timedelta(hours=6)
    acts = [Activity("fake", user_id, last - step * i, "post") for i in range(140)]  # type: ignore[operator]
    return make_account(user_id), acts


def test_cluster_grouping() -> None:
    items = [
        _cliff_with_last("a", at(-0.5)),
        _cliff_with_last("b", at(-0.5) - timedelta(minutes=12)),
        _cliff_with_last("c", at(-0.5) + timedelta(minutes=10)),  # 22 minutes after b: same window
        _cliff_with_last("d", at(-1.5)),
        _cliff_with_last("e", at(-1.5) - timedelta(minutes=5)),
        _cliff_with_last("f", at(-1.0)),  # alone
    ]
    results = score_accounts(items, params())  # type: ignore[arg-type]
    by_id = {r.user_id: r for r in results}
    assert all(r.cliff for r in results)
    assert {by_id[u].cluster_size for u in "abc"} == {3}
    assert {by_id[u].cluster_size for u in "de"} == {2}
    assert by_id["f"].cluster_size == 1 and by_id["f"].cluster_id is None
    # Cluster ids are numbered by stopping time: d/e stop earlier than a/b/c.
    assert by_id["d"].cluster_id == 1 and by_id["a"].cluster_id == 2
    # Cluster membership raises the score above a lone account with an equal profile.
    assert by_id["a"].components["cluster"] > 0
    assert by_id["f"].components["cluster"] == 0
    assert rank(results)[0].cluster_size == 3 or rank(results)[0].score >= rank(results)[-1].score


def test_cluster_window_respects_sync_minutes() -> None:
    items = [
        _cliff_with_last("a", at(-0.5)),
        _cliff_with_last("b", at(-0.5) + timedelta(minutes=45)),
    ]
    tight = score_accounts(items, params(sync_minutes=30))  # type: ignore[arg-type]
    assert [r.cluster_id for r in tight] == [None, None]
    loose = score_accounts(items, params(sync_minutes=60))  # type: ignore[arg-type]
    assert [r.cluster_size for r in loose] == [2, 2]


def test_non_cliff_accounts_do_not_cluster() -> None:
    cliff = _cliff_with_last("a", at(-0.5))
    alive = (make_account("z"), steady("z", at(-30), NOW))
    results = score_accounts([cliff, alive], params())  # type: ignore[list-item]
    assert results[1].cluster_id is None and results[1].cluster_size == 1


def test_params_roundtrip() -> None:
    p = params(min_posts=7)
    assert ScoringParams.from_dict(p.to_dict()) == p
