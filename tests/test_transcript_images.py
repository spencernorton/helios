"""Images reach the person: parsed from every provider shape, cached once,
persisted by path, and drawn as thumbnails (roadmap Phase 1)."""
from __future__ import annotations

import base64
import json

import pytest

from helios.backend import media
from helios.backend.transcript import ImageRef, ToolResult, Turn, images_in, turn_from_record

# A valid 1x1 PNG.
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
ANTHROPIC = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
MCP = {"type": "image", "data": PNG, "mimeType": "image/png"}


def test_user_attachments_and_tool_result_images_are_kept():
    user = turn_from_record({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "what is this?"}, ANTHROPIC]}})
    assert user.role == "user" and user.images == [ImageRef("image/png", data=PNG)]

    tool = turn_from_record({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "screenshot"}, ANTHROPIC]}]}})
    assert tool.role == "tool"
    assert tool.tool_results[0].content == "screenshot"
    assert tool.tool_results[0].images == [ImageRef("image/png", data=PNG)]


def test_image_only_user_turn_still_has_content():
    turn = turn_from_record({"type": "user", "message": {"role": "user", "content": [ANTHROPIC]}})
    assert turn.has_content


def test_images_in_reads_mcp_and_nested_shapes():
    found = images_in({"content": [{"type": "text", "text": "x"}, MCP, {"content": [ANTHROPIC]}]})
    assert [r.data for r in found] == [PNG, PNG]


def test_media_cache_writes_once_and_round_trips_through_a_mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
    first = media.media_path(ImageRef("image/png", data=PNG))
    again = media.media_path(ImageRef("image/png", data=PNG))
    assert first == again and first.read_bytes() == base64.b64decode(PNG)
    assert first.parent == tmp_path / "media"

    block = media.image_block(ImageRef("image/png", data=PNG))
    assert block["source"] == {"type": "path", "media_type": "image/png", "path": str(first)}
    assert "data" not in json.dumps(block)  # never inline base64 in a mirror
    reread = turn_from_record({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [block]}]}})
    assert reread.tool_results[0].images == [ImageRef("image/png", path=str(first))]


def test_codex_image_items_become_tool_result_images(tmp_path):
    from helios.backend.process import codex_app_events as cae

    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    acc._capture_tool_result({"id": "v1", "type": "imageView", "path": str(tmp_path / "a.png")})
    acc._capture_tool_result({"id": "g1", "type": "imageGeneration", "status": "completed",
                              "savedPath": str(tmp_path / "gen.png"), "revisedPrompt": "a cat"})
    acc._capture_tool_result({"id": "m1", "type": "mcpToolCall", "status": "completed",
                              "result": {"content": [MCP]}})
    by_id = {r.tool_use_id: r for r in acc._tool_results}
    assert by_id["v1"].images == [ImageRef(path=str(tmp_path / "a.png"))]
    assert by_id["g1"].images == [ImageRef(path=str(tmp_path / "gen.png"))]
    assert by_id["g1"].content == "a cat"
    assert by_id["m1"].images == [ImageRef("image/png", data=PNG)]


def test_bubble_shows_thumbnails_for_turn_and_late_tool_images():
    pytest.importorskip("gi")
    from helios.backend.transcript import ToolUse
    from helios.widgets.message_bubble import MessageBubble

    turn = Turn(role="assistant", tool_uses=[ToolUse(name="browser_screenshot", input={}, id="s1")])
    bubble = MessageBubble(turn)
    assert bubble._images is None
    assert bubble.attach_tool_result(ToolResult("s1", "shot", images=[ImageRef("image/png", data=PNG)]))
    assert bubble._images is not None and bubble._images.count == 1


def test_tool_cards_offer_to_open_the_file_they_touched(tmp_path):
    pytest.importorskip("gi")
    from helios.widgets.message_bubble import _openable_path

    real = tmp_path / "report.pdf"
    real.write_bytes(b"%PDF-1.4")
    assert _openable_path({"file_path": str(real)}) == str(real)
    assert _openable_path({"file_path": "relative.txt"}) == ""
    assert _openable_path({"path": str(tmp_path / "missing.png")}) == ""
