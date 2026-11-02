"""A deleted or archived session never comes back as a dead "Session <id>" row.

Deleting a session stops its claude process asynchronously (SIGTERM, SIGKILL
backstop at 1.6 s) and unlinks the transcript at once. The dying process then
appends its exit trailer to ``<id>.jsonl`` — recreating the file — and the next
reload listed it: a no-content stub with a fresh mtime sits inside the
sidecar display grace. Observed 2026-11-02 with exactly the three records
below.
"""

from __future__ import annotations

import json
import time

import pytest

import helios.backend.projects as P

# The exact records a terminated claude wrote into a just-deleted transcript.
EXIT_TRAILER = [
    {"type": "last-prompt", "lastPrompt": "hi", "leafUuid": "u1", "sessionId": "doomed"},
    {"type": "atis-latch", "atis": "", "sessionId": "doomed"},
    {"type": "cost-state", "sessionId": "doomed", "totalCostUSD": 0.57},
]


def _user_line(text: str = "hello") -> str:
    return json.dumps(
        {
            "type": "user",
            "cwd": "/home/alice/proj",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    )


def _write(path, records) -> None:
    path.write_text(
        "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records),
        encoding="utf-8",
    )


def _project(tmp_path, *, read_only: bool = False) -> P.Project:
    d = tmp_path / "-home-alice-proj"
    d.mkdir(exist_ok=True)
    return P.Project(
        dirname=d.name,
        cwd="/home/alice/proj",
        path=d,
        origin="macbook" if read_only else "local",
        read_only=read_only,
    )


def _session(project: P.Project, sid: str, records) -> P.Session:
    path = project.path / f"{sid}.jsonl"
    _write(path, records)
    return P.Session(
        project=project, session_id=sid, path=path, mtime=time.time(), size=1
    )


def _no_archive(_s):
    pytest.fail("a deleted session must not be archived")


# ── purge_removed_sessions (GTK-free) ────────────────────────────────────────


def test_a_rewritten_deleted_transcript_is_hidden_and_unlinked(tmp_path):
    project = _project(tmp_path)
    ghost = _session(project, "doomed", EXIT_TRAILER)
    aux = ghost.path.with_suffix("")
    aux.mkdir()
    kept = _session(project, "kept", [_user_line()])

    out = P.purge_removed_sessions(
        [ghost, kept], {"doomed": P.REMOVED_BY_DELETE}, archive=_no_archive
    )

    assert [s.session_id for s in out] == ["kept"]
    assert not ghost.path.exists() and not aux.exists()
    assert kept.path.exists()


def test_late_conversation_content_does_not_resurrect_it(tmp_path):
    """A turn in flight at delete time can land after the unlink. The user
    deleted the session, so content does not earn the row back."""
    ghost = _session(_project(tmp_path), "doomed", [_user_line("late")])
    out = P.purge_removed_sessions(
        [ghost], {"doomed": P.REMOVED_BY_DELETE}, archive=_no_archive
    )
    assert out == [] and not ghost.path.exists()


def test_an_archived_session_rewrite_follows_it_into_the_archive(tmp_path):
    ghost = _session(_project(tmp_path), "doomed", EXIT_TRAILER)
    archived: list[str] = []

    def archive(s):
        archived.append(s.session_id)
        s.path.unlink()

    out = P.purge_removed_sessions(
        [ghost], {"doomed": P.REMOVED_BY_ARCHIVE}, archive=archive
    )
    assert out == [] and archived == ["doomed"]


def test_an_unremovable_rewrite_is_still_hidden(tmp_path):
    ghost = _session(_project(tmp_path), "doomed", EXIT_TRAILER)

    def archive(_s):
        raise OSError("disk full")

    out = P.purge_removed_sessions(
        [ghost], {"doomed": P.REMOVED_BY_ARCHIVE}, archive=archive
    )
    assert out == [] and ghost.path.exists()


def test_read_only_pool_rows_are_never_touched(tmp_path):
    pool = _session(_project(tmp_path, read_only=True), "doomed", [_user_line()])
    out = P.purge_removed_sessions(
        [pool], {"doomed": P.REMOVED_BY_DELETE}, archive=_no_archive
    )
    assert out == [pool] and pool.path.exists()


def test_nothing_removed_is_a_passthrough(tmp_path):
    s = _session(_project(tmp_path), "a", EXIT_TRAILER)
    assert P.purge_removed_sessions([s], {}, archive=_no_archive) == [s]


# ── The real sidebar delete path ─────────────────────────────────────────────


@pytest.fixture()
def sidebar(tmp_path, monkeypatch):
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError:  # pragma: no cover - host without GTK4/Adw typelibs
        pytest.skip("Gtk 4.0 / Adw 1 unavailable")
    from gi.repository import Adw

    from helios.backend import session_archiver
    from helios.widgets import session_list as SL

    Adw.init()
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(session_archiver._self, "ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(SL, "discover_active_session_ids", lambda extra=None: set())
    widget = SL.SessionList()
    try:
        yield widget
    finally:
        widget.shutdown()


def _seed(tmp_path):
    d = tmp_path / "projects" / "-home-alice-proj"
    d.mkdir(parents=True)
    _write(d / "kept.jsonl", [_user_line("keep me")])
    _write(d / "doomed.jsonl", [_user_line("delete me")])
    return d


def _listed(sidebar):
    return set(sidebar._rows_by_id)


def _doomed(sidebar):
    return next(s for s in sidebar._local_sessions if s.session_id == "doomed")


@pytest.mark.parametrize("response", ["delete", "archive"])
def test_dying_process_rewrite_never_returns_as_a_row(tmp_path, sidebar, response):
    d = _seed(tmp_path)
    sidebar.reload(rescan_pool=False)
    assert _listed(sidebar) == {"kept", "doomed"}

    sidebar._on_delete_response(None, response, [_doomed(sidebar)])
    assert _listed(sidebar) == {"kept"}

    # The stopped claude flushes its exit trailer after the removal.
    _write(d / "doomed.jsonl", EXIT_TRAILER)
    sidebar.reload(rescan_pool=False)

    assert _listed(sidebar) == {"kept"}
    assert not (d / "doomed.jsonl").exists()


def test_the_recheck_clears_the_rewrite_without_another_reload(tmp_path, sidebar):
    from gi.repository import GLib

    d = _seed(tmp_path)
    sidebar.reload(rescan_pool=False)
    sidebar._REMOVED_RECHECK_MS = 20
    sidebar._on_delete_response(None, "delete", [_doomed(sidebar)])
    _write(d / "doomed.jsonl", EXIT_TRAILER)

    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    while (d / "doomed.jsonl").exists() and time.monotonic() < deadline:
        ctx.iteration(False)
        time.sleep(0.005)

    assert not (d / "doomed.jsonl").exists()
    assert _listed(sidebar) == {"kept"}
