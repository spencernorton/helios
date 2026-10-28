"""Background tasks pane (roadmap Phase 3b)."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402


def test_driver_publishes_rows_and_stops_only_known_tasks():
    driver = ClaudeCliDriver(cwd="/home/user/proj", permission_mode="default")
    driver._proc = object()
    driver._session_id = "sess-1"
    seen, sent = [], []
    driver.connect("background-tasks-changed", lambda _d, rows: seen.append(rows))
    driver._send_control_request = lambda request, cb: sent.append(request) or True
    driver._dispatch_record({"type": "system", "subtype": "background_tasks_changed", "tasks": [
        {"task_id": "b1", "task_type": "local_bash", "description": "npm run dev"}]})
    assert seen == [[{"task_id": "b1", "task_type": "local_bash", "description": "npm run dev"}]]
    assert driver.stop_background_task("b1") and sent == [{"subtype": "stop_task", "task_id": "b1"}]
    assert not driver.stop_background_task("unknown")
    path = driver.task_output_path("b1")
    assert path.endswith(os.path.join("-home-user-proj", "sess-1", "tasks", "b1.output"))


def test_pane_lists_tasks_and_shows_empty_state():
    from helios.widgets.background_pane import BackgroundPane

    pane = BackgroundPane()
    assert pane._empty.get_visible() and not pane._list.get_visible()

    class Driver:
        stopped = []

        def task_output_path(self, task_id):
            return "/nonexistent"

        def stop_background_task(self, task_id):
            self.stopped.append(task_id)
            return True

    driver = Driver()
    pane.show_tasks(driver, [{"task_id": "b1", "task_type": "local_bash", "description": "sleep 600"}])
    assert pane._list.get_visible() and not pane._empty.get_visible()
    pane._stop(__import__("gi.repository.Gtk", fromlist=["Button"]).Button(), "b1")
    assert driver.stopped == ["b1"]
    pane.show_tasks(None, [])
    assert pane._empty.get_visible()
