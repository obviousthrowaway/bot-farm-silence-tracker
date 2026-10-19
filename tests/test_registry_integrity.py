"""Adversarial tests of the registry's integrity rules, the policy and the gate.

Each critic proof-of-concept case is here as a test that failed before the fix.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.registry import records as R
from bot_farm_silence_tracker.registry import verify as V
from bot_farm_silence_tracker.registry.build import registered_ids
from bot_farm_silence_tracker.registry.gate import PullRequest, render_gate, run_gate
from bot_farm_silence_tracker.registry.reports import build_drafts, render_bluesky, render_reddit
from bot_farm_silence_tracker.sources.base import NewestActivity
from bot_farm_silence_tracker.timeutil import isoformat

from .helpers import EVENT, EVENTS, FIXTURE_EVENTS

runner = CliRunner()
REPO = Path(__file__).resolve().parent.parent
#: The default fixture event's folder, relative to the registry.
EVENT_DIR = f"events/{EVENT.id}"
NOW = datetime(2026, 10, 12, tzinfo=UTC)
T = datetime(2026, 9, 1, tzinfo=UTC)
LAG = timedelta(hours=1)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "current_time", lambda: NOW)


def rec(**changes: Any) -> dict[str, Any]:
    """A valid Bluesky candidate: last activity at T, data to T + 30 days."""
    base = {
        "event_id": EVENT.id,
        "platform": "bluesky",
        "id_b64": R.b64e("did:plc:abc"),
        "username_b64": R.b64e("x.bsky.social"),
        "first_scanned_at": isoformat(T + timedelta(days=30)),
        "last_scanned_at": isoformat(T + timedelta(days=30)),
        "last_activity_at": isoformat(T),
        "scan_window_end": isoformat(T + timedelta(days=30)),
        "baseline_posts": 50,
        "baseline_active_days": 30,
        "cliff": True,
        "tool_version": "1",
        "status": "candidate",
        "evidence": None,
        "status_updated_at": isoformat(T + timedelta(days=30)),
    }
    base.update(changes)
    return base


def arch(name: str, **changes: Any) -> dict[str, Any]:
    """A valid reddit-archive record of ``name``."""
    fields: dict[str, Any] = {
        "platform": "reddit-archive",
        "id_b64": R.b64e(name),
        "username_b64": R.b64e(name),
    }
    fields.update(changes)
    return rec(**fields)


def keyed(*records: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {R.record_key(r): r for r in records}


class Probe:
    """The live source: activity times by decoded ID."""

    def __init__(self, activity: dict[str, list[datetime]] | None = None) -> None:
        self.activity = activity or {}
        self.requests_made = 0
        self.ids: list[str] = []

    def newest(self, record: dict[str, Any], since: datetime, until: datetime) -> NewestActivity:
        self.requests_made += 1
        user = R.b64d(record["id_b64"])
        self.ids.append(user)
        hits = [a for a in self.activity.get(user, []) if since <= a < until]
        return NewestActivity(max(hits) if hits else None, since)

    def close(self) -> None:
        pass


def honest(*records: dict[str, Any]) -> Probe:
    """A probe where every record's last activity exists and nothing follows it."""
    from bot_farm_silence_tracker.timeutil import parse_datetime

    return Probe(
        {
            R.b64d(r["id_b64"]): [
                parse_datetime(r["last_activity_at"]) - timedelta(days=i) for i in range(5)
            ]
            for r in records
        }
    )


# -- the critic's proof-of-concept cases ----------------------------------------------


def test_poc_hidden_activity_fails_check_b() -> None:
    probe = Probe({"did:plc:abc": [T, T + timedelta(days=20)]})
    out = V.check_record(rec(), probe, now=NOW, lag=LAG)
    assert out.verdict == V.FAILED and out.checks["B"] == V.FAILED


def test_poc_window_ending_at_the_last_activity_is_invalid() -> None:
    # Before: valid, and Check B had no window, so hidden activity passed.
    bad = rec(scan_window_end=isoformat(T), last_scanned_at=isoformat(T + timedelta(days=2)))
    assert any("days between" in e for e in R.record_errors(bad))


def test_poc_data_gap_over_three_days_is_invalid() -> None:
    # Before: valid, and only "inconclusive" at check time.
    bad = rec(scan_window_end=isoformat(T + timedelta(hours=1)))
    errors = R.record_errors(bad)
    assert any("before last_scanned_at" in e for e in errors)
    assert any("days between" in e for e in errors)


def test_poc_deletion_fails_without_the_label() -> None:
    base = keyed(rec(id_b64=R.b64e("did:plc:gone")))
    report = V.run_verify(base, {}, Probe(), seed="x", now=NOW, lag=LAG)
    assert not report.ok
    assert any("removed" in f for f in report.failures)
    assert "(removed)" in V.render_summary(report)
    allowed = V.run_verify(base, {}, Probe(), seed="x", now=NOW, labels=[V.OVERRIDE_LABEL])
    assert allowed.ok and allowed.plan.overridden


def test_poc_future_status_date_is_rejected_everywhere(tmp_path: Path) -> None:
    evidence = R.evidence_path("bluesky", rec()["id_b64"])
    verified = rec(
        status="verified_inauthentic",
        evidence=evidence,
        status_updated_at=isoformat(T + timedelta(days=31)),
    )
    forged = rec(status="cleared", status_updated_at="9999-01-01T00:00:00+00:00")
    assert any("in the future" in e for e in R.record_errors(forged))
    # It would win every merge, so it must never load.
    assert R.merge_records(verified, forged)["status"] == "cleared"
    root = tmp_path / "registry"
    R.write_records(root, [forged])
    with pytest.raises(ValueError, match="in the future"):
        R.load_records(root)
    assert any("in the future" in p for p in R.validate_registry(root))
    # The scan-skip lookup ignores it instead of failing.
    assert registered_ids(root, "bluesky", NOW, 365, EVENT.id) == set()


def test_poc_bluesky_dids_spread_over_shards() -> None:
    shards = {R.shard_of(R.b64e(f"did:plc:{x}")) for x in ["aaa", "zzz", "q7w", "0000"]}
    assert len(shards) > 1


def test_poc_reddit_archive_id_must_match_the_username() -> None:
    bad = arch("botacct", username_b64=R.b64e("innocent"))
    assert any("same author name" in e for e in R.record_errors(bad))


# -- H1: inconclusive caps -------------------------------------------------------------


def _scanned(n: int) -> list[dict[str, Any]]:
    return [arch(f"user{i}", status="scanned") for i in range(n)]


def test_inconclusive_share_above_20_percent_fails() -> None:
    rows = _scanned(10)
    probe = honest(*rows[:7])  # Three have no history: Check A fails or is inconclusive.
    for row in rows[7:]:
        probe.activity[R.b64d(row["id_b64"])] = [NOW - timedelta(minutes=10)]  # Unrelated.
    # Make those three inconclusive (the source answers for part of the window only).
    partial = {R.b64d(r["id_b64"]) for r in rows[7:]}

    class Partial(Probe):
        def newest(self, record: dict[str, Any], since: datetime, until: datetime) -> Any:
            answer = super().newest(record, since, until)
            if R.b64d(record["id_b64"]) in partial:
                return NewestActivity(None, until)
            return answer

    flaky = Partial(probe.activity)
    report = V.run_verify({}, keyed(*rows), flaky, seed="s", now=NOW, lag=LAG)
    assert report.count(V.INCONCLUSIVE) == 3 and report.count(V.FAILED) == 0
    assert not report.ok and any("20%" in f for f in report.failures)
    partial = {R.b64d(rows[9]["id_b64"])}
    flaky = Partial({**probe.activity, **honest(*rows[7:9]).activity})
    report = V.run_verify({}, keyed(*rows[:5]), flaky, seed="s", now=NOW, lag=LAG)
    assert report.ok, report.failures


