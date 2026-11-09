from __future__ import annotations

import io
import json

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

from helios.backend.process.kimi_driver import KimiAcpDriver, _normalize_tool  # noqa: E402
from helios.backend.process.streaming import StreamingAssistant  # noqa: E402

MODEL = "kimi-code/k3"


@pytest.fixture(autouse=True)
def drain_callbacks():
    yield
    while GLib.MainContext.default().iteration(False):
        pass


class _Stdin:
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
        return [json.loads(row) for row in self.writes]


class _Process:
    def __init__(self):
        self.pid = 4242
        self.stdin = _Stdin()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode


def _driver(tmp_path, monkeypatch=None):
    driver = KimiAcpDriver(cwd=str(tmp_path), model=MODEL)
    driver._proc = _Process()
    driver._session_id = "session_kimi"
    driver._accepting_input = True
    driver._root_turn_id = "acp-prompt-7"
    driver._execution_attempt_id = "attempt-1"
    driver._streaming = StreamingAssistant(model=MODEL)
    driver._is_busy = True
    if monkeypatch is not None:
        monkeypatch.setattr(driver, "_accept_prompt", lambda _id: True)
    return driver


def test_resume_binds_requested_id_when_response_omits_it(tmp_path):
    driver = KimiAcpDriver(cwd=str(tmp_path), model=MODEL, resume_session_id="session_resume")
    driver._proc = _Process()
    driver._on_session_bound({"configOptions": []})
    assert driver.session_id == "session_resume"
    assert driver.is_accepting_input


def test_acp_message_and_thought_chunks_share_stream(tmp_path, monkeypatch):
    driver = _driver(tmp_path, monkeypatch)
    driver._on_update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "Think"}})
    driver._on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Answer"}})
    blocks = driver._streaming.blocks
    assert [(b.type, b.text) for b in blocks] == [("reasoning_summary", "Think"), ("text", "Answer")]


def test_tool_call_and_update_create_one_card(tmp_path, monkeypatch):
    driver = _driver(tmp_path, monkeypatch)
    driver._on_update({"sessionUpdate": "tool_call", "toolCallId": "call-1", "title": "Read file",
                       "kind": "read", "status": "in_progress", "rawInput": {"path": "a.py"}})
    driver._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "call-1",
                       "status": "completed", "content": [{"type": "text", "text": "contents"}]})
    assert len(driver._streaming.blocks) == 1
    block = driver._streaming.blocks[0]
    assert block.tool_use_name == "Read"
    assert json.loads(block.tool_use_input_json)["file_path"] == "a.py"
    assert driver._tool_outputs["call-1"] == "contents"


def test_live_execute_and_nested_tool_content_normalize(tmp_path, monkeypatch):
    driver = _driver(tmp_path, monkeypatch)
    driver._on_update({"sessionUpdate": "tool_call", "toolCallId": "shell-1",
                       "kind": "execute", "rawInput": {"command": "pwd"}})
    driver._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "shell-1",
                       "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}]})
    assert driver._streaming.blocks[0].tool_use_name == "Bash"
    assert driver._tool_outputs["shell-1"] == "ok"


def test_permission_answer_uses_native_outcome(tmp_path):
    driver = _driver(tmp_path)
    asked = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    driver._on_client_request({"id": 91, "method": "session/request_permission", "params": {
        "toolCall": {"title": "Run tests"}, "options": [
            {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
        ]}})
    assert asked[0][1] == "91"
    driver.answer_question("91", "Allow once")
    response = driver._proc.stdin.frames()[-1]
    assert response["id"] == 91
    assert response["result"]["outcome"] == {"outcome": "selected", "optionId": "allow"}


def test_dismissed_permission_cancels(tmp_path):
    driver = _driver(tmp_path)
    driver._pending_requests["x"] = {"id": "x", "method": "session/request_permission", "params": {"options": []}}
    driver.answer_question("x", None)
    assert driver._proc.stdin.frames()[-1]["result"]["outcome"]["outcome"] == "cancelled"


def _form_elicitation(request_id=55):
    return {"id": request_id, "method": "elicitation/create", "params": {
        "sessionId": "session_kimi", "toolCallId": "call-9", "mode": "form",
        "message": "Which region?\nWhat output?",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "q0": {"type": "string", "title": "Region",
                       "oneOf": [{"const": "us", "title": "us"}, {"const": "eu", "title": "eu"}]},
                "q1": {"type": "array", "title": "Output", "minItems": 1,
                       "items": {"anyOf": [{"const": "json", "title": "json"},
                                           {"const": "table", "title": "table"}]}},
            },
            "required": ["q0", "q1"],
        }}}


