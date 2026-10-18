"""Checks of a registry change: the policy, and spot-checks against the live sources.

The policy (:func:`plan_changes`)
--------------------------------
Comparing the base registry with the head registry gives the removed records, the
added or changed records, and the status transitions. Plain account data (records
that are and stay ``scanned`` or ``candidate`` on a platform the check can query)
needs no reviewer: the spot-checks below decide. Everything else needs the repository
owner's approval, given through the maintainer label :data:`OVERRIDE_LABEL` and an
approving review of the head commit by someone with admin access (see
:mod:`bot_farm_silence_tracker.registry.gate`). Without it the change FAILS when it

* moves a record into ``verified_inauthentic`` or ``cleared`` (a new record with
  either status included, and ``verified_inauthentic`` to ``cleared``);
* changes or removes a record whose base status is ``verified_inauthentic`` or
  ``cleared`` (moving it back to an automatic status, or changing its username
  ``username_b64``, among others). The ID and platform are the record's key, so
  changing them removes the record;
* removes any record;
* changes more than :data:`CHECK_ALL_MAX` records;
* adds or changes any record on a platform the check cannot query (``reddit`` and
  ``x`` need credentials);
* adds, changes or removes any evidence file.

A transition into ``verified_inauthentic`` also needs at least the event's
``verified_min_silence_days`` of silence (21 by default, longer than the
``min_silence_days`` of a candidate), and a record whose account posted again
(:func:`~bot_farm_silence_tracker.registry.records.resumed`) cannot move into it; no
label lifts either. When the caller passes the evidence reviews (the gate does), the
evidence file of such a record must record the live profile check and at least one
finding beyond the silence (see :mod:`bot_farm_silence_tracker.registry.review`). The
summary lists every transition from ``verified_inauthentic`` to ``cleared`` first, as
a high-impact transition whose reason the owner must confirm, and also lists every
evidence file change, every username change of a human-status record, every added or
changed record on a platform the check cannot query ("unverified (platform not
checkable)"), and every added or changed ``verified_inauthentic`` record whose account
posted again ("resumed"; report drafts refuse it).

Which records are checked
-------------------------
Every added or changed record when there are at most :data:`CHECK_ALL_MAX`.
Above that (with the label) every record whose status changed or is not
``scanned``, plus a seeded sample of at least :data:`MIN_SAMPLE` of the
``scanned``-only ones on a platform the check can query. So every change that adds
or changes records gets at least :func:`spot_check_minimum` of them checked against
the live sources: ``min(n, MIN_SAMPLE)`` of its ``n`` queryable records. Records on
``reddit`` and ``x`` cannot be queried without credentials and are reported as
unverified. The seed in CI is
``sha256(base_sha, head_sha, pr_number)`` (:func:`gate_seed`): a rerun draws the
same sample, and only a new head commit, which needs a new approval, draws another.

The checks
----------
For a record with last activity ``L``, scanned data end ``E``, a match tolerance
``tol`` (default 5 minutes: both sources time activity to the second) and the
source's ingestion lag ``lag`` (default 6 hours for the Reddit archive, 1 hour for
Bluesky):

* Check A, the claimed activity exists: own activity in ``[L - tol, L + tol]``. If
  there is none, the record FAILS (it invents activity), unless ``L`` is within the
  ingestion lag, or the account has earlier own activity (in the
  :data:`EARLIER_LOOKBACK` before ``L``): the owner may have deleted the last post.
  Both are inconclusive.
* Check B, no hidden activity: no own activity in ``(L, E - lag]`` (from ``L`` plus
  one second, the timestamps' precision). Activity there FAILS the record: the
  scan saw data up to ``E``, so the record hides activity, which is how an account
  could be framed as silent. Activity in ``(E - lag, E]`` is inconclusive.

The record rules (:func:`~bot_farm_silence_tracker.registry.records.record_errors`) keep
``E`` within :data:`~bot_farm_silence_tracker.registry.records.MAX_DATA_GAP` of the scan
time and a cliff's ``E - L`` at least
:data:`~bot_farm_silence_tracker.registry.records.MIN_SILENCE`, so a shortened window
cannot hide activity.

The verdict
-----------
The change FAILS when the live spot-check stops early (request budget or time
limit) or queries fewer than :func:`spot_check_minimum` records (it fails closed),
when a checked record fails, when more than
:data:`INCONCLUSIVE_MAX_SHARE` of the checked records are inconclusive, or when any
record whose status changed or is not ``scanned`` is inconclusive.

Records are keyed by ``(event_id, platform, id_b64)``: an account has one record per
outage event, and each is checked on its own. The adapters run with the record's
event's cutoff (the Reddit archive may narrow a slow query to start there; the
answer then says so and the check is inconclusive, never wrongly verified).

Only sources that need no credentials are queried: ``reddit-archive`` (the Arctic
Shift archive) and ``bluesky`` (the public AppView), by the record's ID. Every
request goes through the adapters and the shared :class:`HttpClient`, at no more
than 0.5 requests per second, within a request budget and an overall time limit;
what the limits leave unchecked is inconclusive.

The summary names records by ``id_b64`` only; nothing is decoded in the output.
Anything else that comes from the pull request (file names, problem messages) is
shown inside escaped code spans (:func:`~bot_farm_silence_tracker.registry.records.md_code`).
"""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from bot_farm_silence_tracker.events import EventCatalog
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.registry.records import (
    HUMAN_STATUSES,
    STATUS_CLEARED,
    STATUS_SCANNED,
    STATUS_VERIFIED,
    Key,
    RegistryTree,
    b64d,
    event_dir,
    event_of,
    file_kind,
    md_code,
    parse_lines,
    record_errors,
    record_key,
    resumed,
    silence,
    verified_min_silence_of,
)
from bot_farm_silence_tracker.registry.review import review_problems
from bot_farm_silence_tracker.sources.base import NewestActivity, Source, SourceConfig
from bot_farm_silence_tracker.sources.errors import (
    AccountUnavailable,
    BudgetExceeded,
    RateLimited,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.registry import load_source_class
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, parse_datetime, utcnow

VERIFIED = "verified"
FAILED = "failed"
INCONCLUSIVE = "inconclusive"
UNVERIFIED = "unverified"

#: The maintainer label that, with the owner's approving review, allows every change
#: that needs the owner (see the module notes): human statuses, removals, evidence,
#: large changes and records on platforms the check cannot query.
OVERRIDE_LABEL = "registry-override"
#: Platforms the spot-check queries (no credentials needed).
CHECKABLE = ("bluesky", "reddit-archive")
#: Changes of at most this many records are checked in full; larger ones need the
#: label, and their ``scanned``-only records are sampled.
CHECK_ALL_MAX = 15
#: Least number of ``scanned``-only records sampled from a large change.
MIN_SAMPLE = 5
#: Highest share of inconclusive results among the checked records.
INCONCLUSIVE_MAX_SHARE = 0.2
DEFAULT_TOLERANCE = timedelta(minutes=5)
DEFAULT_LAG = {"reddit-archive": timedelta(hours=6), "bluesky": timedelta(hours=1)}
#: How far before a missing last activity Check A looks for earlier activity.
EARLIER_LOOKBACK = timedelta(days=30)
#: Highest request rate the spot-check uses, whatever the adapter's default.
MAX_RPS = 0.5
DEFAULT_MAX_REQUESTS = 200
DEFAULT_TIMEOUT = timedelta(minutes=15)
_SECOND = timedelta(seconds=1)


class Stop(Exception):
    """The request budget or the time limit is used up."""


class Probe(Protocol):
    """Answers "the account's newest own activity in ``[since, until)``"."""

    def newest(self, record: dict[str, Any], since: datetime, until: datetime) -> NewestActivity:
        """Raise :class:`Stop` when out of budget or time."""
        ...


@dataclass
class Outcome:
    event_id: str
    platform: str
    id_b64: str
    verdict: str
    checks: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    must: bool = False  # Its status changed or is not scanned.
    skipped: bool = False  # Not queried: the request budget or time limit ran out.


# -- loading both sides ------------------------------------------------------------


Records = dict[Key, dict[str, Any]]


def load_side(
    spec: str,
    *,
    repo: Path,
    registry_path: str = "registry",
    now: datetime | None = None,
    events: EventCatalog | None = None,
) -> tuple[Records, list[str]]:
    """Records of one side of the diff: a registry directory, or a git ref.

    ``spec`` is used as a directory when one exists at that path, else as a git ref
    of ``repo`` (the registry is read from ``registry_path`` at that ref, as git
    objects). Returns ``(records by key, problems)``; invalid lines (future
    timestamps included) become problems, not records.
    """
    return load_tree(
        side_tree(spec, repo=repo, registry_path=registry_path), now=now, events=events
    )


def side_tree(spec: str, *, repo: Path, registry_path: str = "registry") -> RegistryTree:
    """The registry of one side of the diff (see :func:`load_side`)."""
    directory = Path(spec)
    if directory.is_dir():
        return RegistryTree(directory)
    return RegistryTree(repo=repo, ref=spec, registry_path=registry_path)


def load_tree(
    tree: RegistryTree, *, now: datetime | None = None, events: EventCatalog | None = None
) -> tuple[Records, list[str]]:
    """Valid records by key, and problems. Bad input becomes a problem, never an error.

    ``events`` are the events records must belong to; by default the tree's own
    (an unreadable events file is a problem, and then every record is one too).
    """
    records: Records = {}
    problems: list[str] = []
    if events is None:
        events, event_problems = tree.events(now=now)
        problems.extend(event_problems)
    catalog = events if events is not None else EventCatalog((), "")
    for rel, mode in sorted(tree.modes().items()):
        if file_kind(rel) != "records" or mode != "100644":
            continue
        too_large = tree.too_large(rel)
        if too_large is not None:
            problems.append(too_large)
            continue
        try:
            text = tree.read(rel).decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: not UTF-8")
            continue
        for entry in parse_lines(rel, text):
            where = f"{rel}:{entry.line}"
            if entry.record is None:
                problems.append(f"{where}: {entry.error}")
                continue
            try:
                errors = record_errors(entry.record, now=now, events=catalog)
            except (ValueError, RecursionError) as exc:
                errors = [f"cannot be checked ({type(exc).__name__})"]
            if not errors and event_of(rel) != entry.record["event_id"]:
                errors = [f"event_id {entry.record['event_id']!r} is not its folder's"]
            if errors:
                problems.append(f"{where}: invalid record ({'; '.join(errors)})")
                continue
            records[record_key(entry.record)] = entry.record
    return records, problems


def evidence_changes(base: RegistryTree, head: RegistryTree) -> list[str]:
    """Evidence paths (relative to the registry) added, changed or removed by ``head``."""

    def files(tree: RegistryTree) -> dict[str, str]:
        return {
            rel: tree.fingerprint(rel) if mode == "100644" else f"mode {mode}"
            for rel, mode in tree.modes().items()
            if "/evidence/" in f"/{rel}"
        }

    before, after = files(base), files(head)
    return sorted(rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel))