def test_any_inconclusive_candidate_or_transition_fails() -> None:
    rows = [*_scanned(9), arch("cand")]
    probe = honest(*rows[:9])
    # "cand": its last post is gone, earlier ones exist (L2): inconclusive.
    probe.activity["cand"] = [T - timedelta(days=1)]
    report = V.run_verify({}, keyed(*rows), probe, seed="s", now=NOW, lag=LAG)
    assert report.count(V.INCONCLUSIVE) == 1
    assert not report.ok
    assert any("status change or a status other than scanned" in f for f in report.failures)


# -- H2: tolerance only around the claimed activity -----------------------------------


@pytest.mark.parametrize(
    "after", [timedelta(seconds=2), timedelta(minutes=2), timedelta(minutes=50)]
)
def test_activity_just_after_the_last_activity_fails_check_b(after: timedelta) -> None:
    probe = Probe({"did:plc:abc": [T, T + after]})
    out = V.check_record(rec(), probe, now=NOW, lag=LAG)
    assert out.checks["B"] == V.FAILED, out.reasons


def test_check_a_matches_within_five_minutes_only() -> None:
    close = Probe({"did:plc:abc": [T - timedelta(minutes=4)]})
    assert V.check_record(rec(), close, now=NOW, lag=LAG).checks["A"] == V.VERIFIED
    far = Probe({"did:plc:abc": [T - timedelta(minutes=30)]})
    out = V.check_record(rec(), far, now=NOW, lag=LAG)
    assert out.checks["A"] == V.INCONCLUSIVE  # L2: earlier activity exists.


# -- H3: what is checked ----------------------------------------------------------------


def test_small_changes_are_checked_in_full() -> None:
    rows = _scanned(15)
    report = V.run_verify({}, keyed(*rows), honest(*rows), seed="s", now=NOW, lag=LAG)
    assert report.sampled == 15 and report.ok


def test_large_changes_need_the_label_and_always_check_status_changes() -> None:
    rows = [*_scanned(30), *[arch(f"cand{i}") for i in range(4)]]
    probe = honest(*rows)
    report = V.run_verify({}, keyed(*rows), probe, seed="s", now=NOW, lag=LAG)
    assert not report.ok and any("more than 15" in f for f in report.failures)
    labelled = V.run_verify(
        {}, keyed(*rows), honest(*rows), seed="s", now=NOW, lag=LAG, labels=[V.OVERRIDE_LABEL]
    )
    assert labelled.ok, labelled.failures
    checked = {o.id_b64 for o in labelled.outcomes}
    assert {r["id_b64"] for r in rows[30:]} <= checked
    assert len(checked) == 4 + 11  # The candidates plus a sample of the scanned ones.


def test_the_ci_seed_depends_on_every_part() -> None:
    seed = V.gate_seed("abc", "def", 22)
    assert seed == V.gate_seed("abc", "def", 22)
    others = {
        V.gate_seed("abd", "def", 22),
        V.gate_seed("abc", "deg", 22),
        V.gate_seed("abc", "def", 23),
    }
    assert seed not in others and len(others) == 3


def test_r3_a_rerun_draws_the_same_sample() -> None:
    import inspect

    assert list(inspect.signature(V.gate_seed).parameters) == ["base_sha", "head_sha", "pr_number"]
    assert "run_id" not in inspect.signature(run_gate).parameters
    rows = [arch(f"user{i:02d}", status="scanned") for i in range(40)]
    draws = {
        tuple(r["id_b64"] for r in V.sample_records(rows, 11, V.gate_seed("b", "h", 7)))
        for _ in range(5)
    }
    assert len(draws) == 1


# -- H4: transitions --------------------------------------------------------------------


@pytest.mark.parametrize("human", ["verified_inauthentic", "cleared"])
def test_downgrading_a_human_status_needs_the_label(human: str) -> None:
    evidence = R.evidence_path("bluesky", rec()["id_b64"]) if human != "cleared" else None
    before = rec(status=human, evidence=evidence)
    after = rec(status="candidate")
    report = V.run_verify(keyed(before), keyed(after), honest(after), seed="s", now=NOW, lag=LAG)
    assert not report.ok and any("automatic status" in f for f in report.failures)
    text = V.render_summary(report)
    assert f"`{human}` | `candidate`" in text


def test_human_decisions_between_human_statuses_need_the_owner() -> None:
    evidence = R.evidence_path("bluesky", rec()["id_b64"])
    before = rec(status="cleared")
    after = rec(status="verified_inauthentic", evidence=evidence)
    report = V.run_verify(keyed(before), keyed(after), honest(after), seed="s", now=NOW, lag=LAG)
    assert not report.ok
    assert any("move into verified_inauthentic or cleared" in f for f in report.failures)
    assert any("existing verified_inauthentic or cleared" in f for f in report.failures)
    report = V.run_verify(
        keyed(before), keyed(after), honest(after), seed="s", now=NOW, lag=LAG,
        labels=[V.OVERRIDE_LABEL],
    )  # fmt: skip
    assert report.ok, report.failures
    assert [(t.before, t.after) for t in report.plan.transitions] == [
        ("cleared", "verified_inauthentic")
    ]


def test_verifying_needs_the_minimum_silence() -> None:
    evidence = R.evidence_path("bluesky", rec()["id_b64"])
    before = rec(status="scanned", cliff=False)
    # Valid as a record (a human status is exempt), but not as a new verification.
    after = rec(
        status="verified_inauthentic",
        evidence=evidence,
        cliff=False,
        last_activity_at=isoformat(T + timedelta(days=29)),
    )
    assert R.record_errors(after) == []
    report = V.run_verify(
        keyed(before), keyed(after), honest(after), seed="s", now=NOW, labels=[V.OVERRIDE_LABEL]
    )
    assert not report.ok and any("becomes verified_inauthentic" in f for f in report.failures)


# -- H5: identity binding ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "user_id", "username", "ok"),
    [
        ("reddit-archive", "Alice", "Alice", True),
        ("reddit-archive", "Alice", "alice", False),
        ("reddit", "alice", "Alice", True),
        ("reddit", "bob", "Alice", False),
        ("bluesky", "did:plc:abc", "anything.bsky.social", True),
        ("bluesky", "alice.bsky.social", "alice.bsky.social", False),
        ("x", "12345", "someone", True),
        ("x", "someone", "someone", False),
    ],
)
def test_ids_are_bound_to_names(platform: str, user_id: str, username: str, ok: bool) -> None:
    record = rec(platform=platform, id_b64=R.b64e(user_id), username_b64=R.b64e(username))
    assert (R.record_errors(record) == []) is ok


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("alice", True),
        ("a-b_c", True),
        ("a" * 20, True),
        ("europeanunion-ModTeam", True),  # 21 characters: a subreddit's system account.
        ("abc-ModTeam", True),
        ("a_b" * 7 + "-ModTeam", True),  # A 21-character subreddit name, the longest.
        ("a" * 22, False),
        ("a" * 21, False),
        ("a" * 22 + "-ModTeam", False),  # A subreddit name has at most 21 characters.
        ("ab-ModTeam", True),  # Short enough to be an ordinary username.
        ("europeanunion-modteam", False),  # The suffix is case-sensitive.
        ("europeanunion-MODTEAM", False),
        ("europeanunion-ModTeam2", False),
        ("a-b-c-d-e-f-g-h-ModTeam", False),  # A subreddit name has no hyphen.
        ("europeanunion_ModTeam", False),
        ("-ModTeam", True),  # Eight characters of the ordinary username alphabet.
        ("", False),
        ("ab", False),
        ("bad name", False),
        ("bad.name", False),
        ("bad/name", False),
        ("bad$name-ModTeam", False),
    ],
)
@pytest.mark.parametrize("platform", ["reddit", "reddit-archive"])
def test_reddit_usernames_allow_modteam_system_accounts(platform: str, name: str, ok: bool) -> None:
    assert R.valid_username(platform, name) is ok


