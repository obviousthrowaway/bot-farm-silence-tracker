"""A run's report JSON on any platform adds one row of counts to the main graph."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "add_run_to_chart.py"
DATA = ROOT / "docs" / "charts" / "runs-provisional.json"
GOLDEN = ROOT / "tests" / "golden" / "census"

_spec = importlib.util.spec_from_file_location("add_run_to_chart", SCRIPT)
assert _spec is not None and _spec.loader is not None
add_run_to_chart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(add_run_to_chart)
render_run_chart = add_run_to_chart.render_run_chart


def _report(platform: str = "bluesky", name: str = "census_k1_report.json") -> dict[str, Any]:
    """A golden report of the fake platform, relabelled as a run on ``platform``."""
    report = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    report["run"]["platform"] = platform
    return report


def _data() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _setup(tmp_path: Path, report: dict[str, Any]) -> tuple[Path, list[str]]:
    """A private copy of the aggregates file, a report beside it (outside any git work
    tree), and the arguments that add the report as "Run 1"."""
    assert add_run_to_chart.git_work_tree(tmp_path / "x") is None
    data_path = tmp_path / "runs.json"
    data_path.write_text(DATA.read_text(encoding="utf-8"), encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    args = [str(report_path), "--label", "Run 1", "--data", str(data_path)]
    return data_path, [*args, "--as-of", "2026-10-21"]


@pytest.mark.parametrize("platform", ["reddit-archive", "reddit", "bluesky", "x"])
def test_a_complete_run_on_any_platform_becomes_one_row(platform: str) -> None:
    data = _data()
    row = add_run_to_chart.row_from_report(_report(platform), data["cutoff"], "Run 1")
    assert row == {
        "label": "Run 1",
        "platform": platform,
        "kind": "run",
        "status": "complete",
        "scored_through": "2026-10-15 00:00 UTC",
        "eligible": 52,
        "silent": 14,
        "windows": [{"days": 3.98, "eligible": 52}],
    }
    if platform == "reddit-archive":
        with pytest.raises(ValueError, match="already a row"):
            add_run_to_chart.add_row(data, row)
        return
    added = add_run_to_chart.add_row(data, row)
    assert added["rows"][:-1] == data["rows"] and added["rows"][-1] == row
    assert platform in render_run_chart.platforms(added)
    svg = render_run_chart.render(added, "light")
    assert render_run_chart.platform_label(platform) in svg
    assert "14 of 52" in svg


def test_the_row_holds_counts_only(tmp_path: Path) -> None:
    report = _report()
    names = {c["handle"] for c in report["candidates"]} | {
        c["user_id"] for c in report["candidates"]
    }
    assert names
    data_path, args = _setup(tmp_path, report)
    assert add_run_to_chart.main(args) == 0
    written = [
        data_path,
        tmp_path / "runs-provisional-light.svg",
        tmp_path / "runs-provisional-dark.svg",
    ]
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert not any(name in text for name in names), path.name


def test_main_appends_the_row_and_redraws_both_charts(tmp_path: Path) -> None:
    data_path, args = _setup(tmp_path, _report("x"))
    assert add_run_to_chart.main(args) == 0
    data = json.loads(data_path.read_text(encoding="utf-8"))
    assert data_path.read_text(encoding="utf-8") == json.dumps(data, indent=2) + "\n"
    assert data["as_of"] == "2026-10-21"
    assert data["rows"][:-1] == _data()["rows"]
    assert data["rows"][-1]["platform"] == "x"
    for theme in ("light", "dark"):
        svg = (tmp_path / f"runs-provisional-{theme}.svg").read_text(encoding="utf-8")
        assert svg == render_run_chart.render(data, theme)


def test_replace_updates_the_row_with_the_new_counts(tmp_path: Path) -> None:
    data_path, args = _setup(tmp_path, _report("x"))
    assert add_run_to_chart.main(args) == 0
    newer = _report("x")
    newer["summary"]["eligible"] = 60
    newer["summary"]["cliff"] = 9
    Path(args[0]).write_text(json.dumps(newer), encoding="utf-8")
    # The same label again is refused and changes nothing.
    before = data_path.read_text(encoding="utf-8")
    assert add_run_to_chart.main(args) == 1
    assert data_path.read_text(encoding="utf-8") == before
    assert add_run_to_chart.main([*args, "--replace"]) == 0
    data = json.loads(data_path.read_text(encoding="utf-8"))
    x_rows = [r for r in data["rows"] if r.get("platform") == "x"]
    assert len(x_rows) == 1
    assert (x_rows[0]["eligible"], x_rows[0]["silent"]) == (60, 9)
    assert x_rows[0]["windows"] == [{"days": 3.98, "eligible": 60}]
    svg = (tmp_path / "runs-provisional-light.svg").read_text(encoding="utf-8")
    assert "9 of 60" in svg and "14 of 52" not in svg


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    data_path, args = _setup(tmp_path, _report())
    assert add_run_to_chart.main([*args, "--dry-run"]) == 0
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.json", "runs.json"]


def test_a_report_inside_a_git_work_tree_is_refused(tmp_path: Path, capsys) -> None:
    data_path, args = _setup(tmp_path, _report())
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    inside = clone / "sub" / "run-7.json"
    inside.parent.mkdir()
    inside.write_text(Path(args[0]).read_text(encoding="utf-8"), encoding="utf-8")
    assert add_run_to_chart.git_work_tree(inside) == clone.resolve()
    assert add_run_to_chart.main([str(inside), *args[1:]]) == 1
    err = capsys.readouterr().err
    assert "a path inside a git work tree" in err and "must never be committed" in err
    assert str(clone) not in err and str(tmp_path) not in err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")
    # A worktree's .git is a file; it counts too.
    (clone / ".git").rmdir()
    (clone / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert add_run_to_chart.git_work_tree(inside) == clone.resolve()
    # This repository's own tree is refused as well, without naming its location.
    own = GOLDEN / "census_k1_report.json"
    assert add_run_to_chart.git_work_tree(own) == ROOT
    with pytest.raises(ValueError, match="this repository's work tree") as caught:
        add_run_to_chart.check_report_location(own)
    assert str(ROOT) not in str(caught.value)


def test_run_reports_are_ignored_by_git() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "run-*.json" in ignore


def test_as_of_is_required_and_checked(tmp_path: Path, capsys) -> None:
    data_path, args = _setup(tmp_path, _report())
    without = args[:-2]
    with pytest.raises(SystemExit):
        add_run_to_chart.main(without)
    assert add_run_to_chart.main([*without, "--as-of", "21 Oct"]) == 1
    assert "not a date" in capsys.readouterr().err
    assert add_run_to_chart.main([*without, "--as-of", "2026-10-14"]) == 1
    assert "was scored to (2026-10-15)" in capsys.readouterr().err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")
    assert add_run_to_chart.main([*without, "--as-of", "2026-10-18"]) == 0


@pytest.mark.parametrize("status", ["partial", "cancelled"])
def test_a_stopped_run_is_charted_as_partial(status: str) -> None:
    report = _report()
    report["run"]["status"] = status
    row = add_run_to_chart.row_from_report(report, _data()["cutoff"], "Run 2 (partial)")
    assert row["kind"] == "partial" and row["status"] == "partial"
    assert row["fetched"] == 300 and row["planned"] == 300


def test_a_run_with_early_data_ends_needs_its_shortest_window() -> None:
    report = _report()
    report["summary"]["early_data_end"] = 5
    cutoff = _data()["cutoff"]
    with pytest.raises(ValueError, match="--min-window-days"):
        add_run_to_chart.row_from_report(report, cutoff, "Run 1")
    row = add_run_to_chart.row_from_report(report, cutoff, "Run 1", min_window_days=3.28)
    assert row["window_range"] == [3.28, 3.98] and "windows" not in row
    with pytest.raises(ValueError, match="at most"):
        add_run_to_chart.row_from_report(report, cutoff, "Run 1", min_window_days=5.0)
    with pytest.raises(ValueError, match="only for a run"):
        add_run_to_chart.row_from_report(_report(), cutoff, "Run 1", min_window_days=3.0)


def _changed(path: str, value: Any) -> dict[str, Any]:
    report = _report()
    section, key = path.split(".")
    report[section][key] = value
    return report


@pytest.mark.parametrize(
    ("report", "message"),
    [
        (_report("fake"), "cannot be charted"),
        (_report("mastodon"), "cannot be charted"),
        (_report(name="sampled_k1_report.json"), "two-phase"),
        (_changed("run.provisional", True), "provisional"),
        (_changed("run.status", "running"), "stop a running one"),
        (_changed("run.status", "failed"), "cannot be charted"),
        (_changed("run.status", "sampled"), "cannot be charted"),
        (_changed("run.cutoff", "2026-10-12T00:30:00+00:00"), "not the chart's"),
        (_changed("summary.eligible", 0), "no eligible"),
        (_changed("run.effective_now", "2026-10-10T00:00:00+00:00"), "before the cutoff"),
    ],
)
def test_reports_that_cannot_make_an_honest_row_are_refused(
    report: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        add_run_to_chart.row_from_report(copy.deepcopy(report), _data()["cutoff"], "Run 1")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"cliff": 53}, "silent <= eligible"),
        ({"cliff": -1}, "silent <= eligible"),
        ({"eligible": -5}, "no eligible"),
    ],
)
def test_impossible_counts_are_refused_without_writing(
    tmp_path: Path, capsys, change: dict[str, int], message: str
) -> None:
    report = _report()
    report["summary"].update(change)
    data_path, args = _setup(tmp_path, report)
    assert add_run_to_chart.main(args) == 1
    assert message in capsys.readouterr().err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")


def test_main_reports_a_refusal_without_writing(tmp_path: Path, capsys) -> None:
    data_path, args = _setup(tmp_path, _report("fake"))
    assert add_run_to_chart.main(args) == 1
    assert "cannot be charted" in capsys.readouterr().err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")


def _method() -> dict[str, Any]:
    return add_run_to_chart.expected_method(_data()["event"])


def test_the_published_method_comes_from_the_scoring_defaults_and_the_event() -> None:
    method = _method()
    assert method == {
        "baseline_days": 30,
        "min_active_days": 12,
        "min_posts": 20,
        "recent_days": 7.0,
        "max_weekly_cv": 1.0,
        "cliff_window_hours": 48.0,
        "markers": "sasovo-hub-strike@2026-10-08T06:56:00+00:00,"
        "kaluga-region-strike@2026-10-09T00:00:00+00:00~approx",
        "marker_margin_hours": 12.0,
    }
    # The golden report was scored with that method, so it needs no override.
    row = add_run_to_chart.row_from_report(_report(), _data()["cutoff"], "Run 1", method=method)
    assert "method_note" not in row
    with pytest.raises(ValueError, match=r"not in events\.toml"):
        add_run_to_chart.expected_method("2026-12-unknown")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("baseline_days", 14),
        ("min_active_days", 5),
        ("min_posts", 10),
        ("recent_days", 3.0),
        ("max_weekly_cv", 2.0),
        ("cliff_window_hours", 24.0),
        ("markers", "sasovo-hub-strike@2026-10-08T06:56:00+00:00"),
        ("markers", None),
        ("marker_margin_hours", 6.0),
    ],
)
def test_a_run_with_another_method_is_refused_unless_allowed(key: str, value: Any) -> None:
    report = _report()
    if value is None:
        del report["run"][key]
    else:
        report["run"][key] = value
    cutoff = _data()["cutoff"]
    with pytest.raises(ValueError, match="different method") as caught:
        add_run_to_chart.row_from_report(report, cutoff, "Run 1", method=_method())
    assert key in str(caught.value) and "--allow-nondefault-method" in str(caught.value)
    row = add_run_to_chart.row_from_report(
        report, cutoff, "Run 1", method=_method(), allow_nondefault_method=True
    )
    assert row["method_note"].startswith(f"{key} ")
    added = add_run_to_chart.add_row(_data(), row)
    assert render_run_chart.row_matched(added, added["rows"][-1]) is None
    assert "non-default method" in render_run_chart.render(added, "light")


def test_main_refuses_another_method_and_allows_it_with_the_flag(tmp_path: Path, capsys) -> None:
    report = _report()
    report["run"]["min_posts"] = 10
    data_path, args = _setup(tmp_path, report)
    assert add_run_to_chart.main(args) == 1
    assert "min_posts 10 (default 20)" in capsys.readouterr().err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")
    assert add_run_to_chart.main([*args, "--allow-nondefault-method"]) == 0
    row = json.loads(data_path.read_text(encoding="utf-8"))["rows"][-1]
    assert row["method_note"] == "min_posts 10 (default 20)"


@pytest.mark.parametrize(
    "mangle",
    [
        lambda r: r.pop("run"),
        lambda r: r["run"].pop("effective_now"),
        lambda r: r.update(summary=[]),
        lambda r: r["run"].update(cutoff=5),
    ],
)
def test_a_malformed_report_exits_with_a_clean_message(tmp_path: Path, capsys, mangle) -> None:
    report = _report()
    mangle(report)
    data_path, args = _setup(tmp_path, report)
    assert add_run_to_chart.main(args) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "Traceback" not in err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")


def test_a_missing_report_exits_with_a_clean_message(tmp_path: Path, capsys) -> None:
    _, args = _setup(tmp_path, _report())
    Path(args[0]).unlink()
    assert add_run_to_chart.main(args) == 1
    assert capsys.readouterr().err.startswith("error: cannot read a file")


def test_a_future_as_of_is_refused(tmp_path: Path, capsys) -> None:
    from datetime import UTC, datetime, timedelta

    data_path, args = _setup(tmp_path, _report())
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    assert add_run_to_chart.main([*args[:-2], "--as-of", tomorrow]) == 1
    assert "in the future" in capsys.readouterr().err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")


def test_a_partial_run_that_fetched_more_than_it_planned_is_refused() -> None:
    report = _report()
    report["run"]["status"] = "partial"
    report["summary"]["scored"] = 301
    row = add_run_to_chart.row_from_report(report, _data()["cutoff"], "Run 2 (partial)")
    with pytest.raises(ValueError, match="fetched more accounts than it planned"):
        add_run_to_chart.add_row(_data(), row)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("eligible", 52.7),
        ("eligible", "52"),
        ("eligible", True),
        ("cliff", 14.0),
        ("cliff", "14"),
        ("cliff", False),
        ("early_data_end", 1.5),
        ("early_data_end", "0"),
    ],
)
def test_report_counts_must_be_whole_numbers(key: str, value: Any) -> None:
    report = _report()
    report["summary"][key] = value
    with pytest.raises(ValueError, match=f"the report's {key} is not a whole number"):
        add_run_to_chart.row_from_report(report, _data()["cutoff"], "Run 1")


@pytest.mark.parametrize("key", ["scored", "sampled"])
def test_partial_run_counts_must_be_whole_numbers(key: str) -> None:
    report = _report()
    report["run"]["status"] = "partial"
    report["summary"][key] = 300.5
    with pytest.raises(ValueError, match="not a whole number"):
        add_run_to_chart.row_from_report(report, _data()["cutoff"], "Run 2 (partial)")


def test_main_refuses_a_fractional_count_cleanly(tmp_path: Path, capsys) -> None:
    report = _report()
    report["summary"]["eligible"] = 52.7
    data_path, args = _setup(tmp_path, report)
    assert add_run_to_chart.main(args) == 1
    assert "eligible is not a whole number" in capsys.readouterr().err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")


def test_as_of_may_not_move_before_any_rows_scored_date(tmp_path: Path, capsys) -> None:
    # Run 4 of the committed file was scored to 2026-10-18; the new run to 2026-10-15.
    data_path, args = _setup(tmp_path, _report())
    without = args[:-2]
    assert add_run_to_chart.main([*without, "--as-of", "2026-10-16"]) == 1
    err = capsys.readouterr().err
    assert "before row 'Run 4 (22,083 of 30,000 scored)' was scored to (2026-10-18)" in err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")
    assert add_run_to_chart.main([*without, "--as-of", "2026-10-18"]) == 0
    # A replacement scored later moves the bound forward with it.
    later = _report()
    later["run"]["now"] = later["run"]["effective_now"] = "2026-10-19T12:00:00+00:00"
    Path(args[0]).write_text(json.dumps(later), encoding="utf-8")
    assert add_run_to_chart.main([*without, "--replace", "--as-of", "2026-10-18"]) == 1
    assert "before row 'Run 1' was scored to (2026-10-19)" in capsys.readouterr().err
    assert json.loads(data_path.read_text(encoding="utf-8"))["as_of"] == "2026-10-18"
    assert add_run_to_chart.main([*without, "--replace", "--as-of", "2026-10-19"]) == 0


def test_check_as_of_refuses_an_unreadable_scored_through() -> None:
    rows = [{"label": "Run 1", "scored_through": "mid October"}]
    with pytest.raises(ValueError, match="not in the form"):
        add_run_to_chart.check_as_of("2026-10-20", rows)
    add_run_to_chart.check_as_of("2026-10-20", [{"label": "Union, no date"}])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cutoff", "2026-10-11T00:30:00"),
        ("effective_now", "2026-10-15T00:00:00"),
    ],
)
def test_report_times_without_a_utc_offset_are_refused(key: str, value: str) -> None:
    report = _changed(f"run.{key}", value)
    with pytest.raises(ValueError, match="has no UTC offset"):
        add_run_to_chart.row_from_report(report, _data()["cutoff"], "Run 1")


def test_a_failed_write_exits_cleanly_without_the_path(tmp_path: Path, capsys) -> None:
    data_path, args = _setup(tmp_path, _report())
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("a file where the charts folder should be", encoding="utf-8")
    assert add_run_to_chart.main([*args, "--out-dir", str(blocked / "charts")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: cannot write the aggregates file or the charts")
    assert str(tmp_path) not in err and "Traceback" not in err
    assert data_path.read_text(encoding="utf-8") == DATA.read_text(encoding="utf-8")
