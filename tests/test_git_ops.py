"""Tests for core.orchestrator.git_ops."""

from __future__ import annotations

import shutil
from pathlib import Path

import git
import pytest

from core.orchestrator.git_ops import (
    commit_all,
    create_branch,
    create_worktree,
    current_commit,
    diff_since,
    ensure_clean_worktree,
    format_changed_files,
    merge_worktree,
    prune_worktrees,
    remove_worktree,
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

    assert name == "engine/add-oauth2-authentication"
    assert repo.active_branch.name == name


def test_create_branch_avoids_name_collision(repo: git.Repo) -> None:
    base = repo.active_branch.name

    first = create_branch(repo, "Add feature")
    repo.git.checkout(base)
    second = create_branch(repo, "Add feature")

    assert first == "engine/add-feature"
    assert second == "engine/add-feature-2"


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


def test_create_worktree_checks_out_a_new_branch_from_base(repo: git.Repo) -> None:
    base_branch = create_branch(repo, "Add feature")

    worktree_path, task_branch = create_worktree(repo, base_branch, "backend")

    assert task_branch == "engine-task/add-feature-backend"
    assert worktree_path.is_dir()
    worktree_repo = git.Repo(worktree_path)
    assert worktree_repo.active_branch.name == task_branch
    assert worktree_repo.head.commit.hexsha == repo.head.commit.hexsha


def test_create_worktree_sees_files_committed_on_the_base_branch(repo: git.Repo) -> None:
    base_branch = create_branch(repo, "Add feature")
    Path(repo.working_tree_dir, "architecture.md").write_text("plan\n", encoding="utf-8")
    commit_all(repo, "architecture done")

    worktree_path, _ = create_worktree(repo, base_branch, "backend")

    assert (worktree_path / "architecture.md").is_file()


def test_merge_worktree_merges_changes_back(repo: git.Repo) -> None:
    base_branch = create_branch(repo, "Add feature")
    worktree_path, task_branch = create_worktree(repo, base_branch, "backend")
    Path(worktree_path, "backend.py").write_text("x = 1\n", encoding="utf-8")
    commit_all(git.Repo(worktree_path), "backend done")

    merged = merge_worktree(repo, task_branch)

    assert merged is True
    assert Path(repo.working_tree_dir, "backend.py").is_file()


def test_merge_worktree_returns_false_on_conflict_and_aborts_cleanly(repo: git.Repo) -> None:
    base_branch = create_branch(repo, "Add feature")
    worktree_path, task_branch = create_worktree(repo, base_branch, "backend")

    # main and the worktree branch each change the same file differently
    Path(repo.working_tree_dir, "README.md").write_text("main change\n", encoding="utf-8")
    commit_all(repo, "main changes readme")
    Path(worktree_path, "README.md").write_text("worktree change\n", encoding="utf-8")
    commit_all(git.Repo(worktree_path), "worktree changes readme")

    merged = merge_worktree(repo, task_branch)

    assert merged is False
    assert not repo.is_dirty(untracked_files=True)  # `merge --abort` left it clean


def test_remove_worktree_removes_the_directory(repo: git.Repo) -> None:
    base_branch = create_branch(repo, "Add feature")
    worktree_path, _ = create_worktree(repo, base_branch, "backend")

    remove_worktree(repo, worktree_path)

    assert not worktree_path.exists()


def test_prune_worktrees_cleans_up_stale_registration(repo: git.Repo) -> None:
    base_branch = create_branch(repo, "Add feature")
    worktree_path, _ = create_worktree(repo, base_branch, "backend")
    shutil.rmtree(worktree_path)  # simulate a crashed run that left the directory gone

    prune_worktrees(repo)

    listing = repo.git.worktree("list")
    assert str(worktree_path) not in listing


def test_create_worktree_does_not_reuse_a_leftover_task_branch(repo: git.Repo) -> None:
    """A stage that failed in an earlier run leaves its branch behind on
    purpose, so its partial work stays inspectable (see remove_worktree).
    Deriving the next run's branch name without checking would then die here —
    before reaching the provider, with nothing recorded to explain why.
    Observed in a real run."""
    branch = create_branch(repo, "add a thing")
    first_path, first_branch = create_worktree(repo, branch, "documentation")
    remove_worktree(repo, first_path)  # worktree gone, branch deliberately kept

    second_path, second_branch = create_worktree(repo, branch, "documentation")

    assert second_branch != first_branch
    assert first_branch in {h.name for h in repo.heads}  # earlier work still reachable
    remove_worktree(repo, second_path)


def test_task_branch_names_stay_readable_when_uniquified(repo: git.Repo) -> None:
    branch = create_branch(repo, "add a thing")
    path_one, _ = create_worktree(repo, branch, "tests")
    remove_worktree(repo, path_one)

    path_two, second = create_worktree(repo, branch, "tests")

    assert second.startswith("engine-task/add-a-thing-tests")
    remove_worktree(repo, path_two)
