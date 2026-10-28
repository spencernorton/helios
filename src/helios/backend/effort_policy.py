"""Model-specific effort validation using already-discovered catalog snapshots.

No network or native process is started by this policy. An absent snapshot is
unknown (live changes require discovery); an empty model row
is an explicit statement that no selectable reasoning effort was advertised.
"""
from __future__ import annotations


class EffortPolicyMixin:
    _effort_attribute = "_effort"

    def set_effort_catalog(self, entries) -> None:
        self._effort_catalog = {
            entry.id: (frozenset(key for key, _desc in entry.reasoning_efforts), entry.default_effort)
            for entry in entries
        }
        self._normalize_model_effort()

    def _effort_is_supported(self, key: str) -> bool:
        catalog = getattr(self, "_effort_catalog", None)
        if catalog is None:
            return False  # do not acknowledge a setting without model capabilities
        choices, _default = catalog.get(self._model, (frozenset(), ""))
        return key in choices

    def _normalize_model_effort(self) -> None:
        catalog = getattr(self, "_effort_catalog", None)
        if catalog is None:
            return
        choices, default = catalog.get(self._model, (frozenset(), ""))
        current = getattr(self, self._effort_attribute, "")
        if current and current not in choices:
            # Ultra is a one-session lease, never an implicit fallback.
            standard = tuple(key for key in choices if key != "ultra")
            replacement = default if default in standard else next(
                (key for key in ("high", "medium", "low") if key in standard),
                sorted(standard)[0] if standard else "")
            setattr(self, self._effort_attribute, replacement)