def evidence_reviews(base: Records, head: Records, tree: RegistryTree) -> dict[Key, list[str]]:
    """The review problems of the evidence file of every head record that moves into
    ``verified_inauthentic`` (see :mod:`bot_farm_silence_tracker.registry.review`)."""
    out: dict[Key, list[str]] = {}
    modes = tree.modes()
    for key, record in sorted(head.items()):
        before = base.get(key)
        if record["status"] != STATUS_VERIFIED or (
            before is not None and before["status"] == STATUS_VERIFIED
        ):
            continue
        evidence = record.get("evidence")
        rel = f"{event_dir(key[0])}/{evidence}" if evidence else None
        if rel is None or modes.get(rel) != "100644":
            out[key] = ["it has no evidence file to review"]
            continue
        if tree.too_large(rel) is not None:
            out[key] = ["its evidence file is too large to review"]
            continue
        try:
            text = tree.read(rel).decode("utf-8")
        except UnicodeDecodeError:
            out[key] = ["its evidence file is not UTF-8"]
            continue
        out[key] = review_problems(text)
    return out


def evidence_key(rel: str) -> Key | None:
    """The record key an evidence path belongs to, or ``None`` for another path."""
    if file_kind(rel) != "evidence":
        return None
    _, event_id, _, platform, name = rel.split("/")
    return (event_id, platform, name[: -len(".md")])


