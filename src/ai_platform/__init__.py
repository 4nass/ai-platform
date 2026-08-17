"""CLI entry point for the ai-platform engine."""

from __future__ import annotations

import sys
from pathlib import Path

# core/ and providers/ live at the repo root (outside the installed
# src/ai_platform package): make them importable by adding the repo root to
# sys.path, regardless of the cwd the script is launched from.
#
# This is also the engine install itself -- config/, prompts/ and the shared
# telemetry.sqlite all live here (see core.telemetry.store, providers.base).
# It's fixed, unlike the repo a command actually operates on (see --repo
# below): the engine's own operating parameters don't move just because it's
# pointed at a different project.
ENGINE_ROOT = Path(__file__).resolve().parents[2]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402

console = Console()

app = typer.Typer(help="ai-platform — request -> RAG context -> task DAG -> verified modification.")


REPO_OPTION = typer.Option(
    None,
    "--repo",
    help="Repo to operate on (default: the current directory). The engine's own "
    "config/prompts/telemetry always come from the ai-platform install, regardless of this.",
)


PROJECT_OPTION = typer.Option(
    None,
    "--project",
    "-p",
    help="Allowlisted project id from config/projects.yaml, instead of a path. "
    "The only form a remote caller may use — see docs/security.md.",
)


def _target_root(repo: Path | None) -> Path:
    return (repo or Path.cwd()).resolve()


def _admit(repo: Path | None, project_id: str | None, *, action: str):
    """Resolves what a command may operate on, and returns (path, project).

    Two admission paths on purpose, because there are two trust contexts.
    `--repo <path>` is someone at their own workstation naming a directory
    they can already `cd` into; the engine adds nothing by second-guessing it.
    `--project <id>` is the form anything arriving over a wire must use: the
    caller supplies a name, and the engine — not the caller — decides what it
    refers to, whether it is reachable, and what may be done to it
    (`core.orchestrator.registry`, issue #25).

    Called before anything is indexed and before any provider is chosen, so a
    refusal costs nothing and leaks nothing.
    """
    from core.orchestrator import registry

    if project_id and repo:
        console.print(
            "[bold red]Error:[/bold red] --project and --repo name the same thing two ways. "
            "Use --project for an allowlisted id, --repo for a local path."
        )
        raise typer.Exit(1)

    if not project_id:
        return _target_root(repo), None

    try:
        project = registry.resolve(ENGINE_ROOT, project_id, action=action)
    except registry.RegistryError as exc:
        console.print(f"[bold red]Refused:[/bold red] {exc}")
        raise typer.Exit(1)
    return project.path, project


@app.callback()
def callback() -> None:
    """ai-platform — request -> RAG context -> task DAG -> verified modification."""


@app.command()
def doctor(
    repo: Path = REPO_OPTION,
    project: str = PROJECT_OPTION,
) -> None:
    """Checks whether the engine and target can execute a reliable run.

    ``PASS`` is a usable prerequisite, ``WARN`` is an optional or degraded
    capability, and ``FAIL`` blocks a reliable run. The command is read-only
    and exits non-zero when any check is ``FAIL``.
    """
    from rich.table import Table

    from core import doctor as diagnostics
    from core.orchestrator import registry

    target, _ = _admit(repo, project, action=registry.INSPECT)
    report = diagnostics.run(ENGINE_ROOT, target)

    table = Table(title="ai-platform doctor")
    table.add_column("status", style="bold")
    table.add_column("check")
    table.add_column("detail")
    for check in report.checks:
        style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[check.status]
        detail = check.detail
        if check.remediation:
            detail = f"{detail}\nFix: {check.remediation}"
        table.add_row(f"[{style}]{check.status}[/{style}]", check.name, detail)
    console.print(table)

    if report.failed:
        raise typer.Exit(1)


