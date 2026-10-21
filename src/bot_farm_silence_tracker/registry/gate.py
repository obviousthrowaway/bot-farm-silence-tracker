"""The registry gate: the required check on every pull request, run from trusted code.

The ``Registry gate`` workflow runs on ``pull_request_target``: the workflow file and
this code come from the base branch, never from the pull request. It checks out the
base commit only, fetches the pull request's head as git objects, and reads the head
registry with ``git ls-tree`` and ``git cat-file``: the pull request's files are
never checked out or run.

The review policy (see "Repository rules" in docs/USAGE.md and SECURITY.md):

* Account records (``registry/events/<event-id>/<platform>/<shard>.jsonl``) need no
  reviewer: CODEOWNERS leaves them without an owner, and this gate, a required
  check, validates and spot-checks them.
* Within a data pull request, every change that needs the repository owner (see
  :mod:`bot_farm_silence_tracker.registry.verify`) fails here unless the owner
  approves it with the override below.
* Every other file (code, workflows, docs, ``events.toml``, the schema, evidence,
  CODEOWNERS) is owned by the repository owner in CODEOWNERS, so the main branch's
  ruleset (``.github/rulesets/main.json``) requires the owner's review for it.

The gate, on the change from the merge base to the head:

Registry data is everything under ``registry/`` and the events file ``events.toml``
(see :mod:`bot_farm_silence_tracker.events`), which decides each event's cutoff.

1. No registry change: PASS at once, with "No registry data changed" in the summary
   (the ``CI`` workflow tests code changes, and CODEOWNERS makes them need the
   owner's review). The workflow takes the same decision before installing anything
   and then skips the gate's own steps, so the check reports on every pull request.
2. Registry data and other files changed together: FAIL, so data pull requests and
   code pull requests are reviewed separately. The one exception is
   ``registry/schema.json`` alone with code: the schema follows the code, and the
   test suite checks it byte for byte against ``schema_text()``.
3. A data-only change: the head's events file must validate (schema, id syntax,
   unique ids, cutoff order and range, size limits), and the change to it is
   compared with the base's (:func:`~bot_farm_silence_tracker.events.compare_events`):

   * adding an event needs no label; the summary lists it;
   * changing an existing event's name, cutoff or windows, renaming it or deleting
     it needs the maintainer override below (anyone could otherwise move a cutoff
     to re-mask accounts); the summary lists each such change as high-impact;
   * markers: changing a marker's time or ``approximate`` flag, or removing a marker,
     can re-label accounts (their nearest marker), so it needs the override too, and
     so does any marker change that moves the event's windows (a new earliest
     marker, say). Adding a marker that leaves the windows as they are, and changing
     a marker's name, time note, notes or sources, need no label; the summary lists
     them.

   Then the head registry must validate against the head's events (with this base
   code's rules, so every file is a regular ``100644`` file of an allowed name in
   the folder of an existing event, and every record's ``event_id`` is its
   folder's), and the policy and the spot-checks of
   :mod:`bot_farm_silence_tracker.registry.verify` run against the base and head
   records, with each event's cutoff.

Maintainer overrides need two things (:attr:`PullRequest.override_honoured`):

1. The ``registry-override`` label, which only people with triage access can set. It
   is the explicit request for an override, and the gate honours it only with proof
   that it was added while the head was the commit being checked:

   * on a ``labeled`` event for the label itself, the event payload is the proof: it
     names the head at the moment the label was added;
   * on any other event except ``synchronize``, the proof is a commit status on the
     head commit with the context ``registry-override/pr-<number>``, which the
     ``Registry override`` workflow (``registry-override.yml``) writes as
     ``github-actions[bot]`` for the head named in each ``labeled`` event. The gate
     reads the head's statuses in a trusted step and gets them as a file
     (:func:`override_status_approves`); without that file, or if it cannot be read,
     the label is ignored;
   * on a ``synchronize`` event (a new push) the label is always ignored, and the
     ``Registry override`` workflow removes it, so a maintainer adds it again after
     reviewing the new commits.

2. An approving review of the head commit itself (:func:`review_approves`): a review
   whose state is ``APPROVED`` and whose ``commit_id`` is the head SHA, and which is
   the reviewer's latest approving, change-requesting or dismissed review, by someone
   with admin access (the repository owner) who is not the pull request's author.
   Write or maintain access is not enough. The
   label is added at whatever the head is at that moment, which may be a commit
   pushed during a long review of an earlier one; the review names the commit the
   maintainer actually reviewed. The gate reads the reviews and the reviewers'
   permissions in a trusted step and gets them as files; without them, or if they
   cannot be read, the label is ignored.

The pull request's timeline cannot provide the proof of when the label was added: it
has no server-side time for an ordinary push, and its ``committed`` events carry only
git dates, which the author chooses.

Bad input (oversized files, over-long lines, deeply nested JSON, impossible dates)
fails the gate with a message; it never crashes it. Everything the pull request
controls is shown in the summary inside escaped code spans.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.events import (
    EVENTS_FILE,
    EventCatalog,
    EventChanges,
    compare_events,
    events_path_for,
)
from bot_farm_silence_tracker.registry.records import (
    SCHEMA_FILE,
    RegistryTree,
    git,
    md_code,
    validate_registry,
)
from bot_farm_silence_tracker.registry.verify import (
    DEFAULT_TOLERANCE,
    OVERRIDE_LABEL,
    Probe,
    VerifyReport,
    evidence_changes,
    evidence_reviews,
    gate_seed,
    load_tree,
    render_summary,
    run_verify,
)

#: The event action of a new push to the pull request.
SYNCHRONIZE = "synchronize"
#: The event action of a label being added.
LABELED = "labeled"
#: The account that writes commit statuses with the workflow's ``GITHUB_TOKEN``.
STATUS_WRITER = "github-actions[bot]"
#: The review state of an approval.
APPROVED = "APPROVED"
#: Review states that replace a reviewer's earlier verdict. ``COMMENTED`` and
#: ``PENDING`` reviews leave it as it was, as on GitHub.
VERDICT_STATES = frozenset({APPROVED, "CHANGES_REQUESTED", "DISMISSED"})
#: Repository permissions (as the collaborator permission API reports them) that may
#: approve an override: only ``admin``, which the repository owner has. Writers
#: (``write``, which is also how ``maintain`` is reported) may not; ``triage`` is
#: reported as ``read``.
APPROVER_PERMISSIONS = frozenset({"admin"})


def override_context(pr_number: int) -> str:
    """The commit status context that records an override approval of one head."""
    return f"{OVERRIDE_LABEL}/pr-{pr_number}"


def override_status_approves(text: str, pr_number: int) -> bool:
    """Whether a head commit's statuses approve it for the override label.

    ``text`` holds one JSON object per line, ``{"context", "state", "creator"}``, newest
    first, as the GitHub API lists a commit's statuses. The newest status with the
    pull request's :func:`override_context` decides: it must be ``success`` and written
    by :data:`STATUS_WRITER`. Anything unreadable approves nothing.
    """
    context = override_context(pr_number)
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            status = json.loads(line)
        except ValueError:
            return False
        if not isinstance(status, dict):
            return False
        if status.get("context") == context:
            return status.get("state") == "success" and status.get("creator") == STATUS_WRITER
    return False


def _json_lines(text: str) -> list[dict[str, Any]] | None:
    """One JSON object per non-blank line, or ``None`` if any line is not one."""
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            return None
        if not isinstance(item, dict):
            return None
        items.append(item)
    return items


def review_approves(reviews: str, permissions: str, head_sha: str, author: str) -> bool:
    """Whether a review approves the head commit for the override label.

    ``reviews`` holds one JSON object per line, ``{"id", "user", "state",
    "commit_id"}``, as the GitHub API lists a pull request's reviews (oldest first;
    the ``id`` orders them when present). ``permissions`` holds one JSON object per
    line, ``{"user", "permission"}``, from the collaborator permission API.

    A reviewer approves when their latest review in :data:`VERDICT_STATES` is
    ``APPROVED`` on ``head_sha`` itself, they are not ``author``, and their permission
    is in :data:`APPROVER_PERMISSIONS` (admin: the repository owner). A review of an
    earlier commit, a dismissed review and an approval followed by a request for
    changes approve nothing; so does anything unreadable, and a reviewer whose
    permission is missing.
    """
    review_list = _json_lines(reviews)
    permission_list = _json_lines(permissions)
    if review_list is None or permission_list is None or not head_sha or not author:
        return False
    allowed = {
        str(item.get("user")): item.get("permission")
        for item in permission_list
        if isinstance(item.get("user"), str)
    }
    if all(isinstance(review.get("id"), int) for review in review_list):
        review_list.sort(key=lambda review: review["id"])
    latest: dict[str, dict[str, Any]] = {}
    for review in review_list:
        user = review.get("user")
        if isinstance(user, str) and user and review.get("state") in VERDICT_STATES:
            latest[user] = review
    return any(
        review.get("state") == APPROVED
        and review.get("commit_id") == head_sha
        and user.casefold() != author.casefold()
        and allowed.get(user) in APPROVER_PERMISSIONS
        for user, review in latest.items()
    )


@dataclass(frozen=True)
class PullRequest:
    """What the gate needs from a ``pull_request_target`` event."""

    number: int
    base_sha: str
    head_sha: str
    labels: frozenset[str]
    action: str = ""
    #: The label a ``labeled`` or ``unlabeled`` event is about.
    event_label: str = ""
    #: Whether a trusted record approves the head for the override label (see
    #: :func:`override_status_approves`).
    head_approved: bool = False
    #: The login of the pull request's author, who may not approve the override.
    author: str = ""
    #: Whether an approving review of the head commit itself backs the override label
    #: (see :func:`review_approves`).
    head_reviewed: bool = False
    #: Whether the labels come straight from the person running the gate (a local run
    #: without an event payload), so they need no proof.
    labels_trusted: bool = False

    @classmethod
    def from_event(
        cls, path: Path, *, head_approved: bool = False, head_reviewed: bool = False
    ) -> PullRequest:
        """Read the event payload GitHub wrote (``GITHUB_EVENT_PATH``)."""
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        pr = payload["pull_request"]
        label = payload.get("label")
        user = pr.get("user")
        return cls(
            number=int(pr["number"]),
            base_sha=str(pr["base"]["sha"]),
            head_sha=str(pr["head"]["sha"]),
            labels=frozenset(str(label["name"]) for label in pr.get("labels") or []),
            action=str(payload.get("action") or ""),
            event_label=str(label.get("name") or "") if isinstance(label, dict) else "",
            head_approved=head_approved,
            author=str(user.get("login") or "") if isinstance(user, dict) else "",
            head_reviewed=head_reviewed,
        )

    @property
    def override_problem(self) -> str | None:
        """Why the override label present on the pull request is not honoured, if it is not."""
        if OVERRIDE_LABEL not in self.labels or self.labels_trusted:
            return None
        if self.action == SYNCHRONIZE:
            return (
                f"The `{OVERRIDE_LABEL}` label is ignored after a new push and is being "
                "removed; a maintainer adds it again after reviewing the new commits."
            )
        labeled_now = self.action == LABELED and self.event_label == OVERRIDE_LABEL
        if not (labeled_now or self.head_approved):
            return (
                f"The `{OVERRIDE_LABEL}` label is ignored: nothing shows that it was added "
                "while this commit was the head. A maintainer removes it and adds it again "
                "after reviewing the new commits."
            )
        if not self.head_reviewed:
            return (
                f"The `{OVERRIDE_LABEL}` label is ignored: no approving review of this head "
                f"commit ({md_code(self.head_sha)}) by the repository owner (admin access) "
                "who is not the author. The owner approves this commit and then reruns the "
                "gate."
            )
        return None

    @property
    def override_honoured(self) -> bool:
        """Whether the override label is present, added for this head and approved on it."""
        return OVERRIDE_LABEL in self.labels and self.override_problem is None

    @property
    def effective_labels(self) -> frozenset[str]:
        """The labels the gate honours (see the module notes on the override label)."""
        if self.override_honoured:
            return self.labels
        return self.labels - {OVERRIDE_LABEL}


@dataclass
class GateResult:
    paths: list[str]
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    report: VerifyReport | None = None
    #: What the change does to the events file, when it is a data-only change.
    events: EventChanges | None = None
    #: Changes to existing events that the honoured override label allows.
    events_overridden: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and (self.report is None or self.report.ok)


def changed_paths(repo: Path, base: str, head: str) -> tuple[str, list[str]]:
    """The merge base of ``base`` and ``head``, and the paths ``head`` changes from it."""
    merge_base = git(repo, "merge-base", base, head).strip()
    out = git(repo, "diff", "--name-only", "-z", "--no-renames", merge_base, head, "--")
    return merge_base, sorted(p for p in out.split("\0") if p)


def run_gate(
    repo: Path,
    pr: PullRequest,
    probe: Probe,
    *,
    registry_path: str = "registry",
    now: datetime | None = None,
    tolerance: timedelta = DEFAULT_TOLERANCE,
    lag: timedelta | None = None,
) -> GateResult:
    """Run the gate (see the module notes). Only reads git objects of ``repo``."""
    try:
        merge_base, paths = changed_paths(repo, pr.base_sha, pr.head_sha)
    except ValueError as exc:
        return GateResult(
            [], failures=[f"cannot compare the head with the base: {md_code(str(exc))}"]
        )
    result = GateResult(paths)
    problem = pr.override_problem
    if problem is not None:
        result.notes.append(problem)
    try:
        _gate_data(
            result,
            repo,
            pr,
            merge_base,
            probe,
            registry_path=registry_path,
            now=now,
            tolerance=tolerance,
            lag=lag,
        )
    except (ValueError, RecursionError, UnicodeDecodeError) as exc:
        result.failures.append(f"the registry could not be read ({type(exc).__name__})")
    return result


def _gate_data(
    result: GateResult,
    repo: Path,
    pr: PullRequest,
    merge_base: str,
    probe: Probe,
    *,
    registry_path: str,
    now: datetime | None,
    tolerance: timedelta,
    lag: timedelta | None,
) -> None:
    prefix = registry_path.strip("/") + "/"
    events_file = events_path_for(registry_path)
    paths = result.paths
    data = [p for p in paths if p.startswith(prefix) or p == events_file]
    other = [p for p in paths if not (p.startswith(prefix) or p == events_file)]
    if not data:
        result.notes.append(
            "No registry data changed. The CI workflow checks code changes, and the "
            "repository owner reviews them (CODEOWNERS)."
        )
        return
    if other:
        if data == [prefix + SCHEMA_FILE]:
            result.notes.append(
                f"{prefix}{SCHEMA_FILE} changes with code; the test suite checks it byte "
                "for byte against schema_text()."
            )
        else:
            result.failures.append(
                f"the pull request changes the registry or {md_code(events_file)} "
                f"({len(data)} files) and other files ({len(other)}, for example "
                f"{md_code(other[0])}); open separate pull requests for data and code"
            )
        return
    head_tree = RegistryTree(repo=repo, ref=pr.head_sha, registry_path=registry_path)
    base_tree = RegistryTree(repo=repo, ref=merge_base, registry_path=registry_path)
    check = check_events(base_tree, head_tree, pr.effective_labels, now=now)
    if check.head is None:
        result.validation = check.problems
        result.failures.append(
            f"the head's {md_code(events_file)} is invalid ({len(check.problems)} problems)"
        )
        return
    head_events, base_events = check.head, check.base
    result.events = check.changes
    result.events_overridden = check.overridden
    result.failures += check.failures
    result.validation = validate_registry(tree=head_tree, now=now, events=head_events)
    if result.validation:
        result.failures.append(f"the head registry is invalid ({len(result.validation)} problems)")
        return
    base_records, _ = load_tree(
        base_tree, now=now, events=base_events if base_events is not None else head_events
    )
    head_records, problems = load_tree(head_tree, now=now, events=head_events)
    result.report = run_verify(
        base_records,
        head_records,
        probe,
        seed=gate_seed(pr.base_sha, pr.head_sha, pr.number),
        labels=pr.effective_labels,
        now=now,
        tolerance=tolerance,
        lag=lag,
        problems=problems,
        evidence=evidence_changes(base_tree, head_tree),
        events=head_events,
        reviews=evidence_reviews(base_records, head_records, head_tree),
    )


@dataclass
class EventCheck:
    """The events of both sides and what the policy makes of the change."""

    head: EventCatalog | None
    base: EventCatalog | None
    changes: EventChanges | None = None
    problems: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    overridden: list[str] = field(default_factory=list)


def check_events(
    base_tree: RegistryTree,
    head_tree: RegistryTree,
    labels: Iterable[str],
    *,
    now: datetime | None = None,
) -> EventCheck:
    """Validate the head's events file and apply the event policy to the change.

    Adding an event passes. Changing an existing event's name, cutoff or windows,
    renaming it or deleting it (:attr:`EventChanges.guarded`) needs
    :data:`OVERRIDE_LABEL` in ``labels``, which the gate puts there only when the
    label is honoured (proof of when it was added and an approving review of the
    head). When the base's file exists but is invalid, any difference needs it too.
    """
    head, problems = head_tree.events(now=now)
    if head is None:
        return EventCheck(None, None, problems=problems)
    base, _ = base_tree.events(now=now)
    check = EventCheck(head, base, compare_events(base, head))
    assert check.changes is not None
    guarded = check.changes.guarded
    if base is None and base_tree.has_events_file():
        guarded = [f"the base's {EVENTS_FILE} is invalid, so no change to it can be judged"]
    if guarded:
        if OVERRIDE_LABEL in frozenset(labels):
            check.overridden = guarded
        else:
            check.failures.append(
                f"{len(guarded)} changes to existing outage events (changing a name, cutoff "
                "or window, renaming or deleting an event, moving or removing a marker) "
                "need the "
                f"`{OVERRIDE_LABEL}` label and an approving review of this head commit: "
                + "; ".join(md_code(item) for item in guarded)
            )
    return check


def render_events(changes: EventChanges | None, overridden: Iterable[str] = ()) -> list[str]:
    """The summary section about the events file. Everything in it is escaped."""
    overridden = list(overridden)
    if changes is None or not (changes.any or overridden):
        return []
    lines = ["### Outage events", ""]
    if changes.added:
        lines += ["Added events (no label needed):", ""]
        lines += [
            f"- {md_code(e.id)}: {md_code(e.name)}, cutoff {md_code(e.cutoff_text)}, "
            f"baseline {e.baseline_days} days, silence {e.min_silence_days:g} days, cliff "
            f"window {e.cliff_window_hours:g} hours"
            + (f", {len(e.markers)} markers" if e.markers else "")
            for e in changes.added
        ] + [""]
    if changes.markers_added:
        lines += ["Added markers (no label needed while they leave the windows as they are):", ""]
        lines += [
            f"- {md_code(event_id)}: {md_code(m.id)} {md_code(m.name)} at "
            f"{md_code(m.time_text)}" + (" (approximate)" if m.approximate else "")
            for event_id, m in changes.markers_added
        ] + [""]
    guarded = changes.guarded
    if guarded or overridden:
        allowed = bool(overridden)
        lines += [
            "**High-impact event changes** "
            + (
                f"(allowed by the `{OVERRIDE_LABEL}` label and an approving review):"
                if allowed
                else f"(need the `{OVERRIDE_LABEL}` label and an approving review):"
            ),
            "",
        ]
        lines += [f"- {md_code(item)}" for item in (overridden or guarded)] + [""]
    if changes.default_changed:
        lines += [
            f"**High-impact:** the default event changes from "
            f"{md_code(str(changes.default_before))} to {md_code(str(changes.default_after))} "
            "(no label needed; reviewers confirm it).",
            "",
        ]
    if changes.notes_changed:
        lines += [
            "Notes changed: " + ", ".join(md_code(i) for i in changes.notes_changed) + ".",
            "",
        ]
    if changes.markers_described:
        lines += [
            "Marker names, time notes, notes or sources changed: "
            + ", ".join(f"{md_code(e)} {md_code(m)}" for e, m in changes.markers_described)
            + ".",
            "",
        ]
    return lines


def render_gate(result: GateResult, labels: Iterable[str] = ()) -> str:
    """The Markdown job summary of the gate."""
    lines = [
        "## Registry gate",
        "",
        f"**{'PASS' if result.ok else 'FAIL'}.** Changed paths: {len(result.paths)}. "
        f"Labels: {', '.join(md_code(label) for label in sorted(labels)) or 'none'}.",
        "",
    ]
    if result.failures:
        lines += ["### Why it fails", ""] + [f"- {f}" for f in result.failures] + [""]
    if result.validation:
        lines += ["### Validation", ""] + [f"- {md_code(p)}" for p in result.validation] + [""]
    lines += render_events(result.events, result.events_overridden)
    lines += [f"{note}" for note in result.notes]
    if result.notes:
        lines.append("")
    if result.report is not None:
        lines.append(render_summary(result.report))
    return "\n".join(lines)
