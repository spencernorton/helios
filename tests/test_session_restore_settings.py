"""A restored session shows its own model/settings/context immediately.

On fresh boot the model catalog worker probes four providers and can take
seconds. Session selection used to run entirely against the pre-catalog
state: a GPT/Google/Muse session kept the previous provider's model (and
therefore global-default execution settings) until the worker finished,
and the transcript context fill — requested under that wrong model — was
never re-requested after adoption, so the meter stayed wrong until the
user sent another message.

Restoring an EXISTING session now adopts the persisted remembered model
for display immediately (catalog arrival revalidates it; sending stays
gated), while _model_for_provider stays fail-closed for NEW chats.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.backend import model_catalog  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402
from helios.widgets.chat_toolbar import context_window_for_model  # noqa: E402


def _adoption_window(current: str, remembered: dict):
    chosen: list = []
    fake = types.SimpleNamespace(
        _model=current,
        _provider_models=dict(remembered),
        _openai_entries=[],
        _catalog_entries_by_id={},
        _pending_session_provider_adoption="",
    )
    fake._selected_provider = types.MethodType(MainWindow._selected_provider, fake)
    fake._model_for_provider = types.MethodType(MainWindow._model_for_provider, fake)
    fake._adopt_session_provider = types.MethodType(
        MainWindow._adopt_session_provider, fake
    )

    def apply(alias, *, quiet=False):
        fake._model = alias
        chosen.append(alias)

    fake._apply_model_choice = apply
    fake.chosen = chosen
    return fake


def test_gpt_session_shows_remembered_model_before_catalog_lands():
    fake = _adoption_window(
        "opus", {model_catalog.PROVIDER_OPENAI: "gpt-restore-1"}
    )

    fake._adopt_session_provider(model_catalog.PROVIDER_OPENAI)

    assert fake._model == "gpt-restore-1", (
        "the restored GPT session sat on the Claude model until the "
        "catalog worker finished seconds later"
    )
    assert fake._pending_session_provider_adoption == model_catalog.PROVIDER_OPENAI, (
        "display adoption is provisional: catalog arrival must revalidate"
    )


def test_muse_session_shows_remembered_model_before_catalog_lands():
    fake = _adoption_window(
        "gemini-x", {model_catalog.PROVIDER_MUSE: "muse-restore-1"}
    )

    fake._adopt_session_provider(model_catalog.PROVIDER_MUSE)

    assert fake._model == "muse-restore-1"
    assert fake._pending_session_provider_adoption == model_catalog.PROVIDER_MUSE


def test_pending_adoption_revalidates_against_the_arrived_catalog():
    fake = _adoption_window(
        "opus", {model_catalog.PROVIDER_OPENAI: "gpt-retired"}
    )
    fake._adopt_session_provider(model_catalog.PROVIDER_OPENAI)
    assert fake._model == "gpt-retired"

    entry = model_catalog.ModelEntry(
        "gpt-current", "GPT Current", "OpenAI",
        provider=model_catalog.PROVIDER_OPENAI,
    )
    fake._openai_entries = [entry]
    fake._catalog_entries_by_id = {entry.id: entry}
    fake._adopt_session_provider(model_catalog.PROVIDER_OPENAI)

    assert fake._model == "gpt-current", (
        "a remembered model the arrived catalog does not prove must fall "
        "forward to the catalog default, not stay on screen"
    )
    assert fake._pending_session_provider_adoption == ""


def test_context_fill_is_sized_with_the_model_selected_when_it_lands():
    """The fill was requested under the pre-adoption model; its window must
    be derived from the model on screen at apply time."""
    usage: list = []
    fake = types.SimpleNamespace(
        _destroyed=False,
        _ctx_fill_sid="s1",
        _driver=None,
        _model="muse-wonder-1.0",
        _chat_toolbar=types.SimpleNamespace(
            set_context_usage=lambda u, t: usage.append((u, t))
        ),
    )

    MainWindow._apply_context_fill(fake, "s1", 5_000, "opus")

    assert usage == [(5_000, context_window_for_model("muse-wonder-1.0"))]
    assert usage[0][1] != context_window_for_model("opus")


class _Toggle:
    def __init__(self):
        self.sensitive = None
        self.tooltip = ""

    def set_sensitive(self, value):
        self.sensitive = value

    def set_tooltip_text(self, text):
        self.tooltip = text


def test_catalog_arrival_re_requests_the_viewed_sessions_context_fill():
    muse_entry = model_catalog.ModelEntry(
        "muse-restore-1", "Muse Restore", "Muse",
        provider=model_catalog.PROVIDER_MUSE,
    )
    session = types.SimpleNamespace(session_id="muse-session-1")
    refills: list = []
    fake = types.SimpleNamespace(
        _catalog_refresh_running=True,
        _catalog_force_pending=False,
        _destroyed=False,
        _driver=None,
        _model="gemini-x",
        _provider_models={},
        _catalog_entries_by_id={},
        _openai_entries=[],
        _pending_session_provider_adoption=model_catalog.PROVIDER_MUSE,
        _next_chat=types.SimpleNamespace(
            project=types.SimpleNamespace(cwd="/repo", read_only=False),
            resume_id="muse-session-1",
            resume_provider=model_catalog.PROVIDER_MUSE,
        ),
        _chat_toolbar=types.SimpleNamespace(set_choices=lambda _e: None),
        _gpt_toggle=_Toggle(),
        _ui_state=types.SimpleNamespace(set=lambda *_a: None),
        _ctx_fill_session=session,
        _ctx_fill_visible=True,
        _load_context_fill_async=lambda s: refills.append(s),
        _normalize_openai_model_memory=lambda: None,
        _sync_effort_sensitivity=lambda: None,
        _sync_execution_control=lambda: None,
        _sync_new_chat_actions=lambda: None,
    )
    fake._selected_provider = types.MethodType(MainWindow._selected_provider, fake)
    fake._model_for_provider = types.MethodType(MainWindow._model_for_provider, fake)
    fake._adopt_session_provider = types.MethodType(
        MainWindow._adopt_session_provider, fake
    )
    fake._apply_model_choice = lambda alias, quiet=False: setattr(
        fake, "_model", alias
    )

    types.MethodType(MainWindow._apply_model_catalog, fake)([muse_entry], False)

    assert refills == [session], (
        "without this, the restored meter kept the pre-catalog window "
        "until the user sent another message"
    )
