"""One Accounts view: identity per provider and the CLI flow that changes it."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from helios.backend import accounts, google_env, model_catalog


def _write_agy_token(home: Path, email: str) -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    path = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"token": {"access_token": "x"}, "id_token": f"h.{payload}.s"}))


def test_gemini_identity_comes_from_agys_token_without_running_agy(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.read(model_catalog.PROVIDER_GOOGLE).signed_in is False
    _write_agy_token(tmp_path, "person@example.com")
    account = accounts.read(model_catalog.PROVIDER_GOOGLE)
    assert account.signed_in and account.identity == "person@example.com"
    assert google_env.signed_in_email() == "person@example.com"


def test_switch_signs_out_then_in_with_each_providers_own_cli(monkeypatch):
    from helios.backend import codex_env, muse_binary

    monkeypatch.setattr(codex_env, "find_codex_binary", lambda: SimpleNamespace(path="/x/codex"))
    monkeypatch.setattr(muse_binary, "find_muse_binary", lambda: SimpleNamespace(path="/x/muse"))
    openai = accounts.terminal_command(model_catalog.PROVIDER_OPENAI, "switch")
    assert openai.startswith("/x/codex logout; /x/codex login;")
    muse = accounts.terminal_command(model_catalog.PROVIDER_MUSE, "sign-in")
    assert muse.startswith("/x/muse login;")


def test_openrouter_is_a_key_not_a_terminal_sign_in():
    assert model_catalog.PROVIDER_OPENROUTER not in accounts.TERMINAL_PROVIDERS
    ok, message = accounts.launch(model_catalog.PROVIDER_OPENROUTER, "switch")
    assert not ok and "no terminal sign-in" in message


def test_a_failing_provider_reads_as_an_error_not_a_crash(monkeypatch):
    monkeypatch.setitem(accounts._READERS, model_catalog.PROVIDER_MUSE, lambda: 1 / 0)
    account = accounts.read(model_catalog.PROVIDER_MUSE)
    assert not account.signed_in and "division" in account.error
