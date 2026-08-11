"""Tests for issue #33 Git remote synchronization and delivery guards."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import git
import pytest

from core.orchestrator import git_remote


def _repo(path: Path) -> tuple[git.Repo, git.Repo]:
    repo = git.Repo.init(path / "work")
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    repo.git.checkout("-b", "main")
    (path / "work" / "README.md").write_text("initial\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("initial")
    bare = git.Repo.init(path / "remote.git", bare=True)
    repo.create_remote("origin", str(path / "remote.git"))
    repo.git.push("-u", "origin", "main")
    return repo, bare


def _project(remote: Path, policy: str) -> SimpleNamespace:
    return SimpleNamespace(
        remote=str(remote), base_branch="main", sync_policy=policy
    )


def _remote_commit(remote: Path, clone_path: Path, text: str) -> git.Repo:
    clone = git.Repo.clone_from(str(remote), clone_path, no_checkout=True)
    clone.git.checkout("-b", "main", "origin/main")
    with clone.config_writer() as writer:
        writer.set_value("user", "name", "Other")
        writer.set_value("user", "email", "other@example.com")
    (clone_path / "README.md").write_text(text, encoding="utf-8")
    clone.index.add(["README.md"])
    clone.index.commit("remote change")
    clone.git.push("origin", "main")
    return clone


def test_offline_snapshot_is_pinned_without_touching_checkout(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    before_branch = repo.active_branch.name
    (Path(repo.working_tree_dir) / "dirty.txt").write_text("keep", encoding="utf-8")

    snapshot = git_remote.synchronize_base(repo)

    assert snapshot.base_ref == "main"
    assert snapshot.base_sha == repo.head.commit.hexsha
    assert snapshot.sync_status == "offline"
    assert repo.active_branch.name == before_branch
    assert (Path(repo.working_tree_dir) / "dirty.txt").read_text() == "keep"


def test_fetch_policy_uses_remote_ahead_without_checkout(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    _remote_commit(tmp_path / "remote.git", tmp_path / "other", "remote\n")
    before_branch = repo.active_branch.name
    before_content = (Path(repo.working_tree_dir) / "README.md").read_text()

    snapshot = git_remote.synchronize_base(repo, _project(tmp_path / "remote.git", "fetch"))

    assert snapshot.sync_status == "remote_ahead"
    assert snapshot.base_ref == "refs/remotes/origin/main"
    assert snapshot.base_sha == repo.refs["origin/main"].commit.hexsha
    assert repo.active_branch.name == before_branch
    assert (Path(repo.working_tree_dir) / "README.md").read_text() == before_content


def test_require_up_to_date_rejects_remote_ahead(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    _remote_commit(tmp_path / "remote.git", tmp_path / "other", "remote\n")

    with pytest.raises(git_remote.RemoteSyncError, match="behind remote"):
        git_remote.synchronize_base(repo, _project(tmp_path / "remote.git", "require_up_to_date"))


def test_diverged_base_is_explicitly_rejected(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    (Path(repo.working_tree_dir) / "README.md").write_text("local\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("local change")
    _remote_commit(tmp_path / "remote.git", tmp_path / "other", "remote\n")

    with pytest.raises(git_remote.RemoteSyncError, match="diverged"):
        git_remote.synchronize_base(repo, _project(tmp_path / "remote.git", "fetch"))


def test_delivery_rechecks_remote_base_and_requires_approval(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    snapshot = git_remote.synchronize_base(repo, _project(tmp_path / "remote.git", "fetch"))
    repo.git.checkout("-b", "engine/delivery")
    (Path(repo.working_tree_dir) / "change.txt").write_text("change", encoding="utf-8")
    repo.index.add(["change.txt"])
    repo.index.commit("delivery")

    with pytest.raises(git_remote.RemoteSyncError, match="explicit approval"):
        git_remote.push_delivery_branch(repo, snapshot, "engine/delivery")

    assert git_remote.push_delivery_branch(repo, snapshot, "engine/delivery", approved=True) == "refs/heads/engine/delivery"


def test_delivery_refuses_when_remote_base_moved(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    snapshot = git_remote.synchronize_base(repo, _project(tmp_path / "remote.git", "fetch"))
    _remote_commit(tmp_path / "remote.git", tmp_path / "other", "moved\n")

    with pytest.raises(git_remote.RemoteSyncError, match="moved"):
        git_remote.verify_base_current(repo, snapshot)
