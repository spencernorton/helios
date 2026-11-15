"""Multi-leg Work transcript stitching and model-switch display labels.

GTK-free so backend test lanes and headless utilities can build, inspect, and
export composite conversations across provider and model switches.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from helios.backend import model_catalog, projects
from helios.backend.transcript import Turn, read_transcript_since
from helios.backend.work_store import WorkLeg


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def compact_model_chip(model: str) -> str:
    """Short human-readable chip label for a per-turn model badge."""
    raw = str(model or "").strip()
    if not raw:
        return ""
    if raw.startswith("google:"):
        raw = raw.split(":", 1)[1].strip()
    if "/" in raw:
        _vendor, _sep, tail = raw.partition("/")
        raw = tail or raw
    if raw.startswith("gpt-"):
        return model_catalog._openai_label(raw)
    if raw.startswith("gemini-"):
        parts = raw.split("-")
        return " ".join(
            p.upper() if p in {"pro", "flash"} and len(p) <= 3 else p.capitalize()
            for p in parts
        )
    if raw.startswith("claude-"):
        parsed = model_catalog._parse_pinned(raw)
        if parsed is not None:
            return model_catalog._label_for_pinned(parsed)
    if len(raw) > 36:
        return raw[:33] + "..."
    return raw


def human_model_label(provider: str, model: str) -> str:
    """Human-readable provider/model label for inline handoff dividers."""
    prov_label = model_catalog.PROVIDER_LABELS.get(
        provider,
        provider.capitalize() if provider else "Assistant",
    )
    chip = compact_model_chip(model)
    if not chip:
        return prov_label
    chip_lower = chip.casefold()
    prov_lower = prov_label.casefold()
    if prov_lower in chip_lower or (provider == "openai" and chip_lower.startswith("gpt")):
        return chip
    if provider == "anthropic" and any(
        fam in chip_lower for fam in ("opus", "sonnet", "haiku", "fable")
    ):
        return f"Claude {chip}" if not chip_lower.startswith("claude") else chip
    if provider == "openrouter":
        return f"{chip} (OpenRouter)"
    return chip


def format_model_switch_label(
    from_provider: str,
    from_model: str,
    to_provider: str,
    to_model: str,
) -> str:
    left = human_model_label(from_provider, from_model)
    right = human_model_label(to_provider, to_model)
    if left == right:
        return f"Continued on {right}"
    return f"Switched from {left} to {right}"


@dataclass(frozen=True, slots=True)
class HandoffBoundary:
    """Inline boundary between two sequential legs of one logical Work."""

    leg_seq: int
    from_provider: str
    from_model: str
    to_provider: str
    to_model: str
    capsule_manifest: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def summary_text(self) -> str:
        return format_model_switch_label(
            self.from_provider,
            self.from_model,
            self.to_provider,
            self.to_model,
        )


CompositeItem = Turn | HandoffBoundary


@dataclass(frozen=True, slots=True)
class CompositeTranscript:
    """Chronological multi-leg transcript items plus tail follow cursor."""

    items: tuple[CompositeItem, ...]
    turns: tuple[Turn, ...]
    tail_leg: WorkLeg | None
    tail_path: Path | None
    tail_offset: int


def resolve_leg_transcript_path(leg: WorkLeg, *, cwd: str = "") -> Path | None:
    """Locate the on-disk JSONL mirror for one WorkLeg."""
    if leg.transcript_path:
        direct = Path(leg.transcript_path)
        if direct.is_file():
            return direct
    sid = leg.native_session_id.strip()
    if not sid:
        return None
    if cwd:
        candidate = (
            projects.PROJECTS_DIR
            / projects.encode_project_dirname(cwd)
            / f"{sid}.jsonl"
        )
        if candidate.is_file():
            return candidate
    if projects.PROJECTS_DIR.is_dir():
        for match in projects.PROJECTS_DIR.glob(f"*/{sid}.jsonl"):
            if match.is_file():
                return match
    if leg.transcript_path:
        return Path(leg.transcript_path)
    if cwd:
        return (
            projects.PROJECTS_DIR
            / projects.encode_project_dirname(cwd)
            / f"{sid}.jsonl"
        )
    return None


def _stamp_turn_for_leg(turn: Turn, leg: WorkLeg) -> Turn:
    if not turn.session_id and leg.native_session_id:
        turn.session_id = leg.native_session_id
    if not turn.provider and leg.provider:
        turn.provider = leg.provider
    if not turn.model and leg.model and turn.role == "assistant":
        turn.model = leg.model
    return turn


def _slice_end_for_reused_file(
    turns: list[Turn],
    cursor_idx: int,
    *,
    current_leg: WorkLeg,
    next_leg_overall: WorkLeg,
    next_same_file_leg: WorkLeg,
) -> int:
    """Choose the exclusive end index in `turns` for `current_leg` when a later
    leg reuses the same transcript file (`G1 -> O1 -> G1` or in-place `set_model`).
    """
    remaining = len(turns) - cursor_idx
    if remaining <= 0:
        return cursor_idx

    cutoff_dt = _parse_iso(next_leg_overall.created_at)
    if cutoff_dt is not None:
        dts = [turns[i].dt for i in range(cursor_idx, len(turns))]
        if any(dt is not None for dt in dts):
            has_before = any(dt is not None and dt <= cutoff_dt for dt in dts)
            has_after = any(dt is not None and dt > cutoff_dt for dt in dts)
            if has_before and has_after:
                end = cursor_idx
                while end < len(turns):
                    dt = turns[end].dt
                    if dt is not None and dt > cutoff_dt:
                        break
                    end += 1
                while (
                    end < len(turns)
                    and turns[end].role == "tool"
                    and turns[end].tool_results
                    and not turns[end].tool_uses
                ):
                    end += 1
                return end
            if not has_before and has_after:
                return cursor_idx

    target_idx = next_same_file_leg.started_turn_index
    if cursor_idx < target_idx <= len(turns):
        return target_idx

    if (
        next_same_file_leg.started_turn_index <= 0
        and next_same_file_leg.model
        and current_leg.model
        and next_same_file_leg.model != current_leg.model
    ):
        for idx in range(cursor_idx, len(turns)):
            t = turns[idx]
            if t.role == "assistant" and t.model == next_same_file_leg.model:
                cut = idx
                while cut > cursor_idx and turns[cut - 1].role == "user" and not turns[cut - 1].is_meta:
                    cut -= 1
                    break
                return cut

    return len(turns)


def build_composite_transcript(
    legs: Sequence[WorkLeg],
    *,
    cwd: str = "",
) -> CompositeTranscript:
    """Stitch ordered WorkLegs into one chronological turn + divider stream."""
    ordered = sorted(legs, key=lambda item: item.leg_seq)
    if not ordered:
        return CompositeTranscript(
            items=(),
            turns=(),
            tail_leg=None,
            tail_path=None,
            tail_offset=0,
        )

    leg_paths: list[Path | None] = [
        resolve_leg_transcript_path(leg, cwd=cwd) for leg in ordered
    ]
    file_cache: dict[Path, tuple[list[Turn], int]] = {}
    file_cursors: dict[Path, int] = {}
    legs_by_path: dict[Path, list[int]] = {}

    for idx, path in enumerate(leg_paths):
        if path is None:
            continue
        legs_by_path.setdefault(path, []).append(idx)
        if path not in file_cache:
            tail = read_transcript_since(path, 0)
            file_cache[path] = (tail.turns, tail.offset)
            file_cursors[path] = 0

    items: list[CompositeItem] = []
    flat_turns: list[Turn] = []

    for idx, leg in enumerate(ordered):
        if idx > 0:
            prev = ordered[idx - 1]
            items.append(
                HandoffBoundary(
                    leg_seq=leg.leg_seq,
                    from_provider=prev.provider,
                    from_model=prev.model,
                    to_provider=leg.provider,
                    to_model=leg.model,
                    capsule_manifest=dict(leg.capsule_manifest),
                    created_at=leg.created_at,
                )
            )
        path = leg_paths[idx]
        if path is None or path not in file_cache:
            continue
        turns, _offset = file_cache[path]
        start_idx = file_cursors[path]
        same_path_indices = legs_by_path[path]
        pos_in_same = same_path_indices.index(idx)
        if pos_in_same == len(same_path_indices) - 1:
            end_idx = len(turns)
        else:
            next_same_idx = same_path_indices[pos_in_same + 1]
            end_idx = _slice_end_for_reused_file(
                turns,
                start_idx,
                current_leg=leg,
                next_leg_overall=ordered[idx + 1],
                next_same_file_leg=ordered[next_same_idx],
            )
        file_cursors[path] = end_idx
        for turn in turns[start_idx:end_idx]:
            stamped = _stamp_turn_for_leg(turn, leg)
            items.append(stamped)
            flat_turns.append(stamped)

    tail_leg = ordered[-1]
    tail_path = leg_paths[-1]
    tail_offset = file_cache[tail_path][1] if tail_path in file_cache else 0
    return CompositeTranscript(
        items=tuple(items),
        turns=tuple(flat_turns),
        tail_leg=tail_leg,
        tail_path=tail_path,
        tail_offset=tail_offset,
    )
