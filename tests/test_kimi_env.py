from __future__ import annotations

import json

from helios.backend import kimi_env, model_catalog


def _session_result():
    return {
        "sessionId": "session_kimi",
        "configOptions": [
            {"category": "model", "currentValue": "kimi-code/k3", "options": [
                {"value": "kimi-code/k3", "name": "K3"},
                {"value": "kimi-code/k3-256k", "name": "K3 256K"},
                {"value": "kimi-code/kimi-for-coding", "name": "Code"},
            ]},
            {"category": "thought_level", "currentValue": "high", "options": [
                {"value": "low", "name": "Low"}, {"value": "high", "name": "High"},
                {"value": "max", "name": "Max"},
            ]},
        ],
    }


def test_parse_live_acp_config_into_models():
    models = kimi_env._parse_models(_session_result())
    assert [m.id for m in models] == ["kimi-code/k3", "kimi-code/k3-256k", "kimi-code/kimi-for-coding"]
    assert models[0].is_default and models[0].default_effort == "high"
    assert models[0].context_limit == 1_048_576
    assert models[1].context_limit == 262_144
    assert models[0].reasoning_efforts == (("low", "Low"), ("high", "High"), ("max", "Max"))


def test_catalog_only_accepts_managed_acp_auth():
    auth = kimi_env.KimiAuthStatus(True, state="managedSubscription",
                                   models=kimi_env._parse_models(_session_result()),
                                   catalog_status="kimi-acp")
    entries, status = model_catalog.kimi_entries(auth=auth)
    assert status == "kimi-acp"
    assert model_catalog.preferred_kimi_model(entries) == "kimi-code/k3"
    assert entries[0].provider == model_catalog.PROVIDER_KIMI
    assert model_catalog.provider_for(entries[0].id) == model_catalog.PROVIDER_KIMI
    assert model_catalog.provider_for("moonshotai/kimi-k2") == model_catalog.PROVIDER_OPENROUTER


def test_mcp_inventory_uses_kimi_home_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.estate]\ncommand = "helios-estate-mcp"\nargs = ["--server", "intel"]\n'
    )
    assert kimi_env.list_mcp_servers() == [{
        "name": "estate", "command": "helios-estate-mcp", "args": ["--server", "intel"],
    }]
    assert "api_key" not in json.dumps(kimi_env.list_mcp_servers())
