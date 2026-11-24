"""Provider-backed reconciliation for orphaned or ambiguous execution attempts.

When a provider child process exits or drops its pipe after a prompt has been
dispatched, Helios fails closed and keeps the Work blocked rather than blindly
replaying the prompt or age-expiring the attempt. This module inspects each
provider's native durable state (read-only) to determine whether an authoritative
terminal outcome can be proven:

* **Pre-dispatch** (empty ``prompt_digest``): the prompt never crossed the wire,
  so the attempt can be closed cleanly as a local pre-dispatch abort.
* **Codex (``openai``)**: inspects native rollout JSONL under
  ``$CODEX_HOME/sessions`` (default ``~/.codex/sessions``) for an exact
  ``event_msg`` matching ``attempt.accepted_turn_id`` with ``task_complete`` or
  ``turn_aborted``. Never guesses when ``accepted_turn_id`` is missing.
* **Claude (``anthropic``)**: inspects native Claude CLI JSONL under
  ``$CLAUDE_CONFIG_DIR/projects`` (default ``~/.claude/projects``), refusing
  Helios synthetic mirrors (``version`` starting with ``helios-``) and sidechain
  records. Correlates the dispatched prompt by SHA-256 ``prompt_digest`` and
  accepts only a subsequent non-sidechain terminal ``stop_reason`` (without
  dangling ``tool_use``), ``result`` record, or explicit user interrupt marker.
* **Google (``google``)**: queries Antigravity's read-only
  ``conversation_summaries.db`` via :func:`google_env.probe_conversation_idle`
  with the same ``CASCADE_RUN_STATUS_IDLE`` + quiet-window proof bar used by the
  live liveness watchdog.
* **OpenRouter (``openrouter``)**: stateless HTTP/SSE has no remote session
  status API, and the local mirror can contain partial output on connection
  drops. Always returns ``uncertain`` with forensic detail from local history so
  the operator explicitly confirms unblocking without replay.

GTK-free. Safe to run on a background worker thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_mod
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helios.backend import google_env, model_catalog
from helios.backend.sensitive_text import scrub_sensitive
from helios.backend.work_store import ExecutionAttempt, WorkStore, WorkStoreError
from helios.paths import state_dir

__all__ = [
    "AttemptProbeResult",
    "probe_attempt",
    "reconcile_with_provider_evidence",
]

_MAX_TAIL_BYTES = 2 * 1024 * 1024
_MAX_DETAIL_CHARS = 320
_CLOCK_SKEW_SECONDS = 5.0
_GOOGLE_SKEW_SECONDS = 2.0
_DEFAULT_GOOGLE_QUIET_SECONDS = 30.0

_CLAUDE_TERMINAL_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "stop_sequence", "refusal"}
)
_CLAUDE_ABORT_SUBTYPES = frozenset(
    {"aborted", "interrupted", "cancelled", "canceled"}
)
_CLAUDE_INTERRUPT_MARKER = "[Request interrupted by user]"
_FRACTIONAL_SECONDS_RE = re.compile(r"\.(\d+)")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True, slots=True)
class AttemptProbeResult:
    """Outcome of a read-only provider-native probe for one running attempt."""

    attempt_id: str
    provider: str
    verdict: str  # "confirmed_completed" | "confirmed_aborted" | "confirmed_failed" | "uncertain"
    provider_status: str = ""
    matched_turn_id: str = ""
    matched_native_id: str = ""
    source: str = ""
    detail: str = ""

    @property
    def is_confirmed(self) -> bool:
        return self.verdict in {
            "confirmed_completed",
            "confirmed_aborted",
            "confirmed_failed",
        }

    @property
    def target_status(self) -> str:
        return {
            "confirmed_completed": "completed",
            "confirmed_aborted": "aborted",
            "confirmed_failed": "failed",
        }.get(self.verdict, "")


def _scrub_detail(text: str) -> str:
    clean, _ = scrub_sensitive(str(text or "").strip())
    if len(clean) > _MAX_DETAIL_CHARS:
        clean = clean[: _MAX_DETAIL_CHARS - 1].rstrip() + "…"
    return clean


def _is_safe_id(value: str) -> bool:
    if not value or "/" in value or "\\" in value or ".." in value:
        return False
    return bool(_SAFE_ID_RE.fullmatch(value))


def _parse_iso_epoch(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _FRACTIONAL_SECONDS_RE.sub(
        lambda m: "." + m.group(1)[:6].ljust(6, "0"),
        text,
        count=1,
    )
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _read_tail_jsonl(
    path: Path,
    *,
    max_bytes: int = _MAX_TAIL_BYTES,
) -> list[dict[str, Any]] | None:
    """Safely read up to ``max_bytes`` from the tail of a regular JSONL file."""

    if path.is_symlink() or not path.is_file():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            return None
        truncated = st.st_size > max_bytes
        if truncated:
            os.lseek(fd, st.st_size - max_bytes, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = max_bytes if truncated else st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    lines = raw.splitlines()
    if truncated and lines:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _read_json_regular(path: Path, *, max_bytes: int = _MAX_TAIL_BYTES) -> Any:
    if path.is_symlink() or not path.is_file():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    fd_owned = True
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        fh = os.fdopen(fd, "r", encoding="utf-8", errors="replace")
        fd_owned = False
        with fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass


def _default_codex_sessions_dir() -> Path:
    base = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    return base / "sessions"


def _default_claude_projects_dir() -> Path:
    base = Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")
    ).expanduser()
    return base / "projects"


def _default_openrouter_history_dir() -> Path:
    return state_dir() / "openrouter-sessions"


def _find_codex_rollout(sessions_dir: Path, thread_id: str) -> Path | None:
    if not sessions_dir.is_dir():
        return None
    candidates: list[Path] = []
    try:
        for path in sessions_dir.rglob(f"*{thread_id}.jsonl"):
            if path.is_symlink() or not path.is_file():
                continue
            stem = path.stem
            if stem == thread_id or stem.endswith(f"-{thread_id}"):
                candidates.append(path)
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    return candidates[0]


def _probe_codex_attempt(
    attempt: ExecutionAttempt,
    *,
    codex_sessions_dir: Path | None = None,
) -> AttemptProbeResult:
    native_id = attempt.native_binding_id
    turn_id = attempt.accepted_turn_id
    if not turn_id:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="codex_rollout",
            detail=_scrub_detail(
                "Codex exited before recording an accepted turn ID; Helios cannot prove "
                "from rollout state whether the prompt executed."
            ),
        )
    sessions_dir = codex_sessions_dir or _default_codex_sessions_dir()
    rollout = _find_codex_rollout(sessions_dir, native_id)
    if rollout is None:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="codex_rollout",
            detail=_scrub_detail(
                f"No Codex rollout file found for thread {native_id}."
            ),
        )
    records = _read_tail_jsonl(rollout)
    if records is None:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="codex_rollout",
            detail=_scrub_detail("Codex rollout file could not be read safely."),
        )

    matched_verdict = ""
    matched_status = ""
    for rec in records:
        if rec.get("type") != "event_msg":
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        if str(payload.get("turn_id") or "") != turn_id:
            continue
        ev_type = str(payload.get("type") or "")
        if ev_type == "task_complete":
            matched_verdict = "confirmed_completed"
            matched_status = "task_complete"
        elif ev_type == "turn_aborted":
            matched_verdict = "confirmed_aborted"
            matched_status = "turn_aborted"

    if matched_verdict:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict=matched_verdict,
            provider_status=matched_status,
            matched_turn_id=turn_id,
            matched_native_id=native_id,
            source="codex_rollout",
            detail=_scrub_detail(
                f"Codex rollout recorded {matched_status} for accepted turn {turn_id}."
            ),
        )
    return AttemptProbeResult(
        attempt_id=attempt.attempt_id,
        provider=attempt.provider,
        verdict="uncertain",
        source="codex_rollout",
        detail=_scrub_detail(
            f"Codex rollout has no task_complete or turn_aborted event for turn {turn_id}."
        ),
    )


def _find_claude_transcript(projects_dir: Path, session_id: str) -> Path | None:
    if not projects_dir.is_dir():
        return None
    filename = f"{session_id}.jsonl"
    candidates: list[Path] = []
    direct = projects_dir / filename
    if not direct.is_symlink() and direct.is_file():
        candidates.append(direct)
    try:
        for entry in projects_dir.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            cand = entry / filename
            if not cand.is_symlink() and cand.is_file():
                candidates.append(cand)
    except OSError:
        pass
    if not candidates:
        return None
    candidates.sort(
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    return candidates[0]


def _claude_user_text_candidates(message: dict[str, Any]) -> tuple[list[str], bool]:
    """Return ``(candidate_texts, is_tool_result_only)`` for one Claude user record."""

    content = message.get("content")
    if isinstance(content, str):
        return ([content] if content else []), False
    if not isinstance(content, list):
        return [], False
    texts: list[str] = []
    saw_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            saw_tool_result = True
        elif btype == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    if not texts and saw_tool_result:
        return [], True
    candidates = list(texts)
    if len(texts) > 1:
        candidates.append("\n".join(texts))
    return candidates, False


def _sha256_digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _probe_claude_attempt(
    attempt: ExecutionAttempt,
    *,
    claude_projects_dir: Path | None = None,
) -> AttemptProbeResult:
    native_id = attempt.native_binding_id
    projects_dir = claude_projects_dir or _default_claude_projects_dir()
    transcript = _find_claude_transcript(projects_dir, native_id)
    if transcript is None:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="claude_transcript",
            detail=_scrub_detail(
                f"No native Claude transcript found for session {native_id}."
            ),
        )
    records = _read_tail_jsonl(transcript)
    if records is None:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="claude_transcript",
            detail=_scrub_detail("Native Claude transcript could not be read safely."),
        )

    for rec in records[:25]:
        ver = rec.get("version")
        if isinstance(ver, str) and ver.startswith("helios-"):
            return AttemptProbeResult(
                attempt_id=attempt.attempt_id,
                provider=attempt.provider,
                verdict="uncertain",
                source="claude_transcript",
                detail=_scrub_detail(
                    "Found a Helios mirror transcript instead of a native Claude session log."
                ),
            )

    started_epoch = _parse_iso_epoch(attempt.started_at)
    matched_prompt = False
    turn_closed_by_next_user = False
    outcome_verdict = ""
    outcome_status = ""
    saw_tool_use_without_terminal = False

    for rec in records:
        if rec.get("isSidechain") is True:
            continue
        rec_session = str(rec.get("sessionId") or rec.get("session_id") or "")
        if rec_session and rec_session != native_id:
            continue
        rec_type = str(rec.get("type") or "")
        rec_epoch = _parse_iso_epoch(rec.get("timestamp"))

        if rec_type == "user":
            if rec.get("isMeta") is True:
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            candidates, tool_only = _claude_user_text_candidates(msg)
            if tool_only:
                continue
            is_prompt_match = bool(
                candidates
                and any(_sha256_digest(c) == attempt.prompt_digest for c in candidates)
                and (
                    started_epoch is None
                    or rec_epoch is None
                    or rec_epoch >= started_epoch - _CLOCK_SKEW_SECONDS
                )
            )
            if is_prompt_match:
                matched_prompt = True
                turn_closed_by_next_user = False
                outcome_verdict = ""
                outcome_status = ""
                saw_tool_use_without_terminal = False
                continue
            if matched_prompt and not turn_closed_by_next_user:
                if any(_CLAUDE_INTERRUPT_MARKER in c for c in candidates):
                    if not outcome_verdict:
                        outcome_verdict = "confirmed_aborted"
                        outcome_status = "interrupted"
                        saw_tool_use_without_terminal = False
                elif candidates:
                    # A distinct follow-up human turn began; stop attributing
                    # subsequent records to our earlier attempt.
                    turn_closed_by_next_user = True
            continue

        if not matched_prompt or turn_closed_by_next_user:
            continue

        if rec_type == "assistant":
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            if attempt.accepted_turn_id:
                rec_ids = {
                    str(rec.get("requestId") or ""),
                    str(rec.get("uuid") or ""),
                    str(msg.get("id") or ""),
                }
                if attempt.accepted_turn_id not in rec_ids:
                    continue
            stop_reason = str(msg.get("stop_reason") or "").strip()
            if stop_reason in _CLAUDE_TERMINAL_STOP_REASONS:
                outcome_verdict = "confirmed_completed"
                outcome_status = stop_reason
                saw_tool_use_without_terminal = False
            elif stop_reason == "tool_use":
                outcome_verdict = ""
                outcome_status = ""
                saw_tool_use_without_terminal = True
        elif rec_type == "result":
            subtype = str(rec.get("subtype") or "unknown").strip()
            lowered = subtype.lower()
            is_error = bool(rec.get("is_error")) or bool(str(rec.get("api_error") or "").strip())
            if lowered == "success" and not is_error:
                outcome_verdict = "confirmed_completed"
                outcome_status = "success"
            elif lowered in _CLAUDE_ABORT_SUBTYPES:
                outcome_verdict = "confirmed_aborted"
                outcome_status = subtype
            else:
                outcome_verdict = "confirmed_failed"
                outcome_status = subtype or "error"
            saw_tool_use_without_terminal = False

    if matched_prompt and outcome_verdict:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict=outcome_verdict,
            provider_status=outcome_status,
            matched_turn_id=attempt.accepted_turn_id,
            matched_native_id=native_id,
            source="claude_transcript",
            detail=_scrub_detail(
                f"Native Claude transcript confirmed {outcome_status} for the dispatched prompt."
            ),
        )
    if matched_prompt and saw_tool_use_without_terminal:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="claude_transcript",
            detail=_scrub_detail(
                "Native Claude transcript reached tool_use without a terminal end_turn record."
            ),
        )
    if matched_prompt:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="claude_transcript",
            detail=_scrub_detail(
                "Native Claude transcript recorded the prompt but no terminal assistant response."
            ),
        )
    return AttemptProbeResult(
        attempt_id=attempt.attempt_id,
        provider=attempt.provider,
        verdict="uncertain",
        source="claude_transcript",
        detail=_scrub_detail(
            "Native Claude transcript has no matching dispatched prompt and terminal record."
        ),
    )


def _probe_google_attempt(
    attempt: ExecutionAttempt,
    *,
    google_db_path: Path | None = None,
    google_quiet_seconds: float = _DEFAULT_GOOGLE_QUIET_SECONDS,
    now_epoch: float | None = None,
) -> AttemptProbeResult:
    native_id = attempt.native_binding_id
    snap = google_env.probe_conversation_idle(native_id, db_path=google_db_path)
    if snap is None:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="antigravity_summary_db",
            detail=_scrub_detail(
                f"No Antigravity summary record found for conversation {native_id}."
            ),
        )
    started_epoch = _parse_iso_epoch(attempt.started_at)
    now = time.time() if now_epoch is None else float(now_epoch)
    quiet = now - snap.last_modified_epoch
    if (
        snap.status == "CASCADE_RUN_STATUS_IDLE"
        and not snap.not_fully_idle
        and not snap.killed
        and snap.step_count > snap.last_user_input_step_index
        and started_epoch is not None
        and snap.last_modified_epoch >= started_epoch - _GOOGLE_SKEW_SECONDS
        and quiet >= max(0.0, float(google_quiet_seconds))
    ):
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="confirmed_completed",
            provider_status="CASCADE_RUN_STATUS_IDLE",
            matched_turn_id=attempt.accepted_turn_id,
            matched_native_id=native_id,
            source="antigravity_summary_db",
            detail=_scrub_detail(
                f"Antigravity summary DB confirmed CASCADE_RUN_STATUS_IDLE "
                f"(steps {snap.step_count}, quiet {int(quiet)}s)."
            ),
        )
    return AttemptProbeResult(
        attempt_id=attempt.attempt_id,
        provider=attempt.provider,
        verdict="uncertain",
        source="antigravity_summary_db",
        detail=_scrub_detail(
            f"Antigravity summary reports status={snap.status or 'unknown'}, "
            f"not_fully_idle={snap.not_fully_idle}, killed={snap.killed}, "
            f"steps={snap.step_count}/{snap.last_user_input_step_index}."
        ),
    )


def _probe_openrouter_attempt(
    attempt: ExecutionAttempt,
    *,
    openrouter_history_dir: Path | None = None,
) -> AttemptProbeResult:
    """Forensic inspection only; OpenRouter HTTP/SSE cannot be remotely verified."""

    history_dir = openrouter_history_dir or _default_openrouter_history_dir()
    history_path = history_dir / f"{attempt.native_binding_id}.json"
    data = _read_json_regular(history_path)
    msgs = data.get("messages") if isinstance(data, dict) else None
    summary = "no local history file"
    if isinstance(msgs, list) and msgs:
        last_role = str(msgs[-1].get("role") or "unknown") if isinstance(msgs[-1], dict) else "unknown"
        summary = f"{len(msgs)} local history message(s), last role={last_role}"
    elif isinstance(msgs, list):
        summary = "0 local history messages"
    turn_note = (
        f"response {attempt.accepted_turn_id} was accepted before exit"
        if attempt.accepted_turn_id
        else "no provider response ID was accepted"
    )
    return AttemptProbeResult(
        attempt_id=attempt.attempt_id,
        provider=attempt.provider,
        verdict="uncertain",
        source="openrouter_history",
        detail=_scrub_detail(
            f"OpenRouter has no remote session lookup ({turn_note}; {summary}). "
            "Confirm unblocking manually without replaying the prompt."
        ),
    )


def probe_attempt(
    attempt: ExecutionAttempt,
    *,
    claude_projects_dir: Path | None = None,
    codex_sessions_dir: Path | None = None,
    google_db_path: Path | None = None,
    openrouter_history_dir: Path | None = None,
    google_quiet_seconds: float = _DEFAULT_GOOGLE_QUIET_SECONDS,
    now_epoch: float | None = None,
) -> AttemptProbeResult:
    """Inspect provider-native state for one running ExecutionAttempt (read-only)."""

    if attempt.status != "running":
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="work_store",
            detail=_scrub_detail(f"Attempt is already in terminal state {attempt.status}."),
        )
    if not attempt.prompt_digest:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="confirmed_aborted",
            provider_status="pre_dispatch",
            matched_native_id=attempt.native_binding_id,
            source="work_store",
            detail=_scrub_detail(
                "Driver exited before dispatching the prompt to the provider."
            ),
        )
    if not attempt.provider_request_key:
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="work_store",
            detail=_scrub_detail(
                "Attempt has no durable provider request key; manual confirmation required."
            ),
        )
    if not _is_safe_id(attempt.native_binding_id):
        return AttemptProbeResult(
            attempt_id=attempt.attempt_id,
            provider=attempt.provider,
            verdict="uncertain",
            source="work_store",
            detail=_scrub_detail(
                "No valid provider-native session ID was bound before exit; "
                "inspect provider state before unblocking."
            ),
        )

    if attempt.provider == model_catalog.PROVIDER_OPENAI:
        return _probe_codex_attempt(
            attempt,
            codex_sessions_dir=codex_sessions_dir,
        )
    if attempt.provider == model_catalog.PROVIDER_ANTHROPIC:
        return _probe_claude_attempt(
            attempt,
            claude_projects_dir=claude_projects_dir,
        )
    if attempt.provider == model_catalog.PROVIDER_GOOGLE:
        return _probe_google_attempt(
            attempt,
            google_db_path=google_db_path,
            google_quiet_seconds=google_quiet_seconds,
            now_epoch=now_epoch,
        )
    if attempt.provider == model_catalog.PROVIDER_OPENROUTER:
        return _probe_openrouter_attempt(
            attempt,
            openrouter_history_dir=openrouter_history_dir,
        )

    label = model_catalog.PROVIDER_LABELS.get(attempt.provider, attempt.provider or "provider")
    return AttemptProbeResult(
        attempt_id=attempt.attempt_id,
        provider=attempt.provider,
        verdict="uncertain",
        source="unsupported_provider",
        detail=_scrub_detail(
            f"No automatic native state probe exists for {label}; "
            "confirm unblocking manually without replay."
        ),
    )


def reconcile_with_provider_evidence(
    store: WorkStore,
    attempt: ExecutionAttempt,
    probe: AttemptProbeResult,
) -> ExecutionAttempt:
    """Commit a provider-verified terminal reconciliation without replaying prompts."""

    if probe.attempt_id != attempt.attempt_id:
        raise WorkStoreError("probe result does not match execution attempt")
    if attempt.status != "running":
        raise WorkStoreError(
            f"execution attempt is not running: {attempt.attempt_id} ({attempt.status})"
        )
    if not probe.is_confirmed or not probe.target_status:
        raise WorkStoreError(
            "provider state is uncertain; operator confirmation is required"
        )

    if not attempt.prompt_digest:
        queue_disposition = (
            "discarded"
            if attempt.queue_disposition == "held"
            else attempt.queue_disposition
        )
        return store.finish_execution_attempt(
            attempt.attempt_id,
            status="aborted",
            terminal_reason="reconciled.pre_dispatch",
            queue_disposition=queue_disposition,
        )

    if not probe.provider_status:
        raise WorkStoreError("confirmed probe requires a provider status")

    terminal_receipt: dict[str, Any] = {
        "evidence_type": "provider_terminal",
        "provider_status": probe.provider_status,
        "request_id": attempt.provider_request_key,
    }
    native_id = probe.matched_native_id or attempt.native_binding_id
    if native_id:
        terminal_receipt["native_id"] = native_id
    turn_id = probe.matched_turn_id or attempt.accepted_turn_id
    if turn_id:
        terminal_receipt["turn_id"] = turn_id

    # Never restore/replay held queued drafts during post-exit reconciliation.
    queue_disposition = (
        "discarded"
        if attempt.queue_disposition == "held"
        else (attempt.queue_disposition or "released")
    )
    return store.finish_execution_attempt(
        attempt.attempt_id,
        status=probe.target_status,
        terminal_reason=f"reconciled.provider_{probe.target_status}",
        terminal_receipt=terminal_receipt,
        queue_disposition=queue_disposition,
    )
