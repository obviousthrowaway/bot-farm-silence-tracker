"""The owner placeholder for the GitHub account that hosts a copy of the project.

The old placeholder was a bare word that is also the name of a real GitHub
organization, so install links, attestation checks and User-Agent contacts could have
pointed at someone else's repository. The placeholder is now a token that can never be
a GitHub account name, the installers refuse to run while it is unreplaced, and the
User-Agent then names no project page.

Both tokens are written in parts in this file, so it matches neither the check for the
old token nor the README's replace command, and these tests pass before and after a
copy is published.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.sources import bluesky, reddit_archive
from bot_farm_silence_tracker.sources.http import (
    DEFAULT_USER_AGENT,
    OWNER_PLACEHOLDER,
    PROJECT_URL,
    default_user_agent,
    published_project_url,
    research_user_agent,
)

ROOT = Path(__file__).resolve().parents[1]
OLD_TOKEN = "O" + "WNER"
PLACEHOLDER = "_" + "_OWNER__"
#: The old bare token as a whole word. "\b" treats "_" as part of a word, so the new
#: placeholder does not match.
OLD_TOKEN_PATTERN = re.compile(rf"\b{OLD_TOKEN}\b")
#: Tracked files that may mention the old token on purpose, to document it. Empty: no
#: file needs to.
DOCUMENTED_OLD_TOKEN: frozenset[str] = frozenset()
#: The message both installers print when they refuse to run.
REFUSAL = (
    f"This copy has not been published yet: replace {PLACEHOLDER} with the GitHub account "
    "that hosts it (see Publishing your copy in the README)."
)
UNPUBLISHED_URL = f"https://github.com/{PLACEHOLDER}/bot-farm-silence-tracker"
PUBLISHED_URL = "https://github.com/example-owner/bot-farm-silence-tracker"

needs_posix_tools = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None or shutil.which("perl") is None,
    reason="needs a POSIX sh, grep, xargs and perl",
)


def _tracked_files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [name for name in out.decode("utf-8").split("\0") if name]


def _text(path: Path) -> str | None:
    """The file's text, or None for a binary file."""
    data = path.read_bytes()
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


# -- the tokens ------------------------------------------------------------------------


def test_the_placeholder_can_never_be_a_github_account_name() -> None:
    # GitHub account names have only letters, digits and single inner hyphens.
    account_name = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9])*")
    assert account_name.fullmatch(PLACEHOLDER) is None
    assert account_name.fullmatch(OLD_TOKEN) is not None  # why the old token was unsafe
    assert OWNER_PLACEHOLDER == PLACEHOLDER


def test_the_old_token_pattern_catches_the_old_token_only() -> None:
    assert OLD_TOKEN_PATTERN.search(f"https://github.com/{OLD_TOKEN}/bot-farm-silence-tracker")
    assert OLD_TOKEN_PATTERN.search(f"* @{OLD_TOKEN}")
    assert OLD_TOKEN_PATTERN.search(f"REPO='{OLD_TOKEN}/x'")
    assert not OLD_TOKEN_PATTERN.search(UNPUBLISHED_URL)
    assert not OLD_TOKEN_PATTERN.search(f"* @{PLACEHOLDER}")
    assert not OLD_TOKEN_PATTERN.search("CODEOWNERS and OWNERSHIP")


def test_no_tracked_file_uses_the_old_token() -> None:
    hits = []
    for name in _tracked_files():
        path = ROOT / name
        if name in DOCUMENTED_OLD_TOKEN or not path.is_file():
            continue
        text = _text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if OLD_TOKEN_PATTERN.search(line):
                hits.append(f"{name}:{number}: {line.strip()}")
    assert not hits, "the old placeholder is back; use the new one:\n" + "\n".join(hits)


# -- the User-Agent --------------------------------------------------------------------


def test_an_unpublished_copy_names_no_project_page() -> None:
    assert published_project_url(UNPUBLISHED_URL) is None
    research = research_user_agent(UNPUBLISHED_URL)
    assert research == f"bot-farm-silence-tracker/{__version__} (research tool; unpublished copy)"
    default = default_user_agent(UNPUBLISHED_URL)
    assert default == (
        f"bot-farm-silence-tracker/{__version__} (disinformation research tool; official "
        "platform APIs and documented research-archive APIs only, no scraping; "
        "unpublished copy)"
    )
    for agent in (research, default):
        assert "github" not in agent and "http" not in agent and PLACEHOLDER not in agent


def test_a_published_copy_names_its_project_page() -> None:
    assert published_project_url(PUBLISHED_URL) == PUBLISHED_URL
    assert research_user_agent(PUBLISHED_URL) == (
        f"bot-farm-silence-tracker/{__version__} (research; +{PUBLISHED_URL})"
    )
    assert default_user_agent(PUBLISHED_URL).endswith(f"no scraping; {PUBLISHED_URL})")


def test_the_adapters_use_this_copys_user_agent() -> None:
    assert (published_project_url(PROJECT_URL) is None) == (PLACEHOLDER in PROJECT_URL)
    assert research_user_agent(PROJECT_URL) == bluesky.USER_AGENT
    assert research_user_agent(PROJECT_URL) == reddit_archive.USER_AGENT
    assert default_user_agent(PROJECT_URL) == DEFAULT_USER_AGENT
    third_party = f"github.com/{OLD_TOKEN.lower()}/"
    for agent in (bluesky.USER_AGENT, reddit_archive.USER_AGENT, DEFAULT_USER_AGENT):
        assert third_party not in agent.lower()
        assert PLACEHOLDER not in agent


