"""Render the provisional run chart from a JSON file of aggregate counts.

Usage: render_run_chart.py AGGREGATES_JSON OUTPUT_DIR

Writes ``runs-provisional-light.svg`` and ``runs-provisional-dark.svg`` into OUTPUT_DIR.
The input holds counts only, never an account name, handle or ID. The chart compares the
share of eligible accounts that went silent in each run, in the runs pooled once per
distinct account, and in ordinary weeks, with 95% Wilson intervals. The same input always
gives the same bytes.

This is the main graph of the README, and every platform's results go on it. Each row
names its platform (``platform``, which defaults to ``reddit-archive`` so that older
files keep working), and the chart draws one labelled group per platform that has at
least one row, in the order of :data:`PLATFORM_LABELS`. Rates are never pooled across
platforms, because each platform samples a different population with a different base
rate. A platform's ordinary-week rows and the rough matched base of its runs come from
that platform's own base rates only: ``base_rate`` holds those of ``reddit-archive``
and ``base_rates`` maps any platform to its own. A platform without base rates gets no
ordinary-week rows and no matched base, and its rows say so.

A base may carry a ``definition``: text saying that it was counted with another
definition than the runs (for example another window or no event markers). Its
ordinary-week rows are then drawn as a reference only, and no matched base is
interpolated from it. A row may instead give its own ``matched_base`` (``rate``, ``low``
and ``high``), measured under the runs' own definition, which the chart draws as the
row's matched base.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.registry.chart import FONT, PALETTES, THEMES, esc

Z95 = 1.959963984540054
WIDTH = 960
PLOT_LEFT = 340
PLOT_RIGHT = 920
#: The smallest axis maximum. The axis grows, in whole tick steps, when a row needs more.
AXIS_MAX = 0.06
#: Tick steps in percentage points, the smallest that keeps at most ten ticks is used.
TICK_STEPS = (1, 2, 5, 10, 20, 25, 50)
#: The band behind the ordinary-week rows, visible against each theme surface.
BANDS = {"light": "#e1e0d9", "dark": "#3f3f3b"}
ROW_TOP = 172
ROW_STEP = 56
#: The vertical space a platform's group header takes above its first row.
GROUP_STEP = 34
#: The matched base is interpolated between the 3-day and 7-day base rates only.
MATCH_MIN_DAYS = 3.0
MATCH_MAX_DAYS = 7.0
#: Row kinds that get a series colour; the ordinary-week rows use the secondary text colour.
KINDS = ("run", "union", "partial", "pooled")
#: The platform of a row that names none, so files written before rows had one still work.
DEFAULT_PLATFORM = "reddit-archive"
#: The platforms the chart can show, in the order their groups are drawn, with the label
#: of each group. These are the platforms of the shared registry.
PLATFORM_LABELS = {
    "reddit-archive": "Reddit archive",
    "reddit": "Reddit",
    "bluesky": "Bluesky",
    "x": "X",
}
#: The description of the ``reddit-archive`` base rates when ``base_rate`` gives none.
LEGACY_BASE_NOTE = "three Sundays in Sep 2026, pooled"
#: The keys the file may hold, by place. Any other key is refused, so a file cannot carry
#: account names or IDs under a key the chart does not know about.
TOP_KEYS = frozenset(
    {"event", "as_of", "cutoff", "base_rate", "base_rates", "rows", "union_rule", "rule_note"}
)
ROW_KEYS = frozenset(
    {
        "label",
        "platform",
        "kind",
        "status",
        "scored_through",
        "fetched",
        "planned",
        "distinct_accounts",
        "eligible",
        "silent",
        "windows",
        "window_range",
        "mixed_windows",
        "method_note",
        "matched_base",
    }
)
WINDOW_KEYS = frozenset({"days", "eligible"})
BASE_KEYS = frozenset({"note", "definition", "silent_3d", "silent_7d"})
MATCHED_KEYS = frozenset({"rate", "low", "high"})
RATE_KEYS = frozenset({"events", "eligible", "weekly_rates"})
#: Text that looks like an account identity: a Reddit ``u/name``, ``/u/name`` or
#: ``/user/name`` (any case), an ``@name`` mention whose name has a letter (so a time such
#: as ``@03:00`` is not one), a link to a profile or post on Reddit, X or Bluesky, a
#: Bluesky handle or DID (``did:plc:`` or ``did:web:``), or a Reddit account ID
#: (``t2_``). Any string value of the file that matches is refused.
HANDLE_LIKE = re.compile(
    r"(?i:(?<![\w/])/?(?:u|user)/[a-z0-9_-]{3,})"
    r"|(?<![\w@])@(?=[0-9._]*[A-Za-z])[A-Za-z0-9_.]{2,}"
    r"|(?i:\b(?:[a-z0-9-]+\.)*(?:reddit\.com|redd\.it|x\.com|twitter\.com|bsky\.app)/\S)"
    r"|(?i:\b[a-z0-9-]+\.bsky\.social\b)"
    r"|(?i:\bdid:(?:plc|web):[a-z0-9._:%-]+)"
    r"|(?i:\bt2_[a-z0-9]{2,}\b)"
)
#: How far a pooled base rate may lie outside its rounded weekly rates.
WEEKLY_TOLERANCE = 5e-5


def wilson(successes: int, total: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for a proportion, as (low, high)."""
    if total <= 0:
        raise ValueError("total must be positive")
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _pct(rate: float, digits: int = 1) -> str:
    return f"{rate * 100:.{digits}f}%"


