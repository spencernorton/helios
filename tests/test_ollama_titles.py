"""Ollama and OpenAI-compatible title requests are bounded; readiness never invokes a model."""

import io
import json

import pytest

from helios.backend import ollama_titles as ollama


def respond(monkeypatch, data):
    calls = []

    class Response(io.BytesIO):
        def read(self, size=-1):
            assert size == ollama.MAX_RESPONSE_BYTES + 1
            return super().read(size)

    def open_request(request, *, timeout):
        calls.append((request, timeout))
        return Response(data if isinstance(data, bytes) else json.dumps(data).encode())

    monkeypatch.setattr(ollama.urllib.request, "urlopen", open_request)
    return calls


def test_thinking_model_gets_answer_budget_and_bounded_response(monkeypatch):
    calls = respond(monkeypatch, {"response": "  Inspect scheduler capacity  ", "done": True})
    assert ollama.generate_title("http://server:11434/", "qwen3.6:35b", "A synthetic title") == "Inspect scheduler capacity"
    request, timeout = calls[0]
    payload = json.loads(request.data)
    assert request.full_url == "http://server:11434/api/generate"
    assert request.method == "POST" and timeout == 30
    assert payload["think"] is False
    assert payload["stream"] is False and payload["options"]["num_predict"] == 32


@pytest.mark.parametrize(
    "url,expected_endpoint",
    [
        ("http://localhost:8000/v1", "http://localhost:8000/v1/chat/completions"),
        ("http://localhost:8000/v1/", "http://localhost:8000/v1/chat/completions"),
        ("http://localhost:8000/v1/chat/completions", "http://localhost:8000/v1/chat/completions"),
        ("https://inference.example/api/v1", "https://inference.example/api/v1/chat/completions"),
    ],
)
def test_openai_compatible_chat_completions_strips_think_and_bounds_tokens(monkeypatch, url, expected_endpoint):
    calls = respond(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "<think>drafting a concise title</think>\n  Summarize build cache eviction  ",
                    }
                }
            ]
        },
    )
    title = ollama.generate_title(
        url,
        "qwen2.5-1.5b-instruct",
        "Prompt body",
        api_key="test-local-token",
    )
    assert title == "Summarize build cache eviction"
    request, timeout = calls[0]
    payload = json.loads(request.data)
    assert request.full_url == expected_endpoint
    assert request.method == "POST" and timeout == 30
    assert request.get_header("Authorization") == "Bearer test-local-token"
    assert payload == {
        "model": "qwen2.5-1.5b-instruct",
        "messages": [{"role": "user", "content": "Prompt body"}],
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 32,
    }


def test_openai_compatible_unclosed_think_or_content_blocks(monkeypatch):
    respond(
        monkeypatch,
        {"choices": [{"message": {"content": "<think>ran out of token budget before closing"}}]},
    )
    assert ollama.generate_title("http://localhost:8000/v1", "small-model", "Prompt") == ""

    respond(
        monkeypatch,
        {"choices": [{"message": {"content": [{"type": "text", "text": "  Audit socket permissions  "}]}}]},
    )
    assert ollama.generate_title("http://localhost:8000/v1", "small-model", "Prompt") == "Audit socket permissions"


@pytest.mark.parametrize("model,available", [("qwen3.6:35b", True), ("missing", False), ("gemma4", True)])
def test_readiness_uses_only_get_tags(monkeypatch, model, available):
    calls = respond(monkeypatch, {"models": [{"name": "qwen3.6:35b"}, {"model": "gemma4:latest"}]})
    status = ollama.check_model("http://server:11434", model)
    assert status.available is available
    request, timeout = calls[0]
    assert request.full_url == "http://server:11434/api/tags"
    assert request.method == "GET" and request.data is None and timeout == 5


@pytest.mark.parametrize("model,available", [("qwen2.5-1.5b", True), ("missing", False)])
def test_openai_readiness_uses_only_get_models(monkeypatch, model, available):
    calls = respond(monkeypatch, {"object": "list", "data": [{"id": "qwen2.5-1.5b"}]})
    status = ollama.check_model("http://localhost:8000/v1/", model, api_key="test-local-token")
    assert status.available is available
    request, timeout = calls[0]
    assert request.full_url == "http://localhost:8000/v1/models"
    assert request.method == "GET" and request.data is None and timeout == 5
    assert request.get_header("Authorization") == "Bearer test-local-token"


@pytest.mark.parametrize("url", ["file:///secret", "localhost:11434", "http://host:bad", "http://u:" + "p@host", "http://host?token=x"])
def test_invalid_configuration_makes_no_request(monkeypatch, url):
    calls = respond(monkeypatch, {})
    with pytest.raises(ValueError):
        ollama.check_model(url, "model")
    assert calls == []


@pytest.mark.parametrize("bad_key", ["has space", "line\nbreak", "tab\there"])
def test_invalid_api_key_makes_no_request(monkeypatch, bad_key):
    calls = respond(monkeypatch, {})
    with pytest.raises(ValueError, match="API key"):
        ollama.generate_title("http://localhost:8000/v1", "model", "prompt", api_key=bad_key)
    assert calls == []


def test_oversize_and_malformed_responses_are_rejected(monkeypatch):
    respond(monkeypatch, b"x" * (ollama.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        ollama.check_model("http://server", "model")
    respond(monkeypatch, {"models": "unexpected"})
    with pytest.raises(ValueError, match="model list"):
        ollama.check_model("http://server", "model")
    respond(monkeypatch, {"data": "unexpected"})
    with pytest.raises(ValueError, match="OpenAI model list"):
        ollama.check_model("http://server/v1", "model")
    respond(monkeypatch, {"response": ["not a title"]})
    with pytest.raises(ValueError, match="invalid title"):
        ollama.generate_title("http://server", "model", "prompt")
    respond(monkeypatch, {"choices": []})
    with pytest.raises(ValueError, match="invalid title"):
        ollama.generate_title("http://server/v1", "model", "prompt")
