"""SQLite cache of accounts, activities and runs.

Re-runs read from here instead of refetching. Every cached entry carries
``fetched_at``. Only content hashes are stored, never raw text.

Tables:

* ``accounts``: identity, status, ``created_at``, adapter ``meta`` and, for the
  activity cache, which window was fetched and when.
* ``activities``: one row per activity.
* ``runs``: one row per scan with its parameters, the fetched account ids, a
  summary and ``root_run_id``, so ``bot-farm-silence-tracker report --run-id N`` can re-score
  from the cache. ``root_run_id`` is the first run of a ``--from-run`` chain (a
  fresh scan is its own root), so spend can be summed across resumes, and
  ``heartbeat_json``, what the scanning process last recorded about itself (see
  :meth:`Storage.beat_run`).
* ``run_sample``: the accounts each run sampled, in sample order, as JSON
  snapshots with a per-account fetch status (``pending``, ``fetched``, ``skipped``,
  ``error`` or, for two-phase sampling, ``prechecked``), the two-phase ``stratum``,
  ``inclusion_prob`` and ``phase2`` selection (``NULL`` in census runs), whether the
  fetched history was ``truncated`` and the ``window_end`` the account was scored
  against (where its data ended). Written right after sampling, before any activity
  is fetched, so ``bot-farm-silence-tracker scan --from-run N`` can reuse the sample of an
  interrupted run without paying for discovery again. The snapshot is updated after
  each fetch with the account's status.
* ``spend_events``: estimated spend in integer micro-USD, one row per recorded
  amount or reservation, keyed by platform, UTC day and run. See
  :mod:`bot_farm_silence_tracker.ledger`. Older databases kept a ``spend_ledger`` table; it is
  migrated on open. The databases in the cache folder keep their spend in one shared
  ledger instead, ``<cache>/spend.db`` (see :func:`uses_shared_spend`), attached to
  the connection as ``spend``; their own table is copied there on open
  (:meth:`Storage._sync_own_spend`) and no longer written.
* ``request_log``: per-request instrumentation, one row per HTTP attempt of a run
  (adapters that record it: ``reddit-archive``), with the phase, endpoint kind, HTTP
  status (``NULL`` when no response arrived), latency, page ``limit``, time window
  span in seconds, the request rate at send time, for a 422 its kind (``timeout``,
  ``load`` or ``other``), and the waits: ``wait_s`` (the wait applied after this
  response, ``NULL`` when none), ``reset_s`` (the ``X-RateLimit-Reset`` header) and
  ``retry_after_s`` (the ``Retry-After`` header), and ``send_seq``, the order in which
  the run's requests were sent. ``seq`` is the order rows were stored in; with
  ``--concurrency`` the two can differ, so sort by ``send_seq`` for the true send
  order. No query parameters other than the page ``limit``, no handles and no
  content are stored. The table is created by ``CREATE TABLE IF NOT EXISTS`` on open,
  and the wait columns and ``send_seq`` are added to an older table by the usual
  column migration (``send_seq`` is ``NULL`` in rows written before it existed).
* ``meta``: key/value facts about the database itself. ``db_id`` is a random id
  created on first open; the spend ledger writes it into every fallback record, so
  a fallback file left over from a deleted or replaced database is recognized and
  ignored instead of being matched against unrelated event ids.
* ``spend_fallback_merged``: hashes of fallback-file records that
  :meth:`Storage.reconcile_fallback` already merged into ``spend_events``, so they
  are never counted twice.

A database that cannot be written (a read-only file or directory) is opened
read-only: the schema is not created and migrations are skipped. That is enough
for ``bot-farm-silence-tracker report``, which only reads. Readers of a read-only database treat
tables and columns that an older version did not create as empty. When SQLite
cannot open the file even with ``mode=ro`` (a WAL database in a directory where
the ``-shm`` file cannot be created), it is opened with ``immutable=1``.

The connection uses WAL journaling and a 30-second busy timeout, so several scans
can share one cache database. Every open holds the database's access lock shared, so
the move of the old database (which takes it exclusively) never moves a file that is
open, and a scan holds the lock of its run chain (:meth:`Storage.lock_run`). Lock
files live in a ``locks`` folder next to the database.
"""

from __future__ import annotations

import contextlib
import errno
import glob
import hashlib
import json
import logging
import os
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from bot_farm_silence_tracker.models import STATUS_OPTED_OUT, Account, Activity
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, parse_datetime, utcnow

log = logging.getLogger(__name__)


#: Whether this is Windows; a module constant so tests can exercise both branches.
_IS_WINDOWS = os.name == "nt"


def _default_db_path(*, windows: bool = _IS_WINDOWS) -> Path:
    """The default cache database path for this platform.

    POSIX systems use ``~/.cache/bot-farm-silence-tracker/bot-farm-silence-tracker.db``.
    Windows uses ``%LOCALAPPDATA%\\bot-farm-silence-tracker\\bot-farm-silence-tracker.db``,
    falling back to the POSIX-style path under the home directory when ``LOCALAPPDATA`` is
    unset.
    """
    posix = Path("~/.cache/bot-farm-silence-tracker/bot-farm-silence-tracker.db").expanduser()
    local = os.environ.get("LOCALAPPDATA") if windows else None
    if not local:
        return posix
    return Path(local) / "bot-farm-silence-tracker" / "bot-farm-silence-tracker.db"


#: Where the cache database lived before outage events: one database for every scan.
#: :func:`prepare_event_db` moves it into the default event's folder.
DEFAULT_DB_PATH = _default_db_path()

#: Environment variable that moves the whole cache folder (all events' databases).
CACHE_DIR_ENV = "BOT_FARM_SILENCE_TRACKER_CACHE_DIR"
#: File name of every cache database.
DB_FILE_NAME = "bot-farm-silence-tracker.db"
#: Sub-folder of the cache folder that holds one folder per outage event.
EVENTS_DIR = "events"
#: File in the cache folder where moves of the old database are noted.
MIGRATION_LOG = "migration.log"
#: Suffix of the copy of the old database kept after a move to another device.
BACKUP_SUFFIX = ".pre-events-backup"
#: Lock taken around a move of the old database (``migration`` plus the lock suffix).
MIGRATION_LOCK = "migration"
#: Old paths already warned about in this process (see :func:`migrate_legacy_db`).
_REAPPEARED_WARNED: set[str] = set()


def cache_dir(*, windows: bool = _IS_WINDOWS) -> Path:
    """The cache folder: ``BOT_FARM_SILENCE_TRACKER_CACHE_DIR``, else the folder of
    :data:`DEFAULT_DB_PATH` for this platform."""
    override = os.environ.get(CACHE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _default_db_path(windows=windows).parent


def legacy_db_path() -> Path:
    """The single database of versions before outage events, in :func:`cache_dir`."""
    return cache_dir() / DB_FILE_NAME


def event_db_path(event_id: str) -> Path:
    """The cache database of one outage event: ``<cache>/events/<event-id>/<file>``.

    Raises ``ValueError`` for an id that is not an event id, so it can never name a
    path outside the cache folder.
    """
    from bot_farm_silence_tracker.events import valid_event_id

    if not valid_event_id(event_id):
        raise ValueError(f"not an event id: {event_id!r}")
    return cache_dir() / EVENTS_DIR / event_id / DB_FILE_NAME


def prepare_event_db(event_id: str, *, legacy_event_id: str) -> Path:
    """The database path to use for ``event_id``.

    For the event the old single database belongs to (``legacy_event_id``, the
    default event unless the events file says otherwise), that database
    (:func:`legacy_db_path`) is first moved into the event's folder
    (:func:`migrate_legacy_db`). When it cannot be moved this time (another scan has
    it open, say), the old path is returned, so no run is ever hidden.
    """
    target = event_db_path(event_id)
    if event_id != legacy_event_id:
        return target
    return migrate_legacy_db(legacy_db_path(), target)


def _note_migration(text: str) -> None:
    log.warning("%s", text)
    try:
        folder = cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / MIGRATION_LOG).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{isoformat(utcnow())} {text}\n")
    except OSError:
        pass  # The warning above is the note; the log file is a convenience.


def _move_file(src: Path, dst: Path) -> bool:
    """Move ``src`` to ``dst`` by an atomic rename; return ``False`` when it was copied.

    Across devices a rename is impossible: the file is copied to a temporary name
    next to ``dst``, flushed, renamed into place, and ``src`` is kept as
    ``<src>.pre-events-backup``.
    """
    try:
        os.replace(src, dst)
        return True
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp = dst.with_name(dst.name + ".migrating")
    try:
        with src.open("rb") as reader, tmp.open("wb") as writer:
            while chunk := reader.read(1 << 20):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(src, src.with_name(src.name + BACKUP_SUFFIX))
    return False


def _warn_reappeared(legacy: Path, target: Path) -> None:
    """Warn (once per process) about a database at the old path next to a moved one.

    It was created after the move, by an older version or by a process that still
    had the old database open and reopened it by path; its runs are not in the
    event's database.
    """
    if not legacy.is_file() or str(legacy) in _REAPPEARED_WARNED:
        return
    _REAPPEARED_WARNED.add(str(legacy))
    log.warning(
        "A cache database exists at the old path %s although it was moved to %s. It was "
        "created after the move (by an older version, or by a process that had the old "
        "database open); its runs are not in the event's database. Open it with --db %s.",
        legacy,
        target,
        legacy,
    )


def migrate_legacy_db(legacy: Path, target: Path) -> Path:
    """Move the old single database ``legacy`` to ``target``; return the path to use.

    Nothing happens when ``target`` exists or ``legacy`` does not (a database that
    appears at the old path after the move is warned about), unless an earlier move
    stopped half way, which is then finished. Otherwise the move runs under two
    locks: the lock that other processes take for the same move (``migration.lock``
    in the folder of ``legacy``) and, exclusively, the access lock that every
    :class:`Storage` open holds shared (:func:`access_lock_path`). When a
    :class:`Storage` has the old database open, the access lock is busy: the move is
    skipped with a log note and tried again next time.

    The move is ordered so that a crash at any point loses nothing and is finished
    on the next run (see :func:`_migrate_locked`). It is noted in the log and in
    ``migration.log`` in the cache folder. The database itself is not changed: older
    versions can still open it at its new path.

    When the database is busy or cannot be moved, ``legacy`` is returned (the run
    uses the old path) and the move is tried again next time. A path that no longer
    exists is never returned for the old database.
    """
    pending = _pending_migration(legacy, target)
    if not pending and (target.exists() or not legacy.is_file()):
        _warn_reappeared(legacy, target)
        return target
    with _migration_lock(legacy.parent) as locked:
        if not locked:  # Never move without the lock: use the database in place.
            log.warning("Could not lock %s for the move; using it in place.", legacy)
            return legacy if legacy.is_file() else target
        # Another process may have moved it while this one waited for the lock.
        pending = _pending_migration(legacy, target)
        if not pending and (target.exists() or not legacy.is_file()):
            return target
        try:
            access = acquire_file_lock(access_lock_path(legacy), shared=False)
        except OSError as exc:
            log.warning("Could not lock %s for the move (%s); using it in place.", legacy, exc)
            return legacy if legacy.is_file() else target
        if access is None:
            log.warning(
                "The cache database %s is open in another scan or window; it is moved to %s "
                "on a later run.",
                legacy,
                target,
            )
            return legacy if legacy.is_file() else target
        with access:
            return _migrate_locked(legacy, target)


@contextlib.contextmanager
def _migration_lock(folder: Path) -> Iterator[bool]:
    """Hold the migration lock in ``folder``; yields whether it was taken.

    Unlike :func:`fallback_lock`, a lock that cannot be created or taken is not
    worked around: the caller then does not move anything.
    """
    try:
        handle = (folder / (MIGRATION_LOCK + FALLBACK_LOCK_SUFFIX)).open("a", encoding="utf-8")
    except OSError:
        yield False
        return
    try:
        try:
            _lock_exclusive(handle)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            _unlock(handle)
    finally:
        handle.close()


#: File in the folder of the old database that records a move: ``pending`` from
#: before the first file is touched until the last one is done, then ``done``. A
#: pending record makes the next open finish the move; a done one sends an open of
#: the old path to the new one.
MIGRATION_STATE = "migration-state.json"
_MIGRATION_PENDING = "pending"
_MIGRATION_DONE = "done"


def _same_file(a: Path | str, b: Path | str) -> bool:
    """Whether two paths name the same file, comparing their real folders."""
    first, second = Path(a), Path(b)
    return first.name == second.name and Path(os.path.realpath(first.parent)) == Path(
        os.path.realpath(second.parent)
    )


