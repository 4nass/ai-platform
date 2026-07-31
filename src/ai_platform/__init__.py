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
) -> None:
    """Indexes the repo, selects relevant context, and runs the workflow DAG (see config/workflow.yaml)."""
    from core.orchestrator import supervisor

    try:
        report = supervisor.run(REPO_ROOT, request, dry_run=dry_run)
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    if dry_run:
        return

    if report.summary != "done":
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
