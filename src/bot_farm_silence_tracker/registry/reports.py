"""Local report drafts for people to submit to platform moderators.

Nothing here sends anything. Each draft is a Markdown file a person reads, checks
and submits by hand through the platform's own report form: platforms treat bulk
automated reporting as abuse, and every report needs a human judgment behind it.

The drafts are the only place the registry's base64 usernames and IDs are decoded,
so they contain real account names and must stay private (they are written outside
the registry and outside any git repository, to a directory the user names).

Only ``verified_inauthentic`` records are drafted, unless the caller asks for
others explicitly; every such draft is marked "UNVERIFIED — do not submit".

Drafts are made for one outage event at a time (``report --registry --event``). Each
names the event and its cutoff, and says when the same account also has a record in
another event. Drafts go to ``<out>/<event-id>/<platform>/<id_b64>.md``.

Identity: the account is named by what its ID says, because the spot-check probed
the ID. For Reddit the ID is the author name (the record rules bind the two). For
Bluesky the ID is the DID; the handle can change hands, so the draft links the
DID and shows the handle only as seen at the scan, with a warning to confirm it.

Nothing a pull request can write reaches a draft as Markdown or as a link: names and
IDs must pass the record rules' syntax checks and are shown inside escaped code
spans; links are built only from fixed platform templates and the validated ID or
name. An evidence file's own links and communities are ignored, and only its
numeric facts, well-formed signal names and the fixed words of its live profile
status are used. Text an evidence file carries (the placebo headline) is shown only
inside an escaped code span.

Wording: a draft states what was observed, not a conclusion. It says when the
account stopped posting and with how many similar accounts, the steady test's
numbers, the run's placebo comparison and the live profile status, and it calls the
outage "reported" and cites the event's notes: a stop at the outage is expected of
anyone it affected, so the draft never calls the account part of a network.

Refusals: no draft is written for a record whose account posted again (no longer a
cliff, or activity newer than the silence window), whose silence is shorter than its
event's ``verified_min_silence_days``, or whose evidence was drafted for other facts
than the record now holds (:func:`refusal_reasons`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.controls import CLIFF_RATE, RateComparison, rate_text
from bot_farm_silence_tracker.events import EventCatalog, OutageEvent
from bot_farm_silence_tracker.registry.evidence import parse_evidence, profile_links
from bot_farm_silence_tracker.registry.live import ARCHIVE_ONLY, REVIEWER_CHOICES
from bot_farm_silence_tracker.registry.records import (
    MAX_EVIDENCE_FILE_BYTES,
    STATUS_VERIFIED,
    b64d,
    event_dir,
    load_records,
    md_code,
    resumed,
    silence,
    valid_user_id,
    valid_username,
)
from bot_farm_silence_tracker.scoring import (
    MAIN_OUTAGE,
    NearestMarker,
    marker_label,
    nearest_marker,
)
from bot_farm_silence_tracker.timeutil import isoformat, parse_datetime

REDDIT_PLATFORMS = ("reddit", "reddit-archive")
DRAFT_PLATFORMS = (*REDDIT_PLATFORMS, "bluesky")
REDDIT_REPORT_URL = "https://www.reddit.com/report"
MODMAIL_URL = "https://www.reddit.com/message/compose?to=/r/{community}"

DRAFT_HEADER = (
    "> LOCAL DRAFT. It contains a real account name: keep it private and do not "
    "commit it. Nothing was submitted. Read it, check the evidence against the live "
    "platform, and submit it yourself through the platform's own form; never automate "
    "reports (platforms treat bulk automated reporting as abuse)."
)


UNVERIFIED_HEADER = (
    "> **UNVERIFIED — do not submit.** This record's status is `{status}`, not "
    "`verified_inauthentic`: reviewers have not agreed the account is inauthentic. "
    "It was drafted on request for review only."
)


def identity(record: dict[str, Any]) -> tuple[str, str]:
    """The record's decoded ``(username, ID)``. Raises ``ValueError`` unless both have
    their platform's syntax, so neither can carry Markdown or a link."""
    platform = record["platform"]
    user_id, username = b64d(record["id_b64"]), b64d(record["username_b64"])
    if not valid_user_id(platform, user_id) or not valid_username(platform, username):
        raise ValueError(f"{platform} record {record['id_b64']}: invalid ID or username")
    return username, user_id


