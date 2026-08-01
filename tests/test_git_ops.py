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
    local_modifications,
    merge_worktree,
    prune_worktrees,
    remove_worktree,
    restore_hooks,
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


def test_local_modifications_empty_when_clean(repo: git.Repo) -> None:
    assert local_modifications(repo) == []


def test_local_modifications_lists_tracked_and_untracked_alike(repo: git.Repo) -> None:
    """Both are equally outside the run: the integration worktree is checked
    out from the base commit, so neither reaches the agents."""
    Path(repo.working_tree_dir, "README.md").write_text("edited\n", encoding="utf-8")
    Path(repo.working_tree_dir, "untracked.txt").write_text("x", encoding="utf-8")

    assert sorted(local_modifications(repo)) == ["README.md", "untracked.txt"]


def test_local_modifications_lists_files_inside_untracked_directories(repo: git.Repo) -> None:
    """`git status` collapses an untracked directory to one entry by default,
    which would report "1 modification" for twenty new files."""
    nested = Path(repo.working_tree_dir, "newpkg", "sub")
    nested.mkdir(parents=True)
    (nested / "a.py").write_text("a", encoding="utf-8")
    (nested / "b.py").write_text("b", encoding="utf-8")

    assert sorted(local_modifications(repo)) == ["newpkg/sub/a.py", "newpkg/sub/b.py"]


def test_local_modifications_ignores_the_engines_own_index(repo: git.Repo) -> None:
    """`.ai-platform/` is the vector store and graph cache — something the
    engine wrote, not work the user is in the middle of. Counting it would
    report a local modification on every run against a target that doesn't
    gitignore it."""
    index_dir = Path(repo.working_tree_dir, ".ai-platform", "vector")
    index_dir.mkdir(parents=True)
    (index_dir / "db").write_text("x", encoding="utf-8")

    assert local_modifications(repo) == []


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


def test_disable_hooks_saves_the_previous_value_durably(repo: git.Repo) -> None:
    """A `finally` only runs if the process lives to run it. Killing a worker
    mid-run otherwise left the target pointing at the neutral directory
    permanently, silently disabling the user's own hooks in their repo."""
    with disable_hooks(repo):
        saved = repo.config_reader().get_value("ai-platform", "savedHooksPath")
        assert saved == "<unset>"  # nothing was configured before


def test_restore_hooks_repairs_a_crashed_run(repo: git.Repo) -> None:
    import shutil
    import tempfile

    from core.orchestrator.git_ops import HOOKS_DISABLED_PREFIX

    # simulate a run that neutralized hooks and was killed before its finally
    neutral = tempfile.mkdtemp(prefix=HOOKS_DISABLED_PREFIX)
    with repo.config_writer() as writer:
        writer.set_value("ai-platform", "savedHooksPath", "<unset>")
        writer.set_value("core", "hooksPath", neutral)

    assert restore_hooks(repo) is True

    with pytest.raises(Exception):
        repo.config_reader().get_value("core", "hooksPath")
    shutil.rmtree(neutral, ignore_errors=True)


def test_restore_hooks_puts_a_custom_path_back(repo: git.Repo) -> None:
    """A user who had set their own `core.hooksPath` must get exactly that
    back, not git's default — which is why the previous value is written to
    the repo's config rather than only held in memory."""
    custom = str(Path(repo.working_tree_dir, "my-hooks"))
    with repo.config_writer() as writer:
        writer.set_value("core", "hooksPath", custom)

    with disable_hooks(repo):
        assert repo.config_reader().get_value("core", "hooksPath") != custom

    assert repo.config_reader().get_value("core", "hooksPath") == custom


def test_restore_hooks_after_a_crash_puts_a_custom_path_back(repo: git.Repo) -> None:
    import shutil
    import tempfile

    from core.orchestrator.git_ops import HOOKS_DISABLED_PREFIX

    custom = str(Path(repo.working_tree_dir, "my-hooks"))
    neutral = tempfile.mkdtemp(prefix=HOOKS_DISABLED_PREFIX)
    with repo.config_writer() as writer:
        writer.set_value("ai-platform", "savedHooksPath", custom)
        writer.set_value("core", "hooksPath", neutral)

    assert restore_hooks(repo) is True

    assert repo.config_reader().get_value("core", "hooksPath") == custom
    shutil.rmtree(neutral, ignore_errors=True)


def test_restore_hooks_is_a_no_op_on_an_untouched_repo(repo: git.Repo) -> None:
    assert restore_hooks(repo) is False


def test_restore_hooks_does_not_overwrite_a_deliberate_later_setting(repo: git.Repo) -> None:
    """If `core.hooksPath` no longer points at an engine directory, someone
    set it after the crash. Reversing that would undo a deliberate choice."""
    deliberate = str(Path(repo.working_tree_dir, "chosen-later"))
    with repo.config_writer() as writer:
        writer.set_value("ai-platform", "savedHooksPath", "<unset>")
        writer.set_value("core", "hooksPath", deliberate)

    assert restore_hooks(repo) is False

    assert repo.config_reader().get_value("core", "hooksPath") == deliberate
    # and the stale saved value is dropped, so a later call can't resurrect it
    with pytest.raises(Exception):
        repo.config_reader().get_value("ai-platform", "savedHooksPath")


