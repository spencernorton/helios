"""Local markdown links must resolve to paths the window can open.

Regression: an image-gen chat answered with
`![logo](/home/spencer/Pictures/logo.png)` and `[Download](/home/…)` —
both rendered, neither opened, because a scheme-less path is not a URI
Gtk.Label can activate and nothing intercepted the click.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios.widgets.markdown import _local_link_path  # noqa: E402


def test_bare_absolute_path_is_local():
    assert _local_link_path("/home/spencer/Pictures/logo.png") == (
        "/home/spencer/Pictures/logo.png"
    )


def test_file_uri_is_local_and_unquoted():
    assert _local_link_path("file:///home/spencer/My%20Logo.png") == (
        "/home/spencer/My Logo.png"
    )
    assert _local_link_path("file://localhost/tmp/a.png") == "/tmp/a.png"


def test_web_and_dangerous_links_are_not_local():
    assert _local_link_path("https://example.com/logo.png") is None
    assert _local_link_path("javascript:alert(1)") is None
    assert _local_link_path("relative/path.png") is None
