"""Unit tests for the Muse MSP driver (no real `muse serve` process)."""

from __future__ import annotations

import io
import json

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

from helios.backend.agent_activity import MuseAgentActivityAdapter  # noqa: E402
from helios.backend.process.muse_driver import (  # noqa: E402
    MuseServeDriver,
    _normalize_tool,
    _translate_answers,
)
from helios.backend.process.streaming import StreamingAssistant  # noqa: E402

MODEL = "muse-wonder-1.0"


@pytest.fixture(autouse=True)
def drain_driver_callbacks():
    yield
    while GLib.MainContext.default().iteration(False):
        pass


class _FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass

    def frames(self):
        return [json.loads(data.decode("utf-8")) for data in self.writes]


class _FakeProcess:
    def __init__(self):
        self.pid = 123457
        self.stdin = _FakeStdin()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode


def _driver(tmp_path) -> MuseServeDriver:
    driver = MuseServeDriver(cwd=str(tmp_path), model=MODEL)
    driver._proc = _FakeProcess()
    driver._session_id = "session-1"
    driver._root_turn_id = "turn-1"
    driver._streaming = StreamingAssistant(model=MODEL)
    driver._is_busy = True
    return driver


def test_tool_names_normalize_to_helios_vocabulary():
    name, parsed = _normalize_tool("edit_file", {"path": "a.py", "find": "x", "replace": "y"})
    assert name == "Edit"
    assert parsed["file_path"] == "a.py"
    assert parsed["old_string"] == "x" and parsed["new_string"] == "y"
    assert _normalize_tool("bash", {"command": "ls"})[0] == "Bash"
    assert _normalize_tool("unknown_tool", {})[0] == "unknown_tool"


def test_agent_message_streams_and_reminders_stay_hidden(tmp_path):
    driver = _driver(tmp_path)
    driver._on_item({"itemId": "r1", "kind": "reminderChild", "turnId": "turn-1",
                     "status": "inProgress"}, terminal=False)
    driver._on_item({"itemId": "m1", "kind": "agentMessage", "turnId": "turn-1",
                     "status": "inProgress", "text": ""}, terminal=False)
    driver._on_delta({"itemId": "m1", "field": "text", "delta": "pong"})
    assert driver._agents == {}
    turn = driver._streaming.to_turn()
    assert turn.content and "pong" in turn.content[0].text


def test_subagent_lifecycle_feeds_dock_snapshot(tmp_path):
    driver = _driver(tmp_path)
    emitted = []
    driver.connect("agents-updated", lambda _d, snap: emitted.append(snap))
    driver._on_item({"itemId": "a1", "kind": "subagent", "turnId": "turn-1",
                     "status": "inProgress", "role": "reviewer",
                     "objective": "check auth", "subagentId": "sub-1"}, terminal=False)
    assert driver._agents["a1"]["status"] == "starting"
    driver._on_item({"itemId": "a1", "kind": "subagent", "turnId": "turn-1",
                     "status": "inProgress", "role": "reviewer"}, terminal=False)
    assert driver._agents["a1"]["status"] == "working"
    driver._on_item({"itemId": "a1", "kind": "subagent", "turnId": "turn-1",
                     "status": "completed", "role": "reviewer"}, terminal=True)
    assert driver._agents["a1"]["status"] == "complete"
    assert emitted  # dock was notified at every transition
    # The shared adapter must accept the snapshot unchanged.
    observations = MuseAgentActivityAdapter().observations(driver.observed_agent_snapshot())
    assert [(o.actor_id, o.status.value) for o in observations] == [("a1", "completed")]


def test_workflow_children_become_actors(tmp_path):
    driver = _driver(tmp_path)
    driver._on_item({"itemId": "w1", "kind": "workflow", "turnId": "turn-1",
                     "status": "inProgress",
                     "children": [{"childId": "c1", "attempt": 1, "status": "completed",
                                   "failureKind": "boom", "label": "scan"}]},
                    terminal=False)
    # "completed" with a failureKind means the child failed (MSP rule).
    assert driver._agents["w1:c1:1"]["status"] == "error"


