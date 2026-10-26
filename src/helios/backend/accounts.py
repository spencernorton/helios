"""Every provider sign-in in one place: who it is, and how to change it.

Each provider CLI owns its own credential store; Helios only reads the
identity it reports and opens that CLI's own interactive flow in a terminal
(browser OAuth needs a TTY). Nothing here stores or copies a credential.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from helios.backend import model_catalog
from helios.log import get_logger

_log = get_logger("accounts")

PROVIDERS = (
    model_catalog.PROVIDER_ANTHROPIC,
    model_catalog.PROVIDER_OPENAI,
    model_catalog.PROVIDER_GOOGLE,
    model_catalog.PROVIDER_MUSE,
    model_catalog.PROVIDER_KIMI,
    model_catalog.PROVIDER_OPENROUTER,
)

# OpenRouter is an API key, not a sign-in: its row points at the key field.
TERMINAL_PROVIDERS = PROVIDERS[:-1]

_CLOSE = "; echo; read -rp 'Press Enter to close…'"


@dataclass(frozen=True, slots=True)
class Account:
    provider: str
    signed_in: bool
    identity: str = ""  # email, when the CLI reports one
    detail: str = ""  # plan or sign-in method
    error: str = ""

    @property
    def name(self) -> str:
        return model_catalog.PROVIDER_LABELS.get(self.provider, self.provider)


def read(provider: str) -> Account:
    """Blocking; call off the main loop. Never raises."""
    try:
        return _READERS[provider]()
    except Exception as exc:  # noqa: BLE001 — one provider must not hide the rest
        _log.warning("could not read %s account: %s", provider, exc)
        return Account(provider, False, error=str(exc)[:200])


def _claude() -> Account:
    from helios.backend import claude_env

    s = claude_env.fetch_auth_status()
    if not s.ok:
        return Account(model_catalog.PROVIDER_ANTHROPIC, False, error=s.error)
    plan = (s.subscription_type or "").capitalize()
    return Account(
        model_catalog.PROVIDER_ANTHROPIC,
        s.logged_in,
        s.email,
        " · ".join(b for b in (plan, s.auth_method) if b),
    )


def _openai() -> Account:
    from helios.backend import codex_env

    status = codex_env.fetch_auth_status()
    if not status.ok or not status.logged_in:
        return Account(
            model_catalog.PROVIDER_OPENAI,
            False,
            error="" if status.ok else status.detail,
        )
    try:
        account = codex_env.fetch_account()
    except Exception as exc:  # noqa: BLE001 — identity is a nicety; login is proven
        _log.info("Codex account/read failed: %s", exc)
        account = {}
    plan = str(account.get("planType") or "").capitalize()
    return Account(
        model_catalog.PROVIDER_OPENAI,
        True,
        str(account.get("email") or ""),
        " · ".join(b for b in (plan, "ChatGPT" if status.mode == "chatgpt" else status.detail) if b),
    )


def _google() -> Account:
    from helios.backend import google_env

    # agy's sign-in token is the truth for who is signed in; reading it costs
    # no agy run (a concurrent probe raced the Providers page and read as
    # "not verified").
    email = google_env.signed_in_email()
    return Account(
        model_catalog.PROVIDER_GOOGLE,
        bool(email),
        email,
        "Google account" if email else "",
    )


def _muse() -> Account:
    from helios.backend import muse_env

    s = muse_env.fetch_auth_status(force=True)
    method = {
        "accountLogin": "Meta account",
        "apiKey": "API key",
        "envKey": "META_API_KEY",
    }.get(s.state, s.state)
    return Account(
        model_catalog.PROVIDER_MUSE,
        s.ok and s.logged_in,
        s.label if "@" in (s.label or "") else "",
        method if s.logged_in else "",
        error=s.error,
    )


def _kimi() -> Account:
    from helios.backend import kimi_env

    status = kimi_env.fetch_auth_status(force=True)
    return Account(
        model_catalog.PROVIDER_KIMI,
        status.ok and status.logged_in,
        "",
        "Kimi Code membership" if status.logged_in else "",
        error=status.error,
    )


def _openrouter() -> Account:
    from helios.backend.openrouter import key as or_key

    has_key = bool(or_key.load_key())
    return Account(
        model_catalog.PROVIDER_OPENROUTER,
        has_key,
        "",
        "API key" if has_key else "",
    )


_READERS = {
    model_catalog.PROVIDER_ANTHROPIC: _claude,
    model_catalog.PROVIDER_OPENAI: _openai,
    model_catalog.PROVIDER_GOOGLE: _google,
    model_catalog.PROVIDER_MUSE: _muse,
    model_catalog.PROVIDER_KIMI: _kimi,
    model_catalog.PROVIDER_OPENROUTER: _openrouter,
}


def terminal_command(provider: str, action: str) -> str:
    """Shell text for ``action`` ∈ sign-in / sign-out / switch."""
    if provider == model_catalog.PROVIDER_GOOGLE:
        from helios.backend import google_env

        agy = shlex.quote(str(google_env.find_google_binary().path))
        hint = {
            "sign-in": "Sign in when agy asks, then type /quit.",
            "sign-out": "Type /logout, then /quit.",
            "switch": "Type /logout, then /login and pick the Google account, then /quit.",
        }[action]
        # agy has no login subcommand; its own TUI owns the flow.
        return f"echo {shlex.quote(hint)}; echo; {agy}{_CLOSE}"
    if provider == model_catalog.PROVIDER_KIMI:
        from helios.backend.kimi_binary import find_kimi_binary

        if action != "sign-in":
            raise ValueError("Kimi sign-out uses the ACP credential owner directly.")
        cli = shlex.quote(str(find_kimi_binary().path))
        return f"{cli} login{_CLOSE}"
    if provider == model_catalog.PROVIDER_ANTHROPIC:
        from helios.backend import claude_env

        path = claude_env._claude_path()
        if path is None:
            raise ValueError("No `claude` binary found.")
        cli = shlex.quote(str(path))
        login, logout = f"{cli} auth login --claudeai", f"{cli} auth logout"
    elif provider == model_catalog.PROVIDER_OPENAI:
        from helios.backend import codex_env

        cli = shlex.quote(str(codex_env.find_codex_binary().path))
        login, logout = f"{cli} login", f"{cli} logout"
    elif provider == model_catalog.PROVIDER_MUSE:
        from helios.backend.muse_binary import find_muse_binary

        cli = shlex.quote(str(find_muse_binary().path))
        login, logout = f"{cli} login", f"{cli} logout"
    else:
        raise ValueError(f"{provider} has no terminal sign-in")
    return {
        "sign-in": login,
        "sign-out": logout,
        "switch": f"{logout}; {login}",
    }[action] + _CLOSE


def launch(provider: str, action: str) -> tuple[bool, str]:
    """Open the provider's own flow in a terminal. Returns (ok, message)."""
    from helios.backend.claude_env import _launch_in_terminal

    if provider == model_catalog.PROVIDER_KIMI and action in {"sign-out", "switch"}:
        from helios.backend import kimi_env

        ok, message = kimi_env.logout()
        if not ok or action == "sign-out":
            return ok, message
    try:
        inner = terminal_command(provider, action)
    except Exception as exc:  # noqa: BLE001 — a missing CLI is a message, not a crash
        return False, str(exc)
    return _launch_in_terminal(inner, action)