def changed_records(base: Records, head: Records) -> list[dict[str, Any]]:
    """Head records that are new or differ from the base, sorted by key."""
    return [head[k] for k in sorted(head) if base.get(k) != head[k]]


def sample_records(records: Sequence[dict[str, Any]], k: int, seed: str) -> list[dict[str, Any]]:
    """Up to ``k`` of ``records``, drawn with a string seed, returned sorted by key."""
    ordered = sorted(records, key=record_key)
    if len(ordered) <= k:
        return ordered
    picked = random.Random(f"registry-verify:{seed}").sample(ordered, k)
    return sorted(picked, key=record_key)


def gate_seed(base_sha: str, head_sha: str, pr_number: int | str) -> str:
    """The sampling seed of the gate: fixed for one base, head and pull request.

    Rerunning the gate draws the same sample, so nobody can rerun it until a lucky
    sample passes. A new sample needs a new head commit, which drops the override
    label (see :mod:`bot_farm_silence_tracker.registry.gate`).

    Limit: the seed stops reruns, not grinding. The head SHA is the contributor's to
    choose, so they can compute this seed for many candidate commits offline and push
    one whose sample leaves out a chosen record among the rest. The sample is
    therefore a spot-check, not a guarantee: the records whose status changed or is
    not ``scanned`` are always checked, and reviewers remain responsible for the rest.
    """
    joined = "\0".join(str(part) for part in (base_sha, head_sha, pr_number))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# -- the policy ----------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    event_id: str
    platform: str
    id_b64: str
    before: str | None  # None: the record is new.
    after: str | None  # None: the record was removed.


