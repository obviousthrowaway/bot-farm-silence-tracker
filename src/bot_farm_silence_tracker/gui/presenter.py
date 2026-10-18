"""The desktop app's logic, kept free of Tk so it can be tested without a display.

The view (:mod:`.view`) only draws what this module computes and forwards clicks to
it. Scans run through the ``scan`` command itself (:mod:`.runner`) on a worker
thread; the worker only puts events on a queue, and the view drains that queue on
the Tk main thread (:meth:`ScanJob.drain`), because Tk is not thread-safe.

Credentials typed into the app live only in a :class:`SessionCredentials` object in
memory. They are handed to the scan the same way environment variables are (see
:class:`~bot_farm_silence_tracker.cli.ScanHooks`), never put in the process
environment, never written to a file, the config or the database, and never logged.
"""

from __future__ import annotations

import math
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker import config as user_config
from bot_farm_silence_tracker import events as outage_events
from bot_farm_silence_tracker.cli import (
    ScanHooks,
    _Redactor,
    git_toplevel,
)
from bot_farm_silence_tracker.controls import headline, independent
from bot_farm_silence_tracker.network import format_network, network_report, platform_network
from bot_farm_silence_tracker.output import (
    NOTICE,
    OUTAGE_NOTICE,
    PROVISIONAL_NOTICE,
    truncated_notice,
)
from bot_farm_silence_tracker.pipeline import ScanOutcome, rescore_run
from bot_farm_silence_tracker.registry.build import fired_signals
from bot_farm_silence_tracker.registry.evidence import BSKY_PROFILE_URL, profile_links
from bot_farm_silence_tracker.registry.records import valid_user_id, valid_username
from bot_farm_silence_tracker.scoring import (
    MAIN_OUTAGE,
    AccountResult,
    Marker,
    ScoringParams,
    marker_label,
)
from bot_farm_silence_tracker.secretmap import SecretDict
from bot_farm_silence_tracker.seeds import REDDIT_PLATFORMS, SEED_KEYS, load_seeds
from bot_farm_silence_tracker.sources.base import ENV_PREFIX
from bot_farm_silence_tracker.sources.errors import FetchCancelled
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY, load_source_class
from bot_farm_silence_tracker.storage import EVENTS_DIR, Storage, cache_dir, prepare_event_db
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat, parse_datetime, utcnow

#: The sample size the app suggests. The command line's default is far larger; a
#: first scan from the app should finish in minutes, not hours.
DEFAULT_SAMPLE = 30
#: The command line's own limits on the options the app offers.
MIN_SAMPLE = 1
DEFAULT_MAX_CONSECUTIVE_ERRORS = 10

#: Where the app writes scan results unless the user picks another folder.
DEFAULT_OUTPUT_DIR = Path.home() / "Bot Farm Silence Tracker"

#: Short, plain descriptions of each platform for the Platforms screen.
PLATFORM_BLURBS = {
    "reddit-archive": "Reddit, read from a public archive. No account needed.",
    "reddit": "Reddit's own API. Needs Reddit API credentials.",
    "bluesky": "Bluesky's public API. A login is optional.",
    "x": "X (Twitter) API. Needs a paid API token and a spend cap.",
    "fake": "Made-up demo data. Makes no network requests; good for trying the app.",
}

#: The note every results view shows.
NOT_PROOF = (
    "Flagged accounts are potential bots for manual review only. Going silent at the "
    "cutoff is not proof that an account is a bot: people take breaks, get banned or "
    "leave, and the reported outage cut off ordinary users too."
)
#: What the Network and privacy screen adds about links opened in the browser.
BROWSER_NOTE = (
    "Profile and community links open in your web browser. The browser, not this "
    "program, makes those requests, so they are outside the host list above. The app "
    "itself contacts no host other than the ones listed for the platform you scan, "
    "and only while a scan runs."
)

# -- Platforms -----------------------------------------------------------------


def platform_names() -> list[str]:
    """Every platform the program can scan, demo data last."""
    names = sorted(name for name in SOURCE_REGISTRY if name in SEED_KEYS and name != "fake")
    return [*names, "fake"] if "fake" in SOURCE_REGISTRY else names


@dataclass(frozen=True)
class PlatformSettings:
    """What the config file says, as the Platforms screen shows it."""

    path: Path
    enabled: tuple[str, ...]
    default: str
    warnings: tuple[str, ...] = ()
    error: str | None = None


def load_platform_settings(path: Path | None = None) -> PlatformSettings:
    """Read the config file the command line uses (``config show``)."""
    target = path or user_config.default_config_path()
    try:
        loaded = user_config.load_config(target)
    except user_config.ConfigError as exc:
        return PlatformSettings(
            target, (user_config.DEFAULT_PLATFORM,), user_config.DEFAULT_PLATFORM, (), str(exc)
        )
    return PlatformSettings(loaded.path, loaded.enabled, loaded.default, loaded.warnings)


def save_platform_settings(
    enabled: Sequence[str], default: str, path: Path | None = None
) -> PlatformSettings:
    """Save the choice through the same code as ``config set-platforms`` and
    ``config set-default``. Raises :class:`~bot_farm_silence_tracker.config.ConfigError`
    with a plain message when the choice is not valid."""
    if not enabled:
        raise user_config.ConfigError("choose at least one platform")
    names = user_config.parse_platforms(",".join(enabled))
    if default not in names:
        raise user_config.ConfigError("the default platform must be one of the chosen ones")
    target = path or user_config.default_config_path()
    saved = user_config.set_platforms(target, names)
    if saved.default != default:
        saved = user_config.set_default(target, default)
    return PlatformSettings(saved.path, saved.enabled, saved.default, saved.warnings)


def load_keep_awake(path: Path | None = None) -> bool:
    """The "Keep this computer awake while scanning" checkbox: on unless the config says off."""
    try:
        return user_config.load_config(path or user_config.default_config_path()).keep_awake_enabled
    except user_config.ConfigError:
        return True


def save_keep_awake(keep_awake: bool, path: Path | None = None) -> None:
    """Save the checkbox in the config file. Raises
    :class:`~bot_farm_silence_tracker.config.ConfigError` when the file cannot be written."""
    user_config.set_keep_awake(path or user_config.default_config_path(), keep_awake)


# -- Credentials ---------------------------------------------------------------


@dataclass(frozen=True)
class CredentialField:
    """One credential a platform reads, and which hosts receive it."""

    env: str
    required: bool
    secret: bool
    #: ``(host, how it is sent)`` for every host that receives the value.
    receivers: tuple[tuple[str, str], ...]
    #: Whether the environment already sets it (the value itself is never read here).
    from_environment: bool

    @property
    def label(self) -> str:
        """A readable name: the variable without the program prefix."""
        return self.env.removeprefix(ENV_PREFIX).replace("_", " ").title()


def credential_fields(
    platform: str, environ: Mapping[str, str] | None = None
) -> list[CredentialField]:
    """The credentials ``platform`` reads, from the same declarations as ``network``."""
    env = os.environ if environ is None else environ
    info = platform_network(platform)
    secret: dict[str, bool] = {}
    receivers: dict[str, list[tuple[str, str]]] = {}
    for host in info["hosts"]:
        for cred in host["credentials"]:
            receivers.setdefault(cred["env"], []).append((host["host"], cred["how"]))
            secret[cred["env"]] = secret.get(cred["env"], False) or bool(cred["secret"])
    return [
        CredentialField(
            env=c["env"],
            required=bool(c["required"]),
            secret=secret.get(c["env"], True),
            receivers=tuple(receivers.get(c["env"], ())),
            from_environment=bool(env.get(c["env"], "").strip()),
        )
        for c in info["credentials"]
    ]


