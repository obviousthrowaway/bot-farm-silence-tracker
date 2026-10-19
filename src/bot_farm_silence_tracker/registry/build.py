"""Turn a stored run into registry records and merge them into a registry.

What goes in
------------
Every account of the run that was fully scanned, is ``active`` and had at least one
own activity in the scanned window. Left out, and counted:

* accounts that opted out of logged-out views (Bluesky ``!no-unauthenticated``):
  never written anywhere, as everywhere else in the tool;
* suspended, deleted and private accounts: there is no public activity a reviewer or
  the spot-check could confirm, and the platform has already acted or the account
  is gone;
* accounts that were skipped, failed, or only pre-checked in a two-phase run: their
  history was not read in full, so the facts would be incomplete;
* accounts with no activity in the scanned window: nothing to check;
* accounts whose data ends more than
  :data:`~bot_farm_silence_tracker.registry.records.MAX_DATA_GAP` before the scan (a stale
  source): the record could not be checked.

A run of the synthetic ``fake`` platform is refused, and so is a run of a local-only
custom event (see :mod:`bot_farm_silence_tracker.events`). Every record carries the
run's ``event_id`` and goes to that event's folder; the same account can have a
record in each event.

The ``cliff`` field
-------------------
``true`` only for a cliff that is not provisional and whose silence (data end minus
last activity) is at least the event's ``min_silence_days``.
A provisional cliff is recorded as ``cliff: false``: it is not a claim of silence yet.

Status of a new record
----------------------
``candidate`` when the record's ``cliff`` is true, at least one signal that does not
depend on the outage's timing fired (:func:`fired_signals`: a synchronized stop alone
is not enough, see :mod:`bot_farm_silence_tracker.controls`), and the live profile
check (:mod:`bot_farm_silence_tracker.registry.live`) did not find the account
suspended or deleted, or fail; otherwise ``scanned``. An existing record is
merged with :func:`bot_farm_silence_tracker.registry.records.merge_records`: a newer scan
updates the facts, ``last_activity_at`` keeps the latest value, and the status moves
between ``scanned`` and ``candidate`` only. ``verified_inauthentic`` and ``cleared``
are never changed by automation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.controls import (  # noqa: F401 - re-exported
    CLUSTER_MIN,
    CLUSTER_P_MAX,
    REGULAR_CV_MAX,
    ROUND_THE_CLOCK_BITS,
    ROUND_THE_CLOCK_MIN_POSTS,
    ClusterTest,
    Signal,
    SignalContext,
    fired_signals,
    independent,
    miller_madow_bits,
)
from bot_farm_silence_tracker.early_exit import FETCH_PHASE2_PENDING, FETCH_PRECHECK_ONLY
from bot_farm_silence_tracker.events import (
    EventCatalog,
    OutageEvent,
    event_of_run,
    registry_events,
)
from bot_farm_silence_tracker.models import STATUS_ACTIVE, STATUS_OPTED_OUT, UNAVAILABLE_STATUSES
from bot_farm_silence_tracker.pipeline import ScanOutcome, rescore_run
from bot_farm_silence_tracker.registry.live import Check, LiveStatus, no_live_check
from bot_farm_silence_tracker.registry.records import (
    MAX_DATA_GAP,
    MIN_SILENCE,
    PLATFORMS,
    STATUS_CANDIDATE,
    STATUS_SCANNED,
    b64d,
    b64e,
    future_errors,
    load_records,
    merge_records,
    read_entries,
    record_errors,
    record_key,
    schema_errors,
    write_records,
)
from bot_farm_silence_tracker.scoring import AccountResult, ScoringParams
from bot_farm_silence_tracker.storage import Storage
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, parse_datetime

# The signals live in bot_farm_silence_tracker.controls (the pipeline needs them for the
# placebo runs); they are re-exported here for the registry's callers.
#
# There is no signal for a low ``p_silence``. Silence grows with the time since the
# cutoff, so ``p_silence`` of every cliff account falls below any fixed threshold
# once enough days pass (at one post a day it is below 1e-3 after a week). It
# measures the cliff itself, which is already required, not automation.


def registry_cliff(
    result: AccountResult, window_end: datetime, min_silence: timedelta = MIN_SILENCE
) -> bool:
    """The record's ``cliff``: a non-provisional cliff silent for ``min_silence`` (the
    event's)."""
    if not result.cliff or result.provisional or result.last_activity is None:
        return False
    return ensure_utc(window_end) - ensure_utc(result.last_activity) >= min_silence


