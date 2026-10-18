"""Opt-in manual test: a real macOS power assertion, checked with ``pmset``.

Skipped unless BOT_FARM_SILENCE_TRACKER_REAL_ASSERTION=1 on macOS. CI does not set it.
Run it by hand:

    BOT_FARM_SILENCE_TRACKER_REAL_ASSERTION=1 uv run pytest tests/test_keepawake_manual.py
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from bot_farm_silence_tracker import keepawake

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("BOT_FARM_SILENCE_TRACKER_REAL_ASSERTION") != "1",
    reason="opt-in: set BOT_FARM_SILENCE_TRACKER_REAL_ASSERTION=1 on macOS",
)


def assertions() -> str:
    return subprocess.run(
        ["pmset", "-g", "assertions"], capture_output=True, text=True, check=True
    ).stdout


def test_a_real_assertion_is_created_and_released() -> None:
    assert keepawake.REASON not in assertions()
    with keepawake.keep_awake() as active:
        assert active is True
        held = assertions()
        assert keepawake.REASON in held
        assert "PreventUserIdleSystemSleep" in held
    assert keepawake.REASON not in assertions()
