"""Provider Codex CLI — non implémenté.

Les flags exacts du CLI Codex (invocation non-interactive, passage du
contexte, restriction des outils, format de sortie) n'ont pas pu être
vérifiés (binaire absent de cet environnement, pas d'agent de référence
équivalent à claude-code-guide pour Codex). Décision explicite : ne pas
deviner cette interface — implémenter ce provider seulement une fois la
syntaxe réelle confirmée (ex. sortie de `codex --help` fournie par
l'utilisateur, ou test manuel).
"""

from __future__ import annotations

from providers.base import AgentTask, ProviderResult


def run(task: AgentTask) -> ProviderResult:
    raise NotImplementedError(
        "Provider codex_cli non implémenté : la syntaxe du CLI Codex n'a pas été "
        "vérifiée. Fournis `codex --help` (ou l'équivalent) pour l'implémenter."
    )
