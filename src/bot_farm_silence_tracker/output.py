"""CSV and JSON writers for ranked candidates."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.controls import placebo_lines
from bot_farm_silence_tracker.early_exit import format_estimate_lines
from bot_farm_silence_tracker.last_seen import COLUMNS as LAST_SEEN_COLUMNS
from bot_farm_silence_tracker.last_seen import NOTICE as LAST_SEEN_NOTICE
from bot_farm_silence_tracker.last_seen import LastSeenReport
from bot_farm_silence_tracker.models import STATUS_OPTED_OUT
from bot_farm_silence_tracker.pipeline import ScanOutcome
from bot_farm_silence_tracker.scoring import (
    MARKER_COLUMNS,
    METHOD_COLUMNS,
    AccountResult,
    ScoringParams,
)
from bot_farm_silence_tracker.timeutil import isoformat

NOTICE = "Candidates for manual review — not a determination that any account is a bot."
LAST_SEEN_OTHERS_NOTICE = (
    "LAST SEEN, ALL ACCOUNTS: rows with scored_silent False and an empty rank are accounts "
    "the scan observed that did not score as silent. They are listed only to show when "
    "they were last seen. They are not candidates for manual review, and the notice on "
    "the first line does not apply to them."
)
PROVISIONAL_NOTICE = (
    "PROVISIONAL: less than the minimum silence period has passed since the cutoff, "
    "so silence may just mean the account has not posted yet."
)
TRUNCATED_NOTICE = (
    "TRUNCATED: {count} scored accounts have truncated histories because the platform "
    "stopped returning history before the baseline start. Truncated histories may be "
    "false negatives: missing baseline activity can hide a real cliff. See the truncated "
    "and partial_reason columns (platform_cap)."
)
INTERRUPTED_NOTICE = (
    "INTERRUPTED: {count} scored accounts have partial histories because their fetch was "
    "interrupted (budget or error) before it reached the baseline start. They may be false "
    "negatives; continuing the run with --from-run fetches only the missing older part. "
    "See the partial_reason column (budget or error)."
)


REPOSTS_NOTICE = (
    "REPOSTS: {count} cliff candidates reposted after the cutoff. Reposts are not counted "
    "as activity (the adapter's default), so these accounts stopped posting but did not "
    "go silent. See the reposts_after_cutoff column."
)
OPTED_OUT_NOTICE = (
    "OPTED OUT: {count} sampled accounts asked not to be shown to logged-out viewers. "
    "They are counted here only and never listed."
)


MARKERS_NOTICE = (
    "MARKERS: this event has markers (earlier moments inside the outage; times as "
    "reported, not independently verified). The silence window starts at {start} and "
    "the baseline ends at {end}. Each cliff account is labelled with the marker, or the "
    "main outage (cutoff), its silence began nearest to; see the nearest_marker columns."
)


def markers_notice(params: ScoringParams) -> str | None:
    """The note about markers of a run with markers, else ``None``."""
    if not params.markers:
        return None
    return MARKERS_NOTICE.format(
        start=isoformat(params.window_start), end=isoformat(params.baseline_end)
    )


def markers_text(params: ScoringParams) -> str:
    """The markers of a run in one token: ``id@time`` items, ``~approx`` when approximate."""
    return ",".join(
        f"{m.id}@{isoformat(m.time)}" + ("~approx" if m.approximate else "") for m in params.markers
    )


def account_notices(outcome: ScanOutcome) -> list[str]:
    """Notes about reposting candidates and opted-out accounts, when there are any."""
    s = outcome.summary
    notes = []
    reposted = int(s.get("cliff_reposted_after_cutoff", 0) or 0)
    if reposted:
        notes.append(REPOSTS_NOTICE.format(count=reposted))
    opted_out = int(s.get("opted_out", 0) or 0)
    if opted_out:
        notes.append(OPTED_OUT_NOTICE.format(count=opted_out))
    return notes


def truncated_notice(outcome: ScanOutcome) -> str | None:
    """Return the truncation notes for ``outcome``, or ``None`` if nothing was truncated.

    Platform-capped and interrupted histories get separate sentences. Summaries
    written before the split count every truncated history as platform-capped.
    """
    s = outcome.summary
    total = int(s.get("truncated", 0) or 0)
    if not total:
        return None
    interrupted = int(s.get("truncated_interrupted", 0) or 0)
    platform = int(s.get("truncated_platform", total - interrupted) or 0)
    notes = []
    if platform:
        notes.append(TRUNCATED_NOTICE.format(count=platform))
    if interrupted:
        notes.append(INTERRUPTED_NOTICE.format(count=interrupted))
    return " ".join(notes) or None


COLUMNS = [
    "rank",
    "handle",
    "user_id",
    "platform",
    "score",
    "provisional",
    "cluster_id",
    "cluster_size",
    "status",
    "baseline_posts",
    "active_days",
    "active_day_ratio",
    "posts_per_day",
    "interarrival_cv",
    "last_activity",
    "silence_hours",
    "p_silence",
    "posts_after_cutoff",
    "account_age_days_at_cutoff",
    "hour_entropy",
    "truncated",
    "partial_reason",
    "data_end",
]
#: Extra columns of a two-phase run (``--baseline-sample-rate``); census output keeps
#: :data:`COLUMNS` alone.
SAMPLING_COLUMNS = ["stratum", "inclusion_prob", "weight", "fetch_status"]


def columns_for(outcome: ScanOutcome, last_seen: LastSeenReport | None = None) -> list[str]:
    """The CSV columns of ``outcome``.

    :data:`COLUMNS`, then the adapter's extra columns (``Source.extra_columns``, for
    example Bluesky's ``reposts_after_cutoff`` and ``deactivated``), then the
    sampling ones if sampled, then the method's (:data:`~bot_farm_silence_tracker.
    scoring.METHOD_COLUMNS`), then the marker ones
    (:data:`~bot_farm_silence_tracker.scoring.MARKER_COLUMNS`) for a run with markers,
    then the last-seen ones (:data:`~bot_farm_silence_tracker.last_seen.COLUMNS`) when
    ``report --last-seen`` asked for them.
    """
    columns = [*COLUMNS, *outcome.extra_columns]
    if outcome.summary.get("sampling"):
        columns += SAMPLING_COLUMNS
    columns += METHOD_COLUMNS
    if outcome.params.markers:
        # Appended last, so the columns of a run without markers never change.
        columns += MARKER_COLUMNS
    if last_seen is not None:
        # Appended after everything else, so a report without --last-seen never changes.
        columns += LAST_SEEN_COLUMNS
    return columns


def estimate_lines(outcome: ScanOutcome) -> list[str]:
    """The labelled estimate lines of a two-phase run, or ``[]`` for a census."""
    sampling = outcome.summary.get("sampling")
    return format_estimate_lines(sampling) if sampling else []


def select_rows(
    outcome: ScanOutcome,
    include_unavailable: bool = False,
    last_seen: LastSeenReport | None = None,
) -> list[dict[str, Any]]:
    """Return ranked output rows: cliff candidates, then optionally unavailable accounts.

    An opted-out account is never a row (see :attr:`ScanOutcome.unavailable`). With a
    ``last_seen`` check, each row also carries its answer, and an account that was asked
    about but is not a candidate (``--last-seen-scope all``) follows with an empty rank.
    """
    chosen: list[AccountResult] = [r for r in outcome.candidates if r.status != STATUS_OPTED_OUT]
    if include_unavailable:
        chosen += [r for r in outcome.unavailable if not r.cliff]
    rows = []
    markers = bool(outcome.params.markers)
    for rank_no, result in enumerate(chosen, start=1):
        rows.append(
            {"rank": rank_no, **result.to_row(outcome.extra_columns, marker_columns=markers)}
        )
    if last_seen is None:
        return rows
    listed = {r.user_id for r in chosen}
    others = sorted(
        (r for r in outcome.results if r.user_id in last_seen.answers and r.user_id not in listed),
        key=lambda r: r.user_id,
    )
    for result in others:
        rows.append({"rank": None, **result.to_row(outcome.extra_columns, marker_columns=markers)})
    by_id = {r.user_id: r for r in outcome.results}
    for row in rows:
        answer = last_seen.answers.get(row["user_id"])
        row.update(answer.columns() if answer else dict.fromkeys(LAST_SEEN_COLUMNS[:3]))
        row["scored_silent"] = by_id[row["user_id"]].cliff
    return rows


OUTAGE_NOTICE = (
    "OUTAGE: the outage also cut off ordinary users of the affected services, and it "
    "came at night local time. A stop at it means the account was affected by the "
    "reported outage, not that it is automated: a synchronized stop is not evidence of "
    "automation on its own. See the independent_signals column and the placebo line."
)


def placebo_notice(outcome: ScanOutcome) -> list[str]:
    """The placebo comparison's lines (the headline first)."""
    return placebo_lines(outcome.placebo)


def _run_meta(outcome: ScanOutcome, platform: str) -> dict[str, Any]:
    p = outcome.params
    provenance = outcome.provenance
    meta = {
        "run_id": outcome.run_id,
        "platform": platform,
        "status": outcome.status,
        "cutoff": isoformat(p.cutoff),
        "now": isoformat(p.now),
        "effective_now": isoformat(p.effective_now),
        "horizon": isoformat(p.horizon) if p.horizon is not None else None,
        "baseline_days": p.baseline_days,
        "min_silence_days": p.min_silence_days,
        "min_active_days": p.min_active_days,
        "min_posts": p.min_posts,
        "cliff_window_hours": p.cliff_window_hours,
        "sync_minutes": p.sync_minutes,
        "recent_days": p.recent_days,
        "max_weekly_cv": p.max_weekly_cv,
        "placebo_weeks": p.placebo_weeks,
        "fetch_start": isoformat(p.fetch_start),
        "days_since_cutoff": round(p.days_since_cutoff, 2),
        "provisional": p.provisional,
        # Provenance: what repeats the run (see pipeline.provenance_of).
        "seed": provenance.get("seed"),
        "seeds_sha256": provenance.get("seeds_sha256"),
        "now_given": provenance.get("now_given"),
        "scan_tool_version": provenance.get("scan_tool_version"),
        "tool_version": __version__,
    }
    if p.markers:
        # Only runs with markers carry these, so other runs keep their metadata.
        meta.update(
            markers=markers_text(p),
            marker_margin_hours=p.marker_margin_hours,
            baseline_end=isoformat(p.baseline_end),
            window_start=isoformat(p.window_start),
        )
    return meta


def write_csv(
    path: Path,
    outcome: ScanOutcome,
    platform: str,
    include_unavailable: bool = False,
    last_seen: LastSeenReport | None = None,
) -> int:
    """Write candidates as CSV with ``#`` comment lines up top. Returns the row count."""
    rows = select_rows(outcome, include_unavailable, last_seen)
    meta = _run_meta(outcome, platform)
    with path.open("w", newline="", encoding="utf-8") as fh:
        fh.write(f"# {NOTICE}\n")
        for line in placebo_notice(outcome):
            fh.write(f"# {line.strip()}\n")
        fh.write(f"# {OUTAGE_NOTICE}\n")
        if meta["provisional"]:
            fh.write(f"# {PROVISIONAL_NOTICE}\n")
        note = truncated_notice(outcome)
        if note:
            fh.write(f"# {note}\n")
        for line in account_notices(outcome):
            fh.write(f"# {line}\n")
        marker_note = markers_notice(outcome.params)
        if marker_note:
            fh.write(f"# {marker_note}\n")
        for line in estimate_lines(outcome):
            fh.write(f"# {line}\n")
        if last_seen is not None:
            fh.write(f"# {LAST_SEEN_NOTICE}\n")
            if any(r.get("rank") is None for r in rows):
                fh.write(f"# {LAST_SEEN_OTHERS_NOTICE}\n")
            fh.write(
                "# last_seen "
                + " ".join(f"{k}={json.dumps(v)}" for k, v in last_seen.to_dict().items())
                + "\n"
            )
        fh.write("# " + " ".join(f"{k}={v}" for k, v in meta.items()) + "\n")
        writer = csv.DictWriter(fh, fieldnames=columns_for(outcome, last_seen))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def write_json(
    path: Path,
    outcome: ScanOutcome,
    platform: str,
    include_unavailable: bool = False,
    last_seen: LastSeenReport | None = None,
) -> int:
    """Write candidates as JSON with a notice, run metadata and summary. Returns row count."""
    rows = select_rows(outcome, include_unavailable, last_seen)
    meta = _run_meta(outcome, platform)
    # An account that did not score as silent (``--last-seen-scope all``) has no rank. It
    # is not a candidate, so it goes under its own key and ``candidates`` keeps its meaning.
    candidates = [r for r in rows if r.get("rank") is not None or last_seen is None]
    others = [r for r in rows if r.get("rank") is None and last_seen is not None]
    payload = {
        "notice": NOTICE,
        "placebo_notice": placebo_notice(outcome),
        "outage_notice": OUTAGE_NOTICE,
        "provisional_notice": PROVISIONAL_NOTICE if meta["provisional"] else None,
        "truncated_notice": truncated_notice(outcome),
        **({"account_notices": notes} if (notes := account_notices(outcome)) else {}),
        **({"estimates": estimate_lines(outcome)} if outcome.summary.get("sampling") else {}),
        **({"markers_notice": note} if (note := markers_notice(outcome.params)) else {}),
        **(
            {"last_seen_notice": LAST_SEEN_NOTICE, "last_seen": last_seen.to_dict()}
            if last_seen is not None
            else {}
        ),
        "run": meta,
        "summary": outcome.summary,
        "candidates": candidates,
        **({"last_seen_other_accounts": others} if last_seen is not None else {}),
    }
    # newline="\n": the same bytes on every platform (Windows text mode writes CRLF).
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, indent=2) + "\n")
    return len(rows)
