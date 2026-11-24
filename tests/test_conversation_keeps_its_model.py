"""A conversation opens on its own model, not the provider's last pick.

Selecting a session only told the window which provider produced it, so the
header showed the model last used with that provider anywhere. The next send
then read the header as a picker change and switched the conversation: using
Fable in one chat moved a live Opus chat to Fable on its next message.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.backend import model_catalog  # noqa: E402
from helios.backend.model_catalog import ModelEntry  # noqa: E402
from helios.backend.work_coordinator import WorkCoordinator  # noqa: E402
from helios.backend.work_store import WorkStore  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402
from helios.window_models import _PendingModelSwitch  # noqa: E402

from test_composer_drafts_window import _session, _window  # noqa: E402

ANTHROPIC = model_catalog.PROVIDER_ANTHROPIC
GOOGLE = model_catalog.PROVIDER_GOOGLE


def _header(model: str, **extra) -> types.SimpleNamespace:
    """Just what the restore reads, with the header's model recorded."""
    fields = {
        "_model": model,
        "_work_coordinator": None,
        "_pending_model_switch": None,
        "_catalog_entries_by_id": {},
        "applied": [],
    }
    fields.update(extra)
    win = types.SimpleNamespace(**fields)

    def apply(alias, *, quiet=False):
        assert quiet, "restoring a conversation's model must never toast or switch"
        win._model = alias
        win.applied.append(alias)

    win._apply_model_choice = apply
    return win


def _live(requested: str = "", model: str = "", work_id: str = "") -> types.SimpleNamespace:
    driver = types.SimpleNamespace(model=model, _helios_work_id=work_id)
    if requested:
        driver._helios_requested_model = requested
    return driver


