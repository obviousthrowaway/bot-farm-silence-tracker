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


def test_matched_base_is_refused_outside_three_to_seven_days() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        render_run_chart.matched_base(data, 7.86)
    with pytest.raises(ValueError):
        render_run_chart.matched_base(data, 2.0)


def _rows(data: dict) -> dict[str, dict]:
    return {entry["label"]: entry for entry in data["rows"]}


def test_distinct_account_rows_are_not_sums_of_runs() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows = _rows(data)
    runs = [rows["Run 1"], rows["Run 2"], rows["Run 3"]]
    union = rows["Runs 1 to 3, distinct accounts"]
    pooled = rows["All runs pooled, distinct accounts"]
    assert union["eligible"] < sum(r["eligible"] for r in runs)
    assert union["silent"] < sum(r["silent"] for r in runs)
    assert pooled["mixed_windows"] is True
    for entry in data["rows"]:
        assert entry["silent"] <= entry["eligible"]
        assert ("windows" in entry) != ("window_range" in entry)
        if "windows" in entry:
            assert sum(w["eligible"] for w in entry["windows"]) == entry["eligible"]


def test_rows_past_seven_days_or_mixed_get_no_matched_base() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows = _rows(data)
    assert render_run_chart.row_matched(data, rows["Run 4 (partial, in progress)"]) is None
    assert render_run_chart.row_matched(data, rows["All runs pooled, distinct accounts"]) is None
    weighted, low, high = render_run_chart.row_matched(data, rows["Run 2"])
    assert weighted is None
    assert low == pytest.approx(0.0337, abs=1e-4) and high == pytest.approx(0.0397, abs=1e-4)
    weighted, low, high = render_run_chart.row_matched(data, rows["Run 1"])
    assert weighted == pytest.approx(0.0397, abs=1e-4) and low == high


def test_chart_shows_all_rows_and_no_account_names() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    svg = render_run_chart.render(data, "light")
    for label in ("Run 1:", "Run 2:", "Run 3:", "Run 4 (partial, in progress)"):
        assert label in svg
    assert "Runs 1 to 3, distinct accounts" in svg and "All runs pooled" in svg
    assert "rough matched base" in svg and "No verdict" in svg and "never summed" in svg
    assert "mixed windows" in svg and "not a random" in svg
    assert "handle" not in json.dumps(data) and "user_id" not in json.dumps(data)


def test_chart_height_fits_every_row() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    svg = render_run_chart.render(data, "light")
    rows = len(render_run_chart._row_spec(data))
    height = render_run_chart.chart_height(rows)
    assert f'height="{height}"' in svg
    axis_label_y = render_run_chart.ROW_TOP + render_run_chart.ROW_STEP * rows - 20 + 38
    assert axis_label_y < height


def test_every_silence_row_label_states_its_window() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    labels = [row["label"] for row in render_run_chart._row_spec(data)]
    for label in labels:
        assert ": silent " in label and label.endswith(" days")
