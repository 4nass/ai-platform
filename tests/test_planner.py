"""Tests for core.orchestrator.planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import ConfigError
from core.orchestrator.planner import Plan, Task, load_workflow, plan, prune


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


def test_plan_uses_default_max_parallel_when_absent(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - id: architecture
    agent: architect
    depends_on: []
""",
    )

    assert plan(repo_root).max_parallel == 2


def test_plan_reads_max_parallel_override(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
max_parallel: 4
tasks:
  - id: architecture
    agent: architect
    depends_on: []
""",
    )

    assert plan(repo_root).max_parallel == 4


def test_plan_rejects_non_positive_max_parallel(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
max_parallel: 0
tasks:
  - id: architecture
    agent: architect
    depends_on: []
""",
    )

    with pytest.raises(ConfigError, match="max_parallel"):
        plan(repo_root)


def test_plan_uses_default_decompose_when_absent(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
tasks:
  - id: architecture
    agent: architect
    depends_on: []
""",
    )

    assert plan(repo_root).decompose is True


def test_plan_reads_decompose_override(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
decompose: false
tasks:
  - id: architecture
    agent: architect
    depends_on: []
""",
    )

    assert plan(repo_root).decompose is False


def test_plan_rejects_non_boolean_decompose(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        """
decompose: yes-please
tasks:
  - id: architecture
    agent: architect
    depends_on: []
""",
    )

    with pytest.raises(ConfigError, match="decompose"):
        plan(repo_root)


def test_prune_keeps_only_selected_tasks() -> None:
    original = Plan(
        tasks=[
            Task(id="architecture", agent="architect", depends_on=[]),
            Task(id="backend", agent="backend", depends_on=["architecture"]),
            Task(id="frontend", agent="frontend", depends_on=["architecture"]),
        ]
    )

    pruned = prune(original, {"architecture", "backend"})

    assert [t.id for t in pruned.tasks] == ["architecture", "backend"]


def test_prune_drops_dangling_dependency_references() -> None:
    original = Plan(
        tasks=[
            Task(id="architecture", agent="architect", depends_on=[]),
            Task(id="backend", agent="backend", depends_on=["architecture"]),
        ]
    )

    pruned = prune(original, {"backend"})

    assert pruned.tasks == [Task(id="backend", agent="backend", depends_on=[])]


def test_prune_preserves_max_parallel_and_decompose() -> None:
    original = Plan(
        tasks=[Task(id="architecture", agent="architect", depends_on=[])],
        max_parallel=5,
        decompose=False,
    )

    pruned = prune(original, {"architecture"})

    assert pruned.max_parallel == 5
    assert pruned.decompose is False
