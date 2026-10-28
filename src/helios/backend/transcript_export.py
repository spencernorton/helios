"""Export a session's turns to Markdown.

Every benchmark app (Zed, VS Code, Warp, Codex) can hand a conversation to
someone as a file; Helios could not. This renders the display turns — user
and assistant text, with tool calls named but not dumped — as one Markdown
document. Thinking spans and tool results are omitted: the export answers
"what was asked and answered", and raw tool output would bury it.

GTK-free so the slim CI lane exercises it directly.
"""

from __future__ import annotations

from helios.backend.transcript import Turn
from helios.backend.session_goals import strip_goal_envelope

_VISIBLE_KINDS = ("text", "commentary")


def export_markdown(
    turns: list[Turn],
    *,
    title: str = "",
    provider: str = "",
    model: str = "",
) -> str:
    """One Markdown document for ``turns``. Empty input yields just a header."""
    lines: list[str] = [f"# {title or 'Helios chat'}", ""]
    meta = " · ".join(part for part in (provider, model) if part)
    if meta:
        lines += [meta, ""]
    for turn in turns:
        if turn.is_sidechain or turn.is_meta:
            continue
        body = "\n\n".join(
            span.text.strip()
            for span in turn.content
            if span.kind in _VISIBLE_KINDS and span.text.strip()
        )
        tools = [f"`{tool.name}`" for tool in turn.tool_uses if tool.name]
        if not body and not tools:
            continue
        heading = "You" if turn.role == "user" else "Assistant"
        if turn.role == "user":
            body = strip_goal_envelope(body)
        lines.append(f"## {heading}")
        lines.append("")
        if body:
            lines.append(body)
            lines.append("")
        if tools:
            lines.append("Tools: " + ", ".join(tools))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