@dataclass
class Plan:
    """What a change does and what the policy makes of it."""

    changed: list[dict[str, Any]]
    removed: list[Key]
    transitions: list[Transition]
    must: set[Key]  # Changed records whose status changed or is not scanned.
    selected: list[dict[str, Any]]  # The changed records to check, sorted by key.
    labels: frozenset[str]
    failures: list[str] = field(default_factory=list)
    overridden: list[str] = field(default_factory=list)
    #: verified_inauthentic -> cleared: no label needed, listed first.
    clearings: list[Transition] = field(default_factory=list)
    #: Human-status records whose username changed.
    renamed: list[Key] = field(default_factory=list)
    #: Evidence paths changed, each with whether a transition into
    #: verified_inauthentic of its record accompanies it.
    evidence: list[tuple[str, bool]] = field(default_factory=list)
    #: Added or changed records on platforms the check cannot query.
    unchecked: list[Key] = field(default_factory=list)
    #: Added or changed verified_inauthentic records whose account posted again, with
    #: the reason.
    resumed: list[tuple[Key, str]] = field(default_factory=list)


def plan_changes(
    base: Records,
    head: Records,
    *,
    labels: Iterable[str] = (),
    seed: str,
    check_all_max: int = CHECK_ALL_MAX,
    evidence: Iterable[str] = (),
    events: EventCatalog | None = None,
    reviews: Mapping[Key, Sequence[str]] | None = None,
) -> Plan:
    """Apply the policy (see the module notes) and choose the records to check.

    ``evidence`` holds the evidence paths (relative to the registry) the change adds,
    changes or removes (:func:`evidence_changes`). ``events`` give each event's least
    silence for a transition into ``verified_inauthentic``. ``reviews`` maps the key
    of each record moving into ``verified_inauthentic`` to the problems of its
    evidence file's review (:func:`~bot_farm_silence_tracker.registry.review.
    review_problems`); ``None`` skips that rule.
    """
    names = frozenset(labels)
    changed = changed_records(base, head)
    removed = sorted(set(base) - set(head))
    transitions: list[Transition] = [
        Transition(k[0], k[1], k[2], base[k]["status"], None) for k in removed
    ]
    must: set[Key] = set()
    for record in changed:
        key = record_key(record)
        before = base.get(key)
        old = None if before is None else before["status"]
        if old != record["status"] and (old is not None or record["status"] != STATUS_SCANNED):
            transitions.append(Transition(key[0], key[1], key[2], old, record["status"]))
            must.add(key)
        if record["status"] != STATUS_SCANNED:
            must.add(key)
    transitions.sort(key=lambda t: (t.event_id, t.platform, t.id_b64))
    plan = Plan(changed, removed, transitions, must, [], names)

    def needs_label(reason: str) -> None:
        (plan.overridden if OVERRIDE_LABEL in names else plan.failures).append(reason)

    if removed:
        needs_label(f"{len(removed)} records are removed")
    downgrades = [
        t
        for t in transitions
        if t.before in HUMAN_STATUSES and t.after is not None and t.after not in HUMAN_STATUSES
    ]
    if downgrades:
        needs_label(
            f"{len(downgrades)} records leave verified_inauthentic or cleared for an "
            "automatic status"
        )
    if len(changed) > check_all_max:
        needs_label(
            f"{len(changed)} records are added or changed (more than {check_all_max}); "
            "split the pull request"
        )
    plan.unchecked = sorted(record_key(r) for r in changed if r["platform"] not in CHECKABLE)
    if plan.unchecked:
        needs_label(
            f"{len(plan.unchecked)} records are added or changed on a platform the check "
            f"cannot query ({', '.join(sorted({k[1] for k in plan.unchecked}))}), so CI "
            "cannot confirm them against a live source"
        )
    plan.clearings = [
        t for t in transitions if t.before == STATUS_VERIFIED and t.after == STATUS_CLEARED
    ]
    plan.renamed = sorted(
        record_key(r)
        for r in changed
        if (old := base.get(record_key(r))) is not None
        and old["status"] in HUMAN_STATUSES
        and old["username_b64"] != r["username_b64"]
    )
    if plan.renamed:
        needs_label(
            f"{len(plan.renamed)} records with the status verified_inauthentic or cleared "
            "change their username"
        )
    into_verified = {
        (t.event_id, t.platform, t.id_b64)
        for t in transitions
        if t.after == STATUS_VERIFIED and t.before != STATUS_VERIFIED
    }
    plan.evidence = [(rel, evidence_key(rel) in into_verified) for rel in sorted(set(evidence))]
    if plan.evidence:
        needs_label(f"{len(plan.evidence)} evidence files are added, changed or removed")
    into_human = [t for t in transitions if t.after in HUMAN_STATUSES and t.before != t.after]
    if into_human:
        needs_label(f"{len(into_human)} records move into verified_inauthentic or cleared")
    human_changed = [
        record_key(r)
        for r in changed
        if (old := base.get(record_key(r))) is not None and old["status"] in HUMAN_STATUSES
    ]
    if human_changed:
        needs_label(f"{len(human_changed)} existing verified_inauthentic or cleared records change")
    plan.resumed = [
        (record_key(r), why)
        for r in changed
        if r["status"] == STATUS_VERIFIED and (why := resumed(r, events)) is not None
    ]
    for t in transitions:
        if t.after != STATUS_VERIFIED:
            continue
        key = (t.event_id, t.platform, t.id_b64)
        name = _name(*key)
        least = verified_min_silence_of(t.event_id, events)
        if silence(head[key]) < least:
            plan.failures.append(
                f"{name} becomes verified_inauthentic with less than "
                f"{least.total_seconds() / 86400:g} days between last_activity_at and "
                "scan_window_end (the event's verified_min_silence_days)"
            )
        why = resumed(head[key], events)
        if why is not None:
            plan.failures.append(f"{name} becomes verified_inauthentic although {why}")
        if reviews is not None and t.before != STATUS_VERIFIED:
            for problem in reviews.get(key, ["it has no evidence file to review"]):
                plan.failures.append(f"{name}: evidence review: {problem}")
    if len(changed) <= check_all_max:
        plan.selected = list(changed)
    else:
        musts = [r for r in changed if record_key(r) in must]
        # The sample is drawn from the records the check can query, so a large change
        # always gets live checks of at least spot_check_minimum() of them. Records on
        # other platforms are listed as unverified (plan.unchecked) instead.
        rest = [r for r in changed if record_key(r) not in must and r["platform"] in CHECKABLE]
        k = max(MIN_SAMPLE, check_all_max - len(musts))
        plan.selected = sorted(musts + sample_records(rest, k, seed), key=record_key)
    return plan


