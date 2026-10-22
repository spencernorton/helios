"""Fork a Claude conversation from one of your messages.

A Claude transcript is append-only JSONL whose records chain by
``parentUuid``, and ``claude --resume`` continues from the last record. So the
records BEFORE a user message, copied under a new session id, are a complete
conversation that ends where that message was about to be sent — the same
"edit and resend from here" the CLI's own ``--fork-session`` offers at the head.
The source file is never modified.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path


class ForkError(RuntimeError):
    """The transcript cannot be forked at that message."""


def _message_text(record: dict) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def fork_before(path: Path, message_uuid: str) -> tuple[str, Path, str]:
    """Copy ``path`` up to (not including) the user message ``message_uuid``.

    Returns ``(new_session_id, new_path, message_text)``.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ForkError(f"Cannot read the conversation: {exc}") from exc
    cut = None
    text = ""
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("uuid") == message_uuid:
            if record.get("type") != "user":
                raise ForkError("Fork from one of your own messages.")
            cut, text = index, _message_text(record)
            break
    if cut is None:
        raise ForkError("That message is not in this conversation file.")
    if not any(line.strip() for line in lines[:cut]):
        raise ForkError("This is the first message — start a new chat instead.")

    new_id = str(uuid.uuid4())
    out: list[str] = []
    for line in lines[:cut]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn line never belongs in the copy
        if "sessionId" in record:
            record["sessionId"] = new_id
        out.append(json.dumps(record, ensure_ascii=False))
    target = path.with_name(f"{new_id}.jsonl")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".fork-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        raise ForkError(f"Cannot write the fork: {exc}") from exc
    return new_id, target, text
