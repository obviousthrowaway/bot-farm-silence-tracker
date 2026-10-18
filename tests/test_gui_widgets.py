"""Smoke tests of the desktop app's window (need Tk, CustomTkinter and a display).

One hidden window is shared by the whole test session: Tk does not survive several
root windows in one process (under Tk 9.0 on macOS a second root crashes it), and
the app refuses a second one. The two tests that need a fresh process (the
``--selftest`` entry point and the second-window regression) run in a subprocess.

They are skipped when Tk, CustomTkinter or a display is missing, unless
BOT_FARM_SILENCE_TRACKER_REQUIRE_DISPLAY=1 (set in CI, where Linux runs them under
xvfb), which turns a skip into a failure.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from typing import Any

import pytest

REQUIRED = os.environ.get("BOT_FARM_SILENCE_TRACKER_REQUIRE_DISPLAY") == "1"


def _skip(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"the GUI smoke test cannot run: {reason}")
    pytest.skip(reason)


def _check_installed() -> None:
    for module in ("tkinter", "_tkinter", "customtkinter"):
        if importlib.util.find_spec(module) is None:
            _skip(f"{module} is not installed")


@pytest.fixture(scope="session")
def shared_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Any, int]]:
    """The session's one hidden window, on a small offline sample scan."""
    _check_installed()
    import tkinter

    from bot_farm_silence_tracker.gui import view

    model, run_id = view.sample_run(tmp_path_factory.mktemp("gui"))
    try:
        app = view.App(model, hidden=True)
    except tkinter.TclError as exc:
        _skip(f"no display ({exc})")
    try:
        yield app, run_id
    finally:
        app.close()


def test_every_screen_and_view_renders(shared_app: tuple[Any, int]) -> None:
    from bot_farm_silence_tracker.gui import view

    app, run_id = shared_app
    view.exercise(app, run_id)
    results = app.screens["results"]
    assert results.results is not None and results.results.run_id == run_id
    assert results.graph.find_all() and results.timeline.find_all()
    assert len(results.table.get_children()) == len(results.shown) > 0


