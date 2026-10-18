"""Outage events: named cutoffs, each with its own windows, registry folder and cache.

An outage event is the moment a group of accounts is expected to have gone silent,
such as the takedown of the bot data centers that ran them. Every scan belongs to
one event: the event gives the scan its cutoff and the default lengths of its
baseline window, its silence window and its cliff window.

The events file
---------------
The repository file ``events.toml`` lists the shared events::

    default = "2026-12-example"

    [[event]]
    id = "2026-12-example"
    name = "Example takedown of bot data centers"
    cutoff = "2026-12-03T06:00:00Z"
    baseline_days = 30          # default 30
    min_silence_days = 3.0      # default 3
    cliff_window_hours = 48.0   # default 48
    verified_min_silence_days = 21.0  # optional, default 21
    notes = "Free text."        # optional
    marker_margin_hours = 12.0  # needed in the shared file when there are markers

    [[event.markers]]           # optional: earlier moments inside the same outage
    id = "first-strike"
    name = "First strike"
    time = "2026-12-01T04:00:00Z"
    approximate = false         # true: the time is a guess (labels say "approx.")
    time_note = "Reported time of first statements."
    notes = "Free text."
    sources = ["Agency A", "Agency B"]   # plain text, optional

The windows may be left out of a local events file. The shared one, which the
registry and its gate read, must write them out (``require_windows``).

``verified_min_silence_days`` is the silence a record needs before a pull request
may set it to ``verified_inauthentic``: at least ``min_silence_days`` and by default
:data:`DEFAULT_VERIFIED_MIN_SILENCE_DAYS` (21). Three days of silence is enough to
review an account, not to call it inauthentic. The shorter ``min_silence_days`` stays
the threshold of a cliff and a ``candidate``. Changing it in the shared file is
guarded like the windows.

``default`` names the event used when a command gets no ``--event``; the user config
file can choose another (``config set-default-event``). The optional ``legacy_event``
(by default the ``default`` event) names the event that the single cache database of
versions before events belongs to: it is moved into that event's folder. The rules
(:func:`parse_events`) are strict, because the file decides which accounts count as
silent: unknown keys, malformed ids, duplicate ids, cutoffs out of order or in the
future, and values out of range are all errors.

An id is a lower-case slug (:data:`EVENT_ID_PATTERN`). When it starts with a year
and month (``2026-12-...``), they must be the cutoff's. The name and notes are
restricted to plain characters, so they are safe to show in Markdown.

Markers and the silence window
------------------------------
An outage can come in steps: earlier strikes that knocked out part of the
infrastructure before the main collapse at the cutoff. Such steps are **markers** of
the event, not events of their own. Each marker has a slug id (unique in its event;
``cutoff`` is reserved for the main outage), a name, a UTC time, an ``approximate``
flag, a ``time_note`` saying where the time comes from, notes and optional plain-text
sources. A marker's time must lie between the start of the event's baseline window
(``cutoff - baseline_days``) and ``cutoff + min_silence_days``, and markers are listed
in time order.

Markers change the windows of the event's scans (see :class:`~bot_farm_silence_tracker.
scoring.ScoringParams`), with ``onset = earliest marker - marker_margin_hours``:

* the baseline window ends at ``min(cutoff, onset)``, so a decline caused by an earlier
  strike does not pollute the baseline;
* the silence (cliff) window starts at ``min(cutoff - cliff_window_hours, onset)`` and
  ends at the cutoff, so a silence that began at any marker counts, not only one in
  the ``cliff_window_hours`` before the cutoff.

The margin covers a marker whose reported time is later than the moment it stands
for (the first statements after an overnight strike, say). An event without markers
keeps the windows it always had: the baseline ends at the cutoff and the silence
window is ``cliff_window_hours`` long.

Every flagged account and stop-time cluster is labelled with the marker (or the main
outage, ``cutoff``) its silence began nearest to; see
:func:`bot_farm_silence_tracker.scoring.nearest_marker`.

Where events live
-----------------
* the shared registry keeps each event's records in ``registry/events/<id>/``;
* the local cache keeps one database per event,
  ``<cache>/events/<id>/bot-farm-silence-tracker.db`` (see
  :func:`bot_farm_silence_tracker.storage.event_db_path`).

Custom events
-------------
``--cutoff 2026-12-03T06:00Z`` makes a local-only event named
``custom-20261203T0600Z``. Its id holds its cutoff (minute precision), so it needs
no file: the same ``--cutoff`` or ``--event custom-...`` finds the same event and
database again. Custom events are never written to the shared registry, and the
registry commands refuse them.

Runs of an earlier definition
-----------------------------
A run stores its event id and the cutoff it was scored with. When a shared event's
cutoff changes later, the runs scanned with the old cutoff no longer belong to it:
:func:`event_of_run` gives them the clearly named local event of their own cutoff
(``custom-YYYYMMDDTHHMMZ``, see :func:`legacy_event`). They stay readable where they
are, a resume keeps their cutoff, and the registry never takes them.

Which file is read
------------------
:func:`load_events` reads the file that ``BOT_FARM_SILENCE_TRACKER_EVENTS`` names,
else ``<repo>/events.toml`` in a source checkout, else the copy shipped with the
package. Registry commands read the ``events.toml`` next to the registry directory
when there is one (:func:`registry_events`); the gate reads it from the commit.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.scoring import Marker, ScoringParams
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, parse_datetime, utcnow

#: Environment variable that names a different events file.
EVENTS_ENV = "BOT_FARM_SILENCE_TRACKER_EVENTS"
#: File name of the events file, at the repository root next to ``registry/``.
EVENTS_FILE = "events.toml"
#: An event id: a lower-case slug of 3 to 64 characters.
EVENT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$"
#: The prefix of local-only custom events. Shared events may not use it.
CUSTOM_PREFIX = "custom-"
_CUSTOM_ID = re.compile(r"^custom-(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})Z$")
_YEAR_MONTH_ID = re.compile(r"^(\d{4})-(\d{2})-")
#: A cutoff as written in the file: UTC, to the minute (seconds, when written, are
#: ``00``, as for custom cutoffs and the app's picker), with ``Z`` or ``+00:00``.
_CUTOFF_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:00)?(Z|\+00:00)$")
#: Characters allowed in a name: letters, digits, spaces and plain punctuation.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.()'/:-]{0,119}$")
#: Notes may also hold line breaks and a little more punctuation, but no Markdown.
_NOTES = re.compile(r"^[A-Za-z0-9 ,.()'/:;?!%+=\n-]{0,2000}$")

#: Size limits, checked before the file is parsed.
MAX_EVENTS_FILE_BYTES = 64_000
MAX_EVENTS = 200
#: No cutoff may lie before this: none of the scanned platforms reaches further back.
EARLIEST_CUTOFF = datetime(2005, 1, 1, tzinfo=UTC)
#: How far after now a cutoff may lie (clock skew between contributors).
FUTURE_SLACK = timedelta(hours=1)
#: Allowed ranges of the windows, inclusive.
BASELINE_DAYS_RANGE = (1, 365)
#: At least one day of silence: an event added without a label (with records in the
#: same pull request) cannot make almost any stop count as a cliff.
MIN_SILENCE_DAYS_RANGE = (1.0, 90.0)
CLIFF_WINDOW_HOURS_RANGE = (0.0, 720.0)
#: The silence a record needs before it may become ``verified_inauthentic``, by
#: default, and the allowed range (it must also be at least ``min_silence_days``).
DEFAULT_VERIFIED_MIN_SILENCE_DAYS = 21.0
VERIFIED_MIN_SILENCE_DAYS_RANGE = (1.0, 365.0)

_TOP_KEYS = frozenset({"default", "legacy_event", "event"})
_EVENT_KEYS = frozenset(
    {
        "id",
        "name",
        "cutoff",
        "baseline_days",
        "min_silence_days",
        "cliff_window_hours",
        "marker_margin_hours",
        "verified_min_silence_days",
        "notes",
        "markers",
    }
)
_MARKER_KEYS = frozenset({"id", "name", "time", "approximate", "time_note", "notes", "sources"})
#: Fields whose change moves which accounts count as silent: changing one, renaming
#: an event or deleting one needs the maintainer override in the registry gate.
GUARDED_FIELDS = (
    "name",
    "cutoff",
    "baseline_days",
    "min_silence_days",
    "cliff_window_hours",
    "marker_margin_hours",
    "verified_min_silence_days",
)

#: The id of the main outage among an event's markers (reserved: no marker may use it).
MAIN_OUTAGE_ID = "cutoff"
#: Most markers one event may have.
MAX_MARKERS = 20
#: Most sources one marker may list.
MAX_SOURCES = 10
#: A marker's time note is shorter than its notes.
_TIME_NOTE = re.compile(r"^[A-Za-z0-9 ,.()'/:;?!%+=-]{0,300}$")
#: A source is a short plain name, such as a news agency's.
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.()'/:;&-]{0,199}$")
#: The default margin before the earliest marker (see the module notes). The shared
#: file must write it out for an event with markers, so this default never moves one.
DEFAULT_MARKER_MARGIN_HOURS = 12.0
MARKER_MARGIN_HOURS_RANGE = (0.0, 72.0)


class EventError(ValueError):
    """An events file or an event id is not valid."""


#: The window lengths of an event that does not set them: the scoring algorithm's own
#: defaults (a test checks they equal :class:`~bot_farm_silence_tracker.scoring.
#: ScoringParams`'s). The shared ``events.toml`` must still write every window out
#: (``require_windows``), so changing these in code can never move a shared event.
DEFAULT_WINDOWS: dict[str, Any] = {
    "baseline_days": 30,
    "min_silence_days": 3.0,
    "cliff_window_hours": 48.0,
}
WINDOW_KEYS = tuple(DEFAULT_WINDOWS)


def _algorithm_defaults() -> dict[str, Any]:
    """The window lengths used when an event does not set them."""
    return dict(DEFAULT_WINDOWS)


@dataclass(frozen=True)
class EventMarker:
    """An earlier moment inside an outage event, such as a strike before the collapse.

    Times are as reported by the sources; ``time_note`` says what the time stands for
    and ``approximate`` marks a time that is only a guess.
    """

    id: str
    name: str
    time: datetime
    approximate: bool = False
    time_note: str = ""
    notes: str = ""
    sources: tuple[str, ...] = ()

    def anchor(self) -> Marker:
        """The marker as scoring uses it (id, name, time and the approximate flag)."""
        return Marker(self.id, self.name, ensure_utc(self.time), self.approximate)

    @property
    def time_text(self) -> str:
        return isoformat(self.time)

    def label(self) -> str:
        """``Name (time UTC)``, with ``approx.`` for an approximate time."""
        approx = ", approx." if self.approximate else ""
        return f"{self.name} ({self.time_text}{approx})"


@dataclass(frozen=True)
class OutageEvent:
    """One outage event: a cutoff and the windows scans of it use."""

    id: str
    name: str
    cutoff: datetime
    baseline_days: int
    min_silence_days: float
    cliff_window_hours: float
    notes: str = ""
    #: A local-only event made from ``--cutoff`` (never in the shared registry).
    custom: bool = False
    #: Earlier moments inside this outage, in time order (see the module notes).
    markers: tuple[EventMarker, ...] = ()
    #: How long before the earliest marker the silence window starts.
    marker_margin_hours: float = DEFAULT_MARKER_MARGIN_HOURS
    #: For the local event of runs scanned under an earlier definition of a shared
    #: event (see :func:`legacy_event`): that event's id.
    legacy_of: str = ""
    #: The silence a record needs to become ``verified_inauthentic`` (see the module
    #: notes); at least ``min_silence_days``.
    verified_min_silence_days: float = DEFAULT_VERIFIED_MIN_SILENCE_DAYS

    def scoring_markers(self) -> tuple[Marker, ...]:
        """The markers as :class:`~bot_farm_silence_tracker.scoring.ScoringParams` takes them."""
        return tuple(m.anchor() for m in self.markers)

    def scoring_marker_margin(self) -> float:
        """The margin scans store: the event's, or 0 when it has no markers."""
        return float(self.marker_margin_hours) if self.markers else 0.0

    def windows(self) -> tuple[datetime, datetime, datetime]:
        """``(baseline start, baseline end, silence window start)`` of this event's scans."""
        params = ScoringParams(
            cutoff=ensure_utc(self.cutoff),
            now=ensure_utc(self.cutoff),
            baseline_days=self.baseline_days,
            cliff_window_hours=self.cliff_window_hours,
            markers=self.scoring_markers(),
            marker_margin_hours=self.scoring_marker_margin(),
        )
        return params.baseline_start, params.baseline_end, params.window_start

    def anchors(self) -> tuple[Marker, ...]:
        """The main outage and the markers, as silences are labelled with them (empty
        without markers)."""
        return ScoringParams(
            cutoff=ensure_utc(self.cutoff),
            now=ensure_utc(self.cutoff),
            markers=self.scoring_markers(),
        ).anchors

    def marker(self, marker_id: str) -> EventMarker | None:
        return next((m for m in self.markers if m.id == marker_id), None)

    @property
    def min_silence(self) -> timedelta:
        """The least silence a cliff record of this event needs."""
        return timedelta(days=self.min_silence_days)

    @property
    def verified_min_silence(self) -> timedelta:
        """The least silence a ``verified_inauthentic`` record of this event needs."""
        return timedelta(days=max(self.verified_min_silence_days, self.min_silence_days))

    def scoring_defaults(self) -> dict[str, Any]:
        """The scan options this event decides, as ``scan`` stores them."""
        return {
            "cutoff": isoformat(self.cutoff),
            "baseline_days": self.baseline_days,
            "min_silence_days": self.min_silence_days,
            "cliff_window_hours": self.cliff_window_hours,
        }

    def guarded(self) -> dict[str, Any]:
        """The values of :data:`GUARDED_FIELDS`, for comparing two versions."""
        return {
            "name": self.name,
            "cutoff": isoformat(self.cutoff),
            "baseline_days": self.baseline_days,
            "min_silence_days": float(self.min_silence_days),
            "cliff_window_hours": float(self.cliff_window_hours),
            "marker_margin_hours": float(self.marker_margin_hours),
            "verified_min_silence_days": float(self.verified_min_silence_days),
        }

    @property
    def cutoff_text(self) -> str:
        return isoformat(self.cutoff)


