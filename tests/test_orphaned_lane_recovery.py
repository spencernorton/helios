"""Startup must free the lanes a Helios that died mid-turn still holds.

The store-level release is covered in `test_work_store.py`. What this file
covers is that *the window actually calls it*: the bug was never that the
release was wrong, it was that nothing ever ran one, so a chat orphaned by a
restart refused every later message for good.
"""

from __future__ import annotations

import types

import pytest

from helios.backend.work_store import ExecutionAdmissionError, WorkStore

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios.main_window import MainWindow  # noqa: E402


def _bricked_chat(tmp_path):
    """A Work whose turn was dispatched and whose Helios then died."""

    store = WorkStore(tmp_path / "work" / "work.db")
    work = store.create_work(objective="orphaned", cwd=str(tmp_path))
    participant = store.bind_participant(work.work_id, "anthropic")
    attempt = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
        workspace_root=str(tmp_path),
    )
    store.record_execution_dispatch(
        attempt.attempt_id,
        wire_prompt_text="the turn that never came back",
        provider_request_key=attempt.attempt_id,
    )

    def send_again():
        return store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="anthropic",
            expected_participant_generation=participant.generation,
            workspace_root=str(tmp_path),
        )

    return store, attempt, send_again


def test_startup_reclaims_the_lane_so_the_chat_answers_again(tmp_path):
    store, orphan, send_again = _bricked_chat(tmp_path)
    with pytest.raises(ExecutionAdmissionError, match="already executing"):
        send_again()
    window = types.SimpleNamespace(
        _work_coordinator=types.SimpleNamespace(
            reclaim_orphaned_execution_attempts=(
                store.reclaim_orphaned_execution_attempts
            )
        )
    )

    MainWindow._reclaim_orphaned_execution_lanes(window)

    assert store.get_execution_attempt(orphan.attempt_id).status == "aborted"
    assert send_again().status == "running"


def test_startup_survives_a_store_that_cannot_be_swept(tmp_path):
    """Recovery is best-effort: a failed sweep must not stop Helios opening."""

    def boom():
        raise RuntimeError("work.db is locked")

    window = types.SimpleNamespace(
        _work_coordinator=types.SimpleNamespace(
            reclaim_orphaned_execution_attempts=boom
        )
    )

    MainWindow._reclaim_orphaned_execution_lanes(window)  # must not raise


def test_startup_without_the_work_graph_is_a_no_op():
    MainWindow._reclaim_orphaned_execution_lanes(types.SimpleNamespace())


def test_startup_reaps_orphaned_children_before_releasing_lanes(tmp_path, monkeypatch):
    from helios.backend.process.child_registry import OrphanReapOutcome

    store, orphan, send_again = _bricked_chat(tmp_path)
    order: list[str] = []

    def fake_reap(*, registry_path=None, grace_seconds=1.0):
        order.append(f"reap:{registry_path}")
        return OrphanReapOutcome(
            reaped=({"pid": 12345, "pgid": 12345, "provider": "anthropic", "owner_pid": 9999},)
        )

    def wrapped_reclaim():
        order.append("reclaim")
        return store.reclaim_orphaned_execution_attempts()

    monkeypatch.setattr(
        "helios.window_recovery.child_registry.reap_orphaned_children",
        fake_reap,
    )
    window = types.SimpleNamespace(
        _work_coordinator=types.SimpleNamespace(
            store=store,
            reclaim_orphaned_execution_attempts=wrapped_reclaim,
        )
    )

    MainWindow._reclaim_orphaned_execution_lanes(window)

    expected_registry = tmp_path / "live-children.json"
    assert order == [f"reap:{expected_registry}", "reclaim"]
    assert store.get_execution_attempt(orphan.attempt_id).status == "aborted"
    assert send_again().status == "running"


