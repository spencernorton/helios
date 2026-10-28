"""Session board — every chat as a card, attention first.

The sidebar answers "what do I have"; the board answers "what needs me".
It renders the session list's own snapshot (same rows, same status dots),
so the two surfaces can never disagree. Choosing a card routes through
the existing `app.focus-session` action, exactly like a notification.
"""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402
from helios.backend import model_catalog  # noqa: E402

_STATE_LABELS = {
    "working": "Working",
    "ready": "Ready",
    "awaiting": "Needs you",
    "errored": "Errored",
    "idle": "Idle",
    "question": "Asked you",
}


class BoardDialog(Adw.Dialog):
    """Cards for `(session, state, provider)` triples from the sidebar."""

    __gsignals__ = {
        "session-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, cards: list[tuple]) -> None:
        super().__init__()
        self.set_title("Sessions")
        self.set_content_width(720)
        self.set_content_height(480)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        view.add_top_bar(header)

        if not cards:
            view.set_content(
                Adw.StatusPage(
                    title="No sessions yet",
                    description="Chats you start will appear here as cards.",
                    icon_name="view-grid-symbolic",
                )
            )
        else:
            flow = Gtk.FlowBox()
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_min_children_per_line(2)
            flow.set_max_children_per_line(3)
            flow.set_row_spacing(10)
            flow.set_column_spacing(10)
            flow.set_margin_top(14)
            flow.set_margin_bottom(14)
            flow.set_margin_start(14)
            flow.set_margin_end(14)
            for session, state, provider in cards:
                flow.append(self._card(session, state, provider))
            scroller = Gtk.ScrolledWindow()
            scroller.set_child(flow)
            view.set_content(scroller)
        self.set_child(view)

    def _card(self, session, state: str, provider: str) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("helios-board-card")
        btn.set_tooltip_text("Open this chat")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        dot = Gtk.Box()
        dot.add_css_class("helios-status-dot")
        dot.add_css_class(f"helios-status-{state}")
        dot.set_valign(Gtk.Align.CENTER)
        top.append(dot)
        state_label = Gtk.Label(label=_STATE_LABELS.get(state, state.title()), xalign=0)
        state_label.add_css_class("caption-heading")
        top.append(state_label)
        box.append(top)

        title = Gtk.Label(label=session.display_title, xalign=0)
        title.set_wrap(True)
        title.set_lines(2)
        title.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        title.add_css_class("helios-board-title")
        box.append(title)

        folder = os.path.basename(session.project.cwd.rstrip("/")) or session.project.cwd
        who = model_catalog.PROVIDER_LABELS.get(provider, "Provider unknown")
        caption = Gtk.Label(label=f"{who} · {folder}", xalign=0)
        caption.add_css_class("caption")
        caption.add_css_class("dim-label")
        caption.set_ellipsize(3)
        box.append(caption)

        btn.set_child(box)
        btn.connect("clicked", lambda *_: self.emit("session-chosen", session.session_id))
        return btn