@app.command()
def run(
    request: str = typer.Argument(..., help="Natural-language request to carry out on the repo."),
    repo: Path = REPO_OPTION,
    project: str = PROJECT_OPTION,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the planned workflow and the decomposer's selected tasks without invoking any agent.",
    ),
    session: str = typer.Option(
        None,
        "--session",
        help="Groups this run with others from the same conversation in the telemetry history.",
    ),
    dirty_policy: str = typer.Option(
        "head",
        "--dirty-policy",
        help="What to do about local modifications in the target: 'head' (default) works on the "
        "last commit and leaves them out, reporting how many; 'reject' refuses to start; "
        "'snapshot' would reproduce them inside the run and is not implemented yet.",
    ),
) -> None:
    """Indexes the repo, selects relevant context, and runs the workflow DAG (see `ai-platform config`)."""
    from core.orchestrator import registry, supervisor

    # A dry run indexes and decomposes but writes nothing, so it needs only
    # `inspect`. Asking for `modify` here would make a project that is
    # deliberately read-only unable to even be inspected.
    action = registry.INSPECT if dry_run else registry.MODIFY
    target, admitted = _admit(repo, project, action=action)

    try:
        report = supervisor.run(
            ENGINE_ROOT,
            target,
            request,
            dry_run=dry_run,
            session_id=session,
            dirty_policy=dirty_policy,
            project=admitted,
        )
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    if dry_run:
        return

    if report.summary != "done":
        raise typer.Exit(1)


@app.command()
def context(
    request: str = typer.Argument(..., help="The request to select context for."),
    repo: Path = REPO_OPTION,
    project: str = PROJECT_OPTION,
    show_dropped: bool = typer.Option(
        True, "--dropped/--no-dropped", help="Also list the candidates that were rejected."
    ),
) -> None:
    """Shows which files a request would select, and why — without running anything.

    Indexing and selection only: no agent is invoked and no tokens are spent,
    so the relevance floors and the budget can be tuned against real requests
    for free.
    """
    from rich.table import Table

    from core.context.manager import FULL, POINTERS, ContextManager

    from core.orchestrator import registry

    target, _ = _admit(repo, project, action=registry.INSPECT)
    manager = ContextManager(target, engine_root=ENGINE_ROOT)
    manager.index_repo()
    selected = manager.select_context(request)

    table = Table(title=f"Context for: {request}")
    for column in ("rank", "path", "source", "score", "lift", "decision"):
        table.add_column(column)

    rank = 0
    for decision in selected.decisions:
        if not decision.kept and not show_dropped:
            continue
        rank = rank + 1 if decision.kept else rank
        table.add_row(
            str(rank) if decision.kept else "-",
            decision.path,
            decision.source,
            f"{decision.score:.4f}",
            f"{decision.lift:.2f}x" if decision.lift is not None else "-",
            f"[green]{decision.reason}[/green]" if decision.kept else f"[dim]{decision.reason}[/dim]",
        )

    console.print(table)

    kept = len([d for d in selected.decisions if d.kept])
    if not kept:
        console.print(
            f"[bold yellow]Nothing selected[/bold yellow] — none of {len(selected.decisions)} "
            "candidates cleared the relevance floor."
        )

    for mode, label in ((POINTERS, "pointers"), (FULL, "full")):
        selected.injection_mode = mode
        rendered = selected.render_for(reads_files=True)
        budget_note = f", {rendered.dropped} cut for budget" if rendered.dropped else ""
        plural = "" if rendered.files == 1 else "s"
        console.print(
            f"[bold]{label}:[/bold] {len(rendered.text):,} chars, "
            f"{rendered.files} file{plural}{budget_note}"
        )


@app.command()
def history(
    repo: Path = REPO_OPTION,
    project: str = PROJECT_OPTION,
    runs: int = typer.Option(20, "--runs", help="How many recent runs to show."),
    session: str = typer.Option(None, "--session", help="Only show runs from this session."),
) -> None:
    """Shows what recent runs cost — tokens, price, duration, outcome.

    Telemetry is shared across every repo the engine has been pointed at
    (see core.telemetry.store), so this scopes to the resolved --repo —
    self-targeting by default, meaning nothing changes if you never pass one.
    """
    from rich.table import Table

    from core.orchestrator import registry
    from core.telemetry import store as telemetry

    target, _ = _admit(repo, project, action=registry.INSPECT)
    rows = telemetry.recent_runs(
        ENGINE_ROOT, limit=runs, session_id=session, target_repo=str(target)
    )
    if not rows:
        console.print("No runs recorded yet.")
        return

    table = Table(title="Recent runs")
    for column in ("id", "started", "request", "summary", "calls", "in", "cached", "out", "duration", "cost"):
        table.add_column(column)

    for row in rows:
        duration = f"{row['duration_ms'] / 1000:.1f}s" if row["duration_ms"] else "-"
        # Cost trails the token columns and is blank when no provider reported
        # one — a subscription prices nothing per call, and "$0.0000" would
        # read as free rather than as unknown.
        priced = row["priced_calls"]
        if priced == 0:
            cost = "-"
        elif priced == row["calls"]:
            cost = f"${row['cost_usd']:.4f}"
        else:
            cost = f"${row['cost_usd']:.4f} ({priced}/{row['calls']})"
        table.add_row(
            str(row["id"]),
            (row["started_at"] or "")[:19].replace("T", " "),
            (row["request"] or "")[:48],
            row["summary"] or "-",
            str(row["calls"]),
            f"{row['input_tokens']:,}",
            f"{row['cache_read_tokens']:,}",
            f"{row['output_tokens']:,}",
            duration,
            cost,
        )

    console.print(table)


