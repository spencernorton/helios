"""Google subscription sessions through Antigravity's native NDJSON protocol.

Gemini CLI has a different protocol and is deliberately not a fallback. Native
conversation identities, Work admission and terminal receipts govern every turn.
Headless Antigravity cannot relay approval prompts; escalation is refused.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import replace
from typing import Any

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

from helios.backend.effort_policy import EffortPolicyMixin
from helios.backend import model_catalog
from helios.backend.attachments import google_wire_text
from helios.backend import google_env
from helios.backend.google_env import (
    GoogleBinaryNotFound, find_google_binary, google_subscription_env,
)
from helios.backend.process.child_registry import (
    CHILD_GROUP_ENV,
    ChildRegistryPersistenceError,
    ProcessIdentity,
    ProcessTreeTarget,
    abort_unregistered_child,
    is_real_subprocess,
    inspect_process_tree,
    new_group_token,
    reap_process_trees,
    register_child,
    snapshot_group_descendants,
    unregister_if_dead,
    wake_if_stopped,
)
from helios.backend.process.env_scrub import NORVI_TRACKER_ENV
from helios.backend.process.codex_transcript import CodexTranscriptWriter
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence, ExecutionDispatchEvidence,
    ExecutionTerminalEvidence, MessageDelivery, RequiredPromptContextError,
    UserMessageQueueMixin,
)
from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.project_perms import effective_execution_mode, is_home_cwd
from helios.backend.transcript import Turn
from helios.log import get_logger
from helios.backend.sensitive_text import scrub_sensitive

_log = get_logger("gemini-driver")


class GeminiDriverSpawnError(RuntimeError):
    """Native subscription session could not be started."""


class GeminiCliDriver(EffortPolicyMixin, UserMessageQueueMixin, GObject.Object):
    display_name = "gemini"
    provider = "google"
    _LIVENESS_CHECK_INTERVAL_SECONDS = 15
    # A turn is only probed after this long without any progress. A long
    # reasoning step or a subagent can keep stdout quiet for minutes; the
    # probe treats trajectory writes as progress, so this is the quiet
    # spell before Helios looks, never proof that the turn is over.
    _IDLE_RECONCILE_SILENCE_SECONDS = 90.0
    # Antigravity's records must also have been still for this long.
    _IDLE_DB_QUIET_SECONDS = 60.0
    _HARD_STALL_NOTICE_SECONDS = 1200.0

    __gsignals__ = {
        "session-started": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
        "assistant-streaming": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-appended": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "result": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "usage-updated": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        "rate-limit-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "budget-exhausted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "question-asked": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "queued-user-sent": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
        "delivery-confirmed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "exited": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "spend-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "agents-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "context-compacted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "capabilities-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "activity-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "plan-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-status-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, cwd: str, model: str = "", permission_mode: str = "default",
                 resume_session_id: str | None = None, effort: str = "",
                 **kwargs: Any) -> None:
        GObject.Object.__init__(self)
        self._init_user_queue()
        self._cwd = cwd
        self._model = str(model or "").removeprefix("google:")
        self._effort = effort
        m = re.search(r"-(low|medium|high|xhigh|max)\Z", self._model)
        if m:
            effort_suffix = m.group(1)
            self._model = self._model[:m.start()]
            if not self._effort:
                self._effort = effort_suffix
        self._permission_mode = effective_execution_mode(permission_mode, cwd, provider=self.provider)
        self._resume_session_id = resume_session_id or ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._child_identity: ProcessIdentity | None = None
        self._child_group_token: str | None = None
        self._session_id = ""
        self._is_busy = False
        self._accepting_input = False
        self._stopped = False
        self._last_activity = time.monotonic()
        # Wall-clock twin of _last_activity, comparable with Antigravity's
        # file times when the liveness probe looks for unreported progress.
        self._last_activity_wall = time.time()
        self._streaming: StreamingAssistant | None = None
        self._pending_text = ""
        self._prepared_prompt = None
        self._accepted = False
        self._accepted_turn_id = ""
        self._accounting_failed = False
        self._stderr = ""
        self._startup_error = ""
        self._cumulative_usage: dict[str, int] = {}
        self._completed_turns = 0
        self._seen_result_turns = 0
        self._last_step_index = -1
        # The newest stdout step and this turn's accepted input index: the
        # silent-turn recovery may only close a turn whose final response
        # Helios itself watched finish.
        self._last_step_kind = ""
        self._last_step_state = ""
        self._accepted_input_index: int | None = None
        # Response text by trajectory step for the current turn, so recovery
        # can require the final step's own text, not earlier narration.
        self._response_text_by_step: dict[int, str] = {}
        self._probe_refusal_logged = False
        self._dispatch_step_floor = -1
        self._turn_started_wall = 0.0
        self._liveness_source_id = 0
        self._liveness_probe_in_flight = False
        self._stall_notice_emitted = False
        self._startup_pending_text: str | None = None
        self._transcript = CodexTranscriptWriter(cwd, provider=model_catalog.PROVIDER_GOOGLE)
        self.init_tools: list[str] = []
        self.init_mcp_servers: list[dict[str, Any]] = []

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def is_busy(self) -> bool:
        return self._is_busy

    @property
    def is_initializing(self) -> bool:
        return self.is_running and not self._session_id and not self._stopped and not self._accounting_failed

    @property
    def is_accepting_input(self) -> bool:
        return (
            self._accepting_input
            and not self._stopped
            and not self._accounting_failed
            and self.is_running
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def model(self) -> str:
        return self._model

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def live_session_id(self) -> str:
        return self._session_id

    @property
    def last_activity(self) -> float:
        return self._last_activity

    @property
    def effort_key(self) -> str:
        return self._effort

    def start(self) -> None:
        if not self._model or not self._model.startswith("gemini-"):
            raise GeminiDriverSpawnError("Choose a discovered Google model before starting a session.")
        if is_home_cwd(self._cwd):
            raise GeminiDriverSpawnError("Google sessions cannot start in your home folder. Choose a project directory.")
        try:
            binary = find_google_binary()
            env = google_subscription_env(keep=NORVI_TRACKER_ENV)
        except (GoogleBinaryNotFound, ValueError) as exc:
            raise GeminiDriverSpawnError(str(exc)) from exc
        effort = self._effort
        cmd = [
            str(binary.path),
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--model", self._model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        if self._resume_session_id:
            cmd.extend(["--conversation", self._resume_session_id])
        if self._permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        elif self._permission_mode == "plan":
            cmd.extend(["--mode", "plan", "--sandbox"])
        elif self._permission_mode == "acceptEdits":
            cmd.extend(["--mode", "accept-edits", "--sandbox"])
        else:
            cmd.append("--sandbox")
        group_token = new_group_token()
        spawn_env = dict(env)
        spawn_env[CHILD_GROUP_ENV] = group_token
        try:
            self._proc = subprocess.Popen(cmd, cwd=self._cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=spawn_env,
                bufsize=0, start_new_session=True)
        except OSError as exc:
            raise GeminiDriverSpawnError("Could not start the Antigravity CLI.") from exc
        self._child_group_token = group_token
        is_real_proc = is_real_subprocess(self._proc)
        try:
            self._child_identity = register_child(
                self._proc.pid,
                pgid=self._proc.pid,
                own_group=True,
                provider="google",
                group_token=group_token,
                require_live=is_real_proc,
            )
        except ChildRegistryPersistenceError as exc:
            abort_unregistered_child(
                self._proc.pid,
                pgid=self._proc.pid,
                start_ticks=(
                    exc.child_identity.start_ticks
                    if exc.child_identity is not None
                    else None
                ),
                group_token=group_token,
                descendants=exc.descendants,
                poll=self._proc.poll,
            )
            self._proc = None
            raise GeminiDriverSpawnError(
                f"Could not record Antigravity child process for crash recovery: {exc}"
            ) from exc
        self._stopped = False
        self._accepting_input = False
        threading.Thread(target=self._read_stderr, daemon=True, name="google-stderr").start()
        threading.Thread(target=self._read_stdout, daemon=True, name="google-stdout").start()

    def send_user_text(
        self,
        text: str,
        *,
        _reuse_execution_attempt: bool = False,
    ) -> MessageDelivery:
        if not self.is_running or not self._proc or not self._proc.stdin or self._stopped:
            self.emit("error", "Google process is not running. Sign in with `agy` in a terminal.")
            return MessageDelivery("rejected")
        if self._accounting_failed:
            self.emit("error", "Helios could not verify Google accounting. New Work remains blocked.")
            return MessageDelivery("rejected")
        if self.is_busy and self._startup_pending_text is None:
            self.emit("error", "Google is still processing the current turn.")
            return MessageDelivery("rejected")
        if not self._session_id:
            if self._startup_pending_text is not None:
                self.emit("error", "Google is still preparing this session.")
                return MessageDelivery("rejected")
            reason = self._execution_block_reason()
            if reason:
                self.emit("error", reason)
                return MessageDelivery("rejected")
            try:
                # Recheck routing before each turn, including queued turns.
                google_subscription_env()
                pre_prepared = self._prepare_prompt_context(text)
                if pre_prepared.carryover_images:
                    google_wire_text(pre_prepared.text, carryover=pre_prepared.carryover_images)
            except OSError:
                self.emit("error", "Helios could not stage visual context for Google. The message was not sent.")
                return MessageDelivery("rejected")
            except RequiredPromptContextError as exc:
                self.emit("error", exc.user_message)
                return MessageDelivery("rejected")
            except ValueError as exc:
                self.emit("error", str(exc))
                return MessageDelivery("rejected")
            admission_error = self._begin_execution_attempt(
                reuse_existing=_reuse_execution_attempt
            )
            if admission_error:
                self.emit("error", admission_error)
                return MessageDelivery("rejected")
            self._startup_pending_text = text
            self._is_busy = True
            self._touch_activity()
            return MessageDelivery("pending")
        reason = self._execution_block_reason()
        if reason:
            self.emit("error", reason)
            return MessageDelivery("rejected")
        try:
            # Recheck routing before each turn, including queued turns.
            google_subscription_env()
            prepared = self._prepare_prompt_context(text)
            wire_text = google_wire_text(prepared.text, carryover=prepared.carryover_images)
        except OSError:
            self.emit("error", "Helios could not stage visual context for Google. The message was not sent.")
            return MessageDelivery("rejected")
        except RequiredPromptContextError as exc:
            self.emit("error", exc.user_message)
            return MessageDelivery("rejected")
        except ValueError as exc:
            self.emit("error", str(exc))
            return MessageDelivery("rejected")
        reason = self._begin_execution_attempt(
            reuse_existing=_reuse_execution_attempt
        )
        if reason:
            self.emit("error", reason)
            return MessageDelivery("rejected")
        attempt_id = self.execution_attempt_id
        if not self._record_execution_dispatch(ExecutionDispatchEvidence(
                wire_prompt_text=wire_text, provider_request_key=attempt_id,
                native_binding_id=self._confirmed_execution_native_id(),
                pending_capsule=(
                    prepared.pending_capsule.to_record()
                    if prepared.pending_capsule is not None
                    else None
                ))):
            released = self._finish_execution_with_evidence(ExecutionTerminalEvidence(
                evidence_type="local_abort", status="aborted", reason_code="local_abort", queue_disposition="restored"))
            if released:
                self.emit("error", "Helios could not persist the Google dispatch. The message was not sent.")
            else:
                self._hold_accounting_failure("Helios could not close the unsent Google reservation. This Work remains blocked.")
            return MessageDelivery("rejected")
        self._is_busy = True
        self._accepted = False
        self._accepted_turn_id = ""
        self._pending_text = text
        self._prepared_prompt = prepared
        self._dispatch_step_floor = self._last_step_index
        self._last_step_kind = ""
        self._last_step_state = ""
        self._accepted_input_index = None
        self._response_text_by_step = {}
        self._probe_refusal_logged = False
        self._streaming = StreamingAssistant(model=self._model)
        line = (json.dumps({"event": "user", "message": {"content": wire_text}}) + "\n").encode()
        try:
            # Unbuffered pipes may short-write; every byte must be confirmed.
            offset = 0
            while offset < len(line):
                written = self._proc.stdin.write(line[offset:])
                if not isinstance(written, int) or written <= 0:
                    raise OSError("incomplete input write")
                offset += written
            self._proc.stdin.flush()
        except (OSError, ValueError):
            self.end_input()
            self.emit("error", "Google delivery is uncertain. This Work remains blocked until provider-backed recovery confirms the outcome.")
            return MessageDelivery("uncertain")
        self._touch_activity()
        self._turn_started_wall = time.time()
        self._stall_notice_emitted = False
        self._start_liveness_watchdog()
        # A pipe write is a local handoff. Native user_input/result events
        # provide acceptance; no synthetic conversation id is emitted.
        return MessageDelivery("accepted")

    def _accept_prompt(self, native_turn_id: str) -> bool:
        if self._accounting_failed:
            return False
        if self._accepted or not self.execution_attempt_id:
            return self._accepted
        if not self._session_id or not self._record_execution_acceptance(ExecutionAcceptanceEvidence(
                accepted_turn_id=native_turn_id,
                provider_request_key=self.execution_attempt_id, native_binding_id=self._session_id)):
            self._hold_accounting_failure("Helios could not record Google acceptance. This Work remains blocked.")
            return False
        try:
            self._transcript.note_user_text(self._pending_text)
        except Exception:
            self._hold_accounting_failure("Helios could not save the accepted Google prompt. This Work remains blocked.")
            return False
        self._accepted = True
        self._accepted_turn_id = native_turn_id
        if self._prepared_prompt is not None:
            self._prepared_prompt.mark_sent()
            self._prepared_prompt = None
        self._pending_text = ""
        self._accept_uncertain_queue_delivery()
        self.emit("delivery-confirmed")
        return True

    def _flush_user_queue(self) -> None:
        """Keep a written queue head provisional until native acceptance."""
        if not self._user_queue or not self.is_accepting_input or self._uncertain_queue_delivery is not None:
            return
        reason = self._execution_block_reason()
        if reason:
            self.emit("error", reason)
            return
        delivery = self._user_queue[0]
        self._queue_dispatch_in_progress = True
        try:
            outcome = self.send_user_text(delivery[1])
        finally:
            self._queue_dispatch_in_progress = False
        if outcome.accepted or outcome.uncertain:
            # Both a successful write and a partial write await the provider's
            # own receipt. Stop must not restore either as sendable text.
            self._uncertain_queue_delivery = delivery

    def _dispatch_message(self, msg: dict[str, Any]) -> bool:
        if not isinstance(msg, dict):
            return False
        event = msg.get("event")
        if event == "init":
            if self._stopped or self._accounting_failed:
                return False
            info = msg.get("init")
            sid = msg.get("conversation_id")
            if not isinstance(info, dict) or not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
                self._protocol_failure("Google did not report a valid native conversation identity.")
                return False
            if self._session_id:
                if sid != self._session_id:
                    self._protocol_failure("Google changed its native conversation identity. The Work remains blocked.")
                return False
            if self._resume_session_id and sid != self._resume_session_id:
                self._protocol_failure("Google resumed a different conversation. The requested Work remains blocked.")
                return False
            expected_perm = "always-proceed" if self._permission_mode == "bypassPermissions" else "request-review"
            reported_model = str(info.get("model") or "").removeprefix("google:")
            reported_match = re.search(r"-(low|medium|high|xhigh|max)\Z", reported_model)
            if reported_match:
                reported_model = reported_model[:reported_match.start()]
            if (
                info.get("permission_mode") != expected_perm
                or reported_model != self._model
                or os.path.realpath(str(info.get("cwd", ""))) != os.path.realpath(self._cwd)
            ):
                self._protocol_failure("Google reported an unexpected model, project, or approval policy. The session was stopped.")
                return False
            self.init_tools = list(info.get("tools") or [])
            raw_mcp = info.get("mcp_servers")
            if isinstance(raw_mcp, list):
                self.init_mcp_servers = [dict(row) for row in raw_mcp if isinstance(row, dict)]
            elif isinstance(raw_mcp, dict):
                self.init_mcp_servers = [
                    {"name": str(k), **(v if isinstance(v, dict) else {"status": str(v)})}
                    for k, v in raw_mcp.items()
                ]
            else:
                self.init_mcp_servers = [
                    {"name": s["name"], "status": "configured"}
                    for s in google_env.list_mcp_servers()
                    if isinstance(s, dict) and s.get("name")
                ]
            try:
                self._transcript.bind_thread(sid)
            except Exception:
                self._hold_accounting_failure("Helios could not save the Google session identity. The session remains blocked.")
                return False
            self._session_id = sid
            self._touch_activity()
            self._accepting_input = True
            pending, self._startup_pending_text = self._startup_pending_text, None
            self._is_busy = False
            self.emit("session-started", sid, self._cwd, self._model)
            self.emit(
                "capabilities-updated",
                {"tools": list(self.init_tools), "mcp_servers": list(self.init_mcp_servers)},
            )
            if pending:
                self.send_user_text(pending, _reuse_execution_attempt=True)
        elif event == "step_update":
            step = msg.get("step_update")
            if not isinstance(step, dict) or not self._is_busy or self._stopped:
                return False
            if step.get("conversation_id") != self._session_id:
                self._protocol_failure("Google returned activity for a different conversation.")
                return False
            index = step.get("step_index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index < self._last_step_index:
                return False
            kind = step.get("step_type")
            if kind == "user_input" and (
                    index <= self._dispatch_step_floor or step.get("state") != "DONE"):
                # Receipt evidence must belong to this dispatch. Equal-index
                # response deltas remain valid within their own active step.
                return False
            self._last_step_index = index
            self._last_step_kind = str(kind or "")
            self._last_step_state = str(step.get("state") or "")
            self._touch_activity()
            self._clear_stall_notice()
            self._emit_step_context(step)
            if kind == "user_input":
                # Antigravity identifies accepted input by its conversation
                # and durable step index, rather than a separate turn UUID.
                was_accepted = self._accepted
                if self._accept_prompt(f"{self._session_id}/user_input/{index}") and not was_accepted:
                    self._accepted_input_index = index
            elif kind == "agent_response":
                delta = step.get("text_delta")
                if isinstance(delta, str) and delta:
                    self._response_text_by_step[index] = (
                        self._response_text_by_step.get(index, "") + delta
                    )
                if isinstance(delta, str) and delta and self._streaming is not None:
                    if not self._streaming.blocks:
                        self._streaming.blocks.append(Block(type="text"))
                    self._streaming.blocks[0].text += delta
                    self.emit("assistant-streaming", self._streaming)
            elif kind == "tool":
                self.emit("activity-updated", {"tool": str(step.get("tool_name") or ""), "state": str(step.get("state") or "")})
        elif event == "result":
            result = msg.get("result")
            if not isinstance(result, dict):
                return False
            status = str(result.get("status") or "")
            if not self._session_id:
                if status in {"ERROR", "INVALID"}:
                    detail, _ = scrub_sensitive(str(result.get("error") or "Google could not start the session."))
                    self._startup_error = detail[:2000]
                return False
            if not self.execution_attempt_id:
                return False
            if result.get("conversation_id") != self._session_id:
                self._protocol_failure("Google returned a result for a different conversation.")
                return False
            count = result.get("num_turns")
            if not isinstance(count, int) or isinstance(count, bool) or count <= self._seen_result_turns:
                # A delayed prior result cannot complete the next queued turn.
                return False
            if self._completed_turns and count != self._completed_turns + 1:
                self._protocol_failure("Google returned an uncorrelated turn result. This Work remains blocked.")
                return False
            if status not in {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID"}:
                self._protocol_failure("Google did not confirm a terminal result. This Work remains blocked.")
                return False
            # Latch provider evidence before local persistence. A duplicate
            # receipt must not append the same contribution again if saving
            # or releasing the first receipt failed. The durable lane stays
            # held until its accounting is repaired separately.
            self._seen_result_turns = count
            if not self._accept_prompt(f"{self._session_id}/result/{count}"):
                return False
            # Compute this turn's usage BEFORE writing the mirror: the
            # mirror's assistant record is where a restored session's
            # context fill is read from after a restart, and Codex/
            # OpenRouter already store theirs there. Without it the meter
            # sat at 0 until the next turn.
            turn_usage = {}
            usage = result.get("usage")
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        if not self._resume_session_id or self._completed_turns:
                            turn_usage[key] = max(0, value - self._cumulative_usage.get(key, 0))
                        self._cumulative_usage[key] = value
            if "cache_read_tokens" in turn_usage:
                # terminal_tokens' spelling, so footers and totals count cached input.
                turn_usage["cache_read_input_tokens"] = turn_usage["cache_read_tokens"]
            text = result.get("response")
            if isinstance(text, str) and text:
                turn = Turn(role="assistant")
                turn.add("text", text)
                if not self._record_execution_contribution(turn):
                    self._hold_accounting_failure("Helios could not save the Google response. This Work remains blocked.")
                    return False
                try:
                    self._transcript.append_assistant(
                        turn, model=self._model, result={"usage": turn_usage}
                    )
                except Exception:
                    self._hold_accounting_failure("Helios could not save the Google transcript. This Work remains blocked.")
                    return False
                self.emit("turn-appended", turn)
            normalized = {**result, "turn_usage": turn_usage, "subtype": "success" if status == "SUCCESS" else "aborted" if status in {"CANCELED", "INTERRUPTED"} else "error"}
            # The durable store uses provider-neutral names. Keep the native
            # fields in the display result, and translate only known counters
            # into the receipt schema (thinking is already part of output).
            usage_fields = {
                "input_tokens": "input_tokens",
                "output_tokens": "output_tokens",
                "thinking_tokens": "reasoning_output_tokens",
                "cache_read_tokens": "cache_read_input_tokens",
                "total_tokens": "total_tokens",
            }
            receipt_usage = {usage_fields[key]: value for key, value in turn_usage.items() if key in usage_fields}
            terminal_status = normalized["subtype"]
            receipt_status = {"success": "completed", "aborted": "aborted", "error": "failed"}[terminal_status]
            if not self._finish_execution_with_evidence(ExecutionTerminalEvidence(
                    evidence_type="provider_terminal",
                    status=receipt_status,
                    reason_code="provider_result.failed" if terminal_status == "error" else f"provider_result.{terminal_status}",
                    provider_status=status,
                    request_id=self.execution_attempt_id,
                    turn_id=self._accepted_turn_id,
                    native_id=self._session_id,
                    usage=receipt_usage,
                    queue_disposition="released")):
                self._hold_accounting_failure("Helios could not finish the Google execution receipt. This Work remains blocked.")
                return False
            self._is_busy = False
            self._completed_turns = count
            self._streaming = None
            self._cancel_liveness_watchdog()
            self._touch_activity()
            if status != "SUCCESS":
                detail, _ = scrub_sensitive(str(result.get("error") or status))
                self.emit("error", detail[:2000])
            self.emit("result", normalized)
            if status == "SUCCESS" and not self._stopped:
                self._flush_user_queue()
        return False

    def _emit_step_context(self, step: dict) -> None:
        """A finished step's ``usage`` is that one model call (measured on agy,
        2026-10-16: a two-call turn reported 13,044 then 2,275 + 10,998 cached,
        and the result summed them). Its prompt plus output is therefore the
        window's current fill. agy publishes no window size, so the
        denominator stays 0 — unknown, never guessed.
        """
        usage = step.get("usage")
        if step.get("state") != "DONE" or not isinstance(usage, dict):
            return
        counts = [usage.get(k) for k in ("input_tokens", "cache_read_tokens", "output_tokens")]
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts):
            return
        if sum(counts):
            self.emit("usage-updated", sum(counts), model_catalog.context_window_for(self._model))

    def _finish_predispatch_attempt(self) -> bool:
        if self._startup_pending_text is None:
            return True
        self._startup_pending_text = None
        self._is_busy = False
        return self._finish_execution_with_evidence(
            ExecutionTerminalEvidence(
                evidence_type="local_abort",
                status="aborted",
                reason_code="local_abort",
                queue_disposition="restored",
            )
        )

    def _start_liveness_watchdog(self) -> None:
        if self._liveness_source_id:
            return
        interval = max(1, int(self._LIVENESS_CHECK_INTERVAL_SECONDS))
        self._liveness_source_id = GLib.timeout_add_seconds(
            interval, self._on_liveness_tick
        )

    def _cancel_liveness_watchdog(self) -> None:
        sid, self._liveness_source_id = self._liveness_source_id, 0
        self._liveness_probe_in_flight = False
        if sid:
            try:
                GLib.source_remove(sid)
            except Exception:
                pass

    def _touch_activity(self) -> None:
        self._last_activity = time.monotonic()
        self._last_activity_wall = time.time()

    def _on_liveness_tick(self) -> bool:
        if (
            not self._is_busy
            or self._stopped
            or self._accounting_failed
            or not self.execution_attempt_id
            or not self._session_id
        ):
            self._liveness_source_id = 0
            return False
        silence = time.monotonic() - self._last_activity
        if silence >= self._IDLE_RECONCILE_SILENCE_SECONDS and not self._liveness_probe_in_flight:
            self._liveness_probe_in_flight = True
            proc = self._proc
            threading.Thread(
                target=self._probe_idle_worker,
                args=(
                    self.execution_attempt_id,
                    self._session_id,
                    self._turn_started_wall,
                    self._accepted_input_index,
                    proc.pid if proc is not None else 0,
                ),
                daemon=True,
                name="google-liveness-probe",
            ).start()
        return True

    def _note_stall(self) -> None:
        """Say so once when nothing anywhere has moved for a long time. Judged
        after a probe, so unreported trajectory progress is counted first."""
        silence = time.monotonic() - self._last_activity
        if silence < self._HARD_STALL_NOTICE_SECONDS or self._stall_notice_emitted:
            return
        self._stall_notice_emitted = True
        minutes = max(1, int(silence // 60))
        _log.warning(
            "Gemini turn %s shows no progress for %dm without idle confirmation",
            self._session_id[:8],
            minutes,
        )
        self.emit(
            "activity-updated",
            {
                "category": "stalled",
                "message": f"No output from Antigravity for {minutes}+ minutes",
            },
        )

    def _clear_stall_notice(self) -> None:
        """Work moved again: retract a shown stall notice and re-arm it."""
        if self._stall_notice_emitted:
            self._stall_notice_emitted = False
            self.emit("activity-updated", {"category": "phase", "phase": "requesting"})

    def _probe_idle_worker(
        self,
        attempt_id: str,
        session_id: str,
        turn_started_wall: float = 0.0,
        accepted_input_index: int | None = None,
        pid: int = 0,
    ) -> None:
        try:
            evidence = google_env.assess_turn_completion(
                session_id,
                turn_started_epoch=turn_started_wall,
                accepted_input_index=accepted_input_index,
                quiet_seconds=self._IDLE_DB_QUIET_SECONDS,
            )
            if evidence.finished:
                # agy holds a trajectory open while any of its cascades runs,
                # including a subagent that the summary index has not listed yet.
                open_ids = google_env.open_conversation_ids(pid)
                if open_ids is None:
                    evidence = replace(
                        evidence, finished=False,
                        reason="agy's open conversations could not be listed",
                    )
                elif open_ids:
                    evidence = replace(
                        evidence, finished=False,
                        reason="agy still has a conversation open",
                    )
        except Exception as exc:  # noqa: BLE001 - the in-flight flag must always clear
            evidence = google_env.TurnEvidence(
                False, f"the liveness probe failed ({type(exc).__name__})"
            )
        GLib.idle_add(self._apply_idle_probe, attempt_id, evidence)

    def _saw_final_response(self, trajectory: google_env.TrajectorySnapshot | None) -> bool:
        """Helios watched the trajectory's final response finish on stdout,
        and that response step itself carried text."""
        return (
            trajectory is not None
            and self._accepted
            and self._accepted_input_index is not None
            and self._last_step_kind == "agent_response"
            and self._last_step_state == "DONE"
            and self._last_step_index == trajectory.newest_index
            and bool(self._response_text_by_step.get(trajectory.newest_index, "").strip())
        )

    def _apply_idle_probe(
        self,
        attempt_id: str,
        evidence: google_env.TurnEvidence,
    ) -> bool:
        self._liveness_probe_in_flight = False
        if (
            not self._is_busy
            or self._stopped
            or self._accounting_failed
            or self.execution_attempt_id != attempt_id
            or not self._session_id
        ):
            return False
        now_wall = time.time()
        if self._last_activity_wall + 0.5 < evidence.progress_epoch <= now_wall + 2.0:
            # Antigravity wrote the trajectory (its own or a subagent's) after
            # the last stdout record: a long generation or delegated work is
            # progress, so the quiet spell starts again from that write. A
            # write stamped in the future (a clock step) is not progress.
            self._last_activity_wall = evidence.progress_epoch
            self._last_activity = time.monotonic() - max(
                0.0, now_wall - evidence.progress_epoch
            )
            self._clear_stall_notice()
            return False
        if (time.monotonic() - self._last_activity) < self._IDLE_RECONCILE_SILENCE_SECONDS:
            return False
        self._note_stall()
        streamed_text = ""
        if self._streaming is not None:
            streamed_text = "".join(
                block.text for block in self._streaming.blocks if block.text
            ).strip()
        refusal = ""
        if not evidence.finished:
            refusal = f"Antigravity has not finished it: {evidence.reason}"
        elif not self._saw_final_response(evidence.trajectory):
            refusal = "Helios did not see its final response finish with text"
        elif not streamed_text:
            refusal = "Helios holds no response text for it"
        if refusal:
            # Keep waiting. Closing a turn without its reply loses the work and
            # leaves the conversation looking stuck; a turn that never settles
            # still reaches the stall notice, and Stop stays available.
            if not self._probe_refusal_logged:
                self._probe_refusal_logged = True
                _log.info(
                    "Gemini turn %s quiet on stdout; not closing it: %s",
                    self._session_id[:8],
                    refusal,
                )
            return False
        trajectory = evidence.trajectory
        summary = evidence.summary
        if trajectory is None:  # _saw_final_response already requires one
            return False
        _log.warning(
            "Reconciling silent Gemini turn %s: its trajectory ends with finished "
            "response step %d and no result record arrived (summary %s)",
            self._session_id[:8],
            trajectory.newest_index,
            summary.status if summary is not None else "absent",
        )
        if not self._accept_prompt(f"{self._session_id}/reconciled/{trajectory.newest_index}"):
            return False
        turn = Turn(role="assistant")
        turn.add("text", streamed_text)
        if not self._record_execution_contribution(turn):
            self._hold_accounting_failure(
                "Helios could not save the reconciled Google response. This Work remains blocked."
            )
            return False
        try:
            self._transcript.append_assistant(
                turn, model=self._model, result={"usage": {}}
            )
        except Exception:
            self._hold_accounting_failure(
                "Helios could not save the reconciled Google transcript. This Work remains blocked."
            )
            return False
        self.emit("turn-appended", turn)
        if not self._finish_execution_with_evidence(
            ExecutionTerminalEvidence(
                evidence_type="provider_terminal",
                status="completed",
                reason_code="provider_result.reconciled_idle",
                provider_status="CASCADE_RUN_STATUS_IDLE",
                request_id=self.execution_attempt_id,
                turn_id=self._accepted_turn_id,
                native_id=self._session_id,
                usage={},
                queue_disposition="released",
            )
        ):
            self._hold_accounting_failure(
                "Helios could not finish the reconciled Google receipt. This Work remains blocked."
            )
            return False
        self._is_busy = False
        self._completed_turns += 1
        self._seen_result_turns = max(self._seen_result_turns + 1, self._completed_turns)
        self._streaming = None
        self._cancel_liveness_watchdog()
        self._touch_activity()
        self.emit(
            "result",
            {
                "conversation_id": self._session_id,
                "status": "SUCCESS",
                "subtype": "success",
                "response": streamed_text,
                "reconciled": True,
                "turn_usage": {},
            },
        )
        # Close the wedged stream-json child so its Google slot frees cleanly;
        # any queued follow-ups are returned to the composer on exit rather
        # than sent into a process that stopped emitting result frames.
        self._retire_wedged_process()
        return False

    def _retire_wedged_process(self) -> None:
        self._stopped = True
        self._accepting_input = False
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            threading.Thread(
                target=self._reap_stopped, args=(proc,), daemon=True
            ).start()

    def _hold_accounting_failure(self, message: str) -> None:
        self._accounting_failed = True
        self._accepting_input = False
        self._cancel_liveness_watchdog()
        self._finish_predispatch_attempt()
        self.emit("error", message)

    def _protocol_failure(self, message: str) -> None:
        self._finish_predispatch_attempt()
        self.emit("error", message)
        self.stop()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw_line in proc.stdout:
            try:
                message = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            GLib.idle_add(self._dispatch_message, message)
        code = proc.wait()
        GLib.idle_add(self._on_exit, code)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            detail, _ = scrub_sensitive(line.decode("utf-8", errors="replace"))
            self._stderr = (self._stderr + detail)[-4000:]

    def stop(self, *, interrupt: bool = True) -> None:
        self._stopped = True
        self._accepting_input = False
        self._cancel_liveness_watchdog()
        self._finish_predispatch_attempt()
        proc = self._proc
        ident = getattr(self, "_child_identity", None)
        if proc is not None and proc.poll() is None:
            group_token = getattr(self, "_child_group_token", None)
            descendants = (
                snapshot_group_descendants(ident.pid, ident.pid, ident.start_ticks)
                if ident is not None
                else ()
            )
            if descendants:
                self._stopped_descendants = descendants
            try:
                # Stop the whole local process group; do not infer remote
                # cancellation from a signal or a local process exit.
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            reap_kwargs: dict[str, Any] = {}
            if ident is not None:
                wake_if_stopped(
                    ident.pid,
                    ident.pid,
                    expected_start_ticks=ident.start_ticks,
                    expected_descendants=descendants,
                    expected_group_token=group_token,
                )
                reap_kwargs = {
                    "start_ticks": ident.start_ticks,
                    "group_token": group_token,
                    "descendants": descendants,
                }
            elif group_token is not None:
                reap_kwargs = {"group_token": group_token}
            threading.Thread(
                target=self._reap_stopped,
                args=(proc,),
                kwargs=reap_kwargs,
                daemon=True,
            ).start()

    def process_tree_target(self) -> ProcessTreeTarget | None:
        ident = getattr(self, "_child_identity", None)
        if ident is None:
            return None
        proc = self._proc
        return ProcessTreeTarget(
            pid=ident.pid,
            pgid=ident.pid,
            start_ticks=ident.start_ticks,
            poll=proc.poll if proc is not None else None,
            provider="google",
            group_token=getattr(self, "_child_group_token", None),
            descendants=(
                *getattr(self, "_stopped_descendants", ()),
                *snapshot_group_descendants(ident.pid, ident.pid, ident.start_ticks),
            ),
        )

    @staticmethod
    def _reap_stopped(
        proc: subprocess.Popen,
        *,
        start_ticks: int | None = None,
        group_token: str | None = None,
        descendants: tuple[tuple[int, int], ...] = (),
    ) -> None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        pid = getattr(proc, "pid", None)
        if not (isinstance(pid, int) and pid > 1):
            return
        if start_ticks is None and group_token is None:
            return
        target = ProcessTreeTarget(
            pid=pid,
            pgid=pid,
            start_ticks=start_ticks,
            provider="google",
            group_token=group_token,
            descendants=descendants,
        )
        status, target = inspect_process_tree(target)
        if status == "live":
            reap_process_trees(
                [target],
                timeout=0.0,
                kill_wait=0.25,
                send_term=False,
                unregister=True,
            )
        elif status in ("dead", "recycled"):
            unregister_if_dead(
                pid,
                pgid=pid,
                start_ticks=start_ticks,
                group_token=group_token,
                descendants=target.descendants,
            )

    def end_input(self) -> None:
        idle = (
            self.is_accepting_input and not self.is_busy
            and not self.execution_attempt_id and not self.queued_messages()
        )
        self._accepting_input = False
        self._finish_predispatch_attempt()
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            if idle:
                # EOF normally exits on its own. Keep a stuck idle child from
                # holding a slot forever, and leave active turns to their
                # regular terminal-receipt and cancellation paths.
                threading.Thread(
                    target=self._reap_stopped, args=(proc,), daemon=True,
                ).start()

    def answer_question(self, token: str, answers: Any) -> None:
        self.emit("error", "Antigravity headless cannot accept interactive approvals. Configure scoped permissions in the CLI or use an interactive session.")

    def _on_exit(self, code: int) -> bool:
        self._accepting_input = False
        ident = getattr(self, "_child_identity", None)
        if ident is not None:
            unregister_if_dead(
                ident.pid,
                pgid=ident.pid,
                start_ticks=ident.start_ticks,
                group_token=getattr(self, "_child_group_token", None),
            )
        self._cancel_liveness_watchdog()
        self._finish_predispatch_attempt()
        if self.execution_attempt_id:
            self.emit("error", "Google exited without a confirmed terminal receipt. This Work remains blocked; its input will not be replayed.")
        else:
            self._is_busy = False
        startup_error = self._startup_error if not self._session_id else ""
        detail = startup_error or (self._stderr.strip() if code else "")
        if detail and not self._stopped:
            self.emit("error", detail)
        self.emit("exited", code)
        return False
