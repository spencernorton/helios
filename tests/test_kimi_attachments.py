from __future__ import annotations

import base64

from helios.backend.attachments import kimi_blocks


def test_kimi_native_image_and_resource_blocks(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgo="))
    note = tmp_path / "note.txt"
    note.write_text("attached context")
    blocks = kimi_blocks(f"Inspect @{image} and @\"{note}\"")
    assert blocks[0]["type"] == "image"
    assert blocks[0]["mimeType"] == "image/png"
    assert base64.b64decode(blocks[0]["data"]) == image.read_bytes()
    assert blocks[1] == {"type": "resource", "resource": {
        "uri": f"file://{note}", "mimeType": "text/plain", "text": "attached context",
    }}


def test_kimi_resources_are_explicit_bounded_utf8(tmp_path):
    binary = tmp_path / "data.bin"
    binary.write_bytes(b"\xff\x00")
    large = tmp_path / "large.txt"
    large.write_text("x" * (1024 * 1024 + 1))
    assert kimi_blocks(f"@{binary} @{large}") == []
