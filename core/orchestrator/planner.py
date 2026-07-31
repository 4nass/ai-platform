"""Planner: builds the task DAG for a run.

No LLM involved yet — the DAG is a static template declared in
config/workflow.yaml, the same "declared, not inferred" pattern already used
for config/agents.yaml. Real per-request decomposition (a Task Decomposer
agent) is a later phase; for now every run gets the same dependency graph,
regardless of what the request says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.errors import ConfigError

WORKFLOW_CONFIG_PATH = Path("config/workflow.yaml")


@dataclass
class Task:
    id: str
    agent: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    tasks: list[Task]


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
    execution order for a given config/workflow.yaml."""
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


def load_workflow(repo_root: Path) -> list[Task]:
    path = repo_root / WORKFLOW_CONFIG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks = _parse_tasks(data)
    return _topological_order(tasks)


def plan(repo_root: Path) -> Plan:
    return Plan(tasks=load_workflow(repo_root))
