"""Headless GTK4 integration tests for seamless mid-session model switching,
HandoffDividerRow disclosure, leg-scoped tool pairing, _PendingModelSwitch,
OpenRouter intra-provider/multimodal carryover, and unified SessionList."""

from __future__ import annotations

import json
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
    model_catalog,
    projects,
    session_providers,
    work_store,
)
from helios.backend.composer_state import DraftBook, draft_key  # noqa: E402
from helios.backend.composite_transcript import HandoffBoundary  # noqa: E402
from helios.backend.openrouter import chat as or_chat  # noqa: E402
from helios.backend.process.driver_manager import DriverManager  # noqa: E402
from helios.backend.process.message_queue import RequiredPromptContextError  # noqa: E402
from helios.backend.process.openrouter_driver import OpenRouterDriver  # noqa: E402
from helios.backend.projects import Project  # noqa: E402
from helios.backend.session_router import ChatTarget  # noqa: E402
from helios.backend.transcript import ToolResult, ToolUse, Turn  # noqa: E402
from helios.backend.work_coordinator import WorkCoordinator, tag_driver  # noqa: E402
from helios.backend.work_store import WorkStore  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402
from helios.widgets.message_bubble import MessageBubble  # noqa: E402
from helios.widgets.session_list import SessionList  # noqa: E402
from helios.widgets.transcript_view import HandoffDividerRow, TranscriptView  # noqa: E402
from helios.window_goal import GoalWorkMixin  # noqa: E402


def _pump_gtk(iterations: int = 25) -> None:
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


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


class _FakeDriver:
    def __init__(
        self,
        provider: str,
        model: str,
        session_id: str,
        *,
        cwd: str = "/repo",
    ) -> None:
        self.provider = provider
        self.model = model
        self._helios_requested_model = model
        self.session_id = session_id
        self.cwd = cwd
        self.is_running = True
        self.is_accepting_input = True
        self.is_busy = False
        self.is_initializing = False
        self.ended_input = False
        self.stopped = False
        self._queue: list[tuple[int, str]] = []
        self._next_qid = 1
        self._pending_queue_delivery = None
        self._uncertain_queue_delivery = None

    def end_input(self) -> None:
        self.ended_input = True
        self.is_accepting_input = False
        self.is_running = False

    def stop(self) -> None:
        self.stopped = True
        self.is_busy = False

    def queue_user_text(self, text: str) -> int:
        qid = self._next_qid
        self._next_qid += 1
        self._queue.append((qid, text))
        return qid

    def remove_queued_user(self, qid: int) -> bool:
        before = len(self._queue)
        self._queue = [item for item in self._queue if item[0] != qid]
        return len(self._queue) != before

    def queued_messages(self) -> list[tuple[int, str]]:
        return list(self._queue)

    def take_queued(self) -> list[str]:
        texts = [t for _q, t in self._queue]
        self._queue.clear()
        return texts

    def take_queued_successors_after_pending_delivery(self) -> list[str] | None:
        if (
            self._pending_queue_delivery is None
            or not self._queue
            or self._queue[0] != self._pending_queue_delivery
        ):
            return None
        tail = [t for _q, t in self._queue[1:]]
        self._queue = [self._queue[0]]
        return tail


class _FakeComposer:
    def __init__(self, initial: str = "") -> None:
        self.text = initial
        self.busy = False

    def current_text(self) -> str:
        return self.text

    def set_text(self, value: str) -> None:
        self.text = value

    def set_busy(self, flag: bool) -> None:
        self.busy = flag

    def grab_input_focus(self) -> None:
        pass


