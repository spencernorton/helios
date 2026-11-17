"""Headless GTK4 + real child subprocess (`subprocess.Popen`) proofing suite for:
- Real OS-pipe `KimiAcpDriver` (`session/new` + cold `session/resume` + refusal abort),
- Real OS-pipe `MuseServeDriver` (cold `session/resume` `session/setModel` + refusal abort),
- Real OS-pipe `GeminiCliDriver` (cross-provider detour into a different Google model + return),
- Staged 0-turn model switch surviving sidebar navigation AND full window restart,
- Idle rebound driver `set_model` refusal fallback + mid-stream multi-switch coalescing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("GTK display unavailable", allow_module_level=True)

from helios.backend import (  # noqa: E402
    conversation_perms,
    google_env,
    kimi_env,
    model_catalog,
    muse_env,
    projects,
    session_providers,
    work_store,
)
from helios.backend.composer_state import DraftBook  # noqa: E402
from helios.backend.continuity_capsule import CONTINUITY_CAPSULE_PREFIX  # noqa: E402
from helios.backend.process import (  # noqa: E402
    gemini_driver as gd_mod,
    kimi_driver as kd_mod,
    muse_driver as md_mod,
)
from helios.backend.process.driver_manager import DriverManager  # noqa: E402
from helios.backend.process.gemini_driver import GeminiCliDriver  # noqa: E402
from helios.backend.process.kimi_driver import KimiAcpDriver  # noqa: E402
from helios.backend.process.muse_driver import MuseServeDriver  # noqa: E402
from helios.backend.projects import Project  # noqa: E402
from helios.backend.session_router import ChatTarget  # noqa: E402
from helios.backend.transcript import ToolUse, Turn  # noqa: E402
from helios.backend.work_coordinator import WorkCoordinator, tag_driver  # noqa: E402
from helios.backend.work_store import WorkStore  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402
from helios.widgets.session_list import SessionList  # noqa: E402
from helios.widgets.transcript_view import HandoffDividerRow, TranscriptView  # noqa: E402
from helios.window_goal import GoalWorkMixin  # noqa: E402


def _pump_until(pred, *, timeout: float = 5.0) -> None:
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        if pred():
            return
        time.sleep(0.01)
    while ctx.pending():
        ctx.iteration(False)
    assert pred(), "Condition did not settle before timeout"


def _pump_gtk(iterations: int = 30) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(iterations):
        if not ctx.pending():
            break
        ctx.iteration(False)


def _walk_labels(widget: Gtk.Widget) -> list[str]:
    out: list[str] = []
    if isinstance(widget, Gtk.Label):
        txt = widget.get_label()
        if txt:
            out.append(txt)
    child = widget.get_first_child()
    while child is not None:
        out.extend(_walk_labels(child))
        child = child.get_next_sibling()
    return out


def _list_children(box: Gtk.Widget) -> list[Gtk.Widget]:
    out: list[Gtk.Widget] = []
    child = box.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


def _wire_coordinator_hooks(driver, coord: WorkCoordinator, work_id: str, provider: str) -> None:
    part = coord.participant(work_id, provider)
    if part is not None:
        tag_driver(driver, part)
    else:
        driver._helios_work_id = work_id
        driver._helios_participant_provider = provider

    def _prompt_ctx(_drv, user_text: str):
        model_id = str(getattr(_drv, "model", "") or getattr(_drv, "_model", "") or "")
        return coord.prepare_prompt(
            work_id=work_id,
            provider=provider,
            model=model_id,
            text=user_text,
            goal=None,
        )

    def _admit(cand):
        p = coord.participant(work_id, provider)
        if p is None:
            p = coord.bind_participant(
                work_id,
                provider,
                native_id=str(getattr(cand, "session_id", "") or ""),
                model=str(getattr(cand, "model", "") or ""),
            )
            tag_driver(cand, p)
        att = coord.start_execution_attempt(
            work_id=work_id,
            participant_id=p.participant_id,
            provider=provider,
            participant_generation=p.generation,
            metadata={"model": str(getattr(cand, "model", "") or "")},
        )
        return att.attempt_id, ""

    def _finish(_cand, attempt_id: str, status: str, reason: str) -> None:
        coord.finish_execution_attempt(attempt_id, status=status, terminal_reason=reason)

    def _dispatch(_cand, attempt_id: str, ev) -> None:
        coord.record_execution_dispatch(
            attempt_id,
            wire_prompt_text=ev.wire_prompt_text,
            provider_request_key=ev.provider_request_key,
            pending_capsule=ev.pending_capsule,
        )

    def _accept(cand, attempt_id: str, ev) -> None:
        sid = str(getattr(cand, "session_id", "") or ev.native_binding_id or "")
        if sid:
            p = coord.bind_participant(
                work_id,
                provider,
                native_id=sid,
                model=str(getattr(cand, "model", "") or ""),
            )
            tag_driver(cand, p)
        coord.record_execution_acceptance(
            attempt_id,
            accepted_turn_id=ev.accepted_turn_id,
            provider_request_key=ev.provider_request_key,
        )

    def _finish_ev(_cand, attempt_id: str, ev) -> None:
        receipt = None
        if ev.evidence_type in ("provider_terminal", "verified_rejection"):
            receipt = {
                "evidence_type": ev.evidence_type,
                "provider_status": ev.provider_status or ev.status,
                "request_id": ev.request_id,
                "turn_id": ev.turn_id,
                "native_id": ev.native_id,
            }
            receipt = {k: v for k, v in receipt.items() if v}
        coord.finish_execution_attempt(
            attempt_id,
            status=ev.status,
            terminal_reason=ev.reason_code,
            terminal_receipt=receipt,
            queue_disposition=ev.queue_disposition,
        )

    driver.set_prompt_context_provider(_prompt_ctx)
    driver.set_execution_attempt_controller(
        _admit,
        _finish,
        record_dispatch=_dispatch,
        record_acceptance=_accept,
        finish_with_evidence=_finish_ev,
        record_contribution=lambda cand, turn: coord.record_turn(cand, turn),
    )


def _write_executable(path: Path, code: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + code, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_real_subprocess_kimi_acp_model_switch_on_new_and_cold_resume_before_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    proj_dir = tmp_path / "workspace" / "app"
    proj_dir.mkdir(parents=True)
    wire_log = tmp_path / "kimi_wire.jsonl"
    refuse_flag = tmp_path / "refuse_model"

    kimi_bin = _write_executable(
        tmp_path / "bin" / "kimi",
        f"""
