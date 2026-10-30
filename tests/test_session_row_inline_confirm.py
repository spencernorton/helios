"""A row's trash button confirms in place, never with a modal.

Clicking it morphs the icon into a Delete button and slides Archive in beside
it; only a second, explicit click removes anything. Archive moves the
transcript into the session archive instead of unlinking it.
"""

from __future__ import annotations

import json
import time
import types

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:  # pragma: no cover - host without the GTK4/Adw typelibs
    pytest.skip("Gtk 4.0 / Adw 1 unavailable", allow_module_level=True)

from gi.repository import Adw, Gtk  # noqa: E402

if not Gtk.init_check():  # pragma: no cover - no display to build widgets on
    pytest.skip("no display for GTK widgets", allow_module_level=True)

from helios.backend import model_catalog, session_archiver, session_providers  # noqa: E402
from helios.backend.projects import Project, Session  # noqa: E402
from helios.widgets import session_list as SL  # noqa: E402

Adw.init()


def _session(tmp_path, session_id: str) -> Session:
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"sessionId": session_id, "type": "assistant"}) + "\n",
        encoding="utf-8",
    )
    project = Project(
        dirname="-home-alice-proj",
        cwd="/home/alice/proj",
        path=tmp_path,
        origin="local",
        read_only=False,
    )
    return Session(
        project=project,
        session_id=session_id,
        path=path,
        mtime=time.time(),
        size=path.stat().st_size,
    )


def _row(tmp_path, session_id="s1", **kw):
    calls: list[tuple[str, str]] = []
    row = SL._SessionRow(
        _session(tmp_path, session_id),
        SL.SessionStatus(state=SL.STATE_IDLE, last_response_at=0.0),
        on_delete=lambda s: calls.append(("delete", s.session_id)),
        on_archive=lambda s: calls.append(("archive", s.session_id)),
        **kw,
    )
    return row, calls


def test_trash_reveals_the_pair_in_place_and_removes_nothing(tmp_path):
    shown: list[bool] = []
    row, calls = _row(tmp_path, on_confirm_changed=lambda _r, on: shown.append(on))
    assert row._remove_stack.get_visible_child_name() == "trash"
    assert not row._archive_revealer.get_reveal_child()

    row.reveal_confirm()
    assert row.confirming
    assert row._remove_stack.get_visible_child_name() == "confirm"
    assert row._archive_revealer.get_reveal_child()
    assert row.has_css_class("helios-row-confirming")
    assert calls == [] and row.session.path.exists()

    row.collapse_confirm()
    assert not row.confirming
    assert row._remove_stack.get_visible_child_name() == "trash"
    assert not row._archive_revealer.get_reveal_child()
    assert shown == [True, False]


@pytest.mark.parametrize("button, expected", [("delete", "delete"), ("archive", "archive")])
def test_confirmed_choice_folds_the_row_and_fires_once(tmp_path, button, expected):
    row, calls = _row(tmp_path)
    row.reveal_confirm()
    action = row._on_delete if button == "delete" else row._on_archive
    row._confirm_removal(action)
    row._confirm_removal(action)  # a double click while it folds
    # Unmapped, the revealer settles at once; the backstop timer is a no-op.
    assert not row._body.get_reveal_child()
    assert calls == [(expected, "s1")]
    row.collapse_confirm()  # a removing row stays committed
    assert row.confirming


def test_pool_and_pending_rows_have_no_remove_controls(tmp_path):
    session = _session(tmp_path, "pool")
    session.project.read_only = True
    row = SL._SessionRow(
        session,
        SL.SessionStatus(state=SL.STATE_IDLE, last_response_at=0.0),
        on_delete=lambda _s: pytest.fail("pool rows are view only"),
    )
    row.reveal_confirm()
    assert not row.confirming and row._remove_box is None


def test_opening_one_row_folds_the_other(tmp_path):
    owner = types.SimpleNamespace(_confirming_id="", _rows_by_id={})
    hook = lambda r, on: SL.SessionList._on_row_confirm_changed(owner, r, on)  # noqa: E731
    first, _ = _row(tmp_path, "a", on_confirm_changed=hook)
    second, _ = _row(tmp_path, "b", on_confirm_changed=hook)
    owner._rows_by_id = {"a": first, "b": second}

    first.reveal_confirm()
    second.reveal_confirm()
    assert not first.confirming and second.confirming
    assert owner._confirming_id == "b"
    second.collapse_confirm()
    assert owner._confirming_id == ""


def test_a_retired_row_cannot_close_its_replacement(tmp_path):
    """Pointer leaves an open row, the list rebuilds and reopens a fresh row
    for the same session; the old row's late fold must not orphan it."""
    owner = types.SimpleNamespace(_confirming_id="", _rows_by_id={})
    hook = lambda r, on: SL.SessionList._on_row_confirm_changed(owner, r, on)  # noqa: E731
    old, _ = _row(tmp_path, "a", on_confirm_changed=hook)
    owner._rows_by_id = {"a": old}
    old.reveal_confirm()
    old._arm_leave_fold()
    assert old._leave_source

    fresh, _ = _row(tmp_path, "a", on_confirm_changed=hook)
    owner._rows_by_id = {"a": fresh}
    fresh.reveal_confirm(animate=False)

    old._cancel_leave_fold()  # then fire it by hand (unparented: no fold)
    old._leave_fold()
    old.collapse_confirm()  # even an explicit fold reports for the wrong row
    assert fresh.confirming and owner._confirming_id == "a"


def test_archive_moves_the_transcript_and_keeps_settings(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    monkeypatch.setattr(session_archiver._self, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(SL, "discover_active_session_ids", lambda extra=None: set())
    session = _session(tmp_path, "keep")
    aux = session.path.with_suffix("")
    aux.mkdir()
    session_providers.set_provider("keep", model_catalog.PROVIDER_OPENAI)

    events: list[tuple[str, list[str]]] = []
    reloads: list[dict] = []
    owner = types.SimpleNamespace(
        emit=lambda name, ids: events.append((name, list(ids))),
        _live_session_ids=set(),
        _listbox=types.SimpleNamespace(get_selected_row=lambda: None),
        get_root=lambda: None,
        _set_select_mode=lambda _on: None,
        reload=lambda **kw: reloads.append(kw),
    )
    SL.SessionList._on_delete_response(owner, None, "archive", [session])

    # Drivers still stop first, but observers hear "archived", not "deleted":
    # sessions-deleted wipes execution settings and Work bindings.
    assert events == [("sessions-deleting", ["keep"]), ("sessions-archived", ["keep"])]
    assert not session.path.exists() and not aux.exists()
    dest = archive_root / session.project.dirname
    assert (dest / "keep.jsonl").exists() and (dest / "keep").is_dir()
    assert reloads


def test_cancelled_response_removes_nothing(tmp_path):
    session = _session(tmp_path, "stay")
    owner = types.SimpleNamespace(emit=lambda *_a: pytest.fail("nothing to emit"))
    SL.SessionList._on_delete_response(owner, None, "cancel", [session])
    assert session.path.exists()
