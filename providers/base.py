"""Interface commune à tous les providers (CLI ou API).

Contrat : au retour de `run()`, le disque est déjà à jour — peu importe
comment (un provider CLI édite lui-même via ses propres outils ; un
provider API doit écrire les fichiers reçus avant de retourner). Ça permet
à l'orchestrateur de rester agnostique au provider utilisé.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

PROMPTS_DIR = Path("prompts")


@dataclass
class AgentTask:
    agent: str
    description: str
    repo_root: Path
    context_paths: list[str] = field(default_factory=list)
    context_render: str = ""
    """Contexte complet (contenu des fichiers, git diff, mémoire) pour les
    providers API sans accès disque. Les providers CLI utilisent plutôt
    `context_paths` (juste les chemins — ils lisent les fichiers eux-mêmes)."""


@dataclass
class ProviderResult:
    success: bool
    summary: str
    raw: object = None


class Provider(Protocol):
    def run(self, task: AgentTask) -> ProviderResult: ...


def load_role_prompt(repo_root: Path, agent: str) -> str:
    path = repo_root / PROMPTS_DIR / f"{agent}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
