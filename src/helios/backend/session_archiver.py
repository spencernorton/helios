"""Automatic old-session archival.

Sessions older than the retention window are moved out of
``~/.claude/projects`` so they disappear from the active sidebar, but the raw
JSONL is preserved under ``~/.helios/session-archive``. A caller may supply a
summarizer to write a short memory note when the transcript has durable value;
automatic archival itself never creates unmetered cloud-model work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from helios.backend import projects, session_providers, work_store
from helios.backend.claude_binary import (
    ClaudeBinaryNotFound,
    find_claude_binary,
    supports_max_budget_usd_flag,
)
from helios.backend.process.env_scrub import CLAUDE_AUTH_ENV, scrubbed_child_env
from helios.backend.projects import Session
from helios.backend.session_state import discover_active_session_ids
from helios.backend.transcript import parse_transcript
from helios.log import get_logger
from helios.paths import bind_state_paths

_log = get_logger("session-archiver")

RETENTION_DAYS = 4
RETENTION_SECONDS = RETENTION_DAYS * 86400
ARCHIVE_ROOT: Path
_self = bind_state_paths(__name__, ARCHIVE_ROOT="session-archive")
MAX_PER_RUN = 8
SUMMARY_MAX_BUDGET_USD = 0.25

_SUMMARY_PROMPT = """\
You are reviewing an old coding-assistant session before archival.

Return NO_MEMORY if the transcript contains no durable project/user knowledge.
Otherwise write a concise markdown memory note with:
- what mattered
- files/systems involved
- any follow-up or gotcha worth preserving

Do not include chat filler, tool logs, or generic process commentary.