@pytest.mark.parametrize("name", ["Kabc", "abc\u017f", "\u0130bc", "\u212abc", "abc-ModTeam", "ab"])
def test_reddit_user_ids_are_lower_case_ascii_only(name: str) -> None:
    # An ignore-case match would let the Kelvin sign, the long s and the dotted capital I
    # through, because they fold to ASCII letters.
    assert not R.valid_user_id("reddit", name)


def test_reddit_user_ids_accept_a_lower_case_modteam_account() -> None:
    assert R.valid_user_id("reddit", "europeanunion-modteam")
    assert R.valid_user_id("reddit", "abc_def-1")


def test_a_modteam_account_makes_a_valid_record_on_both_reddit_platforms() -> None:
    name = "europeanunion-ModTeam"
    archive = arch(name)
    assert R.record_errors(archive) == []
    reddit = rec(platform="reddit", id_b64=R.b64e(name.lower()), username_b64=R.b64e(name))
    assert R.record_errors(reddit) == []
    assert R.valid_user_id("reddit", name.lower())
    assert "u/europeanunion-ModTeam" in render_reddit(reddit, None)
    bad = rec(platform="reddit", id_b64=R.b64e("a" * 22), username_b64=R.b64e("a" * 22))
    assert R.record_errors(bad) != []


def test_drafts_name_the_account_by_its_id() -> None:
    reddit = render_reddit(
        rec(platform="reddit", id_b64=R.b64e("alice"), username_b64=R.b64e("Alice")), None
    )
    assert "u/Alice" in reddit
    bluesky = render_bluesky(rec(username_b64=R.b64e("x.bsky.social")), None)
    assert bluesky.startswith("# Report draft: Bluesky ` did:plc:abc `")
    assert "handles can change" in bluesky and "https://bsky.app/profile/did:plc:abc" in bluesky


def test_the_probe_queries_by_id() -> None:
    probe = honest(rec())
    V.check_record(rec(), probe, now=NOW, lag=LAG)
    assert set(probe.ids) == {"did:plc:abc"}


# -- M1, M2 ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["first_scanned_at", "last_scanned_at", "status_updated_at"])
def test_timestamps_more_than_an_hour_ahead_are_errors(name: str) -> None:
    later = NOW + timedelta(hours=2)
    fields = {name: isoformat(later)}
    if name != "status_updated_at":
        fields = {"first_scanned_at": isoformat(later), "last_scanned_at": isoformat(later)}
        fields["scan_window_end"] = isoformat(later - timedelta(days=1))
    assert any("in the future" in e for e in R.record_errors(rec(**fields)))
    soon = rec(status_updated_at=isoformat(NOW + timedelta(minutes=30)))
    assert R.record_errors(soon) == []


def test_fake_platform_is_not_in_the_schema() -> None:
    assert "fake" not in R.SCHEMA["properties"]["platform"]["enum"]
    assert any("platform" in e for e in R.record_errors(rec(platform="fake")))
    schema = json.loads((REPO / "registry" / "schema.json").read_text(encoding="utf-8"))
    assert "fake" not in schema["properties"]["platform"]["enum"]


@pytest.mark.parametrize("platform", ["x", "reddit"])
def test_statuses_on_uncheckable_platforms_need_the_label(platform: str) -> None:
    user = "12345" if platform == "x" else "alice"
    row = rec(platform=platform, id_b64=R.b64e(user), username_b64=R.b64e(user))
    report = V.run_verify({}, keyed(row), Probe(), seed="s", now=NOW)
    assert not report.ok and any("cannot query" in f for f in report.failures)
    labelled = V.run_verify({}, keyed(row), Probe(), seed="s", now=NOW, labels=[V.OVERRIDE_LABEL])
    assert labelled.ok and labelled.count(V.UNVERIFIED) == 1
    # Even a plain scanned record: CI cannot confirm it against a live source.
    scanned = {**row, "status": "scanned"}
    plain = V.run_verify({}, keyed(scanned), Probe(), seed="s", now=NOW)
    assert not plain.ok and any("cannot query" in f for f in plain.failures)
    assert V.run_verify(
        {}, keyed(scanned), Probe(), seed="s", now=NOW, labels=[V.OVERRIDE_LABEL]
    ).ok


def test_merge_drops_a_cliff_left_without_enough_silence() -> None:
    newer = arch(
        "uuu",
        last_scanned_at=isoformat(T + timedelta(days=31)),
        scan_window_end=isoformat(T + timedelta(days=31)),
    )
    older = arch(
        "uuu",
        status="scanned",
        cliff=False,
        last_activity_at=isoformat(T + timedelta(days=29)),
        status_updated_at=isoformat(T + timedelta(days=30)),
    )
    merged = R.merge_records(newer, older)
    assert merged["cliff"] is False and merged["status"] == "scanned"
    assert R.record_errors(merged) == []


# -- M3, M8 ----------------------------------------------------------------------------


