"""Tests for provider-backed execution attempt reconciliation without replay."""

from __future__ import annotations

import json
import os
import sqlite3
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helios.backend import attempt_reconciler, model_catalog
from helios.backend.work_coordinator import WorkCoordinator
from helios.backend.work_store import (
    ExecutionAdmissionError,
    WorkStore,
    WorkStoreError,
)


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(rec) for rec in records) + "\n",
        encoding="utf-8",
    )
    return path


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
            last_modified_time DATETIME NOT NULL,
            parent_conversation_id TEXT NOT NULL DEFAULT ''
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


def _admit_dispatched(
    store: WorkStore,
    tmp_path: Path,
    *,
    provider: str,
    native_id: str,
    prompt_text: str = "verify deployment state",
    accepted_turn_id: str = "",
    hold_queue: bool = True,
):
    work = store.create_work(objective="reconcile test", cwd=str(tmp_path))
    participant = WorkCoordinator(store).bind_participant(work.work_id, provider, native_id=native_id)
    attempt = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider=provider,
        expected_participant_generation=participant.generation,
        workspace_root=str(tmp_path),
    )
    attempt = store.record_execution_dispatch(
        attempt.attempt_id,
        wire_prompt_text=prompt_text,
        provider_request_key=attempt.attempt_id,
    )
    if accepted_turn_id:
        attempt = store.record_execution_acceptance(
            attempt.attempt_id,
            accepted_turn_id=accepted_turn_id,
            provider_request_key=attempt.attempt_id,
        )
    if hold_queue:
        attempt = store.record_execution_stop(
            attempt.attempt_id,
            queue_disposition="held",
        )
    return work, participant, attempt


def test_pre_dispatch_attempt_reconciles_without_terminal_receipt(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        work = store.create_work(objective="pre-dispatch", cwd=str(tmp_path))
        participant = coordinator.bind_participant(
            work.work_id, model_catalog.PROVIDER_OPENAI, native_id="thr-pre"
        )
        attempt = store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider=model_catalog.PROVIDER_OPENAI,
            expected_participant_generation=participant.generation,
            workspace_root=str(tmp_path),
        )

        probe = attempt_reconciler.probe_attempt(attempt)
        assert probe.verdict == "confirmed_aborted"
        assert probe.provider_status == "pre_dispatch"
        assert probe.is_confirmed is True

        settled = coordinator.reconcile_provider_verified_attempt(
            attempt.attempt_id, probe
        )
        assert settled.status == "aborted"
        assert settled.terminal_reason == "reconciled.pre_dispatch"
        assert coordinator.active_attempts_for_work(work.work_id) == []
    finally:
        store.close()


def test_codex_rollout_task_complete_settles_without_replaying_held_queue(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        tid = "019da5da-97ba-7c90-810b-692c856625bd"
        turn_id = "019db210-1b26-7952-a9d3-1be08a80d05d"
        work, participant, attempt = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_OPENAI,
            native_id=tid,
            accepted_turn_id=turn_id,
            hold_queue=True,
        )
        sessions_dir = tmp_path / "codex-sessions"
        _write_jsonl(
            sessions_dir / "2026" / "11" / "23" / f"rollout-2026-11-23T00-00-00-{tid}.jsonl",
            [
                {"type": "session_meta", "payload": {"id": tid}},
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": turn_id},
                },
            ],
        )

        probe = attempt_reconciler.probe_attempt(
            attempt, codex_sessions_dir=sessions_dir
        )
        assert probe.verdict == "confirmed_completed"
        assert probe.provider_status == "task_complete"
        assert probe.matched_turn_id == turn_id
        assert probe.matched_native_id == tid

        settled = coordinator.reconcile_provider_verified_attempt(
            attempt.attempt_id, probe
        )
        assert settled.status == "completed"
        assert settled.terminal_reason == "reconciled.provider_completed"
        assert settled.queue_disposition == "discarded"
        assert settled.terminal_receipt["evidence_type"] == "provider_terminal"
        assert settled.terminal_receipt["provider_status"] == "task_complete"
        assert settled.terminal_receipt["turn_id"] == turn_id

        # Next attempt is now admitted cleanly.
        nxt = store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider=model_catalog.PROVIDER_OPENAI,
            expected_participant_generation=participant.generation,
            workspace_root=str(tmp_path),
        )
        assert nxt.status == "running"
    finally:
        store.close()