def test_approval_uses_question_dialog_and_decides(tmp_path):
    driver = _driver(tmp_path)
    asked = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    driver._on_approval_requested({
        "approvalId": "ap-1", "toolName": "bash",
        "currentRequirementId": {"approvalId": "ap-1", "sourceIndex": 0},
        "availableChoices": [
            {"choiceId": "yes", "label": "Allow once", "decision": "approved", "scope": "once"},
            {"choiceId": "no", "label": "Deny", "decision": "denied", "scope": "once"},
        ]})
    assert asked and asked[0][1] == "ap-1"
    driver.answer_question("ap-1", "Allow once")
    frames = driver._proc.stdin.frames()
    decide = [f for f in frames if f.get("method") == "approval/decide"]
    assert decide and decide[0]["params"]["choiceId"] == "yes"
    # An unrecognized answer denies instead of hanging.
    driver._on_approval_requested({
        "approvalId": "ap-2", "toolName": "bash",
        "currentRequirementId": {"approvalId": "ap-2", "sourceIndex": 0},
        "availableChoices": [
            {"choiceId": "yes", "label": "Allow once", "decision": "approved", "scope": "once"},
            {"choiceId": "no", "label": "Deny", "decision": "denied", "scope": "once"},
        ]})
    driver.answer_question("ap-2", None)
    decide2 = [f for f in driver._proc.stdin.frames()
               if f.get("method") == "approval/decide"][-1]
    assert decide2["params"]["choiceId"] == "no"


def test_translate_answers_shapes():
    prompt = {"questions": [
        {"id": "q1", "selection": {"mode": "single"}, "options": [{"label": "A"}]},
        {"id": "q2", "selection": {"mode": "multiple"},
         "options": [{"label": "X"}, {"label": "Y"}]},
    ]}
    rows = _translate_answers(prompt, {"0": {"answers": ["A"]}, "1": {"answers": ["X", "Y"]}})
    assert rows == [
        {"questionId": "q1", "selectedLabel": "A"},
        {"questionId": "q2", "selectedLabels": ["X", "Y"]},
    ]
    assert _translate_answers(prompt, None) is None
    assert _translate_answers({"questions": [{"id": "q", "selection": {"mode": "single"}}]}, "B") == [
        {"questionId": "q", "freeText": "B"}]


def test_user_input_dialog_roundtrip_retains_question_until_native_ack(tmp_path):
    from helios.widgets.question_dialog import _format_answers, _normalize_questions

    driver = _driver(tmp_path)
    asked = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    driver._on_user_input_requested({"userInputId": "prompt", "questions": [
        {"id": "which_url", "question": "Which URL?", "options": [{"label": "example.com/ops"}]},
        {"id": "details", "question": "Details?", "options": []},
    ]})
    payload, token = asked[0]
    answer = _format_answers(_normalize_questions(payload), [["example.com"], ["After login"]])
    driver.answer_question(token, answer)
    frame = driver._proc.stdin.frames()[-1]
    assert frame["method"] == "userInput/answer"
    assert frame["params"]["answers"] == [
        {"questionId": "which_url", "freeText": "example.com"},
        {"questionId": "details", "freeText": "After login"},
    ]
    assert token in driver._pending_inputs
    # A second click while delivery is pending must not mint another command.
    driver.answer_question(token, answer)
    assert len(driver._proc.stdin.frames()) == 1
    driver._dispatch_message({"id": frame["id"], "result": {"status": "accepted"}})
    assert token not in driver._pending_inputs
    assert driver.is_busy  # answering never releases the running Work


def test_rejected_answer_reopens_question_without_replaying(tmp_path):
    driver = _driver(tmp_path)
    asked = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    driver._on_user_input_requested({"userInputId": "prompt", "questions": [{"id": "q"}]})
    driver.answer_question("prompt", "an answer")
    frame = driver._proc.stdin.frames()[-1]
    driver._dispatch_message({"id": frame["id"], "error": {
        "code": -32057, "message": "answer is invalid for user input prompt"}})
    assert len(asked) == 2 and asked[-1][1] != "prompt"
    assert driver._input_presentations[asked[-1][1]] == "prompt"
    assert "did not accept" in asked[-1][0]["caption"]
    assert "prompt" in driver._pending_inputs
    assert driver._input_commands == {}
    assert len(driver._proc.stdin.frames()) == 1
    driver.answer_question("prompt", "stale answer")
    assert len(driver._proc.stdin.frames()) == 1
    driver.answer_question(asked[-1][1], "corrected answer")
    assert len(driver._proc.stdin.frames()) == 2
    assert driver._proc.stdin.frames()[-1]["params"]["userInputId"] == "prompt"


