"""The README chart: potential bot accounts that have stayed silent since they stopped.

``registry chart`` renders one SVG per outage event and colour theme from the registry
files alone, after the same validation the registry gate uses
(:func:`~bot_farm_silence_tracker.registry.records.validate_registry`). It is written
by hand with no plotting library, so the standalone binaries can produce it too.

What is charted
---------------
The records of one event that claim silence and that no reviewer has cleared:

* ``verified_inauthentic``: a person checked the evidence ("verified");
* ``candidate``: an automatic label, a cliff with at least one signal;
* ``scanned`` with ``cliff`` set: an automatic label, silent since the takedown.

Each record's ``last_activity_at`` is its stop: the newest activity the scan saw, so
the account has had no activity since. ``cleared`` records and ``scanned`` records
without a cliff are left out; the cleared ones are counted in the header.

The chart has two panels. The main panel is the cumulative number of these accounts
by stop time, stacked by platform, from the start of the event's baseline window to
the end of the ``as_of`` day, with a vertical line at the cutoff and at each marker
(dashed when the marker's time is approximate). The second panel is a histogram of
the stop times in hours relative to the cutoff; with markers, each bar is split by
the anchor (the main outage or a marker) the stop lies nearest to
(:func:`~bot_farm_silence_tracker.scoring.nearest_marker`). Many accounts stopping
together show as a spike.

Privacy and safety
------------------
The chart shows counts only, never a username, handle or ID. Every string written
into the SVG (event and marker names come from ``events.toml``) is XML-escaped, and
characters XML cannot carry are dropped.

Determinism
-----------
The output depends only on the records, the event, the ``as_of`` date, the commit and
the theme: the same input gives the same bytes, so the committed charts diff cleanly.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.events import EventCatalog, OutageEvent
from bot_farm_silence_tracker.registry.records import (
    PLATFORMS,
    STATUS_CANDIDATE,
    STATUS_CLEARED,
    STATUS_SCANNED,
    STATUS_VERIFIED,
    RegistryTree,
    validate_registry,
)
from bot_farm_silence_tracker.registry.verify import load_tree
from bot_farm_silence_tracker.scoring import MAIN_OUTAGE, Marker, nearest_marker
from bot_farm_silence_tracker.timeutil import ensure_utc, parse_datetime

#: The file name stem of the default event's charts, which the README shows.
DEFAULT_STEM = "default"
THEMES = ("light", "dark")

#: Status groups, in the order the header lists them.
GROUP_VERIFIED = "verified"
GROUP_CANDIDATE = "candidate"
GROUP_SILENT = "silent"
GROUP_LABELS = {
    GROUP_VERIFIED: "verified by a person",
    GROUP_CANDIDATE: "candidate (automatic)",
    GROUP_SILENT: "silent since takedown (automatic)",
}
PLATFORM_LABELS = {
    "bluesky": "Bluesky",
    "reddit": "Reddit",
    "reddit-archive": "Reddit (archive)",
    "x": "X",
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: Colours by theme. The categorical slots are a colour-blind-safe order (validated
#: for adjacent pairs, as in a stack); each platform keeps its slot whatever is
#: present, and the legend names every series with its count.
PALETTES: dict[str, dict[str, Any]] = {
    "light": {
        "surface": "#fcfcfb",
        "text": "#0b0b0b",
        "text2": "#52514e",
        "muted": "#6b6a65",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "platforms": ("#2a78d6", "#eb6834", "#1baf7a", "#eda100"),
        "anchors": ("#4a3aa7", "#e87ba4", "#008300", "#e34948"),
    },
    "dark": {
        "surface": "#1a1a19",
        "text": "#ffffff",
        "text2": "#c3c2b7",
        "muted": "#a3a29b",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "platforms": ("#3987e5", "#d95926", "#199e70", "#c98500"),
        "anchors": ("#9085e9", "#d55181", "#008300", "#e66767"),
    },
}

WIDTH = 960
LEFT = 64
RIGHT = WIDTH - 32
MAIN_HEIGHT = 240
HIST_HEIGHT = 140
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
EMPTY_TITLE = "No verified data yet"

_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]")


class ChartError(ValueError):
    """The registry cannot be charted (it is invalid, or the event is unknown)."""


def esc(text: object) -> str:
    """``text`` safe inside SVG text and double- or single-quoted attributes."""
    value = _XML_INVALID.sub("", str(text))
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _num(value: float) -> str:
    """A coordinate with one decimal and no ``-0.0``."""
    text = f"{value:.1f}"
    return "0.0" if text == "-0.0" else text


def _when(moment: datetime) -> str:
    """``Oct 8 06:56 UTC``."""
    m = ensure_utc(moment)
    return f"{_MONTHS[m.month - 1]} {m.day} {m.hour:02d}:{m.minute:02d} UTC"


def _day(moment: datetime | date) -> str:
    """``Oct 8``."""
    return f"{_MONTHS[moment.month - 1]} {moment.day}"


def group_of(record: dict[str, Any]) -> str | None:
    """The status group a record is charted in, or ``None`` when it is left out."""
    status = record["status"]
    if status == STATUS_VERIFIED:
        return GROUP_VERIFIED
    if status == STATUS_CANDIDATE:
        return GROUP_CANDIDATE
    if status == STATUS_SCANNED and record["cliff"]:
        return GROUP_SILENT
    return None


@dataclass(frozen=True)
class Stop:
    """One charted account, reduced to what the chart shows: no identity."""

    platform: str
    group: str
    at: datetime


@dataclass(frozen=True)
class ChartData:
    """The aggregate input of one event's chart."""

    event: OutageEvent
    stops: tuple[Stop, ...]
    #: Every record of the event (the footer's N).
    records: int
    cleared: int
    as_of: date
    commit: str = ""

    @property
    def empty(self) -> bool:
        return not self.stops


