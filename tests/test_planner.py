"""Tests for core.orchestrator.planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import ConfigError
from core.orchestrator import platform_config as pc
from core.orchestrator.planner import Plan, Task, load_workflow, plan, prune


def _write_workflow(tmp_path: Path, content: str, mode: str = "standard") -> Path:
    preset_dir = tmp_path / "config/presets/workflow"
    preset_dir.mkdir(parents=True, exist_ok=True)
    (preset_dir / f"{mode}.yaml").write_text(content, encoding="utf-8")
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


def test_load_workflow_resolves_a_named_mode(tmp_path: Path) -> None:
    """The workflow preset is picked by name, same as a profile or a context
    mode — this is what makes `workflow.mode` in platform.yaml mean anything."""
    _write_workflow(tmp_path, "tasks:\n  - {id: a, agent: architect, depends_on: []}\n", mode="lean")

    tasks = load_workflow(tmp_path, mode="lean")

    assert [t.id for t in tasks] == ["a"]


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

    result = plan(repo_root, pc.PlatformConfig())

    assert result == Plan(tasks=[Task(id="architecture", agent="architect", depends_on=[])])


def test_plan_self_loads_platform_config_when_none_is_given(tmp_path: Path) -> None:
    """A standalone caller with no run-scoped PlatformConfig to thread through
    still gets today's shipped defaults."""
    repo_root = _write_workflow(
        tmp_path, "tasks:\n  - {id: architecture, agent: architect, depends_on: []}\n"
    )
    (repo_root / "config/presets/profiles").mkdir(parents=True)
    (repo_root / "config/presets/profiles/balanced.yaml").write_text("{}\n", encoding="utf-8")
    (repo_root / "config/presets/context").mkdir(parents=True)
    (repo_root / "config/presets/context/smart.yaml").write_text(
        "use_git_diff: true\n", encoding="utf-8"
    )

    result = plan(repo_root)

    assert (result.max_parallel, result.decompose, result.max_correction_attempts) == (2, True, 1)


# --- max_parallel/decompose/max_correction_attempts come from PlatformConfig,
# not from the workflow file -- they're operational knobs, not DAG shape.
# Validating them is platform_config.py's job now (tests/test_platform_config.py);
# these confirm plan() actually threads what it's given.


def test_plan_threads_the_given_platform_config(tmp_path: Path) -> None:
    repo_root = _write_workflow(
        tmp_path,
        "tasks:\n  - {id: architecture, agent: architect, depends_on: []}\n",
    )
    config = pc.PlatformConfig(max_parallel=4, decompose=False, max_correction_attempts=3)

    result = plan(repo_root, config)

    assert (result.max_parallel, result.decompose, result.max_correction_attempts) == (4, False, 3)


def test_plan_resolves_the_workflow_mode_from_platform_config(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "tasks:\n  - {id: a, agent: architect, depends_on: []}\n", mode="lean")
    config = pc.PlatformConfig(workflow_mode="lean")

    result = plan(tmp_path, config)

    assert [t.id for t in result.tasks] == ["a"]


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


def test_prune_removes_dependency_that_has_nothing_to_bridge_to() -> None:
    original = Plan(
        tasks=[
            Task(id="architecture", agent="architect", depends_on=[]),
            Task(id="backend", agent="backend", depends_on=["architecture"]),
        ]
    )

    pruned = prune(original, {"backend"})

    assert pruned.tasks == [Task(id="backend", agent="backend", depends_on=[])]


def test_prune_bridges_dependency_through_a_pruned_middle_node() -> None:
    # tests -> security -> documentation, security pruned out: found via a
    # real decomposition, documentation must still depend on tests, not run
    # with zero visibility into what tests (and backend before it) produced.
    original = Plan(
        tasks=[
            Task(id="tests", agent="tests", depends_on=[]),
            Task(id="security", agent="security", depends_on=["tests"]),
            Task(id="documentation", agent="documentation", depends_on=["security"]),
        ]
    )

    pruned = prune(original, {"tests", "documentation"})

    by_id = {t.id: t for t in pruned.tasks}
    assert by_id["documentation"].depends_on == ["tests"]


def test_prune_bridges_through_multiple_consecutive_pruned_nodes() -> None:
    original = Plan(
        tasks=[
            Task(id="a", agent="a", depends_on=[]),
            Task(id="b", agent="b", depends_on=["a"]),
            Task(id="c", agent="c", depends_on=["b"]),
            Task(id="d", agent="d", depends_on=["c"]),
        ]
    )

    pruned = prune(original, {"a", "d"})

    by_id = {t.id: t for t in pruned.tasks}
    assert by_id["d"].depends_on == ["a"]


def test_prune_preserves_max_parallel_and_decompose() -> None:
    original = Plan(
        tasks=[Task(id="architecture", agent="architect", depends_on=[])],
        max_parallel=5,
        decompose=False,
    )

    pruned = prune(original, {"architecture"})

    assert pruned.max_parallel == 5
    assert pruned.decompose is False
