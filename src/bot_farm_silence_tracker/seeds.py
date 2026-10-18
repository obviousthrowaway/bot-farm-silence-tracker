"""Seed communities: where the sampling pool is drawn from.

A :class:`SeedSet` is a flat list of strings whose meaning depends on the platform:
subreddit names for Reddit, search queries for X, and search queries or feed and
list URIs for Bluesky. The defaults live in
``config/seeds.yaml`` and can be replaced with ``--seeds path.yaml`` or with the
repeatable ``--subreddit`` / ``--query`` flags.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

#: Maps a platform to the YAML key that holds its seed list.
SEED_KEYS = {
    "reddit": "subreddits",
    "reddit-archive": "subreddits",
    "x": "queries",
    "bluesky": "queries",
    "fake": "communities",
}

#: Further YAML keys whose entries are appended to a platform's seed list when present
#: (Bluesky feeds and lists sit next to its search queries).
EXTRA_SEED_KEYS: dict[str, tuple[str, ...]] = {"bluesky": ("feeds", "lists")}

#: Platforms whose seeds are subreddit names.
REDDIT_PLATFORMS = frozenset({"reddit", "reddit-archive"})

#: Platforms that read their seed list from another platform's YAML section.
SEED_SECTIONS = {"reddit-archive": "reddit"}


@dataclass(frozen=True)
class SeedSet:
    """The seed communities for one platform.

    ``items`` are subreddit names (no ``r/`` prefix) for ``reddit`` and search
    queries for ``x``. Adapters treat each item as one place to look for active
    accounts.
    """

    platform: str
    items: tuple[str, ...]
    #: True when the items come from the bundled ``seeds.yaml`` (no ``--seeds`` file and
    #: no ``--query`` / ``--subreddit`` override). An adapter may treat the bundled
    #: defaults differently from seeds the user chose, for example to avoid mixing
    #: sampling frames without being asked.
    bundled: bool = False


#: The ``[project] name`` that marks a source checkout of this package.
PROJECT_NAME = "bot-farm-silence-tracker"


def _checkout_seeds_path() -> Path | None:
    """Return ``<repo>/config/seeds.yaml`` when running from a source checkout.

    A folder counts as the checkout only when it also holds a ``pyproject.toml`` whose
    project name is this package. Without that check, a copy of the package unpacked
    somewhere shared would read a ``config/seeds.yaml`` that anyone could plant next
    to it. A frozen (PyInstaller) program never looks: it unpacks itself into a
    subfolder of the temp folder, so the folder two levels up is the shared temp
    folder itself.
    """
    if getattr(sys, "frozen", False):
        return None
    root = Path(__file__).resolve().parents[2]
    seeds = root / "config" / "seeds.yaml"
    pyproject = root / "pyproject.toml"
    if not (seeds.is_file() and pyproject.is_file()):
        return None
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(project, dict) or project.get("name") != PROJECT_NAME:
        return None
    return seeds


def default_seeds_path() -> Path:
    """Return the path of the bundled ``seeds.yaml``.

    In a source checkout this is ``<repo>/config/seeds.yaml``. In an installed wheel
    and in the standalone program the file is shipped as package data.
    """
    checkout = _checkout_seeds_path()
    if checkout is not None:
        return checkout
    packaged = resources.files("bot_farm_silence_tracker") / "_data" / "seeds.yaml"
    return Path(str(packaged))


def _normalize(platform: str, items: Sequence[str]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for raw in items:
        text = str(raw).strip()
        if platform in REDDIT_PLATFORMS:
            text = text.removeprefix("/").removeprefix("r/").removeprefix("R/").strip("/")
        if text:
            # Subreddit names are case-insensitive, so de-duplicate them that way.
            key = text.lower() if platform in REDDIT_PLATFORMS else text
            seen.setdefault(key, text)
    return tuple(seen.values())


def load_seeds(
    platform: str,
    path: Path | str | None = None,
    override: Sequence[str] | None = None,
) -> SeedSet:
    """Build the :class:`SeedSet` for ``platform``.

    If ``override`` is non-empty it replaces the file's list entirely (so
    ``--subreddit a --subreddit b`` scans exactly ``a`` and ``b``). Otherwise the
    list is read from ``path`` or the bundled default file. For Bluesky, the
    ``feeds`` and ``lists`` keys, when present, are appended to ``queries``.
    """
    if platform not in SEED_KEYS:
        raise ValueError(f"no seed list defined for platform {platform!r}")
    if override:
        items = _normalize(platform, override)
    else:
        seed_file = Path(path) if path else default_seeds_path()
        try:
            data = yaml.safe_load(seed_file.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise ValueError(f"cannot read seeds file {seed_file}: {exc}") from exc
        section_name = SEED_SECTIONS.get(platform, platform)
        section = data.get(section_name) or {}
        raw = section.get(SEED_KEYS[platform]) if isinstance(section, dict) else None
        if not isinstance(raw, list):
            raise ValueError(f"{seed_file} has no list at '{section_name}.{SEED_KEYS[platform]}'")
        raw = list(raw)
        for key in EXTRA_SEED_KEYS.get(platform, ()):
            extra = section.get(key)
            if extra is None:
                continue
            if not isinstance(extra, list):
                raise ValueError(f"{seed_file}: '{section_name}.{key}' must be a list")
            raw += extra
        items = _normalize(platform, raw)
    if not items:
        raise ValueError(f"seed list for {platform!r} is empty")
    return SeedSet(platform=platform, items=items, bundled=not override and not path)