@app.command()
def route(
    role: str = typer.Argument(None, help="Role to explain. Omit to show every configured role."),
    complexity: str = typer.Option(
        "complex",
        "--complexity",
        help="Task class used to select role profiles: routine, complex, or critical.",
    ),
) -> None:
    """Shows which provider would serve a role, and why — without running it.

    The counterpart to `context`: no agent is invoked and no quota is spent, so
    the routing thresholds can be tuned against real history for free.
    """
    from rich.table import Table

    import yaml

    from core.orchestrator import platform_config as pc
    from core.orchestrator import scheduler

    platform = pc.load(ENGINE_ROOT)

    if role:
        roles = [role]
    else:
        profile_path = pc.profile_preset_path(ENGINE_ROOT, platform.profile)
        config = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        roles = sorted(config)

    for name in roles:
        try:
            decision = scheduler.route_agent(ENGINE_ROOT, name, complexity, platform_config=platform)
        except Exception as exc:
            console.print(f"[bold red]{name}:[/bold red] {exc}")
            continue

        table = Table(title=f"{name} ({decision.complexity}) → {decision.provider}")
        # model/effort get their own column rather than only appearing inside
        # the wrapped reason prose: they are what this role's routing now turns
        # on, and two candidates can differ by nothing else. Kept to one column
        # because eight of them squeeze the reason into unreadable wrapping.
        for column in ("rank", "provider", "profile", "quota", "success", "calls", "decision"):
            table.add_column(column)

        for candidate in decision.candidates:
            quota_ratio = f"{candidate.quota_ratio:.0%}" if candidate.quota_ratio is not None else "-"
            success = f"{candidate.success_rate:.0%}" if candidate.success_rate is not None else "-"
            style = "green" if candidate.chosen else "dim"
            profile = "/".join(p for p in (candidate.model, candidate.reasoning_effort) if p) or "-"
            table.add_row(
                str(candidate.rank),
                candidate.provider,
                profile,
                quota_ratio,
                success,
                str(candidate.calls),
                f"[{style}]{candidate.reason}[/{style}]",
            )

        console.print(table)


@app.command()
def quota(
    window: float = typer.Option(
        None, "--window", help="Rolling window in hours (defaults to the widest declared budget)."
    ),
) -> None:
    """Shows how much of each subscription's budget recent runs have consumed.

    Neither CLI reports a remaining balance, so this measures what was
    actually recorded against the limits declared in config/platform.yaml.
    """
    from rich.table import Table

    from core.orchestrator import platform_config as pc
    from core.telemetry import quota as quota_store

    rows = quota_store.pressure(ENGINE_ROOT, pc.load(ENGINE_ROOT).quotas, window_hours=window)
    if not rows:
        console.print("No provider usage recorded yet, and no budgets declared.")
        return

    table = Table(title=f"Provider pressure ({rows[0]['window_hours']:g}h window)")
    for column in ("provider", "calls", "in", "out", "total", "budget", "used", "success", "avg"):
        table.add_column(column)

    for row in rows:
        budget = f"{row['budget_tokens']:,}" if row["budget_tokens"] else "-"
        used = f"{row['used_ratio'] * 100:.1f}%" if row["used_ratio"] is not None else "-"
        success = f"{row['success_rate']:.0%}" if row["success_rate"] is not None else "-"
        avg = f"{row['avg_duration_ms'] / 1000:.1f}s" if row["avg_duration_ms"] else "-"
        table.add_row(
            row["provider"],
            str(row["calls"]),
            f"{row['input_tokens']:,}",
            f"{row['output_tokens']:,}",
            f"{row['total_tokens']:,}",
            budget,
            used,
            success,
            avg,
        )

    console.print(table)


