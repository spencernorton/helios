"""Model catalog discovery, provider selection, OpenRouter credit refresh, and
in-place model/provider switching, split out of main_window.

Behaviour-preserving mixin: every method runs on the composed MainWindow
instance so driver-manager, WorkCoordinator, and test monkeypatches on
``helios.main_window`` resolve identically while isolating the model-catalog
and model-switch cluster into one auditable module.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from helios.backend import model_catalog, session_goals
from helios.backend.composer_state import draft_key
from helios.backend.composite_transcript import HandoffBoundary, human_model_label
from helios.backend.session_router import switch_work_participant
from helios.backend.transcript import parse_transcript
from helios.backend.ui_state import DEFAULT_MODEL_KEY, prune_hidden_models
from helios.backend.work_coordinator import tag_driver
from helios.backend.workflow_modes import DEFAULT_WORKFLOW_MODE
from helios.log import get_logger
from helios.widgets.chat_toolbar import DEFAULT_MODEL

_log = get_logger("window")

# OpenRouter credit lookups are cheap but not free; a turn can finish every
# few seconds. Spend moves slowly enough that a minute is plenty.
_OR_CREDITS_MIN_INTERVAL = 60.0


def _mw():
    from helios import main_window

    return main_window


@dataclass
class _PendingModelSwitch:
    """Mid-turn model/provider switch queued to apply on turn completion."""

    work_id: str
    from_provider: str
    from_model: str
    to_provider: str
    to_model: str
    source_driver: object = None
    queued_items: list[tuple[int, str]] = field(default_factory=list)
    stop_requested: bool = False


class WindowModelsMixin:
    """Model catalog, provider memory/toggles, and in-place model switching."""

    def _refresh_model_catalog(self, *, force: bool = False) -> None:
        """Discover models off the main loop and swap them into the picker."""
        if force:
            # A credential mutation invalidates the prior process/account proof
            # immediately, not only after its replacement worker completes.
            self._openai_catalog_authoritative = False
            self._openai_entries = []
            self._openai_workflow_modes = (DEFAULT_WORKFLOW_MODE,)
            self._gpt_toggle.set_sensitive(False)
            self._gpt_toggle.set_tooltip_text(
                "Checking OpenAI model availability…"
            )
            self._sync_new_chat_actions()
        if self._catalog_refresh_running:
            if force:
                # A credential change must supersede a startup/periodic result
                # that may belong to the previous account. Coalesce repeated
                # requests, discard that worker's result in _apply, then rerun.
                self._catalog_force_pending = True
            return
        self._catalog_refresh_running = True

        def work() -> None:
            # Warm required Claude capability probes on this background thread
            # so the first send never blocks the GTK main loop on CLI probes
            # (cli_driver reads the cached results during argv construction).
            try:
                from helios.backend.claude_binary import capability_report

                report = capability_report()
                _log.info(
                    "claude CLI %s; degraded: %s",
                    report.get("version") or "unknown version",
                    ", ".join(report.get("degraded") or []) or "nothing",
                )
                GLib.idle_add(self._report_cli_degradation, "Claude", report)
            except Exception:
                pass
            try:
                entries = model_catalog.anthropic_entries(force=force)
            except Exception as e:  # noqa: BLE001 — retain a safe picker baseline
                _log.warning("Anthropic model catalog refresh failed: %s", e)
                entries = list(model_catalog.FALLBACK_ANTHROPIC)
            openai_authoritative = False
            openai_workflow_modes = (DEFAULT_WORKFLOW_MODE,)
            try:
                openai, status = model_catalog.openai_entries(force=force)
                openai_authoritative = status == "app-server"
                if openai_authoritative:
                    openai_workflow_modes = model_catalog.openai_workflow_modes()
                if openai and openai_authoritative:
                    entries = entries + openai
                _log.info(
                    "model catalog: %d entries (openai: %s)", len(entries), status
                )
            except Exception as e:  # noqa: BLE001 — discovery must never crash the app
                # Keep the Anthropic baseline non-empty so applying this result
                # also removes stale GPT rows from a previous credential.
                _log.warning("OpenAI model catalog refresh failed: %s", e)
            # Background-only capability refresh, including legacy caches
            # without per-model reasoning metadata. UI/send paths stay cached.
            try:
                or_entries, or_status = model_catalog.openrouter_entries(force=force, refresh_if_stale=True)
                if or_entries:
                    entries = entries + or_entries
                _log.info(
                    "model catalog: %d entries (openrouter: %s)",
                    len(entries),
                    or_status,
                )
            except Exception as e:  # noqa: BLE001
                _log.warning("OpenRouter model catalog refresh failed: %s", e)
            # Google / Gemini subscription catalog
            try:
                g_entries, g_status = model_catalog.google_entries(force=force)
                if g_entries:
                    entries = entries + g_entries
                _log.info(
                    "model catalog: %d entries (google: %s)",
                    len(entries),
                    g_status,
                )
            except Exception as e:  # noqa: BLE001
                _log.warning("Google model catalog refresh failed: %s", e)
            # Muse (Meta) native catalog. Only live-proven rows enter the
            # picker: without a working `muse serve` probe there is nothing
            # honest to select, exactly like the OpenAI app-server rule.
            try:
                m_entries, m_status = model_catalog.muse_entries(force=force)
                if m_entries and m_status == "muse-serve":
                    entries = entries + m_entries
                _log.info(
                    "model catalog: %d entries (muse: %s)",
                    len(entries),
                    m_status,
                )
            except Exception as e:  # noqa: BLE001
                _log.warning("Muse model catalog refresh failed: %s", e)
            try:
                k_entries, k_status = model_catalog.kimi_entries(force=force)
                if k_entries and k_status == "kimi-acp":
                    entries = entries + k_entries
                _log.info("model catalog: %d entries (kimi: %s)", len(entries), k_status)
            except Exception as e:  # noqa: BLE001
                _log.warning("Kimi model catalog refresh failed: %s", e)
            GLib.idle_add(
                self._apply_model_catalog,
                entries,
                openai_authoritative,
                openai_workflow_modes,
            )

        threading.Thread(target=work, daemon=True, name="model-catalog").start()

    def _apply_model_catalog(
        self,
        entries: list,
        openai_authoritative: bool,
        openai_workflow_modes: tuple[str, ...] = (DEFAULT_WORKFLOW_MODE,),
    ) -> bool:
        self._catalog_refresh_running = False
        force_pending = self._catalog_force_pending
        self._catalog_force_pending = False
        if self._destroyed:
            return False
        if force_pending:
            # Do not briefly publish a result discovered before the credential
            # change. The replacement worker starts only after this one has
            # fully yielded its slot, so at most one refresh remains active.
            self._refresh_model_catalog(force=True)
            return False
        # The worker normally supplies this baseline itself. Retain the same
        # fail-closed behavior for a malformed/empty callback payload so stale
        # GPT choices can never survive an unsuccessful refresh.
        if not entries:
            entries = list(model_catalog.FALLBACK_ANTHROPIC)
        self._openai_catalog_authoritative = bool(openai_authoritative)
        self._openai_workflow_modes = (
            tuple(openai_workflow_modes)
            if self._openai_catalog_authoritative
            else (DEFAULT_WORKFLOW_MODE,)
        )
        self._openai_entries = (
            [
                entry
                for entry in entries
                if getattr(entry, "provider", "")
                == model_catalog.PROVIDER_OPENAI
            ]
            if self._openai_catalog_authoritative
            else []
        )
        selectable = [
            entry
            for entry in entries
            if (
                getattr(entry, "provider", model_catalog.PROVIDER_ANTHROPIC)
                != model_catalog.PROVIDER_OPENAI
                or entry in self._openai_entries
            )
        ]
        selectable = model_catalog.dedupe_entries(selectable)
        self._chat_toolbar.set_choices(selectable)
        self._catalog_entries_by_id = {entry.id: entry for entry in selectable}
        _mw().MainWindow._prune_obsolete_hidden_models(self, selectable)
        self._normalize_openai_model_memory()
        pending_provider = str(
            getattr(self, "_pending_session_provider_adoption", "") or ""
        )
        if pending_provider and getattr(self, "_driver", None) is None:
            target = getattr(self, "_next_chat", None)
            target_provider = str(
                getattr(target, "resume_provider", "") or ""
            )
            if (
                target is not None
                and getattr(target, "resume_id", "")
                and not getattr(getattr(target, "project", None), "read_only", False)
                and (not target_provider or target_provider == pending_provider)
            ):
                self._pending_session_provider_adoption = ""
                adopt = getattr(self, "_adopt_session_provider", None)
                if callable(adopt):
                    adopt(pending_provider)
                    # The conversation's own model could not be checked
                    # before this catalog existed.
                    _mw().MainWindow._restore_conversation_model(
                        self, pending_provider, str(target.resume_id)
                    )
        # A conversation opened on the provider already on screen leaves no
        # pending adoption, yet may still be owed its own model.
        _mw().MainWindow._retry_deferred_conversation_model(self)
        self._sync_effort_sensitivity()
        self._sync_execution_control()
        # The restored session's context fill was requested at selection
        # time — on fresh boot, under the previous provider's model, before
        # this catalog existed. Re-request it under the adopted model so
        # the meter settles here instead of on the next sent message. Only
        # for a viewed, driver-less session: a live driver owns its meter.
        fill_session = getattr(self, "_ctx_fill_session", None)
        reload_fill = getattr(self, "_load_context_fill_async", None)
        if (
            fill_session is not None
            and getattr(self, "_ctx_fill_visible", False)
            and getattr(self, "_driver", None) is None
            and callable(reload_fill)
        ):
            reload_fill(fill_session)
        available = (
            self._openai_catalog_authoritative
            and bool(self._openai_entries)
        )
        self._gpt_toggle.set_sensitive(available)
        self._gpt_toggle.set_tooltip_text(
            "Chat with GPT (OpenAI)" if available
            else "Add an OpenAI key in Settings → Providers to enable GPT"
        )
        if hasattr(self, "_sync_openrouter_toggle_sensitivity"):
            self._sync_openrouter_toggle_sensitivity()
        self._sync_new_chat_actions()
        return False  # one-shot idle

    def _prune_obsolete_hidden_models(self, selectable: list) -> None:
        """Prune obsolete hidden_models entries only for providers with live catalogs."""
        by_provider: dict[str, set[str]] = {}
        for entry in selectable:
            prov = getattr(entry, "provider", model_catalog.PROVIDER_ANTHROPIC)
            by_provider.setdefault(prov, set()).add(entry.id)
        if not self._openai_catalog_authoritative:
            by_provider.pop(model_catalog.PROVIDER_OPENAI, None)
        # Do not prune Anthropic entries when only the static fallback list loaded.
        fallback_ids = {e.id for e in model_catalog.FALLBACK_ANTHROPIC}
        anth_ids = by_provider.get(model_catalog.PROVIDER_ANTHROPIC, set())
        if anth_ids and anth_ids == fallback_ids:
            by_provider.pop(model_catalog.PROVIDER_ANTHROPIC, None)
        if by_provider:
            try:
                prune_hidden_models(by_provider, state=self._ui_state)
            except Exception as exc:
                _log.warning("could not prune obsolete hidden models: %s", exc)

    def _refresh_openrouter_credits(self, *, throttle: bool = False) -> None:
        """Populate the usage panel's OpenRouter row from the saved key.

        OpenRouter has no rolling window to report, so without this the panel
        stayed empty until a 429 happened to fire. Only fetched while an
        OpenRouter model is selected — the popover discards rows from a
        provider other than the one on screen anyway.

        ``throttle=True`` is for the per-turn refresh: spend only changes when
        a turn completes, but turns can be seconds apart, and the usage meter
        now updates several times per turn. One lookup a minute keeps the
        balance honest without a request per turn.
        """
        if model_catalog.provider_for(self._model) != model_catalog.PROVIDER_OPENROUTER:
            return
        now = time.monotonic()
        if throttle and (now - self._or_credits_checked) < _OR_CREDITS_MIN_INTERVAL:
            return
        self._or_credits_checked = now

        def work() -> None:
            from helios.backend.openrouter import account

            try:
                row = account.fetch_credit_row()
            except Exception as exc:  # noqa: BLE001 — a status panel is not critical
                _log.warning("OpenRouter credit lookup failed: %s", exc)
                return
            if row:
                GLib.idle_add(self._apply_openrouter_credits, row)

        threading.Thread(
            target=work, daemon=True, name="openrouter-credits"
        ).start()

    def _apply_openrouter_credits(self, row: dict) -> bool:
        if self._destroyed:
            return False
        if model_catalog.provider_for(self._model) == model_catalog.PROVIDER_OPENROUTER:
            self._chat_toolbar.update_rate_limit(row)
        return False

    def _normalize_openai_model_memory(self) -> None:
        """Migrate stale remembered GPT models to the agent-ranked default.

        Older Helios builds allowed generic `*-chat-latest` ids into the GPT
        side. If one is still persisted, the header toggle should fall forward
        to the first Codex/agent model instead of recreating a chatbot session.
        """
        preferred = model_catalog.preferred_openai_model(self._openai_entries)
        if not preferred:
            # A mode-only login cannot prove the active credential identity.
            # Never retain a model from the previous account when the current
            # authoritative catalog has no GPT rows.
            self._provider_models[model_catalog.PROVIDER_OPENAI] = ""
            self._ui_state.set(f"model_{model_catalog.PROVIDER_OPENAI}", "")

            if model_catalog.provider_for(self._model) == model_catalog.PROVIDER_OPENAI:
                anthropic_entries = [
                    entry
                    for entry in self._catalog_entries_by_id.values()
                    if getattr(entry, "provider", model_catalog.PROVIDER_ANTHROPIC)
                    == model_catalog.PROVIDER_ANTHROPIC
                ]
                valid_anthropic = {entry.id for entry in anthropic_entries}
                remembered = self._provider_models.get(
                    model_catalog.PROVIDER_ANTHROPIC
                )
                candidates = (remembered, DEFAULT_MODEL, "")
                fallback = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate is not None and candidate in valid_anthropic
                    ),
                    anthropic_entries[0].id if anthropic_entries else "",
                )
                # Quietly switch the next-send backend without tearing down a
                # live/background historical GPT driver. Viewing stays intact;
                # a subsequent send stages the safe Claude Work participant.
                self._apply_model_choice(fallback, quiet=True)
            return
        valid = {e.id for e in self._openai_entries}
        remembered = self._provider_models.get(model_catalog.PROVIDER_OPENAI, "")
        if remembered not in valid:
            self._provider_models[model_catalog.PROVIDER_OPENAI] = preferred
            self._ui_state.set(f"model_{model_catalog.PROVIDER_OPENAI}", preferred)
        if (
            model_catalog.provider_for(self._model) == model_catalog.PROVIDER_OPENAI
            and self._model not in valid
        ):
            self._model = preferred
            self._ui_state.set("model", preferred)
            self._provider_models[model_catalog.PROVIDER_OPENAI] = preferred
            self._ui_state.set(f"model_{model_catalog.PROVIDER_OPENAI}", preferred)
            self._chat_toolbar.set_model(preferred)
            self._chat_toolbar.set_context_model(preferred)
            self._sync_provider_toggle()

    def _on_catalog_tick(self) -> bool:
        if self._destroyed:
            return False  # drop the timer
        # Cheap stat per 6h; the full rescan only runs when the claude
        # binary actually changed (self-update); Codex identity is checked live.
        if model_catalog.claude_binary_changed():
            self._refresh_model_catalog()
        else:
            self._refresh_model_catalog(force=False)
        return True  # keep ticking

    def _known_claude_models(self, driver) -> set[str]:
        """Aliases the picker would offer, plus what the CLI itself listed."""
        known: set[str] = set()
        try:
            known.update(entry.id for entry in model_catalog.anthropic_entries())
        except Exception:  # the scan is cached; a failure just shrinks the list
            pass
        for model in getattr(driver, "_cli_models", None) or []:
            if isinstance(model, dict):
                for key in ("value", "id", "displayName"):
                    value = model.get(key)
                    if isinstance(value, str) and value.strip():
                        known.add(value.strip())
        return known

    def _apply_model_choice(self, alias: str, *, quiet: bool = False) -> None:
        """The single path for every model change (picker, provider toggle,
        settings): persist, remember per provider, reflect everywhere."""
        if not quiet:
            self._pending_session_provider_adoption = ""
            # An explicit pick outranks a conversation model still waiting
            # on its catalog.
            self._deferred_conversation_model = None
        old_model = str(getattr(self, "_model", "") or "")
        old_provider = model_catalog.provider_for(old_model)
        provider = model_catalog.provider_for(alias)
        stripped_alias = model_catalog.canonical_model_id(alias)
        if stripped_alias and model_catalog.provider_for(stripped_alias) == provider:
            alias = stripped_alias
        self._model = alias
        self._ui_state.set("model", alias)  # sticky across restarts
        self._provider_models[provider] = alias
        self._ui_state.set(f"model_{provider}", alias)
        self._chat_toolbar.set_model(alias)
        # Rescale the context meter to the newly-selected model's window now,
        # rather than waiting for the next turn's usage report.
        self._chat_toolbar.set_context_model(alias)
        self._sync_provider_toggle()
        self._sync_assistant_labels()
        self._sync_effort_sensitivity()
        if provider != old_provider:
            self._refresh_openrouter_credits()
        if quiet:
            return
        if _mw().MainWindow._should_switch_conversation_in_place(
            self,
            old_provider=old_provider,
            old_model=old_model,
            new_provider=provider,
            new_model=alias,
        ):
            _mw().MainWindow._switch_conversation_model_in_place(
                self,
                old_provider=old_provider,
                old_model=old_model,
                new_provider=provider,
                new_model=alias,
            )
            return
        if provider != old_provider:
            lead = model_catalog.PROVIDER_LABELS.get(provider, "Claude")
            peer = model_catalog.PROVIDER_LABELS.get(old_provider, "Claude")
            # Nothing typed or started yet: the picker chip already shows the
            # choice, so a "now leading this Work" toast is pure noise.
            target = self._next_chat
            had_conversation = bool(target is not None and target.resume_id) or bool(
                self._driver is not None and self._driver.is_running
            )
            if not self._stage_fresh_chat_for_provider_switch():
                self._toast(
                    f"New {lead} chat — the running {peer} chat continues "
                    "in the background."
                )
                return
            if not had_conversation:
                return
            if self._current_work_id():
                target = self._next_chat
                if target is not None and not target.resume_id:
                    # Single Works never pass one provider's history to
                    # another; say so instead of showing a silent blank chat.
                    self._toast(
                        f"{lead} starts a new conversation in this Work — it "
                        f"can't see the {peer} chat, which stays in the sidebar."
                    )
                    return
                self._toast(
                    f"{lead} is now leading this Work — {peer} remains available."
                )
                return
        label = alias or "default"
        if self._driver is not None and self._driver.is_running:
            if self._try_live_model_change(alias, label):
                return
            if _mw().MainWindow._retire_for_respawn(self, self._driver, "set_model"):
                self._toast(
                    f"Model set to {label} — your next message continues this "
                    "chat on it."
                )
                return
            self._toast(f"Model set to {label} — applies to your next message.")

    def _retire_for_respawn(self, driver, method: str) -> bool:
        """Claude, GPT and Muse switch model in place. Gemini (model fixed at
        spawn) and OpenRouter cannot: wind an idle one down like an idle reap
        so the next send resumes the same native session on the new setting.
        Costs no extra model tokens — a new model reads the conversation
        uncached either way. A busy one is caught at send time."""
        if (
            driver is None
            or callable(getattr(driver, method, None))
            or driver.is_busy
            or not driver.is_accepting_input
            or not self._driver_matches_selected_provider(driver)
            or not self._driver_matches_target_binding(driver)
        ):
            return False
        driver.end_input()
        return True

    def _driver_model_is_stale(self, driver) -> bool:
        """The picker changed while this idle driver kept its old model."""
        if driver.is_busy or _mw().MainWindow._execution_change_pending_for(self, driver):
            return False
        target = model_catalog.canonical_model_id(self._model)
        raw_req = getattr(driver, "_helios_requested_model", None)
        if raw_req is not None and str(raw_req).strip():
            return model_catalog.canonical_model_id(str(raw_req)) != target
        actual = model_catalog.canonical_model_id(
            str(getattr(driver, "model", "") or "")
        )
        return bool(actual and actual != target)

    def _try_live_model_change(self, alias: str, label: str) -> bool:
        """Apply a same-provider model pick to the live Claude process.

        The control channel's `set_model` spares the respawn that this change
        prices at ~44.5k tokens of re-created session baseline. Returns True
        only when the request was dispatched — the toast then comes from the
        async outcome. Any refusal falls back to the staged
        "applies to your next chat" semantics unchanged; a fenced (ambiguous)
        outcome closes the process, which makes that fallback literally true.
        """
        driver = self._driver
        set_model = getattr(driver, "set_model", None)
        if not callable(set_model) or not alias:
            return False
        if not (
            driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        ):
            return False

        model_changes = getattr(self, "_model_changes", None)
        if not isinstance(model_changes, dict):
            model_changes = {}
            self._model_changes = model_changes
        change_token = object()
        model_changes[driver] = change_token
        self._sync_execution_control()

        def applied(success: bool, detail: str) -> None:
            if model_changes.get(driver) is not change_token:
                return
            del model_changes[driver]
            restart_required = bool(
                not success
                and getattr(driver, "execution_restart_required", False)
            )
            if restart_required:
                self._teardown_driver(driver)
            if getattr(self, "_destroyed", False):
                return
            if success:
                driver._helios_requested_model = alias
                coord = getattr(self, "_work_coordinator", None)
                wid = str(getattr(driver, "_helios_work_id", "") or _mw().MainWindow._current_work_id(self) or "")
                prov = self._driver_provider(driver)
                if coord is not None and wid and prov:
                    try:
                        coord.activate_model_binding(wid, prov, model=alias, force_fresh=False)
                    except Exception:
                        pass
                self._toast(f"Model set to {label} — applied to the live chat.")
                self._sync_effort_sensitivity()
            else:
                # `_ensure_driver` retries in place before the next turn.
                self._toast(f"Model set to {label} — applies to your next message.")

        try:
            sent = set_model(alias, applied)
        except Exception:
            model_changes.pop(driver, None)
            self._sync_execution_control()
            _log.exception("live model change failed")
            return False
        if sent is False and model_changes.get(driver) is change_token:
            del model_changes[driver]
            self._sync_execution_control()
            return False
        if not getattr(self, "_destroyed", False):
            self._sync_execution_control()
        # The driver invokes the callback exactly once on every path —
        # synchronous refusals included — so the toast is always its job.
        return True

    def _work_is_executing(self, work_id: str) -> bool:
        """True while any participant of ``work_id`` holds a running attempt
        or is still starting/mid-turn (a Gemini/Muse resume runs before its
        attempt is recorded, and a switch then earned "already executing")."""

        coordinator = getattr(self, "_work_coordinator", None)
        store = getattr(coordinator, "store", None)
        if not work_id or store is None:
            return False
        if any(
            driver.is_busy or getattr(driver, "is_initializing", False)
            for driver in self._driver_manager.drivers_for_work(work_id)
        ):
            return True
        try:
            return any(
                attempt.work_id == work_id
                for attempt in store.list_active_execution_attempts()
            )
        except Exception as exc:
            _log.warning("could not read running attempts for %s: %s", work_id, exc)
            return False

    def _work_id_has_continuity(self, work_id: str) -> bool:
        if not work_id:
            return False
        coordinator = getattr(self, "_work_coordinator", None)
        store = getattr(coordinator, "store", None)
        if store is None:
            return False
        try:
            work = store.get_work(work_id)
            return bool(work is not None and getattr(work, "continuity_enabled", False))
        except Exception:
            return False

    def _target_work_has_continuity(self, target) -> bool:
        if target is None:
            return False
        return _mw().MainWindow._work_id_has_continuity(
            self,
            str(getattr(target, "work_id", "") or ""),
        )

    def _should_switch_conversation_in_place(
        self,
        *,
        old_provider: str,
        old_model: str,
        new_provider: str,
        new_model: str,
    ) -> bool:
        if new_provider == old_provider and new_model == old_model:
            return getattr(self, "_pending_model_switch", None) is not None
        coordinator = getattr(self, "_work_coordinator", None)
        main_stack = getattr(self, "_main_stack", None)
        transcript = getattr(self, "_transcript", None)
        target = getattr(self, "_next_chat", None)
        if (
            coordinator is None
            or main_stack is None
            or transcript is None
            or target is None
            or getattr(target, "project", None) is None
            or bool(getattr(target.project, "read_only", False))
        ):
            return False
        if main_stack.get_visible_child_name() != "transcript":
            return False
        work_id = str(getattr(target, "work_id", "") or self._current_work_id() or "")
        has_turns_fn = getattr(transcript, "has_visible_turns", None)
        had_conversation = (
            bool(getattr(target, "resume_id", ""))
            or bool(self._driver is not None and self._driver.is_running)
            or bool(callable(has_turns_fn) and has_turns_fn())
            or getattr(self, "_pending_model_switch", None) is not None
            or (bool(work_id) and _mw().MainWindow._work_id_has_continuity(self, work_id))
        )
        if not had_conversation:
            return False
        if work_id:
            try:
                work = coordinator.store.get_work(work_id)
                if work is not None and getattr(work, "mode", "single") == "tandem":
                    return False
            except Exception:
                pass
        return True

    def _outgoing_leg_identity(
        self,
        *,
        old_provider: str,
        old_model: str,
    ) -> tuple[str, str, str]:
        """Resolve (outgoing_model, outgoing_session_id, outgoing_transcript_path)."""
        target = getattr(self, "_next_chat", None)
        coordinator = getattr(self, "_work_coordinator", None)
        drv = getattr(self, "_driver", None)
        resolved_model = old_model
        session_id = ""
        if drv is not None and self._driver_provider(drv) == old_provider:
            resolved_model = str(
                getattr(drv, "_helios_requested_model", "")
                or getattr(drv, "model", "")
                or old_model
            )
            session_id = str(getattr(drv, "session_id", "") or "")
        if not session_id and target is not None and target.resume_id:
            res = _mw().MainWindow._target_resume_resolution(self, target)
            if res.known and res.provider == old_provider:
                session_id = str(target.resume_id)
        if not session_id and coordinator is not None and target is not None and target.work_id:
            try:
                session_id = str(coordinator.resume_id(target.work_id, old_provider) or "")
            except Exception:
                session_id = ""
        transcript_path = ""
        if session_id:
            sessions_widget = getattr(self, "_sessions", None)
            path_fn = getattr(sessions_widget, "session_path", None)
            if callable(path_fn):
                transcript_path = str(path_fn(session_id) or "")
            if not transcript_path:
                ctx_sess = getattr(self, "_ctx_fill_session", None)
                if (
                    ctx_sess is not None
                    and getattr(ctx_sess, "session_id", "") == session_id
                    and getattr(ctx_sess, "path", None)
                ):
                    transcript_path = str(ctx_sess.path)
            if not transcript_path and target is not None and target.project is not None:
                transcript_path = str(target.project.path / f"{session_id}.jsonl")
        return resolved_model, session_id, transcript_path

    def _ensure_work_for_outgoing_leg(
        self,
        *,
        old_provider: str,
        old_model: str,
        old_session_id: str,
    ) -> str:
        target = getattr(self, "_next_chat", None)
        coordinator = getattr(self, "_work_coordinator", None)
        if target is None or target.project is None or coordinator is None:
            return ""
        drv = getattr(self, "_driver", None)
        work_id = str(
            getattr(target, "work_id", "")
            or getattr(drv, "_helios_work_id", "")
            or getattr(drv, "_helios_work_id_hint", "")
            or ""
        )
        try:
            if not work_id and old_session_id:
                work = coordinator.resolve_native_work(
                    old_provider,
                    old_session_id,
                    cwd=target.project.cwd,
                    model=old_model,
                )
                work_id = work.work_id
            elif not work_id:
                work = coordinator.ensure_work(
                    work_id="",
                    cwd=target.project.cwd,
                    lead_provider=old_provider,
                    objective=(self._pending_goal.objective if self._pending_goal else ""),
                )
                work_id = work.work_id
                coordinator.bind_participant(
                    work_id,
                    old_provider,
                    native_id=old_session_id,
                    model=old_model,
                )
            else:
                participant = coordinator.participant(work_id, old_provider)
                if participant is None or (
                    old_session_id and not participant.native_id
                ):
                    coordinator.bind_participant(
                        work_id,
                        old_provider,
                        native_id=old_session_id,
                        model=old_model,
                    )
            if old_session_id:
                session_goals.rekey_goal(old_session_id, work_id)
            if getattr(self, "_pending_goal", None) is not None:
                goal = self._goal_with_current_metadata(self._pending_goal)
                session_goals.set_goal(work_id, goal)
                coordinator.record_goal(
                    work_id=work_id,
                    provider=old_provider,
                    goal=goal,
                )
                self._pending_goal = None
            self._next_chat = _mw().stage_work(target, work_id)
            _mw().MainWindow._bind_browser_work(self, work_id)
            return work_id
        except Exception as exc:
            _log.warning("could not ensure Work for model switch: %s", exc)
            return work_id

    def _next_pending_switch_qid(self) -> int:
        seq = int(getattr(self, "_pending_switch_qid_seq", -1)) - 1
        if seq >= 0:
            seq = -1
        self._pending_switch_qid_seq = seq
        return seq

    def _make_pending_switch_remover(self):
        def _remove(qid: int) -> None:
            pending = getattr(self, "_pending_model_switch", None)
            if pending is None:
                return
            before = len(pending.queued_items)
            pending.queued_items = [
                item for item in pending.queued_items if item[0] != qid
            ]
            if len(pending.queued_items) != before and not self._destroyed:
                self._transcript.remove_queued(qid)

        return _remove

    def _queue_on_pending_model_switch(self, text: str) -> bool:
        pending = getattr(self, "_pending_model_switch", None)
        if pending is None:
            return False
        qid = _mw().MainWindow._next_pending_switch_qid(self)
        pending.queued_items.append((qid, text))
        self._transcript.append_queued(
            qid,
            text,
            _mw().MainWindow._make_pending_switch_remover(self),
        )
        return True

    def _drain_pending_model_switch_texts(self, *, remove_rows: bool = True) -> list[str]:
        pending = getattr(self, "_pending_model_switch", None)
        if pending is None or not pending.queued_items:
            return []
        items = list(pending.queued_items)
        pending.queued_items.clear()
        if remove_rows and not getattr(self, "_destroyed", False):
            transcript = getattr(self, "_transcript", None)
            if transcript is not None:
                for qid, _text in items:
                    transcript.remove_queued(qid)
        return [text for _qid, text in items if text]

    def _stash_or_clear_pending_model_switch(self, *, keep_for_work_id: str = "") -> None:
        pending = getattr(self, "_pending_model_switch", None)
        if pending is None:
            return
        if keep_for_work_id and pending.work_id == keep_for_work_id:
            return
        texts = _mw().MainWindow._drain_pending_model_switch_texts(self, remove_rows=True)
        self._pending_model_switch = None
        if texts and pending.work_id:
            drafts = getattr(self, "_drafts", None)
            if drafts is not None:
                key = f"work:{pending.work_id}"
                existing = drafts.take(key).strip()
                combined = texts + ([existing] if existing else [])
                drafts.stash(key, "\n\n".join(combined))

    def _switch_conversation_model_in_place(
        self,
        *,
        old_provider: str,
        old_model: str,
        new_provider: str,
        new_model: str,
    ) -> None:
        outgoing_model, outgoing_sid, outgoing_path = _mw().MainWindow._outgoing_leg_identity(
            self,
            old_provider=old_provider,
            old_model=old_model,
        )
        existing_pending = getattr(self, "_pending_model_switch", None)
        if existing_pending is not None:
            work_id = existing_pending.work_id
        else:
            work_id = _mw().MainWindow._ensure_work_for_outgoing_leg(
                self,
                old_provider=old_provider,
                old_model=outgoing_model,
                old_session_id=outgoing_sid,
            )
        if not work_id:
            self._toast("Helios could not establish continuity for this conversation.")
            return

        drv = getattr(self, "_driver", None)
        checkpoint_pending = _mw().MainWindow._checkpoint_dispatch_pending_for(self, drv)
        busy_now = (
            self._work_is_executing(work_id)
            or bool(drv is not None and (drv.is_busy or getattr(drv, "is_initializing", False)))
            or checkpoint_pending
        )
        if busy_now:
            if existing_pending is not None:
                if (
                    new_provider == existing_pending.from_provider
                    and new_model == existing_pending.from_model
                ):
                    texts = _mw().MainWindow._drain_pending_model_switch_texts(
                        self,
                        remove_rows=True,
                    )
                    src_drv = existing_pending.source_driver or drv
                    self._pending_model_switch = None
                    if src_drv is not None and hasattr(src_drv, "queue_user_text"):
                        for text in texts:
                            qid = src_drv.queue_user_text(text)
                            self._transcript.append_queued(
                                qid,
                                text,
                                self._make_queued_remover(src_drv),
                            )
                    self._toast(
                        f"Staying on {human_model_label(new_provider, new_model)}."
                    )
                    return
                existing_pending.to_provider = new_provider
                existing_pending.to_model = new_model
                self._toast(
                    f"Switch to {human_model_label(new_provider, new_model)} queued — "
                    "applies when this turn finishes."
                )
                return

            drained_texts: list[str] = []
            if drv is not None and hasattr(drv, "queued_messages"):
                queued_rows = list(drv.queued_messages())
                delivery = getattr(drv, "_pending_queue_delivery", None)
                if (
                    isinstance(delivery, tuple)
                    and len(delivery) == 2
                    and queued_rows[:1] == [delivery]
                ):
                    take_succ = getattr(
                        drv,
                        "take_queued_successors_after_pending_delivery",
                        None,
                    )
                    if callable(take_succ):
                        succ = take_succ()
                        if succ:
                            for qid, _t in queued_rows[1:]:
                                self._transcript.remove_queued(qid)
                            drained_texts.extend(succ)
                else:
                    take_all = getattr(drv, "take_queued", None)
                    if callable(take_all):
                        taken = take_all()
                        if taken:
                            for qid, _t in queued_rows:
                                self._transcript.remove_queued(qid)
                            drained_texts.extend(taken)

            pending = _PendingModelSwitch(
                work_id=work_id,
                from_provider=old_provider,
                from_model=outgoing_model,
                to_provider=new_provider,
                to_model=new_model,
                source_driver=drv,
            )
            self._pending_model_switch = pending
            for text in drained_texts:
                _mw().MainWindow._queue_on_pending_model_switch(self, text)
            self._toast(
                f"Switch to {human_model_label(new_provider, new_model)} queued — "
                "applies when this turn finishes."
            )
            return

        _mw().MainWindow._execute_in_place_model_switch(
            self,
            work_id=work_id,
            old_provider=old_provider,
            old_model=outgoing_model,
            outgoing_session_id=outgoing_sid,
            outgoing_transcript_path=outgoing_path,
            new_provider=new_provider,
            new_model=new_model,
            announce=True,
        )

    def _execute_in_place_model_switch(
        self,
        *,
        work_id: str,
        old_provider: str,
        old_model: str,
        outgoing_session_id: str = "",
        outgoing_transcript_path: str = "",
        new_provider: str,
        new_model: str,
        announce: bool = True,
    ) -> bool:
        coordinator = getattr(self, "_work_coordinator", None)
        target = getattr(self, "_next_chat", None)
        if coordinator is None or target is None or target.project is None:
            return False

        if not outgoing_session_id:
            _om, outgoing_session_id, outgoing_transcript_path = (
                _mw().MainWindow._outgoing_leg_identity(
                    self,
                    old_provider=old_provider,
                    old_model=old_model,
                )
            )

        try:
            coordinator.enable_continuity(work_id)
            if outgoing_session_id:
                coordinator.ensure_initial_work_leg(
                    work_id,
                    provider=old_provider,
                    model=old_model,
                    native_session_id=outgoing_session_id,
                    transcript_path=outgoing_transcript_path,
                )
                coordinator.seed_continuity_from_transcript_if_empty(
                    work_id,
                    provider=old_provider,
                    model=old_model,
                    native_session_id=outgoing_session_id,
                    transcript_path=outgoing_transcript_path,
                )
            legs_before = coordinator.list_work_legs(work_id)
            if len(legs_before) > 1:
                coordinator.store.pop_unused_tail_leg(work_id)
        except Exception as exc:
            _log.warning("failed preparing continuity legs for %s: %s", work_id, exc)
            self._toast("Could not switch models cleanly in this conversation.")
            return False

        _mw().MainWindow._rekey_visible_draft(self, draft_key(target, work_id=work_id))

        def _count_existing_turns(tpath: str) -> int:
            if not tpath:
                return 0
            try:
                from pathlib import Path
                p_obj = Path(tpath)
                if p_obj.is_file():
                    return len(parse_transcript(p_obj))
            except Exception:
                pass
            return 0

        drv = getattr(self, "_driver", None)
        existing_google_part = (
            coordinator.participant(work_id, model_catalog.PROVIDER_GOOGLE)
            if new_provider == model_catalog.PROVIDER_GOOGLE
            else None
        )
        force_fresh = bool(
            new_provider == model_catalog.PROVIDER_GOOGLE
            and (
                (old_provider == new_provider and new_model != old_model)
                or (existing_google_part is not None and bool(existing_google_part.native_id))
            )
            and coordinator.store.active_or_matching_binding(
                work_id,
                new_provider,
                new_model,
            )
            is None
        )

        # Same-provider live set_model path (Claude, Codex, Muse, OpenRouter).
        if (
            new_provider == old_provider
            and not force_fresh
            and drv is not None
            and drv.is_accepting_input
            and self._driver_matches_selected_provider(drv)
            and self._driver_matches_target_binding(drv)
            and callable(getattr(drv, "set_model", None))
        ):
            live_sid = str(getattr(drv, "session_id", "") or outgoing_session_id or "")
            live_path = (
                str(target.project.path / f"{live_sid}.jsonl")
                if live_sid
                else ""
            )
            label = human_model_label(new_provider, new_model)
            outcomes: list[tuple[bool, str]] = []
            dispatched_async = False
            model_changes = getattr(self, "_model_changes", None)
            if not isinstance(model_changes, dict):
                model_changes = {}
                self._model_changes = model_changes
            live_token = object()
            model_changes[drv] = live_token
            self._sync_execution_control()

            def _restore_rejected_live_model(detail_msg: str) -> None:
                if getattr(self, "_destroyed", False):
                    return
                self._model = old_model
                if isinstance(getattr(self, "_provider_models", None), dict):
                    self._provider_models[old_provider] = old_model
                ui_state = getattr(self, "_ui_state", None)
                if ui_state is not None:
                    ui_state.set("model", old_model)
                toolbar = getattr(self, "_chat_toolbar", None)
                if toolbar is not None:
                    toolbar.set_model(old_model)
                    toolbar.set_context_model(old_model)
                self._sync_effort_sensitivity()
                sfx = f" ({detail_msg})" if detail_msg else ""
                self._toast(f"Could not switch live session to {label}{sfx}.")

            def _on_live_model_outcome(success: bool, detail: str) -> None:
                if model_changes.get(drv) is not live_token:
                    return
                del model_changes[drv]
                outcomes.append((bool(success), str(detail or "")))
                if not success:
                    restart_req = bool(getattr(drv, "execution_restart_required", False))
                    if restart_req:
                        teardown = getattr(self, "_teardown_driver", None)
                        if callable(teardown):
                            teardown(drv)
                        if (
                            dispatched_async
                            and not getattr(self, "_destroyed", False)
                            and _mw().MainWindow._current_work_id(self) == work_id
                        ):
                            _mw().MainWindow._execute_in_place_model_switch(
                                self,
                                work_id=work_id,
                                old_provider=old_provider,
                                old_model=old_model,
                                outgoing_session_id=live_sid,
                                new_provider=new_provider,
                                new_model=new_model,
                                announce=announce,
                            )
                        return
                    _restore_rejected_live_model(str(detail or ""))
                    return

                drv._helios_requested_model = new_model
                try:
                    coordinator.activate_model_binding(
                        work_id,
                        new_provider,
                        model=new_model,
                        force_fresh=False,
                    )
                    if live_sid:
                        coordinator.append_work_leg(
                            work_id,
                            provider=new_provider,
                            model=new_model,
                            native_session_id=live_sid,
                            transcript_path=live_path,
                            started_turn_index=_count_existing_turns(live_path),
                            capsule_manifest={
                                "schema": 1,
                                "mode": "same_session",
                                "from_provider": old_provider,
                                "from_model": old_model,
                                "to_provider": new_provider,
                                "to_model": new_model,
                            },
                        )
                except Exception as exc:
                    _log.warning("could not record same-session model leg: %s", exc)
                if getattr(self, "_destroyed", False):
                    return
                if dispatched_async and not self._drv_is_current(drv):
                    return
                self._transcript.set_active_assistant(
                    new_provider,
                    new_model,
                    session_id=live_sid,
                    cwd=target.project.cwd,
                )
                now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                self._transcript.replace_or_append_handoff_divider(
                    HandoffBoundary(
                        leg_seq=len(coordinator.list_work_legs(work_id)),
                        from_provider=old_provider,
                        from_model=old_model,
                        to_provider=new_provider,
                        to_model=new_model,
                        capsule_manifest={
                            "schema": 1,
                            "mode": "same_session",
                            "objective": "Same native session retained; full history remains active.",
                        },
                        created_at=now_iso,
                    )
                )
                if announce:
                    self._toast(f"Model set to {label} — applied to the live chat.")
                self._sync_effort_sensitivity()

            try:
                sync_ret = drv.set_model(new_model, _on_live_model_outcome)
            except Exception as exc:
                model_changes.pop(drv, None)
                self._sync_execution_control()
                _log.exception("live model change failed")
                _restore_rejected_live_model(str(exc))
                return False

            if outcomes:
                ok_flag, _det = outcomes[0]
                if ok_flag:
                    return True
                if not bool(getattr(drv, "execution_restart_required", False)):
                    return False
                drv = getattr(self, "_driver", None)
            elif sync_ret is False:
                _on_live_model_outcome(False, "")
                if not bool(getattr(drv, "execution_restart_required", False)):
                    return False
                drv = getattr(self, "_driver", None)
            else:
                dispatched_async = True
                return True

        # Retire idle same-provider driver that cannot switch models in-place (e.g. Google/Kimi).
        if (
            new_provider == old_provider
            and drv is not None
            and drv.is_accepting_input
            and not drv.is_busy
        ):
            try:
                drv.end_input()
            except Exception:
                pass

        try:
            coordinator.select_lead(work_id, new_provider)
            participant, _binding = coordinator.activate_model_binding(
                work_id,
                new_provider,
                model=new_model,
                force_fresh=force_fresh,
            )
        except Exception as exc:
            _log.warning("could not activate target model binding: %s", exc)
            self._toast("Could not activate the selected model for this conversation.")
            return False

        resumed_id = "" if force_fresh else str(participant.native_id or "")
        if resumed_id:
            resolution = _mw().MainWindow._resolve_resume_id(self, resumed_id, target.project)
            if not resolution.known or resolution.provider != new_provider:
                resumed_id = ""

        # Bind an already-live idle driver for the target leg if one exists in DriverManager,
        # without re-rendering or clearing the composite transcript.
        rebound_driver = None
        if not force_fresh:
            candidate = self._driver_manager.driver_for_work_participant(
                work_id,
                new_provider,
                generation=participant.generation,
            )
            if candidate is None and resumed_id:
                candidate = self._driver_manager.driver_for_session(resumed_id)
            if (
                candidate is not None
                and candidate.is_accepting_input
                and self._driver_matches_selected_provider(candidate)
                and _mw().MainWindow._driver_matches_project(candidate, target.project)
            ):
                if new_provider == model_catalog.PROVIDER_GOOGLE and (
                    getattr(candidate, "_helios_requested_model", new_model) != new_model
                ):
                    try:
                        candidate.end_input()
                    except Exception:
                        pass
                else:
                    tag_driver(candidate, participant)
                    rebound_driver = candidate

        self._bind_visible_driver(rebound_driver)
        if rebound_driver is not None and _mw().MainWindow._driver_model_is_stale(self, rebound_driver):
            set_rebound_model = getattr(rebound_driver, "set_model", None)
            if callable(set_rebound_model):
                model_changes = getattr(self, "_model_changes", None)
                if not isinstance(model_changes, dict):
                    model_changes = {}
                    self._model_changes = model_changes
                rebound_token = object()
                model_changes[rebound_driver] = rebound_token
                self._sync_execution_control()

                def _on_rebound_model_outcome(ok: bool, _detail: str) -> None:
                    if model_changes.get(rebound_driver) is not rebound_token:
                        return
                    del model_changes[rebound_driver]
                    if ok:
                        rebound_driver._helios_requested_model = new_model
                        if not getattr(self, "_destroyed", False):
                            self._sync_effort_sensitivity()
                            self._sync_execution_control()
                        return
                    try:
                        rebound_driver.end_input()
                    except Exception:
                        pass
                    if not getattr(self, "_destroyed", False):
                        if getattr(self, "_driver", None) is rebound_driver:
                            self._bind_visible_driver(None)
                        self._sync_execution_control()

                try:
                    if set_rebound_model(new_model, _on_rebound_model_outcome) is False:
                        _on_rebound_model_outcome(False, "")
                except Exception:
                    _on_rebound_model_outcome(False, "")
            else:
                try:
                    rebound_driver.end_input()
                except Exception:
                    pass
                self._bind_visible_driver(None)
        self._stop_following()

        self._next_chat = switch_work_participant(
            _mw().stage_work(target, work_id),
            resumed_id,
            new_provider if resumed_id else "",
        )
        _mw().MainWindow._select_browser_session(self, self._next_chat)

        goal = session_goals.get_goal(work_id)
        preview_manifest = coordinator.preview_continuity_manifest(
            work_id,
            new_provider,
            model=new_model,
            goal=goal,
        )
        if resumed_id:
            resumed_path = str(target.project.path / f"{resumed_id}.jsonl")
            try:
                coordinator.append_work_leg(
                    work_id,
                    provider=new_provider,
                    model=new_model,
                    native_session_id=resumed_id,
                    transcript_path=resumed_path,
                    started_turn_index=_count_existing_turns(resumed_path),
                    capsule_manifest=preview_manifest,
                )
            except Exception as exc:
                _log.warning("could not append resumed work leg: %s", exc)

        self._transcript.set_active_assistant(
            new_provider,
            new_model,
            session_id=resumed_id,
            cwd=target.project.cwd,
        )
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        divider = self._transcript.replace_or_append_handoff_divider(
            HandoffBoundary(
                leg_seq=len(coordinator.list_work_legs(work_id)) + (0 if resumed_id else 1),
                from_provider=old_provider,
                from_model=old_model,
                to_provider=new_provider,
                to_model=new_model,
                capsule_manifest=preview_manifest,
                created_at=now_iso,
            )
        )

        self._sync_effort_sensitivity()
        self._sync_execution_control()
        _mw().MainWindow._refresh_capabilities(self)
        if announce and divider is not None:
            self._toast(
                f"Switched to {human_model_label(new_provider, new_model)} — "
                "essential context carries forward in this chat."
            )
        return True

    def _complete_pending_model_switch_if_ready(
        self,
        drv,
        *,
        allow_auto_send: bool,
    ) -> bool:
        pending = getattr(self, "_pending_model_switch", None)
        if pending is None or getattr(self, "_destroyed", False):
            return False
        drv_work_id = str(
            getattr(drv, "_helios_work_id", "")
            or getattr(drv, "_helios_work_id_hint", "")
            or ""
        )
        if (
            pending.source_driver is not None
            and drv is not pending.source_driver
            and drv_work_id != pending.work_id
        ):
            return False
        if self._work_is_executing(pending.work_id):
            return False
        effective_auto_send = bool(allow_auto_send and not pending.stop_requested)
        queued_texts = _mw().MainWindow._drain_pending_model_switch_texts(
            self,
            remove_rows=True,
        )
        self._pending_model_switch = None
        if not self._drv_is_current(drv) and _mw().MainWindow._current_work_id(self) != pending.work_id:
            if queued_texts:
                drafts = getattr(self, "_drafts", None)
                if drafts is not None:
                    key = f"work:{pending.work_id}"
                    existing = drafts.take(key).strip()
                    drafts.stash(key, "\n\n".join(queued_texts + ([existing] if existing else [])))
            return False

        if pending.to_model and model_catalog.canonical_model_id(
            pending.to_model
        ) != model_catalog.canonical_model_id(str(getattr(self, "_model", "") or "")):
            # Reselecting the source session while the switch was queued put
            # the header back on the source provider's model. The queued
            # texts below are sent under the header, so repair it first.
            self._apply_model_choice(pending.to_model, quiet=True)
        switched = _mw().MainWindow._execute_in_place_model_switch(
            self,
            work_id=pending.work_id,
            old_provider=pending.from_provider,
            old_model=pending.from_model,
            outgoing_session_id=str(getattr(drv, "session_id", "") or ""),
            new_provider=pending.to_provider,
            new_model=pending.to_model,
            announce=True,
        )
        if not queued_texts:
            return switched
        if not switched or not effective_auto_send:
            existing = self._composer.current_text().strip()
            combined = queued_texts + ([existing] if existing else [])
            self._composer.set_text("\n\n".join(combined))
            return switched

        first_text, rest_texts = queued_texts[0], queued_texts[1:]
        self._on_composer_send(self._composer, first_text)
        new_drv = getattr(self, "_driver", None)
        if rest_texts:
            if new_drv is not None and hasattr(new_drv, "queue_user_text"):
                for item_text in rest_texts:
                    qid = new_drv.queue_user_text(item_text)
                    self._transcript.append_queued(
                        qid,
                        item_text,
                        self._make_queued_remover(new_drv),
                    )
            else:
                existing = self._composer.current_text().strip()
                self._composer.set_text(
                    "\n\n".join(rest_texts + ([existing] if existing else []))
                )
        return True

    def _model_for_provider(self, provider: str) -> str:
        """The model alias to use when switching to `provider`: the last one
        used with it, else a sensible default for that side."""
        remembered = self._provider_models.get(provider, "")
        if provider == model_catalog.PROVIDER_OPENAI:
            valid = {e.id for e in self._openai_entries}
            if remembered in valid:
                return remembered
            return model_catalog.preferred_openai_model(self._openai_entries)
        if provider == model_catalog.PROVIDER_GOOGLE:
            google_entries = [entry for entry in getattr(self, "_catalog_entries_by_id", {}).values()
                              if entry.provider == model_catalog.PROVIDER_GOOGLE]
            valid = {e.id for e in google_entries}
            if remembered in valid:
                return remembered
            return model_catalog.preferred_google_model(google_entries)
        if provider == model_catalog.PROVIDER_MUSE:
            muse_entries = [entry for entry in getattr(self, "_catalog_entries_by_id", {}).values()
                            if entry.provider == model_catalog.PROVIDER_MUSE]
            valid = {e.id for e in muse_entries}
            if remembered in valid:
                return remembered
            return model_catalog.preferred_muse_model(muse_entries)
        if provider == model_catalog.PROVIDER_KIMI:
            kimi_entries = [entry for entry in getattr(self, "_catalog_entries_by_id", {}).values()
                            if entry.provider == model_catalog.PROVIDER_KIMI]
            if remembered in {entry.id for entry in kimi_entries}:
                return remembered
            return model_catalog.preferred_kimi_model(kimi_entries)
        if provider == model_catalog.PROVIDER_OPENROUTER:
            if model_catalog.openrouter_model_selectable(remembered):
                return remembered
            or_entries, _ = model_catalog.openrouter_entries()
            return model_catalog.preferred_openrouter_model(or_entries)
        return remembered or DEFAULT_MODEL

    def _valid_anthropic_fallback_model(self) -> str | None:
        """A catalog-proven Claude model for a fail-closed provider fallback."""
        anthropic_entries = [
            entry
            for entry in self._catalog_entries_by_id.values()
            if getattr(entry, "provider", model_catalog.PROVIDER_ANTHROPIC)
            == model_catalog.PROVIDER_ANTHROPIC
        ]
        if not anthropic_entries:
            return None
        valid = {entry.id for entry in anthropic_entries}
        remembered = self._provider_models.get(model_catalog.PROVIDER_ANTHROPIC)
        for candidate in (remembered, DEFAULT_MODEL, ""):
            if candidate is not None and candidate in valid:
                return candidate
        return anthropic_entries[0].id

    def _guard_openai_driver_activation(self) -> bool:
        """Require an authoritative exact-model match before any GPT send.

        This guard runs before driver reuse as well as spawn.  A persisted or
        programmatically assigned GPT id therefore cannot cross the startup
        discovery window.  Once a catalog exists, quietly fall back to a model
        it proves is valid for Claude; without that baseline, fail closed.
        """
        if self._selected_provider() != model_catalog.PROVIDER_OPENAI:
            return True
        valid = {entry.id for entry in self._openai_entries}
        if (
            self._openai_catalog_authoritative
            and self._openai_entries
            and self._model in valid
        ):
            return True
        fallback = self._valid_anthropic_fallback_model()
        if fallback is not None:
            self._apply_model_choice(fallback, quiet=True)
            return True
        self._toast(
            "No OpenAI models available — add a key in Settings → Providers."
        )
        return False

    def _adopt_session_provider(self, provider: str) -> None:
        """Point the active model/toggle at the provider that produced
        the selected session, quietly (no toast, no fresh-chat staging) — used when a
        session is selected so the header tracks the chat you're viewing."""
        if provider not in (
            model_catalog.PROVIDER_ANTHROPIC,
            model_catalog.PROVIDER_OPENAI,
            model_catalog.PROVIDER_OPENROUTER,
            model_catalog.PROVIDER_GOOGLE,
            model_catalog.PROVIDER_MUSE,
            model_catalog.PROVIDER_KIMI,
        ):
            return
        if provider == self._selected_provider():
            if (
                str(getattr(self, "_pending_session_provider_adoption", "") or "")
                != provider
            ):
                self._pending_session_provider_adoption = ""
                return
            # Catalog just landed for the provider already on screen: fall
            # through and revalidate the exact model against it instead of
            # keeping a remembered id the account may no longer have.
        if provider == model_catalog.PROVIDER_OPENAI and not self._openai_entries:
            # Without an authoritative catalog we cannot safely resume/spawn
            # GPT, and a send remains fail-closed. But the header should not
            # sit on another provider's model/settings meanwhile: adopt the
            # remembered GPT model for display and keep the adoption pending
            # so catalog arrival revalidates it (see _normalize above).
            # NOTE: _model_for_provider deliberately stays fail-closed for
            # new chats here (no invented default); restoring an EXISTING
            # session is the one place the persisted remembered model —
            # catalog-proven when picked — is honest display data.
            remembered = str(self._provider_models.get(provider, "") or "")
            if remembered:
                self._apply_model_choice(remembered, quiet=True)
            self._pending_session_provider_adoption = provider
            return
        alias = self._model_for_provider(provider)
        if alias:
            self._pending_session_provider_adoption = ""
            self._apply_model_choice(alias, quiet=True)
        elif (
            provider
            in (model_catalog.PROVIDER_GOOGLE, model_catalog.PROVIDER_MUSE)
            and not bool(getattr(self, "_catalog_entries_by_id", None))
            and str(self._provider_models.get(provider, "") or "")
        ):
            # Same restore-only rule as GPT above, pre-catalog.
            self._apply_model_choice(
                str(self._provider_models[provider]), quiet=True
            )
            self._pending_session_provider_adoption = provider
        else:
            self._pending_session_provider_adoption = provider

    def _conversation_model(self, provider: str, session_id: str, live=None) -> str:
        """The model this conversation itself runs on, or "" when unknown.

        A live driver knows exactly. Otherwise the Work binding recorded it at
        the first send and on every confirmed switch.
        """
        if live is not None:
            model = str(
                getattr(live, "_helios_requested_model", "")
                or getattr(live, "model", "")
                or ""
            ).strip()
            if model:
                return model
        store = getattr(getattr(self, "_work_coordinator", None), "store", None)
        if store is None or not session_id:
            return ""
        try:
            work_id = store.work_id_for_native_session(provider, session_id)
            binding = (
                store.active_binding_for_work_provider(work_id, provider)
                if work_id
                else None
            )
        except Exception as exc:  # noqa: BLE001 - selection must still complete
            _log.warning("could not read the model of session %s: %s", session_id, exc)
            return ""
        if binding is None or binding.native_id != session_id:
            return ""
        return str(binding.model or "").strip()

    def _restore_conversation_model(self, provider: str, session_id: str, live=None) -> bool:
        """Open a conversation on its own model, not the provider's last pick.

        `_adopt_session_provider` only knows the provider, so it shows the
        model last used with that provider anywhere. Left there, the next send
        read that as a picker change and switched this conversation to it:
        using Fable in one chat silently moved every other Claude chat to
        Fable on its next message. Returns True when the header changed.
        """
        mw = _mw().MainWindow
        # Each attempt decides afresh whether a retry is owed.
        self._deferred_conversation_model = None
        pending = getattr(self, "_pending_model_switch", None)
        if pending is not None:
            work_id = str(getattr(live, "_helios_work_id", "") or "")
            store = getattr(getattr(self, "_work_coordinator", None), "store", None)
            if not work_id and store is not None and session_id:
                try:
                    work_id = store.work_id_for_native_session(provider, session_id)
                except Exception:  # noqa: BLE001
                    work_id = ""
            if work_id and pending.work_id == work_id:
                # A switch the user queued for this conversation owns the
                # header until it lands.
                return False
        model = mw._conversation_model(self, provider, session_id, live)
        if not model or model_catalog.provider_for(model) != provider:
            return False
        current = str(getattr(self, "_model", "") or "")
        if model_catalog.canonical_model_id(model) == model_catalog.canonical_model_id(current):
            return False
        if live is None and provider != model_catalog.PROVIDER_ANTHROPIC:
            # Claude accepts ids its catalog no longer lists; every other
            # provider must still offer a recorded model before a cold
            # conversation is pointed at it.
            entries = getattr(self, "_catalog_entries_by_id", None) or {}
            entry = entries.get(model_catalog.canonical_model_id(model))
            if entry is None or getattr(entry, "provider", "") != provider:
                if not any(
                    getattr(row, "provider", "") == provider for row in entries.values()
                ):
                    # This provider's catalog has not landed yet (fresh
                    # boot). Owed again when it does; see
                    # _retry_deferred_conversation_model.
                    self._deferred_conversation_model = (provider, session_id)
                return False
        self._apply_model_choice(model, quiet=True)
        return True

    def _retry_deferred_conversation_model(self) -> bool:
        """Restore a conversation model that had to wait for its catalog.

        Only for the conversation still on screen with no driver started: a
        send already made under the adopted model is not swapped afterwards.
        Returns True while the restore is still owed.
        """
        deferred = getattr(self, "_deferred_conversation_model", None)
        if not deferred:
            return False
        provider, session_id = deferred
        target = getattr(self, "_next_chat", None)
        if (
            getattr(self, "_driver", None) is not None
            or target is None
            or str(getattr(target, "resume_id", "") or "") != session_id
            or bool(getattr(getattr(target, "project", None), "read_only", False))
        ):
            self._deferred_conversation_model = None
            return False
        _mw().MainWindow._restore_conversation_model(self, provider, session_id)
        return bool(getattr(self, "_deferred_conversation_model", None))

    def _selected_provider(self) -> str:
        return model_catalog.provider_for(self._model)

    def _sync_openrouter_toggle_sensitivity(self) -> None:
        """Enable/disable the OpenRouter toggle based on whether a key exists."""
        or_toggle = getattr(self, "_openrouter_toggle", None)
        if or_toggle is None:
            return
        from helios.backend.openrouter import key as or_key
        has_key = bool(or_key.load_key())
        or_toggle.set_sensitive(has_key)
        or_toggle.set_tooltip_text(
            "Chat with OpenRouter" if has_key
            else "Add an OpenRouter API key in Settings → Providers"
        )

    def _driver_matches_selected_provider(self, driver) -> bool:
        return self._driver_manager.driver_matches_provider(
            driver, self._selected_provider()
        )

    def _on_toolbar_model_changed(self, _toolbar, alias: str) -> None:
        # Takes effect on the next driver spawn. If a driver is running, we
        # could optionally restart it; for now we just queue.
        self._apply_model_choice(alias)

    def _model_display_label(self, model: str) -> str:
        entries = getattr(self, "_catalog_entries_by_id", {})
        entry = entries.get(model_catalog.picker_model_id(model, entries))
        return entry.label if entry is not None else (
            model.removesuffix("[1m]") or "the Claude settings default"
        )

    def _apply_default_model_for_new_chat(self) -> None:
        """Every New chat entry point opens on the Settings default model.

        Unconfigured (never saved in Settings) keeps the old behaviour of
        following the last-used model. A default whose provider cannot run
        right now is left to the existing send-time guards, exactly like the
        sticky model restored at startup."""
        store = getattr(self, "_ui_state", None)
        default = store.get(DEFAULT_MODEL_KEY) if store is not None else None
        if isinstance(default, str) and default != getattr(self, "_model", None):
            self._apply_model_choice(default, quiet=True)
