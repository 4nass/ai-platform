"""Scheduler (v1): synchronous execution of the single task via a provider.

No parallelism yet — that's reserved for the full version.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from core.context.manager import SelectedContext
from core.orchestrator.planner import Task
from providers.anthropic_api import adapter as anthropic_api
from providers.base import AgentTask, ProviderResult
from providers.claude_code import adapter as claude_code
from providers.codex_cli import adapter as codex_cli
from providers.openai_api import adapter as openai_api

AGENTS_CONFIG_PATH = Path("config/agents.yaml")
DEFAULT_PROVIDER = "claude_code"

PROVIDERS = {
    "claude_code": claude_code,
    "codex_cli": codex_cli,
    "anthropic_api": anthropic_api,
    "openai_api": openai_api,
}


def _resolve_provider(repo_root: Path, agent: str) -> str:
    config = yaml.safe_load((repo_root / AGENTS_CONFIG_PATH).read_text(encoding="utf-8")) or {}
    return (config.get(agent) or {}).get("provider", DEFAULT_PROVIDER)


def execute(repo_root: Path, tasks: list[Task], context: SelectedContext, agent: str) -> ProviderResult:
    task = tasks[0]
    provider_name = _resolve_provider(repo_root, agent)
    provider = PROVIDERS[provider_name]

    agent_task = AgentTask(
        agent=agent,
        description=task.request,
        repo_root=repo_root,
        context_paths=context.context_paths(),
        context_render=context.render(),
    )
    return provider.run(agent_task)