def row_platform(entry: dict[str, Any]) -> str:
    """The platform of a row: its ``platform``, or :data:`DEFAULT_PLATFORM`."""
    return entry.get("platform", DEFAULT_PLATFORM)


def platform_label(platform: str) -> str:
    """The label of a platform's group, for example ``Reddit archive``."""
    return PLATFORM_LABELS[platform]


def _is_count(value: Any) -> bool:
    """A whole, non-negative count (``True`` and ``False`` are not counts)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_days(value: Any) -> bool:
    """A finite, positive number of days."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _check_base(platform: str, base: Any) -> None:
    if not isinstance(base, dict):
        raise ValueError(f"the base rates of {platform!r} must be an object")
    definition = base.get("definition")
    if definition is not None and (not isinstance(definition, str) or not definition):
        raise ValueError(f"the base rates of {platform!r} need definition to be text")
    for key in ("silent_3d", "silent_7d"):
        rate = base.get(key)
        if not isinstance(rate, dict):
            raise ValueError(f"the base rates of {platform!r} need {key}")
        events, eligible = rate.get("events"), rate.get("eligible")
        weekly = rate.get("weekly_rates")
        if not (_is_count(events) and _is_count(eligible) and events <= eligible):
            raise ValueError(f"{platform!r} {key} needs whole counts with events <= eligible")
        if eligible <= 0:
            raise ValueError(f"{platform!r} {key} needs a positive eligible count")
        if not (
            isinstance(weekly, list)
            and weekly
            and all(
                isinstance(w, int | float) and not isinstance(w, bool) and 0 <= w <= 1
                for w in weekly
            )
        ):
            raise ValueError(f"{platform!r} {key} needs a list of weekly rates between 0 and 1")
        pooled = events / eligible
        if not min(weekly) - WEEKLY_TOLERANCE <= pooled <= max(weekly) + WEEKLY_TOLERANCE:
            raise ValueError(
                f"{platform!r} {key}: the pooled rate {_pct(pooled, 2)} lies outside its "
                "weekly rates, so the counts and the weekly rates do not belong together"
            )


