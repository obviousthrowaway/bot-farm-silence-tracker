"""Whether the ``Registry chart`` workflow may redraw the charts for a commit on main.

The workflow (``.github/workflows/registry-chart.yml``) runs on a push to the default
branch that touches ``registry/`` or ``events.toml``, and on a manual run. The commit
is trusted code, but its registry data must also have passed the ``Registry gate``,
whose spot-check re-fetches records from the live sources. :func:`decide` applies the
rule to data the workflow reads from the GitHub API:

* the ref must be the default branch;
* a manual run (``workflow_dispatch``) must be on the current head of the default
  branch; the workflow then validates the registry again before it draws anything;
* a push needs at least one pull request that was merged into the default branch as
  this very commit (``merge_commit_sha``), as
  ``GET /repos/{owner}/{repo}/commits/{sha}/pulls`` lists them. A direct push has
  none, and is skipped;
* every such pull request's head commit needs a ``Registry gate`` run that came from
  the base branch's workflow file (event ``pull_request_target``, path
  :data:`GATE_WORKFLOW`) and whose latest run on that head succeeded. A pull request
  merged with the gate failing, missing or still running (an admin bypass, say) is
  skipped. The event and path matter: a pull request's own workflows run on
  ``pull_request`` and could name a job ``gate``, but cannot run on
  ``pull_request_target`` with this path.

A skip is not an error: the workflow writes the reason to its summary and ends
without drawing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The gate's workflow file, as the workflow runs API reports its ``path``.
GATE_WORKFLOW = ".github/workflows/registry-gate.yml"
#: The event the gate runs on (from the base branch's workflow file).
GATE_EVENT = "pull_request_target"
PUSH = "push"
DISPATCH = "workflow_dispatch"


@dataclass(frozen=True)
class ChartDecision:
    update: bool
    reason: str
    pull_requests: tuple[int, ...] = field(default=())


def merged_pulls(pulls: Iterable[Mapping[str, Any]], sha: str, default_branch: str) -> list[dict]:
    """The pull requests merged into ``default_branch`` as the commit ``sha``."""
    out = []
    for pr in pulls:
        base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
        if (
            pr.get("merged_at")
            and pr.get("merge_commit_sha") == sha
            and base.get("ref") == default_branch
        ):
            out.append(dict(pr))
    return sorted(out, key=lambda pr: int(pr.get("number") or 0))


def gate_conclusion(runs: Iterable[Mapping[str, Any]], head_sha: str) -> str:
    """The conclusion of the latest trusted ``Registry gate`` run on ``head_sha``.

    ``"missing"`` when there is none, ``"in_progress"`` when the latest has not
    finished. Trusted runs have event :data:`GATE_EVENT` and path :data:`GATE_WORKFLOW`.
    """
    trusted = [
        run
        for run in runs
        if run.get("event") == GATE_EVENT
        and run.get("path") == GATE_WORKFLOW
        and run.get("head_sha") == head_sha
        and isinstance(run.get("id"), int)
    ]
    if not trusted:
        return "missing"
    latest = max(trusted, key=lambda run: (str(run.get("created_at") or ""), run["id"]))
    if latest.get("status") != "completed":
        return "in_progress"
    return str(latest.get("conclusion") or "unknown")


def decide(
    *,
    event_name: str,
    ref: str,
    sha: str,
    default_branch: str,
    main_head: str = "",
    pulls: Sequence[Mapping[str, Any]] = (),
    runs: Sequence[Mapping[str, Any]] = (),
) -> ChartDecision:
    """Apply the rule in the module notes. ``runs`` are workflow runs of any heads."""
    if ref != f"refs/heads/{default_branch}":
        return ChartDecision(False, f"`{ref}` is not the default branch `{default_branch}`.")
    if event_name == DISPATCH:
        if not main_head or sha != main_head:
            return ChartDecision(
                False,
                f"A manual run must be on the current head of `{default_branch}` "
                f"(`{main_head[:12] or 'unknown'}`), not `{sha[:12]}`.",
            )
        return ChartDecision(True, f"Manual rebuild from the current head of `{default_branch}`.")
    if event_name != PUSH:
        return ChartDecision(False, f"The event `{event_name}` does not redraw the charts.")
    merged = merged_pulls(pulls, sha, default_branch)
    if not merged:
        return ChartDecision(
            False,
            f"`{sha[:12]}` was not merged from a pull request (a direct push), so the "
            "Registry gate never spot-checked its data. The charts are left as they are.",
        )
    numbers = tuple(int(pr["number"]) for pr in merged)
    problems = []
    for pr in merged:
        head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
        head_sha = str(head.get("sha") or "")
        conclusion = gate_conclusion(runs, head_sha) if head_sha else "missing"
        if conclusion != "success":
            problems.append(
                f"#{pr['number']} (head `{head_sha[:12]}`): the latest Registry gate run "
                f"is {conclusion}"
            )
    if problems:
        return ChartDecision(
            False,
            "The Registry gate did not pass for every merged pull request, so the charts "
            "are left as they are: " + "; ".join(problems) + ".",
            numbers,
        )
    listed = ", ".join(f"#{n}" for n in numbers)
    reason = f"Merged from {listed}; the Registry gate passed on the head."
    return ChartDecision(True, reason, numbers)


def read_json_lines(text: str) -> list[dict[str, Any]]:
    """JSON objects, one per line, or a JSON array; non-objects are dropped."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        loaded = json.loads(text)
        items = loaded if isinstance(loaded, list) else []
    else:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [item for item in items if isinstance(item, dict)]
