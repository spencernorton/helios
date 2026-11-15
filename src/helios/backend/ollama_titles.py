"""Small, bounded Ollama and OpenAI-compatible requests for title generation and readiness checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from helios.backend.urlcheck import safe_http_url
from helios.paths import bind_state_paths

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:14b"
MAX_RESPONSE_BYTES = 256 * 1024
_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")

_AUTH_PATH: Path
_self = bind_state_paths(__name__, _AUTH_PATH="title-auth.json")


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


def _resolve_implicit_auth() -> tuple[str | None, str | None]:
    env_key = os.environ.get("HELIOS_TITLE_API_KEY", "").strip()
    env_hdr = os.environ.get("HELIOS_TITLE_API_KEY_HEADER", "").strip() or None
    if env_key:
        return env_key, env_hdr
    auth_path = _self._AUTH_PATH
    try:
        st = auth_path.lstat()
    except OSError:
        return None, None
    if not auth_path.is_file() or auth_path.is_symlink():
        return None, None
    if os.name == "posix" and (st.st_mode & 0o077) != 0:
        raise ValueError("Title auth file must be owner-only (mode 0600).")
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Title auth file is not valid JSON.") from exc
    if not isinstance(raw, dict):
        raise ValueError("Title auth file must contain a JSON object.")
    key = raw.get("api_key")
    header = raw.get("header")
    if key is not None and not isinstance(key, str):
        raise ValueError("Invalid API key.")
    if header is not None and not isinstance(header, str):
        raise ValueError("Invalid API key header.")
    return (key.strip() if isinstance(key, str) and key.strip() else None), (
        header.strip() if isinstance(header, str) and header.strip() else None
    )


def _auth_headers(api_key: str | None, api_key_header: str | None = None) -> dict[str, str]:
    if api_key is None:
        api_key, resolved_header = _resolve_implicit_auth()
        if api_key_header is None:
            api_key_header = resolved_header
    if api_key is None:
        return {}
    if not isinstance(api_key, str):
        raise ValueError("Invalid API key.")
    token = api_key.strip()
    if not token:
        return {}
    if any(char.isspace() or ord(char) < 32 for char in token):
        raise ValueError("API key must not contain whitespace or control characters.")
    header_name = (api_key_header or "Authorization").strip()
    if not _HEADER_NAME_RE.fullmatch(header_name):
        raise ValueError("Invalid API key header.")
    if header_name.lower() == "authorization":
        return {"Authorization": f"Bearer {token}"}
    return {header_name: token}


def _strip_think_blocks(text: str) -> str:
    cleaned = _THINK_CLOSED_RE.sub("", text)
    cleaned = _THINK_UNCLOSED_RE.sub("", cleaned)
    return cleaned.strip()


def _read_json_value(request: urllib.request.Request, *, timeout: float) -> object:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ollama response exceeded the size limit.")
    return json.loads(raw.decode("utf-8"))


def _read_json(request: urllib.request.Request, *, timeout: float) -> dict:
    body = _read_json_value(request, timeout=timeout)
    if not isinstance(body, dict):
        raise ValueError("Ollama returned an unexpected response.")
    return body


def _extract_model_names(rows: list[object], keys: tuple[str, ...]) -> set[str]:
    return {
        value
        for row in rows
        if isinstance(row, dict)
        for key in keys
        if isinstance(value := row.get(key), str)
    }


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
    api_key_header: str | None = None,
) -> ModelReadiness:
    """Check installed models only: never load a model or generate text."""
    url, tag = validate_config(base_url, model)
    headers = _auth_headers(api_key, api_key_header)
    candidates = {tag}
    if ":" not in tag.rsplit("/", 1)[-1]:
        candidates.add(tag + ":latest")

    v1_base = _openai_v1_base(url)
    if v1_base is not None:
        request = urllib.request.Request(v1_base + "/models", headers=headers, method="GET")
        body = _read_json_value(request, timeout=timeout)
        if isinstance(body, dict):
            rows = body.get("data")
            if not isinstance(rows, list):
                raise ValueError("The server did not return an OpenAI model list.")
            names = _extract_model_names(rows, ("id", "name", "model"))
        elif isinstance(body, list):
            names = _extract_model_names(body, ("id", "name", "model"))
            if not (candidates & names):
                cat_req = urllib.request.Request(v1_base + "/catalog", headers=headers, method="GET")
                try:
                    cat_body = _read_json_value(cat_req, timeout=timeout)
                except (OSError, urllib.error.URLError, ValueError):
                    cat_body = None
                if isinstance(cat_body, list):
                    names |= _extract_model_names(cat_body, ("id", "name", "model"))
                elif isinstance(cat_body, dict) and isinstance(cat_body.get("data"), list):
                    names |= _extract_model_names(cat_body["data"], ("id", "name", "model"))
        else:
            raise ValueError("The server did not return an OpenAI model list.")
    else:
        request = urllib.request.Request(url + "/api/tags", headers=headers, method="GET")
        body = _read_json(request, timeout=timeout)
        rows = body.get("models")
        if not isinstance(rows, list):
            raise ValueError("The server did not return an Ollama model list.")
        names = _extract_model_names(rows, ("name", "model"))
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
    api_key_header: str | None = None,
) -> str:
    url, tag = validate_config(base_url, model)
    headers = {"Content-Type": "application/json", **_auth_headers(api_key, api_key_header)}
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
