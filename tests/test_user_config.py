"""The user config file, the ``config`` command and ``scan``'s platform precedence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker import config as cfg
from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY

from .golden.generate import NOW_ARG

runner = CliRunner()


@pytest.fixture
def config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "cfgdir" / "config.toml"
    monkeypatch.setenv(cfg.CONFIG_ENV, str(path))
    return path


# ---------------------------------------------------------------- paths


def test_posix_path_is_under_dot_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "ignored"))
    path = cfg.default_config_path(windows=False, environ={"APPDATA": "ignored"})
    assert path == Path("~/.config").expanduser() / "bot-farm-silence-tracker" / "config.toml"


def test_windows_path_uses_appdata() -> None:
    path = cfg.default_config_path(windows=True, environ={"APPDATA": r"C:\Users\A\AppData\Roaming"})
    assert path == Path(r"C:\Users\A\AppData\Roaming") / "bot-farm-silence-tracker" / "config.toml"


def test_windows_without_appdata_falls_back_to_the_posix_style_path() -> None:
    path = cfg.default_config_path(windows=True, environ={})
    assert path == Path("~/.config").expanduser() / "bot-farm-silence-tracker" / "config.toml"


def test_the_environment_variable_overrides_the_path(tmp_path: Path) -> None:
    where = tmp_path / "x.toml"
    for windows in (True, False):
        found = cfg.default_config_path(windows=windows, environ={cfg.CONFIG_ENV: str(where)})
        assert found == where


def test_xdg_config_home_replaces_dot_config(tmp_path: Path) -> None:
    for windows in (False, True):
        path = cfg.default_config_path(windows=windows, environ={"XDG_CONFIG_HOME": str(tmp_path)})
        assert path == tmp_path / "bot-farm-silence-tracker" / "config.toml"


def test_a_relative_xdg_config_home_is_ignored_and_appdata_still_wins(tmp_path: Path) -> None:
    home_style = Path("~/.config").expanduser() / "bot-farm-silence-tracker" / "config.toml"
    assert cfg.default_config_path(windows=False, environ={"XDG_CONFIG_HOME": "rel"}) == home_style
    path = cfg.default_config_path(
        windows=True, environ={"XDG_CONFIG_HOME": str(tmp_path), "APPDATA": str(tmp_path / "ad")}
    )
    assert path == tmp_path / "ad" / "bot-farm-silence-tracker" / "config.toml"


def test_the_config_override_beats_xdg(tmp_path: Path) -> None:
    where = tmp_path / "x.toml"
    env = {cfg.CONFIG_ENV: str(where), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert cfg.default_config_path(windows=False, environ=env) == where


# ---------------------------------------------------------------- read and write


def test_a_missing_file_is_an_empty_config(config_path: Path) -> None:
    loaded = cfg.load_config(config_path)
    assert not loaded.exists
    assert loaded.platforms is None
    assert loaded.default == "reddit-archive"
    assert loaded.enabled == ("reddit-archive",)


def test_write_then_read_round_trips(config_path: Path) -> None:
    cfg.write_config(config_path, ["reddit-archive", "bluesky"], "bluesky")
    assert config_path.read_text(encoding="utf-8") == (
        "# Written by `bot-farm-silence-tracker config`. Edit it there or by hand.\n"
        'platforms = ["reddit-archive", "bluesky"]\n'
        'default_platform = "bluesky"\n'
    )
    loaded = cfg.load_config(config_path)
    assert loaded.exists
    assert loaded.platforms == ("reddit-archive", "bluesky")
    assert loaded.default == "bluesky"
    assert [p.name for p in config_path.parent.iterdir()] == ["config.toml"]


def test_set_platforms_keeps_a_default_that_is_still_listed(config_path: Path) -> None:
    cfg.write_config(config_path, ["reddit-archive", "x"], "x")
    saved = cfg.set_platforms(config_path, ("bluesky", "x"))
    assert saved.default == "x"


def test_set_platforms_moves_the_default_to_the_first_listed(config_path: Path) -> None:
    saved = cfg.set_platforms(config_path, ("bluesky",))
    assert saved.platforms == ("bluesky",)
    assert saved.default == "bluesky"


def test_set_default_enables_the_platform(config_path: Path) -> None:
    saved = cfg.set_default(config_path, "bluesky")
    assert saved.platforms == ("reddit-archive", "bluesky")
    assert saved.default == "bluesky"


@pytest.mark.parametrize(
    "text",
    [
        "platforms = [",
        'platforms = "reddit-archive"',
        "platforms = [1]",
        "default_platform = 3",
    ],
)
def test_a_bad_file_raises(config_path: Path, text: str) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(cfg.ConfigError, match=str(config_path.name)):
        cfg.load_config(config_path)


def test_unknown_platform_names_are_ignored_with_a_warning(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text('platforms = ["myspace", "Bluesky"]\n', encoding="utf-8")
    loaded = cfg.load_config(config_path)
    assert loaded.platforms == ("bluesky",)
    assert len(loaded.warnings) == 1
    assert "myspace" in loaded.warnings[0]


def test_an_unknown_default_falls_back_to_reddit_archive_with_a_warning(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text('default_platform = "myspace"\n', encoding="utf-8")
    loaded = cfg.load_config(config_path)
    assert loaded.default == "reddit-archive"
    assert "myspace" in loaded.warnings[0]


def test_an_empty_platform_list_counts_as_unset(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = []\n", encoding="utf-8")
    loaded = cfg.load_config(config_path)
    assert loaded.platforms is None
    assert loaded.warnings == ()
    assert loaded.enabled == ("reddit-archive",)
    shown = runner.invoke(app, ["config", "show"])
    assert shown.exit_code == 0
    assert "platforms: reddit-archive" in shown.output


def test_a_list_of_only_unknown_names_counts_as_unset(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text('platforms = ["myspace"]\n', encoding="utf-8")
    loaded = cfg.load_config(config_path)
    assert loaded.platforms is None
    assert loaded.warnings


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_writing_through_a_symlinked_config_keeps_the_link(
    config_path: Path, tmp_path: Path
) -> None:
    real = tmp_path / "dotfiles" / "real.toml"
    real.parent.mkdir()
    real.write_text('platforms = ["x"]\ndefault_platform = "x"\n', encoding="utf-8")
    config_path.parent.mkdir(parents=True)
    config_path.symlink_to(real)
    cfg.set_platforms(config_path, ("bluesky",))
    assert config_path.is_symlink()
    assert cfg.load_config(real).platforms == ("bluesky",)


def test_set_platforms_replaces_a_broken_file_and_says_so(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = [", encoding="utf-8")
    saved = cfg.set_platforms(config_path, ("bluesky",))
    assert saved.platforms == ("bluesky",)
    assert "replaced" in saved.warnings[0]
    assert cfg.load_config(config_path).warnings == ()


def test_parse_platforms_validates_and_dedupes() -> None:
    assert cfg.parse_platforms(" Bluesky, reddit-archive ,bluesky,") == (
        "bluesky",
        "reddit-archive",
    )
    for bad in ("", " , ", "reddit-archive,myspace"):
        with pytest.raises(cfg.ConfigError):
            cfg.parse_platforms(bad)


def test_every_registered_platform_is_accepted() -> None:
    assert cfg.parse_platforms(",".join(SOURCE_REGISTRY)) == tuple(SOURCE_REGISTRY)


# ---------------------------------------------------------------- the config command


def test_config_show_without_a_file(config_path: Path) -> None:
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"config file: {config_path} (not created yet)",
        "platforms: reddit-archive",
        "default_platform: reddit-archive",
    ]


def test_config_set_platforms_writes_and_show_reads(config_path: Path) -> None:
    result = runner.invoke(app, ["config", "set-platforms", "reddit-archive,bluesky"])
    assert result.exit_code == 0, result.output
    assert cfg.load_config(config_path).platforms == ("reddit-archive", "bluesky")
    shown = runner.invoke(app, ["config", "show"])
    assert f"config file: {config_path}\n" in shown.output
    assert "platforms: reddit-archive, bluesky" in shown.output


def test_config_set_platforms_rejects_unknown_names_and_writes_nothing(config_path: Path) -> None:
    result = runner.invoke(app, ["config", "set-platforms", "reddit-archive,myspace"])
    assert result.exit_code == 2
    assert "unknown platform 'myspace'" in result.output
    assert not config_path.exists()


def test_config_set_platforms_if_unset_keeps_an_existing_choice(config_path: Path) -> None:
    cfg.write_config(config_path, ["x"], "x")
    result = runner.invoke(app, ["config", "set-platforms", "--if-unset", "reddit-archive"])
    assert result.exit_code == 0, result.output
    assert cfg.load_config(config_path).platforms == ("x",)
    config_path.unlink()
    result = runner.invoke(app, ["config", "set-platforms", "--if-unset", "reddit-archive"])
    assert cfg.load_config(config_path).platforms == ("reddit-archive",)


def test_config_set_default(config_path: Path) -> None:
    result = runner.invoke(app, ["config", "set-default", "bluesky"])
    assert result.exit_code == 0, result.output
    loaded = cfg.load_config(config_path)
    assert (loaded.platforms, loaded.default) == (("reddit-archive", "bluesky"), "bluesky")
    bad = runner.invoke(app, ["config", "set-default", "myspace"])
    assert bad.exit_code == 2
    assert cfg.load_config(config_path).default == "bluesky"


def test_config_set_platforms_overwrites_a_broken_file_with_one_warning(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = [", encoding="utf-8")
    result = runner.invoke(app, ["config", "set-platforms", "bluesky"])
    assert result.exit_code == 0, result.output
    assert result.output.count("warning: replaced the unusable config file") == 1
    assert cfg.load_config(config_path).platforms == ("bluesky",)


def test_config_set_default_overwrites_a_broken_file(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b"\xff\xfe not toml")
    result = runner.invoke(app, ["config", "set-default", "bluesky"])
    assert result.exit_code == 0, result.output
    assert "warning: replaced" in result.output
    assert cfg.load_config(config_path).default == "bluesky"


def test_if_unset_leaves_a_broken_file_alone_and_says_what_to_do(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = [", encoding="utf-8")
    result = runner.invoke(app, ["config", "set-platforms", "--if-unset", "bluesky"])
    assert result.exit_code == 0, result.output
    assert config_path.read_text(encoding="utf-8") == "platforms = ["
    assert "Edit or delete" in result.output
    assert str(config_path) in result.output


def test_if_unset_writes_when_the_list_is_empty(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = []\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "set-platforms", "--if-unset", "bluesky"])
    assert result.exit_code == 0, result.output
    assert "Kept" not in result.output
    assert cfg.load_config(config_path).platforms == ("bluesky",)


def test_config_show_warns_about_unknown_names(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text('platforms = ["myspace", "x"]\n', encoding="utf-8")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert "warning:" in result.output
    assert "platforms: x" in result.output


def test_config_show_reports_a_broken_file(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = [", encoding="utf-8")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 2
    assert "not valid TOML" in result.output


# ---------------------------------------------------------------- scan precedence


def _scan(tmp_path: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        ["scan", *extra, "--n", "20", "--seed", "1", "--now", NOW_ARG,
         "--db", str(tmp_path / "c.db"), "--out", str(tmp_path / "o.csv")],
    )  # fmt: skip


def test_scan_without_a_platform_uses_reddit_archive(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        "bot_farm_silence_tracker.cli.load_seeds", lambda p, *a: seen.append(p) or _stop()
    )
    _scan(tmp_path)
    assert seen == ["reddit-archive"]


def _stop() -> None:
    raise RuntimeError("stop after the platform is known")


def test_scan_uses_the_config_default(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg.write_config(config_path, ["reddit-archive", "fake"], "fake")
    result = _scan(tmp_path)
    assert result.exit_code == 0, result.output
    assert "(fake, " in result.output


def test_an_explicit_platform_beats_the_config(config_path: Path, tmp_path: Path) -> None:
    cfg.write_config(config_path, ["bluesky"], "bluesky")
    result = _scan(tmp_path, "--platform", "fake")
    assert result.exit_code == 0, result.output
    assert "(fake, " in result.output


def test_scanning_a_platform_that_is_not_enabled_prints_one_note(
    config_path: Path, tmp_path: Path
) -> None:
    cfg.write_config(config_path, ["reddit-archive"], "reddit-archive")
    result = _scan(tmp_path, "--platform", "fake")
    assert result.exit_code == 0, result.output
    notes = [line for line in result.output.splitlines() if line.startswith("note: fake is not")]
    assert len(notes) == 1


def test_no_note_without_a_config_file_or_for_an_enabled_platform(
    config_path: Path, tmp_path: Path
) -> None:
    assert "note:" not in _scan(tmp_path, "--platform", "fake").output
    cfg.write_config(config_path, ["fake"], "fake")
    assert "note:" not in _scan(tmp_path, "--platform", "fake").output


def test_an_explicit_platform_still_runs_with_a_broken_config(
    config_path: Path, tmp_path: Path
) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = [", encoding="utf-8")
    assert _scan(tmp_path, "--platform", "fake").exit_code == 0
    broken = _scan(tmp_path)
    assert broken.exit_code == 2
    assert "not valid TOML" in broken.output


def test_scan_with_an_unknown_name_in_the_config_still_runs(
    config_path: Path, tmp_path: Path
) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text('platforms = ["myspace"]\n', encoding="utf-8")
    result = _scan(tmp_path, "--platform", "fake")
    assert result.exit_code == 0, result.output
    assert "warning:" in result.output
    assert "note:" not in result.output


def test_an_empty_platform_list_prints_no_note(config_path: Path, tmp_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = []\n", encoding="utf-8")
    result = _scan(tmp_path, "--platform", "fake")
    assert result.exit_code == 0, result.output
    assert "note:" not in result.output


def _partial_fake_run(tmp_path: Path) -> int:
    from bot_farm_silence_tracker.storage import Storage

    assert _scan(tmp_path, "--platform", "fake").exit_code == 0
    with Storage(tmp_path / "c.db") as storage:
        return max(r["run_id"] for r in (storage.get_run(i) for i in range(1, 5)) if r)


def test_a_resume_without_a_platform_uses_the_stored_platform(
    config_path: Path, tmp_path: Path
) -> None:
    run_id = _partial_fake_run(tmp_path)
    result = runner.invoke(
        app,
        ["scan", "--from-run", str(run_id), "--now", NOW_ARG, "--db", str(tmp_path / "c.db"),
         "--out", str(tmp_path / "r.csv")],
    )  # fmt: skip
    assert "run is a" not in result.output
    assert "(fake, " in result.output, result.output


def test_a_resume_ignores_a_conflicting_config_default_and_prints_no_note(
    config_path: Path, tmp_path: Path
) -> None:
    run_id = _partial_fake_run(tmp_path)
    cfg.write_config(config_path, ["bluesky"], "bluesky")
    result = runner.invoke(
        app,
        ["scan", "--from-run", str(run_id), "--now", NOW_ARG, "--db", str(tmp_path / "c.db"),
         "--out", str(tmp_path / "r.csv")],
    )  # fmt: skip
    assert "cannot be resumed" not in result.output
    assert "note:" not in result.output
    assert "(fake, " in result.output, result.output


def test_a_resume_does_not_read_a_broken_config(config_path: Path, tmp_path: Path) -> None:
    run_id = _partial_fake_run(tmp_path)
    config_path.parent.mkdir(parents=True)
    config_path.write_text("platforms = [", encoding="utf-8")
    result = runner.invoke(
        app,
        ["scan", "--from-run", str(run_id), "--now", NOW_ARG, "--db", str(tmp_path / "c.db"),
         "--out", str(tmp_path / "r.csv")],
    )  # fmt: skip
    assert "not valid TOML" not in result.output


def test_an_unknown_platform_is_still_rejected(config_path: Path, tmp_path: Path) -> None:
    result = _scan(tmp_path, "--platform", "myspace")
    assert result.exit_code == 2
    assert "unknown platform 'myspace'" in result.output


def test_the_golden_census_is_unchanged_with_a_config_that_enables_the_platform(
    config_path: Path, tmp_path: Path
) -> None:
    from .golden.generate import generate, golden_names

    cfg.write_config(config_path, ["fake"], "fake")
    out = tmp_path / "golden"
    out.mkdir()
    generate(out)
    golden = Path(__file__).parent / "golden" / "legacy"  # generate() writes the legacy set
    for name in golden_names():
        assert (out / name).read_bytes() == (golden / name).read_bytes(), name
