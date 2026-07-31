"""CLI entry point for prototype 1 (minimal Hermes)."""

from __future__ import annotations

import sys
from pathlib import Path

# core/ and providers/ live at the repo root (outside the installed
# src/ai_platform package): make them importable by adding the repo root to
# sys.path, regardless of the cwd the script is launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402

console = Console()

app = typer.Typer(help="ai-platform — request -> RAG context -> task DAG -> verified modification.")


@app.callback()
def callback() -> None:
    """ai-platform — request -> RAG context -> task DAG -> verified modification."""


@app.command()
def run(
    request: str = typer.Argument(..., help="Natural-language request to carry out on the repo."),
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
        report = supervisor.run(REPO_ROOT, request, dry_run=dry_run, session_id=session)
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    if dry_run:
        return

    if report.summary != "done":
        raise typer.Exit(1)


@app.command()
def history(
    runs: int = typer.Option(20, "--runs", help="How many recent runs to show."),
    session: str = typer.Option(None, "--session", help="Only show runs from this session."),
) -> None:
    """Shows what recent runs cost — tokens, price, duration, outcome."""
    from rich.table import Table

    from core.telemetry import store as telemetry

    rows = telemetry.recent_runs(REPO_ROOT, limit=runs, session_id=session)
    if not rows:
        console.print("No runs recorded yet.")
        return

    table = Table(title="Recent runs")
    for column in ("id", "started", "request", "summary", "calls", "in", "out", "cost", "duration"):
        table.add_column(column)

    for row in rows:
        # Flag partial pricing rather than letting an unpriced provider make a
        # run look cheaper than it was.
        priced = row["priced_calls"]
        calls = f"{row['calls']}" if priced == row["calls"] else f"{row['calls']} ({priced} priced)"
        duration = f"{row['duration_ms'] / 1000:.1f}s" if row["duration_ms"] else "-"
        table.add_row(
            str(row["id"]),
            (row["started_at"] or "")[:19].replace("T", " "),
            (row["request"] or "")[:48],
            row["summary"] or "-",
            calls,
            f"{row['input_tokens']:,}",
            f"{row['output_tokens']:,}",
            f"${row['cost_usd']:.4f}",
            duration,
        )

    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
