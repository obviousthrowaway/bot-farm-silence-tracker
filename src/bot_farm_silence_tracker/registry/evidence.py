"""Evidence drafts for registry review.

An evidence file is Markdown, committed under
``registry/events/<event-id>/evidence/<platform>/`` next to the record it supports.
It is drafted from the local cache (no request) and then checked and completed by
people. It never holds a decoded username or ID: links
carry the placeholders ``{username}`` and ``{id}``, which ``report`` fills in only in
its local drafts. The cache keeps no post IDs or content (by design), so the links
are profile pages and public archive or API queries scoped to the activity times
listed, not links to single posts.

A fenced ``json`` block under "Machine-readable summary" holds the same facts for
tools (:func:`parse_evidence`): the steady test's numbers, the signals (each marked
whether it is independent of the outage's timing), the stop-time cluster with its
permutation p-value, the run's placebo comparison, and the live profile check made
when the draft was written (:mod:`bot_farm_silence_tracker.registry.live`).

The draft ends with the reviewer's findings (:mod:`bot_farm_silence_tracker.registry.
review`): the live profile status the verifier saw, and fields for evidence that is
not about the silence. Ticking "The behavior is inauthentic" needs at least one of
them, and the registry gate checks that.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.controls import PERMUTATIONS, headline, independent
from bot_farm_silence_tracker.events import EventCatalog
from bot_farm_silence_tracker.output import NOTICE
from bot_farm_silence_tracker.registry.build import fired_signals, run_records
from bot_farm_silence_tracker.registry.live import (
    ARCHIVE_ONLY,
    NOT_CHECKED,
    REVIEWER_CHOICES,
    Check,
    no_live_check,
)
from bot_farm_silence_tracker.registry.records import b64e
from bot_farm_silence_tracker.registry.review import (
    EVIDENCE_FIELDS,
    INAUTHENTIC_ITEM,
    LIVE_FIELD,
)
from bot_farm_silence_tracker.scoring import AccountResult
from bot_farm_silence_tracker.seeds import PROJECT_NAME
from bot_farm_silence_tracker.storage import Storage
from bot_farm_silence_tracker.timeutil import ensure_utc, isoformat

ARCHIVE_URL = "https://arctic-shift.photon-reddit.com"
BSKY_PROFILE_URL = "https://bsky.app/profile/{id}"
BSKY_FEED_URL = (
    "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor={id}&limit=30"
    "&filter=posts_with_replies"
)
#: How many example activity times the draft links to.
EXAMPLES = 3
_SUMMARY_FENCE = "```json"


def profile_links(platform: str) -> list[str]:
    """Profile link templates of ``platform`` (placeholders ``{username}``, ``{id}``)."""
    if platform in ("reddit", "reddit-archive"):
        return ["https://www.reddit.com/user/{username}/"]
    if platform == "bluesky":
        return [BSKY_PROFILE_URL, BSKY_FEED_URL]
    if platform == "x":
        return ["https://x.com/i/user/{id}"]
    return []


def activity_links(platform: str, when: datetime, kind: str) -> list[str]:
    """Public query links that show the account's activity at ``when``."""
    if platform in ("reddit", "reddit-archive"):
        epoch = math.floor(ensure_utc(when).timestamp())
        path = "posts" if kind == "post" else "comments"
        return [
            f"{ARCHIVE_URL}/api/{path}/search?author={{username}}&after={epoch - 1}"
            f"&before={epoch + 1}&limit=10"
        ]
    return []


def _hour_profile(stamps: list[datetime]) -> list[int]:
    counts = Counter(ensure_utc(ts).astimezone(UTC).hour for ts in stamps)
    return [counts.get(hour, 0) for hour in range(24)]


def _bars(profile: list[int]) -> str:
    top = max(profile) or 1
    return "\n".join(
        f"{hour:02d}  {'#' * round(20 * count / top):<20}  {count}"
        for hour, count in enumerate(profile)
    )


def find_result(results: dict[str, AccountResult], account: str) -> AccountResult | None:
    """The result whose user ID equals ``account``, or whose handle does (any case)."""
    if account in results:
        return results[account]
    folded = account.casefold().lstrip("@")
    matches = [r for r in results.values() if r.handle.casefold().lstrip("@") == folded]
    return matches[0] if len(matches) == 1 else None