def test_long_answer_reopens_locally_without_cancelling_or_truncating(tmp_path):
    driver = _driver(tmp_path)
    asked = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    driver._on_user_input_requested({"userInputId": "prompt", "questions": [{"id": "q"}]})
    driver.answer_question("prompt", "x" * 501)
    assert len(asked) == 2 and "500" in asked[-1][0]["caption"]
    assert not driver._proc.stdin.frames()
    assert "prompt" in driver._pending_inputs


def test_cancel_waits_for_native_ack(tmp_path):
    driver = _driver(tmp_path)
    driver._on_user_input_requested({"userInputId": "prompt", "questions": [{"id": "q"}]})
    driver.answer_question("prompt", None)
    frame = driver._proc.stdin.frames()[-1]
    assert frame["method"] == "userInput/cancel"
    assert "prompt" in driver._pending_inputs
    driver._dispatch_message({"id": frame["id"], "result": {"status": "accepted"}})
    assert driver._pending_inputs == {}


def test_settled_question_cannot_reopen_on_late_rejection(tmp_path):
    driver = _driver(tmp_path)
    asked = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    driver._on_user_input_requested({"userInputId": "prompt", "questions": [{"id": "q"}]})
    driver.answer_question("prompt", "answer")
    frame = driver._proc.stdin.frames()[-1]
    driver._dispatch_message({"method": "userInput/settled", "params": {
        "sessionId": "session-1", "userInputId": "prompt"}})
    driver._dispatch_message({"id": frame["id"], "error": {"code": -32057}})
    assert len(asked) == 1
    assert driver._pending_inputs == {}


