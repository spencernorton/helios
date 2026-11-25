"""Context ring and hover popover: colours by category, no restated text.

Reported: the popover's legend and bar were all one grey for Claude (its
measured categories are named "System prompt", "MCP tools", … and none had a
colour), the ring was a single accent arc, and the popover spent half its
height on paragraphs repeating the bar and legend.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("GTK display unavailable", allow_module_level=True)

from helios.backend import context_breakdown  # noqa: E402
from helios.widgets import chat_toolbar as ct  # noqa: E402

# Shape and names as claude 2.1.332 reports them from get_context_usage.
PAYLOAD = {
    "maxTokens": 350000,
    "totalTokens": 127183,
    "categories": [
        {"name": "System prompt", "tokens": 2646},
        {"name": "System tools", "tokens": 3824},
        {"name": "MCP tools", "tokens": 706},
        {"name": "MCP server instructions", "tokens": 791},
        {"name": "MCP tools (deferred)", "tokens": 33734, "isDeferred": True},
        {"name": "Custom agents", "tokens": 981},
        {"name": "Memory files", "tokens": 9404},
        {"name": "Skills", "tokens": 7157},
        {"name": "Messages", "tokens": 101674},
        {"name": "Autocompact buffer", "tokens": 33000},
        {"name": "Free space", "tokens": 189817},
    ],
    "memoryFiles": [{"path": "/work/AGENTS.md", "tokens": 9404}],
    "messageBreakdown": {"toolResultTokens": 60000, "userMessageTokens": 6791},
    "autoCompactThreshold": 317000,
    "isAutoCompactEnabled": True,
}
MEASURED = [
    "system prompt", "system tools", "mcp tools", "mcp server instructions",
    "custom agents", "memory files", "skills", "messages",
]


@pytest.mark.parametrize("dark", [False, True])
def test_every_measured_claude_category_has_its_own_colour(dark):
    colours = [ct._segment_rgb(key, dark) for key in MEASURED]
    assert len(set(colours)) == len(MEASURED)
    other = ct._segment_rgb("something new", dark)
    reserved = ct._segment_rgb(context_breakdown.AUTOCOMPACT, dark)
    assert other not in colours and reserved not in colours
    # The estimate's own categories keep their existing palette.
    assert ct._segment_rgb("tools", dark) == (
        ct._SEGMENT_RGB_DARK if dark else ct._SEGMENT_RGB_LIGHT
    )["tools"]


def test_messages_is_never_the_alarm_red():
    # The largest slice in a normal session must not read as the 95% alarm.
    for dark in (False, True):
        r, g, b = ct._segment_rgb("messages", dark)
        assert b > r and b > g


def _popover():
    pop = ct._ContextPopover()
    pop.set_window(350000)
    pop.set_model("claude-opus-5-5")
    pop.set_usage(127183, 350000)
    return pop


def test_popover_states_usage_once_on_the_bar_and_keeps_details_off_screen():
    pop = _popover()
    pop.set_measured_breakdown(PAYLOAD)

    assert pop._max_label.get_label() == "127.2k / 350k · 36%"
    for gone in ("_usage_label", "_legend_note", "_details_label", "_model_label"):
        assert not hasattr(pop, gone), gone
    # The parts list and provenance stay available on hover.
    assert "Memory files" in (pop._legend.get_tooltip_text() or "")
    title_tip = pop._ctx_title.get_tooltip_text() or ""
    assert "Measured by the provider" in title_tip
    assert "33,734" in title_tip  # deferred schemas, not in the window
    labels = _labels(pop._legend)
    assert "≈" not in " ".join(labels)
    # Shares are of the window, matching the bar's widths.
    assert "101.7k · 29%" in labels


def test_legend_shares_follow_a_window_change():
    pop = _popover()
    pop.set_measured_breakdown(PAYLOAD)
    assert "101.7k · 29%" in _labels(pop._legend)
    pop.set_window(1_000_000)
    labels = _labels(pop._legend)
    assert "101.7k · 10%" in labels
    assert pop._max_label.get_label() == "127.2k / 1M · 12%"


def test_estimated_split_is_marked_on_every_value():
    pop = _popover()
    breakdown = context_breakdown.Breakdown(
        used=1000,
        window=350000,
        segments=(
            context_breakdown.Segment("baseline", "System, tools & memory", 600),
            context_breakdown.Segment("tools", "Tool calls & results", 400),
        ),
    )
    pop.set_breakdown(breakdown)
    values = [v for v in _labels(pop._legend) if "·" in v]
    assert values and all(v.startswith("≈") for v in values)
    assert "estimated" in (pop._ctx_title.get_tooltip_text() or "")


def test_empty_limits_are_one_short_line_with_the_reason_on_hover():
    pop = ct._ContextPopover()
    pop.set_model("gemini-4-neon")
    assert pop._limits_empty.get_label() == "Not reported by Antigravity"
    assert "Antigravity reports token counts" in pop._limits_empty.get_tooltip_text()


def test_ring_wears_the_legend_colours_and_skips_the_reserve():
    toolbar = ct.ChatToolbar()
    try:
        toolbar.set_context_model("claude-opus-5-5")
        toolbar.set_context_usage(127183, 350000)
        toolbar.set_measured_breakdown(PAYLOAD)
        keys = [key for key, _tokens in toolbar._context_meter._segments]
        assert keys == MEASURED
        assert context_breakdown.AUTOCOMPACT not in keys

        # A late transcript estimate never repaints a measured ring.
        toolbar.set_context_breakdown(
            context_breakdown.Breakdown(
                used=1, window=350000,
                segments=(context_breakdown.Segment("tools", "Tool calls & results", 1),),
            )
        )
        assert [k for k, _t in toolbar._context_meter._segments] == MEASURED

        # A reset (new chat) clears the colours with the usage.
        toolbar.set_context_usage(0, 0)
        assert toolbar._context_meter._segments == ()
    finally:
        toolbar.shutdown()


def _labels(widget) -> list[str]:
    out = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            out.append(child.get_label())
        out.extend(_labels(child))
        child = child.get_next_sibling()
    return out


# ── Hover flicker (reported on 0.123.0) ───────────────────────────────────


def _bar_keys(pop) -> list[str]:
    return [s.key for s in pop._bar._segments]


def test_a_usage_update_keeps_the_split_instead_of_one_orange_band():
    """Usage lands once per API request, the measured split once per turn.
    Each usage update used to replace the split with one band in the "tools"
    colour, so hovering during a turn flickered between the two."""
    pop = _popover()
    pop.set_measured_breakdown(PAYLOAD)
    keys = _bar_keys(pop)
    pop.set_usage(150_000, 350_000)
    assert _bar_keys(pop) == keys
    usage = [s for s in pop._bar._segments if s.key != context_breakdown.AUTOCOMPACT]
    assert abs(sum(s.tokens for s in usage) - 150_000) <= len(usage)
    reserve = [s for s in pop._bar._segments if s.key == context_breakdown.AUTOCOMPACT]
    assert [s.tokens for s in reserve] == [33000]
    assert pop._max_label.get_label() == "150.0k / 350k · 42%"


def test_before_any_split_the_single_band_is_neutral_not_tool_coloured():
    pop = _popover()
    assert _bar_keys(pop) == ["unsplit"]
    assert ct._segment_rgb("unsplit", False) != ct._segment_rgb("tools", False)


def test_an_empty_estimate_never_clears_a_measured_split():
    toolbar = ct.ChatToolbar()
    try:
        toolbar.set_context_model("claude-opus-5-5")
        toolbar.set_context_usage(127183, 350000)
        toolbar.set_measured_breakdown(PAYLOAD)
        toolbar.set_context_breakdown(None)
        assert [k for k, _t in toolbar._context_meter._segments] == MEASURED
        assert toolbar._context_popover.ring_segments()
    finally:
        toolbar.shutdown()


def test_the_same_window_again_does_not_rebuild_the_legend_rows():
    pop = _popover()
    pop.set_measured_breakdown(PAYLOAD)
    first = pop._legend.get_first_child()
    pop.set_window(350000)
    assert pop._legend.get_first_child() is first
