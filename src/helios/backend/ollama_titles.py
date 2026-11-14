"""Small, bounded Ollama and OpenAI-compatible requests for title generation and readiness checks."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from helios.backend.urlcheck import safe_http_url

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:14b"
MAX_RESPONSE_BYTES = 256 * 1024
_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def validate_config(base_url: str, model: str) -> tuple[str, str]:
    url = safe_http_url(base_url.strip(), "") if isinstance(base_url, str) else ""
    if not url or any(ord(char) < 32 for char in url):
        raise ValueError("Enter a complete http:// or https:// server URL.")
    parsed = urllib.parse.urlsplit(url)
    try:
        valid_host = bool(parsed.hostname) and (parsed.port is None or parsed.port > 0)
    except ValueError:
        valid_host = False
    if not valid_host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a server URL without credentials, query parameters or fragments.")
    tag = model.strip() if isinstance(model, str) else ""
    if not tag or len(tag) > 256 or any(char.isspace() or ord(char) < 32 for char in tag):
        raise ValueError("Enter an Ollama model name, such as qwen3.6:35b.")
    return urllib.parse.urlunsplit(parsed).rstrip("/"), tag


def _openai_v1_base(url: str) -> str | None:
    """Return the `/v1` base URL when `url` targets an OpenAI-compatible endpoint."""
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    if path.endswith("/chat/completions"):
        return url[: -len("/chat/completions")]
    if path == "/v1" or path.endswith("/v1"):
        return url
    return None


def _auth_headers(api_key: str | None) -> dict[str, str]:
    if api_key is None:
        return {}
    if not isinstance(api_key, str):
        raise ValueError("Invalid API key.")
    token = api_key.strip()
    if not token:
        return {}
    if any(char.isspace() or ord(char) < 32 for char in token):
        raise ValueError("API key must not contain whitespace or control characters.")
    return {"Authorization": f"Bearer {token}"}


def _strip_think_blocks(text: str) -> str:
    cleaned = _THINK_CLOSED_RE.sub("", text)
    cleaned = _THINK_UNCLOSED_RE.sub("", cleaned)
    return cleaned.strip()


def _read_json(request: urllib.request.Request, *, timeout: float) -> dict:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ollama response exceeded the size limit.")
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("Ollama returned an unexpected response.")
    return body


@dataclass(frozen=True)
class ModelReadiness:
    available: bool
    model: str


def check_model(
    base_url: str,
    model: str,
    *,
    timeout: float = 5.0,
    api_key: str | None = None,
) -> ModelReadiness:
    """Check installed models only: never load a model or generate text."""
    url, tag = validate_config(base_url, model)
    headers = _auth_headers(api_key)
    v1_base = _openai_v1_base(url)
    if v1_base is not None:
        request = urllib.request.Request(v1_base + "/models", headers=headers, method="GET")
        body = _read_json(request, timeout=timeout)
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ValueError("The server did not return an OpenAI model list.")
        names = {
            value
            for row in rows
            if isinstance(row, dict)
            for key in ("id", "name", "model")
            if isinstance(value := row.get(key), str)
        }
    else:
        request = urllib.request.Request(url + "/api/tags", headers=headers, method="GET")
        body = _read_json(request, timeout=timeout)
        rows = body.get("models")
        if not isinstance(rows, list):
            raise ValueError("The server did not return an Ollama model list.")
        names = {
            value
            for row in rows
            if isinstance(row, dict)
            for key in ("name", "model")
            if isinstance(value := row.get(key), str)
        }
    candidates = {tag}
    if ":" not in tag.rsplit("/", 1)[-1]:
        candidates.add(tag + ":latest")
    return ModelReadiness(bool(candidates & names), tag)


def _extract_openai_title(body: dict) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("Server returned an invalid title response.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("Server returned an invalid title response.")
    content = message.get("content", "")
    if isinstance(content, str):
        return _strip_think_blocks(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                raise ValueError("Server returned an invalid title response.")
        return _strip_think_blocks("".join(parts))
    raise ValueError("Server returned an invalid title response.")


def generate_title(
    base_url: str,
    model: str,
    prompt: str,
    *,
    timeout: float = 30,
    api_key: str | None = None,
) -> str:
    url, tag = validate_config(base_url, model)
    headers = {"Content-Type": "application/json", **_auth_headers(api_key)}
    v1_base = _openai_v1_base(url)
    if v1_base is not None:
        payload = json.dumps({
            "model": tag,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 32,
        }).encode("utf-8")
        request = urllib.request.Request(
            v1_base + "/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        body = _read_json(request, timeout=timeout)
        return _extract_openai_title(body)

    payload = json.dumps({
        "model": tag,
        "prompt": prompt,
        "stream": False,
        # This is a short title, not reasoning work. On thinking-by-default
        # models, hidden reasoning can otherwise consume the entire 32 tokens.
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 32},
    }).encode("utf-8")
    request = urllib.request.Request(
        url + "/api/generate",
        data=payload,
        headers=headers,
        method="POST",
    )
    body = _read_json(request, timeout=timeout)
    text = body.get("response", "")
    if not isinstance(text, str):
        raise ValueError("Ollama returned an invalid title response.")
    return text.strip()
