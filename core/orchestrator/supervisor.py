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

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

import git
from rich.console import Console

from core.context.manager import ContextManager, SelectedContext
from core.errors import ConfigError
from core.orchestrator import (
    contracts,
    correction,
    decomposer,
    git_ops,
    planner,
    review,
    router,
    scheduler,
    target_config,
    test_runner,
)
from core.orchestrator import platform_config as platform_config_module
from core.orchestrator.scheduler import StageResult
from core.telemetry import store as telemetry
from providers.base import ProviderResult, display_name

console = Console()

DIRTY_HEAD = "head"
"""Work only on the base commit. The default, and the only coherent one
available: the integration worktree is checked out from `base_sha` and every
prompt is built from that worktree, so uncommitted work in the target is
outside the run in both directions — not shown to the agents, not edited by
them."""

DIRTY_REJECT = "reject"
"""Refuse to start at all when the target has local modifications. For
callers who would rather be stopped than have their in-progress work quietly
left out."""

DIRTY_SNAPSHOT = "snapshot"
"""Reproduce the dirty state inside the run. Declared but not implemented:
carrying tracked modifications *and* untracked files across without touching
the user's index is the whole difficulty, and doing it badly would corrupt
the tree it's meant to preserve. Asking for it fails loudly rather than
silently falling back to `head` — a run recorded as `snapshot` that actually
behaved like `head` would be a lie in the telemetry."""

DIRTY_POLICIES = (DIRTY_HEAD, DIRTY_REJECT, DIRTY_SNAPSHOT)


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


def _verify(
    integration_repo: git.Repo, branch: str, config: target_config.TargetConfig
) -> test_runner.TestResult:
    """Runs the target's test command in a throwaway checkout of `branch`.

    The test command is the one actor guaranteed to litter — pytest writes
    `.pytest_cache/`, coverage writes `.coverage`, Python writes
    `__pycache__/`. Running it in the integration worktree meant those
    landed there and were then indistinguishable from something the
    *corrector* had written, which is both a wrong accusation and a real
    one waiting to be missed. A worktree deleted immediately afterwards
    keeps that noise out of the branch under review entirely.
    """
    if config.test_command is None:
        return test_runner.run_tests(Path(integration_repo.working_tree_dir), config)

    try:
        verify_root = git_ops.create_validation_worktree(integration_repo, branch)
    except Exception as exc:
        return test_runner.TestResult(
            passed=False, output=f"could not create the validation worktree: {exc}"
        )
    try:
        return test_runner.run_tests(verify_root, config)
    finally:
        try:
            git_ops.remove_worktree(integration_repo, verify_root)
        except Exception as exc:
            console.print(f"[bold yellow]could not remove {verify_root}[/bold yellow]: {exc}")