def is_candidate(
    result: AccountResult,
    signals: Sequence[Signal],
    window_end: datetime | None = None,
    min_silence: timedelta = MIN_SILENCE,
) -> bool:
    """The rule for ``candidate``: a registry cliff with a signal that does not depend on
    the outage's timing.

    A synchronized stop alone is not enough: a shared stop at the outage is what
    anyone the outage affected shows (see :mod:`bot_farm_silence_tracker.controls`).
    ``window_end`` is the record's ``scan_window_end``; ``None`` skips the silence
    length (a non-provisional cliff, as scored). The live profile check of
    :func:`run_records` comes on top.
    """
    if not independent(signals):
        return False
    if window_end is None:
        return result.cliff and not result.provisional
    return registry_cliff(result, window_end, min_silence)


@dataclass
class RunRecords:
    """The registry records of one run, plus what was left out and why."""

    outcome: ScanOutcome
    records: list[dict[str, Any]]
    results: dict[str, AccountResult]  # By user ID, for the records' accounts.
    excluded: dict[str, int] = field(default_factory=dict)
    event: OutageEvent | None = None
    #: Ways the run's windows differ from its event's, for the user to see.
    notes: list[str] = field(default_factory=list)
    #: The live profile check of each account that would be a candidate, by user ID.
    live: dict[str, LiveStatus] = field(default_factory=dict)
    #: Accounts kept ``scanned`` by the live check, by its status.
    live_blocked: dict[str, int] = field(default_factory=dict)


def _scan_window_end(result: AccountResult, params: ScoringParams) -> datetime:
    end = ensure_utc(result.data_end) if result.data_end else ensure_utc(params.effective_now)
    end = min(end, ensure_utc(params.now))
    if result.last_activity is not None:
        end = max(end, ensure_utc(result.last_activity))
    return end


def exclusion_reason(result: AccountResult) -> str | None:
    """Why an account of a run is not put in the registry, or ``None``."""
    if result.status == STATUS_OPTED_OUT:
        return "opted_out"
    if result.status in UNAVAILABLE_STATUSES:
        return "unavailable"
    if result.status != STATUS_ACTIVE or result.fetch_status in (
        FETCH_PRECHECK_ONLY,
        FETCH_PHASE2_PENDING,
    ):
        return "not_fully_scanned"
    if result.last_activity is None:
        return "no_activity"
    return None


def _stale(result: AccountResult, params: ScoringParams) -> bool:
    return ensure_utc(params.now) - _scan_window_end(result, params) > MAX_DATA_GAP