def draft_evidence(
    storage: Storage,
    run_id: int,
    account: str,
    events: EventCatalog,
    live: Check = no_live_check,
) -> tuple[str, dict[str, Any]]:
    """Draft the evidence Markdown for ``account`` (handle or ID) of ``run_id``.

    ``live`` checks the account's public profile now (see
    :class:`~bot_farm_silence_tracker.registry.live.LiveChecker`); the default makes no
    request. Returns ``(markdown, summary)``. Raises ``LookupError`` when the run has no
    registry record for the account (see :mod:`bot_farm_silence_tracker.registry.build`
    for who is left out).
    """
    # The run's records without requests; only this account's profile is checked below.
    built = run_records(storage, run_id, events)
    assert built.event is not None
    result = find_result(built.results, account)
    if result is None:
        raise LookupError(
            f"run {run_id} has no registry record for that account (unknown, ambiguous, "
            "or left out of the registry: opted out, unavailable, not fully scanned, or "
            "no activity)"
        )
    params = built.outcome.params
    record = next(r for r in built.records if r["id_b64"] == b64e(result.user_id))
    cliffs = [r for r in built.outcome.results if r.cliff]
    context = built.outcome.context
    signals = fired_signals(result, context)
    end = ensure_utc(min(params.now, params.effective_now))
    if result.data_end is not None:
        end = min(end, ensure_utc(result.data_end))
    activities = storage.get_activities(
        result.platform, result.user_id, params.baseline_start, end + timedelta(seconds=1)
    )
    baseline = [a for a in activities if params.baseline_start <= a.ts < params.baseline_end]
    profile = _hour_profile([a.ts for a in baseline])
    communities = Counter(a.community for a in baseline if a.community)
    cluster: dict[str, Any] | None = None
    if result.cluster_id is not None:
        members = sorted(
            b64e(r.user_id)
            for r in cliffs
            if r.cluster_id == result.cluster_id and r.user_id != result.user_id
        )
        test = context.clusters.get(result.cluster_id)
        cluster = {
            "size": result.cluster_size,
            "sync_minutes": params.sync_minutes,
            "cliffs_in_run": len(cliffs),
            "other_members_id_b64": members,
            # Only clusters of at least 3 accounts are tested (see controls).
            "p_permutation": None if test is None else float(f"{test.p_permutation:.3g}"),
            "p_adjusted": None if test is None else float(f"{test.p_adjusted:.3g}"),
            "permutations": context.permutations,
            "nearest": None if test is None else test.nearest,
            "nearest_hours": None if test is None else round(test.nearest_hours, 2),
        }
    examples = sorted(activities, key=lambda a: a.ts, reverse=True)[:EXAMPLES]
    placebo = built.outcome.placebo
    found = live(result.platform, result.user_id, result.handle)
    summary: dict[str, Any] = {
        "event_id": built.event.id,
        "event_name": built.event.name,
        "platform": result.platform,
        "id_b64": record["id_b64"],
        "run_id": run_id,
        "tool_version": __version__,
        "facts": {
            key: record[key]
            for key in (
                "last_scanned_at",
                "last_activity_at",
                "scan_window_end",
                "baseline_posts",
                "baseline_active_days",
                "cliff",
            )
        },
        "cutoff": isoformat(params.cutoff),
        "baseline_start": isoformat(params.baseline_start),
        "provisional": result.provisional,
        "truncated": result.truncated,
        "posts_per_day": round(result.posts_per_day, 4),
        "silence_hours": None if result.silence_hours is None else round(result.silence_hours, 2),
        "steady": {
            "baseline_days": params.baseline_days,
            "active_days": result.active_days,
            "baseline_posts": result.baseline_posts,
            "recent_days": params.recent_days,
            "recent_posts": result.recent_posts,
            "weekly_cv": None if result.weekly_cv is None else round(result.weekly_cv, 3),
            "max_weekly_cv": params.max_weekly_cv,
        },
        "signals": [
            {
                "name": s.name,
                "value": float(f"{s.value:.4g}"),
                "threshold": s.threshold,
                "independent": s.independent,
            }
            for s in signals
        ],
        "independent_signals": len(independent(signals)),
        "cluster": cluster,
        "placebo": None
        if placebo is None
        else {"headline": headline(placebo), "clearly_above": placebo.clearly_above}
        | ({"rates": {c.name: c.to_dict() for c in placebo.rates()}} if placebo.available else {}),
        "live_profile": found.to_dict(),
        "hour_profile_utc": profile,
        "communities": dict(sorted(communities.items(), key=lambda kv: (-kv[1], kv[0]))[:10]),
        "examples": [{"at": isoformat(a.ts), "kind": a.kind} for a in examples],
        "links": profile_links(result.platform),
    }
    return render_evidence(summary, signals_meaning={s.name: s.meaning for s in signals}), summary


