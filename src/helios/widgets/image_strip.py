"""Thumbnails for images in a turn: attachments, screenshots, image results.

Click opens the image in the side pane (``win.open-media``); right-click
copies it. Base64 images are written once to ``state_dir()/media`` so the
pane, other apps and the person's file manager all see one real file.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from helios.backend.media import media_path  # noqa: E402
from helios.backend.transcript import ImageRef  # noqa: E402

THUMB_HEIGHT = 140


def _texture(ref: ImageRef) -> Gdk.Texture | None:
    try:
        if ref.path:
            return Gdk.Texture.new_from_filename(str(Path(ref.path).expanduser()))
        raw = base64.b64decode(ref.data, validate=False)
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
    except (GLib.Error, binascii.Error, ValueError, OSError):
        return None


def _thumbnail(ref: ImageRef) -> tuple[Gdk.Texture, int, int] | None:
    """A texture at most THUMB_HEIGHT tall, plus the original size. A Picture
    grows to its paintable's natural size, so scaling here is what keeps a
    1920px screenshot a thumbnail (and keeps long chats light)."""
    try:
        if ref.path:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(Path(ref.path).expanduser()))
        else:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(base64.b64decode(ref.data, validate=False))
            loader.close()
            pixbuf = loader.get_pixbuf()
    except (GLib.Error, binascii.Error, ValueError, OSError):
        return None
    if pixbuf is None:
        return None
    width, height = pixbuf.get_width(), pixbuf.get_height()
    if height > THUMB_HEIGHT or width > 2 * THUMB_HEIGHT * 2:
        scale = min(THUMB_HEIGHT / height, (4 * THUMB_HEIGHT) / width)
        pixbuf = pixbuf.scale_simple(
            max(1, round(width * scale)), max(1, round(height * scale)),
            GdkPixbuf.InterpType.BILINEAR)
    return Gdk.Texture.new_for_pixbuf(pixbuf), width, height


class ImageStrip(Gtk.FlowBox):
    """A wrapping row of clickable thumbnails."""

    def __init__(self, images: list[ImageRef] | None = None) -> None:
        super().__init__()
        self.set_selection_mode(Gtk.SelectionMode.NONE)
        self.set_homogeneous(False)
        self.set_max_children_per_line(6)
        self.set_column_spacing(6)
        self.set_row_spacing(6)
        self.add_css_class("helios-image-strip")
        self.count = 0
        for ref in images or []:
            self.add_image(ref)

    def add_image(self, ref: ImageRef) -> None:
        thumb = _thumbnail(ref)
        if thumb is None:
            label = Gtk.Label(label="Image could not be decoded")
            label.add_css_class("dim-label")
            label.add_css_class("caption")
            self.append(label)
            self.count += 1
            return
        texture, width, height = thumb
        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_can_shrink(False)
        picture.set_alternative_text(f"Image, {width}×{height}")

        button = Gtk.Button(child=picture)
        button.add_css_class("flat")
        button.add_css_class("helios-image-thumb")
        button.set_tooltip_text(f"{width}×{height} — click to open, right-click to copy")
        button.connect("clicked", lambda *_: self._open(ref))
        menu = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu.connect("pressed", lambda *_: self._copy(ref))
        button.add_controller(menu)
        self.append(button)
        self.count += 1

    def _open(self, ref: ImageRef) -> None:
        path = media_path(ref)
        if path is not None:
            self.activate_action("win.open-media", GLib.Variant.new_string(str(path)))

    def _copy(self, ref: ImageRef) -> None:
        texture = _texture(ref)  # full size, not the thumbnail
        if texture is not None:
            self.get_clipboard().set_texture(texture)
