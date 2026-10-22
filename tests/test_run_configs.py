"""Run configurations (roadmap Phase 3d)."""
from __future__ import annotations

import json
import socket

from helios.backend import run_configs


def test_reads_claude_launch_json_with_comments_env_and_cwd(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "launch.json").write_text("""{
      // dev server
      "configurations": [
        {"name": "web", "runtimeExecutable": "npm", "runtimeArgs": ["run", "dev"], "port": 5173,
         "cwd": "frontend", "env": {"NODE_ENV": "development", "bad key": "x"}},
        {"name": "no command"},
      ]
    }""")
    [config] = run_configs.load(str(tmp_path))
    assert config.name == "web"
    assert config.command == "NODE_ENV=development npm run dev"
    assert config.cwd == str(tmp_path / "frontend")
    assert config.port == 5173


def test_falls_back_to_package_json_scripts(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite", "test": "x"}}))
    assert [c.command for c in run_configs.load(str(tmp_path))] == ["npm run dev"]
    assert run_configs.load(str(tmp_path / "missing")) == []


def test_port_open_detects_a_listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    try:
        assert run_configs.port_open(port)
    finally:
        server.close()
    assert not run_configs.port_open(port)