@dataclass(frozen=True)
class EventCatalog:
    """The events of one events file, in file order, and the default one."""

    events: tuple[OutageEvent, ...]
    default_id: str
    source: str = EVENTS_FILE
    #: The event the cache database of versions before events belongs to.
    legacy_id: str = ""
    _by_id: dict[str, OutageEvent] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._by_id.update({event.id: event for event in self.events})
        if not self.legacy_id:
            object.__setattr__(self, "legacy_id", self.default_id)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(event.id for event in self.events)

    @property
    def default(self) -> OutageEvent:
        return self._by_id[self.default_id]

    def __contains__(self, event_id: object) -> bool:
        return event_id in self._by_id

    def get(self, event_id: str) -> OutageEvent | None:
        """The shared event ``event_id``, or ``None`` (custom ids are not shared)."""
        return self._by_id.get(event_id)

    def resolve(self, event_id: str) -> OutageEvent:
        """The shared event, or the custom event the id names. Raises :class:`EventError`."""
        found = self._by_id.get(event_id)
        if found is not None:
            return found
        if event_id.startswith(CUSTOM_PREFIX):
            return custom_event_from_id(event_id)
        known = ", ".join(self.ids) or "none"
        raise EventError(
            f"unknown event {event_id!r}; known events: {known} (see `events list`), or "
            "give --cutoff for a local custom event"
        )

    def for_cutoff(self, cutoff: datetime) -> OutageEvent:
        """The event a run made before events existed belongs to.

        That is a shared event with the run's cutoff (the legacy event first, then
        the default, then the others in file order), else the custom event of that
        cutoff (to the minute). So a later change of the default event does not
        change which event an old run belongs to.
        """
        when = ensure_utc(cutoff)
        ordered = [self._by_id[self.legacy_id], self.default, *self.events]
        for event in ordered:
            if ensure_utc(event.cutoff) == when:
                return event
        return custom_event(when.replace(second=0, microsecond=0), allow_seconds=True)


