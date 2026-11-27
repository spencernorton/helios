"""Execution-target resolution, permission/effort/workflow controls, and
durable execution attempt accounting, split out of main_window.

Behaviour-preserving mixin: every method runs on the composed MainWindow
instance so driver-manager, WorkCoordinator, and test monkeypatches on
``helios.main_window`` resolve identically while isolating the execution
policy and lane-accounting cluster into one auditable module.
"""

from __future__ import annotations

from helios.backend import model_catalog, session_providers
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence,
    ExecutionDispatchEvidence,
    ExecutionStopEvidence,
    ExecutionTerminalEvidence,
)
from helios.backend.project_perms import (
    PERMISSION_MODES,
    canonical_cwd,
    effective_execution_mode,
    legacy_permission,
    more_restrictive_mode,
)
from helios.backend.projects import HOME_CWD
from helios.backend.session_router import ChatTarget
from helios.backend.work_store import ExecutionAdmissionError
from helios.backend.workflow_modes import (
    DEFAULT_WORKFLOW_MODE,
    canonical_workflow_mode,
    provider_allows_workflow,
    workflow_modes_for_provider,
)
from helios.log import get_logger
from helios.widgets.chat_toolbar import DEFAULT_EFFORT, EFFORT_STOPS

_log = get_logger("window")


def _mw():
    from helios import main_window

    return main_window


def _home_execution_locked(cwd: str) -> bool:
    """HOME is a discovery surface, never an executable workspace."""

    return bool(cwd) and canonical_cwd(cwd) == canonical_cwd(HOME_CWD)


