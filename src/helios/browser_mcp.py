"""MCP stdio server that lets any provider's agent drive the Helios browser pane.

Launched by the agent CLI with a scrubbed environment. Each ``tools/call`` is
forwarded verbatim to the Helios window over the session bus named by
``--bus``; the window owns every decision (what loads, what needs approval).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from typing import Any, Callable

from helios import APP_ID, __version__
from helios.backend.browser_tools import (
    INSTRUCTIONS,
    INTERFACE,
    MCP_SERVER_NAME,
    OBJECT_PATH,
    TOOL_NAMES,
    TOOLS,
)

#: One JSON-RPC line may not exceed this; a longer one is answered with an error.
_MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _success(request_id: object, result: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: object,
    code: int,
    message: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def serve(
    server: Any,
    *,
    input_stream: io.BufferedIOBase | None = None,
    output_stream: io.BufferedIOBase | None = None,
) -> int:
    """Line-delimited JSON-RPC over stdio: one request in, one response out."""
    source = input_stream or sys.stdin.buffer
    sink = output_stream or sys.stdout.buffer
    while True:
        raw = source.readline(_MAX_REQUEST_BYTES + 1)
        if not raw:
            break
        if len(raw) > _MAX_REQUEST_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = source.readline(_MAX_REQUEST_BYTES + 1)
            response = _error(None, -32600, "Request exceeded size limit")
        else:
            try:
                message = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = _error(None, -32700, "Parse error")
            else:
                response = server.handle(message)
        if response is not None:
            encoded = json.dumps(
                response,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            sink.write(encoded + b"\n")
            sink.flush()
    return 0

_PROTOCOL = "2025-06-18"
# Long enough for a slow page load or for the person to answer an approval.
_CALL_TIMEOUT_MS = 180_000

Caller = Callable[[str, dict], dict]


class BrowserMcpServer:
    def __init__(self, call: Caller, *, token: str) -> None:
        self._call = call
        self._token = token

    def handle(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid JSON-RPC request")
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" not in message:
            return None  # notifications need no answer
        if method == "initialize":
            return _success(request_id, {
                "protocolVersion": _PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": MCP_SERVER_NAME, "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _success(request_id, {})
        if method == "tools/list":
            return _success(request_id, {"tools": TOOLS})
        if method != "tools/call":
            return _error(request_id, -32601, f"Unsupported method: {method}")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOL_NAMES or not isinstance(arguments, dict):
            return _error(request_id, -32602, "Unknown browser tool")
        try:
            result = self._call(name, {**arguments, "_token": self._token})
        except Exception as exc:  # the bus, not the page, failed
            result = {"ok": False, "error": f"Helios is not reachable: {exc}"}
        return _success(request_id, _content(result))


def _content(result: dict) -> dict:
    if not result.get("ok"):
        return {"content": [{"type": "text", "text": str(result.get("error") or "Failed")}], "isError": True}
    png = result.pop("png_base64", None)
    result.pop("ok", None)
    content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
    if png:
        content.append({"type": "image", "data": png, "mimeType": "image/png"})
    return {"content": content, "isError": False}


def dbus_caller(address: str) -> Caller:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    connection = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )

    def call(tool: str, arguments: dict) -> dict:
        reply = connection.call_sync(
            APP_ID, OBJECT_PATH, INTERFACE, "Call",
            GLib.Variant("(ss)", (tool, json.dumps(arguments))),
            GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, _CALL_TIMEOUT_MS, None,
        )
        return json.loads(reply.unpack()[0])

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helios browser pane MCP server")
    parser.add_argument("--bus", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    return serve(BrowserMcpServer(dbus_caller(args.bus), token=args.token))


if __name__ == "__main__":
    raise SystemExit(main())
