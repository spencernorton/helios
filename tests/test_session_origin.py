"""Agent-launched Claude sessions are told apart from the user's own.

A headless `claude -p` batch in $HOME flooded the sidebar with hundreds of
rows the user never started (2026-11-23). Helios's own chats are also
`entrypoint: sdk-cli`, so the separating fact is the Work-store binding.
"""

from __future__ import annotations

import json
import sqlite3
import time

from helios.backend import session_origin as SO
from helios.backend.projects import Project, Session


def _transcript(path, entrypoint: str | None, *, preamble: int = 3) -> None:
    lines = [
        json.dumps({"type": "queue-operation", "content": "x" * 4000})
        for _ in range(preamble)
    ]
    record = {"type": "user", "message": {"role": "user", "content": "hi"}}
    if entrypoint is not None:
        record["entrypoint"] = entrypoint
    lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session(tmp_path, sid: str, entrypoint: str | None, *, read_only=False):
    path = tmp_path / f"{sid}.jsonl"
    _transcript(path, entrypoint)
    project = Project(
        dirname="-home-alice",
        cwd="/home/alice",
        path=tmp_path,
        origin="macbook" if read_only else "local",
        read_only=read_only,
    )
    return Session(
        project=project,
        session_id=sid,
        path=path,
        mtime=time.time(),
        size=path.stat().st_size,
    )


def _work_db(path, *, driven=(), attempted=(), viewed=()):
    """A minimal Work store: one Work + participant + binding per native id.

    `driven` Works carry a user.message event, `attempted` ones an
    execution_attempts row, `viewed` ones neither (lazily attached on open)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE participants (participant_id TEXT, work_id TEXT,
                                   native_session_id TEXT);
        CREATE TABLE native_bindings (binding_id TEXT, participant_id TEXT,
                                      provider TEXT, native_id TEXT);
        CREATE TABLE execution_attempts (attempt_id TEXT, work_id TEXT);
        CREATE TABLE events (event_id TEXT, work_id TEXT, event_type TEXT);
        """
    )
    for kind, ids in (("driven", driven), ("attempted", attempted), ("viewed", viewed)):
        for nid in ids:
            work, part = f"w-{nid}", f"p-{nid}"
            conn.execute("INSERT INTO participants VALUES (?, ?, ?)", (part, work, nid))
            conn.execute(
                "INSERT INTO native_bindings VALUES (?, ?, 'anthropic', ?)",
                (f"b-{nid}", part, nid),
            )
            conn.execute(
                "INSERT INTO events VALUES (?, ?, 'work.created')", (f"e0-{nid}", work)
            )
            if kind == "driven":
                conn.execute(
                    "INSERT INTO events VALUES (?, ?, 'user.message')",
                    (f"e1-{nid}", work),
                )
            if kind == "attempted":
                conn.execute(
                    "INSERT INTO execution_attempts VALUES (?, ?)", (f"a-{nid}", work)
                )
    conn.commit()
    conn.close()


def test_entrypoint_found_after_preamble(tmp_path):
    p = tmp_path / "a.jsonl"
    _transcript(p, "sdk-cli")
    assert SO.transcript_entrypoint(p) == "sdk-cli"
    assert SO.is_headless_transcript(p)


def test_interactive_and_mirror_transcripts_are_not_headless(tmp_path):
    cli = tmp_path / "cli.jsonl"
    _transcript(cli, "cli")
    mirror = tmp_path / "mirror.jsonl"
    _transcript(mirror, None)
    assert not SO.is_headless_transcript(cli)
    assert not SO.is_headless_transcript(mirror)
    assert SO.transcript_entrypoint(tmp_path / "missing.jsonl") == ""


def test_scan_is_bounded(tmp_path):
    p = tmp_path / "late.jsonl"
    _transcript(p, "sdk-cli", preamble=SO._SCAN_MAX_LINES + 5)
    assert SO.transcript_entrypoint(p) == ""


def test_miss_is_not_cached(tmp_path):
    p = tmp_path / "fresh.jsonl"
    p.write_text("", encoding="utf-8")
    assert SO.transcript_entrypoint(p) == ""
    _transcript(p, "sdk-py")
    assert SO.transcript_entrypoint(p) == "sdk-py"


