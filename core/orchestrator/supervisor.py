"""Supervisor (v1): creates the branch, runs the provider, commits, tests, reports.

The branch must be created BEFORE the provider runs: a CLI provider
(claude_code) edits files directly on disk while it runs, so branch
isolation only makes sense if it happens before the call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import git
from rich.console import Console

from core.orchestrator import git_ops, test_runner
from providers.base import ProviderResult

console = Console()


@dataclass
class RunReport:
    branch: str
    provider_success: bool
    files_changed: list[str]
    tests_passed: bool
    tests_output: str


def apply_and_verify(
    repo_root: Path, request: str, run_provider: Callable[[], ProviderResult]
) -> RunReport:
    repo = git.Repo(repo_root)
    git_ops.ensure_clean_worktree(repo)
    branch = git_ops.create_branch(repo, request)

    console.rule("Hermes — run report")
    console.print(f"Branch created: [bold]{branch}[/bold]")

    result = run_provider()

    if not result.success:
        console.print(f"[bold red]Provider failed[/bold red]: {result.summary}")
        return RunReport(
            branch=branch, provider_success=False, files_changed=[], tests_passed=False, tests_output=""
        )

    console.print(f"Summary: {result.summary}")

    changed = git_ops.commit_all(repo, result.summary or request)
    console.print("Files changed:")
    if changed:
        for path in changed:
            console.print(f"  - {path}")
    else:
        console.print("  (none)")

    test_result = test_runner.run_tests(repo_root)
    status = "[bold green]PASS[/bold green]" if test_result.passed else "[bold red]FAIL[/bold red]"
    console.print(f"Tests: {status}")
    if test_result.output:
        console.print(test_result.output)

    return RunReport(
        branch=branch,
        provider_success=True,
        files_changed=changed,
        tests_passed=test_result.passed,
        tests_output=test_result.output,
    )