@app.command(name="config")
def show_config() -> None:
    """Shows the resolved platform policy — which preset is active and its
    numbers — without running anything or spending a token.

    The counterpart to `route`/`context`/`quota`: those explain one decision
    each; this answers "which preset am I on" for the whole run at once.
    """
    from rich.table import Table

    from core.orchestrator import platform_config as pc

    config = pc.load(ENGINE_ROOT)

    table = Table(title="Resolved platform config", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("profile", config.profile)
    table.add_row("workflow.mode", config.workflow_mode)
    table.add_row("workflow.max_parallel", str(config.max_parallel))
    table.add_row("workflow.decompose", str(config.decompose))
    table.add_row("workflow.max_correction_attempts", str(config.max_correction_attempts))
    table.add_row("context.mode", config.context_mode)
    if config.context_advanced:
        table.add_row("context.advanced", ", ".join(f"{k}={v}" for k, v in config.context_advanced.items()))
    table.add_row("routing.max_quota_ratio", f"{config.routing.max_quota_ratio:.0%}")
    table.add_row("routing.min_success_rate", f"{config.routing.min_success_rate:.0%}")
    table.add_row("routing.min_samples", str(config.routing.min_samples))
    table.add_row("routing.window_hours", f"{config.routing.window_hours:g}")
    for name, budget in sorted(config.quotas.items()):
        # Not f"quota[{name}]": Rich's table cells render markup by default,
        # and a bracketed provider name is indistinguishable from a style tag
        # -- it gets parsed and silently stripped rather than shown.
        table.add_row(f"quota: {name}", f"{budget.tokens:,} tokens / {budget.window_hours:g}h")

    table.add_row("budgets.mode", config.budget_mode)
    for name, limits in sorted(config.budget_classes.items()):
        # Shown per class rather than only naming them: an unknown class
        # resolves to unlimited on purpose (a run must not die mid-DAG for a
        # typo in a file it never reads), so this listing is where a misspelled
        # class is meant to be caught.
        table.add_row(
            f"budget: {name}",
            f"{limits.max_run_tokens:,}/run · {limits.max_stage_tokens:,}/stage · "
            f"{limits.max_run_calls} calls · {limits.max_window_tokens:,}/{limits.window_hours:g}h",
        )

    console.print(table)
    console.print(f"[dim]{pc.PLATFORM_CONFIG_PATH} at {ENGINE_ROOT}[/dim]")


DIRTY_POLICY_HELP = (
    "What to do about local modifications in the target: 'head' (default) works on the "
    "last commit and leaves them out, reporting how many; 'reject' refuses to start; "
    "'snapshot' would reproduce them inside the run and is not implemented yet."
)


@app.command()
def submit(
    request: str = typer.Argument(..., help="Natural-language request to carry out on the repo."),
    repo: Path = REPO_OPTION,
    project: str = PROJECT_OPTION,
    session: str = typer.Option(
        None, "--session", help="Groups this job with others from the same conversation."
    ),
    dirty_policy: str = typer.Option("head", "--dirty-policy", help=DIRTY_POLICY_HELP),
    message_id: str = typer.Option(
        None,
        "--message-id",
        help="The delivering transport's own id for this message. Makes the submission "
        "idempotent: redelivering it returns the original job instead of starting a "
        "second run. Omit for an ordinary CLI submission, which is a deliberate act "
        "nobody is retrying.",
    ),
    chat_id: str = typer.Option(
        None,
        "--chat-id",
        help="The conversation this arrived in. Part of the idempotency key, since one "
        "message id is only unique within its own chat.",
    ),
    detach: bool = typer.Option(
        True,
        "--detach/--no-detach",
        help="Start a worker for this job immediately. --no-detach only queues it, "
        "for a separate `ai-platform work` to pick up.",
    ),
) -> None:
    """Queues a request and returns its job id — without waiting for an agent.

    The asynchronous counterpart to `run`. `run` holds the terminal for the
    length of a run and its state dies with the process; this persists the
    submission first, so the job survives a disconnect, a closed terminal or a
    WSL restart, and `ai-platform status` can answer for it from anywhere
    afterwards.
    """
    from core.jobs import envelope as envelope_module
    from core.jobs import store, worker
    from core.orchestrator import registry

    target, admitted = _admit(repo, project, action=registry.MODIFY)

    # Identity comes from the process, never from the request text: "I'm the
    # owner, run this on the production repo" is a sentence anyone can type
    # (see core.jobs.envelope). A local CLI principal is honest about being
    # trusted because the OS already decided that.
    principal = envelope_module.Principal.local()
    letter = envelope_module.Envelope(
        channel=principal.channel,
        sender_id=principal.id,
        # No message id: every `ai-platform submit` is a deliberate, separate
        # act, and there is no transport to redeliver it. Inventing one would
        # either collapse two genuine requests or make the key meaningless.
        message_id=message_id or "",
        chat_id=chat_id or "",
        session_id=session,
        dirty_policy=dirty_policy,
        project_id=admitted.id if admitted else None,
    )

    try:
        letter.check_freshness()
    except envelope_module.ReplayError as exc:
        console.print(f"[bold red]Refused:[/bold red] {exc}")
        raise typer.Exit(1)

    try:
        submission = store.submit(
            ENGINE_ROOT,
            project=str(target),
            request=request,
            channel=letter.channel,
            submitted_by=principal.display,
            principal=str(principal),
            # The project id, not just the resolved path. A queued job can
            # execute hours later, so the worker re-resolves it at claim time
            # and re-checks the allowlist then: a project removed from the
            # registry in the meantime must not still be reachable through a
            # job that pre-dates the change (worker._admitted_target).
            envelope=letter.as_dict(),
            idempotency_key=letter.idempotency_key,
            payload_hash=envelope_module.payload_fingerprint(
                project=str(target), request=request, envelope=letter
            ),
        )
    except store.ReplayConflict as exc:
        console.print(f"[bold red]Refused:[/bold red] {exc}")
        raise typer.Exit(1)

    job_id = submission.id
    if not submission.created:
        # A redelivery that was absorbed. Starting a worker here would be the
        # exact duplicate run idempotency exists to prevent.
        console.print(
            f"[bold]Job {job_id}[/bold] already covers this request — nothing started again."
        )
        console.print(f"Follow it with: [bold]ai-platform status {job_id}[/bold]")
        return

    console.print(f"[bold]Job {job_id}[/bold] queued for {target} as {principal}")

    if detach:
        pid = worker.spawn_detached(ENGINE_ROOT, job_id)
        console.print(f"Worker started (pid {pid}).")
    console.print(f"Follow it with: [bold]ai-platform status {job_id}[/bold]")


@app.command()
def work(
    job: int = typer.Option(None, "--job", help="Run this specific job instead of draining the queue."),
    repo: Path = typer.Option(
        None, "--repo", help="Only take jobs targeting this repo (default: any)."
    ),
    limit: int = typer.Option(None, "--limit", help="Stop after this many jobs."),
) -> None:
    """Executes queued jobs in the foreground.

    This is what a managed service unit would run (issue #40), and what picks
    up anything submitted with `--no-detach` or left behind by a restart.
    """
    from core.jobs import store, worker

    for stale in worker.reconcile(ENGINE_ROOT):
        console.print(f"[bold yellow]Job {stale.id} marked interrupted[/bold yellow] — worker gone")

    if job is not None:
        console.print(f"[bold]Job {job}[/bold]: {worker.run_job(ENGINE_ROOT, job)}")
        return

    project = str(_target_root(repo)) if repo else None
    ran = worker.drain(ENGINE_ROOT, project=project, limit=limit)
    if not ran:
        console.print("Nothing queued.")
        return
    for job_id in ran:
        console.print(f"[bold]Job {job_id}[/bold]: {store.get(ENGINE_ROOT, job_id).state}")


@app.command()
def jobs(
    state: str = typer.Option(None, "--state", help="Only show jobs in this state."),
    limit: int = typer.Option(20, "--limit", help="How many jobs to show."),
    repo: Path = typer.Option(
        None, "--repo", help="Only show jobs targeting this repo (default: all)."
    ),
) -> None:
    """Lists submitted jobs and where each one got to."""
    from rich.table import Table

    from core.jobs import store, worker

    for stale in worker.reconcile(ENGINE_ROOT):
        console.print(f"[bold yellow]Job {stale.id} marked interrupted[/bold yellow] — worker gone")

    rows = store.recent(
        ENGINE_ROOT,
        limit=limit,
        state=state,
        project=str(_target_root(repo)) if repo else None,
    )
    if not rows:
        console.print("No jobs submitted yet.")
        return

    table = Table(title="Jobs")
    for column in ("id", "state", "submitted", "request", "stage", "branch", "summary"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row.id),
            f"[{_STATE_STYLE.get(row.state, 'white')}]{row.state}[/]",
            row.submitted_at[:19].replace("T", " "),
            row.request[:40],
            row.stage or "-",
            row.branch or "-",
            row.summary or "-",
        )
    console.print(table)