# -- custom events -------------------------------------------------------------------


def custom_event_id(cutoff: datetime) -> str:
    """``custom-YYYYMMDDTHHMMZ`` for a cutoff (UTC, minute precision)."""
    return CUSTOM_PREFIX + ensure_utc(cutoff).strftime("%Y%m%dT%H%MZ")


def custom_event(cutoff: datetime, *, allow_seconds: bool = False) -> OutageEvent:
    """The local-only event of ``cutoff``, with the algorithm's default windows.

    Raises :class:`EventError` for a cutoff with seconds (its id could not hold
    them) unless ``allow_seconds``, which drops them.
    """
    when = ensure_utc(cutoff)
    if (when.second or when.microsecond) and not allow_seconds:
        raise EventError(
            f"a custom cutoff has minute precision; got {isoformat(when)}. Drop the seconds."
        )
    when = when.replace(second=0, microsecond=0)
    if when < EARLIEST_CUTOFF:
        raise EventError(f"the cutoff {isoformat(when)} is before {isoformat(EARLIEST_CUTOFF)}")
    defaults = _algorithm_defaults()
    event_id = custom_event_id(when)
    return OutageEvent(
        id=event_id,
        name=f"Custom cutoff {isoformat(when)}",
        cutoff=when,
        baseline_days=int(defaults["baseline_days"]),
        min_silence_days=float(defaults["min_silence_days"]),
        cliff_window_hours=float(defaults["cliff_window_hours"]),
        notes="A local-only event made from --cutoff. It is never shared.",
        custom=True,
    )


