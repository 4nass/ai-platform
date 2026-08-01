"""Tests for core.orchestrator.git_ops."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import git
import pytest

from core.orchestrator.git_ops import (
    classify_ignored_writes,
    commit_all,
    create_integration_worktree,
    create_worktree,
    current_commit,
    diff_since,
    disable_hooks,
    exclusive_run_lock,
    format_changed_files,
    ignored_writes,
    merge_worktree,
    prune_worktrees,
    remove_worktree,
    uncommitted_changes,
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


def test_uncommitted_changes_false_when_clean(repo: git.Repo) -> None:
    assert uncommitted_changes(repo) is False


def test_uncommitted_changes_true_when_dirty(repo: git.Repo) -> None:
    """No longer fatal — the run works in its own integration worktree, so a
    dirty target tree is safe. It still gets warned about, since a worktree
    checkout only contains committed state."""
    Path(repo.working_tree_dir, "dirty.txt").write_text("x", encoding="utf-8")

    assert uncommitted_changes(repo) is True


def test_current_commit_matches_head(repo: git.Repo) -> None:
    assert current_commit(repo) == repo.head.commit.hexsha


def test_create_integration_worktree_slugifies_the_request(repo: git.Repo) -> None:
    before = repo.active_branch.name

    path, name = create_integration_worktree(repo, "Add OAuth2 authentication!")

    assert name == "engine/add-oauth2-authentication"
    assert git.Repo(path).active_branch.name == name
    # the caller's own checkout is left exactly where it was
    assert repo.active_branch.name == before
    remove_worktree(repo, path)


def test_create_integration_worktree_avoids_name_collision(repo: git.Repo) -> None:
    first_path, first = create_integration_worktree(repo, "Add feature")
    second_path, second = create_integration_worktree(repo, "Add feature")

    assert first == "engine/add-feature"
    assert second == "engine/add-feature-2"
    remove_worktree(repo, first_path)
    remove_worktree(repo, second_path)


def test_two_integration_worktrees_can_coexist(repo: git.Repo) -> None:
    """The point of the change: two runs against the same repo no longer
    compete for one checkout."""
    first_path, _ = create_integration_worktree(repo, "first run")
    second_path, _ = create_integration_worktree(repo, "second run")

    assert first_path.is_dir() and second_path.is_dir()
    assert first_path != second_path
    remove_worktree(repo, first_path)
    remove_worktree(repo, second_path)


def test_integration_worktree_branches_from_head_not_the_dirty_tree(repo: git.Repo) -> None:
    """A worktree checkout only ever contains committed state — which is what
    makes running against a dirty target safe, and what the supervisor warns
    about."""
    Path(repo.working_tree_dir, "uncommitted.py").write_text("x = 1\n", encoding="utf-8")

    path, _ = create_integration_worktree(repo, "run anyway")

    assert not (path / "uncommitted.py").exists()
    remove_worktree(repo, path)


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
    base_branch = create_integration_worktree(repo, "Add feature")[1]

    worktree_path, task_branch = create_worktree(repo, base_branch, "backend")

    assert task_branch == "engine-task/add-feature-backend"
    assert worktree_path.is_dir()
    worktree_repo = git.Repo(worktree_path)
    assert worktree_repo.active_branch.name == task_branch
    assert worktree_repo.head.commit.hexsha == repo.head.commit.hexsha


def test_create_worktree_sees_files_committed_on_the_base_branch(repo: git.Repo) -> None:
    """A later stage must see what earlier ones merged. Those commits land on
    the *integration* worktree now, not the caller's checkout — which is the
    whole point of the isolation, and what this asserts."""
    integration_path, base_branch = create_integration_worktree(repo, "Add feature")
    integration_repo = git.Repo(integration_path)
    Path(integration_path, "architecture.md").write_text("plan\n", encoding="utf-8")
    commit_all(integration_repo, "architecture done")

    worktree_path, _ = create_worktree(repo, base_branch, "backend")

    assert (worktree_path / "architecture.md").is_file()
    # the caller's own checkout never saw any of it
    assert not Path(repo.working_tree_dir, "architecture.md").exists()
    remove_worktree(repo, worktree_path)
    remove_worktree(repo, integration_path)


def test_merge_worktree_merges_changes_back(repo: git.Repo) -> None:
    integration_path, base_branch = create_integration_worktree(repo, "Add feature")
    worktree_path, task_branch = create_worktree(repo, base_branch, "backend")
    Path(worktree_path, "backend.py").write_text("x = 1\n", encoding="utf-8")
    commit_all(git.Repo(worktree_path), "backend done")

    merged = merge_worktree(git.Repo(integration_path), task_branch)

    assert merged is True
    assert (integration_path / "backend.py").is_file()
    # merged into the run's branch, not into whatever the user had checked out
    assert not Path(repo.working_tree_dir, "backend.py").exists()
    remove_worktree(repo, worktree_path)
    remove_worktree(repo, integration_path)


def test_merge_worktree_returns_false_on_conflict_and_aborts_cleanly(repo: git.Repo) -> None:
    integration_path, base_branch = create_integration_worktree(repo, "Add feature")
    integration_repo = git.Repo(integration_path)
    worktree_path, task_branch = create_worktree(repo, base_branch, "backend")

    # the run branch and the task branch each change the same file differently
    Path(integration_path, "README.md").write_text("run change\n", encoding="utf-8")
    commit_all(integration_repo, "run branch changes readme")
    Path(worktree_path, "README.md").write_text("worktree change\n", encoding="utf-8")
    commit_all(git.Repo(worktree_path), "worktree changes readme")

    merged = merge_worktree(integration_repo, task_branch)

    assert merged is False
    assert not integration_repo.is_dirty(untracked_files=True)  # `merge --abort` left it clean
    remove_worktree(repo, worktree_path)
    remove_worktree(repo, integration_path)


def test_remove_worktree_removes_the_directory(repo: git.Repo) -> None:
    base_branch = create_integration_worktree(repo, "Add feature")[1]
    worktree_path, _ = create_worktree(repo, base_branch, "backend")

    remove_worktree(repo, worktree_path)

    assert not worktree_path.exists()


def test_prune_worktrees_cleans_up_stale_registration(repo: git.Repo) -> None:
    base_branch = create_integration_worktree(repo, "Add feature")[1]
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
    branch = create_integration_worktree(repo, "add a thing")[1]
    first_path, first_branch = create_worktree(repo, branch, "documentation")
    remove_worktree(repo, first_path)  # worktree gone, branch deliberately kept

    second_path, second_branch = create_worktree(repo, branch, "documentation")

    assert second_branch != first_branch
    assert first_branch in {h.name for h in repo.heads}  # earlier work still reachable
    remove_worktree(repo, second_path)


def test_task_branch_names_stay_readable_when_uniquified(repo: git.Repo) -> None:
    branch = create_integration_worktree(repo, "add a thing")[1]
    path_one, _ = create_worktree(repo, branch, "tests")
    remove_worktree(repo, path_one)

    path_two, second = create_worktree(repo, branch, "tests")

    assert second.startswith("engine-task/add-a-thing-tests")
    remove_worktree(repo, path_two)


# --- ignored_writes / disable_hooks (issue #2) ---


def test_ignored_writes_empty_on_a_clean_repo(repo: git.Repo) -> None:
    assert ignored_writes(repo) == []


def test_ignored_writes_reports_gitignored_files(repo: git.Repo) -> None:
    Path(repo.working_tree_dir, ".gitignore").write_text("*.log\n", encoding="utf-8")
    repo.index.add([".gitignore"])
    repo.index.commit("add gitignore")

    Path(repo.working_tree_dir, "exfil.log").write_text("secret\n", encoding="utf-8")

    assert ignored_writes(repo) == ["exfil.log"]


def test_ignored_writes_does_not_flag_ordinary_tracked_changes(repo: git.Repo) -> None:
    Path(repo.working_tree_dir, "README.md").write_text("changed\n", encoding="utf-8")
    Path(repo.working_tree_dir, "new_tracked.py").write_text("x = 1\n", encoding="utf-8")

    assert ignored_writes(repo) == []


def _plant_real_hook(repo: git.Repo, name: str, script: str) -> Path:
    """Writes to the *conventional* hooks path (.git/hooks/<name>) -- what an
    agent that doesn't know about disable_hooks would actually target. Not
    wherever core.hooksPath currently resolves to: a hook planted there would
    trivially fire regardless of the fix, since that's already the directory
    git is configured to look in."""
    real_hooks = Path(repo.git_dir) / "hooks"
    real_hooks.mkdir(parents=True, exist_ok=True)
    hook_path = real_hooks / name
    hook_path.write_text(script, encoding="utf-8")
    os.chmod(hook_path, 0o755)
    return hook_path


def test_disable_hooks_neutralizes_a_hook_at_the_real_conventional_path(
    repo: git.Repo, tmp_path: Path
) -> None:
    """The concrete scenario from issue #2: an agent plants a hook at the
    conventional .git/hooks/pre-commit path, then a later git operation in
    this same run (here, the next commit) would fire it under the old,
    unprotected behavior."""
    marker = tmp_path.parent / f"hook-marker-{tmp_path.name}"
    marker.unlink(missing_ok=True)
    _plant_real_hook(repo, "pre-commit", f"#!/bin/sh\ntouch {marker}\n")

    with disable_hooks(repo):
        Path(repo.working_tree_dir, "f.py").write_text("x = 1\n", encoding="utf-8")
        commit_all(repo, "commit while a real .git/hooks/pre-commit exists")

    assert not marker.exists()


def test_disable_hooks_is_not_permanent_the_real_hook_still_fires_once_lifted(
    repo: git.Repo, tmp_path: Path
) -> None:
    """Guards against a fix that's *too* effective: this must not silently
    disable a user's own legitimate hooks outside the run's write-capable
    window -- only redirect them for its duration."""
    marker = tmp_path.parent / f"hook-marker-lifted-{tmp_path.name}"
    marker.unlink(missing_ok=True)
    _plant_real_hook(repo, "post-commit", f"#!/bin/sh\ntouch {marker}\n")

    with disable_hooks(repo):
        pass

    Path(repo.working_tree_dir, "f.py").write_text("x = 1\n", encoding="utf-8")
    commit_all(repo, "commit after protection is lifted")

    assert marker.exists()


def test_disable_hooks_restores_the_previous_value_on_exit(repo: git.Repo, tmp_path: Path) -> None:
    custom_hooks = tmp_path / "custom-hooks"
    custom_hooks.mkdir()
    with repo.config_writer() as writer:
        writer.set_value("core", "hooksPath", str(custom_hooks))

    with disable_hooks(repo):
        assert repo.config_reader().get_value("core", "hooksPath") != str(custom_hooks)

    assert repo.config_reader().get_value("core", "hooksPath") == str(custom_hooks)


def test_disable_hooks_unsets_cleanly_when_nothing_was_configured_before(repo: git.Repo) -> None:
    with disable_hooks(repo):
        pass

    with pytest.raises(Exception):
        repo.config_reader().get_value("core", "hooksPath")


def test_disable_hooks_restores_even_if_the_block_raises(repo: git.Repo) -> None:
    with pytest.raises(ValueError):
        with disable_hooks(repo):
            raise ValueError("boom")

    with pytest.raises(Exception):
        repo.config_reader().get_value("core", "hooksPath")


def test_a_shell_hook_that_would_unpickle_attacker_bytes_never_runs_at_all(
    repo: git.Repo, tmp_path: Path
) -> None:
    """Not a git_ops fix by itself (see core.graph.builder, issue #3) --
    included here because disable_hooks defends the same class of reach (a
    write that survives to influence a later git operation) one level up:
    it doesn't matter *what* a hook at the real path would have done --
    unpickle a payload, exfiltrate data, anything -- if git never executes
    it in the first place."""
    marker = tmp_path.parent / f"pickle-marker-{tmp_path.name}"
    marker.unlink(missing_ok=True)
    _plant_real_hook(repo, "post-commit", f"#!/bin/sh\ntouch {marker}\n")

    with disable_hooks(repo):
        Path(repo.working_tree_dir, "g.py").write_text("y = 1\n", encoding="utf-8")
        commit_all(repo, "commit with a real hook sitting at .git/hooks/post-commit")

    assert not marker.exists()


# --- run serialization (shared git config) ---


def test_exclusive_run_lock_refuses_a_second_concurrent_run(repo: git.Repo) -> None:
    """disable_hooks rewrites core.hooksPath, which is repository-wide config
    shared by every worktree -- so two concurrent runs would race to restore
    it, and one could leave the other's neutralized path behind."""
    with exclusive_run_lock(repo):
        with pytest.raises(RuntimeError, match="already modifying"):
            with exclusive_run_lock(repo):
                pass


def test_exclusive_run_lock_is_released_afterwards(repo: git.Repo) -> None:
    with exclusive_run_lock(repo):
        pass

    with exclusive_run_lock(repo):  # a later run gets it cleanly
        pass


def test_exclusive_run_lock_is_released_even_if_the_body_raises(repo: git.Repo) -> None:
    with pytest.raises(ValueError):
        with exclusive_run_lock(repo):
            raise ValueError("boom")

    with exclusive_run_lock(repo):
        pass


def test_classify_ignored_writes_splits_declared_from_unexpected(repo: git.Repo) -> None:
    Path(repo.working_tree_dir, ".gitignore").write_text(".pytest_cache/\n*.log\n", encoding="utf-8")
    repo.index.add([".gitignore"])
    repo.index.commit("ignore rules")

    cache = Path(repo.working_tree_dir, ".pytest_cache")
    cache.mkdir()
    (cache / "CACHEDIR.TAG").write_text("x", encoding="utf-8")
    Path(repo.working_tree_dir, "exfil.log").write_text("secret", encoding="utf-8")

    expected, violations = classify_ignored_writes(repo, (".pytest_cache/**",))

    assert any(".pytest_cache" in p for p in expected)
    assert violations == ["exfil.log"]


def test_classify_ignored_writes_subtracts_the_baseline(repo: git.Repo) -> None:
    """Per actor, not once per run: what was already there before this actor
    ran isn't something it wrote."""
    Path(repo.working_tree_dir, ".gitignore").write_text("*.log\n", encoding="utf-8")
    repo.index.add([".gitignore"])
    repo.index.commit("ignore rules")
    Path(repo.working_tree_dir, "pre-existing.log").write_text("x", encoding="utf-8")

    baseline = set(ignored_writes(repo))
    Path(repo.working_tree_dir, "new.log").write_text("y", encoding="utf-8")

    _, violations = classify_ignored_writes(repo, (), baseline)

    assert violations == ["new.log"]