def test_restore_hooks_is_idempotent(repo: git.Repo) -> None:
    with disable_hooks(repo):
        pass

    assert restore_hooks(repo) is False


def _crash_mid_run(repo: git.Repo) -> None:
    """A run that neutralized hooks and never reached its `finally`."""
    disable_hooks(repo).__enter__()


def _hooks_path(repo: git.Repo) -> str | None:
    try:
        return str(repo.config_reader().get_value("core", "hooksPath"))
    except Exception:
        return None


def test_a_crashed_run_does_not_poison_the_next_one(repo: git.Repo) -> None:
    """The leak used to *compound*, which is what made it permanent.

    After a crash `core.hooksPath` is the engine's own neutral directory, so
    the next run read that as "what the user had", saved it, restored it on a
    perfectly clean exit and dropped the saved key — leaving the repo pointing
    at a deleted directory with nothing left to repair it from. Measured on a
    real crashed-then-clean pair before `disable_hooks` repaired on entry.
    """
    custom = str(Path(repo.working_tree_dir, "my-hooks"))
    with repo.config_writer() as writer:
        writer.set_value("core", "hooksPath", custom)

    _crash_mid_run(repo)
    with disable_hooks(repo):  # an ordinary, healthy later run
        pass

    assert _hooks_path(repo) == custom


def test_repeated_crashes_still_restore_the_users_own_path(repo: git.Repo) -> None:
    custom = str(Path(repo.working_tree_dir, "my-hooks"))
    with repo.config_writer() as writer:
        writer.set_value("core", "hooksPath", custom)

    _crash_mid_run(repo)
    _crash_mid_run(repo)
    with disable_hooks(repo):
        pass

    assert _hooks_path(repo) == custom


def test_a_crash_with_nothing_configured_before_ends_unset(repo: git.Repo) -> None:
    _crash_mid_run(repo)
    with disable_hooks(repo):
        pass

    assert _hooks_path(repo) is None


def test_a_repo_already_damaged_by_the_old_behaviour_is_healed(repo: git.Repo) -> None:
    """`core.hooksPath` left pointing at an engine directory with no saved
    value — the end state of the bug above. Nothing can say what the user
    had, so unset is the only honest answer, and it is what git assumes by
    default. Leaving the stale path would keep their hooks disabled forever.
    """
    with repo.config_writer() as writer:
        writer.set_value("core", "hooksPath", "/tmp/engine-hooks-disabled-gone")

    with disable_hooks(repo):
        pass

    assert _hooks_path(repo) is None


def test_a_saved_engine_path_is_never_restored_over_the_user(repo: git.Repo) -> None:
    """Two crashes under the old code left an engine directory recorded as the
    value to put back. Restoring it would reinstate the neutralization."""
    with repo.config_writer() as writer:
        writer.set_value("ai-platform", "savedHooksPath", "/tmp/engine-hooks-disabled-older")
        writer.set_value("core", "hooksPath", "/tmp/engine-hooks-disabled-newer")

    assert restore_hooks(repo) is True

    assert _hooks_path(repo) is None


def test_a_deliberate_setting_made_after_a_crash_survives_the_next_run(repo: git.Repo) -> None:
    _crash_mid_run(repo)
    deliberate = str(Path(repo.working_tree_dir, "chosen-later"))
    with repo.config_writer() as writer:
        writer.set_value("core", "hooksPath", deliberate)

    with disable_hooks(repo):
        pass

    assert _hooks_path(repo) == deliberate


def test_stage_worktrees_finds_what_an_interrupted_run_left(repo: git.Repo) -> None:
    """`worktree prune` will not reclaim these — the directory still exists,
    which is exactly why prune leaves it alone."""
    from core.orchestrator.git_ops import create_integration_worktree, create_worktree, stage_worktrees

    integration, branch = create_integration_worktree(repo, "add oauth")
    stage_path, stage_branch = create_worktree(git.Repo(integration), branch, "backend")

    found = stage_worktrees(repo, branch)

    assert found == [(stage_path, stage_branch)]


def test_stage_worktrees_ignores_other_runs(repo: git.Repo) -> None:
    from core.orchestrator.git_ops import create_integration_worktree, create_worktree, stage_worktrees

    mine, my_branch = create_integration_worktree(repo, "add oauth")
    theirs, their_branch = create_integration_worktree(repo, "something else")
    create_worktree(git.Repo(theirs), their_branch, "backend")

    assert stage_worktrees(repo, my_branch) == []


def test_stage_worktrees_is_empty_for_a_run_that_cleaned_up(repo: git.Repo) -> None:
    from core.orchestrator.git_ops import create_integration_worktree, stage_worktrees

    _, branch = create_integration_worktree(repo, "add oauth")

    assert stage_worktrees(repo, branch) == []
