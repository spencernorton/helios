"""Image input: @path image references go to each provider as real images
(roadmap Phase 1b), stay plain text everywhere else, and show in the composer."""
from __future__ import annotations

import base64
import json

import pytest

from helios.backend import attachments

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def images(tmp_path):
    plain = tmp_path / "shot.png"
    spaced = tmp_path / "My Shots" / "screen 1.jpg"
    spaced.parent.mkdir()
    for path in (plain, spaced):
        path.write_bytes(PNG)
    (tmp_path / "notes.txt").write_text("x")
    return plain, spaced, tmp_path


def test_mentions_find_existing_absolute_images_once(images):
    plain, spaced, root = images
    quoted = '@"' + str(spaced) + '"'
    text = f"look at @{plain} and {quoted}, again @{plain}, @{root}/notes.txt, @{root}/gone.png, @rel.png"
    assert attachments.mentioned_images(text) == [(str(plain), "image/png"), (str(spaced), "image/jpeg")]


def test_each_provider_gets_its_native_image_shape(images):
    plain, _spaced, _root = images
    text = f"what is in @{plain}?"
    data = base64.b64encode(PNG).decode()
    assert attachments.anthropic_blocks(text) == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]
    assert attachments.codex_inputs(text) == [{"type": "localImage", "path": str(plain)}]
    # Probed against `muse serve` turn/start (2026-10-22).
    assert attachments.muse_inputs(text) == [{"type": "image", "mediaType": "image/png", "base64Data": data}]
    assert attachments.openai_parts(text)[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_oversized_images_stay_text_references(images, monkeypatch):
    plain, _spaced, _root = images
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 10)
    text = f"@{plain}"
    assert attachments.anthropic_blocks(text) == []
    assert attachments.codex_inputs(text) == []


def test_mirrored_user_turns_keep_their_images(images, tmp_path, monkeypatch):
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    plain, _spaced, _root = images
    writer = CodexTranscriptWriter(str(tmp_path))
    writer.bind_thread("thread-1")
    writer.note_user_text(f"see @{plain}")
    turns = parse_transcript(writer._path())
    assert turns[0].role == "user"
    assert [i.path for i in turns[0].images] == [str(plain)]
    assert "base64" not in json.dumps([json.loads(line) for line in writer._path().read_text().splitlines()])


def test_composer_shows_thumbnails_for_attached_images(images):
    pytest.importorskip("gi")
    from helios.widgets.composer import Composer

    plain, _spaced, _root = images
    composer = Composer()
    assert not composer._thumbs_slot.get_visible()
    composer.add_attachments([str(plain)])
    assert composer._thumbs_slot.get_visible()
    composer._textview.get_buffer().set_text("")
    assert not composer._thumbs_slot.get_visible()