class WindowExecutionMixin:
    """Execution target, permission/effort/workflow controls, and lane accounting."""

    def _effective_permission_mode(self, cwd: str) -> str:
        """Safe, disclosed fallback for a conversation without its own choice."""

        mode, _source = _mw().MainWindow._permission_fallback(self, cwd)
        return mode

    def _permission_fallback(self, cwd: str) -> tuple[str, str]:
        """Return ``(mode, provenance)`` for an unconfigured conversation.

        The retired workspace file is read-only compatibility evidence. Its
        restrictive choices remain in force and visible during migration;
        legacy Bypass is permanently clamped to Ask.

        Exception: once the user has confirmed an explicit GLOBAL default in
        Settings (`permission_mode_confirmed`), that modern, disclosed choice is
        authoritative — the retired workspace file must not clamp it down. Its
        whole purpose was to avoid silently *widening* permissions for un-migrated
        users; it must not silently *narrow* a default the user deliberately set
        (for example, Auto reduced to a stale per-cwd `dontAsk`).
        """

        if _home_execution_locked(cwd):
            return "plan", "home-plan"
        if bool(self._ui_state.get("permission_mode_confirmed", False)):
            return _mw().sanitize_global_default(self._permission_mode), "default"
        legacy = legacy_permission(cwd)
        if legacy is not None:
            return (
                more_restrictive_mode(self._permission_mode, legacy.mode),
                "legacy-invalid"
                if legacy.invalid
                else "legacy-bypass-retired"
                if legacy.bypass_retired
                else "legacy",
            )
        return _mw().sanitize_global_default(self._permission_mode), "default"

    def _execution_target_provider(self) -> str:
        """Provider that will execute the selected conversation's next turn.

        Session selection normally adopts the transcript's provider. In the
        degraded case where a historical provider is unavailable, Helios keeps
        the safe available provider and stages that Work participant on send;
        execution controls must follow that provider now, not write settings
        into the historical sibling's native id.
        """

        target = self._next_chat
        if (
            target is not None
            and target.resume_id
            and not _mw().MainWindow._target_resume_resolution(self, target).known
        ):
            return ""
        return self._selected_provider()

    def _target_resume_resolution(
        self,
        target: ChatTarget | None,
    ) -> session_providers.ProviderResolution:
        """Re-resolve native ownership at the activation boundary.

        ``ChatTarget.resume_provider`` is display/routing context, not
        authority. The index, conversation record, and transcript marker must
        still agree immediately before a resume id can affect persistence or
        provider activation.
        """

        if target is None or not target.resume_id:
            return session_providers.ProviderResolution()
        return _mw().MainWindow._resolve_resume_id(
            self,
            target.resume_id,
            getattr(target, "project", None),
            cached_provider=str(getattr(target, "resume_provider", "") or ""),
        )

    def _resolve_resume_id(
        self,
        session_id: str,
        project,
        *,
        cached_provider: str = "",
    ) -> session_providers.ProviderResolution:
        """Resolve one native id using durable evidence available right now."""

        if not session_id:
            return session_providers.ProviderResolution()
        project_path = getattr(project, "path", None)
        transcript_path = (
            project_path / f"{session_id}.jsonl"
            if project_path is not None
            else None
        )
        resolution = session_providers.resolve_provider(
            session_id,
            transcript_path,
            conversation_store=getattr(self, "_conversation_perms", None),
        )
        cached = str(cached_provider or "")
        if cached and resolution.known and cached != resolution.provider:
            return session_providers.ProviderResolution(
                sources=(*resolution.sources, "target-cache-conflict"),
                conflicted=True,
            )
        return resolution

    def _execution_target_session_id(
        self,
        target: ChatTarget | None,
        provider: str,
    ) -> str:
        """Native id for the participant that will execute, never a sibling id."""

        if target is None:
            return ""
        if target.resume_id:
            resolution = _mw().MainWindow._target_resume_resolution(self, target)
            if resolution.known and resolution.provider == provider:
                return target.resume_id
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is not None and target.work_id:
            try:
                candidate = str(
                    coordinator.resume_id(target.work_id, provider) or ""
                )
            except Exception as exc:
                _log.warning("could not resolve execution participant: %s", exc)
            else:
                resolution = _mw().MainWindow._resolve_resume_id(
                    self,
                    candidate,
                    target.project,
                )
                if resolution.known and resolution.provider == provider:
                    return candidate
        return ""

    def _execution_target_lock_reason(self) -> str:
        """Return why Execution/send must be view-only for this target."""

        if _mw().MainWindow._execution_target_is_read_only(self):
            return "read-only"
        target = self._next_chat
        if target is None:
            return ""
        if target.resume_id:
            resolution = _mw().MainWindow._target_resume_resolution(self, target)
            if resolution.conflicted:
                return "conflict"
            if not resolution.known:
                return "unknown"

        # In a multi-provider Work, the transcript being viewed and the
        # participant that will execute can be different. Validate the latter
        # too so controls never advertise a writable Claude/GPT target only to
        # reject its stale or corrupt native binding at send time.
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is not None and target.work_id:
            try:
                candidate = str(
                    coordinator.resume_id(
                        target.work_id,
                        self._selected_provider(),
                    )
                    or ""
                )
            except Exception as exc:
                _log.warning("could not resolve execution participant: %s", exc)
                return "unknown"
            if candidate:
                candidate_resolution = _mw().MainWindow._resolve_resume_id(
                    self,
                    candidate,
                    target.project,
                )
                if candidate_resolution.conflicted:
                    return "conflict"
                if (
                    not candidate_resolution.known
                    or candidate_resolution.provider != self._selected_provider()
                ):
                    return "unknown"
        return ""

    def _execution_target_is_read_only(self) -> bool:
        target = self._next_chat
        return bool(
            target is not None
            and target.project is not None
            and target.project.read_only
        )

    def _stored_conversation_permission(self) -> str:
        target = self._next_chat
        provider = _mw().MainWindow._execution_target_provider(self)
        session_id = _mw().MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        if not session_id:
            return ""
        return self._conversation_perms.get(
            provider,
            session_id,
        )

    def _save_conversation_permission(
        self,
        provider: str,
        session_id: str,
        mode: str,
    ) -> bool:
        if not session_id:
            return False
        return self._conversation_perms.set(provider, session_id, mode)

    def _save_conversation_effort(
        self,
        provider: str,
        session_id: str,
        key: str,
        *,
        permission_mode: str,
    ) -> bool:
        if not session_id:
            return False
        # Ultra proactively delegates. Until Helios has a real Parallel
        # lease and family budget it is intentionally live-session-only;
        # old incident-era persisted Ultra records are quarantined on resume.
        if provider == model_catalog.PROVIDER_OPENAI and key == "ultra":
            key = ""
        return self._conversation_perms.set_effort(
            provider,
            session_id,
            key,
            permission_mode=permission_mode,
        )

    def _save_conversation_workflow(
        self,
        provider: str,
        session_id: str,
        mode: str,
        *,
        permission_mode: str,
    ) -> bool:
        if not session_id:
            return False
        return self._conversation_perms.set_workflow(
            provider,
            session_id,
            mode,
            permission_mode=permission_mode,
        )

    def _workflow_for_spawn(self, target: ChatTarget, provider: str) -> str:
        """Resolve workflow independently from the permission profile.

        Catalog discovery controls what the picker offers, but it must never
        silently turn a persisted Plan request into Default. The live OpenAI
        driver performs the authoritative capability check before thread bind
        and fails closed when Plan cannot be proved.
        """

        session_id = _mw().MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        stored = (
            self._conversation_perms.get_workflow(provider, session_id)
            if session_id
            else DEFAULT_WORKFLOW_MODE
        )
        requested = canonical_workflow_mode(
            getattr(self, "_staged_workflow_mode", "") or stored
        )
        if provider == model_catalog.PROVIDER_OPENAI:
            return requested
        return DEFAULT_WORKFLOW_MODE

    def _execution_settings_for_spawn(
        self,
        target: ChatTarget,
        provider: str,
    ) -> tuple[str, str]:
        """Resolve the exact permission/effort pair for a provider spawn."""

        session_id = _mw().MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        permission_mode = (
            self._staged_permission_mode
            or self._conversation_perms.get(provider, session_id)
            or self._effective_permission_mode(target.project.cwd)
        )
        # Enforce provider support and the HOME read-only rule at the spawn
        # boundary, independently of the settings store and displayed mode.
        permission_mode = effective_execution_mode(
            permission_mode,
            target.project.cwd,
            provider=provider,
        )
        effort_key, _effort_source = _mw().MainWindow._execution_effort_for_spawn(
            self,
            target,
            provider,
        )
        return permission_mode, effort_key

    def _execution_effort_for_spawn(
        self,
        target: ChatTarget,
        provider: str,
    ) -> tuple[str, str]:
        """Return the validated effort and the provenance of that exact value."""

        session_id = _mw().MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        stored_effort = self._conversation_perms.get_effort(provider, session_id)
        if provider == model_catalog.PROVIDER_OPENAI and stored_effort == "ultra":
            stored_effort = ""
        if self._staged_effort_key:
            requested_effort = self._staged_effort_key
            source = "Staged"
        elif stored_effort:
            requested_effort = stored_effort
            source = "Saved"
        elif provider == model_catalog.PROVIDER_OPENAI:
            remembered_openai = self._provider_efforts.get(provider, "")
            if remembered_openai == "ultra":
                remembered_openai = ""
            requested_effort = remembered_openai
            source = "Global default" if requested_effort else "Model default"
        else:
            requested_effort = (
                self._provider_efforts.get(provider, "") or self._effort_key
            )
            source = "Global default"
        if provider == model_catalog.PROVIDER_ANTHROPIC:
            allowed = {key for key, _label, _description in EFFORT_STOPS}
            if requested_effort in allowed:
                return requested_effort, source
            return DEFAULT_EFFORT, "Global default"
        else:
            entry = self._catalog_entries_by_id.get(self._model)
            advertised = (
                tuple(key for key, _description in entry.reasoning_efforts)
                if entry is not None
                else ()
            )
            allowed = set(advertised)
            if requested_effort in allowed:
                return requested_effort, source
            if (
                entry is not None
                and entry.default_effort in allowed
                and entry.default_effort != "ultra"
                and not (
                    provider == model_catalog.PROVIDER_OPENAI
                    and entry.default_effort == "low"
                    and ("high" in allowed or "max" in allowed)
                )
            ):
                return entry.default_effort, "Model default"
            # Ultra is an explicit one-session lease, never an ambient model
            # default. Prefer the familiar balanced level when advertised,
            # then the first non-Ultra capability.
            standard = tuple(key for key in advertised if key != "ultra")
            if "high" in standard:
                return "high", "Helios standard"
            return (standard[0], "Helios standard") if standard else ("", "")

    def _sync_execution_control(self) -> None:
        """Reflect the selected conversation's actual execution settings.

        A bound driver is authoritative: background conversations can retain
        different live modes even when they share a workspace.  When no child
        is bound, a durable conversation record (or unsaved-composer staging)
        wins; workspace/global policy is only the new-conversation fallback.
        """
        if getattr(self, "_destroyed", False):
            return
        # Keep the header Work indicator in step with participant/lead
        # changes. Guarded: mocked-window test harnesses borrow this method
        # without the real header widgets.
        _update_work = getattr(self, "_update_work_indicator", None)
        if _update_work is not None:
            _update_work()
        _mw().MainWindow._sync_stuck_attempt_banner(self)
        toolbar = getattr(self, "_chat_toolbar", None)
        if toolbar is None:
            return

        driver = self._driver
        lock_reason = _mw().MainWindow._execution_target_lock_reason(self)
        live = (
            not lock_reason
            and driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        cwd = self._current_project_cwd()
        target = self._next_chat
        viewed_resolution = _mw().MainWindow._target_resume_resolution(self, target)
        provider = (
            self._driver_provider(driver)
            if live
            else _mw().MainWindow._execution_target_provider(self)
        )
        # A read-only historical row has no executor, but its durable values
        # and provider label should still describe the transcript being viewed.
        if lock_reason == "read-only" and viewed_resolution.known:
            provider = viewed_resolution.provider
        session_id = _mw().MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        stored_mode = (
            self._conversation_perms.get(provider, session_id)
            if provider and session_id
            else ""
        )
        stored_settings = (
            self._conversation_perms.get_settings(provider, session_id)
            if provider and session_id
            else None
        )
        stored_workflow = (
            str(getattr(stored_settings, "workflow_mode", "") or "")
            if stored_settings is not None
            else ""
        )
        fallback_method = getattr(self, "_permission_fallback", None)
        if callable(fallback_method):
            fallback_mode, fallback_source = fallback_method(cwd)
        else:
            fallback_mode = self._effective_permission_mode(cwd)
            fallback_source = "default"
        fallback_label = {
            "home-plan": "HOME safety",
            "legacy-bypass-retired": "Legacy retired",
            "legacy-invalid": "Legacy invalid",
            "legacy": "Legacy safeguard",
        }.get(fallback_source, "Global default")
        live_mode = (
            str(getattr(driver, "permission_mode", "") or "") if live else ""
        )
        mode = (
            live_mode
            or self._staged_permission_mode
            or stored_mode
            or fallback_mode
        )
        if provider:
            # Show what the spawn will actually run. A confirmed global Bypass
            # narrows to Ask for a provider that cannot select it; the chip
            # said "Bypass" for a GPT chat that was about to prompt.
            mode = effective_execution_mode(mode, cwd, provider=provider)
        if _home_execution_locked(cwd):
            mode = "plan"
        permission_source = (
            "HOME safety"
            if _home_execution_locked(cwd)
            else "Live"
            if live_mode
            else "Staged"
            if self._staged_permission_mode
            else "Saved"
            if stored_mode
            else fallback_label
        )
        toolbar.set_permission_mode(mode)

        live_workflow = (
            str(getattr(driver, "workflow_mode", "") or "") if live else ""
        )
        advertised_workflows = (
            tuple(getattr(driver, "supported_workflow_modes", ()) or ())
            if live and provider == model_catalog.PROVIDER_OPENAI
            else tuple(
                getattr(
                    self,
                    "_openai_workflow_modes",
                    (DEFAULT_WORKFLOW_MODE,),
                )
            )
            if provider == model_catalog.PROVIDER_OPENAI
            else (DEFAULT_WORKFLOW_MODE,)
        )
        workflow_options = workflow_modes_for_provider(
            provider,
            advertised=advertised_workflows,
        )
        set_workflow_options = getattr(toolbar, "set_workflow_options", None)
        if callable(set_workflow_options):
            set_workflow_options(workflow_options)
        workflow_mode = canonical_workflow_mode(
            live_workflow
            or getattr(self, "_staged_workflow_mode", "")
            or stored_workflow
            or DEFAULT_WORKFLOW_MODE
        )
        if workflow_mode not in workflow_options:
            workflow_mode = DEFAULT_WORKFLOW_MODE
        set_workflow_mode = getattr(toolbar, "set_workflow_mode", None)
        if callable(set_workflow_mode):
            set_workflow_mode(workflow_mode)
        workflow_source = (
            "Live"
            if live_workflow
            else "Staged"
            if getattr(self, "_staged_workflow_mode", "")
            else "Saved"
            if stored_workflow
            else "Default"
        )

        # A live conversation may carry a different effort from the provider
        # default (for example after switching between two background chats).
        configure_efforts = getattr(driver, "set_effort_catalog", None) if live else None
        if callable(configure_efforts):
            configure_efforts(self._catalog_entries_by_id.values())
        live_effort = str(getattr(driver, "effort_key", "") or "") if live else ""
        if live_effort:
            selected_effort = live_effort
            effort_source = "Live"
        elif target is not None and provider:
            selected_effort, effort_source = _mw().MainWindow._execution_effort_for_spawn(
                self,
                target,
                provider,
            )
        else:
            selected_provider = self._selected_provider()
            selected_effort = (
                self._provider_efforts.get(selected_provider, "")
                or self._effort_key
            )
            effort_source = "Global default"
        if selected_effort:
            toolbar.set_effort(selected_effort)

        if lock_reason == "conflict":
            scope = "Conflict · view only"
            scope_detail = (
                "Provider evidence conflicts; execution changes and sends are disabled."
            )
        elif lock_reason == "unknown":
            scope = "Unknown provider · view only"
            scope_detail = (
                "Provider ownership is unverified; execution changes and sends are disabled."
            )
        elif lock_reason == "read-only":
            viewed = model_catalog.PROVIDER_LABELS.get(viewed_resolution.provider, "Assistant")
            scope = f"Read-only · {viewed}"
            scope_detail = "This pooled transcript cannot execute on this machine."
        else:
            source = (
                permission_source
                if not selected_effort or permission_source == effort_source
                else "Mixed"
            )
            scope_detail = f"Permissions: {permission_source}"
            if workflow_mode != DEFAULT_WORKFLOW_MODE:
                scope_detail += f"; Workflow: {workflow_source}"
            if selected_effort:
                scope_detail += f"; Reasoning: {effort_source}"
            executor = model_catalog.PROVIDER_LABELS.get(provider, "Assistant")
            scope = f"{source} · {executor}"
            if (
                viewed_resolution.known
                and provider
                and viewed_resolution.provider != provider
            ):
                short_source = {
                    "Global default": "Global",
                    "Legacy safeguard": "Legacy",
                    "Legacy retired": "Retired",
                    "Legacy invalid": "Invalid",
                }.get(source, source)
                viewed = model_catalog.PROVIDER_LABELS.get(viewed_resolution.provider, "Assistant")
                scope = f"{short_source} · {executor} · view {viewed}"
        toolbar.set_execution_scope(scope, scope_detail)
        toolbar.set_execution_sensitive(not bool(lock_reason))

        toolbar.set_execution_pending(
            not lock_reason
            and _mw().MainWindow._execution_change_pending_for(self, driver)
        )
        _mw().MainWindow._sync_agent_command_capabilities(self)

    def _on_toolbar_permission_changed(self, _toolbar, mode: str) -> None:
        if mode not in PERMISSION_MODES or self._destroyed:
            self._sync_execution_control()
            return
        if _mw().MainWindow._execution_target_lock_reason(self):
            self._sync_execution_control()
            return
        target = self._next_chat
        if target is None or target.project is None:
            self._sync_execution_control()
            return
        if _home_execution_locked(target.project.cwd) and mode != "plan":
            self._toast(
                "HOME chats use read-only permissions. Select a project folder "
                "before granting file changes."
            )
            self._sync_execution_control()
            return
        driver = self._driver
        live = (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        if not live:
            provider = _mw().MainWindow._execution_target_provider(self)
            session_id = _mw().MainWindow._execution_target_session_id(
                self,
                target,
                provider,
            )
            if session_id:
                saved = _mw().MainWindow._save_conversation_permission(
                    self,
                    provider,
                    session_id,
                    mode,
                )
                if not saved:
                    self._toast("Could not save permissions for this conversation.")
                    self._sync_execution_control()
                    return
                self._staged_permission_mode = ""
            else:
                self._staged_permission_mode = mode
            # No toast: the toolbar's permission label IS the confirmation,
            # and a banner per change queues over the composer
            # (2026-08-22). Only failures and partial saves toast.
            self._sync_execution_control()
            return

        changes = self._permission_changes
        if driver in changes:
            self._sync_execution_control()
            return
        token = object()
        changes[driver] = token
        provider = self._driver_provider(driver)
        session_id = str(
            getattr(driver, "session_id", "")
            or _mw().MainWindow._execution_target_session_id(self, target, provider)
        )
        self._sync_execution_control()

        def applied(success: bool, detail: str = "") -> None:
            if self._permission_changes.get(driver) is not token:
                return
            del self._permission_changes[driver]
            durable_saved = True
            if success:
                durable_id = str(getattr(driver, "session_id", "") or session_id)
                if durable_id:
                    durable_saved = _mw().MainWindow._save_conversation_permission(
                        self,
                        provider,
                        durable_id,
                        mode,
                    )
                else:
                    # A fresh process can ACK before system/init gives it a
                    # native id. session-started persists this attached value.
                    driver._helios_staged_permission_mode = mode
            restart_required = bool(
                not success
                and getattr(driver, "execution_restart_required", False)
            )
            if restart_required:
                self._teardown_driver(driver)
            if getattr(self, "_destroyed", False):
                return
            if success and durable_saved:
                pass  # see above: the toolbar label is the confirmation
            elif success:
                self._toast(
                    "Permissions changed for the running conversation, but "
                    "could not be saved for restart.",
                    timeout=6,
                )
            else:
                message = detail or "the provider rejected the change"
                self._toast(f"Could not change permissions: {message}", timeout=6)
            self._sync_execution_control()

        try:
            accepted = driver.set_permission_mode(mode, applied)
        except Exception as exc:
            _log.exception("permission change failed")
            applied(False, str(exc))
            return
        if not accepted and self._permission_changes.get(driver) is token:
            applied(False, "the conversation is not idle")

    def _on_toolbar_workflow_changed(self, _toolbar, mode: str) -> None:
        mode = canonical_workflow_mode(mode)
        if self._destroyed or _mw().MainWindow._execution_target_lock_reason(self):
            self._sync_execution_control()
            return
        target = self._next_chat
        if target is None or target.project is None:
            self._sync_execution_control()
            return
        driver = self._driver
        live = (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        provider = (
            self._driver_provider(driver)
            if live
            else _mw().MainWindow._execution_target_provider(self)
        )
        advertised = (
            tuple(getattr(driver, "supported_workflow_modes", ()) or ())
            if live and provider == model_catalog.PROVIDER_OPENAI
            else self._openai_workflow_modes
            if provider == model_catalog.PROVIDER_OPENAI
            else (DEFAULT_WORKFLOW_MODE,)
        )
        if not provider_allows_workflow(
            provider,
            mode,
            advertised=advertised,
        ):
            self._toast("This provider does not advertise that workflow.")
            self._sync_execution_control()
            return

        session_id = _mw().MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        permission_mode = (
            self._conversation_perms.get(provider, session_id)
            or self._staged_permission_mode
            or self._effective_permission_mode(target.project.cwd)
        )
        if not live:
            if session_id:
                if not _mw().MainWindow._save_conversation_workflow(
                    self,
                    provider,
                    session_id,
                    mode,
                    permission_mode=permission_mode,
                ):
                    self._toast("Could not save workflow for this conversation.")
                    self._sync_execution_control()
                    return
                self._staged_workflow_mode = ""
            else:
                self._staged_workflow_mode = mode
            self._sync_execution_control()
            return

        changes = self._workflow_changes
        if driver in changes:
            self._sync_execution_control()
            return
        token = object()
        changes[driver] = token
        self._sync_execution_control()

        def applied(success: bool, detail: str = "") -> None:
            if self._workflow_changes.get(driver) is not token:
                return
            del self._workflow_changes[driver]
            durable_saved = True
            if success:
                durable_id = str(getattr(driver, "session_id", "") or session_id)
                if durable_id:
                    durable_saved = _mw().MainWindow._save_conversation_workflow(
                        self,
                        provider,
                        durable_id,
                        mode,
                        permission_mode=str(
                            getattr(driver, "permission_mode", "")
                            or permission_mode
                        ),
                    )
                else:
                    driver._helios_staged_workflow_mode = mode
            if not getattr(self, "_destroyed", False):
                if success and not durable_saved:
                    self._toast(
                        "Workflow changed for the running conversation, but "
                        "could not be saved for restart.",
                        timeout=6,
                    )
                elif not success:
                    self._toast(
                        "Could not change workflow: "
                        + (detail or "the provider rejected the change"),
                        timeout=6,
                    )
                self._sync_execution_control()

        setter = getattr(driver, "set_workflow_mode", None)
        if not callable(setter):
            applied(False, "the provider has no native workflow control")
            return
        try:
            accepted = setter(mode, applied)
        except Exception as exc:
            _log.exception("workflow change failed")
            applied(False, str(exc))
            return
        if not accepted and self._workflow_changes.get(driver) is token:
            applied(False, "the conversation rejected the workflow")

    def _sync_effort_sensitivity(self) -> None:
        """Render only the reasoning efforts the selected model supports."""
        # The model picker is a spawn-time choice.  While a same-provider
        # conversation is live it may therefore differ from the newly staged
        # picker value; derive capabilities from the process we will actually
        # update, not from a future model selection.
        execution_model = self._model
        driver = self._driver
        if (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        ):
            execution_model = str(getattr(driver, "model", "") or execution_model)
        provider = model_catalog.provider_for(execution_model)
        target = self._next_chat
        session_id = _mw().MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        stored_effort = ""
        if session_id and _mw().MainWindow._execution_target_provider(self) == provider:
            stored_effort = self._conversation_perms.get_effort(
                provider,
                session_id,
            )
        live = (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        configure_efforts = getattr(driver, "set_effort_catalog", None) if live else None
        if callable(configure_efforts):
            configure_efforts(self._catalog_entries_by_id.values())
        live_effort = str(getattr(driver, "effort_key", "") or "") if live else ""
        conversation_effort = (
            live_effort or self._staged_effort_key or stored_effort
        )
        conversation_owned = live or bool(self._staged_effort_key or stored_effort)
        remembered = self._provider_efforts.get(provider, "")
        if provider == model_catalog.PROVIDER_OPENAI and remembered == "ultra":
            remembered = ""
        if provider == model_catalog.PROVIDER_OPENAI:
            selected_effort = conversation_effort or remembered
        else:
            selected_effort = (
                conversation_effort or remembered or self._effort_key
            )
        if provider == model_catalog.PROVIDER_OPENROUTER:
            # The driver has mapped Helios effort keys onto OpenRouter's
            # unified `reasoning` parameter since the provider landed, with
            # tests, and refuses when the pinned endpoint does not declare it;
            # only this control was ever disabled. So offer the choices for a
            # model whose catalog row declares `reasoning`, and let the
            # driver's endpoint check stay authoritative — it has to be,
            # because `require_parameters: true` reroutes a request carrying a
            # parameter the endpoint does not advertise.
            entry = self._catalog_entries_by_id.get(execution_model)
            if entry is None or not entry.reasoning_efforts:
                self._chat_toolbar.set_effort_sensitive(False)
                self._sync_execution_control()
                return
            effective = self._chat_toolbar.set_effort_options(
                entry.reasoning_efforts,
                default_effort=entry.default_effort,
                selected_effort=selected_effort,
            )
        elif provider in (model_catalog.PROVIDER_OPENAI, model_catalog.PROVIDER_GOOGLE, model_catalog.PROVIDER_MUSE, model_catalog.PROVIDER_KIMI):
            entry = self._catalog_entries_by_id.get(execution_model)
            if entry is None:
                self._chat_toolbar.set_effort_sensitive(False)
                self._sync_execution_control()
                return
            if not entry.reasoning_efforts:
                if not conversation_owned:
                    self._effort_key = ""
                    self._provider_efforts[provider] = ""
                    self._ui_state.set(f"effort_{provider}", "")
                    self._ui_state.set("effort_level", "")
                self._chat_toolbar.set_effort_sensitive(False)
                self._sync_execution_control()
                return
            effective = self._chat_toolbar.set_effort_options(
                entry.reasoning_efforts,
                default_effort=entry.default_effort,
                selected_effort=selected_effort,
            )
        else:
            # Prefer what the CLI stated for this model over the fixed
            # six-stop list. Only a live driver can have been told, and only
            # the driver we would actually update — which is the same process
            # `execution_model` was derived from above.
            supported: list[str] | None = None
            if live and hasattr(driver, "supported_effort_levels"):
                try:
                    supported = driver.supported_effort_levels()
                except Exception as exc:  # pragma: no cover - defensive
                    _log.warning("could not read supported effort levels: %s", exc)
            effective = self._chat_toolbar.set_anthropic_effort_options(
                selected_effort,
                supported_levels=supported,
            )
        if effective and not conversation_owned:
            if provider == model_catalog.PROVIDER_OPENAI:
                if effective != "ultra":
                    self._provider_efforts[provider] = effective
                    self._ui_state.set(f"effort_{provider}", effective)
            else:
                self._effort_key = effective
                self._provider_efforts[provider] = effective
                self._ui_state.set(f"effort_{provider}", effective)
                self._ui_state.set("effort_level", effective)
        # Re-apply the selected conversation after rebuilding the slider.  A
        # catalog refresh must never replace a live/staged/stored choice with
        # the provider-wide fallback used for brand-new conversations.
        self._sync_execution_control()

    def _show_native_execution_binding(self, project, session_id: str) -> None:
        """Make the executor's own transcript visible without losing staging."""

        staged_permission = self._staged_permission_mode
        staged_effort = self._staged_effort_key
        staged_workflow = self._staged_workflow_mode
        current = self._driver
        current_session_id = str(
            getattr(current, "session_id", "")
            or (
                getattr(current, "_helios_expected_resume_id", "")
                if getattr(current, "is_initializing", False)
                else ""
            )
            or ""
        )
        if current is not None and current_session_id == session_id:
            # Force the full selection path even if an earlier degraded route
            # raw-bound this driver while a sibling transcript stayed visible.
            self._bind_visible_driver(None)
        session = _mw().MainWindow._session_for_native_binding(project, session_id)
        self._on_session_selected(self._sessions, session)
        self._staged_permission_mode = staged_permission
        self._staged_effort_key = staged_effort
        self._staged_workflow_mode = staged_workflow
        self._sync_execution_control()

    def _show_fresh_execution_binding(self, target: ChatTarget) -> None:
        """Show a fresh selected-provider participant while preserving choices."""

        staged_permission = self._staged_permission_mode
        staged_effort = self._staged_effort_key
        staged_workflow = self._staged_workflow_mode
        self._show_fresh_chat_ui(
            target.project,
            work_id=target.work_id,
        )
        self._staged_permission_mode = staged_permission
        self._staged_effort_key = staged_effort
        self._staged_workflow_mode = staged_workflow
        self._sync_execution_control()

    def _work_execution_block_reason(self, work_id: str) -> str:
        """Return a fail-closed reason when a Work may not start another turn."""

        from helios.backend.process import child_registry

        if getattr(self, "_orphan_cleanup_block_reason", "") or child_registry.has_uncleaned_aborts():
            from helios.window_recovery import WindowRecoveryMixin

            if not WindowRecoveryMixin._ensure_orphan_cleanup_cleared(self):
                return str(
                    getattr(self, "_orphan_cleanup_block_reason", "")
                    or child_registry.incomplete_cleanup_reason()
                    or "Helios could not confirm previous agent processes stopped."
                )
        if not work_id:
            return (
                "Helios could not establish a bounded Work, so it did not "
                "send your message."
            )
        blocked = getattr(self, "_budget_blocked_work_ids", set())
        if work_id in blocked:
            return "This Work reached its execution budget. Start a new bounded Work."
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            return (
                "Helios could not verify this Work's execution state, so it "
                "did not send your message."
            )
        try:
            return str(coordinator.execution_block_reason(work_id) or "")
        except Exception as exc:
            _log.warning("could not verify Work execution budget: %s", exc)
            return (
                "Helios could not verify this Work's execution budget, so it "
                "did not send your message."
            )

    def _execution_guard_for_driver(self, drv) -> str:
        work_id = str(
            getattr(drv, "_helios_work_id", "")
            or getattr(drv, "_helios_work_id_hint", "")
            or ""
        )
        block_reason = _mw().MainWindow._work_execution_block_reason(self, work_id)
        if block_reason:
            return block_reason

        coordinator = getattr(self, "_work_coordinator", None)
        provider = str(
            getattr(drv, "_helios_participant_provider", "")
            or self._driver_provider(drv)
            or ""
        )
        try:
            participant = coordinator.participant(work_id, provider)
        except Exception as exc:
            _log.warning("could not verify Work participant binding: %s", exc)
            participant = None
        if participant is None:
            return (
                "Helios could not verify this Work participant binding. "
                "The message was not sent."
            )
        if (
            str(getattr(drv, "_helios_participant_id", "") or "")
            != participant.participant_id
            or getattr(drv, "_helios_participant_generation", None)
            != participant.generation
        ):
            return (
                "This Work participant binding changed. Reopen the current "
                "participant before sending another message."
            )
        driver_native_id = str(
            getattr(drv, "session_id", "")
            or getattr(drv, "_helios_expected_resume_id", "")
            or ""
        )
        if (
            participant.native_id
            and driver_native_id
            and participant.native_id != driver_native_id
        ):
            return (
                "This Work participant now points to a different native "
                "conversation. Reopen it before sending another message."
            )
        return ""

    def _start_execution_attempt_for_driver(self, drv) -> tuple[str, str]:
        """Atomically reserve this driver's durable execution lane."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            return "", (
                "Helios execution admission is unavailable. "
                "The message was not sent."
            )
        work_id = str(getattr(drv, "_helios_work_id", "") or "")
        participant_id = str(
            getattr(drv, "_helios_participant_id", "") or ""
        )
        provider = str(
            getattr(drv, "_helios_participant_provider", "") or ""
        )
        generation = getattr(drv, "_helios_participant_generation", None)
        if not work_id or not participant_id or not provider or not generation:
            return "", (
                "Helios could not verify this Work participant before execution. "
                "The message was not sent."
            )
        metadata = {
            "model": str(getattr(drv, "model", "") or ""),
            "effort": str(
                getattr(drv, "effort_key", "")
                or getattr(drv, "_effort", "")
                or ""
            ),
            "transport": str(getattr(drv, "display_name", "") or ""),
        }
        # The lease keys on the driver's OWN cwd and mode, which are the exact
        # values it will execute with — not the composer's current selection,
        # which may already have moved on.
        workspace_root = str(getattr(drv, "cwd", "") or getattr(drv, "_cwd", "") or "")
        permission_mode = str(getattr(drv, "permission_mode", "") or "")
        try:
            attempt = coordinator.start_execution_attempt(
                work_id=work_id,
                participant_id=participant_id,
                provider=provider,
                participant_generation=int(generation),
                metadata=metadata,
                workspace_root=workspace_root,
                permission_mode=permission_mode,
            )
        except ExecutionAdmissionError as exc:
            return "", str(exc)
        except Exception as exc:
            _log.warning("could not reserve Helios execution lane: %s", exc)
            return "", (
                "Helios could not reserve the execution lane. "
                "The message was not sent."
            )
        return attempt.attempt_id, ""

    def _finish_execution_attempt_for_driver(
        self,
        _drv,
        attempt_id: str,
        status: str,
        terminal_reason: str,
    ) -> None:
        """Commit terminal accounting before the driver surfaces its result."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        coordinator.finish_execution_attempt(
            attempt_id,
            status=status,
            terminal_reason=terminal_reason,
        )

    def _record_execution_dispatch_for_driver(
        self,
        _drv,
        attempt_id: str,
        evidence: ExecutionDispatchEvidence,
    ) -> None:
        """Hash the exact wire prompt before any provider I/O can occur."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        _mw().MainWindow._verify_execution_native_binding(
            self,
            _drv,
            evidence.native_binding_id,
        )
        coordinator.record_execution_dispatch(
            attempt_id,
            wire_prompt_text=evidence.wire_prompt_text,
            provider_request_key=evidence.provider_request_key,
            pending_capsule=evidence.pending_capsule,
        )

    def _record_execution_acceptance_for_driver(
        self,
        _drv,
        attempt_id: str,
        evidence: ExecutionAcceptanceEvidence,
    ) -> None:
        """Persist provider-native turn identity without releasing the slot."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        _mw().MainWindow._verify_execution_native_binding(
            self,
            _drv,
            evidence.native_binding_id,
            attempt_id=attempt_id,
        )
        coordinator.record_execution_acceptance(
            attempt_id,
            accepted_turn_id=evidence.accepted_turn_id,
            provider_request_key=evidence.provider_request_key,
        )
        _mw().MainWindow._refresh_visible_handoff_manifest(self, _drv)

    def _verify_execution_native_binding(
        self,
        drv,
        native_binding_id: str,
        *,
        attempt_id: str = "",
    ) -> None:
        """Verify dispatch against current state, receipts against admission.

        A rebind fences future sends, but must not discard a terminal receipt
        for the already admitted generation and leave its lane held forever.
        Historical evidence never retags the driver or changes the binding.
        """

        native_binding_id = str(native_binding_id or "")
        if not native_binding_id:
            return
        if getattr(drv, "_helios_identity_confirmed", False) is not True:
            raise RuntimeError("provider-native identity is not confirmed")
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        work_id = str(getattr(drv, "_helios_work_id", "") or "")
        provider = str(getattr(drv, "_helios_participant_provider", "") or "")
        if attempt_id:
            attempt = coordinator.store.get_execution_attempt(attempt_id)
            if attempt is None or (
                attempt.work_id != work_id
                or attempt.provider != provider
                or attempt.participant_id
                != str(getattr(drv, "_helios_participant_id", "") or "")
                or attempt.participant_generation
                != getattr(drv, "_helios_participant_generation", None)
            ):
                raise RuntimeError("execution receipt does not match admitted driver")
            if attempt.native_binding_id:
                if attempt.native_binding_id != native_binding_id:
                    raise RuntimeError("provider-native identity conflicts with admission")
                return
            # A first native identity may arrive after dispatch. It still
            # requires the current generation before attaching a late id.
        participant = coordinator.participant(work_id, provider)
        if participant is None:
            raise RuntimeError("Work participant is unavailable")
        if participant.native_id and participant.native_id != native_binding_id:
            raise RuntimeError("provider-native identity conflicts with Work binding")
        if not participant.native_id:
            participant = coordinator.bind_participant(
                work_id,
                provider,
                native_id=native_binding_id,
                model=str(getattr(drv, "model", "") or ""),
            )
        if (
            participant.participant_id
            != str(getattr(drv, "_helios_participant_id", "") or "")
            or participant.generation
            != getattr(drv, "_helios_participant_generation", None)
        ):
            raise RuntimeError("provider-native binding changed participant generation")

    def _record_execution_stop_for_driver(
        self,
        _drv,
        attempt_id: str,
        evidence: ExecutionStopEvidence,
    ) -> None:
        """Persist a typed cancellation acknowledgement and queue disposition."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        _mw().MainWindow._verify_execution_native_binding(
            self,
            _drv,
            str(evidence.acknowledgement.get("native_id") or ""),
            attempt_id=attempt_id,
        )
        coordinator.record_execution_stop(
            attempt_id,
            acknowledgement=evidence.acknowledgement or None,
            queue_disposition=evidence.queue_disposition,
        )

    def _finish_execution_attempt_with_evidence_for_driver(
        self,
        _drv,
        attempt_id: str,
        evidence: ExecutionTerminalEvidence,
    ) -> None:
        """Translate typed provider evidence into the durable storage schema."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        _mw().MainWindow._verify_execution_native_binding(
            self,
            _drv,
            evidence.native_id,
            attempt_id=attempt_id,
        )
        terminal_receipt = None
        if evidence.evidence_type in {"verified_rejection", "provider_terminal"}:
            terminal_receipt = {
                "evidence_type": evidence.evidence_type,
                "provider_status": evidence.provider_status,
            }
            if evidence.request_id:
                terminal_receipt["request_id"] = evidence.request_id
            if evidence.turn_id:
                terminal_receipt["turn_id"] = evidence.turn_id
            if evidence.response_id:
                terminal_receipt["response_id"] = evidence.response_id
            if evidence.native_id:
                terminal_receipt["native_id"] = evidence.native_id
        coordinator.finish_execution_attempt(
            attempt_id,
            status=evidence.status,
            terminal_reason=evidence.reason_code,
            terminal_receipt=terminal_receipt,
            usage=evidence.usage,
            cost_micro_usd=evidence.cost_micro_usd,
            stop_acknowledgement=evidence.stop_acknowledgement,
            queue_disposition=evidence.queue_disposition,
        )
        _mw().MainWindow._refresh_visible_handoff_manifest(self, _drv)

    def _execution_change_pending_for(self, driver) -> bool:
        """Whether ``driver`` is waiting for an execution-setting ACK."""

        if driver is None:
            return False
        try:
            tracked = (
                driver in getattr(self, "_permission_changes", {})
                or driver in getattr(self, "_effort_changes", {})
                or driver in getattr(self, "_workflow_changes", {})
                or driver in getattr(self, "_model_changes", {})
            )
        except TypeError:
            tracked = False
        return tracked or bool(getattr(driver, "model_change_pending", False))

    def _restore_blocked_execution_send(self, text: str) -> bool:
        """Restore a submit that could not be routed to an active driver.

        Composer clears after its synchronous ``send`` signal returns, so the
        restoration must run on the next main-loop tick.  Preserve anything
        the user managed to type meanwhile rather than overwriting it.
        """

        if self._destroyed:
            return False
        current = self._composer.current_text().strip()
        restored = text if not current else f"{text}\n\n{current}"
        self._composer.set_text(restored)
        self._composer.grab_input_focus()
        return False

    def _on_driver_permission_mode_changed(self, drv, mode: str) -> None:
        """The CLI changed mode itself; mirror it and make it durable.

        Re-asserting through the toolbar path sends `set_permission_mode`
        (so the CLI and the toolbar agree even after a plain plan approval,
        where the CLI's own choice of mode is not on the wire) and saves the
        conversation's execution settings on the ACK.
        """
        if self._destroyed or not self._drv_is_current(drv):
            return
        if mode not in PERMISSION_MODES:
            return
        toolbar = getattr(self, "_chat_toolbar", None)
        if toolbar is not None:
            toolbar.set_permission_mode(mode)
        self._on_toolbar_permission_changed(toolbar, mode)

    def _on_toolbar_effort_changed(self, _toolbar, key: str) -> None:
        if self._destroyed:
            return
        if _mw().MainWindow._execution_target_lock_reason(self):
            self._sync_execution_control()
            return
        target = self._next_chat
        if target is None or target.project is None:
            self._sync_execution_control()
            return
        driver = self._driver
        live = (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        provider = (
            self._driver_provider(driver)
            if live
            else _mw().MainWindow._execution_target_provider(self)
        )
        if live and not callable(getattr(driver, "set_effort", None)):
            # Gemini/Muse take reasoning only at spawn: save it like an idle
            # chat, then resume on it (was a raw AttributeError toast).
            live = False
            _mw().MainWindow._retire_for_respawn(self, driver, "set_effort")

        if not live:
            _permission, previous_effort = _mw().MainWindow._execution_settings_for_spawn(
                self,
                target,
                provider,
            )
            session_id = _mw().MainWindow._execution_target_session_id(
                self,
                target,
                provider,
            )
            if provider == model_catalog.PROVIDER_OPENAI and key == "ultra":
                # Ultra can proactively delegate. Honor an explicit selection
                # for this one spawn, but never turn it into a sticky setting
                # for future Works. This also keeps the success toast truthful:
                # the staged value is what `_execution_effort_for_spawn` uses.
                self._staged_effort_key = key
                self._sync_execution_control()
                if previous_effort != key:
                    self._toast(
                        "Reasoning set to Ultra for this conversation "
                        "(one session only)."
                    )
                return
            if session_id:
                permission_mode = _mw().MainWindow._stored_conversation_permission(
                    self
                ) or self._effective_permission_mode(target.project.cwd)
                saved = _mw().MainWindow._save_conversation_effort(
                    self,
                    provider,
                    session_id,
                    key,
                    permission_mode=permission_mode,
                )
                if not saved:
                    self._toast("Could not save reasoning for this conversation.")
                    self._sync_execution_control()
                    return
                self._staged_effort_key = ""
            else:
                self._staged_effort_key = key
            # No toast — same reason as the permission path: the toolbar's
            # reasoning label already shows the new value.
            self._sync_execution_control()
            return

        if str(getattr(driver, "effort_key", "") or "") == key:
            session_id = str(
                getattr(driver, "session_id", "")
                or _mw().MainWindow._execution_target_session_id(self, target, provider)
            )
            if session_id:
                _mw().MainWindow._save_conversation_effort(
                    self,
                    provider,
                    session_id,
                    key,
                    permission_mode=str(
                        getattr(driver, "permission_mode", "")
                        or self._effective_permission_mode(target.project.cwd)
                    ),
                )
            self._sync_execution_control()
            return

        changes = self._effort_changes
        if driver in changes:
            self._sync_execution_control()
            return
        token = object()
        changes[driver] = token
        session_id = str(
            getattr(driver, "session_id", "")
            or _mw().MainWindow._execution_target_session_id(self, target, provider)
        )
        self._sync_execution_control()

        def applied(success: bool, detail: str = "") -> None:
            if self._effort_changes.get(driver) is not token:
                return
            del self._effort_changes[driver]
            durable_saved = True
            if success:
                durable_id = str(getattr(driver, "session_id", "") or session_id)
                if durable_id:
                    durable_saved = _mw().MainWindow._save_conversation_effort(
                        self,
                        provider,
                        durable_id,
                        key,
                        permission_mode=str(
                            getattr(driver, "permission_mode", "")
                            or self._effective_permission_mode(target.project.cwd)
                        ),
                    )
                else:
                    driver._helios_staged_effort_key = key
            restart_required = bool(
                not success
                and getattr(driver, "execution_restart_required", False)
            )
            if restart_required:
                self._teardown_driver(driver)
            if getattr(self, "_destroyed", False):
                return
            if success and durable_saved:
                pass  # see above: the toolbar label is the confirmation
            elif success:
                self._toast(
                    "Reasoning changed for the running conversation, but could "
                    "not be saved for restart.",
                    timeout=6,
                )
            else:
                message = detail or "the provider rejected the change"
                self._toast(f"Could not change reasoning: {message}", timeout=6)
            self._sync_execution_control()

        try:
            accepted = driver.set_effort(key, applied)
        except Exception as exc:
            _log.exception("reasoning change failed")
            applied(False, str(exc))
            return
        if not accepted and self._effort_changes.get(driver) is token:
            applied(False, "the conversation is not idle")
