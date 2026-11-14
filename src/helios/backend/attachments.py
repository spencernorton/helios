"""Image attachments ride in the message text as ``@path`` references.

The composer inserts ``@/abs/path.png`` (or ``@"/path with spaces.png"``) for
pasted, dropped and screenshotted images, so queueing, drafts and recovery stay
plain text. Each driver turns the image references into its provider's native
image input at the wire; the text keeps the path so the agent can also act on
the file.
"""

from __future__ import annotations

import base64
import os
import re

from helios.log import get_logger

_log = get_logger("attachments")

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# the Anthropic per-image cap; a larger image stays a text @path the
# agent can open with its own tools. Downscale here if that proves common.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_MENTION = re.compile(r'@"((?:[^"\\]|\\.)*)"|@([^\s"]+)')


def mentioned_images(text: str) -> list[tuple[str, str]]:
    """``(path, media_type)`` for each existing absolute image file referenced
    with ``@``, in order, once each."""
    found: list[tuple[str, str]] = []
    for quoted, bare in _MENTION.findall(text or ""):
        # A bare reference ends at whitespace, so prose punctuation sticks to
        # it ("…in @/a/shot.png?"); a quoted one is exact.
        path = re.sub(r"\\(.)", r"\1", quoted) if quoted else bare.rstrip(".,;:!?)]}'")
        media = IMAGE_TYPES.get(os.path.splitext(path)[1].lower())
        if not media or not os.path.isabs(path) or not os.path.isfile(path):
            continue
        if path not in (p for p, _ in found):
            found.append((path, media))
    return found


def _b64(path: str) -> str | None:
    try:
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            _log.info("image over %d bytes stays a text reference: %s", MAX_IMAGE_BYTES, path)
            return None
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return None


def anthropic_blocks(text: str) -> list[dict]:
    """Claude stream-json user content blocks."""
    blocks = []
    for path, media in mentioned_images(text):
        data = _b64(path)
        if data:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    return blocks


def codex_inputs(text: str) -> list[dict]:
    """Codex App Server ``turn/start`` input items (Codex reads the file)."""
    return [
        {"type": "localImage", "path": path}
        for path, _ in mentioned_images(text)
        if os.path.getsize(path) <= MAX_IMAGE_BYTES
    ]


def muse_inputs(text: str) -> list[dict]:
    """MSP ``turn/start`` input items (shape probed against ``muse serve``)."""
    items = []
    for path, media in mentioned_images(text):
        data = _b64(path)
        if data:
            items.append({"type": "image", "mediaType": media, "base64Data": data})
    return items


def kimi_blocks(text: str) -> list[dict]:
    """ACP image and explicitly mentioned text-resource content blocks."""
    blocks = []
    images = {path for path, _media in mentioned_images(text)}
    for path, media in mentioned_images(text):
        data = _b64(path)
        if data:
            blocks.append({"type": "image", "data": data, "mimeType": media})
    seen: set[str] = set()
    for quoted, bare in _MENTION.findall(text or ""):
        path = re.sub(r"\\(.)", r"\1", quoted) if quoted else bare.rstrip(".,;:!?)]}'")
        if path in seen or path in images or not os.path.isabs(path) or not os.path.isfile(path):
            continue
        seen.add(path)
        try:
            if os.path.getsize(path) > 1024 * 1024:
                continue
            raw = open(path, "rb").read()
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        blocks.append({"type": "resource", "resource": {
            "uri": f"file://{path}", "mimeType": "text/plain", "text": content,
        }})
    return blocks


def openai_parts(text: str) -> list[dict]:
    """OpenAI-compatible ``image_url`` content parts (OpenRouter)."""
    parts = []
    for path, media in mentioned_images(text):
        data = _b64(path)
        if data:
            parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}})
    return parts


from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from helios.backend.product_brief import REFERENCE_ROLES, ReferenceArtifactRef

if TYPE_CHECKING:
    from helios.backend.work_store import WorkStore

AttachmentDisposition = Literal[
    "image_prepared",
    "text_only_reference",
    "missing",
    "too_large",
    "unsupported",
]


