"""The reviewer's findings in an evidence file, and what a verdict needs.

An evidence draft (:mod:`bot_farm_silence_tracker.registry.evidence`) ends with fields
a human verifier fills in on the live platform::

    - Live profile checked: not checked
    - Content evidence:
    - Network evidence:
    - Profile evidence:
    - Other evidence:

and a checklist whose last box is "The behavior is inauthentic". Silence alone is not
evidence of inauthenticity, so :func:`review_problems` accepts that box only when

* the live profile field is resolved (``active``, ``suspended`` or ``deleted``; the
  draft says "not checked", and for Reddit the automatic check can only say
  "unknown (archive only)"), and
* at least one of the evidence fields that are not about the silence (content,
  network, profile or other) is filled in.

The registry gate applies it to the evidence file of every record a pull request
moves into ``verified_inauthentic``.
"""

from __future__ import annotations

import re

from bot_farm_silence_tracker.registry.live import ACTIVE, DELETED, NOT_CHECKED, SUSPENDED

LIVE_FIELD = "Live profile checked"
#: Findings that are not about the silence; a verdict needs at least one.
EVIDENCE_FIELDS = ("Content evidence", "Network evidence", "Profile evidence", "Other evidence")
#: The checklist item a verdict ticks.
INAUTHENTIC_ITEM = "The behavior is inauthentic"
#: Live profile values that resolve the check.
RESOLVED = (ACTIVE, SUSPENDED, DELETED)
#: Field values that count as empty.
_EMPTY = frozenset({"", "-", "none", "n/a", "na", "todo", "tbd", "(none)"})

_FIELD = re.compile(r"^\s*[-*]\s+(?P<name>[A-Za-z ]+?)\s*:\s*(?P<value>.*?)\s*$")
_BOX = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<text>.*)$")


def reviewer_fields(text: str) -> dict[str, str]:
    """The reviewer's fields of an evidence file, by name (the last one wins)."""
    names = {LIVE_FIELD, *EVIDENCE_FIELDS}
    out: dict[str, str] = {}
    for line in text.splitlines():
        found = _FIELD.match(line)
        if found is not None and found["name"] in names:
            out[found["name"]] = found["value"]
    return out


def inauthentic_ticked(text: str) -> bool:
    """Whether the checklist's "The behavior is inauthentic" box is ticked."""
    for line in text.splitlines():
        found = _BOX.match(line)
        if found is not None and found["text"].startswith(INAUTHENTIC_ITEM):
            return found["mark"].lower() == "x"
    return False


def filled(value: str | None) -> bool:
    return value is not None and value.strip().strip("`").lower() not in _EMPTY


def review_problems(text: str) -> list[str]:
    """Why the evidence file does not support ``verified_inauthentic`` (empty: it does)."""
    problems: list[str] = []
    if not inauthentic_ticked(text):
        problems.append(f'the box "{INAUTHENTIC_ITEM}" is not ticked')
    fields = reviewer_fields(text)
    live = fields.get(LIVE_FIELD, NOT_CHECKED).strip().strip("`").lower()
    if live not in RESOLVED:
        problems.append(
            f'"{LIVE_FIELD}" is {live!r}: a verifier must check the live profile and record '
            f"{', '.join(RESOLVED)}"
        )
    if not any(filled(fields.get(name)) for name in EVIDENCE_FIELDS):
        problems.append(
            "no evidence beyond the silence: fill in at least one of "
            + ", ".join(f'"{name}"' for name in EVIDENCE_FIELDS)
        )
    return problems
