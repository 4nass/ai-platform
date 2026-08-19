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
from typing import TYPE_CHECKING, Literal

from core import untrusted
from core.context import selection
from core.context.manager import FULL, POINTERS, SelectedContext
from core.jobs import budget, store
from core.orchestrator import router
from core.orchestrator.planner import Task
from providers.anthropic_api import adapter as anthropic_api
from providers.base import AgentTask, ProviderResult, reads_files
from providers.claude_code import adapter as claude_code
from providers.codex_cli import adapter as codex_cli
from providers.openai_api import adapter as openai_api

if TYPE_CHECKING:
    from core.orchestrator.platform_config import PlatformConfig

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
    engine_root: Path,
    agent: str,
    complexity: str = router.DEFAULT_COMPLEXITY,
    *,
    platform_config: "PlatformConfig | None" = None,
) -> str:
    """The provider this role should use right now, per the router.

    Was a static config lookup; it is now a decision. Callers that only need
    the name keep this signature — `route_agent` returns the reasoning too.
    """
    return route_agent(engine_root, agent, complexity, platform_config=platform_config).provider


def route_agent(
    engine_root: Path,
    agent: str,
    complexity: str = router.DEFAULT_COMPLEXITY,
    *,
    platform_config: "PlatformConfig | None" = None,
) -> router.Decision:
    return router.route(
        engine_root,
        agent,
        known_providers=set(PROVIDERS),
        platform_config=platform_config,
        complexity=complexity,
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
    platform_config: "PlatformConfig | None" = None,
    budget_limits=None,
    run_key: str = "",
    cancel_event=None,
) -> ProviderResult:
    """Runs one task through its configured provider, recording what it cost.

    Every provider call in the system funnels through here — DAG stages, the
    decomposer, and the reviewer — so instrumenting this one function means a
    future call site is measured automatically instead of being silently free.

    It is also where the budget gate lives (issue #27), for exactly that
    reason. `PROVIDERS[...]` is dispatched on one line of this function and
    nowhere else in the engine, so "no adapter can bypass the common budget
    gate" is a structural property rather than a convention every adapter has
    to remember: an adapter cannot be reached without passing it. Capacity is
    reserved *before* dispatch and settled with the real figure afterwards —
    including for a failed call, which spent its tokens regardless.

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

    `platform_config` defaults to a fresh load when not given; `supervisor.run()`
    loads one instance and passes it to every call so a whole run is judged
    against the same policy snapshot.
    """
    engine_root = engine_root or repo_root
    decision = route_agent(engine_root, agent, complexity, platform_config=platform_config)
    provider_name = decision.provider
    provider = PROVIDERS[provider_name]

    provider_reads_files = reads_files(provider)
    rendered = context.render_for(reads_files=provider_reads_files) if context else None
    context_paths = context.context_paths() if context else []

    if cancel_event is not None and cancel_event.is_set():
        raise store.CancellationRequested("run cancellation requested")

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
        cancel_event=cancel_event,
    )

    # --- the budget gate. Nothing below reaches a provider without it. ---
    limits = budget_limits if budget_limits is not None else budget.Limits()
    reservation, estimated = None, 0
    if limits.declared and (key := run_key or _run_key(recorder)):
        mode = platform_config.budget_mode if platform_config is not None else budget.SOFT
        estimated = budget.estimate_tokens(description, agent_task.context_render)
        # `admission`, not `decision`: the router's decision is still live here
        # and is read further down for model, effort and routing reason.
        admission = budget.admit(
            engine_root, limits, run_key=key, estimated=estimated, mode=mode
        )
        if not admission.allowed:
            raise budget.BudgetExceeded(admission)
        reservation = budget.reserve(
            engine_root,
            run_key=key,
            estimated=estimated,
            stage=stage_id or "",
            agent=agent,
            provider=provider_name,
        )

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        result = provider.run(agent_task)
    except BaseException as exc:
        if cancel_event is not None and cancel_event.is_set():
            if reservation is not None:
                budget.release(engine_root, reservation)
            raise store.CancellationRequested("run cancellation requested") from exc
        raise
    if cancel_event is not None and cancel_event.is_set():
        if reservation is not None:
            budget.settle(engine_root, reservation, _spent(result, estimated))
        raise store.CancellationRequested("run cancellation requested")
    duration_ms = int((time.monotonic() - started) * 1000)

    if reservation is not None:
        # Settled, not released, even when the call failed: a provider that
        # errored after processing a 200k-token prompt spent those tokens, and
        # making failures free is precisely backwards for a loop that retries
        # them.
        budget.settle(engine_root, reservation, _spent(result, estimated))

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


def _run_key(recorder) -> str:
    """What reservations for this run are grouped under.

    Derived from the telemetry run id when there is one, so budget and history
    describe the same run. A caller with no recorder (a dry run, most tests)
    gets no key and therefore no reservations — correct, because it also makes
    no provider calls to pay for.
    """
    run_id = getattr(recorder, "run_id", None)
    return f"run-{run_id}" if run_id else ""


def _spent(result: ProviderResult, estimated: int) -> int:
    """What a call actually cost, or the estimate when the provider said
    nothing.

    Falling back to the estimate rather than to zero: a provider that reports
    no usage has not established that it was free, and a budget that treats
    silence as free can be emptied by whichever adapter happens to be quiet.
    """
    usage = result.usage
    if usage is None:
        return estimated
    total = (
        (usage.input_tokens or 0)
        + (usage.output_tokens or 0)
        + (usage.cache_read_tokens or 0)
        + (usage.cache_creation_tokens or 0)
    )
    return total or estimated


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
    edits — see prompts/security.md) still contributes its summary text.

    An upstream summary is model output, so it is *literally* one agent
    writing the next agent's prompt (issue #5). Each is wrapped and its
    control lines defanged (core.untrusted) — mechanical for the control
    lines, advisory for the rest; see that module on why the distinction
    matters and what this does not cover.

    The file list stays outside the wrapper on purpose: those paths come
    from `git_ops.commit_all`, i.e. from git, not from the model.
    """
    completed = [stage for stage in upstream if stage.status == "done"]
    if not completed:
        return request

    lines = [request, "", "Upstream artifacts from earlier stages in this workflow:"]
    for stage in completed:
        summary = stage.result.summary if stage.result else ""
        files = ", ".join(stage.files_changed) if stage.files_changed else "no files changed"
        lines.append(f"- {stage.task.id} ({stage.task.agent}) changed: {files}")
        if summary:
            lines.append(
                untrusted.wrap(summary, source=f"the {stage.task.agent} agent", kind="summary")
            )
    lines += ["", untrusted.DATA_NOT_INSTRUCTIONS]
    return "\n".join(lines)