@dataclass(frozen=True, slots=True)
class AttachmentItemStatus:
    """Per-reference resolution and provider-wire disposition."""

    reference: str
    resolved_path: str
    artifact_id: str
    media_type: str
    role: str
    disposition: AttachmentDisposition
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AttachmentDeliveryReport:
    """Structured attachment payload plus per-item diagnostic dispositions."""

    provider: str
    wire_items: tuple[dict[str, Any], ...]
    items: tuple[AttachmentItemStatus, ...]


def _extract_raw_mentions(text: str) -> list[str]:
    paths: list[str] = []
    for quoted, bare in _MENTION.findall(text or ""):
        raw = re.sub(r"\\(.)", r"\1", quoted) if quoted else bare.rstrip(".,;:!?)]}'")
        if raw and raw not in paths:
            paths.append(raw)
    return paths


def persist_reference_attachment(
    store: "WorkStore",
    work_id: str,
    path: str | Path,
    *,
    role: str = "target",
    caption: str = "",
    source_message_id: str = "",
) -> ReferenceArtifactRef:
    """Copy a reference image into the Work artifact store and link it to ProductBrief."""

    src = Path(path)
    ext = src.suffix.lower()
    media = IMAGE_TYPES.get(ext, "application/octet-stream")
    data = src.read_bytes()
    # Keep Artifact.metadata deterministic across duplicate registrations of the
    # same image bytes; per-message role/caption live on ReferenceArtifactRef.
    artifact = store.publish_artifact(
        work_id,
        data,
        media_type=media,
        metadata={"kind": "reference_attachment"},
    )
    normalized_role = role if role in REFERENCE_ROLES else "target"
    ref = ReferenceArtifactRef(
        artifact_id=artifact.artifact_id,
        digest=artifact.digest,
        role=normalized_role,
        caption=caption.strip(),
        media_type=media,
        source_message_id=source_message_id.strip(),
        original_filename=src.name,
    )
    brief = store.get_product_brief(work_id)
    existing_refs = list(brief.reference_artifacts)
    replaced = False
    for idx, item in enumerate(existing_refs):
        if item.artifact_id == ref.artifact_id:
            existing_refs[idx] = ReferenceArtifactRef(
                artifact_id=ref.artifact_id,
                digest=ref.digest,
                role=normalized_role if role != "target" or not item.role else item.role,
                caption=ref.caption or item.caption,
                media_type=ref.media_type,
                source_message_id=ref.source_message_id or item.source_message_id,
                original_filename=ref.original_filename or item.original_filename,
            )
            ref = existing_refs[idx]
            replaced = True
            break
    if not replaced:
        existing_refs.append(ref)
    msg_ids = list(brief.source_message_ids)
    if source_message_id and source_message_id not in msg_ids:
        msg_ids.append(source_message_id)
    store.update_product_brief(
        work_id,
        expected_revision=brief.revision,
        author_role="user",
        reference_artifacts=existing_refs,
        source_message_ids=msg_ids,
        summary=f"Attached reference artifact {src.name} ({normalized_role})",
    )
    return ref


