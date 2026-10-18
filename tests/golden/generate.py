"""Generate the fake-source census golden files.

The golden files pin the exact bytes of ``scan`` and ``report`` output (CSV and
JSON) for the deterministic fake source, so a change meant for one adapter cannot
silently change every platform's output. There are two sets:

* ``tests/golden/legacy``: the output of the default event's first definition
  (cutoff 2026-10-10T00:00Z, no markers), first generated from a checkout of ``main``
  at 456deb0 (before the Bluesky adapter). It was regenerated on purpose in the pull
  request that gave the method its controls (placebo cutoffs on samples of their
  own, the permutation null of stop-time clusters and the stricter steady test),
  which add notices, metadata, a renamed p-value column and other columns to every
  output. So these files are no longer the old versions' bytes: what they still
  reproduce from the old versions is the flagged accounts and every earlier column of
  their rows. The current code reproduces the files themselves byte for byte, both as
  a custom event of that cutoff (``--cutoff``, :data:`LEGACY_ARGS`) and as the fixture
  event of the same definition::

      python tests/golden/generate.py tests/golden/legacy

* ``tests/golden/census``: the output of the repository's current default event
  (the strikes and the main outage of October 2026, with markers), scanned at
  :data:`DEFAULT_NOW`, generated with::

      python tests/golden/generate.py tests/golden/census --default

This module only drives the CLI, so it runs against any version of the package
that is first on the import path. Run it again only for an intended output change,
and review the diff of the golden files.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from typer.testing import CliRunner

NOW_ARG = "2026-10-14T00:00:00Z"
#: The scans of the legacy set: a custom event with the first definition's cutoff.
LEGACY_ARGS: tuple[str, ...] = ("--cutoff", "2026-10-10T00:00Z")
#: When the current default event's set is scanned: more than its min_silence_days
#: (3) after its cutoff, so its results are not provisional.
DEFAULT_NOW = "2026-10-15T00:00:00Z"

#: name -> extra scan arguments. Every case is scanned with K = 1 and K = 2.
CASES: dict[str, tuple[str, ...]] = {
    "census": (),
    "unavailable": ("--include-unavailable",),
    "sampled": ("--baseline-sample-rate", "0.3"),
}
CONCURRENCY = (1, 2)


def golden_names() -> list[str]:
    """Every golden file name :func:`generate` writes."""
    return [
        f"{case}_k{k}_{cmd}.{fmt}"
        for case in CASES
        for k in CONCURRENCY
        for cmd in ("scan", "report")
        for fmt in ("csv", "json")
    ]


def _invoke(runner: CliRunner, args: list[str]) -> None:
    from bot_farm_silence_tracker.cli import app

    result = runner.invoke(app, args)
    if result.exit_code != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({result.exit_code}): {result.output}")


def normalize(text: str, work: Path) -> str:
    """Replace the run-specific text: the temporary working directory, and the tool
    version that the run metadata records (so a release does not change the files)."""
    from bot_farm_silence_tracker import __version__

    text = text.replace(str(work), "<work>")
    return re.sub(
        r'((?:scan_)?tool_version(?:": "|=))' + re.escape(__version__) + r'(?=["\s,])',
        r"\1<version>",
        text,
    )


def generate(out_dir: Path, event_args: tuple[str, ...] = LEGACY_ARGS, now: str = NOW_ARG) -> None:
    """Write every golden file to ``out_dir``: by default the legacy set; with
    ``event_args=()`` and ``now=DEFAULT_NOW`` the default event's set."""
    out_dir.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    for case, extra in CASES.items():
        for k in CONCURRENCY:
            for fmt in ("csv", "json"):
                with tempfile.TemporaryDirectory() as tmp:
                    work = Path(tmp).resolve()
                    db = work / "c.db"
                    include = ["--include-unavailable"] if "--include-unavailable" in extra else []
                    scan_out = work / f"scan.{fmt}"
                    _invoke(
                        runner,
                        [
                            "scan", "--platform", "fake", "--n", "300", "--seed", "1",
                            "--now", now, "--db", str(db), "--concurrency", str(k),
                            "--out", str(scan_out), *event_args, *extra,
                        ],
                    )  # fmt: skip
                    report_out = work / f"report.{fmt}"
                    _invoke(
                        runner,
                        [
                            "report", "--run-id", "1", "--db", str(db),
                            "--out", str(report_out), *include,
                        ],
                    )  # fmt: skip
                    for cmd, path in (("scan", scan_out), ("report", report_out)):
                        text = normalize(path.read_text(encoding="utf-8"), work)
                        target = out_dir / f"{case}_k{k}_{cmd}.{fmt}"
                        target.write_text(text, encoding="utf-8", newline="")


if __name__ == "__main__":
    if "--default" in sys.argv[2:]:
        generate(Path(sys.argv[1]), (), DEFAULT_NOW)
    else:
        generate(Path(sys.argv[1]))
