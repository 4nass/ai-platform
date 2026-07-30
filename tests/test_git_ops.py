"""Tests for core.orchestrator.git_ops."""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.orchestrator.git_ops import (
    commit_all,
    create_branch,
    current_commit,
    diff_since,
    ensure_clean_worktree,
    format_changed_files,
)


@pytest.fixture
def repo(tmp_path: Path) -> git.Repo:
    r = git.Repo.init(tmp_path)
    with r.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    r.index.add(["README.md"])
    r.index.commit("initial commit")
    return r


def test_ensure_clean_worktree_passes_when_clean(repo: git.Repo) -> None:
    ensure_clean_worktree(repo)


def test_ensure_clean_worktree_raises_when_dirty(repo: git.Repo) -> None:
    Path(repo.working_tree_dir, "dirty.txt").write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError):
        ensure_clean_worktree(repo)


def test_current_commit_matches_head(repo: git.Repo) -> None:
    assert current_commit(repo) == repo.head.commit.hexsha


def test_create_branch_slugifies_the_request(repo: git.Repo) -> None:
    name = create_branch(repo, "Add OAuth2 authentication!")

    assert name == "hermes/add-oauth2-authentication"
    assert repo.active_branch.name == name


def test_create_branch_avoids_name_collision(repo: git.Repo) -> None:
    base = repo.active_branch.name

    first = create_branch(repo, "Add feature")
    repo.git.checkout(base)
    second = create_branch(repo, "Add feature")

    assert first == "hermes/add-feature"
    assert second == "hermes/add-feature-2"


def test_commit_all_with_no_changes_returns_empty(repo: git.Repo) -> None:
    assert commit_all(repo, "nothing to do") == []


def test_commit_all_commits_new_files(repo: git.Repo) -> None:
    Path(repo.working_tree_dir, "new.py").write_text("x = 1\n", encoding="utf-8")

    changed = commit_all(repo, "add new.py")

    assert changed == ["new.py"]
    assert not repo.is_dirty(untracked_files=True)


def test_diff_since_shows_this_runs_own_changes(repo: git.Repo) -> None:
    base = current_commit(repo)
    Path(repo.working_tree_dir, "new.py").write_text("x = 1\n", encoding="utf-8")
    commit_all(repo, "add new.py")

    diff = diff_since(repo, base)

    assert "new.py" in diff
    assert "x = 1" in diff


def test_format_changed_files_empty() -> None:
    assert format_changed_files([]) == "no files changed"


def test_format_changed_files_single() -> None:
    assert format_changed_files(["a.py"]) == "1 file changed: a.py"


def test_format_changed_files_multiple() -> None:
    assert format_changed_files(["a.py", "b.py"]) == "2 files changed: a.py, b.py"
