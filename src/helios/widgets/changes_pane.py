"""Review the worktree: per-hunk stage/unstage/discard, comments for the agent,
commit, push and PR. Reads are bounded background Git calls; every write is an
explicit click, run off the main loop, then the pane refreshes."""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GtkSource", "5")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, GtkSource, Pango

from helios.backend import git_review
from helios.backend.git_changes import (
    ChangedFile,
    ChangesError,
    ChangesSnapshot,
    display_text,
    read_changes,
    read_diff,
)
from helios.backend.latest_worker import LatestTaskRunner


def _load_file(root: str, change: ChangedFile):
    """Hunks for a tracked file; the text preview for an untracked one."""
    if change.status == "??":
        return read_diff(root, change)
    return git_review.file_hunks(root, change)


class ChangesPane(Gtk.Box):
    __gsignals__ = {
        # Review comments, formatted as one message for the current session.
        "feedback": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("helios-changes-pane")
        for edge in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{edge}")(12)
        self._closed = False
        self._cwd = ""
        self._revision = 0
        self._diff_revision = 0
        self._snapshot: ChangesSnapshot | None = None
        self._selected_path = ""
        self._selected: ChangedFile | None = None
        #: (path, hunk title) -> comment text, kept across refreshes.
        self._comments: dict[tuple[str, str], str] = {}
        self._busy = False
        self._buffers: list[GtkSource.Buffer] = []
        # Pin the callbacks; only delivery touches GLib, never GTK off-thread.
        self._status_ready = self._apply_status
        self._diff_ready = self._apply_diff
        self._status_runner = LatestTaskRunner(
            work=lambda request: read_changes(request[1]),
            deliver=lambda request, result: GLib.idle_add(self._status_ready, request, result),
            name="helios-changes-status",
        )
        self._diff_runner = LatestTaskRunner(
            work=lambda request: _load_file(request[2], request[3]),
            deliver=lambda request, result: GLib.idle_add(self._diff_ready, request, result),
            name="helios-changes-diff",
        )
        self._style_manager = Adw.StyleManager.get_default()
        self._theme_handler = self._style_manager.connect("notify::dark", self._apply_theme)

        header = Gtk.Box(spacing=8)
        title = Gtk.Label(label="Review changes", xalign=0, hexpand=True)
        title.add_css_class("heading")
        header.append(title)
        self._refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._refresh.set_tooltip_text("Refresh repository changes")
        self._refresh.connect("clicked", lambda *_: self.refresh())
        header.append(self._refresh)
        self.append(header)
        scope = Gtk.Label(
            label="Current staged, unstaged and untracked files from all sessions.",
            xalign=0, wrap=True,
        )
        scope.add_css_class("caption")
        scope.add_css_class("dim-label")
        self.append(scope)
        self._location = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self._location.add_css_class("caption")
        self.append(self._location)
        self._status = Gtk.Label(xalign=0, wrap=True, label="Select a local project to inspect its changes.")
        self._status.add_css_class("dim-label")
        self.append(self._status)

        self._files = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._files.add_css_class("boxed-list")
        self._files.connect("row-selected", self._on_file_selected)
        file_scroll = Gtk.ScrolledWindow()
        file_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        file_scroll.set_min_content_height(100)
        file_scroll.set_max_content_height(190)
        file_scroll.set_propagate_natural_height(True)
        file_scroll.set_child(self._files)
        self._file_scroll = file_scroll
        self.append(file_scroll)

        file_bar = Gtk.Box(spacing=6)
        self._file_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self._file_title.add_css_class("heading")
        file_bar.append(self._file_title)
        self._file_actions = Gtk.Box(spacing=4)
        file_bar.append(self._file_actions)
        self._file_bar = file_bar
        self.append(file_bar)

        self._hunks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._diff_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._diff_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._diff_scroll.set_child(self._hunks)
        self.append(self._diff_scroll)

        # Review comments → one message to the agent.
        self._feedback_btn = Gtk.Button(label="Send comments to agent")
        self._feedback_btn.add_css_class("suggested-action")
        self._feedback_btn.connect("clicked", self._on_send_feedback)
        self.append(self._feedback_btn)

        commit_box = Gtk.Box(spacing=6)
        self._message = Gtk.Entry(hexpand=True, placeholder_text="Commit message for staged changes")
        self._message.connect("activate", lambda *_: self._on_commit())
        commit_box.append(self._message)
        self._commit_btn = Gtk.Button(label="Commit")
        self._commit_btn.connect("clicked", lambda *_: self._on_commit())
        commit_box.append(self._commit_btn)
        self._push_btn = Gtk.Button.new_from_icon_name("go-up-symbolic")
        self._push_btn.set_tooltip_text("Push this branch")
        self._push_btn.connect("clicked", lambda *_: self._run(git_review.push, "Pushed {}"))
        commit_box.append(self._push_btn)
        self._pr_btn = Gtk.Button(label="Create PR")
        self._pr_btn.set_tooltip_text("Open a pull/merge request with gh or glab in a terminal")
        self._pr_btn.connect("clicked", self._on_create_pr)
        commit_box.append(self._pr_btn)
        self._commit_box = commit_box
        self.append(commit_box)

        self._refresh.set_sensitive(False)
        self._show_files(False)
        self._sync_feedback()

    @property
    def cwd(self) -> str:
        return self._cwd

    def _apply_theme(self, *_args) -> None:
        name = "Adwaita-dark" if self._style_manager.get_dark() else "Adwaita"
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(name)
        if scheme is not None:
            for buffer in self._buffers:
                buffer.set_style_scheme(scheme)

    def set_project(self, project) -> None:
        cwd = str(project.cwd) if project is not None and not project.read_only else ""
        if cwd == self._cwd:
            if cwd:
                self.refresh()
            return
        self._cwd = cwd
        self._revision += 1
        self._diff_revision += 1
        self._snapshot = None
        self._selected_path = ""
        self._selected = None
        self._comments.clear()
        self._sync_feedback()
        self._clear_rows()
        self._clear_hunks()
        self._location.set_label(display_text(cwd))
        self._location.set_tooltip_text(display_text(cwd))
        self._refresh.set_sensitive(bool(cwd))
        self._show_files(False)
        if cwd:
            self.refresh()
        else:
            self._status.set_label("Select a local project to inspect its changes.")

    def refresh(self) -> None:
        if self._closed or not self._cwd:
            return
        self._revision += 1
        self._diff_revision += 1
        self._status.set_label("Refreshing changes…")
        self._files.set_sensitive(False)
        self._status_runner.submit((self._revision, self._cwd))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._style_manager.disconnect(self._theme_handler)
        self._revision += 1
        self._diff_revision += 1
        self._status_runner.shutdown(join=False)
        self._diff_runner.shutdown(join=False)

    # ── layout helpers ─────────────────────────────────────────────────

    def _show_files(self, show: bool) -> None:
        self._file_scroll.set_visible(show)
        self._file_bar.set_visible(show)
        self._diff_scroll.set_visible(show)

    def _clear_rows(self) -> None:
        while (row := self._files.get_first_child()) is not None:
            self._files.remove(row)

    def _clear_hunks(self) -> None:
        while (child := self._hunks.get_first_child()) is not None:
            self._hunks.remove(child)
        self._buffers.clear()

    def _note(self, text: str) -> None:
        self._clear_hunks()
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.add_css_class("dim-label")
        self._hunks.append(label)

    def _code_view(self, text: str, language: str | None) -> Gtk.Widget:
        buffer = GtkSource.Buffer()
        manager = GtkSource.LanguageManager.get_default()
        lang = manager.get_language(language) if language == "diff" else manager.guess_language(language, None)
        buffer.set_language(lang)
        buffer.set_text(display_text(text))
        self._buffers.append(buffer)
        self._apply_theme()
        view = GtkSource.View.new_with_buffer(buffer)
        view.set_editable(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        for side in ("left", "right", "top", "bottom"):
            getattr(view, f"set_{side}_margin")(8)
        view.add_css_class("card")
        return view

    @staticmethod
    def _button(label: str, tooltip: str, callback, *css: str) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.set_tooltip_text(tooltip)
        button.add_css_class("flat")
        for name in css:
            button.add_css_class(name)
        button.connect("clicked", lambda *_: callback())
        return button

    # ── status and selection ───────────────────────────────────────────

    def _apply_status(self, request, result) -> bool:
        if self._closed or request != (self._revision, self._cwd):
            return False
        self._snapshot = None
        self._clear_rows()
        self._files.set_sensitive(True)
        if isinstance(result, Exception):
            message = display_text(str(result))
            if "not a git repository" in message.lower():
                message = "This folder is not a Git repository."
            self._status.set_label(message)
            self._show_files(False)
            self._commit_box.set_visible(False)
            return False
        self._snapshot = result
        self._commit_box.set_visible(True)
        self._location.set_label(display_text(result.root))
        self._location.set_tooltip_text(display_text(result.root))
        count = len(result.files)
        self._status.set_label(
            f"Showing first {count} files — list truncated." if result.truncated
            else (f"{count} changed file{'s' if count != 1 else ''}" if count else "Working tree is clean.")
        )
        self._show_files(bool(count))
        selected = None
        for change in result.files:
            row = Gtk.ListBoxRow()
            row.change = change
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            for edge in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{edge}")(8)
            box.append(Gtk.Label(label=change.label, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE))
            status = Gtk.Label(label=f"{change.status}  {change.description}", xalign=0)
            status.add_css_class("caption")
            status.add_css_class("dim-label")
            box.append(status)
            row.set_child(box)
            row.set_tooltip_text(change.label)
            self._files.append(row)
            if change.path == self._selected_path:
                selected = row
        self._files.select_row(selected or self._files.get_row_at_index(0))
        return False

    def _on_file_selected(self, _list, row) -> None:
        if row is None or self._snapshot is None:
            return
        self._diff_revision += 1
        change = row.change
        self._selected = change
        self._selected_path = change.path
        self._file_title.set_label(change.label)
        self._file_title.set_tooltip_text(change.label)
        self._sync_file_actions(change)
        self._note("Loading…")
        self._diff_runner.submit((self._revision, self._diff_revision, self._snapshot.root, change))

    def _sync_file_actions(self, change: ChangedFile) -> None:
        while (child := self._file_actions.get_first_child()) is not None:
            self._file_actions.remove(child)
        untracked = change.status == "??"
        if untracked or change.status[1] != " ":
            self._file_actions.append(self._button(
                "Stage", "Stage the whole file", lambda: self._run(git_review.stage_file, None, change)))
        if not untracked and change.status[0] not in (" ", "?"):
            self._file_actions.append(self._button(
                "Unstage", "Unstage the whole file", lambda: self._run(git_review.unstage_file, None, change)))
        if untracked or change.status[1] != " ":
            self._file_actions.append(self._button(
                "Discard…", "Throw away unstaged changes to this file",
                lambda: self._confirm_discard_file(change), "destructive-action"))

    def _apply_diff(self, request, result) -> bool:
        if self._closed or request[:2] != (self._revision, self._diff_revision):
            return False
        change = request[3]
        self._clear_hunks()
        if isinstance(result, Exception):
            self._note(display_text(str(result)))
        elif isinstance(result, str):  # untracked file preview
            self._hunks.append(self._code_view(result, display_text(change.path)))
        elif not result:
            self._note("No text diff remains. Refresh to update the file list.")
        else:
            for hunk in result:
                self._hunks.append(self._hunk_card(change, hunk))
        self._diff_scroll.get_vadjustment().set_value(0)
        return False

    def _hunk_card(self, change: ChangedFile, hunk: git_review.Hunk) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar = Gtk.Box(spacing=4)
        badge = Gtk.Label(label="Staged" if hunk.staged else "Unstaged")
        badge.add_css_class("caption")
        badge.add_css_class("success" if hunk.staged else "dim-label")
        bar.append(badge)
        title = Gtk.Label(label=hunk.title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("caption")
        title.add_css_class("monospace")
        bar.append(title)
        if hunk.staged:
            bar.append(self._button("Unstage", "Move this hunk back to unstaged",
                                    lambda: self._run(git_review.unstage_hunk, None, hunk)))
        else:
            bar.append(self._button("Stage", "Stage this hunk",
                                    lambda: self._run(git_review.stage_hunk, None, hunk)))
            bar.append(self._button("Discard", "Throw away this hunk's change",
                                    lambda: self._confirm(f"Discard this change to {change.label}?",
                                                          lambda: self._run(git_review.discard_hunk, None, hunk)),
                                    "destructive-action"))
        key = (change.path, hunk.title)
        comment_btn = Gtk.ToggleButton(icon_name="mail-message-new-symbolic")
        comment_btn.set_tooltip_text("Comment on this hunk for the agent")
        comment_btn.add_css_class("flat")
        bar.append(comment_btn)
        card.append(bar)
        card.append(self._code_view(hunk.body, "diff"))

        entry = Gtk.Entry(placeholder_text="What should change here?")
        entry.set_text(self._comments.get(key, ""))
        entry.set_visible(bool(entry.get_text()))
        comment_btn.set_active(entry.get_visible())
        comment_btn.connect("toggled", lambda btn: (entry.set_visible(btn.get_active()),
                                                    btn.get_active() and entry.grab_focus()))
        entry.connect("changed", lambda e: self._set_comment(key, e.get_text()))
        card.append(entry)
        return card

    # ── comments → agent ───────────────────────────────────────────────

    def _set_comment(self, key: tuple[str, str], text: str) -> None:
        if text.strip():
            self._comments[key] = text
        else:
            self._comments.pop(key, None)
        self._sync_feedback()

    def _sync_feedback(self) -> None:
        count = len(self._comments)
        self._feedback_btn.set_visible(bool(count))
        self._feedback_btn.set_label(f"Send {count} comment{'s' if count != 1 else ''} to agent")

    def feedback_message(self) -> str:
        lines = ["Review comments on the current changes — please address each:", ""]
        for (path, hunk_title), text in self._comments.items():
            lines.append(f"- `{path}` at `{hunk_title}`: {text.strip()}")
        return "\n".join(lines)

    def _on_send_feedback(self, *_) -> None:
        if not self._comments:
            return
        self.emit("feedback", self.feedback_message())
        self._comments.clear()
        self._sync_feedback()
        if self._selected is not None and self._snapshot is not None:
            self._on_file_selected(self._files, self._files.get_selected_row())

    # ── writes ─────────────────────────────────────────────────────────

    def _run(self, action, done: str | None, *args) -> None:
        """Run one write off the main loop, then report and refresh."""
        if self._busy or self._snapshot is None:
            return
        self._busy = True
        root = self._snapshot.root
        self._status.set_label("Working…")

        def work() -> None:
            try:
                result = action(root, *args)
                message = done.format(result) if done else ""
            except ChangesError as exc:
                message = display_text(str(exc))
            GLib.idle_add(self._finish_write, message)

        threading.Thread(target=work, daemon=True, name="helios-review-write").start()

    def _finish_write(self, message: str) -> bool:
        self._busy = False
        if self._closed:
            return False
        self.refresh()
        if message:
            # refresh() overwrites the status line; show the outcome after it lands.
            GLib.timeout_add(600, lambda: self._status.set_label(message) and False)
        return False

    def _on_commit(self) -> None:
        message = self._message.get_text()
        self._run(lambda root, text: git_review.commit(root, text), "Committed {}", message)
        if message.strip():
            self._message.set_text("")

    def _on_create_pr(self, *_) -> None:
        if self._snapshot is None:
            return
        from shlex import quote

        from helios.backend.claude_env import _launch_in_terminal

        try:
            command = git_review.pr_command(self._snapshot.root)
        except ChangesError as exc:
            self._status.set_label(display_text(str(exc)))
            return
        inner = f"cd {quote(self._snapshot.root)} && {command}; echo; read -rp 'Press Enter to close…'"
        ok, message = _launch_in_terminal(inner, "pull request")
        self._status.set_label(message)

    def _confirm(self, heading: str, on_yes) -> None:
        dialog = Adw.AlertDialog.new(heading, "This cannot be undone from Helios.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "discard" and on_yes())
        root = self.get_root()
        if root is not None:
            dialog.present(root)

    def _confirm_discard_file(self, change: ChangedFile) -> None:
        if change.status == "??":
            # Never delete: the trash keeps a mistake recoverable.
            def trash() -> None:
                if self._snapshot is None:
                    return
                try:
                    Gio.File.new_for_path(f"{self._snapshot.root}/{change.path}").trash(None)
                except GLib.Error as exc:
                    self._status.set_label(display_text(exc.message))
                self.refresh()

            self._confirm(f"Move {change.label} to the trash?", trash)
            return
        self._confirm(f"Discard unstaged changes to {change.label}?",
                      lambda: self._run(git_review.discard_file, None, change))
