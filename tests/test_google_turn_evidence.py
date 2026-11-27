"""Antigravity turn-completion evidence: the trajectory decides, never the summary row.

The summary index (``conversation_summaries.db``) trails the real trajectory
and reads idle mid-generation and while a subagent works. These tests pin the
rule that only the trajectory (``conversations/<id>.db``) and the trajectories
of the conversation's subagents can prove a turn finished.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helios.backend import attempt_reconciler, google_env, model_catalog
from helios.backend.work_coordinator import WorkCoordinator
from helios.backend.work_store import WorkStore

CID = "905ea6fa-1111-2222-3333-444444444444"
CHILD = "4866aaf4-5555-6666-7777-888888888888"
GRANDCHILD = "9c20aa0f-9999-aaaa-bbbb-cccccccccccc"

# Antigravity's CortexStepType / CortexStepStatus values, as agy stores them.
USER_INPUT = 14
PLANNER = 15  # planner response
GENERIC = 132  # an ordinary tool step
DONE = 3
GENERATING = 8
CANCELED = 6


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _summary_db(path: Path, rows: list[tuple]) -> Path:
    """rows: (conversation_id, status, not_fully_idle, killed, step_count,
    last_user_input_step_index, last_modified_epoch, parent_conversation_id)."""
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
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(*row[:6], _iso_utc(row[6]), row[7]) for row in rows],
    )
    conn.commit()
    conn.close()
    return path


def _trajectory(conversations: Path, cid: str, steps: list[tuple[int, int, int]],
                *, written: float) -> Path:
    """steps: (idx, step_type, status), as agy's ``steps`` table stores them."""
    conversations.mkdir(parents=True, exist_ok=True)
    path = conversations / f"{cid}.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE steps (idx integer, step_type integer NOT NULL DEFAULT 0, "
        "status integer NOT NULL DEFAULT 0, has_subtrajectory numeric NOT NULL DEFAULT false, "
        "metadata blob, step_payload blob, PRIMARY KEY (idx))"
    )
    conn.executemany("INSERT INTO steps (idx, step_type, status) VALUES (?, ?, ?)", steps)
    conn.commit()
    conn.close()
    os.utime(path, (written, written))
    return path


def _finished_turn(user_index: int = 122, final_index: int = 128) -> list[tuple[int, int, int]]:
    steps = [(i, GENERIC, DONE) for i in range(user_index)]
    steps.append((user_index, USER_INPUT, DONE))
    steps += [(i, GENERIC, DONE) for i in range(user_index + 1, final_index)]
    steps.append((final_index, PLANNER, DONE))
    return steps


@pytest.fixture
def agy(tmp_path):
    root = tmp_path / "antigravity-cli"
    root.mkdir()
    return root


def _assess(agy: Path, *, started: float, now: float, accepted: int | None = 122,
            quiet: float = 60.0) -> google_env.TurnEvidence:
    return google_env.assess_turn_completion(
        CID,
        turn_started_epoch=started,
        accepted_input_index=accepted,
        quiet_seconds=quiet,
        now_epoch=now,
        db_path=agy / "conversation_summaries.db",
        conversations_dir=agy / "conversations",
    )


def test_finished_turn_is_confirmed_by_its_trajectory(agy):
    now = time.time()
    started = now - 600
    _summary_db(agy / "conversation_summaries.db",
                [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, "")])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 120)

    evidence = _assess(agy, started=started, now=now)

    assert evidence.finished is True, evidence.reason
    assert evidence.trajectory.newest_index == 128
    assert evidence.progress_epoch == pytest.approx(now - 120, abs=1)


def test_stale_idle_summary_cannot_finish_a_generating_turn(agy):
    """The first false recovery: the summary row still said 10 idle steps
    while the trajectory held 86 and its newest response was generating."""
    now = time.time()
    started = now - 330
    _summary_db(agy / "conversation_summaries.db",
                [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 10, -1, started + 17, "")])
    steps = [(0, USER_INPUT, DONE)] + [(i, GENERIC, DONE) for i in range(1, 85)]
    steps.append((85, PLANNER, GENERATING))
    _trajectory(agy / "conversations", CID, steps, written=now - 100)

    evidence = _assess(agy, started=started, now=now, accepted=0)

    assert evidence.finished is False
    assert "generating" in evidence.reason