import json, pathlib, sys
log_path = pathlib.Path({str(wire_log)!r})
omit_cfg_path = log_path.parent / "omit_cfg"
refuse_path = pathlib.Path({str(refuse_flag)!r})
cur_sid = "kimi-sess-1"
cur_model = "kimi-code/k3"

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg) + "\\n")
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {{}}
    if method == "initialize":
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"protocolVersion": 1, "agentCapabilities": {{"loadSession": True}}}}}})
    elif method in ("session/new", "session/resume"):
        if method == "session/resume":
            cur_sid = params.get("sessionId") or cur_sid
        send({{
            "jsonrpc": "2.0",
            "id": mid,
            "result": {{
                "sessionId": cur_sid,
                "configOptions": [] if (method == "session/resume" and omit_cfg_path.exists()) else [
                    {{"id": "model", "category": "model", "currentValue": cur_model}},
                    {{"id": "thought_level", "category": "thought_level", "currentValue": "medium"}},
                ],
            }},
        }})
    elif method == "session/set_mode":
        send({{"jsonrpc": "2.0", "id": mid, "result": {{}}}})
    elif method == "session/set_config_option":
        if params.get("configId") == "model" and refuse_path.exists():
            send({{"jsonrpc": "2.0", "id": mid, "error": {{"code": -32602, "message": "tier locked"}}}})
        else:
            if params.get("configId") == "model":
                cur_model = params.get("value", cur_model)
            send({{"jsonrpc": "2.0", "id": mid, "result": {{}}}})
    elif method == "session/prompt":
        send({{
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {{
                "sessionId": cur_sid,
                "update": {{
                    "sessionUpdate": "agent_message_chunk",
                    "content": {{"type": "text", "text": "Kimi reply from " + cur_model}},
                }},
            }},
        }})
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})
""",
    )
    fake_bin_obj = types.SimpleNamespace(path=kimi_bin)
    monkeypatch.setattr(kimi_env, "find_kimi_binary", lambda: fake_bin_obj)
    monkeypatch.setattr(kd_mod, "find_kimi_binary", lambda: fake_bin_obj)
    monkeypatch.setattr(kd_mod, "scrubbed_child_env", lambda **_kw: dict(os.environ))

    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    drivers: list[KimiAcpDriver] = []
    try:
        coord = WorkCoordinator(store)
        work = store.create_work(cwd=str(proj_dir), lead_provider="google", objective="Cross-model Kimi proof")
        coord.enable_continuity(work.work_id)
        p_g = coord.bind_participant(work.work_id, "google", native_id="gem-init", model="gemini-carbon")
        g_drv = types.SimpleNamespace(model="gemini-carbon")
        tag_driver(g_drv, p_g)
        coord.record_user_message(g_drv, "Find bottleneck in cache.py")
        g_turn = Turn(role="assistant", model="gemini-carbon")
        g_turn.text_parts.append(" Bottleneck is linear scan in evict_oldest().")
        coord.record_turn(g_drv, g_turn)

        # 1) session/new with model="kimi-code/k3.1-pro" (server defaults to k3):
        coord.select_lead(work.work_id, "kimi")
        coord.activate_model_binding(work.work_id, "kimi", model="kimi-code/k3.1-pro")
        drv1 = KimiAcpDriver(cwd=str(proj_dir), model="kimi-code/k3.1-pro")
        drivers.append(drv1)
        _wire_coordinator_hooks(drv1, coord, work.work_id, "kimi")
        turns1: list[Turn] = []
        drv1.connect("turn-appended", lambda _d, t: turns1.append(t))
        drv1.start()
        out1 = drv1.send_user_text("Optimize evict_oldest() with heap.")
        assert out1.pending or out1.accepted
        _pump_until(lambda: len(turns1) == 1)
        assert "kimi-code/k3.1-pro" in turns1[0].text
        drv1.stop()

        frames1 = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines()]
        methods1 = [
            (f.get("method"), (f.get("params") or {}).get("configId"), (f.get("params") or {}).get("value"))
            for f in frames1
        ]
        idx_set1 = methods1.index(("session/set_config_option", "model", "kimi-code/k3.1-pro"))
        idx_prompt1 = next(i for i, m in enumerate(methods1) if m[0] == "session/prompt")
        assert idx_set1 < idx_prompt1, "set_config_option(model) MUST fire before session/prompt on session/new!"
        prompt1_blocks = frames1[idx_prompt1]["params"]["prompt"]
        assert any(CONTINUITY_CAPSULE_PREFIX in str(b.get("text", "")) for b in prompt1_blocks)

        # 2) Cold session/resume switching from k3.1-pro -> kimi-code/k3.5-sonnet after GPT detour:
        wire_log.write_text("", encoding="utf-8")
        coord.select_lead(work.work_id, "openai")
        p_oa = coord.bind_participant(work.work_id, "openai", native_id="oa-det", model="gpt-6.1-sol")
        oa_drv = types.SimpleNamespace(model="gpt-6.1-sol")
        tag_driver(oa_drv, p_oa)
        coord.record_user_message(oa_drv, "Add heap invariant check.")
        oa_t = Turn(role="assistant", model="gpt-6.1-sol")
        oa_t.text_parts.append("Added assert_min_heap() helper.")
        coord.record_turn(oa_drv, oa_t)

        coord.select_lead(work.work_id, "kimi")
        coord.activate_model_binding(work.work_id, "kimi", model="kimi-code/k3.5-sonnet")
        drv2 = KimiAcpDriver(
            cwd=str(proj_dir),
            model="kimi-code/k3.5-sonnet",
            resume_session_id="kimi-sess-1",
        )
        drivers.append(drv2)
        _wire_coordinator_hooks(drv2, coord, work.work_id, "kimi")
        turns2: list[Turn] = []
        drv2.connect("turn-appended", lambda _d, t: turns2.append(t))
        drv2.start()
        drv2.send_user_text("Verify heap complexity.")
        _pump_until(lambda: len(turns2) == 1)
        assert "kimi-code/k3.5-sonnet" in turns2[0].text
        drv2.stop()

        frames2 = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines()]
        methods2 = [
            (f.get("method"), (f.get("params") or {}).get("configId"), (f.get("params") or {}).get("value"))
            for f in frames2
        ]
        idx_set2 = methods2.index(("session/set_config_option", "model", "kimi-code/k3.5-sonnet"))
        idx_prompt2 = next(i for i, m in enumerate(methods2) if m[0] == "session/prompt")
        assert idx_set2 < idx_prompt2, "set_config_option(model) MUST fire before session/prompt on cold resume!"

        # 2b) Cold session/resume when server omits configOptions ([]): STILL sends set_config_option!
        wire_log.write_text("", encoding="utf-8")
        (tmp_path / "omit_cfg").write_text("1", encoding="utf-8")
        drv2b = KimiAcpDriver(
            cwd=str(proj_dir),
            model="kimi-code/k3-256k",
            resume_session_id="kimi-sess-1",
        )
        drivers.append(drv2b)
        _wire_coordinator_hooks(drv2b, coord, work.work_id, "kimi")
        turns2b: list[Turn] = []
        drv2b.connect("turn-appended", lambda _d, t: turns2b.append(t))
        drv2b.start()
        drv2b.send_user_text("Verify omitted configOptions resume.")
        _pump_until(lambda: len(turns2b) == 1)
        assert "kimi-code/k3-256k" in turns2b[0].text
        drv2b.stop()
        (tmp_path / "omit_cfg").unlink()

        frames2b = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines()]
        methods2b = [
            (f.get("method"), (f.get("params") or {}).get("configId"), (f.get("params") or {}).get("value"))
            for f in frames2b
        ]
        idx_set2b = methods2b.index(("session/set_config_option", "model", "kimi-code/k3-256k"))
        idx_prompt2b = next(i for i, m in enumerate(methods2b) if m[0] == "session/prompt")
        assert idx_set2b < idx_prompt2b

        # 3) Cold session/resume when Kimi refuses model -> aborts locally without sending prompt!
        wire_log.write_text("", encoding="utf-8")
        refuse_flag.write_text("1", encoding="utf-8")
        drv3 = KimiAcpDriver(
            cwd=str(proj_dir),
            model="kimi-code/k3-ultra",
            resume_session_id="kimi-sess-1",
        )
        drivers.append(drv3)
        _wire_coordinator_hooks(drv3, coord, work.work_id, "kimi")
        errors3: list[str] = []
        drv3.connect("error", lambda _d, msg: errors3.append(msg))
        drv3.start()
        drv3.send_user_text("Must not send on wrong model.")
        _pump_until(lambda: len(errors3) >= 1)
        frames3 = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines()]
        assert all(f.get("method") != "session/prompt" for f in frames3)
        assert store.list_active_execution_attempts() == []
    finally:
        for d in drivers:
            try:
                d.stop()
            except Exception:
                pass
        store.close()


def test_real_subprocess_muse_serve_cold_resume_model_switch_accept_and_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    proj_dir = tmp_path / "workspace" / "muse_app"
    proj_dir.mkdir(parents=True)
    wire_log = tmp_path / "muse_wire.jsonl"
    refuse_flag = tmp_path / "muse_refuse"

    muse_bin = _write_executable(
        tmp_path / "bin" / "muse",
        f"""