def spot_check_minimum(changed: Sequence[dict[str, Any]]) -> int:
    """The least number of ``changed`` records the gate checks against the live sources.

    It is ``min(n, MIN_SAMPLE)``, where ``n`` counts the changed records on a platform
    the check can query (:data:`CHECKABLE`). :func:`plan_changes` selects at least that
    many of them for every change: all of them up to :data:`CHECK_ALL_MAX` records,
    and above that every record whose status changed or is not ``scanned`` plus a
    seeded sample of at least :data:`MIN_SAMPLE` of the queryable rest.
    """
    return min(sum(1 for r in changed if r["platform"] in CHECKABLE), MIN_SAMPLE)


def _name(event_id: str, platform: str, id_b64: str) -> str:
    """A record's name in a summary: its event, platform and ``id_b64``, all escaped."""
    return f"{md_code(event_id)} {md_code(platform)} {md_code(id_b64)}"


# -- the checks ----------------------------------------------------------------------


def check_record(
    record: dict[str, Any],
    probe: Probe,
    *,
    now: datetime,
    tolerance: timedelta = DEFAULT_TOLERANCE,
    lag: timedelta,
) -> Outcome:
    """Run checks A and B on one record. :class:`Stop` propagates."""
    out = Outcome(record["event_id"], record["platform"], record["id_b64"], VERIFIED)
    last = parse_datetime(record["last_activity_at"])
    data_end = parse_datetime(record["scan_window_end"])

    def answer(since: datetime, until: datetime) -> NewestActivity | str:
        try:
            return probe.newest(record, since, until)
        except AccountUnavailable as exc:
            return f"the account is unavailable now ({exc.status})"
        except SourceError as exc:
            return f"the source failed ({type(exc).__name__})"

    # Check A: the claimed last activity exists.
    found = answer(last - tolerance, last + tolerance + _SECOND)
    if isinstance(found, str):
        out.checks["A"] = INCONCLUSIVE
        out.reasons.append(f"A: {found}")
    elif found.newest is not None:
        out.checks["A"] = VERIFIED
    elif ensure_utc(found.covered_since) > last - tolerance:
        out.checks["A"] = INCONCLUSIVE
        out.reasons.append("A: the source could not answer for the whole window")
    elif last > now - lag:
        out.checks["A"] = INCONCLUSIVE
        out.reasons.append("A: no activity found yet, but it is within the ingestion lag")
    else:
        earlier = answer(last - EARLIER_LOOKBACK, last - tolerance)
        if isinstance(earlier, str):
            out.checks["A"] = INCONCLUSIVE
            out.reasons.append(f"A: no own activity at last_activity_at, and {earlier}")
        elif earlier.newest is not None:
            out.checks["A"] = INCONCLUSIVE
            out.reasons.append(
                "A: no own activity at last_activity_at, but earlier activity exists "
                "(the owner may have deleted the last post)"
            )
        elif ensure_utc(earlier.covered_since) > last - EARLIER_LOOKBACK:
            out.checks["A"] = INCONCLUSIVE
            out.reasons.append("A: the source could not answer for the whole window")
        else:
            out.checks["A"] = FAILED
            out.reasons.append(
                "A: no own activity at last_activity_at (claimed activity not found)"
            )

    # Check B: nothing hidden between the last activity and the scanned data end.
    start = last + _SECOND
    fail_end = data_end - lag
    out.checks["B"] = VERIFIED
    if fail_end >= start:
        hidden = answer(start, fail_end + _SECOND)
        if isinstance(hidden, str):
            out.checks["B"] = INCONCLUSIVE
            out.reasons.append(f"B: {hidden}")
        elif hidden.newest is not None:
            out.checks["B"] = FAILED
            out.reasons.append(
                f"B: own activity at {isoformat(hidden.newest)} after last_activity_at, "
                "inside the scanned window (hidden activity)"
            )
        elif ensure_utc(hidden.covered_since) > start:
            out.checks["B"] = INCONCLUSIVE
            out.reasons.append("B: the source could not answer for the whole window")
    if out.checks["B"] == VERIFIED:
        lag_start = max(start, fail_end + _SECOND)
        if data_end >= lag_start:
            late = answer(lag_start, data_end + _SECOND)
            if isinstance(late, str):
                out.checks["B"] = INCONCLUSIVE
                out.reasons.append(f"B: {late}")
            elif late.newest is not None:
                out.checks["B"] = INCONCLUSIVE
                out.reasons.append(
                    "B: own activity just before the data end, possibly ingested after "
                    "the scan (ingestion lag)"
                )
    if FAILED in out.checks.values():
        out.verdict = FAILED
    elif INCONCLUSIVE in out.checks.values():
        out.verdict = INCONCLUSIVE
    return out