def _read_migration_state(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / MIGRATION_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_migration_state(legacy: Path, target: Path, state: str) -> None:
    """Record the move's ``state`` atomically (a temporary file, flushed, renamed)."""
    path = legacy.parent / MIGRATION_STATE
    tmp = path.with_name(path.name + ".tmp")
    record = {"legacy": str(legacy), "target": str(target), "state": state}
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _clear_migration_state(legacy: Path) -> None:
    with contextlib.suppress(OSError):
        (legacy.parent / MIGRATION_STATE).unlink()


def _pending_migration(legacy: Path, target: Path) -> bool:
    """Whether a move of ``legacy`` to ``target`` began and did not finish."""
    state = _read_migration_state(legacy.parent)
    return (
        state is not None
        and state.get("state") == _MIGRATION_PENDING
        and _same_file(str(state.get("legacy") or ""), legacy)
        and _same_file(str(state.get("target") or ""), target)
    )


def moved_database(path: Path) -> Path:
    """The path to open for ``path``, following a move of the old database.

    When ``path`` is the old database of a move that began and did not finish, the
    move is finished first (:func:`migrate_legacy_db`). When ``path`` no longer
    exists because the move is done, the new path is returned, so an open by the
    old path never creates an empty database there. Any other path is returned as
    it is.
    """
    state = _read_migration_state(path.parent)
    if state is None or not _same_file(str(state.get("legacy") or ""), path):
        return path
    target = Path(str(state.get("target") or ""))
    if state.get("state") == _MIGRATION_PENDING:
        migrate_legacy_db(path, target)
    if not path.exists() and target.is_file():
        log.info("The cache database %s was moved to %s; opening it there.", path, target)
        return target
    return path


def _is_fallback_family(name: str, prefix: str) -> bool:
    """Whether ``name`` is the spend fallback file of ``prefix`` or one of its archives."""
    return name.startswith(prefix + FALLBACK_SUFFIX)


def _merge_copy(src: Path, dst: Path) -> bool:
    """Make ``dst`` hold every line of ``src``; return whether ``dst`` was created.

    ``src`` is kept. A missing ``dst`` becomes a flushed copy of ``src``. An
    existing one (from a move that stopped half way) keeps its lines and gets the
    lines of ``src`` it lacks, compared by :func:`fallback_record_hash`, so running
    it twice never duplicates a spend record. The result is written to a
    temporary file and renamed into place.
    """
    created = not dst.exists()
    text = src.read_text(encoding="utf-8")
    if not created:
        kept = dst.read_text(encoding="utf-8")
        present = {fallback_record_hash(line) for line in kept.splitlines() if line.strip()}
        extra = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and fallback_record_hash(line) not in present
        ]
        if not extra:
            return False
        text = kept + ("" if not kept or kept.endswith("\n") else "\n") + "\n".join(extra) + "\n"
    tmp = dst.with_name(dst.name + ".migrating")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as writer:
            writer.write(text)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _FALLBACK_CACHE.pop(str(dst), None)
    return created


def _companions(legacy: Path) -> list[Path]:
    prefix = legacy.name
    return sorted(
        p
        for p in legacy.parent.glob(glob.escape(prefix) + "?*")
        if p.is_file() and not p.name.endswith((BACKUP_SUFFIX, ".migrating", FALLBACK_LOCK_SUFFIX))
    )


#: Byte of the ``-shm`` file that SQLite (3.22 and later) locks shared for as long as
#: a connection to the database is open: its "dead man switch".
_SQLITE_DMS_OFFSET = 128


def _held_by_other_process(db: Path) -> bool:
    """Whether a process other than this one has the WAL database ``db`` open.

    Asks the operating system whether the byte of ``<db>-shm`` that every SQLite
    connection holds shared can be locked exclusively. Meant for a caller that has
    no connection of its own to ``db`` open. Without ``fcntl`` (Windows, where an open
    file cannot be renamed anyway) or a ``-shm`` file the answer is ``False``.
    """
    if _fcntl is None:
        return False
    try:
        fd = os.open(db.with_name(db.name + "-shm"), os.O_RDWR)
    except OSError:
        return False
    try:
        _fcntl.lockf(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB, 1, _SQLITE_DMS_OFFSET, 0)
    except OSError as exc:
        return exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK)
    finally:
        os.close(fd)  # Closing drops the lock.
    return False


def _migrate_locked(legacy: Path, target: Path) -> Path:
    """The move of :func:`migrate_legacy_db`, with both of its locks held.

    The steps, in order, each safe to run again after a crash:

    1. The write-ahead log is checkpointed into the main file.
    2. The move is recorded as pending in :data:`MIGRATION_STATE`.
    3. The spend fallback file and its archives are copied next to ``target``
       (merged into any copy an earlier attempt left, see :func:`_merge_copy`); the
       originals stay, so until the main file moves every record is still read at
       the old path.
    4. The other files of the database (``-wal``, ``-shm``) are moved, then the
       main file, by an atomic rename or, across devices, by a copy that keeps the
       original as a backup (:func:`_move_file`).
    5. The original fallback files are merged into the copies once more and
       deleted, and the move is recorded as done.

    A crash before step 4 leaves the old database complete at the old path; one
    after it leaves every record at the new path. Either way the pending record
    makes the next open finish the move.
    """
    prefix = legacy.name
    main_moved = target.exists()
    if not main_moved:
        try:
            conn = sqlite3.connect(f"{legacy.resolve().as_uri()}?mode=rw", uri=True, timeout=5.0)
            try:
                busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("Could not move the cache database %s yet (%s); using it.", legacy, exc)
            return legacy if legacy.is_file() else target
        if busy and busy[0]:
            log.warning("The cache database %s is in use; it is moved on a later run.", legacy)
            return legacy
        # Versions before the access lock take no lock, so a process of one may still
        # have the database open, even idle between two writes, which the checkpoint
        # cannot see. Moving the file from under it would send its next reopen to a
        # new, empty database.
        if _held_by_other_process(legacy):
            log.warning(
                "The cache database %s is open in another process (an older version, "
                "perhaps); it is moved on a later run.",
                legacy,
            )
            return legacy
    try:
        _write_migration_state(legacy, target, _MIGRATION_PENDING)
    except OSError as exc:
        log.warning("Could not record the move of %s (%s); using it in place.", legacy, exc)
        return legacy if legacy.is_file() else target
    companions = _companions(legacy)
    fallbacks = [p for p in companions if _is_fallback_family(p.name, prefix)]
    others = [p for p in companions if p not in fallbacks]

    def dst_of(src: Path) -> Path:
        return target.with_name(target.name + src.name[len(prefix) :])

    moved: list[tuple[Path, Path, bool]] = []
    if not main_moved:
        copied: list[Path] = []
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for src in fallbacks:
                if _merge_copy(src, dst_of(src)):
                    copied.append(dst_of(src))
            for src in [*others, legacy]:
                moved.append((src, dst_of(src), _move_file(src, dst_of(src))))
        except OSError as exc:
            for src, dst, renamed in reversed(moved):  # Put back what was moved.
                with contextlib.suppress(OSError):
                    if renamed:
                        os.replace(dst, src)
                    else:
                        os.replace(src.with_name(src.name + BACKUP_SUFFIX), src)
                        dst.unlink()
            if target.exists():
                return target
            for dst in copied:  # The originals are still in place.
                with contextlib.suppress(OSError):
                    dst.unlink()
            _clear_migration_state(legacy)
            log.warning(
                "Could not move the cache database %s (%s); using it in place.", legacy, exc
            )
            return legacy if legacy.is_file() else target
    elif legacy.is_file():
        # A copy to another device reached the new path before the original was set
        # aside: the copy is complete, so the original becomes the backup now.
        try:
            os.replace(legacy, legacy.with_name(legacy.name + BACKUP_SUFFIX))
        except OSError as exc:
            log.warning("Could not set aside the moved cache database %s: %s", legacy, exc)
            return target
    try:
        for src in fallbacks:
            _merge_copy(src, dst_of(src))
            _retry_on_sharing_violation(src.unlink)
            _FALLBACK_CACHE.pop(str(src), None)
        _write_migration_state(legacy, target, _MIGRATION_DONE)
    except OSError as exc:
        # Every record is at the new path already; the next open tidies up.
        log.warning("Could not finish the move of %s (%s); it is finished later.", legacy, exc)
        return target
    if not moved:
        _note_migration(
            f"Finished the interrupted move of the cache database {legacy} to {target}."
        )
        return target
    how = (
        "by an atomic rename"
        if all(renamed for _, _, renamed in moved)
        else f"by a copy (another device); the original is kept as {legacy}{BACKUP_SUFFIX}"
    )
    _note_migration(
        f"Moved the cache database {legacy} to {target} {how}. Each outage event now has "
        "its own database; this one belongs to the default event."
    )
    return target


