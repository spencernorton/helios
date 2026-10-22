"""An integrated terminal: tabs of the person's own shell in the chat's folder.

Not an agent tool — agents run commands through their own CLIs. This is for
the person: run the dev server, poke at the repo, and hand output to the chat
with *Send selection to chat*. Shells start only when the pane is first shown.
"""

from __future__ import annotations

import os
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk, Pango  # noqa: E402

from helios.backend import run_configs  # noqa: E402


def _load_vte():
    try:
        gi.require_version("Vte", "3.91")
        from gi.repository import Vte
    except (ImportError, ValueError):
        return None
    return Vte


class TerminalPane(Gtk.Box):
    __gsignals__ = {
        # Selected terminal text, for the composer.
        "send-to-chat": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # A run config's server is answering on its port ("localhost:PORT").
        "preview": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, *, cwd: Callable[[], str]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("helios-terminal-pane")
        self._cwd = cwd
        self._vte = _load_vte()
        self.available = self._vte is not None
        if not self.available:
            page = Adw.StatusPage(
                icon_name="utilities-terminal-symbolic",
                title="Terminal unavailable",
                description="Install gir1.2-vte-3.91 and restart Helios.",
            )
            page.set_vexpand(True)
            self.append(page)
            return

        bar = Gtk.Box(spacing=4)
        bar.add_css_class("toolbar")
        self._title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.START)
        self._title.add_css_class("caption")
        self._title.add_css_class("dim-label")
        bar.append(self._title)
        self._run_menu = Gtk.MenuButton(label="Run", tooltip_text="Start this project's dev server")
        self._run_menu.add_css_class("flat")
        self._run_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        popover = Gtk.Popover(child=self._run_box)
        popover.connect("show", lambda *_: self._fill_run_menu())
        self._run_menu.set_popover(popover)
        bar.append(self._run_menu)
        send = Gtk.Button(label="Send to chat")
        send.add_css_class("flat")
        send.set_tooltip_text("Put the selected terminal text in the message box")
        send.connect("clicked", lambda *_: self._send_selection())
        bar.append(send)
        new = Gtk.Button.new_from_icon_name("tab-new-symbolic")
        new.add_css_class("flat")
        new.set_tooltip_text("New terminal in this chat's folder")
        new.connect("clicked", lambda *_: self.new_tab())
        bar.append(new)
        self.append(bar)

        self._tabs = Adw.TabView(vexpand=True)
        self._tabs.connect("notify::selected-page", lambda *_: self._sync_title())
        self.append(Adw.TabBar(view=self._tabs, autohide=True))
        self.append(self._tabs)
        # First shell when the pane is first shown, not at startup.
        self.connect("map", lambda *_: self._tabs.get_n_pages() or self.new_tab())

    # ── public ──────────────────────────────────────────────────────────

    def new_tab(self, command: str = "", title: str = "", cwd: str = ""):
        """Open a shell in the chat's folder; type ``command`` into it if given.
        Returns the terminal, or None when VTE is unavailable."""
        if not self.available:
            return None
        cwd = cwd or self._cwd() or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        term = self._vte.Terminal()
        term.set_scrollback_lines(10000)
        # VTE asks for 80x24 by default, which pushed the workspace pane past
        # the window edge; it re-flows to whatever width it is given.
        term.set_size(40, 10)
        term.set_vexpand(True)
        term.set_hexpand(True)
        term._helios_cwd = cwd  # for the title
        shell = os.environ.get("SHELL") or "/bin/bash"

        def spawned(_term, pid, error, *_args) -> None:
            if error is None and command:
                term.feed_child((command + "\n").encode())

        # (pty, cwd, argv, envv, flags, child_setup, child_setup_data, timeout,
        # cancellable, callback): the introspected docstring omits setup data,
        # but PyGObject rejects the call without it (measured, VTE 0.84).
        term.spawn_async(
            self._vte.PtyFlags.DEFAULT, cwd, [shell], [],
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, spawned,
        )
        term.connect("child-exited", lambda t, _status: self._close_tab(t))
        page = self._tabs.append(term)
        page.set_title(title or os.path.basename(cwd.rstrip("/")) or cwd)
        self._tabs.set_selected_page(page)
        term.grab_focus()
        self._sync_title()
        return term

    def run(self, config) -> None:
        """Start a run config in its own tab; once its port answers, ask the
        window to open it in the browser pane."""
        self._run_menu.popdown()
        self.new_tab(config.command, config.name, config.cwd)
        if not config.port:
            return
        tries = [0]

        def poll() -> bool:
            tries[0] += 1
            if run_configs.port_open(config.port):
                self.emit("preview", f"localhost:{config.port}")
                return False
            return tries[0] < 240  # ponytail: 2 minutes, then stop waiting

        GLib.timeout_add(500, poll)

    def _fill_run_menu(self) -> None:
        while (child := self._run_box.get_first_child()) is not None:
            self._run_box.remove(child)
        configs = run_configs.load(self._cwd())
        for config in configs:
            button = Gtk.Button(label=config.name + (f"  ·  :{config.port}" if config.port else ""))
            button.add_css_class("flat")
            button.set_tooltip_text(config.command)
            button.connect("clicked", lambda _b, c=config: self.run(c))
            self._run_box.append(button)
        if not configs:
            hint = Gtk.Label(
                label="No run configuration.\nAdd .claude/launch.json or a\npackage.json dev script.",
                justify=Gtk.Justification.CENTER,
            )
            hint.add_css_class("dim-label")
            self._run_box.append(hint)

    # ── internals ───────────────────────────────────────────────────────

    def _selected_terminal(self):
        page = self._tabs.get_selected_page()
        return page.get_child() if page is not None else None

    def _sync_title(self) -> None:
        term = self._selected_terminal()
        self._title.set_label(getattr(term, "_helios_cwd", "") if term is not None else "")

    def _close_tab(self, term) -> None:
        page = self._tabs.get_page(term)
        if page is not None:
            self._tabs.close_page(page)

    def _send_selection(self) -> None:
        term = self._selected_terminal()
        if term is None or not term.get_has_selection():
            return
        text = term.get_text_selected(self._vte.Format.TEXT)
        if text and text.strip():
            self.emit("send-to-chat", text.rstrip())
