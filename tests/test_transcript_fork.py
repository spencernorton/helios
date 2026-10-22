"""Fork a Claude conversation from one of your messages (roadmap Phase 2c)."""
from __future__ import annotations

import json

import pytest

from helios.backend.transcript import parse_transcript
from helios.backend.transcript_fork import ForkError, fork_before


def _records(sid):
    return [
        {"type": "user", "uuid": "u1", "sessionId": sid, "message": {"role": "user", "content": "first"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "answer one"}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": sid,
         "message": {"role": "user", "content": [{"type": "text", "text": "second, try again"}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": sid,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "answer two"}]}},
    ]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "src-session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _records("src-session")) + "\n")
    return path


def test_fork_copies_everything_before_the_message_under_a_new_id(source):
    before = source.read_text()
    new_id, new_path, text = fork_before(source, "u2")
    assert text == "second, try again"
    assert new_path.name == f"{new_id}.jsonl" and new_path.parent == source.parent
    records = [json.loads(line) for line in new_path.read_text().splitlines()]
    assert [r["uuid"] for r in records] == ["u1", "a1"]
    assert {r["sessionId"] for r in records} == {new_id}
    assert [t.role for t in parse_transcript(new_path)] == ["user", "assistant"]
    assert source.read_text() == before  # the source is never modified


def test_fork_refuses_assistant_records_first_message_and_unknown_ids(source):
    with pytest.raises(ForkError, match="your own"):
        fork_before(source, "a1")
    with pytest.raises(ForkError, match="first message"):
        fork_before(source, "u1")
    with pytest.raises(ForkError, match="not in this"):
        fork_before(source, "nope")


def test_window_rewind_before_narrows_to_the_checkpoint_before_the_message(monkeypatch):
    pytest.importorskip("gi")
    from types import SimpleNamespace

    from helios.main_window import MainWindow

    calls = []
    window = SimpleNamespace(_toast=calls.append, _show_rewind=lambda before: calls.append(before))
    MainWindow._rewind_before(window, "2026-10-22T12:00:00Z")
    assert calls == [1792670400.0]
    MainWindow._rewind_before(window, "not a time")
    assert "usable time" in calls[-1]


def test_message_menu_only_on_saved_user_messages():
    pytest.importorskip("gi")
    from gi.repository import Gtk

    from helios.backend.transcript import Turn
    from helios.widgets.message_bubble import MessageBubble

    def has_menu(turn):
        header = MessageBubble(turn).get_first_child()
        child = header.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.MenuButton):
                return True
            child = child.get_next_sibling()
        return False

    saved = Turn(role="user", uuid="u2", timestamp="2026-10-22T12:00:00Z", text_parts=["hi"])
    assert has_menu(saved)
    assert not has_menu(Turn(role="user", text_parts=["just sent"]))  # no uuid yet
    assert not has_menu(Turn(role="assistant", uuid="a1", timestamp="2026-10-22T12:00:00Z", text_parts=["x"]))
