"""Wave 1 stability tests: Gemini liveness reconciliation, slot retry, operator
stuck-turn recovery without replay, Codex thread/closed, and approval code details.
"""

from __future__ import annotations

import io
import sqlite3
import time
import types
from pathlib import Path

import pytest

from helios.backend import google_env, model_catalog
from helios.backend.process import codex_protocol
from helios.backend.work_coordinator import WorkCoordinator, tag_driver
from helios.backend.work_store import (
    ExecutionAdmissionError,
    WorkStore,
    WorkStoreError,
)


def _make_summary_db(path: Path, rows: list[tuple]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT '',
            not_fully_idle NUMERIC NOT NULL DEFAULT 0,
            killed NUMERIC NOT NULL DEFAULT 0,
            step_count INTEGER NOT NULL DEFAULT 0,
            last_user_input_step_index INTEGER NOT NULL DEFAULT -1,
            last_modified_time DATETIME NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO conversation_summaries (
            conversation_id, status, not_fully_idle, killed,
            step_count, last_user_input_step_index, last_modified_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_probe_conversation_idle_parses_nanosecond_utc_timestamp(tmp_path):
    db = _make_summary_db(
        tmp_path / "conversation_summaries.db",
        [
            (
                "905ea6fa-1111-2222-3333-444444444444",
                "CASCADE_RUN_STATUS_IDLE",
                0,
                0,
                129,
                122,
                "2026-11-23 15:04:33.290472337+00:00",
            )
        ],
    )

    snap = google_env.probe_conversation_idle(
        "905ea6fa-1111-2222-3333-444444444444", db_path=db
    )
    assert snap is not None
    assert snap.status == "CASCADE_RUN_STATUS_IDLE"
    assert snap.not_fully_idle is False
    assert snap.killed is False
    assert snap.step_count == 129
    assert snap.last_user_input_step_index == 122
    assert snap.last_modified_epoch > 1_700_000_000


def test_probe_conversation_idle_handles_missing_and_invalid_inputs(tmp_path):
    assert google_env.probe_conversation_idle("", db_path=tmp_path / "missing.db") is None
    assert google_env.probe_conversation_idle("../bad", db_path=tmp_path / "missing.db") is None
    assert google_env.probe_conversation_idle("abc", db_path=tmp_path / "missing.db") is None


def test_operator_reconcile_unblocks_work_without_replaying_queue(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        work = store.create_work(objective="stuck", cwd=str(tmp_path))
        participant = store.bind_participant(work.work_id, "kimi")
        attempt = store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="kimi",
            expected_participant_generation=participant.generation,
            workspace_root=str(tmp_path),
        )
        store.record_execution_dispatch(
            attempt.attempt_id,
            wire_prompt_text="prompt that lost receipt",
            provider_request_key=attempt.attempt_id,
        )
        store.record_execution_stop(attempt.attempt_id, queue_disposition="held")

        with pytest.raises(ExecutionAdmissionError, match="already executing"):
            store.start_execution_attempt(
                work.work_id,
                participant.participant_id,
                provider="kimi",
                expected_participant_generation=participant.generation,
                workspace_root=str(tmp_path),
            )

        assert [a.attempt_id for a in coordinator.active_attempts_for_work(work.work_id)] == [
            attempt.attempt_id
        ]

        released = coordinator.reconcile_uncertain_attempt(attempt.attempt_id)
        assert released.status == "aborted"
        assert released.terminal_reason == "reconciled.operator_confirmed"
        assert released.queue_disposition == "discarded"
        assert coordinator.active_attempts_for_work(work.work_id) == []

        events = store.list_events(work.work_id)
        finished = [ev for ev in events if ev.event_type == "execution.attempt.finished"]
        assert finished and finished[-1].payload["operator_confirmed"] is True
        assert finished[-1].payload["terminal_receipt_recorded"] is False
        assert finished[-1].payload["queue_disposition"] == "discarded"

        with pytest.raises(WorkStoreError, match="not running"):
            coordinator.reconcile_uncertain_attempt(attempt.attempt_id)

        next_attempt = store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="kimi",
            expected_participant_generation=participant.generation,
            workspace_root=str(tmp_path),
        )
        assert next_attempt.status == "running"
    finally:
        store.close()


def test_codex_protocol_classifies_thread_closed_as_handled():
    assert codex_protocol.classify_notification("thread/closed") == "handled"
    assert "thread/closed" in codex_protocol.DRIVER_HANDLED_NOTIFICATION_METHODS
    assert "thread/closed" not in codex_protocol.UNSUPPORTED_NOTIFICATION_METHODS


def test_gemini_driver_reconciles_silent_idle_turn_and_flushes_streamed_text(
    monkeypatch, tmp_path
):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib

    from helios.backend.process import gemini_driver as gd
    from helios.main_window import MainWindow

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        gd,
        "find_google_binary",
        lambda: google_env.GoogleBinary(Path("/test-bin/agy"), source="test"),
    )
    monkeypatch.setattr(gd.os, "killpg", lambda *_args: None)

    class _FakeStdin:
        def __init__(self):
            self.writes = []
            self.closed = False

        def write(self, data):
            self.writes.append(data)
            return len(data)

        def flush(self):
            pass

        def close(self):
            self.closed = True

    class _FakeProc:
        def __init__(self):
            self.pid = 99999
            self.stdin = _FakeStdin()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return 0

    class _InertThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(gd.threading, "Thread", _InertThread)
    proc = _FakeProc()
    monkeypatch.setattr(gd.subprocess, "Popen", lambda *a, **kw: proc)

    store = WorkStore(tmp_path / "work.db")
    coordinator = WorkCoordinator(store)
    driver = gd.GeminiCliDriver(cwd=str(project), model="gemini-3.8-flash", permission_mode="dontAsk")
    try:
        driver.start()
        cid = "905ea6fa-1111-2222-3333-444444444444"
        driver._dispatch_message(
            {
                "event": "init",
                "conversation_id": cid,
                "init": {
                    "cwd": str(project),
                    "model": "gemini-3.8-flash",
                    "permission_mode": "request-review",
                    "tools": [],
                },
            }
        )
        driver._helios_identity_confirmed = True

        work = coordinator.ensure_work(cwd=str(project), lead_provider="google")
        participant = coordinator.bind_participant(
            work.work_id, "google", native_id=cid, model="gemini-3.8-flash"
        )
        tag_driver(driver, participant)
        win = types.SimpleNamespace(_work_coordinator=coordinator)
        driver.set_execution_attempt_controller(
            lambda d: MainWindow._start_execution_attempt_for_driver(win, d),
            lambda *a: pytest.fail("typed terminal required"),
            record_dispatch=lambda d, aid, e: MainWindow._record_execution_dispatch_for_driver(
                win, d, aid, e
            ),
            record_acceptance=lambda d, aid, e: MainWindow._record_execution_acceptance_for_driver(
                win, d, aid, e
            ),
            finish_with_evidence=lambda d, aid, e: MainWindow._finish_execution_attempt_with_evidence_for_driver(
                win, d, aid, e
            ),
            record_contribution=coordinator.record_turn,
        )

        turns, results, errors = [], [], []
        driver.connect("turn-appended", lambda _d, t: turns.append(t))
        driver.connect("result", lambda _d, r: results.append(r))
        driver.connect("error", lambda _d, e: errors.append(e))

        assert driver.send_user_text("answer me").accepted
        aid = driver.execution_attempt_id
        driver._dispatch_message(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": cid,
                    "step_index": 122,
                    "state": "DONE",
                    "step_type": "user_input",
                },
            }
        )
        driver._dispatch_message(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": cid,
                    "step_index": 128,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": "Recovered final answer.",
                },
            }
        )

        # Simulate silence exceeding threshold, with DB showing IDLE + quiet for 40s.
        driver._last_activity = time.monotonic() - 120.0
        driver._turn_started_wall = time.time() - 90.0

        # Non-idle DB snapshot must NOT reconcile.
        busy_snap = google_env.ConversationIdleSnapshot(
            conversation_id=cid,
            status="CASCADE_RUN_STATUS_RUNNING",
            not_fully_idle=True,
            killed=False,
            step_count=129,
            last_user_input_step_index=122,
            last_modified_epoch=time.time() - 40.0,
        )
        driver._apply_idle_probe(aid, busy_snap)
        assert driver.is_busy is True
        assert driver.execution_attempt_id == aid

        # Authoritative IDLE snapshot reconciles, saves text, frees slot, closes wedged child.
        idle_snap = google_env.ConversationIdleSnapshot(
            conversation_id=cid,
            status="CASCADE_RUN_STATUS_IDLE",
            not_fully_idle=False,
            killed=False,
            step_count=129,
            last_user_input_step_index=122,
            last_modified_epoch=time.time() - 40.0,
        )
        driver._apply_idle_probe(aid, idle_snap)
        while GLib.MainContext.default().iteration(False):
            pass

        assert driver.is_busy is False
        assert driver.execution_attempt_id == ""
        assert errors == []
        assert len(turns) == 1
        assert turns[0].text == "Recovered final answer."
        assert len(results) == 1
        assert results[0]["reconciled"] is True
        assert results[0]["subtype"] == "success"
        assert proc.stdin.closed is True

        attempt = store.get_execution_attempt(aid)
        assert attempt.status == "completed"
        assert attempt.terminal_reason == "provider_result.reconciled_idle"
        assert store.list_active_execution_attempts() == []
    finally:
        driver._cancel_liveness_watchdog()
        store.close()


