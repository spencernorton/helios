"""Session board — every chat as a card, with a live preview beside it.

The sidebar answers "what do I have"; the board answers "what needs me".
Cards render the session list's own snapshot (same rows, same status
dots), attention first. Clicking a card previews its transcript in the
right pane; *Open chat* routes through the existing `app.focus-session`
action, exactly like a notification does.
"""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402
from helios.backend import model_catalog  # noqa: E402
from helios.widgets.transcript_view import TranscriptView  # noqa: E402

_STATE_LABELS = {
    "working": "Working",
    "ready": "Ready",
    "awaiting": "Needs you",
    "errored": "Errored",
    "idle": "Idle",
}


class BoardDialog(Adw.Dialog):
    """Cards + preview for `(session, state, provider, asked)` tuples."""

    __gsignals__ = {
        "session-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, cards: list[tuple]) -> None:
        super().__init__()
        self.set_title("Sessions")
        self.set_content_width(980)
        self.set_content_height(560)
        self._selected_id = ""

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        view.add_top_bar(header)
        self._view = view

        if not cards:
            view.set_content(
                Adw.StatusPage(
                    title="No sessions yet",
                    description="Chats you start will appear here as cards.",
                    icon_name="view-grid-symbolic",
                )
            )
            self.set_child(view)
            return

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_wide_handle(True)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(2)
        flow.set_row_spacing(10)
        flow.set_column_spacing(10)
        flow.set_margin_top(14)
        flow.set_margin_bottom(14)
        flow.set_margin_start(14)
        flow.set_margin_end(14)
        self._buttons: dict[str, Gtk.Button] = {}
        for session, state, provider, asked in cards:
            btn = self._card(session, state, provider, asked)
            self._buttons[session.session_id] = btn
            flow.append(btn)
        cards_scroll = Gtk.ScrolledWindow()
        cards_scroll.set_child(flow)
        cards_scroll.set_size_request(330, -1)
        paned.set_start_child(cards_scroll)

        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._preview = TranscriptView()
        self._preview.set_hexpand(True)
        self._preview.set_vexpand(True)
        preview_box.append(self._preview)
        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.set_margin_top(8)
        action_bar.set_margin_bottom(10)
        action_bar.set_margin_end(12)
        action_bar.set_halign(Gtk.Align.END)
        self._open_btn = Gtk.Button(label="Open chat")
        self._open_btn.add_css_class("suggested-action")
        self._open_btn.set_sensitive(False)
        self._open_btn.connect("clicked", self._on_open_clicked)
        action_bar.append(self._open_btn)
        preview_box.append(action_bar)
        paned.set_end_child(preview_box)

        view.set_content(paned)
        self.set_child(view)
        self.connect("closed", self._on_closed)

        first = cards[0][0]
        self._select(first)

    # ── cards ──

    def _card(self, session, state: str, provider: str, asked: bool) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("helios-board-card")
        btn.set_tooltip_text("Preview this chat")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        dot = Gtk.Box()
        dot.add_css_class("helios-status-dot")
        dot.add_css_class(
            "helios-status-question" if asked else f"helios-status-{state}"
        )
        dot.set_valign(Gtk.Align.CENTER)
        top.append(dot)
        label = "Asked you" if asked else _STATE_LABELS.get(state, state.title())
        state_label = Gtk.Label(label=label, xalign=0)
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
        btn.connect("clicked", lambda *_: self._select(session))
        return btn

    # ── selection / preview ──

    def _select(self, session) -> None:
        self._selected_id = session.session_id
        for sid, btn in self._buttons.items():
            if sid == self._selected_id:
                btn.add_css_class("helios-board-selected")
            else:
                btn.remove_css_class("helios-board-selected")
        self._preview.set_session(session)
        self._open_btn.set_sensitive(True)

    def _on_open_clicked(self, _btn) -> None:
        if self._selected_id:
            self.emit("session-chosen", self._selected_id)

    def _on_closed(self, _dialog) -> None:
        self._preview.shutdown()