#: Seconds a connection waits for a lock held by another process.
BUSY_TIMEOUT_SECONDS = 30.0
#: Micro-USD per USD. The ledger stores integers to avoid float drift.
MICRO = 1_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    created_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    meta_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    activity_since TEXT,
    activity_until TEXT,
    activity_fetched_at TEXT,
    PRIMARY KEY (platform, user_id)
);
CREATE TABLE IF NOT EXISTS activities (
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    community TEXT,
    content_hash TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_user ON activities (platform, user_id, ts);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    platform TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    params_json TEXT NOT NULL,
    account_ids_json TEXT NOT NULL DEFAULT '[]',
    summary_json TEXT NOT NULL DEFAULT '{}',
    root_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS run_sample (
    run_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    account_json TEXT NOT NULL,
    fetch_status TEXT NOT NULL DEFAULT 'pending',
    truncated INTEGER,
    window_end TEXT,
    stratum TEXT,
    inclusion_prob REAL,
    phase2 INTEGER,
    PRIMARY KEY (run_id, platform, user_id)
);
CREATE INDEX IF NOT EXISTS idx_run_sample_order ON run_sample (run_id, position);
CREATE TABLE IF NOT EXISTS spend_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    day TEXT NOT NULL,
    run_id INTEGER NOT NULL DEFAULT 0,
    micro_usd INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'settled',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_day ON spend_events (platform, day);
CREATE INDEX IF NOT EXISTS idx_spend_run ON spend_events (run_id);
CREATE TABLE IF NOT EXISTS request_log (
    run_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    platform TEXT NOT NULL,
    at TEXT NOT NULL,
    phase TEXT,
    endpoint TEXT,
    status INTEGER,
    latency_ms REAL,
    page_limit TEXT,
    window_s INTEGER,
    rps REAL,
    slow_kind TEXT,
    attempt INTEGER,
    wait_s REAL,
    reset_s REAL,
    retry_after_s REAL,
    send_seq INTEGER,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_fallback_merged (
    record_hash TEXT PRIMARY KEY,
    merged_at TEXT NOT NULL
);
"""

#: Columns added after the first release, with their DDL, for in-place migration.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("runs", "root_run_id", "INTEGER"),
    ("accounts", "activity_requested_until", "TEXT"),
    ("run_sample", "truncated", "INTEGER"),
    ("spend_events", "settle_seq", "INTEGER"),
    ("spend_events", "mirror_of", "INTEGER"),
    ("run_sample", "window_end", "TEXT"),
    ("request_log", "wait_s", "REAL"),
    ("request_log", "reset_s", "REAL"),
    ("request_log", "retry_after_s", "REAL"),
    ("request_log", "send_seq", "INTEGER"),
    ("run_sample", "stratum", "TEXT"),
    ("run_sample", "inclusion_prob", "REAL"),
    ("run_sample", "phase2", "INTEGER"),
    ("runs", "heartbeat_json", "TEXT"),
    ("spend_events", "db_id", "TEXT"),
    ("spend_events", "src_event_id", "INTEGER"),
)

#: File name of the spend ledger that every database in the cache folder shares.
SPEND_DB_NAME = "spend.db"
#: Schema name the shared spend ledger is attached under.
_SPEND_SCHEMA_NAME = "spend"
#: Value of ``ledger`` in fallback records written while the shared ledger was in
#: use: their ``event_id`` names a row of the shared ledger, not of the database's
#: own ``spend_events`` table.
SHARED_LEDGER = "shared"

#: The shared spend ledger (:data:`SPEND_DB_NAME`). ``db_id`` is the id of the
#: database that recorded the row (its runs are numbered in that database), and
#: ``src_event_id`` the row's id in that database's own ``spend_events`` table when
#: it was copied from there (``NULL`` for rows written to the shared ledger).
_SHARED_SPEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS {s}.spend_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    day TEXT NOT NULL,
    run_id INTEGER NOT NULL DEFAULT 0,
    micro_usd INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'settled',
    updated_at TEXT NOT NULL,
    settle_seq INTEGER,
    mirror_of INTEGER,
    db_id TEXT,
    src_event_id INTEGER
);
CREATE INDEX IF NOT EXISTS {s}.idx_spend_day ON spend_events (platform, day);
CREATE INDEX IF NOT EXISTS {s}.idx_spend_owner ON spend_events (db_id, run_id);
CREATE UNIQUE INDEX IF NOT EXISTS {s}.idx_spend_source ON spend_events (db_id, src_event_id);
CREATE TABLE IF NOT EXISTS {s}.spend_fallback_merged (
    record_hash TEXT PRIMARY KEY,
    merged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def shared_spend_path() -> Path:
    """The spend ledger every database in the cache folder shares: ``<cache>/spend.db``."""
    return cache_dir() / SPEND_DB_NAME


def uses_shared_spend(db: Path) -> bool:
    """Whether the database at ``db`` keeps its spend in :func:`shared_spend_path`.

    True for the databases in the cache folder: every outage event's
    (``<cache>/events/<id>/``) and the old single database (``<cache>/``), so a daily
    cap holds across events and custom cutoffs. A database elsewhere (``--db``)
    keeps its spend in its own ``spend_events`` table.
    """
    if str(db) == ":memory:":
        return False
    folder = Path(os.path.realpath(db.parent))
    cache = Path(os.path.realpath(cache_dir()))
    return folder == cache or (folder.parent == cache / EVENTS_DIR)


SPEND_RESERVED = "reserved"
SPEND_SETTLED = "settled"

#: Reservations older than this, whose run has finished, are reclaimed at run start.
STALE_RESERVATION_AGE = timedelta(hours=1)

SAMPLE_PENDING = "pending"
SAMPLE_FETCHED = "fetched"
SAMPLE_ERROR = "error"
#: Processed without fetching (a zero-cost skip or one over the budget). Runs made
#: before this status existed recorded such accounts as ``fetched``; readers tell
#: them apart by the snapshot's account status.
SAMPLE_SKIPPED = "skipped"
#: Two-phase sampling only: the phase-1 pre-check is done and no full fetch was
#: recorded: the account was not selected for phase 2, or its phase-2 fetch has not
#: happened yet (the ``phase2`` column tells which).
SAMPLE_PRECHECKED = "prechecked"


def _dt(value: str | None) -> datetime | None:
    return parse_datetime(value) if value else None


def _stored_handle(account: Account) -> str:
    """The handle to store: the account's ID in place of an opted-out account's handle."""
    return account.user_id if account.status == STATUS_OPTED_OUT else account.handle


def _stored_meta(account: Account) -> dict[str, Any]:
    """The meta to store: only the flag for an opted-out account."""
    return {"opted_out": True} if account.status == STATUS_OPTED_OUT else account.meta


def _stored_created_at(account: Account) -> str | None:
    """The creation time to store: none for an opted-out account."""
    if account.status == STATUS_OPTED_OUT or account.created_at is None:
        return None
    return isoformat(account.created_at)


def _scrubbed_snapshot(text: str) -> str:
    """A run-sample snapshot with what identifies an opted-out account removed.

    The handle becomes the ID, the creation time is dropped and the meta becomes
    ``{"opted_out": true}``. The snapshot's status is kept: it records what that run
    saw, and an earlier run's report reads the account's current status (``opted_out``)
    from the ``accounts`` table anyway.
    """
    data = json.loads(text)
    data["handle"] = data.get("user_id")
    data["created_at"] = None
    data["meta"] = {"opted_out": True}
    return json.dumps(data, sort_keys=True)


def _account_to_json(account: Account) -> str:
    return json.dumps(
        {
            "platform": account.platform,
            "user_id": account.user_id,
            "handle": _stored_handle(account),
            "created_at": _stored_created_at(account),
            "meta": _stored_meta(account),
            "status": account.status,
        },
        sort_keys=True,
    )


def _account_from_json(text: str) -> Account:
    data = json.loads(text)
    return Account(
        platform=data["platform"],
        user_id=data["user_id"],
        handle=data["handle"],
        created_at=_dt(data.get("created_at")),
        meta=data.get("meta") or {},
        status=data.get("status") or "active",
    )


def _utc_day(when: datetime) -> str:
    return ensure_utc(when).strftime("%Y-%m-%d")


#: Suffix of the JSON-lines file next to the database that receives billed spend the
#: database could not take (see :mod:`bot_farm_silence_tracker.ledger`).
FALLBACK_SUFFIX = ".spend-fallback.jsonl"

#: Infix of fallback files :meth:`Storage.reconcile_fallback` moved aside
#: (``<fallback>.merged-<timestamp>-<nonce>``). Their unmerged records still count.
FALLBACK_ARCHIVE_INFIX = ".merged-"
#: Suffix added to an archive once every record in it is merged, copied forward or
#: belongs to another database; such archives are kept for audit and never read.
FALLBACK_DONE_SUFFIX = ".done"
#: Suffix of the lock file that serializes fallback appends and rotation.
FALLBACK_LOCK_SUFFIX = ".lock"
#: Records without a ``db_id`` (written by older versions) older than this are
#: ignored, with a warning: they cannot be matched to a database and would
#: otherwise be carried forward for ever.
LEGACY_FALLBACK_MAX_AGE = timedelta(days=7)
#: ``SQLITE_READONLY_DIRECTORY``: the database's directory is not writable. Not
#: exported by every Python version's ``sqlite3`` module, so it is spelled out.
SQLITE_READONLY_DIRECTORY = 1544
#: ``.done`` archives are kept for audit this long, then deleted.
DONE_ARCHIVE_MAX_AGE = timedelta(days=30)

_FALLBACK_CACHE: dict[str, tuple[tuple[int, int, int], list[tuple[str, dict[str, Any]]]]] = {}

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None  # type: ignore[assignment]

#: Seconds between attempts to take the Windows byte-range lock.
_WINDOWS_LOCK_POLL_SECONDS = 0.05
#: Attempts, and the first wait in seconds, for a rename or delete that Windows
#: refuses because another handle has the file open. The wait doubles each time.
_WINDOWS_RETRY_ATTEMPTS = 6
_WINDOWS_RETRY_FIRST_WAIT = 0.05
#: Windows error codes worth retrying: access denied, which a pending delete
#: reports, and a sharing violation.
_WINDOWS_RETRYABLE_ERRORS = (5, 32)


def _lock_exclusive(handle: Any) -> None:
    """Block until ``handle`` holds an exclusive lock on its file.

    POSIX uses ``flock``. Windows uses ``msvcrt.locking`` on the first byte, polled
    without a deadline so it blocks like ``flock`` does; Windows releases the lock
    when the handle is closed. Raises :class:`OSError` when locking is not possible.
    """
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:
        handle.seek(0)
        while True:
            try:
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
                    raise
                # Held by another handle: wait for it.
                time.sleep(_WINDOWS_LOCK_POLL_SECONDS)
    raise OSError("no file locking on this platform")


def _unlock(handle: Any) -> None:
    """Release the Windows byte-range lock taken by :func:`_lock_exclusive`.

    Windows releases the lock when the handle is closed, but only eventually, so it
    is released explicitly first. Errors are ignored: the handle is closed next
    either way. POSIX ``flock`` locks are released by the close itself.
    """
    if _fcntl is not None or _msvcrt is None:
        return
    with contextlib.suppress(OSError):
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


#: Byte slots of the Windows emulation of a shared lock: a shared holder locks one
#: free slot, an exclusive holder locks byte 0 and every slot at once.
_WINDOWS_SHARED_SLOTS = 256


def _try_lock(handle: Any, *, shared: bool) -> tuple[int, int] | None:
    """Try once, without waiting, to lock the file of the binary ``handle``.

    Returns the locked byte region ``(offset, length)`` (``(0, 0)`` on POSIX), or
    ``None`` when another handle holds a lock that conflicts. POSIX uses ``flock``,
    which also makes two handles of one process conflict. Windows has no shared
    ``msvcrt`` lock, so a shared holder locks one of :data:`_WINDOWS_SHARED_SLOTS`
    bytes and an exclusive holder locks all of them. Raises :class:`OSError` when
    locking is not possible.
    """
    if _fcntl is not None:
        try:
            _fcntl.flock(
                handle.fileno(), (_fcntl.LOCK_SH if shared else _fcntl.LOCK_EX) | _fcntl.LOCK_NB
            )
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return None
            raise
        return (0, 0)
    if _msvcrt is not None:
        regions = (
            [(1 + slot, 1) for slot in range(_WINDOWS_SHARED_SLOTS)]
            if shared
            else [(0, _WINDOWS_SHARED_SLOTS + 1)]
        )
        for offset, length in regions:
            handle.seek(offset)
            try:
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, length)
                return (offset, length)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
                    raise
        return None
    raise OSError("no file locking on this platform")


class FileLock:
    """A lock held on one lock file, taken by :func:`acquire_file_lock`.

    :meth:`release` gives it back; it is safe to call more than once.
    """

    def __init__(self, path: Path, handle: Any, region: tuple[int, int]) -> None:
        self.path = path
        self._handle = handle
        self._region = region

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if _fcntl is None and _msvcrt is not None:
                offset, length = self._region
                with contextlib.suppress(OSError):
                    handle.seek(offset)
                    _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, length)
        finally:
            handle.close()  # Closing releases a POSIX lock.

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


#: Seconds between attempts to take a lock that another handle holds.
_LOCK_POLL_SECONDS = 0.05


def acquire_file_lock(path: Path, *, shared: bool, wait: float = 0.0) -> FileLock | None:
    """Lock ``path`` (created with its folder when missing), shared or exclusive.

    Tries until ``wait`` seconds have passed (``0``: once). Returns the held
    :class:`FileLock`, or ``None`` when another handle kept a conflicting lock.
    Raises :class:`OSError` when the lock file cannot be created or locking is not
    possible on this platform.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("ab")
    try:
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            region = _try_lock(handle, shared=shared)
            if region is not None:
                return FileLock(path, handle, region)
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(_LOCK_POLL_SECONDS)
    except BaseException:
        handle.close()
        raise


#: Folder next to a database that holds its lock files (see :func:`access_lock_path`
#: and :func:`run_lock_path`). Lock files are kept: deleting one that another process
#: has just opened would let two processes hold "the" lock at once.
LOCKS_DIR = "locks"


def access_lock_path(db: Path) -> Path:
    """The lock every :class:`Storage` open holds shared and a move holds exclusive."""
    return db.parent / LOCKS_DIR / (db.name + ".access" + FALLBACK_LOCK_SUFFIX)


def run_lock_path(db: Path, run_id: int) -> Path:
    """The lock a scan holds for the whole of the run chain rooted at ``run_id``."""
    return db.parent / LOCKS_DIR / f"{db.name}.run-{int(run_id)}{FALLBACK_LOCK_SUFFIX}"


class RunInUse(RuntimeError):
    """The run is being scanned by another process (or another window of the app).

    ``heartbeat`` is what that scan last recorded about itself (``pid``, ``host``
    and ``last_seen``), or ``None`` when nothing was recorded.
    """

    def __init__(self, run_id: int, heartbeat: dict[str, Any] | None = None) -> None:
        self.run_id = run_id
        self.heartbeat = heartbeat
        where = ""
        if heartbeat:
            where = (
                f" (process {heartbeat.get('pid')} on {heartbeat.get('host')}, last seen "
                f"{heartbeat.get('last_seen')})"
            )
        super().__init__(
            f"run {run_id} is still being scanned by another process{where}. Wait for that "
            "scan to finish, or stop it, before you continue the run."
        )


def _retry_on_sharing_violation[**P, T](
    action: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run ``action(*args, **kwargs)``, retrying a :class:`PermissionError` on Windows.

    Windows refuses to rename or delete a file while another handle has it open
    (a concurrent reader, a virus scanner or the search indexer), where POSIX
    allows it. Those handles are short-lived, so the call is retried with a
    bounded exponential backoff; the last error is raised. Only access denied
    (``winerror`` 5) and sharing violations (32) are retried; any other
    :class:`PermissionError`, and every one on POSIX, is raised at once.
    """
    wait = _WINDOWS_RETRY_FIRST_WAIT
    for attempt in range(_WINDOWS_RETRY_ATTEMPTS):
        try:
            return action(*args, **kwargs)
        except PermissionError as exc:
            if (
                not _IS_WINDOWS
                or getattr(exc, "winerror", None) not in _WINDOWS_RETRYABLE_ERRORS
                or attempt == _WINDOWS_RETRY_ATTEMPTS - 1
            ):
                raise
            log.debug(
                "%s failed (%s); retrying in %.2fs (attempt %d of %d)",
                getattr(action, "__name__", action),
                exc,
                wait,
                attempt + 2,
                _WINDOWS_RETRY_ATTEMPTS,
            )
            time.sleep(wait)
            wait *= 2
    raise AssertionError("unreachable")  # pragma: no cover


_LOCK_WARNED = False


def _warn_lock_unavailable(lock_path: Path, exc: OSError) -> None:
    """Warn, once per process, that the fallback lock could not be taken."""
    global _LOCK_WARNED
    if _LOCK_WARNED:
        return
    _LOCK_WARNED = True
    log.warning(
        "Could not lock %s (%s); spend fallback appends and rotation will run unlocked, "
        "so concurrent processes may briefly undercount spend.",
        lock_path,
        exc,
    )


@contextlib.contextmanager
def fallback_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``<path>.lock`` for the duration of the block.

    The ledger takes it around every append to the fallback file and
    :meth:`Storage.reconcile_fallback` around moving the file aside, so a record is
    either in the file before it moves or in the fresh file after it, never written
    to a file already read. When the lock file cannot be created (a read-only
    directory), or the lock cannot be taken (no ``fcntl`` or ``msvcrt``, or the
    call fails), the block runs unlocked and a warning is logged once per process.
    """
    lock_path = path.with_name(path.name + FALLBACK_LOCK_SUFFIX)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a", encoding="utf-8")
    except OSError as exc:
        _warn_lock_unavailable(lock_path, exc)
        yield
        return
    try:
        try:
            _lock_exclusive(handle)
        except OSError as exc:
            _warn_lock_unavailable(lock_path, exc)
        yield
    finally:
        _unlock(handle)
        handle.close()  # Closing releases a POSIX lock.


def fallback_archives(path: Path) -> list[Path]:
    """Archives of ``path`` that may still hold unmerged records, oldest first."""
    pattern = path.name + FALLBACK_ARCHIVE_INFIX + "*"
    try:
        found = [
            p
            for p in path.parent.glob(pattern)
            if not p.name.endswith((FALLBACK_DONE_SUFFIX, FALLBACK_LOCK_SUFFIX))
        ]
    except OSError:
        return []
    return sorted(found)


def load_spend_fallback_all(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Like :func:`load_spend_fallback`, over the active file and its live archives.

    Records are deduplicated by hash (a legacy record copied forward into the
    active file is the same line as its original in the archive).
    """
    # Read the active file before listing the archives. Another process may rename
    # the active file to an archive at any moment. If the archives were listed
    # first, a rename between the listing and the read would hide the records from
    # both. Read first, a rename before the read leaves the file missing but the
    # archive is then listed, and a rename after the read is covered by the earlier
    # read. Hash deduplication handles any record seen twice.
    active = load_spend_fallback(path)
    seen: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    for records in [*(load_spend_fallback(a) for a in fallback_archives(path)), active]:
        for digest, rec in records:
            if digest not in seen:
                seen.add(digest)
                out.append((digest, rec))
    return out


def _only_carried_legacy(path: Path, now: datetime, keep_runs: frozenset[int]) -> bool:
    """Whether every record of the active file is a kept legacy one (no ``db_id``).

    A legacy record is kept when it is not expired, or when its run is in
    ``keep_runs`` (see :func:`legacy_record_dropped`). Such a file only holds lines
    carried forward from an archive, which stay readable in place; rotating it
    would archive them again on every run. A file with a droppable legacy line is
    still rotated, so that line is dropped.
    """
    # Parsed directly rather than through the cache: this runs under the fallback
    # lock, and a cache miss must not trigger anything that takes the lock again.
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    found = False
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            found = True
            if rec.get("db_id") is not None or legacy_record_dropped(rec, now, keep_runs):
                return False
    return found


def _prune_done_archives(path: Path, now: datetime) -> None:
    """Delete ``.done`` archives of ``path`` older than :data:`DONE_ARCHIVE_MAX_AGE`."""
    cutoff = ensure_utc(now).timestamp() - DONE_ARCHIVE_MAX_AGE.total_seconds()
    try:
        done = list(
            path.parent.glob(path.name + FALLBACK_ARCHIVE_INFIX + "*" + FALLBACK_DONE_SUFFIX)
        )
    except OSError:
        return
    for archive in done:
        try:
            if archive.stat().st_mtime < cutoff:
                _retry_on_sharing_violation(archive.unlink)
        except OSError as exc:
            log.warning("Could not prune the old spend archive %s: %s", archive, exc)


def legacy_record_expired(rec: dict[str, Any], now: datetime) -> bool:
    """Whether a record without a ``db_id`` is older than :data:`LEGACY_FALLBACK_MAX_AGE`.

    A record without a readable ``when`` never expires.
    """
    when = rec.get("when")
    if not when:
        return False
    try:
        stamp = parse_datetime(str(when))
    except ValueError:
        return False
    return ensure_utc(now) - ensure_utc(stamp) > LEGACY_FALLBACK_MAX_AGE


def legacy_record_dropped(rec: dict[str, Any], now: datetime, keep_runs: frozenset[int]) -> bool:
    """Whether reconcile drops a record without a ``db_id``.

    It is dropped when :func:`legacy_record_expired`, unless its ``run_id`` is in
    ``keep_runs``: the runs of a chain that may still be resumed. The ledger counts
    a chain's own spend however old it is, so reconcile must not drop it before the
    chain is finished.
    """
    if not legacy_record_expired(rec, now):
        return False
    try:
        return int(rec.get("run_id") or 0) not in keep_runs
    except (TypeError, ValueError):
        return True


def fallback_record_hash(line: str) -> str:
    """Stable id of one fallback-file line (its SHA-256 hex digest)."""
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()


def load_spend_fallback(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(hash, record)`` for every JSON object line of the fallback file.

    Missing or unreadable files give ``[]``; malformed lines are skipped. The parse
    is cached in-process and reused while the file's inode, size and modification
    time are unchanged, so the ledger can consult it before every reservation.
    """
    try:
        st = os.stat(path)
    except OSError:
        _FALLBACK_CACHE.pop(str(path), None)
        return []
    stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _FALLBACK_CACHE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append((fallback_record_hash(line), rec))
    _FALLBACK_CACHE[str(path)] = (stamp, out)
    return out


def _record_time(rec: dict[str, Any], key: str) -> datetime | None:
    value = rec.get(key)
    if not value:
        return None
    try:
        return ensure_utc(parse_datetime(str(value)))
    except ValueError:
        return None


def _is_read_only_error(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "readonly" in text or "read-only" in text or "unable to open" in text


def to_micro(usd: float) -> int:
    """Convert USD to integer micro-USD, rounding to the nearest micro-dollar."""
    return round(float(usd) * MICRO)


def from_micro(micro: int) -> float:
    """Convert integer micro-USD back to USD."""
    return micro / MICRO


#: Seconds between heartbeats of a running scan (see :class:`RunHeartbeat`).
HEARTBEAT_SECONDS = 60.0


def heartbeat_record(now: datetime) -> dict[str, Any]:
    """What a scan records about itself: process id, host name and the time."""
    return {"pid": os.getpid(), "host": socket.gethostname(), "last_seen": isoformat(now)}


class RunHeartbeat:
    """Refreshes a running scan's heartbeat every :data:`HEARTBEAT_SECONDS`.

    It runs on its own thread with its own connection, so a long wait in the scan
    (a rate limit, a lost network) does not stop it. Errors are logged and the next
    beat is tried: the heartbeat only informs, the run lock decides.
    """

    def __init__(self, path: Path, run_id: int, *, interval: float = HEARTBEAT_SECONDS) -> None:
        self.path = path
        self.run_id = run_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name="run-heartbeat", daemon=True)

    def start(self) -> Self:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_SECONDS)
                try:
                    conn.execute(
                        "UPDATE runs SET heartbeat_json = ? WHERE run_id = ?",
                        (json.dumps(heartbeat_record(utcnow()), sort_keys=True), self.run_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                log.debug("Could not record the heartbeat of run %s: %s", self.run_id, exc)


class Storage:
    """Thin wrapper over a SQLite connection. Use as a context manager."""

    def __init__(self, path: Path | str | None = None, *, read_only: bool = False) -> None:
        """Open (and create or migrate) the database at ``path``.

        With ``read_only``, or when the database cannot be written, the file is
        opened with SQLite's ``mode=ro`` and no schema change is attempted;
        :attr:`read_only` tells which happened.
        """
        if path:
            self.path = Path(path).expanduser()
        else:
            from bot_farm_silence_tracker.events import load_events

            catalog = load_events()
            self.path = prepare_event_db(catalog.default_id, legacy_event_id=catalog.legacy_id)
        self.read_only = False
        #: Random id of this database (``meta.db_id``); ``None`` when a read-only
        #: database predates it.
        self.db_id: str | None = None
        #: Whether spend is kept in the shared ledger (:func:`shared_spend_path`)
        #: rather than in this database's own ``spend_events`` table.
        self.spend_shared = False
        #: Schema that holds the spend tables: ``spend`` (the attached shared ledger)
        #: or ``main``.
        self._spend = "main"
        self._access: FileLock | None = None
        memory = str(self.path) == ":memory:"
        if not memory:
            self.path = moved_database(self.path)
            self._access = self._take_access_lock()
        try:
            self._open(memory=memory, read_only=read_only)
        except BaseException:
            self._release_access_lock()
            raise

    def _take_access_lock(self) -> FileLock | None:
        """Hold the database's access lock shared (see :func:`access_lock_path`).

        A move of the old database takes it exclusively, so it never moves a file
        this connection has open. While a move holds it, this waits up to
        :data:`BUSY_TIMEOUT_SECONDS`, then follows the move to the new path. When
        the lock cannot be created (a read-only folder) or taken, the database is
        opened without it.
        """
        for _ in range(2):
            try:
                held = acquire_file_lock(
                    access_lock_path(self.path), shared=True, wait=BUSY_TIMEOUT_SECONDS
                )
            except OSError as exc:
                log.debug("Opening %s without its access lock: %s", self.path, exc)
                return None
            if held is None:
                log.warning(
                    "The cache database %s stayed locked by a move for %.0f seconds; "
                    "opening it anyway.",
                    self.path,
                    BUSY_TIMEOUT_SECONDS,
                )
                return None
            moved = moved_database(self.path)
            if moved == self.path:
                return held
            held.release()  # Moved while this open waited: lock the new path instead.
            self.path = moved
        return None

    def _release_access_lock(self) -> None:
        access, self._access = self._access, None
        if access is not None:
            access.release()

    def _open(self, *, memory: bool, read_only: bool) -> None:
        if read_only and not memory:
            self._open_read_only()
            return
        if not memory:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                if not self.path.exists():
                    raise
        self._conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_SECONDS)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
        # Deleted rows (such as an opted-out account's handle) are overwritten with
        # zeros instead of lingering in free pages of the database file.
        self._conn.execute("PRAGMA secure_delete = ON")
        try:
            if not memory:
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self.db_id = self._ensure_db_id()
        except sqlite3.OperationalError as exc:
            if memory or not _is_read_only_error(exc) or not self.path.exists():
                raise
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._open_read_only()
            return
        if uses_shared_spend(self.path):
            self._attach_shared_spend()

    def _attach_shared_spend(self) -> None:
        """Keep spend in the shared ledger, copying this database's own spend there.

        The ledger is attached to this connection, so a reservation still checks the
        cap and records the row in one ``BEGIN IMMEDIATE`` transaction, now across
        every event's database. This database's own ``spend_events`` rows (written by
        an older version, which may still be running) are copied there by
        :meth:`_sync_own_spend`. When the shared ledger cannot be used, spend stays in
        this database, with a warning.
        """
        path = shared_spend_path()
        try:
            self._conn.execute(f"ATTACH DATABASE ? AS {_SPEND_SCHEMA_NAME}", (str(path),))
        except sqlite3.Error as exc:
            log.warning(
                "Could not open the shared spend ledger %s (%s); spend caps of this event "
                "count only its own spend.",
                path,
                exc,
            )
            return
        try:
            self._conn.execute(f"PRAGMA {_SPEND_SCHEMA_NAME}.journal_mode = WAL")
            self._conn.execute(f"PRAGMA {_SPEND_SCHEMA_NAME}.secure_delete = ON")
            self._conn.executescript(_SHARED_SPEND_SCHEMA.format(s=_SPEND_SCHEMA_NAME))
            self._spend = _SPEND_SCHEMA_NAME
            self.spend_shared = True
            self._sync_own_spend()
        except sqlite3.Error as exc:
            self.rollback()
            self._spend = "main"
            self.spend_shared = False
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute(f"DETACH DATABASE {_SPEND_SCHEMA_NAME}")
            log.warning(
                "Could not use the shared spend ledger %s (%s); spend caps of this event "
                "count only its own spend.",
                path,
                exc,
            )

    def _sync_own_spend(self) -> None:
        """Copy this database's own ``spend_events`` rows into the shared ledger.

        Idempotent: each row is keyed by this database's id and its own event id, so
        a copy is updated in place (a settle or a reclaim that an older version
        wrote), and copies of rows that are gone (a settle-day mirror that was
        replaced) are deleted. The already merged fallback hashes are copied too.
        A fingerprint of the own table, stored in the shared ledger in the same
        transaction, skips the copy while nothing changed. New spend is only ever
        written to the shared ledger.
        """
        s = self._spend
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, MAX(event_id) AS last, SUM(micro_usd) AS micro,"
            " SUM(COALESCE(settle_seq, 0)) AS seqs, MAX(updated_at) AS stamp,"
            " SUM(state = ?) AS reserved,"
            " (SELECT COUNT(*) FROM main.spend_fallback_merged) AS merged"
            " FROM main.spend_events",
            (SPEND_RESERVED,),
        ).fetchone()
        fingerprint = json.dumps(list(row), sort_keys=True)
        key = f"synced:{self.db_id}"
        stored = self._conn.execute(f"SELECT value FROM {s}.meta WHERE key = ?", (key,)).fetchone()
        if stored is None and not row["n"] and not row["merged"]:
            return  # Nothing of its own was ever recorded here.
        if stored is not None and stored["value"] == fingerprint:
            return
        self._conn.commit()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                f"INSERT INTO {s}.spend_events (platform, day, run_id, micro_usd, state,"
                " updated_at, settle_seq, mirror_of, db_id, src_event_id)"
                " SELECT platform, day, run_id, micro_usd, state, updated_at, settle_seq,"
                " NULL, ?, event_id FROM main.spend_events WHERE true"
                " ON CONFLICT (db_id, src_event_id) DO UPDATE SET platform = excluded.platform,"
                " day = excluded.day, run_id = excluded.run_id, micro_usd = excluded.micro_usd,"
                " state = excluded.state, updated_at = excluded.updated_at,"
                " settle_seq = excluded.settle_seq",
                (self.db_id,),
            )
            self._conn.execute(
                f"DELETE FROM {s}.spend_events WHERE db_id = ? AND src_event_id IS NOT NULL"
                " AND src_event_id NOT IN (SELECT event_id FROM main.spend_events)",
                (self.db_id,),
            )
            # A settle-day mirror points at its reservation by the own event id;
            # point the copy at the copy of that reservation.
            self._conn.execute(
                f"UPDATE {s}.spend_events SET mirror_of = (SELECT p.event_id"
                f" FROM {s}.spend_events AS p WHERE p.db_id = {s}.spend_events.db_id"
                " AND p.src_event_id = (SELECT m.mirror_of FROM main.spend_events AS m"
                f" WHERE m.event_id = {s}.spend_events.src_event_id))"
                " WHERE db_id = ? AND src_event_id IS NOT NULL",
                (self.db_id,),
            )
            self._conn.execute(
                f"INSERT OR IGNORE INTO {s}.spend_fallback_merged (record_hash, merged_at)"
                " SELECT record_hash, merged_at FROM main.spend_fallback_merged"
            )
            self._conn.execute(
                f"INSERT OR REPLACE INTO {s}.meta (key, value) VALUES (?, ?)", (key, fingerprint)
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        log.info("Copied the spend of %s into the shared spend ledger.", self.path)

    def _open_read_only(self) -> None:
        """Open the existing database read-only, without touching its schema.

        Tries ``mode=ro`` first. A WAL database in a directory the process cannot
        write fails there (SQLite cannot create the ``-shm`` file), so it is then
        opened with ``immutable=1``, which reads the main file without locking.
        """
        base = self.path.resolve().as_uri()
        try:
            self._conn = self._connect_uri(base + "?mode=ro")
        except sqlite3.OperationalError as exc:
            if not self._immutable_fallback_allowed(exc):
                raise
            log.warning(
                "Opening %s with immutable=1 (mode=ro failed: %s). Changes that exist only "
                "in a write-ahead log will not be visible.",
                self.path,
                exc,
            )
            self._conn = self._connect_uri(base + "?immutable=1")
        self.read_only = True
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'db_id'").fetchone()
        except sqlite3.Error:
            row = None
        self.db_id = str(row["value"]) if row is not None else None

    def _immutable_fallback_allowed(self, exc: sqlite3.OperationalError) -> bool:
        """Whether ``immutable=1`` is safe after ``mode=ro`` failed with ``exc``.

        Immutable mode ignores the write-ahead log and any hot rollback journal, so it
        is only used when the error is in the ``SQLITE_CANTOPEN`` family or is exactly
        ``SQLITE_READONLY_DIRECTORY`` (a directory the process cannot write, so the
        ``-shm`` file cannot be created), and both the ``-wal`` and the ``-journal``
        files are missing or empty, which means nothing would be hidden. Any other
        error, such as another ``SQLITE_READONLY`` code (a hot journal it cannot roll
        back, for example), a locked or a corrupt database, is raised.
        """
        code = getattr(exc, "sqlite_errorcode", None)
        if code is None:
            text = str(exc).lower()
            if "unable to open" not in text and "readonly database" not in text:
                return False
        elif code & 0xFF != sqlite3.SQLITE_CANTOPEN and code != SQLITE_READONLY_DIRECTORY:
            return False
        for suffix in ("-wal", "-journal"):
            side = self.path.with_name(self.path.name + suffix)
            try:
                if side.exists() and side.stat().st_size > 0:
                    return False
            except OSError:
                return False
        return True

    @staticmethod
    def _connect_uri(uri: str) -> sqlite3.Connection:
        """Connect to ``uri`` and prove the database can be read."""
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
            raise
        return conn

    def _ensure_db_id(self) -> str:
        """Create ``meta.db_id`` on first open and return it."""
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('db_id', ?)", (uuid.uuid4().hex,)
        )
        self._conn.commit()
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'db_id'").fetchone()
        return str(row["value"])

    def _migrate(self) -> None:
        """Bring a database written by an older version up to the current schema.

        Safe to run from two processes at once: a column the other process added
        first ("duplicate column") is not an error.
        """
        for table, column, ddl in _ADDED_COLUMNS:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        legacy = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'spend_ledger'"
        ).fetchone()
        if legacy is not None:
            self._conn.execute(
                "INSERT INTO spend_events (platform, day, run_id, micro_usd, state, updated_at)"
                " SELECT platform, day, run_id, CAST(ROUND(usd * ?) AS INTEGER), ?, updated_at"
                " FROM spend_ledger WHERE usd > 0",
                (MICRO, SPEND_SETTLED),
            )
            self._conn.execute("DROP TABLE spend_ledger")
        self._conn.commit()

    def rollback(self) -> None:
        """Discard the connection's uncommitted changes. Never raises ``sqlite3.Error``.

        The spend ledger calls this after a failed write, so a statement whose commit
        failed cannot be committed later by an unrelated write and counted twice.
        """
        with contextlib.suppress(sqlite3.Error):
            self._conn.rollback()

    def close(self) -> None:
        """Commit and close the connection, then release the access lock."""
        try:
            self._conn.commit()
            self._conn.close()
        finally:
            self._release_access_lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- accounts ----------------------------------------------------------------

    def upsert_account(self, account: Account, fetched_at: datetime) -> None:
        """Insert or update an account, preserving its activity-cache bookkeeping.

        An opted-out account (status ``opted_out``) is stored by ID only: its handle
        is replaced by the ID, its creation time is cleared, its meta becomes
        ``{"opted_out": true}``, and any activity cached for it is dropped. Every
        run's stored sample snapshot of the account (``run_sample.account_json``) is
        scrubbed the same way, so earlier runs keep no handle or meta either. The
        ``accounts`` row stays so a later run knows the status without a lookup.
        """
        opted_out = account.status == STATUS_OPTED_OUT
        if opted_out:
            self._conn.execute(
                "DELETE FROM activities WHERE platform = ? AND user_id = ?",
                (account.platform, account.user_id),
            )
        self._conn.execute(
            """
            INSERT INTO accounts (platform, user_id, handle, created_at, status, meta_json,
                                  fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (platform, user_id) DO UPDATE SET
                handle = excluded.handle,
                created_at = COALESCE(excluded.created_at, accounts.created_at),
                status = excluded.status,
                meta_json = excluded.meta_json,
                fetched_at = excluded.fetched_at
            """,
            (
                account.platform,
                account.user_id,
                _stored_handle(account),
                _stored_created_at(account),
                account.status,
                json.dumps(_stored_meta(account), sort_keys=True),
                isoformat(fetched_at),
            ),
        )
        if opted_out:
            self._conn.execute(
                "UPDATE accounts SET created_at = NULL, activity_since = NULL,"
                " activity_until = NULL, activity_fetched_at = NULL,"
                " activity_requested_until = NULL WHERE platform = ? AND user_id = ?",
                (account.platform, account.user_id),
            )
            snapshots = self._conn.execute(
                "SELECT run_id, account_json FROM run_sample WHERE platform = ? AND user_id = ?",
                (account.platform, account.user_id),
            ).fetchall()
            self._conn.executemany(
                "UPDATE run_sample SET account_json = ?"
                " WHERE run_id = ? AND platform = ? AND user_id = ?",
                [
                    (
                        _scrubbed_snapshot(row["account_json"]),
                        row["run_id"],
                        account.platform,
                        account.user_id,
                    )
                    for row in snapshots
                ],
            )
        self._conn.commit()

    def get_account(self, platform: str, user_id: str) -> Account | None:
        """Return the cached account, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE platform = ? AND user_id = ?", (platform, user_id)
        ).fetchone()
        if row is None:
            return None
        return Account(
            platform=row["platform"],
            user_id=row["user_id"],
            handle=row["handle"],
            created_at=_dt(row["created_at"]),
            meta=json.loads(row["meta_json"]),
            status=row["status"],
        )

    def account_fetched_at(self, platform: str, user_id: str) -> datetime | None:
        """Return when the account row was last written."""
        row = self._conn.execute(
            "SELECT fetched_at FROM accounts WHERE platform = ? AND user_id = ?",
            (platform, user_id),
        ).fetchone()
        return _dt(row["fetched_at"]) if row else None

    # -- activities --------------------------------------------------------------

    def save_activities(
        self,
        account: Account,
        activities: Iterable[Activity],
        since: datetime,
        until: datetime,
        fetched_at: datetime,
        *,
        covered_until: datetime | None = None,
    ) -> None:
        """Replace the cached activities in ``[since, until)`` and record coverage.

        ``covered_until`` is where the fetched data really ends, when that is
        before ``until`` (a source whose data horizon lags ``until``); it is stored
        as the cached window end, so a later run knows to refetch. The account row
        must already exist (see :meth:`upsert_account`).
        """
        stamp = isoformat(fetched_at)
        window_end = ensure_utc(until)
        if covered_until is not None:
            window_end = min(window_end, ensure_utc(covered_until))
        self._conn.execute(
            "DELETE FROM activities WHERE platform = ? AND user_id = ? AND ts >= ? AND ts < ?",
            (account.platform, account.user_id, isoformat(since), isoformat(until)),
        )
        self._conn.executemany(
            "INSERT INTO activities (platform, user_id, ts, kind, community, content_hash,"
            " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    a.platform,
                    a.user_id,
                    isoformat(a.ts),
                    a.kind,
                    a.community,
                    a.content_hash,
                    stamp,
                )
                for a in activities
            ],
        )
        self._conn.execute(
            "UPDATE accounts SET activity_since = ?, activity_until = ?,"
            " activity_fetched_at = ?, activity_requested_until = ?"
            " WHERE platform = ? AND user_id = ?",
            (
                isoformat(since),
                isoformat(window_end),
                stamp,
                isoformat(until),
                account.platform,
                account.user_id,
            ),
        )
        self._conn.commit()

    def merge_gap_activities(
        self,
        account: Account,
        activities: Iterable[Activity],
        since: datetime,
        gap_until: datetime,
        fetched_at: datetime,
    ) -> None:
        """Merge activities fetched for ``[since, gap_until)`` below a partial window.

        For a cached window that starts after ``since`` (a fetch that stopped
        part-way, see :meth:`save_activities`): replaces the cached activities in
        ``[since, gap_until)`` with ``activities`` and moves the window start down to
        ``since``. The window end, its fetch time and its requested end stay as they
        were, since the newer part of the window was not refetched. The account row
        and its window must already exist.
        """
        stamp = isoformat(fetched_at)
        self._conn.execute(
            "DELETE FROM activities WHERE platform = ? AND user_id = ? AND ts >= ? AND ts < ?",
            (account.platform, account.user_id, isoformat(since), isoformat(gap_until)),
        )
        self._conn.executemany(
            "INSERT INTO activities (platform, user_id, ts, kind, community, content_hash,"
            " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (a.platform, a.user_id, isoformat(a.ts), a.kind, a.community, a.content_hash, stamp)
                for a in activities
            ],
        )
        self._conn.execute(
            "UPDATE accounts SET activity_since = ? WHERE platform = ? AND user_id = ?",
            (isoformat(since), account.platform, account.user_id),
        )
        self._conn.commit()

    def forget_activity(self, platform: str, user_id: str, *, status: str | None = None) -> None:
        """Drop an account's cached activity and its coverage, so it is fetched again.

        With ``status``, the cached account's status is set to it as well.
        """
        self._conn.execute(
            "DELETE FROM activities WHERE platform = ? AND user_id = ?", (platform, user_id)
        )
        self._conn.execute(
            "UPDATE accounts SET activity_since = NULL, activity_until = NULL,"
            " activity_fetched_at = NULL, activity_requested_until = NULL,"
            " status = COALESCE(?, status)"
            " WHERE platform = ? AND user_id = ?",
            (status, platform, user_id),
        )
        self._conn.commit()

    def get_activities(
        self,
        platform: str,
        user_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Activity]:
        """Return cached activities with ``since <= ts < until``, oldest first."""
        query = "SELECT * FROM activities WHERE platform = ? AND user_id = ?"
        args: list[Any] = [platform, user_id]
        if since is not None:
            query += " AND ts >= ?"
            args.append(isoformat(since))
        if until is not None:
            query += " AND ts < ?"
            args.append(isoformat(until))
        rows = self._conn.execute(query + " ORDER BY ts", args).fetchall()
        return [
            Activity(
                platform=r["platform"],
                user_id=r["user_id"],
                ts=parse_datetime(r["ts"]),
                kind=r["kind"],
                community=r["community"],
                content_hash=r["content_hash"],
            )
            for r in rows
        ]

    def activity_window(
        self, platform: str, user_id: str
    ) -> tuple[datetime, datetime, datetime] | None:
        """Return the cached activity window ``(since, until, fetched_at)``, or ``None``."""
        row = self._conn.execute(
            "SELECT activity_since, activity_until, activity_fetched_at FROM accounts"
            " WHERE platform = ? AND user_id = ?",
            (platform, user_id),
        ).fetchone()
        if row is None or None in (
            row["activity_since"],
            row["activity_until"],
            row["activity_fetched_at"],
        ):
            return None
        return (
            parse_datetime(row["activity_since"]),
            parse_datetime(row["activity_until"]),
            parse_datetime(row["activity_fetched_at"]),
        )

    def activity_requested_until(self, platform: str, user_id: str) -> datetime | None:
        """Return the ``until`` the cached window was fetched for, or ``None``.

        Later than the window end (:meth:`activity_window`) when the source's data
        stopped at its horizon before ``until``. ``None`` for windows cached by
        older versions.
        """
        try:
            row = self._conn.execute(
                "SELECT activity_requested_until FROM accounts WHERE platform = ? AND user_id = ?",
                (platform, user_id),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None or row["activity_requested_until"] is None:
            return None
        return parse_datetime(row["activity_requested_until"])

    def activity_cache_covers(self, platform: str, user_id: str, since: datetime) -> bool:
        """Return whether cached activity exists and starts at or before ``since``.

        Ignores age. ``scan --from-run`` uses it for accounts the earlier run already
        fetched.
        """
        window = self.activity_window(platform, user_id)
        return window is not None and window[0] <= ensure_utc(since)

    def activity_cache_fresh(
        self,
        platform: str,
        user_id: str,
        since: datetime,
        now: datetime,
        ttl: timedelta,
        *,
        until: datetime | None = None,
    ) -> bool:
        """Return whether cached activity covers ``since`` and is younger than ``ttl``.

        ``now`` is the wall-clock time the age is measured against. With ``until``
        (the scan's reference time), the cached window must also end no more than
        ``ttl`` before it, so a pinned or later ``--now`` cannot reuse a window that
        stops long before the period being scored.
        """
        window = self.activity_window(platform, user_id)
        if window is None:
            return False
        cached_since, cached_until, fetched_at = window
        covers = cached_since <= ensure_utc(since)
        fresh = ensure_utc(now) - fetched_at <= ttl
        reaches = until is None or ensure_utc(until) - cached_until <= ttl
        return covers and fresh and reaches

    # -- runs --------------------------------------------------------------------

    def create_run(
        self,
        platform: str,
        params: dict[str, Any],
        started_at: datetime,
        *,
        root_run_id: int | None = None,
    ) -> int:
        """Start a run and return its id.

        ``root_run_id`` is the first run of the ``--from-run`` chain this run
        continues; ``None`` makes the new run its own root.
        """
        cur = self._conn.execute(
            "INSERT INTO runs (started_at, platform, params_json, root_run_id) VALUES (?, ?, ?, ?)",
            (isoformat(started_at), platform, json.dumps(params, sort_keys=True), root_run_id),
        )
        assert cur.lastrowid is not None
        if root_run_id is None:
            self._conn.execute(
                "UPDATE runs SET root_run_id = ? WHERE run_id = ?", (cur.lastrowid, cur.lastrowid)
            )
        self._conn.commit()
        return cur.lastrowid

    def update_run_params(self, run_id: int, params: dict[str, Any]) -> None:
        """Replace the stored parameters of ``run_id`` (for example once the scoring
        reference time is known)."""
        self._conn.execute(
            "UPDATE runs SET params_json = ? WHERE run_id = ?",
            (json.dumps(params, sort_keys=True), run_id),
        )
        self._conn.commit()

    def root_run_id(self, run_id: int) -> int:
        """Return the root of ``run_id``'s chain (``run_id`` itself for an unknown run)."""
        row = self._conn.execute(
            "SELECT root_run_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None or row["root_run_id"] is None:
            return run_id
        return int(row["root_run_id"])

    def run_chain(self, root_run_id: int) -> list[int]:
        """Return the ids of every run whose root is ``root_run_id``, oldest first."""
        rows = self._conn.execute(
            "SELECT run_id FROM runs WHERE COALESCE(root_run_id, run_id) = ? ORDER BY run_id",
            (root_run_id,),
        ).fetchall()
        return [int(r["run_id"]) for r in rows]

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        account_ids: Sequence[str],
        summary: dict[str, Any],
        finished_at: datetime,
    ) -> None:
        """Record the outcome of a run (``complete``, ``partial``, ``cancelled`` or ``failed``)."""
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, account_ids_json = ?, summary_json = ?"
            " WHERE run_id = ?",
            (
                isoformat(finished_at),
                status,
                json.dumps(list(account_ids)),
                json.dumps(summary, sort_keys=True),
                run_id,
            ),
        )
        self._conn.commit()

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        """Return a run as a dict with decoded JSON fields, or ``None``."""
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        # ``root_run_id`` is missing from databases an older version wrote and that
        # were opened read-only (so not migrated).
        keys = row.keys()
        root = row["root_run_id"] if "root_run_id" in keys else None
        beat = row["heartbeat_json"] if "heartbeat_json" in keys else None
        try:
            heartbeat = json.loads(beat) if beat else None
        except ValueError:
            heartbeat = None
        return {
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "platform": row["platform"],
            "status": row["status"],
            "params": json.loads(row["params_json"]),
            "account_ids": json.loads(row["account_ids_json"]),
            "summary": json.loads(row["summary_json"]),
            "root_run_id": root if root is not None else row["run_id"],
            #: What the scanning process last recorded about itself (see
            #: :meth:`beat_run`); ``None`` for runs of older versions.
            "heartbeat": heartbeat if isinstance(heartbeat, dict) else None,
        }

    def run_ids(self) -> list[int]:
        """Return the id of every stored run, oldest first."""
        rows = self._conn.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall()
        return [int(r["run_id"]) for r in rows]

    # -- run locks and heartbeats ---------------------------------------------------

    def lock_run(self, run_id: int) -> FileLock | None:
        """Take the lock of the run chain rooted at ``run_id``, or raise :class:`RunInUse`.

        The scanning process holds it for the whole run (see
        :func:`bot_farm_silence_tracker.pipeline.run_scan`), so a second process
        cannot continue the same chain at the same time. It is an operating-system
        file lock (:func:`run_lock_path`), released when the process ends, so a run
        left ``running`` by a crash can be continued at once. Returns ``None``
        (unlocked, with a warning) when the lock file cannot be created, and for an
        in-memory database.
        """
        if str(self.path) == ":memory:":
            return None
        try:
            held = acquire_file_lock(run_lock_path(self.path, run_id), shared=False)
        except OSError as exc:
            log.warning("Could not lock run %s (%s); continuing without the lock.", run_id, exc)
            return None
        if held is None:
            raise RunInUse(run_id, self.run_heartbeat(run_id))
        return held

    def run_in_use(self, run_id: int) -> bool:
        """Whether a scan holds the lock of the run chain rooted at ``run_id`` now."""
        if str(self.path) == ":memory:":
            return False
        try:
            held = acquire_file_lock(run_lock_path(self.path, run_id), shared=False)
        except OSError:
            return False
        if held is None:
            return True
        held.release()
        return False

    def beat_run(self, run_id: int, *, now: datetime | None = None) -> None:
        """Record that this process is scanning ``run_id`` (its heartbeat).

        Stored as ``heartbeat_json`` (``pid``, ``host`` and ``last_seen``). It only
        informs: the run lock (:meth:`lock_run`) decides whether a run is in use.
        """
        stamp = ensure_utc(now) if now is not None else utcnow()
        self._conn.execute(
            "UPDATE runs SET heartbeat_json = ? WHERE run_id = ?",
            (json.dumps(heartbeat_record(stamp), sort_keys=True), run_id),
        )
        self._conn.commit()

    def run_heartbeat(self, run_id: int) -> dict[str, Any] | None:
        """The heartbeat recorded for ``run_id`` or, when it has none, for the newest
        run of its chain; ``None`` when none was recorded (or the column is missing)."""
        rows = self._read_optional(
            "SELECT heartbeat_json FROM runs WHERE heartbeat_json IS NOT NULL"
            " AND (run_id = ? OR COALESCE(root_run_id, run_id) = ?)"
            " ORDER BY run_id = ? DESC, run_id DESC LIMIT 1",
            (run_id, run_id, run_id),
        )
        if not rows:
            return None
        try:
            data = json.loads(rows[0]["heartbeat_json"])
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # -- run samples ---------------------------------------------------------------

    def save_run_sample(self, run_id: int, accounts: Sequence[Account]) -> None:
        """Store ``accounts`` as the sample of ``run_id``, in order, all ``pending``."""
        self._conn.executemany(
            "INSERT OR REPLACE INTO run_sample (run_id, position, platform, user_id,"
            " account_json, fetch_status) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, pos, a.platform, a.user_id, _account_to_json(a), SAMPLE_PENDING)
                for pos, a in enumerate(accounts)
            ],
        )
        self._conn.commit()

    def set_sample_status(self, run_id: int, account: Account, status: str) -> None:
        """Set the fetch status of one sampled account (``fetched``, ``skipped`` or ``error``).

        Also refreshes the stored snapshot from ``account`` (its status, for example
        ``error`` or ``suspended``, and its ``meta``) and records
        ``account.meta["truncated"]`` for this run.
        """
        truncated = account.meta.get("truncated")
        self._conn.execute(
            "UPDATE run_sample SET fetch_status = ?, account_json = ?, truncated = ?"
            " WHERE run_id = ? AND platform = ? AND user_id = ?",
            (
                status,
                _account_to_json(account),
                None if truncated is None else int(bool(truncated)),
                run_id,
                account.platform,
                account.user_id,
            ),
        )
        self._conn.commit()

    def get_run_truncated(self, run_id: int) -> dict[str, bool]:
        """Return ``{user_id: truncated}`` for the sampled accounts of ``run_id`` that
        reported it. Accounts that did not report it are left out."""
        rows = self._read_optional(
            "SELECT user_id, truncated FROM run_sample WHERE run_id = ? AND truncated IS NOT NULL",
            (run_id,),
        )
        return {r["user_id"]: bool(r["truncated"]) for r in rows}

    def set_sample_window_ends(
        self, run_id: int, ends: Iterable[tuple[str, str, datetime]]
    ) -> None:
        """Record, per sampled account, where the data it was scored against ends.

        ``ends`` holds ``(platform, user_id, window_end)``. ``bot-farm-silence-tracker report``
        reads them back with :meth:`get_run_window_ends`, so a re-score measures
        silence to the same per-account ends as the scan did.
        """
        self._conn.executemany(
            "UPDATE run_sample SET window_end = ?"
            " WHERE run_id = ? AND platform = ? AND user_id = ?",
            [(isoformat(end), run_id, platform, user_id) for platform, user_id, end in ends],
        )
        self._conn.commit()

    def get_run_window_ends(self, run_id: int) -> dict[str, datetime]:
        """Return ``{user_id: window_end}`` stored by :meth:`set_sample_window_ends`.

        Empty for runs made before window ends were stored.
        """
        rows = self._read_optional(
            "SELECT user_id, window_end FROM run_sample"
            " WHERE run_id = ? AND window_end IS NOT NULL",
            (run_id,),
        )
        return {r["user_id"]: parse_datetime(r["window_end"]) for r in rows}

    def set_sample_design(
        self, run_id: int, account: Account, stratum: str, inclusion_prob: float, phase2: bool
    ) -> None:
        """Record an account's two-phase design: stratum, inclusion probability and
        whether it was selected for the phase-2 fetch (see
        :mod:`bot_farm_silence_tracker.early_exit`)."""
        self._conn.execute(
            "UPDATE run_sample SET stratum = ?, inclusion_prob = ?, phase2 = ?"
            " WHERE run_id = ? AND platform = ? AND user_id = ?",
            (stratum, inclusion_prob, int(phase2), run_id, account.platform, account.user_id),
        )
        self._conn.commit()

    def get_run_design(self, run_id: int) -> dict[str, tuple[str, float, bool, str]]:
        """Return ``{user_id: (stratum, inclusion_prob, phase2, fetch_status)}`` for the
        accounts of ``run_id`` that have a two-phase design. Empty for census runs."""
        rows = self._read_optional(
            "SELECT user_id, stratum, inclusion_prob, phase2, fetch_status FROM run_sample"
            " WHERE run_id = ? AND stratum IS NOT NULL",
            (run_id,),
        )
        return {
            r["user_id"]: (
                str(r["stratum"]),
                float(r["inclusion_prob"]),
                bool(r["phase2"]),
                str(r["fetch_status"]),
            )
            for r in rows
        }

    def _read_optional(self, sql: str, args: Sequence[Any]) -> list[sqlite3.Row]:
        """Run a read that may hit a table or column an older version did not create.

        A database opened read-only is never migrated, so such a read gives ``[]``
        there instead of raising. On a writable database errors are raised.
        """
        try:
            return self._conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if self.read_only and ("no such table" in text or "no such column" in text):
                return []
            raise

    def get_run_sample(self, run_id: int) -> list[tuple[Account, str]]:
        """Return ``(account, fetch_status)`` for the run's sample, in sample order.

        Empty for runs made before samples were stored, or whose sampling failed.
        """
        rows = self._read_optional(
            "SELECT account_json, fetch_status FROM run_sample WHERE run_id = ? ORDER BY position",
            (run_id,),
        )
        return [(_account_from_json(r["account_json"]), r["fetch_status"]) for r in rows]

    # -- request log -----------------------------------------------------------------

    _REQUEST_LOG_KEYS = (
        "at",
        "phase",
        "endpoint",
        "status",
        "latency_ms",
        "page_limit",
        "window_s",
        "rps",
        "slow_kind",
        "attempt",
        "wait_s",
        "reset_s",
        "retry_after_s",
        "send_seq",
    )

    def save_request_log(self, run_id: int, platform: str, rows: Sequence[dict[str, Any]]) -> int:
        """Append per-request instrumentation rows to ``run_id``; returns how many.

        Rows are numbered after the run's existing ones, so several saves of one run
        keep their order. Unknown keys are ignored and missing ones stored as NULL.
        """
        if not rows:
            return 0
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS last FROM request_log WHERE run_id = ?", (run_id,)
        ).fetchone()
        first = int(row["last"]) + 1
        self._conn.executemany(
            "INSERT INTO request_log (run_id, seq, platform, at, phase, endpoint, status,"
            " latency_ms, page_limit, window_s, rps, slow_kind, attempt, wait_s, reset_s,"
            " retry_after_s, send_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    first + i,
                    platform,
                    str(r.get("at") or isoformat(utcnow())),
                    *(r.get(key) for key in self._REQUEST_LOG_KEYS[1:]),
                )
                for i, r in enumerate(rows)
            ],
        )
        self._conn.commit()
        return len(rows)

    def get_request_log(self, run_id: int) -> list[dict[str, Any]]:
        """Return the run's request rows in order (empty for runs without any)."""
        rows = self._read_optional(
            "SELECT * FROM request_log WHERE run_id = ? ORDER BY seq", (run_id,)
        )
        return [dict(r) for r in rows]

    # -- spend ledger ----------------------------------------------------------------

    def add_spend(
        self,
        platform: str,
        usd: float,
        *,
        run_id: int | None = None,
        when: datetime | None = None,
    ) -> None:
        """Record ``usd`` for ``platform``, the UTC day of ``when``, and run.

        ``when`` defaults to the current wall-clock time. ``run_id`` ``None`` is filed
        under run 0. Stored as integer micro-USD.
        """
        self.add_spend_micro(platform, to_micro(usd), run_id=run_id, when=when)

    def add_spend_micro(
        self,
        platform: str,
        micro_usd: int,
        *,
        run_id: int | None = None,
        when: datetime | None = None,
    ) -> None:
        """Like :meth:`add_spend`, with the amount in integer micro-USD."""
        stamp = ensure_utc(when) if when is not None else utcnow()
        self._execute_spend_write(
            f"INSERT INTO {self._spend}.spend_events (platform, day, run_id, micro_usd, state,"
            " updated_at, db_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                platform,
                _utc_day(stamp),
                run_id or 0,
                int(micro_usd),
                SPEND_SETTLED,
                isoformat(stamp),
                self.db_id,
            ),
        )

    def _micro_on(self, platform: str, day: str) -> int:
        """Micro-USD on ``day`` for ``platform``, of every database sharing the ledger."""
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(micro_usd), 0) AS total FROM {self._spend}.spend_events"
            " WHERE platform = ? AND day = ?",
            (platform, day),
        ).fetchone()
        return int(row["total"])

    def _micro_for_runs(self, run_ids: Sequence[int], platform: str | None) -> int:
        if not run_ids:
            return 0
        marks = ", ".join("?" for _ in run_ids)
        # Run ids are numbered per database: only this database's rows are its runs'.
        query = (
            f"SELECT COALESCE(SUM(micro_usd), 0) AS total FROM {self._spend}.spend_events"
            f" WHERE COALESCE(db_id, ?) = ? AND run_id IN ({marks}) AND mirror_of IS NULL"
        )
        args: list[Any] = [self.db_id, self.db_id, *run_ids]
        if platform is not None:
            query += " AND platform = ?"
            args.append(platform)
        return int(self._conn.execute(query, args).fetchone()["total"])

    def spend_on(self, platform: str, day: str) -> float:
        """USD recorded for ``platform`` on UTC day ``day`` (``YYYY-MM-DD``), all runs."""
        return from_micro(self._micro_on(platform, day))

    def spend_today(self, platform: str, *, now: datetime | None = None) -> float:
        """USD recorded for ``platform`` on the current UTC day (or the day of ``now``)."""
        return self.spend_on(platform, _utc_day(now if now is not None else utcnow()))

    def spend_for_run(self, run_id: int, platform: str | None = None) -> float:
        """USD recorded for ``run_id`` across all days (optionally one platform)."""
        return from_micro(self._micro_for_runs([run_id], platform))

    def spend_for_chain(self, root_run_id: int, platform: str | None = None) -> float:
        """USD recorded for every run of the ``--from-run`` chain rooted at ``root_run_id``."""
        chain = self.run_chain(root_run_id) or [root_run_id]
        return from_micro(self._micro_for_runs(chain, platform))

    def reserve_spend(
        self,
        platform: str,
        micro_usd: int,
        cap_micro_usd: int,
        *,
        scope: str,
        run_id: int | None,
        root_run_id: int | None,
        when: datetime | None = None,
    ) -> tuple[int | None, int]:
        """Atomically check a cap and, if it holds, record a reservation.

        Runs in one ``BEGIN IMMEDIATE`` transaction, so no other connection can
        record spend between the check and the insert. ``scope`` is ``"day"``
        (``platform`` on the UTC day of ``when``) or ``"run"`` (``platform`` across
        the chain rooted at ``root_run_id``). Returns ``(event_id, spent_before)``;
        ``event_id`` is ``None`` when ``spent_before + micro_usd`` would exceed
        ``cap_micro_usd``, in which case nothing is written.
        """
        stamp = ensure_utc(when) if when is not None else utcnow()
        day = _utc_day(stamp)
        self._conn.commit()  # End any implicit transaction before taking the write lock.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if scope == "day":
                spent = self._micro_on(platform, day)
            elif scope == "run":
                root = root_run_id if root_run_id is not None else run_id
                chain = (self.run_chain(root) or [root]) if root is not None else [0]
                spent = self._micro_for_runs(chain, platform)
            else:
                raise ValueError(f"unknown spend scope {scope!r}")
            if spent + int(micro_usd) > int(cap_micro_usd):
                self._conn.rollback()
                return None, spent
            cur = self._conn.execute(
                f"INSERT INTO {self._spend}.spend_events (platform, day, run_id, micro_usd,"
                " state, updated_at, db_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    platform,
                    day,
                    run_id or 0,
                    int(micro_usd),
                    SPEND_RESERVED,
                    isoformat(stamp),
                    self.db_id,
                ),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        assert cur.lastrowid is not None
        return cur.lastrowid, spent

    def _execute_spend_write(self, sql: str, args: Sequence[Any]) -> None:
        """Execute one spend write and commit it; roll it back if either step fails."""
        try:
            self._conn.execute(sql, args)
            self._conn.commit()
        except BaseException:
            self.rollback()
            raise

    def settle_spend(
        self,
        event_id: int,
        micro_usd: int,
        *,
        when: datetime | None = None,
        seq: int | None = None,
    ) -> None:
        """Replace a reservation's amount with the actual ``micro_usd`` and mark it settled.

        The amount is absolute, so settling again overwrites rather than adds. When
        the settle falls on a later UTC day than the reservation (a request reserved
        just before midnight), the row keeps its reserve day and a mirror row
        (``mirror_of = event_id``) files the same amount under the settle day, so
        both days' caps count it; run and chain totals skip mirror rows.
        ``seq`` numbers the settle calls of one reservation; the ledger compares it
        with the fallback file to pick the latest settle. With ``seq``, the update
        only applies when the stored ``settle_seq`` is lower, so an older settle
        never overwrites a newer one. A failed write is rolled back.
        """
        stamp = ensure_utc(when) if when is not None else utcnow()
        try:
            self._apply_settle(event_id, int(micro_usd), stamp, seq, _utc_day(stamp))
            self._conn.commit()
        except BaseException:
            self.rollback()
            raise

    def _apply_settle(
        self, event_id: int, micro_usd: int, stamp: datetime, seq: int | None, settle_day: str
    ) -> bool:
        """Apply one settle without committing; return whether the row changed.

        Shared by :meth:`settle_spend` and :meth:`reconcile_fallback`, so a settle
        merged from the fallback file gets the same settle-day mirror row as one
        written directly.
        """
        sql = (
            f"UPDATE {self._spend}.spend_events SET micro_usd = ?, state = ?, updated_at = ?,"
            " settle_seq = ? WHERE event_id = ?"
        )
        args: list[Any] = [int(micro_usd), SPEND_SETTLED, isoformat(stamp), seq, event_id]
        if seq is not None:
            sql += " AND COALESCE(settle_seq, 0) < ?"
            args.append(int(seq))
        cur = self._conn.execute(sql, args)
        if not cur.rowcount:
            return False
        row = self._conn.execute(
            f"SELECT platform, day, run_id, db_id FROM {self._spend}.spend_events"
            " WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        self._conn.execute(
            f"DELETE FROM {self._spend}.spend_events WHERE mirror_of = ?", (event_id,)
        )
        if row is not None and row["day"] != settle_day and int(micro_usd) > 0:
            # Settled on a later UTC day than it was reserved: count it on both
            # days, so neither day's cap misses it. The mirror row is left out
            # of run and chain totals, which therefore count it once.
            self._conn.execute(
                f"INSERT INTO {self._spend}.spend_events (platform, day, run_id, micro_usd,"
                " state, updated_at, settle_seq, mirror_of, db_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["platform"],
                    settle_day,
                    row["run_id"],
                    int(micro_usd),
                    SPEND_SETTLED,
                    isoformat(stamp),
                    seq,
                    event_id,
                    row["db_id"],
                ),
            )
        return True

    def fallback_event_key(self, rec: dict[str, Any]) -> int | tuple[str, int]:
        """The spend event a fallback ``settle`` record names, in the active ledger.

        A record written while the shared ledger was in use (``ledger`` is
        :data:`SHARED_LEDGER`), and every record of a database that keeps its own
        spend, names the row by its id there. An older record of a database that now
        uses the shared ledger names a row of its own ``spend_events`` table: it is
        matched to that row's copy. When there is no copy yet, ``("unmatched", id)``
        is returned, which matches no row (the record then never lowers a total).
        Raises ``KeyError``, ``TypeError`` or ``ValueError`` for a record without a
        readable ``event_id``.
        """
        event_id = int(rec["event_id"])
        if not self.spend_shared or rec.get("ledger") == SHARED_LEDGER:
            return event_id
        row = self._conn.execute(
            f"SELECT event_id FROM {self._spend}.spend_events WHERE db_id = ? AND src_event_id = ?",
            (self.db_id, event_id),
        ).fetchone()
        return int(row["event_id"]) if row is not None else ("unmatched", event_id)

    def fallback_record_tags(self) -> dict[str, str]:
        """Fields the ledger adds to every fallback record it writes for this database."""
        return {"ledger": SHARED_LEDGER} if self.spend_shared else {}

    def spend_events(self, event_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """Return ``{event_id: row}`` for the given spend events that exist.

        Each row holds ``platform``, ``day``, ``run_id``, ``micro_usd``, ``state``
        and ``settle_seq``.
        """
        ids = sorted({int(i) for i in event_ids})
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = self._conn.execute(
            "SELECT event_id, platform, day, run_id, micro_usd, state, settle_seq"
            f" FROM {self._spend}.spend_events WHERE event_id IN ({marks})",
            ids,
        ).fetchall()
        return {int(r["event_id"]): dict(r) for r in rows}

    def spend_mirrors(self, event_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
        """Return ``{event_id: [mirror rows]}`` for the settle-day mirrors of events.

        Each mirror row holds ``platform``, ``day``, ``run_id`` and ``micro_usd``
        (see :meth:`settle_spend`). Events without mirrors are left out.
        """
        ids = sorted({int(i) for i in event_ids})
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = self._read_optional(
            "SELECT mirror_of, platform, day, run_id, micro_usd"
            f" FROM {self._spend}.spend_events WHERE mirror_of IN ({marks})",
            ids,
        )
        out: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(int(r["mirror_of"]), []).append(dict(r))
        return out

    # -- spend housekeeping ------------------------------------------------------------

    @property
    def spend_fallback_path(self) -> Path:
        """The ledger's JSON-lines fallback file for this database.

        ``<db>.spend-fallback.jsonl`` next to a file database. An in-memory
        database gets a path in the system temp directory named after its random
        :attr:`db_id`, so it never shares a file with another database.
        """
        if str(self.path) == ":memory:":
            name = (
                f"bot-farm-silence-tracker-memory-{self.db_id or uuid.uuid4().hex}{FALLBACK_SUFFIX}"
            )
            return Path(tempfile.gettempdir()) / name
        return self.path.with_name(self.path.name + FALLBACK_SUFFIX)

    def merged_fallback_hashes(self, candidates: Iterable[str] | None = None) -> set[str]:
        """Hashes of fallback records already merged by :meth:`reconcile_fallback`.

        With ``candidates``, only those of them that are present in the table are
        returned, so the query does not read the whole table.
        """
        try:
            if candidates is None:
                rows = self._conn.execute(
                    f"SELECT record_hash FROM {self._spend}.spend_fallback_merged"
                )
                return {str(r["record_hash"]) for r in rows.fetchall()}
            wanted = sorted(set(candidates))
            found: set[str] = set()
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                marks = ", ".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT record_hash FROM {self._spend}.spend_fallback_merged"
                    f" WHERE record_hash IN ({marks})",
                    chunk,
                )
                found.update(str(r["record_hash"]) for r in rows.fetchall())
            return found
        except sqlite3.Error:
            return set()

    def reclaim_stale_reservations(
        self, *, older_than: timedelta = STALE_RESERVATION_AGE, now: datetime | None = None
    ) -> int:
        """Close ``reserved`` spend rows left behind by runs that have finished.

        A reservation whose settle never arrived (the process was killed between
        the request and the settle) stays ``reserved`` forever. Rows older than
        ``older_than`` whose run has a ``finished_at`` are marked ``settled`` at
        their reserved amount. That is the conservative choice: the request may
        have been billed, so the worst case stays counted; only ``state`` and
        ``updated_at`` change, so no total moves. ``settle_seq`` is left alone, so
        a settle that arrives afterwards, written directly by a still-running
        process or merged from the fallback file, replaces the reserved amount
        with the settled one as usual. Returns the number of rows reclaimed; 0 on
        a read-only database.
        """
        if self.read_only:
            return 0
        stamp = ensure_utc(now) if now is not None else utcnow()
        cur = self._conn.execute(
            f"UPDATE {self._spend}.spend_events SET state = ?, updated_at = ?"
            " WHERE state = ? AND updated_at < ? AND COALESCE(db_id, ?) = ? AND run_id IN"
            " (SELECT run_id FROM main.runs WHERE finished_at IS NOT NULL)",
            (
                SPEND_SETTLED,
                isoformat(stamp),
                SPEND_RESERVED,
                isoformat(stamp - older_than),
                self.db_id,
                self.db_id,
            ),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def reconcile_fallback(self, *, now: datetime | None = None) -> int:
        """Merge this database's fallback-file records into ``spend_events``.

        Called at run start. First, under :func:`fallback_lock`, the active file is
        renamed to ``<file>.merged-<timestamp>-<nonce>``, so a record appended
        while the merge runs lands in a fresh active file instead of in a file that
        was already read. Then every live archive (this one and any an earlier
        reconcile could not finish) is read, and the records carrying this
        database's :attr:`db_id` are merged in one transaction: an ``add`` becomes a
        settled event, and the latest ``settle`` of each event is applied like
        :meth:`settle_spend` (only when newer than the row's ``settle_seq``, with a
        mirror row on the settle day when that is a later UTC day than the
        reservation; when the row is gone, it becomes a settled event of the settled
        amount on the settle day). Each merged record's hash is stored in
        ``spend_fallback_merged``, so merging is idempotent and nothing is counted
        twice.

        Records without a ``db_id`` (written by older versions) cannot be matched
        safely; they are appended to the active file again (still under the lock),
        unless older than :data:`LEGACY_FALLBACK_MAX_AGE` and not of a chain that may
        still be resumed (:func:`legacy_record_dropped`), which are dropped with a
        warning. Until that append, the ledger still counts them in the archive.
        Records of other databases are left in the archive. An archive whose records
        are all handled is renamed with :data:`FALLBACK_DONE_SUFFIX` and kept for
        audit. Returns the number of records merged. Does nothing on a read-only
        database.
        """
        if self.read_only or self.db_id is None:
            return 0
        path = self.spend_fallback_path
        if not path.exists() and not fallback_archives(path):
            return 0
        stamp = ensure_utc(now) if now is not None else utcnow()
        with fallback_lock(path):
            try:
                rotate = path.exists() and path.stat().st_size > 0
            except OSError:
                rotate = False
            keep_runs = self._resumable_chain_runs()
            if rotate and _only_carried_legacy(path, stamp, keep_runs):
                # Rotating a file that holds only legacy lines carried forward from
                # an archive would copy them into a new archive on every run.
                rotate = False
            if rotate:
                name = f"{path.name}{FALLBACK_ARCHIVE_INFIX}"
                name += f"{stamp.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
                try:
                    _retry_on_sharing_violation(path.rename, path.with_name(name))
                except OSError as exc:
                    log.warning("Could not rotate the spend fallback file %s: %s", path, exc)
            _FALLBACK_CACHE.pop(str(path), None)
        _prune_done_archives(path, stamp)
        archives = fallback_archives(path)
        if not archives:
            return 0
        per_file = [(archive, load_spend_fallback(archive)) for archive in archives]
        mine = [(h, r) for _, recs in per_file for h, r in recs if r.get("db_id") == self.db_id]
        merged = self._merge_fallback_records(mine, stamp) if mine else 0
        carried = self._carry_legacy_forward(path, per_file, stamp, keep_runs)
        if carried:
            # A known, accepted race remains for legacy lines: a ledger read in another
            # process can read the active file before the carry-forward append above
            # and list the archives after the rename below, so that one read misses the
            # carried lines and undercounts. The next read, which every new reservation
            # does, sees them in the active file and corrects the total. Holding the
            # fallback lock across both steps would make every ledger read take it.
            for archive, _ in per_file:
                done = archive.with_name(archive.name + FALLBACK_DONE_SUFFIX)
                try:
                    _retry_on_sharing_violation(archive.rename, done)
                    # The rename keeps the archive's old mtime; reset it so the prune
                    # age of a .done archive counts from this merge, not from its rotation.
                    os.utime(done, (stamp.timestamp(), stamp.timestamp()))
                except OSError:
                    pass
                _FALLBACK_CACHE.pop(str(archive), None)
        if merged:
            log.info("Merged %d spend records from %s into the database.", merged, path)
        return merged

    def _merge_fallback_records(
        self, records: Sequence[tuple[str, dict[str, Any]]], stamp: datetime
    ) -> int:
        """Merge this database's fallback records in one transaction; return the count."""
        merged = 0
        self._conn.commit()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            done = {
                str(r["record_hash"])
                for r in self._conn.execute(
                    f"SELECT record_hash FROM {self._spend}.spend_fallback_merged"
                )
            }
            latest: dict[int | tuple[str, int], tuple[str, dict[str, Any]]] = {}
            for digest, rec in records:
                if digest in done:
                    continue
                if rec.get("what") == "settle" and "settled_micro_usd" in rec:
                    try:
                        int(rec["event_id"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    key = self.fallback_event_key(rec)
                    prev = latest.get(key)
                    if prev is None or int(rec.get("seq") or 0) >= int(prev[1].get("seq") or 0):
                        latest[key] = (digest, rec)
                elif rec.get("what") == "add":
                    micro = int(rec.get("micro_usd") or 0)
                    if micro > 0:
                        self._insert_settled(rec, micro, stamp)
                else:
                    continue
                done.add(digest)
                self._mark_merged(digest, stamp)
                merged += 1
            for event_id, (_, rec) in latest.items():
                settled = max(0, int(rec.get("settled_micro_usd") or 0))
                seq = int(rec.get("seq") or 0)
                settle_day = str(rec.get("settle_day") or rec.get("day") or _utc_day(stamp))
                settled_at = _record_time(rec, "settled_at") or _record_time(rec, "when") or stamp
                row = (
                    self._conn.execute(
                        f"SELECT event_id FROM {self._spend}.spend_events WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if isinstance(event_id, int)
                    else None
                )
                if row is None or not isinstance(event_id, int):
                    if settled > 0:
                        self._insert_settled({**rec, "day": settle_day}, settled, stamp)
                else:
                    self._apply_settle(event_id, settled, settled_at, seq, settle_day)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return merged

    def _resumable_chain_runs(self) -> frozenset[int]:
        """The ids of every run in a chain that may still be resumed.

        A chain may still be resumed while one of its runs is ``partial``,
        ``failed`` or ``running`` (a process that died mid-run leaves its row
        ``running``). Returns an empty set when the runs table cannot be read.
        """
        try:
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE COALESCE(root_run_id, run_id) IN ("
                " SELECT COALESCE(root_run_id, run_id) FROM runs"
                " WHERE status IN ('partial', 'failed', 'cancelled', 'running'))"
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("Could not list the resumable runs: %s", exc)
            return frozenset()
        return frozenset(int(r["run_id"]) for r in rows)

    def _carry_legacy_forward(
        self,
        path: Path,
        per_file: Sequence[tuple[Path, list[tuple[str, dict[str, Any]]]]],
        stamp: datetime,
        keep_runs: frozenset[int] = frozenset(),
    ) -> bool:
        """Append the archives' kept legacy records to the active file.

        A legacy record is kept unless :func:`legacy_record_dropped` (expired, and
        not of a chain in ``keep_runs``). Returns whether every archive is fully
        handled (so it can be marked done).
        Lines are copied verbatim, so a copy has the same hash as its original and
        the ledger never counts both.
        """
        legacy: dict[str, str] = {}
        expired = 0
        for archive, recs in per_file:
            wanted = set()
            for digest, rec in recs:
                if rec.get("db_id") is not None:
                    continue
                if legacy_record_dropped(rec, stamp, keep_runs):
                    expired += 1
                    continue
                wanted.add(digest)
            if not wanted:
                continue
            try:
                lines = archive.read_text(encoding="utf-8").splitlines()
            except OSError:
                return False
            for line in lines:
                if line.strip() and fallback_record_hash(line) in wanted:
                    legacy.setdefault(fallback_record_hash(line), line.strip())
        if expired:
            log.warning(
                "Dropped %d spend fallback records without a database id that are older "
                "than %d days; they cannot be matched to this database.",
                expired,
                LEGACY_FALLBACK_MAX_AGE.days,
            )
        if not legacy:
            return True
        with fallback_lock(path):
            present = {h for h, _ in load_spend_fallback(path)}
            try:
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    for digest, line in legacy.items():
                        if digest not in present:
                            fh.write(line + "\n")
            except OSError as exc:
                log.warning("Could not carry legacy spend records into %s: %s", path, exc)
                return False
            finally:
                _FALLBACK_CACHE.pop(str(path), None)
        return True

    def _insert_settled(self, rec: dict[str, Any], micro: int, stamp: datetime) -> None:
        day = rec.get("day") or _utc_day(stamp)
        try:
            run_id = int(rec.get("run_id") or 0)
        except (TypeError, ValueError):
            run_id = 0
        self._conn.execute(
            f"INSERT INTO {self._spend}.spend_events (platform, day, run_id, micro_usd, state,"
            " updated_at, db_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(rec.get("platform") or ""),
                str(day),
                run_id,
                int(micro),
                SPEND_SETTLED,
                isoformat(stamp),
                self.db_id,
            ),
        )

    def _mark_merged(self, digest: str, stamp: datetime) -> None:
        self._conn.execute(
            f"INSERT OR IGNORE INTO {self._spend}.spend_fallback_merged (record_hash, merged_at)"
            " VALUES (?, ?)",
            (digest, isoformat(stamp)),
        )
