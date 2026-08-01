"""Planner: builds the task DAG for a run.

The DAG's dependency structure is a static template declared in the selected
workflow preset (config/presets/workflow/<mode>.yaml, see
core.orchestrator.platform_config), the same "declared, not inferred" pattern
already used for provider profiles — task ids, roles and edges are never
invented by an LLM. What *can* vary per request is which subset of that
pre-validated DAG actually runs: core.orchestrator.decomposer (an LLM call)
picks a subset of task ids, and `prune()` here narrows the plan down to it.
Pruning a subgraph of an already-cycle-free, already-reference-checked DAG is
safe by construction — no need to re-validate.

`max_parallel`/`decompose`/`max_correction_attempts` are not part of the
preset: they're operational knobs a user tunes in config/platform.yaml, not
DAG shape, so `plan()` takes them as explicit arguments rather than parsing
them out of the workflow file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from core.errors import ConfigError

if TYPE_CHECKING:
    from core.orchestrator.platform_config import PlatformConfig

DEFAULT_MAX_PARALLEL = 2
DEFAULT_DECOMPOSE = True
DEFAULT_MAX_CORRECTION_ATTEMPTS = 1


@dataclass
class Task:
    id: str
    agent: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    tasks: list[Task]
    max_parallel: int = DEFAULT_MAX_PARALLEL
    decompose: bool = DEFAULT_DECOMPOSE
    max_correction_attempts: int = DEFAULT_MAX_CORRECTION_ATTEMPTS
    """How many test/review-failure -> corrector -> re-check passes run() may
    attempt before giving up as "needs attention". Bounded on purpose: an
    unbounded retry against a real provider is a quota/cost risk, not just a
    latency one (see core.orchestrator.correction)."""


def _parse_tasks(data: dict) -> list[Task]:
    raw_tasks = data.get("tasks") or []
    tasks: list[Task] = []
    for entry in raw_tasks:
        if not isinstance(entry, dict) or "id" not in entry or "agent" not in entry:
            raise ConfigError(f"Workflow task entry missing 'id' or 'agent': {entry}")
        tasks.append(Task(id=entry["id"], agent=entry["agent"], depends_on=list(entry.get("depends_on") or [])))

    ids = [t.id for t in tasks]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ConfigError(f"Duplicate task id(s) in workflow: {', '.join(duplicates)}")

    known_ids = set(ids)
    for task in tasks:
        unknown = [dep for dep in task.depends_on if dep not in known_ids]
        if unknown:
            raise ConfigError(f"Task '{task.id}' depends on unknown task(s): {', '.join(unknown)}")

    return tasks


def _topological_order(tasks: list[Task]) -> list[Task]:
    """Kahn's algorithm, ties broken by declaration order — deterministic
    execution order for a given workflow preset."""
    declaration_order = [t.id for t in tasks]
    remaining = {t.id: t for t in tasks}
    ordered: list[Task] = []

    while remaining:
        ready_ids = [
            tid
            for tid in declaration_order
            if tid in remaining and all(dep not in remaining for dep in remaining[tid].depends_on)
        ]
        if not ready_ids:
            raise ConfigError(f"Cycle detected in workflow dependencies among: {', '.join(sorted(remaining))}")
        for tid in ready_ids:
            ordered.append(remaining.pop(tid))

    return ordered


def _read_workflow_data(engine_root: Path, mode: str) -> dict:
    from core.orchestrator import platform_config as pc

    path = pc.workflow_preset_path(engine_root, mode)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_workflow(engine_root: Path, mode: str = "standard") -> list[Task]:
    data = _read_workflow_data(engine_root, mode)
    tasks = _parse_tasks(data)
    return _topological_order(tasks)


def plan(engine_root: Path, platform_config: "PlatformConfig | None" = None) -> Plan:
    """`platform_config` defaults to a fresh load when not given (a standalone
    caller/test), but `supervisor.run()` loads one instance and passes it so
    the workflow mode and scalars agree with everything else the run reads."""
    from core.orchestrator import platform_config as pc

    if platform_config is None:
        platform_config = pc.load(engine_root)

    data = _read_workflow_data(engine_root, platform_config.workflow_mode)
    tasks = _topological_order(_parse_tasks(data))
    return Plan(
        tasks=tasks,
        max_parallel=platform_config.max_parallel,
        decompose=platform_config.decompose,
        max_correction_attempts=platform_config.max_correction_attempts,
    )


def prune(plan: Plan, keep_ids: set[str]) -> Plan:
    """Keeps only the tasks in keep_ids. A dependency on a pruned-out task is
    *bridged* to that task's own dependencies (recursively), not dropped
    outright — pruning `security` from `tests -> security -> documentation`
    must leave `documentation` depending on `tests`, not on nothing. Dropping
    the edge instead of bridging it would silently let `documentation` start
    before `tests` even ran, with no visibility into what was actually built
    (found via a real decomposition that pruned exactly this way — the
    resulting docs described the feature as unbuilt while `backend`, running
    concurrently and invisibly to it, was implementing it).

    Safe by construction: a subgraph of an already-cycle-free,
    already-reference-checked DAG is still cycle-free, so the recursion
    below always terminates without needing to re-run cycle detection.
    """
    by_id = {task.id: task for task in plan.tasks}

    def bridged_deps(task_id: str, seen: set[str]) -> set[str]:
        result: set[str] = set()
        for dep in by_id[task_id].depends_on:
            if dep in seen:
                continue
            seen.add(dep)
            if dep in keep_ids:
                result.add(dep)
            else:
                result |= bridged_deps(dep, seen)
        return result

    pruned_tasks = [
        Task(id=task.id, agent=task.agent, depends_on=sorted(bridged_deps(task.id, set())))
        for task in plan.tasks
        if task.id in keep_ids
    ]
    return Plan(
        tasks=pruned_tasks,
        max_parallel=plan.max_parallel,
        decompose=plan.decompose,
        max_correction_attempts=plan.max_correction_attempts,
    )
