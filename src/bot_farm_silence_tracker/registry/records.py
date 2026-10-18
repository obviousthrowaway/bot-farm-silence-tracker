"""Registry records: format, JSON schema, validation, sharding and file layout.

Layout
------
Each outage event (see :mod:`bot_farm_silence_tracker.events`) has its own folder,
``<registry>/events/<event-id>/``. ``<event folder>/<platform>/<shard>.jsonl`` holds
the event's records of one platform whose shard is ``<shard>``: the first two hex
digits of ``sha256(id_b64)``. A hash spreads records evenly over 256 files per
platform (a prefix of ``id_b64`` would not: every Bluesky DID encodes to the prefix
``ZGlk``), and lower-case hex never makes two names that differ only in case, which
macOS and Windows file systems would merge. Each line is one record, serialized with
sorted keys and no spaces; lines are sorted by ``id_b64``. Evidence files live under
``<event folder>/evidence/``, and a record's ``evidence`` path is relative to its
event folder. The JSON schema is ``<registry>/schema.json`` (the same as
:data:`SCHEMA`), shared by every event.

Every record carries its ``event_id``, which must be its folder's name and an event
of ``events.toml``. The same account may have one record in each event: the key of a
record is ``(event_id, platform, id_b64)``.

Identity encoding
-----------------
``id_b64`` and ``username_b64`` are URL-safe base64 without padding of the stored
platform ID (a Reddit author name as the archive stores it, a Bluesky DID, an X user
ID) and of the handle. This only keeps names out of casual search: anyone can
decode them. It is not anonymity.

Identity binding
----------------
The spot-check probes an account by its ID, and report drafts name it by its ID, so
a record cannot claim one account's ID with another account's name:

* ``reddit-archive``: the archive's ID is the author name, so ``id`` must equal
  ``username``.
* ``reddit``: the ID is the lower-cased name, so ``id`` must equal
  ``username.lower()``.
* ``bluesky``: the ID must be a DID. The handle can change; drafts name the account
  by its DID and show the handle only as seen at the scan.
* ``x``: the ID must be a numeric user ID.

Usernames must have their platform's syntax (:func:`username_errors`): a Bluesky
handle is a lower-case atproto domain name of at most 253 characters, a Reddit name
3 to 20 of ``A-Z a-z 0-9 _ -``, and an X username 1 to 15 of ``A-Z a-z 0-9 _``. So
a name can never carry Markdown, a link or a line break into a summary or a draft.

Time rules
----------
No timestamp may lie more than :data:`FUTURE_SLACK` after now: a future
``status_updated_at`` would win every merge. The scanned data may end at most
:data:`MAX_DATA_GAP` before the scan (``last_scanned_at - scan_window_end``), and a
cliff or ``candidate`` record needs at least :data:`MIN_SILENCE` between its last
activity and its data end, so a record cannot shrink its window to hide activity.
``cleared`` and ``verified_inauthentic`` records are exempt from the silence rule: a
rescan keeps a human status even when the account posted again. A pull request that
sets ``verified_inauthentic`` is held to it by the gate.

Statuses
--------
* ``scanned``: scanned, no reason for review.
* ``candidate``: a cliff with at least one signal (see
  :func:`bot_farm_silence_tracker.registry.build.fired_signals`); needs human review.
* ``verified_inauthentic``: reviewers agreed the account is inauthentic. Needs an
  evidence file. Set only by hand, in a pull request.
* ``cleared``: reviewers decided it is not. Set only by hand, in a pull request.

``registry add`` moves an account between ``scanned`` and ``candidate`` only; it
never sets or changes ``verified_inauthentic`` or ``cleared``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.events import (
    EVENT_ID_PATTERN,
    EVENTS_FILE,
    MAX_EVENTS_FILE_BYTES,
    EventCatalog,
    events_path_for,
    load_events,
    parse_events,
)
from bot_farm_silence_tracker.timeutil import ensure_utc, parse_datetime, utcnow

STATUS_SCANNED = "scanned"
STATUS_CANDIDATE = "candidate"
STATUS_VERIFIED = "verified_inauthentic"
STATUS_CLEARED = "cleared"
STATUSES = (STATUS_SCANNED, STATUS_CANDIDATE, STATUS_VERIFIED, STATUS_CLEARED)
#: Statuses only people set (in a reviewed pull request). Automation never changes them.
HUMAN_STATUSES = frozenset({STATUS_VERIFIED, STATUS_CLEARED})
#: Statuses ``registry add`` may set and change.
AUTOMATIC_STATUSES = frozenset({STATUS_SCANNED, STATUS_CANDIDATE})

#: Platforms a record may name: the real scan platforms. ``fake`` (synthetic test
#: data) is never accepted.
PLATFORMS = ("bluesky", "reddit", "reddit-archive", "x")

#: How far after now a timestamp may lie (clock skew between contributors).
FUTURE_SLACK = timedelta(hours=1)
#: Most a record's data end may lie before its scan time.
MAX_DATA_GAP = timedelta(days=3)
#: Least silence (data end minus last activity) of a cliff or ``candidate`` record when
#: no event is known; each event's ``min_silence_days`` replaces it.
MIN_SILENCE = timedelta(days=3)
#: Least silence of a record that becomes ``verified_inauthentic`` when no event is
#: known; each event's ``verified_min_silence_days`` replaces it.
VERIFIED_MIN_SILENCE = timedelta(days=21)
_TIME_FIELDS = (
    "first_scanned_at",
    "last_scanned_at",
    "last_activity_at",
    "scan_window_end",
    "status_updated_at",
)
_DID = r"^did:(plc|web):[A-Za-z0-9._:%-]+$"
#: The atproto handle syntax (lower case, as the AppView returns handles).
_BSKY_HANDLE = r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}"
BSKY_HANDLE_MAX = 253
_REDDIT_NAME = r"[A-Za-z0-9_-]{3,20}"
_X_NAME = r"[A-Za-z0-9_]{1,15}"
_SHARD = r"^[0-9a-f]{2}$"

SCHEMA_FILE = "schema.json"
EVIDENCE_DIR = "evidence"
#: The folder of the per-event folders.
EVENTS_DIR = "events"
_B64 = r"^[A-Za-z0-9_-]+$"
_DATETIME = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$"
_EVIDENCE = r"^evidence/[a-z0-9-]+/[A-Za-z0-9_-]+\.md$"

#: Size limits, checked before anything is parsed.
MAX_RECORD_FILE_BYTES = 5_000_000
MAX_EVIDENCE_FILE_BYTES = 200_000
MAX_LINE_BYTES = 8192

#: The record schema (JSON Schema draft 2020-12). ``registry/schema.json`` is this,
#: written by :func:`schema_text`. :func:`validate_record` checks the subset of JSON
#: Schema used here, so no schema library is needed.
SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Registry record",
    "description": (
        "One scanned account. id_b64 and username_b64 are URL-safe base64 without "
        "padding: obfuscation against casual search for a trusted research group, "
        "reversible by anyone, not anonymity. No scores are stored here; an evidence "
        "file may hold the signals."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": [
        "baseline_active_days",
        "baseline_posts",
        "cliff",
        "event_id",
        "evidence",
        "first_scanned_at",
        "id_b64",
        "last_activity_at",
        "last_scanned_at",
        "platform",
        "scan_window_end",
        "status",
        "status_updated_at",
        "tool_version",
        "username_b64",
    ],
    "properties": {
        "event_id": {
            "type": "string",
            "pattern": EVENT_ID_PATTERN,
            "description": "The outage event (an id of events.toml); equal to the name of "
            "the events/<event_id>/ folder holding the record.",
        },
        "platform": {"type": "string", "enum": list(PLATFORMS)},
        "id_b64": {
            "type": "string",
            "pattern": _B64,
            "description": "Stable platform ID, URL-safe base64 without padding.",
        },
        "username_b64": {
            "type": "string",
            "pattern": _B64,
            "description": "Handle at the last scan, URL-safe base64 without padding.",
        },
        "first_scanned_at": {"type": "string", "pattern": _DATETIME},
        "last_scanned_at": {
            "type": "string",
            "pattern": _DATETIME,
            "description": "Reference time (now) of the newest scan that saw the account.",
        },
        "last_activity_at": {
            "type": "string",
            "pattern": _DATETIME,
            "description": "Newest own activity seen at scan time.",
        },
        "scan_window_end": {
            "type": "string",
            "pattern": _DATETIME,
            "description": "Where the scanned data ended (the source's data horizon, "
            "or the scan time for a live source).",
        },
        "baseline_posts": {"type": "integer", "minimum": 0},
        "baseline_active_days": {"type": "integer", "minimum": 0},
        "cliff": {"type": "boolean"},
        "tool_version": {"type": "string", "pattern": r"^[0-9A-Za-z.+-]+$"},
        "status": {"type": "string", "enum": list(STATUSES)},
        "evidence": {
            "type": ["string", "null"],
            "pattern": _EVIDENCE,
            "description": "Evidence file, relative to the event folder: "
            "evidence/<platform>/<id_b64>.md. Required for verified_inauthentic.",
        },
        "status_updated_at": {"type": "string", "pattern": _DATETIME},
    },
}

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def schema_text() -> str:
    """The schema file's exact content."""
    return json.dumps(SCHEMA, indent=2, sort_keys=True) + "\n"


