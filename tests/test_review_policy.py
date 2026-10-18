"""The review policy: CODEOWNERS, the main branch's ruleset, its apply script, and the
gate rules that decide which registry changes need the repository owner's approval."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from bot_farm_silence_tracker.registry import records as R
from bot_farm_silence_tracker.registry import verify as V
from bot_farm_silence_tracker.registry.gate import (
    APPROVER_PERMISSIONS,
    PullRequest,
    render_gate,
    run_gate,
)
from bot_farm_silence_tracker.sources.base import NewestActivity

from .test_registry_integrity import (
    APPROVED,
    EVENT_DIR,
    LAG,
    NOW,
    VERIFIED,
    GateRepo,
    Probe,
    _event_gate,
    _permission,
    _r3_repo,
    _review,
    _write_evidence,
    arch,
    honest,
    keyed,
    rec,
)

REPO = Path(__file__).resolve().parents[1]
CODEOWNERS = REPO / ".github" / "CODEOWNERS"
RULESET = REPO / ".github" / "rulesets" / "main.json"
SCRIPT = REPO / "scripts" / "apply_repo_rules.sh"
WORKFLOWS = REPO / ".github" / "workflows"
GITHUB_ACTIONS_APP = 15368


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "current_time", lambda: NOW)


# -- CODEOWNERS ------------------------------------------------------------------------


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """A CODEOWNERS pattern as a regex, for the subset this repository uses.

    As in gitignore: ``*`` matches anything but ``/``; a leading ``/`` anchors the
    pattern at the root; a pattern without a ``/`` matches at any depth; a trailing
    ``/`` matches everything under that directory.
    """
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.strip("/")
    regex = "".join("[^/]*" if c == "*" else re.escape(c) for c in body)
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.*" if pattern.endswith("/") else "(?:/.*)?"
    return re.compile(f"^{prefix}{regex}{suffix}$")


def _rules() -> list[tuple[re.Pattern[str], list[str]]]:
    rules = []
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        assert "**" not in pattern and "?" not in pattern and "[" not in pattern, pattern
        rules.append((_pattern_regex(pattern), owners))
    return rules


def owners_of(path: str) -> list[str]:
    """The owners of ``path``: the last matching line wins, as on GitHub."""
    found: list[str] = []
    for regex, owners in _rules():
        if regex.match(path):
            found = owners
    return found


RECORD = f"registry/{EVENT_DIR}/bluesky/14.jsonl"
EVIDENCE_FILE = f"registry/{EVENT_DIR}/evidence/bluesky/ZGlkOnBsYzphYmM.md"


@pytest.mark.parametrize(
    "path",
    [
        RECORD,
        f"registry/{EVENT_DIR}/reddit-archive/00.jsonl",
        "registry/events/2026-12-takedown/x/ff.jsonl",
        "registry/events/2026-12-takedown/reddit/a0.jsonl",
    ],
)
def test_account_records_have_no_owner(path: str) -> None:
    assert owners_of(path) == []
    assert R.file_kind(path.removeprefix("registry/")) == "records"


@pytest.mark.parametrize(
    "path",
    [
        EVIDENCE_FILE,
        "events.toml",
        "registry/schema.json",
        ".github/CODEOWNERS",
        ".github/rulesets/main.json",
        ".github/workflows/registry-gate.yml",
        "scripts/apply_repo_rules.sh",
        "src/bot_farm_silence_tracker/registry/gate.py",
        "src/bot_farm_silence_tracker/cli.py",
        "README.md",
        "docs/charts/default-light.svg",
        # Not records: no path ambiguity with the ownerless line.
        f"registry/{EVENT_DIR}/evidence/notes.jsonl",
        f"registry/{EVENT_DIR}/evidence/bluesky/aa.jsonl",
        f"registry/{EVENT_DIR}/notes.md",
        f"registry/{EVENT_DIR}/bluesky/14.md",
        "registry/bluesky/14.jsonl",
    ],
)
def test_everything_else_is_owned(path: str) -> None:
    owners = owners_of(path)
    assert owners and all(o.startswith("@") for o in owners), path
    assert R.file_kind(path.removeprefix("registry/")) != "records"


def test_the_ownerless_paths_are_exactly_the_record_files() -> None:
    ownerless = [owners for _, owners in _rules() if not owners]
    assert len(ownerless) == 1
    assert "/registry/events/*/*/*.jsonl" in CODEOWNERS.read_text(encoding="utf-8").split()


# -- the ruleset -----------------------------------------------------------------------


def _ruleset() -> dict[str, Any]:
    return json.loads(RULESET.read_text(encoding="utf-8"))


def _rule(kind: str) -> dict[str, Any]:
    rules = [r for r in _ruleset()["rules"] if r["type"] == kind]
    assert len(rules) == 1, kind
    return rules[0]


def test_ruleset_has_the_required_keys() -> None:
    ruleset = _ruleset()
    assert {"name", "target", "enforcement", "conditions", "rules", "bypass_actors"} <= set(ruleset)
    assert ruleset["target"] == "branch" and ruleset["enforcement"] == "active"
    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert {r["type"] for r in ruleset["rules"]} == {
        "pull_request",
        "required_status_checks",
        "non_fast_forward",
        "deletion",
    }


def test_ruleset_review_rules() -> None:
    review = _rule("pull_request")["parameters"]
    assert review["required_approving_review_count"] == 0
    assert review["require_code_owner_review"] is True
    assert review["dismiss_stale_reviews_on_push"] is True
    assert review["require_last_push_approval"] is True
    assert isinstance(review["required_review_thread_resolution"], bool)


def test_ruleset_bypass_actors() -> None:
    actors = _ruleset()["bypass_actors"]
    # The repository admin role (id 5), and only through a pull request: nobody, the
    # owner included, may push to main directly. GitHub refuses the GitHub Actions
    # app as a bypass actor in a repository owned by a user, so the chart workflow
    # opens a pull request instead.
    admin = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
    assert actors == [admin]


def _jobs() -> dict[str, tuple[str, dict[str, Any], dict[str, Any]]]:
    """Check-run names of every workflow job, with their workflow's triggers."""
    names: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = workflow.get(True) or workflow.get("on")
        for job_id, job in workflow["jobs"].items():
            name = job.get("name", job_id)
            matrix = (job.get("strategy") or {}).get("matrix") or {}
            if "${{ matrix." in name:
                keys = re.findall(r"\$\{\{ matrix\.(\w+) \}\}", name)
                if len(keys) != 1:
                    continue  # Not a required check; only one-key names are expanded.
                key = keys[0]
                values = matrix.get(key) or [
                    item[key] for item in matrix.get("include", []) if key in item
                ]
                for value in values:
                    names[re.sub(r"\$\{\{ matrix\.\w+ \}\}", str(value), name)] = (
                        path.name,
                        triggers,
                        job,
                    )
            else:
                names[name] = (path.name, triggers, job)
    return names


