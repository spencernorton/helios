"""Per-chat browser pane and D-Bus tool-broker mixin, split out of main_window.

Behaviour-preserving mixin: every method runs on the composed MainWindow
instance so widget handles and session-target state resolve identically while
giving the browser-pane surface clear module ownership.
"""

from __future__ import annotations

import json

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from helios.backend import browser_target, browser_tools
from helios.log import get_logger

_log = get_logger("window")


class WindowBrowserMixin:
    """Per-chat browser pane and session D-Bus tool broker for MainWindow."""

    def _open_in_pane(self, path: str) -> None:
        """Show a local file in the side pane: WebKit renders images (with
        click-to-zoom), PDFs (built-in PDF.js), HTML and text."""
        if not self._browser.available:
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self, None, None, None)
            return
        cwd = self._current_project_cwd() or ""
        try:
            url = browser_target.localize(browser_target.resolve(path, cwd), cwd)
        except browser_target.TargetError as exc:
            self._toast(str(exc))
            return
        self._browser_btn.set_active(True)
        self._browser.open(url)

    def _on_browser_toggle(self, button: Gtk.ToggleButton) -> None:
        shown = button.get_active()
        if shown and not self._browser.get_visible():
            # Half the content width on first use, then wherever it was left.
            width = self._browser_paned.get_width() or 1000
            saved = int(self._ui_state.get("browser_paned", 0) or 0)
            self._browser_paned.set_position(saved if 200 < saved < width - 200 else width // 2)
        self._browser.set_shown(shown)

    @staticmethod
    def _browser_target_identity(target) -> tuple[str, str, str]:
        if target is None:
            return "", "", ""
        project = target.project
        origin = ""
        if getattr(project, "read_only", False):
            origin = str(getattr(project, "origin", "") or "remote")
        work_id = "" if origin else str(getattr(target, "work_id", "") or "")
        return str(target.resume_id or ""), origin, work_id

    def _bind_browser_work(self, work_id: str) -> None:
        browser = getattr(self, "_browser", None)
        if browser is not None and work_id:
            browser.bind_work(work_id)

    def _select_browser_session(self, target = None) -> None:
        browser = getattr(self, "_browser", None)
        if browser is None:
            return
        session_id, origin, work_id = WindowBrowserMixin._browser_target_identity(target)
        browser.select_session(session_id, origin=origin, work_id=work_id)

    def _register_browser_bus(self) -> None:
        """Export the pane on the session bus and tell drivers how to reach it."""
        app = self.get_application()
        connection = app.get_dbus_connection() if app is not None else None
        self._browser_bus_id = 0
        if connection is None or not self._browser.available:
            return
        node = Gio.DBusNodeInfo.new_for_xml(browser_tools.INTERFACE_XML)
        # closures2 is GLib 2.84+; the older call is what 24.04 (2.80) has.
        register = (
            getattr(connection, "register_object_with_closures2", None)
            or connection.register_object
        )
        try:
            self._browser_bus_id = register(
                browser_tools.OBJECT_PATH,
                node.interfaces[0],
                self._on_browser_bus_call,
                None,
                None,
            )
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            _log.warning("browser pane is not reachable by agents: %s", exc.message)
            return
        self._browser_bus = connection
        browser_tools.publish(address)

    def _on_browser_bus_call(
        self, _conn, _sender, _path, _iface, _method, params, invocation
    ) -> None:
        replied: list[bool] = []

        def reply(result: dict) -> None:
            if not replied:
                replied.append(True)
                invocation.return_value(GLib.Variant("(s)", (json.dumps(result),)))

        tool, raw = params.unpack()
        try:
            args = json.loads(raw)
        except ValueError:
            args = None
        if not isinstance(args, dict):
            reply({"ok": False, "error": "Browser tool arguments must be a JSON object."})
        elif self._destroyed:
            reply({"ok": False, "error": "Helios is closing."})
        else:
            # Folder and agent come from the token Helios issued at spawn,
            # never from the payload; an unknown caller gets nothing.
            session = browser_tools.session_for(args.pop("_token", None))
            args = {k: v for k, v in args.items() if not str(k).startswith("_")}
            if session is None or session[2] is None:
                reply({"ok": False, "error": "Unknown Helios browser session."})
                return
            args["_cwd"], args["_agent"], scope = session
            self._browser.handle_call(scope, tool, args, reply)
