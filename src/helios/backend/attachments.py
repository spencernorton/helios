"""Image attachments ride in the message text as ``@path`` references.

The composer inserts ``@/abs/path.png`` (or ``@"/path with spaces.png"``) for
pasted, dropped and screenshotted images, so queueing, drafts and recovery stay
plain text. Each driver turns the image references into its provider's native
image input at the wire; the text keeps the path so the agent can also act on
the file.

Cross-provider continuity capsules also supply pre-validated ``CarryoverImage``
bytes read once with ``O_NOFOLLOW`` + magic-byte sniffing so target legs encode
directly from memory (or an immutable ``0400`` content-addressed snapshot for
Codex App Server) without TOCTOU re-reads.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from helios.backend.product_brief import REFERENCE_ROLES, ReferenceArtifactRef
from helios.log import get_logger
from helios.paths import state_dir

if TYPE_CHECKING:
    from helios.backend.work_store import WorkStore

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

_CONTINUITY_MEDIA_TTL_SECONDS = 7 * 24 * 3600
_CONTINUITY_MEDIA_MAX_FILES = 200

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


def _read_raw_checked(path: str) -> bytes | None:
    try:
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            _log.info("image over %d bytes stays a text reference: %s", MAX_IMAGE_BYTES, path)
            return None
        with open(path, "rb") as fh:
            data = fh.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            return None
        return data
    except OSError:
        return None


def _b64(path: str) -> str | None:
    raw = _read_raw_checked(path)
    if raw is None:
        return None
    return base64.b64encode(raw).decode("ascii")


def _iter_carryover(
    carryover: Sequence[Any],
    seen_hashes: set[str],
) -> list[tuple[bytes, str, str, str]]:
    items: list[tuple[bytes, str, str, str]] = []
    for entry in carryover or ():
        data = getattr(entry, "data", b"")
        media = str(getattr(entry, "media_type", "") or "")
        digest = str(getattr(entry, "sha256", "") or "")
        ext = str(getattr(entry, "ext", "") or "")
        if (
            not isinstance(data, (bytes, bytearray))
            or not data
            or len(data) > MAX_IMAGE_BYTES
            or media not in IMAGE_TYPES.values()
        ):
            continue
        raw = bytes(data)
        if not digest:
            digest = hashlib.sha256(raw).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        if not ext or ext not in IMAGE_TYPES:
            ext = next((k for k, v in IMAGE_TYPES.items() if v == media), ".png")
        items.append((raw, media, digest, ext))
    return items


def _prune_continuity_media_dir(media_dir: Path) -> None:
    try:
        entries = []
        now = time.time()
        for child in media_dir.iterdir():
            if child.name.startswith("."):
                continue
            try:
                st = child.lstat()
            except OSError:
                continue
            if now - st.st_mtime > _CONTINUITY_MEDIA_TTL_SECONDS:
                try:
                    os.chmod(child, 0o600)
                    child.unlink()
                except OSError:
                    pass
                continue
            entries.append((st.st_mtime, child))
        if len(entries) > _CONTINUITY_MEDIA_MAX_FILES:
            entries.sort(key=lambda pair: pair[0])
            for _mtime, victim in entries[: len(entries) - _CONTINUITY_MEDIA_MAX_FILES]:
                try:
                    os.chmod(victim, 0o600)
                    victim.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def stage_codex_carryover_snapshot(data: bytes, sha256_hex: str, ext: str) -> str | None:
    """Write an immutable 0400 content-addressed image snapshot for Codex."""
    safe_hash = re.sub(r"[^0-9a-f]", "", (sha256_hex or "").lower())
    if len(safe_hash) != 64:
        safe_hash = hashlib.sha256(data).hexdigest()
    safe_ext = ext if ext in IMAGE_TYPES else ".png"
    media_dir = state_dir() / "continuity-media"
    try:
        media_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(media_dir, 0o700)
        dest = media_dir / f"{safe_hash}{safe_ext}"
        if dest.exists() and not dest.is_symlink():
            return str(dest)
        fd, tmp_name = tempfile.mkstemp(prefix=".img-", suffix=safe_ext, dir=media_dir)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp_path, 0o400)
            os.replace(tmp_path, dest)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        _prune_continuity_media_dir(media_dir)
        return str(dest)
    except OSError as exc:
        _log.warning("could not stage continuity image snapshot: %s", exc)
        return None


def anthropic_blocks(text: str, *, carryover: Sequence[Any] = ()) -> list[dict]:
    """Claude stream-json user content blocks."""
    blocks = []
    seen_hashes: set[str] = set()
    for path, media in mentioned_images(text):
        raw = _read_raw_checked(path)
        if raw is not None:
            seen_hashes.add(hashlib.sha256(raw).hexdigest())
            data = base64.b64encode(raw).decode("ascii")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    for raw, media, _digest, _ext in _iter_carryover(carryover, seen_hashes):
        data = base64.b64encode(raw).decode("ascii")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    return blocks


def codex_inputs(text: str, *, carryover: Sequence[Any] = ()) -> list[dict]:
    """Codex App Server ``turn/start`` input items (Codex reads the file)."""
    items: list[dict] = []
    seen_hashes: set[str] = set()
    for path, _ in mentioned_images(text):
        raw = _read_raw_checked(path)
        if raw is not None:
            seen_hashes.add(hashlib.sha256(raw).hexdigest())
            items.append({"type": "localImage", "path": path})
    for raw, _media, digest, ext in _iter_carryover(carryover, seen_hashes):
        staged = stage_codex_carryover_snapshot(raw, digest, ext)
        if staged:
            items.append({"type": "localImage", "path": staged})
    return items


def muse_inputs(text: str, *, carryover: Sequence[Any] = ()) -> list[dict]:
    """MSP ``turn/start`` input items (shape probed against ``muse serve``)."""
    items = []
    seen_hashes: set[str] = set()
    for path, media in mentioned_images(text):
        raw = _read_raw_checked(path)
        if raw is not None:
            seen_hashes.add(hashlib.sha256(raw).hexdigest())
            data = base64.b64encode(raw).decode("ascii")
            items.append({"type": "image", "mediaType": media, "base64Data": data})
    for raw, media, _digest, _ext in _iter_carryover(carryover, seen_hashes):
        data = base64.b64encode(raw).decode("ascii")
        items.append({"type": "image", "mediaType": media, "base64Data": data})
    return items


def kimi_blocks(text: str, *, carryover: Sequence[Any] = ()) -> list[dict]:
    """ACP image and explicitly mentioned text-resource content blocks."""
    blocks = []
    seen_hashes: set[str] = set()
    mentioned = mentioned_images(text)
    images = {path for path, _media in mentioned}
    for path, media in mentioned:
        raw = _read_raw_checked(path)
        if raw is not None:
            seen_hashes.add(hashlib.sha256(raw).hexdigest())
            data = base64.b64encode(raw).decode("ascii")
            blocks.append({"type": "image", "data": data, "mimeType": media})
    for raw, media, _digest, _ext in _iter_carryover(carryover, seen_hashes):
        data = base64.b64encode(raw).decode("ascii")
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
            with open(path, "rb") as fh:
                raw_text = fh.read()
            content = raw_text.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        blocks.append({"type": "resource", "resource": {
            "uri": f"file://{path}", "mimeType": "text/plain", "text": content,
        }})
    return blocks


def openai_parts(text: str, *, carryover: Sequence[Any] = ()) -> list[dict]:
    """OpenAI-compatible ``image_url`` content parts (OpenRouter)."""
    parts = []
    seen_hashes: set[str] = set()
    for path, media in mentioned_images(text):
        raw = _read_raw_checked(path)
        if raw is not None:
            seen_hashes.add(hashlib.sha256(raw).hexdigest())
            data = base64.b64encode(raw).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}})
    for raw, media, _digest, _ext in _iter_carryover(carryover, seen_hashes):
        data = base64.b64encode(raw).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}})
    return parts


openrouter_parts = openai_parts


def google_wire_text(text: str, *, carryover: Sequence[Any] = ()) -> str:
    """Stage verified carryover images to immutable 0400 paths for Google."""
    if not carryover:
        return text
    seen_hashes: set[str] = set()
    for path, _ in mentioned_images(text):
        raw = _read_raw_checked(path)
        if raw is not None:
            seen_hashes.add(hashlib.sha256(raw).hexdigest())
    staged_refs: list[str] = []
    for raw, _media, digest, ext in _iter_carryover(carryover, seen_hashes):
        staged = stage_codex_carryover_snapshot(raw, digest, ext)
        if not staged:
            raise OSError("could not stage carried-over image attachment for Google")
        staged_refs.append(f"@{staged}")
    if not staged_refs:
        return text
    refs_line = "[Carried-over image attachments: " + " ".join(staged_refs) + "]"
    return f"{text}\n\n{refs_line}" if text else refs_line


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