class SessionCredentials:
    """Credentials typed into the app, for this session only.

    Kept in memory, handed to scans like environment variables, and cleared when the
    app closes. Never written anywhere; :meth:`__repr__` shows names only.
    """

    def __init__(self) -> None:
        self._values: SecretDict = SecretDict()
        self._lock = threading.Lock()

    def set(self, env: str, value: str) -> None:
        with self._lock:
            if value.strip():
                self._values[env] = value
            else:
                self._values.pop(env, None)

    def has(self, env: str) -> bool:
        with self._lock:
            return env in self._values

    def snapshot(self) -> SecretDict:
        """A copy of the values, for one scan (its ``repr`` shows names only)."""
        with self._lock:
            return SecretDict(self._values)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def __repr__(self) -> str:
        with self._lock:
            names = ", ".join(sorted(self._values))
        return f"SessionCredentials({names or 'empty'})"

    __str__ = __repr__


# -- New scan ------------------------------------------------------------------


# -- Cutoff and outage events -------------------------------------------------


@dataclass(frozen=True)
class EventChoice:
    """One entry of the New scan screen's event menu.

    An event is a reported outage with its own cutoff and its own data store
    (see :mod:`bot_farm_silence_tracker.events`). The menu lists the outage events of
    ``events.toml``, the default one first, and then :data:`CUSTOM_EVENT`, whose
    cutoff the user picks: a scan of it is a local-only custom event
    (``custom-YYYYMMDDTHHMMZ``) with its own store, never shared.
    """

    id: str
    label: str
    #: ``None`` for the custom entry: the date picker decides.
    cutoff: datetime | None


CUSTOM_EVENT = EventChoice("custom", "Custom date\u2026", None)


def format_cutoff(cutoff: datetime) -> str:
    """A cutoff as people read it: ``YYYY-MM-DD HH:MM UTC``."""
    return ensure_utc(cutoff).strftime("%Y-%m-%d %H:%M UTC")


# Local time hints. Every stored and entered time is UTC; these only help people read
# a UTC moment in the time zone of their computer. The zone, its abbreviation and its
# offset come from the converted moment itself (``datetime.astimezone()``), so daylight
# saving time is the one in force on that date, not today's.

#: The minus sign of a negative offset: U+2212, not a hyphen.
_MINUS = "\u2212"


def _offset_text(offset: timedelta | None) -> str:
    """``UTC`` for no offset, else ``UTC+5:30`` or ``UTC-4``."""
    seconds = int((offset or timedelta()).total_seconds())
    if seconds == 0:
        return "UTC"
    sign = "+" if seconds > 0 else _MINUS
    hours, rest = divmod(abs(seconds), 3600)
    minutes = rest // 60
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def _clock(local: datetime) -> str:
    """``8:30 PM`` (12-hour clock, no leading zero; portable, unlike ``%-I``)."""
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _short_date(local: datetime) -> str:
    """``Oct 10`` (no leading zero)."""
    return f"{MONTHS[local.month - 1]} {local.day}"


def local_time(when: datetime) -> datetime:
    """``when`` in the computer's local time zone."""
    return ensure_utc(when).astimezone()


def is_local_utc(when: datetime) -> bool:
    """Whether the computer's zone is UTC itself at ``when`` (no offset)."""
    return not local_time(when).utcoffset()


def local_zone(when: datetime) -> str:
    """The local zone at ``when``: ``EDT, UTC-4``; just ``UTC-4`` when the zone
    has no letter abbreviation; ``UTC`` for UTC itself."""
    local = local_time(when)
    offset = _offset_text(local.utcoffset())
    if offset == "UTC":
        return "UTC"
    name = local.tzname() or ""
    if not name.isalpha() or name.upper() in ("UTC", "GMT"):
        return offset
    return f"{name}, {offset}"


def local_picker_line(when: datetime) -> str:
    """The line under the cutoff picker: ``= Sat Oct 10, 8:30 PM on this computer (EDT,
    UTC-4)``. On a computer set to UTC the time is not repeated."""
    if is_local_utc(when):
        return "= the same time on this computer (UTC)"
    local = local_time(when)
    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[local.weekday()]
    return (
        f"= {weekday} {_short_date(local)}, {_clock(local)} on this computer ({local_zone(when)})"
    )


def local_hint(when: datetime) -> str:
    """The secondary local time after a UTC time: ``(Oct 10 8:30 PM local)``, or
    ``(UTC)`` when the computer's zone is UTC."""
    if is_local_utc(when):
        return "(UTC)"
    local = local_time(when)
    return f"({_short_date(local)} {_clock(local)} local)"


def with_local(when: datetime) -> str:
    """A UTC time with its local hint: ``2026-12-03 06:00 UTC (Dec 3 1:00 AM local)``."""
    return f"{format_cutoff(when)} {local_hint(when)}"


def default_event() -> outage_events.OutageEvent:
    """The event the app starts with: the config file's ``default_event`` when it is
    an event, else the default of ``events.toml`` (as the command line does)."""
    catalog = outage_events.load_events()
    try:
        chosen = user_config.load_config().default_event
    except user_config.ConfigError:
        chosen = None
    if chosen is not None:
        try:
            return catalog.resolve(chosen)
        except outage_events.EventError:
            pass
    return catalog.default


def choice_of(event: outage_events.OutageEvent) -> EventChoice:
    """The menu entry of an event; a custom event is the custom entry."""
    if event.custom:
        return CUSTOM_EVENT
    return EventChoice(event.id, f"{event.name} ({format_cutoff(event.cutoff)})", event.cutoff)


def event_choices() -> list[EventChoice]:
    """The event menu: the default event, the other events of ``events.toml`` in file
    order, then a custom date."""
    first = default_event()
    others = [e for e in outage_events.load_events().events if e.id != first.id]
    named = [choice_of(first)] if not first.custom else []
    return [*named, *(choice_of(e) for e in others), CUSTOM_EVENT]


def default_choice() -> EventChoice:
    """The menu entry the New scan screen starts with."""
    return event_choices()[0]


def default_cutoff() -> datetime:
    """The cutoff the New scan screen starts with (the default event's)."""
    return ensure_utc(default_event().cutoff)


def __getattr__(name: str) -> Any:
    """``DEFAULT_CUTOFF`` and ``DEFAULT_EVENT_ID``, read from the events when used, so
    the app never hardcodes a date."""
    if name == "DEFAULT_CUTOFF":
        return default_cutoff()
    if name == "DEFAULT_EVENT_ID":
        return default_choice().id
    raise AttributeError(name)


def event_of_choice(choice_id: str, cutoff: datetime) -> outage_events.OutageEvent:
    """The outage event a menu choice and a picked cutoff stand for.

    A named event whose cutoff the picker still shows is that event; the custom
    entry, or a named one whose cutoff was changed, is the custom event of the
    picked cutoff. Raises :class:`~bot_farm_silence_tracker.events.EventError`.
    """
    catalog = outage_events.load_events()
    event = catalog.get(choice_id)
    if event is not None and ensure_utc(event.cutoff) == ensure_utc(cutoff):
        return event
    return outage_events.custom_event(ensure_utc(cutoff))


def event_store(event_id: str | None = None, db: Path | None = None) -> Path:
    """The database the app reads for ``event_id`` (``None``: the default event):
    ``db`` when one was given, else the event's own store, as the command line picks
    it (the old single database is moved into the default event's folder first)."""
    if db is not None:
        return Path(db)
    catalog = outage_events.load_events()
    return prepare_event_db(event_id or default_event().id, legacy_event_id=catalog.legacy_id)


def _cutoff_text(cutoff: datetime) -> str:
    """A cutoff as ``scan --cutoff`` takes it: the date alone when it is midnight UTC."""
    when = ensure_utc(cutoff)
    if when.time() == dt_time(0, 0):
        return when.strftime("%Y-%m-%d")
    return isoformat(when)


def event_by_label(label: str) -> EventChoice:
    return next((e for e in event_choices() if e.label == label), CUSTOM_EVENT)