@app.command()
def status(job_id: int = typer.Argument(..., help="Job to describe.")) -> None:
    """Shows one job in full, including how it reached its current state.

    Readable from any process at any time — that is the point of persisting
    the lifecycle rather than holding it in the submitting terminal.
    """
    from rich.table import Table

    from core.jobs import store, worker

    worker.reconcile(ENGINE_ROOT)
    try:
        job = store.get(ENGINE_ROOT, job_id)
    except store.JobError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    console.print(
        f"[bold]Job {job.id}[/bold] "
        f"[{_STATE_STYLE.get(job.state, 'white')}]{job.state}[/] — {job.request}"
    )
    fields = [
        ("project", job.project),
        ("submitted", f"{job.submitted_at[:19].replace('T', ' ')} via {job.channel}"),
        ("base", f"{job.base_ref or '-'} @ {job.base_sha[:12] or '-'}"),
        ("branch", job.branch or "-"),
        ("worktree", job.integration_root or "-"),
        ("stage", job.stage or "-"),
        ("attempt", str(job.attempt)),
        ("run_id", str(job.run_id) if job.run_id else "-"),
        ("worker", f"pid {job.worker_pid} on {job.worker_host}" if job.worker_pid else "-"),
        ("heartbeat", job.heartbeat_at[:19].replace("T", " ") if job.heartbeat_at else "-"),
        ("finished", job.finished_at[:19].replace("T", " ") if job.finished_at else "-"),
        ("detail", job.detail or "-"),
    ]
    table = Table(show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for name, value in fields:
        table.add_row(name, value)
    console.print(table)

    history_table = Table(title="Lifecycle")
    for column in ("at", "from", "to", "note"):
        history_table.add_column(column)
    for event in store.events(ENGINE_ROOT, job_id):
        history_table.add_row(
            event["at"][:19].replace("T", " "),
            event["from_state"] or "-",
            event["to_state"],
            event["note"] or "",
        )
    console.print(history_table)

    if job.integration_root:
        console.print(
            f"[dim]Its worktree is still on disk — inspect it at {job.integration_root}[/dim]"
        )

    if job.state == store.INTERRUPTED:
        done = _completed_stages(job)
        kept = f"keeping {', '.join(done)}" if done else "nothing merged yet, so from the start"
        console.print(
            f"Resume it with [bold]ai-platform resume {job.id}[/bold] — {kept}."
        )


@app.command()
def resume(
    job_id: int = typer.Argument(..., help="Interrupted job to pick back up."),
    detach: bool = typer.Option(
        True,
        "--detach/--no-detach",
        help="Start a worker immediately. --no-detach only re-queues it, "
        "for a separate `ai-platform work` to pick up.",
    ),
) -> None:
    """Continues an interrupted run instead of starting it over.

    A worker that dies leaves its branch, its integration worktree and every
    stage it merged intact — this puts the same job back in the queue so a new
    worker carries on from there. Stages already on the branch are not run
    again; verification and review are, since the tree they judged has moved.
    """
    from core.jobs import store, worker

    try:
        job = store.get(ENGINE_ROOT, job_id)
        store.resume(ENGINE_ROOT, job_id)
    except store.JobError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    console.print(f"[bold]Job {job_id}[/bold] re-queued — {job.request}")
    done = _completed_stages(job)
    if done:
        console.print(f"Skipping {len(done)} stage(s) already merged: {', '.join(done)}")
    else:
        console.print(
            "[dim]No merged stage to keep — this will run the workflow from the start.[/dim]"
        )

    if detach:
        pid = worker.spawn_detached(ENGINE_ROOT, job_id)
        console.print(f"Worker started (pid {pid}).")
    console.print(f"Follow it with: [bold]ai-platform status {job_id}[/bold]")


def _completed_stages(job) -> list[str]:
    """Stage ids an interrupted job has already merged onto its branch.

    Empty whenever there is nothing recoverable — no worktree, no checkpoint,
    or one this engine version cannot read — which is exactly when a resume
    will start over, so the caller can say so before spending anything.
    """
    from core.orchestrator import checkpoint

    if not job.integration_root:
        return []
    state = checkpoint.load(Path(job.integration_root))
    return sorted(state.completed_ids) if state else []


@app.command()
def approvals(
    show_all: bool = typer.Option(
        False, "--all", help="Also show decided and expired requests."
    ),
) -> None:
    """Lists actions waiting on a decision.

    A remote request buys one thing: the run. Anything whose consequences
    outlive it — a push, a deployment, an overrun of the token budget — is a
    separate decision, and this is where those queue up (issue #28).
    """
    from rich.table import Table

    from core.jobs import approvals as approvals_store

    # Deterministic rather than lazy: a request past its expiry that still read
    # `pending` would say "waiting for you", which is the one thing it is not.
    expired = approvals_store.expire_stale(ENGINE_ROOT)
    if expired:
        console.print(f"[dim]{expired} request(s) expired without a decision[/dim]")

    rows = approvals_store.pending(ENGINE_ROOT)
    if not rows and not show_all:
        console.print("Nothing waiting for a decision.")
        return

    table = Table(title="Approvals")
    for column in ("id", "state", "requested", "what", "by", "expires"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row.id),
            row.state,
            row.requested_at[:19].replace("T", " "),
            row.describe(),
            row.requested_by or "-",
            row.expires_at[:19].replace("T", " "),
        )
    console.print(table)
    console.print("Decide with: [bold]ai-platform approve <id>[/bold] or [bold]deny <id>[/bold]")


