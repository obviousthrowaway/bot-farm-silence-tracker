"""Add one run's aggregate counts from a ``report`` JSON file to the main graph.

Usage:
  add_run_to_chart.py REPORT_JSON --label LABEL --as-of YYYY-MM-DD [--status TEXT]
                      [--min-window-days DAYS] [--replace] [--allow-nondefault-method]
                      [--data AGGREGATES_JSON] [--out-dir DIR] [--dry-run]

REPORT_JSON is the file that ``bot-farm-silence-tracker report --run-id N --out
~/run-N.json`` writes for a run on any platform (``reddit-archive``, ``reddit``,
``bluesky`` or ``x``). It holds account names, so it must be written outside the
repository; the script refuses a report inside a git work tree. The script reads only
the run's metadata and its summary counts from it, never an account: the platform, the
run's status, the cutoff, the time its data was scored to (``effective_now``), and the
numbers of eligible and silent (``cliff``) accounts. It adds one row for that platform
to the aggregates file (by default ``docs/charts/runs-provisional.json``), sets the
chart's as-of date to ``--as-of`` (which may not be earlier than the day the run was
scored to), and redraws the light and dark charts next to the file, so every platform's
runs land on the same main graph, each platform in its own group.

The script refuses, with the reason, a report it cannot turn into an honest row: a
platform the chart does not know (such as the ``fake`` demo platform), a two-phase run
(its counts are weighted estimates, not counts), a provisional, failed, cost-only or
still running run (stop it first; a stopped run is charted as partial), a cutoff other
than the chart's, no eligible accounts, and a run in which some accounts' data ended
early unless ``--min-window-days`` gives the shortest window (the row then gets a window
range). A run scored with other parameters than the published rows and base rates
(baseline, eligibility and cliff windows, markers; see :data:`METHOD_KEYS`) is refused
unless ``--allow-nondefault-method`` is given; the row then carries a ``method_note`` and
gets no matched base. A label that is already used on the same platform is refused
unless ``--replace`` is given. Distinct-account and pooled rows combine several runs and cannot
come from one report; they are added by hand, one platform at a time (see
data-analysis.md). Only the aggregates file and the two charts are committed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.events import EventError, load_events_file
from bot_farm_silence_tracker.output import markers_text
from bot_farm_silence_tracker.scoring import ScoringParams

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "docs" / "charts" / "runs-provisional.json"
EVENTS = ROOT / "events.toml"

_spec = importlib.util.spec_from_file_location(
    "render_run_chart", Path(__file__).resolve().parent / "render_run_chart.py"
)
assert _spec is not None and _spec.loader is not None
render_run_chart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_run_chart)

#: The chart's cutoff format, for example ``2026-10-11 00:30 UTC``.
CHART_TIME = "%Y-%m-%d %H:%M UTC"
#: The row kind of each run status whose counts may be charted: a complete run is a
#: ``run``, a stopped or cancelled one is ``partial``. A running, failed or cost-only
#: (``sampled``) run is refused.
ROW_KINDS = {"complete": "run", "partial": "partial", "cancelled": "partial"}


#: The scoring parameters, stored in a report's ``run`` block, that change which accounts
#: are eligible or count as silent. A run charted beside the published rows and base rates
#: must use the method they used: the scoring defaults, with the windows and markers of
#: the chart's event.
METHOD_KEYS = (
    "baseline_days",
    "min_active_days",
    "min_posts",
    "recent_days",
    "max_weekly_cv",
    "cliff_window_hours",
    "markers",
    "marker_margin_hours",
)


def _utc(text: str) -> datetime:
    """A report time in UTC. A time without a UTC offset is refused, never read as local."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"the report time {text!r} has no UTC offset")
    return moment.astimezone(UTC)


def _count(section: dict[str, Any], key: str, default: int | None = None) -> int:
    """A count from the report: a whole number, never a fraction, text or true/false."""
    value = section.get(key, default) if default is not None else section[key]
    if value is None and default is not None:
        value = default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"the report's {key} is not a whole number")
    return value


def expected_method(event_id: str, events_path: Path = EVENTS) -> dict[str, Any]:
    """The method of the published rows for ``event_id``: the scoring defaults
    (:class:`~bot_farm_silence_tracker.scoring.ScoringParams`), with the event's baseline
    and cliff windows, its markers (as a report writes them) and its marker margin, from
    the repository's ``events.toml``. ``markers`` and ``marker_margin_hours`` are ``None``
    for an event without markers, as a report then leaves them out."""
    try:
        event = load_events_file(events_path).get(event_id)
    except EventError as exc:
        raise ValueError(f"cannot read the events file: {exc}") from None
    if event is None:
        raise ValueError(f"the chart's event {event_id!r} is not in events.toml")
    params = ScoringParams(
        cutoff=event.cutoff,
        baseline_days=event.baseline_days,
        cliff_window_hours=event.cliff_window_hours,
        markers=event.scoring_markers(),
        marker_margin_hours=event.scoring_marker_margin(),
    )
    method = {key: getattr(params, key) for key in METHOD_KEYS[:6]}
    method["markers"] = markers_text(params) if params.markers else None
    method["marker_margin_hours"] = params.marker_margin_hours if params.markers else None
    return method


