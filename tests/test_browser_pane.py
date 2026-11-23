"""The pane answers agent tool calls with real WebKit (display lane only)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402


def _pane(tmp_path, monkeypatch):
    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    import tempfile

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "scratch").mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "scratch"))
    from helios.widgets.browser_pane import BrowserPane

    pane = BrowserPane(on_reveal=lambda: None, on_close=lambda: None, cwd=lambda: str(tmp_path / "proj"))
    if not pane.available:
        pytest.skip("WebKitGTK 6 is not installed")
    return pane


def _call(pane, tool, **args):
    replies = []
    pane.handle_call(tool, args, replies.append)
    deadline = time.monotonic() + 15
    while not replies and time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    assert len(replies) == 1, f"{tool} never answered"
    return replies[0]


def test_open_read_and_refuse(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "report.html").write_text("<title>Report</title><h1>Totals →</h1><p>42 rows</p>")
    pane = _pane(tmp_path, monkeypatch)

    opened = _call(pane, "browser_open", url="report.html")
    assert opened["ok"] and opened["status"] == "loaded" and opened["title"] == "Report"
    assert opened["url"].startswith("helios-file://")
    read = _call(pane, "browser_read", tab=opened["tab"])
    assert "Totals →" in read["text"] and "42 rows" in read["text"]  # UTF-8 without <meta charset>
    assert _call(pane, "browser_open", target="report.html")["error"].startswith('browser_open needs "url"')
    assert not _call(pane, "browser_open", url="javascript:alert(1)")["ok"]
    assert _call(pane, "browser_tabs")["tabs"][0]["tab"] == opened["tab"]
    assert not _call(pane, "browser_navigate", tab="99", action="reload")["ok"]
    assert not _call(pane, "browser_nope")["ok"]


def test_outside_the_project_asks_and_what_the_person_opened_is_redacted(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    private = tmp_path / "private.html"
    private.write_text("<title>Bank statement</title><p>balance 1,234</p>")
    pane = _pane(tmp_path, monkeypatch)
    asked = []

    def deny(scope, args, verb, proceed, reply):
        if not scope:
            return proceed()
        asked.append((scope, verb))
        reply({"ok": False, "error": f"The person declined access to {scope}."})

    monkeypatch.setattr(pane, "_consent", deny)
    refused = _call(pane, "browser_open", url=str(private))
    assert not refused["ok"] and asked == [(str(private), "open")]

    # The person opens it themselves; the agent may see that a tab exists,
    # not what is in it, until it asks.
    view = pane.open(private.as_uri())
    deadline = time.monotonic() + 15
    while view.is_loading() or not view.get_title():
        GLib.MainContext.default().iteration(False)
        assert time.monotonic() < deadline
    tab = _call(pane, "browser_tabs")["tabs"][0]
    assert tab["title"] == "(not shared)" and "private" not in tab["url"]
    assert not _call(pane, "browser_read", tab=tab["tab"])["ok"]
    assert asked[-1] == (str(private), "read")


def test_project_files_cannot_reach_outside_through_symlinks_or_dotdot(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    secret = tmp_path / "secret.html"
    secret.write_text("<title>Secret</title><p>SECRET-OUTSIDE</p>")
    (tmp_path / "proj" / "link.html").symlink_to(secret)
    (tmp_path / "proj" / "index.html").write_text(
        '<title>Index</title><p>inside</p><iframe src="../secret.html"></iframe>'
        f'<iframe src="{secret.as_uri()}"></iframe>')
    pane = _pane(tmp_path, monkeypatch)
    asked = []
    def deny(scope, args, verb, proceed, reply):
        if not scope:
            return proceed()
        asked.append(scope)
        reply({"ok": False, "error": "declined"})

    monkeypatch.setattr(pane, "_consent", deny)

    # resolve() follows the link to the real file, which is outside: it asks.
    assert not _call(pane, "browser_open", url="link.html")["ok"]
    assert asked == [str(secret)]
    index = _call(pane, "browser_open", url="index.html")
    assert index["ok"] and index["title"] == "Index", index
    read = _call(pane, "browser_read", tab=index["tab"])
    assert "inside" in read["text"] and "SECRET" not in read["text"]


def test_browser_tabs_and_sidebar_visibility_follow_their_chat(tmp_path, request):
    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    from helios.widgets.browser_pane import SessionBrowser

    project = tmp_path / "proj"
    project.mkdir()
    browser = SessionBrowser(
        on_shown=lambda shown: browser.set_shown(shown),
        attach=lambda _path: None,
        cwd=lambda: str(project),
    )
    if not browser.available:
        pytest.skip("WebKitGTK 6 is not installed")
    window = Gtk.Window()
    window.set_default_size(800, 600)
    window.set_child(browser)
    request.addfinalizer(window.destroy)
    browser.set_shown(False)
    window.present()
    for name in ("alpha", "beta", "background"):
        (project / f"{name}.html").write_text(f"<title>{name}</title><p>{name}</p>")

    def for_chat(scope):
        return SimpleNamespace(handle_call=lambda tool, args, reply: browser.handle_call(
            scope, tool, {**args, "_cwd": str(project)}, reply,
        ))

    scope_a = browser.scope
    a = for_chat(scope_a)
    alpha = _call(a, "browser_open", url="alpha.html")
    assert alpha["ok"] and browser.get_visible()
    browser.bind_session("native-a", scope_a)
    assert browser.scope is scope_a

    browser.select_session("native-b")
    assert not browser.get_visible()
    b = for_chat(browser.scope)
    assert _call(b, "browser_tabs") == {"ok": True, "tabs": []}
    beta = _call(b, "browser_open", url="beta.html")
    assert beta["ok"] and beta["tab"] != alpha["tab"]
    assert not _call(b, "browser_read", tab=alpha["tab"])["ok"]
    assert not _call(a, "browser_screenshot", tab=alpha["tab"])["ok"]
    assert not _call(a, "browser_open", url="https://example.invalid/")["ok"]

    current_pane = browser.get_visible_child()
    assert _call(a, "browser_open", url="background.html")["ok"]
    assert browser.get_visible_child() is current_pane
    assert [tab["title"] for tab in _call(b, "browser_tabs")["tabs"]] == ["beta"]
    browser.set_shown(False)
    browser.select_session("native-a")
    assert browser.scope is scope_a and browser.get_visible()
    tabs = _call(a, "browser_tabs")["tabs"]
    assert [tab["title"] for tab in tabs] == ["alpha", "background"]
    assert [tab["title"] for tab in tabs if tab["selected"]] == ["background"]

    browser.select_session("native-b")
    assert not browser.get_visible()
    browser.select_session("native-a", origin="remote")
    assert not browser.get_visible() and browser.scope is not scope_a


def test_browser_tabs_survive_mid_session_model_switch_and_cwd_updates(tmp_path, request):
    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    from helios.widgets.browser_pane import SessionBrowser

    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_bg = tmp_path / "proj_bg"
    for p in (proj_a, proj_b, proj_bg):
        p.mkdir()
    (proj_a / "leg1.html").write_text("<title>Leg 1</title><p>first</p>")
    (proj_b / "leg2.html").write_text("<title>Leg 2</title><p>second</p>")
    (proj_bg / "other.html").write_text("<title>Other</title><p>bg</p>")

    current_cwd = [str(proj_a)]
    browser = SessionBrowser(
        on_shown=lambda shown: browser.set_shown(shown),
        attach=lambda _path: None,
        cwd=lambda: current_cwd[0],
    )
    if not browser.available:
        pytest.skip("WebKitGTK 6 is not installed")
    window = Gtk.Window()
    window.set_default_size(800, 600)
    window.set_child(browser)
    request.addfinalizer(window.destroy)
    browser.set_shown(False)
    window.present()

    def for_chat(scope, cwd):
        return SimpleNamespace(handle_call=lambda tool, args, reply: browser.handle_call(
            scope, tool, {**args, "_cwd": str(cwd)}, reply,
        ))

    # Open preview on a fresh chat, then switch models before/after first send.
    scope_a = browser.scope
    a = for_chat(scope_a, proj_a)
    first = _call(a, "browser_open", url="leg1.html")
    assert first["ok"] and browser.get_visible()

    browser.bind_work("wrk-1")
    assert browser.matches_target("", work_id="wrk-1")
    browser.bind_session("native-a1", scope_a, work_id="wrk-1")

    # Mid-session model switch stages an unbound second leg under the same Work.
    browser.select_session("", work_id="wrk-1")
    assert browser.scope is scope_a and browser.get_visible()
    assert browser.matches_target("", work_id="wrk-1")

    # Background scope lookup for another conversation must not disturb Chat A.
    scope_bg = browser.scope_for("native-bg", work_id="wrk-bg")
    assert scope_bg is not scope_a and browser.scope is scope_a and browser.get_visible()
    bg = for_chat(scope_bg, proj_bg)
    assert _call(bg, "browser_open", url="other.html")["ok"]
    assert browser.scope is scope_a

    # Second leg binds a new native session id under the same Work.
    browser.bind_session("native-a2", scope_a, work_id="wrk-1")
    current_cwd[0] = str(proj_b)
    pane_a = browser._panes[scope_a]
    assert pane_a._cwd() == str(proj_b)
    assert browser._panes[scope_bg]._cwd() == str(proj_bg)

    # Switching away hides Chat A's pane; returning via either leg restores it.
    browser.select_session("native-other", work_id="wrk-other")
    assert not browser.get_visible()
    browser.select_session("native-a2", work_id="wrk-1")
    assert browser.scope is scope_a and browser.get_visible()
    assert [tab["title"] for tab in _call(a, "browser_tabs")["tabs"]] == ["Leg 1"]


def test_main_window_browser_target_identity_and_work_binding(tmp_path, request):
    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    from helios.backend.projects import Project
    from helios.backend.session_router import ChatTarget
    from helios.main_window import MainWindow
    from helios.widgets.browser_pane import SessionBrowser

    project = tmp_path / "proj"
    project.mkdir()
    (project / "preview.html").write_text("<title>Preview</title><p>ok</p>")

    browser = SessionBrowser(
        on_shown=lambda shown: browser.set_shown(shown),
        attach=lambda _path: None,
        cwd=lambda: str(project),
    )
    if not browser.available:
        pytest.skip("WebKitGTK 6 is not installed")
    window = Gtk.Window()
    window.set_default_size(800, 600)
    window.set_child(browser)
    request.addfinalizer(window.destroy)
    browser.set_shown(False)
    window.present()

    fake_win = SimpleNamespace(_browser=browser)
    local_proj = Project(dirname="-proj", cwd=str(project), path=project)
    remote_proj = Project(
        dirname="-proj", cwd=str(project), path=project, read_only=True, origin="remote-host",
    )

    # Fresh chat opens a preview, then acquires a Work ID before sending.
    scope_a = browser.scope
    a = SimpleNamespace(handle_call=lambda tool, args, reply: browser.handle_call(
        scope_a, tool, {**args, "_cwd": str(project)}, reply,
    ))
    assert _call(a, "browser_open", url="preview.html")["ok"] and browser.get_visible()

    MainWindow._bind_browser_work(fake_win, "wrk-42")
    MainWindow._select_browser_session(
        fake_win, ChatTarget(project=local_proj, resume_id="", work_id="wrk-42"),
    )
    assert browser.scope is scope_a and browser.get_visible()

    # Switching to another conversation hides the preview; returning via new leg restores it.
    MainWindow._select_browser_session(
        fake_win, ChatTarget(project=local_proj, resume_id="other-sid", work_id="wrk-99"),
    )
    assert not browser.get_visible()
    MainWindow._select_browser_session(
        fake_win, ChatTarget(project=local_proj, resume_id="leg2-sid", work_id="wrk-42"),
    )
    assert browser.scope is scope_a and browser.get_visible()

    # A read-only remote session with the same session id never shares the local scope.
    MainWindow._select_browser_session(
        fake_win, ChatTarget(project=remote_proj, resume_id="leg2-sid", work_id="wrk-42"),
    )
    assert not browser.get_visible() and browser.scope is not scope_a


class _Request:
    def __init__(self, uri, view_uri):
        from urllib.parse import urlsplit

        self._uri, self.view_uri, self.result = uri, view_uri, None
        self._path = urlsplit(uri).path

    def get_uri(self):
        return self._uri

    def get_path(self):
        return self._path

    def get_web_view(self):
        return type("View", (), {"get_uri": lambda _self: self.view_uri})()

    def finish(self, stream, length, mime):
        self.result = ("ok", stream.read_bytes(length, None).get_data(), mime)

    def finish_error(self, error):
        self.result = ("error", error.message)


def test_a_project_root_is_served_only_to_its_own_tab_and_only_inside(tmp_path):
    from helios.backend.browser_target import localize
    from helios.widgets.browser_pane import _serve_project_file

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "notes.log").write_text("plain text")
    (tmp_path / "secret").write_text("SECRET")
    (tmp_path / "a" / "out").symlink_to(tmp_path / "secret")
    page = localize((tmp_path / "a" / "notes.log").as_uri(), str(tmp_path / "a"))
    key = page.split("/")[2]

    ok = _Request(page, f"helios-file://{key}/index.html")
    _serve_project_file(ok)
    assert ok.result == ("ok", b"plain text", "text/plain; charset=utf-8")
    for uri, view_uri in [
        (page, "helios-file://someotherkey/frame.html"),       # another chat's tab
        (f"helios-file://{key}/out", f"helios-file://{key}/"),  # symlink leaving the root
        (f"helios-file://{key}/%00x", f"helios-file://{key}/"),  # NUL: ValueError, not OSError
        (f"helios-file://{key}/", f"helios-file://{key}/"),     # a directory
    ]:
        request = _Request(uri, view_uri)
        _serve_project_file(request)
        assert request.result[0] == "error", uri


def test_text_without_a_known_type_is_shown_as_text():
    from helios.widgets.browser_pane import _mime

    assert _mime("/p/build.log", b"line 1\n") == "text/plain; charset=utf-8"
    assert _mime("/p/main.rs", "fn main() {} // é".encode()) == "text/plain; charset=utf-8"
    assert _mime("/p/report.html", b"<h1>") == "text/html"
    assert _mime("/p/blob.bin", b"\x00\x01binary") == "application/octet-stream"
