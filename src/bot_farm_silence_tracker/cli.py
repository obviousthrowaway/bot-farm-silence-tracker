"""Command-line interface: ``bot-farm-silence-tracker scan``, ``report``, ``events`` and more.

Every scan belongs to an outage event (:mod:`bot_farm_silence_tracker.events`), which
gives its cutoff and windows and picks its cache database
(:func:`bot_farm_silence_tracker.storage.event_db_path`). ``--event`` chooses one;
``--cutoff`` makes a local-only custom event.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import shlex
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker import config as user_config
from bot_farm_silence_tracker import events as outage_events
from bot_farm_silence_tracker.controls import (
    DEFAULT_PLACEBO_WEEKS,
    MAX_PLACEBO_WEEKS,
    MIN_PLACEBO_WEEKS,
)
from bot_farm_silence_tracker.keepawake import keep_awake
from bot_farm_silence_tracker.ledger import SpendLedger
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.network import format_network, network_report
from bot_farm_silence_tracker.output import (
    NOTICE,
    OUTAGE_NOTICE,
    PROVISIONAL_NOTICE,
    estimate_lines,
    placebo_notice,
    truncated_notice,
    write_csv,
    write_json,
)
from bot_farm_silence_tracker.pipeline import (
    SECRET_OPTION_PATTERN,  # noqa: F401 - re-exported for callers of the old name
    ScanOutcome,
    ScanSettings,
    format_request_summary,
    has_url_userinfo,
    is_secret_option,
    load_run_sample,
    rescore_run,
    run_scan,
    storable_options,
)
from bot_farm_silence_tracker.sampling import (
    DEFAULT_POOL_FACTOR,
    DEFAULT_SAMPLE_SIZE,
    random_seed,
)
from bot_farm_silence_tracker.scoring import Marker, ScoringParams, marker_label
from bot_farm_silence_tracker.secretmap import SecretDict, secret_dict
from bot_farm_silence_tracker.seeds import REDDIT_PLATFORMS, SEED_KEYS, SeedSet, load_seeds
from bot_farm_silence_tracker.sources.base import (
    ENV_PREFIX,
    Source,
    SourceAuthError,
    SourceConfig,
    SourceError,
)
from bot_farm_silence_tracker.sources.http import HttpClient, certifi_ssl_context, redact_userinfo
from bot_farm_silence_tracker.sources.registry import (
    AdapterNotAvailable,
    MissingCredentials,
    collect_credentials,
    load_source_class,
)
from bot_farm_silence_tracker.storage import (
    RunInUse,
    Storage,
    event_db_path,
    prepare_event_db,
)
from bot_farm_silence_tracker.timeutil import isoformat, parse_datetime, utcnow

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    # Never print local variables in tracebacks: they include credentials.
    pretty_exceptions_show_locals=False,
    help=f"{NOTICE}\n\nSample accounts from seed communities and flag steady posters "
    "that went silent at an outage event's cutoff (see `events list`).",
)

DbOption = Annotated[
    Path | None,
    typer.Option(
        "--db",
        help="SQLite cache path [default: the event's own database, "
        "<cache>/events/<event-id>/bot-farm-silence-tracker.db].",
    ),
]
EventOption = Annotated[
    str | None,
    typer.Option(
        "--event",
        "-e",
        help="Outage event id (see `events list`) [default: default_event in the config "
        "file, else the default event of events.toml].",
    ),
]
OutOption = Annotated[
    Path | None,
    typer.Option("--out", help="Output file [default: bot-farm-silence-tracker-run-<id>.<fmt>]."),
]
FormatOption = Annotated[
    str | None,
    typer.Option("--format", help="csv or json [default: from --out extension, else csv]."),
]


#: What ``--hide-handles`` puts in place of an account name that has no row number.
HIDDEN = "[hidden]"


class _Redactor:
    """Hides account names in console text, for ``--hide-handles``.

    Known names (the handles and IDs of the accounts a scan discovered, sampled or
    scored) are replaced by the account's row number in the top-candidates table
    (``#1``, ``#2``, ...) when it has one, else by ``[hidden]``. Whatever the names,
    DIDs, the owner part of AT-URIs and the handle in bsky.app profile and
    starter-pack URLs (a seed list's curator, say) are replaced by ``[hidden]`` too.
    Output files are never redacted.
    """

    _DID = re.compile(r"did:[a-z0-9]+:[A-Za-z0-9._:%-]*[A-Za-z0-9]")
    _AT_OWNER = re.compile(r"(at://)([^/\s'\"]+)")
    _BSKY_ACTOR = re.compile(r"(bsky\.app/(?:profile|starter-pack)/)([^/\s'\"]+)")

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._pattern: re.Pattern[str] | None = None

    def add(self, name: str | None, label: str = HIDDEN) -> None:
        """Hide ``name`` from now on, as ``label`` (a row number wins over ``[hidden]``)."""
        if not name or (label == HIDDEN and name in self._names):
            return
        if self._names.get(name) != label:
            self._names[name] = label
            self._pattern = None

    def add_account(self, account: Account, label: str = HIDDEN) -> None:
        self.add(account.handle, label)
        self.add(account.user_id, label)

    def __call__(self, text: str) -> str:
        if self._names:
            if self._pattern is None:
                names = sorted(self._names, key=len, reverse=True)
                self._pattern = re.compile(
                    r"(?<![\w.-])(" + "|".join(map(re.escape, names)) + r")(?![\w-]|\.\w)"
                )
            text = self._pattern.sub(lambda m: self._names[m.group(1)], text)
        text = self._AT_OWNER.sub(rf"\g<1>{HIDDEN}", text)
        text = self._BSKY_ACTOR.sub(rf"\g<1>{HIDDEN}", text)
        return self._DID.sub(HIDDEN, text)


#: The redactor of the running command, when ``--hide-handles`` is set. The app
#: callback clears it before every command.
_REDACTOR: list[_Redactor] = []


def _hide(text: str) -> str:
    """``text`` with account names hidden when ``--hide-handles`` is set."""
    return _REDACTOR[0](text) if _REDACTOR else text


def _start_hiding(hide_handles: bool) -> _Redactor | None:
    _REDACTOR.clear()
    if not hide_handles:
        return None
    redactor = _Redactor()
    _REDACTOR.append(redactor)
    return redactor


def _watch_accounts(source: Source, redactor: _Redactor) -> None:
    """Teach ``redactor`` every account ``source`` discovers, hydrates or fetches.

    Live log lines (a fatal stop logs its warnings) are then redacted before the
    scan's outcome, which names every scored account, is known.
    """
    discover, hydrate, fetch = source.discover, source.hydrate, source.fetch_activity

    def watched_discover(seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        for account in discover(seeds, pool_limit):
            redactor.add_account(account)
            yield account

    def watched_hydrate(account: Account) -> Account:
        redactor.add_account(account)
        result = hydrate(account)
        redactor.add_account(result)
        return result

    def watched_fetch(account: Account, *args: Any, **kwargs: Any) -> Any:
        redactor.add_account(account)
        return fetch(account, *args, **kwargs)

    source.discover = watched_discover  # type: ignore[method-assign]
    source.hydrate = watched_hydrate  # type: ignore[method-assign]
    source.fetch_activity = watched_fetch  # type: ignore[method-assign]


def _learn_outcome(redactor: _Redactor | None, outcome: ScanOutcome) -> None:
    """Teach ``redactor`` the outcome's accounts, top candidates by row number."""
    if redactor is None:
        return
    for rank, result in enumerate(outcome.candidates[:10], 1):
        redactor.add(result.handle, f"#{rank}")
        redactor.add(result.user_id, f"#{rank}")
    for result in outcome.results:
        redactor.add(result.handle)
        redactor.add(result.user_id)


@dataclasses.dataclass
class ScanHooks:
    """Callbacks for running ``scan`` in-process, as the desktop app does.

    ``credentials`` are values keyed by environment variable name, used in place of
    (or on top of) the environment when the adapter's credentials are collected.
    They are only read, never stored or printed: they are held in a
    :class:`~bot_farm_silence_tracker.secretmap.SecretDict`, and ``repr`` of the hooks
    omits them, so no log line or traceback can show a value. ``on_client`` receives the scan's
    shared HTTP client once it exists, so the caller can cancel it from another
    thread (the same clean stop as Ctrl-C: the run is saved and ``--from-run``
    continues it). ``on_progress`` receives every ``(done, total)`` fetch update.
    ``on_run`` receives the id of the run the scan creates, as soon as it exists.
    """

    credentials: Mapping[str, str] = dataclasses.field(default_factory=SecretDict, repr=False)
    on_client: Callable[[HttpClient], None] | None = None
    on_progress: Callable[[int, int], None] | None = None
    on_run: Callable[[int], None] | None = None

    def __post_init__(self) -> None:
        self.credentials = secret_dict(self.credentials)

    def __repr__(self) -> str:
        names = ", ".join(sorted(self.credentials)) or "none"
        return (
            f"ScanHooks(credentials=<{names}>, on_client={self.on_client is not None}, "
            f"on_progress={self.on_progress is not None}, on_run={self.on_run is not None})"
        )

    __str__ = __repr__


_HOOKS = threading.local()


@contextlib.contextmanager
def scan_hooks(hooks: ScanHooks) -> Iterator[ScanHooks]:
    """Use ``hooks`` for every ``scan`` this thread runs inside the block."""
    previous = getattr(_HOOKS, "value", None)
    _HOOKS.value = hooks
    try:
        yield hooks
    finally:
        _HOOKS.value = previous


def _current_hooks() -> ScanHooks | None:
    return getattr(_HOOKS, "value", None)


def _credential_environ(hooks: ScanHooks | None) -> Mapping[str, str] | None:
    """The environment to read credentials from: the real one, plus session values."""
    if hooks is None or not hooks.credentials:
        return None
    given = {name: value for name, value in hooks.credentials.items() if value.strip()}
    return secret_dict({**os.environ, **given})


def _print_warnings(warnings: Iterable[str]) -> None:
    for warning in warnings:
        typer.secho(_hide(f"warning: {warning}"), fg=typer.colors.YELLOW, err=True)


class _EchoHandler(logging.Handler):
    """Logging handler that writes through ``typer.echo`` to the current stderr."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            typer.echo(_hide(self.format(record)), err=True)
        except Exception:  # pragma: no cover - logging must never break a scan
            self.handleError(record)


def _setup_logging() -> None:
    """Show Bot Farm Silence Tracker INFO logs (such as long rate-limit waits) on stderr."""
    logger = logging.getLogger("bot_farm_silence_tracker")
    if not any(isinstance(h, _EchoHandler) for h in logger.handlers):
        handler = _EchoHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def parse_source_options(items: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``--source-option key=value`` flags. The last value of a key wins."""
    options: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"--source-option must look like key=value, got {item!r}")
        options[key] = value.strip()
    return options


def _print_cost(source: Source) -> None:
    """Print the adapter's spend line. Runs on every exit path, so it never raises."""
    try:
        line = source.cost_summary()
    except Exception as exc:  # pragma: no cover - defensive
        line = f"(could not compute the cost summary: {exc})"
    if line:
        typer.echo(_hide(line), err=True)


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.secho(_hide(f"error: {message}"), fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _parse_when(text: str, option: str) -> object:
    try:
        return parse_datetime(text)
    except ValueError:
        raise _fail(f"{option} must be YYYY-MM-DD or an ISO-8601 timestamp, got {text!r}") from None


def _resolve_format(fmt: str | None, out: Path | None) -> str:
    if fmt is None:
        return "json" if out is not None and out.suffix.lower() == ".json" else "csv"
    fmt = fmt.lower()
    if fmt not in ("csv", "json"):
        raise _fail("--format must be csv or json")
    return fmt


def stored_active_within(params: dict[str, Any]) -> float | None:
    """The ``--active-within-days`` a stored run used, or ``None`` (absent or invalid)."""
    value = params.get("active_within_days")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def _discovery_line(days: float, from_run: Any = None) -> str:
    """One line saying the sample came from the ``--active-within-days`` window."""
    origin = f" (inherited from run {from_run})" if from_run is not None else ""
    return (
        f"Discovery: --active-within-days {days:g}{origin}: authors active in the last "
        f"{days:g} days before the effective now, clipped to the baseline window before "
        "the cutoff."
    )


HideHandlesOption = Annotated[
    bool,
    typer.Option(
        "--hide-handles",
        help="Hide account names on the console: row numbers in the top-candidates table, "
        "and row numbers or [hidden] in warnings and errors (output files are unchanged).",
    ),
]


def _print_summary(outcome: ScanOutcome, platform: str, *, hide_handles: bool = False) -> None:
    s = outcome.summary
    sampling = s.get("sampling")
    table = Table(
        title=f"Bot Farm Silence Tracker run {outcome.run_id} ({platform}, {outcome.status})"
    )
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    eligible_row = (
        ("Eligible, fully fetched (unweighted; see the estimates)", s.get("eligible_observed"))
        if sampling
        else ("Eligible (regular in baseline)", s["eligible"])
    )
    cliff_label = "Cliff accounts"
    if sampling and sampling.get("candidates_incomplete"):
        cliff_label = "Cliff accounts (lower bound: candidates not fully fetched)"
    elif sampling and not sampling.get("cliffs_exact", True):
        cliff_label = "Cliff accounts (observed, unweighted; see the estimates)"
    rows = [
        ("Sampled", s["sampled"]),
        ("Scored", s["scored"]),
        ("Skipped (not fetched)", s.get("skipped", 0)),
        (
            "Unavailable (suspended / deleted / private)",
            f"{s['suspended']} / {s['deleted']} / {s['private']}",
        ),
        ("Fetch errors", s.get("errors", 0)),
        ("Truncated histories (possible false negatives)", s.get("truncated", 0)),
        ("Scored against an earlier data end (see data_end)", s.get("early_data_end", 0)),
        eligible_row,
        (cliff_label, s["cliff"]),
        ("  of which provisional", s["provisional"]),
        (
            "  with a signal independent of the outage's timing",
            sum(1 for r in outcome.results if r.cliff and r.independent_signals),
        ),
        ("Synchronized clusters", s["clusters"]),
        ("  largest cluster", s["largest_cluster"]),
        (
            "  significant (adjusted permutation p-value <= 0.01)",
            sum(1 for t in s.get("cluster_tests") or [] if t.get("p_adjusted", 1) <= 0.01),
        ),
    ]
    # Adapter-specific rows (Bluesky), shown only when the summary carries them.
    extra: list[tuple[str, Any]] = []
    if "deactivated" in s:
        extra.append(("  deleted, of which deactivated", s["deactivated"]))
    if "opted_out" in s or "dropped_opted_out" in s:
        extra.append(
            (
                "Opted out, counted only (sampled / dropped)",
                f"{s.get('opted_out', 0)} / {s.get('dropped_opted_out', 0)}",
            )
        )
    rows[4:4] = extra
    if "cliff_reposted_after_cutoff" in s:
        at = next(i for i, row in enumerate(rows) if row[0] == "  of which provisional")
        rows.insert(
            at + 1, ("  of which reposted after the cutoff", s["cliff_reposted_after_cutoff"])
        )
    if sampling:
        rows[2:2] = [
            ("Pre-checked only (not scored, see stratum)", sampling.get("precheck_only", 0)),
            ("Waiting for the phase-2 fetch", sampling.get("phase2_pending", 0)),
            ("Phase-2 fetch failed (nonresponse)", sampling.get("phase2_failed", 0)),
            ("Fetched past the stop point (not in the estimates)", sampling.get("after_stop", 0)),
        ]
    nearest = s.get("cliff_nearest_marker")
    if isinstance(nearest, dict) and nearest:
        names = {a.id: a for a in outcome.params.anchors}
        at = next(i for i, row in enumerate(rows) if row[0] == "Synchronized clusters")
        rows[at:at] = [
            (
                f"  silence began nearest to {marker_label(names[key]) if key in names else key}",
                count,
            )
            for key, count in nearest.items()
        ]
    for label, value in rows:
        table.add_row(label, str(value))
    console = Console()
    console.print(table)
    placebo = placebo_notice(outcome)
    clearly = outcome.placebo is not None and outcome.placebo.clearly_above
    for index, line in enumerate(placebo):
        console.print(
            line,
            style=("bold" if clearly else "bold red") if index == 0 else None,
            highlight=False,
            markup=False,
        )
    console.print(OUTAGE_NOTICE, style="yellow", highlight=False, markup=False)
    for line in estimate_lines(outcome):
        console.print(_hide(line), highlight=False, markup=False)
    top = outcome.candidates[:10]
    if top:
        detail = Table(title="Top candidates (for manual review)")
        first = "candidate" if hide_handles else "handle"
        columns = [
            first,
            "score",
            "signals",
            "cluster",
            "last activity (UTC)",
            "posts/day",
            "p_silence",
        ]
        anchors = {a.id: a for a in outcome.params.anchors}
        if anchors:
            columns.append("nearest marker")
        for col in columns:
            detail.add_column(col, no_wrap=col.startswith("last"))
        for rank, r in enumerate(top, 1):
            cells = [
                f"#{rank}" if hide_handles else r.handle,
                f"{r.score:.3f}",
                str(r.independent_signals),
                f"{r.cluster_id} (n={r.cluster_size})" if r.cluster_id else "-",
                isoformat(r.last_activity) if r.last_activity else "-",
                f"{r.posts_per_day:.2f}",
                f"{r.p_silence:.2g}" if r.p_silence is not None else "-",
            ]
            if anchors:
                marker = anchors.get(r.nearest_marker or "")
                cells.append(marker_label(marker, r.nearest_marker_offset_hours) if marker else "-")
            detail.add_row(*cells)
        console.print(detail)
    note = truncated_notice(outcome)
    if note:
        console.print(_hide(note), style="yellow", highlight=False, markup=False)
    if s.get("spend_usd"):
        console.print(
            f"Estimated spend recorded in the ledger for this run: ${s['spend_usd']:,.2f}"
        )
    chain_spend = s.get("chain_spend_usd")
    root = s.get("root_run_id")
    if chain_spend and root is not None and root != outcome.run_id:
        console.print(
            f"Estimated spend across the resume chain from run {root}: ${chain_spend:,.2f}"
        )
    console.print(f"[dim]{NOTICE}[/dim]")


def _warn_provisional(outcome: ScanOutcome) -> None:
    p = outcome.params
    if p.provisional:
        banner = "!" * 72
        typer.secho(banner, fg=typer.colors.YELLOW, err=True)
        if p.days_since_cutoff < p.min_silence_days:
            since = (
                "the cutoff" if p.horizon is None else "the cutoff (to the source's data horizon)"
            )
            headline = (
                f"WARNING: only {p.days_since_cutoff:.1f} days have passed since {since} "
                f"(minimum {p.min_silence_days:g}). All cliff results are PROVISIONAL."
            )
        else:
            headline = "WARNING: --allow-stale-source was used. All cliff results are PROVISIONAL."
        typer.secho(headline, fg=typer.colors.YELLOW, bold=True, err=True)
        typer.secho(PROVISIONAL_NOTICE, fg=typer.colors.YELLOW, err=True)
        typer.secho(
            "Rerun later (the cache makes this cheap) before drawing conclusions.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        typer.secho(banner, fg=typer.colors.YELLOW, err=True)


def _write_output(
    outcome: ScanOutcome,
    platform: str,
    out: Path | None,
    fmt: str | None,
    include_unavailable: bool,
) -> Path:
    resolved = _resolve_format(fmt, out)
    path = out or Path(f"bot-farm-silence-tracker-run-{outcome.run_id}.{resolved}")
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = write_json if resolved == "json" else write_csv
    count = writer(path, outcome, platform, include_unavailable)
    typer.echo(f"Wrote {count} rows to {path}")
    return path


def _tolerate_unencodable_output() -> None:
    """On Windows, replace characters the console encoding cannot show.

    Redirected output on Windows uses the ANSI code page, which may lack characters
    the CLI prints: a Japanese system's cp932, for example, has no em dash, so the
    notice would raise :class:`UnicodeEncodeError` without this. POSIX streams are
    left as they are. :func:`run` calls it before any output, ``--help`` and
    ``--version`` included.
    """
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(errors="replace")


def _version_callback(value: bool) -> None:
    """Print the version and exit before typer asks for a command."""
    if value:
        typer.echo(f"bot-farm-silence-tracker {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Entry point; the options live on the subcommands."""
    _setup_logging()
    _start_hiding(False)


#: Scan options that ``--from-run`` takes from the stored run unless given, with
#: their defaults. Scoring keys live under ``params["scoring"]``. The cutoff has no
#: default here: it comes from the scan's outage event, and a resume keeps the run's.
#: The window defaults are the algorithm's; an event may set its own.
_SCORING_KEYS = (
    "cutoff",
    "baseline_days",
    "min_silence_days",
    "min_active_days",
    "min_posts",
    "recent_days",
    "max_weekly_cv",
    "cliff_window_hours",
    "sync_minutes",
)
RESUMABLE_DEFAULTS: dict[str, Any] = {
    "cutoff": None,
    "baseline_days": 30,
    "min_silence_days": 3.0,
    "min_active_days": 12,
    "min_posts": 20,
    "recent_days": 7.0,
    "max_weekly_cv": 1.0,
    "cliff_window_hours": 48.0,
    "sync_minutes": 30.0,
    "n": DEFAULT_SAMPLE_SIZE,
    "pool_factor": DEFAULT_POOL_FACTOR,
    "seed": None,
    "new_accounts_days": None,
    "max_requests": None,
}
_FLAG = {key: "--" + key.replace("_", "-") for key in RESUMABLE_DEFAULTS}


def _stored_resumable(params: dict[str, Any]) -> dict[str, Any]:
    """Pull the resumable options out of a stored run's params (missing keys skipped)."""
    scoring = params.get("scoring") or {}
    out: dict[str, Any] = {}
    for key in RESUMABLE_DEFAULTS:
        source = scoring if key in _SCORING_KEYS else params
        if key in source:
            out[key] = source[key]
    return out


_TRUE_TEXT = frozenset({"true", "yes", "on", "1"})
_FALSE_TEXT = frozenset({"false", "no", "off", "0"})


def _as_bool(value: Any) -> bool | None:
    """``value`` as a bool when it is one or spells one (true/false, yes/no, on/off, 1/0)."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_TEXT:
        return True
    if text in _FALSE_TEXT:
        return False
    return None


def _same(key: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    if key == "cutoff":
        return parse_datetime(str(a)) == parse_datetime(str(b))
    if isinstance(a, bool) or isinstance(b, bool):
        left, right = _as_bool(a), _as_bool(b)
        return left is not None and left == right
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return a == b


def _same_option(stored: Any, given: Any) -> bool:
    """Compare a stored ``--source-option`` value with a given one.

    Numbers compare as numbers (``2000`` matches ``2000.0``) and bools as bools
    (``True`` matches ``true``, ``yes`` or ``on``).
    """
    if str(stored) == str(given):
        return True
    try:
        return float(str(stored)) == float(str(given))
    except ValueError:
        pass
    left, right = _as_bool(stored), _as_bool(given)
    return left is not None and left == right


def pending_repass_keys(stored_params: dict[str, Any]) -> set[str]:
    """Names of options a stored run could not keep, which a resume has to pass again.

    These are the recorded ``unstored_option_keys`` plus keys whose stored value is a
    legacy redacted URL with credentials. Names only, never values.
    """
    keys = set(map(str, stored_params.get("unstored_option_keys") or ()))
    keys.update(
        str(k)
        for k, v in (stored_params.get("source_options") or {}).items()
        if has_url_userinfo(v)
    )
    return keys


def _clamp_inherited_history_limit(options: dict[str, Any], run_id: int) -> dict[str, Any]:
    """Raise an inherited reddit-archive ``history_limit`` below the minimum to it.

    Runs made before the minimum existed may have stored a smaller limit. Refusing it
    would make such a run impossible to resume without ``--force``, so the resume uses
    the minimum instead and says so. A limit given on the command line is not touched
    (the adapter still refuses it).
    """
    from bot_farm_silence_tracker.sources.reddit_archive import MIN_HISTORY_LIMIT

    value = options.get("history_limit")
    if value is None:
        return options
    try:
        limit = int(str(value).strip())
    except ValueError:
        return options
    if not 1 <= limit < MIN_HISTORY_LIMIT:
        return options
    typer.secho(
        f"warning: run {run_id} used --source-option history_limit={limit}, below the "
        f"minimum of {MIN_HISTORY_LIMIT} this version allows (a smaller page could skip "
        f"rows of a busy second); the resumed run uses history_limit={MIN_HISTORY_LIMIT}.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    return {**options, "history_limit": str(MIN_HISTORY_LIMIT)}


def resolve_resume(
    run_id: int,
    stored_params: dict[str, Any],
    explicit: dict[str, Any],
    explicit_options: dict[str, str],
    *,
    force: bool,
    secret_options: tuple[str, ...] = (),
    credential_destination_options: Iterable[str] = (),
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Merge a stored run's options with the ones given on the command line.

    Returns ``(values, source_options, overrides)``. Stored values win over
    defaults; a value given explicitly must match the stored one. A mismatch is a
    conflict: without ``force`` it raises ``ValueError`` listing every conflict,
    with ``force`` the given value wins and the conflict is returned in
    ``overrides``. Secret-looking option keys (see
    :func:`~bot_farm_silence_tracker.pipeline.is_secret_option`, plus ``secret_options``) are never
    stored, so they are taken from the command line without a check. A value that
    is a URL with credentials (``user:pass@``, see
    :func:`~bot_farm_silence_tracker.pipeline.has_url_userinfo`) is handled the same way: it is
    taken without a check, and a redacted ``***@`` value an older run stored is not
    inherited, so it has to be passed again. An option the stored run did not have
    is a conflict too, so adding one needs ``force``.

    ``credential_destination_options`` (see
    :attr:`~bot_farm_silence_tracker.sources.base.Source.credential_destination_options`)
    decide where credentials are sent, so a stored value is never inherited: anyone
    who can write the database could otherwise redirect the login. A value given on
    the command line is taken without a check. The caller refuses a resume that
    would need the stored value (see :func:`dropped_credential_destinations`).
    """
    stored = _stored_resumable(stored_params)
    conflicts: list[str] = []
    values = {**RESUMABLE_DEFAULTS, **stored}
    for key, value in explicit.items():
        if key in stored and not _same(key, value, stored[key]):
            conflicts.append(f"{_FLAG[key]}: run {run_id} used {stored[key]!r}, got {value!r}")
        values[key] = value if (force or key not in stored) else stored[key]
    # Older runs stored URL values with their credentials redacted to ``***@``;
    # reusing one would send a placeholder, so it is dropped like a secret.
    stored_options = {
        k: v
        for k, v in (stored_params.get("source_options") or {}).items()
        if not has_url_userinfo(v)
    }
    destinations = set(credential_destination_options)
    options = {k: v for k, v in stored_options.items() if k not in destinations}
    needs_repass = pending_repass_keys(stored_params)
    for key in sorted(needs_repass - set(explicit_options)):
        typer.secho(
            f"warning: run {run_id} used the option {key!r}, which is not stored because "
            f"it holds a credential. Pass --source-option {key}=... again, or the resumed "
            "run will go without it.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    for key, value in explicit_options.items():
        secret = (
            is_secret_option(key, secret_options) or has_url_userinfo(value) or key in destinations
        )
        # A key that has to be passed again was never stored, so a credential-free value
        # for it cannot differ from anything the run used.
        repass_only = key in needs_repass and key not in stored_options
        if (
            not secret
            and not repass_only
            and (key not in stored_options or not _same_option(stored_options[key], value))
        ):
            before = stored_options.get(key, "<unset>")
            conflicts.append(f"--source-option {key}: run {run_id} used {before!r}, got {value!r}")
        if force or secret or key not in stored_options:
            options[key] = value
    if conflicts and not force:
        raise ValueError(
            f"these options differ from run {run_id}, which --from-run continues: "
            + "; ".join(conflicts)
            + ". Drop them to use the run's values, or pass --force to override."
        )
    return values, {k: str(v) for k, v in options.items()}, conflicts


def dropped_credential_destinations(
    stored_params: dict[str, Any],
    explicit_options: dict[str, str],
    credential_destination_options: Iterable[str],
) -> dict[str, str]:
    """The stored options that decide where credentials go and were not passed again.

    :func:`resolve_resume` does not inherit them. Returns ``{key: stored value}``.
    """
    stored = stored_params.get("source_options") or {}
    return {
        key: str(stored[key])
        for key in sorted(credential_destination_options)
        if key in stored and key not in explicit_options
    }


def resume_command(
    platform: str,
    run_id: int,
    db: Path | None,
    values: dict[str, Any],
    options: dict[str, Any],
    *,
    event_id: str,
    secret_options: tuple[str, ...] = (),
    allow_stale_source: bool = False,
) -> str:
    """Return the shell command that continues ``run_id`` with the same options.

    The command names the run's outage event (``--event``): a resume refuses any
    other. With ``allow_stale_source`` (the run used ``--allow-stale-source``) the
    command passes it again, so the resume does not stop on the same stale source.

    ``--db`` is given as a resolved absolute path, so the command works from any
    directory; it is left out when it is the event's own database. Floats are
    written with ``repr`` so they round-trip exactly. The cutoff is not repeated: a
    resume keeps the run's. Secret-looking options are left out; see
    :func:`~bot_farm_silence_tracker.pipeline.unstored_options` for the list to re-pass.
    """
    parts = ["bot-farm-silence-tracker", "scan", "--platform", platform, "--from-run", str(run_id)]
    parts += ["--event", event_id]
    if allow_stale_source:
        parts.append("--allow-stale-source")
    if db is not None:
        resolved = Path(db).expanduser().resolve()
        try:
            own = event_db_path(event_id).resolve()
        except ValueError:
            own = None
        if resolved != own:
            parts += ["--db", str(resolved)]
    for key, default in RESUMABLE_DEFAULTS.items():
        value = values.get(key)
        if key == "cutoff":
            continue
        if value is not None and not _same(key, value, default):
            parts += [_FLAG[key], repr(value) if isinstance(value, float) else str(value)]
    for key, value in sorted(storable_options(options, secret_options).items()):
        if platform == "x" and key == "max_usd":
            parts += ["--max-usd", str(value)]  # The dedicated flag reads clearer.
        else:
            parts += ["--source-option", f"{key}={value}"]
    return join_command(parts)


#: Characters PowerShell takes literally in a bare command argument.
_POWERSHELL_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-+=.,:/\\@"
)
#: Characters PowerShell reads as a single quote; inside a single-quoted string
#: each one is written twice to stand for itself.
_POWERSHELL_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"


def _powershell_quote(arg: str) -> str:
    """Quote ``arg`` so PowerShell passes it to a program unchanged."""
    if arg and all(char in _POWERSHELL_SAFE for char in arg):
        return arg
    quoted = "".join(char * 2 if char in _POWERSHELL_SINGLE_QUOTES else char for char in arg)
    return f"'{quoted}'"


def join_command(parts: list[str], *, windows: bool | None = None) -> str:
    """Join ``parts`` into one command line quoted for the user's shell.

    POSIX shells get :func:`shlex.join`. Windows (``windows`` defaults to
    ``os.name == "nt"``) targets PowerShell, the documented Windows shell: an
    argument with any character outside letters, digits and ``_-+=.,:/\\@`` is put
    in single quotes, with each single quote inside it doubled, so characters such
    as ``(``, ``&``, ``;``, ``{``, ``$`` and ``%`` reach the program literally and
    backslashes in paths are kept. ``cmd.exe`` does not understand single quotes;
    users of ``cmd.exe`` should paste the command into PowerShell instead.
    """
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return " ".join(_powershell_quote(part) for part in parts)
    return shlex.join(parts)


def _credential_destination_options_of(platform: str) -> frozenset[str]:
    """The adapter's ``credential_destination_options``, or none when it cannot load."""
    try:
        return frozenset(load_source_class(platform).credential_destination_options)
    except Exception:  # The adapter is loaded (and its errors reported) later.
        return frozenset()


def _secret_options_of(platform: str) -> tuple[str, ...]:
    """The adapter's ``secret_options``, or ``()`` when the adapter cannot be loaded."""
    try:
        return tuple(load_source_class(platform).secret_options)
    except Exception:  # The adapter is loaded (and its errors reported) later.
        return ()


def _catalog() -> outage_events.EventCatalog:
    """The events of this installation; a bad events file stops the command."""
    try:
        return outage_events.load_events()
    except outage_events.EventError as exc:
        raise _fail(str(exc)) from None


def _resolve_event(
    given: str | None, cutoff: str | None = None
) -> tuple[outage_events.OutageEvent, bool]:
    """The outage event of a command, and whether the user named it.

    ``--cutoff`` makes a custom event; with ``--event`` too, both must name the same
    event. Without either: the config file's ``default_event``, else the events
    file's default.
    """
    catalog = _catalog()
    if cutoff is not None:
        try:
            custom = outage_events.parse_cutoff_option(cutoff)
        except outage_events.EventError as exc:
            raise _fail(str(exc)) from None
        if given is not None and given != custom.id:
            raise _fail(
                f"--cutoff {cutoff} is the custom event {custom.id}, but --event says "
                f"{given}; give one of them"
            )
        return custom, True
    if given is not None:
        try:
            return catalog.resolve(given.strip()), True
        except outage_events.EventError as exc:
            raise _fail(str(exc)) from None
    try:
        loaded = user_config.load_config()
    except user_config.ConfigError:
        loaded = None  # The platform step reports a broken config file.
    chosen = loaded.default_event if loaded is not None else None
    if chosen is not None:
        try:
            return catalog.resolve(chosen), False
        except outage_events.EventError:
            typer.secho(
                f"warning: the config file's default_event {chosen!r} is not an event; "
                f"using {catalog.default_id}. `bot-farm-silence-tracker config "
                "set-default-event` changes it.",
                fg=typer.colors.YELLOW,
                err=True,
            )
    return catalog.default, False


def _event_db(db: Path | None, event: outage_events.OutageEvent) -> Path:
    """``--db``, else the event's own database (moving the old single one into the
    default event's folder the first time; see :func:`prepare_event_db`)."""
    if db is not None:
        return Path(db).expanduser()
    catalog = _catalog()
    return prepare_event_db(event.id, legacy_event_id=catalog.legacy_id)


def _run_event(run: dict[str, Any] | None, run_id: int) -> outage_events.OutageEvent:
    """The outage event a stored run belongs to (see ``events.event_of_run``)."""
    try:
        return outage_events.event_of_run((run or {}).get("params") or {}, _catalog())
    except outage_events.EventError as exc:
        raise _fail(f"run {run_id}: {exc}") from None


def _check_run_event(
    run: dict[str, Any] | None,
    run_id: int,
    event: outage_events.OutageEvent,
    explicit: bool,
    db: Path,
) -> outage_events.OutageEvent:
    """The run's event; refuse when the user named another one."""
    own = _run_event(run, run_id)
    if explicit and own.id != event.id and own.legacy_of == event.id:
        # The run was scanned under an earlier definition of the named event (an old
        # resume command names it): it continues with its own cutoff, never the new one.
        typer.secho(
            f"note: run {run_id} was scanned with the cutoff {own.cutoff_text}, an earlier "
            f"definition of {event.id} (now cutoff {event.cutoff_text}); it continues "
            f"under the local event {own.id} with its own cutoff (pass --event {own.id} "
            "from now on).",
            err=True,
        )
        return own
    if explicit and own.id != event.id:
        raise _fail(
            f"run {run_id} belongs to the outage event {own.id} (cutoff {own.cutoff_text}), "
            f"not {event.id}; a run is only continued or used under its own event. Pass "
            f"--event {own.id} --db {db} instead."
        )
    return own


def _shared_event_of_old_resume(
    custom: outage_events.OutageEvent, from_run: int, db: Path | None
) -> outage_events.OutageEvent:
    """The shared event a resume command printed before events existed means.

    Those commands carry ``--cutoff`` (the run's cutoff) and no ``--event``. When the
    cutoff is a shared event's, and the run either belongs to that event (a run made
    before events, in the database ``--db`` names) or is not in the custom event's
    own database (the old database was moved into the shared event's folder), the
    resume continues under the shared event and says so.
    """
    catalog = _catalog()
    shared = catalog.for_cutoff(custom.cutoff)
    if shared.custom:
        return custom
    own = Path(db).expanduser() if db is not None else event_db_path(custom.id)
    run: dict[str, Any] | None = None
    if own.is_file():
        try:
            with Storage(own) as storage:
                run = storage.get_run(from_run)
        except Exception:  # The resume step reports an unreadable database.
            return custom
    if run is not None:
        if _run_event(run, from_run).id != shared.id:
            return custom
    elif db is not None:
        return custom  # The resume step reports the missing run.
    typer.secho(
        f"note: --cutoff {custom.cutoff_text} is the cutoff of the outage event "
        f"{shared.id}; continuing run {from_run} under that event (pass --event "
        f"{shared.id} from now on).",
        err=True,
    )
    return shared


def _scan_markers(
    event: outage_events.OutageEvent, stored_run: dict[str, Any] | None
) -> dict[str, Any]:
    """The markers a scan uses: the event's for a new scan; for a resume, the ones the
    run was scored with (none for a run from before markers), like its cutoff."""
    if stored_run is None:
        return {
            "markers": event.scoring_markers(),
            "marker_margin_hours": event.scoring_marker_margin(),
        }
    scoring = ((stored_run.get("params") or {}).get("scoring")) or {}
    stored = scoring.get("markers") or []
    try:
        markers = tuple(Marker.from_dict(m) for m in stored)
        margin = float(scoring.get("marker_margin_hours") or 0.0) if markers else 0.0
    except (KeyError, TypeError, ValueError):
        raise _fail("the stored run's markers cannot be read") from None
    return {"markers": markers, "marker_margin_hours": margin}


def _print_event(event: outage_events.OutageEvent) -> None:
    kind = "custom event, local only" if event.custom else "outage event"
    typer.echo(f"Event: {event.id} ({kind}: {event.name}), cutoff {event.cutoff_text}.", err=True)
    if event.markers:
        _, _, start = event.windows()
        typer.echo(
            "Markers (times as reported): "
            + "; ".join(m.label() for m in event.markers)
            + f". Silence window from {isoformat(start)}.",
            err=True,
        )


def _stored_run_platform(from_run: int, db: Path | None) -> str | None:
    """The platform the stored run ``from_run`` used, or ``None`` when it cannot be read."""
    try:
        with Storage(db) as storage:
            run = storage.get_run(from_run)
    except Exception:  # The resume step reports a missing or unreadable database.
        return None
    return str(run["platform"]) if run and run.get("platform") else None


def _resolve_platform(
    given: str | None, from_run: int | None = None, db: Path | None = None
) -> str:
    """The platform to scan: ``--platform``, the resumed run's, the config default, else
    reddit-archive.

    A resume without ``--platform`` continues the run on the platform it used, whatever
    the config says, and prints no note. Otherwise a platform the user did not enable
    still runs; a one-line note says so. The note appears only when the config file
    records an enabled list.
    """
    if given is None and from_run is not None:
        stored = _stored_run_platform(from_run, db)
        if stored is not None:
            return stored
    if given is not None:
        platform = given.lower()
        try:
            loaded = user_config.load_config()
        except user_config.ConfigError:
            return platform  # An explicit --platform never depends on a readable config.
    else:
        try:
            loaded = user_config.load_config()
        except user_config.ConfigError as exc:
            raise _fail(str(exc)) from None
        platform = loaded.default
    _config_warnings(loaded)
    if loaded.platforms is not None and platform in SEED_KEYS and platform not in loaded.platforms:
        typer.secho(
            f"note: {platform} is not in your enabled platforms ({', '.join(loaded.platforms)}); "
            "scanning it anyway. `bot-farm-silence-tracker config set-platforms` changes the list.",
            err=True,
        )
    return platform


RegistryOption = Annotated[
    Path | None,
    typer.Option("--registry", help="Shared registry directory [default: registry]."),
]
DEFAULT_REGISTRY = Path("registry")
AllowInsideRepoOption = Annotated[
    bool,
    typer.Option(
        "--allow-inside-repo",
        help="Write --out even inside a git repository (refused by default: the file "
        "holds real account names).",
    ),
]


def _registered(
    registry: Path | None, platform: str, now: Any, days: float, event_id: str
) -> set[str]:
    """IDs the registry records as scanned for ``event_id`` on ``platform`` within
    ``days`` of ``now``."""
    from bot_farm_silence_tracker.registry.build import registered_ids

    root = registry or DEFAULT_REGISTRY
    if not root.is_dir():
        raise _fail(f"no registry directory at {root}")
    try:
        return registered_ids(root, platform, now, days, event_id)
    except ValueError as exc:
        raise _fail(f"the registry is invalid: {exc}") from None


def _skip_registered(source: Source, registered: set[str]) -> list[int]:
    """Make ``source.discover`` leave out ``registered`` accounts; return the skip count.

    Discovery is asked for up to ``pool_limit`` more accounts (at most twice the pool)
    so the skipped ones do not shrink the pool.
    """
    discover = source.discover
    count = [0]

    def filtered(seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        kept = 0
        wider = pool_limit + min(len(registered), pool_limit)
        for account in discover(seeds, wider):
            if account.user_id in registered:
                count[0] += 1
                continue
            kept += 1
            yield account
            if kept >= pool_limit:
                return

    source.discover = filtered  # type: ignore[method-assign]
    return count


RecentDaysOption = typer.Option(
    "--recent-days",
    min=0.0,
    help="The steady test also needs activity in the last D days before the baseline "
    "ends (0 turns this off) [default: 7].",
)
MaxWeeklyCvOption = typer.Option(
    "--max-weekly-cv",
    min=0.0,
    help="The steady test's cap on the coefficient of variation of the weekly baseline "
    "counts [default: 1.0].",
)


@app.command()
def scan(
    platform: Annotated[
        str | None,
        typer.Option(
            "--platform",
            "-p",
            help="reddit, reddit-archive, x, bluesky or fake [default: the default_platform "
            f"in the config file (see `config show`), else {user_config.DEFAULT_PLATFORM}].",
        ),
    ] = None,
    n: Annotated[
        int | None,
        typer.Option("--n", min=1, help=f"Sample size [default: {DEFAULT_SAMPLE_SIZE}]."),
    ] = None,
    new_accounts_days: Annotated[
        float | None,
        typer.Option(
            "--new-accounts-days", min=0.1, help="Only accounts created within the last D days."
        ),
    ] = None,
    event: EventOption = None,
    cutoff: Annotated[
        str | None,
        typer.Option(
            "--cutoff",
            help="A custom cutoff, a UTC date or date and time to the minute (2026-12-03 or "
            "2026-12-03T06:00Z). It makes the local-only custom event "
            "custom-YYYYMMDDTHHMMZ, with its own database; it is never written to the "
            "shared registry [default: the event's cutoff].",
        ),
    ] = None,
    baseline_days: Annotated[
        int | None, typer.Option("--baseline-days", min=1, help="[default: the event's]")
    ] = None,
    min_silence_days: Annotated[
        float | None,
        typer.Option(
            "--min-silence-days",
            min=0.0,
            help="Silence needed before results stop being provisional [default: the event's].",
        ),
    ] = None,
    min_active_days: Annotated[
        int | None, typer.Option("--min-active-days", min=1, help="[default: 12]")
    ] = None,
    min_posts: Annotated[int | None, typer.Option("--min-posts", min=1, help="[default: 20]")] = (
        None
    ),
    recent_days: Annotated[float | None, RecentDaysOption] = None,
    max_weekly_cv: Annotated[float | None, MaxWeeklyCvOption] = None,
    cliff_window_hours: Annotated[
        float | None,
        typer.Option(
            "--cliff-window-hours",
            min=0.0,
            help="Last activity must be this close to the cutoff [default: the event's].",
        ),
    ] = None,
    sync_minutes: Annotated[
        float | None,
        typer.Option(
            "--sync-minutes",
            min=0.0,
            help="Stops within this window count as synchronized [default: 30].",
        ),
    ] = None,
    seed: Annotated[int | None, typer.Option("--seed", help="RNG seed for the sample.")] = None,
    pool_factor: Annotated[
        float | None,
        typer.Option(
            "--pool-factor",
            min=1.0,
            help=f"Discovery pool = factor x n [default: {DEFAULT_POOL_FACTOR:g}, "
            "or the platform's own default (1 for X, 1.2 for reddit-archive)].",
        ),
    ] = None,
    active_within_days: Annotated[
        float | None,
        typer.Option(
            "--active-within-days",
            min=0.1,
            help="reddit-archive only: discover authors active in the seed communities in "
            "the last D days before the scan's effective now (now, or the archive horizon "
            "if earlier), clipped to the baseline window before the cutoff, instead of in "
            "that whole window. A resume inherits the run's value.",
        ),
    ] = None,
    baseline_sample_rate: Annotated[
        float | None,
        typer.Option(
            "--baseline-sample-rate",
            help="reddit-archive and bluesky only, opt in: two-phase sampling. Pre-check "
            "every account with one or two existence queries over [cutoff - cliff window, "
            "now), then fetch the full baseline for every cliff candidate and for this "
            "fraction P (0 < P <= 1) of the others. Cliffs stay an exact count (unless data "
            "changed after the pre-check); the eligible total and the cliff rate become "
            "estimates with 95% intervals. 0.1 to 0.2 is the useful range; 0.5 or more can "
            "cost more than a census. Off by default (full census). A resume inherits "
            "the run's value.",
        ),
    ] = None,
    seeds_path: Annotated[
        Path | None, typer.Option("--seeds", help="YAML seed file replacing the bundled one.")
    ] = None,
    subreddit: Annotated[
        list[str] | None, typer.Option("--subreddit", help="Seed subreddit (repeatable).")
    ] = None,
    query: Annotated[
        list[str] | None,
        typer.Option(
            "--query",
            help="Seed search query for X or Bluesky (repeatable). For Bluesky it may "
            "also be a feed or list AT-URI or bsky.app URL.",
        ),
    ] = None,
    max_requests: Annotated[
        int | None, typer.Option("--max-requests", min=1, help="Hard cap on HTTP requests.")
    ] = None,
    source_option: Annotated[
        list[str] | None,
        typer.Option(
            "--source-option",
            help="Adapter option as key=value (repeatable). See the platform's README "
            "section. Do not pass secrets here; use BOT_FARM_SILENCE_TRACKER_* "
            "environment variables.",
        ),
    ] = None,
    from_run: Annotated[
        int | None,
        typer.Option(
            "--from-run",
            min=1,
            help="Continue this run: reuse its stored sample (skips discovery) and its "
            "options, and fetch only accounts it did not fetch.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="With --from-run, let given options override the run's stored ones."
        ),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help=(
                "Refetch every account instead of reusing cached activity. A refresh "
                "stopped by the budget keeps the older cached rows below where it "
                "stopped, so those are not refetched."
            ),
        ),
    ] = False,
    allow_stale_source: Annotated[
        bool,
        typer.Option(
            "--allow-stale-source",
            help="Run even when the source's data ends too soon after the cutoff to judge "
            "silence; every result is then provisional.",
        ),
    ] = False,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            min=1,
            help="Fetch this many accounts at once (reddit-archive and bluesky: 1 to 3; other "
            "platforms: 1). The request rate stays capped by the one shared pacer "
            "(max_rps); more workers only overlap one account's waits with another's "
            "requests. Results match 1, except that a scan that stops early may also store "
            "accounts other workers had already finished.",
        ),
    ] = 1,
    max_consecutive_errors: Annotated[
        int,
        typer.Option(
            "--max-consecutive-errors",
            min=1,
            help="Abort after this many accounts in a row fail with an error.",
        ),
    ] = 10,
    allow_sleep: Annotated[
        bool,
        typer.Option(
            "--allow-sleep",
            help="Let the computer go to sleep while the scan runs. By default the scan asks "
            "the system not to start an idle sleep (the screen may still turn off), as a "
            "sleeping computer loses its network and the scan stalls.",
        ),
    ] = False,
    max_usd: Annotated[
        float | None,
        typer.Option(
            "--max-usd",
            min=0.0,
            help="X only: hard USD spend cap (required for X; same as --source-option max_usd=D).",
        ),
    ] = None,
    dry_run_cost: Annotated[
        bool,
        typer.Option(
            "--dry-run-cost",
            help="Run discovery and sampling (billed on X, so it needs --yes), store the "
            "sample, print the projected cost of fetching it, and fetch no activity. "
            "Continue later with --from-run.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm that billed discovery may run. Required on paid platforms "
            "(X) for any scan without --from-run.",
        ),
    ] = False,
    out: OutOption = None,
    fmt: FormatOption = None,
    db: DbOption = None,
    cache_ttl_hours: Annotated[
        float, typer.Option("--cache-ttl-hours", min=0.0, help="Reuse cached activity this long.")
    ] = 24.0,
    include_unavailable: Annotated[
        bool,
        typer.Option(
            "--include-unavailable",
            help="Also list suspended/deleted/private accounts in the output (accounts that "
            "opted out of logged-out views are only counted, never listed).",
        ),
    ] = False,
    hide_handles: HideHandlesOption = False,
    trust_env: Annotated[
        bool,
        typer.Option(
            "--trust-env",
            envvar=f"{ENV_PREFIX}TRUST_ENV",
            help="Use the proxy (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY) and CA bundle "
            "(SSL_CERT_FILE, SSL_CERT_DIR) settings from the environment, for networks "
            "that need a proxy. Off by default: the scan connects directly and trusts "
            f"only its bundled CA certificates. Same as {ENV_PREFIX}TRUST_ENV=1.",
        ),
    ] = False,
    skip_registered_within_days: Annotated[
        float | None,
        typer.Option(
            "--skip-registered-within-days",
            min=0.0,
            help="Leave out of discovery the accounts the shared registry (--registry) "
            "records as scanned within the last D days, so the sample is spent on accounts "
            "nobody checked recently. Not with --from-run.",
        ),
    ] = None,
    registry: RegistryOption = None,
    placebo_weeks: Annotated[
        int | None,
        typer.Option(
            "--placebo-weeks",
            min=0,
            max=MAX_PLACEBO_WEEKS,
            help="Compare the cliff rate with placebo cutoffs this many weeks before the real "
            "one (the same weekday and hour). Each placebo cutoff gets a sample of its own, "
            "discovered and fetched in windows moved back by the same weeks, so this repeats "
            "discovery and fetching that many times; platforms that cannot discover in "
            "earlier weeks have no comparison. 0 turns it off; otherwise at least 3 "
            "[default: 3, or the resumed run's].",
        ),
    ] = None,
    now: Annotated[
        str | None, typer.Option("--now", hidden=True, help="Pin the reference time (testing).")
    ] = None,
) -> None:
    """Sample accounts, fetch activity (cached) and flag cliff candidates.

    The scan belongs to an outage event (``--event``, or a custom one from
    ``--cutoff``), which gives the cutoff and the default windows and picks the cache
    database. ``--from-run`` continues a run only under the event it started with.
    """
    hooks = _current_hooks()
    redactor = _start_hiding(hide_handles)
    chosen_event, event_named = _resolve_event(event, cutoff)
    if from_run is not None and event is None and chosen_event.custom:
        chosen_event = _shared_event_of_old_resume(chosen_event, from_run, db)
    db = _event_db(db, chosen_event)
    platform = _resolve_platform(platform, from_run, db)
    _resolve_format(fmt, out)  # Fail on a bad format before doing any work.
    try:
        explicit_options = parse_source_options(source_option)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    if max_usd is not None:
        given_option = explicit_options.get("max_usd")
        if given_option is not None and not _same_option(given_option, max_usd):
            typer.secho(
                f"warning: --max-usd {max_usd:g} overrides --source-option "
                f"max_usd={given_option}; the cap is ${max_usd:,.2f}.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        # Stored with the run like any non-secret source option, so --from-run inherits
        # the cap (and a different --max-usd on a resume is a conflict).
        explicit_options["max_usd"] = str(max_usd)
    given = {
        "baseline_days": baseline_days,
        "min_silence_days": min_silence_days,
        "min_active_days": min_active_days,
        "min_posts": min_posts,
        "recent_days": recent_days,
        "max_weekly_cv": max_weekly_cv,
        "cliff_window_hours": cliff_window_hours,
        "sync_minutes": sync_minutes,
        "n": n,
        "pool_factor": pool_factor,
        "seed": seed,
        "new_accounts_days": new_accounts_days,
        "max_requests": max_requests,
    }
    explicit = {key: value for key, value in given.items() if value is not None}

    resumed_size: int | None = None
    inherited_unstored: set[str] = set()
    if from_run is not None:
        with Storage(db) as storage:
            try:
                # Validate before any request.
                resumed_accounts = load_run_sample(storage, from_run, platform).accounts
                resumed_size = len(resumed_accounts)
                if redactor is not None:
                    for account in resumed_accounts:
                        redactor.add_account(account)
            except (LookupError, ValueError) as exc:
                raise _fail(str(exc)) from None
            stored_run = storage.get_run(from_run)
            # Refused before any request. The scan takes the lock itself and refuses
            # again if another process took it in the meantime.
            root_run = storage.root_run_id(from_run)
            if storage.run_in_use(root_run):
                raise _fail(str(RunInUse(from_run, storage.run_heartbeat(root_run)))) from None
        assert stored_run is not None
        # Not even with --force: the cutoff and the database belong to the event.
        chosen_event = _check_run_event(stored_run, from_run, chosen_event, event_named, db)
        try:
            values, options, overrides = resolve_resume(
                from_run,
                stored_run["params"],
                explicit,
                explicit_options,
                force=force,
                secret_options=_secret_options_of(platform),
                credential_destination_options=_credential_destination_options_of(platform),
            )
        except ValueError as exc:
            raise _fail(str(exc)) from None
        inherited_unstored = pending_repass_keys(stored_run["params"]) - set(explicit_options)
        if platform == "reddit-archive" and "history_limit" not in explicit_options:
            options = _clamp_inherited_history_limit(options, from_run)
        for item in overrides:
            typer.secho(
                _hide(f"warning: --force override, {item}"), fg=typer.colors.YELLOW, err=True
            )
        if values.get("cutoff") is None:  # A run without a stored cutoff: its event's.
            values["cutoff"] = chosen_event.cutoff_text
    else:
        values = {**RESUMABLE_DEFAULTS, **chosen_event.scoring_defaults(), **explicit}
        options = explicit_options

    if placebo_weeks is not None and 0 < placebo_weeks < MIN_PLACEBO_WEEKS:
        raise _fail(
            f"--placebo-weeks must be 0 (off) or at least {MIN_PLACEBO_WEEKS}, so the placebo "
            "spread can be estimated"
        )
    if placebo_weeks is None:
        stored_scoring = (stored_run["params"].get("scoring") or {}) if from_run is not None else {}
        placebo_weeks = int(stored_scoring.get("placebo_weeks", DEFAULT_PLACEBO_WEEKS))
    if values.get("seed") is None and from_run is None:
        # A recorded seed, never None, so every run can be repeated (see `report`).
        values["seed"] = random_seed()
        typer.echo(
            f"Discovery and sampling seed: {values['seed']} (repeat with --seed {values['seed']}).",
            err=True,
        )

    when_cutoff = _parse_when(str(values["cutoff"]), "the cutoff")
    when_now = _parse_when(now, "--now") if now else utcnow()
    if when_now <= when_cutoff:  # type: ignore[operator]
        raise _fail("the cutoff must be in the past; nothing can have gone silent yet")
    registered: set[str] = set()
    if skip_registered_within_days is not None:
        if from_run is not None:
            raise _fail(
                "--skip-registered-within-days only changes discovery, which --from-run skips"
            )
        if chosen_event.custom:
            raise _fail(
                "--skip-registered-within-days reads the shared registry, which has no "
                f"records of the custom event {chosen_event.id}"
            )
        registered = _registered(
            registry, platform, when_now, skip_registered_within_days, chosen_event.id
        )
    elif registry is not None:
        raise _fail("--registry only applies with --skip-registered-within-days")

    override: list[str] | None = None
    if subreddit:
        if platform not in (*REDDIT_PLATFORMS, "fake"):
            raise _fail("--subreddit only applies to --platform reddit or reddit-archive")
        override = subreddit
    if query:
        if platform not in ("x", "bluesky", "fake"):
            raise _fail("--query only applies to --platform x or bluesky")
        if override:
            raise _fail("use either --subreddit or --query, not both")
        override = query
    if platform not in SEED_KEYS:
        raise _fail(
            f"unknown platform {platform!r}; choose reddit, reddit-archive, x, bluesky or fake"
        )
    if max_usd is not None and platform != "x":
        raise _fail("--max-usd only applies to --platform x")
    try:
        seeds = load_seeds(platform, seeds_path, override)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    if from_run is not None and not override and seeds_path is None:
        # The run stores the seeds it used; prefer them over today's bundled list.
        stored = stored_run["params"].get("seeds")
        if isinstance(stored, list) and stored and all(isinstance(i, str) for i in stored):
            seeds = SeedSet(platform=platform, items=tuple(stored))

    try:
        source_cls = load_source_class(platform)
        credentials = collect_credentials(source_cls, _credential_environ(hooks))
    except (AdapterNotAvailable, MissingCredentials) as exc:
        raise _fail(str(exc)) from None
    logged_in = source_cls.has_login(credentials)
    if from_run is not None and stored_run is not None:
        dropped = dropped_credential_destinations(
            stored_run["params"], explicit_options, source_cls.credential_destination_options
        )
        if dropped and logged_in:
            key, value = next(iter(dropped.items()))
            raise _fail(
                f"run {from_run} used --source-option {key}={redact_userinfo(value)}, which "
                f"decides where the {platform} login is sent, so --from-run does not take it "
                "from the database. Pass it again (--source-option "
                f"{key}=...) to continue with it, or unset the login variables."
            )
    if logged_in:
        # A login sent anywhere but the adapter's default host is always announced.
        try:
            moved = source_cls.credential_destinations(options) - (
                source_cls.credential_destinations(None)
            )
        except ValueError:
            moved = frozenset()  # The adapter reports the bad option below.
        for host in sorted(moved):
            typer.secho(
                f"note: the {platform} login is sent to {host} (set by --source-option), "
                "not to the default host.",
                fg=typer.colors.YELLOW,
                err=True,
            )
    if concurrency > source_cls.max_concurrency:
        if source_cls.max_concurrency == 1:
            raise _fail(
                f"--concurrency does not apply to --platform {platform}; it fetches one "
                "account at a time"
            )
        raise _fail(
            f"--concurrency must be at most {source_cls.max_concurrency} for --platform "
            f"{platform}, got {concurrency}"
        )
    if baseline_sample_rate is not None:
        if not 0.0 < baseline_sample_rate <= 1.0:
            raise _fail(f"--baseline-sample-rate must be in (0, 1], got {baseline_sample_rate:g}")
        if not source_cls.supports_baseline_sampling:
            raise _fail(
                f"--baseline-sample-rate does not apply to --platform {platform}: it cannot "
                "fetch an older part of an account's history without paging through the newer "
                "part again, so the two-phase fetch would save nothing"
            )
    if from_run is not None:
        stored_rate = ((stored_run or {}).get("params") or {}).get("baseline_sample_rate")
        if baseline_sample_rate is not None and (
            stored_rate is None or float(stored_rate) != baseline_sample_rate
        ):
            # Not even with --force: the stored design depends on the rate.
            raise _fail(
                f"--baseline-sample-rate {baseline_sample_rate:g} differs from run {from_run}'s "
                f"({'none, a census' if stored_rate is None else f'{float(stored_rate):g}'}); "
                "a run chain keeps one rate. Drop the flag to continue with the run's value."
            )
        baseline_sample_rate = None if stored_rate is None else float(stored_rate)
    if active_within_days is not None:
        if not source_cls.supports_active_within:
            raise _fail(f"--active-within-days does not apply to --platform {platform}")
        if from_run is not None:
            raise _fail(
                "--active-within-days only changes discovery, which --from-run skips; "
                "drop it to continue the run's stored sample"
            )
    resumed_params: dict[str, Any] = (
        stored_run["params"] if from_run is not None and stored_run is not None else {}
    )
    if from_run is not None:
        # Discovery is skipped on a resume, so the flag itself is refused above; the
        # stored value is inherited for the record (run params and report output).
        active_within_days = stored_active_within(resumed_params)
    stored_pool = from_run is not None and "pool_factor" in resumed_params
    if pool_factor is None and not stored_pool and source_cls.default_pool_factor is not None:
        # A fresh scan without --pool-factor uses the platform's own default. A resumed
        # run keeps the stored value (discovery does not run again anyway); one that has
        # none records the default that applies to the platform, not the core default.
        values["pool_factor"] = source_cls.default_pool_factor

    scoring = ScoringParams(
        cutoff=when_cutoff,  # type: ignore[arg-type]
        now=when_now,  # type: ignore[arg-type]
        baseline_days=int(values["baseline_days"]),
        min_silence_days=float(values["min_silence_days"]),
        min_active_days=int(values["min_active_days"]),
        min_posts=int(values["min_posts"]),
        recent_days=float(values["recent_days"]),
        max_weekly_cv=float(values["max_weekly_cv"]),
        cliff_window_hours=float(values["cliff_window_hours"]),
        sync_minutes=float(values["sync_minutes"]),
        placebo_weeks=placebo_weeks,
        **_scan_markers(chosen_event, stored_run if from_run is not None else None),
    )
    if active_within_days is not None and from_run is None:
        # Refuse a lookback that cannot give a usable discovery window whatever the
        # archive horizon turns out to be, before the probe and coverage check spend
        # any request on it.
        problem = source_cls.active_within_problem(
            days=active_within_days,
            now=scoring.now,
            cutoff=scoring.cutoff,
            baseline_start=scoring.baseline_start,
            min_silence_days=scoring.min_silence_days,
            allow_stale_source=allow_stale_source,
        )
        if problem is not None:
            raise _fail(problem)
    effective_max_requests = values["max_requests"]
    settings = ScanSettings(
        platform=platform,
        scoring=scoring,
        n=int(values["n"]),
        new_accounts_days=values["new_accounts_days"],
        pool_factor=float(values["pool_factor"]),
        seed=values["seed"],
        cache_ttl_hours=cache_ttl_hours,
        max_consecutive_errors=max_consecutive_errors,
        max_requests=effective_max_requests,
        source_options=options,
        refresh=refresh,
        secret_options=tuple(source_cls.secret_options),
        allow_stale_source=allow_stale_source,
        inherited_unstored_keys=tuple(inherited_unstored),
        active_within_days=active_within_days,
        concurrency=concurrency,
        baseline_sample_rate=baseline_sample_rate,
        event_id=chosen_event.id,
        now_given=now is not None,
    )

    def progress(done: int, total: int) -> None:
        if hooks is not None and hooks.on_progress is not None:
            hooks.on_progress(done, total)
        if done % 100 == 0 or done == total:
            typer.echo(f"  fetched {done}/{total}", err=True)

    with Storage(db) as storage:
        # One pacer for the whole scan, sized by the adapter's class attributes.
        if trust_env:
            typer.secho(
                "warning: --trust-env is on: a proxy from the environment sees every host "
                "this scan contacts, and a CA from SSL_CERT_FILE or SSL_CERT_DIR could let "
                "it read the requests, credentials included.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        http = HttpClient(
            max_requests=effective_max_requests,
            requests_per_second=source_cls.default_rps,
            burst=source_cls.default_burst,
            timeout=source_cls.default_timeout,
            trust_env=trust_env,
        )
        if hooks is not None and hooks.on_client is not None:
            hooks.on_client(http)
        config = SourceConfig(
            http=http,
            credentials=credentials,
            now=scoring.now,
            cutoff=scoring.cutoff,
            seed=settings.seed,
            options=options,
            baseline_start=scoring.baseline_start,
            ledger=SpendLedger(storage, platform),
            min_posts=scoring.min_posts,
            active_within_days=active_within_days,
            concurrency=concurrency,
        )
        try:
            source = source_cls(config)
        except (KeyError, TypeError, ValueError) as exc:
            http.close()
            raise _fail(f"invalid adapter configuration: {exc}") from None

        # The pipeline asks for the data horizon before discovery (and a resume skips
        # discovery), so let the adapter see the seeds up front.
        note_seeds = getattr(source, "note_seeds", None)
        if callable(note_seeds):
            note_seeds(seeds)
        if redactor is not None:
            _watch_accounts(source, redactor)
        skipped_registered = _skip_registered(source, registered) if registered else None

        typer.echo(NOTICE, err=True)
        _print_event(chosen_event)
        if scoring.placebo_weeks and baseline_sample_rate is None and not dry_run_cost:
            problem = source.placebo_discovery_problem(seeds)
            typer.echo(
                _hide(
                    f"Placebo comparison not available on {platform}: {problem}."
                    if problem is not None
                    else f"After the scan, {scoring.placebo_weeks} placebo samples of "
                    f"{settings.n} accounts each are discovered and fetched 1 to "
                    f"{scoring.placebo_weeks} weeks earlier, which repeats discovery and "
                    "fetching that many times (--placebo-weeks 0 turns it off)."
                ),
                err=True,
            )
        # An adapter whose seeds can draw from different sampling frames says which.
        sampling_frame = getattr(source, "sampling_frame", None)
        frame = sampling_frame() if callable(sampling_frame) else None
        typer.echo(
            _hide(frame)
            if frame
            else "Sampling is random among accounts active in the seed communities, "
            "not among all users.",
            err=True,
        )
        try:
            plan = source.plan_spend(
                seeds,
                # A resume fetches the stored sample, whatever --n says.
                n=resumed_size if resumed_size is not None else settings.n,
                pool_limit=math.ceil(settings.pool_factor * settings.n),
                resume=from_run is not None,
            )
        except Exception:
            source.close()
            raise
        if plan is not None:
            for line in plan.lines:
                typer.echo(_hide(line), err=True)
            if plan.refuse:
                source.close()
                raise _fail(plan.refuse)
            if plan.confirm:
                typer.secho(_hide(f"warning: {plan.confirm}"), fg=typer.colors.YELLOW, err=True)
                if not yes:
                    source.close()
                    raise _fail(f"{plan.confirm} Rerun with --yes to start anyway.")
            if plan.paid_discovery and from_run is None and not yes:
                source.close()
                raise _fail(
                    "discovery on this platform is billed (projection above). Rerun with "
                    "--yes to confirm, or continue a stored sample with --from-run RUN_ID."
                )

        if dry_run_cost and from_run is not None:
            try:
                resumed = load_run_sample(storage, from_run, platform)
                projection = source.cost_projection(
                    resumed.accounts, scoring.fetch_start, scoring.now
                )
            finally:
                _print_cost(source)
                source.close()
            _print_warnings(source.drain_warnings())
            typer.echo(_hide(projection or "This platform reports no cost projection."))
            return

        try:
            with keep_awake(not allow_sleep):
                outcome = run_scan(
                    source,
                    seeds,
                    storage,
                    settings,
                    progress,
                    from_run=from_run,
                    sample_only=dry_run_cost,
                    on_run=hooks.on_run if hooks is not None else None,
                )
            projection = None
            if dry_run_cost:
                sampled = [acct for acct, _ in storage.get_run_sample(outcome.run_id)]
                projection = source.cost_projection(sampled, scoring.fetch_start, scoring.now)
        except RunInUse as exc:
            raise _fail(str(exc)) from None
        except SourceAuthError as exc:
            raise _fail(f"authentication failed: {exc}", 3) from None
        except SourceError as exc:
            raise _fail(str(exc), 4) from None
        finally:
            _print_cost(source)
            source.close()

    _learn_outcome(redactor, outcome)
    if skipped_registered is not None:
        typer.echo(
            f"Skipped {skipped_registered[0]} discovered accounts that the registry records "
            f"as scanned within the last {skip_registered_within_days:g} days.",
            err=True,
        )
    if dry_run_cost:
        _print_warnings(outcome.warnings)
        typer.echo(_hide(projection or "This platform reports no cost projection."))
        command = resume_command(
            platform, outcome.run_id, db, values, options, event_id=chosen_event.id
        )
        typer.echo(
            _hide(
                f"Stored the {outcome.summary.get('sampled', 0)}-account sample as run "
                f"{outcome.run_id}; no activity was fetched. Fetch it without new discovery "
                f"(the run's options, budget included, are inherited): {command}"
            )
        )
        return

    _print_warnings(outcome.warnings)
    if effective_max_requests is not None:
        typer.echo(f"Requests used: {outcome.requests_made}/{effective_max_requests}", err=True)
    request_stats = outcome.summary.get("requests")
    if isinstance(request_stats, dict):
        typer.echo(format_request_summary(request_stats), err=True)
    if active_within_days is not None:
        typer.echo(_discovery_line(active_within_days, from_run), err=True)
    _warn_provisional(outcome)
    _print_summary(outcome, platform, hide_handles=hide_handles)
    _write_output(outcome, platform, out, fmt, include_unavailable)
    again = f" --event {chosen_event.id}" if event_named else ""
    typer.echo(
        "Re-score later without refetching: bot-farm-silence-tracker report "
        f"--run-id {outcome.run_id}{again}"
    )
    if outcome.status != "complete":
        secrets = tuple(source_cls.secret_options)
        command = resume_command(
            platform,
            outcome.run_id,
            db,
            values,
            options,
            event_id=chosen_event.id,
            secret_options=secrets,
            allow_stale_source=allow_stale_source,
        )
        typer.echo(_hide(f"Continue this sample without new discovery: {command}"))
        dropped = settings.to_dict()["unstored_option_keys"]
        if dropped:
            typer.echo(
                "These --source-option values were not stored because they look like "
                "secrets or carry credentials; pass them again when you continue: "
                + ", ".join(dropped)
            )


@app.command()
def report(
    run_id: Annotated[
        int | None, typer.Option("--run-id", help="Run to re-score from the cache.")
    ] = None,
    event: EventOption = None,
    registry: Annotated[
        Path | None,
        typer.Option(
            "--registry",
            help="Instead of re-scoring a run, write local report drafts for the registry "
            "records of --event with --status on --platform to the --out directory. "
            "Drafts are for people to check and submit by hand; nothing is ever submitted.",
        ),
    ] = None,
    status: Annotated[
        str, typer.Option("--status", help="With --registry: the record status to draft.")
    ] = "verified_inauthentic",
    include_unverified: Annotated[
        bool,
        typer.Option(
            "--include-unverified",
            help="With --registry: allow a --status other than verified_inauthentic. Every "
            'such draft is marked "UNVERIFIED — do not submit".',
        ),
    ] = False,
    allow_inside_repo: AllowInsideRepoOption = False,
    platform: Annotated[
        str | None,
        typer.Option("--platform", help="With --registry: reddit, reddit-archive or bluesky."),
    ] = None,
    min_silence_days: Annotated[float | None, typer.Option("--min-silence-days")] = None,
    min_active_days: Annotated[int | None, typer.Option("--min-active-days")] = None,
    min_posts: Annotated[int | None, typer.Option("--min-posts")] = None,
    recent_days: Annotated[float | None, RecentDaysOption] = None,
    max_weekly_cv: Annotated[float | None, MaxWeeklyCvOption] = None,
    cliff_window_hours: Annotated[float | None, typer.Option("--cliff-window-hours")] = None,
    sync_minutes: Annotated[float | None, typer.Option("--sync-minutes")] = None,
    out: OutOption = None,
    fmt: FormatOption = None,
    db: DbOption = None,
    include_unavailable: Annotated[bool, typer.Option("--include-unavailable")] = False,
    hide_handles: HideHandlesOption = False,
) -> None:
    """Re-score a previous run from the cache (no network). Cutoff and window are fixed.

    The run is read from its event's database (``--event``, or ``--db``); a run of
    another event is refused. Works on a read-only database: :class:`Storage` then
    opens it read-only and skips migrations.
    """
    redactor = _start_hiding(hide_handles)
    chosen_event, event_named = _resolve_event(event)
    if registry is not None:
        if run_id is not None:
            raise _fail("use either --run-id or --registry, not both")
        _report_drafts(
            registry, status, platform, out, include_unverified, allow_inside_repo, chosen_event
        )
        return
    if run_id is None:
        raise _fail("give --run-id (re-score a run) or --registry (report drafts)")
    _resolve_format(fmt, out)
    db = _event_db(db, chosen_event)
    with Storage(db) as storage:
        stored = storage.get_run(run_id)
        own_event = (
            _check_run_event(stored, run_id, chosen_event, event_named, db)
            if stored is not None
            else chosen_event
        )

        def fetch_command(new_min_posts: int) -> str:
            params = (stored or {}).get("params") or {}
            values = {**_stored_resumable(params), "min_posts": new_min_posts}
            command = resume_command(
                str((stored or {}).get("platform") or "unknown"),
                run_id,
                db,
                values,
                params.get("source_options") or {},
                event_id=own_event.id,
            )
            return f"{command} --force"

        try:
            outcome = rescore_run(
                storage,
                run_id,
                fetch_command=fetch_command,
                min_silence_days=min_silence_days,
                min_active_days=min_active_days,
                min_posts=min_posts,
                recent_days=recent_days,
                max_weekly_cv=max_weekly_cv,
                cliff_window_hours=cliff_window_hours,
                sync_minutes=sync_minutes,
            )
        except (LookupError, ValueError) as exc:
            raise _fail(str(exc)) from None
        run = storage.get_run(run_id)
    platform = run["platform"] if run else "unknown"
    typer.echo(NOTICE, err=True)
    _print_event(own_event)
    lookback = stored_active_within((run or {}).get("params") or {})
    if lookback is not None:
        typer.echo(
            _discovery_line(lookback, (run or {}).get("params", {}).get("from_run")), err=True
        )
    _learn_outcome(redactor, outcome)
    _print_warnings(outcome.warnings)
    _warn_provisional(outcome)
    _print_summary(outcome, platform, hide_handles=hide_handles)
    _write_output(outcome, platform, out, fmt, include_unavailable)


@app.command()
def network(
    platform: Annotated[
        list[str] | None,
        typer.Option(
            "--platform",
            "-p",
            help="Platform to describe (repeatable) [default: every platform].",
        ),
    ] = None,
    source_option: Annotated[
        list[str] | None,
        typer.Option(
            "--source-option",
            help="key=value adapter option that changes the hosts (Bluesky's pds_url).",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show which hosts each platform contacts and which credentials go where (no network).

    Generated from the same declarations the HTTP client enforces as its host
    allowlist. Credentials are named by environment variable; values are never read
    or printed.
    """
    try:
        options = parse_source_options(source_option)
        report = network_report(platform, options)
    except (AdapterNotAvailable, ValueError) as exc:
        raise _fail(str(exc)) from None
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        typer.echo(format_network(report))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def git_toplevel(path: Path) -> Path | None:
    """The git work tree that would hold ``path`` (which need not exist yet), or ``None``.

    Asks ``git rev-parse --show-toplevel`` in the nearest existing directory; without
    git, looks for a ``.git`` entry in that directory and its parents.
    """
    import subprocess

    probe = path.resolve()
    while not probe.is_dir():
        if probe.parent == probe:
            return None
        probe = probe.parent
    try:
        done = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        for folder in (probe, *probe.parents):
            if (folder / ".git").exists():
                return folder
        return None
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def _refuse_inside_repo(out: Path, allow: bool, what: str) -> None:
    top = git_toplevel(out)
    if top is None:
        return
    if not allow:
        raise _fail(
            f"--out {out} is inside the git repository {top}; {what} contain real account "
            "names and must never be committed. Write them elsewhere, or pass "
            "--allow-inside-repo if the path is ignored and you are sure."
        )
    typer.secho(
        f"warning: writing {what} inside the git repository {top}. Make sure the path is "
        "ignored and never committed.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _shared_event(event: outage_events.OutageEvent, registry: Path) -> outage_events.EventCatalog:
    """The registry's events, after checking ``event`` is one of them (not custom)."""
    if event.custom:
        raise _fail(
            f"{event.id} is a local-only custom event; the shared registry never holds "
            "custom events. Add the event to events.toml in its own pull request first."
        )
    try:
        catalog = outage_events.registry_events(registry)
    except outage_events.EventError as exc:
        raise _fail(str(exc)) from None
    if event.id not in catalog:
        raise _fail(f"{event.id} is not an event of the registry's {catalog.source}")
    return catalog


def _report_drafts(
    registry: Path,
    status: str,
    platform: str | None,
    out: Path | None,
    include_unverified: bool = False,
    allow_inside_repo: bool = False,
    event: outage_events.OutageEvent | None = None,
) -> None:
    from bot_farm_silence_tracker.registry.records import STATUS_VERIFIED, STATUSES
    from bot_farm_silence_tracker.registry.reports import DRAFT_PLATFORMS, build_drafts

    if event is None:
        event = _resolve_event(None)[0]

    if platform is None:
        raise _fail(f"--registry needs --platform ({', '.join(DRAFT_PLATFORMS)})")
    if out is None:
        raise _fail("--registry needs --out, a directory for the local drafts")
    if status not in STATUSES:
        raise _fail(f"--status must be one of {', '.join(STATUSES)}")
    if not registry.is_dir():
        raise _fail(f"no registry directory at {registry}")
    if _inside(out, registry):
        raise _fail(
            "--out must be outside the registry: drafts contain real account names and "
            "must never be committed"
        )
    if status != STATUS_VERIFIED and not include_unverified:
        raise _fail(
            f"report drafts are only for {STATUS_VERIFIED} records; pass "
            "--include-unverified to draft others for review"
        )
    catalog = _shared_event(event, registry)
    _refuse_inside_repo(out, allow_inside_repo, "report drafts")
    if status != STATUS_VERIFIED:
        typer.secho(
            f"WARNING: drafting {status} records, which reviewers have NOT verified. Every "
            'draft is marked "UNVERIFIED — do not submit". Do not submit any of them.',
            fg=typer.colors.RED,
            bold=True,
            err=True,
        )
    refused: list[Any] = []
    try:
        drafts = build_drafts(
            registry,
            status,
            platform.lower(),
            out,
            event_id=event.id,
            events=catalog,
            include_unverified=include_unverified,
            refused=refused,
        )
    except ValueError as exc:
        raise _fail(str(exc)) from None
    for refusal in refused:
        typer.secho(
            f"Refused to draft {refusal.platform} {refusal.id_b64}: {'; '.join(refusal.reasons)}.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    typer.echo(
        f"Wrote {len(drafts)} report drafts for the event {event.id} (cutoff "
        f"{event.cutoff_text}) to {out / event.id}"
    )
    for draft in drafts:
        if draft.also_in:
            typer.echo(
                f"  {draft.platform} {draft.id_b64} also appears in event "
                + ", ".join(draft.also_in)
            )
    typer.secho(
        "warning: the drafts contain real account names. Keep them private. Nothing was "
        "submitted: a person must check each draft and submit it by hand.",
        fg=typer.colors.YELLOW,
        err=True,
    )


@app.command("export-ids")
def export_ids(
    out: Annotated[
        Path, typer.Option("--out", help="File to write (.csv or .json), outside any repository.")
    ],
    run_id: Annotated[
        int | None, typer.Option("--run-id", help="Run to export [default: all runs].")
    ] = None,
    all_runs: Annotated[
        bool, typer.Option("--all-runs", help="Export every run of the event's database.")
    ] = False,
    event: EventOption = None,
    fmt: FormatOption = None,
    db: DbOption = None,
    allow_inside_repo: AllowInsideRepoOption = False,
) -> None:
    """Write real platform IDs, handles and scan facts of a run to a local file.

    Local only: nothing is uploaded. The file contains real accounts and must stay
    private. Accounts that opted out of logged-out views are never written. Runs are
    read from the event's database (``--event``, or ``--db``).
    """
    from bot_farm_silence_tracker.export import PRIVATE_WARNING, export_rows, write_export

    if (run_id is None) == (not all_runs):
        raise _fail("give exactly one of --run-id N or --all-runs")
    resolved = _resolve_format(fmt, out)
    _refuse_inside_repo(out, allow_inside_repo, "exported IDs")
    chosen_event, event_named = _resolve_event(event)
    db = _event_db(db, chosen_event)
    with Storage(db) as storage:
        ids = storage.run_ids() if all_runs else [int(run_id)]  # type: ignore[arg-type]
        kept: list[int] = []
        for one in ids:
            stored = storage.get_run(one)
            if stored is None:
                raise _fail(f"no run with id {one}")
            if not all_runs:
                _check_run_event(stored, one, chosen_event, event_named, db)
            elif event_named and _run_event(stored, one).id != chosen_event.id:
                continue  # --all-runs --event: only that event's runs.
            kept.append(one)
        if all_runs and len(kept) < len(ids):
            typer.echo(
                f"Left out {len(ids) - len(kept)} runs of other events than {chosen_event.id}.",
                err=True,
            )
        ids = kept
        try:
            rows = export_rows(storage, ids)
        except (LookupError, ValueError) as exc:
            raise _fail(str(exc)) from None
    write_export(out, rows, resolved)
    typer.echo(f"Wrote {len(rows)} rows to {out}")
    typer.secho(PRIVATE_WARNING.format(path=out), fg=typer.colors.YELLOW, bold=True, err=True)


registry_app = typer.Typer(
    no_args_is_help=True,
    help="The shared scan registry: base64-encoded identities (obfuscation for a trusted "
    "research group, reversible by anyone, not anonymity), statuses changed only by "
    "reviewed pull requests.",
)
app.add_typer(registry_app, name="registry")


def _registry_dir(registry: Path | None) -> Path:
    return registry or DEFAULT_REGISTRY


NoLiveCheckOption = Annotated[
    bool,
    typer.Option(
        "--no-live-check",
        help="Make no request: skip the live profile check. Bluesky accounts then stay "
        "scanned (and an evidence draft says not checked); Reddit and X are never "
        "checked automatically.",
    ),
]


def _live_checker() -> Any:
    """The live profile checker of the registry commands (tests replace it)."""
    from bot_farm_silence_tracker.registry.live import LiveChecker

    return LiveChecker()


@contextlib.contextmanager
def _live_check(skip: bool) -> Iterator[Any]:
    """The check function for ``registry add`` and ``registry evidence``."""
    from bot_farm_silence_tracker.registry.live import no_live_check

    if skip:
        yield no_live_check
        return
    checker = _live_checker()
    try:
        yield checker.check
    finally:
        checker.close()


@registry_app.command("add")
def registry_add(
    run_id: Annotated[int, typer.Option("--run-id", help="Run whose accounts to merge.")],
    event: EventOption = None,
    registry: RegistryOption = None,
    db: DbOption = None,
    no_live_check: NoLiveCheckOption = False,
) -> None:
    """Merge a run's scanned accounts into its event's folder of the registry.

    The run is read from its event's database (``--event``, or ``--db``) and must
    belong to that event; custom events are refused. Nothing is fetched except the
    live profile check. New accounts become ``candidate`` when they are a
    non-provisional cliff with at least one signal independent of the outage's timing
    (a synchronized stop alone is not enough) and the live profile check did not find
    them suspended or deleted, or fail (Bluesky: one logged-out getProfile each;
    Reddit and X: not checked, the verifier resolves it); else ``scanned``. Existing
    records of the same event are updated by a newer scan; ``verified_inauthentic``
    and ``cleared`` are never changed. Opted-out and unavailable accounts are left out.
    """
    from bot_farm_silence_tracker.registry.build import add_run

    root = _registry_dir(registry)
    chosen_event, event_named = _resolve_event(event)
    db = _event_db(db, chosen_event)
    with Storage(db) as storage:
        stored = storage.get_run(run_id)
        if stored is None:
            raise _fail(f"no run with id {run_id}")
        own = _check_run_event(stored, run_id, chosen_event, event_named, db)
        catalog = _shared_event(own, root)
        try:
            with _live_check(no_live_check) as live:
                stats = add_run(root, storage, run_id, catalog, live)
        except (LookupError, ValueError) as exc:
            raise _fail(str(exc)) from None
    typer.echo(
        f"Registry {root}, event {stats.event_id}: {stats.added} added, {stats.updated} updated "
        f"({stats.to_candidate} became candidate, {stats.to_scanned} back to scanned), "
        f"{stats.unchanged} unchanged; {len(stats.changed_files)} files changed."
    )
    for note in stats.notes:
        typer.secho(f"note: {note}", fg=typer.colors.YELLOW, err=True)
    if stats.excluded:
        left = ", ".join(
            f"{count} {reason.replace('_', ' ')}" for reason, count in stats.excluded.items()
        )
        typer.echo(f"Left out: {left}.")
    if stats.live_blocked:
        kept = ", ".join(f"{count} {status}" for status, count in stats.live_blocked.items())
        typer.secho(
            f"Kept scanned by the live profile check, not candidate: {kept}. A suspended or "
            "deleted account was removed, not silent; a check that failed (not checked) can "
            "be repeated by running registry add again.",
            fg=typer.colors.YELLOW,
            err=True,
        )


@registry_app.command("merge")
def registry_merge(
    registry: RegistryOption = None,
    other: Annotated[
        list[Path] | None,
        typer.Option("--from", help="Another registry directory to merge in (repeatable)."),
    ] = None,
) -> None:
    """Dedupe across shards (and --from registries), validate, sort and rewrite.

    Covers every event. Records are only merged within one event, and every record
    must belong to an event of the registry's events file.
    """
    from bot_farm_silence_tracker.registry.records import (
        load_records,
        merge_records,
        min_silence_of,
        write_records,
    )

    root = _registry_dir(registry)
    try:
        catalog = outage_events.registry_events(root)
        records = load_records(root, events=catalog)
        for extra in other or []:
            if not extra.is_dir():
                raise _fail(f"no registry directory at {extra}")
            for key, record in load_records(extra, events=catalog).items():
                least = min_silence_of(key[0], catalog)
                records[key] = (
                    merge_records(records[key], record, min_silence=least)
                    if key in records
                    else record
                )
    except ValueError as exc:
        raise _fail(f"cannot merge an invalid registry: {exc}") from None
    changed = write_records(root, records.values())
    typer.echo(f"Registry {root}: {len(records)} records; {len(changed)} files changed.")


@registry_app.command("validate")
def registry_validate(
    registry: RegistryOption = None,
    ref: Annotated[
        str | None,
        typer.Option(
            "--ref",
            help="Validate the registry at this git commit instead (read as git objects; "
            "--registry is then the path inside the repository).",
        ),
    ] = None,
    repo: Annotated[Path, typer.Option("--repo", help="Git repository for --ref.")] = Path("."),
) -> None:
    """Check the registry and its events file; fails on an invalid events.toml,
    duplicates, schema and rule errors, bad order, unexpected files or modes, records
    outside their event's folder or of an unknown event, future timestamps or missing
    evidence.

    Covers every event. Every ``verified_inauthentic`` record needs its evidence file.
    """
    from bot_farm_silence_tracker.registry.records import RegistryTree, file_kind, validate_registry

    root = _registry_dir(registry)
    if ref is not None:
        tree = RegistryTree(repo=repo, ref=ref, registry_path=root.as_posix())
    elif not root.is_dir():
        raise _fail(f"no registry directory at {root}")
    else:
        tree = RegistryTree(root)
    try:
        problems = validate_registry(tree=tree)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    for problem in problems:
        typer.secho(f"error: {problem}", fg=typer.colors.RED, err=True)
    if problems:
        raise typer.Exit(1)
    count = sum(tree.read(rel).count(b"\n") for rel in tree.modes() if file_kind(rel) == "records")
    typer.echo(f"Registry {tree.describe()} is valid ({count} records).")


@registry_app.command("evidence")
def registry_evidence(
    run_id: Annotated[int, typer.Option("--run-id", help="Run that scanned the account.")],
    account: Annotated[str, typer.Option("--account", help="The account's handle or ID.")],
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="File to write [default: "
            "<registry>/events/<event-id>/evidence/<platform>/<id_b64>.md].",
        ),
    ] = None,
    event: EventOption = None,
    registry: RegistryOption = None,
    db: DbOption = None,
    no_live_check: NoLiveCheckOption = False,
) -> None:
    """Draft an evidence file for one account from the cache.

    The run is read from its event's database (``--event``, or ``--db``); the draft
    goes to that event's evidence folder. Nothing is fetched except the live profile
    check (Bluesky: one logged-out getProfile; Reddit and X: not checked), whose
    result the draft records. The draft keeps the identity base64-encoded, so it can
    be committed with the pull request that changes the account's status. People
    check and complete it.
    """
    from bot_farm_silence_tracker.registry.evidence import draft_evidence, write_evidence
    from bot_farm_silence_tracker.registry.records import evidence_path, registry_evidence_path

    root = _registry_dir(registry)
    chosen_event, event_named = _resolve_event(event)
    db = _event_db(db, chosen_event)
    with Storage(db) as storage:
        stored = storage.get_run(run_id)
        if stored is None:
            raise _fail(f"no run with id {run_id}")
        own = _check_run_event(stored, run_id, chosen_event, event_named, db)
        catalog = _shared_event(own, root)
        try:
            with _live_check(no_live_check) as live:
                text, summary = draft_evidence(storage, run_id, account, catalog, live)
        except (LookupError, ValueError) as exc:
            raise _fail(str(exc)) from None
    target = out or root / registry_evidence_path(own.id, summary["platform"], summary["id_b64"])
    write_evidence(target, text)
    typer.echo(f"Wrote the evidence draft for {summary['id_b64']} to {target}")
    typer.echo(
        f'Reference it as "evidence": "{evidence_path(summary["platform"], summary["id_b64"])}" '
        f"(relative to registry/events/{own.id}/) in a pull request; it needs two approving "
        "reviews to set verified_inauthentic."
    )
    live = (summary.get("live_profile") or {}).get("status")
    typer.echo(
        f"Live profile check: {live}. Before ticking 'The behavior is inauthentic', fill in "
        "the reviewer findings: what the live profile shows, and at least one finding "
        "beyond the silence. The registry gate refuses a verdict without them."
    )


MaxRequestsOption = Annotated[
    int, typer.Option("--max-requests", min=1, help="Request budget for the whole check.")
]
TimeoutOption = Annotated[
    float, typer.Option("--timeout-minutes", min=0.1, help="Overall time limit.")
]
ToleranceOption = Annotated[
    float,
    typer.Option("--tolerance-minutes", min=0.0, help="Match window around last activity."),
]
LagOption = Annotated[
    float | None,
    typer.Option(
        "--ingestion-lag-hours",
        min=0.0,
        help="Source ingestion lag [default: 6 for reddit-archive, 1 for bluesky].",
    ),
]
SummaryOption = Annotated[
    Path | None,
    typer.Option("--summary", help="Append the Markdown summary to this file."),
]


def _append_summary(summary: Path | None, text: str) -> None:
    if summary is not None:
        with summary.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")


@registry_app.command("verify")
def registry_verify(
    base: Annotated[str, typer.Option("--base", help="Base side: a git ref or a registry dir.")],
    head: Annotated[str, typer.Option("--head", help="Head side: a git ref or a registry dir.")],
    repo: Annotated[Path, typer.Option("--repo", help="Git repository for refs.")] = Path("."),
    registry_path: Annotated[
        str, typer.Option("--registry-path", help="Registry directory inside the repository.")
    ] = "registry",
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="A pull request label, such as registry-override."),
    ] = None,
    seed: Annotated[
        str | None,
        typer.Option("--seed", help="Seed for sampling a large change [default: the head ref]."),
    ] = None,
    max_requests: MaxRequestsOption = 200,
    timeout_minutes: TimeoutOption = 15.0,
    tolerance_minutes: ToleranceOption = 5.0,
    lag_hours: LagOption = None,
    summary: SummaryOption = None,
) -> None:
    """Apply the registry policy to a change and spot-check it against the live sources.

    Covers every event, each record with its own event's cutoff and windows. Fails
    (exit 1) on an invalid head events.toml, on a change to an existing event's name,
    cutoff or windows and on a renamed or deleted event (allowed by --label
    registry-override), on removed records, human statuses moved back, large changes,
    status changes on platforms that cannot be checked, username changes of
    verified_inauthentic or cleared records, or evidence changes without a transition
    into verified_inauthentic (each allowed by --label registry-override); on a
    checked record that claims activity that does not exist or hides activity; on too
    many inconclusive results; and on invalid lines.
    """
    from datetime import timedelta

    from bot_farm_silence_tracker.registry.gate import check_events, render_events
    from bot_farm_silence_tracker.registry.verify import (
        SourceProbe,
        evidence_changes,
        evidence_reviews,
        load_tree,
        render_summary,
        run_verify,
        side_tree,
    )

    try:
        base_tree = side_tree(base, repo=repo, registry_path=registry_path)
        head_tree = side_tree(head, repo=repo, registry_path=registry_path)
        check = check_events(base_tree, head_tree, label or ())
        if check.head is None:
            for problem in check.problems:
                typer.secho(f"error: {problem}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        base_records, _ = load_tree(
            base_tree, events=check.base if check.base is not None else check.head
        )
        head_records, problems = load_tree(head_tree, events=check.head)
        evidence = evidence_changes(base_tree, head_tree)
        reviews = evidence_reviews(base_records, head_records, head_tree)
    except (ValueError, OSError) as exc:
        raise _fail(str(exc)) from None
    probe = SourceProbe(max_requests=max_requests, timeout=timedelta(minutes=timeout_minutes))
    try:
        result = run_verify(
            base_records,
            head_records,
            probe,
            seed=seed or head,
            labels=label or (),
            tolerance=timedelta(minutes=tolerance_minutes),
            lag=None if lag_hours is None else timedelta(hours=lag_hours),
            problems=problems,
            evidence=evidence,
            events=check.head,
            reviews=reviews,
        )
    finally:
        probe.close()
    result.failures = check.failures + result.failures
    lines = render_events(check.changes, check.overridden)
    text = render_summary(result) + ("\n" + "\n".join(lines) if lines else "")
    typer.echo(text)
    _append_summary(summary, text)
    if not result.ok:
        raise typer.Exit(1)


@registry_app.command("gate")
def registry_gate(
    event: Annotated[
        Path | None,
        typer.Option(
            "--event",
            help="The pull_request_target event payload (GITHUB_EVENT_PATH): number, base "
            "and head SHAs, and labels.",
        ),
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", help="Git repository holding both commits.")
    ] = Path("."),
    base_sha: Annotated[str | None, typer.Option("--base-sha", help="Without --event.")] = None,
    head_sha: Annotated[str | None, typer.Option("--head-sha", help="Without --event.")] = None,
    pr_number: Annotated[int, typer.Option("--pr-number", help="Without --event.")] = 0,
    label: Annotated[
        list[str] | None, typer.Option("--label", help="Without --event: a label (repeatable).")
    ] = None,
    override_statuses: Annotated[
        Path | None,
        typer.Option(
            "--override-statuses",
            help="With --event: the head commit's statuses, one JSON object per line "
            "(context, state, creator), newest first. Without it the registry-override "
            "label counts only on the labeled event that added it.",
        ),
    ] = None,
    override_reviews: Annotated[
        Path | None,
        typer.Option(
            "--override-reviews",
            help="With --event: the pull request's reviews, one JSON object per line "
            "(id, user, state, commit_id). Without it, or without --reviewer-permissions, "
            "the registry-override label is ignored.",
        ),
    ] = None,
    reviewer_permissions: Annotated[
        Path | None,
        typer.Option(
            "--reviewer-permissions",
            help="With --event: the reviewers' repository permissions, one JSON object "
            "per line (user, permission).",
        ),
    ] = None,
    registry_path: Annotated[
        str, typer.Option("--registry-path", help="Registry directory inside the repository.")
    ] = "registry",
    max_requests: MaxRequestsOption = 200,
    timeout_minutes: TimeoutOption = 15.0,
    tolerance_minutes: ToleranceOption = 5.0,
    lag_hours: LagOption = None,
    summary: SummaryOption = None,
) -> None:
    """The required check on every pull request (run from the base branch's code).

    Reads the head commit as git objects only. Passes code-only changes; fails a
    change to the registry together with other files (except registry/schema.json
    with code); validates a data-only change and applies the registry policy and
    spot-checks to it (see `registry verify`).
    """
    from datetime import timedelta

    from bot_farm_silence_tracker.registry.gate import (
        PullRequest,
        override_status_approves,
        render_gate,
        review_approves,
        run_gate,
    )
    from bot_farm_silence_tracker.registry.verify import SourceProbe

    if event is not None:
        try:
            pr = PullRequest.from_event(event)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise _fail(f"cannot read the event payload: {exc}") from None
        if override_statuses is not None:
            try:
                text = override_statuses.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise _fail(f"cannot read the override statuses: {exc}") from None
            approved = override_status_approves(text, pr.number)
            pr = dataclasses.replace(pr, head_approved=approved)
        if override_reviews is not None and reviewer_permissions is not None:
            try:
                reviews = override_reviews.read_text(encoding="utf-8")
                permissions = reviewer_permissions.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise _fail(f"cannot read the override reviews: {exc}") from None
            reviewed = review_approves(reviews, permissions, pr.head_sha, pr.author)
            pr = dataclasses.replace(pr, head_reviewed=reviewed)
    elif base_sha and head_sha:
        pr = PullRequest(pr_number, base_sha, head_sha, frozenset(label or ()), labels_trusted=True)
    else:
        raise _fail("give --event, or --base-sha and --head-sha")
    probe = SourceProbe(max_requests=max_requests, timeout=timedelta(minutes=timeout_minutes))
    try:
        result = run_gate(
            repo,
            pr,
            probe,
            registry_path=registry_path,
            tolerance=timedelta(minutes=tolerance_minutes),
            lag=None if lag_hours is None else timedelta(hours=lag_hours),
        )
    finally:
        probe.close()
    text = render_gate(result, pr.effective_labels)
    typer.echo(text)
    _append_summary(summary, text)
    if not result.ok:
        raise typer.Exit(1)


@registry_app.command("chart")
def registry_chart(
    registry: RegistryOption = None,
    out: Annotated[
        Path, typer.Option("--out", help="Folder for the SVG files [default: docs/charts].")
    ] = Path("docs/charts"),
    event: Annotated[
        list[str] | None,
        typer.Option("--event", "-e", help="Event to chart (repeatable) [default: every event]."),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="UTC date the chart runs to (YYYY-MM-DD) [default: today]."),
    ] = None,
    commit: Annotated[
        str, typer.Option("--commit", help="Commit SHA named in the footer (first 7 shown).")
    ] = "",
) -> None:
    """Draw the README charts: potential bot accounts silent since they stopped.

    Validates the registry with the gate's rules first, then writes
    ``<event>-light.svg`` and ``<event>-dark.svg`` per event, and
    ``default-light.svg`` and ``default-dark.svg`` for the default event. Counts
    only: no username or ID is written. The same input gives the same bytes.
    """
    from datetime import UTC, date, datetime

    from bot_farm_silence_tracker.registry.chart import ChartError, write_charts

    root = _registry_dir(registry)
    if not root.is_dir():
        raise _fail(f"no registry directory at {root}")
    if commit and not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise _fail("--commit must be a hexadecimal commit SHA")
    try:
        day = date.fromisoformat(as_of) if as_of else datetime.now(UTC).date()
    except ValueError:
        raise _fail("--as-of must be a date such as 2026-10-13") from None
    try:
        written = write_charts(root, out, as_of=day, commit=commit, event_ids=event or ())
    except ChartError as exc:
        raise _fail(str(exc)) from None
    for path in written:
        typer.echo(f"Wrote {path}")


@registry_app.command("chart-decision")
def registry_chart_decision(
    event_name: Annotated[str, typer.Option("--event-name", help="GITHUB_EVENT_NAME.")],
    ref: Annotated[str, typer.Option("--ref", help="GITHUB_REF.")],
    sha: Annotated[str, typer.Option("--sha", help="GITHUB_SHA.")],
    default_branch: Annotated[str, typer.Option("--default-branch", help="Default branch.")],
    main_head: Annotated[
        str, typer.Option("--main-head", help="The default branch's current head SHA.")
    ] = "",
    pulls: Annotated[
        Path | None,
        typer.Option("--pulls", help="JSON of GET /repos/{o}/{r}/commits/{sha}/pulls."),
    ] = None,
    runs: Annotated[
        Path | None,
        typer.Option(
            "--runs", help="Workflow runs of the pull requests' heads, one JSON object per line."
        ),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="GITHUB_OUTPUT: gets update=true|false.")
    ] = None,
    summary: SummaryOption = None,
) -> None:
    """Decide whether the Registry chart workflow may redraw the charts (see
    registry/chart_trust.py). Exits 0 either way; the answer is in --output."""
    from bot_farm_silence_tracker.registry.chart_trust import decide, read_json_lines

    try:
        pull_list = read_json_lines(pulls.read_text(encoding="utf-8")) if pulls else []
        run_list = read_json_lines(runs.read_text(encoding="utf-8")) if runs else []
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _fail(f"cannot read the API data: {exc}") from None
    decision = decide(
        event_name=event_name,
        ref=ref,
        sha=sha,
        default_branch=default_branch,
        main_head=main_head,
        pulls=pull_list,
        runs=run_list,
    )
    verdict = "Redrawing the charts." if decision.update else "Not redrawing the charts."
    text = f"## Registry chart\n\n**{verdict}** {decision.reason}\n"
    typer.echo(text)
    _append_summary(summary, text)
    if output is not None:
        with output.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"update={'true' if decision.update else 'false'}\n")