def _drafts_registry(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    R.write_records(root, [arch("some_author")])
    return root


def test_drafts_are_for_verified_records_unless_asked(tmp_path: Path) -> None:
    root = _drafts_registry(tmp_path)
    out = tmp_path / "drafts"
    with pytest.raises(ValueError, match="include-unverified"):
        build_drafts(root, "candidate", "reddit-archive", out, event_id=EVENT.id, events=EVENTS)
    args = ["report", "--registry", str(root), "--platform", "reddit-archive", "--out", str(out),
            "--status", "candidate"]  # fmt: skip
    refused = runner.invoke(app, args)
    assert refused.exit_code == 2 and "--include-unverified" in refused.output
    result = runner.invoke(app, [*args, "--include-unverified"])
    assert result.exit_code == 0, result.output
    assert "NOT verified" in result.output
    (draft,) = out.rglob("*.md")
    text = draft.read_text(encoding="utf-8")
    assert text.splitlines()[0].endswith("(UNVERIFIED — do not submit)")
    assert "**UNVERIFIED — do not submit.**" in text


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


def test_private_outputs_are_refused_inside_a_git_repository(tmp_path: Path) -> None:
    repo = tmp_path / "work"
    _init(repo)
    root = tmp_path / "registry"
    evidence = R.evidence_path("reddit-archive", R.b64e("some_author"))
    R.write_records(root, [arch("some_author", status="verified_inauthentic", evidence=evidence)])
    (root / evidence).parent.mkdir(parents=True)
    (root / evidence).write_text("# Evidence\n")
    args = ["report", "--registry", str(root), "--platform", "reddit-archive",
            "--out", str(repo / "drafts" / "new")]  # fmt: skip
    refused = runner.invoke(app, args)
    assert refused.exit_code == 2 and "inside the git repository" in refused.output
    assert not (repo / "drafts").exists()
    allowed = runner.invoke(app, [*args, "--allow-inside-repo"])
    assert allowed.exit_code == 0, allowed.output
    export = runner.invoke(
        app, ["export-ids", "--run-id", "1", "--db", str(tmp_path / "none.db"),
              "--out", str(repo / "ids.csv")],
    )  # fmt: skip
    assert export.exit_code == 2 and "inside the git repository" in export.output


def test_gitignore_covers_the_default_private_outputs() -> None:
    text = (REPO / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("report-drafts/", "*-ids.csv", "*-ids.json"):
        assert pattern in text


def test_add_refuses_a_fake_run(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    out = str(tmp_path / "o.csv")
    scan = ["scan", "--platform", "fake", "--n", "30", "--seed", "1",
            "--now", "2026-10-11T00:00:00Z", "--db", str(db), "--out", out]  # fmt: skip
    assert runner.invoke(app, scan).exit_code == 0
    result = runner.invoke(
        app,
        ["registry", "add", "--run-id", "1", "--db", str(db), "--registry", str(tmp_path / "r")],
    )
    assert result.exit_code == 2 and "fake" in result.output


# -- the gate ----------------------------------------------------------------------------


class GateRepo:
    """A git repository with a base commit on main and a pull request branch."""

    def __init__(self, path: Path, base_records: list[dict[str, Any]]) -> None:
        self.path = path
        _init(path)
        (path / "README.md").write_text("project\n")
        (path / "events.toml").write_bytes(FIXTURE_EVENTS.read_bytes())
        if base_records:
            R.write_records(path / "registry", base_records)
        else:
            (path / "registry").mkdir()
            (path / "registry" / "schema.json").write_text(R.schema_text())
        _git(path, "add", "-A")
        _git(path, "commit", "-qm", "base")
        self.base = _git(path, "rev-parse", "HEAD")
        _git(path, "checkout", "-q", "-b", "pr")

    def commit(self) -> str:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-qm", "change", "--allow-empty")
        return _git(self.path, "rev-parse", "HEAD")

    def gate(self, probe: Probe | None = None, labels: tuple[str, ...] = ()) -> Any:
        head = self.commit()
        pr = PullRequest(7, self.base, head, frozenset(labels), labels_trusted=True)
        return run_gate(self.path, pr, probe or Probe(), now=NOW, lag=LAG)


def test_gate_passes_code_only_changes(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    (repo.path / "README.md").write_text("changed\n")
    result = repo.gate()
    assert result.ok and result.report is None


def test_gate_fails_mixed_changes(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    (repo.path / "README.md").write_text("changed\n")
    R.write_records(repo.path / "registry", [arch("someone", status="scanned")])
    result = repo.gate()
    assert not result.ok and "separate pull requests" in result.failures[0]


def test_gate_allows_the_schema_alone_with_code(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    (repo.path / "README.md").write_text("changed\n")
    (repo.path / "registry" / "schema.json").write_text(R.schema_text() + " ")
    result = repo.gate()
    assert result.ok and "schema_text" in result.notes[0]


def test_gate_passes_an_honest_data_only_change(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [arch("old", status="scanned")])
    new = arch("someone")
    R.write_records(repo.path / "registry", [arch("old", status="scanned"), new])
    result = repo.gate(honest(new))
    assert result.ok, render_gate(result)
    assert "| reddit-archive |" in render_gate(result)  # The transition table.


@pytest.mark.parametrize(
    "case", ["deletion", "downgrade", "future", "mismatch", "executable", "symlink", "stray"]
)
def test_gate_fails_tampering(tmp_path: Path, case: str) -> None:
    evidence = R.evidence_path("reddit-archive", R.b64e("old"))
    verified = arch("old", status="verified_inauthentic", evidence=evidence)
    repo = GateRepo(tmp_path / "r", [verified, arch("other", status="scanned")])
    (repo.path / "registry" / EVENT_DIR / evidence).parent.mkdir(parents=True)
    (repo.path / "registry" / EVENT_DIR / evidence).write_text("# Evidence\n")
    _git(repo.path, "add", "-A")
    _git(repo.path, "commit", "-qm", "evidence")
    repo.base = _git(repo.path, "rev-parse", "HEAD")
    reg = repo.path / "registry"
    rows = [verified, arch("other", status="scanned")]
    if case == "deletion":
        R.write_records(reg, rows[:1])
    elif case == "downgrade":
        R.write_records(reg, [{**verified, "status": "scanned", "evidence": None}, rows[1]])
    elif case == "future":
        R.write_records(
            reg,
            [*rows, arch("new", status="scanned", status_updated_at="2027-01-01T00:00:00+00:00")],
        )
    elif case == "mismatch":
        R.write_records(reg, [*rows, arch("bot", username_b64=R.b64e("innocent"))])
    elif case == "executable":
        shard = reg / R.record_path(EVENT.id, "reddit-archive", R.b64e("other"))
        os.chmod(shard, 0o755)
        _git(repo.path, "update-index", "--chmod=+x", str(shard))
    elif case == "symlink":
        (reg / EVENT_DIR / "evidence" / "reddit-archive" / f"{R.b64e('x')}.md").symlink_to(
            "/etc/passwd"
        )
    elif case == "stray":
        (reg / "notes.txt").write_text("hi\n")
    result = repo.gate(honest(*rows))
    assert not result.ok, render_gate(result)
    labelled = repo.gate(honest(*rows), labels=(V.OVERRIDE_LABEL,))
    assert labelled.ok is (case in ("deletion", "downgrade"))


def test_gate_accepts_a_modteam_account_record(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    row = arch("europeanunion-ModTeam", status="scanned")
    R.write_records(repo.path / "registry", [row])
    repo.commit()
    result = repo.gate(honest(row))
    assert result.ok, render_gate(result)


def test_gate_reads_the_event_payload_and_never_checks_out_the_head(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    R.write_records(repo.path / "registry", [arch("someone", status="scanned")])
    head = repo.commit()
    _git(repo.path, "checkout", "-q", "main")
    before = sorted(
        p.relative_to(repo.path / "registry").as_posix()
        for p in (repo.path / "registry").rglob("*")
    )
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "number": 7, "base": {"sha": repo.base}, "head": {"sha": head},
        "labels": [{"name": V.OVERRIDE_LABEL}, {"name": "other"}],
    }}))  # fmt: skip
    pr = PullRequest.from_event(event)
    assert pr.labels == {V.OVERRIDE_LABEL, "other"} and pr.head_sha == head
    result = run_gate(repo.path, pr, honest(arch("someone")), now=NOW)
    assert result.ok, render_gate(result)
    after = sorted(
        p.relative_to(repo.path / "registry").as_posix()
        for p in (repo.path / "registry").rglob("*")
    )
    assert before == after == ["schema.json"]


def test_cli_gate_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = GateRepo(tmp_path / "r", [arch("old", status="scanned")])
    R.write_records(repo.path / "registry", [])
    head = repo.commit()
    monkeypatch.setattr(V, "SourceProbe", lambda **kwargs: Probe())
    summary = tmp_path / "summary.md"
    args = ["registry", "gate", "--repo", str(repo.path), "--base-sha", repo.base,
            "--head-sha", head, "--summary", str(summary)]  # fmt: skip
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "removed" in summary.read_text(encoding="utf-8")
    assert runner.invoke(app, [*args, "--label", V.OVERRIDE_LABEL]).exit_code == 0
    validate = runner.invoke(app, ["registry", "validate", "--repo", str(repo.path), "--ref", head])
    assert validate.exit_code == 0, validate.output


def test_the_workflows_follow_the_integrity_design() -> None:
    gate = (REPO / ".github" / "workflows" / "registry-gate.yml").read_text(encoding="utf-8")
    assert "pull_request_target:" in gate and "paths" not in gate
    assert "persist-credentials: false" in gate and "enable-cache: false" in gate
    assert "contents: read" in gate and "secrets." not in gate
    assert "ref: ${{ github.event.pull_request.base.sha }}" in gate
    assert "uv sync --locked" in gate and "registry gate" in gate
    assert "head.ref" not in gate and "github.head_ref" not in gate
    assert not (REPO / ".github" / "workflows" / "registry-verify.yml").exists()
    template = (REPO / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    assert "createdAt" in template and "baseline_posts" in template


# -- round-2 critic cases ---------------------------------------------------------------

DID = "did:plc:abc"
EVIDENCE = R.evidence_path("bluesky", R.b64e(DID))
LATER = isoformat(T + timedelta(days=31))
VERIFIED = rec(
    status="verified_inauthentic", evidence=EVIDENCE, status_updated_at=LATER, last_scanned_at=LATER
)
EVIDENCE_TEXT = (
    '# Evidence\n\n```json\n{"links": ["https://bsky.app/profile/{id}"], '
    '"communities": {"worldnews": 3}}\n```\n\n'
    "- [x] The behavior is inauthentic, not a person who stopped posting.\n"
    "- Live profile checked: active\n"
    "- Content evidence: the same three posts as forty other accounts, hours apart\n"
)


def _write_evidence(repo: GateRepo, text: str = EVIDENCE_TEXT) -> None:
    path = repo.path / "registry" / EVENT_DIR / EVIDENCE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _verified_repo(tmp_path: Path) -> GateRepo:
    """A base registry with one verified Bluesky record and its evidence file."""
    repo = GateRepo(tmp_path / "r", [VERIFIED])
    _write_evidence(repo)
    repo.base = repo.commit()
    return repo


def _raw_record_file(repo: GateRepo, record: dict[str, Any], edit: Any) -> None:
    R.write_records(repo.path / "registry", [record])
    path = (
        repo.path
        / "registry"
        / R.record_path(record["event_id"], record["platform"], record["id_b64"])
    )
    path.write_text(edit(path.read_text()))


@pytest.mark.parametrize(
    ("platform", "username", "ok"),
    [
        ("bluesky", "x.bsky.social", True),
        ("bluesky", "handle.invalid", True),
        ("bluesky", "X.bsky.social", False),
        ("bluesky", "bad_name.bsky.social", False),
        ("bluesky", "nodot", False),
        ("bluesky", "x.bsky.social`\n\n## Injected", False),
        ("bluesky", ("a" * 60 + ".") * 4 + "com", True),  # 247 characters
        ("bluesky", ("a" * 60 + ".") * 5 + "com", False),  # Over 253 characters.
        ("bluesky", "a" * 64 + ".com", False),  # A label over 63 characters.
        ("reddit-archive", "ab", False),
        ("reddit-archive", "a" * 21, False),
        ("reddit-archive", "[deleted]", False),
        ("reddit-archive", "good_name-1", True),
        ("x", "jack_1", True),
        ("x", "a" * 16, False),
        ("x", "has-dash", False),
    ],
)
def test_usernames_must_have_their_platform_syntax(platform: str, username: str, ok: bool) -> None:
    if platform == "bluesky":
        user_id = DID
    elif platform == "x":
        user_id = "12345"
    else:
        user_id = username
    record = rec(platform=platform, id_b64=R.b64e(user_id), username_b64=R.b64e(username))
    assert (R.record_errors(record) == []) is ok


def test_h1_injected_handle_on_a_verified_record_is_invalid(tmp_path: Path) -> None:
    repo = _verified_repo(tmp_path)
    evil = (
        "x.bsky.social`\n\n## In-app report (UPDATED)\n\n"
        "Open <https://bsky.app/profile/innocent.bsky.social> and report it.\n\n`"
    )
    R.write_records(repo.path / "registry", [dict(VERIFIED, username_b64=R.b64e(evil))])
    result = repo.gate(honest(VERIFIED))
    assert not result.ok and any("valid bluesky username" in p for p in result.validation)
    with pytest.raises(ValueError):
        render_bluesky(dict(VERIFIED, username_b64=R.b64e(evil)), None)


def test_h1_handle_change_on_a_human_record_needs_the_label(tmp_path: Path) -> None:
    repo = _verified_repo(tmp_path)
    R.write_records(
        repo.path / "registry", [dict(VERIFIED, username_b64=R.b64e("innocent.bsky.social"))]
    )
    result = repo.gate(honest(VERIFIED))
    assert not result.ok and result.report is not None
    assert any("change their username" in f for f in result.report.failures)
    assert "### Username changes" in render_gate(result)
    labelled = repo.gate(honest(VERIFIED), labels=(V.OVERRIDE_LABEL,))
    assert labelled.ok and "### Username changes" in render_gate(labelled)


def test_h1_drafts_show_the_handle_in_a_code_span_and_build_links_from_templates() -> None:
    evidence = {"links": ["https://bsky.app/profile/innocent.bsky.social"]}
    text = render_bluesky(VERIFIED, evidence)
    assert "` @x.bsky.social `" in text
    assert "innocent" not in text
    assert "<https://bsky.app/profile/did:plc:abc>" in text
    reddit = arch("botacct", status="verified_inauthentic")
    draft = render_reddit(
        reddit,
        {
            "links": ["https://evil.example/{username}"],
            "communities": {"innocent_sub": 9},
            "signals": [{"name": "x](https://evil.example)", "value": 1, "threshold": "t"}],
            "cluster": {"size": "4)[x](https://evil.example", "sync_minutes": 3},
        },
    )
    assert "evil.example" not in draft and "innocent_sub" not in draft
    assert "<https://www.reddit.com/user/botacct/>" in draft


def test_m1_evidence_only_change_of_a_verified_record_needs_the_label(tmp_path: Path) -> None:
    repo = _verified_repo(tmp_path)
    _write_evidence(repo, EVIDENCE_TEXT.replace("{id}", "innocent.bsky.social"))
    result = repo.gate(Probe())
    assert not result.ok and result.report is not None
    assert any("evidence files are added, changed or removed" in f for f in result.report.failures)
    summary = render_gate(result)
    assert "### Evidence file changes" in summary and EVIDENCE in summary
    assert repo.gate(Probe(), labels=(V.OVERRIDE_LABEL,)).ok


def test_m1_evidence_removal_of_a_cleared_record_needs_the_label(tmp_path: Path) -> None:
    cleared = dict(VERIFIED, status="cleared")
    repo = GateRepo(tmp_path / "r", [cleared])
    _write_evidence(repo)
    repo.base = repo.commit()
    R.write_records(repo.path / "registry", [dict(cleared, evidence=None)])
    (repo.path / "registry" / EVENT_DIR / EVIDENCE).unlink()
    result = repo.gate(honest(cleared))
    assert not result.ok and EVIDENCE in render_gate(result)


def test_m1_evidence_with_a_transition_into_verified_needs_the_owner(tmp_path: Path) -> None:
    candidate = rec()
    repo = GateRepo(tmp_path / "r", [candidate])
    _write_evidence(repo)
    R.write_records(repo.path / "registry", [VERIFIED])
    result = repo.gate(honest(VERIFIED))
    assert not result.ok and result.report is not None
    failures = result.report.failures
    assert any("move into verified_inauthentic or cleared" in f for f in failures)
    assert any("evidence files are added, changed or removed" in f for f in failures)
    assert "with its record's transition into verified_inauthentic" in render_gate(result)
    allowed = repo.gate(honest(VERIFIED), labels=(V.OVERRIDE_LABEL,))
    assert allowed.ok, render_gate(allowed)
    # A new record added straight as verified_inauthentic, with its evidence: the old
    # rule let up to 15 of these through without a label.
    fresh = GateRepo(tmp_path / "f", [])
    _write_evidence(fresh)
    R.write_records(fresh.path / "registry", [VERIFIED])
    assert not fresh.gate(honest(VERIFIED)).ok
    assert fresh.gate(honest(VERIFIED), labels=(V.OVERRIDE_LABEL,)).ok


def test_a_verdict_needs_evidence_beyond_the_silence(tmp_path: Path) -> None:
    candidate = rec()
    repo = GateRepo(tmp_path / "r", [candidate])
    _write_evidence(repo, EVIDENCE_TEXT.split("\n\n- [x]")[0] + "\n")
    R.write_records(repo.path / "registry", [VERIFIED])
    result = repo.gate(honest(VERIFIED), labels=(V.OVERRIDE_LABEL,))
    summary = render_gate(result)
    assert not result.ok
    assert 'evidence review: the box "The behavior is inauthentic" is not ticked' in summary
    assert "no evidence beyond the silence" in summary
    assert "a verifier must check the live profile" in summary


def test_m4_clearing_needs_the_owner_and_is_listed_first(tmp_path: Path) -> None:
    repo = _verified_repo(tmp_path)
    cleared = dict(VERIFIED, status="cleared", status_updated_at=isoformat(NOW))
    R.write_records(repo.path / "registry", [cleared])
    refused = repo.gate(honest(VERIFIED))
    assert not refused.ok and refused.report is not None
    assert any("move into verified_inauthentic or cleared" in f for f in refused.report.failures)
    result = repo.gate(honest(VERIFIED), labels=(V.OVERRIDE_LABEL,))
    assert result.ok, render_gate(result)
    summary = render_gate(result)
    assert "High-impact transitions" in summary and "confirm the reason" in summary
    assert summary.index("High-impact") < summary.index("Status transitions")


def test_m3_the_label_is_ignored_after_a_new_push(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [arch("old", status="scanned")])
    R.write_records(repo.path / "registry", [])
    head = repo.commit()
    pushed = PullRequest(7, repo.base, head, frozenset({V.OVERRIDE_LABEL}), "synchronize")
    result = run_gate(repo.path, pushed, Probe(), now=NOW)
    assert not result.ok and "ignored after a new push" in render_gate(result)
    labelled = PullRequest(
        7, repo.base, head, frozenset({V.OVERRIDE_LABEL}), "labeled", V.OVERRIDE_LABEL,
        author="author", head_reviewed=True,
    )  # fmt: skip
    assert run_gate(repo.path, labelled, Probe(), now=NOW).ok
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "synchronize", "pull_request": {
        "number": 7, "base": {"sha": repo.base}, "head": {"sha": head},
        "labels": [{"name": V.OVERRIDE_LABEL}]}}))  # fmt: skip
    assert PullRequest.from_event(event).effective_labels == frozenset()


@pytest.mark.parametrize(
    "edit",
    [
        lambda s: s.replace('"tool_version":"1"', '"tool_version":"1\\n"'),
        lambda s: s.replace(
            '"status_updated_at":"2026-10-01T00:00:00+00:00"',
            '"status_updated_at":"2026-10-01T00:00:00+00:00\\n"',
        ),
    ],
)
def test_l1_patterns_match_the_whole_value(tmp_path: Path, edit: Any) -> None:
    repo = GateRepo(tmp_path / "r", [])
    record = rec(status="scanned", cliff=False)
    _raw_record_file(repo, record, edit)
    result = repo.gate(honest(record))
    assert not result.ok and any("does not match" in p for p in result.validation)


def test_l2_an_impossible_date_fails_without_a_crash(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    record = rec(status="scanned", cliff=False)
    _raw_record_file(
        repo, record, lambda s: s.replace(record["status_updated_at"], "2026-13-45T00:00:00+00:00")
    )
    result = repo.gate(honest(record))
    assert not result.ok and any("not a valid date" in p for p in result.validation)


@pytest.mark.parametrize("depth", [3000, 200_000])
def test_l2_deep_nesting_fails_without_a_crash(tmp_path: Path, depth: int) -> None:
    repo = GateRepo(tmp_path / "r", [])
    path = repo.path / "registry" / EVENT_DIR / "bluesky" / "aa.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("[" * depth + "]" * depth + "\n")
    result = repo.gate()
    assert not result.ok and result.validation
    records, problems = V.load_tree(R.RegistryTree(repo.path / "registry"))
    assert records == {} and problems


def test_l2_size_limits_apply_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GateRepo(tmp_path / "r", [])
    record = rec(status="scanned", cliff=False)
    _raw_record_file(repo, record, lambda s: s.rstrip("\n") + " " * 9000 + "\n")
    result = repo.gate(honest(record))
    assert any("line longer than 8192 bytes" in p for p in result.validation)
    big = GateRepo(tmp_path / "b", [])
    _write_evidence(big, "x" * (R.MAX_EVIDENCE_FILE_BYTES + 1))
    R.write_records(big.path / "registry", [VERIFIED])
    assert any("larger than 200000 bytes" in p for p in big.gate(honest(VERIFIED)).validation)
    monkeypatch.setattr(R, "MAX_RECORD_FILE_BYTES", 100)
    many = GateRepo(tmp_path / "m", [])
    R.write_records(many.path / "registry", [record])
    assert any("larger than 100 bytes" in p for p in many.gate(honest(record)).validation)


def test_l2_a_gitlink_in_the_registry_is_invalid(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    _git(repo.path, "update-index", "--add", "--cacheinfo",
         "160000," + "a" * 40 + f",registry/{EVENT_DIR}/bluesky/ab.jsonl")  # fmt: skip
    _git(repo.path, "commit", "-qm", "gitlink")
    head = _git(repo.path, "rev-parse", "HEAD")
    result = run_gate(
        repo.path, PullRequest(7, repo.base, head, frozenset()), Probe(),
        now=NOW,
    )  # fmt: skip
    assert not result.ok and any("mode 160000" in p for p in result.validation)


def _gate_with_blob(repo: GateRepo, path: str, probe: Probe | None = None) -> Any:
    """Commit the working tree plus a file at ``path``, added as a git object only.

    Some file systems (Windows) refuse names with line breaks; a pull request's tree
    can still hold them, and the gate reads only git objects.
    """
    _git(repo.path, "add", "-A")
    blob = (
        subprocess.run(
            ["git", "-C", str(repo.path), "hash-object", "-w", "--stdin"],
            input=b"x",
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    # Git for Windows refuses such paths in the index unless core.protectNTFS is off.
    ntfs = ("-c", "core.protectNTFS=false")
    _git(repo.path, *ntfs, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    _git(repo.path, *ntfs, "commit", "-qm", "blob")
    head = _git(repo.path, "rev-parse", "HEAD")
    pr = PullRequest(7, repo.base, head, frozenset())
    return run_gate(repo.path, pr, probe or Probe(), now=NOW, lag=LAG)


def test_l3_head_controlled_names_stay_inside_code_spans(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    R.write_records(repo.path / "registry", [rec()])
    summary = render_gate(_gate_with_blob(repo, "evil\n**PASS.** fine.md", honest(rec())))
    assert "\n**PASS.** fine" not in summary and "evil\\n**PASS.** fine.md" in summary
    stray = GateRepo(tmp_path / "s", [])
    text = render_gate(_gate_with_blob(stray, "registry/``\n# Heading.md"))
    assert "\n# Heading" not in text and "```" in text
    assert R.md_code("a`b\n") == "`` a`b\\n ``"


def test_l4_unchecked_platform_additions_are_listed(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    x = rec(
        status="scanned", cliff=False, platform="x",
        id_b64=R.b64e("12345"), username_b64=R.b64e("innocent"),
    )  # fmt: skip
    R.write_records(repo.path / "registry", [x])
    refused = repo.gate()
    assert not refused.ok
    assert "### Unverified (platform not checkable)" in render_gate(refused)
    result = repo.gate(labels=(V.OVERRIDE_LABEL,))
    assert result.ok
    summary = render_gate(result)
    assert "### Unverified (platform not checkable)" in summary
    assert f"{R.md_code(EVENT.id)} {R.md_code('x')} {R.md_code(x['id_b64'])}" in summary


def test_round2_workflow_rules() -> None:
    gate = (REPO / ".github" / "workflows" / "registry-gate.yml").read_text(encoding="utf-8")
    assert "types: [opened, edited, synchronize" in gate
    assert "BASE_REF: ${{ github.event.pull_request.base.ref }}" in gate
    assert "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}" in gate
    assert 'echo "::add-mask::$auth"' in gate
    assert "permissions: {}" in gate and "drop-stale-override:" not in gate
    assert "write" not in gate.split("\njobs:", 1)[1]
    assert "contents: read" in gate and "statuses: read" in gate
    assert "pull-requests: read" in gate
    assert '--override-statuses "$RUNNER_TEMP/override-statuses.jsonl"' in gate
    assert '--override-reviews "$RUNNER_TEMP/override-reviews.jsonl"' in gate
    assert '--reviewer-permissions "$RUNNER_TEMP/reviewer-permissions.jsonl"' in gate
    # A pull_request_review run would use the pull request's own copy of the workflow.
    assert "pull_request_review" not in gate.split("\njobs:", 1)[0].split("\non:", 1)[1]
    # A bounded retry when the status is missing, and set -e so failed reads fail.
    assert "for attempt in 1 2 3; do" in gate and "sleep 10" in gate
    assert "set -euo pipefail" in gate
    assert "GITHUB_RUN_ID" not in gate and "--run-id" not in gate
    override = (REPO / ".github" / "workflows" / "registry-override.yml").read_text(
        encoding="utf-8"
    )
    # Nothing may cancel it: the gate's own runs are cancelled by newer events.
    assert not [line for line in override.splitlines() if line.strip().startswith("concurrency")]
    assert "permissions: {}" in override
    assert "types: [labeled, synchronize]" in override
    record, drop = override.split("  record-override:", 1)[1].split("  drop-stale-override:", 1)
    assert "statuses: write" in record and "pull-requests: write" in record
    assert "pull-requests: write" in drop and "statuses" not in drop
    # record-override writes the status and a comment naming the commit.
    assert record.count("gh api") == 2 and "/comments" in record
    assert drop.count("gh api") == 1
    for job in (record, drop):
        assert "checkout" not in job and "uv" not in job
    # Expressions only in env:, never inside run: scripts.
    for text in (gate, override):
        for block in text.split("run: ")[1:]:
            script = block.split("\n      - ", 1)[0].split("\n  ", 1)[0]
            assert "${{" not in script, script
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "dismisses stale approvals when new commits are pushed" in readme
    ruleset = json.loads((REPO / ".github" / "rulesets" / "main.json").read_text(encoding="utf-8"))
    review = next(r for r in ruleset["rules"] if r["type"] == "pull_request")["parameters"]
    assert review["dismiss_stale_reviews_on_push"] is True
    assert "rerun the `Registry gate`" in readme
    template = (REPO / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    assert "Reviewers must confirm that reason" in template


# -- R3: a stale registry-override label after a new push ----------------------------


def _status(context: str, state: str = "success", creator: str = "github-actions[bot]") -> str:
    return json.dumps({"context": context, "state": state, "creator": creator})


APPROVED = _status("registry-override/pr-7")


def _r3_repo(tmp_path: Path) -> tuple[GateRepo, str, str]:
    """Head A (a harmless commit a maintainer approved) and head B (a removal)."""
    repo = GateRepo(
        tmp_path / "r", [arch("alice", status="scanned"), arch("bobby", status="scanned")]
    )
    head_a = repo.commit()
    R.write_records(repo.path / "registry", [arch("alice", status="scanned")])
    head_b = repo.commit()
    return repo, head_a, head_b


def _review(commit: str, user: str = "maint", state: str = "APPROVED", review_id: int = 1) -> str:
    return json.dumps({"id": review_id, "user": user, "state": state, "commit_id": commit})


def _permission(user: str = "maint", permission: str = "admin") -> str:
    return json.dumps({"user": user, "permission": permission})


#: Reviews: the default approves whatever head the test checks.
HEAD_REVIEW = object()


def _event_gate(
    tmp_path: Path,
    repo: GateRepo,
    head: str,
    action: str,
    event_label: str = "",
    statuses: list[str] | None = None,
    reviews: Any = HEAD_REVIEW,
    permissions: list[str] | None = None,
) -> Any:
    """Run the gate command as the workflow does: event, statuses and review files.

    ``reviews`` defaults to an approving review of ``head`` by a writer; ``None``
    leaves the review files out.
    """
    payload: dict[str, Any] = {"action": action, "pull_request": {
        "number": 7, "base": {"sha": repo.base}, "head": {"sha": head},
        "user": {"login": "author"},
        "labels": [{"name": V.OVERRIDE_LABEL}]}}  # fmt: skip
    if event_label:
        payload["label"] = {"name": event_label}
    event = tmp_path / f"event-{action}.json"
    event.write_text(json.dumps(payload))
    args = ["registry", "gate", "--repo", str(repo.path), "--event", str(event)]
    if statuses is not None:
        listing = tmp_path / "statuses.jsonl"
        listing.write_text("".join(line + "\n" for line in statuses))
        args += ["--override-statuses", str(listing)]
    if reviews is HEAD_REVIEW:
        reviews = [_review(head)]
    if reviews is not None:
        review_file = tmp_path / "reviews.jsonl"
        review_file.write_text("".join(line + "\n" for line in reviews))
        permission_file = tmp_path / "permissions.jsonl"
        lines = [_permission()] if permissions is None else permissions
        permission_file.write_text("".join(line + "\n" for line in lines))
        args += ["--override-reviews", str(review_file)]
        args += ["--reviewer-permissions", str(permission_file)]
    return runner.invoke(app, args)


@pytest.mark.parametrize("action", ["edited", "reopened", "labeled", "unlabeled"])
def test_r3_a_stale_label_fails_on_every_later_event(tmp_path: Path, action: str) -> None:
    repo, head_a, head_b = _r3_repo(tmp_path)
    # The maintainer adds the label to head A, a harmless commit.
    assert _event_gate(tmp_path, repo, head_a, "labeled", V.OVERRIDE_LABEL, []).exit_code == 0
    label = "other" if action in ("labeled", "unlabeled") else ""
    # The status approves head A only; the API lists statuses of head B, which has none.
    for statuses in (None, [], [_status("ci")]):
        result = _event_gate(tmp_path, repo, head_b, action, label, statuses)
        assert result.exit_code == 1, result.output
        assert "FAIL" in result.output and "label is ignored" in result.output
    # The same check with the payload as the critic built it (no proof at all).
    pr = PullRequest(7, repo.base, head_b, frozenset({V.OVERRIDE_LABEL}), action=action)
    assert not run_gate(repo.path, pr, Probe(), now=NOW, lag=LAG).ok


def test_r3_edited_with_a_stale_label_fails(tmp_path: Path) -> None:
    repo, _, head_b = _r3_repo(tmp_path)
    result = _event_gate(tmp_path, repo, head_b, "edited", statuses=[])
    assert result.exit_code == 1 and "label is ignored" in result.output


@pytest.mark.parametrize("action", ["edited", "reopened", "labeled", "unlabeled"])
def test_r3_an_approved_head_keeps_the_label(tmp_path: Path, action: str) -> None:
    repo, _, head_b = _r3_repo(tmp_path)
    label = "other" if action in ("labeled", "unlabeled") else ""
    result = _event_gate(tmp_path, repo, head_b, action, label, [_status("ci"), APPROVED])
    assert result.exit_code == 0, result.output


def test_r3_the_labeled_event_itself_is_the_proof(tmp_path: Path) -> None:
    repo, _, head_b = _r3_repo(tmp_path)
    result = _event_gate(tmp_path, repo, head_b, "labeled", V.OVERRIDE_LABEL, [])
    assert result.exit_code == 0, result.output
    # A synchronize event never honours the label, even with an approval on record.
    pushed = _event_gate(tmp_path, repo, head_b, "synchronize", statuses=[APPROVED])
    assert pushed.exit_code == 1 and "ignored after a new push" in pushed.output


@pytest.mark.parametrize(
    "statuses",
    [
        [_status("registry-override/pr-8")],  # Another pull request's approval.
        [_status("registry-override/pr-7", creator="someone")],
        [_status("registry-override/pr-7", state="failure"), APPROVED],  # Newest first.
        [_status("registry-override/pr-7", state="pending")],
        ["not json", APPROVED],
        ["[1, 2]"],
        [],
    ],
)
def test_r3_only_the_newest_bot_status_for_this_pull_request_approves(
    statuses: list[str],
) -> None:
    from bot_farm_silence_tracker.registry.gate import override_status_approves

    assert not override_status_approves("\n".join(statuses), 7)
    assert override_status_approves("\n".join([_status("ci"), APPROVED, "\n"]), 7)


def test_r3_a_local_run_honours_the_given_label(tmp_path: Path) -> None:
    repo, _, head_b = _r3_repo(tmp_path)
    args = ["registry", "gate", "--repo", str(repo.path), "--base-sha", repo.base,
            "--head-sha", head_b, "--pr-number", "7"]  # fmt: skip
    assert runner.invoke(app, args).exit_code == 1
    assert runner.invoke(app, [*args, "--label", V.OVERRIDE_LABEL]).exit_code == 0


# -- R4: the override is bound to an approving review of the head commit --------------

HEAD = "b" * 40
OLDER = "a" * 40


def _approves(reviews: list[str], permissions: list[str] | None = None) -> bool:
    from bot_farm_silence_tracker.registry.gate import review_approves

    lines = [_permission()] if permissions is None else permissions
    return review_approves("\n".join(reviews), "\n".join(lines), HEAD, "author")


def test_r4_an_admins_approval_of_the_head_approves() -> None:
    assert _approves([_review(HEAD)])
    assert _approves([_review(HEAD)], [_permission(permission="admin")])
    # Comments after the approval leave it standing, as on GitHub.
    assert _approves([_review(HEAD), _review(HEAD, state="COMMENTED", review_id=2)])
    # Another reviewer's approval of the head is enough.
    assert _approves(
        [_review(OLDER, user="other"), _review(HEAD, review_id=2)],
        [_permission(), _permission("other")],
    )


@pytest.mark.parametrize(
    ("reviews", "permissions"),
    [
        ([_review(OLDER)], None),  # A review of an earlier commit.
        ([_review(HEAD, user="author")], [_permission("author")]),  # The author.
        ([_review(HEAD, user="Author")], [_permission("Author")]),  # Any letter case.
        ([_review(HEAD)], [_permission(permission="write")]),  # A writer is not the owner.
        ([_review(HEAD)], [_permission(permission="read")]),  # Triage reports read.
        ([_review(HEAD)], [_permission(permission="none")]),
        ([_review(HEAD)], [_permission(permission="maintain")]),  # Not an API value.
        ([_review(HEAD)], []),  # The permission could not be read.
        ([_review(HEAD)], [_permission("someone-else")]),
        ([_review(HEAD, state="COMMENTED")], None),
        ([_review(HEAD, state="CHANGES_REQUESTED")], None),
        ([_review(HEAD, state="DISMISSED")], None),  # A dismissed approval.
        ([_review(HEAD, state="PENDING")], None),
        ([_review(HEAD), _review(HEAD, state="CHANGES_REQUESTED", review_id=2)], None),
        # Listed out of order: the id decides which review is the latest.
        ([_review(HEAD, state="CHANGES_REQUESTED", review_id=2), _review(HEAD)], None),
        ([], None),
        (["not json", _review(HEAD)], None),
        ([_review(HEAD)], ["[1]"]),
        ([json.dumps({"id": 1, "user": None, "state": "APPROVED", "commit_id": HEAD})], None),
    ],
)
def test_r4_other_reviews_approve_nothing(
    reviews: list[str], permissions: list[str] | None
) -> None:
    assert not _approves(reviews, permissions)


def test_r4_no_author_or_head_approves_nothing() -> None:
    from bot_farm_silence_tracker.registry.gate import review_approves

    reviews, permissions = _review(HEAD), _permission()
    assert review_approves(reviews, permissions, HEAD, "author")
    assert not review_approves(reviews, permissions, HEAD, "")
    assert not review_approves(reviews, permissions, "", "author")


def test_r4_the_label_needs_a_review_of_the_commit_it_was_added_to(tmp_path: Path) -> None:
    """The critic's case: head A is under review, the author pushes B, the label lands on B."""
    repo, head_a, head_b = _r3_repo(tmp_path)
    reviewed_a = [_review(head_a)]
    for action, label, statuses in [
        ("labeled", V.OVERRIDE_LABEL, []),  # The labeled event names head B.
        ("edited", "", [APPROVED]),  # record-override stamped head B.
        ("labeled", "other", [APPROVED]),
    ]:
        result = _event_gate(tmp_path, repo, head_b, action, label, statuses, reviewed_a)
        assert result.exit_code == 1, result.output
        assert "no approving review of this head" in result.output
        # Once the maintainer approves head B itself, a rerun passes.
        approved = [*reviewed_a, _review(head_b, review_id=2)]
        assert _event_gate(tmp_path, repo, head_b, action, label, statuses, approved).exit_code == 0


def test_r4_the_label_without_a_review_is_ignored(tmp_path: Path) -> None:
    repo, _, head_b = _r3_repo(tmp_path)
    for reviews in (None, [], [_review(head_b, user="author")]):
        perms = [_permission(), _permission("author", "admin")]
        result = _event_gate(
            tmp_path, repo, head_b, "labeled", V.OVERRIDE_LABEL, [], reviews, perms
        )
        assert result.exit_code == 1 and "label is ignored" in result.output
    # A read-only reviewer's approval does not count either.
    result = _event_gate(
        tmp_path,
        repo,
        head_b,
        "labeled",
        V.OVERRIDE_LABEL,
        [],
        None,
        [_permission("maint", "read")],
    )
    assert result.exit_code == 1


def test_r4_a_review_without_the_label_overrides_nothing(tmp_path: Path) -> None:
    repo, _, head_b = _r3_repo(tmp_path)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "edited", "pull_request": {
        "number": 7, "base": {"sha": repo.base}, "head": {"sha": head_b},
        "user": {"login": "author"}, "labels": []}}))  # fmt: skip
    files = {"statuses": [APPROVED], "reviews": [_review(head_b)], "perms": [_permission()]}
    paths = {}
    for name, lines in files.items():
        paths[name] = tmp_path / f"{name}.jsonl"
        paths[name].write_text("".join(line + "\n" for line in lines))
    result = runner.invoke(app, [
        "registry", "gate", "--repo", str(repo.path), "--event", str(event),
        "--override-statuses", str(paths["statuses"]),
        "--override-reviews", str(paths["reviews"]),
        "--reviewer-permissions", str(paths["perms"]),
    ])  # fmt: skip
    assert result.exit_code == 1 and "label is ignored" not in result.output
    pr = PullRequest.from_event(event, head_approved=True, head_reviewed=True)
    assert pr.author == "author" and not pr.override_honoured


def test_r4_the_author_comes_from_the_event(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "edited", "pull_request": {
        "number": 7, "base": {"sha": "a"}, "head": {"sha": "b"}, "labels": []}}))  # fmt: skip
    assert PullRequest.from_event(event).author == ""