import json, pathlib, sys
log_path = pathlib.Path({str(wire_log)!r})
refuse_path = pathlib.Path({str(refuse_flag)!r})
cur_model = "muse-wonder-1.0"

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg) + "\\n")
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {{}}
    if method == "initialize":
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"grantedCapabilities": []}}}})
    elif method in ("session/start", "session/resume"):
        sid = params.get("sessionId") or "muse-sess-1"
        send({{
            "jsonrpc": "2.0",
            "id": mid,
            "result": {{
                "session": {{
                    "sessionId": sid,
                    "modelId": cur_model,
                    "workspaceRoot": {str(proj_dir)!r},
                }}
            }},
        }})
    elif method == "usage/read":
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"usage": {{}}}}}})
    elif method == "session/setModel":
        if refuse_path.exists():
            send({{"jsonrpc": "2.0", "id": mid, "result": {{"status": "rejected"}}}})
        else:
            cur_model = (params.get("model") or {{}}).get("modelId") or cur_model
            send({{"jsonrpc": "2.0", "id": mid, "result": {{"status": "accepted", "modelId": cur_model}}}})
    elif method == "turn/start":
        tid = "muse-turn-1"
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"turnId": tid}}}})
        send({{"jsonrpc": "2.0", "method": "turn/started", "params": {{"turnId": tid}}}})
        send({{
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {{
                "item": {{
                    "itemId": "msg-1",
                    "kind": "agentMessage",
                    "turnId": tid,
                    "status": "completed",
                    "text": "Muse reply on " + cur_model,
                }}
            }},
        }})
        send({{
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {{"turnId": tid, "status": "completed"}},
        }})
""",
    )
    fake_bin_obj = types.SimpleNamespace(path=muse_bin)
    monkeypatch.setattr(muse_env, "find_muse_binary", lambda: fake_bin_obj)
    monkeypatch.setattr(md_mod, "find_muse_binary", lambda: fake_bin_obj)
    monkeypatch.setattr(md_mod, "scrubbed_child_env", lambda **_kw: dict(os.environ))

    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    drivers: list[MuseServeDriver] = []
    try:
        coord = WorkCoordinator(store)
        work = store.create_work(cwd=str(proj_dir), lead_provider="muse", objective="Muse cold resume switch")
        coord.enable_continuity(work.work_id)
        coord.bind_participant(work.work_id, "muse", native_id="muse-sess-1", model="muse-wonder-1.0")

        coord.select_lead(work.work_id, "openai")
        p_oa = coord.bind_participant(work.work_id, "openai", native_id="oa-m1", model="gpt-6.1-sol")
        oa_drv = types.SimpleNamespace(model="gpt-6.1-sol")
        tag_driver(oa_drv, p_oa)
        coord.record_user_message(oa_drv, "Draft schema v2.")
        oa_t = Turn(role="assistant", model="gpt-6.1-sol")
        oa_t.text_parts.append("Schema v2 adds idx_events_seq.")
        coord.record_turn(oa_drv, oa_t)

        # Return to Muse on DIFFERENT model (muse-spark-1.3) resuming muse-sess-1:
        coord.select_lead(work.work_id, "muse")
        coord.activate_model_binding(work.work_id, "muse", model="muse-spark-1.3")
        drv1 = MuseServeDriver(
            cwd=str(proj_dir),
            model="muse-spark-1.3",
            resume_session_id="muse-sess-1",
        )
        drivers.append(drv1)
        _wire_coordinator_hooks(drv1, coord, work.work_id, "muse")
        turns1: list[Turn] = []
        drv1.connect("turn-appended", lambda _d, t: turns1.append(t))
        drv1.start()
        drv1.send_user_text("Apply schema v2 migration.")
        _pump_until(lambda: len(turns1) == 1)
        assert "muse-spark-1.3" in turns1[0].text
        drv1.stop()

        frames1 = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines()]
        methods1 = [f.get("method") for f in frames1]
        assert methods1.index("session/setModel") < methods1.index("turn/start")
        tstart = frames1[methods1.index("turn/start")]["params"]["input"]
        assert any("idx_events_seq" in str(item.get("text", "")) for item in tstart)

        # Refusal path: session/setModel rejected -> turn/start NEVER fires & slot released!
        wire_log.write_text("", encoding="utf-8")
        refuse_flag.write_text("1", encoding="utf-8")
        drv2 = MuseServeDriver(
            cwd=str(proj_dir),
            model="muse-spark-2.0",
            resume_session_id="muse-sess-1",
        )
        drivers.append(drv2)
        _wire_coordinator_hooks(drv2, coord, work.work_id, "muse")
        errors2: list[str] = []
        drv2.connect("error", lambda _d, msg: errors2.append(msg))
        drv2.start()
        drv2.send_user_text("Should abort cleanly.")
        _pump_until(lambda: len(errors2) >= 1)
        frames2 = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines()]
        assert all(f.get("method") != "turn/start" for f in frames2)
        assert store.list_active_execution_attempts() == []
    finally:
        for d in drivers:
            try:
                d.stop()
            except Exception:
                pass
        store.close()


def test_real_subprocess_gemini_cross_provider_detour_into_different_google_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    proj_dir = tmp_path / "workspace" / "gem_app"
    proj_dir.mkdir(parents=True)
    argv_log = tmp_path / "agy_invocations.jsonl"

    agy_bin = _write_executable(
        tmp_path / "bin" / "agy",
        f"""
import json, pathlib, sys
log_path = pathlib.Path({str(argv_log)!r})
args = sys.argv[1:]
model = args[args.index("--model") + 1]
conv = args[args.index("--conversation") + 1] if "--conversation" in args else ""
# Simulate real Antigravity CLI model immutability per conversation:
if conv == "gem-carbon-1" and model != "gemini-carbon":
    reported_model = "gemini-carbon"
else:
    reported_model = model
sid = conv or ("gem-carbon-1" if model == "gemini-carbon" else "gem-flash-2")

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

