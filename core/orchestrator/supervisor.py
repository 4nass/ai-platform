"""Supervisor: owns the whole pipeline and prints a staged report.

The branch must be created BEFORE the task provider runs: a CLI provider
(claude_code) edits files directly on disk while it runs, so branch
isolation only makes sense if it happens before the call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import git
from rich.console import Console

from core.context.manager import ContextManager
from core.orchestrator import git_ops, planner, review, scheduler, test_runner
from providers.base import display_name

console = Console()


@dataclass
class RunReport:
    branch: str
    provider_success: bool
    files_changed: list[str]
    tests_passed: bool
    tests_output: str
    review_passed: bool | None
    review_summary: str
    summary: str


def run(repo_root: Path, request: str, agent: str) -> RunReport:
    repo = git.Repo(repo_root)
    git_ops.ensure_clean_worktree(repo)

    console.rule("Hermes")

    tasks = planner.plan(request)
    console.print("[bold]Plan generated[/bold]")

    context_manager = ContextManager(repo_root)
    context_manager.index_repo()
    context = context_manager.select_context(request)
    console.print(f"[bold]Context selected:[/bold] {len(context.context_paths())} files")

    provider_name = scheduler.resolve_provider(repo_root, agent)
    console.print(f"[bold]Agent:[/bold] {display_name(provider_name)}")

    base_sha = git_ops.current_commit(repo)
    branch = git_ops.create_branch(repo, request)

    result = scheduler.execute(repo_root, tasks, context, agent)
    if not result.success:
        console.print(f"[bold red]Agent failed[/bold red]: {result.summary}")
        return RunReport(
            branch=branch,
            provider_success=False,
            files_changed=[],
            tests_passed=False,
            tests_output="",
            review_passed=None,
            review_summary="",
            summary="needs attention",
        )

    changed = git_ops.commit_all(repo, result.summary or request)
    console.print(f"[bold]Changes:[/bold] {git_ops.format_changed_files(changed)}")

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

    overall_ok = test_result.passed and review_passed is True
    summary = "done" if overall_ok else "needs attention"
    console.print(f"[bold]Summary:[/bold] {summary}")

    return RunReport(
        branch=branch,
        provider_success=True,
        files_changed=changed,
        tests_passed=test_result.passed,
        tests_output=test_result.output,
        review_passed=review_passed,
        review_summary=review_result.summary,
        summary=summary,
    )