def test_codex_rollout_turn_aborted_and_missing_or_mismatched_turn_id(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        sessions_dir = tmp_path / "codex-sessions"

        # 1) turn_aborted confirms aborted
        tid1 = "thr-aborted-001"
        turn1 = "turn-aborted-001"
        _, _, att1 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_OPENAI,
            native_id=tid1,
            accepted_turn_id=turn1,
        )
        _write_jsonl(
            sessions_dir / f"rollout-{tid1}.jsonl",
            [{"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": turn1}}],
        )
        p1 = attempt_reconciler.probe_attempt(att1, codex_sessions_dir=sessions_dir)
        assert p1.verdict == "confirmed_aborted"
        assert p1.provider_status == "turn_aborted"
        s1 = coordinator.reconcile_provider_verified_attempt(att1.attempt_id, p1)
        assert s1.status == "aborted"

        # 2) Missing accepted_turn_id MUST remain uncertain even if rollout has task_complete
        tid2 = "thr-no-turn-002"
        _, _, att2 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_OPENAI,
            native_id=tid2,
            accepted_turn_id="",
        )
        _write_jsonl(
            sessions_dir / f"rollout-{tid2}.jsonl",
            [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "some-turn"}}],
        )
        p2 = attempt_reconciler.probe_attempt(att2, codex_sessions_dir=sessions_dir)
        assert p2.verdict == "uncertain"
        assert p2.is_confirmed is False
        with pytest.raises(WorkStoreError, match="uncertain"):
            coordinator.reconcile_provider_verified_attempt(att2.attempt_id, p2)

        # 3) Mismatched turn_id MUST remain uncertain
        tid3 = "thr-wrong-turn-003"
        _, _, att3 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_OPENAI,
            native_id=tid3,
            accepted_turn_id="turn-expected",
        )
        _write_jsonl(
            sessions_dir / f"rollout-{tid3}.jsonl",
            [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-other"}}],
        )
        p3 = attempt_reconciler.probe_attempt(att3, codex_sessions_dir=sessions_dir)
        assert p3.verdict == "uncertain"

        # 4) Symlink rollout MUST be rejected
        tid4 = "thr-symlink-004"
        _, _, att4 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_OPENAI,
            native_id=tid4,
            accepted_turn_id="turn-sym",
        )
        real_file = tmp_path / "outside.jsonl"
        _write_jsonl(
            real_file,
            [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-sym"}}],
        )
        (sessions_dir / f"rollout-{tid4}.jsonl").symlink_to(real_file)
        p4 = attempt_reconciler.probe_attempt(att4, codex_sessions_dir=sessions_dir)
        assert p4.verdict == "uncertain"
    finally:
        store.close()


def test_claude_native_transcript_end_turn_and_safety_guards(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        projects_dir = tmp_path / "claude-projects"
        proj_subdir = projects_dir / "-tmp-proj"

        # 1) Exact prompt digest + non-sidechain end_turn confirms completed
        sid1 = "535c584b-362d-489b-9526-764bfac31c34"
        prompt1 = "audit the backup job carefully"
        work1, part1, att1 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_ANTHROPIC,
            native_id=sid1,
            prompt_text=prompt1,
            hold_queue=True,
        )
        ts = att1.started_at
        _write_jsonl(
            proj_subdir / f"{sid1}.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": sid1,
                    "isSidechain": False,
                    "timestamp": ts,
                    "version": "2.1.330",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt1}],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": sid1,
                    "isSidechain": False,
                    "timestamp": ts,
                    "version": "2.1.330",
                    "message": {"role": "assistant", "stop_reason": "tool_use"},
                },
                {
                    "type": "user",
                    "sessionId": sid1,
                    "isSidechain": False,
                    "timestamp": ts,
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": sid1,
                    "isSidechain": False,
                    "timestamp": ts,
                    "version": "2.1.330",
                    "message": {"role": "assistant", "stop_reason": "end_turn"},
                },
            ],
        )
        p1 = attempt_reconciler.probe_attempt(att1, claude_projects_dir=projects_dir)
        assert p1.verdict == "confirmed_completed"
        assert p1.provider_status == "end_turn"
        s1 = coordinator.reconcile_provider_verified_attempt(att1.attempt_id, p1)
        assert s1.status == "completed"
        assert s1.queue_disposition == "discarded"
        assert s1.terminal_receipt["provider_status"] == "end_turn"
        assert store.start_execution_attempt(
            work1.work_id,
            part1.participant_id,
            provider=model_catalog.PROVIDER_ANTHROPIC,
            expected_participant_generation=part1.generation,
            workspace_root=str(tmp_path),
        ).status == "running"

        # 2) Mid-tool-loop crash + sidechain end_turn MUST stay uncertain
        sid2 = "048270a2-03fe-4e56-9603-ce2e829c7051"
        prompt2 = "run tool loop"
        _, _, att2 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_ANTHROPIC,
            native_id=sid2,
            prompt_text=prompt2,
        )
        _write_jsonl(
            proj_subdir / f"{sid2}.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": sid2,
                    "isSidechain": False,
                    "timestamp": att2.started_at,
                    "version": "2.1.330",
                    "message": {"role": "user", "content": prompt2},
                },
                {
                    "type": "assistant",
                    "sessionId": sid2,
                    "isSidechain": False,
                    "timestamp": att2.started_at,
                    "message": {"role": "assistant", "stop_reason": "tool_use"},
                },
                {
                    "type": "assistant",
                    "sessionId": sid2,
                    "isSidechain": True,
                    "timestamp": att2.started_at,
                    "message": {"role": "assistant", "stop_reason": "end_turn"},
                },
            ],
        )
        p2 = attempt_reconciler.probe_attempt(att2, claude_projects_dir=projects_dir)
        assert p2.verdict == "uncertain"
        assert "tool_use" in p2.detail

        # 3) Helios mirror transcript (version=helios-*) MUST be refused
        sid3 = "71a2c9d5-a136-4e25-a1ae-f82ea04579f5"
        prompt3 = "mirror prompt"
        _, _, att3 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_ANTHROPIC,
            native_id=sid3,
            prompt_text=prompt3,
        )
        _write_jsonl(
            proj_subdir / f"{sid3}.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": sid3,
                    "version": "helios-codex",
                    "timestamp": att3.started_at,
                    "message": {"role": "user", "content": prompt3},
                },
                {
                    "type": "assistant",
                    "sessionId": sid3,
                    "version": "helios-codex",
                    "timestamp": att3.started_at,
                    "message": {"role": "assistant", "stop_reason": "end_turn"},
                },
            ],
        )
        p3 = attempt_reconciler.probe_attempt(att3, claude_projects_dir=projects_dir)
        assert p3.verdict == "uncertain"
        assert "mirror" in p3.detail.lower()

        # 4) Explicit user interrupt marker confirms aborted
        sid4 = "48adce08-8e86-420d-8e57-c5df875bf0d5"
        prompt4 = "interrupt me"
        _, _, att4 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_ANTHROPIC,
            native_id=sid4,
            prompt_text=prompt4,
        )
        _write_jsonl(
            proj_subdir / f"{sid4}.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": sid4,
                    "isSidechain": False,
                    "timestamp": att4.started_at,
                    "version": "2.1.330",
                    "message": {"role": "user", "content": prompt4},
                },
                {
                    "type": "user",
                    "sessionId": sid4,
                    "isSidechain": False,
                    "timestamp": att4.started_at,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "[Request interrupted by user]"}],
                    },
                },
            ],
        )
        p4 = attempt_reconciler.probe_attempt(att4, claude_projects_dir=projects_dir)
        assert p4.verdict == "confirmed_aborted"
        assert p4.provider_status == "interrupted"

        # 5) Later distinct user turn's end_turn MUST NOT settle an unfinished earlier turn
        sid5 = "99adce08-8e86-420d-8e57-c5df875bf0d5"
        prompt5 = "first unfinished prompt"
        _, _, att5 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_ANTHROPIC,
            native_id=sid5,
            prompt_text=prompt5,
        )
        _write_jsonl(
            proj_subdir / f"{sid5}.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": sid5,
                    "isSidechain": False,
                    "timestamp": att5.started_at,
                    "version": "2.1.330",
                    "message": {"role": "user", "content": prompt5},
                },
                {
                    "type": "user",
                    "sessionId": sid5,
                    "isSidechain": False,
                    "timestamp": att5.started_at,
                    "message": {"role": "user", "content": "second unrelated prompt"},
                },
                {
                    "type": "assistant",
                    "sessionId": sid5,
                    "isSidechain": False,
                    "timestamp": att5.started_at,
                    "message": {"role": "assistant", "stop_reason": "end_turn"},
                },
            ],
        )
        p5 = attempt_reconciler.probe_attempt(att5, claude_projects_dir=projects_dir)
        assert p5.verdict == "uncertain"

        # 6) Stale identical prompt older than attempt.started_at MUST NOT match
        sid6 = "aaadce08-8e86-420d-8e57-c5df875bf0d5"
        prompt6 = "status"
        _, _, att6 = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_ANTHROPIC,
            native_id=sid6,
            prompt_text=prompt6,
        )
        _write_jsonl(
            proj_subdir / f"{sid6}.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": sid6,
                    "isSidechain": False,
                    "timestamp": "2020-01-01T00:00:00Z",
                    "version": "2.1.330",
                    "message": {"role": "user", "content": prompt6},
                },
                {
                    "type": "assistant",
                    "sessionId": sid6,
                    "isSidechain": False,
                    "timestamp": "2020-01-01T00:00:05Z",
                    "message": {"role": "assistant", "stop_reason": "end_turn"},
                },
            ],
        )
        p6 = attempt_reconciler.probe_attempt(att6, claude_projects_dir=projects_dir)
        assert p6.verdict == "uncertain"
    finally:
        store.close()


