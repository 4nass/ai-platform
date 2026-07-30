"""Point d'entrée CLI du prototype 1 (Hermes minimal)."""

from __future__ import annotations

import sys
from pathlib import Path

# core/ et providers/ vivent à la racine du repo (hors du package installé
# src/ai_platform) : on les rend importables en ajoutant la racine du repo à
# sys.path, indépendamment du cwd depuis lequel le script est lancé.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import typer  # noqa: E402

app = typer.Typer(help="ai-platform — prototype 1 : demande -> contexte RAG -> agent -> modification vérifiée.")


@app.callback()
def callback() -> None:
    """ai-platform — prototype 1 : demande -> contexte RAG -> agent -> modification vérifiée."""


@app.command()
def run(
    request: str = typer.Argument(..., help="Demande en langage naturel à réaliser sur le repo."),
    agent: str = typer.Option("backend", help="Rôle d'agent à mobiliser (voir config/agents.yaml)."),
) -> None:
    """Indexe le repo, sélectionne le contexte pertinent, pilote le provider, applique et vérifie le résultat."""
    from core.context.manager import ContextManager
    from core.orchestrator import planner, scheduler, supervisor

    context_manager = ContextManager(REPO_ROOT)

    n_chunks = context_manager.index_repo()
    typer.echo(f"Repo indexé : {n_chunks} chunks.")

    selected_context = context_manager.select_context(request)
    tasks = planner.plan(request)
    supervisor.apply_and_verify(
        REPO_ROOT,
        request,
        run_provider=lambda: scheduler.execute(REPO_ROOT, tasks, selected_context, agent),
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