def run_records(
    storage: Storage, run_id: int, events: EventCatalog, live: Check = no_live_check
) -> RunRecords:
    """Build the records of ``run_id`` from the cache.

    The records belong to the run's event (:func:`~bot_farm_silence_tracker.events.
    event_of_run`). Raises ``ValueError`` for a run of a custom event or of an event
    that ``events`` does not hold. ``live`` checks the public profile of every account
    that would become a candidate (:class:`~bot_farm_silence_tracker.registry.live.
    LiveChecker`); the default makes no request, so a Bluesky account then stays
    ``scanned``.
    """
    run = storage.get_run(run_id)
    if run is None:
        raise LookupError(f"no run with id {run_id}")
    event = event_of_run(run["params"], events)
    if event.legacy_of:
        shared = events.get(event.legacy_of)
        now_text = f" (now {shared.cutoff_text})" if shared is not None else ""
        raise ValueError(
            f"run {run_id} was scored with the cutoff {event.cutoff_text}, an earlier "
            f"definition of the event {event.legacy_of}{now_text}; it belongs to the local "
            f"event {event.id}, which the shared registry never takes. Scan the event again"
        )
    if event.custom or event.id not in events:
        raise ValueError(
            f"run {run_id} belongs to the local-only custom event {event.id!r}, which the "
            "shared registry never takes (add the event to events.toml in its own pull "
            "request and scan it with --event)"
        )
    outcome = rescore_run(storage, run_id)
    params = outcome.params
    if ensure_utc(params.cutoff) != ensure_utc(event.cutoff):
        raise ValueError(
            f"run {run_id} was scored with the cutoff {isoformat(params.cutoff)}, but its "
            f"event {event.id} has the cutoff {event.cutoff_text} (the events file changed, "
            "or the run is older than the event); scan the event again"
        )

    def timing(markers: Any) -> list[tuple[str, datetime, bool]]:
        return [(m.id, ensure_utc(m.time), m.approximate) for m in markers]

    if (
        timing(params.markers) != timing(event.scoring_markers())
        or float(params.marker_margin_hours) != event.scoring_marker_margin()
    ):
        # The markers decide the windows and the labels, like the cutoff.
        raise ValueError(
            f"run {run_id} was scored with other markers than its event {event.id} has "
            "now (the events file changed after the scan); scan the event again"
        )
    notes = [
        f"run {run_id} used {name} {got:g}, the event {event.id} {want:g}"
        for name, got, want in (
            ("baseline_days", params.baseline_days, event.baseline_days),
            ("min_silence_days", params.min_silence_days, event.min_silence_days),
            ("cliff_window_hours", params.cliff_window_hours, event.cliff_window_hours),
        )
        if float(got) != float(want)
    ]
    platforms = {r.platform for r in outcome.results} - set(PLATFORMS)
    if platforms:
        raise ValueError(
            f"run {run_id} is from {', '.join(sorted(platforms))}, which the shared registry "
            "does not take (synthetic or unknown platform)"
        )
    context = outcome.context
    scanned = isoformat(params.now)
    checked: dict[str, LiveStatus] = {}
    blocked: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    results: dict[str, AccountResult] = {}
    excluded: dict[str, int] = {}
    for result in outcome.results:
        reason = exclusion_reason(result)
        if reason is None and _stale(result, params):
            reason = "stale_data"
        if reason is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        assert result.last_activity is not None
        window_end = _scan_window_end(result, params)
        signals = fired_signals(result, context)
        status = STATUS_SCANNED
        if is_candidate(result, signals, window_end, event.min_silence):
            found = live(result.platform, result.user_id, result.handle)
            checked[result.user_id] = found
            if found.blocks_candidate:
                blocked[found.status] = blocked.get(found.status, 0) + 1
            else:
                status = STATUS_CANDIDATE
        records.append(
            {
                "event_id": event.id,
                "platform": result.platform,
                "id_b64": b64e(result.user_id),
                "username_b64": b64e(result.handle),
                "first_scanned_at": scanned,
                "last_scanned_at": scanned,
                "last_activity_at": isoformat(result.last_activity),
                "scan_window_end": isoformat(window_end),
                "baseline_posts": result.baseline_posts,
                "baseline_active_days": result.active_days,
                "cliff": registry_cliff(result, window_end, event.min_silence),
                "tool_version": __version__,
                "status": status,
                "evidence": None,
                "status_updated_at": scanned,
            }
        )
        results[result.user_id] = result
    return RunRecords(
        outcome,
        records,
        results,
        dict(sorted(excluded.items())),
        event,
        notes,
        checked,
        dict(sorted(blocked.items())),
    )


