"""Contract tests for Wave 3 MainWindow mixin decomposition (browser, recovery, checkpoints)."""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios.main_window import MainWindow  # noqa: E402
from helios.window_browser import WindowBrowserMixin  # noqa: E402
from helios.window_checkpoints import WindowCheckpointsMixin  # noqa: E402
from helios.window_goal import GoalWorkMixin  # noqa: E402
from helios.window_recovery import WindowRecoveryMixin  # noqa: E402


def test_main_window_inherits_all_extracted_controller_mixins():
    assert issubclass(MainWindow, GoalWorkMixin)
    assert issubclass(MainWindow, WindowBrowserMixin)
    assert issubclass(MainWindow, WindowRecoveryMixin)
    assert issubclass(MainWindow, WindowCheckpointsMixin)


def test_extracted_methods_resolve_from_their_owning_mixins():
    assert MainWindow._select_browser_session is WindowBrowserMixin._select_browser_session
    assert MainWindow._on_browser_bus_call is WindowBrowserMixin._on_browser_bus_call
    assert MainWindow._on_reconcile_stuck_turn is WindowRecoveryMixin._on_reconcile_stuck_turn
    assert MainWindow._stage_capacity_retry_send is WindowRecoveryMixin._stage_capacity_retry_send
    assert MainWindow._capture_checkpoint is WindowCheckpointsMixin._capture_checkpoint
    assert MainWindow._show_rewind is WindowCheckpointsMixin._show_rewind
