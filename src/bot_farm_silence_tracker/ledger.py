"""Persistent spend ledger, so a USD cap holds across reruns and resumed runs.

Paid platforms (X bills per post and per user returned) need a cap that survives a
rerun: a scan with a $50 cap that stops at $49 and is rerun must not get another
$50. The ledger records estimated spend in a ``spend_events`` table, keyed by
platform, UTC calendar day (wall clock, not ``--now``) and run id. The databases in
the cache folder share one such table, in ``<cache>/spend.db``, so a daily cap holds
across outage events and custom cutoffs; a database elsewhere keeps its own (see
:func:`bot_farm_silence_tracker.storage.uses_shared_spend`). Amounts are stored as
integer micro-USD so repeated small charges do not drift; the API takes and returns
float USD.

Design, kept deliberately small:

* An adapter receives a :class:`SpendLedger` as ``SourceConfig.ledger`` (``None``
  when no storage is attached, for example in unit tests).
* The atomic way to spend is :meth:`SpendLedger.reserve` before a billed request
  and :meth:`Reservation.settle` with the actual amount after it. ``reserve``
  checks the cap and records the reservation in one ``BEGIN IMMEDIATE``
  transaction, so two scans sharing a database cannot both pass the cap. It raises
  :class:`~bot_farm_silence_tracker.sources.errors.BudgetExceeded` when the reservation would go
  over the cap.
* The simple way still works: :meth:`SpendLedger.spent` before a request to compare
  against the cap, and :meth:`SpendLedger.add` with the USD just charged. It is
  not atomic across processes.
* ``scope="day"`` (the default) sums every run on this platform today (UTC), so the
  cap is a daily cap across reruns. ``scope="run"`` sums the current run and every
  run it was resumed from with ``--from-run`` (the chain shares a root run id), so
  resuming does not reset a per-run cap.
* The pipeline binds the ledger to the run id (and its chain root) with
  :meth:`SpendLedger.bind_run` when the run starts. Spend recorded before that
  (there should be none) is filed under run id 0.
* Spend that was billed must never be dropped. If writing it to the database fails,
  the failed statement is rolled back (so it cannot be committed later and counted
  twice), and :meth:`SpendLedger.add` and :meth:`Reservation.settle` retry, and as a last resort
  append the amount to a JSON-lines fallback file next to the database
  (:attr:`SpendLedger.fallback_path`) and log an error. :meth:`SpendLedger.spent`
  includes amounts found in the fallback file. Every fallback record carries the
  database's random ``db_id``; records of another database are ignored, and a
  record that cannot be matched to an event row never lowers a total. At run
  start the pipeline calls :meth:`bot_farm_silence_tracker.storage.Storage.reconcile_fallback`,
  which moves the file aside and merges this database's records into
  ``spend_events``; until a record is merged, the ledger still counts it in the
  moved-aside archive. Appends and the move share a file lock. Each record has a
  random ``nonce``, so two identical charges are two records. The pipeline also
  calls :meth:`bot_farm_silence_tracker.storage.Storage.reclaim_stale_reservations`, which closes
  reservations of finished runs at their reserved (worst-case) amount.

The numbers are the adapter's estimates, not the platform's invoice.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bot_farm_silence_tracker.sources.errors import BudgetExceeded
from bot_farm_silence_tracker.storage import (
    FALLBACK_SUFFIX,
    fallback_lock,
    legacy_record_expired,
    load_spend_fallback,
    load_spend_fallback_all,
)
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, utcnow

if TYPE_CHECKING:
    from bot_farm_silence_tracker.storage import Storage

log = logging.getLogger(__name__)

SpendScope = Literal["day", "run"]
SPEND_SCOPES: tuple[str, ...] = ("day", "run")

#: Attempts to write billed spend to the database before using the fallback file.
WRITE_ATTEMPTS = 3
#: Seconds between those attempts (doubled each time).
WRITE_RETRY_DELAY = 0.2

_MICRO = 1_000_000


def _to_micro(usd: float) -> int:
    return round(float(usd) * _MICRO)


def _day(when: datetime) -> str:
    return ensure_utc(when).strftime("%Y-%m-%d")


def _fallback_path_of(storage: Storage) -> Path:
    path = getattr(storage, "spend_fallback_path", None)
    if path is not None:
        return Path(path)
    db = str(storage.path)  # pragma: no cover - storage without spend_fallback_path
    base = (
        Path(tempfile.gettempdir()) / "bot-farm-silence-tracker-memory.db"
        if db == ":memory:"
        else Path(db)
    )
    return base.with_name(base.name + FALLBACK_SUFFIX)


def _record_tags(storage: Storage) -> dict[str, str]:
    tags = getattr(storage, "fallback_record_tags", None)
    return tags() if tags is not None else {}


def _event_key(storage: Storage, rec: dict[str, Any]) -> int | tuple[str, int]:
    """The spend event a fallback settle record names (see
    :meth:`bot_farm_silence_tracker.storage.Storage.fallback_event_key`)."""
    key_of = getattr(storage, "fallback_event_key", None)
    return key_of(rec) if key_of is not None else int(rec["event_id"])


@dataclass
class Reservation:
    """Spend reserved by :meth:`SpendLedger.reserve`. Call :meth:`settle` once."""

    ledger: SpendLedger
    event_id: int
    usd: float
    scope: str
    settled: bool = field(default=False)
    #: Number of :meth:`settle` calls so far; the latest one wins.
    settle_count: int = field(default=0)

    def settle(self, actual_usd: float) -> None:
        """Replace the reserved amount with ``actual_usd`` (0 releases it).

        Idempotent: the settled amount is absolute, so calling it again overwrites
        the amount instead of adding to it, also after an earlier settle went to the
        fallback file. Never drops billed spend: database errors are rolled back,
        retried and then written to the fallback file.
        """
        self.settle_count += 1
        self.ledger._settle(self, actual_usd)
        self.settled = True


class SpendLedger:
    """Platform-scoped view of the ``spend_events`` table for one scan."""

    def __init__(
        self,
        storage: Storage,
        platform: str,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.storage = storage
        self.platform = platform
        self.run_id: int | None = None
        #: First run of the ``--from-run`` chain; equals ``run_id`` for a fresh scan.
        self.root_run_id: int | None = None
        self._clock = clock
        self._sleep = sleep
        self._warned_foreign = False
        self._warned_expired = False
        #: Serializes the cap check and the record of :meth:`reserve`, :meth:`add` and
        #: settles within this process. Paid adapters do not allow ``--concurrency``
        #: (the ledger writes through the scan's database connection, which belongs to
        #: the pipeline's thread), so this is a guard, not permission to call it from
        #: worker threads.
        #:
        #: The lock is held across :meth:`_write`, including its retry sleeps (a few
        #: short waits for a locked database). That is deliberate: releasing it during
        #: the sleep would let another caller's check and record run between a failed
        #: write and its retry, breaking the atomicity the lock exists for, and the
        #: only callers are paid adapters, which are single-threaded (their
        #: ``max_concurrency`` is 1), so no thread ever waits on it in practice.
        self._lock = threading.RLock()
        #: JSON-lines file that receives billed spend the database could not take
        #: (:attr:`bot_farm_silence_tracker.storage.Storage.spend_fallback_path`). An in-memory
        #: database gets a unique temp path.
        self.fallback_path: Path = _fallback_path_of(storage)

    def __repr__(self) -> str:
        return (
            f"SpendLedger(platform={self.platform!r}, run_id={self.run_id!r}, "
            f"root_run_id={self.root_run_id!r})"
        )

    def bind_run(self, run_id: int, root_run_id: int | None = None) -> None:
        """Attribute future spend to ``run_id``. Called by the pipeline.

        ``root_run_id`` defaults to the root stored for ``run_id`` in the runs table.
        """
        self.run_id = run_id
        self.root_run_id = (
            root_run_id if root_run_id is not None else self.storage.root_run_id(run_id)
        )

    # -- recording ---------------------------------------------------------------

    def _write(self, action: Callable[[], None], what: str, payload: dict[str, object]) -> None:
        """Run ``action``, retrying database errors, then fall back to the file."""
        delay = WRITE_RETRY_DELAY
        error: Exception | None = None
        for attempt in range(WRITE_ATTEMPTS):
            try:
                action()
                return
            except sqlite3.Error as exc:
                error = exc
                # Discard the failed statement so a later commit cannot apply it too.
                self._rollback()
                if attempt < WRITE_ATTEMPTS - 1:
                    self._sleep(delay)
                    delay *= 2
        self._fallback(what, payload, error)

    def _rollback(self) -> None:
        rollback = getattr(self.storage, "rollback", None)
        if rollback is None:  # pragma: no cover - storage without rollback
            return
        with contextlib.suppress(sqlite3.Error):  # Storage.rollback swallows these too.
            rollback()

    def _fallback(self, what: str, payload: dict[str, object], error: Exception | None) -> None:
        record = {
            "what": what,
            "platform": self.platform,
            "run_id": self.run_id or 0,
            "root_run_id": self.root_run_id,
            "db_id": getattr(self.storage, "db_id", None),
            "when": isoformat(self._clock()),
            "day": _day(self._clock()),
            # Two identical charges in the same instant must stay two records.
            "nonce": uuid.uuid4().hex,
            **_record_tags(self.storage),
            **payload,
        }
        try:
            self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with (
                fallback_lock(self.fallback_path),
                self.fallback_path.open("a", encoding="utf-8", newline="\n") as fh,
            ):
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:  # Nowhere left to write it: shout.
            log.critical(
                "Could not record billed spend anywhere (database: %s; fallback file %s: %s). "
                "Record this by hand: %s",
                error,
                self.fallback_path,
                exc,
                json.dumps(record, sort_keys=True),
            )
            return
        log.error(
            "Could not write spend to the database (%s); recorded it in %s instead.",
            error,
            self.fallback_path,
        )

    def _fallback_micro(self, scope: str) -> int:
        """Micro-USD in the fallback file that belongs to ``scope``.

        ``scope`` is ``"day"`` (today, UTC), ``"run"`` (the bound run's chain) or
        ``"this_run"`` (the bound run only).

        Only records of this database count: each record carries the database's
        random ``db_id`` (see :attr:`bot_farm_silence_tracker.storage.Storage.db_id`), and records
        of another database (a deleted or replaced cache file whose fallback file
        stayed behind) are ignored, with one warning. Records already merged by
        :meth:`bot_farm_silence_tracker.storage.Storage.reconcile_fallback` are skipped.

        ``add`` records count as written. ``settle`` records hold the absolute
        settled amount of one reservation (``settled_micro_usd``): per event id only
        the latest settle counts, and only when it is newer than the settle stored in
        the database, as the difference to the amount the database holds. A settle
        whose event row is missing never lowers the total: its settled amount is
        counted in full.

        Records without a ``db_id`` (written by older versions) cannot be matched
        to this database safely, so they only ever raise the total: a negative
        difference or correction is ignored. Such records older than
        :data:`bot_farm_silence_tracker.storage.LEGACY_FALLBACK_MAX_AGE` are ignored, with one
        warning.

        The active file and every archive that
        :meth:`~bot_farm_silence_tracker.storage.Storage.reconcile_fallback` moved aside but has
        not finished are read (deduplicated by record hash), so a record never
        drops out of the total between the move and the merge.

        A settle on a later UTC day than its reservation counts on the settle day
        too, as :meth:`~bot_farm_silence_tracker.storage.Storage.settle_spend` would record it
        with a mirror row: the settle day comes from the record's ``settle_day``.
        """
        records = load_spend_fallback_all(self.fallback_path)
        if not records:
            return 0
        now = self._clock()
        today = _day(now)
        runs: set[int] = set()
        if scope == "run" and self.root_run_id is not None:
            try:
                runs = set(self.storage.run_chain(self.root_run_id)) | {self.root_run_id}
            except sqlite3.Error:
                runs = {self.root_run_id}
        elif scope == "this_run":
            runs = {self.run_id or 0}

        def in_scope(platform: object, day: object, run_id: object) -> bool:
            if platform != self.platform:
                return False
            if scope == "day":
                return day == today
            return run_id in runs

        db_id = getattr(self.storage, "db_id", None)
        merged: set[str] = set()
        if any(rec.get("db_id") == db_id and db_id is not None for _, rec in records):
            merged_of = getattr(self.storage, "merged_fallback_hashes", None)
            merged = merged_of([h for h, _ in records]) if merged_of is not None else set()

        active = {h for h, _ in load_spend_fallback(self.fallback_path)}
        total = 0
        foreign = 0
        expired = 0
        settles: dict[int | tuple[str, int], tuple[bool, dict[str, Any]]] = {}
        for digest, rec in records:
            rec_db = rec.get("db_id")
            if rec_db is not None and rec_db != db_id:
                # Archived foreign records were warned about when they were active.
                foreign += digest in active
                continue
            if digest in merged:
                continue
            legacy = rec_db is None
            # A resumed chain can span more than the expiry age, and a chain's own
            # spend must keep counting against its budget however old the run is.
            in_chain = scope == "run" and rec.get("run_id") in runs
            if legacy and not in_chain and legacy_record_expired(rec, now):
                expired += 1
                continue
            if rec.get("what") == "settle" and "settled_micro_usd" in rec:
                try:
                    key = _event_key(self.storage, rec)
                except (KeyError, TypeError, ValueError, sqlite3.Error):
                    continue
                prev = settles.get(key)
                if prev is None or int(rec.get("seq") or 0) >= int(prev[1].get("seq") or 0):
                    settles[key] = (legacy, rec)
                continue
            if in_scope(rec.get("platform"), rec.get("day"), rec.get("run_id")):
                micro = int(rec.get("micro_usd", 0))
                total += max(0, micro) if legacy else micro
        if foreign and not self._warned_foreign:
            self._warned_foreign = True
            log.warning(
                "Ignoring %d spend records in %s that belong to another database "
                "(the cache database was replaced or deleted).",
                foreign,
                self.fallback_path,
            )
        if expired and not self._warned_expired:
            self._warned_expired = True
            log.warning(
                "Ignoring %d spend records in %s without a database id that are more "
                "than 7 days old; they cannot be matched to this database.",
                expired,
                self.fallback_path,
            )
        if settles:
            # A key that is not an id names a row that has no copy in the shared
            # ledger yet (see Storage.fallback_event_key): it matches no row.
            ids = [key for key in settles if isinstance(key, int)]
            try:
                rows = self.storage.spend_events(ids)
            except sqlite3.Error:
                rows = {}
            mirrors_of = getattr(self.storage, "spend_mirrors", None)
            try:
                mirrors = mirrors_of(ids) if mirrors_of is not None else {}
            except sqlite3.Error:
                mirrors = {}
            for event_id, (legacy, rec) in settles.items():
                row = rows.get(event_id) if isinstance(event_id, int) else None
                settled = max(0, int(rec["settled_micro_usd"]))
                if row is None:
                    # No matching row (deleted, or unreadable): never lower the total.
                    if in_scope(rec.get("platform"), rec.get("day"), rec.get("run_id")):
                        total += settled
                    continue
                if int(row.get("settle_seq") or 0) >= int(rec.get("seq") or 0):
                    continue  # A newer (or the same) settle reached the database.
                # What the database holds now against what it would hold with this
                # settle applied: the row, plus a mirror row on the settle day when
                # that is a later day than the reservation (mirrors never count
                # toward run totals).
                settle_day = rec.get("settle_day") or rec.get("day")
                delta = 0
                if in_scope(row["platform"], row["day"], row["run_id"]):
                    delta += settled - int(row["micro_usd"])
                if scope == "day":
                    for mirror in mirrors.get(event_id, []):
                        if in_scope(mirror["platform"], mirror["day"], mirror["run_id"]):
                            delta -= int(mirror["micro_usd"])
                    if (
                        settle_day
                        and settle_day != row["day"]
                        and settled > 0
                        and in_scope(row["platform"], settle_day, row["run_id"])
                    ):
                        delta += settled
                if legacy and delta < 0:
                    continue  # Cannot prove this record is about this row.
                total += delta
        return total

    def add(self, usd: float) -> None:
        """Record ``usd`` of estimated spend now. Zero and negative amounts are ignored.

        Not atomic with a cap check; see :meth:`reserve` for that.
        """
        micro = _to_micro(usd)
        if micro <= 0:
            return
        with self._lock:
            self._add_micro(micro)

    def _add_micro(self, micro: int) -> None:
        when = self._clock()
        self._write(
            lambda: self.storage.add_spend_micro(
                self.platform, micro, run_id=self.run_id, when=when
            ),
            "add",
            {"micro_usd": micro},
        )

    def reserve(self, usd: float, cap: float, scope: SpendScope | str = "day") -> Reservation:
        """Atomically check ``cap`` in ``scope`` and reserve ``usd`` against it.

        Call it before a billed request with the request's worst-case cost, then
        call :meth:`Reservation.settle` with the actual cost (0 when nothing was
        billed). Two processes sharing the database cannot both pass the cap.

        Raises:
            BudgetExceeded: spend in ``scope`` plus ``usd`` would exceed ``cap``, or
                the database could not record the reservation (fail closed).
            ValueError: unknown ``scope``.
        """
        if scope not in SPEND_SCOPES:
            raise ValueError(f"spend scope must be one of {SPEND_SCOPES}, got {scope!r}")
        with self._lock:
            return self._reserve(usd, cap, scope)

    def _reserve(self, usd: float, cap: float, scope: str) -> Reservation:
        micro = max(0, _to_micro(usd))
        cap_micro = _to_micro(cap) - self._fallback_micro(scope)
        try:
            event_id, spent = self.storage.reserve_spend(
                self.platform,
                micro,
                cap_micro,
                scope=scope,
                run_id=self.run_id,
                root_run_id=self.root_run_id,
                when=self._clock(),
            )
        except sqlite3.Error as exc:
            # Fail closed: without a reservation the cap cannot be enforced.
            self._rollback()
            raise BudgetExceeded(
                f"{self.platform} spend could not be reserved in the ledger database "
                f"({exc}); refusing the billed request so the ${cap:,.2f} per {scope} cap "
                "holds. Check the database (locked by another process, disk full or "
                "read-only) and resume the run."
            ) from exc
        if event_id is None:
            spent_usd = (spent + self._fallback_micro(scope)) / _MICRO
            raise BudgetExceeded(
                f"{self.platform} spend cap of ${cap:,.2f} per {scope} reached "
                f"(${spent_usd:,.4f} spent, ${usd:,.4f} more requested)"
            )
        return Reservation(ledger=self, event_id=event_id, usd=usd, scope=scope)

    def _settle(self, reservation: Reservation, actual_usd: float) -> None:
        with self._lock:
            self._settle_locked(reservation, actual_usd)

    def _settle_locked(self, reservation: Reservation, actual_usd: float) -> None:
        micro = max(0, _to_micro(actual_usd))
        when = self._clock()
        seq = max(1, reservation.settle_count)
        self._write(
            lambda: self.storage.settle_spend(reservation.event_id, micro, when=when, seq=seq),
            "settle",
            # Absolute amount keyed by event id, so a later settle (in the database or
            # here) replaces it instead of adding to it.
            {
                "event_id": reservation.event_id,
                "settled_micro_usd": micro,
                "reserved_micro_usd": _to_micro(reservation.usd),
                "seq": seq,
                # The settle's own UTC day and time, so the settle-day mirror row is
                # rebuilt from it (by the ledger now, by reconcile later) even when
                # the record is written after midnight.
                "settle_day": _day(when),
                "settled_at": isoformat(when),
            },
        )

    # -- reading -----------------------------------------------------------------

    def spent_today(self) -> float:
        """USD recorded for this platform on the current UTC day, across all runs."""
        return (
            self.storage.spend_today(self.platform, now=self._clock())
            + self._fallback_micro("day") / _MICRO
        )

    def spent_this_run(self) -> float:
        """USD recorded for the bound run only (0.0 before :meth:`bind_run`)."""
        if self.run_id is None:
            return 0.0
        return (
            self.storage.spend_for_run(self.run_id, self.platform)
            + self._fallback_micro("this_run") / _MICRO
        )

    def spent_this_chain(self) -> float:
        """USD recorded for the bound run and every run it resumed (0.0 before binding)."""
        if self.root_run_id is None:
            return 0.0
        return (
            self.storage.spend_for_chain(self.root_run_id, self.platform)
            + self._fallback_micro("run") / _MICRO
        )

    def spent(self, scope: SpendScope | str = "day") -> float:
        """USD spent in ``scope``: ``"day"`` (default) or ``"run"`` (the whole chain)."""
        if scope == "day":
            return self.spent_today()
        if scope == "run":
            return self.spent_this_chain()
        raise ValueError(f"spend scope must be one of {SPEND_SCOPES}, got {scope!r}")
