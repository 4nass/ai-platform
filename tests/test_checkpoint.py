"""Tests for core.orchestrator.checkpoint."""

from __future__ import annotations

import json
from pathlib import Path

import git
import pytest

from core.orchestrator import checkpoint, git_ops


@pytest.fixture
def repo(tmp_path: Path) -> git.Repo:
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")
    repo.index.add(["src.py"])
    repo.index.commit("initial")
    return repo


@pytest.fixture
def worktree(repo: git.Repo) -> Path:
    path, branch = git_ops.create_integration_worktree(repo, "add oauth")
    checkpoint.save(
        path,
        checkpoint.Checkpoint(
            base_sha=repo.head.commit.hexsha,
            branch=branch,
            request="add oauth",
            complexity="complex",
            task_ids=["architecture", "backend"],
        ),
    )
    return path


def test_a_checkpoint_round_trips(worktree: Path) -> None:
    state = checkpoint.load(worktree)

    assert state is not None
    assert state.request == "add oauth"
    assert state.complexity == "complex"
    assert state.task_ids == ["architecture", "backend"]
    assert state.completed_ids == set()


def test_recording_a_stage_appends_and_persists_it(worktree: Path) -> None:
    state = checkpoint.load(worktree)

    state = checkpoint.record_stage(
        worktree,
        state,
        checkpoint.StageRecord(
            id="architecture", agent="architect", summary="designed it", files_changed=["a.md"]
        ),
    )

    assert checkpoint.load(worktree) == state
    assert checkpoint.load(worktree).completed_ids == {"architecture"}


def test_recording_returns_a_new_checkpoint_rather_than_mutating(worktree: Path) -> None:
    """Frozen on purpose: the DAG walk holds one snapshot and rebinds it, so
    there is no shared object a still-running stage can observe half-updated."""
    before = checkpoint.load(worktree)

    after = checkpoint.record_stage(
        worktree, before, checkpoint.StageRecord(id="backend", agent="backend")
    )

    assert before.stages == []
    assert [s.id for s in after.stages] == ["backend"]


def test_the_checkpoint_is_invisible_to_a_stage_commit(repo: git.Repo, worktree: Path) -> None:
    """It lives in the worktree's git directory, not the worktree. In the tree,
    `git_ops.commit_all`'s `git add -A` would sweep it onto the branch under
    review — engine bookkeeping committed as if an agent had written it."""
    (worktree / "new.py").write_text("y = 2\n", encoding="utf-8")

    changed = git_ops.commit_all(git.Repo(worktree), "stage work")

    assert changed == ["new.py"]
    assert checkpoint.load(worktree) is not None


def test_the_checkpoint_dies_with_the_worktree(repo: git.Repo, worktree: Path) -> None:
    """A successful run removes its integration worktree, so its checkpoint
    goes too — nothing can offer to resume a run that already finished."""
    git_ops.remove_worktree(repo, worktree)

    assert checkpoint.load(worktree) is None


def test_loading_a_path_that_was_never_a_worktree_is_not_an_error(tmp_path: Path) -> None:
    assert checkpoint.load(tmp_path / "nowhere") is None


def test_a_truncated_checkpoint_reads_as_nothing_to_resume(worktree: Path) -> None:
    """Exactly what a crash mid-write leaves. It means "start over", which is
    safe; raising here would instead fail the command that noticed."""
    checkpoint.path_for(worktree).write_text('{"base_sha": "abc", "bra', encoding="utf-8")

    assert checkpoint.load(worktree) is None


def test_a_checkpoint_from_another_version_is_refused(worktree: Path) -> None:
    path = checkpoint.path_for(worktree)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["version"] = checkpoint.VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert checkpoint.load(worktree) is None


def test_an_unwritable_checkpoint_does_not_fail_the_run(worktree: Path, monkeypatch) -> None:
    """Called from the middle of a DAG walk with real merged work behind it.
    Losing the record costs a resumed run some repeated stages; raising would
    cost the run itself."""
    monkeypatch.setattr(
        checkpoint, "path_for", lambda _: Path("/proc/definitely/not/writable")
    )

    checkpoint.save(worktree, checkpoint.Checkpoint(base_sha="a", branch="b", request="c", complexity="routine"))