@pytest.fixture
def coordinator(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        yield WorkCoordinator(store)
    finally:
        store.close()


def test_live_conversation_keeps_the_model_its_driver_runs():
    win = _header("fable[1m]")
    live = _live(requested="opus[1m]", model="claude-opus-5-5")

    assert MainWindow._restore_conversation_model(win, ANTHROPIC, "s1", live)

    # The alias Helios asked for, not the pinned id the CLI resolved it to.
    assert win._model == "opus[1m]"
    # ...so the send-time check no longer sees a picker change to apply.
    idle = types.SimpleNamespace(
        is_busy=False, model="claude-opus-5-5", _helios_requested_model="opus[1m]",
    )
    win._model_changes = {}
    assert not MainWindow._driver_model_is_stale(win, idle)


def test_live_driver_without_a_requested_model_reports_its_own():
    win = _header("fable[1m]")
    assert MainWindow._restore_conversation_model(
        win, ANTHROPIC, "s1", _live(model="claude-opus-4-8"),
    )
    assert win._model == "claude-opus-4-8"


def test_matching_header_is_left_alone():
    win = _header("opus[1m]")
    assert not MainWindow._restore_conversation_model(
        win, ANTHROPIC, "s1", _live(requested="opus[1m]"),
    )
    assert win.applied == []


def test_cold_conversation_reopens_on_its_recorded_model(coordinator):
    coordinator.resolve_native_work(ANTHROPIC, "s1", cwd="/repo", model="opus[1m]")
    win = _header("fable[1m]", _work_coordinator=coordinator)

    assert MainWindow._restore_conversation_model(win, ANTHROPIC, "s1")
    assert win._model == "opus[1m]"


def test_unknown_conversation_falls_back_to_the_adopted_model(coordinator):
    win = _header("fable[1m]", _work_coordinator=coordinator)
    assert not MainWindow._restore_conversation_model(win, ANTHROPIC, "never-seen")
    assert win._model == "fable[1m]"


def test_cold_non_claude_model_must_still_be_offered(coordinator):
    coordinator.resolve_native_work(GOOGLE, "g1", cwd="/repo", model="gemini-4-argon")
    win = _header("gemini-4-neon", _work_coordinator=coordinator)

    # Not in the catalog (retired, or the catalog has not landed): stay put.
    assert not MainWindow._restore_conversation_model(win, GOOGLE, "g1")
    assert win._model == "gemini-4-neon"

    win._catalog_entries_by_id = {
        "gemini-4-argon": ModelEntry("gemini-4-argon", "Gemini 4 Argon", "Google", GOOGLE),
    }
    assert MainWindow._restore_conversation_model(win, GOOGLE, "g1")
    assert win._model == "gemini-4-argon"


def _viewing(win, session_id: str) -> None:
    win._driver = None
    win._next_chat = types.SimpleNamespace(
        resume_id=session_id, project=types.SimpleNamespace(read_only=False),
    )


def test_a_model_waiting_on_its_catalog_is_restored_when_it_lands(coordinator):
    # Fresh boot: a cold Argon conversation is opened while the header already
    # sits on Gemini (no provider adoption pending) and no catalog exists.
    coordinator.resolve_native_work(GOOGLE, "g1", cwd="/repo", model="gemini-4-argon")
    win = _header("gemini-4-neon", _work_coordinator=coordinator)
    _viewing(win, "g1")

    assert not MainWindow._restore_conversation_model(win, GOOGLE, "g1")
    assert win._deferred_conversation_model == (GOOGLE, "g1")
    # Still owed while the catalog is missing: the send path waits on this.
    assert MainWindow._retry_deferred_conversation_model(win)
    assert win._model == "gemini-4-neon"

    win._catalog_entries_by_id = {
        "gemini-4-neon": ModelEntry("gemini-4-neon", "Gemini 4 Neon", "Google", GOOGLE),
        "gemini-4-argon": ModelEntry("gemini-4-argon", "Gemini 4 Argon", "Google", GOOGLE),
    }
    assert not MainWindow._retry_deferred_conversation_model(win)
    assert win._model == "gemini-4-argon"
    assert win._deferred_conversation_model is None


def test_a_retired_model_is_not_left_waiting(coordinator):
    coordinator.resolve_native_work(GOOGLE, "g1", cwd="/repo", model="gemini-4-argon")
    win = _header("gemini-4-neon", _work_coordinator=coordinator)
    # Gemini's catalog is here and no longer offers Argon: nothing to wait for.
    win._catalog_entries_by_id = {
        "gemini-4-neon": ModelEntry("gemini-4-neon", "Gemini 4 Neon", "Google", GOOGLE),
    }
    assert not MainWindow._restore_conversation_model(win, GOOGLE, "g1")
    assert win._deferred_conversation_model is None


@pytest.mark.parametrize("moved_on", ["other-session", "driver-started"])
def test_a_deferred_restore_never_follows_you_elsewhere(coordinator, moved_on):
    coordinator.resolve_native_work(GOOGLE, "g1", cwd="/repo", model="gemini-4-argon")
    win = _header("gemini-4-neon", _work_coordinator=coordinator)
    _viewing(win, "g1")
    MainWindow._restore_conversation_model(win, GOOGLE, "g1")
    win._catalog_entries_by_id = {
        "gemini-4-argon": ModelEntry("gemini-4-argon", "Gemini 4 Argon", "Google", GOOGLE),
    }
    if moved_on == "other-session":
        _viewing(win, "g2")
    else:
        win._driver = object()  # a message already went out under Neon

    assert not MainWindow._retry_deferred_conversation_model(win)
    assert win._model == "gemini-4-neon"
    assert win._deferred_conversation_model is None


def test_a_queued_switch_repairs_the_header_before_it_sends(monkeypatch):
    """Reselecting the source session mid-turn put the header back on the
    source provider; the queued texts were then sent under it."""
    seen: list[str] = []
    win = _header("opus[1m]")
    win._destroyed = False
    win._pending_model_switch = _PendingModelSwitch(
        work_id="wrk_1",
        from_provider=ANTHROPIC, from_model="opus[1m]",
        to_provider=model_catalog.PROVIDER_OPENAI, to_model="gpt-6.2-sol",
    )
    win._work_is_executing = lambda _wid: False
    win._drv_is_current = lambda _drv: True
    monkeypatch.setattr(
        MainWindow, "_drain_pending_model_switch_texts", lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        MainWindow, "_execute_in_place_model_switch",
        lambda self, **kw: seen.append(self._model) or True,
    )
    driver = types.SimpleNamespace(session_id="s1", _helios_work_id="wrk_1")

    assert MainWindow._complete_pending_model_switch_if_ready(
        win, driver, allow_auto_send=True,
    )
    assert seen == ["gpt-6.2-sol"]


def test_a_switch_queued_for_this_conversation_owns_the_header(coordinator):
    work = coordinator.resolve_native_work(ANTHROPIC, "s1", cwd="/repo", model="opus[1m]")
    win = _header("fable[1m]", _work_coordinator=coordinator)
    win._pending_model_switch = _PendingModelSwitch(
        work_id=work.work_id,
        from_provider=ANTHROPIC, from_model="opus[1m]",
        to_provider=ANTHROPIC, to_model="fable[1m]",
    )

    assert not MainWindow._restore_conversation_model(win, ANTHROPIC, "s1")
    assert win._model == "fable[1m]"

    # A switch queued for some other conversation does not hold this one.
    win._pending_model_switch = _PendingModelSwitch(
        work_id="wrk_other",
        from_provider=ANTHROPIC, from_model="opus[1m]",
        to_provider=ANTHROPIC, to_model="fable[1m]",
    )
    assert MainWindow._restore_conversation_model(win, ANTHROPIC, "s1")
    assert win._model == "opus[1m]"


def test_a_record_for_another_provider_is_ignored():
    win = _header("fable[1m]")
    assert not MainWindow._restore_conversation_model(
        win, ANTHROPIC, "s1", _live(requested="gpt-6.2-sol"),
    )
    assert win._model == "fable[1m]"


def test_selecting_a_session_restores_its_model_and_keeps_the_record(tmp_path):
    """The reported sequence, through the real selection handler."""
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        opus_chat = _session(tmp_path, "opus-chat", "/repo")
        # The conversation was started on Opus...
        work = coordinator.resolve_native_work(
            ANTHROPIC, opus_chat.session_id, cwd="/repo", model="opus[1m]",
        )
        # ...then another Claude chat used Fable, which is what the header
        # still shows (same provider, so adoption changes nothing).
        win = _window()
        win._work_coordinator = coordinator
        win._model = "fable[1m]"
        win._pending_model_switch = None
        win._catalog_entries_by_id = {}

        def apply(alias, *, quiet=False):
            win._model = alias

        win._apply_model_choice = apply

        MainWindow._on_session_selected(win, None, opus_chat)

        assert win._model == "opus[1m]"
        binding = store.active_binding_for_work_provider(work.work_id, ANTHROPIC)
        assert binding.model == "opus[1m]"
        participant = coordinator.participant(work.work_id, ANTHROPIC)
        assert participant.metadata.get("model") == "opus[1m]", (
            "selection recorded the header's Fable against the Opus conversation"
        )
    finally:
        store.close()
