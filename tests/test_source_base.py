"""The Source base class: secrets, warnings, options, rate attributes, credentials."""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Iterator
from typing import ClassVar

import pytest

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.models import Account, Activity
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import ENV_PREFIX, Source, SourceConfig
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.sources.http import HttpClient
from bot_farm_silence_tracker.sources.registry import (
    SOURCE_REGISTRY,
    collect_credentials,
    load_source_class,
)

from .helpers import CUTOFF, NOW

SECRET = "s3cr3t-token-value"


class Minimal(Source):
    name: ClassVar[str] = "minimal"
    required_credentials: ClassVar[tuple[str, ...]] = ("BOT_FARM_SILENCE_TRACKER_MINIMAL_TOKEN",)
    optional_credentials: ClassVar[tuple[str, ...]] = ("BOT_FARM_SILENCE_TRACKER_MINIMAL_PASSWORD",)

    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        return iter(())

    def hydrate(self, account: Account) -> Account:
        return account

    def fetch_activity(self, account, since, until) -> list[Activity]:  # type: ignore[no-untyped-def]
        return []


def config(**options: object) -> SourceConfig:
    return SourceConfig(
        http=HttpClient(),
        credentials={"BOT_FARM_SILENCE_TRACKER_MINIMAL_TOKEN": SECRET},
        now=NOW,
        cutoff=CUTOFF,
        options={"password": SECRET, **options},
    )


def test_secrets_are_absent_from_repr() -> None:
    cfg = config()
    text = repr(cfg)
    assert SECRET not in text
    assert "credentials" not in text and "options" not in text
    assert "cutoff" in text  # The rest of the config is still shown.
    assert SECRET not in str(cfg)


def test_cli_never_shows_locals_in_tracebacks() -> None:
    assert app.pretty_exceptions_show_locals is False


def test_rate_defaults_and_cost_summary() -> None:
    assert (Source.default_rps, Source.default_burst) == (1.0, 1.0)
    assert (FakeSource.default_rps, FakeSource.default_burst) == (1.0, 1.0)
    assert Minimal(config()).cost_summary() is None


def test_warn_dedupes_and_drains() -> None:
    src = Minimal(config())
    src.warn("seed r/gone is banned; skipped")
    src.warn("seed r/gone is banned; skipped")
    src.warn("discovery could not reach the cutoff")
    assert src.drain_warnings() == [
        "seed r/gone is banned; skipped",
        "discovery could not reach the cutoff",
    ]
    assert src.drain_warnings() == []


def test_option_helpers_parse_cli_strings() -> None:
    src = Minimal(
        config(qpm="100", full_archive="false", lang=" en ", depth="3", blank="", flag="yes")
    )
    assert src.option_float("qpm") == 100.0
    assert src.option_int("depth") == 3
    assert src.option_bool("full_archive") is False  # bool("false") would be True.
    assert src.option_bool("flag") is True
    assert src.option_bool("missing", default=True) is True
    assert src.option_str("lang") == "en"
    assert src.option_str("blank", "fallback") == "fallback"
    assert src.option_float("missing", 2.5) == 2.5
    with pytest.raises(ValueError, match="'qpm'"):
        Minimal(config(qpm="fast")).option_float("qpm")
    with pytest.raises(ValueError, match="'depth'"):
        Minimal(config(depth="1.5")).option_int("depth")
    with pytest.raises(ValueError, match="'flag'"):
        Minimal(config(flag="maybe")).option_bool("flag")
    # Library callers may pass real types.
    assert Minimal(config(flag=True)).option_bool("flag") is True


def test_optional_credentials_read_when_present() -> None:
    env = {"BOT_FARM_SILENCE_TRACKER_MINIMAL_TOKEN": "t"}
    assert collect_credentials(Minimal, env) == {"BOT_FARM_SILENCE_TRACKER_MINIMAL_TOKEN": "t"}
    env["BOT_FARM_SILENCE_TRACKER_MINIMAL_PASSWORD"] = "  pw "
    assert collect_credentials(Minimal, env) == {
        "BOT_FARM_SILENCE_TRACKER_MINIMAL_TOKEN": "t",
        "BOT_FARM_SILENCE_TRACKER_MINIMAL_PASSWORD": "pw",
    }
    env["BOT_FARM_SILENCE_TRACKER_MINIMAL_PASSWORD"] = "   "
    assert "BOT_FARM_SILENCE_TRACKER_MINIMAL_PASSWORD" not in collect_credentials(Minimal, env)


def test_registered_adapters_use_the_env_prefix() -> None:
    """Every registered adapter whose module exists loads and names its variables
    ``BOT_FARM_SILENCE_TRACKER_<PLATFORM>_*`` (``env_platform`` when set). A missing module is
    skipped; any other import failure of a present module fails the test."""
    checked = 0
    for platform, target in SOURCE_REGISTRY.items():
        module_name = target.partition(":")[0]
        try:
            present = importlib.util.find_spec(module_name) is not None
        except ModuleNotFoundError:
            present = False
        if not present:
            continue
        cls = load_source_class(platform)  # AdapterNotAvailable here fails the test.
        checked += 1
        prefix = f"{ENV_PREFIX}{(cls.env_platform or platform).upper()}_"
        for name in (*cls.required_credentials, *cls.optional_credentials):
            assert name.startswith(prefix), f"{platform}: {name} lacks {prefix}"
        assert cls.default_rps > 0 and cls.default_burst >= 1
    assert checked >= 1


def test_option_bool_blank_returns_default() -> None:
    assert Minimal(config(flag="")).option_bool("flag", default=True) is True
    assert Minimal(config(flag="  ")).option_bool("flag") is False
    assert Minimal(config(flag=" no ")).option_bool("flag", default=True) is False


def test_warnings_are_logged_at_debug_only(caplog: pytest.LogCaptureFixture) -> None:
    src = Minimal(config())
    with caplog.at_level(logging.DEBUG, logger="bot_farm_silence_tracker.sources.base"):
        src.warn("seed skipped")
        src.warn("seed skipped")
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.DEBUG]  # Once, and not at WARNING (the CLI prints it).
    assert src.drain_warnings() == ["seed skipped"]
