"""Release notes come from CHANGELOG.md, and no workflow may auto-generate them."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

import bot_farm_silence_tracker

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_notes.py"
CHANGELOG = ROOT / "CHANGELOG.md"

_spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
assert _spec is not None and _spec.loader is not None
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)


def test_workflows_do_not_auto_generate_notes() -> None:
    """Auto-generated notes list contributor mentions, so no workflow may ask for them."""
    for workflow in (ROOT / ".github" / "workflows").glob("*.y*ml"):
        text = workflow.read_text(encoding="utf-8")
        assert "--generate-notes" not in text, workflow.name
        assert "generate_release_notes" not in text, workflow.name


def test_no_auto_notes_config() -> None:
    assert not (ROOT / ".github" / "release.yml").exists()
    assert not (ROOT / ".github" / "release.yaml").exists()


def test_current_version_has_changelog_section() -> None:
    version = bot_farm_silence_tracker.__version__
    assert release_notes.extract_section(CHANGELOG.read_text(encoding="utf-8"), version), (
        f"CHANGELOG.md has no section for version {version}"
    )


def test_every_released_version_has_changelog_section() -> None:
    """Every version heading in the changelog is non-empty, and the current one is present."""
    text = CHANGELOG.read_text(encoding="utf-8")
    versions = re.findall(r"^## \[(\d[^\]]*)\]", text, re.MULTILINE)
    assert bot_farm_silence_tracker.__version__ in versions
    for version in versions:
        assert release_notes.extract_section(text, version), version


def test_script_prints_section_and_checksum_line() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "0.2.0"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "desktop app" in result.stdout
    assert "0.1.0" not in result.stdout
    assert result.stdout.rstrip().endswith("Verify downloads against SHA256SUMS.txt.")


def test_script_fails_for_missing_section() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "9.9.9"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert result.stdout == ""


@pytest.mark.parametrize("version", ["v0.1.0", "0.1.0"])
def test_leading_v_is_accepted(version: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert release_notes.extract_section(text, version)