def custom_event_from_id(event_id: str) -> OutageEvent:
    """The custom event an id names. Raises :class:`EventError` for a malformed id."""
    match = _CUSTOM_ID.fullmatch(event_id)
    if match is None:
        raise EventError(
            f"{event_id!r} is not a custom event id; custom ids look like "
            "custom-20261203T0600Z (made by --cutoff)"
        )
    try:
        when = datetime(*(int(part) for part in match.groups()), tzinfo=UTC)  # type: ignore[misc]
    except ValueError:
        raise EventError(f"{event_id!r} does not hold a valid date and time") from None
    return custom_event(when)


def is_custom(event_id: str) -> bool:
    return event_id.startswith(CUSTOM_PREFIX)


def valid_event_id(event_id: str) -> bool:
    """Whether ``event_id`` has the syntax of a shared or a custom event id."""
    return bool(re.fullmatch(EVENT_ID_PATTERN, event_id) or _CUSTOM_ID.fullmatch(event_id))


def parse_cutoff_option(text: str) -> OutageEvent:
    """The custom event of a ``--cutoff`` value (a date, or a UTC date and time)."""
    try:
        when = parse_datetime(text)
    except ValueError:
        raise EventError(
            f"--cutoff must be a date or a UTC date and time (2026-12-03 or "
            f"2026-12-03T06:00Z), got {text!r}"
        ) from None
    return custom_event(when)


# -- parsing and validating the events file ------------------------------------------


