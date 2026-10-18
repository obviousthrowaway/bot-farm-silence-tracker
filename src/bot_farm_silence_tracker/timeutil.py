"""Small helpers for timezone-aware UTC datetimes."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as UTC, treating naive datetimes as already being UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(text: str) -> datetime:
    """Parse ``YYYY-MM-DD`` or an ISO-8601 timestamp (``Z`` allowed) into UTC."""
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(cleaned))


def isoformat(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string with second precision."""
    return ensure_utc(value).isoformat(timespec="seconds")