def test_required_checks_exist_and_run_on_every_pull_request() -> None:
    checks = _rule("required_status_checks")["parameters"]
    assert checks["strict_required_status_checks_policy"] is False
    contexts = [c["context"] for c in checks["required_status_checks"]]
    assert all(c["integration_id"] == GITHUB_ACTIONS_APP for c in checks["required_status_checks"])
    assert len(contexts) == len(set(contexts))
    assert "gate" in contexts and "lint" in contexts and "test-windows" in contexts
    jobs = _jobs()
    ci_jobs = {name for name, (file, _, _) in jobs.items() if file == "ci.yml"}
    assert ci_jobs <= set(contexts), ci_jobs - set(contexts)
    for context in contexts:
        assert context in jobs, context
        file, triggers, job = jobs[context]
        # The check must report on every pull request: a pull request trigger with no
        # path or branch filter, and no job-level condition.
        event = "pull_request_target" if context == "gate" else "pull_request"
        assert event in triggers, (context, file)
        options = triggers[event] or {}
        assert not {"paths", "paths-ignore", "branches", "branches-ignore"} & set(options)
        assert "if" not in job, context
        assert "needs" not in job, context


def test_gate_reports_on_every_pull_request_with_a_fast_no_op() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "registry-gate.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["gate"]["steps"]
    names = [s.get("name") or s.get("uses") or s.get("run") for s in steps]
    classify = next(i for i, s in enumerate(steps) if s.get("id") == "classify")
    # Everything before it is the base check, the base checkout and the head fetch.
    assert classify == 3, names
    assert "No registry data changed" in steps[classify]["run"]
    assert "registry/|events\\.toml$" in steps[classify]["run"]
    for step in steps[classify + 1 :]:
        assert step.get("if") == "steps.classify.outputs.data == 'true'", step
    for step in steps[:classify]:
        assert "if" not in step


