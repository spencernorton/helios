"""Image files for transcript images.

Inline base64 images are written once, content-addressed, under
``state_dir()/media``: the side pane, other apps and the file manager all get
a real file, and provider mirrors store a path instead of megabytes of base64.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
from pathlib import Path

from helios import paths
from helios.backend.transcript import ImageRef
from helios.log import get_logger

_log = get_logger("media")


def media_path(ref: ImageRef) -> Path | None:
    """A file holding ``ref``'s pixels; writes inline data to the media cache."""
    if ref.path:
        path = Path(ref.path).expanduser()
        return path if path.is_file() else None
    try:
        raw = base64.b64decode(ref.data, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    ext = mimetypes.guess_extension(ref.media_type) or ".png"
    target = paths.state_dir() / "media" / f"{hashlib.sha256(raw).hexdigest()[:24]}{ext}"
    if not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(target)
        except OSError as exc:
            _log.warning("could not cache image: %s", exc)
            return None
    return target


def image_block(ref: ImageRef) -> dict | None:
    """A transcript ``image`` block that points at a file, never inline data."""
    path = media_path(ref)
    if path is None:
        return None
    return {"type": "image", "source": {"type": "path", "media_type": ref.media_type, "path": str(path)}}
