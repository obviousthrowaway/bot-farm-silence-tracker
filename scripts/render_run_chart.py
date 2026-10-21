"""Render the provisional run chart from a JSON file of aggregate counts.

Usage: render_run_chart.py AGGREGATES_JSON OUTPUT_DIR

Writes ``runs-provisional-light.svg`` and ``runs-provisional-dark.svg`` into OUTPUT_DIR.
The input holds counts only, never an account name, handle or ID. The chart compares the
share of eligible accounts that went silent in each run, in the runs pooled once per
distinct account, and in ordinary weeks, with 95% Wilson intervals. The same input always
gives the same bytes.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.registry.chart import FONT, PALETTES, THEMES, esc

Z95 = 1.959963984540054
WIDTH = 960
PLOT_LEFT = 340
PLOT_RIGHT = 920
AXIS_MAX = 0.06
#: The band behind the ordinary-week rows, visible against each theme surface.
BANDS = {"light": "#e1e0d9", "dark": "#3f3f3b"}
ROW_TOP = 172
ROW_STEP = 56
#: The matched base is interpolated between the 3-day and 7-day base rates only.
MATCH_MIN_DAYS = 3.0
MATCH_MAX_DAYS = 7.0
#: Row kinds that get a series colour; the ordinary-week rows use the secondary text colour.
KINDS = ("run", "union", "partial", "pooled")


def wilson(successes: int, total: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for a proportion, as (low, high)."""
    if total <= 0:
        raise ValueError("total must be positive")
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _x(rate: float) -> float:
    return PLOT_LEFT + (PLOT_RIGHT - PLOT_LEFT) * rate / AXIS_MAX


def _pct(rate: float, digits: int = 1) -> str:
    return f"{rate * 100:.{digits}f}%"


def chart_height(rows: int) -> int:
    """The height of the chart for a number of rows, leaving room for the axis label."""
    return ROW_TOP + ROW_STEP * rows + 28


def matched_base(data: dict[str, Any], days: float) -> float:
    """A rough straight-line interpolation of the base rate between 3 and 7 days."""
    if not MATCH_MIN_DAYS <= days <= MATCH_MAX_DAYS:
        raise ValueError(f"no matched base outside 3 to 7 days (got {days})")
    r3 = data["base_rate"]["silent_3d"]
    r7 = data["base_rate"]["silent_7d"]
    b3 = r3["events"] / r3["eligible"]
    b7 = r7["events"] / r7["eligible"]
    return b3 + (b7 - b3) * (days - 3.0) / 4.0


