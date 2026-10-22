"""Review actions on a worktree: hunks, stage/unstage/discard, commit, push.

Reads go through ``git_changes`` (bounded, read-only). Everything here WRITES
to the repository and is only ever called for an explicit click. Call from a
worker thread.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from helios.backend.git_changes import ChangedFile, ChangesError, _safe_path

GIT_WRITE_TIMEOUT = 30.0
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


@dataclass(frozen=True)
class Hunk:
    """One ``@@`` hunk with the file header it needs to apply on its own."""

    header: str  # "diff --git …\n--- a/…\n+++ b/…\n"
    body: str  # "@@ … @@ …\n" plus its lines
    staged: bool

    @property
    def patch(self) -> str:
        return self.header + self.body

    @property
    def title(self) -> str:
        return self.body.split("\n", 1)[0]


def _env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


def git(root: str, *args: str, stdin: str | None = None, timeout: float = GIT_WRITE_TIMEOUT) -> str:
    """Run a git command that may write; raise ChangesError with git's message."""
    try:
        proc = subprocess.run(
            ["git", "--no-pager", "--literal-pathspecs", "-c", "color.ui=false", *args],
            cwd=root, env=_env(), input=stdin, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChangesError(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode:
        message = (proc.stderr or proc.stdout).strip()
        raise ChangesError(message[:600] or f"git {args[0]} failed.")
    return proc.stdout


def split_hunks(diff: str, *, staged: bool) -> list[Hunk]:
    """Split ``git diff`` output for ONE file into independently applicable hunks."""
    lines = diff.splitlines(keepends=True)
    header: list[str] = []
    hunks: list[Hunk] = []
    current: list[str] = []
    for line in lines:
        if _HUNK_HEADER.match(line):
            if current:
                hunks.append(Hunk("".join(header), "".join(current), staged))
            current = [line]
        elif current:
            current.append(line)
        else:
            header.append(line)
    if current:
        hunks.append(Hunk("".join(header), "".join(current), staged))
    return hunks


def file_hunks(root: str, change: ChangedFile) -> list[Hunk]:
    """Staged then unstaged hunks of a tracked file (untracked files have none)."""
    _check(change)
    if change.status == "??":
        return []
    paths = [change.path, *([change.original_path] if change.original_path else [])]
    hunks: list[Hunk] = []
    for staged in (True, False):
        diff = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                   *(["--cached"] if staged else []), "--", *paths)
        hunks.extend(split_hunks(diff, staged=staged))
    return hunks


def stage_hunk(root: str, hunk: Hunk) -> None:
    git(root, "apply", "--cached", "--whitespace=nowarn", "-", stdin=hunk.patch)


def unstage_hunk(root: str, hunk: Hunk) -> None:
    git(root, "apply", "--cached", "-R", "--whitespace=nowarn", "-", stdin=hunk.patch)


def discard_hunk(root: str, hunk: Hunk) -> None:
    """Revert an UNSTAGED hunk in the working tree."""
    if hunk.staged:
        raise ChangesError("Unstage the hunk before discarding it.")
    git(root, "apply", "-R", "--whitespace=nowarn", "-", stdin=hunk.patch)


def stage_file(root: str, change: ChangedFile) -> None:
    _check(change)
    git(root, "add", "-A", "--", change.path)


def unstage_file(root: str, change: ChangedFile) -> None:
    _check(change)
    git(root, "restore", "--staged", "--", change.path)


def discard_file(root: str, change: ChangedFile) -> None:
    """Drop unstaged edits of a tracked file. Untracked files are never deleted
    here: the caller moves them to the trash so a mistake is recoverable."""
    _check(change)
    if change.status == "??":
        raise ChangesError("Untracked files go to the trash instead.")
    git(root, "restore", "--worktree", "--", change.path)


def commit(root: str, message: str) -> str:
    if not message.strip():
        raise ChangesError("Write a commit message first.")
    git(root, "commit", "-m", message)
    return git(root, "log", "-1", "--format=%h %s").strip()


def push(root: str) -> str:
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD":
        raise ChangesError("Detached HEAD — check out a branch before pushing.")
    has_upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=root, env=_env(), capture_output=True, text=True, check=False,
    ).returncode == 0
    args = ["push"] if has_upstream else ["push", "-u", "origin", branch]
    git(root, *args, timeout=120)
    return branch


def pr_command(root: str) -> str:
    """Shell text that opens a PR/MR for the current branch with the forge CLI."""
    remote = git(root, "remote", "get-url", "origin").strip().lower()
    tool = "gh pr create --fill" if "github.com" in remote else "glab mr create --fill"
    return tool


def _check(change: ChangedFile) -> None:
    if not _safe_path(change.path) or (change.original_path and not _safe_path(change.original_path)):
        raise ChangesError("The selected path is not inside the repository.")