def _wire_item_for_provider(provider: str, path: str, media: str) -> dict[str, Any] | None:
    if provider == "anthropic":
        data = _b64(path)
        return (
            {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
            if data
            else None
        )
    if provider == "openai":
        return {"type": "localImage", "path": path}
    if provider == "muse":
        data = _b64(path)
        return {"type": "image", "mediaType": media, "base64Data": data} if data else None
    if provider == "kimi":
        data = _b64(path)
        return {"type": "image", "data": data, "mimeType": media} if data else None
    if provider == "openrouter":
        data = _b64(path)
        return (
            {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
            if data
            else None
        )
    return None


def resolve_for_provider(
    provider: str,
    text: str = "",
    *,
    store: "WorkStore | None" = None,
    work_id: str = "",
    include_brief_references: bool = False,
) -> AttachmentDeliveryReport:
    """Resolve @path mentions and Work reference artifacts for ``provider``."""

    brief_refs: tuple[ReferenceArtifactRef, ...] = ()
    by_filename: dict[str, ReferenceArtifactRef] = {}
    by_artifact_id: dict[str, ReferenceArtifactRef] = {}
    if store is not None and work_id:
        try:
            brief = store.get_product_brief(work_id)
            brief_refs = brief.reference_artifacts
            for ref in brief_refs:
                by_artifact_id[ref.artifact_id] = ref
                if ref.original_filename and ref.original_filename not in by_filename:
                    by_filename[ref.original_filename] = ref
        except Exception as exc:
            _log.warning("could not inspect brief references for %s: %s", work_id, exc)

    candidates: list[tuple[str, str, str, str]] = []  # (reference, resolved_path, artifact_id, role)
    seen_resolved: set[str] = set()

    for raw in _extract_raw_mentions(text):
        ext = os.path.splitext(raw)[1].lower()
        media = IMAGE_TYPES.get(ext)
        if not media and raw not in by_artifact_id:
            continue
        matched_ref = by_artifact_id.get(raw) or by_filename.get(os.path.basename(raw))
        resolved_path = raw
        artifact_id = matched_ref.artifact_id if matched_ref else ""
        role = matched_ref.role if matched_ref else "target"
        if (not os.path.isabs(resolved_path) or not os.path.isfile(resolved_path)) and (
            store is not None and work_id and matched_ref is not None
        ):
            art = store.get_artifact(work_id, matched_ref.artifact_id)
            if art is not None and art.path.is_file():
                resolved_path = str(art.path)
        key = artifact_id or resolved_path
        if key not in seen_resolved:
            seen_resolved.add(key)
            candidates.append((raw, resolved_path, artifact_id, role))

    if include_brief_references and store is not None and work_id:
        for ref in brief_refs:
            if ref.artifact_id in seen_resolved:
                continue
            art = store.get_artifact(work_id, ref.artifact_id)
            resolved_path = str(art.path) if art is not None else ""
            seen_resolved.add(ref.artifact_id)
            candidates.append(
                (
                    ref.original_filename or ref.artifact_id,
                    resolved_path,
                    ref.artifact_id,
                    ref.role,
                )
            )

    wire_items: list[dict[str, Any]] = []
    statuses: list[AttachmentItemStatus] = []
    for reference, resolved_path, artifact_id, role in candidates:
        ext = os.path.splitext(reference)[1].lower()
        media = IMAGE_TYPES.get(ext)
        if not media and artifact_id in by_artifact_id:
            media = by_artifact_id[artifact_id].media_type
        if not media or media not in IMAGE_TYPES.values():
            statuses.append(
                AttachmentItemStatus(
                    reference=reference,
                    resolved_path=resolved_path,
                    artifact_id=artifact_id,
                    media_type=media or "application/octet-stream",
                    role=role,
                    disposition="unsupported",
                    detail="Unsupported media type",
                )
            )
            continue
        if not resolved_path or not os.path.isfile(resolved_path):
            statuses.append(
                AttachmentItemStatus(
                    reference=reference,
                    resolved_path=resolved_path,
                    artifact_id=artifact_id,
                    media_type=media,
                    role=role,
                    disposition="missing",
                    detail="Image file and Work artifact copy are both absent",
                )
            )
            continue
        try:
            size = os.path.getsize(resolved_path)
        except OSError:
            size = MAX_IMAGE_BYTES + 1
        if size > MAX_IMAGE_BYTES:
            statuses.append(
                AttachmentItemStatus(
                    reference=reference,
                    resolved_path=resolved_path,
                    artifact_id=artifact_id,
                    media_type=media,
                    role=role,
                    disposition="too_large",
                    detail=f"Exceeds {MAX_IMAGE_BYTES} byte limit; kept as path reference",
                )
            )
            continue
        if provider == "google":
            statuses.append(
                AttachmentItemStatus(
                    reference=reference,
                    resolved_path=resolved_path,
                    artifact_id=artifact_id,
                    media_type=media,
                    role=role,
                    disposition="text_only_reference",
                    detail="Driver delivers path reference in prompt text",
                )
            )
            continue
        item = _wire_item_for_provider(provider, resolved_path, media)
        if item is None:
            statuses.append(
                AttachmentItemStatus(
                    reference=reference,
                    resolved_path=resolved_path,
                    artifact_id=artifact_id,
                    media_type=media,
                    role=role,
                    disposition="unsupported",
                    detail=f"No native wire image builder for provider {provider!r}",
                )
            )
            continue
        wire_items.append(item)
        statuses.append(
            AttachmentItemStatus(
                reference=reference,
                resolved_path=resolved_path,
                artifact_id=artifact_id,
                media_type=media,
                role=role,
                disposition="image_prepared",
            )
        )

    return AttachmentDeliveryReport(
        provider=provider,
        wire_items=tuple(wire_items),
        items=tuple(statuses),
    )
