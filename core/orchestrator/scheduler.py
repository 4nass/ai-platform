"""Scheduler: resolves each task's provider and dispatches its run.

Still sequential — the DAG's `depends_on` only decides *ordering*
(supervisor.py walks the plan and skips a task if a dependency didn't
succeed). Real concurrent execution needs per-task git worktrees and is a
later phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from core.errors import ConfigError
from core.orchestrator.planner import Task
from providers.anthropic_api import adapter as anthropic_api
from providers.base import AgentTask, ProviderResult
from providers.claude_code import adapter as claude_code
from providers.codex_cli import adapter as codex_cli
from providers.openai_api import adapter as openai_api

AGENTS_CONFIG_PATH = Path("config/agents.yaml")

PROVIDERS = {
    "claude_code": claude_code,
    "codex_cli": codex_cli,
    "anthropic_api": anthropic_api,
    "openai_api": openai_api,
}


@dataclass
class StageResult:
    task: Task
    status: Literal["done", "failed", "skipped"]
    result: ProviderResult | None = None
    files_changed: list[str] = field(default_factory=list)


def resolve_provider(repo_root: Path, agent: str) -> str:
    config = yaml.safe_load((repo_root / AGENTS_CONFIG_PATH).read_text(encoding="utf-8")) or {}

    if agent not in config:
        known = ", ".join(sorted(config)) or "(none configured)"
        raise ConfigError(f"Unknown agent role '{agent}'. Configured roles: {known}")

    provider_name = (config[agent] or {}).get("provider")
    if not provider_name:
        raise ConfigError(f"Agent role '{agent}' has no 'provider' set in {AGENTS_CONFIG_PATH}")

    if provider_name not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise ConfigError(
            f"Unknown provider '{provider_name}' for agent '{agent}'. Available providers: {known}"
        )

    return provider_name


def run_task(
    repo_root: Path,
    agent: str,
    description: str,
    context_paths: list[str] | None = None,
    context_render: str = "",
) -> ProviderResult:
    provider_name = resolve_provider(repo_root, agent)
    provider = PROVIDERS[provider_name]

    agent_task = AgentTask(
        agent=agent,
        description=description,
        repo_root=repo_root,
        context_paths=context_paths or [],
        context_render=context_render,
    )
    return provider.run(agent_task)


def build_stage_description(request: str, upstream: list[StageResult]) -> str:
    """The request plus a recap of what earlier stages in the workflow
    already produced — the only way stages communicate (no direct agent-to-
    agent calls). A stage with no files changed (e.g. security, which never
    edits — see prompts/security.md) still contributes its summary text."""
    completed = [stage for stage in upstream if stage.status == "done"]
    if not completed:
        return request

    lines = [request, "", "Upstream artifacts from earlier stages in this workflow:"]
    for stage in completed:
        summary = stage.result.summary if stage.result else ""
        files = ", ".join(stage.files_changed) if stage.files_changed else "no files changed"
        lines.append(f"- {stage.task.id} ({stage.task.agent}): {summary}\n  files: {files}")
    return "\n".join(lines)