# -- the apply script ------------------------------------------------------------------

needs_bash = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None, reason="needs bash"
)


def _script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


@needs_bash
def test_apply_script_dry_run_prints_the_payload(tmp_path: Path) -> None:
    # No gh on PATH: a dry run calls no API.
    empty = tmp_path / "bin"
    empty.mkdir()
    python = Path(shutil.which("python3") or "/usr/bin/python3")
    (empty / "python3").symlink_to(python)
    result = _script(
        "--dry-run", "example-owner/example-repo", env={"PATH": f"{empty}:/usr/bin:/bin"}
    )
    assert result.returncode == 0, result.stderr
    assert 'ruleset "main review policy" on example-owner/example-repo' in result.stdout
    payload = result.stdout.split("Payload:\n", 1)[1]
    assert json.loads(payload) == _ruleset()


@needs_bash
@pytest.mark.parametrize(("owner", "warns"), [("_" + "_OWNER__", True), ("example-owner", False)])
def test_apply_script_warns_about_the_unreplaced_owner_placeholder(
    tmp_path: Path, owner: str, warns: bool
) -> None:
    # A copy of the script in a tree whose CODEOWNERS names the given owner. The
    # placeholder is written in two parts so that publishing a copy leaves this test intact.
    (tmp_path / "scripts").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    (tmp_path / ".github" / "rulesets").mkdir(parents=True)
    shutil.copy2(RULESET, tmp_path / ".github" / "rulesets" / RULESET.name)
    (tmp_path / ".github" / "CODEOWNERS").write_text(f"* @{owner}\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / SCRIPT.name), "--dry-run", "example-owner/x"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert ("still names the @" + "_" + "_OWNER__ placeholder" in result.stderr) is warns
    assert ("warning" in result.stderr) is warns


@needs_bash
@pytest.mark.parametrize(
    "args",
    [(), ("--dry-run",), ("not-a-repo",), ("a/b", "c/d"), ("--force", "a/b"), ("a b/c",)],
)
def test_apply_script_refuses_bad_arguments(args: tuple[str, ...]) -> None:
    result = _script(*args)
    assert result.returncode == 2, result.stdout + result.stderr


FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$GH_LOG"
case "$*" in
  *"rulesets?includes_parents"*) [ -n "$EXISTING_ID" ] && echo "$EXISTING_ID"; exit 0 ;;
  *"--method POST"*) echo 4242; exit 0 ;;
  *"--method PUT"*) echo "$EXISTING_ID"; exit 0 ;;
  *codeowners/errors*) exit 0 ;;
  *) echo "Ruleset main review policy"; exit 0 ;;