@app.command()
def approve(
    approval_id: int = typer.Argument(..., help="Approval to grant."),
    note: str = typer.Option("", "--note", help="Recorded with the decision."),
) -> None:
    """Grants one pending action — that action, with the inputs it was shown
    against. A later change to the diff, target, command or amount needs a new
    decision."""
    _decide(approval_id, approved=True, note=note)


@app.command()
def deny(
    approval_id: int = typer.Argument(..., help="Approval to refuse."),
    note: str = typer.Option("", "--note", help="Recorded with the decision."),
) -> None:
    """Refuses one pending action. Terminal: a denial is a decision, not a
    pause."""
    _decide(approval_id, approved=False, note=note)


def _decide(approval_id: int, *, approved: bool, note: str) -> None:
    from core.jobs import approvals as approvals_store
    from core.jobs.envelope import Principal

    try:
        decided = approvals_store.decide(
            ENGINE_ROOT,
            approval_id,
            approved=approved,
            principal=str(Principal.local()),
            note=note,
        )
    except approvals_store.ApprovalError as exc:
        console.print(f"[bold red]Refused:[/bold red] {exc}")
        raise typer.Exit(1)

    verb = "Approved" if approved else "Denied"
    console.print(f"[bold]{verb} {decided.id}[/bold]: {decided.describe()}")
    if approved and decided.job_id:
        console.print(
            f"Resume its job with: [bold]ai-platform resume {decided.job_id}[/bold]"
        )


