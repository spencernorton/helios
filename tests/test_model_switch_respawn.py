"""Same-provider model/reasoning changes on drivers that cannot switch in place.

Only Claude has `set_model`; GPT/Gemini/Muse/OpenRouter kept answering on the
old model until the idle reaper closed the process, and a Gemini/Muse
reasoning change toasted a raw AttributeError.
"""
from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.main_window import MainWindow  # noqa: E402


class _GeminiLike:
    __hash__ = object.__hash__
    provider = "google"

    def __init__(self) -> None:
        self.is_running = True
        self.is_accepting_input = True
        self.is_busy = False
        self.ended = 0
        self._helios_requested_model = "gemini-3.8-flash"

    def end_input(self) -> None:
        self.ended += 1
        self.is_accepting_input = False


def _window(driver, toasts):
    window = types.SimpleNamespace(
        _model="gemini-3.8-flash",
        _ui_state=types.SimpleNamespace(set=lambda *_a: None),
        _provider_models={},
        _pending_session_provider_adoption="",
        _chat_toolbar=types.SimpleNamespace(
            set_model=lambda _alias: None,
            set_context_model=lambda _alias: None,
        ),
        _sync_provider_toggle=lambda: None,
        _sync_assistant_labels=lambda: None,
        _sync_effort_sensitivity=lambda: None,
        _refresh_openrouter_credits=lambda: None,
        _toast=lambda text, **_kw: toasts.append(text),
        _driver=driver,
        _driver_matches_selected_provider=lambda _d: True,
        _driver_matches_target_binding=lambda _d: True,
    )
    window._try_live_model_change = lambda alias, label: MainWindow._try_live_model_change(
        window, alias, label
    )
    return window


def test_idle_gemini_model_pick_resumes_on_the_new_model():
    toasts: list[str] = []
    driver = _GeminiLike()
    window = _window(driver, toasts)

    MainWindow._apply_model_choice(window, "gemini-3.8-pro")

    assert driver.ended == 1
    assert toasts == [
        "Model set to gemini-3.8-pro — your next message continues this chat on it."
    ]


def test_busy_driver_is_left_alone_then_caught_at_send_time():
    toasts: list[str] = []
    driver = _GeminiLike()
    driver.is_busy = True
    window = _window(driver, toasts)

    MainWindow._apply_model_choice(window, "gemini-3.8-pro")

    assert driver.ended == 0
    assert not MainWindow._driver_model_is_stale(window, driver)
    driver.is_busy = False
    assert MainWindow._driver_model_is_stale(window, driver)


def test_in_place_drivers_are_never_retired():
    """Claude/GPT/Muse switch in place: no respawn, no rebuilt session."""
    driver = _GeminiLike()
    driver.set_model = lambda *_a, **_k: True
    window = _window(driver, [])
    assert not MainWindow._retire_for_respawn(window, driver, "set_model")
    assert driver.ended == 0


def test_driver_without_requested_model_tag_is_not_stale():
    driver = _GeminiLike()
    del driver._helios_requested_model
    window = _window(driver, [])
    window._model = "gemini-3.8-pro"
    assert not MainWindow._driver_model_is_stale(window, driver)


def test_a_starting_participant_counts_as_executing():
    """A Gemini/Muse resume runs before its attempt is recorded; switching
    providers then joined the Work and earned "already executing"."""
    starting = _GeminiLike()
    starting.is_initializing = True
    window = types.SimpleNamespace(
        _work_coordinator=types.SimpleNamespace(
            store=types.SimpleNamespace(list_active_execution_attempts=lambda: [])
        ),
        _driver_manager=types.SimpleNamespace(drivers_for_work=lambda _w: {starting}),
    )
    assert MainWindow._work_is_executing(window, "work-1")
    starting.is_initializing = False
    assert not MainWindow._work_is_executing(window, "work-1")
