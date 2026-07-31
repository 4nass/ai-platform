"""Supervisor: walks the task DAG and prints a staged report.

The branch must be created BEFORE any task's provider runs: a CLI provider
(claude_code) edits files directly on disk while it runs, so branch
isolation only makes sense if it happens before the first call.

Each stage is committed right after it runs — success or failure — not just
the successful ones: a failed stage can still have partially edited files on
disk (the CLI can write before it errors out), and leaving those uncommitted
would let them bleed into a sibling stage's commit (e.g. `backend` fails
mid-edit, `frontend` — which only depends on `architecture`, not `backend` —
still runs next and would otherwise sweep up backend's stray edits via
`git add -A`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import git
from rich.console import Console

from core.context.manager import ContextManager, SelectedContext
from core.orchestrator import contracts, git_ops, planner, review, scheduler, test_runner
from core.orchestrator.scheduler import StageResult
from providers.base import display_name

console = Console()


@dataclass
class StageReport:
    id: str
    agent: str
    status: str  # "done" | "failed" | "skipped" | "violated"
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


def _run_stage(
    repo: git.Repo,
    repo_root: Path,
    task: planner.Task,
    request: str,
    context: SelectedContext,
    completed: list[StageResult],
) -> StageResult:
    provider_name = scheduler.resolve_provider(repo_root, task.agent)
    console.print(f"[bold]{task.id}[/bold] ({display_name(provider_name)})...")

    description = scheduler.build_stage_description(request, completed)
    result = scheduler.run_task(repo_root, task.agent, description, context.context_paths(), context.render())

    status = "done" if result.success else "failed"
    changed = git_ops.commit_all(repo, f"{task.id}: {result.summary or request}")

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
    else:
        console.print(f"[bold]{task.id}[/bold]: {git_ops.format_changed_files(changed)}")

    return StageResult(task=task, status=status, result=result, files_changed=changed)


def run(repo_root: Path, request: str) -> RunReport:
    repo = git.Repo(repo_root)
    git_ops.ensure_clean_worktree(repo)

    console.rule("Hermes")

    workflow = planner.plan(repo_root)
    console.print(f"[bold]Plan generated[/bold]: {len(workflow.tasks)} tasks")

    context_manager = ContextManager(repo_root)
    context_manager.index_repo()
    context = context_manager.select_context(request)
    console.print(f"[bold]Context selected:[/bold] {len(context.context_paths())} files")

    base_sha = git_ops.current_commit(repo)
    branch = git_ops.create_branch(repo, request)

    completed: list[StageResult] = []
    completed_ids: set[str] = set()
    stage_reports: list[StageReport] = []
    all_files_changed: list[str] = []

    for task in workflow.tasks:
        missing_deps = [dep for dep in task.depends_on if dep not in completed_ids]
        if missing_deps:
            console.print(f"[bold yellow]{task.id}[/bold yellow]: skipped (upstream dependency didn't complete)")
            stage_reports.append(StageReport(id=task.id, agent=task.agent, status="skipped", summary=""))
            continue

        stage_result = _run_stage(repo, repo_root, task, request, context, completed)
        completed.append(stage_result)
        stage_reports.append(
            StageReport(
                id=task.id,
                agent=task.agent,
                status=stage_result.status,
                summary=stage_result.result.summary if stage_result.result else "",
                files_changed=stage_result.files_changed,
            )
        )
        if stage_result.status == "done":
            completed_ids.add(task.id)
            all_files_changed.extend(stage_result.files_changed)

    any_stage_incomplete = any(s.status != "done" for s in stage_reports)

    test_result = test_runner.run_tests(repo_root)
    console.print(f"[bold]Tests:[/bold] {'PASS' if test_result.passed else 'FAIL'}")
    if test_result.output:
        console.print(test_result.output)

    diff = git_ops.diff_since(repo, base_sha)
    review_result = scheduler.run_task(repo_root, "reviewer", review.build_description(request, diff))
    review_passed = review.parse_verdict(review_result.summary) if review_result.success else None
    review_label = {True: "PASS", False: "FAIL", None: "UNKNOWN"}[review_passed]
    console.print(f"[bold]Review:[/bold] {review_label}")
    if review_result.summary:
        console.print(review_result.summary)

    overall_ok = not any_stage_incomplete and test_result.passed and review_passed is True
    summary = "done" if overall_ok else "needs attention"
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
    )
