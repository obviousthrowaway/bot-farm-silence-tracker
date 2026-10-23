"""The provisional runs chart renders deterministically from counts only."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
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


def _interpolated() -> dict:
    """The committed file as if its base rates matched the runs' definition and no row
    gave its own matched base, so the chart interpolates one from the base rates."""
    data = json.loads(DATA.read_text(encoding="utf-8"))
    data["base_rate"].pop("definition", None)
    for entry in data["rows"]:
        entry.pop("matched_base", None)
    return data


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
    data = _interpolated()
    rows = _rows(data)
    assert render_run_chart.row_matched(data, rows["Run 4 (22,083 of 30,000 scored)"]) is None
    assert render_run_chart.row_matched(data, rows["All runs pooled, distinct accounts"]) is None
    weighted, low, high = render_run_chart.row_matched(data, rows["Run 2"])
    assert weighted is None
    assert low == pytest.approx(0.0337, abs=1e-4) and high == pytest.approx(0.0397, abs=1e-4)
    weighted, low, high = render_run_chart.row_matched(data, rows["Run 1"])
    assert weighted == pytest.approx(0.0397, abs=1e-4) and low == high


def test_chart_shows_all_rows_and_no_account_names() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    svg = render_run_chart.render(data, "light")
    for label in ("Run 1:", "Run 2:", "Run 3:", "Run 4 (22,083 of 30,000 scored)"):
        assert label in svg
    assert "Runs 1 to 3, distinct accounts" in svg and "All runs pooled" in svg
    assert "matched base, same definition as the runs" in svg
    assert "No verdict" in svg and "never summed" in svg
    assert "mixed windows" in svg and "not a random" in svg
    assert "handle" not in json.dumps(data) and "user_id" not in json.dumps(data)


@pytest.mark.parametrize("extra", [0, 1, 2])
def test_chart_height_fits_every_row(extra: int) -> None:
    data = _multi() if extra else json.loads(DATA.read_text(encoding="utf-8"))
    if extra == 1:
        data["rows"] = [r for r in data["rows"] if r.get("platform") != "x"]
    svg = render_run_chart.render(data, "light")
    rows = len(render_run_chart._row_spec(data))
    groups = len(render_run_chart.platforms(data))
    assert groups == 1 + extra
    height = render_run_chart.chart_height(rows, groups)
    assert f'height="{height}"' in svg
    label = re.search(r'<text x="[0-9.]+" y="([0-9.]+)"[^>]*>Share of eligible', svg)
    assert label is not None and float(label.group(1)) + 8 < height


def test_every_silence_row_label_states_its_window() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    labels = [row["label"] for row in render_run_chart._row_spec(data)]
    for label in labels:
        assert ": silent " in label and label.endswith(" days")


# Every platform's results go on this one chart, one group per platform.

BLUESKY_BASE = {
    "note": "example base rates for the tests",
    "silent_3d": {"events": 30, "eligible": 1000, "weekly_rates": [0.02, 0.04]},
    "silent_7d": {"events": 10, "eligible": 1000, "weekly_rates": [0.008, 0.012]},
}


def _multi() -> dict:
    """The committed file plus a Bluesky run with base rates and an X run without."""
    data = json.loads(DATA.read_text(encoding="utf-8"))
    data["rows"] += [
        {
            "label": "Run 1",
            "platform": "x",
            "kind": "run",
            "eligible": 400,
            "silent": 44,
            "windows": [{"days": 5.0, "eligible": 400}],
        },
        {
            "label": "Run 1",
            "platform": "bluesky",
            "kind": "run",
            "eligible": 3000,
            "silent": 60,
            "windows": [{"days": 4.0, "eligible": 3000}],
        },
    ]
    data["base_rates"] = {"bluesky": copy.deepcopy(BLUESKY_BASE)}
    return data


def _ys(svg: str, text: str) -> list[float]:
    pattern = rf'<text x="[0-9.]+" y="([0-9.]+)"[^>]*>{re.escape(text)}</text>'
    return [float(y) for y in re.findall(pattern, svg)]


def test_rows_without_a_platform_are_reddit_archive() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    assert all("platform" not in entry for entry in data["rows"])
    assert render_run_chart.platforms(data) == ["reddit-archive"]
    assert {row["platform"] for row in render_run_chart._row_spec(data)} == {"reddit-archive"}
    svg = render_run_chart.render(data, "light")
    assert len(_ys(svg, "Reddit archive")) == 1
    explicit = copy.deepcopy(data)
    for entry in explicit["rows"]:
        entry["platform"] = "reddit-archive"
    assert render_run_chart.render(explicit, "light") == svg


def test_each_platform_is_its_own_group_in_a_fixed_order() -> None:
    data = _multi()
    assert render_run_chart.platforms(data) == ["reddit-archive", "bluesky", "x"]
    svg = render_run_chart.render(data, "dark")
    ElementTree.fromstring(svg)
    assert svg == render_run_chart.render(data, "dark")
    reddit, bluesky, x = (_ys(svg, name)[0] for name in ("Reddit archive", "Bluesky", "X"))
    assert reddit < bluesky < x
    run_rows = _ys(svg, "Run 1: silent 4.0 days") + _ys(svg, "Run 1: silent 5.0 days")
    assert bluesky < run_rows[0] < x < run_rows[1]
    # Ordinary weeks are drawn for the two platforms with base rates, never for X.
    assert len(_ys(svg, "Ordinary weeks: silent 3 days")) == 2
    assert all(y < x for y in _ys(svg, "Ordinary weeks: silent 7 days"))
    assert "no ordinary-week base rate for X yet" in svg
    assert "No ordinary-week base rate has been measured for X yet." in svg
    assert "never pooled across platforms" in svg and "Platforms are never pooled" in svg


def test_matched_base_uses_the_rows_own_platform() -> None:
    data = _multi()
    rows = {(render_run_chart.row_platform(e), e["label"]): e for e in data["rows"]}
    bluesky = render_run_chart.row_matched(data, rows[("bluesky", "Run 1")])
    assert bluesky is not None
    weighted, low, high = bluesky
    assert weighted == pytest.approx(0.025) and low == high == weighted
    assert render_run_chart.matched_base(data, 4.0) == pytest.approx(0.0337, abs=1e-4)
    assert render_run_chart.row_matched(data, rows[("x", "Run 1")]) is None
    with pytest.raises(ValueError, match="no base rates"):
        render_run_chart.matched_base(data, 4.0, "x")


def test_a_platform_without_rows_gets_no_group() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    data["base_rates"] = {"bluesky": copy.deepcopy(BLUESKY_BASE)}
    svg = render_run_chart.render(data, "light")
    assert render_run_chart.platforms(data) == ["reddit-archive"]
    assert not _ys(svg, "Bluesky") and "Bluesky" not in svg
    assert len(_ys(svg, "Ordinary weeks: silent 3 days")) == 1


def test_a_platform_without_base_rates_has_no_ordinary_weeks() -> None:
    data = _interpolated()
    del data["base_rate"]
    svg = render_run_chart.render(data, "light")
    assert "Ordinary weeks" not in svg and "Grey band" not in svg
    assert "rough matched base" in svg  # The legend line stays; no row has a marker.
    assert "matched base ~" not in svg
    assert "no ordinary-week base rate for Reddit archive yet" in svg


def test_the_axis_grows_for_higher_rates() -> None:
    data = _multi()
    svg = render_run_chart.render(data, "light")
    top, step = render_run_chart.axis_max(render_run_chart._row_spec(data))
    assert (top, step) == (0.16, 2)
    assert ">16%</text>" in svg and ">1%</text>" not in svg
    current = json.loads(DATA.read_text(encoding="utf-8"))
    assert render_run_chart.axis_max(render_run_chart._row_spec(current)) == (0.06, 1)
    data["rows"][-1]["silent"] = 2900
    assert render_run_chart.axis_max(render_run_chart._row_spec(data)) == (1.0, 10)


def _broken(change) -> dict:
    data = _multi()
    change(data)
    return data


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["rows"][0].update(platform="fake"), "unknown platform"),
        (lambda d: d["rows"][0].update(platform="all"), "unknown platform"),
        (lambda d: d["rows"][0].update(handle="someone"), "does not know"),
        (lambda d: d["rows"][0].update(silent=7000), "silent <= eligible"),
        (lambda d: d["rows"][0].update(eligible=0, silent=0), "silent <= eligible|no eligible"),
        (lambda d: d["rows"][0].update(window_range=[3.0, 4.0]), "exactly one"),
        (lambda d: d["rows"][0]["windows"][0].update(eligible=1), "add up"),
        (lambda d: d["rows"].append(dict(d["rows"][0])), "appears twice"),
        (lambda d: d["base_rates"].update({"reddit-archive": BLUESKY_BASE}), "base_rate and"),
        (lambda d: d["base_rates"].update({"mastodon": BLUESKY_BASE}), "not a platform"),
        (lambda d: d["base_rates"]["bluesky"].pop("note"), "need a note"),
        (lambda d: d["base_rates"]["bluesky"].pop("silent_7d"), "need silent_7d"),
        (lambda d: d.update(rows=[]), "at least one row"),
    ],
)
def test_validate_refuses_what_the_chart_cannot_draw_honestly(change, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        render_run_chart.render(_broken(change), "light")


def test_the_same_label_on_two_platforms_is_fine() -> None:
    data = _multi()
    labels = [e["label"] for e in data["rows"]]
    assert labels.count("Run 1") == 3
    render_run_chart.validate(data)


def test_write_charts_writes_both_themes(tmp_path: Path) -> None:
    data = _multi()
    paths = render_run_chart.write_charts(data, tmp_path)
    assert [p.name for p in paths] == ["runs-provisional-light.svg", "runs-provisional-dark.svg"]
    for path, theme in zip(paths, ("light", "dark"), strict=True):
        assert path.read_bytes() == render_run_chart.render(data, theme).encode("utf-8")


IDENTITY_KEYS = [
    "handle",
    "usernames",
    "user_id",
    "account_ids",
    "id",
    "did",
    "screen_name",
    "candidates",
    "accounts",
    "authors",
    "u/someuser",
    "Label ",
]


@pytest.mark.parametrize("key", IDENTITY_KEYS)
@pytest.mark.parametrize("where", ["top", "row", "window", "base", "rate"])
def test_unknown_keys_are_refused_at_every_level_without_echoing_them(key: str, where: str) -> None:
    data = _multi()
    target = {
        "top": data,
        "row": data["rows"][0],
        "window": data["rows"][0]["windows"][0],
        "base": data["base_rates"]["bluesky"],
        "rate": data["base_rates"]["bluesky"]["silent_3d"],
    }[where]
    target[key] = ["someone"]
    with pytest.raises(ValueError, match="a key the chart does not know") as caught:
        render_run_chart.validate(data)
    assert repr(key) not in str(caught.value) and "someone" not in str(caught.value)


def test_base_rates_keys_must_be_platforms_and_are_not_echoed() -> None:
    data = _multi()
    data["base_rates"]["u/someuser"] = copy.deepcopy(BLUESKY_BASE)
    with pytest.raises(ValueError, match="not a platform") as caught:
        render_run_chart.validate(data)
    assert "someuser" not in str(caught.value)


HANDLE_TEXT = [
    "u/someuser",
    "/u/some_one",
    "Run 1 by u/abc",
    "@someone",
    "Run 1 (@some.one)",
    "@ab",
    "@123abc",
    "https://reddit.com/u/someuser",
    "https://www.reddit.com/user/someuser/",
    "old.reddit.com/r/somewhere/comments/abc",
    "https://x.com/someuser",
    "twitter.com/someuser/status/1",
    "bsky.app/profile/alice.bsky.social",
    "alice.bsky.social",
    "U/SomeUser",
    "/user/someuser",
    "/USER/SomeUser",
    "/u/someuser",
    "did:plc:abcdefghijklmnopqrstuvwx",
    "DID:web:example.org",
    "t2_abc123",
    "account T2_9xyz",
    "3 u/day",
]


@pytest.mark.parametrize("text", HANDLE_TEXT)
def test_handle_like_text_and_profile_links_are_refused_without_echoing_them(text: str) -> None:
    for place in ("label", "status"):
        data = _multi()
        data["rows"][0][place] = text
        with pytest.raises(ValueError, match="looks like an account handle") as caught:
            render_run_chart.validate(data)
        assert text not in str(caught.value)
        assert f"rows[0].{place}" in str(caught.value)
    data = _multi()
    data["base_rates"]["bluesky"]["note"] = f"measured with {text}"
    with pytest.raises(ValueError, match="looks like an account handle"):
        render_run_chart.validate(data)


@pytest.mark.parametrize(
    "text",
    [
        "Run 1b",
        "Runs 1 and 2 (u/ is not a name)",
        "user@host",
        "someone@example.com",
        "4 @ 3 days",
        "stopped @03:00",
        "stopped @03:00 UTC",
        "https://github.com/obviousthrowaway/bot-farm-silence-tracker",
        "see box.com/files",
        "the x.com API",
        "docs/user/guide",
        "did not post",
        "a did: plc note",
        "t2 timings",
    ],
)
def test_ordinary_text_is_not_mistaken_for_a_handle(text: str) -> None:
    data = _multi()
    data["rows"][0]["label"] = text
    data["rows"][0]["status"] = text
    render_run_chart.validate(data)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda e: e.update(platform="someuser"), "unknown platform"),
        (lambda e: e.update(kind="someuser"), "unknown kind"),
    ],
)
def test_unknown_platform_and_kind_values_are_not_echoed(change, message: str) -> None:
    data = _multi()
    change(data["rows"][0])
    with pytest.raises(ValueError, match=message) as caught:
        render_run_chart.validate(data)
    assert "someuser" not in str(caught.value)


def test_fetched_must_not_exceed_planned() -> None:
    data = _multi()
    partial = data["rows"][4]
    assert partial["kind"] == "partial"
    partial["fetched"] = partial["planned"] + 1
    with pytest.raises(ValueError, match="fetched more accounts than it planned"):
        render_run_chart.validate(data)
    partial["fetched"] = True
    with pytest.raises(ValueError, match="fetched to be a whole count"):
        render_run_chart.validate(data)


def test_a_row_with_a_method_note_has_no_matched_base() -> None:
    data = _multi()
    entry = data["rows"][0]
    assert render_run_chart.row_matched(data, entry) is not None
    entry["method_note"] = "min_posts 10 (default 20)"
    assert render_run_chart.row_matched(data, entry) is None
    assert "non-default method, no matched base" in render_run_chart._row(data, entry)["sub"]
    svg = render_run_chart.render(data, "light")
    assert "non-default method (min_posts 10 (default 20))" in svg
    entry["method_note"] = ""
    with pytest.raises(ValueError, match="method_note to be text"):
        render_run_chart.validate(data)


def _first(data: dict) -> dict:
    return data["rows"][0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: _first(d).update(silent=True), "whole counts"),
        (lambda d: _first(d).update(eligible=True, silent=False), "whole counts"),
        (lambda d: _first(d).update(silent=-1), "whole counts"),
        (lambda d: _first(d).update(silent=2.5), "whole counts"),
        (lambda d: _first(d)["windows"][0].update(days=float("nan")), "finite, positive"),
        (lambda d: _first(d)["windows"][0].update(days=float("inf")), "finite, positive"),
        (lambda d: _first(d)["windows"][0].update(days=True), "finite, positive"),
        (lambda d: _first(d)["windows"][0].update(days="3.28"), "finite, positive"),
        (lambda d: _first(d)["windows"][0].update(eligible=True), "finite, positive"),
        (lambda d: _first(d)["windows"][0].pop("eligible"), "days and eligible"),
        (lambda d: _first(d).update(windows=[3.28]), "days and eligible"),
        (lambda d: _first(d).update(windows={"days": 3.28}), "list of windows"),
        (lambda d: _first(d).update(windows=[]), "list of windows"),
        (lambda d: _first(d).update(mixed_windows="yes"), "true or false"),
        (lambda d: d["rows"][1].update(window_range=["3", "4"]), "two finite days"),
        (lambda d: d["rows"][1].update(window_range=[3.0]), "two finite days"),
        (lambda d: d["rows"][1].update(window_range=[4.0, 3.0]), "two finite days"),
        (lambda d: d["rows"][1].update(window_range=[3.0, float("nan")]), "two finite days"),
        (lambda d: d["rows"][1].update(window_range=3.5), "two finite days"),
        (lambda d: d["rows"][5].pop("mixed_windows"), "needs mixed_windows: true"),
        (
            lambda d: d["base_rate"]["silent_3d"].update(weekly_rates=[0.01, 0.02]),
            "outside its weekly rates",
        ),
        (
            lambda d: d["base_rates"]["bluesky"]["silent_7d"].update(events=True),
            "whole counts",
        ),
        (
            lambda d: d["base_rates"]["bluesky"]["silent_7d"].update(weekly_rates=[True]),
            "weekly rates between",
        ),
        (lambda d: d.update(rows="Run 1"), "at least one row"),
    ],
)
def test_malformed_values_are_refused_with_a_reason(change, message: str) -> None:
    data = _multi()
    assert data["rows"][5]["kind"] == "pooled" and "window_range" in data["rows"][1]
    change(data)
    with pytest.raises(ValueError, match=message):
        render_run_chart.validate(data)


@pytest.mark.parametrize(
    ("window_range", "sub"),
    [
        ([2.5, 7.5], "no matched base outside 3 to 7 days"),
        ([7.5, 8.0], "compare with the 7-day row (no matched base past 7 days)"),
        ([2.0, 2.5], "no matched base under 3 days"),
        ([2.5, 4.0], "no matched base under 3 days"),
        ([3.0, 7.0], "matched base ~"),
    ],
)
def test_rows_outside_three_to_seven_days_say_why_they_have_no_matched_base(
    window_range: list[float], sub: str
) -> None:
    data = _interpolated()
    entry = data["rows"][1]
    entry["window_range"] = window_range
    row = render_run_chart._row(data, entry)
    assert sub in row["sub"]
    assert (row["matched"] is None) == (sub != "matched base ~")


def test_the_partial_note_says_only_that_the_row_is_not_a_random_subsample() -> None:
    svg = render_run_chart.render(json.loads(DATA.read_text(encoding="utf-8")), "light")
    assert "Partial rows cover part of a draw, scored in fetch order, so they are not a" in svg
    assert "so it is not a random subsample of the draw" in svg
    for text in (
        "still running",
        "will change",
        "may change",
        "paused",
        "resum",
        "stopped",
        "in progress",
    ):
        assert text not in svg


def test_a_base_of_another_definition_is_a_reference_only() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows = _rows(data)
    assert data["base_rate"]["definition"]
    # Run 4 and the pooled row get no matched base from a base of another definition.
    run4 = render_run_chart._row(data, rows["Run 4 (22,083 of 30,000 scored)"])
    assert run4["matched"] is None
    assert "no matched base under the runs' definition" in run4["sub"]
    assert "compare with the 7-day row" not in run4["sub"]
    # Without its own matched base, a row of runs 1 to 3 gets none either.
    entry = copy.deepcopy(rows["Run 3"])
    entry.pop("matched_base")
    assert render_run_chart.row_matched(data, entry) is None
    svg = render_run_chart.render(data, "light")
    assert svg.count("other definition (48 h window, no markers), reference only") == 2
    assert "so they are a reference only and not a matched comparison" in svg
    data["base_rate"]["definition"] = ""
    with pytest.raises(ValueError, match="definition to be text"):
        render_run_chart.validate(data)


def test_a_rows_own_matched_base_is_drawn_as_given() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows = _rows(data)
    union = rows["Runs 1 to 3, distinct accounts"]
    assert render_run_chart.row_matched(data, union) == (0.0347, 0.0341, 0.0363)
    assert render_run_chart.row_matched(data, rows["Run 1"]) == (0.0441, 0.0441, 0.0441)
    sub = render_run_chart._row(data, union)["sub"]
    assert sub == "698 of 19,868; matched base ~3.47% (3.41-3.63)"
    # A method note still drops the matched base.
    entry = copy.deepcopy(rows["Run 1"])
    entry["method_note"] = "min_posts 10 (default 20)"
    assert render_run_chart.row_matched(data, entry) is None


@pytest.mark.parametrize(
    "given",
    [
        {"rate": 0.03, "low": 0.04, "high": 0.05},
        {"rate": 0.03, "low": 0.02},
        {"rate": True, "low": 0.0, "high": 0.1},
        {"rate": 1.5, "low": 0.0, "high": 2.0},
        [0.03, 0.02, 0.04],
    ],
)
def test_a_malformed_matched_base_is_refused(given) -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    data["rows"][0]["matched_base"] = given
    with pytest.raises(ValueError, match="matched_base"):
        render_run_chart.validate(data)


def test_unknown_matched_base_keys_are_refused() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    data["rows"][0]["matched_base"]["source"] = "anything"
    with pytest.raises(ValueError, match=r"rows\[0\]\.matched_base has a key"):
        render_run_chart.validate(data)


def test_a_mixed_window_row_with_a_matched_base_is_refused() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    pooled = next(e for e in data["rows"] if e.get("mixed_windows"))
    pooled["matched_base"] = {"rate": 0.03, "low": 0.02, "high": 0.04}
    with pytest.raises(ValueError, match="mixes windows, so it cannot carry a matched_base"):
        render_run_chart.validate(data)
