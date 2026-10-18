"""Lazy platform registry.

Adapters are imported only when their platform is requested, so a missing or
half-built adapter never breaks the other platforms.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping

from bot_farm_silence_tracker.secretmap import SecretDict
from bot_farm_silence_tracker.sources.base import Source

#: Platform name to ``"module:Class"`` import path.
SOURCE_REGISTRY: dict[str, str] = {
    "bluesky": "bot_farm_silence_tracker.sources.bluesky:BlueskySource",
    "fake": "bot_farm_silence_tracker.sources.fake:FakeSource",
    "reddit": "bot_farm_silence_tracker.sources.reddit:RedditSource",
    "reddit-archive": "bot_farm_silence_tracker.sources.reddit_archive:RedditArchiveSource",
    "x": "bot_farm_silence_tracker.sources.x:XSource",
}


class AdapterNotAvailable(Exception):
    """The adapter module for a platform could not be imported."""


class MissingCredentials(Exception):
    """One or more required environment variables are unset."""

    def __init__(self, platform: str, missing: list[str]) -> None:
        self.platform = platform
        self.missing = missing
        names = ", ".join(missing)
        super().__init__(
            f"Missing credentials for platform '{platform}': set {names} in the "
            f"environment. See the '{platform}' section of the README for setup steps."
        )


def load_source_class(platform: str, registry: Mapping[str, str] | None = None) -> type[Source]:
    """Import and return the adapter class for ``platform``."""
    table = registry if registry is not None else SOURCE_REGISTRY
    if platform not in table:
        choices = ", ".join(sorted(table))
        raise AdapterNotAvailable(f"Unknown platform '{platform}'. Choose one of: {choices}.")
    module_name, _, class_name = table[platform].partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name and (module_name == exc.name or module_name.startswith(exc.name + ".")):
            raise AdapterNotAvailable(
                f"The '{platform}' adapter ({table[platform]}) is not installed yet. "
                "Adapters are added separately; try `--platform fake` for a demo run."
            ) from exc
        raise AdapterNotAvailable(
            f"The '{platform}' adapter needs a missing dependency: {exc.name}."
        ) from exc
    cls = getattr(module, class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, Source)):
        raise AdapterNotAvailable(
            f"{table[platform]} does not define a Source subclass named {class_name}."
        )
    return cls


def collect_credentials(
    source_cls: type[Source], environ: Mapping[str, str] | None = None
) -> SecretDict:
    """Read the adapter's credentials from the environment.

    Every name in ``required_credentials`` must be set; :class:`MissingCredentials`
    lists the unset ones. Names in ``optional_credentials`` are included only when
    set to a non-blank value. Values are stripped.
    """
    env = environ if environ is not None else os.environ
    values = {name: env.get(name, "").strip() for name in source_cls.required_credentials}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise MissingCredentials(source_cls.name, missing)
    for name in source_cls.optional_credentials:
        value = env.get(name, "").strip()
        if value:
            values[name] = value
    return SecretDict(values)