#: Address the ``selftest-tls`` command checks. Any public HTTPS site with a certificate
#: from a common authority works; PyPI's simple index answers a HEAD request cheaply.
SELFTEST_TLS_URL = "https://pypi.org/simple/"


events_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="The outage events scans are measured against (events.toml).",
)
app.add_typer(events_app, name="events")


@events_app.command("list")
def events_list() -> None:
    """List the outage events: id, cutoff and name. The default is marked with *."""
    catalog = _catalog()
    chosen, _ = _resolve_event(None)
    typer.echo(f"events file: {catalog.source}")
    for item in catalog.events:
        mark = "*" if item.id == chosen.id else " "
        markers = f" ({len(item.markers)} markers)" if item.markers else ""
        typer.echo(f"{mark} {item.id}  {item.cutoff_text}  {item.name}{markers}")
    if chosen.custom:
        typer.echo(f"* {chosen.id}  {chosen.cutoff_text}  {chosen.name} (custom, local only)")
    typer.echo(
        "Custom events: give --cutoff to make one, custom-YYYYMMDDTHHMMZ (local only, "
        "never in the shared registry)."
    )


@events_app.command("show")
def events_show(
    event_id: Annotated[str, typer.Argument(help="An event id, or a custom-... id.")],
) -> None:
    """Show one event: its cutoff, windows, notes, registry folder and cache database."""
    catalog = _catalog()
    try:
        item = catalog.resolve(event_id.strip())
    except outage_events.EventError as exc:
        raise _fail(str(exc)) from None
    chosen, _ = _resolve_event(None)
    typer.echo(f"id: {item.id}{' (default)' if item.id == chosen.id else ''}")
    typer.echo(f"name: {item.name}")
    typer.echo(f"cutoff: {item.cutoff_text}")
    typer.echo(f"baseline_days: {item.baseline_days}")
    typer.echo(f"min_silence_days: {item.min_silence_days:g}")
    typer.echo(f"cliff_window_hours: {item.cliff_window_hours:g}")
    typer.echo(
        f"verified_min_silence_days: {item.verified_min_silence_days:g} (silence a record "
        "needs before it may become verified_inauthentic)"
    )
    if item.notes:
        typer.echo(f"notes: {item.notes}")
    for line in outage_events.describe_markers(item):
        typer.echo(line)
    if item.custom:
        typer.echo("registry: none (a custom event is local only)")
    else:
        typer.echo(f"registry folder: registry/events/{item.id}/")
    typer.echo(f"cache database: {event_db_path(item.id)}")


