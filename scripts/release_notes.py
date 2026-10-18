"""Print the release notes for one version, taken from the matching CHANGELOG.md section.

Usage: release_notes.py VERSION [CHANGELOG_PATH]

VERSION may carry a leading "v". The script exits with an error when the section is
missing or empty, so a release can never be published without hand-written notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHECKSUM_LINE = "Verify downloads against SHA256SUMS.txt."


def extract_section(changelog: str, version: str) -> str | None:
    """Return the body of the "## [version]" section, or None when it is missing."""
    version = version.removeprefix("v")
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s.*)?$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        return None
    rest = changelog[match.end() :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    body = rest[: next_heading.start()] if next_heading else rest
    return body.strip() or None


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    default_path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    path = Path(argv[2]) if len(argv) == 3 else default_path
    body = extract_section(path.read_text(encoding="utf-8"), argv[1])
    if body is None:
        message = f"No CHANGELOG.md section for version {argv[1]!r}; add one before releasing."
        print(message, file=sys.stderr)
        return 1
    print(f"{body}\n\n{CHECKSUM_LINE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