def test_startup_keeps_lanes_blocked_when_orphan_cleanup_fails_or_is_unresolved(
    tmp_path, monkeypatch
):
    from helios.backend.process.child_registry import OrphanReapOutcome

    for label, behavior in (
        (
            "remaining",
            lambda **_kw: OrphanReapOutcome(
                remaining=({"pid": 22222, "pgid": 22222, "provider": "anthropic", "owner_pid": 9999},)
            ),
        ),
        (
            "unresolved",
            lambda **_kw: OrphanReapOutcome(
                unresolved=({"pid": 33333, "pgid": 33333, "provider": "anthropic", "owner_pid": 9999},)
            ),
        ),
        (
            "error",
            lambda **_kw: OrphanReapOutcome(error="child registry contains invalid JSON"),
        ),
        (
            "exception",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("registry failure")),
        ),
    ):
        case_dir = tmp_path / label
        case_dir.mkdir()
        store, orphan, send_again = _bricked_chat(case_dir)
        monkeypatch.setattr(
            "helios.window_recovery.child_registry.reap_orphaned_children",
            behavior,
        )
        window = types.SimpleNamespace(
            _work_coordinator=types.SimpleNamespace(
                store=store,
                reclaim_orphaned_execution_attempts=store.reclaim_orphaned_execution_attempts,
            )
        )
        MainWindow._reclaim_orphaned_execution_lanes(window)
        assert store.get_execution_attempt(orphan.attempt_id).status == "running", label
        with pytest.raises(Exception):
            send_again()


def test_startup_keeps_lanes_blocked_on_real_corrupt_registry_file(tmp_path):
    store, orphan, _ = _bricked_chat(tmp_path)
    (tmp_path / "live-children.json").write_text("{corrupt", encoding="utf-8")
    window = types.SimpleNamespace(
        _work_coordinator=types.SimpleNamespace(
            store=store,
            reclaim_orphaned_execution_attempts=store.reclaim_orphaned_execution_attempts,
        )
    )
    MainWindow._reclaim_orphaned_execution_lanes(window)
    assert store.get_execution_attempt(orphan.attempt_id).status == "running"


def test_failed_orphan_cleanup_blocks_reconciliation_and_admission_until_retry_succeeds(
    tmp_path, monkeypatch
):
    """Pass 2 Finding 3: reconciliation and new sends stay blocked until cleanup succeeds."""
    from helios.backend.process.child_registry import OrphanReapOutcome
    from helios.window_recovery import WindowRecoveryMixin

    store, orphan, _ = _bricked_chat(tmp_path)
    work_id = store.list_works()[0].work_id
    other_work = store.create_work(
        objective="unrelated work",
        cwd=str(tmp_path),
    )

    state = {"remaining": True, "calls": 0}

    def fake_reap(*, registry_path=None, grace_seconds=1.0, lock_timeout=None):
        state["calls"] += 1
        if state["remaining"]:
            return OrphanReapOutcome(
                remaining=({"pid": 44444, "pgid": 44444, "provider": "anthropic", "owner_pid": 9999},)
            )
        return OrphanReapOutcome()

    monkeypatch.setattr(
        "helios.window_recovery.child_registry.reap_orphaned_children",
        fake_reap,
    )
    toasts: list[str] = []
    window = types.SimpleNamespace(
        _destroyed=False,
        _orphan_cleanup_block_reason="",
        _next_chat=None,
        _toast=toasts.append,
        _work_coordinator=types.SimpleNamespace(
            store=store,
            reclaim_orphaned_execution_attempts=store.reclaim_orphaned_execution_attempts,
            active_attempts_for_work=store.list_active_execution_attempts_for_work,
            reconcile_uncertain_attempt=store.reconcile_operator_aborted_attempt,
            execution_block_reason=lambda _wid: "",
        ),
    )

    # 1. Startup fails cleanup -> attempt stays running and block is recorded.
    MainWindow._reclaim_orphaned_execution_lanes(window)
    assert store.get_execution_attempt(orphan.attempt_id).status == "running"
    assert window._orphan_cleanup_block_reason

    # 2. Operator reconciliation cannot bypass the failed cleanup block.
    cleared = WindowRecoveryMixin._reconcile_orphaned_attempts_confirmed(
        window, work_id, [orphan.attempt_id]
    )
    assert cleared == 0
    assert store.get_execution_attempt(orphan.attempt_id).status == "running"

    # 3. Global admission blocks even an unrelated Work and _ensure_driver().
    block = MainWindow._work_execution_block_reason(window, other_work.work_id)
    assert "previous agent processes stopped" in block
    assert MainWindow._ensure_driver(window) is False
    assert toasts

    # 4. Once cleanup succeeds on retry, block clears and reconciliation works.
    state["remaining"] = False
    cleared = WindowRecoveryMixin._reconcile_orphaned_attempts_confirmed(
        window, work_id, [orphan.attempt_id]
    )
    assert cleared == 1
    assert window._orphan_cleanup_block_reason == ""
    assert store.get_execution_attempt(orphan.attempt_id).status == "aborted"
    assert MainWindow._work_execution_block_reason(window, other_work.work_id) == ""
