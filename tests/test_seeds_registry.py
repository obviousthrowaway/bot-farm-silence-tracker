from __future__ import annotations

from pathlib import Path

import pytest

from bot_farm_silence_tracker.seeds import load_seeds
from bot_farm_silence_tracker.sources.base import Source
from bot_farm_silence_tracker.sources.fake import FakeSource
from bot_farm_silence_tracker.sources.registry import (
    AdapterNotAvailable,
    MissingCredentials,
    collect_credentials,
    load_source_class,
)


def test_default_seeds_cover_expected_communities() -> None:
    reddit = load_seeds("reddit")
    assert {"worldnews", "ukraine", "UkrainianConflict", "Sakartvelo", "vaccines"} <= set(
        reddit.items
    )
    x = load_seeds("x")
    assert {"#Ukraine", "#StopFundingUkraine", "#AfD", "#Biolabs"} <= set(x.items)


def test_override_and_custom_file(tmp_path: Path) -> None:
    assert load_seeds("reddit", override=["r/Foo", "/r/bar/", "foo"]).items == ("Foo", "bar")
    path = tmp_path / "s.yaml"
    path.write_text('x:\n  queries: ["#One", "two words"]\n')
    assert load_seeds("x", path).items == ("#One", "two words")
    with pytest.raises(ValueError):
        load_seeds("reddit", path)  # No reddit section.
    with pytest.raises(ValueError):
        load_seeds("mastodon")


def test_registry_loads_fake_and_reports_missing_adapters() -> None:
    assert load_source_class("fake") is FakeSource
    with pytest.raises(AdapterNotAvailable, match="not installed yet"):
        load_source_class("reddit", {"reddit": "bot_farm_silence_tracker.sources.nonexistent:Nope"})
    with pytest.raises(AdapterNotAvailable, match="Unknown platform"):
        load_source_class("mastodon")
    with pytest.raises(AdapterNotAvailable, match="Source subclass"):
        load_source_class("x", {"x": "bot_farm_silence_tracker.models:Account"})


def test_collect_credentials() -> None:
    class Needy(Source):
        name = "needy"
        required_credentials = ("A_TOKEN", "B_TOKEN")

        def discover(self, seeds, pool_limit):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def hydrate(self, account):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def fetch_activity(self, account, since, until):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    with pytest.raises(MissingCredentials, match="A_TOKEN, B_TOKEN"):
        collect_credentials(Needy, {})
    assert collect_credentials(Needy, {"A_TOKEN": "a", "B_TOKEN": " b "}) == {
        "A_TOKEN": "a",
        "B_TOKEN": "b",
    }
