"""Integrated terminal (roadmap Phase 3a)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")


def test_terminal_opens_in_the_chat_folder_and_sends_selection(tmp_path):
    from helios.widgets import terminal_pane

    pane = terminal_pane.TerminalPane(cwd=lambda: str(tmp_path))
    if not pane.available:
        pytest.skip("VTE for GTK4 is not installed")
    sent = []
    pane.connect("send-to-chat", lambda _p, text: sent.append(text))
    term = pane.new_tab()
    assert term._helios_cwd == str(tmp_path)
    assert pane._tabs.get_n_pages() == 1
    pane._send_selection()  # nothing selected: nothing sent
    assert sent == []


def test_missing_folder_falls_back_to_home(monkeypatch, tmp_path):
    from helios.widgets import terminal_pane

    pane = terminal_pane.TerminalPane(cwd=lambda: str(tmp_path / "gone"))
    if not pane.available:
        pytest.skip("VTE for GTK4 is not installed")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pane.new_tab()._helios_cwd == str(tmp_path)


def test_terminal_text_lands_in_the_composer_as_a_code_block():
    from helios.main_window import MainWindow

    state = {"text": "Why does this fail?"}
    composer = SimpleNamespace(
        current_text=lambda: state["text"],
        set_text=lambda t: state.__setitem__("text", t),
        grab_input_focus=lambda: None,
    )
    MainWindow._on_terminal_send(SimpleNamespace(_composer=composer), None, "boom")
    assert state["text"] == "Why does this fail?\n\n```\nboom\n```\n"
