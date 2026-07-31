"""Tests for core.context.manager."""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.context import manager as manager_module
from core.context.manager import ContextManager, SelectedContext, load_config


def test_load_config_overrides_and_defaults(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "context.yaml").write_text("use_git_diff: false\nmax_files: 3\n", encoding="utf-8")

    config = load_config(tmp_path)

    assert config.use_git_diff is False
    assert config.max_files == 3
    assert config.use_vector_db is True  # not overridden, keeps its default


def test_selected_context_render_combines_all_sections() -> None:
    context = SelectedContext(
        chunks=[
            {
                "path": "a.py",
                "kind": "function",
                "name": "foo",
                "start_line": 1,
                "end_line": 2,
                "text": "def foo(): pass",
            }
        ],
        git_diff="diff --git a/a.py b/a.py",
        memory_docs={"rules.md": "Always write tests."},
    )

    rendered = context.render()

    assert "## Project memory" in rendered
    assert "rules.md" in rendered
    assert "## Current git diff" in rendered
    assert "## Relevant code excerpts" in rendered
    assert "a.py" in rendered


def test_selected_context_render_empty_context_is_empty_string() -> None:
    assert SelectedContext().render() == ""


def test_selected_context_render_includes_related_files() -> None:
    context = SelectedContext(related_files=["core/orchestrator/scheduler.py"])

    rendered = context.render()

    assert "## Related via project graph" in rendered
    assert "core/orchestrator/scheduler.py" in rendered


def test_selected_context_paths_deduplicates_and_sorts() -> None:
    context = SelectedContext(
        chunks=[
            {"path": "b.py", "kind": "file", "name": "b.py", "start_line": 1, "end_line": 1, "text": ""},
            {"path": "a.py", "kind": "function", "name": "foo", "start_line": 1, "end_line": 1, "text": ""},
            {"path": "a.py", "kind": "function", "name": "bar", "start_line": 2, "end_line": 2, "text": ""},
        ]
    )

    assert context.context_paths() == ["a.py", "b.py"]


def test_selected_context_paths_includes_related_files() -> None:
    context = SelectedContext(
        chunks=[{"path": "a.py", "kind": "file", "name": "a.py", "start_line": 1, "end_line": 1, "text": ""}],
        related_files=["b.py"],
    )

    assert context.context_paths() == ["a.py", "b.py"]


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "context.yaml").write_text(
        "use_git_diff: true\nuse_graph: false\nuse_vector_db: true\nuse_memory: true\nmax_files: 5\n",
        encoding="utf-8",
    )
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "architecture.md").write_text("We use a layered architecture.", encoding="utf-8")
    (tmp_path / "auth.py").write_text("def authenticate(user):\n    return True\n", encoding="utf-8")

    repo.index.add(["config/context.yaml", "memory/architecture.md", "auth.py"])
    repo.index.commit("initial commit")
    return tmp_path


def test_context_manager_index_and_select(fake_repo: Path) -> None:
    manager = ContextManager(fake_repo)

    n_chunks = manager.index_repo()
    assert n_chunks > 0

    context = manager.select_context("where is authentication handled?")

    assert context.memory_docs == {"architecture.md": "We use a layered architecture."}
    assert any(c["path"] == "auth.py" for c in context.chunks)


def test_select_context_expands_with_the_graph_when_enabled(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    (fake_repo / "config" / "context.yaml").write_text(
        "use_git_diff: true\nuse_graph: true\nuse_vector_db: true\nuse_memory: true\nmax_files: 5\n",
        encoding="utf-8",
    )
    repo = git.Repo(fake_repo)
    repo.index.add(["config/context.yaml"])
    repo.index.commit("enable graph")

    monkeypatch.setattr(
        manager_module.graph_builder,
        "related_files",
        lambda graph, seeds, limit: ["extra_related.py"],
    )

    manager = ContextManager(fake_repo)
    manager.index_repo()
    context = manager.select_context("where is authentication handled?")

    assert context.related_files == ["extra_related.py"]
    assert "extra_related.py" in context.context_paths()
    assert "## Related via project graph" in context.render()


def test_select_context_skips_graph_expansion_when_disabled(fake_repo: Path) -> None:
    manager = ContextManager(fake_repo)  # fake_repo's config has use_graph: false
    manager.index_repo()

    context = manager.select_context("where is authentication handled?")

    assert context.related_files == []