def test_google_summary_db_probe_confirmed_vs_running(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        cid = "905ea6fa-1111-2222-3333-444444444444"
        accepted = f"{cid}/user_input/122"
        _, _, att = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_GOOGLE,
            native_id=cid,
            accepted_turn_id=accepted,
        )
        started_epoch = attempt_reconciler._parse_iso_epoch(att.started_at)
        assert started_epoch is not None

        # The trajectory ends on this turn's finished response (step 128).
        conversations = tmp_path / "conversations"
        conversations.mkdir()
        trajectory = conversations / f"{cid}.db"
        conn = sqlite3.connect(trajectory)
        conn.execute("CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer, status integer)")
        conn.executemany(
            "INSERT INTO steps VALUES (?, ?, ?)",
            [(122, 14, 3), (123, 132, 3), (128, 15, 3)],
        )
        conn.commit()
        conn.close()
        os.utime(trajectory, (started_epoch + 10, started_epoch + 10))

        db_running = _make_summary_db(
            tmp_path / "running.db",
            [(cid, "CASCADE_RUN_STATUS_RUNNING", 1, 0, 129, 122, _iso_utc(started_epoch + 5))],
        )
        p_running = attempt_reconciler.probe_attempt(
            att,
            google_db_path=db_running,
            google_conversations_dir=conversations,
            now_epoch=started_epoch + 60,
        )
        assert p_running.verdict == "uncertain"

        db_idle = _make_summary_db(
            tmp_path / "idle.db",
            [(cid, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, _iso_utc(started_epoch + 10))],
        )
        # Too recent (< 30s quiet) stays uncertain.
        p_fresh = attempt_reconciler.probe_attempt(
            att,
            google_db_path=db_idle,
            google_conversations_dir=conversations,
            now_epoch=started_epoch + 20,
        )
        assert p_fresh.verdict == "uncertain"

        # An idle summary with no trajectory behind it proves nothing.
        p_blind = attempt_reconciler.probe_attempt(
            att,
            google_db_path=db_idle,
            google_conversations_dir=tmp_path / "no-trajectories",
            now_epoch=started_epoch + 50,
        )
        assert p_blind.verdict == "uncertain"

        # Quiet >= 30s with the trajectory's finished response confirms
        # completed and settles cleanly.
        p_idle = attempt_reconciler.probe_attempt(
            att,
            google_db_path=db_idle,
            google_conversations_dir=conversations,
            now_epoch=started_epoch + 50,
        )
        assert p_idle.verdict == "confirmed_completed"
        assert p_idle.provider_status == "CASCADE_RUN_STATUS_IDLE"
        settled = coordinator.reconcile_provider_verified_attempt(att.attempt_id, p_idle)
        assert settled.status == "completed"
        assert settled.terminal_receipt["turn_id"] == accepted
    finally:
        store.close()