def _check_number(
    problems: list[str], where: str, key: str, value: Any, bounds: tuple[float, float], kind: type
) -> float | None:
    """``value`` when it is a number of ``kind`` in ``(low, high]`` (``[low, high]`` for
    whole days, for the silence and for the marker margin, whose floors are allowed),
    else ``None`` with a problem recorded."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        problems.append(f"{where}: {key} must be a number")
        return None
    if kind is int and not isinstance(value, int):
        problems.append(f"{where}: {key} must be a whole number of days")
        return None
    low, high = bounds
    closed = kind is int or key in (
        "min_silence_days",
        "marker_margin_hours",
        "verified_min_silence_days",
    )
    inside = low <= value <= high if closed else low < value <= high
    if not inside:
        span = f"[{low:g}, {high:g}]" if closed else f"({low:g}, {high:g}]"
        problems.append(f"{where}: {key} must be in {span}, got {value!r}")
        return None
    return float(value)


def _parse_time(problems: list[str], where: str, key: str, text: Any) -> datetime | None:
    """A UTC time written to the minute (see :data:`_CUTOFF_TEXT`), else ``None``."""
    if not isinstance(text, str) or not _CUTOFF_TEXT.fullmatch(text):
        problems.append(
            f"{where}: {key} must be a UTC ISO-8601 date and time string to the "
            'minute, such as "2026-12-03T06:00:00Z"'
        )
        return None
    try:
        return parse_datetime(text)
    except ValueError:
        problems.append(f"{where}: {key} is not a valid date and time")
        return None


def _parse_markers(
    problems: list[str],
    where: str,
    listed: Any,
    *,
    cutoff: datetime | None,
    baseline_days: float | None,
    min_silence_days: float | None,
    limit: datetime,
) -> tuple[EventMarker, ...]:
    """Validate an event's ``[[event.markers]]`` tables (see the module notes)."""
    if not isinstance(listed, list):
        problems.append(f"{where}: markers must be a list of [[event.markers]] tables")
        return ()
    if len(listed) > MAX_MARKERS:
        problems.append(f"{where}: more than {MAX_MARKERS} markers")
        return ()
    out: list[EventMarker] = []
    seen: set[str] = set()
    for index, item in enumerate(listed, 1):
        here = f"{where}: marker {index}"
        if not isinstance(item, dict):
            problems.append(f"{here}: must be a table")
            continue
        for key in sorted(set(item) - _MARKER_KEYS):
            problems.append(f"{here}: unknown key {key!r}")
        marker_id = item.get("id")
        if not isinstance(marker_id, str) or not re.fullmatch(EVENT_ID_PATTERN, marker_id):
            problems.append(f"{here}: id must match {EVENT_ID_PATTERN}")
            continue
        here = f"{where}: marker {marker_id!r}"
        if marker_id == MAIN_OUTAGE_ID:
            problems.append(f"{here}: the id {MAIN_OUTAGE_ID!r} is reserved for the main outage")
            continue
        if marker_id in seen:
            problems.append(f"{here}: duplicate marker id")
            continue
        seen.add(marker_id)
        ok = True
        name = item.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            problems.append(
                f"{here}: name must be 1 to 120 letters, digits, spaces or the characters "
                ",.()'/:- (starting with a letter or digit)"
            )
            ok = False
        approximate = item.get("approximate", False)
        if not isinstance(approximate, bool):
            problems.append(f"{here}: approximate must be true or false")
            ok = False
        time_note = item.get("time_note", "")
        if not isinstance(time_note, str) or not _TIME_NOTE.fullmatch(time_note):
            problems.append(f"{here}: time_note must be at most 300 plain characters on one line")
            ok = False
        notes = item.get("notes", "")
        if not isinstance(notes, str) or not _NOTES.fullmatch(notes):
            problems.append(f"{here}: notes must be at most 2000 plain characters")
            ok = False
        sources = item.get("sources", [])
        if (
            not isinstance(sources, list)
            or len(sources) > MAX_SOURCES
            or not all(isinstance(x, str) and _SOURCE.fullmatch(x) for x in sources)
        ):
            problems.append(
                f"{here}: sources must be a list of at most {MAX_SOURCES} plain names of "
                "1 to 200 characters"
            )
            ok = False
        when = _parse_time(problems, here, "time", item.get("time"))
        if when is None:
            continue
        if when > limit:
            problems.append(f"{here}: time is in the future ({isoformat(when)})")
            ok = False
        if cutoff is not None and baseline_days is not None and min_silence_days is not None:
            earliest = cutoff - timedelta(days=baseline_days)
            latest = cutoff + timedelta(days=min_silence_days)
            if not earliest <= when <= latest:
                problems.append(
                    f"{here}: time {isoformat(when)} must lie between the baseline start "
                    f"{isoformat(earliest)} (cutoff - baseline_days) and "
                    f"{isoformat(latest)} (cutoff + min_silence_days)"
                )
                ok = False
        if out and when < out[-1].time:
            problems.append(
                f"{here}: markers must be listed in time order; {isoformat(when)} is before "
                f"that of {out[-1].id!r} ({out[-1].time_text})"
            )
            ok = False
        if ok:
            out.append(
                EventMarker(
                    id=marker_id,
                    name=str(name),
                    time=when,
                    approximate=bool(approximate),
                    time_note=str(time_note),
                    notes=str(notes),
                    sources=tuple(sources),
                )
            )
    return tuple(out)


