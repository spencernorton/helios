"""Revisioned product brief, guardrails, and delivery evidence for a Work.

The Work record owns live session intent across turns, provider switches, and
scratchpad handoffs. ``ProductBrief`` keeps user-owned outcome, acceptance
criteria, visual reference artifacts, rejected approaches, explicit stops, and
delivery receipts separate from chronological partner chatter so long sessions
cannot lose essential steering or silently clear user holds.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from helios.backend.sensitive_text import scrub_sensitive

READINESS_STAGES: tuple[str, ...] = (
    "design_only",
    "compiled",
    "fixture_verified",
    "installed_candidate",
    "loaded_normal_launcher",
    "user_accepted",
)
_STAGE_INDEX = {stage: idx for idx, stage in enumerate(READINESS_STAGES)}

FUNCTIONAL_STATUSES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "verified",
    "failed",
    "blocked",
)
VISUAL_STATUSES: tuple[str, ...] = (
    "not_applicable",
    "pending",
    "candidate",
    "verified",
    "rejected",
)
REFERENCE_ROLES: tuple[str, ...] = (
    "target",
    "baseline",
    "bug_annotation",
    "candidate",
    "approved_example",
)
AUTHOR_ROLES: tuple[str, ...] = ("user", "model", "system")

_ID_CLEAN_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_MAX_SHORT_TEXT = 2_000
_MAX_CRITERIA = 40
_MAX_REFERENCES = 32
_MAX_STOPS = 24
_MAX_CORRECTIONS = 32
_MAX_EVIDENCE = 48


class ProductBriefError(ValueError):
    """Base validation error for product-brief operations."""


class StaleBriefRevisionError(ProductBriefError):
    """Raised when an update's expected_revision does not match current state."""