def test_codex_driver_handles_thread_closed_cleanly_without_drift():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")

    from helios.backend.process.codex_app_driver import CodexAppServerDriver

    drv = CodexAppServerDriver(cwd="/tmp", model="gpt-5.4")
    drv._native_mode = True
    drv._closed = False
    drv._app_acc.thread_id = "thr-123"
    drv._hub = types.SimpleNamespace(release=lambda _c: None)

    drifts, errors, exits = [], [], []
    drv.connect("capability-drift", lambda _d, payload: drifts.append(payload))
    drv.connect("error", lambda _d, msg: errors.append(msg))
    drv.connect("exited", lambda _d, code: exits.append(code))

    # Child thread closure must NOT tear down root driver.
    drv._consume_app_notification("thread/closed", {"threadId": "thr-child"})
    assert drv._closed is False
    assert exits == []
    assert drifts == []

    # Root thread closure detaches idle driver cleanly with exit code 0.
    drv._consume_app_notification("thread/closed", {"threadId": "thr-123"})
    assert drv._closed is True
    assert exits == [0]
    assert errors == []
    assert drifts == []


def test_main_window_google_capacity_auto_retry_dispatches_once_slot_frees(monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    try:
        gi.require_version("GtkSource", "5")
    except ValueError:
        pytest.skip("GtkSource 5 unavailable")
    from gi.repository import GLib

    from helios.main_window import MainWindow

    state = {"capacity": "closing"}
    dispatched = []
    restored = []
    toasts = []
    busy_calls = []

    class _FakeManager:
        def prepare_provider_start(self, provider, limit):
            return state["capacity"]

    win = types.SimpleNamespace(
        _destroyed=False,
        _driver=None,
        _driver_manager=_FakeManager(),
        _next_chat=types.SimpleNamespace(
            work_id="wrk_1",
            resume_id="",
            project=types.SimpleNamespace(cwd="/tmp"),
        ),
        _model="gemini-3.8-flash",
        _selected_provider=lambda: model_catalog.PROVIDER_GOOGLE,
        _composer=types.SimpleNamespace(set_busy=lambda v: busy_calls.append(v)),
        _chat_toolbar=types.SimpleNamespace(set_busy=lambda v: None),
        _activity=types.SimpleNamespace(set_activity=lambda s: None, clear=lambda: None),
        _clear_busy_ui=lambda: busy_calls.append(False),
        _restore_blocked_execution_send=lambda text: (restored.append(text), False)[1],
        _toast=toasts.append,
        _pending_capacity_send=None,
    )

    monkeypatch.setattr(
        MainWindow,
        "_on_composer_send",
        lambda self, _c, text: dispatched.append(text),
    )

    MainWindow._stage_capacity_retry_send(win, "run after eviction")
    assert win._pending_capacity_send is not None
    assert busy_calls[-1] is True

    # Still closing: no dispatch yet, no draft dump.
    assert MainWindow._maybe_retry_capacity_send(win, from_timer=True) is True
    assert dispatched == []
    assert restored == []

    # Victim finishes closing -> ready -> dispatches once automatically.
    state["capacity"] = "ready"
    assert MainWindow._maybe_retry_capacity_send(win, from_timer=False) is False
    assert dispatched == ["run after eviction"]
    assert win._pending_capacity_send is None
    assert restored == []

    # Timeout case restores draft cleanly without dispatching.
    state["capacity"] = "closing"
    MainWindow._stage_capacity_retry_send(win, "timed out prompt")
    win._pending_capacity_send["deadline"] = time.monotonic() - 1.0
    assert MainWindow._maybe_retry_capacity_send(win, from_timer=True) is False
    while GLib.MainContext.default().iteration(False):
        pass
    assert restored == ["timed out prompt"]
    assert win._pending_capacity_send is None


def test_main_window_orphaned_attempts_skips_live_running_driver(tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    try:
        gi.require_version("GtkSource", "5")
    except ValueError:
        pytest.skip("GtkSource 5 unavailable")

    from helios.main_window import MainWindow

    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        work = store.create_work(objective="test", cwd=str(tmp_path))
        p = store.bind_participant(work.work_id, "google")
        att = store.start_execution_attempt(
            work.work_id,
            p.participant_id,
            provider="google",
            expected_participant_generation=p.generation,
            workspace_root=str(tmp_path),
        )
        live_drv = types.SimpleNamespace(is_running=True, execution_attempt_id=att.attempt_id)
        dead_drv = types.SimpleNamespace(is_running=False, execution_attempt_id=att.attempt_id)

        win_live = types.SimpleNamespace(
            _work_coordinator=coordinator,
            _driver_manager=types.SimpleNamespace(drivers_for_shutdown=lambda: [live_drv]),
        )
        assert MainWindow._orphaned_attempts_for_work(win_live, work.work_id) == []
        assert (
            MainWindow._reconcile_orphaned_attempts_confirmed(
                win_live, work.work_id, [att.attempt_id]
            )
            == 0
        )

        win_dead = types.SimpleNamespace(
            _work_coordinator=coordinator,
            _driver_manager=types.SimpleNamespace(drivers_for_shutdown=lambda: [dead_drv]),
        )
        orphans = MainWindow._orphaned_attempts_for_work(win_dead, work.work_id)
        assert [a.attempt_id for a in orphans] == [att.attempt_id]
        assert (
            MainWindow._reconcile_orphaned_attempts_confirmed(
                win_dead, work.work_id, [att.attempt_id]
            )
            == 1
        )
        assert store.get_execution_attempt(att.attempt_id).status == "aborted"
    finally:
        store.close()


def test_approval_code_detail_renders_visible_non_collapsed_height():
    """Regression test for collapsed approval command details in GTK4."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, GLib, Gtk

    if Gdk.Display.get_default() is None:
        pytest.skip("No GTK display available")

    from helios.widgets import question_dialog

    app = Adw.Application(application_id="com.example.HeliosWave1ApprovalTest")
    observed = {}

    def _find_widgets(root_widget, cls):
        found = []
        stack = [root_widget]
        while stack:
            w = stack.pop()
            if isinstance(w, cls):
                found.append(w)
            child = w.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return found

    def on_activate(application):
        win = Adw.ApplicationWindow(application=application)
        win.set_default_size(960, 680)
        win.present()
        dlg = question_dialog.present_question(
            win,
            {
                "presentation": "tool-approval",
                "allowOther": False,
                "detail": {
                    "kind": "code",
                    "title": "Command",
                    "text": "python3 -m unittest -v",
                    "language": "bash",
                },
                "grantScope": "Allow once applies only to this request.",
                "questions": [
                    {
                        "header": "Allow Bash?",
                        "question": "Run the project test suite.",
                        "options": [{"label": "Allow once"}, {"label": "Deny"}],
                    }
                ],
            },
            lambda _ans: None,
            lambda: None,
        )

        def inspect():
            views = _find_widgets(dlg, Gtk.TextView)
            scrollers = _find_widgets(dlg.get_child(), Gtk.ScrolledWindow)
            observed["text_view_count"] = len(views)
            observed["text_view_height"] = max((v.get_height() for v in views), default=0)
            observed["text_view_width"] = max((v.get_width() for v in views), default=0)
            observed["scroller_count"] = len(scrollers)
            if views:
                buf = views[0].get_buffer()
                observed["text"] = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
            dlg.close()
            win.close()
            application.quit()
            return False

        GLib.timeout_add(300, inspect)

    app.connect("activate", on_activate)
    app.run([])

    assert observed.get("text_view_count") == 1
    assert observed.get("scroller_count") == 1
    assert observed.get("text_view_width", 0) >= 400
    assert observed.get("text_view_height", 0) >= 40
    assert observed.get("text") == "python3 -m unittest -v"