def parse_events(
    text: str | bytes,
    *,
    source: str = EVENTS_FILE,
    now: datetime | None = None,
    require_windows: bool = False,
) -> tuple[EventCatalog | None, list[str]]:
    """Parse and validate an events file. Returns ``(catalog, problems)``.

    The catalog is ``None`` whenever there is any problem, so a partly valid file is
    never used. Never raises on bad input. With ``require_windows`` (the registry
    and its gate), every event must write out each of :data:`WINDOW_KEYS`, so its
    windows never depend on defaults in code.
    """
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > MAX_EVENTS_FILE_BYTES:
        return None, [f"{source}: larger than {MAX_EVENTS_FILE_BYTES} bytes"]
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None, [f"{source}: not UTF-8"]
    except (tomllib.TOMLDecodeError, ValueError, RecursionError) as exc:
        return None, [f"{source}: not valid TOML ({type(exc).__name__}: {str(exc)[:200]})"]
    problems: list[str] = []
    for key in sorted(set(data) - _TOP_KEYS):
        problems.append(f"{source}: unknown top-level key {key!r}")
    listed = data.get("event")
    if not isinstance(listed, list) or not listed:
        problems.append(f"{source}: needs at least one [[event]] table")
        return None, problems
    if len(listed) > MAX_EVENTS:
        return None, [*problems, f"{source}: more than {MAX_EVENTS} events"]
    limit = ensure_utc(now or utcnow()) + FUTURE_SLACK
    defaults = _algorithm_defaults()
    events: list[OutageEvent] = []
    seen: set[str] = set()
    previous: OutageEvent | None = None
    for index, item in enumerate(listed, 1):
        where = f"{source}: event {index}"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be a table")
            continue
        for key in sorted(set(item) - _EVENT_KEYS):
            problems.append(f"{where}: unknown key {key!r}")
        event_id = item.get("id")
        if not isinstance(event_id, str) or not re.fullmatch(EVENT_ID_PATTERN, event_id):
            problems.append(f"{where}: id must match {EVENT_ID_PATTERN}")
            continue
        where = f"{source}: event {event_id!r}"
        if event_id.startswith(CUSTOM_PREFIX):
            problems.append(f"{where}: ids starting with {CUSTOM_PREFIX!r} are reserved")
            continue
        if event_id in seen:
            problems.append(f"{where}: duplicate id")
            continue
        seen.add(event_id)
        name = item.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            problems.append(
                f"{where}: name must be 1 to 120 letters, digits, spaces or the characters "
                ",.()'/:- (starting with a letter or digit)"
            )
            name = None
        notes = item.get("notes", "")
        if not isinstance(notes, str) or not _NOTES.fullmatch(notes):
            problems.append(f"{where}: notes must be at most 2000 plain characters")
            notes = ""
        cutoff = _parse_time(problems, where, "cutoff", item.get("cutoff"))
        if cutoff is not None:
            if cutoff < EARLIEST_CUTOFF:
                problems.append(f"{where}: cutoff is before {isoformat(EARLIEST_CUTOFF)}")
            if cutoff > limit:
                problems.append(f"{where}: cutoff is in the future ({isoformat(cutoff)})")
            prefix = _YEAR_MONTH_ID.match(event_id)
            if prefix is not None and (int(prefix[1]), int(prefix[2])) != (
                cutoff.year,
                cutoff.month,
            ):
                problems.append(
                    f"{where}: the id starts with {prefix[1]}-{prefix[2]} but the cutoff is in "
                    f"{cutoff.year:04d}-{cutoff.month:02d}"
                )
        values: dict[str, float | None] = {}
        if require_windows:
            missing = [key for key in WINDOW_KEYS if key not in item]
            if missing:
                problems.append(
                    f"{where}: the shared events file must set {', '.join(missing)} "
                    "(the defaults are 30, 3 and 48)"
                )
        for key, bounds, kind in (
            ("baseline_days", BASELINE_DAYS_RANGE, int),
            ("min_silence_days", MIN_SILENCE_DAYS_RANGE, float),
            ("cliff_window_hours", CLIFF_WINDOW_HOURS_RANGE, float),
        ):
            value = item.get(key, defaults[key])
            values[key] = _check_number(problems, where, key, value, bounds, kind)
        margin = _check_number(
            problems,
            where,
            "marker_margin_hours",
            item.get("marker_margin_hours", DEFAULT_MARKER_MARGIN_HOURS),
            MARKER_MARGIN_HOURS_RANGE,
            float,
        )
        verified = _check_number(
            problems,
            where,
            "verified_min_silence_days",
            item.get("verified_min_silence_days", DEFAULT_VERIFIED_MIN_SILENCE_DAYS),
            VERIFIED_MIN_SILENCE_DAYS_RANGE,
            float,
        )
        silence = values.get("min_silence_days")
        if "verified_min_silence_days" not in item and verified is not None and silence:
            # The default never asks for less silence than a cliff needs.
            verified = max(verified, silence)
        if verified is not None and silence is not None and verified < silence:
            problems.append(
                f"{where}: verified_min_silence_days ({verified:g}) must be at least "
                f"min_silence_days ({silence:g})"
            )
            verified = None
        markers: tuple[EventMarker, ...] = ()
        if "markers" in item:
            markers = _parse_markers(
                problems,
                where,
                item["markers"],
                cutoff=cutoff,
                baseline_days=values.get("baseline_days"),
                min_silence_days=values.get("min_silence_days"),
                limit=limit,
            )
            if require_windows and markers and "marker_margin_hours" not in item:
                problems.append(
                    f"{where}: the shared events file must set marker_margin_hours for an "
                    f"event with markers (the default is {DEFAULT_MARKER_MARGIN_HOURS:g})"
                )
        if (
            cutoff is None
            or name is None
            or margin is None
            or verified is None
            or None in values.values()
        ):
            continue
        event = OutageEvent(
            id=event_id,
            name=name,
            cutoff=cutoff,
            baseline_days=int(values["baseline_days"]),  # type: ignore[arg-type]
            min_silence_days=float(values["min_silence_days"]),  # type: ignore[arg-type]
            cliff_window_hours=float(values["cliff_window_hours"]),  # type: ignore[arg-type]
            notes=notes,
            markers=markers,
            marker_margin_hours=float(margin),
            verified_min_silence_days=float(verified),
        )
        if previous is not None and event.cutoff < previous.cutoff:
            problems.append(
                f"{where}: events must be listed in cutoff order; this cutoff "
                f"({event.cutoff_text}) is before that of {previous.id!r} "
                f"({previous.cutoff_text})"
            )
        previous = event
        events.append(event)
    default = data.get("default")
    if not isinstance(default, str):
        problems.append(f"{source}: 'default' must name one of the events")
    elif default not in seen:
        problems.append(f"{source}: the default event {default!r} is not listed")
    if require_windows and "legacy_event" not in data:
        problems.append(
            f"{source}: the shared events file must set 'legacy_event', so a change of "
            "'default' never moves it"
        )
    legacy = data.get("legacy_event", default)
    if not isinstance(legacy, str) or legacy not in seen:
        problems.append(f"{source}: 'legacy_event' must name one of the events")
    if problems:
        return None, problems
    return EventCatalog(tuple(events), str(default), source, str(legacy)), []


def load_events_file(
    path: Path, *, now: datetime | None = None, require_windows: bool = False
) -> EventCatalog:
    """Read and validate one events file. Raises :class:`EventError`."""
    try:
        size = path.stat().st_size
        if size > MAX_EVENTS_FILE_BYTES:
            raise EventError(f"{path}: larger than {MAX_EVENTS_FILE_BYTES} bytes")
        raw = path.read_bytes()
    except OSError as exc:
        raise EventError(f"cannot read the events file {path}: {exc}") from None
    catalog, problems = parse_events(
        raw, source=str(path), now=now, require_windows=require_windows
    )
    if catalog is None:
        raise EventError("; ".join(problems))
    return catalog


