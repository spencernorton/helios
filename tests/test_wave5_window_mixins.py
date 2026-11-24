"""Tests for Wave 5 MainWindow decomposition into WindowModelsMixin and WindowExecutionMixin."""

from __future__ import annotations

import types
from pathlib import Path

import pytest


def test_main_window_inherits_models_and_execution_mixins():
    pytest.importorskip("gi")
    from helios.main_window import MainWindow
    from helios.window_execution import WindowExecutionMixin
    from helios.window_models import WindowModelsMixin

    assert issubclass(MainWindow, WindowModelsMixin)
    assert issubclass(MainWindow, WindowExecutionMixin)

    for name in (
        "_refresh_model_catalog",
        "_apply_model_catalog",
        "_prune_obsolete_hidden_models",
        "_apply_model_choice",
        "_switch_conversation_model_in_place",
        "_execute_in_place_model_switch",
        "_complete_pending_model_switch_if_ready",
        "_apply_default_model_for_new_chat",
    ):
        assert hasattr(WindowModelsMixin, name)
        assert getattr(MainWindow, name) is getattr(WindowModelsMixin, name)

    for name in (
        "_effective_permission_mode",
        "_permission_fallback",
        "_execution_target_provider",
        "_execution_target_session_id",
        "_execution_target_lock_reason",
        "_sync_execution_control",
        "_on_toolbar_permission_changed",
        "_on_toolbar_effort_changed",
        "_start_execution_attempt_for_driver",
        "_finish_execution_attempt_with_evidence_for_driver",
    ):
        assert hasattr(WindowExecutionMixin, name)
        assert getattr(MainWindow, name) is getattr(WindowExecutionMixin, name)


def test_execution_mixin_honors_main_window_sanitize_global_default_monkeypatch(monkeypatch):
    pytest.importorskip("gi")
    from helios import main_window as mw_mod
    from helios.main_window import MainWindow

    calls: list[str] = []
    monkeypatch.setattr(
        mw_mod,
        "sanitize_global_default",
        lambda mode: (calls.append(mode), "plan")[1],
    )
    stub = types.SimpleNamespace(_permission_mode="acceptEdits", _ui_state={})
    mode, source = MainWindow._permission_fallback(stub, "/tmp/demo-project")
    assert mode == "plan"
    assert source == "default"
    assert calls == ["acceptEdits"]


def test_models_mixin_reexports_pending_switch_and_home_lock():
    pytest.importorskip("gi")
    from helios import main_window as mw_mod
    from helios.backend.projects import HOME_CWD
    from helios.window_execution import _home_execution_locked
    from helios.window_models import _OR_CREDITS_MIN_INTERVAL, _PendingModelSwitch

    assert mw_mod._home_execution_locked is _home_execution_locked
    assert mw_mod._PendingModelSwitch is _PendingModelSwitch
    assert mw_mod._OR_CREDITS_MIN_INTERVAL == _OR_CREDITS_MIN_INTERVAL
    assert _home_execution_locked(HOME_CWD) is True
    assert _home_execution_locked("/tmp/not-home") is False


def test_main_window_stays_reduced_after_wave5_extraction():
    mw_file = Path(__file__).resolve().parents[1] / "src" / "helios" / "main_window.py"
    line_count = len(mw_file.read_text(encoding="utf-8").splitlines())
    assert line_count < 7700, f"main_window.py grew back to {line_count} lines"
