"""Scheduler: resolves each task's provider and dispatches its run.

Concurrency itself lives in supervisor.py (per-task git worktrees, a
ThreadPoolExecutor-driven ready queue) — this module only knows how to run
one task in whatever `repo_root` it's given (the shared repo, or a task's
own isolated worktree), and how to build its prompt from upstream artifacts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    status: Literal["done", "failed", "skipped", "violated", "conflict"]
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
    *,
    recorder=None,
    stage_id: str | None = None,
) -> ProviderResult:
    """Runs one task through its configured provider, recording what it cost.

    Every provider call in the system funnels through here — DAG stages, the
    decomposer, and the reviewer — so instrumenting this one function means a
    future call site is measured automatically instead of being silently free.

    `recorder` is passed in rather than built from `repo_root`: for DAG
    stages `repo_root` is the task's throwaway worktree, while the telemetry
    belongs to the main repo. Keyword-only and optional, so callers that
    don't care about telemetry (and every existing test) are unaffected.
    """
    provider_name = resolve_provider(repo_root, agent)
    provider = PROVIDERS[provider_name]

    agent_task = AgentTask(
        agent=agent,
        description=description,
        repo_root=repo_root,
        context_paths=context_paths or [],
        context_render=context_render,
    )

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    result = provider.run(agent_task)
    duration_ms = int((time.monotonic() - started) * 1000)

    if recorder is not None:
        recorder.record_call(
            agent=agent,
            provider=provider_name,
            result=result,
            stage_id=stage_id,
            context_files=len(context_paths or []),
            context_chars=len(context_render),
            duration_ms=duration_ms,
            started_at=started_at,
        )
    return result


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
