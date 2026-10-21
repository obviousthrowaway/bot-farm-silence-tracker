"""The registry chart, the chart workflow's trust rule and the spot-check minimum."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.events import EventMarker, OutageEvent
from bot_farm_silence_tracker.registry.chart import (
    EMPTY_TITLE,
    GROUP_CANDIDATE,
    GROUP_VERIFIED,
    ChartError,
    chart_data,
    esc,
    group_of,
    render_svg,
    write_charts,
)
from bot_farm_silence_tracker.registry.chart_trust import decide, gate_conclusion
from bot_farm_silence_tracker.registry.records import (
    b64e,
    evidence_path,
    registry_evidence_path,
    write_records,
)
from bot_farm_silence_tracker.registry.verify import (
    CHECK_ALL_MAX,
    CHECKABLE,
    MIN_SAMPLE,
    OVERRIDE_LABEL,
    plan_changes,
    spot_check_minimum,
)
from bot_farm_silence_tracker.timeutil import isoformat

from .helpers import EVENTS, FIXTURE_EVENTS

runner = CliRunner()
GOLDEN = Path(__file__).parent / "golden" / "charts"
REPO = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 20)
NOW = datetime.fromisoformat("2026-10-20T12:00:00+00:00")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
MAIN = EVENTS.get("fixture-main")
STRIKES = EVENTS.get("fixture-strikes")
assert MAIN is not None and STRIKES is not None

#: Identities of the fixture accounts: none of these may appear in a chart.
ACCOUNTS = {
    "reddit-archive": [
        ("quietposter01", "quietposter01"),
        ("steadyvoice22", "steadyvoice22"),
        ("morningnews33", "morningnews33"),
        ("clearedone44", "clearedone44"),
        ("stillhere55", "stillhere55"),
    ],
    "bluesky": [
        ("did:plc:abcdefghijklmnop", "lowkey.bsky.social"),
        ("did:plc:qrstuvwxyz234567", "nightowl.bsky.social"),
    ],
    "x": [("1234567890123", "xuser_quiet")],
}


def rec(
    event: OutageEvent,
    platform: str,
    user_id: str,
    username: str,
    stop: datetime,
    *,
    status: str = "candidate",
    cliff: bool = True,
) -> dict[str, Any]:
    scanned = stop + timedelta(days=5)
    id_b64 = b64e(user_id)
    return {
        "event_id": event.id,
        "platform": platform,
        "id_b64": id_b64,
        "username_b64": b64e(username),
        "first_scanned_at": isoformat(scanned),
        "last_scanned_at": isoformat(scanned),
        "last_activity_at": isoformat(stop),
        "scan_window_end": isoformat(scanned - timedelta(hours=1)),
        "baseline_posts": 100,
        "baseline_active_days": 30,
        "cliff": cliff,
        "tool_version": "0.1.0",
        "status": status,
        "evidence": evidence_path(platform, id_b64) if status == "verified_inauthentic" else None,
        "status_updated_at": isoformat(scanned),
    }


def registry(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    """A registry directory with the fixture events file next to it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_EVENTS, tmp_path / "events.toml")
    root = tmp_path / "registry"
    write_records(root, records)
    for r in records:
        if r["status"] == "verified_inauthentic":
            path = root / registry_evidence_path(r["event_id"], r["platform"], r["id_b64"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Evidence\n", encoding="utf-8")
    return root


def one_platform() -> list[dict[str, Any]]:
    cut = MAIN.cutoff
    names = ACCOUNTS["reddit-archive"]
    return [
        rec(MAIN, "reddit-archive", *names[0], cut - timedelta(hours=2)),
        rec(MAIN, "reddit-archive", *names[1], cut - timedelta(hours=1)),
        rec(MAIN, "reddit-archive", *names[2], cut - timedelta(hours=30), status="scanned"),
    ]


def multi_platform() -> list[dict[str, Any]]:
    cut = STRIKES.cutoff
    first = STRIKES.markers[0].time
    second = STRIKES.markers[1].time
    reddit, bsky, x = ACCOUNTS["reddit-archive"], ACCOUNTS["bluesky"], ACCOUNTS["x"]
    return [
        rec(STRIKES, "reddit-archive", *reddit[0], first + timedelta(minutes=20)),
        rec(
            STRIKES,
            "reddit-archive",
            *reddit[1],
            first + timedelta(minutes=50),
            status="verified_inauthentic",
        ),
        rec(STRIKES, "reddit-archive", *reddit[2], second - timedelta(hours=1), status="scanned"),
        rec(STRIKES, "reddit-archive", *reddit[3], cut - timedelta(hours=1), status="cleared"),
        rec(
            STRIKES,
            "reddit-archive",
            *reddit[4],
            cut - timedelta(hours=3),
            status="scanned",
            cliff=False,
        ),
        rec(STRIKES, "bluesky", *bsky[0], second + timedelta(minutes=10)),
        rec(STRIKES, "bluesky", *bsky[1], cut - timedelta(minutes=15)),
        rec(STRIKES, "x", *ACCOUNTS["x"][0], cut + timedelta(hours=2), status="scanned"),
        rec(STRIKES, "x", "9876543210", x[0][1] + "b", cut - timedelta(days=40)),
    ]


CASES = {"empty": [], "one_platform": one_platform(), "multi_markers": multi_platform()}
CASE_EVENTS = {
    "empty": "fixture-main",
    "one_platform": "fixture-main",
    "multi_markers": "fixture-strikes",
}


def render_case(tmp_path: Path, case: str) -> dict[str, str]:
    root = registry(tmp_path, CASES[case])
    out = tmp_path / "out"
    write_charts(root, out, as_of=AS_OF, commit=COMMIT, event_ids=[CASE_EVENTS[case]], now=NOW)
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(out.iterdir())}


@pytest.mark.parametrize("case", sorted(CASES))
def test_golden_and_deterministic(tmp_path: Path, case: str) -> None:
    first = render_case(tmp_path / "a", case)
    second = render_case(tmp_path / "b", case)
    assert first == second
    event_id = CASE_EVENTS[case]
    for theme in ("light", "dark"):
        text = first[f"{event_id}-{theme}.svg"]
        ET.fromstring(text)  # well-formed XML
        golden = GOLDEN / f"{case}-{theme}.svg"
        if os.environ.get("UPDATE_CHART_GOLDEN") == "1":
            golden.parent.mkdir(parents=True, exist_ok=True)
            golden.write_text(text, encoding="utf-8", newline="\n")
        assert text == golden.read_text(encoding="utf-8"), f"regenerate {golden.name}"
    if event_id == "fixture-main":  # the default fixture event also gets default-*.svg
        assert first["default-light.svg"] == first[f"{event_id}-light.svg"]


def test_empty_state(tmp_path: Path) -> None:
    text = render_case(tmp_path, "empty")["fixture-main-light.svg"]
    assert EMPTY_TITLE in text
    assert "from 0 records in fixture-main at commit 0123456." in text
    assert "<title" in text and "<desc" in text


def test_counts_status_split_and_markers(tmp_path: Path) -> None:
    text = render_case(tmp_path, "multi_markers")["fixture-strikes-light.svg"]
    # 4 candidates + 1 verified; cleared and every scanned record (cliff or not) left out.
    assert (
        "5 potential bot accounts: 1 verified by a person, 4 candidate (automatic); "
        "1 cleared by a reviewer (not charted)."
    ) in text
    assert "silent since takedown" not in text
    assert "Reddit (archive) 2" in text and "Bluesky 2" in text and "X 1" in text
    assert "from 9 records in fixture-strikes" in text
    assert "Second strike, Oct 8 18:00 UTC (approx.)" in text
    assert text.count('stroke-dasharray="5 4"') == 2  # the approximate marker, both panels
    # Histogram bucketed by the nearest anchor.
    assert "First strike 2" in text and "Second strike 1" in text
    assert "Silence alone is not proof" in text
    assert "1 earlier" in text  # the stop 40 days before the cutoff is outside the range
    assert " bots" not in text.replace("potential bot accounts", "")


def test_scanned_cliff_records_are_not_charted(tmp_path: Path) -> None:
    # Scanned records with a cliff were scanned, not marked for review: the chart shows
    # none of them, so a registry holding only such records draws the empty chart.
    cut = MAIN.cutoff
    names = ACCOUNTS["reddit-archive"]
    records = [
        rec(MAIN, "reddit-archive", *names[i], cut - timedelta(hours=i + 1), status="scanned")
        for i in range(3)
    ]
    assert all(group_of(r) is None for r in records)
    data = chart_data(MAIN, records, as_of=AS_OF, commit=COMMIT)
    assert data.empty and data.records == 3 and data.cleared == 0
    text = write_and_read(tmp_path, records)
    assert EMPTY_TITLE in text
    assert "0 potential bot accounts: 0 verified by a person, 0 candidate (automatic)." in text
    assert "from 3 records in fixture-main" in text
    assert "silent since takedown" not in text


def test_group_of_by_status() -> None:
    stop = MAIN.cutoff - timedelta(hours=1)
    user_id, username = ACCOUNTS["reddit-archive"][0]

    def group(status: str, cliff: bool = True) -> str | None:
        return group_of(
            rec(MAIN, "reddit-archive", user_id, username, stop, status=status, cliff=cliff)
        )

    assert group("verified_inauthentic") == GROUP_VERIFIED
    assert group("candidate") == GROUP_CANDIDATE
    assert group("scanned") is None
    assert group("scanned", cliff=False) is None
    assert group("cleared") is None


def write_and_read(tmp_path: Path, records: list[dict[str, Any]]) -> str:
    root = registry(tmp_path, records)
    out = tmp_path / "out"
    write_charts(root, out, as_of=AS_OF, commit=COMMIT, event_ids=[MAIN.id], now=NOW)
    return (out / f"{MAIN.id}-light.svg").read_text(encoding="utf-8")


def test_no_identities_in_output(tmp_path: Path) -> None:
    rendered = {
        **render_case(tmp_path / "m", "multi_markers"),
        **render_case(tmp_path / "o", "one_platform"),
    }
    secrets = set()
    for pairs in ACCOUNTS.values():
        for user_id, username in pairs:
            secrets |= {user_id, username, b64e(user_id), b64e(username)}
    secrets |= {"9876543210", b64e("9876543210")}
    for name, text in rendered.items():
        for secret in secrets:
            assert secret not in text, (name, secret)


HOSTILE = "</text><script>alert(\"x\")</script> & 'quote' \x00\x1b"


def test_hostile_names_are_escaped() -> None:
    marker = EventMarker("evil", HOSTILE, STRIKES.markers[0].time, approximate=True)
    event = OutageEvent(
        id="fixture-evil",
        name=HOSTILE,
        cutoff=STRIKES.cutoff,
        baseline_days=30,
        min_silence_days=3.0,
        cliff_window_hours=48.0,
        markers=(marker,),
        marker_margin_hours=12.0,
    )
    stop = rec(
        event,
        "bluesky",
        "did:plc:abcdefghijklmnop",
        "lowkey.bsky.social",
        STRIKES.cutoff - timedelta(hours=1),
    )
    data = chart_data(event, [stop], as_of=AS_OF, commit=COMMIT)
    for theme in ("light", "dark"):
        text = render_svg(data, theme)
        root = ET.fromstring(text)
        assert "<script" not in text and "</text><" not in text.replace("</text>\n<", "")
        assert "\x00" not in text and "\x1b" not in text
        assert all(el.tag.split("}")[-1] != "script" for el in root.iter())
        assert "&lt;script&gt;" in text and "&amp;" in text and "&#39;quote&#39;" in text


def test_esc() -> None:
    assert esc("a<b>&\"'\x07") == "a&lt;b&gt;&amp;&quot;&#39;"


def test_unknown_event_and_invalid_registry(tmp_path: Path) -> None:
    root = registry(tmp_path, one_platform())
    with pytest.raises(ChartError, match="unknown event"):
        write_charts(root, tmp_path / "out", as_of=AS_OF, event_ids=["nope"], now=NOW)
    (root / "events" / "fixture-main" / "reddit-archive" / "zz.jsonl").write_text("{}\n")
    with pytest.raises(ChartError, match="invalid"):
        write_charts(root, tmp_path / "out", as_of=AS_OF, now=NOW)


def test_cli_writes_every_event(tmp_path: Path) -> None:
    root = registry(tmp_path, [])
    out = tmp_path / "charts"
    result = runner.invoke(
        app,
        [
            "registry",
            "chart",
            "--registry",
            str(root),
            "--out",
            str(out),
            "--as-of",
            "2026-10-20",
            "--commit",
            COMMIT[:12],
        ],
    )
    assert result.exit_code == 0, result.output
    names = sorted(p.name for p in out.iterdir())
    assert "default-light.svg" in names and "default-dark.svg" in names
    for event_id in EVENTS.ids:
        assert f"{event_id}-light.svg" in names and f"{event_id}-dark.svg" in names
    bad = runner.invoke(app, ["registry", "chart", "--registry", str(root), "--commit", "x;y"])
    assert bad.exit_code == 2


# -- the workflow's trust rule -------------------------------------------------------

SHA = "a" * 40
HEAD = "b" * 40
HEAD2 = "c" * 40


def pull(number: int, head: str, *, sha: str = SHA, merged: bool = True) -> dict[str, Any]:
    return {
        "number": number,
        "merged_at": "2026-10-13T00:00:00Z" if merged else None,
        "merge_commit_sha": sha,
        "base": {"ref": "main"},
        "head": {"sha": head},
    }


def run(
    run_id: int,
    head: str,
    conclusion: str = "success",
    *,
    created: str = "2026-10-13T00:00:00Z",
    event: str = "pull_request_target",
    path: str = ".github/workflows/registry-gate.yml",
    status: str = "completed",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "event": event,
        "path": path,
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
    }


def push(pulls: list[dict[str, Any]], runs: list[dict[str, Any]], **kw: Any):
    args = {
        "event_name": "push",
        "ref": "refs/heads/main",
        "sha": SHA,
        "default_branch": "main",
        "pulls": pulls,
        "runs": runs,
    }
    args.update(kw)
    return decide(**args)


def test_pr_with_gate_success_updates() -> None:
    decision = push([pull(7, HEAD)], [run(1, HEAD)])
    assert decision.update and decision.pull_requests == (7,)


def test_direct_push_skips() -> None:
    decision = push([], [])
    assert not decision.update and "direct push" in decision.reason
    # A pull request that only contains the commit, or was merged as another one.
    assert not push([pull(7, HEAD, merged=False)], [run(1, HEAD)]).update
    assert not push([pull(7, HEAD, sha="d" * 40)], [run(1, HEAD)]).update
    other_base = pull(7, HEAD)
    other_base["base"] = {"ref": "dev"}
    assert not push([other_base], [run(1, HEAD)]).update


@pytest.mark.parametrize(
    "runs",
    [
        [],  # bypassed: no gate run at all
        [run(1, HEAD, "failure")],
        [run(1, HEAD, "success"), run(2, HEAD, "failure", created="2026-10-13T01:00:00Z")],
        [run(1, HEAD, None, status="in_progress")],
        [run(1, HEAD, event="pull_request")],  # a pull request's own workflow
        [run(1, HEAD, path=".github/workflows/fake-gate.yml")],
        [run(1, HEAD2)],  # another head
    ],
)
def test_gate_failed_or_bypassed_skips(runs: list[dict[str, Any]]) -> None:
    decision = push([pull(7, HEAD)], runs)
    assert not decision.update and "#7" in decision.reason


def test_multiple_prs_require_all() -> None:
    pulls = [pull(7, HEAD), pull(8, HEAD2)]
    assert push(pulls, [run(1, HEAD), run(2, HEAD2)]).update
    decision = push(pulls, [run(1, HEAD), run(2, HEAD2, "failure")])
    assert not decision.update and "#8" in decision.reason and "#7" not in decision.reason


def test_latest_gate_run_decides() -> None:
    runs = [run(1, HEAD, "failure"), run(2, HEAD, "success", created="2026-10-13T02:00:00Z")]
    assert gate_conclusion(runs, HEAD) == "success"
    assert gate_conclusion([], HEAD) == "missing"


def test_branch_and_dispatch_rules() -> None:
    assert not push([pull(7, HEAD)], [run(1, HEAD)], ref="refs/heads/feature").update
    dispatch = {"event_name": "workflow_dispatch", "pulls": [], "runs": []}
    assert push(**dispatch, main_head=SHA).update
    assert not push(**dispatch, main_head="e" * 40).update
    assert not push(**dispatch).update
    assert not push([], [], event_name="schedule").update


def test_decision_cli(tmp_path: Path) -> None:
    import json

    pulls = tmp_path / "pulls.json"
    pulls.write_text(json.dumps([pull(7, HEAD)]))
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps(run(1, HEAD)) + "\n")
    output = tmp_path / "out"
    summary = tmp_path / "summary.md"
    args = [
        "registry",
        "chart-decision",
        "--event-name",
        "push",
        "--ref",
        "refs/heads/main",
        "--sha",
        SHA,
        "--default-branch",
        "main",
        "--pulls",
        str(pulls),
        "--runs",
        str(runs),
        "--output",
        str(output),
        "--summary",
        str(summary),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert output.read_text() == "update=true\n"
    runs.write_text("")
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert output.read_text().endswith("update=false\n")
    assert "Not redrawing" in summary.read_text()


# -- the spot-check minimum ----------------------------------------------------------


def scanned(platform: str, index: int) -> dict[str, Any]:
    ids = {
        "reddit-archive": (f"steady{index:04d}", f"steady{index:04d}"),
        "bluesky": (f"did:plc:acct{index:04d}", f"acct{index:04d}.bsky.social"),
        "x": (f"{1000 + index}", f"xacct{index:04d}"),
        "reddit": (f"steady{index:04d}", f"steady{index:04d}"),
    }
    user_id, username = ids[platform]
    return rec(
        MAIN, platform, user_id, username, MAIN.cutoff - timedelta(hours=2), status="scanned"
    )


def checked_live(plan: Any) -> int:
    return sum(1 for r in plan.selected if r["platform"] in CHECKABLE)


@pytest.mark.parametrize("size", [1, 3, 5, 15])
def test_small_changes_are_checked_in_full(size: int) -> None:
    head = {
        (r["event_id"], r["platform"], r["id_b64"]): r
        for r in (scanned("reddit-archive", i) for i in range(size))
    }
    plan = plan_changes({}, head, seed="s")
    assert len(plan.selected) == size
    assert checked_live(plan) >= spot_check_minimum(plan.changed) == min(size, MIN_SAMPLE)


def test_large_change_samples_queryable_records() -> None:
    # Before the fix the sample was drawn from every scanned-only record, so a large
    # change padded with records the check cannot query could get no live check.
    records = [scanned("x", i) for i in range(40)]
    records += [scanned("reddit-archive", i) for i in range(6)]
    head = {(r["event_id"], r["platform"], r["id_b64"]): r for r in records}
    for seed in ("a", "b", "c", "d"):
        plan = plan_changes({}, head, seed=seed, labels=[OVERRIDE_LABEL])
        assert len(plan.changed) > CHECK_ALL_MAX
        assert checked_live(plan) == 6 >= spot_check_minimum(plan.changed)
        assert all(r["platform"] in CHECKABLE for r in plan.selected)


def test_minimum_holds_for_random_mixes() -> None:
    rng = random.Random(5)
    platforms = ("reddit-archive", "bluesky", "x", "reddit")
    for trial in range(60):
        size = rng.randint(1, 60)
        records = [scanned(rng.choice(platforms), i) for i in range(size)]
        head = {(r["event_id"], r["platform"], r["id_b64"]): r for r in records}
        plan = plan_changes({}, head, seed=str(trial), labels=[OVERRIDE_LABEL])
        assert checked_live(plan) >= spot_check_minimum(plan.changed)
        # The draw is seeded: the same head gives the same sample.
        again = plan_changes({}, head, seed=str(trial), labels=[OVERRIDE_LABEL])
        assert again.selected == plan.selected


# -- the workflow file ---------------------------------------------------------------


def test_workflow_rules() -> None:
    text = (REPO / ".github" / "workflows" / "registry-chart.yml").read_text(encoding="utf-8")
    assert "cancel-in-progress: false" in text
    assert "persist-credentials: false" in text and text.count("persist-credentials: false") == 2
    grants = [line.strip() for line in text.splitlines() if line.startswith("      ")]
    assert grants.count("contents: write") == 1
    assert "41898+github-actions[bot]@users.noreply.github.com" in text
    for line in text.splitlines():
        if line.strip().startswith("- uses:") or line.strip().startswith("uses:"):
            ref = line.split("@", 1)[1].split()[0]
            assert len(ref) == 40, line


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint is not installed")
def test_actionlint() -> None:
    result = subprocess.run(
        ["actionlint", str(REPO / ".github" / "workflows" / "registry-chart.yml")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
