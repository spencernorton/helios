"""A Claude turn can end while background shells/subagents keep running; the
CLI wakes the session when they finish. The idle reaper used to EOF such a
session after 10 minutes, killing the work ("a session randomly stopped")."""
from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402
from helios.backend.process.driver_manager import (  # noqa: E402
    DriverManager,
    working_session_ids,
)


class FakeDriver:
    def __init__(self, session_id: str, *, last_activity: float = 0.0) -> None:
        self.session_id = session_id
        self.is_busy = False
        self.is_accepting_input = True
        self.last_activity = last_activity
        self.has_background_work = False
        self.ended = 0

    def end_input(self) -> None:
        self.ended += 1


def _driver():
    driver = ClaudeCliDriver(cwd="/tmp", permission_mode="default")
    driver._proc = object()
    return driver


def test_driver_tracks_background_tasks_from_the_cli():
    driver = _driver()
    assert not driver.has_background_work
    # Measured on claude 2.1.308: the result lands first, the task later.
    driver._dispatch_record({"type": "system", "subtype": "background_tasks_changed",
                             "tasks": [{"task_id": "b3i4r9qh8", "task_type": "local_bash"}]})
    assert driver.has_background_work and not driver.is_busy
    driver._dispatch_record({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
    assert not driver.has_background_work


def test_reaper_and_cap_leave_sessions_with_background_work_alone():
    waiting = FakeDriver("waiting", last_activity=0)
    waiting.has_background_work = True
    stale = FakeDriver("stale", last_activity=0)
    mgr = DriverManager(max_live=2, idle_seconds=60)
    mgr.live = {"waiting": waiting, "stale": stale}

    mgr.reap_idle(now=100)
    mgr.evict_for_cap()

    assert waiting.ended == 0 and stale.ended >= 1
    assert working_session_ids(mgr) == {"waiting"}


def test_sidebar_dot_follows_the_driver_not_a_mid_turn_tail():
    """Mid-turn the transcript tail often ends on a text record, which reads
    as READY: a green dot under a "working" caption."""
    import types

    from helios.backend.session_state import STATE_READY, STATE_WORKING, SessionStatus
    from helios.widgets.session_list import SessionList

    painted = {}

    def row(sid):
        return types.SimpleNamespace(
            status=SessionStatus(STATE_READY, 0.0),
            session=types.SimpleNamespace(project=types.SimpleNamespace(read_only=False)),
            update_status=lambda st: painted.__setitem__(sid, st.state),
            update_age=lambda st: None,
        )

    fake = types.SimpleNamespace(
        _status_tick_running=True, _destroyed=False, _reload_gen=1, _pool_provider_gen=0,
        _rows_by_id={"mine": row("mine"), "other": row("other")},
        _working_ids_provider=lambda: {"mine"},
    )
    ready = SessionStatus(STATE_READY, 0.0)
    SessionList._apply_status_updates(fake, 1, {"mine": ready, "other": ready}, {})
    assert painted == {"mine": STATE_WORKING}