def b64e(text: str) -> str:
    """URL-safe base64 of ``text`` (UTF-8) without padding."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def b64d(value: str) -> str:
    """Inverse of :func:`b64e`. Raises ``ValueError`` on malformed input."""
    if not re.fullmatch(_B64, value or ""):
        raise ValueError("not URL-safe base64")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"not URL-safe base64 of UTF-8 text: {exc}") from None


def md_code(text: str) -> str:
    """``text`` as one inline Markdown code span, safe for untrusted input.

    Non-ASCII and control characters (line breaks included) are escaped as in
    ``ascii()``, and the span's backtick fence is longer than any run of backticks
    inside, so the text cannot end the span or start a new line.
    """
    escaped = ascii(str(text))[1:-1]
    longest = max((len(run) for run in re.findall(r"`+", escaped)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {escaped} {fence}"


def current_time() -> datetime:
    """Now, for the time rules. Tests replace it to pin the clock."""
    return utcnow()


def shard_of(id_b64: str) -> str:
    """The shard name of ``id_b64``: the first two hex digits of its SHA-256."""
    return hashlib.sha256(id_b64.encode("utf-8")).hexdigest()[:2]


def event_dir(event_id: str) -> str:
    """The folder of an event, relative to the registry directory."""
    return f"{EVENTS_DIR}/{event_id}"


def record_path(event_id: str, platform: str, id_b64: str) -> str:
    """The shard file of a record, relative to the registry directory."""
    return f"{event_dir(event_id)}/{platform}/{shard_of(id_b64)}.jsonl"


def evidence_path(platform: str, id_b64: str) -> str:
    """The evidence file of a record, relative to its event folder (the record's
    ``evidence`` value)."""
    return f"{EVIDENCE_DIR}/{platform}/{id_b64}.md"


def registry_evidence_path(event_id: str, platform: str, id_b64: str) -> str:
    """The evidence file of a record, relative to the registry directory."""
    return f"{event_dir(event_id)}/{evidence_path(platform, id_b64)}"


#: The key of a record: ``(event_id, platform, id_b64)``.
Key = tuple[str, str, str]


def record_key(record: dict[str, Any]) -> Key:
    return (str(record.get("event_id")), str(record.get("platform")), str(record.get("id_b64")))


def canonical_line(record: dict[str, Any]) -> str:
    """One record as its file line (without the newline)."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _type_ok(value: Any, names: str | list[str]) -> bool:
    for name in [names] if isinstance(names, str) else names:
        if name in ("integer",) and isinstance(value, bool):
            continue
        if isinstance(value, _TYPES[name]):
            return True
    return False