def matched_range(
    data: dict[str, Any], windows: list[dict[str, Any]]
) -> tuple[float, float, float]:
    """The matched base for a row as (mix-weighted, lowest, highest).

    ``windows`` lists, for each silence window in days, how many eligible accounts were
    scored over it. The weighted value averages the interpolation over those accounts.
    """
    total = sum(w["eligible"] for w in windows)
    values = [matched_base(data, w["days"]) for w in windows]
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

    A row has no matched base when its windows mix scoring windows on purpose (a pooled
    row) or reach past 7 days, where the base rates give nothing to interpolate. A row
    that gives only the range of its windows, not how many accounts each window holds,
    gets the range without a weighted value.
    """
    days = window_days(entry)
    if entry.get("mixed_windows") or not all(MATCH_MIN_DAYS <= d <= MATCH_MAX_DAYS for d in days):
        return None
    if "windows" in entry:
        return matched_range(data, entry["windows"])
    values = [matched_base(data, d) for d in days]
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
        raise ValueError(f"unknown row kind {entry['kind']!r}")
    matched = row_matched(data, entry)
    counts = f"{entry['silent']:,} of {entry['eligible']:,}"
    if entry.get("mixed_windows"):
        sub = f"{counts}; mixed windows, compare with neither base rate"
    elif matched is None:
        sub = f"{counts}; compare with the 7-day row (no matched base past 7 days)"
    else:
        sub = f"{counts}; {_matched_text(matched)}"
    return {
        "label": f"{entry['label']}: silent {_days_text(window_days(entry))} days",
        "sub": sub,
        "events": entry["silent"],
        "total": entry["eligible"],
        "band": None,
        "kind": entry["kind"],
        "matched": matched,
    }


def _row_spec(data: dict[str, Any]) -> list[dict[str, Any]]:
    base3 = data["base_rate"]["silent_3d"]
    base7 = data["base_rate"]["silent_7d"]
    rows = [_row(data, entry) for entry in data["rows"]]
    rows += [
        {
            "label": "Ordinary weeks: silent 3 days",
            "sub": "three Sundays in Sep 2026, pooled",
            "events": base3["events"],
            "total": base3["eligible"],
            "band": (min(base3["weekly_rates"]), max(base3["weekly_rates"])),
            "kind": "base",
            "matched": None,
        },
        {
            "label": "Ordinary weeks: silent 7 days",
            "sub": "three Sundays in Sep 2026, pooled",
            "events": base7["events"],
            "total": base7["eligible"],
            "band": (min(base7["weekly_rates"]), max(base7["weekly_rates"])),
            "kind": "base",
            "matched": None,
        },
    ]
    return rows


def render(data: dict[str, Any], theme: str) -> str:
    """The SVG text of the chart for one colour theme."""
    pal = PALETTES[theme]
    colors = dict(zip(KINDS, pal["platforms"], strict=True))
    base_color = pal["text2"]
    pal = {**pal, "band": BANDS[theme]}
    rows = _row_spec(data)
    height = chart_height(len(rows))
    title = "All runs (provisional): share of accounts that went silent"
    row_text = "; ".join(
        f"{e['label']}, {_days_text(window_days(e))} days: {e['silent']:,} of "
        f"{e['eligible']:,} eligible accounts"
        for e in data["rows"]
    )
    desc = (
        f"Event {data['event']}, cutoff {data['cutoff']}, as of {data['as_of']}. Went "
        f"silent: {row_text}. Ordinary weeks: "
        f"{_pct(rows[-2]['events'] / rows[-2]['total'], 2)} at 3 days and "
        f"{_pct(rows[-1]['events'] / rows[-1]['total'], 2)} at 7 days. Each row is scored "
        "as of its run's stored data horizon. Runs overlap, so rows are never summed; the "
        "distinct-account rows count each account once, on its longest scored window. Run 4 "
        "is partial, still being scanned, and not a random subsample of its draw. The pooled "
        "row mixes windows and compares with neither base rate. The grey band on each "
        "ordinary-weeks row is the range of the three weekly rates. Hollow markers and short "
        "bars are a rough matched base rate, interpolated for each window. There is no "
        "verdict. Counts only; silence alone is not proof that an account is a bot."
    )
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="t d" '
        f'font-family="{esc(FONT)}">',
        f'<title id="t">{esc(title)}</title>',
        f'<desc id="d">{esc(desc)}</desc>',
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
    notes = [
        f"Event {data['event']}, cutoff {data['cutoff']}, as of {data['as_of']}. "
        "Dots are rates with 95% Wilson intervals.",
        "Each row is scored as of its run's stored data horizon. Hollow marker or short bar: "
        "rough matched base.",
        "Runs overlap: rows are never summed. Run 4 is partial, still running, not a random "
        "draw; its figures will change.",
        "No verdict. Pooled row mixes windows. Silence alone is not proof of a bot.",
    ]
    for n, line in enumerate(notes):
        text(24, 56 + 20 * n, line, 13, pal["text2"])
    weekly = data["base_rate"]["silent_3d"]["weekly_rates"]
    text(
        24,
        136,
        "Grey band: range of the three weekly rates "
        f"(3-day row: {_pct(min(weekly), 2)} to {_pct(max(weekly), 2)}).",
        13,
        pal["text2"],
    )

    axis_y = ROW_TOP + ROW_STEP * len(rows) - 20
    for tick in range(0, round(AXIS_MAX * 100) + 1):
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

    for i, row in enumerate(rows):
        y = ROW_TOP + ROW_STEP * i
        rate = row["events"] / row["total"]
        low, high = wilson(row["events"], row["total"])
        if not 0 <= high <= AXIS_MAX or (row["band"] and row["band"][1] > AXIS_MAX):
            raise ValueError(f"rate of {row['label']!r} does not fit the axis (max {AXIS_MAX})")
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


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    out_dir = Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        path = out_dir / f"runs-provisional-{theme}.svg"
        path.write_text(render(data, theme), encoding="utf-8")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
