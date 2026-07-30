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

app = typer.Typer(help="ai-platform — prototype 1: request -> RAG context -> agent -> verified modification.")


@app.callback()
def callback() -> None:
    """ai-platform — prototype 1: request -> RAG context -> agent -> verified modification."""


@app.command()
def run(
    request: str = typer.Argument(..., help="Natural-language request to carry out on the repo."),
    agent: str = typer.Option("backend", help="Agent role to use (see config/agents.yaml)."),
) -> None:
    """Indexes the repo, selects relevant context, drives the provider, applies and verifies the result."""
    from core.orchestrator import supervisor

    try:
        report = supervisor.run(REPO_ROOT, request, agent)
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    if report.summary != "done":
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
