"""Tell agent-launched Claude sessions apart from the user's own.

Scripts, cron jobs and other agents run headless ``claude -p`` (or the Agent
SDK) and every run writes a transcript into ``~/.claude/projects/`` beside the
user's real conversations. A single batch job can produce hundreds of them, all
in ``$HOME``, so the cwd-based Temporary filter cannot catch them.

Two facts separate them:

* Claude Code stamps every record with ``entrypoint``. An interactive terminal
  session writes ``cli``; anything driven programmatically writes ``sdk-*``
  (``sdk-cli``, ``sdk-ts``, ``sdk-py``).
* Helios drives Claude through the same SDK stream protocol, so its own chats
  are ``sdk-cli`` too. But every turn Helios sends is recorded against a Work
  in its Work store (an ``execution_attempts`` row and/or a ``user.message``
  event). A binding alone is not enough: merely *opening* a transcript in the
  sidebar lazily attaches it to a fresh Work (``resolve_native_work``), so
  bindings exist for sessions Helios never drove.

A local transcript is *agent-launched* when it is headless and no Work Helios
has actually driven is bound to it. Sending a message in one adopts it. The check fails open: if the Work store cannot be read or a
transcript carries no entrypoint (Helios's own Gemini/Codex/Muse mirrors), the
session is treated as the user's and stays in the default list.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from helios.backend.work_store import default_work_db_path

HEADLESS_ENTRYPOINT_PREFIX = "sdk"

# Claude Code writes the first `entrypoint` on the opening user record (line 4,
# under 30 KB even with large CLAUDE.md attachments ahead of it). Bound the scan
# so a transcript that never carries one cannot cost a full read.
_SCAN_MAX_LINES = 64
_SCAN_MAX_BYTES = 256 * 1024
_ENTRYPOINT_MARKER = b'"entrypoint"'

# An entrypoint never changes once written, so cache only definitive answers.
# A miss is not cached: a just-created transcript may not have its first
# record yet.
_entrypoint_cache: dict[str, str] = {}


def transcript_entrypoint(path: Path) -> str:
    """The transcript's Claude Code ``entrypoint`` (``""`` when none is found)."""
    key = str(path)
    cached = _entrypoint_cache.get(key)
    if cached is not None:
        return cached
    try:
        with path.open("rb") as handle:
            consumed = 0
            for index, raw in enumerate(handle):
                if index >= _SCAN_MAX_LINES or consumed > _SCAN_MAX_BYTES:
                    break
                consumed += len(raw)
                if _ENTRYPOINT_MARKER not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                value = obj.get("entrypoint") if isinstance(obj, dict) else None
                if isinstance(value, str) and value:
                    _entrypoint_cache[key] = value
                    return value
    except OSError:
        pass
    return ""


def is_headless_transcript(path: Path) -> bool:
    return transcript_entrypoint(path).startswith(HEADLESS_ENTRYPOINT_PREFIX)


# Native ids (any binding generation, any provider) whose Work Helios has
# sent at least one turn in.
_OWNED_SQL = """
WITH driven AS (
    SELECT DISTINCT p.work_id
      FROM participants p
     WHERE EXISTS (SELECT 1 FROM execution_attempts a WHERE a.work_id = p.work_id)
        OR EXISTS (
            SELECT 1 FROM events e
             WHERE e.work_id = p.work_id AND e.event_type = 'user.message'
        )
)
SELECT b.native_id
  FROM native_bindings b
  JOIN participants p ON p.participant_id = b.participant_id
 WHERE p.work_id IN driven AND b.native_id != ''
UNION
SELECT p.native_session_id
  FROM participants p
 WHERE p.work_id IN driven AND p.native_session_id != ''
"""


def helios_owned_session_ids(db_path: Path | None = None) -> frozenset[str] | None:
    """Every native session id bound to a Work Helios has sent a turn in.

    Returns ``None`` when the store cannot be read, which callers must treat as
    "ownership unknown" and hide nothing. Opened read-only: a sidebar refresh
    must never create, migrate or lock the Work database.
    """
    path = db_path or default_work_db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return None
    try:
        return frozenset(str(row[0]) for row in conn.execute(_OWNED_SQL))
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def agent_launched_ids(
    sessions: Iterable,
    *,
    owned_ids: frozenset[str] | None,
    keep_ids: Iterable[str] = (),
) -> set[str]:
    """Ids of local, headless sessions Helios did not launch.

    ``keep_ids`` (live, selected or just-created sessions) are never
    classified, so a chat Helios is starting cannot vanish before its binding
    lands. Pool rows from other hosts are skipped: this host's Work store says
    nothing about who launched them.
    """
    if owned_ids is None:
        return set()
    keep = set(keep_ids)
    out: set[str] = set()
    for session in sessions:
        sid = session.session_id
        if sid in owned_ids or sid in keep:
            continue
        if getattr(session, "pending", False) or session.project.read_only:
            continue
        if is_headless_transcript(session.path):
            out.add(sid)
    return out