config_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Show or change the platforms you chose to scan and the default outage event "
    "(stored in the config file).",
)
app.add_typer(config_app, name="config")


def _config_warnings(loaded: user_config.UserConfig) -> None:
    for line in loaded.warnings:
        typer.secho(f"warning: {line}", err=True)


def _config_error(exc: user_config.ConfigError) -> typer.Exit:
    return _fail(str(exc))


@config_app.command("show")
def config_show() -> None:
    """Print the config file path, the enabled platforms and the default platform."""
    try:
        loaded = user_config.load_config()
    except user_config.ConfigError as exc:
        raise _config_error(exc) from None
    _config_warnings(loaded)
    state = "" if loaded.exists else " (not created yet)"
    typer.echo(f"config file: {loaded.path}{state}")
    typer.echo(f"platforms: {', '.join(loaded.enabled)}")
    typer.echo(f"default_platform: {loaded.default}")
    if loaded.default_event is not None:
        typer.echo(f"default_event: {loaded.default_event}")
    if loaded.keep_awake is not None:
        typer.echo(f"keep_awake (desktop app): {str(loaded.keep_awake).lower()}")


@config_app.command("set-default-event")
def config_set_default_event(
    event_id: Annotated[str, typer.Argument(help="The outage event commands use without --event.")],
) -> None:
    """Set the outage event used when a command gets no --event (see `events list`)."""
    try:
        chosen = _catalog().resolve(event_id.strip())
    except outage_events.EventError as exc:
        raise _fail(str(exc)) from None
    try:
        saved = user_config.set_default_event(user_config.default_config_path(), chosen.id)
    except user_config.ConfigError as exc:
        raise _config_error(exc) from None
    _config_warnings(saved)
    typer.echo(f"Saved to {saved.path}")
    typer.echo(f"default_event: {saved.default_event} (cutoff {chosen.cutoff_text})")