def base_rates(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The ordinary-week base rates of each platform that has them.

    ``base_rate`` is the base of ``reddit-archive`` (the only base the first files had),
    and ``base_rates`` maps a platform to its own. Each base gets a ``note``: its own,
    or :data:`LEGACY_BASE_NOTE` for ``base_rate``. A platform given twice is refused.
    """
    extra = data.get("base_rates", {})
    if not isinstance(extra, dict):
        raise ValueError("base_rates must map a platform to its base rates")
    rates: dict[str, dict[str, Any]] = {}
    for platform, base in extra.items():
        if platform not in PLATFORM_LABELS:
            raise ValueError("base_rates has a key that is not a platform")
        _check_base(platform, base)
        if not isinstance(base.get("note"), str) or not base["note"]:
            raise ValueError(f"the base rates of {platform!r} need a note saying what they are")
        rates[platform] = base
    if "base_rate" in data:
        if DEFAULT_PLATFORM in rates:
            raise ValueError(f"{DEFAULT_PLATFORM!r} has base rates in base_rate and base_rates")
        _check_base(DEFAULT_PLATFORM, data["base_rate"])
        rates[DEFAULT_PLATFORM] = {"note": LEGACY_BASE_NOTE, **data["base_rate"]}
    return rates


def _check_keys(value: Any, allowed: frozenset[str], where: str) -> None:
    """Refuse a key of ``value`` (when it is an object) that is not in ``allowed``. The
    message names the place and the allowed keys, never the unknown key itself."""
    if isinstance(value, dict) and not set(value) <= allowed:
        raise ValueError(
            f"{where} has a key the chart does not know (allowed: {', '.join(sorted(allowed))}); "
            "the file holds counts only"
        )


def _check_text(value: Any, where: str) -> None:
    """Refuse a string value, at any depth, that looks like a handle or a profile link.
    The message names the field, never the text."""
    if isinstance(value, dict):
        for key, item in value.items():
            _check_text(item, f"{where}.{key}")
    elif isinstance(value, list):
        for n, item in enumerate(value):
            _check_text(item, f"{where}[{n}]")
    elif isinstance(value, str) and HANDLE_LIKE.search(value):
        raise ValueError(
            f"{where} looks like an account handle or profile link; the chart takes counts only"
        )


def _check_identities(data: dict[str, Any]) -> None:
    """Refuse keys outside the known ones at every level, then text that looks like a
    handle or a profile link anywhere in the file."""
    _check_keys(data, TOP_KEYS, "the file")
    bases = [("base_rate", data["base_rate"])] if "base_rate" in data else []
    extra = data.get("base_rates", {})
    if isinstance(extra, dict):
        if not set(extra) <= set(PLATFORM_LABELS):
            raise ValueError(
                "base_rates has a key that is not a platform; use one of "
                + ", ".join(PLATFORM_LABELS)
            )
        bases += [(f"base_rates.{p}", base) for p, base in extra.items()]
    for where, base in bases:
        _check_keys(base, BASE_KEYS, where)
        if isinstance(base, dict):
            for key in ("silent_3d", "silent_7d"):
                _check_keys(base.get(key), RATE_KEYS, f"{where}.{key}")
    rows = data.get("rows")
    for n, entry in enumerate(rows if isinstance(rows, list) else []):
        _check_keys(entry, ROW_KEYS, f"rows[{n}]")
        if isinstance(entry, dict):
            _check_keys(entry.get("matched_base"), MATCHED_KEYS, f"rows[{n}].matched_base")
        windows = entry.get("windows") if isinstance(entry, dict) else None
        for m, window in enumerate(windows if isinstance(windows, list) else []):
            _check_keys(window, WINDOW_KEYS, f"rows[{n}].windows[{m}]")
    _check_text(data, "the file")


def _check_windows(label: str, entry: dict[str, Any], eligible: int) -> None:
    if ("windows" in entry) == ("window_range" in entry):
        raise ValueError(f"row {label!r} needs exactly one of windows and window_range")
    if "windows" in entry:
        windows = entry["windows"]
        if not isinstance(windows, list) or not windows:
            raise ValueError(f"row {label!r} needs a list of windows")
        for window in windows:
            if not isinstance(window, dict) or set(window) != {"days", "eligible"}:
                raise ValueError(f"each window of row {label!r} needs days and eligible only")
            if not _is_days(window["days"]) or not _is_count(window["eligible"]):
                raise ValueError(
                    f"row {label!r} needs windows of finite, positive days and whole counts"
                )
        if sum(w["eligible"] for w in windows) != eligible:
            raise ValueError(f"the windows of row {label!r} do not add up to its eligible count")
    else:
        bounds = entry["window_range"]
        if not (
            isinstance(bounds, list)
            and len(bounds) == 2
            and all(_is_days(b) for b in bounds)
            and bounds[0] <= bounds[1]
        ):
            raise ValueError(
                f"row {label!r} needs a window range of two finite days with 0 < low <= high"
            )


def _check_row(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise ValueError("every row must be an object")
    label = entry.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError("every row needs a label")
    if row_platform(entry) not in PLATFORM_LABELS:
        raise ValueError(
            f"row {label!r} has an unknown platform; use one of {', '.join(PLATFORM_LABELS)}"
        )
    if entry.get("kind") not in KINDS:
        raise ValueError(f"row {label!r} has an unknown kind; use one of {', '.join(KINDS)}")
    for key in ("fetched", "planned", "distinct_accounts"):
        if key in entry and not _is_count(entry[key]):
            raise ValueError(f"row {label!r} needs {key} to be a whole count")
    if "fetched" in entry and "planned" in entry and entry["fetched"] > entry["planned"]:
        raise ValueError(f"row {label!r} fetched more accounts than it planned")
    note = entry.get("method_note")
    if note is not None and (not isinstance(note, str) or not note):
        raise ValueError(f"row {label!r} needs method_note to be text")
    eligible, silent = entry.get("eligible"), entry.get("silent")
    if not (_is_count(eligible) and _is_count(silent) and silent <= eligible):
        raise ValueError(f"row {label!r} needs whole counts with 0 <= silent <= eligible")
    if eligible <= 0:
        raise ValueError(f"row {label!r} has no eligible accounts, so it has no rate")
    mixed = entry.get("mixed_windows", False)
    if not isinstance(mixed, bool):
        raise ValueError(f"row {label!r} needs mixed_windows to be true or false")
    if entry["kind"] == "pooled" and not mixed:
        raise ValueError(f"pooled row {label!r} mixes windows and needs mixed_windows: true")
    _check_windows(label, entry, eligible)
    if mixed and "matched_base" in entry:
        raise ValueError(f"row {label!r} mixes windows, so it cannot carry a matched_base")
    if "matched_base" in entry:
        given = entry["matched_base"]
        values = [given.get(k) for k in ("low", "rate", "high")] if isinstance(given, dict) else []
        if not (
            len(values) == 3
            and all(
                isinstance(v, int | float) and not isinstance(v, bool) and 0 <= v <= 1
                for v in values
            )
            and values[0] <= values[1] <= values[2]
        ):
            raise ValueError(
                f"row {label!r} needs matched_base to give rate, low and high between 0 and 1, "
                "with low <= rate <= high"
            )


def base_definition(data: dict[str, Any], platform: str) -> str | None:
    """The ``definition`` of a platform's base rates when they were counted with another
    definition than the runs, or None."""
    base = base_rates(data).get(platform)
    return base.get("definition") if base else None


def validate(data: dict[str, Any]) -> None:
    """Refuse a file the chart cannot draw honestly, with the reason.

    Only the known keys are allowed at each level (:data:`TOP_KEYS`, :data:`ROW_KEYS`,
    :data:`WINDOW_KEYS`, :data:`BASE_KEYS`, :data:`RATE_KEYS`, and platforms in
    ``base_rates``), and no string value may look like a handle or a profile link
    (:data:`HANDLE_LIKE`); the messages never repeat the refused key or text. Every row
    needs a known platform (or none, for ``reddit-archive``), a known kind, whole counts
    with ``0 <= silent <= eligible`` and a positive eligible count, and exactly one of
    ``windows`` (finite, positive days whose eligible counts add up to the row's) and
    ``window_range`` (two finite, positive days, low first). A pooled row needs
    ``mixed_windows: true``, ``fetched`` may not exceed ``planned``, and a ``method_note``
    is text. Labels are unique within a platform. The base rates are checked by
    :func:`base_rates`, including that each pooled rate lies within its weekly rates.
    """
    if not isinstance(data, dict):
        raise ValueError("the file must be an object")
    _check_identities(data)
    for key in ("event", "cutoff", "as_of"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"the file needs {key}")
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("the file needs at least one row")
    seen: set[tuple[str, str]] = set()
    for entry in rows:
        _check_row(entry)
        key = (row_platform(entry), entry["label"])
        if key in seen:
            raise ValueError(f"row {entry['label']!r} appears twice on {key[0]!r}")
        seen.add(key)
    base_rates(data)


def platforms(data: dict[str, Any]) -> list[str]:
    """The platforms that have at least one row, in the order their groups are drawn."""
    present = {row_platform(entry) for entry in data["rows"]}
    return [p for p in PLATFORM_LABELS if p in present]


def matched_base(data: dict[str, Any], days: float, platform: str = DEFAULT_PLATFORM) -> float:
    """A rough straight-line interpolation of a platform's base rate between 3 and 7 days."""
    if not MATCH_MIN_DAYS <= days <= MATCH_MAX_DAYS:
        raise ValueError(f"no matched base outside 3 to 7 days (got {days})")
    base = base_rates(data).get(platform)
    if base is None:
        raise ValueError(f"no base rates for {platform!r}")
    r3 = base["silent_3d"]
    r7 = base["silent_7d"]
    b3 = r3["events"] / r3["eligible"]
    b7 = r7["events"] / r7["eligible"]
    return b3 + (b7 - b3) * (days - 3.0) / 4.0


def matched_range(
    data: dict[str, Any], windows: list[dict[str, Any]], platform: str = DEFAULT_PLATFORM
) -> tuple[float, float, float]:
    """The matched base for a row as (mix-weighted, lowest, highest).

    ``windows`` lists, for each silence window in days, how many eligible accounts were
    scored over it. The weighted value averages the interpolation over those accounts.
    """
    total = sum(w["eligible"] for w in windows)
    values = [matched_base(data, w["days"], platform) for w in windows]
    weighted = sum(v * w["eligible"] for v, w in zip(values, windows, strict=True)) / total
    return weighted, min(values), max(values)


def window_days(entry: dict[str, Any]) -> list[float]:
    """The silence windows of a row in days, sorted, from its windows or its range."""
    if "windows" in entry:
        return sorted({w["days"] for w in entry["windows"]})
    low, high = entry["window_range"]
    return [low, high] if high > low else [low]


def row_matched(
    data: dict[str, Any], entry: dict[str, Any]
) -> tuple[float | None, float, float] | None:
    """The matched base of a row as (weighted or None, lowest, highest), or None.

    A row's own ``matched_base`` is used as it is (unless the row has a ``method_note``).
    Otherwise a row has no matched base when its platform has no base rates or its base
    rates were counted with another definition (``definition``), when it was scored
    with another method than the base rates (``method_note``), when its windows mix
    scoring windows on purpose (a pooled row), or when they fall outside 3 to 7 days,
    where the base rates give nothing to interpolate. A row that gives only the range of
    its windows, not how many accounts each window holds, gets the range without a
    weighted value.
    """
    platform = row_platform(entry)
    if entry.get("method_note"):
        return None
    if "matched_base" in entry:
        given = entry["matched_base"]
        return given["rate"], given["low"], given["high"]
    if platform not in base_rates(data) or base_definition(data, platform):
        return None
    days = window_days(entry)
    if entry.get("mixed_windows") or not all(MATCH_MIN_DAYS <= d <= MATCH_MAX_DAYS for d in days):
        return None
    if "windows" in entry:
        return matched_range(data, entry["windows"], platform)
    values = [matched_base(data, d, platform) for d in days]
    return None, min(values), max(values)


def _matched_text(matched: tuple[float | None, float, float]) -> str:
    weighted, low, high = matched
    if weighted is None:
        return f"matched base ~{low * 100:.2f}-{high * 100:.2f}% (range only)"
    if high - low < 5e-5:
        return f"matched base ~{_pct(weighted, 2)}"
    return f"matched base ~{_pct(weighted, 2)} ({low * 100:.2f}-{high * 100:.2f})"


def _days_text(days: list[float]) -> str:
    if len(days) == 1:
        return f"{days[0]:.1f}"
    return f"{days[0]:.1f} to {days[-1]:.1f}"


def _row(data: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    if entry["kind"] not in KINDS:
        raise ValueError(f"row {entry['label']!r} has an unknown kind")
    platform = row_platform(entry)
    matched = row_matched(data, entry)
    days = window_days(entry)
    counts = f"{entry['silent']:,} of {entry['eligible']:,}"
    if entry.get("mixed_windows"):
        sub = f"{counts}; mixed windows, compare with neither base rate"
    elif matched is not None and "matched_base" in entry:
        sub = f"{counts}; {_matched_text(matched)}"
    elif platform not in base_rates(data):
        sub = f"{counts}; no ordinary-week base rate for {platform_label(platform)} yet"
    elif entry.get("method_note"):
        sub = f"{counts}; non-default method, no matched base"
    elif base_definition(data, platform):
        sub = f"{counts}; no matched base under the runs' definition"
    elif matched is None and days[0] < MATCH_MIN_DAYS and days[-1] > MATCH_MAX_DAYS:
        sub = f"{counts}; no matched base outside 3 to 7 days"
    elif matched is None and days[-1] > MATCH_MAX_DAYS:
        sub = f"{counts}; compare with the 7-day row (no matched base past 7 days)"
    elif matched is None:
        sub = f"{counts}; no matched base under 3 days"
    else:
        sub = f"{counts}; {_matched_text(matched)}"
    return {
        "label": f"{entry['label']}: silent {_days_text(days)} days",
        "sub": sub,
        "events": entry["silent"],
        "total": entry["eligible"],
        "band": None,
        "kind": entry["kind"],
        "matched": matched,
        "platform": platform,
    }


def _base_rows(platform: str, base: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, days in (("silent_3d", 3), ("silent_7d", 7)):
        rate = base[key]
        rows.append(
            {
                "label": f"Ordinary weeks: silent {days} days",
                "sub": (
                    f"other definition ({base['definition']}), reference only"
                    if base.get("definition")
                    else base["note"]
                ),
                "events": rate["events"],
                "total": rate["eligible"],
                "band": (min(rate["weekly_rates"]), max(rate["weekly_rates"])),
                "kind": "base",
                "matched": None,
                "platform": platform,
            }
        )
    return rows


def _groups(data: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Each platform with rows, with its drawn rows: its runs, then its ordinary weeks."""
    rates = base_rates(data)
    groups = []
    for platform in platforms(data):
        rows = [_row(data, e) for e in data["rows"] if row_platform(e) == platform]
        if platform in rates:
            rows += _base_rows(platform, rates[platform])
        groups.append((platform, rows))
    return groups


def _row_spec(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every drawn row, group after group."""
    return [row for _, rows in _groups(data) for row in rows]


def chart_height(rows: int, groups: int = 1) -> int:
    """The height of the chart for a number of rows and platform groups, with the axis label."""
    return ROW_TOP + GROUP_STEP * groups + ROW_STEP * rows + 20


def axis_max(rows: list[dict[str, Any]]) -> tuple[float, int]:
    """The axis maximum as a rate and the tick step in percentage points.

    At least :data:`AXIS_MAX`, and grown in whole tick steps until every interval, band
    and matched base fits, up to 100%.
    """
    top = AXIS_MAX
    for row in rows:
        top = max(top, wilson(row["events"], row["total"])[1])
        if row["band"]:
            top = max(top, row["band"][1])
        if row["matched"]:
            top = max(top, row["matched"][2])
    points = min(100, math.ceil(round(top * 100, 6)))
    step = next(s for s in TICK_STEPS if points / s <= 10)
    return min(1.0, math.ceil(points / step) * step / 100), step


def _desc(data: dict[str, Any], groups: list[tuple[str, list[dict[str, Any]]]]) -> str:
    rates = base_rates(data)
    parts = []
    for platform, _ in groups:
        name = platform_label(platform)
        row_text = "; ".join(
            f"{e['label']}, {_days_text(window_days(e))} days: {e['silent']:,} of "
            f"{e['eligible']:,} eligible accounts"
            for e in data["rows"]
            if row_platform(e) == platform
        )
        parts.append(f"{name}, went silent: {row_text}.")
        if platform in rates:
            r3, r7 = rates[platform]["silent_3d"], rates[platform]["silent_7d"]
            parts.append(
                f"{name}, ordinary weeks: {_pct(r3['events'] / r3['eligible'], 2)} at 3 days "
                f"and {_pct(r7['events'] / r7['eligible'], 2)} at 7 days."
            )
            definition = rates[platform].get("definition")
            if definition:
                parts.append(
                    f"These ordinary-week rows were counted with another definition than the "
                    f"runs ({definition}), so they are a reference only and not a matched "
                    "comparison."
                )
        else:
            parts.append(f"No ordinary-week base rate has been measured for {name} yet.")
    kinds = {e["kind"] for e in data["rows"]}
    parts.append(
        "Each row is scored as of its run's stored data horizon. Each platform is its own "
        "group and rates are never pooled across platforms, because they sample different "
        "populations with different base rates. Runs overlap, so rows are never summed; the "
        "distinct-account rows count each account once, on its longest scored window."
    )
    if "partial" in kinds:
        parts.append(
            "A partial row covers only part of its draw, scored in the order the accounts "
            "were fetched, so it is not a random subsample of the draw."
        )
    if any(e.get("mixed_windows") for e in data["rows"]):
        parts.append("A row that mixes windows compares with neither base rate.")
    for e in data["rows"]:
        if e.get("method_note"):
            parts.append(
                f"{platform_label(row_platform(e))} {e['label']} was scored with a "
                f"non-default method ({e['method_note']}), so it has no matched base."
            )
    if any("matched_base" in e for e in data["rows"]):
        matched = (
            "Hollow markers and short bars are a matched base rate: on a row that gives its "
            "own, it was measured under the runs' own definition; otherwise it is a rough "
            "interpolation for each window from the same platform's base rates."
        )
    else:
        matched = (
            "Hollow markers and short bars are a rough matched base rate, interpolated for "
            "each window from the same platform's base rates."
        )
    parts.append(
        "The grey band on each ordinary-weeks row is the range of the weekly rates. "
        f"{matched} There is no verdict. Counts only; silence alone is not proof that an "
        "account is a bot."
    )
    return f"Event {data['event']}, cutoff {data['cutoff']}, as of {data['as_of']}. " + " ".join(
        parts
    )


def render(data: dict[str, Any], theme: str) -> str:
    """The SVG text of the chart for one colour theme."""
    validate(data)
    pal = PALETTES[theme]
    colors = dict(zip(KINDS, pal["platforms"], strict=True))
    base_color = pal["text2"]
    pal = {**pal, "band": BANDS[theme]}
    groups = _groups(data)
    rows = [row for _, group in groups for row in group]
    top, step = axis_max(rows)

    def _x(rate: float) -> float:
        return PLOT_LEFT + (PLOT_RIGHT - PLOT_LEFT) * rate / top

    height = chart_height(len(rows), len(groups))
    title = "All runs (provisional): share of accounts that went silent"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="t d" '
        f'font-family="{esc(FONT)}">',
        f'<title id="t">{esc(title)}</title>',
        f'<desc id="d">{esc(_desc(data, groups))}</desc>',
        f'<rect width="{WIDTH}" height="{height}" rx="8" fill="{pal["surface"]}"/>',
    ]

    def text(
        x: float, y: float, s: str, size: int, fill: str, anchor: str = "start", bold: bool = False
    ) -> None:
        extra = f' text-anchor="{anchor}"' if anchor != "start" else ""
        extra += ' font-weight="bold"' if bold else ""
        out.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}"{extra}>{esc(s)}</text>'
        )

    text(24, 34, title, 20, pal["text"], bold=True)
    kinds = {e["kind"] for e in data["rows"]}
    overlap = "Runs overlap: rows are never summed."
    if "partial" in kinds:
        overlap += (
            " Partial rows cover part of a draw, scored in fetch order, so they are not a random"
            " subsample."
        )
    verdict = "No verdict. Platforms are never pooled."
    if any(e.get("mixed_windows") for e in data["rows"]):
        verdict += " Pooled rows mix windows."
    if any("matched_base" in e for e in data["rows"]):
        legend = "matched base, same definition as the runs."
    else:
        legend = "rough matched base."
    notes = [
        f"Event {data['event']}, cutoff {data['cutoff']}, as of {data['as_of']}. "
        "Dots are rates with 95% Wilson intervals.",
        f"Each row is scored as of its run's stored data horizon. Hollow marker or short bar: "
        f"{legend}",
        overlap,
        f"{verdict} Silence alone is not proof of a bot.",
    ]
    for n, line in enumerate(notes):
        text(24, 56 + 20 * n, line, 13, pal["text2"])
    rates = base_rates(data)
    bands = [
        f"{platform_label(p)} 3-day row: "
        f"{_pct(min(rates[p]['silent_3d']['weekly_rates']), 2)} to "
        f"{_pct(max(rates[p]['silent_3d']['weekly_rates']), 2)}"
        for p, _ in groups
        if p in rates
    ]
    if bands:
        text(
            24, 136, f"Grey band: range of the weekly rates ({'; '.join(bands)}).", 13, pal["text2"]
        )

    # Lay out the groups: a header, then the group's rows.
    cursor = ROW_TOP - 8
    placed: list[tuple[float, dict[str, Any]]] = []
    for platform, group in groups:
        text(24, cursor, platform_label(platform), 15, pal["text"], bold=True)
        cursor += GROUP_STEP
        for row in group:
            placed.append((cursor, row))
            cursor += ROW_STEP
    axis_y = cursor - 20

    for tick in range(0, round(top * 100) + 1, step):
        rate = tick / 100
        x = _x(rate)
        out.append(
            f'<line x1="{x:.1f}" y1="{ROW_TOP - 24}" x2="{x:.1f}" y2="{axis_y}" '
            f'stroke="{pal["grid"]}" stroke-width="1"/>'
        )
        text(x, axis_y + 18, f"{tick}%", 12, pal["muted"], "middle")
    out.append(
        f'<line x1="{PLOT_LEFT}" y1="{axis_y}" x2="{PLOT_RIGHT}" y2="{axis_y}" '
        f'stroke="{pal["axis"]}" stroke-width="1"/>'
    )
    text(
        (PLOT_LEFT + PLOT_RIGHT) / 2,
        axis_y + 38,
        "Share of eligible accounts that went silent",
        12,
        pal["text2"],
        "middle",
    )

    for y, row in placed:
        rate = row["events"] / row["total"]
        low, high = wilson(row["events"], row["total"])
        color = colors.get(row["kind"], base_color)
        text(24, y - 4, row["label"], 14, pal["text"], bold=row["kind"] != "base")
        text(24, y + 14, row["sub"], 12, pal["muted"])
        if row["band"] is not None:
            lo, hi = row["band"]
            out.append(
                f'<rect x="{_x(lo):.1f}" y="{y - 14}" width="{_x(hi) - _x(lo):.1f}" '
                f'height="24" rx="3" fill="{pal["band"]}"/>'
            )
        out.append(
            f'<line x1="{_x(low):.1f}" y1="{y - 2}" x2="{_x(high):.1f}" y2="{y - 2}" '
            f'stroke="{color}" stroke-width="2.5"/>'
        )
        for edge in (low, high):
            out.append(
                f'<line x1="{_x(edge):.1f}" y1="{y - 8}" x2="{_x(edge):.1f}" '
                f'y2="{y + 4}" stroke="{color}" stroke-width="2"/>'
            )
        if row["matched"]:
            weighted, m_low, m_high = row["matched"]
            if m_high - m_low >= 5e-5:
                out.append(
                    f'<line x1="{_x(m_low):.1f}" y1="{y + 7}" x2="{_x(m_high):.1f}" y2="{y + 7}" '
                    f'stroke="{base_color}" stroke-width="2"/>'
                )
            if weighted is not None:
                out.append(
                    f'<circle cx="{_x(weighted):.1f}" cy="{y - 2}" r="4.5" fill="none" '
                    f'stroke="{base_color}" stroke-width="2"/>'
                )
        out.append(f'<circle cx="{_x(rate):.1f}" cy="{y - 2}" r="6" fill="{color}"/>')
        label = f"{_pct(rate, 2)} ({_pct(low, 2)} to {_pct(high, 2)})"
        edge_low = min(low, row["band"][0]) if row["band"] else low
        edge_high = max(high, row["band"][1]) if row["band"] else high
        if row["matched"]:
            edge_low = min(edge_low, row["matched"][1])
            edge_high = max(edge_high, row["matched"][2])
        if _x(edge_high) + 10 + 175 > PLOT_RIGHT + 30:
            text(_x(edge_low) - 18, y + 3, label, 12, pal["text"], "end")
        else:
            text(_x(edge_high) + 10, y + 3, label, 12, pal["text"])
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_charts(data: dict[str, Any], out_dir: Path) -> list[Path]:
    """Write the light and dark charts into ``out_dir`` and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for theme in THEMES:
        path = out_dir / f"runs-provisional-{theme}.svg"
        path.write_text(render(data, theme), encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    for path in write_charts(data, Path(argv[2])):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
