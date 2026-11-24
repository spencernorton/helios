"""Execution lane recovery, stuck-turn reconciliation, budget clearing, and
bounded Google slot capacity retry, split out of main_window.

Behaviour-preserving mixin: every method runs on the composed MainWindow
instance so driver-manager, WorkCoordinator, and UI state resolve identically
while isolating recovery workflows into one auditable module.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from helios.backend import model_catalog
from helios.backend.project_perms import canonical_cwd
from helios.log import get_logger
from helios.widgets.activity_indicator import STATE_THINKING

_log = get_logger("window")


class WindowRecoveryMixin:
    """Orphan lane cleanup, operator stuck-turn recovery, budget clearing, and slot retries."""

    _CAPACITY_RETRY_TIMEOUT_SECONDS = 6.0
    _CAPACITY_RETRY_POLL_MS = 200

    def _reclaim_orphaned_execution_lanes(self) -> None:
        """Free execution lanes a Helios that exited mid-turn still holds.

        Startup only, and it must stay that way: a Helios killed mid-turn
        leaves its attempt 'running', and per-Work admission then refuses every
        later message in that chat with "already executing" — permanently,
        since the driver that would produce a terminal receipt died with the
        process. Nothing is live this early, so any running row is an orphan.
        """

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            return
        try:
            orphans = coordinator.reclaim_orphaned_execution_attempts()
        except Exception as exc:
            _log.warning("could not reclaim orphaned execution attempts: %s", exc)
            return
        for orphan in orphans:
            _log.warning(
                "released execution attempt %s orphaned by a previous Helios",
                orphan.attempt_id,
            )

    def _on_clear_budget_block(self) -> None:
        """Reopen the selected Work after the budget breaker closed it.

        The breaker stays one-way for a process respawn; a human choice is the
        renewal path. Without this, one trip abandoned the conversation — which
        is what a notional dollar figure did on 2026-08-05.
        """
        from helios.main_window import MainWindow

        if self._destroyed:
            return
        work_id = str(self._current_work_id() or "")
        if not work_id:
            self._toast("Select a Work first — there is nothing to clear.")
            return

        blocked = getattr(self, "_budget_blocked_work_ids", None)
        latched = isinstance(blocked, set) and work_id in blocked
        coordinator = getattr(self, "_work_coordinator", None)
        cleared = False
        if coordinator is not None:
            try:
                work = coordinator.clear_budget_exhausted(
                    work_id=work_id,
                    reason="operator cleared the budget block from the menu",
                )
                cleared = getattr(work, "status", "") == "active"
            except Exception as exc:
                _log.warning("could not clear Work budget block: %s", exc)
                self._toast(f"Could not clear the budget block: {exc}")
                return

        # The in-memory latch closes the gate even when the store is healthy, so
        # it has to be released too or the Work stays blocked until restart.
        if latched:
            blocked.discard(work_id)

        if cleared or latched:
            self._toast("Budget block cleared. This Work can run again.")
        else:
            self._toast("This Work is not blocked by the budget breaker.")
        MainWindow._sync_execution_control(self)

    def _orphaned_attempts_for_work(self, work_id: str) -> list:
        """Return durable running attempts for work_id with no live running driver."""

        if not work_id:
            return []
        coordinator = getattr(self, "_work_coordinator", None)
        active_fn = getattr(coordinator, "active_attempts_for_work", None)
        if not callable(active_fn):
            return []
        try:
            attempts = list(active_fn(work_id) or ())
        except Exception as exc:
            _log.warning("could not inspect active attempts for %s: %s", work_id, exc)
            return []
        if not attempts:
            return []
        live_attempt_ids: set[str] = set()
        manager = getattr(self, "_driver_manager", None)
        drivers_fn = getattr(manager, "drivers_for_shutdown", None)
        if callable(drivers_fn):
            for drv in drivers_fn():
                aid = str(getattr(drv, "execution_attempt_id", "") or "")
                if aid and getattr(drv, "is_running", False):
                    live_attempt_ids.add(aid)
        return [att for att in attempts if att.attempt_id not in live_attempt_ids]

    def _sync_stuck_attempt_banner(self) -> None:
        """Reveal an in-UI unblock banner when the viewed Work has an orphaned attempt."""

        banner = getattr(self, "_stuck_attempt_banner", None)
        if banner is None or getattr(self, "_destroyed", False):
            return
        current_work = getattr(self, "_current_work_id", None)
        work_id = str(current_work() or "") if callable(current_work) else ""
        orphaned = WindowRecoveryMixin._orphaned_attempts_for_work(self, work_id)
        if not orphaned:
            banner.set_revealed(False)
            return
        provider = orphaned[0].provider
        label = model_catalog.PROVIDER_LABELS.get(provider, provider or "provider")
        banner.set_title(
            f"This conversation is blocked by an unfinished {label} turn (not auto-replayed)."
        )
        banner.set_revealed(True)

    def _reconcile_orphaned_attempts_confirmed(
        self, work_id: str, attempt_ids: list[str]
    ) -> int:
        """Execute operator-confirmed reconciliation without replaying any prompt."""

        coordinator = getattr(self, "_work_coordinator", None)
        reconcile_fn = getattr(coordinator, "reconcile_uncertain_attempt", None)
        if not callable(reconcile_fn) or not work_id or not attempt_ids:
            return 0
        still_orphaned = {
            att.attempt_id
            for att in WindowRecoveryMixin._orphaned_attempts_for_work(self, work_id)
        }
        cleared = 0
        for aid in attempt_ids:
            if aid not in still_orphaned:
                continue
            reconcile_fn(aid, reason="reconciled.operator_confirmed")
            cleared += 1
        return cleared

    def _on_reconcile_stuck_turn(self) -> None:
        """Confirm with the user before clearing an unfinished provider attempt."""
        from helios.main_window import MainWindow

        if getattr(self, "_destroyed", False):
            return
        work_id = str(self._current_work_id() or "")
        if not work_id:
            self._toast("Select a conversation first — there is no stuck turn to clear.")
            return
        orphaned = WindowRecoveryMixin._orphaned_attempts_for_work(self, work_id)
        if not orphaned:
            coordinator = getattr(self, "_work_coordinator", None)
            active_fn = getattr(coordinator, "active_attempts_for_work", None)
            active = list(active_fn(work_id) or ()) if callable(active_fn) else []
            if active:
                self._toast(
                    "A provider process is still running for this turn. Stop it first before clearing."
                )
            else:
                self._toast("This conversation has no stuck provider turn.")
            WindowRecoveryMixin._sync_stuck_attempt_banner(self)
            return
        attempt_ids = [att.attempt_id for att in orphaned]
        first = orphaned[0]
        label = model_catalog.PROVIDER_LABELS.get(first.provider, first.provider or "provider")
        details = f"{label} attempt {first.attempt_id} (started {first.started_at})"
        if len(orphaned) > 1:
            details += f" and {len(orphaned) - 1} more"
        dlg = Adw.AlertDialog.new(
            "Unblock this conversation?",
            (
                f"The previous {details} ended without a confirmed provider receipt.\n\n"
                "Clearing it marks the unfinished attempt as aborted so you can send a new message. "
                "Helios will not automatically replay the previous prompt."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("unblock", "Unblock conversation")
        dlg.set_response_appearance("unblock", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response != "unblock" or getattr(self, "_destroyed", False):
                return
            try:
                cleared = WindowRecoveryMixin._reconcile_orphaned_attempts_confirmed(
                    self, work_id, attempt_ids
                )
            except Exception as exc:
                _log.warning("could not reconcile stuck turn for %s: %s", work_id, exc)
                self._toast(f"Could not clear the stuck turn: {exc}")
                return
            if cleared:
                self._toast("Unfinished turn cleared. You can send a new message.")
            else:
                self._toast("No stuck turn needed clearing.")
            MainWindow._sync_execution_control(self)

        dlg.connect("response", on_response)
        dlg.present(self)

    def _stage_capacity_retry_send(self, text: str) -> None:
        """Retry a Gemini send automatically once an evicted idle slot closes."""

        WindowRecoveryMixin._cancel_capacity_retry_send(self, restore_draft=False)
        target = getattr(self, "_next_chat", None)
        cwd = (
            canonical_cwd(target.project.cwd)
            if target is not None and target.project is not None
            else ""
        )
        pending = {
            "text": text,
            "work_id": str(getattr(target, "work_id", "") or "") if target is not None else "",
            "resume_id": str(getattr(target, "resume_id", "") or "") if target is not None else "",
            "cwd": cwd,
            "provider": model_catalog.PROVIDER_GOOGLE,
            "model": str(getattr(self, "_model", "") or ""),
            "deadline": time.monotonic() + WindowRecoveryMixin._CAPACITY_RETRY_TIMEOUT_SECONDS,
            "timer_id": 0,
        }
        self._pending_capacity_send = pending
        composer = getattr(self, "_composer", None)
        if composer is not None:
            composer.set_busy(True)
        toolbar = getattr(self, "_chat_toolbar", None)
        if toolbar is not None:
            toolbar.set_busy(True)
        activity = getattr(self, "_activity", None)
        if activity is not None:
            activity.set_activity(STATE_THINKING)
        pending["timer_id"] = GLib.timeout_add(
            WindowRecoveryMixin._CAPACITY_RETRY_POLL_MS,
            lambda: WindowRecoveryMixin._maybe_retry_capacity_send(self, from_timer=True),
        )

    def _cancel_capacity_retry_send(self, *, restore_draft: bool = True) -> None:
        pending = getattr(self, "_pending_capacity_send", None)
        if not isinstance(pending, dict):
            return
        self._pending_capacity_send = None
        timer_id = int(pending.get("timer_id", 0) or 0)
        if timer_id:
            try:
                GLib.source_remove(timer_id)
            except Exception as exc:
                _log.debug("capacity retry timer already cleared: %s", exc)
        if getattr(self, "_destroyed", False):
            return
        clear_busy = getattr(self, "_clear_busy_ui", None)
        if callable(clear_busy) and getattr(self, "_driver", None) is None:
            clear_busy()
        text = str(pending.get("text", "") or "")
        if restore_draft and text:
            GLib.idle_add(self._restore_blocked_execution_send, text)

    def _maybe_retry_capacity_send(self, *, from_timer: bool = False) -> bool:
        from helios.main_window import MainWindow

        pending = getattr(self, "_pending_capacity_send", None)
        if not isinstance(pending, dict) or getattr(self, "_destroyed", False):
            return False
        target = getattr(self, "_next_chat", None)
        cwd = (
            canonical_cwd(target.project.cwd)
            if target is not None and target.project is not None
            else ""
        )
        matches_target = (
            target is not None
            and str(getattr(target, "work_id", "") or "") == pending["work_id"]
            and str(getattr(target, "resume_id", "") or "") == pending["resume_id"]
            and cwd == pending["cwd"]
            and self._selected_provider() == pending["provider"]
            and str(getattr(self, "_model", "") or "") == pending["model"]
        )
        if not matches_target:
            WindowRecoveryMixin._cancel_capacity_retry_send(self, restore_draft=True)
            return False
        manager = getattr(self, "_driver_manager", None)
        capacity = (
            manager.prepare_provider_start(
                pending["provider"], MainWindow._MAX_LIVE_GOOGLE_DRIVERS
            )
            if manager is not None
            else "busy"
        )
        if capacity == "ready":
            text = pending["text"]
            WindowRecoveryMixin._cancel_capacity_retry_send(self, restore_draft=False)
            MainWindow._on_composer_send(self, getattr(self, "_composer", None), text)
            return False
        if capacity == "busy" or time.monotonic() >= pending["deadline"]:
            WindowRecoveryMixin._cancel_capacity_retry_send(self, restore_draft=True)
            self._toast(
                "Gemini session slots are still busy — your draft is kept."
                if capacity == "busy"
                else "An idle Gemini session took too long to close — your draft is kept."
            )
            return False
        return from_timer