@app.command()
def cancel(job_id: int = typer.Argument(..., help="Job to cancel.")) -> None:
    """Cancels a job, or asks a running one to stop."""
    from core.jobs import store

    try:
        if store.cancel(ENGINE_ROOT, job_id):
            if store.get(ENGINE_ROOT, job_id).state == store.CANCEL_REQUESTED:
                console.print(
                    f"[bold]Job {job_id}[/bold] asked to stop — it reports "
                    f"[bold]cancelled[/bold] once its worker has actually stopped."
                )
            else:
                console.print(f"[bold]Job {job_id}[/bold] cancelled.")
        else:
            console.print(
                f"Job {job_id} already finished as "
                f"[bold]{store.get(ENGINE_ROOT, job_id).state}[/bold] — nothing to cancel."
            )
    except store.JobError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)


@app.command(name="service-health")
def service_health(
    json_output: bool = typer.Option(False, "--json", help="Print a machine-readable report."),
    env_file: Path = typer.Option(None, "--env-file", help="Load explicit KEY=VALUE service settings."),
    readiness: bool = typer.Option(True, "--readiness/--no-readiness"),
    liveness: bool = typer.Option(True, "--liveness/--no-liveness"),
) -> None:
    """Run local liveness/readiness probes without contacting a remote service."""
    from core import service
    try:
        if env_file:
            service.load_env_file(env_file)
        config = service.ServiceConfig.from_env(ENGINE_ROOT)
        report = service.health(config)
    except (OSError, ValueError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)
    if json_output:
        console.print(service.health_json(config))
    else:
        from rich.table import Table
        table = Table(title="ai-platform service health")
        table.add_column("scope"); table.add_column("status"); table.add_column("check"); table.add_column("detail")
        checks = (("liveness", report.liveness) if liveness else ())
        checks += (("readiness", report.readiness) if readiness else ())
        for scope, entries in checks:
            for check in entries:
                style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[check.status]
                table.add_row(scope, f"[{style}]{check.status}[/{style}]", check.name, check.detail)
        console.print(table)
    if readiness and not report.ready:
        raise typer.Exit(1)


