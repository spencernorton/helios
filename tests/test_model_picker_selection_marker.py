"""The picker's ✓ follows the selected model, including legacy plain aliases."""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from helios.backend.model_catalog import ModelEntry  # noqa: E402
from helios.widgets.chat_toolbar import ChatToolbar  # noqa: E402

_ENTRIES = [
    ModelEntry("fable[1m]", "Fable 5.2", "Latest"),
    ModelEntry("opus[1m]", "Opus 5.5", "Latest"),
    ModelEntry("haiku", "Haiku 5.5", "Latest"),
    ModelEntry("", "Claude Code default", "Claude Code"),
    ModelEntry("claude-opus-4-8", "Opus 4.8", "Opus"),
]


def _ticked(toolbar: ChatToolbar) -> list[str]:
    toolbar._fill_model_rows(toolbar._model_rows_box, toolbar._model_popover_ref, "")
    out: list[str] = []

    def walk(widget) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button) and (child.get_label() or "").startswith("✓"):
                out.append(child.get_label())
            if isinstance(child, Gtk.Expander):
                # A collapsed expander keeps its rows off the widget tree,
                # and builds them only once opened.
                if child.get_child() is not None:
                    walk(child.get_child())
            else:
                walk(child)
            child = child.get_next_sibling()

    walk(toolbar._model_rows_box)
    return out


@pytest.fixture()
def toolbar():
    if not Gtk.init_check():
        pytest.skip("no display")
    tb = ChatToolbar()
    tb.set_choices(list(_ENTRIES))
    return tb


def test_marker_moves_with_the_selection(toolbar):
    toolbar.set_model("claude-opus-4-8")
    assert _ticked(toolbar) == ["✓ Opus 4.8"]
    toolbar.set_model("fable[1m]")
    assert _ticked(toolbar) == ["✓ Fable 5.2"]


def test_legacy_plain_alias_marks_and_labels_its_1m_row(toolbar):
    toolbar.set_model("opus")
    assert _ticked(toolbar) == ["✓ Opus 5.5"]
    assert toolbar._model_label.get_label() == "Opus 5.5"