def _checkout_events_path() -> Path | None:
    """``<repo>/events.toml`` when running from a source checkout of this package.

    The same rule as the seeds file (see
    :func:`bot_farm_silence_tracker.seeds._checkout_seeds_path`): the folder must also
    hold this project's ``pyproject.toml``, and a frozen program never looks.
    """
    if getattr(sys, "frozen", False):
        return None
    root = Path(__file__).resolve().parents[2]
    candidate = root / EVENTS_FILE
    pyproject = root / "pyproject.toml"
    if not (candidate.is_file() and pyproject.is_file()):
        return None
    from bot_farm_silence_tracker.seeds import PROJECT_NAME

    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(project, dict) or project.get("name") != PROJECT_NAME:
        return None
    return candidate


def default_events_path(environ: Mapping[str, str] | None = None) -> Path:
    """The events file :func:`load_events` reads (see the module notes)."""
    env = os.environ if environ is None else environ
    override = env.get(EVENTS_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    checkout = _checkout_events_path()
    if checkout is not None:
        return checkout
    return Path(str(resources.files("bot_farm_silence_tracker") / "_data" / EVENTS_FILE))


_CACHE: dict[tuple[str, int, int], EventCatalog] = {}


def load_events(path: Path | None = None) -> EventCatalog:
    """The events of this installation (cached while the file is unchanged)."""
    target = path or default_events_path()
    try:
        stat = target.stat()
    except OSError as exc:
        raise EventError(f"cannot read the events file {target}: {exc}") from None
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is None:
        cached = load_events_file(target)
        _CACHE.clear()
        _CACHE[key] = cached
    return cached


def registry_events(registry: Path) -> EventCatalog:
    """The events of a registry directory: the ``events.toml`` next to it, if any,
    read with the shared file's rules (as the gate reads it), else
    :func:`load_events`."""
    sibling = registry.expanduser().resolve().parent / EVENTS_FILE
    if sibling.is_file():
        return load_events_file(sibling, require_windows=True)
    return load_events()


def events_path_for(registry_path: str) -> str:
    """The events file's path in a repository whose registry is at ``registry_path``."""
    parent = registry_path.strip("/").rpartition("/")[0]
    return f"{parent}/{EVENTS_FILE}" if parent else EVENTS_FILE


# -- comparing two versions (the gate) ------------------------------------------------


@dataclass
class EventChanges:
    """What a change to the events file does."""

    added: list[OutageEvent] = field(default_factory=list)
    removed: list[OutageEvent] = field(default_factory=list)
    #: ``(event id, [(field, before, after), ...])`` for changed guarded fields.
    changed: list[tuple[str, list[tuple[str, Any, Any]]]] = field(default_factory=list)
    #: Events whose notes alone changed.
    notes_changed: list[str] = field(default_factory=list)
    #: ``(event id, marker)`` for markers added to an existing event (no label needed,
    #: unless the addition moves the event's windows; that shows in ``changed``).
    markers_added: list[tuple[str, EventMarker]] = field(default_factory=list)
    #: ``(event id, marker id)`` for markers whose name, time note, notes or sources
    #: alone changed (no label needed).
    markers_described: list[tuple[str, str]] = field(default_factory=list)
    default_before: str | None = None
    default_after: str | None = None
    legacy_before: str | None = None
    legacy_after: str | None = None

    @property
    def guarded(self) -> list[str]:
        """Descriptions of the changes that need the maintainer override.

        Besides the event changes, moving ``legacy_event`` is guarded: it decides
        into which event's folder users' old cache databases are moved.
        """
        out = [f"event {e.id!r} is removed or renamed" for e in self.removed]
        for event_id, diffs in self.changed:
            parts = ", ".join(f"{name} {before!r} -> {after!r}" for name, before, after in diffs)
            out.append(f"event {event_id!r} changes {parts}")
        if self.legacy_changed:
            out.append(f"legacy_event changes from {self.legacy_before!r} to {self.legacy_after!r}")
        return out

    @property
    def default_changed(self) -> bool:
        return self.default_before is not None and self.default_before != self.default_after

    @property
    def legacy_changed(self) -> bool:
        return self.legacy_before is not None and self.legacy_before != self.legacy_after

    @property
    def any(self) -> bool:
        return bool(
            self.added
            or self.removed
            or self.changed
            or self.notes_changed
            or self.markers_added
            or self.markers_described
            or self.default_changed
            or self.legacy_changed
        )


def compare_events(base: EventCatalog | None, head: EventCatalog) -> EventChanges:
    """Compare two versions of the events file. ``base`` ``None``: there was none."""
    before = {e.id: e for e in (base.events if base else ())}
    after = {e.id: e for e in head.events}
    changes = EventChanges(
        added=[after[i] for i in head.ids if i not in before],
        removed=[before[i] for i in (base.ids if base else ()) if i not in after],
        default_before=base.default_id if base else None,
        default_after=head.default_id,
        legacy_before=base.legacy_id if base else None,
        legacy_after=head.legacy_id,
    )
    for event_id in head.ids:
        if event_id not in before:
            continue
        old, new = before[event_id].guarded(), after[event_id].guarded()
        diffs = [(name, old[name], new[name]) for name in GUARDED_FIELDS if old[name] != new[name]]
        fields_changed = bool(diffs)
        diffs += _marker_diffs(
            before[event_id], after[event_id], event_id, changes, windows=not fields_changed
        )
        if diffs:
            changes.changed.append((event_id, diffs))
        elif before[event_id].notes != after[event_id].notes:
            changes.notes_changed.append(event_id)
    return changes


def _marker_diffs(
    old: OutageEvent,
    new: OutageEvent,
    event_id: str,
    changes: EventChanges,
    *,
    windows: bool = True,
) -> list[tuple[str, Any, Any]]:
    """The guarded marker changes of one event; free ones are recorded in ``changes``.

    The policy: a marker whose time or ``approximate`` flag changes, or that is
    removed, can re-label accounts (their nearest marker), so it is guarded. Adding a
    marker is free, and so is a change of a marker's name, time note, notes or
    sources. But any change that moves the event's windows (the baseline end or the
    silence window start, for example a new earliest marker) is a window change, and
    window changes are guarded. With ``windows`` false (a guarded field changed
    already, which moves the windows anyway) the window diffs are left out.
    """
    diffs: list[tuple[str, Any, Any]] = []
    before = {m.id: m for m in old.markers}
    after = {m.id: m for m in new.markers}
    for marker_id, marker in before.items():
        if marker_id not in after:
            diffs.append((f"marker {marker_id}", marker.time_text, "removed"))
            continue
        other = after[marker_id]
        if ensure_utc(other.time) != ensure_utc(marker.time):
            diffs.append((f"marker {marker_id} time", marker.time_text, other.time_text))
        if other.approximate != marker.approximate:
            diffs.append((f"marker {marker_id} approximate", marker.approximate, other.approximate))
        if (other.name, other.time_note, other.notes, other.sources) != (
            marker.name,
            marker.time_note,
            marker.notes,
            marker.sources,
        ):
            changes.markers_described.append((event_id, marker_id))
    for marker_id, marker in after.items():
        if marker_id not in before:
            changes.markers_added.append((event_id, marker))
    if not windows:
        return diffs
    old_windows, new_windows = old.windows(), new.windows()
    for name, a, b in zip(
        ("baseline end", "silence window start"), old_windows[1:], new_windows[1:], strict=True
    ):
        if a != b:
            diffs.append((name, isoformat(a), isoformat(b)))
    return diffs


def event_of_run(params: Mapping[str, Any], catalog: EventCatalog) -> OutageEvent:
    """The event a stored run belongs to (its ``params``).

    A run stores its ``event_id``. A run made before events existed is matched by its
    cutoff (:meth:`EventCatalog.for_cutoff`). Raises :class:`EventError` when the
    stored event is no longer in the events file.
    """
    stored = params.get("event_id")
    scoring = params.get("scoring") or {}
    try:
        cutoff: datetime | None = parse_datetime(str(scoring["cutoff"]))
    except (KeyError, ValueError):
        cutoff = None
    if isinstance(stored, str) and stored:
        event = catalog.resolve(stored)
        if cutoff is not None and not event.custom and ensure_utc(cutoff) != event.cutoff:
            # Scanned under an earlier definition of the event: the run keeps its own
            # cutoff and belongs to the local event of it, never to the new definition.
            return legacy_event(event, cutoff)
        return event
    if cutoff is None:
        return catalog.default
    return catalog.for_cutoff(cutoff)


def legacy_event(event: OutageEvent, cutoff: datetime) -> OutageEvent:
    """The local event of runs scanned with ``cutoff`` under an earlier definition of
    the shared ``event``: the custom event of that cutoff, named after ``event``."""
    custom = custom_event(ensure_utc(cutoff), allow_seconds=True)
    return replace(
        custom,
        name=f"Earlier definition of {event.id} (cutoff {custom.cutoff_text})",
        notes=(
            f"Runs scanned with the cutoff {custom.cutoff_text}, which the shared event "
            f"{event.id} had before its cutoff changed to {event.cutoff_text}. They keep "
            "their own cutoff and windows, stay local and are never shared."
        ),
        legacy_of=event.id,
    )


def describe(event: OutageEvent) -> str:
    """One line naming an event and its cutoff."""
    return f"{event.id} ({event.name}), cutoff {event.cutoff_text}"


def describe_markers(event: OutageEvent) -> list[str]:
    """The lines ``events show`` and the app print about an event's markers and windows.

    Marker times are as the sources reported them.
    """
    if not event.markers:
        return []
    baseline_start, baseline_end, window_start = event.windows()
    lines = [f"markers ({len(event.markers)}; times as reported, not independently verified):"]
    for marker in event.markers:
        lines.append(f"  {marker.id}: {marker.name}")
        lines.append(f"    time: {marker.time_text}" + (" (approx.)" if marker.approximate else ""))
        if marker.time_note:
            lines.append(f"    time note: {marker.time_note}")
        if marker.notes:
            lines.append(f"    notes: {marker.notes}")
        if marker.sources:
            lines.append(f"    sources: {', '.join(marker.sources)}")
    lines += [
        f"marker_margin_hours: {event.marker_margin_hours:g}",
        f"baseline window: {isoformat(baseline_start)} to {isoformat(baseline_end)}",
        f"silence window: {isoformat(window_start)} to {event.cutoff_text}",
    ]
    return lines