@config_app.command("set-platforms")
def config_set_platforms(
    platforms: Annotated[
        str, typer.Argument(help="Comma-separated platforms, e.g. reddit-archive,bluesky.")
    ],
    if_unset: Annotated[
        bool,
        typer.Option(
            "--if-unset", help="Only write when the config file does not list platforms yet."
        ),
    ] = False,
) -> None:
    """Record which platforms you chose to scan."""
    try:
        names = user_config.parse_platforms(platforms)
        path = user_config.default_config_path()
        if if_unset:
            try:
                existing = user_config.load_config(path)
            except user_config.ConfigError as exc:
                typer.secho(
                    f"warning: left {path} alone because it cannot be used ({exc}). "
                    f"Edit or delete {path}, then run `bot-farm-silence-tracker config "
                    "set-platforms` again.",
                    err=True,
                )
                return
            _config_warnings(existing)
            if existing.platforms is not None:
                typer.echo(f"Kept the platforms already in {path}.")
                return
        saved = user_config.set_platforms(path, names)
    except user_config.ConfigError as exc:
        raise _config_error(exc) from None
    _config_warnings(saved)
    typer.echo(f"Saved to {saved.path}")
    typer.echo(f"platforms: {', '.join(saved.enabled)}")
    typer.echo(f"default_platform: {saved.default}")


