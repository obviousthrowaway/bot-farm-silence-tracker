"""The provisional runs chart renders deterministically from counts only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_run_chart.py"
DATA = ROOT / "docs" / "charts" / "runs-provisional.json"

_spec = importlib.util.spec_from_file_location("render_run_chart", SCRIPT)
assert _spec is not None and _spec.loader is not None
render_run_chart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_run_chart)


def test_wilson_matches_known_value() -> None:
    low, high = render_run_chart.wilson(50, 100)
    assert low == pytest.approx(0.4038, abs=1e-3)
    assert high == pytest.approx(0.5962, abs=1e-3)


def test_wilson_handles_zero_and_rejects_empty() -> None:
    low, high = render_run_chart.wilson(0, 10)
    assert low == 0.0 and 0.0 < high < 0.4
    with pytest.raises(ValueError):
        render_run_chart.wilson(0, 0)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_render_is_valid_deterministic_svg(theme: str) -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    first = render_run_chart.render(data, theme)
    assert first == render_run_chart.render(data, theme)
    ElementTree.fromstring(first)
    assert "went silent" in first


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_committed_charts_are_current(theme: str) -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    committed = ROOT / "docs" / "charts" / f"runs-provisional-{theme}.svg"
    assert committed.read_text(encoding="utf-8") == render_run_chart.render(data, theme)


def test_matched_base_interpolates_between_three_and_seven_days() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    assert render_run_chart.matched_base(data, 3.0) == pytest.approx(0.0420, abs=1e-4)
    assert render_run_chart.matched_base(data, 7.0) == pytest.approx(0.00865, abs=1e-4)
    assert render_run_chart.matched_base(data, 3.28) == pytest.approx(0.0397, abs=1e-4)
    assert render_run_chart.matched_base(data, 4.0) == pytest.approx(0.0337, abs=1e-4)


def test_matched_range_weights_the_window_mix() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    windows = [{"days": 3.28, "eligible": 1}, {"days": 4.0, "eligible": 3}]
    weighted, low, high = render_run_chart.matched_range(data, windows)
    assert low == pytest.approx(0.0337, abs=1e-4)
    assert high == pytest.approx(0.0397, abs=1e-4)
    assert weighted == pytest.approx((0.25 * 0.0397) + (0.75 * 0.0337), abs=1e-4)
    single, s_low, s_high = render_run_chart.matched_range(data, windows[:1])
    assert single == s_low == s_high


def test_union_and_new_only_are_not_sums_of_runs() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    runs = {r["run_id"]: r for r in data["runs"]}
    assert data["union"]["eligible"] < sum(r["eligible"] for r in runs.values())
    assert data["union"]["silent"] < sum(r["silent"] for r in runs.values())
    assert data["new_only"]["eligible"] <= runs[3]["eligible"]
    assert data["new_only"]["silent"] <= runs[3]["silent"]
    for entry in [*runs.values(), data["union"], data["new_only"]]:
        assert sum(w["eligible"] for w in entry["windows"]) == entry["eligible"]
        assert entry["silent"] <= entry["eligible"]


def test_chart_shows_all_rows_and_no_account_names() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    svg = render_run_chart.render(data, "light")
    assert "Run 1" in svg and "Run 2" in svg and "Run 3 (partial)" in svg
    assert "All distinct accounts" in svg and "accounts not in runs 1 or 2" in svg
    assert "rough matched base" in svg and "no verdict" in svg and "never summed" in svg
    assert "handle" not in json.dumps(data) and "user_id" not in json.dumps(data)


def test_every_silence_row_label_states_its_window() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    labels = [row["label"] for row in render_run_chart._row_spec(data)]
    new_only = [label for label in labels if label.startswith(data["new_only"]["label"])]
    assert len(new_only) == 1
    assert ": silent " in new_only[0] and new_only[0].endswith(" days")
