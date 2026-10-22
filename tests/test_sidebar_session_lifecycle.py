"""Sidebar rows for just-started and just-deleted sessions."""
from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.main_window import MainWindow  # noqa: E402
from helios.widgets import session_list as session_list_module  # noqa: E402
from helios.widgets.session_list import SessionList  # noqa: E402


def test_new_session_row_waits_cheaply_until_its_file_lands(tmp_path, monkeypatch):
    """Gemini/Muse/Codex write their transcript seconds after session-started;
    the single 400ms refresh missed it. Waiting is a glob, not a list rebuild."""
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        "helios.main_window.GLib.timeout_add",
        lambda ms, fn, *args: scheduled.append((ms, fn, args)),
    )
    monkeypatch.setattr("helios.main_window._projects.PROJECTS_DIR", tmp_path)
    reloads: list[bool] = []
    selected: list[str] = []
    window = types.SimpleNamespace(
        _destroyed=False,
        _SIDEBAR_LIVE_RETRIES=MainWindow._SIDEBAR_LIVE_RETRIES,
        _sync_live_ids=lambda: None,
        _drv_is_current=lambda _d: True,
        _sessions=types.SimpleNamespace(
            reload=lambda **_kw: reloads.append(True),
            select_session=selected.append,
        ),
    )
    window._refresh_sidebar_for_live = lambda *a: MainWindow._refresh_sidebar_for_live(
        window, *a
    )

    MainWindow._refresh_sidebar_for_live(window, "drv", "s1", 0)
    assert scheduled[-1][0] == 1000 and scheduled[-1][2] == ("drv", "s1", 1)
    assert reloads == [] and selected == []
    (tmp_path / "-repo").mkdir()
    (tmp_path / "-repo" / "s1.jsonl").write_text("{}\n", encoding="utf-8")
    MainWindow._refresh_sidebar_for_live(window, *scheduled[-1][2])
    assert reloads == [True] and selected == ["s1"]
    assert len(scheduled) == 1  # found: no further retries


def _session(tmp_path, sid):
    path = tmp_path / f"{sid}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return types.SimpleNamespace(session_id=sid, path=path, project=object())


def test_delete_stops_drivers_first_and_skips_external_holders(tmp_path, monkeypatch):
    events: list[tuple[str, list[str]]] = []
    ours = _session(tmp_path, "ours")
    theirs = _session(tmp_path, "theirs")
    gone = types.SimpleNamespace(
        session_id="gone", path=tmp_path / "gone.jsonl", project=object()
    )
    monkeypatch.setattr(
        session_list_module,
        "discover_active_session_ids",
        lambda extra=None: {"ours", "theirs"},
    )

    def emit(name, ids):
        # Helios-owned drivers stop while the file still exists.
        events.append((name, list(ids)))
        if name == "sessions-deleting":
            assert ours.path.exists()

    reloads: list[dict] = []
    viewed = types.SimpleNamespace(session=types.SimpleNamespace(session_id="open"))
    fake = types.SimpleNamespace(
        _live_session_ids={"ours"},
        _listbox=types.SimpleNamespace(get_selected_row=lambda: viewed),
        emit=emit,
        _set_select_mode=lambda _on: None,
        reload=lambda **kw: reloads.append(kw),
        get_root=lambda: None,
    )
    SessionList._on_delete_response(fake, None, "delete", [ours, theirs, gone])
    # The open chat survived: never auto-select another row (that re-pointed
    # the chat and swapped the picked Muse model for Claude).
    assert reloads[-1]["preserve_selection"] is True

    assert events == [
        ("sessions-deleting", ["ours", "gone"]),
        ("sessions-deleted", ["ours", "gone"]),
    ]
    assert not ours.path.exists()
    assert theirs.path.exists()  # another process would only rewrite it
