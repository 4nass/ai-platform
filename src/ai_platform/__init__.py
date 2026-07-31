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


def _target_root(repo: Path | None) -> Path:
    return (repo or Path.cwd()).resolve()


@app.callback()
def callback() -> None:
    """ai-platform — request -> RAG context -> task DAG -> verified modification."""


@app.command()
def run(
    request: str = typer.Argument(..., help="Natural-language request to carry out on the repo."),
    repo: Path = REPO_OPTION,
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
) -> None:
    """Indexes the repo, selects relevant context, and runs the workflow DAG (see config/workflow.yaml)."""
    from core.orchestrator import supervisor

    try:
        report = supervisor.run(ENGINE_ROOT, _target_root(repo), request, dry_run=dry_run, session_id=session)
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

    manager = ContextManager(_target_root(repo), engine_root=ENGINE_ROOT)
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
    runs: int = typer.Option(20, "--runs", help="How many recent runs to show."),
    session: str = typer.Option(None, "--session", help="Only show runs from this session."),
) -> None:
    """Shows what recent runs cost — tokens, price, duration, outcome.

    Telemetry is shared across every repo the engine has been pointed at
    (see core.telemetry.store), so this scopes to the resolved --repo —
    self-targeting by default, meaning nothing changes if you never pass one.
    """
    from rich.table import Table

    from core.telemetry import store as telemetry

    rows = telemetry.recent_runs(
        ENGINE_ROOT, limit=runs, session_id=session, target_repo=str(_target_root(repo))
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
) -> None:
    """Shows which provider would serve a role, and why — without running it.

    The counterpart to `context`: no agent is invoked and no quota is spent, so
    the routing thresholds can be tuned against real history for free.
    """
    from rich.table import Table

    import yaml

    from core.orchestrator import router as routing
    from core.orchestrator import scheduler

    if role:
        roles = [role]
    else:
        config = yaml.safe_load((ENGINE_ROOT / routing.AGENTS_CONFIG_PATH).read_text(encoding="utf-8")) or {}
        roles = sorted(config)

    for name in roles:
        try:
            decision = scheduler.route_agent(ENGINE_ROOT, name)
        except Exception as exc:
            console.print(f"[bold red]{name}:[/bold red] {exc}")
            continue

        table = Table(title=f"{name} → {decision.provider}")
        for column in ("rank", "provider", "quota", "success", "calls", "decision"):
            table.add_column(column)

        for candidate in decision.candidates:
            quota_ratio = f"{candidate.quota_ratio:.0%}" if candidate.quota_ratio is not None else "-"
            success = f"{candidate.success_rate:.0%}" if candidate.success_rate is not None else "-"
            style = "green" if candidate.chosen else "dim"
            table.add_row(
                str(candidate.rank),
                candidate.provider,
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
    actually recorded against the limits declared in config/quota.yaml.
    """
    from rich.table import Table

    from core.telemetry import quota as quota_store

    rows = quota_store.pressure(ENGINE_ROOT, window_hours=window)
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
