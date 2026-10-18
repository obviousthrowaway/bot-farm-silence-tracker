"""Checks for the installer scripts and workflow that run without macOS or Windows.

The shell scripts run in a throwaway HOME inside pytest's temporary folder, so nothing is
installed or removed in the real home folder. The Inno Setup script and the workflows are
checked as text, because Setup.exe is built and tested by the Windows CI job.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY

ROOT = Path(__file__).resolve().parent.parent
MACOS = ROOT / "packaging" / "macos"
SCRIPTS = MACOS / "scripts"
ISS = ROOT / "packaging" / "windows" / "bot-farm-silence-tracker.iss"
WORKFLOWS = ROOT / ".github" / "workflows"
TOOL = "bot-farm-silence-tracker"
PLATFORM_NAMES = {"reddit-archive", "bluesky", "x", "reddit"}

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
# These run the macOS and Unix scripts against POSIX paths; Git's sh on Windows turns
# the Windows temporary paths into other paths, so they only run on macOS and Linux.
needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="runs POSIX scripts on POSIX paths"
)


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _env(home: Path, **extra: str) -> dict[str, str]:
    """A clean environment whose HOME is the temporary folder."""
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home)}
    env.update(extra)
    return env


# --- M1: the release never ships the stand-in app ---------------------------------------


def _steps(job: str) -> list[dict]:
    data = yaml.safe_load((WORKFLOWS / "installers.yml").read_text(encoding="utf-8"))
    return data["jobs"][job]["steps"]


@pytest.mark.parametrize("job", ["macos-pkg", "windows-setup"])
def test_stand_in_steps_only_run_outside_a_release(job: str) -> None:
    stand_ins = [s for s in _steps(job) if "stand-in" in s.get("name", "").lower()]
    assert stand_ins, "the test build needs a stand-in step"
    for step in stand_ins:
        assert step["if"].replace(" ", "") == "${{!inputs.release}}", step["name"]


def test_release_downloads_the_real_desktop_app_and_checks_it() -> None:
    mac = _steps("macos-pkg")
    downloads = [s for s in mac if s.get("with", {}).get("path") == "dist-gui"]
    assert [s["with"]["name"] for s in downloads] == [
        "bot-farm-silence-tracker-macos-${{ matrix.arch }}.app.zip"
    ]
    win = _steps("windows-setup")
    downloads = [s for s in win if s.get("with", {}).get("path") == "dist-gui"]
    assert [s["with"]["name"] for s in downloads] == [
        "bot-farm-silence-tracker-gui-windows-x64.exe"
    ]
    for steps in (mac, win):
        for step in steps:
            if step.get("with", {}).get("path") == "dist-gui" or "desktop app is" in step.get(
                "name", ""
            ):
                assert step["if"].replace(" ", "") == "${{inputs.release}}"
        checks = [s for s in steps if s.get("name", "").startswith("Check the desktop app")]
        assert len(checks) == 1
        assert "is missing" in checks[0]["run"]
        assert "exit 1" in checks[0]["run"] or "throw" in checks[0]["run"]


def test_stand_in_output_never_has_a_release_file_name() -> None:
    text = (WORKFLOWS / "installers.yml").read_text(encoding="utf-8")
    real = [
        "bot-farm-silence-tracker-macos-arm64.app.zip",
        "bot-farm-silence-tracker-macos-x64.app.zip",
        "bot-farm-silence-tracker-gui-windows-x64.exe",
    ]
    mac_standin = [s for s in _steps("macos-pkg") if "stand-in" in s.get("name", "").lower()]
    win_standin = [s for s in _steps("windows-setup") if "stand-in" in s.get("name", "").lower()]
    for step in mac_standin + win_standin:
        run = step["run"]
        assert "standin" in run
        assert not any(f'"{name}"' in run or f" {name}" in run for name in real)
    # Only the installers are uploaded, never the stand-in files.
    uploads = [
        s["with"]["path"]
        for job in ("macos-pkg", "windows-setup")
        for s in _steps(job)
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert all("standin" not in path and "dist-gui" not in path for path in uploads)
    assert "dist-bin/bot-farm-silence-tracker-gui-windows-x64.exe" not in text


def test_release_workflow_runs_installers_in_release_mode() -> None:
    data = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    job = data["jobs"]["installers"]
    assert job["with"] == {"release": True}
    assert "binary" in job["needs"]


# --- L1: the Setup /PLATFORMS= value is validated ---------------------------------------


def test_setup_validates_the_platforms_parameter() -> None:
    text = ISS.read_text(encoding="utf-8")
    code = text[text.index("[Code]") :]
    assert "function InitializeSetup: Boolean;" in code
    init = code[code.index("function InitializeSetup") :]
    init = init[: init.index("\nend;")]
    assert "ValidPlatformList(Given)" in init
    assert "Result := False" in init  # aborts Setup, which exits with a nonzero code
    assert "WizardSilent" in init  # no message box in a silent run
    valid = code[code.index("function ValidPlatformList") :]
    valid = valid[: valid.index("\nend;\n")]
    assert "(C >= 'a') and (C <= 'z')" in valid
    assert "(C = ',') or (C = '-')" in valid
    names = set(re.findall(r"\(Name <> '([^']+)'\)", valid))
    names |= set(re.findall(r"\(Name <> '(\{#DefaultPlatform\})'\)", valid))
    names = {"reddit-archive" if n == "{#DefaultPlatform}" else n for n in names}
    assert names == PLATFORM_NAMES
    assert set(SOURCE_REGISTRY) >= PLATFORM_NAMES
    # SavePlatforms checks the value again before it reaches the command line.
    save = code[code.index("procedure SavePlatforms") :]
    assert save.index("ValidPlatformList(List)") < save.index("RunCliCapture(Params")


# --- the owner placeholder: Setup names no address for an unpublished copy -------------

# Written in two parts so that the README's replace command leaves these tests intact.
OWNER_PLACEHOLDER = "_" + "_OWNER__"


def test_setup_shows_the_project_address_only_for_a_published_copy() -> None:
    text = ISS.read_text(encoding="utf-8")
    assert '#define OwnerPlaceholder "_" + "_OWNER__"' in text
    setup = text[text.index("[Setup]") : text.index("\n[", text.index("[Setup]") + 1)]
    block = re.search(
        r"^#if Pos\(OwnerPlaceholder, RepoUrl\) == 0\n(.*?)^#endif\n", setup, re.S | re.M
    )
    assert block, "the address lines must be inside the placeholder check"
    assert block.group(1).splitlines() == [
        "AppPublisherURL={#RepoUrl}",
        "AppSupportURL={#RepoUrl}",
        "AppUpdatesURL={#RepoUrl}/releases",
    ]
    # No other line of the script names the address.
    outside = text.replace(block.group(0), "")
    assert not re.search(r"^[^;#].*\{#RepoUrl\}", outside, re.M)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not installed")
@pytest.mark.parametrize(
    ("repo_url", "passed"),
    [
        (None, False),
        (f"https://github.com/{OWNER_PLACEHOLDER}/bot-farm-silence-tracker", False),
        ("https://github.com/example-owner/bot-farm-silence-tracker", True),
    ],
)
def test_build_setup_passes_the_address_only_for_a_published_copy(
    tmp_path: Path, repo_url: str | None, passed: bool
) -> None:
    # A fake ISCC.exe that records its arguments and writes the expected Setup.exe.
    log = tmp_path / "iscc-args.txt"
    out = tmp_path / "out"
    iscc = tmp_path / "fake_iscc.ps1"
    setup_exe = out / "bot-farm-silence-tracker-1.2.3-windows-x64-setup.exe"
    iscc.write_text(
        f"$args | Set-Content -LiteralPath '{log}'\n"
        f"Set-Content -LiteralPath '{setup_exe}' x\n"
        "exit 0\n",
        encoding="utf-8",
    )
    cli = tmp_path / "cli.exe"
    gui = tmp_path / "gui.exe"
    cli.write_text("x")
    gui.write_text("x")
    args = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(ROOT / "packaging" / "windows" / "build_setup.ps1"),
        "-Iscc",
        str(iscc),
        "-Version",
        "1.2.3",
        "-CliExe",
        str(cli),
        "-GuiExe",
        str(gui),
        "-OutputDir",
        str(out),
    ]
    if repo_url is None:
        # The default address: this copy's own, unpublished until it is replaced.
        script = (ROOT / "packaging" / "windows" / "build_setup.ps1").read_text(encoding="utf-8")
        match = re.search(r"\[string\]\$RepoUrl = '([^']*)'", script)
        assert match
        repo_url = match.group(1)
        passed = OWNER_PLACEHOLDER not in repo_url
    else:
        args += ["-RepoUrl", repo_url]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert "/DAppVersion=1.2.3" in recorded
    repo_args = [a for a in recorded if a.startswith("/DRepoUrl=")]
    if passed:
        assert repo_args == [f"/DRepoUrl={repo_url}"]
        assert "placeholder" not in result.stdout + result.stderr
    else:
        assert repo_args == []
        assert "Setup names no project address" in result.stdout + result.stderr


def test_windows_test_script_covers_the_new_behaviour() -> None:
    text = (ROOT / "packaging" / "windows" / "test_setup.ps1").read_text(encoding="utf-8")
    assert "Invalid /PLATFORMS= value" in text
    assert "so the platforms you saved earlier were kept" in text


# --- L2 and L3: the uninstall script ------------------------------------------------------


def _fake_pkgutil(bin_dir: Path, log: Path) -> None:
    _write_exe(bin_dir / "pkgutil", f'#!/bin/sh\necho "$@" >>"{log}"\n')


def _install_files(home: Path) -> None:
    _write_exe(home / ".local" / "bin" / TOOL, "#!/bin/sh\n")
    _write_exe(home / ".local" / "bin" / f"{TOOL}-uninstall", "#!/bin/sh\n")
    (home / "Applications" / "Bot Farm Silence Tracker.app").mkdir(parents=True)


def _run_uninstall(home: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(MACOS / "uninstall.sh"), *args],
        env=_env(home, **env),
        capture_output=True,
        text=True,
        check=False,
        cwd=home,
    )


@needs_sh
def test_uninstall_forgets_every_receipt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_files(home)
    fake = tmp_path / "fakebin"
    log = tmp_path / "pkgutil.log"
    _fake_pkgutil(fake, log)
    result = _run_uninstall(home, "--yes", PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    forgotten = {line.split()[-1] for line in log.read_text().splitlines()}
    assert forgotten == {
        f"{TOOL}.app",
        f"{TOOL}.cli",
        f"{TOOL}.finish",
        *(f"{TOOL}.platform.{name}" for name in PLATFORM_NAMES),
    }


@needs_sh
def test_uninstall_ignores_a_relative_xdg_config_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_files(home)
    real_config = home / ".config" / TOOL
    real_config.mkdir(parents=True)
    (real_config / "config.toml").write_text("x")
    relative = home / "rel" / TOOL  # what XDG_CONFIG_HOME=rel would name, from cwd=home
    relative.mkdir(parents=True)
    (relative / "config.toml").write_text("keep")
    result = _run_uninstall(home, "--yes", "--purge", XDG_CONFIG_HOME="rel")
    assert result.returncode == 0, result.stderr
    assert not real_config.exists()
    assert (relative / "config.toml").read_text() == "keep"


@needs_sh
@needs_posix
def test_uninstall_uses_an_absolute_xdg_config_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_files(home)
    xdg = tmp_path / "xdg"
    config = xdg / TOOL
    config.mkdir(parents=True)
    (config / "config.toml").write_text("x")
    fish = xdg / "fish" / "conf.d" / f"{TOOL}.fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("fish_add_path\n")
    result = _run_uninstall(home, "--yes", "--purge", XDG_CONFIG_HOME=str(xdg))
    assert result.returncode == 0, result.stderr
    assert not config.exists()
    assert not fish.exists()  # the bin folder is empty afterwards, so the PATH entry goes


@needs_sh
@needs_posix
def test_uninstall_removes_the_path_line_from_zdotdir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_files(home)
    zdot = tmp_path / "zdot"
    zdot.mkdir()
    marker = "# Added by the Bot Farm Silence Tracker installer"
    rc = zdot / ".zshrc"
    rc.write_text(f'alias a=b\n\n{marker}\nexport PATH="{home}/.local/bin:$PATH"\n')
    result = _run_uninstall(home, "--yes", ZDOTDIR=str(zdot))
    assert result.returncode == 0, result.stderr
    assert rc.read_text() == "alias a=b\n"


# --- L4: the package's PATH setup matches install.sh ----------------------------------------


def _install_sh_add_to_path() -> str:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    start = text.index("\nadd_to_path() {") + 1
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _profile_after(home: Path, shell: str, extra_env: dict[str, str], *, use_package: bool) -> str:
    """Run one implementation and return the text of the one file it wrote (path relative
    to the temp root, then its content)."""
    root = home.parent
    install_dir = f"{home}/.local/bin"
    if use_package:
        body = f'. "{SCRIPTS}/common.sh"\nlogin_shell() {{ printf %s "{shell}"; }}\n'
        body += f'add_to_path "{home}" "{install_dir}"\n'
    else:
        body = (
            f'TOOL_NAME={TOOL}\nsay() {{ :; }}\nORIG_PATH=/usr/bin\nINSTALL_DIR="{install_dir}"\n'
            "uname() { echo Darwin; }\n" + _install_sh_add_to_path() + "add_to_path\n"
        )
    env = _env(home, **extra_env)
    if not use_package:
        env["SHELL"] = shell
    subprocess.run(["sh", "-c", body], env=env, check=True, capture_output=True, cwd=home)
    written = sorted(p for p in root.rglob("*") if p.is_file())
    return "\n".join(f"{p.relative_to(root)}\n{p.read_text()}" for p in written)


@needs_sh
@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash", "/usr/local/bin/fish", "/bin/sh"])
@pytest.mark.parametrize("variant", ["none", "absolute", "relative"])
def test_package_path_setup_matches_install_sh(tmp_path: Path, shell: str, variant: str) -> None:
    results = []
    for use_package in (True, False):
        base = tmp_path / ("pkg" if use_package else "sh")
        home = base / "home"
        home.mkdir(parents=True)
        env: dict[str, str] = {}
        if variant == "absolute":
            env = {"XDG_CONFIG_HOME": str(base / "xdg"), "ZDOTDIR": str(base / "zdot")}
        elif variant == "relative":
            env = {"XDG_CONFIG_HOME": "xdg", "ZDOTDIR": "zdot"}
        (base / "xdg").mkdir(exist_ok=True)
        (base / "zdot").mkdir(exist_ok=True)
        out = _profile_after(home, shell, env, use_package=use_package)
        # The two runs use different temp roots; compare with the root removed.
        results.append(out.replace(str(base), "BASE"))
    assert results[0] == results[1]
    assert "Added by the Bot Farm Silence Tracker installer" in results[0]
    if variant == "relative":
        # A relative XDG_CONFIG_HOME or ZDOTDIR would land in the working folder (the home).
        assert "home/xdg" not in results[0]
        assert "home/zdot" not in results[0]


# --- L5: the package tells the user the saved platforms were kept ---------------------------


def _cli_wrapper(home: Path) -> None:
    _write_exe(
        home / ".local" / "bin" / TOOL,
        f'#!/bin/sh\nexec "{sys.executable}" -c '
        "\"import sys; from bot_farm_silence_tracker.cli import run; sys.argv[0]='"
        + TOOL
        + '\'; run()" "$@"\n',
    )


def _run_finish(home: Path, platforms: list[str], config: Path) -> subprocess.CompletedProcess[str]:
    choices = home / "Library" / "Caches" / f"{TOOL}-installer" / "platforms"
    choices.parent.mkdir(parents=True, exist_ok=True)
    choices.write_text("".join(f"{p}\n" for p in platforms))
    env = _env(home, BOT_FARM_SILENCE_TRACKER_CONFIG=str(config), PYTHONPATH=str(ROOT / "src"))
    return subprocess.run(
        ["sh", str(SCRIPTS / "finish-postinstall"), "pkg", str(home)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=home,
    )


@needs_sh
@needs_posix
def test_finish_tells_the_user_when_saved_platforms_are_kept(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _cli_wrapper(home)
    config = tmp_path / "config.toml"
    first = _run_finish(home, ["reddit-archive", "bluesky"], config)
    assert first.returncode == 0, first.stderr
    assert "were kept" not in first.stdout
    again = _run_finish(home, ["reddit-archive"], config)
    assert again.returncode == 0, again.stderr
    assert "the platforms you saved earlier were kept" in again.stdout
    assert f"{TOOL} config set-platforms" in again.stdout
    assert "bluesky" in config.read_text()


@needs_sh
def test_finish_has_no_note_when_nothing_was_saved_before(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _cli_wrapper(home)
    result = _run_finish(home, ["reddit-archive"], tmp_path / "config.toml")
    assert result.returncode == 0, result.stderr
    assert "were kept" not in result.stdout


def test_conclusion_page_mentions_the_saved_platforms() -> None:
    text = (MACOS / "resources" / "conclusion.html").read_text(encoding="utf-8")
    assert "config set-platforms" in text
