"""Run configurations: how to start a project's dev server.

Reads ``.claude/launch.json`` — the file Claude desktop's *Preview* uses —
whose ``configurations`` entries carry ``name``, ``runtimeExecutable``,
``runtimeArgs``, ``port`` and optional ``cwd``/``env``. Without one, a
``package.json`` ``dev`` or ``start`` script is offered.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class RunConfig:
    name: str
    command: str  # shell text typed into a terminal
    cwd: str
    port: int = 0


def _strip_comments(text: str) -> str:
    # launch.json is often JSONC; drop whole-line // comments and trailing commas.
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def load(project_cwd: str) -> list[RunConfig]:
    if not project_cwd or not os.path.isdir(project_cwd):
        return []
    launch = os.path.join(project_cwd, ".claude", "launch.json")
    configs: list[RunConfig] = []
    try:
        with open(launch, encoding="utf-8") as fh:
            data = json.loads(_strip_comments(fh.read()))
    except (OSError, ValueError):
        data = None
    for entry in (data or {}).get("configurations", []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict) or not entry.get("runtimeExecutable"):
            continue
        args = [str(entry["runtimeExecutable"]), *map(str, entry.get("runtimeArgs") or [])]
        env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        prefix = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items() if re.fullmatch(r"[A-Za-z_]\w*", str(k)))
        cwd = os.path.normpath(os.path.join(project_cwd, str(entry.get("cwd") or ".")))
        port = entry.get("port")
        configs.append(RunConfig(
            name=str(entry.get("name") or args[0]),
            command=(prefix + " " if prefix else "") + shlex.join(args),
            cwd=cwd,
            port=port if isinstance(port, int) and 0 < port < 65536 else 0,
        ))
    if configs:
        return configs
    try:
        with open(os.path.join(project_cwd, "package.json"), encoding="utf-8") as fh:
            scripts = json.load(fh).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return []
    return [
        RunConfig(name=f"npm run {name}", command=f"npm run {name}", cwd=project_cwd)
        for name in ("dev", "start") if isinstance(scripts, dict) and name in scripts
    ]


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False
