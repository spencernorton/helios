"""A new session reaches the sidebar immediately, not when its file lands.

Before this, the sidebar was purely transcript-file-driven: New Chat staged
nothing in the list, and after session-started MainWindow polled ~1s apart
for ``<session_id>.jsonl`` — which some providers write only once the first
turn is accepted. A session could stay invisible for seconds to minutes, so
several sessions could not be launched in quick succession and told apart.

The pending rows are in-memory placeholders: staged (New Chat clicked) and
live (provider id known). Discovery supersedes a live placeholder the moment
its file exists; a driver leaving the live set without a file prunes it.
"""

from __future__ import annotations

import json
import types

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:  # pragma: no cover - host without the GTK4/Adw typelibs
    pytest.skip("Gtk 4.0 / Adw 1 unavailable", allow_module_level=True)

from gi.repository import Adw  # noqa: E402

import helios.backend.projects as projects_module  # noqa: E402
from helios.backend.projects import Project  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402
from helios.widgets.session_list import PENDING_STAGED_ID, SessionList  # noqa: E402

Adw.init()


@pytest.fixture()
def sidebar():
    widget = SessionList()
    widget._local_sessions = []
    widget._pool_sessions = []
    try:
        yield widget
    finally:
        widget.shutdown()


def _project(tmp_path, cwd: str = "/repo") -> Project:
    path = tmp_path / "proj"
    path.mkdir(parents=True, exist_ok=True)
    return Project(dirname="-repo", cwd=cwd, path=path, origin="local")


def test_live_session_shows_immediately_without_a_transcript_file(sidebar, tmp_path):
    project = _project(tmp_path)

    assert sidebar.show_pending_session("s-live", project=project, title="fix it")

    assert not (project.path / "s-live.jsonl").exists(), "precondition: no file yet"
    row = sidebar._rows_by_id["s-live"]
    assert row.session.pending is True
    assert row.session.title == "fix it"


def test_staged_chat_shows_immediately_and_the_live_row_replaces_it(sidebar, tmp_path):
    project = _project(tmp_path)

    assert sidebar.show_staged_session(project)
    assert PENDING_STAGED_ID in sidebar._rows_by_id

    assert sidebar.show_staged_session(project, title="repair the parser")
    assert sidebar.show_pending_session("s-live", project=project)

    row = sidebar._rows_by_id["s-live"]
    assert row.session.title == "repair the parser", (
        "the prompt typed at send time distinguishes just-launched sessions"
    )
    # Selecting the started session (MainWindow, on session-started) is what
    # retires the staged placeholder for that same chat.
    assert sidebar.clear_staged_session()
    assert PENDING_STAGED_ID not in sidebar._rows_by_id


def test_several_sessions_can_be_launched_in_quick_succession(sidebar, tmp_path):
    """The reported workflow: launch, New Chat, launch — every session is
    listed and told apart by its prompt before any transcript exists."""
    project = _project(tmp_path)

    sidebar.show_staged_session(project, title="first task")
    sidebar.show_pending_session("s-1", project=project)
    sidebar.clear_staged_session()
    sidebar.show_staged_session(project, title="second task")
    sidebar.show_pending_session("s-2", project=project)
    sidebar.clear_staged_session()

    assert sidebar._rows_by_id["s-1"].session.title == "first task"
    assert sidebar._rows_by_id["s-2"].session.title == "second task"
    assert not (project.path / "s-1.jsonl").exists()
    assert not (project.path / "s-2.jsonl").exists()


def test_a_background_start_does_not_erase_a_newer_staged_chat(sidebar, tmp_path):
    """Launch A, click New Chat (staged B), then A's id arrives: B stays."""
    project = _project(tmp_path)
    sidebar.show_staged_session(project)

    sidebar.show_pending_session("s-background", project=project)

    assert PENDING_STAGED_ID in sidebar._rows_by_id
    assert "s-background" in sidebar._rows_by_id


def test_staged_row_never_emits_a_selection(sidebar, tmp_path):
    emitted: list = []
    sidebar.connect("session-selected", lambda _w, s: emitted.append(s))
    sidebar.show_staged_session(_project(tmp_path))

    sidebar._on_row_selected(sidebar._listbox, sidebar._rows_by_id[PENDING_STAGED_ID])

    assert emitted == []