# -- the live probe ------------------------------------------------------------------


class SourceProbe:
    """Queries the live sources through their adapters, politely.

    One adapter (and one :class:`HttpClient`) per event and platform, created when
    its first record comes up, with the event's cutoff (:meth:`use_events`; without
    events, a cutoff in the distant past). Records are checked in sorted order, so
    one event and platform finishes before the next starts, and each new client gets
    only the requests the earlier ones left: the budget is a hard cap over the whole
    run.
    """

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        timeout: timedelta = DEFAULT_TIMEOUT,
        now: datetime | None = None,
        clock: Callable[[], float] = time.monotonic,
        client_factory: Callable[..., HttpClient] = HttpClient,
    ) -> None:
        self.max_requests = max_requests
        self.now = now or utcnow()
        self._clock = clock
        self._deadline = clock() + timeout.total_seconds()
        self._factory = client_factory
        self._sources: dict[tuple[str, str], Source] = {}
        self._spent_closed = 0
        self._events: EventCatalog | None = None

    def use_events(self, events: EventCatalog | None) -> None:
        """Give the events whose cutoffs the adapters use (see the class notes)."""
        self._events = events

    @property
    def requests_made(self) -> int:
        return self._spent_closed + sum(s.http.requests_made for s in self._sources.values())

    def _cutoff(self, event_id: str) -> datetime:
        event = self._events.get(event_id) if self._events is not None else None
        # Without the event, a cutoff in the distant past: the archive then never
        # narrows a slow query.
        return event.cutoff if event is not None else datetime(1970, 1, 1, tzinfo=UTC)

    def _source(self, event_id: str, platform: str) -> Source:
        key = (event_id, platform)
        source = self._sources.get(key)
        if source is not None:
            return source
        for other in list(self._sources):
            done = self._sources.pop(other)
            self._spent_closed += done.http.requests_made
            done.close()
        remaining = self.max_requests - self.requests_made
        if remaining <= 0:
            raise Stop("the request budget is used up")
        cls = load_source_class(platform)
        left = max(1.0, self._deadline - self._clock())
        http = self._factory(
            max_requests=remaining,
            requests_per_second=min(MAX_RPS, cls.default_rps),
            burst=1.0,
            timeout=cls.default_timeout,
            max_retry_wait=min(60.0, left),
            max_total_wait=min(120.0, left),
            max_reset_wait_total=min(120.0, left),
        )
        config = SourceConfig(
            http=http,
            credentials={},
            now=self.now,
            # The archive's existence query may narrow a slow window to start at the
            # cutoff, and says so in ``covered_since``, which makes the check
            # inconclusive rather than wrong.
            cutoff=self._cutoff(event_id),
        )
        source = cls(config)
        self._sources[key] = source
        return source

    def newest(self, record: dict[str, Any], since: datetime, until: datetime) -> NewestActivity:
        if self._clock() >= self._deadline:
            raise Stop("the time limit is reached")
        source = self._source(str(record.get("event_id")), record["platform"])
        user_id = b64d(record["id_b64"])
        account = Account(record["platform"], user_id, b64d(record["username_b64"]))
        newest: datetime | None = None
        covered = since
        try:
            for kind in source.precheck_kinds:
                answer = source.newest_activity(account, kind, since, until)
                covered = max(covered, ensure_utc(answer.covered_since))
                if answer.newest is not None and (newest is None or answer.newest > newest):
                    newest = answer.newest
        except (BudgetExceeded, RateLimited) as exc:
            raise Stop(str(exc)) from None
        return NewestActivity(newest, covered)

    def close(self) -> None:
        for source in self._sources.values():
            source.close()