def test_canceled_response_is_not_a_finished_turn(agy):
    now = time.time()
    _summary_db(agy / "conversation_summaries.db",
                [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 44, -1, now - 300, "")])
    steps = [(0, USER_INPUT, DONE)] + [(i, GENERIC, DONE) for i in range(1, 43)]
    steps.append((43, PLANNER, CANCELED))
    _trajectory(agy / "conversations", CID, steps, written=now - 300)

    evidence = _assess(agy, started=now - 600, now=now, accepted=0)

    assert evidence.finished is False
    assert "not a finished response" in evidence.reason


def test_working_subagent_keeps_the_parent_turn_open(agy):
    """A parent can rest on a finished response, quiet and 'idle', while the
    subagent it started is still generating."""
    now = time.time()
    started = now - 900
    _summary_db(agy / "conversation_summaries.db", [
        (CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 400, ""),
        (CHILD, "CASCADE_RUN_STATUS_RUNNING", 1, 0, 170, -1, now - 300, CID),
    ])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 400)
    _trajectory(agy / "conversations", CHILD,
                [(0, USER_INPUT, DONE), (1, GENERIC, DONE), (2, PLANNER, GENERATING)],
                written=now - 5)

    evidence = _assess(agy, started=started, now=now)

    assert evidence.finished is False
    assert "subagent" in evidence.reason
    # The subagent's write counts as progress the stdout stream never showed.
    assert evidence.progress_epoch == pytest.approx(now - 5, abs=1)


def test_nested_subagent_writes_count_as_progress(agy):
    now = time.time()
    started = now - 900
    _summary_db(agy / "conversation_summaries.db", [
        (CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 400, ""),
        (CHILD, "CASCADE_RUN_STATUS_IDLE", 0, 0, 3, -1, now - 400, CID),
        (GRANDCHILD, "CASCADE_RUN_STATUS_IDLE", 0, 0, 3, -1, now - 400, CHILD),
    ])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 400)
    _trajectory(agy / "conversations", CHILD,
                [(0, USER_INPUT, DONE), (1, GENERIC, DONE), (2, PLANNER, DONE)],
                written=now - 400)
    _trajectory(agy / "conversations", GRANDCHILD,
                [(0, USER_INPUT, DONE), (1, GENERIC, DONE), (2, PLANNER, DONE)],
                written=now - 20)

    evidence = _assess(agy, started=started, now=now)

    assert evidence.finished is False
    assert GRANDCHILD[:8] in evidence.reason
    assert evidence.progress_epoch == pytest.approx(now - 20, abs=1)


def test_subagent_untouched_since_the_turn_began_is_ignored(agy):
    """A subagent left frozen mid-step by an earlier turn does not hold this
    turn open forever."""
    now = time.time()
    started = now - 600
    _summary_db(agy / "conversation_summaries.db", [
        (CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, ""),
        (CHILD, "CASCADE_RUN_STATUS_IDLE", 0, 0, 3, -1, started - 3600, CID),
    ])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 120)
    _trajectory(agy / "conversations", CHILD,
                [(0, USER_INPUT, DONE), (1, PLANNER, GENERATING)],
                written=started - 3600)

    assert _assess(agy, started=started, now=now).finished is True


@pytest.mark.parametrize(
    ("accepted", "why"),
    [(None, "uncorrelated"), (100, "an earlier input"), (130, "a later input")],
)
def test_final_response_must_follow_this_turns_own_input(agy, accepted, why):
    now = time.time()
    _summary_db(agy / "conversation_summaries.db",
                [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, "")])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 120)

    evidence = _assess(agy, started=now - 600, now=now, accepted=accepted)

    assert evidence.finished is False, why
    assert "this turn's prompt" in evidence.reason


