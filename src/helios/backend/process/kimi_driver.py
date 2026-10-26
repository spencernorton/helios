"""Native Kimi Code subscription sessions over Agent Client Protocol (ACP)."""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import threading
import time
from typing import Any

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

from helios.backend import browser_tools, kimi_env, model_catalog
from helios.backend.attachments import kimi_blocks
from helios.backend.kimi_binary import KimiBinaryNotFound, find_kimi_binary
from helios.backend.process.codex_transcript import CodexTranscriptWriter
from helios.backend.process.env_scrub import NORVI_TRACKER_ENV, scrubbed_child_env
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence, ExecutionDispatchEvidence,
    ExecutionTerminalEvidence, MessageDelivery, PreparedPrompt, RequiredPromptContextError,
    UserMessageQueueMixin,
)
from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.project_perms import effective_execution_mode, is_home_cwd
from helios.backend.sensitive_text import scrub_sensitive
from helios.backend.transcript import ToolResult, images_in
from helios.log import get_logger

_log = get_logger("kimi-driver")
KIMI_INTERACTIVE_ENV = NORVI_TRACKER_ENV

_MODES = {
    "default": "default", "acceptEdits": "auto", "auto": "auto",
    "bypassPermissions": "yolo", "plan": "plan", "dontAsk": "yolo",
}
_TOOL_NAMES = {
    "read": "Read", "read_text_file": "Read", "write": "Write",
    "write_text_file": "Write", "edit": "Edit", "bash": "Bash",
    "shell": "Bash", "grep": "Grep", "glob": "Glob", "web_search": "WebSearch",
    "web_fetch": "WebFetch", "fetch_url": "FetchURL", "agent": "Agent",
    "task": "Agent", "todo": "TodoWrite", "todowrite": "TodoWrite",
}


def _normalize_tool(name: str, args: Any) -> tuple[str, dict]:
    key = name.lower().replace("-", "_")
    parsed = args if isinstance(args, dict) else {}
    display = _TOOL_NAMES.get(key, name or "Tool")
    if display in {"Read", "Write", "Edit"} and "path" in parsed:
        parsed = dict(parsed)
        parsed.setdefault("file_path", parsed.get("path"))
    return display, parsed


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, list):
        return "\n".join(filter(None, (_content_text(row) for row in content)))
    return ""


class KimiDriverSpawnError(RuntimeError):
    pass