def render_evidence(summary: dict[str, Any], *, signals_meaning: dict[str, str]) -> str:
    facts = summary["facts"]
    lines = [
        f"# Evidence: {summary['platform']} {summary['id_b64']}",
        "",
        f"> Draft written by {PROJECT_NAME} {summary['tool_version']} from run "
        f"{summary['run_id']}. {NOTICE} Reviewers check every item below against the "
        "live platform before a pull request sets `verified_inauthentic`.",
        "",
        "Usernames and IDs stay base64-encoded in the registry. In the links below, "
        "`{username}` and `{id}` stand for the decoded `username_b64` and `id_b64`; "
        "`report` fills them in for its local drafts.",
        "",
        "## Scan facts",
        "",
        "| Fact | Value |",
        "| --- | --- |",
        f"| Outage event | {summary['event_name']} (`{summary['event_id']}`) |",
        f"| Platform | {summary['platform']} |",
        f"| id_b64 | `{summary['id_b64']}` |",
        f"| Cutoff | {summary['cutoff']} |",
        f"| Baseline window starts | {summary['baseline_start']} |",
        f"| Last scanned | {facts['last_scanned_at']} |",
        f"| Scanned data ends | {facts['scan_window_end']} |",
        f"| Last own activity seen | {facts['last_activity_at']} |",
        f"| Baseline posts | {facts['baseline_posts']} |",
        f"| Baseline active days | {facts['baseline_active_days']} |",
        f"| Posts per day (baseline) | {summary['posts_per_day']} |",
        f"| Silence (hours, to the data end) | {summary['silence_hours']} |",
        f"| Cliff | {'yes' if facts['cliff'] else 'no'} |",
        f"| Provisional | {'yes' if summary['provisional'] else 'no'} |",
        f"| Truncated history | {'yes' if summary['truncated'] else 'no'} |",
        "",
        "## Steady posting before the outage",
        "",
        *steady_lines(summary),
        "",
        "## Signals that fired",
        "",
        "A stop near a marker or the cutoff first of all means the account was affected by "
        "the reported outage, as ordinary users of the affected services were. Only signals "
        "measured on the baseline, independent of the outage's timing, point to automation.",
        "",
    ]
    if summary["signals"]:
        lines += [
            "| Signal | Independent of the timing | Value | Threshold | Meaning |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {s['name']} | {'yes' if s.get('independent', True) else 'no'} | {s['value']} "
            f"| {s['threshold']} | {signals_meaning.get(s['name'], '')} |"
            for s in summary["signals"]
        ]
    else:
        lines.append("None.")
    lines += ["", "## Stop-time cluster", ""]
    cluster = summary["cluster"]
    if cluster:
        lines += [
            f"Stopped within {cluster['sync_minutes']:g} minutes of "
            f"{cluster['size'] - 1} other cliff accounts ({cluster['cliffs_in_run']} cliff "
            "accounts in the run).",
            "",
            cluster_test_text(cluster),
            "",
            "Other members (id_b64):",
            "",
            *[f"- `{member}`" for member in cluster["other_members_id_b64"]],
        ]
    else:
        lines.append("Not in a synchronized-stop cluster.")
    lines += ["", "## Placebo comparison of the run", "", placebo_text(summary), ""]
    lines += ["## Live profile", "", live_text(summary)]
    lines += [
        "",
        "## Hour profile (baseline, UTC)",
        "",
        "```text",
        _bars(summary["hour_profile_utc"]),
        "```",
        "",
        "## Communities (baseline)",
        "",
    ]
    if summary["communities"]:
        lines += [f"- {name}: {count}" for name, count in summary["communities"].items()]
    else:
        lines.append("Not recorded by this source.")
    lines += ["", "## Public links", ""]
    lines += [f"- Profile: <{link}>" for link in summary["links"]]
    for example in summary["examples"]:
        when = datetime.fromisoformat(example["at"])
        found = activity_links(summary["platform"], when, example["kind"])
        lines += [f"- {example['kind']} at {example['at']}: <{link}>" for link in found] or [
            f"- {example['kind']} at {example['at']} (find it on the profile)"
        ]
    lines += [
        "",
        "## Reviewer notes",
        "",
        "CI checks only that the last activity exists and that no own activity is hidden "
        "after it. It does not check the baseline counts or the cliff flag: check them "
        "here.",
        "",
        "- [ ] The last activity above exists on the live platform.",
        "- [ ] No newer own activity exists up to the scanned data end.",
        "- [ ] The baseline posts and active days match the account's public history "
        "in the baseline window.",
        "- [ ] The account really went silent at the cliff (it is not merely provisional).",
        "- [ ] Bluesky: no post has a `createdAt` backdated before the window (such a post "
        "is listed at its `createdAt` and can be missed by the scan and the check); "
        "read the profile, not only the feed query.",
        "- [ ] The account named by the ID is the one in this evidence (Bluesky: the DID; "
        "the handle can change).",
        f"- [ ] {INAUTHENTIC_ITEM}, not a person who stopped posting or a user of the "
        "services the outage cut off (needs the live profile check and at least one "
        "finding below that is not about the silence).",
        "",
        "## Reviewer findings",
        "",
        "Fill these in on the live platform. Silence, however long, is not evidence of "
        "inauthenticity; the registry gate refuses a verdict without at least one of the "
        "evidence fields below. Leave a field empty when you found nothing.",
        "",
        f"- {LIVE_FIELD}: {NOT_CHECKED}",
        f"  (one of: {' / '.join(REVIEWER_CHOICES)}; what the live profile shows today)",
        *[f"- {name}: " for name in EVIDENCE_FIELDS],
        "",
        "## Machine-readable summary",
        "",
        _SUMMARY_FENCE,
        json.dumps(summary, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def steady_lines(summary: dict[str, Any]) -> list[str]:
    """The steady test's numbers, as the draft and the report drafts state them."""
    steady = summary.get("steady")
    if not isinstance(steady, dict):
        return ["Not recorded by the version that wrote this draft."]
    cv = steady.get("weekly_cv")
    return [
        f"- Posted on {steady['active_days']} of the {steady['baseline_days']} baseline days "
        f"({steady['baseline_posts']} posts or comments).",
        f"- {steady['recent_posts']} of them in the last {steady['recent_days']:g} days "
        "before the baseline ended.",
        f"- Weekly counts' coefficient of variation: {'n/a' if cv is None else cv} (at most "
        f"{steady['max_weekly_cv']:g} counts as steady).",
    ]


def cluster_test_text(cluster: dict[str, Any]) -> str:
    """How the draft states a cluster's permutation test."""
    if cluster.get("p_adjusted") is None:
        return (
            "Clusters of fewer than 3 accounts are not tested. A shared stop at the outage "
            "is expected of anyone the outage affected."
        )
    return (
        f"Permutation p-value {cluster['p_adjusted']:g}, measured against the largest cluster "
        "of each permutation, which adjusts it for the run's many clusters "
        f"({cluster.get('permutations', PERMUTATIONS)} permutations that redraw each "
        "account's stop time from its own hour-of-day posting profile, keeping accounts "
        "found through the same thread together). It measures how synchronized the stops "
        "are, not why: the reported outage explains a shared stop as well as shared "
        "automation does, so it is not evidence of automation on its own."
    )


def placebo_text(summary: dict[str, Any]) -> str:
    placebo = summary.get("placebo")
    if not isinstance(placebo, dict) or not placebo.get("headline"):
        return "Not recorded by the version that wrote this draft."
    return str(placebo["headline"])


def live_text(summary: dict[str, Any]) -> str:
    live = summary.get("live_profile")
    if not isinstance(live, dict):
        return "Not recorded by the version that wrote this draft."
    status = live.get("status")
    when = f" on {live['checked_at']}" if live.get("checked_at") else ""
    text = f"Automatic check{when}: {status} (method: {live.get('method')})."
    if status == ARCHIVE_ONLY:
        text += (
            " Reddit has no logged-out API without credentials, so the archive cannot tell "
            "silence from removal: the verifier must open the profile on the live site and "
            f'record what it shows under "{LIVE_FIELD}" below.'
        )
    elif status not in ("active", "suspended", "deleted"):
        text += f' The verifier must check the live profile and record it under "{LIVE_FIELD}".'
    return text


def parse_evidence(text: str) -> dict[str, Any]:
    """The machine-readable summary of an evidence file. Raises ``ValueError``."""
    start = text.find(_SUMMARY_FENCE)
    if start < 0:
        raise ValueError("no machine-readable summary block")
    body = text[start + len(_SUMMARY_FENCE) :]
    end = body.find("```")
    if end < 0:
        raise ValueError("unterminated machine-readable summary block")
    try:
        data = json.loads(body[:end])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid machine-readable summary: {exc.msg}") from None
    if not isinstance(data, dict):
        raise ValueError("the machine-readable summary must be a JSON object")
    return data


def write_evidence(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
