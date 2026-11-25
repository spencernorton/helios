"""Settings → Defaults → Model owns new chats, not the last picker change.

The Settings default and the picker's sticky choice used to share one
`model` key, so the next pick in any chat silently replaced the default and
New chat reopened on whatever was used last (default Fable, new chat Haiku).
"""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios import main_window as main_window_module  # noqa: E402
from helios.backend.ui_state import DEFAULT_MODEL_KEY  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


class _Store(dict):
    def set(self, key, value):
        self[key] = value


class _Project:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.read_only = False


def _window(*, model: str, default: str | None = None, driver=None):
    store = _Store()
    if default is not None:
        store[DEFAULT_MODEL_KEY] = default
    applied: list[str] = []
    shown: list[object] = []
    toasts: list[str] = []
    window = types.SimpleNamespace(
        _ui_state=store,
        _model=model,
        _driver=driver,
        _catalog_entries_by_id={},
        _next_chat=None,
        _composer=types.SimpleNamespace(grab_input_focus=lambda: None),
        _show_fresh_chat_ui=shown.append,
        _toast=toasts.append,
        _permission_mode="default",
        _sync_execution_control=lambda: None,
    )

    def apply(alias, *, quiet=False):
        applied.append(alias)
        window._model = alias

    window._apply_model_choice = apply
    window._start_new_chat = lambda **kw: MainWindow._start_new_chat(window, **kw)
    window._model_for_provider = lambda _p: "gpt-6.2-sol"
    window._sync_provider_toggle = lambda: None
    return window, store, applied, shown, toasts


@pytest.fixture(autouse=True)
def _projects(monkeypatch):
    monkeypatch.setattr(main_window_module, "configured_default_cwd", lambda: "/work")
    monkeypatch.setattr(main_window_module, "ensure_local_project", _Project)


def test_new_chat_opens_on_the_settings_default_not_the_last_pick():
    window, _store, applied, shown, _ = _window(model="haiku", default="fable[1m]")

    MainWindow._start_new_chat(window)

    assert applied == ["fable[1m]"]
    assert window._model == "fable[1m]"
    assert len(shown) == 1


def test_unconfigured_default_keeps_following_the_last_pick():
    window, _store, applied, shown, _ = _window(model="haiku")

    MainWindow._start_new_chat(window)

    assert applied == []
    assert window._model == "haiku"
    assert len(shown) == 1


def test_provider_specific_new_chat_outranks_the_default():
    window, _store, applied, _shown, _ = _window(model="opus[1m]", default="fable[1m]")

    MainWindow._start_new_chat_with_provider(window, "openai")

    assert applied == ["gpt-6.2-sol"]
    assert window._model == "gpt-6.2-sol"


def test_saving_settings_records_the_default_under_its_own_key(monkeypatch):
    monkeypatch.setattr(main_window_module, "sanitize_global_default", lambda m: m)
    window, store, applied, _shown, toasts = _window(model="haiku")

    MainWindow._on_prefs_apply(window, None, "default", "fable[1m]")

    assert store[DEFAULT_MODEL_KEY] == "fable[1m]"
    # A fresh (unbound) chat picks the new default up immediately.
    assert applied == ["fable[1m]"]
    assert toasts and "new chats use" in toasts[0]


def test_saving_settings_leaves_a_running_conversation_on_its_model(monkeypatch):
    monkeypatch.setattr(main_window_module, "sanitize_global_default", lambda m: m)
    window, store, applied, _shown, _ = _window(model="haiku", driver=object())

    MainWindow._on_prefs_apply(window, None, "default", "fable[1m]")

    assert store[DEFAULT_MODEL_KEY] == "fable[1m]"
    assert applied == []
    assert window._model == "haiku"


def test_matching_provider_new_chat_uses_the_saved_default():
    window, _store, applied, shown, _ = _window(model="haiku", default="fable[1m]")
    window._model_for_provider = lambda _provider: "haiku"

    MainWindow._start_new_chat_with_provider(window, "anthropic")

    assert applied == ["fable[1m]"]
    assert len(shown) == 1


@pytest.mark.parametrize("provider", [None, "anthropic"])
def test_claude_cli_default_is_distinct_from_no_default(provider):
    window, _store, applied, shown, _ = _window(model="haiku", default="")
    if provider is None:
        MainWindow._start_new_chat(window)
    else:
        MainWindow._start_new_chat_with_provider(window, provider)

    assert applied == [""]
    assert len(shown) == 1


def test_saving_default_does_not_change_an_unbound_existing_conversation(monkeypatch):
    monkeypatch.setattr(main_window_module, "sanitize_global_default", lambda m: m)
    window, store, applied, _shown, _toasts = _window(model="haiku")
    window._next_chat = types.SimpleNamespace(resume_id="saved-session")

    MainWindow._on_prefs_apply(window, None, "default", "fable[1m]")

    assert store[DEFAULT_MODEL_KEY] == "fable[1m]"
    assert applied == []


def test_settings_model_pick_is_saved_immediately():
    window, store, applied, _shown, toasts = _window(model="claude-opus-5-5", default="fable[1m]")
    window._destroyed = False
    window._driver = object()  # a live chat keeps its own model

    MainWindow._on_default_model_changed(window, None, "gpt-6.2-sol")

    assert store[DEFAULT_MODEL_KEY] == "gpt-6.2-sol"
    assert applied == []
    assert toasts and "New chats use" in toasts[-1]
