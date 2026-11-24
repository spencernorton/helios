"""Model picker widget: one header per provider, your list, then "More".

The Claude section used to draw a header and a divider for every family — four
headers for four rows — plus an "Other" group holding only the settings
sentinel. The rules for what lands where are tested GTK-free in
test_model_picker_layout.py; these tests check the widget draws them and that
hide/add/reorder work from the dropdown itself.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk  # noqa: E402

from helios.backend import model_catalog, ui_state  # noqa: E402
from helios.backend.model_catalog import ModelEntry  # noqa: E402
from helios.widgets.chat_toolbar import ChatToolbar  # noqa: E402

OPENAI = model_catalog.PROVIDER_OPENAI
ANTHROPIC = model_catalog.PROVIDER_ANTHROPIC

_KEYS = (
    ui_state.HIDDEN_MODELS_KEY,
    ui_state.MODEL_ORDER_KEY,
    ui_state.OPENROUTER_PICKER_KEY,
)


def _catalog() -> list[ModelEntry]:
    return [
        ModelEntry("fable[1m]", "Fable 5.2", "Latest"),
        ModelEntry("opus[1m]", "Opus 5.5", "Latest"),
        ModelEntry("sonnet[1m]", "Sonnet 5.5", "Latest"),
        ModelEntry("haiku", "Haiku 5.5", "Latest"),
        model_catalog.CLAUDE_CODE_DEFAULT,
        ModelEntry("claude-fable-5-2", "Fable 5.2", "Fable"),
        ModelEntry("claude-opus-4-8", "Opus 4.8", "Opus"),
        ModelEntry("gpt-6.2-sol", "GPT-6.2 Sol", "OpenAI", OPENAI),
        ModelEntry("gpt-6.2-terra", "GPT-6.2 Terra", "OpenAI", OPENAI),
    ]


@pytest.fixture()
def toolbar():
    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    store = ui_state.store()
    previous = {key: store.get(key, []) for key in _KEYS}
    store.update(**{key: [] for key in _KEYS})
    bar = ChatToolbar()
    bar.set_choices(_catalog())
    bar.set_model("opus[1m]")
    yield bar
    store.update(**previous)


def _fill(bar: ChatToolbar, query: str = "") -> None:
    bar._fill_model_rows(bar._model_rows_box, bar._model_popover_ref, query)


def _drain() -> None:
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def _walk(widget, out: list) -> list:
    child = widget.get_first_child()
    while child is not None:
        out.append(child)
        if isinstance(child, Gtk.Expander):
            if child.get_child() is not None:
                _walk(child.get_child(), out)
        else:
            _walk(child, out)
        child = child.get_next_sibling()
    return out


def _widgets(bar: ChatToolbar) -> list:
    return _walk(bar._model_rows_box, [])


def _texts(bar: ChatToolbar, css: str) -> list[str]:
    return [
        w.get_label() for w in _widgets(bar)
        if isinstance(w, (Gtk.Label, Gtk.Button)) and w.has_css_class(css)
    ]


def _expanders(bar: ChatToolbar) -> list[Gtk.Expander]:
    return [w for w in _widgets(bar) if isinstance(w, Gtk.Expander)]


def test_one_header_per_provider_and_no_other_group(toolbar):
    _fill(toolbar)
    assert _texts(toolbar, "helios-model-section-title") == ["Claude", "GPT"]
    assert _texts(toolbar, "helios-model-pick") == [
        "Fable 5.2", "✓ Opus 5.5", "Sonnet 5.5", "Haiku 5.5",
        "GPT-6.2 Sol", "GPT-6.2 Terra",
    ]
    labels = [
        w.get_label() for w in _widgets(toolbar) if isinstance(w, Gtk.Label)
    ]
    assert "Other" not in labels
    assert [e.get_label() for e in _expanders(toolbar)] == [
        "More Claude models (2)",
    ]


def test_more_builds_its_rows_on_first_open(toolbar):
    _fill(toolbar)
    (more,) = _expanders(toolbar)
    assert more.get_child() is None
    more.set_expanded(True)
    # The pinned twin of the Fable row is not listed; the Claude Code
    # default closes the list.
    assert _texts(toolbar, "helios-model-pick")[4:6] == [
        "Opus 4.8", "Claude Code default",
    ]


def test_hide_and_add_from_the_dropdown(toolbar):
    _fill(toolbar)
    toolbar._on_model_curate(None, "hide", ANTHROPIC, "haiku")
    _drain()
    assert "Haiku 5.5" not in _texts(toolbar, "helios-model-pick")
    assert _expanders(toolbar)[0].get_label() == "More Claude models (3)"

    toolbar._on_model_curate(None, "add", ANTHROPIC, "claude-opus-4-8")
    _drain()
    assert _texts(toolbar, "helios-model-pick")[:4] == [
        "Fable 5.2", "✓ Opus 5.5", "Sonnet 5.5", "Opus 4.8",
    ]


def test_edit_mode_draws_handles_arrows_and_reset(toolbar):
    toolbar._model_edit_btn.set_active(True)
    assert toolbar._model_edit_hint.get_visible()
    # Names are plain labels while editing: a stray click must not switch
    # model (and possibly provider) mid-curation.
    assert _texts(toolbar, "helios-model-pick") == []
    assert _texts(toolbar, "helios-model-name")[:4] == [
        "Fable 5.2", "✓ Opus 5.5", "Sonnet 5.5", "Haiku 5.5",
    ]
    moves = [w for w in _widgets(toolbar) if isinstance(w, Gtk.Button)
             and w.has_css_class("helios-model-move")]
    assert len(moves) == 2 * 6  # up + down on every listed row
    assert [m.get_sensitive() for m in moves[:2]] == [False, True]
    assert _texts(toolbar, "helios-model-section-reset") == []

    toolbar._on_model_curate(None, "down", ANTHROPIC, "fable[1m]")
    _drain()
    assert _texts(toolbar, "helios-model-name")[:2] == ["✓ Opus 5.5", "Fable 5.2"]
    assert _texts(toolbar, "helios-model-section-reset") == ["Reset"]

    toolbar._on_model_curate(None, "reset", ANTHROPIC, "")
    _drain()
    assert _texts(toolbar, "helios-model-name")[:2] == ["Fable 5.2", "✓ Opus 5.5"]



def test_reopening_starts_in_pick_mode(toolbar):
    window = Gtk.Window()
    window.set_child(toolbar)
    window.present()
    try:
        _drain()
        toolbar._model_edit_btn.set_active(True)
        toolbar._model_popover_ref.popup()
        _drain()
        assert not toolbar._model_edit_btn.get_active()
        assert not toolbar._model_edit_hint.get_visible()
        assert _texts(toolbar, "helios-model-pick")[1] == "✓ Opus 5.5"
        toolbar._model_popover_ref.popdown()
    finally:
        window.set_child(None)
        window.destroy()
        _drain()


def test_drop_places_the_row_in_the_gap_it_was_dropped_on(toolbar):
    # Gaps count the list as drawn: dropping Fable (row 0) below Sonnet
    # (row 2) is gap 3, which is index 2 once Fable leaves row 0.
    assert toolbar._drop_model_at(f"{ANTHROPIC}\nfable[1m]", ANTHROPIC, 3)
    _drain()
    sections = toolbar._picker_sections()
    assert sections[0].main_ids == ["opus[1m]", "sonnet[1m]", "fable[1m]", "haiku"]
    # Rows never cross providers.
    assert not toolbar._drop_model_at(f"{ANTHROPIC}\nhaiku", OPENAI, 0)


def test_search_marks_rows_by_tier(toolbar):
    _fill(toolbar, "opus")
    actions = [
        w.get_tooltip_text() for w in _widgets(toolbar)
        if isinstance(w, Gtk.Button) and w.has_css_class("helios-model-row-action")
    ]
    assert _texts(toolbar, "helios-model-pick") == ["✓ Opus 5.5", "Opus 4.8"]
    assert actions == ["Hide from your list", "Add to your list"]