def test_failed_answer_write_retains_prompt_without_automatic_replay(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    driver._on_user_input_requested({"userInputId": "prompt", "questions": [{"id": "q"}]})
    monkeypatch.setattr(driver, "_write", lambda _message: False)
    errors = []
    driver.connect("error", lambda _d, message: errors.append(message))
    driver.answer_question("prompt", "answer")
    assert "uncertain" in errors[-1]
    assert "prompt" in driver._pending_inputs
    assert driver.is_busy


def test_serve_args_follow_permission_mode():
    from helios.backend.process.muse_driver import MUSE_INTERACTIVE_ENV, serve_args

    assert "--disable-sandbox" in serve_args("muse-x", "bypassPermissions")
    assert "--disable-sandbox" not in serve_args("muse-x", "default")
    plan = serve_args("muse-x", "plan")
    assert "--disable-shell" in plan and "--disable-sandbox" not in plan
    assert {"META_API_KEY", "NORVI_TRACKER_API_TOKEN"} <= MUSE_INTERACTIVE_ENV



def test_session_bound_populates_tools_and_emits_capabilities(tmp_path, monkeypatch):
    from helios.backend import muse_env

    monkeypatch.setattr(
        muse_env,
        "list_mcp_servers",
        lambda: [{"name": "code-intel", "command": "python3", "args": []}],
    )
    driver = MuseServeDriver(cwd=str(tmp_path), model=MODEL)
    driver._proc = _FakeProcess()
    caps = []
    driver.connect("capabilities-updated", lambda _d, payload: caps.append(payload))
    driver._on_session_bound({"sessionId": "s-123", "modelId": MODEL, "workspaceRoot": str(tmp_path)})
    assert driver.init_tools
    assert driver.init_mcp_servers == [{"name": "code-intel", "status": "configured"}]
    assert caps and caps[0]["mcp_servers"] == [{"name": "code-intel", "status": "configured"}]


def test_reasoning_summary_survives_empty_updates_and_raw_text_fallback(tmp_path):
    driver = _driver(tmp_path)
    driver._on_item({"itemId": "rs", "kind": "reasoning", "turnId": "turn-1",
                     "status": "inProgress", "summary": []}, terminal=False)
    driver._on_delta({"itemId": "rs", "field": "summary.0", "delta": "Reading the "})
    driver._on_delta({"itemId": "rs", "field": "summary.0", "delta": "schema."})
    driver._on_delta({"itemId": "rs", "field": "summary.1", "delta": "Applying rules."})
    # An empty re-emission must not wipe the streamed parts.
    driver._on_item({"itemId": "rs", "kind": "reasoning", "turnId": "turn-1",
                     "status": "inProgress", "summary": []}, terminal=False)
    assert driver._blocks["rs"].text == "Reading the schema.\n\nApplying rules."
    # No summary, only committed raw text: show the raw text.
    driver._on_item({"itemId": "rt", "kind": "reasoning", "turnId": "turn-1",
                     "status": "completed", "summary": [], "text": "why"}, terminal=True)
    assert driver._blocks["rt"].text == "why"
    assert driver._blocks["rt"].type == "reasoning_summary"


def test_set_model_uses_native_session_set_model_without_respawn(tmp_path):
    driver = _driver(tmp_path)
    driver._is_busy = False
    driver._accepting_input = True
    driver._stopped = False
    outcomes = []
    assert driver.set_model("muse-wonder-full-1.0", lambda ok, detail: outcomes.append((ok, detail)))
    sent = driver._proc.stdin.frames()[-1]
    assert sent["method"] == "session/setModel"
    assert sent["params"]["sessionId"] == "session-1"
    assert sent["params"]["model"] == {"modelId": "muse-wonder-full-1.0"}
    assert outcomes == [] and driver.model == MODEL  # only the ack applies it
    driver._dispatch_message({"jsonrpc": "2.0", "id": sent["id"],
                            "result": {"commandId": "c", "status": "accepted"}})
    assert outcomes == [(True, "")] and driver.model == "muse-wonder-full-1.0"
    # Reasoning is a per-turn field: no RPC at all.
    assert driver.set_effort("high", lambda ok, _d: outcomes.append(ok))
    assert driver._effort == "high" and outcomes[-1] is True


def test_context_meter_gets_used_and_window_not_cumulative_tokens(tmp_path):
    """tokenUsage's cumulative (prompt, output) used to reach the meter as
    (used, window): "2k max, 100% used"."""
    driver = _driver(tmp_path)
    seen = []
    driver.connect("usage-updated", lambda _d, used, window: seen.append((used, window)))
    driver._dispatch_message({"jsonrpc": "2.0", "method": "session/tokenUsage", "params": {
        "usage": {"promptTokens": 5}, "cumulative": {"promptTokens": 90_000, "outputTokens": 2_000}}})
    assert seen == []
    driver._dispatch_message({"jsonrpc": "2.0", "method": "session/contextUsage", "params": {
        "usedTokens": 41_000, "windowTokens": 1_007_997, "pressure": "low"}})
    assert seen == [(41_000, 1_007_997)]
def test_context_and_subscription_usage_reach_the_meter(tmp_path):
    """Occupancy comes from session/contextUsage; cumulative token counts and
    the 5-hour/weekly percentages are not mistaken for (used, window)."""
    driver = _driver(tmp_path)
    usage, limits = [], []
    driver.connect("usage-updated", lambda _d, used, window: usage.append((used, window)))
    driver.connect("rate-limit-updated", lambda _d, row: limits.append(row))

    driver._dispatch_message({"method": "session/tokenUsage", "params": {
        "usage": {"inputTokens": 900, "outputTokens": 40},
        "cumulative": {"promptTokens": 5000, "outputTokens": 400, "totalTokens": 5400}}})
    assert usage == []
    driver._dispatch_message({"method": "session/contextUsage", "params": {
        "sessionId": "session-1", "usedTokens": 42_000, "windowTokens": 256_000,
        "pressure": "normal", "sourceRange": {}, "viewCursor": "c1"}})
    assert usage == [(42_000, 256_000)]

    driver._dispatch_message({"method": "usage/changed", "params": {
        "observedAtMs": 1, "tier": "pro",
        "window": {"usedPercent": 55, "resetsAtMs": 1_900_000_000_000, "windowDurationMins": 300},
        "weekly": {"usedPercent": 130, "resetsAtMs": 1_900_000_500_000}}})
    assert [(r["rateLimitType"], r["label"], r["usedPercent"], r["status"], r["resetsAt"]) for r in limits] == [
        ("muse_window", "5-hour usage", 55.0, "allowed", 1_900_000_000),
        ("muse_weekly", "Weekly usage", 100.0, "exceeded", 1_900_000_500),
    ]
    assert {r["provider"] for r in limits} == {"muse"}


def test_session_mcp_browser_is_offered_only_when_granted(tmp_path):
    from helios.backend import browser_tools

    driver = _driver(tmp_path)
    driver._session_id = ""
    browser_tools.publish("unix:path=/run/user/1000/bus")
    try:
        driver._begin_session()
        assert "config" not in driver._proc.stdin.frames()[-1]["params"]
        driver._session_mcp = True
        driver._begin_session()
        server = driver._proc.stdin.frames()[-1]["params"]["config"]["mcpServers"]["helios_browser"]
        assert server["transport"] == "stdio" and server["mode"] == "optional"
        assert browser_tools.session_for(server["args"][-1])[1] == "Muse"
    finally:
        browser_tools.publish("")


def test_background_workflow_and_host_initiated_continuation_turn(tmp_path):
    driver = _driver(tmp_path)
    driver._transcript.bind_thread("session-1")
    appended = []
    results = []
    bg_events = []
    statuses = []
    driver.connect("turn-appended", lambda _d, turn: appended.append(turn))
    driver.connect("result", lambda _d, res: results.append(res))
    driver.connect("background-tasks-changed", lambda _d, rows: bg_events.append(rows))
    driver.connect("turn-status-updated", lambda _d, st: statuses.append(st))

    # Turn 1 spawns a background workflow and finishes its foreground turn.
    driver._dispatch_message({
        "method": "item/started",
        "params": {
            "item": {
                "itemId": "w1",
                "kind": "workflow",
                "turnId": "turn-1",
                "status": "inProgress",
                "role": "review-council",
                "objective": "10-agent review",
                "workflowRunId": "wf-1",
            }
        },
    })
    assert driver.has_background_work is True
    assert driver.background_task_rows == [{
        "task_id": "wf-1",
        "task_type": "workflow",
        "description": "review-council — 10-agent review",
    }]

    # Simulate end of Turn 1 (no active attempt id in this unit fixture).
    driver._dispatch_message({
        "method": "turn/completed",
        "params": {"turnId": "turn-1", "terminal": "completed", "usage": {}},
    })
    assert driver.is_busy is False
    # Background workflow is still running between turns -> prevents idle reap / eviction.
    assert driver.has_background_work is True

    # Workflow finishes and wakes `muse serve` for a host-initiated continuation turn.
    driver._dispatch_message({
        "method": "item/completed",
        "params": {
            "item": {
                "itemId": "w1",
                "kind": "workflow",
                "turnId": "turn-1",
                "status": "completed",
                "role": "review-council",
                "workflowRunId": "wf-1",
            }
        },
    })
    assert driver._agents["w1"]["status"] == "complete"
    assert driver.has_background_work is False
    assert bg_events[-1] == []

    # Host-initiated continuation turn starts with no user execution_attempt_id.
    assert driver.execution_attempt_id == ""
    driver._dispatch_message({
        "method": "turn/started",
        "params": {"turnId": "turn-2"},
    })
    assert driver.is_busy is True
    assert driver._streaming is not None
    assert statuses[-1] == {"turnId": "turn-2", "status": "inProgress"}

    driver._dispatch_message({
        "method": "item/started",
        "params": {
            "item": {
                "itemId": "m2",
                "kind": "agentMessage",
                "turnId": "turn-2",
                "status": "inProgress",
                "text": "",
            }
        },
    })
    driver._dispatch_message({
        "method": "item/delta",
        "params": {
            "itemId": "m2",
            "field": "text",
            "delta": "All 10 review findings fixed and verified.",
        },
    })
    driver._dispatch_message({
        "method": "turn/completed",
        "params": {
            "turnId": "turn-2",
            "terminal": "completed",
            "usage": {"inputTokens": 1200, "outputTokens": 300},
        },
    })

    assert driver.is_busy is False
    assert len(appended) == 1
    assert "All 10 review findings fixed and verified." in appended[0].content[0].text
    assert results[-1]["subtype"] == "success"
    assert results[-1]["usage"] == {"input_tokens": 1200, "output_tokens": 300}


def test_open_tool_call_survives_turn_completion_as_background_work_and_settles_across_turns(tmp_path):
    driver = _driver(tmp_path)
    bg_events = []
    driver.connect("background-tasks-changed", lambda _d, rows: bg_events.append(rows))

    driver._dispatch_message({
        "method": "item/started",
        "params": {
            "item": {
                "itemId": "tc-1",
                "kind": "toolCall",
                "turnId": "turn-1",
                "status": "inProgress",
                "tool": "bash",
                "args": {"command": "pytest -q", "description": "Run test suite"},
            }
        },
    })
    # Foreground turn completes while tc-1 is still inProgress -> promoted to background work.
    driver._dispatch_message({
        "method": "turn/completed",
        "params": {"turnId": "turn-1", "terminal": "completed", "usage": {}},
    })
    assert driver.has_background_work is True
    assert driver.background_task_rows == [{
        "task_id": "tc-1",
        "task_type": "local_bash",
        "description": "Run test suite",
    }]

    # Even after a later turn starts, item/completed with the earlier turnId settles tc-1.
    driver._dispatch_message({
        "method": "turn/started",
        "params": {"turnId": "turn-2"},
    })
    driver._dispatch_message({
        "method": "item/completed",
        "params": {
            "item": {
                "itemId": "tc-1",
                "kind": "toolCall",
                "turnId": "turn-1",
                "status": "completed",
                "tool": "bash",
            }
        },
    })
    assert driver.has_background_work is False
    assert driver.background_task_rows == []
    assert bg_events[-1] == []


def test_session_goal_changed_emits_native_goal_updated(tmp_path):
    driver = _driver(tmp_path)
    goals = []
    driver.connect("native-goal-updated", lambda _d, payload: goals.append(payload))

    driver._dispatch_message({
        "method": "session/goalChanged",
        "params": {
            "sessionId": "session-1",
            "goal": {"goalId": "g-1", "objective": "Ship feature", "status": "active"},
        },
    })
    driver._dispatch_message({
        "method": "session/goalChanged",
        "params": {"sessionId": "session-1", "goal": None},
    })
    assert goals == [
        {"goal": {"goalId": "g-1", "objective": "Ship feature", "status": "active"}, "cleared": False},
        {"goal": None, "cleared": True},
    ]


def test_stale_terminal_does_not_finish_newer_continuation(tmp_path):
    driver = _driver(tmp_path)
    driver._root_turn_id = "continuation-2"
    results = []
    driver.connect("result", lambda _d, result: results.append(result))
    driver._dispatch_message({"method": "turn/completed", "params": {
        "turnId": "turn-1", "terminal": "completed"}})
    assert driver.is_busy
    assert driver._streaming is not None
    assert not results


def test_continuation_transcript_failure_blocks_instead_of_claiming_saved(tmp_path):
    driver = _driver(tmp_path)
    driver._on_item({"itemId": "reply", "kind": "agentMessage", "text": "Done"}, terminal=True)
    class BrokenTranscript:
        def append_assistant(self, *args, **kwargs):
            raise OSError("disk full")
    driver._transcript = BrokenTranscript()
    appended, results = [], []
    driver.connect("turn-appended", lambda _d, turn: appended.append(turn))
    driver.connect("result", lambda _d, result: results.append(result))
    driver._finish_turn("completed")
    assert driver._accounting_failed
    assert not appended
    assert not results


def test_agent_updates_preserve_native_task_identity(tmp_path):
    driver = _driver(tmp_path)
    driver._on_item({"itemId": "w1", "kind": "workflow", "status": "inProgress",
                     "workflowRunId": "native-workflow"}, terminal=False)
    driver._on_item({"itemId": "w1", "kind": "workflow", "status": "inProgress"}, terminal=False)
    assert driver.background_task_rows[0]["task_id"] == "native-workflow"
    assert driver.stop_background_task("native-workflow")
    assert driver._proc.stdin.frames()[-1]["params"]["workflowRunId"] == "native-workflow"