def test_recent_trajectory_write_is_not_quiet_yet(agy):
    now = time.time()
    _summary_db(agy / "conversation_summaries.db",
                [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, "")])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 10)

    evidence = _assess(agy, started=now - 600, now=now)

    assert evidence.finished is False
    assert "moments ago" in evidence.reason


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        (("CASCADE_RUN_STATUS_RUNNING", 0, 0), "RUNNING"),
        (("CASCADE_RUN_STATUS_IDLE", 1, 0), "work outstanding"),
        (("CASCADE_RUN_STATUS_IDLE", 0, 1), "killed"),
    ],
)
def test_summary_row_can_still_veto(agy, row, fragment):
    now = time.time()
    status, not_fully_idle, killed = row
    _summary_db(agy / "conversation_summaries.db",
                [(CID, status, not_fully_idle, killed, 129, 122, now - 120, "")])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 120)

    evidence = _assess(agy, started=now - 600, now=now)

    assert evidence.finished is False
    assert fragment in evidence.reason


def test_summary_row_from_before_the_turn_is_not_agreement(agy):
    now = time.time()
    started = now - 600
    _summary_db(agy / "conversation_summaries.db",
                [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, started - 60, "")])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 120)

    evidence = _assess(agy, started=started, now=now)

    assert evidence.finished is False
    assert "predates" in evidence.reason


def test_unreadable_records_are_never_proof(agy):
    now = time.time()
    # No trajectory at all.
    _summary_db(agy / "conversation_summaries.db",
                [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, "")])
    assert _assess(agy, started=now - 600, now=now).finished is False

    # A trajectory that is not an agy steps table.
    conversations = agy / "conversations"
    conversations.mkdir()
    (conversations / f"{CID}.db").write_bytes(b"not a database")
    evidence = _assess(agy, started=now - 600, now=now)
    assert evidence.finished is False
    assert "could not be read" in evidence.reason

    # A readable trajectory with no summary database beside it.
    (conversations / f"{CID}.db").unlink()
    _trajectory(conversations, CID, _finished_turn(), written=now - 120)
    (agy / "conversation_summaries.db").unlink()
    assert _assess(agy, started=now - 600, now=now).finished is False


def test_trajectory_last_write_ignores_an_empty_wal(tmp_path):
    now = time.time()
    conversations = tmp_path / "conversations"
    _trajectory(conversations, CID, _finished_turn(), written=now - 300)
    wal = conversations / f"{CID}.db-wal"

    # A read-only reader can create an empty WAL; that is not agy writing.
    wal.write_bytes(b"")
    os.utime(wal, (now, now))
    snap = google_env.probe_conversation_trajectory(CID, conversations_dir=conversations)
    assert snap.last_write_epoch == pytest.approx(now - 300, abs=1)

    # A WAL holding frames is a write.
    wal.write_bytes(b"\0" * 32)
    os.utime(wal, (now - 5, now - 5))
    snap = google_env.probe_conversation_trajectory(CID, conversations_dir=conversations)
    assert snap.last_write_epoch == pytest.approx(now - 5, abs=1)
    assert snap.newest_index == 128
    assert snap.last_user_input_index == 122
    assert snap.ends_with_final_response is True


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", "a\\b", ".", ".."])
def test_trajectory_probe_refuses_unsafe_ids(tmp_path, bad):
    assert google_env.probe_conversation_trajectory(bad, conversations_dir=tmp_path) is None


def test_open_conversation_ids_reads_an_agy_process_descriptors(tmp_path):
    conversations = tmp_path / "antigravity-cli" / "conversations"
    conversations.mkdir(parents=True)
    fd = tmp_path / "proc" / "4242" / "fd"
    fd.mkdir(parents=True)
    os.symlink(conversations / f"{CHILD}.db", fd / "8")
    os.symlink(conversations / f"{CHILD}.db-wal", fd / "14")
    os.symlink(conversations / f"{CID}.db-shm", fd / "15")
    os.symlink(tmp_path / "antigravity-cli" / "log" / "cli.log", fd / "1")
    os.symlink(tmp_path / "elsewhere" / f"{GRANDCHILD}.db", fd / "9")

    ids = google_env.open_conversation_ids(
        4242, proc_root=tmp_path / "proc", conversations_dir=conversations
    )

    assert ids == frozenset({CHILD, CID})
    assert google_env.open_conversation_ids(
        4343, proc_root=tmp_path / "proc", conversations_dir=conversations
    ) is None
    assert google_env.open_conversation_ids(0) is None