def schema_errors(record: Any, schema: dict[str, Any] = SCHEMA) -> list[str]:
    """Errors of ``record`` against ``schema`` (the subset of JSON Schema it uses)."""
    if not isinstance(record, dict):
        return ["a record must be a JSON object"]
    errors: list[str] = []
    props: dict[str, Any] = schema["properties"]
    for name in schema["required"]:
        if name not in record:
            errors.append(f"missing field {name!r}")
    if schema.get("additionalProperties") is False:
        errors.extend(f"unknown field {name!r}" for name in sorted(set(record) - set(props)))
    for name, rule in props.items():
        if name not in record:
            continue
        value = record[name]
        if not _type_ok(value, rule["type"]):
            errors.append(f"{name}: expected {rule['type']}, got {type(value).__name__}")
            continue
        if value is None:
            continue
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"{name}: {value!r} is not one of {rule['enum']}")
        pattern = rule.get("pattern")
        if pattern and isinstance(value, str) and not re.fullmatch(pattern, value):
            errors.append(f"{name}: {value!r} does not match {rule['pattern']}")
        if "minimum" in rule and value < rule["minimum"]:
            errors.append(f"{name}: below the minimum {rule['minimum']}")
    return errors


def future_errors(record: dict[str, Any], now: datetime | None = None) -> list[str]:
    """The timestamps of a schema-valid ``record`` that lie in the future."""
    limit = ensure_utc(now or current_time()) + FUTURE_SLACK
    return [
        f"{name} is in the future ({record[name]})"
        for name in _TIME_FIELDS
        if parse_datetime(record[name]) > limit
    ]


def valid_username(platform: str, username: str) -> bool:
    """Whether ``username`` has the syntax of a username of ``platform``."""
    if platform == "bluesky":
        return len(username) <= BSKY_HANDLE_MAX and bool(re.fullmatch(_BSKY_HANDLE, username))
    if platform in ("reddit", "reddit-archive"):
        return bool(re.fullmatch(_REDDIT_NAME, username))
    if platform == "x":
        return bool(re.fullmatch(_X_NAME, username))
    return False


def valid_user_id(platform: str, user_id: str) -> bool:
    """Whether ``user_id`` has the syntax of a stored ID of ``platform``."""
    if platform == "bluesky":
        return bool(re.fullmatch(_DID, user_id))
    if platform == "x":
        return bool(re.fullmatch(r"[0-9]{1,30}", user_id))
    return valid_username(platform, user_id)


