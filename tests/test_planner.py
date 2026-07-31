"""Tests for core.orchestrator.planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import ConfigError
from core.orchestrator.planner import Plan, Task, load_workflow, plan


def _write_workflow(tmp_path: Path, content: str) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "workflow.yaml").write_text(content, encoding="utf-8")
    return tmp_path


def test_load_workflow_returns_tasks_in_topological_order(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - id: tests
    agent: tests
    depends_on: [backend, frontend]
  - id: backend
    agent: backend
    depends_on: [architecture]
  - id: architecture
    agent: architect
    depends_on: []
  - id: frontend
    agent: frontend
    depends_on: [architecture]
""",
    )

    tasks = load_workflow(repo_root)

    ids = [t.id for t in tasks]
    assert ids.index("architecture") < ids.index("backend")
    assert ids.index("architecture") < ids.index("frontend")
    assert ids.index("backend") < ids.index("tests")
    assert ids.index("frontend") < ids.index("tests")


def test_load_workflow_ties_broken_by_declaration_order(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - id: b
    agent: backend
    depends_on: []
  - id: a
    agent: architect
    depends_on: []
""",
    )

    tasks = load_workflow(repo_root)

    assert [t.id for t in tasks] == ["b", "a"]


def test_load_workflow_duplicate_id_raises(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - id: backend
    agent: backend
    depends_on: []
  - id: backend
    agent: backend
    depends_on: []
""",
    )

    with pytest.raises(ConfigError, match="Duplicate task id"):
        load_workflow(repo_root)


def test_load_workflow_unknown_dependency_raises(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - id: backend
    agent: backend
    depends_on: [does_not_exist]
""",
    )

    with pytest.raises(ConfigError, match="unknown task"):
        load_workflow(repo_root)


def test_load_workflow_cycle_raises(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - id: a
    agent: architect
    depends_on: [b]
  - id: b
    agent: backend
    depends_on: [a]
""",
    )

    with pytest.raises(ConfigError, match="Cycle detected"):
        load_workflow(repo_root)


def test_load_workflow_missing_id_or_agent_raises(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - agent: backend
    depends_on: []
""",
    )

    with pytest.raises(ConfigError, match="missing 'id' or 'agent'"):
        load_workflow(repo_root)


def test_plan_wraps_load_workflow(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - id: architecture
    agent: architect
    depends_on: []
""",
    )

    result = plan(repo_root)

    assert result == Plan(tasks=[Task(id="architecture", agent="architect", depends_on=[])])