@app.command(name="service-run")
def service_run(
    once: bool = typer.Option(False, "--once", help="Run one worker cycle and exit."),
    env_file: Path = typer.Option(None, "--env-file", help="Load explicit KEY=VALUE service settings."),
    log: Path = typer.Option(None, "--log", help="Append service logs to this file."),
) -> None:
    """Run the local durable worker with bounded restart backoff."""
    from dataclasses import replace
    from core import service
    try:
        if env_file:
            service.load_env_file(env_file)
        config = service.ServiceConfig.from_env(ENGINE_ROOT)
        if log:
            config = replace(config, log_path=log)
        code = service.run_forever(config, once=once)
    except (OSError, ValueError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)
    if code:
        raise typer.Exit(code)


@app.command()
def backup(
    destination: Path = typer.Option(None, "--destination", help="Backup directory (default: <engine>/backups)."),
    keep: int = typer.Option(7, "--keep", min=1, help="Number of snapshots to retain."),
) -> None:
    """Create a WAL-aware, integrity-checked backup of engine databases."""
    from core import backup as backup_store
    try:
        result = backup_store.create(ENGINE_ROOT, destination, keep=keep)
    except (OSError, backup_store.BackupError) as exc:
        console.print(f"[bold red]Backup failed:[/bold red] {exc}")
        raise typer.Exit(1)
    console.print(f"Backup created: [bold]{result.path}[/bold]")
    console.print(f"Included: {', '.join(result.files) or 'none'}")
    if result.skipped:
        console.print(f"Skipped (not present): {', '.join(result.skipped)}")


@app.command()
def restore(
    backup_path: Path = typer.Argument(..., help="Snapshot directory containing manifest.json."),
    force: bool = typer.Option(False, "--force", help="Restore despite active jobs; stop service first."),
) -> None:
    """Restore checked SQLite snapshots; stop the service before invoking this command."""
    from core import backup as backup_store
    try:
        restored = backup_store.restore(ENGINE_ROOT, backup_path, force=force)
    except (OSError, backup_store.BackupError) as exc:
        console.print(f"[bold red]Restore refused:[/bold red] {exc}")
        raise typer.Exit(1)
    console.print(f"Restored: {', '.join(restored) or 'none'}")


@app.command(name="security-check")
def security_check(
    json_output: bool = typer.Option(False, "--json", help="Print a machine-readable readiness report."),
) -> None:
    """Evaluate the fail-closed gate before enabling remote exposure."""
    from rich.table import Table
    from core import security_readiness

    report = security_readiness.evaluate(ENGINE_ROOT)
    if json_output:
        typer.echo(security_readiness.report_json(report))
    else:
        table = Table(title=f"Remote security readiness: {report.decision}")
        table.add_column("status")
        table.add_column("check")
        table.add_column("detail")
        for check in report.checks:
            style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[check.status]
            detail = check.detail
            if check.remediation:
                detail += f"\nFix: {check.remediation}"
            table.add_row(f"[{style}]{check.status}[/{style}]", check.name, detail)
        console.print(table)
        if report.risk_acceptance:
            console.print(
                f"[bold yellow]Risk acceptance:[/bold yellow] {report.risk_acceptance.identifier} "
                f"by {report.risk_acceptance.owner} until {report.risk_acceptance.expires_at}"
            )
    if not report.operator_go:
        raise typer.Exit(1)


_STATE_STYLE = {
    "queued": "cyan",
    "running": "yellow",
    "waiting_approval": "magenta",
    "cancel_requested": "dim yellow",
    "succeeded": "green",
    "failed": "red",
    "cancelled": "dim",
    "interrupted": "bold yellow",
}


def main() -> None:
    app()


if __name__ == "__main__":
    main()