def test_pending_row_survives_reload_until_discovery_supersedes_it(
    sidebar, tmp_path, monkeypatch
):
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "-repo"
    proj_dir.mkdir(parents=True)
    monkeypatch.setattr(projects_module, "PROJECTS_DIR", projects_dir)
    project = Project(dirname="-repo", cwd="/repo", path=proj_dir, origin="local")
    sidebar.show_pending_session("s-live", project=project, title="fix it")

    sidebar.reload(preserve_selection=True, rescan_pool=False)
    assert sidebar._rows_by_id["s-live"].session.pending is True

    line = json.dumps(
        {
            "type": "user",
            "cwd": "/repo",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "fix it"}],
            },
        }
    )
    (proj_dir / "s-live.jsonl").write_text(line + "\n", encoding="utf-8")
    sidebar.reload(preserve_selection=True, rescan_pool=False)

    assert sidebar.pending_session_ids() == set()
    row = sidebar._rows_by_id["s-live"]
    assert row.session.pending is False
    assert row.session.title == "fix it"


def test_pending_row_is_pruned_when_its_driver_leaves_the_live_set(sidebar, tmp_path):
    sidebar.show_pending_session("s-live", project=_project(tmp_path))

    sidebar.set_live_session_ids({"s-live"})
    assert "s-live" in sidebar._rows_by_id

    sidebar.set_live_session_ids(set())
    assert "s-live" not in sidebar._rows_by_id
    assert sidebar.pending_session_ids() == set()


def test_pending_rows_are_not_deletable(sidebar, tmp_path):
    project = _project(tmp_path)
    sidebar.show_pending_session("s-live", project=project)

    row = sidebar._rows_by_id["s-live"]
    sidebar._listbox.select_row(row)
    assert sidebar._deletable_selected_sessions() == [], (
        "a pending row has no transcript file for delete to remove"
    )


def test_mainwindow_sidebar_wrappers_delegate_and_tolerate_old_fakes():
    calls: list = []
    sessions = types.SimpleNamespace(
        show_pending_session=lambda sid, cwd="": calls.append((sid, cwd)),
        show_staged_session=lambda project, title="": calls.append(("staged", title)),
        clear_staged_session=lambda: calls.append(("clear",)),
    )
    win = types.SimpleNamespace(_sessions=sessions)
    MainWindow._sidebar_show_pending(win, "s9", "/repo")
    MainWindow._sidebar_show_staged(win, object(), title="hello")
    MainWindow._sidebar_clear_staged(win)
    assert calls == [("s9", "/repo"), ("staged", "hello"), ("clear",)]

    bare = types.SimpleNamespace(_sessions=types.SimpleNamespace())
    MainWindow._sidebar_show_pending(bare, "s9", "/repo")  # must not raise
    MainWindow._sidebar_show_staged(bare, object())
    MainWindow._sidebar_clear_staged(bare)


def test_background_session_started_still_gets_its_sidebar_row(monkeypatch):
    """Launch A, stage chat B, then A's session-started lands: A must
    appear immediately even though its driver is no longer visible."""
    import helios.main_window as mw

    scheduled: list = []
    monkeypatch.setattr(
        mw.GLib, "timeout_add", lambda ms, fn, *a: scheduled.append((ms, a)) or 0
    )
    shown: list = []
    drv = types.SimpleNamespace(
        session_id="",
        _helios_work_id="",
        _helios_expected_resume_id="",
        _helios_goal_session_id_hint="",
        init_tools=[],
        init_mcp_servers=[],
        provider="anthropic",
    )
    manager = types.SimpleNamespace(
        starting=drv,
        register_started=lambda d, sid: setattr(d, "session_id", sid),
    )
    win = types.SimpleNamespace(
        _destroyed=False,
        _driver_manager=manager,
        _work_coordinator=None,
        _next_chat=None,
        _driver=drv,
        _sessions=types.SimpleNamespace(
            show_pending_session=lambda sid, cwd="": shown.append((sid, cwd))
        ),
        _bind_pending_goal_to_session=lambda *_a: None,
        _refresh_sidebar_for_live=lambda *_a: False,
        _drv_is_current=lambda _d: False,
        _driver_provider=lambda _d: "anthropic",
    )

    MainWindow._on_session_started(win, drv, "s-bg", "/repo", "opus")

    assert shown == [("s-bg", "/repo")]
    assert scheduled and scheduled[-1][1][-1] == 0  # reconciliation timer armed