def identity_errors(record: dict[str, Any]) -> list[str]:
    """Errors in a schema-valid ``record``'s ID and username syntax and binding."""
    try:
        user_id, username = b64d(record["id_b64"]), b64d(record["username_b64"])
    except ValueError:
        return []  # Reported by record_errors.
    platform = record["platform"]
    if not valid_username(platform, username):
        return [f"{platform}: username_b64 does not encode a valid {platform} username"]
    if platform == "reddit-archive" and user_id != username:
        return ["reddit-archive: id_b64 and username_b64 must encode the same author name"]
    if platform == "reddit" and user_id != username.lower():
        return ["reddit: id_b64 must encode the lower-cased username_b64"]
    if platform == "bluesky" and not re.fullmatch(_DID, user_id):
        return ["bluesky: id_b64 must encode a DID (did:plc:... or did:web:...)"]
    if platform == "x" and not re.fullmatch(r"[0-9]{1,30}", user_id):
        return ["x: id_b64 must encode a numeric user ID"]
    return []


def silence(record: dict[str, Any]) -> timedelta:
    """``scan_window_end - last_activity_at`` of a schema-valid record."""
    return parse_datetime(record["scan_window_end"]) - parse_datetime(record["last_activity_at"])


def claims_silence(record: dict[str, Any]) -> bool:
    """Whether the record claims a cliff (and so needs :data:`MIN_SILENCE`)."""
    return bool(record["cliff"]) or record["status"] == STATUS_CANDIDATE


def min_silence_of(event_id: str, events: EventCatalog | None) -> timedelta:
    """The least silence a cliff record of ``event_id`` needs."""
    event = events.get(event_id) if events is not None else None
    return event.min_silence if event is not None else MIN_SILENCE


def verified_min_silence_of(event_id: str, events: EventCatalog | None) -> timedelta:
    """The least silence a record of ``event_id`` needs to become
    ``verified_inauthentic`` (the event's ``verified_min_silence_days``)."""
    event = events.get(event_id) if events is not None else None
    return event.verified_min_silence if event is not None else VERIFIED_MIN_SILENCE


def resumed(record: dict[str, Any], events: EventCatalog | None) -> str | None:
    """Why a schema-valid record shows that its account posted again, or ``None``.

    The account is no longer a cliff (``cliff`` false: a newer scan saw activity that
    leaves too little silence), or its last activity lies at or after its event's
    cutoff, newer than the silence window. A ``verified_inauthentic`` record keeps its
    status through a rescan, so this is how a stale verdict shows.
    """
    if not record["cliff"]:
        return "it is no longer a cliff (a newer scan saw activity)"
    event = events.get(record["event_id"]) if events is not None else None
    if event is not None and parse_datetime(record["last_activity_at"]) >= ensure_utc(event.cutoff):
        return (
            f"its last activity ({record['last_activity_at']}) is at or after the event's "
            f"cutoff ({event.cutoff_text}), newer than the silence window"
        )
    return None


def record_errors(
    record: Any, *, now: datetime | None = None, events: EventCatalog | None = None
) -> list[str]:
    """Schema errors plus the rules a schema cannot express (see the module notes).

    With ``events``, the record's ``event_id`` must be one of them, and the event's
    ``min_silence_days`` is the least silence of a cliff.
    """
    errors = schema_errors(record)
    if errors:
        return errors
    if events is not None and events.get(record["event_id"]) is None:
        return [f"event_id {record['event_id']!r} is not an event of {EVENTS_FILE}"]
    for name in ("id_b64", "username_b64"):
        try:
            b64d(record[name])
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
        else:
            if b64e(b64d(record[name])) != record[name]:
                errors.append(f"{name}: not canonical base64 (padding or trailing bits)")
    if errors:
        return errors
    for name in _TIME_FIELDS:
        try:
            parse_datetime(record[name])
        except ValueError:
            errors.append(f"{name}: not a valid date and time")
    if errors:
        return errors
    errors += identity_errors(record)
    errors += future_errors(record, now)
    if record["first_scanned_at"] > record["last_scanned_at"]:
        errors.append("first_scanned_at is after last_scanned_at")
    if record["last_activity_at"] > record["scan_window_end"]:
        errors.append("last_activity_at is after scan_window_end")
    if record["scan_window_end"] > record["last_scanned_at"]:
        errors.append("scan_window_end is after last_scanned_at")
    gap = parse_datetime(record["last_scanned_at"]) - parse_datetime(record["scan_window_end"])
    if gap > MAX_DATA_GAP:
        errors.append(
            f"the scanned data ends {gap} before last_scanned_at (at most {MAX_DATA_GAP})"
        )
    if record["status"] == STATUS_CANDIDATE and not record["cliff"]:
        errors.append("a candidate must be a cliff")
    least = min_silence_of(record["event_id"], events)
    if claims_silence(record) and silence(record) < least:
        errors.append(
            f"a cliff or candidate needs at least {_days(least)} days between "
            "last_activity_at and scan_window_end"
        )
    if record["status"] == STATUS_VERIFIED and not record["evidence"]:
        errors.append("verified_inauthentic needs an evidence file")
    evidence = record["evidence"]
    if evidence and evidence != evidence_path(record["platform"], record["id_b64"]):
        errors.append(
            f"evidence must be {evidence_path(record['platform'], record['id_b64'])!r}, "
            f"got {evidence!r}"
        )
    return errors


