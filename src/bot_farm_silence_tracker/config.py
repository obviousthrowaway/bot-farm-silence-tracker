"""The user config file: which platforms the user chose, and the default one.

The file is TOML and lives in the platform's config directory: ``~/.config/bot-farm-silence-tracker/
config.toml`` on macOS and Linux, ``%APPDATA%\\bot-farm-silence-tracker\\config.toml`` on Windows
(falling back to the POSIX-style path when ``APPDATA`` is unset). The environment variable
``BOT_FARM_SILENCE_TRACKER_CONFIG`` names a different file. When ``XDG_CONFIG_HOME`` is set to an
absolute path, ``$XDG_CONFIG_HOME/bot-farm-silence-tracker/config.toml`` replaces the
``~/.config`` location.

Keys::

    platforms = ["reddit-archive"]   # the platforms the user chose to scan
    default_platform = "reddit-archive"
    default_event = "..."            # optional: the outage event used without --event
    keep_awake = true                # optional: the desktop app keeps the computer awake

The ``platforms`` list only records what the user picked. Scanning another platform is
still allowed. An empty list counts as not set. Platform names this version does not know
(a file written by a newer version) are ignored with a warning, and an unknown
``default_platform`` falls back to reddit-archive; only a file that is not TOML, or has
values of the wrong type, is an error. The installers write this file by running
``config set-platforms`` on the installed program, so this module is the one place that
knows the format.

``default_event`` names the outage event (see :mod:`bot_farm_silence_tracker.events`)
that commands use without ``--event``; ``config set-default-event`` writes it. Its
syntax is checked here; whether the event exists is checked when it is used, because
the events file can change after the config was written.

``keep_awake`` is the desktop app's "Keep this computer awake while scanning" checkbox
(on when the key is absent). The command line does not read it: it keeps the computer
awake unless ``scan --allow-sleep`` is given.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from bot_farm_silence_tracker.sources.base import ENV_PREFIX
from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY

#: The platform used when the user has not chosen one.
DEFAULT_PLATFORM = "reddit-archive"

#: Environment variable that names the config file.
CONFIG_ENV = ENV_PREFIX + "CONFIG"

_APP_DIR = "bot-farm-silence-tracker"
_FILE_NAME = "config.toml"
_IS_WINDOWS = os.name == "nt"


class ConfigError(Exception):
    """The config file or a requested change is not valid."""


@dataclass(frozen=True)
class UserConfig:
    """What the config file says. ``platforms`` is ``None`` when the file does not list any."""

    path: Path
    exists: bool
    platforms: tuple[str, ...] | None
    default_platform: str | None
    #: Problems that were tolerated while reading the file; the CLI shows them on stderr.
    warnings: tuple[str, ...] = ()
    #: The outage event chosen with ``config set-default-event``, or ``None``.
    default_event: str | None = None
    #: The desktop app's keep-awake checkbox, or ``None`` when the file does not say.
    keep_awake: bool | None = None

    @property
    def keep_awake_enabled(self) -> bool:
        """Whether the app keeps the computer awake during a scan (the default)."""
        return True if self.keep_awake is None else self.keep_awake

    @property
    def default(self) -> str:
        """The platform ``scan`` uses without ``--platform``."""
        return self.default_platform or DEFAULT_PLATFORM

    @property
    def enabled(self) -> tuple[str, ...]:
        """The platforms the user chose; the default platform alone when none were recorded."""
        return self.platforms if self.platforms is not None else (DEFAULT_PLATFORM,)


def default_config_path(
    *, windows: bool = _IS_WINDOWS, environ: Mapping[str, str] | None = None
) -> Path:
    """The config file path for this platform, or the one
    ``BOT_FARM_SILENCE_TRACKER_CONFIG`` names."""
    env = os.environ if environ is None else environ
    override = env.get(CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    appdata = env.get("APPDATA", "") if windows else ""
    if appdata:
        return Path(appdata) / _APP_DIR / _FILE_NAME
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    if xdg and Path(xdg).is_absolute():  # The XDG specification says to ignore a relative one.
        return Path(xdg) / _APP_DIR / _FILE_NAME
    return Path("~/.config").expanduser() / _APP_DIR / _FILE_NAME


def parse_platforms(text: str) -> tuple[str, ...]:
    """Split a comma list of platform names, check each is registered, and drop repeats."""
    names: list[str] = []
    for part in text.split(","):
        name = part.strip().lower()
        if name:
            names.append(name)
    if not names:
        raise ConfigError("no platform given; name at least one, for example reddit-archive")
    for name in names:
        validate_platform(name)
    return tuple(dict.fromkeys(names))


def validate_platform(name: str) -> str:
    """Return ``name`` when it is a registered platform, else raise :class:`ConfigError`."""
    if name not in SOURCE_REGISTRY:
        choices = ", ".join(sorted(SOURCE_REGISTRY))
        raise ConfigError(f"unknown platform {name!r}; choose from: {choices}")
    return name


def load_config(path: Path | None = None) -> UserConfig:
    """Read the config file. A missing file is an empty config; a bad one raises."""
    target = path if path is not None else default_config_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return UserConfig(target, False, None, None)
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{target} is not valid TOML: {exc}") from None
    warnings: list[str] = []
    platforms: tuple[str, ...] | None = None
    if "platforms" in data:
        listed = data["platforms"]
        if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
            raise ConfigError(f"{target}: 'platforms' must be a list of platform names")
        known: list[str] = []
        for item in listed:
            name = item.strip().lower()
            if name in SOURCE_REGISTRY:
                known.append(name)
            else:
                warnings.append(f"{target}: ignoring unknown platform {item!r} in 'platforms'")
        if known:
            platforms = tuple(dict.fromkeys(known))
    default: str | None = None
    if "default_platform" in data:
        value = data["default_platform"]
        if not isinstance(value, str):
            raise ConfigError(f"{target}: 'default_platform' must be a platform name")
        if value.strip().lower() in SOURCE_REGISTRY:
            default = value.strip().lower()
        else:
            warnings.append(
                f"{target}: unknown default_platform {value!r}; using {DEFAULT_PLATFORM}"
            )
    event: str | None = None
    if "default_event" in data:
        value = data["default_event"]
        if not isinstance(value, str):
            raise ConfigError(f"{target}: 'default_event' must be an event id")
        if _event_id_ok(value):
            event = value
        else:
            warnings.append(f"{target}: ignoring the malformed default_event {value!r}")
    awake: bool | None = None
    if "keep_awake" in data:
        value = data["keep_awake"]
        if not isinstance(value, bool):
            raise ConfigError(f"{target}: 'keep_awake' must be true or false")
        awake = value
    return UserConfig(target, True, platforms, default, tuple(warnings), event, awake)


def _event_id_ok(value: str) -> bool:
    from bot_farm_silence_tracker.events import valid_event_id

    return valid_event_id(value)


def render(
    platforms: Iterable[str],
    default_platform: str,
    default_event: str | None = None,
    keep_awake: bool | None = None,
) -> str:
    """The file text. Names are validated registry keys and event ids, so they need no
    escaping."""
    listed = ", ".join(f'"{name}"' for name in platforms)
    text = (
        "# Written by `bot-farm-silence-tracker config`. Edit it there or by hand.\n"
        f"platforms = [{listed}]\n"
        f'default_platform = "{default_platform}"\n'
    )
    if default_event is not None:
        text += f'default_event = "{default_event}"\n'
    if keep_awake is not None:
        text += f"keep_awake = {'true' if keep_awake else 'false'}\n"
    return text


def write_config(
    path: Path,
    platforms: Iterable[str],
    default_platform: str,
    default_event: str | None = None,
    keep_awake: bool | None = None,
) -> None:
    """Write the config file atomically, creating its folder."""
    names = tuple(dict.fromkeys(platforms))
    if default_platform not in names:
        raise ConfigError("the default platform must be one of the enabled platforms")
    if default_event is not None and not _event_id_ok(default_event):
        raise ConfigError(f"not an event id: {default_event!r}")
    text = render(names, default_platform, default_event, keep_awake)
    try:
        path = path.resolve()  # Keep a symlinked config file a symlink.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc}") from None


def _load_for_replace(path: Path) -> tuple[UserConfig, tuple[str, ...]]:
    """Read the current file for an explicit change. A file that cannot be read or is
    invalid is not an obstacle: the change replaces it, and a warning says so."""
    try:
        return load_config(path), ()
    except ConfigError as exc:
        notice = f"replaced the unusable config file {path} ({exc})"
        return UserConfig(path, False, None, None), (notice,)


def set_platforms(path: Path, platforms: tuple[str, ...]) -> UserConfig:
    """Record the enabled platforms.

    The default platform stays as it was when the new list still holds it; otherwise it
    becomes the first listed platform, so the default is always an enabled one. An
    unreadable or invalid file is replaced; the result's warnings say so.
    """
    current, notices = _load_for_replace(path)
    default = current.default if current.default in platforms else platforms[0]
    write_config(path, platforms, default, current.default_event, current.keep_awake)
    return replace(load_config(path), warnings=notices)


def set_default(path: Path, platform: str) -> UserConfig:
    """Make ``platform`` the default, adding it to the enabled list when it is not there.

    An unreadable or invalid file is replaced; the result's warnings say so.
    """
    current, notices = _load_for_replace(path)
    enabled = current.enabled
    if platform not in enabled:
        enabled = (*enabled, platform)
    write_config(path, enabled, platform, current.default_event, current.keep_awake)
    return replace(load_config(path), warnings=notices)


def set_default_event(path: Path, event_id: str) -> UserConfig:
    """Make ``event_id`` the event used without ``--event``, keeping the platforms.

    The caller checks that the event exists. An unreadable or invalid file is
    replaced; the result's warnings say so.
    """
    current, notices = _load_for_replace(path)
    write_config(path, current.enabled, current.default, event_id, current.keep_awake)
    return replace(load_config(path), warnings=notices)


def set_keep_awake(path: Path, keep_awake: bool) -> UserConfig:
    """Record the desktop app's keep-awake choice, keeping everything else.

    An unreadable or invalid file is replaced; the result's warnings say so.
    """
    current, notices = _load_for_replace(path)
    write_config(path, current.enabled, current.default, current.default_event, keep_awake)
    return replace(load_config(path), warnings=notices)