def test_accepted_input_index_parses_only_this_conversations_ids():
    assert google_env.accepted_input_index(f"{CID}/user_input/44", CID) == 44
    assert google_env.accepted_input_index(f"{CID}/user_input/0", CID) == 0
    assert google_env.accepted_input_index(f"{CID}/result/3", CID) is None
    assert google_env.accepted_input_index(f"{CHILD}/user_input/44", CID) is None
    assert google_env.accepted_input_index(f"{CID}/user_input/-1", CID) is None
    assert google_env.accepted_input_index("", CID) is None


def test_child_conversation_ids_walks_nested_subagents(tmp_path):
    now = time.time()
    db = _summary_db(tmp_path / "summaries.db", [
        (CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 1, -1, now, ""),
        (CHILD, "CASCADE_RUN_STATUS_IDLE", 0, 0, 1, -1, now, CID),
        (GRANDCHILD, "CASCADE_RUN_STATUS_IDLE", 0, 0, 1, -1, now, CHILD),
        ("unrelated-1111", "CASCADE_RUN_STATUS_IDLE", 0, 0, 1, -1, now, "other-parent"),
    ])
    assert google_env.child_conversation_ids(CID, db_path=db) == (CHILD, GRANDCHILD)
    assert google_env.child_conversation_ids(CID, db_path=tmp_path / "missing.db") is None

    # An agy without subagents has no parent links to follow.
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert google_env.child_conversation_ids(CID, db_path=legacy) == ()


def test_child_conversation_ids_covers_the_whole_tree_or_refuses(tmp_path, monkeypatch):
    now = time.time()
    chain = [CID] + [f"deep{n:04d}-aaaa-bbbb-cccc-dddddddddddd" for n in range(6)]
    rows = [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 1, -1, now, "")]
    rows += [(child, "CASCADE_RUN_STATUS_IDLE", 0, 0, 1, -1, now, parent)
             for parent, child in zip(chain, chain[1:])]
    db = _summary_db(tmp_path / "summaries.db", rows)
    # No depth limit: a subagent six levels down is still covered.
    assert google_env.child_conversation_ids(CID, db_path=db) == tuple(chain[1:])

    # Past the inspection limit Helios refuses rather than returning a prefix.
    monkeypatch.setattr(google_env, "_MAX_SUBAGENTS", 3)
    assert google_env.child_conversation_ids(CID, db_path=db) is None


def test_active_subagent_after_many_historical_ones_is_still_seen(agy):
    """Subagents accumulate over a conversation's life; the newest working one
    must not be hidden behind older ones."""
    now = time.time()
    started = now - 600
    rows = [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, "")]
    for n in range(80):
        old = f"hist{n:04d}-aaaa-bbbb-cccc-dddddddddddd"
        rows.append((old, "CASCADE_RUN_STATUS_IDLE", 0, 0, 3, -1, started - 7200, CID))
        _trajectory(agy / "conversations", old,
                    [(0, USER_INPUT, DONE), (1, GENERIC, DONE), (2, PLANNER, DONE)],
                    written=started - 7200)
    rows.append((CHILD, "CASCADE_RUN_STATUS_IDLE", 0, 0, 3, -1, now - 300, CID))
    _summary_db(agy / "conversation_summaries.db", rows)
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 120)
    _trajectory(agy / "conversations", CHILD,
                [(0, USER_INPUT, DONE), (1, PLANNER, GENERATING)], written=now - 3)

    evidence = _assess(agy, started=started, now=now)

    assert evidence.finished is False
    assert CHILD[:8] in evidence.reason


def test_unreadable_subagent_touched_this_turn_is_not_settled(agy):
    now = time.time()
    started = now - 600
    _summary_db(agy / "conversation_summaries.db", [
        (CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, ""),
        (CHILD, "CASCADE_RUN_STATUS_IDLE", 0, 0, 3, -1, now - 300, CID),
    ])
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 120)
    child = agy / "conversations" / f"{CHILD}.db"
    child.write_bytes(b"locked or not an agy trajectory")
    os.utime(child, (now - 300, now - 300))

    evidence = _assess(agy, started=started, now=now)

    assert evidence.finished is False
    assert "could not be read" in evidence.reason

    # The same unreadable file, untouched since before the turn, is history.
    os.utime(child, (started - 3600, started - 3600))
    assert _assess(agy, started=started, now=now).finished is True


# ── Orphaned attempts (no live driver) use the same evidence ──────────────