def test_form_elicitation_preserves_question_ids_and_native_answer_types(tmp_path):
    driver = _driver(tmp_path)
    asked = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    driver._dispatch_message(_form_elicitation())

    (payload, token), = asked
    region, output = payload["questions"]
    assert region["id"] == "q0"
    assert region["header"] == "Region"
    assert region["question"] == "Which region?"
    assert [option["label"] for option in region["options"]] == ["us", "eu"]
    assert region["allowOther"] is False
    assert output["id"] == "q1"
    assert output["multiSelect"] is True
    assert output["question"] == "What output?"
    assert not driver._proc.stdin.writes

    driver.answer_question(token, {"q0": {"answers": ["eu"]}, "q1": {"answers": ["json", "table"]}})
    response = driver._proc.stdin.frames()[-1]
    assert response["id"] == 55
    assert response["result"] == {
        "action": "accept", "content": {"q0": "eu", "q1": ["json", "table"]}}
    assert driver._pending_requests == {}


def test_dismissing_or_retiring_form_rejects_late_answers(tmp_path):
    driver = _driver(tmp_path)
    retired = []
    driver.connect("interaction-resolved", lambda _d, token: retired.append(token))
    driver._dispatch_message(_form_elicitation())
    driver.answer_question("55", None)
    assert driver._proc.stdin.frames()[-1]["result"] == {"action": "cancel"}

    driver._dispatch_message(_form_elicitation(56))
    driver._on_exit(0)
    driver.answer_question("56", {"q0": {"answers": ["eu"]}, "q1": {"answers": ["json"]}})
    assert retired == ["56"]
    assert len(driver._proc.stdin.frames()) == 1


def test_dynamic_model_effort_and_mode_settings(tmp_path):
    driver = _driver(tmp_path)
    driver._is_busy = False
    assert driver.set_model("kimi-code/k3.1-pro")
    assert driver.set_effort("max")
    assert driver.set_permission_mode("plan")
    frames = driver._proc.stdin.frames()
    assert [f["params"]["configId"] for f in frames] == ["model", "thinking", "mode"]
    for frame, value in zip(frames, ("kimi-code/k3.1-pro", "max", "plan"), strict=True):
        driver._dispatch_message({"id": frame["id"], "result": {}})
        assert frame["params"]["value"] == value
    assert driver.model == "kimi-code/k3.1-pro"
    assert driver.effort_key == "max"
    assert driver.permission_mode == "plan"


def test_native_fork_emits_source_and_fork_ids(tmp_path):
    driver = _driver(tmp_path)
    driver._is_busy = False
    forks = []
    driver.connect("thread-forked", lambda _d, payload: forks.append(payload))
    assert driver.request_fork().pending
    request = driver._proc.stdin.frames()[-1]
    assert request["method"] == "session/fork"
    assert request["params"]["cwd"] == str(tmp_path)
    assert request["params"]["mcpServers"] == []
    driver._dispatch_message({"id": request["id"], "result": {"sessionId": "session_fork"}})
    assert forks == [{"source_thread_id": "session_kimi", "thread_id": "session_fork"}]


def test_plan_and_usage_updates(tmp_path, monkeypatch):
    driver = _driver(tmp_path, monkeypatch)
    plans = []
    usage = []
    driver.connect("plan-updated", lambda _d, payload: plans.append(payload))
    driver.connect("usage-updated", lambda _d, used, size: usage.append((used, size)))
    driver._on_update({"sessionUpdate": "plan", "entries": [{"content": "Test", "status": "in_progress"}]})
    driver._on_update({"sessionUpdate": "usage_update", "usage": {
        "usedTokens": 100, "size": 1000, "inputTokens": 70, "outputTokens": 30,
    }})
    assert plans[0]["plan"][0]["content"] == "Test"
    assert usage == [(100, 1000)]
    assert driver._turn_usage == {"input_tokens": 70, "output_tokens": 30}


def test_normalize_known_kimi_tools():
    assert _normalize_tool("edit", {"path": "a.py"})[0] == "Edit"
    assert _normalize_tool("bash", {"command": "pwd"})[0] == "Bash"
