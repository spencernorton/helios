"""Export renders the readable conversation, not the machinery."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from helios.backend.transcript import ToolUse, Turn  # noqa: E402
from helios.backend.transcript_export import export_markdown  # noqa: E402


def test_export_renders_user_and_assistant_text():
    turns = [
        Turn("user", text_parts=["Fix the login bug"]),
        Turn("assistant", text_parts=["Fixed it."], tool_uses=[ToolUse("Edit", {})]),
    ]
    out = export_markdown(turns, title="Login fix", provider="Claude")
    assert out.startswith("# Login fix")
    assert "Claude" in out
    assert "## You\n\nFix the login bug" in out
    assert "## Assistant\n\nFixed it." in out
    assert "Tools: `Edit`" in out


def test_export_skips_thinking_sidechain_and_empty_turns():
    turns = [
        Turn("assistant", thinking_parts=["secret scratch"], text_parts=["Answer"]),
        Turn("assistant", text_parts=["child"], is_sidechain=True),
        Turn("user"),
    ]
    out = export_markdown(turns)
    assert "Answer" in out
    assert "secret scratch" not in out
    assert "child" not in out


def test_export_empty_history_is_header_only():
    assert export_markdown([]) == "# Helios chat\n"