# -- the run and its summary ---------------------------------------------------------


@dataclass
class VerifyReport:
    plan: Plan
    outcomes: list[Outcome]
    problems: list[str]
    requests_made: int = 0
    stopped: str | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.plan.changed)

    @property
    def sampled(self) -> int:
        return len(self.plan.selected)

    def count(self, verdict: str) -> int:
        return sum(1 for o in self.outcomes if o.verdict == verdict)

    @property
    def checked(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict != UNVERIFIED)

    @property
    def ok(self) -> bool:
        return not self.failures


def judge(report: VerifyReport) -> list[str]:
    """Why the change fails (empty: it passes). See the module notes."""
    failures = [f"{len(report.problems)} invalid lines"] if report.problems else []
    failures += report.plan.failures
    # Fail closed: the live spot-check is mandatory. A run that stopped early (the
    # request budget or the time limit) or queried fewer than spot_check_minimum()
    # records proves nothing about the records it left out.
    if report.stopped:
        failures.append(f"the live spot-check could not finish ({report.stopped})")
    least = spot_check_minimum(report.plan.changed)
    queried = sum(1 for o in report.outcomes if o.verdict != UNVERIFIED and not o.skipped)
    if queried < least:
        failures.append(
            f"only {queried} records were checked against the live sources; at least "
            f"{least} must be"
        )
    if report.count(FAILED):
        failures.append(f"{report.count(FAILED)} checked records failed")
    inconclusive = report.count(INCONCLUSIVE)
    if report.checked and inconclusive > INCONCLUSIVE_MAX_SHARE * report.checked:
        failures.append(
            f"{inconclusive} of {report.checked} checked records are inconclusive "
            f"(more than {INCONCLUSIVE_MAX_SHARE:.0%})"
        )
    key_inconclusive = [o for o in report.outcomes if o.must and o.verdict == INCONCLUSIVE]
    if key_inconclusive:
        failures.append(
            f"{len(key_inconclusive)} records with a status change or a status other than "
            "scanned are inconclusive"
        )
    return failures


def run_verify(
    base: Records,
    head: Records,
    probe: Probe,
    *,
    seed: str,
    labels: Iterable[str] = (),
    now: datetime | None = None,
    tolerance: timedelta = DEFAULT_TOLERANCE,
    lag: timedelta | None = None,
    problems: Iterable[str] = (),
    check_all_max: int = CHECK_ALL_MAX,
    evidence: Iterable[str] = (),
    events: EventCatalog | None = None,
    reviews: Mapping[Key, Sequence[str]] | None = None,
) -> VerifyReport:
    """Apply the policy, check the selected records, and judge the change.

    ``events`` (the head's) give each event's least silence and, through
    ``probe.use_events`` when the probe has it, each event's cutoff.
    """
    when = now or utcnow()
    plan = plan_changes(
        base,
        head,
        labels=labels,
        seed=seed,
        check_all_max=check_all_max,
        evidence=evidence,
        events=events,
        reviews=reviews,
    )
    use_events = getattr(probe, "use_events", None)
    if callable(use_events):
        use_events(events)
    outcomes: list[Outcome] = []
    stopped: str | None = None
    for record in plan.selected:
        platform = record["platform"]
        must = record_key(record) in plan.must
        if platform not in CHECKABLE:
            outcomes.append(
                Outcome(
                    record["event_id"],
                    platform,
                    record["id_b64"],
                    UNVERIFIED,
                    reasons=["unverified (platform not checkable)"],
                    must=must,
                )
            )
            continue
        if stopped is None:
            try:
                outcome = check_record(
                    record,
                    probe,
                    now=when,
                    tolerance=tolerance,
                    lag=lag if lag is not None else DEFAULT_LAG[platform],
                )
            except Stop as exc:
                stopped = str(exc)
            else:
                outcome.must = must
                outcomes.append(outcome)
                continue
        outcomes.append(
            Outcome(
                record["event_id"],
                platform,
                record["id_b64"],
                INCONCLUSIVE,
                reasons=[f"not checked: {stopped}"],
                must=must,
                skipped=True,
            )
        )
    report = VerifyReport(
        plan=plan,
        outcomes=outcomes,
        problems=list(problems),
        requests_made=getattr(probe, "requests_made", 0),
        stopped=stopped,
    )
    report.failures = judge(report)
    return report