def _days(span: timedelta) -> str:
    return f"{span.total_seconds() / 86400:g}"


@dataclass(frozen=True)
class Entry:
    """One line of a registry file: its record, or why it could not be parsed."""

    path: str  # Relative to the registry directory, with forward slashes.
    line: int
    record: dict[str, Any] | None
    error: str | None = None


def registry_files(root: Path) -> list[Path]:
    """The record files under ``root``, sorted (evidence is not scanned)."""
    if not root.is_dir():
        return []
    return sorted(
        p
        for p in root.glob(f"{EVENTS_DIR}/*/*/*.jsonl")
        if p.is_file()
        and p.parent.name != EVIDENCE_DIR
        and re.fullmatch(EVENT_ID_PATTERN, p.parent.parent.name)
    )


def parse_lines(path: str, text: str) -> Iterator[Entry]:
    """Each line of a record file as an :class:`Entry`. Never raises on bad input:
    over-long lines, invalid JSON and too deeply nested JSON become errors."""
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            yield Entry(path, number, None, "blank line")
            continue
        if len(raw.encode("utf-8")) > MAX_LINE_BYTES:
            yield Entry(path, number, None, f"line longer than {MAX_LINE_BYTES} bytes")
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            yield Entry(path, number, None, f"invalid JSON: {exc.msg}")
            continue
        except (ValueError, RecursionError) as exc:
            yield Entry(path, number, None, f"invalid JSON ({type(exc).__name__})")
            continue
        if not isinstance(value, dict):
            yield Entry(path, number, None, "a record must be a JSON object")
            continue
        yield Entry(path, number, value)


def read_entries(root: Path) -> list[Entry]:
    """Every line of every record file under ``root``."""
    entries: list[Entry] = []
    for file in registry_files(root):
        rel = file.relative_to(root).as_posix()
        entries.extend(parse_lines(rel, file.read_text(encoding="utf-8")))
    return entries


def load_records(
    root: Path, *, now: datetime | None = None, events: EventCatalog | None = None
) -> dict[Key, dict[str, Any]]:
    """The records under ``root`` by :func:`record_key`; duplicates are merged.

    Raises ``ValueError`` for a line that is not a valid record (a future timestamp
    or, with ``events``, an unknown event included) and for a record outside its
    event's folder, naming the file and line, so a broken registry is never silently
    rewritten.
    """
    out: dict[Key, dict[str, Any]] = {}
    for entry in read_entries(root):
        if entry.record is None:
            raise ValueError(f"{entry.path}:{entry.line}: {entry.error}")
        errors = record_errors(entry.record, now=now, events=events)
        if not errors and entry.path.split("/")[1] != entry.record["event_id"]:
            errors = [_folder_error(entry.record, entry.path)]
        if errors:
            raise ValueError(f"{entry.path}:{entry.line}: {'; '.join(errors)}")
        key = record_key(entry.record)
        least = min_silence_of(key[0], events)
        out[key] = (
            merge_records(out[key], entry.record, min_silence=least) if key in out else entry.record
        )
    return out


def _folder_error(record: dict[str, Any], path: str) -> str:
    return (
        f"event_id {record['event_id']!r} does not match its folder "
        f"{'/'.join(path.split('/')[:2])!r}"
    )


