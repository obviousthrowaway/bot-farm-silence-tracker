"""Where the default seeds file is read from (source checkout, wheel, frozen program)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bot_farm_silence_tracker import seeds

REPO_ROOT = Path(__file__).resolve().parents[1]


def _plant(root: Path, pyproject: str | None) -> tuple[Path, Path]:
    """Lay out ``root`` like a folder that holds an unpacked copy of the package.

    Returns the fake ``seeds.py`` path (two levels below ``root``) and the planted
    ``config/seeds.yaml``.
    """
    module = root / "_MEI12345" / "bot_farm_silence_tracker" / "seeds.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    planted = root / "config" / "seeds.yaml"
    planted.parent.mkdir()
    planted.write_text("reddit:\n  subreddits: [planted_by_someone_else]\n")
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject)
    return module, planted


def test_a_source_checkout_reads_its_own_config() -> None:
    assert seeds.default_seeds_path() == REPO_ROOT / "config" / "seeds.yaml"


def test_a_frozen_program_ignores_a_planted_seeds_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even a planted pyproject.toml with the right name must not make a frozen
    # program read files from the shared folder its unpack folder sits in.
    module, planted = _plant(tmp_path, '[project]\nname = "bot-farm-silence-tracker"\n')
    monkeypatch.setattr(seeds, "__file__", str(module))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    path = seeds.default_seeds_path()

    assert path != planted
    assert path.parts[-2:] == ("_data", "seeds.yaml")
    assert "planted_by_someone_else" not in load_items(path)


@pytest.mark.parametrize(
    "pyproject",
    [None, '[project]\nname = "something-else"\n', "not = [valid toml", "[tool.x]\n"],
    ids=["no-pyproject", "other-project", "broken-toml", "no-project-table"],
)
def test_a_config_folder_without_this_projects_pyproject_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pyproject: str | None
) -> None:
    module, planted = _plant(tmp_path, pyproject)
    monkeypatch.setattr(seeds, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert seeds.default_seeds_path() != planted


def test_a_checkout_with_this_projects_pyproject_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, planted = _plant(tmp_path, '[project]\nname = "bot-farm-silence-tracker"\n')
    monkeypatch.setattr(seeds, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert seeds.default_seeds_path() == planted


def load_items(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""
