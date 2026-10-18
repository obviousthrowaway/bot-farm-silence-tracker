"""A stop whose oldest second is the window end must not store a start past ``until``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot_farm_silence_tracker.models import PARTIAL_FETCH_KEY, Account, Activity
from bot_farm_silence_tracker.pipeline import _load_or_fetch
from bot_farm_silence_tracker.sources.errors import BudgetExceeded
from bot_farm_silence_tracker.storage import Storage
from bot_farm_silence_tracker.timeutil import isoformat

SINCE = datetime(2026, 1, 1, tzinfo=UTC)
UNTIL = SINCE + timedelta(days=10)
OLD_END = SINCE + timedelta(days=20)


class _StopsAtTheEnd:
    """A source whose fetch stops after nothing before the end of the window."""

    def __init__(self, covered: datetime) -> None:
        self.covered = covered

    def hydrate(self, account: Account) -> None:
        raise AssertionError("the account is already hydrated")

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        account.meta[PARTIAL_FETCH_KEY] = {
            "reason": "budget",
            "since": isoformat(self.covered),
            "posts": 1,
        }
        err = BudgetExceeded("budget")
        err.partial = []  # Posts are strictly before ``until``, so none sit at it.
        raise err


@pytest.mark.parametrize("until_offset_ms", [0, 500])
def test_stop_at_until_never_saves_a_window_starting_past_until(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until_offset_ms: int
) -> None:
    """The kept-rows path saves the new pages from ``covered + GAP_OVERLAP``. When
    ``covered`` equals ``until`` that start passed ``until``, so an
    inverted window (start after end) was committed before the follow-up merge moved
    the start down to the kept rows. Tests both whole-second and sub-second ``until``
    values to ensure no stored window has start > end."""
    until = UNTIL + timedelta(milliseconds=until_offset_ms)
    covered = until  # Timestamps are stored to the second.
    saved: list[tuple[datetime, datetime]] = []
    inverted_on_disk: list[bool] = []
    real_save = Storage.save_activities

    def spy(
        self: Storage, account: Account, activities, since, window_until, fetched_at, **kw
    ) -> None:  # type: ignore[no-untyped-def]
        real_save(self, account, activities, since, window_until, fetched_at, **kw)
        saved.append((since, window_until))
        window = self.activity_window(account.platform, account.user_id)
        inverted_on_disk.append(window is not None and window[0] > window[1])

    with Storage(tmp_path / "c.db") as storage:
        account = Account("x", "7", "h", SINCE - timedelta(days=400))
        storage.upsert_account(account, SINCE)
        old = [Activity("x", "7", SINCE + timedelta(days=d), "post") for d in range(1, 15)]
        storage.save_activities(account, old, SINCE, OLD_END, SINCE)
        monkeypatch.setattr(Storage, "save_activities", spy)
        with pytest.raises(BudgetExceeded):
            _load_or_fetch(
                _StopsAtTheEnd(covered),  # type: ignore[arg-type]
                storage,
                Account("x", "7", "h", SINCE - timedelta(days=400)),
                SINCE,
                until,
                timedelta(0),
                use_cache=False,
            )
        window = storage.activity_window("x", "7")
        rows = storage.get_activities("x", "7", SINCE, OLD_END)
    assert saved and all(since <= window_until for since, window_until in saved)
    assert not any(inverted_on_disk), (
        f"until_offset_ms={until_offset_ms}: inverted window found on disk"
    )
    # The kept rows still reach ``since``, and no cached row was lost.
    # Storage truncates to whole seconds, so a sub-second ``until`` stores the second
    # below it. The stored end never passes ``until`` and the rows match the
    # whole-second case.
    assert window is not None and window[0] == SINCE
    assert window[0] <= window[1] <= until
    assert [r.ts for r in rows] == [a.ts for a in old]
