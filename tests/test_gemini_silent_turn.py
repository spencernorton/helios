"""The Gemini silent-turn watchdog must never close a turn that is still working.

Each case drives the real liveness tick against Antigravity records laid out
under a throwaway home, as agy writes them: the summary index row, the
conversation's trajectory and its subagents' trajectories.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib

from helios.backend import google_env
from helios.backend.process import gemini_driver as gd
from helios.backend.process.gemini_driver import GeminiCliDriver
from helios.widgets.activity_indicator import STATE_STALLED, native_activity_state

MODEL = "gemini-3.8-flash"
CID = "50c9aa4a-c5b9-41f4-a958-8ac571746a1a"
CHILD = "4866aaf4-6325-4836-b98c-f344f570cee2"

# Antigravity's CortexStepType / CortexStepStatus values, as agy stores them.
USER_INPUT = 14
PLANNER = 15  # planner response
GENERIC = 132
DONE = 3
GENERATING = 8


def _drain():
    while GLib.MainContext.default().iteration(False):
        pass


@pytest.fixture(autouse=True)
def drain_callbacks():
    _drain()
    yield
    _drain()


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


class _FakeProcess:
    def __init__(self):
        self.pid = 2_000_000_001
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


class _SyncThread:
    """Runs the liveness probe inline so a test sees its verdict."""

    def __init__(self, *, target, args=(), daemon=None, name=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _iso_utc(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def agy_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / ".gemini" / "antigravity-cli"
    (root / "conversations").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return root


def _summary(root: Path, rows):
    """rows: (cid, status, not_fully_idle, step_count, user_step, modified, parent)."""
    conn = sqlite3.connect(root / "conversation_summaries.db")
    conn.execute(
        """
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT '',
            not_fully_idle NUMERIC NOT NULL DEFAULT 0, killed NUMERIC NOT NULL DEFAULT 0,
            step_count INTEGER NOT NULL DEFAULT 0,
            last_user_input_step_index INTEGER NOT NULL DEFAULT -1,
            last_modified_time DATETIME NOT NULL,
            parent_conversation_id TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.executemany(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
        [(c, s, n, sc, u, _iso_utc(m), p) for c, s, n, sc, u, m, p in rows],
    )
    conn.commit()
    conn.close()


def _trajectory(root: Path, cid: str, steps, *, written: float):
    path = root / "conversations" / f"{cid}.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE steps (idx integer, step_type integer NOT NULL DEFAULT 0, "
        "status integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))"
    )
    conn.executemany("INSERT INTO steps VALUES (?, ?, ?)", steps)
    conn.commit()
    conn.close()
    os.utime(path, (written, written))


