"""Local time hints of the desktop app: every time stays UTC, and the app shows the
same moment in the computer's own zone next to it. Each test sets a fixed zone with
TZ and time.tzset (not available on Windows, where these tests are skipped)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest

from bot_farm_silence_tracker.gui import presenter as P
from bot_farm_silence_tracker.scoring import Marker, ScoringParams

pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is unavailable")

MINUS = "\u2212"
#: The repository's default cutoff: Sunday 2026-10-11 00:30 UTC.
CUTOFF = datetime(2026, 10, 11, 0, 30, tzinfo=UTC)


Zone = Callable[[str], None]


@pytest.fixture
def zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[Zone]:
    """Set the process's local zone for one test, and restore it afterwards."""

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


def test_a_negative_offset(zone: Zone) -> None:
    zone("America/New_York")
    assert P.local_picker_line(CUTOFF) == (
        f"= Sat Oct 10, 8:30 PM on this computer (EDT, UTC{MINUS}4)"
    )
    assert P.local_hint(CUTOFF) == "(Oct 10 8:30 PM local)"
    assert P.with_local(CUTOFF) == "2026-10-11 00:30 UTC (Oct 10 8:30 PM local)"


def test_the_other_side_of_a_dst_change_uses_that_dates_offset(zone: Zone) -> None:
    zone("America/New_York")
    winter = datetime(2026, 11, 10, 0, 30, tzinfo=UTC)
    assert P.local_picker_line(winter) == (
        f"= Mon Nov 9, 7:30 PM on this computer (EST, UTC{MINUS}5)"
    )
    # Summer time on the earlier date, whatever today's offset is.
    assert P.local_zone(CUTOFF) == f"EDT, UTC{MINUS}4"


def test_a_positive_offset(zone: Zone) -> None:
    zone("Europe/Berlin")
    assert P.local_picker_line(CUTOFF) == "= Sun Oct 11, 2:30 AM on this computer (CEST, UTC+2)"
    assert P.local_hint(CUTOFF) == "(Oct 11 2:30 AM local)"


def test_a_half_hour_zone(zone: Zone) -> None:
    zone("Asia/Kolkata")
    assert P.local_picker_line(CUTOFF) == "= Sun Oct 11, 6:00 AM on this computer (IST, UTC+5:30)"


def test_a_zone_without_a_letter_abbreviation_shows_the_offset_alone(zone: Zone) -> None:
    zone("America/Sao_Paulo")
    assert P.local_zone(CUTOFF) == f"UTC{MINUS}3"


def test_utc_itself_does_not_repeat_the_time(zone: Zone) -> None:
    zone("UTC")
    assert P.local_picker_line(CUTOFF) == "= the same time on this computer (UTC)"
    assert P.local_hint(CUTOFF) == "(UTC)"
    assert P.with_local(CUTOFF) == "2026-10-11 00:30 UTC (UTC)"


def test_the_timeline_legend_and_event_details_show_local_hints(zone: Zone) -> None:
    zone("America/New_York")
    strike = Marker("strike", "Strike", datetime(2026, 10, 8, 6, 56, tzinfo=UTC), True)
    params = ScoringParams(cutoff=CUTOFF, now=CUTOFF, markers=(strike,), marker_margin_hours=12)
    cutoff_line, marker_line = P.timeline_lines(params, params.baseline_start)
    assert cutoff_line.label.endswith("2026-10-11 00:30 UTC (Oct 10 8:30 PM local)")
    assert marker_line.label == "Strike (approx.) 2026-10-08 06:56 UTC (Oct 8 2:56 AM local)"
    details = P.event_details(None, params)
    assert "Cutoff (main outage): 2026-10-11 00:30 UTC (Oct 10 8:30 PM local)" in details
    assert "Strike (approx.): 2026-10-08 06:56 UTC (Oct 8 2:56 AM local)" in details
