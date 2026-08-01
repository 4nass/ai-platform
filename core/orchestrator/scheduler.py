"""Scheduler: resolves each task's provider and dispatches its run.

Concurrency itself lives in supervisor.py (per-task git worktrees, a
ThreadPoolExecutor-driven ready queue) — this module only knows how to run
one task in whatever `repo_root` it's given (the shared repo, or a task's
own isolated worktree), and how to build its prompt from upstream artifacts.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core.context import selection
from core.context.manager import FULL, POINTERS, SelectedContext
from core.orchestrator import router
from core.orchestrator.planner import Task
from providers.anthropic_api import adapter as anthropic_api
from providers.base import AgentTask, ProviderResult, reads_files
from providers.claude_code import adapter as claude_code
from providers.codex_cli import adapter as codex_cli
from providers.openai_api import adapter as openai_api

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


def resolve_provider(
    engine_root: Path, agent: str, complexity: str = router.DEFAULT_COMPLEXITY
) -> str:
    """The provider this role should use right now, per the router.

    Was a static config lookup; it is now a decision. Callers that only need
    the name keep this signature — `route_agent` returns the reasoning too.
    """
    return route_agent(engine_root, agent, complexity).provider


def route_agent(
    engine_root: Path, agent: str, complexity: str = router.DEFAULT_COMPLEXITY
) -> router.Decision:
    return router.route(
        engine_root, agent, known_providers=set(PROVIDERS), complexity=complexity
    )


def run_task(
    repo_root: Path,
    agent: str,
    description: str,
    context: SelectedContext | None = None,
    *,
    recorder=None,
    stage_id: str | None = None,
    engine_root: Path | None = None,
    complexity: str = router.DEFAULT_COMPLEXITY,
) -> ProviderResult:
    """Runs one task through its configured provider, recording what it cost.

    Every provider call in the system funnels through here — DAG stages, the
    decomposer, and the reviewer — so instrumenting this one function means a
    future call site is measured automatically instead of being silently free.

    The context arrives unrendered on purpose. Which rendering a provider gets
    depends on whether it can read the repo itself, and that isn't known until
    the router has picked one — so rendering happens here rather than at the
    call site, and the char count recorded is what was really sent instead of
    the length of a string the provider may have ignored.

    `repo_root` is the target — the repo being modified, or one of its task
    worktrees. `engine_root` is the ai-platform install: where prompts/config
    live and where the shared telemetry/routing state is read and written.
    They coincide when the engine operates on itself and by default here (so
    existing single-repo callers/tests keep working unchanged), but must not
    be conflated in general — routing decisions and recorded history are
    engine-scoped, not per-worktree, so pointing them at a throwaway worktree
    would have every stage cold-start from an empty, momentary copy instead
    of the engine's real, persistent one.
    """
    engine_root = engine_root or repo_root
    decision = route_agent(engine_root, agent, complexity)
    provider_name = decision.provider
    provider = PROVIDERS[provider_name]

    provider_reads_files = reads_files(provider)
    rendered = context.render_for(reads_files=provider_reads_files) if context else None
    context_paths = context.context_paths() if context else []

    agent_task = AgentTask(
        agent=agent,
        description=description,
        repo_root=repo_root,
        engine_root=engine_root,
        model=decision.model,
        reasoning_effort=decision.reasoning_effort,
        complexity=complexity,
        context_paths=context_paths,
        context_render=rendered.text if rendered else "",
    )

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    result = provider.run(agent_task)
    duration_ms = int((time.monotonic() - started) * 1000)

    if recorder is not None:
        recorder.record_call(
            agent=agent,
            provider=provider_name,
            model=decision.model,
            reasoning_effort=decision.reasoning_effort,
            result=result,
            stage_id=stage_id,
            # What this call actually received — not what was selected. The
            # character budget is applied per provider shape, so the two can
            # legitimately differ inside one run.
            context_files=rendered.files if rendered else 0,
            context_chars=len(rendered.text) if rendered else 0,
            duration_ms=duration_ms,
            started_at=started_at,
            routing_reason=decision.reason,
            context_reason=_context_reason(context, rendered),
            # Per call, not per run: two providers in the same run can get
            # different renderings, so the run-level config snapshot alone
            # can't tell you what a given call was actually sent.
            metadata=_call_metadata(context, provider_reads_files, result, complexity),
        )
    return result


def _context_reason(context: SelectedContext | None, rendered) -> str:
    """The decision log for this call, as JSON.

    Stored per call rather than per run so a `calls` row answers "why these
    files?" on its own, without a join. Kept to the survivors plus dropped
    counts by rule — the full per-candidate detail is one `ai-platform
    context` away and doesn't need to live in every row.
    """
    if context is None:
        return ""
    reason = selection.summarize(context.decisions)
    if rendered is not None and rendered.dropped:
        reason["dropped"] = {**reason["dropped"], "max_context_chars": rendered.dropped}
    return json.dumps(reason)


MAX_ERROR_CHARS = 2000


def _call_metadata(
    context: SelectedContext | None,
    provider_reads_files: bool,
    result: ProviderResult,
    complexity: str,
) -> dict:
    """What this call needs recorded beyond its numbers.

    Extends via the metadata JSON rather than a new column per evolution —
    SQLite's json_extract keeps it queryable, so extensibility costs nothing
    analytically.

    A failure carries its provider message. Without it the table records that
    something failed and nothing about why, which is the difference between
    measuring a failure rate and being able to act on one. Truncated so a
    runaway stderr can't bloat the row.
    """
    metadata: dict = {
        "injection": _injection_label(context, provider_reads_files),
        "complexity": complexity,
    }
    if result.usage is not None and result.usage.model:
        metadata["effective_model"] = result.usage.model
    if not result.success and result.summary:
        metadata["error"] = result.summary[:MAX_ERROR_CHARS]
    return metadata


def _injection_label(context: SelectedContext | None, provider_reads_files: bool) -> str:
    if context is None:
        return "none"
    return POINTERS if (provider_reads_files and context.injection_mode == POINTERS) else FULL


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
