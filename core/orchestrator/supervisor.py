"""Supervisor: runs the task DAG concurrently and prints a staged report.

Each task runs in its own git worktree (core.orchestrator.git_ops), so
independent tasks (e.g. backend/frontend, both only depending on
architecture) can run at the same time without the claude_code provider's
concurrent CLI processes colliding on the same working tree. Only the merge
of a finished task's worktree branch back into the shared run branch
(engine/<slug>) is serialized on the main thread — that's cheap; the
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
from core.orchestrator import (
    contracts,
    correction,
    decomposer,
    git_ops,
    planner,
    review,
    router,
    scheduler,
    test_runner,
)
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
    correction_attempts: int = 0
    """How many test/review-failure -> corrector -> re-check passes actually
    ran (see planner.Plan.max_correction_attempts). 0 means either nothing
    failed, or nothing failed *and had files to fix* — see run()."""


def format_totals(totals: dict) -> str:
    """One-line usage summary — tokens first, price last and optional.

    Both providers are flat-rate subscriptions, so a dollar figure is not
    something the subscriber can act on; tokens are what consume quota. Cost
    is still shown when a provider volunteered one (claude_code does, codex
    doesn't), because measured data is worth keeping — but it no longer leads,
    and no provider is required to supply it.

    The token sum has to stay right in both directions, and each direction
    cost a real bug:

    - `input_tokens` alone is only the *uncached remainder*, so reporting it
      on its own showed "28 in" for a run that processed 600k tokens. Report
      the sum, and break out the cached share.
    - That sum is only correct because every adapter normalizes into the
      convention in providers.base.TokenUsage. Codex reports the cached
      portion *inside* its input count; passing that through would have
      inflated a 14k-token prompt to 27k here.
    """
    if not totals:
        return "not recorded"
    calls = totals.get("calls", 0)

    cached = totals.get("cache_read_tokens", 0)
    total_in = totals.get("input_tokens", 0) + cached + totals.get("cache_creation_tokens", 0)
    cached_note = f" ({cached:,} cached)" if cached else ""

    line = (
        f"{total_in:,} in{cached_note} / {totals.get('output_tokens', 0):,} out · {calls} calls"
    )

    priced = totals.get("priced_calls", 0)
    if priced:
        scope = "" if priced == calls else f" for {priced}/{calls}"
        line += f" · ${totals.get('cost_usd', 0):.4f}{scope}"
    return line


def _print_test_result(result: test_runner.TestResult) -> None:
    label = "SKIPPED" if result.skipped else ("PASS" if result.passed else "FAIL")
    sandbox_note = "" if result.sandboxed or result.skipped else " (unsandboxed)"
    console.print(f"[bold]Tests:[/bold] {label}{sandbox_note}")
    if result.sandbox_warning:
        console.print(f"[bold yellow]{result.sandbox_warning}[/bold yellow]")
    if result.output:
        console.print(result.output)


def _run_stage_in_worktree(
    target_root: Path,
    engine_root: Path,
    branch: str,
    task: planner.Task,
    request: str,
    context: SelectedContext,
    completed_snapshot: list[StageResult],
    complexity: str = router.DEFAULT_COMPLEXITY,
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

    **Never raises** (issue #1). Anything escaping here surfaces on the main
    thread at `future.result()`, which kills the entire run *and* strands the
    worktree this function created — one misconfigured role took down every
    other stage, and left a directory in /tmp nothing would ever reclaim. So
    the whole body is guarded and an unexpected error is reported the same
    way a provider failure is: this stage fails, the DAG degrades around it,
    and the worktree comes back to the caller so it still gets cleaned up.
    """
    try:
        worktree_path, task_branch = git_ops.create_worktree(git.Repo(target_root), branch, task.id)
    except Exception as exc:
        # Nothing was created (or it failed half-way and git's own bookkeeping
        # will be pruned next run), so there's no worktree to hand back.
        failure = ProviderResult(success=False, summary=f"worktree setup failed: {exc}")
        return StageResult(task=task, status="failed", result=failure), None, None

    try:
        provider_name = scheduler.resolve_provider(engine_root, task.agent, complexity)
        console.print(f"[bold]{task.id}[/bold] ({display_name(provider_name)})...")

        description = scheduler.build_stage_description(request, completed_snapshot)
        result = scheduler.run_task(
            worktree_path,
            task.agent,
            description,
            context,
            recorder=recorder,
            stage_id=task.id,
            engine_root=engine_root,
            complexity=complexity,
        )

        worktree_repo = git.Repo(worktree_path)
        changed = git_ops.commit_all(worktree_repo, f"{task.id}: {result.summary or request}")

        status = "done" if result.success else "failed"
        bad_files: list[str] = []
        if result.success:
            bad_files = contracts.violations(task.agent, changed)
            # Distinct from a contract violation (which is about *where within
            # its scope* a role wrote): this is a write git will never see at
            # all -- gitignored, so invisible to commit_all/the reviewer's diff
            # regardless of role (see #2). Applies even to backend/frontend/
            # tests, which have no declared artifact contract.
            ignored = git_ops.ignored_writes(worktree_repo)
            if ignored:
                bad_files = bad_files + [f"{path} [gitignored]" for path in ignored]
            if bad_files:
                status = "violated"

        if status == "failed":
            console.print(f"[bold red]{task.id} failed[/bold red]: {result.summary}")
        elif status == "violated":
            console.print(f"[bold red]{task.id} violated its contract[/bold red]: touched {', '.join(bad_files)}")

        return (
            StageResult(task=task, status=status, result=result, files_changed=changed),
            worktree_path,
            task_branch,
        )
    except Exception as exc:
        # A config error naming an unknown role, a provider raising, a git
        # failure mid-commit — all of it becomes this one stage's failure
        # rather than the run's. The type name is kept because "why did this
        # stage fail" is unanswerable from a bare message.
        console.print(f"[bold red]{task.id} failed[/bold red]: {type(exc).__name__}: {exc}")
        failure = ProviderResult(success=False, summary=f"{type(exc).__name__}: {exc}")
        return StageResult(task=task, status="failed", result=failure), worktree_path, task_branch


def run(
    engine_root: Path,
    target_root: Path,
    request: str,
    dry_run: bool = False,
    session_id: str | None = None,
) -> RunReport:
    """`engine_root` is the ai-platform install (config/, prompts/, the
    shared telemetry.sqlite); `target_root` is the repo this run actually
    modifies. They're the same directory when the engine operates on itself
    — the only mode that existed before `--repo` — but must not be conflated
    for an external target: config/prompts/telemetry are engine-scoped and
    fixed, while git operations, the test command and the context index are
    target-scoped (see core.context.manager, core.orchestrator.test_runner).
    """
    repo = git.Repo(target_root)
    if not dry_run:
        git_ops.ensure_clean_worktree(repo)
        git_ops.prune_worktrees(repo)

    console.rule("Engine")

    workflow = planner.plan(engine_root)
    console.print(f"[bold]Plan generated[/bold]: {len(workflow.tasks)} tasks (up to {workflow.max_parallel} in parallel)")

    context_manager = ContextManager(target_root, engine_root=engine_root)
    context_manager.index_repo()
    # Baseline for the correction loop's ignored_writes check below: unlike a
    # DAG stage's worktree (a fresh checkout that starts with none of these),
    # target_root already legitimately has .ai-platform/ at this point --
    # index_repo() just wrote it. Without subtracting this, the corrector's
    # very own context index would be reported as something *it* wrote.
    baseline_ignored = set(git_ops.ignored_writes(repo))
    context = context_manager.select_context(request)
    kept_files = len(context.context_paths())
    if kept_files:
        console.print(
            f"[bold]Context selected:[/bold] {kept_files} of {len(context.decisions)} candidates "
            f"(injected as {context_manager.config.injection_mode})"
        )
    else:
        # Not an error: asking for a feature the repo has no trace of is the
        # normal case for new work. Saying so is the point — silently shipping
        # twenty irrelevant files is what this replaced.
        console.print(
            f"[bold yellow]No context selected[/bold yellow] — none of "
            f"{len(context.decisions)} candidates cleared the relevance floor. "
            "The agent will explore on its own; run `ai-platform context` to see why."
        )

    # Created before the decomposer call, which is itself a billable provider
    # call — starting the recorder any later would understate every run. The
    # config snapshot and engine commit are captured now because neither can
    # be reconstructed from a past row: they're what makes runs comparable
    # across engine versions and across config changes.
    recorder = None
    routing = router.load_thresholds(engine_root)
    if not dry_run:
        recorder = telemetry.RunRecorder(
            engine_root,
            request,
            target_repo=str(target_root),
            session_id=session_id,
            engine_commit=git_ops.current_commit(repo),
            metadata={
                "use_graph": context_manager.config.use_graph,
                "use_vector_db": context_manager.config.use_vector_db,
                "use_git_diff": context_manager.config.use_git_diff,
                "use_memory": context_manager.config.use_memory,
                "max_files": context_manager.config.max_files,
                "injection_mode": context_manager.config.injection_mode,
                # The floors a run was judged against: comparing two runs'
                # file counts means nothing without them.
                "min_similarity": context_manager.config.min_similarity,
                "min_similarity_ratio": context_manager.config.min_similarity_ratio,
                "min_lift": context_manager.config.min_lift,
                "max_context_chars": context_manager.config.max_context_chars,
                # The thresholds routing was judged against. Same reason as the
                # context floors: comparing two runs' provider choices means
                # nothing without knowing the bar each was held to.
                "max_quota_ratio": routing.max_quota_ratio,
                "min_success_rate": routing.min_success_rate,
                "min_samples": routing.min_samples,
                "routing_window_hours": routing.window_hours,
                "decompose": workflow.decompose,
                "max_parallel": workflow.max_parallel,
                "max_correction_attempts": workflow.max_correction_attempts,
            },
        )

    complexity = router.DEFAULT_COMPLEXITY
    if workflow.decompose:
        known_ids = [t.id for t in workflow.tasks]
        decomposer_result = scheduler.run_task(
            target_root,
            "decomposer",
            decomposer.build_description(request, known_ids),
            context,
            recorder=recorder,
            engine_root=engine_root,
            complexity="routine",
        )
        chosen = decomposer.parse_tasks(decomposer_result.summary, known_ids) if decomposer_result.success else None
        classified = (
            decomposer.parse_complexity(decomposer_result.summary)
            if decomposer_result.success else None
        )
        complexity = classified or router.DEFAULT_COMPLEXITY
        if chosen is None:
            console.print("[bold yellow]Decomposition unavailable[/bold yellow] — running the full workflow")
        else:
            workflow = planner.prune(workflow, set(chosen))
            dropped = sorted(set(known_ids) - set(chosen))
            selected = ", ".join(t.id for t in workflow.tasks)
            dropped_note = f" ({', '.join(dropped)} not needed)" if dropped else ""
            console.print(f"[bold]Decomposed to:[/bold] {selected}{dropped_note}")

    console.print(f"[bold]Task complexity:[/bold] {complexity}")
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

    with git_ops.disable_hooks(repo):
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
                        _run_stage_in_worktree,
                        target_root,
                        engine_root,
                        branch,
                        task,
                        request,
                        context,
                        snapshot,
                        complexity,
                        recorder,
                    )
                    in_flight[future] = task

            _dispatch_ready()
            while in_flight:
                done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    task = in_flight.pop(future)
                    # _run_stage_in_worktree is written never to raise, so this
                    # is the backstop for a bug in *that* guarantee: without it
                    # a single unexpected error still takes down every other
                    # in-flight stage, which is the failure mode issue #1 is
                    # about. No worktree path is recoverable here — the run's
                    # own prune_worktrees will reclaim it next time.
                    try:
                        stage_result, worktree_path, task_branch = future.result()
                    except Exception as exc:
                        console.print(
                            f"[bold red]{task.id} crashed[/bold red]: {type(exc).__name__}: {exc}"
                        )
                        stage_result = StageResult(
                            task=task,
                            status="failed",
                            result=ProviderResult(success=False, summary=f"{type(exc).__name__}: {exc}"),
                        )
                        worktree_path, task_branch = None, None

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
                            # Best-effort: a worktree we can't remove is worth a
                            # message, not a dead run — the stage already failed
                            # and every other one is still in flight.
                            try:
                                git_ops.remove_worktree(repo, worktree_path)
                            except Exception as exc:
                                console.print(
                                    f"[bold yellow]could not remove {worktree_path}[/bold yellow]: {exc}"
                                )
                            # The branch outlives the worktree on purpose (see
                            # git_ops.remove_worktree) — but a branch nobody is
                            # told about is a leak, not a safety net.
                            if stage_result.files_changed:
                                console.print(
                                    f"  partial work from {task.id} kept on branch {task_branch}"
                                )

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

        test_result = test_runner.run_tests(target_root)
        _print_test_result(test_result)

        diff = git_ops.diff_since(repo, base_sha)
        review_result = scheduler.run_task(
            target_root, "reviewer", review.build_description(request, diff), recorder=recorder, engine_root=engine_root, complexity=complexity
        )
        review_passed = review.parse_verdict(review_result.summary) if review_result.success else None
        review_label = {True: "PASS", False: "FAIL", None: "UNKNOWN"}[review_passed]
        console.print(f"[bold]Review:[/bold] {review_label}")
        if review_result.summary:
            console.print(review_result.summary)

        overall_ok = not any_stage_incomplete and test_result.passed and review_passed is True

        # Bounded test/review -> corrector -> re-check loop (see
        # core.orchestrator.correction and planner.Plan.max_correction_attempts).
        # Deliberately scoped to test/review failure only: a DAG stage that
        # itself failed, was skipped, or hit a merge conflict isn't something a
        # single corrector pass can retroactively complete, so those runs go
        # straight to "needs attention" as before.
        correction_attempts = 0
        can_correct = (
            not any_stage_incomplete
            and not overall_ok
            and any(s.status == "done" and s.files_changed for s in stage_reports)
        )
        if can_correct:
            for attempt in range(1, workflow.max_correction_attempts + 1):
                correction_attempts = attempt
                console.print(
                    f"[bold yellow]Correction attempt {attempt}/{workflow.max_correction_attempts}[/bold yellow]"
                )
                correction_description = correction.build_description(
                    request,
                    test_output=test_result.output if not test_result.passed else "",
                    review_summary=review_result.summary if review_passed is False else "",
                )
                correction_result = scheduler.run_task(
                    target_root,
                    "corrector",
                    correction_description,
                    context,
                    recorder=recorder,
                    stage_id=f"correction-{attempt}",
                    engine_root=engine_root,
                    complexity=complexity,
                )
                corrected_files = git_ops.commit_all(
                    repo, f"correction {attempt}: {correction_result.summary or request}"
                )
                # The corrector runs directly on target_root, not a throwaway
                # worktree -- an ignored write here persists past this run, unlike
                # a DAG stage's (discarded with its worktree either way). Treated
                # as reason to stop outright rather than keep iterating: an
                # anomaly here isn't something another correction attempt should
                # be trusted to reason about. Diffed against baseline_ignored,
                # not raw -- target_root already has .ai-platform/ from
                # index_repo() above, unlike a stage's fresh worktree checkout.
                ignored = [p for p in git_ops.ignored_writes(repo) if p not in baseline_ignored]
                if ignored:
                    console.print(
                        f"[bold red]correction-{attempt} wrote outside version control[/bold red]: "
                        f"{', '.join(ignored)} — stopping the correction loop rather than trusting it"
                    )
                    overall_ok = False
                    break
                all_files_changed.extend(corrected_files)
                console.print(f"[bold]correction-{attempt}[/bold]: {git_ops.format_changed_files(corrected_files)}")

                test_result = test_runner.run_tests(target_root)
                _print_test_result(test_result)

                diff = git_ops.diff_since(repo, base_sha)
                review_result = scheduler.run_task(
                    target_root,
                    "reviewer",
                    review.build_description(request, diff),
                    recorder=recorder,
                    engine_root=engine_root,
                    complexity=complexity,
                )
                review_passed = review.parse_verdict(review_result.summary) if review_result.success else None
                review_label = {True: "PASS", False: "FAIL", None: "UNKNOWN"}[review_passed]
                console.print(f"[bold]Review:[/bold] {review_label}")
                if review_result.summary:
                    console.print(review_result.summary)

                overall_ok = not any_stage_incomplete and test_result.passed and review_passed is True
                if overall_ok:
                    break

    summary = "done" if overall_ok else "needs attention"

    totals: dict = {}
    if recorder is not None:
        recorder.finish(branch=branch, summary=summary)
        totals = telemetry.run_totals(engine_root, recorder.run_id)
        console.print(f"[bold]Usage:[/bold] {format_totals(totals)}")

    if correction_attempts:
        console.print(f"[bold]Corrections:[/bold] {correction_attempts} attempt(s)")
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
        correction_attempts=correction_attempts,
    )