class BriefAuthorityError(ProductBriefError):
    """Raised when a non-user caller attempts a user-only state transition."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, *, max_len: int = _MAX_SHORT_TEXT) -> str:
    text, _ = scrub_sensitive("" if value is None else str(value))
    text = text.strip()
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _clean_id(value: Any, prefix: str, index: int) -> str:
    raw = str(value or "").strip()
    cleaned = _ID_CLEAN_RE.sub("-", raw).strip("-")[:96]
    return cleaned or f"{prefix}-{index}"


def _normalize_stage(value: Any, *, default: str = "design_only") -> str:
    stage = str(value or "").strip()
    return stage if stage in _STAGE_INDEX else default


def _normalize_functional(value: Any) -> str:
    status = str(value or "").strip()
    if status == "completed":
        return "verified"
    return status if status in FUNCTIONAL_STATUSES else "pending"


def _normalize_visual(value: Any) -> str:
    status = str(value or "").strip()
    return status if status in VISUAL_STATUSES else "pending"


def _normalize_role(value: Any) -> str:
    role = str(value or "").strip()
    return role if role in REFERENCE_ROLES else "target"


@dataclass(frozen=True, slots=True)
class BriefCriterion:
    """One acceptance item with separate functional, visual, and user gates."""

    criterion_id: str
    text: str
    functional_status: str = "pending"
    visual_status: str = "pending"
    user_accepted: bool = False
    readiness_stage: str = "design_only"
    updated_at: str = ""

    @property
    def is_satisfied(self) -> bool:
        return self.user_accepted or (
            self.functional_status == "verified"
            and self.visual_status in {"verified", "not_applicable"}
            and self.readiness_stage in {"loaded_normal_launcher", "user_accepted"}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "text": self.text,
            "functional_status": self.functional_status,
            "visual_status": self.visual_status,
            "user_accepted": self.user_accepted,
            "readiness_stage": self.readiness_stage,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_any(cls, raw: Any, index: int = 1) -> BriefCriterion:
        if isinstance(raw, BriefCriterion):
            return raw
        if isinstance(raw, str):
            text = _clean_text(raw)
            if not text:
                raise ProductBriefError("criterion text must be non-empty")
            return cls(criterion_id=f"crit-{index}", text=text)
        if not isinstance(raw, Mapping):
            raise ProductBriefError("criterion must be a mapping or string")
        text = _clean_text(raw.get("text"))
        if not text:
            raise ProductBriefError("criterion text must be non-empty")
        user_accepted = bool(raw.get("user_accepted", False))
        stage = _normalize_stage(
            raw.get("readiness_stage"),
            default="user_accepted" if user_accepted else "design_only",
        )
        if user_accepted:
            stage = "user_accepted"
        elif stage == "user_accepted":
            stage = "loaded_normal_launcher"
        return cls(
            criterion_id=_clean_id(raw.get("criterion_id") or raw.get("id"), "crit", index),
            text=text,
            functional_status=_normalize_functional(raw.get("functional_status") or raw.get("status")),
            visual_status=_normalize_visual(raw.get("visual_status")),
            user_accepted=user_accepted,
            readiness_stage=stage,
            updated_at=_clean_text(raw.get("updated_at"), max_len=64),
        )


@dataclass(frozen=True, slots=True)
class ReferenceArtifactRef:
    """Durable reference to an annotated screenshot or baseline in Work artifacts."""

    artifact_id: str
    digest: str
    role: str = "target"
    caption: str = ""
    media_type: str = "image/png"
    source_message_id: str = ""
    original_filename: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "digest": self.digest,
            "role": self.role,
            "caption": self.caption,
            "media_type": self.media_type,
            "source_message_id": self.source_message_id,
            "original_filename": self.original_filename,
        }

    @classmethod
    def from_any(cls, raw: Any, index: int = 1) -> ReferenceArtifactRef:
        if isinstance(raw, ReferenceArtifactRef):
            return raw
        if not isinstance(raw, Mapping):
            raise ProductBriefError("reference artifact must be a mapping")
        artifact_id = _clean_text(raw.get("artifact_id"), max_len=160)
        digest = _clean_text(raw.get("digest") or artifact_id, max_len=160)
        if not artifact_id:
            raise ProductBriefError(f"reference artifact #{index} requires artifact_id")
        return cls(
            artifact_id=artifact_id,
            digest=digest or artifact_id,
            role=_normalize_role(raw.get("role")),
            caption=_clean_text(raw.get("caption"), max_len=600),
            media_type=_clean_text(raw.get("media_type") or "image/png", max_len=96),
            source_message_id=_clean_text(raw.get("source_message_id"), max_len=128),
            original_filename=_clean_text(raw.get("original_filename"), max_len=240),
        )


@dataclass(frozen=True, slots=True)
class ExplicitStop:
    """User-owned hold or scope stop that model checkpoints cannot clear."""

    stop_id: str
    scope: str
    reason: str
    owner: str = "user"
    clearance_condition: str = "Explicit user clearance required"
    active: bool = True
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_id": self.stop_id,
            "scope": self.scope,
            "reason": self.reason,
            "owner": self.owner,
            "clearance_condition": self.clearance_condition,
            "active": self.active,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_any(cls, raw: Any, index: int = 1) -> ExplicitStop:
        if isinstance(raw, ExplicitStop):
            return raw
        if isinstance(raw, str):
            reason = _clean_text(raw)
            if not reason:
                raise ProductBriefError("explicit stop text must be non-empty")
            return cls(stop_id=f"stop-{index}", scope="work", reason=reason)
        if not isinstance(raw, Mapping):
            raise ProductBriefError("explicit stop must be a mapping or string")
        scope = _clean_text(raw.get("scope") or "work", max_len=240)
        reason = _clean_text(raw.get("reason") or raw.get("text"))
        if not reason:
            raise ProductBriefError("explicit stop reason must be non-empty")
        return cls(
            stop_id=_clean_id(raw.get("stop_id") or raw.get("id"), "stop", index),
            scope=scope,
            reason=reason,
            owner=_clean_text(raw.get("owner") or "user", max_len=120) or "user",
            clearance_condition=_clean_text(
                raw.get("clearance_condition") or "Explicit user clearance required",
                max_len=300,
            ),
            active=bool(raw.get("active", True)),
            recorded_at=_clean_text(raw.get("recorded_at"), max_len=64),
        )


@dataclass(frozen=True, slots=True)
class UserCorrection:
    """Unresolved user layout/functional correction retained at full fidelity."""

    correction_id: str
    text: str
    criterion_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    resolved: bool = False
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "correction_id": self.correction_id,
            "text": self.text,
            "criterion_ids": list(self.criterion_ids),
            "artifact_ids": list(self.artifact_ids),
            "resolved": self.resolved,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_any(cls, raw: Any, index: int = 1) -> UserCorrection:
        if isinstance(raw, UserCorrection):
            return raw
        if isinstance(raw, str):
            text = _clean_text(raw, max_len=4_000)
            if not text:
                raise ProductBriefError("user correction text must be non-empty")
            return cls(correction_id=f"corr-{index}", text=text)
        if not isinstance(raw, Mapping):
            raise ProductBriefError("user correction must be a mapping or string")
        text = _clean_text(raw.get("text"), max_len=4_000)
        if not text:
            raise ProductBriefError("user correction text must be non-empty")
        crit_ids = tuple(
            _clean_text(item, max_len=96)
            for item in (raw.get("criterion_ids") or ())
            if _clean_text(item, max_len=96)
        )
        art_ids = tuple(
            _clean_text(item, max_len=160)
            for item in (raw.get("artifact_ids") or ())
            if _clean_text(item, max_len=160)
        )
        return cls(
            correction_id=_clean_id(raw.get("correction_id") or raw.get("id"), "corr", index),
            text=text,
            criterion_ids=crit_ids,
            artifact_ids=art_ids,
            resolved=bool(raw.get("resolved", False)),
            recorded_at=_clean_text(raw.get("recorded_at"), max_len=64),
        )


@dataclass(frozen=True, slots=True)
class DeliveryEvidence:
    """Criterion-linked runtime verification receipt."""

    evidence_id: str
    criterion_id: str
    candidate_sha: str = ""
    installed_artifact: str = ""
    loaded_process: str = ""
    normal_entry_point: str = ""
    host: str = ""
    window_state: str = ""
    functional_status: str = "pending"
    visual_status: str = "pending"
    readiness_stage: str = "design_only"
    stale_process_detected: bool = False
    launcher_shadowed: bool = False
    user_accepted: bool = False
    recorded_at: str = ""

    def has_runtime_mismatch(self) -> bool:
        if self.stale_process_detected or self.launcher_shadowed:
            return True
        installed = self.installed_artifact.strip()
        loaded = self.loaded_process.strip()
        if installed and loaded and installed != loaded:
            return True
        return False

    def effective_readiness_stage(self) -> str:
        stage = _normalize_stage(self.readiness_stage)
        if self.has_runtime_mismatch():
            if _STAGE_INDEX[stage] > _STAGE_INDEX["installed_candidate"]:
                return "installed_candidate"
            return stage
        if stage == "user_accepted" and not self.user_accepted:
            return "loaded_normal_launcher"
        if self.user_accepted:
            return "user_accepted"
        return stage

    def to_dict(self) -> dict[str, Any]:
        effective = self.effective_readiness_stage()
        return {
            "evidence_id": self.evidence_id,
            "criterion_id": self.criterion_id,
            "candidate_sha": self.candidate_sha,
            "installed_artifact": self.installed_artifact,
            "loaded_process": self.loaded_process,
            "normal_entry_point": self.normal_entry_point,
            "host": self.host,
            "window_state": self.window_state,
            "functional_status": self.functional_status,
            "visual_status": self.visual_status,
            "readiness_stage": effective,
            "stale_process_detected": self.stale_process_detected,
            "launcher_shadowed": self.launcher_shadowed,
            "user_accepted": self.user_accepted and not self.has_runtime_mismatch(),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_any(cls, raw: Any, index: int = 1) -> DeliveryEvidence:
        if isinstance(raw, DeliveryEvidence):
            return replace(
                raw,
                readiness_stage=raw.effective_readiness_stage(),
                user_accepted=raw.user_accepted and not raw.has_runtime_mismatch(),
            )
        if not isinstance(raw, Mapping):
            raise ProductBriefError("delivery evidence must be a mapping")
        criterion_id = _clean_text(raw.get("criterion_id"), max_len=96)
        if not criterion_id:
            raise ProductBriefError("delivery evidence requires criterion_id")
        inst = cls(
            evidence_id=_clean_id(raw.get("evidence_id") or raw.get("id"), "evd", index),
            criterion_id=criterion_id,
            candidate_sha=_clean_text(raw.get("candidate_sha"), max_len=96),
            installed_artifact=_clean_text(raw.get("installed_artifact"), max_len=400),
            loaded_process=_clean_text(raw.get("loaded_process"), max_len=400),
            normal_entry_point=_clean_text(raw.get("normal_entry_point"), max_len=400),
            host=_clean_text(raw.get("host"), max_len=120),
            window_state=_clean_text(raw.get("window_state"), max_len=200),
            functional_status=_normalize_functional(raw.get("functional_status")),
            visual_status=_normalize_visual(raw.get("visual_status")),
            readiness_stage=_normalize_stage(raw.get("readiness_stage")),
            stale_process_detected=bool(raw.get("stale_process_detected", False)),
            launcher_shadowed=bool(raw.get("launcher_shadowed", False)),
            user_accepted=bool(raw.get("user_accepted", False)),
            recorded_at=_clean_text(raw.get("recorded_at"), max_len=64) or _utc_now(),
        )
        return replace(
            inst,
            readiness_stage=inst.effective_readiness_stage(),
            user_accepted=inst.user_accepted and not inst.has_runtime_mismatch(),
        )


@dataclass(frozen=True, slots=True)
class ProductBrief:
    """Revisioned user-owned product brief projected onto a Helios Work."""

    work_id: str
    revision: int = 0
    outcome: str = ""
    supersedes: str = ""
    delivery_target: str = ""
    criteria: tuple[BriefCriterion, ...] = ()
    reference_artifacts: tuple[ReferenceArtifactRef, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    rejected_approaches: tuple[str, ...] = ()
    explicit_stops: tuple[ExplicitStop, ...] = ()
    unresolved_corrections: tuple[UserCorrection, ...] = ()
    delivery_evidence: tuple[DeliveryEvidence, ...] = ()
    updated_by: str = "user"
    updated_at: str = ""

    def active_stops(self) -> tuple[ExplicitStop, ...]:
        return tuple(stop for stop in self.explicit_stops if stop.active)

    def open_criteria(self) -> tuple[BriefCriterion, ...]:
        return tuple(crit for crit in self.criteria if not crit.is_satisfied)

    def open_corrections(self) -> tuple[UserCorrection, ...]:
        return tuple(corr for corr in self.unresolved_corrections if not corr.resolved)

    def has_material_content(self) -> bool:
        return bool(
            self.revision > 0
            or self.outcome.strip()
            or self.criteria
            or self.reference_artifacts
            or self.active_stops()
            or self.open_corrections()
            or self.rejected_approaches
        )

    def overall_readiness_stage(self) -> str:
        if not self.criteria and not self.delivery_evidence:
            return "design_only"
        stages = [crit.readiness_stage for crit in self.criteria] or [
            ev.effective_readiness_stage() for ev in self.delivery_evidence
        ]
        if not stages:
            return "design_only"
        return min(stages, key=lambda item: _STAGE_INDEX.get(item, 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "revision": self.revision,
            "outcome": self.outcome,
            "supersedes": self.supersedes,
            "delivery_target": self.delivery_target,
            "criteria": [c.to_dict() for c in self.criteria],
            "reference_artifacts": [r.to_dict() for r in self.reference_artifacts],
            "source_message_ids": list(self.source_message_ids),
            "rejected_approaches": list(self.rejected_approaches),
            "explicit_stops": [s.to_dict() for s in self.explicit_stops],
            "unresolved_corrections": [u.to_dict() for u in self.unresolved_corrections],
            "delivery_evidence": [e.to_dict() for e in self.delivery_evidence],
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProductBrief:
        work_id = _clean_text(data.get("work_id"), max_len=128)
        revision = int(data.get("revision") or 0)
        criteria = tuple(
            BriefCriterion.from_any(item, idx + 1)
            for idx, item in enumerate(list(data.get("criteria") or ())[:_MAX_CRITERIA])
        )
        refs = tuple(
            ReferenceArtifactRef.from_any(item, idx + 1)
            for idx, item in enumerate(
                list(data.get("reference_artifacts") or ())[:_MAX_REFERENCES]
            )
        )
        stops = tuple(
            ExplicitStop.from_any(item, idx + 1)
            for idx, item in enumerate(list(data.get("explicit_stops") or ())[:_MAX_STOPS])
        )
        corrections = tuple(
            UserCorrection.from_any(item, idx + 1)
            for idx, item in enumerate(
                list(data.get("unresolved_corrections") or ())[:_MAX_CORRECTIONS]
            )
        )
        evidence = tuple(
            DeliveryEvidence.from_any(item, idx + 1)
            for idx, item in enumerate(
                list(data.get("delivery_evidence") or ())[:_MAX_EVIDENCE]
            )
        )
        src_msgs = tuple(
            _clean_text(item, max_len=128)
            for item in list(data.get("source_message_ids") or ())[:64]
            if _clean_text(item, max_len=128)
        )
        rejected = tuple(
            _clean_text(item, max_len=400)
            for item in list(data.get("rejected_approaches") or ())[:32]
            if _clean_text(item, max_len=400)
        )
        return cls(
            work_id=work_id,
            revision=max(0, revision),
            outcome=_clean_text(data.get("outcome")),
            supersedes=_clean_text(data.get("supersedes")),
            delivery_target=_clean_text(data.get("delivery_target"), max_len=400),
            criteria=criteria,
            reference_artifacts=refs,
            source_message_ids=src_msgs,
            rejected_approaches=rejected,
            explicit_stops=stops,
            unresolved_corrections=corrections,
            delivery_evidence=evidence,
            updated_by=_clean_text(data.get("updated_by") or "user", max_len=32),
            updated_at=_clean_text(data.get("updated_at"), max_len=64),
        )

    def to_handoff_summary(
        self,
        *,
        artifact_paths: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Structured summary placed ahead of transcript_tail in handoffs."""

        paths = artifact_paths or {}
        return {
            "work_id": self.work_id,
            "intent_revision": self.revision,
            "outcome": self.outcome,
            "supersedes": self.supersedes,
            "delivery_target": self.delivery_target,
            "overall_readiness_stage": self.overall_readiness_stage(),
            "open_criteria": [c.to_dict() for c in self.open_criteria()],
            "accepted_criteria_ids": [
                c.criterion_id for c in self.criteria if c.user_accepted
            ],
            "unresolved_corrections": [u.to_dict() for u in self.open_corrections()],
            "reference_artifacts": [
                {
                    **r.to_dict(),
                    **(
                        {"durable_path": paths[r.artifact_id]}
                        if r.artifact_id in paths
                        else {}
                    ),
                }
                for r in self.reference_artifacts
            ],
            "rejected_approaches": list(self.rejected_approaches),
            "explicit_stops": [s.to_dict() for s in self.active_stops()],
            "latest_delivery_evidence": [e.to_dict() for e in self.delivery_evidence[-8:]],
        }

    def render_essential_context(
        self,
        *,
        artifact_paths: Mapping[str, str] | None = None,
        max_chars: int = 3_200,
    ) -> str:
        """Render protected intent lines that survive chronological truncation."""

        if not self.has_material_content():
            return ""
        paths = artifact_paths or {}
        lines: list[str] = [
            f"Brief revision: {self.revision} (readiness: {self.overall_readiness_stage()})"
        ]
        if self.outcome:
            lines.append(f"Outcome: {self.outcome}")
        if self.supersedes:
            lines.append(f"Supersedes prior framing: {self.supersedes}")
        if self.delivery_target:
            lines.append(f"Delivery target: {self.delivery_target}")
        for stop in self.active_stops():
            lines.append(
                f"ACTIVE STOP [{stop.stop_id}] ({stop.scope}; owner={stop.owner}): "
                f"{stop.reason} — clearance: {stop.clearance_condition}"
            )
        if self.rejected_approaches:
            lines.append(
                "Rejected approaches (do not reopen): "
                + "; ".join(self.rejected_approaches[:8])
            )
        open_corrs = self.open_corrections()
        if open_corrs:
            lines.append("Unresolved user corrections:")
            for corr in open_corrs[-6:]:
                refs = f" [refs: {', '.join(corr.artifact_ids)}]" if corr.artifact_ids else ""
                lines.append(f"- ({corr.correction_id}) {corr.text}{refs}")
        open_crits = self.open_criteria()
        if open_crits:
            lines.append("Open acceptance criteria:")
            for crit in open_crits[:12]:
                lines.append(
                    f"- [{crit.criterion_id}] {crit.text} "
                    f"(functional={crit.functional_status}, visual={crit.visual_status}, "
                    f"stage={crit.readiness_stage})"
                )
        if self.reference_artifacts:
            lines.append("Reference artifacts:")
            for ref in self.reference_artifacts[:12]:
                loc = paths.get(ref.artifact_id) or ref.original_filename or ref.artifact_id
                cap = f" — {ref.caption}" if ref.caption else ""
                lines.append(
                    f"- [{ref.role}] {ref.artifact_id} ({loc}){cap}"
                )
        rendered = "\n".join(lines)
        if len(rendered) > max_chars:
            return rendered[: max_chars - 1].rstrip() + "…"
        return rendered