def _run_stage_in_worktree(
    integration_root: Path,
    engine_root: Path,
    branch: str,
    task: planner.Task,
    request: str,
    context: SelectedContext,
    completed_snapshot: list[StageResult],
    config: target_config.TargetConfig,
    platform_config: platform_config_module.PlatformConfig,
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
        worktree_path, task_branch = git_ops.create_worktree(
            git.Repo(integration_root), branch, task.id
        )
    except Exception as exc:
        # Nothing was created (or it failed half-way and git's own bookkeeping
        # will be pruned next run), so there's no worktree to hand back.
        failure = ProviderResult(success=False, summary=f"worktree setup failed: {exc}")
        return StageResult(task=task, status="failed", result=failure), None, None

    try:
        provider_name = scheduler.resolve_provider(
            engine_root, task.agent, complexity, platform_config=platform_config
        )
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
            platform_config=platform_config,
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
            # A stage worktree is a fresh checkout, so it starts with no
            # ignored files at all -- everything found here was written by
            # this stage. Expected artifacts (the target's declared caches)
            # are reported, not punished; anything else still is.
            expected, unexpected = git_ops.classify_ignored_writes(
                worktree_repo, config.allowed_ephemeral_writes
            )
            if expected:
                console.print(
                    f"  [dim]{task.id}: {len(expected)} declared ephemeral path(s) ignored[/dim]"
                )
            if unexpected:
                bad_files = bad_files + [f"{path} [gitignored]" for path in unexpected]
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


def _apply_dirty_policy(repo: git.Repo, policy: str) -> list[str]:
    """Decides what a run does about local modifications in the target, and
    returns the paths it is leaving out.

    A run works on `base_sha` and nothing else: the integration worktree is
    checked out from it, and every prompt is built from that worktree. So
    tracked modifications, untracked files and uncommitted memory edits are
    all simply outside the run. That is coherent — the agent reads and edits
    the same tree — but it is not obvious from the outside, which is why the
    count is printed rather than left implicit.

    A warning was the previous answer and it wasn't enough: it described a
    mismatch (context showing edits the agents couldn't see) instead of
    removing it, and for a run driven from a phone, "your tree is dirty" is
    not something the reader can act on mid-run.
    """
    if policy not in DIRTY_POLICIES:
        raise ConfigError(
            f"Unknown --dirty-policy {policy!r}. Valid policies: {', '.join(DIRTY_POLICIES)}"
        )
    if policy == DIRTY_SNAPSHOT:
        raise ConfigError(
            "--dirty-policy=snapshot is not implemented yet: carrying tracked modifications "
            "and untracked files into the run without touching your index is the hard part, "
            f"and a half-done version corrupts what it's meant to preserve. Use "
            f"--dirty-policy={DIRTY_HEAD} to run on the last commit, or commit/stash first."
        )

    excluded = git_ops.local_modifications(repo)
    if not excluded:
        return []

    if policy == DIRTY_REJECT:
        raise ConfigError(
            f"{len(excluded)} local modification(s) in {repo.working_tree_dir} and "
            f"--dirty-policy={DIRTY_REJECT}: commit or stash them, or re-run with "
            f"--dirty-policy={DIRTY_HEAD} to work on the last commit and leave them out."
        )

    shown = ", ".join(excluded[:5])
    more = f", +{len(excluded) - 5} more" if len(excluded) > 5 else ""
    console.print(
        f"[bold yellow]{len(excluded)} local modification(s) excluded from the run[/bold yellow] — "
        f"it works on the last commit only ({shown}{more})"
    )
    return excluded


def _guarded(progress: Callable[..., None] | None) -> Callable[..., None]:
    """Wraps a progress callback so it can neither be absent nor fatal.

    A callback that writes to a database, a socket or a log is a dependency
    the run did not choose and cannot vouch for. Losing one progress update
    costs an observer some precision; letting it raise costs the run.
    """
    if progress is None:
        return lambda **_: None

    def report(**fields) -> None:
        try:
            progress(**fields)
        except Exception as exc:
            console.print(f"[dim]progress not recorded: {type(exc).__name__}: {exc}[/dim]")

    return report


def _discard_integration_worktree(
    repo: git.Repo, integration_repo: git.Repo, integration_root: Path, branch: str
) -> None:
    """Removes a worktree and its branch, for a run that produced nothing.

    Best-effort on both halves: a worktree that won't go is worth a message,
    not a failed command, and the branch is deleted from `repo` rather than
    `integration_repo` because the latter's checkout no longer exists by then.
    """
    try:
        git_ops.remove_worktree(integration_repo, integration_root)
    except Exception as exc:
        console.print(f"[bold yellow]could not remove {integration_root}[/bold yellow]: {exc}")
        return
    try:
        repo.git.branch("-D", branch)
    except Exception as exc:
        console.print(f"[bold yellow]could not delete branch {branch}[/bold yellow]: {exc}")


def run(
    engine_root: Path,
    target_root: Path,
    request: str,
    dry_run: bool = False,
    session_id: str | None = None,
    dirty_policy: str = DIRTY_HEAD,
    progress: Callable[..., None] | None = None,
) -> RunReport:
    """`engine_root` is the ai-platform install (config/, prompts/, the
    shared telemetry.sqlite); `target_root` is the repo this run actually
    modifies. They're the same directory when the engine operates on itself
    — the only mode that existed before `--repo` — but must not be conflated
    for an external target: config/prompts/telemetry are engine-scoped and
    fixed, while git operations, the test command and the context index are
    target-scoped (see core.context.manager, core.orchestrator.test_runner).

    A third root appears once work starts: the run's own *integration*
    worktree, checked out from `base_sha`, where the `engine/<slug>` branch
    lives and where every merge, correction commit, test run and diff
    happens. `target_root`'s own checkout is only ever read — never switched,
    never written to — so a run can proceed while its target is being worked
    in.

    That worktree is also the **only** tree the run reads from. Context is
    indexed from it, not from `target_root`: file content, chunk excerpts and
    project memory all used to come off the user's own checkout while the
    agents edited a checkout of `base_sha`, so an uncommitted edit could
    describe code that did not exist in what was being edited. Local
    modifications are counted and reported, and are otherwise not part of the
    run — see `dirty_policy` and `_apply_dirty_policy`.

    Order matters here and is enforced by structure rather than convention:
    the run lock is taken before the first mutating operation, so a run that
    is refused has created no branch, no worktree, and pruned nothing.

    `progress` is called with keyword fields as the run reaches them —
    `base_sha`, `branch`, the integration worktree path, the stages currently
    in flight, the correction attempt. Deliberately a bare callable rather
    than a job-store handle: the supervisor should not know that a durable
    queue exists (see core.jobs), and a caller with no interest in progress
    passes nothing. What it buys is that a run killed halfway leaves a record
    of where its work is, instead of an orphan branch nobody can attribute.
    """
    repo = git.Repo(target_root)
    # Progress reporting must never be able to fail a run: it is bookkeeping
    # for an observer, and a queue with a bad row is a smaller problem than a
    # run that died reporting into it.
    report_progress = _guarded(progress)

    console.rule("Engine")

    # Loaded once, here, and threaded through the rest of the run rather than
    # re-read per module (router.py/planner.py/manager.py each used to do
    # their own independent read) -- one config snapshot per run, so an
    # unknown-preset typo surfaces once, early, instead of five calls deep
    # into whichever module happened to read it first, and a run stays
    # internally consistent even if config/platform.yaml changes mid-run.
    platform_config = platform_config_module.load(engine_root)
    workflow = planner.plan(engine_root, platform_config)
    console.print(f"[bold]Plan generated[/bold]: {len(workflow.tasks)} tasks (up to {workflow.max_parallel} in parallel)")

    # Nothing above this line touches the target. Everything below it does —
    # pruning rewrites git's worktree bookkeeping, `disable_hooks` rewrites
    # repository-wide config, and the integration worktree creates a branch —
    # so the lock wraps all of it, including a dry run. A dry run still
    # prunes, still creates a worktree to index and still calls the
    # decomposer; exempting it would leave exactly the races the lock exists
    # to prevent.
    with git_ops.exclusive_run_lock(repo), git_ops.disable_hooks(repo):
        git_ops.prune_worktrees(repo)

        base_sha = git_ops.current_commit(repo)
        report_progress(base_ref=git_ops.current_ref(repo), base_sha=base_sha)
        excluded = _apply_dirty_policy(repo, dirty_policy)
        # The run's policy, frozen. Read from the base commit rather than from
        # any working tree, and never re-read: roles without an artifact
        # contract can write .ai-platform.yml, and re-reading it after they run
        # let a stage set `test_sandbox: false` and have the engine honour it —
        # a real, demonstrated bypass of issue #4's sandbox that still reported
        # the run as `done`. See core.orchestrator.target_config.
        config = target_config.load_at_commit(repo, base_sha)

        integration_root, branch = git_ops.create_integration_worktree(repo, request)
        # Everything from here on operates on the run's own checkout. `repo`
        # (the target's working tree) is still used for hook neutralization,
        # which is repository-wide config rather than per-worktree, and for
        # pruning.
        integration_repo = git.Repo(integration_root)
        console.print(f"[bold]Integration worktree:[/bold] {integration_root} ({branch})")
        # Reported before a single agent runs: from here on there is state on
        # disk, and anything that kills this process without recording where
        # it is leaves an orphan branch nobody can attribute to a request.
        report_progress(branch=branch, integration_root=str(integration_root))

        # Indexed from the integration worktree, so the prompt and the edited
        # tree are the same state. The index itself stays under `target_root`:
        # it belongs to the project, and writing it into a directory this run
        # deletes would both throw away the graph cache every time and risk
        # `commit_all` sweeping `.ai-platform/` onto the branch in a target
        # that doesn't gitignore it.
        context_manager = ContextManager(
            integration_root,
            engine_root=engine_root,
            storage_root=target_root,
            platform_config=platform_config,
        )
        context_manager.index_repo()
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
        if not dry_run:
            recorder = telemetry.RunRecorder(
                engine_root,
                request,
                target_repo=str(target_root),
                session_id=session_id,
                engine_commit=git_ops.current_commit(repo),
                metadata={
                    "profile": platform_config.profile,
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
                    "max_quota_ratio": platform_config.routing.max_quota_ratio,
                    "min_success_rate": platform_config.routing.min_success_rate,
                    "min_samples": platform_config.routing.min_samples,
                    "routing_window_hours": platform_config.routing.window_hours,
                    "decompose": workflow.decompose,
                    "max_parallel": workflow.max_parallel,
                    "max_correction_attempts": workflow.max_correction_attempts,
                    # What the run was allowed to see of the target. A run whose
                    # source was dirty is not wrong, but it is not reproducible
                    # from `base_sha` alone either — the excluded edits explain a
                    # result the commit doesn't.
                    "dirty_policy": dirty_policy,
                    "source_dirty": bool(excluded),
                    "excluded_local_modifications": len(excluded),
                },
            )
            report_progress(run_id=recorder.run_id)

        complexity = router.DEFAULT_COMPLEXITY
        if workflow.decompose:
            report_progress(stage="decompose")
            known_ids = [t.id for t in workflow.tasks]
            decomposer_result = scheduler.run_task(
                integration_root,
                "decomposer",
                decomposer.build_description(request, known_ids),
                context,
                recorder=recorder,
                engine_root=engine_root,
                complexity="routine",
                platform_config=platform_config,
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
            # A dry run produces nothing, so it keeps nothing: the worktree it
            # indexed from and the branch that carried it both go. Left behind,
            # they'd accumulate an `engine/<slug>-N` branch per inspection.
            _discard_integration_worktree(repo, integration_repo, integration_root, branch)
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

        remaining = {t.id: t for t in workflow.tasks}
        completed: list[StageResult] = []
        completed_ids: set[str] = set()
        blocked_ids: set[str] = set()
        stage_reports: list[StageReport] = []
        all_files_changed: list[str] = []
        # Reported as a set, not a single name: up to `max_parallel` stages
        # run at once, and naming only the last dispatched would describe a
        # run that isn't happening.
        dispatched: set[str] = set()

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
                    dispatched.add(task.id)
                    report_progress(stage=", ".join(sorted(dispatched)))
                    future = executor.submit(
                        _run_stage_in_worktree,
                        integration_root,
                        engine_root,
                        branch,
                        task,
                        request,
                        context,
                        snapshot,
                        config,
                        platform_config,
                        complexity,
                        recorder,
                    )
                    in_flight[future] = task

            _dispatch_ready()
            while in_flight:
                done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    task = in_flight.pop(future)
                    dispatched.discard(task.id)
                    report_progress(stage=", ".join(sorted(dispatched)))
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
                        merged = git_ops.merge_worktree(integration_repo, task_branch)
                        if merged:
                            git_ops.remove_worktree(integration_repo, worktree_path)
                            integration_repo.git.branch("-D", task_branch)
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
                                git_ops.remove_worktree(integration_repo, worktree_path)
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

        report_progress(stage="verify")
        test_result = _verify(integration_repo, branch, config)
        _print_test_result(test_result)

        report_progress(stage="review")
        diff = git_ops.diff_since(integration_repo, base_sha)
        review_result = scheduler.run_task(
            integration_root,
            "reviewer",
            review.build_description(request, diff),
            recorder=recorder,
            engine_root=engine_root,
            complexity=complexity,
            platform_config=platform_config,
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
                report_progress(stage=f"correction-{attempt}", attempt=attempt)
                # Per actor, not once per run: earlier attempts and the
                # merges before them may legitimately have left files here.
                baseline_ignored = set(git_ops.ignored_writes(integration_repo))
                console.print(
                    f"[bold yellow]Correction attempt {attempt}/{workflow.max_correction_attempts}[/bold yellow]"
                )
                correction_description = correction.build_description(
                    request,
                    test_output=test_result.output if not test_result.passed else "",
                    review_summary=review_result.summary if review_passed is False else "",
                )
                correction_result = scheduler.run_task(
                    integration_root,
                    "corrector",
                    correction_description,
                    context,
                    recorder=recorder,
                    stage_id=f"correction-{attempt}",
                    engine_root=engine_root,
                    complexity=complexity,
                    platform_config=platform_config,
                )
                corrected_files = git_ops.commit_all(
                    integration_repo, f"correction {attempt}: {correction_result.summary or request}"
                )
                # The corrector runs directly on target_root, not a throwaway
                # worktree -- an ignored write here persists past this run, unlike
                # a DAG stage's (discarded with its worktree either way). Treated
                # as reason to stop outright rather than keep iterating: an
                # anomaly here isn't something another correction attempt should
                # be trusted to reason about. Diffed against baseline_ignored,
                # not raw -- target_root already has .ai-platform/ from
                # index_repo() above, unlike a stage's fresh worktree checkout.
                _, ignored = git_ops.classify_ignored_writes(
                    integration_repo, config.allowed_ephemeral_writes, baseline_ignored
                )
                if ignored:
                    console.print(
                        f"[bold red]correction-{attempt} wrote outside version control[/bold red]: "
                        f"{', '.join(ignored)} — stopping the correction loop rather than trusting it"
                    )
                    overall_ok = False
                    break
                all_files_changed.extend(corrected_files)
                console.print(f"[bold]correction-{attempt}[/bold]: {git_ops.format_changed_files(corrected_files)}")

                test_result = _verify(integration_repo, branch, config)
                _print_test_result(test_result)

                diff = git_ops.diff_since(integration_repo, base_sha)
                review_result = scheduler.run_task(
                    integration_root,
                    "reviewer",
                    review.build_description(request, diff),
                    recorder=recorder,
                    engine_root=engine_root,
                    complexity=complexity,
                    platform_config=platform_config,
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

        # The branch is the deliverable and always survives — nothing is ever
        # auto-merged, so `git checkout engine/<slug>` is how the work is picked
        # up either way. The *directory* only earns its keep when something went
        # wrong: that's when the exact on-disk state (untracked files, test
        # artifacts, a half-applied edit) answers questions the branch alone
        # can't. On success it would just accumulate, which is the leak that
        # issue #1 was about, one level up.
        if overall_ok:
            try:
                git_ops.remove_worktree(integration_repo, integration_root)
                # Cleared rather than left pointing at a directory that no
                # longer exists: an observer reading the job row afterwards
                # should see "the branch is the deliverable", not a dead path.
                report_progress(integration_root="", stage="")
            except Exception as exc:
                console.print(
                    f"[bold yellow]could not remove {integration_root}[/bold yellow]: {exc}"
                )
        else:
            console.print(f"[bold]Left for inspection:[/bold] {integration_root} ({branch})")

        totals: dict = {}
        if recorder is not None:
            recorder.finish(branch=branch, summary=summary)
            totals = telemetry.run_totals(engine_root, recorder.run_id)
            console.print(f"[bold]Usage:[/bold] {format_totals(totals)}")

        if correction_attempts:
            console.print(f"[bold]Corrections:[/bold] {correction_attempts} attempt(s)")
        console.print(f"[bold]Summary:[/bold] {summary}")
        if overall_ok:
            console.print(f"[bold]Branch:[/bold] {branch} — review it, then merge if you're happy")

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