emit({{
    "event": "init",
    "conversation_id": sid,
    "init": {{
        "model": "google:" + reported_model,
        "permission_mode": "request-review",
        "cwd": {str(proj_dir)!r},
        "tools": ["Read", "Edit"],
        "mcp_servers": [],
    }},
}})
step = 0
turns = 0
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    frame = json.loads(raw)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"args": args, "sid": sid, "frame": frame}}) + "\\n")
    turns += 1
    step += 1
    emit({{
        "event": "step_update",
        "step_update": {{
            "conversation_id": sid,
            "step_index": step,
            "step_type": "user_input",
            "state": "DONE",
        }},
    }})
    step += 1
    reply = f"Reply from {{reported_model}} ({{sid}})"
    emit({{
        "event": "step_update",
        "step_update": {{
            "conversation_id": sid,
            "step_index": step,
            "step_type": "agent_response",
            "text_delta": reply,
        }},
    }})
    emit({{
        "event": "result",
        "result": {{
            "conversation_id": sid,
            "status": "SUCCESS",
            "num_turns": turns,
            "response": reply,
            "usage": {{"input_tokens": 50, "output_tokens": 20}},
        }},
    }})
""",
    )
    fake_bin_obj = types.SimpleNamespace(path=agy_bin)
    monkeypatch.setattr(google_env, "find_google_binary", lambda: fake_bin_obj)
    monkeypatch.setattr(gd_mod, "find_google_binary", lambda: fake_bin_obj)
    monkeypatch.setattr(google_env, "google_subscription_env", lambda **_kw: dict(os.environ))
    monkeypatch.setattr(gd_mod, "google_subscription_env", lambda **_kw: dict(os.environ))
    monkeypatch.setattr(google_env, "list_mcp_servers", lambda: [])

    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    drivers: list[GeminiCliDriver] = []
    try:
        coord = WorkCoordinator(store)
        work = store.create_work(cwd=str(proj_dir), lead_provider="google", objective="Google detour proof")
        coord.enable_continuity(work.work_id)

        # Leg 1: gemini-carbon (gem-carbon-1)
        coord.activate_model_binding(work.work_id, "google", model="gemini-carbon")
        d1 = GeminiCliDriver(cwd=str(proj_dir), model="gemini-carbon")
        drivers.append(d1)
        _wire_coordinator_hooks(d1, coord, work.work_id, "google")
        t1_done: list[Turn] = []
        d1.connect("turn-appended", lambda _d, t: t1_done.append(t))
        d1.start()
        d1.send_user_text("Analyze parser_v1.py")
        _pump_until(lambda: len(t1_done) == 1)
        coord.ensure_initial_work_leg(
            work.work_id,
            provider="google",
            model="gemini-carbon",
            native_session_id="gem-carbon-1",
        )
        p_g1 = coord.participant(work.work_id, "google")
        assert p_g1 is not None and p_g1.native_id == "gem-carbon-1"
        g1_tag = types.SimpleNamespace(model="gemini-carbon")
        tag_driver(g1_tag, p_g1)
        coord.record_user_message(g1_tag, "Analyze parser_v1.py")
        d1.stop()

        # Leg 2: GPT-6.1 Sol detour
        coord.select_lead(work.work_id, "openai")
        p_oa = coord.bind_participant(work.work_id, "openai", native_id="codex-det-1", model="gpt-6.1-sol")
        oa_tag = types.SimpleNamespace(model="gpt-6.1-sol")
        tag_driver(oa_tag, p_oa)
        coord.record_user_message(oa_tag, "Write tokenizer table.")
        oa_turn = Turn(role="assistant", model="gpt-6.1-sol")
        oa_turn.text_parts.append("Built DFA_STATE_TABLE_42 in tokenizer.py.")
        coord.record_turn(oa_tag, oa_turn)

        # Leg 3: Detour from OpenAI -> DIFFERENT Google model (gemini-flash)!
        # Store must retire gem-carbon-1 so resume_session_id == "" (NO --conversation gem-carbon-1!)
        coord.select_lead(work.work_id, "google")
        p_flash, b_flash = coord.activate_model_binding(
            work.work_id, "google", model="gemini-flash", force_fresh=False
        )
        assert b_flash is None and p_flash.native_id == ""
        d3 = GeminiCliDriver(
            cwd=str(proj_dir),
            model="gemini-flash",
            resume_session_id=p_flash.native_id,
        )
        drivers.append(d3)
        _wire_coordinator_hooks(d3, coord, work.work_id, "google")
        t3_done: list[Turn] = []
        errs3: list[str] = []
        d3.connect("turn-appended", lambda _d, t: t3_done.append(t))
        d3.connect("error", lambda _d, msg: errs3.append(msg))
        d3.start()
        d3.send_user_text("Benchmark DFA_STATE_TABLE_42.")
        _pump_until(lambda: len(t3_done) == 1)
        assert errs3 == []
        assert "gemini-flash (gem-flash-2)" in t3_done[0].text
        p_flash_bound = coord.participant(work.work_id, "google")
        assert p_flash_bound is not None
        fl_tag = types.SimpleNamespace(model="gemini-flash")
        tag_driver(fl_tag, p_flash_bound)
        coord.record_user_message(fl_tag, "Benchmark DFA_STATE_TABLE_42.")
        d3.stop()

        # Leg 4: Switch back to gemini-carbon -> resumes --conversation gem-carbon-1 with ONLY delta!
        p_ret, b_ret = coord.activate_model_binding(
            work.work_id, "google", model="gemini-carbon", force_fresh=False
        )
        assert b_ret is not None and b_ret.native_id == "gem-carbon-1"
        d4 = GeminiCliDriver(
            cwd=str(proj_dir),
            model="gemini-carbon",
            resume_session_id=p_ret.native_id,
        )
        drivers.append(d4)
        _wire_coordinator_hooks(d4, coord, work.work_id, "google")
        t4_done: list[Turn] = []
        d4.connect("turn-appended", lambda _d, t: t4_done.append(t))
        d4.start()
        d4.send_user_text("Ship final PR.")
        _pump_until(lambda: len(t4_done) == 1)
        assert "gemini-carbon (gem-carbon-1)" in t4_done[0].text
        d4.stop()

        records = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 3
        # Invocation 2 (gemini-flash) must NOT pass --conversation and must carry BOTH Leg 1 & Leg 2:
        assert "--conversation" not in records[1]["args"]
        wire_flash = json.dumps(records[1]["frame"])
        assert "Reply from gemini-carbon" in wire_flash
        assert "DFA_STATE_TABLE_42" in wire_flash
        # Invocation 3 (return to gemini-carbon) MUST pass --conversation gem-carbon-1 and carry ONLY delta:
        assert "--conversation" in records[2]["args"]
        assert records[2]["args"][records[2]["args"].index("--conversation") + 1] == "gem-carbon-1"
        wire_ret = json.dumps(records[2]["frame"])
        assert "DFA_STATE_TABLE_42" in wire_ret
        assert "gemini-flash (gem-flash-2)" in wire_ret
        assert "Analyze parser_v1.py" not in wire_ret
    finally:
        for d in drivers:
            try:
                d.stop()
            except Exception:
                pass
        store.close()


def _build_nav_window_harness(
    state_root: Path,
    coord: WorkCoordinator,
    *,
    initial_model: str = "gemini-carbon",
):
    manager = DriverManager(max_live=6, idle_seconds=60)
    transcript = TranscriptView()
    drafts = DraftBook()
    ui_dict: dict[str, object] = {"model": initial_model}
    toasts: list[str] = []

    class _Comp:
        def __init__(self) -> None:
            self._txt = ""
            self.busy = False
            self.read_only = False

        def current_text(self) -> str:
            return self._txt

        def set_text(self, val: str) -> None:
            self._txt = val

        def set_busy(self, flag: bool) -> None:
            self.busy = flag

        def set_read_only(self, flag: bool, _note: str = "") -> None:
            self.read_only = flag

        def set_visible(self, _vis: bool) -> None:
            pass

        def grab_input_focus(self) -> None:
            pass

    composer = _Comp()
    win = types.SimpleNamespace(
        _destroyed=False,
        _model=initial_model,
        _provider_models={model_catalog.provider_for(initial_model): initial_model},
        _ui_state=types.SimpleNamespace(
            get=lambda k, d=None: ui_dict.get(k, d),
            set=lambda k, v: ui_dict.__setitem__(k, v),
        ),
        _pending_session_provider_adoption="",
        _pending_model_switch=None,
        _pending_switch_qid_seq=-1,
        _pending_goal=None,
        _staged_permission_mode="",
        _staged_effort_key="",
        _staged_workflow_mode="",
        _driver=None,
        _driver_manager=manager,
        _work_coordinator=coord,
        _conversation_perms=conversation_perms.store(),
        _transcript=transcript,
        _composer=composer,
        _drafts=drafts,
        _draft_key="",
        _next_chat=None,
        _changes=None,
        _plan_progress=None,
        _shared=types.SimpleNamespace(set_handoff_target=lambda _s: None),
        _goal_strip=types.SimpleNamespace(clear_goal=lambda: None),
        _context=types.SimpleNamespace(
            clear_usage=lambda: None,
            set_usage=lambda *_a, **_kw: None,
            set_project=lambda _p: None,
        ),
        _main_stack=types.SimpleNamespace(
            _vis="welcome",
            get_visible_child_name=lambda: "transcript",
            set_visible_child_name=lambda _n: None,
        ),
        _chat_toolbar=types.SimpleNamespace(
            set_model=lambda _m: None,
            set_context_model=lambda _m: None,
            set_context_usage=lambda *_a: None,
            set_session_tokens=lambda *_a: None,
            set_visible=lambda _v: None,
            set_busy=lambda _b: None,
            set_permission_mode=lambda _m: None,
            set_effort_key=lambda _e: None,
            set_workflow_mode=lambda _w: None,
        ),
        _activity=types.SimpleNamespace(clear=lambda: None),
        _plan=types.SimpleNamespace(
            append_turn=lambda _t: None,
            set_session=lambda _s: None,
            clear=lambda: None,
        ),
        _sessions=types.SimpleNamespace(reload=lambda **_kw: None),
        _sync_provider_toggle=lambda: None,
        _sync_assistant_labels=lambda: None,
        _sync_effort_sensitivity=lambda: None,
        _sync_execution_control=lambda: None,
        _refresh_openrouter_credits=lambda: None,
        _refresh_capabilities=lambda: None,
        _refresh_goal_strip=lambda: None,
        _load_context_fill_async=lambda _s: None,
        _start_following=lambda _s: None,
        _stop_following=lambda: None,
        _pump_questions=lambda: None,
        _staged_sidebar_project=None,
        _toast=lambda msg, **_kw: toasts.append(msg),
        _show_fresh_chat_ui=lambda *_a, **_kw: pytest.fail(
            "_show_fresh_chat_ui must never fire during model switch"
        ),
    )
    win._selected_provider = lambda: model_catalog.provider_for(win._model)
    win._current_work_id = lambda: GoalWorkMixin._current_work_id(win)
    win._drv_is_current = lambda d: manager.is_current(d)
    win._driver_provider = lambda d: getattr(d, "provider", "")
    win._driver_matches_selected_provider = (
        lambda d: getattr(d, "provider", "") == win._selected_provider()
    )
    win._driver_matches_target_binding = lambda d: True
    win._driver_matches_project = staticmethod(lambda _d, _p: True)
    win._work_is_executing = lambda _wid: bool(win._driver and win._driver.is_busy)
    win._checkpoint_dispatch_pending_for = lambda _d: False
    win._take_checkpoint_dispatch = lambda _d: None
    win._stage_native_wire_recovery = lambda _d: None
    win._return_native_locally_buffered_user = lambda _d: None
    win._clear_busy_ui = lambda: MainWindow._clear_busy_ui(win)
    win._resolve_resume_id = lambda rid, proj: session_providers.resolve_provider(
        rid, proj.path / f"{rid}.jsonl", conversation_store=win._conversation_perms
    )
    win._target_resume_resolution = lambda target: (
        win._resolve_resume_id(target.resume_id, target.project)
        if target.resume_id
        else session_providers.ProviderResolution()
    )

    win._try_live_model_change = lambda alias, label: MainWindow._try_live_model_change(
        win, alias, label
    )
    win._stage_fresh_chat_for_provider_switch = (
        lambda: MainWindow._stage_fresh_chat_for_provider_switch(win)
    )
    win._drain_queue_to_composer = lambda d, leading_texts=(): MainWindow._drain_queue_to_composer(
        win, d, leading_texts=leading_texts
    )
    win._make_queued_remover = lambda d: (
        lambda qid: d.remove_queued_user(qid) and transcript.remove_queued(qid)
    )

    def _apply_model(model_id: str, *, quiet: bool = False) -> None:
        MainWindow._apply_model_choice(win, model_id, quiet=quiet)

    def _adopt_prov(prov: str) -> None:
        fallback = {"google": "gemini-carbon", "openai": "gpt-6.1-sol", "anthropic": "fable[1m]"}
        win._model = win._provider_models.get(prov) or fallback.get(prov, win._model)

    def _bind_visible(d) -> None:
        manager.bind_current(d)
        win._driver = d

    win._apply_model_choice = _apply_model
    win._adopt_session_provider = _adopt_prov
    win._bind_visible_driver = _bind_visible
    return win, transcript, composer, toasts


def test_staged_zero_turn_switch_survives_sidebar_navigation_and_window_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_home = tmp_path / "claude"
    state_root = tmp_path / "state"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("HELIOS_STATE_DIR", str(state_root))
    proj_dir = claude_home / "projects" / "-workspace-demo"
    proj_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects, "PROJECTS_DIR", claude_home / "projects")

    sid_a = "gem-sess-a"
    sid_b = "claude-sess-b"
    many_records = []
    for i in range(6):
        many_records.append(
            {
                "type": "user",
                "sessionId": sid_a,
                "version": "helios-gemini",
                "cwd": "/workspace/demo",
                "timestamp": f"2026-11-15T10:0{i}:00Z",
                "message": {"role": "user", "content": f"Step {i + 1}: Design zero-copy ring buffer"},
            }
        )
        many_records.append(
            {
                "type": "assistant",
                "sessionId": sid_a,
                "version": "helios-gemini",
                "cwd": "/workspace/demo",
                "timestamp": f"2026-11-15T10:0{i}:05Z",
                "message": {
                    "role": "assistant",
                    "model": "gemini-carbon",
                    "content": f"Step {i + 1} reply: Use power-of-two mask.",
                },
            }
        )
    (proj_dir / f"{sid_a}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in many_records) + "\n",
        encoding="utf-8",
    )
    (proj_dir / f"{sid_b}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": sid_b,
                "version": "2.1.0",
                "cwd": "/workspace/demo",
                "timestamp": "2026-11-15T09:00:00Z",
                "message": {"role": "user", "content": "Unrelated sidebar session"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    session_providers.set_provider(sid_a, "google")
    session_providers.set_provider(sid_b, "anthropic")

    store = WorkStore(state_root / "work" / "work.db")
    monkeypatch.setattr(work_store, "default_work_db_path", lambda: state_root / "work" / "work.db")
    try:
        coord = WorkCoordinator(store)
        work_a = coord.ensure_work(
            cwd="/workspace/demo", lead_provider="google", objective="Ring buffer"
        )
        p_a = coord.bind_participant(
            work_a.work_id, "google", native_id=sid_a, model="gemini-carbon"
        )
        drv_a = types.SimpleNamespace(model="gemini-carbon")
        tag_driver(drv_a, p_a)
        coord.record_user_message(drv_a, "Design zero-copy ring buffer")
        t_a = Turn(role="assistant", session_id=sid_a, provider="google", model="gemini-carbon")
        t_a.text_parts.append("Use power-of-two mask with acquire/release cursors.")
        coord.record_turn(drv_a, t_a)

        work_b = coord.ensure_work(
            cwd="/workspace/demo", lead_provider="anthropic", objective="Other"
        )
        coord.bind_participant(work_b.work_id, "anthropic", native_id=sid_b, model="fable[1m]")

        disc_projects = projects.discover_projects()
        assert len(disc_projects) == 1
        sessions_by_id = {s.session_id: s for s in disc_projects[0].load_sessions()}
        sess_a = sessions_by_id[sid_a]
        sess_b = sessions_by_id[sid_b]

        win1, tv1, comp1, _toasts1 = _build_nav_window_harness(state_root, coord)
        MainWindow._on_session_selected(win1, None, sess_a)
        _pump_gtk()
        comp1.set_text("unsent draft across switch")

        # Stage 0-turn model switch Gemini Carbon -> GPT-6.1 Sol (do NOT send yet!)
        MainWindow._on_toolbar_model_changed(win1, None, "gpt-6.1-sol")
        _pump_gtk()
        assert win1._model == "gpt-6.1-sol"
        assert win1._next_chat.work_id == work_a.work_id
        assert win1._next_chat.resume_id == ""
        dividers_initial = [
            w for w in _list_children(tv1._list) if isinstance(w, HandoffDividerRow)
        ]
        assert len(dividers_initial) == 1

        # Navigate away to Session B and back to Session A in sidebar:
        MainWindow._on_session_selected(win1, None, sess_b)
        _pump_gtk()
        assert win1._model != "gpt-6.1-sol"

        MainWindow._on_session_selected(win1, None, sess_a)
        _pump_gtk()
        assert win1._model == "gpt-6.1-sol"
        assert win1._next_chat.work_id == work_a.work_id
        assert win1._next_chat.resume_id == ""
        assert comp1.current_text() == "unsent draft across switch"
        dividers_after_nav = [
            w for w in _list_children(tv1._list) if isinstance(w, HandoffDividerRow)
        ]
        assert len(dividers_after_nav) == 1
        # Even with 12 turns (> BATCH=8), divider MUST be strictly at the tail!
        assert tv1._list.get_last_child() is dividers_after_nav[0]
        labels_nav = " ".join(_walk_labels(dividers_after_nav[0]))
        assert "Switched from Gemini Carbon to GPT-6.1 Sol" in labels_nav

        # Simulate FULL WINDOW RESTART against same state_root + WorkStore:
        slist = SessionList()
        restarted_projs = projects.discover_projects()
        coalesced, _meta = slist._coalesce_continuity_sessions(
            restarted_projs[0].load_sessions()
        )
        assert len(coalesced) == 2  # Session A unified + Session B!

        win2, tv2, _comp2, _toasts2 = _build_nav_window_harness(
            state_root, coord, initial_model="fable[1m]"
        )
        MainWindow._on_session_selected(win2, None, sess_a)
        _pump_gtk()
        assert win2._model == "gpt-6.1-sol"
        assert win2._next_chat.work_id == work_a.work_id
        assert win2._next_chat.resume_id == ""
        dividers_restart = [
            w for w in _list_children(tv2._list) if isinstance(w, HandoffDividerRow)
        ]
        assert len(dividers_restart) == 1
        assert tv2._list.get_last_child() is dividers_restart[0]
        labels_restart = " ".join(_walk_labels(dividers_restart[0]))
        assert "Switched from Gemini Carbon to GPT-6.1 Sol" in labels_restart
    finally:
        store.close()


def test_rebound_idle_driver_refusal_fallback_and_mid_stream_multi_switch_coalescing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    proj_dir = tmp_path / "claude" / "projects" / "-workspace-demo"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "claude-1.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "claude-1",
                "version": "2.1.0",
                "cwd": "/workspace/demo",
                "message": {"role": "user", "content": "Hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    session_providers.set_provider("claude-1", "anthropic")
    project = Project(dirname="-workspace-demo", cwd="/workspace/demo", path=proj_dir)

    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    try:
        coord = WorkCoordinator(store)
        work = coord.ensure_work(
            cwd="/workspace/demo", lead_provider="anthropic", objective="Refusal + coalescing"
        )
        p_cl = coord.bind_participant(
            work.work_id, "anthropic", native_id="claude-1", model="sonnet[1m]"
        )

        class _RefusingIdleClaudeDriver:
            def __init__(self) -> None:
                self.provider = "anthropic"
                self.model = "sonnet[1m]"
                self._helios_requested_model = "sonnet[1m]"
                self.session_id = "claude-1"
                self.cwd = "/workspace/demo"
                self._cwd = "/workspace/demo"
                self.is_running = True
                self.is_accepting_input = True
                self.is_busy = False
                self.is_initializing = False
                self.ended_input = False

            def set_model(self, _new_model: str, callback=None) -> bool:
                if callback is not None:
                    callback(False, "model locked by policy")
                return True

            def end_input(self) -> None:
                self.ended_input = True
                self.is_running = False
                self.is_accepting_input = False

        idle_claude = _RefusingIdleClaudeDriver()
        tag_driver(idle_claude, p_cl)
        coord.record_user_message(idle_claude, "Initial turn")
        t1 = Turn(role="assistant", session_id="claude-1", provider="anthropic", model="sonnet[1m]")
        t1.text_parts.append("Initial Sonnet response.")
        t1.tool_uses.append(ToolUse(id="u1", name="Read", input={"file_path": "app.py"}))
        coord.record_turn(idle_claude, t1)

        win, tv, _comp, _toasts = _build_nav_window_harness(
            tmp_path / "state", coord, initial_model="sonnet[1m]"
        )
        win._driver_manager.handlers = {idle_claude: [1]}
        win._driver_manager.live = {"claude-1": idle_claude}
        win._bind_visible_driver(idle_claude)
        win._next_chat = ChatTarget(project, "claude-1", work.work_id, "anthropic")

        # Part A: Detour Sonnet -> GPT-6.1 Sol -> Opus where idle_claude refuses set_model("opus[1m]"):
        MainWindow._on_toolbar_model_changed(win, None, "gpt-6.1-sol")
        _pump_gtk()
        assert win._driver is None
        assert idle_claude.ended_input is False  # kept warm in DriverManager pool

        MainWindow._on_toolbar_model_changed(win, None, "opus[1m]")
        _pump_gtk()
        # Because set_model refused, rebound_driver MUST be retired & unbound so next send cold-resumes!
        assert idle_claude.ended_input is True
        assert win._driver is None
        assert win._model == "opus[1m]"

        # Part B: Mid-stream rapid multi-switch coalescing + queued follow-ups:
        class _BusyStreamingDriver:
            def __init__(self) -> None:
                self.provider = "openai"
                self.model = "gpt-6.1-sol"
                self._helios_requested_model = "gpt-6.1-sol"
                self.session_id = "codex-busy-1"
                self.cwd = "/workspace/demo"
                self._cwd = "/workspace/demo"
                self.is_running = True
                self.is_accepting_input = True
                self.is_busy = True
                self.is_initializing = False
                self.ended_input = False

            def queued_messages(self):
                return []

            def take_queued(self):
                return []

            def end_input(self) -> None:
                self.ended_input = True
                self.is_running = False

        p_oa = coord.bind_participant(
            work.work_id, "openai", native_id="codex-busy-1", model="gpt-6.1-sol"
        )
        busy_oa = _BusyStreamingDriver()
        tag_driver(busy_oa, p_oa)
        win._apply_model_choice("gpt-6.1-sol", quiet=True)
        win._driver_manager.handlers[busy_oa] = [2]
        win._driver_manager.live["codex-busy-1"] = busy_oa
        win._bind_visible_driver(busy_oa)
        win._next_chat = ChatTarget(project, "codex-busy-1", work.work_id, "openai")

        dispatched_after_switch: list[tuple[str, str]] = []

        class _SpawnedFableDriver:
            def __init__(self) -> None:
                self.provider = "anthropic"
                self.model = "fable[1m]"
                self._helios_requested_model = "fable[1m]"
                self.session_id = "claude-fable-2"
                self.cwd = "/workspace/demo"
                self._cwd = "/workspace/demo"
                self.is_running = True
                self.is_accepting_input = True
                self.is_busy = True
                self.is_initializing = False
                self._q: list[tuple[int, str]] = []

            def queue_user_text(self, txt: str) -> int:
                qid = len(self._q) + 1
                self._q.append((qid, txt))
                return qid

            def remove_queued_user(self, qid: int) -> bool:
                before = len(self._q)
                self._q = [it for it in self._q if it[0] != qid]
                return len(self._q) != before

            def queued_messages(self) -> list[tuple[int, str]]:
                return list(self._q)

        def _spawn_fable_on_send(_c, txt: str) -> None:
            dispatched_after_switch.append((win._model, txt))
            fable_drv = _SpawnedFableDriver()
            win._driver_manager.handlers[fable_drv] = [3]
            win._driver_manager.live["claude-fable-2"] = fable_drv
            win._bind_visible_driver(fable_drv)

        win._on_composer_send = _spawn_fable_on_send

        # Rapidly switch 3 times mid-stream and queue 2 messages:
        MainWindow._on_toolbar_model_changed(win, None, "gemini-carbon")
        MainWindow._on_toolbar_model_changed(win, None, "kimi-code/k3")
        MainWindow._on_toolbar_model_changed(win, None, "fable[1m]")
        assert MainWindow._queue_on_pending_model_switch(win, "First post-switch prompt")
        assert MainWindow._queue_on_pending_model_switch(win, "Second post-switch prompt")
        _pump_gtk()

        assert win._pending_model_switch is not None
        assert win._pending_model_switch.to_model == "fable[1m]"
        assert [t for _q, t in win._pending_model_switch.queued_items] == [
            "First post-switch prompt",
            "Second post-switch prompt",
        ]

        # Complete streaming turn and fire pending switch:
        coord.record_user_message(busy_oa, "Streamed prompt")
        t_stream = Turn(role="assistant", session_id="codex-busy-1", provider="openai", model="gpt-6.1-sol")
        t_stream.text_parts.append("Completed streamed GPT analysis.")
        coord.record_turn(busy_oa, t_stream)
        busy_oa.is_busy = False

        assert MainWindow._complete_pending_model_switch_if_ready(
            win, busy_oa, allow_auto_send=True
        )
        _pump_gtk()
        assert win._pending_model_switch is None
        assert win._model == "fable[1m]"
        assert dispatched_after_switch == [("fable[1m]", "First post-switch prompt")]
        assert [t for _q, t in win._driver.queued_messages()] == ["Second post-switch prompt"]
    finally:
        store.close()


def test_startup_model_refusal_retry_and_pending_model_ack_barrier_on_kimi_and_muse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch
    from helios.backend.process import cli_driver as cl_mod

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))

    # Verify qualified <provider>:<id> normalization across drivers:
    with patch.object(kd_mod, "effective_execution_mode", return_value="default"):
        kd_qual = KimiAcpDriver(cwd=str(tmp_path), model="kimi:kimi-code/k3")
    assert kd_qual.model == "kimi-code/k3"
    with patch.object(md_mod, "effective_execution_mode", return_value="default"):
        md_qual = MuseServeDriver(cwd=str(tmp_path), model="muse:muse-spark-1.3")
    assert md_qual.model == "muse-spark-1.3"
    cl_qual = cl_mod.ClaudeCliDriver(cwd=str(tmp_path), model="anthropic:fable[1m]")
    assert cl_qual.model == "fable[1m]"

    class _CapStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.closed = False

        def write(self, data: bytes) -> int:
            if self.closed:
                raise OSError("closed")
            self.writes.append(data)
            return len(data)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        def getvalue(self) -> bytes:
            return b"".join(self.writes)

    def _new_synthetic_driver(mod, cls, desired: str):
        with patch.object(mod, "effective_execution_mode", return_value="default"):
            drv = cls(cwd=str(tmp_path), model=desired, resume_session_id="native-1")
        drv._helios_requested_model = desired
        drv._proc = types.SimpleNamespace(stdin=_CapStdin(), poll=lambda: None)
        drv._transcript = types.SimpleNamespace(bind_thread=lambda _sid: None)
        drv.set_execution_attempt_controller(
            lambda _cand: ("synthetic-attempt", ""),
            lambda *_a: None,
            record_dispatch=lambda *_a: True,
            finish_with_evidence=lambda *_a: True,
        )
        return drv

    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    try:
        coord = WorkCoordinator(store)
        win, _tv, _comp, _toasts = _build_nav_window_harness(
            tmp_path / "state", coord, initial_model="kimi-code/k3.1-pro"
        )

        for mod, cls, old_m, desired_m in (
            (kd_mod, KimiAcpDriver, "kimi-code/k3", "kimi-code/k3.1-pro"),
            (md_mod, MuseServeDriver, "muse-wonder-1.0", "muse-spark-1.3"),
        ):
            # 1) Startup model refusal MUST fence/retire driver and block retry on old model:
            drv = _new_synthetic_driver(mod, cls, desired_m)
            drv.send_user_text("first prompt")
            bound_payload = (
                {
                    "sessionId": "native-1",
                    "configOptions": [{"id": "model", "category": "model", "currentValue": old_m}],
                }
                if mod is kd_mod
                else {
                    "sessionId": "native-1",
                    "modelId": old_m,
                    "mcpServers": [{"name": "synthetic"}],
                }
            )
            drv._on_session_bound(bound_payload)
            changes = drv._setting_changes if mod is kd_mod else drv._model_changes
            rpc_id = next(iter(changes))
            rejection = (
                {"jsonrpc": "2.0", "id": rpc_id, "error": {"message": "model unavailable"}}
                if mod is kd_mod
                else {"jsonrpc": "2.0", "id": rpc_id, "result": {"status": "rejected"}}
            )
            drv._dispatch_message(rejection)

            win._model = desired_m
            assert MainWindow._driver_model_is_stale(win, drv) is True
            assert drv.is_accepting_input is False
            retry = drv.send_user_text("retry same selected model")
            assert retry.accepted is False
            frames = [json.loads(x) for x in drv._proc.stdin.getvalue().splitlines()]
            assert all(f.get("method") not in {"session/prompt", "turn/start"} for f in frames)

            # 2) Live set_model RPC in flight MUST fence prompt dispatch until ACK:
            live = _new_synthetic_driver(mod, cls, old_m)
            live._session_id = "native-1"
            live._accepting_input = True
            assert live.set_model(desired_m, lambda *_a: None) is True
            assert live.model_change_pending is True
            assert MainWindow._execution_change_pending_for(win, live) is True
            delivery = live.send_user_text("prompt while model ACK is pending")
            assert delivery.accepted is False
            live_frames = [json.loads(x) for x in live._proc.stdin.getvalue().splitlines()]
            assert all(f.get("method") not in {"session/prompt", "turn/start"} for f in live_frames)
    finally:
        store.close()


def test_mid_batch_second_model_switch_coalesces_at_tail_without_interleaving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_home = tmp_path / "claude"
    state_root = tmp_path / "state"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("HELIOS_STATE_DIR", str(state_root))
    proj_dir = claude_home / "projects" / "-workspace-batch"
    proj_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects, "PROJECTS_DIR", claude_home / "projects")

    sid = "gem-batch-14"
    recs = []
    for i in range(7):
        recs.append(
            {
                "type": "user",
                "sessionId": sid,
                "version": "helios-gemini",
                "cwd": "/workspace/batch",
                "timestamp": f"2026-11-15T11:0{i}:00Z",
                "message": {"role": "user", "content": f"Turn {i + 1} question"},
            }
        )
        recs.append(
            {
                "type": "assistant",
                "sessionId": sid,
                "version": "helios-gemini",
                "cwd": "/workspace/batch",
                "timestamp": f"2026-11-15T11:0{i}:04Z",
                "message": {
                    "role": "assistant",
                    "model": "gemini-carbon",
                    "content": f"Turn {i + 1} answer",
                },
            }
        )
    (proj_dir / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n",
        encoding="utf-8",
    )
    session_providers.set_provider(sid, "google")

    store = WorkStore(state_root / "work" / "work.db")
    monkeypatch.setattr(work_store, "default_work_db_path", lambda: state_root / "work" / "work.db")
    try:
        coord = WorkCoordinator(store)
        work = coord.ensure_work(cwd="/workspace/batch", lead_provider="google", objective="Batch order")
        p_g = coord.bind_participant(work.work_id, "google", native_id=sid, model="gemini-carbon")
        drv_g = types.SimpleNamespace(model="gemini-carbon")
        tag_driver(drv_g, p_g)
        coord.record_user_message(drv_g, "Turn 1 question")
        t_g = Turn(role="assistant", session_id=sid, provider="google", model="gemini-carbon")
        t_g.text_parts.append("Turn 1 answer")
        coord.record_turn(drv_g, t_g)

        # Stage first 0-turn switch to gpt-6.1-sol:
        coord.enable_continuity(work.work_id)
        coord.ensure_initial_work_leg(
            work.work_id,
            provider="google",
            model="gemini-carbon",
            native_session_id=sid,
            transcript_path=str(proj_dir / f"{sid}.jsonl"),
        )
        coord.select_lead(work.work_id, "openai")
        coord.activate_model_binding(work.work_id, "openai", model="gpt-6.1-sol")

        disc = projects.discover_projects()
        sess = disc[0].load_sessions()[0]
        win, tv, _comp, _toasts = _build_nav_window_harness(state_root, coord)

        # Select session WITHOUT pumping GLib yet -> Batch 1 (8 of 15 items) rendered, 7 pending!
        MainWindow._on_session_selected(win, None, sess)
        assert tv._bulk_loading is True

        # Switch AGAIN mid-bulk-load BEFORE Batch 2 runs!
        MainWindow._on_toolbar_model_changed(win, None, "fable[1m]")
        # Now drain remaining idle batches:
        _pump_gtk(40)
        assert tv._bulk_loading is False

        children = _list_children(tv._list)
        dividers = [w for w in children if isinstance(w, HandoffDividerRow)]
        assert len(dividers) == 1
        assert children[-1] is dividers[0], "Divider MUST remain strictly after all 14 turns!"
        div_text = " ".join(_walk_labels(dividers[0]))
        assert "Switched from Gemini Carbon to Claude Fable 1M" in div_text
    finally:
        store.close()


def test_claude_resolved_model_id_is_not_treated_as_stale_against_alias_and_300_turn_prefix_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from helios.backend.composite_transcript import CompositeTranscript, HandoffBoundary

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    proj_dir = tmp_path / "claude" / "projects" / "-workspace-long"
    proj_dir.mkdir(parents=True, exist_ok=True)
    sid = "claude-long-1"
    (proj_dir / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": sid,
                "version": "2.1.0",
                "cwd": "/workspace/long",
                "message": {"role": "user", "content": "Hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    session_providers.set_provider(sid, "anthropic")

    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    try:
        coord = WorkCoordinator(store)
        win, tv, _comp, _toasts = _build_nav_window_harness(
            tmp_path / "state", coord, initial_model="sonnet[1m]"
        )
        # 1) Claude alias sonnet[1m] whose runtime resolved model is claude-sonnet-5 MUST NOT be stale:
        claude_live = types.SimpleNamespace(
            provider="anthropic",
            model="claude-sonnet-5",
            _helios_requested_model="sonnet[1m",
            is_busy=False,
        )
        claude_live._helios_requested_model = "sonnet[1m]"
        assert MainWindow._driver_model_is_stale(win, claude_live) is False

        # 2) 300-turn composite transcript keeps its _history_start_index prefix intact on mid-batch switch:
        disc = projects.Project(dirname="-workspace-long", cwd="/workspace/long", path=proj_dir)
        sess = disc.load_sessions()[0]
        turns_300: list[Turn] = []
        for idx in range(150):
            u = Turn(role="user", session_id=sid, provider="anthropic")
            u.text_parts.append(f"U-{idx}")
            a = Turn(role="assistant", session_id=sid, provider="anthropic", model="sonnet[1m]")
            a.text_parts.append(f"A-{idx}")
            turns_300.extend([u, a])
        b1 = HandoffBoundary(
            leg_seq=2,
            from_provider="anthropic",
            from_model="sonnet[1m]",
            to_provider="openai",
            to_model="gpt-6.1-sol",
            capsule_manifest={},
        )
        comp_301 = CompositeTranscript(
            items=(*turns_300, b1),
            turns=tuple(turns_300),
            tail_leg=None,
            tail_path=None,
            tail_offset=0,
        )
        tv.set_composite_session(sess, comp_301)
        start_idx = tv._history_start_index
        assert start_idx > 0
        assert tv._bulk_loading is True

        # Coalesce mid-batch BEFORE draining GLib idle:
        b2 = HandoffBoundary(
            leg_seq=2,
            from_provider="openai",
            from_model="gpt-6.1-sol",
            to_provider="google",
            to_model="gemini-carbon",
            capsule_manifest={},
        )
        tv.replace_or_append_handoff_divider(b2)
        assert len(tv._composite_items) == 301
        assert tv._composite_items[:start_idx] == tuple(turns_300[:start_idx])
        assert isinstance(tv._composite_items[-1], HandoffBoundary)
        assert tv._composite_items[-1].to_model == "gemini-carbon"
        _pump_gtk(60)
        assert tv._bulk_loading is False
        assert isinstance(tv._list.get_last_child(), HandoffDividerRow)
    finally:
        store.close()
