"""Inspect Kimi's managed subscription through a bounded, tool-free ACP probe."""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from helios.backend.kimi_binary import KimiBinaryNotFound, find_kimi_binary
from helios.backend.process.env_scrub import scrubbed_child_env
from helios.backend.sensitive_text import scrub_sensitive

CLIENT_NAME = "helios"


class KimiAcpError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KimiModel:
    id: str
    label: str
    context_limit: int = 0
    reasoning_efforts: tuple[tuple[str, str], ...] = ()
    default_effort: str = ""
    is_default: bool = False


@dataclass(slots=True)
class KimiAuthStatus:
    logged_in: bool
    state: str = ""
    label: str = ""
    models: tuple[KimiModel, ...] = ()
    catalog_status: str = ""
    usage: dict | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def context_limit_for(model_id: str) -> int:
    name = model_id.rsplit("/", 1)[-1]
    if name.startswith("k3") and name != "k3-256k":
        return 1_048_576
    return 262_144


def acp_query(
    requests: list[tuple[str, dict[str, Any]]], *, binary: Path | None = None,
    cwd: str | None = None, timeout: float = 15.0,
) -> dict[str, Any]:
    resolved = binary or find_kimi_binary().path
    try:
        proc = subprocess.Popen(
            [str(resolved), "acp"], cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=scrubbed_child_env(), bufsize=0, start_new_session=True,
        )
    except OSError as exc:
        raise KimiAcpError("Could not start `kimi acp`.") from exc
    deadline = time.monotonic() + timeout
    sequence = 0
    pending: dict[int, str] = {}
    results: dict[str, Any] = {}

    def send(method: str, params: dict[str, Any] | None, *, notify: bool = False) -> None:
        nonlocal sequence
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            sequence += 1
            message["id"] = sequence
            pending[sequence] = method
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()

    def await_result(method: str) -> Any:
        assert proc.stdout is not None
        while method not in results:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KimiAcpError(f"`kimi acp` timed out waiting for {method}.")
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                raise KimiAcpError(f"`kimi acp` timed out waiting for {method}.")
            line = proc.stdout.readline()
            if not line:
                raise KimiAcpError("`kimi acp` exited during the probe.")
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict) or "id" not in message:
                continue
            name = pending.pop(message["id"], None)
            if name is None:
                continue
            if "error" in message:
                detail = str((message["error"] or {}).get("message") or "request failed")
                raise KimiAcpError(f"`kimi acp` refused {name}: {detail[:300]}")
            results[name] = message.get("result")
        return results[method]

    session_id = ""
    try:
        from helios import __version__ as helios_version

        send("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": CLIENT_NAME, "version": helios_version},
            "clientCapabilities": {},
        })
        initialized = await_result("initialize")
        if not isinstance(initialized, dict) or not initialized.get("agentCapabilities"):
            raise KimiAcpError("The installed Kimi CLI does not provide a compatible ACP interface.")
        for method, params in requests:
            send(method, params)
            await_result(method)
        created = results.get("session/new")
        if isinstance(created, dict):
            session_id = str(created.get("sessionId") or "")
        return results
    finally:
        if session_id:
            try:
                send("session/close", {"sessionId": session_id})
            except OSError:
                pass
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass


def _option(config: Any, category: str) -> dict:
    for row in config if isinstance(config, list) else []:
        if isinstance(row, dict) and row.get("category") == category:
            return row
    return {}


def _parse_models(result: Any) -> tuple[KimiModel, ...]:
    if not isinstance(result, dict):
        return ()
    options = result.get("configOptions")
    model = _option(options, "model")
    thinking = _option(options, "thought_level")
    efforts = tuple(
        (str(row["value"]), str(row.get("name") or row["value"]))
        for row in thinking.get("options") or []
        if isinstance(row, dict) and row.get("value")
    )
    current = str(model.get("currentValue") or "")
    return tuple(
        KimiModel(
            id=str(row["value"]), label=str(row.get("name") or row["value"]),
            context_limit=context_limit_for(str(row["value"])),
            reasoning_efforts=efforts,
            default_effort=str(thinking.get("currentValue") or ""),
            is_default=str(row["value"]) == current,
        )
        for row in model.get("options") or []
        if isinstance(row, dict) and row.get("value")
    )


_cache: KimiAuthStatus | None = None


def cached_auth_status() -> KimiAuthStatus | None:
    return _cache


def fetch_auth_status(*, force: bool = False, timeout: float = 15.0) -> KimiAuthStatus:
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        binary = find_kimi_binary().path
    except KimiBinaryNotFound as exc:
        _cache = KimiAuthStatus(False, error=str(exc), catalog_status="no-binary")
        return _cache
    try:
        results = acp_query([
            ("authenticate", {"methodId": "login"}),
            ("session/new", {"cwd": os.getcwd(), "mcpServers": []}),
        ], binary=binary, timeout=timeout)
    except KimiAcpError as exc:
        detail, _ = scrub_sensitive(str(exc))
        _cache = KimiAuthStatus(False, error=detail[:400], catalog_status="probe-failed")
        return _cache
    models = _parse_models(results.get("session/new"))
    _cache = KimiAuthStatus(
        True, state="managedSubscription", label="Kimi Code membership",
        models=models, catalog_status="kimi-acp" if models else "empty-catalog",
        usage={},
    )
    return _cache


def is_subscription_billing(*, refresh: bool = False) -> bool:
    status = fetch_auth_status(force=refresh)
    return status.ok and status.logged_in and status.state == "managedSubscription"


def launch_login() -> tuple[bool, str]:
    from helios.backend.claude_env import _launch_in_terminal

    try:
        path = find_kimi_binary().path
    except KimiBinaryNotFound as exc:
        return False, str(exc)
    inner = f"{shlex.quote(str(path))} login; echo; read -rp 'Press Enter to close…'"
    return _launch_in_terminal(inner, "sign-in")


def logout() -> tuple[bool, str]:
    global _cache
    try:
        acp_query([("logout", {})])
    except (KimiAcpError, KimiBinaryNotFound) as exc:
        detail, _ = scrub_sensitive(str(exc))
        return False, detail[:300]
    _cache = None
    return True, "Signed out of Kimi."


def list_mcp_servers() -> list[dict[str, Any]]:
    config_path = Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code")) / "config.toml"
    if not config_path.is_file():
        return []
    try:
        import tomllib

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        servers = data.get("mcp_servers") or data.get("mcpServers") or {}
        return [
            {"name": str(name), "command": str(cfg.get("command") or cfg.get("url") or "stdio"),
             "args": list(cfg.get("args") or [])}
            for name, cfg in servers.items() if isinstance(cfg, dict)
        ]
    except (OSError, ValueError, TypeError):
        return []