def _orphaned_google_attempt(store: WorkStore, tmp_path: Path, accepted: str):
    work = store.create_work(objective="orphan test", cwd=str(tmp_path))
    participant = WorkCoordinator(store).bind_participant(
        work.work_id, model_catalog.PROVIDER_GOOGLE, native_id=CID
    )
    attempt = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider=model_catalog.PROVIDER_GOOGLE,
        expected_participant_generation=participant.generation,
        workspace_root=str(tmp_path),
    )
    attempt = store.record_execution_dispatch(
        attempt.attempt_id, wire_prompt_text="review it", provider_request_key=attempt.attempt_id
    )
    attempt = store.record_execution_acceptance(
        attempt.attempt_id, accepted_turn_id=accepted, provider_request_key=attempt.attempt_id
    )
    return store.record_execution_stop(attempt.attempt_id, queue_disposition="held")


def test_orphan_probe_does_not_confirm_a_turn_that_died_mid_generation(tmp_path, monkeypatch):
    """A crashed turn whose stale summary row reads idle must stay uncertain."""
    home = tmp_path / "home"
    agy = home / ".gemini" / "antigravity-cli"
    agy.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    store = WorkStore(tmp_path / "work.db")
    try:
        att = _orphaned_google_attempt(store, tmp_path, f"{CID}/user_input/0")
        started = attempt_reconciler._parse_iso_epoch(att.started_at)
        _summary_db(agy / "conversation_summaries.db",
                    [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 10, -1, started + 17, "")])
        steps = [(0, USER_INPUT, DONE)] + [(i, GENERIC, DONE) for i in range(1, 85)]
        steps.append((85, PLANNER, GENERATING))
        _trajectory(agy / "conversations", CID, steps, written=started + 330)

        probe = attempt_reconciler.probe_attempt(
            att,
            google_db_path=agy / "conversation_summaries.db",
            now_epoch=started + 1200,
        )

        assert probe.verdict == "uncertain"
        assert probe.is_confirmed is False
        assert "generating" in probe.detail
    finally:
        store.close()


def test_orphan_probe_confirms_a_turn_its_trajectory_finished(tmp_path):
    agy = tmp_path / "antigravity-cli"
    agy.mkdir()
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        att = _orphaned_google_attempt(store, tmp_path, f"{CID}/user_input/122")
        started = attempt_reconciler._parse_iso_epoch(att.started_at)
        _summary_db(agy / "conversation_summaries.db",
                    [(CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, started + 20, "")])
        _trajectory(agy / "conversations", CID, _finished_turn(), written=started + 20)

        probe = attempt_reconciler.probe_attempt(
            att,
            google_db_path=agy / "conversation_summaries.db",
            google_conversations_dir=agy / "conversations",
            now_epoch=started + 120,
        )

        assert probe.verdict == "confirmed_completed", probe.detail
        assert probe.source == "antigravity_trajectory"
        settled = coordinator.reconcile_provider_verified_attempt(att.attempt_id, probe)
        assert settled.status == "completed"
    finally:
        store.close()


def test_subagent_without_a_trajectory_is_unproven_unless_it_predates_the_turn(agy):
    """A listed subagent whose trajectory cannot be statted (not yet created,
    removed, or out of reach) is only ignored when its own summary row shows it
    belongs to an earlier turn."""
    now = time.time()
    started = now - 600
    _trajectory(agy / "conversations", CID, _finished_turn(), written=now - 120)

    # Created during this turn, no trajectory yet: not settled.
    _summary_db(agy / "conversation_summaries.db", [
        (CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, ""),
        (CHILD, "CASCADE_RUN_STATUS_RUNNING", 1, 0, 0, -1, now - 300, CID),
    ])
    evidence = _assess(agy, started=started, now=now)
    assert evidence.finished is False
    assert "has no trajectory to check" in evidence.reason

    # From an earlier turn, trajectory since removed: history.
    (agy / "conversation_summaries.db").unlink()
    _summary_db(agy / "conversation_summaries.db", [
        (CID, "CASCADE_RUN_STATUS_IDLE", 0, 0, 129, 122, now - 120, ""),
        (CHILD, "CASCADE_RUN_STATUS_IDLE", 0, 0, 3, -1, started - 3600, CID),
    ])
    assert _assess(agy, started=started, now=now).finished is True