def test_the_timeline_draws_the_markers_of_an_event_with_markers(
    shared_app: tuple[Any, int], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A run of the fixture event with markers: one line per row for the cutoff and
    each marker (the approximate one dashed), a legend, and the marker notes."""
    from bot_farm_silence_tracker.gui import view

    app, run_id = shared_app
    model, strikes_run = view.sample_run(tmp_path_factory.mktemp("strikes"), "fixture-strikes")
    results = app.screens["results"]
    old_db = app.model.db
    app.model.db = model.db
    try:
        results.select_run(strikes_run)
        assert results.results is not None and len(results.results.lines) == 3
        view.check_timeline_lines(results)
        canvas = results.timeline
        dashed = [item for item in canvas.find_withtag("approx") if canvas.itemcget(item, "dash")]
        assert dashed and len(dashed) == len(canvas.find_withtag("approx"))
        solid = canvas.find_withtag("cutoff")
        assert solid and not any(canvas.itemcget(i, "dash") for i in solid)
        assert "The second fixture strike." in results.details.get("1.0", "end")
        assert results.filter_menus["marker"].cget("values")[0] == "All"
    finally:
        app.model.db = old_db
        results.select_run(run_id)


def test_minimum_size(shared_app: tuple[Any, int]) -> None:
    import tkinter

    app, _ = shared_app
    # CustomTkinter scales the minimum by the display's scaling factor (1 or more).
    width, height = tkinter.Tk.wm_minsize(app)
    assert width >= 900 and height >= 650


def test_session_credentials_are_forgotten(shared_app: tuple[Any, int]) -> None:
    app, _ = shared_app
    env = "BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN"
    scan = app.screens["scan"]
    scan.set_platform("x")
    app.update()
    entry = scan.cred_entries[env]
    entry.insert(0, "secret-value")
    app.model.credentials.set(env, entry.get())
    app.forget_session()
    assert app.model.credentials.snapshot() == {}
    assert entry.get() == ""
    scan.set_platform("fake")


def test_pasted_credentials_are_read_at_start_and_survive_a_platform_switch(
    shared_app: tuple[Any, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = shared_app
    env = "BOT_FARM_SILENCE_TRACKER_BSKY_APP_PASSWORD"
    scan = app.screens["scan"]
    scan.set_platform("bluesky")
    app.update()
    # A middle-click paste or a password manager changes the text without a key event.
    scan.cred_entries[env].insert(0, "pasted-value")
    assert not app.model.credentials.has(env)

    # Rebuilding the fields for another platform and back shows the value again.
    scan.sync_credentials()
    scan.set_platform("fake")
    app.update()
    scan.set_platform("bluesky")
    app.update()
    assert scan.cred_entries[env].get() == "pasted-value"
    assert app.model.credentials.snapshot()[env] == "pasted-value"

    # start() and resume() read the fields first, so nothing typed is ever missed.
    started: list[Any] = []
    monkeypatch.setattr(app, "start_job", started.append)
    app.model.credentials.clear()
    scan.start()
    assert len(started) == 1
    assert started[0]._credentials[env] == "pasted-value"
    assert "pasted-value" not in repr(started[0]._credentials)
    app.model.credentials.clear()
    run = app.screens["run"]
    run.runs = {"r": {"platform": "bluesky", "run_id": 1}}
    run.resume_menu.set("r")
    run.resume()
    assert len(started) == 2 and started[1]._credentials[env] == "pasted-value"
    app.forget_session()
    assert scan.cred_entries[env].get() == ""
    scan.set_platform("fake")


def test_export_warns_before_it_writes(
    shared_app: tuple[Any, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from bot_farm_silence_tracker.gui import presenter as P
    from bot_farm_silence_tracker.gui import view

    app, run_id = shared_app
    results = app.screens["results"]
    app.show("results")
    results.select_run(run_id)
    results.reload()
    asked: list[Any] = []
    target = tmp_path / "out.csv"

    def ask(**kwargs: Any) -> str:
        asked.append(kwargs)
        return str(target)

    monkeypatch.setattr(view.filedialog, "asksaveasfilename", ask)
    results.export()  # First press: warning only.
    assert not asked and not target.exists() and results.export_armed
    assert results.message.cget("text") == P.EXPORT_WARNING
    results.cancel_export()  # Cancel writes nothing and re-arms the warning.
    assert not results.export_armed and not target.exists() and not asked
    results.export()
    results.export()  # Second press: the file dialog, then the file.
    assert len(asked) == 1 and target.exists() and not results.export_armed


def test_a_second_window_is_refused(shared_app: tuple[Any, int]) -> None:
    from bot_farm_silence_tracker.gui import view

    with pytest.raises(RuntimeError, match="one window per process"):
        view.App(hidden=True)


def _python(code: str) -> subprocess.CompletedProcess[str]:
    _check_installed()
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if done.returncode != 0 and "could not open a window" in done.stderr:
        _skip(f"no display: {done.stderr.strip()[:300]}")
    return done


def test_closing_and_reopening_fails_cleanly_instead_of_crashing() -> None:
    """Regression: a second root after a closed one crashed Tk 9.0 on macOS
    (SIGSEGV in showRootWindow, from update() inside a widget's constructor)."""
    done = _python(
        """
        import tkinter
        from bot_farm_silence_tracker.gui import view
        try:
            first = view.App(hidden=True)
        except tkinter.TclError as exc:
            raise SystemExit(f"The desktop app could not open a window ({exc})")
        first.update()
        first.close()
        try:
            view.App(hidden=True)
        except RuntimeError as exc:
            print("refused:", exc)
        """
    )
    assert done.returncode == 0, done.stderr[-3000:]  # A crash gives a negative code.
    assert "refused: The desktop app opens one window per process" in done.stdout


def test_selftest_entry_point_exits_0() -> None:
    done = _python(
        """
        from bot_farm_silence_tracker.gui import launch
        raise SystemExit(launch(selftest=True))
        """
    )
    assert done.returncode == 0, done.stderr[-3000:]
    assert "gui selftest ok" in done.stdout
    assert "Tk " in done.stdout