esac
"""


@needs_bash
@pytest.mark.parametrize(("existing", "method"), [("", "POST"), ("777", "PUT")])
def test_apply_script_creates_or_updates_by_name(
    tmp_path: Path, existing: str, method: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    result = _script(
        "example-owner/example-repo",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GH_LOG": str(log),
            "EXISTING_ID": existing,
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert "repos/example-owner/example-repo/rulesets?includes_parents=false" in calls[0]
    assert 'select(.name == "main review policy"' in calls[0]
    writes = [c for c in calls if "--method" in c]
    assert len(writes) == 1 and f"--method {method}" in writes[0]
    assert f"--input {RULESET}" in writes[0]
    if existing:
        assert f"--method PUT repos/example-owner/example-repo/rulesets/{existing} " in writes[0]


@needs_bash
def test_apply_script_refuses_two_rulesets_with_the_name(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    result = _script(
        "example-owner/example-repo",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GH_LOG": str(log),
            "EXISTING_ID": "1\n2",
        },
    )
    assert result.returncode == 1 and "more than one ruleset" in result.stderr
    assert not [c for c in log.read_text().splitlines() if "--method" in c]


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint is not installed")
def test_actionlint_on_every_workflow() -> None:
    files = [str(p) for p in sorted(WORKFLOWS.glob("*.yml"))]
    result = subprocess.run(["actionlint", *files], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_shellcheck_on_the_apply_script() -> None:
    result = subprocess.run(
        ["shellcheck", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# -- the gate: the no-op path and mixed pull requests ----------------------------------


def test_gate_no_op_for_code_only_pull_requests(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [arch("alice", status="scanned")])
    (repo.path / "README.md").write_text("changed\n")
    (repo.path / ".github").mkdir()
    (repo.path / ".github" / "CODEOWNERS").write_text("* @someone\n")
    result = repo.gate()
    assert result.ok and result.report is None and not result.failures
    assert "No registry data changed" in render_gate(result)


@pytest.mark.parametrize(
    "other", ["README.md", ".github/CODEOWNERS", ".github/rulesets/main.json", "src/x.py"]
)
def test_gate_fails_mixed_data_and_code(tmp_path: Path, other: str) -> None:
    repo = GateRepo(tmp_path / "r", [])
    path = repo.path / other
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n")
    R.write_records(repo.path / "registry", [arch("someone", status="scanned")])
    result = repo.gate(honest(arch("someone", status="scanned")))
    assert not result.ok and "separate pull requests" in result.failures[0]
    # Even the honoured override does not allow it.
    labelled = repo.gate(honest(arch("someone", status="scanned")), labels=(V.OVERRIDE_LABEL,))
    assert not labelled.ok


# -- the gate: what needs the owner's approval -----------------------------------------


def test_plain_scanned_records_need_no_review(tmp_path: Path) -> None:
    records = [arch(f"user{i}", status="scanned") for i in range(3)] + [rec()]
    repo = GateRepo(tmp_path / "r", [])
    R.write_records(repo.path / "registry", records)
    result = repo.gate(honest(*records))
    assert result.ok, render_gate(result)
    assert result.report is not None and result.report.sampled == 4
    assert result.report.count(V.VERIFIED) == 4


def _needs_owner(repo: GateRepo, probe: Probe, needle: str) -> None:
    """Fails without the override, and passes with it."""
    refused = repo.gate(probe)
    assert not refused.ok, render_gate(refused)
    failures = (refused.report.failures if refused.report else []) + refused.failures
    assert any(needle in f for f in failures), failures
    allowed = repo.gate(probe, labels=(V.OVERRIDE_LABEL,))
    assert allowed.ok, render_gate(allowed)


def test_a_new_verified_record_needs_the_owner(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    _write_evidence(repo)
    R.write_records(repo.path / "registry", [VERIFIED])
    _needs_owner(repo, honest(VERIFIED), "move into verified_inauthentic or cleared")


def test_a_new_cleared_record_needs_the_owner(tmp_path: Path) -> None:
    cleared = arch("alice", status="cleared")
    repo = GateRepo(tmp_path / "r", [])
    R.write_records(repo.path / "registry", [cleared])
    _needs_owner(repo, honest(cleared), "move into verified_inauthentic or cleared")


def test_a_candidate_moving_into_verified_needs_the_owner(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [rec()])
    _write_evidence(repo)
    R.write_records(repo.path / "registry", [VERIFIED])
    _needs_owner(repo, honest(VERIFIED), "move into verified_inauthentic or cleared")


def test_many_verified_records_without_a_label_fail(tmp_path: Path) -> None:
    """The old rule let up to 15 records be added as verified_inauthentic unlabelled."""
    names = [f"user{i:02d}" for i in range(15)]
    records = [arch(n, status="verified_inauthentic") for n in names]
    records = [dict(r, evidence=R.evidence_path("reddit-archive", r["id_b64"])) for r in records]
    repo = GateRepo(tmp_path / "r", [])
    for r in records:
        path = repo.path / "registry" / EVENT_DIR / r["evidence"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Evidence\n")
    R.write_records(repo.path / "registry", records)
    result = repo.gate(honest(*records))
    assert not result.ok and result.report is not None
    assert any("15 records move into" in f for f in result.report.failures)


@pytest.mark.parametrize("status", ["verified_inauthentic", "cleared"])
def test_any_change_to_a_human_status_record_needs_the_owner(tmp_path: Path, status: str) -> None:
    before = dict(VERIFIED, status=status)
    repo = GateRepo(tmp_path / "r", [before])
    _write_evidence(repo)
    repo.base = repo.commit()
    # Only the scan bookkeeping changes; the status stays.
    later = dict(before, last_scanned_at=isoformat_days(32), scan_window_end=isoformat_days(31))
    R.write_records(repo.path / "registry", [later])
    _needs_owner(repo, honest(later), "existing verified_inauthentic or cleared records change")


def isoformat_days(days: int) -> str:
    from bot_farm_silence_tracker.timeutil import isoformat

    from .test_registry_integrity import T

    return isoformat(T + timedelta(days=days))


@pytest.mark.parametrize("change", ["add", "edit", "delete"])
def test_any_evidence_change_needs_the_owner(tmp_path: Path, change: str) -> None:
    """Even an evidence file of a record that stays a candidate."""
    candidate = rec()
    repo = GateRepo(tmp_path / "r", [candidate])
    if change != "add":
        _write_evidence(repo, "# Evidence\n")
        repo.base = repo.commit()
    path = repo.path / "registry" / EVENT_DIR / "evidence" / "bluesky" / f"{candidate['id_b64']}.md"
    if change == "delete":
        path.unlink()
    else:
        _write_evidence(repo, "# Evidence, edited\n")
    _needs_owner(repo, honest(candidate), "evidence files are added, changed or removed")


@pytest.mark.parametrize(("platform", "user"), [("reddit", "alice"), ("x", "12345")])
def test_records_ci_cannot_check_need_the_owner(tmp_path: Path, platform: str, user: str) -> None:
    row = rec(
        platform=platform, id_b64=R.b64e(user), username_b64=R.b64e(user),
        status="scanned", cliff=False,
    )  # fmt: skip
    repo = GateRepo(tmp_path / "r", [])
    R.write_records(repo.path / "registry", [row])
    _needs_owner(repo, Probe(), "cannot query")


def test_removing_a_record_needs_the_owner(tmp_path: Path) -> None:
    both = [arch("alice", status="scanned"), arch("bobby", status="scanned")]
    repo = GateRepo(tmp_path / "r", both)
    R.write_records(repo.path / "registry", [arch("alice", status="scanned")])
    shutil.rmtree(repo.path / "registry" / "events")
    R.write_records(repo.path / "registry", [arch("alice", status="scanned")])
    _needs_owner(repo, Probe(), "records are removed")


# -- the gate: the live spot-check fails closed ----------------------------------------


class StoppingProbe:
    """A live source that is out of budget at once."""

    requests_made = 0

    def newest(self, record: dict[str, Any], since: datetime, until: datetime) -> NewestActivity:
        raise V.Stop("the request budget is used up")


def test_the_spot_check_fails_closed_when_it_cannot_run() -> None:
    rows = [arch(f"user{i}", status="scanned") for i in range(2)]
    report = V.run_verify({}, keyed(*rows), StoppingProbe(), seed="s", now=NOW, lag=LAG)
    assert not report.ok
    assert any("could not finish" in f for f in report.failures)
    assert any("only 0 records were checked" in f for f in report.failures)


def test_the_spot_check_fails_closed_even_with_the_label() -> None:
    rows = [arch(f"user{i}", status="scanned") for i in range(30)]
    report = V.run_verify(
        {}, keyed(*rows), StoppingProbe(), seed="s", now=NOW, lag=LAG, labels=[V.OVERRIDE_LABEL]
    )
    assert not report.ok and report.sampled >= V.MIN_SAMPLE
    assert any("could not finish" in f for f in report.failures)


def test_the_minimum_sample_stays_mandatory() -> None:
    rows = [arch(f"user{i:02d}", status="scanned") for i in range(40)]
    plan = V.plan_changes({}, keyed(*rows), seed="s", labels=[V.OVERRIDE_LABEL])
    assert len(plan.selected) >= V.spot_check_minimum(plan.changed) == V.MIN_SAMPLE
    honest_report = V.run_verify(
        {}, keyed(*rows), honest(*rows), seed="s", now=NOW, lag=LAG, labels=[V.OVERRIDE_LABEL]
    )
    assert honest_report.ok, honest_report.failures


# -- the approver must be the owner ----------------------------------------------------


def test_only_admins_may_approve_an_override() -> None:
    assert frozenset({"admin"}) == APPROVER_PERMISSIONS


@pytest.mark.parametrize(("permission", "ok"), [("admin", True), ("write", False), ("read", False)])
def test_the_gate_honours_only_the_owners_approval(
    tmp_path: Path, permission: str, ok: bool
) -> None:
    repo, _, head_b = _r3_repo(tmp_path)  # Head B removes a record: needs the owner.
    result = _event_gate(
        tmp_path, repo, head_b, "edited", "", [APPROVED], [_review(head_b, user="maint")],
        [_permission("maint", permission)],
    )  # fmt: skip
    assert (result.exit_code == 0) is ok, result.output
    if not ok:
        assert "by the repository owner (admin access)" in result.output


def test_the_owner_cannot_approve_their_own_pull_request(tmp_path: Path) -> None:
    repo, _, head_b = _r3_repo(tmp_path)
    result = _event_gate(
        tmp_path, repo, head_b, "edited", "", [APPROVED], [_review(head_b, user="author")],
        [_permission("author", "admin")],
    )  # fmt: skip
    assert result.exit_code == 1 and "no approving review of this head" in result.output


def test_the_label_alone_is_not_the_owners_approval(tmp_path: Path) -> None:
    repo = GateRepo(tmp_path / "r", [])
    _write_evidence(repo)
    R.write_records(repo.path / "registry", [VERIFIED])
    head = repo.commit()
    # The label was added for this head, but nobody approved it.
    pr = PullRequest(
        7, repo.base, head, frozenset({V.OVERRIDE_LABEL}), action="labeled",
        event_label=V.OVERRIDE_LABEL, author="author", head_reviewed=False,
    )  # fmt: skip
    result = run_gate(repo.path, pr, honest(VERIFIED), now=NOW, lag=LAG)
    assert not result.ok
    approved = PullRequest(
        7, repo.base, head, frozenset({V.OVERRIDE_LABEL}), action="labeled",
        event_label=V.OVERRIDE_LABEL, author="author", head_reviewed=True,
    )  # fmt: skip
    assert run_gate(repo.path, approved, honest(VERIFIED), now=NOW, lag=LAG).ok
