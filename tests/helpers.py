"""Builders shared by the tests."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

from bot_farm_silence_tracker.events import load_events_file
from bot_farm_silence_tracker.models import Account, Activity

#: The tests' own outage events; conftest.py points every test at this file.
FIXTURE_EVENTS = Path(__file__).parent / "fixtures" / "events.toml"
EVENTS = load_events_file(FIXTURE_EVENTS)
#: The default fixture event, and its cutoff, which most tests scan against.
EVENT = EVENTS.default
CUTOFF = EVENT.cutoff
NOW = CUTOFF + timedelta(days=4)
#: A second fixture event with other windows.
SECOND = EVENTS.get("fixture-second")
assert SECOND is not None


def make_account(user_id: str = "u1", created_at: datetime | None = None) -> Account:
    return Account("fake", user_id, f"handle_{user_id}", created_at)


def steady(user_id: str, start: datetime, end: datetime, per_day: int = 4) -> list[Activity]:
    """Evenly spaced activities every day from ``start`` up to (not including) ``end``."""
    step = timedelta(hours=24 / per_day)
    out: list[Activity] = []
    ts = start
    while ts < end:
        out.append(Activity("fake", user_id, ts, "post"))
        ts += step
    return out


def at(days_from_cutoff: float) -> datetime:
    return CUTOFF + timedelta(days=days_from_cutoff)


class MidRandom(random.Random):
    """A random source whose ``uniform`` returns the midpoint, for exact wait asserts.

    Timeout backoffs then keep their nominal value (factor 1.0) and reset waits add
    2.5 s of jitter on top of the 1 s margin.
    """

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2