----- BEGIN TRANSCRIPT TAIL -----
{tail}
----- END TRANSCRIPT TAIL -----
"""


@dataclass(slots=True)
class ArchiveResult:
    session_id: str
    archived_to: Path
    memory_path: Path | None = None


@dataclass(slots=True)
class ArchiveReport:
    archived: list[ArchiveResult] = field(default_factory=list)
    skipped_live: int = 0
    errors: list[str] = field(default_factory=list)


Summarizer = Callable[[Session], str]


def _continuity_archival_groups(
    sessions: list[Session],
) -> tuple[list[list[Session]], set[str]]:
    """Group continuity-enabled multi-leg Works so legs age and archive together."""
    db_path = work_store.default_work_db_path()
    if db_path is None or not db_path.exists():
        return [[s] for s in sessions], set()
    store = None
    try:
        store = work_store.WorkStore(db_path)
        groups = store.continuity_work_groups()
        if not groups:
            return [[s] for s in sessions], set()
        sid_to_work: dict[str, str] = {}
        all_work_sids: dict[str, set[str]] = {}
        non_exclusive: set[str] = set()
        for wid, (_work, legs) in groups.items():
            if len(legs) < 2:
                continue
            exclusive = {
                nid for _prov, nid in store.exclusive_native_sessions_for_work(wid)
            }
            leg_sids = {leg.native_session_id for leg in legs if leg.native_session_id}
            all_work_sids[wid] = leg_sids
            for sid in leg_sids:
                sid_to_work[sid] = wid
                if sid not in exclusive:
                    non_exclusive.add(sid)
    except Exception:
        return [[s] for s in sessions], set()
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    if not sid_to_work:
        return [[s] for s in sessions], set()

    by_work: dict[str, list[Session]] = {}
    ordered_units: list[list[Session]] = []
    seen_works: set[str] = set()
    for s in sessions:
        wid = sid_to_work.get(s.session_id, "")
        if not wid:
            ordered_units.append([s])
            continue
        by_work.setdefault(wid, []).append(s)
        if wid not in seen_works:
            seen_works.add(wid)
            ordered_units.append(by_work[wid])
    return ordered_units, non_exclusive


def archive_old_sessions(
    *,
    now: float | None = None,
    live_ids: set[str] | None = None,
    max_sessions: int = MAX_PER_RUN,
    summarizer: Summarizer | None = None,
) -> ArchiveReport:
    """Archive local sessions older than four days.

    ``summarizer`` is injectable for explicit/manual workflows and tests. The
    automatic production path does not call a cloud model: archival is
    housekeeping, not an authorization to create a separate provider budget.
    """
    now = now if now is not None else time.time()
    live_ids = discover_active_session_ids(extra=live_ids or set())
    summarizer = summarizer or _no_summary
    report = ArchiveReport()
    cutoff = now - RETENTION_SECONDS
    archived_count = 0
    units, non_exclusive = _continuity_archival_groups(
        projects.discover_local_sessions()
    )
    for group in units:
        if archived_count >= max_sessions:
            break
        if any(s.session_id in live_ids for s in group):
            report.skipped_live += sum(1 for s in group if s.session_id in live_ids)
            continue
        if max(s.mtime for s in group) >= cutoff:
            continue
        archivable = [s for s in group if s.session_id not in non_exclusive]
        if not archivable:
            continue
        staged_moves: list[tuple[Session, Path, Path | None]] = []
        group_failed = False
        for session in archivable:
            memory_path: Path | None = None
            try:
                memory_path = _write_memory_if_useful(session, summarizer(session))
                dest = _move_session_to_archive(session)
                staged_moves.append((session, dest, memory_path))
            except Exception as e:
                group_failed = True
                if memory_path is not None:
                    try:
                        memory_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                _log.warning("failed to archive session %s; rolling back group: %s", session.session_id, e)
                report.errors.append(f"{session.session_id}: {e}")
                break
        if group_failed:
            for moved_session, moved_dest, moved_mem in reversed(staged_moves):
                try:
                    moved_session.path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(moved_dest, moved_session.path)
                except OSError as rb_exc:
                    _log.warning("rollback of archived session %s failed: %s", moved_session.session_id, rb_exc)
                if moved_mem is not None:
                    try:
                        moved_mem.unlink(missing_ok=True)
                    except OSError:
                        pass
            continue
        for moved_session, moved_dest, moved_mem in staged_moves:
            session_providers.forget(moved_session.session_id)
            report.archived.append(
                ArchiveResult(
                    session_id=moved_session.session_id,
                    archived_to=moved_dest,
                    memory_path=moved_mem,
                )
            )
        archived_count += 1
    return report


def archive_session(session: Session) -> Path:
    """Archive one session now — the sidebar row's inline Archive action.

    Same move and provider bookkeeping as the automatic pass, without its
    age or live checks: the caller has already stopped Helios's own driver
    and refused sessions a foreign process still writes.
    """
    dest = _move_session_to_archive(session)
    session_providers.forget(session.session_id)
    return dest


def _no_summary(_session: Session) -> str:
    """Default archiver policy: preserve data without hidden model spend."""
    return ""


def summarize_with_haiku(session: Session) -> str:
    tail = _transcript_tail_for_prompt(session)
    if not tail:
        return ""
    try:
        binary = find_claude_binary().path
    except ClaudeBinaryNotFound:
        return ""
    if not supports_max_budget_usd_flag():
        return ""
    prompt = _SUMMARY_PROMPT.format(tail=tail)
    try:
        proc = subprocess.run(
            [
                str(binary),
                "--print",
                "--model",
                "haiku",
                "--max-thinking-tokens",
                "0",
                "--max-budget-usd",
                f"{SUMMARY_MAX_BUDGET_USD:g}",
                # Scoped down to what summarising a transcript needs, the same
                # way title generation was before. Measured on a development machine
                # 2026-08-05: 34,377 prompt tokens with `--setting-sources
                # user`, 23,965 with these three flags — 10,412 fewer (30%)
                # for the same job. It also matters for trust: the tail is
                # untrusted text, and `--setting-sources user` put the global
                # CLAUDE.md, its memory index and every configured MCP server
                # in front of a model that is reading it.
                #
                # ORDER IS LOAD-BEARING: `--mcp-config` is variadic, so it must
                # never be the last option before the positional prompt or it
                # swallows it and the model summarises nothing. Keep a
                # non-variadic flag between them (see the argv test).
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--setting-sources",
                "",
                "--permission-mode",
                "dontAsk",
                "--allowed-tools",
                "",
                "--no-session-persistence",
                prompt,
            ],
            cwd=str(Path.home()),
            text=True,
            capture_output=True,
            timeout=25,
            check=False,
            # Same scoped policy as the interactive Claude driver.
            env=scrubbed_child_env(keep=CLAUDE_AUTH_ENV),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    text = proc.stdout.strip()
    if not text or text.upper().startswith("NO_MEMORY"):
        return ""
    return text


def _transcript_tail_for_prompt(session: Session, *, max_turns: int = 14) -> str:
    parts: list[str] = []
    for turn in parse_transcript(session.path):
        if turn.role not in ("user", "assistant") or turn.is_meta:
            continue
        text = turn.text.strip()
        if not text:
            continue
        if len(text) > 1200:
            text = text[:1197] + "..."
        parts.append(f"{turn.role.upper()}: {text}")
    return "\n\n".join(parts[-max_turns:])


def _write_memory_if_useful(session: Session, summary: str) -> Path | None:
    summary = (summary or "").strip()
    if not summary:
        return None
    mem_dir = session.project.path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    path = mem_dir / f"archived-session-{session.session_id[:8]}.md"
    title = session.ensure_title()
    body = (
        "---\n"
        f"name: archived-session-{session.session_id[:8]}\n"
        f"description: Summary extracted before Helios archived session {session.session_id}\n"
        "metadata:\n"
        "  node_type: memory\n"
        "  type: session-archive\n"
        f"  originSessionId: {session.session_id}\n"
        "---\n\n"
        f"# Archived session: {title}\n\n"
        f"- Session: `{session.session_id}`\n"
        f"- CWD: `{session.project.cwd}`\n"
        f"- Archived source: `{session.path}`\n\n"
        f"{summary}\n"
    )
    path.write_text(body, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _move_session_to_archive(session: Session) -> Path:
    dest_dir = _self.ARCHIVE_ROOT / session.project.dirname
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(dest_dir / session.path.name)
    shutil.move(str(session.path), str(dest))

    aux = session.path.with_suffix("")
    if aux.is_dir():
        shutil.move(str(aux), str(_unique_path(dest_dir / aux.name)))
    return dest


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        cand = path.with_name(f"{stem}.{i}{suffix}")
        if not cand.exists():
            return cand
    raise OSError(f"too many archive collisions for {path}")