@dataclass
class AddStats:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    to_candidate: int = 0
    to_scanned: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    event_id: str = ""
    notes: list[str] = field(default_factory=list)
    #: Accounts the live profile check kept from becoming candidates, by its status.
    live_blocked: dict[str, int] = field(default_factory=dict)
    #: With ``flagged_only``: fully scanned accounts not written because they are neither
    #: a cliff nor a candidate, and how many of them already have a record (left as it is).
    skipped_unflagged: int = 0
    skipped_unflagged_registered: int = 0


def is_flagged(record: dict[str, Any]) -> bool:
    """Whether a record is flagged: ``cliff`` is true, or it is a ``candidate``."""
    return bool(record["cliff"]) or record["status"] == STATUS_CANDIDATE


def add_run(
    root: Path,
    storage: Storage,
    run_id: int,
    events: EventCatalog,
    live: Check = no_live_check,
    flagged_only: bool = False,
) -> AddStats:
    """Merge the records of ``run_id`` into its event's folder of the registry at
    ``root``. ``live`` is the live profile check (see :func:`run_records`).

    With ``flagged_only``, only the flagged records are written (``cliff`` true, or
    ``candidate``, see :func:`is_flagged`); the other fully scanned accounts are counted in
    ``skipped_unflagged`` and an existing record of one of them is left as it is.
    """
    built = run_records(storage, run_id, events, live)
    for record in built.records:
        errors = record_errors(record, events=events)
        if errors:
            raise ValueError(
                f"run {run_id} gives an invalid record ({record['id_b64']}): {'; '.join(errors)}"
            )
    existing = load_records(root, events=events)
    assert built.event is not None
    least = built.event.min_silence
    stats = AddStats(
        excluded=built.excluded,
        event_id=built.event.id,
        notes=built.notes,
        live_blocked=built.live_blocked,
    )
    for record in built.records:
        key = record_key(record)
        old = existing.get(key)
        if flagged_only and not is_flagged(record):
            stats.skipped_unflagged += 1
            if old is not None:
                stats.skipped_unflagged_registered += 1
            continue
        if old is None:
            existing[key] = record
            stats.added += 1
            continue
        merged = merge_records(old, record, min_silence=least)
        if merged == old:
            stats.unchanged += 1
            continue
        if merged["status"] != old["status"]:
            if merged["status"] == STATUS_CANDIDATE:
                stats.to_candidate += 1
            elif merged["status"] == STATUS_SCANNED:
                stats.to_scanned += 1
        existing[key] = merged
        stats.updated += 1
    stats.changed_files = write_records(root, existing.values())
    return stats


def registered_ids(
    root: Path,
    platform: str,
    now: datetime,
    within_days: float,
    event_id: str,
    events: EventCatalog | None = None,
) -> set[str]:
    """Decoded IDs of ``platform`` accounts last scanned for ``event_id`` within
    ``within_days`` of ``now``. Records of other events do not count: an account
    scanned for one event is still unchecked for another.

    A record with a timestamp after ``now`` (beyond the clock slack) is ignored: it
    cannot have been scanned recently, and a forged future ``last_scanned_at`` must
    not keep an account out of new scans. Raises ``ValueError`` for any other
    invalid line.
    """
    catalog = events if events is not None else registry_events(root)
    since = ensure_utc(now) - timedelta(days=within_days)
    out: set[str] = set()
    for entry in read_entries(root):
        where = f"{entry.path}:{entry.line}"
        if entry.record is None:
            raise ValueError(f"{where}: {entry.error}")
        record = entry.record
        errors = record_errors(record, now=now, events=catalog)
        if not errors and entry.path.split("/")[1] != record["event_id"]:
            errors = [f"event_id {record['event_id']!r} is not its folder's"]
        future = set(future_errors(record, now)) if errors and not schema_errors(record) else set()
        if any(e not in future for e in errors):
            raise ValueError(f"{where}: {'; '.join(errors)}")
        if future:
            continue
        if (
            record["event_id"] == event_id
            and record["platform"] == platform
            and parse_datetime(record["last_scanned_at"]) >= since
        ):
            out.add(b64d(record["id_b64"]))
    return out
