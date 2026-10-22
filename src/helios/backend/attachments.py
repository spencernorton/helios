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
# ponytail: the Anthropic per-image cap; a larger image stays a text @path the
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


def openai_parts(text: str) -> list[dict]:
    """OpenAI-compatible ``image_url`` content parts (OpenRouter)."""
    parts = []
    for path, media in mentioned_images(text):
        data = _b64(path)
        if data:
            parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}})
    return parts
