"""Per-conversation composer drafts. GTK-free so the slim CI lane runs it.

The composer is a single buffer shared by every conversation, so switching
sessions used to carry your half-typed message into the next one.
The window parks the outgoing text here under the key of the conversation it
belongs to, and takes it back on the way in.
"""

from __future__ import annotations


def draft_key(target, *, work_id: str = "") -> str:
    """Stable per-conversation draft slot, preferring durable Work identity."""

    wid = str(work_id or getattr(target, "work_id", "") or "").strip()
    if wid:
        return f"work:{wid}"
    sid = str(getattr(target, "session_id", "") or "")
    if sid:
        return sid
    cwd = str(getattr(getattr(target, "project", None), "cwd", "") or "")
    return f"new:{cwd}"


class DraftBook:
    """key -> unsent composer text.

    A blank stash EVICTS rather than stores, so clearing the composer in one
    session does not resurrect a stale draft when you come back to it.

    Known limit: in-memory only, no TTL and no cap — drafts die with the window,
    and the book is bounded by how many conversations one window visits.
    Persist to ui-state if anyone asks for drafts to survive a restart.
    """

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def stash(self, key: str, text: str) -> None:
        if key and text.strip():
            self._d[key] = text
        elif key:
            self._d.pop(key, None)

    def take(self, key: str, *, fallback_key: str = "") -> str:
        """Read a draft, retiring the old slot if its identity has changed."""
        if fallback_key and fallback_key != key and fallback_key in self._d:
            prior = self._d.pop(fallback_key)
            if key:
                return self._d.setdefault(key, prior)
            return prior
        return self._d.get(key, "")