@config_app.command("set-default")
def config_set_default(
    platform: Annotated[str, typer.Argument(help="The platform `scan` uses without --platform.")],
) -> None:
    """Set the platform used when `scan` gets no --platform (it is also enabled)."""
    try:
        name = user_config.validate_platform(platform.strip().lower())
        saved = user_config.set_default(user_config.default_config_path(), name)
    except user_config.ConfigError as exc:
        raise _config_error(exc) from None
    _config_warnings(saved)
    typer.echo(f"Saved to {saved.path}")
    typer.echo(f"platforms: {', '.join(saved.enabled)}")
    typer.echo(f"default_platform: {saved.default}")


@app.command("gui")
def gui(
    selftest: Annotated[
        bool,
        typer.Option(
            "--selftest",
            help="Build the window, show every screen with sample data, close it and exit "
            "(used to check a build).",
        ),
    ] = False,
) -> None:
    """Open the desktop app for running scans (needs the "gui" extra and a display)."""
    from bot_farm_silence_tracker.gui import launch

    code = launch(selftest=selftest)
    if code:
        raise typer.Exit(code)


@app.command("selftest-tls", hidden=True)
def selftest_tls(
    url: Annotated[
        str, typer.Option("--url", help="https:// address to request (no user name or password).")
    ] = SELFTEST_TLS_URL,
) -> None:
    """Check that HTTPS works: CA bundle present, TLS handshake and a HEAD request.

    Used by the release smoke tests so a standalone build that lost its certificate
    bundle fails at build time instead of on the user's first scan. Sends one HEAD
    request with no credentials, follows no redirect, uses no proxy and trusts only
    the bundled CA certificates (like a scan without ``--trust-env``).
    """
    import ssl

    import certifi
    import httpx

    try:
        target = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        raise _fail(f"--url must be an https:// address, got {redact_userinfo(url)!r}") from None
    if target.scheme != "https" or not target.host or target.userinfo:
        raise _fail(
            "--url must be an https:// address without a user name or password, "
            f"got {redact_userinfo(url)!r}"
        )
    bundle = Path(certifi.where())
    typer.echo(f"openssl: {ssl.OPENSSL_VERSION}")
    typer.echo(f"ca bundle: {bundle} ({'present' if bundle.is_file() else 'MISSING'})")
    if not bundle.is_file():
        raise _fail("the CA certificate bundle is missing", code=1)
    try:
        with httpx.Client(
            timeout=20.0, follow_redirects=False, trust_env=False, verify=certifi_ssl_context()
        ) as client:
            response = client.head(target)
    except httpx.HTTPError as exc:
        raise _fail(f"HTTPS request to {url} failed: {exc!r}", code=1) from None
    typer.echo(f"HEAD {url} -> {response.status_code} ({response.http_version})")
    if response.status_code >= 400:
        raise _fail(f"HTTPS request to {url} returned {response.status_code}", code=1)
    typer.echo("tls ok")


def run() -> None:
    """Console entry point: make output encoding-safe, then run the app.

    Doing this before :data:`app` parses arguments covers ``--help`` and
    ``--version``, which exit before any callback runs.
    """
    _tolerate_unencodable_output()
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