def reddit_name(record: dict[str, Any]) -> str:
    """The Reddit author name the record's ID gives (the ID is the name)."""
    username, user_id = identity(record)
    return username if username.lower() == user_id.lower() else user_id


@dataclass(frozen=True)
class Draft:
    path: Path
    platform: str
    id_b64: str
    event_id: str = ""
    #: Other events in which the same account has a record.
    also_in: tuple[str, ...] = ()


@dataclass(frozen=True)
class Refusal:
    """A record no draft was written for, and why."""

    platform: str
    id_b64: str
    event_id: str
    reasons: tuple[str, ...]


def _fill(template: str, username: str, user_id: str) -> str:
    return template.replace("{username}", username).replace("{id}", user_id)


_SIGNAL_NAME = r"[a-z][a-z0-9_]{0,63}"


def _number(value: Any) -> float | None:
    """``value`` when it is a plain finite number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if abs(number) < 1e12 else None


def _cluster(evidence: dict[str, Any] | None) -> tuple[float, int] | None:
    """``(sync minutes, other members)`` from an evidence summary, or ``None``."""
    cluster = (evidence or {}).get("cluster")
    if not isinstance(cluster, dict):
        return None
    sync, size = _number(cluster.get("sync_minutes")), _number(cluster.get("size"))
    if sync is None or size is None or size < 2:
        return None
    return sync, int(size) - 1


def _steady(record: dict[str, Any], evidence: dict[str, Any] | None) -> str:
    """The steady test's numbers: from the evidence when it has them, else the record's."""
    steady = (evidence or {}).get("steady")
    text = (
        f"posted on {record['baseline_active_days']} distinct days of the baseline window "
        f"({record['baseline_posts']} posts or comments)"
    )
    if not isinstance(steady, dict):
        return text
    days, recent = _number(steady.get("baseline_days")), _number(steady.get("recent_posts"))
    window, cv = _number(steady.get("recent_days")), _number(steady.get("weekly_cv"))
    if days is not None:
        text = (
            f"posted on {record['baseline_active_days']} of the {days:g} baseline days "
            f"({record['baseline_posts']} posts or comments)"
        )
    if recent is not None and window is not None:
        text += f", {recent:g} of them in the last {window:g} days before the baseline ended"
    if cv is not None:
        text += f", with a weekly coefficient of variation of {cv:.2g}"
    return text


def _live(evidence: dict[str, Any] | None) -> str:
    """The live profile status of an evidence summary, as fixed words only."""
    live = (evidence or {}).get("live_profile")
    status = live.get("status") if isinstance(live, dict) else None
    if status not in (*REVIEWER_CHOICES, ARCHIVE_ONLY, "unknown (needs credentials)"):
        return "not recorded in the evidence"
    when = live.get("checked_at") if isinstance(live, dict) else None
    stamp = (
        f" on {when}" if isinstance(when, str) and re.fullmatch(r"[0-9T:+-]{19,25}", when) else ""
    )
    return f"{status} (automatic check{stamp})"


def _placebo(evidence: dict[str, Any] | None) -> str | None:
    """The placebo comparison of an evidence summary, rebuilt from its numbers (the
    evidence file's own text is never used), or ``None``."""
    placebo = (evidence or {}).get("placebo")
    rates = placebo.get("rates") if isinstance(placebo, dict) else None
    cliff = rates.get(CLIFF_RATE) if isinstance(rates, dict) else None
    if not isinstance(cliff, dict):
        return None
    keys = ("real", "placebo_mean", "placebo_sd", "excess", "excess_low", "excess_high")
    values = [_number(cliff.get(key)) for key in keys]
    if any(v is None for v in values):
        return None
    real, mean, sd, excess, low, high = (float(v) for v in values)  # type: ignore[arg-type]
    text = "cliff rate " + rate_text(
        RateComparison(CLIFF_RATE, real, (), mean, sd, excess, low, high)
    )
    if low > 0:
        return text + ": clearly above the placebo"
    return text + ": NOT CLEARLY ABOVE THE PLACEBO"


def _signals(evidence: dict[str, Any] | None) -> list[tuple[str, float, str]]:
    """Well-formed ``(name, value, threshold)`` signals of an evidence summary."""
    out: list[tuple[str, float, str]] = []
    for signal in (evidence or {}).get("signals") or []:
        if not isinstance(signal, dict):
            continue
        name, value = signal.get("name"), _number(signal.get("value"))
        if not isinstance(name, str) or not re.fullmatch(_SIGNAL_NAME, name) or value is None:
            continue
        if name != "synchronized_stop":
            out.append((name, value, str(signal.get("threshold", ""))))
    return out


def _event_lines(event: OutageEvent | None, also_in: tuple[str, ...]) -> list[str]:
    """Which outage event the draft is about, and the other events of the account.

    Event names and ids pass the events file's syntax rules, and are still shown in
    escaped code spans."""
    if event is None:
        return []
    lines = [
        f"- Outage event (as reported): {md_code(event.name)} ({md_code(event.id)}), cutoff "
        f"{event.cutoff_text}."
    ]
    if also_in:
        lines.append(
            "- This account also appears in event "
            + ", ".join(md_code(other) for other in also_in)
            + " of the registry, as a separate record; check each event on its own."
        )
    return lines


def _nearest(record: dict[str, Any], event: OutageEvent | None) -> NearestMarker | None:
    """The marker (or main outage) the record's silence began nearest to, for an event
    with markers; else ``None``."""
    if event is None or not event.markers:
        return None
    try:
        last = parse_datetime(str(record["last_activity_at"]))
    except (KeyError, ValueError):
        return None
    return nearest_marker(last, event.anchors())


def _marker_line(record: dict[str, Any], event: OutageEvent | None) -> list[str]:
    """The evidence line naming the marker the silence began nearest to."""
    found = _nearest(record, event)
    if found is None:
        return []
    when = isoformat(found.marker.time)
    if found.id == MAIN_OUTAGE:
        where = f"the main outage at the cutoff ({when})"
    else:
        approx = ", approximate" if found.approximate else ""
        where = f"the marker {md_code(marker_label(found.marker))} ({when}, as reported{approx})"
    return [
        f"- The silence began nearest to {where}: the last activity is "
        f"{found.offset_hours:+.1f} hours from it. Times of the outage are as reported, "
        "not independently verified."
    ]


def _summary_lines(
    record: dict[str, Any],
    evidence: dict[str, Any] | None,
    event: OutageEvent | None = None,
    also_in: tuple[str, ...] = (),
) -> list[str]:
    lines = [
        *_event_lines(event, also_in),
        *_notes_line(event),
        *_marker_line(record, event),
        f"- Before the outage it {_steady(record, evidence)}.",
        f"- Last own activity seen: {record['last_activity_at']}; none found after it up "
        f"to {record['scan_window_end']} (scanned {record['last_scanned_at']}).",
    ]
    cluster = _cluster(evidence)
    if cluster is not None:
        sync, others = cluster
        lines.append(
            f"- Stopped within {sync:g} minutes of {others} other accounts that also "
            "stopped; a shared stop at the reported outage is expected of anyone it "
            "affected, so it is not evidence of automation on its own."
        )
    for name, value, threshold in _signals(evidence):
        lines.append(f"- Signal `{name}`: {value:.4g} (threshold {md_code(threshold)}).")
    placebo = _placebo(evidence)
    lines.append(
        "- Placebo comparison of the scan (the same accounts at the same weekday and hour "
        f"in earlier weeks): {placebo or 'not recorded in the evidence'}."
    )
    lines.append(f"- Live profile status: {_live(evidence)}. Check it again before you submit.")
    return lines


def _notes_line(event: OutageEvent | None) -> list[str]:
    """The event's notes, cited: the outage is as reported, not a verified fact."""
    if event is None or not event.notes:
        return []
    return [
        f"- The event's notes (as reported, not independently verified): {md_code(event.notes)}"
    ]


def _short_text(
    record: dict[str, Any], evidence: dict[str, Any] | None, event: OutageEvent | None = None
) -> str:
    cluster = _cluster(evidence)
    together = (
        f", together with {cluster[1]} similar accounts within {cluster[0]:g} minutes"
        if cluster is not None
        else ""
    )
    text = (
        f"This account stopped posting at {record['last_activity_at']}{together}; see the evidence"
    )
    found = _nearest(record, event)
    if found is not None and found.id != MAIN_OUTAGE:
        approx = ", approximate" if found.approximate else ""
        assert event is not None
        text += (
            f". The stop is near the reported time of {found.marker.name} "
            f"({isoformat(found.marker.time)}{approx}), during a reported outage (event "
            f"{event.id}, cutoff {event.cutoff_text}; times as reported in the event notes)"
        )
    elif event is not None:
        text += (
            f". The stop is just before a reported outage (event {event.id}, cutoff "
            f"{event.cutoff_text}; as reported in the event notes)"
        )
    text += (
        f". Before that it {_steady(record, evidence)}. A stop at an outage alone does not "
        "show automation; the evidence lists what else was found"
    )
    placebo = _placebo(evidence)
    if placebo is not None and placebo.endswith("NOT CLEARLY ABOVE THE PLACEBO"):
        text += (
            ". In the scan, the cliff rate was not clearly higher than in ordinary earlier "
            "weeks (placebo comparison)"
        )
    return text + ". Please review it for spam or platform manipulation."


def _links(record: dict[str, Any], name: str, uid: str) -> list[str]:
    """Public links from the platform's fixed templates (never from evidence files)."""
    return [_fill(t, name, uid) for t in profile_links(record["platform"])]


def render_reddit(
    record: dict[str, Any],
    evidence: dict[str, Any] | None,
    event: OutageEvent | None = None,
    also_in: tuple[str, ...] = (),
) -> str:
    """The draft of a Reddit record. Raises ``ValueError`` for an invalid name."""
    name, uid = reddit_name(record), identity(record)[1]
    text = _short_text(record, evidence, event)
    lines = [
        f"# Report draft: Reddit u/{name}",
        "",
        DRAFT_HEADER,
        "",
        "## 1. Reddit report form",
        "",
        f"Open <{REDDIT_REPORT_URL}>, choose **Spam** (or the option for inauthentic or "
        "manipulated activity when the form offers one), give the username, and paste:",
        "",
        f"- Username: `u/{name}`",
        "",
        "```text",
        text,
        "```",
        "",
        "## 2. Subreddit modmail",
        "",
        "Send modmail only to communities where you have seen the account active "
        "yourself on the live site (this draft does not take community names from the "
        "evidence file). Open "
        f"`{MODMAIL_URL.format(community='COMMUNITY')}` with the community's name in "
        "place of `COMMUNITY`, use the subject `Possible inauthentic account active in "
        "your community`, and paste:",
        "",
        "```text",
        f"Hello. While researching accounts that went silent during a reported outage we "
        f"found u/{name}, which was active in your community. {text} The evidence summary "
        "is below. We have not reported it on your behalf; the decision is yours.",
        "```",
        "",
    ]
    lines += ["## Evidence summary", "", *_summary_lines(record, evidence, event, also_in), ""]
    lines += ["## Public links", "", *[f"- <{u}>" for u in _links(record, name, uid)], ""]
    return "\n".join(lines)


def render_bluesky(
    record: dict[str, Any],
    evidence: dict[str, Any] | None,
    event: OutageEvent | None = None,
    also_in: tuple[str, ...] = (),
) -> str:
    """The draft of a Bluesky record. Raises ``ValueError`` for an invalid DID or handle."""
    name, uid = identity(record)
    lines = [
        f"# Report draft: Bluesky {md_code(uid)}",
        "",
        DRAFT_HEADER,
        "",
        f"The account is identified by its DID, {md_code(uid)}. The handle seen at the "
        f"scan was {md_code('@' + name)}; handles can change and pass to another account, "
        "so confirm that "
        "the profile at the DID link below still shows the account in the evidence, and "
        "report it from that link, not by handle.",
        "",
        "## In-app report",
        "",
        f"Open <https://bsky.app/profile/{uid}> in the Bluesky app, open the profile menu, "
        "choose **Report account**, pick the reason **Spam** (or **Misleading** if the "
        "account impersonates or deceives), and paste:",
        "",
        "```text",
        _short_text(record, evidence, event),
        "```",
        "",
        "## Evidence summary",
        "",
        *_summary_lines(record, evidence, event, also_in),
        "",
        "## Public links",
        "",
        *[f"- <{u}>" for u in _links(record, name, uid)],
        "",
    ]
    return "\n".join(lines)


def refusal_reasons(
    record: dict[str, Any],
    evidence: dict[str, Any] | None,
    event: OutageEvent,
    events: EventCatalog,
) -> list[str]:
    """Why no draft may be written for ``record`` (empty: it may). See the module notes."""
    reasons: list[str] = []
    why = resumed(record, events)
    if why is not None:
        reasons.append(f"the account posted again: {why}")
    least = event.verified_min_silence
    if silence(record) < least:
        reasons.append(
            f"its silence ({silence(record).total_seconds() / 86400:.1f} days to the scanned "
            f"data end) is shorter than the event's verified_min_silence_days "
            f"({least.total_seconds() / 86400:g})"
        )
    facts = (evidence or {}).get("facts")
    if isinstance(facts, dict) and facts.get("last_activity_at") not in (
        None,
        record["last_activity_at"],
    ):
        reasons.append(
            "the evidence was drafted for an older last activity than the record now holds "
            "(the account was scanned again); draft the evidence again"
        )
    return reasons


def build_drafts(
    registry: Path,
    status: str,
    platform: str,
    out: Path,
    *,
    event_id: str,
    events: EventCatalog,
    include_unverified: bool = False,
    refused: list[Refusal] | None = None,
) -> list[Draft]:
    """Write one draft per ``platform`` record of ``event_id`` with ``status`` to
    ``out/<event_id>/<platform>/``.

    Raises ``ValueError`` for an event that is not one of ``events`` (custom events
    have no registry records), for a platform without a draft template, and for a
    status other than ``verified_inauthentic`` unless ``include_unverified`` is set;
    then every draft starts with :data:`UNVERIFIED_HEADER`. A record with
    :func:`refusal_reasons` gets no draft; it is appended to ``refused`` when given.
    """
    event = events.get(event_id)
    if event is None:
        raise ValueError(
            f"{event_id!r} is not an event of the registry's events file; custom events "
            "are local-only and have no registry records"
        )
    if platform not in DRAFT_PLATFORMS:
        raise ValueError(
            f"no report draft template for {platform!r}; choose one of {', '.join(DRAFT_PLATFORMS)}"
        )
    if status != STATUS_VERIFIED and not include_unverified:
        raise ValueError(
            f"report drafts are only for {STATUS_VERIFIED} records; drafting {status!r} "
            "records needs --include-unverified"
        )
    render = render_reddit if platform in REDDIT_PLATFORMS else render_bluesky
    drafts: list[Draft] = []
    records = load_records(registry, events=events)
    accounts: dict[tuple[str, str], set[str]] = {}
    for ev, plat, id_b64 in records:
        accounts.setdefault((plat, id_b64), set()).add(ev)
    for (ev, plat, id_b64), record in sorted(records.items()):
        if ev != event_id or plat != platform or record["status"] != status:
            continue
        also_in = tuple(sorted(accounts[(plat, id_b64)] - {event_id}))
        evidence: dict[str, Any] | None = None
        if record["evidence"]:
            path = registry / event_dir(event_id) / record["evidence"]
            if path.is_file() and path.stat().st_size <= MAX_EVIDENCE_FILE_BYTES:
                try:
                    evidence = parse_evidence(path.read_text(encoding="utf-8"))
                except (ValueError, RecursionError):
                    evidence = None
        reasons = refusal_reasons(record, evidence, event, events)
        if reasons:
            if refused is not None:
                refused.append(Refusal(plat, id_b64, ev, tuple(reasons)))
            continue
        text = render(record, evidence, event, also_in)
        if status != STATUS_VERIFIED:
            title, rest = text.split("\n", 1)
            text = f"{title} (UNVERIFIED — do not submit)\n\n" + (
                UNVERIFIED_HEADER.format(status=status) + "\n" + rest
            )
        target = out / event_id / platform / f"{id_b64}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        drafts.append(Draft(target, platform, id_b64, event_id, also_in))
    return drafts