def _status(value: str | None, *, removed: bool = False) -> str:
    if value is None:
        return "(removed)" if removed else "(new)"
    return f"`{value}`"


def render_summary(report: VerifyReport) -> str:
    """The Markdown job summary. Records are named by ``id_b64`` only."""
    plan = report.plan
    lines = [
        "## Registry spot-check",
        "",
        f"**{'PASS' if report.ok else 'FAIL'}.** Added or changed records: {report.changed}. "
        f"Removed: {len(plan.removed)}. Checked: {report.sampled}. "
        f"Requests made: {report.requests_made}.",
        "",
    ]
    if plan.clearings:
        lines += [
            "### High-impact transitions: verified_inauthentic to cleared",
            "",
            f"Clearing a record needs the `{OVERRIDE_LABEL}` label and the owner's approval; "
            "the owner must confirm the reason for each one:",
            "",
        ]
        lines += [f"- {_name(t.event_id, t.platform, t.id_b64)}" for t in plan.clearings] + [""]
    if report.failures:
        lines += ["### Why it fails", ""] + [f"- {f}" for f in report.failures] + [""]
    if report.problems:
        lines += ["### Invalid lines", ""] + [f"- {md_code(p)}" for p in report.problems]
        lines.append("")
    if plan.overridden:
        lines += [f"### Allowed by the `{OVERRIDE_LABEL}` label", ""]
        lines += [f"- {o}" for o in plan.overridden] + [""]
    if plan.transitions:
        lines += [
            "### Status transitions",
            "",
            "| Event | Platform | id_b64 | Before | After |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {md_code(t.event_id)} | {t.platform} | `{t.id_b64}` | {_status(t.before)} | "
            f"{_status(t.after, removed=True)} |"
            for t in plan.transitions
        ]
        lines.append("")
    if plan.renamed:
        lines += ["### Username changes of verified_inauthentic or cleared records", ""]
        lines += [f"- {_name(*key)}" for key in plan.renamed] + [""]
    if plan.evidence:
        lines += ["### Evidence file changes", ""]
        lines += [
            f"- {md_code(rel)}: "
            + (
                f"with its record's transition into verified_inauthentic (needs `{OVERRIDE_LABEL}`)"
                if ok
                else f"no transition into verified_inauthentic (needs `{OVERRIDE_LABEL}`)"
            )
            for rel, ok in plan.evidence
        ] + [""]
    if plan.unchecked:
        lines += ["### Unverified (platform not checkable)", ""]
        lines += [f"- {_name(*key)}" for key in plan.unchecked] + [""]
    if plan.resumed:
        lines += [
            "### Resumed: verified_inauthentic records whose account posted again",
            "",
            "Report drafts refuse these records. Reviewers should consider clearing them.",
            "",
        ]
        lines += [f"- {_name(*key)}: {why}" for key, why in plan.resumed] + [""]
    lines += [
        "| Verified | Failed | Inconclusive | Unverified |",
        "| ---: | ---: | ---: | ---: |",
        f"| {report.count(VERIFIED)} | {report.count(FAILED)} | {report.count(INCONCLUSIVE)} "
        f"| {report.count(UNVERIFIED)} |",
        "",
    ]
    for verdict, title in (
        (FAILED, "Failed"),
        (INCONCLUSIVE, "Inconclusive"),
        (UNVERIFIED, "Unverified (warning: not checked)"),
    ):
        rows = [o for o in report.outcomes if o.verdict == verdict]
        if not rows:
            continue
        lines += [f"### {title}", ""]
        for o in rows:
            mark = " (status change or not scanned)" if o.must else ""
            name = _name(o.event_id, o.platform, o.id_b64)
            lines.append(f"- {name}{mark}: {'; '.join(o.reasons)}")
        lines.append("")
    if report.stopped:
        lines += [f"Stopped early: {report.stopped}. Unchecked records count as inconclusive.", ""]
    lines += [
        "Check A: the claimed last activity exists. Check B: no own activity is hidden "
        "between the last activity and the scanned data end. The change fails on any "
        f"failed record, on more than {INCONCLUSIVE_MAX_SHARE:.0%} inconclusive results, "
        "and on any inconclusive record whose status changed or is not scanned. CI does "
        "not check baseline_posts, baseline_active_days or cliff: reviewers check them "
        "against the evidence file.",
        "",
    ]
    return "\n".join(lines)
