"""Cooperative cancellation for OpenRouter streaming requests."""

from __future__ import annotations

import threading
from collections.abc import Callable


class CancellationToken:
    """Thread-safe cooperative cancellation with response-close callbacks."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_callback_id = 0

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        with self._lock:
            was_cancelled = self._event.is_set()
            self._event.set()
            callbacks = tuple(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Cancellation must remain content-free and best effort.
                pass
        return not was_cancelled

    def wait(self, timeout_seconds: float) -> bool:
        return self._event.wait(timeout_seconds)

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self._event.is_set():
                call_now = True
                callback_id = -1
            else:
                call_now = False
                callback_id = self._next_callback_id
                self._next_callback_id += 1
                self._callbacks[callback_id] = callback
        if call_now:
            try:
                callback()
            except Exception:
                pass

        def remove() -> None:
            if callback_id < 0:
                return
            with self._lock:
                self._callbacks.pop(callback_id, None)

        return remove