@pytest.fixture
def driver(tmp_path, monkeypatch, agy_home):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        gd, "find_google_binary",
        lambda: google_env.GoogleBinary(Path("/test-bin/agy"), source="test"),
    )
    monkeypatch.setattr(gd.threading, "Thread", _InertThread)
    monkeypatch.setattr(gd.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(gd.subprocess, "Popen", lambda *a, **kw: _FakeProcess())
    drv = GeminiCliDriver(cwd=str(project), model=MODEL, permission_mode="dontAsk")
    drv.infos = []
    monkeypatch.setattr(gd._log, "info", lambda msg, *args: drv.infos.append(msg % args))
    drv.lifecycle = []
    drv.results = []
    drv.connect("result", lambda _d, result: drv.results.append(result))
    attempts = iter(range(1, 100))
    drv.set_execution_attempt_controller(
        lambda _d: (f"attempt-{next(attempts)}", ""),
        lambda *_a: pytest.fail("Google must use the typed terminal adapter"),
        record_dispatch=lambda _d, a, e: drv.lifecycle.append(("dispatch", a)),
        record_acceptance=lambda _d, a, e: drv.lifecycle.append(("accept", a, e.accepted_turn_id)),
        record_stop=lambda *_a: None,
        record_contribution=lambda _d, turn: turn,
        finish_with_evidence=lambda _d, a, e: drv.lifecycle.append(("terminal", a, e.reason_code)),
    )
    drv.start()
    drv._dispatch_message({
        "event": "init", "conversation_id": CID,
        "init": {"cwd": str(project), "model": MODEL,
                 "permission_mode": "request-review", "tools": []},
    })
    drv._helios_identity_confirmed = True
    _drain()
    yield drv
    drv._cancel_liveness_watchdog()


def _step(index, kind, state="DONE", **fields):
    return {"event": "step_update", "step_update": {
        "conversation_id": CID, "step_index": index, "step_type": kind,
        "state": state, **fields}}


def _start_turn(drv, user_index, *, started_ago):
    assert drv.send_user_text("review it").accepted
    drv._dispatch_message(_step(user_index, "user_input"))
    drv._turn_started_wall = time.time() - started_ago


def _go_quiet(drv, seconds):
    drv._last_activity = time.monotonic() - seconds
    drv._last_activity_wall = time.time() - seconds


def _tick(drv, monkeypatch):
    monkeypatch.setattr(gd.threading, "Thread", _SyncThread)
    drv._on_liveness_tick()
    _drain()


def _still_running(drv, attempt):
    return (
        drv.is_busy
        and drv.execution_attempt_id == attempt
        and not drv._proc.stdin.closed
        and drv.results == []
        and not any(event[0] == "terminal" for event in drv.lifecycle)
    )


def test_stale_idle_summary_does_not_close_a_turn_mid_answer(driver, agy_home, monkeypatch):
    """Antigravity's summary row said 10 idle steps while the trajectory held
    86 and the final response was still generating; Helios closed that turn
    after 45 quiet seconds and lost the answer."""
    _start_turn(driver, 0, started_ago=330)
    for index in range(1, 85):
        driver._dispatch_message(_step(index, "tool", tool_name="run_command"))
    attempt = driver.execution_attempt_id
    now = time.time()
    _summary(agy_home, [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 10, -1, now - 313, "")])
    steps = [(0, USER_INPUT, DONE)] + [(i, GENERIC, DONE) for i in range(1, 85)]
    _trajectory(agy_home, CID, steps + [(85, PLANNER, GENERATING)], written=now - 100)
    _go_quiet(driver, 100)

    _tick(driver, monkeypatch)

    assert _still_running(driver, attempt)
    assert driver._last_activity_wall == pytest.approx(now - 100, abs=1)
    assert any("step 85 is still generating" in line for line in driver.infos), driver.infos


def test_running_subagent_keeps_the_turn_open(driver, agy_home, monkeypatch):
    """The parent rests on a finished response while its subagent works; the
    subagent's writes are progress, so the quiet spell starts over."""
    _start_turn(driver, 44, started_ago=900)
    driver._dispatch_message(_step(78, "agent_response", text_delta="Delegating the survey."))
    attempt = driver.execution_attempt_id
    now = time.time()
    _summary(agy_home, [
        (CID, "CASCADE_RUN_STATUS_IDLE", 0, 79, 44, now - 400, ""),
        (CHILD, "CASCADE_RUN_STATUS_RUNNING", 1, 170, -1, now - 300, CID),
    ])
    parent = [(i, GENERIC, DONE) for i in range(44)] + [(44, USER_INPUT, DONE)]
    parent += [(i, GENERIC, DONE) for i in range(45, 78)] + [(78, PLANNER, DONE)]
    _trajectory(agy_home, CID, parent, written=now - 400)
    _trajectory(agy_home, CHILD,
                [(0, USER_INPUT, DONE), (1, GENERIC, DONE), (2, PLANNER, GENERATING)],
                written=now - 5)
    _go_quiet(driver, 400)

    _tick(driver, monkeypatch)

    assert _still_running(driver, attempt)
    assert time.monotonic() - driver._last_activity < 60


def test_trajectory_progress_restarts_the_quiet_spell(driver, agy_home, monkeypatch):
    """A long generation writes its trajectory without a stdout record."""
    _start_turn(driver, 0, started_ago=300)
    attempt = driver.execution_attempt_id
    now = time.time()
    _summary(agy_home, [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 2, -1, now - 280, "")])
    _trajectory(agy_home, CID, [(0, USER_INPUT, DONE), (1, GENERIC, DONE),
                                (2, PLANNER, GENERATING)], written=now - 12)
    _go_quiet(driver, 150)

    _tick(driver, monkeypatch)

    assert _still_running(driver, attempt)
    assert driver._last_activity_wall == pytest.approx(now - 12, abs=1)