def event_for_cutoff(cutoff: datetime) -> EventChoice:
    """The event whose cutoff is ``cutoff``, else the custom entry."""
    when = ensure_utc(cutoff)
    return next(
        (e for e in event_choices() if e.cutoff is not None and ensure_utc(e.cutoff) == when),
        CUSTOM_EVENT,
    )


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def picker_choices(now: datetime | None = None) -> dict[str, list[str]]:
    """Values of the date and time picker's menus (years up to this one)."""
    this_year = ensure_utc(now or utcnow()).year
    first = min(default_cutoff().year, this_year) - 10
    return {
        "year": [str(y) for y in range(this_year, first - 1, -1)],
        "month": [f"{i:02d} {name}" for i, name in enumerate(MONTHS, 1)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "hour": [f"{h:02d}" for h in range(24)],
        "minute": [f"{m:02d}" for m in range(60)],
    }


def picker_values(cutoff: datetime) -> dict[str, str]:
    """What the picker's menus show for ``cutoff`` (UTC)."""
    when = ensure_utc(cutoff)
    return {
        "year": str(when.year),
        "month": f"{when.month:02d} {MONTHS[when.month - 1]}",
        "day": f"{when.day:02d}",
        "hour": f"{when.hour:02d}",
        "minute": f"{when.minute:02d}",
    }


def cutoff_from_picker(values: Mapping[str, str]) -> datetime:
    """The UTC cutoff the picker's menus describe. Raises ``ValueError`` with a plain
    message for a day the month does not have."""
    try:
        parts = {key: int(str(values[key]).split()[0]) for key in picker_values(utcnow())}
    except (KeyError, ValueError, IndexError):
        raise ValueError("Choose a full cutoff date and time.") from None
    try:
        return datetime(
            parts["year"], parts["month"], parts["day"], parts["hour"], parts["minute"], tzinfo=UTC
        )
    except ValueError:
        raise ValueError("That cutoff date does not exist; check the day of the month.") from None


@dataclass
class ScanForm:
    """What the New scan screen collects, as text, the way the user typed it."""

    platform: str = user_config.DEFAULT_PLATFORM
    n: str = str(DEFAULT_SAMPLE)
    #: The event menu's choice (see :func:`event_choices`) and the picked cutoff.
    event: str = field(default_factory=lambda: default_choice().id)
    cutoff: datetime = field(default_factory=default_cutoff)
    #: Seed communities (subreddits) or queries, one per line or comma-separated;
    #: empty means the bundled defaults.
    seeds: str = ""
    # Advanced
    concurrency: str = "1"
    max_requests: str = ""
    max_consecutive_errors: str = str(DEFAULT_MAX_CONSECUTIVE_ERRORS)
    # X only
    max_usd: str = ""
    confirm_billed: bool = False
    #: Keep the computer from idle sleep while the scan runs.
    keep_awake: bool = True


@dataclass(frozen=True)
class ScanRequest:
    """A validated scan."""

    platform: str
    n: int
    cutoff: datetime
    seeds: tuple[str, ...]
    concurrency: int
    max_requests: int | None
    max_consecutive_errors: int
    max_usd: float | None
    confirm_billed: bool
    #: The outage event (an id of ``events.toml``, or a ``custom-...`` id for a
    #: picked date); its store holds the run.
    event_id: str = ""
    keep_awake: bool = True

    @property
    def custom(self) -> bool:
        return outage_events.is_custom(self.event_id)


def default_seeds(platform: str) -> tuple[str, ...]:
    """The seed communities or queries the command line uses by default."""
    try:
        return load_seeds(platform).items
    except ValueError:
        return ()


def seed_kind(platform: str) -> str:
    """What a seed is called on ``platform`` (for labels)."""
    if platform in REDDIT_PLATFORMS:
        return "subreddits"
    if platform == "fake":
        return "communities"
    return "search queries or lists"


def _split_seeds(text: str) -> tuple[str, ...]:
    parts = re.split(r"[\n,]", text)
    return tuple(dict.fromkeys(p.strip() for p in parts if p.strip()))


def _int(text: str, label: str, errors: list[str], *, low: int, high: int | None = None) -> int:
    try:
        value = int(text.strip())
    except ValueError:
        errors.append(f"{label} must be a whole number.")
        return low
    if value < low or (high is not None and value > high):
        span = f"between {low} and {high}" if high is not None else f"at least {low}"
        errors.append(f"{label} must be {span}.")
    return value


def max_concurrency(platform: str) -> int:
    try:
        return int(load_source_class(platform).max_concurrency)
    except Exception:
        return 1


def validate_form(
    form: ScanForm, *, now: datetime | None = None
) -> tuple[ScanRequest | None, list[str]]:
    """Check ``form`` with the command line's rules. Returns the request or the problems."""
    errors: list[str] = []
    platform = form.platform.strip().lower()
    if platform not in SEED_KEYS or platform not in SOURCE_REGISTRY:
        errors.append(f"Unknown platform {form.platform!r}.")
        platform = user_config.DEFAULT_PLATFORM
    n = _int(form.n, "Sample size", errors, low=MIN_SAMPLE)
    cutoff = form.cutoff
    event_id = ""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        errors.append("The cutoff must carry a time zone (UTC).")
    elif ensure_utc(cutoff) >= ensure_utc(now or utcnow()):
        errors.append("The cutoff must be in the past.")
    else:
        cutoff = ensure_utc(cutoff)
        try:
            event_id = event_of_choice(form.event, cutoff).id
        except outage_events.EventError as exc:
            errors.append(f"{exc}.")
    seeds = _split_seeds(form.seeds)
    top = max_concurrency(platform)
    concurrency = _int(form.concurrency, "Accounts fetched at once", errors, low=1, high=top)
    max_requests = None
    if form.max_requests.strip():
        max_requests = _int(form.max_requests, "Request limit", errors, low=1)
    errs = _int(form.max_consecutive_errors, "Errors in a row before stopping", errors, low=1)
    max_usd: float | None = None
    if platform == "x":
        try:
            max_usd = float(form.max_usd.strip())
            if not math.isfinite(max_usd) or max_usd < 0:
                raise ValueError
        except ValueError:
            errors.append("X scans need a spend cap in US dollars (for example 5).")
        if not form.confirm_billed:
            errors.append("X bills for discovery: tick the box to confirm the cost.")
    if errors:
        return None, errors
    return (
        ScanRequest(
            platform,
            n,
            cutoff,
            seeds,
            concurrency,
            max_requests,
            errs,
            max_usd,
            form.confirm_billed,
            event_id,
            form.keep_awake,
        ),
        [],
    )


def scan_argv(
    request: ScanRequest, *, out: Path, db: Path | None = None, hide_handles: bool = False
) -> list[str]:
    """The ``scan`` command line for ``request`` (no credential is ever part of it).

    A named event is passed as ``--event``; a picked date as ``--cutoff``, which the
    command makes a local-only custom event of.
    """
    argv = ["scan", "--platform", request.platform, "--n", str(request.n)]
    if request.event_id and not request.custom:
        argv += ["--event", request.event_id]
    else:
        argv += ["--cutoff", _cutoff_text(request.cutoff)]
    flag = "--subreddit" if request.platform in (*REDDIT_PLATFORMS, "fake") else "--query"
    for seed in request.seeds:
        argv += [flag, seed]
    if request.concurrency != 1:
        argv += ["--concurrency", str(request.concurrency)]
    if request.max_requests is not None:
        argv += ["--max-requests", str(request.max_requests)]
    if request.max_consecutive_errors != DEFAULT_MAX_CONSECUTIVE_ERRORS:
        argv += ["--max-consecutive-errors", str(request.max_consecutive_errors)]
    if request.max_usd is not None:
        argv += ["--max-usd", f"{request.max_usd:g}"]
    if request.confirm_billed and request.platform == "x":
        argv.append("--yes")
    if not request.keep_awake:
        argv.append("--allow-sleep")
    return [*argv, *_common(out, db, hide_handles)]


def resume_argv(
    platform: str,
    run_id: int,
    *,
    out: Path,
    db: Path | None = None,
    hide_handles: bool = False,
    event_id: str | None = None,
    keep_awake: bool = True,
) -> list[str]:
    """The command that continues ``run_id``, as the command line prints it: under
    the run's own event, from that event's store."""
    argv = ["scan", "--platform", platform, "--from-run", str(run_id)]
    if event_id:
        argv += ["--event", event_id]
    if not keep_awake:
        argv.append("--allow-sleep")
    return [*argv, *_common(out, db, hide_handles)]


def _common(out: Path, db: Path | None, hide_handles: bool) -> list[str]:
    argv = ["--out", str(out)]
    if db is not None:
        argv += ["--db", str(db)]
    if hide_handles:
        argv.append("--hide-handles")
    return argv


def output_path(folder: Path, *, now: datetime | None = None) -> Path:
    """A new CSV file name in ``folder`` for one scan."""
    stamp = ensure_utc(now or utcnow()).strftime("%Y%m%d-%H%M%S")
    return folder / f"bot-farm-silence-tracker-scan-{stamp}.csv"


#: Rough requests per sampled account (hydrate plus a page or two of history).
_REQUESTS_PER_ACCOUNT = {"reddit-archive": 3, "reddit": 3, "bluesky": 3, "x": 2}


def estimate_seconds(platform: str, n: int) -> float:
    """A rough duration for a scan of ``n`` accounts, from the platform's default rate.

    Discovery reads about one listing page per 100 pooled accounts; each sampled
    account then needs a few requests. Rate limits and slow servers make real
    scans longer.
    """
    if platform == "fake":
        return max(1.0, n / 500)
    try:
        cls = load_source_class(platform)
        rps = float(cls.default_rps) or 1.0
        pool = float(cls.default_pool_factor or 3)
    except Exception:
        rps, pool = 1.0, 3.0
    requests = math.ceil(pool * n / 100) + n * _REQUESTS_PER_ACCOUNT.get(platform, 3)
    return requests / rps


def describe_duration(seconds: float) -> str:
    """``seconds`` in words, rounded the way people say it."""
    if seconds < 60:
        return "under a minute"
    minutes = seconds / 60
    if minutes < 90:
        return f"about {max(1, round(minutes))} minutes"
    hours = minutes / 60
    if hours < 36:
        return f"about {hours:.1f} hours".replace(".0 ", " ")
    return f"about {hours / 24:.1f} days".replace(".0 ", " ")


def estimate_text(platform: str, n: int) -> str:
    return (
        f"Estimated time: {describe_duration(estimate_seconds(platform, n))} "
        "(a rough guess; rate limits can make it longer)."
    )


# -- Running a scan ------------------------------------------------------------


@dataclass(frozen=True)
class LogLine:
    text: str


@dataclass(frozen=True)
class Progress:
    done: int
    total: int


@dataclass(frozen=True)
class Finished:
    """The scan ended. ``run_id`` is the run it created (``None`` if it made none)."""

    code: int
    run_id: int | None
    run_status: str | None
    cancelled: bool
    message: str
    #: The outage event whose store holds ``run_id`` (``None``: the store given as
    #: ``db``), so the app shows that run even if the menu changed meanwhile.
    event_id: str | None = None


Event = LogLine | Progress | Finished

#: ``run_cli``'s signature, so tests can run a scan without the real command.
Runner = Callable[[Sequence[str], Callable[[str], None], ScanHooks], int]


def _default_runner(argv: Sequence[str], sink: Callable[[str], None], hooks: ScanHooks) -> int:
    from bot_farm_silence_tracker.gui.runner import run_cli

    return run_cli(argv, sink, hooks)


def list_runs(db: Path | None = None, event_id: str | None = None) -> list[dict[str, Any]]:
    """Stored runs of an event's store (see :func:`event_store`), newest first: id,
    platform, status, start time, sample size, cutoff, event and ``in_use``, whether
    a scan (in another window or process) holds the run's lock right now."""
    path = event_store(event_id, db)
    if not path.exists():
        return []
    with Storage(path) as storage:
        out = []
        for run_id in reversed(storage.run_ids()):
            run = storage.get_run(run_id) or {}
            summary = run.get("summary") or {}
            in_use = run.get("status") == "running" and storage.run_in_use(
                int(run.get("root_run_id") or run_id)
            )
            out.append(
                {
                    "run_id": run_id,
                    "platform": run.get("platform"),
                    "status": run.get("status"),
                    "started_at": run.get("started_at"),
                    "sampled": summary.get("sampled"),
                    "from_run": (run.get("params") or {}).get("from_run"),
                    "cutoff": _run_cutoff(run),
                    "event_id": _run_event_id(run),
                    "in_use": in_use,
                    # Where the run is stored, so a later action uses the same store.
                    "store": path,
                    "store_event": None if db is not None else (event_id or default_event().id),
                }
            )
    return out


def _run_cutoff(run: Mapping[str, Any]) -> datetime | None:
    """The cutoff a stored run was scanned with, or ``None`` when unreadable."""
    value = ((run.get("params") or {}).get("scoring") or {}).get("cutoff")
    try:
        return ensure_utc(parse_datetime(str(value))) if value else None
    except ValueError:
        return None


def _run_event_id(run: Mapping[str, Any]) -> str | None:
    """The outage event a stored run belongs to, or ``None`` when it cannot be told."""
    try:
        return outage_events.event_of_run(run.get("params") or {}, outage_events.load_events()).id
    except (outage_events.EventError, ValueError):
        return None


#: Run statuses ``scan --from-run`` can continue. A run left ``running`` was
#: stopped without being finished (the program was killed), or is still being
#: scanned elsewhere (``in_use`` in :func:`list_runs`), which resuming refuses.
RESUMABLE_STATUSES = frozenset({"partial", "failed", "cancelled", "sampled", "running"})

#: Shown for a run that a scan in another window (or process) still holds.
IN_USE_TEXT = "This scan is still running in another window."


def run_in_use(run: Mapping[str, Any]) -> bool:
    """Whether a scan holds ``run`` (a :func:`list_runs` row) right now, checked again."""
    store = run.get("store")
    if run.get("status") != "running" or not store or not Path(store).exists():
        return False
    try:
        with Storage(Path(store)) as storage:
            stored = storage.get_run(int(run["run_id"])) or {}
            return storage.run_in_use(int(stored.get("root_run_id") or run["run_id"]))
    except Exception:
        return False


def resumable_runs(db: Path | None = None, event_id: str | None = None) -> list[dict[str, Any]]:
    """Runs that stopped early and that no later run has continued yet."""
    runs = list_runs(db, event_id)
    continued = {r["from_run"] for r in runs if r.get("from_run") is not None}
    return [r for r in runs if r["status"] in RESUMABLE_STATUSES and r["run_id"] not in continued]


#: No request completed for longer than this many seconds shows the stall hint.
STALL_SECONDS = 120.0


def _span_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    return f"{minutes // 60} hours"


def stall_hint(idle_since: datetime | None, now: datetime) -> str | None:
    """The hint for a scan whose last request completed at ``idle_since`` (or that began
    then, before its first request), or ``None`` while it is making progress."""
    if idle_since is None:
        return None
    idle = (ensure_utc(now) - ensure_utc(idle_since)).total_seconds()
    if idle <= STALL_SECONDS:
        return None
    return (
        f"No progress for {_span_text(idle)} \u2014 the archive may be slow, or this "
        "computer may have been asleep."
    )


def last_request_text(last_request: datetime | None) -> str:
    """When the last request completed, in the computer's local time."""
    if last_request is None:
        return "No request has completed yet."
    return f"Last request completed at {ensure_utc(last_request).astimezone():%H:%M:%S}."


class ScanJob:
    """One scan on a worker thread, reporting through a queue.

    Only :meth:`drain` (on the UI thread) reads the queue; the worker never touches
    a widget. :meth:`cancel` stops the scan the same way Ctrl-C does on the command
    line: the shared HTTP client is cancelled, the pipeline saves every account
    processed so far, and the run can be continued with ``--from-run``.
    """

    def __init__(
        self,
        argv: Sequence[str],
        credentials: Mapping[str, str] | None = None,
        *,
        db: Path | None = None,
        runner: Runner | None = None,
        event_id: str | None = None,
    ) -> None:
        self.argv = list(argv)
        self._credentials = SecretDict(credentials or {})
        #: The store the scan writes to: ``db``, else the event's own.
        self._db = event_store(event_id, db)
        #: The event whose store holds the run (``None`` only with an explicit ``db``).
        self.event_id = event_id if event_id is not None or db is not None else default_event().id
        self._runner = runner or _default_runner
        self.events: queue.Queue[Event] = queue.Queue()
        self._client: HttpClient | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        #: When the scan started; the stall clock runs from it until the first request.
        self.started_at: datetime | None = None

    @property
    def last_request_at(self) -> datetime | None:
        """When the latest request completed (any HTTP status), or ``None`` yet."""
        with self._lock:
            client = self._client
        stamp = client.last_response_at if client is not None else None
        return None if stamp is None else datetime.fromtimestamp(stamp, UTC)

    def stall_hint(self, now: datetime | None = None) -> str | None:
        """The stall hint for this running scan, or ``None``."""
        if not self.running:
            return None
        return stall_hint(self.last_request_at or self.started_at, now or utcnow())

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("this scan was already started")
        self.started_at = utcnow()
        self._thread = threading.Thread(target=self._work, name="scan", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def cancel(self) -> None:
        """Ask the scan to stop. Safe to call from the UI thread, more than once."""
        self._cancel.set()
        with self._lock:
            client = self._client
        if client is not None:
            client.cancel()

    def drain(self, limit: int = 500) -> list[Event]:
        """Take up to ``limit`` waiting events (UI thread)."""
        out: list[Event] = []
        while len(out) < limit:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # -- worker thread --

    def _on_client(self, client: HttpClient) -> None:
        with self._lock:
            self._client = client
        if self._cancel.is_set():
            client.cancel()

    def _on_progress(self, done: int, total: int) -> None:
        """Report progress; once cancelled, stop before the next account is fetched.

        The pipeline calls this on the scan thread just before it loads each
        account, so raising here ends the scan between accounts, the way Ctrl-C
        does: the pipeline records the accounts finished so far, marks the run, and
        re-raises. (The HTTP client is cancelled too, which ends a wait or a
        request that is under way.)
        """
        self.events.put(Progress(done, total))
        if self._cancel.is_set():
            raise FetchCancelled("the scan was cancelled in the app")

    def _work(self) -> None:
        reported: list[int] = []
        hooks = ScanHooks(
            credentials=self._credentials,
            on_client=self._on_client,
            on_progress=self._on_progress,
            on_run=reported.append,
        )
        cancelled = False
        code = 1
        try:
            code = self._runner(self.argv, lambda line: self.events.put(LogLine(line)), hooks)
        except FetchCancelled:
            cancelled = True
        except Exception as exc:  # A bug or an unexpected error: report, do not crash.
            self.events.put(LogLine(f"error: {type(exc).__name__}: {exc}"))
            code = 1
        finally:
            self._credentials.clear()
            with self._lock:
                self._client = None
        cancelled = cancelled or (self._cancel.is_set() and code != 0)
        # The id the scan reported when it created its run (see ScanHooks.on_run).
        run_id = reported[-1] if reported else None
        status = _run_status(self._db, run_id)
        self.events.put(
            Finished(
                code,
                run_id,
                status,
                cancelled,
                finish_message(code, run_id, status, cancelled),
                self.event_id,
            )
        )


def _run_status(db: Path | None, run_id: int | None) -> str | None:
    if run_id is None:
        return None
    try:
        with Storage(db) as storage:
            run = storage.get_run(run_id)
    except Exception:
        return None
    return str(run["status"]) if run else None


def finish_message(code: int, run_id: int | None, status: str | None, cancelled: bool) -> str:
    """One plain sentence about how the scan ended."""
    if cancelled:
        if run_id is not None:
            return (
                f"Scan cancelled. Run {run_id} was saved with what was fetched so far; "
                "choose it under Resume to continue it later."
            )
        return "Scan cancelled before it started fetching."
    if code == 0 and status == "complete":
        return f"Scan finished (run {run_id}). Open Results to see the flagged accounts."
    if code == 0 and run_id is not None:
        return (
            f"Scan stopped early (run {run_id}, {status}). The results so far are saved; "
            "Resume continues it."
        )
    if run_id is not None:
        return f"The scan stopped with an error (run {run_id} is saved; see the log)."
    return "The scan could not start; see the log for the reason."


# -- Results -------------------------------------------------------------------


def signals_label(count: int) -> str:
    """How the app names an account's count of independent signals: ``signals: N``."""
    return f"signals: {count}"


def signal_count(result: AccountResult, outcome: ScanOutcome) -> int:
    """How many signals that do not depend on the outage's timing fired for a flagged
    account (:func:`~bot_farm_silence_tracker.registry.build.fired_signals`: regular
    timing, round-the-clock posting).

    A synchronized stop is not counted: a shared stop at the outage is what anyone it
    affected shows (see :mod:`bot_farm_silence_tracker.controls`). The count is not a
    confidence: the signals are not calibrated, and no count makes an account a bot.
    """
    return len(independent(fired_signals(result, outcome.context)))


_REDDIT_SUB = re.compile(r"[A-Za-z0-9_]{2,21}")
_RKEY = r"[A-Za-z0-9._:~-]{1,512}"
_AT_URI = re.compile(
    r"at://(did:(?:plc|web):[A-Za-z0-9._:%-]+)/(app\.bsky\.graph\.list|app\.bsky\.feed\.generator"
    rf"|app\.bsky\.graph\.starterpack)/({_RKEY})"
)
_BSKY_WEB = re.compile(
    r"https://bsky\.app/(?:profile/([^/\s]+)/(lists|feed)/(" + _RKEY + r")"
    r"|starter-pack/([^/\s]+)/(" + _RKEY + r"))/?"
)


def profile_url(platform: str, handle: str | None, user_id: str | None) -> str | None:
    """The public profile URL of an account, or ``None`` when no safe one exists.

    Built only from fixed templates (the registry's
    :func:`~bot_farm_silence_tracker.registry.evidence.profile_links`) filled with a
    name that passes the registry's syntax checks, never from a URL in fetched data.
    """
    handle = (handle or "").strip()
    user_id = (user_id or "").strip()
    if platform in REDDIT_PLATFORMS:
        name = handle or user_id
        if valid_username(platform, name):
            return profile_links(platform)[0].format(username=name).rstrip("/")
        return None
    if platform == "bluesky":
        for value in (handle, user_id):
            if value and (valid_username("bluesky", value) or valid_user_id("bluesky", value)):
                return BSKY_PROFILE_URL.format(id=value)
        return None
    if platform == "x":
        if valid_username("x", handle):
            return f"https://x.com/{handle}"
        if valid_user_id("x", user_id):
            return profile_links("x")[0].format(id=user_id)
        return None
    return None


def community_url(platform: str, community: str) -> str | None:
    """The public page of a community (subreddit, Bluesky list, feed or starter pack),
    built from fixed templates with validated parts, or ``None``."""
    text = community.strip()
    if platform in REDDIT_PLATFORMS:
        name = text.removeprefix("/").removeprefix("r/")
        return f"https://www.reddit.com/r/{name}" if _REDDIT_SUB.fullmatch(name) else None
    if platform == "bluesky":
        match = _AT_URI.fullmatch(text)
        if match:
            did, collection, rkey = match.groups()
            if not valid_user_id("bluesky", did):
                return None
            if collection == "app.bsky.graph.starterpack":
                return f"https://bsky.app/starter-pack/{did}/{rkey}"
            kind = "lists" if collection == "app.bsky.graph.list" else "feed"
            return f"https://bsky.app/profile/{did}/{kind}/{rkey}"
        match = _BSKY_WEB.fullmatch(text)
        if match:
            actor, kind, rkey, pack_actor, pack_rkey = match.groups()
            if actor is not None:
                ok = valid_username("bluesky", actor) or valid_user_id("bluesky", actor)
                return f"https://bsky.app/profile/{actor}/{kind}/{rkey}" if ok else None
            ok = valid_username("bluesky", pack_actor) or valid_user_id("bluesky", pack_actor)
            return f"https://bsky.app/starter-pack/{pack_actor}/{pack_rkey}" if ok else None
        return None
    return None


@dataclass(frozen=True)
class Timeline:
    """Daily post counts of one account, oldest day first."""

    start: datetime
    counts: tuple[int, ...]
    #: Index of the day that starts at (or contains) the cutoff.
    cutoff_index: int


def daily_counts(
    stamps: Iterable[datetime], start: datetime, cutoff: datetime, end: datetime
) -> Timeline:
    """Bucket ``stamps`` into UTC days from ``start`` up to ``end`` (exclusive)."""
    start = ensure_utc(start)
    days = max(1, math.ceil((ensure_utc(end) - start).total_seconds() / 86400))
    counts = [0] * days
    for ts in stamps:
        offset = (ensure_utc(ts) - start).total_seconds() / 86400
        if 0 <= offset < days:
            counts[int(offset)] += 1
    cut = int(max(0.0, (ensure_utc(cutoff) - start).total_seconds() / 86400))
    return Timeline(start, tuple(counts), min(cut, days - 1))


#: How many days after the cutoff a timeline shows (at most).
TIMELINE_DAYS_AFTER = 14


@dataclass(frozen=True)
class TimelineLine:
    """A vertical line of the silence timeline: the cutoff or one of the event's markers."""

    id: str
    label: str
    #: Days from the timeline's start (fractional, so a line sits at its hour).
    day: float
    #: Drawn dashed: the marker's time is only a guess.
    approximate: bool
    kind: str  # "cutoff" or "marker"


def timeline_lines(params: ScoringParams, start: datetime) -> list[TimelineLine]:
    """The timeline's lines for a run: the cutoff, then each marker in time order.

    Positions are days from ``start`` (the timeline's first day). A run without
    markers gets the cutoff line alone.
    """

    def day(when: datetime) -> float:
        return (ensure_utc(when) - ensure_utc(start)).total_seconds() / 86400

    lines = [
        TimelineLine(
            MAIN_OUTAGE,
            f"Main outage (cutoff) {with_local(params.cutoff)}",
            day(params.cutoff),
            False,
            "cutoff",
        )
    ]
    for marker in params.markers:
        lines.append(
            TimelineLine(
                marker.id,
                f"{marker_label(marker)} {with_local(marker.time)}",
                day(marker.time),
                marker.approximate,
                "marker",
            )
        )
    return lines


def event_details(event: outage_events.OutageEvent | None, params: ScoringParams) -> str:
    """What the Results screen's event tab shows: the event, its windows and markers.

    The windows and marker times are the ones the run was scored with; names and notes
    come from the events file. Marker times are as reported.
    """
    lines: list[str] = []
    if event is not None:
        lines += [f"Outage event: {event.name} ({event.id})"]
        if event.notes:
            lines += ["", event.notes]
    lines += [
        "",
        f"Cutoff (main outage): {with_local(params.cutoff)}",
        f"Baseline window: {format_cutoff(params.baseline_start)} to "
        f"{format_cutoff(params.baseline_end)}",
        f"Silence window: {format_cutoff(params.window_start)} to {format_cutoff(params.cutoff)}",
    ]
    if not params.markers:
        return "\n".join(lines).strip()
    lines += [
        "",
        "Markers (earlier moments inside this outage; times as reported, not "
        "independently verified). Each flagged account is labelled with the marker, or "
        "the main outage, its silence began nearest to.",
    ]
    for marker in params.markers:
        described = event.marker(marker.id) if event is not None else None
        lines += ["", f"{marker_label(marker)}: {with_local(marker.time)}"]
        if described is not None:
            if described.time_note:
                lines.append(f"  Time: {described.time_note}")
            if described.notes:
                lines.append(f"  {described.notes}")
            if described.sources:
                lines.append(f"  Sources: {', '.join(described.sources)}")
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class FlaggedAccount:
    """One flagged account as the Results screen shows it."""

    rank: int
    platform: str
    user_id: str
    handle: str
    #: What the app shows: the handle, or ``#rank`` with hidden handles.
    label: str
    score: float
    #: ``signals: N``: the count of signals independent of the outage's timing.
    signals: str
    cluster_id: int | None
    cluster_size: int
    last_activity: datetime | None
    posts_per_day: float
    provisional: bool
    communities: tuple[str, ...]
    profile_url: str | None
    timeline: Timeline
    #: Other outage events whose stores also sampled this account.
    also_in: tuple[str, ...] = ()
    #: Runs with markers: the marker (or main outage) the silence began nearest to,
    #: as the filter names it (``Name`` or ``Name (approx.)``), and with the offset.
    marker: str | None = None
    marker_text: str | None = None
    marker_id: str | None = None


@dataclass(frozen=True)
class Community:
    name: str
    platform: str
    accounts: int
    url: str | None


@dataclass(frozen=True)
class Cluster:
    cluster_id: int
    size: int
    members: tuple[str, ...]
    first_stop: datetime | None
    last_stop: datetime | None
    #: The cutoff the group was found with, and how long before it the group stopped.
    cutoff: datetime
    #: Runs with markers: the marker nearest to the group's first stop.
    marker: str | None = None

    @property
    def hours_before_cutoff(self) -> float | None:
        if self.last_stop is None:
            return None
        return (ensure_utc(self.cutoff) - ensure_utc(self.last_stop)).total_seconds() / 3600


@dataclass
class Results:
    run_id: int
    platform: str
    status: str
    accounts: list[FlaggedAccount]
    communities: dict[str, Community]
    clusters: list[Cluster]
    summary: list[tuple[str, str]]
    notices: list[str]
    hide_handles: bool
    cutoff: datetime
    #: The outage event of the run.
    event_id: str = ""
    #: The silence timeline's vertical lines (the cutoff and the markers).
    lines: list[TimelineLine] = field(default_factory=list)
    #: The event tab's text: the event, its windows and its marker notes.
    details: str = ""
    #: The placebo comparison's headline, for the header of the Results screen.
    placebo: str = ""


def _communities_of(
    meta: Mapping[str, Any], activity_communities: Iterable[str | None]
) -> tuple[str, ...]:
    seen: list[str] = []
    for key in ("community", "discovered_in"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            seen.append(value.strip())
    seen += [c.strip() for c in activity_communities if isinstance(c, str) and c.strip()]
    return tuple(dict.fromkeys(seen))


def load_results(
    run_id: int,
    *,
    db: Path | None = None,
    hide_handles: bool = False,
    event_id: str | None = None,
) -> Results:
    """Re-score ``run_id`` from the event's store (like ``report --run-id``; no
    network) and gather what the Results screen shows. Raises
    ``LookupError``/``ValueError`` like ``report`` does for a run that cannot be read.
    Each flagged account says which other events' stores also hold it."""
    store = event_store(event_id, db)
    with Storage(store) as storage:
        outcome = rescore_run(storage, run_id)
        run = storage.get_run(run_id) or {}
        sample = {acct.user_id: acct for acct, _ in storage.get_run_sample(run_id)}
        params = outcome.params
        start = params.baseline_start
        end = min(
            ensure_utc(params.effective_now),
            ensure_utc(params.cutoff) + timedelta(days=TIMELINE_DAYS_AFTER),
        )
        candidates = [r for r in outcome.candidates if r.status != "opted_out"]
        redactor = _Redactor() if hide_handles else None
        anchors = {a.id: a for a in params.anchors}
        accounts: list[FlaggedAccount] = []
        for rank, result in enumerate(candidates, 1):
            activities = storage.get_activities(result.platform, result.user_id, start, end)
            account = sample.get(result.user_id)
            meta = account.meta if account is not None else {}
            communities = _communities_of(meta, (a.community for a in activities))
            if redactor is not None:
                redactor.add(result.handle, f"#{rank}")
                redactor.add(result.user_id, f"#{rank}")
            accounts.append(
                FlaggedAccount(
                    rank=rank,
                    platform=result.platform,
                    user_id=result.user_id,
                    handle=result.handle,
                    label=f"#{rank}" if hide_handles else result.handle,
                    score=result.score,
                    signals=signals_label(signal_count(result, outcome)),
                    cluster_id=result.cluster_id,
                    cluster_size=result.cluster_size,
                    last_activity=result.last_activity,
                    posts_per_day=result.posts_per_day,
                    provisional=result.provisional,
                    communities=communities,
                    profile_url=None
                    if hide_handles
                    else profile_url(result.platform, result.handle, result.user_id),
                    timeline=daily_counts((a.ts for a in activities), start, params.cutoff, end),
                    **_marker_fields(result, anchors),
                )
            )
    own_event = _run_event_id(run) or event_id or default_event().id
    try:
        own = outage_events.event_of_run(run.get("params") or {}, outage_events.load_events())
    except (outage_events.EventError, ValueError):
        own = None
    # With an explicit database (the self-test, say) the user's own stores are not read.
    also = also_in_events(accounts, own_event, exclude=store) if db is None else {}
    accounts = [
        dataclasses_replace(a, also_in=tuple(also.get((a.platform, a.user_id), ())))
        for a in accounts
    ]
    if redactor is not None:
        accounts = hide_community_names(accounts, redactor)
    platform = str(run.get("platform") or (accounts[0].platform if accounts else "unknown"))
    return Results(
        run_id=run_id,
        platform=platform,
        status=outcome.status,
        accounts=accounts,
        communities=_community_index(accounts, platform, hide_handles),
        clusters=_clusters(accounts, params.cutoff, outcome.results, anchors),
        summary=summary_rows(outcome),
        notices=_notices(outcome),
        hide_handles=hide_handles,
        cutoff=ensure_utc(params.cutoff),
        event_id=own_event,
        lines=timeline_lines(params, start),
        details=event_details(own, params),
        placebo=headline(outcome.placebo),
    )


def _marker_fields(result: AccountResult, anchors: Mapping[str, Marker]) -> dict[str, Any]:
    """The ``marker`` and ``marker_text`` of a flagged account (none without markers)."""
    marker = anchors.get(result.nearest_marker or "")
    if marker is None:
        return {}
    return {
        "marker": marker_label(marker),
        "marker_text": marker_label(marker, result.nearest_marker_offset_hours),
        "marker_id": marker.id,
    }


def event_stores() -> dict[str, Path]:
    """Every event's store that exists in the cache folder: the events of
    ``events.toml`` and the custom events scanned on this computer."""
    root = cache_dir() / EVENTS_DIR
    out: dict[str, Path] = {}
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for folder in folders:
        if not outage_events.valid_event_id(folder.name):
            continue
        path = folder / "bot-farm-silence-tracker.db"
        if path.is_file():
            out[folder.name] = path
    return out


def also_in_events(
    accounts: Sequence[FlaggedAccount], event_id: str, *, exclude: Path | None = None
) -> dict[tuple[str, str], list[str]]:
    """Which other events' stores sampled each account: ``{(platform, user_id):
    [event ids]}``. Read-only; an unreadable store is skipped."""
    wanted = {(a.platform, a.user_id) for a in accounts}
    found: dict[tuple[str, str], list[str]] = {}
    if not wanted:
        return found
    skip = exclude.resolve() if exclude is not None else None
    for other, path in event_stores().items():
        if other == event_id or (skip is not None and path.resolve() == skip):
            continue
        try:
            with Storage(path, read_only=True) as storage:
                rows = storage._conn.execute(
                    "SELECT DISTINCT platform, user_id FROM run_sample"
                ).fetchall()
        except Exception:
            continue
        for row in rows:
            key = (str(row[0]), str(row[1]))
            if key in wanted:
                found.setdefault(key, []).append(other)
    return found


#: Text that can name a Bluesky list's, feed's or starter pack's curator.
_BSKY_IDENTITY = re.compile(r"at://|bsky\.app/|did:[a-z0-9]+:", re.IGNORECASE)


def _bluesky_kind(name: str) -> str:
    lowered = name.lower()
    if "starterpack" in lowered or "starter-pack" in lowered:
        return "Bluesky starter pack"
    if "feed" in lowered:
        return "Bluesky feed"
    if "graph.list" in lowered or "/lists/" in lowered:
        return "Bluesky list"
    return "Bluesky community"


def hide_community_names(
    accounts: Sequence[FlaggedAccount], redactor: Callable[[str], str]
) -> list[FlaggedAccount]:
    """Hide what community names reveal when account names are hidden.

    A Bluesky list, feed or starter pack is named by an AT-URI or a bsky.app URL,
    which holds its curator's DID or handle. Each such community becomes a numbered
    label ("Bluesky list 1"), the same one everywhere it appears (the "Seen in"
    column, the filter and the map), numbered by the sorted original names so the
    result does not depend on the order of the accounts. Any other name goes through
    ``redactor``, which hides the names of known accounts.
    """
    names = sorted({c for a in accounts for c in a.communities})
    counters: Counter[str] = Counter()
    mapping: dict[str, str] = {}
    for name in names:
        if _BSKY_IDENTITY.search(name):
            kind = _bluesky_kind(name)
            counters[kind] += 1
            mapping[name] = f"{kind} {counters[kind]}"
        else:
            mapping[name] = redactor(name)
    return [_replace_communities(a, tuple(mapping[c] for c in a.communities)) for a in accounts]


def _replace_communities(account: FlaggedAccount, communities: tuple[str, ...]) -> FlaggedAccount:
    from dataclasses import replace

    return replace(account, communities=communities)


def _community_index(
    accounts: Sequence[FlaggedAccount], platform: str, hide_handles: bool
) -> dict[str, Community]:
    counts: Counter[str] = Counter()
    for account in accounts:
        counts.update(set(account.communities))
    out: dict[str, Community] = {}
    for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        url = community_url(platform, name)
        if hide_handles and url is not None and platform == "bluesky":
            url = None  # A list's or feed's address names its curator.
        out[name] = Community(name, platform, count, url)
    return out


def _clusters(
    accounts: Sequence[FlaggedAccount],
    cutoff: datetime,
    results: Iterable[AccountResult] = (),
    anchors: Mapping[str, Marker] | None = None,
) -> list[Cluster]:
    groups: dict[int, list[FlaggedAccount]] = {}
    for account in accounts:
        if account.cluster_id is not None:
            groups.setdefault(account.cluster_id, []).append(account)
    nearest = {
        r.cluster_id: r.cluster_nearest_marker
        for r in results
        if r.cluster_id is not None and r.cluster_nearest_marker
    }
    out = []
    for cid, members in sorted(groups.items()):
        stops = [m.last_activity for m in members if m.last_activity is not None]
        marker = (anchors or {}).get(nearest.get(cid) or "")
        out.append(
            Cluster(
                cid,
                len(members),
                tuple(m.label for m in members),
                min(stops) if stops else None,
                max(stops) if stops else None,
                ensure_utc(cutoff),
                marker_label(marker) if marker is not None else None,
            )
        )
    return out


def summary_rows(outcome: ScanOutcome) -> list[tuple[str, str]]:
    """The main counts of the command line's summary table."""
    s = outcome.summary
    rows = [
        ("Accounts sampled", s.get("sampled", 0)),
        ("Accounts scored", s.get("scored", 0)),
        ("Regular posters before the cutoff (eligible)", s.get("eligible", 0)),
        ("Flagged: went silent at the cutoff", s.get("cliff", 0)),
        ("  of which provisional", s.get("provisional", 0)),
        (
            "  with a signal independent of the outage's timing",
            sum(1 for r in outcome.results if r.cliff and r.independent_signals),
        ),
        ("Groups that stopped at the same time (clusters)", s.get("clusters", 0)),
        ("  largest group", s.get("largest_cluster", 0)),
        (
            "Unavailable (suspended / deleted / private)",
            f"{s.get('suspended', 0)} / {s.get('deleted', 0)} / {s.get('private', 0)}",
        ),
        ("Fetch errors", s.get("errors", 0)),
    ]
    return [(label, str(value)) for label, value in rows]


def _notices(outcome: ScanOutcome) -> list[str]:
    notes = [headline(outcome.placebo), NOT_PROOF, NOTICE, OUTAGE_NOTICE]
    if outcome.params.provisional:
        notes.append(PROVISIONAL_NOTICE)
    note = truncated_notice(outcome)
    if note:
        notes.append(note)
    return notes


# -- Filters and the account <-> community map ----------------------------------

ALL = "All"


@dataclass(frozen=True)
class Filters:
    platform: str = ALL
    community: str = ALL
    cluster: str = ALL  # ALL, "none" or a cluster id as text
    #: ``signals: N`` (see :func:`signals_label`).
    signals: str = ALL
    #: The nearest marker, as :attr:`FlaggedAccount.marker` names it.
    marker: str = ALL


def filter_accounts(accounts: Iterable[FlaggedAccount], filters: Filters) -> list[FlaggedAccount]:
    out = []
    for a in accounts:
        if filters.platform != ALL and a.platform != filters.platform:
            continue
        if filters.community != ALL and filters.community not in a.communities:
            continue
        if filters.cluster != ALL:
            want = None if filters.cluster == "none" else filters.cluster
            if (str(a.cluster_id) if a.cluster_id is not None else None) != want:
                continue
        if filters.signals != ALL and a.signals != filters.signals:
            continue
        if filters.marker != ALL and a.marker != filters.marker:
            continue
        out.append(a)
    return out


def filter_choices(results: Results) -> dict[str, list[str]]:
    """Values each filter menu offers."""
    platforms = sorted({a.platform for a in results.accounts})
    clusters = [str(c.cluster_id) for c in results.clusters]
    has_unclustered = any(a.cluster_id is None for a in results.accounts)
    return {
        "platform": [ALL, *platforms],
        "community": [ALL, *results.communities],
        "cluster": [ALL, *clusters, *(["none"] if has_unclustered else [])],
        "signals": [ALL, *sorted({a.signals for a in results.accounts})],
        # In the timeline's order (the cutoff, then the markers), only those in use.
        "marker": [ALL, *dict.fromkeys(_marker_order(results))],
    }


def _marker_order(results: Results) -> list[str]:
    """The markers the flagged accounts are labelled with, in the timeline's order."""
    order = {line.id: index for index, line in enumerate(results.lines)}
    seen: dict[str, int] = {}
    for account in results.accounts:
        if account.marker and account.marker_id:
            seen[account.marker] = order.get(account.marker_id, len(order))
    return sorted(seen, key=lambda name: (seen[name], name))


@dataclass(frozen=True)
class GraphNode:
    key: str
    label: str
    kind: str  # "account" or "community"
    size: int
    #: The account's stop-time cluster (accounts only), for its color.
    cluster_id: int | None = None


@dataclass(frozen=True)
class Graph:
    accounts: list[GraphNode]
    communities: list[GraphNode]
    #: ``(account key, community key)`` pairs.
    edges: list[tuple[str, str]]


def account_key(account: FlaggedAccount) -> str:
    return f"{account.platform}:{account.user_id}"


def build_graph(results: Results, filters: Filters | None = None) -> Graph:
    """The bipartite account <-> community map for the filtered accounts.

    A community's size is the number of filtered flagged accounts seen in it. The
    community filter itself does not hide other communities' nodes of the
    remaining accounts, so the map shows where those accounts also appear.
    """
    shown = filter_accounts(results.accounts, filters or Filters())
    counts: Counter[str] = Counter()
    edges: list[tuple[str, str]] = []
    for account in shown:
        for community in account.communities:
            counts[community] += 1
            edges.append((account_key(account), community))
    accounts = [GraphNode(account_key(a), a.label, "account", 1, a.cluster_id) for a in shown]
    communities = [
        GraphNode(name, name, "community", count)
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    ]
    return Graph(accounts, communities, edges)


#: Distinct colors for stop-time clusters, readable on light and dark backgrounds.
CLUSTER_COLORS = (
    "#e6550d",
    "#3182bd",
    "#31a354",
    "#756bb1",
    "#d6616b",
    "#8c6d31",
    "#17becf",
    "#bcbd22",
)


def cluster_color(cluster_id: int | None, clusters: Sequence[Cluster]) -> str | None:
    """The color of ``cluster_id``'s group in both views, or ``None`` when unclustered."""
    if cluster_id is None:
        return None
    ids = [c.cluster_id for c in clusters]
    if cluster_id not in ids:
        return None
    return CLUSTER_COLORS[ids.index(cluster_id) % len(CLUSTER_COLORS)]


# -- Export, folders, links ------------------------------------------------------


#: Shown before the export writes anything.
EXPORT_WARNING = (
    "Before you export: the CSV holds the real account names and IDs, even while "
    "Hide account names is on. Keep the file private and never commit or post it. "
    "Press the button again to choose where to save it, or Cancel."
)


def export_csv(
    run_id: int, path: Path, *, db: Path | None = None, event_id: str | None = None
) -> int:
    """Write the run like ``export-ids --run-id N --out PATH.csv``. Returns the row count.

    Refuses a path inside a git repository, as the command does without
    ``--allow-inside-repo``: the file holds real account names.
    """
    from bot_farm_silence_tracker.export import export_rows, write_export

    top = git_toplevel(path)
    if top is not None:
        raise ValueError(
            f"{path} is inside the git repository {top}. The file holds real account "
            "names and must never be committed; choose a folder outside it."
        )
    with Storage(event_store(event_id, db)) as storage:
        if storage.get_run(run_id) is None:
            raise LookupError(f"no run with id {run_id}")
        rows = export_rows(storage, [run_id])
    write_export(path, rows, "csv")
    return len(rows)


def open_command(path: Path, platform: str = sys.platform) -> list[str] | None:
    """The command that shows ``path`` in the system file manager (``None``: Windows,
    which uses :func:`os.startfile`)."""
    if platform.startswith("win"):
        return None
    if platform == "darwin":
        return ["open", str(path)]
    return ["xdg-open", str(path)]


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    command = open_command(path)
    if command is None:
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_link(url: str | None, opener: Callable[[str], Any] = webbrowser.open) -> bool:
    """Open a link this module built (never one taken from fetched data)."""
    if not url or not url.startswith("https://"):
        return False
    opener(url)
    return True


# -- Network and privacy ---------------------------------------------------------


def network_text() -> str:
    """The ``network`` command's report, plus the note about browser links."""
    return format_network(network_report()) + "\n\n" + BROWSER_NOTE


def run_label(run: Mapping[str, Any]) -> str:
    started = run.get("started_at")
    try:
        when = isoformat(parse_datetime(str(started)))[:16].replace("T", " ")
    except ValueError:
        when = str(started)
    size = f", {run['sampled']} accounts" if run.get("sampled") else ""
    cut = f", cutoff {format_cutoff(run['cutoff'])}" if run.get("cutoff") else ""
    busy = " (still running in another window)" if run.get("in_use") else ""
    return (
        f"Run {run['run_id']}: {run['platform']}, {run['status']}{cut}, started {when} UTC"
        f"{size}{busy}"
    )


@dataclass
class AppState:
    """What the app remembers between screens (never saved)."""

    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    db: Path | None = None
    #: The outage event whose store the Results and Resume screens read (``None``:
    #: the default event). The New scan screen sets it.
    event_id: str | None = None
    hide_handles: bool = False
    credentials: SessionCredentials = field(default_factory=SessionCredentials)
    last_run_id: int | None = None
    job: ScanJob | None = None