def chart_data(
    event: OutageEvent,
    records: Iterable[dict[str, Any]],
    *,
    as_of: date,
    commit: str = "",
) -> ChartData:
    """Reduce an event's records to stops by platform and group (no identities)."""
    stops: list[Stop] = []
    total = cleared = 0
    for record in records:
        if record["event_id"] != event.id:
            continue
        total += 1
        if record["status"] == STATUS_CLEARED:
            cleared += 1
        group = group_of(record)
        if group is not None:
            stop = ensure_utc(parse_datetime(record["last_activity_at"]))
            stops.append(Stop(record["platform"], group, stop))
    stops.sort(key=lambda s: (s.at, PLATFORMS.index(s.platform), s.group))
    return ChartData(event, tuple(stops), total, cleared, as_of, commit[:7])


def _nice_step(span: float, ticks: int = 4) -> int:
    """A 1, 2 or 5 times a power of ten step giving about ``ticks`` intervals."""
    raw = max(span / ticks, 1.0)
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if factor * power >= raw:
            return int(factor * power)
    return int(10 * power)  # pragma: no cover


def _anchors(event: OutageEvent) -> list[tuple[Marker, bool]]:
    """The cutoff and the markers, in time order, each with whether it is the cutoff."""
    out = [(Marker(MAIN_OUTAGE, "Main outage (cutoff)", ensure_utc(event.cutoff), False), True)]
    out += [(m.anchor(), False) for m in event.markers]
    return sorted(out, key=lambda item: (item[0].time, not item[1]))


class _Svg:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def add(self, text: str) -> None:
        self.parts.append(text)

    def text(
        self,
        x: float,
        y: float,
        content: str,
        *,
        size: int = 13,
        fill: str,
        anchor: str = "start",
        weight: str = "normal",
    ) -> None:
        attrs = f'x="{_num(x)}" y="{_num(y)}" font-size="{size}" fill="{fill}"'
        if anchor != "start":
            attrs += f' text-anchor="{anchor}"'
        if weight != "normal":
            attrs += f' font-weight="{weight}"'
        self.add(f"<text {attrs}>{esc(content)}</text>")

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        stroke: str,
        width: float = 1.0,
        dash: str = "",
    ) -> None:
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(
            f'<line x1="{_num(x1)}" y1="{_num(y1)}" x2="{_num(x2)}" y2="{_num(y2)}" '
            f'stroke="{stroke}" stroke-width="{width:g}"{extra}/>'
        )