def _finished_records(agy_home, *, final_index=128):
    now = time.time()
    _summary(agy_home, [(CID, "CASCADE_RUN_STATUS_IDLE", 0, final_index + 1, 122, now - 70, "")])
    steps = [(i, GENERIC, DONE) for i in range(122)] + [(122, USER_INPUT, DONE)]
    steps += [(i, GENERIC, DONE) for i in range(123, final_index)]
    _trajectory(agy_home, CID, steps + [(final_index, PLANNER, DONE)], written=now - 120)


def test_recovery_needs_the_final_response_seen_on_stdout(driver, agy_home, monkeypatch):
    """Antigravity finished, but Helios never received the final response:
    closing now would leave an empty turn, so it keeps waiting."""
    _start_turn(driver, 122, started_ago=600)
    driver._dispatch_message(_step(126, "agent_response", text_delta="Checking one more file."))
    driver._dispatch_message(_step(127, "tool", tool_name="view_file"))
    attempt = driver.execution_attempt_id
    _finished_records(agy_home)
    monkeypatch.setattr(google_env, "open_conversation_ids", lambda pid: frozenset())
    _go_quiet(driver, 120)

    _tick(driver, monkeypatch)

    assert _still_running(driver, attempt)
    assert any("did not see its final response" in line for line in driver.infos), driver.infos


def test_open_trajectory_vetoes_recovery_until_agy_closes_it(driver, agy_home, monkeypatch):
    """Every record agrees the turn is over and Helios holds the final answer,
    but agy still has a conversation open: a cascade is running somewhere."""
    _start_turn(driver, 122, started_ago=600)
    driver._dispatch_message(_step(128, "agent_response", text_delta="Recovered final answer."))
    attempt = driver.execution_attempt_id
    _finished_records(agy_home)
    open_ids = {"value": frozenset({CHILD})}
    monkeypatch.setattr(google_env, "open_conversation_ids", lambda pid: open_ids["value"])
    _go_quiet(driver, 120)

    _tick(driver, monkeypatch)
    assert _still_running(driver, attempt)
    assert driver.infos and "still has a conversation open" in driver.infos[-1]

    # agy's descriptors cannot be listed: still not proof.
    driver._probe_refusal_logged = False
    open_ids["value"] = None
    _tick(driver, monkeypatch)
    assert _still_running(driver, attempt)
    assert "could not be listed" in driver.infos[-1]

    # The cascade settled and released every trajectory: a genuinely wedged
    # turn (finished, but no result record) is recovered with its answer.
    open_ids["value"] = frozenset()
    _tick(driver, monkeypatch)

    assert driver.is_busy is False
    assert driver.execution_attempt_id == ""
    assert driver._proc.stdin.closed is True
    assert [r["response"] for r in driver.results] == ["Recovered final answer."]
    assert driver.results[0]["reconciled"] is True
    assert ("terminal", attempt, "provider_result.reconciled_idle") in driver.lifecycle
    mirror = [json.loads(line) for line in driver._transcript._path().read_text().splitlines()]
    assert mirror[-1]["type"] == "assistant"


def test_future_stamped_write_is_not_progress(driver, agy_home, monkeypatch):
    """After a clock step, a trajectory stamped in the future must not keep
    resetting the quiet spell (that would also hide the stall notice)."""
    _start_turn(driver, 0, started_ago=300)
    attempt = driver.execution_attempt_id
    now = time.time()
    _summary(agy_home, [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 2, -1, now - 280, "")])
    _trajectory(agy_home, CID, [(0, USER_INPUT, DONE), (1, GENERIC, DONE),
                                (2, PLANNER, GENERATING)], written=now + 3600)
    _go_quiet(driver, 150)

    _tick(driver, monkeypatch)

    assert _still_running(driver, attempt)
    assert driver._last_activity_wall == pytest.approx(now - 150, abs=1)


