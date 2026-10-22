"""Review actions against a real repository (roadmap Phase 2a)."""
from __future__ import annotations

import subprocess

import pytest

from helios.backend import git_review
from helios.backend.git_changes import ChangedFile, ChangesError, read_changes

ORIGINAL = "".join(f"line {i}\n" for i in range(1, 41))


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.txt").write_text(ORIGINAL)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    edited = ORIGINAL.replace("line 3\n", "line three\n").replace("line 37\n", "line thirty-seven\n")
    (tmp_path / "a.txt").write_text(edited)
    return tmp_path


def _change(repo):
    return next(f for f in read_changes(str(repo)).files if f.path == "a.txt")


def test_two_distant_edits_are_two_hunks_staged_independently(repo):
    hunks = git_review.file_hunks(str(repo), _change(repo))
    assert [h.staged for h in hunks] == [False, False]
    git_review.stage_hunk(str(repo), hunks[0])
    assert "line three" in _git(repo, "diff", "--cached")
    assert "thirty-seven" not in _git(repo, "diff", "--cached")
    staged = [h for h in git_review.file_hunks(str(repo), _change(repo)) if h.staged]
    git_review.unstage_hunk(str(repo), staged[0])
    assert _git(repo, "diff", "--cached") == ""


def test_discard_reverts_only_that_hunk(repo):
    hunks = git_review.file_hunks(str(repo), _change(repo))
    git_review.discard_hunk(str(repo), hunks[1])
    text = (repo / "a.txt").read_text()
    assert "line three" in text and "line 37\n" in text


def test_discard_refuses_staged_hunks_and_untracked_files(repo):
    git_review.stage_file(str(repo), _change(repo))
    staged = git_review.file_hunks(str(repo), _change(repo))[0]
    with pytest.raises(ChangesError):
        git_review.discard_hunk(str(repo), staged)
    with pytest.raises(ChangesError):
        git_review.discard_file(str(repo), ChangedFile("new.txt", "??"))


def test_stage_commit_and_messages(repo):
    with pytest.raises(ChangesError):
        git_review.commit(str(repo), "  ")
    git_review.stage_file(str(repo), _change(repo))
    summary = git_review.commit(str(repo), "Rename two lines")
    assert summary.endswith("Rename two lines")
    assert read_changes(str(repo)).files == ()


def test_unsafe_paths_are_refused(repo):
    with pytest.raises(ChangesError):
        git_review.stage_file(str(repo), ChangedFile("../outside", " M"))


def test_push_without_a_branch_is_explained(repo):
    _git(repo, "checkout", "-q", "--detach")
    with pytest.raises(ChangesError, match="Detached HEAD"):
        git_review.push(str(repo))
