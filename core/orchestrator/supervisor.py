"""Supervisor: runs the task DAG concurrently and prints a staged report.

Each task runs in its own git worktree (core.orchestrator.git_ops), so
independent tasks (e.g. backend/frontend, both only depending on
architecture) can run at the same time without the claude_code provider's
concurrent CLI processes colliding on the same working tree. Only the merge
of a finished task's worktree branch back into the shared run branch
(hermes/<slug>) is serialized on the main thread — that's cheap; the
expensive part (the actual provider call) runs concurrently, up to
`Plan.max_parallel` tasks at once.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

import git
from rich.console import Console

from core.context.manager import ContextManager, SelectedContext
from core.orchestrator import contracts, decomposer, git_ops, planner, review, scheduler, test_runner
from core.orchestrator.scheduler import StageResult
from core.telemetry import store as telemetry
from providers.base import ProviderResult, display_name

console = Console()


@dataclass
class StageReport:
    id: str
    agent: str
    status: str  # "done" | "failed" | "skipped" | "violated" | "conflict"
    summary: str
    files_changed: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    branch: str
    stages: list[StageReport]
    files_changed: list[str]
    tests_passed: bool
    tests_output: str
    review_passed: bool | None
    review_summary: str
    summary: str
    totals: dict = field(default_factory=dict)
    """Rolled-up cost/tokens for the run (see core.telemetry.store.run_totals).
    Empty on a dry run, which records nothing."""


def format_totals(totals: dict) -> str:
    """One-line cost summary.

    Reports how many calls were actually priced when that differs from the
    call count: a provider that returns no cost (anthropic_api) would
    otherwise make a run look cheaper than it was.
    """
    if not totals:
        return "not recorded"
    calls = totals.get("calls", 0)
    priced = totals.get("priced_calls", 0)
    priced_note = f" ({priced}/{calls} priced)" if priced != calls else ""
    return (
        f"${totals.get('cost_usd', 0):.4f}{priced_note} · "
        f"{totals.get('input_tokens', 0):,} in / {totals.get('output_tokens', 0):,} out · "
        f"{calls} calls"
    )


def _run_stage_in_worktree(
    repo_root: Path,
    branch: str,
    task: planner.Task,
    request: str,
    context: SelectedContext,
    completed_snapshot: list[StageResult],
    recorder: telemetry.RunRecorder | None = None,
) -> tuple[StageResult, Path | None, str | None]:
    """Runs entirely inside a worker thread — touches nothing shared: its
    own worktree, its own git.Repo instance. Never merges or removes the
    worktree itself; that happens back on the main thread, serialized, once
    this returns.

    `completed_snapshot` is a copy of the stages finished so far, taken at
    dispatch time — not a live reference. Two siblings dispatched together
    correctly only see the same (smaller) upstream set, since neither knows
    about the other's still-in-progress work; and it avoids a data race
    with the main thread, which keeps appending to the real list while
    other tasks are still in flight.
    """
    try:
        worktree_path, task_branch = git_ops.create_worktree(git.Repo(repo_root), branch, task.id)
    except git.GitCommandError as exc:
        failure = ProviderResult(success=False, summary=f"worktree setup failed: {exc}")
        return StageResult(task=task, status="failed", result=failure), None, None

    provider_name = scheduler.resolve_provider(repo_root, task.agent)
    console.print(f"[bold]{task.id}[/bold] ({display_name(provider_name)})...")

    description = scheduler.build_stage_description(request, completed_snapshot)
    result = scheduler.run_task(
        worktree_path,
        task.agent,
        description,
        context.context_paths(),
        context.render(),
        recorder=recorder,
        stage_id=task.id,
    )

    worktree_repo = git.Repo(worktree_path)
    changed = git_ops.commit_all(worktree_repo, f"{task.id}: {result.summary or request}")

    status = "done" if result.success else "failed"
    bad_files: list[str] = []
    if result.success:
        bad_files = contracts.violations(task.agent, changed)
        if bad_files:
            status = "violated"

    if status == "failed":
        console.print(f"[bold red]{task.id} failed[/bold red]: {result.summary}")
    elif status == "violated":
        console.print(
            f"[bold red]{task.id} violated its contract[/bold red]: touched "
            f"{', '.join(bad_files)} — outside its declared scope"
        )

    return StageResult(task=task, status=status, result=result, files_changed=changed), worktree_path, task_branch


def run(repo_root: Path, request: str, dry_run: bool = False, session_id: str | None = None) -> RunReport:
    repo = git.Repo(repo_root)
    if not dry_run:
        git_ops.ensure_clean_worktree(repo)
        git_ops.prune_worktrees(repo)

    console.rule("Hermes")

    workflow = planner.plan(repo_root)
    console.print(f"[bold]Plan generated[/bold]: {len(workflow.tasks)} tasks (up to {workflow.max_parallel} in parallel)")

    context_manager = ContextManager(repo_root)
    context_manager.index_repo()
    context = context_manager.select_context(request)
    console.print(f"[bold]Context selected:[/bold] {len(context.context_paths())} files")

    # Created before the decomposer call, which is itself a billable provider
    # call — starting the recorder any later would understate every run. The
    # config snapshot and engine commit are captured now because neither can
    # be reconstructed from a past row: they're what makes runs comparable
    # across engine versions and across config changes.
    recorder = None
    if not dry_run:
        recorder = telemetry.RunRecorder(
            repo_root,
            request,
            session_id=session_id,
            engine_commit=git_ops.current_commit(repo),
            metadata={
                "use_graph": context_manager.config.use_graph,
                "use_vector_db": context_manager.config.use_vector_db,
                "use_git_diff": context_manager.config.use_git_diff,
                "use_memory": context_manager.config.use_memory,
                "max_files": context_manager.config.max_files,
                "decompose": workflow.decompose,
                "max_parallel": workflow.max_parallel,
            },
        )

    if workflow.decompose:
        known_ids = [t.id for t in workflow.tasks]
        decomposer_result = scheduler.run_task(
            repo_root,
            "decomposer",
            decomposer.build_description(request, known_ids),
            context.context_paths(),
            context.render(),
            recorder=recorder,
        )
        chosen = decomposer.parse_tasks(decomposer_result.summary, known_ids) if decomposer_result.success else None
        if chosen is None:
            console.print("[bold yellow]Decomposition unavailable[/bold yellow] — running the full workflow")
        else:
            workflow = planner.prune(workflow, set(chosen))
            dropped = sorted(set(known_ids) - set(chosen))
            selected = ", ".join(t.id for t in workflow.tasks)
            dropped_note = f" ({', '.join(dropped)} not needed)" if dropped else ""
            console.print(f"[bold]Decomposed to:[/bold] {selected}{dropped_note}")

    if dry_run:
        console.print("[bold]Dry run[/bold] — no agent will be invoked")
        console.print("[bold]Planned workflow:[/bold]")
        for task in workflow.tasks:
            deps = ", ".join(task.depends_on) if task.depends_on else "none"
            console.print(f"  - {task.id} ({task.agent}) depends_on: {deps}")
        return RunReport(
            branch="",
            stages=[],
            files_changed=[],
            tests_passed=False,
            tests_output="",
            review_passed=None,
            review_summary="",
            summary="dry-run",
        )

    base_sha = git_ops.current_commit(repo)
    branch = git_ops.create_branch(repo, request)

    remaining = {t.id: t for t in workflow.tasks}
    completed: list[StageResult] = []
    completed_ids: set[str] = set()
    blocked_ids: set[str] = set()
    stage_reports: list[StageReport] = []
    all_files_changed: list[str] = []

    with ThreadPoolExecutor(max_workers=workflow.max_parallel) as executor:
        in_flight: dict[Future, planner.Task] = {}

        def _dispatch_ready() -> None:
            for task_id in list(remaining):
                task = remaining[task_id]
                pending_deps = [d for d in task.depends_on if d not in completed_ids and d not in blocked_ids]
                if pending_deps:
                    continue  # still waiting on something not yet resolved

                blocked_deps = [d for d in task.depends_on if d in blocked_ids]
                if blocked_deps:
                    console.print(
                        f"[bold yellow]{task.id}[/bold yellow]: skipped (upstream dependency didn't complete)"
                    )
                    stage_reports.append(StageReport(id=task.id, agent=task.agent, status="skipped", summary=""))
                    blocked_ids.add(task.id)
                    del remaining[task_id]
                    continue

                if len(in_flight) >= workflow.max_parallel:
                    continue  # ready, but no free worker slot right now -- retried next pass

                del remaining[task_id]
                snapshot = list(completed)
                future = executor.submit(
                    _run_stage_in_worktree, repo_root, branch, task, request, context, snapshot, recorder
                )
                in_flight[future] = task

        _dispatch_ready()
        while in_flight:
            done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                task = in_flight.pop(future)
                stage_result, worktree_path, task_branch = future.result()

                if stage_result.status == "done":
                    merged = git_ops.merge_worktree(repo, task_branch)
                    if merged:
                        git_ops.remove_worktree(repo, worktree_path)
                        repo.git.branch("-D", task_branch)
                        console.print(
                            f"[bold]{task.id}[/bold]: {git_ops.format_changed_files(stage_result.files_changed)}"
                        )
                        completed.append(stage_result)
                        completed_ids.add(task.id)
                        all_files_changed.extend(stage_result.files_changed)
                    else:
                        stage_result.status = "conflict"
                        blocked_ids.add(task.id)
                        console.print(
                            f"[bold red]{task.id}: merge conflict[/bold red] — resolve manually in "
                            f"{worktree_path} (branch {task_branch})"
                        )
                else:
                    blocked_ids.add(task.id)
                    if worktree_path is not None:
                        git_ops.remove_worktree(repo, worktree_path)

                stage_reports.append(
                    StageReport(
                        id=task.id,
                        agent=task.agent,
                        status=stage_result.status,
                        summary=stage_result.result.summary if stage_result.result else "",
                        files_changed=stage_result.files_changed,
                    )
                )

            _dispatch_ready()

    any_stage_incomplete = any(s.status != "done" for s in stage_reports)

    test_result = test_runner.run_tests(repo_root)
    console.print(f"[bold]Tests:[/bold] {'PASS' if test_result.passed else 'FAIL'}")
    if test_result.output:
        console.print(test_result.output)

    diff = git_ops.diff_since(repo, base_sha)
    review_result = scheduler.run_task(
        repo_root, "reviewer", review.build_description(request, diff), recorder=recorder
    )
    review_passed = review.parse_verdict(review_result.summary) if review_result.success else None
    review_label = {True: "PASS", False: "FAIL", None: "UNKNOWN"}[review_passed]
    console.print(f"[bold]Review:[/bold] {review_label}")
    if review_result.summary:
        console.print(review_result.summary)

    overall_ok = not any_stage_incomplete and test_result.passed and review_passed is True
    summary = "done" if overall_ok else "needs attention"

    totals: dict = {}
    if recorder is not None:
        recorder.finish(branch=branch, summary=summary)
        totals = telemetry.run_totals(repo_root, recorder.run_id)
        console.print(f"[bold]Cost:[/bold] {format_totals(totals)}")

    console.print(f"[bold]Summary:[/bold] {summary}")

    return RunReport(
        branch=branch,
        stages=stage_reports,
        files_changed=all_files_changed,
        tests_passed=test_result.passed,
        tests_output=test_result.output,
        review_passed=review_passed,
        review_summary=review_result.summary,
        summary=summary,
        totals=totals,
    )
