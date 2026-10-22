"""Background tasks the visible Claude chat is running between turns:
backgrounded shells, monitors and subagents (``background_tasks_changed``).
Each can be stopped, and its live output opened in the side pane."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

_KIND = {"local_bash": "Shell", "monitor": "Monitor", "local_agent": "Agent", "agent": "Agent"}


class BackgroundPane(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for edge in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{edge}")(12)
        title = Gtk.Label(label="Background tasks", xalign=0)
        title.add_css_class("heading")
        self.append(title)
        hint = Gtk.Label(
            label="Shells, monitors and agents a Claude chat left running between "
                  "turns. Helios keeps such a session alive until they finish.",
            xalign=0, wrap=True,
        )
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        self.append(hint)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._empty = Adw.StatusPage(
            icon_name="object-select-symbolic", title="Nothing running in the background",
        )
        self._empty.add_css_class("compact")
        self.append(self._empty)
        self.append(self._list)
        self._driver = None
        self.show_tasks(None, [])

    def show_tasks(self, driver, rows: list[dict]) -> None:
        """Render ``rows`` for ``driver`` (the visible chat's driver, or None)."""
        self._driver = driver
        while (child := self._list.get_first_child()) is not None:
            self._list.remove(child)
        for row in rows:
            self._list.append(self._row(row))
        self._list.set_visible(bool(rows))
        self._empty.set_visible(not rows)

    def _row(self, task: dict) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(GLib.markup_escape_text(task.get("description") or task["task_id"]))
        row.set_subtitle(_KIND.get(task.get("task_type", ""), task.get("task_type") or "Task"))
        row.set_title_lines(2)
        driver = self._driver
        output = driver.task_output_path(task["task_id"]) if driver is not None else ""
        if output and os.path.isfile(output):
            view = Gtk.Button(label="Output", valign=Gtk.Align.CENTER)
            view.add_css_class("flat")
            view.set_tooltip_text("Open this task's live output in the side pane")
            view.connect("clicked", lambda b: b.activate_action(
                "win.open-media", GLib.Variant.new_string(output)))
            row.add_suffix(view)
        stop = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        stop.add_css_class("flat")
        stop.add_css_class("destructive-action")
        stop.connect("clicked", lambda b: self._stop(b, task["task_id"]))
        row.add_suffix(stop)
        label = row.get_first_child()
        if isinstance(label, Gtk.Label):
            label.set_ellipsize(Pango.EllipsizeMode.END)
        return row

    def _stop(self, button: Gtk.Button, task_id: str) -> None:
        stop = getattr(self._driver, "stop_background_task", None)
        if callable(stop) and stop(task_id):
            button.set_sensitive(False)
            button.set_label("Stopping…")