def test_earlier_narration_is_not_the_final_answer(driver, agy_home, monkeypatch):
    """The final response step finished without text of its own; narration
    from an earlier step must not be saved as the turn's answer."""
    _start_turn(driver, 122, started_ago=600)
    driver._dispatch_message(_step(126, "agent_response", text_delta="I will inspect the files."))
    driver._dispatch_message(_step(127, "tool", tool_name="view_file"))
    driver._dispatch_message(_step(128, "agent_response"))
    attempt = driver.execution_attempt_id
    _finished_records(agy_home)
    monkeypatch.setattr(google_env, "open_conversation_ids", lambda pid: frozenset())
    _go_quiet(driver, 120)

    _tick(driver, monkeypatch)

    assert _still_running(driver, attempt)
    assert any("final response finish with text" in line for line in driver.infos), driver.infos


def test_stall_notice_waits_for_the_probe_and_clears_on_progress(driver, agy_home, monkeypatch):
    activity = []
    driver.connect("activity-updated", lambda _d, payload: activity.append(payload))
    _start_turn(driver, 0, started_ago=1500)
    attempt = driver.execution_attempt_id
    now = time.time()
    _summary(agy_home, [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 2, -1, now - 1400, "")])
    traj = agy_home / "conversations" / f"{CID}.db"
    _trajectory(agy_home, CID, [(0, USER_INPUT, DONE), (1, GENERIC, DONE),
                                (2, PLANNER, GENERATING)], written=now - 30)

    # Twenty-one quiet minutes on stdout, but the trajectory moved 30 s ago:
    # progress, so no stall notice.
    _go_quiet(driver, 1260)
    _tick(driver, monkeypatch)
    assert not any(p.get("category") == "stalled" for p in activity)
    assert _still_running(driver, attempt)

    # Nothing anywhere for twenty-one minutes: the notice appears, once.
    os.utime(traj, (now - 1300, now - 1300))
    _go_quiet(driver, 1260)
    _tick(driver, monkeypatch)
    _tick(driver, monkeypatch)
    notices = [p for p in activity if p.get("category") == "stalled"]
    assert len(notices) == 1
    # What the status line actually renders, not just the payload.
    assert native_activity_state(notices[0]) == (
        STATE_STALLED, "No output from Antigravity for 21+ minutes"
    )

    # Work resumes: the notice is retracted, and a later stall can raise it again.
    os.utime(traj, (time.time() - 5, time.time() - 5))
    _tick(driver, monkeypatch)
    assert activity[-1] == {"category": "phase", "phase": "requesting"}
    assert driver._stall_notice_emitted is False
    assert _still_running(driver, attempt)


def test_a_failing_probe_never_strands_the_watchdog(driver, agy_home, monkeypatch):
    """An unexpected error while reading Antigravity's records must not leave
    the probe marked in flight, which would silence recovery and the stall
    notice for the rest of the turn."""
    _start_turn(driver, 0, started_ago=300)
    attempt = driver.execution_attempt_id

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected layout")

    monkeypatch.setattr(google_env, "assess_turn_completion", boom)
    _go_quiet(driver, 150)

    _tick(driver, monkeypatch)

    assert _still_running(driver, attempt)
    assert driver._liveness_probe_in_flight is False
    assert any("liveness probe failed (RuntimeError)" in line for line in driver.infos), driver.infos


def test_stdout_progress_retracts_the_stall_notice_and_rearms_it(driver, agy_home, monkeypatch):
    """After a stall notice, stdout activity resumes and a later stall must be
    announced again rather than suppressed for the rest of the turn."""
    activity = []
    driver.connect("activity-updated", lambda _d, payload: activity.append(payload))
    _start_turn(driver, 0, started_ago=3000)
    attempt = driver.execution_attempt_id
    now = time.time()
    _summary(agy_home, [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 2, -1, now - 2900, "")])
    _trajectory(agy_home, CID, [(0, USER_INPUT, DONE), (1, GENERIC, DONE),
                                (2, PLANNER, GENERATING)], written=now - 2900)

    _go_quiet(driver, 1260)
    _tick(driver, monkeypatch)
    assert sum(p.get("category") == "stalled" for p in activity) == 1

    # Stdout moves again: the notice is retracted at once.
    driver._dispatch_message(_step(3, "tool", tool_name="run_command"))
    assert driver._stall_notice_emitted is False
    assert {"category": "phase", "phase": "requesting"} in activity

    # And a second long stall is announced again.
    _go_quiet(driver, 1300)
    _tick(driver, monkeypatch)
    assert sum(p.get("category") == "stalled" for p in activity) == 2
    assert _still_running(driver, attempt)