class KimiAcpDriver(UserMessageQueueMixin, GObject.Object):
    display_name = "kimi"
    provider = "kimi"

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
        "thread-forked": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, cwd: str, model: str = "", permission_mode: str = "default",
                 resume_session_id: str | None = None, effort: str = "",
                 **kwargs: Any) -> None:
        GObject.Object.__init__(self)
        self._init_user_queue()
        self._cwd = cwd
        self._model = model
        self._effort = effort
        self._permission_mode = effective_execution_mode(permission_mode, cwd, provider=self.provider)
        self._resume_session_id = resume_session_id or ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._session_id = ""
        self._is_busy = False
        self._accepting_input = False
        self._stopped = False
        self._last_activity = time.monotonic()
        self._streaming: StreamingAssistant | None = None
        self._pending_text = ""
        self._prepared_prompt = None
        self._accepted = False
        self._accepted_turn_id = ""
        self._accounting_failed = False
        self._stderr = ""
        self._startup_pending_text: str | None = None
        self._rpc_seq = 0
        self._pending_rpc: dict[int, str] = {}
        self._root_turn_id = ""
        self._turn_usage: dict[str, int] = {}
        self._blocks: dict[str, Block] = {}
        self._tool_outputs: dict[str, str] = {}
        self._tool_images: dict[str, list] = {}
        self._agents: dict[str, dict[str, str]] = {}
        self._pending_requests: dict[str, dict] = {}
        self._setting_changes: dict[int, tuple[str, str, Any]] = {}
        self._prompt_rpc_id: int | None = None
        self._config_options: list[dict] = []
        self._transcript = CodexTranscriptWriter(cwd, provider=model_catalog.PROVIDER_KIMI)
        self.init_tools: list[str] = []
        self.init_mcp_servers: list[dict[str, Any]] = []
        self._mcp_servers: list[dict[str, Any]] = []

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
        return self._accepting_input and not self._stopped and not self._accounting_failed and self.is_running

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

    @property
    def observed_agent_root_turn_id(self) -> str:
        return self._root_turn_id

    def observed_agent_snapshot(self) -> dict:
        return copy.deepcopy(self._agents)

    def _send(self, method: str, params: dict | None = None, *, notify: bool = False) -> bool:
        if not self._proc or not self._proc.stdin:
            return False
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._rpc_seq += 1
            message["id"] = self._rpc_seq
            self._pending_rpc[self._rpc_seq] = method
        return self._write(message)

    def _respond(self, request_id: Any, result: Any) -> bool:
        return self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _respond_error(self, request_id: Any, code: int, message: str) -> bool:
        return self._write({"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": code, "message": message}})

    def _write(self, message: dict) -> bool:
        if not self._proc or not self._proc.stdin:
            return False
        line = (json.dumps(message) + "\n").encode()
        try:
            offset = 0
            while offset < len(line):
                written = self._proc.stdin.write(line[offset:])
                if not isinstance(written, int) or written <= 0:
                    raise OSError("incomplete input write")
                offset += written
            self._proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def _notify(callback, success: bool, detail: str = "") -> None:
        if callback is not None:
            try:
                callback(success, detail)
            except Exception:
                _log.exception("execution-setting callback failed")

    def _set_config(self, config_id: str, value: str, callback=None) -> bool:
        if not value or self._is_busy or not self.is_accepting_input:
            self._notify(callback, False, "the Kimi session is busy or not ready")
            return False
        if not self._send("session/set_config_option", {
                "sessionId": self._session_id, "configId": config_id, "value": value}):
            self._notify(callback, False, "Kimi is not accepting input")
            return False
        self._setting_changes[self._rpc_seq] = (config_id, value, callback)
        return True

    def set_effort(self, key: str, callback=None) -> bool:
        return self._set_config("thinking", key, callback)

    def set_permission_mode(self, mode: str, callback=None) -> bool:
        native = _MODES.get(mode)
        if native is None:
            self._notify(callback, False, "unknown permission mode")
            return False
        return self._set_config("mode", native, callback)

    def set_model(self, model: str, callback=None) -> bool:
        if not isinstance(model, str) or not model.startswith("kimi-code/"):
            self._notify(callback, False, "unknown Kimi model")
            return False
        if model == self._model:
            self._notify(callback, True)
            return True
        return self._set_config("model", model, callback)

    @property
    def supports_manual_compaction(self) -> bool:
        return True

    @property
    def supports_native_fork(self) -> bool:
        return True

    def request_review(self, arguments: str = "") -> MessageDelivery:
        text = f"/review {arguments.strip()}" if arguments.strip() else "/review"
        return self.send_user_text(text, with_context=False)

    def request_fork(self) -> MessageDelivery:
        if self._is_busy or not self.is_accepting_input or not self._session_id:
            return MessageDelivery("rejected")
        if not self._send("session/fork", {"sessionId": self._session_id}):
            return MessageDelivery("uncertain")
        return MessageDelivery("pending")

    def start(self) -> None:
        if not self._model or not self._model.startswith("kimi-code/"):
            raise KimiDriverSpawnError("Choose a discovered Kimi model before starting a session.")
        if is_home_cwd(self._cwd):
            raise KimiDriverSpawnError("Kimi sessions cannot start in your home folder. Choose a project directory.")
        try:
            binary = find_kimi_binary()
        except KimiBinaryNotFound as exc:
            raise KimiDriverSpawnError(str(exc)) from exc
        try:
            self._proc = subprocess.Popen(
                [str(binary.path), "acp"], cwd=self._cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=scrubbed_child_env(keep=KIMI_INTERACTIVE_ENV), bufsize=0,
                start_new_session=True)
        except OSError as exc:
            raise KimiDriverSpawnError("Could not start `kimi acp`.") from exc
        self._stopped = False
        self._accepting_input = False
        threading.Thread(target=self._read_stderr, daemon=True, name="kimi-stderr").start()
        threading.Thread(target=self._read_stdout, daemon=True, name="kimi-stdout").start()
        from helios import __version__ as helios_version

        if not self._send("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": kimi_env.CLIENT_NAME, "version": helios_version},
                "clientCapabilities": {"elicitation": {"form": {}}}}):
            raise KimiDriverSpawnError("Could not initialize `kimi acp`.")

    def _begin_session(self) -> None:
        browser = browser_tools.server_entry(self._cwd, "Kimi")
        self._mcp_servers = [{"name": browser_tools.MCP_SERVER_NAME, "type": "stdio", **browser}] if browser else []
        try:
            from helios.backend.estate_config import load_selected
            from helios.backend.estate_setup import _resolve_estate_script
            from helios.paths import state_dir
            from pathlib import Path
            import sys

            script = _resolve_estate_script(Path(__file__).resolve().parents[4])
            for config in load_selected():
                self._mcp_servers.append({
                    "name": config.name, "type": "stdio", "command": sys.executable,
                    "args": [str(script), "--server", config.name,
                             "--manifest", str(state_dir() / "estate-mcp.json")],
                    "env": {},
                })
        except Exception as exc:
            _log.info("estate MCP not forwarded to Kimi: %s", exc)
        params: dict[str, Any] = {"sessionId": self._resume_session_id} if self._resume_session_id else {
            "cwd": self._cwd, "mcpServers": self._mcp_servers}
        if self._resume_session_id:
            params.update({"cwd": self._cwd, "mcpServers": self._mcp_servers})
        self._send("session/resume" if self._resume_session_id else "session/new", params)

    def _on_session_bound(self, session: Any) -> None:
        if self._stopped or self._accounting_failed or not isinstance(session, dict):
            return
        sid = session.get("sessionId")
        if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
            self._protocol_failure("Kimi did not report a valid native session identity.")
            return
        if self._resume_session_id and sid != self._resume_session_id:
            self._protocol_failure("Kimi resumed a different session. The requested Work remains blocked.")
            return
        if self._session_id and sid != self._session_id:
            self._protocol_failure("Kimi changed its native session identity. The Work remains blocked.")
            return
        try:
            self._transcript.bind_thread(sid)
        except Exception:
            self._hold_accounting_failure("Helios could not save the Kimi session identity. The session remains blocked.")
            return
        self._session_id = sid
        self._config_options = [dict(row) for row in session.get("configOptions") or [] if isinstance(row, dict)]
        model = kimi_env._option(self._config_options, "model")
        thinking = kimi_env._option(self._config_options, "thought_level")
        requested_model = self._model
        requested_effort = self._effort
        if self._resume_session_id:
            self._model = str(model.get("currentValue") or self._model)
            self._effort = str(thinking.get("currentValue") or self._effort)
        self.init_tools = ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebSearch", "Agent"]
        self.init_mcp_servers = [{"name": str(row.get("name")), "status": "configured"}
                                 for row in self._mcp_servers]
        self._last_activity = time.monotonic()
        self._accepting_input = True
        pending, self._startup_pending_text = self._startup_pending_text, None
        self._is_busy = False
        self.emit("session-started", sid, self._cwd, self._model)
        self.emit("capabilities-updated", {"tools": list(self.init_tools),
                  "mcp_servers": list(self.init_mcp_servers), "config_options": copy.deepcopy(self._config_options)})
        if not self._resume_session_id:
            if requested_model != model.get("currentValue"):
                self.set_model(requested_model)
            if requested_effort and requested_effort != thinking.get("currentValue"):
                self.set_effort(requested_effort)
            self._send("session/set_mode", {"sessionId": sid, "modeId": _MODES.get(self._permission_mode, "default")})
        if pending:
            self.send_user_text(pending, _reuse_execution_attempt=True)

    def send_user_text(self, text: str, *, with_context: bool = True,
                       _reuse_execution_attempt: bool = False) -> MessageDelivery:
        if not self.is_running or self._stopped:
            self.emit("error", "Kimi process is not running. Sign in with `kimi login`.")
            return MessageDelivery("rejected")
        if self._accounting_failed:
            self.emit("error", "Helios could not verify Kimi accounting. New Work remains blocked.")
            return MessageDelivery("rejected")
        if self.is_busy and self._startup_pending_text is None:
            self.emit("error", "Kimi is still processing the current turn.")
            return MessageDelivery("rejected")
        if not self._session_id:
            if self._startup_pending_text is not None:
                self.emit("error", "Kimi is still preparing this session.")
                return MessageDelivery("rejected")
            reason = self._execution_block_reason()
            if reason:
                self.emit("error", reason)
                return MessageDelivery("rejected")
            try:
                self._prepare_prompt_context(text)
            except RequiredPromptContextError as exc:
                self.emit("error", exc.user_message)
                return MessageDelivery("rejected")
            admission_error = self._begin_execution_attempt(reuse_existing=_reuse_execution_attempt)
            if admission_error:
                self.emit("error", admission_error)
                return MessageDelivery("rejected")
            self._startup_pending_text = text
            self._is_busy = True
            self._last_activity = time.monotonic()
            return MessageDelivery("pending")
        reason = self._execution_block_reason()
        if reason:
            self.emit("error", reason)
            return MessageDelivery("rejected")
        try:
            prepared = self._prepare_prompt_context(text) if with_context else PreparedPrompt(text)
        except RequiredPromptContextError as exc:
            self.emit("error", exc.user_message)
            return MessageDelivery("rejected")
        reason = self._begin_execution_attempt(reuse_existing=_reuse_execution_attempt)
        if reason:
            self.emit("error", reason)
            return MessageDelivery("rejected")
        if not self._record_execution_dispatch(ExecutionDispatchEvidence(
                wire_prompt_text=prepared.text, provider_request_key=self.execution_attempt_id,
                native_binding_id=self._session_id)):
            released = self._finish_execution_with_evidence(ExecutionTerminalEvidence(
                evidence_type="local_abort", status="aborted", reason_code="local_abort",
                queue_disposition="restored"))
            if released:
                self.emit("error", "Helios could not persist the Kimi dispatch. The message was not sent.")
            else:
                self._hold_accounting_failure("Helios could not close the unsent Kimi reservation. This Work remains blocked.")
            return MessageDelivery("rejected")
        self._is_busy = True
        self._accepted = False
        self._accepted_turn_id = ""
        self._root_turn_id = ""
        self._pending_text = text
        self._prepared_prompt = prepared
        self._streaming = StreamingAssistant(model=self._model)
        self._blocks = {}
        self._tool_outputs = {}
        self._tool_images = {}
        self._agents = {}
        self._turn_usage = {}
        params = {"sessionId": self._session_id,
                  "prompt": [{"type": "text", "text": prepared.text}, *kimi_blocks(text)]}
        if not self._send("session/prompt", params):
            self.end_input()
            self.emit("error", "Kimi delivery is uncertain. This Work remains blocked until provider-backed recovery confirms the outcome.")
            return MessageDelivery("uncertain")
        self._prompt_rpc_id = self._rpc_seq
        self._root_turn_id = f"acp-prompt-{self._prompt_rpc_id}"
        self._last_activity = time.monotonic()
        return MessageDelivery("accepted")

    def _accept_prompt(self, native_turn_id: str) -> bool:
        if self._accounting_failed:
            return False
        if self._accepted or not self.execution_attempt_id:
            return self._accepted
        if not native_turn_id or not self._session_id or not self._record_execution_acceptance(
                ExecutionAcceptanceEvidence(accepted_turn_id=native_turn_id,
                provider_request_key=self.execution_attempt_id, native_binding_id=self._session_id)):
            self._hold_accounting_failure("Helios could not record Kimi acceptance. This Work remains blocked.")
            return False
        try:
            self._transcript.note_user_text(self._pending_text)
        except Exception:
            self._hold_accounting_failure("Helios could not save the accepted Kimi prompt. This Work remains blocked.")
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
            self._uncertain_queue_delivery = delivery

    def _dispatch_message(self, msg: dict[str, Any]) -> bool:
        if not isinstance(msg, dict):
            return False
        self._last_activity = time.monotonic()
        if "method" in msg and "id" in msg:
            self._on_client_request(msg)
            return False
        if "id" in msg and ("result" in msg or "error" in msg):
            method = self._pending_rpc.pop(msg["id"], "")
            change = self._setting_changes.pop(msg["id"], None)
            if change is not None:
                config_id, value, callback = change
                if "error" not in msg:
                    if config_id == "model":
                        self._model = value
                    elif config_id == "thinking":
                        self._effort = value
                    elif config_id == "mode":
                        self._permission_mode = next((key for key, native in _MODES.items() if native == value), "default")
                    self._notify(callback, True)
                else:
                    self._notify(callback, False, str((msg.get("error") or {}).get("message") or "Kimi refused the setting")[:200])
                return False
            if "error" in msg:
                detail, _ = scrub_sensitive(str((msg.get("error") or {}).get("message") or "request failed"))
                if method in ("initialize", "session/new", "session/resume"):
                    self._protocol_failure(f"Kimi refused to start: {detail[:400]}")
                elif method == "session/prompt" and self.execution_attempt_id:
                    self._finish_turn("failed", error_text=detail[:400])
                elif method:
                    self.emit("error", detail[:400])
                return False
            result = msg.get("result")
            if method == "initialize":
                capabilities = result.get("agentCapabilities") if isinstance(result, dict) else None
                if not isinstance(capabilities, dict) or not capabilities.get("loadSession"):
                    self._protocol_failure("The installed Kimi CLI lacks required ACP session support.")
                else:
                    self._begin_session()
            elif method in ("session/new", "session/resume"):
                self._on_session_bound(result)
            elif method == "session/fork" and isinstance(result, dict):
                fork_id = str(result.get("sessionId") or "")
                self.emit("thread-forked", {"source_thread_id": self._session_id, "thread_id": fork_id})
            elif method == "session/prompt":
                stop_reason = str((result or {}).get("stopReason") or "end_turn") if isinstance(result, dict) else "end_turn"
                usage = (result or {}).get("usage") if isinstance(result, dict) else None
                if isinstance(usage, dict):
                    self._record_usage(usage)
                self._finish_turn({"end_turn": "completed", "cancelled": "cancelled",
                                   "max_tokens": "failed", "refusal": "failed"}.get(stop_reason, stop_reason))
            return False
        if msg.get("method") == "session/update":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if params.get("sessionId") == self._session_id:
                self._on_update(params.get("update") if isinstance(params.get("update"), dict) else {})
        return False

    def _on_update(self, update: dict) -> None:
        kind = str(update.get("sessionUpdate") or "")
        if not kind or not self.execution_attempt_id:
            return
        self._accept_prompt(self._root_turn_id)
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            content = update.get("content")
            text = _content_text(content)
            if text and self._streaming is not None:
                block_type = "reasoning_summary" if kind == "agent_thought_chunk" else "text"
                previous = self._blocks.get(kind)
                if previous is None:
                    previous = Block(type=block_type)
                    self._blocks[kind] = previous
                    self._streaming.blocks.append(previous)
                previous.text += text
                self.emit("assistant-streaming", self._streaming)
        elif kind in ("tool_call", "tool_call_update"):
            self._on_tool_call(update)
        elif kind == "plan":
            entries = update.get("entries") or []
            self.emit("plan-updated", {"source": "turn", "plan": entries})
        elif kind == "available_commands_update":
            commands = update.get("availableCommands") or []
            self.emit("capabilities-updated", {"tools": list(self.init_tools),
                      "mcp_servers": list(self.init_mcp_servers), "commands": commands,
                      "config_options": copy.deepcopy(self._config_options)})
        elif kind == "config_option_update":
            options = update.get("configOptions") or []
            if isinstance(options, list):
                self._config_options = [dict(row) for row in options if isinstance(row, dict)]
        elif kind == "current_mode_update":
            self.emit("activity-updated", {"category": "mode", "message": str(update.get("currentModeId") or "")})
        elif kind == "usage_update":
            usage = update.get("usage") if isinstance(update.get("usage"), dict) else update
            self._record_usage(usage)
            used = usage.get("usedTokens") or usage.get("totalTokens")
            size = usage.get("size") or usage.get("contextWindow")
            if isinstance(used, int) and isinstance(size, int) and size > 0:
                self.emit("usage-updated", used, size)
        elif kind == "compaction":
            self.emit("context-compacted", update)

    def _on_tool_call(self, update: dict) -> None:
        tool_id = str(update.get("toolCallId") or "")
        if not tool_id or self._streaming is None:
            return
        block = self._blocks.get(tool_id)
        if block is None:
            block = Block(type="tool_use")
            self._blocks[tool_id] = block
            self._streaming.blocks.append(block)
        title = str(update.get("title") or update.get("kind") or "Tool")
        raw_input = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
        display = block.tool_use_name or title
        if raw_input or update.get("kind") or not block.tool_use_name:
            display, parsed = _normalize_tool(str(raw_input.get("tool") or update.get("kind") or title), raw_input)
            block.tool_use_name = display
            block.tool_use_input_json = json.dumps(parsed or raw_input or {"title": title})
        block.tool_use_id = tool_id
        text = _content_text(update.get("content"))
        if text:
            self._tool_outputs[tool_id] = text
        images = images_in([update.get("content")])
        if images:
            self._tool_images[tool_id] = images
        status = str(update.get("status") or "in_progress")
        if status in {"completed", "failed", "cancelled"}:
            self._agents.pop(tool_id, None)
        elif str(update.get("kind") or "") in {"agent", "task", "subagent"}:
            self._agents[tool_id] = {"status": "working", "name": title, "role": title,
                                     "message": "", "tool": "subagent", "task_id": tool_id}
        self.emit("assistant-streaming", self._streaming)
        self.emit("agents-updated", copy.deepcopy(self._agents))
        self.emit("activity-updated", {"category": "tool-progress", "tool": display,
                                       "message": f"{title}: {status}"})

    def _record_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        aliases = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                   "reasoningTokens": "reasoning_output_tokens", "cachedReadTokens": "cache_read_input_tokens",
                   "totalTokens": "total_tokens"}
        for source, target in aliases.items():
            value = usage.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self._turn_usage[target] = value

    def _on_client_request(self, msg: dict) -> None:
        method = str(msg.get("method") or "")
        request_id = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        token = str(request_id)
        if method == "session/request_permission":
            options = [row for row in params.get("options") or [] if isinstance(row, dict)]
            tool = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            self._pending_requests[token] = msg
            payload = {"questions": [{"question": f"Allow Kimi to run {tool.get('title') or 'this tool'}?",
                       "header": "Approval", "multiSelect": False,
                       "options": [{"label": str(row.get("name") or row.get("optionId") or ""),
                                    "description": str(row.get("kind") or "")} for row in options]}]}
            self.emit("question-asked", payload, token)
        elif method == "elicitation/create":
            self._pending_requests[token] = msg
            self.emit("question-asked", self._question_payload(params), token)
        elif method in ("fs/read_text_file", "fs/write_text_file", "terminal/create"):
            self._respond_error(request_id, -32601, "Helios leaves filesystem and terminal execution to the Kimi CLI.")
        else:
            self._respond_error(request_id, -32601, f"Unsupported ACP client method: {method}")

    def _question_payload(self, params: dict) -> dict:
        questions = params.get("questions") if isinstance(params.get("questions"), list) else [params]
        rows = []
        for question in questions:
            if not isinstance(question, dict):
                continue
            options = question.get("options") or []
            rows.append({"question": str(question.get("question") or question.get("message") or "Kimi requested input"),
                         "header": str(question.get("header") or "Kimi"),
                         "multiSelect": bool(question.get("multiSelect")),
                         "options": [{"label": str(row.get("label") or row.get("name") or row.get("value") or ""),
                                      "description": str(row.get("description") or "")}
                                     for row in options if isinstance(row, dict)]})
        return {"questions": rows}

    def answer_question(self, token: str, answer: Any) -> None:
        request = self._pending_requests.pop(token, None)
        if request is None:
            return
        request_id = request["id"]
        method = request.get("method")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if method == "session/request_permission":
            selected = None
            if isinstance(answer, str):
                selected = next((row for row in params.get("options") or []
                                 if isinstance(row, dict) and row.get("name") == answer), None)
            if selected is None:
                self._respond(request_id, {"outcome": {"outcome": "cancelled"}})
            else:
                self._respond(request_id, {"outcome": {"outcome": "selected",
                              "optionId": selected.get("optionId")}})
        elif method == "elicitation/create":
            if answer is None:
                self._respond(request_id, {"action": "cancel"})
            else:
                values = answer if isinstance(answer, dict) else {"answer": answer}
                self._respond(request_id, {"action": "accept", "content": values})

    def _finish_turn(self, terminal: str, *, error_text: str = "") -> None:
        if not self.execution_attempt_id:
            return
        if not self._accept_prompt(self._accepted_turn_id or self._root_turn_id):
            return
        subtype = {"completed": "success", "cancelled": "aborted"}.get(terminal, "error")
        if self._streaming is not None:
            turn = self._streaming.to_turn()
            for tool_use in turn.tool_uses:
                output = self._tool_outputs.get(tool_use.id)
                images = self._tool_images.get(tool_use.id, [])
                if output or images:
                    turn.tool_results.append(ToolResult(tool_use_id=tool_use.id, content=output or "",
                                            is_error=subtype == "error", images=images))
            if turn.content or turn.tool_uses:
                if not self._record_execution_contribution(turn):
                    self._hold_accounting_failure("Helios could not save the Kimi response. This Work remains blocked.")
                    return
                try:
                    self._transcript.append_assistant(turn, model=self._model)
                except Exception:
                    self._hold_accounting_failure("Helios could not save the Kimi transcript. This Work remains blocked.")
                    return
                self.emit("turn-appended", turn)
        receipt_status = {"success": "completed", "aborted": "aborted", "error": "failed"}[subtype]
        if not self._finish_execution_with_evidence(ExecutionTerminalEvidence(
                evidence_type="provider_terminal", status=receipt_status,
                reason_code=f"provider_result.{subtype}", provider_status=terminal or subtype,
                request_id=self.execution_attempt_id, turn_id=self._accepted_turn_id,
                native_id=self._session_id, usage=self._turn_usage, queue_disposition="released")):
            self._hold_accounting_failure("Helios could not finish the Kimi execution receipt. This Work remains blocked.")
            return
        self._is_busy = False
        self._streaming = None
        self._blocks = {}
        self._tool_outputs = {}
        self._tool_images = {}
        self._prompt_rpc_id = None
        self._last_activity = time.monotonic()
        result = {"subtype": subtype, "usage": self._turn_usage, "turn_usage": self._turn_usage}
        if error_text:
            result["error"] = error_text
            self.emit("error", error_text[:2000])
        self.emit("turn-status-updated", {"turnId": self._root_turn_id, "status": terminal or subtype})
        self.emit("result", result)
        if subtype == "success" and not self._stopped:
            self._flush_user_queue()

    def _finish_predispatch_attempt(self) -> bool:
        if self._startup_pending_text is None:
            return True
        self._startup_pending_text = None
        self._is_busy = False
        return self._finish_execution_with_evidence(ExecutionTerminalEvidence(
            evidence_type="local_abort", status="aborted", reason_code="local_abort",
            queue_disposition="restored"))

    def _hold_accounting_failure(self, message: str) -> None:
        self._accounting_failed = True
        self._accepting_input = False
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
        self._finish_predispatch_attempt()
        if interrupt and self._session_id and self._is_busy:
            self._send("session/cancel", {"sessionId": self._session_id}, notify=True)
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except OSError:
                pass
            threading.Thread(target=self._reap_stopped, args=(self._proc,), daemon=True).start()

    @staticmethod
    def _reap_stopped(proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def stop_task(self, actor_id: str) -> bool:
        return False

    def end_input(self) -> None:
        self._accepting_input = False
        self._finish_predispatch_attempt()
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def _on_exit(self, code: int) -> bool:
        self._accepting_input = False
        self._finish_predispatch_attempt()
        if self.execution_attempt_id:
            self.emit("error", "Kimi exited without a confirmed terminal receipt. This Work remains blocked; its input will not be replayed.")
        else:
            self._is_busy = False
        if code and self._stderr and not self._stopped:
            self.emit("error", self._stderr.strip())
        self.emit("exited", code)
        return False