def method_differences(run: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Each method parameter of ``run`` that differs from ``expected``, as
    ``"name value (default value)"``; empty when the run used the published method."""
    differences = []
    for key in METHOD_KEYS:
        got, want = run.get(key), expected[key]
        if isinstance(want, int | float) and isinstance(got, int | float):
            same = float(got) == float(want)
        else:
            same = got == want
        if not same:
            shown = "none" if got is None else got
            default = "none" if want is None else want
            differences.append(f"{key} {shown} (default {default})")
    return differences


def row_from_report(
    report: dict[str, Any],
    chart_cutoff: str,
    label: str,
    status: str | None = None,
    min_window_days: float | None = None,
    method: dict[str, Any] | None = None,
    allow_nondefault_method: bool = False,
) -> dict[str, Any]:
    """The chart row of one run, from the run metadata and summary of its report JSON.

    ``method`` is the method of the published rows (:func:`expected_method`). A run whose
    method differs is refused, unless ``allow_nondefault_method`` is set: the row then
    gets a ``method_note`` listing the differences, and the chart gives it no matched
    base, because the base rates were measured with the published method.
    """
    run, summary = report["run"], report["summary"]
    platform = run["platform"]
    if platform not in render_run_chart.PLATFORM_LABELS:
        raise ValueError(
            f"platform {platform!r} cannot be charted; use one of "
            f"{', '.join(render_run_chart.PLATFORM_LABELS)}"
        )
    if summary.get("sampling") or summary.get("eligible") is None:
        raise ValueError(
            "this is a two-phase run: its eligible count is a weighted estimate, not a "
            "count, so it has no Wilson interval to chart"
        )
    if run.get("provisional"):
        raise ValueError("the run is provisional: less than the minimum silence period passed")
    kind = ROW_KINDS.get(run["status"])
    if kind is None:
        raise ValueError(
            f"a {run['status']!r} run cannot be charted; chart a complete run, or stop a "
            "running one first so that it is charted as partial"
        )
    cutoff = _utc(run["cutoff"])
    expected = datetime.strptime(chart_cutoff, CHART_TIME).replace(tzinfo=UTC)
    if cutoff != expected:
        raise ValueError(
            f"the run's cutoff {cutoff.strftime(CHART_TIME)} is not the chart's {chart_cutoff}"
        )
    eligible, silent = _count(summary, "eligible"), _count(summary, "cliff")
    if eligible <= 0:
        raise ValueError("the run has no eligible accounts, so it has no rate")
    scored_to = _utc(run["effective_now"])
    days = round((scored_to - cutoff).total_seconds() / 86400, 2)
    if days <= 0:
        raise ValueError("the run was scored before the cutoff")
    row: dict[str, Any] = {
        "label": label,
        "platform": platform,
        "kind": kind,
        "status": status or ("complete" if kind == "run" else "partial"),
        "scored_through": scored_to.strftime(CHART_TIME),
    }
    if kind == "partial":
        row["fetched"] = _count(summary, "scored")
        row["planned"] = _count(summary, "sampled")
    row["eligible"] = eligible
    row["silent"] = silent
    differences = method_differences(run, method) if method is not None else []
    if differences and not allow_nondefault_method:
        raise ValueError(
            "the run was scored with a different method than the published rows and base "
            f"rates ({'; '.join(differences)}), so its population is not comparable; pass "
            "--allow-nondefault-method to chart it with a method note and no matched base"
        )
    if differences:
        row["method_note"] = "; ".join(differences)
    early = _count(summary, "early_data_end", 0)
    if early:
        if min_window_days is None:
            raise ValueError(
                f"the data of {early} accounts ended before {row['scored_through']}, so the "
                "run mixes windows; give the shortest one with --min-window-days"
            )
        if not 0 < min_window_days <= days:
            raise ValueError(f"--min-window-days must be above 0 and at most {days}")
        row["window_range"] = [min_window_days, days]
    elif min_window_days is not None:
        raise ValueError("--min-window-days is only for a run whose accounts' data ended early")
    else:
        row["windows"] = [{"days": days, "eligible": eligible}]
    return row


def add_row(data: dict[str, Any], row: dict[str, Any], replace: bool = False) -> dict[str, Any]:
    """A copy of ``data`` with ``row`` added, or replacing the row of the same label and
    platform when ``replace`` is set. The result is validated before it is returned."""
    rows = list(data["rows"])
    platform = render_run_chart.row_platform
    same = [
        i
        for i, entry in enumerate(rows)
        if platform(entry) == platform(row) and entry["label"] == row["label"]
    ]
    if same and not replace:
        raise ValueError(
            f"{row['label']!r} is already a row of {platform(row)!r}; use --replace to update it"
        )
    if same:
        rows[same[0]] = row
    else:
        rows.append(row)
    result = {**data, "rows": rows}
    render_run_chart.validate(result)
    return result


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add one run's aggregate counts from a report JSON file to the main graph."
    )
    parser.add_argument("report", type=Path, help="the report JSON of one run")
    parser.add_argument("--label", required=True, help='the row label, for example "Run 1"')
    parser.add_argument("--status", help="the status text kept with the row")
    parser.add_argument("--min-window-days", type=float, help="shortest window, if mixed")
    parser.add_argument("--as-of", required=True, help="the chart's new as-of date, YYYY-MM-DD")
    parser.add_argument("--replace", action="store_true", help="replace a row of this label")
    parser.add_argument(
        "--allow-nondefault-method",
        action="store_true",
        help="chart a run scored with other parameters, with a method note and no matched base",
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="the aggregates file")
    parser.add_argument("--out-dir", type=Path, help="where the charts go (default: --data's)")
    parser.add_argument("--dry-run", action="store_true", help="print the row, write nothing")
    return parser.parse_args(argv)


def git_work_tree(path: Path) -> Path | None:
    """The git work tree that holds ``path``, or ``None``: the nearest folder, from the
    file's own folder up, that has a ``.git`` entry (a folder, or a file in a worktree)."""
    folder = path.resolve().parent
    for candidate in (folder, *folder.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def check_report_location(path: Path) -> None:
    """Refuse a report inside a git work tree: it holds account names and must never be
    committed, so it belongs outside the repository (for example in the home folder)."""
    tree = git_work_tree(path)
    if tree is not None:
        place = (
            "a path inside this repository's work tree"
            if tree == ROOT
            else "a path inside a git work tree"
        )
        raise ValueError(
            f"the report is at {place}. A report holds account names and must never be "
            "committed: write it outside the repository (for example report --out "
            "~/run-N.json) and pass that path"
        )


def check_as_of(as_of: str, rows: list[dict[str, Any]]) -> None:
    """Refuse an as-of date that is not a date, that is in the future (UTC), or that is
    before the day any row of the chart (the new or replaced one included) was scored to,
    so the chart's "as of" date never moves before its own data."""
    try:
        day = datetime.strptime(as_of, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"--as-of {as_of!r} is not a date in the form YYYY-MM-DD") from None
    if day > datetime.now(UTC).date():
        raise ValueError(f"--as-of {as_of} is in the future")
    for entry in rows:
        if "scored_through" not in entry:
            continue
        try:
            scored = datetime.strptime(entry["scored_through"], CHART_TIME).date()
        except (TypeError, ValueError):
            raise ValueError(
                f"row {entry['label']!r} has a scored_through that is not in the form "
                "YYYY-MM-DD HH:MM UTC"
            ) from None
        if day < scored:
            raise ValueError(
                f"--as-of {as_of} is before row {entry['label']!r} was scored to ({scored})"
            )


def main(argv: list[str]) -> int:
    args = _parse(argv)
    try:
        check_report_location(args.report)
        report = json.loads(args.report.read_text(encoding="utf-8"))
        data = json.loads(args.data.read_text(encoding="utf-8"))
        render_run_chart.validate(data)
        row = row_from_report(
            report,
            data["cutoff"],
            args.label,
            args.status,
            args.min_window_days,
            expected_method(data["event"]),
            args.allow_nondefault_method,
        )
        data = add_row({**data, "as_of": args.as_of}, row, args.replace)
        check_as_of(args.as_of, data["rows"])
        charts = {theme: render_run_chart.render(data, theme) for theme in ("light", "dark")}
    except OSError as exc:
        print(f"error: cannot read a file: {exc.strerror}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, AttributeError) as exc:
        missing = f" (missing or malformed: {exc})" if isinstance(exc, KeyError) else ""
        print(
            "error: the report is not a JSON report of one run as `report --out` writes "
            f"it{missing}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(row, indent=2))
    if args.dry_run:
        return 0
    out_dir = args.out_dir or args.data.parent
    try:
        # The folder first, so a folder that cannot be made leaves every file unchanged.
        out_dir.mkdir(parents=True, exist_ok=True)
        for theme, svg in charts.items():
            path = out_dir / f"runs-provisional-{theme}.svg"
            path.write_text(svg, encoding="utf-8", newline="\n")
        args.data.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(
            f"error: cannot write the aggregates file or the charts: {exc.strerror}",
            file=sys.stderr,
        )
        return 1
    print("wrote the aggregates file and the light and dark charts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