def enforce_update_authority(
    existing: ProductBrief,
    *,
    author_role: str,
    next_criteria: Iterable[BriefCriterion] | None,
    next_stops: Iterable[ExplicitStop] | None,
) -> None:
    """Reject model attempts to clear user stops or self-certify user acceptance."""

    if author_role not in AUTHOR_ROLES:
        raise ProductBriefError(f"unknown author_role {author_role!r}")
    if author_role == "user":
        return

    if next_stops is not None:
        incoming_active = {
            stop.stop_id: stop for stop in next_stops if stop.active
        }
        for current_stop in existing.active_stops():
            replacement = incoming_active.get(current_stop.stop_id)
            if replacement is None:
                raise BriefAuthorityError(
                    f"non-user role {author_role!r} cannot clear active explicit stop "
                    f"{current_stop.stop_id!r}"
                )
            if (
                replacement.scope != current_stop.scope
                or replacement.reason != current_stop.reason
            ):
                raise BriefAuthorityError(
                    f"non-user role {author_role!r} cannot weaken active explicit stop "
                    f"{current_stop.stop_id!r}"
                )

    if next_criteria is not None:
        previously_accepted = {
            crit.criterion_id for crit in existing.criteria if crit.user_accepted
        }
        for crit in next_criteria:
            if (
                crit.user_accepted or crit.readiness_stage == "user_accepted"
            ) and crit.criterion_id not in previously_accepted:
                raise BriefAuthorityError(
                    f"non-user role {author_role!r} cannot mark criterion "
                    f"{crit.criterion_id!r} as user_accepted"
                )