def _make_switch_window_harness(
    tmp_path: Path,
    *,
    initial_model: str = "gemini-4-carbon",
    initial_provider: str = "google",
    initial_sid: str = "gem-111",
):
    proj_dir = tmp_path / "projects" / "-repo"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{initial_sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": initial_sid,
                "version": "helios-gemini",
                "cwd": "/repo",
                "message": {"role": "user", "content": "Initial question"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    session_providers.set_provider(initial_sid, initial_provider)

    project = Project(dirname="-repo", cwd="/repo", path=proj_dir)
    store = WorkStore(tmp_path / "work" / "work.db")
    coord = WorkCoordinator(store)
    work = coord.ensure_work(cwd="/repo", lead_provider=initial_provider, objective="Switch seamlessly")
    part = coord.bind_participant(
        work.work_id,
        initial_provider,
        native_id=initial_sid,
        model=initial_model,
    )

    drv = _FakeDriver(initial_provider, initial_model, initial_sid)
    tag_driver(drv, part)
    coord.record_user_message(drv, "Initial question")
    t_asst = Turn(role="assistant", session_id=initial_sid, provider=initial_provider, model=initial_model)
    t_asst.text_parts.append("Initial Gemini Carbon analysis.")
    t_asst.tool_uses.append(ToolUse(id="u1", name="Edit", input={"file_path": "src/service.py"}))
    coord.record_turn(drv, t_asst)

    manager = DriverManager(max_live=6, idle_seconds=60)
    manager.handlers = {drv: [1]}
    manager.live = {initial_sid: drv}
    manager.bind_current(drv)

    transcript = TranscriptView()
    u_turn = Turn(role="user", session_id=initial_sid, provider=initial_provider)
    u_turn.text_parts.append("Initial question")
    transcript.append_turn(u_turn)
    transcript.append_turn(t_asst)
    _pump_gtk()

    composer = _FakeComposer("draft in progress")
    drafts = DraftBook()
    toasts: list[str] = []
    ui_dict: dict[str, object] = {"model": initial_model}
    sent_on_new_model: list[tuple[str, str]] = []

    win = types.SimpleNamespace(
        _destroyed=False,
        _model=initial_model,
        _provider_models={initial_provider: initial_model},
        _ui_state=types.SimpleNamespace(
            get=lambda k, d=None: ui_dict.get(k, d),
            set=lambda k, v: ui_dict.__setitem__(k, v),
        ),
        _pending_session_provider_adoption="",
        _pending_model_switch=None,
        _pending_switch_qid_seq=-1,
        _pending_goal=None,
        _driver=drv,
        _driver_manager=manager,
        _work_coordinator=coord,
        _conversation_perms=conversation_perms.store(),
        _transcript=transcript,
        _composer=composer,
        _drafts=drafts,
        _draft_key=draft_key(
            ChatTarget(project, initial_sid, work.work_id),
            work_id=work.work_id,
        ),
        _next_chat=ChatTarget(project, initial_sid, work.work_id, initial_provider),
        _main_stack=types.SimpleNamespace(get_visible_child_name=lambda: "transcript"),
        _chat_toolbar=types.SimpleNamespace(
            set_model=lambda _m: None,
            set_context_model=lambda _m: None,
            set_busy=lambda _b: None,
        ),
        _activity=types.SimpleNamespace(clear=lambda: None),
        _plan=types.SimpleNamespace(append_turn=lambda _t: None, set_session=lambda _s: None),
        _sessions=types.SimpleNamespace(reload=lambda **_kw: None),
        _sync_provider_toggle=lambda: None,
        _sync_assistant_labels=lambda: None,
        _sync_effort_sensitivity=lambda: None,
        _sync_execution_control=lambda: None,
        _refresh_openrouter_credits=lambda: None,
        _refresh_capabilities=lambda: None,
        _stop_following=lambda: None,
        _toast=lambda msg, **_kw: toasts.append(msg),
        _show_fresh_chat_ui=lambda *_a, **_kw: pytest.fail(
            "_show_fresh_chat_ui must never fire during in-place model switch"
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
    win._drain_queue_to_composer = lambda d, leading_texts=(): MainWindow._drain_queue_to_composer(
        win, d, leading_texts=leading_texts
    )
    win._make_queued_remover = lambda d: (
        lambda qid: d.remove_queued_user(qid) and transcript.remove_queued(qid)
    )
    win._resolve_resume_id = lambda rid, proj: session_providers.resolve_provider(
        rid, proj.path / f"{rid}.jsonl", conversation_store=win._conversation_perms
    )
    win._target_resume_resolution = lambda target: (
        win._resolve_resume_id(target.resume_id, target.project)
        if target.resume_id
        else session_providers.ProviderResolution()
    )

    def _bind_visible(d):
        manager.bind_current(d)
        win._driver = d

    win._bind_visible_driver = _bind_visible

    def _fake_composer_send(_comp, text: str) -> None:
        if MainWindow._queue_on_pending_model_switch(win, text):
            return
        cur = win._driver
        if cur is not None and cur.is_busy:
            qid = cur.queue_user_text(text)
            transcript.append_queued(qid, text, win._make_queued_remover(cur))
            return
        prov = win._selected_provider()
        new_sid = f"{prov}-spawned"
        (proj_dir / f"{new_sid}.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": new_sid,
                    "version": f"helios-{prov}" if prov != "openai" else "helios-codex",
                    "cwd": "/repo",
                    "message": {"role": "user", "content": text},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        session_providers.set_provider(new_sid, prov)
        spawned = _FakeDriver(prov, win._model, new_sid)
        spawned.is_busy = True
        p_cur = coord.bind_participant(work.work_id, prov, native_id=new_sid, model=win._model)
        tag_driver(spawned, p_cur)
        coord.bind_work_leg_session(
            work.work_id,
            provider=prov,
            model=win._model,
            native_session_id=new_sid,
            transcript_path=str(proj_dir / f"{new_sid}.jsonl"),
        )
        manager.handlers[spawned] = [1]
        manager.live[new_sid] = spawned
        _bind_visible(spawned)
        sent_on_new_model.append((win._model, text))

    win._on_composer_send = _fake_composer_send
    return win, store, coord, transcript, composer, toasts, sent_on_new_model


def test_in_place_model_switch_preserves_mounted_bubbles_renders_divider_and_coalesces_unused_switches(
    tmp_path,
):
    win, store, coord, transcript, composer, _toasts, _sent = _make_switch_window_harness(
        tmp_path
    )
    try:
        initial_children = _list_children(transcript._list)
        assert len(initial_children) == 2
        assert all(isinstance(w, MessageBubble) for w in initial_children)
        b0_id, b1_id = id(initial_children[0]), id(initial_children[1])

        # Switch 1: Gemini Carbon -> GPT 6.1 Sol in place.
        MainWindow._apply_model_choice(win, "gpt-6.1-sol")
        _pump_gtk()

        after_s1 = _list_children(transcript._list)
        assert len(after_s1) == 3
        assert id(after_s1[0]) == b0_id
        assert id(after_s1[1]) == b1_id
        divider = after_s1[2]
        assert isinstance(divider, HandoffDividerRow)
        assert "Switched from" in divider.boundary.summary_text
        assert "GPT" in divider.boundary.summary_text
        assert divider.is_expanded is False
        assert composer.current_text() == "draft in progress"

        # Progressive disclosure toggle expands & reveals manifest details.
        divider._toggle_btn.emit("clicked")
        _pump_gtk()
        assert divider.is_expanded is True
        assert "src/service.py" in divider._details_label.get_label()

        # Unused consecutive switch -> Claude Fable coalesces trailing divider cleanly.
        MainWindow._apply_model_choice(win, "claude-fable-4-7")
        _pump_gtk()
        after_coalesce = _list_children(transcript._list)
        assert len(after_coalesce) == 3
        assert isinstance(after_coalesce[2], HandoffDividerRow)
        assert "Gemini" in after_coalesce[2].boundary.summary_text
        assert "Claude" in after_coalesce[2].boundary.summary_text

        # Switching right back to the original Gemini Carbon before any turn removes the no-op divider!
        MainWindow._apply_model_choice(win, "gemini-4-carbon")
        _pump_gtk()
        assert len(_list_children(transcript._list)) == 2

        # Switch again to GPT 6.1 Sol, send Turn 2, verify per-turn model chip, then switch back to Gemini Carbon!
        MainWindow._apply_model_choice(win, "gpt-6.1-sol")
        _pump_gtk()
        gpt_sid = "gpt-222"
        (win._next_chat.project.path / f"{gpt_sid}.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": gpt_sid,
                    "version": "helios-codex",
                    "cwd": "/repo",
                    "message": {"role": "user", "content": "Follow-up on GPT"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        session_providers.set_provider(gpt_sid, "openai")
        o_part = coord.bind_participant(
            win._next_chat.work_id,
            "openai",
            native_id=gpt_sid,
            model="gpt-6.1-sol",
        )
        coord.bind_work_leg_session(
            win._next_chat.work_id,
            provider="openai",
            model="gpt-6.1-sol",
            native_session_id=gpt_sid,
            transcript_path=str(win._next_chat.project.path / f"{gpt_sid}.jsonl"),
        )
        o_drv = _FakeDriver("openai", "gpt-6.1-sol", gpt_sid)
        tag_driver(o_drv, o_part)
        win._bind_visible_driver(o_drv)

        u2 = Turn(role="user", session_id=gpt_sid, provider="openai")
        u2.text_parts.append("Follow-up on GPT")
        coord.record_user_message(o_drv, "Follow-up on GPT")
        transcript.append_turn(u2)
        a2 = Turn(role="assistant", session_id=gpt_sid, provider="openai", model="gpt-6.1-sol")
        a2.text_parts.append("GPT Sol completed the patch.")
        coord.record_turn(o_drv, a2)
        transcript.append_turn(a2)
        _pump_gtk()

        # Assistant bubble carries per-turn model chip label.
        children_after_t2 = _list_children(transcript._list)
        asst_bubble = children_after_t2[-1]
        assert isinstance(asst_bubble, MessageBubble)
        header_labels = _walk_labels(asst_bubble)
        assert any("6.1" in lbl or "Sol" in lbl for lbl in header_labels)

        # Return to Gemini Carbon -> resumes gem-111 with second HandoffDividerRow!
        MainWindow._apply_model_choice(win, "gemini-4-carbon")
        _pump_gtk()
        assert win._next_chat.resume_id == "gem-111"
        final_children = _list_children(transcript._list)
        assert [type(w) for w in final_children] == [
            MessageBubble,
            MessageBubble,
            HandoffDividerRow,
            MessageBubble,
            MessageBubble,
            HandoffDividerRow,
        ]
        assert id(final_children[0]) == b0_id
        assert id(final_children[1]) == b1_id
    finally:
        store.close()


def test_intra_provider_google_switch_retires_idle_driver_and_keeps_view_mounted(tmp_path):
    win, store, coord, transcript, _composer, _toasts, _sent = _make_switch_window_harness(
        tmp_path
    )
    try:
        old_drv = win._driver
        b0 = _list_children(transcript._list)[0]

        MainWindow._apply_model_choice(win, "gemini-3.8-flash")
        _pump_gtk()

        assert old_drv.ended_input is True
        part = coord.participant(win._next_chat.work_id, "google")
        assert part is not None and part.generation == 2
        assert win._next_chat.resume_id == ""
        children = _list_children(transcript._list)
        assert id(children[0]) == id(b0)
        assert isinstance(children[-1], HandoffDividerRow)
        assert "Flash" in children[-1].boundary.summary_text
    finally:
        store.close()


def test_leg_scoped_tool_use_id_collision_never_cross_attaches_across_legs():
    tv = TranscriptView()

    leg1_asst = Turn(role="assistant", session_id="leg-1", provider="google", model="gemini-4-carbon")
    leg1_asst.text_parts.append("Running leg 1 command")
    leg1_asst.tool_uses.append(ToolUse(id="call_1", name="Bash", input={"command": "echo leg1"}))
    leg1_asst.tool_results.append(ToolResult(tool_use_id="call_1", content="LEG1_OUTPUT", is_error=False))
    tv.append_turn(leg1_asst)

    tv.append_handoff_divider(
        HandoffBoundary(
            leg_seq=2,
            from_provider="google",
            from_model="gemini-4-carbon",
            to_provider="openai",
            to_model="gpt-6.1-sol",
        )
    )

    leg2_asst = Turn(role="assistant", session_id="leg-2", provider="openai", model="gpt-6.1-sol")
    leg2_asst.text_parts.append("Running leg 2 command")
    leg2_asst.tool_uses.append(ToolUse(id="call_1", name="Bash", input={"command": "echo leg2"}))
    tv.append_turn(leg2_asst)

    # Foreign session tool result with colliding call_1 MUST NOT attach to leg-2!
    foreign_res = Turn(
        role="tool",
        session_id="foreign-leg",
        provider="openai",
        tool_results=[ToolResult(tool_use_id="call_1", content="FOREIGN_POISON", is_error=True)],
    )
    tv.append_turn(foreign_res)

    # Matching leg-2 tool result attaches cleanly to leg-2's bubble without touching leg-1!
    leg2_res = Turn(
        role="tool",
        session_id="leg-2",
        provider="openai",
        tool_results=[ToolResult(tool_use_id="call_1", content="LEG2_OUTPUT", is_error=False)],
    )
    tv.append_turn(leg2_res)
    _pump_gtk()

    bubbles = [w for w in _list_children(tv._list) if isinstance(w, MessageBubble)]
    asst_bubbles = [w for w in bubbles if w._turn.role == "assistant"]
    tool_bubbles = [w for w in bubbles if w._turn.role == "tool"]
    assert len(asst_bubbles) == 2
    assert [r.content for r in asst_bubbles[0]._all_tool_results()] == ["LEG1_OUTPUT"]
    assert [r.content for r in asst_bubbles[1]._all_tool_results()] == ["LEG2_OUTPUT"]
    assert len(tool_bubbles) == 1
    assert [r.content for r in tool_bubbles[0]._all_tool_results()] == ["FOREIGN_POISON"]


def test_mid_turn_pending_model_switch_preserves_inflight_delivery_auto_dispatches_and_stops_losslessly(
    tmp_path,
):
    win, store, _coord, _transcript, composer, _toasts, sent_on_new_model = (
        _make_switch_window_harness(tmp_path)
    )
    try:
        drv = win._driver
        drv.is_busy = True
        qid_wire = drv.queue_user_text("already_on_wire")
        qid_succ = drv.queue_user_text("unsent_successor")
        drv._pending_queue_delivery = (qid_wire, "already_on_wire")
        win._transcript.append_queued(qid_wire, "already_on_wire", win._make_queued_remover(drv))
        win._transcript.append_queued(qid_succ, "unsent_successor", win._make_queued_remover(drv))

        # Switch mid-turn to GPT 6.1 Sol -> wire item stays on drv; unsent successor moves to _PendingModelSwitch!
        MainWindow._apply_model_choice(win, "gpt-6.1-sol")
        assert win._pending_model_switch is not None
        assert drv.queued_messages() == [(qid_wire, "already_on_wire")]
        assert [t for _q, t in win._pending_model_switch.queued_items] == ["unsent_successor"]

        # Mid-turn follow-up submitted after switch is queued onto _PendingModelSwitch too!
        win._on_composer_send(composer, "second_followup")
        assert [t for _q, t in win._pending_model_switch.queued_items] == [
            "unsent_successor",
            "second_followup",
        ]

        # Streaming turn finishes -> auto-switches to GPT 6.1 Sol, sends first item, and queues second on new driver!
        drv.is_busy = False
        MainWindow._complete_pending_model_switch_if_ready(win, drv, allow_auto_send=True)
        _pump_gtk()

        assert win._pending_model_switch is None
        assert sent_on_new_model == [("gpt-6.1-sol", "unsent_successor")]
        assert [t for _q, t in win._driver.queued_messages()] == ["second_followup"]

        # Sub-case B: Explicit Stop while _PendingModelSwitch is queued drains back to composer without auto-sending!
        cur_drv = win._driver
        cur_drv.take_queued()
        cur_drv.is_busy = True
        composer.set_text("")
        MainWindow._apply_model_choice(win, "claude-fable-4-7")
        win._on_composer_send(composer, "must_return_to_composer")
        assert win._pending_model_switch is not None

        MainWindow._on_composer_stop(win)
        _pump_gtk()
        assert composer.current_text() == "must_return_to_composer"
        # Even if a late turn result arrives with allow_auto_send=True, nothing auto-dispatches!
        sent_before = list(sent_on_new_model)
        MainWindow._complete_pending_model_switch_if_ready(win, cur_drv, allow_auto_send=True)
        assert sent_on_new_model == sent_before
        assert composer.current_text() == "must_return_to_composer"
    finally:
        store.close()


def test_window_goal_fails_closed_when_continuity_context_cannot_compile(tmp_path):
    store = WorkStore(tmp_path / "work" / "work.db")
    try:
        w = store.create_work(cwd="/repo", lead_provider="openai")
        store.enable_continuity(w.work_id)

        def _boom(**_kw):
            raise RuntimeError("simulated continuity failure")

        fake_win = types.SimpleNamespace(
            _driver=None,
            _driver_manager=types.SimpleNamespace(starting=None),
            _pending_goal=None,
            _work_coordinator=types.SimpleNamespace(
                store=store,
                prepare_prompt=_boom,
            ),
            _driver_provider=lambda _d: "openai",
            _toast=lambda *_a, **_kw: None,
        )
        drv = types.SimpleNamespace(
            _helios_work_id=w.work_id,
            _helios_participant_provider="openai",
            session_id="thr-1",
            model="gpt-6.1-sol",
            supports_native_goals=False,
        )
        with pytest.raises(RequiredPromptContextError):
            GoalWorkMixin._goal_context_for_driver(fake_win, drv, "hello")
    finally:
        store.close()


def test_openrouter_driver_set_model_strips_reasoning_details_and_carryover_stays_transient(
    tmp_path,
    monkeypatch,
):
    drv = OpenRouterDriver(cwd=str(tmp_path), model="anthropic/claude-sonnet-4.6")
    drv._session_id = "or-sess-1"
    drv._history = [
        {"role": "user", "content": "Solve puzzle"},
        {
            "role": "assistant",
            "content": "Answer is 42.",
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque_sig"}],
            "reasoning": "step by step",
        },
    ]
    outcomes: list[tuple[bool, str]] = []
    drv.set_model("openai/gpt-5.4", lambda ok, detail: outcomes.append((ok, detail)))
    assert outcomes and outcomes[0][0] is True
    assert drv.model == "openai/gpt-5.4"
    assert "reasoning_details" not in drv._history[1]
    assert "reasoning" not in drv._history[1]
    assert drv._history[1]["content"] == "Answer is 42."

    # Verify transient wire_history multimodal injection keeps _history[-1]["content"] as str.
    drv._history.append({"role": "user", "content": "Inspect carried image"})
    captured_messages: list[list[dict]] = []

    def _fake_stream_chat(messages, **_kwargs):
        captured_messages.append(messages)
        yield or_chat.TextDelta("Looks sharp.")
        yield or_chat.Done(
            types.SimpleNamespace(
                finish_reason="stop",
                usage=types.SimpleNamespace(
                    input_tokens=120,
                    output_tokens=10,
                    cached_tokens=0,
                    cost_usd=0.001,
                    reported=True,
                ),
                reasoning_details=(),
            )
        )

    monkeypatch.setattr(or_chat, "stream_chat", _fake_stream_chat)
    monkeypatch.setattr(drv, "_context_window", lambda: 128_000)
    monkeypatch.setattr(drv, "_route", None)
    streaming = types.SimpleNamespace(blocks=[])
    img_parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]

    drv._stream_round(
        "sk-or-test",
        streaming,
        max_tokens=512,
        round_context=(),
        handoff=False,
        on_response_accepted=lambda _rid: None,
        image_parts=img_parts,
    )
    assert len(captured_messages) == 1
    sent_user = captured_messages[0][-1]
    assert isinstance(sent_user["content"], list)
    assert sent_user["content"][1]["type"] == "image_url"
    # Persistent replay history remains a plain string!
    assert drv._history[-1]["content"] == "Inspect carried image"


def test_session_list_coalesces_multi_leg_work_and_deletes_all_exclusive_legs(
    tmp_path,
    monkeypatch,
):
    proj_root = tmp_path / "claude-projects"
    proj_dir = proj_root / "-repo"
    monkeypatch.setattr(projects, "PROJECTS_DIR", proj_root)
    monkeypatch.setattr(projects, "discover_pool_projects", lambda: [])

    s1_path = proj_dir / "coal-gem-1.jsonl"
    s2_path = proj_dir / "coal-gpt-2.jsonl"
    _write_jsonl(
        s1_path,
        [
            {
                "type": "user",
                "sessionId": "coal-gem-1",
                "version": "helios-gemini",
                "cwd": "/repo",
                "message": {"role": "user", "content": "Unified multi-model title"},
            }
        ],
    )
    _write_jsonl(
        s2_path,
        [
            {
                "type": "user",
                "sessionId": "coal-gpt-2",
                "version": "helios-codex",
                "cwd": "/repo",
                "message": {"role": "user", "content": "Second leg prompt"},
            }
        ],
    )
    session_providers.set_provider("coal-gem-1", "google")
    session_providers.set_provider("coal-gpt-2", "openai")

    store = WorkStore(work_store.default_work_db_path())
    try:
        w = store.create_work(cwd="/repo", lead_provider="openai")
        store.enable_continuity(w.work_id)
        store.bind_participant(w.work_id, "google", native_session_id="coal-gem-1")
        store.bind_participant(w.work_id, "openai", native_thread_id="coal-gpt-2")
        store.ensure_initial_work_leg(
            w.work_id,
            provider="google",
            model="gemini-4-carbon",
            native_session_id="coal-gem-1",
            transcript_path=str(s1_path),
        )
        store.append_work_leg(
            w.work_id,
            provider="openai",
            model="gpt-6.1-sol",
            native_session_id="coal-gpt-2",
            transcript_path=str(s2_path),
        )
    finally:
        store.close()

    proj = projects._scan_project_dir(proj_dir, origin="local", read_only=False)
    assert proj is not None
    raw_sessions = proj.load_sessions()
    assert len(raw_sessions) == 2

    slist = SessionList()
    coalesced, meta = slist._coalesce_continuity_sessions(raw_sessions)
    assert len(coalesced) == 1
    rep = coalesced[0]
    assert rep.session_id == "coal-gpt-2"
    assert rep.title == "Unified multi-model title"
    assert meta[rep.session_id].model_count == 2
    assert set(meta[rep.session_id].member_session_ids) == {"coal-gem-1", "coal-gpt-2"}

    # Render in SessionList and verify 1 row + "2 models" chip + aliased lookup for both leg IDs!
    slist.reload(rescan_pool=False)
    _pump_gtk(40)
    rows = _list_children(slist._listbox)
    assert len(rows) == 1
    row_labels = _walk_labels(rows[0])
    assert "2 models" in row_labels
    assert slist._rows_by_id["coal-gem-1"] is rows[0]
    assert slist._rows_by_id["coal-gpt-2"] is rows[0]

    # Deleting the unified row deletes BOTH underlying exclusive leg .jsonl files!
    deleted_events: list[list[str]] = []
    slist.connect("sessions-deleted", lambda _w, ids: deleted_events.append(list(ids)))
    SessionList._on_delete_response(slist, None, "delete", [rep])
    assert not s1_path.exists()
    assert not s2_path.exists()
    assert deleted_events and set(deleted_events[0]) == {"coal-gem-1", "coal-gpt-2"}



def test_rejected_async_live_set_model_rolls_back_ui_and_never_persists_phantom_leg(tmp_path):
    win, store, coord, transcript, _composer, toasts, _sent = _make_switch_window_harness(
        tmp_path,
        initial_model="claude-fable-4-7",
        initial_provider="anthropic",
        initial_sid="cl-live-1",
    )
    try:
        pending_callbacks: list = []

        def _fake_set_model(alias: str, cb) -> bool:
            pending_callbacks.append((alias, cb))
            return True

        win._driver.set_model = _fake_set_model
        initial_children = _list_children(transcript._list)
        assert len(initial_children) == 2

        # Dispatch async set_model -> no divider or new leg before RPC reply!
        MainWindow._apply_model_choice(win, "claude-sonnet-4-6")
        _pump_gtk()
        assert len(pending_callbacks) == 1
        assert len(coord.list_work_legs(win._next_chat.work_id)) == 1
        assert len(_list_children(transcript._list)) == 2

        # Simulate RPC refusal -> restores claude-fable-4-7 and appends ZERO legs/dividers!
        _alias, cb = pending_callbacks.pop()
        cb(False, "model unavailable on tier")
        _pump_gtk()
        assert win._model == "claude-fable-4-7"
        assert len(coord.list_work_legs(win._next_chat.work_id)) == 1
        assert len(_list_children(transcript._list)) == 2
        assert any("Could not switch live session" in t for t in toasts)

        # Now dispatch and succeed -> appends leg 2 and renders HandoffDividerRow!
        MainWindow._apply_model_choice(win, "claude-sonnet-4-6")
        _alias2, cb2 = pending_callbacks.pop()
        cb2(True, "")
        _pump_gtk()
        assert win._model == "claude-sonnet-4-6"
        assert len(coord.list_work_legs(win._next_chat.work_id)) == 2
        after_ok = _list_children(transcript._list)
        assert len(after_ok) == 3
        assert isinstance(after_ok[-1], HandoffDividerRow)
    finally:
        store.close()


# ── A switched-away Claude must not take the conversation back ─────────────
#
# Reported 2026-11-25: a Fable chat was switched to GPT. GPT answered, then
# two seconds later Fable's still-running process finished a background task,
# began a turn by itself, and its `system/init` was recorded as the next Work
# leg — the chat flipped back to Fable with no user action, and Fable then
# rewrote the files GPT had just produced.


class _SelfWakingClaude:
    """Stands in for ClaudeCliDriver (isinstance is patched to this class)."""

    def __init__(self, session_id: str, model: str, *, busy: bool = False) -> None:
        self.provider = "anthropic"
        self.session_id = session_id
        self.model = model
        self.is_running = True
        self.is_accepting_input = True
        self.is_busy = busy
        self.has_background_work = False
        self.permission_mode = ""
        self.effort_key = ""
        self.workflow_mode = ""
        self.supports_native_goals = False


def _switched_to_gpt_work(tmp_path):
    store = WorkStore(tmp_path / "work" / "work.db")
    coord = WorkCoordinator(store)
    work = coord.ensure_work(cwd="/repo", lead_provider="anthropic")
    claude_part = coord.bind_participant(
        work.work_id, "anthropic", native_id="cl-1", model="fable[1m]"
    )
    coord.enable_continuity(work.work_id)
    coord.ensure_initial_work_leg(
        work.work_id, provider="anthropic", model="fable[1m]", native_session_id="cl-1"
    )
    # The user's switch to GPT: lead moves, GPT's leg becomes the tail.
    coord.select_lead(work.work_id, "openai")
    coord.bind_participant(work.work_id, "openai", native_id="th-1", model="gpt-6.2-sol")
    coord.append_work_leg(
        work.work_id, provider="openai", model="gpt-6.2-sol", native_session_id="th-1"
    )
    session_providers.set_provider("cl-1", "anthropic")
    return store, coord, work.work_id, claude_part


def _session_started_window(coord, *, torn: list, toasts: list, registered: list | None = None):
    registered = registered if registered is not None else []
    return types.SimpleNamespace(
        _destroyed=False,
        _driver_manager=types.SimpleNamespace(
            starting=object(),
            register_started=lambda d, sid: registered.append((d, sid)),
        ),
        _work_coordinator=coord,
        _conversation_perms=conversation_perms.store(),
        _next_chat=None,
        _driver_provider=lambda d: d.provider,
        _drv_is_current=lambda _d: False,
        _teardown_driver=lambda d: torn.append(d),
        _toast=lambda msg, **_kw: toasts.append(msg),
    )


@pytest.fixture
def _claude_is_fake(monkeypatch):
    import helios.main_window as mw

    monkeypatch.setattr(mw, "ClaudeCliDriver", _SelfWakingClaude)
    monkeypatch.setattr(mw.GLib, "timeout_add", lambda *_a, **_k: 0)
    monkeypatch.setattr(MainWindow, "_sidebar_show_pending", lambda *_a, **_k: None)


def test_switched_away_claude_waking_on_its_own_neither_retakes_the_chat_nor_keeps_running(
    tmp_path, _claude_is_fake
):
    store, coord, work_id, claude_part = _switched_to_gpt_work(tmp_path)
    try:
        torn: list = []
        toasts: list[str] = []
        registered: list = []
        win = _session_started_window(coord, torn=torn, toasts=toasts, registered=registered)
        claude = _SelfWakingClaude("cl-1", "claude-fable-5-2")
        tag_driver(claude, claude_part)

        # Background task finished → the CLI's own turn → system/init.
        MainWindow._on_session_started(win, claude, "cl-1", "/repo", "claude-fable-5-2")

        legs = coord.list_work_legs(work_id)
        assert [leg.provider for leg in legs] == ["anthropic", "openai"]
        assert store.get_work(work_id).lead_provider == "openai"
        # Stopped synchronously, inside the init handler: a deferred stop let
        # the turn's next stdout records (tool requests) be read first.
        assert torn == [claude]
        assert registered == []
        assert len(toasts) == 1
        assert "Stopped" in toasts[0] and "on its own" in toasts[0]
        assert "GPT" in toasts[0]
    finally:
        store.close()


def test_helios_dispatched_or_leading_claude_turns_are_never_stopped(
    tmp_path, _claude_is_fake
):
    store, coord, work_id, claude_part = _switched_to_gpt_work(tmp_path)
    try:
        torn: list = []
        toasts: list[str] = []
        win = _session_started_window(coord, torn=torn, toasts=toasts)

        # A turn Helios sent (busy) is not a stray, even off-lead — but it
        # does not become the conversation's leg either.
        busy = _SelfWakingClaude("cl-1", "claude-fable-5-2", busy=True)
        tag_driver(busy, claude_part)
        MainWindow._on_session_started(win, busy, "cl-1", "/repo", "claude-fable-5-2")
        assert torn == []
        assert coord.list_work_legs(work_id)[-1].provider == "openai"

        # Claude leads again (user switched back): its init records the leg.
        coord.select_lead(work_id, "anthropic")
        lead = _SelfWakingClaude("cl-1", "claude-fable-5-2")
        tag_driver(lead, claude_part)
        MainWindow._on_session_started(win, lead, "cl-1", "/repo", "claude-fable-5-2")
        assert coord.list_work_legs(work_id)[-1].provider == "anthropic"
        assert torn == [] and toasts == []
    finally:
        store.close()


@pytest.mark.parametrize("background", [True, False])
def test_switching_provider_stops_the_old_claude_only_when_its_tasks_would_wake_it(
    tmp_path, background
):
    win, store, coord, _transcript, _composer, toasts, _sent = _make_switch_window_harness(
        tmp_path,
        initial_model="claude-fable-4-7",
        initial_provider="anthropic",
        initial_sid="cl-bg-1",
    )
    try:
        old = win._driver
        old.has_background_work = background
        torn: list = []
        win._teardown_driver = lambda d: torn.append(d)

        MainWindow._apply_model_choice(win, "gpt-6.1-sol")
        _pump_gtk()

        assert win._model == "gpt-6.1-sol"
        if background:
            assert torn == [old]
            assert any("background" in t and "Switched to" in t for t in toasts)
        else:
            # Idle with nothing pending: kept warm for a cheap switch back.
            assert torn == []
            assert not any("background" in t for t in toasts)
    finally:
        store.close()
