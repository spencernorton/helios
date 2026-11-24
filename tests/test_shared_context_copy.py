"""Tests for Shared Context async Copy full entry action."""

from __future__ import annotations

import threading
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("Gtk 4.0 / Adw 1 unavailable", allow_module_level=True)

from gi.repository import Adw, GLib  # noqa: E402

from helios.backend import scratchpad  # noqa: E402
from helios.widgets import shared_context_pane as SCP  # noqa: E402

Adw.init()


def _pump(predicate, timeout: float = 5.0) -> bool:
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.iteration(False):
            if predicate():
                return True
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_copy_full_entry_copies_uncapped_unicode_structured_payload(monkeypatch) -> None:
    payload = {
        "note": "café naïve 🚀 日本語",
        "blob": "A" * (120 * 1024),
    }
    expected_full = scratchpad.format_full_data(payload)
    entry = scratchpad.Entry(key="handoff/big", summary="Large handoff", data=payload)
    monkeypatch.setattr(SCP.scratchpad, "read_entry", lambda k: entry)

    copied: list[str] = []
    pane = SCP.SharedContextPane()
    pane._write_clipboard = lambda text: copied.append(text)
    try:
        assert pane._copy_btn.get_visible() is False
        row = SCP._EntryRow(entry)
        pane._on_row_activated(pane._listbox, row)
        assert pane._copy_btn.get_visible() is True

        buf = pane._detail_data.get_buffer()
        assert _pump(lambda: buf.get_char_count() > 0)
        preview = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        assert len(preview) < len(expected_full)
        assert preview.endswith("use Copy full entry above)")

        pane._on_copy_full_clicked()
        assert _pump(lambda: bool(copied))
        assert copied == [expected_full]
        assert pane._copy_btn.get_sensitive() is True
        assert "Copied full entry" in pane._copy_status.get_label()
    finally:
        pane.shutdown()


def test_copy_full_entry_ignores_stale_selection_and_handles_missing_entry(monkeypatch) -> None:
    gate = threading.Event()
    first = scratchpad.Entry(key="entry/one", summary="One", data="first-full-payload")
    second = scratchpad.Entry(key="entry/two", summary="Two", data="second-full-payload")

    def fake_read(key: str):
        if key == "entry/one":
            gate.wait(timeout=2.0)
            return first
        if key == "entry/missing":
            raise scratchpad.ScratchpadError("scratchpad HTTP 404 on /scratch/read/entry/missing")
        return second

    monkeypatch.setattr(SCP.scratchpad, "read_entry", fake_read)
    copied: list[str] = []
    pane = SCP.SharedContextPane()
    pane._write_clipboard = lambda text: copied.append(text)
    try:
        pane._on_row_activated(pane._listbox, SCP._EntryRow(first))
        pane._on_copy_full_clicked()
        # Switch to entry/two while entry/one copy is still in flight.
        pane._on_row_activated(pane._listbox, SCP._EntryRow(second))
        gate.set()
        time.sleep(0.05)
        ctx = GLib.MainContext.default()
        for _ in range(20):
            ctx.iteration(False)
        # Stale completion from entry/one must NEVER write to clipboard.
        assert copied == []

        # Missing/expired entry surfaces an honest recoverable error and keeps Copy enabled.
        missing = scratchpad.Entry(key="entry/missing", summary="Gone")
        pane._on_row_activated(pane._listbox, SCP._EntryRow(missing))
        pane._on_copy_full_clicked()
        assert _pump(lambda: "Could not copy full entry" in pane._copy_status.get_label())
        assert "HTTP 404" in pane._copy_status.get_label()
        assert pane._copy_btn.get_sensitive() is True
        assert copied == []

        # Going back to the list hides the Copy button cleanly.
        pane._show_list()
        assert pane._copy_btn.get_visible() is False
    finally:
        pane.shutdown()
