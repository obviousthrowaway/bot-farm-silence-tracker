"""``export-ids``: write real platform IDs, handles and scan facts to a local file.

The file holds real accounts, so it must stay private: it is written only to the
path the user gives, with owner-only permissions where the system supports them,
and never uploaded anywhere. Accounts that opted out of logged-out views are never
written, as in every other output.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bot_farm_silence_tracker.models import STATUS_OPTED_OUT
from bot_farm_silence_tracker.output import COLUMNS
from bot_farm_silence_tracker.pipeline import rescore_run
from bot_farm_silence_tracker.scoring import MARKER_COLUMNS, METHOD_COLUMNS
from bot_farm_silence_tracker.storage import Storage

PRIVATE_WARNING = (
    "WARNING: {path} contains real platform user IDs and handles. Keep it private: do "
    "not commit, share or upload it. The shared registry only ever holds base64-encoded "
    "identities."
)
#: Columns of the export: the run, then the scan output's columns (rank excluded).
EXPORT_COLUMNS = [
    "run_id",
    *[c for c in COLUMNS if c != "rank"],
    "cliff",
    "eligible",
    *METHOD_COLUMNS,
]
#: Columns appended at the end when an exported run has markers.
EXPORT_MARKER_COLUMNS = list(MARKER_COLUMNS)


def export_columns(rows: Iterable[dict[str, Any]]) -> list[str]:
    """:data:`EXPORT_COLUMNS`, then :data:`EXPORT_MARKER_COLUMNS` when any row has them
    (an exported run with markers), so an export without markers keeps its columns."""
    marked = any(MARKER_COLUMNS[0] in row for row in rows)
    return [*EXPORT_COLUMNS, *(EXPORT_MARKER_COLUMNS if marked else [])]


def export_rows(storage: Storage, run_ids: Iterable[int]) -> list[dict[str, Any]]:
    """One row per account per run, from the cache (no request)."""
    rows: list[dict[str, Any]] = []
    for run_id in run_ids:
        outcome = rescore_run(storage, run_id)
        markers = bool(outcome.params.markers)
        for result in sorted(outcome.results, key=lambda r: r.user_id):
            if result.status == STATUS_OPTED_OUT:
                continue
            row = result.to_row(marker_columns=markers)
            rows.append(
                {
                    "run_id": run_id,
                    **{k: row.get(k) for k in COLUMNS if k != "rank"},
                    "cliff": result.cliff,
                    "eligible": result.eligible,
                    **{k: row.get(k) for k in METHOD_COLUMNS},
                    **({k: row.get(k) for k in MARKER_COLUMNS} if markers else {}),
                }
            )
    return rows


def _private_open(path: Path) -> Any:
    """Open ``path`` for writing, created owner-only (0600) on POSIX systems."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with contextlib.suppress(OSError):  # An existing file keeps its mode otherwise.
        os.chmod(path, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline="")


def write_export(path: Path, rows: list[dict[str, Any]], fmt: str) -> None:
    with _private_open(path) as fh:
        if fmt == "json":
            payload = {"private": PRIVATE_WARNING.format(path=path.name), "rows": rows}
            fh.write(json.dumps(payload, indent=2))
            fh.write("\n")
        else:
            writer = csv.DictWriter(fh, fieldnames=export_columns(rows), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