# -- the installers refuse an unpublished copy -------------------------------------------


def _unpublished_copy(tmp_path: Path, name: str, line: str, replacement: str) -> Path:
    """A copy of the installer NAME whose repository line holds the placeholder."""
    text = (ROOT / name).read_text(encoding="utf-8")
    text, count = re.subn(line, replacement, text, count=1, flags=re.MULTILINE)
    assert count == 1
    copy = tmp_path / name
    copy.write_text(text, encoding="utf-8")
    return copy


def _logging_fakes(tmp_path: Path) -> tuple[Path, Path]:
    """A folder of fake downloaders and tools that log every call, and the log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("curl", "wget", "gh", "uv"):
        fake = bin_dir / tool
        fake.write_text(f'#!/bin/sh\necho "{tool} $*" >>"{log}"\nexit 1\n')
        fake.chmod(0o755)
    return bin_dir, log


@needs_posix_tools
def test_install_sh_refuses_an_unpublished_copy_before_any_download(tmp_path: Path) -> None:
    script = _unpublished_copy(
        tmp_path, "install.sh", r"^REPO=.*$", f'REPO="{PLACEHOLDER}/bot-farm-silence-tracker"'
    )
    bin_dir, log = _logging_fakes(tmp_path)
    result = subprocess.run(
        ["sh", str(script), "--platforms", "bluesky"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin", "TMPDIR": str(tmp_path)},
    )
    assert result.returncode != 0
    assert REFUSAL in result.stderr
    assert "Looking up" not in result.stdout + result.stderr
    assert not log.exists(), log.read_text()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not installed")
def test_install_ps1_refuses_an_unpublished_copy_before_any_download(tmp_path: Path) -> None:
    script = _unpublished_copy(
        tmp_path,
        "install.ps1",
        r"^    \$Repo = '.*'$",
        f"    $Repo = '{PLACEHOLDER}/bot-farm-silence-tracker'",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BOT_FARM_SILENCE_TRACKER_")
    }
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            # The error message is printed whole, without PowerShell's line wrapping.
            f"try {{ Get-Content -Raw -LiteralPath '{script}' | Invoke-Expression }} "
            "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }",
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={**env, "HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path)},
    )
    assert result.returncode != 0
    assert REFUSAL in result.stderr
    assert "Looking up" not in result.stdout + result.stderr


# -- publishing a copy -----------------------------------------------------------------


def _replace_command() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Publishing your copy", 1)[1].split("\n## ", 1)[0]
    (command,) = re.findall(r"^grep -rlI .*$", section, flags=re.MULTILINE)
    assert "your-name" in command
    return command


def test_the_replace_command_cannot_rewrite_its_own_text() -> None:
    command = _replace_command()
    assert PLACEHOLDER not in command
    assert "_[_]OWNER__" in command


@needs_posix_tools
def test_publishing_a_copy_replaces_every_placeholder(tmp_path: Path) -> None:
    # A copy of the tree without git, like an unpacked .zip.
    copy = tmp_path / "bot-farm-silence-tracker"
    for name in _tracked_files():
        source = ROOT / name
        if source.is_file():
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    # A copy that is already published names its owner instead of the placeholder. Put the
    # placeholder back first, so that the command has something to replace.
    owner = PROJECT_URL.removeprefix("https://github.com/").split("/", 1)[0]
    if owner != PLACEHOLDER:
        for path in sorted(p for p in copy.rglob("*") if p.is_file()):
            text = _text(path)
            if text is not None and owner in text:
                path.write_bytes(text.replace(owner, PLACEHOLDER).encode("utf-8"))
    readme_before = (copy / "README.md").read_text(encoding="utf-8")
    command = _replace_command().replace("your-name", "example-owner")
    subprocess.run(["sh", "-c", command], cwd=copy, check=True)

    left = []
    for path in sorted(p for p in copy.rglob("*") if p.is_file()):
        text = _text(path)
        if text is not None and PLACEHOLDER in text:
            left.append(str(path.relative_to(copy)))
    assert not left, f"the placeholder is left in {left}"
    readme = (copy / "README.md").read_text(encoding="utf-8")
    assert _replace_command() in readme, "the command rewrote its own text"
    assert readme.count("example-owner") > readme_before.count("example-owner")

    install_sh = (copy / "install.sh").read_text(encoding="utf-8")
    assert 'REPO="example-owner/bot-farm-silence-tracker"' in install_sh
    assert 'OWNER_PLACEHOLDER="_""_OWNER__"' in install_sh
    subprocess.run(["sh", "-n", str(copy / "install.sh")], check=True)
    install_ps1 = (copy / "install.ps1").read_text(encoding="utf-8")
    assert "$Repo = 'example-owner/bot-farm-silence-tracker'" in install_ps1
    assert "$OwnerPlaceholder = '_' + '_OWNER__'" in install_ps1
    assert "* @example-owner" in (copy / ".github" / "CODEOWNERS").read_text(encoding="utf-8")

    # The published copy's User-Agent names its own project page.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from bot_farm_silence_tracker.sources import bluesky, http, reddit_archive\n"
            "print(http.DEFAULT_USER_AGENT)\nprint(bluesky.USER_AGENT)\n"
            "print(reddit_archive.USER_AGENT)",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(copy / "src")},
    )
    agents = probe.stdout.splitlines()
    assert agents[0].endswith(f"no scraping; {PUBLISHED_URL})")
    assert (
        agents[1]
        == agents[2]
        == f"bot-farm-silence-tracker/{__version__} (research; +{PUBLISHED_URL})"
    )
