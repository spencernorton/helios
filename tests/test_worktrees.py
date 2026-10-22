"""Opt-in worktrees (roadmap Phase 3e)."""
from __future__ import annotations

import subprocess

import pytest

from helios.backend import worktrees
from helios.backend.git_changes import ChangesError


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    src = tmp_path / "proj"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "T")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "init")
    return src


def test_create_makes_a_branch_outside_the_repo_and_remove_keeps_it(repo, tmp_path):
    path = worktrees.create(str(repo))
    assert worktrees.is_helios_worktree(path)
    assert not path.startswith(str(repo))
    assert (tmp_path / "state" / "worktrees").as_posix() in path
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert branch.startswith("helios/proj-")

    (__import__("pathlib").Path(path) / "a.txt").write_text("changed\n")
    with pytest.raises(ChangesError, match="Commit or discard"):
        worktrees.remove(path)
    _git(path, "commit", "-qam", "work")
    assert worktrees.remove(path) == branch
    assert not __import__("os").path.exists(path)
    assert branch in _git(repo, "branch", "--list", branch)  # the work survives


def test_remove_refuses_folders_helios_did_not_create(repo):
    assert not worktrees.is_helios_worktree(str(repo))
    with pytest.raises(ChangesError, match="Only worktrees Helios created"):
        worktrees.remove(str(repo))
