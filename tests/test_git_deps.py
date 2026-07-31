"""Tests for core.graph.git_deps."""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.graph.git_deps import co_change_counts


@pytest.fixture
def repo(tmp_path: Path) -> git.Repo:
    r = git.Repo.init(tmp_path)
    with r.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    return r


def _commit(repo: git.Repo, tmp_path: Path, files: dict[str, str], message: str) -> None:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    repo.index.add(list(files))
    repo.index.commit(message)


def test_files_changed_together_are_counted(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1", "b.py": "1"}, "first")
    _commit(repo, tmp_path, {"a.py": "2", "b.py": "2"}, "second")

    counts = co_change_counts(tmp_path)

    assert counts[("a.py", "b.py")].count == 2


def test_files_changed_alone_do_not_pair(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1"}, "first")
    _commit(repo, tmp_path, {"b.py": "1"}, "second")

    assert co_change_counts(tmp_path) == {}


def test_max_commits_caps_the_history_scanned(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1", "b.py": "1"}, "first")
    _commit(repo, tmp_path, {"a.py": "2", "b.py": "2"}, "second")
    _commit(repo, tmp_path, {"a.py": "3", "b.py": "3"}, "third")

    counts = co_change_counts(tmp_path, max_commits=1)

    assert counts[("a.py", "b.py")].count == 1


def test_two_file_commit_has_maximum_possible_strength(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1", "b.py": "1"}, "focused")

    counts = co_change_counts(tmp_path)

    # a commit touching exactly the pair (the smallest possible co-changing
    # commit) yields 1/2 — the highest strength any single commit can give.
    assert counts[("a.py", "b.py")].strength == pytest.approx(0.5)


def test_large_commit_dilutes_strength(repo: git.Repo, tmp_path: Path) -> None:
    files = {f"f{i}.py": "1" for i in range(18)}
    files["a.py"] = "1"
    files["b.py"] = "1"
    _commit(repo, tmp_path, files, "mega commit touching 20 files")

    counts = co_change_counts(tmp_path)

    assert counts[("a.py", "b.py")].strength == pytest.approx(1 / 20)