def test_owned_ids_require_a_turn_helios_sent(tmp_path):
    """Opening a batch transcript in the sidebar mints a Work for it; that
    alone must not make it look like the user's own chat."""
    db = tmp_path / "work.db"
    _work_db(db, driven=["s-msg"], attempted=["s-att"], viewed=["s-viewed"])
    assert SO.helios_owned_session_ids(db) == frozenset({"s-msg", "s-att"})


def test_owned_ids_against_the_real_schema(tmp_path):
    """The query must run against the schema WorkStore actually creates."""
    from helios.backend.work_store import WorkStore

    db = tmp_path / "work" / "work.db"
    store = WorkStore(db)
    store.ensure_work_for_native("anthropic", "viewed-only", cwd="/home/alice")
    sent = store.ensure_work_for_native("anthropic", "sent-in", cwd="/home/alice")
    store.append_event(sent.work_id, "user.message", {"text": "hi"})
    assert SO.helios_owned_session_ids(db) == frozenset({"sent-in"})


def test_owned_ids_unknown_when_store_unreadable(tmp_path):
    assert SO.helios_owned_session_ids(tmp_path / "absent.db") is None
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    assert SO.helios_owned_session_ids(junk) is None


def test_classification(tmp_path):
    batch = _session(tmp_path, "batch", "sdk-cli")
    helios = _session(tmp_path, "helios", "sdk-cli")
    live = _session(tmp_path, "live", "sdk-cli")
    terminal = _session(tmp_path, "terminal", "cli")
    gemini = _session(tmp_path, "gemini", None)
    remote = _session(tmp_path, "remote", "sdk-cli", read_only=True)
    pending = _session(tmp_path, "pending", "sdk-cli")
    pending.pending = True
    got = SO.agent_launched_ids(
        [batch, helios, live, terminal, gemini, remote, pending],
        owned_ids=frozenset({"helios"}),
        keep_ids={"live"},
    )
    assert got == {"batch"}


def test_fails_open_without_ownership(tmp_path):
    batch = _session(tmp_path, "batch", "sdk-cli")
    assert SO.agent_launched_ids([batch], owned_ids=None) == set()


# ── Sidebar filters ──────────────────────────────────────────────────


def _sidebar_or_skip():
    import pytest

    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError:  # pragma: no cover - host without GTK4/Adw typelibs
        pytest.skip("Gtk 4.0 / Adw 1 unavailable")
    from gi.repository import Adw

    from helios.widgets import session_list as SL

    Adw.init()
    return SL


def test_sidebar_hides_agent_runs_behind_their_own_filter(tmp_path):
    SL = _sidebar_or_skip()
    mine = _session(tmp_path, "mine", "sdk-cli")
    batch = _session(tmp_path, "batch", "sdk-cli")
    widget = SL.SessionList()
    try:
        widget._local_sessions = [mine, batch]
        widget._pool_sessions = []
        widget._agent_session_ids = {"batch"}
        widget._rebuild_filter_choices()
        assert SL.FILTER_AGENT in widget._filter_ids
        # The batch's cwd is shared with a real chat, so it keeps its choice;
        # no extra cwd entry is minted for agent-only dirs.
        assert widget._filter_ids.count("cwd:/home/alice") == 1

        for f in (SL.FILTER_ALL, SL.FILTER_LOCAL, "cwd:/home/alice"):
            widget._filter = f
            assert widget._matches_filter(mine)
            assert not widget._matches_filter(batch), f
        widget._filter = SL.FILTER_AGENT
        assert widget._matches_filter(batch)
        assert not widget._matches_filter(mine)

        widget._agent_session_ids = set()
        widget._rebuild_filter_choices()
        assert SL.FILTER_AGENT not in widget._filter_ids
        assert widget._filter == SL.FILTER_ALL  # stale choice falls back
    finally:
        widget.shutdown()


def test_agent_only_cwd_gets_no_filter_choice(tmp_path):
    SL = _sidebar_or_skip()
    batch = _session(tmp_path, "batch", "sdk-cli")
    widget = SL.SessionList()
    try:
        widget._local_sessions = [batch]
        widget._pool_sessions = []
        widget._agent_session_ids = {"batch"}
        widget._rebuild_filter_choices()
        assert not any(i.startswith("cwd:") for i in widget._filter_ids)
    finally:
        widget.shutdown()