def render_svg(data: ChartData, theme: str = "light") -> str:
    """The chart of ``data`` as an SVG document (see the module notes)."""
    if theme not in PALETTES:
        raise ChartError(f"unknown theme {theme!r}")
    pal = PALETTES[theme]
    event = data.event
    cutoff = ensure_utc(event.cutoff)
    baseline_start, _, window_start = event.windows()
    start = ensure_utc(baseline_start)
    end = datetime.combine(data.as_of, time(0), tzinfo=UTC) + timedelta(days=1)
    if data.stops:
        end = max(end, data.stops[-1].at + timedelta(hours=1))
    end = max(end, cutoff + timedelta(days=1))
    anchors = _anchors(event)
    svg = _Svg()

    platforms = [p for p in PLATFORMS if any(s.platform == p for s in data.stops)]
    counts = {p: sum(1 for s in data.stops if s.platform == p) for p in platforms}
    groups = {g: sum(1 for s in data.stops if s.group == g) for g in GROUP_LABELS}
    total = len(data.stops)

    # -- header ---------------------------------------------------------------------
    title = f"Potential bot accounts silent since the {event.name}"
    y = 34.0
    svg.text(LEFT - 40, y, title, size=20, fill=pal["text"], weight="bold")
    y += 22
    svg.text(
        LEFT - 40,
        y,
        "Registry accounts with no activity since they stopped posting, cumulative by "
        "stop time. Event " + event.id + ".",
        size=13,
        fill=pal["text2"],
    )
    y += 24
    status = f"{total} potential bot accounts: " + ", ".join(
        f"{groups[g]} {GROUP_LABELS[g]}" for g in GROUP_LABELS
    )
    if data.cleared:
        status += f"; {data.cleared} cleared by a reviewer (not charted)"
    svg.text(LEFT - 40, y, status + ".", size=14, fill=pal["text"])
    if platforms:
        y += 22
        x = LEFT - 40.0
        for p in platforms:
            colour = pal["platforms"][PLATFORMS.index(p)]
            svg.add(
                f'<rect x="{_num(x)}" y="{_num(y - 10)}" width="12" height="12" rx="2" '
                f'fill="{colour}"/>'
            )
            label = f"{PLATFORM_LABELS[p]} {counts[p]}"
            svg.text(x + 18, y, label, size=13, fill=pal["text"])
            x += 18 + 8.0 * len(label) + 24

    # -- anchor labels, one row each, above the main panel ----------------------------
    y += 14
    label_rows: list[float] = []
    for _ in anchors:
        y += 17
        label_rows.append(y)
    top = y + 10
    bottom = top + MAIN_HEIGHT
    span = (end - start).total_seconds()

    def x_of(moment: datetime) -> float:
        seconds = (ensure_utc(moment) - start).total_seconds()
        return LEFT + (RIGHT - LEFT) * min(max(seconds / span, 0.0), 1.0)

    # -- main panel -------------------------------------------------------------------
    y_max_value = max(total, 1)
    step = _nice_step(y_max_value)
    y_top = step * math.ceil(y_max_value / step)

    def y_of(value: float) -> float:
        return bottom - (bottom - top) * value / y_top

    for tick in range(0, y_top + 1 if data.stops else 1, step):
        ty = y_of(tick)
        svg.line(LEFT, ty, RIGHT, ty, stroke=pal["grid"] if tick else pal["axis"])
        svg.text(LEFT - 8, ty + 4, str(tick), size=12, fill=pal["muted"], anchor="end")
    days = int(span // 86400)
    day_step = 7 if days > 21 else (2 if days > 7 else 1)
    for d in range(0, days + 1, day_step):
        moment = start + timedelta(days=d)
        tx = x_of(moment)
        svg.line(tx, bottom, tx, bottom + 4, stroke=pal["axis"])
        svg.text(tx, bottom + 18, f"day {d}", size=12, fill=pal["muted"], anchor="middle")
        svg.text(tx, bottom + 32, _day(moment), size=12, fill=pal["muted"], anchor="middle")
    svg.text(
        (LEFT + RIGHT) / 2,
        bottom + 50,
        f"Days from the baseline start ({_day(start)}) to {_day(data.as_of)} {data.as_of.year}"
        " (UTC)",
        size=12,
        fill=pal["text2"],
        anchor="middle",
    )
    svg.add(
        f'<text transform="translate({_num(LEFT - 44)} {_num((top + bottom) / 2)}) rotate(-90)" '
        f'font-size="12" fill="{pal["text2"]}" text-anchor="middle">Accounts (cumulative)</text>'
    )

    if data.stops:
        times = sorted({max(s.at, start) for s in data.stops})
        below = [0] * len(times)
        for p in platforms:
            colour = pal["platforms"][PLATFORMS.index(p)]
            own = [max(s.at, start) for s in data.stops if s.platform == p]
            level = []
            for i, t in enumerate(times):
                level.append(below[i] + sum(1 for o in own if o <= t))
            upper = _steps(times, level, x_of, y_of, end)
            lower = _steps(times, below, x_of, y_of, end)
            points = upper + list(reversed(lower))
            path = " ".join(f"{_num(px)},{_num(py)}" for px, py in points)
            svg.add(
                f'<polygon points="{path}" fill="{colour}" stroke="{pal["surface"]}" '
                'stroke-width="1" stroke-linejoin="round"/>'
            )
            below = level
    else:
        mid = (top + bottom) / 2
        svg.text(
            (LEFT + RIGHT) / 2,
            mid - 4,
            EMPTY_TITLE,
            size=20,
            fill=pal["text"],
            anchor="middle",
            weight="bold",
        )
        svg.text(
            (LEFT + RIGHT) / 2,
            mid + 20,
            "No potential bot accounts are in the registry for this event yet.",
            size=13,
            fill=pal["text2"],
            anchor="middle",
        )

    for (marker, is_cutoff), row in zip(anchors, label_rows, strict=True):
        ax = x_of(marker.time)
        colour = pal["text"] if is_cutoff else pal["text2"]
        svg.line(
            ax,
            row + 4,
            ax,
            bottom,
            stroke=colour,
            width=1.5 if is_cutoff else 1.0,
            dash="5 4" if marker.approximate else "",
        )
        label = f"{marker.name}, {_when(marker.time)}"
        if marker.approximate:
            label += " (approx.)"
        right = ax > LEFT + 0.6 * (RIGHT - LEFT)
        svg.text(
            ax - 6 if right else ax + 6,
            row,
            label,
            size=12,
            fill=colour,
            anchor="end" if right else "start",
        )

    # -- histogram of stop times relative to the cutoff -------------------------------
    y = bottom + 84
    lo_hours = (ensure_utc(window_start) - cutoff).total_seconds() / 3600
    hi_hours = max(48.0, float(event.min_silence_days) * 24)
    bin_hours = 6 if hi_hours - lo_hours <= 240 else 24
    lo = bin_hours * math.floor(lo_hours / bin_hours)
    hi = bin_hours * math.ceil(hi_hours / bin_hours)
    bins = int((hi - lo) // bin_hours)
    svg.text(
        LEFT - 40,
        y,
        f"Stop times in hours relative to the cutoff ({bin_hours}-hour bins). Accounts that "
        "stopped together show as a spike.",
        size=14,
        fill=pal["text"],
    )
    scoring_anchors = event.anchors()
    order = {m.id: i for i, m in enumerate(scoring_anchors)}
    cells: dict[tuple[int, str], int] = {}
    earlier = later = 0
    near_counts: dict[str, int] = {}
    for s in data.stops:
        hours = (s.at - cutoff).total_seconds() / 3600
        if hours < lo:
            earlier += 1
            continue
        if hours >= hi:
            later += 1
            continue
        index = int((hours - lo) // bin_hours)
        near = nearest_marker(s.at, scoring_anchors) if scoring_anchors else None
        key = near.marker.id if near is not None else MAIN_OUTAGE
        cells[(index, key)] = cells.get((index, key), 0) + 1
        near_counts[key] = near_counts.get(key, 0) + 1
    if scoring_anchors and data.stops:
        y += 20
        x = LEFT - 40.0
        svg.text(x, y, "Nearest to:", size=13, fill=pal["text2"])
        x += 82
        for m in scoring_anchors:
            colour = pal["anchors"][order[m.id] % len(pal["anchors"])]
            svg.add(
                f'<rect x="{_num(x)}" y="{_num(y - 10)}" width="12" height="12" rx="2" '
                f'fill="{colour}"/>'
            )
            label = f"{m.name} {near_counts.get(m.id, 0)}"
            svg.text(x + 18, y, label, size=13, fill=pal["text"])
            x += 18 + 7.4 * len(label) + 22
    htop = y + 16
    hbottom = htop + HIST_HEIGHT
    peak = max([sum(v for (i, _), v in cells.items() if i == b) for b in range(bins)] + [1])
    hstep = _nice_step(peak, 3)
    h_top = hstep * math.ceil(peak / hstep)
    bin_w = (RIGHT - LEFT) / bins

    def hx(hours: float) -> float:
        return LEFT + (RIGHT - LEFT) * (hours - lo) / (hi - lo)

    def hy(value: float) -> float:
        return hbottom - (hbottom - htop) * value / h_top

    for tick in range(0, h_top + 1 if data.stops else 1, hstep):
        ty = hy(tick)
        svg.line(LEFT, ty, RIGHT, ty, stroke=pal["grid"] if tick else pal["axis"])
        svg.text(LEFT - 8, ty + 4, str(tick), size=12, fill=pal["muted"], anchor="end")
    tick_hours = 24 if hi - lo > 72 else 12
    first = tick_hours * math.ceil(lo / tick_hours)
    for h in range(int(first), int(hi) + 1, tick_hours):
        tx = hx(h)
        svg.line(tx, hbottom, tx, hbottom + 4, stroke=pal["axis"])
        label = "0" if h == 0 else f"{h:+d}h"
        svg.text(tx, hbottom + 18, label, size=12, fill=pal["muted"], anchor="middle")
    keys = [m.id for m in scoring_anchors] or [MAIN_OUTAGE]
    for b in range(bins):
        level = 0
        for key in keys:
            value = cells.get((b, key), 0)
            if not value:
                continue
            colour = (
                pal["anchors"][order[key] % len(pal["anchors"])]
                if scoring_anchors
                else pal["anchors"][0]
            )
            y1, y0 = hy(level + value), hy(level)
            svg.add(
                f'<rect x="{_num(LEFT + b * bin_w + 1)}" y="{_num(y1)}" '
                f'width="{_num(bin_w - 2)}" height="{_num(y0 - y1)}" fill="{colour}" '
                f'stroke="{pal["surface"]}" stroke-width="1"/>'
            )
            level += value
    for marker, is_cutoff in anchors:
        hours = (marker.time - cutoff).total_seconds() / 3600
        if lo <= hours <= hi:
            svg.line(
                hx(hours),
                htop,
                hx(hours),
                hbottom,
                stroke=pal["text"] if is_cutoff else pal["text2"],
                width=1.5 if is_cutoff else 1.0,
                dash="5 4" if marker.approximate else "",
            )
    if not data.stops:
        svg.text(
            (LEFT + RIGHT) / 2,
            (htop + hbottom) / 2,
            "No stop times yet",
            size=14,
            fill=pal["text2"],
            anchor="middle",
        )
    y = hbottom + 36
    if earlier or later:
        svg.text(
            LEFT,
            y,
            f"Outside the range: {earlier} earlier, {later} later.",
            size=12,
            fill=pal["text2"],
        )
        y += 18

    # -- caption and footer -----------------------------------------------------------
    y += 6
    svg.text(
        LEFT - 40,
        y,
        "Silence alone is not proof that an account is a bot. “Candidate” and "
        "“silent since takedown” are automatic labels",
        size=13,
        fill=pal["text"],
    )
    y += 18
    svg.text(
        LEFT - 40,
        y,
        "that mark leads for human review; only “verified” means a person checked "
        "the evidence. Dashed lines are approximate times.",
        size=13,
        fill=pal["text"],
    )
    y += 26
    footer = (
        f"Last updated {data.as_of.isoformat()} (UTC) from {data.records} records in {event.id}"
    )
    if data.commit:
        footer += f" at commit {data.commit}"
    svg.text(LEFT - 40, y, footer + ".", size=12, fill=pal["muted"])
    height = int(y + 20)

    if data.empty:
        desc = (
            f"{EMPTY_TITLE}: the registry has no potential bot accounts for the event "
            f"{event.id} ({event.name}) yet."
        )
    else:
        by_platform = ", ".join(f"{counts[p]} on {PLATFORM_LABELS[p]}" for p in platforms)
        desc = (
            f"{status}. By platform: {by_platform}. Cutoff {_when(cutoff)}"
            + "".join(f"; marker {m.name} {_when(m.time)}" for m, c in anchors if not c)
            + "."
        )
    head = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="t d" '
        f'font-family="{esc(FONT)}">\n'
        f'<title id="t">{esc(title)}</title>\n'
        f'<desc id="d">{esc(desc)}</desc>\n'
        f'<rect width="{WIDTH}" height="{height}" rx="8" fill="{pal["surface"]}"/>\n'
    )
    return head + "\n".join(svg.parts) + "\n</svg>\n"


def _steps(
    times: Sequence[datetime],
    values: Sequence[int],
    x_of: Any,
    y_of: Any,
    end: datetime,
) -> list[tuple[float, float]]:
    """The vertices of a step line: 0 until the first time, then each value onwards."""
    points = [(x_of(times[0]), y_of(0))]
    for i, t in enumerate(times):
        x = x_of(t)
        if i:
            points.append((x, y_of(values[i - 1])))
        points.append((x, y_of(values[i])))
    points.append((x_of(end), y_of(values[-1])))
    if points[0][0] > LEFT:
        points.insert(0, (LEFT, y_of(0)))
    return points


def load_chart_records(
    root: Path, *, now: datetime | None = None
) -> tuple[EventCatalog, list[dict[str, Any]]]:
    """The events and the records of a registry directory, after the gate's validation.

    Raises :class:`ChartError` when the registry or its events file is invalid.
    """
    tree = RegistryTree(root)
    try:
        problems = validate_registry(tree=tree, now=now)
    except ValueError as exc:
        raise ChartError(str(exc)) from None
    if problems:
        raise ChartError(f"the registry is invalid ({len(problems)} problems): {problems[0]}")
    catalog, event_problems = tree.events(now=now)
    if catalog is None:
        raise ChartError(f"the events file is invalid: {'; '.join(event_problems)}")
    records, load_problems = load_tree(tree, now=now, events=catalog)
    if load_problems:
        raise ChartError(f"the registry is invalid: {load_problems[0]}")
    return catalog, [records[key] for key in sorted(records)]


def write_charts(
    root: Path,
    out: Path,
    *,
    as_of: date,
    commit: str = "",
    event_ids: Sequence[str] = (),
    now: datetime | None = None,
) -> list[Path]:
    """Write ``<event>-<theme>.svg`` for each event (all by default) and
    ``default-<theme>.svg`` for the default event. Returns the paths written."""
    catalog, records = load_chart_records(root, now=now)
    wanted = list(event_ids) or list(catalog.ids)
    for event_id in wanted:
        if catalog.get(event_id) is None:
            raise ChartError(f"unknown event {event_id!r}; known events: {', '.join(catalog.ids)}")
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for event_id in wanted:
        event = catalog.get(event_id)
        assert event is not None
        data = chart_data(event, records, as_of=as_of, commit=commit)
        stems = [event_id] + ([DEFAULT_STEM] if event_id == catalog.default_id else [])
        for theme in THEMES:
            text = render_svg(data, theme)
            for stem in stems:
                path = out / f"{stem}-{theme}.svg"
                path.write_text(text, encoding="utf-8", newline="\n")
                written.append(path)
    return written
