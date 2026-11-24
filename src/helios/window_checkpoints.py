"""Pre-turn git checkpoint dispatch fencing and Rewind UI, split out of main_window.

Behaviour-preserving mixin: owns checkpoint capture, deadline fallback,
dispatch ownership tokens, and Rewind file restoration while executing on the
composed MainWindow instance.
"""

from __future__ import annotations

from datetime import datetime
import threading
from typing import NamedTuple

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

from helios.backend import checkpoints
from helios.backend.project_perms import canonical_cwd
from helios.log import get_logger

_log = get_logger("window")

# How long a pre-turn snapshot may hold up the send. Milliseconds on a normal
# repo; this only bites on a pathological one, where shipping the message
# without an undo point beats freezing the composer.
_CHECKPOINT_DEADLINE_MS = 4000


class _CheckpointDispatch(NamedTuple):
    """One pre-turn snapshot that still owns an unsent composer submit."""

    driver: object
    generation: int
    text: str
    provider: str
    work_id: str
    cwd: str


class WindowCheckpointsMixin:
    """Pre-turn git checkpoint capture, dispatch fencing, and Rewind UI."""

    def _checkpoint_dispatch_for(self, drv) -> _CheckpointDispatch | None:
        """Return the current unsent checkpoint dispatch for ``drv``."""

        if drv is None:
            return None
        pending = getattr(self, "_pending_checkpoint_dispatches", None)
        if not isinstance(pending, dict):
            return None
        entry = pending.get(id(drv))
        if not isinstance(entry, _CheckpointDispatch) or entry.driver is not drv:
            return None
        return entry

    def _checkpoint_dispatch_pending_for(self, drv) -> bool:
        return WindowCheckpointsMixin._checkpoint_dispatch_for(self, drv) is not None

    def _begin_checkpoint_dispatch(self, drv, text: str) -> int:
        """Install a new one-shot dispatch generation for ``drv``."""

        generation = int(getattr(self, "_checkpoint_dispatch_generation", 0)) + 1
        self._checkpoint_dispatch_generation = generation
        pending = getattr(self, "_pending_checkpoint_dispatches", None)
        if not isinstance(pending, dict):
            pending = {}
            self._pending_checkpoint_dispatches = pending
        target = getattr(self, "_next_chat", None)
        target_project = getattr(target, "project", None)
        pending[id(drv)] = _CheckpointDispatch(
            drv,
            generation,
            text,
            str(getattr(drv, "provider", "") or ""),
            str(
                getattr(drv, "_helios_work_id", "")
                or getattr(drv, "_helios_work_id_hint", "")
                or getattr(target, "work_id", "")
                or ""
            ),
            canonical_cwd(
                str(
                    getattr(target_project, "cwd", "")
                    or getattr(drv, "_cwd", "")
                    or ""
                )
            ),
        )
        return generation

    def _checkpoint_dispatch_matches_target(self, drv) -> bool:
        """Verify the staged provider/Work/cwd without requiring a native id."""

        entry = WindowCheckpointsMixin._checkpoint_dispatch_for(self, drv)
        target = getattr(self, "_next_chat", None)
        if entry is None or target is None:
            return False
        selected_provider = str(self._selected_provider() or "")
        current_work_id = str(getattr(target, "work_id", "") or "")
        current_cwd = canonical_cwd(
            str(getattr(getattr(target, "project", None), "cwd", "") or "")
        )
        return (
            (not entry.provider or entry.provider == selected_provider)
            and entry.work_id == current_work_id
            and entry.cwd == current_cwd
        )

    def _take_checkpoint_dispatch(
        self,
        drv,
        generation: int | None = None,
    ) -> _CheckpointDispatch | None:
        """Atomically claim/cancel the matching dispatch on the GTK thread."""

        entry = WindowCheckpointsMixin._checkpoint_dispatch_for(self, drv)
        if entry is None or (
            generation is not None and entry.generation != generation
        ):
            return None
        self._pending_checkpoint_dispatches.pop(id(drv), None)
        return entry

    def _restore_checkpoint_dispatch_text(self, drv, text: str) -> bool:
        """Return one unsent checkpoint-owned prompt to its visible/background owner."""
        from helios.main_window import MainWindow

        if not text:
            return False
        if self._destroyed or not self._drv_is_current(drv):
            drafts = getattr(self, "_native_unsent_drafts", None)
            if not isinstance(drafts, dict):
                drafts = {}
                self._native_unsent_drafts = drafts
            bucket = drafts.setdefault(MainWindow._native_draft_key(drv), [])
            bucket.append(text)
            return True
        existing = self._composer.current_text().strip()
        self._composer.set_text(f"{text}\n\n{existing}" if existing else text)
        return True

    def _rewind_before(self, timestamp: str) -> None:
        try:
            when = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            self._toast("That message has no usable time to rewind to.")
            return
        self._show_rewind(before=when)

    def _checkpoint_cwd(self) -> str:
        target = self._next_chat
        project = target.project if target is not None else None
        return str(getattr(project, "cwd", "") or "")

    def _capture_checkpoint(self, drv, text: str, dispatch) -> None:
        """Snapshot the working tree, THEN release the turn.

        git work happens off the main loop so the UI never freezes, but
        ``dispatch`` is called only once the snapshot has been written — and
        always exactly once, including on failure. A checkpoint that races the
        edits it is supposed to precede would silently capture a half-applied
        turn.
        """
        cwd = self._checkpoint_cwd()
        session_id = str(getattr(drv, "session_id", "") or "")
        generation = WindowCheckpointsMixin._begin_checkpoint_dispatch(self, drv, text)
        if not cwd:
            if WindowCheckpointsMixin._take_checkpoint_dispatch(
                self,
                drv,
                generation,
            ) is not None:
                dispatch()
            return

        def work() -> None:
            point = None
            try:
                point = checkpoints.snapshot(cwd, text)
            except Exception as exc:  # noqa: BLE001 — a send must never be lost
                _log.warning("checkpoint snapshot raised: %s", exc)
            # One idle callback for both outcomes so `dispatch` cannot be
            # skipped by an early return: no repo, git missing, or a raise all
            # still send the message, just without an undo point.
            GLib.idle_add(
                self._checkpoint_captured,
                drv,
                generation,
                session_id,
                point,
                dispatch,
            )

        threading.Thread(target=work, daemon=True, name="checkpoint").start()
        GLib.timeout_add(
            _CHECKPOINT_DEADLINE_MS,
            self._checkpoint_deadline,
            drv,
            generation,
            dispatch,
        )

    def _checkpoint_captured(
        self,
        drv,
        generation: int,
        session_id: str,
        point,
        dispatch,
    ) -> bool:
        # Worker, deadline, Stop and teardown all compete for the same token on
        # the GTK thread. Only the exact current generation may store or send.
        if WindowCheckpointsMixin._take_checkpoint_dispatch(self, drv, generation) is None:
            if point is not None:
                _log.info("discarding checkpoint from a cancelled/stale dispatch")
            return False
        if self._destroyed:
            return False
        if point is not None:
            self._store_checkpoint(drv, session_id, point)
        dispatch()
        return False

    def _checkpoint_deadline(self, drv, generation: int, dispatch) -> bool:
        """Release the turn when the snapshot is taking too long.

        Correctness for the checkpoint must not become a hostage situation for
        the send: a slow clean filter, a network filesystem, or a wedged git
        would otherwise stall the message behind git's own 30s-per-command
        timeout. Past the deadline the turn goes out with no undo point, and
        the user is told so rather than left wondering.
        """
        if self._destroyed or WindowCheckpointsMixin._take_checkpoint_dispatch(
            self,
            drv,
            generation,
        ) is None:
            return False
        _log.warning("checkpoint capture exceeded its deadline; sending without one")
        self._toast(
            "Sent without a checkpoint — the snapshot was taking too long. "
            "Rewind is unavailable for this message.",
            timeout=6,
        )
        dispatch()
        return False

    def _store_checkpoint(self, drv, session_id: str, point) -> bool:
        if self._destroyed:
            return False
        sid = session_id or str(getattr(drv, "session_id", "") or "")
        if sid:
            checkpoints.record_async(sid, point)
        else:
            # No id yet: hold it on the driver so session-started can file it.
            pending = getattr(drv, "_helios_pending_checkpoints", None)
            if pending is None:
                pending = []
                drv._helios_pending_checkpoints = pending
            pending.append(point)
        return False

    def _show_rewind(self, before: float | None = None) -> None:
        """Open Rewind. ``before`` (unix time of one of your messages) narrows
        it to the checkpoint taken just before that message was sent."""
        from helios.main_window import MainWindow
        from helios.widgets.rewind_dialog import RewindDialog

        if self._destroyed:
            return

        drv = self._driver
        checkpoint_pending = WindowCheckpointsMixin._checkpoint_dispatch_pending_for(self, drv)
        if self._agent_is_writing(drv) or checkpoint_pending:
            # A rewind and a running turn both write the same files. Whichever
            # lands second wins, so the result is neither the checkpoint nor
            # the turn — it is a mix that never existed. Refuse rather than
            # race, and say why.
            self._toast(
                "Stop the current response before rewinding — it is still "
                "writing files.",
                timeout=6,
            )
            return
        if self._restore_in_flight:
            self._toast("A restore is already running.")
            return
        session_id = str(
            getattr(drv, "session_id", "")
            or getattr(self._next_chat, "resume_id", "")
            or ""
        )
        work_id = str(
            getattr(drv, "_helios_work_id", "")
            or getattr(getattr(self, "_next_chat", None), "work_id", "")
            or ""
        )
        if work_id and MainWindow._work_id_has_continuity(self, work_id):
            coordinator = getattr(self, "_work_coordinator", None)
            leg_sids: list[str] = []
            if coordinator is not None:
                for leg in coordinator.list_work_legs(work_id):
                    if leg.native_session_id and leg.native_session_id not in leg_sids:
                        leg_sids.append(leg.native_session_id)
            if session_id and session_id not in leg_sids:
                leg_sids.append(session_id)
            merged_points = []
            for sid in leg_sids:
                merged_points.extend(checkpoints.load(sid))
            merged_points.sort(key=lambda p: p.created_at)
            points = merged_points
        else:
            points = checkpoints.load(session_id) if session_id else []
        if before is not None:
            # A checkpoint is taken as the message is sent; allow clock skew.
            earlier = [p for p in points if p.created_at <= before + 5]
            if not earlier:
                self._toast("No checkpoint was taken before that message.")
                return
            points = [max(earlier, key=lambda p: p.created_at)]
        # `changes_since` takes a fresh snapshot and shells out to git several
        # times; on a large repo that is seconds. Hand the dialog an async
        # loader so neither opening it nor selecting a checkpoint can freeze
        # the window.
        dialog = RewindDialog(points, load_changes=self._load_changes_async)
        dialog.connect("restore-requested", self._on_restore_requested)
        dialog.present(self)

    def _load_changes_async(self, point, on_ready) -> None:
        """Compute a checkpoint's change list off the main loop."""

        def work() -> None:
            try:
                changes = checkpoints.changes_since(point)
            except Exception as exc:  # noqa: BLE001 — never crash the dialog
                _log.warning("checkpoint diff failed: %s", exc)
                changes = []
            GLib.idle_add(on_ready, changes)

        threading.Thread(target=work, daemon=True, name="checkpoint-diff").start()

    @staticmethod
    def _agent_is_writing(drv) -> bool:
        """Whether a turn is currently in flight and could touch the worktree."""
        return drv is not None and bool(getattr(drv, "is_busy", False))

    def _on_restore_requested(self, _dialog, point, paths) -> None:
        if self._destroyed:
            return
        # Re-checked here, not only when the dialog opened: the agent may have
        # started a queued turn while the user was reading the change list.
        checkpoint_pending = WindowCheckpointsMixin._checkpoint_dispatch_pending_for(
            self,
            self._driver,
        )
        if self._agent_is_writing(self._driver) or checkpoint_pending:
            self._toast(
                "Restore cancelled — a response started while the dialog was "
                "open and is writing files.",
                timeout=6,
            )
            return
        if self._restore_in_flight:
            self._toast("A restore is already running.")
            return
        self._restore_in_flight = True

        def work() -> None:
            result = None
            error = ""
            try:
                result = checkpoints.restore(point, list(paths))
            except Exception as exc:  # noqa: BLE001 — never die silently
                # The dialog has already closed. Without this the thread dies
                # and the user is left with no idea whether files moved.
                _log.exception("checkpoint restore failed")
                error = str(exc)
            finally:
                GLib.idle_add(self._report_restore, result, error)

        threading.Thread(target=work, daemon=True, name="checkpoint-restore").start()

    def _report_restore(self, result, error: str = "") -> bool:
        # Cleared unconditionally, before any early return: leaving the flag
        # set would wedge the action for the rest of the session.
        self._restore_in_flight = False
        if self._destroyed:
            return False
        if result is None:
            self._toast(
                f"Restore failed: {error or 'unknown error'}. Your files were "
                "not fully changed — check the log.",
                timeout=8,
            )
            return False
        touched = len(result.restored) + len(result.removed)
        if result.failed:
            self._toast(
                f"Restored {touched} file(s); {len(result.failed)} could not be "
                "changed.",
                timeout=6,
            )
        elif result.kept:
            # Never silent: a refused deletion means the checkpoint and the
            # disk disagree about what predates the turn, and the user needs
            # to know a file they may have expected gone is still there.
            self._toast(
                f"Restored {touched} file(s). Kept {len(result.kept)} that "
                "existed before this checkpoint.",
                timeout=6,
            )
        elif touched:
            note = (
                f" Removed files were moved to {result.trash_dir}."
                if result.trash_dir
                else ""
            )
            self._toast(
                f"Restored {touched} file(s) from the checkpoint.{note}",
                timeout=8 if note else 4,
            )
        else:
            self._toast("Nothing to restore.")
        return False