def render_files(records: Iterable[dict[str, Any]]) -> dict[str, str]:
    """The canonical file contents for ``records``, by relative path."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        path = record_path(record["event_id"], record["platform"], record["id_b64"])
        grouped.setdefault(path, []).append(record)
    return {
        path: "".join(canonical_line(r) + "\n" for r in sorted(rows, key=lambda r: r["id_b64"]))
        for path, rows in sorted(grouped.items())
    }


def write_records(root: Path, records: Iterable[dict[str, Any]]) -> list[str]:
    """Write ``records`` to ``root`` canonically; return the paths whose bytes changed.

    Record files that end up with no records are removed, and ``schema.json`` is
    (re)written. Files are written with ``\\n`` line endings on every platform.
    """
    root.mkdir(parents=True, exist_ok=True)
    wanted = render_files(records)
    wanted[SCHEMA_FILE] = schema_text()
    changed: list[str] = []
    for file in registry_files(root):
        rel = file.relative_to(root).as_posix()
        if rel not in wanted:
            file.unlink()
            changed.append(rel)
            folder = file.parent
            while folder != root and not any(folder.iterdir()):
                folder.rmdir()
                folder = folder.parent
    for rel, text in wanted.items():
        target = root / rel
        if target.is_file() and target.read_bytes() == text.encode("utf-8"):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        changed.append(rel)
    return sorted(changed)


def merge_records(
    a: dict[str, Any], b: dict[str, Any], *, min_silence: timedelta = MIN_SILENCE
) -> dict[str, Any]:
    """Merge two records of the same account. Symmetric and deterministic.

    * Scan facts come from the record with the newer ``last_scanned_at``;
      ``first_scanned_at`` is the earliest and ``last_activity_at`` the latest of the
      two.
    * Status: a human status (``verified_inauthentic`` or ``cleared``) always beats
      an automatic one. Between two human statuses the newer ``status_updated_at``
      wins (on a tie, ``cleared``: the cautious choice). Between two automatic ones
      the record with the newer scan decides. The evidence travels with the status
      that wins. When both records have the same status, ``status_updated_at`` is
      the earlier one (when the account first got it).
    * When the kept ``last_activity_at`` leaves less than ``min_silence`` (the
      event's) to the data end, the result is not a cliff, and a ``candidate`` becomes
      ``scanned``.
    """
    if record_key(a) != record_key(b):
        raise ValueError("cannot merge records of different accounts")
    if a == b:
        return dict(a)
    # The tie-break on the canonical line keeps the result independent of order.
    newer = max((a, b), key=lambda r: (r["last_scanned_at"], canonical_line(r)))
    out = dict(newer)
    out["first_scanned_at"] = min(a["first_scanned_at"], b["first_scanned_at"])
    out["last_activity_at"] = max(a["last_activity_at"], b["last_activity_at"])
    out["scan_window_end"] = max(out["scan_window_end"], out["last_activity_at"])
    humans = [r for r in (a, b) if r["status"] in HUMAN_STATUSES]
    if humans:
        winner = max(
            humans,
            key=lambda r: (
                r["status_updated_at"],
                r["status"] == STATUS_CLEARED,
                canonical_line(r),
            ),
        )
    else:
        winner = newer
    out["status"] = winner["status"]
    out["evidence"] = winner["evidence"]
    if out["evidence"] is None and winner["status"] in HUMAN_STATUSES:
        other = b if winner is a else a
        out["evidence"] = other["evidence"]
    same = [r["status_updated_at"] for r in (a, b) if r["status"] == out["status"]]
    out["status_updated_at"] = min(same)
    # The later last activity of the older scan can leave too little silence for the
    # newer scan's cliff: the merged record then is no cliff (and no candidate).
    if claims_silence(out) and silence(out) < min_silence:
        out["cliff"] = False
        if out["status"] == STATUS_CANDIDATE:
            out["status"] = STATUS_SCANNED
            same = [r["status_updated_at"] for r in (a, b) if r["status"] == STATUS_SCANNED]
            out["status_updated_at"] = min(same) if same else out["last_scanned_at"]
    return out


class RegistryTree:
    """The files of a registry, read from a directory or from a git commit.

    The git mode reads objects only (``git ls-tree`` and ``git cat-file``): nothing of
    the commit is checked out, so a pull request's files are never written to disk.

    The events file is read from next to the registry: ``events.toml`` in the
    registry directory's parent, or at :func:`~bot_farm_silence_tracker.events.
    events_path_for` in the commit (see :meth:`events`).
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        repo: Path | None = None,
        ref: str | None = None,
        registry_path: str = "registry",
    ) -> None:
        if (root is None) == (ref is None):
            raise ValueError("give a registry directory or a git ref")
        self.root, self.repo, self.ref = root, repo or Path("."), ref
        self.prefix = registry_path.strip("/") + "/"
        self._modes: dict[str, str] | None = None
        self._sizes: dict[str, int] = {}
        self._oids: dict[str, str] = {}

    def describe(self) -> str:
        return str(self.root) if self.root is not None else f"{self.ref}:{self.prefix}"

    @property
    def events_path(self) -> str:
        """Where the events file is: a path, or ``<ref>:<path>`` in the repository."""
        if self.root is not None:
            return str(self.root.expanduser().resolve().parent / EVENTS_FILE)
        return f"{self.ref}:{events_path_for(self.prefix)}"

    def has_events_file(self) -> bool:
        """Whether the events file exists (in any form) next to the registry."""
        if self.root is not None:
            path = self.root.expanduser().resolve().parent / EVENTS_FILE
            return path.exists() or path.is_symlink()
        name = events_path_for(self.prefix)
        return bool(git(self.repo, "ls-tree", "--full-tree", str(self.ref), "--", name).strip())

    def events(self, *, now: datetime | None = None) -> tuple[EventCatalog | None, list[str]]:
        """The events of this registry, or ``None`` with the problems.

        A directory without an ``events.toml`` next to it uses the events of this
        installation (:func:`~bot_farm_silence_tracker.events.load_events`). A commit
        must have the file. The file must be a regular file of at most
        :data:`~bot_farm_silence_tracker.events.MAX_EVENTS_FILE_BYTES`; its content is
        validated by :func:`~bot_farm_silence_tracker.events.parse_events`, so bad
        input is a problem, never an error.
        """
        name = events_path_for(self.prefix)
        if self.root is not None:
            path = self.root.expanduser().resolve().parent / EVENTS_FILE
            if not path.exists() and not path.is_symlink():
                try:
                    return load_events(), []
                except ValueError as exc:
                    return None, [str(exc)]
            if path.is_symlink() or not path.is_file():
                return None, [f"{EVENTS_FILE} must be a regular file"]
            if path.stat().st_size > MAX_EVENTS_FILE_BYTES:
                return None, [f"{EVENTS_FILE}: larger than {MAX_EVENTS_FILE_BYTES} bytes"]
            return parse_events(
                path.read_bytes(), source=EVENTS_FILE, now=now, require_windows=True
            )
        out = git(self.repo, "ls-tree", "-z", "-l", "--full-tree", str(self.ref), "--", name)
        items = [item for item in out.split("\0") if item]
        if not items:
            return None, [f"{name} is missing"]
        fields = items[0].split("\t", 1)[0].split()
        if fields[0] != "100644" or fields[1] != "blob":
            return None, [f"{name} must be a regular, non-executable file (mode {fields[0]})"]
        if not fields[-1].isdigit() or int(fields[-1]) > MAX_EVENTS_FILE_BYTES:
            return None, [f"{name}: larger than {MAX_EVENTS_FILE_BYTES} bytes"]
        raw = git_bytes(self.repo, "cat-file", "blob", f"{self.ref}:{name}")
        return parse_events(raw, source=name, now=now, require_windows=True)

    def modes(self) -> dict[str, str]:
        """Every file by path relative to the registry, with its git mode.

        Directory files report ``100644``; hidden files (``.DS_Store`` and the like)
        are skipped in a directory but not in a commit.
        """
        if self._modes is not None:
            return self._modes
        modes: dict[str, str] = {}
        if self.root is not None:
            if self.root.is_dir():
                for path in sorted(self.root.rglob("*")):
                    rel = path.relative_to(self.root).as_posix()
                    if any(part.startswith(".") for part in rel.split("/")):
                        continue
                    if path.is_symlink():
                        modes[rel] = "120000"
                    elif path.is_file():
                        modes[rel] = "100644"
                        self._sizes[rel] = path.stat().st_size
        else:
            out = git(
                self.repo,
                "ls-tree",
                "-r",
                "-z",
                "-l",
                "--full-tree",
                str(self.ref),
                "--",
                self.prefix,
            )
            for item in out.split("\0"):
                if not item:
                    continue
                meta, name = item.split("\t", 1)
                fields = meta.split()
                rel = name[len(self.prefix) :]
                modes[rel] = fields[0]
                self._oids[rel] = fields[2]
                if fields[-1].isdigit():
                    self._sizes[rel] = int(fields[-1])
        self._modes = modes
        return modes

    def size(self, rel: str) -> int | None:
        """The size in bytes of a regular file (``None`` for other entries)."""
        self.modes()
        return self._sizes.get(rel)

    def fingerprint(self, rel: str) -> str:
        """An ID of the file's content: its git object ID, or a SHA-256 of a file on disk."""
        self.modes()
        if self.root is None:
            return self._oids[rel]
        return hashlib.sha256((self.root / rel).read_bytes()).hexdigest()

    def too_large(self, rel: str) -> str | None:
        """Why the file at ``rel`` is too large to read, or ``None``."""
        limit = MAX_EVIDENCE_FILE_BYTES if file_kind(rel) == "evidence" else MAX_RECORD_FILE_BYTES
        size = self.size(rel)
        if size is None or size <= limit:
            return None
        return f"{rel}: larger than {limit} bytes"

    def read(self, rel: str) -> bytes:
        if self.root is not None:
            return (self.root / rel).read_bytes()
        return git_bytes(self.repo, "cat-file", "blob", f"{self.ref}:{self.prefix}{rel}")


def git_bytes(repo: Path, *args: str) -> bytes:
    """Run ``git -C repo args`` and return its stdout. Raises ``ValueError``."""
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    except OSError as exc:
        raise ValueError(f"git is not available: {exc}") from None
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"git {' '.join(args[:2])} failed: {err}")
    return done.stdout


def git(repo: Path, *args: str) -> str:
    return git_bytes(repo, *args).decode("utf-8")


def file_kind(rel: str) -> str | None:
    """``"records"``, ``"evidence"`` or ``"schema"`` for an allowed registry path.

    Records are ``events/<event-id>/<platform>/<shard>.jsonl`` and evidence is
    ``events/<event-id>/evidence/<platform>/<id_b64>.md``; whether the event exists
    is checked by :func:`validate_registry`.
    """
    if rel == SCHEMA_FILE:
        return "schema"
    parts = rel.split("/")
    if len(parts) < 4 or parts[0] != EVENTS_DIR or not re.fullmatch(EVENT_ID_PATTERN, parts[1]):
        return None
    rest = parts[2:]
    if (
        len(rest) == 2
        and rest[0] in PLATFORMS
        and rest[1].endswith(".jsonl")
        and re.fullmatch(_SHARD, rest[1][: -len(".jsonl")])
    ):
        return "records"
    if (
        len(rest) == 3
        and rest[0] == EVIDENCE_DIR
        and rest[1] in PLATFORMS
        and rest[2].endswith(".md")
        and re.fullmatch(_B64, rest[2][: -len(".md")])
    ):
        return "evidence"
    return None


def event_of(rel: str) -> str | None:
    """The event folder name of a records or evidence path, else ``None``."""
    return rel.split("/")[1] if file_kind(rel) in ("records", "evidence") else None


def validate_registry(
    root: Path | None = None,
    *,
    tree: RegistryTree | None = None,
    now: datetime | None = None,
    events: EventCatalog | None = None,
) -> list[str]:
    """Every problem with the registry; empty when it is valid.

    ``root`` is a registry directory; ``tree`` reads one from a git commit instead.
    ``events`` are the events to check against; by default the tree's own
    (:meth:`RegistryTree.events`), whose problems are reported too.
    Fails on an invalid events file, files that are not records, evidence or the
    schema (or not regular files), files in the folder of an unknown event,
    unparsable lines, schema and rule errors (future timestamps included), a record
    whose ``event_id`` is not its folder or not an event, the same account recorded
    twice in one event (in any files), a record in the wrong shard file, files that
    are not in canonical form (run ``registry merge``), a ``schema.json`` that
    differs from the tool's, and an evidence path that does not exist in the
    record's event folder, which a ``verified_inauthentic`` record must have.
    """
    tree = tree or RegistryTree(root)
    problems: list[str] = []
    if events is None:
        events, event_problems = tree.events(now=now)
        problems.extend(event_problems)
    catalog = events if events is not None else EventCatalog((), "", EVENTS_FILE)
    modes = tree.modes()
    oversized: set[str] = set()
    for rel, mode in sorted(modes.items()):
        if file_kind(rel) is None:
            problems.append(f"{rel}: unexpected file (only records, evidence and schema.json)")
        elif mode != "100644":
            problems.append(f"{rel}: must be a regular, non-executable file (mode {mode})")
        elif (why := tree.too_large(rel)) is not None:
            problems.append(why)
            oversized.add(rel)
        if (folder := event_of(rel)) is not None and folder not in catalog:
            problems.append(f"{rel}: in the folder of {folder!r}, which is not an event")
    seen: dict[Key, str] = {}
    by_file: dict[str, list[dict[str, Any]]] = {}
    texts: dict[str, bytes] = {}
    valid = not oversized
    for rel in sorted(r for r, m in modes.items() if file_kind(r) == "records" and m == "100644"):
        if rel in oversized:
            continue
        texts[rel] = tree.read(rel)
        try:
            text = texts[rel].decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: not UTF-8")
            valid = False
            continue
        for entry in parse_lines(rel, text):
            where = f"{entry.path}:{entry.line}"
            if entry.record is None:
                problems.append(f"{where}: {entry.error}")
                valid = False
                continue
            record = entry.record
            try:
                errors = record_errors(record, now=now, events=catalog)
            except (ValueError, RecursionError) as exc:
                errors = [f"cannot be checked ({type(exc).__name__})"]
            if errors:
                problems.extend(f"{where}: {e}" for e in errors)
                valid = False
                continue
            if event_of(entry.path) != record["event_id"]:
                problems.append(f"{where}: {_folder_error(record, entry.path)}")
                valid = False
                continue
            key = record_key(record)
            if key in seen:
                problems.append(
                    f"{where}: duplicate of {seen[key]} ({record['event_id']} "
                    f"{record['platform']} {record['id_b64']})"
                )
                valid = False
            else:
                seen[key] = where
            expected = record_path(record["event_id"], record["platform"], record["id_b64"])
            if entry.path != expected:
                problems.append(f"{where}: belongs in {expected}")
                valid = False
            evidence = record["evidence"]
            full = f"{event_dir(record['event_id'])}/{evidence}" if evidence else None
            if full and modes.get(full) != "100644":
                problems.append(f"{where}: evidence file {full} does not exist")
            by_file.setdefault(entry.path, []).append(record)
    if valid:
        rendered = render_files(r for rows in by_file.values() for r in rows)
        for rel in sorted(by_file):
            if texts[rel] != rendered[rel].encode("utf-8"):
                problems.append(f"{rel}: not in canonical form (run registry merge)")
    if by_file or SCHEMA_FILE in modes:
        if SCHEMA_FILE not in modes:
            problems.append(f"{SCHEMA_FILE} is missing (run registry merge)")
        elif modes[SCHEMA_FILE] == "100644" and tree.read(SCHEMA_FILE) != schema_text().encode(
            "utf-8"
        ):
            problems.append(f"{SCHEMA_FILE} differs from this version's schema")
    return problems