def test_openrouter_always_requires_operator_confirmation_with_forensics(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        sid = "or-sess-1234"
        _, _, att = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_OPENROUTER,
            native_id=sid,
            prompt_text="secret prompt text that must never appear in probe detail",
            accepted_turn_id="gen-resp-987",
        )
        hist_dir = tmp_path / "openrouter-sessions"
        hist_dir.mkdir()
        (hist_dir / f"{sid}.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "messages": [
                        {"role": "user", "content": "secret prompt text"},
                        {"role": "assistant", "content": "partial SSE answer"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        probe = attempt_reconciler.probe_attempt(
            att, openrouter_history_dir=hist_dir
        )
        assert probe.verdict == "uncertain"
        assert probe.is_confirmed is False
        assert "gen-resp-987" in probe.detail
        assert "2 local history" in probe.detail
        assert "last role=assistant" in probe.detail
        assert "secret prompt text" not in probe.detail

        with pytest.raises(WorkStoreError, match="uncertain"):
            coordinator.reconcile_provider_verified_attempt(att.attempt_id, probe)
    finally:
        store.close()


def test_window_recovery_applies_verified_probes_and_holds_uncertain(monkeypatch, tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from helios.main_window import MainWindow
    from helios.window_recovery import WindowRecoveryMixin

    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        tid = "thr-win-rec-1"
        turn_id = "turn-win-rec-1"
        work, part, att = _admit_dispatched(
            store,
            tmp_path,
            provider=model_catalog.PROVIDER_OPENAI,
            native_id=tid,
            accepted_turn_id=turn_id,
            hold_queue=True,
        )

        class _FakeBanner:
            def __init__(self):
                self.revealed = False
                self.title = ""

            def set_revealed(self, value: bool) -> None:
                self.revealed = bool(value)

            def set_title(self, text: str) -> None:
                self.title = text

        banner = _FakeBanner()
        toasts: list[str] = []
        sync_calls: list[int] = []
        monkeypatch.setattr(
            MainWindow,
            "_sync_execution_control",
            lambda _self: (
                sync_calls.append(1),
                WindowRecoveryMixin._sync_stuck_attempt_banner(_self),
            ),
        )

        # 1) Live driver owning the attempt prevents premature reconciliation.
        live_drv = types.SimpleNamespace(
            execution_attempt_id=att.attempt_id,
            is_running=True,
        )
        win_live = types.SimpleNamespace(
            _destroyed=False,
            _work_coordinator=coordinator,
            _driver_manager=types.SimpleNamespace(
                drivers_for_shutdown=lambda: [live_drv]
            ),
            _stuck_attempt_banner=banner,
            _current_work_id=lambda: work.work_id,
            _toast=lambda msg: toasts.append(msg),
            _probing_orphan_works={work.work_id},
        )
        confirmed = attempt_reconciler.AttemptProbeResult(
            attempt_id=att.attempt_id,
            provider=model_catalog.PROVIDER_OPENAI,
            verdict="confirmed_completed",
            provider_status="task_complete",
            matched_turn_id=turn_id,
            matched_native_id=tid,
            source="codex_rollout",
            detail="confirmed",
        )
        WindowRecoveryMixin._apply_orphaned_attempt_probes(
            win_live, work.work_id, [confirmed]
        )
        assert len(coordinator.active_attempts_for_work(work.work_id)) == 1
        assert sync_calls == []

        # 2) Once the driver exited (no live owner), uncertain probe keeps Work blocked and shows banner.
        win_exited = types.SimpleNamespace(
            _destroyed=False,
            _work_coordinator=coordinator,
            _driver_manager=types.SimpleNamespace(drivers_for_shutdown=lambda: []),
            _stuck_attempt_banner=banner,
            _current_work_id=lambda: work.work_id,
            _toast=lambda msg: toasts.append(msg),
            _probing_orphan_works={work.work_id},
        )
        uncertain = attempt_reconciler.AttemptProbeResult(
            attempt_id=att.attempt_id,
            provider=model_catalog.PROVIDER_OPENAI,
            verdict="uncertain",
            source="codex_rollout",
            detail="missing terminal event",
        )
        WindowRecoveryMixin._apply_orphaned_attempt_probes(
            win_exited, work.work_id, [uncertain]
        )
        assert len(coordinator.active_attempts_for_work(work.work_id)) == 1
        assert banner.revealed is True
        with pytest.raises(ExecutionAdmissionError, match="already executing"):
            store.start_execution_attempt(
                work.work_id,
                part.participant_id,
                provider=model_catalog.PROVIDER_OPENAI,
                expected_participant_generation=part.generation,
                workspace_root=str(tmp_path),
            )

        # 3) Authoritative probe automatically settles without replay and hides banner.
        WindowRecoveryMixin._apply_orphaned_attempt_probes(
            win_exited, work.work_id, [confirmed]
        )
        assert coordinator.active_attempts_for_work(work.work_id) == []
        assert banner.revealed is False
        assert len(sync_calls) == 1
        assert any("Reconciled finished" in msg for msg in toasts)
        done = store.get_execution_attempt(att.attempt_id)
        assert done is not None and done.status == "completed"
        assert done.queue_disposition == "discarded"
    finally:
        store.close()
