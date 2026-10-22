"""Opt-in git worktrees for a chat: isolation the person chooses.

Not the removed automatic redirect (v0.66.0, see AGENTS.md) — nothing here
runs unless "Chat in a Worktree" is picked. Each worktree gets its own
``helios/<name>`` branch from the current commit, under
``state_dir()/worktrees``, so it never nests inside the repository. Removing
one keeps its branch (it may hold commits or an open PR) and refuses while
anything is uncommitted. Call from a worker thread.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from helios import paths
from helios.backend.git_changes import ChangesError
from helios.backend.git_review import git


def root() -> Path:
    return paths.state_dir() / "worktrees"


def create(source_cwd: str) -> str:
    """A new worktree of ``source_cwd``'s repository at its HEAD; returns its path."""
    repo = git(source_cwd, "rev-parse", "--show-toplevel").strip()
    name = f"{os.path.basename(repo) or 'repo'}-{time.strftime('%Y%m%d-%H%M%S')}"
    target = root() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", f"helios/{name}", str(target), "HEAD", timeout=120)
    return str(target)


def is_helios_worktree(cwd: str) -> bool:
    try:
        Path(cwd).resolve().relative_to(root().resolve())
    except (ValueError, OSError):
        return False
    return True


def remove(cwd: str) -> str:
    """Remove a Helios worktree; returns the branch it leaves behind."""
    if not is_helios_worktree(cwd):
        raise ChangesError("Only worktrees Helios created can be removed here.")
    if git(cwd, "status", "--porcelain").strip():
        raise ChangesError("Commit or discard the changes in this worktree first.")
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    git(os.path.dirname(common.rstrip("/")), "worktree", "remove", cwd)
    return branch

